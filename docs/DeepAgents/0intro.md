---
title: 引言：DeepAgents 源码整体架构与阅读路线
description: 从仓库目录、三层代码分工、构建流程和运行流程入手，建立 DeepAgents 源码阅读的全局地图
tags: [DeepAgents, LangChain, LangGraph, Agent Harness, 源码精读]
sidebar_position: 0
---

# 引言：DeepAgents 源码整体架构与阅读路线

## 一、我们要研究的不是一个函数，而是一套 Agent Harness

前面已经学习了 Agent Harness 的主要运行机制。

现在换一种方式：不再从概念推导架构，而是从 DeepAgents 的真实代码出发，看看这些机制究竟如何实现。

我们仍然使用同一个任务贯穿整个系列：

> 修复优惠券部分退款计算 Bug，必要时修改代码、执行测试、请求审批，并最终报告验证结果。

应用代码可能只有几行：

```python
from deepagents import create_deep_agent

agent = create_deep_agent(
    model="anthropic:claude-sonnet-4-6",
)

result = agent.invoke({
    "messages": [
        {
            "role": "user",
            "content": "修复优惠券部分退款 Bug，并运行测试",
        }
    ]
})
```

这里需要安装模型集成并配置凭据。另外，默认 `StateBackend` 不提供真正的 Shell 执行能力，因此不能仅凭上面几行代码就假定 Agent 可以运行 `pytest`。

我们真正感兴趣的是：

```python
create_deep_agent(...)
```

背后装配了什么？

```python
agent.invoke(...)
```

背后究竟执行了什么？

这是整个系列的两条主线。

---

## 二、先看 DeepAgents 的三个代码层次

DeepAgents 不是一个完全从零开发的 Agent Runtime。

它依赖 LangChain 和 LangGraph。

代码的主要分工如下：

```text
                      Application
                           |
                           v
                create_deep_agent()
                           |
                           v
  =================================================
                  DeepAgents
  =================================================
  负责 Harness 能力装配

  - Model 选择与 Harness Profile
  - Middleware 组合
  - Filesystem / Backend
  - Skills / Memory
  - Subagents
  - Permissions / HITL 配置
  - Context Summarization

                           |
                           v
                  create_agent()
                           |
                           v
  =================================================
                   LangChain
  =================================================
  负责构建 Agent 执行逻辑

  - Model Node
  - Tools Node
  - Agent Middleware
  - Tool Calling
  - Model Request / Response
  - Agent Loop 路由

                           |
                           v
                   StateGraph
                           |
                           v
  =================================================
                   LangGraph
  =================================================
  负责实际执行与状态管理

  - Graph Execution
  - State / Reducer
  - Checkpoint
  - Interrupt / Resume
  - Streaming
  - Task Scheduling

                           |
                           v
                     Agent Result
```

**这是阅读整个项目最重要的结构。**

例如：

如果想知道 `read_file` 是怎么实现的，应该从 DeepAgents 的 FilesystemMiddleware 开始。

如果想知道模型为什么执行完工具后又被调用一次，应该进入 LangChain 的 Agent Factory。

如果想知道工具结果如何写入状态、Graph 如何暂停与恢复，就要进一步阅读 LangGraph。

不要把三个项目的职责混在一起。

---

## 三、DeepAgents 仓库的核心目录

先看 Python SDK 中最值得阅读的文件。

以下是经过筛选的**源码阅读目录**，不是完整仓库树。

```text
deepagents/
|
+-- libs/
    |
    +-- ARCHITECTURE.md
    |   官方架构说明
    |
    +-- deepagents/
        |
        +-- deepagents/
            |
            +-- __init__.py
            |   公共 API 出口
            |
            +-- graph.py
            |   ★ 核心入口
            |   create_deep_agent()
            |   DeepAgentState
            |
            +-- _models.py
            |   模型解析
            |   resolve_model()
            |
            +-- _messages_reducer.py
            |   消息更新与合并
            |
            +-- backends/
            |   |
            |   +-- protocol.py
            |   |   Backend 能力协议
            |   |
            |   +-- state.py
            |   |   StateBackend
            |   |
            |   +-- filesystem.py
            |   |   FilesystemBackend
            |   |
            |   +-- store.py
            |   |   StoreBackend
            |   |
            |   +-- composite.py
            |   |   CompositeBackend
            |   |
            |   +-- sandbox.py
            |       Sandbox Backend 支撑代码
            |
            +-- middleware/
            |   |
            |   +-- filesystem.py
            |   |   文件工具及权限
            |   |
            |   +-- subagents.py
            |   |   子 Agent 委派
            |   |
            |   +-- async_subagents.py
            |   |   异步子 Agent
            |   |
            |   +-- summarization.py
            |   |   上下文压缩配置
            |   |
            |   +-- patch_tool_calls.py
            |   |   工具调用历史修补
            |   |
            |   +-- skills.py
            |   |   Skill 发现与加载
            |   |
            |   +-- memory.py
            |       Memory 注入
            |
            +-- profiles/
                模型或 Provider 对应的 Harness 配置
```

