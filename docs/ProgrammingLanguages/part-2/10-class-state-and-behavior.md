---
title: "第 10 章｜为什么要把数据与操作组织在一起？"
sidebar_position: 10
description: "从订单系统的一次具体失败出发，理解为什么要把数据与操作组织在一起？，以及由此带来的语法、运行行为与设计取舍。"
slug: /programming-languages/10-class-state-and-behavior
---

# 第 10 章：为什么要把数据与操作组织在一起？

订单的验证、付款、改价已经写了好几个自由函数。调用者频繁传入同一个 `order`，难以看清哪些操作属于订单自身。

## 函数加数据在小程序中完全有效

函数加数据在小程序中完全有效，C 中也经常用这种模式。它甚至更容易测试纯函数。引入方法不是为了宣称函数不够“面向对象”，而是为了让同一实体的操作具有稳定的查找与调用位置。

**教学实现：**

```python
def pay(order): order["paid"] = True
def amount(order): return order["amount"]
order = {"amount": 100, "paid": False}
pay(order)
```

## 当多个订单同时在内存中时

当多个订单同时在内存中时，调用者很容易把动作作用到错误对象。我们需要一个更加直接的表达：“让这笔订单执行付款”。这并不能杜绝逻辑错误，但能让代码的责任关系更加可读。

**用于触发限制的代码／输入（实验场景，不伪称来自框架）：**

```python
# 每添加一个动作，都依赖调用者自行配对对象和函数
pay(order)
# pay(other_order) 写错对象也完全符合语法
```

**因此需要的能力。** 给特定数据实例附着一组可调用操作：对象是某个状态集合与行为入口的结合，类是 Python/Java/C++ 等语言中构造此关系的一种方式。

## 把新机制落实成代码

**改进后的教学实现：**

```python
class Order:
    def __init__(self, amount: int):
        self.amount = amount
        self.paid = False
    def pay(self):
        if self.paid: raise ValueError("paid twice")
        self.paid = True

order = Order(100)
order.pay()
print(order.paid)  # True
```

`self` 表示当前接收操作的实例；`order.pay()` 可以直观理解为选中 `order`，然后对它执行 `pay`。但对象方法不一定比普通函数更安全，尤其当字段仍是公开可写时。

## Order(100) 创建实例并运行 __init__ 设置两个属

`Order(100)` 创建实例并运行 `__init__` 设置两个属性；`order.pay()` 将该实例作为方法的接收对象，检查之前的 `paid` 为 False，再修改为 True。另一个 `Order(250)` 不会因这个实例的付款而改变。

## Rust 没有 Java 式的类继承

Rust 没有 Java 式的类继承，但能为结构体定义方法。

**Rust 示例（教学代码，非所引用仓库的源码节选）：**

```rust
struct Order { amount: i64, paid: bool }
impl Order {
    fn pay(&mut self) { self.paid = true; }
}
let mut order = Order { amount: 100, paid: false };
order.pay();
```

Rust `impl` 将方法与类型关联，`&mut self` 还明确表达本次调用借用并可能修改对象。方法不是“必须动态分派的函数”；是否动态分派是另一层问题。

## 方法调用里面隐藏了哪个输入？

我们已经让订单只能通过 `add_item` 修改商品。但下面两个写法在职责上很接近：

```python
class Order:
    def __init__(self, items): self.items = list(items)
    def total(self): return sum(self.items)

order = Order([100, 250])
assert order.total() == 350
assert Order.total(order) == 350
```

在这个 Python 例子里，`order.total()` 的属性访问绑定了接收者，因此调用者不用显式写第一个参数；直接从类上取得 `Order.total` 时，就要显式提供实例。这帮助我们理解 `self` 是传入方法的接收者，而不是什么隐形的全局变量。

同样的抽象在其他语言并不一定需要类：

```go
// 教学片段：定义一个有接收者的方法
// func (o Order) Total() int64 { ... }
```

Go 用接收者声明方法，Rust 使用 `impl Order { fn total(&self) ... }`，C 也可以写 `total(const Order *o)`。语法不同，都是在表达“这个操作对哪份订单数据工作”。

还要注意：给状态添加方法，不会自动修复所有封装漏洞。如果 `Order.items` 仍然是公开可变列表，`order.items.append(999)` 依旧可能绕开业务校验。封装要看真实可访问路径，而非是否出现 `class`。

## 对照规范和真实实现

**源码/规范阅读路线（供读者核对，并非声称已经逐行验证）：** [Python 3.13 类、实例与方法调用规则](https://docs.python.org/3.13/reference/)。

追踪从 `order.pay` 属性查找到调用的语言规则，再在 CPython 源码中寻找绑定方法对象相关实现。首次只看一次正常路径，不急着研究描述符协议的所有细节。

## 实验：分别创建两笔订单

分别创建两笔订单，支付其中一笔；然后将 `Order.pay(order2)` 与 `order2.pay()` 对照，观察接收对象怎样传递。

**应当观察到（自行运行前属于预期）：** 只支付选中的实例；`Order.pay(order2)` 在这个例子中可以显式传入接收对象，显示方法调用与函数调用存在联系。

## 边界：类把状态与行为组织在一起

类把状态与行为组织在一起，不意味着所有计算都应塞进某个“大对象”。纯转换、通用算法和无状态规则仍可能更适合自由函数。

**跨语言迁移时，** Python/Java/C++ 偏向类与对象语法，Rust/Go/Zig 通过结构体与方法也能形成相似组织，C 通过 `struct` 和接收指针的函数可以表达同样的业务意图。

**下一层问题：** 为什么有些数据应该只读？

[上一章：为什么不能允许任意代码修改订单？](09-encapsulation.md) · [下一章：为什么有些数据应该只读？](11-immutability.md)
