---
title: "06 为什么赋值和函数调用可能转移所有权？"
sidebar_position: 6
description: "为什么赋值和函数调用可能转移所有权？沿问题、代码、运行机制与设计取舍逐步推导。"
slug: /rust-design/06-move-on-assign-and-call
---

# 06 为什么赋值和函数调用可能转移所有权？

> **第 2 篇 · 为什么资源必须有明确的所有者？**  
> 本文聚焦一个因果问题：为什么赋值和函数调用可能转移所有权？；代码示例以稳定 Rust 的通用语法为主（适用于 2021/2024 Edition），除非明确注明为示意或故意无法编译的例子。

同样写 `let b = a`，整数在 Rust 中通常还可以继续使用 `a`，但 `String` 不行。这不是语法不一致，而是不同类型对复制与资源责任的语义不同。理解它可以决定何时按值传参，何时借用，以及何时显式克隆。

## 赋值之后，原变量为什么不一定可用

假设 `String` 赋值只复制指针、长度和容量，同时两个变量都在作用域结束时释放同一缓冲区，就会双重释放。若自动深拷贝，则赋值可能隐含昂贵的内存分配。Rust 选择默认把不能简单 `Copy` 的值视为移动，让调用者在需要额外副本时显式选择克隆。

区分三种操作：**Move** 转移原值的使用权与管理责任；**Copy** 创建一个按位可复制且不会造成资源重复管理的值副本；**Clone** 是显式调用的复制语义，可能很昂贵。类型是否实现 `Copy` 是 trait 约束问题，而非“栈/堆二选一”的简单规则。

## Move 不是每次都复制堆内存

对于非 `Copy` 类型，`let b = a` 表达“新绑定接收值，旧绑定不再使用”；但具体机器级操作可能只移动指针、长度、容量等元数据，甚至被优化掉。初学者容易把 Move 想成昂贵的 `memcpy`，或者想成把数据从栈搬到堆；这都不是 Rust 语义的要求。Rust 检查的是**后续由谁能够继续使用该值**。

`Copy` 则表示按位复制具有独立且合法的值语义，因此两个绑定都可继续使用。整数、布尔等典型实现 `Copy`；拥有堆分配的 `String` 不能直接实现 `Copy`，因为复制元数据会造成共享释放责任的冲突。`Clone` 与 `Copy` 不同：它是一个显式的方法，可以执行深拷贝、浅拷贝或其他由实现定义的复制语义。

## Move、Copy 与 Clone 的语义边界

```rust
fn main() {
    let x = 10_u64;
    let y = x;               // Copy
    println!("{x} {y}");

    let a = String::from("order");
    let b = a;               // Move
    println!("{b}");
    // println!("{a}");      // 编译失败
}
```

`x` 与 `y` 是两个独立的整数值；`a` 移动后不可用，`b` 管理原字符串。这里并不承诺编译器必须做多少字节的实际搬运：编译器可以优化物理数据移动，只要保持语言语义。观察编译器错误可帮助理解“使用已移动的值”是一种静态非法行为。

## 用代码辨别三种行为

```rust
#[derive(Clone)]
struct Label { text: String }
fn take(label: Label) -> usize { label.text.len() }
fn read(label: &Label) -> usize { label.text.len() }
fn main() {
    let a = Label { text: "paid".into() };
    assert_eq!(read(&a), 4);          // 借用，不转移所有权
    let b = a.clone();               // 显式复制数据
    assert_eq!(take(b), 4);          // b 被移动
    assert_eq!(a.text, "paid");      // a 仍然可用
}
```

别只盯着 `a` 是否可用，还要看运行成本：`clone()` 对 `String` 通常需要为字节创建独立缓冲区，而借用没有这种深复制。把所有借用错误都用 `.clone()` 解决虽然常能让代码编译，却可能模糊对象的责任边界。合理的克隆应该对应真实的“需要独立拥有一份”的业务要求。

## 函数参数也是所有权转移的通道

这一规则塑造了很多库 API：`Vec::push(T)` 接收一个被拥有的元素；`Iterator::collect()` 会构造新容器；`into_iter()` 通常消费容器，而 `iter()` 借用元素。带 `self` 的方法也可能表达一种不可逆的状态转换或资源交接。

```rust
fn main() {
    let original = String::from("invoice");
    let independent = original.clone();
    assert_eq!(original, independent);
    let numbers = vec![1_u8, 2, 3];
    let a = &numbers;
    assert_eq!(a.len(), 3);
    assert_eq!(numbers.len(), 3);
}
```

`clone()` 为字符串建立独立拥有的内容，可能发生分配；`&numbers` 只是创建引用，不克隆 `Vec`。如果只是读取数据，通常优先借用。若需要跨任务保留或独立修改，拥有数据、克隆或共享所有权可能更合适。选择不是口号，而是成本与生命周期的权衡。

## 进阶：部分移动与结构体拆解

```rust
struct Invoice { id: u64, customer: String }
fn main() {
    let invoice = Invoice { id: 7, customer: "Ada".into() };
    let customer = invoice.customer;
    println!("{} / {}", invoice.id, customer);
    // println!("{:?}", invoice.customer); // 预期编译失败：字段已移动
}
```

