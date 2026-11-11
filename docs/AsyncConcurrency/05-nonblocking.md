---
title: "异步与高并发 05：同步调用一定阻塞吗？从 EAGAIN 开始"
sidebar_label: "五、同步调用一定阻塞吗"
sidebar_position: 6
description: "同一个 recv 接口的阻塞与非阻塞行为。"
slug: /async-concurrency/05-nonblocking
---

# 同步调用一定阻塞吗？从 `EAGAIN` 开始

线程池的局限来自工作线程可能长期等待一个尚无数据的连接。如果读操作发现“暂时没数据”，立刻返回给我们，当前线程就可以去做其他事。

我们不换 API，仍用 `recv()`；**只改变 socket 的阻塞模式**。

## 同一个接收操作，结果为何不同？

```python title="实验 05｜非阻塞 recv 先失败，稍后成功"
import socket
import threading
import time

server = socket.socket()
server.bind(("127.0.0.1", 0))
server.listen()
port = server.getsockname()[1]

def upstream():
    conn, _ = server.accept()
    with conn:
        time.sleep(0.2)
        conn.sendall(b"ready\n")
    server.close()

t = threading.Thread(target=upstream)
t.start()
with socket.create_connection(("127.0.0.1", port)) as client:
    client.setblocking(False)
    try:
        print("first read:", client.recv(1024))
    except BlockingIOError as exc:
        print("not ready yet, errno:", exc.errno)

    while True:
        try:
            data = client.recv(1024)
            print("later read:", data)
            break
        except BlockingIOError:
            time.sleep(0.01)     # 先用笨办法检查；下章会替换

t.join()
```

在上游还没有发送数据时，第一次读取通常抛出 `BlockingIOError`，其对应的 Linux 错误码通常是 `EAGAIN` / `EWOULDBLOCK`。`EAGAIN` 不意味着连接已关闭、读取最终失败，表示**此刻无法在不等待的条件下取得数据**。如果执行环境极度拥塞，第一次读取恰好发生在上游发送之后，也可能直接成功；本例的 0.2 秒延迟只是提高观察概率。

## 为什么这仍叫同步？

`client.recv(1024)` 的调用语义没有变：**这一次调用在返回前就确定了自己的即时结果**——读到字节、读到 EOF，或者返回/抛出一个“此刻不可读”的错误。它没有交给程序一个表示“这次读取未来会完成”的操作对象，也没有约定完成时触发回调。

所以我们得到一个特别重要的组合：

| 操作 | 返回方式 | 等待行为 |
|---|---|---|
| 默认阻塞 socket 的 `recv` | 同步 | 没有数据时可等待 |
| 非阻塞 socket 的 `recv` | 同步 | 当前不可读时立即报告 |

“同步/异步”涉及操作的完成与结果如何交付；“阻塞/非阻塞”涉及调用线程是否需要等待。在 OS I/O 文档中这两组词有不同维度，不能简单画等号。

## 但是我们的程序变得难看了

上面虽然摆脱了长期阻塞，却开始每隔 10 毫秒调用一次 `recv()`。连接越多，检查次数越多。将 `time.sleep(0.01)` 去掉，就可能出现高频循环，CPU 在没有新数据时做大量无用检查。

这种反复检查叫**忙轮询**（busy polling）。本例加了睡眠，降低了空转但会引入额外发现延迟。它不是我们想要的最终方案。

```text
阻塞 recv：无数据 → 线程等着
非阻塞 recv：无数据 → 报 EAGAIN → 立刻返回
问题还在：怎样避免无意义地重复尝试？
```

这里暂时不要引入 `await`。我们首先只需要一个机制：让内核帮我们等“多个 socket 中至少一个变得可读”。

## 设计边界

非阻塞不代表任何调用都瞬间返回；连接创建、名称解析、TLS 握手与业务代码也可能造成其他等待或耗时。设置 `setblocking(False)` 只改变 socket 相关操作的阻塞模式。尤其要记住：**一次 `recv` 不保证拿到完整业务消息**。

官方参考：[Linux recv(2)](https://man7.org/linux/man-pages/man2/recv.2.html)、[Python socket.setblocking](https://docs.python.org/3.13/library/socket.html#socket.socket.setblocking)。

下一章不再让 Python 反复猜测是否就绪，而是直接问操作系统：[哪些连接可以读？](./06-select.md)
