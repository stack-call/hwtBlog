---
title: "32 为什么异步任务需要执行器与调度机制？"
sidebar_position: 32
description: "为什么异步任务需要执行器与调度机制？沿问题、代码、运行机制与设计取舍逐步推导。"
slug: /rust-design/32-executors-and-scheduling
---

# 32 为什么异步任务需要执行器与调度机制？

> **第 7 篇 · 为什么异步计算又带来新的资源约束？**  
> 本文聚焦一个因果问题：为什么异步任务需要执行器与调度机制？；代码示例以稳定 Rust 的通用语法为主（适用于 2021/2024 Edition），除非明确注明为示意或故意无法编译的例子。

仅仅创建一个 Future，并不会自动发起网络请求或让计算向前推进。Future 是一种可被轮询的计算对象，需要外部执行器调度；当 IO 尚未准备好时，任务应避免反复忙等，而在能够取得进展时收到唤醒。

## Future 为什么不会自动推进

`Future::poll` 可能返回 `Ready(output)`，表示任务完成；也可能返回 `Pending`，表示暂时不能完成。后一种情况需要通过 `Waker` 在将来某条件改变时提醒执行器。执行器安排多个 Future 的推进顺序，可以是单线程或多线程，但语言本身不强制某一种调度策略。

区分三层：Future 描述状态和推进协议；Executor 决定何时轮询；I/O 驱动部分负责监听网络/计时等事件。取消通常表现为 Future 被丢弃，不自动撤回已发生的数据库写入。结构化并发、背压与超时策略属于更高层任务治理问题。

## 谁负责在 Pending 后再来一次

Future 的 `poll` 在尚不能产生结果时返回 `Pending`，并通过 `Context` 中的 Waker 安排适当时机再次被轮询。一个网络 Future 可能把兴趣注册给操作系统事件接口：当套接字变为可读，事件处理方触发对应 Waker；执行器收到通知，把任务放回可运行队列，再对 Future 进行 `poll`。这条路径说明 async 并不等于“所有任务都在一个神奇的后台线程自动跑”。

`Poll::Pending` 不是“sleep 10ms 后再试”的承诺，执行器也不应该无意义地忙等轮询。正确的 Waker 协议使任务能够在有进展可能时被重新调度，减少空转 CPU 成本。

## poll 和 Waker 的最小协议

```rust
use std::future::{ready, Future};
use std::task::{Context, Poll};
use std::pin::Pin;

struct Immediate(u64);
impl Future for Immediate {
    type Output = u64;
    fn poll(self: Pin<&mut Self>, _cx: &mut Context<'_>) -> Poll<u64> {
        Poll::Ready(self.get_mut().0)
    }
}
fn main() {
    let _a = Immediate(5);
    let _b = ready(9);
}
```

这个最小 Future 不需要异步等待，一轮 poll 就能返回 `Ready`。真实 Future 在 `Pending` 时必须遵守正确注册唤醒的契约，否则执行器可能永远不会重新推进它。示例没有构造 `Context`，因为正确的 Waker 不是可以随意伪造的细节。

## 用手写 Future 观察 wake 的职责

```rust
use std::{future::Future, pin::Pin, task::{Context, Poll}};
struct Once { first: bool }
impl Future for Once {
    type Output = &'static str;
    fn poll(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Self::Output> {
        if self.first {
            self.first = false;
            cx.waker().wake_by_ref();
            Poll::Pending
        } else {
            Poll::Ready("ok")
        }
    }
}
```

这段完整定义了 Future，但没有创建执行器，故不会自行输出结果。第一次轮询后它主动请求再被调度，第二次就绪。真实 IO Future 不会无条件在第一次轮询就唤醒，而是根据事件源的状态安排后续进展。你可以将它与 `Future::poll(self: Pin<&mut Self>, cx: &mut Context<'_>)` 的签名逐项对照：为什么需要可变状态，为什么需要 Pin，为什么需要 Waker？

## 执行器与事件源各自负责什么

执行器的存在影响 API 设计：函数可能返回 Future 而不是直接结果；任务需要明确关闭和取消策略；多线程执行器往往对任务施加 `Send` 要求；并发请求数需要背压上限。应用层不能把无界 spawn 当作没有成本的免费并发。

```rust
async fn step(v: u64) -> u64 { v + 1 }
async fn pipeline() -> u64 {
    let first = step(10).await;
    step(first).await
}
fn main() {
    let task = pipeline();
    drop(task); // 未轮询，直接取消未来的计算
}
```

