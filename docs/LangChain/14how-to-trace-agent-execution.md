---
title: "13｜从 create_agent() 到 Runtime：完整追踪一次 Coding Agent 执行"
date: 2026-10-27
slug: /langchain-langgraph-source/how-to-trace-agent-execution
description: "LangChain × LangGraph 源码精读核心篇收束：沿退款精度问题的同一条执行链，贯通 create_agent、StateGraph、Pregel、ToolNode、Middleware、Channel 与 Checkpoint，并用无需 API Key 的真实 pytest 实验检验边界。"
tags:
  - LangChain
  - LangGraph
  - Agent
  - create_agent
  - Pregel
  - ToolNode
  - Checkpoint
  - 源码精读
  - Python
---

> **LangChain × LangGraph 源码精读 · 第 13 章（核心篇终章）**  
> 上一篇：[第 12 章｜不改 Agent Loop，怎样统一控制模型调用与工具执行？](./13how-to-add-agent-middleware.md)  
> **本章唯一主问题：** 当我们调用 `create_agent(...).invoke(...)`，究竟是谁、在什么时候，把一次模型提出的工具请求变为现实操作，并把结果交还下一轮模型？

前面十二章，我们已经分别认识了工具请求、消息反馈、Agent Loop、图构建、图编译、运行时调度、State、Reducer、Streaming、Checkpoint、人工审批和 Middleware。

但是知道每个零件有什么用，不等于知道一辆车是怎么动起来的。如果我们仍然需要翻着前十二篇，才能回答“模型返回一个 `tool_call` 后，接下来到底发生了什么”，那么这一系列还没有完成最重要的任务。

现在，我们只做一件事：**把镜头固定在同一次执行上，按真正的调用和数据先后关系，从 LangChain 一直追到 LangGraph Runtime，再回到模型。** 不为完整而再引入一套术语。源码里存在的其它分支，只在它们会改变这条执行路径时提示边界。

仍然使用贯穿全系列的任务：`refund.py` 用浮点数计算退款金额，测试要求精确的 `Decimal("0.30")`。Agent 应先运行 `pytest`，发现失败，得到允许后修复文件，再运行 `pytest` 确认测试通过。

这里特意加入一个拦截场景：脚本化模型的第一条请求试图修改 `../outside.py`。它**应该被 Middleware 拒绝，而且底层写文件工具一次也不能被调用**。这个小插曲能让我们实际看见几层代码之间的分工，而不只是把它们画在图上。

<!-- truncate -->

## 1. 先定义我们要观察的事实，而不是先摆出架构图

先把这次任务写成能够直接检查的输入与结果。

```python title="refund.py（实验初始文件）"
def refund_amount():
    return 0.1 + 0.2
```

```python title="test_refund.py（真实 pytest 用例）"
from decimal import Decimal
from refund import refund_amount


def test_partial_refund():
    assert refund_amount() == Decimal("0.30")
```

我们暂时不关心模型是否聪明。为了排除模型采样造成的变数，本章用**脚本化假模型**固定它的每次工具请求；它只决定下一步要调用哪个工具，不自动读懂项目，也不保证任何修复策略具有泛化能力。

这次执行要能提供以下可验证证据：

| 观察时刻 | 期望看到什么 | 能说明什么 |
| --- | --- | --- |
| 第一次模型响应 | `apply_fix('../outside.py')`，调用 ID 为 `call-0` | 模型只是提出请求 |
| 工具执行边界 | `denied: target not allowed`，无文件写入 | 拦截器阻止了实际工具 |
| 第二次模型响应 | `run_tests('test_refund.py')`，ID 为 `call-1` | 模型收到了上一轮工具反馈 |
| 第一次测试 | `pytest` 退出码为 `1` | 失败来自真实测试，而不是伪造输出 |
| 第三次模型响应 | `apply_fix('refund.py')`，ID 为 `call-2` | 合法的文件修改被执行 |
| 第二次测试 | `pytest` 退出码为 `0`，ID 为 `call-3` | 可以验证修复后的真实结果 |
| 最后一次模型响应 | 没有 `tool_calls` | 路由应走向结束而不是再运行工具 |

只要这些事实成立，我们就可以逐层追问：请求从何而来、谁执行工具、谁合并消息、谁选择下一步，以及保存点发生在哪里。

一个容易混淆的边界要先说清楚：**`pytest` 退出码为 1，并不一定是 Graph Runtime 失败。** 只要运行测试的工具正常完成、把 `exit_code=1` 作为工具结果返回，这仍然是一次成功执行的工具节点；模型反而需要这个失败反馈来继续解决业务问题。只有诸如工具抛出未被处理的异常、节点超时等情况，才涉及另一类运行失败。

## 2. 第一个入口不在 Runtime，而在 LangChain Agent Factory

上一章停在 `wrap_tool_call`。现在回到整条路径的最早位置：

```python title="入口：教学调用示意，非源码节选"
agent = create_agent(
    model=scripted_model,
    tools=[run_tests, apply_fix],
    middleware=[guard],
    checkpointer=checkpointer,
)
result = agent.invoke({"messages": [user_message]}, config=config)
```

我们看见的只有几行 API。但在真正执行前，`create_agent()` 已经做了至少四类工作：收集工具；组合模型与工具的调用包装器；为模型节点、工具节点建立执行入口；把节点及路由交给 `StateGraph` 编译。这些事情发生在**组装阶段**，不是在执行第一个工具请求的时刻。

