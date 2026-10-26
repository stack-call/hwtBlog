---
title: 第三章：为什么 Agent Loop 要从 while 循环演化为 StateGraph？
description: 从最小 Agent Loop 的工程局限出发，逐步推导 Node、Edge、StateGraph 和 Runtime，并结合 LangGraph 与 LangChain 真实源码实现一个完整的模型—工具循环
tags: [DeepAgents, LangGraph, Agent Loop, StateGraph, Pregel, 源码精读]
sidebar_position: 3
---

# 第三章：为什么 Agent Loop 要从 while 循环演化为 StateGraph？

## 一、从上一章的 Agent Loop 开始

前两章，我们已经解决两个问题。

第一章回答：

**为什么需要 Agent Loop？**

因为复杂任务不是一次模型调用就能完成的。

模型必须根据工具产生的新信息，持续决定下一步行动。

第二章回答：

**模型和工具如何交换信息？**

通过：

```python
AIMessage.tool_calls
```

表达行动请求，通过：

```python
ToolMessage
```

反馈执行结果。

因此，一个最小 Agent Loop 已经可以写成：

```python
# 教学伪代码

while True:

    ai_message = model.invoke(messages)

    messages.append(ai_message)

    if not ai_message.tool_calls:
        break

    for call in ai_message.tool_calls:

        tool = tools[call["name"]]

        result = tool.invoke(call)

        messages.append(result)
```

这段代码已经拥有完整的反馈循环：

```text
               Model
                 |
                 v
             AIMessage
                 |
           有 Tool Call？
                 |
        +--------+--------+
        |                 |
        v                 v
       否                 是
        |                 |
        v                 v
       END             执行工具
                          |
                          v
                     ToolMessage
                          |
                          v
                         Model
```

那么问题来了：

**这段代码明明已经可以运行，为什么还需要 LangGraph？**

先给出一个非常重要的判断：

> 普通 `while` 循环完全可以实现 Agent。使用 Graph 不是 Agent 成立的必要条件，而是为了让复杂 Agent 的执行结构更容易组合、观察和管理。

所以本章不是要证明 `while` 循环不好。

而是研究：

**当我们希望把这个最小循环扩展为通用 Agent Harness 时，为什么会逐渐需要 Graph 所提供的结构化执行能力？**

---

## 二、问题一：所有执行逻辑都混在一个循环里

继续使用退款 Bug 案例。

用户要求：

> 修复 `refund.py` 中的优惠券分摊精度问题，并运行测试。

在最初的 Agent Loop 中：

```python
while True:

    ai_message = model.invoke(messages)

    if ai_message.tool_calls:

        for call in ai_message.tool_calls:

            result = execute_tool(call)

    else:

        break
```

这里实际混合了几类完全不同的责任。

### 模型调用

```python
model.invoke(messages)
```

负责根据已有信息产生新决策。

### 工具执行

```python
execute_tool(call)
```

负责真正修改外部环境。

### 执行路由

```python
if ai_message.tool_calls:
```

负责决定继续执行工具，还是结束任务。

### 历史更新

```python
messages.append(...)
```

负责让下一次模型调用能够看到新信息。

在一个几十行的程序里，这些逻辑放在一起没有问题。

但假设我们想增加一个功能：

> 每次执行工具前记录日志。

可以写：

```python
log_tool_call(call)

result = execute_tool(call)
```

再增加：

> 每次调用模型前检查 Token 数量。

就要在模型调用处添加：

```python
check_context(messages)
```

再增加：

> 每次文件修改前检查权限。

又要修改工具执行流程。

随着能力增加：

```python
while True:

    check_context()

    load_memory()

    ai_message = model.invoke(messages)

    update_plan()

    if ai_message.tool_calls:

        for call in ai_message.tool_calls:

            log_tool_call(call)

            check_permissions(call)

            result = execute_tool(call)

            save_artifacts(result)

    else:

        break
```

问题不在于这段代码不能工作。

而在于：

**所有扩展逻辑都必须知道主循环的内部结构，并且直接修改它。**

这样会使模型执行、工具执行、日志、权限和状态管理之间的耦合越来越强。

### 2.1 我们真正需要什么？

仔细观察这些代码。

虽然处理逻辑变多了，但核心流程并没有改变：

```text
模型执行
    |
    v
判断是否调用工具
    |
    v
工具执行
    |
    v
返回模型
```

我们希望保留这条稳定的执行主线。

同时允许不同组件在相应位置接入。

于是产生第一个设计需求：

> **将执行步骤从循环控制代码中分离出来，让执行步骤本身成为可独立注册、替换和组合的单元。**

现在才适合引入 Graph 的第一个概念。

---

## 三、Node：把执行步骤变成独立单元

Node 的意思是**节点**。

在 LangGraph 中，一个节点通常对应一次可执行操作。

例如：

```text
Model Node：
    调用模型，产生 AIMessage

Tools Node：
    执行工具，产生 ToolMessage
```

我们可以先把普通循环拆成两个函数：

```python
def model_node(state):
    ...


def tools_node(state):
    ...
```

它们各自只负责一件事。

### 3.1 Model Node 的职责

它接收当前状态，调用模型，返回新消息。

```python
def model_node(state):

    response = model.invoke(
        state["messages"]
    )

    return {
        "messages": [response]
    }
```

注意返回值：

```python
{
    "messages": [response]
}
```

为什么不直接执行：

```python
state["messages"].append(response)
```

？

因为我们希望模型节点只描述：

**这一次执行产生了什么新状态。**

至于怎样把新消息加入完整状态，由运行时统一处理。

这个问题稍后再讨论。

### 3.2 Tools Node 的职责

它读取当前消息中的 Tool Call，执行对应工具，返回 ToolMessage。

概念上：

```python
def tools_node(state):

    ai_message = state["messages"][-1]

    results = []

    for call in ai_message.tool_calls:

        tool = tools[call["name"]]

        result = tool.invoke(call)

        results.append(result)

    return {
        "messages": results
    }
```

这段是教学化简。

真实 LangGraph 的 `ToolNode` 还要处理工具参数校验、Runtime 注入、异常、并发调度等问题。

但对于本章，我们已经获得了一个重要结果：

```text
                   Agent Loop
                       |
             +---------+---------+
             |                   |
             v                   v
          Model Node          Tools Node
             |                   |
             v                   v
          模型决策             工具执行
```

### 3.3 为什么 Node 是有价值的抽象？

因为我们可以单独替换它们。

例如：

```python
builder.add_node(
    "model",
    model_node,
)
```

模型可以从一个 Provider 换到另一个 Provider。

而工具执行仍然由：

