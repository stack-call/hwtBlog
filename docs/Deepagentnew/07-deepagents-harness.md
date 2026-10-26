---
title: 第七章：DeepAgents 如何装配 Agent Harness？
description: 从修复优惠券部分退款 Bug 的 Coding Agent 出发，沿 DeepAgents 0.7.32 的 create_deep_agent 源码，逐层研究 Model、Backend、Middleware、Subagents、HarnessProfile、State 和 LangChain create_agent 的装配过程
tags: [DeepAgents, LangChain, LangGraph, Agent Loop, Agent Harness, Middleware, 源码精读]
sidebar_position: 7
---

# 第七章：DeepAgents 如何装配 Agent Harness？

> **本章核心问题：** 我们已经知道 LangChain 如何将 Middleware 接入 Agent Loop。那么 DeepAgents 为什么还需要 `create_deep_agent()`？它究竟多做了什么，又把什么工作交还给 `create_agent()`？

## 一、从上一章留下的问题开始

上一章，我们跟踪了 LangChain `create_agent()` 的源码，已经理解了：

- `before_model` 等生命周期 Hook 可以被编译成 Graph Node。
- `wrap_model_call` 与 `wrap_tool_call` 通过 Handler 包装真实调用。
- Middleware 可以声明工具、扩展 State，而不仅仅是拦截一次执行。
- Agent Loop 的调度仍由 LangGraph 负责。

到这里，我们其实已经能够搭建一个基础 Coding Agent。

沿用全书的任务：

> 检查电商项目中的 `refund.py`，修复优惠券部分退款的精度 Bug，运行相关测试，并根据测试结果继续处理。

最简单的代码可能是：

```python
from langchain.agents import create_agent

agent = create_agent(
    model=model,
    tools=[read_file, edit_file, run_tests],
)
```

这在只有几个事先写好的工具时完全合理。模型提出 Tool Call，ToolNode 执行，工具结果返回模型，模型继续判断。

```text
用户任务
   |
   v
 Model <------------+
   |                |
   v                |
 ToolNode           |
   |                |
   +----------------+
```

但当我们真正尝试用它完成退款 Bug 修复时，就会接连遇到新问题。

首先，`read_file` 和 `edit_file` 应该访问哪里？如果运行在本地、远程容器或测试环境中，是否每换一个环境就要重写全部工具？

其次，测试日志积累了几十轮以后，谁来管理上下文？

再次，如果需要把测试失败的调查交给一个子 Agent，谁负责创建和注册子 Agent 的入口？

最后，这些能力都有了以后，每个项目是否都要手动拼接十几个 Middleware，自己维护它们的顺序？

我们真正缺少的，不是另一套 Agent Loop，而是一个**在构建阶段统一装配这些能力的入口**。

DeepAgents 把这个入口命名为：

```python
create_deep_agent(...)
```

这一章不从庞大的总架构图开始。我们先从基础 Agent 出发，每暴露一个新的需求，就去 `create_deep_agent()` 中寻找对应的一小段代码。等所有装配步骤都理解了，完整的 Harness 架构自然就出现了。

### 1.1 本章版本边界

本文固定阅读：

```text
DeepAgents Python: 0.7.32
Git Commit:        e85c6b9
入口文件:          libs/deepagents/deepagents/graph.py
```

主要源码：

- [`graph.py`（固定 Commit）](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py)
- [`middleware/filesystem.py`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py)
- [`middleware/subagents.py`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/subagents.py)
- [`profiles/harness/harness_profiles.py`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/profiles/harness/harness_profiles.py)

上一章参考的 LangChain 版本为 `1.4.7`。本章在描述 LangChain 运行路径时承接上一章的解释，但分析的**装配规则**以 DeepAgents 的上述固定 Commit 为准。

下文的代码块会明确标识为“真实源码节选”“教学化简”或“概念伪代码”。真实源码节选可能省略无关语句，但不改变所讨论分支的核心语义。所有运行轨迹若未实际执行，均标为预期结果。

---

## 二、先看最小入口：DeepAgents 返回的是什么？

我们先尝试把原来的 `create_agent()` 替换为：

```python
from deepagents import create_deep_agent

agent = create_deep_agent(model=model)
```

这行代码看起来像在“创建一个新的 Agent 类型”。但真实源码给出的答案更具体。

打开 `graph.py`，直接跳到 `create_deep_agent()` 的末尾。

**真实源码节选：** [`graph.py` 第 920—942 行](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py#L920-L942)

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
    state_schema=state_schema if state_schema is not None else DeepAgentState,
).with_config(
    {
        "recursion_limit": 9_999,
        "metadata": {
            "ls_integration": "deepagents",
            "lc_versions": {"deepagents": _lc_version()},
            "lc_agent_name": name,
        },
    }
)
```

这段代码足以回答本章最重要的架构判断：

**`create_deep_agent()` 自己不负责重新实现 Model → Tool → Model 循环。它准备好模型、系统指令、工具、Middleware 和状态配置，再调用 LangChain 的 `create_agent()`。**

换句话说，DeepAgents 的主要工作是**装配**；LangChain 的主要工作是**构建 Agent Graph**；真正运行 Graph 的是 LangGraph Runtime。

先把关系画到最小：

```text
create_deep_agent()
        |
        | 准备配置与组件
        v
create_agent()
        |
        | 构建、编译 Graph
        v
CompiledStateGraph
        |
        | invoke / stream
        v
