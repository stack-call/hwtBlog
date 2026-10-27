---
title: "10｜执行中断以后，LangGraph 怎样找回上一步？"
date: 2026-10-27
slug: /langchain-langgraph-source/how-to-checkpoint-graph-state
description: "沿退款测试 Agent 的同一条执行链，从测试失败后进程退出的反例出发，逐层阅读 LangGraph 的 Checkpoint、InMemorySaver、PregelLoop 保存与恢复机制，并区分进程内暂停、磁盘恢复和外部工具副作用。"
tags:
  - LangChain
  - LangGraph
  - Checkpoint
  - Pregel
  - StateGraph
  - 源码精读
  - Python
---

> **LangChain × LangGraph 源码精读 · 第 10 章**  
> 上一篇：[第 09 章｜为什么 invoke() 只能看到结果，而 stream() 能看到过程？](./10how-to-stream-graph-execution.md)  
> **本章唯一的问题：** 当退款 Agent 已经得到第一次 `pytest` 的失败结果、正要修改文件时，执行却中断了，下一次调用怎样从这里继续，而不是把已经做过的事情重新做一遍？

上一章的 `stream()` 让我们看到了测试失败、修改文件、重新测试等过程。但**看得到过程**和**中断后能够找回过程**是两件事。假设终端在输出下面两行后突然退出：

```text
model → run pytest
tools → pytest exit=1
# 进程退出，尚未修改 refund.py
```

用户重新运行程序，希望它接下来修改 `refund.py`，而不是又从第一次测试开始。问题是：新进程没有上一个进程里的 Python 字典，没有正在运行的 `while`，也不知道下一步本来应该做什么。

本文继续使用同一个 Coding Agent 任务。我们不会先介绍一套复杂的持久化系统，而是从**究竟丢了什么信息**开始；每多出现一项具体缺失，才引入对应的最小机制。

<!-- truncate -->

## 1. `stream()` 已经告诉我们执行到哪里，为什么还是不能恢复？

回忆前几章手写 Agent Loop 的形状：

```python title="教学化简：不是 LangGraph 源码"
state = {"attempts": 0, "last_exit": None, "patched": False}

while True:
    action = choose_action(state)
    if action == "end":
        break
    update = execute_tool(action)
    state.update(update)
```

如果第一次 `pytest` 已经返回失败码，内存里的 `state` 可能是：

```python
{"attempts": 1, "last_exit": 1, "patched": False}
```

这时候进程退出，再启动一遍，`state` 又被初始化为 `attempts=0`。我们可以读取上一章输出的文本日志，却不能仅凭一行日志确信接下来该执行哪一个节点。比如，是否已经成功写入了修复文件？失败结果已经交给模型了吗？上一次是在节点执行前中断，还是执行到一半才出错？

最自然的第一反应是把 `state` 保存成 JSON 文件。这个办法**在简单串行循环里确实有效**，但单独保存字段值仍然留下一个问题：

```text
保存的状态：attempts=1, last_exit=1, patched=False
恢复以后：谁来决定下一步从哪里进入？
```

你可以再运行一次 `choose_action(state)`。这对我们当前这个非常小的脚本也能工作。可是，如果某一次是“修改文件节点刚结束、重新测试尚未开始”，或者图中存在多个可以执行的后继节点，**只看业务字段并不总能唯一恢复调度进度**。

因此我们需要保存两类事实：

1. **数据进度**：截至某个明确边界，状态字段已经变成了什么。
2. **执行进度**：哪些更新已经提交，后面还有哪些任务需要运行。

此时才值得引出本章的新概念：**Checkpoint，是 Runtime 可用于重新构建某个执行边界的持久化记录**。它不意味着保存整段 Python 调用栈，更不是把整个进程休眠到文件中。

## 2. 先设计一个最小的“保存点”

目前我们只需要串行处理一个退款 Bug，所以最简单的记录可以写成：

```json title="概念示例：教学化简，不是 LangGraph 实际的 Checkpoint 结构"
{
  "thread_id": "refund-demo",
  "next_node": "patch",
  "state": {
    "attempts": 1,
    "last_exit": 1,
    "patched": false
  }
}
```

