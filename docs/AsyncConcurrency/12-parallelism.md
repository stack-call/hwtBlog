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

## 并发与并行现在有了可见区别

- 前面的两次网络读取是**并发**：两个任务在同一时间段推进，主要收益是等待重叠。
- 本章的进程池可以实现**并行计算**：当系统提供足够 CPU 资源时，两个进程的指令可以在不同核心上同时执行。

二者并非互斥；一个多线程服务器可能同时具有并发和并行。反过来，单线程事件循环能有很多并发 I/O 任务，却不意味着 CPU 密集型回调自动并行。

## 为什么不直接用 Python 线程计算？

标准 CPython 构建通常存在 GIL，对**执行 Python 字节码**的多个线程形成限制。因此普通的纯 Python CPU 密集工作往往不能仅靠 `ThreadPoolExecutor` 就实现理想的多核加速。但某些 C 扩展、I/O 操作会释放 GIL；CPython 3.13 还引入了可选的 free-threaded 构建，不能把“Python 线程永远不能并行”作为无条件事实。

从设计上看，CPU 任务下沉到进程池、原生计算库或支持并行执行的运行时，是不同的技术选择；不是“async 失败了”。参考 [concurrent.futures](https://docs.python.org/3.13/library/concurrent.futures.html)、[Python free-threading HOWTO](https://docs.python.org/3.13/howto/free-threading-python.html)。

**下一步的问题**：当我们真的让多个执行者同时处理请求，它们如果共同修改一份全局统计数据，结果会不会总是正确？去[第 13 章](./13-races.md)。
