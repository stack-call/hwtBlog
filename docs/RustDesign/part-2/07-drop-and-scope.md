---
title: "07 为什么资源释放应当与作用域关联？"
sidebar_position: 7
description: "为什么资源释放应当与作用域关联？沿问题、代码、运行机制与设计取舍逐步推导。"
slug: /rust-design/07-drop-and-scope
---

# 07 为什么资源释放应当与作用域关联？

> **第 2 篇 · 为什么资源必须有明确的所有者？**  
> 本文聚焦一个因果问题：为什么资源释放应当与作用域关联？；代码示例以稳定 Rust 的通用语法为主（适用于 2021/2024 Edition），除非明确注明为示意或故意无法编译的例子。

一个订单处理函数可能在检查权限、打开文件、写数据库等十几个位置提前返回。如果每条路径都必须显式清理已经取得的资源，遗漏风险会随分支数量增长。Rust 的 RAII/Drop 设计让清理责任跟随对象的拥有者。

## 资源如何与控制流相遇

对函数而言，返回成功、返回错误和发生 panic 可能经过不同控制路径。资源清理不该由每个分支重复拼装。RAII 的基本想法是：资源作为某个值的责任，值结束其存活期时执行清理逻辑。Rust 的 `Drop` 让类型能够定义退出作用域或被显式销毁时的清理行为。

`Drop` 不是垃圾回收，也不是“保证任何情况下都会立即调用”。在普通作用域退出时，局部值会被 drop；`std::mem::forget`、进程中止、引用环泄漏等情形可能不执行某些析构。系统设计不能把 Drop 当作数据库事务提交或持久化成功的证明。

## 把销毁视为控制流的一部分

不仅正常函数返回会离开作用域，`?`、早期 `return`、循环中的 `break` 等也会改变控制流。若资源清理必须写在函数最后一行，每一个早退分支都可能漏掉。RAII 的重要性在于：把资源释放责任绑定到拥有它的对象，而不是依赖每条代码路径都记得手工调用释放函数。Rust 的 `Drop` 是这种思想在类型系统中的体现。

然而“离开作用域就必定执行 Drop”也需要限定：正常退出会清理，panic 展开模式下通常会清理已经初始化的局部值；进程被强制终止、使用 abort 策略或显式遗忘资源时可能不会运行析构。因而 `Drop` 适合释放进程内资源，不应独自承担重要数据持久化承诺。

## Drop 究竟何时执行、负责什么

```rust
struct Session { id: u64 }
impl Drop for Session {
    fn drop(&mut self) {
        println!("release session {}", self.id);
    }
}
fn handle(fail: bool) -> Result<(), &'static str> {
    let _session = Session { id: 5 };
    if fail { return Err("rejected"); }
    Ok(())
}
fn main() {
    let _ = handle(true);
}
```

`_session` 被局部作用域拥有，发生提前 `return` 时也会触发常规清理。`Drop::drop(&mut self)` 接收可变借用，是由语言在销毁时调用的 hook；正常情况下用户不直接调用它来“提前析构”，需要提前释放时可使用 `std::mem::drop(value)`。

## 看一次确定性的 Drop 顺序

```rust
struct Trace(&'static str);
impl Drop for Trace {
    fn drop(&mut self) { println!("drop {}", self.0); }
}
fn demo(fail: bool) -> Result<(), &'static str> {
    let _first = Trace("first");
    let _second = Trace("second");
    if fail { return Err("failed"); }
    Ok(())
}
fn main() { let _ = demo(true); }
```

通常在退出作用域时，较后声明的局部变量先被清理，因此输出顺序为 `second`、`first`。这是方便理解的简单情况；字段销毁和临时值的具体顺序需参考语言规则，不能只凭“先进后出”推断所有表达式。真正要关注的是：即使提前返回，拥有的资源仍有明确的释放路径。

## 错误分支也需要清理保证

RAII 深刻影响 Rust 标准库：`MutexGuard` 离开作用域时释放锁；文件对象销毁时关闭文件句柄；拥有的 `Vec` 清理缓冲区。因为清理被组织进值的生命周期，错误传播 `?` 可以保持代码简洁，而不必在每次早退前写重复清理逻辑。

```rust
use std::sync::Mutex;
fn main() {
    let count = Mutex::new(0);
    {
        let mut guard = count.lock().unwrap();
        *guard += 1;
    } // guard 在这里销毁，锁被释放
    assert_eq!(*count.lock().unwrap(), 1);
}
```

这里展示了“用类型代表一次资源占用”：`MutexGuard` 的存活区间就是锁被持有的区间。显式缩小大括号可以缩短持锁时间。这种模式比“先 lock 再在每条路径 unlock”更容易局部推理，尤其适用于有错误传播的函数。

