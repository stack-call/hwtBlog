---
title: 引言：从 Agent Loop 到 Agent Harness
description: 以 Agent Loop 为核心，系统研究 LangChain、LangGraph 与 DeepAgents 的架构设计、Middleware 扩展机制及完整运行时实现
tags: [DeepAgents, Agent Loop, Agent Harness, Middleware, LangChain, LangGraph, 源码精读]
sidebar_position: 0
---

# DeepAgents 源码精读：从 Agent Loop 到 Agent Harness

## 引言：重新理解 Agent 的本质
:::tip
经过上一个版本的deepagent源码分析，我突然意识到，agent harness就是在整个agent loop上不断的添加各种控制，容错组件，核心是agent loop，在loop的不同位置增强机制，但是核心仍是整个loop循环。因此这一次读源码以agent loop为核心着眼点，看不同组件是怎么在各个位置增强agent loop的
:::

如果让大语言模型回答一个问题，我们通常只需要一次模型调用：

```python
response = model.invoke(
    "优惠券部分退款应该如何计算？"
)
```

模型根据输入生成答案，调用结束。

但如果用户提出：

> 请检查项目中的 `refund.py`，修复优惠券部分退款精度 Bug，运行相关测试，并确保没有引入新的问题。

事情就不同了。

模型首先需要读取代码。

读取后，可能发现还需要查看测试文件。

分析完测试文件，才有足够的信息决定怎样修改代码。

修改完成以后，需要运行 pytest。

如果测试失败，又需要根据新的错误信息继续调查。

整个过程可能是：

```text
                   用户任务
                      |
                      v
                   模型思考
                      |
                      v
                 选择下一步行动
                      |
                      v
                   执行工具
                      |
                      v
                  获得执行结果
                      |
                      v
                   模型再思考
                      |
                      v
                   再次执行工具
                      |
                      v
                     ...
                      |
                      v
                   完成任务
```

这里发生了一个重要变化。

模型不再只是一次性地生成答案，而是在不断获取新信息的过程中，决定下一步该做什么。

因此，理解 Agent 的第一步，不是研究它具备多少工具，而是理解这个持续运转的**决策—执行—反馈循环**。

我们将它称为：

**Agent Loop。**

---

## 一、Agent Loop 是整个系统的核心

一个最小的 Tool Calling Agent 可以抽象成：

```python
# 概念伪代码

while True:

    response = model.invoke(messages)

    messages.append(response)

    if not response.tool_calls:
        break

    for tool_call in response.tool_calls:

        result = execute_tool(tool_call)

        messages.append(result)
```

这段代码虽然简单，却包含 Agent 最基本的运行机制：

1. 模型观察当前上下文。
2. 模型决定下一步行动。
3. 系统执行行动。
4. 执行结果反馈给模型。
5. 模型根据新的信息再次决策。
6. 直到满足结束条件。

从这个角度来看，Agent 最基本的能力不是“拥有工具”，而是：

**能够将工具产生的外部反馈不断纳入后续决策。**

工具提供行动能力。

Loop 将这些行动组织成持续推进的任务。

但是，这样的最小循环距离一个真正可用的 Coding Agent 还很远。

---

## 二、为什么一个最小 Agent Loop 还不够？

假设我们已经实现：

```text
Model
  |
  v
Tools
  |
  v
Model
```

现在需要将它用于真实的软件开发任务。

我们会不断遇到新的工程问题。

### 问题一：模型上下文越来越长

Agent 执行几十轮后，可能积累大量文件内容和测试日志。

如果每次调用模型都发送完整历史，成本和上下文占用会持续增加。

因此，我们希望：

> 每次模型调用之前，能够检查并调整模型实际接收到的上下文。

### 问题二：工具执行可能存在风险

模型可能提出：

```python
execute("rm -rf ...")
```

或者修改重要的项目配置。

因此，我们希望：

> 在真正执行工具之前，能够检查权限、要求审批，必要时拒绝执行。

### 问题三：复杂任务可能缺少计划

模型可能反复检查相同的文件，忘记哪些步骤已经完成，或者在没有验证的情况下提前宣布任务成功。

