---
title: 第三章：Tool Calling——从模型意图到真实工具执行
description: 以 DeepAgents 的 read_file 为主线，从工程问题、协议设计和能力边界出发，追踪 FilesystemMiddleware、LangChain create_agent、LangGraph ToolNode 与 Backend 的完整调用链
tags: [DeepAgents, LangChain, LangGraph, Tool Calling, Middleware, ToolNode, 源码精读]
sidebar_position: 3
---

# 第三章：Tool Calling——模型如何真正读取 refund.py？

## 一、承接上一章：Agent Loop 留下了什么问题？

上一章我们已经知道，当用户提出：

> 修复优惠券部分退款精度 Bug，并运行测试。

Agent 会反复执行：

```text
Model
  |
  v
Tools
  |
  v
Model
```

第一轮，模型可能输出：

```python
AIMessage(
    content="",
    tool_calls=[
        {
            "name": "read_file",
            "args": {
                "file_path": "/refund.py"
            },
            "id": "call-001",
        }
    ],
)
```

这条消息表达：

> 模型希望读取 `/refund.py`。

但是，这里有一个必须强调的事实：

**模型并没有读取文件。**

它只是生成了一个包含工具名称和参数的数据结构。

真正的文件读取还没有发生。

这就引出了本章的问题：

**谁负责把模型提出的行动请求变成真实执行？**

我们先从最简单的实现方案开始。

---

## 二、最直接的实现：模型说什么，程序就执行什么

假设我们只有一个文件读取函数：

```python
def read_file(path: str) -> str:
    with open(path) as f:
        return f.read()
```

收到模型的 Tool Call 后，可以这样写：

```python
call = ai_message.tool_calls[0]

if call["name"] == "read_file":
    result = read_file(
        call["args"]["file_path"]
    )
```

这个方案确实能让模型触发文件读取。

但如果用它构建一个通用 Coding Agent，很快就会遇到四个问题。

### 问题一：模型怎么知道能使用哪些工具？

模型需要知道：

```text
read_file
edit_file
write_file
grep
execute
```

否则它只能猜测工具名称和参数格式。

### 问题二：程序怎么找到对应的 Python 函数？

随着工具数量增加，我们不能无限编写：

```python
if name == "read_file":
    ...
elif name == "write_file":
    ...
elif name == "execute":
    ...
```

我们需要一种统一的工具注册和查找方式。

### 问题三：模型提供的参数可信吗？

模型可能生成：

```python
{
    "name": "read_file",
    "args": {
        "file_path": 123
    }
}
```

但 `file_path` 应该是字符串。

模型也可能请求读取不允许访问的路径。

因此，不能直接信任 Tool Call 参数。

### 问题四：执行需要的内部信息从哪里来？

真实工具可能还需要：

```text
当前 Graph State
当前 Tool Call ID
Backend
运行时配置
持久化 Store
```

这些不是用户任务参数，也不应该允许模型自由伪造。

所以我们发现：

**Tool Calling 不是简单的函数调用，它是一个从不可信模型请求到受控系统执行的边界。**

由此可以推导出一套更合理的设计。

```text
                 Model Tool Call
                        |
                        v
                  Tool Registry
                        |
                        v
                 Parameter Validation
                        |
                        v
                 Runtime Injection
                        |
                        v
                 Permission Checking
                        |
                        v
                   Real Execution
                        |
                        v
                    Tool Result
```

接下来，我们就在 DeepAgents 源码中寻找这些机制。

---

## 三、第一个设计：为什么工具必须先注册？

先解决第一个问题：

> 模型怎么知道存在 `read_file`？系统又怎么找到对应的函数？

这需要一份统一的工具定义。

一个工具至少要描述：

```text
工具叫什么？
用来做什么？
接受哪些参数？
真正执行哪个函数？
```

LangChain 使用 `BaseTool` 作为统一的工具抽象。

例如：

```python
from langchain_core.tools import tool

@tool
def read_file(file_path: str) -> str:
    """Read a file by path."""
    ...
```

这会把普通函数转换成符合 LangChain 工具协议的对象。

但 DeepAgents 的内置文件工具，不是这样简单定义的。

因为它需要额外支持：

- 不同 Backend。
- 文件权限。
- 分页读取。
- 大输出处理。
- 同步和异步执行。
- ToolMessage 关联。
- 多种文件内容类型。

因此，DeepAgents 将文件工具的创建集中到 `FilesystemMiddleware` 中。

### 3.1 真实源码：`FilesystemMiddleware`

源码：

`deepagents/middleware/filesystem.py`

找到：

```python
class FilesystemMiddleware(
    AgentMiddleware[FilesystemState, ContextT, ResponseT]
):
    ...
```

这个类负责为 Agent 提供文件操作能力。

在它的初始化逻辑中，可以找到：

```python
self.tools = self._create_tools()
```

继续进入：

```python
def _create_tools(self) -> list[BaseTool]:
    ...
```

核心源码：

```python
tool_factories = (
    ("ls", self._create_ls_tool),
    ("read_file", self._create_read_file_tool),
    ("write_file", self._create_write_file_tool),
    ("edit_file", self._create_edit_file_tool),
    ("delete", self._create_delete_tool),
    ("glob", self._create_glob_tool),
    ("grep", self._create_grep_tool),
    ("execute", self._create_execute_tool),
)
```

然后根据启用的工具名称创建工具对象。

注意，这里的 `_create_tools()` **并不执行文件操作**。

它只负责创建能够在未来执行文件操作的工具对象。

这就有了第一条调用关系：

