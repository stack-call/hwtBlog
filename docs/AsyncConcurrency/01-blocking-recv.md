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

## 先把问题限定得足够小

你正在写一个价格查询服务。客户端请求一个商品价格，服务只需要从上游拿到一行 `price=42`，然后原样返回。我们此时**不讨论高并发**，因为只有一个请求；也不讨论 HTTP 框架，因为它会把我们关心的等待隐藏起来。

我们先约定一份最简单的“程序合同”：调用 `fetch()`，得到一段字节，然后调用方继续。它的代码大致是：

```python title="教学化简：只有一条正常路径"
def fetch(sock):
    return sock.recv(1024)

price = fetch(sock)
print(price)
```

这在上游能够立即提供数据时，完全合理。关键不是代码写得有问题，而是现实世界的两个时间点往往不一致：**我们想读的时刻**与**上游数据到达的时刻**。假设连接已经建立，程序在 t=0.1s 调用 `recv`，上游却在 t=1.1s 才发送。那么 t=0.1s 到 t=1.1s 之间发生了什么？

一个直觉猜测是：CPU 一遍遍问网卡“到了吗”；另一个猜测是：CPU 直接离开这条线程，改干别的。仅从 Python 打印顺序无法判断，我们需要先拿到稳定可复现的现象，再到下一章讨论调度。

注意我们把“建立 TCP 连接”和“接收数据”分开。`connect()` 也可能需要等待，但那是另一种等待条件；本章只讨论**已连接 socket 的读取**，以免一个实验同时引进三件事。后续每章都尽量保持这种限制范围的习惯。

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

## 读懂实验：是哪一行变慢，而不是整个进程变慢

上面的实验有意在同一个脚本里创建一个上游线程。这会引发一个看似矛盾的疑问：**既然我们还没学线程，为什么实验已经使用了 `threading.Thread`？** 因为我们需要一个可控制的服务器；否则外部网络的不确定性会让“等待一秒”很难稳定复现。把上游当作实验仪器就好；需要理解的业务代码只有主线程里的三行：计时、`recv()`、打印结果。

把时间写成执行轨迹：

```text
时刻        查询程序（主线程）                 模拟上游
T0          连接已经建立                     accept 返回
T1          print("before recv")             开始等待 1s
T2          进入 recv，尚无数据                尚未发送
T3          （主线程无法执行下一行）           sendall("price=42\\n")
T4          recv 返回一段字节                 连接关闭
T5          print("after recv")
```

注意 T2 和 T3 之间的空白并不意味着“这个程序没有任何线程工作”。恰好相反，模拟上游能够在等待结束后发送数据，说明进程内的其他执行路径没有被主线程的 `recv` 永久挡住。这是下一章问题的伏笔。

现在尝试改动**一个变量**：把上游的 `time.sleep(1.0)` 改成 `time.sleep(0.1)`，或者改成 `2.0`。你预计哪一行的计时会跟着变？如果答案是主线程 `recv` 前后的耗时，就说明我们已经把观察对象锁定到一次 I/O 调用，而不是“Python 整体执行慢”。

还要辨认 `recv` 的第三种结果。假如上游不发送任何字节，直接关闭连接，`recv` 并不一定永远等待：在 TCP 对端有序关闭且缓冲区数据已读尽时，它返回 `b''`。这是 **EOF**，不是“暂时没有数据”。等我们引入非阻塞模式，必须把 EOF 和 `EAGAIN` 分开，否则网络服务会错误地把正常关闭当成“稍后再读”。

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

本例恰好同时具备这两种性质，**但不能据此认为“同步就是阻塞”**。我们将在[第 05 章](./05-nonblocking.md)对同一个 `recv` 改变 socket 的阻塞模式，得到同步而非阻塞的调用。

## 先建立一个判断框架：调用完成，还是业务完成？

刚刚得到 `b'price=42\n'`，我们可以说“这一次 `recv` 完成了”，却不能泛化为“这个 HTTP 请求完成了”。因为 TCP 是字节流而非消息流，若上游分两次 `sendall(b'price=')` 和 `sendall(b'42\n')`，一次 `recv` 可能只取得前半段。甚至发送一次 `sendall`，也不保证接收端的一次 `recv` 会得到完全相同的分段。

于是有了两个不同的完成条件：

1. **调用级别**：`recv` 返回字节、返回 EOF 或报错。
2. **业务级别**：已经收齐一条以换行结尾的价格消息。

我们现在的实验故意只读取一次，是为了看清阻塞调用。若要让它成为真正可靠的“按行读取”接口，就需要如下逻辑：

```python title="教学片段：可以替换原实验中单次 recv 的逻辑"
def read_line(sock):
    collected = bytearray()
    while True:
        part = sock.recv(1024)
        if not part:  # 对端关闭
            if collected:
                raise EOFError("对端关闭时消息尚不完整")
            raise EOFError("对端关闭且无数据")
        collected.extend(part)
        if b"\n" in collected:
            return bytes(collected).split(b"\n", 1)[0]
```

这里还没有处理“同一次 `recv` 收到多行”的剩余字节，这是有意保留的限制：如果它变成长期运行的协议解析器，就得维护跨读取的缓冲区。每新增一层复杂度，都必须有实际输入迫使我们这样做。

读者可以把当前程序想成“必须拿到返回值，调用者才能进入下一行”的接口；这就是本系列讨论**同步读取**时的第一条具体语义。至于当前线程是否会等，则由 socket 阻塞模式和数据是否就绪等条件决定，二者不是一件事。我们还不能据此说“同步意味着阻塞”。这个反例会在第 05 章亲眼见到。

## 用系统调用观察，不靠猜

在 Linux 上将完整脚本临时保存为 `chapter01.py` 后，可使用：

