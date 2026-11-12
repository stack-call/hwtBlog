---
title: "20 为什么相同算法与不同实现需要不同的多态机制？"
sidebar_position: 20
description: "为什么相同算法与不同实现需要不同的多态机制？沿问题、代码、运行机制与设计取舍逐步推导。"
slug: /rust-design/20-static-vs-dynamic-dispatch
---

# 20 为什么相同算法与不同实现需要不同的多态机制？

> **第 5 篇 · 为什么 Rust 的类型系统不只描述数据类型？**  
> 本文聚焦一个因果问题：为什么相同算法与不同实现需要不同的多态机制？；代码示例以稳定 Rust 的通用语法为主（适用于 2021/2024 Edition），除非明确注明为示意或故意无法编译的例子。

一个函数要取任意切片的第一个元素，不需要知道类型是什么；而不同定价策略的 `apply` 方法确实有不同算法。这两个场景常被统称为多态，却抽象了完全不同的变化：前者是类型变化，后者是行为实现变化。

## 多态是在抽象哪一种变化

经典分类包括参数化多态、特设多态、子类型多态；静态/动态分派是另一个维度。不要把“泛型 = 静态分派、接口 = 动态分派”当作定理：Rust Trait 可以参与泛型静态调用，也可以通过 Trait Object 提供动态调用；运行时分支也能通过 enum 完成。

先问**什么在变化**，再问**何时知道具体实现**。`fn first<T>` 保留精确类型关系；`T: Pricing` 说明每种类型有合适行为；`dyn Pricing` 以统一接口表示不同具体类型；`enum PricingKind` 在编译期枚举有限实现。它们不是互斥技术，而是不同抽象维度的组合。

## 先建立两条轴，避免分类混乱

经典理论中的参数化多态是“同一算法对不同类型工作”，特设多态是“相同操作可有依类型不同的实现”，子类型多态关注一种类型能否在另一种类型的上下文中合法替换。静态与动态分派则是在回答另一个问题：具体调用目标在编译时还是运行时决定。把“泛型、接口、虚函数”简单排成先进程度排序，会混淆语义和执行机制。

最典型的反例是 Rust 的 `T: Pricing`：它既使用泛型的参数化抽象，也依赖 Trait 实现提供具体行为，调用通常由静态分派处理。同一个 Trait 又可通过 `dyn Pricing` 支持动态分派。因此 Trait 是一种行为契约机制，不只是“动态多态的代名词”。

## 参数化多态怎样保留类型信息

```rust
fn first<T>(slice: &[T]) -> Option<&T> { slice.first() }
trait Pricing { fn apply(&self, cents: u64) -> u64; }
struct Vip;
impl Pricing for Vip {
    fn apply(&self, cents: u64) -> u64 { cents * 9 / 10 }
}
fn checkout<P: Pricing>(p: &P, cents: u64) -> u64 {
    p.apply(cents)
}
fn main() {
    assert_eq!(first(&[1_u8, 2]), Some(&1));
    assert_eq!(checkout(&Vip, 100), 90);
}
```

`first` 对元素能力几乎没有要求，体现参数化多态。`checkout` 对 `P` 附加了行为能力约束，通常会经单态化得到针对具体类型的实现，因此仍保留编译期具体类型信息。Trait 实现还体现“同一行为契约具有不同具体实现”的特设多态因素。

## 为什么 dyn Trait 能放进异构容器

```rust
trait Pricing { fn apply(&self, cents: u64) -> u64; }
struct Full; struct Discount;
impl Pricing for Full { fn apply(&self, c: u64) -> u64 { c } }
impl Pricing for Discount { fn apply(&self, c: u64) -> u64 { c / 2 } }
fn main() {
    let rules: Vec<Box<dyn Pricing>> = vec![Box::new(Full), Box::new(Discount)];
    let outputs: Vec<_> = rules.iter().map(|rule| rule.apply(200)).collect();
    assert_eq!(outputs, vec![200, 100]);
}
```

容器中每个 `Box` 都是大小已知的句柄，可以指向大小和布局不同的具体类型；`dyn Pricing` 通过动态分派元数据在运行时找到目标方法。`dyn Trait` 自身是动态大小类型，不能直接像普通定长元素那样存入 `Vec<dyn Pricing>`。典型实现使用数据指针与虚表指针，但不应把这些底层表示细节作为所有平台上的稳定 ABI 承诺。

## Trait、静态分派和动态分派的交叉点

这种分类直接影响 API：如果算法不需要具体行为，不必给 `T` 添加 Trait；如果需要运行时异构存储，可以使用 `Box<dyn Pricing>`；如果有限策略集合需要穷尽匹配，enum 可能更合适。把分派方式和多态语义混在一起，会导致错误性能判断。

```rust
trait Show { fn show(&self) -> String; }
impl Show for i32 { fn show(&self) -> String { format!("i={self}") } }
impl Show for bool { fn show(&self) -> String { format!("b={self}") } }
fn render(x: &dyn Show) -> String { x.show() }
fn main() {
    let values: Vec<Box<dyn Show>> = vec![Box::new(3), Box::new(true)];
    assert_eq!(values.iter().map(|x| render(x.as_ref())).collect::<Vec<_>>(),
               vec!["i=3", "b=true"]);
}
```

`Box<dyn Show>` 统一保存不同具体类型的对象，方法通常通过动态分派查找实现。这里还需要 Box 提供大小已知的间接句柄，因为 `dyn Show` 是动态大小类型。动态分派解决运行时异构集合问题，并非“比泛型更面向对象”的价值判断。

## 为什么 enum 可以避免动态 Trait Object