```text
                FilesystemMiddleware
                         |
                         v
                      __init__
                         |
                         v
                   _create_tools()
                         |
             +-----------+-----------+
             |           |           |
             v           v           v
         read_file   write_file   edit_file
             |           |           |
             v           v           v
          BaseTool    BaseTool    BaseTool
             |           |           |
             +-----------+-----------+
                         |
                         v
                    self.tools
```

### 为什么将工具放在 Middleware 里？

因为 `FilesystemMiddleware` 不只是某一次工具执行的 Hook。

它还负责**向 Agent 声明自己提供哪些能力**。

于是，Middleware 可以同时承担两项相关职责：

```text
构建阶段：
    声明、创建、注册文件工具

执行阶段：
    提供文件工具相关的拦截与处理
```

这就解释了你之前发现的现象：

**LangChain 为什么会手动读取 Middleware 的 `tools` 属性？**

因为这里的 `tools` 是 Middleware 向 Agent 构建器声明能力的方式。

它不是一个普通的执行 Hook。

---

## 四、第二个设计：为什么工具需要 Schema？

现在已经能够创建 `read_file` 工具。

但是模型应该怎样知道它的参数？

假设模型生成：

```python
{
    "file_path": "/refund.py"
}
```

系统怎么确认它符合工具要求？

这需要一个参数契约。

也就是 Schema。

### 4.1 看 DeepAgents 的真实定义

仍然在：

`deepagents/middleware/filesystem.py`

找到：

```python
class ReadFileSchema(BaseModel):
    ...
```

源码的主要字段是：

```python
class ReadFileSchema(BaseModel):

    file_path: str = Field(
        description=(
            "Absolute path to the file to read. "
            "Must be absolute, not relative."
        )
    )

    offset: int = Field(
        default=DEFAULT_READ_OFFSET,
        description="Line number to start reading from",
    )

    limit: int = Field(
        default=DEFAULT_READ_LIMIT,
        description="Maximum number of lines to read",
    )
```

这是保留实际字段与默认值关系的节选，说明文字略有缩短。

它定义了三个参数：

| 参数 | 作用 | 约束 |
|---|---|---|
| `file_path` | 读取哪个文件 | 必须是字符串 |
| `offset` | 从第几行开始 | 整数，默认值由框架提供 |
| `limit` | 最多读取多少行 | 整数，默认值由框架提供 |

为什么需要 `offset` 和 `limit`？

因为 Coding Agent 可能读取一个几千行的源码文件。

如果每次 `read_file` 都返回完整内容，模型上下文可能迅速变大。

所以，工具本身就要提供分页能力：

```python
read_file(
    file_path="/refund.py",
    offset=100,
    limit=50,
)
```

这样 Agent 可以按需要分段读取文件。

这与第 06 章的 Context Management 具有直接关系：

**上下文管理不一定只在模型调用前发生，也可以从工具接口设计开始。**

### 4.2 Schema 能保证文件安全吗？

不能。

Schema 可以检查参数结构和基本类型。

但：

```python
file_path="/etc/passwd"
```

也是一个字符串。

它能通过“字符串类型”检查，并不代表应该允许读取。

因此需要把两件事分开：

```text
Schema Validation：
    参数在结构上是否合法？

Permission Check：
    这次操作是否被允许？
```

这是两个不同的设计问题，不能因为有 Pydantic Schema，就认为工具已经安全。

---

## 五、第三个设计：Schema 怎样绑定到真实执行函数？

现在我们有：

```python
ReadFileSchema
```

但还没有找到真正执行文件读取的函数。

继续进入：

```python
FilesystemMiddleware._create_read_file_tool()
```

这个方法内部定义了同步和异步的读取函数：

```python
sync_read_file(...)
async_read_file(...)
```

最后，真实源码使用：

```python
return StructuredTool.from_function(
    name="read_file",
    description=tool_description,
    func=sync_read_file,
    coroutine=async_read_file,
    infer_schema=False,
    args_schema=args_schema,
)
```

这里的 `args_schema` 根据配置选择 `ReadFileSchema` 或其视频扩展版本。

这段代码是一个很重要的连接点。

它将四项信息绑定到同一个工具对象：

```text
name
    |
    +-- read_file

description
    |
    +-- 让模型理解工具用途

args_schema
    |
    +-- 约束模型可以提供的参数

func / coroutine
    |
    +-- 真正执行工具的 Python 函数
```

因此，`StructuredTool` 可以理解为：

**将工具的对外调用协议与实际执行实现绑定在一起的对象。**

但请注意：

```python
StructuredTool.from_function(...)
```

仍然不是执行文件读取。

它只是注册：

> 以后有人调用 `read_file`，应该执行哪个函数。

---

## 六、工具创建好了，LangChain 怎么知道它存在？

现在我们回到整个架构。

DeepAgents 的入口：

```python
create_deep_agent(...)
```

会创建：

```python
FilesystemMiddleware(
    backend=backend,
    ...
)
```

然后把这个 Middleware 交给 LangChain：

```python
create_agent(
    model=model,
    middleware=deepagent_middleware,
    ...
)
```

接下来进入：

`langchain/agents/factory.py`

### 6.1 真实源码：收集 Middleware 工具

找到：

```python
middleware_tools = [
    t
    for m in middleware
    for t in getattr(m, "tools", [])
]
```

这就是我们此前多次讨论的手动解析机制。

现在它终于有了具体意义。

假设 Middleware 为：

```python
[
    FilesystemMiddleware(...),
    OtherMiddleware(...),
]
```

`create_agent()` 会读取这些 Middleware 声明的工具。

然后：

```python
available_tools = middleware_tools + regular_tools
```

这里的：

```python
regular_tools
```

是开发者直接传入的普通工具。

例如：

```python
create_agent(
    model=model,
    tools=[my_custom_tool],
    middleware=[FilesystemMiddleware(...)],
)
```