因此，我们希望：

> 能够让 Agent 记录任务计划和进度，并将这些信息反馈给后续决策。

### 问题四：某些任务需要专业分工

例如修复退款 Bug 时，主 Agent 可能希望委派另一个 Agent 专门分析测试失败原因。

因此，我们希望：

> 允许主 Agent 将任务委派出去，并在子任务结束后取得结果。

### 问题五：任务可能被中断

Agent 可能需要等待审批，也可能由于运行环境重启而中断。

因此，我们希望：

> 保存执行状态，并在适当位置恢复任务。

这些问题有一个共同特点：

**它们都不要求我们重新发明 Agent Loop，而是要求我们增强循环执行过程中的某个环节。**

那么应该怎样实现这些增强？

---

## 三、从修改 Loop 到扩展 Loop

最直接的办法，是将所有能力写进一个巨大的循环：

```python
while True:

    check_context()

    load_memory()

    response = model.invoke(messages)

    update_plan()

    if response.tool_calls:

        for call in response.tool_calls:

            check_permissions(call)

            request_approval(call)

            result = execute_tool(call)

            save_artifacts(result)

    save_checkpoint()
```

刚开始，这似乎很合理。

但随着能力增加，问题就会出现：

- 模型调用与业务策略混在一起。
- 每增加一种能力都需要修改主循环。
- 不同能力的执行顺序越来越复杂。
- 某些能力只应该执行一次，某些却应该每轮执行。
- 不同 Agent 很难复用同一套扩展机制。

这意味着，我们需要把两件事分开：

**Agent Loop：决定模型与工具怎样循环。**

**扩展机制：允许外部组件在循环的特定位置改变执行行为。**

这就是 Middleware 设计的出发点。

---

## 四、Middleware：Agent Loop 的生命周期扩展机制

一个 Agent Loop 不只有模型调用和工具调用。

从运行过程来看，它存在多个自然的处理位置：

```text
                      START
                        |
                        v
                   before_agent
                        |
                        v
                   before_model  <----------+
                        |                   |
                        v                   |
                   Model Node               |
                        |                   |
                        v                   |
                   after_model              |
                        |                   |
                        v                   |
                 是否存在 Tool Call？       |
                        |                   |
             +----------+----------+        |
             |                     |        |
             v                     v        |
            否                     是       |
             |                     |        |
             v                     v        |
        after_agent             ToolNode     |
             |                     |        |
             v                     v        |
            END                执行工具 -----+
```

这是简化后的生命周期图。

其中，Model Node 内部还可以通过：

```python
wrap_model_call(...)
```

包装一次模型调用。

ToolNode 内部也可以通过：

```python
wrap_tool_call(...)
```

包装一次工具执行。

由此，我们就得到了一个核心架构：

```text
                 基础 Agent Loop
                        |
             +----------+----------+
             |          |          |
             v          v          v
          模型前处理  模型调用包装  工具调用包装
             |          |          |
             +----------+----------+
                        |
                        v
                   Agent 行为增强
```

例如：

- 上下文压缩可以在模型调用阶段处理。
- 工具审计可以包裹工具执行。
- 人工审批可以拦截敏感操作。
- 任务规划可以通过额外的 Tool 和 State 接入循环。
- 子 Agent 可以通过委派工具启动独立的执行过程。

但是这里需要特别区分：

**Middleware 是扩展 Agent Loop 的重要机制，却不是 Agent Harness 的全部。**

因为有些能力属于循环本身所依赖的底层基础设施。

---

## 五、Agent Loop、Middleware 与 Runtime 的关系

例如：

```python
read_file("/refund.py")
```

最终需要某个真实环境存储文件。

这属于 Backend 的责任。

又例如：

```python
state["messages"]
```

需要保存 Agent 的消息与执行状态。

这属于 Graph State 的责任。

再例如，Agent 被人工审批暂停后，需要恢复执行。

这属于 LangGraph Runtime 与 Checkpointer 的责任。

所以，更完整的关系应该是：

