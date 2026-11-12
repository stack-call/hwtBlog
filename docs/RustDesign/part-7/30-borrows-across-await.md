---
title: "30 为什么跨越 await 的数据仍必须满足所有权与生命周期要求？"
sidebar_position: 30
description: "为什么跨越 await 的数据仍必须满足所有权与生命周期要求？沿问题、代码、运行机制与设计取舍逐步推导。"
slug: /rust-design/30-borrows-across-await
---

# 30 为什么跨越 await 的数据仍必须满足所有权与生命周期要求？

> **第 7 篇 · 为什么异步计算又带来新的资源约束？**  
> 本文聚焦一个因果问题：为什么跨越 await 的数据仍必须满足所有权与生命周期要求？；代码示例以稳定 Rust 的通用语法为主（适用于 2021/2024 Edition），除非明确注明为示意或故意无法编译的例子。

同步函数结束前会沿一条连续调用路径使用局部变量；异步函数可能在 `.await` 暂停，将局部状态留在 Future 中。若 Future 只借用了调用方的一部分数据，而调用方在任务尚未结束时销毁数据，就会产生悬垂引用风险。Rust 不因为是 async 就放弃原有借用规则。

## 跨越 await 的变量为什么不能随意消失

异步状态机使“数据在哪里、何时有效”变成更显式的问题。一个借用可能跨越暂停点，这意味着借用需要在 Future 被推进、直到该引用最后一次使用的期间保持有效。若 Future 被交给可独立执行的线程池，还可能需要拥有数据或满足 `'static` 等执行器所要求的边界。

区分两种方式：**借用式异步**，调用方等待 Future 完成，借用有效期通常被限定在调用方作用域内；**拥有式任务**，使用 `async move` 将资源交给 Future，方便后者被保存或提交给任务调度器。并非所有 async 都要求 `'static`，要求来自具体的任务 API。

## 暂停点改变了局部变量的生存期形状

在同步函数中，局部变量只需在函数尚未返回时有效；而异步函数可能在 `.await` 暂停，把尚需使用的局部变量保存在 Future 状态中。借用关系同样需要跨越暂停点保持有效。编译器不只分析“这行代码之前是不是有变量”，还必须分析未来某次轮询恢复后该引用是否仍有效。

一个常见误区是把 `async move` 理解成“把一切变成静态数据”。它只是按捕获语义把相关值移动进异步块，并不会延长捕获的短生命周期引用；移动一个持有 `&'a T` 的结构体仍然携带 `'a` 约束。

## async move 与普通 async 借用的区别

```rust
async fn length(text: &str) -> usize { text.len() }
async fn caller() -> usize {
    let message = String::from("order");
    length(&message).await
}
fn main() {
    let _future = caller();
}
```

这里 `message` 是 `caller` Future 中的局部资源；`length(&message).await` 的借用在关联的 Future 完成之前保持有效。由于调用和等待都发生在同一异步函数中，所有权关系可以由编译器追踪。没有执行器的例子仅构造 Future，语义分析仍可在编译期完成。

## 对照两个 Future 的所有权关系

```rust
async fn len_borrowed(s: &str) -> usize { s.len() }
async fn len_owned(s: String) -> usize { s.len() }
fn main() {
    let s = String::from("hello");
    let borrowed = len_borrowed(&s);  // Future 借用 s
    drop(borrowed);                  // 先放弃 Future
    let owned = len_owned(s);        // Future 拥有字符串
    drop(owned);                     // 丢弃 Future 时清理其持有资源
}
```

两段 Future 都没被轮询，但拥有关系已经不同：`borrowed` 不拥有 `s`；`owned` 消费了 `s`。如果把借用的 Future 放到要求 `'static` 的后台任务里，可能不满足生命周期要求；拥有型 Future 往往更容易跨越独立任务边界，但可能导致数据复制或更长的资源持有时间。

## 任务生命周期比调用作用域长时怎么办

异步借用影响 API 设计：如果返回 Future 借用了 `&self`，该 Future 的有效期通常依赖调用者；如果后台任务需要独立存活，可能改为拥有 `Arc<T>` 或移动独立数据。这里体现“资源流动决定任务边界”，而不仅是把 `fn` 改成 `async fn`。

```rust
async fn make_receipt(mut input: String) -> String {
    input.push_str(" paid");
    input
}
fn main() {
    let note = String::from("order-1");
    let task = make_receipt(note); // note 进入 Future
    // println!("{note}"); // 无法再用原绑定
    drop(task); // 还没执行，也会清理拥有的内容
}
```

这个例子强调：构造 async Future 也可能完成参数所有权转移。即使从未轮询任务，销毁 Future 时它拥有的内容仍按规则被清理。取消任务是否撤销已经发生的网络副作用则是另一个问题，不能靠 Drop 语义推导。