最后可执行工具由两部分组成：

```text
                create_agent()
                      |
          +-----------+-----------+
          |                       |
          v                       v
       tools 参数            middleware.tools
          |                       |
          v                       v
     用户自定义工具            文件系统工具
          |                       |
          +-----------+-----------+
                      |
                      v
                available_tools
```

### 6.2 为什么不让模型直接调用 Middleware？

因为 Middleware 不是一个单一工具。

它可能声明：

```text
tools
state_schema
before_model
wrap_model_call
wrap_tool_call
```

这些内容承担不同职责。

`create_agent()` 要先读取工具声明，才能建立工具执行表。

因此：

**工具注册属于 Agent 构建阶段，工具执行属于 Agent 运行阶段。**

这是必须记住的一个区分。

---

## 七、第四个设计：为什么需要 ToolNode？

现在我们已经有：

```python
available_tools
```

假设里面包含：

```text
read_file
write_file
edit_file
grep
```

模型运行时返回：

```python
AIMessage(
    content="",
    tool_calls=[
        {
            "name": "read_file",
            "args": {
                "file_path": "/refund.py"
            },
            "id": "call-001",
        }
    ],
)
```

系统需要根据：

```python
"name": "read_file"
```

找到刚才注册的工具对象。

最直接的方式是建立：

```python
tools_by_name = {
    tool.name: tool
    for tool in available_tools
}
```

这本质上是一个工具注册表。

但真正的执行还涉及参数注入、错误处理、工具包装器和多个 Tool Call 的调度。

所以 LangGraph 提供了通用的：

```python
ToolNode
```

### 7.1 真实源码：创建 ToolNode

在 `factory.py` 中：

```python
tool_node = ToolNode(
    tools=available_tools,
    wrap_tool_call=wrap_tool_call_wrapper,
    awrap_tool_call=awrap_tool_call_wrapper,
)
```

这是有工具或工具包装器时所走的核心分支。

于是 LangChain 把工具执行能力交给 LangGraph。

这里的职责是：

```text
LangChain：
    收集工具
    组合 Middleware
    构建 Agent Graph

LangGraph ToolNode：
    查找工具
    注入 Runtime
    执行工具
    处理结果
```

### 7.2 ToolNode 如何建立注册表？

打开：

`langgraph/prebuilt/tool_node.py`

找到 `ToolNode.__init__()`。

实际代码包含：

```python
self._tools_by_name = {}

for tool in tools:

    if not isinstance(tool, BaseTool):
        tool_ = create_tool(tool)
    else:
        tool_ = tool

    self._tools_by_name[tool_.name] = tool_
```

这就是工具名称到工具对象的映射。

于是：

```text
"read_file"
     |
     v
self._tools_by_name["read_file"]
     |
     v
StructuredTool
     |
     v
sync_read_file()
```

至此，第一个问题得到解决：

**模型输出的是工具名称，ToolNode 根据注册表找到真正的工具对象。**

---

## 八、但 ToolNode 为什么还需要注入 Runtime？

假设模型调用：

```python
{
    "name": "read_file",
    "args": {
        "file_path": "/refund.py"
    },
    "id": "call-001",
}
```

注意，模型只提供：

```python
file_path
```

但 DeepAgents 的实际执行函数是：

```python
def sync_read_file(
    file_path: str,
    runtime: ToolRuntime[None, FilesystemState],
    offset: int = DEFAULT_READ_OFFSET,
    limit: int = DEFAULT_READ_LIMIT,
) -> ToolMessage | Command:
    ...
```

这里突然多出一个：

```python
runtime
```

它从哪里来？

不能让模型提供：

```python
{
    "runtime": {
        "state": "...",
        "tool_call_id": "..."
    }
}
```

原因很简单：

**模型不应该控制系统内部的运行时对象。**

否则模型就可能伪造：

- 当前 State。
- Tool Call ID。
- Store。
- 内部配置。
- 其他工具不应暴露的上下文。

所以 LangGraph 把 Tool Call 参数分成两类。

### 8.1 模型参数

由模型生成。

例如：

```python
{
    "file_path": "/refund.py",
    "offset": 0,
    "limit": 100,
}
```

### 8.2 系统参数

由 Runtime 注入。

例如：

```python
runtime.tool_call_id
runtime.state
runtime.config
runtime.store
```

这就产生了一个重要的安全边界：

```text
                    Model
                      |
                      v
                Tool Call Args
                      |
                      v
                  ToolNode
                      |
             +--------+--------+
             |                 |
             v                 v
       Model Arguments    Runtime Injection
             |                 |
             +--------+--------+
                      |
                      v
                 Real Tool
```

### 8.3 真实源码：创建 ToolRuntime

在 ToolNode 的执行入口中，源码会构造：

```python
tool_runtime = ToolRuntime(
    state=state,
    tool_call_id=call["id"],
    config=cfg,
    context=runtime.context,
    store=runtime.store,
    stream_writer=runtime.stream_writer,
    ...
)
```

随后真正执行时：

```python
injected_call = self._inject_tool_args(
    call,
    request.runtime,
    tool,
)
```

这个方法负责把可信的 Runtime 信息注入工具。

实际实现还会剔除模型尝试伪造的注入参数，再放入系统提供的真实值。

因此：

**ToolRuntime 是由 Agent 运行时提供的受控执行上下文，不是模型自由填写的业务参数。**

这就是它存在的原因。

---

## 九、第五个设计：为什么还需要 `wrap_tool_call`？

现在工具已经注册，也能找到工具，Runtime 也能够注入参数。

似乎可以直接执行了。

但真实 Coding Agent 还有另一个问题：

> 如果模型请求修改文件，是否每次都应该直接执行？

