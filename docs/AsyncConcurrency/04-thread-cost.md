---
title: "异步与高并发 04：10,000 个连接，难道需要 10,000 个线程？"
sidebar_label: "四、一万个连接就要一万个线程吗"
sidebar_position: 5
description: "从线程资源开销推导线程池和有界并发的需求。"
slug: /async-concurrency/04-thread-cost
---

# 10,000 个连接，难道需要 10,000 个线程？

[上一章](./03-threads.md)用两个工作线程重叠了两个网络请求。服务增长后，如果每个连接都由一个独立线程等待，我们很快会遇到另一个问题：**等待任务几乎不计算，却依然占有线程资源**。

先不要笼统地说“线程慢”。让我们做一个受控实验，观察线程数随任务数增加。

## 先分清“连接数”和“正在处理的工作数”

上一章的两个上游请求都很快有结果，所以两个线程很自然。但现实里的长连接服务不一样：客户端可以建立连接，然后几分钟不发数据。此时“同时存在的连接”可能远多于“此刻有业务计算的请求”。如果每条连接都有一个正在 `recv` 的线程，**连接数量**就被机械地绑定到**线程数量**。

先做一组不用任何高级异步机制的数量推演：

```text
连接数 N = 2000
其中 95% 暂时没有业务数据 = 1900 条等待连接
每连接一个工作线程 → 约 2000 个工作线程
真正做业务计算的连接可能只有约 100 条
```

这里最不划算的不是某个线程做了很多计算，而是有相当多的线程**仅为了表示“我在等某个 socket”而存在**。每个线程都有执行上下文、栈空间与调度相关成本；这些成本因平台而异，不能拿某个固定值乘以 2,000 当作普适结论。

我们暂不直接跳到 `epoll`。既然问题看起来是线程数量失控，第一反应自然应当是：把线程数限制住，复用它们。这就是线程池要做的事。下面的池实验有效地解决了“不要为每个任务创建独立线程”，但我们还要观察它是否解决了“长期等待连接”的问题。

## 把原有方案扩大

为了避免在本机不受控地创建海量线程，这个实验只创建 40 个短时间等待线程，使用 `threading.active_count()` 观察活跃数量。等待代表上游请求的停顿，不是压力测试结果。

```python title="实验 04A｜任务数量与线程数量"
import threading
import time

start = threading.Event()

def wait_for_upstream():
    start.wait()                 # 一起开始
    time.sleep(0.2)             # 模拟等待上游

threads = [threading.Thread(target=wait_for_upstream) for _ in range(40)]
for t in threads:
    t.start()
print("active before release:", threading.active_count())
start.set()
for t in threads:
    t.join()
print("active after join:", threading.active_count())
```

预期：释放前活跃线程数明显增加，全部 `join` 后减少。线程需要自己的执行栈、调度元数据和运行时相关资源；具体内存大小与线程创建上限取决于 OS 配置、地址空间、栈设置与运行时，不要用固定的“每线程 X MB”作为普遍事实。

## 先尝试线程池，而不是直接跳到 epoll

一个直接改进是：不要为每个请求立刻创建新的线程，而是复用固定数目的工作线程。`ThreadPoolExecutor` 提供这一机制。

```python title="实验 04B｜线程池让活跃工作线程有上限"
import concurrent.futures
import threading
import time

names = set()
lock = threading.Lock()

def fetch(i):
    with lock:
        names.add(threading.current_thread().name)
    time.sleep(0.1)
    return i * 2

begin = time.monotonic()
with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
    results = list(pool.map(fetch, range(20)))
print("work threads used:", len(names))
print("results:", results[:4])
print("elapsed:", round(time.monotonic() - begin, 2))
```

工作线程通常不超过 5 个；20 个任务会分批完成。线程池确实避免了“每个工作任务都有独立线程”的模式，但**待执行任务依然要排队**。此外，`ThreadPoolExecutor.submit()` 自身不会提供严格的无限队列背压保证；生产系统必须另外限制任务提交速度和积压数量。

## 一个会让线程池方案暴露限制的具体输入

假设线程池只有 2 个工作线程。有三个客户端：A、B 连接后长期不发送数据，C 连接后立刻发送 `query\n`。最直接的处理器是：

```python title="教学化简：逐连接阻塞读，示意线程池工作槽位占用"
def handle_client(sock):
    line = sock.recv(1024)  # 未收到数据就可能长期等待
    if line == b"query\n":
        sock.sendall(b"OK\n")
```

线程池先调度 A、B 进入 `recv()`，于是两个工作槽位都被占住。C 即使已经有数据，也只能排队。注意这不是说 `recv` 消耗了 CPU；恰恰相反，A、B 可能几乎不占 CPU，却占有线程池的有限工作槽位。这是**资源占用和实际 CPU 使用率不是一回事**的典型例子。

你也可以把线程池扩大到 20、200。它会推迟饥饿出现的时机，却没有改变“一个等待连接占一个线程”的关系。现在才形成一个真正的新需求：**保留每条连接的等待状态，但不把每条连接固定绑定到一个阻塞中的 OS 线程。**

我们不能凭空完成这件事。此前 `recv` 的调用方式会让当前线程等待，因此首先得问：如果读不到数据，是否允许它**立刻返回**，把“以后再来”的决定交给应用？这正是下一章要做的唯一改变。

