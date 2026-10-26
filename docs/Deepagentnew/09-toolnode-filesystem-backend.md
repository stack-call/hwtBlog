---
title: 第九章：Tool Call 怎样真正落到文件上？——ToolNode、Filesystem 与 Backend
description: 从优惠券部分退款 Bug 的 read_file、edit_file 与 pytest 任务出发，沿 DeepAgents 0.7.32 与 LangChain 1.4.7 源码，渐进理解工具分发、ToolRuntime、FilesystemMiddleware、BackendProtocol、StateBackend、FilesystemBackend、CompositeBackend 及 execute 能力边界
tags: [DeepAgents, LangChain, LangGraph, ToolNode, FilesystemMiddleware, Backend, Coding Agent, 源码精读]
sidebar_position: 11
---

# 第九章：Tool Call 怎样真正落到文件上？——ToolNode、Filesystem 与 Backend

> **本章核心问题：** 模型已经知道要修改 `refund.py`，也生成了合法的 `edit_file` Tool Call。接下来谁找到这个工具、谁实际修改文件、结果怎样返回模型？为什么相同的工具名称可以操作 Agent State、本地磁盘或远程执行环境？

:::tip
FilesystemMiddleware 提供的工具函数，实际上也是由 ToolNode 调用的。

filesystem只是负责提供一些filesystem的工具，后端也只是工具执行的需要，真正的执行是toolnode，因此可以理解为filesysmtem只是一个特殊的工具，需要与真实接轨，但是实际上提供的还是工具

filesystem在提供与真实存储交互的工具的同时还需要管理真实存储中的 **处理权限、文件冲突、超大输出** 等问题，这些处理可以理解为是对工具的保障，但是无论如何把filesysmtem看成一个特殊的工具，与toolnode的区分要搞清楚，toolnode是执行tool用的，真正的toolruntime是toolnode负责的
:::
## 一、接着第八章：模型知道下一步，不代表文件已经改变

第八章研究的是一次模型调用之前的工作：怎样从 Agent 的历史、项目规则和专项技能中，整理出真正发送给模型的上下文。

现在，Coding Agent 已经知道：

```text
用户目标：修复优惠券部分退款金额的精度 Bug。
已读代码：shop/refund.py、shop/coupon_service.py。
项目约定：金额计算使用 Decimal，不能依赖二进制浮点数。
当前假设：部分退款的比例分摊可能产生 0.01 的尾差。
下一步：先修改 refund.py，再运行相关 pytest。
```

于是模型返回一条工具调用请求：

```python
# 教学示例：表示一条模型可能产生的 Tool Call，并非真实运行输出
{
    "name": "edit_file",
    "args": {
        "file_path": "/shop/refund.py",
        "old_string": "refund = round(total * ratio, 2)",
        "new_string": "refund = (total * ratio).quantize(Decimal('0.01'))",
    },
    "id": "call_edit_refund_01",
    "type": "tool_call",
}
```

**这里尚未发生文件修改。**

这条记录只是模型希望执行的动作。像我们在第二章讨论过的那样，`AIMessage.tool_calls` 是调用意图，而不是外部副作用本身。

如果 Agent 只把这段字典打印出来，再把“已修改”写进回答，磁盘上的文件不会有任何变化。

最直接的办法当然是自己写一个分发器。但正是这个最小方案，能帮我们理解框架为什么一步步长出后面的结构。

### 1.1 阅读边界

本章固定使用：

```text
DeepAgents Python：0.7.32
DeepAgents Commit： e85c6b9
LangChain：        1.4.7（对应源码 Commit fbb8a35）
LangGraph：        由固定 LangChain / DeepAgents 环境实际解析的依赖版本
```

主要入口：

- [DeepAgents `graph.py`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py)
- [DeepAgents `middleware/filesystem.py`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py)
- [DeepAgents `backends/protocol.py`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/backends/protocol.py)
- [DeepAgents `backends/state.py`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/backends/state.py)
- [DeepAgents `backends/filesystem.py`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/backends/filesystem.py)
- [DeepAgents `backends/composite.py`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/backends/composite.py)
- [LangChain `agents/factory.py`（Commit `fbb8a35`）](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/factory.py)

**标注约定**：真实源码节选表示来自上述固定文件的短片段；教学化简保留调用关系，但不是仓库原文；概念伪代码仅用于说明设计意图。单独标记“实验”的代码会注明实际运行范围。本文不把模型生成的示例参数当成实际运行结果。

---

## 二、第一步：先用普通 Python 把一次 Tool Call 执行起来

假设目前只允许修改一份预先加载到内存的测试文件。我们完全可以使用一个 Python 字典保存文件内容：

```python
# 教学化简：最小工具执行器
files = {
    "/shop/refund.py": "refund = round(total * ratio, 2)\n",
}


def edit_file(file_path: str, old_string: str, new_string: str) -> str:
    content = files[file_path]
    if old_string not in content:
        return "Error: old_string not found"
    files[file_path] = content.replace(old_string, new_string, 1)
    return "Updated"


tools = {"edit_file": edit_file}

call = {
    "name": "edit_file",
    "args": {
        "file_path": "/shop/refund.py",
        "old_string": "round(total * ratio, 2)",
        "new_string": "(total * ratio).quantize(Decimal('0.01'))",
    },
    "id": "call_01",
}

result = tools[call["name"]](**call["args"])
assert result == "Updated"
```

这样已经能完成一次修改。这里的 `tools` 字典承担两个职责：

1. 根据模型返回的 `name` 找到 Python 函数。
2. 把 `args` 传入该函数并收集结果。

它在单工具、单调用的场景中完全够用。我们没有理由为了这几十行代码提前引入整个 Graph。

但现在看一次更真实的模型回复：

```python
# 教学示例
{
    "tool_calls": [
        {"name": "read_file", "args": {"file_path": "/shop/refund.py"}, "id": "call_read_01"},
        {"name": "read_file", "args": {"file_path": "/shop/tests/test_refund.py"}, "id": "call_read_02"},
    ]
}
```

我们马上就需要回答：

