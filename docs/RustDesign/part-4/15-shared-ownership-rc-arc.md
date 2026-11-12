---
title: "15 为什么单一所有权不能表达所有对象关系？"
sidebar_position: 15
description: "为什么单一所有权不能表达所有对象关系？沿问题、代码、运行机制与设计取舍逐步推导。"
slug: /rust-design/15-shared-ownership-rc-arc
---

# 15 为什么单一所有权不能表达所有对象关系？

> **第 4 篇 · 为什么所有权会改变数据结构与状态管理？**  
> 本文聚焦一个因果问题：为什么单一所有权不能表达所有对象关系？；代码示例以稳定 Rust 的通用语法为主（适用于 2021/2024 Edition），除非明确注明为示意或故意无法编译的例子。

一个订单系统可能让多个订单共同引用用户档案或价格表。把整份数据复制到每个订单会浪费内存；只保存借用又可能使用户档案的生命周期约束蔓延到整个系统。于是需要共享所有权，但共享的方式应与线程环境相匹配。

## 一棵树为什么容易拥有，一个图为什么困难

单一所有权擅长表达树状责任：父对象持有子对象，移动父对象时子对象随之转移。但共享缓存、图节点和订阅者集合常存在多对一甚至环状关系。Rust 不否认这些结构，而是要求通过不同容器显式描述：`Box` 是拥有堆数据，`Rc` 是单线程共享，`Arc` 是线程安全的引用计数共享。

先不要把三种指针记成“高级引用”；它们回答的是三个不同问题。`Box<T>`：需要固定大小的间接存储或递归结构吗？`Rc<T>`：多个单线程使用者需要共同拥有吗？`Arc<T>`：共享所有权需要跨线程传递吗？它们本身均不保证共享可变状态的任意安全修改。

## 递归类型需要一个大小已知的间接层

```rust
#[derive(Debug)]
enum Tree {
    Leaf(u64),
    Pair(Box<Tree>, Box<Tree>),
}
fn sum(t: &Tree) -> u64 {
    match t {
        Tree::Leaf(n) => *n,
        Tree::Pair(a, b) => sum(a) + sum(b),
    }
}
fn main() {
    let t = Tree::Pair(Box::new(Tree::Leaf(2)), Box::new(Tree::Leaf(3)));
    assert_eq!(sum(&t), 5);
}
```

如果递归分支直接内嵌 `Tree`，其静态大小会无限展开。`Box<Tree>` 以大小确定的拥有型指针打断无限递归，这是 `Box` 的一个重要用途。它解决的是**间接存储与独占所有权**，不是“只要有多个引用就应该 Box”。`Rc` 与 `Arc` 解决的是另一件事：让多个句柄共同拥有同一份分配。

## Box 解决间接存储，Rc 解决共享所有权

```rust
use std::rc::Rc;
#[derive(Debug)]
struct Customer { name: String }
struct Order { customer: Rc<Customer>, cents: u64 }
fn main() {
    let customer = Rc::new(Customer { name: "Ada".into() });
    let first = Order { customer: Rc::clone(&customer), cents: 100 };
    let second = Order { customer: Rc::clone(&customer), cents: 200 };
    assert_eq!(first.customer.name, second.customer.name);
    assert_eq!(Rc::strong_count(&customer), 3);
    assert_eq!(first.cents + second.cents, 300);
}
```

`Rc::clone` 克隆的是共享所有权句柄，不复制整个 `Customer`。三方都持有强引用，最后一个强引用离开时底层对象才会被销毁。这适合只需读取客户资料的单线程缓存；若要多线程共享，就不能直接把 `Rc` 发过去。

## 一个真实的共享引用计数案例

```rust
use std::rc::Rc;
#[derive(Debug)]
struct Customer { name: String }
fn main() {
    let customer = Rc::new(Customer { name: "Alice".into() });
    let order_a = Rc::clone(&customer);
    let order_b = Rc::clone(&customer);
    assert_eq!(Rc::strong_count(&customer), 3);
    println!("{} / {}", order_a.name, order_b.name);
}
```

`Rc::clone` 不会深复制 `Customer`；它增加强引用计数，释放每个句柄时再减少计数。这个便利带来额外的指针间接访问和计数操作。并且它不能像跟踪式 GC 那样自动处理强引用环：如果 A 强持有 B、B 又强持有 A，两者的强计数可能始终不归零。

双向关系通常需要一端使用 `Weak` 来表示“可以尝试升级，但不拥有”。`Weak::upgrade()` 返回 `Option<Rc<T>>`，正好表达目标可能已经被释放的事实。

## 引用计数并不能自动收集引用环

共享所有权使类型能够表示网络、缓存和多对一关系，但也引入释放时机更分散、循环引用可能泄漏的问题。`Weak` 通过不拥有强生命周期责任的弱引用表达反向链接，常用于树的 parent 指针。资源关系设计因而影响数据模型甚至业务拓扑。

```rust
use std::rc::{Rc, Weak};
struct Node { label: String, parent: Weak<Node> }
fn main() {
    let parent = Rc::new(Node { label: "root".into(), parent: Weak::new() });
    let child = Node { label: "leaf".into(), parent: Rc::downgrade(&parent) };
    assert_eq!(child.parent.upgrade().unwrap().label, "root");
    assert_eq!(child.label, "leaf");
}
```

