---
title: "07｜节点读到的 State 从哪里来？从 TypedDict 到 Channel 的读写边界"
date: 2026-10-27
slug: /langchain-langgraph-source/how-to-read-write-state
description: "从退款测试 Agent 的一次状态更新出发，沿固定版本 LangGraph 源码追踪 StateGraph._add_schema、CompiledStateGraph.attach_node、PregelNode、ChannelWrite、apply_writes 和 LastValue，理解节点如何读状态、提交部分更新。"
tags:
  - LangChain
  - LangGraph
  - StateGraph
  - Channel
  - Runtime
  - 源码精读
  - Python
---

> 本文是「LangChain × LangGraph 源码精读」的**第 07 章**，对应文件名 `08how-to-read-write-state.md`。建议先读 [第 06 章：一次 graph.invoke()，是谁让 model → tools → model 持续运行？](./07how-to-run-pregel-loop.md)。
>
> **本章唯一要弄清楚的事情：** 用户在节点里看到的是一个普通 `State` 字典，但 Pregel Runtime 管理的并不是一份任由节点原地修改的共享字典。**节点读到的数据怎样组装出来、节点返回的部分更新怎样变成下一轮可见的状态？**

上一章停在 `PregelLoop.after_tick()`：我们看见 Runtime 在步边界应用任务写入，然后准备下一轮任务。至此我们知道“谁让 `model → tools → model` 不断执行”，却还有一个空白：**应用的到底是什么？**

在退款精度任务中，`tools` 刚运行完 `pytest`，我们希望下一轮 `model` 至少读到三个事实：这是第几次测试、上一次退出码是多少、业务测试是否通过。很自然，我们可能用一个字典表示它们：

```python
from typing import TypedDict

class State(TypedDict):
    project_dir: str
    attempts: int
    last_exit: int
    status: str
```

使用这四个字段就足够描述当前实验。此时先不引入消息列表、Reducer、Checkpoint 或额外存储机制。那些机制为什么出现，要等我们真的遇到对应问题再研究。

<!-- truncate -->

## 1. 一个被忽略的问题：节点 `return` 之后，谁修改了 `State`？

我们先不用 LangGraph。假设当前退款项目尚未修复，状态是：

```python
state = {
    "project_dir": "/tmp/refund-project",
    "attempts": 0,
    "last_exit": -1,
    "status": "not_run",
}
```

测试节点只返回自己新观察到的三个字段：

```python
# 教学化简；这里的值用来说明更新形状，不是一次真实 pytest 输出。
def check_node(state):
    return {
        "attempts": state["attempts"] + 1,
        "last_exit": 1,
        "status": "failed",
    }
```

运行 `update = check_node(state)` 之后，`update` 已经表示“第一次测试失败”，但原先的 `state["attempts"]` 仍然是 `0`。只要没有其它代码接收并提交结果，**函数返回值不会自动改写调用者的字典**。

最直觉的补充方案是：

```python
update = check_node(state)
state = {**state, **update}
```

在**只有一个节点、每一步只进行一次更新**时，这种做法完全够用。我们不应把它说成错误设计。它甚至帮助我们明确了一个最重要的分工：

- 节点函数负责计算“我想更新什么”；
- 外面的执行系统负责决定“这些更新何时真正生效”。

但是在 LangGraph 中，`StateGraph(State)` 的字段描述是什么时候变成真实的数据容器？节点返回的 `{"status": "failed"}` 又凭什么只更新 `status`，而不把 `project_dir` 等其它字段清掉？

为回答这两个更具体的问题，我们才有必要引入一个新概念：**Channel**。你现在只需把它理解为**一个状态字段在运行时的保存与更新单元**；并不是模型的消息通道，也不是操作系统管道。

## 2. 先跑一个无需 API Key 的真实实验

我们仍然使用贯穿任务：`refund.py` 错把浮点数相加得到的结果当成精确退款金额；`pytest` 会失败。我们的实验代码会**按预先写好的脚本**修复它，再次运行真正的 `pytest`。这里的“修复”是教学演示，绝不声称模型自主推理出修复办法，也不建议把脚本中的退款算法当作生产代码。

