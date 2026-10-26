---
title: 第四章：Backend——Agent 为什么需要独立的存储与执行抽象
description: 从 Coding Agent 的真实文件操作出发，理解 BackendProtocol、StateBackend、FilesystemBackend、StoreBackend、CompositeBackend 和 Sandbox 执行能力的设计与实现
tags: [DeepAgents, Backend, Architecture, Filesystem, Sandbox, 源码精读]
sidebar_position: 4
---

# 第四章：Backend——为什么工具接口不能直接绑定文件系统？

## 一、承接上一章：`read_file()` 到底在读取什么？

上一章我们已经研究了完整的工具调用过程。

用户希望修复退款 Bug。

模型输出：

```python
{
    "name": "read_file",
    "args": {
        "file_path": "/refund.py"
    },
    "id": "call-001",
}
```

经过 LangChain 与 LangGraph：

```text
模型生成 Tool Call
        |
        v
      ToolNode
        |
        v
找到 read_file 工具
        |
        v
执行 sync_read_file()
        |
        v
调用 backend.read()
```

最后一个调用：

```python
backend.read("/refund.py")
```

就是我们今天的起点。

先考虑最简单的实现。

如果 Agent 运行在自己的电脑上，我们完全可以写：

```python
from pathlib import Path

def read_file(path: str) -> str:
    return Path(path).read_text(
        encoding="utf-8"
    )
```

这样就完成了文件读取。

**那么 DeepAgents 为什么不直接这样写？**

因为这种实现偷偷做出了一个假设：

> Agent 使用的文件一定存在于当前 Python 进程能够访问的本地文件系统中。

对于一个个人开发脚本，这个假设或许成立。

但对于一个通用 Agent Harness，它不一定成立。

---

## 二、一个需求变化，就暴露了直接使用 `open()` 的问题

假设我们要让同一个 Coding Agent 支持三种运行方式。

### 场景 A：本地项目开发

用户在自己的电脑上运行 Agent。

代码位于：

```text
/home/developer/shop/refund.py
```

Agent 需要读取真实文件、修改代码，并运行测试。

### 场景 B：网页中的临时 Agent

用户在网页上提供一段代码，希望 Agent 分析和修改。

这时代码可能存储在当前会话状态中：

```python
{
    "files": {
        "/refund.py": {
            "content": "...",
            "encoding": "utf-8",
        }
    }
}
```

并不存在一个必须写入宿主机的：

```text
/refund.py
```

### 场景 C：远程隔离环境

Agent 运行在服务端，但代码和测试环境位于远程容器中。

```text
Agent Service
      |
      v
Remote Container
      |
      +-- refund.py
      +-- test_refund.py
      +-- Python / pytest
```

如果 `read_file()` 写死：

```python
Path(path).read_text()
```

那么它只会读取 Agent 服务进程所在机器的文件。

它不会自动读取远程容器中的文件。

### 于是产生一个设计矛盾

我们希望模型看到的工具保持一致：

```python
read_file("/refund.py")
```

但实际数据位置却可能不同：

```text
Graph State
Local Filesystem
Remote Container
Persistent Store
```

如果为每一种环境分别创建不同的模型工具：

```text
read_state_file
read_local_file
read_remote_file
read_store_file
```

就会带来新的问题：

- 模型需要理解更多工具。
- 工具描述变得复杂。
- Agent Prompt 必须适配环境。
- 切换环境可能需要重新编排工具。
- 相同的读取逻辑被重复实现。

所以我们需要一种机制：

**保持模型调用接口稳定，同时允许底层存储和执行方式发生变化。**

这就是 Backend 抽象要解决的第一个核心问题。

---

## 三、由问题推导设计：在 Tool 和真实存储之间增加一层接口

我们刚才有：

```text
read_file Tool
     |
     v
Path.read_text()
```

它将工具绑定到了本地文件系统。

现在我们希望变成：

```text
                  read_file Tool
                        |
                        v
                   backend.read()
                        |
            +-----------+-----------+
            |           |           |
            v           v           v
         Graph State  Local Disk  Remote Storage
```

模型仍然使用：

```python
read_file(
    file_path="/refund.py"
)
```

Tool 的主要逻辑也保持一致：

```python
result = backend.read(
    "/refund.py"
)
```

变化的是：

```python
backend
```

这个对象的具体实现。

如果 Backend 使用 Graph State：

```text
backend.read()
       |
       v
Graph State["files"]
```

如果 Backend 使用本地磁盘：

```text
backend.read()
       |
       v
Local Filesystem
```

如果 Backend 连接远程环境：

```text
backend.read()
       |
       v
Remote Execution Environment
```

### 这里体现什么设计原则？

这是**依赖倒置与面向接口编程**。

高层的 Tool 不应该直接依赖某一个具体存储实现。

它应该依赖稳定的能力接口。

于是：

```text
高层：FilesystemMiddleware
          |
          v
抽象：BackendProtocol
          ^
          |
低层：StateBackend / FilesystemBackend / ...
```

这样可以把：

**工具需要做什么**

与：

**操作具体在哪里完成**

分开。

这是 Backend 设计最根本的价值。

---

## 四、现在进入真实源码：BackendProtocol

打开：

`deepagents/backends/protocol.py`

找到：

```python
class BackendProtocol(abc.ABC):
    ...
```

这个类提供一套统一的文件操作接口。

选取其中最关键的方法，可以简化为：

```python
# 根据真实源码提取的接口节选

class BackendProtocol(abc.ABC):

    def ls(self, path: str) -> LsResult:
        raise NotImplementedError

    def read(
        self,
        file_path: str,
        offset: int = 0,
        limit: int = 2000,
    ) -> ReadResult:
        raise NotImplementedError

    def write(
        self,
        file_path: str,
        content: str,
    ) -> WriteResult:
        raise NotImplementedError

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        raise NotImplementedError
```

实际协议还有：

```text
delete
glob
grep
upload_files
download_files
以及对应异步方法
```

我们先不要急着研究全部方法。

只关注一个设计事实：

**不同 Backend 必须通过一致的方法名称和结果类型，为上层 Tool 提供文件能力。**

### 为什么还要统一返回结果？

假设不同 Backend 分别返回：