```python
builder.add_node(
    "tools",
    tool_node,
)
```

负责。

这样，模型执行与工具执行不再必须写在同一个函数内部。

但现在又出现一个问题。

**Node 已经独立了，谁决定它们应该按照什么顺序执行？**

这就自然引出了 Edge。

---

## 四、Edge：执行完一个节点以后去哪里？

Edge 的意思是**边**。

在 Graph 中，它表示节点之间的执行关系。

例如：

```text
Tools Node
    |
    v
Model Node
```

它表达：

> 工具执行完成以后，重新进入模型决策。

在 LangGraph 中可以写：

```python
builder.add_edge(
    "tools",
    "model",
)
```

这条边是固定的。

无论工具结果是什么，只要正常完成这个节点，就返回模型节点。

但 Model Node 的后续路由不同。

因为模型输出可能是：

```python
AIMessage(
    content="",
    tool_calls=[...],
)
```

也可能是：

```python
AIMessage(
    content="任务已经完成。",
    tool_calls=[],
)
```

前者需要执行工具。

后者通常可以结束。

因此，Model Node 不能简单地固定连接到 Tools Node。

它需要一个判断。

---

## 五、为什么需要 Conditional Edge？

回到原来的 `while`：

```python
if not ai_message.tool_calls:
    break
```

这段代码本质上在回答：

**模型执行以后，下一步去哪里？**

因此，它本来就属于执行路由逻辑。

拆成 Graph 后，我们可以将它定义为一个独立函数：

```python
from langgraph.graph import END


def should_continue(state):

    last_message = state["messages"][-1]

    if last_message.tool_calls:
        return "tools"

    return END
```

然后注册：

```python
builder.add_conditional_edges(
    "model",
    should_continue,
)
```

这就形成：

```text
                  Model Node
                       |
                       v
                 should_continue
                       |
              +--------+--------+
              |                 |
              v                 v
            tools              END
```

### 5.1 为什么不让 Model Node 自己调用 ToolNode？

例如：

```python
def model_node(state):

    response = model.invoke(...)

    if response.tool_calls:
        return tools_node(state)
```

这样也可以实现调用。

但会使 Model Node 同时承担：

- 模型决策。
- 执行路由。
- 工具调用。
- 工具执行结果管理。

我们前面刚刚希望将这些责任分离。

如果在 Model Node 中又直接调用 Tools Node，就重新产生了耦合。

因此 Graph 采用：

```text
Node：
    做什么？

Edge：
    下一步去哪里？
```

这是本章第一个重要的架构分离。

---

## 六、真实源码：StateGraph 怎样保存 Node 和 Edge？

现在我们已经从设计需求推导出了：

```text
Node
Edge
Conditional Edge
```

接下来打开真实源码：

`langgraph/graph/state.py`

找到：

```python
class StateGraph(...):
    ...
```

### 6.1 为什么 StateGraph 初始化时不执行任何节点？

源码的初始化逻辑中包含：

```python
self.nodes = {}

self.edges = set()

self.branches = defaultdict(dict)

self.schemas = {}

self.channels = {}

self.compiled = False
```

这是实际源码的核心字段节选。

这里的三个结构特别重要。

```python
self.nodes
```

存放注册的节点。

```python
self.edges
```

存放固定执行边。

```python
self.branches
```

存放条件路由规则。

为什么要分别保存？

因为我们现在还处于：

**定义执行结构的阶段。**

这时候并没有用户任务输入，也不需要真正调用模型。

例如：

```python
builder = StateGraph(MessagesState)
```

只是创建一个 Graph 构建器。

接下来：

```python
builder.add_node(...)
```

注册节点。

```python
builder.add_edge(...)
```

注册固定边。

```python
builder.add_conditional_edges(...)
```

注册条件路由。

所以，Graph 的构建与运行是两个不同阶段。

这一点非常重要。

---

## 七、真实源码：`add_node()` 做了什么？

在：

`langgraph/graph/state.py`

中找到：

```python
def add_node(
    self,
    node,
    action=None,
    *,
    ...
):
    ...
```

我们传入：

```python
builder.add_node(
    "model",
    model_node,
)
```

源码并不会立即执行：

```python
model_node(...)
```

它主要将函数包装为 Runnable，并保存节点描述。

核心代码：

```python
self.nodes[node] = StateNodeSpec(
    coerce_to_runnable(
        action,
        name=node,
        trace=False,
    ),
    metadata,
    input_schema=self.state_schema,
    ...
)
```

这是根据真实源码删去其他参数的节选。

其中：

```python
coerce_to_runnable(...)
```

将普通函数适配为 LangChain/LangGraph 可执行的 Runnable。

而：

```python
StateNodeSpec
```

保存该节点的构建信息。

### 为什么这里需要 Runnable？

因为运行时不能只支持：

```python
def model_node(...):
    ...
```

这样的同步函数。

它可能还要执行：

```text
异步函数
模型 Runnable
工具节点
子 Graph
经过包装的调用链
```

因此框架需要统一的执行接口。

`Runnable` 就承担了这一层适配。

不过现在要记住的重点是：

**add_node() 是注册，不是执行。**

---

## 八、真实源码：`add_edge()` 和 `add_conditional_edges()`

先看固定边。

调用：

```python
builder.add_edge(
    "tools",
    "model",
)
```

源码核心操作：

```python
self.edges.add(
    (start_key, end_key)
)
```

因此 Graph 构建器记录：

```python
("tools", "model")
```

表示工具节点完成后返回模型节点。

再看条件边。

调用：

```python
builder.add_conditional_edges(
    "model",
    should_continue,
)
```

源码核心操作：

```python
path = coerce_to_runnable(
    path,
    name=None,
    trace=True,
)

name = path.name or "condition"

self.branches[source][name] = (
    BranchSpec.from_path(
        path,
        path_map,
        True,
    )
)
```

这里出现一个新结构：

```python
BranchSpec
```

不要把它理解成新的一种 Agent 能力。

它只是为了保存：

```text
从哪个节点出发？
使用哪个条件判断？
条件返回值对应哪些目标节点？
```

例如：

```text
source:
    model

condition:
    should_continue

destinations:
    tools / END
```

所以：

```text
固定边
    → 保存一个确定的后继节点

条件边
    → 保存一个运行时判断后继节点的规则
```

这就是 Graph 能够表达 Agent Loop 的原因。

---

## 九、现在才引入 State：节点之间如何传递信息？

我们已经把模型和工具拆成两个节点。

但是还需要解决：

**Model Node 怎样看到 Tools Node 上一轮返回的结果？**

如果节点之间没有共享的运行状态，那么：