```bash
strace -f -tt -e trace=accept,accept4,connect,recvfrom,sendto,close python chapter01.py
```

可以期待看到 `recvfrom` 或相近系统调用等待到数据可用后返回。`-f` 用于跟踪实验中创建的线程；不同 Python 与内核版本可能显示不同调用组合，不应凭一份示例输出推断所有系统都相同。若系统没安装 `strace`，仅运行 Python 实验也能验证用户可观察的等待。

> [!NOTE] 本章边界
> `time.sleep` 是**模拟上游延迟**，不是主线程发起网络读取的实现方式。真正阻塞主线程的是没有数据可读时的 `recv()`。此外，“同步”在并发控制领域也指线程协调，这里不讨论那个含义。

## 更深入一层：如果读不到数据，程序究竟在“等什么”？

对阻塞 `recv()`，最容易产生的错误直觉是“执行了一条耗时一秒的 CPU 指令”。其实这次调用并没有一个固定的“一秒计算任务”。我们把上游发送时间从 0.1 秒改到 2 秒，`recv` 的等待时长就跟着变了；业务函数本身没有多做任何算法计算。也就是说，控制程序进度的关键不再是 CPU 执行了多少条指令，而是**某个外部条件何时成立**。

从操作系统角度，可读条件也不等于“网卡收到了一个包”。协议栈必须把对应的数据处理到这个 socket 的接收路径；已接收数据可能被缓存，应用调用 `recv` 时通常从相应缓冲区读取。程序处理的是字节流，不是原样“取走网卡一个包”。如果你把这些层次混成一件事，就会误以为每个 TCP 包恰好对应一次 Python `recv`。

可以利用“发送两段数据”挑战自己的假设：

```python title="实验 01B｜上游分段发送，客户端按行累计（独立运行）"
import socket, threading, time
listener = socket.socket()
listener.bind(("127.0.0.1", 0)); listener.listen()
port = listener.getsockname()[1]

def server():
    conn, _ = listener.accept()
    with conn:
        conn.sendall(b"price=")
        time.sleep(0.15)
        conn.sendall(b"42\n")
    listener.close()

t = threading.Thread(target=server); t.start()
with socket.create_connection(("127.0.0.1", port)) as s:
    buffer = bytearray()
    while b"\n" not in buffer:
        part = s.recv(1024)
        if not part:
            raise EOFError("上游未发送完整的一行")
        print("got chunk:", repr(part))
        buffer.extend(part)
    print("complete line:", bytes(buffer).split(b"\n", 1)[0])
t.join()
```

通常会读到两段，但 TCP **不保证**这两次 `recv` 的分段一定与两次 `sendall` 对应。真正应当断言的是最终读到 `price=42`，而不是一定打印两次 `got chunk`。这是这一课最早接触到的“观察结果与 API 保证不同”原则。

再回头看同步接口：我们的 `read_line` 在返回前始终留在该函数的调用链里，直到读够一行或发生异常。若上游一直没给换行，业务层就一直等。第 10 章会解释超时怎样约束这种长期等待；现在先不要抢跑，因为线程究竟如何让出 CPU 还没有被解释。

## 历史旁注与进一步证据

多道程序系统出现的动力之一，是在外部设备等待期间充分利用 CPU。这里我们刚看到“等待”，下一章再解释操作系统如何利用这段时间。接口语义可对照 [Linux recv(2)](https://man7.org/linux/man-pages/man2/recv.2.html) 和 [Python socket 文档](https://docs.python.org/3.13/library/socket.html)。

## 把 `strace` 日志与用户代码一行一行对上

如果你使用 Linux，建议给第一份 TCP 实验增加一个 `print("PID")`，再通过 `strace -f -ttT -e trace=network` 观察 socket 相关系统调用。我们不在这里粘贴一份“所有机器都会这样”的固定日志，因为 `connect`、`accept`、`recvfrom`、`sendto` 等具体显示形式随 Python、系统调用封装和平台不同而变化。

请尝试在纸上做三列记录：

| 用户程序时刻 | 系统调用层最可能观察到的事 | 上游在做什么 |
|---|---|---|
| `socket.create_connection` 返回 | 已建立连接，相关调用已返回 | 接受连接 |
| 打印 `before recv` | 还没进入这次读取调用 | 还没发送 |
| `recv` 调用等待 | 读取调用持续未返回 | 上游人为延迟 |
| 打印 `after recv` | 读取调用已经返回 | 数据已送出并处理 |

如果你看到的是一个持续约一秒的 `recvfrom` 调用，这个日志告诉你**系统调用没有立即返回**，却依然不能单凭这一行证明“进程在内核里是哪种 TASK 状态”。下一章才通过线程状态与调度解释。这里我们刻意不抢先推理内核线程执行细节。

再思考另一种输入：上游本来就把完整一行放进了缓冲区，随后客户端才调用 `recv`。这种情况下 `recv` 可能很快返回，即使 socket 仍处于默认阻塞模式。于是“阻塞 socket”表示**必要时允许等待**，并非“每次调用都会等待一段固定时间”。把模式与实际发生的等待分开，后面的非阻塞 I/O 才容易理解。

## 本章自测：先预测，再运行

把服务器延迟改成 0.5 秒，在调用 `recv` 前增加 `print("ready to read")`。请先写下三个预测：① 哪两行打印之间会出现等待？② 假如上游立即关闭但不发送字节，`recv` 返回什么？③ 假如缓冲区已经有两个字节，`recv(1024)` 会非得等到 1024 个字节吗？

如果这三题能直接回答，本章目标就达到了。我们**仍然不知道内核如何让线程暂停，也不知道 CPU 是否空闲**。下一章恰好从这里出发，而不是突然引入一大堆调度算法。
