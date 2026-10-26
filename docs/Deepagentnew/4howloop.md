---
title: 第四章：LangChain create_agent 如何构建 Agent Loop？
description: 沿 LangChain factory.py 的真实源码，从模型和工具的输入开始，逐步理解工具注册、模型节点创建、条件路由、Middleware 扩展位置以及 Graph 编译过程
tags: [DeepAgents, LangChain, LangGraph, Agent Loop, create_agent, 源码精读]
sidebar_position: 4
---

# 第四章：LangChain `create_agent()` 如何构建 Agent Loop？

## 一、从上一章留下的问题开始

前面三章，我们已经逐步建立了对 Agent 的理解。

首先，我们发现一次模型调用不能完成复杂的代码修复任务。

因此，需要让模型能够使用工具。

接着，我们发现模型执行一次工具后，可能需要根据结果继续决策。

因此，需要 Agent Loop。

然后，我们理解了模型与工具之间的通信协议：

```text
                  Model
                    |
                    v
                 AIMessage
                    |
                    v
               tool_calls
                    |
                    v
                Tool Executor
                    |
                    v
                ToolMessage
                    |
                    v
                  Model
```

第三章进一步研究了为什么可以使用 StateGraph 表达这一循环。

现在，我们已经理解 Agent Loop 的基本组成：

```text
Model Node
ToolNode
Conditional Edge
State
Runtime
```

但这留下了一个新问题：

> 如果每次创建 Agent 都需要开发者手动定义这些节点和边，是否过于繁琐？

假设我们需要开发十个不同的 Agent。

它们可能使用不同模型和工具，但都具有相同的基本循环：

```text
Model → Tools → Model
```

难道我们每次都要重新写：

```python
builder = StateGraph(...)

builder.add_node("model", ...)
builder.add_node("tools", ...)

builder.add_conditional_edges(...)
builder.add_edge(...)

agent = builder.compile()
```

显然可以，但会产生大量重复代码。

因此，我们自然会想到：

**能不能写一个函数，负责把模型和工具自动组装成可执行的 Agent Loop？**

这正是 LangChain：

```python
create_agent(...)
```

存在的直接原因。

本章不再研究为什么需要 StateGraph，而是研究：

**LangChain 如何使用 StateGraph 创建一个通用 Agent Loop。**

---

## 二、先从最简单的接口开始

假设我们希望创建一个 Coding Agent。

我们已经有模型：

```python
model = ...
```

以及工具：

```python
tools = [
    read_refund,
    edit_refund,
    run_refund_tests,
]
```

最理想的接口是：

```python
agent = create_agent(
    model=model,
    tools=tools,
)
```

然后：

```python
result = agent.invoke({
    "messages": [
        {
            "role": "user",
            "content": "修复退款精度 Bug 并运行测试",
        }
    ]
})
```

这里的：

```python
create_agent(...)
```

负责构建 Agent。

而：

```python
agent.invoke(...)
```

负责运行 Agent。

这两个阶段必须区分。

### 构建阶段

```text
提供 Model 和 Tools
         |
         v
    create_agent()
         |
         v
构建 Agent Graph
         |
         v
返回可执行对象
```

### 执行阶段

```text
    agent.invoke()
         |
         v
执行 Model Node
         |
         v
判断是否需要 Tools
         |
         v
执行 ToolNode
         |
         v
返回 Model Node
         |
         v
直到结束
```

**`create_agent()` 本身主要负责建立执行结构，而不是立即完成用户任务。**

这就是我们阅读 `factory.py` 时首先需要建立的区别。

---

## 三、从 DeepAgents 找到 LangChain 的入口

本系列研究的是 DeepAgents。

所以我们先从：

```python
create_deep_agent(...)
```

进入源码。

文件：

`deepagents/graph.py`

在固定版本的函数结尾，可以看到：

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
).with_config(
    {
        "recursion_limit": 9_999,
        # 其他 metadata 省略
    }
)
```

这段代码透露了一个非常重要的架构关系。

DeepAgents 没有在这里实现：

```python
while True:
    ...
```

它负责准备：

```text
Model
Tools
Middleware
State Schema
Backend 相关能力
Checkpoint 配置
```

然后交给：

```python
create_agent(...)
```

去构建真正的 Agent Loop。

因此：

```text
                 DeepAgents
                     |
                     v
              create_deep_agent()
                     |
                     v
               准备 Harness
                     |
                     v
                 create_agent()
                     |
                     v
               构建 Agent Loop
                     |
                     v
               LangGraph Runtime
```

这一层关系要记住。

后面研究各类 DeepAgents Middleware 时，我们会不断回到这里。

但现在先只看一个最简单的：

```python
create_agent(
    model=model,
    tools=tools,
)
```

了解它怎样从零构建一个循环。

---

## 四、打开 `factory.py`：第一步为什么是整理输入？

源码位置：

`langchain/agents/factory.py`

找到：

```python
def create_agent(...):
    ...
```

它接收模型、工具、系统指令以及一些扩展配置。

如果只关注最基本的 Agent，可以暂时简化为：

```python
def create_agent(
    model,
    tools=None,
    *,
    system_prompt=None,
    middleware=(),
    ...
):
    ...
```

为什么一开始就需要整理这些输入？

因为不同调用者提供的内容可能不同。

例如模型既可能是一个实例：

```python
model = ChatOpenAI(...)
```

也可能是一个模型标识字符串：

```python
model = "provider:model-name"
```

所以真实源码首先判断：

```python
if isinstance(model, str):
    model = init_chat_model(model)