例如：

```python
edit_file(
    file_path="/refund.py",
    old_string="...",
    new_string="...",
)
```

我们可能需要：

```text
检查操作权限
记录审计日志
限制工具调用
人工审批
处理大结果
```

这些逻辑不属于 `edit_file` 的核心业务实现。

如果全部写进每个工具：

```python
def edit_file(...):
    check_permissions()
    audit()
    execute()
    truncate_result()
```

那么 `read_file`、`write_file` 等工具又要重复很多代码。

所以我们需要一个可以包裹工具执行的扩展点。

这就是：

```python
wrap_tool_call
```

### 9.1 在 ToolNode 中找到执行入口

打开：

`langgraph/prebuilt/tool_node.py`

找到：

```python
def _run_one(...):
    ...
```

核心代码：

```python
tool = self.tools_by_name.get(
    call["name"]
)

tool_request = ToolCallRequest(
    tool_call=call,
    tool=tool,
    state=tool_runtime.state,
    runtime=tool_runtime,
)
```

然后：

```python
if self._wrap_tool_call is None:
    return self._execute_tool_sync(
        tool_request,
        input_type,
        config,
    )
```

如果存在包装器：

```python
def execute(req):
    return self._execute_tool_sync(
        req,
        input_type,
        config,
    )

return self._wrap_tool_call(
    tool_request,
    execute,
)
```

这段源码体现了一个非常明确的设计：

**ToolNode 把真正执行工具的操作封装成一个回调，交给外层包装器决定什么时候调用。**

### 9.2 为什么传入 `execute` 而不是直接执行？

因为包装器可以实现：

```python
def audit_wrapper(request, execute):

    print("Before Tool")

    result = execute(request)

    print("After Tool")

    return result
```

也可以拒绝某次操作：

```python
def deny_wrapper(request, execute):

    if request.tool_call["name"] == "delete":
        return ToolMessage(
            content="Delete operation denied",
            tool_call_id=request.tool_call["id"],
            name="delete",
            status="error",
        )

    return execute(request)
```

后一段是教学示例，并非 DeepAgents 默认权限策略。

这里体现了 Wrapper 的两个重要能力：

1. 执行前后插入逻辑。
2. 在某些情况下不执行原始工具。

当然，实际包装器必须遵守 ToolMessage 和 Command 的返回协议。

### 9.3 DeepAgents 的 FilesystemMiddleware 也实现了这个 Hook

回到：

`deepagents/middleware/filesystem.py`

找到：

```python
def wrap_tool_call(
    self,
    request: ToolCallRequest,
    handler,
) -> ToolMessage | Command:
    ...
```

它会进行一些工具结果处理，例如：

```python
if error := _parallel_file_mutation_error(request):
    return error
```

然后：

```python
tool_result = handler(request)
```

后续还可能处理大型工具输出。

为什么需要检查并行文件修改？

因为同一次模型响应可能同时请求：

```text
edit_file("/refund.py", ...)
edit_file("/refund.py", ...)
```

对同一文件并行修改可能产生冲突。

因此 DeepAgents 对这种情况提供了额外保护。

注意：这并不意味着所有跨轮次、跨进程或外部系统的文件修改冲突，都被这个 Hook 自动解决。

### 这里形成了两级扩展

```text
                 LangChain
               AgentMiddleware
                      |
                      v
               wrap_tool_call
                      |
                      v
                 create_agent()
                      |
                      v
              Wrapper Composition
                      |
                      v
               LangGraph ToolNode
                      |
                      v
                execute(request)
                      |
                      v
                 Actual Tool
```

所以前面你提出的问题，现在可以得到更完整的答案：

**LangChain 负责解析和组合 AgentMiddleware，LangGraph ToolNode 提供实际调用包装器的位置。**

---

## 十、终于进入真正的文件读取

到目前为止，我们只是：

```text
创建工具
注册工具
找到工具
准备参数
进入包装器
```

还没有真正读取文件。

现在回到 DeepAgents 的：

```python
sync_read_file(...)
```

它的核心代码如下：

```python
def sync_read_file(
    file_path: str,
    runtime: ToolRuntime[None, FilesystemState],
    offset: int = DEFAULT_READ_OFFSET,
    limit: int = DEFAULT_READ_LIMIT,
) -> ToolMessage | Command:

    resolved_backend = self.backend

    try:
        validated_path = validate_path(file_path)
    except ValueError as e:
        return ToolMessage(
            content=f"Error: {e}",
            name="read_file",
            tool_call_id=runtime.tool_call_id,
            status="error",
        )

    if (
        error := _permission_error(
            self._permissions,
            "read",
            self.backend,
            validated_path,
        )
    ) is not None:
        return ToolMessage(
            content=error,
            name="read_file",
            tool_call_id=runtime.tool_call_id,
            status="error",
        )

    read_result = resolved_backend.read(
        validated_path,
        offset=offset,
        limit=limit,
    )

    return _handle_read_result(
        read_result,
        validated_path,
        runtime.tool_call_id,
        offset,
        limit,
    )
```

这是接近固定版本真实实现的核心代码节选，省略了文档字符串。

现在终于能清楚看到几个步骤。

### 10.1 第一步：确定 Backend

```python
resolved_backend = self.backend
```

为什么还需要 Backend？

因为 `read_file` 是面向 Agent 的工具接口。

但文件到底存放在什么地方，是另一件事。

例如：

```text
Graph State
Host Filesystem
Remote Sandbox
External Store
```

这些都可能是文件的真实存储位置。

所以 DeepAgents 不应该把：

```python
open(file_path)
```

直接写死在工具里。

它通过 Backend 隔离存储实现。

### 10.2 第二步：路径校验

