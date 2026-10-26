---
title: 第一章：从 create_deep_agent 进入源码
description: 通过真实源码和离线实验，理解 DeepAgents 如何装配 Middleware、Backend，并通过 LangChain 和 LangGraph 实现 Agent Loop
tags: [DeepAgents, LangChain, LangGraph, Agent Harness, 源码分析]
sidebar_position: 1
---

# 第一章：从 create_deep_agent 进入源码——一个 Deep Agent 究竟是怎样创建的？

## 一、先从四行代码开始

假设我们仍然要开发一个 Coding Agent。

用户提出：

> 帮我修复优惠券部分退款计算错误，先检查代码，再修复并验证。

使用 DeepAgents，我们可以这样创建 Agent：

```python
from deepagents import create_deep_agent

agent = create_deep_agent(
    model="anthropic:claude-sonnet-4-6",
)

result = agent.invoke({
    "messages": [
        {
            "role": "user",
            "content": "帮我修复优惠券部分退款 Bug",
        }
    ]
})
```

这是实际的公开 API 调用方式，需要安装对应依赖并配置模型凭据。

但这里有一个问题。

我们只调用了：

```python
create_deep_agent(...)
```

并没有自己编写：

```python
while True:
    response = llm(...)
    ...
```

也没有写：

```python
registry.register("read_file", ...)
```

更没有手动添加：

```python
summarize_context(...)
```

可是 DeepAgents 却具备文件工具、上下文压缩、子 Agent 等能力。

**这些能力究竟在哪里被加入的？**

这就是我们第一章要追踪的问题。

---

## 二、第一处源码入口：create_deep_agent()

打开源码：

[deepagents/graph.py](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/graph.py)

找到：

```python
def create_deep_agent(
    model=...,
    tools=...,
    *,
    system_prompt=...,
    middleware=...,
    subagents=...,
    skills=...,
    memory=...,
    permissions=...,
    backend=...,
    interrupt_on=...,
    checkpointer=...,
    ...
):
    ...
```

这里列出了与我们学习有关的关键参数；省略号代表教学简化，不是完整的函数签名。

不要急着逐个研究参数。

我们先问：

> 这个函数最终返回什么？

沿着源码一直往下找，会发现，核心是：

```python
return create_agent(...)
```

随后还会调用：

```python
.with_config(...)
```

具体传入的内容包括模型、系统提示、工具、中间件、Checkpoint、Store 和 State Schema 等。

**这是整份源码最重要的发现。**

`create_deep_agent()` 本身主要负责装配。

真正创建并运行 Agent Loop 的能力，来自它调用的：

```python
langchain.agents.create_agent()
```

所以第一条源码调用链是：

```text
Application
    |
    v
create_deep_agent()
    |
    +-- 配置 Model
    |
    +-- 配置 Backend
    |
    +-- 组装 Middleware
    |
    +-- 配置 Subagents
    |
    +-- 配置 Skills / Memory
    |
    +-- 配置 Checkpoint
    |
    v
langchain.agents.create_agent()
    |
    v
CompiledStateGraph
    |
    v
agent.invoke(...)
```

在这里，我们需要建立第一个重要的心智模型：

**DeepAgents 是装配 Harness 能力的上层，LangChain 负责建立 Agent 运行循环，LangGraph 提供执行这个循环的状态化 Runtime。**

---

## 三、为什么 DeepAgents 不自己写 Agent Loop？

我们先假设 DeepAgents 自己实现循环。

那么可能有：

```python
def deep_agent_loop():
    while True:
        response = model.invoke(messages)

        if not response.tool_calls:
            return response

        for call in response.tool_calls:
            execute_tool(call)
```

这是教学伪代码。

看起来很简单。

但一旦进入生产系统，就必须回答：

- Middleware 在模型调用前后什么时候执行？
- 多个工具请求怎样调度？
- 状态由谁更新？
- 工具调用中断后如何恢复？
- Checkpoint 保存在哪里？
- 流式响应如何传播？
- 子 Agent 执行如何关联？
- 某个工具失败后如何返回模型？

这些问题已经超出一个简单循环。

