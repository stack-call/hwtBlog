---
title: "第 58 章｜为什么多个执行流程不能随意修改同一个值？"
sidebar_position: 58
description: "从订单系统的一次具体失败出发，理解为什么多个执行流程不能随意修改同一个值？，以及由此带来的语法、运行行为与设计取舍。"
slug: /programming-languages/58-data-races
---

# 第 58 章：为什么多个执行流程不能随意修改同一个值？

为了加快订单汇总，我们让多个工作线程同时给同一个 `total` 累加金额。偶尔最终总额却少了一部分。

## 顺序执行时每次累加读取的都是上一步保存的结果

顺序执行时每次累加读取的都是上一步保存的结果。在无共享并发写入条件下，状态转换序列清楚。

**教学实现：**

```python
total = 0
for x in [100, 250, 300]:
    total += x
print(total)  # 650
```

## total += amount 在语义上依赖“读取旧值 + 计算新

`total += amount` 在语义上依赖“读取旧值 + 计算新值 + 更新状态”，并不自动保证与其他执行流程的对应步骤不可交错。

**用于触发限制的代码／输入（实验场景，不伪称来自框架）：**

```text
初始 total=0
线程 A 读到 0，准备写入 100
线程 B 读到 0，准备写入 250
A 写入 100，B 写入 250
期望 350，实际只有 250
```

**因此需要的能力。** 为共享的读-改-写操作提供互斥或原子更新机制，使不同执行流程不会丢失彼此的更新。

## 把新机制落实成代码

**改进后的教学实现：**

```python
import threading
lock = threading.Lock()
total = 0

def add(amount):
    global total
    with lock:
        total += amount

threads = [threading.Thread(target=add, args=(x,)) for x in [100,250,300]]
for t in threads: t.start()
for t in threads: t.join()
print(total)  # 650
```

锁让受保护的临界区在同一时刻由一个线程进入。不要依赖某个 Python 解释器版本的 GIL 作为业务互斥机制；不同构建和版本的并发行为可能不同。

## 线程 A 获得锁

线程 A 获得锁，读 0 写 100，释放；B 获得锁，读 100 写 350；C 最后写 650。真实加锁顺序不必固定，但结果满足每笔累加只发生一次的前提。

## C++ 使用 mutex 与 lock_guard 管理临界区生命

C++ 使用 mutex 与 lock_guard 管理临界区生命周期。

**C++ 示例（教学代码，非所引用仓库的源码节选）：**

```cpp
std::mutex mu;
long total = 0;
auto add = [&](long x) {
    std::lock_guard<std::mutex> guard(mu);
    total += x;
};
```

`lock_guard` 将释放锁绑定到作用域，避免中途 return 忘记解锁；但它不自动避免多个锁之间的死锁。

## 一个最小的丢失更新

两个任务都读 `balance = 350`，任务 A 准备加 100，任务 B 准备减 50。若它们都基于旧值计算，后写入者可能覆盖先写入者的结果。正确的顺序执行结果应为 400，而错误交错可能得到 300 或 450。

```text
A: read 350
B: read 350
A: calculate 450
B: calculate 300
A: write 450
B: write 300  ← A 的更新丢失
```

这段轨迹是一种概念交错，不声称在任意 Python 版本中以下三行就必定出现同样顺序。真实的线程执行粒度、GIL 或优化、内存模型和操作系统调度都会影响复现方法。

修复方案之一是把读—改—写放进受互斥保护的临界区；若操作适合，还可以使用原子读改写。但仅将单次读取改为原子读取，不等于整组更新原子化。

本章应优先解释**哪几步需要不可分割**，然后再去看不同语言的锁和原子类型，而不是背诵 Lock API。

## 对照规范和真实实现

**源码/规范阅读路线（供读者核对，并非声称已经逐行验证）：** [C++ mutex、memory model 参考](https://en.cppreference.com/w/)。

核对 C++ data race 的规范后果与 mutex 的同步语义，再看 Python threading 文档。数据竞争是否属于未定义行为取决于语言，不应一概套用 C++ 规则。

## 实验：增加 10 个线程各累加一万次

增加 10 个线程各累加一万次，比较显式锁与无锁版本；记录具体 Python 版本，不用单次输出证明无锁一定正确。

**应当观察到（自行运行前属于预期）：** 加锁版本保持总额正确（假设没有其他写入口）；无锁在某些实现/调度下可能出现丢失，或在实验中偶然正确，后者不构成安全证明。

## 边界：锁保护共享状态

锁保护共享状态，也使并发变成部分串行，并可能产生等待和死锁。若线程只处理私有局部和，最后再归并，往往更容易推理。

**跨语言迁移时，** Rust 借用+Mutex、Go sync.Mutex/channel、Java synchronized/Lock、C++ mutex/atomic、C 原子、Zig 同步原语、Python Lock 都解决不同层次的共享状态问题。

**下一层问题：** 为什么线程安全不等于结果一定正确？

[上一章：为什么顺序处理无法充分利用等待时间或多核？](57-concurrency-motivation.md) · [下一章：为什么线程安全不等于结果一定正确？](59-thread-safety-vs-correctness.md)
