---
title: "第 19 章｜为什么需要从外部提供依赖对象？"
sidebar_position: 19
description: "从订单系统的一次具体失败出发，理解为什么需要从外部提供依赖对象？，以及由此带来的语法、运行行为与设计取舍。"
slug: /programming-languages/19-dependency-injection
---

# 第 19 章：为什么需要从外部提供依赖对象？

订单服务已经依赖抽象计价能力，但生产环境还要选活动、加载配置；测试环境则使用假数据。由谁决定具体对象从哪里来？

## 构造函数已允许传入依赖

构造函数已允许传入依赖，但如果各处调用方仍然散落着生产策略选择，整个项目的创建方式可能不一致。

**教学实现：**

```python
class Checkout:
    def __init__(self, pricing): self.pricing = pricing
    def total(self, x): return self.pricing.calculate(x)
```

## 同一业务环境里的两个入口构造了不同计价组件

同一业务环境里的两个入口构造了不同计价组件。问题不是 `Checkout` 的算法，而是装配规则分散在多个入口，难以统一测试和配置。

**用于触发限制的代码／输入（实验场景，不伪称来自框架）：**

```python
# 页面 A
checkout_a = Checkout(VipPricing())
# 页面 B 误用了另一种策略
checkout_b = Checkout(RegularPricing())
```

**因此需要的能力。** 把“创建并连接对象”的责任放到一个可控的组合入口。依赖注入描述依赖来自外部，组合根（composition root）是常用的装配地点。

## 把新机制落实成代码

**改进后的教学实现：**

```python
class RegularPricing:
    def calculate(self, x): return x
class Checkout:
    def __init__(self, pricing): self.pricing = pricing
    def total(self, x): return self.pricing.calculate(x)

def build_application():
    pricing = RegularPricing()
    return Checkout(pricing)

app = build_application()
print(app.total(200))
```

`build_application` 对生产策略作一次决定，`Checkout` 保持只知道如何使用。示例无需安装任何 DI 框架，也不需要扫描类、反射或神秘注解。

## 程序启动时先执行 build_application()

程序启动时先执行 `build_application()`，创建 `RegularPricing`，然后把该对象传入 `Checkout`；处理订单时 `Checkout.total` 使用已经装配的依赖。创建与执行发生在不同阶段。

## Go 常在 main 或构造函数中显式装配依赖

Go 常在 `main` 或构造函数中显式装配依赖。

**Go 示例（教学代码，非所引用仓库的源码节选）：**

```go
type Pricing interface { Calculate(int64) int64 }
type Checkout struct { Price Pricing }
func (c Checkout) Total(x int64) int64 {
    return c.Price.Calculate(x)
}
// 在 main 中选择具体实现并组装 Checkout
```

Go 没有强制要求 IoC 容器，接口和结构体就足够表达依赖注入。显式装配让依赖图容易查看，代价是需要维护创建代码。

## 注入和容器是两件不同的事

订单依赖价格计算器与支付网关。最简单的注入只需要构造参数：

```python
class Checkout:
    def __init__(self, pricing, gateway):
        self.pricing = pricing
        self.gateway = gateway
    def pay(self, items):
        cents = self.pricing(items)
        return self.gateway.charge(cents)
```

装配过程明确发生在程序入口：

```python
checkout = Checkout(regular_price, sandbox_gateway)
```

这里 `regular_price` 与 `sandbox_gateway` 是调用方已经准备好的对象；无需引入全局服务定位器或大型 DI 容器。这样测试时可以传入记录调用的假实现，不必启动真实网络服务。

当系统增加数百个对象，手工装配可能出现循环依赖、生命周期、作用域等问题，这时才有理由研究 DI 容器。但是容器帮忙**构造和连接对象**，不会自动判断依赖方向是否合理，也不能确保业务规则正确。

实验：把 `Checkout` 内部写死的网关改为构造参数，测试一次支付失败，检查是否恰好调用一次 `.charge`。然后尝试把同一个有状态网关共享给两个请求，观察它是否会意外共享请求状态。这里会自然通向对象生命周期管理。

## 对照规范和真实实现

**源码/规范阅读路线（供读者核对，并非声称已经逐行验证）：** [Go 规范：接口与结构体类型](https://go.dev/ref/spec)。

检查 Go 的接口类型与结构体字段规则；源码层面装配通常是应用代码行为而非特殊的 Runtime 机制。区分“语言允许传值”与“架构决定从哪里得到值”。

## 实验：让 build_application(confi

让 `build_application(config)` 接受 `"vip"` 或 `"regular"`，只在这个函数中选择策略；整个项目其他调用位置不写 `if mode`。

**应当观察到（自行运行前属于预期）：** 配置改变只影响组合入口的具体依赖，业务流程不改。若配置非法，应在装配阶段尽早报错，而不是让第一笔订单来时才失败。

## 边界：依赖注入并非总需要容器

依赖注入并非总需要容器：容器能自动装配复杂图，也会隐藏运行时关系和错误位置。显式构造通常是更好的最小起点。

**跨语言迁移时，** 这是一种软件装配方法，不属于 Python 或 Java 专利。七门语言都可以在函数参数、构造函数或模块工厂中传入依赖。

**下一层问题：** 为什么有些类型之间适合使用继承？

[上一章：为什么不直接依赖具体实现？](18-depend-on-abstractions.md) · [下一章：为什么有些类型之间适合使用继承？](20-when-inheritance-fits.md)
