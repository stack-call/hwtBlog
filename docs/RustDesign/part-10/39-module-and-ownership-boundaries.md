---
title: "39 为什么 Rust 程序需要同时设计模块边界和所有权边界？"
sidebar_position: 39
description: "为什么 Rust 程序需要同时设计模块边界和所有权边界？沿问题、代码、运行机制与设计取舍逐步推导。"
slug: /rust-design/39-module-and-ownership-boundaries
---

# 39 为什么 Rust 程序需要同时设计模块边界和所有权边界？

> **第 10 篇 · 这些设计最终如何塑造 Rust 程序的架构？**  
> 本文聚焦一个因果问题：为什么 Rust 程序需要同时设计模块边界和所有权边界？；代码示例以稳定 Rust 的通用语法为主（适用于 2021/2024 Edition），除非明确注明为示意或故意无法编译的例子。

把订单、计价、仓储和网络适配器拆成模块，并不会自动得到好架构。一个模块可能虽然依赖 Trait，却把整个数据库连接或全局共享状态暴露出去；另一模块也可能为了避免生命周期参数而把所有数据复制多份。Rust 要求我们同时考虑职责边界与资源流向。

## 为什么模块边界也是数据边界

模块边界回答“谁应该知道哪些业务细节”，所有权边界回答“谁应负责管理和修改哪些数据”。两者需要配合：领域模型可以拥有自己的状态，应用服务借用规则对象，基础设施适配器实现存储契约，线程任务接收拥有的数据或共享句柄。过多跨模块借用可能令生命周期耦合扩散；过多全局共享又会带来锁与依赖问题。

选择 API 时先从业务需求出发，再明确资源关系：查询通常通过共享借用返回拥有的 DTO 或有限生命周期的视图；命令通常需要可变访问或消费输入；需要长期运行的后台任务应该明确拥有其任务消息。Trait 用于隔离变化点，不能替代状态归属设计。

## 把订单系统分成三种责任

领域层负责订单状态、金额和业务约束；应用层负责协调“校验 → 定价 → 保存 → 发布结果”的流程；基础设施层负责数据库、文件或网络。Rust 的模块系统与任何成熟语言一样可以组织文件，但它的额外要求是：跨层传递的数据到底由谁拥有？服务是否只借用一个 `Order`？持久化函数是否需要消费订单？长时间运行的任务又持有哪份依赖？

一个好的模块边界应尽量避免无意义的生命周期传播。比如应用服务通常拥有仓库实现，在单次调用中只借用它；保存订单时仓库可能只需要 `&Order`，不必夺走领域模型的所有权。

## 订单系统怎样从单文件演化

```rust
struct Order { id: u64, cents: u64 }
trait Repository {
    fn save(&mut self, order: Order) -> Result<(), String>;
}
struct MemoryRepo { data: Vec<Order> }
impl Repository for MemoryRepo {
    fn save(&mut self, order: Order) -> Result<(), String> {
        self.data.push(order); Ok(())
    }
}
fn submit<R: Repository>(repo: &mut R, order: Order) -> Result<(), String> {
    repo.save(order)
}
fn main() {
    let mut repo = MemoryRepo { data: vec![] };
    submit(&mut repo, Order { id: 1, cents: 100 }).unwrap();
    assert_eq!(repo.data[0].id, 1);
    assert_eq!(repo.data[0].cents, 100);
}
```

`submit` 只依赖 `Repository` 契约，仓储 `save` 消费 `Order`，因此把订单持久保管责任交给实现方。`&mut R` 则允许仓储更新自身状态而不移动仓储对象。真实仓储还要考虑网络故障、事务、幂等和重试，但数据交接契约已经清楚。

## 一个最小的端口与适配器示例

