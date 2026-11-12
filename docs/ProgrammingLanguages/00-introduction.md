---
title: "引言｜从一笔订单推导七门语言的共同原理"
sidebar_position: 0
description: "逐章通过真实代码、失败输入与执行过程理解 Python、Java、C、C++、Rust、Zig、Go。"
slug: /programming-languages/00-introduction
---

# 引言：为什么要用同一笔订单学习七门语言？

> 十篇 70 章的完整目录见 [《从计算本质到语言实现：贯通 Python、Java、C、C++、Rust、Zig、Go》](./00-catalog.md)，按篇列出每章的研究问题、关键词与阶段成果。

最初只需要给几笔订单金额求和。我们写一个整数相加，随后出现可变输入、订单状态、支付失败、多个计价策略、文件读写、并发请求。每增加一个真实约束，原来的简单做法就暴露一个可观察问题。这时才有必要寻找更强的语言机制。

```python
amounts = [100, 250, -30]
total = sum(c for c in amounts if c >= 0)
assert total == 350
```

它只是基线，而不是最终架构。此时我们不需要讲虚函数表、GC、分布式事务；后面每个机制都应从它所解决的局部困难自然长出来。

## 怎样阅读每一章

先看输入和最简单的实现，自己预测执行结果；再找到使原方案失效的反例，辨认新设计究竟改变了什么；然后运行本章嵌入的代码，最后再去源码或规范核对机制。跨语言示例服务于同一个问题，不意味着每种语言都必须拥有相同的关键字或运行时模型。

**关于质量与验证的边界：** 第 09、13、17 章是单独长篇深度精读，并含固定版本的真实源码节选及调用路径。其余章节为逐章扩写的专题教程，含独立的案例、具体语法、运行推导、跨语言区别和源码/规范阅读线索，但不冒充已经完成对所有七门语言编译器的逐行源码验证。多数代码属于教学示例，不等于已经逐段编译或运行。

## 十篇课程与完整目录

每一章都可以独立阅读，但推荐按章节顺序，因为后面的失败场景复用前面的设计。

### 第 1 篇：计算如何被表达

- [第 01 章｜为什么一个算式还不算完整的程序？](part-1/01-expression-to-program.md)
- [第 02 章｜为什么计算需要记住过去？](part-1/02-variables-remember-state.md)
- [第 03 章｜为什么程序需要决定下一步做什么？](part-1/03-control-flow.md)
- [第 04 章｜为什么要把计算过程变成函数？](part-1/04-functions.md)
- [第 05 章｜为什么相同的代码面对不同数据会失败？](part-1/05-types-matter.md)
- [第 06 章｜为什么数据需要放在一起？](part-1/06-composite-data.md)
- [第 07 章｜为什么有些计算会改变外部世界？](part-1/07-side-effects.md)

### 第 2 篇：对象与数据组织

- [第 08 章｜为什么一组字段还不足以表达一笔订单？](part-2/08-from-struct-to-object.md)
- [09｜为什么不能让任何代码随便修改订单：从字典到封装与类](part-2/09-encapsulation.md)
- [第 10 章｜为什么要把数据与操作组织在一起？](part-2/10-class-state-and-behavior.md)
- [第 11 章｜为什么有些数据应该只读？](part-2/11-immutability.md)
- [第 12 章｜为什么一个对象会引用另一个对象？](part-2/12-object-references.md)
- [13｜为什么一个巨大的 Order 类会失控：从条件分支推导组合](part-2/13-composition-over-inheritance.md)
- [第 14 章｜为什么函数也能作为数据传递？](part-2/14-functions-as-values.md)

### 第 3 篇：组件替换与协作

- [第 15 章｜为什么增加一种计价方式就要修改订单代码？](part-3/15-open-closed-pricing.md)
- [第 16 章｜为什么同一个操作可以由不同组件实现？](part-3/16-polymorphism.md)
- [17｜为什么组合还不够：从一次 TypeError 推导接口、Protocol 与动态分派](part-3/17-interfaces-and-contracts.md)
- [第 18 章｜为什么不直接依赖具体实现？](part-3/18-depend-on-abstractions.md)
- [第 19 章｜为什么需要从外部提供依赖对象？](part-3/19-dependency-injection.md)
- [第 20 章｜为什么有些类型之间适合使用继承？](part-3/20-when-inheritance-fits.md)
- [第 21 章｜为什么继承容易形成脆弱的类型体系？](part-3/21-fragile-inheritance.md)
- [第 22 章｜为什么不同类型需要复用同一套算法？](part-3/22-generics-reuse-algorithms.md)
- [第 23 章｜为什么大型程序需要模块边界？](part-3/23-module-boundaries.md)

### 第 4 篇：类型系统

- [第 24 章｜为什么有些错误可以在执行前发现？](part-4/24-compile-time-checks.md)
- [第 25 章｜为什么不必为每个变量手写类型？](part-4/25-type-inference.md)
- [第 26 章｜为什么存在不同形式的多态？](part-4/26-forms-of-polymorphism.md)
- [第 27 章｜为什么相似的类型不一定能够互相替代？](part-4/27-subtyping-and-variance.md)
- [第 28 章｜为什么并非所有业务状态都适合用类层次表达？](part-4/28-algebraic-data-types.md)
- [第 29 章｜为什么空值和失败状态容易造成错误？](part-4/29-null-and-failure-states.md)
- [第 30 章｜为什么有些规则适合在编译期计算或检查？](part-4/30-comptime-and-macros.md)