```

这样，后续逻辑就能够使用统一的模型对象接口。

### 系统指令也需要标准化

开发者可能传入：

```python
system_prompt = "你是一个 Coding Agent"
```

但 LangChain 的模型调用使用结构化消息。

因此：

```python
if system_prompt is not None:
    if isinstance(system_prompt, SystemMessage):
        system_message = system_prompt
    else:
        system_message = SystemMessage(
            content=system_prompt
        )
```

现在得到：

```python
system_message
```

后面就不必每次判断：

```text
这是字符串？
还是 SystemMessage？
```

### Tools 也需要标准化

如果开发者没有传入工具：

```python
tools=None
```

源码会处理为：

```python
if tools is None:
    tools = []
```

这些步骤虽然简单，却体现 Factory 的第一项职责：

**将不同形式的外部输入，整理成后续构建流程可以统一处理的内部表示。**

---

## 五、接下来需要解决：工具从哪里来？

假设我们创建：

```python
agent = create_agent(
    model=model,
    tools=[
        read_refund,
        run_refund_tests,
    ],
)
```

那么工具显然来自：

```python
tools
```

但 DeepAgents 的情况不一样。

例如：

```python
FilesystemMiddleware(...)
```

会声明：

```python
self.tools
```

其中包含：

```text
read_file
write_file
edit_file
grep
...
```

那么 LangChain 应该怎样得到这些工具？

最简单的选择是：

> 所有工具都必须显式传给 create_agent(tools=...)。

但这样一来，Middleware 即使想提供新工具，也必须要求调用者手动提取。

例如：

```python
filesystem = FilesystemMiddleware(...)

agent = create_agent(
    model=model,
    tools=filesystem.tools,
    middleware=[filesystem],
)
```

这会造成重复配置。

既然 Middleware 自己声明了工具，Agent Factory 就可以在构建时收集。

### 真实源码：收集 Middleware 工具

```python
middleware_tools = [
    t
    for m in middleware
    for t in getattr(m, "tools", [])
]
```

这里不需要先研究 Middleware Hook。

只需要知道：

**Middleware 可以在构建阶段声明自己提供的工具。**

然后：

```python
regular_tools = [
    t
    for t in tools
    if not isinstance(t, dict)
]
```

最后合并：

```python
available_tools = (
    middleware_tools + regular_tools
)
```

这里还区分了 Provider 原生工具字典与需要客户端执行的工具。

因为不是所有向模型声明的工具，都需要由本地 ToolNode 执行。

对于我们的退款任务，普通 Python 文件工具属于客户端执行的工具。

因此会进入：

```python
available_tools
```

### 为什么需要统一的工具集合？

因为下一步，我们要建立一个工具执行节点。

执行器必须知道：

```text
read_refund 对应哪个 Python 函数？
run_refund_tests 对应哪个 Python 函数？
```

所以必须先完成工具注册。

这就自然引出了：

```python
ToolNode
```

---

## 六、Factory 怎样创建 ToolNode？

上一章已经讲过工具调用协议。

模型产生：

```python
AIMessage(
    tool_calls=[
        {
            "name": "read_refund",
            "args": {},
            "id": "call-001",
        }
    ]
)
```

系统需要一个执行器，根据：

```python
"name": "read_refund"
```

找到真正的函数。

LangGraph 已经提供：

```python
ToolNode
```

因此 LangChain 无须自己重复实现工具分发器。

### 真实源码

```python
tool_node = (
    ToolNode(
        tools=available_tools,
        wrap_tool_call=wrap_tool_call_wrapper,
        awrap_tool_call=awrap_tool_call_wrapper,
    )
    if (
        available_tools
        or wrap_tool_call_wrapper
        or awrap_tool_call_wrapper
    )
    else None
)
```

现在只关注：

```python
ToolNode(
    tools=available_tools,
)
```

它将工具注册到 ToolNode 内部。

后续当 Graph 派发：

```text
read_refund
```

ToolNode 才能查找和执行对应工具。

### 为什么不是每个工具一个 Graph Node？

例如：

```text
read_refund Node
edit_refund Node
run_refund_tests Node
```

这样当然也能构建 Graph。

但如果工具有几十个，就需要不断增加节点和路由边。

而通用 Agent Loop 只关心：

**模型是否希望执行工具。**

具体执行哪个工具，可以交给统一的 ToolNode 处理。

因此：

```text
                 Model
                   |
                   v
                 Tools
                   |
          +--------+--------+
          |        |        |
          v        v        v
         read     edit     test
```

其中 `Tools` 是统一执行节点。

里面可以注册多个实际工具。

这让 Agent Graph 的基础结构保持稳定。

### 一个特殊情况

如果没有客户端工具，也没有能处理动态工具的相关包装器：

```python
tool_node = None
```

那么 Agent 可能只需要一个 Model Node。

这意味着：

**`create_agent()` 不会无条件创建一个没有实际用途的 ToolNode。**

---

## 七、为什么模型也需要一个独立节点？

现在已经有 ToolNode。

还需要：

```text
Model Node
```

回顾我们最初的 `while`：

```python
while True:

    response = model.invoke(messages)

    ...
```

如果放进 Graph，就需要把：

```python
model.invoke(...)
```

包装成一个可调度节点。

它只做一轮模型调用。

Graph Runtime 决定什么时候再次调用它。

### 真实源码：创建 Model Node

在 `factory.py` 中：

```python
def model_node(state, runtime):
    ...