```python
validated_path = validate_path(file_path)
```

这是在检查工具请求的路径形式。

如果路径不符合要求，会返回错误 ToolMessage。

### 10.3 第三步：权限检查

```python
_permission_error(...)
```

判断该路径的读取是否被允许。

如果不允许，不会继续执行：

```python
backend.read(...)
```

这里体现的是：

**模型提出请求，不代表 Harness 必须执行请求。**

### 10.4 第四步：真正读取文件

```python
read_result = resolved_backend.read(
    validated_path,
    offset=offset,
    limit=limit,
)
```

到这一步，才真正进入文件访问。

这也是第 04 章 Backend 的入口。

### 10.5 第五步：转换成 ToolMessage

```python
return _handle_read_result(
    read_result,
    validated_path,
    runtime.tool_call_id,
    offset,
    limit,
)
```

为什么不能直接把 Backend 的 `ReadResult` 原样返回？

因为：

```python
ReadResult
```

是 Backend 层的结果结构。

它可能包含：

```text
file_data
error
start_line
end_line
total_lines
```

而模型下一轮需要的是符合 Tool Calling 协议的反馈：

```python
ToolMessage
```

因此需要做结果转换。

例如：

```python
ToolMessage(
    content="文件内容...",
    name="read_file",
    tool_call_id="call-001",
    status="success",
)
```

这就完成了从 Backend 数据到 Agent 消息协议的转换。

---

## 十一、一次读取为什么会产生两个层次的结果？

这是理解整个架构时容易忽略的一点。

我们现在有两个结果类型。

### Backend Result

例如：

```python
ReadResult(
    file_data=...,
    error=None,
)
```

它回答：

> Backend 读取文件的结果是什么？

### ToolMessage

例如：

```python
ToolMessage(
    content="...",
    name="read_file",
    tool_call_id="call-001",
)
```

它回答：

> 这次 Tool Call 应该向 Agent 返回什么反馈？

它们的职责不同。

```text
                    Tool Call
                        |
                        v
                    read_file
                        |
                        v
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
                   Graph State
                        |
                        v
                    Model Node
```

这种分层有一个很重要的收益：

**Backend 可以专注于文件操作，不必依赖模型消息协议；Tool 可以专注于将 Backend 能力暴露给 Agent。**

这也意味着以后更换 Backend 时，不需要重新设计 Tool Calling 协议。

---

## 十二、如果文件不存在，Agent Loop 会怎样处理？

假设模型调用：

```python
read_file(
    file_path="/missing.py"
)
```

Backend 返回错误。

`sync_read_file()` 会把该错误转换成：

```python
ToolMessage(
    content="Error: ...",
    name="read_file",
    tool_call_id="call-001",
    status="error",
)
```

这个 ToolMessage 会进入 Graph State。

下一轮模型就能看到：

```text
刚才读取文件失败了。
```

然后可能决定：

```python
glob(
    pattern="**/*refund*.py"
)
```

重新搜索文件。

这是 Tool Calling 的一个重要设计思想：

**可恢复的工具错误，可以作为新的环境反馈交还给模型，让 Agent 决定是否修正行动。**

但需要注意，不能把所有异常都转换成普通错误消息。

例如：

```text
Graph Interrupt
严重运行时异常
程序错误
```

可能需要直接向上抛出，而不是让模型继续尝试。

因此 ToolNode 还需要区分：

```text
模型可以修正的工具调用错误

与

Runtime 必须中止或向上报告的错误
```

这也是为什么 ToolNode 的异常处理源码比简单的 `try/except` 复杂得多。

---

## 十三、ToolMessage 与 Command 为什么都可以作为工具结果？

前面我们主要讨论：

```python
ToolMessage
```

它最适合表示：

> 某个工具执行完成，向模型返回一段结果。

但有时候工具不只是产生文本。

例如：

```text
更新 Todo 列表
修改 Graph State
触发其他节点
```

这时，仅返回一段 ToolMessage 不一定够用。

LangGraph 还允许工具返回：

```python
Command(...)
```

例如概念上：

```python
Command(
    update={
        "task_status": "verified"
    }
)
```

实际使用时，工具触发的消息更新还需要符合 LangGraph 对 Tool Call 关联的要求。

### 两种结果的区别

| 返回类型 | 主要表达 |
|---|---|
| `ToolMessage` | 这次工具执行向 Agent 反馈什么 |
| `Command` | 这次执行需要对 Graph State 或控制流产生什么更新 |

所以：

```text
工具只能返回文本
```

并不是 LangGraph 的完整能力边界。

它还能够参与状态更新。

这个设计对于下一步研究 Planning 很重要。

例如：

```text
write_todos Tool
       |
       v
Command(update={"todos": ...})
       |
       v
Graph State
```

但本章先只追踪最普通的 `read_file`，不把 Todo 的具体实现提前展开。

---

## 十四、模型看见的工具，与真正可执行的工具，是同一回事吗？

不是。

这是整个 Tool Calling 架构中一个非常重要的区分。

### 模型可见工具

这些工具会被放进模型请求中。

例如：

```python
request.tools
```

模型会根据工具 Schema 决定是否调用。

### Runtime 已注册工具

这些工具会在 ToolNode 的注册表中。

例如：

```python
tool_node.tools_by_name
```

Runtime 根据工具名称找到对应对象。

### Backend 能力

即使工具对象存在，Backend 也可能不支持对应操作。

例如：

```python
StateBackend()
```

支持虚拟文件读写。

但它不提供通用 Shell 执行能力。

因此：

```python
execute(...)
```

是否能真正使用，还取决于 Backend。

### DeepAgents 怎样处理 Backend 能力不匹配？

源码中有：

```python
_filter_unsupported_tools_and_apply_prompt(...)
```

