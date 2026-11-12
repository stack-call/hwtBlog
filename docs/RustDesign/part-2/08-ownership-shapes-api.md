---
title: "08 为什么所有权会改变函数和 API 的设计？"
sidebar_position: 8
description: "为什么所有权会改变函数和 API 的设计？沿问题、代码、运行机制与设计取舍逐步推导。"
slug: /rust-design/08-ownership-shapes-api
---

# 08 为什么所有权会改变函数和 API 的设计？

> **第 2 篇 · 为什么资源必须有明确的所有者？**  
> 本文聚焦一个因果问题：为什么所有权会改变函数和 API 的设计？；代码示例以稳定 Rust 的通用语法为主（适用于 2021/2024 Edition），除非明确注明为示意或故意无法编译的例子。

传统 API 讨论常聚焦参数类型和返回类型。Rust 还要求你回答：传入参数之后调用者是否仍可用它？函数是否有权修改它？返回值是在借用原数据还是新拥有一份数据？这使所有权成为 API 契约的一部分。

## 函数签名其实是一份资源契约

假设 `submit_order` 只需要读取订单，若签名接受 `Order`，调用者将失去订单的所有权；如果调用内部直接 `.clone()`，可能无谓分配。另一方面，提交确实可能代表生命周期中的一次状态变化，此时消费旧订单反而恰当。问题不是一律“多借用”，而是权限与语义要匹配。

三个常见签名对应三个不同承诺：`fn inspect(&T)` 使用只读共享引用；`fn update(&mut T)` 需要独占可变访问；`fn finalize(T) -> U` 消费输入并返回另一种状态。对字符串类参数，还要区分 `String`（拥有）与 `&str`（借用视图），尽量避免无谓限制。

## 从使用者的角度读一条签名

`fn label(&self) -> &str` 承诺返回的视图依附于 `self` 的有效借用；`fn rename(&mut self, &str)` 要求调用者提供独占可变访问；`fn submit(self) -> Submitted` 则意味着调用后旧的 `Draft` 不再可用。相比“这个方法可以修改字段”的文档描述，签名本身使编译器也能验证一部分约束。它不是纯粹语法风格，而是权限、资源责任和状态转移的交汇点。

请特别区分 `&str` 与 `String`：前者表达字符串视图，不拥有字符缓冲区；后者拥有一份字符串数据。若函数只读一个标签，接受 `&str` 通常比要求 `&String` 更通用；若函数准备把字符串放到长期队列中，它需要拥有数据，可能接受 `String` 或显式复制。

## 选择借用、消费还是产生新值

```rust
struct Draft { id: u64, note: String }
struct Submitted { id: u64, note: String }
impl Draft {
    fn note(&self) -> &str { &self.note }
    fn rename(&mut self, text: &str) { self.note = text.into(); }
    fn submit(self) -> Submitted {
        Submitted { id: self.id, note: self.note }
    }
}
fn main() {
    let mut d = Draft { id: 1, note: "new".into() };
    d.rename("ready");
    assert_eq!(d.note(), "ready");
    let s = d.submit();
    assert_eq!(s.id, 1);
}
```

`note` 返回 `&str`，借用直接绑定到 `&self`；`rename` 使用 `&mut self` 修改对象；`submit` 消费 `Draft`，避免旧状态被继续调用。`Submitted` 又是一个不同类型，可以承载新的业务 API。类型状态模式由此产生，但不是所有状态机都适合拆成多个类型。

## 一种从业务约束推导出的类型状态 API

```rust
struct Draft { lines: Vec<u64> }
struct Submitted { total: u64 }
impl Draft {
    fn add(&mut self, cents: u64) { self.lines.push(cents); }
    fn submit(self) -> Option<Submitted> {
        let total = self.lines.into_iter().try_fold(0_u64, u64::checked_add)?;
        Some(Submitted { total })
    }
}
fn main() {
    let mut draft = Draft { lines: vec![100] };
    draft.add(30);
    let submitted = draft.submit().expect("总额溢出");
    assert_eq!(submitted.total, 130);
    // draft.add(1); // 预期编译失败：draft 已被消费
}
```

消费 `Draft` 的 `submit` 把“一张草稿不能提交两次”这一局部约束体现在类型转换中。它不能阻止客户端重新创建另一张内容相同的草稿，因此数据库侧仍需幂等键等业务机制。类型状态适用于状态数有限、转换边界明确的场景；如果状态组合爆炸，使用 `enum` 或运行时验证通常更实用。

## 何时返回引用，何时返回拥有值

所有权会影响函数可组合性：借用版函数通常适合阅读链路，消费版函数适合状态转换或资源移交。若你在模块间传递大量引用，生命周期约束可能传播到结构体和 Trait；有时改为拥有数据或小范围克隆反而降低整体复杂度。

```rust
fn normalize(mut s: String) -> String {
    s.make_ascii_lowercase();
    s
}
fn checksum(s: &str) -> usize { s.bytes().map(usize::from).sum() }
fn main() {
    let name = String::from("VIP");
    let before = checksum(&name);
    let lower = normalize(name);
    assert_ne!(before, checksum(&lower));
}
```

