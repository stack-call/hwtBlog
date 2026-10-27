---
title: "08｜两个节点同时更新 State，为什么不能直接覆盖？"
date: 2026-10-27
slug: /langchain-langgraph-source/how-to-reduce-concurrent-state
description: "沿退款测试 Agent 的并行检查场景，从 LastValue 的冲突出发，逐步追踪 LangGraph 1.2.17 的 apply_writes、Annotated、BinaryOperatorAggregate 和 Reducer，理解同一步状态更新的合并规则。"
tags:
  - LangChain
  - LangGraph
  - StateGraph
  - Reducer
  - Channel
  - Pregel
  - 源码精读
  - Python
---

> **LangChain × LangGraph 源码精读 · 第 08 章**  
> 上一篇：[第 07 章｜节点读到的 State 从哪里来？](./08how-to-read-write-state.md)  
> 本章唯一的问题：**如果同一步里两个节点都返回 `{"evidence": ...}`，谁应该决定 State 中的 `evidence` 最终是什么？**

上一章我们已经沿着 `节点返回局部更新 → ChannelWrite → apply_writes() → Channel.update()` 走完了一条正常的数据链。在那条链里，每一轮对同一个字段只有一个写入者：运行 `pytest`，得到退出码；修复文件；再次运行 `pytest`。所以我们只需要最简单的字段更新规则：把这个新值保存下来。

这种规则确实解决了单节点写入的问题。可是，同一个退款任务还可以让两个彼此独立的动作同时进行：**一个节点运行测试，另一个节点阅读 `refund.py`，检查是否存在浮点加法。** 两个节点都只想报告自己观察到的证据，没有哪个节点应该负责覆盖另一个节点。

问题就在这里发生了变化：不再是“怎样保存一个节点的新值”，而是“怎样保存**同一步多个节点对同一个字段的更新**”。

<!-- truncate -->

## 1. 先让上一章的简单方案真正遇到问题

我们仍然只研究 `refund.py`：

```python title="refund.py（实验用，故意保留问题）"
def refund_amount():
    return 0.1 + 0.2
```

```python title="test_refund.py（真实 pytest 测试）"
from decimal import Decimal
from refund import refund_amount


def test_partial_refund():
    assert refund_amount() == Decimal("0.30")
```

我们的目的不是让两个节点都尝试修复文件。它们只收集观察结果，因此可以安全地用同一份**步骤开始时的状态**工作：

```text
                   ┌── run_pytest ─────┐
START → 输入 State ┤                   ├─→ 合并 evidence → END
                   └── inspect_source ─┘
```

这里两条分支在同一步运行，并不表示 Python 线程在同一个纳秒开始执行；准确含义是：**它们使用该步已准备好的输入，各自产生写入，并在步结束时由 Runtime 统一应用。** 这是上一章学习的 Pregel 执行边界。

我们希望最终获得如下状态：

```python
{
    "project_dir": "...",
    "evidence": [
        "pytest: exit=1",
        "source: float addition",
    ],
}
```

两条信息都应该保留下来。如果采用最熟悉的字典更新，可能这样写：

```python title="教学化简：覆盖式合并"
state = {"evidence": []}
state.update({"evidence": ["pytest: exit=1"]})
state.update({"evidence": ["source: float addition"]})
print(state["evidence"])
# ['source: float addition']
```

第二次更新覆盖了第一次更新。调换两行顺序，又会得到另一个结果。**这里的错误不是 `dict.update()` 做错了什么，而是我们尚未声明两个贡献应该如何组合。**

一种临时办法是规定“先运行 pytest，再运行源码检查，第二个节点负责保留前一个结果”。但这样就把本来独立的两个节点变成了有顺序依赖的节点：源码检查需要读取 pytest 的更新，反过来也一样。为了收集两条独立证据，没有必要人为制造这种依赖。

于是，真正缺少的不是某个“更强的字典”，而是一个**字段级的多写入合并规则**。

## 2. 先用真实 pytest 复现两个写入，再观察冲突

先不引入 LangGraph。用纯 Python 运行一次真实 `pytest`，再读取 `refund.py`，可以把“两个节点在同一步各自产生什么”看得非常清楚。