## 为什么析构方法不能随意消费自己

`Drop::drop(&mut self)` 只得到一个可变借用，不能直接把整个 `self` 再移动走；这是因为析构完成后系统还需要按规则清理字段。若你设计一个保存句柄、缓存和日志状态的复杂类型，析构动作应尽量可靠且不依赖可能失败的外部操作。对需要向调用者报告失败的关闭或提交，通常应提供显式 `finish(self) -> Result<...>` 一类方法，再把 `Drop` 作为兜底清理。

Rust 不允许同时为类型实现 `Copy` 和 `Drop`：允许隐式复制的值若还拥有非平凡析构责任，会让“谁应该清理”变得含糊。由此可见，Trait 并不是孤立的抽象标签，它能够参与资源语义的约束。

## 看清临时对象与字段销毁

普通对象离开作用域时，Rust 按其 Drop 语义和字段规则清理资源。对于包含文件句柄和内存缓冲区的结构，错误路径不会让已初始化的字段凭空消失；即使业务逻辑提前返回，资源对象仍有管理责任。但对象的字段销毁顺序与局部变量的销毁顺序是两个层面，精确写底层库时应查阅相关语言规则而不是只靠“先进后出”的口诀。

```rust
struct Note(&'static str);
impl Drop for Note { fn drop(&mut self) { println!("{}", self.0); } }
struct Bundle { first: Note, second: Note }
fn main() {
    let _bundle = Bundle { first: Note("A"), second: Note("B") };
}
```

在这个简单结构中，字段按声明顺序清理，输出 `A` 和 `B`。与前文“后声明的局部变量先清理”不同，字段规则不应被混为一谈。理解这种差异对手写资源拥有型结构尤其重要：如果某个字段的 Drop 依赖另一个字段仍有效，应当仔细设计字段布局和析构逻辑。

## 失败清理不应隐瞒错误

文件 `Drop` 不能方便地向业务调用方返回“磁盘同步失败”这样的错误，因此重要的持久化动作应提供显式返回 `Result` 的方法。类似地，`MutexGuard` 离开作用域会解锁，但它不负责修复已经被破坏的业务不变量。RAII 解决“别忘记释放资源”，不负责“外部操作绝对成功”。

作业写一个 `finalize(self) -> io::Result<()>` 的草图，让它先提交工作，再释放资源；模拟提交失败和成功，标注不同路径哪些对象会 Drop，以及调用者能拿到什么错误。这能帮助你在设计 `Drop` 与显式 close/commit API 时做出合理取舍。

## 自定义析构如何影响类型设计

C++ 的析构与 RAII 思想非常相近；Java 通过 `try-with-resources` 等手段显式关闭外部资源；Go 使用 `defer` 安排作用域结束时的调用；Rust 把 `Drop` 与所有权转移协调起来。它不是唯一能做确定性清理的语言，特别之处在于与借用规则的组合。

`Drop` 不应执行必须报告成功与否的重要业务动作，因为析构调用本身没有 `Result` 返回给调用者。数据库提交、网络确认或 `sync_all` 等应在普通显式方法里处理错误。还有一个约束：实现 `Drop` 的类型不能同时实现 `Copy`。

## 文件与事务是不同层次的资源

读文件句柄离开作用域后会被清理，并不保证写入字节已同步到持久介质；数据库连接被释放，也不等于事务已经成功提交。对于金融订单，正确流程通常是显式检查写入、提交结果与幂等请求标识，而不是在 `Drop` 内默默尝试提交。把“必须报告错误的协议完成”与“可以自动做的资源回收”拆开，能得到更清晰的 API。

练习：写一个 `Transaction` 类型草图，它拥有连接和事务状态，提供 `commit(self) -> Result<...>`。解释为什么 `Drop` 最多适合尽力回滚或清理，而不适合隐藏会失败的正式提交步骤。

## 从文件句柄到数据库事务的边界

1. 给 `Session` 增加日志，分别尝试正常返回和 `Err` 提前返回。
2. 用不同作用域持有 `MutexGuard`，观察第二次加锁的时机。
3. 设计 `flush(self) -> io::Result<()>` 与 `Drop` 的职责区分：哪些错误必须主动处理？

本章解释了资源何时清理。下一章把所有权、借用和清理责任放到函数签名里，研究怎样设计可读且不制造不必要克隆的 API。

**相关阅读**：[book](https://doc.rust-lang.org/book/); [ref](https://doc.rust-lang.org/reference/)。

---

[← 上一篇](06-move-on-assign-and-call.md) · [系列目录](../00-introduction.md) · [下一篇：为什么所有权会改变函数和 API 的设计？](08-ownership-shapes-api.md)