```text
                    Agent Application
                           |
                           v
                       Agent Loop
                           |
             +-------------+-------------+
             |             |             |
             v             v             v
         Model Node     ToolNode      Conditional
             |             |           Routing
             |             |             |
             +-------------+-------------+
                           |
                           v
                 Middleware Extensions
                           |
             +-------------+-------------+
             |             |             |
             v             v             v
          Context       Planning        HITL
          Skills        Subagents       Memory
             |             |             |
             +-------------+-------------+
                           |
                           v
                 Runtime Infrastructure
                           |
             +-------------+-------------+
             |             |             |
             v             v             v
           State         Backend      Checkpointer
             |             |             |
             +-------------+-------------+
                           |
                           v
                      LangGraph
```

这张图表达的是职责关系，并非严格的运行时调用顺序。

其中一些 Middleware 会声明工具或 State 字段；另一些会直接修改模型请求或拦截工具执行。

它们的实现方式不同，但共同目标是：

**在保持 Agent Loop 核心结构相对稳定的前提下，为它增加新的任务执行能力。**

因此，我们可以将本教程的中心认识概括为：

> **Agent Harness 是以 Agent Loop 为核心，通过生命周期扩展机制接入不同的行为策略，并由 State、Backend 和 Runtime 提供执行基础设施的系统。**

---

## 六、为什么选择 DeepAgents 作为研究对象？

理解上述架构之后，我们就可以提出一个新的问题：

> 一个真实的开源框架，是如何将这些机制组合起来的？

DeepAgents 很适合用来回答这个问题。

因为它的核心入口：

```python
create_deep_agent(...)
```

并没有独立实现一套全新的模型—工具循环。

它主要负责：

- 选择模型与系统指令。
- 配置文件系统工具和 Backend。
- 组装不同 Middleware。
- 按配置提供 Skills、Memory、Subagents 等能力。
- 配置 State、Checkpointer 和 Store。
- 将这些组件交给 LangChain 的 `create_agent()`。

接下来，LangChain 将它们编译成可以执行的 Agent Graph。

最终由 LangGraph Runtime 负责执行。

因此，整个调用关系是：

```text
                 create_deep_agent()
                         |
                         v
                装配 Agent Harness
                         |
             +-----------+-----------+
             |           |           |
             v           v           v
          Model       Middleware    Backend
             |           |           |
             +-----------+-----------+
                         |
                         v
                  create_agent()
                         |
                         v
                   解析 Middleware
                         |
                         v
                    构建 StateGraph
                         |
                         v
                    graph.compile()
                         |
                         v
                  LangGraph Runtime
                         |
                         v
                     Agent Loop
```

这也决定了我们的源码研究顺序。

**先理解 Loop，再理解扩展点，然后研究 DeepAgents 怎样使用这些扩展点。**

最后再深入支撑它运行的状态和执行基础设施。

这样学习，就不会将各个模块误认为互不相关的功能集合。

---

## 七、源码版本与研究边界

为了保证分析可复现，本教程固定使用：

```text
DeepAgents Python：0.7.32
Git Commit：e85c6b9
```

其中：

- DeepAgents 负责 Harness 装配与具体增强能力。
- LangChain `create_agent()` 负责 Agent Graph 的构建和 Middleware 集成。
- LangGraph 负责状态图执行、节点调度、状态更新、中断与恢复。
- LangChain Core 提供 Model、Message、Tool 等基础协议。

需要注意，LangChain 和 LangGraph 的具体实现必须以与该 DeepAgents 版本兼容的依赖源码为准，不能将不同版本的函数签名和执行机制随意混用。

还有一个版本相关的例子：

DeepAgents 0.7.32 默认不再自动安装 `TodoListMiddleware`。

如果希望使用 Todo Planning，需要显式配置。

因此，在研究源码时，我们必须明确区分：

**框架默认提供的能力**与**框架允许按需扩展的能力**。

---

## 八、统一的贯穿案例

为了避免每一章使用不同场景，整套教程使用同一个 Coding Agent 任务：

> 修复电商系统中的优惠券部分退款精度 Bug。

假设项目结构为：

```text
shop/
├── refund.py
├── coupon_service.py
└── tests/
    └── test_refund.py
```

任务目标：

