---
title: "第 39 章｜为什么 Java、Go、Python 依赖自动内存管理？"
sidebar_position: 39
description: "从订单系统的一次具体失败出发，理解为什么 Java、Go、Python 依赖自动内存管理？，以及由此带来的语法、运行行为与设计取舍。"
slug: /programming-languages/39-garbage-collection
---

# 第 39 章：为什么 Java、Go、Python 依赖自动内存管理？

订单服务大量创建临时查询结果。让每个调用方记住谁该释放何时释放，不仅繁琐，还容易重复清理。

## 创建临时字典并丢弃结果在 Python 中无需手工 free

创建临时字典并丢弃结果在 Python 中无需手工 free。程序员关注值的逻辑生命周期，解释器负责管理对象资源。

**教学实现：**

```python
def invoice(amount):
    return {"amount": amount, "tax": amount // 10}

for x in range(3):
    invoice(x * 100)
```

## 自动管理减少悬垂指针和手工释放

自动管理减少悬垂指针和手工释放，但只要程序持续保留可达引用，内存仍可能不断增长。自动回收不是自动判断对象“业务上不再有用”。

**用于触发限制的代码／输入（实验场景，不伪称来自框架）：**

```python
saved = []
for i in range(100_000):
    saved.append({"amount": i})
# 所有对象仍可从 saved 到达，自动回收不会凭“内存很多”丢弃它们
```

**因此需要的能力。** 让运行时基于引用计数、可达性等机制管理动态对象的回收，而不是让每条调用路径显式释放；同时理解“自动回收”不等于无需资源规划。

## 把新机制落实成代码

**改进后的教学实现：**

```python
def order_summary(amounts):
    return [{"amount": x} for x in amounts]

summary = order_summary([100, 250])
print(len(summary))  # 2
summary.clear()       # 解除本列表持有的对象引用
```

调用方不用知道列表内部对象怎样释放；通过删除或清空保留引用，可以让不再可达的对象成为可回收对象。CPython 常用引用计数并辅以循环 GC；其他 Python 实现细节可能不同。

## 创建两个字典并放入 summary

创建两个字典并放入 `summary`；清空后列表不再持有这两个元素引用；如果没有其他引用，运行时能按其对象管理策略处理回收。具体回收时间因实现而异。

## Go 的 GC 管理堆对象

Go 的 GC 管理堆对象，但切片仍可保留巨大底层数组的可达性。

**Go 示例（教学代码，非所引用仓库的源码节选）：**

```go
data := make([]byte, 1_000_000)
small := data[:10]
_ = small
// 即使只使用前 10 个元素，仍需理解底层数组引用
```

GC 管理可达内存，不会因为你“逻辑上只需要 10 字节”就自动替换成一个更小的数组。不同语言的 GC/对象模型差别很大。

## 不必手工 free，为何仍然会泄漏？

Python、Java、Go 的典型运行时会自动管理许多对象的内存，但“自动回收”不是“对象在你不想要时立即消失”。假设订单缓存不断加入新的记录，而全局字典始终保留引用：

```python
cache = {}
for i in range(10000):
    cache[i] = [100] * 100
assert len(cache) == 10000
```

只要这些对象仍从可达根引用得到，GC 就没有理由将它们回收。这个案例是一种**逻辑上的保留型内存泄漏**，不是垃圾回收器没工作。

CPython 常用引用计数并辅以循环垃圾回收；Java 和 Go 采用不同的追踪垃圾回收机制与暂停/并发策略。不能用“Python 有 GC，所以和 Go 一样”概括实际运行时。

另一个边界是非内存资源：数据库连接、文件句柄和事务不宜单靠对象最终被回收才清理。Python 的 `with`、Java try-with-resources 与 Go 的 `defer` 都能在语义上更明确地表达释放时机。

## 对照规范和真实实现

**源码/规范阅读路线（供读者核对，并非声称已经逐行验证）：** [CPython 3.13.5 对象容器实现 + 内存管理入口](https://github.com/python/cpython/blob/v3.13.5/Objects/listobject.c)。

阅读 CPython `Objects/listobject.c` 对元素引用的管理，并结合 `Modules/gcmodule.c` 等固定版本源码与官方 `gc` 文档理解回收边界；不要把 CPython 的引用计数推广为全部 Python 实现。

## 实验：用 tracemalloc 测量保存十万条记录前后

用 `tracemalloc` 测量保存十万条记录前后内存分配，再 `saved.clear()` 和观察快照，解释为何这不保证操作系统进程占用立刻下降。

**应当观察到（自行运行前属于预期）：** 保存大量对象一般增加跟踪到的分配；解除引用使对象更可能被回收，但分配器可能继续保留已申请的内存用于复用。

## 边界：GC 降低手工管理成本

GC 降低手工管理成本，通常需要运行时元数据与回收开销；实时延迟要求、外部文件句柄等仍需显式管理。

**跨语言迁移时，** Java/Go 主要依赖追踪 GC，CPython 以引用计数和周期性回收为重要机制；C++/Rust/Zig 的常规内存管理选择不同。

**下一层问题：** 为什么 Zig 强调显式分配器？

[上一章：为什么 Rust 需要所有权与借用？](38-rust-ownership-and-borrowing.md) · [下一章：为什么 Zig 强调显式分配器？](40-zig-explicit-allocators.md)
