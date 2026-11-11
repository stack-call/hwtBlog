---
title: "异步与高并发 08：内核告诉我们 I/O 就绪后，谁推进任务？"
sidebar_label: "八、I/O 就绪之后谁推进任务"
sidebar_position: 9
description: "从手工事件分发搭建最小事件循环。"
slug: /async-concurrency/08-event-loop
---

# 内核告诉我们 I/O 就绪后，谁推进任务？

上一章已经让一个线程从 `epoll` 得到多个 socket 的就绪通知。但新的问题很明显：内核不知道上游 A 应该进行“读响应头”，还是“读响应体”，更不知道读完后要交给哪个客户端。

于是，我们需要在**用户态保存每个请求的业务进度**，并在适当事件到来时让对应逻辑继续运行。这个小小的需求将推动我们做出一个最简事件循环。

## 为什么 epoll 返回一个 fd，还不等于业务继续执行

到第 07 章为止，我们可以用 Linux 通知高效地发现“哪些 socket 现在适合尝试读取”。但设想一个更真实的客户端：它要先发 `query\n`，查询服务才去访问两个上游，收到两份结果再回复。现在每个连接处在不同阶段：

```text
客户端 A：已接收 "que"，等待后续 "ry\\n"
客户端 B：请求已完整，正在等待上游 A 的结果
客户端 C：两份上游都完成，等待把响应写出去
```

哪怕 `epoll` 返回了 A 的 fd，它也只知道“此时可读”，不知道 A 之前已经收了 `que`。因此我们需要保存**连接对应的业务状态**。最直觉的设计是字典：`fd → (输入缓冲区, 当前阶段, 处理函数)`。当收到就绪事件时，找到这个 fd 的处理函数，再推进它一步。

这不是为了制造复杂架构；它只是把我们之前散落在局部变量里的“执行到哪了”保留下来。它在只有一个请求的版本里根本不需要，因为同步函数的调用栈本身就保存了执行状态；只有一个线程交替照顾多个请求时，保存每个请求的独立进度才变得必要。

## 最直觉的做法：把 socket 与处理函数关联

如果只有一个上游，我们收到可读事件就调用 `recv` 即可。现在有 A、B 两个上游，必须知道**哪个事件属于哪个请求**。Python 的 `selectors` 模块允许注册 socket 以及自定义 `data`，并由 `select()` 返回这份关联信息。

```python title="实验 08｜最小事件循环：两个上游、两份状态"
import selectors
import socket
import threading
import time

selector = selectors.DefaultSelector()
connections = []

def make_upstream(name, delay):
    reader, writer = socket.socketpair()
    reader.setblocking(False)
    connections.extend([reader, writer])

    def produce():
        time.sleep(delay)
        writer.sendall((name + ":42\n").encode())
        writer.close()

    thread = threading.Thread(target=produce)
    thread.start()
    return reader, thread

threads = []
for name, delay in [("A", 0.20), ("B", 0.10)]:
    reader, thread = make_upstream(name, delay)
    threads.append(thread)
    selector.register(reader, selectors.EVENT_READ, data={"name": name, "buffer": b""})

try:
    while selector.get_map():
        for key, mask in selector.select(timeout=1.0):
            state = key.data
            try:
                part = key.fileobj.recv(1024)
            except BlockingIOError:
                continue  # 就绪条件可能改变；下次再试
            if part:
                state["buffer"] += part
                if b"\n" in state["buffer"]:
                    print("complete", state["name"], state["buffer"].split(b"\n", 1)[0])
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
            else:
                print("closed", state["name"])
                selector.unregister(key.fileobj)
                key.fileobj.close()
finally:
    selector.close()
    for thread in threads:
        thread.join()
```

两个上游相互独立，B 通常先完成。**没有一个“为 A 的客户端读取而阻塞等待”的线程**；主线程在 selector 上等就绪事件，随后短暂处理对应状态。示例只按首个 `\n` 判断消息结束，没有处理同一连接上的多条响应；真实协议需要更完整的状态机。

## 先造一个只有两种动作的最小调度循环