```

以及异步版本：

```python
async def amodel_node(state, runtime):
    ...
```

随后注册：

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

这里我们可以看到第二项关键职责：

**将模型调用变成 Graph 中一个可重复调度的执行单元。**

注意：

`model_node()` 本身不是一个无限循环。

它只执行当前这一轮的模型调用。

调用结束后，Graph 通过路由决定下一步。

### 为什么需要同步和异步版本？

因为使用场景可能不同。

例如：

```python
agent.invoke(...)
```

是同步调用。

```python
await agent.ainvoke(...)
```

是异步调用。

Factory 通过 `RunnableCallable` 将两种实现注册到同一个逻辑节点上。

这样我们不需要维护两套完全不同的 Agent Graph。

---

## 八、进入 Model Node：它真正做了什么？

我们已经知道 Model Node 的职责是调用模型。

但直接写：

```python
model.invoke(state["messages"])
```

是否就够了？

对于一个完全没有扩展的 Agent，可能接近够用。

但 LangChain 要为后续能力留出空间。

例如：

```text
不同模型
不同系统指令
不同工具集合
结构化输出
模型请求修改
```

因此，在调用模型之前，Factory 需要组织一次完整的模型请求。

### 真实源码：构建 ModelRequest

```python
def model_node(state, runtime):

    messages = _patch_invalid_tool_calls(
        state["messages"]
    )

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

    ...
```

这里的 `ModelRequest` 不是新的一轮 Agent Loop。

它只是把一次模型调用需要的信息集中起来。

为什么要这么做？

因为当前输入已经不只是：

```python
messages
```

还包含：

```text
模型对象
可用工具
系统指令
Runtime
当前 State
输出格式
```

如果未来某个扩展需要修改本轮模型请求，就可以基于统一对象处理。

不过，这属于后面 Middleware 章节的重点。

本章只需要理解：

**Model Node 会为每轮调用构造 ModelRequest，然后再真正调用模型。**

---

## 九、模型真正在哪里执行？

继续阅读：

```python
_execute_model_sync(...)
```

其核心源码是：

```python
def _execute_model_sync(request):

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

    ...
```

终于看到真正的：

```python
model_.invoke(messages)
```

了。

但这里仍然有一个问题：

```python
model_
```

为什么不是直接使用原来的：

```python
model
```

因为模型调用前可能还要完成工具绑定。

### 工具绑定发生在哪里？

在 `_get_bound_model()` 中，普通 Tool Calling 路径包含：

```python
request.model.bind_tools(
    final_tools,
    tool_choice=request.tool_choice,
    **request.model_settings,
)
```

这使模型能够知道当前有哪些工具。

例如：

```text
read_refund
patch_refund
run_refund_tests
```

但要注意：

**`bind_tools()` 只是向模型提供工具描述，不是执行工具。**

执行仍然发生在 ToolNode。

到这里，我们已经打通了 Model Node 的主要过程：

```text
                    Model Node
                        |
                        v
                  读取 messages
                        |
                        v
                 构造 ModelRequest
                        |
                        v
                  准备模型工具
                        |
                        v
                    bind_tools()
                        |
                        v
                   model.invoke()
                        |
                        v
                      AIMessage
```

现在就产生了一个新问题。

模型返回 AIMessage 以后，下一步该怎么办？

---

## 十、模型调用完成以后，谁更新 State？

假设模型返回：

```python
AIMessage(
    content="",
    tool_calls=[
        {
            "name": "read_refund",
            "args": {},
            "id": "call-001",
        }
    ],
)
```

这条消息必须进入 Graph State。

否则，下一个工具节点无法知道模型刚才提出了什么请求。

但我们已经在第三章学习过：

**Graph Node 不应该依赖随意修改共享 State 来传播结果。**

它应该提交状态更新。

因此 LangChain 使用：

```python
_build_commands(...)
```

整理模型响应。

最基本的状态更新可以理解为：

```python
Command(
    update={
        "messages": [ai_message]
    }
)
```

这里是简化后的普通模型响应路径。

真实实现还会处理：

```text
结构化输出
异常 Tool Call 修补
Middleware 返回的 Command
特殊路由
```

但当前只需要理解一点：

**Model Node 运行完成以后，新的 AIMessage 会作为状态更新交给 Graph Runtime。**

于是：

```text
                   model.invoke()
                         |
                         v
                      AIMessage
                         |
                         v
                   State Update
                         |
                         v
                     Graph State
```

但现在还没有开始工具执行。

因为 Graph 还不知道应该走哪条边。

这就是下一步要解决的问题。

---

## 十一、为什么必须有 Model → Tools 的条件路由？

现在我们回到最小 `while`：

```python
if not response.tool_calls:
    break
```

这段代码决定：

- 有工具请求，继续执行。
- 没有工具请求，结束。

把 Agent Loop 变成 Graph 后，判断仍然需要存在。

只是它不再写在一个大循环内部。

而是成为 Graph 的条件路由规则。

### 最简单的路由

我们可以想象：

```python
def route_after_model(state):

    last_message = state["messages"][-1]

    if last_message.tool_calls:
        return "tools"

    return END
```

这段是教学化简代码。

它的目的只是对应：

```python
if not response.tool_calls:
    break
