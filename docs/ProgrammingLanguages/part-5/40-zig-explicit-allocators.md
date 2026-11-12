---
title: "第 40 章｜为什么 Zig 强调显式分配器？"
sidebar_position: 40
description: "从订单系统的一次具体失败出发，理解为什么 Zig 强调显式分配器？，以及由此带来的语法、运行行为与设计取舍。"
slug: /programming-languages/40-zig-explicit-allocators
---

# 第 40 章：为什么 Zig 强调显式分配器？

订单批处理已经知道每批数据处理完就可以整体释放。每个对象单独申请并回收，可能比业务工作还复杂。我们希望根据使用场景选择分配策略。

## 使用通用 allocator 已经能够满足正确的动态存储需求

使用通用 allocator 已经能够满足正确的动态存储需求，优点是简单可控。但大量临时对象的生命周期相同，逐一追踪它们的释放可能很繁琐。

**教学实现：**

```zig
const std = @import("std");
// 需要动态保存订单数量时，先从通用分配器申请空间
fn make(a: std.mem.Allocator, n: usize) ![]i64 {
    return try a.alloc(i64, n);
}
```

## 资源的实际生命周期是“整批处理结束”

资源的实际生命周期是“整批处理结束”，却把释放责任拆成每条对象的单独动作。我们需要把内存分配策略与业务生命周期对齐。

**用于触发限制的代码／输入（实验场景，不伪称来自框架）：**

```zig
// 假设一次批处理创建数万条临时记录
// 若每条记录都单独申请，并在每个错误出口 individually free，
// 清理路径将变得冗长且容易遗漏
```

**因此需要的能力。** 让分配器成为显式依赖，并按场景选择通用堆分配、arena 批量释放或其他策略。重点不是“Zig 不允许自动释放”，而是控制权在哪里。

## 把新机制落实成代码

**改进后的教学实现：**

```zig
const std = @import("std");
pub fn main() !void {
    var arena = std.heap.ArenaAllocator.init(std.heap.page_allocator);
    defer arena.deinit();
    const a = arena.allocator();
    const amounts = try a.alloc(i64, 3);
    amounts[0] = 100;
    amounts[1] = 250;
    amounts[2] = 300;
}
```

这段 Zig 0.13 风格示例将临时分配交给 arena；作用域结束通过 `defer` 调用 `arena.deinit()` 释放其管理的资源。该例并不意味着任何 arena 分配都没有容量或错误成本。

## 初始化 arena 并取得 allocator

初始化 arena 并取得 allocator；三元素切片从该 allocator 申请；写入金额；函数退出时 defer 执行 deinit，整组临时存储被归还。清理时机对应批处理边界。

## C 也可以自己实现 arena

C 也可以自己实现 arena，Zig 只是将分配器参数与常用抽象更显式地放进 API。

**C 示例（教学代码，非所引用仓库的源码节选）：**

```c
/* 伪代码：由调用者提供 arena，批结束后统一 reset */
void process_batch(Arena *arena) {
    long *amounts = arena_alloc(arena, 3 * sizeof(long));
    /* 使用 amounts，不在这里逐项 free */
}
```

C 中的 `Arena` 类型和 `arena_alloc` 是教学伪代码，并非标准库接口。真正需要读的是资源批量释放的设计模式，而非误以为 C 有同名标准函数。

## 显式分配器让资源策略成为参数

订单程序需要建立临时列表。在 Zig 中，许多可分配内存的容器或函数要求调用方提供 allocator，而不是悄悄使用唯一的全局堆策略。概念上：

```zig
const std = @import("std");
// 教学片段：具体容器 API 需以选定 Zig 版本核对
fn work(allocator: std.mem.Allocator) !void {
    const data = try allocator.alloc(i64, 3);
    defer allocator.free(data);
    data[0] = 100;
}
```

`try` 传播可能的分配错误，`defer` 把清理绑定到当前作用域退出。显式 allocator 让调用者决定是否采用 arena、通用堆或受限内存策略，代价是更多 API 参数和明确的生命周期约定。

如果把内存交给某个 arena 统一释放，单个对象就不再按其自身生命周期精细回收；这样可简化一批请求内对象的清理，却可能在 arena 生命周期过长时保留过多内存。这里没有“一种分配器统治所有场景”，只有**数据保留时间和释放成本**的不同权衡。

## 对照规范和真实实现

**源码/规范阅读路线（供读者核对，并非声称已经逐行验证）：** [Zig 0.13.0 Allocators / defer 参考](https://ziglang.org/documentation/0.13.0/)。

核对 Zig 0.13 的 `ArenaAllocator`、`allocator()`、`deinit()` 与 `defer` 行为。其他 Zig 版本 API 可能变化，示例应以固定版本为准。

## 实验：把 defer arena.deinit() 删除

把 `defer arena.deinit()` 删除，思考在长时间循环处理批次时会出现什么；再把它加回并在每批结束处释放。

**应当观察到（自行运行前属于预期）：** 没有匹配清理可能导致资源持续占用；按批次创建与销毁 arena 让生命周期跟随业务边界。实际分配和释放行为仍需用 Zig 工具链验证。

## 边界：显式 allocator 增加 API 参数和责任

显式 allocator 增加 API 参数和责任，但能根据业务特点控制策略；把 arena 生命期设计得太长也会积累不再需要的数据。

**跨语言迁移时，** Rust 可用自定义 allocator/arena crate、C/C++ 可实现内存池、Go/Java/Python 依赖运行时分配及局部复用策略；差异在可见控制与安全保证。

**下一层问题：** 为什么计算机不能直接执行高级语言文本？

[上一章：为什么 Java、Go、Python 依赖自动内存管理？](39-garbage-collection.md) · [下一章：为什么计算机不能直接执行高级语言文本？](../part-6/41-from-source-to-machine.md)