这里的 `next_node` 就是我们人为保存的继续位置：第一次测试已经做完，下一步修改文件。它是一种**适用于这条教学脚本的简化表示**；真实 LangGraph 并不是在基础 Checkpoint 中简单保存一个名为 `next_node` 的字符串。真实格式我们稍后到源码中核对。

把保存点放在“第一次测试完成之后、修改文件开始之前”，恢复行为就能观察得非常清楚：

```text
初次运行                    重新启动
   │                          │
运行真实 pytest              读取保存点
   │                          │
exit=1                        │
   │                          │
记录 state + 下一步           │
   │                          │
退出进程 ──持久化记录───────→ 直接进入 patch
                              │
                           修改 refund.py
                              │
                          再运行真实 pytest
                              │
                            exit=0
```

注意我们没有发明“模型记忆能力”。这里需要的是非常朴素的执行记录：**上一次已经完成的动作是什么，下一次从哪里继续。**

## 3. 两个独立 Python 进程，真实验证“退出—恢复”

这一实验无需 API Key，也不依赖 LangGraph 安装。和前几章一样，它仍在临时目录中创建存在浮点精度问题的 `refund.py`：

```python title="refund.py（初始错误实现）"
def refund_amount():
    return 0.1 + 0.2
```

测试仍然是：

```python title="test_refund.py"
from decimal import Decimal
from refund import refund_amount


def test_partial_refund():
    assert refund_amount() == Decimal("0.30")
```

这次的验证比单纯在一个函数中模拟“恢复”更严格：**第一阶段运行一个 Python 子进程，让它执行失败测试并退出；第二阶段再启动另一个独立 Python 子进程，从磁盘 JSON 中恢复。** 代码如下。

```python title="verify_checkpoint_pure.py（纯 Python 教学实验，已实际运行）"
"""A teaching-only disk checkpoint exercise with two independent Python processes."""
from __future__ import annotations
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import subprocess
import sys

BROKEN = 'def refund_amount():\n    return 0.1 + 0.2\n'
FIXED = 'from decimal import Decimal\n\ndef refund_amount():\n    return Decimal("0.30")\n'
TEST = ('from decimal import Decimal\n'
        'from refund import refund_amount\n'
        '\n'
        'def test_partial_refund():\n'
        '    assert refund_amount() == Decimal("0.30")\n')


def pytest_exit(project: Path) -> int:
    result = subprocess.run(
        [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', 'test_refund.py'],
        cwd=project,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    with (project / 'pytest_runs.log').open('a', encoding='utf-8') as log:
        log.write(f'exit={result.returncode}\n')
    return result.returncode


def write_snapshot(project: Path, snapshot: dict) -> None:
    (project / 'checkpoint.json').write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2), encoding='utf-8'
    )


def start(project: Path) -> None:
    (project / 'refund.py').write_text(BROKEN, encoding='utf-8')
    (project / 'test_refund.py').write_text(TEST, encoding='utf-8')
    status = pytest_exit(project)
    assert status == 1, status
    write_snapshot(project, {
        'thread_id': 'refund-demo',
        'next_node': 'patch',
        'state': {'attempts': 1, 'last_exit': 1, 'patched': False},
    })
    print('PROCESS-1: pytest exit=1; checkpoint.next_node=patch; exiting')


def resume(project: Path) -> None:
    snapshot = json.loads((project / 'checkpoint.json').read_text(encoding='utf-8'))
    assert snapshot['thread_id'] == 'refund-demo'
    assert snapshot['next_node'] == 'patch'
    assert snapshot['state']['attempts'] == 1
    (project / 'refund.py').write_text(FIXED, encoding='utf-8')
    status = pytest_exit(project)
    assert status == 0, status
    snapshot['state'].update(attempts=2, last_exit=0, patched=True)
    snapshot['next_node'] = 'END'
    write_snapshot(project, snapshot)
    print('PROCESS-2: restored next_node=patch; pytest exit=0; END')


def verify() -> None:
    with TemporaryDirectory(prefix='checkpoint-ch10-') as temp:
        project = Path(temp)
        for operation in ('start', 'resume'):
            done = subprocess.run(
                [sys.executable, __file__, operation, str(project)],
                check=True, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True,
            )
            print(done.stdout.strip())
            if operation == 'start':
                assert (project / 'refund.py').read_text(encoding='utf-8') == BROKEN
                state = json.loads((project / 'checkpoint.json').read_text(encoding='utf-8'))
                assert state['next_node'] == 'patch'
        runs = (project / 'pytest_runs.log').read_text(encoding='utf-8').splitlines()
        final = json.loads((project / 'checkpoint.json').read_text(encoding='utf-8'))
        assert runs == ['exit=1', 'exit=0'], runs
        assert final['next_node'] == 'END' and final['state']['attempts'] == 2
        assert (project / 'refund.py').read_text(encoding='utf-8') == FIXED
        print('ASSERTIONS PASSED: two processes, two real pytest runs, one restart')


if __name__ == '__main__':
    if len(sys.argv) == 1:
        verify()
    elif sys.argv[1] == 'start':
        start(Path(sys.argv[2]))
    elif sys.argv[1] == 'resume':
        resume(Path(sys.argv[2]))
    else:
        raise SystemExit(f'Unknown operation {sys.argv[1]}')
```