这份目录可以分成四个研究重点。

| 位置 | 主要研究什么 |
|---|---|
| `graph.py` | 整个 Agent 如何创建 |
| `middleware/` | Agent 的能力如何插入运行流程 |
| `backends/` | 文件、内存、命令执行在哪里发生 |
| `profiles/` | 不同模型如何调整 Harness 行为 |

其中最重要的是：

**`graph.py` 是装配入口，但不负责实现所有模块。**

因此我们的阅读顺序应该从 `graph.py` 开始，再沿着它导入和调用的具体模块逐步深入。

---

## 四、LangChain 与 LangGraph 的关键源码位置

只阅读 DeepAgents 本身还不够。

因为它把通用 Agent Loop 的构建交给 LangChain，把实际运行交给 LangGraph。

```text
langchain/
|
+-- libs/langchain_v1/langchain/
    |
    +-- agents/
        |
        +-- factory.py
        |   ★ create_agent()
        |   构建 Agent Loop
        |
        +-- middleware/
            |
            +-- types.py
                AgentMiddleware
                AgentState
                ModelRequest
                ToolCallRequest


langgraph/
|
+-- libs/langgraph/langgraph/
    |
    +-- graph/
    |   |
    |   +-- state.py
    |       StateGraph
    |
    +-- pregel/
        |
        +-- main.py
        |   Graph 执行入口
        |
        +-- _loop.py
            PregelLoop
            执行步进与状态推进
```

另外，工具执行还需要跟到：

```text
langgraph.prebuilt.tool_node
```

其中的 `ToolNode` 负责通用工具执行逻辑。

现在就可以确定三条核心源码入口：

```text
DeepAgents:
  deepagents/graph.py
       |
       v
  create_deep_agent()


LangChain:
  langchain/agents/factory.py
       |
       v
  create_agent()


LangGraph:
  langgraph/graph/state.py
  langgraph/pregel/main.py
       |
       v
  Graph Execution
```

接下来整个系列都会围绕这几条入口展开。

---

## 五、必须区分两个阶段：构建 Agent 与运行 Agent

理解 DeepAgents 的第一道难关，就是不要混淆构建阶段和执行阶段。

### 5.1 构建阶段：create_deep_agent()

当我们执行：

```python
agent = create_deep_agent(
    model=model,
    backend=backend,
)
```

主要发生的是：

```text
                create_deep_agent()
                         |
                         v
                 Resolve Model
                         |
                         v
                 Resolve Profile
                         |
                         v
                 Select Backend
                         |
                         v
                 Build Middleware
                         |
          +--------------+--------------+
          |              |              |
          v              v              v
     Filesystem     Summarization    Subagents
     Middleware       Middleware      Middleware
          |              |              |
          +--------------+--------------+
                         |
                         v
                 Optional Features
                         |
          +--------------+--------------+
          |              |              |
          v              v              v
        Skills          Memory          HITL
          |              |              |
          +--------------+--------------+
                         |
                         v
                LangChain create_agent()
                         |
                         v
                 Build StateGraph
                         |
                         v
                  Compile Graph
                         |
                         v
                CompiledStateGraph
```

这里主要在创建执行结构、工具集合和 Middleware 栈。

**它不是在执行退款修复任务。**

可以在 DeepAgents 的真实源码中看到：

```python
backend = backend if backend is not None else StateBackend()
```

随后构造以 `FilesystemMiddleware` 为起点的 Middleware 栈，再根据配置加入其他能力，最终调用 `create_agent()`。

### 5.2 运行阶段：agent.invoke()

