---
title: "17｜为什么组合还不够：从一次 TypeError 推导接口、Protocol 与动态分派"
sidebar_position: 17
description: "同一个订单计价组件替换失败，逐层理解接口的必要性、语法、契约边界，并追踪 Go net/http 的真实接口调用链。"
slug: /programming-languages/17-interfaces-and-contracts
---

# 组合已经能替换行为，为什么还需要接口？

上一章，我们把订单的价格计算交给外部组件。一个 `Order` 可以持有普通计价函数、VIP 折扣函数或一个配置好的优惠券对象，完全不需要修改订单自身的业务逻辑。

这个设计是有用的，但团队扩展后发生了一次真实世界里很常见的事故：开发者给订单传入了一个“计价策略”，它只有 `discount` 字段，没有可以调用的计算能力。程序能启动，直到某笔订单执行结算，才报错。

这章只解决一个中心问题：**一份被替换的组件，应该如何约定它必须提供什么操作？** 只有理解这个问题，接口、抽象类、Protocol、trait 才不会变成七套要背的关键字。

## 旧方案如何在具体输入下失败？

先看上一章最精简的订单代码：

```python
class Order:
    def __init__(self, prices, pricing):
        self.prices = list(prices)
        self.pricing = pricing

    def total(self):
        return self.pricing(sum(self.prices))

def regular(amount):
    return amount

assert Order([100, 250], regular).total() == 350
```

在一个小项目里，这个设计完全合理。但调用者不清楚 `pricing` 必须是一个函数或可调用对象，于是写成：

```python
class FixedDiscount:
    def __init__(self, cents):
        self.cents = cents

broken = Order([100, 250], FixedDiscount(50))
# broken.total()
# TypeError: 'FixedDiscount' object is not callable
```

错误的核心不是 Python 不支持组合，而是**调用者和组件作者没有共享一份清晰的能力约定**。调用者认为它可以被调用，组件作者只提供了配置字段。两边都能通过各自的简单测试，一放到一起就失败。

先别急着引入 `interface`。在 Python 中，我们甚至只需要一句准确的约定：`pricing` 应该接受以分为单位的非负整数，返回以分为单位的非负整数。把输入输出明确下来，已经大幅降低沟通成本。

不过，光靠口头约定还不能帮助工具尽早发现不匹配。于是才轮到语言层面的接口表达机制。

## 用方法把组件能力说清楚

为了和 Java、Go、Rust 的方法式接口保持同一语义，我们把计价函数改成有 `calculate` 方法的对象：

```python
class RegularPricing:
    def calculate(self, subtotal: int) -> int:
        return subtotal

class VipPricing:
    def calculate(self, subtotal: int) -> int:
        return subtotal * 90 // 100
```

对应的订单：

```python
class Order:
    def __init__(self, prices: list[int], pricing):
        self.prices = list(prices)
        self.pricing = pricing

    def total(self) -> int:
        subtotal = sum(self.prices)
        return self.pricing.calculate(subtotal)

assert Order([100, 250], RegularPricing()).total() == 350
assert Order([100, 250], VipPricing()).total() == 315
```

这时方法名已经统一，但仍然可能传入 `BrokenPricing`：

```python
class BrokenPricing:
    def discount(self, subtotal: int) -> int:
        return 50

# Order([100, 250], BrokenPricing()).total()  # AttributeError
```

我们要表达的是“只要具有正确的 `calculate` 能力，就能参与订单计算”，而不是“必须继承某个具体的父类”。这个区分很重要：**能力的约束，不等于实现代码的继承关系。**

## Python：使用 Protocol 表达结构化约定

Python 的 `typing.Protocol` 可以把对组件的要求写成类型：

```python
from typing import Protocol

class Pricing(Protocol):
    def calculate(self, subtotal: int) -> int:
        ...

class Order:
    def __init__(self, prices: list[int], pricing: Pricing):
        self.prices = list(prices)
        self.pricing = pricing

    def total(self) -> int:
        return self.pricing.calculate(sum(self.prices))
```

