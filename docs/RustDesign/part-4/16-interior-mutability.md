---
title: "16 为什么共享的数据有时仍然需要修改？"
sidebar_position: 16
description: "为什么共享的数据有时仍然需要修改？沿问题、代码、运行机制与设计取舍逐步推导。"
slug: /rust-design/16-interior-mutability
---

# 16 为什么共享的数据有时仍然需要修改？

> **第 4 篇 · 为什么所有权会改变数据结构与状态管理？**  
> 本文聚焦一个因果问题：为什么共享的数据有时仍然需要修改？；代码示例以稳定 Rust 的通用语法为主（适用于 2021/2024 Edition），除非明确注明为示意或故意无法编译的例子。

缓存命中次数、延迟初始化状态、测试中的调用次数都需要在拥有共享引用时更新。但普通 `&T` 不允许随意通过它修改内部字段。Rust 必须在保护别名规则的同时允许这些合理的需求，于是提供内部可变性抽象。

## 共享和修改为什么产生张力

如果“多个共享引用 + 任意可变写入”毫无限制，就会回到令人难以推理的别名问题。内部可变性不应该成为逃避借用规则的后门，而应当选择可证明安全的机制：`Cell` 用复制/替换的方式操作小值；`RefCell` 在运行时执行借用检查；线程间共享则需要同步原语。

先区分三个维度：是否需要多个所有者、是否需要内部修改、是否跨线程。这分别对应 `Rc/Arc`、`Cell/RefCell`、`Mutex/RwLock` 等不同问题。把所有情况写成 `Arc<Mutex<T>>` 虽然有时能工作，却可能制造没必要的锁与复杂度。

## 先从一个合理需求出发

缓存、统计计数器和测试用的调用记录器经常有一个特点：调用者只拿到共享引用，组件内部却需要记住一些状态。若一律要求 `&mut self`，调用方必须沿整个调用链提供独占可变访问，可能与接口定义不匹配。内部可变性不是违背 Rust 的“不变量”，而是让某些类型自行负责维护运行时访问约束。

`Cell<T>` 适合通过值复制或替换进行的小型内部状态操作；`RefCell<T>` 支持借用内部 `T`，但在运行时跟踪读写借用冲突。它们通常只用于单线程；跨线程共享需要 `Mutex`、`RwLock` 或原子操作等不同机制。

## RefCell 把哪些检查放在运行时

```rust
use std::cell::Cell;
struct Metrics { hit_count: Cell<u64> }
impl Metrics {
    fn hit(&self) { self.hit_count.set(self.hit_count.get() + 1); }
    fn hits(&self) -> u64 { self.hit_count.get() }
}
fn main() {
    let metrics = Metrics { hit_count: Cell::new(0) };
    metrics.hit();
    metrics.hit();
    assert_eq!(metrics.hits(), 2);
}
```

`hit` 只拿到 `&self`，却通过 `Cell<u64>` 修改内部计数。这不是未受约束的普通共享可变引用：`Cell` 不向外暴露长期保持的内部 `&mut u64`，而是提供受控的 get/set 操作。它适合单线程、可按值读写的数据。

## 为什么 Cell 和 RefCell 不能混为一谈

```rust
use std::cell::{Cell, RefCell};
struct Metrics {
    hits: Cell<u64>,
    messages: RefCell<Vec<String>>,
}
impl Metrics {
    fn record(&self, text: &str) {
        self.hits.set(self.hits.get() + 1);
        self.messages.borrow_mut().push(text.into());
    }
}
fn main() {
    let m = Metrics { hits: Cell::new(0), messages: RefCell::new(vec![]) };
    m.record("ok");
    assert_eq!(m.hits.get(), 1);
    assert_eq!(m.messages.borrow().len(), 1);
}
```

外部只有 `&Metrics`，却能记录调用次数。`Cell` 没有向外暴露内部值的普通借用；`RefCell` 返回受运行时约束的 `Ref` 或 `RefMut` 守卫。当这些守卫仍存在时，再申请不兼容的借用会触发 panic。这里的数据安全依赖库的正确实现，而不是编译器对普通字段访问的静态独占证明。

## 用 Cell 处理无需暴露引用的小值

这个设计影响 API 语义：看到 `&self` 不能简单断言“绝不改变任何可观察状态”。Rust 对外承诺的是在安全机制内受控访问。闭包捕获、缓存、惰性计算和观察者模式都可能利用内部可变性，而类型设计需要明确何处承担同步或动态检查成本。

```rust
use std::cell::RefCell;
fn main() {
    let notes = RefCell::new(Vec::<String>::new());
    {
        let mut writer = notes.borrow_mut();
        writer.push("paid".into());
    }
    let reader = notes.borrow();
    assert_eq!(reader[0], "paid");
}
```

`RefCell` 允许通过共享引用在运行时获取受检查的 `Ref`/`RefMut`。如果写借用仍然活跃时再请求冲突的读/写借用，会 panic。通过缩小 `writer` 的作用域，可以清晰管理动态借用区间；也可使用 `try_borrow` 系列把冲突转成 `Result`。

## 守卫的持有时间为何影响 API

```rust
use std::cell::RefCell;
fn main() {
    let shared = RefCell::new(vec![1]);
    let borrowed = shared.borrow();
    // shared.borrow_mut().push(2); // 运行时会 panic，故意不执行
    println!("{}", borrowed.len());
    drop(borrowed);
    shared.borrow_mut().push(2); // 现在可以
}
```

