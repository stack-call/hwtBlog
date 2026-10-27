---
title: "06｜一次 graph.invoke()，是谁让 model → tools → model 持续运行？"
date: 2026-10-27
slug: /langchain-langgraph-source/how-to-run-pregel-loop
description: "承接 StateGraph.compile，从一次退款测试 Agent 的执行轨迹进入 LangGraph Pregel Runtime，追踪 invoke、stream、PregelLoop.tick、PregelRunner.tick、写入提交与下一轮任务准备。"
tags:
  - LangChain
  - LangGraph
  - Pregel
  - Agent
  - Runtime
  - 源码精读
  - Python
---

> 本文是「LangChain × LangGraph 源码精读」的**第 06 章**，文件名为 `07how-to-run-pregel-loop.md`。建议先阅读[第 05 章：Graph 定义好了，为什么还不能直接执行？](./06how-to-compile-stategraph.md)。
>
> **本章只解决一个核心问题：** `StateGraph.compile()` 已经产生了可运行的图对象；当我们调用 `graph.invoke()` 后，到底是谁决定执行哪个节点、什么时候让它的结果生效，以及下一轮为什么会回到 `model`？

上一章结束时，我们手里有一张已经编译好的图：

```text
START → model ──需要工具──→ tools
          ↑                    │
          └────────────────────┘
          │
          └──不需要工具──→ END
```

`model` 是脚本化假模型：第一次决定运行 `pytest`；看到测试失败后决定修改临时目录里的 `refund.py`；修复后再次运行 `pytest`；最后结束。`tools` 执行这些具体操作。

这张图看起来已经说清楚了“接下来去哪里”。但把上一章的 `graph = builder.compile()` 改成 `result = graph.invoke(input)` 后，出现了一个新问题：**边本身不会调用 Python 函数。谁把图上这条边变成了下一次真实执行？**

如果只是把箭头画出来，我们仍然没有程序。这里才轮到 LangGraph 的 Runtime 出场。

<!-- truncate -->

## 1. 先看一个最小矛盾：节点返回之后，程序凭什么继续？

不使用框架时，我们完全可以这样写：

```python
# 教学化简：只展示单一正常路径，非 LangGraph 源码。
state = {"project_dir": "/tmp/refund-demo"}

while True:
    model_update = model_node(state)
    state.update(model_update)
    if state["action"] == "finish":
        break

    tool_update = tools_node(state)
    state.update(tool_update)
```

这段代码在我们目前的退款任务中能工作。事实上，前面几章已经用类似做法完成了“测—改—再测”。

但注意，**这个 `while` 同时做了三件事**：调用节点、合并结果、决定下一次调用谁。它们恰好挤在了一个函数里，所以看起来不复杂。

现在给它加一个小需求：每一步结束后，我们想先记录“本步做了什么”，再决定下一步；不能在某个节点函数尚未返回时，就让另一个普通节点读到它的中间结果。这样我们就必须分清：

1. **本轮选择了哪些节点**：当前哪些任务可以运行？
2. **本轮节点如何真正执行**：谁调用节点函数、处理完成或失败？
3. **本轮结果什么时候写回**：下一轮节点应该读到哪一个版本的状态？

为了回答第三个问题，需要先给“轮次”一个最小定义：**一次 step 是一批已确定任务的执行与结果提交边界**。在本文的 `model → tools → model` 例子里，每轮只有一个普通业务节点，所以看起来和按顺序调用没区别；当出现多个可同时运行的节点时，这个区别才会变得不可忽略。

这里不需要先记住 Pregel 的所有术语，只要理解刚才三件事不能永远混在一起。接下来我们沿着真实源码找到这三个职责的归属。

## 2. 锁定版本：只研究同步、无检查点的正常路径

为了避免把后续版本的新实现套进旧版本，本系列仍固定以下基线：

