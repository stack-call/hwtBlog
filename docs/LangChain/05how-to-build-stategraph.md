---
title: "04｜一段 Agent Loop，怎样变成 StateGraph 的节点与边？"
date: 2026-10-27
slug: /langchain-langgraph-source/how-to-build-stategraph
description: "沿 create_agent 到 StateGraph 的真实源码路径，从退款测试结束后仍误调用工具的反例出发，理解状态描述、节点注册、普通边、条件分支，以及图构建与编译的边界。"
tags:
  - LangChain
  - LangGraph
  - StateGraph
  - Agent
  - 源码精读
  - Python
---

> 本文是「LangChain × LangGraph 源码精读」的**第 04 章**，对应文件 `05how-to-build-stategraph.md`。建议先阅读[第 03 章：手写 Agent Loop 已经能工作，为什么还需要 create_agent？](/langchain-langgraph-source/how-to-create-agent)。
>
> **本章只有一个主问题：** 已经知道 `create_agent()` 会创建图，但我们究竟怎样把“调用模型、执行工具、决定下一步”交给 `StateGraph` 表达？它在构建时保存了什么，又为什么不在注册节点时直接执行？

上一章的结尾，我们从 LangChain 的 `create_agent()` 里看到了 `model`、`tools` 两个节点，也看到了 `graph.compile()`。不过这里有一个特别容易出现的误解：**把 `model` 和 `tools` 注册到图里，就好像已经写好了执行顺序。**

我们的退款案例刚好能指出这个理解的漏洞。

`refund.py` 最初计算 `0.3 - 0.2`，真实 `pytest` 会发现它与 `0.1` 不相等。Agent 先运行测试，再调用教学用修复工具，再次运行测试。到了最后一步，测试已经通过，脚本化“模型”返回的是完成消息，不再请求任何工具。

这时如果我们把执行顺序写死为 `model → tools → model`，就会出现一个荒谬的结果：**任务已经完成，程序却仍然进入工具执行环节。**

这个小问题，就是本章学习 `StateGraph` 的入口。我们不急着看完整 Runtime，也不急着讨论并发、持久化和 Reducer。先看我们究竟需要怎样的一张最小执行图。

<!-- truncate -->

## 1. 一个固定连接的反例：任务结束了，为什么还要运行工具？

把上一章“模型节点”的行为继续压缩，只保留一个已经足够判断下一步的信号：`tests_passed`。

```python
# 教学化简：纯 Python；不是 LangGraph 的节点实现。
def model_node(state: dict) -> dict:
    return {"next_tool": None if state["tests_passed"] else "run_tests"}


def tools_node(state: dict) -> str:
    if state["next_tool"] != "run_tests":
        raise ValueError(f"无效工具请求: {state['next_tool']!r}")
    return "pytest_called"
```

当 `tests_passed=False` 时，调用工具完全合理。但当 `tests_passed=True` 时，`model_node` 返回 `{"next_tool": None}`。它的意思很清楚：这轮不再需要执行测试。

如果外层仅有一条**无条件**的 `model → tools` 连接，那么接下来仍然会调用 `tools_node`，得到：

```text
固定边失败: 无效工具请求: None
```

因此，我们需要的不是“无论模型输出什么，都跳往工具”，而是：**执行完模型节点后，根据它刚产生的结果选择下一步。**

最小路由函数可以写成：

```python
# 教学化简：本章真正要引入的唯一新控制能力。
def route_after_model(state: dict) -> str:
    return "END" if state["next_tool"] is None else "tools"
```

在这个例子里，模型节点已经把一次决策写进了状态，路由只负责根据这个决定跳转。于是图形应当是：

```text
                ┌─────────┐
START ──────────▶│  model  │◀──────────┐
                └────┬────┘           │
                     │                │
                看 next_tool          │
                   /     \           │
               None     run_tests     │
                 │           │        │
                 ▼           ▼        │
                END       ┌───────┐   │
                          │ tools │───┘
                          └───────┘
```