```

但真实 LangChain 需要处理更多情况。

例如：

- AIMessage 中有多个 Tool Call。
- 某些工具请求已经有对应结果。
- 某些工具属于结构化输出。
- 某些扩展希望改变正常路由。

所以 Factory 提供：

```python
_make_model_to_tools_edge(...)
```

### 真实源码：先检查是否需要结束

```python
last_ai_message, tool_messages = (
    _fetch_last_ai_and_tool_messages(
        state["messages"]
    )
)

if last_ai_message is None:
    return end_destination

if len(last_ai_message.tool_calls) == 0:
    return end_destination
```

这里对应最小 Loop 的结束条件。

但还不能立刻把所有 Tool Call 都执行一遍。

为什么？

因为有些 Tool Call 可能已经有对应 ToolMessage。

这时重复执行可能产生错误。

因此，源码继续计算：

```python
pending_tool_calls = [
    c
    for c in last_ai_message.tool_calls
    if (
        c["id"] not in tool_message_ids
        and c["name"] not in structured_output_tools
    )
]
```

这正好连接上一章的通信协议：

```text
ToolCall.id
    |
    v
ToolMessage.tool_call_id
```

通过两个 ID 的对应关系，Runtime 能够判断哪些调用仍未完成。

### 如果还有待执行的工具怎么办？

真实源码：

```python
if pending_tool_calls:

    return [
        Send("tools", [tool_call])
        for tool_call in pending_tool_calls
    ]
```

这意味着：

**Graph 可以为每一个待执行 Tool Call 生成一个面向工具节点的任务。**

为什么不把所有 Tool Call 简单拼接成一段字符串？

因为工具执行是结构化任务，需要保留：

```text
工具名称
参数
调用 ID
```

LangGraph 的 `Send` 用于表达动态派发任务。

在这一步我们只需知道：

**路由函数决定下一步的目标，并可将 Tool Call 派发给 ToolNode。**

真正的并发调度与 ToolRuntime 注入，将在工具执行章节展开。

---

## 十二、工具完成后为什么还需要一条返回边？

现在假设模型发起：

```python
read_refund()
```

ToolNode 返回：

```python
ToolMessage(
    content="refund.py 的源代码……",
    name="read_refund",
    tool_call_id="call-001",
)
```

这条消息进入 State。

但是用户的任务并没有完成。

因为模型还需要分析文件内容。

所以执行不能停在 ToolNode。

默认应该：

```text
Tools
  |
  v
Model
```

这就是工具节点返回模型的原因。

### 真实源码

`factory.py` 中：

```python
_make_tools_to_model_edge(...)
```

普通路径最终返回：

```python
return model_destination
```

这里的：

```python
model_destination
```

在没有额外 Middleware 时，就是：

```text
model
```

因此：

```text
Model Node
    |
    v
ToolNode
    |
    v
Model Node
```

形成循环。

### 为什么工具结束后不总是返回模型？

因为有些工具可以声明：

```python
return_direct=True
```

表示执行完成后直接结束当前 Agent Loop。

另外，结构化输出也有自己的结束条件。

因此真实 Factory 并不是简单写死：

```python
graph.add_edge("tools", "model")
```

而是使用条件路由处理这些特殊情况。

但我们最常见的 Coding Agent 路径仍然是：

```text
ToolMessage
    |
    v
再次调用 Model
```

至此，我们已经得到一个完整的 Agent Loop。

---

## 十三、现在才真正开始创建 StateGraph

前面我们理解了：

- Model Node 需要什么。
- ToolNode 怎样得到工具。
- 模型调用完成以后怎样决定下一步。
- 工具执行以后为什么需要返回模型。

现在可以看 Factory 怎样把这些内容变成一张 Graph。

### 13.1 创建 Graph Builder

真实源码：

```python
graph = StateGraph(
    state_schema=resolved_state_schema,
    input_schema=input_schema,
    output_schema=output_schema,
    context_schema=context_schema,
)
```

我们先暂时只关注：

```python
state_schema
```

它决定节点之间共享的状态字段及其更新规则。

对于 Agent，最重要的是：

```python
messages
```

因为模型和工具必须通过消息历史交换信息。

其他 Schema 的处理属于 State 章节。

### 13.2 注册节点

真实源码：

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

然后：

```python
if tool_node is not None:
    graph.add_node(
        "tools",
        tool_node,
    )
```

注意：

到这一步，只创建了节点。

还没有明确它们之间怎样跳转。

### 13.3 连接入口

无 Middleware 的简单情况下：

```python
graph.add_edge(
    START,
    "model",
)
```

这表示 Agent 启动后，首先进入模型节点。

### 13.4 添加条件路由

Factory 调用：

```python
graph.add_conditional_edges(
    "model",
    RunnableCallable(
        _make_model_to_tools_edge(
            model_destination="model",
            structured_output_tools=...,
            end_destination=END,
        ),
        trace=False,
    ),
    ...,
)
```

这是按照无 Middleware 的普通路径整理的源码结构。

实际代码使用 `loop_exit_node` 等变量，以便后续支持不同 Middleware 节点。

工具执行后：

```python
graph.add_conditional_edges(
    "tools",
    RunnableCallable(
        _make_tools_to_model_edge(
            tool_node=tool_node,
            model_destination="model",
            structured_output_tools=...,
            end_destination=END,
        ),
        trace=False,
    ),
    ...,
)
```

现在我们得到：

```text
                     START
                       |
                       v
                    Model
                       |
                       v
                Has Tool Calls?
                       |
             +---------+---------+
             |                   |
             v                   v
           False                True
             |                   |
             v                   v
            END                 Tools
                                  |
                                  v
                                Model
