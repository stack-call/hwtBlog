---
title: "17 为什么某些安全规则需要在运行时检查？"
sidebar_position: 17
description: "为什么某些安全规则需要在运行时检查？沿问题、代码、运行机制与设计取舍逐步推导。"
slug: /rust-design/17-runtime-checks-refcell
---

# 17 为什么某些安全规则需要在运行时检查？

> **第 4 篇 · 为什么所有权会改变数据结构与状态管理？**  
> 本文聚焦一个因果问题：为什么某些安全规则需要在运行时检查？；代码示例以稳定 Rust 的通用语法为主（适用于 2021/2024 Edition），除非明确注明为示意或故意无法编译的例子。

有些程序中的借用冲突是否发生，要到运行时才能根据分支和调用顺序确定。`RefCell` 的存在不是否定 Rust 的静态借用检查，而是承认静态系统不适合独自表达全部合理使用模式。关键在于运行时检查失败要以安全方式暴露，不能制造未定义行为。

## 什么检查必须留在运行时

普通共享引用的规则为编译器和程序员提供强约束。内部可变性需要在共享引用后面维护可变数据，但不能直接制造违反引用别名要求的 `&mut T`。Rust 在底层用 `UnsafeCell<T>` 表达可通过共享引用发生内部修改的特殊存储语义，再由 `Cell`、`RefCell`、`Mutex` 等安全抽象施加各自规则。

区分**语言级安全承诺**和**库级动态检查**：借用检查器证明普通安全引用的基本合法性；`RefCell` 在运行时计数/记录当前借用并拒绝冲突；`Mutex` 通过同步协调多线程的访问。安全抽象可以使用底层特殊机制，但必须维护其公开 API 承诺的不变量。

## 编译期不能知道所有运行时访问顺序

标准的 `&mut T` 独占借用约束能够静态检查，但一个具有动态分支和回调的数据结构，可能直到运行时才知道具体先借读还是借写。让编译器精确证明所有程序路径的可重入关系，会遭遇复杂度和可判定性限制。Rust 因此提供另一种安全路径：把访问规则交给类型内部的运行时状态机检查，只要检查不通过就拒绝操作，而不是产生未定义行为。

`RefCell<T>` 是最直观例子。`borrow()` 取得共享借用守卫，`borrow_mut()` 取得独占借用守卫；如果当前存在冲突，会 panic。对应的 `try_borrow()`、`try_borrow_mut()` 可以把失败表示为 `Result`，更便于业务代码处理。

## RefCell 的失败如何被观察

```rust
use std::cell::RefCell;
fn main() {
    let state = RefCell::new(5_u32);
    let a = state.borrow();
    let b = state.try_borrow_mut();
    assert!(b.is_err());
    assert_eq!(*a, 5);
    drop(a);
    *state.borrow_mut() = 6;
}
```

`try_borrow_mut` 在共享借用活跃时返回错误，不会暴露两个冲突的引用。`drop(a)` 显式结束 `Ref` 守卫的存活期，随后可获得可变借用。这种动态检查不改变静态引用的基础规则，而是通过受控的代理对象实现安全访问。

## 让冲突变成可以处理的错误

```rust
use std::cell::RefCell;
fn main() {
    let items = RefCell::new(vec![1, 2]);
    let read_guard = items.borrow();
    assert!(items.try_borrow_mut().is_err());
    println!("len={}", read_guard.len());
    drop(read_guard);
    items.try_borrow_mut().unwrap().push(3);
    assert_eq!(items.borrow().len(), 3);
}
```

程序保持内存安全的关键不是“现在允许多个不兼容引用共存”，而是借用守卫阻止了违背规则的访问。与普通借用相比，这里多了运行时计数或状态检查成本，并可能在运行时失败。如果把 `try_borrow_mut` 换成 `borrow_mut` 且存在冲突，就会 panic；在核心服务中通常应该主动决定怎样处理这种失败。

## UnsafeCell 才是底层可变性原语

内部可变性直接影响数据结构表达和框架设计：事件总线、缓存、GUI 状态可能需要运行时借用；多线程服务则需要锁或原子类型。另一方面，依赖运行时检查也意味着冲突可能在测试或生产流量下暴露，因此设计要有明确的借用范围和错误策略。

```rust
use std::sync::Mutex;
fn main() {
    let state = Mutex::new(vec!["pending"]);
    {
        let mut guard = state.lock().unwrap();
        guard.push("paid");
    }
    assert_eq!(state.lock().unwrap().len(), 2);
}
```

`Mutex` 与 `RefCell` 都可通过保护对象提供受控访问，但前者面向多线程同步，涉及锁竞争和可能的 poison 状态；后者不提供跨线程同步。理解它们的区别时应从“保护什么不变量”出发，而非从语法上都是 `.borrow_mut()` 或 `.lock()` 出发。