```python
# 本地文件系统
"文件内容"
```

```python
# 状态存储
{"data": "文件内容"}
```

```python
# 远程容器
{"stdout": "文件内容", "exit_code": 0}
```

那么 `read_file` Tool 就必须知道：

```text
当前使用哪个 Backend？
它会返回什么格式？
怎样判断错误？
```

这又把 Tool 和 Backend 绑定起来了。

所以统一接口不仅要统一输入，还必须统一输出。

---

## 五、Backend 为什么返回 `ReadResult`，而不是直接返回字符串？

源码中定义：

```python
@dataclass
class ReadResult:
    error: str | None = None
    file_data: FileData | None = None
    total_lines: int | None = None
    start_line: int | None = None
    end_line: int | None = None
    next_offset: int | None = None
    no_lines_requested: bool = False
```

这里省略了部分文档和校验逻辑。

我们再回到退款文件。

模型请求：

```python
read_file(
    file_path="/refund.py",
    offset=100,
    limit=50,
)
```

文件可能有 1000 行。

Backend 不能只返回文本，还应该能描述：

```text
这次读取是否成功？
返回的是什么内容？
文件总共有多少行？
本次从哪一行读到哪一行？
下一次应该从什么位置继续读？
```

因此才需要：

```python
ReadResult
```

而不是简单返回：

```python
str
```

### 但是模型最终看到的不是 `ReadResult`

回忆上一章：

```text
Backend.read()
      |
      v
   ReadResult
      |
      v
_handle_read_result()
      |
      v
   ToolMessage
      |
      v
     Model
```

为什么还要经过 `_handle_read_result()`？

因为两个层次关心的东西不同。

**Backend 关心文件操作的结构化结果。**

**Tool 关心怎样把这个结果变成模型可以理解的消息。**

例如：

```text
Backend Result：
    file_data
    start_line
    end_line
    next_offset
```

转换成模型可见内容后，可能表现为带行号的源码：

```text
101  def calculate_refund(...):
102      ...
103      ...
```

所以：

```text
Backend Result ≠ ToolMessage
```

这种分离意味着以后更换 Backend 时，上层的消息格式仍然可以保持一致。

---

## 六、最简单的 Backend：StateBackend

现在我们有了统一接口。

下一步要实现它。

先回到前面提到的场景 B：

> 用户只在当前 Agent 会话中提供一份临时代码，不需要直接修改服务器磁盘上的真实项目。

这时文件可以存储在 Graph State 中。

我们在第 05 章已经研究过 State：

```python
state = {
    "messages": [...],
    "files": {...},
}
```

现在就可以用：

```python
state["files"]
```

表示虚拟文件系统。

这就是 `StateBackend` 的基本设计。

### 6.1 真实源码：默认使用 StateBackend

打开：

`deepagents/graph.py`

在 `create_deep_agent()` 中找到：

```python
backend = (
    backend
    if backend is not None
    else StateBackend()
)
```

这说明：

当开发者没有显式传入 Backend 时，DeepAgents 默认使用：

```python
StateBackend()
```

为什么默认不直接选择 `FilesystemBackend()`？

一个重要原因是：

**通用 Agent 的默认文件能力不应该自动指向宿主机真实文件系统。**

StateBackend 为 Agent 提供的是 Graph State 中的虚拟文件存储。

它不需要为每一份临时文件创建宿主机上的真实文件。

不过，这不表示仅使用 StateBackend 就自动具备完整的安全沙箱；它解决的是存储位置和执行能力的选择问题。

---

## 七、StateBackend 怎么从 Graph State 读取文件？

打开：

`deepagents/backends/state.py`

找到：

```python
class StateBackend(BackendProtocol):
    ...
```

我们想象它的实现可能是：

```python
class StateBackend:

    def __init__(self, state):
        self.state = state

    def read(self, path):
        return self.state["files"][path]
```

但真实源码没有采用这种简单方式。

为什么？

因为 LangGraph State 并不是一个随意共享和修改的普通 Python 字典。

上一章讲过：

```text
Graph State
    |
    v
Channel
    |
    v
Reducer
    |
    v
State Update
```

如果 Backend 长期持有某个旧的 State 字典引用，就可能读到过时数据。

如果它绕过 Channel 直接修改 State，也可能破坏 LangGraph 的状态更新协议。

因此，StateBackend 必须通过 LangGraph Runtime 提供的接口访问状态。

### 7.1 真实源码：读取当前 State

找到：

```python
def _read_files(self) -> dict[str, Any]:
    ...
```

其核心过程是：

```python
config = self._get_config()
```

再通过 LangGraph 的：

```python
CONFIG_KEY_READ
```

读取当前 `files` Channel。

源码还使用 `fresh=True`，以便在当前执行阶段应用待处理的状态更新，获得相应的 read-your-writes 行为。

这里出现的：

```python
CONFIG_KEY_READ
```

不是模型参数。

它是 LangGraph Runtime 在执行期间提供的内部状态读取能力。

### 为什么必须通过 Runtime？

因为 StateBackend 需要回答：

> 当前正在运行的这个 Agent Thread，其最新 `files` 状态是什么？

而不能只是读取对象创建时保存的某个旧字典。

这说明 StateBackend 虽然实现了普通文件操作接口，但它的内部依赖 LangGraph 的执行上下文。

---

## 八、StateBackend 怎样写入文件？

读取已经解决。

现在考虑修改 `refund.py`。

例如模型调用：

```python
write_file(
    file_path="/refund.py",
    content="新的代码内容",
)
```

如果 Backend 直接执行：

```python
state["files"]["/refund.py"] = content
```

就会绕过 Graph State 更新机制。

所以，真实源码采用另一种方式。

在：

`deepagents/backends/state.py`

中找到：

```python
def _send_files_update(
    self,
    update: dict[str, Any],
) -> None:
    ...
```

核心实现：

```python
config = self._get_config()

send = config["configurable"][CONFIG_KEY_SEND]

send([("files", update)])
```

例如向 Graph Runtime 提交：

```python
{
    "/refund.py": {
        "content": "...",
        "encoding": "utf-8",
        ...
    }
}
```

其中：

```python
"files"
```

就是 State Channel 的名称。

### 为什么不是提交整个文件字典？

假设当前有：

