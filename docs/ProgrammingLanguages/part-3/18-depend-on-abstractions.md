---
title: "第 18 章｜为什么不直接依赖具体实现？"
sidebar_position: 18
description: "从订单系统的一次具体失败出发，理解为什么不直接依赖具体实现？，以及由此带来的语法、运行行为与设计取舍。"
slug: /programming-languages/18-depend-on-abstractions
---

# 第 18 章：为什么不直接依赖具体实现？

订单服务直接 new 一个 `VipPricing`，导致单元测试必须带上完整营销配置。现在我们要替换成测试策略，却发现创建位置藏在业务逻辑内部。

## 业务永远只有一个计价实现时

业务永远只有一个计价实现时，直接构造依赖更短，也更容易理解。我们不把“依赖具体类”本身当作犯罪；需要看变化是否跨越了不相关的边界。

**教学实现：**

```python
class Checkout:
    def total(self, amount):
        pricing = VipPricing()  # 写死具体实现
        return pricing.calculate(amount)
```

## 测试只是想核对订单流程是否调用计价规则

测试只是想核对订单流程是否调用计价规则，却被具体策略构造过程绑定。业务流程与技术装配的变化原因不同，这种耦合开始产生实际成本。

**用于触发限制的代码／输入（实验场景，不伪称来自框架）：**

```python
# 测试需要固定返回 123，但 Checkout.total 内部总是创建 VipPricing
# 无法通过普通参数替换为 FakePricing，测试只能绕道 monkeypatch
```

**因此需要的能力。** 让高层流程依赖稳定的能力约定，而不是固定的具体类与构造地点。所谓依赖倒置，重点是让变化方向不再穿透稳定业务规则。

## 把新机制落实成代码

**改进后的教学实现：**

```python
class Checkout:
    def __init__(self, pricing):
        self.pricing = pricing
    def total(self, amount):
        return self.pricing.calculate(amount)

class FakePricing:
    def calculate(self, amount): return 123

assert Checkout(FakePricing()).total(999) == 123
```

高层 `Checkout` 只使用 `calculate` 能力。此时即使把它标注为 `Pricing`，核心收益仍然是构造依赖从内部移到了外部，替换不必修改类。

## 测试创建 FakePricing()

测试创建 `FakePricing()`，再创建 `Checkout(fake)`，调用时直接返回 `123`；生产环境把同一入口换为 `VipPricing()`。高层计算流程代码没有因测试场景而改变。

## Java 构造函数注入同样表达“稳定代码接受可替换依赖”

Java 构造函数注入同样表达“稳定代码接受可替换依赖”。

**Java 示例（教学代码，非所引用仓库的源码节选）：**

```java
interface Pricing { long calculate(long x); }
final class Checkout {
    private final Pricing pricing;
    Checkout(Pricing pricing) { this.pricing = pricing; }
    long total(long x) { return pricing.calculate(x); }
}
```

Java 中 `final` 保护了字段引用在构造后的重新赋值，不确保依赖对象内部不变。接口令编译器检查方法契约，构造注入令创建与使用分离，二者是独立机制。

## 为什么“依赖接口”不是“代码里写一个接口名”

如果订单里直接构造具体支付渠道：

```python
class Checkout:
    def pay(self, cents):
        gateway = CardGateway()  # 依赖在内部写死
        return gateway.charge(cents)
```

无论给 `CardGateway` 加多少父类或接口，调用者仍然在 `Checkout` 里决定使用哪一个实现。测试时想替换为不真正扣款的版本，就不得不修改代码或复杂地打补丁。

将依赖变成构造参数：

```python
class Checkout:
    def __init__(self, gateway):
        self.gateway = gateway
    def pay(self, cents):
        return self.gateway.charge(cents)

class FakeGateway:
    def __init__(self): self.calls = []
    def charge(self, cents):
        self.calls.append(cents)
        return "ok"

fake = FakeGateway()
assert Checkout(fake).pay(350) == "ok"
assert fake.calls == [350]
```

这里没有显式的 Python `Protocol` 也能替换，说明关键是**依赖方向和装配位置**。接口可以让合同更清晰、静态检查更早，但接口本身不会改变一个类内部硬编码具体依赖的事实。

若系统只有唯一渠道、没有替换或测试需求，构造参数也可能只是多一层间接；必要性要由变化和测试的具体痛点证明。

## 对照规范和真实实现

**源码/规范阅读路线（供读者核对，并非声称已经逐行验证）：** [Java Language Specification：Interfaces / Constructors](https://docs.oracle.com/javase/specs/jls/se24/html/)。

先用 JLS 核对接口、字段、构造器的语义，然后再阅读具体代码库如何装配对象。无需一开始引入 IoC 容器；容器只是管理装配的一种工具。

## 实验：制作 ExplodingPricing

制作 `ExplodingPricing`，在 `calculate` 中抛异常，检验 `Checkout.total` 是否传播异常；再比较 `FakePricing` 的稳定返回。

**应当观察到（自行运行前属于预期）：** 测试替身可以方便地注入成功或失败路径。这个结果展示边界可控，不等于整个系统已经实现了重试、事务或错误恢复。

## 边界：多加一个接口与构造参数会提高使用复杂性

多加一个接口与构造参数会提高使用复杂性。只有一份实现并且不需要隔离测试时，直接依赖具体函数可能更清楚。

**跨语言迁移时，** Rust 泛型注入、Go 接口字段、C 函数指针参数、Zig comptime 策略都可达到类似方向；依赖倒置是架构原则，不是某种语言关键字。

**下一层问题：** 为什么需要从外部提供依赖对象？

[上一章：为什么需要明确规定组件必须具备什么能力？](17-interfaces-and-contracts.md) · [下一章：为什么需要从外部提供依赖对象？](19-dependency-injection.md)
