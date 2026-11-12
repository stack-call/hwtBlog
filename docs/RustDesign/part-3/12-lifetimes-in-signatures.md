---
title: "12 为什么生命周期需要参与函数和结构体的类型表达？"
sidebar_position: 12
description: "为什么生命周期需要参与函数和结构体的类型表达？沿问题、代码、运行机制与设计取舍逐步推导。"
slug: /rust-design/12-lifetimes-in-signatures
---

# 12 为什么生命周期需要参与函数和结构体的类型表达？

> **第 3 篇 · 为什么拥有资源不等于必须独占所有访问？**  
> 本文聚焦一个因果问题：为什么生命周期需要参与函数和结构体的类型表达？；代码示例以稳定 Rust 的通用语法为主（适用于 2021/2024 Edition），除非明确注明为示意或故意无法编译的例子。

一个函数接收两段字符串并返回较长的一段：返回引用可能来自第一个输入，也可能来自第二个输入。类型如果只写 `&str`，编译器需要知道返回引用与哪个输入相关。两个输入同时存在时，这种关系通常需要显式表示。

## 生命周期参数究竟描述了什么关系

生命周期参数不是计时器，而是类型层面的关系变量。`'a` 表示输入引用和输出引用之间需要满足兼容的有效期；它不会改变任何变量的实际销毁时刻。对于结构体，若字段里保存引用，也必须表达这个引用所受的有效性约束。

理解生命周期时，先看**输出引用是否与输入借用有关**，再看**结构体是否保存了引用**。没有引用的普通拥有型数据往往不需要显式生命周期参数。Rust 有生命周期省略规则，用于处理简单且无歧义的关系；复杂返回关系必须写清。

## 从 longest 开始，但不要把它当计时器

`fn longest<'a>(x: &'a str, y: &'a str) -> &'a str` 的意思不是“x、y 和结果存活完全相同的秒数”，而是返回引用不能超出由这两个输入共同满足的约束范围。实际数据可能一个在静态区域，另一个是局部 `String`；结果也只能在两者都可证明有效的范围内使用。

```rust
fn longest<'a>(a: &'a str, b: &'a str) -> &'a str {
    if a.len() >= b.len() { a } else { b }
}
fn main() {
    let left = String::from("long");
    let chosen;
    {
        let right = String::from("x");
        chosen = longest(&left, &right);
        println!("{chosen}");
    }
    // println!("{chosen}"); // 预期编译失败：可能借用了 right
}
```

即使人眼看出本次总会返回 `left`，类型签名仍允许另一个输入成为结果来源，因此编译器基于签名进行保守分析。这有利于函数单独验证，也解释了为什么某些安全的特殊情况仍会被拒绝。

## 读懂函数签名中的输入与输出

```rust
fn longer<'a>(left: &'a str, right: &'a str) -> &'a str {
    if left.len() >= right.len() { left } else { right }
}
fn main() {
    let a = String::from("record");
    let b = String::from("id");
    let selected = longer(&a, &b);
    assert_eq!(selected, "record");
}
```

两个输入被标成 `'a` 并不意味着它们必须有完全相同的物理寿命；编译器在调用处寻找满足约束的有效借用范围。由于函数可能返回任一输入，调用者只能在两者都仍然有效的范围内安全使用结果。这里是“保证关系”，不是“控制销毁”。

## 把借用保存在结构体中的影响

```rust
struct CustomerView<'a> { name: &'a str }
impl<'a> CustomerView<'a> {
    fn upper(&self) -> String { self.name.to_uppercase() }
}
fn main() {
    let owned = String::from("alice");
    let view = CustomerView { name: &owned };
    println!("{}", view.upper());
}
```

`CustomerView` 是一个借用视图，不应被长期独立保存于超出 `owned` 生存期的队列中。结构体上添加 `'a` 不是所有权自动变复杂，而是明确让 API 使用者看到资源关系。若将字段改成 `String`，类型无需生命周期参数，却必须拥有或克隆字符串。视图型对象与拥有型对象经常需要同时存在，例如标准库的 `String` 和 `&str`。

## 为什么引用字段会传播类型参数

这个机制使函数 API 可以精确表达借用来源；但当一个结构体长期保存引用时，生命周期参数会成为类型的一部分，可能逐层传播到容器、Trait 对象与模块。这就是为什么一些 Rust 架构选择让长期实体拥有 `String`，而临时解析视图使用 `&str`。

```rust
struct OrderView<'a> {
    customer: &'a str,
    cents: u64,
}
fn main() {
    let name = String::from("Ada");
    let view = OrderView { customer: &name, cents: 1500 };
    assert_eq!(view.customer, "Ada");
    assert_eq!(view.cents, 1500);
}
```

`OrderView<'a>` 不能脱离其 `customer` 所借用的数据而被安全使用。它适合短期解析结果或临时视图；如果需要把订单缓存到长生命周期的队列，改成拥有 `String` 可能比不断传递生命周期参数更合适。

