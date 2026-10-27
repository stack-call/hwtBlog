---
title: "09｜为什么 invoke() 只能看到结果，而 stream() 能看到过程？"
date: 2026-10-27
slug: /langchain-langgraph-source/how-to-stream-graph-execution
description: "沿退款测试 Agent 的真实执行链，从 invoke 的最终状态出发，逐层追踪 LangGraph 1.2.17 的 stream、SyncQueue、PregelLoop.output_writes、after_tick 和 _output，理解 updates、values 与 custom 事件究竟从哪里产生。"
tags:
  - LangChain
  - LangGraph
  - Streaming
  - Pregel
  - Runtime
  - StateGraph
  - 源码精读
  - Python
---

> **LangChain × LangGraph 源码精读 · 第 09 章**  
> 上一篇：[第 08 章｜两个节点同时更新 State，为什么不能直接覆盖？](./09how-to-reduce-concurrent-state.md)  
> **本章唯一的问题：** 同一个 Agent 正在“运行测试 → 修改代码 → 再运行测试”，为什么 `invoke()` 的调用者通常只能等最终结果，而 `stream()` 可以让调用者在执行尚未结束时看到过程？

上一章，我们已经解释了 Pregel 怎样在执行步结束时合并多个节点的写入。到这里，框架内部其实已经能够回答“当前执行到了哪里”：`model` 做出了哪个决定、`tools` 刚刚运行了什么、`pytest` 的退出码是多少。**但框架知道，不意味着调用者也能及时知道。**

设想我们把之前的退款测试 Agent 放进一个终端界面：用户输入“修复 `refund.py` 中部分退款的金额精度问题”，随后屏幕安静了十几秒，最后只显示：

```text
{'attempts': 2, 'last_exit': 0, 'patched': True, ...}
```

最后状态是有用的，可它回答不了这些更早就该回答的问题：第一次测试是否失败？Agent 现在是在修改文件，还是在反复运行 `pytest`？如果测试需要一分钟，用户怎么知道它并没有卡住？

<!-- truncate -->

## 1. 最简单的办法：执行完以后再打印日志

我们先不用 LangGraph。将上一章的 Agent Loop 简化为：

```python title="教学化简：不是 LangGraph 源码"
state = initial_state()
while True:
    action = model_node(state)
    if action == "end":
        break
    update = tools_node(state, action)
    state.update(update)

print("最终状态:", state)
```

这段程序完全合理。如果唯一的需求是“完成以后判断退款测试有没有通过”，它已经足够。问题发生在**调用者需要在程序仍在运行时收到反馈**的那一刻。把 `print()` 移到循环内当然可以在本地终端显示消息，但这样会把“执行逻辑”和“日志的接收方式”绑定在一起：Web 页面、命令行、测试代码都只能去读标准输出，没法方便地按节点、按事件类型消费。

于是需要的不是另一个 Agent 机制，而是一个更小的能力：**执行者每产生一条可观察信息，就把它交给调用者，而不必等待整个图结束。**

用 Python 最熟悉的形式，这可以是一个生成器：

```python title="教学化简：建立最小直觉"
def execute():
    yield {"node": "model", "action": "test"}
    # 实际运行工具……
    yield {"node": "tools", "last_exit": 1}
    # 再次决策、修复、重测……
    yield {"node": "tools", "last_exit": 0}

for event in execute():
    print(event)
```

这里的 `yield` 只负责把信息交出去，并不会自动调用模型、运行测试或保存状态。这正是我们要保留的分工：**Runtime 负责执行，流式接口负责让外界观察执行。**

还需要注意一个 Python 细节：生成器函数被调用时通常只返回一个迭代器；它的主体在迭代时才推进。LangGraph 的 `stream()` 也不能只创建迭代器就假设任务已经全部执行，调用方需要消费它。

## 2. 我们真正想看到三种不同的东西

仍然只看同一个退款任务。当 `model` 决定“先运行测试”时，界面希望知道**刚刚哪个节点做出了什么决定**；当 `pytest` 失败、工具节点返回时，我们希望看到**这次节点的状态更新**；当更新被 Runtime 应用后，我们还希望看到**现在整个 State 是什么**。

