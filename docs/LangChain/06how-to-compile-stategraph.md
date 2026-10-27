---
title: "05｜Graph 定义好了，为什么还不能直接执行？"
date: 2026-10-27
slug: /langchain-langgraph-source/how-to-compile-stategraph
description: "从退款测试工具不应在图构建阶段执行这一可观察边界出发，精读 LangGraph 1.2.17 的 StateGraph.compile、CompiledStateGraph 与 attach_node/attach_edge/attach_branch：构建描述如何被装配为可执行对象。"
tags:
  - LangChain
  - LangGraph
  - StateGraph
  - CompiledStateGraph
  - Pregel
  - 源码精读
  - Python
---

> 本文是「LangChain × LangGraph 源码精读」的**第 05 章**，对应文件 `06how-to-compile-stategraph.md`。建议先阅读[第 04 章：一段 Agent Loop，怎样变成 StateGraph 的节点与边？](/langchain-langgraph-source/how-to-build-stategraph)。
>
> **本章只回答一个问题：** `StateGraph` 已经收集了节点、固定边和条件分支，为什么还需要 `compile()`？它如何把这些描述装配成真正可调用的对象，**又为什么不会在编译时就运行节点**？

在上一章，我们终于能用图描述退款问题的 Agent Loop：`model` 决定是运行测试、应用修复，还是结束；`tools` 负责在隔离的临时项目里执行相应操作；模型节点后面的条件分支决定下一步去哪里。

看起来，整个 Agent 已经定义完了。

然而，考虑一个具体场景：业务要求先构建 Agent，等用户真正提交“检查退款精度”任务后才开始运行 `pytest`。如果 `add_node("tools", tools_node)` 或 `compile()` 就执行了工具，那么**仅仅创建 Agent 就会修改文件或执行命令**。显然，这不是我们想要的行为。

于是，一个真正需要解决的问题出现了：**如何事先准备好一套执行关系，却把真正的节点调用留到稍后的运行阶段？**

这正是我们阅读 `StateGraph.compile()` 的起点。它不负责判断 `refund.py` 是否正确，也不替模型作决定；它完成的是另一件事：把“描述中的图”转换为“运行时能够解释和执行的结构”。

<!-- truncate -->

## 1. 先把“描述程序”和“运行程序”分开

我们暂时不用 LangGraph。先在脑海里保留一个最小场景：`tools_node()` 一旦真正调用，就会运行测试，甚至可能写入文件。我们希望以下代码只准备工作流程：

```python
# 概念伪代码：接口并非 LangGraph 原样实现。
builder.add_node("tools", tools_node)
builder.add_edge("START", "tools")
program = builder.compile()

# 直到调用 program.invoke(...)，才允许发生真实工具执行。
```

这不是语法上的小区别，而是一条非常重要的**副作用边界**。

如果图在构建时就执行节点，会产生几个马上可观察的矛盾。

第一，创建图时还没有用户本轮的输入。`tools_node()` 不知道项目目录，无法知道这一次应当测什么。第二，同一个图可能要处理多次请求；构建一次和运行多次并非同一件事。第三，错误发生时我们需要知道是图定义有问题，还是执行中测试失败，而不希望两种错误混在 `compile()` 里。

因此，可以把我们需要的职责分成两步：

```text
构建阶段                                  运行阶段

登记 model / tools                         接收本轮输入
登记 START / 条件分支 / END                真正调用 model
                 │                         按模型结果触发 tools
                 ▼                         得到工具反馈并继续
             compile()                            ▲
                 │                                │
                 └───── 可调用的图对象 ── invoke() ┘
```

请留意：这张图表达的是**构建与运行的阶段边界**，不是 LangGraph 的底层调度时序。Runtime 内部怎样选择节点，我们下一章再看。

### 一次无需框架的最小验证

为了亲眼确认我们为什么需要这条边界，可以写一个很小的教学版构建器。它只负责检查入口、记录节点和边，然后返回一份执行计划；它**不**实现 LangGraph 的状态传播，也**不是** LangGraph 的源码。

```python
# 教学化简：只演示“装配不会执行工具”，不模拟 Pregel。
from dataclasses import dataclass
from typing import Callable

calls = []


def run_tests(state: dict) -> dict:
    calls.append("pytest_invoked")
    return {"test_exit_code": 1}


@dataclass
class Plan:
    nodes: dict[str, Callable]
    entry: str


def compile_spec(nodes: dict, edges: set[tuple[str, str]]) -> Plan:
    targets = [end for start, end in edges if start == "START"]
    if len(targets) != 1 or targets[0] not in nodes:
        raise ValueError("缺少唯一且有效的 START 入口（教学限制）")
    return Plan(nodes=nodes.copy(), entry=targets[0])


plan = compile_spec(
    {"tools": run_tests},
    {("START", "tools"), ("tools", "END")},
)
assert calls == []   # 这里只是在准备执行计划

plan.nodes[plan.entry]({})  # 我们显式调用了工具函数
assert calls == ["pytest_invoked"]
```

