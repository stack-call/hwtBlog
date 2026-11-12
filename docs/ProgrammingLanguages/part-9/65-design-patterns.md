---
title: "第 65 章｜为什么某些设计结构会反复出现？"
sidebar_position: 65
description: "从订单系统的一次具体失败出发，理解为什么某些设计结构会反复出现？，以及由此带来的语法、运行行为与设计取舍。"
slug: /programming-languages/65-design-patterns
---

# 第 65 章：为什么某些设计结构会反复出现？

订单系统出现几种熟悉的问题：计价策略要替换、旧接口要适配、日志要附加到调用边界。这些方案似乎总被不同项目重复发明。

## 一条固定分支在小项目里完全够用

一条固定分支在小项目里完全够用。只有真实出现多种独立变化时，才值得把问题识别成一个可复用设计结构。

**教学实现：**

```python
def checkout(amount, mode):
    if mode == "vip": return amount * 90 // 100
    return amount
```

## 三种变化并不相同

三种变化并不相同：策略替换、接口形状不一致、附加横切行为。若都堆进 checkout 一个函数，核心业务再次膨胀。

**用于触发限制的代码／输入（实验场景，不伪称来自框架）：**

```python
# 税率 API 只有 old_tax(x)，计价模块却只认识 calculate(x)
# 再加一个日志要求：每次计算前后记录输入与输出
```

**因此需要的能力。** 识别反复出现的问题结构，并为之使用足够小的成熟方案。设计模式是方案的名字，不是要求必须使用复杂类图。

## 把新机制落实成代码

**改进后的教学实现：**

```python
def old_tax(x): return x // 10

class TaxAdapter:
    def calculate(self, x):  # Adapter：转换旧接口
        return old_tax(x)

def logging_decorator(fn):
    def wrapped(x):          # Decorator：附加行为
        print("input", x)
        result = fn(x)
        print("result", result)
        return result
    return wrapped
```

Adapter 不改变旧函数，只转换调用约定；Decorator 用包装函数增加日志，不把日志写进每个业务策略。Strategy 则解决不同算法替换。三个模式分别有具体失败场景。

## 调用 TaxAdapter().calculate(100) 返回

调用 `TaxAdapter().calculate(100)` 返回 10；调用 `logging_decorator(old_tax)(100)` 先打印 input 100，再计算 10，再打印 result 10，最后返回 10。行为扩展位置和核心算法解耦。

## Java 的策略模式可能通过 interface 与实现类表达

Java 的策略模式可能通过 interface 与实现类表达，但不应强迫每个简单回调都创建类。

**Java 示例（教学代码，非所引用仓库的源码节选）：**

```java
interface Pricing { long apply(long x); }
Pricing vip = x -> x * 90 / 100;
Pricing regular = x -> x;
```

Java lambda 可以非常简洁地承载策略模式的核心意图；设计模式并不等于需要固定数量的类。

## 设计模式是给已经发生的设计命名

从普通/VIP 定价一路发展，订单把计价交给可替换对象。这时你会发现它与“策略模式”很像。但如果反过来先背策略模式，再给每个加法建立策略接口，就失去了最小设计的判断。

```python
def regular(items): return sum(items)
def vip(items): return sum(items) * 9 // 10

def checkout(items, strategy):
    return strategy(items)
```

这里一个函数参数已经实现最小策略。若后来策略需要状态、校验、多方法协作，才有理由增加类或接口。类似地，适配器用于对接**已有但签名不同**的 API；装饰器用于在原有操作边界外增加行为；工厂用于集中复杂构造逻辑。

要判断某模式是否必要，必须给出一个未经该模式处理就真实发生的冲突，而非“使代码更优雅”。模式的成本包括更多对象、间接调用和调试路径；收益则可能是让独立变化可以局部实现。

## 对照规范和真实实现

**源码/规范阅读路线（供读者核对，并非声称已经逐行验证）：** [JLS：Functional Interfaces / Lambda Expressions](https://docs.oracle.com/javase/specs/jls/se24/html/)。

通过语言规范确认接口与 lambda 的调用语义，再对照典型模式的目标与已知限制。不要把“看见类名 Factory”当作使用工厂模式的充分证据。

## 实验：把日志包装器套在 VIP 计价函数上

把日志包装器套在 VIP 计价函数上，再更换为普通计价，确认日志逻辑不修改计价算法。

**应当观察到（自行运行前属于预期）：** 两个策略都获得相同的输入输出日志；若日志本身失败，仍要设计是否影响业务，装饰器不自动保证可靠观测。

## 边界：模式减少重复设计成本

模式减少重复设计成本，也容易被过度使用。只有当问题与模式解决的矛盾吻合时，模式才能提升可维护性。

**跨语言迁移时，** Strategy、Adapter、Decorator 的核心想法可由 Java/C++ 类、Python 函数、Rust trait、Go interface、C 函数指针、Zig 结构/函数等表达。

**下一层问题：** 为什么业务逻辑不应该直接绑定数据库或网络？

[上一章：为什么代码复用不等于设计良好？](64-reuse-is-not-design.md) · [下一章：为什么业务逻辑不应该直接绑定数据库或网络？](66-layered-architecture.md)