## 锁和 await 为何容易组合出问题

在某些执行器中，持有阻塞式 `std::sync::MutexGuard` 跨 `.await` 可能使 Future 不能满足 `Send`，更糟时可能阻塞执行器线程并导致活锁或死锁风险。即使使用异步锁，也应避免在持锁期间进行长时间网络 IO，除非业务确实需要在整个等待期间保持互斥。最好先在短作用域内提取所需数据，再释放锁并等待外部操作。

```rust
use std::sync::Mutex;
async fn read_snapshot(lock: &Mutex<u64>) -> u64 {
    let value = { *lock.lock().unwrap() }; // 守卫在块末尾释放
    value
}
```

这里没有真正的 `.await`，示意如何通过短作用域把锁守卫与后续异步工作分离。加入网络调用时，可先取快照，再在没有持锁的情况下 `.await`。注意快照之后其他任务可能修改数据，业务一致性仍要单独设计。

## 跨 await 的借用如何限制后台任务

假设一个 HTTP 请求把 `&str` 传给 async 函数，函数在 `.await` 之前或之后仍需要使用它。Future 的存活期因此与原请求缓冲区有关，不能任意交给可能比请求活得更久的后台执行器。把数据改为拥有型 `String` 可以切断这条依赖，但也会延长缓冲区的实际存活时间：它会被 Future 持有直到完成或被丢弃。

```rust
async fn describe(name: String) -> String {
    format!("order: {name}")
}
fn main() {
    let name = String::from("ABC");
    let work = describe(name); // Future 持有 name
    // println!("{name}"); // 预期编译失败：已移动
    drop(work);              // 取消这个未执行的 Future，清理 name
}
```

这个例子没有执行器，也没有实际等待点；它只演示 Future 如何取得输入所有权。加入真实 IO 时，数据会在 Future 挂起和恢复之间持续保存。后台任务要求 `'static` 时，拥有数据通常比保留请求中的短暂借用更自然。

## async API 必须给出取消安全说明

一个 `read_exact` 风格的操作、一个扣款事务和一个等待消息的 Future，其取消后能否安全重试完全不同。对于外部副作用，一次 Future Drop 不能告诉你远端是否已经提交。若在锁守卫跨越 await 时任务被暂停，还可能长期持有锁并阻塞其他工作。因此要把“持有什么资源、在哪个 await 前后改变状态、取消时会留下什么”写入 API 文档。

练习列出“任务创建、首次轮询、IO 已提交、收到回执、任务取消”五个节点；每个节点判断谁拥有订单、网络连接和幂等键。所有权只是第一层，取消安全是异步系统必须补上的第二层契约。

## 锁守卫跨 await 带来什么危险

JavaScript Promise 通常不以借用生命周期表达任务对外部变量的依赖，Go goroutine 倾向让逃逸变量由 GC 保持有效；Rust 则要求 Future 所含引用在静态规则内合法。优点是安全与控制，代价是某些跨任务借用需要更显式的拥有或共享设计。

`async move` 并不保证 Future 一定 `Send`：如果捕获了 `Rc<T>` 或跨 await 保留某种非 `Send` 状态，就可能无法在多线程执行器调度。`'static` 也不等于永不销毁，而是没有依赖短寿命引用的要求。

## 取消和 Drop 的新含义

异步任务被取消常意味着它所持有的 Future 被丢弃，已经完成的副作用不会自动回滚，未提交的资源会按对应 Drop 规则清理。若任务在扣款后、记录回执前取消，状态可能不一致；这不是借用检查器能发现的问题。可靠的异步 API 需要明确哪些阶段可取消、哪些操作要保持幂等、哪些事务必须在取消时补偿。

练习：设计一个 `upload(order: Order)` Future，列出它在“创建后立即丢弃、上传中途取消、成功返回”三种情形下谁拥有订单和网络连接；如果上传需要重试，说明是否应持有独立可克隆的请求数据。

## 为任务设计清晰的数据归属

1. 对比借用 `&String` 的 async 函数和消费 `String` 的 async 函数。
2. 让一个 async 块使用 `Rc<String>`，解释它何时不满足跨线程执行器的 Send 要求。
3. 画出取消一个 Future 后局部拥有资源和外部数据库副作用分别如何处理。

本章把借用模型延伸到暂停计算。下一章讨论更难的地址稳定性：为什么某些 Future 不允许在被固定后任意移动，以及 Pin 到底保证什么。

**相关阅读**：[async](https://rust-lang.github.io/async-book/); [ref](https://doc.rust-lang.org/reference/)。

---

[← 上一篇](29-async-state-machines.md) · [系列目录](../00-introduction.md) · [下一篇：为什么某些异步对象需要地址稳定性？](31-pin-and-address-stability.md)