只有当我们执行：

```python
result = agent.invoke({
    "messages": [...]
})
```

才会进入真正的 Agent Loop。

```text
                       agent.invoke()
                            |
                            v
                      LangGraph START
                            |
                            v
                     Load Graph State
                            |
                            v
                    Before-Model Hooks
                            |
                            v
                      Model Request
                            |
                            v
                       Model Node
                            |
                            v
                       AIMessage
                            |
                   +--------+--------+
                   |                 |
                   v                 v
             No Tool Calls       Tool Calls
                   |                 |
                   v                 v
               Final Answer      Tools Node
                   |                 |
                   |                 v
                   |          Tool Execution
                   |                 |
                   |                 v
                   |           ToolMessage
                   |                 |
                   |                 v
                   |          Update Graph State
                   |                 |
                   |                 +----+
                   |                      |
                   |                      v
                   |                 Model Node
                   |                      |
                   |                    ...
                   |
                   v
                 END
```

这张图是最核心的运行流程，省略了 Middleware 的具体条件路由、工具返回后的终止分支以及 Interrupt。

我们以后每一章都从这条流程中选一个位置深入。

---

## 六、用一小段真实源码把两条链连接起来

在 `deepagents/graph.py` 中，我们能找到核心构建逻辑。

例如：

```python
backend = backend if backend is not None else StateBackend()
```

和 Middleware 的组装：

```python
deepagent_middleware = [
    FilesystemMiddleware(
        backend=backend,
        custom_tool_descriptions=_profile.tool_description_overrides,
        permissions=permissions,
    ),
]
```

以上保留了真实源码中的关键语句和结构，省略了外围类型与条件分支。

随后增加：

```text
SubAgentMiddleware
ParentNotifierMiddleware
SummarizationMiddleware
PatchToolCallsMiddleware
...
```

最后将模型、工具、Middleware、状态配置等交给 LangChain：

```python
create_agent(...)
```

这里真正重要的不是记住所有 Middleware 名字。

而是看清楚：

```text
DeepAgents 选择运行能力
             |
             v
LangChain 构建执行流程
             |
             v
LangGraph 执行这些流程
```

这三层形成了一套完整的 Agent Harness。

---

## 七、我们要如何追踪一次真正的工具调用？

接下来所有章节都围绕同一个例子。

用户请求：

```text
修复优惠券部分退款 Bug。
```

模型决定读取文件：

```text
read_file("refund.py")
```

我们将沿着这条链路阅读：

```text
Application
     |
     v
agent.invoke()
     |
     v
LangGraph Runtime
     |
     v
Model Node
     |
     v
AIMessage(tool_calls)
     |
     v
Agent Routing
     |
     v
ToolNode
     |
     v
Filesystem Tool
     |
     v
FilesystemMiddleware
     |
     v
BackendProtocol
     |
     v
StateBackend / FilesystemBackend
     |
     v
File Result
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

注意，这是一条**逻辑调用链**，不是声称每个框架内部都严格按这份顺序进行 Python 函数调用；例如 Middleware 可能在工具注册时提供工具执行器，而不是在每次工具调用时都作为独立函数节点运行。

研究过程中，我们会同时回答：

1. Tool Call 是在哪里从模型响应中识别出来的？
2. ToolNode 如何查找与执行工具？
3. FilesystemMiddleware 怎样提供 `read_file`？
4. Backend 如何真正读取文件？
5. 为什么执行结果会变成 `ToolMessage`？
6. State 怎样把它保存下来？
7. Graph 为什么又开始下一次模型调用？

这样才能把之前的 Agent Harness 原理真正落到代码上。

---

## 八、推荐的阅读方式：先看整体，再逐步进入实现

直接打开 `factory.py` 的上千行代码，很容易陷入细节。

我们采用以下顺序：

```text
先看公开 API
    |
    v
找到负责装配的函数
    |
    v
找到它调用的底层接口
    |
    v
找到控制流关键分支
    |
    v
定位相关的 State / Tool / Middleware
    |
    v
用最小实验验证
    |
    v