LangChain 和 LangGraph 已经提供了相关基础设施。

所以 DeepAgents 不需要重新实现全部功能。

它只要决定：

> 我希望在通用 Agent Loop 上，默认增加哪些能力？

例如：

```text
普通 LangChain Agent
        |
        +-- 文件操作
        |
        +-- 上下文摘要
        |
        +-- 子 Agent
        |
        +-- Skills
        |
        +-- Memory
        |
        +-- 权限与 HITL
        |
        v
      Deep Agent
```

这也是源码为什么要引入大量 Middleware 的原因。

---

## 四、create_deep_agent() 的第一个动作：处理模型

回到 `graph.py`。

在函数内部，我们可以找到：

```python
model = resolve_model(model)
```

这是实际源码中处理显式传入模型的一条关键语句。

`resolve_model` 定义于：

[deepagents/_models.py](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/_models.py)

它负责将调用者传入的模型配置转换为可使用的 ChatModel。

例如：

```python
create_deep_agent(
    model="anthropic:claude-sonnet-4-6",
)
```

调用者传入一个字符串。

另一种方式是直接传入已经创建好的模型实例：

```python
from langchain_anthropic import ChatAnthropic

model = ChatAnthropic(
    model="claude-sonnet-4-6",
)

agent = create_deep_agent(
    model=model,
)
```

为什么允许这两种写法？

因为 DeepAgents 希望依赖统一的模型抽象，而不只支持某个固定 Provider。

[第十六章 Model Adapter](../Agent/16-model-adapter.md) 用一整章推过这件事，结论是：**Model Adapter 的价值不在于「支持多个厂商」，而在于把「模型能力差异」收敛成一处可判断的接口。**

从源码看，这个收敛点就是 `resolve_model` → `BaseChatModel` 这一段：不管调用方传字符串还是传实例，进入 Agent Loop 之前都被归一成同一个抽象。

与之前的 Model Adapter 原理对应：

```text
create_deep_agent
        |
        v
resolve_model
        |
        v
BaseChatModel
        |
        +-- Anthropic
        |
        +-- OpenAI
        |
        +-- 其他 Provider
```

另外，当前版本不建议省略 `model` 参数。依赖默认模型的用法已经弃用。

### 但 Adapter 只解决了一半问题

这里要补上理论没讲的那一半：**收敛之后，差异并没有消失，只是被推到了上游。**

`BaseChatModel` 统一了「怎么发一次请求」，没有统一「这个模型支不支持 tool calling」「支不支持并行工具调用」「上下文窗口多大」。这些能力差异会以另一种形式重新出现在 Agent 里——比如 `create_agent` 要根据模型能力决定要不要加一个隐藏的结构化输出工具，`SummarizationMiddleware` 要根据上下文窗口决定什么时候压缩。

所以读这段源码时，正确的结论不是「有 Adapter 就不用管模型差异」，而是：

> **Adapter 把模型差异从「调用方式」搬到了「能力判断」。前者被消除了，后者只是换了个地方集中管理。**

### 源码阅读重点

此时你不用钻进每一家模型 Provider 的实现。

只需要确认：

**DeepAgents 最终向 LangChain 传递的是模型抽象，而不是在 Agent Loop 中写死某家模型的 HTTP 协议。**

---

## 五、第二个动作：决定文件存在哪里

继续阅读 `graph.py`。

找到下面这条真实源码语句：

```python
backend = backend if backend is not None else StateBackend()
```

这是一个特别重要的设计。

为什么创建 Agent 时需要 Backend？

因为 Agent 可能需要：

```text
read_file
write_file
edit_file
glob
grep
```

但文件究竟在哪里？

可能是：

```text
LangGraph State
```

也可能是：

```text
Local Filesystem
```

还可能是：

```text
Container / Remote Sandbox
```

如果工具直接绑定本地磁盘，那么未来换成远程执行就很麻烦。

所以 DeepAgents 先确定 Backend，再让相关工具使用这个 Backend。

逻辑关系是：

