---
title: 第二章：Agent Loop——从任务执行模型到 LangChain 与 DeepAgents 源码
description: 从 Coding Agent 修复退款 Bug 的需求出发，理解 Agent Loop 的设计动机、模型与执行器的职责分离、Graph 状态路由，以及 create_agent 的真实实现
tags: [DeepAgents, LangChain, LangGraph, Agent Loop, ReAct, 源码精读]
sidebar_position: 2
---

# 第二章：Agent Loop——Agent 为什么需要一个持续执行的循环？

## 一、从一个模型调用无法完成的任务开始

上一章，我们研究了：

```python
create_deep_agent(...)
```

知道它会将模型、工具、Backend 和 Middleware 装配成一个 Agent。

但当我们执行：

```python
agent.invoke({
    "messages": [
        {
            "role": "user",
            "content": "修复优惠券部分退款精度 Bug，并运行测试",
        }
    ]
})
```

时，内部究竟会发生什么？

要回答这个问题，我们先不看 `factory.py`，而是思考这个任务本身。

### 1.1 为什么不能直接调用一次 LLM？

假设我们直接写：

```python
response = model.invoke(
    "请修复 refund.py 中的优惠券退款精度问题"
)
```

模型可能回答：

```text
建议检查 round() 的使用，并改用 Decimal。
```

但这里存在一个明显问题：

**模型并没有读取项目文件，也没有真正修改代码。**

模型只能依据输入生成结果。

它不知道当前：

```text
refund.py 具体写了什么
test_refund.py 包含哪些断言
修改后测试是否通过
```

所以，要完成任务，模型必须获取外部环境的信息，并对外部环境执行操作。

这意味着它需要工具。

### 1.2 有了工具，为什么还不够？

假设我们为模型提供：

```text
read_file
edit_file
execute
```

模型第一次调用：

```python
read_file("/refund.py")
```

得到：

```python
def calculate_refund(amount):
    return round(amount)
```

现在模型才知道问题可能出在哪里。

但接下来它还需要：

```text
分析源码
    |
    v
修改源码
    |
    v
执行测试
    |
    v
读取测试结果
```

这显然不是一次模型调用能完成的。

更重要的是，**模型第一次调用时还不知道后续会发生什么**。

可能修改成功，也可能失败。

可能测试通过，也可能出现新的错误。

因此，后续步骤不能全部预先固定。

这就引出了 Agent Loop 要解决的真正问题：

> 如何让模型根据每一次操作得到的新结果，持续决定下一步应该做什么？

---

## 二、第一次设计：人工编写固定执行流程

如果不使用 Agent，我们可以写一个普通程序：

```python
source = read_file("refund.py")

suggestion = model.invoke(
    f"根据以下代码提出修改建议：{source}"
)

edit_file("refund.py", suggestion.content)

test_result = execute("pytest")

print(test_result)
```

这种程序确实能够串起几步操作。

但是它有一个限制：

**执行顺序完全由程序员提前决定。**

```text
读取文件
    |
    v
调用模型
    |
    v
修改文件
    |
    v
运行测试
    |
    v
结束
```

假设测试失败了：

```text
FAILED test_partial_refund_with_coupon

Expected: Decimal("42.50")
Actual: Decimal("42.49")
```

此时程序不知道应该：

- 再次读取哪个文件。
- 查看哪个测试用例。
- 修改哪段代码。
- 是否需要重新运行全部测试。

我们可以继续写 `if`：

```python
if test_result.failed:
    ...
```

但不同问题会有不同的失败原因，无法为所有情形预先写出固定流程。

### 2.1 这里出现了两种不同的执行模型

**固定工作流：**

```text
开发者预先决定下一步
        |
        v
程序按固定顺序执行
```

**Agent：**

```text
模型根据当前信息决定下一步
        |
        v
执行器完成这一步
        |
        v
模型根据结果重新决定下一步
```

所以 Agent Loop 并不是简单地“重复调用模型”。

它真正引入的能力是：

**让后续操作依赖运行时观察到的结果，而不是全部由开发者事先写死。**

---

## 三、Agent Loop 的理论基础：观察、行动与反馈

现在我们可以自然引入 Agent Loop。

它的基本过程是：

```text
观察当前任务状态
        |
        v
决定下一步行动
        |
        v
执行行动
        |
        v
获得新的观察结果
        |
        v
更新任务状态
        |
        v
再次决定下一步
```

在 Agent 研究中，这与 ReAct 所强调的“推理与行动交替进行”具有直接联系。

但要注意：

现代 Tool Calling Agent 不一定会显式生成一段叫做 `Thought` 的文本。

它完全可以直接输出结构化的 Tool Call。

因此，下面的概念关系更准确：

```text
            Agent Current Context
                     |
                     v
                   Model
                     |
                     v
               Next Decision
                     |
            +--------+--------+
            |                 |
            v                 v
       Final Answer        Tool Call
            |                 |
            v                 v
           END            Tool Execution
                              |
                              v
                         Tool Result
                              |
                              v
                         Agent State
                              |
                              v
                             Model
```

这是一种**反馈驱动的执行循环**。

为什么强调反馈？

因为下一次模型决策使用了上一次操作得到的新信息。

例如：

```text
模型决定运行测试
        |
        v
工具运行 pytest
        |
        v
测试失败
        |
        v
模型看到失败日志
        |
        v
决定进一步调查
```

如果没有将测试结果反馈给模型，那么即使模型调用了一百次，也不构成一个有效的任务执行循环。

### 核心结论

Agent Loop 至少需要四种能力：

1. 模型能够表达下一步操作。
2. 系统能够执行这项操作。
3. 执行结果能够反馈给模型。
4. 系统能够判断何时继续、何时结束。

我们下面逐项在源码中寻找它们。

---

## 四、第一个设计问题：模型怎样表达“下一步行动”？

假设模型已经读取 `refund.py`。

它决定：

> 我需要运行退款测试。

如果模型只是返回普通文本：

```text
请运行 pytest。
```

那么系统还需要从自然语言中解析命令。

这种做法存在明显的问题：

- 工具名称可能不明确。
- 参数可能缺失。
- 文本格式不稳定。
- 容易把自然语言误当成可执行指令。

