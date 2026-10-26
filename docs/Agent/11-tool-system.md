---
title: 第十一章：Tool System——为什么不能直接执行模型返回的命令？
sidebar_label: 第十一章：Tool System
description: 从读取源码到工具注册、参数校验、执行路由、结果配对及错误隔离
sidebar_position: 12
tags: [Agent, Harness, Tools, Codex]
---


# 第十一章：Tool System——为什么 Agent 不能直接调用函数？

上一章，我们已经实现了 Agent Loop。

模型决定下一步行动，Harness 执行工具，再把结果交还给模型。

```text
LLM
 |
 | tool_call
 v
Harness
 |
 | execute_tool()
 v
Tool
 |
 | result
 v
LLM
```

看起来非常简单。

但是，我们当时留下了一个没有解释的函数：

```python
result = await execute_tool(call)
```

**这个函数内部究竟应该做什么？**

我们继续开发那个负责修复优惠券退款 Bug 的 Coding Agent。

## 一、最简单的工具执行方式

目前，我们只有三个工具：

```python
grep(pattern)
read_file(path)
execute(command)
```

模型返回：

```json
{
  "id": "call_001",
  "name": "read_file",
  "arguments": {
    "path": "src/refund.py"
  }
}
```

小李最初写了一个简单的分发函数：

```python
def execute_tool(name, args):
    if name == "grep":
        return grep(**args)

    if name == "read_file":
        return read_file(**args)

    if name == "execute":
        return execute(**args)

    raise ValueError("Unknown tool")
```

这有什么问题？

其实，对于只有三个工具的 Demo，**完全没有问题**。

不应该因为成熟框架使用 Tool Registry，就认为所有项目从第一天起都需要它。

我们先让系统运行起来。

Agent 搜索退款计算函数，读取代码，然后修改文件。

一切正常。

直到某次模型输出：

```json
{
  "name": "read_file",
  "arguments": {
    "file_path": "src/refund.py"
  }
}
```

我们的工具函数要求的是 `path`，模型却传了 `file_path`。

结果：

```text
TypeError:
read_file() got an unexpected keyword argument 'file_path'
```

小李发现，问题并不在工具内部。

**模型生成的参数，与工具函数实际接受的参数，不一定一致。**

为什么会出现这种问题？

因为此前，我们只告诉模型“存在一个 read_file 工具”，却没有严格规定这个工具的输入格式。

这迫使我们引入第一个机制。

## 二、第一次改进：Tool Definition

### 为什么工具需要一个独立的定义？

我们之前把工具当成普通 Python 函数。

但从模型的角度，它并不能直接阅读 Python 函数签名或调用运行时对象。

模型需要的是一个明确的工具契约：

```json
{
  "name": "read_file",
  "description": "读取项目中的文件内容",
  "parameters": {
    "type": "object",
    "properties": {
      "path": {
        "type": "string",
        "description": "项目内的文件路径"
      }
    },
    "required": ["path"]
  }
}
```

这个定义告诉模型：

- 工具叫什么。
- 解决什么问题。
- 接受什么参数。
- 参数是什么类型。
- 哪些参数必填。

这就是 **Tool Definition / Tool Schema**。

注意，我们现在有了两样东西：

```text
Tool Definition
    给 LLM 看

Tool Implementation
    Harness 实际执行
```

例如：

```python
# Tool Definition
{
    "name": "read_file",
    "parameters": {
        "path": "string"
    }
}

# Tool Implementation
def read_file(path: str):
    ...
```

它们描述同一个能力，但处于不同的系统边界。

那么，有了 Schema，模型是不是一定会生成合法参数？

**不是。**

Schema 能够约束和引导模型输出，但不能代替运行时验证。

模型可能仍然返回：

```json
{
  "path": 123
}
```

或者：

```json
{
  "file_path": "refund.py"
}
```

因此，Harness 必须在执行前验证参数。

```text
LLM 生成 Tool Call
        |
        v
   Schema Validation
        |
     +--+--+
     |     |
   合法   不合法
     |     |
     v     v
   执行   返回错误
```