```text
create_deep_agent
        |
        v
Backend Selection
        |
        +-----------+-----------+
        |           |           |
        v           v           v
   StateBackend  Filesystem   Sandbox /
                  Backend     Remote Backend
        |           |           |
        +-----------+-----------+
                    |
                    v
           FilesystemMiddleware
                    |
                    v
              File Tools
```

这里最容易误解的地方是：

**Backend 不等于 Sandbox。**

例如：

`StateBackend` 将文件放进 LangGraph State。

`FilesystemBackend` 读写实际文件系统。

只有提供对应执行能力的 Backend，才能进一步支持 Shell 执行；具体是否具备安全隔离，需要检查其实现。

所以：

```python
backend=FilesystemBackend(...)
```

并不意味着 Agent 已经被安全地隔离在容器中。

这与我们之前分析 Execution Environment 时得出的结论完全一致。

---

## 六、第三个动作：组装 Middleware

现在进入 `create_deep_agent()` 最有价值的部分。

找到：

```python
deepagent_middleware = [...]
```

当前源码的核心装配逻辑可以教学化简化为：

```python
# 教学化摘录：保留真实类名，省略参数和条件分支
middleware_stack = [
    FilesystemMiddleware(...),

    # 有同步子 Agent 时
    SubAgentMiddleware(...),

    _ParentNotifierMiddleware(),

    create_summarization_middleware(...),

    PatchToolCallsMiddleware(),

    # 可选：
    # AsyncSubAgentMiddleware(...)
    # SkillsMiddleware(...)
    # MemoryMiddleware(...)
    # HumanInTheLoopMiddleware(...)
]
```

注意：这是根据源码整理的教学化代码，不是可以直接运行的原样代码；真实顺序中还包含 Profile、Prompt Caching、Unsupported Content、Tool Exclusion 及用户自定义 Middleware 的处理。

现在我们不需要立即研究这些 Middleware 的全部实现。

先看它们分别解决什么问题。

### 6.1 FilesystemMiddleware

提供文件相关工具。

源码：

[middleware/filesystem.py](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/middleware/filesystem.py)

例如：

```text
read_file
write_file
edit_file
glob
grep
ls
```

实际读写由 Backend 负责。

### 6.2 SubAgentMiddleware

提供子 Agent 委派能力。

源码：

[middleware/subagents.py](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/middleware/subagents.py)

例如模型请求：

```text
task(
    subagent_type="general-purpose",
    description="调查优惠券分摊逻辑"
)
```

SubAgentMiddleware 负责将这项工作转交给对应的子 Agent。

默认情况下，DeepAgents 通常会装配一个 general-purpose Subagent，因此会有 `task` 工具。

### 6.3 SummarizationMiddleware

解决上下文增长的问题。

例如 Agent 已经执行很多轮：

```text
User
Assistant
Tool
Assistant
Tool
Assistant
Tool
...
```

当上下文接近限制时，中间件可以执行摘要或历史管理。

### 6.4 PatchToolCallsMiddleware

处理需要修补的工具调用与消息历史，避免不完整或不一致的工具调用记录破坏后续执行。

具体如何修补，我们会在 Context 与 State 章节研究。

### 6.5 SkillsMiddleware

根据 Skills 配置为 Agent 提供按需加载的工作说明。

### 6.6 HumanInTheLoopMiddleware

在配置了相应审批条件时，拦截敏感工具请求，配合 LangGraph 的 Interrupt 机制等待人工决定。

### 为什么 Middleware 而不是普通工具？

思考一个问题：

> 我们希望模型调用之前自动执行 Context Compaction，应该把它注册为 Tool 吗？

如果只提供：

```text
compact_context
```

这个工具，模型必须主动决定调用它。

但 Context Compaction 往往应该由 Harness 根据上下文状况触发。

因此，普通 Tool 并不合适。

Middleware 可以在模型调用前后介入。

这也是 DeepAgents 大量使用 Middleware 的根本原因：

**Tool 是模型可以主动选择的行动；Middleware 是 Harness 用来改变执行流程、工具集合或模型请求的机制。**

### 但 Middleware 里还藏着两套不同的机制

这里要先埋一个伏笔，否则第二章会看不懂。