| 代码库 | 固定版本与 Commit | 本章关注的真实文件 |
| --- | --- | --- |
| LangGraph | `1.2.17` · `4c52e129eb1ab69a3da58b799f67929f77d81149` | [`pregel/main.py`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/main.py) |
| LangGraph | 同上 | [`pregel/_loop.py`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/_loop.py) |
| LangGraph | 同上 | [`pregel/_runner.py`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/_runner.py) |
| LangGraph | 同上 | [`pregel/_algo.py`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/_algo.py) |
| LangChain | `1.4.7` · `fbb8a35da7023ac0382c3b22f047337861bd3486` | [`agents/factory.py`](https://github.com/langchain-ai/langchain/blob/fbb8a35da7023ac0382c3b22f047337861bd3486/libs/langchain_v1/langchain/agents/factory.py) |

正文中严格区分三类代码：

- **真实源码节选**：从上述固定 Commit 截取的短片段，保持原有语义，给出准确位置。为了让第一次阅读集中在正常路径，不复制整个方法。
- **教学化简**：为了展示“挑选任务—执行—提交”的因果关系自己写的代码，**不是**框架内部实现。
- **公开 API 实验**：可以在安装了相应依赖的环境运行的示例。本文环境是否真的运行，会单独说明。

本章先不研究异步执行、并行分支、Checkpoint、`Command`、复杂 Reducer 或中断恢复。它们确实存在，但不是解释这一次普通调用的必要前置知识。

## 3. 第一站：`invoke()` 不亲自调度每一个节点

上一章已经知道 `CompiledStateGraph` 继承 `Pregel`，因此调用编译产物的 `invoke()` 会进入 LangGraph 的执行入口。

打开 [`pregel/main.py`，`Pregel.invoke()` 约 3704–3819 行](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/main.py#L3704-L3819)。先不要逐个研究参数；只观察它如何获取结果。

**真实源码定位**：默认 `version="v1"` 分支通过 `self.stream(...)` 迭代执行输出，并把最后一次 `values` 输出保存为 `latest`；默认 `stream_mode="values"` 时返回 `latest`。另一条 `version="v2"` 分支也调用 `self.stream(...)`，只是返回结构有所不同。

把这段行为压缩成一张**教学化简**的调用图：

```text
graph.invoke(input)
       │
       ▼
Pregel.invoke()
       │
       └── 遍历 self.stream(...)
                       │
                       ▼
                  持续产生执行结果
                       │
                       ▼
                  汇总最终返回值
```

**不要把上图误解为：`invoke()` 只读取某个已经算好的缓存。** 它会通过消费 `stream()` 驱动同步执行过程。区别在于，`invoke()` 主要把过程包装成“一次输入、一次结果”；实际的调度循环位于更下一层。

这也解释了为什么 `invoke()` 看起来不像我们手写的 `while`：节点执行逻辑没有消失，只是被移到了 `stream()` 使用的 Runtime 中。

> 一个有用的自测问题：如果 `invoke()` 只负责汇总结果，那么控制 `model → tools → model` 反复运行的循环应当在哪里？下一节去找。

## 4. 第二站：`stream()` 建立执行环境，而不是简单打印日志

在 [`pregel/main.py` 的 `Pregel.stream()`（约 2598 行起）](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/main.py#L2598-L2910) 中，沿着同步正常路径可以看到几个动作。

首先，它处理本次调用的配置、输出模式和运行上下文。随后它创建两个关键对象：

- [`SyncPregelLoop`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/main.py#L2823-L2844)：负责维护本次图执行的位置、待执行任务，以及步与步之间的状态推进。
- [`PregelRunner`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/main.py#L2846-L2855)：负责将已经选中的任务真正运行起来，并报告执行结果。

为什么分成两个对象？先想一想我们的退款场景。

当 `model` 返回“现在运行测试”时，有两个不同的问题：

- **应该运行 `tools` 吗？** 要依赖图的边、条件路由和当前触发关系。
- **既然选中了 `tools`，怎么运行它？** 要拿到可执行任务，调用实际节点函数，并处理成功或异常。

让同一层既负责“这次谁有资格运行”，又负责“怎么执行所有任务”，会让控制流和执行细节紧紧耦合。源码把这两个职责分别安放在 Loop 与 Runner 中。

现在再看同一文件 [`main.py:2881–2906`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/main.py#L2881-L2906)。这里终于出现真正的主循环。

**真实源码节选（固定 Commit，删去外围配置与输出处理）**：

```python
while loop.tick():
    for task in loop.match_cached_writes():
        loop.put_writes(task.id, task.writes, cached=True)
```

紧接着，源码把尚未具有已完成写入的任务交给 `runner.tick(...)`；Runner 执行期间，`stream()` 可以输出结果；本轮执行完后调用 `loop.after_tick()`。

因此，把外围选项先放下，这条控制链可以用下面的**教学化简**表示：

```python
# 教学化简：不是框架源码，省略缓存、回调、异常及流式输出。
while loop.tick():                # 选出本轮任务
    runner.execute(loop.tasks)   # 执行本轮任务
    loop.after_tick()             # 合并本轮更新
```

注意：源码实际调用的是 `runner.tick(...)`，而不是 `runner.execute(...)`。这里故意用了不同名字，避免伪代码冒充源码。

我们终于找到了对应手写 `while` 的运行机制，但还没有回答“下一轮为什么选中 `tools`”。这要继续进入 `tick()`。

## 5. 第三站：`PregelLoop.tick()` 怎样确定本轮轮到谁？

假设退款任务刚开始，我们没有任何工具反馈。`model` 应当成为第一批业务任务。运行完它以后，`tools` 不应因为自己存在于图中就自动执行；只有当 `model` 的结果选择了那条路径，`tools` 才会被触发。

打开 [`pregel/_loop.py` 的 `PregelLoop.tick()`，约 626–704 行](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/_loop.py#L626-L704)。这一步最值得看的是：

**真实源码节选**：

```python
self.tasks = prepare_next_tasks(
    self.checkpoint,
    self.checkpoint_pending_writes,
```

这个调用还会接收节点定义、Channel、配置、当前 step、触发索引等参数。它不是在此处执行 `model_node()`，而是准备本轮的任务对象。

向下跟到 [`pregel/_algo.py` 的 `prepare_next_tasks()`，约 367–480 行](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/_algo.py#L367-L480)，会看到它综合已有触发信息，选出本轮需要创建的任务。这里有个很能解释设计意图的细节：代码可以利用“上一轮更新了哪些 Channel、哪些节点监听这些触发”来缩小候选范围；它不是无条件把图中所有节点执行一遍。

对于我们的例子，正常路径可以概括为：

```text
初始化输入
   │
   ▼
发现 START 对应的入口 → 准备 model 任务
   │
   ▼
model 任务完成，路由写出“去 tools”所需的触发
   │
   ▼
下一轮准备 tools 任务
   │
   ▼
tools 完成，固定边提供返回 model 的触发
   │
   ▼
下一轮准备 model 任务
```

这里的“发现”不是一个额外的 LLM 判断。**Runtime 只是执行图中已经定义的路由规则**。对于条件边，路由函数是在节点运行时的对应写入链上被计算的；Runtime 再根据它产生的触发安排下一轮任务。不要把“本轮路由判断”和“下一轮目标节点实际执行”混成一件事。

再看 [`_loop.py:675–678`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/_loop.py#L675-L678)。当本轮没有任务可执行时，Loop 把状态标为 `done`，并返回 `False`。外层的 `while loop.tick()` 因此退出。

因此，**结束并不是有个特殊的 END 函数被调用了**。在这条普通路径上，条件分支决定不再安排后续业务节点，最终下一步无任务，执行循环自然结束。

## 6. 第四站：`PregelRunner.tick()` 怎样真的调用节点？

到这里，我们已经知道本轮应该执行 `tools`。但 `tools` 任务在队列里，仍然只是一个待执行对象；pytest 还没有运行。

现在进入 [`pregel/_runner.py` 的 `PregelRunner.tick()`，约 164 行起](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/_runner.py#L164-L238)。

在单任务、没有额外超时或等待器的正常分支中，Runner 会调用 `run_with_retry(t, retry_policy, ...)` 执行任务，然后通过 `self.commit(t, None)` 登记成功结果；遇到异常则进入相应异常处理路径。

我们现在不研究重试策略的各种分支，只看为什么 Runner 必须独立存在。

`model` 与 `tools` 有不同的业务逻辑：前者返回下一步要做什么，后者真正启动子进程。Runtime 不可能把所有业务逻辑硬编码进去。但它也不应该把任务执行失败、执行超时、任务完成通知等责任交给每个业务节点自己重复实现。

所以 Runner 接收的是编译阶段准备好的**可执行任务**，调用任务绑定的可运行对象；对普通节点来说，最终会进入你注册的 Python 节点函数。

把我们的例子接回去：

```text
Loop 选中 tools 任务
          │
          ▼
Runner 调用该任务
          │
          ▼
执行我们注册的 tools_node(state)
          │
          ▼
subprocess.run([...pytest...])
          │
          ▼
tools_node 返回 {"test_exit_code": 1, ...}
```

有两件事情需要特别分开：

第一，`pytest` 返回退出码 1 不等于 LangGraph 的 Runner 抛了异常。我们的节点函数把非零退出码**正常返回为状态数据**时，Runtime 仍把它当作一次成功完成的节点调用；后续由 `model` 决定测试失败意味着什么。

第二，工具是否能访问文件、是否有执行权限，并不由 Graph 自动保证。我们只在自己创建的临时目录里运行固定命令；真实 Agent 的路径限制、命令授权和沙箱隔离是另外一层安全设计，不能因引入 Runner 就假定已经解决。

## 7. 第五站：节点返回结果，为什么不会立即递归调用下一节点？

现在轮到最容易误读的一段：`tools_node()` 返回了 `{"test_exit_code": 1}`，然后发生什么？

直觉上，我们可能想在工具函数末尾直接写：

```python
# 错误的架构直觉：把路由/状态推进又塞回业务节点。
def tools_node(state):
    result = run_pytest(state)
    return model_node({**state, **result})
```

这对只有一个路径的小练习也许能跑，但它破坏了上一章好不容易建立的节点边界：工具节点不再只负责自己的工作，还擅自承担下一轮调度；它也绕过了 Graph 原本定义的路由与状态更新机制。

真实 LangGraph 采用不同的方式。

### 7.1 Runner 先登记任务写入

在 [`pregel/_runner.py` 的 `PregelRunner.commit()`，约 560–600 行](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/_runner.py#L560-L600)，正常完成的分支会调用 `put_writes` 登记任务产生的写入。

**真实源码节选（成功路径末尾）**：

```python
self.put_writes()(task.id, task.writes)  # type: ignore[misc]
```

这一步值得用一句话记住：**任务结束，先交回“它写了什么”；不要让任务自己递归调用后继节点。**

但到这儿也不能直接说“整个图状态已经完成提交”。对普通同步步进来说，收集任务写入与把它们应用到共享 Channel，是两个不同阶段。

### 7.2 Loop 在步边界应用写入

回到 [`pregel/_loop.py` 的 `PregelLoop.after_tick()`，约 705–779 行](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/_loop.py#L705-L779)。

这里先汇总本轮任务的写入，然后通过 `apply_writes(...)` 更新 Channel，并保存本轮究竟更新了哪些 Channel。

**真实源码节选（调用起始处）**：

```python
self.updated_channels = apply_writes(
    self.checkpoint,
    self.channels,
    self.tasks.values(),
```

再进入 [`pregel/_algo.py` 的 `apply_writes()`，约 222–323 行](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/_algo.py#L222-L323)：它把任务的写入按 Channel 汇集，调用各 Channel 的更新方法，形成后续任务能够观察到的新值，并跟踪哪些 Channel 已发生更新。

这就是本章真正想解释的**步边界**：

```text
第 N 步                                    第 N+1 步

已选中 tools 任务                          根据触发准备 model 任务
       │                                           │
       ▼                                           ▼
执行 tools_node                              model_node 读取新状态
       │                                           │
       ▼                                           ▼
得到测试失败的结果                          决定执行 apply_fix
       │
       ▼
Runner 记录本任务的写入
       │
       ▼
Loop 在本步结束时应用写入
       │
       └──────────────────────────────────────────►
```

这一过程实现了同步执行语义中非常重要的一点：**第 N 步的普通 Channel 更新，在第 N+1 步才成为其他普通节点下一轮所读取的结果。** 如果以后出现同一步内多个节点，就更不能简单理解成“谁先在 Python 中返回，另一个节点就立刻读到它”。

为了准确起见，还有两个限制要说明。

首先，条件分支在前驱节点运行时可以基于它的本轮输出选择路由；这不等于后继节点已经运行。源码在 `local_read(..., fresh=True)` 等位置提供了用于路由读值的特殊逻辑，不能把这条正常路径的描述误扩展成“所有读操作一律必须等待下一步”。

其次，这里说的“提交”指**将本轮更新应用到运行中的状态 Channel**，并不自动等于把副作用写入事务日志，更不等于外部 `subprocess`、文件系统与数据库操作具备精确一次保证。Checkpoint 和恢复语义属于后续章节。

## 8. 第六站：为什么再次回到 `model`，而不是无限运行所有节点？

现在把执行链完整地放回退款任务中。

- 第一次 `model` 运行：输入只有任务要求，返回 `action="run_tests"`。
- 第一次 `tools` 运行：真实 `pytest` 得到非零退出码，返回 `test_exit_code=1`。
- 第二次 `model` 运行：读到上一步的测试结果，返回 `action="apply_fix"`。
- 第二次 `tools` 运行：仅修改临时目录中的 `refund.py`。
- 第三次 `model` 运行：读到 `apply_fix` 已完成，要求再次运行 `pytest`。
- 第三次 `tools` 运行：pytest 返回退出码 0。
- 第四次 `model` 运行：读到成功结果，返回 `action="finish"`，路由到 `END`。

每一段的“为什么轮到下一个节点”，都来自同样的机制：上一步节点及其路由写出触发 → 步边界应用写入 → 下一轮准备满足触发条件的任务。

在第七次业务节点调用后，条件分支走向 `END`；没有新的业务任务被安排，Loop 下一次准备任务时发现无任务，结束。**我们看到的是七次业务节点调用，不应直接宣称底层恰好执行了七个 Pregel step**：运行时还有输入装载等内部步骤。后面示例会验证的是业务节点序列，不是未经测量的内部 step 数。

### 为什么需要一个停止条件？

把最后一个分支改成“始终继续”就会出现另一个具体问题：工具反复被调用，直到碰到系统的执行上限。`pregel/main.py` 在循环后对 `out_of_steps` 状态有处理，会抛出 `GraphRecursionError`。相关代码见 [`main.py:2923–2933`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/main.py#L2923-L2933)。

这件事教给我们一个边界：**Runtime 能执行和限制循环，但不会自动判断“退款业务已经真的正确”。** 在本文中，结束条件来自脚本化假模型对真实测试退出码的判断；测试覆盖以外的业务正确性仍然要靠额外验证。

## 9. 已实际运行的最小实验：在无框架环境验证“执行—提交—下一轮”

现在先别急着安装 LangGraph。为理解这条正常路径，我们可以只用 Python 做一个**教学化简**，把三个动作清楚地分开：

```python
# 教学化简：单活跃节点，不能模拟真实 Channel、调度并发和 Checkpoint。
snapshot = state.copy()
partial_update = selected_node(snapshot)  # 执行节点
state.update(partial_update)               # 在此示例的步边界应用写入
selected_node = route_from_new_state(state)  # 为下一轮选节点
```

这里的 `snapshot` 表示普通节点拿到本轮输入。`partial_update` 表示节点只负责返回它产生的更新，而不是通过调用别的节点来推进图。真实框架的 Channel 规则、内部任务对象和写入结构远比这个三行示例复杂，不能把 `dict.update()` 当作 `apply_writes()` 的等价实现。

为了让这个验证有实际价值，我用与上一章一致的退款测试材料运行了一个完整单节点步进实验。核心步骤如下：

1. 创建独立临时目录，放入有浮点精度问题的 `refund.py` 与 `test_refund.py`。
2. 脚本化 `model` 首次选择运行测试。
3. `tools` 真正启动 `python -m pytest`；观察到退出码 1。
4. 脚本决定将退款计算替换为基于 `Decimal` 的写法。
5. 再次执行真实 `pytest`；观察到退出码 0。
6. 最后由模型脚本选择 `finish`，模拟 Runtime 不再安排后继任务。

**本次环境实际输出**（已在 Python 3.13 环境调用 pytest 执行）：

```text
step=1 node=model delta={'action': 'run_tests'}
step=2 node=tools delta={'last_tool': 'run_tests', 'test_exit_code': 1}
step=3 node=model delta={'action': 'apply_fix'}
step=4 node=tools delta={'last_tool': 'apply_fix'}
step=5 node=model delta={'action': 'run_tests'}
step=6 node=tools delta={'last_tool': 'run_tests', 'test_exit_code': 0}
step=7 node=model delta={'action': 'finish', 'summary': '测试通过（脚本判断）'}
checked: 7 node steps, pytest exit codes [1, 0], END reached
```

**验证的边界**：真实执行的是 `pytest` 与教学版的循环，不是 `PregelRunner`。这个结果能证明我们期待的业务节点序列与测试结果可复现，不能单独证明 LangGraph 1.2.17 在当前环境执行出了相同输出。下面再用公开 API 写同一条链路，并明确标注执行状态。

## 10. 再用 LangGraph 的公开 API 观察相同的业务节点序列

上一章已经写过一个 `StateGraph` 的退款测试。现在不是重复讲如何添加边，而是给它增加**每步可观察性**：不用一次只看最后的 `graph.invoke()` 结果，而用 `graph.stream(..., stream_mode="updates")` 查看每个节点返回的更新。

在固定源码里，`stream()` 对 `updates` 的说明是“按节点/任务提供更新”。相关参数与说明见 [`pregel/main.py:2615–2633`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/main.py#L2615-L2633)。**这里的 `stream()` 是公开观察接口，内部 Runtime 仍按上面讲过的方式运行**。

将下面代码保存为 `demo_pregel_runtime.py`：

```python
# 公开 API 实验：LangGraph 1.2.17；不需要模型 API Key。
# 本文环境未安装固定版本，以下代码经过语法核对但未完成框架执行。
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal, TypedDict

from langgraph.graph import START, END, StateGraph

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
    summary: str


# 脚本化假模型：只提供预设决策，不证明 LLM 的智能。
def model_node(state: FlowState) -> dict:
    if "last_tool" not in state:
        return {"action": "run_tests"}
    if state["last_tool"] == "apply_fix":
        return {"action": "run_tests"}
    if state["last_tool"] == "run_tests" and state["test_exit_code"] != 0:
        return {"action": "apply_fix"}
    if state["last_tool"] == "run_tests" and state["test_exit_code"] == 0:
        return {"action": "finish", "summary": "脚本判断：测试通过"}
    raise AssertionError("不在教学正常路径中的工具反馈")


def tools_node(state: FlowState) -> dict:
    root = Path(state["project_dir"])
    if state["action"] == "apply_fix":
        # 只修改我们创建的临时目录；不开放任意文件写入能力。
        (root / "refund.py").write_text(FIXED, encoding="utf-8")
        return {"last_tool": "apply_fix"}

    if state["action"] != "run_tests":
        raise ValueError(f"未授权动作: {state['action']}")

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_refund.py"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    # pytest 返回 1 是业务反馈，而不是节点运行时抛出异常。
    return {"last_tool": "run_tests", "test_exit_code": result.returncode}


def route(state: FlowState) -> Literal["tools", "finish"]:
    return "finish" if state["action"] == "finish" else "tools"


def build_graph():
    builder = StateGraph(FlowState)
    builder.add_node("model", model_node)
    builder.add_node("tools", tools_node)
    builder.add_edge(START, "model")
    builder.add_conditional_edges(
        "model", route, {"tools": "tools", "finish": END}
    )
    builder.add_edge("tools", "model")
    return builder.compile()


def main():
    with TemporaryDirectory() as temp:
        root = Path(temp)
        (root / "refund.py").write_text(BROKEN, encoding="utf-8")
        (root / "test_refund.py").write_text(TEST, encoding="utf-8")

        graph = build_graph()
        observed = []
        for chunk in graph.stream(
            {"project_dir": str(root)}, stream_mode="updates"
        ):
            # 默认 v1 的 updates 模式按节点名返回本轮节点更新。
            print(chunk)
            for node_name, update in chunk.items():
                if node_name in {"model", "tools"}:
                    observed.append((node_name, update))

        expected = ["model", "tools", "model", "tools", "model", "tools", "model"]
        assert [name for name, _ in observed] == expected
        assert [update["test_exit_code"] for name, update in observed
                if name == "tools" and "test_exit_code" in update] == [1, 0]
        assert observed[-1][1]["action"] == "finish"
        assert (root / "refund.py").read_text(encoding="utf-8") == FIXED
        print("验证完成：业务节点顺序、两次 pytest 结果及修复内容一致")


if __name__ == "__main__":
    main()
```

建议在独立虚拟环境中运行：

```bash
python -m venv .venv
# macOS / Linux:
source .venv/bin/activate
# Windows PowerShell：.venv\Scripts\Activate.ps1
python -m pip install "langgraph==1.2.17" "pytest>=8,<10"
python demo_pregel_runtime.py
```

**预期轨迹（不是本次环境的框架实测输出）**应当依次出现七个业务节点更新，分别对应：要求测试、测试失败、要求修复、修复完成、要求再测、测试通过、选择结束。内部任务、初始输入装载与 Stream 格式参数可能影响观察到的其它元信息，因此不硬写未经验证的完整控制台日志。

为什么这段示例故意不调用一次真实模型？因为我们研究的是 Runtime 的**调度能力**，而不是模型是否能正确诊断退款 Bug。固定脚本可以避免不同模型给出不同答案，让同一条执行链更容易被审查。等这条链理顺，再把 `model_node` 换成真正的模型调用，不会改变本章要解释的 Runner/Loop 职责边界。

## 11. 回到 `create_agent()`：它交给 Runtime 的到底是什么？

前几章可以被串成一条真实分工链：

```text
LangChain create_agent()
    │
    │ 构造模型节点、工具节点、条件边
    ▼
LangGraph StateGraph
    │
    │ compile() 装配节点及触发关系
    ▼
CompiledStateGraph（继承 Pregel）
    │
    │ invoke() 消费 stream()
    ▼
Pregel Runtime
    │
    ├── Loop.tick()        准备本轮可执行任务
    │
    ├── Runner.tick()      执行被选中的节点
    │
    ├── Runner.commit()    登记任务写入
    │
    └── Loop.after_tick()  应用更新，供下一轮读取
    │
    └── 继续 tick() 或结束
```

这张图现在才适合出现，因为每一层的“为什么存在”都有了对应问题：

- `create_agent()` 不需要亲自保存一个巨大 `while`；它要把 Agent 所需的节点和路由规则组装起来。
- `StateGraph` 负责描述，不应因用户还没调用 Agent 就执行工具。
- `CompiledStateGraph` 提供执行入口，Runtime 根据编译后的触发关系推进任务。
- Loop 与 Runner 分别处理“这轮轮到谁”和“被选中的任务如何运行”。
- 节点更新在步边界应用，使下一个普通节点基于适当的状态版本工作。

需要再次强调的是：这是一张**正常路径的职责图**，不是对 LangGraph 全部内部接口、任务缓存、检查点、嵌套图、并发或错误恢复的完整枚举。它也不能被用来宣称框架一定会让模型推理得更好。

## 12. 本章只解决了什么，还没解决什么？

到目前为止，我们已经可以回答开头的问题。

`graph.invoke()` 能持续完成 `model → tools → model`，不是因为 `StateGraph` 的边会自己执行，也不是因为 `ToolNode` 会主动递归调用模型。真实执行由 Pregel Runtime 驱动：Loop 先准备满足触发条件的任务，Runner 执行任务并登记写入，Loop 在步边界应用更新，然后根据新的触发关系准备下一步。没有后继任务时，执行结束。

这个机制解决了**可调度、可继续、可终止**的基本执行问题；它没有替我们解决以下问题：

- 多个节点同时写同一个状态字段时，应当合并、拒绝，还是保留某一种更新？
- 当两个节点同一步都读取状态时，它们分别能看到哪些值？
- 执行期间进程崩溃、需要人工批准、外部文件已经改写时，如何恢复并保证业务安全？

这些问题非常真实，但不是必须在今天讲完。特别是“多个节点一起写”已经不能简单地通过示例里的 `dict.update()` 处理，它正好会推动我们进入后续的 **State 与 Channel**，再由真实的冲突场景引出 Reducer。

在这之前，下一章先做一件更窄的事：**同样的 `model → tools` 节点，为什么在 Runtime 中能收到正确的状态输入？编译后的节点到底是怎样从 Channel 读取数据并形成写入的？** 我们会把本章刚刚看见的 `apply_writes()` 与 `StateGraph` 中用户写的 `TypedDict` 接起来，而不是突然罗列所有 Channel 类。

---

## 附录 A：源码追踪清单（固定 Commit）

以下链接按本文的因果链顺序排列，建议真正打开代码时也按这个顺序看：

| 源码位置 | 此刻只需要验证什么 |
| --- | --- |
| [`Pregel.invoke()`，`main.py:3704–3819`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/main.py#L3704-L3819) | 为什么 `invoke()` 会消费 `stream()` 输出 |
| [`Pregel.stream()`，`main.py:2598–2910`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/main.py#L2598-L2910) | Loop 和 Runner 在哪里创建，外层调度循环在哪里 |
| [`PregelLoop.tick()`，`_loop.py:626–704`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/_loop.py#L626-L704) | 为什么这一步只选择任务，而不直接在此执行节点 |
| [`prepare_next_tasks()`，`_algo.py:367–480`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/_algo.py#L367-L480) | 被触发的节点怎样变为本轮任务 |
| [`PregelRunner.tick()`，`_runner.py:164–238`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/_runner.py#L164-L238) | 谁真正调用任务以及处理正常完成或异常 |
| [`PregelRunner.commit()`，`_runner.py:560–600`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/_runner.py#L560-L600) | 节点完成后的写入如何登记 |
| [`PregelLoop.after_tick()`，`_loop.py:705–779`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/_loop.py#L705-L779) | 写入在什么时候应用，下轮怎样获知哪些 Channel 更新了 |
| [`apply_writes()`，`_algo.py:222–323`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/_algo.py#L222-L323) | 数据写入为什么要经过 Channel 的更新方法 |
| [`GraphRecursionError` 路径，`main.py:2923–2933`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/langgraph/langgraph/pregel/main.py#L2923-L2933) | 如果执行始终没有停止条件会怎样 |

## 附录 B：教学实验与复现声明

本章中 **“七次节点运行，两次 pytest 退出码分别为 1 和 0”** 来自真实执行的纯 Python 教学实验。执行程序使用 `TemporaryDirectory()`，pytest 运行的是本实验生成的 `test_refund.py`，`refund.py` 只在该临时目录内修改。示例中固定写好的“修复动作”不是模型自行发现的；它只验证执行路径与反馈机制。

本次工作环境没有安装 `langgraph==1.2.17`，所以 `demo_pregel_runtime.py` 是按已核对源码和公开接口编写、并做 Python 语法检查的**待本地运行示例**。**不能把它的预期输出冒充实测**。如果未来固定版本中存在不同的依赖解析或接口行为，应以实际执行结果为准，记录环境并修正文档。

本章学习完成的标准并不是能背出 `PregelRunner` 的所有方法，而是能够不用猜测地复述：

> 一次 `invoke()` 会进入运行循环；Loop 为这一轮选择任务，Runner 执行它们，任务写入在步边界应用，下一轮再依据更新后的触发关系继续。只有当新的业务节点真的被选中时，才会执行对应的 Python 函数。

下一篇：**第 07 章｜节点读到的 State 从哪里来？——从 `TypedDict` 走向 Channel 的读写边界**（计划文件名：`08how-to-read-write-state.md`）。