这里 Future 还未被执行，`drop(task)` 便不再推进它。若 task 曾经执行到数据库写入之后再被取消，外部副作用可能已经发生。因此取消安全和重试语义要由应用设计，不能仅依赖 Rust 资源清理能力。

## 运行时不只负责轮询

真实执行器需要任务队列、唤醒通知、调度策略和关闭流程；异步运行时还可能封装定时器、非阻塞网络 IO、阻塞任务池。Tokio 等库提供这些能力，但 Rust 语言本身并未规定某一个统一执行器。因此 `async fn` 可以定义待执行计算，却不能仅凭语法决定任务如何被调度到线程。

需要认真设计任务取消：当 `JoinHandle` 被丢弃时，某些运行时可能使任务继续在后台执行，另一些取消 API 则会丢弃 Future。不同运行时和 API 契约不同，不应一概而论。取消 Future 也不回滚已完成的网络副作用。

## Waker 不是让 Future 直接执行的按钮

一个 Future 返回 `Pending` 时，它把继续推进的机会交给执行器。Waker 通常负责通知执行器“这个任务应该再次获得轮询机会”，并不保证调用 `wake` 的瞬间就同步执行 Future。真实运行时会管理任务队列、事件注册、定时器、IO 驱动和工作线程；Wake 只是其中的一个协调协议。

观察一个概念上的事件顺序：

```text
任务 A: poll() -> Pending
执行器: 暂时调度任务 B
网络事件: A 的 socket 可读，触发 wake(A)
执行器: 将 A 放入可运行队列
任务 A: poll() -> Ready(response)
```

这段是事件时序示意，不是 Rust 源代码。它说明为什么 Future 不必在等待网络期间占住一个专属线程；也说明如果 Future 返回 `Pending` 却没有正确安排唤醒，它可能永远没有下一次进展。

## 从任务生命周期反推 Executor 的责任

执行器除了轮询，还应定义任务何时结束、如何取消、错误如何上报、子任务是否可能在父任务结束后继续运行。一个后台任务保存整个订单可能让内存长时间占用；一个无界任务队列可能在流量高峰把内存耗尽。语言层的 `async` 并没有强制统一的任务结构，所以运行时库和应用架构必须补上这些约束。

练习写一张任务生命周期表：`Created → Runnable → Pending → Woken → Ready`，另加 `Cancelled` 分支；每一次状态变化列出谁触发、是否有资源清理、是否可能存在已发出的外部 IO。最后解释为什么“返回 Pending”与“线程被阻塞”不是同一个概念。

## 取消、任务队列与背压

JavaScript 运行时将事件循环与 Promise 生态整合得更直接；Python asyncio 通常使用事件循环；Go goroutine 的调度由运行时负责。Rust 提供较小的核心 Future 协议，允许不同执行器生态，但也需要程序员明确选择和理解运行时。

`Pending` 不是建议执行器忙等；Future 必须安排适当唤醒。异步不保证任务会公平执行，也不自动处理阻塞代码、无限队列、取消后副作用或跨系统幂等性。面对这些问题应设计超时、背压、任务监督和状态恢复。

## 背压和结构化生命周期

若生产速度高于消费速度，任务队列和 Channel 的容量就会不断膨胀。真实系统要设置并发上限、队列容量、超时和失败策略。结构化并发希望父任务明确管理子任务生命周期，使错误传播与取消边界更清楚；但 Rust 的语言层 async 并不强制所有框架采用同一种结构化并发模型。

练习写出一个“读取订单 → 查询库存 → 写数据库”的异步状态流程图，标出三次 IO 等待，以及每个等待点保存了哪些局部数据；再列出取消发生在不同阶段时需要哪些补偿或幂等操作。

## 从简化调度器走向真实运行时

1. 解释 `Future`、`Executor`、`Waker` 之间的职责边界，画出 `Pending -> wake -> poll` 流程。
2. 对订单网络请求设置并发上限，说明无界任务增长的资源风险。
3. 设计“取消发生在扣款后、写订单前”的恢复策略，说明 Drop 为什么不足以保证业务原子性。

第七篇完成了从普通函数到异步状态机的推导。下一篇进入 Rust 的安全边界：既然编译器能拒绝很多危险操作，为什么语言仍必须保留 unsafe？

**相关阅读**：[async](https://rust-lang.github.io/async-book/); [std](https://doc.rust-lang.org/std/)。

---

[← 上一篇](31-pin-and-address-stability.md) · [系列目录](../00-introduction.md) · [下一篇：为什么编译器无法静态证明所有安全操作？](../part-8/33-limits-of-static-proof.md)