再把限制加大一点：`pytest` 很慢。此时工具尚未返回，节点级更新也没有产生；但用户仍然希望看到“测试开始了”。这要求工具能够主动报告**执行过程中的进度**。

这些信息不能混为一谈。先给它们一个最小的对应关系：

| 当前要观察的事实 | 最合适的输出 | 准确边界 |
| --- | --- | --- |
| `model` 返回 `{"action": "test"}` | `updates` | 显示节点返回的更新；不是完整 State |
| 一步写入被应用后，`attempts`、`last_exit` 当前是多少 | `values` | 显示该时点可读取的状态；可能包含初始输入对应的值 |
| `pytest` 还没结束，但已启动 | `custom` | 由节点显式发事件；不是 LangGraph 自动捕获每行 stdout |

此时才有必要知道 LangGraph 对外暴露的名字：`graph.stream(..., stream_mode="updates")` 或 `"values"`；需要同时看多类事件时可传列表。本文只研究这三个模式，其余模式等出现相应问题再引入。

## 3. 先用真实 pytest 跑出一条可观察的事件链

源码不是靠输出样例证明的。先做一个不需要 API Key、也不依赖 LangGraph 安装的实验。我们仍然使用临时目录里的两个文件：

```python title="refund.py（实验初始内容，故意错误）"
def refund_amount():
    return 0.1 + 0.2
```

```python title="test_refund.py"
from decimal import Decimal
from refund import refund_amount


def test_partial_refund():
    assert refund_amount() == Decimal("0.30")
```

这次不再使用单纯的 `print()`。我们让执行者 `yield (mode, payload)`，这样调用方可以按事件类型观察同一条执行链。下面是完整的纯 Python 教学实验；**它模拟的是流式输出的因果边界，不是 LangGraph 的内部实现，也不是一个具备推理能力的大模型**。

```python title="verify_streaming_pure.py（纯 Python 教学实验，已实际运行）"
from __future__ import annotations
from collections.abc import Iterator
from pathlib import Path
from tempfile import TemporaryDirectory
import subprocess
import sys

BROKEN = '''def refund_amount():\n    return 0.1 + 0.2\n'''
FIXED = '''from decimal import Decimal\n\ndef refund_amount():\n    return Decimal("0.30")\n'''
TEST = '''from decimal import Decimal\nfrom refund import refund_amount\n\ndef test_partial_refund():\n    assert refund_amount() == Decimal("0.30")\n'''

def choose(state: dict) -> str:
    if state['attempts'] == 0: return 'test'
    if state['last_exit'] != 0 and not state['patched']: return 'patch'
    if state['last_exit'] != 0: return 'test'
    return 'end'

def read_stream(project: Path) -> Iterator[tuple[str, dict]]:
    state = dict(attempts=0, last_exit=None, patched=False, action='')
    while True:
        action = choose(state)
        state['action'] = action
        yield ('updates', {'model': {'action': action}})
        yield ('values', dict(state))
        if action == 'end': break
        if action == 'test':
            yield ('custom', {'phase': 'pytest:start'})
            result = subprocess.run(
                [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', 'test_refund.py'],
                cwd=project, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
            )
            state.update(attempts=state['attempts'] + 1, last_exit=result.returncode)
            yield ('custom', {'phase': 'pytest:exit', 'exit': result.returncode})
            update = {'attempts': state['attempts'], 'last_exit': state['last_exit']}
        else:
            yield ('custom', {'phase': 'patch:start'})
            (project / 'refund.py').write_text(FIXED, encoding='utf-8')
            state['patched'] = True
            yield ('custom', {'phase': 'patch:done'})
            update = {'patched': True}
        yield ('updates', {'tools': update})
        yield ('values', dict(state))

with TemporaryDirectory(prefix='stream-ch09-') as temp:
    root=Path(temp)
    (root / 'refund.py').write_text(BROKEN, encoding='utf-8')
    (root / 'test_refund.py').write_text(TEST, encoding='utf-8')
    events=list(read_stream(root))
    for mode, payload in events:
        if mode == 'values':
            print('values', {'action':payload['action'], 'attempts':payload['attempts'],'last_exit':payload['last_exit'],'patched':payload['patched']})
        else:
            print(mode, payload)
    tools = [obj['tools'] for mode, obj in events if mode == 'updates' and 'tools' in obj]
    assert tools == [
        {'attempts':1,'last_exit':1},
        {'patched':True},
        {'attempts':2,'last_exit':0},
    ], tools
    assert events[-1][1]['action'] == 'end'
    assert events[-1][1]['last_exit'] == 0
    assert (root/'refund.py').read_text(encoding='utf-8') == FIXED
    print('ASSERTIONS PASSED')
```

