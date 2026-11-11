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

## 为什么这一章不是“再教一个 Semaphore API”

上一章解决了“放弃一个已经超时的任务”。但设想服务每秒接收 500 个请求，后端每秒只能处理 100 个；每个请求都能在十秒内完成，而且客户端愿意一直等。即使没有任何逻辑错误，队列仍以**每秒约 400 个请求**的速度积累。

用最简单的守恒关系就能描述它：

```text
积压变化 ≈ 新进入的任务数 - 已完成的任务数
每秒新进入 500，完成 100
→ 每秒约增加 400 个未处理任务
```

这时增加 `Semaphore(10)` 限制同时运行的上游查询数，确实保护了上游，但**在信号量外面等待的 Task 仍可能一直增长**。一个生产系统必须区分两种上限：同时占用下游资源的任务数，以及已经接纳但尚未处理的任务总数。

真正的背压并不是一个语法词，它是一种协议关系：下游没能力接更多工作时，上游应该等、减速或收到拒绝，而不是继续无限投递。

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

## 用真实、可运行的生产者与消费者看见背压

先不引入完整服务器，把请求流简化成生产者不断投递工作、消费者每次处理一个。`asyncio.Queue(maxsize=2)` 的意义不是“让消费者更快”，而是第三个未消费的任务到来时，`put()` 必须等待空位。

```python title="实验 11A｜队列满时生产者会暂停"
import asyncio
import time

async def main():
    queue = asyncio.Queue(maxsize=2)
    start = time.monotonic()

    async def producer():
        for item in range(5):
            print(round(time.monotonic()-start, 2), "put begin", item)
            await queue.put(item)
            print(round(time.monotonic()-start, 2), "put done ", item)
        await queue.put(None)  # 结束标记

    async def consumer():
        while True:
            item = await queue.get()
            try:
                if item is None:
                    return
                print(round(time.monotonic()-start, 2), "consume", item)
                await asyncio.sleep(0.1)
            finally:
                queue.task_done()

    await asyncio.gather(producer(), consumer())
    await queue.join()

asyncio.run(main())
```

预期你会看到：初期 `put` 很快，队列填满后，后续某些 `put done` 必须等消费者腾出位置才能打印。输出时间不固定，但**生产者不能无限成功塞入**是队列接口的保证。

这里消费者的 `None` 结束标记也是一份队列元素，仍必须调用 `task_done()`；忽略这一点，`queue.join()` 可能无法返回。这说明并发控制机制不只影响性能，还给程序引入了需要维护的正确性约束。

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

## 从进程内队列一直追到网络入口

在实际服务里，如果消费者来不及处理：

```text
上游请求源 → TCP 连接 → 请求解析 → 有界业务队列 → 上游 API
```

如果业务队列满了，单纯让 `await queue.put()` 暂停，只保证**这一条生产协程**停止提交队列元素；前端网络层是否继续接收字节、操作系统缓冲区何时变满、客户端是否被拒绝，都取决于你如何设计整条链路。

因此“我用了 `Queue(maxsize=...)`，系统就不会 OOM”仍然是错误结论。应用如果一开始就为每个到来的客户端创建一个排队 Task，那么队列有界，**排在队列外等待入队的 Task 数仍可无界增长**。入口还需要有界接纳、连接限制、超时或明确的拒绝策略。

背压的设计目标是让资源消耗与系统处理能力之间形成反馈，而不是只在某个函数内部加一个 `await`。这一层引出系统容量管理；之后我们会转去研究另一种瓶颈：不是等待任务太多，而是每个任务本身都要占用大量 CPU。

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

## 把背压写成容量预算，而不是一个“用了 Queue”的标签

一个服务处理任务会消耗几类不同资源：内存中的等待对象、已经建立但暂时空闲的 socket、正在占用上游额度的请求、已经完成但待发送的响应。给其中一种资源设置并发上限，不自动限制其他资源。

