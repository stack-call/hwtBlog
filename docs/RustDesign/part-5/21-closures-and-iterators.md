---
title: "21 为什么所有权会影响闭包和迭代器？"
sidebar_position: 21
description: "为什么所有权会影响闭包和迭代器？沿问题、代码、运行机制与设计取舍逐步推导。"
slug: /rust-design/21-closures-and-iterators
---

# 21 为什么所有权会影响闭包和迭代器？

> **第 5 篇 · 为什么 Rust 的类型系统不只描述数据类型？**  
> 本文聚焦一个因果问题：为什么所有权会影响闭包和迭代器？；代码示例以稳定 Rust 的通用语法为主（适用于 2021/2024 Edition），除非明确注明为示意或故意无法编译的例子。

闭包能够捕获外部变量，但 Rust 还必须知道捕获的是借用、可变借用还是拥有的值。如果闭包执行时会把捕获的数据消费掉，它就不能再以完全相同的状态被反复调用。于是闭包的调用能力与所有权模型直接相连。

## 闭包除了捕获值，还要说明怎么捕获

迭代器也有相同问题：读取一个集合的元素、修改元素、逐个拿走元素，是三种不同的资源关系。Rust 不把它们伪装成单一的“遍历”，而用 `iter`、`iter_mut`、`into_iter` 等接口表达借用与消费。函数式语法因此不仅是高阶函数，也是一套资源流动协议。

`Fn` 可以通过共享借用调用，`FnMut` 可以通过可变借用调用，`FnOnce` 可以消费自身调用。能力关系并非三个互斥标签：实现 `Fn` 的闭包也可满足 `FnMut` 和 `FnOnce` 的调用场景。闭包实现哪些 Trait 取决于捕获值在闭包体里怎样被使用。

## 闭包是一种带环境的值

普通函数通常只从参数得到所需数据；闭包却可以捕获创建位置的局部变量。捕获的方式可能是借用、可变借用或取得所有权；即便使用 `move` 捕获，闭包是否会在调用时消费内部值，仍取决于函数体的操作。`FnOnce` 接收闭包自身的所有权，`FnMut` 需要可变借用，`Fn` 只需要共享借用。能实现 `Fn` 的闭包也能满足 `FnMut` 和 `FnOnce` 约束，三者是能力层级而不是三种互斥标签。

## FnOnce、FnMut、Fn 的能力层级

```rust
fn apply_twice<F: FnMut()>(mut f: F) { f(); f(); }
fn main() {
    let mut count = 0;
    apply_twice(|| count += 1);
    assert_eq!(count, 2);

    let lines = vec![10_u64, 20];
    let total: u64 = lines.iter().copied().sum();
    assert_eq!(total, 30);
    assert_eq!(lines.len(), 2);
}
```

闭包通过可变借用更新 `count`，因此需要 `FnMut` 调用能力；调用完成后，借用结束，`count` 可以继续使用。`lines.iter()` 不消费集合，迭代得到元素引用；`copied()` 将 `u64` 复制成值，以便求和。这里闭包与迭代器都把访问权限编码在 API 中。

## 三个闭包分别表达不同的状态关系

```rust
fn main() {
    let name = String::from("Alice");
    let read = || name.len();              // 可共享调用
    assert_eq!(read(), 5);
    assert_eq!(read(), 5);

    let mut count = 0;
    let mut increment = || { count += 1; count };
    assert_eq!(increment(), 1);
    assert_eq!(increment(), 2);

    let payload = String::from("sent");
    let consume = move || drop(payload);  // 调用时消费捕获的资源
    consume();
    // consume(); // 预期编译失败：FnOnce 已被消费
}
```

为什么 `increment` 要 `mut`？因为它修改捕获环境，需要以可变方式调用。为什么 `consume` 不能再调用？它把拥有的字符串交给 `drop`，第一次调用便消费了闭包持有的资源。三种行为来自所有权和借用语义，而不是出于分类整齐的语言设计。

## 所有权怎样影响迭代器的消费方式

闭包捕获机制影响线程任务和异步回调：线程闭包常需要 `move` 转移所有权并满足 `Send + 'static`；一次性消费型回调通常要求 `FnOnce`；允许内部状态变化的处理器会要求 `FnMut`。与普通函数相比，闭包类型还携带捕获环境的信息。

```rust
fn main() {
    let lines = vec![String::from("A"), String::from("B")];
    let owned: Vec<String> = lines.into_iter()
        .map(|s| s.to_lowercase()).collect();
    assert_eq!(owned, vec!["a", "b"]);
    // lines 在 into_iter 之后已经被消费
}
```

`.into_iter()` 接管集合元素所有权，`map` 每次拿到拥有的 `String`，`collect` 创建新的集合。与 `.iter()` 相比，它不需要复制每个元素来获得所有权，但调用者将不能再使用原集合。这是语言级 Move 语义渗透进抽象库的直接结果。

## Iterator 的三个入口同样是所有权协议

```rust
fn main() {
    let words = vec![String::from("A"), String::from("B")];
    let count = words.iter().filter(|s| !s.is_empty()).count();
    assert_eq!(count, 2);
    let owned: Vec<String> = words.into_iter().map(|s| s + "!").collect();
    // println!("{:?}", words); // 预期失败：words 已消费
    assert_eq!(owned[0], "A!");
}
```

