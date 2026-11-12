---
title: "引言｜一条主线理解七门编程语言"
sidebar_position: 0
description: "从订单程序的持续演化，理解 Python、Java、C、C++、Rust、Zig、Go 共有的计算问题、抽象与底层实现。"
slug: /programming-languages/00-introduction
---

# 引言：为什么要用同一个程序理解七门语言？

如果分别背 Python 的 `class`、Java 的 `interface`、Rust 的 `trait`、Go 的 `interface` 和 Zig 的 `comptime`，你能学到七套语法，却很难回答一个更重要的问题：它们为何出现？都在解决什么限制？不同语言又怎样取舍？

这套课程从一个具体任务开始：**给一组订单金额求和。** 此时只需要数字、赋值和循环；不需要类、接口、垃圾回收、异步或编译器。订单系统继续成长后，每次只允许一个新的、可观察的失败迫使我们增加一个机制。读者要亲手经历：简单写法为什么原本足够、什么条件让它不够、最小新方案怎样修改运行行为。

## 一、贯穿案例：一笔订单怎样发展成完整系统

我们的基线是可以运行的 Python 程序：

```python
amounts = [100, 250, -30]
total = 0
for amount in amounts:
    if amount >= 0:
        total += amount
print(total)  # 350
```

它只是一笔业务规则：忽略负数，对其余金额求和。随后逐渐出现数据约束、函数复用、对象状态、不同计价策略、接口契约、内存生命周期、真实编译与运行、文件和网络、多个执行任务、构建和测试等问题。**需求始终围绕同一订单系统演化；更换的是必须解决的问题，不是随意切换案例。**

## 二、每章都要完成一条因果链

1. **眼前任务**：一组具体输入和明确输出。
2. **最小实现**：先用能直接读懂的语法把需求做出来。
3. **失败条件**：给出反例，说明旧方案到底哪里不够。
4. **最小机制**：只增加足以解决当前失败的一种设计。
5. **手工执行**：对照输入、状态变化、输出或副作用。
6. **跨语言对照**：理解共同语义和不同语言的额外约束。
7. **固定源码入口**：用真实项目的特定版本核对实现，不把伪代码冒充源码。
8. **边界与实验**：说明它不保证什么，留下可运行的验证路径。

这种安排并不把“术语多”当作知识深度。真正的深度来自能解释因果，而且能在实际代码里找到证据。

## 三、需要掌握哪些语言？

**先保证能读 Python，再用其他语言建立对照。** 一章不强迫一次讲完七门语法，而是挑选最有解释力的另一门语言。例如讨论对象时，Python 与 C 的“结构体 + 函数”对照能够揭示封装不是 `class` 专属；讨论借用时，Rust 与 C++ 对照更有价值；讨论 channel 时，Go 的实际运行时实现更直接。

课程采用以下基准作为源码观察入口，均会在相应章节标明并要求检查：CPython `v3.13.5`，Go `go1.24.5`；其他语言在引用规范或具体实现时单独说明所用版本，不把主分支当作稳定快照。

## 四、代码与实验的可信度约定

正文中的代码直接嵌入 Markdown 围栏，不建立独立代码工程。**教学代码**是为了理解机制编写的最小示例；**实验**提供预期输出与运行条件，除非明确说明已执行，否则不宣称通过；**真实源码**必须有仓库、版本或提交、文件及可核对的代码。链接到源码的“阅读入口”不是逐字源码节选。

涉及整数溢出、对象生命周期、编译优化或并发调度时，本书会刻意区分语言规范承诺的行为和某个实现中观察到的行为。跨语言语法可以相似，但运行条件与语义并不因此相同。

## 五、完整阅读路径（10 篇，70 章）