下面的纯 Python 程序刻意让节点**只返回局部更新**，不直接修改状态字典；文件改写是另一种明确的外部副作用，不能混为状态更新。运行它不需要 LangChain、LangGraph 或 API Key。

```python title="verify_state_ch07.py（纯 Python 教学实验，已在当前环境运行）"
"""第07章纯 Python 实验：验证局部更新和真实 pytest，非 LangGraph 源码。"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TypedDict

BROKEN = 'def refund_amount():\n    return 0.1 + 0.2\n'
FIXED = (
    'from decimal import Decimal\n\n'
    'def refund_amount():\n'
    '    return Decimal("0.10") + Decimal("0.20")\n'
)
TEST = (
    'from decimal import Decimal\n'
    'from refund import refund_amount\n\n'
    'def test_partial_refund():\n'
    '    assert refund_amount() == Decimal("0.30")\n'
)

class State(TypedDict):
    project_dir: str
    attempts: int
    last_exit: int
    status: str

def check(state: State) -> dict:
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "test_refund.py"],
        cwd=state["project_dir"], capture_output=True, text=True, timeout=30,
    )
    assert completed.returncode in (0, 1), completed.stderr
    return {
        "attempts": state["attempts"] + 1,
        "last_exit": completed.returncode,
        "status": "passed" if completed.returncode == 0 else "failed",
    }

def fix(state: State) -> dict:
    assert state["status"] == "failed"
    Path(state["project_dir"], "refund.py").write_text(FIXED, encoding="utf-8")
    return {"status": "patched"}

def main() -> None:
    with TemporaryDirectory(prefix="ch07-state-") as folder:
        root = Path(folder)
        (root / "refund.py").write_text(BROKEN, encoding="utf-8")
        (root / "test_refund.py").write_text(TEST, encoding="utf-8")
        state: State = {"project_dir": folder, "attempts": 0, "last_exit": -1, "status": "not_run"}
        assert state["attempts"] == 0
        patch1 = check(state)
        assert state["attempts"] == 0  # 节点返回更新不等于原字典已被修改
        state = {**state, **patch1}
        print("check#1", {k: state[k] for k in ("attempts", "last_exit", "status")})
        assert state["status"] == "failed" and state["last_exit"] == 1
        patch2 = fix(state)
        state = {**state, **patch2}
        print("fix", {k: state[k] for k in ("attempts", "last_exit", "status")})
        assert state["status"] == "patched" and state["last_exit"] == 1
        patch3 = check(state)
        state = {**state, **patch3}
        print("check#2", {k: state[k] for k in ("attempts", "last_exit", "status")})
        assert state["attempts"] == 2 and state["last_exit"] == 0 and state["status"] == "passed"
        print("pure_python_assertions: PASS")

if __name__ == "__main__":
    main()
```

运行：

```bash
python verify_state_ch07.py
```

**本次环境实际输出**（临时目录路径没有打印，因此每次可一致观察字段变化）：

```text
check#1 {'attempts': 1, 'last_exit': 1, 'status': 'failed'}
fix {'attempts': 1, 'last_exit': 1, 'status': 'patched'}
check#2 {'attempts': 2, 'last_exit': 0, 'status': 'passed'}
pure_python_assertions: PASS
```

注意第二行：修复节点只返回 `{"status": "patched"}`，但 `attempts=1` 和 `last_exit=1` 没有被冲掉。因为我们的合并动作只覆盖**这次提供的键**。实验里还断言，在调用者执行合并之前，`check()` 的返回值并不会改动原 `state`。

这里已经出现一个有用的设计要求：**运行时应该接受部分更新，而不是要求每个节点重新构造完整状态。** 接下来我们检查 LangGraph 是否确实这样实现。

## 3. 固定源码版本：避免把另一版的实现套进来

本系列继续使用：