```python
state["files"] = {
    "/refund.py": ...,
    "/test_refund.py": ...,
    "/coupon_service.py": ...,
}
```

现在只修改：

```text
/refund.py
```

就不需要每次提交全部文件内容。

只需要发送该路径的更新。

之后由 Graph State 的合并规则维护最终状态。

所以完整关系是：

```text
               write_file Tool
                      |
                      v
                StateBackend.write
                      |
                      v
                  新文件内容
                      |
                      v
                CONFIG_KEY_SEND
                      |
                      v
                files Channel
                      |
                      v
                 File Reducer
                      |
                      v
                Updated Graph State
```

### 一个重要限制

`StateBackend` 必须运行在能够提供相应 LangGraph Runtime 配置的上下文中。

因此，通常不能在普通脚本顶层直接写：

```python
backend = StateBackend()

backend.write(
    "/refund.py",
    "..."
)
```

然后期待它像本地文件系统一样工作。

源码会检查运行时配置；如果没有 `CONFIG_KEY_READ` 等必要内部能力，就会报错。

**这不是 StateBackend 的缺陷，而是它选择依赖 Graph State 的结果。**

---

## 九、StateBackend 解决了什么，又没有解决什么？

我们现在已经理解它的核心实现。

它很适合：

```text
临时文件
当前任务中间产物
随 Agent Thread 保存的虚拟文件
不需要宿主机真实文件的工作流
```

但是，回到原始退款任务。

用户还要求：

> 修改真实项目，并运行 pytest。

StateBackend 有两个明显限制。

### 限制一：它不等于真实项目目录

如果真实文件在：

```text
/home/developer/shop/refund.py
```

那么修改：

```python
state["files"]["/refund.py"]
```

不代表真实项目文件也被修改了。

### 限制二：它不能运行 Shell 命令

即使 StateBackend 保存了：

```text
refund.py
test_refund.py
```

也不意味着它能够运行：

```bash
pytest
```

因为保存文件和执行程序是两种不同能力。

所以，现在需要第二种 Backend。

---

## 十、FilesystemBackend：文件真的存在本地磁盘时怎么办？

假设我们正在开发一个本地 Coding Agent。

真实项目是：

```text
/home/developer/shop/
    |
    +-- refund.py
    +-- coupon_service.py
    +-- tests/
        |
        +-- test_refund.py
```

我们需要让 Agent 的：

```python
read_file("/refund.py")
```

真正访问这个项目里的文件。

这时就应该使用：

```python
FilesystemBackend
```

### 10.1 真实源码

打开：

`deepagents/backends/filesystem.py`

找到：

```python
class FilesystemBackend(BackendProtocol):
    ...
```

初始化：

```python
backend = FilesystemBackend(
    root_dir="/home/developer/shop",
    virtual_mode=True,
)
```

这里的关键不是 `root_dir` 本身，而是：

```python
virtual_mode=True
```

为什么需要它？

因为模型使用的是：

```python
"/refund.py"
```

但真实文件位于：

```text
/home/developer/shop/refund.py
```

我们需要一套统一的路径解释规则。

---

## 十一、为什么需要虚拟路径？

假设我们希望相同的 Agent Tool Call：

```python
read_file(
    file_path="/refund.py"
)
```

既能够被 StateBackend 理解，也能够被 FilesystemBackend 理解。

那么模型不应该每次都知道真实机器的目录结构。

对于本地项目：

```text
Agent Path:
    /refund.py

Physical Path:
    /home/developer/shop/refund.py
```

于是：

```text
                 Model Tool Call
                       |
                       v
                 /refund.py
                       |
                       v
                 FilesystemBackend
                       |
                       v
                  Path Mapping
                       |
                       v
          /home/developer/shop/refund.py
```

这种设计同时解决了两个问题。

**第一：为 Agent 提供稳定的路径命名空间。**

**第二：在虚拟路径模式下，将文件操作限制在指定项目根目录内。**

这里的命名空间可以理解为：

> 模型看到的文件路径体系。

它不必与宿主机的真实路径完全相同。

---

## 十二、真实源码：FilesystemBackend 如何解析路径？

找到：

```python
def _resolve_path(self, key: str) -> Path:
    ...
```

在：

```python
virtual_mode=True
```

时，源码核心逻辑如下：

```python
if self.virtual_mode:

    vpath = (
        key
        if key.startswith("/")
        else "/" + key
    )

    if ".." in vpath or vpath.startswith("~"):
        raise ValueError(
            "Path traversal not allowed"
        )

    full = (
        self.cwd / vpath.lstrip("/")
    ).resolve()

    try:
        full.relative_to(self.cwd)
    except ValueError:
        raise ValueError(
            "Path outside root directory"
        )

    return full
```

以上是根据真实源码整理的核心分支，省略了符号链接循环等检查。

我们用具体例子理解。

### 正常路径

```python
backend = FilesystemBackend(
    root_dir="/home/developer/shop",
    virtual_mode=True,
)
```

模型请求：

```python
read_file("/refund.py")
```

解析为：

```text
/home/developer/shop/refund.py
```

### 非法路径

模型请求：

```python
read_file("/../secrets.txt")
```

这种请求试图通过路径回退访问根目录之外的内容。

源码会进行路径穿越检查并拒绝。

### 为什么还要检查 `relative_to()`？

因为即使初始路径看起来正常，符号链接也可能指向项目根目录之外。

所以源码不仅检查原始路径字符串，还要检查解析后的真实路径是否仍处于指定根目录下。

这里需要注意，路径解析检查不等于完整的操作系统沙箱，也不能仅靠它消除所有文件系统竞争条件。

### virtual_mode=False 又意味着什么？

源码在非虚拟模式下保留较宽松的行为：

```python
if path.is_absolute():
    return path
```

这意味着绝对路径可以绕过 `root_dir` 的相对路径定位作用。

因此：

```python
virtual_mode=False
```

不是“更方便但同样安全”的配置。

它意味着更大的文件系统访问范围。

对于模型驱动的文件操作，不能忽略这一点。

---

## 十三、但 FilesystemBackend 为什么仍然不能执行 pytest？

现在我们已经拥有：

```python
FilesystemBackend(...)
```

它能够访问真实文件。