通过 `Weak`，子节点可以“知道”父节点，但不会让父节点因为这条反向链接而永远不能释放。调用 `upgrade` 得到 `Option<Rc<T>>`，因为父节点可能已经被销毁。这是资源生命周期逻辑如何进入 API 类型的又一个例子。

## Arc 并不能自动让内部数据可随意修改

`Arc<T>` 使用原子引用计数以支持相应的跨线程所有权共享，但能否跨线程发送或共享仍取决于 `T` 的 `Send`、`Sync` 性质。`Arc<RefCell<T>>` 不会神奇地变成线程安全的共享可变容器。需要同时修改共享数据时，可以使用 `Arc<Mutex<T>>`、`Arc<RwLock<T>>` 或者消息传递。

一份只读配置常适合 `Arc<Config>`，因为数据不变且多个任务长期使用；一个高频写入的共享计数器则可能更适合原子类型；一个复杂订单队列可以通过 Channel 转移所有权，减少共享锁争用。资源关系先于指针类型的选择。

## 强引用环的失效过程

一个常见图结构是父节点拥有子节点，子节点又保存指向父节点的引用。如果两条边都是 `Rc` 强引用，那么外部即使不再持有任何节点，这两个节点也可能互相保持强计数不为零，导致内存不释放。`Rc` 在这里正确完成了自己的工作：只在强计数归零时清理；错误来自建模者把本应非拥有的反向链接误写为拥有关系。

```rust
use std::rc::{Rc, Weak};
use std::cell::RefCell;
struct Node { parent: RefCell<Weak<Node>> }
fn main() {
    let parent = Rc::new(Node { parent: RefCell::new(Weak::new()) });
    let child = Rc::new(Node { parent: RefCell::new(Rc::downgrade(&parent)) });
    assert!(child.parent.borrow().upgrade().is_some());
    drop(parent);
    assert!(child.parent.borrow().upgrade().is_none());
}
```

这里使用 `Weak` 明确告诉读者：“子节点可以尝试访问父节点，但不负责让父节点继续存活。”`upgrade` 返回 `Option` 的设计自然来自弱引用不保证目标存在。若把它当强引用使用，就会重新出现资源责任不清的问题。

## 选择共享前先估算代价

`Rc`/`Arc` 的句柄通常比一个直接值多一层间接访问和计数维护，还可能造成数据在某处被长期保持而无法及时清理。对几字节的订单 ID，复制通常更简单；对数 MB 的不可变配置，共享可能明显更合理。跨线程读写时，还要问是否真需要共享同一个可变对象，或者可以把所有权移交给单一工作线程。

最后为自己建立“指针选择图”：有大小已知且能直接存放的数据就直接拥有；需要递归间接存储考虑 `Box`；单线程多方长期共享考虑 `Rc`；跨线程共享考虑 `Arc` 并验证内部安全能力；共享可变性再引入适合的同步或内部可变性工具。

## Arc 的线程安全到底覆盖到哪里

C++ 的 `shared_ptr`/`weak_ptr` 与这一模式很相似；Java/Go 的托管引用通常由 GC 处理对象可达性，但仍可能在应用层意外长期保留对象。Rust 通过类型显式区分强拥有关系和弱关系，使某些生命周期架构更可见。

不要把 `Box` 理解为共享指针；也不要以为 `Arc<T>` 一定使 `T` 可被跨线程修改。`Arc<T>` 的线程安全能力依赖内部类型满足相应要求；如果内部需要同步修改，还需 Mutex、RwLock 或原子类型等机制。

## 决策时先回答四个问题

这份数据有几个长期拥有者？是否跨线程？需要修改吗？能否避免长期共享、改用索引或消息？如果答案分别是“一、否、否、是”，可能普通的 `Box` 或直接值组合就足够；如果是“多、否、是、否”，可能考虑 `Rc<RefCell<T>>`；如果是“多、是、是、否”，通常考虑线程同步结构。

练习为客户对象画一个 `Rc` 强引用环，观察强引用计数何时无法归零；再把反向链接替换成 `Weak`，解释为什么 `upgrade` 必须返回可能失败的结果。

## 先决定共享关系，再选择指针

1. 运行强引用计数例子，分别 `drop(first)` 和 `drop(second)` 后检查计数。
2. 尝试设计强引用循环，再改为 `Weak`；解释资源泄漏和内存不安全的区别。
3. 说明 `Box<dyn Trait>` 与 `Rc<dyn Trait>` 在所有权上的差异。

共享所有权可以让多方持续访问同一数据，却没有回答“如果这份共享数据还要修改怎么办”。下一章将研究内部可变性与借用检查何时转移到运行时。

**相关阅读**：[book](https://doc.rust-lang.org/book/); [ref](https://doc.rust-lang.org/reference/)。

---

[← 上一篇](14-own-or-borrow-in-data-structures.md) · [系列目录](../00-introduction.md) · [下一篇：为什么共享的数据有时仍然需要修改？](16-interior-mutability.md)
