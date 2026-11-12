---
title: "第 28 章｜为什么并非所有业务状态都适合用类层次表达？"
sidebar_position: 28
description: "从订单系统的一次具体失败出发，理解为什么并非所有业务状态都适合用类层次表达？，以及由此带来的语法、运行行为与设计取舍。"
slug: /programming-languages/28-algebraic-data-types
---

# 第 28 章：为什么并非所有业务状态都适合用类层次表达？

订单有“待付款、已付款、已发货、已取消”四种状态。若用几个布尔字段 `paid/shipped/cancelled`，会出现同时发货又取消的组合。

## 布尔标记直观地表达单一开关

布尔标记直观地表达单一开关，只有一个状态维度时完全合理。但多个互斥状态用布尔组合会制造大量无效组合。

**教学实现：**

```python
order = {"paid": True, "shipped": True, "cancelled": True}
# 三个布尔值能表示 8 种组合，但不全合法
```

## 程序可以输出一个状态

程序可以输出一个状态，却没有修正数据内部的矛盾。我们需要在模型层表达“任何时候恰好只有一个订单状态”。

**用于触发限制的代码／输入（实验场景，不伪称来自框架）：**

```python
def describe(order):
    if order["cancelled"]: return "cancelled"
    if order["shipped"]: return "shipped"
    if order["paid"]: return "paid"
    return "pending"
# 对同时 cancelled/shipped 的矛盾状态，优先级掩盖了错误
```

**因此需要的能力。** 用枚举或代数数据类型把合法状态作为有限选择，减少无法解释的字段组合。之后用模式匹配把每种情况对应到处理逻辑。

## 把新机制落实成代码

**改进后的教学实现：**

```python
from enum import Enum, auto
class Status(Enum):
    PENDING = auto()
    PAID = auto()
    SHIPPED = auto()
    CANCELLED = auto()

def label(s: Status) -> str:
    match s:
        case Status.PENDING: return "待付款"
        case Status.PAID: return "已付款"
        case Status.SHIPPED: return "已发货"
        case Status.CANCELLED: return "已取消"
```

一个 `Status` 值只能取这些枚举成员之一，消除了用三个布尔值表示互斥状态的歧义。若状态还携带不同字段，Rust enum 等 ADT 能将有效数据形状与状态绑定得更紧。

## 初始 PENDING

初始 `PENDING`，付款后变成 `PAID`，发货后变成 `SHIPPED`。某时刻只有一个状态标记；但“是否允许从 PENDING 直接跳到 SHIPPED”仍需要状态转换规则检查。

## Rust 的枚举可以让不同变体携带不同数据

Rust 的枚举可以让不同变体携带不同数据，并用 `match` 做模式匹配。

**Rust 示例（教学代码，非所引用仓库的源码节选）：**

```rust
enum OrderState {
    Pending,
    Paid { transaction: String },
    Shipped { tracking: String },
    Cancelled,
}
fn is_done(s: &OrderState) -> bool {
    matches!(s, OrderState::Shipped {..} | OrderState::Cancelled)
}
```

Rust 的和类型能同时表达“不同状态携带不同字段”；Python Enum 示例只表达有限状态标签。两者不要简单看成完全等价。

## 为什么把订单状态写成任意字符串很危险

订单状态最初是 `"pending"`、`"paid"` 等字符串。字符串方便但没有封闭边界：

```python
status = "paied"  # 拼写错误，语法仍合法
```

如果只有三种合法状态，我们希望模型显式列出允许的选择。Python 的 `Enum` 可以表达受控名字；Rust 的 `enum` 更能把不同状态与不同数据关联起来：

```rust
enum Payment {
    Pending,
    Paid { receipt_id: String },
    Failed { reason: String },
}
```

`Paid` 拥有收据号，而 `Pending` 不需要这个字段。与“一个对象同时有 status、receipt_id、failure_reason 三个可选字段”相比，类型结构更接近业务的不变量：不能出现 `Pending` 却同时带失败原因的组合。

当分支增加，模式匹配还可以检查是否处理了所有定义的状态。Java 的 sealed 类型与 pattern matching、C 的标签联合体、Go 的接口加类型选择提供不同程度的表达与检查。

实验：新增 `Refunded`，尝试找出哪些处理付款状态的分支需要更新；这比单纯比较 `if/else` 和 `match` 的语法有意义。

## 对照规范和真实实现

**源码/规范阅读路线（供读者核对，并非声称已经逐行验证）：** [Rust Reference：Enumerations、Patterns](https://doc.rust-lang.org/reference/)。

查 Rust enum 的变体类型与匹配规则；对照 Python Enum 与 match 的规范。先只研究一次正常状态转移，不急着扩展到事件溯源。

## 实验：写 pay(order)

写 `pay(order)`：仅当状态为 `PENDING` 时改成 `PAID`；输入 `SHIPPED` 必须拒绝。先给出预期，再执行。

**应当观察到（自行运行前属于预期）：** 枚举消除了非法状态标签组合，转换函数则禁止非法状态跳转。它们分别解决“状态表示”和“状态流转约束”。

## 边界：有时简单布尔值就足够

有时简单布尔值就足够，复杂枚举反而增加代码量。选用代数数据类型的关键是业务状态确实存在互斥分支且携带不同规则。

**跨语言迁移时，** Java enum/sealed types、Rust enum、C tagged union、C++ variant、Python Enum/union、Zig union(enum)、Go 接口或标记结构均可表达受限状态，但穷尽检查程度不同。

**下一层问题：** 为什么空值和失败状态容易造成错误？

[上一章：为什么相似的类型不一定能够互相替代？](27-subtyping-and-variance.md) · [下一章：为什么空值和失败状态容易造成错误？](29-null-and-failure-states.md)
