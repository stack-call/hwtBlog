---
title: "第 27 章｜为什么相似的类型不一定能够互相替代？"
sidebar_position: 27
description: "从订单系统的一次具体失败出发，理解为什么相似的类型不一定能够互相替代？，以及由此带来的语法、运行行为与设计取舍。"
slug: /programming-languages/27-subtyping-and-variance
---

# 第 27 章：为什么相似的类型不一定能够互相替代？

系统有 `Order` 和 `PaidOrder`。开发者以为 `list[PaidOrder]` 必然可以交给任何 `list[Order]` 参数，因为每个已支付订单也是订单。

## 单个 PaidOrder 作为 Order 使用通常没有问题

单个 `PaidOrder` 作为 `Order` 使用通常没有问题，前提是派生类遵守行为契约。这个直觉来自子类型的可替换关系。

**教学实现：**

```python
class Order: pass
class PaidOrder(Order): pass

def show_one(o: Order):
    print(type(o).__name__)
show_one(PaidOrder())
```

## 容器是可变的

容器是可变的。若把仅允许已付款订单的列表当成普通订单列表传入，另一个函数就能向其中追加未付款订单，从而破坏容器元素约束。

**用于触发限制的代码／输入（实验场景，不伪称来自框架）：**

```python
def add_plain(xs: list[Order]):
    xs.append(Order())

paid_only: list[PaidOrder] = [PaidOrder()]
# 如果静态检查器允许 add_plain(paid_only)，列表就混入普通订单
```

**因此需要的能力。** 不仅判断 A 是否是 B 的子类型，还要判断 `Container[A]` 能不能安全作为 `Container[B]` 使用。这叫型变问题，取决于容器支持读、写还是二者都有。

## 把新机制落实成代码

**改进后的教学实现：**

```python
from typing import Sequence

def show_all(xs: Sequence[Order]) -> None:
    for x in xs: print(type(x).__name__)

paid_only: list[PaidOrder] = [PaidOrder()]
show_all(paid_only)  # 只读序列接口支持此类协变使用
```

`Sequence` 只提供读为主的操作契约，避免了前例通过 `append` 混入不匹配对象的危险。Python 静态类型工具将可变 `list[T]` 按不变性处理，而 `Sequence[T]` 可以协变。

## show_all 只遍历读取每个元素

`show_all` 只遍历读取每个元素，没有修改容器；把 `list[PaidOrder]` 当成 `Sequence[Order]` 使用时，读出的对象依旧满足 `Order` 约定。

## Java 泛型列表也是不变的

Java 泛型列表也是不变的，可通过 `? extends` 表达受限读取。

**Java 示例（教学代码，非所引用仓库的源码节选）：**

```java
void show(List<? extends Order> orders) {
    for (Order o : orders) System.out.println(o);
    // orders.add(new Order()); // 不允许
}
```

Java 的通配符约束与 Python `Sequence` 的只读接口类似地阻止某些不安全写入，但两者不是相同实现。

## 子类型关系为何需要考虑读和写

假设 `Dog` 是 `Animal` 的子类型。我们能否把 `list[Dog]` 直接当作可随意写入的 `list[Animal]`？如果允许：

```python
class Animal: pass
class Dog(Animal): pass
class Cat(Animal): pass

dogs = [Dog()]
animals = dogs
animals.append(Cat())
assert isinstance(dogs[1], Cat)
```

运行时这段普通 Python 代码能执行，但如果 `dogs` 对外宣称永远只装 `Dog`，写入 `Cat` 就破坏了静态类型假设。因此静态类型检查器通常不能把可变的 `list[Dog]` 直接视为 `list[Animal]`。这就是为什么必须同时考虑“能读出什么”和“能写入什么”。

只读序列更容易建立安全的协变关系，因为消费者不能往里放不属于具体子类型的新元素。函数参数和返回值也有相应的方向性问题：实现可以接受更广的输入、返回更具体的结果，但反过来可能破坏调用者的预期。

这里并非要求马上记住协变和逆变的术语。先保留这个失败：**一个容器既能读又能写，简单的子类型直觉就不总成立。**

## 对照规范和真实实现

**源码/规范阅读路线（供读者核对，并非声称已经逐行验证）：** [JLS：Subtyping、Type Arguments、Wildcards](https://docs.oracle.com/javase/specs/jls/se24/html/)。

核对 Java 通配符与类型赋值规则；再查 Python typing 中 `list` 和 `Sequence` 的型变声明。不要凭“继承了某类”判断所有泛型容器都可替换。

## 实验：在静态类型工具中分别测试 list[PaidOrd

在静态类型工具中分别测试 `list[PaidOrder]` 传给 `list[Order]` 与 `Sequence[Order]`，并尝试在函数内部追加普通 `Order`。

**应当观察到（自行运行前属于预期）：** 前者通常因列表不变性被拒绝；后者可以作为只读序列契约使用，但不能通过 `Sequence` 调用 `append`。

## 边界：型变增强类型安全

型变增强类型安全，却使 API 签名更精细。只需要读取时不要要求可变列表，既降低约束也更清楚地表达意图。

**跨语言迁移时，** Java、C++、Rust、Go、Python 对容器类型关系及子类型的表达差异很大。真正要比较的是谁能读、谁能写、怎样保证写入不破坏约束。

**下一层问题：** 为什么并非所有业务状态都适合用类层次表达？

[上一章：为什么存在不同形式的多态？](26-forms-of-polymorphism.md) · [下一章：为什么并非所有业务状态都适合用类层次表达？](28-algebraic-data-types.md)
