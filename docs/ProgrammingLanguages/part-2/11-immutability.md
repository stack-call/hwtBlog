---
title: "第 11 章｜为什么有些数据应该只读？"
sidebar_position: 11
description: "从订单系统的一次具体失败出发，理解为什么有些数据应该只读？，以及由此带来的语法、运行行为与设计取舍。"
slug: /programming-languages/11-immutability
---

# 第 11 章：为什么有些数据应该只读？

订单已经支付。一个函数只是想查看金额，却意外修改了它。即使有类，所有引用都拥有写权限，约束仍不够明确。

## 返回列表使调用者能轻松读取明细

返回列表使调用者能轻松读取明细，调用方便。但返回的是同一个可变列表对象的引用，未必符合“只是查看”的意图。

**教学实现：**

```python
order = {"items": [100, 250]}
def view(order):
    return order["items"]
items = view(order)
```

## 查询函数没有显式调用“修改订单”

查询函数没有显式调用“修改订单”，外部却通过别名改变了内部明细。原有接口没有区分读操作与写操作。这不是变量名的问题，而是可变共享数据暴露。

**用于触发限制的代码／输入（实验场景，不伪称来自框架）：**

```python
items.append(999)
print(order["items"])  # [100, 250, 999]
```

**因此需要的能力。** 只读视图或不可变快照，让读者拿到数据但不能通过这个返回值修改原始列表。先解决眼前的泄漏，不急着引入持久化数据结构。

## 把新机制落实成代码

**改进后的教学实现：**

```python
class Order:
    def __init__(self, items):
        self._items = list(items)
    def items(self):
        return tuple(self._items)

order = Order([100, 250])
view = order.items()
print(view)  # (100, 250)
```

构造时先复制输入，避免调用者持有原始列表继续修改；读取时返回元组，阻止通过这个结果追加元素。这里的元素是整数，不涉及嵌套可变对象；若元素本身可变，浅复制并不等于深度不可变。

## 输入列表先被复制成内部 _items

输入列表先被复制成内部 `_items`；`items()` 将当前元素复制为元组；外部对最初列表执行 `append` 不影响订单，返回的元组也没有 `append`。一次拷贝换来了更清晰的修改边界。

## C++ 使用 const 引用表达当前接口不允许通过该引用修改数据

C++ 使用 `const` 引用表达当前接口不允许通过该引用修改数据。

**C++ 示例（教学代码，非所引用仓库的源码节选）：**

```cpp
struct Order { std::vector<long long> items; };
long long total(const Order& order) {
    long long sum = 0;
    for (auto x : order.items) sum += x;
    return sum;
}
```

`const Order&` 并不复制对象，约束通过该引用访问的可变性；这与 Python 创建元组快照的成本和语义都不同。不能将“只读”简单等同于“不可变对象”。

## 不可变到底禁止了什么

订单付款以后，我们想保留一个不会被修改的快照。给变量加“不能重新赋值”的约束，并不一定阻止被引用对象内部变化：

```python
from dataclasses import dataclass
@dataclass(frozen=True)
class Receipt:
    items: list[int]

r = Receipt([100, 250])
r.items.append(999)  # 仍然允许修改列表
assert r.items == [100, 250, 999]
```

`frozen=True` 禁止常规属性重赋值，却不会递归冻结嵌套列表。更接近我们要求的是：

```python
@dataclass(frozen=True)
class ReceiptSafe:
    items: tuple[int, ...]

r = ReceiptSafe((100, 250))
assert sum(r.items) == 350
```

这样把可变列表替换成不可变元组，不允许常规 `append`。但业务身份、数据库记录和外部缓存仍可能变化；“不可变对象”只是一个特定层级的保证。

C++ 的 `const`、Rust 的 `&T`、Java 的 `final`、Go 的值复制与 Zig 的 `const` 各自对可变性的限制不同，不应把它们翻译成一个完全相同的“只读”概念。

## 对照规范和真实实现

**源码/规范阅读路线（供读者核对，并非声称已经逐行验证）：** [CPython 3.13.5 列表实现](https://github.com/python/cpython/blob/v3.13.5/Objects/listobject.c)。

在 `listobject.c` 中研究列表如何持有元素与支持变长操作；再对照 `Objects/tupleobject.c` 的元组实现。重点是“暴露内部可变列表”和“返回新容器”的不同可观察效果。

## 实验：将输入改为 [[100],[250]]

将输入改为 `[[100],[250]]`，返回 `tuple(self._items)` 后尝试 `view[0].append(999)`，观察结果。

**应当观察到（自行运行前属于预期）：** 元组不能增删顶层元素，但嵌套列表仍可变，内部数据会受到影响；浅层不可变不能自动保证深度不可变。

## 边界：复制数据有时间和空间成本

复制数据有时间和空间成本；返回只读视图又可能仍共享底层存储。需要根据数据规模与并发访问要求选择，而不是无条件复制。

**跨语言迁移时，** Rust 的共享借用 `&T`、C++ 的 `const`、Java 的不可变对象约定、Go 的值复制或不公开字段、Python 的不可变容器各有不同保障范围。

**下一层问题：** 为什么一个对象会引用另一个对象？

[上一章：为什么要把数据与操作组织在一起？](10-class-state-and-behavior.md) · [下一章：为什么一个对象会引用另一个对象？](12-object-references.md)
