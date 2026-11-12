---
title: "第 57 章｜为什么顺序处理无法充分利用等待时间或多核？"
sidebar_position: 57
description: "从订单系统的一次具体失败出发，理解为什么顺序处理无法充分利用等待时间或多核？，以及由此带来的语法、运行行为与设计取舍。"
slug: /programming-languages/57-concurrency-motivation
---

# 第 57 章：为什么顺序处理无法充分利用等待时间或多核？

一千笔订单需要分别请求远程税率服务。每笔大部分时间都在等待网络，而不是在 CPU 上计算。顺序处理的吞吐量开始受到等待累积限制。

## 顺序逻辑很容易推理

顺序逻辑很容易推理：第一笔完成后才处理第二笔。只有几笔订单且无需高吞吐时，这种简单性很珍贵。

**教学实现：**

```python
import time

def fetch_rate(order):
    time.sleep(0.1)  # 教学模拟等待，不是真实网络
    return order * 0.1

print([fetch_rate(x) for x in [100, 250, 300]])
```

## 三笔独立等待各 0.1 秒

三笔独立等待各 0.1 秒，顺序累计约 0.3 秒。输入变成千笔时，等待会线性叠加。我们希望等待期间安排其他可独立推进的任务。

**用于触发限制的代码／输入（实验场景，不伪称来自框架）：**

```python
start = time.perf_counter()
for amount in [100, 250, 300]:
    fetch_rate(amount)
print(round(time.perf_counter() - start, 1))  # 约 0.3 秒
```

**因此需要的能力。** 区分并发（多个任务在时间上交错推进）与并行（同一时刻实际在多个处理单元执行）。对独立 IO 等待，先用并发就可能改善总完成时间。

## 把新机制落实成代码

**改进后的教学实现：**

```python
from concurrent.futures import ThreadPoolExecutor
with ThreadPoolExecutor(max_workers=3) as pool:
    results = list(pool.map(fetch_rate, [100, 250, 300]))
print(results)
```

线程池允许多个等待过程重叠。这里的 `sleep` 仅模拟等待，不能证明线程会加速 Python 纯计算；线程调度成本与平台行为仍存在。

## 三个任务被提交

三个任务被提交，线程池允许它们各自进入等待；第一个任务等待时其他任务能够推进；完成后 `map` 按输入顺序提供结果。总时间可能接近最长单次等待加调度开销，而非三次等待之和。

## Go 用 goroutine 表示可以并发运行的轻量执行任务

Go 用 goroutine 表示可以并发运行的轻量执行任务。

**Go 示例（教学代码，非所引用仓库的源码节选）：**

```go
var wg sync.WaitGroup
for _, x := range []int64{100, 250, 300} {
    wg.Add(1)
    go func(x int64) {
        defer wg.Done()
        time.Sleep(100 * time.Millisecond)
        _ = x
    }(x)
}
wg.Wait()
```

goroutine 是 Go 运行时管理的任务，不等于每个 goroutine 都独占 OS 线程。例子需导入 sync 和 time。

## 两种等待与真正同时计算不是一回事

订单任务 A 需要等待支付网关 200ms，任务 B 需要等待库存服务 200ms。顺序等待的总时间通常接近两次等待之和；若两次请求彼此独立，可以让等待重叠。这里先出现的需求是**并发组织等待**，不一定是多核并行计算。

```python
import asyncio
async def fetch(label):
    await asyncio.sleep(0.2)
    return label

async def main():
    return await asyncio.gather(fetch('pay'), fetch('stock'))
```

这个实验用 `sleep` 模拟等待，不是真实网络测试；可以演示任务交错，但不能证明远端服务延迟或吞吐的实际提升。CPU 密集型订单加密计算则可能需要不同的并行执行手段。

区分并发和并行的方式：前者讨论多个任务在时间上如何推进，后者讨论多个计算是否真的同时占用不同执行资源。线程、协程、进程、goroutine 都是组织任务的工具，不是同义词。

## 对照规范和真实实现

**源码/规范阅读路线（供读者核对，并非声称已经逐行验证）：** [Go 1.24.5 `src/runtime/` 调度实现入口](https://github.com/golang/go/tree/go1.24.5)。

先观察 Go 源码 runtime 中创建与调度 goroutine 的结构，再研究是否运行在多个 OS 线程上。不要凭“go 关键字”断言任务一定同时在多个 CPU 核上运行。

## 实验：将模拟等待改为 time.sleep(0.02)

将模拟等待改为 `time.sleep(0.02)`，分别测顺序与线程池处理 5、20、100 项的总耗时，记录工作线程数。

**应当观察到（自行运行前属于预期）：** 独立等待任务可能因重叠而缩短总耗时；线程数过多会带来调度与资源成本，耗时不是简单的 1/线程数。

## 边界：并发提高吞吐潜力

并发提高吞吐潜力，但增加状态共享、取消、错误传播与资源限制问题。只有任务独立且有等待或并行需求时，复杂度才有实际收益。

**跨语言迁移时，** Python thread/asyncio、Java threads/virtual threads、Go goroutine、Rust async/thread、C/C++ thread、Zig thread 都能组织并发，但调度与内存安全保障不同。

**下一层问题：** 为什么多个执行流程不能随意修改同一个值？

[上一章：为什么与外部代码交互需要格外小心？](../part-7/56-ffi-boundaries.md) · [下一章：为什么多个执行流程不能随意修改同一个值？](58-data-races.md)
