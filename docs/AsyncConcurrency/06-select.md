---
title: "异步与高并发 06：不阻塞了，难道要一直检查每个 socket？"
sidebar_label: "六、不阻塞就要一直轮询吗"
sidebar_position: 7
description: "由忙轮询的缺陷自然推导 select 与 poll。"
slug: /async-concurrency/06-select
---

# 不阻塞了，难道要一直检查每个 socket？

上一章的非阻塞 `recv()` 可以立即报告 `EAGAIN`。但我们的新问题是：如果有 100 个连接尚无数据，就要反复检查 100 次，绝大部分检查什么也得不到。

我们需要新的能力：**向内核提交一批连接，等待其中至少一个满足可读条件**。这不是异步结果交付机制，而是一种 I/O **就绪等待**机制。

## 先做两个连接的最小实验

为了专门观察就绪通知，这章用 `socket.socketpair()` 创建两对已经连接的本地字节流 socket。它们不是 TCP 连接，但在“可读/不可读”的多路复用实验中可以隔离掉 `connect` 等步骤；下一章再将机制联系到 TCP 服务。

```python title="实验 06｜select 等待任意一个连接可读"
import select
import socket
import threading
import time

a_read, a_write = socket.socketpair()
b_read, b_write = socket.socketpair()

def send_later():
    time.sleep(0.15)
    b_write.sendall(b"B first")
    time.sleep(0.15)
    a_write.sendall(b"A next")

producer = threading.Thread(target=send_later)
producer.start()
readers = [a_read, b_read]

try:
    while readers:
        ready, _, _ = select.select(readers, [], [], 1.0)
        if not ready:
            raise TimeoutError("nothing became ready")
        for sock in ready:
            print("ready:", "A" if sock is a_read else "B", sock.recv(1024))
            readers.remove(sock)    # 每个演示连接只读一次
finally:
    producer.join()
    for sock in (a_read, a_write, b_read, b_write):
        sock.close()
```

预期先打印 B，后打印 A，因为我们故意先给 B 发数据。关键观察是：程序不再手写“每 10ms 尝试一次每个 socket”，而是由 `select.select` 等待一组文件描述符变得就绪。

## `select()` 到底做了什么？

对可读集合、可写集合和异常条件集合，`select` 检查哪些文件描述符满足相应就绪条件，并可在没有事件时等待。它让一个线程管理多个连接的等待条件。由于 `select` **本身可以阻塞**，所以“使用 I/O 多路复用”与“程序里没有任何阻塞系统调用”不是一回事。

```text
用户程序登记：A、B 都想读
             ↓
内核等待 A 或 B 的可读条件
             ↓
B 先变得可读 → select 返回 B
             ↓
用户程序对 B 调用 recv，推进 B 的业务状态
```

第 05 章的同步非阻塞 `recv` 与这里的 `select` 可以搭配：`select` 负责等待**何时值得尝试**，`recv` 负责**实际取得字节**。为了避免就绪状态变化、并发读取等条件下意外卡住，生产事件驱动程序通常仍将受管理的 socket 设置为非阻塞。

## 为什么还要 `poll()`？

传统 `select` 使用文件描述符集合接口，常有 `FD_SETSIZE` 等平台级约束；`poll` 用描述符数组表示关注事件，避免部分此类接口限制。不过，**`poll` 并不自动消除逐次扫描候选描述符的成本**。不同平台、连接分布和负载条件下性能差异不同。

历史上，BSD `select` 和 System V `poll` 代表了解决同一类问题的不同接口。你现在已经理解它们共同解决的问题，而不必先记住所有结构体字段。

## 观察底层系统调用

把本章代码存成临时文件后，Linux 上可以试：

```bash
strace -f -e trace=select,pselect6,poll,ppoll python chapter06.py
```

在现代 CPython/Linux 中，Python 的 `select.select()` 可能落到 `pselect6` 等系统调用；具体取决于实现版本。该观察有助于区别 Python API 名称与真实系统调用名称。

可查证：[select(2)](https://man7.org/linux/man-pages/man2/select.2.html)、[poll(2)](https://man7.org/linux/man-pages/man2/poll.2.html)。

**留下的新问题**：如果我们服务有几万个空闲连接，每轮都要传递、检查大量描述符，是否还能减少管理成本？去[第 07 章](./07-epoll.md)。
