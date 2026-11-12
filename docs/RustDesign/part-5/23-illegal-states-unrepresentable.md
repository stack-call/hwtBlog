---
title: "23 为什么非法业务状态应该尽可能无法表达？"
sidebar_position: 23
description: "为什么非法业务状态应该尽可能无法表达？沿问题、代码、运行机制与设计取舍逐步推导。"
slug: /rust-design/23-illegal-states-unrepresentable
---

# 23 为什么非法业务状态应该尽可能无法表达？

> **第 5 篇 · 为什么 Rust 的类型系统不只描述数据类型？**  
> 本文聚焦一个因果问题：为什么非法业务状态应该尽可能无法表达？；代码示例以稳定 Rust 的通用语法为主（适用于 2021/2024 Edition），除非明确注明为示意或故意无法编译的例子。

订单可能是草稿、已支付或取消。若用 `is_paid` 和 `is_cancelled` 两个布尔值，就会出现“已支付且已取消”这样的含混组合。内存安全的订单仍可能业务错误。Rust 的 enum 与模式匹配可以把允许状态的边界明确写进类型。

## 非法状态为什么容易藏在布尔字段中

类型系统可以约束**数据形态**，不只是资源访问。`Option<T>` 表示可能缺失；`Result<T, E>` 表示可能失败；带数据的 `enum` 让不同状态携带不同信息。通过穷尽匹配，新增状态时编译器可以提醒处理逻辑尚未覆盖它。

“让非法状态不可表达”不是说所有规则都能静态解决。订单金额不超过信用额度、退款是否允许等通常仍需运行时检查。更现实的目标是先把明显互斥的状态做成类型，然后在状态转换函数中校验业务规则，并用 `Result` 把拒绝原因返回调用者。

## 两个布尔字段为何产生四种状态

如果订单包含 `is_paid` 与 `is_cancelled` 两个 bool，就存在四种组合。业务可能只认可“草稿、已支付、已取消”三种；第四种既已支付又已取消究竟代表什么？如果这种组合无定义，那么数据结构已经允许了非法状态。换成 `enum`，每个值恰好对应一个明确变体，还可以把仅在某状态下存在的数据放入该变体。

```rust
#[derive(Debug)]
enum Status {
    Draft,
    Paid { receipt_id: String },
    Cancelled { reason: String },
}
fn describe(s: &Status) -> &str {
    match s {
        Status::Draft => "待提交",
        Status::Paid { .. } => "已支付",
        Status::Cancelled { .. } => "已取消",
    }
}
```

如果将来增加 `Refunded`，这里的 `match` 需要同步处理新分支。编译期的穷尽检查迫使你审视所有使用方，这是一种维护成本，也是一种可靠性收益。

## enum 让状态空间可见

```rust
#[derive(Debug)]
enum Status {
    Draft,
    Paid { transaction_id: String },
    Cancelled { reason: String },
}
fn describe(status: &Status) -> &'static str {
    match status {
        Status::Draft => "draft",
        Status::Paid { .. } => "paid",
        Status::Cancelled { .. } => "cancelled",
    }
}
fn main() {
    let status = Status::Paid { transaction_id: "tx-1".into() };
    assert_eq!(describe(&status), "paid");
}
```

`Status::Paid` 必须携带交易标识；`Draft` 不会误带退款理由。`match` 要对所有变体给出处理方式，新加 `Refunded` 会让缺少分支的匹配产生编译错误。这里只读借用 `&Status`，因此不会移动 `String` 字段。

## 错误是业务结果的一种，而不是字符串旁注

```rust
#[derive(Debug)]
enum PayError { AlreadyFinalized, InvalidAmount }
fn pay(state: Status, cents: u64) -> Result<Status, PayError> {
    if cents == 0 { return Err(PayError::InvalidAmount); }
    match state {
        Status::Draft => Ok(Status::Paid { receipt_id: format!("R{cents}") }),
        _ => Err(PayError::AlreadyFinalized),
    }
}
fn main() {
    let state = pay(Status::Draft, 100).unwrap();
    assert_eq!(describe(&state), "已支付");
}
```

`Result<T,E>` 用类型明确成功和失败，调用者必须显式处理或选择放弃处理（如 `unwrap`）。`Option<T>` 则适合“没有值本身就是预期情况”，例如查找不到优惠券。区分“正常缺席”和“操作失败”，能让 API 的业务含义更清晰。

## Result 与 Option 如何表达失败路径

状态建模会改变 API：与其接受任意 `bool` 组合，不如接受 `Status`；与其用空字符串表达失败，不如返回 `Result`。所有权还允许把某个阶段的对象消费后转换成另一个类型，形成类型状态模式，但这种做法也可能增加泛型与转换成本。

```rust
#[derive(Debug, PartialEq)]
enum PayError { AlreadyFinished }
fn pay(status: Status, id: String) -> Result<Status, PayError> {
    match status {
        Status::Draft => Ok(Status::Paid { transaction_id: id }),
        _ => Err(PayError::AlreadyFinished),
    }
}
fn main() {
    let paid = pay(Status::Draft, "tx-2".into()).unwrap();
    assert!(matches!(paid, Status::Paid { .. }));
}
```

函数消费原状态并返回新的状态或错误值。`Result` 的成功/失败是互斥分支，调用者必须选择处理方式（也可以明确地 `unwrap`，在错误时 panic）。要注意这里错误分支会丢弃原输入状态；真实业务可能需要返回原状态以供恢复或重试。