例如一个服务允许同一时刻最多 20 个上游请求，每个上游请求平均耗时 200ms。暂且忽略方差和其他开销，这意味着它的理想处理能力量级约是每秒 100 次上游调用。如果客户端持续投递每秒 500 个请求，即使单次延迟不大，未进入上游执行区的请求也会持续积累。一个 `Semaphore(20)` 保护了上游，却不会主动让前端停止创建协程。

这时我们需要至少区分三层控制：

1. **正在执行的额度**：例如允许最多 20 个上游请求同时进行。
2. **等待区容量**：例如最多保留 100 个已接纳但尚未分配到上游额度的请求。
3. **入口决策**：等待区满了，是暂缓读取、返回忙碌错误、还是让客户端以后重试？

如果只有第一层，内存里的等待 Task 仍可能不断增加；如果只有第二层而不限制上游，消费者又可能在下游造成洪峰。背压的设计，是让这些容量边界形成一条实际可执行的控制路径。

不要机械地拿某个固定数字当最佳参数。`20` 和 `100` 只是展示边界关系的例子。生产系统要通过吞吐、延迟分布、上游限额、连接寿命和失败比例测量容量。队列也不应该无限囤积已经过期的请求；结合第 10 章的截止时间，过期工作通常应该尽早退出，否则容量被不再有价值的任务占满。

这也解释为什么现代异步框架除了提供 `async/await`，还常常提供限流、连接池、任务组、队列和超时等能力。它们不是异步语法的装饰，而是在真正的高并发场景下逐层暴露出来的生存需求。

## 从性能到系统稳定性

这章没有新增系统调用。问题来自“一个线程能管理很多等待任务”这一能力被用到极端：CPU 可能不忙，但**内存和等待时间已无法控制**。因此背压不是异步编程中的锦上添花，而是处理到达速率与服务速率失衡的必要手段。

进一步核对 [asyncio Queue](https://docs.python.org/3.13/library/asyncio-queue.html) 和 [Semaphore](https://docs.python.org/3.13/library/asyncio-sync.html#asyncio.Semaphore)。

## 既然队列有界，为什么服务仍可能爆内存？

这是背压章节必须有的最后一个反例。假设我们使用 `Queue(maxsize=100)`，有 10 个消费者；程序却在 socket 接受连接时无条件创建一个 handler Task。每个 handler 会执行 `await queue.put(request)`，只要队列满，它就挂在这个 await 上等待空位。

此时队列确实只有 100 个元素，但**有可能有 100,000 个 handler Task 正在队列外等待**。如果每个 Task 还持有一个客户端 socket、若干缓冲区及上下文对象，内存仍可能持续增长。于是我们必须让入队前的接纳阶段也受到容量约束，而不是只给业务队列加一个 maxsize。

真正的入口策略有几种自然选择：在接入层限制同时活动的连接；在达到限额时立即回应忙碌或拒绝；让读取端暂时停止消耗新请求，使 TCP 流量控制逐步向上游传递压力；或者使用消息队列等系统承担可持久化的排队。但每一种都有代价：拒绝会影响客户端体验，延迟读取可能占住连接，外部队列还要考虑过期与重复消费。

因此背压不是单个 Python 对象的性质，而是一条**跨越接收、解析、排队、执行、返回**的资源控制链。它要求工程师先明确容量预算，再决定在哪一层等待、在哪一层拒绝，以及什么条件下可以继续接纳。

可以为这个服务设计一份最小容量表：最多接纳 500 条连接，最多 100 个排队请求，同时最多 20 条上游请求；三项数字都只是讨论用的假设。重点是解释三道限制分别防止什么失控，以及为什么一项限额不足以替代其他两项。能解释这一点，我们才真正完成了“异步为什么也需要并发控制”这一章。

## 本章小结与下章桥梁

取消解决“这个任务已经没必要继续”；背压解决“这份工作现在能不能进入”。即使两者都设计正确，我们的服务仍可能在一种情况下很慢：把原本等待网络的上游处理换成大量计算。那时增加事件循环、扩大队列都不一定提升吞吐，因为瓶颈已经从等待 I/O 转移到了 CPU。本系列下一次真正需要改变的，是计算资源的使用方式。