那么是不是可以：

```python
backend.execute("pytest")
```

了？

**不能直接这样推断。**

因为：

```text
读取、编辑文件
```

与：

```text
启动操作系统进程
```

不是同一类能力。

文件操作需要：

```text
Path
Read
Write
Edit
Delete
```

命令执行则需要：

```text
Process Creation
Environment Variables
Working Directory
Timeout
Exit Code
stdout / stderr
```

而且命令执行有更高的安全风险。

因此 DeepAgents 并没有要求每一个普通 Backend 都支持 Shell。

这就自然引出另一种接口。

---

## 十四、为什么需要单独的 SandboxBackendProtocol？

打开：

`deepagents/backends/protocol.py`

找到：

```python
class SandboxBackendProtocol(
    BackendProtocol
):
    ...
```

它在基本 Backend 能力之上增加：

```python
def execute(
    self,
    command: str,
    *,
    timeout: int | None = None,
) -> ExecuteResponse:
    ...
```

以及对应的：

```python
aexecute(...)
```

这里的继承关系是：

```text
                   BackendProtocol
                          |
                          v
                   File Operations
                          |
                          |
                SandboxBackendProtocol
                          |
                          v
                   Command Execution
```

请注意：

**这里不是说所有 Backend 都必须继承 SandboxBackendProtocol。**

实际上：

```text
StateBackend
    可以保存文件
    不提供通用 Shell 执行

FilesystemBackend
    可以读写本地文件
    不直接提供 execute

LocalShellBackend
    可以读写本地文件
    还可以执行本地 Shell
```

所以 Backend 协议被划分成了不同的能力层次。

### 这是什么设计思想？

这是**能力分离**。

不是要求每个 Backend 实现一个巨大的接口，而是：

```text
需要文件操作：
    使用 BackendProtocol

需要命令执行：
    使用额外的执行协议
```

它使执行能力成为显式选择，而不是文件能力自动附带的权限。

---

## 十五、为什么执行协议返回 ExecuteResponse？

假设 Agent 执行：

```bash
pytest tests/test_refund.py
```

程序可能返回：

```text
stdout:
    12 passed

stderr:
    ...

exit code:
    0
```

所以，仅仅返回：

```python
"12 passed"
```

并不够。

因为真实执行结果至少应该包含：

```text
命令输出
退出码
是否发生截断
其他执行信息
```

DeepAgents 定义：

```python
@dataclass
class ExecuteResponse:
    output: str
    exit_code: int | None = None
    ...
```

为什么退出码重要？

因为：

```text
打印了 "success"
```

不代表命令成功。

对于 Shell，退出码才是判断进程是否成功完成的重要信号。

例如：

```python
result.exit_code == 0
```

通常表示进程正常成功退出。

对于 Coding Agent：

**判断测试是否通过，不能仅依赖模型对一段日志的主观总结。**

还应该结合真实执行结果。

---

## 十六、LocalShellBackend：为什么它不是 Sandbox？

现在我们需要一个真正能执行本地命令的 Backend。

DeepAgents 提供：

```python
LocalShellBackend
```

真实源码：

`deepagents/backends/local_shell.py`

找到：

```python
class LocalShellBackend(
    FilesystemBackend,
    SandboxBackendProtocol,
):
    ...
```

它同时具备：

```text
文件系统操作
Shell 命令执行
```

但是这里存在一个特别重要的安全问题。

我们来看真实执行实现。

### 16.1 真实源码：execute()

关键代码：

```python
result = subprocess.run(
    command,
    check=False,
    shell=True,
    capture_output=True,
    stdin=subprocess.DEVNULL,
    text=True,
    timeout=effective_timeout,
    env=self._env,
    cwd=str(self.cwd),
    start_new_session=(
        sys.platform != "win32"
    ),
)
```

这是对实际源码的核心参数节选。

注意：

```python
shell=True
```

以及：

```python
cwd=str(self.cwd)
```

这说明命令是在当前宿主机的 Shell 环境中执行，只是以指定工作目录作为当前目录。

### 16.2 为什么指定 cwd 不等于 Sandbox？

假设：

```python
LocalShellBackend(
    root_dir="/home/developer/shop",
)
```

然后执行：

```bash
python some_script.py
```

虽然工作目录是：

```text
/home/developer/shop
```

但进程通常仍然拥有当前操作系统用户的权限。

它可能访问：

```text
其他用户可访问的目录
环境变量
网络
其他系统程序
```

`cwd` 只定义命令的工作目录。

它不是：

```text
文件系统隔离
进程隔离
网络隔离
权限隔离
```

因此：

**LocalShellBackend 是本地命令执行适配器，不是安全沙箱。**

源码文档也明确发出了这一警告。

如果需要真正的安全边界，应在受控容器、虚拟机或其他隔离环境中执行，并配合资源限制、权限控制和审批机制。

### 16.3 为什么要保留 LocalShellBackend？

因为本地开发工具确实存在合理需求。

例如：

```text
开发者主动运行一个 Coding Agent
        |
        v
Agent 修改当前项目
        |
        v
Agent 运行项目测试
```

这种场景下，用户可能有意赋予 Agent 本地执行能力。

但生产服务、多人环境、未知代码运行等场景，不应该仅凭 LocalShellBackend 就认为已实现隔离。

**接口支持执行，与执行环境足够安全，是两件完全不同的事。**

---

## 十七、如果需要远程执行环境怎么办？

现在考虑第三种场景。

真实代码位于远程环境中：

```text
Agent Service
      |
      v
Remote Environment
      |
      +-- refund.py
      +-- test_refund.py
      +-- Python
      +-- pytest
```

我们希望 Agent 仍然使用：

```python
read_file(...)
```

以及：

```python
execute(...)
```

但真正执行发生在远程环境。

这就要求具体 Backend 将这些操作转发到远程执行系统。

DeepAgents 提供了：

```python
SandboxBackendProtocol
```

以及用于实现相关文件操作的基础代码。

`BaseSandbox` 的设计思路是：

**当远程环境能够执行命令、传输文件时，可以在这些底层能力之上实现读取、搜索、编辑等更高级的文件操作。**

例如概念上：

