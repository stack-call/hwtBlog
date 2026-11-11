---
title: "异步与高并发 16：把所有机制拼成一个真实的并发服务"
sidebar_label: "十六、把所有机制拼成真实的并发服务"
sidebar_position: 17
description: "用完整可运行的本地服务汇总线程、I/O、事件循环、取消、背压与语言模型。"
slug: /async-concurrency/16-system-design
---

# 把所有机制拼成一个真实的并发服务

前十五章，我们从一个简单的阻塞 `recv` 出发，经历了线程并发、线程资源成本、非阻塞 I/O、就绪等待、事件循环、协程、任务取消、背压以及真正的 CPU 并行。

现在回到贯穿全书的任务：**客户端发起一次查询，服务端分别询问两个上游，再把结果合并返回。** 这是我们第一次把前面的知识放进同一个完整程序里。

## 先把最初的程序和今天的需求放在一起

第一章只需要从一个上游读 `price=42\n`。使用同步阻塞 `recv` 完全正确。之后每一次设计变化都来自可复现的新困难：两个上游等待可以重叠；连接增多时线程与队列要付成本；非阻塞读取虽然释放线程，却产生频繁重试；就绪通知解决等待效率，却不知道业务执行到哪一步；事件循环维护请求状态，协程让业务流程重新具有顺序可读性；最后还需要超时、取消与容量限制。

如果我们现在不把这些因果前提写出来，而是直接给一份很大的 `asyncio` 服务代码，读者会再次面对你一直反对的情况：`TaskGroup`、`Semaphore`、`timeout` 突然一起出现。所以下面先给每个机制一个**只能由它解决的明确需求**，再看完整程序。

| 当前需求 | 如果删掉对应机制会发生什么 | 当前采用的手段 |
|---|---|---|
| 同一查询访问两个独立上游 | 顺序等待导致延迟相加 | 两个子任务共同推进 |
| 上游可能很慢 | 处理路径可无期限等待 | 设置请求截止时间 |
| 多个客户端同时到达 | 只能按顺序服务，等待串行叠加 | 每客户端独立异步处理路径 |
| 上游允许有限并发 | 大量同时访问可能压垮上游 | 在上游调用位置使用信号量 |
| 任务失败仍需关闭 socket | 连接与缓冲区资源泄漏 | `try/finally` 与关闭逻辑 |

特别强调：这份最终实验的信号量限制**在途上游调用数**，不保证全服务的任务数与入口排队总量都有限。完整入口背压需要第 11 章所述的额外接纳限制；本章会明确保留这个工程边界，而不宣称一个教学脚本已经是生产级服务器。

## 我们这次真正需要什么？

最初只有一个请求，阻塞式 `recv` 已经够用。现在假设一次会有多个客户端：

1. 客户端发送一行 `query\n`。
2. 服务端向两个上游各读取一个整数。
3. 上游响应时间不可预测，应当允许等待重叠。
4. 同时访问上游的数量必须受限。
5. 单个客户端不应该无限等待。
6. 处理完连接要清理资源。

这里选 Python `asyncio` 实现，但下面的原理并不只属于 Python。

## 读代码之前，先预测一次请求的时间线

假设有 1 个客户端、2 个上游，二者各等待 80ms。如果上游并发额度至少为 2，客户端的大致轨迹会是：

```text
T0   接入服务，收到 query\n
T1   A、B 两个子任务被安排执行
T2   A、B 均向上游发送 get\n
T3   两个上游都处于等待阶段
T4   任意上游先返回，相关子任务恢复
T5   另一上游返回，TaskGroup 离开
T6   将两个结果相加，回复 OK 84\n
```

如果把上游并发额度改成 1，那么 A、B 虽仍属于两个子任务，却不能同时进入受限上游区域：总等待时间可能更接近两次上游延迟之和。**协程数量与同时占用外部资源的数量不是一回事。**

对于 5 个客户端，情况更明显：总共会尝试产生 10 次上游调用，但额度为 2；等候信号量的协程可以暂停，不需各占一个 OS 线程。现在再读下面完整代码，`TaskGroup` 和 `Semaphore` 都不会像魔法符号一样突然出现。

## 一个文件里的完整实验

本章把**模拟上游、服务端和 5 个测试客户端**放在同一块代码中。只使用标准库，不需要访问互联网、不需要 API Key。将其保存为临时脚本执行即可。