1. 读取并理解当前退款逻辑。
2. 找到优惠券金额分摊的精度问题。
3. 修改相关代码。
4. 运行 pytest。
5. 根据测试结果继续修复。
6. 确认修复结果并回复用户。

在研究不同机制时，我们只改变系统实现，不改变任务背景。

例如：

```text
Agent Loop：
    为什么需要多轮模型与工具调用？

Middleware：
    如何在每轮模型调用前插入处理逻辑？

Context：
    测试日志太长怎么办？

Planning：
    如何避免遗漏验证步骤？

Subagents：
    如何委派测试分析？

HITL：
    修改重要文件前怎样等待批准？

State：
    文件修改与消息怎样进入状态？

Checkpoint：
    人工审批中断后怎样恢复？
```

通过同一个案例，我们将逐步观察：

**一个最小循环是怎样成长为完整 Coding Agent 的。**

---

# 全书目录

## 第一篇：Agent Loop 的基本原理与实现

这一篇先不研究复杂 Middleware。

我们要真正理解 Agent Loop 本身，包括它的必要组成部分，以及 LangChain 为什么使用 LangGraph 实现这个循环。

### 第 01 章：为什么需要 Agent Loop？

**核心问题：** 为什么一次 LLM 调用无法完成动态任务？普通固定工作流有什么局限？

研究内容：

- 从 LLM 调用到多步任务。
- 固定 Workflow 与 Agent 的区别。
- 决策、执行、观察和反馈。
- 为什么工具必须将结果反馈给模型。
- 最小 Agent Loop 的必要条件。
- 使用纯 Python 构建最小循环。

核心产出：

一个不依赖 LangChain 的最小 Agent Loop。

### 第 02 章：Agent Loop 的通信协议

**核心问题：** 模型怎样表达行动？系统怎样将执行结果与行动关联？

研究内容：

- `HumanMessage`、`AIMessage`、`ToolMessage`。
- `AIMessage.tool_calls` 的结构。
- Tool Call ID 与 ToolMessage 的关联。
- 模型提出行动与系统实际执行的区别。
- 工具执行失败怎样反馈给模型。
- 什么情况下循环结束。

核心源码：

```text
langchain_core/messages/
langchain_core/tools/
```

核心产出：

完整理解一次 Model → Tool → Model 的数据流。

### 第 03 章：为什么从 while Loop 演化到 StateGraph？

**核心问题：** 简单循环为什么不足以支撑真实 Agent？

研究内容：

- 普通 `while` 循环的局限。
- Node 与 Edge 的职责。
- Model Node、Tools Node。
- 条件路由与循环边。
- 多工具任务的分发。
- StateGraph 与 LangGraph Runtime 的区别。
- 构建阶段与执行阶段。

核心源码：

```text
langgraph/graph/state.py
langgraph/pregel/
```

核心产出：

使用 LangGraph 手动构建一个最小 Agent Loop。

### 第 04 章：LangChain create_agent 如何构建 Agent Loop？

**核心问题：** `create_agent()` 到底怎样把 Model 和 Tools 编译成 Graph？

研究内容：

- `create_agent()` 的整体构建流程。
- Model Node 的生成。
- ToolNode 的创建。
- Tool Calling 的条件路由。
- 工具完成后怎样返回 Model。
- 终止条件、结构化输出和特殊路由。
- Graph 编译与最终执行。

核心源码：

```text
langchain/agents/factory.py
langgraph/prebuilt/tool_node.py
```

核心产出：

沿真实源码走通一次 Agent Loop。

---

## 第二篇：Middleware——Agent Loop 的扩展机制

第一篇结束后，我们已经有一个能够运行的 Agent Loop。

但它还不能方便地添加上下文压缩、权限控制、日志和规划等能力。

第二篇研究的就是：

**怎样在不修改核心 Loop 的前提下扩展行为？**

### 第 05 章：为什么 Agent Loop 需要 Middleware？

**核心问题：** 为什么不能把所有处理逻辑直接写进 Model Node 或 ToolNode？

研究内容：