它会在模型请求阶段，根据 Backend 实际能力过滤不支持的工具。

例如一个 Backend 不支持 Shell：

```text
注册阶段：
    FilesystemMiddleware 创建 execute 工具

模型请求阶段：
    判断 Backend 不支持 execute
            |
            v
    不将 execute 暴露给本轮模型
```

但这里必须强调：

**隐藏工具不是可靠的安全授权。**

真正的安全边界仍然需要在执行侧验证能力和权限。

否则即使模型通常看不见某个工具，仍然可能存在绕过正常模型选择路径的调用方式。

### 三层能力边界

```text
                Model-visible Tools
                        |
                        v
                 Model Tool Call
                        |
                        v
                 Registered Tools
                        |
                        v
                Runtime Validation
                        |
                        v
                Backend Capabilities
                        |
                        v
                   Real Execution
```

所以我们应该将三件事分别设计：

1. 模型可以知道哪些工具。
2. Runtime 可以分发哪些工具。
3. 系统最终允许执行哪些操作。

这三个集合不一定完全相同。

---

## 十五、将全部真实调用链连接起来

现在我们已经自然引入了：

```text
Tool Schema
Tool Registry
ToolNode
ToolRuntime
Tool Wrapper
Backend
ToolMessage
```

终于可以把它们放进同一条调用链。

假设模型决定：

```python
read_file(
    file_path="/refund.py"
)
```

实际流程如下：

```text
                  create_deep_agent()
                           |
                           v
                  FilesystemMiddleware
                           |
                           v
                    _create_tools()
                           |
                           v
                   _create_read_file_tool()
                           |
                           v
                  StructuredTool("read_file")
                           |
                           v
                   middleware.tools
                           |
                           v
                  LangChain create_agent()
                           |
                           v
                    middleware_tools
                           |
                           v
                     available_tools
                           |
                           v
                      ToolNode(...)
                           |
                           v
                 tools_by_name 注册表
                           |
                    构建阶段结束
                           |
                           v
                      agent.invoke()
                           |
                           v
                       Model Node
                           |
                           v
                    model.bind_tools()
                           |
                           v
                      model.invoke()
                           |
                           v
                      AIMessage
                           |
                           v
                   tool_calls[0]
                           |
                           v
                  Graph Conditional Edge
                           |
                           v
                    Send("tools")
                           |
                           v
                       ToolNode
                           |
                           v
                        _run_one()
                           |
                           v
             tools_by_name["read_file"]
                           |
                           v
                    ToolCallRequest
                           |
                           v
                   wrap_tool_call()
                           |
                           v
                  _execute_tool_sync()
                           |
                           v
                    _inject_tool_args()
                           |
                           v
                       tool.invoke()
                           |
                           v
                    sync_read_file()
                           |
                           v
                    validate_path()
                           |
                           v
                   permission check
                           |
                           v
                     backend.read()
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
                       Model Node
```

这张图贯穿三个仓库：

**DeepAgents：** 创建文件工具，并通过 Backend 实现文件能力。

**LangChain：** 收集工具、组织 Middleware、创建 Agent Graph。

**LangGraph：** 路由 Tool Call、注入 Runtime、执行工具、返回状态更新。

这就是第 03 章需要掌握的完整主线。

---

## 十六、实验：用真正的 DeepAgents 文件工具验证 Tool Calling

这次实验不重新模拟 `read_file`。

我们直接使用 DeepAgents 的：

```python
FilesystemMiddleware
```

以及：

```python
FilesystemBackend
```

让真实文件工具参与 LangChain 的 ToolNode 执行流程。

为了避免依赖模型 API Key，我们用可控的 Fake Chat Model 代替真实 LLM。

这样能把实验重点集中在 Tool Calling 本身。

### 16.1 安装依赖

```bash
pip install "deepagents==0.7.32"
```

该版本会引入对应的 LangChain、LangGraph 依赖。

### 16.2 完整实验代码

创建：

`test_deepagents_tool_calling.py`

```python
from pathlib import Path
from tempfile import TemporaryDirectory

from deepagents.backends import FilesystemBackend
from deepagents.middleware.filesystem import (
    FilesystemMiddleware,
)

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware

from langchain_core.language_models.fake_chat_models import (
    FakeMessagesListChatModel,
)

from langchain_core.messages import (
    AIMessage,
    ToolMessage,
)


# ==========================================
# 1. 定义可控的 Fake Model
# ==========================================

class ScriptedModel(FakeMessagesListChatModel):

    def bind_tools(self, tools, **kwargs):
        # Fake Model 不会调用真实 Provider。
        # Tool Call 已经预先写在 responses 中。
        return self


# ==========================================
# 2. 创建审计 Middleware
# ==========================================

class AuditToolMiddleware(AgentMiddleware):

    def wrap_tool_call(self, request, handler):

        name = request.tool_call["name"]

        print(f"[AUDIT] Before {name}")

        result = handler(request)

        print(f"[AUDIT] After {name}")

        return result


# ==========================================
# 3. 创建临时项目目录
# ==========================================

with TemporaryDirectory() as root:

    refund_file = Path(root) / "refund.py"

    refund_file.write_text(
        "def calculate_refund(amount):\n"
        "    return round(amount)\n",
        encoding="utf-8",
    )

    # ======================================
    # 4. 创建真实的 DeepAgents Backend
    # ======================================

    backend = FilesystemBackend(
        root_dir=root,
        virtual_mode=True,
    )

    # ======================================
    # 5. 创建真实的文件 Middleware
    # ======================================

    filesystem = FilesystemMiddleware(
        backend=backend,
        tools=["read_file"],
    )

    # 工具注册在 Middleware 中。
    assert any(
        tool.name == "read_file"
        for tool in filesystem.tools
    )

    # ======================================
    # 6. 创建模型返回的 Tool Call
    # ======================================

    tool_call_message = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "read_file",
                "args": {
                    "file_path": "/refund.py",
                    "offset": 0,
                    "limit": 20,
                },
                "id": "read-call-001",
            }
        ],
    )

    final_message = AIMessage(
        content="已经读取 refund.py，发现 round(amount)。"
    )

    model = ScriptedModel(
        responses=[
            tool_call_message,
            final_message,
        ]
    )

    # ======================================
    # 7. 创建真正的 LangChain Agent
    # ======================================

    agent = create_agent(
        model=model,
        middleware=[
            filesystem,
            AuditToolMiddleware(),
        ],
    )

    # ======================================
    # 8. 执行
    # ======================================

    result = agent.invoke({
        "messages": [
            {
                "role": "user",
                "content": "读取 refund.py 并分析精度问题",
            }
        ]
    })

    # ======================================
    # 9. 提取工具反馈
    # ======================================

    tool_messages = [
        message
        for message in result["messages"]
        if isinstance(message, ToolMessage)
    ]

    assert len(tool_messages) == 1

    tool_result = tool_messages[0]

    # ToolMessage 必须与模型发起的请求关联。
    assert tool_result.tool_call_id == "read-call-001"

    # 文件确实经过 Backend 被读取。
    assert "round(amount)" in str(
        tool_result.content
    )

    # ======================================
    # 10. 打印结果
    # ======================================

    print("\n=== Tool Result ===")
    print(tool_result.content)

    print("\n=== Final Model Message ===")
    print(result["messages"][-1].content)

    print("\nTool Calling flow verified.")
```