因此，现代 LangChain Agent 使用结构化 Tool Calling。

### 4.1 `AIMessage.tool_calls`

模型可以返回：

```python
from langchain_core.messages import AIMessage

message = AIMessage(
    content="",
    tool_calls=[
        {
            "name": "execute",
            "args": {
                "command": "pytest tests/test_refund.py"
            },
            "id": "call-001",
        }
    ],
)
```

这里有三个关键字段。

**name**

决定调用哪个工具。

```python
"name": "execute"
```

**args**

提供工具参数。

```python
"args": {
    "command": "pytest tests/test_refund.py"
}
```

**id**

标识这次工具请求。

```python
"id": "call-001"
```

为什么需要 `id`？

因为模型可能一次请求多个工具。

后续工具返回结果时，系统必须知道每个结果对应哪一次 Tool Call。

### 4.2 为什么 Tool Call 不等于工具已经执行？

这是一个特别重要的设计边界。

```python
AIMessage(
    tool_calls=[...]
)
```

只代表：

**模型提出了执行请求。**

它不代表：

**工具已经执行完成。**

模型不应该因为生成了一段 JSON，就直接获得任意外部执行能力。

真正的执行必须由运行时完成。

于是产生了职责分离：

```text
                    LLM
                     |
                     v
                 Tool Call
                     |
                     v
                Agent Runtime
                     |
                     v
             Tool Validation / Policy
                     |
                     v
                Tool Execution
                     |
                     v
                 Tool Result
```

这种分工的好处是：

模型负责决定行动意图，执行器负责实际执行和约束行动。

例如，Shell 命令的权限校验、人工审批、文件系统隔离，都应该放在执行层，而不是寄希望于模型自行遵守。

---

## 五、第二个设计问题：工具执行结果怎样反馈给模型？

模型发出：

```python
{
    "name": "execute",
    "args": {
        "command": "pytest tests/test_refund.py"
    },
    "id": "call-001",
}
```

工具执行后得到：

```text
FAILED test_partial_refund_with_coupon
Expected: 42.50
Actual: 42.49
```

现在我们必须把这个结果提供给模型。

LangChain 使用：

```python
ToolMessage
```

例如：

```python
from langchain_core.messages import ToolMessage

result = ToolMessage(
    content=(
        "FAILED test_partial_refund_with_coupon\n"
        "Expected: 42.50\n"
        "Actual: 42.49"
    ),
    name="execute",
    tool_call_id="call-001",
)
```

注意：

```python
tool_call_id="call-001"
```

与之前的：

```python
AIMessage.tool_calls[0]["id"]
```

相对应。

### 为什么不直接把结果变成普通 HumanMessage？

因为工具结果与用户消息具有不同的语义。

```text
HumanMessage：
    用户提出的要求

AIMessage：
    模型的响应或行动请求

ToolMessage：
    某次工具执行的结果
```

通过独立的 ToolMessage 协议，运行时能够识别：

- 哪次 Tool Call 已经完成。
- 哪些 Tool Call 仍然等待执行。
- 哪个结果属于哪个工具。
- 是否应该继续调用模型。

所以消息协议并不仅仅是为了方便显示。

**它还是 Agent Loop 用来管理行动与反馈的重要数据结构。**

---

## 六、到这里，我们已经可以写出最简单的 Agent Loop

先暂时忽略真实框架。

从前面两节，可以推导出一个教学版循环：

```python
# 概念伪代码，不是 LangChain 原样实现

while True:

    ai_message = model.invoke(messages)

    messages.append(ai_message)

    if not ai_message.tool_calls:
        break

    for call in ai_message.tool_calls:

        tool = tools[call["name"]]

        result = tool.invoke(call["args"])

        messages.append(
            ToolMessage(
                content=str(result),
                tool_call_id=call["id"],
            )
        )
```

这段代码抓住了 Agent Loop 的基本逻辑：

```text
模型决策
    |
    v
检查 Tool Calls
    |
    +-- 没有 --> 结束
    |
    +-- 有 ----> 执行工具
                    |
                    v
                 保存结果
                    |
                    v
                 再次调用模型
```

但真实生产框架不能只依赖这几十行代码。

为什么？

因为我们还没有解决：

- 多个工具如何并发执行？
- 工具参数错误怎么办？
- 工具执行失败怎么办？
- 如何暂停等待人工批准？
- 如何记录中间状态？
- 进程中断后如何恢复？
- 如何插入 Middleware？
- 如何观察每个执行步骤？
- 如何限制无限循环？

所以，真实 Agent Harness 需要一个更完整的执行系统。

这时我们再进入 DeepAgents 和 LangChain 的真实代码。

---

## 七、DeepAgents 自己实现了 Agent Loop 吗？

现在回到第 01 章。

打开：

`deepagents/graph.py`

找到：

```python
create_deep_agent(...)
```

DeepAgents 会装配：

```python
FilesystemMiddleware(...)
```

以及：

```python
create_summarization_middleware(...)
```

还有 Subagent、Skills、Memory 等能力。

但真正构建 Agent Loop 的入口在函数结尾：

```python
return create_agent(
    model,
    system_prompt=final_system_prompt,
    tools=_tools,
    middleware=deepagent_middleware,
    response_format=response_format,
    context_schema=context_schema,
    checkpointer=checkpointer,
    store=store,
    debug=debug,
    name=name,
    cache=cache,
    state_schema=(
        state_schema
        if state_schema is not None
        else DeepAgentState
    ),
).with_config(...)
```

这是对固定版本真实源码的格式整理。

这里说明：

**DeepAgents 不重新发明基础 Agent Loop，而是复用 LangChain 提供的 Agent 构建器。**

因此可以把它们的分工理解为：

```text
                    DeepAgents
                         |
                         v
               create_deep_agent()
                         |
                         v
             装配 Harness Components
                         |
             +-----------+-----------+
             |           |           |
             v           v           v
          Tools       Backend     Middleware
             |           |           |
             +-----------+-----------+
                         |
                         v
                 LangChain create_agent()
                         |
                         v
                  构建 Agent Graph
                         |
                         v
                   LangGraph Runtime
```

为什么这样分层？

