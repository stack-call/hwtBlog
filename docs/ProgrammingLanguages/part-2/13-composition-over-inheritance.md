---
title: "13｜为什么一个巨大的 Order 类会失控：从条件分支推导组合"
sidebar_position: 13
description: "在同一个订单系统中，让不断新增的计价规则逼出组合，并对照 CPython logging 的真实 Handler 组合实现。"
slug: /programming-languages/13-composition-over-inheritance
---

# 为什么一个巨大的 `Order` 类会失控？

上一章我们把订单数据放进了 `Order`，让商品修改和支付都经过明确的方法。这个设计解决了支付后不能改价的问题。接下来业务部门提出了一个看似简单的要求：**普通会员按原价，VIP 会员打九折**。

对已经有 `Order.total()` 的系统来说，最直接的做法当然是加一个判断。先不要因为听过“组合优于继承”就急着创建抽象。我们先看看简单办法何时真的开始难用。

## 只有一种折扣时，if 完全合理

下面是一个独立的简化版订单，金额单位统一为“分”，避免在此章引入浮点货币误差：

```python
class Order:
    def __init__(self, prices: list[int], vip: bool = False):
        self.prices = list(prices)
        self.vip = vip

    def total(self) -> int:
        amount = sum(self.prices)
        if self.vip:
            return amount * 90 // 100
        return amount

assert Order([100, 250]).total() == 350
assert Order([100, 250], vip=True).total() == 315
```

当规则永远只有两种时，这段代码简单、易测试，也没有任何结构性错误。此时创建几十个策略类只会增加阅读成本。

真正的问题出现在下一个需求：新增固定减 50 分的优惠券，而且会员折扣和优惠券可以同时使用。业务约定是**先享受会员折扣，再减优惠券，并且最终不能为负**。

有两种独立变化：会员身份、优惠方式。若继续在订单里维护所有组合，开发者必须回答每一种组合该走哪个分支；再加地区税率、运费、活动限制，判断很容易互相影响。

```python
# 一个逐渐走向失控的方向（概念性反例）
if vip and coupon and holiday: ...
elif vip and coupon: ...
elif vip and holiday: ...
elif coupon and holiday: ...
```

问题不在 `if` 这个语法；条件判断是任何语言都需要的。真正的矛盾是：**订单生命周期变化和计价规则变化，开始集中挤在同一个模块里。** 一次促销改动，可能触碰原本稳定的支付状态代码；一次订单字段改动，也需要重新检查计价分支。

我们需要将这两个变化原因分开。

## 先尝试最小方案：把计价移出 Order

甚至不必先定义接口。先写两个普通函数：

```python
def regular(total_cents: int) -> int:
    return total_cents

def vip_10_percent_off(total_cents: int) -> int:
    return total_cents * 90 // 100
```

订单无需知道“VIP”三个字，只需要知道把原价交给哪个计算函数：

```python
class Order:
    def __init__(self, prices: list[int], pricing):
        self.prices = list(prices)
        self.pricing = pricing

    def total(self) -> int:
        return self.pricing(sum(self.prices))

assert Order([100, 250], regular).total() == 350
assert Order([100, 250], vip_10_percent_off).total() == 315
```

注意我们实际改动了什么：

1. 原先 `Order.total()` 自己判断会员身份，现在只负责计算原价，并调用已有的计算函数。
2. 计价行为以参数 `pricing` 的形式从外部传入，订单不再创建或选择具体计价方法。
3. 两个订单实例可以持有不同的函数，调用处不必写 `if order.vip`。

这就是一个十分轻量的**组合与委托**：组合表示把已有的不同能力组织在一个对象中；委托表示某项工作由其中被持有的组件完成。这里的组件甚至可以是函数。

不用立刻创造一个 `PricingFactory` 或 `AbstractPricingService`。只有当最小结构不够表达需求时才继续扩展。

## 当计价需要自身配置时，让函数升级为对象

现在优惠券有一个配置：减免多少分。有时是 50，有时是 80。我们当然可以写闭包，但如果配置项越来越多，一个专门保存规则参数的对象可能更清楚。

