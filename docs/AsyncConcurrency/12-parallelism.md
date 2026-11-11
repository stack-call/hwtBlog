---
title: "异步与高并发 12：为什么异步网络服务遇到大量计算又变慢了？"
sidebar_label: "十二、异步服务遇到大量计算为何变慢"
sidebar_position: 13
description: "以 CPU 密集计算推导并发、并行和多进程执行。"
slug: /async-concurrency/12-parallelism
---

# 为什么异步网络服务遇到大量计算又变慢了？

前面的服务主要做一件事：**等待上游 I/O**。事件循环在一项请求等待时继续处理别的请求，所以有明显收益。现在业务增加了“对返回的大量数据做复杂计算”。突然出现一个反常现象：即使我们写了 `async def`，服务的响应仍然会变慢。

这一次瓶颈不是 I/O 等待，而是**代码持续占用 CPU**。

## 先解释为什么前面的方法突然失效

到第 11 章，我们用了非阻塞 socket、事件循环、协程和有界队列，能够较好地管理大量等待网络的任务。现在让查询服务多做一件事：上游返回价格之后，对每个结果计算一个昂贵的评分，纯 Python 算法每次要消耗 300ms CPU。

原来的等待可以这样重叠：A 等 I/O 时去推进 B；但计算不同，CPU 必须真正执行指令，才能得到结果。如果在一条事件循环线程里执行两段各 300ms 的纯 Python 计算，就不存在“等外部设备期间让出 CPU”的空隙。把函数名字改成 `async def` 不会让这些算术指令自动分布到多个核心。

这时我们才需要明确区分：**并发**是系统管理多个尚未完成的任务，**并行**是两个计算在同一物理时刻由不同执行资源推进。前面通过重叠网络等待取得的性能提升，并不自动转化为 CPU 并行能力。

## 先让事件循环被计算占住

```python title="实验 12A｜CPU 计算堵住同一个事件循环"
import asyncio
import time

def cpu_work():
    total = 0
    for i in range(6_000_000):
        total += i * i
    return total

async def heavy():
    print("heavy start")
    result = cpu_work()       # 完全同步的 CPU 运算
    print("heavy end", result % 10)

async def ping():
    print("ping begins")
    await asyncio.sleep(0)
    print("ping resumed")

async def main():
    await asyncio.gather(heavy(), ping())

start = time.monotonic()
asyncio.run(main())
print("elapsed:", round(time.monotonic() - start, 3))
```

在常规执行顺序下，`heavy` 从头到尾没有 `await`，事件循环无法在这段执行过程中切到 `ping`。通常会先看到 `heavy start → heavy end`，之后才是 `ping` 的输出。即使 `heavy` 是 `async def`，其函数体也不会因此被自动切分到其他 CPU 核心上。

## 为什么“定期 await 一下”有时有用，却不能增加计算总量

你或许会想到在计算循环中插入 `await asyncio.sleep(0)`。这能为其他任务提供被事件循环调度的机会，改善**响应性和公平性**；但如果依然只有一条执行计算的 OS 线程，它不会凭空提供第二颗 CPU 核心，更不会减少必须执行的总算术工作量。

这两个目标不同：

```text
目标一：让计算过程中其他请求别一直饿着
办法：分批计算，适当让出控制权

目标二：让两份独立计算真正同时推进
办法：使用可并行执行的计算资源
```

在标准 CPython 3.13 默认带 GIL 的构建里，多条运行普通 Python 字节码的线程通常不能在同一个解释器中持续并行执行这些字节码。这不是“操作系统不能把线程放到两个核心”，而是具体解释器的执行约束。释放 GIL 的原生扩展、独立进程以及无 GIL 构建等情形又有不同边界。

所以本章讨论进程池，是因为它能在常见 CPython 版本上为独立的 CPU 工作使用多个进程与多个 CPU 核心；不是说所有 CPU 任务永远都应该用进程池。

## 我们缺少的是 CPU 并行，而不是更多 await

如果目标是充分利用多个核心执行耗 CPU 的工作，那么一个可行方案是把计算交给另一个**进程**。多个进程的 Python 字节码可以由不同 CPU 核心分别执行；代价是进程启动、跨进程通信以及数据复制/序列化。

```python title="实验 12B｜使用进程池提交两个 CPU 任务（保存为脚本后运行）"
import concurrent.futures
import os
import time

def cpu_work(n):
    total = 0
    for i in range(n):
        total += i * i
    return total

def main():
    n = 4_000_000
    start = time.monotonic()
    sequential = [cpu_work(n), cpu_work(n)]
    print("sequential:", round(time.monotonic() - start, 3))

    start = time.monotonic()
    with concurrent.futures.ProcessPoolExecutor(max_workers=2) as pool:
        parallel = list(pool.map(cpu_work, [n, n]))
    print("process pool:", round(time.monotonic() - start, 3))
    assert sequential == parallel
    print("CPU cores reported:", os.cpu_count())

if __name__ == "__main__":
    main()
```

务必把它**保存为脚本运行**，不要直接在交互式 REPL 里运行：进程池需要能找到可导入的顶层函数。进程池未必比顺序版快：任务太短、CPU 配额有限或启动/通信成本较大时，可能更慢。这里的实验目标是比较执行模型，而不是保证获得 2 倍加速。

## 判断性能时别只看总耗时，要看并发的瓶颈在哪

设想两个需要各 300ms CPU 的任务：

- 只有一个核心时，两者即便被抢占式交替运行，总工作量也约是 600ms 核心计算时间，加上调度开销。
- 有两个可用核心，且使用可真正并行的执行实体，两份独立计算理论上可能重叠；但进程创建、任务序列化和结果传输也要时间。
- 任务特别小（例如每个 1ms），跨进程传递数据的开销可能盖过并行收益。