## 为什么线程池仍不是全部答案？

假设有 2,000 个客户端保持长连接，几乎不发数据。如果固定 20 个线程，每个线程都进入某个客户端的阻塞 `recv()`，其余客户端即使已经有数据，也可能长期排在后面。虽然线程数量受控，但**一个长期等待连接仍占有一个池工作槽位**。

这时我们真正缺少的是：**能否用少量线程同时了解很多 socket 的状态，而不是让每个 socket 占一个等待线程？**

```text
每连接一个线程：处理简单，但线程数量随连接数增长
固定线程池：线程数量有界，但阻塞读可能长期占住工作槽位
下一步的需求：无数据时释放执行线程，同时保留连接状态
```

## 给线程池一次公平的机会，再观察它真正失效在哪里

直接拿线程池管理 10,000 个纯计算任务，和拿它管理 10,000 条长时间空闲连接，得到的结论并不一样。前者任务如果很短，线程池可能运行得很好；后者每个处理函数在 `recv` 里等很久，使工作线程无法及时归还池中。

为了不一上来就启动成千上万条 OS 线程，我们用下面的程序只观察两个工作槽位：

```python title="实验 04C｜两个工作槽位被等待占满"
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import time

release = Event()

def slow(name):
    print(name, "occupied worker")
    release.wait()   # 模拟连接长期无数据；由主线程最终释放
    return name

def fast():
    print("C finally started")
    return "C"

with ThreadPoolExecutor(max_workers=2) as pool:
    a = pool.submit(slow, "A")
    b = pool.submit(slow, "B")
    time.sleep(0.05)  # 提高 A/B 已占用两个槽位的观察概率
    c = pool.submit(fast)
    time.sleep(0.05)
    print("C done before release?", c.done())
    release.set()
    print(a.result(), b.result(), c.result())
```

在常见执行中，`C done before release?` 是 `False`，释放 A/B 后 C 才开始。这里有两个很重要的限定：`Event.wait()` 只是在隔离环境里模拟工作槽位被长时间占住，**不是网络系统调用**；并且线程执行顺序依赖调度，0.05 秒只是提高复现概率，不构成严格同步保证。

把这个实验映射回真实服务，问题就很清楚：C 的网络数据可能早已经到达，但 C 的处理器尚未得到池中的线程。单纯把线程池从 2 扩到 20，只会使这个具体问题在更多连接出现时才暴露。我们需要的是让“socket 没数据”不等于“必须占一个工作槽位”。

不过别急着得出“只用事件循环”的结论：如果任务包含大量同步数据库库调用、文件 I/O、可能阻塞的第三方 SDK，把它们交给一个有界线程池仍然是工程上常见且合理的选择。**我们在寻找适合高连接数、长空闲等待的模型，不是在证明线程池过时。**

## C10K 是什么历史背景？

互联网服务器的发展让“同时维护大量连接”的问题受到关注。Dan Kegel 的 [The C10K Problem](https://www.kegel.com/c10k.html) 汇总了多进程、多线程和事件机制等路径。C10K 是重要的工程问题名称，不意味着每连接一线程在所有硬件或负载下都会失败，也不表示某一架构永远更优。

## 从实验得到一个必须先实现的接口能力

我们已经看到了线程池的真实限制：一条连接如果长期等在同步 `recv`，它会占着一个工作槽位。现在假设我们想让同一个工作线程轮流检查 200 个连接。最容易想到的伪代码是这样的：

```python title="概念伪代码：尚未改变 socket 模式时，这个方案会在第一条慢连接卡住"
for sock in all_connections:
    data = sock.recv(1024)
    if data:
        process(data)
```

在默认阻塞 socket 上，这段代码并不能实现我们想要的轮流处理：如果列表第一条连接没有数据，它就阻塞在第一次 `recv`，根本走不到第二条连接。我们不能只靠“写了 for 循环”就创造并发能力。

因此下一步必须有一个**比线程池更底层的接口变化**：调用 `recv` 时，如果当前没有数据，告诉调用者“现在不能读”，但不要把当前线程留在那里。应用于是能够继续尝试其他连接。

这就是为什么我们会先讲非阻塞 socket，再讲 `select/epoll`，而不是直接从线程池跳到事件循环。新的 I/O 接口性质是因果链里不可缺少的一环。

从历史角度，C10K 描述的是“如何处理约一万连接”的工程压力，但从未规定唯一正确答案。与其记“C10K 之后大家都不用线程了”，不如记住这一具体失败输入：**一小组线程被几条长期空闲连接占满，其他已经有数据的连接无法及时得到执行机会**。后续每一章都在试着消除这个具体限制。

## 反例与适用边界：不要把线程池写成反派

若每个任务都会很快完成，或者使用的库只提供阻塞 API，那么有界线程池仍然是实用方案。尤其要区分 `ThreadPoolExecutor(max_workers=...)` 只限制了**同时工作的线程数**，并不天然限制提交队列长度。我们以后讲背压时，会发现“工作数有限”与“等待中的任务总数有限”是两个独立控制目标。

本章尚未证明事件驱动一定更快。它只证明：对于大量长期空闲连接，线程池的工作槽位会被阻塞读取占住。下一章试着让同一个 `recv` 在无数据时直接返回，看看我们是不是有机会让一个线程照顾多个连接。
