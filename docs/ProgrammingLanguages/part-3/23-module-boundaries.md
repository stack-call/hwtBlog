---
title: "第 23 章｜为什么大型程序需要模块边界？"
sidebar_position: 23
description: "从订单系统的一次具体失败出发，理解为什么大型程序需要模块边界？，以及由此带来的语法、运行行为与设计取舍。"
slug: /programming-languages/23-module-boundaries
---

# 第 23 章：为什么大型程序需要模块边界？

订单、计价、配送、报表逐渐增长到几千行。一个文件里任何函数都能访问其他函数和状态，名称冲突与依赖循环越来越多。

## 单文件结构在原型阶段很合适

单文件结构在原型阶段很合适，不需要额外目录和包配置。代码短时，上下文甚至更容易搜索。

**教学实现：**

```python
# order_app.py
DISCOUNT = 90

def price(x): return x * DISCOUNT // 100

def checkout(x): return price(x)

def report(x): return checkout(x)
```

## 所有职责暴露在同一名字空间

所有职责暴露在同一名字空间，报表意外改变计价全局配置。现在需要让“谁可以依赖谁、谁负责什么状态”成为可阅读的结构约束。

**用于触发限制的代码／输入（实验场景，不伪称来自框架）：**

```python
# 报表模块只想展示数据，却通过全局变量修改了折扣规则
DISCOUNT = 50
print(checkout(200))  # 100，而非预期 180
```

**因此需要的能力。** 将变化原因不同的代码放入独立模块，并通过稳定接口交互；命名空间、包和可见性机制在这里发挥作用。

## 把新机制落实成代码

**改进后的教学实现：**

```python
# 教学结构示意：下列代码属于不同模块
# pricing.py
def vip_price(amount): return amount * 90 // 100

# checkout.py
# from pricing import vip_price
# def checkout(amount): return vip_price(amount)

# report.py
# from checkout import checkout
# def report(amount): return checkout(amount)
```

`pricing` 管定价，`checkout` 组合结算，`report` 只依赖公开的结算入口。因为用户要求代码直接嵌在文档，本示例用注释展示模块边界，而不是另附独立工程文件。真实实验可以按注释拆成三个文件。

## 当 report(200) 被调用

当 `report(200)` 被调用，控制沿 `report → checkout → vip_price` 走，最后返回 180。报表不需要直接知道折扣比例，也不应写入计价模块的内部状态。

## Rust 将代码组织为 crate 与 module

Rust 将代码组织为 crate 与 module，并控制项目内可见性。

**Rust 示例（教学代码，非所引用仓库的源码节选）：**

```rust
mod pricing {
    pub fn vip(x: i64) -> i64 { x * 90 / 100 }
}
fn checkout(x: i64) -> i64 {
    pricing::vip(x)
}
```

Rust 的 `pub` 明确可见性边界，Python 主要依赖包结构与命名约定。不同语言的“模块”粒度和编译单元也不相同。

## 一个文件能运行不代表一个模块边界合理

订单程序只有一个脚本时，函数直接互调是最短路径。但把支付、计价、存储全部塞进 `order.py`，测试计价也可能触发网络连接和数据库初始化。真正的失败不是“文件太长”，而是**引入一个职责时被迫启动无关部分**。

可先分为三个模块：`pricing.py` 只计算金额，`domain.py` 表达订单状态，`entry.py` 负责选择支付网关并装配。调用方向应该可解释：入口依赖业务规则，而业务规则不必导入入口。

```python
# pricing.py

def payable(items):
    return sum(x for x in items if x >= 0)

# entry.py（示意，假设 pricing.py 在导入路径）
from pricing import payable
print(payable([100, 250, -30]))
```

这段代码没有展示真实目录，读者需自己建两个文件验证。将 `pricing.py` 反过来导入 `entry.py`，就可能造成循环依赖或执行入口副作用。此时“模块”不仅是命名空间，也是**依赖被允许朝哪个方向流动的边界**。

Java package、Go package、Rust module/crate、C/C++ 翻译单元与头文件各有可见性、链接与构建规则。相似的业务划分并不会自动得到完全相同的部署单元。

## 对照规范和真实实现

**源码/规范阅读路线（供读者核对，并非声称已经逐行验证）：** [Rust Reference：Modules、Visibility](https://doc.rust-lang.org/reference/)。

核对 Rust `mod`、`pub` 的可见性规则以及 Python import 语义。不要先谈复杂微服务：源码模块边界与进程分布式边界不是一回事。

## 实验：在单文件版本中尝试将定价函数改名

在单文件版本中尝试将定价函数改名，然后列出需要修改的调用方；再按示意拆分，只让结算模块接触计价细节。

**应当观察到（自行运行前属于预期）：** 清晰模块边界使外部依赖更集中，但如果接口本身改变仍可能影响多个调用者。模块是依赖管理手段，不是绝对防止耦合的魔法。

## 边界：拆得太细会导致跳转成本高

拆得太细会导致跳转成本高，循环依赖也可能更加隐蔽。模块边界应该跟随真实变化原因和调用方向，而不只是每个类建一个文件。

**跨语言迁移时，** Python package、Java package/module、C 头文件与源文件、C++ module/namespace、Rust crate、Zig @import、Go package 都在组织命名与依赖，但访问控制和链接规则不同。

**下一层问题：** 为什么有些错误可以在执行前发现？

[上一章：为什么不同类型需要复用同一套算法？](22-generics-reuse-algorithms.md) · [下一章：为什么有些错误可以在执行前发现？](../part-4/24-compile-time-checks.md)