可以把机制压缩成下面几行，这段是**概念伪代码**，并不冒充 `asyncio` 的实现：

```python title="概念伪代码：统一等待事件，分发到相应业务处理器"
watch = {}  # fd -> handler，保存每个连接的处理逻辑
while watch:
    events = poller.wait()
    for fd, flags in events:
        handler = watch[fd]
        handler.on_ready(flags)
```

这里每个名字存在都是有原因的：`watch` 保存连接和处理逻辑之间的关系；`poller.wait()` 从内核获得就绪事实；`on_ready` 把事实转化为一次用户态业务动作。循环只是让“等待—分发—再次等待”持续发生。

为什么不能在 `on_ready` 里直接做一个可能阻塞五秒的 `recv` 或复杂 CPU 计算？因为这时**整个事件分发循环**就被这个处理器占住了。它无法及时分发其他 fd，即使那些 fd 已经准备好。这个局限是后来“非阻塞 handler”“协作式让出控制权”等设计出现的根源。

我们暂时不需要协程。一个保存状态的回调就足以工作。但随着业务阶段增加，handler 可能充满 `state == WAIT_A`、`state == WAIT_B` 之类条件判断；它能工作，却越来越难读。下一章才有理由引入保存暂停位置的协程。

## 哪一行真正构成事件循环？

核心就是：

```python title="教学化简：不是 CPython 源码"
while pending:
    events = selector.select(timeout)
    for event in events:
        advance_the_corresponding_request(event)
```

当没有可执行的业务代码时，线程可以**阻塞在 `select/epoll` 等 OS 等待接口**；当 I/O 就绪时，用户态循环恢复对应的业务处理。这不是矛盾：我们没有要求“线程永远不阻塞”，而是避免“一个线程只为一个连接阻塞”。

## 用同一条请求说明两种调度者分别管什么

假设一个 Linux 线程执行我们的事件循环：

```text
内核：选中事件循环所在的 OS 线程，给它 CPU
用户态：loop 从就绪队列选出回调 B，调用 B
回调 B：读取一段网络数据后返回
用户态：loop 再选择回调 A
内核：中途可能抢占这个 OS 线程去运行其他线程
```

上面的“内核选择线程”和“用户态循环选择回调”是两个不同层次的决定。内核并不知道 A、B 的业务回调；事件循环也不能保证自己永不被内核抢占。把这层关系看清楚，后面就不会把“协程切换”和“内核线程上下文切换”写成一回事。

顺便回答一个看似反常的现象：**一个事件循环线程本身可以阻塞在 `epoll_wait` 里，但整个程序仍然实现异步服务模型。** 因为这次阻塞代表的是“没有任何就绪业务可执行”，而不是“为某条空闲连接独占一个 OS 线程”。

## 这里到底有几种“调度”？

- **内核调度器**调度 OS 线程使用 CPU。
- **用户态事件循环**根据事件与任务状态选择哪个回调或请求步骤先运行。

事件循环执行的回调通常仍在某个 OS 线程上执行。若回调中运行很长的 CPU 计算，其他回调可能被延迟；事件循环不会神奇地让一条线程并行计算。

## 为什么这个方案还不够舒服？

你已经看到状态里保存 `buffer`，后续如果还要保存“已读头部”“等待另一上游”“请求已取消”等字段，回调逻辑会越来越复杂。原本自然的业务流程：

```text
读取上游 A → 解析 A → 读取上游 B → 合并 → 返回客户端
```

如今被拆成多个事件函数，还得手工维护下一步。从功能上它已经能工作；从开发体验上，迫使我们寻找**可暂停、稍后从原处恢复的执行流程**。

## 业务状态机为什么会越来越复杂：一条完整的异常路径

我们先前只研究正常路径：读完请求，发起上游请求，收到回复，写回客户端。真实服务里还有一种输入：客户端只发送 `b'que'` 后断开连接。此时单靠 `EPOLLIN` 提示“可读”并不能判断业务是否完成，必须由业务处理器先读取，看见 EOF，再根据已有缓冲区里没有换行，认定请求不完整并清理资源。

于是连接状态至少要回答这些问题：

