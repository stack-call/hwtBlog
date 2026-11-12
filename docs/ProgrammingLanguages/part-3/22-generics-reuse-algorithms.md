---
title: "第 22 章｜为什么不同类型需要复用同一套算法？"
sidebar_position: 22
description: "从订单系统的一次具体失败出发，理解为什么不同类型需要复用同一套算法？，以及由此带来的语法、运行行为与设计取舍。"
slug: /programming-languages/22-generics-reuse-algorithms
---

# 第 22 章：为什么不同类型需要复用同一套算法？

除了整数订单号，系统还要排序字符串订单号。我们写了两份查找逻辑，除了值类型几乎一样。

## 每种类型各写一份的实现完全正确

每种类型各写一份的实现完全正确，只要种类固定且少，重复的成本也可能可以接受。

**教学实现：**

```python
def first_int(xs: list[int]) -> int | None:
    return xs[0] if xs else None

def first_str(xs: list[str]) -> str | None:
    return xs[0] if xs else None
```

## 算法本身不依赖元素是整数还是字符串

算法本身不依赖元素是整数还是字符串，只关心它是一个序列并且可以取第一个元素。重复是因为我们把“不相关的类型差异”写死进函数名和签名。

**用于触发限制的代码／输入（实验场景，不伪称来自框架）：**

```python
# 新增 Order、Address、Coupon 类型后，还要复制 first_xxx
# 更改空序列处理规则，必须改所有副本
```

**因此需要的能力。** 把算法真正关心的结构与暂时不关心的元素类型分开。这是参数化多态的动机，也就是泛型的一种常见形式。

## 把新机制落实成代码

**改进后的教学实现：**

```python
from typing import TypeVar, Sequence
T = TypeVar("T")

def first(xs: Sequence[T]) -> T | None:
    return xs[0] if xs else None

print(first([100, 250]))
print(first(["A", "B"]))
```

`T` 代表一次调用中的元素类型，`Sequence[T]` 表明所需的是支持索引的序列，而非某个具体列表类型。Python 中这些标注帮助静态分析，但运行时不会自动为每个 T 生成不同机器代码。

## first([100,250]) 返回整数 100

`first([100,250])` 返回整数 100；`first(["A","B"])` 返回字符串 `"A"`。函数体相同，输入与输出的具体元素类型对应变化。

## C++ 模板在实例化时形成针对具体类型的代码

C++ 模板在实例化时形成针对具体类型的代码。

**C++ 示例（教学代码，非所引用仓库的源码节选）：**

```cpp
template<class T>
const T* first(const std::vector<T>& xs) {
    return xs.empty() ? nullptr : &xs[0];
}
```

C++ 这里返回指针避免复制，Python 返回对象引用语义；两段算法目标相近，但类型编译与对象生命周期完全不同。模板实例化也可能增加代码体积。

## 为什么复制四份排序函数不是好主意

订单号是整数，订单状态是字符串；我们都希望在列表里寻找指定元素。最初写两份函数很容易：

```python
def has_int(xs, x): return x in xs
def has_str(xs, x): return x in xs
```

重复不在于内容短，而在于操作规则与元素具体类型无关。我们需要表达“对任意支持相等比较的元素序列，执行查找”。在 Python 中，一个泛型函数可以描述这种关联：

```python
from typing import TypeVar
T = TypeVar("T")
def contains(xs: list[T], item: T) -> bool:
    return item in xs
```

这里两个 `T` 不是两个任意独立类型，而是**同一类型参数在参数之间建立联系**。不过 Python 类型提示默认不保证运行时阻止非法调用；在 Java 的泛型、Rust 的 trait bounds、C++ 的 templates/concepts、Go 的类型参数中，规则和编译策略又不同。

一个经常被忽视的问题是：泛型函数内部究竟允许对 `T` 做什么？如果算法需要 `<` 排序，仅声明“任意 T”就不够，必须要求可比较。这才引出约束、trait bound 或 concept，而不是为了炫耀语法先介绍它们。

实验：把代码里的 `item in xs` 改成 `item * 2`，尝试对字符串、整数、业务对象调用，分清类型检查与运行时表现。

## 对照规范和真实实现

**源码/规范阅读路线（供读者核对，并非声称已经逐行验证）：** [C++ templates 与 Python typing 的官方参考](https://en.cppreference.com/w/)。

在 C++ 模板参考中核对实例化与类型约束，在 Python `typing` 文档中核对 `TypeVar` 的用途。不要把“有泛型”统一解释为运行时反射或编译期复制代码。

## 实验：尝试 first([])、first([1])、f

尝试 `first([])`、`first([1])`、`first([Order(...)])`；思考为什么返回值类型需要允许 `None`，并在静态检查器里观察结果类型。

**应当观察到（自行运行前属于预期）：** 空序列返回 `None`；非空返回第一个元素。泛型保留输入元素类型信息，但容器为空这一运行时事实仍需单独建模。

## 边界：泛型避免重复算法

泛型避免重复算法，也可能带来复杂报错、模板膨胀或 API 约束难度。只有算法真正独立于具体类型时，才值得抽象成泛型。

**跨语言迁移时，** Java generics、C++ templates、Rust generics、Go type parameters、Zig comptime、Python type parameters、C 宏/void 指针方案分别提供不同能力，必须比较类型检查与运行时表示。

**下一层问题：** 为什么大型程序需要模块边界？

[上一章：为什么继承容易形成脆弱的类型体系？](21-fragile-inheritance.md) · [下一章：为什么大型程序需要模块边界？](23-module-boundaries.md)
