---
title: "09｜为什么不能让任何代码随便修改订单：从字典到封装与类"
sidebar_position: 9
description: "一笔已支付订单仍能改价，如何从具体反例推导出状态不变量、封装、类，以及不用类的替代方案。"
slug: /programming-languages/09-encapsulation
---

# 为什么不能让任何代码随便修改订单？

我们已经会用函数计算订单金额。假设订单里有两项商品，单价分别是 100 分和 250 分，合计 350 分。最初，为了快速完成需求，用字典表示订单十分自然：

```python
order = {"items": [100, 250], "paid": False}
print(sum(order["items"]))  # 350
```

这里没有必要急着引入类：两种字段、一次求和，一眼就能看懂。但新需求改变了局面：**支付完成的订单不允许再修改商品项**。我们只沿这一个约束，把现有设计推进到能可靠表达它为止。

## 字典不是错误的，错误在于我们的假设

支付前，调用方添加一项商品：

```python
order["items"].append(50)
order["paid"] = True
```

此时 `sum(order["items"]) == 400`。但另一名开发者不知道支付后禁止改价，继续写出：

```python
order["items"].append(900)
print(sum(order["items"]))  # 1300
```

程序没有报错，却生成一个违反业务规则的状态。这个错误比“程序崩溃”更隐蔽：数据结构仍然合法，业务意义已经错了。

现在我们可以把规则表达得精确一些：

> 对同一笔订单，如果 `paid` 已经为 `True`，那么后续任何**被允许的业务操作**都不应该改变它的商品项。

这叫**状态不变量**：在一组规定的操作前后，始终要保持的事实。这个术语不是为了显得专业，而是为了让我们能检验设计是否满足要求。仅写“代码要规范”无法执行测试，而“支付后商品列表不变”是可检验的。

先尝试最小补丁。我们加一个专用函数：

```python

def add_item(order: dict, cents: int) -> None:
    if order["paid"]:
        raise ValueError("paid order is immutable")
    if cents < 0:
        raise ValueError("negative price")
    order["items"].append(cents)
```

它解决了**通过 `add_item` 修改**的路径：

```python
order = {"items": [100, 250], "paid": False}
add_item(order, 50)
order["paid"] = True
# add_item(order, 900)  # ValueError
```

但是，这还不是一个封闭的系统。其他代码仍然能够执行 `order["items"].append(900)`，绕开唯一的检查。我们真正缺少的，不是另一个 `if`，而是一个**让修改集中经过同一入口的边界**。

## 先不要急着使用 class：纯函数能否解决？

可以。让订单成为不可变的数据，并让每次操作返回新订单。这样旧订单仍然存在，而不是由多个调用者共同原地修改：

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class OrderData:
    items: tuple[int, ...]
    paid: bool = False

def add_item_data(order: OrderData, cents: int) -> OrderData:
    if order.paid or cents < 0:
        raise ValueError("invalid change")
    return OrderData(order.items + (cents,), order.paid)

def pay_data(order: OrderData) -> OrderData:
    return OrderData(order.items, True)

original = OrderData((100, 250))
changed = add_item_data(original, 50)
paid = pay_data(changed)
assert original.items == (100, 250)
assert paid.items == (100, 250, 50)
```

这个方案已经能让常规操作保持我们的规则：`frozen=True` 防止给字段重新赋值，元组又避免普通列表的原地 `append`。但 `frozen` 不是安全沙箱，也不是对任意嵌套可变对象的深度冻结。更重要的是，使用者仍需约定只通过我们提供的操作创建和变更对象。

这给出了第一个重要结论：**状态约束不必由面向对象实现；不可变值 + 纯函数也是一条合理路线。** 如果系统以事件和不可变快照为中心，这种方式可能更容易推理。

那么为什么还会有类？在订单需要多次在原对象上执行 `add_item`、`pay`、`cancel` 等动作时，我们希望把“状态”和“允许改变状态的操作”呈现为一个统一的业务单元。

## 类第一次真正帮上了忙

下面这段 Python 代码是**教学实现**，不是 CPython 项目的源码：

```python
class Order:
    def __init__(self, items: list[int]):
        if any(x < 0 for x in items):
            raise ValueError("negative price")
        self._items = list(items)  # 复制，避免外部列表继续影响订单
        self._paid = False

    @property
    def items(self) -> tuple[int, ...]:
        return tuple(self._items)  # 不向外返回内部可变列表

    @property
    def paid(self) -> bool:
        return self._paid

    def add_item(self, cents: int) -> None:
        if self._paid:
            raise ValueError("cannot change paid order")
        if cents < 0:
            raise ValueError("negative price")
        self._items.append(cents)

    def pay(self) -> None:
        if self._paid:
            raise ValueError("already paid")
        self._paid = True

    def total(self) -> int:
        return sum(self._items)