只有 `customer` 字段被移动，`id` 仍可访问。这在数据转换时非常有用：把订单拆分成独立消息，不必先克隆每个字段。不过如果类型自己实现了 `Drop`，部分移动受到进一步限制，因为析构逻辑可能需要完整对象。类型自身的清理策略会反过来约束字段的移动方式，这是第七章的重要伏笔。

## 移动语义如何影响错误 API

订单交给支付网关前，你可能希望网关在拒绝支付时把原订单归还给调用者，以便修改后重试。与简单的 `fn submit(order: Order) -> Result<(), E>` 相比，`fn submit(order: Order) -> Result<Receipt, (E, Order)>` 明确把失败时的原订单返还给调用者。这不是风格问题，而是失败后资源所有权的协议。

```rust
#[derive(Debug)]
struct Order { cents: u64 }
fn send(order: Order) -> Result<(), Order> {
    if order.cents == 0 { Err(order) } else { Ok(()) }
}
fn main() {
    let order = Order { cents: 0 };
    let returned = send(order).unwrap_err();
    assert_eq!(returned.cents, 0);
}
```

与之对照，若错误只有 `String`，而函数消耗了 `Order`，调用方就可能无法继续修改这份原始数据。当然实际支付请求往往要保留记录并生成幂等标识，不是简单返回原值就能保证外部事务安全。这只是展示 Rust 的类型可以描述资源返还路径。

## 从 `Copy` 约束看泛型设计

泛型函数如果写 `fn duplicate<T: Copy>(x: T) -> (T, T) { (x, x) }`，就明确要求所有参数可安全隐式复制。想处理 `String` 时必须改成 `T: Clone` 并显式调用 `clone`，或改变算法让结果通过借用共享。类型约束不只是“告诉编译器可以调用某个方法”，还决定算法是否能无代价地复制值。

练习给订单增加一个 `String` 字段，观察为何整个结构体无法 `derive(Copy)`；再只对独立的 `OrderId(u64)` 派生 `Copy`。解释哪些字段真正管理资源，而哪些只是可复制的值。


## 为何 `Result` 的错误侧可以携带原值

所有权转移不是单向不可恢复的“销毁”。一个函数可以消费输入，并在失败时返回它，调用者在处理 `Err` 后重新取得所有权。这在尝试插入有界队列、发送消息或进行可重试转换时特别重要。如果错误类型没有包含输入值，失败后还能否重试就取决于函数内部是否已保存或复制那份数据。

比较两个接口：`fn enqueue(order: Order) -> Result<(), Error>` 与 `fn enqueue(order: Order) -> Result<(), (Error, Order)>`。前者清楚地表示成功后订单属于队列，但失败时调用者未必拥有原值；后者允许失败时把资源返还。你还应区分“入队失败时返还订单”和“远端处理失败后返还同一个订单”——后者需要分布式消息协议，不能只靠函数返回类型。

进一步练习：定义 `enum QueueError { Full, Closed }`，让有界队列失败返回 `(QueueError, Order)`。然后问成功入队但数据库写入失败时，原请求是否已经得到确认？这一步会把所有权和业务可靠性区分开。

## 容器和结构体中的部分移动

C++ 也有移动语义，但其默认赋值、复制构造以及资源管理约定与 Rust 不同。Java 的对象赋值通常复制引用而不是对象，值对象与原始类型则另当别论。Go 的赋值具有按值语义，但 slice/map 等值内部包含共享底层状态的句柄信息，需要单独分析。

不是所有小类型都实现 `Copy`，例如包含资源所有权或定义了析构行为的类型通常不能 `Copy`。也不能简单断言 `Clone` 永远是“深拷贝”：对于 `Rc` 等类型，克隆是增加共享所有权句柄，而不是复制底层对象。

## 以调用图替代“变量都在同一作用域”的直觉

将 `Order` 从路由函数传到工作队列，再由消费者取出并写入数据库，画三次所有权交接。每条交接边上写出函数签名；每个失败点问“订单现在还由谁拥有”。对比返回 `Result<(), Order>` 和 `Result<(), Error>` 的不同恢复可能性：前者可以在失败时返还原值，后者若消费后丢弃数据，则调用者无法再次使用原变量。错误类型与所有权语义由此相互影响。

不要混淆复制句柄与复制资源：`Arc::clone` 只增加共享所有权计数，而 `String::clone` 通常复制字符串缓冲区。虽然都叫 `clone`，代价与语义必须结合具体类型判断。

## 追踪一次所有权流动

1. 为仅含 `u64` 的小结构体派生 `Copy, Clone`，观察赋值后两边都可用。
2. 把一个 `String` 放入 `Vec<String>` 后尝试使用原变量，解释错误。
3. 对比 `Rc<String>::clone` 与 `String::clone`，说明底层内容是否产生独立副本。

Move、Copy、Clone 共同解决了“传递一个值意味着什么”。下一章研究为什么资源清理与作用域结合，以及 Drop 怎样让错误路径也能保持良好的资源管理。

**相关阅读**：[book](https://doc.rust-lang.org/book/); [ref](https://doc.rust-lang.org/reference/)。

---

[← 上一篇](05-resource-responsibility.md) · [系列目录](../00-introduction.md) · [下一篇：为什么资源释放应当与作用域关联？](07-drop-and-scope.md)