因为 Agent Loop 具有很强的通用性。

无论我们开发：

```text
Coding Agent
Research Agent
Customer Support Agent
Data Analysis Agent
```

都可能需要：

```text
Model -> Tool -> Model
```

真正因应用不同而变化的，更多是：

```text
可用工具
执行环境
权限
上下文策略
任务规划
子 Agent
```

所以让 LangChain 负责通用 Agent Loop，DeepAgents 负责更高级的 Harness 装配，是一种职责分离。

---

## 八、进入 `factory.py`：LangChain 怎样构建循环？

打开：

`langchain/agents/factory.py`

找到：

```python
def create_agent(...):
    ...
```

这里有一个容易误解的地方。

**`create_agent()` 并不是在运行时执行一个 Python `while True`。**

它主要负责构建一张具有循环边的 Graph。

Graph 由 LangGraph 执行。

### 8.1 为什么选择 Graph 而不是普通 while 循环？

因为普通循环比较适合：

```text
顺序执行
同步控制
简单状态
```

但 Agent 需要支持：

```text
模型节点
工具节点
多个 Middleware 节点
条件路由
并行工具
中断与恢复
状态持久化
```

这些行为天然适合用状态图表达。

所以，最基本的 Agent Graph 是：

```text
                      START
                        |
                        v
                      Model
                        |
                  Has Tool Call?
                        |
              +---------+---------+
              |                   |
              v                   v
             No                  Yes
              |                   |
              v                   v
             END                Tools
                                  |
                                  v
                                Model
```

这张图不是两个模型节点。

下面的 Model 是**同一个节点被再次调度执行**。

这就是 Graph 形式的循环。

### 8.2 真实源码：创建 StateGraph

`factory.py` 中：

```python
graph = StateGraph(
    state_schema=resolved_state_schema,
    input_schema=input_schema,
    output_schema=output_schema,
    context_schema=context_schema,
)
```

这时还没有执行模型。

LangChain 只是在定义：

> 这张 Graph 将使用什么状态结构？

例如其中包含：

```python
messages
```

之后再注册节点。

```python
graph.add_node(
    "model",
    RunnableCallable(
        model_node,
        amodel_node,
        trace=False,
    ),
)
```

如果有 ToolNode：

```python
graph.add_node(
    "tools",
    tool_node,
)
```

这时我们才得到了基本的两个执行节点。

### 设计思想

Graph Node 负责：

**某一步做什么。**

Graph Edge 负责：

**做完之后去哪里。**

因此：

```text
Model Node
```

不需要自己无限循环。

它只需要完成一次模型调用，并返回结果。

是否再次执行 Model，由 Graph 的路由机制决定。

---

## 九、Model Node 实际负责什么？

现在进入最核心的运行时代码。

`factory.py` 中：

```python
def model_node(
    state: AgentState,
    runtime: Runtime,
):
    ...
```

第一步：

```python
messages = _patch_invalid_tool_calls(
    state["messages"]
)
```

它会先处理某些不完整的工具调用历史。

接下来构造：

```python
request = ModelRequest(
    model=model,
    tools=default_tools,
    system_message=system_message,
    response_format=initial_response_format,
    messages=messages,
    tool_choice=None,
    state=state,
    runtime=runtime,
)
```

为什么需要 `ModelRequest`？

因为此时需要交给模型的不仅是消息：

```text
messages
```

还包括：

```text
model
tools
system_message
state
runtime
```

而且我们已经知道，Middleware 可能需要修改其中某些内容。

比如第 06 章研究过的 SummarizationMiddleware，会修改本轮模型可见消息。

因此，LangChain 将这些信息组织成统一的模型请求对象。

### 9.1 模型真正在哪里执行？

继续找到：

```python
def _execute_model_sync(request):
    ...
```

关键代码：

```python
model_, effective_response_format = (
    _get_bound_model(request)
)

messages = request.messages

if request.system_message:
    messages = [
        request.system_message,
        *messages,
    ]

output = model_.invoke(messages)
```

现在终于看到真正的：

```python
model_.invoke(...)
```

了。

但与我们最初的简单模型调用不同：

它已经完成：

1. 读取当前 State。
2. 构造 ModelRequest。
3. 准备工具绑定。
4. 允许 Middleware 修改请求。
5. 调用真实模型。

### 9.2 为什么需要 `bind_tools()`？

如果模型需要发起 Tool Call，它首先应该知道：

```text
有哪些工具？
每个工具有什么参数？
每个工具的用途是什么？
```

因此 `_get_bound_model()` 会按需要调用：

```python
request.model.bind_tools(
    final_tools,
    tool_choice=request.tool_choice,
    **request.model_settings,
)
```

这里要特别区分两个动作：

**向模型声明工具：**

```python
model.bind_tools(...)
```

**真正执行工具：**

```python
tool.invoke(...)
```

前者属于模型调用准备阶段。

后者属于工具执行阶段。

这解释了为什么不能认为：

> 模型通过 bind_tools 获得了工具，就等于工具已经执行。

`bind_tools()` 通常只是将工具描述和调用约束绑定到模型请求中。

真正的工具执行仍然由 ToolNode 完成。

---

## 十、模型返回之后，怎样进入 Graph State？

假设模型返回：

```python
AIMessage(
    content="",
    tool_calls=[
        {
            "name": "read_file",
            "args": {
                "file_path": "/refund.py"
            },
            "id": "call-read-1",
        }
    ],
)
```

`_execute_model_sync()` 会将模型输出整理成：

```python
ModelResponse(
    result=messages_list,
    structured_response=structured_response,
)
```

然后，`model_node()` 会通过：

```python
_build_commands(...)
```

将结果转换成 Graph 能接受的状态更新。

在普通路径中，其核心形式是：

```python
Command(
    update={
        "messages": model_response.result
    }
)
```

为什么要使用 `Command`？

因为 Model Node 只负责产出更新。

它不应该绕过 Graph Runtime，直接随意修改全局 State。

我们在第 05 章已经研究过：

```text
Node Update
    |
    v
Channel / Reducer
    |
    v
Updated Graph State
```

因此，Model Node 的核心职责到此结束。

它已经完成一次模型调用，并提交了新消息。