```rust
#[derive(Clone)]
struct Order { id: u64, cents: u64 }
trait OrderRepository {
    fn save(&mut self, order: &Order) -> Result<(), String>;
}
struct MemoryRepo { rows: Vec<Order> }
impl OrderRepository for MemoryRepo {
    fn save(&mut self, order: &Order) -> Result<(), String> {
        self.rows.push(order.clone());
        Ok(())
    }
}
fn submit<R: OrderRepository>(repo: &mut R, order: &Order) -> Result<(), String> {
    if order.cents == 0 { return Err("金额不能为零".into()); }
    repo.save(order)
}
fn main() {
    let mut repo = MemoryRepo { rows: vec![] };
    submit(&mut repo, &Order { id: 1, cents: 100 }).unwrap();
    assert_eq!(repo.rows.len(), 1);
}
```

应用流程依赖 Trait，不依赖某种数据库；内存适配器为了长期保存订单，克隆了数据，这是其存储责任，而不是应用层滥用 clone。换成真实数据库时，保存过程可能序列化 `&Order` 并返回结果，未必需要克隆整份对象。API 的所有权形状可以随着持久化模型调整，但应在边界处显式决定。

## Trait 把业务依赖转向抽象

模块结构会受到编译规则影响：暴露 `&'a T` 的 API 把生命周期约束传给上层；返回 `T` 往往减轻长期耦合；跨线程背景任务常选择 `Arc` 共享配置、通过 channel 交接任务消息。好的架构不是“借用越多越好”，而是依赖方向、资源边界和失败路径一致。

```rust
trait Pricing { fn total(&self, original: u64) -> u64; }
struct Vip;
impl Pricing for Vip { fn total(&self, original: u64) -> u64 { original * 9 / 10 } }
fn settle(order: &Order, pricing: &dyn Pricing) -> u64 {
    pricing.total(order.cents)
}
fn main() {
    let order = Order { id: 9, cents: 1000 };
    assert_eq!(settle(&order, &Vip), 900);
}
```

计价策略只需借用订单、策略接口使用动态分派以支持装配时选择。这种抽象允许把策略置于不同模块，但如果只有固定一个 VIP 算法，增加 `dyn Trait` 也可能没有必要。架构设计首先识别真实变化点，再决定是否需要运行时可替换性。

## 为什么不把所有组件都包装成 `Arc<Mutex<_>>`

全局共享锁虽然容易让各模块“都拿得到状态”，却会模糊责任、扩大争用，还容易造成锁顺序问题。对于纯计算的计价策略，`&dyn Pricing` 或泛型引用就足够；对于需要独立运行的后台任务，可以显式传入拥有型命令；对长期只读配置，`Arc<Config>` 比 `Arc<Mutex<Config>>` 更准确。架构不应该因怕借用错误而把所有资源塞进同一种容器。

Trait Object 适合运行时选择数据库实现；泛型适合编译时已知的实现并易于测试；enum 适合有限的不同状态。抽象方式应由变化点决定，而不是被“所有东西都必须是 Trait”这样的习惯支配。

## 让模块依赖方向与资源责任一致

在一个成熟订单项目里，领域对象不应该必须知道数据库连接池的具体类型。应用层需要的是“能够保存订单”的契约；基础设施层才负责数据库序列化和事务。Rust 的 Trait 适合描述这种稳定行为，但仍要决定保存方法接受 `&Order` 还是 `Order`。前者表示只借用，持久化层不能无条件长期保存这个引用；后者表示交接拥有权，可将订单移动进内部工作队列。

```rust
trait Store {
    type Error;
    fn save(&mut self, id: u64, bytes: &[u8]) -> Result<(), Self::Error>;
}
struct BufferStore(Vec<u8>);
impl Store for BufferStore {
    type Error = ();
    fn save(&mut self, _id: u64, bytes: &[u8]) -> Result<(), ()> {
        self.0.extend_from_slice(bytes);
        Ok(())
    }
}
fn main() {
    let mut store = BufferStore(vec![]);
    store.save(1, b"paid").unwrap();
    assert_eq!(store.0, b"paid");
}
```