- 横切逻辑为什么会污染主循环。
- 生命周期扩展点的设计动机。
- `AgentMiddleware` 抽象。
- `before_agent` 与 `after_agent`。
- `before_model` 与 `after_model`。
- `wrap_model_call` 与 `wrap_tool_call`。
- Node Hook 与 Wrapper Hook 的本质区别。

核心源码：

```text
langchain/agents/middleware/types.py
```

核心产出：

为最小 Agent Loop 添加自定义 Middleware。

### 第 06 章：Middleware 怎样真正接入 Agent Loop？

**核心问题：** 定义一个 Middleware 方法后，LangChain 为什么知道何时调用它？

研究内容：

- `create_agent()` 怎样识别 Middleware Hook。
- Hook 方法重写检测。
- 如何将生命周期 Hook 编译成 Graph Node。
- Model Wrapper 的调用链。
- Tool Wrapper 的调用链。
- Middleware 顺序与嵌套关系。
- `state_schema` 如何扩展 Agent State。
- `middleware.tools` 如何扩展工具集合。
- `Command` 与 Middleware 状态更新。

核心源码：

```text
langchain/agents/factory.py
langchain/agents/middleware/types.py
langgraph/prebuilt/tool_node.py
```

核心产出：

完整理解 Middleware 从声明到运行的过程。

### 第 07 章：DeepAgents 如何装配 Agent Harness？

**核心问题：** `create_deep_agent()` 究竟在通用 Agent Loop 上增加了什么？

研究内容：

- `create_deep_agent()` 入口。
- Model、Tools、Backend 的初始化。
- 默认 Middleware 的创建。
- 可选 Middleware 的装配。
- Middleware 执行顺序。
- State Schema 与 Runtime 配置。
- DeepAgents 如何调用 LangChain `create_agent()`。
- 默认能力与可选能力的区别。

核心源码：

```text
deepagents/graph.py
deepagents/middleware/
```

核心产出：

一张准确的 DeepAgents 装配图。

---

## 第三篇：Loop 扩展能力的真实实现

现在我们已经知道：

- Agent Loop 如何执行。
- Middleware 为什么存在。
- Middleware 如何接入 Loop。
- DeepAgents 如何完成装配。

接下来才开始逐项研究实际功能。

但阅读顺序不再按照模块名称排列，而是按照它们扩展 Agent Loop 的方式组织。

### 第 08 章：模型输入如何被增强？——Context、Skills 与 Memory

**核心问题：** 模型每一轮调用前，需要看到哪些信息？

研究内容：

- State 与模型实际输入的区别。
- 为什么完整历史不应该每轮全部发送。
- SummarizationMiddleware 的触发和摘要。
- Skills 如何为任务提供专项指令。
- Memory 如何提供项目级或长期知识。
- 模型请求如何被修改。
- 摘要、检索与外部文件存储的关系。

核心源码：

```text
deepagents/middleware/summarization.py
deepagents/middleware/skills.py
deepagents/middleware/memory.py
```

核心产出：

理解 DeepAgents 如何组织每一轮模型的工作上下文。

### 第 09 章：工具执行如何被增强？——ToolNode、Filesystem 与 Backend

**核心问题：** 模型的工具调用怎样变成受控的真实执行？

研究内容：

- Tool 注册与 Schema。
- `middleware.tools`。
- ToolNode 的工具查找与分发。
- `ToolRuntime` 注入。
- `wrap_tool_call`。
- FilesystemMiddleware。
- StateBackend 与 FilesystemBackend。
- CompositeBackend。
- 大工具输出处理。
- 文件能力与 Shell 执行能力分离。

核心源码：

```text
deepagents/middleware/filesystem.py
deepagents/backends/
langgraph/prebuilt/tool_node.py
```

核心产出：

走通从 `read_file` Tool Call 到真实 Backend 操作的链路。

### 第 10 章：Loop 如何受到控制？——HITL、权限与中断

**核心问题：** 如果模型提出危险操作，系统怎样在实际执行之前阻止它？

研究内容：

- 模型行动意图与系统执行权限。
- Tool Wrapper 的拦截能力。
- HumanInTheLoopMiddleware。
- 审批规则与工具请求。
- `interrupt()`。
- 用户批准或拒绝后怎样继续。
- 工具错误、重试和执行边界。
- 为什么恢复执行需要考虑副作用。