- 如何连续处理多个 Tool Call？
- 如果第二个工具不存在，是否仍要为那次调用提供错误反馈？
- 哪个结果属于 `call_read_01`，哪个属于 `call_read_02`？
- 工具抛出异常后，模型如何知道失败原因并继续尝试？
- 除了模型给的参数，工具是否还需要获取会话状态、调用 ID 或运行时资源？

于是，最初的 `tools[name](**args)` 分发语句，开始演化成一条专门的**工具执行路径**。

这时才有必要重新看我们已认识的 `ToolNode`。

---

## 三、第二步：ToolNode 让一次工具调用成为可靠的 Agent Loop 步骤

### 3.1 它不负责决定“是否应该修改”

上一章中，模型已经做出动作选择：

```text
Model → AIMessage(tool_calls=[...])
```

下一步不是再让另一个模型解释这条消息，而是让 Runtime 根据这条结构化请求调用对应的工具：

```text
Model
  |
  | AIMessage.tool_calls
  v
ToolNode
  |
  | 查找工具 → 校验参数 → 执行 → 组织结果
  v
ToolMessage
  |
  v
Model
```

这里有一个职责边界：

- **Model** 提出调用哪个工具、传什么业务参数。
- **ToolNode** 负责找到并执行框架注册过的工具。
- **ToolMessage** 将对应工具的执行结果反馈给模型。

ToolNode 并不会判断模型的退款计算方案是否正确。它至多能可靠执行某个工具，或者报告工具执行未成功。

### 3.2 在真实 Factory 中，ToolNode 从哪里来？

