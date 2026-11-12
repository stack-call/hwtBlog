---
title: "第 21 章｜为什么继承容易形成脆弱的类型体系？"
sidebar_position: 21
description: "从订单系统的一次具体失败出发，理解为什么继承容易形成脆弱的类型体系？，以及由此带来的语法、运行行为与设计取舍。"
slug: /programming-languages/21-fragile-inheritance
---

# 第 21 章：为什么继承容易形成脆弱的类型体系？

普通配送和加急配送的继承看起来不错。但团队推出“免费配送”，它重写 `cost` 直接返回 0，使得基类调用方此前依赖的条件不再成立。

## 基类为所有配送对象提供了输入前提

基类为所有配送对象提供了输入前提：负重量应拒绝。调用方可能依赖这个规则来发现上游错误。

**教学实现：**

```python
class Shipping:
    def cost(self, weight):
        if weight < 0: raise ValueError("negative weight")
        return weight * 2
```

## FreeShipping 仍然是 Shipping 的子类

`FreeShipping` 仍然是 `Shipping` 的子类，却没有遵守基类拒绝负重量的约定。只用“语法上继承了”无法保证可替换的业务语义。

**用于触发限制的代码／输入（实验场景，不伪称来自框架）：**

```python
class FreeShipping(Shipping):
    def cost(self, weight):
        return 0  # 对负重量也直接返回 0
```

**因此需要的能力。** 使用子类型时必须考察行为契约：替换实现后，调用方依赖的前置条件、输出承诺和状态约束是否保持。里氏替换原则关注的是这种可观察行为。

## 把新机制落实成代码

**改进后的教学实现：**

```python
class FreeShipping(Shipping):
    def cost(self, weight):
        if weight < 0:
            raise ValueError("negative weight")
        return 0

for s in [Shipping(), FreeShipping()]:
    try:
        s.cost(-1)
    except ValueError:
        print("rejected")
```

修复后的免费配送仍允许费用不同，但保留了拒绝负重量的共同承诺。这里我们不是要求所有子类型输出相同数字，而是要求它们满足调用方赖以成立的约束。

## 调用方循环使用 Shipping 和 FreeShipping

调用方循环使用 `Shipping` 和 `FreeShipping`，输入 `-1` 时两者都抛 `ValueError`；输入 `10` 时分别返回 20 和 0。结果可以不同，只要共同业务契约没有被违反。

## C++ 可以通过虚函数允许派生类重写

C++ 可以通过虚函数允许派生类重写，但编译器不检查业务契约。

**C++ 示例（教学代码，非所引用仓库的源码节选）：**

```cpp
struct Shipping {
    virtual long cost(long weight) const = 0;
    virtual ~Shipping() = default;
};
struct FreeShipping : Shipping {
    long cost(long weight) const override {
        if (weight < 0) throw std::invalid_argument("negative");
        return 0;
    }
};
```

`override` 能检查确实覆盖了基类虚函数，却不会自动证明负重量处理符合约定。编译期类型检查和业务契约验证是两种不同能力。

## 用一个违反替换原则的测试推导问题

假设普通配送承诺“任何非负重量都能报价”，但子类改变了接受范围：

```python
class Shipping:
    def quote(self, weight):
        if weight < 0: raise ValueError
        return weight * 2

class SmallOnly(Shipping):
    def quote(self, weight):
        if weight > 10: raise ValueError("too heavy")
        return super().quote(weight)

def weekly_quote(service):
    return service.quote(20)
```

`weekly_quote(Shipping())` 成功，`weekly_quote(SmallOnly())` 却失败。类型关系在语法上成立，但子类收紧了调用者原本被允许的输入。这比简单地说“继承层次太深”更具体。

若这两种配送本来是不同能力，就该改变模型：让调用方显式询问是否支持重量，或者将能力约束放进合同，而不是把不兼容类型强行放进同一承诺下面。

替换原则也涉及返回值和副作用。子类即使能返回数字，若悄悄重复扣款，也破坏了调用者对服务的假设。编译器无法证明完整业务契约，测试与模型设计仍然必要。

通过这个反例再回看组合：订单持有配送策略，只要求它满足当前明确的接口；若策略能力不同，业务可以在选择策略时显式处理。

## 对照规范和真实实现

**源码/规范阅读路线（供读者核对，并非声称已经逐行验证）：** [C++ 语言参考：virtual functions、override](https://en.cppreference.com/w/)。

核对虚函数覆盖与析构规则，并单独写业务测试断言非法重量必须被拒绝。不要将 `override` 误当成自动的形式化规格检查器。

## 实验：写一个统一契约测试

写一个统一契约测试：对每种 Shipping 实现输入 `-1` 必须拒绝，输入 `0` 返回非负值。新增实现时复用这组测试。

**应当观察到（自行运行前属于预期）：** 最初的 `FreeShipping` 无法通过负重量测试；修复后通过。合同测试不能证明所有输入都正确，但能守住关键行为边界。

## 边界：类型继承使实现耦合

类型继承使实现耦合，在深层继承链中修改基类可能产生广泛影响。共同契约稳定但实现易变时，接口加组合常常更易维护。

**跨语言迁移时，** Java 和 C++ 可以形成类层次；Rust trait 和 Go interface 也需要行为契约，即使没有类继承。里氏替换的核心是行为，不是某个语言关键词。

**下一层问题：** 为什么不同类型需要复用同一套算法？

[上一章：为什么有些类型之间适合使用继承？](20-when-inheritance-fits.md) · [下一章：为什么不同类型需要复用同一套算法？](22-generics-reuse-algorithms.md)