`AgentMiddleware` 基类上的方法看起来是一组平行的 hook，实际上它们分两类，**调度方式完全不同**：

| 方法 | 签名里有 `handler` | 实现方式 | 能做什么 |
|---|---|---|---|
| `before_agent` / `before_model` | 没有 | 编译成 LangGraph 节点 | 改状态，不能短路 |
| `after_model` / `after_agent` | 没有 | 编译成 LangGraph 节点 | 改状态，不能短路 |
| `wrap_model_call` | 有 | 组合成闭包链 | 能短路、能重试、能改写结果 |
| `wrap_tool_call` | 有 | 组合成闭包链 | 同上 |

判据只有一条：**签名里有没有 `handler`。**

有 `handler`，说明它拿到的是「这次调用」这个动作本身，可以决定调几次、要不要调；
没有 `handler`，说明它拿到的只是「某个时刻」，只能修改状态然后交出去。

这个区分在 [第十七章 Hooks / MCP / Skills](../Agent/17-hooks-mcp-skills.md) 里是从扩展点的角度讲的，在这里则是从**调度器**的角度讲的。第二章读 `create_agent` 时会看到：`before_*` 变成 `graph.add_node(...)`，`wrap_*` 变成 `_chain_model_call_handlers(...)`。**同一个列表，一半进了图结构，一半进了函数调用栈。**

> [!TIP] 写 Middleware 时最容易犯的错
> 在 `before_model` 里想「如果条件满足就跳过这次模型调用」。
> 做不到。`before_model` 没有 `handler`，它无法阻止后面的节点执行。
> 这类需求必须写在 `wrap_model_call` 里。

---

## 七、第四个动作：把所有东西交给 LangChain

现在回到 `create_deep_agent()` 的结尾。

它最终把以下信息交给 `create_agent()`：

```text
model
system_prompt
tools
middleware
response_format
context_schema
checkpointer
store
state_schema
```

形成：

```text
              DeepAgents
                  |
                  v
          create_deep_agent()
                  |
      +-----------+-----------+
      |           |           |
      v           v           v
    Model       Backend    Middleware
      |           |           |
      +-----------+-----------+
                  |
                  v
         LangChain create_agent()
                  |
                  v
          LangGraph StateGraph
                  |
                  v
          CompiledStateGraph
                  |
                  v
             invoke / stream
```

至此，构建阶段结束。

这一阶段做的是**配置和组装**。

并不是调用 `create_deep_agent()` 就已经开始修复退款 Bug。

真正的执行要等到：

```python
agent.invoke(...)
```

或者：

```python
agent.stream(...)
```

才开始。

这叫区分：

**Construction Time（构建阶段）**

和：

**Execution Time（执行阶段）**。

---

## 八、继续追一层：LangChain 的 create_agent() 做了什么？

现在离开 DeepAgents 仓库，进入 LangChain。

源码：

