---
title: "异步与高并发 01：一次 recv() 为什么能让程序停下来？"
sidebar_label: "一、一次 recv 为什么会让程序停下"
sidebar_position: 2
description: "用可控制的本地 TCP 服务观察同步阻塞读取。"
slug: /async-concurrency/01-blocking-recv
---

# 一次 `recv()` 为什么能让程序停下来？

假设数据查询服务只接收一个上游结果。最直接的实现就是：连接上游，调用 `recv()`，拿到结果再打印。**只有一个请求时，这样写是正确的**，无需多线程或异步。

但是，如果上游还没有发数据，执行到 `recv()` 时到底发生了什么？是 CPU 在不断检查网络吗？还是线程停止执行了？

## 先做一个能观察到等待的实验

为了不依赖外部服务器，我们把上游服务器与客户端放进同一个 Python 脚本。上游故意等待约一秒才回复。

```python title="实验 01｜本地 TCP：单次同步读取"
import socket
import threading
import time

listener = socket.socket()
listener.bind(("127.0.0.1", 0))  # 0 表示由系统分配临时端口
listener.listen()
port = listener.getsockname()[1]

def upstream():
    conn, _ = listener.accept()
    with conn:
        time.sleep(1.0)            # 模拟上游计算/网络延迟
        conn.sendall(b"price=42\n")
    listener.close()

worker = threading.Thread(target=upstream)
worker.start()

with socket.create_connection(("127.0.0.1", port)) as sock:
    begin = time.monotonic()
    print("before recv")
    data = sock.recv(1024)
    print("after recv:", data, "elapsed:", round(time.monotonic()-begin, 2))

worker.join()
```

预期现象：先出现 `before recv`，约一秒后才出现 `after recv: b'price=42\n' ...`。具体耗时会受操作系统调度影响，不要求精确等于 1.00 秒。

这里的 `threading.Thread` **只是用于在同一个实验进程中模拟上游**，本章还不研究它如何并发执行。我们关心的是主线程调用 `recv()` 之后不能立刻走到下一行。

## `recv()` 在等待什么？

TCP 为应用提供**字节流**，接收的数据通常先由内核网络协议栈放入相应 socket 的接收队列/缓冲区。`recv(1024)` 的含义不是“必须读满 1024 字节”，而是“尝试最多读取 1024 字节”。在本例中，上游暂时没有发送数据，默认阻塞模式的 `recv()` 就可能等待。

可以把本次路径简化成：

```text
用户代码调用 sock.recv(1024)
        ↓
CPython socket 封装调用操作系统 socket 接口
        ↓
内核检查该 socket 是否有可读取的字节/结束/错误
        ↓
当前条件不满足 → 调用线程进入等待
        ↓
数据到达并被协议栈处理，等待条件满足
        ↓
线程被唤醒、重新获得运行机会，recv 返回
```

注意“**唤醒**”不等于“立刻运行”。唤醒使线程重新具备运行资格；何时真正获得 CPU 是下一章的问题。

`recv` 的成功返回只表示**这一次读取**得到一些字节。TCP 消息可能被拆成多次读取；接收到 `b""` 通常表示对端有序关闭且剩余数据已读尽。真实协议必须自己处理消息边界，这里用 `\n` 作为简单分隔符。

## 那么什么叫同步、什么叫阻塞？

这一章只给最小定义，避免提前引入异步运行时。

- **同步读取接口**：正常返回时，本次读取的结果或错误已经可以交给调用者；调用者直接拿返回值继续处理。
- **阻塞调用**：如果暂时不能满足调用条件，执行该调用的线程会等待，而非立刻返回让调用者处理“尚未就绪”。

本例恰好同时具备这两种性质，**但不能据此认为“同步就是阻塞”**。我们将在[第 05 章](05-nonblocking.md)对同一个 `recv` 改变 socket 的阻塞模式，得到同步而非阻塞的调用。

## 用系统调用观察，不靠猜

在 Linux 上将完整脚本临时保存为 `chapter01.py` 后，可使用：

```bash
strace -f -tt -e trace=accept,accept4,connect,recvfrom,sendto,close python chapter01.py
```

可以期待看到 `recvfrom` 或相近系统调用等待到数据可用后返回。`-f` 用于跟踪实验中创建的线程；不同 Python 与内核版本可能显示不同调用组合，不应凭一份示例输出推断所有系统都相同。若系统没安装 `strace`，仅运行 Python 实验也能验证用户可观察的等待。

> [!NOTE] 本章边界
> `time.sleep` 是**模拟上游延迟**，不是主线程发起网络读取的实现方式。真正阻塞主线程的是没有数据可读时的 `recv()`。此外，“同步”在并发控制领域也指线程协调，这里不讨论那个含义。

## 历史旁注与进一步证据

多道程序系统出现的动力之一，是在外部设备等待期间充分利用 CPU。这里我们刚看到“等待”，下一章再解释操作系统如何利用这段时间。接口语义可对照 [Linux recv(2)](https://man7.org/linux/man-pages/man2/recv.2.html) 和 [Python socket 文档](https://docs.python.org/3.13/library/socket.html)。

**下一步的问题**：当主线程不再执行指令时，CPU 是真的空闲了吗？去[第 02 章](./02-scheduler.md)追踪线程状态。