但它还没有执行 Tool Call。

接下来，Graph 需要决定路由。

---

## 十一、第三个设计问题：谁决定下一步进入 Tools？

现在 State 中已经有：

```python
AIMessage(
    tool_calls=[...]
)
```

系统必须决定：

```text
有 Tool Call -> 执行工具

没有 Tool Call -> 结束
```

这是一项控制流判断。

我们不应该让模型再次生成一句：

```text
下一步请执行工具节点。
```

而应该由 Runtime 根据结构化消息决定。

这就是条件路由存在的原因。

### 11.1 真实源码：`_make_model_to_tools_edge()`

打开 `factory.py`：

```python
def _make_model_to_tools_edge(
    *,
    model_destination,
    structured_output_tools,
    end_destination,
):
    ...
```

它创建一个路由函数。

核心逻辑是：

```python
last_ai_message, tool_messages = (
    _fetch_last_ai_and_tool_messages(
        state["messages"]
    )
)
```

先找到最近一次 AIMessage，以及它之后已经出现的 ToolMessage。

然后判断：

```python
if len(last_ai_message.tool_calls) == 0:
    return end_destination
```

**如果模型没有请求执行工具，就进入结束路径。**

这就是普通 Agent Loop 最重要的自然终止条件。

### 11.2 如果有 Tool Call 呢？

源码并不是简单地直接返回：

```python
"tools"
```

它先计算仍然需要执行的 Tool Call：

```python
pending_tool_calls = [
    c
    for c in last_ai_message.tool_calls
    if c["id"] not in tool_message_ids
    and c["name"] not in structured_output_tools
]
```

为什么需要这一步？

因为一个模型响应可能包含多个 Tool Call。

某些工具也可能已经由其他机制产生了对应的 ToolMessage。

因此，不能盲目重复执行所有调用。

只应该处理尚未完成、且需要交给 ToolNode 的请求。

### 11.3 使用 `Send` 派发工具任务

核心代码：

```python
if pending_tool_calls:
    return [
        Send("tools", [tool_call])
        for tool_call in pending_tool_calls
    ]
```

这里的 `Send` 是 LangGraph 提供的任务派发机制。

例如模型一次返回：

```text
read_file("refund.py")
read_file("test_refund.py")
```

就可能生成两个面向 Tools Node 的执行任务。

```text
                  Model Node
                      |
                      v
              AIMessage.tool_calls
                      |
             +--------+--------+
             |                 |
             v                 v
         Tool Call A       Tool Call B
             |                 |
             v                 v
         Send(tools)       Send(tools)
             |                 |
             +--------+--------+
                      |
                      v
                Tool Results
```

这里的两个工具任务具备并行调度的可能性。

但这不代表所有工具一定同时执行，也不代表并发修改同一份文件自动安全。

真正的并发调度由 LangGraph Runtime 和执行配置负责。

### 为什么要这样设计？

因为：

```text
模型决定要执行哪些工具
```

与：

```text
运行时决定怎样调度这些工具
```

是两件不同的事情。

模型只需要给出结构化行动请求。

运行时可以根据工具性质、系统资源与调度机制完成执行。

---

## 十二、ToolNode 真正怎样执行工具？

现在终于进入：

`langgraph/prebuilt/tool_node.py`

上一章我们研究过 ToolNode 的工具注册机制。

这一章只关注它在 Agent Loop 中承担什么责任。

`ToolNode` 的输入是一个待执行的 Tool Call。

例如：

```python
{
    "name": "read_file",
    "args": {
        "file_path": "/refund.py"
    },
    "id": "call-read-1",
}
```

它需要：

1. 找到真实工具。
2. 准备工具参数。
3. 执行工具。
4. 将结果转成 ToolMessage。
5. 返回给 Graph Runtime。

### 12.1 找到工具实例

在 `_run_one()` 中：

```python
tool = self.tools_by_name.get(
    call["name"]
)
```

前面 `create_agent()` 已经构建了：

```python
ToolNode(
    tools=available_tools,
    wrap_tool_call=wrap_tool_call_wrapper,
    awrap_tool_call=awrap_tool_call_wrapper,
)
```

所以 ToolNode 知道：

```text
read_file -> 对应工具对象
execute   -> 对应工具对象
edit_file -> 对应工具对象
```

当然，DeepAgents 的一些工具来自 FilesystemMiddleware，并非全部都是直接通过 `create_agent(tools=...)` 显式传入。

### 12.2 为什么还需要 ToolCallRequest？

ToolNode 会构造：

```python
tool_request = ToolCallRequest(
    tool_call=call,
    tool=tool,
    state=tool_runtime.state,
    runtime=tool_runtime,
)
```

因为真正执行工具时，可能还需要：

```text
当前 Graph State
运行时配置
工具调用 ID
Backend / Store
上下文信息
```

这些不应该全部由模型手动填写。

例如，模型决定：

```python
read_file("/refund.py")
```

但它不应该负责传入内部 Runtime 对象。

所以 ToolNode 将：

**模型可控制的参数**与**系统注入的执行信息**分开。

### 12.3 实际调用发生在哪里？

继续进入：

```python
_execute_tool_sync(...)
```

其中关键代码是：

```python
injected_call = self._inject_tool_args(
    call,
    request.runtime,
    tool,
)
```

然后：

```python
response = tool.invoke(
    call_args,
    config,
)
```

这才是真正的工具调用。

因此调用链是：

```text
                Model AIMessage
                       |
                       v
                    Tool Call
                       |
                       v
                LangChain Routing
                       |
                       v
                     ToolNode
                       |
                       v
               tools_by_name 查询
                       |
                       v
                ToolCallRequest
                       |
                       v
                  Tool Wrapper
                       |
                       v
                _execute_tool_sync
                       |
                       v
                   tool.invoke
                       |
                       v
                    Tool Result
```

对于 DeepAgents 的文件工具，随后还会进入 FilesystemMiddleware、Backend 等逻辑。

这些具体细节属于第 03、04 章。

在本章，我们只需把握：

**ToolNode 是 Agent Loop 中将模型行动意图变成实际工具执行的节点。**

---

## 十三、工具执行完之后，为什么还要重新调用模型？

假设工具返回：