如果没有这一步，模型生成的任意参数就可能直接进入文件系统、Shell 或数据库。

### 源码对照：OpenCode

这一层设计在 OpenCode 中非常清晰。

阅读：

[OpenCode tool.ts](https://github.com/anomalyco/opencode/blob/dev/packages/core/src/tool/tool.ts)

在 OpenCode 的 Core Tool 实现中，`Tool.make()` 将工具描述、输入 Schema、输出 Schema 和执行函数组合起来。

其中一个非常关键的操作是：

```typescript
Schema.decodeUnknownEffect(config.input)(call.input)
```

这里的意思是：

**先使用工具定义的输入 Schema 解码和校验模型参数，然后才进入工具执行函数。**

执行结束后，OpenCode 还会校验输出是否满足声明的输出 Schema。

为什么连输出都要校验？

假设工具声明返回：

```json
{
  "success": true,
  "content": "..."
}
```

但实现却因为 Bug 返回了 `null`。

如果完全不做校验，下游系统可能错误地把它当作正常工具结果。

所以真正的 Tool Contract 包含：

```text
Input Schema
     |
     v
Tool Implementation
     |
     v
Output Schema
```

这里我们理解了一个重要设计：

> Tool Definition 不只是写给模型看的说明，也是 Harness 验证模型输入、约束工具输出的一份契约。

---

## 三、第二次改进：工具越来越多，为什么需要 Tool Registry？

经过几周开发，我们的 Coding Agent 增加了更多能力：

```text
grep
glob
read_file
write_file
edit_file
execute
git_status
git_diff
run_tests
search_docs
create_task
...
```

工具数量达到几十个。

小李原来的分发函数变成：

```python
if name == "grep":
    ...
elif name == "read_file":
    ...
elif name == "write_file":
    ...
elif name == "git_diff":
    ...
```

每增加一个工具，都需要修改分发函数。

更麻烦的是，不同团队开发的工具也想接入。

例如，数据库团队提供一个新工具：

```python
query_order_database(sql)
```

我们难道每次都修改 Agent Loop 的核心代码？

小李意识到：

**工具的增加，不应该导致 Agent Loop 本身发生变化。**

于是，他把工具统一注册到一个容器里。

```python
class ToolRegistry:
    def __init__(self):
        self.tools = {}

    def register(self, name, tool):
        if name in self.tools:
            raise ValueError("Duplicate tool")
        self.tools[name] = tool

    def get(self, name):
        return self.tools.get(name)
```

使用时：

```python
registry.register("grep", grep_tool)
registry.register("read_file", read_file_tool)
registry.register("execute", execute_tool)
```

执行工具时：

```python
tool = registry.get(call.name)

if tool is None:
    return tool_error("Unknown tool")

return tool.execute(call.arguments)
```

Agent Loop 不再需要知道每个工具的具体实现。

它只需要请求 Registry 找到相应工具。

```text
Agent Loop
    |
    v
Tool Registry
    |
    +-- grep
    |
    +-- read_file
    |
    +-- execute
    |
    +-- git_diff
    |
    +-- MCP Tools
```

这就是 **Tool Registry（工具注册表）**。

它解决的不是“如何执行某一个函数”，而是“如何管理不断增长、来源不同的一组可调用能力”。

### 源码对照：Codex

打开：

[Codex tools/registry.rs](https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/registry.rs)

重点阅读：

- `ToolRegistry`
- `RegisteredTool`
- `register_trusted`
- `dispatch_any_with_state`

Codex 的 `ToolRegistry` 不只是一个简单的 HashMap。

它还维护工具运行时、暴露方式和策略等信息。

`RegisteredTool` 将 Runtime 和 Exposure 等属性关联起来。

而 `dispatch_any_with_state()` 负责根据调用信息查找对应工具，并进入执行流程。

其中有一个关键步骤：

```rust
let tool = match self.tool(&tool_name) {
    // ...
};
```

这与我们推导的 `registry.get(name)` 本质一致。

区别在于真实 Runtime 还需要处理调用跟踪、取消、工具类型检查和 Hooks 等。

为什么不直接在 Registry 里保存函数？

因为一个真实工具不只有函数本身，还可能包含：

```text
Tool
 |
 +-- Name
 +-- Description / Schema
 +-- Executor
 +-- Exposure
 +-- Policy
 +-- Runtime Metadata
```

但也要注意：Registry 可以负责保存工具及其元数据，权限执行、参数验证等职责则可能由其他组件完成。

不同项目不一定把所有能力都放在同一个类里。

---

## 四、第三次改进：注册了工具，为什么不一定要给模型看？

现在小李接入了一个数据库工具：

```python
delete_order(order_id)
```

这个工具属于管理员使用的内部能力。

但是我们发现，只要把所有注册的 Tool Definition 都发给模型，模型就有机会请求它。

于是一个问题出现了：

**Tool Registry 里存在的工具，是否应该全部暴露给当前 Agent？**

答案是否定的。

例如：

```text
Coding Agent
    可以看到：
      grep
      read_file
      edit_file
      execute

Audit Agent
    可以看到：
      query_orders
      check_refund_policy

Admin Agent
    可以看到：
      query_orders
      delete_order
```

这些 Agent 可以共享底层工具实现，但它们看到的工具集合不同。

这里我们需要区分三个概念：

1. **Registered Tools**：系统已经注册了哪些工具。
2. **Exposed Tools**：本次模型请求能看到哪些工具。
3. **Authorized Tools**：这次实际执行被允许哪些操作。

三者不能简单画等号。

例如，模型没有看到 `delete_order`，不等于底层执行入口就可以不做权限检查。

因为模型输出可能出错，其他插件也可能绕过普通工具列表直接构造调用。

因此：

**工具是否向模型展示，是能力发现问题；工具是否被允许执行，是安全问题。**

### 源码对照：DeepAgents

我们看：

[DeepAgents graph.py](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/graph.py)

`create_deep_agent()` 既接受调用者通过 `tools=` 传入的工具，也装配内置 Middleware 提供的工具。

例如 `FilesystemMiddleware` 可以提供：

```text
ls
read_file
write_file
edit_file
glob
grep
execute（取决于 Backend）
```

其中 `execute` 的能力取决于所选择的 Backend 是否支持相应的沙箱执行接口。

当前 DeepAgents 也支持配置部分文件工具的允许列表，例如：

```python
from deepagents.middleware.filesystem import FilesystemMiddleware

filesystem = FilesystemMiddleware(
    tools=["read_file", "glob", "grep"]
)
```

这样可以限制所使用的内置文件工具集合。

这不只是 Prompt 控制，而是改变了实际可用的工具面。

但需要注意，FilesystemMiddleware 的工具权限检查属于工具层机制，不能替代操作系统或沙箱本身的权限隔离。

### 源码对照：OpenCode

OpenCode 也会基于当前 Agent、会话配置和工具权限，构造有效的工具集合。

可以阅读：

[OpenCode Tool Registry](https://github.com/anomalyco/opencode/blob/dev/packages/core/src/tool/registry.ts)

以及：

[OpenCode Tools 文档](https://opencode.ai/docs/tools/)

OpenCode 允许工具来自内置实现、自定义工具和 MCP 服务。

这说明 Tool Registry 不仅需要管理静态内置工具，还要考虑动态扩展。

现在我们的 Tool System 开始有了两个不同的阶段：

```text
阶段 A：构造模型请求

Registry
   |
   v
选择当前可暴露工具
   |
   v
Tool Definitions
   |
   v
LLM


阶段 B：模型请求执行

Tool Call
   |
   v
查找工具
   |
   v
校验实际执行权限
   |
   v
Tool Executor
```

这两个阶段必须协调，但不能混为一谈。

---

## 五、第四次改进：为什么工具执行不能直接等同于函数调用？

模型请求：

```json
{
  "name": "execute",
  "arguments": {
    "command": "pytest tests/test_refund.py"
  }
}
```

Harness 找到工具，调用执行器。

结果返回：

```text
FAIL test_refund

Expected: 80
Actual:   100
```

我们现在有个问题：

**这算工具执行失败，还是工具执行成功？**

如果从 Shell 的角度看，命令以非零退出码结束。

但从 Tool System 的角度看，工具执行器成功启动了测试进程，并拿到了它的结果。

这是一种正常的环境反馈。

如果直接向外抛异常，中断整个 Agent Loop，反而妨碍 Agent 修复代码。

因此应该区分不同的错误：

| 情况 | 如何处理 |
|---|---|
| 工具不存在 | 返回 Unknown Tool 错误 |
| 参数不符合 Schema | 返回 Invalid Arguments 错误 |
| 命令正常执行，但测试失败 | 返回测试结果和退出码 |
| 权限不足 | 拒绝执行，必要时请求审批 |
| Shell 服务异常 | 根据策略重试或中止 |
| 用户取消执行 | 传播取消信号并清理资源 |

一个工具结果至少应该保留：

```text
Tool Call ID
Tool Name
Execution Status
Output Content
Error Information（如有）
```

例如：

```json
{
  "tool_call_id": "call_008",
  "tool_name": "execute",
  "status": "completed",
  "exit_code": 1,
  "content": "FAIL test_refund..."
}
```

其中 `completed` 表示工具执行过程已结束，并不意味着测试通过。

具体字段格式取决于 Harness 和模型 API；这是我们教学用的统一表示。

### 为什么 Tool Call ID 必须保留？

假设模型同时读取：

```text
call_1 -> read_file("coupon.py")
call_2 -> read_file("refund.py")
```

如果两个工具并发执行，`refund.py` 可能先返回。

Harness 必须按 ID 关联结果，而不是简单根据返回顺序推断对应关系。

否则就可能把退款文件的内容交给错误的调用。

这就是 **Tool Call Correlation（工具调用关联）**。

此外，批量执行时还应保证模型请求的 Tool Call 都得到协议允许的对应结果，或者通过受支持的错误、取消、中断流程处理，不能悄悄丢失某次调用。

---

## 六、第五次改进：为什么 Tool Runtime 需要 Middleware？

现在我们的工具系统已经可以注册、查找、验证和执行工具。

但产品提出了新的要求：

> 所有执行 Shell 的操作必须记录日志；部分危险命令必须审批；临时失败的只读请求可以重试；输出过大的结果必须卸载到文件系统。

最简单的做法是，在每一个工具内部加入逻辑：

```python
def read_file(...):
    check_permission(...)
    log_start(...)
    ...
    log_end(...)
```

然后在 `execute()`、`grep()`、`write_file()` 中重复一次。

问题出现了：

**这些逻辑与工具本身的业务能力无关，却必须在每个工具中重复实现。**

有没有办法在工具执行的统一入口进行拦截？

于是我们自然引入 Middleware。

```text
Tool Call
   |
   v
Permission Middleware
   |
   v
Logging Middleware
   |
   v
Tool Executor
   |
   v
Result Middleware
   |
   v
Tool Result
```

这是一种逻辑分层图；具体 Middleware 可能同时处理执行前和执行后的工作，而不是只能处在其中一个位置。

### 源码对照：DeepAgents / LangChain

这是 DeepAgents 特别值得研究的设计。

DeepAgents 的 Agent Loop 主要由底层 LangChain / LangGraph 驱动。

其中 LangGraph 的 ToolNode 负责执行工具调用。

调用关系可以概括为：

```text
create_deep_agent()
       |
       v
LangChain create_agent()
       |
       v
LangGraph Agent Loop
       |
       v
ToolNode
       |
       v
ToolCallRequest
       |
       v
Middleware Chain
       |
       v
Actual Tool
       |
       v
ToolMessage / Command
```

在 LangChain Middleware 接口中，有一个重要的方法：

```python
wrap_tool_call(request, handler)
```

它允许 Middleware 在工具调用前后介入。

例如，我们自己定义一个简单的日志 Middleware：

```python
def wrap_tool_call(self, request, handler):
    print("Tool started:", request.tool_call["name"])

    result = handler(request)

    print("Tool finished")
    return result
```

这个例子展示 Middleware 的核心思想，不包含完整类声明和异常处理。

其中 `handler(request)` 是继续执行后续 Middleware 或真实工具的入口。

如果不调用 `handler`，Middleware 还可以选择返回模拟结果或拒绝执行。

如果调用多次，可以实现某些重试策略。但对写文件、支付、数据库修改等具有副作用的工具，不能简单地无条件重试。

### DeepAgents 中的真实应用

查看：

[DeepAgents filesystem.py](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/middleware/filesystem.py)

`FilesystemMiddleware` 不只提供文件工具，还能拦截较大的工具结果，将完整内容卸载到文件存储中，减少上下文占用。

这正好连接我们之前研究的 Context Management。

注意职责关系：

```text
Tool Runtime
    负责执行工具

FilesystemMiddleware
    负责提供文件工具并处理部分执行边界逻辑

Context Management
    负责控制哪些输出进入模型上下文

Backend
    负责数据读写和执行能力
```

它们不是四套完全独立的执行流程，而是同一个执行链路中的不同责任层。

---

## 七、第六次改进：为什么工具执行还需要 Runtime Context？

现在 Coding Agent 开始支持多个项目、多个用户。

两个不同用户同时调用：

```text
read_file("src/refund.py")
```

虽然工具名称和参数完全一样，但他们访问的项目不同。

用户 A 应该读取：

```text
/workspaces/project-a/src/refund.py
```

用户 B 应该读取：

```text
/workspaces/project-b/src/refund.py
```

如果工具内部直接使用全局当前目录，就有可能读取错误的项目，甚至发生跨用户数据泄露。

因此，仅有下面的信息是不够的：

```python
execute_tool(name, arguments)
```

还需要执行环境：

```python
execute_tool(
    name,
    arguments,
    runtime_context
)
```

例如：

```text
RuntimeContext
 |
 +-- session_id
 +-- workspace
 +-- user / principal
 +-- cancellation_token
 +-- tool_call_id
 +-- sandbox
 +-- tracing_context
```

这是我们推导出的通用结构，不是某个框架的原样类型定义。

关键是分清：

**Tool Arguments 是模型提供的输入。**

**Runtime Context 是 Harness 提供的受信任执行信息。**

例如用户的权限、真实工作目录、服务端凭据，不应该由模型通过普通参数任意指定。

### 源码对照：OpenCode

OpenCode 的工具接口就区分了 Tool Input 和执行上下文。

查看：

[OpenCode tool.ts](https://github.com/anomalyco/opencode/blob/dev/packages/core/src/tool/tool.ts)

其工具执行接口中可以看到类似：

```typescript
execute(input, context)
```

当前 Core Tool 的 `Context` 中包含 Session、Agent、Assistant Message、Tool Call 等标识。

这样执行器不需要通过模型参数来猜测自己属于哪个会话。

而 OpenCode 的 Registry 与执行服务还负责把工具定义和具体调用联系起来。

为什么这一点重要？

因为工具可能被不同 Agent、不同 Session 甚至不同 Workspace 复用。

**工具实现可以复用，但执行上下文必须与当前请求绑定。**

---

## 八、再进一步：工具执行顺序由谁决定？

假设模型一次请求三个工具：

```text
read_file("coupon.py")
read_file("refund.py")
execute("pytest")
```

可以同时执行吗？

前两个读取操作通常可以并行，但第三个测试操作是否能够并行，取决于它是否依赖正在发生的代码修改或其他操作。

如果模型请求：

```text
edit_file("refund.py", ...)
edit_file("refund.py", ...)
```

直接并发执行就可能出现覆盖或顺序冲突。

因此 Tool System 还需要考虑：

```text
Tool Call Batch
       |
       v
Dependency / Conflict Check
       |
       +--> 可独立并行
       |
       +--> 必须串行
       |
       +--> 需要拒绝或协调
```

实际实现并不一定需要一个通用的依赖规划器。

更常见的做法是：工具通过能力元数据、特定资源锁或执行策略表达并行限制。

例如 Codex 的 `ToolRegistry` 就有与并行工具调用能力相关的判断接口。

但也不能认为所有被标记为只读的工具都天然安全，工具内部可能访问共享服务或依赖其他状态。

这里我们获得了新的设计边界：

**Agent Loop 决定什么时候执行一批工具；Tool Runtime 决定如何调度这些工具，并保持执行结果的一致性。**

---

## 九、最终整理：真实 Harness 的 Tool System 有哪些不同？

现在把三个项目放到同一个工程问题下比较。

| 设计问题 | Codex | DeepAgents / LangGraph | OpenCode |
|---|---|---|---|
| 工具在哪里注册？ | `ToolRegistry` | Agent Tools + Middleware / ToolNode | `Tool.make`、Registry |
| 工具如何描述？ | Tool Spec | `BaseTool` / Structured Tool | Schema 驱动的 Tool Definition |
| 谁负责实际分发？ | Registry / Handler 执行链 | LangGraph `ToolNode` | Tool Runtime / Registry 调用 |
| 如何处理执行上下文？ | `ToolInvocation` 等 | `ToolRuntime` / Graph Runtime | Tool `Context` |
| 如何扩展工具？ | 内置工具、MCP、动态工具 | 自定义 Tool、Middleware | 自定义 Tool、MCP、Plugin |
| 如何控制权限？ | Tool Policy、审批与 Sandbox | Middleware、FilesystemPermission、HITL | Permission Rules |
| 工具输出如何管理？ | Tool Output 与响应转换 | `ToolMessage`、Offloading | Tool Output、截断与存储 |

这些项目的共同点并不是使用相同的类名，而是都需要解决几类基本问题：

1. 把工具能力描述给模型。
2. 将模型的工具请求转换为可执行操作。
3. 在执行边界检查输入与权限。
4. 将执行结果可靠地送回模型。
5. 支持扩展、观测与取消。

差别在于职责被拆分到哪里。

**Codex** 偏向显式的 Runtime、Registry、Handler 与执行策略。

**DeepAgents** 偏向 Middleware 组合，通过 LangChain / LangGraph 的工具执行基础设施工作。

**OpenCode** 则突出 Schema 化工具定义、运行时注册与可扩展工具服务。

没有必要认为其中一种组织方式一定优于其他两种。它们面对的兼容性、扩展性和产品边界不同。

---

## 十、如果自己实现 Tool System，应该长什么样？

现在我们把之前学到的东西放在一起：

```text
                   Agent Loop
                       |
                       v
                  Model Call
                       |
                       v
                Tool Call Batch
                       |
                       v
                Tool Dispatcher
                       |
               +-------+-------+
               |               |
               v               v
         Tool Registry    Runtime Context
               |               |
               +-------+-------+
                       |
                       v
                Input Validation
                       |
                       v
               Permission / Approval
                       |
                       v
                Tool Middleware
                       |
                       v
                 Tool Executor
                       |
                       v
                Output Validation
                       |
                       v
              Result Normalization
                       |
                       v
               ToolMessage / Result
                       |
                       v
                   Agent Loop
```

这是一张逻辑责任图，并不是说所有 Harness 都必须按照这些模块顺序分别调用。

对于一个最小实现，我们可以先保留四个核心接口：

```python
class Tool:
    name: str
    schema: dict

    async def execute(self, args, runtime):
        ...


class ToolRegistry:
    def register(self, tool): ...
    def get(self, name): ...


class ToolDispatcher:
    async def dispatch(self, call, runtime):
        ...


class ToolResult:
    call_id: str
    content: str
    is_error: bool
```

这四个抽象足以支持一个简单的 Tool Calling Agent。

之后根据真实问题逐步增加：

- Permission Policy
- Middleware
- Timeout / Cancellation
- Artifact Storage
- Concurrency Control
- Tracing
- MCP Integration

不要在最开始就实现全部功能。

**一个合理的 Tool System 应该能随着工程需求逐步扩展，而不是要求业务工具理解整个 Agent Harness。**

---

## 十一、本章的五个核心问题

### 1. Tool System 是什么？

Tool System 是 Agent Harness 中管理工具定义、注册、发现、调用、执行及结果回传的机制。

### 2. 为什么需要？

因为模型只能提出工具调用请求，不能可靠地直接执行操作。Harness 必须负责将请求转换成受控的真实执行。

### 3. 没有它会怎样？

简单场景可以通过 `if/else` 工作，但随着工具数量增加，会出现重复分发代码、参数不一致、权限难以管理、执行结果不统一等问题。

### 4. 实际怎么做？

通常包括：

```text
Tool Definition
     |
Tool Registry
     |
Tool Dispatcher
     |
Validation / Policy
     |
Tool Executor
     |
Tool Result
```

根据需求添加 Middleware、并行调度与 Runtime Context。

### 5. 处于整个 Harness 的什么位置？

它位于 Agent Loop 与外部执行环境之间。

向上接收模型的行动请求，向下连接 Shell、文件系统、数据库、MCP 服务等能力，并将执行反馈送回 Agent Loop。

---

## 十二、独立验证：把 Tool System 迁移到另一个场景

现在假设我们不再开发 Coding Agent，而是开发一个数据分析 Agent。

它有三个工具：

```text
query_database(sql)
generate_chart(data)
export_report(format)
```

用户要求：

> 查询最近一周的订单异常数据，生成分析图表，然后导出报告。

由于前面已经建立了 Tool System，新的 Agent 不需要重新编写一套 Agent Loop。

我们只需要：

- 定义三个工具的 Schema。
- 注册工具实现。
- 为查询工具提供只读数据库权限。
- 为导出工具配置存储位置与权限。
- 将工具结果统一转换成模型能够使用的反馈。

同一套 Agent Loop 就能够驱动新的业务场景。

这说明我们真正实现了一个重要的系统分离：

**Agent Loop 不需要知道业务工具如何实现；Tool System 不需要决定整个任务的下一步行动。**

---

## 参考源码与阅读顺序

建议沿着我们遇到的工程问题阅读，而不是从文件第一行读到最后一行。

### OpenCode：先理解工具契约

1. [tool.ts](https://github.com/anomalyco/opencode/blob/dev/packages/core/src/tool/tool.ts)  
   重点：`Tool.make()`、输入输出 Schema、`settle()`。

2. [registry.ts](https://github.com/anomalyco/opencode/blob/dev/packages/core/src/tool/registry.ts)  
   重点：工具注册、有效工具集合与运行时执行边界。

3. [Tools 文档](https://opencode.ai/docs/tools/)  
   重点：内置工具、自定义工具与权限配置。

### DeepAgents：理解工具为什么通过 Middleware 提供

1. [graph.py](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/graph.py)  
   重点：`create_deep_agent()` 如何组装工具和 Middleware。

2. [filesystem.py](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/middleware/filesystem.py)  
   重点：FilesystemMiddleware、Backend、结果卸载。

3. [LangGraph tool_node.py](https://github.com/langchain-ai/langgraph/blob/main/libs/prebuilt/langgraph/prebuilt/tool_node.py)  
   重点：ToolNode 如何接收、查找和执行工具调用。

### Codex：理解真实工具分发与执行控制

1. [tools/registry.rs](https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/registry.rs)  
   重点：ToolRegistry、RegisteredTool、dispatch_any_with_state。

2. [tools/orchestrator.rs](https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/orchestrator.rs)  
   重点：工具执行时的策略与环境控制。

源码均为持续演进的仓库入口，阅读时应固定具体 Commit，以避免不同版本的类型、目录与 API 差异。

## 本章总结

Tool System 最初只是一个普通的函数分发器：

```python
if name == "read_file":
    return read_file(...)
```

但随着工程需求不断增加，我们被迫分离出工具契约、注册表、执行上下文、权限、Middleware 和结果协议。

最终形成的核心认识是：

> **Tool System 不是把函数提供给模型那么简单，而是建立模型决策与真实世界操作之间的一条可验证、可扩展、可控制的执行边界。**

下一章将沿着这条边界继续深入：

**Execution Environment：为什么 Agent 即使拥有合法工具，也不能直接在宿主机上任意执行？**

我们会从一次看似正常的 `execute("pytest")` 出发，逐渐推导 Workspace、Process、Sandbox、Filesystem Isolation、Resource Limit 和执行环境生命周期。