```python title="实验 16｜本地并发数据查询服务（Python 3.11+）"
import asyncio
import time

HOST = "127.0.0.1"
UPSTREAM_DELAY = 0.08
UPSTREAM_LIMIT = 2

async def close_writer(writer):
    writer.close()
    try:
        await writer.wait_closed()
    except ConnectionError:
        pass

async def upstream_handler(reader, writer):
    """模拟上游：收到一行请求，稍后回复一个数字。"""
    try:
        if await reader.readline() == b"get\n":
            await asyncio.sleep(UPSTREAM_DELAY)
            writer.write(b"42\n")
            await writer.drain()
    finally:
        await close_writer(writer)

async def query_one(upstream_port, limiter):
    """在并发上限内完成一次上游请求。"""
    async with limiter:
        reader, writer = await asyncio.open_connection(HOST, upstream_port)
        try:
            writer.write(b"get\n")
            await writer.drain()
            line = await reader.readline()
            if not line:
                raise ConnectionError("upstream closed")
            return int(line.strip())
        finally:
            await close_writer(writer)

async def client_handler(reader, writer, upstream_port, limiter):
    """一个客户端请求，对两个上游查询并合并。"""
    try:
        if await reader.readline() != b"query\n":
            writer.write(b"ERR bad request\n")
        else:
            try:
                async with asyncio.timeout(1.0):
                    async with asyncio.TaskGroup() as tg:
                        a = tg.create_task(query_one(upstream_port, limiter))
                        b = tg.create_task(query_one(upstream_port, limiter))
                    total = a.result() + b.result()
                writer.write(f"OK {total}\n".encode())
            except TimeoutError:
                writer.write(b"ERR timeout\n")
        await writer.drain()
    finally:
        await close_writer(writer)

async def test_client(port, ident):
    reader, writer = await asyncio.open_connection(HOST, port)
    try:
        writer.write(b"query\n")
        await writer.drain()
        reply = await reader.readline()
        print("client", ident, reply.decode().strip())
    finally:
        await close_writer(writer)

async def main():
    upstream_server = await asyncio.start_server(upstream_handler, HOST, 0)
    upstream_port = upstream_server.sockets[0].getsockname()[1]
    limiter = asyncio.Semaphore(UPSTREAM_LIMIT)

    service_server = await asyncio.start_server(
        lambda r, w: client_handler(r, w, upstream_port, limiter),
        HOST, 0,
    )
    service_port = service_server.sockets[0].getsockname()[1]

    try:
        start = time.monotonic()
        await asyncio.gather(*(test_client(service_port, i) for i in range(5)))
        print("elapsed:", round(time.monotonic() - start, 3))
    finally:
        service_server.close()
        upstream_server.close()
        await service_server.wait_closed()
        await upstream_server.wait_closed()

asyncio.run(main())
```

预期：五个客户端都收到 `OK 84`，打印顺序不必固定。由于一共十次上游请求，同时最多允许两次，每次上游模拟等待约 0.08 秒，总耗时通常明显大于 0.08 秒，但具体数字不能视为跨平台保证。

## 用四个层次追踪同一条请求

我们不用一上来画巨大的架构图，而是把你已经了解的四个层次对齐：

```text
业务：       收到 query → 开 A/B 上游查询 → 合并 → 回复
Python：    Task / TaskGroup / Future / await / Event Loop
操作系统：  非阻塞 socket / 就绪等待 / 线程调度
硬件：      CPU 执行指令；网卡、计时器等提供外部事件
```

**业务层**知道什么时候算“得到完整答案”；TCP 和 `epoll` 不知道字符串 `OK 84` 有什么含义。**Python 运行时**知道某个 Task 等待了哪个操作，可以在完成后恢复协程；但它不直接决定物理 CPU 核心什么时候调度。**操作系统**管理 socket 缓冲区和内核线程；但它不理解 Python `TaskGroup` 的业务父子关系。

在 Linux 典型 asyncio Selector 事件循环中，当 `await reader.readline()` 暂时没有足够数据时，相关 Task 可以暂停，事件循环继续推进其他任务。等可读事件出现，运行时读取并更新相关等待状态；条件满足后，Task 才沿着原来的 `await` 继续执行。

这一整条链中没有哪个单独的机制“实现高并发”。它们分别解决不同的局部约束：内核负责高效管理 I/O 条件，运行时负责保存和调度任务，应用负责定义完整业务结果与容量限制。现在就可以回答最初那个问题：为什么阻塞 I/O 不是错，但在大量空闲连接的特殊场景下，我们可能选择其他编排方式。

## 这段程序里每个机制为何存在？

现在我们已经能按一次请求的真实运行顺序回答：

