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

## 机制边界

超时解决“最多等多久”，不是保证“资源绝不泄漏”；取消需要下游操作提供合理清理。与操作系统的信号、线程终止、网络 `RST` 等也不是一回事。特别是**异步任务被取消**与**远端业务真正被取消**是两个不同事件。

文档核对：[asyncio.timeout 与 TaskGroup](https://docs.python.org/3.13/library/asyncio-task.html)。

**下一步的问题**：即使任务都能正常结束，如果客户端每秒送来一万条请求，而服务每秒只能处理一千条，未完成任务仍可能无限增长。我们需要[第 11 章的背压](./11-backpressure.md)。