执行方式：

```bash
python -m pip install pytest
python verify_checkpoint_pure.py
```

下面是本次实际运行的输出：

```text
PROCESS-1: pytest exit=1; checkpoint.next_node=patch; exiting
PROCESS-2: restored next_node=patch; pytest exit=0; END
ASSERTIONS PASSED: two processes, two real pytest runs, one restart
```

这不是“只给出预期输出”的演示。实验真正调用了两个独立 Python 进程、两次 `python -m pytest`，并断言 `pytest_runs.log` 中恰好是 `exit=1` 和 `exit=0` 两条记录；第一次结束后 `refund.py` 仍然是错误实现，第二次才被教学脚本改写。

当然，这也不等于我们已经验证了 LangGraph 的 Checkpoint：代码里 `checkpoint.json` 是我们自己设计的格式，`next_node` 是自己保存的指针，保存过程也没有处理原子提交、并发写入或写入过程断电。**这个实验只验证“把数据进度和继续位置留下，两个进程之间就有可能接续工作”这一必要条件**。下面我们才去看真实框架怎样解决它。

## 4. 真实 Checkpoint 为什么不只保存一个 State 字典？

请先打开固定 Commit 的 [`Checkpoint` 类型定义](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/checkpoint/langgraph/checkpoint/base/__init__.py)。注意这是 `libs/checkpoint` 子项目中的代码，而不是 `StateGraph` 文件里的某个普通 Python 字典。

在 `langgraph.checkpoint.base` 中，`Checkpoint` 的主要字段包括：

| 字段 | 此刻为什么需要它 |
| --- | --- |
| `id`、`ts` | 识别是哪一次保存，允许定位不同的保存点 |
| `channel_values` | 还原当时各 Channel 的已保存值 |
| `channel_versions` | 记录各 Channel 到达哪个版本 |
| `versions_seen` | 记录节点已经看过哪些 Channel 版本，为下一轮任务判断提供依据 |
| `updated_channels` | 记录当前 Checkpoint 涉及的更新 Channel 信息 |

这是固定 Commit 的**真实类型结构概述**，不是我们自己定义的接口。关键证据在 [`Checkpoint` 的字段定义](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/checkpoint/langgraph/checkpoint/base/__init__.py) 中。其中 `versions_seen` 的源码注释明确指出，它被用于判断下次执行哪个节点。

至此可以回答前面的疑问：LangGraph 不需要在基础 Checkpoint 中写死一个 `next_node="patch"`。它保留 Channel 值和版本进度，Runtime 加载这些信息后，根据已有图定义、触发关系与保存的写入情况，**重新准备待执行任务**。我们在公开 API 里看到的 `StateSnapshot.next`，是面向调用者的下一个任务视图，不能当成基础 Checkpoint 内部的同名字段。