| 当前状态 | 新观察到的事件 | 最小正确动作 |
|---|---|---|
| 还没收齐一行 | 读到更多字节 | 追加缓冲区，检查换行 |
| 还没收齐一行 | `recv` 返回 EOF | 报不完整请求，释放连接 |
| 正在等待上游 | 上游超时 | 转为错误响应并取消无用工作 |
| 响应尚未发完 | 发送 socket 可写 | 继续发送剩余字节 |
| 任意状态 | 发生致命错误 | 注销 fd、关闭资源、清理映射 |

只要业务阶段超过两个，手写 `if state == ...` 的代码就会增多。我们完全可以把它写正确，但每个状态都需要维护“下一步”与“错误后去哪儿”的关系。这就是第 09 章协程写法有吸引力的真实原因：减少**人工维护暂停位置**的工作，而不是让 OS 本身变得更快。

对照事件循环的完整执行流程，我们还会发现一个实现责任：**注册、注销与关闭必须成对管理**。若某个 fd 已经关闭却在字典里留下处理器，OS 后续可能复用这个整数 fd，导致事件误投到旧状态对象。教学实验短小，连接关闭后直接退出，不容易遇到这种情况；真实服务器必须为每条连接维护明确生命周期。

最后用一句话切开用户态和内核的边界：内核有能力报告“fd=7 可读”，却不能替你决定“这是客户端的第 2 个商品价格还是第 3 个 HTTP 字段”。因此事件循环不是一个新增的“网络协议”，而是一套把 I/O 事实交给正确业务处理者的用户态组织机制。

## 官方实现入口

本章程序是教学实现。若进一步阅读 Python 3.13 的真实事件循环接口，可从 [asyncio event loop 文档](https://docs.python.org/3.13/library/asyncio-eventloop.html) 的 `SelectorEventLoop` 及其标注的源码入口 `Lib/asyncio/base_events.py`、`Lib/asyncio/selector_events.py` 开始。不同平台的执行器可能不同：Windows 还存在 IOCP 对应的实现。

## 只引入事件循环，仍然缺少“暂停后的调用位置”

假设处理器已经收到一行 `query`，现在要请求 A 和 B，然后把它们的返回值相加。我们可以把业务拆成三个回调，分别等待两次上游可读：

```python title="概念伪代码：显式回调保存下一步业务"
def on_client_line(line):
    request_a(line, callback=on_a_reply)

def on_a_reply(value_a):
    request_b(callback=lambda value_b: on_b_reply(value_a, value_b))

def on_b_reply(value_a, value_b):
    send_to_client(value_a + value_b)
```

它能表达正确流程，但 `value_a` 必须被回调闭包保存起来，才能在 B 回来后参与求和。再把超时和异常加入，就要为每个阶段写不同的错误回调。如果未来改成“同时查询 A 与 B”，还要维护“哪个先回来、是否都完成”的状态。这些状态并不是凭空可以省掉，只是需要有人负责管理。

因此下一章的设计目标不是“不要回调，回调很落后”，而是**让开发者不必在业务代码里手工展开这些跳转**。语言运行时可以保存一段函数暂停时的状态，包括局部变量与继续执行的位置，随后由事件完成驱动恢复。这样我们才有充分理由引入协程和 Future。

这也解释了为什么第 08 章只讨论最小事件循环，不先讨论 `await`：如果没经历过回调状态管理的痛点，就不会知道 `await` 真正改善的是什么，甚至会误以为 `await` 是让操作系统变快的神秘关键字。

## 一次回调管理的完整生命周期

从一个新连接开始：① 注册读事件；② 可读时追加到输入缓冲区；③ 读到换行才认定请求完整；④ 切换到等待上游状态；⑤ 上游返回后准备输出；⑥ 可写时发送剩余字节；⑦ 完成后注销并关闭。你可以发现：一条请求虽然只写了几个业务步骤，但回调方案已经需要管理状态与错误分支。

下一章我们不再改变 OS I/O 能力，也不引入另一个 `epoll`。我们只问：**能否把“状态机的显式分支”还原为“看起来是顺序执行的代码”，而由语言运行时保存暂停位置？**
