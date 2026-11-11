---
title: "异步与高并发 09：怎样把回调重新写得像顺序代码？"
sidebar_label: "九、怎样把回调写得像顺序代码"
sidebar_position: 10
description: "从保存任务状态的成本推导协程、Task、Future 和 await。"
slug: /async-concurrency/09-coroutines
---

# 怎样把回调重新写得像顺序代码？

现在我们的最小事件循环已经能知道哪个连接可读，却需要手工保存每一步的状态。如果一个请求依次经过“连接、读取、解析、组合”，回调和状态字段就容易失去清晰的控制流程。

我们真正缺少的不是更强的 `epoll`，而是**让业务函数能够在 I/O 等待时暂停，以后从暂停位置恢复**。

## 先观察两种写法

回调写法需要明确告诉循环下一步是什么：

```python title="概念伪代码：只展示控制流"
def after_connect(conn):
    register_readable(conn, after_read)

def after_read(data):
    parse_then_reply(data)
```

如果函数自己能够暂停和恢复，写法就可以回到自然顺序：

```python title="概念伪代码：表达目标，不是可直接运行的接口"
async def handle():
    data = await read_from_upstream()
    parsed = parse(data)
    return parsed
```

`await` 暂停的是**当前协程的后续执行**，并不要求它所在的 OS 线程跟着进入睡眠。之后由执行器安排这个协程继续运行。

## 用真实 asyncio 复现两个上游

本实验仍使用两个本机字节流连接，让主线程的事件循环分别读取它们。`loop.sock_recv` 要求 socket 是非阻塞的。

```python title="实验 09｜两个协程在一个事件循环中交替等待"
import asyncio
import socket
import threading
import time

def upstream(writer, name, delay):
    time.sleep(delay)
    writer.sendall((name + ":42\n").encode())
    writer.close()

async def fetch(name, reader):
    loop = asyncio.get_running_loop()
    data = await loop.sock_recv(reader, 1024)
    print("complete", name, data)
    return data

async def main():
    a_reader, a_writer = socket.socketpair()
    b_reader, b_writer = socket.socketpair()
    a_reader.setblocking(False)
    b_reader.setblocking(False)
    ta = threading.Thread(target=upstream, args=(a_writer, "A", 0.20))
    tb = threading.Thread(target=upstream, args=(b_writer, "B", 0.10))
    ta.start(); tb.start()
    try:
        start = time.monotonic()
        results = await asyncio.gather(fetch("A", a_reader), fetch("B", b_reader))
        print("elapsed:", round(time.monotonic() - start, 2), "count:", len(results))
    finally:
        a_reader.close(); b_reader.close()
        ta.join(); tb.join()

asyncio.run(main())
```

上游的两个模拟线程只是用于制造到达时间；**客户端侧的两个 `fetch` 协程由同一个事件循环管理**。预期 B 先完成，整体时间接近最长的那次等待，而不是两次等待之和。此示例每个上游只发送一小段数据；生产版要循环读取、进行消息分帧。

## 协程、Task、Future 谁负责什么？

这里必须清楚区分：

1. 调用 `async def` 函数会产生**协程对象**；仅创建对象通常尚未开始执行函数体。
2. `asyncio.gather` 将传入的协程安排成任务并等待结果；`asyncio.create_task` 则显式创建可调度的 **Task**。
3. **Future** 表示以后才会得到的结果或异常；等待 Future 可以使协程暂停，结果准备好后触发恢复流程。
4. **事件循环**负责推进就绪任务、处理计时器与 I/O 事件。协程本身不会自行轮询网卡。

```text
Task 执行 fetch 的代码
  ↓
遇到 await sock_recv，暂时没有数据
  ↓
fetch 协程暂停；事件循环处理其他就绪任务
  ↓
内核报告 socket 可读
  ↓
运行时完成等待、使 Task 恢复
  ↓
fetch 从 await 后继续执行
```

不应把这个简化流程误认为“每个 await 都会发生一次系统调用”。如果等待对象已完成，`await` 可能无需真正挂起；不同实现还有优化。

## 一个反直觉实验：有 await 也可能串行

下面代码创建第一个协程后就等它完成，再创建第二个。它没有获得两个等待同时推进的收益：

```python title="可独立运行：先后等待与并发等待的区别"
import asyncio
import time

async def fetch(name):
    await asyncio.sleep(0.1)
    return name

async def main():
    t = time.monotonic()
    await fetch("A")
    await fetch("B")
    print("sequential await:", round(time.monotonic()-t, 2))
    t = time.monotonic()
    await asyncio.gather(fetch("A"), fetch("B"))
    print("concurrent:", round(time.monotonic()-t, 2))

asyncio.run(main())
```

常见输出约为 `0.2` 与 `0.1` 秒。这证明 `await` 是等待与恢复的语言工具，**不是自动把所有操作变成并发**。

## 与真正源码建立联系

本章的代码是教学实验，不是 CPython 内部源码。Python 3.13 官方资料分别定义了 [Task/Future](https://docs.python.org/3.13/library/asyncio-task.html) 和 [loop.sock_recv](https://docs.python.org/3.13/library/asyncio-eventloop.html#asyncio.loop.sock_recv) 的行为。想进一步定位实际实现，可从 `Lib/asyncio/tasks.py`、`Lib/asyncio/futures.py` 与 `Lib/asyncio/selector_events.py` 在固定版本的 CPython 仓库中查看；不要假定它们内部永远使用同样的函数结构。

**下一步的问题**：协程可以暂停后恢复，但如果上游永远不返回、用户已经离开，谁来清理这个尚未完成的任务？去[第 10 章](./10-cancellation.md)。