```

这就是最小 Agent Loop 对应的 Graph。

---

## 十四、为什么需要 `compile()`？

现在我们已经创建：

```python
StateGraph(...)
```

注册了节点：

```python
add_node(...)
```

以及路由规则：

```python
add_conditional_edges(...)
```

能不能直接执行？

通常不行。

因为 StateGraph 此时还是一个构建器。

它保存的是：

```text
节点定义
路由关系
状态 Schema
构建配置
```

它还不是最终的 Runtime 执行对象。

因此需要：

```python
compile()
```

### 真实源码：编译 Graph

Factory 最终返回：

```python
return graph.compile(
    checkpointer=checkpointer,
    store=store,
    interrupt_before=interrupt_before,
    interrupt_after=interrupt_after,
    debug=debug,
    name=name,
    cache=cache,
    transformers=...,
).with_config(config)
```

这里暂时不需要深入解释所有参数。

我们只关心核心过程：

```text
                  StateGraph Builder
                          |
                          v
                       compile()
                          |
                          v
                   CompiledStateGraph
                          |
                          v
                        invoke()
                          |
                          v
                    LangGraph Runtime
```

为什么这样设计？

因为：

**构建执行结构，与真正执行结构，是两个不同阶段。**

在构建阶段，Factory 可以检查和连接节点。

在运行阶段，Runtime 才根据实际 State 调度节点。

### 到这里，我们能回答一个关键问题

为什么：

```python
agent = create_agent(...)
```

返回的对象能够直接调用：

```python
agent.invoke(...)
```

？

因为 Factory 最终返回的不是普通配置字典。

而是已经编译完成的可执行 Graph。

---

## 十五、把 `create_agent()` 的构建过程完整串起来

现在回头看。

我们并不是简单地逐行翻译 `factory.py`。

而是从一个问题开始：

> 怎样把一个模型和一组工具，自动变成可执行的 Agent Loop？

这要求 Factory 依次完成以下工作。

```text
                   create_agent()
                         |
                         v
                  整理模型与配置
                         |
                         v
                    收集工具
                         |
                         v
                   创建 ToolNode
                         |
                         v
                    定义 State
                         |
                         v
                  创建 StateGraph
                         |
                         v
                  定义 Model Node
                         |
                         v
                  注册 Model/Tools
                         |
                         v
                   连接 START
                         |
                         v
              注册 Model → Tools 路由
                         |
                         v
              注册 Tools → Model 路由
                         |
                         v
                     compile()
                         |
                         v
                 CompiledStateGraph
```

最终：

```python
agent.invoke(...)
```

就会启动这个 Graph。

这里最值得注意的是：

**`create_agent()` 的主要工作是组装，而不是重新实现模型或工具本身。**

模型来自 LangChain Core / 模型 Provider。

工具执行复用 LangGraph ToolNode。

状态图由 LangGraph 提供。

Factory 将这些能力按照 Agent Loop 的需求连接起来。

---

## 十六、但 DeepAgents 为什么还需要 Middleware？

现在我们已经拥有：

```python
agent = create_agent(
    model=model,
    tools=tools,
)
```

它已经能够完成：

```text
读取文件
修改文件
执行测试
根据结果继续决策
```

假设任务变得更加复杂。

我们又希望：

```text
每次调用模型前检查上下文长度
每次执行危险工具前检查权限
模型调用后记录运行信息
任务开始时加载项目知识
```

这些能力应该写在哪里？

如果全部塞进：

```python
model_node()
```

或：

```python
tool_node()
```

那么每增加一种能力，就要修改 Factory 的核心逻辑。

这会破坏我们刚刚得到的通用 Agent Loop。

所以 LangChain 需要一种可扩展的装配机制。

这就是 Middleware。

现在先不要深入研究 Middleware 的具体 Hook。

只观察 Factory 已经为它保留了哪些位置。

### 16.1 Middleware 可以提供工具

前面已经看到：

```python
middleware_tools = [
    t
    for m in middleware
    for t in getattr(m, "tools", [])
]
```

这允许一个扩展组件为 Agent 提供新工具。

### 16.2 Middleware 可以增加 State 字段

Factory 还会收集：

```python
state_schemas = [
    *(m.state_schema for m in middleware),
    base_state,
]
```

然后解析成 Graph 可以使用的 State Schema。

为什么需要？

因为某些扩展除了读取消息，还需要保存自己的运行状态。

例如任务规划可能需要：

```python
todos
```

### 16.3 Middleware 可以在模型和工具调用阶段介入

在 Model Node 中，我们已经看到：

```python
if wrap_model_call_handler is None:
    model_response = _execute_model_sync(request)
else:
    result = wrap_model_call_handler(
        request,
        _execute_model_sync,
    )
```

这说明模型调用逻辑可以被包装。

ToolNode 创建时也会接收：

```python
wrap_tool_call=wrap_tool_call_wrapper
```

它允许工具执行被包装。

但我们现在只需知道：

**Factory 在构建 Agent Loop 时预留了这些扩展位置。**

下一篇才会详细研究它们的生命周期和调用顺序。

### 16.4 为什么有些 Middleware 甚至会成为 Graph Node？

Factory 中还可以找到：

```python
graph.add_node(
    f"{m.name}.before_model",
    before_node,
    ...
)
```

这里说明：

有的 Middleware 会被编译为新的 Graph 节点。

而另一些 Middleware 则不会增加新的节点，而是在 Model Node 或 ToolNode 内部包裹调用。

为什么会有两种设计？

因为：

```text
在某轮模型调用前执行一步逻辑
```

与：

```text
包裹模型调用本身
```

不是同一件事。

但这个问题留到 Middleware 章节再展开。

我们在本章只建立一个基本认识：

**Factory 不仅能构建最小 Agent Loop，还能够在组装时为扩展能力预留结构。**

---

# 十七、实验：使用真实 `create_agent()` 完成退款修复循环

前面我们已经从源码理解 Factory 的构建过程。

现在通过一次真实执行验证。

本实验与第一章保持相同任务：

```text
读取 refund.py
    |
    v
