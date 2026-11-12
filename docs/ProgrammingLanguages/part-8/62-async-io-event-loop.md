---
title: "第 62 章｜为什么等待 IO 不一定需要占用一个线程？"
sidebar_position: 62
description: "从订单系统的一次具体失败出发，理解为什么等待 IO 不一定需要占用一个线程？，以及由此带来的语法、运行行为与设计取舍。"
slug: /programming-languages/62-async-io-event-loop
---

# 第 62 章：为什么等待 IO 不一定需要占用一个线程？

订单服务同时等待上千个远程接口。为每个等待任务创建 OS 线程可能带来明显资源与调度成本，我们希望一个线程也能推进多个等待任务。

## 阻塞函数清楚、易调试

阻塞函数清楚、易调试。少量任务时线程池简单可用，但任务数很大时可能需要更细的等待管理。

**教学实现：**

```python
import time

def fetch(x):
    time.sleep(0.1)
    return x
```

## 等待时 CPU 其实不需要持续计算

等待时 CPU 其实不需要持续计算，关键是保存“任务尚未完成、完成后从哪里继续”。这不必与独占线程完全绑定。

**用于触发限制的代码／输入（实验场景，不伪称来自框架）：**

```text
1000 个任务都在等待 100ms 的外部事件。
若每个等待都绑定一个 OS 线程，
线程栈、上下文切换与资源调度可能成为成本。
```

**因此需要的能力。** 把等待点写成可挂起和恢复的执行状态，让事件循环在任务等待期间运行其他任务。这是协程与 async/await 的基本动机。

## 把新机制落实成代码

**改进后的教学实现：**

```python
import asyncio

async def fetch(x):
    await asyncio.sleep(0.1)  # 教学模拟异步等待
    return x

async def main():
    result = await asyncio.gather(fetch(100), fetch(250), fetch(300))
    print(result)

asyncio.run(main())
```

`await` 使任务在等待可恢复事件时交回控制，事件循环安排其他任务。`asyncio.sleep` 是模拟，不代表同步阻塞库自动变成非阻塞。

## main 创建三个协程任务

`main` 创建三个协程任务，调用 gather；每个 fetch 在 sleep 处挂起；计时器完成后任务恢复并返回原值；gather 组合结果 `[100,250,300]`。

## Rust async 函数返回 Future

Rust async 函数返回 Future，需要执行器轮询推进。

**Rust 示例（教学代码，非所引用仓库的源码节选）：**

```rust
async fn fetch(x: i64) -> i64 {
    x
}
// 仅调用 fetch(100) 得到 Future，
// 还需要执行器实际驱动它取得结果
```

Rust 的语言层提供 async/Future 语义，但具体运行时（如 Tokio）不是语言自动附带的唯一调度器；Python asyncio 则有标准库事件循环。

## 为什么 `await` 不是开一个新线程

结账需要同时等待支付和库存。协程在遇到可等待操作时可以暂停，把执行机会交还事件循环；等 IO 完成后再从暂停点继续。

```python
import asyncio
async def price():
    await asyncio.sleep(0.1)
    return 350
async def pay():
    amount = await price()
    return f'paid:{amount}'
```

`async def` 调用产生协程对象；没有实际调度或 `await`，函数体不一定已经执行。事件循环、Future/Task 负责组织运行进度。`await` 的语义是协作式暂停点，不代表计算一定被另一个系统线程并行执行。

在 CPU 密集型长循环中如果没有让出执行机会，同一个事件循环中的其他任务仍会被阻塞。要改进的是任务类型和调度方法，而不是给同步加法套上 `async` 关键字。

Rust Future 通常是惰性的，需要 executor 轮询；Java 的异步 API、Go goroutine 的调度机制则不同。比较时先追“谁安排下一次执行”。

## 对照规范和真实实现

**源码/规范阅读路线（供读者核对，并非声称已经逐行验证）：** [CPython 3.13.5 asyncio 与协程实现入口](https://github.com/python/cpython/tree/v3.13.5)。

核对 Python `async def` 返回协程对象的语义，再从 `Lib/asyncio/` 追踪 Task 如何调度恢复。切勿把 async 理解为自动使用多个 CPU 核。

## 实验：比较三个 asyncio.sleep(0.1) 用

比较三个 `asyncio.sleep(0.1)` 用 `gather` 并发等待和在循环中逐个 `await`，测总耗时。

**应当观察到（自行运行前属于预期）：** gather 的独立等待可重叠，逐个 await 则通常依次等待。真实网络 API 是否真正异步需要看其实现。

## 边界：异步对大量 IO 等待很有价值

异步对大量 IO 等待很有价值，但 CPU 密集计算仍可能阻塞事件循环，取消传播与资源生命周期也会更复杂。

**跨语言迁移时，** Python asyncio、Java 虚拟线程/异步 API、Rust Future、Go goroutine、C/C++ 协程与事件循环、Zig async 相关能力依版本而异；都要分析调度时机和阻塞边界。

**下一层问题：** 为什么异步任务还需要调度、取消和生命周期管理？

[上一章：为什么有些并发系统倾向于传递消息？](61-message-passing.md) · [下一章：为什么异步任务还需要调度、取消和生命周期管理？](63-async-task-lifecycle.md)