因此“并行可能快”需要一个前提：工作可拆分、资源确实可用，且调度/通信开销可接受。不要从一次演示计时推导出固定的倍数。CPU 密集型程序还会受到内存带宽、缓存、负载、系统配额等限制。

完成这个实验后，新的正确性问题随之而来：当多个执行实体真的可能同时工作时，如果它们修改同一份计数、缓存或连接状态，最终结果还一定正确吗？下一章将不再讨论速度，而是讨论**共享状态的更新为什么会丢失**。

## 并发与并行现在有了可见区别

- 前面的两次网络读取是**并发**：两个任务在同一时间段推进，主要收益是等待重叠。
- 本章的进程池可以实现**并行计算**：当系统提供足够 CPU 资源时，两个进程的指令可以在不同核心上同时执行。

二者并非互斥；一个多线程服务器可能同时具有并发和并行。反过来，单线程事件循环能有很多并发 I/O 任务，却不意味着 CPU 密集型回调自动并行。

## `await` 不会自动把 CPU 工作移走：用一个定时器观察

继续沿用数据查询服务。现在假设一个请求需要做 400ms 的纯 CPU 计算；同时另一个客户端只想等待 50ms 后得到一个“服务还活着”的响应。如果把计算直接放在事件循环里，后者即使只等待了 50ms 的定时器，输出也可能被前者拖延。

```python title="实验 12C｜CPU 任务拖慢无关协程（独立运行）"
import asyncio, time

def burn_cpu(duration):
    deadline = time.perf_counter() + duration
    while time.perf_counter() < deadline:
        pass

async def cpu_request():
    print("CPU begin")
    burn_cpu(0.2)
    print("CPU end")

async def heartbeat():
    start = time.perf_counter()
    await asyncio.sleep(0.05)
    print("heartbeat elapsed:", round(time.perf_counter()-start, 3))

async def main():
    await asyncio.gather(heartbeat(), cpu_request())

asyncio.run(main())
```

在默认调度中，心跳很可能明显晚于 50ms 才打印。这不是 `asyncio.sleep` 的计时器失准，而是事件循环线程在执行同步 CPU 循环期间没有机会恢复心跳任务。你可以把 `burn_cpu(0.2)` 改成分成多块、每块之间显式让出控制权，观察心跳延迟变化；但总体需要的计算指令不会因为多了 `await` 就消失。

如果将计算提交到进程池，需要考虑返回值的序列化及进程间通信。把一个数据量庞大的对象发到另一个进程，通信成本甚至可能盖过计算并行收益。所以合理选择应当从**任务规模、是否可独立拆分、可用 CPU 数量和传输成本**出发，而不是“凡是计算都要 multiprocessing”。

这章最后还要警惕一个边界：不同 Python 实现、CPython 的不同构建方式，以及部分释放 GIL 的 C 扩展，会改变线程的并行能力。我们在这里讨论的是明确的 Python 3.13 典型 GIL 构建里的普通 Python 计算，不把它推广到所有语言和所有原生库。

## 为什么不直接用 Python 线程计算？

标准 CPython 构建通常存在 GIL，对**执行 Python 字节码**的多个线程形成限制。因此普通的纯 Python CPU 密集工作往往不能仅靠 `ThreadPoolExecutor` 就实现理想的多核加速。但某些 C 扩展、I/O 操作会释放 GIL；CPython 3.13 还引入了可选的 free-threaded 构建，不能把“Python 线程永远不能并行”作为无条件事实。

从设计上看，CPU 任务下沉到进程池、原生计算库或支持并行执行的运行时，是不同的技术选择；不是“async 失败了”。参考 [concurrent.futures](https://docs.python.org/3.13/library/concurrent.futures.html)、[Python free-threading HOWTO](https://docs.python.org/3.13/howto/free-threading-python.html)。

## 什么情况下“多线程”依然是一个好答案？

本章开始讨论 CPU 并行后，常见误解会反过来出现：既然标准 CPython 3.13 的 GIL 会限制普通 Python 字节码的多线程并行，那么是不是多线程就没有用？当然不是。第 03 章两条线程重叠网络等待的收益仍然成立，很多原生扩展可能在耗时操作中释放 GIL，线程之间也可以自然共享进程地址空间，而不需要跨进程序列化大量数据。

合理选择应当按照瓶颈展开：

| 任务特征 | 首先需要回答的问题 | 常见候选方案 |
|---|---|---|
| 大量独立网络等待 | 能否避免一连接占一线程？ | 异步 I/O、轻量线程、适量线程池 |
| 少量阻塞第三方 SDK | 可以隔离到工作线程吗？ | 线程池 |
| 独立的大计算任务 | 多核执行收益能覆盖数据传输吗？ | 进程池、合适的原生并行库 |
| 大量计算但需要快速响应其他请求 | 如何避免事件循环被占住？ | 计算卸载、拆分计算、专用工作服务 |

表里没有固定赢家，因为语言运行时、任务粒度、输入规模和硬件会改变具体结果。对程序员而言，最危险的不是选错了某一个 API，而是在尚未确认瓶颈时就把所有慢都解释成“需要异步”。

这一章还没有触及共享状态，所以即使两个进程各自计算正确，也不保证同时修改同一个外部计数时结果正确。只有当我们在下一章明确引入共享计数与相互覆盖的写入时，锁和原子操作才有真正出现的理由。

## 自测

请用一句话区分：I/O 等待重叠为什么能让单线程事件循环同时服务多个请求；两个纯计算任务为什么不能仅凭 `await` 在一个核心上加速完成。然后给出一个你宁可使用线程池而非进程池的例子，以及一个可能需要进程池的例子。能解释权衡，比背“CPU 密集用多进程”更可靠。
