---
title: "第 70 章｜为什么七门语言会从共同的计算需求走向不同的设计？"
sidebar_position: 70
description: "从订单系统的一次具体失败出发，理解为什么七门语言会从共同的计算需求走向不同的设计？，以及由此带来的语法、运行行为与设计取舍。"
slug: /programming-languages/70-unified-model
---

# 第 70 章：为什么七门语言会从共同的计算需求走向不同的设计？

我们从 `100 + 250` 出发，如今已经拥有订单规则、接口、内存、编译器、网络、并发与架构。最后不再添加新关键词，只把整个系统重新压缩成几条能够预测语言设计的共同问题。

## 这是全课程最初的问题

这是全课程最初的问题：读取输入、过滤非法业务项、累计状态、返回结果。它足以说明核心计算，不需要类、接口或并发。

**教学实现：**

```python
def sales_total(amounts: list[int]) -> int:
    total = 0
    for amount in amounts:
        if amount >= 0:
            total += amount
    return total

assert sales_total([100, 250, -30]) == 350
```

## 最初的函数从未“变错”

最初的函数从未“变错”，但它解决的问题边界越来越窄。每种后续机制的出现，都能追溯到一个新约束：变化、共享、执行、等待或可靠性。

**用于触发限制的代码／输入（实验场景，不伪称来自框架）：**

```python
# 数据变成外部文件、订单可能有不同折扣策略，
# 上千个网络请求需要等待，运行过程会出错或取消。
# 单一 sales_total 函数仍是正确算法，却不是完整业务系统。
```

**因此需要的能力。** 不再背七套语言独立定义，而是建立统一问题表：如何表达计算、如何组织变化、如何限制不合法状态、如何管理资源、如何执行、如何并发、如何和外部世界协作。

## 把新机制落实成代码

**改进后的教学实现：**

```python
from dataclasses import dataclass
from typing import Protocol

@dataclass(frozen=True)
class Order:
    amount: int

class Pricing(Protocol):
    def calculate(self, amount: int) -> int: ...

class Regular:
    def calculate(self, amount: int) -> int: return amount

def checkout(orders: list[Order], pricing: Pricing) -> int:
    total = sum(o.amount for o in orders if o.amount >= 0)
    return pricing.calculate(total)

assert checkout([Order(100), Order(250), Order(-30)], Regular()) == 350
```

我们只组合了订单值、计价能力与结算流程三种已讲过的概念，没有在最后突然新增 Middleware 或分布式事务。这段代码仍是教学核心，而不是可直接处理支付的生产服务。

## 输入三条订单记录

输入三条订单记录；逐条筛选后累计 350；将数值交给 `Regular.calculate` 返回 350。换计价实现只改变最后一步；把数据从文件读取属于调用者职责；并发聚合需要额外安排状态所有权。

## Rust 可用 struct、trait、泛型和切片表达相同业务抽

Rust 可用 struct、trait、泛型和切片表达相同业务抽象，但其资源与类型约束不同。

**Rust 示例（教学代码，非所引用仓库的源码节选）：**

```rust
struct Order { amount: i64 }
trait Pricing { fn calculate(&self, amount: i64) -> i64; }
fn checkout<P: Pricing>(orders: &[Order], pricing: &P) -> i64 {
    let total: i64 = orders.iter()
        .filter(|o| o.amount >= 0)
        .map(|o| o.amount).sum();
    pricing.calculate(total)
}
```

Rust 借用切片与 trait 约束在编译阶段明确；Python 通过动态对象和 Protocol 标注组织调用。相似的业务结构不意味着相同的内存成本和运行方式。

## 最终对照：用同一个问题检验七种语言

最终订单系统要同时满足五条明确约束：金额以整数分表示；已支付订单禁止修改商品；计价策略可替换；错误不能被误当成功；外部扣款重复提交要有明确业务处理。

把这些约束逐项问给每门语言，而不是比较哪门语言“更现代”。Python 可以用 dataclass/Protocol/context manager，Java 可用 record/interface/try-with-resources，C 可用结构体与函数指针配合显式资源契约，C++ 用类/模板/RAII，Rust 用 struct/trait/所有权/Result，Zig 用 struct/comptime/显式 allocator，Go 用 struct/interface/error/goroutine。

对同一份数据写三个实验：①纯计算的输入与输出；②付款状态变化后试图修改商品项；③一个支付调用超时后重复提交。先比较它们在**业务语义**上的一致性，再记录各自需要多少编译期约束、运行时支持和代码约定。

最后才测性能、部署成本和生态适配。语言机制没有一个在所有维度都获胜；真正的可迁移能力是明确当前任务不可放弃的性质，再选择能用可接受复杂度实现这些性质的工具。

如果学完仍只能复述 `trait` 与 `interface` 的语法，这套教程没有成功；如果能够解释何时函数足够、何时需要接口、何时不得不考虑内存/并发/外部副作用，以及源代码如何让这些承诺落地，才算完成。

## 对照规范和真实实现

**源码/规范阅读路线（供读者核对，并非声称已经逐行验证）：** [CPython v3.13.5、Go go1.24.5 等固定源码入口总对照](https://github.com/python/cpython/blob/v3.13.5/Python/bytecodes.c)。

本章源码复盘从一个实际函数出发：CPython `Python/bytecodes.c` 核对字节码行为、`Objects/listobject.c` 核对容器引用、Go `src/runtime/slice.go` 核对切片扩容、`src/runtime/chan.go` 核对消息同步。其余语言先用正式规范建立语义，再选择固定版本的实现源码，避免拼凑不同时期的内部细节。

## 实验：同样实现输入 [100,250,-30] 的订单结

同样实现输入 `[100,250,-30]` 的订单结算，至少使用 Python、Go、Rust 中两种语言。逐项记录：金额类型、容器类型、计价接口/函数、错误表示、执行产物和是否需要手工释放。

**应当观察到（自行运行前属于预期）：** 核心业务结果都应是 350，但 API、类型、所有权、执行路径各不相同。用“相同输入输出”比较语义，用固定工具链源码/实验比较实现；不要直接给语言性能排名。

## 边界：七门语言并不是一条由低级到高级的单向进化线

七门语言并不是一条由低级到高级的单向进化线。Python 强调表达力，Java 强调托管运行时与成熟生态，C 强调低层可控，C++ 拥有丰富的泛型与资源抽象，Rust 强调编译期内存安全，Zig 强调显式控制，Go 强调简洁并发与工程体验；它们在各自边界内选择不同的成本结构。

**跨语言迁移时，** 面对任何新的语言特性，可以按六问分析：它解决什么具体失败？以前的最小方案为何不够？它约束什么语义？实际运行时如何实现？额外成本是什么？在另六门语言里有哪些替代方式？这套问法才是课程最终可迁移的能力。

**课程闭环**：最初只有数据与计算，随后逐步出现状态和控制、封装与组件、类型与资源、执行与环境、并发与架构。现在每个模块为什么存在，读者都可以沿订单系统中的具体失败往回追溯。

**下一层问题：** 回到课程引言，检验整套统一模型。

[上一章：为什么更高层的抽象不一定更慢或更好？](../part-9/69-abstraction-cost.md) · [返回引言](../00-introduction.md)
