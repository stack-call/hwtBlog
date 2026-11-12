---
title: "19 为什么不同类型需要实现共同的行为契约？"
sidebar_position: 19
description: "为什么不同类型需要实现共同的行为契约？沿问题、代码、运行机制与设计取舍逐步推导。"
slug: /rust-design/19-trait-contracts
---

# 19 为什么不同类型需要实现共同的行为契约？

> **第 5 篇 · 为什么 Rust 的类型系统不只描述数据类型？**  
> 本文聚焦一个因果问题：为什么不同类型需要实现共同的行为契约？；代码示例以稳定 Rust 的通用语法为主（适用于 2021/2024 Edition），除非明确注明为示意或故意无法编译的例子。

普通、VIP 和企业定价都在计算订单金额，但算法不同。如果结算服务写死每个具体策略，增加一个策略就要改动分支。我们希望结算逻辑只依赖“能根据金额计算新金额”的契约，具体策略在其他地方实现。

## 代码想复用的是能力，而不一定是结构

这是一种抽象边界问题：业务流程关心能力，而不必知道所有实现的内部结构。Trait 将操作签名和部分默认行为组织成契约，impl 把具体类型与契约连接起来。Rust 不需要通过类继承共享字段布局来完成这种行为抽象。

定义 Trait 时需要决定方法参数、所有权关系、返回错误的方式，以及哪些实现应该保证相同业务语义。`&self` 适合纯读取配置的策略，`&mut self` 可表达带状态策略，`self` 可表达一次性消费策略。这说明行为接口和所有权契约不可分割。

## 接口的价值不是把 if 改个地方

如果每增加一个 VIP、满减或地区税费规则，都要修改核心 `checkout`，变化点就没有被隔离。抽象的目标是让结算流程只依赖“可以计算价格”这一能力，而每一种策略自己承担算法实现。Trait 在这里表达可被调用的行为契约，而不是一个必须存放公共字段的父类。

```rust
trait Pricing {
    fn apply(&self, cents: u64) -> u64;
}
struct Regular;
struct Vip { percent: u64 }
impl Pricing for Regular {
    fn apply(&self, cents: u64) -> u64 { cents }
}
impl Pricing for Vip {
    fn apply(&self, cents: u64) -> u64 {
        cents.saturating_mul(self.percent) / 100
    }
}
fn charge<P: Pricing>(rule: &P, cents: u64) -> u64 {
    rule.apply(cents)
}
fn main() {
    assert_eq!(charge(&Vip { percent: 90 }, 1000), 900);
}
```

示例使用 `saturating_mul` 只是避免乘法溢出 panic；正式金额计算需要明确溢出和舍入策略，不应默认饱和就是业务正确。接口抽象使策略可替换，但不代替业务契约的设计。

## 从新增定价策略的痛点出发

```rust
trait Pricing {
    fn apply(&self, cents: u64) -> u64;
}
struct Regular;
struct Vip;
impl Pricing for Regular {
    fn apply(&self, cents: u64) -> u64 { cents }
}
impl Pricing for Vip {
    fn apply(&self, cents: u64) -> u64 { cents * 9 / 10 }
}
fn checkout<P: Pricing>(p: &P, cents: u64) -> u64 {
    p.apply(cents)
}
fn main() { assert_eq!(checkout(&Vip, 100), 90); }
```

`checkout` 使用 `P: Pricing` 约束具体类型，但并未将 `Regular`、`Vip` 写入函数体。`impl` 给不同类型提供不同实现。注意货币计算简化为整数比例，真实业务要明确舍入规则、折扣叠加顺序与溢出处理，Trait 只建立接口，不替你制定这些政策。

## Trait 不只是“接口里列几个函数”

关联类型允许 Trait 实现指定与自身相关的类型：

```rust
trait Source {
    type Item;
    fn next(&mut self) -> Option<Self::Item>;
}
struct Counter(u64);
impl Source for Counter {
    type Item = u64;
    fn next(&mut self) -> Option<u64> {
        self.0 += 1;
        Some(self.0)
    }
}
```

`Iterator` 采用相似的关联类型思路，表达“某个具体迭代器产生什么元素”。这比把一切关系都塞进某个通用父类更贴合 Rust 的类型模型。Trait 还允许默认方法、超 Trait 约束，但并不等同于传统类实现继承。

## Trait 如何定义行为契约

Trait 让模块之间的依赖方向改变：高层结算逻辑可以依赖 `Pricing` 契约，低层实现可以独立替换。它也让标准库能够定义如 `Iterator`、`Read`、`Write` 等跨类型行为。与所有权相配合，Trait 方法可以要求借用、独占修改或消费接收者。

```rust
trait Repository {
    type Error;
    fn save(&mut self, id: u64) -> Result<(), Self::Error>;
}
struct MemoryRepo(Vec<u64>);
impl Repository for MemoryRepo {
    type Error = std::convert::Infallible;
    fn save(&mut self, id: u64) -> Result<(), Self::Error> {
        self.0.push(id); Ok(())
    }
}
```