简单到几乎不需要框架，但它已经暴露出一个值得坚持的原则：**有函数可供执行，不意味着现在就必须执行。**

真实 `StateGraph` 也沿着这条边界工作。不过，相比这份教学版，它还必须把节点之间的数据读取、状态写回和下一步的触发关系准备好。接下来我们逐层进入源码。

## 2. 先固定研究对象，避免把别的版本混进来

本系列继续使用引言中约定的源码基线：

| 项目 | 固定基线 | 本章真实入口 |
| --- | --- | --- |
| LangGraph | `1.2.17`；Commit `4c52e129eb1ab69a3da58b799f67929f77d81149` | [`graph/state.py`：`StateGraph.compile()`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/graph/state.py#L1087-L1297) |
| LangGraph | 同一 Commit | [`graph/state.py`：`CompiledStateGraph`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/graph/state.py#L1299-L1507) |
| LangGraph | 同一 Commit | [`pregel/main.py`：`Pregel.invoke()`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/main.py#L3704-L3790) |
| LangChain | `1.4.7`；Commit `fbb8a35da7023ac0382c3b22f047337861bd3486` | [`agents/factory.py`：`create_agent()` 编译并返回图的部分](https://github.com/langchain-ai/langchain/blob/fbb8a35da7023ac0382c3b22f047337861bd3486/libs/langchain_v1/langchain/agents/factory.py#L1754-L1770) |

源码路径以固定 Commit 为准。本文中的代码分为三类：

- **真实源码节选**：保持固定 Commit 的原文，仅截出支撑当前结论的极短片段，提供精确行号。
- **教学化简**：用自己的小代码表达某一职责，不声称内部实现与框架一致。
- **实验代码**：使用公开 API，可以复制运行；本文会分别说明哪些已执行、哪些仅核对过语法和接口。

我们并不需要马上学习所有 `compile()` 参数。当前正常路径没有开启检查点、缓存或中断；相关参数只在源码导航中出现，不在本章展开。

## 3. 为什么编译要先验证图？

回到上一章的退款测试图。如果不小心忘了添加 `START → model`，就算 `model` 和 `tools` 两个函数本身完全正确，运行时也不知道从哪里开始。

把这种错误拖到第一次执行 `pytest` 时才报告，不仅晚，而且不必要。它与业务输入无关：**没有入口是图定义本身的问题**。

因此，构建器需要在交付可执行对象之前检查结构。

在固定版本的 [`StateGraph.validate()`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/graph/state.py#L1042-L1086) 中，可以沿着以下顺序阅读：

1. 汇总来自固定边、条件分支等结构的可能起点。
2. 检查这些起点是否是已登记节点或 `START`。
3. 检查**至少有一条来自 `START` 的入口连接**。
4. 检查能静态确定的目标节点是否存在，并检查配置的中断节点。
5. 完成构建器的结构验证。

它能提前拦截的是**静态结构错误**，并不是运行时所有错误。例如模型可能不断要求重试测试，或某个分支路径函数在运行时返回不合适的目标；这并不能仅凭编译阶段的一次静态检查全部证明安全。

真实 `compile()` 经过参数准备后会明确调用 `self.validate(...)`。注意这里存在一个很容易忽视的设计区别：**校验图定义，不等于执行节点函数来测试它们是否正确**。

我们可以在真实框架的公开接口上针对这个边界写一个最小反例：

```python
# 框架实验；当前写作环境没有 langgraph，尚未实际运行。
from typing import TypedDict
from langgraph.graph import StateGraph


class FlowState(TypedDict):
    value: int


calls = []


def model_node(state: FlowState) -> dict:
    calls.append("model")
    return {"value": state["value"] + 1}


builder = StateGraph(FlowState)
builder.add_node("model", model_node)

try:
    builder.compile()  # 没有 START 入口
except ValueError as exc:
    print(type(exc).__name__, str(exc))
else:
    raise AssertionError("应当拒绝缺少入口的图")

assert calls == []  # 即使校验失败，也不应调用 model_node
```

在此基线源码中，缺少 `START` 会触发 `ValueError`。**这里展示的是根据源码推导的预期行为，不是本环境已经运行的框架输出。**

这个问题解决后，`compile()` 至少知道图定义满足必要的基本约束。不过，仅有一份“已经合法”的节点和边的列表，仍然不足以让 Runtime 直接执行；必须把列表装配成执行器认识的格式。

## 4. `compile()` 的主路径：不是执行，而是装配

现在进入本章中心：[`StateGraph.compile()`，`state.py:1087–1297`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/graph/state.py#L1087-L1297)。

我们不按源文件从第一行读到最后一行。先只抽出对正常执行最重要的五个设计动作：

```text
已登记的 StateGraph
      │
      ├─ 检查图结构是否合法
      │
      ├─ 确定输入、输出和可传递的状态字段
      │
      ├─ 创建 CompiledStateGraph（此时还没有附着所有用户节点）
      │
      ├─ 把节点、边、条件分支装配进去
      │
      └─ 校验装配好的可执行结构，并返回
```

这张图中的每个动作都能在固定源码中找到对应位置：

| 动作 | 关键位置 | 它解决什么问题 |
| --- | --- | --- |
| 图结构验证 | `state.py:1163–1170` | 没有入口或指向未知节点，应该尽早失败 |
| 明确输出与流数据范围 | `state.py:1171–1188` | 执行结果要从哪些状态字段取出 |
| 创建可执行对象 | `state.py:1245–1269` | 将构建描述交给执行器能理解的对象 |
| 装配节点 | `state.py:1271–1273` | 把登记过的函数变成运行时节点 |
| 装配边与条件分支、再次验证 | `state.py:1287–1297` | 把“下一步去哪儿”变成可使用的触发关系 |

在源码中，我们可以取一个足以证明“装配而非运行”的极短真实节选：

```python
# 真实源码节选：state.py:1271–1273
compiled.attach_node(START, None)
for key, node in self.nodes.items():
    compiled.attach_node(key, node)
```

这里有三个非常具体的事实。

**第一，`self.nodes` 属于构建器。** 上一章我们通过 `add_node()` 登记的 `model_node` 和 `tools_node`，此刻才被逐个拿出来交给编译产物。

**第二，编译产物有自己的 `nodes`。** 这不是把 `StateGraph` 原封不动返回。图描述中的节点说明需要被转换成执行器能够识别的对象，后面才能建立输入读取、输出写回和触发关系。

**第三，循环调用的是 `attach_node()`，不是节点自身的 `invoke()` 或 `tools_node()`。** 这就回答了最开始的疑问：当我们创建 Agent 时，实际上是在**准备怎样运行工具**，而不是此时就执行测试。

如果把 `compile()` 理解为“将 Python 代码编译成另一份机器代码”，就会误读这一步。对这条路径，更准确的理解是：**组装一份供现有运行引擎使用的执行结构**。它类似制定执行计划，但比简单的邻接表多了状态读写和触发机制。

## 5. 为什么它要生成 `CompiledStateGraph`，而不是返回自己？

到这里，读者可能会问：既然 `StateGraph` 已经有 `nodes`、`edges`、`branches`，为什么不直接给这个类加一个 `invoke()`？

技术上当然可以写一个最小图执行器，这也是我们前几章手写 Loop 能成立的原因。但真实框架面对的是另一种职责分工：**构建阶段维护“用户想要什么图”；运行阶段维护“这一轮有哪些节点能够运行、读什么、写什么”。**

在固定源码的 [`state.py:1299–1315`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/graph/state.py#L1299-L1315) 中，`CompiledStateGraph` **继承自 `Pregel`**。

现在只需要知道 Pregel 的最小含义：**它是 LangGraph 用来承载图执行能力的运行时类**。在本章，它不是一个我们需要背诵的新算法；它出现是为了回答：`CompiledStateGraph` 的 `invoke()` 能力从哪里来？

这条继承关系意味着，构建器交付的对象不再只是一套编辑中的图说明，而是一个拥有执行接口、能够接收输入的对象。固定基线中，`Pregel.invoke()` 定义在 [`pregel/main.py:3704` 起](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/main.py#L3704-L3790)。沿源码再往下可以看到它通过流式执行接口收集结果。

但现在不要顺着这个函数继续跳到执行循环内部，否则会突然出现一大串我们尚未遇到问题的 Runtime 术语。本章只需要确认**接口归属**：

```text
StateGraph                     CompiledStateGraph
（编辑中的定义）                  （可调用的执行对象）

nodes / edges / branches  ───▶  nodes / channels / triggers
                                      │
                                      └── 继承 Pregel 的运行接口
                                             │
                                         invoke(...)  ← 下一章追踪
```

一个小问题还有待回答：编译产物为什么不仅记录节点名，还需要记录 `channels` 和 `triggers`？这是本章唯一需要向底层再走近的一步。

## 6. 把节点登记为“可以被触发的任务”

先想 `tools` 节点。它在图上看起来只是一个写着 `tools` 的方框。但当 Runtime 真正开始一次运行时，必须知道三个问题：

- **什么时候运行它？** 例如模型刚刚选择了 `run_tests`。
- **给它什么数据？** 例如 `project_dir`、`action` 等当前状态。
- **运行完把结果交到哪里？** 例如 `test_exit_code` 回到图状态，供模型的下一轮决策使用。

这就解释了为什么编译产物中的一个节点，比 `{"tools": tools_node}` 更丰富。

阅读 [`CompiledStateGraph.attach_node()`，`state.py:1335–1436`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/graph/state.py#L1335-L1436)，重点观察它创建的 `PregelNode` 所承载的几类信息：

| 运行时节点的部分 | 在当前案例中如何理解 |
| --- | --- |
| `bound` | 真正绑定的 `tools_node` 可调用对象；本章尚未执行它 |
| `channels` | 执行时从哪些状态字段读取输入 |
| `triggers` | 哪个信号出现时，这个节点有机会被调度 |
| `writers` | 节点完成后应将更新写往何处 |

读到这里第一次出现了 `Channel`。目前把它理解成**运行时用来传递状态值或触发信号的载体**就够了。它不等同于“消息列表”，也不是所有状态字段都一定采用相同的更新规则；不同 Channel 的细分设计，我们会在状态章节展开。

为什么需要在编译时确定这些关系？因为如果所有节点每次执行都靠运行时重新扫描整份构建描述，运行引擎就难以用统一方式读取状态、调度节点并收集写入。编译把这部分结构先组织好，使执行阶段可以依据已装配的对象工作。

以当前图为例，`model`、`tools` 不是两个立刻运行的普通 Python 函数，而是两个**带着读取、写回和触发信息的待运行节点**。

### 一个与上一章对应得上的细节

在固定源码 [`state.py:1412–1433`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/graph/state.py#L1412-L1433) 中，对普通节点会构造形如 `branch:to:tools` 的目标触发通道，并把它放到该节点的触发条件里。

这个内部名字并不需要记忆。重要的是它回答了一件以前不太明确的事：**一条指向 `tools` 的边最终不是画在图上的线，而是执行器能够识别的触发关系。**

不过，仅仅装配好了目标节点，还缺少一半：究竟是谁产生“请运行 tools”的触发信号？

## 7. 固定边和条件分支，编译时分别做了什么？

上一章的图存在两种跳转：

```text
START ──▶ model
            │
            ├─ 要调用工具 ──▶ tools ──▶ model
            │
            └─ 已完成 ─────▶ END
```

这两类箭头在图上长得类似，但程序含义不同。

### 7.1 固定边：把确定关系装配成写入目标触发信号的动作

从 `tools` 回到 `model` 的关系，在我们的最小示例里没有判断条件。源码中的 [`CompiledStateGraph.attach_edge()`，`state.py:1437–1461`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/graph/state.py#L1437-L1461) 会为普通单起点边配置触发目标节点的写入动作。换句话说，它让执行器知道：**当 `tools` 完成并进入相应状态更新流程后，下一步应该触发 `model`。**

这里不是当场调用 `model_node()`，而是把“完成 `tools` 后该触发谁”登记到执行结构里。这一差别，正是编译与执行的分界。

源码也支持多个起点汇合再触发某个目标节点；那会需要等待多个前置节点的机制。我们现在的退款任务没有这个需求，先不展开。

### 7.2 条件分支：登记稍后计算路由的方法

`model` 的去向不同：它可能请求工具，也可能已经完成。我们不能在构建时就选定 `tools` 或 `END`，因为**构建时还没有这轮模型的结果**。

因此，编译条件分支时不能执行路径判断函数，而只能保存一个**稍后运行的路由动作**。

阅读 [`CompiledStateGraph.attach_branch()`，`state.py:1462–1507`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/graph/state.py#L1462-L1507)，它会将 `BranchSpec` 形成的可运行写入动作装配到前驱节点的 `writers` 中。真正的路径函数调用位于 [`BranchSpec._route()`，`graph/_branch.py:138–159`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/graph/_branch.py#L138-L159) 的运行阶段。

于是，`model` 本轮产生 `{"action": "run_tests"}` 后，条件路由才可能得到 `tools`；产生 `{"action": "finish"}` 后，路由才会选择结束。这不是 `compile()` 事先能决定的事情。

还有一个必须说清的边界：选择 `END` 表示**结束这条执行路径**，不意味着有一个会执行用户代码的 `END` 节点。对我们当前的普通分支，结束时不会再触发 `tools`。

这就完成了从“图上的连线”到“执行器实际理解的触发/路由关系”的最小解释。

## 8. 装配结束后为什么还要再验证一次？

我们已经看到 `StateGraph.validate()` 对构建描述进行结构检查。那么，为什么 [`compile()` 末尾](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/graph/state.py#L1287-L1297) 还要调用一次 `compiled.validate()`？

两次验证并不是简单的重复。

第一次验证的是**用户声明的图**：有没有入口、边是否指向不存在的节点、静态可知的分支目标是否合理。

第二次面对的是**已经装配出来的运行对象**：节点是否拥有合法的触发与读写关系、输入输出 Channel 的配置是否有效、能否构成有效的调度结构。固定版本的运行时校验见 [`Pregel.validate()`，`pregel/main.py:937–952`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/main.py#L937-L952)。

这里真正值得理解的设计逻辑是：**图定义合法，不自动等于装配产物合法。** 前者约束用户提交的关系，后者约束执行器实际接收的结构。

当然，即使这两关都通过，也不代表 `pytest` 一定成功，更不代表模型会采取正确修复。这两次校验都不是业务正确性验证。

## 9. 回到真实退款案例：编译完以后，文件应该一字未改

前面我们只靠源码推导出 `compile()` 不应该执行工具。接下来需要一个能被观察的实验，不让结论只停留在口头。

继续使用同一个临时项目：

- `refund.py` 最初使用浮点减法，`0.3 - 0.2` 不能满足严格等于 `0.1` 的测试。
- `model_node` 是脚本化假模型：根据最近一次工具结果决定 `run_tests`、`apply_fix` 或 `finish`。
- `tools_node` 通过子进程运行真实 `pytest`，或把本实验预设的 `Decimal` 补丁写入临时目录中的 `refund.py`。

这章要验证的主要断言不是“最终测试通过”，而是更靠前的两个：

1. 调用 `builder.compile()` **之后**，两个节点的调用记录仍为空，`refund.py` 仍是有缺陷的版本。
2. 调用 `graph.invoke(...)` **之后**，才观察到节点调用、测试失败、文件修复与测试通过。

> **边界说明**：以下是公开 API 实验，不是 LangGraph 内部源码重写。本环境没有安装固定版本的 `langgraph`，因此只检查了示例的 Python 语法；本文不能声称已实际运行这个框架实验。文中给出的轨迹是按脚本逻辑和源码推导的**预期轨迹**。附录中纯 Python 编译边界实验与单独的真实 `pytest` 测试材料，则已经运行。

### 9.1 安装实验依赖

建议使用独立虚拟环境：

```bash
python -m venv .venv
# macOS / Linux
source .venv/bin/activate
# Windows PowerShell 请改用 .venv\Scripts\Activate.ps1

python -m pip install "langgraph==1.2.17" "pytest>=8,<10"
python -m pip freeze > requirements-resolved.txt
```

`langgraph==1.2.17` 是本系列用于源码定位的版本；测试代码无需外部模型 API Key。`pip freeze` 的作用是保存你实际安装的传递依赖版本，便于日后复现。

### 9.2 可复制的框架实验

保存为 `demo_compile_boundary.py`：

```python
"""公开 API 实验：检查 compile() 和 invoke() 的副作用边界。"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph


BROKEN = """def refund_amount(price: float, coupon: float) -> float:
    return price - coupon
"""

FIXED = """from decimal import Decimal


def refund_amount(price: float, coupon: float) -> float:
    return float(Decimal(str(price)) - Decimal(str(coupon)))
"""

TEST = """from refund import refund_amount


def test_refund_exact_money():
    assert refund_amount(0.3, 0.2) == 0.1
"""


class FlowState(TypedDict, total=False):
    project_dir: str
    action: str
    last_tool: str
    test_exit_code: int
    trace: list[str]
    summary: str


# 脚本化假模型：用固定决策验证运行路径，不具备模型推理能力。
node_calls: list[str] = []


def model_node(state: FlowState) -> dict:
    node_calls.append("model")
    last = state.get("last_tool")
    if last is None:
        return {"action": "run_tests"}
    if last == "run_tests" and state["test_exit_code"] != 0:
        return {"action": "apply_fix"}
    if last == "apply_fix":
        return {"action": "run_tests"}
    if last == "run_tests" and state["test_exit_code"] == 0:
        return {"action": "finish", "summary": "脚本判断：测试通过"}
    raise RuntimeError(f"无法处理工具结果: {last!r}")


def tools_node(state: FlowState) -> dict:
    node_calls.append("tools")
    root = Path(state["project_dir"])
    action = state["action"]

    if action == "run_tests":
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_refund.py"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        )
        update = {"last_tool": "run_tests", "test_exit_code": result.returncode}
        event = f"run_tests exit_code={result.returncode}"
    elif action == "apply_fix":
        # 只修改本实验的临时目录，不接触真实业务项目。
        (root / "refund.py").write_text(FIXED, encoding="utf-8")
        update = {"last_tool": "apply_fix"}
        event = "apply_fix patch_applied"
    else:
        raise ValueError(f"未授权的工具动作: {action}")

    return {**update, "trace": state.get("trace", []) + [event]}


def route_after_model(state: FlowState) -> Literal["tools", "finish"]:
    return "finish" if state["action"] == "finish" else "tools"


def main() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "refund.py").write_text(BROKEN, encoding="utf-8")
        (root / "test_refund.py").write_text(TEST, encoding="utf-8")

        builder = StateGraph(FlowState)
        builder.add_node("model", model_node)
        builder.add_node("tools", tools_node)
        builder.add_edge(START, "model")
        builder.add_conditional_edges(
            "model", route_after_model, {"tools": "tools", "finish": END}
        )
        builder.add_edge("tools", "model")

        assert node_calls == []
        assert (root / "refund.py").read_text(encoding="utf-8") == BROKEN

        graph = builder.compile()
        # 本章最重要的两个断言：compile 尚未执行节点或修复文件。
        assert node_calls == []
        assert (root / "refund.py").read_text(encoding="utf-8") == BROKEN
        print("编译完成：节点调用 0 次；refund.py 未修改")

        final = graph.invoke({"project_dir": str(root), "trace": []})
        assert final["action"] == "finish"
        assert final["trace"] == [
            "run_tests exit_code=1",
            "apply_fix patch_applied",
            "run_tests exit_code=0",
        ]
        assert node_calls == [
            "model", "tools", "model", "tools", "model", "tools", "model"
        ]
        assert (root / "refund.py").read_text(encoding="utf-8") == FIXED
        print("\n".join(final["trace"]))
        print("调用节点总数:", len(node_calls))
        print(final["summary"])


if __name__ == "__main__":
    main()
```

运行：

```bash
python demo_compile_boundary.py
```

**预期观察**（尚不是本文环境里实际得到的框架输出）：

```text
编译完成：节点调用 0 次；refund.py 未修改
run_tests exit_code=1
apply_fix patch_applied
run_tests exit_code=0
调用节点总数: 7
脚本判断：测试通过
```

这段实验故意把 `node_calls` 与 `refund.py` 内容的断言放在 `compile()` 前后。一旦某个错误实现提前执行了 `model_node`、`tools_node`，甚至发生了文件副作用，断言会立即失败。

它还控制了两个容易误导学习者的变量：**脚本化假模型不代表模型智能**，以及 **pytest 的通过证明测试样例通过，并不证明所有退款业务场景正确**。

### 9.3 怎样准确看待这次验证？

实验最终成功时，可以支持的结论是：

- 在当前框架版本及这条执行路径上，`compile()` 不调用用户注册的节点，也不执行这些节点中的子进程与写文件动作。
- `invoke()` 进入后，节点按我们的任务状态逐步运行，产生可观察的 `pytest` 失败、修复和再次测试。

但这个实验**不能**证明图的所有分支正确、并发更新安全或外部副作用可以自动回滚。比如实际工程中的 `apply_fix` 属于文件系统副作用，图编译器并不会自动提供文件权限隔离、变更审核或撤销保证。

因此，我们研究的是**一个执行阶段边界**，不是宣称编译图后就获得了生产级安全。

## 10. 回到 LangChain：`create_agent()` 在这里交付了什么？

现在可以重新理解第 03 章里那句关键的 `create_agent()`：它**返回一个编译好的图**，而不是返回一次已经发生的模型回答。

在固定版本的 [LangChain Agent Factory](https://github.com/langchain-ai/langchain/blob/fbb8a35da7023ac0382c3b22f047337861bd3486/libs/langchain_v1/langchain/agents/factory.py#L1754-L1770) 中，`create_agent()` 在配置完成模型节点、工具节点与路由后，调用 `graph.compile(...)` 并返回可运行对象。

因此，把我们前面几章连起来，就得到现在足以理解的一小段架构：

```text
LangChain: create_agent(...)
       │
       ├─ 决定需要哪些节点与路由
       │
       ▼
LangGraph: StateGraph
       │
       ├─ 描述状态、节点、固定边与条件分支
       │
       ▼
StateGraph.compile()
       │
       ├─ 校验构建描述
       ├─ 装配节点、状态读写与触发关系
       │
       ▼
CompiledStateGraph（具备执行接口）
       │
       └── 用户调用 invoke(...) 才进入真正的执行过程
```

这张图不再是架构图先行。我们已经分别在前面几章弄清楚：为什么需要工具执行、为什么需要反馈循环、为什么需要把循环表达成节点与路由；现在又弄清楚为什么必须把**编辑中的图描述**转换为**运行时对象**。

这也解释了一个常见误会：`compile()` 不是“把 Agent 训练好”；它不让模型更聪明，不决定测试应该如何修复，也不会自动调用真实工具。它是在准备**谁能够执行、在什么条件下被触发、结果怎样交回运行机制**。

## 11. 回看这一次演化，并留下下一章真正的问题

从最早的手写 `while` 到现在，我们没有因为喜爱抽象才无端引入编译层。

最初，一段 `while` 同时负责所有事情；当我们要明确结束条件，才把固定执行动作与条件路由分开；当我们把两者交给 `StateGraph`，又面临新的问题：构建器保存的是一份**尚未运行的关系描述**。要在用户任务到来时复用它，就需要先完成结构校验和装配，使运行对象知道节点、状态输入输出与触发关系。

所以，本章要带走的结论可以压缩成一句话：

> **`StateGraph.compile()` 不是执行图，而是把图的声明装配为 `CompiledStateGraph`；后者继承运行引擎的调用能力，直到执行接口收到输入，节点才真正开始运行。**

现在，读者应该能回答五个具体问题：

1. 为什么注册一个工具节点不能直接运行工具？因为那时还没有本轮运行输入，且构建与执行应当分离。
2. 为什么 `compile()` 需要校验？因为缺少入口之类的错误属于图结构错误，可以提前发现。
3. `CompiledStateGraph` 与 `StateGraph` 有何不同？前者把声明装配成带读写、触发和运行接口的对象。
4. 普通边与条件分支在编译时被如何处理？前者装配确定的目标触发，后者装配将来根据运行结果决定去向的动作。
5. 为什么 `compile()` 成功不等于退款测试通过？因为节点在编译阶段尚未运行，图结构合法也不能替代业务正确性。

但一个新问题已经自然出现：**既然 `CompiledStateGraph` 已经准备好节点、状态读写和触发关系，当我们真正调用 `graph.invoke(...)` 时，它究竟怎样启动第一个节点、收集这轮结果、再决定下一轮要运行谁？**

这就是下一章的主题：**第 06 章《一次 `invoke()`，究竟是谁让 model 和 tools 轮流运行？》**。建议文件名：`07how-to-run-pregel-loop.md`。

我们将在下一章沿 `Pregel.invoke()`、`stream()` 和执行循环，追踪一次正常的模型—工具—模型调度轨迹；而不是在本章就提前堆砌并发、Channel 更新算法、Checkpoint 和恢复机制。

---

## 附录 A：真正已经执行的纯 Python 对照实验

为了不把“已核对源码”误说成“已执行框架”，这里保留一个不依赖 LangGraph 的可复制小实验。它**只证明教学上的编译/调用阶段区分**，并不模拟真实 Runtime 的执行语义。

保存为 `verify_compile_boundary.py`：

```python
"""教学化简：只验证“校验/装配不会调用节点”，不是 LangGraph 实现。"""
from dataclasses import dataclass
from typing import Callable

executions: list[str] = []


def dangerous_tool(state: dict) -> dict:
    executions.append("ran_tool")
    return {"status": "tool_done"}


@dataclass
class TinyPlan:
    nodes: dict[str, Callable]
    entry: str


class TinyBuilder:
    def __init__(self):
        self.nodes: dict[str, Callable] = {}
        self.edges: set[tuple[str, str]] = set()

    def add_node(self, name: str, action: Callable):
        self.nodes[name] = action

    def add_edge(self, start: str, end: str):
        self.edges.add((start, end))

    def compile(self) -> TinyPlan:
        start_targets = [end for start, end in self.edges if start == "START"]
        if len(start_targets) != 1:
            raise ValueError("需要唯一的 START 入口（教学约束）")
        entry = start_targets[0]
        if entry not in self.nodes:
            raise ValueError(f"入口节点不存在: {entry}")
        for start, end in self.edges:
            if start != "START" and start not in self.nodes:
                raise ValueError(f"未知源节点: {start}")
            if end not in self.nodes and end != "END":
                raise ValueError(f"未知目标节点: {end}")
        return TinyPlan(self.nodes.copy(), entry)


bad = TinyBuilder()
bad.add_node("tools", dangerous_tool)
try:
    bad.compile()
except ValueError as exc:
    print("无入口编译失败:", exc)
else:
    raise AssertionError("缺少 START 应当被本教学编译器拒绝")
assert executions == []

builder = TinyBuilder()
builder.add_node("tools", dangerous_tool)
builder.add_edge("START", "tools")
builder.add_edge("tools", "END")
plan = builder.compile()
assert plan.entry == "tools"
assert "tools" in plan.nodes
assert executions == []
print("编译完成；执行计数:", len(executions))

plan.nodes[plan.entry]({"status": "broken"})
assert executions == ["ran_tool"]
print("显式调用节点后；执行计数:", len(executions))
```

在本文写作环境已经实际运行，输出如下：

```text
无入口编译失败: 需要唯一的 START 入口（教学约束）
编译完成；执行计数: 0
显式调用节点后；执行计数: 1
```

注意 `TinyBuilder` 额外要求“唯一入口”，只是为了让演示更简单，**不是声称 LangGraph 在所有图结构下都采用这个约束**。这里唯一需要迁移到真实源码的直觉，是“准备好的计划不等于已经调用过计划中的函数”。

## 附录 B：退款测试材料的实测与源码索引

对第 9 节使用的测试材料，本文在独立临时目录中真实执行过 `python -m pytest -q`：

| 运行阶段 | `refund.py` 计算方式 | 本文实测 |
| --- | --- | --- |
| 初始缺陷 | `price - coupon` | 退出码 `1`，`1 failed` |
| 教学修复后 | `float(Decimal(str(price)) - Decimal(str(coupon)))` | 退出码 `0`，`1 passed` |

这项实测只验证测试输入与补丁可以产生可观察差异。第 9 节的**完整 LangGraph 框架脚本仍未在本文环境运行**；不能把两者混为一谈。

以下索引按我们本章遇到问题的顺序排列，而不是框架全部 API 的清单：

| 源码位置 | 本章阅读目的 |
| --- | --- |
| [`StateGraph.validate()`，`state.py:1042–1086`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/graph/state.py#L1042-L1086) | 为什么不等到运行阶段才发现没有入口 |
| [`StateGraph.compile()`，`state.py:1087–1297`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/graph/state.py#L1087-L1297) | 校验、创建对象、装配与返回的实际调用顺序 |
| [`CompiledStateGraph`，`state.py:1299–1315`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/graph/state.py#L1299-L1315) | 为什么编译产物具有运行接口 |
| [`CompiledStateGraph.attach_node()`，`state.py:1335–1436`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/graph/state.py#L1335-L1436) | 为什么一个运行时节点包含触发、读取、写入和绑定动作 |
| [`CompiledStateGraph.attach_edge()`，`state.py:1437–1461`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/graph/state.py#L1437-L1461) | 固定边如何转成目标触发关系 |
| [`CompiledStateGraph.attach_branch()`，`state.py:1462–1507`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/graph/state.py#L1462-L1507) | 条件分支为什么装配的是以后要运行的路由动作 |
| [`BranchSpec._route()`，`graph/_branch.py:138–159`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/graph/_branch.py#L138-L159) | 条件函数在什么阶段真正被调用 |
| [`Pregel.validate()`，`pregel/main.py:937–952`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/main.py#L937-L952) | 为什么装配完成后还有第二道结构检查 |
| [`Pregel.invoke()`，`pregel/main.py:3704` 起](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/main.py#L3704-L3790) | 下一章从哪里进入真正运行阶段 |
| [`LangChain create_agent()` 的编译返回](https://github.com/langchain-ai/langchain/blob/fbb8a35da7023ac0382c3b22f047337861bd3486/libs/langchain_v1/langchain/agents/factory.py#L1754-L1770) | 这条源码路径怎样接回 LangChain Agent 工厂 |

**核对与执行状态汇总**：源码入口与关键调用关系已针对固定 Commit 核对；纯 Python 阶段边界实验已运行；退款测试材料在修复前后已用真实 `pytest` 跑过；完整的 LangGraph `compile()` / `invoke()` 示例仅进行了 Python 语法检查与静态分析，**未在本地实际执行**。