我们先回到 LangChain 的 [固定版本 `create_agent()`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/factory.py#L1008-L1060)。

第六章已经研究过，Middleware 可以通过 `self.tools` 提供工具，Factory 会将这些工具和调用者直接传入的工具合并。

**真实源码节选，`factory.py`：**

```python
available_tools = middleware_tools + regular_tools
```

随后，Factory 基于 `available_tools` 创建 `ToolNode`，并把已经组合好的工具包装器一并传入。

```python
# 教学化简：等价表达 create_agent 的关键装配意图
node = ToolNode(
    tools=available_tools,
    wrap_tool_call=combined_sync_wrapper,
    awrap_tool_call=combined_async_wrapper,
)
```

为什么这里需要两个参数 `wrap_tool_call` 和 `awrap_tool_call`？

因为一次工具执行除了“实际调用工具”，还可能需要前后处理，例如日志、权限判定、超大结果处理。同步和异步执行需要对应的包装链。**包装器不是在工具之外再构造一个新的模型决策循环，而是在工具执行边界包裹 Handler。**

因此，`ToolNode` 是 LangChain Agent Loop 的通用执行机构，**并不是 DeepAgents 自己重写出来的第二套 Loop**。

### 3.3 工具结果必须能对应原始调用

假设 `edit_file` 成功，框架需要返回带有原始调用 ID 的工具消息：

```python
# 教学示例：ToolMessage 的核心字段
ToolMessage(
    content="Successfully replaced 1 instance(s)...",
    name="edit_file",
    tool_call_id="call_edit_refund_01",
    status="success",
)
```

如果文件路径不存在或者目标字符串无法匹配，模型也应该收到可理解的失败信息，而不是误以为操作已成功。

例如：

```python
# 教学示例
ToolMessage(
    content="Error: File '/shop/refund.py' not found",
    name="edit_file",
    tool_call_id="call_edit_refund_01",
    status="error",
)
```

这里我们终于理解了消息协议与执行的关系：**`tool_call_id` 不是装饰字段，而是模型请求与工具反馈之间的关联键。**

接下来出现一个新问题：

即使 ToolNode 能正确分发，`edit_file` 这个函数究竟应该把数据写去哪里？

---

## 四、第三步：让工具名称与存储位置脱钩

### 4.1 一个最开始足够简单、后来不够用的实现

在本地开发机上，我们完全可以把工具写成：

```python
# 概念伪代码：直接读写本机文件
from pathlib import Path


def read_file(file_path: str) -> str:
    return Path(file_path).read_text(encoding="utf-8")
```

这对可信任的本地脚本可能是合理的。

但在 Coding Agent 中，以下三种运行方式都很常见：

```text
情形 A：教程与单元测试
  /shop/refund.py → 测试会话内部的虚拟文件

情形 B：本地 Coding Agent
  /shop/refund.py → 当前项目目录里的真实文件

情形 C：隔离执行环境
  /shop/refund.py → 远程容器或其他沙箱内的文件
```

如果每换一个环境，就重写 `read_file`、`edit_file`、`grep` 和 `glob`，工具定义会和存储实现紧密绑定。

而模型根本不应该关心文件存放在 State、磁盘还是容器里。它只需要稳定地表达：

```python
{"name": "read_file", "args": {"file_path": "/shop/refund.py"}}
```

因此，现在真正缺少的不是更多工具名称，而是**同一文件操作的一套统一接口**。

DeepAgents 称它为 `BackendProtocol`。

### 4.2 BackendProtocol 约定“能做什么”，不规定“保存在哪里”

打开 [`deepagents/backends/protocol.py`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/backends/protocol.py#L372-L490)。

**真实源码节选：**

```python
class BackendProtocol(abc.ABC):
    ...
```

它的核心设计不是强迫所有 Backend 都使用本地文件，而是约定一组可替换的文件操作。

```python
# 教学化简：用简短形式展示接口边界，非原始完整签名
class BackendContract:
    def ls(self, path): ...
    def read(self, file_path, offset=0, limit=2000): ...
    def write(self, file_path, content): ...
    def edit(self, file_path, old_string, new_string, replace_all=False): ...
    def glob(self, pattern, path="/"): ...
    def grep(self, pattern, path="/"): ...
```

这意味着：

```text
模型看到：read_file("/shop/refund.py")
                |
                v
    一组统一的文件操作接口
                |
        +-------+-------+
        |       |       |
        v       v       v
      State    本地    远程环境
```

注意，`BackendProtocol` 不等于某一个具体的 Backend，它只是约定不同实现要怎样被工具层使用。

从这里开始，文件工具可以稳定，存储环境则可以替换。

但还差一层：模型说的是 `read_file`，Backend 的方法名却是 `read`。是谁把两者连接起来？

---

## 五、第四步：FilesystemMiddleware 把模型工具翻译成 Backend 操作

### 5.1 为什么 ToolNode 不能直接调用 Backend？

一个直觉方案是让 ToolNode 自己认识所有 Backend 类型：

```python
# 概念伪代码：不要把这种分支塞进通用 ToolNode
if tool_name == "read_file":
    if backend_type == "state":
        ...
    elif backend_type == "local":
        ...
    elif backend_type == "sandbox":
        ...
```

这样写会让通用 ToolNode 了解 DeepAgents 的文件工具语义。以后再增加工具，就得不断修改通用执行器。

我们需要的是一个更薄的桥梁：

1. 向 Agent 注册 `read_file`、`edit_file` 等 Tool。
2. 在这些 Tool 的 Python 实现里，调用统一的 Backend 接口。
3. 将 Backend 的结构化结果变成模型可理解的 `ToolMessage`。

DeepAgents 将这层桥梁放在 `FilesystemMiddleware` 中。

### 5.2 它在构建阶段创建工具

打开 [`middleware/filesystem.py`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L1892-L2044)。

这里的构建过程可以概括为：

```python
# 教学化简：表达真实的构建顺序
class FilesystemMiddleware:
    def __init__(self, backend):
        self.backend = backend
        self.tools = self._create_tools()

    def _create_tools(self):
        return [
            self._create_read_file_tool(),
            self._create_edit_file_tool(),
            # 其他已启用的文件工具……
        ]
```

真实版本还创建 `ls`、`write_file`、`delete`、`glob`、`grep` 和 `execute`，并允许通过 `tools=` 进行工具级配置。`read_file` 是该配置要求保留的基础工具。实际公开给模型的工具还会受 Backend 能力和 HarnessProfile 等因素影响。

重点不是“它有八个工具”，而是这个机制发生在 **Agent 构建阶段**：

```text
create_deep_agent()
        |
        v
FilesystemMiddleware(backend)
        |
        v
创建并注册 read_file / edit_file / ...
        |
        v
create_agent() 收集 middleware.tools
        |
        v
ToolNode 可以按名称分发这些工具
```

这样就解决了“工具从哪里注册”的问题。

### 5.3 看一次真正的 read_file 路径

打开 [`_create_read_file_tool()`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L2156-L2331)。

同步工具函数先验证路径，再检查读权限，然后调用 Backend：

**真实源码节选（只保留关键一行）：**

```python
read_result = resolved_backend.read(validated_path, offset=offset, limit=limit)
```

前后的操作也有意义：

- `file_path`：来自模型的工具参数。
- `validated_path`：完成路径格式校验后准备交给 Backend 的路径。
- `self.backend`：构建 Middleware 时配置的 Backend。
- `runtime.tool_call_id`：由执行框架提供，用于构造结果消息。
- `read_result`：Backend 返回的结构化读取结果。

随后，工具将 `ReadResult` 转换成包含文本内容、行号/分页信息或错误说明的 `ToolMessage`。

因此，这一次调用链变成：

```text
模型生成 read_file Tool Call
       |
       v
ToolNode 查找 "read_file"
       |
       v
FilesystemMiddleware 创建的 read_file 工具
       |
       +-- 校验 file_path
       +-- 检查读权限
       |
       v
backend.read(path, offset, limit)
       |
       v
ReadResult
       |
       v
ToolMessage(tool_call_id=原始调用 ID)
       |
       v
Model 继续分析文件内容
```

**这是本章的第一条真实端到端正常路径。**

### 5.4 再看一次 edit_file

现在模型决定将 `refund.py` 中一段旧代码替换为新代码。

打开 [`_create_edit_file_tool()`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L2425-L2519)。

与读取类似，写操作同样先做路径验证和权限检查。但最终调用变成：

```python
# 真实源码节选
res: EditResult = resolved_backend.edit(validated_path, old_string, new_string, replace_all=replace_all)
```

这个调用不是把整段源代码重新交给模型，而是把模型指定的旧字符串和新字符串传给 Backend，由 Backend 尝试完成替换。

如果替换失败，例如旧字符串不在文件中，Backend 可以返回 `EditResult(error=...)`，工具再把错误转换为 `ToolMessage(status="error")`。

这恰好对应 Coding Agent 中最常见的一种反馈循环：

```text
Model：替换旧代码
  |
  v
Tool：找不到 old_string
  |
  v
ToolMessage(status="error")
  |
  v
Model：重新 read_file，定位真实代码，再尝试修改
```

这里的 Loop 让模型有机会调整操作；**它并不保证模型一定能改对**。真实业务正确性仍需要测试验证。

### 5.5 ToolRuntime 为什么不能让模型自己填？

读到这里，你会注意到工具定义中有一个特殊参数：

```python
runtime: ToolRuntime[None, FilesystemState]
```

为什么模型调用里没有 `runtime`，工具函数却能够拿到它？

因为 `runtime` 不是业务参数，而是框架在执行工具时注入的运行时信息。其中包括 `tool_call_id`，还可以提供状态和其他运行上下文。

可以把两种输入分开看：

```text
模型控制：
  file_path、old_string、new_string、offset、limit

执行框架提供：
  runtime（包含 tool_call_id、state 等运行信息）
```

如果把 `runtime` 设计成模型可以自由填写的普通 JSON 字段，模型便可能伪造调用身份或依赖错误的会话状态。工具执行层需要保证业务参数和 Runtime 信息来自不同渠道。

到这里，我们理解了**工具分发、文件工具适配、实际 Backend 调用**三层分工。

可是还有一个问题：相同的 `backend.read()` 与 `backend.edit()`，在不同 Backend 中究竟怎样实现？

---

## 六、第五步：StateBackend 为什么不用真实磁盘？

### 6.1 先从一个不会触碰项目文件的练习开始

假设我们正在开发本章的教学实验，希望 Agent 可以“修改” `/shop/refund.py`，但不希望因此改坏本地工程。

最直观的做法，就是把文件放进一份字典。

```python
# 概念模型：文件仅保存在状态中
state = {
    "files": {
        "/shop/refund.py": {
            "content": "refund = round(total * ratio, 2)\n",
            "encoding": "utf-8",
        }
    }
}
```

如果这份 `files` 数据本来就是 Agent 的 State，那么写入操作就不必调用操作系统文件 API。只需要更新这份状态即可。

这就是 `StateBackend` 的核心方向。

### 6.2 真实源码：StateBackend 通过 Graph State 读写

打开 [`backends/state.py`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/backends/state.py#L35-L109)。

它不是在构造函数中持有一个永远独立的 `self.files = {}`，而是从当前运行中的 LangGraph 配置中获取 State 访问能力。

源码使用 `CONFIG_KEY_READ` 和 `CONFIG_KEY_SEND`。读时，取得 `files` 通道目前的内容；写时，向该通道提交文件字段的局部更新。

```python
# 教学化简：只保留实际的读写方向
class StateBackendIdea:
    def read_files(self):
        return graph_read("files")

    def send_file_update(self, path, file_data):
        graph_send([("files", {path: file_data})])
```

注意：`graph_read`、`graph_send` 是帮助理解的占位函数，不是可导入的真实 API。

真实 [`StateBackend.write()`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/backends/state.py#L188-L202) 的关键动作，是构造文件数据并提交 `files` 更新，而不是调用 `Path.write_text()`。

这意味着：

```text
edit_file("/shop/refund.py")
            |
            v
StateBackend.edit(...)
            |
            v
检查 / 读取当前 files 状态
            |
            v
执行字符串替换
            |
            v
提交 {"files": {"/shop/refund.py": 新数据}}
            |
            v
Graph 合并状态更新
```

它特别适用于：

- 不希望直接修改宿主机文件的教学或测试场景。
- 文件生命周期应与 Agent 会话状态关联的场景。
- 希望利用 Graph State / Checkpoint 管理文件版本的场景。

但它并不意味着“所有用户都能从主机磁盘看到修改后的 Python 文件”。

### 6.3 为什么 StateBackend 不能随处直接调用？

它还有一个重要限制：

> `StateBackend` 的读写依赖 LangGraph 执行上下文；不能把它当作普通的独立内存文件系统，在任意 Python 函数里直接调用并期待访问当前会话的 `files` 通道。

该限制可在 [`_get_config()`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/backends/state.py#L52-L74) 找到。缺少 Graph 执行上下文时，源码会抛出解释性错误。

因此，如果我们要通过 `create_deep_agent()` 预置教学文件，应将文件数据作为 Graph 输入状态提供，而不是在 Graph 之外直接依赖 `StateBackend.write()` 初始化它们。

这就带来了一个新的需求。

我们最终确实需要修改真实项目目录，那该怎么办？

---

## 七、第六步：FilesystemBackend 让同样的工具面向本地项目

### 7.1 需求变化，但工具协议不需要变化

现在我们从教学环境切换到本地开发目录：

```text
project/
  shop/
    refund.py
    coupon_service.py
    tests/
      test_refund.py
```

希望 Agent 的 `edit_file("/shop/refund.py", ...)` 最终修改的是项目磁盘上的真实文件。

模型 Tool Call 的结构不需要变化，`FilesystemMiddleware` 也不需要重新设计工具集合。我们只需提供另一个实现 `BackendProtocol` 的 Backend：`FilesystemBackend`。

打开 [`backends/filesystem.py`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/backends/filesystem.py#L84-L151)。

其初始化接口支持：

```python
# 真实 API 示例：非本次环境实际调用
from deepagents.backends import FilesystemBackend

backend = FilesystemBackend(
    root_dir="/workspace/project",
    virtual_mode=True,
)
```

在 `virtual_mode=True` 时，Agent 看到的虚拟路径可以映射到 `root_dir` 下对应文件。例如教学示例中的 `/shop/refund.py` 对应宿主机上的 `/workspace/project/shop/refund.py`。

但我们必须保持两件事的区别：

- **路径映射约束**：限制 Backend 怎样解析文件路径。
- **安全隔离沙箱**：限制进程能访问什么资源以及能执行什么行为。

`virtual_mode=True` 是路径语义及一部分路径防护，**不是进程隔离或完整安全沙箱**。

源码的安全说明也明确提醒：直接使用本地文件 Backend 需要谨慎，尤其不能因为传入了一个根目录，就认定运行环境已经安全。

### 7.2 替换 Backend 后，Tool Call 仍保持稳定

```python
# 教学配置示例：需要安装匹配依赖，并有可用模型凭据
from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend

agent = create_deep_agent(
    model="openai:YOUR_MODEL",
    backend=FilesystemBackend(
        root_dir="/workspace/project",
        virtual_mode=True,
    ),
)
```

这里使用 `YOUR_MODEL` 作为占位符，以免把某个未校验可用性的模型名称误认为本实验已经可以运行。

配置上与默认 StateBackend 的差别是：

```text
默认 StateBackend：
  read_file → 从 Agent 的 files State 读取
  edit_file → 更新 Agent 的 files State

FilesystemBackend：
  read_file → 从 root_dir 映射的真实文件读取
  edit_file → 修改对应的真实文件
```

**注意：** 如果项目中的 `pytest` 需要实际运行，本地文件 Backend 只解决读写文件问题，并不自动提供 `execute` 工具。我们下一节再处理这一点。

### 7.3 第一个明确的取舍

`StateBackend` 与 `FilesystemBackend` 都实现相同的文件操作语义，但副作用范围不同。

| 问题 | StateBackend | FilesystemBackend |
|---|---|---|
| 文件放在哪里？ | Graph 的 `files` 状态 | 宿主机文件系统 |
| 直接修改真实仓库？ | 否 | 可以 |
| 是否需要 Graph Context？ | 需要 | 核心文件操作不依赖该 State 通道 |
| 是否随会话状态一起处理？ | 是 | 不是靠 `files` State 自动回滚 |
| 是否自动支持 Shell？ | 否 | 否 |
| 核心风险 | State 体积、会话生命周期与持久化策略 | 宿主机文件误改、敏感文件访问 |

我们此时还不需要研究 Checkpoint 的所有细节。只需要记住：**文件落在哪里，与文件修改是否可恢复，不是同一个问题。**

---

## 八、第七步：为什么 BackendProtocol 的文件能力不等于 Shell 能力？

### 8.1 到这里为止，Coding Agent 仍然做不到真正运行 pytest

回到退款修复案例：

```text
read_file("/shop/refund.py")    ✓
edit_file("/shop/refund.py")    ✓
execute("pytest ...")          ?
```

我们已经能够读取、修改文件，但 **`pytest` 是一个进程执行动作**，不是文件内容替换。

一个简单想法是直接让任意 Backend 执行 Shell 命令。可是 `StateBackend` 保存的是 Graph State，里面根本没有一个可以执行 Linux 命令的进程；`StoreBackend` 一类存储实现也不必提供终端。

因此，不能因为某个 Backend 支持 `read()` 与 `edit()`，就推断它支持 `execute()`。

这时才需要第二层更强的接口：`SandboxBackendProtocol`。

### 8.2 真实源码：执行能力是可选扩展

打开 [`backends/protocol.py`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/backends/protocol.py#L820-L866)。

**真实源码节选：**

```python
class SandboxBackendProtocol(BackendProtocol):
    ...
```

它在基础文件能力之上增加 `execute()` 和 `aexecute()`，用来表示能够在相应执行环境中运行命令。

这里的“Sandbox”表达接口的预期使用场景，但**仅仅满足该接口，并不能单独证明具体实现已经足够安全**。实际的容器/虚拟机隔离、权限、网络和资源限制仍要由部署环境保证。

### 8.3 执行能力怎样影响模型可见的工具？

`FilesystemMiddleware` 在构建阶段可以创建名为 `execute` 的工具，但它并不应该无条件向模型宣称“你可以执行 Shell”。

[`supports_execution()`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L1705-L1723) 会判断当前 Backend 是否满足执行能力；对于 `CompositeBackend`，它检查默认 Backend 的能力。

在每次模型请求前，`FilesystemMiddleware.wrap_model_call()` 还会通过 `_filter_unsupported_tools_and_apply_prompt()` 过滤不支持的工具。

```python
# 教学化简：不是源码逐字复制
if not supports_execution(backend):
    visible_tools = [
        tool for tool in request.tools
        if tool.name != "execute"
    ]
    request = request.override(tools=visible_tools)
```

所以必须区分：

```text
A. 工具在 ToolNode 中被构建 / 注册
B. 这轮模型请求实际向模型公布哪些工具
C. 工具调用到来时，执行边界是否真的允许执行
```

这三件事不总是同一件事。

同样，`delete` 也会根据 Backend 是否支持相应能力进行过滤。

这可以避免一种糟糕体验：模型反复尝试执行环境根本不具备的工具。

### 8.4 什么情况下才真正能运行 pytest？

当 Backend 确实提供命令执行能力，并且环境中存在 Python、pytest、项目依赖以及待测代码时，模型才可能发出：

```python
# 教学示例
{
    "name": "execute",
    "args": {
        "command": "python -m pytest shop/tests/test_refund.py -q"
    },
    "id": "call_pytest_01",
}
```

执行路径是：

```text
AIMessage.tool_calls
        |
        v
ToolNode
        |
        v
FilesystemMiddleware 创建的 execute Tool
        |
        v
检查执行能力与参数
        |
        v
SandboxBackendProtocol.execute(command)
        |
        v
ExecuteResponse（包含 output、exit_code 等）
        |
        v
ToolMessage（附执行结果、可选 artifact）
        |
        v
Model 判断测试结果并决定下一步
```

这里需要特别注意两点。

第一，**工具调用返回 `ToolMessage(status="success")` 不等于 pytest 全部通过**。在该版本的执行工具中，命令状态还应查看输出中的 `exit_code`；退出码非零意味着命令级失败，即使工具调用本身成功完成了“执行命令”这件事。

第二，框架无法凭空安装项目依赖。要做真正的自动化修复，仍需为执行环境准备源代码、依赖与合适的权限边界。

---

## 九、第八步：同一个工作空间里，为什么还需要 CompositeBackend？

### 9.1 单个 Backend 够用，直到两类文件有不同的生命周期

现在假设我们的 Coding Agent 既要修改 `/shop/refund.py`，也要读写长期项目笔记：

```text
/shop/refund.py
/shop/tests/test_refund.py
/memories/AGENTS.md
/memories/refund-notes.md
```

我们可能希望：

- 项目代码进入可执行的工作环境，供 pytest 使用。
- 长期笔记保存到独立的持久化存储。
- 模型仍使用统一的 `read_file`、`write_file`，不必理解两套存储系统。

如果只用一个 Backend，就很难同时满足不同的持久化需求。

因此，这时才有必要引入一个按路径路由的 Backend：`CompositeBackend`。

### 9.2 路由规则由文件路径决定

打开 [`backends/composite.py`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/backends/composite.py#L175-L262)。

其核心思路是：

```text
                              /memories/... → Store Backend
                             /
统一 Backend 接口 → 按路径选择
                             \
                              其他路径 → Default Backend
```

构造实例时，我们可以定义默认 Backend 与额外路由：

```python
# 教学配置示例：StoreBackend 的 store/namespace 需按部署准备
from deepagents.backends import CompositeBackend, StateBackend, StoreBackend

backend = CompositeBackend(
    default=StateBackend(),
    routes={
        "/memories/": StoreBackend(
            namespace=lambda _runtime: ("project-memory",),
        ),
    },
)
```

这段配置展示路由意图，不作为独立可运行示例：持久化 Store 与运行环境仍需正确注入，相关完整配置请以固定版本的 Backend API 为准。

实际路径匹配逻辑会优先使用更长的路径前缀。例如：

```text
/memories/refund-notes.md
    → 匹配 /memories/
    → 交给对应 StoreBackend
    → Backend 内部路径 /refund-notes.md

/shop/refund.py
    → 没有命中特别路由
    → 交给 default Backend
    → 内部路径保持 /shop/refund.py
```

也就是说：**CompositeBackend 改变的是文件操作落在哪个 Backend，不是修改模型 Tool Call 的名称。**

### 9.3 一个必须注意的非对称性：Shell 不能简单按文件路径路由

`read()`、`edit()` 都带文件路径，所以可以根据路径选择 Backend。

但 Shell 命令可能是：

```bash
python -m pytest shop/tests/test_refund.py -q
```

它不是一个简单的文件路径。命令会读取哪些文件、工作目录是什么、运行环境在哪里，都取决于执行进程。

因此，[`CompositeBackend.execute()`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/backends/composite.py#L739-L765) **统一转发给默认 Backend**，而不是根据命令字符串匹配 `/memories/` 等文件前缀。

这条边界解释了一个真实的坑：

```text
read_file("/memories/AGENTS.md") 可能成功

但在默认执行环境里直接运行：
cat /memories/AGENTS.md

却未必能看到同一份文件。
```

原因不是 `read_file` 出错，而是 **Backend 的虚拟路径与 Shell 的工作环境并不自动一致**。

这也说明了为什么 DeepAgents 在需要时会补充有关路径路由的提示，提醒模型不要假定所有虚拟文件路径都能直接被 Shell 访问。

---

## 十、第九步：工具执行完成不代表执行反馈可以无限大

### 10.1 修复退款精度问题时，pytest 可能吐出巨量日志

假设 `pytest` 返回了数万行失败日志。

如果把全部输出原样塞进下一轮模型输入，会重现第八章解决过的问题：上下文持续膨胀。

最简单的改法是直接截断输出：

```python
# 概念伪代码
output = output[:4000]
```

但是，如果关键的错误上下文恰好位于截断掉的部分，模型可能看不到真正失败的断言。

我们希望既控制模型上下文，又允许它在必要时回读完整结果。

这时才需要在**工具执行边界**处理大结果。

### 10.2 真实源码：FilesystemMiddleware 包装工具执行

打开 [`wrap_tool_call()`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L3850-L3878)。

这条路径首先拒绝同一模型回复里针对同一路径的后续并行文件修改，再调用真实工具 Handler；如达到配置阈值，部分工具结果会尝试转移到 Backend 存储，并返回可回读的预览。

```python
# 教学化简：保留控制顺序，未列出二进制内容及结果类型分支

def wrap_tool_call(request, handler):
    conflict = detect_same_path_parallel_mutation(request)
    if conflict:
        return conflict

    result = handler(request)
    if result_is_oversized_and_evictable(result):
        return save_and_replace_with_preview(result)
    return result
```

为什么必须在 `handler(request)` 的外围做这件事？

因为这里能看到不同工具的执行结果，而不是只能处理某个具体 Backend 的一次 `read()` 调用。

因此它可以针对整个工具调用链实施跨工具的结果体积控制。

不过需要准确说明：

- `read_file` 自身有分页和内容处理，不是所有输出都走相同的通用 eviction 机制。
- `ls`、`glob`、`grep` 也有各自的截断与结果控制方式。
- 结果迁移是否成功取决于 Backend 和对应路径是否可写。
- 结果被迁移、显示预览，并不等于模型已经检查了完整原始日志。

这与第八章的 Summarization 是不同层次：

```text
FilesystemMiddleware：针对工具输出过大，在工具边界保存/缩减结果

SummarizationMiddleware：针对历史上下文过大，在模型请求边界整理历史
```

二者都可能减少上下文压力，但触发位置和对象不同。

### 10.3 并行修改为什么也在工具边界处理？

再设想模型在一次响应中同时提出：

```text
edit_file("/shop/refund.py", old=A, new=B)   # call_1
edit_file("/shop/refund.py", old=B, new=C)   # call_2
```

如果这些工具并行执行，第二个编辑可能在第一个尚未完成时读取旧内容，或者两个操作互相覆盖，产生不可预测结果。

这个时候“两个 Tool Call 都合法”仍然不等于“两个操作可以安全并行”。

DeepAgents 在 [`_parallel_file_mutation_error()`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L200-L226) 中，检查同一 AIMessage 内是否存在对相同路径的较早修改调用，并拒绝后续冲突调用。

这一规则针对已识别的文件修改工具（`write_file`、`edit_file`、`delete`）以及同一路径冲突；它**不等于整个工作空间的分布式事务机制**，也不保证来自其他进程的写入不会竞争。

到这里，我们可以看见 Tool Wrapper 的第二种价值：不仅围绕工具添加日志，还能在工具真正落到 Backend 之前发现特定的不安全执行形态。

---

## 十一、最小实验：用无模型、无磁盘依赖的代码验证三层分工

真实 DeepAgents 运行需要安装指定版本及兼容的 LangChain/LangGraph 依赖；完整退款测试还需要真实项目与合适的执行环境。

但本章的核心机制可以先拆开验证：

> **模型 Tool Call → 工具分发 → Backend 修改 → ToolMessage 反馈**。

下面是一个可独立运行的纯 Python 教学实验。它不导入 DeepAgents，也不会向任何远程模型发送请求；作用是验证我们刚刚推导出的执行结构。

把它保存为 `chapter09_minimal.py`：

```python
from dataclasses import dataclass


@dataclass
class ToolResult:
    name: str
    tool_call_id: str
    status: str
    content: str


class MemoryBackend:
    """教学 Backend：仅修改当前实例中的字典。"""

    def __init__(self, files: dict[str, str]):
        self.files = dict(files)

    def read(self, path: str) -> str:
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    def edit(self, path: str, old: str, new: str) -> int:
        content = self.read(path)
        occurrences = content.count(old)
        if occurrences != 1:
            raise ValueError(f"expected 1 occurrence, found {occurrences}")
        self.files[path] = content.replace(old, new, 1)
        return occurrences


class FileTools:
    """教学工具适配层：把业务参数转给 Backend。"""

    def __init__(self, backend: MemoryBackend):
        self.backend = backend

    def run(self, call: dict) -> ToolResult:
        name, args, call_id = call["name"], call["args"], call["id"]
        try:
            if name == "read_file":
                content = self.backend.read(args["file_path"])
            elif name == "edit_file":
                count = self.backend.edit(
                    args["file_path"], args["old_string"], args["new_string"]
                )
                content = f"replaced {count} occurrence(s)"
            else:
                raise ValueError(f"unknown tool: {name}")
            return ToolResult(name, call_id, "success", content)
        except (FileNotFoundError, ValueError) as exc:
            return ToolResult(name, call_id, "error", str(exc))


def main():
    old = "refund = round(total * ratio, 2)"
    new = "refund = (total * ratio).quantize(Decimal('0.01'))"

    backend = MemoryBackend({"/shop/refund.py": old + "\n"})
    tools = FileTools(backend)

    read = tools.run({
        "name": "read_file",
        "args": {"file_path": "/shop/refund.py"},
        "id": "call_read_01",
    })
    assert read.status == "success"
    assert old in read.content

    edit = tools.run({
        "name": "edit_file",
        "args": {
            "file_path": "/shop/refund.py",
            "old_string": old,
            "new_string": new,
        },
        "id": "call_edit_02",
    })
    assert edit.status == "success"
    assert edit.tool_call_id == "call_edit_02"
    assert new in backend.read("/shop/refund.py")

    failed = tools.run({
        "name": "edit_file",
        "args": {
            "file_path": "/shop/refund.py",
            "old_string": old,
            "new_string": new,
        },
        "id": "call_edit_03",
    })
    assert failed.status == "error"
    assert failed.tool_call_id == "call_edit_03"

    # 另一份 Backend 独立保存数据；证明工具接口与实例数据可分离。
    separate = MemoryBackend({"/shop/refund.py": old + "\n"})
    assert old in separate.read("/shop/refund.py")
    assert new not in separate.read("/shop/refund.py")

    print("PASS: read → edit → error feedback → backend isolation")


if __name__ == "__main__":
    main()
```

执行：

```bash
python chapter09_minimal.py
```

**实际验证记录：** 我在当前 Python 环境提取并执行了以上完整脚本，断言全部通过，终端输出为：

```text
PASS: read → edit → error feedback → backend isolation
```

这段实验验证四件事：

1. 工具分发器可以使用稳定的工具名称。
2. 工具业务逻辑可以调用独立的 Backend。
3. 修改失败可以形成与原调用 ID 关联的错误反馈。
4. 换一份 Backend 数据，不必改变工具名称或 Tool Call 格式。

**验证边界：** 该实验不会执行 LangGraph 的 `ToolNode`、不会模拟 `ToolRuntime` 注入、不会写入 Graph State，更不会运行真正的 `pytest`。它证明的是本章最小的接口分工，不是 DeepAgents 完整运行正确性。

### 11.1 从教学实验替换为真实框架时，哪里改变？

```text
教学实验                          真实框架

手写的 call 字典                 AIMessage.tool_calls

FileTools.run()                  LangGraph ToolNode

手工维护 call_id                 ToolRuntime.tool_call_id

FileTools 调用 backend.edit()    FilesystemMiddleware 生成的 edit_file 工具

MemoryBackend                    StateBackend / FilesystemBackend / Sandbox Backend

ToolResult                       ToolMessage（或状态更新 Command）
```

不是说真实框架与教学版的每个类都一一相等，而是它们分别处在相同的职责位置。

真正的下一步验证，应在已安装固定依赖的 Python 环境里：构建 `create_deep_agent()`、准备 `files` 初始状态、使用可控模型输出触发 `read_file` 与 `edit_file`，检查 Graph State 中的文件变化和对应 `ToolMessage`。若要验证真实仓库修改与测试执行，还必须另外准备可执行 Backend 和项目测试依赖。

---

## 十二、现在才拼出完整架构：一次修改究竟经过几层？

最开始我们只写了：

```python
result = tools[call["name"]](**call["args"])
```

这个实现没有错。它只是还没有处理工程系统中的新要求。

随着问题逐步出现，我们依次增加：

1. **多个调用与反馈关联**：ToolNode 负责分发执行并生成与调用 ID 对应的结果。
2. **稳定的文件工具语义**：FilesystemMiddleware 创建文件工具，把参数转成 Backend 操作。
3. **存储环境可替换**：BackendProtocol 隔离 Tool 与具体存储实现。
4. **会话内虚拟文件**：StateBackend 将文件操作提交到 Graph State。
5. **真实仓库文件**：FilesystemBackend 将操作映射到宿主机项目目录。
6. **运行测试的必要能力**：SandboxBackendProtocol 为部分 Backend 增加 Shell 执行。
7. **不同数据生命周期**：CompositeBackend 按文件路径分发操作。
8. **运行边界保护**：工具 Wrapper 处理冲突、过大结果以及必要的请求改写。

至此，我们才有理由画出完整的工具执行链：

```text
用户：修复优惠券部分退款精度 Bug
                    |
                    v
               Agent Model
                    |
                    | AIMessage.tool_calls
                    v
             LangChain ToolNode
                    |
                    | ToolCallRequest
                    v
       Middleware 的 wrap_tool_call 链
                    |
                    | handler(request)
                    v
      FilesystemMiddleware 注册的 Tool
                    |
                    +-- read_file / edit_file / grep / ...
                    |          |
                    |          v
                    |    BackendProtocol
                    |          |
                    |    +-----+----------+----------------+
                    |    |                |                |
                    |    v                v                v
                    | StateBackend  FilesystemBackend  CompositeBackend
                    |                                      |
                    |                               按路径选择实际 Backend
                    |
                    +-- execute（仅支持执行的环境可用）
                               |
                               v
                     SandboxBackendProtocol
                    |
                    v
        ToolMessage / 必要的 State Update
                    |
                    v
               Agent Model
```

图中有三种不同的箭头意义：

- **上半部分**是控制调用：Model 选择工具，ToolNode 调度工具。
- **中间部分**是能力委托：文件工具将实际读写交给 Backend。
- **下半部分**是结果反馈：工具执行结果被组织为消息，再被模型消费。

尤其不能把 `Backend`、`ToolRuntime` 与 `ToolNode` 混成“同一个运行时”：

| 组件 | 解决的问题 | 不负责的事情 |
|---|---|---|
| ToolNode | 找到工具、执行并关联结果 | 决定哪种退款算法正确 |
| ToolRuntime | 提供当前工具调用的运行上下文 | 替模型编造文件编辑参数 |
| FilesystemMiddleware | 暴露文件工具，转换并增强工具调用 | 自己构建第二套 Agent Loop |
| BackendProtocol | 定义统一文件访问契约 | 规定一定保存到本地磁盘 |
| StateBackend | 使用 Graph `files` State 存文件 | 直接修改真实仓库、执行 Shell |
| FilesystemBackend | 访问本机文件系统 | 提供进程隔离沙箱或 Shell |
| CompositeBackend | 根据文件路径选择 Backend | 将任意 Shell 命令路由到某个文件 Backend |
| SandboxBackendProtocol | 表示额外的命令执行能力 | 自动证明隔离策略和审批配置充分 |

### 12.1 三个最容易误解的边界

**第一：Tool Call 合法，不代表有权限执行。** `edit_file` 在语法层面有效，仍可能因路径、权限或后端能力限制被拒绝。下一章再专门研究审批、中断与恢复。

**第二：工具成功，不代表业务完成。** 成功写入 `refund.py`，不代表精度问题已修复；成功运行 `pytest` 命令，也不等于退出码为零。必须把具体测试结果重新交给模型判断。

**第三：Backend 可替换，不代表安全性质完全等价。** State、宿主机文件与远程沙箱有不同的可见性、副作用、持久性与攻击面。统一接口只是降低调用层耦合，不是消除部署风险。

### 12.2 这一章只用到了 `FilesystemMiddleware` 的一小部分

上面这张表里，`FilesystemMiddleware` 那一行的描述是「暴露文件工具，转换并增强工具调用」。这个描述对应的是本章需要的部分——大约一百行。

但它的源文件有 3909 行。剩下的部分在做本章没有涉及的事：

- 它为什么要定义**两个** `state_schema`，以及 `DeltaChannel` 的批量 reducer 与单次版本差在哪；
- `tools=` 白名单为什么让被排除的工具**根本不生成**，而不是只从 schema 里隐藏；
- 八个工具共用的那三段骨架是什么，工具描述为什么会在每次模型调用时被重写；
- `wrap_tool_call` 做的三件事里，有一件是**明确不碰它自己那七个工具**的；
- `permissions` 在什么情况下会直接拒绝初始化。

这些内容单独成篇：

**第九章附：FilesystemMiddleware 到底在做什么？——State、八种工具、两个 Hook 与 Backend 的分工**

另外，本章和附一都默认了一个前提——模型已经知道这些工具存在。这个前提本身也不显然：一个 Middleware 在 `__init__` 里造出的工具对象，要经过收集、注册、每轮筛选、序列化四道手，才进入模型请求。这条链路同样单独成篇：

**第九章附二：工具是怎么走到模型面前的？——注册、暴露、执行三段链路**

---

## 十三、下一章：工具可以执行，但谁有权批准修改？

现在，我们已经能够完整解释：

```text
模型选择 edit_file
    → ToolNode 识别调用
    → FilesystemMiddleware 校验并调用 Backend
    → Backend 修改对应环境里的文件
    → ToolMessage 将结果反馈给模型
```

但这也暴露出下一个问题。

假设模型认为修复退款 Bug 的最快方法，是删除整个测试目录后再运行 pytest：

```python
# 教学示例：危险工具调用意图
{
    "name": "delete",
    "args": {"file_path": "/shop/tests"},
    "id": "call_delete_tests_01",
}
```

这个调用在结构上可能完全合法；对应 Backend 也可能确实具有删除能力。

**能执行，仍然不等于应该直接执行。**

我们还需要在工具真正产生副作用前决定：

- 哪些操作可以自动放行？
- 哪些操作必须由人审核？
- Agent 怎样暂停，等待批准或拒绝？
- 用户批准后，怎样继续同一任务，而不是从头再跑？

这就自然引出下一章：

**第十章：Human-in-the-Loop——工具执行前如何中断、审批与恢复？**

---

## 附：本章源码导航

以下 DeepAgents 链接均固定在 Commit `e85c6b9`，LangChain 固定在 `fbb8a35`：

| 想确认的问题 | 固定源码 |
|---|---|
| DeepAgents 何时创建 FilesystemMiddleware | [`graph.py#L821-L849`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py#L821-L849) |
| DeepAgents 怎样最终交给 create_agent | [`graph.py#L920-L942`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py#L920-L942) |
| LangChain 怎样汇总工具并创建 ToolNode | [`factory.py#L1008-L1060`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/factory.py#L1008-L1060) |
| 文件工具如何初始化与注册 | [`filesystem.py#L1892-L2044`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L1892-L2044) |
| read_file 如何校验并访问 Backend | [`filesystem.py#L2156-L2331`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L2156-L2331) |
| edit_file 如何替换并反馈结果 | [`filesystem.py#L2425-L2519`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L2425-L2519) |
| State 文件字段如何声明 | [`filesystem.py#L1277-L1313`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L1277-L1313) |
| StateBackend 怎样访问和写入 Graph State | [`state.py#L35-L109`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/backends/state.py#L35-L109) |
| StateBackend 的 write/edit 实现 | [`state.py#L188-L230`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/backends/state.py#L188-L230) |
| 统一文件契约与 Shell 扩展接口 | [`protocol.py#L372-L490`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/backends/protocol.py#L372-L490)、[`#L820-L866`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/backends/protocol.py#L820-L866) |
| 本地文件 Backend 的安全边界 | [`filesystem.py（Backend）#L84-L151`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/backends/filesystem.py#L84-L151) |
| CompositeBackend 的路径路由 | [`composite.py#L175-L262`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/backends/composite.py#L175-L262) |
| CompositeBackend 怎样执行 Shell | [`composite.py#L739-L765`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/backends/composite.py#L739-L765) |
| 按 Backend 能力过滤可见工具 | [`filesystem.py#L3057-L3089`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L3057-L3089) |
| 工具输出处理与并行文件修改冲突 | [`filesystem.py#L3850-L3878`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L3850-L3878)、[`#L200-L226`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L200-L226) |

**本章核心产出：** 读者已经可以从一条 `AIMessage.tool_calls` 出发，解释 ToolNode 如何分发，FilesystemMiddleware 如何适配，Backend 如何产生实际副作用，以及结果为何必须以带关联 ID 的消息返回 Agent Loop；并能区分 State 文件、本地磁盘文件和 Shell 执行能力的边界。
