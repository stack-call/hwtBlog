---
title: "第 30 章｜为什么有些规则适合在编译期计算或检查？"
sidebar_position: 30
description: "从订单系统的一次具体失败出发，理解为什么有些规则适合在编译期计算或检查？，以及由此带来的语法、运行行为与设计取舍。"
slug: /programming-languages/30-comptime-and-macros
---

# 第 30 章：为什么有些规则适合在编译期计算或检查？

折扣档位来自固定的编译期配置。程序每次执行都重复构建一张相同的表；某些无效配置本可以在发布前就排除。

## 运行时构造数组直观而正确

运行时构造数组直观而正确。在请求量不大时，额外构造成本并不是问题。

**教学实现：**

```python
def build_rates():
    return [100, 90, 80]

def price(amount, tier):
    return amount * build_rates()[tier] // 100
```

## 固定表的形状在程序运行前就已知

固定表的形状在程序运行前就已知，但访问索引可能直到用户请求时才失败。如果配置本身是常量，有些工作能更早完成。

**用于触发限制的代码／输入（实验场景，不伪称来自框架）：**

```python
# 只允许 0..2，但 tier=3 会在运行时抛 IndexError
print(price(100, 3))
```

**因此需要的能力。** 区分“运行前已经确定的信息”与“运行时用户才提供的信息”。前者可以常量折叠、生成代码或在编译期间检查；后者仍必须在运行时验证。

## 把新机制落实成代码

**改进后的教学实现：**

```python
RATES = (100, 90, 80)
def price(amount: int, tier: int) -> int:
    if not 0 <= tier < len(RATES):
        raise ValueError("unknown tier")
    return amount * RATES[tier] // 100
```

Python 这里只是把固定表提到函数外并增加输入校验，不等于执行了完整编译期元编程。真正的编译期计算可以比较 Zig `comptime` 或 C++ `constexpr`。

## 初始化模块时建立一次不可变元组

初始化模块时建立一次不可变元组；调用 `price(100,1)` 读取比例 90 返回 90；`price(100,3)` 在索引访问前按业务规则抛 `ValueError`。要注意固定表不等于运行时输入已知。

## Zig 的 comptime 能在编译阶段评价依赖于已知值的表达式

Zig 的 `comptime` 能在编译阶段评价依赖于已知值的表达式。

**Zig 示例（教学代码，非所引用仓库的源码节选）：**

```zig
const std = @import("std");
fn rate(comptime tier: usize) u32 {
    const rates = [_]u32{ 100, 90, 80 };
    if (tier >= rates.len) @compileError("invalid tier");
    return rates[tier];
}
// const vip = rate(1);
```

这里 `tier` 必须是编译期已知值；来自网络请求的动态档位不能直接用同一个 `comptime` 参数处理。编译期检查的边界就是可用信息的边界。

## 何时值得把工作移到编译期

不同国家订单可能需要一个固定税率表。运行时查表是简单可靠的基线；如果规则在构建时已经确定，有些语言允许预先计算常量或生成专门实现。

```zig
const rates = [_]u8{ 0, 5, 10 };
const selected = rates[1];
```

Zig 的 `comptime` 不只是“宏文本替换”，它在编译阶段求值并可影响生成的程序结构。但是能在编译期处理，并不代表应该把频繁变化的运营活动写进构建产物：否则每次活动变更都要重新发布。

C++ `constexpr`、Rust `const`、Java 的编译期常量表达式与 Zig `comptime` 各自允许的操作与阶段不同。比较之前要明确：当前表达式是否依赖输入？是否会访问外部系统？生成代码是否随配置膨胀？

实验：将固定费率改成从数据库读取的新费率。你会发现它不再是构建时已知的信息；这时候把动态配置硬塞进编译期只会制造部署耦合。

## 对照规范和真实实现

**源码/规范阅读路线（供读者核对，并非声称已经逐行验证）：** [Zig 0.13.0 Language Reference：comptime](https://ziglang.org/documentation/0.13.0/)。

核对 `comptime` 参数何时可用及 `@compileError` 的触发条件；不要将 Java 的 JIT 或 Python 的常量元组说成完全相同的编译期机制。

## 实验：在 Zig 例子中调用 rate(3)

在 Zig 例子中调用 `rate(3)`，预测编译期是否通过；再思考如果 tier 来自命令行参数需要怎样设计运行时检验。

**应当观察到（自行运行前属于预期）：** 编译期已知的非法索引会触发编译错误；动态输入需要运行时的边界判断。能提前检查多少取决于信息何时可得。

## 边界：把工作前移到编译期可以消除部分运行时代价

把工作前移到编译期可以消除部分运行时代价，但会增加编译复杂性，且无法处理未知输入。不要把所有业务配置都强行做成常量。

**跨语言迁移时，** C++ constexpr、Zig comptime、Rust const、Go 常量求值、Java 编译期常量、Python 常量折叠各自能力不同；关键问题是“此刻掌握哪些事实”。

**下一层问题：** 一个整数和一个对象在机器里是什么？

[上一章：为什么空值和失败状态容易造成错误？](29-null-and-failure-states.md) · [下一章：一个整数和一个对象在机器里是什么？](../part-5/31-values-in-memory.md)