```text
                 Agent
                   |
                   v
               read_file()
                   |
                   v
             Remote Backend
                   |
                   v
           Remote Command/API
                   |
                   v
             Remote Filesystem
```

具体远程执行后端仍然需要正确实现：

```text
环境生命周期
命令执行
超时
结果传输
文件上传下载
认证与隔离
```

**实现了 SandboxBackendProtocol，并不自动证明底层真的具有强隔离。**

隔离保障来自实际基础设施，而不是 Python 类名。

---

## 十八、现在还有一个问题：不同类型的文件能否使用不同 Backend？

我们已经解决了：

```text
同一个工具可以更换 Backend
```

但一个真实 Agent 可能同时需要访问不同类型的数据。

例如：

```text
项目源代码
任务临时文件
长期业务知识
历史测试日志
```

这些数据不一定应该保存在相同的位置。

考虑：

```text
/refund.py
/test_refund.py
/memories/refund_rules.md
```

我们可能希望：

```text
refund.py
    -> 本地项目目录

test_refund.py
    -> 本地项目目录

memories/refund_rules.md
    -> 持久化 Store
```

如果一个 Agent 只能使用一个 Backend，就需要在外部写大量分发逻辑。

例如：

```python
if path.startswith("/memories/"):
    return store.read(path)

return filesystem.read(path)
```

这就是新需求：

**保持统一文件路径接口，同时根据路径将操作路由到不同存储。**

DeepAgents 为此提供：

```python
CompositeBackend
```

---

## 十九、CompositeBackend：为什么需要组合存储？

打开：

`deepagents/backends/composite.py`

找到：

```python
class CompositeBackend(BackendProtocol):
    ...
```

构造函数：

```python
def __init__(
    self,
    default: BackendProtocol | StateBackend,
    routes: dict[str, BackendProtocol],
    *,
    artifacts_root: str = "/",
):
    ...
```

这里有两个关键参数。

### default

默认 Backend。

没有匹配到特殊路径时，就使用它。

### routes

路径前缀对应的 Backend。

例如：

```python
CompositeBackend(
    default=filesystem_backend,
    routes={
        "/memories/": store_backend,
    },
)
```

它表达：

```text
其他文件
    -> filesystem_backend

/memories/ 下的文件
    -> store_backend
```

### 19.1 为什么不直接写进 FilesystemMiddleware？

因为 `FilesystemMiddleware` 是面向 Agent 的工具实现。

它应该关心：

```text
模型请求读取什么文件？
参数是否合法？
如何返回 ToolMessage？
```

而：

```text
某条路径应该使用哪个存储系统？
```

属于 Backend 的责任。

因此把路径分发放进 CompositeBackend，能够避免 Tool 承担存储路由逻辑。

---

## 二十、真实源码：CompositeBackend 如何选择 Backend？

找到：

```python
def _get_backend_and_key(
    self,
    key: str,
) -> tuple[BackendProtocol, str]:
    ...
```

核心代码会调用：

```python
_route_for_path(
    default=self.default,
    sorted_routes=self.sorted_routes,
    path=key,
)
```

路由根据路径前缀进行匹配。

例如：

```text
路径：
    /memories/refund_rules.md
```

匹配：

```text
前缀：
    /memories/
```

结果：

```text
Backend：
    store_backend

传给 Backend 的实际路径：
    /refund_rules.md
```

这里有个细节：

**路由前缀会从路径中剥离。**

为什么？

因为子 Backend 不需要知道自己挂载在：

```text
/memories/
```

之下。

它只需要处理自己的内部路径：

```text
/refund_rules.md
```

这就是虚拟挂载点的思想。

### 20.1 为什么要最长前缀匹配？

假设我们配置：

```python
routes = {
    "/memories/": memory_backend,
    "/memories/projects/": project_memory_backend,
}
```

模型请求：

```text
/memories/projects/refund.md
```

它同时符合：

```text
/memories/
```

和：

```text
/memories/projects/
```

应该选择哪个？

通常应该选择更具体的：

```text
/memories/projects/
```

所以源码使用：

```python
self.sorted_routes = sorted(
    routes.items(),
    key=lambda x: len(x[0]),
    reverse=True,
)
```

这样可以优先匹配更长、更具体的路径前缀。

这是文件系统挂载路由和 URL 路由中常见的设计原则。

### 20.2 read() 怎样委派？

在 CompositeBackend 中：

```python
def read(
    self,
    file_path: str,
    offset: int = 0,
    limit: int = 2000,
) -> ReadResult:

    backend, stripped_key = (
        self._get_backend_and_key(file_path)
    )

    return backend.read(
        stripped_key,
        offset=offset,
        limit=limit,
    )
```

这里出现了一个很清晰的结构：

```text
                     read_file()
                         |
                         v
                  CompositeBackend
                         |
                         v
                    路径匹配
                         |
            +------------+------------+
            |                         |
            v                         v
       默认 Backend            特定路径 Backend
            |                         |
            v                         v
       Local Files              Persistent Store
```

模型无需知道底层有多少种存储。

这就是组合 Backend 机制要提供的能力。

---

## 二十一、为什么 CompositeBackend 的 execute() 不按路径路由？

现在假设用户要求：

```python
execute(
    command="pytest tests/test_refund.py"
)
```

我们应该将它路由到哪个 Backend？

注意，这次请求没有：

```python
file_path
```

它只有：

```python
command
```

而一个命令可能访问多个路径：

```bash
pytest tests/test_refund.py
```

它还可能：

```text
读取配置文件
访问环境变量
启动子进程
访问网络
```

因此：

**命令执行不能像文件读写那样，仅根据单个文件路径前缀进行路由。**

DeepAgents 的真实源码也做出了明确选择。

在 `CompositeBackend.execute()` 中：

```python
if isinstance(
    self.default,
    SandboxBackendProtocol,
):
    return self.default.execute(
        command,
        ...
    )
```

如果默认 Backend 不支持执行，就抛出 `NotImplementedError`。

也就是说：

**CompositeBackend 的文件操作可以按路径路由，但 Shell 执行始终委派给默认 Backend。**

这是一个很值得注意的设计取舍。

它让：

```text
文件存储路由
```

与：

```text
进程执行环境
```

保持明确分离。

代价是：

