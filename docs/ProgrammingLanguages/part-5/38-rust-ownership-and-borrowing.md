---
title: "第 38 章｜为什么 Rust 需要所有权与借用？"
sidebar_position: 38
description: "从订单系统的一次具体失败出发，理解为什么 Rust 需要所有权与借用？，以及由此带来的语法、运行行为与设计取舍。"
slug: /programming-languages/38-rust-ownership-and-borrowing
---

# 第 38 章：为什么 Rust 需要所有权与借用？

多个模块都能访问订单缓冲区。一个模块释放它，另一个模块继续读，C/C++ 中就可能出现悬垂引用。我们想在执行前排除大量此类路径。

## C 的指针使共享地址容易表达

C 的指针使共享地址容易表达，调用方拥有灵活控制权。但不同别名之间没有内建的所有权关系约束，管理责任要靠程序员维护。

**教学实现：**

```c
char *p = malloc(16);
char *alias = p;
free(p);
// alias 指向的动态存储已被释放，不能继续解引用
```

## 值层面的别名还存在

值层面的别名还存在，但它所指向的对象生命周期已经结束。只有靠运行时检测或人工审查可能错过这个错误。

**用于触发限制的代码／输入（实验场景，不伪称来自框架）：**

```c
char *p = malloc(16);
char *alias = p;
free(p);
// printf("%c", alias[0]); // use-after-free：未定义行为
```

**因此需要的能力。** 在静态阶段跟踪哪段代码拥有资源、什么时候移动所有权、哪些引用只能暂时借用，从而拒绝一批潜在无效访问。

## 把新机制落实成代码

**改进后的教学实现：**

```rust
fn length(xs: &Vec<i64>) -> usize {
    xs.len()
}
let amounts = vec![100, 250];
let n = length(&amounts);  // 临时共享借用
println!("{n}: {:?}", amounts);
```

函数仅借用容器，没有接管它的所有权；调用结束后原变量仍然可用。Rust 同时可以通过移动语义转移资源控制权，帮助确定应由谁最终释放。

## amounts 持有向量资源

`amounts` 持有向量资源；`length(&amounts)` 创建一个受约束的共享借用并读取长度；调用结束后借用不再被使用；`amounts` 继续有效并可打印。

## C++ 也有引用与 RAII

C++ 也有引用与 RAII，但一般不会对同类借用别名做同样的静态限制。

**C++ 示例（教学代码，非所引用仓库的源码节选）：**

```cpp
std::vector<long> amounts{100, 250};
auto length = [](const auto& xs) { return xs.size(); };
std::cout << length(amounts);
```

`const auto&` 能表达不经由该引用修改容器，但不能与 Rust 借用检查的全部别名限制划等号。

## 两个修改者为什么让 Rust 编译器拒绝程序

我们已经知道多个引用可能指向同一数据。如果两个位置同时准备修改，哪个结果应该算最终状态？Rust 在安全代码中对可变借用施加独占约束：

```rust
let mut total = 350_i64;
let a = &mut total;
*a += 100;
// 在 a 的最后一次使用结束后，可以产生新的借用
let b = &mut total;
*b += 50;
assert_eq!(total, 500);
```

这段顺序使用的可变借用可以成立。但若要求 `a` 与 `b` 的有效使用范围重叠，编译器会拒绝两个指向同一值的活动可变借用。重点是**使用范围与访问冲突**，不是“出现两次 `&mut` 一定违法”。

所有权还决定资源由谁在离开作用域时执行析构；借用让函数临时访问数据而不夺走所有权。`move`、`Copy`、共享借用和可变借用要结合具体类型讨论。Rust 不能防止逻辑上的重复扣款，它主要控制一类内存与并发访问问题。

## 对照规范和真实实现

**源码/规范阅读路线（供读者核对，并非声称已经逐行验证）：** [Rust Reference：Ownership 相关语义 / Borrowing](https://doc.rust-lang.org/reference/)。

核对 move 与 borrow 的语言约束，并与 Rust Book 的所有权章节对照。不要把“所有权”神化成可以防止一切逻辑错误；`unsafe`、死锁和业务不变量仍有各自风险。

## 实验：尝试 let moved = amounts; p

尝试 `let moved = amounts; println!("{:?}", amounts);` 并预测编译错误，再改为 `let borrowed = &amounts`。

**应当观察到（自行运行前属于预期）：** 对 `Vec` 移动所有权后原绑定不能再被当作拥有该值来使用；共享借用后仍可在合法借用范围内使用原变量。

## 边界：所有权检查给出强内存安全保障

所有权检查给出强内存安全保障，也引入对借用关系的学习成本；有时需要重新设计数据结构而不是绕开检查器。

**跨语言迁移时，** Rust 在编译阶段拒绝不少内存错误，C/C++ 依靠规则和工具，Java/Go/Python 大多使用托管内存以避免悬垂对象引用，Zig 则更多显式控制。

**下一层问题：** 为什么 Java、Go、Python 依赖自动内存管理？

[上一章：为什么 C++ 使用 RAII？](37-raii-in-cpp.md) · [下一章：为什么 Java、Go、Python 依赖自动内存管理？](39-garbage-collection.md)