`RegularPricing`、`VipPricing` 不必显式继承 `Pricing`，它们只要提供兼容的方法签名，就符合这种**结构化类型约定**。这和 Java 的显式 `implements` 不一样。

**重要边界：** Python 类型标注通常不会在运行时强制校验。`Protocol` 的主要价值是在静态类型检查器、IDE 和代码审查时表达共同能力。只有实际运行到 `pricing.calculate(...)`，Python 解释器才真正执行方法查找与调用；即使标注为 `Pricing`，也不能自动保证金额非负、折扣计算正确。

如果你安装了 mypy，可以用下面的代码建立一个**预期的静态检查实验**：

```python
# 文件：check_pricing.py（需要先安装 mypy；本例未将其作为默认依赖）
from typing import Protocol

class Pricing(Protocol):
    def calculate(self, x: int) -> int: ...

class Good:
    def calculate(self, x: int) -> int: return x

class Bad:
    def discount(self, x: int) -> int: return x

def checkout(p: Pricing) -> int:
    return p.calculate(350)

checkout(Good())
checkout(Bad())   # 预期被类型检查器标记为不兼容
```

在这里，检查器关心的是 `Bad` 缺少 `calculate`，而不是 `Bad` 有没有继承某个类。注意这与程序运行时抛出的 `AttributeError` 是不同阶段的反馈。

## 接口不是单纯的“函数签名清单”

接口至少包含两个层次的约定。

第一层是机器较容易检查的**形状约定**：方法名、参数类型、返回类型是否匹配。上面的 Python `Protocol` 和后面的 Java、Go 接口都主要帮助表达这一层。

第二层是业务层面的**行为约定**：输入应是非负整数分；输出也必须是非负整数分；VIP 九折不能意外变成九倍；调用是否修改原订单。通常，类型检查器不能直接证明这些业务要求。我们仍然需要测试、文档、必要时运行时检查。

例如，下面的类从方法签名看没有问题，但它是一个错误实现：

```python
class BadMathPricing:
    def calculate(self, subtotal: int) -> int:
        return -999999
```

它能满足 `Pricing` 的方法形状，却违背业务契约。这意味着：**“接口类型匹配”不是“行为正确”的充分条件**。

我们可以在订单边界加入最小运行时检查：

```python
class CheckedOrder:
    def __init__(self, prices: list[int], pricing: Pricing):
        if any(p < 0 for p in prices):
            raise ValueError("negative item price")
        self.prices = list(prices)
        self.pricing = pricing

    def total(self) -> int:
        subtotal = sum(self.prices)
        amount = self.pricing.calculate(subtotal)
        if not isinstance(amount, int) or amount < 0:
            raise ValueError("pricing returned invalid cents")
        return amount

assert CheckedOrder([100, 250], VipPricing()).total() == 315
try:
    CheckedOrder([100, 250], BadMathPricing()).total()
except ValueError:
    pass
else:
    raise AssertionError("invalid pricing must be rejected")
```

这段额外检查不会让所有计价策略都变得可信，但可以把关键业务不变量固定在订单边界。它也说明接口不能取代错误处理和测试。

## Java：当接口需要显式实现

把同一个问题交给 Java。为了避免引入太多其他概念，使用一个独立文件就能编译的示例：

```java
public class PricingDemo {
    interface Pricing {
        long calculate(long subtotal);
    }

    static class Regular implements Pricing {
        public long calculate(long subtotal) { return subtotal; }
    }

    static class Vip implements Pricing {
        public long calculate(long subtotal) {
            return subtotal * 90 / 100;
        }
    }

    static long checkout(long subtotal, Pricing strategy) {
        return strategy.calculate(subtotal);
    }

    public static void main(String[] args) {
        System.out.println(checkout(350, new Regular())); // 350
        System.out.println(checkout(350, new Vip()));     // 315
    }
}
```

Java 的 `implements Pricing` 显式声明类型关系。若 `Vip` 漏写 `calculate`，作为具体类的它通常不能通过编译。`checkout` 只依赖 `Pricing`，并不需要知道两个实现类的名字。

