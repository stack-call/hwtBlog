---
title: "第 47 章｜对象方法和接口调用究竟如何执行？"
sidebar_position: 47
description: "从订单系统的一次具体失败出发，理解对象方法和接口调用究竟如何执行？，以及由此带来的语法、运行行为与设计取舍。"
slug: /programming-languages/47-method-and-virtual-dispatch
---

# 第 47 章：对象方法和接口调用究竟如何执行？

`pricing.calculate(200)` 不知道 `pricing` 具体是普通策略还是 VIP 策略。我们已经知道这是一种多态，现在追问运行时如何找到实际方法。

## Python 可以动态查找对象上的方法

Python 可以动态查找对象上的方法，调用方无需写具体类型分支。但“调用结果正确”不等于我们已经知道它底层怎么找到方法。

**教学实现：**

```python
class Regular:
    def calculate(self, x): return x
class VIP:
    def calculate(self, x): return x * 90 // 100

def pay(pricing): return pricing.calculate(200)
```

## 同一条调用语句既可以找到不同函数

同一条调用语句既可以找到不同函数，也可能根本找不到。执行引擎必须把接收对象、属性名和可调用对象关联起来。

**用于触发限制的代码／输入（实验场景，不伪称来自框架）：**

```python
class Broken: pass
# pay(Broken())  # AttributeError，没有 calculate
```

**因此需要的能力。** 研究方法查找和动态分派：运行时根据实际对象类型或接口表示找到要调用的实现。某些编译器也可以在已知类型时提前确定调用目标。

## 把新机制落实成代码

**改进后的教学实现：**

```python
for strategy in [Regular(), VIP()]:
    method = strategy.calculate
    print(method(200))  # 200, 180
```

拆成“先取得方法，再调用”两步，更容易观察绑定方法是什么。CPython 的属性访问、描述符与优化路径很复杂，不能把所有动态调用简化成“一张虚函数表”。

## 第一轮方法查找定位 Regular.calculate 并与实例关

第一轮方法查找定位 `Regular.calculate` 并与实例关联，输出 200；第二轮定位 `VIP.calculate`，输出 180。调用者的代码相同，实际被调函数不同。

## C++ 的虚函数调用在典型实现中可通过虚表间接分派

C++ 的虚函数调用在典型实现中可通过虚表间接分派，但语言标准不要求特定虚表布局。

**C++ 示例（教学代码，非所引用仓库的源码节选）：**

```cpp
struct Pricing {
    virtual long calculate(long x) const = 0;
    virtual ~Pricing() = default;
};
long pay(const Pricing& p) { return p.calculate(200); }
```

这段代码只表达虚调用语义；虚表是常见 ABI 实现技术，不是 C++ 语言文本规定的唯一存储结构。

## `order.price()` 究竟指向哪个函数？

我们已经有普通与 VIP 两种计价对象。当调用方拿到接口类型而不知道具体实现时，它仍需要找到实际的 `price` 方法。

```java
interface Pricing { long price(long amount); }
class Regular implements Pricing {
    public long price(long amount) { return amount; }
}
class Vip implements Pricing {
    public long price(long amount) { return amount * 9 / 10; }
}
```

`Pricing p = new Vip(); p.price(350)` 的目标是 VIP 实现。语义上这叫动态分派，但 JVM 如何解析、链接、缓存和 JIT 优化方法调用，不能简单简化成“Java 一定通过一张固定虚表”。

C++ 的虚函数、Rust 的 `dyn Trait`、Go interface 的动态调用都可用于同类问题；而 Rust 泛型对具体类型的调用可能采用静态分派。接口约束与分派方式要分开研究。

实验：在 Java 中分别以 `Regular`、`Vip` 对象通过同一 `Pricing` 变量调用，验证结果为 350 与 315。然后把对象创建点固定为 `new Vip()`，思考 JIT 是否可能根据运行时信息优化分派。

## 对照规范和真实实现

**源码/规范阅读路线（供读者核对，并非声称已经逐行验证）：** [Python 3.13 类实例属性与方法语义](https://docs.python.org/3.13/reference/)。

核对 Python 属性访问顺序，再查 CPython v3.13.5 `Objects/typeobject.c` 的方法查找路径；对于 C++ 对照，读具体 ABI/编译输出而非凭空编造某个虚表偏移。

## 实验：打印 type(Regular().calcula

打印 `type(Regular().calculate)` 与 `type(Regular.calculate)`，对比绑定方法与类函数属性；尝试在实例上替换同名属性。

**应当观察到（自行运行前属于预期）：** 通过实例访问的普通方法通常是绑定方法，类上访问的是函数描述符；动态对象属性可能影响查找，但细节受描述符优先级影响。

## 边界：动态分派提供替换性

动态分派提供替换性，也可能带来间接调用与难以内联；静态分派便于优化但增加编译期绑定。实际性能需要测量，不能一概而论。

**跨语言迁移时，** Java invokevirtual/interface、C++ virtual、Rust dyn Trait、Go interface、Python 方法查找、C 函数指针、Zig 函数表/编译期分派都可服务多态，但机制不完全一样。

**下一层问题：** 编译器为什么能够改变程序的执行方式？

[上一章：高级语言中的函数调用如何落到机器上？](46-function-calls-to-machine.md) · [下一章：编译器为什么能够改变程序的执行方式？](48-compiler-optimizations.md)