```text
Model Node
    |
    v
Tools Node
    |
    v
Model Node
```

虽然形成了循环，但第二次模型调用未必知道第一次工具执行发生了什么。

因此必须有一个跨节点的数据载体。

在 LangGraph 中，这就是：

**State。**

最简单的状态可以是：

```python
{
    "messages": [...]
}
```

为什么保存 messages？

因为上一章已经证明：

```text
HumanMessage
AIMessage
ToolMessage
```

共同组成了模型下一轮决策的历史。

### 9.1 StateGraph 的节点协议

真实源码中的 `StateGraph` 文档明确规定：

```text
State → Partial<State>
```

也就是说：

节点读取当前 State。

然后返回需要更新的部分。

例如 Model Node：

```python
def model_node(state):

    response = model.invoke(
        state["messages"]
    )

    return {
        "messages": [response]
    }
```

Tools Node：

```python
def tools_node(state):

    ...

    return {
        "messages": tool_results
    }
```

注意两个节点都返回：

```python
{
    "messages": [...]
}
```

但它们只返回**本次新增的消息**。

为什么不返回完整历史？

因为那会让每个节点都必须自己负责复制和维护完整消息列表。

我们希望 Node 只负责产生新信息，Graph Runtime 负责状态更新。

但这里又出现一个问题。

---

## 十、Node 返回新消息以后，怎样与旧消息合并？

假设当前 State 是：

```python
{
    "messages": [
        HumanMessage(
            content="修复退款 Bug"
        )
    ]
}
```

Model Node 返回：

```python
{
    "messages": [
        AIMessage(
            content="",
            tool_calls=[...],
        )
    ]
}
```

如果简单覆盖：

```python
state["messages"] = update["messages"]
```

用户最初的任务消息就会丢失。

但如果每次都要求 Node 返回全部历史，又会重新产生前面的耦合问题。

所以我们需要定义：

**新增消息如何合并到已有消息历史。**

这就是消息 Reducer 需要解决的问题。

本章暂时不展开 Reducer 的全部细节。

只需要知道，LangGraph 提供：

```python
add_messages
```

用于合并消息。

真实源码：

`langgraph/graph/message.py`

核心定义：

```python
class MessagesState(TypedDict):

    messages: Annotated[
        list[AnyMessage],
        add_messages,
    ]
```

其中：

```python
add_messages
```

负责把新消息加入历史，并根据消息 ID 处理必要的替换和删除。

因此，我们现在有：

```text
                  Model Node
                      |
                      v
                新 AIMessage
                      |
                      v
                 State Update
                      |
                      v
                 add_messages
                      |
                      v
                完整 messages
                      |
                      v
                   ToolNode
                      |
                      v
                新 ToolMessage
                      |
                      v
                 State Update
                      |
                      v
                 add_messages
                      |
                      v
                完整 messages
                      |
                      v
                  Model Node
```

这样就实现了：

**节点只产生更新，StateGraph 负责在节点之间维护一致的消息历史。**

这里要特别注意：

`MessagesState` 是一个方便使用的预定义 State Schema。

它不是 DeepAgents 全部 State 字段的完整定义。

以后 DeepAgents 还会在 State 中加入其他能力需要的字段。

---

## 十一、现在终于可以定义完整的 Graph 结构

经过前面的问题推导，我们已经有：

```text
Node：
    模型与工具分别执行

Edge：
    决定固定后继关系

Conditional Edge：
    根据模型输出决定下一步

State：
    在不同节点之间传递消息

Reducer：
    合并节点产生的消息更新
```

现在可以构建 Agent Loop。

```python
from langgraph.graph import (
    START,
    END,
    StateGraph,
    MessagesState,
)

from langgraph.prebuilt import (
    ToolNode,
    tools_condition,
)


builder = StateGraph(MessagesState)

builder.add_node(
    "model",
    model_node,
)

builder.add_node(
    "tools",
    ToolNode(tools),
)

builder.add_edge(
    START,
    "model",
)

builder.add_conditional_edges(
    "model",
    tools_condition,
)

builder.add_edge(
    "tools",
    "model",
)
```

这就形成：

```text
                      START
                        |
                        v
                     model
                        |
                        v
                 tools_condition
                        |
              +---------+---------+
              |                   |
              v                   v
             END                tools
                                  |
                                  v
                                model
```

注意：

**这里只有一个 Model Node。**

不是每次进入循环就创建一个新的模型节点。

当工具执行结束时：

```python
builder.add_edge(
    "tools",
    "model",
)
```

Runtime 会再次调度原来的 Model Node。

这样，就用一张 Graph 表达了原本的：

```python
while True:
    ...
```

但这里还少了一步。

---

## 十二、为什么还需要 `compile()`？

现在 Graph 已经定义好了：

```python
builder = StateGraph(MessagesState)
```

并注册了节点和边。

我们能否直接执行：

```python
builder.invoke(...)
```

？

不能。

因为：

```python
StateGraph
```

本身是一个 **Builder（构建器）**。

它主要保存：

```text
Node 定义
Edge 定义
路由规则
State Schema
```

而真正执行这些节点，还需要另外一套运行时机制。

这就引出了：

```python
graph = builder.compile()
```

### 12.1 `compile()` 究竟做了什么？

打开：

`langgraph/graph/state.py`

找到：

```python
def compile(
    self,
    checkpointer=None,
    *,
    ...
):
    ...
```

源码会先验证 Graph 的结构。

```python
self.validate(...)
```

然后创建：

```python
compiled = CompiledStateGraph(
    builder=self,
    ...
)
```

接下来将此前保存的节点和边连接到可执行 Graph：

```python
compiled.attach_node(
    START,
    None,
)

for key, node in self.nodes.items():
    compiled.attach_node(
        key,
        node,
    )

for start, end in self.edges:
    compiled.attach_edge(
        start,
        end,
    )

for start, branches in self.branches.items():
    for name, branch in branches.items():
        compiled.attach_branch(
            start,
            name,
            branch,
        )
```

最后返回：

```python
return compiled.validate()
```

这是固定源码主分支中真实 `compile()` 的关键执行流程节选。

这里值得注意的是：

**构建阶段保存的只是声明，编译阶段才将这些声明转换为可执行的节点、通道和分支。**

### 12.2 为什么需要单独的编译阶段？

因为 Graph Runtime 需要在执行前确定：

- 哪些节点存在。
- 每个节点使用什么输入和输出 State。
- 哪些边是固定的。
- 哪些边需要运行时判断。
- 哪些 State 字段如何合并。
- 是否配置 Checkpointer。
- 是否配置中断点。

