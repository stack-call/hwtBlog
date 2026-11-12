---
title: "第 26 章｜为什么存在不同形式的多态？"
sidebar_position: 26
description: "从订单系统的一次具体失败出发，理解为什么存在不同形式的多态？，以及由此带来的语法、运行行为与设计取舍。"
slug: /programming-languages/26-forms-of-polymorphism
---

# 第 26 章：为什么存在不同形式的多态？

同一个 `total()` 有时要处理多种类型的容器，有时要调用不同的计价实现。它们都被称为“多态”，但究竟是不是同一种机制？

## 相同算法处理不同元素类型

相同算法处理不同元素类型，像“把类型放在一个空位”。这与 `pricing.calculate()` 选择不同算法是两回事。

**教学实现：**

```python
def first(xs): return xs[0]
print(first([1, 2]))
print(first(["A", "B"]))
```

## 如果把泛型复用和策略动态选择混为一谈

如果把泛型复用和策略动态选择混为一谈，分析性能时就容易断言“泛型总需要虚表”或“多态总是动态调用”。这两句都不对。

**用于触发限制的代码／输入（实验场景，不伪称来自框架）：**

```python
# first 不依赖元素类型；而下例行为确实因策略而异
class VIP:
    def calculate(self, x): return x * 90 // 100
class Full:
    def calculate(self, x): return x
```

**因此需要的能力。** 区分至少三类情况：参数化多态（类型参数）、子类型多态（共同能力接口）、临时多态（同名操作依类型有不同实现，如重载）。

## 把新机制落实成代码

**改进后的教学实现：**

```python
from typing import TypeVar, Sequence
T = TypeVar('T')
def first(xs: Sequence[T]) -> T: return xs[0]

def checkout(x: int, strategy) -> int:
    return strategy.calculate(x)
```

`first` 复用同一算法并保持元素类型关系；`checkout` 统一调用约定但具体算法可变化。这两个函数的差异比它们是否都叫“多态”更重要。

## 对 [1,2] 的 first 与 ["A","B"] 的 fir

对 `[1,2]` 的 `first` 与 `["A","B"]` 的 `first` 都做“取第一个”；对 `VIP` 与 `Full` 的 `checkout`，相同输入 100 得到 90 或 100。前者改变类型实例，后者改变行为实现。

## C++ 同时支持模板参数化和虚函数动态分派

C++ 同时支持模板参数化和虚函数动态分派，区别特别清楚。

**C++ 示例（教学代码，非所引用仓库的源码节选）：**

```cpp
template<class T>
T identity(T x) { return x; }

struct Pricing {
    virtual long calc(long x) const = 0;
    virtual ~Pricing() = default;
};
```

模板实例化通常在编译期产生针对类型的实现；虚函数设计允许运行时选择实现。优化器可能消除一些间接调用，但它们的语言机制不同。

## 三种都叫多态，却在解决不同的问题

考虑订单打印：`print(order)` 可以针对不同对象选择表示方法，这类行为依赖具体值的类型；`sort<T>` 则希望同一算法适用于许多元素类型；`+` 在整数与字符串上有不同含义。这三种现象的机制和约束并不相同。

以统一的 `contains` 为例：

```python
from typing import TypeVar
T = TypeVar("T")
def contains(xs: list[T], x: T) -> bool:
    return x in xs
```

这是参数化多态的语义方向。若传入具备共同接口的定价对象并调用 `.calculate()`，我们更多讨论子类型或结构协议带来的行为替换。若一个名字 `+` 对若干类型有不同已定义行为，可从操作符重载或临时多态的角度理解。

这种区分影响实现：C++ 模板常可为具体类型生成对应代码，Java 泛型常通过擦除实现，Python 通常在运行时处理对象操作；不能从“都是泛型”推断同样的机器表示或性能。

实验：列出你写过的 `print(x)`、`len(x)`、`list[T]`、`service.charge()`，分别解释调用方依据什么约定使用它，而不是只给它贴一个“多态”标签。

## 对照规范和真实实现

**源码/规范阅读路线（供读者核对，并非声称已经逐行验证）：** [C++ templates / virtual functions 参考](https://en.cppreference.com/w/)。

分别阅读模板实例化与虚函数行为规范，切勿把它们统一归为“运行时查方法”。再用自己的话解释 Python 鸭子类型为何不要求显式继承。

## 实验：写一个泛型 last 和两个 Pricing 实现

写一个泛型 `last` 和两个 `Pricing` 实现，测试输入类型变化与策略变化分别影响什么。

**应当观察到（自行运行前属于预期）：** `last` 只改变返回元素的类型；不同策略在相同数值输入下返回不同结果。两种变化是正交维度，可以组合。

## 边界：参数化泛型可能带来编译时间或代码大小成本

参数化泛型可能带来编译时间或代码大小成本，动态分派可能增加间接调用与接口约束成本。必须结合具体语言实现分析。

**跨语言迁移时，** Java 类型擦除、C++ 模板实例化、Rust 单态化与 trait object、Go 泛型与 interface、Zig comptime、Python Protocol 形成不同组合，不应按一个“多态性能”标签粗暴排名。

**下一层问题：** 为什么相似的类型不一定能够互相替代？

[上一章：为什么不必为每个变量手写类型？](25-type-inference.md) · [下一章：为什么相似的类型不一定能够互相替代？](27-subtyping-and-variance.md)