你现在可以重新理解“面向接口编程”：调用者声明自己需要的**最小能力**，具体实现由外部提供。它的价值在于减小调用者对具体实现的依赖，而不是必须先建立复杂的对象继承树。

## Go：没有 implements，也能形成接口

Go 的接口由方法集合隐式满足。同样的订单计价：

```go
package main

import "fmt"

type Pricing interface {
    Calculate(int64) int64
}

type Regular struct{}
func (Regular) Calculate(x int64) int64 { return x }

type Vip struct{}
func (Vip) Calculate(x int64) int64 { return x * 90 / 100 }

func Checkout(subtotal int64, p Pricing) int64 {
    return p.Calculate(subtotal)
}

func main() {
    fmt.Println(Checkout(350, Regular{})) // 350
    fmt.Println(Checkout(350, Vip{}))     // 315
}
```

此处没有任何 `implements` 关键字：`Regular` 和 `Vip` 只要方法集符合 `Pricing`，就可以传入 `Checkout`。但这也不意味着“Go 没有类型检查”；相反，编译器会在调用等具体使用点检查是否满足所需接口。

现在我们第一次能回答接口的两个不同问题：

- **对调用者来说**：它只知道 `Pricing`，就足够发起 `Calculate` 调用。
- **对实现者来说**：它提供自己的实现方法；是否需要显式声明实现关系由语言决定。

## 真实源码验证：Go 的 HTTP 服务器为什么也依赖接口？

我们已经从订单的计价组件推导出接口的需求。现在观察一套远比订单示例成熟的真实系统：Go 标准库的 HTTP 服务器。它同样需要让核心调度逻辑只依赖**一种能力**，而不是依赖所有具体业务处理器类型。