和普通借用类似，动态借用也有有效区间，但它由守卫值负责在运行时管理。一个隐藏在很长函数中的 `RefMut` 可能让后续组件意外 panic，或使代码难以重入。好的设计应该尽量缩短守卫存在时间，避免在持有可变守卫时调用未知回调。

## 缓存为什么容易诱发内部可变性

一个 `fn get(&self, key: &str) -> Option<&Value>` 看似只读，但为了提高性能，缓存内部可能需要更新最近访问时间和命中统计。这里需要分清业务不变性和实现内部的可变性：调用者看到的查询语义不应改变，但缓存可以更新内部统计。若把整个缓存包进 `RefCell`，每次查询可能引入动态借用和潜在的重入冲突；若只为命中数使用 `Cell<u64>`，反而更局部。

```rust
use std::cell::Cell;
struct Counter { hits: Cell<usize> }
impl Counter {
    fn read(&self) -> usize {
        self.hits.set(self.hits.get() + 1);
        self.hits.get()
    }
}
fn main() {
    let c = Counter { hits: Cell::new(0) };
    assert_eq!(c.read(), 1);
    assert_eq!(c.read(), 2);
}
```

`Cell` 允许在共享引用下通过值的方式读取、替换内部计数，避免向外暴露对内部状态的普通引用。它不意味着 `Cell` 可跨线程共享；如果把计数器作为全局多线程指标，要使用适当的原子或锁结构。

## 一个真实的运行时借用失败场景

在 `RefCell<Vec<_>>` 上取得 `Ref` 后，如果仍持有它而调用需要 `RefMut` 的函数，就可能 panic。这与静态 `&T`/`&mut T` 的冲突语义相近，却延迟到运行时发现。为避免隐蔽失败，应缩短借用守卫范围，不要持有 `RefMut` 后执行未知的回调，更不要把内部可变性遍布所有领域对象。

将示例改成 `fn increment(&mut self)`，比较调用链如何变化。若只有一层函数需要写入，用普通 `&mut` 往往最清晰；若必须在接口上保持共享读取而更新局部缓存，才有使用 `Cell` 或 `RefCell` 的合理理由。


## 一个不适合 `RefCell` 的场景

假设一个订单对象被多个线程共享，客户端试图写 `Arc<RefCell<Order>>`，认为外层 Arc 是原子引用计数，因此内部也一定安全。这种判断混淆了两个独立问题：Arc 只同步共享所有权计数，RefCell 的动态借用状态并非线程安全。编译器会通过相应 Trait 条件限制此类跨线程共享。

如果只是读取同一不可变订单，`Arc<Order>` 可能就足够；如果需要共享修改，通常需要 `Arc<Mutex<Order>>` 或相应同步机制；如果每笔订单由唯一任务处理，把所有权送入队列通常更容易维护。这些选择不只影响运行时成本，也影响 API 是否可能 panic、是否会阻塞以及是否存在锁竞争。内部可变性不应该成为所有借用报错的默认修复方案。

## 共享可变性需要明确规则而非任意放开

C++ 可以借助 `mutable` 等机制改变 const 视图下的内部状态；Java/Go 的对象引用通常不要求这种编译期独占规则，因此修改表达不同。Rust 的内部可变性体现一种折中：编译期难表达的安全访问，交由受控运行时检查实现。

`Cell`/`RefCell` 不是跨线程同步工具；`Rc<RefCell<T>>` 不能安全地作为任意跨线程共享状态。内部可变性也不能替你证明业务事务的一致性，例如读取两次余额之间是否被其他操作修改。

## 是否一定需要内部可变性？

先尝试拆分状态：把纯业务逻辑改成接受 `&Order`，把计数器交给另一层拥有可变状态的组件；如果只是局部需要更新，直接传 `&mut Metrics` 更清晰。只有在接口必须允许共享访问且确实需要内部改变时，再选择 `Cell` / `RefCell`。它们并不是“更高级的 Rust”，而是以运行时成本换取表达灵活性。

练习把上面的 `Metrics` 改为显式 `&mut self`，列出调用处发生了什么变化；再设计一个线程安全版本，说明为什么不能直接把原 `RefCell` 放进 `Arc` 就跨线程使用。

## 如何判断应该重构还是用内部可变性

1. 在 `RefCell` 的可变借用尚未结束时调用 `borrow()`，观察 panic，然后改为 `try_borrow()`。
2. 用 `Cell<bool>` 实现缓存的 `initialized` 标记，思考是否会出现中间状态不一致。
3. 对同一数据分别选择 `&mut T` 与 `RefCell<T>`，比较静态保证和运行时成本。

内部可变性让共享数据可以被修改，但我们还需要知道它的安全基础是什么。下一章进入 `UnsafeCell`、运行时借用检查和安全抽象的边界。

**相关阅读**：[book](https://doc.rust-lang.org/book/); [ref](https://doc.rust-lang.org/reference/)。

---

[← 上一篇](15-shared-ownership-rc-arc.md) · [系列目录](../00-introduction.md) · [下一篇：为什么某些安全规则需要在运行时检查？](17-runtime-checks-refcell.md)