`.iter()` 遍历借用元素；`.iter_mut()` 需要可变访问；`.into_iter()` 在拥有型容器上常意味着消费容器，产出拥有的元素。`Iterator::next(&mut self)` 又要求迭代器自身可以推进状态。这个抽象把函数式链式写法与明确的资源访问约束结合起来。

## 从回调的签名反推闭包的捕获行为

假设 `retry` 接收一个闭包并可能多次执行它。如果闭包每次调用都把捕获的请求体消费掉，就无法重试；如果闭包只是读取借用的数据，可以多次执行；如果闭包每次要递增重试计数，它就需要可变访问捕获状态。闭包是值，值的调用方式也需要与资源管理规则一致。

```rust
fn retry<F: FnMut() -> bool>(mut task: F, times: usize) -> bool {
    for _ in 0..times {
        if task() { return true; }
    }
    false
}
fn main() {
    let mut attempts = 0;
    let ok = retry(|| { attempts += 1; attempts >= 3 }, 5);
    assert!(ok);
    assert_eq!(attempts, 3);
}
```

`FnMut` 恰好允许重试闭包修改捕获的计数器；如果采用 `FnOnce`，调用者不能假设可以重复执行。反过来，一个专门消费请求体并发送出去的闭包适合 `FnOnce`，但失败重试需要重新创建可重用的数据或独立拥有的缓冲区。

## 为什么迭代器也是一种状态机

`Iterator::next(&mut self)` 每次调用都会推进游标状态；`.map()` 和 `.filter()` 创建适配器后通常是惰性的，只有消费迭代器时才会执行。它们不是自动构造一个新数组，而是通过迭代器状态组合算法。`.into_iter()` 将拥有型容器消费掉，使元素能够被移动出来，避免不必要的克隆。

练习构建 `prices.iter().copied().filter(|x| *x > 0).sum::<u64>()`，然后用 `.into_iter()` 移动 `Vec<String>`，记录哪些操作使容器还能被访问。最后解释为什么 `Iterator` 的调用接收者是 `&mut self`，而不是每次都移动整个迭代器。


## `move` 关键字与 `FnOnce` 并非同义词

```rust
fn main() {
    let prefix = String::from("order-");
    let label = move |id: u64| format!("{prefix}{id}");
    assert_eq!(label(1), "order-1");
    assert_eq!(label(2), "order-2");
}
```

这个闭包使用 `move` 把 `prefix` 移入自身，但每次调用只借用捕获的字符串完成格式化，并不会消耗它，所以闭包仍可以多次调用。只有当闭包体把捕获的非 `Copy` 值移动出去或以其他方式消费它时，才会限制为仅实现 `FnOnce` 的调用能力。理解这个区别非常重要：捕获方式与调用时的消费方式是两个步骤。

在实际异步任务中，`async move` 也不等于“总是把所有数据彻底复制”。它把捕获的值移入 Future，而对一个引用值的移动只是移动引用本身，引用所指资源仍然必须有效。把这条规则与 `thread::spawn` 的 `Send + 'static` 约束一起理解，才能正确判断一个闭包能否安全脱离当前作用域运行。

## 跨线程闭包为何需要 move 与 Send

Python 的生成器与闭包、JavaScript 的闭包、Java 的函数式接口都支持高阶抽象；Rust 的 Fn 系列额外体现捕获资源的访问与消费能力。C++ lambda 可以通过值或引用捕获，但没有普通 Rust 借用检查那样的统一静态验证。

`move` 闭包表示按值捕获相关环境，不保证捕获的值都进行了堆内存深拷贝，也不自动让闭包满足 `'static`：如果移动进来的是引用，其有效性仍受约束。迭代器惰性执行也意味着很多适配器只有在被消费时才真正执行。

## 为什么线程闭包常出现 move

`std::thread::spawn` 通常要求闭包能够在线程中独立持有它所需要的数据，因此示例常写 `move || ...`。但 `move` 本身并不保证捕获的数据满足 `Send`，也不自动把短寿命引用变成 `'static`。如果移动的是一个持有短暂引用的结构体，线程边界仍可能拒绝它。

练习为 `sort_by_key`、`filter` 和线程任务分别设计闭包，观察 API 要求哪一种 `Fn` 能力。用“是否修改环境、是否消费捕获值、是否跨线程”解释差异，比记忆三个 Trait 的名称更持久。

## 函数式链条仍然必须遵守资源规则

1. 把 `FnMut` 改成 `Fn`，观察修改捕获计数器的闭包为何不满足。
2. 写一个 `FnOnce` 闭包，在调用时 `drop` 捕获的 `String`。
3. 比较 `iter`、`iter_mut`、`into_iter` 对元素类型和原集合可用性的影响。

这里最值得保留的结论是：函数式抽象也必须遵守资源规则。下一章讨论 Rust 如何把 `Copy`、`Drop`、`Deref` 等资源相关能力统一纳入 Trait 体系。

**相关阅读**：[book](https://doc.rust-lang.org/book/); [ref](https://doc.rust-lang.org/reference/)。

---

[← 上一篇](20-static-vs-dynamic-dispatch.md) · [系列目录](../00-introduction.md) · [下一篇：为什么 Copy、Drop、Deref 需要融入 Trait 体系？](22-copy-drop-deref.md)