固定源码为 `golang/go` 仓库的 `go1.24.5` 标签，文件 `src/net/http/server.go`：[查看固定版本源码](https://github.com/golang/go/blob/go1.24.5/src/net/http/server.go)。

先看最小定义，**真实源码节选**（约第 83 行）：

```go
type Handler interface {
    ServeHTTP(ResponseWriter, *Request)
}
```

这里第一次出现的名称 `ResponseWriter` 和 `Request` 不需要现在展开 HTTP 协议细节。只要知道：HTTP 服务器准备好一个响应输出口和当前请求，交给某个处理器。服务器需要的是“处理 HTTP 请求”的能力，而不是“某个特定处理器类”。

下一步应该问：**谁实际调用这个接口？** 如果只展示定义，不展示调用链，就仍然是在背术语。

在同一文件的 `serverHandler.ServeHTTP`（约第 3084 行）中，源码会先取 `Server.Handler`；如果为空则选择默认复用器，在处理特定 `OPTIONS *` 请求时还有特殊路径；之后才执行调用。当前我们仅截取支持“由服务端选定组件并委托执行”的核心调用：

```go
handler.ServeHTTP(rw, req)
```

不要把这句看成魔法。`handler` 在此处是一个 `Handler` 接口值；调用者并不知道它背后究竟是哪个具体处理器。在我们的订单案例里，这就相当于 `pricing.calculate(subtotal)`：调用者只承诺需要一个能力，具体实现可以替换。

再看 `HandlerFunc`（约第 2145 行）如何将一个普通 Go 函数适配成 Handler：

```go
type HandlerFunc func(ResponseWriter, *Request)
```

实际代码还定义了 `HandlerFunc.ServeHTTP`，它在内部调用保存的函数 `f(w, r)`。这使开发者可以写普通函数，然后转换为实现了 `Handler` 的值。于是我们得到了完整的、可以跟踪的调用路径：

```text
一个请求到达 HTTP 服务器
        ↓
连接处理流程调用 serverHandler.ServeHTTP
        ↓
选择 Server.Handler 或默认复用器
        ↓
通过 Handler 接口调用 ServeHTTP
        ↓
具体实现可能是 HandlerFunc
        ↓
HandlerFunc.ServeHTTP 调用用户提供的函数
        ↓
用户函数写入 HTTP 响应
```

现在这张图的每一层都知道为什么存在：服务器不应在核心循环里硬编码所有业务处理器；接口定义共同调用约定；函数适配器让普通函数也能参与这一约定。它还说明**接口并不必然对应一个复杂的 class 层次**。

这条链是对正常路径的教学化简。真实服务端还包含网络读取、请求上下文、错误与 panic 处理等机制，我们暂时不提前讨论它们。

## 为什么不直接使用函数参数？

值得反问：Go 的 `Checkout` 能不能直接接收 `func(int64) int64`，Python 的 `Order` 能不能直接接收一个 callable？当然可以。上一章我们已经证明过。

在只有一个计算方法、没有额外状态或生命周期能力时，函数参数经常是更简洁的设计。接口的优势开始明显的条件是：

- 一个组件需要稳定地提供一组相关操作；
- 多个不同实现必须可以替换；
- 调用者希望拥有明确的依赖边界；
- 类型系统能够帮助发现一部分不匹配。

反过来，仅有两个固定的计价公式、永远不会替换、只有一个调用点，就不必因为“项目是企业级”而强行引入 `PricingInterface`、`PricingAbstractFactory` 与一整套注入容器。

接口有成本：多一个抽象需要维护；阅读时可能无法一眼看出实际实现；在某些语言和场景下间接分派可能带来运行成本。至于具体分派是编译期确定、虚表调用、接口数据结构、动态查找还是被优化成直接调用，属于后续“语言实现与运行时”的问题，不能仅凭 `interface` 一个关键字武断判断。

## 对其余语言，我们目前能确定什么？

到这里不必再贴四份几乎相同的代码，但应建立正确的对照坐标：

| 语言 | 表达这一能力约定的常见方式 | 此刻最值得记住的差别 |
| --- | --- | --- |
| Python | `Protocol`、ABC、普通鸭子类型 | 可用结构化类型提示，也可在运行时动态查找 |
| Java | `interface`、抽象类 | 一般需要显式声明 `implements` |
| C | 函数指针、操作表、约定 | 可手工构建可替换行为，但需自行维护类型和资源边界 |
| C++ | 抽象基类、虚函数、concept、模板 | 既有运行时多态，也有编译期约束路线 |
| Rust | `trait` | `dyn Trait` 与泛型 `T: Trait` 对分派方式有不同含义 |
| Zig | 编译期参数、显式函数/操作表 | 不以 Java 式 class/interface 继承体系为核心 |
| Go | `interface` | 方法集合隐式满足接口 |

比较到这一步已经足够。Rust 的 trait object 内存表示、C++ 虚表和 Go 的接口运行时布局，会在读者真正需要追踪“谁被调用”时进一步展开。

## 可复现实验和本章验收

第一项：把 Python 的 `RegularPricing` 替换成 `VipPricing`，验证结果从 350 变为 315，但 `Order.total()` 的实现没有改变。

第二项：传入 `BrokenPricing`，观察未加静态检查时的 `AttributeError`；使用 mypy 时，再观察 `Protocol` 对不兼容类型的提示。区分**运行时失败**和**静态工具提前诊断**。

第三项：把 Java 和 Go 示例分别保存成 `PricingDemo.java` 和 `main.go`，使用 `javac PricingDemo.java && java PricingDemo`、`go run main.go`。预期两种语言均输出两行数字 `350`、`315`；它们描述的是同一替换能力，但实现关系的声明方式不同。

第四项：打开 Go 固定版本的 `server.go`，沿 `Handler` → `serverHandler.ServeHTTP` → `HandlerFunc.ServeHTTP` 阅读。每走一步都回答：谁调用、输入是什么、具体调用权交给谁、下一步在哪里？不要仅因为看到 `Handler` 定义就说已经读懂 HTTP 服务器。

我们现在能够准确区分三个概念：**组合决定组件怎么合作；接口规定组件能提供什么；多态表示同一个调用位置可以运行不同实现。** 它们经常一起出现，但绝不是同义词。

下一层自然的问题是：假设程序运行时拿到一个接口值，计算机究竟怎样找到真实的实现方法？那时才值得走进方法表、运行时类型信息、虚调用与编译器的静态分派优化。
