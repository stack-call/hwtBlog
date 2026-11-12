---
title: "第 14 章｜为什么函数也能作为数据传递？"
sidebar_position: 14
description: "从订单系统的一次具体失败出发，理解为什么函数也能作为数据传递？，以及由此带来的语法、运行行为与设计取舍。"
slug: /programming-languages/14-functions-as-values
---

# 第 14 章：为什么函数也能作为数据传递？

计价组件仅负责一行小计算，却要为每种折扣新建类。我们发现真正需要变化的是“计算动作”，不一定需要有状态的对象。

## 策略类适合管理需要配置或状态的复杂逻辑

策略类适合管理需要配置或状态的复杂逻辑，但对只有一个公式的小策略，创建一个类可能比需求本身还重。

**教学实现：**

```python
class VipPrice:
    def calculate(self, x): return x * 90 // 100
class RegularPrice:
    def calculate(self, x): return x
```

## 类可以工作，却导致大量只有单个方法的样板。这里真正

类可以工作，却导致大量只有单个方法的样板。这里真正需要的只有“一段可在另一个函数中调用的计算”。

**用于触发限制的代码／输入（实验场景，不伪称来自框架）：**

```python
# 新增满减活动时又要写一个类，尽管它没有任何实例状态
class Minus20:
    def calculate(self, x): return max(0, x - 20)
```

**因此需要的能力。** 让函数本身作为值传递。这样核心流程不需要知道策略来自某个类、lambda 还是普通函数。

## 把新机制落实成代码

**改进后的教学实现：**

```python
def checkout(amount: int, pricing) -> int:
    return pricing(amount)

def vip(amount: int) -> int:
    return amount * 90 // 100

print(checkout(200, vip))
print(checkout(200, lambda x: max(0, x - 20)))
```

传入的 `pricing` 是可调用对象；`checkout` 不必预先定义所有算法。函数可以作为参数、返回值或保存在字段中。若匿名函数引用外层变量，还会引出闭包捕获问题。

## 第一次调用 pricing 绑定到函数 vip

第一次调用 `pricing` 绑定到函数 `vip`，得到 `180`；第二次绑定到一个 lambda，得到 `180`。调用语句相同，但实际执行的计算不同。这里已经出现一种不依赖类继承的行为多态。

## Go 的函数也是一等值

Go 的函数也是一等值，能够传给另一个函数。

**Go 示例（教学代码，非所引用仓库的源码节选）：**

```go
func checkout(amount int64, price func(int64) int64) int64 {
    return price(amount)
}
// checkout(200, func(x int64) int64 { return x * 90 / 100 })
```

Go 的 `func(int64) int64` 明确了可调用签名；Python 在当前代码里动态检查调用是否可行。函数值不等于“没有闭包分配成本”，具体成本需要分析实现。

## “函数是值”对可变业务规则意味着什么

计价规则最初被硬编码为 `sum(items)`，优惠活动出现后，我们希望不修改订单求和器就能换规则。此时最小替代方案甚至不需要类：

```python
def checkout(items, price_fn):
    return price_fn(items)

def regular(items): return sum(items)
def vip(items): return sum(items) * 9 // 10
assert checkout([100, 200], regular) == 300
assert checkout([100, 200], vip) == 270
```

`price_fn` 在这里是一个值：调用者能把某个函数作为参数传入。更进一步，若活动折扣率在运行时决定，可以让内部函数捕获外层变量：

```python
def discount(rate):
    def calculate(items):
        return sum(items) * rate // 100
    return calculate

assert checkout([100, 200], discount(80)) == 240
```

`calculate` 在 `discount` 已返回后仍能访问捕获的 `rate`，这正是闭包需要解释的现象。捕获的不是“文本复制”，而是与自由变量相关的环境；不同语言对按值、按引用、逃逸和生命周期的规则不同。

如果规则仅是短小函数，函数注入往往比先建立复杂类体系更合适；有状态、多个协作方法或扩展约束时，再考虑接口/对象。

## 对照规范和真实实现

**源码/规范阅读路线（供读者核对，并非声称已经逐行验证）：** [Python 3.13 函数对象、闭包和表达式规则](https://docs.python.org/3.13/reference/)。

查语言规范中 lambda、自由变量与作用域；如果进入 CPython 源码，先从函数对象定义和调用通道观察，而不是直接把闭包解释成某一种固定内存结构。

## 实验：写一个 make_discount(percent

写一个 `make_discount(percent)` 返回 lambda，再创建 `d90=make_discount(90)` 和 `d80=make_discount(80)`，分别计算 200。

**应当观察到（自行运行前属于预期）：** 两个闭包分别返回 180 与 160；它们共享代码形状，但捕获了不同的外层参数绑定。

## 边界：回调在简单行为替换中很轻量

回调在简单行为替换中很轻量，但如果策略需要很多相互关联的方法、配置与生命周期，接口对象可能更清晰。函数式与面向对象不是非此即彼。

**跨语言迁移时，** Python、Java、C、C++、Rust、Zig、Go 都支持某种可调用值或函数引用；C 函数指针与可携带环境的闭包不同，Java lambda 也有自己的捕获限制。

**下一层问题：** 为什么增加一种计价方式就要修改订单代码？

[上一章：为什么有时组合比扩展一个大类更合理？](13-composition-over-inheritance.md) · [下一章：为什么增加一种计价方式就要修改订单代码？](../part-3/15-open-closed-pricing.md)
