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

## 我们要消除的不是等待，而是人工维护暂停位置

上一章的事件循环已经解决两个问题：在内核级别只等待真正就绪的连接；在用户态把事件分发给正确的请求处理器。它可以正确运行，为什么还要多学一个“协程”？答案绝不是“因为语法更现代”。

假设查询请求必须依次完成三件事：收到客户端一行消息，向上游 A 请求，再向上游 B 请求，最后回复客户端。用回调与状态枚举可以写出来，但每次等待都要把下一步藏进回调、闭包或状态分支里。业务顺序原本非常清晰：

```text
读请求 → 查 A → 查 B → 发结果
```

实际回调结构却更像：

```text
on_read_ready: 若收齐行 → 注册等待 A
on_A_ready: 若拿到数据 → 注册等待 B
on_B_ready: 若拿到数据 → 准备输出
on_write_ready: 若写完 → 清理连接
```

这不是回调天然错误，而是**业务顺序与代码阅读顺序开始脱离**。如果增加超时、异常清理、调用者取消，状态分支还会变多。我们需要一个机制，能够在等待时保留“这段函数执行到了哪里”，等结果就绪后从这里继续，免去大量手写的跳转逻辑。这时才有必要引入协程。

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

## `await` 到底暂停了谁？

先只观察一个 `await` 的前后关系，不急着背 Task、Future 的完整 API：

```python title="实验 09A｜任务 A 暂停期间，任务 B 得到机会"
import asyncio

async def fetch(name):
    print(name, "before wait")
    await asyncio.sleep(0.1)
    print(name, "after wait")

async def main():
    await asyncio.gather(fetch("A"), fetch("B"))

asyncio.run(main())
```

通常的输出顺序是两个 `before wait` 先出现，然后两个 `after wait` 出现。它说明 A 的执行路径可以在 `await` 处暂停，运行时有机会继续推进 B。请务必区分三个层次：

1. A 的**协程执行**暂停，后面的 `print` 暂时不运行。
2. 当前**OS 线程**并不必因为 A 的 `await` 而睡眠；事件循环可以调用 B。
3. 若暂时没有可执行任务，**事件循环线程**反而可能在内核 I/O 等待中睡眠。

这里的 `asyncio.sleep` 是用计时器制造一个确定的暂停点。真实 socket 读取通常还需要事件循环关注 fd 就绪、读到字节后完成相应等待对象。我们没有因为改成 `await` 就绕过 Linux socket；只是把“什么时候重试与恢复”交给了运行时。

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

## 从一个无法开始执行的对象，推导出 Task 的必要性

在 Python 的常见默认语义下，调用 `fetch("A")` 只创建协程对象，不等于它已经执行。单独把它赋给变量，事件循环还不知道你希望在何时推进它。这里出现了一个明确的新需求：**有人负责把可暂停的协程安排进事件循环，并在它等待的结果就绪后恢复它。**

`Task` 就承担这样的调度包装职责。协程提供“可暂停的执行过程”；Task 负责在事件循环中推进它并跟踪完成结果；Future 表示以后可能完成的结果，外部事件或运行时机制可以让它变为完成状态。不要把这三个名字理解成三个独立 OS 线程。

一个简化的控制流程是：

```text
Task.step() 推进协程
    ↓
协程执行到 await 一个尚未完成的 Future
    ↓
Task 暂时不继续，登记“完成后再唤醒我”
    ↓
事件循环处理其他任务/等待 I/O
    ↓
Future 得到结果 → Task 再次被安排执行
    ↓
协程从 await 后继续
```