```python title="verify_reducer_ch08_pure.py（纯 Python 教学实验，已运行）"
from __future__ import annotations

import operator
import subprocess
import sys
from functools import reduce
from pathlib import Path
from tempfile import TemporaryDirectory

BROKEN = 'def refund_amount():\n    return 0.1 + 0.2\n'
TEST = (
    'from decimal import Decimal\n'
    'from refund import refund_amount\n\n'
    'def test_partial_refund():\n'
    '    assert refund_amount() == Decimal("0.30")\n'
)


def run_pytest(snapshot: dict) -> list[str]:
    finished = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "test_refund.py"],
        cwd=snapshot["project_dir"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert finished.returncode in (0, 1), finished.stderr
    return [f"pytest: exit={finished.returncode}"]


def inspect_source(snapshot: dict) -> list[str]:
    code = (Path(snapshot["project_dir"]) / "refund.py").read_text(
        encoding="utf-8"
    )
    return [
        "source: float addition"
        if "0.1 + 0.2" in code
        else "source: no match"
    ]


def last_value(updates: list[list[str]]) -> list[str]:
    # 教学化简：模仿默认字段一次只能接收一个值的约束；非框架代码
    if len(updates) != 1:
        raise ValueError("at most one value per step")
    return updates[0]


def main() -> None:
    with TemporaryDirectory(prefix="ch08-reducer-") as folder:
        root = Path(folder)
        (root / "refund.py").write_text(BROKEN, encoding="utf-8")
        (root / "test_refund.py").write_text(TEST, encoding="utf-8")

        snapshot = {"project_dir": folder, "evidence": []}
        updates = [run_pytest(snapshot), inspect_source(snapshot)]
        assert updates[0] == ["pytest: exit=1"]
        assert updates[1] == ["source: float addition"]
        print("same_step_writes:", updates)

        try:
            last_value(updates)
        except ValueError as exc:
            print("last_value:", str(exc))
        else:
            raise AssertionError("expected conflict")

        merged = reduce(operator.add, updates, snapshot["evidence"])
        assert sorted(merged) == sorted(
            ["pytest: exit=1", "source: float addition"]
        )
        print("reduced:", sorted(merged))

        old = ["previous-round"]
        bad_updates = [
            old + ["pytest: exit=1"],
            old + ["source: float addition"],
        ]
        bad_merge = reduce(operator.add, bad_updates, old)
        assert bad_merge.count("previous-round") == 3
        print("bad_full_history_repeated:", bad_merge.count("previous-round"))
        print("pure_python_assertions: PASS")


if __name__ == "__main__":
    main()
```

执行：

```bash
python -m pip install pytest
python verify_reducer_ch08_pure.py
```

**当前环境实际运行输出：**

```text
same_step_writes: [['pytest: exit=1'], ['source: float addition']]
last_value: at most one value per step
reduced: ['pytest: exit=1', 'source: float addition']
bad_full_history_repeated: 3
pure_python_assertions: PASS
```

这段实验包含三个不同层次的证据，不能混在一起说：

- `pytest: exit=1` 来自**真正执行的 pytest**，但本例没有调用大模型。
- `last_value()` 是我们写的**教学化简**，只用来明确“这一字段拒绝同一步多个写入”；`ValueError` 不是 LangGraph 抛出的异常。
- `reduce(operator.add, ...)` 是**纯 Python 的合并验证**。它说明两个列表片段可以保留，但还没有证明 LangGraph 内部就是这样实现的。

至此我们已经发现：如果两个节点都需要写入 `evidence`，我们就必须明确新旧证据如何合并。现在再去看源码，才知道为什么默认规则会失败，以及新规则应该装在哪一层。

## 3. 默认 `LastValue` 不是“随便挑一个值覆盖”

本系列固定源码基线仍然是：

