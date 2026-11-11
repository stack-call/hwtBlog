---
title: "异步与高并发 11：请求进入得比处理得快，为什么内存越来越大？"
sidebar_label: "十一、进来的比处理得快会怎样"
sidebar_position: 12
description: "从任务积压推导有界队列、并发限制和背压。"
slug: /async-concurrency/11-backpressure
---

# 请求进入得比处理得快，为什么内存越来越大？

我们已经能在一个事件循环中管理大量请求，并能处理超时与取消。但是，**异步任务轻量并不意味着它们没有成本**。每个排队任务、缓冲区和 socket 都会消耗资源。

如果上游每秒只能处理 100 个请求，而客户端每秒提交 1,000 个，我们不应只问“能否创建更多 Task”，还应问：**谁告诉上游的请求生产者先慢一点？**

## 一个看似正确但不充分的方案

```python title="教学片段：限制同时执行，不限制任务总数"
import asyncio

limit = asyncio.Semaphore(5)

async def fetch(i):
    async with limit:
        await asyncio.sleep(0.1)
        return i

async def main():
    tasks = [asyncio.create_task(fetch(i)) for i in range(100_000)]
    await asyncio.gather(*tasks)

# 本片段不默认执行：它会创建大量排队 Task，目的正是暴露限制。
```

`Semaphore(5)` 限制了同时进入关键区域的任务数，但列表推导式已经**一次性创建了十万个 Task**。它没有对“任务创建速度”施加背压。于是我们要在提交端也加入限制。

## 新机制：有界队列

把上游请求先送进一个容量有限的队列。队列满了时，生产者的 `await queue.put(...)` 会等待，**暂停生产**而不是无穷创建新任务。

```python title="实验 11｜有界队列 + 固定数量的消费者"
import asyncio
import time

async def worker(name, queue):
    while True:
        item = await queue.get()
        try:
            if item is None:
                print(name, "stopping")
                return
            await asyncio.sleep(0.05)   # 模拟上游请求
            print(name, "handled", item)
        finally:
            queue.task_done()

async def produce(queue, count):
    for i in range(count):
        await queue.put(i)            # 满队列时，生产者在此暂停
        print("submitted", i, "queued", queue.qsize())

async def main():
    queue = asyncio.Queue(maxsize=3)
    workers = [asyncio.create_task(worker(str(i), queue)) for i in range(2)]
    start = time.monotonic()
    await produce(queue, 10)
    await queue.join()                 # 等待所有业务项被确认处理
    for _ in workers:
        await queue.put(None)         # 每个消费者一条退出哨兵
    await asyncio.gather(*workers)
    print("elapsed:", round(time.monotonic() - start, 2))

asyncio.run(main())
```

在这个小实验里，你可以观察到队列长度从不超过 3，生产者会被消费者的速度牵制。`queue.join()` 依赖 `task_done()` 与每次 `put` 数量匹配；哨兵消息也需要正常确认。本例用哨兵结束工作线程式的消费者任务，但在异常处理中还需要更完善的生命周期管理。

## 背压到底传递给谁？

在真实网络服务里，客户端仍可能继续发送请求。服务端的有界队列只是一个**局部限制**；如果上游生产逻辑仍无界缓冲，压力会转移到其他位置。因此真正的背压必须沿着链路传播：

```text
客户端发送速度
    ↓
接入层的连接/请求上限
    ↓
有界等待队列
    ↓
上游请求并发限制
    ↓
上游响应处理速度
```

需要综合使用并发上限、排队容量、超时、拒绝策略以及网络流控机制。`asyncio.Queue` 不等价于 TCP 流量控制；二者处于不同层级。

## 从性能到系统稳定性

这章没有新增系统调用。问题来自“一个线程能管理很多等待任务”这一能力被用到极端：CPU 可能不忙，但**内存和等待时间已无法控制**。因此背压不是异步编程中的锦上添花，而是处理到达速率与服务速率失衡的必要手段。

进一步核对 [asyncio Queue](https://docs.python.org/3.13/library/asyncio-queue.html) 和 [Semaphore](https://docs.python.org/3.13/library/asyncio-sync.html#asyncio.Semaphore)。

**下一步的问题**：现在我们限制住了等待任务的规模。可是如果上游返回后要执行一段很重的计算，为什么同样的事件循环突然变慢？去[第 12 章](12-parallelism.md)。