```rust
#[derive(Clone, Copy)]
enum Rule { Full, Half }
impl Rule {
    fn apply(self, cents: u64) -> u64 {
        match self { Rule::Full => cents, Rule::Half => cents / 2 }
    }
}
fn main() {
    let selected = if std::env::args().len() > 1 { Rule::Half } else { Rule::Full };
    println!("{}", selected.apply(200));
}
```

这个例子同样可以在运行时选择策略，却没有 `dyn Trait`。区别在扩展模型：enum 的变体集合在定义处封闭，新增策略通常要修改枚举；Trait 的实现集合更加开放，新的 crate 可以在满足实现规则的前提下增加实现。运行时分支不等于动态分派，这一区分是理解 Rust 抽象设计的关键。

## 把多态放到真实配置系统里试一次

假设服务从配置文件读取 `pricing = "vip"`。在程序启动后才能知道策略，但策略集合实际上只有普通和 VIP 两种。如果使用 enum，配置解析可返回 `Result<Rule, ParseError>`，之后通过 `match` 调用对应算法；这种设计无需每次分配 `Box<dyn Pricing>`，但新增一种策略要修改枚举与匹配位置。若策略来自外部插件或不同 crate，则 Trait Object 可以减少中央枚举的维护需求。

```rust
#[derive(Debug)]
enum Rule { Regular, Vip }
fn parse_rule(s: &str) -> Option<Rule> {
    match s { "regular" => Some(Rule::Regular), "vip" => Some(Rule::Vip), _ => None }
}
fn price(rule: Rule, cents: u64) -> u64 {
    match rule { Rule::Regular => cents, Rule::Vip => cents * 9 / 10 }
}
fn main() { assert_eq!(price(parse_rule("vip").unwrap(), 100), 90); }
```

运行时决定配置并不意味着语言必须执行动态方法分派。选择何种抽象，要把“配置何时知道”和“实现集合是否开放”分开。

## Trait Object 与泛型的接口发展路径

最初项目只有一个 `Vip` 实现，`fn checkout<P: Pricing>(&P)` 简单且可优化。后来需要在一个集合中保存多个不同实现，`Vec<Box<dyn Pricing>>` 才有了明确价值。再后来系统要求插件热替换，就还要考虑对象所有权、共享引用、线程同步和插件生命周期；`dyn Trait` 只解决行为调用统一问题，不自动保证插件热更新安全。

研究性能时，应同时对照内存分配、vtable 调用、代码体积、接口复杂度与变更成本。静态泛型在小型热循环里可能很有价值，但给请求响应增加一个数据库查询就可能抵消所有微观收益。多态机制的“正确选择”取决于系统真正的变化点。


## 一个设计评审中最容易出现的错误问题

开发者经常问：“该用泛型还是接口？”这个问题省略了两个重要维度。首先接口契约与泛型并不冲突：`fn f<T: Pricing>(x: T)` 就同时使用泛型和 Trait。其次即使需要运行时选择策略，也不必总是动态 Trait Object：固定集合的 `enum Rule` 可以在运行时通过 `match` 选择实现。正确的设计评审应该先问实现集合是否开放、调用者是否需要保留具体类型、是否要存放异构集合、是否处于性能热点。

如果业务规则由程序自身维护且只有三种，枚举更容易保证穷尽处理；如果允许第三方 crate 添加符合契约的实现，Trait + dyn 可以减少中央分支表；如果调用处知道具体策略类型，并且计算路径非常频繁，泛型静态分派可能更符合目标。不同选择还会影响二进制体积、编译时间与 API 稳定性。把这些成本都写入决策记录，比单纯讨论“虚表慢不慢”更深入。

## enum 和 Trait Object 是不同的扩展策略

Java 的接口调用常见动态分派；C++ 模板通常保留具体类型，virtual 提供运行时分派；Go 的 interface 支持动态行为调用，泛型则满足另一类复用需求。Rust 将 Trait Bound、Trait Object 和 enum 等并置，要求我们按场景选择，而不是依赖单一层次结构。

静态分派通常有利于内联，却可能增加代码体积；动态分派有间接调用成本，但编译器有时能去虚拟化。Trait Object 也有对象安全（dyn compatibility）等限制。不要推断“只要动态就是慢，泛型一定最快”，应根据热点路径测量。

## 性能取舍不能只看“动态”两个字

泛型单态化可能为不同类型生成不同代码，有利于内联，但增加编译时间与二进制大小；动态分派通常有间接调用成本，可能阻碍某些优化，也可能减少重复实现。编译器有时能够把动态调用去虚拟化。对真实订单系统，更大的性能开销可能来自数据库或网络，而不是一次 Trait 方法调用。

练习同时实现 `fn pay<P: Pricing>(&P)`、`fn pay(&dyn Pricing)` 和 `enum Rule` 三个版本，记录代码行数、异构容器表达能力和扩展规则，然后基准测试大量纯计算场景。只有把“设计开放性”和“运行时成本”分开，选择才有依据。

## 用成本和变化频率选择机制

1. 把 `checkout<P: Pricing>` 改成 `checkout(&dyn Pricing)`，比较调用端和存储异构列表的差别。
2. 使用 enum 定义 `Regular | Vip`，在 `match` 中分派，实现相同结果。
3. 解释 `first<T>`、`T: Pricing`、`dyn Pricing` 分别保留和隐藏了哪些类型信息。

多态机制为不同变化点提供工具，而不是竞争的“高级程度”。下一章从所有权出发，研究为什么 Rust 的闭包和迭代器需要不同的调用与消费能力。

**相关阅读**：[book](https://doc.rust-lang.org/book/); [ref](https://doc.rust-lang.org/reference/)。

---

[← 上一篇](19-trait-contracts.md) · [系列目录](../00-introduction.md) · [下一篇：为什么所有权会影响闭包和迭代器？](21-closures-and-iterators.md)