核心源码：

```text
langchain/agents/middleware/human_in_the_loop.py
langgraph/types.py
langgraph/pregel/
```

核心产出：

实现一个运行测试前需要审批的 Agent。

### 第 11 章：复杂任务如何规划？——Planning 与 Todo

**核心问题：** Agent 已经能够循环，为什么还需要显式计划？

研究内容：

- Agent Loop 与 Planning 的区别。
- 为什么模型容易重复操作或遗漏步骤。
- Todo 作为外部任务状态的作用。
- `TodoListMiddleware`。
- `write_todos` 工具。
- Todo 如何写入 State。
- 任务状态如何重新进入后续上下文。
- Todo 与真正任务调度系统的区别。

核心源码：

```text
langchain/agents/middleware/todo.py
deepagents/graph.py
```

版本说明：

DeepAgents 0.7.32 中 TodoListMiddleware 需要按需配置，不再属于默认安装的 Middleware。

核心产出：

让退款任务具备可观察、可更新的执行计划。

### 第 12 章：一个 Agent Loop 如何调用另一个 Loop？——Subagents

**核心问题：** 为什么复杂任务需要任务委派，而不是让一个模型处理全部工作？

研究内容：

- 单个 Agent 长上下文与多职责问题。
- 主 Agent 和子 Agent 的职责分工。
- `task` 工具为什么是委派入口。
- SubAgentMiddleware 怎样注册委派能力。
- 子 Agent 的独立 Loop。
- 子 Agent 的模型、工具、State 与上下文。
- 子任务结果如何返回父 Agent。
- 同步与异步委派的区别。
- 子 Agent 的成本与隔离边界。

核心源码：

```text
deepagents/middleware/subagents.py
deepagents/middleware/async_subagents.py
```

核心产出：

实现一个主 Agent 委派子 Agent 检查退款测试的例子。

---

## 第四篇：支撑 Agent Loop 的运行时基础设施

前面研究了 Agent Loop 的多种增强能力。

但一个问题始终存在：

> 所有 Middleware 和 Tool 都可能产生状态更新，谁负责保存、调度和恢复这些更新？

第四篇回到 LangGraph Runtime。

### 第 13 章：Agent Loop 如何保持状态？——State、Reducer 与 Checkpoint

**核心问题：** Agent 为什么不是一个简单的 `while` 循环加全局字典？

研究内容：

- Agent State 的职责。
- Node Update 与状态合并。
- Reducer 和 Channel。
- 消息与文件的不同合并语义。
- DeepAgentState。
- DeltaChannel。
- Checkpointer 与 Thread。
- Interrupt/Resume 的恢复语义。
- State、Checkpoint 与 Store 的区别。
- 外部副作用与幂等性。

核心源码：

```text
deepagents/graph.py
deepagents/_messages_reducer.py
deepagents/backends/state.py
langgraph/graph/state.py
langgraph/channels/
langgraph/checkpoint/
```

核心产出：

让退款任务能够保存执行状态，并从中断位置继续。

### 第 14 章：如何观察一个 Agent Loop？——Streaming、Tracing 与调试

**核心问题：** Agent 执行了几十轮，我们怎样知道每一步真正发生了什么？

研究内容：

- Graph 节点执行事件。
- Streaming 输出。
- State Update 与模型 Token Stream。
- Tool Call 追踪。
- Middleware 执行顺序验证。
- LangSmith Tracing 的作用。
- 调试中断、工具失败与无限循环。
- Agent Loop 的基本测试策略。
- 如何区分模型错误、工具错误与 Runtime 错误。

核心源码：

```text
langgraph/pregel/
langchain/agents/factory.py
deepagents/graph.py
```

核心产出：

构建可以观察每一轮模型调用、工具执行和状态变化的调试环境。

---

## 第五篇：完整架构验证

### 第 15 章：从最小 Agent Loop 构建完整 DeepAgents

**核心问题：** 前面研究的机制怎样共同完成一项真实任务？

