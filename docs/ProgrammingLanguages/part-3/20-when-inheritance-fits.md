---
title: "第 20 章｜为什么有些类型之间适合使用继承？"
sidebar_position: 20
description: "从订单系统的一次具体失败出发，理解为什么有些类型之间适合使用继承？，以及由此带来的语法、运行行为与设计取舍。"
slug: /programming-languages/20-when-inheritance-fits
---

# 第 20 章：为什么有些类型之间适合使用继承？

普通配送和加急配送共享部分行为，开发者想复用代码。我们先区分“能复用实现”和“在行为上真是一种特殊类型”。

## 两个类之间有重复公式

两个类之间有重复公式，把重复写两遍在很小的程序里可以工作。若所有配送类型都承诺相同调用协议，可以考虑结构上的复用。

**教学实现：**

```python
class Shipping:
    def cost(self, weight): return weight * 2

class ExpressShipping:
    def cost(self, weight): return weight * 2 + 20
```

## 多个类型确实共享一段稳定基础行为

多个类型确实共享一段稳定基础行为。但“共享几行算法”不自动说明它们在业务含义上可相互替换，需要先识别共同契约。

**用于触发限制的代码／输入（实验场景，不伪称来自框架）：**

```python
# 新增保险运费、国际运费后，每个类复制基础运费算法
# 同一规则修改时容易有的类漏掉修改
```

**因此需要的能力。** 在确有一致语义的类型族中，可以把共同实现放在基类，再让派生类扩展。这个机制叫继承，但它不是复用的唯一工具。

## 把新机制落实成代码

**改进后的教学实现：**

```python
class Shipping:
    def base_cost(self, weight): return weight * 2
    def cost(self, weight): return self.base_cost(weight)

class ExpressShipping(Shipping):
    def cost(self, weight): return super().cost(weight) + 20

print(ExpressShipping().cost(10))  # 40
```

`ExpressShipping` 继承基本运费计算并覆盖 `cost` 增加加急费。`super()` 允许在当前方法内使用基类实现；它不应被理解为“把两份对象内存简单拼接”，具体布局由语言与实现决定。

## ExpressShipping().cost(10) 先进入重写方

`ExpressShipping().cost(10)` 先进入重写方法，再调用 `super().cost(10)`，其内部调用 `base_cost(10)` 得到 20，最后加 20 返回 40。`cost` 的调用路径出现了覆盖与委托。

## Java 用 extends 表达类继承

Java 用 `extends` 表达类继承，方法覆盖需要满足类型规则。

**Java 示例（教学代码，非所引用仓库的源码节选）：**

```java
class Shipping {
    long cost(long w) { return w * 2; }
}
class Express extends Shipping {
    @Override long cost(long w) { return super.cost(w) + 20; }
}
```

Java 的类继承和 Python 的动态类继承都可用于代码复用及子类型关系，但 Java 是单类继承；Rust、Go、Zig 不提供同样的类继承语法。

## 共享实现和可替换类型要分别论证

`ExpressShipping` 继承基础运费并额外加急费，看起来很自然。但“复用十行代码”和“加急配送能在任何要求普通配送的地方工作”并不是同一个命题。

```python
class Shipping:
    def quote(self, weight):
        if weight < 0: raise ValueError("weight")
        return 2 * weight

class Express(Shipping):
    def quote(self, weight):
        return super().quote(weight) + 20
```

对于非负重量，子类仍返回合法运费；但假设未来某个派生类只允许重量小于 10，而基类承诺支持任意非负重量，把派生类交给基类的调用者就会破坏前置条件。即使继承语法完全正确，替换关系依然可能是错误的。

还要观察 `super().quote(weight)`：它通过类的解析关系委托上级实现，并不是“先复制基类对象再调用”。Python 的方法解析顺序支持多继承；Java 只有单个直接类父类型；C++ 可以多继承，但对象布局更复杂。Rust、Go 和 Zig 通常采用组合、trait/interface 或显式委托表达类似需求。

本章应该保留继承作为**确有稳定 is-a 关系**时的选择，而不是把它定为代码复用的默认工具。

## 对照规范和真实实现

**源码/规范阅读路线（供读者核对，并非声称已经逐行验证）：** [JLS：Classes、Inheritance、Overriding](https://docs.oracle.com/javase/specs/jls/se24/html/)。

核对 `extends`、`super`、重写与访问规则。不要把“继承减少重复代码”直接当作“符合替换原则”的证明，下一章会专门构造不满足契约的反例。

## 实验：增加 EconomyShipping 返回基本运费

增加 `EconomyShipping` 返回基本运费的八折，再尝试在基类中增加新的公共规则，观察哪些派生类会受到影响。

**应当观察到（自行运行前属于预期）：** 共享基础方法的改动可能影响所有派生类；这既是复用收益，也是潜在的脆弱耦合。

## 边界：继承适合稳定、语义一致的类型关系

继承适合稳定、语义一致的类型关系，但当差别只是一个计算策略时，组合对象或函数往往更灵活。多层继承会让执行路径难以追踪。

**跨语言迁移时，** Python/Java/C++ 有类继承；Rust trait、Go 接口、Zig 结构组合、C 结构体加函数可以表达相似能力，但不能因此称为“等价继承”。

**下一层问题：** 为什么继承容易形成脆弱的类型体系？

[上一章：为什么需要从外部提供依赖对象？](19-dependency-injection.md) · [下一章：为什么继承容易形成脆弱的类型体系？](21-fragile-inheritance.md)
