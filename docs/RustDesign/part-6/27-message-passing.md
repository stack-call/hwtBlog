---
title: "27 为什么消息传递适合表达并发任务之间的协作？"
sidebar_position: 27
description: "为什么消息传递适合表达并发任务之间的协作？沿问题、代码、运行机制与设计取舍逐步推导。"
slug: /rust-design/27-message-passing
---

# 27 为什么消息传递适合表达并发任务之间的协作？

> **第 6 篇 · 为什么所有权规则会影响并发程序？**  
> 本文聚焦一个因果问题：为什么消息传递适合表达并发任务之间的协作？；代码示例以稳定 Rust 的通用语法为主（适用于 2021/2024 Edition），除非明确注明为示意或故意无法编译的例子。

订单服务可以把“请处理订单 17”的消息放进队列，由工作线程独立处理。相较多个线程直接修改同一订单对象，这种模式能够把可变状态集中在接收方，减少共享状态的范围。Rust 的消息通道可直接体现所有权转移。

## 消息传递把共享状态转化为所有权转交

消息传递并不是无锁魔法，底层通道也需要正确同步；它的优势在于业务层可以把任务当作独立拥有的值传递。生产者发送后通常不再持有原消息，消费者获得处理责任。如此构建的模块边界更清晰，也容易进行重试和负载控制。

先区分**发送的内容**与**通道的协调**。`Sender<T>`/`Receiver<T>` 协助线程间交接 `T`；若要把某个拥有 `String` 的订单发出去，`send(order)` 通常会移动订单。通道并不保证消息被成功处理、持久化或恰好一次执行，这些是更高层协议问题。

## 将一份订单交给负责处理它的任务

与多个线程同时修改同一订单不同，我们也可以让生产者构造订单，再把它通过 Channel 移交给消费者线程。这样数据不必长期留在共享可变容器里，消费者拥有收到的订单，处理时不需要围绕每个字段加锁。消息传递的价值不是“完全没有同步”，而是把同步边界集中在发送与接收上。

```rust
use std::sync::mpsc;
use std::thread;
fn main() {
    let (tx, rx) = mpsc::channel::<String>();
    let worker = thread::spawn(move || {
        while let Ok(order) = rx.recv() {
            println!("handled {order}");
        }
    });
    tx.send(String::from("order-42")).unwrap();
    drop(tx); // 关闭最后一个发送端，让接收循环结束
    worker.join().unwrap();
}
```

发送 `String` 后，发送端原绑定被移动，接收端取得该值的所有权。这与所有权系统天然契合。若通道两端之一被关闭，`send` 和 `recv` 使用 `Result` 明确表达失败，调用者不能假设消息必然送达。

## Channel 的发送端和接收端各负何责

```rust
use std::sync::mpsc;
use std::thread;
fn main() {
    let (tx, rx) = mpsc::channel::<String>();
    let handle = thread::spawn(move || {
        let order = rx.recv().unwrap();
        format!("processed {order}")
    });
    tx.send(String::from("#17")).unwrap();
    assert_eq!(handle.join().unwrap(), "processed #17");
}
```

消息是拥有所有权的 `String`，从发送端进入通道再由接收端取得。发送端可以继续使用 `tx`，但不能再次使用已经移动进 `send` 的那个 `String` 绑定。`recv` 的 `Result` 还体现另一种失败：发送端都离开后，接收可能不再得到新消息。

## 共享内存与消息传递是两种组织方式

共享内存方案通常把状态保存在一个长期对象中，多个任务通过锁访问；消息传递方案倾向于把状态集中在一个处理者内部，由其他任务发送请求。前者可降低消息复制或调度开销，适合需要低延迟共享读写的情况；后者使状态拥有者更清晰，适合业务流水线、批量处理和有独立职责的后台服务。

不过消息传递仍可能导致逻辑错误：两笔订单消息顺序不确定、处理失败后是否重试、重复消息是否幂等、消费者处理速度是否落后于生产者。类型安全并不会自动实现“恰好处理一次”的分布式语义。

## 为什么单消费者模型容易推理

消息传递会影响系统架构：每个 worker 拥有哪些数据、哪些消息可以重放、状态变更由谁串行执行，都成为显式设计。对于 CPU 密集任务可以通过多 worker 并行处理；对于需要顺序处理同一客户订单的场景，可能需要按键分区，避免跨 worker 的业务竞争。

```rust
use std::sync::mpsc;
fn main() {
    let (tx, rx) = mpsc::sync_channel::<u64>(1);
    tx.send(10).unwrap();
    assert_eq!(rx.recv().unwrap(), 10);
}
```

