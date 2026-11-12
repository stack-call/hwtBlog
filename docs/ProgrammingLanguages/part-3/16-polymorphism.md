---
title: "第 16 章｜为什么同一个操作可以由不同组件实现？"
sidebar_position: 16
description: "从订单系统的一次具体失败出发，理解为什么同一个操作可以由不同组件实现？，以及由此带来的语法、运行行为与设计取舍。"
slug: /programming-languages/16-polymorphism
---

# 第 16 章：为什么同一个操作可以由不同组件实现？

订单希望统一调用 `calculate`，但会员计价和优惠券计价算法不同。调用者不应每次知道具体组件类型再写分支。

## 按类型标签分支功能明确

按类型标签分支功能明确，适合类型集合固定的简单工具。但是每增加一种实现，调用方的类型清单也要改。

**教学实现：**

```python
def checkout(amount, kind):
    if kind == "vip": return amount * 90 // 100
    if kind == "regular": return amount
    raise ValueError(kind)
```

## 新增实现本身是合法的业务扩展

新增实现本身是合法的业务扩展，但中心函数必须知道所有可能类型。中心组件依赖了具体组件的枚举名单。

**用于触发限制的代码／输入（实验场景，不伪称来自框架）：**

```python
# 假设第三方追加 "festival" 计价规则
# checkout 不认识该标签，直接 raise ValueError
```

**因此需要的能力。** 让调用位置保持相同，由传入组件决定具体执行哪一种算法。这种“同一调用约定，不同实现行为”的能力称为多态。

## 把新机制落实成代码

**改进后的教学实现：**

```python
class Vip:
    def calculate(self, amount): return amount * 90 // 100
class Festival:
    def calculate(self, amount): return max(0, amount - 30)

def checkout(amount, pricing):
    return pricing.calculate(amount)

print(checkout(200, Vip()))      # 180
print(checkout(200, Festival())) # 170
```

`checkout` 只知道操作名 `calculate`，不需要判定 `pricing` 的具体类。Python 通过运行时属性查找完成这次调用；这是动态多态，不要求两类有共同父类。

## 入口绑定到 Vip() 时调用它的 calculate 返回 18

入口绑定到 `Vip()` 时调用它的 `calculate` 返回 180；绑定到 `Festival()` 时调用另一个实现返回 170。调用语句没有变，真正被调用的函数发生了变化。

## Go 通过接口值可以将不同实现交给同一调用方

Go 通过接口值可以将不同实现交给同一调用方。

**Go 示例（教学代码，非所引用仓库的源码节选）：**

```go
type Pricing interface { Calculate(int64) int64 }
type VIP struct{}
func (VIP) Calculate(x int64) int64 { return x * 90 / 100 }
func checkout(x int64, p Pricing) int64 { return p.Calculate(x) }
```

Go 的接口由方法集隐式满足，不要求 `VIP` 书写 `implements`。接口调用可能产生间接分派，也可能在优化条件下被去虚拟化，不能凭语法绝对断言开销。

## 同一个调用表达式为什么能有两种结果

`checkout(items, calculator)` 的 `calculator` 并非固定函数。因此相同调用位置 `calculator(items)` 可能根据所传实现产生不同结果：

```python
def full(items): return sum(items)
def nine_tenths(items): return sum(items) * 9 // 10
for f in (full, nine_tenths):
    print(f([100, 200]))  # 300, 270
```

这已经是多态的一种形式：调用者使用共同的操作形状，实际行为随实现而改变。多态不是“必须有继承树”，也不等于所有调用都是运行时通过虚表完成。上面 Python 的函数值被逐次调用；C 中可以通过函数指针实现类似结构；Java 的接口调用和 C++ 的虚函数调用则有各自的分派规则。

可以做一个实验：把 `nine_tenths` 替换为需要第二个参数的 `special(items, coupon)`。调用者会立即在函数调用处失败。可替换性因此不仅要求名字相同，还要求参数、返回结果以及业务含义兼容。

另外，动态分派的运行开销不能仅靠“多态一定慢”断言。优化编译器可能内联可确定的调用，而 Python 自身的对象与解释开销又与 C++ 不同。语义层先讲**同一调用入口**，实现层再研究分派机制。

## 对照规范和真实实现

**源码/规范阅读路线（供读者核对，并非声称已经逐行验证）：** [Go Language Specification：Method sets / Interface types](https://go.dev/ref/spec)。

阅读 Go 规范中方法集与接口类型的要求，核对为何 `VIP` 可以作为 `Pricing` 的值；具体运行时表示属于后续源码章节，不应提前把接口简化为“只有一张虚表”。

## 实验：新增一个 Broken 类

新增一个 `Broken` 类，它只有 `calc` 没有 `calculate`，调用 Python `checkout`，记录错误；再在 Go 中尝试不满足方法集的类型。

**应当观察到（自行运行前属于预期）：** Python 到实际访问 `calculate` 时才失败；Go 将不满足接口的类型赋给 `Pricing` 会遭到编译期类型检查。

## 边界：多态解除调用方对具体实现的枚举依赖

多态解除调用方对具体实现的枚举依赖，但必须确保不同实现遵守相同业务承诺。例如负数是否允许、折扣顺序如何，单靠同名方法无法保证。

**跨语言迁移时，** Java 接口、C++ 虚函数与模板、Rust trait、Go interface、Python 鸭子类型、C 函数指针、Zig comptime/函数表都能表达某类多态，但检查与分派方式不同。

**下一层问题：** 为什么需要明确规定组件必须具备什么能力？

[上一章：为什么增加一种计价方式就要修改订单代码？](15-open-closed-pricing.md) · [下一章：为什么需要明确规定组件必须具备什么能力？](17-interfaces-and-contracts.md)