### 16.3 为什么这个实验有意义？

因为它使用了真实的：

```text
FilesystemMiddleware
StructuredTool
ToolNode
FilesystemBackend
ToolMessage
```

只有模型本身是预设的 Fake Model。

所以我们可以验证：

**第一，DeepAgents 真实注册了 `read_file` 工具。**

**第二，LangChain 从 Middleware 中收集了该工具。**

**第三，ToolNode 通过工具名称找到并执行了它。**

**第四，`wrap_tool_call()` 在工具执行前后介入。**

**第五，真实文件内容通过 Backend 返回，并进入 ToolMessage。**

**第六，模型下一轮能够接收到工具结果。**

### 16.4 预期执行顺序

```text
[AUDIT] Before read_file

[AUDIT] After read_file

=== Tool Result ===
包含 refund.py 的文件内容

=== Final Model Message ===
已经读取 refund.py，发现 round(amount)。

Tool Calling flow verified.
```

这里用 `TemporaryDirectory()` 创建了一个隔离的演示目录，避免访问真实项目文件。

`virtual_mode=True` 会将 `/refund.py` 映射到该目录中的对应文件，但它不是操作系统级沙箱。

### 16.5 为什么实验使用 `create_agent()` 而不是 `create_deep_agent()`？

因为这一章只想验证：

```text
DeepAgents FilesystemMiddleware
         |
         v
LangChain Tool Registration
         |
         v
LangGraph ToolNode
         |
         v
Backend
```

使用 `create_agent()` 可以避免额外启用 Subagents、Summarization 等机制，方便观察这条调用链。

但这不是一个伪造的 DeepAgents 文件工具。

`FilesystemMiddleware`、`StructuredTool` 和 `FilesystemBackend` 都来自真实 DeepAgents 实现。

在完整的：

```python
create_deep_agent(...)
```

中，同样会经过这套底层机制。

本例依据固定版本源码与相关 API 编写；当前环境没有安装这些依赖，因此尚未执行验证，实际结果以本地安装版本为准。

---

## 十七、再看这个架构解决了哪些设计问题

我们从一开始最简单的代码：

```python
if name == "read_file":
    return read_file(path)
```

逐步走到了一个比较复杂的执行结构。

是不是过度设计？

只有对照实际问题，才能判断。

| 设计 | 如果没有它，会出现什么问题？ | 引入的代价 |
|---|---|---|
| `BaseTool` / `StructuredTool` | 工具缺少统一的调用协议 | 需要包装函数和声明 Schema |
| Tool Registry | 每新增工具都要手写分发逻辑 | 要维护工具名称和注册关系 |
| `ReadFileSchema` | 模型参数结构不可控 | Schema 不能替代业务和权限校验 |
| `ToolRuntime` | 模型参数与内部执行信息混杂 | 增加注入和上下文传递机制 |
| `wrap_tool_call` | 权限、审计、重试逻辑散落在工具里 | 包装链顺序与错误处理更复杂 |
| `BackendProtocol` | 工具逻辑与具体存储方式绑定 | 多一层抽象和配置 |
| `ToolMessage` | 执行结果难以关联到具体 Tool Call | 需要维护消息 ID 与结构 |
| `Command` | 工具无法方便地参与 Graph State 更新 | 需要理解 Runtime 状态协议 |

可以看到：

**这套抽象不是为了让一次 `read_file()` 更复杂，而是为了让几十种工具、不同 Backend 和多种执行策略能够使用同一套工具执行机制。**

这才是其真正的架构价值。

---

## 十八、Tool Calling 最重要的安全边界

最后必须强调一个生产级 Agent Harness 的基本原则：

**Tool Call 是模型产生的请求，不是系统必须执行的命令。**

例如模型可能请求：

```python
{
    "name": "execute",
    "args": {
        "command": "rm -rf /project"
    }
}
```

我们不能因为它符合某个工具 Schema，就直接认为应该执行。

真正的控制应该由系统承担：