如果你希望不同路径使用不同的远程执行环境，就需要更明确的执行目标和调度机制，而不能简单依赖 CompositeBackend 的路径路由。

---

## 二十二、StoreBackend：为什么长期知识不能只放在 StateBackend？

现在我们来看一个不同的问题。

Agent 修复退款 Bug 的过程中，用户提供了一条长期规则：

> 优惠券分摊后的退款金额，必须保证各项金额之和等于总退款金额。

这条规则不仅对当前任务有用。

以后修复其他退款问题时，也可能需要它。

如果只保存在：

```python
state["files"]
```

中，那么它主要属于当前 Agent Thread 的状态。

其他会话未必能够直接访问。

所以，我们需要一种跨会话的数据存储能力。

这就是：

```python
StoreBackend
```

要解决的问题。

### 22.1 真实源码

打开：

`deepagents/backends/store.py`

找到：

```python
class StoreBackend(BackendProtocol):
    ...
```

它使用 LangGraph 的：

```python
BaseStore
```

保存文件。

其构造函数包含：

```python
def __init__(
    self,
    *,
    namespace: NamespaceFactory,
    store: BaseStore | None = None,
):
    ...
```

这里出现了一个新概念：

```python
namespace
```

为什么需要它？

因为长期存储可能被多个用户、项目或 Agent 共享。

不能把所有人的文件都直接混在同一个无隔离空间里。

例如概念上：

```text
Store
  |
  +-- User A
  |     |
  |     +-- refund_rules.md
  |
  +-- User B
        |
        +-- refund_rules.md
```

虽然文件名相同，但属于不同命名空间。

因此 StoreBackend 需要通过 Namespace 确定当前操作的数据范围。

**Namespace 是数据组织和隔离的一部分，但不应被当成完整的访问控制机制。**

真正的权限检查仍然需要由系统保证。

### 22.2 StoreBackend 与 StateBackend 的区别

| 维度 | StateBackend | StoreBackend |
|---|---|---|
| 主要存储位置 | LangGraph State | LangGraph Store |
| 数据范围 | 当前 Thread 状态 | 可跨 Thread |
| 典型用途 | 临时文件、中间产物 | 长期知识、共享记忆 |
| 持久化依赖 | Checkpointer | 实际 Store 实现 |
| Shell 执行 | 不支持 | 不支持 |

注意：

使用 StoreBackend 不意味着数据自动永久保存。

如果底层是内存 Store，进程结束以后数据仍可能丢失。

持久性取决于实际选用的 Store 实现。

### 为什么它与 CompositeBackend 很适合配合？

因为我们可以把：

```text
/memories/
```

单独路由到 StoreBackend。

其他临时文件仍然保留在默认 Backend 中。

```text
                CompositeBackend
                         |
             +-----------+-----------+
             |                       |
             v                       v
       /memories/*             其他文件
             |                       |
             v                       v
        StoreBackend           StateBackend
             |                       |
             v                       v
        长期知识               当前任务文件
```

这样 Agent 使用统一的文件工具，却可以通过路径区分数据生命周期。

---

## 二十三、完整实验：用真实 Backend 验证路径映射与存储路由

前面我们讲了多个 Backend。

现在需要通过实验验证最核心的两个设计：

1. Agent 使用的虚拟路径可以映射到真实目录。
2. 相同的文件操作接口可以被 CompositeBackend 路由到不同存储。

这次不用 LLM，也不需要 API Key。

只使用 DeepAgents 的真实 Backend 类。

为了避免误操作，全部文件都创建在临时目录中。

### 23.1 安装

```bash
pip install "deepagents==0.7.32"
```

### 23.2 实验代码

创建：

`test_backends.py`

```python
from pathlib import Path
from tempfile import TemporaryDirectory

from deepagents.backends import (
    FilesystemBackend,
    CompositeBackend,
)

from deepagents.backends.protocol import (
    SandboxBackendProtocol,
)


def read_text(backend, path: str) -> str:
    """Extract text from a real Backend ReadResult."""

    result = backend.read(path)

    assert result.error is None, result.error
    assert result.file_data is not None

    return result.file_data["content"]


with TemporaryDirectory() as temp:

    root = Path(temp)

    # =====================================
    # 1. 准备两个真实目录
    # =====================================

    project_dir = root / "project"
    memory_dir = root / "memory"

    project_dir.mkdir()
    memory_dir.mkdir()

    # =====================================
    # 2. 创建两个真实 Backend
    # =====================================

    project_backend = FilesystemBackend(
        root_dir=str(project_dir),
        virtual_mode=True,
    )

    memory_backend = FilesystemBackend(
        root_dir=str(memory_dir),
        virtual_mode=True,
    )

    # =====================================
    # 3. 创建统一的路由 Backend
    # =====================================

    backend = CompositeBackend(
        default=project_backend,
        routes={
            "/memories/": memory_backend,
        },
    )

    # =====================================
    # 4. 写入项目源码
    # =====================================

    source = (
        "def calculate_refund(amount):\n"
        "    return round(amount)\n"
    )

    result1 = backend.write(
        "/refund.py",
        source,
    )

    assert result1.error is None

    # =====================================
    # 5. 写入业务规则
    # =====================================

    rule = (
        "Coupon refunds must preserve "
        "the allocated total.\n"
    )

    result2 = backend.write(
        "/memories/refund_rules.md",
        rule,
    )

    assert result2.error is None

    # =====================================
    # 6. 验证真实文件位置
    # =====================================

    assert (
        project_dir / "refund.py"
    ).exists()

    assert (
        memory_dir / "refund_rules.md"
    ).exists()

    # 这个文件不应该写入项目目录
    assert not (
        project_dir
        / "memories"
        / "refund_rules.md"
    ).exists()

    print("Physical project file:")
    print(project_dir / "refund.py")

    print("\nPhysical memory file:")
    print(memory_dir / "refund_rules.md")

    # =====================================
    # 7. 使用同一接口读取不同 Backend
    # =====================================

    actual_source = read_text(
        backend,
        "/refund.py",
    )

    actual_rule = read_text(
        backend,
        "/memories/refund_rules.md",
    )

    assert "round(amount)" in actual_source
    assert "allocated total" in actual_rule

    # =====================================
    # 8. 验证虚拟路径不能直接穿越根目录
    # =====================================

    try:
        project_backend._resolve_path(
            "/../secrets.txt"
        )
    except ValueError:
        print("\nTraversal rejected")
    else:
        raise AssertionError(
            "Expected traversal to be rejected"
        )

    # =====================================
    # 9. 验证文件能力不意味着 Shell 能力
    # =====================================

    assert not isinstance(
        project_backend,
        SandboxBackendProtocol,
    )

    try:
        backend.execute("echo hello")
    except NotImplementedError:
        print("Shell execution not supported")
    else:
        raise AssertionError(
            "Execution should not be available"
        )

    print("\nBackend routing verified.")
```