这里的应用只暴露借用的字节视图，具体适配器决定是否复制并长期保存。在换成真正的数据库时，连接池和事务对象可以留在适配器内部，不必穿过领域层。抽象边界越窄，所有权关系越容易维护。

## 生命周期如何反过来影响架构选择

假设一个长期存活的仓库对象内部保存了请求级的 `&Order`，那么它本身的生存期也被请求限制，难以放入全局服务容器。这经常意味着你的模块边界选错了：仓库通常应保存订单的拥有型数据、持久化记录或独立 ID，而不是把临时借用带到跨请求状态中。

练习把内存适配器 `MemoryRepo` 改为拥有型存储，再尝试借用型存储，对比类型中是否出现生命周期参数，以及调用者是否能在原订单释放后继续访问已保存内容。最后写出你选择前者或后者的业务理由。


## 依赖注入的 Rust 风格并不要求一个大型容器

对于一个结算服务，可以让结构体直接拥有仓库实现 `CheckoutService<R: OrderRepository> { repo: R }`，在构造时传入具体类型；也可以保存 `Box<dyn OrderRepository>`，以换取运行时灵活切换实现。二者都属于从外部装配依赖，不需要强制引入反射容器或宏框架。前者保留具体类型，后者通过 Trait Object 隐藏它。

更重要的是仓库的生命周期与服务的生命周期应该一致：如果服务长期存在，它通常应该拥有仓库句柄或共享池，而不是借用某个请求局部变量。对单次请求的订单对象则恰好相反，服务一般只需要临时 `&Order`。在设计上先区分长寿命依赖与短寿命请求数据，许多生命周期错误就会变成更清晰的资源模型选择。

## 所有权如何在层与层之间流动

Java/C# 服务常通过依赖注入容器组织对象依赖，Go 使用 interface 与显式装配，C++ 使用模板、抽象类与 RAII；Rust 也支持这些结构性思想。它的额外考量在于所有权和借用会影响依赖对象如何生存，以及异步任务如何获得长期有效的数据。

过度以生命周期贯穿每一层可能形成难维护的泛型结构；一味使用 `Arc<Mutex<_>>` 则可能退化为隐式共享状态系统。不要把“所有权图”当作业务架构的完整替代，它必须与内聚、职责划分、事务边界和测试策略共同设计。

## 从单文件迁移到模块的操作步骤

先把领域模型和金额类型放进 `domain`，公开最少的构造与查询接口；然后将流程放到 `application`，只依赖仓库 Trait；最后让 `infrastructure` 实现文件或数据库存储，在 `main` 中装配具体实例。每迁移一步就运行测试，确保所有权边界与 API 语义没有改变。

练习要求：不能让领域层直接导入数据库客户端；订单 ID 和金额都使用明确的新类型；保存失败由 `Result` 返回；至少有一个内存仓库测试替身。做到这些，比机械创建 `domain/`、`service/`、`repository/` 文件夹更能证明架构已经解耦。

## 避免把数据库句柄传遍所有模块

1. 将 `MemoryRepo` 改为文件仓储，保持 `Repository` 契约不变，并讨论错误类型设计。
2. 比较让 `save` 接受 `Order` 与 `&Order`：到底谁承担数据持久保存的责任？
3. 画一张系统图，同时标记模块依赖方向和每种重要数据的拥有者。

良好的 Rust 架构需要同时回答“谁负责什么业务”与“谁拥有和访问什么资源”。最后一章把所有设计放回现实选择：何时借用、何时克隆、何时共享，何时使用静态或动态抽象。

**相关阅读**：[book](https://doc.rust-lang.org/book/); [cargo](https://doc.rust-lang.org/cargo/)。

---

[← 上一篇](../part-9/38-zero-cost-abstractions.md) · [系列目录](../00-introduction.md) · [下一篇：为什么没有一种所有权、抽象或共享策略适合所有场景？](40-no-one-strategy.md)