为什么要做得比我们的 JSON 复杂？因为上一章已经出现了 Reducer 和并行写入：有可能多条任务在同一个执行步运行；单独保存一个字符串不仅无法表达这种情况，还无法可靠区分“哪个节点已经看到过哪次更新”。

这里仍不需要展开 Channel 的所有版本管理算法。记住这一句就够了：**State 字段告诉我们现在是什么，Channel 版本与已消费进度帮助 Runtime 判断下一步该做什么。**

## 5. 第一个真正的接口：谁负责把保存点放到哪里？

如果 Runtime 直接执行 `open("checkpoint.json")`，它就只能支持某一种磁盘格式。但生产系统可能需要进程内存、SQLite、PostgreSQL 等不同存储位置，所以保存行为应该与执行引擎分开。

对应源码在 [`BaseCheckpointSaver`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/checkpoint/langgraph/checkpoint/base/__init__.py) 及其具体实现中。现在只关注最相关的三个动作：

- `get_tuple(config)`：按配置读取某一个保存点及其相关信息。
- `put(config, checkpoint, metadata, new_versions)`：写入一个 Checkpoint，并返回定位该保存点的配置。
- `put_writes(config, writes, task_id, ...)`：保存某个任务产生、但未必已经并入完整 Checkpoint 的写入。

刚开始理解恢复路径时，先看 `get_tuple` 和 `put` 即可。我们在本章后面才会解释为什么还需要 `put_writes`。

最容易验证的实现是 [`InMemorySaver`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/checkpoint/langgraph/checkpoint/memory/__init__.py)：

```python title="真实源码节选：InMemorySaver.get_tuple() 中的关键读取键"
thread_id: str = config["configurable"]["thread_id"]
checkpoint_ns: str = config["configurable"].get("checkpoint_ns", "")
```

这两行说明为什么使用带 Checkpointer 的图时，需要通过 `configurable.thread_id` 指明一条执行历史：**Saver 需要知道应该在哪条历史里查找记录。** `checkpoint_ns` 等更多字段用于区分图的保存命名空间；这一章暂时只在根图使用默认命名空间。

如果提供特定 `checkpoint_id`，`InMemorySaver.get_tuple()` 会尝试读取该保存点；没有指定时则获取对应线程命名空间里的最新保存点。它取出的并不只是 `state`，还会返回 `CheckpointTuple`，包括 `checkpoint`、`metadata`、`parent_config`、`pending_writes` 等信息。谁会消费这些信息？下面看 Runtime。

这里不要误解 `thread_id`：它是查找执行历史的标识，不是 Python 线程对象，也不是用户身份验证机制。不同执行应该使用不会意外冲突的 ID，并由应用控制访问权限。

## 6. 再回到 StateGraph：为什么编译时要传 checkpointer？

我们在第 05 章已经见过 `StateGraph.compile()`：它把图的节点和路由规则装配成可执行的 `CompiledStateGraph`。现在新出现的工程限制是**运行时需要有地方保存和读取执行进度**。

因此编译入口多了一个此前不必关心的参数：

```python title="真实公开接口用法：将 Saver 交给编译后的图"
from langgraph.checkpoint.memory import InMemorySaver

checkpointer = InMemorySaver()
graph = builder.compile(checkpointer=checkpointer)
```

这段是使用固定版本公开 API 的示例，不是 `compile()` 内部源码节选。`builder` 仍然只负责定义图结构；`checkpointer` 随编译结果进入 Runtime，供执行过程读写。把它独立出来，图节点不需要知道保存后端的数据库表结构。

但这还不构成恢复：如果下一次调用换一个 `thread_id`，Saver 找到的是另一条历史；如果下一次调用仍然传入一份**新的普通输入**，也不能假设它一定会被解释成“接着上一次中断继续”。

因此我们需要一个明确的继续动作：

```python title="真实公开接口用法：在同一条执行历史继续"
config = {"configurable": {"thread_id": "refund-ch10"}}
# 第一次调用先执行到明确的暂停点
# graph.invoke(initial_input, config=config)

# 下一次告诉图：不提供新的业务输入，继续保存的执行进度
# graph.invoke(None, config=config)
```

当前这两行还只是为了把问题放在眼前。我们很快用实际节点把它们连起来。