运行方法：

```bash
python -m pip install pytest
python verify_streaming_pure.py
```

因为脚本使用 `TemporaryDirectory`，不会修改你的真实工作项目。脚本里的“模型决策”由固定分支实现；测试由**真实的 `python -m pytest` 子进程**执行，修复操作则是教学脚本直接重写临时目录中的 `refund.py`。两者不能混为“模型真的理解了代码并自主修复”。

为了方便阅读，将刚才实际执行的输出按模式分组摘出关键轨迹（其余状态快照省略）：

```text
updates {'model': {'action': 'test'}}
custom  {'phase': 'pytest:start'}
custom  {'phase': 'pytest:exit', 'exit': 1}
updates {'tools': {'attempts': 1, 'last_exit': 1}}

updates {'model': {'action': 'patch'}}
custom  {'phase': 'patch:start'}
custom  {'phase': 'patch:done'}
updates {'tools': {'patched': True}}

updates {'model': {'action': 'test'}}
custom  {'phase': 'pytest:start'}
custom  {'phase': 'pytest:exit', 'exit': 0}
updates {'tools': {'attempts': 2, 'last_exit': 0}}
updates {'model': {'action': 'end'}}
ASSERTIONS PASSED
```

这是**本地真实运行过**的结果，其中 `exit=1` 与 `exit=0` 分别来自两次真实的 `pytest` 调用。三个观察结果值得保留：

- `model` 与 `tools` 的更新分别出现，不需要等到图结束才展示。
- `custom` 的“测试开始”可以发生在 `pytest` 返回以前；但它并没有自动产生 pytest 的逐行输出。
- 同一个字段更新被应用以后，完整 State 才能体现新值；完整状态与“某个节点刚返回了什么”是两种视角。

现在问题才从“为什么需要输出事件”，变成“真实 LangGraph 是怎么把这些事件送出来的”。

## 4. 源码第一站：`invoke()` 和 `stream()` 不是两套执行引擎

