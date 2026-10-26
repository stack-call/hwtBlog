---
title: LangChain 源码总览：仓库结构、核心模块与调用关系
description: 理解 LangChain Monorepo 中 langchain-core、langchain、langchain-classic、Provider Integrations 和 LangGraph 的职责与源码位置
tags: [LangChain, LangGraph, Agent, Middleware, 源码分析]
sidebar_position: 0
---

# LangChain 源码总览：先弄清楚代码库的结构

## 一、LangChain 不是一个单独的 Python 包

首先打开官方仓库：

https://github.com/langchain-ai/langchain

你会发现它不是一个普通的 Python 项目，而是一个 Monorepo。

所谓 Monorepo，就是多个相关的软件包放在同一个 Git 仓库中维护。

因此：

```python
from langchain.agents import create_agent
```

```python
from langchain_core.tools import BaseTool
```

```python
from langchain_openai import ChatOpenAI
```

虽然经常在同一个程序中使用，但它们实际来自不同的 Python 包，也可能拥有独立的版本。

我们先看仓库结构。

## 二、LangChain Monorepo 整体目录

以下是根据当前仓库整理的主要目录，不是完整文件清单。

```text
langchain/
│
├── libs/
│   │
│   ├── core/
│   │   └── langchain_core/
│   │       ├── language_models/
│   │       ├── messages/
│   │       ├── tools/
│   │       ├── runnables/
│   │       ├── prompts/
│   │       ├── output_parsers/
│   │       ├── callbacks/
│   │       └── ...
│   │
│   ├── langchain_v1/
│   │   └── langchain/
│   │       ├── agents/
│   │       │   ├── factory.py
│   │       │   ├── structured_output.py
│   │       │   └── middleware/
│   │       │       ├── types.py
│   │       │       ├── human_in_the_loop.py
│   │       │       ├── summarization.py
│   │       │       ├── model_retry.py
│   │       │       ├── tool_retry.py
│   │       │       └── ...
│   │       │
│   │       ├── chat_models/
│   │       ├── embeddings/
│   │       ├── messages/
│   │       └── tools/
│   │
│   ├── langchain/
│   │   └── langchain_classic/
│   │       ├── chains/
│   │       └── ...
│   │
│   ├── partners/
│   │   ├── openai/
│   │   │   └── langchain_openai/
│   │   │
│   │   ├── anthropic/
│   │   │   └── langchain_anthropic/
│   │   │
│   │   └── ...
│   │
│   ├── model-profiles/
│   ├── standard-tests/
│   └── text-splitters/
│
├── openwiki/
├── .github/
└── ...
```

对于我们当前的 DeepAgents 源码分析，最重要的目录只有三个：

```text
libs/core/
libs/langchain_v1/
libs/partners/
```

其中：

```text
core
    定义接口和数据类型

langchain_v1
    构建 Agent 和 Middleware 机制

partners
    实现具体模型 Provider
```

另外还有一个特别重要的项目：

```text
langgraph
```

但它**不在这个仓库中**。

它的地址是：

https://github.com/langchain-ai/langgraph

这是理解 LangChain 代码组织的第一步。

---

## 三、第一层：langchain-core，整个生态的基础抽象

源码位置：

```text
libs/core/langchain_core/
```

对应 Python 包：

```text
langchain-core
```

导入时：

```python
import langchain_core
```

这一层不会负责构建完整的 Agent Loop。

它主要定义整个 LangChain 生态都需要遵守的基础接口。

例如：

```text
                   langchain-core
                         |
         +---------------+---------------+
         |               |               |
         v               v               v
      Messages         Models           Tools
         |               |               |
         +---------------+---------------+
                         |
                         v
                     Runnables
                         |
                         v
                 Callbacks / Tracing
```

### 3.1 `messages/`：消息协议

重点源码：