[langchain/agents/factory.py](https://github.com/langchain-ai/langchain/blob/master/libs/langchain_v1/langchain/agents/factory.py)

这是整个系列非常重要的跨仓库跳转。

在这里，你会找到 Graph 的构建逻辑。

例如，源码会添加：

```python
graph.add_node("model", ...)
```

还会在存在工具执行节点时添加：

```python
graph.add_node("tools", tool_node)
```

这两句对应最简单的 Agent Loop：

```text
                   START
                     |
                     v
             Before-Agent Hooks
                     |
                     v
             Before-Model Hooks
                     |
                     v
                Model Node
                     |
                     v
             After-Model Hooks
                     |
                     v
              Has Tool Calls?
                     |
           +---------+---------+
           |                   |
          No                  Yes
           |                   |
           v                   v
       Final Answer        Tools Node
           |                   |
           v                   v
       After-Agent        ToolMessage
           |                   |
           v                   |
          END                  |
                               |
                               +----> Before-Model Hooks
                                            |
                                            v
                                        Model Node
```

这是根据 LangChain Graph 路由逻辑整理的简化运行流程，不代表每个 Middleware 必然生成同名节点，也省略了其他跳转条件。

**真正的 Agent Loop 就在这一层被构建出来。**

DeepAgents 只是告诉 LangChain：

> 给这个通用循环装上哪些能力。

LangChain 再把这些能力编排为 Graph。

LangGraph 负责实际执行 Graph。

---

## 九、动手实验：不用真实 LLM，也能验证 Agent Loop

前面已经看了源码。

但如果只是阅读函数，很容易觉得：

> 我理解了，但还不知道它实际会怎样运行。

现在我们做一个离线实验。

这个实验不需要模型 API Key。

它使用 LangChain 自带的 `FakeMessagesListChatModel`，预先给定两次模型响应。

但要注意：

**模拟的只有模型决策，DeepAgents 的 Graph、工具注册、工具执行和消息状态都是真实代码。**

这也是 LangGraph 自己的集成测试使用过的思路：

[LangGraph Deep Agent 集成测试](https://github.com/langchain-ai/langgraph/blob/main/libs/sdk-py/integration/graph/deep_agent.py)

### 9.1 安装依赖

使用 Python 3.11 或更新版本。

```bash
pip install "deepagents==0.7.32"
```

### 9.2 编写离线实验

创建 `inspect_deep_agent.py`：

```python
from typing import Any

from deepagents import create_deep_agent

from langchain_core.language_models.fake_chat_models import (
    FakeMessagesListChatModel,
)

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    ToolMessage,
)

from langchain_core.tools import tool


# ============================================
# 1. 伪造模型，但保留真实模型接口
# ============================================

class ScriptedModel(FakeMessagesListChatModel):

    def bind_tools(
        self,
        tools: Any,
        **kwargs: Any,
    ):
        # 模型响应已经预先指定好工具调用。
        # 这里只是满足 LangChain 的工具绑定接口。
        return self


# ============================================
# 2. 创建一个真实可执行的工具
# ============================================

@tool
def inspect_refund() -> str:
    """Inspect the current coupon refund calculation."""

    print("[ACTUAL TOOL] inspect_refund executed")

    return (
        "Current formula uses round(amount). "
        "Partial refund 42.50 becomes 42."
    )


# ============================================
# 3. 模拟模型的两次回复
# ============================================

model = ScriptedModel(
    responses=[
        # 第一次：模型决定调用工具
        AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "call-refund-1",
                    "name": "inspect_refund",
                    "args": {},
                }
            ],
        ),

        # 第二次：模型给出最终回答
        AIMessage(
            content=(
                "已定位问题：退款金额计算丢失了小数精度。"
            ),
        ),
    ]
)


# ============================================
# 4. 创建真正的 Deep Agent
# ============================================

agent = create_deep_agent(
    model=model,
    tools=[inspect_refund],
)


# ============================================
# 5. 观察编译后的 Graph
# ============================================

graph = agent.get_graph()

print("\nGraph Nodes:")
for name in graph.nodes:
    print(" -", name)


# ============================================
# 6. 真正执行 Graph
# ============================================

result = agent.invoke({
    "messages": [
        HumanMessage(
            content="定位优惠券部分退款 Bug"
        )
    ]
})


# ============================================
# 7. 查看真实消息历史
# ============================================

print("\nMessage History:")

for message in result["messages"]:

    print(
        type(message).__name__,
        getattr(message, "name", None),
    )

    if isinstance(message, AIMessage):
        print("tool_calls =", message.tool_calls)

    if isinstance(message, ToolMessage):
        print("tool_call_id =", message.tool_call_id)

    print("content =", str(message.content)[:160])


# ============================================
# 8. 验证工具调用确实完成
# ============================================

assert any(
    isinstance(message, ToolMessage)
    and message.name == "inspect_refund"
    and message.tool_call_id == "call-refund-1"
    for message in result["messages"]
)

print("\nAgent Loop verified")
```

这是基于真实 API 编写的教学实验。它不调用外部模型；当前示例没有在本环境完成依赖安装后的运行验证，因此以你本地的实际输出为准。

### 9.3 这个实验应该观察到什么？

首先，Graph 中应该能看到与模型和工具执行相关的节点：

```text
model
tools
```

根据当前 Middleware 配置，还可能有额外的节点。

其次，工具执行时应该看到：

```text
[ACTUAL TOOL] inspect_refund executed
```

这证明真正执行了我们注册的 Python 函数。

最后，消息历史应该包含如下顺序：

```text
HumanMessage
    |
    v
AIMessage
    tool_calls:
      inspect_refund
      id = call-refund-1
    |
    v
ToolMessage
    tool_call_id = call-refund-1
    |
    v
AIMessage
    最终回答
```

注意，这个最终回答是预先设置的 ScriptedModel 结果。

因此它不能证明模型真的推理出了 Bug 的原因。

但它能验证：

**模型提出的 Tool Call，确实经过真实的 DeepAgents/LangChain 工具执行流程，并以 ToolMessage 的形式进入 Graph State。**

这正是我们当前要研究的 Harness 行为。

---

## 十、用源码解释刚才那次实验

现在不要再把实验看成一个整体。

我们按照真实运行步骤重新走一遍。

### 第一步：创建 Agent

```python
agent = create_deep_agent(
    model=model,
    tools=[inspect_refund],
)
```

发生于构建阶段。

DeepAgents 组装 Middleware，然后调用 LangChain `create_agent()`。

### 第二步：执行用户请求

```python
agent.invoke({
    "messages": [
        HumanMessage(content="定位优惠券部分退款 Bug")
    ]
})
```

LangGraph 开始运行已编译的 Graph。

### 第三步：模型返回 Tool Call

第一次模型响应是：

```python
AIMessage(
    content="",
    tool_calls=[
        {
            "id": "call-refund-1",
            "name": "inspect_refund",
            "args": {},
        }
    ],
)
```

这是一个行动请求。

此时还没有执行工具。

### 第四步：Graph 路由到 Tools Node

LangChain 发现模型输出了工具请求。

因此执行进入工具处理路径。

可以在：

[LangChain Agent Factory](https://github.com/langchain-ai/langchain/blob/master/libs/langchain_v1/langchain/agents/factory.py)

找到 ToolNode 装配和路由逻辑。

### 第五步：执行真正的工具

`inspect_refund()` 被调用。

打印：

```text
[ACTUAL TOOL] inspect_refund executed
```

并返回退款 Bug 的诊断信息。

### 第六步：生成 ToolMessage

工具结果被包装成：

```text
ToolMessage
  name = inspect_refund
  tool_call_id = call-refund-1
  content = ...
```

为什么 Tool Call ID 必须保留？

因为模型可能在一次回复中提出多个 Tool Call。

Runtime 必须正确关联每个工具请求及其结果。

### 第七步：进入下一次模型调用

工具结果被加入 State 中的消息历史。

Graph 继续调用模型。

这次 ScriptedModel 返回普通文本，不再请求工具。

于是 Graph 结束本次 Agent 执行。

到这里，我们终于看清：

```text
User
  |
  v
LangGraph State
  |
  v
Model Node
  |
  v
AIMessage(tool_calls)
  |
  v
Tools Node
  |
  v
inspect_refund()
  |
  v
ToolMessage
  |
  v
LangGraph State
  |
  v
Model Node
  |
  v
AIMessage(final answer)
  |
  v
END
```

这正是前面学习 Agent Loop 时的理论流程，只不过现在能在真实的 DeepAgents 和 LangChain 代码中找到对应实现。

---

## 十一、一个常见误区：create_deep_agent() 创建后有哪些默认能力？

理解入口函数以后，还需要纠正几个常见误解。

### 误区一：所有工具都必须通过 tools 参数传入

不是。

```python
create_deep_agent(
    model=model,
    tools=[inspect_refund],
)
```

这里的 `tools` 是额外提供的工具。

DeepAgents 的 FilesystemMiddleware 还会提供文件相关工具。

### 误区二：不配置 Backend，就能在本地执行 Shell

不是。

默认的 `StateBackend` 主要负责 Graph State 中的文件数据。

它并不自动提供宿主机 Shell 执行能力。

Shell 工具是否可用，取决于具体 Backend 的执行接口及 Middleware 的能力判断。

### 误区三：DeepAgents 默认就有 TodoList

当前版本不是。

DeepAgents 0.7.0 起不再默认安装 `TodoListMiddleware`。

需要显式启用：

```python
from langchain.agents.middleware import (
    TodoListMiddleware,
)

agent = create_deep_agent(
    model=model,
    middleware=[TodoListMiddleware()],
)
```

### 误区四：传入 Checkpointer，文件就自动保存在真实磁盘上

不一定。

Checkpoint 管理 Graph State 的恢复。

文件的存储位置由 Backend 决定。

两者是不同的持久化边界。

### 误区五：Middleware 只是执行前后打印日志

不是。

Middleware 还可以：

- 注入工具。
- 修改模型请求。
- 提供自定义 State。
- 拦截 Tool Call。
- 执行上下文压缩。
- 触发 HITL。
- 改变部分 Graph 控制流程。

所以 Middleware 是 DeepAgents 的主要扩展机制之一，而不只是普通的日志 Hook。

---

## 十二、把本章源码映射回 Agent Harness 原理

现在我们已经能够把上一套教程的概念与实际代码对应起来。

| 之前的架构原理 | 对应理论章节 | DeepAgents / 底层源码入口 |
|---|---|---|
| Agent 构建（组合式 Harness） | [第十九章](../Agent/19-build-agent-harness.md) | `deepagents/graph.py` |
| Agent Loop | [第十章](../Agent/10-agent-loop.md) | `langchain/agents/factory.py` |
| Model Adapter | [第十六章](../Agent/16-model-adapter.md) | `BaseChatModel`、`resolve_model` |
| Tool System | [第十一章](../Agent/11-tool-system.md) | LangChain `ToolNode` |
| File Tools | [第十一章](../Agent/11-tool-system.md) | `FilesystemMiddleware` |
| Execution Backend | [第十二章](../Agent/12-execution-environment.md) | `backends/` |
| Permission | [第十三章](../Agent/13-permission-approval.md) | `FilesystemPermission`、`HumanInTheLoopMiddleware` |
| Context Compaction | [第一章](../Agent/1context.md)、[第七章](../Agent/07-context-assembly.md) | `SummarizationMiddleware` |
| Skills | [第十七章](../Agent/17-hooks-mcp-skills.md) | `SkillsMiddleware` |
| Memory | [第三章](../Agent/agent-context-memory-chapter-3.md) | `MemoryMiddleware` |
| Subagents | [第四章](../Agent/agent-context-subagents-chapter-4.md) | `SubAgentMiddleware` |
| HITL | [第十五章](../Agent/15-human-in-the-loop.md) | `HumanInTheLoopMiddleware` |
| Planning | [第十四章](../Agent/14-planning-task-management.md) | `TodoListMiddleware`（0.7.0 起不默认装配） |
| State / Checkpoint | [第六章](../Agent/06-checkpoint-resume.md) | `DeepAgentState`、LangGraph Runtime |

从这张表可以看出：

**DeepAgents 的源码分析，不能只看 `deepagents` 仓库。**

因为它自己并没有实现整个 Runtime。

我们必须沿着实际调用关系，必要时进入：

```text
langchain
```

和：

```text
langgraph
```

对应的源码。

但是每次跨仓库阅读，都必须有明确原因。

不能看见一个新类就一直跳下去。

我们的原则是：

**只追踪能够解释当前执行行为的调用链。**

---

## 十三、第一章最后再回顾一遍完整入口流程

```text
                         Application
                              |
                              v
                     create_deep_agent()
                              |
              +---------------+---------------+
              |               |               |
              v               v               v
         resolve_model    Select Backend   Resolve Profile
              |               |               |
              +---------------+---------------+
                              |
                              v
                       Build Middleware
                              |
           +------------------+------------------+
           |                  |                  |
           v                  v                  v
      Filesystem         Summarization       Subagents
      Middleware          Middleware         Middleware
           |                  |                  |
           +------------------+------------------+
                              |
                              v
                  Add Optional Middleware
                              |
               +--------------+--------------+
               |              |              |
               v              v              v
             Skills         Memory           HITL
               |              |              |
               +--------------+--------------+
                              |
                              v
                    LangChain create_agent()
                              |
                              v
                      Build StateGraph
                              |
               +--------------+--------------+
               |                             |
               v                             v
           Model Node                    Tools Node
               |                             |
               +--------------+--------------+
                              |
                              v
                     Compile LangGraph
                              |
                              v
                       Compiled Agent
                              |
                              v
                         agent.invoke()
                              |
                              v
                         Agent Loop
                              |
                              v
                     Model → Tool → Model
                              |
                              v
                         Final Result
```

这是本章的总调用链。

要特别区分图中的两个阶段：

**创建 Agent 时：**

```text
选择依赖 → 装配 Middleware → 编译 Graph
```

**运行 Agent 时：**

```text
读取 State → 调用 Model → 执行 Tool → 更新 State → 再调用 Model
```

混淆这两个阶段，是刚开始阅读 DeepAgents 源码时最常见的问题之一。

---

## 十四、读完这一章，应该自己能够回答什么？

### 问题一：DeepAgents 的 Agent Loop 在哪里？

主要由 LangChain `create_agent()` 构建，底层使用 LangGraph 执行。

### 问题二：DeepAgents 的核心价值是什么？

装配一组适合复杂任务的 Middleware、Backend、子 Agent 和其他 Harness 能力。

### 问题三：为什么需要 Backend？

因为文件工具等能力不应该绑定某一种固定存储实现。

### 问题四：为什么要使用 Middleware？

因为某些行为需要在模型调用前后、工具执行前后或状态更新过程中自动运行，而不是等待模型主动调用普通工具。

### 问题五：创建 Agent 和运行 Agent 的区别是什么？

`create_deep_agent()` 负责构建。

`agent.invoke()` 触发真正的 Graph 执行。

---

## 本章总结

我们从：

```python
agent = create_deep_agent(...)
```

一路追踪到了：

```text
DeepAgents
    |
    v
LangChain create_agent()
    |
    v
LangGraph StateGraph
    |
    v
Model Node ↔ Tools Node
```

最终得出三个核心结论：

**第一，DeepAgents 是组合式 Harness，而不是一套完全独立的 Agent Runtime。**

**第二，Middleware 是 DeepAgents 将文件、子 Agent、上下文管理等能力接入通用 Agent Loop 的关键机制。**

**第三，只有区分构建阶段和执行阶段，才能沿正确调用链阅读源码。**

下一章我们正式进入真正的执行核心：

# 第二章：LangChain create_agent() 如何构建 Agent Loop？

我们将继续从这次 `inspect_refund()` 工具调用出发，追踪：

```text
agent.invoke()
      |
      v
LangGraph START
      |
      v
ModelRequest
      |
      v
Model Node
      |
      v
AIMessage(tool_calls)
      |
      v
ToolNode
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

重点研究：

- `StateGraph` 如何创建 `model` 和 `tools` 节点。
- `ModelRequest` 如何组装消息和工具定义。
- Middleware 的 `before_model`、`wrap_model_call` 如何进入实际调用路径。
- `ToolNode` 如何执行模型产生的 Tool Call。
- `ToolMessage` 如何被写入 State。
- Graph 为什么会继续调用模型，以及什么条件下停止。

不再停留在架构示意图，而是直接追踪 LangChain `factory.py` 的关键函数和分支。

## 本章源码入口

1. [DeepAgents graph.py](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/graph.py)
2. [DeepAgents ARCHITECTURE.md](https://github.com/langchain-ai/deepagents/blob/main/libs/ARCHITECTURE.md)
3. [DeepAgents models.py](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/_models.py)
4. [DeepAgents FilesystemMiddleware](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/middleware/filesystem.py)
5. [LangChain Agent Factory](https://github.com/langchain-ai/langchain/blob/master/libs/langchain_v1/langchain/agents/factory.py)
6. [LangGraph Fake Model 集成测试](https://github.com/langchain-ai/langgraph/blob/main/libs/sdk-py/integration/graph/deep_agent.py)

建议将研究代码固定到 `deepagents==0.7.32` 附近的仓库版本，避免后续 `main` 分支变化导致源码行号和接口不一致。
