---
title: "29 为什么异步函数必须保存执行状态？"
sidebar_position: 29
description: "为什么异步函数必须保存执行状态？沿问题、代码、运行机制与设计取舍逐步推导。"
slug: /rust-design/29-async-state-machines
---

# 29 为什么异步函数必须保存执行状态？

> **第 7 篇 · 为什么异步计算又带来新的资源约束？**  
> 本文聚焦一个因果问题：为什么异步函数必须保存执行状态？；代码示例以稳定 Rust 的通用语法为主（适用于 2021/2024 Edition），除非明确注明为示意或故意无法编译的例子。

网络订单处理包含大量等待：等待数据库回应、等待网络包、等待远端支付确认。如果每个等待都占住一个操作系统线程，资源成本可能很高。异步机制允许任务在尚未完成时把执行机会交还给执行器，并在条件满足时继续推进。问题是：暂停的函数必须记住什么？

## async 的第一步不是多线程，而是可暂停的计算

普通函数调用依赖调用栈与返回地址；异步函数可能在某个 `.await` 处暂停并稍后恢复，因此要保存当前位置、跨暂停点需要继续使用的局部变量及状态。Rust 的 `async` 代码会生成实现 `Future` 的状态机。Future 描述可被轮询推进的计算，并不等于创建了一条新线程。

理解异步先区分三个角色：**Future** 保存尚未完成的计算状态；**执行器** 轮询 Future；**Waker** 在条件可能改变时提示执行器重新轮询。Rust 标准库提供核心 Future 抽象，但完整的任务运行时通常由 Tokio 等外部库提供。

## 从同步函数到可以暂停的函数

普通同步函数在执行栈上按顺序推进，遇到阻塞 IO 时线程可能等待；异步函数将等待表示成 Future 的状态推进。Future 可以被轮询，在尚不能完成时返回 `Poll::Pending`，并在满足条件后由执行器再次调度。当执行跨过 `.await`，后续还需要的局部值必须作为状态保留下来，这就是“异步函数变成状态机”的直觉来源。

Future 不等于线程：你可以创建很多 Future 而不为每个 Future 创建 OS 线程。一个 Future 的计算进度通常依赖执行器轮询；单纯调用 `async fn` 得到 Future 并不自动运行其中所有逻辑。

## Future 保存什么状态

```rust
use std::future::Future;
async fn compute_total(a: u64, b: u64) -> u64 {
    a + b
}
fn make_future() -> impl Future<Output = u64> {
    compute_total(100, 200)
}
fn main() {
    let _future = make_future(); // 未被执行器轮询
}
```

调用 `async fn` 产生 Future，通常不会立即执行完函数体。`Future<Output = u64>` 说明最终完成时将产生 `u64`。示例没有运行它，因为标准库没有通用的顶层 `block_on`；这恰好提醒我们，不要把“构造 Future”和“实际驱动 Future 完成”混为一谈。

## 一个不依赖异步运行时的 Future 示例

```rust
use std::future::Future;
async fn total() -> u64 {
    println!("future body is running");
    42
}
fn inspect_type<F: Future<Output = u64>>(_f: F) {}
fn main() {
    let f = total(); // 此处创建 Future，本体尚未被轮询
    inspect_type(f); // 消费但不轮询
}
```

运行此程序通常不会打印 `future body is running`，因为 Future 没有被轮询。这说明 `async fn` 的调用语义更接近“构造一个待执行计算”，不是同步执行函数体。`inspect_type` 的 Trait Bound 只描述返回类型的能力，没有驱动计算。要让任务推进，执行器必须通过 `poll` 协议对其进行调度。

## 为什么创建 Future 通常不会执行函数体

状态机模型影响所有权：跨 `.await` 使用的变量可能成为 Future 对象的一部分；Future 被取消或销毁时，其拥有的资源按相应规则清理；对于跨线程调度，Future 捕获的内容还可能需要满足 `Send`。异步 API 因而在语义上与借用、Drop 和类型能力紧密连接。

```rust
async fn composed() -> u64 {
    let subtotal = compute_total(100, 200).await;
    subtotal + 50
}
fn main() {
    let _pending = composed();
}
```

`composed` 可看作包含“等待子 Future 完成”和“得到结果后继续加 50”等状态。`.await` 只能出现在合适的异步上下文中。真实的网络请求等待由运行时和 IO 机制驱动，不能把这里的纯计算示例误认为它天然可以提高 CPU 密集计算速度。

## 一个手写的简化状态机

```rust
use std::{future::Future, pin::Pin, task::{Context, Poll}};
struct TwoSteps { done: bool }
impl Future for TwoSteps {
    type Output = u64;
    fn poll(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<u64> {
        if !self.done {
            self.done = true;
            cx.waker().wake_by_ref();
            Poll::Pending
        } else {
            Poll::Ready(42)
        }
    }
}
```