固定本系列基线：LangGraph **`1.2.17`，Commit [`4c52e129eb1ab69a3da58b799f67929f77d81149`](https://github.com/langchain-ai/langgraph/tree/4c52e129eb1ab69a3da58b799f67929f77d81149)**。下面提到的函数和字段均以此版本为准。

先去看 [`pregel/main.py` 的 `Pregel.invoke()`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/main.py#L3704-L3819)。真实源码里，`invoke()` 本质上会循环消费 `self.stream(...)` 的结果。如果选择默认的 `values` 模式，就持续记录最新输出，并在调用结束时返回；选择其他流模式时，它也能把事件收集成列表返回。

```python title="教学化简：保留真实 invoke() 的主要控制关系，非逐行源码"
def invoke(input):
    latest = None
    for chunk in self.stream(input, stream_mode="values"):
        latest = chunk
    return latest
```

为什么这段简化很重要？因为它马上纠正了一个常见误解：**`invoke()` 不是完全没有中间结果，而是调用者在同步调用期间拿不到这些中间结果；底层依然沿 `stream()` 的执行路径推进。**

再往下看，`stream()` 并没有另起一个专门执行 Agent 的循环。它使用的仍是上一章认识的 `SyncPregelLoop` 和 `PregelRunner`。`model → tools → model` 的控制关系没有变化，增加的是**把执行过程中产生的信息交给外部的通道**。

因此，改用 `stream()` 的收益首先是可观察性，而不是自动增加模型推理、工具权限或错误恢复能力。

## 5. 为什么要引入一个 Queue？节点执行和消费事件发生在不同位置

我们已经知道需要产生事件。可 `tools_node()` 是 Runtime 调用的，终端界面的 `for event in graph.stream(...)` 则在外层；两者并不是相同的函数栈位置。最简单的 `return` 只能交还节点最终的更新，无法为整个框架建立一个统一的事件出口。

于是需要一个暂存和转发事件的位置。在固定版本的 [`pregel/main.py`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/main.py#L2683-L2705) 中，可以直接找到这一行真实源码：

```python title="真实源码节选：pregel/main.py"
stream = SyncQueue()
```

随后，运行时创建 `SyncPregelLoop` 时，将事件写入回调交给内部的 `StreamProtocol`：源码位置见 [`main.py:L2823-L2844`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/main.py#L2823-L2844)。

此时只需要理解 `StreamProtocol` 的最小职责：它把**允许输出的模式**和一个**写入事件的回调**放在一起，提供给 Loop。事件的具体内容来自节点写入、状态提交或主动报告的进度，并不是 `Queue` 凭空算出来的。

```text
节点完成 / 提交状态 / 节点报告进度
              │
              ▼
      按 stream_mode 生成事件
              │
              ▼
    StreamProtocol → SyncQueue
                         │
                         ▼
                 stream() 的外层迭代器
                         │
                         ▼
                   调用者的 for 循环
```

这也解释了为什么 `stream_mode` 不是一个仅影响 `print()` 的格式选项。它会影响 Runtime 选择、产生和传出的事件。

## 6. `updates` 从哪里来？先有节点写入，才有节点级事件

先看最常见的 `updates`。假设 `pytest` 第一次返回退出码 1，工具节点返回：

```python
{"attempts": 1, "last_exit": 1}
```

上一章知道，节点返回的字典不能绕过 Runtime 直接覆盖 State。这个结果会先进入任务写入路径。现在沿着 [`pregel/_loop.py` 的 `put_writes()`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/_loop.py#L427-L537) 看：在记录写入之后，它会调用 `output_writes(...)`，再由这个方法根据请求的模式决定是否构建对外的更新事件。

关键的真实实现见 [`output_writes()`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/_loop.py#L1536-L1585)：它选择 `updates` 时，会交给 `map_output_updates` 把任务写入整理成按节点名归类的数据。

`map_output_updates()` 的源码在 [`pregel/_io.py:L110-L166`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/_io.py#L110-L166)。它的输出结构可以理解为：

```python title="教学示例：节点更新事件的外形，不是函数源码"
{"tools": {"attempts": 1, "last_exit": 1}}
```

这一步有三个细节：

第一，**这里输出的是节点产生的更新，而不是整份 State**。如果 `project_dir` 没被工具节点修改，它就不会凭空出现在这条 `tools` 更新中。

第二，**“收到 updates 事件”和“本步所有字段已完成最终合并”不是同一个时刻**。普通节点结束时可以产生任务更新事件；之前研究的 Reducer 仍然要按照步边界合并写入。这个区别在两个并发节点更新同一字段时尤其重要。

第三，`updates` 通常以节点完成为观察边界。长时间运行的同步 `pytest` 还没返回时，不能指望它已经产生 `{"tools": {"last_exit": ...}}`；退出码毕竟尚未得到。

到了这里，读者已经能根据一次 `tools` 返回值，解释为什么流里会出现 `{"tools": {...}}`。然而，如果界面想知道“所有字段现在是什么”，仅靠更新事件还不够。

## 7. `values` 为什么不同？它必须在状态写入应用之后读取

我们把注意力移到上一章已经理解的 `after_tick()`。固定版本 [`pregel/_loop.py:L705-L744`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/_loop.py#L705-L744) 的真实路径是：收集本轮任务写入，调用 `apply_writes()` 应用状态变更，然后在相关输出字段确实更新时，向 `values` 模式提供新的状态值。

`values` 对应的读取逻辑进入 [`map_output_values()`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/_io.py#L93-L108)。它依靠 Channel 中此时可读取的值来构建输出，而不是拼接最近一个节点的返回字典。

于是就出现了两种自然不同的视角：

```text
工具节点刚结束：
    updates → {"tools": {"last_exit": 1, "attempts": 1}}

Runtime 在步边界应用写入后：
    values  → {"project_dir": "...", "action": "test",
               "last_exit": 1, "attempts": 1, "patched": False}
```

上面是**用于说明数据外形的教学示例**，不是在本地执行 LangGraph 得到的逐字输出。在真实的 `values` 流里，还可能看到由初始输入写入产生的状态事件；不要机械地认为“总是每个业务节点恰好一个 values”。

这里还有一条必须守住的边界：当多个节点在同一步完成时，`updates` 能分别告诉我们“每个节点提交了什么候选更新”；`values` 则更适合观察**这些写入按字段合并规则应用后的状态**。这就是为什么上一章的 Reducer 和这一章的 Streaming 能沿同一条执行路径接起来。

## 8. 最后一站：事件怎样变成调用者能 `for` 到的内容？

仅仅往队列里写事件还不够。调用者要的是 Python 迭代器产出的值。因此在 [`pregel/main.py:L2881-L2922`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/main.py#L2881-L2922) 中，我们会看到这样的控制顺序：

```text
loop.tick()                   准备本轮可执行任务
    ↓
runner.tick()                 执行任务；过程中可能写入事件
    ↓
_output(...)                  把队列中可用事件转为 stream 的产出
    ↓
loop.after_tick()             统一应用本步写入；可能生成 values 事件
    ↓
下一轮 / 退出阶段再次 _output(...)  继续交出剩余事件
```

这是**依源码整理的控制流示意**，不是逐行复制。注意 `runner.tick()` 与 `_output()` 的协作：当开启 `custom` 等需要尽早消费事件的模式时，`stream()` 会建立等待机制，让事件不必总是等到长任务结束才被转发；但这不是对操作系统进程 stdout 的自动流式读取。

真正把队列项变成 Python `yield` 的地方在 [`pregel/main.py` 的 `_output()`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/main.py#L4129-L4188)。它持续读取队列，按需要打印调试数据，并依照 `stream_mode`、是否有子图、流版本等条件决定交给调用者什么形状的事件。

现在整条链终于接通了：

```text
model / tools
    │ 返回更新，或主动报告进度
    ▼
PregelLoop：output_writes / after_tick
    │ 按 updates / values / custom 等生成事件
    ▼
StreamProtocol → SyncQueue
    │
    ▼
Pregel.stream() → _output() → yield
    │
    ▼
for event in graph.stream(...)
```

我们没有换掉上一章的执行引擎，而是把原来藏在执行引擎中的可观察信息，沿着一个明确的出口送给外部。

## 9. `updates` 仍然无法说明“长时间运行的 pytest 正在做什么”

到这里问题又自然前进了一层。`pytest` 可能需要一分钟才能返回。我们的 `updates` 事件只能在工具节点完成后告诉外界退出码；`values` 也不能凭空知道正在执行的子进程已经跑到了哪里。

于是需要一个**不等节点返回，就由节点主动报告进度**的出口。LangGraph 在这个场景下提供 `custom`。例如在工具节点里：

```python title="公开 API：可嵌入 LangGraph 节点的最小示例"
from langgraph.config import get_stream_writer


def tools_node(state):
    writer = get_stream_writer()
    writer({"phase": "pytest:start"})
    result = run_pytest(state["project_dir"])  # 教学占位函数
    writer({"phase": "pytest:exit", "exit": result.returncode})
    return {"last_exit": result.returncode}
```

`run_pytest` 在这里仅表示执行工具的占位函数，不是假装存在于 LangGraph 内部的 API。真实的公开入口是 [`langgraph.config.get_stream_writer()`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/config.py#L118-L146)，而 `stream()` 设置自定义 writer 的位置可以在 [`pregel/main.py:L2770-L2807`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/main.py#L2770-L2807) 核对。

需要避免三个误解。第一，调用 `get_stream_writer()` 并不会改变 State；这些进度事件不是 Reducer 的输入。第二，这两条事件只是“开始/结束”，**不是 pytest 的逐行日志**；如果想逐行报告 stdout，需要改写工具，真正读取子进程输出并逐行调用 writer。第三，启用 `custom` 不能保证任意阻塞调用都毫无延迟地推送 UI；调用方的消费速度、执行线程以及传输层同样影响可见时机。

这就是为什么我们把 `custom` 留到这一刻才讲：**只有当节点级结果不能满足“执行中进度”的需求时，它才变得必要。**

## 10. 用真正的 LangGraph API 复现同一条链

现在把纯 Python 的教学事件链换成框架公开 API。案例没有变化：先运行真实 `pytest`；如果失败，写入教学用修复；再运行真实 `pytest`；通过后结束。模型部分仍是脚本化分支，因此**只能验证调度和事件输出，不代表模型智能已被验证**。

安装方式：

```bash
python -m pip install "langgraph==1.2.17" pytest
```

保存为 `demo_streaming_langgraph.py`：

```python title="demo_streaming_langgraph.py（LangGraph 公开 API，可复现；本环境未运行）"
from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import subprocess
import sys
from typing import TypedDict

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph


class AgentState(TypedDict):
    project_dir: str
    action: str
    attempts: int
    last_exit: int | None
    patched: bool


def model_node(state: AgentState) -> dict:
    if state["attempts"] == 0:
        action = "test"
    elif state["last_exit"] != 0 and not state["patched"]:
        action = "patch"
    elif state["last_exit"] != 0:
        action = "test"
    else:
        action = "end"
    return {"action": action}


def tools_node(state: AgentState) -> dict:
    project = Path(state["project_dir"])
    writer = get_stream_writer()

    if state["action"] == "patch":
        writer({"phase": "patch:start"})
        (project / "refund.py").write_text(
            'from decimal import Decimal\n\ndef refund_amount():\n    return Decimal("0.30")\n',
            encoding="utf-8",
        )
        writer({"phase": "patch:done"})
        return {"patched": True}

    writer({"phase": "pytest:start"})
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_refund.py"],
        cwd=project,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    writer({"phase": "pytest:exit", "exit": result.returncode})
    return {"attempts": state["attempts"] + 1, "last_exit": result.returncode}


def route(state: AgentState) -> str:
    return "end" if state["action"] == "end" else "tools"


def build_graph():
    builder = StateGraph(AgentState)
    builder.add_node("model", model_node)
    builder.add_node("tools", tools_node)
    builder.add_edge(START, "model")
    builder.add_conditional_edges("model", route, {"tools": "tools", "end": END})
    builder.add_edge("tools", "model")
    return builder.compile()


if __name__ == "__main__":
    broken = 'def refund_amount():\n    return 0.1 + 0.2\n'
    test = (
        'from decimal import Decimal\n'
        'from refund import refund_amount\n'
        'def test_partial_refund():\n'
        '    assert refund_amount() == Decimal("0.30")\n'
    )
    with TemporaryDirectory(prefix="langgraph-stream-ch09-") as dirname:
        project = Path(dirname)
        (project / "refund.py").write_text(broken, encoding="utf-8")
        (project / "test_refund.py").write_text(test, encoding="utf-8")
        graph = build_graph()
        initial: AgentState = {
            "project_dir": str(project),
            "action": "",
            "attempts": 0,
            "last_exit": None,
            "patched": False,
        }
        for mode, chunk in graph.stream(
            initial, stream_mode=["custom", "updates", "values"], version="v1"
        ):
            if mode == "values":
                print(mode, {k: chunk.get(k) for k in ("action", "attempts", "last_exit", "patched")})
            else:
                print(mode, chunk)
```

执行：

```bash
python demo_streaming_langgraph.py
```

这里特意同时订阅 `custom`、`updates`、`values`。本实验采用 **`version="v1"`**，所以当 `stream_mode` 是列表时，外层迭代结果可以拆成 `(mode, payload)`。你应当检查的**行为条件**是：

1. 第一轮测试产生 `pytest:exit` 且退出码为 `1`，之后能看到工具节点的测试更新。
2. 脚本化 `model` 在失败信息到来后选择 `patch`，工具节点写入修复文件。
3. 第二轮真实测试产生退出码 `0`，最终模型选择 `end`。
4. `values` 提供当前状态快照；`updates` 保留节点名及其更新；`custom` 展示节点主动发出的进度。

以上是**可复现时应验证的行为条件，不是我已经从真实框架运行得到的日志**。截至本文写作，本地环境有 `pytest`，但未安装 `langgraph==1.2.17`；这份框架脚本已通过 Python 语法编译检查，尚未在该版本框架下执行。与之对应的纯 Python 实验则已经真实运行并通过断言。

### 为什么明确传 `version="v1"`？

这不是无关紧要的版本细节。固定 Commit 的 [`Pregel.stream()`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/main.py#L2560-L2615) 同时支持 `v1` 和 `v2` 输出形状。`v1` 是这里的默认值；当订阅多个模式时，它用类似 `(mode, data)` 的元组表示。该 Commit 也支持显式传入 `version="v2"`，以统一结构返回事件：

```python title="接口外形示意，非本地运行输出"
{"type": "updates", "ns": (), "data": {"tools": {"last_exit": 1}}}
```

因此不要把 `v1` 的 `(mode, chunk)` 解包代码，原样复制到 `version="v2"` 的调用里。我们本章为了集中理解数据从哪里产生，固定使用最简单的 `v1` 多模式消费方式。

## 11. Streaming 能解决什么，不能解决什么？

回看开头的问题，现在可以准确回答：Streaming 让 Agent 的**可观察执行进度**暴露给调用方；它不改变模型决策，不替代工具执行，不负责保存可恢复的状态，也不意味着每一种内部活动都会自动变成事件。

如果把真实 `pytest` 的失败退出码当作工具的普通返回结果，Agent 可以继续修复；若工具节点**直接抛出未被处理的 Python 异常**，流式执行也可能随异常终止。你能收到多少事件，取决于异常发生在什么时候以及你选择订阅什么，而不是因为使用 `stream()` 就天然拥有错误恢复能力。

还要区分 `stream_mode="messages"` 与我们当前的实验：它面向模型消息/Token 等事件，在真实模型提供相应流式回调时才有可观察的内容；**脚本化假模型不能被拿来证明大模型逐 Token 输出。** 本章暂时不展开它。

最后，`stream()` 不等于 Checkpoint。刚才即使看到了每次 `pytest` 的进度，如果执行进程突然退出，下一次运行能不能恢复到上一步，仍然是一个独立问题。恰好，这就是下一章需要解决的限制。

## 12. 回看我们是怎样得到这一设计的

本章没有先罗列 Streaming API，而是顺着退款任务的实际困境走了一条路径：

```text
invoke() 结束才有最终结果
       ↓
需要在执行中观察节点做了什么
       ↓
updates：观察节点返回的局部更新
       ↓
需要知道写入合并后的完整状态
       ↓
values：观察步边界应用写入后的 State
       ↓
工具运行很久，还没有局部更新
       ↓
custom：节点主动报告执行中的进度
       ↓
Runtime 内部的事件如何到达调用方？
       ↓
StreamProtocol → SyncQueue → _output() → yield
```

到这里，我们已经沿真实调用链理解了：**图如何执行、节点怎样更新 State、更新怎样合并，以及这些过程怎样被外界观察。** 但我们还没有解决“执行到一半进程消失”的问题。

下一篇：[**第 10 章｜执行中断以后，LangGraph 怎样找回上一步？**](./11how-to-checkpoint-graph-state.md)，预计文件名：`11how-to-checkpoint-graph-state.md`。我们将仍然让退款 Agent 跑同一个任务，只是在“第一次测试失败、准备修复代码”之后人为中断执行，看看没有持久化状态时会丢失什么；然后再引入 Checkpoint，沿真实源码解释保存与恢复的边界。

---

## 源码定位与实验说明

- **版本固定**：LangGraph `1.2.17`，Git Commit [`4c52e129eb1ab69a3da58b799f67929f77d81149`](https://github.com/langchain-ai/langgraph/tree/4c52e129eb1ab69a3da58b799f67929f77d81149)。本章直接涉及 LangGraph Runtime，不需要新引入 LangChain 的另一套版本入口。
- **真实源码入口**：[`Pregel.invoke()` / `Pregel.stream()` / `_output()`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/main.py)、[`PregelLoop.output_writes()` / `after_tick()`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/_loop.py)、[`map_output_updates()` / `map_output_values()`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/_io.py)、[`get_stream_writer()`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/config.py)。
- **实际运行**：纯 Python 教学实验（包括两次真实 `pytest`），观察到退出码 `1 → 0`，并通过事件序列断言。
- **未运行部分**：`StateGraph` 框架公开 API 脚本已进行 Python 语法检查；由于当前环境未安装固定的 LangGraph 包，未宣称其输出已经实测。
- **安全边界**：示例仅在临时目录读写 `refund.py`，调用本地 pytest；它不是执行不可信代码的安全沙箱。真实 Coding Agent 的工具权限、文件路径和超时应另行约束。