## 为什么有些签名可以省略生命周期

`fn head(s: &str) -> &str` 根据生命周期省略规则通常可以推导输出借用来自唯一输入引用；`fn choose(a: &str, b: &str) -> &str` 则缺少足够的推导信息，必须显式表示关系或调整 API。方法里的 `&self` 还有特定省略规则，可以帮助编译器把输出引用绑定到接收者。省略只是语法便利，不意味着对应约束消失。Rust Reference 的 [lifetime elision](https://doc.rust-lang.org/reference/lifetime-elision.html) 一节给出了正式规则。

此外 `&'static str` 常见于字符串字面量，但绝大多数动态创建的业务字符串并不具有 `'static` 引用。不要为了消除报错把普通借用改为 `'static`；应该找出真正的拥有者以及期望的存活关系。

## 为什么两个输入引用需要一个共享约束

有些函数返回输入的一个切片，例如在两个候选商品名之间选择较长者。函数体可能依赖运行时长度决定返回哪一边，所以调用者不能假设结果永远来自左边。生命周期标注必须覆盖所有可能路径。即使某次调用左边明显更长，编译器通常也不会针对这个调用点重新证明函数体的所有分支。

```rust
fn pick<'a>(a: &'a str, b: &'a str) -> &'a str {
    if a.len() > b.len() { a } else { b }
}
fn main() {
    let stable = String::from("invoice");
    let result;
    {
        let temporary = String::from("id");
        result = pick(&stable, &temporary);
        println!("{result}");
    }
    // println!("{result}"); // 预期编译失败：结果也可能来自 temporary
}
```

这段看似可由常量长度判断的场景，实际上涉及函数对所有合法输入的承诺。若业务明确只允许返回左边，可以改写成 `fn left<'a>(a: &'a str, _b: &str) -> &'a str { a }`，让签名表达更精确的依赖关系。Rust 类型系统能利用这些结构事实，但不会凭空理解业务上的“不可能”。

## 生命周期省略规则带来的人机分工

函数签名只有一个输入引用时，返回引用通常可以省略显式生命周期，例如 `fn title(x: &Order) -> &str`；当两个输入都可能成为结果来源时，要明确关系。这种规则帮助最常见的 API 保持简洁，却不会降低语义严格度。读 Rust 文档时，应当能在脑中补出省略的关联约束，而不只是记住 `'a` 怎么写。

请比较 `&'static str`、`String` 与 `Arc<str>`：字符串字面量拥有静态存储期，`String` 拥有运行期分配的数据，`Arc<str>` 让多个句柄共享其生命周期。生命周期约束既不等同于 GC，也不代替共享所有权管理。

## 生命周期省略规则的适用边界

C++ 的引用和迭代器通常要求程序员额外确保有效期；Java/Go 通过托管对象机制解决大量对象引用存活问题。Rust 则用类型关系与编译期分析让许多无强制 GC 的借用 API 成为可能。生命周期参数不是一般意义上的泛型值类型参数，但都参与静态约束。

不要随意把所有输入写为同一 `'a`：如果输出只依赖其中一个输入，这会人为收紧 API。也不要过度显式标注——`fn first(s: &str) -> &str` 通常可由省略规则推导。能拥有简单数据时，未必需要把引用长期塞进业务对象。

## 实验：从签名判断可否缓存

设计三个函数：`fn label(&Order)->&str`、`fn copy_label(&Order)->String`、`fn literal()->&'static str`。逐一回答返回值是否可以在订单释放后存入缓存，分别是否会分配，以及能否返回动态用户输入。再把 `CustomerView<'a>` 放进长期运行任务的结构体，观察编译器提出的生命周期限制。

这个实验的重点不是消除所有 `'a`，而是学会把生命周期标注读作“哪一个引用依赖哪一个资源”的约束图。后续处理 Trait Object、Future 和线程任务时，你会不断重用这一模型。

## 避免把 'static 当作万能解法

1. 写一个 `fn choose<'a>(x: &'a str, y: &str) -> &'a str`，只返回 `x`，观察为什么不要求 `y` 同寿命。
2. 在 `OrderView` 中尝试借用一个作用域内创建的临时字符串后跨作用域使用，预测错误。
3. 比较 `OrderView<'a>` 与拥有 `String` 的 `Order` 在缓存、线程和返回值方面的差别。

本章把生命周期从一个函数的隐式关系扩展为显式的类型契约。下一章解释编译器为什么偶尔仍拒绝“我们肉眼认为安全”的代码，以及应如何调整设计而不是盲目添加注解。

**相关阅读**：[book](https://doc.rust-lang.org/book/); [ref](https://doc.rust-lang.org/reference/)。

---

[← 上一篇](11-reference-outlives-data.md) · [系列目录](../00-introduction.md) · [下一篇：为什么编译器有时会拒绝看似安全的代码？](13-borrow-checker-limits.md)
