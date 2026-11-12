---
title: "14 为什么数据结构必须明确拥有还是借用内部数据？"
sidebar_position: 14
description: "为什么数据结构必须明确拥有还是借用内部数据？沿问题、代码、运行机制与设计取舍逐步推导。"
slug: /rust-design/14-own-or-borrow-in-data-structures
---

# 14 为什么数据结构必须明确拥有还是借用内部数据？

> **第 4 篇 · 为什么所有权会改变数据结构与状态管理？**  
> 本文聚焦一个因果问题：为什么数据结构必须明确拥有还是借用内部数据？；代码示例以稳定 Rust 的通用语法为主（适用于 2021/2024 Edition），除非明确注明为示意或故意无法编译的例子。

业务上“订单有一个客户名字”只是一种关系描述，代码却必须决定名字存放在哪里、是否独立拥有以及能否脱离原客户数据存在。Rust 在数据模型建立时就把这件事推到设计者面前：字段是 `String` 还是 `&str`，意义截然不同。

## 对象关系到底是拥有还是查看

长期持有引用的结构体可能把其生命周期约束传播到所有使用者。另一方面，全部复制也可能浪费内存。关键不是崇拜“零拷贝”，而是找到数据真实的责任边界：是否需要独立序列化、缓存、跨线程移动或在原数据变化后仍保持快照。

常见建模选择是：**拥有的字段** `String`、`Vec<T>` 适合独立实体；**借用字段** `&'a str`、`&'a [T]` 适合视图；**共享所有权字段** `Rc<T>`/`Arc<T>` 适合多方长期持有。三者表达的是资源关系，而不只是访问速度不同。

## 不同对象关系意味着不同的资源责任

订单包含订单明细是一种强的值组合关系：删除订单通常也释放其明细。订单引用一份用户档案则未必表示它拥有用户，尤其当多个订单共享同一个客户记录时。用同样的“has-a”自然语言描述这两种关系，会掩盖重要区别。Rust 的字段类型迫使设计者选择 `Customer`、`&Customer` 或 `Arc<Customer>`，于是资源责任就变得可见。

不要把“字段是 `&T`”误认为“没有成本的通用方案”：它可以避免复制，但会把引用的生命周期传播到整个结构体和所有存储该结构体的地方。在跨异步任务、缓存或长寿命组件中，传播的约束可能比一次明确克隆更昂贵。

## 带引用字段会把约束传播给谁

```rust
struct Order { customer: String, cents: u64 }
impl Order {
    fn summary(&self) -> String {
        format!("{}: {}", self.customer, self.cents)
    }
}
fn main() {
    let order = Order { customer: "Ada".into(), cents: 500 };
    assert_eq!(order.summary(), "Ada: 500");
}
```

订单直接拥有 `customer` 的字符串内容，因此即使创建它的函数已经返回，订单值仍可以被移动到队列中。`summary` 通过 `&self` 读取数据，返回新的 `String`；若需要避免分配，可以设计借用字段的只读视图或使用格式化输出接口。

## 一份拥有型快照与一份借用型视图

```rust
struct Customer { name: String }
struct CustomerView<'a> { name: &'a str }
struct Receipt { customer_name: String }
fn view(c: &Customer) -> CustomerView<'_> {
    CustomerView { name: &c.name }
}
fn snapshot(c: &Customer) -> Receipt {
    Receipt { customer_name: c.name.clone() }
}
fn main() {
    let customer = Customer { name: "Alice".into() };
    let current = view(&customer);
    println!("{}", current.name);
    let saved = snapshot(&customer);
    drop(customer);
    println!("{}", saved.customer_name);
}
```

视图只能在原客户数据有效时使用；收据快照复制了名字，因此不再依赖客户对象的生存期。对审计日志而言，保存当时的快照可能恰恰是正确业务语义，不是低效的“多余克隆”。反过来，大量短暂的格式化展示使用借用更自然。

## 数据快照与借用视图的取舍

所有权字段使业务对象更容易独立移动和持久化；借用字段适合解析器、临时计算视图；共享所有权适合缓存或多个订单共同引用同一客户。不同选择还影响是否能满足线程任务的 `'static` 约束、结构体是否需要生命周期参数以及序列化的复杂程度。

```rust
struct OrderView<'a> { customer: &'a str, cents: u64 }
fn preview<'a>(customer: &'a str, cents: u64) -> OrderView<'a> {
    OrderView { customer, cents }
}
fn main() {
    let customer = String::from("Ada");
    let view = preview(&customer, 500);
    assert_eq!(view.customer, "Ada");
    assert_eq!(view.cents, 500);
}
```

`OrderView` 无需拥有客户名的堆缓冲区，很适合只在输入有效期间使用的零拷贝视图；但不能安全地在原字符串销毁后保留。注意结构体上的生命周期参数并不为它分配额外存储，而是描述字段引用的有效性约束。