如果这些规则混杂在主执行循环里，扩展和验证都会变得更加复杂。

因此 LangGraph 将：

```text
定义执行结构
```

与：

```text
执行结构
```

分开。

我们可以把它理解为：

```text
                StateGraph Builder
                        |
                        v
               注册 Node / Edge
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
                     Runtime
```

注意，`compile()` 并不是把 Python 代码编译为机器码。

它是根据 Graph 声明构建可执行的运行时结构。

---

## 十三、编译后究竟由谁执行循环？

现在出现最后一个关键问题。

我们已经不再自己写：

```python
while True:
    ...
```

那么是谁在反复调度：

```text
Model → Tools → Model
```

呢？

答案是：

**LangGraph Runtime。**

继续看 `state.py`。

真实源码：

```python
class CompiledStateGraph(
    Pregel[StateT, ContextT, InputT, OutputT],
    Generic[StateT, ContextT, InputT, OutputT],
):
    ...
```

这里可以看到：

```python
CompiledStateGraph
```

继承了：

```python
Pregel
```

现在才需要引入这个名字。

为什么？

因为前面我们已经完成了：

```text
Graph 定义
    |
    v
Graph 编译
```

但还缺少：

```text
Graph 执行
```

`Pregel` 就是 LangGraph 的通用运行时执行机制。

### 13.1 Pregel 怎样推进 Graph？

为了方便理解，可以先将每一轮执行抽象为：

```text
选择当前需要执行的节点
            |
            v
执行这些节点
            |
            v
收集节点产生的 State Update
            |
            v
应用更新
            |
            v
根据边选择下一批节点
```

这与此前的：

```python
while True:
    ...
```

有相似之处。

但两者职责不同。

普通 `while` 由开发者直接编排执行顺序。

Graph Runtime 根据预先声明的节点和边，结合当前状态决定下一步。

例如：

```text
           当前可执行：Model Node
                       |
                       v
                 执行 Model
                       |
                       v
               产生 AIMessage
                       |
                       v
                  更新 State
                       |
                       v
                检查条件路由
                       |
              +--------+--------+
              |                 |
              v                 v
             END              ToolNode
                                 |
                                 v
                              执行工具
                                 |
                                 v
                              更新 State
                                 |
                                 v
                             Model Node
```

### 13.2 为什么 Runtime 需要统一控制状态更新？

因为真实 Graph 不一定永远只有两个节点。

它还可能包含：

```text
多个工具节点
执行前校验节点
人工审批节点
独立任务处理节点
```

这些节点可能在同一个执行阶段中产生多个更新。

因此 Runtime 需要协调：

```text
执行哪些节点
什么时候应用状态更新
哪些节点可以并行
下一阶段执行哪些节点
```

LangGraph 使用分步骤的状态图执行模型来完成这些工作。

但不要误解为：

> 只要使用 StateGraph，所有 Node 就会自动同时运行。

是否能够并行，取决于 Graph 结构以及 Runtime 调度条件。

---

## 十四、这个结构为什么比普通 while 更适合扩展？

现在我们可以回头看最初的问题。

假设我们希望在每次模型调用前执行：

```python
check_context()
```

在普通循环中，需要修改：

```python
while True:

    check_context()

    response = model.invoke(...)
```

而在 Graph 结构中，我们有一个新的可能：

```text
                      START
                        |
                        v
                   before_model
                        |
                        v
                    Model Node
                        |
                        v
                   条件路由
                        |
              +---------+---------+
              |                   |
              v                   v
             END                Tools
                                  |
                                  v
                              before_model
```

这里：

```text
before_model
```

可以成为一个独立的执行节点。

只要把 Loop 的返回位置连接到它，就能保证：

**每一轮模型调用之前，都先执行相应处理逻辑。**

同样，如果希望每次工具执行前都记录日志，也可以在 ToolNode 内部增加包装器。

这里出现一个重要区别：

**Graph Node 可以改变整个执行流程。**

**Wrapper 可以包裹某一次 Node 内部的具体调用。**

这两种机制以后都会被 LangChain Middleware 使用。

我们下一篇将深入研究这部分。

本章先只需要知道：

**明确的 Node 和 Edge 边界，使得 Agent Loop 更容易在不同生命周期位置添加新行为。**

这正是你的新理解能够在真实源码中得到支持的地方。

---

# 十五、实验：用真实 LangGraph 重建退款修复循环

前面的讨论已经足够建立设计模型。

现在通过实验验证：

**不写 `while True`，只声明 Graph，是否真的能够完成读取、修改、测试失败、再次修改、测试通过这一过程？**

这次我们使用真正的：

```python
StateGraph
ToolNode
tools_condition
MessagesState
```

模型仍然用一个可控的脚本函数模拟。

因为本章想验证的是：

**Graph 的执行与路由机制，而不是模型的智能水平。**

工具则会真实修改临时目录中的源码，并通过 Python `unittest` 执行测试。

## 15.1 安装依赖

使用 Python 3.11 及以上。

```bash
pip install "langgraph>=1.0,<2.0" "langchain-core>=1.6,<2.0"
```

本章使用的 API 以已核对的 LangGraph 1.x 系列实现为依据。

## 15.2 创建文件

```text
agent_graph_demo.py
```

完整代码如下。