打开固定 Commit 中的 [`langchain/agents/factory.py`](https://github.com/langchain-ai/langchain/blob/fbb8a35da7023ac0382c3b22f047337861bd3486/libs/langchain_v1/langchain/agents/factory.py)：

- `create_agent()` 为工具执行准备 `ToolNode`；源码还允许 Middleware 提供工具，或用工具调用包装器处理动态工具。
- 为本次最小案例，Factory 构建 `StateGraph`，注册 `model` 和 `tools` 两个主要执行节点。
- 条件分支负责区分“还有未处理的工具调用”和“当前模型输出已可结束”。
- 最后返回的是经过 `graph.compile(...)` 编译、再附加配置的可运行图，而不是一段立刻开始执行的 `while`。

**真实源码节选（LangChain 固定 Commit，省略周边语句）：**

```python
graph.add_node("model", RunnableCallable(model_node, amodel_node, trace=False))
```

这行的调用者是 `create_agent()`；输入是经过包装的模型节点函数；结果只是把可调用对象注册到图中。**这里不会调用模型。** 同理，`ToolNode(...)` 的创建只把工具及包装器登记起来，不会运行 `pytest`。

我们之前手写过 `while` 循环：程序员在循环里直接调用 `model()` 和 `tool()`。Factory 把这些动作变成**节点定义和路由规则**，使执行策略不再写死在那段业务循环里。

为什么需要这次转变？因为接下来 Runtime 还要在节点边界上做统一调度、状态读写、流式输出及可选的保存恢复。只有“模型调用”和“工具执行”成为可识别的执行单元，才存在这些明确的介入位置。**这不是说所有场景都必须用 Graph；简单的两步任务仍然可以只写普通 Python。**

### 一个需要纠正的简化印象：不是永远只有 `model → tools` 一条边

在这个固定版本，Factory 中的 [`_make_model_to_tools_edge()`](https://github.com/langchain-ai/langchain/blob/fbb8a35da7023ac0382c3b22f047337861bd3486/libs/langchain_v1/langchain/agents/factory.py#L1829) 会读取消息中的工具调用，排除已被对应 `ToolMessage` 处理过的请求，再把尚未处理的调用分派到 `tools`。

**真实源码节选：**

```python
return [Send("tools", [tool_call]) for tool_call in pending_tool_calls]
```

这一行是从**控制流**看整个 Agent 的关键证据。`Send` 表示将指定任务交给目标节点（本例是 `tools`）。如果某个模型响应一次提出多个工具请求，就不应武断地把它理解为“所有调用拼成一个顺序循环”。本章只安排每次一条工具请求，因此执行轨迹仍容易读懂。

这个路由器也不是工具执行器：**它决定把请求交给谁，本身不触碰文件。**

## 3. 第二个入口：编译结果为什么能够 `invoke()`？

`create_agent()` 在注册节点和分支后调用 `graph.compile(...)`。进入 LangGraph 固定版本 [`graph/state.py`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/graph/state.py)，可以看到构建器校验结构、装配节点和边，产生 `CompiledStateGraph`。

特别注意：`CompiledStateGraph` 的父类是 `Pregel`。这不是“图本身会执行”这一空泛比喻，而是具体的 Python 类型关系。**构建器负责定义一次运行中有哪些执行单元及其触发关系；`Pregel` 提供这些单元真正运行时的机制。**

沿源码继续追 [`pregel/main.py` 中的 `Pregel.invoke()`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/main.py#L3704)。固定版本的实现通过 `self.stream(...)` 取得图执行期间的输出，再根据调用时选择的模式整理返回值。

因此不能理解成：

> `invoke()` 调用模型、自己判断 `tool_calls`、自己调用工具，然后自己存 State。

这把多个层次错误地压缩到了同一个函数。实际分工是：

```text title="调用链的前半段：控制流示意图，非逐行源码"
调用方 agent.invoke(...)
            │
            ▼
CompiledStateGraph / Pregel.invoke()
            │
            ▼
Pregel.stream()
            │
            ▼
SyncPregelLoop + PregelRunner
            │
            ▼
准备节点任务 → 执行节点 → 收集写入 → 提交 → 下一步
```

`invoke()` 负责向调用方提供“本次运行的结果”这一接口；`stream()` 负责在运行过程中把执行信息组织为可消费的输出；而选哪些任务、执行哪些节点和提交何种写入，是更深一层的职责。到了这里，前面三章（编译、Pregel、Streaming）终于能在一条连续调用链上对齐。

## 4. 第三层：Runtime 先决定“轮到谁”，然后才执行

假设 `agent.invoke(...)` 的初始输入只有一条 `HumanMessage`。它需要先进入模型节点。我们不是去读取某个隐藏的“全局消息列表”，而是在每一步通过 Graph 的输入和触发关系安排任务。

进入 [`pregel/_loop.py` 的 `tick()`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/_loop.py#L626)：它通过 `prepare_next_tasks()` 根据当前检查点、Channel、触发关系等准备本轮任务；没有后继任务时，循环进入完成状态。

随后在 [`pregel/_runner.py` 的 `PregelRunner.tick()`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/_runner.py#L164)，Runner 执行这些任务，处理结果并把写入交给运行循环。

这解释了一个此前一直被口语化描述的事实：

**不是模型函数自己“跳进”工具函数；是模型节点的输出导致下一批工具任务变得可执行，Runtime 在合适的执行步调度它们。**

我们现在只追踪一个正常路径：模型节点 → 一个工具任务 → 模型节点。在有多个并行任务、失败重试或动态任务时，具体的执行拓扑更复杂；这些复杂性不会改变本章的基本责任分界。

## 5. 当轮到 model 节点时，真正调用模型的是谁？

回到 LangChain 的 [`factory.py` 模型执行部分](https://github.com/langchain-ai/langchain/blob/fbb8a35da7023ac0382c3b22f047337861bd3486/libs/langchain_v1/langchain/agents/factory.py#L1368)。我们可以把一个节点内部看成两段：准备请求，以及真正委托给模型。

- `model_node(state, runtime)` 读取当前消息 State，建立 `ModelRequest`，其中包含模型、可用工具、消息、Runtime 等当前所需信息。
- 如果配置了模型调用 Wrapper，节点会通过包装器委托实际调用；否则直接使用内部执行函数。
- 内部执行函数按当前请求绑定工具，再调用模型；模型响应随后变成能够提交给图的更新。

**真实源码节选：**

```python
output = model_.invoke(messages)
```

这才是模型调用发生的那个动作。前面的 `bind_tools` 是提供工具描述，不等于执行工具；后面的 `AIMessage.tool_calls` 是模型提出的请求，也不等于已经发生外部副作用。

对我们的任务，第一次模型返回的简化数据大致是：

```python title="第 1 次模型输出：结构示意，非真实源码"
{
    "type": "AIMessage",
    "content": "",
    "tool_calls": [{
        "id": "call-0", "name": "apply_fix", "args": {"target": "../outside.py"}
    ],
}
```

这条响应要能够被下一层使用，有两个最重要的条件：工具名和参数能表达意图；调用 ID 能用于与后来的 `ToolMessage` 对应。这里没有使用隐藏的字符串解析，也没有通过从自然语言里寻找“运行一下测试”来决定执行。

**这个节点的输出会进入 State 的更新路径，而不是直接在 `model_node` 内调用 `apply_fix()`。** 也正因如此，图可以先识别待执行的工具请求，再安排相应任务。

## 6. 工具没有被直接调用：先经过路由，再经过统一校验

第一轮模型请求的目标是 `../outside.py`。这是本章特意设置的失败场景：它可以是格式合法的工具调用，却不是一个应该被允许的文件写入。

Agent Factory 把对工具执行的公共包装器交给 `ToolNode`。打开 LangGraph 固定版本 [`prebuilt/tool_node.py`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/prebuilt/langgraph/prebuilt/tool_node.py)：`ToolNode` 能从节点输入解析工具请求，也会建立相应的运行时信息，然后通过单次调用处理路径执行工具。

在这条路径上：

```text title="一个请求的执行边界：控制流示意图"
AIMessage.tool_calls
       │
       ▼
Agent 条件路由 / Send("tools", ...)
       │
       ▼
ToolNode 获得 ToolCallRequest
       │
       ▼
Middleware.wrap_tool_call(request, handler)
    ┌──┴──────────────┐
    │                 │
  拒绝               放行
    │                 │
    ▼                 ▼
ToolMessage      handler(request)
                      │
                      ▼
                 BaseTool.invoke()
                      │
                      ▼
                 ToolMessage
```

**真实源码节选（`ToolNode._execute_tool_sync()` 中的执行动作）：**

```python
response = tool.invoke(call_args, config)
```

这是确实会进入工具执行的调用边界。但有包装器时，只有策略放行并实际调用委托函数，才会抵达这里。本例 `call-0` 被拒绝，底层 `apply_fix()` 根本不运行；工具节点仍然返回一条带 `call-0` ID 的拒绝消息。

这个差别十分重要：**拦截一次调用不意味着整个 Agent 必须抛异常中止**。拒绝结果可以像普通工具反馈一样返回给模型，后者改为提出一个合法请求。我们使用这一点，让假模型第二次请求 `run_tests('test_refund.py')`。

这里不能把路径白名单当作生产级安全沙箱。演示中的工具名和目标都是两个固定值，因此策略清楚可测；生产环境还要在真正执行器或隔离运行环境里实施权限限制，不能仅依赖模型消息或 Middleware 字符串判断。

## 7. 工具结果返回时，State 为什么不会立刻被下一节点随手修改？

当第二次请求运行 `pytest` 时，工具返回类似下面的内容：

```python title="示意 ToolMessage：测试失败仍可作为正常工具反馈"
{
    "type": "ToolMessage",
    "tool_call_id": "call-1",
    "content": "exit_code=1\n... test_partial_refund FAILED ...",
}
```

这里有两个关系必须同时成立。

第一个是**消息关联**：这个结果是对 `call-1` 的响应，不能误当成 `call-0` 的拒绝结果。否则模型无法判断究竟哪一次调用已经完成。

第二个是**状态提交**：节点返回的只是更新。框架并不是让两个节点拿着同一个字典随意写，而是把更新交到相关 Channel，并在执行步边界应用写入。我们在第 07、08 章已经验证了单字段和并发更新规则；这一章只用它来解释完整链路里的时序。

固定版本 [`pregel/_loop.py` 的 `after_tick()`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/_loop.py#L705) 收集本轮任务写入，然后调用 [`apply_writes()`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/_algo.py#L222) 应用更新。

这样一来，下一轮模型节点看到的消息历史才能稳定地包含上一轮模型提出的请求和对应工具返回的结果。

```text title="前后两步的观察点：状态流示意"
当前消息 State
      │
      ▼
model 节点返回 AIMessage(call-1)
      │
      ▼
状态提交；路由准备工具任务
      │
      ▼
tools 节点返回 ToolMessage(call-1, exit_code=1)
      │
      ▼
本轮写入提交至消息 State
      │
      ▼
下一个 model 节点读取包含失败反馈的 State
```

这幅图不是源码中的调度顺序日志。尤其在存在条件分支和多任务时，路由读写有特定实现边界；本章旨在说明**节点更新、Runtime 提交、后继读取**不能混成一个瞬时操作。

### 为什么不能把 `pytest` 失败简单当成 Agent 已失败？

现在已经能从完整调用链给出一个准确答案：

- 如果工具执行了真实 `pytest`，进程以退出码 `1` 结束，工具把这个退出码**作为返回内容**，`ToolNode` 的执行可以正常结束，消息也会正常写回；
- 下一轮脚本化模型看到 `exit_code=1`，决定调用 `apply_fix('refund.py')`；
- 若工具自身未被处理的异常打断运行，那才是另一个层级的失败，不保证产生同样的 `ToolMessage`。

因此“测试失败”是任务环境提供的诊断信号，不意味着 Pregel 调度失败，更不意味着要通过隐藏的无条件重试来修复代码。

## 8. Checkpoint 在哪一层出现？为什么它不替代消息 State？

假设我们只保留 `result['messages']`，运行结束后当然也能阅读完整轨迹。但如果希望在执行过程中暂停、恢复或者检查中途状态，就需要保存**执行进度**，不只是保存最终文本。

当调用方给 `create_agent(..., checkpointer=...)` 配置了保存器，并用同一条执行历史的 `thread_id` 调用图时，Runtime 可以通过保存器获得或记录检查点。固定版本 [`pregel/_loop.py`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/_loop.py#L1740) 中，执行上下文的进入路径会读取已有保存记录；执行步结束时，`after_tick()` 也会进入写入检查点的路径。

这不意味着每一条 `ToolMessage` 都有一个“立刻磁盘落盘且事务完成”的保证。持久化时机还受保存器与 durability 配置等因素影响。把某个存储策略的语义写成绝对的“节点完成就永久保存”是不准确的。

本次跟踪要区分三件不同的东西：

| 对象 | 它是什么 | 它不是什么 |
| --- | --- | --- |
| `messages` State | 模型和工具对话的业务状态 | 下一步调度完整元数据 |
| Checkpoint | 用于恢复/重放的图执行保存记录（包含状态及进度关联信息） | 外部文件系统的事务快照 |
| `thread_id` | 查找同一执行历史的配置键 | 访问控制凭证或沙箱边界 |

假如进程崩溃在真实写文件之后，却还没完成相应保存，那么重新恢复时仍可能面临副作用重放。**Checkpoint 解决执行进度恢复，不自动实现“真实文件修改恰好一次”。** 这也是前面人工审批章节要求副作用安排在明确边界之后，并考虑幂等性和版本检查的原因。

对于本章的示例，我们可以使用 `InMemorySaver` 观察保存点；但它在进程退出后就失去内存内容，不能拿它证明跨进程灾难恢复。

## 9. 把各个入口拼成一条真实可追踪的调用链

直到现在，读者已经知道每一层为什么出现，我们才适合给出整张执行图。

```text title="从 create_agent 到工具反馈的调用链汇总（概念结构图）"
      ┌──────────────── LangChain：构建阶段 ─────────────────┐
      │ create_agent(model, tools, middleware, checkpointer) │
      │       ├─ 组织工具、Model Wrapper、Tool Wrapper        │
      │       ├─ StateGraph：注册 model / tools / 分支         │
      │       └─ compile()：返回编译图                       │
      └───────────────────────┬──────────────────────────────┘
                              │
                              ▼
                  ┌── LangGraph：运行阶段 ──┐
                  │ Pregel.invoke / stream │
                  │    ┌──────────────┐    │
                  │    │ Loop + Runner│    │
                  │    └──────┬───────┘    │
                  │           │            │
                  │           ▼            │
                  │        model 节点       │
                  │           │            │
                  │       ModelRequest     │
                  │           │            │
                  └───────────┼────────────┘
                              ▼
                 模型真正 invoke，返回 AIMessage
                              │
                   State 更新 + 路由判定
                       /            \
                无待处理工具       有待处理工具
                    │                   │
                    ▼                   ▼
                   END           Send("tools", ...)
                                        │
                                        ▼
                                  ToolNode
                                        │
                               wrap_tool_call
                                  /     \
                                拒绝    放行
                                 │       │
                                 │    BaseTool.invoke
                                 │       │
                                 └───┬───┘
                                     ▼
                                ToolMessage
                                     │
                       Channel 写入 / after_tick 提交
                                     │
                            可选 Checkpoint 保存
                                     │
                                     ▼
                                 下一轮 model
```

这张图故意把**构建阶段**与**运行阶段**分开，也把**控制流**（路由、Send）、**数据流**（AIMessage / ToolMessage）和**状态提交**（Channel / Checkpoint）标在不同的位置。它是前十二章设计因果链的汇总，而不是建议读者从头背诵的最终架构。

现在再看“LangChain 与 LangGraph 的分工”，应该能够给出更精确的解释：**LangChain Agent Factory 决定构建什么样的 Agent 以及扩展点怎样组合；LangGraph 负责把这些定义变成受调度的状态化执行。** 工具最终执行的代码仍是实际的 `BaseTool` 及其下游函数，并不会被 Graph 自动变得安全或更智能。

## 10. 实验 A：用纯 Python 复现整条观察轨迹（无需 API Key，已运行）

我们先不假装已经装好框架。下面这份程序仅使用 Python 标准库和 `pytest`，复现本章最重要的外部可见行为：模型生成请求、公共策略拒绝、运行真实测试、工具反馈、部分更新提交、保存点快照、继续下一轮，直到结束。

它是**教学化简**，不是 LangChain / LangGraph 的任何一个真实类；其中的 `commit()` 只是为了让读者在屏幕上看到更新时机，**并不模拟 Channel 版本机制、并发 Pregel 调度或真实 Checkpoint 序列化格式**。

环境要求：Python 3.11+，`python -m pip install pytest`；将下面整段保存为 `trace_minimal.py`。

```python title="trace_minimal.py（已实际运行的教学化简）"
"""No-key teaching trace. This is NOT LangGraph's implementation."""
from __future__ import annotations

import copy
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

BROKEN = 'def refund_amount():\n    return 0.1 + 0.2\n'
FIXED = 'from decimal import Decimal\n\ndef refund_amount():\n    return Decimal("0.30")\n'
TEST = (
    'from decimal import Decimal\n'
    'from refund import refund_amount\n\n'
    'def test_partial_refund():\n'
    '    assert refund_amount() == Decimal("0.30")\n'
)


def scripted_model(messages: list[dict]) -> dict:
    last = messages[-1]
    if last['role'] == 'human':
        requested = ('call-0', 'apply_fix', '../outside.py')
    elif last['role'] == 'tool':
        result = last['content']
        if result.startswith('denied:'):
            requested = ('call-1', 'run_tests', 'test_refund.py')
        elif 'exit_code=1' in result:
            requested = ('call-2', 'apply_fix', 'refund.py')
        elif result == 'patch_applied':
            requested = ('call-3', 'run_tests', 'test_refund.py')
        elif 'exit_code=0' in result:
            return {'role': 'ai', 'content': '退款测试已通过', 'tool_calls': []}
        else:
            raise AssertionError(f'unexpected tool result: {result}')
    else:
        raise AssertionError(f'unexpected message: {last}')
    call_id, name, target = requested
    return {
        'role': 'ai', 'content': '',
        'tool_calls': [{'id': call_id, 'name': name, 'args': {'target': target}}],
    }


def run() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        (root / 'refund.py').write_text(BROKEN, encoding='utf-8')
        (root / 'test_refund.py').write_text(TEST, encoding='utf-8')
        state = {'messages': [{'role': 'human', 'content': '修复退款精度问题并运行测试'}]}
        checkpoints: list[dict] = []
        execution_log: list[str] = []
        tool_results: list[dict] = []

        def commit(new_messages: list[dict], step: str) -> None:
            state['messages'].extend(new_messages)
            checkpoints.append(copy.deepcopy(state))
            print(f'COMMIT {step}: messages={len(state["messages"])}')

        def execute_tool(call: dict) -> str:
            name, target = call['name'], call['args']['target']
            allowed = {'run_tests': 'test_refund.py', 'apply_fix': 'refund.py'}
            if allowed.get(name) != target:
                print(f'GUARD  {call["id"]}: denied')
                return 'denied: target not allowed'
            execution_log.append(f'{name}:{target}')
            if name == 'apply_fix':
                (root / target).write_text(FIXED, encoding='utf-8')
                print(f'TOOL   {call["id"]}: patch_applied')
                return 'patch_applied'
            result = subprocess.run(
                [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', target],
                cwd=root, capture_output=True, text=True, timeout=30, check=False,
            )
            print(f'TOOL   {call["id"]}: exit_code={result.returncode}')
            return f'exit_code={result.returncode}\n{result.stdout}'

        for round_no in range(1, 8):
            ai = scripted_model(state['messages'])
            commit([ai], f'model#{round_no}')
            if not ai['tool_calls']:
                print('ROUTE  END')
                break
            print('ROUTE  tools')
            outputs = []
            for call in ai['tool_calls']:
                content = execute_tool(call)
                msg = {
                    'role': 'tool', 'content': content,
                    'tool_call_id': call['id'], 'name': call['name'],
                }
                tool_results.append(msg)
                outputs.append(msg)
            commit(outputs, f'tools#{round_no}')
        else:
            raise AssertionError('loop did not finish')

        assert [x['tool_call_id'] for x in tool_results] == [
            'call-0', 'call-1', 'call-2', 'call-3'
        ]
        assert execution_log == [
            'run_tests:test_refund.py', 'apply_fix:refund.py', 'run_tests:test_refund.py'
        ]
        assert 'exit_code=1' in tool_results[1]['content']
        assert 'exit_code=0' in tool_results[3]['content']
        assert (root / 'refund.py').read_text(encoding='utf-8') == FIXED
        assert len(checkpoints) == 9
        assert len(state['messages']) == 10
        print(f'CHECK  snapshots={len(checkpoints)}; executed_tools={len(execution_log)}')
        print('PASS   all assertions')


if __name__ == '__main__':
    run()
```

我在隔离的临时目录中实际执行了上述代码。关键输出如下（已省略 `pytest` 的测试文件详细报告；`exit_code` 和状态断言来自真实运行）：

```text title="实验 A 的实际终端输出"
COMMIT model#1: messages=2
ROUTE  tools
GUARD  call-0: denied
COMMIT tools#1: messages=3
COMMIT model#2: messages=4
ROUTE  tools
TOOL   call-1: exit_code=1
COMMIT tools#2: messages=5
COMMIT model#3: messages=6
ROUTE  tools
TOOL   call-2: patch_applied
COMMIT tools#3: messages=7
COMMIT model#4: messages=8
ROUTE  tools
TOOL   call-3: exit_code=0
COMMIT tools#4: messages=9
COMMIT model#5: messages=10
ROUTE  END
CHECK  snapshots=9; executed_tools=3
PASS   all assertions
```

这段输出必须读出比“最后通过了”更多的信息。

**第一，模型的请求次数与真实工具执行次数不同。** `call-0` 虽然格式合法，但被 Guard 拦截，因此总共四条工具请求只发生了三次真实工具执行。把这四个请求都叫作“调用过一次 `apply_fix` 或 `run_tests`”会误导我们判断副作用。

**第二，模型一共进入五次。** 前四次提出工具请求；第五次不再附带工具请求，于是路由到结束。这解释了 Agent Loop 的终止条件。不要把 `pytest` 通过当成 Graph 的自动终止规则：是脚本化模型看见通过结果后，**决定不再调用工具**。

**第三，9 份 `checkpoints` 是教学快照，不是 LangGraph 真实保存器的“9 次持久化”证明。** 它们只用来检查每次 `commit()` 后的消息数量不断增长，且每条工具消息都关联了相应调用 ID。真正的 Checkpoint 何时创建、如何读取，已经由对应源码的保存器和 Runtime 决定。

实验跑通后，我们再从这个输出出发阅读框架，而不是拿一堆 API 说明去推测输出应该怎样。

## 11. 实验 B：用固定版本框架接口观察相同执行（可复现，尚未在本环境运行）

纯 Python 实验已经证明本章设计推理中的外部行为是可运行的。但是它没有证明 `create_agent()` 实际上怎样生成这些行为。要验证框架层，需要让真实 `ToolNode`、`StateGraph` 和 `Pregel` 接管同一条链。

下面的代码把上面的脚本模型改为 LangChain 的 `BaseChatModel` 实现，并接入 `create_agent()`、`wrap_tool_call`、`InMemorySaver` 和 `stream_mode="updates"`。为了防止 Python 或模型默认状态带来不可复现的行为，仍然使用相同的固定调用顺序、固定工具名、固定临时文件。

**重要：这个框架脚本在写作环境中只通过了 Python 语法检查，没有在安装了指定 LangChain / LangGraph 版本的环境里执行。** 因而以下的预期轨迹不是实测结果，也不构成跨仓库版本兼容性验证。

```python title="trace_langchain_agent.py（基于固定源码的框架复现示例，尚未运行）"
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import subprocess
import sys

from langchain.agents import create_agent
from langchain.agents.middleware import wrap_tool_call
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

BROKEN = 'def refund_amount():\n    return 0.1 + 0.2\n'
FIXED = 'from decimal import Decimal\n\ndef refund_amount():\n    return Decimal("0.30")\n'
TEST = ('from decimal import Decimal\n'
        'from refund import refund_amount\n\n'
        'def test_partial_refund():\n'
        '    assert refund_amount() == Decimal("0.30")\n')


class ScriptedModel(BaseChatModel):
    @property
    def _llm_type(self) -> str:
        return 'scripted-capstone-trace'

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        # 只适配工具绑定接口，不会真正推理，更不会在这里执行工具
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        last = messages[-1]
        if isinstance(last, HumanMessage):
            ai = self._request('call-0', 'apply_fix', '../outside.py')
        elif isinstance(last, ToolMessage):
            text = str(last.content)
            if 'denied:' in text:
                ai = self._request('call-1', 'run_tests', 'test_refund.py')
            elif 'exit_code=1' in text:
                ai = self._request('call-2', 'apply_fix', 'refund.py')
            elif 'patch_applied' in text:
                ai = self._request('call-3', 'run_tests', 'test_refund.py')
            elif 'exit_code=0' in text:
                ai = AIMessage(content='退款测试已经通过。')
            else:
                raise AssertionError(f'Unexpected tool result: {text}')
        else:
            raise AssertionError(f'Unexpected message: {type(last).__name__}')
        return ChatResult(generations=[ChatGeneration(message=ai)])

    @staticmethod
    def _request(call_id: str, name: str, target: str) -> AIMessage:
        return AIMessage(
            content='',
            tool_calls=[{'id': call_id, 'name': name, 'args': {'target': target}}],
        )


@wrap_tool_call
def guard(request, handler):
    allowed = {'run_tests': 'test_refund.py', 'apply_fix': 'refund.py'}
    call = request.tool_call
    if allowed.get(call['name']) != call['args'].get('target'):
        return ToolMessage(
            content='denied: target not allowed',
            name=call['name'],
            tool_call_id=call['id'],
        )
    return handler(request)


def main() -> None:
    with TemporaryDirectory() as folder:
        root = Path(folder)
        (root / 'refund.py').write_text(BROKEN, encoding='utf-8')
        (root / 'test_refund.py').write_text(TEST, encoding='utf-8')
        execution_log: list[str] = []

        @tool
        def run_tests(target: str) -> str:
            """Run the teaching pytest target."""
            execution_log.append(f'run_tests:{target}')
            proc = subprocess.run(
                [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', target],
                cwd=root, capture_output=True, text=True, timeout=30, check=False,
            )
            return f'exit_code={proc.returncode}\n{proc.stdout}'

        @tool
        def apply_fix(target: str) -> str:
            """Apply the pre-defined patch to the teaching file."""
            execution_log.append(f'apply_fix:{target}')
            (root / target).write_text(FIXED, encoding='utf-8')
            return 'patch_applied'

        saver = InMemorySaver()
        agent = create_agent(
            model=ScriptedModel(),
            tools=[run_tests, apply_fix],
            middleware=[guard],
            checkpointer=saver,
        )
        config = {
            'configurable': {'thread_id': 'capstone-demo-01'},
            'recursion_limit': 25,
        }

        # 真正进入 Pregel 运行，而不是调用我们写的 while
        for update in agent.stream(
            {'messages': [HumanMessage(content='修复 refund.py 并验证')]},
            config=config,
            stream_mode='updates',
        ):
            print('UPDATE', update)

        # 读取持久化配置关联的图状态，而不是从 print 日志猜最终结果
        snapshot = agent.get_state(config)
        replies = [
            m for m in snapshot.values['messages']
            if isinstance(m, ToolMessage)
        ]
        assert [m.tool_call_id for m in replies] == [
            'call-0', 'call-1', 'call-2', 'call-3'
        ]
        assert 'denied:' in str(replies[0].content)
        assert 'exit_code=1' in str(replies[1].content)
        assert 'patch_applied' in str(replies[2].content)
        assert 'exit_code=0' in str(replies[3].content)
        assert execution_log == [
            'run_tests:test_refund.py',
            'apply_fix:refund.py',
            'run_tests:test_refund.py',
        ]
        assert (root / 'refund.py').read_text(encoding='utf-8') == FIXED
        print('PASS: tool IDs, execution log, and final file checked')


if __name__ == '__main__':
    main()
```

在新的虚拟环境中准备依赖，并根据安装器解析结果确认各子包的实际版本：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install 'langchain==1.4.7' 'langgraph==1.2.17' pytest
python -m pip show langchain langchain-core langgraph langgraph-prebuilt langgraph-checkpoint
python trace_langchain_agent.py
```

Windows 环境需换用对应的虚拟环境激活命令。此处两个顶层包的版本与此前源码阅读基线保持一致；`langchain-core`、`langgraph-prebuilt` 等是独立发布的包，**不能仅根据仓库 Commit 号推定它们的发行版本**，因此特意给出 `pip show` 让读者记录实际解析情况。

执行时应重点检查的不是一串固定格式的 `UPDATE` 日志，而是三种相互独立的证据：

1. `stream_mode='updates'` 事件中可以看到哪些节点提交了更新，是否能区分模型输出和工具输出；
2. `agent.get_state(config)` 是否取回带完整消息关联的状态；
3. `execution_log` 是否只包含 **三次真实工具执行**，同时留下四条工具反馈（第一条是 Middleware 拒绝）。

如果指定版本在实际安装中遇到 API 或依赖差异，应先记录差异再更新示例，不能把本章的预期行为说成“已经在框架中验证通过”。

### 如果把 `invoke()` 和 `stream()` 都跑一遍，会发生什么？

要注意这不是无害的观察替换。上面示例已经运行完整次 Agent 并修改了临时文件；如果在同一状态、同一 `thread_id` 上再执行一次 `invoke()` 并重新传入人类消息，就可能产生一段**新的执行历史**。我们这里选择 `stream()` 完成一次运行，再通过 `get_state()` 获取结果，避免为了对比 API 而意外执行两次外部工具。

要学习 `invoke()` 的入口逻辑，直接阅读固定版本 [`Pregel.invoke()` 源码](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/main.py#L3704)；要观察运行中的更新，则使用相同 Agent 的 `stream()`。二者的调用形式不同，但不要把它们误认为两套不相关的运行引擎。

## 12. 容易读错的三个地方：用本次轨迹反证

### 12.1 “一次工具请求”并不等于“一次副作用”

证据是 `call-0`：请求格式合法，被路由到工具执行边界，但 Middleware 返回拒绝消息。我们没有执行 `apply_fix()`。工具请求、路由、Wrapper 调用与真实外部副作用，是四件不同的事情。

因此生产系统不能拿“模型会遵守提示词”替代执行前的授权检查，也不能拿“有一个 ToolMessage”就推断底层写操作已经成功。

### 12.2 “Graph 结束”并不等于“业务问题已解决”

本次测试真正通过，需要同时确认：`pytest` 返回码为 0，且我们关心的断言被执行。如果模型只是返回“已经修复”但没有运行测试，Graph 一样可能在没有工具请求时结束。**END 只是图控制流的结束位置，不是业务质量保证。**

类似地，Checkpoint 不自动撤销已经写入的 `refund.py`，Reducer 不自动处理外部写文件冲突，而 Middleware 也不自动拥有沙箱权限。这些机制解决的是不同层面的具体问题。

### 12.3 “Channel 有最新值”并不等于“当前任意节点都可即时读取”

我们在上一节用教学 `commit()` 打印了消息数量，但真实 Pregel 中有更明确的状态提交边界。普通节点任务产生写入，经 `after_tick()` / `apply_writes()` 生效，后续执行步才能看到提交后的相应状态。条件路由在本地读写视图上的处理是特殊路径，已经在第 07 章单独分析；不能因为路由能根据当前模型输出判定，就把它泛化成“所有节点共享一个实时可变字典”。

## 13. 回看整个核心系列：每一个机制为什么存在？

现在我们终于不必按 API 名称背诵架构，可以按同一条执行链上的问题回忆它们：

| 具体困难 | 最小解决机制 | 对应章节 |
| --- | --- | --- |
| 模型给出工具调用，但现实世界没有发生操作 | 请求与工具执行分离 | [01](./02how-to-execute-a-tool-call.md) |
| 工具返回后，模型没有下一轮输入 | 消息关联和反馈循环 | [02](./03how-to-build-agent-loop.md) |
| 手写循环难以复用统一的执行边界 | `create_agent()` 组织节点、工具与路由 | [03](./04how-to-create-agent.md) |
| 需要声明执行步骤及动态跳转 | `StateGraph` | [04](./05how-to-build-stategraph.md) |
| 定义好的图还不能被调度 | `compile()` 与可运行图 | [05](./06how-to-compile-stategraph.md) |
| 节点运行、状态提交、下一步选择需要执行者 | Pregel Loop + Runner | [06](./07how-to-run-pregel-loop.md) |
| 节点不能直接随意修改共享状态 | Channel 读写边界 | [07](./08how-to-read-write-state.md) |
| 并行节点写入同一字段出现冲突 | Reducer | [08](./09how-to-reduce-concurrent-state.md) |
| 只能在任务结束后看到最终结果 | Streaming | [09](./10how-to-stream-graph-execution.md) |
| 执行中断后丢失运行进度 | Checkpoint | [10](./11how-to-checkpoint-graph-state.md) |
| 修改实际文件前必须等人批准 | `interrupt()` / `Command(resume=...)` | [11](./12how-to-interrupt-and-resume-agent.md) |
| 多个工具都需要一致的执行检查 | Middleware | [12](./13how-to-add-agent-middleware.md) |
| 只知零件、不知端到端运行过程 | 本章完整执行链 | **13** |

现在回到开篇的中心问题：

> 当大模型决定调用工具时，这个决定究竟如何变成真实的程序执行？执行结果如何改变状态、驱动下一步决策，并最终形成一个可控制、可观察、可恢复的 Agent？

完整回答是：LangChain 先把模型、工具、扩展行为和路由规则组织成图；LangGraph 的编译结果提供运行入口，Pregel Runtime 选择并执行节点；模型产出的 `AIMessage.tool_calls` 经路由变成工具任务，`ToolNode` 在必要的校验与包装边界之后调用真实工具；工具返回 `ToolMessage`，在执行步的状态写入后成为下一轮模型的输入；模型不再提出工具调用时，图可以结束。若配置了 Checkpoint，Runtime 还可以保存执行状态以支持适当的恢复场景。

这段话里没有一个机制是为了“把架构做得更复杂”而出现的：每一层都有上一层所暴露的明确需求，也都只承担自己能承担的职责。

## 14. 核心篇结束后，真正值得深入的方向

到这里，第 01—13 章组成的**核心篇结束**。不是因为 LangChain / LangGraph 的源码已被“全部讲完”，而是因为贯穿案例中的一条正常执行链，我们已经可以从入口追踪到返回，并能解释每一层为什么存在。

接下来如果继续研究，不应为了列举高级组件而再次把内容写成“功能清单”。更自然的是从新的失败场景另开专题：当多个 Agent 协作并互相交接任务时怎样管理状态边界；当需要跨会话记忆时为什么不应把所有东西塞进消息 State；当外部工具具有不可撤销副作用时怎样构造幂等键、补偿和一致性策略；当大量写入导致历史增长时如何选择合适的存储与增量机制。

这些内容可以继续沿同一个退款修复任务展开，但**不是本次完整执行链的前置条件**。我们不会在本章中提前把它们全部介绍一遍。

最后给读者一个源码阅读练习：在固定版本中分别找到 `create_agent()`、`_make_model_to_tools_edge()`、`ToolNode._execute_tool_sync()`、`PregelRunner.tick()`、`PregelLoop.after_tick()`，只用五句话解释每处代码的输入、它自己完成的动作以及下游消费者。如果能清楚说出它们之间**哪里是控制流、哪里是消息数据、哪里是状态提交、哪里才真正发生副作用**，这十三章的核心目标就达到了。

---

## 固定源码索引与验证声明

### 固定阅读基线

- **LangChain：** `1.4.7`，仓库 Commit [`fbb8a35da7023ac0382c3b22f047337861bd3486`](https://github.com/langchain-ai/langchain/tree/fbb8a35da7023ac0382c3b22f047337861bd3486)。
- **LangGraph：** `1.2.17`，仓库 Commit [`4c52e129eb1ab69a3da58b799f67929f77d81149`](https://github.com/langchain-ai/langgraph/tree/4c52e129eb1ab69a3da58b799f67929f77d81149)。
- **跨包注意：** `langchain-core`、`langgraph-prebuilt`、`langgraph-checkpoint` 具有独立发行版本；这里使用的是固定仓库 Commit 中的源码文件，不宣称未实测的所有包版本组合已经通过集成验证。

| 源码文件 | 关键入口 | 在执行链中负责什么 |
| --- | --- | --- |
| [LangChain `agents/factory.py`](https://github.com/langchain-ai/langchain/blob/fbb8a35da7023ac0382c3b22f047337861bd3486/libs/langchain_v1/langchain/agents/factory.py) | `create_agent`、`model_node`、`_execute_model_sync`、`_make_model_to_tools_edge` | 组装图、真正请求模型、识别待执行工具调用 |
| [LangGraph `graph/state.py`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/graph/state.py) | `StateGraph.compile`、`CompiledStateGraph` | 从图定义生成可执行结构 |
| [LangGraph `pregel/main.py`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/main.py) | `Pregel.invoke`、`Pregel.stream` | 对外暴露运行/观察接口，进入运行引擎 |
| [LangGraph `pregel/_loop.py`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/_loop.py) | `tick`、`after_tick`、`__enter__` | 准备任务、提交本轮写入、处理检查点 |
| [LangGraph `pregel/_runner.py`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/_runner.py) | `PregelRunner.tick` | 执行本轮任务与结果处理 |
| [LangGraph `pregel/_algo.py`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/_algo.py) | `apply_writes` | 将本轮写入应用到 Channel 与运行状态 |
| [LangGraph `prebuilt/tool_node.py`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/prebuilt/langgraph/prebuilt/tool_node.py) | `ToolNode`、`_execute_tool_sync` | 承载真实工具执行与工具包装边界 |

### 哪些结果是真的跑过？

**已实际执行：** 本章实验 A，即 `trace_minimal.py`。运行环境的 Python 和 `pytest` 真正执行了两次测试，分别得到退出码 `1` 和 `0`；非法写入没有进入底层工具，3 次真实工具执行、4 条工具反馈、5 次脚本化模型输出与所有断言均通过。它用于验证可观察的设计行为，**不**用于证明 LangGraph 内部的精确执行顺序、Channel 版本规则或持久化语义。

**未实际执行：** 实验 B，即使用 `create_agent()`、`InMemorySaver`、`stream_mode='updates'` 的真实框架 API 路径。代码已通过 `python -m py_compile` 语法检查，但当前环境没有安装对应的 LangChain / LangGraph 依赖。文中提到的框架事件类型、方法职责和执行关系来自上述固定 Commit 源码核对，不能冒充集成运行日志。

**代码类型区分：** 实验 A 是可运行的教学化简；实验 B 是按真实 API 编写的可复现示例但尚未执行；标注“真实源码节选”的小段代码来自链接所指的固定 Commit，其余图示及数据结构是教学解释，不冒充项目逐行实现。
