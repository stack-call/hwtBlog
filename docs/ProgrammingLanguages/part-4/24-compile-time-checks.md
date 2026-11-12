---
title: "第 24 章｜为什么有些错误可以在执行前发现？"
sidebar_position: 24
description: "从订单系统的一次具体失败出发，理解为什么有些错误可以在执行前发现？，以及由此带来的语法、运行行为与设计取舍。"
slug: /programming-languages/24-compile-time-checks
---

# 第 24 章：为什么有些错误可以在执行前发现？

结算系统上线后才发现某个分支把 `"250"` 当金额传入。明明类型错误与业务流量无关，为什么必须等到生产订单经过那条分支才失败？

## Python 动态检查实际操作

Python 动态检查实际操作，整数输入能正常运行，写起来很简洁。对于快速脚本、探索性任务，运行时发现类型不匹配未必是问题。

**教学实现：**

```python
def checkout(amount):
    return amount * 90 // 100

print(checkout(250))
```

## 只运行正常样例

只运行正常样例，无法覆盖未走到的路径。一个输入明显与操作不匹配，但执行时才暴露，容易在庞大程序中被延后发现。

**用于触发限制的代码／输入（实验场景，不伪称来自框架）：**

```python
if False:
    checkout("250")  # 这条分支本次没有运行
# 运行成功不代表隐藏分支的类型操作正确
```

**因此需要的能力。** 在没有具体运行所有分支的情况下，根据变量与操作的类型约束先排除一部分不合法程序。这是静态类型检查的重要价值。

## 把新机制落实成代码

**改进后的教学实现：**

```python
def checkout(amount: int) -> int:
    return amount * 90 // 100

ok = checkout(250)
# bad = checkout("250")  # 静态检查器应报告参数类型不匹配
```

注解给检查工具提供信息，但 Python 默认运行时不强制执行；要提前发现错误需要运行类型检查器。静态类型系统的收益与检查器覆盖范围、类型信息质量相关。

## 静态分析 checkout("250") 时

静态分析 `checkout("250")` 时，调用处推断实参为 `str`，函数要求 `int`，因此可在真正执行前报告不兼容。若数据来自未标注的外部边界，分析器可能无法完全判断。

## Java 对同样的参数不匹配在编译期报错

Java 对同样的参数不匹配在编译期报错。

**Java 示例（教学代码，非所引用仓库的源码节选）：**

```java
static long checkout(long amount) {
    return amount * 90 / 100;
}
// long n = checkout("250");  // 编译错误
```

Java 的内建静态类型检查与 Python 外置静态检查工具的工作流程不同；两者都无法替你验证“250 是否真是一笔合法订单”。

## 不要把静态类型当成外部输入的守门员

假设来自 API 的金额字段是 `"350"`。Java 的 `long cents` 无法直接承接 JSON 字符串，但反序列化器或手工解析器必须先接触这份原始数据。静态类型检查发生在程序构建阶段；网络上究竟发送什么，编译器并不知道。

```python
def parse_cents(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    raise ValueError("invalid cents")
```

`object` 表示调用方可能给任意 Python 值，函数在边界将它收窄成有效的 `int`。对照 Rust 的 `str::parse::<i64>() -> Result<i64, _>`，你会看到另一套显式表达失败的语法。

动态类型并不代表“没有类型”：字符串和整数在运行时仍区分；静态类型也不代表“程序永不出错”：数组越界、网络超时、溢出和逻辑错误仍可能存在。

实验不要只验证 `[100, 250]`，还应输入 `True`、`"3.50"`、`None`、超过目标整数范围的巨大值，明确每一种是拒绝、转换还是交给业务层处理。

## 对照规范和真实实现

**源码/规范阅读路线（供读者核对，并非声称已经逐行验证）：** [JLS：Types、Expressions、Conversions](https://docs.oracle.com/javase/specs/jls/se24/html/)。

核对 Java 参数转换与方法调用的语言规则；在 Python 端查官方 typing 文档，明确标注不等于默认运行时检查。不要把“能在编译期检查某类错误”说成“程序不会出错”。

## 实验：用 python -m compileall 编译

用 `python -m compileall` 编译带类型标注的脚本，再分别调用 `checkout("250")` 和运行静态类型检查器，比较能发现什么。

**应当观察到（自行运行前属于预期）：** `compileall` 主要编译语法和字节码，不进行完整静态类型校验；实际调用字符串版本会在算术操作中失败；类型检查器可提前报告不匹配。

## 边界：动态类型提供灵活性

动态类型提供灵活性，静态类型提供部分提前约束；“静态一定更快”“动态一定更简单”都不是从类型机制单独可以得出的结论。

**跨语言迁移时，** Rust/Go/Zig/C/C++/Java 静态检查发生在各自编译流程中，Python 更依赖运行时检查与可选的静态工具。重要的是类型约束覆盖了哪些行为。

**下一层问题：** 为什么不必为每个变量手写类型？

[上一章：为什么大型程序需要模块边界？](../part-3/23-module-boundaries.md) · [下一章：为什么不必为每个变量手写类型？](25-type-inference.md)