回到总调用链
```

每一章固定研究四件事：

**第一：源码位置**

指出具体仓库、文件、函数或类。

**第二：调用关系**

明确谁调用它，它又调用谁。

**第三：关键实现**

展示少量真实源码，并解释参数、状态变化和关键分支。

**第四：运行验证**

使用真实运行时加一个可控制的模型或工具测试替身，验证关键行为。

不会只贴几十行源码，然后逐行翻译。

我们需要解释的是源码背后的运行机制。

---

## 九、后续章节的源码阅读路线

| 章节 | 主入口 | 要追踪的核心行为 | 对应理论章节 |
|---|---|---|---|
| 01 创建 Agent | `graph.py` | `create_deep_agent()` 如何完成装配 | [第十九章 构建 Harness](../Agent/19-build-agent-harness.md)、[第十六章 Model Adapter](../Agent/16-model-adapter.md) |
| 02 Agent Loop | `factory.py` | Model → Tools → Model 如何循环 | [第十章 Agent Loop](../Agent/10-agent-loop.md) |
| 03 Tool Calling | `tool_node.py`、Middleware | Tool Call 如何找到真实工具 | [第十一章 Tool System](../Agent/11-tool-system.md)、[第十三章 Permission](../Agent/13-permission-approval.md) |
| 04 Backend | `filesystem.py`、`backends/` | 文件实际存在哪里、如何执行 | [第十二章 Execution Environment](../Agent/12-execution-environment.md) |
| 05 State | `DeepAgentState`、LangGraph | ToolMessage 如何进入 State | [第六章 Checkpoint 与恢复](../Agent/06-checkpoint-resume.md) |
| 06 Context | `summarization.py` | 历史何时压缩、输出如何卸载 | [第一章](../Agent/1context.md)、[第二章](../Agent/2context.md)、[第七章 Context Assembly](../Agent/07-context-assembly.md) |
| 07 Planning | `TodoListMiddleware` | Todo 如何进入状态和上下文 | [第十四章 Planning](../Agent/14-planning-task-management.md) |
| 08 Subagents | `subagents.py` | `task` 如何启动子 Agent | [第四章 子 Agent](../Agent/agent-context-subagents-chapter-4.md) |
| 09 HITL | `HumanInTheLoopMiddleware` | 如何暂停、审批和恢复 | [第十五章 Human-in-the-loop](../Agent/15-human-in-the-loop.md) |
| 10 Skills / Memory | `skills.py`、`memory.py` | 指令和知识如何加载 | [第十七章 Hooks / MCP / Skills](../Agent/17-hooks-mcp-skills.md)、[第三章 记忆](../Agent/agent-context-memory-chapter-3.md) |
| 11 Observability | Graph Streaming / LangSmith | 如何观察模型和工具执行 | [第十八章 可观测性](../Agent/18-observability-tracing.md) |
| 12 全链路 | 以上全部 | 完整追踪优惠券退款任务 | 全部 |

这份顺序不是按照目录字母排序，而是按照**一次 Agent 请求的真实生命周期**安排。

最后一列不是装饰。它规定了每一章的**入口问题从哪里来**——见下一节。

---

## 九·补、源码分析为什么必须带着理论问题读

这套教程和 Agent Harness 专题是同一个任务的两种视角：

| | Agent Harness 专题 | 本专题 |
|---|---|---|
| 起点 | 一次具体故障 | 一个具体函数 |
| 推进方式 | 事故 → 改进 → 再出事故 | 疑问 → 定位 → 读调用链 |
| 产出 | 知道**为什么**需要这个机制 | 知道这个机制**长什么样** |
| 缺失 | 不知道工业实现有多少妥协 | 不知道这段代码**为谁而写** |

只读源码，最容易落入一个陷阱：**把实现当成设计。**

看到一个 `try/except` 就以为错误处理就该这么写，看到一个 `if` 分支就以为这是唯一解。实际上源码里的每一处结构，几乎都是某个具体故障逼出来的补丁。不知道那个故障，就分不清「必须这样」和「当时只能这样」。

所以本专题的每一章都遵循同一个三段式：

**第一段：先提出理论问题。**

不是「`wrap_tool_call` 在哪一行」，而是「工具执行失败时，Agent 该不该停下来」。这个问题在 [第十章 Agent Loop](../Agent/10-agent-loop.md) 里已经用事故推过一遍，这里只需要回忆结论。

**第二段：再看源码怎么回答。**

带着问题读代码，才能看出哪些分支是在处理真实世界的复杂性。同样的十行代码，没问题是「参数校验」，有问题是「防止模型把相对路径写到宿主机根目录」。

**第三段：最后对比理论与实现。**

这一步最容易被跳过，但价值最高。理论给出的是**应该有的边界**，源码给出的是**实际付出的代价**。两者不一致的地方，往往就是这个系统最值得学的地方。

> [!TIP] 读源码时唯一值得记的笔记
> 不要记「这个函数做了什么」，记「如果没有这段代码，会出现什么故障」。
> 前者是文档能给的，后者只有源码能给。

后面每一章会尽量做到三件事：开头点明这一章对应哪个理论问题并链回原章节；关键代码处说明它对应哪一次故障；章末用一张表列出「理论怎么说、源码怎么做、差在哪里」。

如果某一章的理论前提你记不清了，先回去读对应的理论章节，再回来。**顺序反了，源码会变成一堆没有动机的语句。**

---

## 十、开始阅读之前，先记住三个结论

**第一个结论：DeepAgents 的主要源码入口是 `create_deep_agent()`，但通用 Agent Loop 不在这里实现。**

它主要负责组装一套有默认行为的 Agent Harness。

**第二个结论：LangChain 的 `create_agent()` 构建执行图，LangGraph 负责运行这张图。**

理解两者的分工，就能找到 Model Node、Tools Node、State 和 Checkpoint 的代码位置。

**第三个结论：工具能否被模型使用、调用能否被授权、操作实际在哪里执行，是三个不同问题。**

它们分别涉及 Tool / Middleware、Permission 和 Backend。

这也是后面阅读 DeepAgents 源码时最需要保持清晰的边界。

---

## 本引言的最终源码地图

```text
                  你的应用代码
                       |
                       v
               create_deep_agent()
                       |
                       v
      +---------------------------------+
      |             DeepAgents          |
      |                                 |
      |  graph.py                       |
      |  middleware/                    |
      |  backends/                      |
      |  profiles/                      |
      +---------------------------------+
                       |
                       v
                 create_agent()
                       |
                       v
      +---------------------------------+
      |             LangChain           |
      |                                 |
      |  agents/factory.py              |
      |  agents/middleware/types.py     |
      |  ToolNode                       |
      +---------------------------------+
                       |
                       v
                 Compiled Graph
                       |
                       v
      +---------------------------------+
      |             LangGraph           |
      |                                 |
      |  graph/state.py                 |
      |  pregel/main.py                 |
      |  pregel/_loop.py                |
      |  Checkpoint / Interrupt         |
      +---------------------------------+
                       |
                       v
                   Agent Loop
                       |
               +-------+-------+
               |               |
               v               v
           Model Node       Tools Node
               |               |
               +-------+-------+
                       |
                       v
                    Graph State
                       |
                       v
                  下一轮或结束
