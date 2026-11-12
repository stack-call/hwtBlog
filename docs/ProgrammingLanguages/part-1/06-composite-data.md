---
title: "第 06 章｜为什么数据需要放在一起？"
sidebar_position: 6
description: "从订单系统的一次具体失败出发，理解为什么数据需要放在一起？，以及由此带来的语法、运行行为与设计取舍。"
slug: /programming-languages/06-composite-data
---

# 第 06 章：为什么数据需要放在一起？

订单原先只有金额。现在需要订单号、金额和是否支付三个字段，并且一次处理很多笔订单。继续用三个平行数组会让数据错位。

## 平行数组在所有列表长度相等、顺序一致时有效

平行数组在所有列表长度相等、顺序一致时有效。通过相同下标可以拼出一笔订单，最简单的脚本甚至不需要新的数据类型。

**教学实现：**

```python
ids = ["A", "B"]
amounts = [100, 250]
paid = [True, False]
print(ids[1], amounts[1], paid[1])
```

## 一次更新漏改一个列表

一次更新漏改一个列表，索引关系就崩溃了。问题不是“下标不够方便”，而是“属于同一个实体的字段没有一起维护”，无法保证数据结构的一致性。

**用于触发限制的代码／输入（实验场景，不伪称来自框架）：**

```python
ids = ["A", "B"]
amounts = [100, 250]
paid = [True]  # B 的状态缺失
print(ids[1], amounts[1], paid[1])  # IndexError
```

**因此需要的能力。** 将一笔订单的相关字段组织成一个整体，再把多笔订单组织成集合。这里要区分“记录中的字段”和“记录的序列”两个层次。

## 把新机制落实成代码

**改进后的教学实现：**

```python
from dataclasses import dataclass

@dataclass
class Order:
    order_id: str
    amount: int
    paid: bool

orders = [Order("A", 100, True), Order("B", 250, False)]
print(orders[1].amount)  # 250
```

`Order` 把三个字段绑定为一条记录；列表管理多条记录的顺序和长度。`@dataclass` 生成常见样板方法，但并没有自动验证金额一定大于零，也没有保护对象不被任意修改。

## 现在 orders[1] 先获得第二条记录

现在 `orders[1]` 先获得第二条记录，再通过 `.amount` 取出 250。新增订单只需要追加一个完整 `Order` 实例，而不是分别在三个平行数组里维护一致的下标。

## Go 的 struct 同样用于把字段组成一个值

Go 的 `struct` 同样用于把字段组成一个值。

**Go 示例（教学代码，非所引用仓库的源码节选）：**

```go
type Order struct {
    ID string
    Amount int64
    Paid bool
}
orders := []Order{{"A", 100, true}, {"B", 250, false}}
fmt.Println(orders[1].Amount)
```

在 Go 中 `struct` 是数据形状；它不等于 Java 风格的类继承。Python dataclass 实例通常具有动态对象开销，Go 结构体切片的布局也不同，但两者都表达相同的“记录集合”意图。

## 为什么不同的数据结构导致不同的操作代价

现在要记录每笔订单，不再只有合计。列表容许重复、保留顺序：

```python
orders = [(101, 100), (102, 250), (103, -30)]
assert orders[1] == (102, 250)
```

但客服希望根据订单号快速找到金额。每次从头搜索仍然正确，只是订单达到百万级时会反复扫描：

```python
def find_order(rows, oid):
    for row_id, cents in rows:
        if row_id == oid:
            return cents
    return None
```

如果主要操作变成“按唯一订单号查找”，就可以建立映射：

```python
by_id = {oid: cents for oid, cents in orders}
assert by_id[102] == 250
```

列表与字典不是“高级和低级”的关系，而是对不同操作进行不同权衡。字典还引入键唯一的假设：两个相同订单号会发生覆盖。选择结构之前，要写清楚数据是否有序、是否允许重复、读取与写入谁更频繁。

C 的数组提供连续存储和下标访问；Go 的 map、Rust 的 HashMap、Java 的 HashMap 在使用场景上相似，但迭代顺序、哈希策略和并发安全不应假定相同。

## 对照规范和真实实现

**源码/规范阅读路线（供读者核对，并非声称已经逐行验证）：** [CPython 3.13.5 `Objects/listobject.c`](https://github.com/python/cpython/blob/v3.13.5/Objects/listobject.c)。

在固定版本的 `listobject.c` 中观察 Python 列表如何管理元素引用、扩容与访问；这条实现线能解释为什么列表提供可变长度，但不能从中推出 `Order` 字段的全部布局。

## 实验：分别追加 Order("C", 0, False)

分别追加 `Order("C", 0, False)` 与 `Order("D", -20, True)`，再问自己：这两个对象在语法层面是否合法，在业务层面是否合法？

**应当观察到（自行运行前属于预期）：** 两条记录都能被 dataclass 构造出来。数据组织解决字段组合，业务规则必须额外定义；这正是下一章开始遇到的约束问题。

## 边界：把字段捆在一起避免下标错位

把字段捆在一起避免下标错位，但并不能阻止别人随意设置 `order.amount = -20`。记录是数据结构，不天然等于封装良好的领域对象。

**跨语言迁移时，** C 的 `struct`、C++ 的 `struct/class`、Rust 的 `struct`、Zig 的 `struct`、Go 的 `struct`、Java 的 `record/class`、Python 的 dataclass 可以承担相近建模角色，但复制、相等和内存布局不相同。

**下一层问题：** 为什么有些计算会改变外部世界？

[上一章：为什么相同的代码面对不同数据会失败？](05-types-matter.md) · [下一章：为什么有些计算会改变外部世界？](07-side-effects.md)