## 7. 保存发生在哪里？回到已经读过的 PregelLoop

现在可以从前几章已经熟悉的调用链继续向下：

```text
CompiledStateGraph.invoke(...)
             │
             ▼
         Pregel.stream(...)
             │
             ▼
       PregelLoop 的执行步
             │
     本轮节点产生写入
             │
             ▼
       apply_writes(...)
             │
             ▼
   将当前执行进度交给 Checkpointer
```

在固定 Commit 的 [`PregelLoop.after_tick()`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/_loop.py) 中，我们能看到前一章讲过的 `apply_writes(...)`，随后调用保存步骤：

```python title="真实源码节选：PregelLoop.after_tick()"
# save checkpoint
self._put_checkpoint({"source": "loop"})
```

为什么不是节点一返回就立刻把完整 State 保存下来？因为上一章已经看到，同一个执行步内可能有多个节点写入；Runtime 要先收集并应用本轮更新，再建立能够代表这个边界的保存记录。否则拿到的可能是一半旧、一半新的状态。

这里还有一个必须准确表达的细节：`_put_checkpoint()` 将保存工作交给 Checkpointer 相关调用，但“已提交一个运行时执行步”不等于“任何时刻突然断电都绝对不会丢失记录”。具体何时真正写入底层存储，还受保存后端和 durability 选项影响。我们先完成正常暂停与继续路径，稍后再讨论故障边界。

## 8. 恢复时为什么不是重新执行所有节点？

Checkpoint 保存了，下一次运行从哪里开始？继续看 [`PregelLoop.tick()`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/_loop.py) 的关键调用：

```python title="真实源码节选：PregelLoop.tick() 的任务准备入口"
self.tasks = prepare_next_tasks(
    self.checkpoint,
    self.checkpoint_pending_writes,
    self.nodes,
    self.channels,
    # 其余参数省略
)
```

这段截取省略了其余参数；它并不是一个可以直接复制运行的函数调用示例。这里最重要的是前两个输入：已加载的 Checkpoint，以及相关的 pending writes。`prepare_next_tasks(...)` 不是凭空猜测“之前运行到哪一行 Python”，而是根据**保存的通道与执行进度、图的节点关系和未完成情况**准备接下来的任务。

加载保存点的逻辑属于循环进入阶段，而不是每个业务节点自己打开磁盘文件。恢复时，Runtime 会根据配置向 Checkpointer 获取保存记录，恢复 Channel 进度，并在下一次 `tick()` 中重建任务集。只有走通这条链，才能解释为什么同一张已经编译的图可以在第二次调用里继续前一次未完成的工作。

别把“恢复”理解成无条件跳过一切以前见过的工具：已完成并被正确记录的任务可能不必重跑；**未完成的任务可能重跑**。这一区别在并行任务与意外失败时尤其重要。

## 9. 先让图在 `patch` 前停住，而不引入人工审批

怎样稳定制造“第一次测试失败，但还没有修改文件”的状态？最简单的做法不是真的强制杀进程，而是先用一个明确的**节点前暂停边界**。

假设图只有两个业务节点：

```text
START → test ── pytest 通过 ──→ END
           │
        pytest 失败
           ▼
         patch
           │
           └──────────────→ test
```

我们要在进入 `patch` 之前停住。使用 `compile(interrupt_before=["patch"])`，此时暂停发生在节点执行之前；`patch` 本身还没有被调用。固定 Commit 的 [`PregelLoop.tick()`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/_loop.py) 会先准备下一批任务，再检查这个中断条件；条件满足则结束当前执行段，把后续执行留给恢复调用。

这个机制用来制造**可复现的暂停点**，并不包含“人类批准才能修改文件”的权限判断。后者是下一章的问题。我们现在只关心：停下来以后，Checkpoint 是否足以支撑继续。

## 10. 用真实 LangGraph API 复现保存与恢复

下面的框架实验沿用同一个退款任务，并且继续采用**脚本化决策**，不调用真实模型。选择 `InMemorySaver` 是为了最小化依赖，让我们看清 Runtime 的行为，而不是展示跨进程持久化。