这张图没有引入新的业务能力。它只是把原来 `while` 中一个重要事实表达清楚：**一个步骤完成以后，是否执行下一个步骤，取决于上一步的输出。**

我已在纯 Python 中实际执行了上述固定边反例与两个路由断言，结果是：

```text
固定边失败: 无效工具请求: None
条件路由: 已完成 -> END，待测试 -> tools
```

这只验证了我们的设计矛盾，不代表已经运行 LangGraph。完整、可复制的纯 Python 验证脚本放在文末。

现在需要把这张小图交给真实框架描述。但首先得回答：路由依据的 `next_tool` 放在哪里？

## 2. 节点之间需要一份共同可读的数据

在手写循环中，我们可以直接访问局部变量：

```python
# 概念伪代码：仅展示数据依赖。
next_tool = model(messages)
if next_tool is None:
    break
execute(next_tool)
```

一旦把模型调用和工具执行拆成两个独立节点，它们不再共享这一段函数的局部变量。我们至少需要说清楚：**每个节点在开始时读取什么，结束时交出什么。**

这就引出 `StateGraph` 名字里的 `State`。本章只需把它理解成“一次执行过程中，节点之间约定共享的数据形状”。

例如，下面是一份只服务于当前退款案例的最小状态描述：

```python
from typing import TypedDict


class FlowState(TypedDict, total=False):
    action: str               # model 本轮选择的工具，或 finish
    last_tool: str            # 上次实际执行的工具
    test_exit_code: int       # 最近一次 pytest 的退出码
```

注意：我们没有直接采用 `AgentState` 的完整消息历史，而是先用三个字段保留同样的**决策关系**：模型决定做什么、工具返回什么、下一轮怎样判断。真实 `create_agent()` 会使用更丰富的状态，包括消息等信息；这不是对其完整数据结构的复制。

现在每种节点就能用统一的方式表达：

```python
# 教学化简：以下返回值表示“要更新的字段”，并不表示修改整个 state。
def model_node(state: FlowState) -> dict:
    if state.get("last_tool") == "run_tests" and state.get("test_exit_code") == 0:
        return {"action": "finish"}
    return {"action": "run_tests"}


def tools_node(state: FlowState) -> dict:
    # 真实执行细节稍后接回 refund.py / pytest
    return {"last_tool": "run_tests", "test_exit_code": 1}
```

这里的 `model_node` 只是为了解释接口，并不能处理“测试失败后修复，再测试”的全部情况；完整实验会补齐这个选择过程。

为什么返回一个 `dict` 而不是在节点里直接改 `state`？因为我们想显式表达：**节点交给执行系统的是本轮更新，而不是一次不可观察的全局变量修改。** 对这章的顺序执行案例，先把它看作“更新哪些字段”就够了。字段之间究竟怎样合并，是第 07、08 章要用更具体的失败场景研究的问题。

现在可以走进真正的 `StateGraph` 源码。

## 3. 打开 `StateGraph.__init__()`：它一开始保存了什么？

本章固定源码基线如下（与前文保持一致）：

