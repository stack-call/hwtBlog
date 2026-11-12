---
title: "第 60 章｜为什么不同线程看到的内存变化可能不同？"
sidebar_position: 60
description: "从订单系统的一次具体失败出发，理解为什么不同线程看到的内存变化可能不同？，以及由此带来的语法、运行行为与设计取舍。"
slug: /programming-languages/60-memory-model
---

# 第 60 章：为什么不同线程看到的内存变化可能不同？

一个线程设置 `ready=true`，另一个线程看到 ready 之后读取订单结果。开发者以为“先写结果再置 ready”就足以保证对方读到正确结果。

## 单线程从上到下阅读语句很自然

单线程从上到下阅读语句很自然，但两个执行流程之间缺少明确同步关系时，编译器与硬件的重排和缓存可见性可能影响观察。

**教学实现：**

```text
线程 A：result = 350; ready = true
线程 B：while (!ready) {} ; print(result)
看起来像“先写再读”的普通顺序。
```

## 在 C++ 内存模型中

在 C++ 内存模型中，缺少同步的竞争读写可能是未定义行为。不能靠“CPU 最终总会刷新缓存”来证明程序正确。

**用于触发限制的代码／输入（实验场景，不伪称来自框架）：**

```cpp
// 教学反例：非原子变量上的并发读写可能构成 data race
bool ready = false;
int result = 0;
// 线程 A 写，线程 B 同时无同步地读
```

**因此需要的能力。** 定义跨线程同步及可见性规则，让一个线程的写入按规定的先后关系被另一个线程观察。原子类型与内存序属于这一层机制。

## 把新机制落实成代码

**改进后的教学实现：**

```cpp
#include <atomic>
std::atomic<bool> ready{false};
int result = 0;
// 线程 A：result = 350; ready.store(true, std::memory_order_release);
// 线程 B：while (!ready.load(std::memory_order_acquire)) {}
//         assert(result == 350);
```

当 B 的 acquire 读到 A 的 release 写入时，建立跨线程的同步关系，让之前的 `result` 写入在这个场景下可被正确观察。它不是允许其他线程随意并发写 `result`。

## A 先写 result=350

A 先写 `result=350`，再 release 存储 true；B 循环 acquire 读取直到观测该 true；同步关系使 B 之后读取 result 的行为受约束。若不满足同一同步链，推论不能直接套用。

## Java 的 volatile 也提供特定可见性与有序性保证

Java 的 volatile 也提供特定可见性与有序性保证，但语法和形式规则不同。

**Java 示例（教学代码，非所引用仓库的源码节选）：**

```java
private int result = 0;
private volatile boolean ready = false;
// writer: result = 350; ready = true;
// reader: if (ready) System.out.println(result);
```

volatile 读写可建立相应的 happens-before 关系，但 `volatile int count; count++` 仍不是原子的复合更新。

## 程序顺序为何不总是另一个线程的观察顺序

线程 A 先写订单内容再设置 `ready = true`，线程 B 看到 `ready` 就读取内容。若普通共享变量之间没有合法同步关系，在某些语言内存模型中不能仅凭源码写入顺序推断线程 B 一定看到全部内容。

```text
线程 A：write(order); publish(ready)
线程 B：wait(ready); read(order)
```

正确实现通常需要锁、channel、具有合适内存序的原子操作等，建立必要的 happens-before 关系。C++ 与 Rust 有形式化的原子内存序，Java `volatile` 提供特定可见性与排序保证，Go 内存模型对同步操作有定义。Python 的特定实现行为不等同于一份适用于所有解释器的统一硬件内存模型承诺。

不要把“加 `volatile` 就能修所有并发问题”当成结论：可见性与排序不等于复合业务操作的原子性。实际工程应优先使用更高层同步原语，只有必要时才接触弱内存序的细节。

## 对照规范和真实实现

**源码/规范阅读路线（供读者核对，并非声称已经逐行验证）：** [Go Memory Model 与多语言内存模型对照入口](https://go.dev/ref/mem)。

核对 Go 内存模型中的同步概念，再阅读 C++/Java 规范对 acquire-release 或 volatile 的具体条款。不要混用不同语言内存模型的术语细节。

## 实验：将 Java volatile 字段换成普通字段

将 Java `volatile` 字段换成普通字段，讨论为什么测试中的“通常能正常”不是正确性证明；再用 `AtomicInteger` 测试原子递增。

**应当观察到（自行运行前属于预期）：** 没有足够同步就不能建立可见性保证；原子增量与只是 volatile 修饰的读改写不是同一语义。

## 边界：内存序提供更低层的性能与控制

内存序提供更低层的性能与控制，但使用错误非常隐蔽。业务代码优先使用锁、channel 或成熟的高层同步容器。

**跨语言迁移时，** Java、C/C++、Rust、Go 都有各自明确定义的并发内存约束；Python 的具体线程执行行为依赖解释器与同步 API，Zig 也有原子与同步机制。

**下一层问题：** 为什么有些并发系统倾向于传递消息？

[上一章：为什么线程安全不等于结果一定正确？](59-thread-safety-vs-correctness.md) · [下一章：为什么有些并发系统倾向于传递消息？](61-message-passing.md)