### 23.3 预期验证结果

实验中的虚拟文件系统：

```text
/
|
+-- refund.py
|
+-- memories/
    |
    +-- refund_rules.md
```

真实存储位置则是：

```text
Temporary Directory
       |
       +-- project/
       |     |
       |     +-- refund.py
       |
       +-- memory/
             |
             +-- refund_rules.md
```

两个文件属于不同真实目录。

但是调用方式仍然统一：

```python
backend.read(path)
```

这说明：

**上层调用者不需要知道每个文件真正存储在哪里。**

### 23.4 为什么实验故意调用 `execute()`？

因为我们要验证一个重要边界：

```text
CompositeBackend 支持文件操作
```

不等于：

```text
CompositeBackend 必然支持命令执行
```

当前默认 Backend 是 FilesystemBackend，它不实现 `SandboxBackendProtocol`。

因此，CompositeBackend 不会运行命令，而是拒绝这次请求。

这里的 `echo hello` 只用于检查不支持执行的分支，不会通过这个 Backend 启动 Shell。

### 23.5 实验限制

这段实验验证：

```text
FilesystemBackend
CompositeBackend
虚拟路径映射
路径前缀路由
Shell 能力边界
```

它没有验证：

```text
StateBackend 的 Graph Runtime 集成
真正的远程 Sandbox
生产环境权限隔离
跨进程持久化
真实 pytest 执行
```

这些属于不同层次的问题。

本实验依据固定版本源码中的接口编写，未在当前环境安装 DeepAgents 执行；实际结果以本地安装环境为准。

---

## 二十四、为什么不直接用一个“万能 Backend”？

现在我们已经理解了：

```text
StateBackend
FilesystemBackend
StoreBackend
CompositeBackend
SandboxBackendProtocol
LocalShellBackend
```

可能会产生一个新问题：

> 为什么不把所有能力都实现到一个超级 Backend 中？

例如：

```python
class UniversalBackend:
    def read(...): ...
    def write(...): ...
    def execute(...): ...
    def store_memory(...): ...
    def restore_state(...): ...
```

这样看起来接口更统一。

但会带来三个问题。

### 问题一：不是所有环境都有所有能力

StateBackend 没有真实 Shell。

StoreBackend 也不是进程执行环境。

强迫它们实现：

```python
execute(...)
```

只会产生大量无意义或抛出异常的方法。

### 问题二：能力越集中，权限边界越模糊

如果一个对象同时负责：

```text
读取文件
删除文件
执行 Shell
访问网络
操作长期存储
```

就需要额外机制约束各项权限。

能力边界不清晰容易增加安全风险。

### 问题三：实现难以替换

用户可能只想更换：

```text
长期记忆存储
```

却不想改变：

```text
代码执行环境
```

如果所有能力耦合在一起，更换局部实现会影响整个系统。

因此 DeepAgents 采用：

**基础协议 + 可选执行协议 + 多个具体 Backend + 组合路由。**

这种设计在通用 Agent Harness 中更灵活。

---

## 二十五、现在把完整的文件调用链重新串起来

经过这一章，我们终于理解上一章为什么停在：

```python
resolved_backend.read(...)
```

现在把从模型到真实文件的过程重新走一遍。

```text
                   User Task
                       |
                       v
                    Model
                       |
                       v
              AIMessage.tool_calls
                       |
                       v
                 LangGraph ToolNode
                       |
                       v
                  read_file Tool
                       |
                       v
               FilesystemMiddleware
                       |
                       v
                validate_path()
                       |
                       v
                permission check
                       |
                       v
                  BackendProtocol
                       |
             +---------+---------+
             |                   |
             v                   v
        StateBackend       CompositeBackend
             |                   |
             v             Path Routing
       Graph files               |
             |          +--------+--------+
             |          |                 |
             |          v                 v
             |    FilesystemBackend   StoreBackend
             |          |                 |
             |          v                 v
             |     Local Files       Persistent Store
             |          |                 |
             +----------+-----------------+
                        |
                        v
                    ReadResult
                        |
                        v
               _handle_read_result()
                        |
                        v
                    ToolMessage
                        |
                        v
                    Graph State
                        |
                        v
                      Model
```

这张图体现一个关键事实：

**模型不应该关心文件具体存储在哪里；Tool 也不应该把自己的逻辑写死在某个存储实现上。**

模型只需要知道：

```python
read_file(file_path)
```

Tool 只需要知道：

```python
backend.read(file_path)
```

具体环境由 Backend 决定。

---

## 二十六、Backend 与其他 Agent Harness 机制有什么关系？

我们在这一章研究的是 Backend。

但它并不是独立存在的组件。

### 与 Tool Calling 的关系

Backend 为工具提供实际执行能力。

```text
Tool
  |
  v
Backend
```

### 与 State 的关系

StateBackend 可以将虚拟文件保存到 Graph State。

```text
StateBackend
     |
     v
files Channel
```

### 与 Context Management 的关系

摘要前的旧历史、大工具输出可以卸载到 Backend。

```text
Large Context
     |
     v
Backend Artifact
```

### 与 Memory 的关系

StoreBackend 可以提供跨会话文件存储能力。

```text
Long-term Knowledge
        |
        v
    StoreBackend
```

### 与 HITL 的关系

执行 Shell、删除文件等高风险操作可能需要审批。

```text
Tool Call
    |
    v
Permission / Approval
    |
    v
Backend Execution
```

但权限和人工审批并不由 Backend 抽象自动保证。

这就是为什么 Agent Harness 必须由多个机制共同组成。

---