第一次修复
    |
    v
测试失败
    |
    v
第二次修复
    |
    v
测试通过
    |
    v
结束
```

但这次我们不再自己写：

```python
while True:
    ...
```

而是使用：

```python
create_agent(...)
```

让 LangChain 构建真正的 Agent Graph。

为了避免 API Key 干扰，我们使用一个受控 Fake Model。

Fake Model 负责预先生成工具调用。

真正的 ToolNode、工具执行、路由和状态更新由框架完成。

## 17.1 安装

```bash
pip install "deepagents==0.7.32"
```

这个固定版本要求 Python 3.11 或以上。

也可以在兼容环境中单独安装 LangChain 与 LangGraph。

## 17.2 完整代码

创建：

`test_create_agent_loop.py`

```python
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import subprocess
import sys

from langchain.agents import create_agent

from langchain_core.language_models.fake_chat_models import (
    FakeMessagesListChatModel,
)

from langchain_core.messages import (
    AIMessage,
    ToolMessage,
)

from langchain_core.tools import tool


# ==========================================
# 1. 准备退款项目
# ==========================================

INITIAL_SOURCE = '''
def allocate_coupon(total, count):
    return [round(float(total) / count, 2)] * count
'''

NAIVE_PATCH = '''
from decimal import Decimal, ROUND_HALF_UP

def allocate_coupon(total, count):
    share = (
        Decimal(str(total)) / count
    ).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )

    return [share] * count
'''

CORRECT_PATCH = '''
from decimal import Decimal

def allocate_coupon(total, count):
    cents = int(Decimal(str(total)) * 100)
    base, remainder = divmod(cents, count)

    return [
        Decimal(base + (i < remainder)) / 100
        for i in range(count)
    ]
'''

TEST_SOURCE = '''
import unittest
from decimal import Decimal

from refund import allocate_coupon


class RefundTests(unittest.TestCase):

    def test_one_cent_two_items(self):
        values = allocate_coupon("0.01", 2)

        self.assertEqual(
            sum(values),
            Decimal("0.01"),
        )

    def test_three_cents_two_items(self):
        values = allocate_coupon("0.03", 2)

        self.assertEqual(
            sum(values),
            Decimal("0.03"),
        )
'''


# ==========================================
# 2. 使用 Fake Model 模拟模型决策
# ==========================================

class ScriptedModel(FakeMessagesListChatModel):

    def bind_tools(
        self,
        tools: Any,
        **kwargs: Any,
    ):
        # Fake Model 已经预先生成 ToolCall。
        # 因此这里不连接真实模型 Provider。
        return self


def call(name, args, call_id):

    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": name,
                "args": args,
                "id": call_id,
            }
        ],
    )


with TemporaryDirectory() as folder:

    root = Path(folder)

    (root / "refund.py").write_text(
        INITIAL_SOURCE,
        encoding="utf-8",
    )

    (root / "test_refund.py").write_text(
        TEST_SOURCE,
        encoding="utf-8",
    )

    # ======================================
    # 3. 定义真实可执行工具
    # ======================================

    @tool
    def read_refund() -> str:
        """Read refund.py."""

        print("[TOOL] read_refund")

        return (
            root / "refund.py"
        ).read_text(
            encoding="utf-8"
        )

    @tool
    def patch_refund(strategy: str) -> str:
        """Apply a demo refund precision patch."""

        print(
            "[TOOL] patch_refund:",
            strategy,
        )

        patches = {
            "naive": NAIVE_PATCH,
            "correct": CORRECT_PATCH,
        }

        if strategy not in patches:
            return "Unknown patch strategy"

        (root / "refund.py").write_text(
            patches[strategy],
            encoding="utf-8",
        )

        return f"Patch applied: {strategy}"

    @tool
    def run_refund_tests() -> str:
        """Run refund unit tests."""

        print("[TOOL] run_refund_tests")

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "unittest",
                "discover",
                "-s",
                str(root),
                "-p",
                "test_refund.py",
                "-q",
            ],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )

        status = (
            "PASS"
            if result.returncode == 0
            else "FAIL"
        )

        return (
            f"{status} "
            f"(exit={result.returncode})\n"
            f"{result.stdout[-300:]}\n"
            f"{result.stderr[-500:]}"
        )

    # ======================================
    # 4. 预先设置六轮模型响应
    # ======================================

    model = ScriptedModel(
        responses=[
            call(
                "read_refund",
                {},
                "call-1",
            ),
            call(
                "patch_refund",
                {"strategy": "naive"},
                "call-2",
            ),
            call(
                "run_refund_tests",
                {},
                "call-3",
            ),
            call(
                "patch_refund",
                {"strategy": "correct"},
                "call-4",
            ),
            call(
                "run_refund_tests",
                {},
                "call-5",
            ),
            AIMessage(
                content=(
                    "退款精度修复完成，"
                    "第二次运行测试通过。"
                )
            ),
        ]
    )

    # ======================================
    # 5. 创建真正的 Agent Graph
    # ======================================

    agent = create_agent(
        model=model,
        tools=[
            read_refund,
            patch_refund,
            run_refund_tests,
        ],
    )

    # ======================================
    # 6. 观察 Graph 包含哪些节点
    # ======================================

    graph = agent.get_graph()

    print(
        "Graph nodes:",
        list(graph.nodes.keys()),
    )

    # ======================================
    # 7. 运行真正的 Agent Loop
    # ======================================

    result = agent.invoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "修复优惠券部分退款精度 Bug，"
                        "并运行测试。"
                    ),
                }
            ]
        },
        config={
            "recursion_limit": 50,
        },
    )

    # ======================================
    # 8. 提取真实 ToolMessage
    # ======================================

    tool_results = [
        message
        for message in result["messages"]
        if isinstance(message, ToolMessage)
    ]

    assert len(tool_results) == 5

    assert [
        message.name
        for message in tool_results
    ] == [
        "read_refund",
        "patch_refund",
        "run_refund_tests",
        "patch_refund",
        "run_refund_tests",
    ]

    # ======================================
    # 9. 检查两次测试结果
    # ======================================

    test_results = [
        message
        for message in tool_results
        if message.name == "run_refund_tests"
    ]

    assert len(test_results) == 2

    assert test_results[0].content.startswith(
        "FAIL"
    )

    assert test_results[1].content.startswith(
        "PASS"
    )

    # ======================================
    # 10. 验证正常结束
    # ======================================

    final_message = result["messages"][-1]

    assert isinstance(
        final_message,
        AIMessage,
    )

    assert not final_message.tool_calls

    print("\nFinal:", final_message.content)

    print("\nAgent Loop verified.")