```python
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import subprocess
import sys

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    ToolMessage,
)
from langchain_core.tools import tool

from langgraph.graph import (
    START,
    StateGraph,
    MessagesState,
)

from langgraph.prebuilt import (
    ToolNode,
    tools_condition,
)


# ==========================================
# 1. 准备演示项目
# ==========================================

INITIAL_CODE = '''
def allocate_coupon(total, count):
    return [round(float(total) / count, 2)] * count
'''

# 第一次修复：
# 使用 Decimal，但仍然分别舍入。
FIRST_PATCH = '''
from decimal import Decimal, ROUND_HALF_UP

def allocate_coupon(total, count):
    part = (
        Decimal(str(total)) / count
    ).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )
    return [part] * count
'''

# 第二次修复：
# 先以整数分分配，再处理余数。
SECOND_PATCH = '''
from decimal import Decimal

def allocate_coupon(total, count):
    cents = int(Decimal(str(total)) * 100)
    base, remainder = divmod(cents, count)

    return [
        Decimal(base + (i < remainder)) / 100
        for i in range(count)
    ]
'''

# 这个测试检查的是最基本的总额守恒。
# 并不覆盖完整生产优惠券业务规则。
TEST_CODE = '''
import unittest
from decimal import Decimal

from refund import allocate_coupon


class RefundTests(unittest.TestCase):

    def test_one_cent(self):
        shares = allocate_coupon("0.01", 2)

        self.assertEqual(
            sum(shares),
            Decimal("0.01"),
        )

    def test_three_cents(self):
        shares = allocate_coupon("0.03", 2)

        self.assertEqual(
            sum(shares),
            Decimal("0.03"),
        )
'''


with TemporaryDirectory() as temp_dir:

    root = Path(temp_dir)

    (root / "refund.py").write_text(
        INITIAL_CODE,
        encoding="utf-8",
    )

    (root / "test_refund.py").write_text(
        TEST_CODE,
        encoding="utf-8",
    )

    # ======================================
    # 2. 创建真实工具
    # ======================================

    @tool
    def read_file(file_path: str) -> str:
        """Read a source file in the demo project."""

        if file_path not in {
            "refund.py",
            "test_refund.py",
        }:
            return "ERROR: file is not allowed"

        print(f"[TOOL] read_file: {file_path}")

        return (root / file_path).read_text(
            encoding="utf-8"
        )

    @tool
    def patch_file(revision: str) -> str:
        """Apply a first or second demonstration patch."""

        print(f"[TOOL] patch_file: {revision}")

        if revision == "first":
            source = FIRST_PATCH

        elif revision == "second":
            source = SECOND_PATCH

        else:
            return "ERROR: unknown revision"

        (root / "refund.py").write_text(
            source,
            encoding="utf-8",
        )

        return f"PATCHED: {revision}"

    @tool
    def run_tests() -> str:
        """Run actual unittest tests in the demo project."""

        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "unittest",
                "discover",
                "-s",
                str(root),
                "-p",
                "test_refund.py",
                "-v",
            ],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )

        status = (
            "PASS"
            if completed.returncode == 0
            else "FAIL"
        )

        print(
            "[TOOL] run_tests:",
            status,
        )

        return (
            f"{status} (exit={completed.returncode})\n"
            f"{completed.stdout[-500:]}\n"
            f"{completed.stderr[-500:]}"
        )

    # ======================================
    # 3. 定义模型节点
    #
    # 这里使用受控脚本模拟 LLM。
    #
    # 它读取消息历史中的最新工具结果，
    # 决定下一步 Tool Call。
    #
    # 真实 LLM 应通过 model.invoke()
    # 产生 AIMessage。
    # ======================================

    def model_node(
        state: MessagesState,
    ) -> dict:

        messages = state["messages"]

        last = messages[-1]

        # 第一次进入模型节点：
        # 读取退款代码。
        if isinstance(last, HumanMessage):

            name = "read_file"

            args = {
                "file_path": "refund.py"
            }

        elif isinstance(last, ToolMessage):

            # 读取完源码后：
            # 如果观察到原始错误实现，
            # 尝试第一种修复。
            if last.name == "read_file":

                if "round(float(total)" not in str(
                    last.content
                ):
                    return {
                        "messages": [
                            AIMessage(
                                content=(
                                    "源码不符合本实验预期，"
                                    "停止执行。"
                                ),
                            )
                        ]
                    }

                name = "patch_file"

                args = {
                    "revision": "first"
                }

            # 文件修改完以后：
            # 运行测试。
            elif last.name == "patch_file":

                name = "run_tests"

                args = {}

            # 根据测试结果决定下一步。
            elif last.name == "run_tests":

                if str(last.content).startswith("PASS"):

                    print("[MODEL] Final answer")

                    return {
                        "messages": [
                            AIMessage(
                                content=(
                                    "退款示例修复完成，"
                                    "测试通过。"
                                )
                            )
                        ]
                    }

                # 第一次失败后，尝试第二次修复。
                patch_count = sum(
                    isinstance(message, ToolMessage)
                    and message.name == "patch_file"
                    for message in messages
                )

                if patch_count == 1:

                    name = "patch_file"

                    args = {
                        "revision": "second"
                    }

                else:

                    return {
                        "messages": [
                            AIMessage(
                                content=(
                                    "第二次修复后测试仍失败，"
                                    "需要人工进一步排查。"
                                )
                            )
                        ]
                    }

            else:
                raise ValueError(
                    f"Unexpected tool: {last.name}"
                )

        else:
            raise ValueError(
                f"Unexpected message: {type(last)}"
            )

        call_id = f"call-{len(messages)}"

        print(f"[MODEL] Tool Call: {name}")

        return {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": name,
                            "args": args,
                            "id": call_id,
                        }
                    ],
                )
            ]
        }

    # ======================================
    # 4. 创建真正的 ToolNode
    # ======================================

    tool_node = ToolNode([
        read_file,
        patch_file,
        run_tests,
    ])

    # ======================================
    # 5. 创建 StateGraph Builder
    # ======================================

    builder = StateGraph(MessagesState)

    # ======================================
    # 6. 注册执行节点
    # ======================================

    builder.add_node(
        "model",
        model_node,
    )

    builder.add_node(
        "tools",
        tool_node,
    )

    # ======================================
    # 7. 注册固定边与条件边
    # ======================================

    builder.add_edge(
        START,
        "model",
    )

    # 有 Tool Call -> tools
    # 没有 Tool Call -> END
    builder.add_conditional_edges(
        "model",
        tools_condition,
    )

    # 工具完成后返回模型。
    builder.add_edge(
        "tools",
        "model",
    )

    # ======================================
    # 8. 编译成可执行 Graph
    # ======================================

    graph = builder.compile()

    # ======================================
    # 9. 真正执行 Graph
    #
    # 注意：这里没有手写 while True。
    # ======================================

    result = graph.invoke(
        {
            "messages": [
                HumanMessage(
                    content=(
                        "修复退款精度问题，"
                        "并运行测试。"
                    ),
                )
            ]
        },
        config={
            "recursion_limit": 30,
        },
    )

    # ======================================
    # 10. 验证结果
    # ======================================

    tool_messages = [
        msg
        for msg in result["messages"]
        if isinstance(msg, ToolMessage)
    ]

    assert [
        msg.name
        for msg in tool_messages
    ] == [
        "read_file",
        "patch_file",
        "run_tests",
        "patch_file",
        "run_tests",
    ]

    tests = [
        msg
        for msg in tool_messages
        if msg.name == "run_tests"
    ]

    assert len(tests) == 2

    assert str(tests[0].content).startswith("FAIL")
    assert str(tests[1].content).startswith("PASS")

    assert isinstance(
        result["messages"][-1],
        AIMessage,
    )

    assert not result["messages"][-1].tool_calls

    # 验证所有工具结果都有对应的调用 ID。
    for msg in tool_messages:
        assert msg.tool_call_id

    # 验证代码真的修改到了第二版。
    final_source = (
        root / "refund.py"
    ).read_text(
        encoding="utf-8"
    )

    assert "divmod" in final_source

    print("\n=== Final Message ===")

    print(result["messages"][-1].content)

    print("\nStateGraph Agent Loop verified.")
```