```python title="demo_checkpoint_langgraph.py（LangGraph 1.2.17 API；未在本地运行）"
"""LangGraph fixed-commit API reproducible demo; syntax checked, not executed here."""
from __future__ import annotations
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TypedDict
import subprocess
import sys

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import START, END, StateGraph

BROKEN = 'def refund_amount():\n    return 0.1 + 0.2\n'
FIXED = 'from decimal import Decimal\n\ndef refund_amount():\n    return Decimal("0.30")\n'
TEST = ('from decimal import Decimal\n'
        'from refund import refund_amount\n'
        '\n'
        'def test_partial_refund():\n'
        '    assert refund_amount() == Decimal("0.30")\n')


class RepairState(TypedDict):
    project_dir: str
    attempts: int
    last_exit: int | None
    patched: bool


def run_test(state: RepairState) -> dict:
    result = subprocess.run(
        [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', 'test_refund.py'],
        cwd=state['project_dir'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, check=False,
    )
    print('pytest exit:', result.returncode)
    return {'attempts': state['attempts'] + 1, 'last_exit': result.returncode}


def choose_after_test(state: RepairState) -> str:
    if state['last_exit'] == 0 or state['patched']:
        return END
    return 'patch'


def patch_refund(state: RepairState) -> dict:
    (Path(state['project_dir']) / 'refund.py').write_text(FIXED, encoding='utf-8')
    return {'patched': True}


def build_graph():
    builder = StateGraph(RepairState)
    builder.add_node('test', run_test)
    builder.add_node('patch', patch_refund)
    builder.add_edge(START, 'test')
    builder.add_conditional_edges('test', choose_after_test)
    builder.add_edge('patch', 'test')
    saver = InMemorySaver()
    return builder.compile(checkpointer=saver, interrupt_before=['patch'])


if __name__ == '__main__':
    with TemporaryDirectory(prefix='langgraph-checkpoint-') as temp:
        project = Path(temp)
        (project / 'refund.py').write_text(BROKEN, encoding='utf-8')
        (project / 'test_refund.py').write_text(TEST, encoding='utf-8')
        graph = build_graph()
        config = {'configurable': {'thread_id': 'refund-ch10'}}
        initial: RepairState = {
            'project_dir': str(project), 'attempts': 0,
            'last_exit': None, 'patched': False,
        }
        graph.invoke(initial, config=config)
        paused = graph.get_state(config)
        print('paused:', paused.values, 'next:', paused.next)
        assert paused.values['attempts'] == 1
        assert paused.values['last_exit'] == 1
        assert paused.next == ('patch',)
        assert (project / 'refund.py').read_text(encoding='utf-8') == BROKEN
        graph.invoke(None, config=config)
        done = graph.get_state(config)
        print('done:', done.values, 'next:', done.next)
        assert done.values['attempts'] == 2
        assert done.values['last_exit'] == 0
        assert done.next == ()
        assert (project / 'refund.py').read_text(encoding='utf-8') == FIXED
```

如果你的环境已经安装指定版本及测试依赖：

```bash
python -m pip install "langgraph==1.2.17" pytest
python demo_checkpoint_langgraph.py
```

**应观察的行为条件**如下。下面写的是代码内断言需要满足的状态，而不是声称已经从框架运行得到的日志。

| 时刻 | 已完成 | Checkpoint 对外可见状态 | 下一步 |
| --- | --- | --- | --- |
| 第一次 `invoke(initial, config)` 后 | `test` 运行一次，`pytest` 失败 | `attempts=1`，`last_exit=1`，`patched=False` | `("patch",)` |
| 第二次 `invoke(None, config)` 后 | `patch` 修改临时文件，`test` 再次运行且通过 | `attempts=2`，`last_exit=0`，`patched=True` | `()`，无待运行任务 |

这份框架示例里的 `graph.get_state(config)` 返回的是公开 API 的 State 快照视图：`values` 让我们检查业务状态，`next` 让我们检查等待运行的节点。它们不是 `InMemorySaver` 原封不动返回的数据库记录字段。

