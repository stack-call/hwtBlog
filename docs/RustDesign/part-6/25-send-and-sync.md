---
title: "25 为什么能够转移资源不代表能够安全共享资源？"
sidebar_position: 25
description: "为什么能够转移资源不代表能够安全共享资源？沿问题、代码、运行机制与设计取舍逐步推导。"
slug: /rust-design/25-send-and-sync
---

# 25 为什么能够转移资源不代表能够安全共享资源？

> **第 6 篇 · 为什么所有权规则会影响并发程序？**  
> 本文聚焦一个因果问题：为什么能够转移资源不代表能够安全共享资源？；代码示例以稳定 Rust 的通用语法为主（适用于 2021/2024 Edition），除非明确注明为示意或故意无法编译的例子。

把一个拥有的订单对象转移到另一个线程，与让两个线程长期同时引用它，是两种不同的操作。转移意味着新线程成为责任方，原线程不再使用；共享则要求同时存在的访问本身是安全的。Rust 用两个自动 Trait 分别表达这两类能力。

## 移动到线程与共享到线程为何不同

`Send` 意味着某类型的值可以安全跨线程转移所有权；`Sync` 意味着该类型的共享引用 `&T` 可以安全跨线程共享。对于 `Rc<T>`，非原子引用计数使其不满足这两类线程共享条件；`Arc<T>` 的安全能力还取决于其内部 `T`。

很多类型可由编译器根据字段自动推导 `Send/Sync`，并非需要为每个业务结构手写实现。更精确地说 `T: Sync` 与 `&T: Send` 密切相关。理解自动 Trait 时必须沿字段和封装机制分析：某个成员是否含有非线程安全的状态。

## 先区分移动所有权与共享引用

将一个 `String` 移动给后台线程，意味着原线程不再通过原绑定使用它；而让两个线程同时持有 `&String`，意味着同一底层数据需要满足并发共享的安全条件。这是两类不同的问题。`Send` 大体表达一个值可以安全地转移到另一线程；`Sync` 大体表达其共享引用可以在线程之间安全使用。Rust 中 `T: Sync` 与 `&T: Send` 有紧密关系，但并不能把所有类型都简单视为同时具有这两个能力。

`Rc<T>` 的引用计数不是线程安全的，两个线程同时增减它的计数可能产生数据竞争，因此 `Rc<T>` 不实现 `Send` / `Sync`。`Arc<T>` 通过原子计数允许线程安全地共享计数本身，但若 `T` 的内部访问本身不具备线程安全性质，`Arc<T>` 不会自动把它修好。

## Send 和 Sync 的逻辑关系

```rust
use std::sync::Arc;
use std::thread;
fn main() {
    let customer = Arc::new(String::from("Ada"));
    let other = Arc::clone(&customer);
    let handle = thread::spawn(move || other.len());
    assert_eq!(handle.join().unwrap(), 3);
    assert_eq!(customer.as_str(), "Ada");
}
```

`Arc<String>` 可以被安全移交线程，因为引用计数维护使用了原子操作且字符串满足相应类型条件。主线程和子线程都拥有一个 `Arc` 句柄，可以并发读取同一字符串。这里的 `Arc` 不允许直接通过共享句柄任意修改内部 `String`。

## 把任务数据完整移入线程

```rust
use std::thread;
fn main() {
    let order = String::from("order-7");
    let handle = thread::spawn(move || {
        println!("processing {order}");
        order.len()
    });
    let size = handle.join().unwrap();
    assert_eq!(size, 7);
    // println!("{order}"); // 预期编译失败：所有权已经转移
}
```

这里没有共享可变状态。工作线程独立拥有字符串，处理完成后把长度通过 `JoinHandle` 的返回值交还主线程。很多并发程序使用移动所有权就足够了，完全不需要 `Arc<Mutex<T>>`。若线程要把一份计算结果交回调用者，返回值比共享一个可变结果对象更加简单。

## Rc 为什么不能直接跨线程

这两个 Trait 深刻影响泛型库：`thread::spawn`、异步多线程执行器和共享容器会对任务及其捕获的数据施加 `Send`/`Sync` 约束。某个函数从单线程环境迁往多线程环境时，报错常常沿着闭包捕获字段追溯到一个不满足自动 Trait 的成员。

```rust
use std::sync::{Arc, Mutex};
use std::thread;
fn main() {
    let count = Arc::new(Mutex::new(0));
    let another = Arc::clone(&count);
    let h = thread::spawn(move || { *another.lock().unwrap() += 1; });
    h.join().unwrap();
    assert_eq!(*count.lock().unwrap(), 1);
}
```

当需要共享可变数据时，`Arc` 解决共同拥有，`Mutex` 解决互斥访问。两者不能相互替代：`Arc<Cell<T>>` 不能因外层是 Arc 就任意安全跨线程使用。记住“拥有”“共享”“修改”是三个独立维度。

## 何时才需要共享

