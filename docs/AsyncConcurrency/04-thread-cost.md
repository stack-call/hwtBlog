---
title: "异步与高并发 04：10,000 个连接，难道需要 10,000 个线程？"
sidebar_label: "四、一万个连接就要一万个线程吗"
sidebar_position: 5
description: "从线程资源开销推导线程池和有界并发的需求。"
slug: /async-concurrency/04-thread-cost
---

# 10,000 个连接，难道需要 10,000 个线程？

[上一章](03-threads.md)用两个工作线程重叠了两个网络请求。服务增长后，如果每个连接都由一个独立线程等待，我们很快会遇到另一个问题：**等待任务几乎不计算，却依然占有线程资源**。

先不要笼统地说“线程慢”。让我们做一个受控实验，观察线程数随任务数增加。

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

## 为什么线程池仍不是全部答案？

假设有 2,000 个客户端保持长连接，几乎不发数据。如果固定 20 个线程，每个线程都进入某个客户端的阻塞 `recv()`，其余客户端即使已经有数据，也可能长期排在后面。虽然线程数量受控，但**一个长期等待连接仍占有一个池工作槽位**。

这时我们真正缺少的是：**能否用少量线程同时了解很多 socket 的状态，而不是让每个 socket 占一个等待线程？**

```text
每连接一个线程：处理简单，但线程数量随连接数增长
固定线程池：线程数量有界，但阻塞读可能长期占住工作槽位
下一步的需求：无数据时释放执行线程，同时保留连接状态
```

## C10K 是什么历史背景？

互联网服务器的发展让“同时维护大量连接”的问题受到关注。Dan Kegel 的 [The C10K Problem](https://www.kegel.com/c10k.html) 汇总了多进程、多线程和事件机制等路径。C10K 是重要的工程问题名称，不意味着每连接一线程在所有硬件或负载下都会失败，也不表示某一架构永远更优。

别忘了我们还没证明非阻塞 socket 能帮忙；下一章只改一件事：**当 socket 没有数据时，让读取立即返回**。从这个实验中自己观察 [同步与非阻塞](./05-nonblocking.md) 如何并存。