| 范围 | 版本与源码坐标 |
| --- | --- |
| LangChain Agent Factory | [`langchain 1.4.7`，Commit `fbb8a35da7023ac0382c3b22f047337861bd3486`](https://github.com/langchain-ai/langchain/blob/fbb8a35da7023ac0382c3b22f047337861bd3486/libs/langchain_v1/langchain/agents/factory.py) |
| LangGraph StateGraph | [`langgraph 1.2.17`，Commit `4c52e129eb1ab69a3da58b799f67929f77d81149`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/graph/state.py) |
| LangGraph 分支定义 | [`graph/_branch.py`，同一 LangGraph Commit](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/graph/_branch.py) |

阅读的主文件是：

[`libs/langgraph/langgraph/graph/state.py`，`StateGraph.__init__()`，约第 201—251 行](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/graph/state.py#L201-L251)。

这里并没有立即调用模型，也没有调用工具。它先建立一些用于**记账**的结构。

**真实源码节选**（取自固定 Commit，相邻行）：

```python
self.nodes = {}
self.edges = set()
self.branches = defaultdict(dict)
```

这三项分别回答了三个很朴素的问题：

- `nodes`：有哪些步骤，各自由哪个 Python 函数或 Runnable 完成？
- `edges`：有哪些无条件的“从 A 接着到 B”的关系？
- `branches`：从某个步骤离开时，哪些去向需要由条件函数来决定？

同一个构造函数还记录 `state_schema`，并用它准备后续节点读写所依赖的状态字段信息。源码中出现的 `channels` 暂时只需理解为**编译和运行时承载这些字段的机制之一**，不必现在就学习它的全部子类型。

这一节的调用者是谁？上一章的 `create_agent()`，在 [`factory.py` 的图创建位置](https://github.com/langchain-ai/langchain/blob/fbb8a35da7023ac0382c3b22f047337861bd3486/libs/langchain_v1/langchain/agents/factory.py#L1124-L1141)，先解析 Agent 所需的状态结构，然后创建 `StateGraph(...)`。

**这里的因果关系很重要：** 因为 `create_agent()` 要在同一个地方注册模型节点、工具节点和路由，所以它需要一个尚未执行的构建器来收集这些定义。`StateGraph` 现在扮演的就是这个角色。

但是，只有一个空容器还不够。下一步我们需要告诉它：`model` 和 `tools` 究竟是什么。

## 4. `add_node()` 为什么只是注册，而不是执行？

在应用代码中，我们当然可以直接调用模型函数：

```python
answer = model_node(state)
```

这会马上产生一个结果。但我们在构建 Agent 时还**没有收到本轮任务的输入**，也不知道它未来运行到这里时应该读取什么状态。

因此，我们真正想做的是：把执行动作先交给图，让图在未来运行到对应步骤时再调用。

```python
# 框架 API 示例：构建图，不是马上执行模型。
builder.add_node("model", model_node)
builder.add_node("tools", tools_node)
```

对应的真实源码是 [`StateGraph.add_node()`，约第 616—858 行](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/graph/state.py#L616-L858)。函数本身有不少代码，用来处理节点名称、可选策略、输入类型等。当前正常路径只需要跟住这几件事：

1. **调用者传入**：节点名 `"model"`，以及它未来应该执行的函数或 Runnable。
2. **先做检查**：节点名是否重复、是否用了 `START` / `END` 这类保留名称。
3. **适配并保存**：将待执行动作转成框架可以统一处理的 Runnable 形式，存入该节点的描述记录 `StateNodeSpec`。
4. **返回**：仍返回构建器本身，允许继续添加连接；这一步不会产生 `AIMessage`，也不会执行 `pytest`。

其中用于适配普通 Python 函数的关键表达式是：

**真实源码节选**：

```python
coerce_to_runnable(action, name=node, trace=False)
```

它的意义不是“提前执行动作”，而是将不同形式的动作规范成后续可调用的对象。调用 `add_node("tools", tools_node)` 时，`tools_node` 在这里是**被保存的函数**，不是**已经执行过的函数**。

我们可以用一个不依赖 LangGraph 的对照实验理解这种差别：

```python
# 概念伪代码，说明“注册”与“调用”的区别。
calls = []

def tools_node(state):
    calls.append("tools")
    return {"done": True}

registry = {"tools": tools_node}  # 只注册
assert calls == []                 # 此时没有执行

registry["tools"]({})              # 现在才执行
assert calls == ["tools"]
```

当然，LangGraph 不是真的只用了这样一个字典。它还保存输入结构、执行适配器等信息。我们强调的只是：**注册节点的时机，与节点真正运行的时机，必须分开。**

在 LangChain 中，证据就在 [`factory.py` 的节点注册位置](https://github.com/langchain-ai/langchain/blob/fbb8a35da7023ac0382c3b22f047337861bd3486/libs/langchain_v1/langchain/agents/factory.py#L1475-L1479)：`create_agent()` 将模型执行包装为一个 Runnable 注册成 `model`，再根据有无客户端工具选择是否注册 `tools`。

两个节点已经存在，问题变成：第一个先执行谁？

## 5. `add_edge()` 解决的是“确定从哪里走到哪里”

模型和工具不是随机启动的。退款案例的第一次动作是让模型决定下一步，所以我们希望这张图的入口指向 `model`：

```python
from langgraph.graph import START

builder.add_edge(START, "model")
```

工具执行完成后，下一次决策仍然需要模型：

```python
builder.add_edge("tools", "model")
```

两条连接有一个共同点：它们都不需要再额外判断去向，因此属于**固定边**。

进入 [`StateGraph.add_edge()`，约第 860—906 行](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/graph/state.py#L860-L906)。函数会检查非法起终点，对普通单起点边记录连接关系；本例不用多起点等待逻辑。

**真实源码节选**：

```python
self.edges.add((start_key, end_key))
```

注意，添加 `tools → model` 并不会立刻让程序返回模型节点。这里仅仅将二者的**连接关系**写入构建器，留到之后编译与执行时使用。

一张图至此有了两个节点、两条固定边。但是我们绝不能再追加一条 `model → tools` 的无条件连接，因为第一节已经用反例证明：模型完成任务以后不能继续调用工具。

所以，只剩下一个合乎逻辑的问题：**怎样注册一条运行时才做出选择的连接？**

## 6. `add_conditional_edges()`：注册的是决策函数，不是现在就做决策

从业务角度，条件函数极其简单：

```python
from typing import Literal


def route_after_model(state: FlowState) -> Literal["tools", "finish"]:
    return "finish" if state["action"] == "finish" else "tools"
```

注意它接收的是**模型节点执行之后**能够读取的状态。我们现在希望得到这样的行为：

- 模型返回 `{"action": "run_tests"}`：去工具节点。
- 模型返回 `{"action": "finish"}`：结束这次执行。

于是把决策函数交给构建器：

```python
from langgraph.graph import END

builder.add_conditional_edges(
    "model",
    route_after_model,
    {"tools": "tools", "finish": END},
)
```

第三个参数把路由函数的**返回值**映射为真正的目的节点。这里 `"finish"` 是我们随意选择的标记名，`END` 才是框架的终止标记；这样写也避免把自己设计的 `"finish"` 和 LangGraph 内部的终止标识混淆。

现在进入 [`StateGraph.add_conditional_edges()`，约第 908—952 行](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/graph/state.py#L908-L952)。

调用者传入节点名、路径选择函数、可选目标映射。源码会把路径函数适配为 Runnable，检查同一来源下的分支名称是否冲突，再把它封装为 `BranchSpec` 放进刚才的 `branches` 注册表。

**真实源码节选**：

```python
self.branches[source][name] = BranchSpec.from_path(path, path_map, True)
```

**这里特别容易搞反一个时间点：** 这行执行时，没有本轮运行的测试结果，因此无法判断究竟走 `"tools"` 还是 `"finish"`。它保存的是“未来执行到 `model` 后，应该用什么函数决定去向”。

这也解释了为什么条件路由**不是**编译阶段事先确定的一条固定边：第一次 `model` 可能会选择测试，第二次可能选择修复，第三次可能选择测试，最后一次则会结束。决定去向所依赖的输入，在每次执行结束后都可能不同。

如果继续沿源码追一小步，在同一 Commit 的 [`graph/_branch.py`，`BranchSpec._route()`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/graph/_branch.py#L138-L159) 中，可以看到运行时调用路径函数，再将结果送给后续处理。我们现在只需知道**运行时会重新计算路径**，不用为了这一个事实提前掌握 Channel 的写入与订阅机制。

到这里，图描述已经足够完整：

```text
节点：   model, tools
入口：   START -> model
固定边： tools -> model
条件边： model -> tools / END（根据本轮状态）
```

它和上一章的手写 `while` 仍然是同一个执行逻辑，只是控制结构已经可以作为**数据和函数注册关系**单独描述。

## 7. 为什么 `StateGraph` 到这里依然不能直接 `invoke()`？

现在有三份构建记录：节点、固定边、条件分支。但它们还只是“计划中的程序”，不是最终执行器。

这个设计并不是为了给 API 增加一个仪式性的 `.compile()`。可以想想，如果允许我们一边添加节点、一边直接运行，会产生什么歧义：

- `model` 已注册，但 `tools` 尚未注册，此时收到工具请求，下一步该去哪里？
- `model` 与 `tools` 都已注册，但还未指定入口，谁先执行？
- 条件函数声称会跳转到一个根本不存在的节点，应该在真正运行时才发现吗？

所以，构建阶段需要一个**明确结束的时刻**。结束后，框架才能检查描述是否成立，并把这些描述装配成一个实际可调用的对象。

`StateGraph` 在自身的类说明中就把它定位成构建器；可执行对象来自 [`StateGraph.compile()`，约第 1087—1297 行](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/graph/state.py#L1087-L1297)。

沿着当前正常路径，`compile()` 做了几件关键事情：

1. **先验证图的结构**：检查入口及节点、边引用等基本条件。
2. **创建 `CompiledStateGraph`**：准备运行所需的对象，而不是开始执行当前退款任务。
3. **装配节点和连接**：将构建器记录的节点、固定边、条件分支分别附加到编译结果。
4. **返回可执行对象**：应用代码随后才可以调用 `invoke()`、`stream()` 等接口。

其中，第 1271—1297 行可以看到节点、边和分支分别被附加到 `compiled` 对象，最后返回编译结果。**真实源码节选**：

```python
return compiled.validate()
```

另外，[`CompiledStateGraph` 的类定义](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/graph/state.py#L1299-L1315)表明它继承了 `Pregel`。这告诉我们：**构建出来的图，与真正运行节点的引擎之间已经连通。** 但 `Pregel` 到底怎样确定可运行节点，留给后续专门追踪 `invoke()` 的章节，而不是在这里突然把 Runtime 的内部结构全部摊开。

有一个容易忽略的工程边界：同一版本的 `add_node()` / `add_edge()` 在图已编译后再修改构建器，会给出“这些修改不会反映到已编译图里”的警告。也就是说，**一份已经编译的执行对象，应以其编译时装配好的结构来理解**，不能假定之后修改构建器就能热更新正在使用的执行器。

我们现在已经能够把阶段关系写清楚：

```text
定义阶段（没有运行退款任务）
  StateGraph(FlowState)
      │
      ├─ add_node("model", ...)
      ├─ add_node("tools", ...)
      ├─ add_edge(START, "model")
      ├─ add_conditional_edges("model", ...)
      └─ add_edge("tools", "model")
                    │
                    ▼
               compile()
                    │
                    ▼
             编译后的执行对象
                    │
       （真正提交本轮任务输入）
                    ▼
                invoke(...)
```

这张图比第一节多了构建、编译两个阶段，但还没有加入并发与持久化，因为解释当前问题根本不需要它们。

## 8. 把退款案例重新写成最小的 `StateGraph` 程序

接下来给出一个完整可复制的框架实验。它不使用 LangChain 的 `create_agent()`，而是直接使用 LangGraph 的 `StateGraph`，让我们专门观察本章讲过的构建 API。

我们依然使用同一个 `refund.py` 精度问题，依然运行**真实 `pytest`**。区别是决策者改成了一个非常明确的脚本化假模型：第一次选择测试，失败后选择套用教学补丁，之后重新测试，通过则结束。

这个程序验证的是**执行图的构建与基本路由**，而不是大模型的自主推理能力。修复补丁是事先写好的，不能据此宣称模型会修 Bug。

> **运行状态必须说明白**：写作环境有 `pytest`，但没有安装 `langgraph`，所以这里的框架脚本仅完成了 Python 语法编译检查，**没有实际运行 `StateGraph` / `compile()` / `invoke()`**。我单独执行了脚本中完全相同的 `refund.py` / `test_refund.py` 测试材料：修改前退出码为 `1`（1 failed），修改后退出码为 `0`（1 passed）。这只能验证测试材料，不能代替框架端到端验证。

建议在新虚拟环境中安装固定主版本：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install "langgraph==1.2.17" pytest
python -m pip freeze > requirements-resolved.txt
```

Windows PowerShell 将激活命令改为 `.venv\Scripts\Activate.ps1`。实际解析的依赖版本应以本地 `requirements-resolved.txt` 为准，本文未对全部依赖进行安装兼容性实测。

保存为 `demo_stategraph.py`：

```python
"""LangGraph API 教学示例；当前环境无 langgraph，未运行框架部分。"""
from __future__ import annotations

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


def model_node(state: FlowState) -> dict:
    # 脚本化假模型，只验证路由；没有大模型推理。
    last = state.get("last_tool")
    if last is None:
        return {"action": "run_tests"}
    if last == "run_tests" and state["test_exit_code"] != 0:
        return {"action": "apply_fix"}
    if last == "apply_fix":
        return {"action": "run_tests"}
    if last == "run_tests" and state["test_exit_code"] == 0:
        return {"action": "finish", "summary": "测试通过（脚本验证）"}
    raise RuntimeError(f"unknown tool: {last!r}")


def tools_node(state: FlowState) -> dict:
    root = Path(state["project_dir"])
    action = state["action"]
    if action == "run_tests":
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_refund.py"],
            cwd=root, capture_output=True, text=True, timeout=30, check=False,
        )
        code = result.returncode
        update = {"last_tool": "run_tests", "test_exit_code": code}
        event = f"run_tests exit_code={code}"
    elif action == "apply_fix":
        (root / "refund.py").write_text(FIXED, encoding="utf-8")
        update = {"last_tool": "apply_fix"}
        event = "apply_fix patch_applied"
    else:
        raise ValueError(f"unapproved action: {action}")
    return {**update, "trace": state.get("trace", []) + [event]}


def route_after_model(state: FlowState) -> Literal["tools", "finish"]:
    return "finish" if state["action"] == "finish" else "tools"


def main() -> None:
    with TemporaryDirectory() as folder:
        root = Path(folder)
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
        graph = builder.compile()

        state = graph.invoke({"project_dir": str(root), "trace": []})
        assert state["action"] == "finish"
        assert state["summary"] == "测试通过（脚本验证）"
        assert state["trace"] == [
            "run_tests exit_code=1",
            "apply_fix patch_applied",
            "run_tests exit_code=0",
        ]
        print("\n".join(state["trace"]))
        print(state["summary"])


if __name__ == "__main__":
    main()
```

运行：

```bash
python demo_stategraph.py
```

**预期轨迹**（依据程序决策顺序推导，尚非 LangGraph 在本环境的真实输出）：

```text
run_tests exit_code=1
apply_fix patch_applied
run_tests exit_code=0
测试通过（脚本验证）
```

把这个程序同最开始的图对照，会发现每一项都有清楚的作用：

| API / 函数 | 现在解决的具体问题 | 还没有解决的问题 |
| --- | --- | --- |
| `FlowState` | 约定两个节点各自能读取与写回什么 | 并发更新怎样合并 |
| `add_node("model", ...)` | 注册每轮的决策步骤 | 该步骤什么时候被调度 |
| `add_node("tools", ...)` | 注册真实 `pytest` / 教学补丁执行步骤 | 工具权限与安全隔离 |
| `add_edge(START, "model")` | 指定这次执行从哪里开始 | 运行时如何触发节点 |
| `add_conditional_edges(...)` | 根据本轮 `action` 决定结束还是执行工具 | 路由结果如何变成运行时任务 |
| `add_edge("tools", "model")` | 测试或修复后回到决策步骤 | 数据怎样提交给下次调用 |
| `compile()` | 检查并装配可执行图 | 引擎内部的调度细节 |

请留意这个刻意的简化：示例用 `trace` 记录事件时，每轮返回的是本地计算出的**新列表值**，整个流程是顺序执行，没有并发向同一个字段写入。在更复杂的图里，不能照搬它来处理并发或假定任意列表更新都会自动追加。这恰恰是未来理解 Reducer 的真实问题来源。

此外，程序虽然把测试放在临时目录内执行，但临时目录**不是沙箱**。这里只允许调用固定的测试文件、应用固定补丁；不能把这个教学用工具节点直接当成能安全执行任意模型生成 Shell 命令的 Agent。

## 9. 回到 `create_agent()`：它究竟用了这些 API 的哪一层？

如果现在回看上一章的 `create_agent()`，以前那些看上去突然出现的 LangGraph 接口，就能一一找到位置。

在固定的 LangChain Commit 中：

- [`factory.py:1124—1141`](https://github.com/langchain-ai/langchain/blob/fbb8a35da7023ac0382c3b22f047337861bd3486/libs/langchain_v1/langchain/agents/factory.py#L1124-L1141)：整理状态结构，创建 `StateGraph`。
- [`factory.py:1475—1479`](https://github.com/langchain-ai/langchain/blob/fbb8a35da7023ac0382c3b22f047337861bd3486/libs/langchain_v1/langchain/agents/factory.py#L1475-L1479)：将模型和工具执行者注册为节点。
- [`factory.py:1574—1599`](https://github.com/langchain-ai/langchain/blob/fbb8a35da7023ac0382c3b22f047337861bd3486/libs/langchain_v1/langchain/agents/factory.py#L1574-L1599)：根据当前配置确定入口；在没有额外中间件的正常路径中，它会从 `model` 开始。
- [`factory.py:1600—1639`](https://github.com/langchain-ai/langchain/blob/fbb8a35da7023ac0382c3b22f047337861bd3486/libs/langchain_v1/langchain/agents/factory.py#L1600-L1639)：为有工具的 Agent 配置模型之后和工具之后的条件路由。
- [`factory.py:1754—1770`](https://github.com/langchain-ai/langchain/blob/fbb8a35da7023ac0382c3b22f047337861bd3486/libs/langchain_v1/langchain/agents/factory.py#L1754-L1770)：调用 `compile()`，返回配置好的可执行图。

这里需要特别纠正一个过度简化的说法：**LangChain 当前固定版本的工具返回路径不总是一条简单的 `tools → model` 固定边**。在 `create_agent()` 中，工具执行后的去向也使用了条件路由，以处理必要的提前结束或其他特殊情况。我们本章自己写的最小图使用固定边，只是因为贯穿案例当前没有这些需求。

因此，我们研究的是**相同的设计职责**，不是声称这个最小例子与 `create_agent()` 的所有分支在源码上逐行同构。

这个区分非常重要。它使我们既能够从小例子学习，又不会因为教学简化而误读真实框架。

## 10. 复盘：`StateGraph` 这一层到底解决了什么？

这章我们没有增加一个更强大的 Agent，也没有增加新的模型能力。我们只是解决了一个程序组织问题。

最早的 `while` 把“调用模型”“执行工具”“判断结束”都写在同一段控制流里。这在简单情况下完全没有问题。当我们想复用同一套执行结构时，开始需要将**执行动作**和**下一步去向**分别描述。

首先，用 `add_node()` 保存可以稍后执行的动作；接着，用 `add_edge()` 描述确定关系；再用 `add_conditional_edges()` 描述必须依赖本轮结果决定的关系。为了让各步骤彼此交换本轮执行信息，提供共同的状态结构。最后，用 `compile()` 在构建阶段结束时检查、装配，产生可执行对象。

所以，这一章可以带走一个非常具体的结论：

> **`StateGraph` 首先是描述执行程序的构建器。节点登记“做什么”，边与分支登记“做完以后去哪儿”，状态结构约定“节点间传什么”。这些定义在构建阶段被保存、校验并装配，真正的节点运行发生在编译后调用图的阶段。**

同时也应记住它的边界：图不会让脚本化假模型拥有推理能力；节点注册不等于工具执行；Graph 也不是免除业务权限检查和副作用管理的安全边界。

至此，我们能把 `create_agent()` 的那一层结构看懂了。不过，下一层问题已经出现：**既然 `StateGraph` 只是保存了节点和路由，为什么编译出来的对象就能执行？`compile()` 究竟把这些记录装配成了什么？**

下一章进入 **第 05 章《Graph 定义好了，为什么还不能直接执行？》**，建议文件名 `06how-to-compile-stategraph.md`。我们将沿 `StateGraph.compile()` 到 `CompiledStateGraph`，只研究从“构建描述”到“可执行对象”的那一次转换；等这个对象真正清楚了，再进入 `invoke()` 如何调度的源码。

---

## 附录 A：纯 Python 路由反例的完整验证脚本

以下脚本不依赖 LangChain / LangGraph，不访问模型，也不模拟框架内部执行器。它只复现本章开头发现的“固定边不适合有结束条件”这个设计问题。

保存为 `verify_branch.py`：

```python
"""纯 Python 路由对照实验：不是 LangGraph 源码。"""

def model_node(state: dict) -> dict:
    return {"next_tool": None if state["tests_passed"] else "run_tests"}


def tools_node(state: dict) -> str:
    if state["next_tool"] != "run_tests":
        raise ValueError(f"无效工具请求: {state['next_tool']!r}")
    return "pytest_called"


completed = model_node({"tests_passed": True})
try:
    tools_node(completed)  # 错误设计：model -> tools 是无条件边
except ValueError as exc:
    print(f"固定边失败: {exc}")
else:
    raise AssertionError("固定边应当暴露无效工具请求")


def route_after_model(state: dict) -> str:
    return "END" if state["next_tool"] is None else "tools"

assert route_after_model(completed) == "END"
not_done = model_node({"tests_passed": False})
assert route_after_model(not_done) == "tools"
assert tools_node(not_done) == "pytest_called"
print("条件路由: 已完成 -> END，待测试 -> tools")
```

运行：

```bash
python verify_branch.py
```

**验证情况**：上面的脚本已经在写作环境实际运行，输出与第 1 节一致；它不提供任何 LangGraph Runtime 已运行的证据。

## 附录 B：源码阅读索引与验证边界

| 入口 | 读这段是为了解释什么 |
| --- | --- |
| [`StateGraph.__init__()`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/graph/state.py#L201-L251) | 为什么先准备节点、固定边、分支的注册位置 |
| [`StateGraph.add_node()`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/graph/state.py#L616-L858) | 为什么注册动作不是立即调用动作 |
| [`StateGraph.add_edge()`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/graph/state.py#L860-L906) | 怎样描述确定的下一跳 |
| [`StateGraph.add_conditional_edges()`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/graph/state.py#L908-L952) | 为什么条件函数在构建时被保存而不是马上执行 |
| [`BranchSpec._route()`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/graph/_branch.py#L138-L159) | 路由函数将来在什么位置被调用 |
| [`StateGraph.compile()`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/graph/state.py#L1087-L1297) | 构建描述怎样进入可执行对象 |
| [`CompiledStateGraph`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/graph/state.py#L1299-L1315) | 为什么下一章需要认识 Pregel 执行对象 |
| [`LangChain create_agent()` 的图装配](https://github.com/langchain-ai/langchain/blob/fbb8a35da7023ac0382c3b22f047337861bd3486/libs/langchain_v1/langchain/agents/factory.py#L1475-L1639) | 两个框架是如何沿同一执行路径连接起来的 |

**已实际执行**：纯 Python 固定边反例与条件路由断言；`refund.py` 浮点精度测试数据在应用补丁前后分别得到 `1 failed` 和 `1 passed`。

**仅核对固定 Commit 源码、未在写作环境执行**：`demo_stategraph.py` 的 `StateGraph` 注册、编译和调用。其 Python 语法已通过检查，预期轨迹是推导结果而非真实框架输出。所有“真实源码节选”均对应链接指向的固定文件；其余代码均为标注过的教学化简或实验代码。