## UnsafeCell 的职责为何不同

`UnsafeCell<T>` 是 Rust 内部可变性的底层原语，它允许在共享访问的场景下对其内部值建立特殊的可变性模型，但**不会自动执行借用冲突检查**。`Cell`、`RefCell`、`Mutex` 等更高级抽象需要负责各自的额外安全不变量。用 `UnsafeCell` 自己写安全容器时，必须证明不会形成违反引用规则的访问，确保内存有效和并发同步正确。

把 `UnsafeCell` 当成“比 RefCell 更快的 RefCell”是危险的：前者不提供自动的动态借用管理。普通业务代码优先使用标准库已经审查过的安全封装；只有构建底层同步原语或特殊容器时，才有理由承担原始指针和不变量审查责任。

## 动态检查引入了一种新的失败方式

从 `&mut T` 改成 `RefCell<T>` 并不是免费提高表达能力。原先编译器可以拒绝冲突访问，而现在代码可能编译成功，却在极少见的回调顺序下 panic。为了把这种错误变成可恢复的结果，你可能使用 `try_borrow_mut()`，把动态借用失败传播给上层：这与 `Result` 错误模型又建立了联系。

```rust
use std::cell::{RefCell, BorrowMutError};
fn append(cell: &RefCell<Vec<u64>>, v: u64) -> Result<(), BorrowMutError> {
    cell.try_borrow_mut()?.push(v);
    Ok(())
}
fn main() {
    let c = RefCell::new(vec![1]);
    let guard = c.borrow();
    assert!(append(&c, 2).is_err());
    drop(guard);
    assert!(append(&c, 2).is_ok());
}
```

这里的失败不是底层内存已被破坏，而是类型阻止了违反独占访问协议的操作。安全抽象把潜在的未定义行为转成受控的运行时失败；代价是调用者现在需要决定如何处理 `BorrowMutError`。

## UnsafeCell 与锁内部同样面临证明责任

`UnsafeCell` 只是允许内部改变被包装值的别名优化假设，不提供锁或动态计数。实现 `Mutex`、`RefCell` 这样的容器，仍须保证不会通过 safe API 暴露违反 Rust 引用规则的访问。普通程序大可使用标准库：设计者应关注传递到边界的是 `&T`、`&mut T` 还是某种守卫，而不是直接操作原始内部存储。

对一个缓存设计两套接口：`get(&self)` 返回值快照，`update(&mut self)` 修改；以及通过 `RefCell` 在 `&self` 上更新缓存。比较调用端的灵活性、运行时失败路径和多线程适用性。只有全面比较，才能知道动态检查是否值得。

## 安全封装如何维持不变量

C++、Java、Go 都能通过锁、原子或受控封装保护共享可变状态。Rust 的不同之处在于普通安全引用先受静态规则约束，动态机制再作为有明确契约的例外。`UnsafeCell` 是底层构件，不是建议日常业务代码直接操作它。

安全代码仍可能因 `RefCell` 冲突而 panic；“不会发生内存不安全”不等于“不会失败”。`unsafe` 的存在也不自动表示 API 不安全：标准库可以内部使用 unsafe 实现并对外提供安全接口，只要封装不变量正确。

## 一次设计决策练习

假设一个订单缓存读取率为 99%，每次查找都要记录命中数。方案 A：业务状态拥有缓存可变引用；方案 B：用 `Cell<u64>` 记录线程内计数；方案 C：多线程环境使用 `AtomicU64`；方案 D：每次访问取得整个 `Mutex<Cache>`。比较这四种方案的锁竞争、类型边界和可组合性。

关键认知是：静态借用、动态借用、原子和互斥锁分别保证不同性质。不要把“运行时检查”与“线程同步”混为一谈，尤其不要假设 `RefCell<T>` 能直接替代 `Mutex<T>`。

## 何时不值得追求纯静态证明

1. 用 `try_borrow_mut` 实现冲突时返回业务错误，而不是 panic。
2. 把局部 `RefCell<Vec<_>>` 改为 `Mutex<Vec<_>>`，讨论为什么后者适合线程共享而前者不适合。
3. 解释“静态拒绝”“动态返回错误”“动态 panic”各在什么情况下出现。

第四篇收束于一个原则：共享不是一个单维度问题，它涉及所有权、访问许可和线程环境。下一篇转向类型系统，理解为何 Rust 的类型还需要表达能力、状态和资源行为。

**相关阅读**：[book](https://doc.rust-lang.org/book/); [ref](https://doc.rust-lang.org/reference/)。

---

[← 上一篇](16-interior-mutability.md) · [系列目录](../00-introduction.md) · [下一篇：为什么类型不仅需要说明数据是什么，还需要约束能做什么？](../part-5/18-types-constrain-behavior.md)
