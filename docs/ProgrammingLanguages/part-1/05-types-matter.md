---
title: "第 05 章｜为什么相同的代码面对不同数据会失败？"
sidebar_position: 5
description: "从订单系统的一次具体失败出发，理解为什么相同的代码面对不同数据会失败？，以及由此带来的语法、运行行为与设计取舍。"
slug: /programming-languages/05-types-matter
---

# 第 05 章：为什么相同的代码面对不同数据会失败？

订单数据改从外部输入。一笔订单金额变成字符串 `"250"`，前几章的比较运算突然抛出异常；业务方却以为任何“看起来像数字”的东西都能相加。

## 当列表里全是整数时

当列表里全是整数时，函数的每个比较、加法操作都有定义。直到现在，调用者和函数都依赖一条未写明的前提：输入元素是可以比较并相加的数。

**教学实现：**

```python
def sales_total(xs):
    return sum(x for x in xs if x >= 0)
print(sales_total([100, 250]))  # 350
```

## "250" 在人看来是金额

`"250"` 在人看来是金额，但它是字符串；表达式 `"250" >= 0` 对这些操作数没有定义。失败恰好发生在 `if` 比较，而不是 `sum()` 的最后返回。

**用于触发限制的代码／输入（实验场景，不伪称来自框架）：**

```python
sales_total([100, "250"])
# TypeError: 字符串不能按此方式与整数比较
```

**因此需要的能力。** 明确值与操作之间的合法关系，并决定何时检查。类型系统是对“哪些操作在何种值上成立”的系统化约束，而不是只给变量写标签。

## 把新机制落实成代码

**改进后的教学实现：**

```python
def parse_amount(raw: str) -> int:
    return int(raw)

def sales_total(xs: list[int]) -> int:
    return sum(x for x in xs if x >= 0)

print(sales_total([100, parse_amount("250")]))
```

我们把外部字符串转换集中在输入边界，再让核心算法处理已定义的数据。`list[int]` 是静态工具可检查的意图；Python 运行时不会因为标注自动拒绝 `list[str]`，所以边界转换仍然不可省。

## "250" 首先由 int 转换为整数 250

`"250"` 首先由 `int` 转换为整数 `250`，然后与 `100` 一起进入求和函数，比较 `250 >= 0` 成立，最终返回 `350`。如果输入是 `"abc"`，转换阶段就会抛 `ValueError`，错误位置因此更接近输入来源。

## Java 对数组元素类型进行编译期检查

Java 对数组元素类型进行编译期检查。

**Java 示例（教学代码，非所引用仓库的源码节选）：**

```java
static long total(long[] xs) {
    long s = 0;
    for (long x : xs) if (x >= 0) s += x;
    return s;
}
// long[] xs = {100, "250"}; // 编译错误
```

Java 的静态检查能提前发现这段不合法的数组初始化，但不等于它能自动验证网络上来的原始文本。无论静态还是动态语言，都需要在不可信输入边界作解析与校验。

## 追到失败发生的那一行

错误输入来自支付接口：金额字段可能是 JSON 数字，也可能是字符串。我们曾假定每一项能和 `0` 比较；这条假设一旦失效，错误就在比较处出现，而不是在打印总价时才出现。

```python
def valid_total(xs):
    result = 0
    for x in xs:
        if x >= 0:
            result += x
    return result

try:
    valid_total([100, "250"])
except TypeError as exc:
    print(type(exc).__name__)
```

一种最小修复是在输入边界做显式转换：

```python
def parse_cents(raw):
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    if isinstance(raw, str) and raw.isdecimal():
        return int(raw)
    raise ValueError("invalid amount")
```

这里特别排除了 `bool`，因为 Python 中 `bool` 是 `int` 的子类，`isinstance(True, int)` 为真，而订单金额不应接受布尔值。由此可以看到：“类型合法”与“业务输入合法”并不总是同一件事。

静态类型标注可以帮助检查调用链，但不能独自校验网络收到的任意 JSON。无论使用 Java、Rust 还是 Python，**不可信外部输入**都需要边界检查。

## 对照规范和真实实现

**源码/规范阅读路线（供读者核对，并非声称已经逐行验证）：** [Python 3.13 数据模型与类型相关语言规则](https://docs.python.org/3.13/reference/)。

参考语言规范中比较运算、调用和类型标注的不同职责，并核对 `typing` 的文档：静态标注与运行时检查不是同一机制。不要声称 `list[int]` 自动检查列表每个元素。

## 实验：分别输入 "250"、"-30"、" 20 "、"

分别输入 `"250"`、`"-30"`、`" 20 "`、`"free"` 到 `int`，对比正常返回和异常；自行决定非法金额是拒绝订单还是记录错误。

**应当观察到（自行运行前属于预期）：** 前三个文本能被 `int` 转换，最后一个产生 `ValueError`。这是转换规则的结果，不是类型标注在运行时自动起作用。

## 边界：类型约束能减少某类错误

类型约束能减少某类错误，却不能替代业务有效性检查：一个合法的整数 `-99999` 仍可能不符合订单规则。过早设计过于复杂的类型层次，也会使简单算法难以理解。

**跨语言迁移时，** Rust、Go、Java、C、C++、Zig 主要使用静态类型检查；Python 主要在运行时根据对象类型决定操作是否合法。具体强制转换、溢出、泛型与空值规则各不相同。

**下一层问题：** 为什么数据需要放在一起？

[上一章：为什么要把计算过程变成函数？](04-functions.md) · [下一章：为什么数据需要放在一起？](06-composite-data.md)