```

## 17.3 这个实验应该观察到什么？

构建 Agent 时：

```text
create_agent()
    |
    v
Model Node
Tools Node
```

执行时：

```text
[TOOL] read_refund

[TOOL] patch_refund: naive

[TOOL] run_refund_tests

[TOOL] patch_refund: correct

[TOOL] run_refund_tests

Final: 退款精度修复完成，第二次运行测试通过。

Agent Loop verified.
```

第一次测试会失败。

因为第一次修复只是将金额计算转换成 Decimal，但仍然对每一项单独舍入。

第二次修复使用整数分分配余数。

因此这两个演示测试应该通过。

这里的模型决策是预设的，不是模型真实分析测试失败后自主找到第二种算法。

不过：

**模型节点如何循环、工具如何执行、结果如何进入消息历史、何时结束，都交给了真实的 Agent Graph。**

实验代码根据所核对的框架接口编写，尚未在当前环境安装依赖执行；上面列出的是预期输出，而非已确认的本地运行记录。

---

## 十八、这个实验究竟证明了什么？

我们可以将第一章与本章对照。

### 第一章：我们自己管理整个 Loop

```python
while True:

    response = fake_model(messages)

    if response.tool_calls:
        execute_tools(...)
    else:
        break
```

开发者需要自己管理：

```text
模型调用
工具分发
消息记录
循环结束
```

### 本章：由 LangChain 和 LangGraph 管理

```python
agent = create_agent(
    model=model,
    tools=tools,
)

agent.invoke(...)
```

开发者只提供：

```text
Model
Tools
```

Factory 负责构建：

```text
Model Node
Tools Node
Conditional Edges
StateGraph
```

Runtime 负责执行：

```text
Model
  |
  v
Tools
  |
  v
Model
```

这就是 Agent Factory 的价值。

它并不是提供了另一种 Agent 定义。

而是将我们已经理解的 Agent Loop，封装成一种可复用的构建过程。

---

## 十九、为什么不能把 Factory 直接理解为 Middleware？

我们现在已经接近本系列最重要的架构问题。

Factory 和 Middleware 都与 Agent 扩展有关。

但它们不是一回事。

### Factory

主要负责：

```text
构建 Agent
创建节点
注册工具
组合扩展
连接路由
编译 Graph
```

### Middleware

主要负责：

```text
声明额外能力
在特定位置处理模型请求
在特定位置处理工具调用
增加运行前后的逻辑
扩展 State 或 Tools
```

所以它们的关系是：

```text
                     create_agent()
                           |
                           v
                    Agent Factory
                           |
             +-------------+-------------+
             |             |             |
             v             v             v
         Model Node     ToolNode       Middleware
             |             |             |
             +-------------+-------------+
                           |
                           v
                       StateGraph
                           |
                           v
                         compile()
                           |
                           v
                      Agent Runtime
```

**Factory 负责装配 Middleware，Middleware 负责提供扩展行为。**

这也是为什么我们要先研究 `create_agent()`，再研究 Middleware。

如果没有理解 Agent Graph 怎样构建，就很难理解：

```python
before_model()
```

为什么会成为一个 Graph Node，而：

```python
wrap_model_call()
```

为什么在 Model Node 内部运行。

---

## 二十、完整源码调用链

现在我们可以从 DeepAgents 入口开始，完整理解创建 Agent 的过程：

```text
                   create_deep_agent()
                           |
                           v
                     准备 Model
                           |
                           v
                     准备 Tools
                           |
                           v
                  准备 Middleware
                           |
                           v
                      create_agent()
                           |
                           v
                    统一模型与配置
                           |
                           v
                    收集所有工具
                           |
                           v
                     创建 ToolNode
                           |
                           v
                   整理 State Schema
                           |
                           v
                     创建 StateGraph
                           |
                           v
                     定义 Model Node
                           |
                           v
                   绑定模型和工具
                           |
                           v
                    注册 Graph Nodes
                           |
                           v
                    构建条件路由
                           |
                           v
                        compile()
                           |
                           v
                   CompiledStateGraph
                           |
                           v
                       agent.invoke()
                           |
                           v
                       Agent Loop
