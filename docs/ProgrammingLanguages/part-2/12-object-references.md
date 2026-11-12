---
title: "第 12 章｜为什么一个对象会引用另一个对象？"
sidebar_position: 12
description: "从订单系统的一次具体失败出发，理解为什么一个对象会引用另一个对象？，以及由此带来的语法、运行行为与设计取舍。"
slug: /programming-languages/12-object-references
---

# 第 12 章：为什么一个对象会引用另一个对象？

订单除了金额，还需要收货地址和定价规则。把所有字段直接放进 `Order` 似乎最简单，但地址还有自己的校验和生命周期。

## 把几个地址字段平铺到订单里可直接工作

把几个地址字段平铺到订单里可直接工作，不存在抽象上的错误。若地址永远只在订单中出现一次，结构保持扁平可能更简单。

**教学实现：**

```python
order = {
    "amount": 100,
    "city": "Nanjing",
    "street": "Road 1",
    "postal_code": "210000"
}
```

## 地址既出现在客户资料也出现在订单

地址既出现在客户资料也出现在订单。如果希望它们共同变动，就需要共享某个地址实体；如果订单必须保留历史发货地址，就需要复制快照。平铺字段让这种业务关系隐身。

**用于触发限制的代码／输入（实验场景，不伪称来自框架）：**

```python
customer = {"city": "Nanjing", "street": "Road 1"}
order = {"city": "Nanjing", "street": "Road 1"}
customer["street"] = "Road 2"
# 订单和客户地址从此可能不一致
```

**因此需要的能力。** 清楚表达“订单拥有地址信息”与“订单引用某个独立地址对象”的差别，并确定是共享还是复制。对象间的关系不是数据库外键的同义词。

## 把新机制落实成代码

**改进后的教学实现：**

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class Address:
    city: str
    street: str

@dataclass
class Order:
    amount: int
    ship_to: Address

order = Order(100, Address("Nanjing", "Road 1"))
```

地址成为独立的值对象，订单通过字段引用它。`frozen=True` 防止一般的字段重新赋值，适合此处不可变字符串字段。我们没有强制把所有对象设计成继承关系。

## 构造一个 Address 实例

构造一个 `Address` 实例，再作为 `ship_to` 传给 `Order`。访问 `order.ship_to.city` 先取得订单的地址引用，再读取其城市字段。如果要变更配送地址，需要给订单设置一个新地址值，而不是随处修改旧地址。

## Go 的嵌套结构体也可以形成“拥有一个地址值”的组合

Go 的嵌套结构体也可以形成“拥有一个地址值”的组合。

**Go 示例（教学代码，非所引用仓库的源码节选）：**

```go
type Address struct { City, Street string }
type Order struct {
    Amount int64
    ShipTo Address
}
o := Order{Amount: 100, ShipTo: Address{"Nanjing", "Road 1"}}
```

这里 Go 的 `ShipTo Address` 是结构体字段持有一个地址值，Python 的示例则是持有对象引用。语义上都是“订单由地址等部分组成”，但复制行为不同。

## 一个对象保存另一个对象，不等于把它复制进去

订单需要一个收货地址。先把地址作为普通对象保存：

```python
class Address:
    def __init__(self, city): self.city = city
class Order:
    def __init__(self, address): self.address = address

addr = Address("A")
o = Order(addr)
addr.city = "B"
assert o.address.city == "B"
```

这说明 Python 示例里的关联是共享对象引用，而不是自动制作深拷贝。仅凭类图画出 `Order → Address`，无法判断修改是否传播、对象能否独立存在、生命周期由谁负责。

在领域设计里，“组合”和“聚合”常被用来描述强弱不同的整体—部分关系，但不能直接从一个字段赋值推导出运行时所有权。C++ 按值成员、Rust 按值字段、Java 引用字段可能具有很不同的存储和生命周期语义。

如果地址只在订单创建时被快照保存，可以让 `Address` 不可变，或者构造时复制其值。选哪种要由业务来决定：用户以后修改账户地址，历史订单究竟该随之改变吗？通常历史订单需要保留当时地址，因此**领域时态**才是要求复制数据的真正原因。

## 对照规范和真实实现

**源码/规范阅读路线（供读者核对，并非声称已经逐行验证）：** [Python 3.13 属性引用与对象模型](https://docs.python.org/3.13/reference/)。

核对 Python 对象引用与属性读写的语言行为，随后在 `Objects/typeobject.c` 追踪属性查找的部分实现。不要把“对象关系”过早等同于 ORM 的关系映射。

## 实验：创建两个订单共用一个不可变 Address

创建两个订单共用一个不可变 `Address`，再给其中一个订单重新赋值 `ship_to`，观察另一个订单是否变化。

**应当观察到（自行运行前属于预期）：** 另一个订单仍引用原地址对象，不会因为第一个订单的字段被重绑而变化。共享对象、字段重新绑定和修改共享对象是不同事件。

## 边界：对象拆分能清楚表达职责

对象拆分能清楚表达职责，也会增加跳转和层次。对于始终一起变化且没有独立约束的小字段，简单记录更易维护。

**跨语言迁移时，** 组合可以以值嵌入、指针、引用、句柄等形式出现。七门语言都能表达，但地址是否共享、复制有何成本，需要读具体类型语义。

**下一层问题：** 为什么有时组合比扩展一个大类更合理？

[上一章：为什么有些数据应该只读？](11-immutability.md) · [下一章：为什么有时组合比扩展一个大类更合理？](13-composition-over-inheritance.md)