```

先注意 `list(items)`。如果直接写 `self._items = items`，调用方手里的原列表和订单内部会指向同一个列表。那样即使我们封装了 `add_item`，外部仍然可以通过原列表改变订单：

```python
source = [100, 250]
order = Order(source)
source.append(900)
assert order.total() == 350  # 复制输入后，这个断言才成立
```

再看 `items` 属性：我们返回元组，而不是内部列表。否则 `order.items.append(...)` 又会穿透检查边界。**封装不只是把变量名前面加下划线，更重要的是不给调用方留下可变的旁路。**

最后，`pay()` 和 `add_item()` 在同一个状态所有者中实现。它们能共同维护“已支付订单不可修改”这个规则，而不需要每个页面重复判断 `paid`。

我们亲自追踪一次执行：

| 操作 | 调用前状态 | 执行结果 | 调用后状态 |
| --- | --- | --- | --- |
| `Order([100, 250])` | 无订单 | 创建成功 | `items=(100,250), paid=False` |
| `add_item(50)` | 未支付 | 成功 | `items=(100,250,50)` |
| `pay()` | 未支付 | 成功 | `paid=True` |
| `add_item(900)` | 已支付 | 抛出 `ValueError` | `items` 保持不变 |

这张表解释了为什么我们需要方法：不是为了“面向对象”四个字，而是为了集中表达业务允许的状态转换。

## Python 的 `self`、`@property` 到底做了什么？

`order.add_item(50)` 可以在概念上理解成：找到 `Order` 类中对应的方法，并把当前实例作为首个参数传入。因此方法内部 `self._paid` 指向的是**这笔订单**的状态，而不是全局变量。Python 的方法绑定还涉及描述符，这个底层机制我们到“方法调用如何执行”一章再深入。

`@property` 让使用者可以像读取字段那样写 `order.items`，但实际上执行的是一个读取方法。在本例中，这个方法返回新的元组快照。没有定义 `items` 的 setter，就不支持普通的 `order.items = (...)` 赋值方式。

有必要说清楚边界：Python 的 `_items` 是非公开约定，不是不可绕过的安全控制。外部仍可有意访问 `order._items`。类帮助团队建立正确的 API 和局部推理边界，不是用来防御恶意调用方的沙箱。

### 真实源码：为什么 `order.items` 会执行读取方法？

现在才有必要朝执行实现看一眼。固定为 CPython `v3.13.5`，文件 [`Objects/descrobject.c`](https://github.com/python/cpython/blob/v3.13.5/Objects/descrobject.c)，函数 `property_descr_get`（约第 1564 行）。这个函数在属性访问遇到 `property` 时负责取值，其中正常读取路径的关键一行是：

**真实源码节选：**

```c
return PyObject_CallOneArg(gs->prop_get, obj);
```

`gs->prop_get` 就是之前 `@property` 注册的读取函数，`obj` 是当前订单实例。于是 `order.items` 的运行过程可以具体展开为：查到类上的 `property` → 由描述符调用 getter → getter 把内部列表转换为元组 → 把元组交还给调用者。**不需要把所有属性访问都说成是读取一段内存**；这里确实发生了函数调用。

同文件中的 `property_descr_set`（约第 1598 行）处理设置属性的路径。当没有设置函数时，它会给出没有 setter 的 `AttributeError`。因此我们的 `@property` 只读接口不是文字约定，而是在这一 CPython 版本里有明确的运行机制。注意这只能说明 property 这一路径，不意味着 Python 的其他属性都会调用同一个 getter。

## 换成 Java，边界会发生什么变化？

Java 提供语言级访问控制。下面示例保持同样的规则，但不追求把 Python 语法逐字翻译成 Java：

```java
import java.util.ArrayList;
import java.util.List;