---

## 十六、实验预期结果

执行：

```bash
python agent_graph_demo.py
```

预期会观察到：

```text
[MODEL] Tool Call: read_file
[TOOL] read_file: refund.py

[MODEL] Tool Call: patch_file
[TOOL] patch_file: first

[MODEL] Tool Call: run_tests
[TOOL] run_tests: FAIL

[MODEL] Tool Call: patch_file
[TOOL] patch_file: second

[MODEL] Tool Call: run_tests
[TOOL] run_tests: PASS

[MODEL] Final answer

=== Final Message ===
退款示例修复完成，测试通过。

StateGraph Agent Loop verified.
```

这个实验与第一章最小 Loop 的执行目标相同。

但这次我们没有写：

```python
while True:
    ...
```

而是通过：

```python
builder.add_node(...)
```

声明执行步骤。

通过：

```python
builder.add_conditional_edges(...)
```

声明动态路由。

通过：

```python
builder.add_edge(...)
```

声明循环返回关系。

最后交给：

```python
graph.invoke(...)
```

执行。

### 16.1 这次实验验证了什么？

第一，**Node 是独立的执行单元。**

模型决策逻辑位于：

```python
model_node()
```

工具执行逻辑交给：

```python
ToolNode
```

第二，**Edge 决定执行关系。**

模型结束后不需要自己调用工具函数。

工具完成以后也不需要自己调用模型函数。

第三，**State 在节点之间传递消息。**

模型节点只返回本次的 AIMessage。

ToolNode 只返回本次的 ToolMessage。

Runtime 负责将它们合并为历史。

第四，**Runtime 负责循环调度。**

用户只调用一次：

```python
graph.invoke(...)
```

内部却可以执行多轮：

```text
Model → Tools → Model → Tools ...
```

第五，**工具执行确实产生了外部副作用。**

测试使用临时目录中的真实源码文件，并通过 `unittest` 检查结果。

### 16.2 实验没有证明什么？

必须明确：

`model_node()` 使用的是受控脚本。

它不是一个真正理解代码的语言模型。

因此，它不验证：

```text
LLM 的自主推理能力
模型是否能发现未知 Bug
模型是否能选择最优修复方案
```

它验证的是：

**LangGraph 对模型—工具循环的实际执行与状态传递机制。**

另外，示例没有配置 Checkpointer，因此不能据此认为 Agent 已经具备跨进程恢复能力。

示例依据所核对的 LangGraph 和 LangChain Core API 编写；当前环境未安装这些依赖，尚未完成本地运行验证。上述是预期执行轨迹，实际结果以兼容版本环境运行的断言为准。

---

## 十七、`tools_condition` 究竟是什么？

实验中我们没有自己写：

```python
def should_continue(...):
    ...
```

而是直接使用：

```python
from langgraph.prebuilt import tools_condition
```

为什么？

因为：

```text
如果最后一条 AIMessage 有 Tool Call
    -> 执行工具

否则
    -> 结束
```

是 Tool Calling Agent 的一个非常常见的路由规则。

因此 LangGraph 提供了预置实现。

打开：

`langgraph/prebuilt/tool_node.py`

找到：

```python
def tools_condition(
    state,
    messages_key="messages",
):
    ...
```

其核心代码为：

```python
if isinstance(state, list):
    ai_message = state[-1]

elif messages := state.get(
    messages_key,
    [],
):
    ai_message = messages[-1]

else:
    raise ValueError(...)

if (
    hasattr(ai_message, "tool_calls")
    and len(ai_message.tool_calls) > 0
):
    return "tools"

return "__end__"
```

这是按真实源码删去其他输入类型分支的节选。

这里的：

```python
"tools"
```

对应我们注册的：

```python
builder.add_node(
    "tools",
    tool_node,
)
```

而：

```python
"__end__"
```

就是：

```python
END
```

的内部标识。

### 为什么路由返回字符串，而不是直接执行工具？

因为 `tools_condition()` 的责任只有：

**决定去哪里。**

它不需要知道：

```text
工具参数怎样校验
工具怎样执行
Backend 在哪里
工具执行是否失败
```

这些属于 ToolNode 的职责。

这体现了 Graph 的核心思想：

**把执行动作与执行路由分离。**

---

## 十八、从源码看一次真正的 Graph 执行

现在我们已经有一个可执行 Agent Graph。

但可以继续往下一层看。

前面调用：

```python
graph = builder.compile()
```

得到：

```python
CompiledStateGraph
```

再调用：

```python
graph.invoke(...)
```

它内部需要推进 Graph 的执行阶段。

对于我们的退款任务，可以简化成：

```text
开始执行
    |
    v
调度 Model Node
    |
    v
Model Node 产生 AIMessage
    |
    v
更新 messages
    |
    v
根据 tools_condition 选择 Tools Node
    |
    v
调度 ToolNode
    |
    v
ToolNode 产生 ToolMessage
    |
    v
更新 messages
    |
    v
根据固定边返回 Model Node
    |
    v
再次执行 Model Node
```

在 LangGraph Runtime 中，这些执行被组织成一个个执行阶段。

你可以先把它理解为：

```text
一组当前符合执行条件的节点
        |
        v
执行这些节点
        |
        v
应用这一阶段产生的状态更新
        |
        v
决定下一阶段的节点
```

这也是为什么 StateGraph 可以扩展到：

```text
多个节点
多个条件分支
并行任务
中断
恢复
```

等场景。

但有两个边界必须说明。

### 18.1 Graph 并不自动解决所有状态冲突

假设两个节点同时更新：

```python
{
    "status": ...
}
```

如果没有合适的状态合并规则，仍然可能产生冲突或更新错误。

所以 Graph 需要与 State Schema、Reducer 和 Channel 协作。

### 18.2 Graph 并不自动保证外部副作用只执行一次

假设 ToolNode 修改了真实文件。

随后 Runtime 因为异常或恢复而重新执行相关节点。

外部文件修改是否能够安全重复，需要由具体工具设计保证。

因此：

**Graph 提供执行结构和调度机制，但它不是自动完成所有事务、安全和恢复问题的魔法层。**

这些将是后续章节研究的内容。

---

## 十九、现在回到 LangChain：`create_agent()` 做的是什么？

到这里，我们已经自己使用：

```python
StateGraph
```

构建了一个 Agent Loop。

