---
title: "异步与高并发 10：请求超时或客户端断开，未完成的任务怎么办？"
sidebar_label: "十、任务超时或断开之后怎么办"
sidebar_position: 11
description: "任务取消、超时、清理与结构化并发的必要性。"
slug: /async-concurrency/10-cancellation
---

# 请求超时或客户端断开，未完成的任务怎么办？

前章的 `fetch()` 在上游正常返回时表现很好。然而一旦上游迟迟不回复，客户端可能已经放弃请求，而服务端的任务仍然等待。原来的方案能**发起和恢复**任务，却还没定义任务**何时应该结束**。

先处理最小需求：给等待上游的操作一个时间上限。

## 在真正出现超时之前，不必发明“任务生命周期管理”

前一章的两次上游查询很顺利：开始任务、等待结果、返回。一个简单的 `await fetch()` 已经足够。但假设 A 在 100ms 内回应，B 因网络故障一直没有回复；客户端却规定 500ms 内必须给出结果。此时我们面临一个非常具体的选择：让请求无限挂着，还是在截止时间之后放弃它？

最小方案是在调用方设置一个期限。与其口头说“支持超时”，我们先写出可观察的行为：

```python title="实验 10｜超时如何传播到 finally"
import asyncio

async def upstream():
    try:
        print("upstream started")
        await asyncio.sleep(2)
        return 42
    finally:
        print("upstream cleanup")

async def main():
    try:
        async with asyncio.timeout(0.1):
            await upstream()
    except TimeoutError:
        print("deadline exceeded")

asyncio.run(main())
```

常见输出先是 started，随后 cleanup，最后 deadline exceeded。**`finally` 运行这一点比精确计时更重要**：我们让任务终止等待，并给它执行清理逻辑的机会。并不是在 0.1 秒处强行销毁 OS 线程；它主要依赖任务在可取消的暂停点响应取消。

## 用超时表达“不再等待”

```python title="实验 10A｜超时后进入清理路径"
import asyncio

async def query_upstream():
    try:
        print("upstream: begin")
        await asyncio.sleep(10)       # 模拟长时间等待
        return "response"
    finally:
        print("upstream: cleanup")

async def main():
    try:
        async with asyncio.timeout(0.1):
            print(await query_upstream())
    except TimeoutError:
        print("request: timed out")

asyncio.run(main())
```

通常输出 `begin → cleanup → timed out`。这不是因为睡眠后台“自然完成”，而是超时机制取消当前任务中未完成的等待，并让协程有机会运行 `finally`。

## 一次取消请求究竟能保证什么

假设上游正在 `await asyncio.sleep(2)`。任务收到取消后，通常可以在挂起点观察到 `CancelledError`，然后沿异常退出路径运行 `finally`。但是如果任务改成：

```python title="教学片段：会挡住事件循环的同步 CPU 循环"
while expensive_calculation_not_done():
    do_one_cpu_step()
```

这段是伪代码，没有定义两个业务函数。其重点是：**没有把控制权交还给事件循环的协程，无法靠协作调度及时处理取消**。如果任务把工作交给了线程池或远端数据库，即使等待它的协程被取消，也不等于那个底层工作一定停止，更不等于外部副作用可以自动撤销。

这迫使我们区分两个概念：① 调用方决定不再等待；② 被调用的实际操作已经停止且资源已清理。前者可以较容易表达，后者需要具体 API 的取消能力、协作点、异常处理和资源释放共同保证。

为什么结构化并发会有价值？一条客户端请求触发 A、B 两个子任务。如果 B 失败而 A 继续运行，没有哪个外层对象负责 A，就容易出现“客户端早就离开，但上游请求还在跑”的孤儿任务。`TaskGroup` 把这些子任务放进明确的生命周期范围，必要时协调退出与异常传播。它不是“自动修复业务错误”，而是让责任边界更加明确。

## 为什么取消不是“强行杀死任意代码”？

Python `asyncio` 的取消是一种**协作机制**：任务收到取消请求后，`CancelledError` 会在适当的挂起点被抛给协程。协程可在 `finally` 里关闭 socket、释放租约、记录状态，然后通常应继续让取消向上传播。

```python title="实验 10B｜观察一个任务的取消"
import asyncio

async def worker():
    try:
        print("worker starts")
        await asyncio.sleep(10)
    finally:
        print("worker cleanup")

async def main():
    task = asyncio.create_task(worker())
    await asyncio.sleep(0)   # 给 worker 先运行到等待位置的机会
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        print("caller observed cancellation")

asyncio.run(main())
```

**取消 Python Task 不意味着外部副作用一定被撤销**。如果上游已经收到写入请求，或工作被提交给了不可中断的底层线程，Task 取消并不会自动回滚这些动作。外部系统需要自己提供超时、幂等、补偿或取消协议。

## 把故障从一个任务扩展到整个请求

想象下面的时间线：

```text
T0  客户端开始一个聚合查询
T1  创建上游 A、B 子任务
T2  A 完成，B 继续等待
T3  客户端超时离开
T4  若没有取消与清理，B 仍可能继续占用连接
```

如果每天有大量请求在 T3 超时，T4 的残留工作会变成下一章的积压来源。现在我们才意识到：**即使事件循环处理等待很高效，也不能让未完成任务无限增长。** 取消解决“已经不再需要的工作能否退出”；背压要解决“本来就不该无限进入系统的工作如何被限制”。它们作用的时间点不同，必须分开设计。

## 从孤立任务到“谁负责它”的关系

