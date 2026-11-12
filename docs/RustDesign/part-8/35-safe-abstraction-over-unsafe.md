---
title: "35 为什么安全抽象可以包含 unsafe 实现？"
sidebar_position: 35
description: "为什么安全抽象可以包含 unsafe 实现？沿问题、代码、运行机制与设计取舍逐步推导。"
slug: /rust-design/35-safe-abstraction-over-unsafe
---

# 35 为什么安全抽象可以包含 unsafe 实现？

> **第 8 篇 · 为什么 Rust 仍然需要 unsafe？**  
> 本文聚焦一个因果问题：为什么安全抽象可以包含 unsafe 实现？；代码示例以稳定 Rust 的通用语法为主（适用于 2021/2024 Edition），除非明确注明为示意或故意无法编译的例子。

标准库中的动态数组、锁和许多高性能容器，都可能需要对原始内存作精细控制。若安全 API 本身不允许任何内部 unsafe，实现空间会大幅受限。Rust 的策略是将底层不变量集中在库内部，外部用户只需满足公开的安全接口契约。

## 安全 API 可能建立在 unsafe 内核之上

一个安全抽象的关键不是实现文件里有没有 `unsafe`，而是**所有安全调用者是否都无法通过它造成未定义行为**。例如切片的 `get` 在越界时返回 `None`，容器内部可能做过指针运算，但调用者不会因此得到指向越界元素的安全引用。

审计一个安全封装时要核查：构造阶段建立了什么不变量；每个公开方法是否保持它；错误和 Drop 路径是否清理资源；并发调用是否受到正确限制；能否通过任意安全输入违反别名或初始化条件。这类推理比简单搜索 unsafe 关键字重要得多。

## 安全函数内部使用 unsafe 并不矛盾

Rust 标准库和许多高性能容器都需要处理原始指针、分配器与未初始化内存。如果它们只暴露 `unsafe` 接口，普通应用将承担巨大的安全责任；如果实现者能够证明公开的安全方法始终维持不变量，就可以把危险集中在少数审查过的实现里。这里的关键不是有没有 `unsafe` 关键字，而是**任何通过安全接口允许的调用序列是否都保持安全**。

例如，将一个切片拆成两个互不重叠的可变切片，需要证明两者没有别名。安全接口 `split_at_mut` 已为调用者处理这种证明；自己直接从同一个裸指针制造两个重叠的 `&mut` 则可能违反引用规则。

## 封装的真正单位是不可被破坏的不变量

```rust
struct NonEmptyVec<T> { values: Vec<T> }
impl<T> NonEmptyVec<T> {
    fn new(first: T) -> Self { Self { values: vec![first] } }
    fn first(&self) -> &T { &self.values[0] }
    fn push(&mut self, value: T) { self.values.push(value); }
}
fn main() {
    let mut v = NonEmptyVec::new(7);
    v.push(9);
    assert_eq!(*v.first(), 7);
}
```

这是一个完全用安全 Rust 写的封装，帮助先理解“不变量如何转化为 API”：构造时至少一个元素，公开方法不提供删除最后一个元素的操作，因此 `first()` 的索引合法。真正的底层容器还需面对未初始化内存、分配器与异常路径，复杂程度远高于这个例子。

## 一个无须自己写 unsafe 的正确用法

```rust
fn discount_first_half(values: &mut [u64]) {
    let mid = values.len() / 2;
    let (left, right) = values.split_at_mut(mid);
    for x in left { *x /= 2; }
    for x in right { *x += 10; }
}
fn main() {
    let mut totals = [100, 200, 300, 400];
    discount_first_half(&mut totals);
    assert_eq!(totals, [50, 100, 310, 410]);
}
```

调用者同时得到两个可变切片，仍然是安全的，因为它们针对互不重叠的内存区域。这正是“更强能力来自更精确的安全证明”的案例。容器库可以用 unsafe 处理底层地址，但安全 API 将可被证明的前提转换为稳定、易用的接口。

## 从切片拆分理解不重叠借用

这种设计使系统由少量可信底层组件与大量安全业务代码组成。`Vec` 使用者不需要每次重新证明缓冲区布局；`MutexGuard` 使用者不需要手工证明何时解锁。良好的安全抽象能够把复杂证明局部化，让高层架构在约束内组合这些能力。

```rust
use std::num::NonZeroU64;
struct OrderId(NonZeroU64);
impl OrderId {
    fn new(raw: u64) -> Option<Self> {
        NonZeroU64::new(raw).map(Self)
    }
    fn get(&self) -> u64 { self.0.get() }
}
fn main() {
    assert!(OrderId::new(0).is_none());
    assert_eq!(OrderId::new(42).unwrap().get(), 42);
}
```