那么 LangChain 的：

```python
create_agent(...)
```

是不是做了完全不同的事情？

不是。

它主要是在这个基础上提供更完整的 Agent 构建逻辑。

打开：

`langchain/agents/factory.py`

找到：

```python
create_agent(...)
```

其中创建：

```python
graph = StateGraph(
    state_schema=resolved_state_schema,
    input_schema=input_schema,
    output_schema=output_schema,
    context_schema=context_schema,
)
```

然后注册：

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

如果需要工具：

```python
graph.add_node(
    "tools",
    tool_node,
)
```

接下来配置条件边。

例如：

```python
graph.add_conditional_edges(
    loop_exit_node,
    RunnableCallable(
        _make_model_to_tools_edge(...),
        trace=False,
    ),
    model_to_tools_destinations,
)
```

以及：

```python
graph.add_conditional_edges(
    "tools",
    RunnableCallable(
        _make_tools_to_model_edge(...),
        trace=False,
    ),
    tools_to_model_destinations,
)
```

最后：

```python
graph.compile(...)
```

也就是说：

**LangChain 的 `create_agent()` 本质上是一个更完善的 Agent Graph Factory。**

它不需要重新发明 Graph Runtime。

它负责确定：

```text
使用什么 Model Node？
使用什么 ToolNode？
什么时候执行 Middleware？
哪些状态字段需要加入？
哪些条件决定继续或结束？
```

然后把这些规则交给 LangGraph。

---

## 二十、为什么 LangChain 不直接使用 `tools_condition`？

我们在实验中使用了：

```python
tools_condition
```

它的逻辑很简单：

```text
有 Tool Call
    -> Tools

没有 Tool Call
    -> END
```

但真实 LangChain Agent 的情况更加复杂。

例如：

- 某些工具设置 `return_direct=True`。
- Middleware 可能改变执行路由。
- 某些 Tool Call 属于结构化输出机制。
- 某些 ToolMessage 已经由 Middleware 产生。
- Agent 可能需要继续一轮模型处理，而不是直接执行新工具。

因此 LangChain 在：

```python
agents/factory.py
```

中使用更复杂的：

```python
_make_model_to_tools_edge(...)
```

和：

```python
_make_tools_to_model_edge(...)
```

来建立真实 Agent 的路由逻辑。

这里就体现了一个重要的设计演化：

```text
最小 Agent Loop
        |
        v
简单 tools_condition
        |
        v
支持多种执行策略
        |
        v
更复杂的 Conditional Edge
```

Graph 的结构并没有发生根本改变。

变化的是：

**路由规则越来越丰富。**

而这恰恰也是选择 Graph 的原因之一。

---

## 二十一、Graph 为什么是 Middleware 的重要基础？

现在我们重新回到最初的架构问题。

你的新理解是：

> Agent 的核心是一个 Agent Loop，各种能力在 Loop 的不同位置接入。

经过这一章，我们可以更准确地表达它。

一个最小 Graph：

```text
START
  |
  v
Model
  |
  +----> Tools
  |        |
  |        v
  +--------+
  |
  v
END
```

当我们希望：

```text
每次模型调用前检查上下文
```

可以在模型前接入处理。

当我们希望：

```text
模型调用结果需要审计
```

可以在模型调用前后包裹处理。

当我们希望：

```text
工具执行前检查权限
```

可以包裹 ToolNode 内部的工具调用。

于是得到：

```text
                    START
                      |
                      v
                 before_agent
                      |
                      v
                 before_model <-------------+
                      |                    |
                      v                    |
                  Model Node               |
                      |                    |
                      v                    |
                 after_model               |
                      |                    |
                      v                    |
                 Conditional Edge          |
                      |                    |
             +--------+--------+           |
             |                 |           |
             v                 v           |
            END              ToolNode -----+
```

这里仍然只是生命周期示意图。

真实 LangChain 有两类不同的扩展方式：

**Node 型扩展：**

例如：

```python
before_model
after_model
```

它们可以表现为 Graph 中的独立节点。

**Wrapper 型扩展：**

例如：

```python
wrap_model_call
wrap_tool_call
```

它们包裹具体的模型调用或工具调用，不一定表现为独立 Graph Node。

因此不要简单地认为：

> 所有 Middleware 都只是向 Graph 增加一个节点。

更准确的理解是：

> **Graph 提供明确的执行阶段与路由边界，Middleware 则可以通过新增节点、包装调用、扩展工具和 State 等方式接入这些边界。**

这是后面研究 LangChain Middleware 的基础。

---

## 二十二、StateGraph 和普通 while 循环的设计取舍

经过完整推导，我们现在可以回答：

**StateGraph 到底带来了什么？**

| 维度 | 普通 while 循环 | StateGraph |
|---|---|---|
| 执行步骤 | 直接写在控制代码中 | 注册为 Node |
| 执行顺序 | 由 Python 语句和条件控制 | 由 Edge 与 Runtime 控制 |
| 动态路由 | `if / elif` | Conditional Edge |
| 消息状态 | 开发者自己维护 | State Schema 与 Reducer |
| 多执行分支 | 手动协调 | Graph 可以显式表达 |
| 异步与并发 | 开发者自行管理 | Runtime 提供相应机制 |
| 扩展位置 | 修改循环主体或自行设计 Hook | 显式生命周期和包装机制 |
| 持久化恢复 | 需要自行设计 | 可结合 Checkpointer |
| 可观测性 | 需要自行埋点 | 可以基于节点、更新和事件观察 |
| 实现复杂度 | 小程序较简单 | 多了一层 Graph 与 Runtime 抽象 |

但不能因此得出：

```text
while 不好，Graph 一定更好
```

对于简单任务：

```python
model.invoke(...)
```

加少量工具调用，直接使用 Python 循环可能已经足够。

而当系统需要：

```text
更多执行阶段
多种工具
动态路由
Middleware
中断恢复
长时间运行
并行任务
```

时，Graph 的结构化执行模型会更有价值。

**Graph 是一种工程架构选择，而不是 Agent 的定义条件。**

---

## 二十三、重新理解这一章的核心演化过程

我们从最小 Agent Loop 开始：

```python
while True:
    ...
```

首先发现：

**问题一：模型调用和工具执行混在一起。**

因此引入：

```text
Node
```

把不同职责拆开。

接着发现：

**问题二：节点之间需要决定执行顺序。**

因此引入：

```text
Edge
```

然后发现：

**问题三：模型输出不同，下一步也不同。**

因此引入：

```text
Conditional Edge
```

再发现：

**问题四：不同节点需要共享前面产生的信息。**

因此引入：

