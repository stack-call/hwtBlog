---
title: "异步与高并发 07：已经有 select，Linux 为什么还需要 epoll？"
sidebar_label: "七、有了 select 为什么还要 epoll"
sidebar_position: 8
description: "用注册关注集合和就绪事件理解 epoll 的适用条件。"
slug: /async-concurrency/07-epoll
---

# 已经有 `select`，Linux 为什么还需要 `epoll`？

`select` 已经解决了“一个线程等多个连接”的问题。但服务可能有上万个长期空闲的连接，每次为了等待少量活跃连接，仍要反复管理和检查整个候选集合。

我们希望**对关注的连接只做必要的登记，之后主要取得就绪事件**。Linux 的 `epoll` 为这类场景提供了适合大规模描述符监控的接口。

## 先看最小可运行的 `epoll`

```python title="实验 07｜登记两个连接并等待就绪"
import select
import socket
import threading
import time

left_a, right_a = socket.socketpair()
left_b, right_b = socket.socketpair()
for s in (left_a, left_b):
    s.setblocking(False)

def produce():
    time.sleep(0.1)
    right_b.sendall(b"from B")
    time.sleep(0.1)
    right_a.sendall(b"from A")

p = threading.Thread(target=produce)
p.start()
fd_to_socket = {left_a.fileno(): left_a, left_b.fileno(): left_b}
fd_to_name = {left_a.fileno(): "A", left_b.fileno(): "B"}

with select.epoll() as ep:
    for fd in fd_to_socket:
        ep.register(fd, select.EPOLLIN)
    remaining = set(fd_to_socket)
    while remaining:
        for fd, event in ep.poll(1.0):
            if fd in remaining and event & select.EPOLLIN:
                data = fd_to_socket[fd].recv(1024)
                print(fd_to_name[fd], data)
                ep.unregister(fd)
                remaining.remove(fd)

p.join()
for s in (left_a, right_a, left_b, right_b):
    s.close()
```

这里每个连接只接收一段已知的小数据，所以读一次之后立刻取消监听。生产程序不能只凭一次 `EPOLLIN` 就假设完整协议消息到齐；还要处理 EOF、错误、读到一部分数据等情况。

## 三个动作构成新的机制

- `epoll()`：创建内核维护的 epoll 实例。
- `register()`：登记关注哪个文件描述符的哪些事件；底层对应 `epoll_ctl` 一类操作。
- `poll()`：等待并获取已就绪事件；底层对应 `epoll_wait` 一类操作。

从概念上，内核维护**关注集合（interest list）**与**就绪集合（ready list）**。这使得程序无需每次像传统 `select` 一样重新传递整个候选集合。它通常很适合“连接很多但每轮活跃数量较少”的工作负载；然而不能因此宣称“epoll 的所有操作都是 O(1)”或“任何规模都比 select 快”。

## 为什么有水平触发和边缘触发？

默认的**水平触发**（LT）可以理解为：只要可读条件仍满足，就可能继续通知。**边缘触发**（ET）强调状态变化；处理者必须特别谨慎地在非阻塞模式下反复读取，直到 `EAGAIN`，否则可能留有数据却不再获得预期提醒。

本章使用默认 LT，避免额外复杂度。ET 不是“更高级所以一定更快”，而是要求更严格的消费策略。

## 更关键的区别：就绪 ≠ 完成

`EPOLLIN` 意味着现在可以尝试读数据，**不表示业务请求已完整到达，也不是先前某个 read 操作的完成通知**。和它不同，Windows IOCP、Linux `io_uring` 的一些操作模式提供“提交请求、稍后获得完成结果”的模型。

这一区别将在第 16 章回到系统架构时比较；现在不需要深入 `io_uring` 的共享环结构。

## 历史和真实证据

`epoll` 在 Linux 2.5 开发系列中出现，并进入 Linux 2.6；`kqueue` 是另一条 BSD 系统的发展路径。与其记年代，不如现在就从 [epoll(7)](https://man7.org/linux/man-pages/man7/epoll.7.html) 核对它的关注集合、就绪集合、LT/ET 语义。上面的示例是**教学代码**，不是 Linux 内核源码节选。

**当前成果**：我们已经有一个线程等待多个描述符的手段。但内核只知道 A、B 哪个 socket 就绪，不知道 A 的业务解析进行到哪一步。下一章要解决这个新问题：[谁来恢复每个请求的业务处理？](08-event-loop.md)
