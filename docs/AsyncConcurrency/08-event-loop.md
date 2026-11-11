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

## 哪一行真正构成事件循环？

核心就是：

```python title="教学化简：不是 CPython 源码"
while pending:
    events = selector.select(timeout)
    for event in events:
        advance_the_corresponding_request(event)
```

当没有可执行的业务代码时，线程可以**阻塞在 `select/epoll` 等 OS 等待接口**；当 I/O 就绪时，用户态循环恢复对应的业务处理。这不是矛盾：我们没有要求“线程永远不阻塞”，而是避免“一个线程只为一个连接阻塞”。

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

## 官方实现入口

本章程序是教学实现。若进一步阅读 Python 3.13 的真实事件循环接口，可从 [asyncio event loop 文档](https://docs.python.org/3.13/library/asyncio-eventloop.html) 的 `SelectorEventLoop` 及其标注的源码入口 `Lib/asyncio/base_events.py`、`Lib/asyncio/selector_events.py` 开始。不同平台的执行器可能不同：Windows 还存在 IOCP 对应的实现。

**下一步的问题**：能不能既保持“遇到 I/O 就把控制权交出去”，又用连续代码描述完整流程？去[第 09 章](./09-coroutines.md)。