[langchain_core/messages](https://github.com/langchain-ai/langchain/blob/master/libs/core/langchain_core/messages/__init__.py)

以及：

- `messages/base.py`
- `messages/ai.py`
- `messages/tool.py`

这里定义我们前几章反复使用的：

```python
HumanMessage
AIMessage
SystemMessage
ToolMessage
```

例如：

```python
from langchain_core.messages import (
    HumanMessage,
    AIMessage,
    ToolMessage,
)

user = HumanMessage(
    content="检查 refund.py"
)

assistant = AIMessage(
    content="",
    tool_calls=[
        {
            "name": "read_file",
            "args": {"file_path": "/refund.py"},
            "id": "call-1",
        }
    ],
)

result = ToolMessage(
    content="文件内容...",
    tool_call_id="call-1",
)
```

为什么 Message 放在 Core？

因为：

- LangChain Agent 要使用。
- LangGraph ToolNode 要使用。
- OpenAI Provider 要使用。
- Anthropic Provider 也要使用。
- DeepAgents 也要使用。

它不能只属于某一个 Agent 实现。

**Message 是跨框架共享的数据协议。**

### 3.2 `language_models/`：模型抽象

重点文件：

[language_models/chat_models.py](https://github.com/langchain-ai/langchain/blob/master/libs/core/langchain_core/language_models/chat_models.py)

关键类：

```python
BaseChatModel
```

我们自己写测试模型时：

```python
class MyModel(BaseChatModel):
    ...
```

使用的就是这里的协议。

`BaseChatModel` 规定模型可以通过统一接口被调用。

例如：

```python
model.invoke(messages)
```

或者：

```python
model.stream(messages)
```

但它不关心：

```text
为什么模型需要被调用第二次？
```

这是 Agent Loop 的问题，不属于 Model 抽象。

### 3.3 `tools/`：工具抽象

重点文件：

[tools/base.py](https://github.com/langchain-ai/langchain/blob/master/libs/core/langchain_core/tools/base.py)

关键类包括：

```python
BaseTool
StructuredTool
```

以及工具创建相关能力。

例如：

```python
from langchain_core.tools import tool

@tool
def inspect_refund() -> str:
    """Inspect refund logic."""
    return "Refund precision issue found."
```

这里定义的是：

```text
工具是什么
如何描述参数
如何调用函数
如何处理工具执行结果
```

但不会自动创建一个完整的 Agent Loop。

因此：

**BaseTool 与 ToolNode 是两个不同的东西。**

`BaseTool` 在 langchain-core。

`ToolNode` 的执行实现主要位于 langgraph。

### 3.4 `runnables/`：统一可执行对象协议

重点文件：

[runnables/base.py](https://github.com/langchain-ai/langchain/blob/master/libs/core/langchain_core/runnables/base.py)

核心类：

```python
Runnable
RunnableSerializable
```

这是理解 LangChain 整体设计非常重要的抽象。

例如：

```python
model.invoke(...)
```

```python
tool.invoke(...)
```

```python
chain.invoke(...)
```

为什么不同对象都支持类似的方法？

因为它们使用了统一的 Runnable 协议。

可以理解为：

```text
                       Runnable
                          |
         +----------------+----------------+
         |                |                |
         v                v                v
      Chat Model        Tool         RunnableSequence
         |                |                |
         +----------------+----------------+
                          |
                          v
                invoke / ainvoke
                stream / astream
                batch / abatch
```

注意，这是一张接口关系示意图，不表示所有这些类型都直接继承同一个具体实现类。

### 3.5 `callbacks/`：调用事件和追踪

这个目录提供通用的 Callback 接口和管理机制。

例如模型开始、模型结束、工具开始、工具结束等事件的处理。

这与我们之前研究的 Agent Middleware 不完全相同。

**Callbacks 更侧重调用过程的事件通知与观察。**

**AgentMiddleware 更侧重在 Agent 生命周期中修改或控制行为。**

两者都可以观察执行过程，但不是一套接口。

---

## 四、第二层：langchain_v1，Agent 构建与编排

现在进入我们真正关心的部分。

源码位置：

```text
libs/langchain_v1/langchain/
```

对应安装包：

```bash
pip install langchain
```

对应导入：

```python
import langchain
```

注意：

**目录叫 `langchain_v1`，但导入路径仍是 `langchain`。**

其核心目录是：

```text
langchain/
│
├── agents/
│   ├── __init__.py
│   ├── factory.py
│   ├── structured_output.py
│   │
│   └── middleware/
│       ├── __init__.py
│       ├── types.py
│       ├── human_in_the_loop.py
│       ├── summarization.py
│       ├── model_retry.py
│       ├── tool_retry.py
│       └── ...
│
├── chat_models/
│   └── base.py
│
├── embeddings/
├── tools/
└── ...
```

这里我们重点分析三个位置。

### 4.1 `agents/factory.py`

源码：

[Agent Factory](https://github.com/langchain-ai/langchain/blob/master/libs/langchain_v1/langchain/agents/factory.py)

它就是：

```python
from langchain.agents import create_agent
```

背后的实现。

这个文件负责：

```text
模型解析
    |
    v
工具收集
    |
    v
Middleware 分析
    |
    v
State Schema 合并
    |
    v
Model Node 构建
    |
    v
Tools Node 构建
    |
    v
Graph 条件路由
    |
    v
编译 StateGraph
```

也就是说：

**`factory.py` 是 LangChain Agent 的构建器。**

我们前面看到的一大堆手动解析 Middleware 的代码，就在这里。

它同时承担了 Agent Graph 的构建责任。

### 4.2 `agents/middleware/types.py`

源码：

[Middleware Types](https://github.com/langchain-ai/langchain/blob/master/libs/langchain_v1/langchain/agents/middleware/types.py)

这是 Middleware 协议的定义位置。

主要包括：

```python
AgentMiddleware
AgentState
ModelRequest
ModelResponse
ToolCallRequest
```

它定义：

```text
Middleware 可以声明什么？
Middleware 可以在哪些生命周期执行？
Middleware 接收什么参数？
Middleware 可以返回什么？
```

例如：

```python
class AgentMiddleware:
    tools = ...
    state_schema = ...

    def before_model(...):
        ...

    def wrap_model_call(...):
        ...

    def after_model(...):
        ...

    def wrap_tool_call(...):
        ...
```

这是简化接口，不是原样源码。

你可以将它理解为：

**`types.py` 定义 Middleware 协议。**

**`factory.py` 解释并编译 Middleware 协议。**

这正好对应你刚才发现的“LangChain 会手动取出 Middleware 字段”的现象。

### 4.3 `agents/middleware/` 下的具体 Middleware

这个目录除了 `types.py`，还包含 LangChain 自己实现的一些 Middleware。

例如：

```text
summarization.py
    长对话摘要

human_in_the_loop.py
    人工审批与中断

model_retry.py
    模型调用重试

tool_retry.py
    工具执行重试
```

因此这里又可以区分：

```text
                    AgentMiddleware
                           |
             +-------------+-------------+
             |                           |
             v                           v
         协议定义                   具体 Middleware
             |                           |
          types.py              summarization.py
                                human_in_the_loop.py
                                model_retry.py
                                ...
```

DeepAgents 的：

```python
FilesystemMiddleware
SubAgentMiddleware
SkillsMiddleware
```

也是按照这个协议实现的，只不过源码位于 DeepAgents 仓库。

### 4.4 `agents/structured_output.py`

源码：

[structured_output.py](https://github.com/langchain-ai/langchain/blob/master/libs/langchain_v1/langchain/agents/structured_output.py)

它负责 Agent 的结构化输出策略。

包括：

```python
ToolStrategy
ProviderStrategy
AutoStrategy
```

例如要求 Agent 最后返回符合指定 Schema 的数据。

它与普通 Tool Calling 有关联，但关注的是：

```text
如何约束或解析最终输出
```

而不是执行普通业务工具。

### 4.5 `chat_models/base.py`

源码：

[langchain/chat_models/base.py](https://github.com/langchain-ai/langchain/blob/master/libs/langchain_v1/langchain/chat_models/base.py)

这里的核心入口是：

```python
init_chat_model(...)
```

注意区分：

```text
langchain-core：
    BaseChatModel
    定义模型统一接口

langchain：
    init_chat_model
    根据配置选择并创建具体模型
```

例如：

```python
from langchain.chat_models import init_chat_model

model = init_chat_model(
    "openai:gpt-5.5"
)
```

实际调用还需要对应 Provider 依赖和 API 凭据。

---

## 五、第三层：partners，具体模型 Provider 实现

现在模型抽象和 Agent Factory 已经有了。

但是：

```python
BaseChatModel
```

只是接口。

具体是谁调用 OpenAI API？

谁调用 Anthropic API？

这些由 Provider 集成包负责。

例如：

```text
libs/partners/
│
├── openai/
│   └── langchain_openai/
│       ├── chat_models/
│       ├── embeddings/
│       └── ...
│
├── anthropic/
│   └── langchain_anthropic/
│       ├── chat_models/
│       └── ...
│
└── ...
```

以 OpenAI 为例：

源码：

[langchain_openai/chat_models/base.py](https://github.com/langchain-ai/langchain/blob/master/libs/partners/openai/langchain_openai/chat_models/base.py)

这里可以找到具体的：

```python
ChatOpenAI
```

其调用过程可以理解为：

```text
LangChain Model Node
          |
          v
BaseChatModel.invoke()
          |
          v
ChatOpenAI
          |
          v
Provider Request Encoding
          |
          v
OpenAI API
          |
          v
Provider Response Parsing
          |
          v
AIMessage
```

所以：

**LangChain Agent Factory 不需要自己知道 OpenAI API 的具体请求格式。**

它只需要通过 `BaseChatModel` 使用 Provider。

另外，并不是所有第三方集成都放在 `partners/` 下；许多 Provider 已经在独立仓库中维护。

---

## 六、第四层：LangGraph，实际执行 Agent 的 Runtime

现在我们已经有：

```text
langchain-core
    数据类型和接口

langchain
    Agent 构建器

partners
    具体模型
```

但是 Agent Loop 真正在哪里运行？

答案是：

**LangGraph。**

它是单独的仓库：

https://github.com/langchain-ai/langgraph

与本章最相关的目录如下。

```text
langgraph/
│
└── libs/
    │
    ├── langgraph/
    │   └── langgraph/
    │       ├── graph/
    │       │   └── state.py
    │       │
    │       ├── pregel/
    │       │   ├── main.py
    │       │   └── _loop.py
    │       │
    │       ├── channels/
    │       ├── checkpoint/
    │       └── ...
    │
    └── prebuilt/
        └── langgraph/
            └── prebuilt/
                └── tool_node.py
```

这里需要注意，目录树是筛选后的源码地图。

### 6.1 `graph/state.py`

定义：

```python
StateGraph
```

LangChain `create_agent()` 会用它来创建 Graph：

```python
graph = StateGraph(...)
```

然后：

```python
graph.add_node(...)
graph.add_conditional_edges(...)
```

### 6.2 `pregel/`

这里是 LangGraph 执行调度的重要部分。

例如：

```text
读取 State
    |
    v
决定可执行任务
    |
    v
调度 Graph Node
    |
    v
收集更新
    |
    v
通过 Channel / Reducer 更新 State
    |
    v
推进下一执行步骤
```

`CompiledStateGraph.invoke()` 最终依赖这套运行时完成执行。

### 6.3 `prebuilt/tool_node.py`

这是我们上一章研究的：

```python
ToolNode
```

的主要实现位置。

它负责执行模型产生的 Tool Call。

因此要记住：

**LangChain 创建 Tools Node，但通用 ToolNode 的实现来自 LangGraph。**

---

## 七、把四个部分连接成一次真正的源码调用

现在我们从 DeepAgents 的用户请求出发：

```python
agent = create_deep_agent(
    model=model,
)

agent.invoke({
    "messages": [...]
})
```

从仓库与源码职责角度，完整调用链如下：

```text
                     DeepAgents
                         |
                         v
                 create_deep_agent()
                         |
                         v
                 LangChain create_agent()
                         |
                         v
             agents/factory.py
                         |
          +--------------+--------------+
          |              |              |
          v              v              v
       Model          Tools         Middleware
          |              |              |
          v              v              v
    BaseChatModel      BaseTool     AgentMiddleware
     (core)             (core)       (langchain)
          |              |              |
          +--------------+--------------+
                         |
                         v
                    StateGraph
                    (langgraph)
                         |
              +----------+----------+
              |                     |
              v                     v
          Model Node             Tools Node
              |                     |
              v                     v
          ChatOpenAI              ToolNode
          (partners)             (langgraph)
              |                     |
              v                     v
          Model API              Tool.invoke()
              |                     |
              v                     v
           AIMessage              ToolMessage
              |                     |
              +----------+----------+
                         |
                         v
                    Graph State
                         |
                         v
                 Pregel Runtime
                         |
                         v
                    下一轮 / 结束
```

这就是 LangChain、langchain-core、Provider 和 LangGraph 的协作方式。

---

## 八、为什么 `create_agent()` 会显得特别庞大？

回到你前面发现的问题。

`factory.py` 为什么要手动解析 Middleware？

因为它不仅仅创建一个简单的：

```python
model.invoke(...)
```

而是要构建：

```text
StateGraph
    |
    +-- Model Node
    |
    +-- Tools Node
    |
    +-- Before Model Middleware Nodes
    |
    +-- After Model Middleware Nodes
    |
    +-- Wrapper Chains
    |
    +-- Conditional Routing
    |
    +-- State Schema
    |
    +-- Checkpointer
```

所以它既负责：

**静态编译工作：**

```text
提取 Middleware 声明
收集工具
合并 State Schema
识别生命周期 Hook
创建 Graph Node
建立条件边
```

又负责定义：

**运行时节点行为：**

```text
构造 ModelRequest
绑定工具
调用模型
处理模型结果
更新 Graph State
判断路由
```

这也是为什么 `factory.py` 看起来有大量逻辑。

从工程设计的角度，它把比较多的 Agent 构建职责集中在同一个文件中。

你可以将其理解为一个高度集中的 Agent Graph Builder。

---

## 九、一个非常容易混淆的目录：`libs/langchain/`

最后专门解释这个目录。

在当前 Monorepo 中：

```text
libs/langchain_v1/
```

对应：

```bash
pip install langchain
```

而：

```text
libs/langchain/
```

对应：

```bash
pip install langchain-classic
```

后者主要保留旧版的 Chain 等功能。

例如旧教程中经常出现：

```python
LLMChain
ConversationChain
RetrievalQA
```

这些属于早期 LangChain 的重要抽象。

但如果现在研究：

```python
from langchain.agents import create_agent
```

应该优先阅读：

```text
libs/langchain_v1/langchain/agents/
```

而不是去旧的 Classic Agent 实现中找对应逻辑。

**这一点非常重要，否则会把新旧两套 Agent 架构混在一起。**

---

## 十、我们应该按什么顺序读源码？

现在已经知道仓库结构，不建议从 `langchain-core` 的所有基础类开始逐行研究。

那样会花大量时间，却不容易看清 Agent 运行主线。

对于我们当前的 DeepAgents 系列，更合适的顺序是：

| 顺序 | 源码 | 为什么先读 |
|---|---|---|
| 1 | `langchain/agents/factory.py` | 找到 Agent Loop 如何构建 |
| 2 | `langchain/agents/middleware/types.py` | 明白 Middleware 协议 |
| 3 | `langchain_core/messages/` | 明白模型与工具消息 |
| 4 | `langchain_core/tools/base.py` | 明白工具接口 |
| 5 | `langgraph/prebuilt/tool_node.py` | 明白如何执行 Tool Call |
| 6 | `langgraph/graph/state.py` | 明白 Graph 如何组织状态 |
| 7 | `langgraph/pregel/` | 明白 Graph 如何执行 |
| 8 | `langchain_core/language_models/chat_models.py` | 明白模型统一接口 |
| 9 | `langchain/agents/middleware/` | 研究各种具体 Middleware |

注意这里不是在推荐先读最底层的抽象。

我们采用：

**先看 Agent 主流程 → 再根据调用关系进入 Core → 最后深入执行 Runtime。**

这样比较符合你之前研究 DeepAgents 时的思路。

---

## 十一、源码导航：看到一个类，应该去哪里找？

以后阅读 DeepAgents 时，可以直接使用这张表。

| 看到的类或函数 | 应该进入哪个仓库 |
|---|---|
| `create_deep_agent` | DeepAgents |
| `FilesystemMiddleware` | DeepAgents |
| `StateBackend` | DeepAgents |
| `create_agent` | LangChain |
| `AgentMiddleware` | LangChain |
| `ModelRequest` | LangChain |
| `AgentState` | LangChain |
| `ToolStrategy` | LangChain |
| `init_chat_model` | LangChain |
| `BaseChatModel` | LangChain Core |
| `AIMessage` / `ToolMessage` | LangChain Core |
| `BaseTool` / `StructuredTool` | LangChain Core |
| `Runnable` | LangChain Core |
| `ChatOpenAI` | LangChain OpenAI 集成 |
| `StateGraph` | LangGraph |
| `CompiledStateGraph` | LangGraph |
| `ToolNode` | LangGraph |
| `Command` / `Send` | LangGraph |
| `Checkpoint` / `Interrupt` | LangGraph |

这张表的价值是帮助你确定**源码属于哪个职责层**，避免在不同仓库之间盲目跳转。

---

## 十二、最后用一张图总结 LangChain 代码库

```text
                 Application / DeepAgents
                           |
                           v
                 langchain.create_agent()
                           |
                           v
        +------------------------------------+
        |             langchain              |
        |                                    |
        |  agents/factory.py                 |
        |  agents/middleware/types.py        |
        |  agents/structured_output.py       |
        |  chat_models/base.py               |
        +------------------------------------+
                           |
              +------------+------------+
              |                         |
              v                         v
    +-------------------+    +-------------------+
    |  langchain-core   |    |     partners      |
    |                   |    |                   |
    |  BaseChatModel    |    |  ChatOpenAI       |
    |  BaseTool         |    |  ChatAnthropic    |
    |  AIMessage        |    |  Other Providers  |
    |  ToolMessage      |    |                   |
    |  Runnable         |    +-------------------+
    +-------------------+             |
              |                       |
              +------------+----------+
                           |
                           v
                  CompiledStateGraph
                           |
                           v
        +------------------------------------+
        |             langgraph              |
        |                                    |
        |  StateGraph                        |
        |  ToolNode                          |
        |  Pregel Runtime                    |
        |  Channel / Reducer                 |
        |  Checkpoint / Interrupt            |
        +------------------------------------+
                           |
                           v
                    Agent Execution
```

**最终只需要记住四句话：**

1. `langchain-core` 定义消息、模型、工具、Runnable 等基础协议。
2. `langchain` 通过 `create_agent()` 把模型、工具、Middleware 编译成 Agent Graph。
3. `partners` 实现具体模型与外部 Provider 的集成。
4. `langgraph` 执行 Graph，并管理状态、节点调度、中断与恢复。

而 **DeepAgents 位于它们之上，负责装配更复杂的 Agent Harness 能力。**

## 建议收藏的源码入口

- [LangChain Monorepo 官方说明](https://github.com/langchain-ai/langchain/blob/master/libs/README.md)
- [LangChain Agent Factory](https://github.com/langchain-ai/langchain/blob/master/libs/langchain_v1/langchain/agents/factory.py)
- [LangChain Middleware Types](https://github.com/langchain-ai/langchain/blob/master/libs/langchain_v1/langchain/agents/middleware/types.py)
- [LangChain Core Runnable](https://github.com/langchain-ai/langchain/blob/master/libs/core/langchain_core/runnables/base.py)
- [LangChain Core ChatModel](https://github.com/langchain-ai/langchain/blob/master/libs/core/langchain_core/language_models/chat_models.py)
- [LangChain Core Tool](https://github.com/langchain-ai/langchain/blob/master/libs/core/langchain_core/tools/base.py)
- [LangGraph ToolNode](https://github.com/langchain-ai/langgraph/blob/main/libs/prebuilt/langgraph/prebuilt/tool_node.py)
- [LangGraph StateGraph](https://github.com/langchain-ai/langgraph/blob/main/libs/langgraph/langgraph/graph/state.py)

---

## 下一步：真正读懂 `AgentMiddleware` 的实现

有了这张源码地图，接下来我们应该直接进入：

```text
libs/langchain_v1/langchain/agents/middleware/types.py
```

然后追踪：

```text
AgentMiddleware 定义
       |
       v
create_agent() 发现 Middleware
       |
       v
解析 tools / state_schema
       |
       v
识别 before_model / wrap_model_call
       |
       v
创建 Graph Node 或 Wrapper
       |
       v
agent.invoke() 触发真实执行
```

这样就能够从源码彻底理解你刚才提出的疑问：

**LangChain 的 Middleware 到底是怎样从一个普通 Python 对象，被 `create_agent()` 编译成真实执行流程的？**