```text
客户端 TCP 连接到数据查询服务
       ↓
asyncio 接收连接，为它安排处理任务
       ↓
client_handler 读取一行请求（暂时可暂停）
       ↓
TaskGroup 发起 A、B 两个上游查询
       ↓
Semaphore 控制访问上游的并发数量
       ↓
无数据时，协程暂停；运行时管理 I/O 就绪
       ↓
上游回复：查询任务恢复、解析整数
       ↓
TaskGroup 汇总结果，写回客户端
       ↓
finally 关闭连接；超时路径触发取消与清理
```

- OS 层负责 socket、网络协议栈、系统调用与线程调度。
- Python 运行时负责将可等待的网络操作组织成可恢复的 Task，推进事件循环。
- 应用负责业务协议、超时、并发上限与结果组合。

它们是合作关系，而不是某一层替代另一层。

## 三个故意保留的反例：不要把演示程序当成生产系统

**第一，入口积压。** 即使 `UPSTREAM_LIMIT=2`，大量客户端仍可能被接纳、创建 handler，随后在信号量外等待。单个信号量不是全链路背压。真正的服务会结合接入队列、连接数限制、超时、拒绝响应与上游容量保护。

**第二，请求取消与外部副作用。** 客户端离开后，框架是否立刻观察到断连并终止子任务，必须看具体读取/写入行为和取消传播方式。即使 Python Task 被取消，远端上游可能已经执行了请求，不能声称副作用自动回滚。

**第三，CPU 计算。** 如果把求和换成每次耗费 300ms 的纯 Python 计算，一个事件循环线程会被占住。真正需要多核加速时应研究进程、可释放 GIL 的原生计算、不同运行时的线程能力与跨执行单元的数据传递。

还有 TCP 分帧、限长、TLS、身份认证、日志、监控、半关闭与写缓冲等复杂性。这些不是被忘记，而是暂时没有进入我们的中心问题；本系列不把一份最小演示虚构成完整生产架构。

## 这个教学服务还缺什么？

这里必须诚实列出边界。它**不是生产可部署的公网服务器**：没有认证、日志、限流入口、完整异常响应、请求体大小上限、连接生命周期追踪或跨实例的资源限额。`Semaphore` 限制的是同时访问上游的数量，**并没有限制接入端创建多少客户端任务**；真正背压还要在入口和排队环节实现。超时取消也不会自动撤销上游已发生的副作用。

另外，`asyncio` 使用的底层机制依平台不同。Unix 上常见 `SelectorEventLoop`（使用合适的选择器，例如 epoll/kqueue），Windows 可以采用不同的 I/O 完成机制；并不意味着每一次 `await` 都直接调用 `epoll_wait`。

## 三次改动，验证我们真的理解了系统，而不是仅仅跑通代码

第 16 章的完整脚本只要运行成功，通常会看到几个客户端都收到合并结果。但是“能返回 `OK 84`”还不够证明我们理解了它。现在有意识地改变三个前提：

**改动 A：把 `UPSTREAM_LIMIT` 从 2 改成 1。** 在上游总任务数不变时，一次只允许一个上游调用进入关键区域。预计总耗时上升，但同一时刻的上游压力降低。解释这个现象时，必须同时提及：协程可以大量存在，真正占用上游配额的操作却有限。不能据此说“只有一个协程在运行”。

**改动 B：把 `UPSTREAM_DELAY` 从 0.08 改成 2.0，同时保持请求截止时间 1.0。** 预计很多请求得到超时响应而非正常值。讨论这个结果时，请分清：请求等待超时、子任务收到取消、上游模拟处理是否已经被执行到某个步骤，这三件事分别发生在哪里。取消不会自动撤销已经发送到远端的外部操作。

**改动 C：在上游完成后插入一次 300ms 的纯 Python 计算。** 即使网络 I/O 的等待依然重叠，这段计算如果直接在事件循环线程执行，也会拖慢其他客户端响应。我们于是重新遇到第 12 章的瓶颈：并发任务的存在不等于 CPU 计算并行。

这三次实验不是让你记住哪个参数“越大越好”，而是把不同资源的控制权对应起来：`Semaphore` 对上游在途数量负责，`timeout` 对等待期限负责，CPU 调度方案对计算资源负责。它们并不能互相替代。

如果继续做生产级服务，下一阶段才需要从这里引入连接池、真正的入口限流、认证、监控、超时分层、协议分帧与稳定的服务关闭过程。那些工程专题应该由新的失败输入推动，而不是提前在这 16 章里堆满配置项。

## 用我们已经掌握的语言比较架构