| 仓库 | 固定基线 | 当前只看的位置 |
| --- | --- | --- |
| [LangGraph](https://github.com/langchain-ai/langgraph) | `1.2.17`，Commit [`4c52e129eb1ab69a3da58b799f67929f77d81149`](https://github.com/langchain-ai/langgraph/commit/4c52e129eb1ab69a3da58b799f67929f77d81149) | `graph/state.py`、`pregel/_algo.py`、`pregel/_io.py`、`pregel/_write.py`、`channels/last_value.py` |
| [LangChain](https://github.com/langchain-ai/langchain) | `1.4.7`，Commit [`fbb8a35da7023ac0382c3b22f047337861bd3486`](https://github.com/langchain-ai/langchain/commit/fbb8a35da7023ac0382c3b22f047337861bd3486) | 本章不额外钻入 Agent Factory，只承接前文所述的 `create_agent()` 入口 |

以下所有 `langgraph` 源码定位均指上述固定 Commit，不是主分支的实时内容。正文中用三种标注区分证据：**真实源码节选**与仓库相同；**教学化简**只是为了展示因果关系；**公开 API 实验**是供读者运行的示例，不冒充框架内部实现。

本章走的是**普通同步执行、默认状态字段、非并发**路径。对于 `LastValue`，我们会提前看见一种并发冲突的错误，但把解决它的必要机制留到下一章。

## 4. 第一站：`TypedDict` 是怎么变成一组 Channel 的？

用户写的是 `class State(TypedDict)`，它是 Python 中对字典字段的**类型描述**。仅仅定义这个类，不会自动创建一个会保存运行时状态的“智能字典”。

真正的转换发生在构建器登记状态 Schema 时。打开 [LangGraph `graph/state.py` 的 `StateGraph._add_schema()`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/graph/state.py#L317-L347)。它会调用 `_get_channels(schema)`，把得到的字段 Channel 登记在构建器的 `channels` 中。转换规则的落点在同一个文件后面的 [`_get_channels()` / `_get_channel()`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/graph/state.py#L1687-L1741)。

**真实源码节选：**

```python
# graph/state.py, _get_channels()
type_hints = get_type_hints(schema, include_extras=True)
all_keys = {name: _get_channel(name, typ) for name, typ in type_hints.items() if name != "__slots__"}
```

为什么这两行重要？`TypedDict` 在此处不再只是供 IDE 检查的注解。构建器要逐个取出字段类型，并为其找到对应的运行时 Channel。`include_extras=True` 保留了 `Annotated` 等附加声明；至于它什么时候真的必需，我们会在**多个节点写同一个字段**时再讲。

对于本章最普通的字段，如 `attempts: int`、`status: str`，没有写特殊声明，所以 `_get_channel()` 会走默认分支。

**真实源码节选：**

```python
# graph/state.py, _get_channel() 默认回退分支
fallback: LastValue = LastValue(annotation)
fallback.key = name
return fallback
```

这意味着，在目前的普通 Schema 下，构建器实际上为 `project_dir`、`attempts`、`last_exit` 和 `status` 各安排了一个 `LastValue`。现在只需把它理解为“保存字段最近一次成功提交的值”的 Channel；我们不需要立刻学习它所有细节。

这里也要说清楚一个边界：`TypedDict` 对字段形状的描述不等于每次运行时都对 Python 对象进行严格类型检查。尤其不能因为写了 `status: str`，就认为所有不合法的 Python 值都会被它自动拦截。

## 5. 第二站：为什么节点拿到的是 `dict`，而不是四个 Channel 对象？

如果运行时直接把四个 `LastValue` 传给业务节点，每个 `check()` 都得认识框架内部类型。那样反而会让简单的测试逻辑耦合执行引擎。

真正的需求是：**框架维护字段存储；业务节点仍然接收简单的数据结构。**

在上一章的编译过程中，`CompiledStateGraph.attach_node()` 会准备每个节点的输入来源。看 [`graph/state.py:1403–1434`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/graph/state.py#L1403-L1434)。

**真实源码节选：**

```python
# graph/state.py, attach_node() 的 PregelNode 配置
channels=("__root__" if is_single_input else input_channels),
mapper=mapper,
```

这里的 `input_channels` 来自节点指定的输入 Schema。对于我们使用 `TypedDict` 的四字段状态，Runtime 知道要从这四个 Channel 读取数据，再构造节点的输入字典。只有特殊的单字段根状态等情形才走 `__root__` 分支；这一章不展开。

**真正获取普通字段值的地方**是 [`pregel/_io.py` 的 `read_channels()`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/_io.py#L20-L49)，它对每个选中字段通过 Channel 的 `get()` 取值，再组成结果：

```python
# 教学化简：说明本章这次读取的形状，不是库的原函数体。
node_input = {
    "project_dir": project_dir_channel.get(),
    "attempts": attempts_channel.get(),
    "last_exit": last_exit_channel.get(),
    "status": status_channel.get(),
}
```

于是节点仍然能写：

```python
def check(state: State) -> dict:
    print(state["attempts"])
    # ... 真实执行 pytest ...
    return {"attempts": state["attempts"] + 1}
```

在真实执行链中，上一章 [`prepare_single_task()`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/_algo.py#L563-L611) 会通过 `_proc_input(...)` 准备任务输入；任务带着这个输入交给 Runner，最终调用节点。**业务节点并不直接从全局字典读取**，而是从该轮已准备好的 Channel 值获得输入。

别在这里误解成“每个字段都做了深拷贝”。框架定义的是**在执行步骤边界读写字段的规则**，不是承诺将所有 Python 可变对象隔离成深度不可变副本。以后遇到列表、嵌套对象时，仍然应该避免依靠原地修改节点输入来表达图状态更新。

## 6. 第三站：节点只返回 `status`，其它字段为什么还在？

读的问题解决后，接着看写。我们上面的 `fix()` 只返回：

```python
return {"status": "patched"}
```

如果把这段返回值视为新的完整 `State`，那么 `project_dir`、`attempts` 和 `last_exit` 就都丢了。LangGraph 需要的是**字段级更新**，不是“节点输出替换整个状态对象”。

在 [`CompiledStateGraph.attach_node()`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/graph/state.py#L1335-L1394) 内部，关键步骤是 `_get_updates()`：它处理普通 `dict` 输出时，只保留其中合法的状态字段，转换为 `(key, value)` 写入对。

**真实源码节选：**

```python
# graph/state.py, attach_node() 内部
elif isinstance(input, dict):
    return [(k, v) for k, v in input.items() if k in output_keys]
```

这三行足以解释本章最重要的输入/输出变化：

```text
fix() 返回                  编译产物登记的状态写入
{"status": "patched"}   →   [("status", "patched")]

不存在以下写入：
("attempts", None)
("last_exit", None)
("project_dir", None)
```

这里并不是因为其它字段自动从节点输出里“补全”。**根本没有对那些字段提交新值**；它们原先的 Channel 值继续保存。这也意味着：未写某字段，与显式写入 `{"field": None}` 是两种不同操作；后者是对该字段提交一个 `None` 值，具体语义受 Channel 规则支配。

为什么这个转换设计得如此靠近 `attach_node()`？因为图编译时已经知道**哪些字段存在，哪些输出允许写回**。业务节点只需表明业务变化；具体如何把输出映射为运行时可识别的写入，由编译时装配的写入器负责。

## 7. 第四站：为什么转换出 `(key, value)` 仍然不算提交？

现在我们得到 `[('status', 'patched')]`，但它还只是一个**候选写入**，不是已经被其它节点看到的最终状态。这恰好和上一章的 Loop / Runner 边界衔接。

`attach_node()` 为节点配置了一个 `ChannelWrite(...)`。它负责将节点输出加工成写入对，并交给执行任务的写入回调。打开 [`pregel/_write.py` 的 `ChannelWrite.do_write()`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/_write.py#L97-L117)。

**真实源码节选：**

```python
# pregel/_write.py, ChannelWrite.do_write()
write: TYPE_SEND = config[CONF][CONFIG_KEY_SEND]
write(_assemble_writes(writes))
```

这两行有一个容易被忽略的关键词：`CONFIG_KEY_SEND`。**`ChannelWrite` 不在这里自己调用 `LastValue.update()`**；它把写入列表送进 Runtime 提供的回调。

Runtime 在准备任务时，会创建任务专属的 `writes` 集合，并在配置里注入写入函数。对应源码是 [`pregel/_algo.py:628–713`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/_algo.py#L628-L713)。

**真实源码节选：**

```python
# pregel/_algo.py, prepare_single_task() 的任务配置片段
CONFIG_KEY_SEND: writes.extend,
```

把它们连起来，输出流变得可追踪：

```text
节点 fix(state)
  返回 {"status": "patched"}
           │
           ▼
attach_node() 装配的 _get_updates()
  变为 [("status", "patched")]
           │
           ▼
ChannelWrite.do_write()
  调用 CONFIG_KEY_SEND
           │
           ▼
任务写入列表 writes
  保存 ("status", "patched")
           │
           ▼
【尚未到其它节点可读取的新状态】
```

这里特意停一下：业务节点运行了，也返回了，写入列表里已经有内容了；但是我们**还不能**说“全图所有 Channel 都立刻被更新”。接下来才到步边界的应用阶段。

## 8. 第五站：`apply_writes()` 终于让值进入 Channel

上一章说过，Loop 在这一轮节点完成后调用更新逻辑。现在我们知道这一轮的任务写入是什么，就可以继续读 [`pregel/_algo.py` 的 `apply_writes()`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/_algo.py#L222-L310)。

这个函数除了状态更新，还负责版本、触发器等事情。现在只看它的**核心数据路径**：

1. 收集本轮任务的 `task.writes`；
2. 按 Channel 名分组，例如 `status → ["patched"]`；
3. 对每个已知 Channel 调用 `channels[chan].update(vals)`；
4. 记录哪些 Channel 有更新，供下一步判断触发关系。

**真实源码节选：**

```python
# pregel/_algo.py, apply_writes() 中真正应用值的语句
if channels[chan].update(vals) and next_version is not None:
    checkpoint["channel_versions"][chan] = next_version
```

这一步与上一节有本质差异：`ChannelWrite` 提交的是任务写入，`apply_writes()` 才调用 Channel 的更新方法。从而**“节点返回了什么”**和**“运行时接受什么、何时写入”**不是同一个动作。

对于当前只有一个节点每步执行的退款任务，数据可以这样观察：

| 时刻 | `attempts` | `last_exit` | `status` | 当前写入 |
| --- | ---: | ---: | --- | --- |
| 初始输入 | 0 | -1 | `not_run` | 输入进入状态 Channel |
| 首次 `check` 的更新被应用 | 1 | 1 | `failed` | `attempts=1`, `last_exit=1`, `status=failed` |
| `fix` 的更新被应用 | 1 | 1 | `patched` | 只有 `status=patched` |
| 第二次 `check` 的更新被应用 | 2 | 0 | `passed` | `attempts=2`, `last_exit=0`, `status=passed` |

这个表格不是说 Runtime 内部用四列关系数据库保存状态，也不是一份真实的 Checkpoint 转储。它只是把**Channel 逐步形成的可见业务状态**重组成我们容易理解的形状。

## 9. 第六站：普通字段为什么是 `LastValue`，而不是任意覆盖的字典？

回到第 4 节：`attempts`、`last_exit`、`status` 都使用普通类型注解，没有额外更新规则，因此默认是 `LastValue`。它做的事很简单：**在一次有效更新中记住新值，并在下一次读取时返回该值。**

打开 [`channels/last_value.py`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/channels/last_value.py#L18-L66)。重点不是背诵类的全部方法，而是对照我们的数据：

- `update(["patched"])`：允许一个新值，并用它更新当前字段；
- `get()`：返回该字段已保存的值；
- `update([])`：没有新值时不改动已有值；
- `update(["failed", "passed"])`：在同一个执行步对同一个默认 Channel 提交多个值，会触发更新冲突错误。

**真实源码节选：**

```python
# channels/last_value.py, LastValue.update() 的核心成功路径
self.value = values[-1]
return True
```

注意这里的“最后一次”不是说**同一步并发写入时永远挑最后一个覆盖掉其它值**。同一个 `LastValue` 每步最多接受一个值；源码还对 `len(values) != 1` 作了明确检查并抛出 `InvalidUpdateError`。那正是我们下一章要面对的失败场景。

对今天的顺序执行链来说，规则很合适：第一次测试让 `status` 变为 `failed`；修复节点让它变为 `patched`；第二次测试让它变为 `passed`。这三个动作发生在不同的执行步，所以不会被误判为“同一步多次写入”。

## 10. 一个容易误会的例外：条件路由为什么能看到本节点刚返回的更新？

读到这里，细心的读者可能会发现一个看似矛盾的问题：

> 我们说普通状态更新要到步边界才提交；但 `check` 的条件路由必须在看到 `status="passed"` 后才能决定走 `END`。如果路由还看见旧值 `patched`，图岂不是会再跑一次 `fix`？

这个问题非常好，因为它迫使我们区分**下一轮普通节点的状态读取**，与**当前节点的条件边用于决策时的读取**。它们并不完全是同一条路径。

在这个固定版本中，[`pregel/_algo.py` 的 `local_read()`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/_algo.py#L180-L216) 提供了 `fresh` 参数。当条件路由需要“包含本节点当前写入”的状态时，它会从 Channel 的副本读取，并把**当前任务的写入**应用到这些副本上；这样路由便可以判断 `status` 的新值，而不必抢先提交整个执行步的更新。

**真实源码节选：**

```python
# pregel/_algo.py, local_read() 的关键动作
cc = channels[k].copy()
cc.update(updated[k])
local_channels[k] = cc
```

**请不要把这个例外放大为“其它所有节点都能立刻看到未提交写入”。** 它解释的是条件分支为什么可以据当前节点输出选择下一跳；普通节点之间的状态可见性仍遵循运行时的步骤边界。

对于当前退款案例：

```text
check 节点读取 attempts=1, status="patched"
   │
   ├─ 真正执行 pytest → 退出码 0
   │
   └─ 产生 status="passed" 的任务写入
             │
             ├─ 条件路由以当前节点的“新鲜视图”判断 → END
             │
             └─ 步结束后，Runtime 把写入应用到 Channel
```

至此，“为什么图能正确终止”和“为什么其它普通节点要等下一步才能读新状态”这两个问题就不矛盾了。

## 11. 用真实框架接口重现：三个节点更新、四个状态字段

现在把前面的纯 Python 节点带回 LangGraph。我们故意不调用真实 LLM，让学习目标锁定在**State 的读取和部分写回**；`check` 仍然调用真正的 `pytest`。整个案例的业务测试、修复输入与前文一致。

```python title="demo_stategraph_ch07.py（公开 API 实验，本次环境未执行框架部分）"
"""第07章框架复现：需 langgraph==1.2.17 + pytest。此环境未运行。"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

BROKEN = 'def refund_amount():\n    return 0.1 + 0.2\n'
FIXED = (
    'from decimal import Decimal\n\n'
    'def refund_amount():\n'
    '    return Decimal("0.10") + Decimal("0.20")\n'
)
TEST = (
    'from decimal import Decimal\n'
    'from refund import refund_amount\n\n'
    'def test_partial_refund():\n'
    '    assert refund_amount() == Decimal("0.30")\n'
)

class State(TypedDict):
    project_dir: str
    attempts: int
    last_exit: int
    status: str

def check(state: State) -> dict:
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "test_refund.py"],
        cwd=state["project_dir"], capture_output=True, text=True, timeout=30,
    )
    if completed.returncode not in (0, 1):
        raise RuntimeError(completed.stderr)
    return {
        "attempts": state["attempts"] + 1,
        "last_exit": completed.returncode,
        "status": "passed" if completed.returncode == 0 else "failed",
    }

def fix(state: State) -> dict:
    Path(state["project_dir"], "refund.py").write_text(FIXED, encoding="utf-8")
    return {"status": "patched"}

def route(state: State) -> str:
    return "done" if state["status"] == "passed" else "retry"

def make_graph():
    builder = StateGraph(State)
    builder.add_node("check", check)
    builder.add_node("fix", fix)
    builder.add_edge(START, "check")
    builder.add_conditional_edges("check", route, {"done": END, "retry": "fix"})
    builder.add_edge("fix", "check")
    return builder.compile()

def main() -> None:
    with TemporaryDirectory(prefix="ch07-stategraph-") as folder:
        root = Path(folder)
        (root / "refund.py").write_text(BROKEN, encoding="utf-8")
        (root / "test_refund.py").write_text(TEST, encoding="utf-8")
        graph = make_graph()
        initial: State = {
            "project_dir": folder, "attempts": 0, "last_exit": -1, "status": "not_run",
        }
        updates = list(graph.stream(initial, stream_mode="updates"))
        for item in updates:
            print(item)
        assert [next(iter(item)) for item in updates] == ["check", "fix", "check"]
        assert updates[0]["check"] == {"attempts": 1, "last_exit": 1, "status": "failed"}
        assert updates[1]["fix"] == {"status": "patched"}
        assert updates[2]["check"] == {"attempts": 2, "last_exit": 0, "status": "passed"}

if __name__ == "__main__":
    main()
```

复现命令（请在隔离的虚拟环境中运行；不要把工具命令暴露给不受信任的模型）：

```bash
python -m venv .venv
# macOS / Linux
source .venv/bin/activate
# Windows PowerShell 请使用：.venv\Scripts\Activate.ps1
python -m pip install "langgraph==1.2.17" "pytest>=8,<10"
python demo_stategraph_ch07.py
```

如果安装依赖成功且 API 与固定版本一致，**预期** `stream_mode="updates"` 应按顺序给出这些业务节点更新（下面为**预期结构，不是本次环境的框架实测日志**）：

```text
{'check': {'attempts': 1, 'last_exit': 1, 'status': 'failed'}}
{'fix': {'status': 'patched'}}
{'check': {'attempts': 2, 'last_exit': 0, 'status': 'passed'}}
```

特别观察中间一行：`fix` 只返回 `status`。这正是本章要验证的框架行为：**节点更新是局部字段写入，而不是重建完整状态。**

也要注意实验的安全与边界：它只修改 `TemporaryDirectory()` 创建的临时项目；这里的 `pytest` 代码由我们编写并了解内容。真实 Coding Agent 若允许模型调用 shell、修改用户文件，需要另外设置权限、执行隔离和副作用管控；`StateGraph` 自身不等于沙箱。

## 12. 从一个节点的返回值，重新拼出完整的执行链

直到今天，我们才能把“状态从哪里来、去哪里”补进前一章的 Pregel 轨迹：

```text
开发者定义 State(TypedDict)
        │
        ▼
StateGraph._add_schema() / _get_channels()
  每个普通状态字段准备默认 LastValue
        │
        ▼
compile() / CompiledStateGraph.attach_node()
  指定节点读哪些 Channel、如何把输出转换成字段写入
        │
        ▼
graph.invoke(...) 或 graph.stream(...)
        │
        ▼
PregelLoop 准备任务 → prepare_single_task() 准备节点输入
        │
        ▼
读取 Channel.get() → 拼成业务节点看到的 State
        │
        ▼
Runner 调用 check(state) / fix(state)
        │
        ▼
节点返回局部 dict
        │
        ▼
_get_updates() → ChannelWrite → 任务写入列表
        │
        ▼
本步结束：apply_writes() → 每个 Channel.update(vals)
        │
        ▼
下一步的节点再读取提交后的状态
```

这个过程解决的，并不是“字典不能保存数据”。一个字典当然可以保存数据。**Channel 的价值在于把业务节点的计算，与运行时字段读写和更新规则分开**。这样，业务节点仍然是容易理解的 Python 函数，而 Runtime 可以在确定的执行边界处理它们的写入。

与此同时，也不能神化 Channel。它不会因为字段声明为 `int` 就自动保证模型推理正确，不会自动避免文件写入的副作用，也不会在这个无持久化实验中突然带来断点续跑能力。我们以后讨论 Checkpoint 时，再区分“运行时通道的当前值”和“持久化存储的快照”。

## 13. 自检：你现在能解释这五个问题吗？

1. `StateGraph(State)` 中的 `TypedDict` 为什么不等于运行时直接共享一个可变字典？
2. 普通 `status: str` 在构建器里默认被映射成什么 Channel？
3. 为什么 `fix()` 返回 `{"status": "patched"}` 不会把 `attempts` 等字段清掉？
4. `ChannelWrite` 已经产生写入，为什么还要等 `apply_writes()`？
5. 为什么条件路由可以看到 `check()` 的最新结果，但同一步里的另一个普通节点不应依赖这种立即可见性？

如果能够按照 `_get_channels → attach_node → read_channels → _get_updates → ChannelWrite → apply_writes → LastValue.update` 这条路径复述，并说清每一步的输入、职责和输出，本章就完成了。

## 14. 下一章：两个节点同时修改一个字段时，会发生什么？

目前为止，我们只让一个业务节点在一个执行步里写同一个状态字段，因此 `LastValue` 每步收到的是零个或一个值。这个约束很自然，也足够支撑退款案例的“测试—修复—再测试”。

但现在假设我们将工作并行化：一个节点跑单元测试，另一个节点跑静态检查；它们都在同一步尝试更新 `status`。在我们最简单的设计里，会出现两个候选写入。**应该覆盖、拒绝，还是按规则组合？** 如果我们只是照搬 Python 的 `dict.update()`，写入顺序可能无意中决定最后状态。

这不是要提前讲完并发架构，而是一个精确到字段的反例，它刚好足以推动我们研究下一种机制。

**下一篇：第 08 章｜多个节点同时更新 State，为什么不能直接覆盖？**（建议文件名：`09how-to-reduce-concurrent-state.md`）。我们会从默认 `LastValue` 的真实冲突出发，只有在看到它为什么不够用之后，再引入 `Annotated` 和 Reducer。

---

## 附录 A：推荐源码阅读顺序（固定 Commit）

建议按下表顺序阅读，别从 `StateGraph` 的文件头一次性读到末尾：

| 当前要回答的一个问题 | 固定源码位置 |
| --- | --- |
| Schema 如何被登记？ | [`StateGraph._add_schema()`，`graph/state.py:317–347`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/graph/state.py#L317-L347) |
| 普通类型字段为何变成 `LastValue`？ | [`_get_channels()` / `_get_channel()`，`graph/state.py:1687–1741`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/graph/state.py#L1687-L1741) |
| 节点读什么字段、写什么结果？ | [`CompiledStateGraph.attach_node()`，`graph/state.py:1335–1434`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/graph/state.py#L1335-L1434) |
| 字段值怎样被读成节点输入？ | [`read_channels()`，`pregel/_io.py:20–49`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/_io.py#L20-L49)、[`prepare_single_task()`，`pregel/_algo.py:563–611`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/_algo.py#L563-L611) |
| 节点的部分输出如何变成写入对？ | [`_get_updates()`，`graph/state.py:1346–1382`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/graph/state.py#L1346-L1382) |
| 为什么写入不是立刻更新字段？ | [`ChannelWrite.do_write()`，`pregel/_write.py:97–117`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/_write.py#L97-L117) |
| 任务写入从哪里收集？ | [`prepare_single_task()`，`pregel/_algo.py:628–713`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/_algo.py#L628-L713) |
| 本步写入何时应用？ | [`apply_writes()`，`pregel/_algo.py:222–310`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/_algo.py#L222-L310) |
| `LastValue` 在什么条件下接收或拒绝更新？ | [`LastValue.update()`，`channels/last_value.py:51–66`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/channels/last_value.py#L51-L66) |
| 当前节点的条件路由怎样读到自己的新写入？ | [`local_read()`，`pregel/_algo.py:180–216`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/_algo.py#L180-L216) |

## 附录 B：实验真实性声明

- **已经执行：** 当前环境的纯 Python 教学实验 `verify_state_ch07.py`；它真实启动了 `pytest`，确认初次退出码是 `1`，按照明确写好的脚本修复后退出码为 `0`，并断言所有中间状态。
- **已经检查但未执行：** `demo_stategraph_ch07.py` 通过 Python 语法编译检查；本文写作环境未安装 `langgraph==1.2.17`，所以它的 `stream` 输出仍是预期结构，不是本次实测数据。
- **已经核对：** 文中列出的 LangGraph `1.2.17` / Commit `4c52e129...` 的源文件和关键函数路径、返回值与写入机制；不能把阅读静态源码说成“完成了端到端框架验证”。
- **未覆盖：** 异步任务、同一步并发写入、Reducer、Checkpoint、外部操作的精确一次保证、生产级退款业务规则。这些都不属于本章唯一问题的范围。

**本章的结束句是：** Node 看见的是 State，返回的是局部更新；Runtime 管理的是 Channel，在步边界把更新应用到字段上。读懂这条数据流，下一章的并发冲突才会变成一个自然的问题，而不是突然出现的术语。