注意两点。第一，两个 `invoke()` 使用同一个 `graph` 对象和同一个 `InMemorySaver`，**并不模拟进程退出后恢复**；我们在第 3 节用磁盘 JSON 和两个 Python 进程验证的是另一件事。第二，`patch_refund()` 直接写临时目录里的文件，这仍然是教学脚本主动修改代码，不能说成模型真的自行修复了 Bug。

该框架脚本已进行 Python 语法检查；当前执行环境没有安装固定版本 `langgraph==1.2.17`，因此**本章没有宣称框架示例已运行通过**。

## 11. 如果某个执行步里有两个并行节点，其中一个先完成怎么办？

现在才值得引入刚才暂时搁置的 `put_writes`。第 08 章已经见过两条并行证据：一个节点执行 `pytest`，另一个节点检查 `refund.py`。假设两者在同一个执行步里运行：

```text
             ┌─ pytest ── 已完成 ─┐
上一保存点 ───┤                  ├─ 本步状态提交
             └─ inspect ─ 失败 ──┘
```

如果只有“整步结束才记录状态”一种保存方式，某个节点失败时，已经完成的兄弟节点结果也可能完全无从找回。于是出现一个比简单 State 快照更细的需求：**记录某个任务已经产生的写入，即使本执行步尚未整体结束。**

这才是 [`put_writes()`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/checkpoint/langgraph/checkpoint/memory/__init__.py) 存在的理由。固定 Commit 的 `PregelLoop.put_writes()` 会收集任务写入，并在适用的 durability 模式下通过 Checkpointer 保存这些 pending writes；恢复时，它们可以重新对应到已经完成的任务。

`CheckpointTuple` 也因此包含 `pending_writes`。在 [`PregelLoop.tick()`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/_loop.py) 的恢复路径里，已记录的写入可能被重新关联到相应任务，而仍未完成的任务继续执行或重试。**Checkpoint 与 pending writes 不是两套相互替代的方案：前者标记可恢复的执行边界，后者补充这个边界附近已产生的任务结果。**

先把这个边界记住，不需要在本章提前研究它们的全部序列化格式、DeltaChannel 或并行重放算法。我们当前只需要回答：为什么框架不能只保存一个 `dict`？因为恢复既涉及**状态**，也涉及**任务执行进度**。

## 12. Checkpoint 保证了什么，又没有保证什么？

我们终于可以把开头的问题完整回答了：Checkpoint 使 LangGraph 能够在明确的执行边界保存状态和调度进度，之后利用相同执行历史的记录继续任务。但“能够继续”有几个非常具体的限制。

**第一，`InMemorySaver` 不是跨进程持久化后端。** 它的记录存放在当前 Python 进程内；如果进程真的退出，新进程创建一个全新的 `InMemorySaver()`，原来的数据不会凭空回来。第 3 节磁盘 JSON 的两个进程实验不能拿来证明 `InMemorySaver` 具有磁盘持久性。需要真实进程重启后的恢复，应选择持久化 Saver（例如适合环境的数据库后端），并在同一条 `thread_id` 下恢复。

**第二，恢复的是执行进度，不是被暂停的 Python 调用栈。** 如果某个节点运行到一半突然失败，节点内局部变量不一定会被保存。恢复通常从有记录的边界重新准备任务；未完成任务可能重新执行。

**第三，State 恢复不意味着外部副作用恰好执行一次。** 比如 `patch_refund()` 已经写了文件，但进程在相关记录可靠保存前退出。之后如果这一步重试，就可能再次写文件。我们这里的覆盖式写入相对容易重复执行；但扣款、发送邮件、调用生产数据库等操作不能随便假设重复执行是无害的。它们需要幂等键、事务或相应补偿设计，Checkpoint 本身不会替外部系统提供“精确一次”保证。

**第四，存储语义还要区分 durability。** 固定版本提供不同持久化时机的选择；采用异步保存时，执行速度和崩溃窗口之间存在权衡。不能把节点刚返回的瞬间等同于“记录已永久落盘”。我们本章并没有进行断电注入、数据库事务或不同 durability 模式的崩溃测试，因此不会宣称实测得到某一种崩溃保证。