```python
class FixedOff:
    def __init__(self, discount_cents: int):
        if discount_cents < 0:
            raise ValueError("negative discount")
        self.discount_cents = discount_cents

    def __call__(self, amount_cents: int) -> int:
        return max(0, amount_cents - self.discount_cents)
```

因为 Python 对象可以实现 `__call__`，它能够像函数一样被 `Order.total()` 使用：

```python
assert Order([100, 250], FixedOff(50)).total() == 300
```

这一步解决的不是“对象比函数高级”，而是**计价规则现在既有行为，也有需要长期保存的配置**。如若只有单个不变规则，普通函数仍然更合适。

接下来需要同时使用会员折扣和优惠券。直觉上可以给 `Order` 加一个 `vip_and_coupon` 分支，但那会把分离出来的职责又放回订单里。更简单的办法是让多个计价动作按业务指定顺序依次执行。

```python
class PricingPipeline:
    def __init__(self, steps):
        self.steps = tuple(steps)

    def __call__(self, amount_cents: int) -> int:
        for step in self.steps:
            amount_cents = step(amount_cents)
        return amount_cents

pricing = PricingPipeline([
    vip_10_percent_off,
    FixedOff(50),
])
assert Order([100, 250], pricing).total() == 265
```

按手工执行来看：

| 阶段 | 输入（分） | 操作 | 输出（分） |
| --- | ---: | --- | ---: |
| 计算原价 | 100 + 250 | 累加 | 350 |
| 会员折扣 | 350 | `350 * 90 // 100` | 315 |
| 优惠券 | 315 | `max(0, 315-50)` | 265 |

这时候最重要的并不是多了一个 `PricingPipeline` 类，而是订单的代码**没有随着折扣的组合变化而变化**：我们把可变的计价部分组织在订单外部，再把整个计价动作交给订单使用。

这个结构也有边界：折扣的顺序会影响价格，必须明确为业务规则；`int` 的金额模型只适合我们目前约定的整数分处理；计价不是实际支付，更没有包含税务、财务对账或并发一致性。

## 为什么不用继承来造 VIPOrder？

很容易想到下面这种写法：

```python
class VipOrder(Order):
    def total(self):
        return super().total() * 90 // 100
```

但这段代码在我们当前版本里甚至不符合设计：父类 `total()` 已经根据传入的计价方法算出了结果，子类再打折可能造成**重复折扣**。为了让继承工作，我们不得不重新改造父类的行为契约。

更根本的麻烦是，VIP 身份是订单的一个**可变业务属性或计价政策**，不必然构成“订单本体类型”的派生关系。未来如果需要“VIP + 优惠券 + 节假日”，用子类表达组合可能导致大量交叉派生类型，或需要在各个子类中重写行为。

所以“组合优于继承”不该被理解为“永远禁止继承”。准确表达是：当多个职责可以独立变化、需要任意替换和组合时，优先考虑明确的组件关系；只有确实存在稳定的可替换类型关系、并能保持父类型的行为承诺时，继承才是合理选项。

## 真实源码里，组合是否也这么简单？

为了验证这不是教学玩具，我们暂时离开订单业务，观察一个非常常见的真实系统：Python 标准库的日志记录器。

我们要解决的任务很具体：同一条日志既要输出到终端，也要写入另一个目标。最笨的办法是在 `Logger` 里写 `if to_console` 和 `if to_file`；每增加一个输出目标，就修改 Logger。

CPython `v3.13.5` 的 `Lib/logging/__init__.py` 采取的是不同的结构：`Logger` 持有一组 handler；记录产生之后，遍历符合条件的 handler，让各自处理记录。这与我们让 `Order` 持有计价函数、让 `PricingPipeline` 持有若干步骤的方式十分接近。