这里再展示业务层的安全抽象：`OrderId` 不允许零值，通过构造函数保持不变量。它与 unsafe 封装的思路有相似性——把验证集中在边界，外部调用者操作已经合法的值。但业务不变量与内存安全不变量不能混为一谈；这个例子无需 unsafe。

## FFI 的所有权契约比函数名重要

设想 C 库返回 `char*`。这段指针究竟应通过哪个函数释放？它是否保证零终止？可能为 null 吗？内存会在下一次调用时被复用吗？能否从其他线程访问？这些问题都不能通过 Rust 的类型 `*mut c_char` 自动回答。FFI 包装层必须阅读外部 API 契约，把这些事实转换成明确的 Rust 类型、长度和 Drop 规则。

过早把不可信的外部指针转换成 `&str` 或 `&[u8]` 可能直接违反 Rust 引用的有效性要求。安全封装应先验证长度、编码和生存期；如果外部资源有专门的释放函数，则用拥有型包装在 Drop 中调用该函数，而不是误用 Rust 分配器释放外部内存。

## 从两段可变切片理解可证明的不重叠

如果你希望并行修改一组订单明细的前半部分与后半部分，需要两个 `&mut [Item]`。Rust 禁止通过同一可变借用直接制造两个可能重叠的可变引用，但标准库的 `split_at_mut` 可以安全返回两段，因为它确认边界后能够保证左右区间互不重叠。调用者因此获得能力更强、同时也安全的接口。

```rust
fn scale(values: &mut [u64]) {
    let middle = values.len() / 2;
    let (left, right) = values.split_at_mut(middle);
    for v in left { *v *= 2; }
    for v in right { *v += 1; }
}
fn main() {
    let mut values = [1, 2, 3, 4];
    scale(&mut values);
    assert_eq!(values, [2, 4, 4, 5]);
}
```

安全的本质是两个区间没有交集，而不是某个关键字让它们神奇地不冲突。在更复杂的树、链表或分配器中，类似的证明可能需要 unsafe 访问原始指针，再封装成安全的视图 API。

## 为什么 panic 安全也是资源封装的一部分

如果容器在修改长度后、初始化元素之前发生 panic，Drop 逻辑可能会尝试销毁尚未初始化的内存。设计底层容器时，必须保证每个可能提前退出的控制路径仍维持内部不变量：哪些元素已初始化、长度字段可信、分配指针是否仍归当前容器所有。良好的底层 Rust 抽象不仅要证明正常路径，也要证明失败路径仍然安全。

练习为一个自制动态数组写一份“不变量清单”，包括指针、容量、长度和已初始化元素数；然后设计 `push` 如果分配失败应返回什么、原数组是否仍可用。即使不写 unsafe 代码，也能建立审查标准。

## FFI 为什么需要更严格的资源协议

C++ 也有依靠封装维护不变量的容器，Java/Go 的标准库实现同样可能包含运行时级别的底层操作。Rust 独特之处在于安全/unsafe 区分与借用、类型系统的协同，使公共安全 API 获得更明确的语言保证。

安全封装必须对所有安全输入维持不变量，不能把“用户应该不会这样调用”当安全证明。测试、模糊测试和 Miri 等工具有助于发现问题，但不能替代正确的不变量论证。标准库或第三方库如果存在 unsafe 实现错误，也可能伤害使用安全 API 的程序。

## 安全封装的审查清单

列出公开操作后逐个检查：构造是否允许无效状态？两个安全方法连续调用是否可能形成别名？panic 发生在中途时资源是否仍处于可清理状态？drop 顺序是否会导致悬垂？并发共享需要哪些 `Send/Sync` 条件？外部代码能否通过回调重入？

若你无法回答这些问题，就不应该仅因某段代码跑通了测试而对外暴露安全接口。练习查阅标准库 `slice::split_at_mut` 的行为文档，写一份不依赖具体实现的证明：为什么 `mid <= len` 时左右切片互不重叠，以及 `mid > len` 应如何处理。

## 如何审查和测试安全封装

1. 给 `NonEmptyVec` 添加 `pop`，要求不能返回最后一个元素；设计返回类型。
2. 将 `OrderId` 字段设为私有，尝试在模块外绕过构造函数直接创建零值。
3. 为一个假想的底层集合编写安全性审计清单：构造、修改、返回引用、释放和并发。

第八篇说明了 Rust 的安全边界如何被组织。下一篇研究编译器究竟在何时、通过什么中间表示与分析来执行这些约束。

**相关阅读**：[nomicon](https://doc.rust-lang.org/nomicon/); [ref](https://doc.rust-lang.org/reference/)。

---

[← 上一篇](34-unsafe-keyword.md) · [系列目录](../00-introduction.md) · [下一篇：为什么编译器必须理解类型、所有权和借用关系？](../part-9/36-compiler-and-borrow-tracking.md)