## 二十七、本章的设计取舍

最后，我们用架构设计的角度评价 Backend。

| 设计决策 | 解决的问题 | 引入的代价 |
|---|---|---|
| BackendProtocol | Tool 与存储实现耦合 | 需要维护统一接口与结果类型 |
| StateBackend | 任务需要虚拟文件，但不一定有真实磁盘 | 依赖 LangGraph Runtime，受 Thread 状态范围约束 |
| FilesystemBackend | 需要操作真实本地项目 | 必须处理路径、权限、并发与宿主机风险 |
| virtual_mode | 提供稳定虚拟路径，并限制文件操作根目录 | 不能代替进程级沙箱 |
| StoreBackend | 长期数据需要跨 Thread 访问 | 需要命名空间、持久化与权限治理 |
| CompositeBackend | 不同路径需要不同存储后端 | 路由规则、路径转换和多存储一致性更复杂 |
| SandboxBackendProtocol | 文件能力与 Shell 执行能力分离 | 需要额外实现执行环境 |
| LocalShellBackend | 支持本地开发时的命令执行 | 宿主机权限风险，不能视为隔离沙箱 |

这一章最重要的安全认识是：

**存储抽象、路径约束、进程隔离和人工审批，是不同的控制层次，不能相互替代。**

---

## 二十八、本章总结

我们从一个非常简单的问题开始：

> `read_file()` 为什么不直接调用 `open()`？

然后发现：

**第一个问题：** 文件可能不在本地磁盘。

因此需要统一的 Backend 文件操作协议。

**第二个问题：** 一部分文件只属于当前 Agent Thread。

因此需要 StateBackend。

**第三个问题：** 一部分文件是本地真实项目。

因此需要 FilesystemBackend 和虚拟路径映射。

**第四个问题：** 一部分知识需要跨任务保存。

因此需要 StoreBackend。

**第五个问题：** 不同路径需要不同存储。

因此需要 CompositeBackend。

**第六个问题：** 修复代码还需要运行 pytest，但存储文件不等于执行命令。

因此需要独立的执行能力协议。

**第七个问题：** 命令执行可能危及宿主机。

因此必须区分本地 Shell 执行与真正的隔离环境。

最终，我们得到了 DeepAgents 的 Backend 架构：

```text
                      Agent
                        |
                        v
                 Filesystem Tools
                        |
                        v
                  BackendProtocol
                        |
          +-------------+-------------+
          |             |             |
          v             v             v
     StateBackend  FilesystemBackend  StoreBackend
          |             |             |
          v             v             v
      Graph State    Local Disk    BaseStore
                        ^
                        |
                CompositeBackend
                  路径选择后端


              需要命令执行时
                        |
                        v
               SandboxBackendProtocol
                        |
             +----------+----------+
             |                     |
             v                     v
       LocalShellBackend     Remote Implementation
             |                     |
             v                     v
          Host Shell          Isolated Environment
          非沙箱               取决于实际部署
```

本章最值得记住的一句话是：

> **Backend 的根本目标，是把 Agent 面向模型的工具能力，与实际存储位置和执行环境分离；这种分离让同一套工具可以用于不同部署场景，但安全、权限和持久化仍然必须由对应的机制明确保证。**

---

# 下一章：第 05 章——State 与 Checkpoint

我们现在理解了：

```python
backend.read(...)
```

和：

```python
backend.write(...)
```

但 StateBackend 又提出了一个重要问题：

> 当 Backend 把文件内容写入 Graph State 后，为什么不是简单地修改字典，而必须经过 Channel 和 Reducer？

进一步的问题是：

> Agent 中断或进程重启后，怎样恢复之前的 messages、files 和执行位置？

下一章就从 StateBackend 的：

```python
send([("files", update)])
```

继续向下追踪。

学习：

```text
State Schema
     |
     v
Channel
     |
     v
Reducer
     |
     v
Checkpoint
     |
     v
Resume
```

它将帮助我们理解：**Agent 怎样从一个能执行工具的循环，进一步成为可以保存、恢复和长期运行的状态化执行系统。**

---

## 源码导航

DeepAgents 固定版本：`0.7.32`

固定 Commit：`e85c6b9`

| 研究位置 | 源码 | 重点 |
|---|---|---|
| Agent 默认 Backend | `graph.py` | 默认创建 StateBackend |
| 统一文件协议 | `backends/protocol.py` | BackendProtocol |
| 执行协议 | `backends/protocol.py` | SandboxBackendProtocol |
| Graph State 文件 | `backends/state.py` | CONFIG_KEY_READ / SEND |
| 本地文件系统 | `backends/filesystem.py` | FilesystemBackend、_resolve_path |
| 长期存储 | `backends/store.py` | StoreBackend |
| 路径路由 | `backends/composite.py` | CompositeBackend |
| 本地 Shell | `backends/local_shell.py` | LocalShellBackend |
| 远程执行基础实现 | `backends/sandbox.py` | BaseSandbox |
| Agent 文件工具 | `middleware/filesystem.py` | read_file、write_file、execute |

### 推荐按这条真实调用链阅读

```text
FilesystemMiddleware
        |
        v
_create_read_file_tool()
        |
        v
sync_read_file()
        |
        v
BackendProtocol.read()
        |
        +-- StateBackend.read()
        |
        +-- FilesystemBackend.read()
        |
        +-- CompositeBackend.read()
                    |
                    v
               Route to Backend
        |
        v
ReadResult
        |
        v
_handle_read_result()
        |
        v
ToolMessage
```

理解完文件能力后，再独立阅读：

```text
execute Tool
     |
     v
SandboxBackendProtocol
     |
     v
LocalShellBackend / Remote Sandbox
     |
     v
ExecuteResponse
```

不要一开始就把文件存储和 Shell 执行混成同一套机制。

### 固定源码链接

- [DeepAgents graph.py](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py)
- [BackendProtocol](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/backends/protocol.py)
- [StateBackend](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/backends/state.py)
- [FilesystemBackend](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/backends/filesystem.py)
- [StoreBackend](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/backends/store.py)
- [CompositeBackend](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/backends/composite.py)
- [LocalShellBackend](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/backends/local_shell.py)
- [Sandbox 基础实现](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/backends/sandbox.py)
