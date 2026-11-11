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

## 我们这次真正需要什么？

最初只有一个请求，阻塞式 `recv` 已经够用。现在假设一次会有多个客户端：

1. 客户端发送一行 `query\n`。
2. 服务端向两个上游各读取一个整数。
3. 上游响应时间不可预测，应当允许等待重叠。
4. 同时访问上游的数量必须受限。
5. 单个客户端不应该无限等待。
6. 处理完连接要清理资源。

这里选 Python `asyncio` 实现，但下面的原理并不只属于 Python。

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

## 这个教学服务还缺什么？

这里必须诚实列出边界。它**不是生产可部署的公网服务器**：没有认证、日志、限流入口、完整异常响应、请求体大小上限、连接生命周期追踪或跨实例的资源限额。`Semaphore` 限制的是同时访问上游的数量，**并没有限制接入端创建多少客户端任务**；真正背压还要在入口和排队环节实现。超时取消也不会自动撤销上游已发生的副作用。

另外，`asyncio` 使用的底层机制依平台不同。Unix 上常见 `SelectorEventLoop`（使用合适的选择器，例如 epoll/kqueue），Windows 可以采用不同的 I/O 完成机制；并不意味着每一次 `await` 都直接调用 `epoll_wait`。

## 用我们已经掌握的语言比较架构

| 要求 | 一请求一 OS 线程 | Python/Node 风格事件循环 | Go goroutine / Java 虚拟线程 |
|---|---|---|---|
| 编程模型 | 顺序、每线程单独等待 | 明确异步任务及其恢复 | 顺序式代码，由运行时管理轻量执行单元 |
| 等待时的 OS 线程成本 | 可能占住对应线程 | 可释放事件循环去服务其他任务 | 在支持的挂起场景可释放承载线程 |
| CPU 密集负载 | 可利用多个核心，受语言运行时约束 | 需避免长时间堵住事件循环 | 可利用多个核心，但仍需控制任务量 |
| 取消、背压、可靠性 | 都要设计 | 都要设计 | 都要设计 |

`epoll` 代表**就绪通知**路线；Windows IOCP 与 Linux `io_uring` 的某些用法提供**完成通知**路线。两种模式都可能支撑高级异步接口，但不要将它们混为一个系统调用机制。可对照 [epoll(7)](https://man7.org/linux/man-pages/man7/epoll.7.html)、[io_uring(7)](https://man7.org/linux/man-pages/man7/io_uring.7.html)、[Python asyncio](https://docs.python.org/3.13/library/asyncio.html)。

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
