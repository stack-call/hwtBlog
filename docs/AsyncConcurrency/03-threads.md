---
title: "异步与高并发 03：既然会等待，为什么不创建第二个线程？"
sidebar_label: "三、为什么不直接开第二个线程"
sidebar_position: 4
description: "从独立上游请求推导多线程并发与并行的区别。"
slug: /async-concurrency/03-threads
---

# 既然会等待，为什么不创建第二个线程？

现在我们的服务一次要从上游 A、B 两处取数据。如果顺序执行，每个请求都等待约 0.3 秒，总时间接近 0.6 秒。前两章告诉我们：线程等待时，CPU 可以执行其他线程。那就**先创建两个线程**，不用急着学事件循环。

## 让最简单的方案先工作

下面仍然运行在本机，不依赖外部网络。两个上游连接分别等待 0.3 秒后返回数据。客户端读取时采用逐次 `recv`，直到对端关闭，避免假设一次 `recv` 就能读到整个数据。

```python title="实验 03｜同一个服务的顺序版与双线程版"
import socket
import threading
import time

listener = socket.socket()
listener.bind(("127.0.0.1", 0))
listener.listen()
port = listener.getsockname()[1]

def upstream():
    responders = []
    for _ in range(4):          # 本实验总共发起四次连接
        conn, _ = listener.accept()
        t = threading.Thread(target=respond, args=(conn,))
        t.start()
        responders.append(t)
    for t in responders:
        t.join()
    listener.close()

def respond(conn):
    with conn:
        time.sleep(0.3)
        conn.sendall(b"value=42\n")

server_thread = threading.Thread(target=upstream)
server_thread.start()

def fetch(name):
    with socket.create_connection(("127.0.0.1", port)) as s:
        pieces = []
        while part := s.recv(1024):
            pieces.append(part)
        print(name, b"".join(pieces))

start = time.monotonic()
fetch("A")
fetch("B")
print("sequential:", round(time.monotonic() - start, 2))

start = time.monotonic()
a = threading.Thread(target=fetch, args=("A",))
b = threading.Thread(target=fetch, args=("B",))
a.start(); b.start()
a.join(); b.join()
print("threaded:", round(time.monotonic() - start, 2))
server_thread.join()
```

通常会看到：顺序约 0.6 秒，双线程约 0.3 秒加少量调度开销。这里的上游服务在后台按连接创建线程，只为提供可重叠的等待；我们比较的是**调用方**顺序请求与双线程请求。不同机器上的时间不是性能保证。

## 这为什么叫并发？

两个上游读取都可以在同一个时间段内推进。即使 CPU 只有一个核心，在其中一个线程因 `recv` 等待时，操作系统也可以调度另一个线程发起请求。这就是**并发**：多个任务在一段时间内交替或重叠推进。

**并行**要求同一时刻确实有两个执行实体在不同计算资源上执行指令。对这个实验而言，收益主要来自**I/O 等待重叠**，不需要两段 Python 代码同时计算。

```text
顺序：A 发起 ──等待── A 完成 ── B 发起 ──等待── B 完成
并发：A 发起 ──等待────── A 完成
      B 发起 ──等待────── B 完成
```

线程运行过程中可因系统调用等待、抢占式调度或时间片等原因发生切换。**多线程不等于异步 API**：每个工作线程依然同步调用阻塞式 `recv`。只是调用方可以在两个线程都工作时做其他事情，最后 `join()` 等待两个线程结束。

## 为什么需要 `join()`？

`start()` 把线程交给运行时/操作系统安排执行，并不会把 `fetch()` 的返回值直接交给当前代码。如果没有 `join()`，主线程打印总耗时可能发生在工作未完成时。这里首次出现两个不同层次的“等待”：**线程执行 `recv` 的内核等待**与**主线程等待两个工作线程完成**。

`join()` 本身可能阻塞主线程，但这不会让工作线程自动停止。要收集返回值，真实程序还需要 `Future`、队列或者其他结果传递方式；本章只打印结果，保持最小。

## 这个设计已经解决了什么？没有解决什么？

它解决了两个独立慢请求的时间重叠。对少量连接，多线程是直接、合理的方案。但如果一台服务需要保持数千甚至更多连接，每个连接都配一个工作线程，线程栈、调度与资源限制会怎样？

这不是说“一万个线程必然崩溃”，而是说**资源成本与负载有关，需要量化**。历史上线程和协程也并行发展，不是“线程被异步淘汰”。可参考 [Python threading](https://docs.python.org/3.13/library/threading.html) 和 [pthread_create(3)](https://man7.org/linux/man-pages/man3/pthread_create.3.html)。

**下一步的问题**：当客户端持续增长，线程数还能与连接数一一对应吗？去[第 04 章](04-thread-cost.md)。