```text
State
```

然后发现：

**问题五：节点只返回更新，怎样形成完整状态？**

因此需要：

```text
Reducer
```

最后发现：

**问题六：我们定义了这些结构，但由谁负责真正执行？**

因此需要：

```text
CompiledStateGraph
    |
    v
Pregel Runtime
```

最终形成完整的演化链：

```text
                 最小 while Loop
                       |
                       v
                拆分执行责任
                       |
                       v
                  Model / Tools
                       |
                       v
                     Nodes
                       |
                       v
                需要定义执行顺序
                       |
                       v
                     Edges
                       |
                       v
                 需要动态路由
                       |
                       v
                Conditional Edges
                       |
                       v
                需要跨节点传递数据
                       |
                       v
                     State
                       |
                       v
                 需要合并状态
                       |
                       v
                    Reducer
                       |
                       v
                构建 StateGraph
                       |
                       v
                    compile()
                       |
                       v
               CompiledStateGraph
                       |
                       v
                  Pregel Runtime
                       |
                       v
                 执行 Agent Loop
```

现在就能看清：

**LangGraph 并没有改变 Agent Loop 的基本原理。**

它改变的是：

**如何描述、组合和执行这个循环。**

---

## 二十四、本章总结

我们从一个很简单的问题开始：

> 为什么不用 `while True`，而要使用 StateGraph？

经过源码分析和实验，答案逐渐清晰。

普通 `while` 循环已经可以提供：

```text
模型决策
工具执行
结果反馈
循环终止
```

但当我们希望对执行过程进行更多工程化管理时，需要把：

```text
执行动作
路由逻辑
状态管理
运行时调度
```

分离开来。

LangGraph 提供：

```text
Node
Edge
Conditional Edge
StateGraph
CompiledStateGraph
Pregel Runtime
```

将它们重新组合为一个可执行的状态图。

因此，本章最重要的一句话是：

> **Agent Loop 定义了模型与工具之间的反馈关系；StateGraph 则将这种关系表达为一组独立节点、显式路由和共享状态，使循环能够被扩展、调度和管理。**

这一点尤其重要。

因为下一篇我们要研究：

**如何让一个稳定的 Agent Loop 在不同生命周期位置接入新的执行能力。**

而首先需要弄清楚的，就是 LangChain 如何利用 LangGraph 创建真实的 Agent Loop。

---

# 下一章：LangChain `create_agent()` 如何构建 Agent Loop？

本章我们自己编写：

```python
builder = StateGraph(MessagesState)
```

然后注册：

```python
builder.add_node("model", model_node)

builder.add_node("tools", ToolNode(tools))
```

最后：

```python
graph = builder.compile()
```

但 DeepAgents 的真实入口并不是要求开发者手动写出以上所有代码。

它调用：

```python
create_agent(...)
```

因此下一章要研究：

> **LangChain 的 `create_agent()` 究竟怎样将模型、工具、状态和路由规则，自动组装成一张能够运行的 Agent Graph？**

重点追踪：

```text
create_agent()
    |
    v
解析模型与工具
    |
    v
创建 StateGraph
    |
    v
构建 Model Node
    |
    v
构建 ToolNode
    |
    v
注册条件路由
    |
    v
graph.compile()
    |
    v
CompiledStateGraph
```

我们还会解释一个非常重要的设计细节：

**为什么 LangChain 在源码中区分 Agent 首次进入的位置和每轮循环重新进入的位置？**

这是理解 Middleware 生命周期的关键。

---

## 本章真实源码导航

### 1. LangGraph：StateGraph 构建器

文件：

`libs/langgraph/langgraph/graph/state.py`

建议按以下顺序阅读：

```text
StateGraph.__init__()
        |
        v
StateGraph.add_node()
        |
        v
StateGraph.add_edge()
        |
        v
StateGraph.add_conditional_edges()
        |
        v
StateGraph.compile()
        |
        v
CompiledStateGraph
```

源码：

https://github.com/langchain-ai/langgraph/blob/main/libs/langgraph/langgraph/graph/state.py

### 2. LangGraph：消息状态

文件：

`libs/langgraph/langgraph/graph/message.py`

重点：

```text
MessagesState
add_messages
```

源码：

https://github.com/langchain-ai/langgraph/blob/main/libs/langgraph/langgraph/graph/message.py

### 3. LangGraph：ToolNode 与条件路由

文件：

`libs/prebuilt/langgraph/prebuilt/tool_node.py`

重点：

```text
ToolNode
tools_condition
```

源码：

https://github.com/langchain-ai/langgraph/blob/main/libs/prebuilt/langgraph/prebuilt/tool_node.py

### 4. LangChain：真实 Agent Graph 构建

文件：

`libs/langchain_v1/langchain/agents/factory.py`

本章重点：

```text
create_agent()
    |
    v
StateGraph(...)
    |
    v
graph.add_node("model", ...)
    |
    v
graph.add_node("tools", ...)
    |
    v
graph.add_conditional_edges(...)
    |
    v
graph.compile(...)
```

源码：

https://github.com/langchain-ai/langchain/blob/master/libs/langchain_v1/langchain/agents/factory.py

### 5. DeepAgents：最外层 Agent 装配入口

固定版本：

`0.7.32`，Commit `e85c6b9`

文件：

`libs/deepagents/deepagents/graph.py`

重点：

```python
create_deep_agent()
```

以及：

```python
create_agent(...)
```

源码：

https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py

注意：DeepAgents 链接固定了 Commit；LangChain 和 LangGraph 链接指向当前主分支。正式逐行调试时，需要使用与 DeepAgents 0.7.32 实际安装依赖相对应的版本源码。

---

## 本章自检

读完以后，应该能够回答：

1. 为什么 `while True` 已经可以实现 Agent，却仍然需要 Graph？
2. Node 与 Edge 分别负责什么？
3. 为什么 Model Node 不应该自己调用 ToolNode？
4. 为什么模型到工具的边必须是条件边？
5. 为什么 Tools Node 执行完成后需要返回 Model Node？
6. State 怎样让不同节点共享执行历史？
7. 为什么节点只返回 Partial State Update？
8. `add_messages` 解决了什么问题？
9. `StateGraph` 与 `CompiledStateGraph` 有什么区别？
10. `compile()` 在执行前做了什么？
11. `Pregel Runtime` 与 Model Node 分别负责什么？
12. 为什么 Graph 是后续 Middleware 架构的重要基础？

如果这些问题已经清楚，那么下一章进入 LangChain 的 `create_agent()` 时，我们就不再只是观察源码中的函数调用，而能理解每一步装配背后的设计原因。