- **第 1 篇 · 计算是如何被表达的？**（01—07）—— 一个数字怎样演变为可复用、有状态的程序
- **第 2 篇 · 数据和行为如何组织成对象？**（08—14）—— 从松散字段到稳定对象边界
- **第 3 篇 · 不同组件如何替换与协作？**（15—23）—— 从单一实现到可扩展结构
- **第 4 篇 · 类型系统如何约束复杂程序？**（24—30）—— 从迟到的错误到更强的语义约束
- **第 5 篇 · 数据在内存中究竟如何存在？**（31—40）—— 从函数返回数据到资源管理
- **第 6 篇 · 源代码究竟如何被执行？**（41—50）—— 从语言文本到真正的机器行为
- **第 7 篇 · 程序如何与操作系统交互？**（51—56）—— 从纯计算到文件、网络与进程
- **第 8 篇 · 为什么并发程序如此难写？**（57—63）—— 从顺序执行到多任务协调
- **第 9 篇 · 大型程序为什么需要架构？**（64—69）—— 从能运行到能维护和演化
- **第 10 篇 · 七门语言的统一模型**（70—70）—— 同一需求、不同设计取舍

## 六、所有章节的链接

- [01. 为什么一个算式还不算完整的程序？](part-1/01-expression-to-program.md)
- [02. 为什么计算需要记住过去？](part-1/02-variables-remember-state.md)
- [03. 为什么程序需要决定下一步做什么？](part-1/03-control-flow.md)
- [04. 为什么要把计算过程变成函数？](part-1/04-functions.md)
- [05. 为什么相同的代码面对不同数据会失败？](part-1/05-types-matter.md)
- [06. 为什么数据需要放在一起？](part-1/06-composite-data.md)
- [07. 为什么有些计算会改变外部世界？](part-1/07-side-effects.md)
- [08. 为什么一组字段还不足以表达一笔订单？](part-2/08-from-struct-to-object.md)
- [09. 为什么不能允许任意代码修改订单？](part-2/09-encapsulation.md)
- [10. 为什么要把数据与操作组织在一起？](part-2/10-class-state-and-behavior.md)
- [11. 为什么有些数据应该只读？](part-2/11-immutability.md)
- [12. 为什么一个对象会引用另一个对象？](part-2/12-object-references.md)
- [13. 为什么有时组合比扩展一个大类更合理？](part-2/13-composition-over-inheritance.md)
- [14. 为什么函数也能作为数据传递？](part-2/14-functions-as-values.md)
- [15. 为什么增加一种计价方式就要修改订单代码？](part-3/15-open-closed-pricing.md)
- [16. 为什么同一个操作可以由不同组件实现？](part-3/16-polymorphism.md)
- [17. 为什么需要明确规定组件必须具备什么能力？](part-3/17-interfaces-and-contracts.md)
- [18. 为什么不直接依赖具体实现？](part-3/18-depend-on-abstractions.md)
- [19. 为什么需要从外部提供依赖对象？](part-3/19-dependency-injection.md)
- [20. 为什么有些类型之间适合使用继承？](part-3/20-when-inheritance-fits.md)
- [21. 为什么继承容易形成脆弱的类型体系？](part-3/21-fragile-inheritance.md)
- [22. 为什么不同类型需要复用同一套算法？](part-3/22-generics-reuse-algorithms.md)
- [23. 为什么大型程序需要模块边界？](part-3/23-module-boundaries.md)
- [24. 为什么有些错误可以在执行前发现？](part-4/24-compile-time-checks.md)
- [25. 为什么不必为每个变量手写类型？](part-4/25-type-inference.md)
- [26. 为什么存在不同形式的多态？](part-4/26-forms-of-polymorphism.md)
- [27. 为什么相似的类型不一定能够互相替代？](part-4/27-subtyping-and-variance.md)
- [28. 为什么并非所有业务状态都适合用类层次表达？](part-4/28-algebraic-data-types.md)
- [29. 为什么空值和失败状态容易造成错误？](part-4/29-null-and-failure-states.md)
- [30. 为什么有些规则适合在编译期计算或检查？](part-4/30-comptime-and-macros.md)
- [31. 一个整数和一个对象在机器里是什么？](part-5/31-values-in-memory.md)
- [32. 为什么数组和链式数据结构的访问方式不同？](part-5/32-array-vs-linked-structure.md)
- [33. 为什么需要地址、指针和引用？](part-5/33-pointers-and-references.md)
- [34. 为什么函数返回后有些数据会失效？](part-5/34-lifetime-and-dangling.md)
- [35. 为什么动态数据需要不同的存储方式？](part-5/35-heap-allocation.md)
- [36. 为什么手工管理内存容易出错？](part-5/36-manual-memory-management.md)
- [37. 为什么 C++ 使用 RAII？](part-5/37-raii-in-cpp.md)
- [38. 为什么 Rust 需要所有权与借用？](part-5/38-rust-ownership-and-borrowing.md)
- [39. 为什么 Java、Go、Python 依赖自动内存管理？](part-5/39-garbage-collection.md)
- [40. 为什么 Zig 强调显式分配器？](part-5/40-zig-explicit-allocators.md)
- [41. 为什么计算机不能直接执行高级语言文本？](part-6/41-from-source-to-machine.md)
- [42. 为什么语法正确的程序仍然可能不合法？](part-6/42-parse-vs-semantics.md)
- [43. 如何让一个极简解释器真正跑起来？](part-6/43-build-a-mini-interpreter.md)
- [44. 为什么不直接反复遍历语法树？](part-6/44-from-tree-to-bytecode.md)
- [45. 为什么编译器需要中间表示？](part-6/45-intermediate-representation.md)
- [46. 高级语言中的函数调用如何落到机器上？](part-6/46-function-calls-to-machine.md)
- [47. 对象方法和接口调用究竟如何执行？](part-6/47-method-and-virtual-dispatch.md)
- [48. 编译器为什么能够改变程序的执行方式？](part-6/48-compiler-optimizations.md)
- [49. 为什么 Python、Java 与 C/Rust/Go 有不同的执行路线？](part-6/49-execution-routes.md)
- [50. 多个源码文件如何变成一个可运行程序？](part-6/50-linking-and-build.md)
- [51. 为什么读取文件不能只靠普通内存操作？](part-7/51-file-io-and-syscalls.md)
- [52. 为什么运行中的程序需要进程？](part-7/52-processes.md)
- [53. 为什么网络读写可能一直无法完成？](part-7/53-network-io-blocks.md)
- [54. 为什么外部操作需要明确处理失败？](part-7/54-error-handling-boundaries.md)
- [55. 为什么同一程序能在不同机器上运行？](part-7/55-portable-binaries-and-abi.md)
- [56. 为什么与外部代码交互需要格外小心？](part-7/56-ffi-boundaries.md)
- [57. 为什么顺序处理无法充分利用等待时间或多核？](part-8/57-concurrency-motivation.md)
- [58. 为什么多个执行流程不能随意修改同一个值？](part-8/58-data-races.md)
- [59. 为什么线程安全不等于结果一定正确？](part-8/59-thread-safety-vs-correctness.md)
- [60. 为什么不同线程看到的内存变化可能不同？](part-8/60-memory-model.md)
- [61. 为什么有些并发系统倾向于传递消息？](part-8/61-message-passing.md)
- [62. 为什么等待 IO 不一定需要占用一个线程？](part-8/62-async-io-event-loop.md)
- [63. 为什么异步任务还需要调度、取消和生命周期管理？](part-8/63-async-task-lifecycle.md)
- [64. 为什么代码复用不等于设计良好？](part-9/64-reuse-is-not-design.md)
- [65. 为什么某些设计结构会反复出现？](part-9/65-design-patterns.md)
- [66. 为什么业务逻辑不应该直接绑定数据库或网络？](part-9/66-layered-architecture.md)
- [67. 为什么大型代码库需要独立构建和版本管理？](part-9/67-modules-and-versioning.md)
- [68. 为什么通过测试还不等于程序可靠？](part-9/68-testing-limits.md)
- [69. 为什么更高层的抽象不一定更慢或更好？](part-9/69-abstraction-cost.md)
- [70. 为什么七门语言会从共同的计算需求走向不同的设计？](part-10/70-unified-model.md)

## 七、建议的学习方式

一章只做一个小实验：先预测，再运行，再解释。如果某章需要深入 C、Rust 或 JVM，但本地没有工具链，就先读懂代码并记录尚未验证的部分；没有运行过的程序不能凭“看起来正确”算作实验成功。推荐保留一本对照笔记，每页写四列：问题、最小机制、不同语言语法、源码证据。

**从这里开始：** [第 01 章：为什么一个算式还不算完整的程序？](part-1/01-expression-to-program.md)。