```

请特别注意中间的：

```text
创建 ToolNode
```

与：

```text
真正执行 ToolNode
```

不是同一阶段。

前者发生在创建 Agent 时。

后者发生在运行 Agent 时。

同样：

```text
定义 Model Node
```

与：

```text
真正调用模型
```

也不是同一阶段。

理解这个区别，能够避免很多源码阅读中的混淆。

---

## 二十一、本章最终应该掌握什么？

我们从一个很简单的需求出发：

> 能否只给出 Model 和 Tools，就得到一个能运行的 Agent Loop？

然后逐步推导出 `create_agent()` 必须完成的工作。

首先，要整理模型和系统指令。

然后，要收集实际可用的工具。

接着，要创建负责执行工具的 ToolNode。

之后，需要创建负责调用模型的 Model Node。

但只有两个节点还不够。

还需要定义：

```text
模型返回 Tool Call 后去哪里？
工具执行完成以后去哪里？
什么情况下结束？
```

因此需要条件边。

最后，Graph 的声明结构还必须被编译成可执行对象。

这就得到了：

```text
Model + Tools
      |
      v
 create_agent()
      |
      v
  StateGraph
      |
      v
   compile()
      |
      v
    Agent
```

本章最重要的结论是：

> **LangChain 的 `create_agent()` 本质上是一个 Agent Loop Factory：它将模型、工具、状态结构和执行路由组装成一张可运行的 LangGraph，并为后续 Middleware 扩展预留接入位置。**

---

# 下一章：为什么 Agent Loop 需要 Middleware？

现在我们已经得到了一个完整、通用的 Agent Loop。

但接下来，Coding Agent 又会遇到新的问题：

- 每轮模型调用前，需要检查上下文长度。
- 执行文件修改前，需要检查权限。
- 执行工具后，需要处理大型结果。
- Agent 运行前，需要加载某些项目知识。

如果把这些逻辑全部写进：

```python
model_node()
```

和：

```python
tool_node()
```

就会不断修改 Agent Factory 的核心逻辑。

因此下一章从这个问题开始：

**怎样让 Agent Loop 保持稳定，却能够在循环的不同位置插入新的处理逻辑？**

这时我们才正式引入：

```python
AgentMiddleware
```

并研究：

```text
before_agent
before_model
wrap_model_call
after_model
wrap_tool_call
after_agent
```

为什么分别存在。

我们将继续遵循同一条阅读路径：

```text
先遇到真实问题
      |
      v
尝试最简单的方案
      |
      v
发现方案的局限
      |
      v
推导需要的新能力
      |
      v
查看对应的真实源码
      |
      v
通过实验验证
```

---

## 本章源码导航

### DeepAgents

固定版本：

```text
0.7.32
Commit e85c6b9
```

入口：

`libs/deepagents/deepagents/graph.py`

重点：

```python
create_deep_agent()
```

以及：

```python
return create_agent(...)
```

源码：

https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py

### LangChain Agent Factory

文件：

`libs/langchain_v1/langchain/agents/factory.py`

建议按照下面的顺序阅读：

```text
create_agent()
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
StateGraph(...)
    |
    v
model_node()
    |
    v
_execute_model_sync()
    |
    v
graph.add_node(...)
    |
    v
_make_model_to_tools_edge()
    |
    v
_make_tools_to_model_edge()
    |
    v
graph.add_conditional_edges(...)
    |
    v
graph.compile()
```

源码：

https://github.com/langchain-ai/langchain/blob/master/libs/langchain_v1/langchain/agents/factory.py

### LangGraph

本章主要关注：

```text
StateGraph
ToolNode
CompiledStateGraph
```

源码：

https://github.com/langchain-ai/langgraph/blob/main/libs/langgraph/langgraph/graph/state.py

https://github.com/langchain-ai/langgraph/blob/main/libs/prebuilt/langgraph/prebuilt/tool_node.py

**版本注意：** DeepAgents 使用固定 Commit；LangChain、LangGraph 的链接指向仓库主分支。DeepAgents 0.7.32 要求 `langchain>=1.4.7,<2.0.0`。如果需要逐行断点调试，应以实际安装的依赖版本为准。

---

## 本章自检

读完以后，应该能够自己解释：

1. 为什么要将 `create_agent()` 与 `agent.invoke()` 分开？
2. `create_agent()` 为什么要统一模型和工具的输入形式？
3. `middleware.tools` 是怎样进入工具集合的？
4. 为什么需要统一的 ToolNode，而不是每个工具都定义一个 Graph Node？
5. Model Node 怎样进行一次真正的模型调用？
6. `bind_tools()` 与执行工具有什么区别？
7. 模型响应怎样进入 Graph State？
8. Model → Tools 与 Tools → Model 为什么都需要路由？
9. `compile()` 为什么是构建流程的最后一步？
10. Factory 与 Middleware 的职责有什么区别？

如果这些问题都能够解释清楚，就可以自然进入下一章，而不需要提前背诵 Middleware 的全部 Hook。