假设多个线程共同读取一份不可变汇率表，那么 `Arc<RateTable>` 可以避免为每个任务复制完整表格。如果表本身需要热更新，就要引入额外同步或快照替换机制。跨线程共享的数据类型还必须满足相应的线程安全约束：`Arc<RefCell<Vec<_>>>` 不会因为外层 `Arc` 有原子计数就具备线程安全的内部可变性。

要分清三种原子性：引用计数加减的原子性、一个整数更新的原子性、订单整体业务事务的原子性。前两者是底层同步属性，第三者需要你在业务操作层设计正确的临界区或事务协议。

## 使用线程作用域理解 `'static` 限制的来源

`thread::spawn` 创建的线程可以在当前函数返回后继续执行，因此它要求闭包通常不包含会过早失效的借用。并非所有线程都必须要求 `'static`：`std::thread::scope` 可以让线程的生命周期明确限制在一个作用域中，从而安全借用该作用域里的局部变量。

```rust
use std::thread;
fn main() {
    let prices = vec![10_u64, 20, 30];
    thread::scope(|scope| {
        scope.spawn(|| {
            println!("sum={}", prices.iter().sum::<u64>());
        });
    }); // 线程在退出 scope 前结束
    assert_eq!(prices.len(), 3);
}
```

这里不需要把 `prices` 包装到 `Arc`，因为作用域保证线程不会比被借用的局部数据活得更久。这也说明 Rust 的借用规则并不是“线程里禁止引用”，而是要求跨线程访问有可证明的生存期和线程安全能力。

## Send 与 Sync 的规则也会影响 API 演进

一个库最初只在单线程使用 `Rc<RefCell<T>>`，如果后来要让对象跨线程流动，就可能需要重新选择 `Arc`、锁或消息通道。这不是简单地把类型名从 Rc 改成 Arc，因为内部可变性、数据竞争和锁持有范围也会改变。能力约束会从底层字段一路传播到公共 Trait Bound 和函数签名。

练习写一个接受 `F: FnOnce() + Send + 'static` 的任务接口，再用 `thread::scope` 写可借用局部数据的版本。比较它们提供的生命周期保证，以及调用者必须提供的资源形式。


## 线程作用域如何改变线程 API 的所有权要求

普通 `spawn` 的线程可能在调用函数结束后还活着，因此捕获外部局部引用通常不符合 `'static` 需求。但 `thread::scope` 保证在离开作用域前等待线程结束，使借用本地数据成为合法设计。可见 Rust 不禁止线程借用数据，而是要求线程的实际存活关系与借用关系相匹配。

对一个只读共享价格表，若所有工作都在请求作用域结束前完成，可以用 scoped threads 借用；若工作要交给长期后台线程，就需要传入拥有型数据或共享所有权句柄。选择不仅是方便与否，还涉及请求取消时线程是否继续运行、资源何时释放、调用者是否需要等待完成。`Send`/`Sync` 是底层安全能力约束，而任务生命周期是另一层设计承诺。

## Arc 也受内部类型能力限制

Java 通过内存模型与同步原语定义并发可见性，Go 通过类型、运行时和同步约定组织共享；Rust 将某些能否跨线程的性质表示为 Trait Bound。它并没有消灭锁、原子或内存模型，而是把非法组合在安全代码中更早地拒绝。

`Send`/`Sync` 是关于线程内存安全的能力，不意味着一个服务“线程安全地实现了业务需求”。某些安全类型仍可能发生逻辑竞争或死锁。也不要尝试随意 `unsafe impl Send` 来压制错误：这会转移安全证明责任。

## 将能力约束变成可读 API

如果写一个通用任务执行器，`F: FnOnce() + Send + 'static` 不是形式主义：它说明任务可以被消费调用、能够跨线程转移，并且不依赖容易过早失效的非静态引用。`'static` 不表示这个任务会一直运行到程序结束，只表示其内部引用所需有效性可以满足这种边界。

练习尝试把 `Rc<String>` 移到 `thread::spawn` 中，记录编译器指出的 Trait 缺失；改为 `Arc<String>` 再运行。然后在 `Arc` 里面放入 `RefCell<i32>`，解释为什么新的组合仍然不适合直接跨线程共享。

## 把线程边界写进函数签名

1. 尝试把 `Rc<String>` 移到 `thread::spawn`，记录错误后改为 `Arc<String>`。
2. 比较 `Arc<String>` 与 `Arc<Mutex<String>>` 能提供的修改能力。
3. 给一个包含 `Rc<u64>` 字段的结构体分析其 `Send`/`Sync` 性质。

线程转移与共享的区分已经清晰。下一章继续拆分“共享所有权”和“同步访问”，研究 Mutex、RwLock 与原子操作分别保护什么。

**相关阅读**：[book](https://doc.rust-lang.org/book/); [ref](https://doc.rust-lang.org/reference/)。

---

[← 上一篇](24-shared-mutable-across-threads.md) · [系列目录](../00-introduction.md) · [下一篇：为什么共享所有权与同步访问是两个不同问题？](26-shared-ownership-vs-sync.md)