final class Order {
    private final List<Long> items;
    private boolean paid;

    Order(List<Long> source) {
        if (source.stream().anyMatch(x -> x < 0))
            throw new IllegalArgumentException("negative price");
        this.items = new ArrayList<>(source);
    }

    public void addItem(long cents) {
        if (paid || cents < 0)
            throw new IllegalStateException("invalid change");
        items.add(cents);
    }

    public void pay() { paid = true; }
    public List<Long> items() { return List.copyOf(items); }
}
```

`private` 让普通类外代码不能直接访问 `items`；`final` 约束的是字段引用不能重新指向另一个列表，**不是**列表内容不可变。因此仍必须复制输入，并避免返回内部可变列表。`List.copyOf` 返回不可修改的列表视图式结果或不可变列表对象，其具体复用策略由实现决定；这里的语义重点是调用者不能通过返回值修改原订单。

这里也暴露出另一层差异：Java 编译器可以阻止某些越界字段访问；Python 主要依靠 API 约定与运行时检查。它们解决的是同一个设计问题，但提供的强制程度不同。

## C 没有 class，就不能封装吗？

也能。假设我们在头文件只声明一个不完整类型和操作函数：

```c
/* order.h：公开接口（教学代码） */
#include <stddef.h>
typedef struct Order Order;
Order *order_new(void);
int order_add_item(Order *order, long cents);
int order_pay(Order *order);
void order_free(Order *order);
```

`struct Order` 的具体字段只放在实现文件 `order.c` 里，外部编译单元无法仅凭这个头文件直接访问内部成员。这通常被称为 **opaque pointer（不透明指针）** 技巧。

这说明封装并不是 `class` 专属。C 的边界主要由头文件、编译单元和调用纪律组织，而不是 Java 式对象访问控制。这里展示的是接口轮廓；完整的 C 实现还需要分配失败检查、容量管理与释放逻辑，不能把这个声明片段当成独立可运行程序。

## 到底什么是类、对象和封装？

现在再给它们命名就不会显得突兀：

- **对象**是程序中具有身份或状态、可被操作的实体。这里 `order` 是某一笔实际订单。
- **类**是 Python、Java、C++ 等语言中描述一类对象如何创建、保存状态和提供操作的机制。`Order` 描述的是订单的通用规则。
- **封装**是对外公布稳定的操作边界，同时把可变的内部细节留在边界内。它的目的首先是保持约束、降低耦合，而不是绝对隐藏一切信息。

要是只有一个 20 行的脚本，原始字典可能就很好。只有当“多个地方修改同一业务状态”成为可观察的失败原因时，类的投入才真正值得。

## 动手验证：两个最容易遗漏的旁路

把完整 Python `Order` 类和下面测试代码放在同一文件运行：

```python
source = [100, 250]
o = Order(source)
source.append(999)
assert o.total() == 350             # 外部原列表不能偷偷改内部状态

snapshot = o.items
assert snapshot == (100, 250)
o.pay()
try:
    o.add_item(50)
except ValueError as e:
    assert str(e) == "cannot change paid order"
else:
    raise AssertionError("should reject modification")

assert o.items == (100, 250)
print("order invariants preserved")
```

这段测试没有声称对象永远不可能被恶意修改。它验证的是**我们公布的操作路径**和两个常见共享可变数据旁路，在预期用法下是否保持了不变量。

## 留给下一章的问题

现在，一个 `Order` 类可以守住自己的状态。但如果折扣规则变成“普通会员不打折、VIP 九折、节假日另有满减”，难道我们要在 `Order` 里持续添加 `if is_vip`、`if holiday` 和 `if coupon` 吗？

封装解决的是**谁可以改变订单状态**，并没有解决**不断变化的不同职责应该放在哪里**。这正是下一章引入组合的理由。