| 项目 | 固定版本 / Commit | 本章使用范围 |
| --- | --- | --- |
| [LangGraph](https://github.com/langchain-ai/langgraph) | `1.2.17` / [`4c52e129eb1ab69a3da58b799f67929f77d81149`](https://github.com/langchain-ai/langgraph/commit/4c52e129eb1ab69a3da58b799f67929f77d81149) | `channels/last_value.py`、`graph/state.py`、`pregel/_algo.py`、`channels/binop.py` |
| [LangChain](https://github.com/langchain-ai/langchain) | `1.4.7` / [`fbb8a35da7023ac0382c3b22f047337861bd3486`](https://github.com/langchain-ai/langchain/commit/fbb8a35da7023ac0382c3b22f047337861bd3486) | 延续前文的 `create_agent()` 入口；本章没有必要再钻回 Agent Factory |

从上一章已看过的 [`LastValue.update()`，`channels/last_value.py:51–62`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/channels/last_value.py#L51-L62) 开始。对于没有声明特殊合并规则的普通字段，`StateGraph` 会使用 `LastValue`。

**真实源码节选**（省略构造错误信息的代码）：

```python
if len(values) != 1:
    # ...
    raise InvalidUpdateError(msg)

self.value = values[-1]
```

注意函数收到的不是一个值，而是本步这一字段的 `values` 序列。这样设计不是偶然：**多个节点都可能产生写入，但字段究竟怎样解释这些写入，需要由对应 Channel 决定。**

`LastValue` 的选择是保守的：没有值就不更新；恰好一个值就保存；**同一步出现多个值则拒绝**。错误码是 `INVALID_CONCURRENT_GRAPH_UPDATE`，而不是“默默按最后完成的节点覆盖”。你可以沿该源码链接核对其 `InvalidUpdateError` 与报错信息。

为什么拒绝更合理？假设两个节点都写出一条证据，如果默认任意选择其中之一，我们不仅丢掉另一条，还可能误认为程序完整收集了所有信息。`LastValue` 不擅自选择合并语义，让这个问题尽早暴露。

它对另一类字段反而很合适。例如 `last_exit: int` 表示“最新一次测试的退出码”。如果每个执行步只安排一个测试节点，那么一个新值替换旧值正好符合需求。**不应该为了消除冲突，给所有 State 字段都加 Reducer。**

## 4. 两个写入是怎样一起交给 Channel 的？

上一章已经跟踪过节点输出进入 `ChannelWrite` 的路径；现在只需要补上一个新的事实：**同一步中来自不同任务的相同字段写入，会被放到同一个列表里。**

在固定 Commit 的 [`pregel/_algo.py:241–242`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/_algo.py#L241-L242)，`apply_writes()` 首先按照任务路径对任务排序。这使写入应用顺序可确定，但**排序不等于这些节点具有业务上的先后依赖**。

继续看 [`pregel/_algo.py:276–304`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/_algo.py#L276-L304)。这两处是当前问题的关键。

**真实源码节选**（两处之间省略其它分支和循环）：

```python
pending_writes_by_channel[chan].append(val)
# ...
channels[chan].update(vals)
```

把它与退款案例直接对应：

```text
同一步完成的任务写入：

  pytest 节点   → ("evidence", ["pytest: exit=1"])
  inspect 节点 → ("evidence", ["source: float addition"])

按字段收集后：

  pending_writes_by_channel["evidence"]
      = [["pytest: exit=1"], ["source: float addition"]]

之后：

  channels["evidence"].update(这两个列表片段)
```

上面是**教学化简的数据轨迹**，用于解释两个 `append()` 结果和后续 `update()` 的关系；它不是运行时日志。

这里没有出现“让两个节点修改同一个全局列表”的操作，也没有要求节点抢锁。节点各自返回局部更新，Runtime 收集后交给**对应字段的 Channel**。这就是合并机制最合适的安放位置。

现在问题从“在哪里合并”变成了“用什么规则合并”。

## 5. 只为 `evidence` 引入 Reducer

对 `evidence` 而言，我们想表达的是：**新节点返回的证据是新增片段，应当追加在已有证据之后，而不是替换已有证据。**

用最小的 Python 操作，规则就是列表拼接：

```python
old = ["earlier observation"]
update = ["pytest: exit=1"]
new = old + update
# ['earlier observation', 'pytest: exit=1']
```

这样的 `(旧值, 新更新) -> 新值` 函数就叫 **Reducer**。这个名字现在才有必要出现，因为我们真的碰到了一个字段需要处理多个写入的问题。

在 `StateGraph` 中，为该字段声明规则：

```python title="公开 API 用法"
import operator
from typing import Annotated, TypedDict

class State(TypedDict):
    project_dir: str
    evidence: Annotated[list[str], operator.add]
```

它仍然是一个 `TypedDict` 字段，节点仍然接收 Python 字典，返回值仍然是 `{"evidence": [...]}`。只改变一点：`evidence` 的更新方式不再是默认保存新值，而是使用 `operator.add` 合并列表。`project_dir` 没有添加规则，因此依旧使用默认的 Channel。

理解这行声明时，不要把 `Annotated` 当成会在运行时自动调用模型或执行函数的东西。它只是在类型注解旁边附加了元信息：框架**构建图时读取这份元信息**，再决定使用哪种 Channel。

于是我们需要再核对两个源码问题：**谁读取 `Annotated`？谁真正调用 `operator.add`？**

## 6. 从 `Annotated` 到 `BinaryOperatorAggregate`：构建器在哪里选择 Channel？

打开 [`graph/state.py:1687–1741`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/graph/state.py#L1687-L1741)。`_get_channels()` 用 `get_type_hints(..., include_extras=True)` 读取 Schema；这里的 `include_extras=True` 很关键，否则 `Annotated` 的附加信息就不能被按预期保留下来供后续识别。

进入 `_get_channel()`，默认 `LastValue` 之前多了一条分支：

**真实源码节选**（[`graph/state.py:1733–1741`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/graph/state.py#L1733-L1741)）：

```python
elif channel := _is_field_binop(annotation):
    channel.key = name
    return channel
```

此处的 `_is_field_binop()` 不是 Python 的内置算法，而是 LangGraph 用来检查字段注解的辅助函数。它会确认注解末尾有可调用对象，检查签名中有两个位置参数，然后构建聚合 Channel。

**真实源码节选**（[`graph/state.py:1770–1787`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/graph/state.py#L1770-L1787)）：

```python
return BinaryOperatorAggregate(typ, meta[-1])
```

这行源码的意义在于：我们声明的 `operator.add` 并没有丢失，它被作为参数存进了 `BinaryOperatorAggregate`。此前默认的 `LastValue` 路径对这个字段不再适用。

到这里，我们已经找到“**配置 Reducer**”的地方。但配置只是第一步，还要继续看运行时接收更新时如何调用它。

## 7. `BinaryOperatorAggregate.update()` 怎样接收两个节点的贡献？

进入固定 Commit 的 [`channels/binop.py:59–136`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/channels/binop.py#L59-L136)。这个类保存当前累计值，也保存创建时传入的 `operator`。

现在只看普通追加路径；源码另外有显式覆盖语义，但本章不需要用到。

**真实源码节选**（[`channels/binop.py:115–136`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/channels/binop.py#L115-L136)）：

```python
self.value = self.operator(self.value, value)
```

用退款任务的数据手动展开就是：

```text
已有 evidence: []

第一条新更新：['pytest: exit=1']
  [] + ['pytest: exit=1']
  = ['pytest: exit=1']

第二条新更新：['source: float addition']
  ['pytest: exit=1'] + ['source: float addition']
  = ['pytest: exit=1', 'source: float addition']
```

这是**教学化简的折叠过程**。真实类还负责初始化、空值、特殊覆盖请求等行为。我们没有必要在第一次理解 Reducer 时把它们全部展开。

尤其要注意：`BinaryOperatorAggregate` 没有去重新执行节点。它接受的 `values` 已经是 `apply_writes()` 收集好的**结果**。所以 Reducer 所在的位置是在**字段更新阶段**，而不是节点调度阶段。

经过这一层转换，整个问题已经形成闭环：

```text
TypedDict 中的 evidence: Annotated[list[str], operator.add]
                           │
                     StateGraph 构建
                           ▼
                   BinaryOperatorAggregate
                           ▲
                 apply_writes 按字段归集
                    ┌──────┴──────┐
                pytest         inspect
                    │              │
              独立返回片段    独立返回片段
```

注意图中的“归集 → Channel”发生在运行时，而 `Annotated → BinaryOperatorAggregate` 发生在图构建阶段。**配置阶段选择规则，执行阶段应用规则**；这是两层不同的职责。

## 8. 用真实 LangGraph API 复现“失败 → 最小修复”

下面的代码会执行两个独立节点，并让它们在同一步写入 `evidence`。我们先用无 Reducer 的 Schema 构建图，预期抛出 `InvalidUpdateError`；然后仅改变 `evidence` 的注解，再运行同样的节点，检查两条证据是否同时保留。

**验证状态：该 LangGraph 框架实验尚未在当前环境实际执行。** 当前环境没有安装固定版本的 `langgraph` 依赖，因此以下是根据固定源码核对的可复现示例与预期断言，不是本次环境的真实框架运行记录。

```bash
python -m pip install "langgraph==1.2.17" "pytest>=8,<10"
```

```python title="verify_reducer_ch08_langgraph.py（真实框架 API，待运行）"
from __future__ import annotations

import operator
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, TypedDict

from langgraph.errors import InvalidUpdateError
from langgraph.graph import END, START, StateGraph

BROKEN = 'def refund_amount():\n    return 0.1 + 0.2\n'
TEST = (
    'from decimal import Decimal\n'
    'from refund import refund_amount\n\n'
    'def test_partial_refund():\n'
    '    assert refund_amount() == Decimal("0.30")\n'
)


class PlainState(TypedDict):
    project_dir: str
    evidence: list[str]  # 默认 LastValue，不支持同一步多个值


class ReducedState(TypedDict):
    project_dir: str
    evidence: Annotated[list[str], operator.add]


def run_pytest(state: dict) -> dict:
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "test_refund.py"],
        cwd=state["project_dir"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode in (0, 1), completed.stderr
    return {"evidence": [f"pytest: exit={completed.returncode}"]}


def inspect_source(state: dict) -> dict:
    text = (Path(state["project_dir"]) / "refund.py").read_text(
        encoding="utf-8"
    )
    return {
        "evidence": [
            "source: float addition"
            if "0.1 + 0.2" in text
            else "source: no match"
        ]
    }


def make_graph(state_schema: type):
    builder = StateGraph(state_schema)
    builder.add_node("pytest", run_pytest)
    builder.add_node("inspect", inspect_source)
    builder.add_edge(START, "pytest")
    builder.add_edge(START, "inspect")
    builder.add_edge("pytest", END)
    builder.add_edge("inspect", END)
    return builder.compile()


def main() -> None:
    with TemporaryDirectory(prefix="ch08-graph-") as folder:
        root = Path(folder)
        (root / "refund.py").write_text(BROKEN, encoding="utf-8")
        (root / "test_refund.py").write_text(TEST, encoding="utf-8")
        initial = {"project_dir": folder, "evidence": []}

        plain = make_graph(PlainState)
        try:
            plain.invoke(initial)
        except InvalidUpdateError as exc:
            print("plain_state: conflict", type(exc).__name__)
        else:
            raise AssertionError("expected concurrent update conflict")

        reduced = make_graph(ReducedState)
        result = reduced.invoke(initial)
        assert result["project_dir"] == folder
        assert sorted(result["evidence"]) == [
            "pytest: exit=1",
            "source: float addition",
        ]
        print("reduced_state:", sorted(result["evidence"]))
        print("langgraph_assertions: PASS")


if __name__ == "__main__":
    main()
```

这段代码采用最少的新机制：**没有 LLM、没有真实工具调用消息、没有 Checkpoint、没有 Middleware**。因为我们研究的不是模型智能，而是 Runtime 对同一步多个状态写入的处理。两个独立检查动作足以复现问题。

按固定版本源码，预期结构为：

```text
plain_state: conflict InvalidUpdateError
reduced_state: ['pytest: exit=1', 'source: float addition']
langgraph_assertions: PASS
```

**这是预期输出，不是实际框架运行输出。** 如果读者本地出现差异，应首先检查安装的 `langgraph` 版本，以及 `pytest` 是否能在临时目录中执行；不要用上面的预期内容冒充运行日志。

还有一个小细节：断言使用 `sorted()`，因为我们只需要保证两条证据都存在，**不把列表中元素先后顺序当作业务时间顺序**。在这个固定版本中，`apply_writes()` 会按任务路径排序以确定应用顺序，但这不代表哪个任务先完成，也不代表两份证据之间有因果先后。

## 9. Reducer 解决了什么，又没有解决什么？

这时很容易过度推断：“既然两个并发节点能用 Reducer 合并，那我们干脆给所有字段加一个合并函数，Agent 就不会再冲突了。”这不对。

### 9.1 不是所有字段都适合追加

`evidence` 是累积证据，列表追加符合业务意义；`last_exit` 则通常代表最近一次测试的退出码，两个节点并发产生不同值时，正确做法往往是**澄清哪个值表示哪一次测试**，而不是简单求和。

如果有两个独立测试任务，可以考虑把每次结果作为结构化的 `test_results` 片段进行合并；如果只有一个“本轮最终状态”，就应该在图上明确它的生产者或做一次汇总决策。Reducer 不会替你定义业务语义。

### 9.2 Reducer 的输入必须是“新增内容”，不是完整历史

这一点很容易在源码精读或自己编写节点时踩坑。我们为 `evidence` 使用列表相加，因此节点必须返回**新增片段**：

```python
# 正确：这一次新增一条证据
return {"evidence": ["pytest: exit=1"]}
```

下面这种写法看上去更完整，实际上会让历史被反复追加：

```python
# 错误示例：把旧证据再返回一次，Reducer 会重复累计
return {"evidence": state["evidence"] + ["pytest: exit=1"]}
```

假设旧状态是 `['previous-round']`，两个节点都读取到了它，各自返回“旧历史 + 自己的新内容”，最后聚合会出现三份 `previous-round`：一份在原状态，两份在两个节点的更新里。第 2 节的真实纯 Python 实验断言了这个重复现象：`bad_full_history_repeated: 3`。

这不是框架的 Bug。**Reducer 无法知道你返回的列表是“新片段”还是“全量快照”；它只会按照声明的规则处理。**

### 9.3 并行合并不等于确定的业务顺序

列表相加满足结合律，但不满足交换律：

```python
assert (["a"] + ["b"]) != (["b"] + ["a"])
```

`apply_writes()` 的固定任务路径排序可以让某次图执行的合并顺序可预测，但我们仍不应把最终列表顺序理解为外部事件真实发生的顺序。如果之后的逻辑需要时间先后或优先级，应该在事件结构中显式记录可以验证的序号、来源或时间，并定义相应规则。

对于可能分批重放的增量合并，结合律尤为重要。选用 Reducer 时应审视它在不同分组下的合并结果是否一致；不要依赖一个包含随机数、当前时间或不可控外部副作用的合并函数。

### 9.4 Reducer 不修复外部副作用冲突

我们的两个节点只读取文件和运行测试，因此可以共享同一项目目录。但如果两个并行节点都在修改 `refund.py`，即使它们的 `evidence` 字段能够合并，也无法阻止它们在磁盘上互相覆盖修改。

**State 的合并规则不等于文件系统事务，也不等于 Shell 命令安全隔离。** 需要解决文件写入冲突时，应修改任务依赖、安排文件所有权或引入适当的执行隔离，而不是期待 Reducer 解决。

## 10. 回到完整执行链：这一层设计是怎样长出来的？

现在我们可以从前几章已经掌握的部分，把新机制放回完整路径，而不用突然引入更多架构名词：

```text
① create_agent() / StateGraph 构建图
       │
       ├─ 默认字段 → LastValue
       └─ Annotated 字段 → BinaryOperatorAggregate(reducer)

② graph.invoke() 开始运行
       │
       ▼
③ PregelLoop 准备本步任务
       │
       ▼
④ PregelRunner 执行 pytest 和 inspect 两个节点
       │
       ├─ (evidence, ["pytest: exit=1"])
       └─ (evidence, ["source: float addition"])
       │
       ▼
⑤ apply_writes() 按字段收集本步写入
       │
       ▼
⑥ channels["evidence"].update(两个片段)
       │
       ├─ 如果是 LastValue → InvalidUpdateError
       └─ 如果是 BinaryOperatorAggregate → 调用 reducer 合并
       │
       ▼
⑦ 下一步节点读取已提交的 evidence
```

这张图应该让你感到熟悉：从上一章到这一章，唯一真正新增的知识是**同一字段如何接收多个更新**。`PregelLoop`、节点读写、Channel 的工作已经理解，我们只是终于看到了默认 Channel 不够用的场景。

你现在应该能亲自复述三个问题的答案：

1. **为什么出现冲突？** 两个独立节点在同一执行步写了同一个字段，默认 `LastValue` 每步只接受一个新值。
2. **Reducer 从哪里来？** `StateGraph` 在构建 Schema 时读取 `Annotated`，为该字段选择携带二元合并函数的 `BinaryOperatorAggregate`。
3. **谁调用 Reducer？** Runtime 先按字段收集写入，`apply_writes()` 调用 Channel 的 `update()`；聚合 Channel 才在内部对每份值应用合并函数。

## 11. 下一章：状态能更新了，怎样观察执行过程？

经过这章，我们已经能够理解一次执行如何完成多个节点的更新合并。但还有一个体验上的问题：目前通过 `graph.invoke()`，调用者通常要到运行结束才能得到最终结果。如果一次 Coding Agent 要运行测试、阅读代码、修复、重测，用户怎样知道它此刻执行到了哪一步？

这不是对 Reducer 的补充，而是沿执行链自然出现的下一个需求：**一边运行，一边观察每个执行阶段的进展**。

下一篇将进入 **Streaming**，从 `invoke()` 的最终结果出发，再研究 `stream()` 如何按执行阶段提供 `updates` 和 `values`，以及我们看到的事件与实际状态提交之间是什么关系。

**下一篇：第 09 章｜为什么 invoke() 要等到结束？怎样用 stream() 看见 Agent 的每一步？**  
建议文件名：`10how-to-stream-graph-execution.md`。

---

## 附录 A｜本章真正使用的源码定位

所有链接均固定到 **LangGraph `1.2.17` / Commit `4c52e129eb1ab69a3da58b799f67929f77d81149`**，不以主分支替代固定版本。

| 要验证的设计事实 | 真实源码位置 |
| --- | --- |
| 默认字段同一步接收两个值时为何报错？ | [`LastValue.update()`，`channels/last_value.py:51–62`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/channels/last_value.py#L51-L62) |
| 任务写入怎样按字段分组，再交给 Channel？ | [`apply_writes()`，`pregel/_algo.py:241–304`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/_algo.py#L241-L304) |
| `Annotated` 信息在哪里被读取？ | [`_get_channels()`，`graph/state.py:1687–1706`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/graph/state.py#L1687-L1706) |
| 为什么有 Reducer 的字段不再走默认 `LastValue`？ | [`_get_channel()`，`graph/state.py:1719–1741`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/graph/state.py#L1719-L1741) |
| 怎样确认 Reducer 是二元函数？ | [`_is_field_binop()`，`graph/state.py:1770–1787`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/graph/state.py#L1770-L1787) |
| 累积值最终在哪里调用合并函数？ | [`BinaryOperatorAggregate.update()`，`channels/binop.py:115–136`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/channels/binop.py#L115-L136) |

## 附录 B｜验证边界及术语边界

- **已实际运行**：本章第 2 节的纯 Python 实验，包含真实 pytest 失败结果、两个检查动作的写入值、教学版单值冲突、列表合并断言，以及错误全量更新导致证据重复的断言。
- **已核对静态源码**：上表所列固定 Commit 的入口、关键条件和更新路径。
- **尚未在当前环境实际执行**：第 8 节的 `StateGraph` API 对照实验。其错误与输出明确标记为预期，不宣称实测。
- **并发含义**：这里的“同一步多节点”是 Pregel 执行步和写入提交层面的并行；不把它偷换成“所有 Python 线程严格同时执行”。
- **合并边界**：Reducer 决定字段值的更新语义，不决定模型思考能力、不自动解决外部文件竞争，也不保证业务事件恰好发生一次。

**本章留给读者的唯一新认识是：`State` 不只是“有哪些字段”，还隐含“每个字段能怎样接收更新”。默认 `LastValue` 足以处理一个写入者；多个写入者真正出现时，才需要显式的 Reducer。**