```python
ToolMessage(
    content=(
        "refund.py:\n"
        "def calculate_refund(amount):\n"
        "    return round(amount)"
    ),
    tool_call_id="call-read-1",
    name="read_file",
)
```

现在系统已经取得源码。

能不能直接返回给用户？

通常不能。

因为：

> 读取文件不是用户的最终目标，修复 Bug 才是。

所以，需要将工具结果加入 State，让模型基于新信息继续决策。

```text
               ToolNode
                   |
                   v
               ToolMessage
                   |
                   v
                Graph State
                   |
                   v
                Model Node
                   |
                   v
          基于工具结果重新决策
```

这就引出了另一条条件边。

### 13.1 真实源码：`_make_tools_to_model_edge()`

`factory.py` 中：

```python
def _make_tools_to_model_edge(
    *,
    tool_node,
    model_destination,
    structured_output_tools,
    end_destination,
):
    ...
```

最常见的分支是：

```python
return model_destination
```

也就是：

```text
ToolNode
    |
    v
Model Node
```

但并不是所有 Tool Call 都必然返回模型。

例如某个工具配置：

```python
return_direct=True
```

可能要求：

> 工具执行后直接结束当前 Agent Loop。

因此源码也需要检查此类特殊结束条件。

此外，结构化输出工具的成功解析，也可能导致结束。

### 为什么需要这些例外？

因为 Agent Loop 应该提供一个默认控制流程：

```text
工具执行完成 -> 模型继续决策
```

同时允许少数工具表达：

```text
工具结果本身就是最终结果
```

这体现了默认约定与特殊控制语义的结合。

---

## 十四、到这里，我们终于能够画出真正的 Agent Loop

现在把已经理解的源码连接起来：

```text
                    agent.invoke()
                          |
                          v
                    LangGraph START
                          |
                          v
                       Model Node
                          |
                          v
                  构造 ModelRequest
                          |
                          v
                  Middleware Wrappers
                          |
                          v
                    model.invoke()
                          |
                          v
                      AIMessage
                          |
                          v
                   Command(update)
                          |
                          v
                     Graph State
                          |
                          v
               _make_model_to_tools_edge
                          |
                +---------+---------+
                |                   |
                v                   v
          No Tool Calls        Pending Tool Calls
                |                   |
                v                   v
               END              Send("tools")
                                    |
                                    v
                                 ToolNode
                                    |
                                    v
                                tool.invoke()
                                    |
                                    v
                                ToolMessage
                                    |
                                    v
                                Graph State
                                    |
                                    v
                       _make_tools_to_model_edge
                                    |
                                    v
                                Model Node
```

这才是我们真正要理解的主线。

这里有一个重要区别：

**图中的 `Model Node` 不是直接递归调用自己。**

它执行一次、提交状态更新，然后由 LangGraph Runtime 根据边决定是否再次执行。

这意味着模型调用、工具执行和循环调度，被分成了不同责任。

---

## 十五、为什么 Middleware 必须放在循环的特定位置？

前面我们一直在讨论 Middleware 是谁支持的。

现在可以从 Agent Loop 的角度解释它为什么这样设计。

假设我们希望：

```text
每次模型调用前检查上下文长度
```

那它必须在每一轮 Model Node 前执行。

假设我们希望：

```text
Agent 开始时初始化一次运行环境
```

那它不应该在每一轮循环里重复执行。

所以 LangChain 区分：

```text
before_agent
before_model
wrap_model_call
after_model
after_agent
wrap_tool_call
```

这些 Hook 的差别，本质上来自**生命周期位置不同**。

### 15.1 构建阶段确定循环入口

`factory.py` 中有：

```python
if middleware_w_before_model:
    loop_entry_node = (
        f"{middleware_w_before_model[0].name}.before_model"
    )
else:
    loop_entry_node = "model"
```

这段源码非常关键。

它意味着：

如果定义了 `before_model` Middleware，那么工具执行结束后，不能直接返回 Model Node。

而应该返回：

```text
before_model
    |
    v
model
```

否则：

```python
before_model()
```

可能只运行一次，而不是每轮模型调用前都运行。

### 15.2 Agent 首次入口和循环入口不同

`factory.py` 还区分：

```python
entry_node
```

以及：

```python
loop_entry_node
```

为什么？

因为：

```python
before_agent()
```

应该在 Agent 执行开始时调用一次。

但：

```python
before_model()
```

应该在每次模型调用前执行。

所以：

```text
                      START
                        |
                        v
                    before_agent
                        |
                        v
                    before_model <-----------+
                        |                    |
                        v                    |
                       model                 |
                        |                    |
                        v                    |
                      tools -----------------+
```

如果把 `before_agent` 放进循环，就可能反复执行初始化操作。

因此，LangChain 在编译 Graph 时必须区分这些生命周期。

**Middleware 的生命周期差异，最终体现为 Graph 中不同的节点位置和路由边。**

这也解释了为什么 `factory.py` 需要手动解析 Middleware。

---

## 十六、一次完整的退款任务，Agent Loop 会怎样运行？

假设用户要求：

```text
修复 refund.py 并运行测试。
```

下面是一个简化但符合 Agent Loop 协议的执行过程。

### 第一次模型调用

模型返回：

```python
AIMessage(
    content="",
    tool_calls=[
        {
            "name": "read_refund",
            "args": {},
            "id": "call-1",
        }
    ],
)
```

路由器发现：

```text
有待执行 Tool Call
```

于是进入 ToolNode。

### 第一次工具执行

工具读取文件：

```python
ToolMessage(
    content="当前使用 round(amount) 计算退款金额",
    tool_call_id="call-1",
    name="read_refund",
)
```

该结果写入 State。

然后返回 Model Node。

### 第二次模型调用

模型基于源码决定修改：

```python
AIMessage(
    content="",
    tool_calls=[
        {
            "name": "patch_refund",
            "args": {},
            "id": "call-2",
        }
    ],
)
```

再次进入 ToolNode。

### 第二次工具执行

工具返回：

```python
ToolMessage(
    content="refund.py 已修改",
    tool_call_id="call-2",
    name="patch_refund",
)
```

然后继续调用模型。

### 第三次模型调用