这一章不再独立介绍新机制，而是将之前全部代码串起来。

任务仍然是：

> 修复电商优惠券部分退款精度 Bug，并运行真实测试。

我们将分阶段构建 Agent。

#### 第一阶段：最小 Agent Loop

```text
Model
  |
  v
Tools
  |
  v
Model
```

只支持基本行动与反馈。

#### 第二阶段：接入文件系统

```text
Agent Loop
    |
    v
Filesystem Tools
    |
    v
Backend
```

让 Agent 能读取、修改真实项目。

#### 第三阶段：接入上下文管理

```text
Agent Loop
    |
    v
SummarizationMiddleware
```

控制长任务的模型输入。

#### 第四阶段：接入规划与任务委派

```text
Agent Loop
    |
    +-- Todo Planning
    |
    +-- Subagent Task
```

让 Agent 跟踪任务并委派独立调查。

#### 第五阶段：接入审批与恢复

```text
Agent Loop
    |
    +-- HITL
    |
    +-- Checkpoint
```

支持敏感操作审批和中断后恢复。

#### 第六阶段：加入可观测性

记录：

- 每次模型请求。
- 每次 Tool Call。
- 每次 Middleware 执行。
- State 的更新。
- 任务结束原因。
- 测试验证结果。

最终构建：

```text
                        User Task
                            |
                            v
                      create_deep_agent()
                            |
                            v
                       Agent Loop
                            |
                 +----------+----------+
                 |                     |
                 v                     v
             Model Node             ToolNode
                 |                     |
                 v                     v
          Model Middleware       Tool Middleware
                 |                     |
                 v                     v
          Context / Skills        Filesystem
          Memory / Summary        Backend / HITL
                 |                     |
                 +----------+----------+
                            |
                            v
                       Graph State
                            |
                            v
                       Checkpointer
                            |
                            v
                     LangGraph Runtime
                            |
                            v
                       Final Result
```

最后，我们会回答整套教程的核心问题：

**DeepAgents 到底是一个怎样的 Agent 系统？**

---

# 六、整套教程的源码阅读原则

本教程不是按文件顺序翻译源码，也不是单纯罗列功能。

每章都坚持相同的研究方式：

```text
实际任务遇到问题
        |
        v
为什么最简单的方案不够？
        |
        v
需要引入什么能力？
        |
        v
DeepAgents / LangChain 怎样设计？
        |
        v
真实源码在哪里实现？
        |
        v
一次调用实际怎样执行？
        |
        v
通过实验验证
        |
        v
分析设计收益与代价
```

阅读源码时，始终区分：

**设计动机**

为什么需要这项能力？

**编译与装配**

这项能力怎样被注册到 Agent？

**运行时执行**

它在 Loop 的哪个位置被调用？

**数据与状态**

它接收什么、返回什么、修改什么？

**架构权衡**

它为什么采用这种设计，又有哪些限制？

同时严格区分真实源码、教学化简代码和概念伪代码。

---

# 七、学习完成后的目标

完成这套教程后，我们不应该只知道：

```text
DeepAgents 有哪些 Middleware
```

而应该能回答更本质的问题：

1. 如果从零实现 Agent，最小 Loop 需要什么？
2. 为什么 LangChain 将 Agent Loop 编译成 Graph？
3. Middleware 如何影响 Graph 结构或包裹某次调用？
4. 为什么有些 Middleware 声明工具，有些修改 State，有些拦截执行？
5. DeepAgents 的增强能力如何复用同一套 Loop？
6. State、Backend、Checkpoint 为什么不能全部归类为 Middleware？
7. 一个新的 Agent 能力，应该接入 Loop 的什么位置？
8. 如何判断某项能力应当实现为 Middleware、Tool、Backend，还是 Runtime 功能？
9. 当 Agent 失败时，如何沿调用链定位问题？
10. 如何以相同架构自行设计新的 Agent Harness？

最终，我们希望真正掌握的是：

> **以 Agent Loop 为核心理解 Agent，以 Middleware 理解行为扩展，以 State 和 Runtime 理解执行过程，以 Backend 理解外部能力，并通过真实源码建立这些机制之间的完整联系。**