| 要求 | 一请求一 OS 线程 | Python/Node 风格事件循环 | Go goroutine / Java 虚拟线程 |
|---|---|---|---|
| 编程模型 | 顺序、每线程单独等待 | 明确异步任务及其恢复 | 顺序式代码，由运行时管理轻量执行单元 |
| 等待时的 OS 线程成本 | 可能占住对应线程 | 可释放事件循环去服务其他任务 | 在支持的挂起场景可释放承载线程 |
| CPU 密集负载 | 可利用多个核心，受语言运行时约束 | 需避免长时间堵住事件循环 | 可利用多个核心，但仍需控制任务量 |
| 取消、背压、可靠性 | 都要设计 | 都要设计 | 都要设计 |

`epoll` 代表**就绪通知**路线；Windows IOCP 与 Linux `io_uring` 的某些用法提供**完成通知**路线。两种模式都可能支撑高级异步接口，但不要将它们混为一个系统调用机制。可对照 [epoll(7)](https://man7.org/linux/man-pages/man7/epoll.7.html)、[io_uring(7)](https://man7.org/linux/man-pages/man7/io_uring.7.html)、[Python asyncio](https://docs.python.org/3.13/library/asyncio.html)。

## 一张最后才有资格出现的执行链图

第一章只有一行 `recv()`，根本无需知道下面整张图。现在我们已经亲手推导了每一层存在的原因，才把它们放到一起：

```text
客户端到达一个 query 请求
          │
          ▼
业务 handler（知道何时收齐完整的一行）
          │
          ├── A 上游 Task ──┐
          └── B 上游 Task ──┤  TaskGroup 管理子任务范围
                            │
        任务遇到 I/O 等待 ───┘
          │
          ▼
      asyncio Event Loop
          │  保存/恢复 Task，安排其他就绪工作
          ▼
      非阻塞 socket 调用
          │
          ▼
     Linux I/O 就绪机制
          │  内核管理连接状态与等待条件
          ▼
      网卡/网络协议栈
          │
          └── 相关 socket 可读 → 事件循环继续读取
                                     │
                                     ▼
                            Task 恢复并处理结果
                                     │
                                     ▼
                             服务返回 OK 84
```

请注意这是一张**概念层次关系图**，不是 Linux 或 CPython 的逐函数调用栈。真实系统的 TCP/IP 数据流、事件循环内部状态与具体 socket 方法之间，会存在更多实现细节；但这些细节不改变图中的职责边界。

我们的最终架构不是“最初方案必然失败，所以后来全部换成异步”，而是一系列权衡：少量慢 I/O 可以用同步线程；大量长连接可以用事件驱动；有些语言通过轻量线程保留同步风格；CPU 密集工作需要考虑实际并行；任何模型都必须有正确的取消、背压和共享状态规则。

如果你继续写高级专题，建议挑一条**具体、可固定版本的真实源码路径**，比如 Python Selector 事件循环怎样对非阻塞 socket 注册读就绪，或 Go 某版本网络轮询器怎样唤醒 goroutine。每次只追一个函数调用链，并用实验观察它的结果；这样不会让“源码精读”退回无因果关系的类名罗列。

## 从最初的 `recv()` 走到这里

我们并没有证明“事件循环是所有语言的最佳选择”，而是完整经历了：

```text
一个阻塞 recv
   → 线程等待，OS 调度其他线程
   → 多线程重叠网络等待
   → 线程资源成本推动非阻塞读取
   → EAGAIN 推动就绪等待
   → select/poll/epoll 提供多路 I/O 通知
   → 用户态事件循环维护任务进度
   → 协程让暂停与恢复更容易表达
   → 取消、队列和并发限制守住系统边界
   → CPU 并行与同步原语保证计算能力与正确性
   → 各语言运行时给出不同但可比较的抽象
```

这不是技术的历史严格发明顺序，而是一条经过验证的**工程问题推导路径**。你现在再读任何一个异步框架，都可以沿四个问题追查：谁在执行？谁在等待？谁收到通知？谁恢复后续计算？这就是系列真正的终点。

## 最终验收：不看定义，亲自解释一条执行轨迹

请从 `asyncio.open_connection()` 开始，自问：① 这一步可能等待什么？② 当前协程与 OS 线程哪个暂停？③ Linux 怎样知道 socket 变得就绪？④ 哪个 Task 会被恢复？⑤ 上游数据只到了一半时谁负责继续收集？⑥ 客户端超时之后谁应该清理资源？⑦ 当请求到达速度超过处理速度时，在哪里施加入口背压？⑧ 把网络等待换成 CPU 计算后，哪个策略必须变化？

如果能沿着我们真正写过的代码回答这些问题，整套课程的目标就实现了：不是背诵术语，而是能从一条具体程序的运行过程，解释现代并发系统为什么需要这些不同层次的机制。