`checksum` 接受 `&str`，可作用于 `String` 和字符串字面量的视图；`normalize` 接受拥有的缓冲区并返回。这样的组合比所有函数都接受 `String` 更灵活。关键是看是否真的需要保留、修改或消费输入数据。

## 借用返回值会传播关系约束

```rust
struct Order { note: String }
fn note(order: &Order) -> &str { &order.note }
fn main() {
    let o = Order { note: "new".into() };
    let n = note(&o);
    println!("{n}");
}
```

这里返回值不是“独立的字符串”，它的有效性受 `o` 的借用关系约束。若需要在原订单被释放后继续保存标签，改成 `fn note_owned(&Order) -> String` 并克隆数据反而更符合需求。返回引用可以减少复制，却也提高调用者的生命周期耦合；返回拥有型数据增加成本，却换来更独立的使用边界。

## 用签名判断“借出的是整个订单还是一个字段”

假设计算金额时需要订单价格列表，更新备注时只需要可变借用备注。若两个函数都接受 `&mut Order`，调用方很难在同一片逻辑中保留金额的借用同时更新备注；如果让函数接受更小的字段视图，就可能实现无冲突组合。这个现象说明 Rust API 的精细程度会影响整个系统的可组合性。

```rust
struct Order { prices: Vec<u64>, note: String }
fn total(prices: &[u64]) -> u64 { prices.iter().sum() }
fn rename(note: &mut String, name: &str) { *note = name.into(); }
fn main() {
    let mut order = Order { prices: vec![10, 20], note: "old".into() };
    let prices = &order.prices;
    rename(&mut order.note, "new");
    assert_eq!(total(prices), 30);
}
```

这里两个字段的访问路径彼此独立；若 `rename` 接受 `&mut Order`，即使函数内部只动 `note`，调用点也可能与仍活跃的整体借用冲突。因而不要为了省事把所有函数都设计成“接受一个很大的上下文对象”。权限范围越清晰，编译器和读者越容易看到组件之间的独立性。

## 状态转换接口的设计边界

类型状态模式可以让 `Draft::submit(self)` 之后不能再按草稿操作，但对于可取消、可退款、部分发货等复杂流程，类型组合数量可能迅速膨胀。此时用 `enum OrderStatus` 搭配校验函数更现实。API 能阻止一部分非法调用，却不能独自保证不同数据库事务之间的顺序；不要把类型状态模式应用到每个领域状态上。

最后设计一张函数表，列出 `inspect`、`rename`、`submit`、`enqueue`、`save` 的接收者类型与返回类型。若两个函数看起来做同一件事，但一个消费所有权而另一个借用，就要在文档里说明为什么它们允许不同的调用序列。

## 类型状态如何阻止错误的业务调用

Java 里的方法参数通常不会表现对象引用的独占借用；C++ 可以用 `const T&`、`T&&` 等表达部分相近意图，但不具备统一借用检查；Go 常通过值、指针与接口组合表达能力。Rust 的函数签名会直接参与编译期资源验证。

`&mut T` 不是简单的“允许修改”标记，还表达该借用期间的独占访问要求。`&T` 也不保证对象内部绝无可变状态，因为 `Cell`、`Mutex` 等提供受控内部可变性。API 设计必须考虑这些例外，避免将语义过度简化。

## 一份 API 评审清单

针对每个公开函数问五个问题：参数是否需要被保存、是否需要被修改、调用后原数据是否应该继续可用、返回值是否依赖输入、发生错误时所有权是否需要返还。把这五项写成表格再决定 `T`、`&T`、`&mut T`、`Cow<'a, T>` 或 `Arc<T>`，通常比“先写成 String，编译不通过就 clone”更稳妥。

C++ 中 `const&` 和右值引用可以表达相似意图，但不会普遍验证 Rust 的借用限制；Java 的对象引用语义则不会在普通函数签名中展示独占访问。这是 Rust API 风格不同的根本原因之一。

## 设计一个可审计的订单 API

1. 把 `rename` 改成消费 `self` 并返回新的 `Draft`，比较调用方式与表达力。
2. 写 `fn label(&Order) -> &str`，说明返回引用和对象生命周期的关系。
3. 给一个需要持久保存订单的队列设计 `push(Order)` 与 `push(&Order)`，解释为什么队列通常应拥有其保存的数据。

第二篇收束为一个原则：Rust 的 API 契约除了值类型，还包含所有权流动。下一篇开始回答最难的问题之一：既然资源有所有者，为什么还允许别人临时访问它？

**相关阅读**：[book](https://doc.rust-lang.org/book/); [ref](https://doc.rust-lang.org/reference/)。

---

[← 上一篇](07-drop-and-scope.md) · [系列目录](../00-introduction.md) · [下一篇：为什么每次访问都转移所有权并不合理？](../part-3/09-borrow-instead-of-move.md)
