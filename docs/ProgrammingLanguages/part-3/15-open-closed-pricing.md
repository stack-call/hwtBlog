---
title: "第 15 章｜为什么增加一种计价方式就要修改订单代码？"
sidebar_position: 15
description: "从订单系统的一次具体失败出发，理解为什么增加一种计价方式就要修改订单代码？，以及由此带来的语法、运行行为与设计取舍。"
slug: /programming-languages/15-open-closed-pricing
---

# 第 15 章：为什么增加一种计价方式就要修改订单代码？

运营又加了“学生折扣”。每次改价都得进入 `Order.total()` 添加一个 `elif`，改动范围远大于业务变化本身。

## 分支只有两条时可读性很好

分支只有两条时可读性很好，也最容易调试。我们不假设分支一定有害，而是观察它是否随外部变化反复被修改。

**教学实现：**

```python
def total(amount, mode):
    if mode == "vip": return amount * 90 // 100
    return amount
```

## 订单的核心流程没有变化

订单的核心流程没有变化，只是某一种计算规则增加了。每次修改同一个分支函数，会导致结算发布与运营规则绑定。具体失败模式是“变化集中挤进稳定模块”。

**用于触发限制的代码／输入（实验场景，不伪称来自框架）：**

```python
# 新活动要求 mode == "student" 时八折
# 若继续扩展 total，结算模块要随营销规则变化反复发布
```

**因此需要的能力。** 先识别 **稳定流程** 和 **易变规则**，再把计算规则从流程中抽离。这个识别比记忆任何接口关键词重要。

## 把新机制落实成代码

**改进后的教学实现：**

```python
def regular(x): return x
def vip(x): return x * 90 // 100
def student(x): return x * 80 // 100

def checkout(amount, pricing):
    if amount < 0: raise ValueError("negative")
    return pricing(amount)

print(checkout(200, student))  # 160
```

订单总流程只负责校验与调用，具体折扣作为传入的函数改变。新增加的 `student` 没有要求编辑 `checkout`。这是一种以变化点为单位的抽象。

## 调用 checkout(200,student)

调用 `checkout(200,student)`：200 通过非负校验，调用 `student(200)` 得到 160；换 `vip` 得 180。入口函数没有检测 `mode`，也不知道策略清单。

## Java 可以用函数式接口表达相同的变化点

Java 可以用函数式接口表达相同的变化点。

**Java 示例（教学代码，非所引用仓库的源码节选）：**

```java
import java.util.function.LongUnaryOperator;
static long checkout(long amount, LongUnaryOperator price) {
    if (amount < 0) throw new IllegalArgumentException();
    return price.applyAsLong(amount);
}
// checkout(200, x -> x * 80 / 100)
```

Java 的 `LongUnaryOperator` 限定了输入与输出类型，Python 函数参数默认没有这种静态约束。核心不是“必须用接口”，而是让变化通过参数进入稳定流程。

## 一种小变化为什么引起多处修改

订单目前只有普通用户，结账函数直接求和。VIP 需求很容易让人增加一个布尔参数：

```python
def price(items, vip=False):
    s = sum(items)
    return s * 90 // 100 if vip else s
```

这在两种规则时可读、可测，并非一出现 `if` 就必须重构。但再来优惠券、会员日、阶梯折扣，`price` 不仅要决定规则，还要实现所有规则；每次新增活动都要修改这个既有函数。真正的问题是**变化的维度开始独立于订单本身**。

可以先将规则从结账过程抽出来：

```python
def checkout(items, calculator):
    amount = calculator(items)
    if amount < 0:
        raise ValueError("negative payable")
    return amount
```

注意这个修改之后，`checkout` 不再负责决定哪个活动可用，但仍负责确保最终金额合法。这叫划分职责，而不是把所有校验都移走。后续如果外部计算器返回字符串，错误会在比较处暴露；于是接口契约成为下一章的自然问题。

可以比较两次需求改动需要修改哪些位置：原版新增策略要改 `price` 分支；新版只需增加计算器并在应用装配处选择它。**变化是否真正被隔离**，需要通过实际改动范围而非抽象词汇来判断。

## 对照规范和真实实现

**源码/规范阅读路线（供读者核对，并非声称已经逐行验证）：** [Python 3.13 可调用对象与函数参数语义](https://docs.python.org/3.13/reference/)。

先核对 Python 函数作为值传递的语义。Java 对照可以查 JDK `java.util.function.LongUnaryOperator` API 文档。不要把“每种策略可替换”直接等同于“运行时一定采用虚函数表”。

## 实验：加入 coupon(x)

加入 `coupon(x)`：满 100 减 30，否则原价，并调用同一 `checkout`。再尝试 `pricing=None`，看失败发生在何处。

**应当观察到（自行运行前属于预期）：** 折扣正常替换；None 会在调用处产生类型相关异常，说明我们还缺少对“策略能否调用”的显式契约。

## 边界：变化点抽离后测试更容易

变化点抽离后测试更容易，但抽象得过早会让业务阅读必须在多个文件间跳转。先观察实际变化频率，避免凭空制造策略类。

**跨语言迁移时，** 所有七门语言都可以把函数、接口实现或函数指针交给稳定流程；真正的差别是静态检查、运行时开销和表示方式。

**下一层问题：** 为什么同一个操作可以由不同组件实现？

[上一章：为什么函数也能作为数据传递？](../part-2/14-functions-as-values.md) · [下一章：为什么同一个操作可以由不同组件实现？](16-polymorphism.md)