模型决定运行测试：

```python
AIMessage(
    content="",
    tool_calls=[
        {
            "name": "run_refund_tests",
            "args": {},
            "id": "call-3",
        }
    ],
)
```

执行测试工具。

### 第三次工具执行

工具返回：

```python
ToolMessage(
    content="测试通过",
    tool_call_id="call-3",
    name="run_refund_tests",
)
```

再次调用模型。

### 第四次模型调用

模型返回：

```python
AIMessage(
    content="已完成修复并检查测试结果。",
    tool_calls=[],
)
```

此时：

```python
len(last_ai_message.tool_calls) == 0
```

Graph 进入结束路径。

我们可以把这个过程概括为：

```text
User Task
    |
    v
Model #1
    |
    v
read_refund
    |
    v
Tool Result #1
    |
    v
Model #2
    |
    v
patch_refund
    |
    v
Tool Result #2
    |
    v
Model #3
    |
    v
run_refund_tests
    |
    v
Tool Result #3
    |
    v
Model #4
    |
    v
Final Answer
    |
    v
END
```

注意这里的 `Model #1` 到 `Model #4` 是同一个模型节点的不同执行轮次，不是创建了四个 Agent。

---

## 十七、实验：使用真实 LangChain Agent Loop 验证执行顺序

接下来我们不再自己写 `while True`。

而是使用真实的：

```python
create_agent()
```

创建 Agent。

为了避免依赖模型 API Key，我们使用一个可控的 Fake Chat Model。

它不负责真正理解代码，而是按照预设规则依次产生 Tool Call。

实验的目的不是测试模型智能，而是验证：

**LangChain 是否真的按照 Model → Tools → Model 的方式调度执行。**

### 17.1 安装依赖

```bash
pip install "langchain>=1.4.7,<2" langchain-core
```

这里选择与 DeepAgents 0.7.32 的依赖范围相兼容的 LangChain 版本。

### 17.2 完整代码

创建：

`test_agent_loop.py`

```python
from typing import Any

from langchain.agents import create_agent

from langchain_core.language_models.fake_chat_models import (
    FakeMessagesListChatModel,
)

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    ToolMessage,
)

from langchain_core.outputs import (
    ChatGeneration,
    ChatResult,
)

from langchain_core.tools import tool


# =========================================
# 1. 模拟项目文件
# =========================================

project = {
    "/refund.py": (
        "def refund(amount):\n"
        "    return round(amount)\n"
    ),
}


# =========================================
# 2. 定义三个真实可执行的工具函数
# =========================================

@tool
def read_refund() -> str:
    """Read the current refund.py source code."""

    print("[TOOL] Reading refund.py")

    return project["/refund.py"]


@tool
def patch_refund() -> str:
    """Apply a demonstration precision patch."""

    print("[TOOL] Patching refund.py")

    project["/refund.py"] = (
        "from decimal import Decimal\n"
        "\n"
        "def refund(amount):\n"
        "    return Decimal(str(amount)).quantize("
        "Decimal('0.01'))\n"
    )

    return "Demonstration patch applied."


@tool
def run_refund_tests() -> str:
    """Run a simulated refund test against the demo file."""

    print("[TOOL] Running simulated refund tests")

    source = project["/refund.py"]

    if "quantize" not in source:
        return "FAILED: precision handling not found"

    return "PASSED: simulated precision check"


# =========================================
# 3. 定义可控的 Fake Chat Model
# =========================================

class RefundScriptModel(FakeMessagesListChatModel):

    def bind_tools(
        self,
        tools: Any,
        **kwargs: Any,
    ):
        # 测试模型已经预先知道工具名称。
        # 因此只需要允许 create_agent 调用 bind_tools。
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop=None,
        run_manager=None,
        **kwargs: Any,
    ) -> ChatResult:

        tool_messages = [
            message
            for message in messages
            if isinstance(message, ToolMessage)
        ]

        completed = len(tool_messages)

        steps = [
            "read_refund",
            "patch_refund",
            "run_refund_tests",
        ]

        if completed < len(steps):

            tool_name = steps[completed]

            print(
                f"[MODEL] Requesting {tool_name}"
            )

            response = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": tool_name,
                        "args": {},
                        "id": f"call-{completed + 1}",
                    }
                ],
            )

        else:

            print("[MODEL] Returning final answer")

            response = AIMessage(
                content=(
                    "演示流程已完成：读取源码、"
                    "应用示例修改并检查模拟测试。"
                )
            )

        return ChatResult(
            generations=[
                ChatGeneration(
                    message=response
                )
            ]
        )


# =========================================
# 4. 使用真正的 create_agent 构建循环
# =========================================

model = RefundScriptModel(
    responses=[],
)

agent = create_agent(
    model=model,
    tools=[
        read_refund,
        patch_refund,
        run_refund_tests,
    ],
)


# =========================================
# 5. 执行任务
# =========================================

result = agent.invoke(
    {
        "messages": [
            {
                "role": "user",
                "content": (
                    "修复优惠券退款精度问题，"
                    "并验证结果。"
                ),
            }
        ]
    }
)


# =========================================
# 6. 检查完整执行历史
# =========================================

print("\n=== Message History ===")

for message in result["messages"]:

    print(
        type(message).__name__,
        str(message.content)[:100],
    )


# =========================================
# 7. 断言 Agent Loop 实际执行了三次工具
# =========================================

tool_results = [
    message
    for message in result["messages"]
    if isinstance(message, ToolMessage)
]

assert len(tool_results) == 3

assert [message.name for message in tool_results] == [
    "read_refund",
    "patch_refund",
    "run_refund_tests",
]

assert "quantize" in project["/refund.py"]

assert isinstance(
    result["messages"][-1],
    AIMessage,
)

assert not result["messages"][-1].tool_calls

print("\nAgent Loop verified.")
```

### 17.3 预期观察到什么？

终端输出的关键执行顺序应该类似：

```text
[MODEL] Requesting read_refund

[TOOL] Reading refund.py

[MODEL] Requesting patch_refund

[TOOL] Patching refund.py

[MODEL] Requesting run_refund_tests

[TOOL] Running simulated refund tests

[MODEL] Returning final answer

Agent Loop verified.
```

