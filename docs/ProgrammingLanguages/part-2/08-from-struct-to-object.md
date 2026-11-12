---
title: "第 08 章｜为什么一组字段还不足以表达一笔订单？"
sidebar_position: 8
description: "从订单系统的一次具体失败出发，理解为什么一组字段还不足以表达一笔订单？，以及由此带来的语法、运行行为与设计取舍。"
slug: /programming-languages/08-from-struct-to-object
---

# 第 08 章：为什么一组字段还不足以表达一笔订单？

我们已经把订单的编号、金额和付款状态放在同一个记录里。但财务报表给一条订单填了负金额，虽然字段完整，业务却不合理。

## 字典让订单字段聚合

字典让订单字段聚合，避免平行数组错位。只需要临时记录时，它很省事，也容易序列化。

**教学实现：**

```python
order = {"id": "A", "amount": 100, "paid": False}
print(order["amount"])
```

## 字典把字段放在一起

字典把字段放在一起，但无法说明允许哪些转换。`paid=True` 的订单还能随意改金额，报表与支付过程就可能互相矛盾。问题从“字段如何组合”变为“什么状态才算合法”。

**用于触发限制的代码／输入（实验场景，不伪称来自框架）：**

```python
order = {"id": "A", "amount": 100, "paid": False}
order["amount"] = -900
order["paid"] = True
print(order)  # 数据形状合法，业务状态可疑
```

**因此需要的能力。** 明确订单必须一直满足的约束，即不变量。例如金额不能是负数，已付款订单不能再变动金额。先用一个局部验证函数，而不是立即发明层层框架。

## 把新机制落实成代码

**改进后的教学实现：**

```python
def validate(order: dict) -> None:
    if order["amount"] < 0:
        raise ValueError("negative amount")

def change_amount(order: dict, amount: int) -> None:
    if order["paid"]:
        raise ValueError("already paid")
    if amount < 0:
        raise ValueError("negative amount")
    order["amount"] = amount
```

`validate` 描述当前数据是否被业务允许，`change_amount` 描述合法状态转换。记录字段、值类型和业务约束是三个不同层次；把它们混在一起很容易误以为结构类型已经保证了正确业务。

## 订单初始 amount=100, paid=False

订单初始 `amount=100, paid=False`。调用 `change_amount(order,120)` 成功；将 `paid=True` 后再调用 `change_amount(order,130)`，函数在赋值之前抛异常，订单金额继续保持 120。

## C 也可以用结构体加一个受控修改函数表达同一规则

C 也可以用结构体加一个受控修改函数表达同一规则。

**C 示例（教学代码，非所引用仓库的源码节选）：**

```c
typedef struct { long amount; int paid; } Order;
int change_amount(Order *o, long next) {
    if (o->paid || next < 0) return 0;
    o->amount = next;
    return 1;
}
```

C 的公开结构体字段仍然可能被调用方直接修改；遵守 API 约定靠项目边界和代码审查。某些语言的可见性机制能进一步缩小这种绕过空间。

## 数据结构与业务身份不是同一层

订单号为 101 的记录可以使用字典、元组或结构体表示，它们都保存字段，却未必表达“同一笔订单”的业务含义。考虑两个值：

```python
a = {"id": 101, "cents": 100}
b = {"id": 101, "cents": 100}
assert a == b
assert a is not b
```

`==` 在字典上比较内容；`is` 检查对象身份。这两个字典内容一样，却是不同对象。而业务上它们也可能代表“同一订单的两份快照”，不能仅靠内存对象身份判断。

为订单建模前要确定字段间的约束：`id` 是否全局唯一？金额能否为负？支付状态是否允许逆转？如果只使用一个匿名元组 `(101, 100, True)`，调用者很容易写错位置；具名字段更便于阅读：

```python
from dataclasses import dataclass
@dataclass(frozen=True)
class OrderSnapshot:
    order_id: int
    cents: int
    paid: bool
```

这一阶段 `dataclass` 主要是生成清晰的数据表示，不要求我们马上把所有行为塞进类。下一章才会因为“支付后不允许改价”而需要集中修改入口。

## 对照规范和真实实现

**源码/规范阅读路线（供读者核对，并非声称已经逐行验证）：** [Python 3.13 数据模型与映射类型规范](https://docs.python.org/3.13/reference/)。

核对字典读写语义，并在 CPython `Objects/dictobject.c`（v3.13.5）观察映射存储如何实现。字典的存储机制无法替业务验证“金额非负”；不要将语言提供的数据容器等同于领域约束。

## 实验：自己尝试四条状态

自己尝试四条状态：未付款正常改价、已付款改价、负金额、重复设置原价。预测是否允许，运行 `change_amount` 记录实际状态。

**应当观察到（自行运行前属于预期）：** 未付款正金额允许；已付款和负金额拒绝；重复设相同金额在当前实现中仍允许。是否禁止重复操作属于业务设计，不是语言内建。

## 边界：验证函数在单一入口下有效

验证函数在单一入口下有效，却无法阻止其他代码绕过它直接写字典。我们下一步需要的是访问边界，而不是更多散落的条件判断。

**跨语言迁移时，** Python dict、Go struct、C struct、Java record 都可以表示字段，但封装约束不因此相同。先问“允许哪些状态转换”，再问应该使用哪种语法。

**下一层问题：** 为什么不能允许任意代码修改订单？

[上一章：为什么有些计算会改变外部世界？](../part-1/07-side-effects.md) · [下一章：为什么不能允许任意代码修改订单？](09-encapsulation.md)