```

现在我们已经知道这套代码的大体分布，也知道怎样从应用入口追踪到真正的执行循环。

下一章正式从 `deepagents/graph.py` 的 `create_deep_agent()` 开始，逐段分析它如何选择 Model、Backend、Profile、Middleware 和 Subagent，最终生成可执行的 LangGraph。

## 源码入口

- [DeepAgents 官方架构文档](https://github.com/langchain-ai/deepagents/blob/main/libs/ARCHITECTURE.md)
- [DeepAgents graph.py](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/graph.py)
- [DeepAgents Middleware](https://github.com/langchain-ai/deepagents/tree/main/libs/deepagents/deepagents/middleware)
- [DeepAgents Backends](https://github.com/langchain-ai/deepagents/tree/main/libs/deepagents/deepagents/backends)
- [LangChain Agent Factory](https://github.com/langchain-ai/langchain/blob/master/libs/langchain_v1/langchain/agents/factory.py)
- [LangChain Middleware Types](https://github.com/langchain-ai/langchain/blob/master/libs/langchain_v1/langchain/agents/middleware/types.py)
- [LangGraph StateGraph](https://github.com/langchain-ai/langgraph/blob/main/libs/langgraph/langgraph/graph/state.py)
- [LangGraph Pregel Loop](https://github.com/langchain-ai/langgraph/blob/main/libs/langgraph/langgraph/pregel/_loop.py)

正式逐行研究时，建议将 DeepAgents、LangChain、LangGraph 的仓库版本固定，避免不同版本的源码和运行环境不对应。