注意：

这里并没有由我们编写：

```python
while True:
    ...
```

也没有手动执行：

```python
read_refund.invoke(...)
```

这些调度由真实的 `create_agent()` 生成的 LangGraph 完成。

### 17.4 这个实验验证了什么？

**第一：模型可以通过 AIMessage.tool_calls 发起操作。**

**第二：ToolNode 能够找到并执行对应工具。**

**第三：工具结果会通过 ToolMessage 反馈给下一轮模型。**

**第四：只要模型继续产生 Tool Call，Graph 就能继续循环。**

**第五：当模型不再请求工具时，Graph 可以进入结束路径。**

### 17.5 实验限制

必须明确：

这里的 Fake Model 不是在真正分析代码。

它按照工具消息数量执行预设的三个动作。

而且：

```python
run_refund_tests()
```

只检查了模拟文件中的字符串，不是真的运行 `pytest`。

示例补丁也不代表完整、正确的优惠券部分退款业务实现。

这个实验只用于验证 **Agent Loop 的运行机制**。

如果需要验证实际 Bug 修复，还必须使用真实项目文件、真实执行环境和真实测试用例。

代码按所查阅接口编写，当前环境未安装 LangChain，因此尚未执行验证。

---

## 十八、如果换成 DeepAgents，哪些部分会改变？

现在我们已经验证了 LangChain 的基础 Agent Loop。

接下来回到 DeepAgents。

假设使用：

```python
from deepagents import create_deep_agent

agent = create_deep_agent(
    model=model,
)
```

它不只是创建基础的 Model 和 Tools 节点。

它还会装配：

```text
FilesystemMiddleware
SummarizationMiddleware
PatchToolCallsMiddleware
SubAgentMiddleware（按配置）
SkillsMiddleware（按配置）
MemoryMiddleware（按配置）
HumanInTheLoopMiddleware（按配置）
```

因此，Graph 的实际行为会更丰富。

例如：

```text
                      START
                        |
                        v
                  before_agent Hooks
                        |
                        v
                  before_model Hooks
                        |
                        v
                     Model Node
                        |
                        v
                 wrap_model_call
                        |
                        v
                    Real Model
                        |
                        v
                  after_model Hooks
                        |
                  Has Tool Call?
                        |
               +--------+--------+
               |                 |
               v                 v
              No                Yes
               |                 |
               v                 v
        after_agent Hooks      ToolNode
               |                 |
               v                 v
              END          wrap_tool_call
                                 |
                                 v
                            Backend / Tool
                                 |
                                 v
                            ToolMessage
                                 |
                                 v
                         before_model Hooks
                                 |
                                 v
                              Model Node
```

这张图是说明职责的简化图，具体 Middleware 组合、节点数量和执行顺序由实际配置决定。

特别注意：

- `before_model` 等节点型 Hook 由 `create_agent()` 编译成 Graph 节点。
- `wrap_model_call` 主要作为 Model Node 内部的包装链执行。
- `wrap_tool_call` 由 LangChain 组合后，交给 LangGraph ToolNode 调用。

所以 DeepAgents 与 LangChain 的关系不是两层独立的 Agent Loop。

而是：

**DeepAgents 为同一个 Agent Loop 提供更多执行能力与控制策略。**

---

## 十九、Agent Loop 如何避免无限执行？

到这里还需要面对一个实际问题。

假设模型不断输出：

```text
read_file
read_file
read_file
read_file
...
```

会发生什么？

如果我们的 Agent 只是：

```python
while True:
    ...
```

而没有限制，就可能无限消耗资源。

真实 Agent Harness 必须考虑终止与约束。

### 19.1 正常结束：模型不再请求工具

这是最基本的结束条件：

```python
if not last_ai_message.tool_calls:
    return END
```

不过真实 `factory.py` 还会考虑：

```text
Structured Output
return_direct
Middleware Jump
已经完成的 Tool Call
```

因此它的完整终止逻辑不只有这一行。

### 19.2 Runtime 执行限制

LangGraph 支持：

```python
recursion_limit
```

来限制 Graph 执行步骤数。

例如：

```python
agent.invoke(
    input,
    config={
        "recursion_limit": 50
    },
)
```

超过限制时，Graph 不会一直无限运行。

固定 DeepAgents 版本还在构建结果上设置了较高的默认递归限制：

```python
{
    "recursion_limit": 9_999
}
```

但这**不应该被理解为生产环境的成本保护策略**。

因为 9,999 个 Graph 执行步骤可能非常昂贵。

实际业务仍应考虑：

```text
最大模型调用次数
Token 预算
工具执行超时
工具重试次数
任务时间限制
危险操作权限
人工审批
```

### 19.3 为什么“模型说完成了”不等于“任务真的完成了”？

这是 Agent Loop 设计中一个重要的边界。

模型返回：

```python
AIMessage(
    content="代码已经修复，测试全部通过。",
    tool_calls=[],
)
```

从控制流来看，Agent Loop 可以结束。

但这并不保证：

```text
文件真的修改成功
测试真的运行过
测试真的通过
```

因此：

**Agent Loop 的自然结束条件与业务任务的成功条件不是同一个概念。**

对于 Coding Agent，成功条件应该尽量由真实工具结果验证：

```text
实际文件变更
真实 pytest 返回码
相关测试断言
必要的代码审查
```

这也是为什么 DeepAgents 的 Harness 不只包含 Agent Loop，还需要工具、Backend、Planning、权限和可观测性。

---

## 二十、用设计取舍的角度理解 Agent Loop

现在我们可以评价这套架构。

### 20.1 为什么让模型决定下一步？

因为复杂任务的后续行动依赖运行时获得的信息。

收益：

```text
能够处理预先未知的情况
能够根据测试失败继续调查
不需要为每种错误手写流程
```

代价：

```text
执行路径不完全可预测
可能产生多余工具调用
可能重复操作
需要预算和权限控制
```

### 20.2 为什么让 Runtime 而不是模型执行工具？

因为实际工具执行涉及：

```text
权限
参数校验
安全
状态更新
错误处理
```

这些能力不能只依赖模型自行约束。

因此：

```text
模型负责提出行动
Runtime 负责执行行动
```