固定源码：[CPython v3.13.5 — Lib/logging/__init__.py](https://github.com/python/cpython/blob/v3.13.5/Lib/logging/__init__.py)。可以对照 `Logger.addHandler`（约第 1563 行）和 `Logger.callHandlers`（约第 1598 行）。

**真实源码节选 A：** `Logger.addHandler` 在锁内避免重复注册，再将处理器加入列表：

```python
with _lock:
    if not (hdlr in self.handlers):
        self.handlers.append(hdlr)
```

这里的 `self.handlers` 就是组合关系在实际对象状态中的体现。`Logger` 不必变成 `FileLogger`、`ConsoleLogger` 或 `FileAndConsoleLogger` 的子类才能拥有多个输出能力。

**真实源码节选 B：** `Logger.callHandlers` 中的核心分派片段为：

```python
for hdlr in c.handlers:
    if record.levelno >= hdlr.level:
        hdlr.handle(record)
```

当前的 `c` 从这个 logger 开始，还可能沿 logger 层级向父级传播。上面的节选省略了层级遍历、`propagate` 和兜底处理，我们这里只研究**一条日志交给多个组件**的路径，不把它误解成 logging 的全部机制。

一条简化的调用链是：

```text
用户调用 logger.info("order_paid")
       ↓
Logger 创建 LogRecord 并进入 handle
       ↓
callHandlers 遍历当前 logger 的 Handler
       ↓
每个符合级别条件的 Handler.handle(record)
       ↓
具体 Handler 执行输出或其他操作
```

这是**调用链**，不是对象的继承树。`Logger` 负责组织与分派，具体 `Handler` 负责处理。源码证据支持的结论很明确：一个核心对象持有多个可协作组件，不需要将每种输出组合硬编码成新子类。

## 把真实源码缩成能运行的实验

下面使用真实 `logging` 标准库，不需要密钥或网络。两段内存缓冲区模拟两个独立输出目标：

```python
import io
import logging

left = io.StringIO()
right = io.StringIO()

logger = logging.getLogger("course.composition.demo")
logger.handlers.clear()
logger.propagate = False
logger.setLevel(logging.INFO)
logger.addHandler(logging.StreamHandler(left))
logger.addHandler(logging.StreamHandler(right))

logger.info("order_paid=42")
assert left.getvalue().strip() == "order_paid=42"
assert right.getvalue().strip() == "order_paid=42"
print("same record reached two handlers")
```

一个调用引起两个输出结果，但 `logger.info(...)` 完全没有意识到具体有两个 `StringIO`。这就是组合的运行效果。请注意：示例中 `handlers.clear()` 只为让独立演示更可重复；真实应用不建议随意清空共享 logger 的处理器配置。

## 和 Go/C 的写法有什么关系？

即使语言没有 Python 式“可调用对象”，也能达到相同的协作目的。在 Go 中，可以把一段计价行为作为函数值保存到结构体字段里：

```go
package main
import "fmt"

type PricingFunc func(int64) int64

type Order struct {
    Amount  int64
    Pricing PricingFunc
}
func (o Order) Total() int64 { return o.Pricing(o.Amount) }
func main() {
    vip := func(x int64) int64 { return x * 90 / 100 }
    fmt.Println((Order{Amount: 350, Pricing: vip}).Total()) // 315
}
```

C 也有函数指针，C++ 有可调用对象和 `std::function`，Rust 能使用函数、闭包和 trait 等方式，Zig 可以用显式函数参数或泛型组合。它们不需要先拥有完全相同的 class 语法，才能解决“一个对象委托另一个组件完成工作”的问题。

## 组合解决了什么，留下什么问题？

此刻我们已经解决：订单不直接编码所有计价分支；一个组合可以按照规则串联多个计价步骤；新增定价方式通常不需要修改 `Order.total()`。

但代码仍有一个明显漏洞。`Order` 接收的是任何 `pricing`，我们没有声明这个组件**必须能够接收整数金额并返回整数金额**。传入 `pricing=123` 时，只会在调用现场出现 `TypeError`。在 Python 的动态语义里这是合理的，但如果系统很大，我们会希望更早、更明确地知道组件是否符合要求。

下一章不急着介绍某门语言的 `interface` 关键字，而是从这个具体问题开始：**多个实现既然要彼此替换，它们究竟应该共同承诺什么？**
