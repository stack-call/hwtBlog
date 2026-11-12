---
title: "第 29 章｜为什么空值和失败状态容易造成错误？"
sidebar_position: 29
description: "从订单系统的一次具体失败出发，理解为什么空值和失败状态容易造成错误？，以及由此带来的语法、运行行为与设计取舍。"
slug: /programming-languages/29-null-and-failure-states
---

# 第 29 章：为什么空值和失败状态容易造成错误？

查询订单号时可能找不到记录。旧函数直接返回 `None`，调用者却马上取 `.amount`，引发运行时异常。

## 返回 None 是非常轻量的缺失表示

返回 `None` 是非常轻量的缺失表示，小脚本没有问题。它比抛异常更适合“找不到是正常情况”的查询。

**教学实现：**

```python
def find_order(order_id):
    if order_id == "A": return {"amount": 100}
    return None
```

## 调用方假设结果必有订单

调用方假设结果必有订单，但函数的实际返回可能是缺失。当前 API 没有迫使调用者先处理另一种结果情况。

**用于触发限制的代码／输入（实验场景，不伪称来自框架）：**

```python
order = find_order("missing")
# print(order["amount"])  # TypeError: None 不可下标访问
```

**因此需要的能力。** 把“有值或无值”“成功或失败”变成 API 的明确结果形状，让调用方更容易发现必须处理的分支。

## 把新机制落实成代码

**改进后的教学实现：**

```python
from typing import TypedDict
class Order(TypedDict):
    amount: int

def find_order(order_id: str) -> Order | None:
    return {"amount": 100} if order_id == "A" else None

order = find_order("missing")
if order is None:
    print("not found")
else:
    print(order["amount"])
```

返回类型明确包括 `None`，静态类型检查器可以提示未经收窄就直接访问结果的问题。若有详细失败原因，可以使用异常或显式结果类型，而不是把所有失败都归成 None。

## find_order("missing") 返回 None

`find_order("missing")` 返回 None；分支命中缺失路径并打印 `not found`；不会再尝试读取订单字段。成功路径则读取 amount。程序的控制流现在显式对应结果的两种可能。

## Rust 将“可能缺失”和“可能失败”分别建模为 Option 与

Rust 将“可能缺失”和“可能失败”分别建模为 Option 与 Result。

**Rust 示例（教学代码，非所引用仓库的源码节选）：**

```rust
fn find(id: &str) -> Option<i64> {
    if id == "A" { Some(100) } else { None }
}
match find("missing") {
    Some(amount) => println!("{amount}"),
    None => println!("not found"),
}
```

Rust 的 `Option<T>` 值在类型层编码两种分支，Java Optional 和 Python `T|None` 也能表达缺失，但异常处理与静态穷尽检查能力不同。

## 错误是怎样悄悄变成正常值的

查找订单时，最小实现可能返回 `None` 表示没找到：

```python
def find(orders, oid):
    return orders.get(oid)

orders = {101: 350}
assert find(orders, 102) is None
```

但如果业务允许订单值本身为 `None`，返回 `None` 就不能区分“没找到”和“找到了一个空值”。于是我们必须让不同结果有可区分的表示。在 Rust 中可以用 `Option<T>` 表达有无；操作可能失败时用 `Result<T,E>` 区分成功与失败原因。

```rust
fn find(orders: &std::collections::HashMap<u64, i64>, id: u64) -> Option<i64> {
    orders.get(&id).copied()
}
```

`None` 是一种预期业务分支还是一种系统错误？二者应分开思考。Java 的 `Optional` 并不代替异常机制；Go 常返回 `(value, error)`；Python 可以返回可辨识的结果类型或抛异常。

当调用者必须区分“未找到”“权限不足”“数据库断线”时，把三者统统吞成 `None` 会丢失恢复决策所需的信息。

## 对照规范和真实实现

**源码/规范阅读路线（供读者核对，并非声称已经逐行验证）：** [Rust Reference：Enums、Pattern matching](https://doc.rust-lang.org/reference/)。

对照 Rust `Option`/`Result` 标准库文档与 match 语义；观察类型约束能提示什么、仍有哪些业务错误不能由类型保证。

## 实验：让 find_order 的缺失情况返回 None

让 `find_order` 的缺失情况返回 `None`，数据存储不可用时抛异常；解释“记录不存在”和“数据库连接失败”为何不能混为一谈。

**应当观察到（自行运行前属于预期）：** 前者是查询的合法结果分支，后者是操作失败。把二者全部映射为 `None` 会让调用者无法决定是否重试。

## 边界：显式结果类型增加处理分支

显式结果类型增加处理分支，也使调用方更准确；异常简化正常路径，但需要清晰的传播与清理策略。哪种更适合取决于失败是否是预期业务情况。

**跨语言迁移时，** Go 多返回值 `(value,error)`、Rust `Result`、Java 异常/Optional、Python 异常/None、C 错误码、Zig 错误联合类型、C++ 异常/expected 都是不同选择。

**下一层问题：** 为什么有些规则适合在编译期计算或检查？

[上一章：为什么并非所有业务状态都适合用类层次表达？](28-algebraic-data-types.md) · [下一章：为什么有些规则适合在编译期计算或检查？](30-comptime-and-macros.md)