**第五，恢复必须依赖兼容的图与外部环境。** 如果恢复前删除了图中的 `patch` 节点、更改了状态格式，或者临时目录早已消失，即使 Checkpoint 仍在，后续任务也未必能正确继续。本章通过 `TemporaryDirectory` 将实验范围控制在同一次测试会话中，并不构成真实项目升级后的兼容性方案。

这些边界都不是否定 Checkpoint 的价值。恰恰相反，正因为我们可以明确指出它负责的东西，才不会把它误当成万能的异常处理、数据库事务或文件系统快照。

## 13. 回看：为什么执行循环必然会需要保存点？

回到最开始的手写 `while`。当所有步骤都顺利完成时，简单循环完全足够。我们之所以需要更复杂的实现，只因为遇到了新的、可复现的限制：

```text
可以运行 Agent Loop
       ↓
第一次 pytest 失败后进程退出
       ↓
内存 State 与下一步执行位置丢失
       ↓
最小方案：保存业务状态 + 继续位置
       ↓
真实 Graph 还存在 Channel 版本与并行任务
       ↓
Checkpoint：保存状态与执行进度
       ↓
Saver：负责读取和写入保存记录
       ↓
PregelLoop：在执行步边界保存，恢复时准备任务
       ↓
有任务已经完成而整步未结束
       ↓
Pending writes：记录任务级结果以支持恢复
```

这条演化链里，每一个新机制都有它存在的理由；我们没有因为“框架支持 Checkpoint”，就一开始要求所有 Agent 必须接入数据库。

请用自己的话回答三个问题来检验是否真正理解了本章：

1. 为什么只有 `{"attempts": 1}` 这样的业务字段，不一定足以恢复一个图？
2. 为什么 `graph.invoke(None, config)` 和重新提供一份新的初始输入，不是同一个动作？
3. 为什么 Checkpoint 已经保存成功，也不能保证一个外部文件修改绝对只执行一次？

如果这三题能解释清楚，我们就已经具备继续研究“人工干预执行”的基础。现在的 `interrupt_before=["patch"]` 只是一个固定节点前暂停点；下一章的业务需求会更具体：**程序准备修改退款代码时，必须等待人类阅读修复方案并批准，才能继续。** 这时才有理由引入动态 `interrupt()` 与 `Command(resume=...)`，而不是提前把它们塞进本章。

下一篇：[**第 11 章｜修改真实文件之前，怎样让 Agent 等待人工批准？**](./12how-to-interrupt-and-resume-agent.md)  
建议文件名：`12how-to-interrupt-and-resume-agent.md`。

---

## 源码位置与验证记录

本系列继续固定 **LangGraph `1.2.17` / Commit [`4c52e129eb1ab69a3da58b799f67929f77d81149`](https://github.com/langchain-ai/langgraph/tree/4c52e129eb1ab69a3da58b799f67929f77d81149)**。本文涉及的源码文件：

- [`libs/checkpoint/langgraph/checkpoint/base/__init__.py`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/checkpoint/langgraph/checkpoint/base/__init__.py)：`Checkpoint`、`CheckpointTuple`、`BaseCheckpointSaver`。
- [`libs/checkpoint/langgraph/checkpoint/memory/__init__.py`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/checkpoint/langgraph/checkpoint/memory/__init__.py)：`InMemorySaver.get_tuple()`、`put()`、`put_writes()`。
- [`libs/langgraph/langgraph/graph/state.py`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/graph/state.py)：`StateGraph.compile()` 如何接收 Checkpointer。
- [`libs/langgraph/langgraph/pregel/_loop.py`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/_loop.py)：`PregelLoop.tick()`、`after_tick()`、`put_writes()` 和 Checkpoint 保存及恢复逻辑。

**实验说明：**纯 Python 教学实验已在两个独立 Python 子进程中运行，并真实调用 `pytest`，验证了失败码 `1 → 0`、记录读取与继续位置；LangGraph 公共 API 示例仅做 Python 语法检查，**未在本地的指定框架版本下执行**。文章中的运行轨迹严格区分实测与预期。所有文件改写限制在临时目录，示例不构成执行不可信代码的安全沙箱。
