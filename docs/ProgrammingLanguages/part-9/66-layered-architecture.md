---
title: "第 66 章｜为什么业务逻辑不应该直接绑定数据库或网络？"
sidebar_position: 66
description: "从订单系统的一次具体失败出发，理解为什么业务逻辑不应该直接绑定数据库或网络？，以及由此带来的语法、运行行为与设计取舍。"
slug: /programming-languages/66-layered-architecture
---

# 第 66 章：为什么业务逻辑不应该直接绑定数据库或网络？

订单总额函数原本直接查询数据库。现在要单元测试退款规则，却必须启动数据库，测试又慢又不稳定。

## 直接查询数据库便于快速完成端到端功能

直接查询数据库便于快速完成端到端功能，如果系统规模小且数据库稳定，并不一定是严重问题。

**教学实现：**

```python
def checkout(order_id):
    row = database.fetch(order_id)  # 假设全局 database
    return sum(row["amounts"])
```

## 计算规则本质上不依赖数据库协议

计算规则本质上不依赖数据库协议，但函数签名强制它依赖外部系统。现在“查询数据”和“处理数据”的变化混在一起。

**用于触发限制的代码／输入（实验场景，不伪称来自框架）：**

```python
# 在离线 CI 或单元测试中没有真实 database 连接
# 测试只关心 [100,250,-30] 的业务计算，却无法单独调用
```

**因此需要的能力。** 将业务核心定义为输入明确的纯计算，把数据获取与持久化移到边界；必要时用端口接口连接外部适配器。

## 把新机制落实成代码

**改进后的教学实现：**

```python
def sales_total(amounts: list[int]) -> int:
    return sum(x for x in amounts if x >= 0)

class OrderReader:
    def fetch_amounts(self, order_id: str):
        raise NotImplementedError

def checkout(order_id: str, reader: OrderReader) -> int:
    return sales_total(reader.fetch_amounts(order_id))
```

现在可以直接测试 `sales_total`，也可以注入内存 Reader 测试查询到计算的连接。Reader 是边界契约，数据库实现细节不进入总额算法。

## 调用方传入 OrderReader 实现

调用方传入 OrderReader 实现；`checkout` 请求金额列表；结果传给 `sales_total`；函数过滤负数返回合计。单元测试可跳过数据库，集成测试再验证实际适配器。

## Rust 的 trait 与泛型依赖也适合端口/适配器设计

Rust 的 trait 与泛型依赖也适合端口/适配器设计。

**Rust 示例（教学代码，非所引用仓库的源码节选）：**

```rust
trait OrderReader {
    fn amounts(&self, id: &str) -> Vec<i64>;
}
fn checkout<R: OrderReader>(id: &str, reader: &R) -> i64 {
    reader.amounts(id).into_iter().filter(|x| *x >= 0).sum()
}
```

示例省略了数据库错误传播，真实接口往往应返回 Result。架构原则与 Rust trait 的语义机制不是同一层。

## 为什么不能让价格计算直接连数据库

若 `calculate_price()` 每次都从数据库查询会员折扣，单元测试一个纯数学规则也必须准备数据库。问题不是“数据库不好”，而是业务计算与 IO 绑定，使可预测的逻辑受外部状态影响。

一种渐进式改动是先把折扣值作为参数传进函数：

```python
def calculate_price(items, discount_percent):
    return sum(items) * (100 - discount_percent) // 100
```

再由用例层负责从会员服务取出折扣，调用该函数。这样价格计算没有网络/数据库依赖；如果业务规则后来需要查询复杂政策，再抽出一个能表达所需能力的端口，由基础设施适配器实现。

“分层”不是按 `controller/service/repository` 文件夹机械摆放。正确判断方法是：修改数据库 SDK，会不会迫使纯价格算法跟着改？单测价格规则是否需要真实服务？若答案是是，就有可观察的耦合问题。

端口与适配器的边界也不自动提供事务一致性；跨多个外部资源的提交仍是下一层问题。

## 对照规范和真实实现

**源码/规范阅读路线（供读者核对，并非声称已经逐行验证）：** [Rust trait 约束和方法调用规范](https://doc.rust-lang.org/reference/)。

先核对 trait 方法与泛型约束，再用真实依赖图记录 `checkout` 只知道 OrderReader 的能力。不要未经必要就将单进程模块分拆成微服务。

## 实验：写一个 FakeReader 返回 [100,25

写一个 `FakeReader` 返回 `[100,250,-30]`，测试 `checkout("A",fake)` 返回 350，再写一个 Reader 抛异常验证传播。

**应当观察到（自行运行前属于预期）：** 核心规则无需数据库即可测试；外部错误仍需要上层明确处理，分层不自动修复数据库故障。

## 边界：端口与适配器增加抽象和装配代码

端口与适配器增加抽象和装配代码，对极简单脚本可能过重。是否值得使用取决于外部依赖替换与测试隔离的实际需求。

**跨语言迁移时，** Python Protocol、Java interface、Rust trait、Go interface、C/C++ 函数/抽象类、Zig 函数表/comptime 等都可表达业务端口，数据访问层是应用设计决定的。

**下一层问题：** 为什么大型代码库需要独立构建和版本管理？

[上一章：为什么某些设计结构会反复出现？](65-design-patterns.md) · [下一章：为什么大型代码库需要独立构建和版本管理？](67-modules-and-versioning.md)