## 静态状态机与运行时状态机的取舍

使用 `DraftOrder`、`PaidOrder` 两个独立类型，并让 `pay(self)` 消费草稿返回已支付类型，可以阻止某些重复调用；但是订单从数据库或网络恢复时，其状态只有运行时才能知道，此时使用 `enum` 加验证更自然。两种方案都可能正确，关键在于状态是否在编译期已经确定、调用链是否需要编译时排除某些操作。

不要认为用 enum 就自动防止支付重复：两个不同请求可能都读取到相同数据库草稿状态，随后同时提交。事务隔离、唯一约束与幂等键仍然必需。Rust 类型系统只能约束当前进程中的值操作，不能代替分布式业务一致性协议。

## 状态建模还会影响错误处理的粒度

假设支付 API 返回布尔值：`true` 表示成功，`false` 表示失败。但失败可能是库存不足、订单已取消、网络故障或金额非法，它们对应不同的重试与补偿策略。使用枚举错误能够保留这些差异，并让调用者用 `match` 明确处理。

```rust
#[derive(Debug, PartialEq, Eq)]
enum PayError { Cancelled, InsufficientStock, Network }
fn retryable(e: &PayError) -> bool {
    match e {
        PayError::Network => true,
        PayError::Cancelled | PayError::InsufficientStock => false,
    }
}
fn main() {
    assert!(retryable(&PayError::Network));
}
```

这里的错误不是“异常对象”或“返回码”的语法区别，而是业务状态空间的建模。新增错误变体时，穷尽匹配会提示需要更新相关处理；但是某些调用方也可能希望提供 `_` 兜底以保持兼容。是否公开 `#[non_exhaustive]` 等演化策略，会影响公共 API 的稳定性。

## 类型状态与 enum 不应混为一谈

类型状态模型把不同阶段做成不同的静态类型，适合一个对象的合法操作集随阶段发生明确变化的情况；enum 则在一个统一类型中表达运行时可能的多个状态。订单从网络反序列化时通常需要先判断运行时状态，因此 enum 更自然；从“构造 → 配置 → 启动”的资源 API 则可能适合类型状态，使忘记启动之前的校验步骤无法编译。

练习设计 `DraftOrder::submit(self) -> Result<PaidOrder, PayError>`，再设计 `OrderStatus` enum，用一个复杂的退款流程比较哪种方案会导致状态组合过多。不要认为越多状态进入类型系统就一定越好。


## 模式匹配的穷尽性有时是兼容成本

当你公开一个 enum，并在下个版本增加新变体时，下游代码里原本穷尽的 `match` 可能无法继续编译。从维护者角度看，这是提示调用方重新处理新业务状态的优势；但对稳定的公共库来说，它也可能成为破坏性升级。可以考虑在适当场合使用 `#[non_exhaustive]`，要求外部调用者为未来可能出现的情况保留兜底分支。

对于订单内部的状态机，允许编译器在新增退款状态时指出所有漏改的地方，通常十分有用；对于跨版本公共协议，未知状态的处理策略则需要提前设计。类型穷尽性不只影响程序是否安全，还影响系统如何演化。请为“订单待发货、已发货、退款中”新增一个状态，列出需要更新的匹配逻辑，再讨论是否应该让外部使用者看到这个具体变体。

## 模式匹配为什么需要穷尽检查

Java 的 sealed hierarchy 与现代 switch、Kotlin 的 sealed class、Swift 的 enum、Haskell 的代数数据类型都表达相近思想。Rust 不是独有“枚举”，它的特色之一是把这类模型与所有权、模式匹配和错误传播一起用于系统编程。

不要把所有运行时规则都塞进数十种类型状态：状态爆炸可能让 API 比业务本身还复杂。`Result` 不保证无人调用 `unwrap`；`enum` 不保证交易号真实存在。类型能排除部分非法表示，剩余正确性仍需验证和测试。

## 让模型随着需求演化

练习先定义 `bool` 版本订单，列出全部组合及其业务含义；再改为 enum，追加退款状态，并观察需要修改哪些 `match`。最后将 `pay` 的返回错误区分为“非法状态”和“金额非法”，为两种情况编写测试。对比 Java 的 sealed hierarchy、Kotlin 的 sealed class、TypeScript 的判别联合类型：重要的不是 Rust 独占这种思想，而是它与所有权和显式错误处理协同使用。

## 状态类型不能替代事务和业务验证

1. 为 `Status` 添加 `Refunded`，观察 `describe` 编译错误如何提示遗漏。
2. 将 `pay` 改为 `Result<Status, (PayError, Status)>`，在失败时归还原状态。
3. 比较两个布尔字段与一个三分支 enum 的状态空间，说明被排除了哪些组合。

第五篇收束为两条并行主线：类型系统既描述能力，也描述合法数据形态；所有权则描述资源责任。下一篇把它们带入并发，检验这些静态约束如何跨线程发挥作用。

**相关阅读**：[book](https://doc.rust-lang.org/book/); [ref](https://doc.rust-lang.org/reference/)。

---

[← 上一篇](22-copy-drop-deref.md) · [系列目录](../00-introduction.md) · [下一篇：为什么多个线程不能随意共享可变数据？](../part-6/24-shared-mutable-across-threads.md)