关联类型 `type Error` 表达“每个存储实现选择自己的错误类型”，从而避免无意义的全局统一。`&mut self` 体现存储可能改变内部状态。Trait 的价值不仅是方法名字相同，还包括契约如何表达输入、输出、错误与资源关系。

## 为什么不能给任何外部类型随便实现任何外部 Trait

Rust 的一致性与孤儿规则大体限制：在你自己的 crate 中实现 Trait 时，Trait 或涉及的类型需要满足本地性要求。它避免两个完全无关的依赖库为同一外部类型提供冲突实现，导致组合依赖时出现难以预测的行为。对框架作者而言，这直接影响扩展点设计：要让用户扩展行为，可能定义自己的新类型包装外部类型，再为包装类型实现 Trait。

这是一种典型的语言层取舍：减少全局实现冲突，换来某些扩展写法的限制。不要把孤儿规则误解为“Rust 不支持插件”，它要求插件式扩展在类型和 crate 边界上更明确。

## Trait 的接口边界应稳定于实现变化

定义 `Pricing` 时，如果把普通 VIP 定价的细节写死进方法名，例如 `vip_discount_percent`，新加入优惠券或地区税规则就不自然。更好的契约往往表达稳定的需求：给定价格和必要上下文，计算一个结果，必要时返回明确的错误。接口应尽量少知道实现内部结构，但仍必须明确舍入、溢出和异常处理语义。

```rust
#[derive(Debug)]
enum PriceError { Overflow }
trait Pricing {
    fn total(&self, subtotal: u64) -> Result<u64, PriceError>;
}
struct Rate { numerator: u64, denominator: u64 }
impl Pricing for Rate {
    fn total(&self, subtotal: u64) -> Result<u64, PriceError> {
        let product = subtotal.checked_mul(self.numerator).ok_or(PriceError::Overflow)?;
        Ok(product / self.denominator.max(1))
    }
}
```

这是用于说明接口形状的简化实现，`denominator.max(1)` 只是避免除零，并不意味着输入为零是合法的业务利率。成熟 API 应在 `Rate::new` 中拒绝非法参数，而不是运行时悄悄改写它。Trait 能把能力抽象出来，但业务不变量仍要主动建立。

## 为什么通用的 Trait 不等于万能动态接口

带泛型方法的 Trait、关联类型或返回 `Self` 等设计可能不能直接作为 `dyn Trait` 使用，因为动态分派需要稳定的调用边界。设计 Trait 时先想它主要用于泛型约束，还是准备保存进 `Box<dyn Trait>`。不同用途会影响方法签名的选择。

练习给 `Pricing` 增加 `fn id(&self) -> &str`，再尝试在运行时列表中保存不同实现。最后说明何种方法需要返回拥有型值、何种可返回借用视图，以及这些选择如何影响 Trait Object 的使用期限。

## 默认方法和关联类型意味着什么

Java interface、Go interface、C++ concepts/抽象类和 Rust Trait 在能力抽象方面有相似动机，但实现规则、动态分派方式及继承能力不同。Rust 还有孤儿规则等实现一致性约束，用来限制跨 crate 的某些冲突实现。

并非任何共享方法名都需要抽成 Trait：如果只有一个实现、变化点不清楚，直接使用具体类型可能更简单。Trait 不自动带来依赖倒置，只有当模块确实依赖契约而不是具体实现时，抽象才有价值。

## Trait 契约不等于行为替换必然正确

编译器可以确认 `Vip` 提供 `apply` 方法，无法证明它在所有订单中都符合“金额不为负、折扣不会超过预算”等业务要求。里氏替换原则强调调用方依赖的行为约定，Rust Trait 只表达其中能够被类型系统约束的部分。

实验：新增 `Coupon` 策略，列出应放入 Trait 类型签名的约束（输入输出类型、错误表达），以及只能放入业务测试的约束（舍入规则、税费顺序、折扣叠加）。真正的抽象质量取决于契约是否准确，而不仅是是否使用了 Trait。

## 孤儿规则为何也属于模块设计

1. 为优惠券策略实现 `Pricing`，讨论百分比折扣与固定金额抵扣的舍入边界。
2. 把 `Pricing::apply` 改成返回 `Result<u64, PriceError>`，让异常输入有显式失败路径。
3. 解释为什么 `Repository::save` 采用 `&mut self`，而某些数据库连接 API 也可能选择 `&self` 配合内部同步。

Trait 帮助我们分离契约与实现。但还没有回答“同一算法复用”和“具体实现替换”是否属于同一种多态。下一章将把语义上的多态和执行层的分派区别开来。

**相关阅读**：[book](https://doc.rust-lang.org/book/); [ref](https://doc.rust-lang.org/reference/)。

---

[← 上一篇](18-types-constrain-behavior.md) · [系列目录](../00-introduction.md) · [下一篇：为什么相同算法与不同实现需要不同的多态机制？](20-static-vs-dynamic-dispatch.md)