这段是教学用途的 Future 实现，省略执行器代码。第一次轮询切换状态并通知需要再轮询；第二次轮询返回结果。注意返回 `Pending` 的 Future 不能仅靠反复空转轮询“碰运气”，正确调度依赖 Waker 代表的唤醒协议。`TwoSteps` 的 `bool` 状态与真实 async 函数保存的局部变量和分支状态具有同一基本思想，但真实转换更复杂。

## 手动执行一个 Future 的状态迁移

理解 async 的突破口不是记 `async/await` 语法，而是想象一次状态迁移。初始时 Future 只持有输入和初始状态；首次 `poll` 执行直到遇到需要等待的节点，保存必要的局部值并返回 `Pending`；外部事件发生后通过 Waker 通知执行器，再次 `poll` 从暂停点恢复；完成时返回 `Ready(value)`。执行器调度与 Future 内部计算是两个不同职责。

下面的手写 Future 在两次轮询之间保存一个布尔状态。它也提醒我们，Future 不是简单可反复调用的函数：`Ready` 后继续轮询的行为由具体 Future 契约约束，通常不应随意重轮询已经完成的 Future。

```rust
use std::{future::Future, pin::Pin, task::{Context, Poll}};
struct Immediate(u64);
impl Future for Immediate {
    type Output = u64;
    fn poll(self: Pin<&mut Self>, _cx: &mut Context<'_>) -> Poll<u64> {
        Poll::Ready(self.0)
    }
}
```

这里不需要 Waker，因为结果立即就绪。与前面的 `TwoSteps` 对照：一个任务是否可以直接产生结果，决定了它是否需要调度后续轮询。真正的网络 Future 还要把唤醒与操作系统事件源连接起来。

## 为什么 async 的取消不是撤销操作

如果一个异步支付函数已经把请求写到网络，然后在等待回执时被取消，远端仍可能成功处理付款。Future 被丢弃只意味着本地不再继续这条计算，已经发生的副作用不会自动逆转。因此支付系统需要幂等键、查询状态和补偿流程，而不只是依赖 `Drop` 清理 Future 内部内存。

练习在纸上画三个暂停点：读取网络、计算订单、写入数据库；标出每个暂停点之后仍需保存的局部值。再分别讨论取消发生在哪里会影响资源清理、哪里会影响外部业务一致性。


## 为什么手写 Future 可能无限停滞

如果一个手写 Future 在尚未完成时返回 `Poll::Pending`，却既没有登记 IO 唤醒，也没有调用 `wake` 安排重新轮询，那么执行器可能再也不会安排它执行。这不是线程死锁的典型形式，而是异步进展协议不完整。Future 需要与唤醒机制合作，不是让执行器持续忙等调用 `poll`。

相反，如果它每次返回 Pending 都无条件立即唤醒自己，却又没有任何状态进展，就可能造成高 CPU 的空转。正确的 Future 应在外部条件真正可能变化时触发唤醒，例如事件源有了新数据。理解这个细节，才能读懂 Tokio 等运行时如何把网络通知、定时器和任务队列联系起来。

## Poll 的 Ready 与 Pending 是怎样的协议

Python asyncio、JavaScript Promise、C# async/await、Go goroutine 都处理等待与并发，但底层模型不同。Rust 选择以类型化 Future、轮询协议和外部执行器为中心；它的所有权检查还会继续约束被保存的状态与引用。

异步不等于并行，不等于非阻塞 IO 自动发生，也不意味着所有 Future 都零开销。一个 Future 内部若直接调用阻塞函数，可能阻塞执行器线程。任务取消也不是自动“回滚业务事务”，必须明确资源和外部副作用的边界。

## Future 的边界与所有权

一个 Future 可以拥有其处理的数据，例如 `async move` 块捕获 `String` 并在完成时移交结果；也可以借用外部数据，但借用必须在 Future 被使用的整个相关期间有效。这会影响任务能否被放入 `'static` 的后台队列，以及执行器是否可以跨线程调度它。

练习：定义一个 `async fn price(cents: u64) -> u64`，在创建 Future 前后分别打印日志；在没有执行器的情况下尝试只丢弃 Future，观察函数体是否运行。随后阅读 `Future::poll` 的签名，解释其为何接受 `Pin<&mut Self>`。

## 从最小状态机理解编译器转换

1. 写一个 `async fn` 并观察它的返回类型与直接返回 `u64` 的区别。
2. 画出两个 `.await` 可能产生的暂停位置，以及哪些局部变量需要保存。
3. 解释为什么 `.await` 对 CPU 密集循环不自动带来多核加速。

异步让计算状态成为一个可保存、可移动、可销毁的值。下一章研究跨挂起点的借用为什么比普通同步函数更复杂，以及怎样组织异步任务的数据所有权。

**相关阅读**：[async](https://rust-lang.github.io/async-book/); [ref](https://doc.rust-lang.org/reference/)。

---

[← 上一篇](../part-6/28-memory-safety-vs-logic.md) · [系列目录](../00-introduction.md) · [下一篇：为什么跨越 await 的数据仍必须满足所有权与生命周期要求？](30-borrows-across-await.md)
