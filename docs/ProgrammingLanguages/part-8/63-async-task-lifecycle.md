---
title: "第 63 章｜为什么异步任务还需要调度、取消和生命周期管理？"
sidebar_position: 63
description: "从订单系统的一次具体失败出发，理解为什么异步任务还需要调度、取消和生命周期管理？，以及由此带来的语法、运行行为与设计取舍。"
slug: /programming-languages/63-async-task-lifecycle
---

# 第 63 章：为什么异步任务还需要调度、取消和生命周期管理？

用户取消订单了，但后台税率请求仍在执行。程序虽然能并发等待，却没有明确谁负责取消、回收和收集未完成任务。

## 单个异步函数的 await 关系很清楚

单个异步函数的 `await` 关系很清楚，完成后返回即可。但真实服务会同时启动许多子任务，用户可能提前离开。

**教学实现：**

```python
import asyncio
async def work():
    await asyncio.sleep(10)
    return 100
```

## 请求完成不代表后台任务完成

请求完成不代表后台任务完成。孤立任务可能继续消耗资源、异常无人收集，或在运行时关闭时被取消。需要明确子任务归属于谁。

**用于触发限制的代码／输入（实验场景，不伪称来自框架）：**

```python
async def main():
    asyncio.create_task(work())  # 没有保存任务和定义生命周期
    return "request finished"
```

**因此需要的能力。** 让任务在一个有边界的生命周期内创建、等待、取消和收集结果。结构化并发关注“父任务结束时子任务应如何处理”。

## 把新机制落实成代码

**改进后的教学实现：**

```python
import asyncio

async def work(x):
    await asyncio.sleep(0.1)
    return x

async def main():
    async with asyncio.TaskGroup() as group:
        a = group.create_task(work(100))
        b = group.create_task(work(250))
    print(a.result(), b.result())

asyncio.run(main())
```

`TaskGroup` 将子任务限制在明确作用域内，正常离开时子任务已经完成；出现失败时还有清楚的取消与异常组处理规则。它并不自动解决所有外部任务幂等问题。

## 进入 TaskGroup 创建两个工作任务

进入 TaskGroup 创建两个工作任务；组在作用域退出前等待完成；完成后读取各自返回值 100、250。若一个任务失败，组的退出语义会处理其他任务的取消并传播异常。

## Java 的结构化任务作用域相关 API 随 JDK 版本演进

Java 的结构化任务作用域相关 API 随 JDK 版本演进，不能简单假定所有版本都稳定可用。

**Go 示例（教学代码，非所引用仓库的源码节选）：**

```go
ctx, cancel := context.WithCancel(context.Background())
defer cancel()
// 启动的 goroutine 必须显式检查 ctx.Done()
// 并由调用方等待或收集其结束状态
```

Go 的 Context 能传递取消信号，但不自动 join goroutine，也不能强迫忽略 Context 的操作立即停下。

## 请求已经取消，后台扣款还在继续怎么办

用户关闭支付页面，服务端请求超时，但已经启动的后台任务可能继续调用外部网关。如果任务失去管理者，系统可能泄漏资源或在用户以为取消后仍执行副作用。

```python
import asyncio
async def worker():
    try:
        await asyncio.sleep(10)
    finally:
        print('cleanup')
```

`task.cancel()` 通常通过向协程注入取消请求使其在合适的暂停点响应；这并不等于强制撤销已经提交给外部系统的扣款。`finally` 可用于释放本地资源，但外部事务仍需业务状态协调。

结构化并发要求子任务生命周期被其所属作用域管理，减少“启动后没人等待也没人取消”的孤儿任务。然而不同语言运行时对取消传播、子任务错误聚合和结束等待有不同规则。

实验：创建两个任务，让一个抛错，观察另一个是否被取消、谁等待清理结束。实际扣款不要用模拟 `sleep` 误当成事务回滚。

## 对照规范和真实实现

**源码/规范阅读路线（供读者核对，并非声称已经逐行验证）：** [CPython v3.13.5 `Lib/asyncio/taskgroups.py`](https://github.com/python/cpython/tree/v3.13.5)。

核对 TaskGroup 正常退出、异常传播与取消路径；固定版本链接在 CPython v3.13.5 仓库下。先跟随两个成功任务的路径，再研究一个失败分支。

## 实验：让第二个 work 抛 ValueError

让第二个 `work` 抛 ValueError，观察 TaskGroup 的异常传播，并记录第一个任务可能被取消的情况。

**应当观察到（自行运行前属于预期）：** 任务组不会默默忽略子任务失败；具体异常组展示与取消时机依运行情况不同，实验应记录实际版本。

## 边界：结构化并发降低孤儿任务风险

结构化并发降低孤儿任务风险，但取消并不等于外部副作用回滚。限流、超时和背压仍需要应用协议。

**跨语言迁移时，** Python TaskGroup、Go context+errgroup 模式、Java 结构化作用域、Rust task scope/JoinSet 等都在管理任务生命周期，具体标准化程度不同。

**下一层问题：** 为什么代码复用不等于设计良好？

[上一章：为什么等待 IO 不一定需要占用一个线程？](62-async-io-event-loop.md) · [下一章：为什么代码复用不等于设计良好？](../part-9/64-reuse-is-not-design.md)