有界同步通道允许控制容量，在队列已满时发送可能阻塞，从而向上游施加背压。这个例子不涉及额外线程，但展示了容量作为系统资源约束的重要性。无界队列若生产速度持续高于消费速度，可能导致内存持续增长。

## 背压是必须显式设计的资源边界

无限制的队列会把突发流量转化成不断增长的内存使用。`mpsc::sync_channel(n)` 可以设置容量上限，发送端在队列满时受到阻塞或通过尝试发送接口得到失败；异步运行时通常也有有界 Channel 选择。容量并不是随意常数：它应由生产速度、消费速度、平均消息体积和可接受的排队时间共同决定。

若订单中包含几 MB 附件，移动消息并不等于复制整个附件；但让队列持有大量订单会让相应内存持续存活。所有权系统解释了谁拥有这些消息，容量策略解释了同时允许多少消息存活，两者缺一不可。

## 用回应通道把责任链闭合

发送订单进入工作队列，只保证发送操作按通道协议成功；消费者处理过程中仍可能发生数据库故障。因此可以为每条消息携带一个回执发送端，处理者完成后返回 `Result`。这让请求者能够明确区分“已入队”和“已处理成功”，是消息所有权之外的业务完成协议。

```rust
use std::sync::mpsc;
use std::thread;
struct Command { cents: u64, reply: mpsc::Sender<Result<u64, &'static str>> }
fn main() {
    let (tx, rx) = mpsc::channel::<Command>();
    let worker = thread::spawn(move || {
        if let Ok(cmd) = rx.recv() {
            let answer = if cmd.cents == 0 { Err("invalid") } else { Ok(cmd.cents) };
            let _ = cmd.reply.send(answer);
        }
    });
    let (reply_tx, reply_rx) = mpsc::channel();
    tx.send(Command { cents: 100, reply: reply_tx }).unwrap();
    assert_eq!(reply_rx.recv().unwrap(), Ok(100));
    worker.join().unwrap();
}
```

消息中的 `reply` 也是拥有型资源；把它移动给消费者，消费者就可以负责发送回执。若消费者提前退出，接收端可能获得通道关闭错误，这也是协议必须处理的失败路径。

## Channel 和数据库提交之间还有一道鸿沟

进程内 Channel 不具备断电后持久化的保证。若必须在重启后恢复未处理订单，应使用持久化消息队列、事件日志或数据库工作表，并定义确认、重试、死信和幂等机制。所有权能把进程内任务责任表达得很明确，却不会自动把它延伸到机器宕机以后的世界。

练习对三个时刻设计应答：发送前崩溃、消费者处理中崩溃、写库完成但回执丢失。说明为什么仅凭 `send().is_ok()` 不能认定订单支付成功。

## 消息传递仍有背压和失败问题

Go 的 channel 很常见，Java/C++ 也有阻塞队列与消息系统。Rust 的通道实现将消息类型和所有权转移结合到 API 中；但“发送即移动”的静态约束并不自动决定业务上的送达语义。网络消息队列还需确认、持久化和去重机制。

消息传递不是对共享内存的绝对替代：复杂共享状态、低延迟多读场景可能更适合锁或不可变快照。通道同样可能死锁、阻塞和丢失未处理的任务。设计时要说明容量、失败路径、关闭语义与消费者退出方式。

## 用任务协议完成练习

把上面 `String` 消息改成 `enum Command { Create(Order), Shutdown }`，让消费者在 `match` 中处理两种命令。随后增加 `mpsc::Sender<Result<Receipt, Error>>` 作为回执通道，思考谁拥有回执、错误消息怎样返回、如何检测消费者提前退出。你会发现 `enum`、`Result`、所有权与 Channel 是互相协作的，而不是四种孤立的语法。

## 用订单工作队列串起生命周期

1. 将一个拥有 `String` 的 `Order` 通过 channel 发送，尝试发送后再访问原对象。
2. 使用 `sync_channel(1)` 与两个线程制造生产快于消费的场景，观察背压。
3. 设计一个“至少一次处理”的协议，指出单纯 `send`/`recv` 缺少哪些步骤。

消息传递让资源流动与工作责任更加可见，但它不能取代业务同步设计。下一章区分数据竞争、死锁和事务一致性，明确 Rust 类型系统能保证的边界。

**相关阅读**：[book](https://doc.rust-lang.org/book/); [ref](https://doc.rust-lang.org/reference/)。

---

[← 上一篇](26-shared-ownership-vs-sync.md) · [系列目录](../00-introduction.md) · [下一篇：为什么内存安全仍不能保证并发逻辑正确？](28-memory-safety-vs-logic.md)