这是**教学化简**，不是对 CPython 某一版本内部函数逐行的原样摘录。真实任务调度有取消、异常、上下文变量与优化等细节；本章只证明上图的必要机制。可以对照 Python 3.13 的 [Task 文档](https://docs.python.org/3.13/library/asyncio-task.html) 与 [Event Loop 文档](https://docs.python.org/3.13/library/asyncio-eventloop.html)。

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

## 不是每一个 `await` 都意味着“马上切走”：再做一次反例

初学者看到 `await` 往往会以为：“只要执行到它，事件循环就一定先运行另一个任务。”这并不正确。某个可等待对象如果已经完成，`await` 可以直接拿到结果，根本不需要经历一次明显的暂停。反过来，一个协程如果在 `await` 之前执行了耗时的同步循环，也可能长时间占着事件循环。

试着用这个实验比较两种情况：

```python title="实验 09C｜await 已完成结果与真正等待"
import asyncio

async def immediate():
    return "already ready"

async def delayed():
    await asyncio.sleep(0.05)
    return "ready later"

async def main():
    async def observer():
        print("observer runs")

    task = asyncio.create_task(observer())
    print("before immediate")
    print(await immediate())
    print("after immediate")
    print(await delayed())
    await task

asyncio.run(main())
```

在默认调度下，`immediate()` 没有产生需要等待的未完成对象，`observer` 通常要到 `delayed()` 让出控制权后才有机会运行。**语法上出现 `await` 与运行时真的挂起是两个不同事实**。这对理解所谓“协作式调度”十分关键：事件循环只能在任务交出控制权时安排其他工作。

至于 `asyncio` 的真实代码，建议沿官方文档先找到 `Lib/asyncio/tasks.py` 中的 Task 行为、`Lib/asyncio/base_events.py` 中的事件循环推进逻辑，再根据当前版本进一步查找 Python/C 实现差异。我们在这里不贴未经核对的源码片段，也不虚构固定函数调用栈；本章给出的 Task→Future→恢复是准确表达设计责任的教学化简。

最后把这个实验和前一章连起来：第 08 章的回调系统必须自己登记“网络就绪后继续谁”，协程系统则把这项责任放到 Task/Future 的调度协议里。但应用开发者仍要知道等待什么时候真正发生，否则 `async` 函数里塞进重 CPU 计算同样会卡住其他请求。

## 与真正源码建立联系

本章的代码是教学实验，不是 CPython 内部源码。Python 3.13 官方资料分别定义了 [Task/Future](https://docs.python.org/3.13/library/asyncio-task.html) 和 [loop.sock_recv](https://docs.python.org/3.13/library/asyncio-eventloop.html#asyncio.loop.sock_recv) 的行为。想进一步定位实际实现，可从 `Lib/asyncio/tasks.py`、`Lib/asyncio/futures.py` 与 `Lib/asyncio/selector_events.py` 在固定版本的 CPython 仓库中查看；不要假定它们内部永远使用同样的函数结构。

## 把第 08 章回调代码重新写成一条顺序叙事

现在我们可以把先前的 `on_client_line → on_a_reply → on_b_reply` 改成如下业务流程：

```python title="概念片段：真正的 read_upstream/send_client 实现需由运行时提供"
async def handle_query(line):
    value_a = await read_upstream("A", line)
    value_b = await read_upstream("B", line)
    await send_client(value_a + value_b)
```

请注意这段仍然是**顺序等待**：B 只有在 A 的读取完成后才开始。这是合理的版本，如果 B 依赖 A 的结果，它就是正确设计。如果 A、B 独立，并且目标是重叠它们的等待，我们才需要同时安排两个任务，再收集它们的结果：

```python title="概念片段：在独立查询时才能共同推进"
async def handle_query(line):
    task_a = asyncio.create_task(read_upstream("A", line))
    task_b = asyncio.create_task(read_upstream("B", line))
    a, b = await asyncio.gather(task_a, task_b)
    await send_client(a + b)
```

这两份代码都出现 `async/await`，但是否并发取决于任务何时启动及依赖关系，而不是关键字是否存在。使用真实 `asyncio` 时还应明确任务失败、取消及子任务生命周期；这正是第 10 章要研究的内容，而不是需要在此突然解释全部异常组合语义。

协程没有消灭“谁在等待结果”这个问题，而是把状态保存的职责转移给可暂停执行过程和调度机制。假设 `value_a` 已经取得，B 仍未完成，恢复所需的局部值不再必须由开发者放进匿名回调的闭包，它可以作为协程帧/相关运行时状态保留。阅读现代异步框架源码时，把这个“暂停状态在何处”当成首要线索，比从一堆类名开始更有用。

## 从 `async` 语法退回最初的需求

我们并没有抛弃第 07、08 章：在 Linux 典型 Selector 事件循环路径里，socket 仍以非阻塞方式读取，事件循环仍靠就绪机制得知什么时候值得尝试。变化主要在用户态：过去业务开发者显式写回调与状态分支；现在协程机制和执行器共同保存、安排“从哪个暂停点恢复”。

一旦能保存大量未完成任务，下一个问题就会出现：**如果调用者放弃等待、上游迟迟不返回、或者请求已经超时，这些尚未完成的任务谁来收拾？** 下一章讨论的不是性能优化，而是任务是否还有主人。