LangGraph Runtime 执行 Agent Loop
```

还需要观察一个容易忽视的细节：最终调用使用的是 `tools=_tools`，而不是把所有文件工具、子 Agent 工具都手工拼成一个大列表。

为什么？因为上一章已经看到，`create_agent()` 会从 Middleware 中收集它们声明的工具。DeepAgents 只要正确装配相应 Middleware，这些工具就能进入后续 Agent 构建流程。

由此我们有了下一步问题：**DeepAgents 的第一个内置能力——文件访问——怎样装进来？**

---

## 三、工具不是直接绑定磁盘：先引入 Backend

### 3.1 最直觉的文件工具

起初，我们可能为退款项目写出：

```python
from pathlib import Path


def read_file(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")
```

在固定的本地目录里，这样写没有问题。

但如果同一个 Agent 在 CI 容器中运行，或者文件并不存储在本地磁盘，而是放在可恢复的状态中，那么 `Path(path)` 就不再足够。

我们当然可以为每种环境实现一套 `read_file`、`write_file`、`edit_file`。可是很快会出现重复代码：文件读写的接口相似，真正不同的只是数据保存和执行的位置。

因此，需要将两件事分开：

1. 模型看到的是**文件操作工具**。
2. 工具背后访问的是**具体的文件与执行环境**。

后者就是本章在这里才需要引入的 `Backend`。

```text
Model Tool Call: read_file("/refund.py")
                   |
                   v
          FilesystemMiddleware
                   |
                   v
             Backend 接口
                   |
        +----------+----------+
        |                     |
        v                     v
   StateBackend         其他 Backend
```

这张图表示的是**职责与调用关系的教学化简**，不是说所有 Backend 都能执行 Shell 或都具备相同持久化能力。

### 3.2 真实源码：默认 Backend 是怎样确定的？

**真实源码节选：** [`graph.py` 第 604 行](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py#L604)

```python
backend = backend if backend is not None else StateBackend()
```

调用者没有提供 Backend 时，DeepAgents 使用 `StateBackend()`。

这意味着：

```python
agent = create_deep_agent(model=model)
```

与：

```python
agent = create_deep_agent(
    model=model,
    backend=StateBackend(),
)
```

在 Backend 选择上遵循同样的默认规则。

但这**不意味着** `refund.py` 自动对应宿主机磁盘上的真实文件。`StateBackend` 与本地文件系统不是同一种存储方式。更不能因为工具列表中有 `execute`，就假定默认 Backend 能运行 `pytest`。

固定版本的函数文档还特别说明：`execute` 工具只有在 Backend 实现 `SandboxBackendProtocol` 时才能真正运行命令；不支持这一能力的 Backend 会返回错误信息。

因此，在完整退款案例中：

- 只研究文件工具的注册和路由，可以使用默认 Backend。
- 若要真实修改仓库文件并执行 `pytest`，还必须选择具有相应文件访问与命令执行能力的环境，并明确其安全边界。

### 3.3 真实源码：FilesystemMiddleware 怎么使用 Backend？

继续向下阅读主 Agent Middleware 的创建处。

**真实源码节选：** [`graph.py` 第 821—828 行](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py#L821-L828)

```python
deepagent_middleware: list[AgentMiddleware[Any, Any, Any]] = [
    FilesystemMiddleware(
        backend=backend,
        custom_tool_descriptions=_profile.tool_description_overrides,
        permissions=permissions,
    ),
]
```

这里调用的不是一次 `read_file()`。

它是在**构建阶段**创建 `FilesystemMiddleware` 实例，将前面确定的 Backend 交给它。

随后 LangChain `create_agent()` 会收集 Middleware 提供的工具。实际运行时，模型产生 `read_file` 等 Tool Call，ToolNode 分发到工具，文件工具才经由 Backend 完成操作。

所以要区分两个时间点：

```text
构建阶段：
create_deep_agent()
    -> 创建 FilesystemMiddleware(backend=...)
    -> 交给 create_agent() 注册工具

执行阶段：
Model 发起 read_file Tool Call
    -> ToolNode
    -> 文件工具
    -> Backend
    -> ToolMessage
```

**Backend 解决“操作落在哪里”，FilesystemMiddleware 解决“向 Agent 提供哪些文件能力以及如何接入”。**

如果把两者混为一谈，就容易把 Backend 误当成可以直接被模型调用的工具，或者把工具名误认为一种具体存储实现。

现在 Agent 已经有了较统一的文件能力。不过运行几十轮后，又会出现一个新问题。

---

## 四、运行越来越长：为什么装配 SummarizationMiddleware？

假设退款任务逐步经历：

```text
读取 refund.py
读取 coupon_service.py
读取 test_refund.py
修改 refund.py
运行 pytest
查看长错误日志
再次修改
再次运行测试
...
```

如果不断把完整历史送回模型，消息上下文会持续增长。

我们在前面章节已经理解，控制模型上下文是典型的 Middleware 需求。现在我们不需要重新设计摘要算法，只需要回答：**DeepAgents 在哪里将这一能力装进去？**

**真实源码节选：** [`graph.py` 第 843—849 行](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py#L843-L849)

```python
deepagent_middleware.extend(
    [
        _ParentNotifierMiddleware(),
        create_summarization_middleware(model, backend),
        PatchToolCallsMiddleware(),
    ]
)
```

注意这里有三个组件，而不只有摘要 Middleware。

对当前问题最重要的是：

```python
create_summarization_middleware(model, backend)
```

它将当前模型与 Backend 传入摘要 Middleware 的创建过程，让 Harness 具备后续处理长上下文的能力。

此时可以先将我们已经理解的装配过程简化为：

```text
create_deep_agent()
    |
    +-- Backend
    |
    +-- FilesystemMiddleware
    |
    +-- SummarizationMiddleware
    |
    v
create_agent()
```

另外两个组件在这里先只把握必要信息：

- `_ParentNotifierMiddleware` 是主 Agent 装配链中的内置组件，不应在研究默认顺序时遗漏。
- `PatchToolCallsMiddleware` 用于处理工具调用相关消息的修补问题，其内部策略不属于本章的核心。

暂时不展开摘要触发阈值、历史消息改写和文件卸载机制。这些问题留到第八章，与 Context、Skills 和 Memory 一起沿运行时路径研究。

但现在又产生了另一种更独立的需求：同一个 Agent 要改代码，还要分析复杂测试失败，是否必须把所有调查都塞进它自己的上下文？

---

## 五、需要委派任务时：Subagents 怎样装配？

### 5.1 为什么仅增加一个 `task` 工具还不够？

假设主 Agent 正在修改 `refund.py`，但 pytest 出现一个与优惠券金额分摊有关的边界用例失败。

我们希望另一个 Agent 只负责：

> 阅读失败测试，分析舍入与金额分摊逻辑，返回可供主 Agent 使用的结论。

一种最简单的设想是为主 Agent 注册一个 `task` 工具。可是 `task` 工具真正执行时，还必须知道：

- 哪些子 Agent 可以调用？
- 子 Agent 使用什么 Model 和 Tools？
- 子 Agent 需要怎样的 Middleware？
- 子任务结束后，结果怎样返回主 Agent？

所以问题不是一个工具名，而是**工具背后还存在另一套 Agent 执行配置**。

DeepAgents 因此先准备子 Agent 配置，再创建 `SubAgentMiddleware`。

### 5.2 真实源码：默认的 general-purpose 子 Agent

在 `graph.py` 中，系统先处理调用者传入的子 Agent，随后判断是否自动补充默认的 `general-purpose` 子 Agent。

**真实源码节选：** [`graph.py` 第 755—761 行](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py#L755-L761)

```python
gp_profile = _profile.general_purpose_subagent or GeneralPurposeSubagentProfile()
if gp_profile.enabled is not False and not any(
    spec["name"] == GENERAL_PURPOSE_SUBAGENT["name"]
    for spec in inline_subagents
):
    ...
```

这一判断包含两个条件：

1. 当前 HarnessProfile 没有明确禁用默认子 Agent。
2. 调用者没有已经提供同名的子 Agent。

也就是说：`general-purpose` 通常会自动加入，但**并非无条件存在**。用户可以提供同名定义进行替代，Profile 也可以禁用它。

当默认子 Agent 被禁用并且没有其他同步子 Agent 时，主 Agent 就不应被假定一定有 `task` 工具。

默认子 Agent 也有自己的 Middleware 栈。源码包含：

**真实源码节选：** [`graph.py` 第 762—770 行](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py#L762-L770)

```python
gp_middleware: list[AgentMiddleware[Any, Any, Any]] = [
    FilesystemMiddleware(
        backend=backend,
        custom_tool_descriptions=_profile.tool_description_overrides,
        permissions=permissions,
    ),
    create_summarization_middleware(model, backend),
    PatchToolCallsMiddleware(),
]
```

这意味着子 Agent 不是“共享主 Agent 的同一次模型思考过程”。它有自己的 Agent 配置，并能够形成独立执行过程。

主 Agent 与子 Agent 可以共享所配置的 Backend，但共享 Backend **不等于共享全部消息历史，也不等于共享全部状态字段**。具体的数据继承和回写规则留到第十二、十三章。

### 5.3 真实源码：如何把子 Agent 变成主 Agent 的能力？

经过子 Agent 配置的准备后，主 Agent 的装配逻辑出现：

**真实源码节选：** [`graph.py` 第 829—842 行](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py#L829-L842)

```python
sub_agent_middleware: SubAgentMiddleware | None = None
if inline_subagents:
    sub_agent_middleware = SubAgentMiddleware(
        backend=backend,
        subagents=inline_subagents,
        task_description=_profile.tool_description_overrides.get("task"),
        state_schema=state_schema,
    )
    deepagent_middleware.append(sub_agent_middleware)
```

这几行的因果关系很明确：

```text
已有子 Agent 配置
        |
        v
创建 SubAgentMiddleware
        |
        v
向主 Agent 提供委派入口
        |
        v
Model 可以发起 task Tool Call
```

这里的 `inline_subagents` 是已经准备好的子 Agent 定义。`SubAgentMiddleware` 才是主 Agent 侧承接委派能力的组件。

到此为止，主 Agent 同时拥有文件能力、摘要能力和任务委派能力。这些能力不会自动让模型知道哪个修改一定正确，它们只是把可以采取的行动及执行机制准备好了。

那么，面对不同模型，它们的系统提示词、工具描述、默认子 Agent 策略是否必须完全相同？

---

## 六、模型不同，Harness 也可能不同：引入 HarnessProfile

### 6.1 简单写死配置为什么不够？

想象我们为某种模型写死一份系统提示词，以及 `task`、`read_file` 等工具的描述。

在一个项目中它可能表现良好。但切换另一种模型后，可能需要调整工具描述、Prompt 策略或一些扩展组件。

我们可以在 `create_deep_agent()` 中写大量：

```python
# 概念伪代码：并非项目源码
if model_provider == "A":
    ...
elif model_provider == "B":
    ...
```

然而这样会让模型差异配置与通用装配逻辑混在一起。

此时才有必要引入 `HarnessProfile`：**把面向特定模型的 Harness 行为配置，与通用装配步骤分开。**

### 6.2 真实源码：先解析模型，再取得 Profile

**真实源码节选：** [`graph.py` 第 563—589 行](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py#L563-L589)

```python
_model_spec: str | None = model if isinstance(model, str) else None
if model is None:
    # 此处原源码先发出 deprecated 警告；为突出主路径省略
    model = _build_default_model()
else:
    model = resolve_model(model)
_profile = _harness_profile_for_model(model, _model_spec)
_validate_excluded_middleware_config(
    _profile,
    required_classes=_REQUIRED_MIDDLEWARE_CLASSES,
    required_names=_REQUIRED_MIDDLEWARE_NAMES,
)
```

这段代码已经出现完整的先后关系：

```text
调用者提供 model
        |
        v
   resolve_model()
        |
        v
_harness_profile_for_model()
        |
        v
校验 Profile 配置
```

它不是让 Profile 代替 Model。Model 仍然负责预测消息与工具调用；Profile 负责帮助 Harness 决定怎样组合 Prompt、工具描述以及相关扩展。

固定版本中，`model=None` 仍可回退到 `claude-sonnet-4-6`，但这一隐式默认行为已经被弃用。实际项目宜显式提供 Model。

### 6.3 Profile 先影响工具描述与 Prompt

接下来，源码使用 Profile 改写调用者提供的工具描述：

**真实源码节选：** [`graph.py` 第 596—602 行](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py#L596-L602)

```python
_tools = _apply_tool_description_overrides(
    tools,
    _profile.tool_description_overrides,
)
```

这里先不要把 `tools` 与所有文件工具混为一谈：传入的 `tools` 是调用者提供的工具集合；文件工具等还可能由 Middleware 提供。

同一个 Profile 也会影响系统 Prompt。

**真实源码节选：** [`graph.py` 第 604—614 行](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py#L604-L614)

```python
base_prompt = _apply_profile_prompt(_profile, "")
if system_prompt is None:
    final_system_prompt: str | SystemMessage = base_prompt
elif isinstance(system_prompt, SystemMessage):
    if base_prompt:
        final_system_prompt = SystemMessage(
            content_blocks=[
                *system_prompt.content_blocks,
                {"type": "text", "text": f"\n\n{base_prompt}"},
            ]
        )
    else:
        final_system_prompt = system_prompt
else:
    final_system_prompt = system_prompt + (f"\n\n{base_prompt}" if base_prompt else "")
```

这说明 `system_prompt` 不是总能简单地原样传给 LangChain。DeepAgents 先根据 Profile 生成 `base_prompt`，再与调用者 Prompt 合并。

同时应注意：0.7.32 已不再无条件给每个 Agent 注入早期那套固定的 DeepAgents 基础提示词；旧 `BASE_AGENT_PROMPT` 只作为弃用兼容入口存在。具体模型使用什么 Profile 内容，必须查看实际匹配的 Profile，而不能凭旧教程猜测。

### 6.4 Profile 还会影响 Middleware 选择

看到这里，Profile 的存在已经合理了。但如果某种模型需要额外 Middleware 呢？

源码使用：

```python
# 真实源码节选：graph.py 第 860 行
deepagent_middleware.extend(_profile.materialize_extra_middleware())
```

这个调用将 Profile 声明的额外 Middleware 实例化后加入栈中。

随后还会处理 Profile 的 Middleware 排除规则及工具排除规则。为什么这些排除需要严格校验，我们会在下一节顺着装配顺序解释。

至此可以把已知关系扩展一层：

```text
Model
  |
  v
HarnessProfile
  |-- 工具描述调整
  |-- System Prompt 调整
  |-- 额外 Middleware
  |-- 子 Agent 默认策略
  v
DeepAgents 装配
```

这张图只表示 Profile 对装配结果的影响，不表示一次真实模型请求依次调用上述节点。

---

## 七、关键问题：Middleware 到底按什么顺序装配？

### 7.1 如果只是全部追加，为什么还不够？

我们现在至少有：

```text
FilesystemMiddleware
SubAgentMiddleware
SummarizationMiddleware
PatchToolCallsMiddleware
SkillsMiddleware
MemoryMiddleware
HumanInTheLoopMiddleware
...
```

一个直觉写法是：

```python
# 概念伪代码
all_middleware = defaults + user_middleware
```

这似乎足够简单。

但它马上遇到两个问题。

**第一个问题：** 如果用户想定制一个内置 `FilesystemMiddleware`，简单追加会产生同名组件，而不是替换原来的组件。

**第二个问题：** 如果新 Middleware 必须位于某些 Prompt、缓存或 Memory 处理之前，全部追加会改变原来的预期顺序。

所以 DeepAgents 需要的不只是“存放 Middleware 的列表”，而是一套**可预测的装配规则**。

### 7.2 真实源码：主 Agent 的核心栈

我们先只看主 Agent 的基础部分。

**真实源码节选：** [`graph.py` 第 821—856 行](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py#L821-L856)

```python
deepagent_middleware = [
    FilesystemMiddleware(...),
]

if inline_subagents:
    deepagent_middleware.append(SubAgentMiddleware(...))

deepagent_middleware.extend([
    _ParentNotifierMiddleware(),
    create_summarization_middleware(model, backend),
    PatchToolCallsMiddleware(),
])

if async_subagents:
    deepagent_middleware.append(
        AsyncSubAgentMiddleware(async_subagents=async_subagents)
    )

_main_core_names = {m.name for m in deepagent_middleware}
```

**说明：以上是教学化简。** 省略了构造函数参数，但保持了真实的添加条件和先后关系。

现在可以明确，主 Agent 的核心栈通常以 Filesystem 开始；同步子 Agent 存在时加入 SubAgent；随后是 ParentNotifier、Summarization、PatchToolCalls；如有异步子 Agent，再加入 AsyncSubAgent。

这里的 `_main_core_names` 很重要。系统此时记下“核心组件”的名字，后面将用它确定**新的用户 Middleware 应当插入到什么位置**。

### 7.3 核心之后，还有 Profile、Skills、缓存和 Memory

**真实源码节选：** [`graph.py` 第 857—884 行](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py#L857-L884)

```python
deepagent_middleware.extend(_profile.materialize_extra_middleware())

if skills is not None:
    deepagent_middleware.append(
        SkillsMiddleware(backend=backend, sources=skills)
    )

append_prompt_caching_middleware(deepagent_middleware)

if memory is not None:
    deepagent_middleware.append(
        MemoryMiddleware(
            backend=backend,
            sources=memory,
            add_cache_control=True,
        )
    )

main_interrupt_on = _merge_fs_interrupt_on(
    _build_interrupt_on_from_permissions(permissions or [], backend),
    interrupt_on,
)
if main_interrupt_on is not None:
    deepagent_middleware.append(
        HumanInTheLoopMiddleware(interrupt_on=main_interrupt_on)
    )

deepagent_middleware.append(UnsupportedContentMiddleware())
```

这里可以观察到四种不同的规则：

- `SkillsMiddleware` 只有在存在 Skills 来源时才被添加；如果调用者在自定义 Middleware 中提供了同名 SkillsMiddleware，源码还会尝试提取其 sources。
- Prompt Caching 通过帮助函数处理，并不是断言每种模型一定额外新增一个完全相同的缓存 Middleware。
- `MemoryMiddleware` 仅在传入 `memory` 来源时加入。
- HITL 只有在文件权限规则或用户 `interrupt_on` 生成了中断配置时才加入。

再注意版本差异：**此处没有默认的 `TodoListMiddleware`。** 在 DeepAgents 0.7.32 中，不应把 Todo Planning 画进“无条件默认安装”的 Middleware 栈。若需要规划功能，应明确配置相关 Middleware。

### 7.4 自定义 Middleware 不是简单追加：按名称替换

假设用户传入：

```python
agent = create_deep_agent(
    model=model,
    middleware=[CustomAuditMiddleware()],
)
```

如果它是一个全新的名字，放在哪里？如果它与某个默认 Middleware 同名，又怎样处理？

真正的答案在 `_apply_custom_middleware()` 中。

**真实源码节选：** [`graph.py` 第 195—234 行](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py#L195-L234)

```python
for m in custom:
    if m.name in current_names:
        replacements[m.name] = m
    else:
        to_append.append(m)

result = list(base)
for i, m in enumerate(result):
    if m.name in replacements:
        replacement = replacements[m.name]
        # 原源码还包含 Filesystem 权限冲突与继承校验
        result[i] = replacement

if to_append and core_names is not None:
    pos = max(
        (i for i, m in enumerate(result) if m.name in core_names),
        default=len(result) - 1,
    ) + 1
    result[pos:pos] = to_append
else:
    result.extend(to_append)
```

读这段代码时，先抓住两条规则：

1. **同名：** 原位替换，维持原先位置。
2. **新名字：** 在设置了 `core_names` 的主 Agent 中，插到最后一个核心 Middleware 之后、Profile / Skills / Caching / Memory 等尾部组件之前。

因此最终不是：

```text
默认组件 A, 默认组件 B, ..., 默认组件 Z, 自定义组件
```

而更接近：

```text
核心 Middleware
      |
      v
新增加的自定义 Middleware
      |
      v
Profile / Skills / Caching / Memory 等后续组件
```

如果同名替换的是 `FilesystemMiddleware`，源码还会检查权限规则是否冲突，防止通过替换误丢掉已有的文件权限约束。

### 7.5 为什么还要在自定义装配后再次过滤？

Profile 可能声明排除某些 Middleware。如果先过滤一次，再允许自定义 Middleware 重新插入同名组件，排除结果就可能失效。

所以源码采用如下顺序：

**真实源码节选：** [`graph.py` 第 885—901 行](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py#L885-L901)

```python
deepagent_middleware = _apply_excluded_middleware(
    deepagent_middleware, _profile, ...
)

deepagent_middleware = _apply_custom_middleware(
    deepagent_middleware,
    middleware or [],
    core_names=_main_core_names,
)

deepagent_middleware = _apply_excluded_middleware(
    deepagent_middleware, _profile, ...
)

if _profile.excluded_tools:
    deepagent_middleware.append(
        _ToolExclusionMiddleware(excluded=_profile.excluded_tools)
    )
```

这里的 `...` 表示省略了统计匹配情况的参数，是源码节选而非可运行代码。

为什么 `excluded_tools` 对应的 Middleware 最后追加？因为它需要在已经完成工具注入和自定义组件装配后，再处理最终可见工具集合。

另外，0.7.32 明确保护 `FilesystemMiddleware` 和 `SubAgentMiddleware` 等核心基础结构，不能通过 Profile 的 `excluded_middleware` 规则任意移除；否则文件能力、子 Agent 委派或权限保证可能被破坏。

于是我们得到一条更准确的装配规律：

> **Middleware 的排列不是自由堆叠，而是先建立核心，再插入可定制能力，最后施加模型 Profile 的约束。**

但要强调：这是**装配列表顺序**。具体到一次请求中的 Hook 执行顺序，还要由上一章研究过的 LangChain Factory 决定。例如 `before_model` 通常顺序进入，而后置 Hook 与 Wrapper 的返回路径有反向执行关系。不能把列表简单理解成“所有组件都会从上到下完整执行一次”。

---

## 八、权限与 HITL：为什么不能只提供文件工具？

我们的退款任务终于进入修改阶段：

```text
Model: edit_file("/refund.py", ...)
```

模型能够产生合法 Tool Call，并不等于允许它立即执行修改。

最直接的做法是在 `edit_file` 内硬编码一次确认，但这样会把具体文件操作、权限规则与人工审批绑死在一起。尤其当我们切换 Backend 或引入更多修改工具后，这种写法就不容易统一管理。

在 `create_deep_agent()` 中，调用者可以传入：

```python
agent = create_deep_agent(
    model=model,
    interrupt_on={"edit_file": True},
)
```

这表示希望对特定工具调用设置审批行为。它不会在构建时立即弹出审批，而是装配一个等待真实工具调用的控制机制。

### 8.1 真实源码：先合并权限产生的中断规则

**真实源码节选：** [`graph.py` 第 174—189 行](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py#L174-L189)

```python
def _merge_fs_interrupt_on(fs_interrupt_on, user_interrupt_on):
    if not fs_interrupt_on and not user_interrupt_on:
        return None
    merged = {**fs_interrupt_on}
    if user_interrupt_on:
        merged.update(user_interrupt_on)
    return merged
```

这段代码不复杂，却解决了一个重要问题：文件权限规则和用户显式指定的 `interrupt_on` 可能同时作用于同一个工具。

源码规定：**用户显式提供的同名 `interrupt_on` 配置会覆盖从文件权限生成的中断配置。**

注意这是合并中断配置的优先级，不意味着所有 Backend 级权限检查都会被绕过。

### 8.2 真实源码：有配置才安装 HITL

回到主装配流程：

```python
# 真实源码节选：graph.py 第 878—883 行
main_interrupt_on = _merge_fs_interrupt_on(
    _build_interrupt_on_from_permissions(permissions or [], backend),
    interrupt_on,
)
if main_interrupt_on is not None:
    deepagent_middleware.append(
        HumanInTheLoopMiddleware(interrupt_on=main_interrupt_on)
    )
```

整个设计是：

```text
用户权限与审批配置
         |
         v
  合并 interrupt_on
         |
         v
 HumanInTheLoopMiddleware
         |
         v
 工具执行前的审批路径
```

到这里暂时只需要掌握装配关系。`interrupt()` 怎样暂停运行、批准后如何恢复、Checkpoint 在其中扮演什么角色，都留到第十章。

还有一个安全边界必须明确：文件根目录不是自动安全沙箱；工具级审批也不能代替底层执行环境的隔离策略。

---

## 九、State 与 Checkpointer：在什么阶段进入 Harness？

到这里，主 Agent 已经能够读写文件、压缩上下文、委派任务，并可根据配置拦截敏感工具调用。

但还有一个问题：

> 如果 Agent 在测试失败之后继续执行，消息历史存在哪里？如果审批需要暂停，恢复时依赖什么？

上一章已经知道 `create_agent()` 依赖 State。DeepAgents 在此基础上定义了一个自己的默认 State Schema。

### 9.1 真实源码：DeepAgentState

**真实源码节选：** [`graph.py` 第 73—76 行](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py#L73-L76)

```python
class DeepAgentState(AgentState):
    """AgentState with DeltaChannel on messages to reduce checkpoint growth from O(N²) to O(N)."""

    messages: Required[
        Annotated[
            list[AnyMessage],
            DeltaChannel(_messages_delta_reducer, snapshot_frequency=50),
        ]
    ]
```

**说明：** 将源码中的一行类型声明换行排版，语义保持不变，省略类型检查器注释。

为什么需要这一层？

假设第 1 轮消息有 10 条，第 2 轮 20 条，第 3 轮 30 条。如果每次 Checkpoint 都重复保存整份增长中的消息历史，持久化量可能持续扩大。`DeltaChannel` 的目的之一是降低这种重复写入的成本。

这里不急着展开增量合并算法。读者当前只需知道：**DeepAgents 为默认消息字段选择了更适合长任务的状态表示方式。**

### 9.2 真实源码：默认状态和自定义状态怎样进入 LangChain？

回看函数返回处：

```python
# 真实源码节选：graph.py 第 920—933 行
return create_agent(
    model,
    ...,
    context_schema=context_schema,
    checkpointer=checkpointer,
    store=store,
    ...,
    state_schema=state_schema if state_schema is not None else DeepAgentState,
)
```

三类东西的职责不同：

- `state_schema`：说明 Graph 管理哪些状态字段以及相应字段的更新语义。
- `checkpointer`：为执行状态保存、恢复提供持久化机制。
- `store`：提供独立的数据存储能力，可被需要它的 Backend 或应用组件使用。

它们不是同一个对象，也不会因为指定了其中一个就自动替代其他两个。

在前面的 `SubAgentMiddleware` 创建过程中，源码还传递了 `state_schema`；之后又会收集状态字段，并为 SubAgentMiddleware 设置私有状态字段信息。这说明主 Agent 与子 Agent 的状态边界也属于 Harness 装配的一部分，但本章只需识别这个装配入口即可。

复杂的 Reducer、DeltaChannel、Interrupt/Resume 语义留到第十三章集中研究。

---

## 十、最终一步：组件如何回到 `create_agent()`？

我们现在已经亲眼看到，为什么需要下列东西：

```text
Model           选择实际调用的模型
HarnessProfile  调整不同模型的 Harness 行为
Backend         指定文件与执行能力的底层环境
Filesystem      暴露文件工具
Summarization   管理长上下文能力
Subagents       提供任务委派入口
HITL            对敏感工具执行设置审批
State           规定执行状态结构
Checkpointer    支持状态持久化与恢复
```

接下来，不必再创造一种新 Loop。DeepAgents 只要把已装配的结果交给上一章研究的 LangChain Factory。

**真实源码：** [`graph.py` 第 920—942 行](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py#L920-L942)。

最关键的参数对应关系是：

| `create_agent()` 参数 | DeepAgents 传入的内容 |
|---|---|
| `model` | 已解析的模型实例 |
| `system_prompt` | `final_system_prompt` |
| `tools` | 经 Profile 调整的调用者工具 `_tools` |
| `middleware` | 已完成组装、替换和过滤的 `deepagent_middleware` |
| `state_schema` | 用户自定义 Schema 或默认 `DeepAgentState` |
| `context_schema` | 调用者传入的运行上下文类型 |
| `checkpointer` | 调用者提供的 Checkpointer |
| `store` | 调用者提供的 Store |
| `response_format` | 结构化输出设置 |

注意 `backend` 本身没有作为独立参数传给 `create_agent()`。原因并不神秘：它早已被装入 Filesystem、Summarization、Subagent 等具体 Middleware 的构造参数中。

类似地，`skills`、`memory`、`permissions` 也没有原封不动地传入 LangChain Factory，因为它们已经影响了 Middleware 的创建和配置。

于是装配流程可以完整地写成：

```text
调用者配置
  model / tools / backend / skills / memory / subagents / ...
                         |
                         v
                 create_deep_agent()
                         |
             +-----------+-----------+
             |                       |
             v                       v
       Model + Profile         Backend 选择
             |                       |
             +-----------+-----------+
                         |
                         v
              构建子 Agent 配置
                         |
                         v
            装配主 Agent Middleware
                         |
                         v
               合并、替换、过滤
                         |
                         v
                LangChain create_agent()
                         |
                         v
              构建并编译 StateGraph
                         |
                         v
                CompiledStateGraph
                         |
                         v
              LangGraph Runtime 执行
```

这是一张**构建与执行阶段的调用关系图**，不是单次模型请求的数据流图。

运行阶段依然是读者在第一至第六章已经掌握的结构：

```text
Model Node
    |
    v
是否产生 Tool Call？
    |
    +-- 否 --> 正常结束路径
    |
    +-- 是 --> ToolNode --> Middleware Wrapper --> 真正工具
                                              |
                                              v
                                         ToolMessage
                                              |
                                              +----> 下一轮 Model
```

DeepAgents 负责让这个 Loop **具备哪些能力**；LangChain 与 LangGraph 负责这些能力**怎样被连接和执行**。

---

## 十一、最小可验证实验：不调用模型，先验证装配思想

本章研究的是 Harness 构建阶段，因此第一项实验不需要 API Key。

我们先用纯 Python 重现 `_apply_custom_middleware()` 最重要的两个规则：同名原位替换、新名字插入核心栈之后。

### 11.1 实验 A：纯 Python 模拟装配规则

以下是**教学化简**，不是 DeepAgents 的原函数实现。它只验证列表位置规则，不验证真实 LangChain Hook、权限或工具调用。

```python
from dataclasses import dataclass


@dataclass
class MiniMiddleware:
    name: str
    source: str = "builtin"


def merge_middleware(base, custom, core_names):
    result = list(base)
    known_names = {m.name for m in base}
    replacements = {m.name: m for m in custom if m.name in known_names}
    novel = [m for m in custom if m.name not in known_names]

    for i, m in enumerate(result):
        if m.name in replacements:
            result[i] = replacements[m.name]

    if novel:
        pos = max(
            (i for i, m in enumerate(result) if m.name in core_names),
            default=len(result) - 1,
        ) + 1
        result[pos:pos] = novel

    return result


base = [
    MiniMiddleware("FilesystemMiddleware"),
    MiniMiddleware("SummarizationMiddleware"),
    MiniMiddleware("SkillsMiddleware"),
]

custom = [
    MiniMiddleware("FilesystemMiddleware", "custom"),
    MiniMiddleware("AuditMiddleware", "custom"),
]

merged = merge_middleware(
    base,
    custom,
    core_names={"FilesystemMiddleware", "SummarizationMiddleware"},
)

assert [(m.name, m.source) for m in merged] == [
    ("FilesystemMiddleware", "custom"),
    ("SummarizationMiddleware", "builtin"),
    ("AuditMiddleware", "custom"),
    ("SkillsMiddleware", "builtin"),
]

for m in merged:
    print(m.name, m.source)
```

**预期输出（非本章实际运行记录）：**

```text
FilesystemMiddleware custom
SummarizationMiddleware builtin
AuditMiddleware custom
SkillsMiddleware builtin
```

这项小实验可以验证一个设计直觉：Middleware 装配不是只能“在尾部追加”，还可以保留默认栈结构，提供可预测的定制插入位置。

它没有实现真实 `_apply_custom_middleware()` 的权限冲突检查、Profile 过滤、重复命名限制及其他分支，不能代替框架测试。

### 11.2 实验 B：用真实 DeepAgents 检查 Graph 是否构建

现在才进入真实框架。

```bash
pip install "deepagents==0.7.32" "langchain==1.4.7" langchain-openai
```

以下示例需要已正确设置相应模型提供商的 API Key，且依赖版本能够兼容。它用于**构建与检查 Graph**，不会主动运行退款任务或调用 `pytest`。

```python
from deepagents import create_deep_agent

agent = create_deep_agent(
    model="openai:gpt-5.5",
    system_prompt="你是退款系统代码审查助手。修改前先理解现有逻辑。",
)

print(type(agent))
print(sorted(agent.get_graph().nodes.keys()))
```

观察重点不是打印结果恰好有哪些 Middleware 名称，而是：

1. 返回对象是否是可执行的编译 Graph。
2. 是否存在构成 Agent Loop 的模型和工具节点。
3. 自定义的 Middleware 是否通过 LangChain Factory 编译为相应节点或 Wrapper。

**验证边界：** 我们没有在本文中实际运行该环境，因此不伪造 `print()` 输出，也不宣称退款 Bug 已修复。更不能用“Graph 构建成功”证明 `pytest` 已通过。

如果要真正完成贯穿任务，还必须提供能够访问项目文件、允许受控命令执行的 Backend，并让测试输出回到 Agent Loop。这将作为后续各章的综合实验。

---

## 十二、回看装配过程：为什么最终会长成 Agent Harness？

现在可以从最初的需求重新推导整个系统，而不需要背组件清单。

一开始，模型只需要回答退款计算问题，单次模型调用就可以给出建议。

接着，用户要求修改真实项目。模型需要 Tool Calling，工具结果又必须反馈给模型，于是出现 Agent Loop。

然后，我们要把它用于不同环境中的项目文件。直接用 `Path.read_text()` 不再通用，于是将文件工具和 Backend 分开，由 FilesystemMiddleware 提供统一接口。

任务变长后，上下文管理成为真实问题，于是装配 SummarizationMiddleware。

一个 Agent 承担所有分析会让上下文与职责过度集中，于是准备子 Agent 配置，通过 SubAgentMiddleware 提供委派能力。

不同模型可能需要不同的 Prompt 和工具行为，于是将模型相关的 Harness 差异提取到 HarnessProfile。

Middleware 越来越多后，简单追加失去可预测性，于是需要核心栈、同名替换、位置插入及 Profile 过滤。

敏感工具执行不能只凭模型意图，于是通过权限配置与 HumanInTheLoopMiddleware 接入审批机制。

长任务还需要 State 与 Checkpointer，于是 `create_deep_agent()` 为 `create_agent()` 传入默认或自定义状态结构，以及可选的持久化设施。

最后我们发现，这些步骤都没有要求 DeepAgents 新写一套 Model → Tool → Model 循环。

因此：

> **Agent Harness 是以通用 Agent Loop 为执行核心，在构建阶段组织模型、工具、扩展策略与执行基础设施，在运行阶段让这些能力按已编译的规则协同工作的系统。**

对于我们固定阅读的 DeepAgents 0.7.32，可以更具体地说：

> **`create_deep_agent()` 是 Harness 的装配入口；`create_agent()` 是 Agent Graph 的构建入口；LangGraph Runtime 是执行入口。**

三个层次分别回答：

```text
DeepAgents：装什么能力？
LangChain：怎样把能力组织成 Graph？
LangGraph：Graph 怎样真正运行、更新状态与恢复？
```

### 12.1 本章设计边界

需要避免几个很容易出现的误解：

| 容易产生的误解 | 准确理解 |
|---|---|
| DeepAgents 自己重新实现了 Agent Loop | 核心 Loop 由 LangChain 构建、LangGraph 执行 |
| Backend 就是文件工具 | Backend 是文件/执行能力的底层抽象，工具由 Middleware 暴露 |
| 默认 Agent 一定能运行 pytest | `execute` 取决于 Backend 的实际协议和执行能力 |
| 所有 Middleware 默认都安装 | Skills、Memory、HITL、AsyncSubAgent 等受条件控制；Todo 不是此版本默认组件 |
| 自定义 Middleware 总是放在最后 | 存在按名称原位替换、核心栈之后插入等规则 |
| Middleware 列表顺序就是完整执行顺序 | Hook 节点与 Wrapper 的执行规则由 LangChain Factory 确定 |
| Graph 构建成功等于任务成功 | 构建只准备能力，正确性仍取决于实际执行和测试反馈 |
| Checkpoint 自动保证外部副作用恰好执行一次 | 状态恢复与真实外部副作用需要分别设计 |

---

## 十三、下一章：模型每一轮究竟看见什么？

本章解决了一个构建时的问题：

> DeepAgents 怎样在通用 Agent Loop 的基础上装配完整 Harness？

我们已经知道，`SummarizationMiddleware`、`SkillsMiddleware` 与 `MemoryMiddleware` 可以被加入最终的 Middleware 列表。

但这只说明它们**存在**，还没有解释它们在一次实际模型请求中**怎样改变输入**。

例如退款任务执行到第十五轮时：

```text
已有 15 轮模型和工具交互
测试日志超过原来的上下文预算
项目提供了额外的 Skills 指令
Memory 中保存了金额精度规则
```

那么模型下一次调用时究竟看见：

- 全部原始消息，还是摘要后的历史？
- 哪些 Skills 指令被加载？
- Memory 从哪里读取、怎样加入系统消息？
- 持久化 State 与实际发送给模型的请求有什么区别？

这就是第八章的核心问题：

**模型输入如何被增强？——Context、Skills 与 Memory。**

下一章将从一次具体的 Model Request 出发，逐步追踪它经过 Middleware 后发生的变化，而不再停留于构建阶段的装配列表。

---

## 附：本章主要源码索引

以下链接都固定在 `e85c6b9`，可作为复核入口。

| 研究问题 | 源码位置 |
|---|---|
| `create_deep_agent()` 函数签名 | [`graph.py#L262-L282`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py#L262-L282) |
| Model 解析与 HarnessProfile 选择 | [`graph.py#L563-L589`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py#L563-L589) |
| Backend 默认选择、系统指令处理 | [`graph.py#L596-L614`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py#L596-L614) |
| 自定义子 Agent 预处理 | [`graph.py#L632-L754`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py#L632-L754) |
| 默认 general-purpose 子 Agent | [`graph.py#L755-L820`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py#L755-L820) |
| 主 Agent 的核心 Middleware | [`graph.py#L821-L856`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py#L821-L856) |
| Profile、Skills、Memory、HITL 装配 | [`graph.py#L857-L884`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py#L857-L884) |
| Middleware 排除、替换与工具过滤 | [`graph.py#L885-L901`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py#L885-L901) |
| 自定义 Middleware 按名称合并 | [`graph.py#L195-L234`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py#L195-L234) |
| 最终交给 `create_agent()` | [`graph.py#L920-L942`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py#L920-L942) |

**本章核心产出：** 能够从固定 Commit 的源码出发，解释 DeepAgents 是怎样一步步完成 Harness 装配，并准确区分 DeepAgents、LangChain 与 LangGraph 的职责。