```text
               Untrusted Model Output
                          |
                          v
                    Tool Call
                          |
                          v
                  Registered Tool?
                          |
                          v
                  Valid Parameters?
                          |
                          v
                   Allowed Action?
                          |
                          v
                 Backend Supports It?
                          |
                          v
               Approval Required?
                          |
                          v
                    Execute Tool
```

其中：

- Schema 验证参数形状。
- 工具注册表限制可分发的工具。
- Middleware 提供执行拦截点。
- Backend 决定实际执行能力和环境边界。
- HITL 可以在敏感操作之前要求人工批准。

这些控制并不必然在同一个位置完成。

尤其是 Shell 执行，不能仅因为存在 `SandboxBackendProtocol` 就假定执行环境具备强隔离。

**Agent Harness 的安全性来自多个明确的执行边界，而不是来自模型的自我约束。**

---

## 十九、本章完整总结

我们从用户的一个简单任务开始：

> 模型想读取 `/refund.py`。

首先发现：

```python
AIMessage.tool_calls
```

只是行动请求。

它还不是真实执行。

为了把请求变成实际操作，系统必须依次解决：

```text
工具如何定义？
    |
    v
工具如何注册？
    |
    v
工具如何查找？
    |
    v
参数如何验证？
    |
    v
内部 Runtime 如何注入？
    |
    v
执行前后如何拦截？
    |
    v
真实操作在哪里完成？
    |
    v
结果如何反馈给模型？
```

DeepAgents、LangChain、LangGraph 分别承担其中不同的责任。

```text
                     DeepAgents
                         |
                         v
                 FilesystemMiddleware
                         |
                         v
                  Create Real Tools
                         |
                         v
                      LangChain
                         |
                         v
                     create_agent
                         |
                         v
                   Collect Tools
                         |
                         v
                      LangGraph
                         |
                         v
                       ToolNode
                         |
                         v
                    ToolRuntime
                         |
                         v
                     Tool.invoke
                         |
                         v
                  DeepAgents Backend
                         |
                         v
                     ToolMessage
                         |
                         v
                      Agent State
                         |
                         v
                       Model
```

最终，本章最重要的结论是：

> **Tool Calling 是一套将模型产生的行动意图，转换为受控、可验证、可反馈的系统执行过程的协议与运行机制。**

模型通过它获得使用外部能力的方式。

而 Harness 通过它保留对真实执行的控制权。

---

# 下一章：第 04 章——Backend 为什么必须独立于 Tool？

我们现在已经追踪到：

```python
resolved_backend.read(
    validated_path,
    offset=offset,
    limit=limit,
)
```

但接下来还有一个更深的问题：

> 同样是 `read_file("/refund.py")`，为什么有时读取 Graph State，有时读取本地磁盘，有时又要进入远程 Sandbox？

下一章从这个问题开始，研究：

```text
BackendProtocol
StateBackend
FilesystemBackend
StoreBackend
CompositeBackend
SandboxBackendProtocol
LocalShellBackend
```

重点不只是每个 Backend 的实现，而是：

**为什么 Agent 的“工具能力”必须与“实际存储和执行环境”分离，以及这种分离如何支持隔离、持久化和能力复用。**

---

## 本章源码导航

DeepAgents 固定版本：`0.7.32`，Commit `e85c6b9`

| 研究位置 | 文件 | 关键实现 |
|---|---|---|
| DeepAgents 装配 | `deepagents/graph.py` | `create_deep_agent()` |
| 文件工具声明 | `middleware/filesystem.py` | `FilesystemMiddleware._create_tools()` |
| 参数契约 | `middleware/filesystem.py` | `ReadFileSchema` |
| 工具绑定 | `middleware/filesystem.py` | `_create_read_file_tool()` |
| 文件读取 | `middleware/filesystem.py` | `sync_read_file()` |
| 工具结果处理 | `middleware/filesystem.py` | `_handle_read_result()` |
| Agent 工具汇总 | LangChain `agents/factory.py` | `middleware_tools` / `available_tools` |
| ToolNode 注册 | LangGraph `tool_node.py` | `ToolNode.__init__()` |
| ToolNode 分发 | LangGraph `tool_node.py` | `_run_one()` |
| 实际执行 | LangGraph `tool_node.py` | `_execute_tool_sync()` |
| Runtime 注入 | LangGraph `tool_node.py` | `_inject_tool_args()` |
| Backend 读写 | `backends/state.py`、`backends/filesystem.py` | `StateBackend` / `FilesystemBackend` |

### 推荐阅读顺序

```text
create_deep_agent()
        |
        v
FilesystemMiddleware.__init__()
        |
        v
_create_tools()
        |
        v
_create_read_file_tool()
        |
        v
create_agent()
        |
        v
ToolNode.__init__()
        |
        v
ToolNode._run_one()
        |
        v
ToolNode._execute_tool_sync()
        |
        v
sync_read_file()
        |
        v
Backend.read()
        |
        v
ToolMessage
```

### 真实源码

- [DeepAgents `graph.py`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py)
- [DeepAgents `filesystem.py`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py)
- [DeepAgents `FilesystemBackend`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/backends/filesystem.py)
- [DeepAgents `StateBackend`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/backends/state.py)
- [LangChain `factory.py`](https://github.com/langchain-ai/langchain/blob/master/libs/langchain_v1/langchain/agents/factory.py)
- [LangGraph `tool_node.py`](https://github.com/langchain-ai/langgraph/blob/main/libs/prebuilt/langgraph/prebuilt/tool_node.py)

注意：DeepAgents 固定在指定 Commit，LangChain 和 LangGraph 的链接指向当前主分支。进行严格逐行调试时，应使用实际安装依赖的源码版本，避免版本变化导致内部函数和类型定义不一致。