## 为什么把对象同时放在内部又借用内部很难

设想 `struct S { text: String, view: &str }`，希望 `view` 永远指向同一个 `S.text`。当整个 `S` 被移动，内部引用可能失效；即使 `String` 的堆缓冲区通常不会因为移动 String 头部而改变地址，仍涉及自引用初始化、生命周期和修改时重新分配等复杂不变量。Rust 不允许通过普通安全引用轻易构造这种通用自引用结构。

更好的第一选择往往是避免保存冗余引用：需要时调用 `self.text.as_str()`；或者保存索引/范围，在使用时重新借用；必须保持地址敏感关系时，才考虑专门的 Pin 设计和经过审查的封装。这个问题会在 Future 与 Pin 章节重新出现。

## 订单模型中“关联”有至少三种不同含义

订单明细属于订单本身，通常适合 `Vec<OrderItem>` 直接拥有；客户记录属于外部客户系统，订单可能只保存一个 `CustomerId`；历史收据则通常应保存提交时客户名称的快照，而不是每次展示都查询当前客户资料。如果把这些业务关系都表达成指向 `Customer` 对象的引用，就会把快照、关联标识和共享内存三种语义混为一谈。

```rust
#[derive(Clone, Copy, Debug)]
struct CustomerId(u64);
struct Item { cents: u64 }
struct Order { customer: CustomerId, items: Vec<Item> }
struct Receipt { customer_name_at_purchase: String }
fn total(order: &Order) -> u64 {
    order.items.iter().map(|i| i.cents).sum()
}
```

这个模型没有在 `Order` 中保存 `&Customer`，因为业务更需要稳定身份而不是对内存对象的临时访问。跨数据库、网络和服务边界时，ID 往往比内存地址更有意义。所有权的严格约束反而促使我们厘清领域关系，从而减少并不必要的对象图。

## 自引用与双向对象图的替代方案

如果每个订单都要引用客户，而客户还保存订单列表，直接让两个结构体互相包含引用会产生复杂生命周期甚至自引用难题。可以用数据库关系、索引数组、ID 表或者 `Rc/Weak` 在不同场景下表达关系。它们的关键区别是：是否需要真实共享同一个内存对象？是否跨线程？是否会形成引用环？

练习把客户-订单关系写成 `HashMap<CustomerId, Customer>` 与 `HashMap<OrderId, Order>` 两个表，由 ID 进行关联。再比较 `Rc<RefCell<...>>` 对象图在更新、序列化和长期持久化时的复杂度。不是任何面向对象的“对象引用”都应该直接映射成 Rust 的 `&T`。

## 自引用结构为何格外困难

Java 的普通对象字段通常持有托管引用；Go 的字符串、slice 等值也具有不同的底层表示与共享特性；C++ 常用值、引用、`shared_ptr`；Rust 则要求借用字段所需的生命周期关系明确可检查。概念上“组合”相似，资源语义却不相同。

不要机械地将每个 `String` 换成 `&str` 来追求性能：在跨异步任务、缓存和复杂返回值时，拥有字段有时更简单且总体更高效。也不要把 `Arc` 作为万能解决方案，原子引用计数与共享状态都具有成本和架构含义。

## 对比对象图与值树

树状数据通常天然适合独占所有权：父节点拥有 `Vec<Child>`，遍历时借用子节点。如果要表达任意图：多个父节点共享一个节点、节点又可反向引用父节点，就要面对所有权共享、引用环、内部可变性和节点标识。`Rc<RefCell<Node>>` 可以处理部分单线程图结构，但不是唯一方案。用索引存储 `Vec<Node>` 加邻接表，往往更容易追踪关系，也能避免引用环。

作业：分别画出订单明细树、客户-订单双向图和不可变收据快照，说明哪一部分真的需要共享引用，哪一部分适合值组合。只有在对象关系明确后，智能指针选择才有依据。

## 为订单模型画出资源关系图

1. 定义拥有 `String` 的 `Order` 与借用 `&str` 的 `OrderView`，比较它们能否从创建者函数返回。
2. 对一个需要在队列中停留一分钟的订单，解释为何拥有数据更自然。
3. 增加 `fn view(&Order) -> OrderView<'_>`，说明返回视图的有效期由什么决定。

数据结构不只决定字段布局，也决定资源责任。下一章讨论当多个实体必须长期共享同一数据时，为什么单一所有权与短期借用都不够用。

**相关阅读**：[book](https://doc.rust-lang.org/book/); [ref](https://doc.rust-lang.org/reference/)。

---

[← 上一篇](../part-3/13-borrow-checker-limits.md) · [系列目录](../00-introduction.md) · [下一篇：为什么单一所有权不能表达所有对象关系？](15-shared-ownership-rc-arc.md)