### 第 5 篇：内存和资源

- [第 31 章｜一个整数和一个对象在机器里是什么？](part-5/31-values-in-memory.md)
- [第 32 章｜为什么数组和链式数据结构的访问方式不同？](part-5/32-array-vs-linked-structure.md)
- [第 33 章｜为什么需要地址、指针和引用？](part-5/33-pointers-and-references.md)
- [第 34 章｜为什么函数返回后有些数据会失效？](part-5/34-lifetime-and-dangling.md)
- [第 35 章｜为什么动态数据需要不同的存储方式？](part-5/35-heap-allocation.md)
- [第 36 章｜为什么手工管理内存容易出错？](part-5/36-manual-memory-management.md)
- [第 37 章｜为什么 C++ 使用 RAII？](part-5/37-raii-in-cpp.md)
- [第 38 章｜为什么 Rust 需要所有权与借用？](part-5/38-rust-ownership-and-borrowing.md)
- [第 39 章｜为什么 Java、Go、Python 依赖自动内存管理？](part-5/39-garbage-collection.md)
- [第 40 章｜为什么 Zig 强调显式分配器？](part-5/40-zig-explicit-allocators.md)

### 第 6 篇：编译与执行

- [第 41 章｜为什么计算机不能直接执行高级语言文本？](part-6/41-from-source-to-machine.md)
- [第 42 章｜为什么语法正确的程序仍然可能不合法？](part-6/42-parse-vs-semantics.md)
- [第 43 章｜如何让一个极简解释器真正跑起来？](part-6/43-build-a-mini-interpreter.md)
- [第 44 章｜为什么不直接反复遍历语法树？](part-6/44-from-tree-to-bytecode.md)
- [第 45 章｜为什么编译器需要中间表示？](part-6/45-intermediate-representation.md)
- [第 46 章｜高级语言中的函数调用如何落到机器上？](part-6/46-function-calls-to-machine.md)
- [第 47 章｜对象方法和接口调用究竟如何执行？](part-6/47-method-and-virtual-dispatch.md)
- [第 48 章｜编译器为什么能够改变程序的执行方式？](part-6/48-compiler-optimizations.md)
- [第 49 章｜为什么 Python、Java 与 C/Rust/Go 有不同的执行路线？](part-6/49-execution-routes.md)
- [第 50 章｜多个源码文件如何变成一个可运行程序？](part-6/50-linking-and-build.md)

### 第 7 篇：操作系统和外部世界

- [第 51 章｜为什么读取文件不能只靠普通内存操作？](part-7/51-file-io-and-syscalls.md)
- [第 52 章｜为什么运行中的程序需要进程？](part-7/52-processes.md)
- [第 53 章｜为什么网络读写可能一直无法完成？](part-7/53-network-io-blocks.md)
- [第 54 章｜为什么外部操作需要明确处理失败？](part-7/54-error-handling-boundaries.md)
- [第 55 章｜为什么同一程序能在不同机器上运行？](part-7/55-portable-binaries-and-abi.md)
- [第 56 章｜为什么与外部代码交互需要格外小心？](part-7/56-ffi-boundaries.md)

### 第 8 篇：并发与异步

- [第 57 章｜为什么顺序处理无法充分利用等待时间或多核？](part-8/57-concurrency-motivation.md)
- [第 58 章｜为什么多个执行流程不能随意修改同一个值？](part-8/58-data-races.md)
- [第 59 章｜为什么线程安全不等于结果一定正确？](part-8/59-thread-safety-vs-correctness.md)
- [第 60 章｜为什么不同线程看到的内存变化可能不同？](part-8/60-memory-model.md)
- [第 61 章｜为什么有些并发系统倾向于传递消息？](part-8/61-message-passing.md)
- [第 62 章｜为什么等待 IO 不一定需要占用一个线程？](part-8/62-async-io-event-loop.md)
- [第 63 章｜为什么异步任务还需要调度、取消和生命周期管理？](part-8/63-async-task-lifecycle.md)

### 第 9 篇：架构与工程

- [第 64 章｜为什么代码复用不等于设计良好？](part-9/64-reuse-is-not-design.md)
- [第 65 章｜为什么某些设计结构会反复出现？](part-9/65-design-patterns.md)
- [第 66 章｜为什么业务逻辑不应该直接绑定数据库或网络？](part-9/66-layered-architecture.md)
- [第 67 章｜为什么大型代码库需要独立构建和版本管理？](part-9/67-modules-and-versioning.md)
- [第 68 章｜为什么通过测试还不等于程序可靠？](part-9/68-testing-limits.md)
- [第 69 章｜为什么更高层的抽象不一定更慢或更好？](part-9/69-abstraction-cost.md)

### 第 10 篇：语言设计总对照

- [第 70 章｜为什么七门语言会从共同的计算需求走向不同的设计？](part-10/70-unified-model.md)

## 全程固定的代码与源码约定

代码直接嵌在 Markdown 正文中。若片段为教学代码，会明确其目的；真实源码节选应提供固定版本、文件与调用位置；给出官方仓库或规范链接仅代表阅读路线，不等于完成源码追踪。所有未执行的示例都应由读者在本地核对编译器版本、测试输出和环境差异。

## 学完以后应该做到什么

给出一个新的语言特性，不急着背语法，而是先回答：它解决哪种具体失败？没有它是否存在更简单的实现？它为调用者做出了什么保证？这些保证来自编译器、运行时还是开发者约定？最后，真实程序怎样验证这份保证？