### 20.3 为什么使用结构化消息？

因为下一步路由必须依据明确的数据。

```python
AIMessage.tool_calls
```

和：

```python
ToolMessage.tool_call_id
```

提供了可机器处理的关联关系。

代价是 Agent 必须处理工具调用格式、ID 和消息配对的一致性。

### 20.4 为什么用 Graph 而不是 while 循环？

因为 Graph 更容易表达：

```text
多个节点
条件路由
并发执行
Middleware 生命周期
State 更新
Checkpoint
Interrupt
```

代价是：

```text
编译逻辑更复杂
Runtime 抽象更多
调试时需要理解节点和状态通道
```

### 20.5 为什么 DeepAgents 不自己实现 Agent Loop？

因为 Agent Loop 属于通用编排能力。

DeepAgents 更值得投入的，是其上层的：

```text
Backend
Context Management
Planning
Subagents
Skills
Memory
HITL
```

这样可以复用 LangChain、LangGraph 的通用机制。

代价则是必须理解不同代码库之间的接口边界与版本兼容性。

---

## 二十一、最后重新理解 Agent Loop 的本质

经过前面的推导，我们已经知道：

Agent Loop 不是：

```python
while True:
    model.invoke(...)
```

那么简单。

它真正提供了三种机制。

### 第一：动态行动选择

模型根据当前消息决定：

```text
继续使用工具
或者
返回最终回答
```

### 第二：行动执行与结果反馈

Runtime 根据结构化 Tool Call 执行工具。

然后把结果通过 ToolMessage 反馈给模型。

### 第三：状态化控制循环

LangGraph 负责：

```text
调度节点
更新 State
判断下一条边
处理终止与中断
```

因此完整结构是：

```text
                     User Task
                         |
                         v
                      State
                         |
                         v
                    Model Node
                         |
                         v
                Decide Next Action
                         |
              +----------+----------+
              |                     |
              v                     v
           Finish               Tool Call
              |                     |
              v                     v
             END                 ToolNode
                                    |
                                    v
                              Tool Execution
                                    |
                                    v
                               ToolMessage
                                    |
                                    v
                               State Update
                                    |
                                    v
                                Model Node
```

可以将它理解成一个**由模型提供决策、由执行器产生反馈、由状态图负责调度的动态控制循环**。

这是一种有助于理解的架构类比，不意味着 LLM Agent 自动具备传统控制理论中的稳定性或收敛保证。

---

## 二十二、与下一章的连接：Tool Call 究竟怎样成为真实执行？

我们现在解决了：

> Agent 为什么要循环，以及 LangChain 怎样构建这个循环？

但是有一个关键部分还没有展开。

假设模型返回：

```python
{
    "name": "read_file",
    "args": {
        "file_path": "/refund.py"
    },
    "id": "call-123",
}
```

我们已经知道：

```text
Model
    |
    v
Conditional Edge
    |
    v
ToolNode
```

但还不知道：

- `read_file` 最初在哪里注册？
- LangChain 怎么从 Middleware 提取工具？
- ToolNode 如何根据名称找到工具？
- 工具参数如何通过 Schema 校验？
- `ToolRuntime` 怎样注入 State 和 Backend？
- 为什么 `wrap_tool_call` 可以拦截工具？
- `ToolMessage` 和 `Command` 有什么区别？

这正是下一章：

**第 03 章：Tool Calling——从 AIMessage.tool_calls 到真实工具执行。**

下一章从：

```python
ToolNode(
    tools=available_tools,
    wrap_tool_call=wrap_tool_call_wrapper,
    awrap_tool_call=awrap_tool_call_wrapper,
)
```

继续向下追踪。

---

## 二十三、本章真实源码导航

### DeepAgents 0.7.32

固定 Commit：`e85c6b9`

**入口：**

`libs/deepagents/deepagents/graph.py`

重点：

```python
create_deep_agent()
```

以及最终调用：

```python
create_agent(...)
```

源码：

https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py

### LangChain

**Agent 构建器：**

`libs/langchain_v1/langchain/agents/factory.py`

按照以下顺序阅读：

```text
create_agent()
    |
    v
收集 tools 和 middleware
    |
    v
创建 ToolNode
    |
    v
创建 StateGraph
    |
    v
定义 model_node()
    |
    v
定义 _execute_model_sync()
    |
    v
注册 model / tools 节点
    |
    v
_make_model_to_tools_edge()
    |
    v
_make_tools_to_model_edge()
    |
    v
graph.compile()
```

源码：

https://github.com/langchain-ai/langchain/blob/master/libs/langchain_v1/langchain/agents/factory.py

### LangGraph

**工具执行器：**

`libs/prebuilt/langgraph/prebuilt/tool_node.py`

重点：

```text
ToolNode
   |
   v
_run_one()
   |
   v
_execute_tool_sync()
   |
   v
tool.invoke()
```

源码：

https://github.com/langchain-ai/langgraph/blob/main/libs/prebuilt/langgraph/prebuilt/tool_node.py

注意：DeepAgents 固定到指定 Commit；这里的 LangChain 和 LangGraph 链接指向主分支。本章依据已核对的这些实现路径分析，但主分支可能与某个具体安装环境存在差异。做逐行调试时，应固定实际安装版本。

---

## 本章总结

我们从一个现实问题开始：

> 修复代码不是一次性回答问题，而是必须根据文件和测试结果不断调整行动。

这使我们从固定调用链逐步推导出：

1. 模型需要用结构化 Tool Call 表达行动。
2. Runtime 需要实际执行工具。
3. ToolMessage 需要把结果反馈给模型。
4. State 需要保存跨轮次的信息。
5. 条件路由需要决定继续执行还是结束。
6. Graph Runtime 需要负责调度，而不只是简单循环。
7. Middleware 需要接入循环的不同生命周期。
8. DeepAgents 在这套通用循环之上装配 Coding Agent 所需要的 Harness 能力。

最终最重要的结论是：

> **Agent Loop 将一次性的模型推理，组织成一个能够根据执行反馈持续采取行动的状态化运行过程。LangChain 负责构建这个过程，LangGraph 负责执行它，而 DeepAgents 为它提供更完整的任务执行能力。**