假设一个客户端请求同时发起三个上游查询。请求失败后，如果其他子任务还游离在后台，程序就会积累不受管理的任务。我们需要明确父任务与子任务的生命周期关系。

Python 3.11+ 的 `asyncio.TaskGroup` 支持把一组任务放到同一个生命周期范围里：

```python title="实验 10C｜用 TaskGroup 管理任务范围"
import asyncio

async def fetch(name, delay):
    await asyncio.sleep(delay)
    return name

async def main():
    async with asyncio.TaskGroup() as group:
        a = group.create_task(fetch("A", 0.05))
        b = group.create_task(fetch("B", 0.08))
    # 退出 async with 时，所属任务均已结束；若有失败，将按规则传播
    print(a.result(), b.result())

asyncio.run(main())
```

这属于**结构化并发**思想的一种表现：任务不只是随处创建的对象，它们与创建它们的作用域建立明确关系。Python 的 `TaskGroup` 在一个子任务发生普通错误时，会依特定规则取消同组其他未完成任务并报告错误；异常组等细节可留给更深入的运行时专题。

## 取消之后真的清理了吗？用“正在等待另一个任务”验证

请求服务常常同时启动上游 A 和 B。设想 A 很快报错，B 还在等待：若我们直接用 `create_task` 丢出 B，却从未收集它的结果，B 就可能成为生命周期失控的后台任务。这不是事件循环本身的错误，而是“哪个父请求拥有哪个子任务”未被表达。

可以把关系用一份最小实验观察：

```python title="实验 10D｜TaskGroup 中兄弟任务的错误传播与清理"
import asyncio

async def fail_soon():
    await asyncio.sleep(0.03)
    raise RuntimeError("upstream A failed")

async def slow():
    try:
        await asyncio.sleep(2)
        print("slow unexpectedly completed")
    finally:
        print("slow cleaned up")

async def main():
    try:
        async with asyncio.TaskGroup() as group:
            group.create_task(fail_soon())
            group.create_task(slow())
    except* RuntimeError as errors:
        print("group failed:", len(errors.exceptions))

asyncio.run(main())
```

常见结果是打印 `slow cleaned up`，再汇报 group failed。这说明同一组子任务的失败会触发对其他相关子任务的取消与等待收尾。但是 `finally` 不能魔法般撤销 `slow` 先前已完成的任何外部写入；如果它在远端数据库提交了事务，事务是否能回滚必须由具体数据库和应用协议保证。

还有一个常见误判：父请求“停止等待”并不一定会告诉服务端“客户端已经断开”。有些框架会给你取消通知，有些需要监听连接错误或读事件。真实服务必须把**本地 Task 生命周期**与**外部连接状态**明确连起来，不能从一个 `asyncio.timeout` 例子就推断整条请求链完全可靠。

这章不是为了补一份 `TaskGroup` API 列表，而是要用失败场景把“任务是谁的”这个设计问题写清楚。接下来即便所有任务都正常结束，如果入口创建任务的速度远大于它们结束的速度，系统仍可能因为积压失控。

## 机制边界

超时解决“最多等多久”，不是保证“资源绝不泄漏”；取消需要下游操作提供合理清理。与操作系统的信号、线程终止、网络 `RST` 等也不是一回事。特别是**异步任务被取消**与**远端业务真正被取消**是两个不同事件。

文档核对：[asyncio.timeout 与 TaskGroup](https://docs.python.org/3.13/library/asyncio-task.html)。

## 为什么取消必须和资源关闭一起讲

当客户端不再等待结果，查询任务可能还持有 TCP 连接。如果你只在应用层把任务标记为“失败”，却没有执行 socket 清理，文件描述符会一直占用资源，最终可能导致新连接建立失败。因此取消的完整路径不只是“发出取消信号”，还包括让控制流走到关闭代码。

这也是我们使用 `try/finally` 的理由：

```python title="概念片段：清理不应依赖正常路径独占"
async def query(reader, writer):
    try:
        result = await reader.readline()
        return result
    finally:
        writer.close()
        await writer.wait_closed()
```

这段假设 `writer` 是 asyncio StreamWriter，运行时需要允许正常清理；在断连、异常连接或某些取消场景下，`wait_closed` 本身也可能出现异常或等待，真实工程通常还要处理这些边界。关键是：**清理动作必须写在任何结果路径都有机会经过的位置**，而不是只在 `return` 前写一次。

另一个典型错误是吞掉 `CancelledError` 后继续运行，却没有向调用方解释为何不能取消。在协作式取消模型中，任务可以捕获取消异常进行必要清理，但随意压制它可能破坏父任务的超时与结构化并发语义。某些操作确实必须完成原子收尾，但那就要显式说明保证范围，而不是悄悄无视取消。

最后，把“取消”和“背压”区分开。取消通常处理的是已经被接纳、现在却不再需要的工作；背压更早地决定新的工作是否应该进入。前者不能替代后者：当每秒 1,000 个请求持续进入、只有 100 个能够完成时，即使每个请求最终都会成功，也照样可能排队失控。

## 本章的能力边界

`asyncio.timeout` 约束当前任务中的等待范围；客户端断开连接并不必然自动取消所有上游任务，你需要根据所用服务框架如何报告断开和传播取消来实现处理。`TaskGroup` 提供子任务的生命周期组织，但不等于所有外部操作具备可撤销能力。

把“超时 → 取消请求 → 子任务清理”观察清楚后，下一章才讨论另一种失败：即使没有一个请求超时，单位时间涌入的请求比系统能处理的多，也会把程序慢慢撑满。
