---
title: "异步与高并发 13：两个执行者都完成了工作，结果为什么还是错的？"
sidebar_label: "十三、都执行完了为什么结果还是错的"
sidebar_position: 14
description: "从丢失更新的可重复实验推导锁与同步原语。"
slug: /async-concurrency/13-races
---

# 两个执行者都完成了工作，结果为什么还是错的？

我们的数据查询服务现在可以把耗 CPU 的步骤交给多个执行者。在给客户端统计请求成功次数时，最直觉的做法是让每个工作者执行 `success_count += 1`。

可是多个执行者对同一变量操作时，存在这样一种可能：**两个人都看到旧值，各自加一后把同一个结果写回去**。两个请求明明完成，计数却只增加一次。

## 一个故意放大错误窗口的实验

注意：在标准 CPython 上，简单的 `x += 1` 并不稳定地复现丢失更新。我们显式拆开“读—计算—写”，并在中间让出执行机会；这是真实的数据竞争模式，但故意扩大了发生窗口。

```python title="实验 13A｜可观察的丢失更新"
import threading
import time

counter = 0

def unsafe_increment():
    global counter
    for _ in range(1_000):
        old = counter
        time.sleep(0)           # 放大读和写之间的切换窗口
        counter = old + 1

threads = [threading.Thread(target=unsafe_increment) for _ in range(2)]
for t in threads:
    t.start()
for t in threads:
    t.join()
print("expected:", 2_000, "actual:", counter)
```

结果通常小于 2000。你可能得到不同值，这正是并发交错不可预测的体现。若偶然等于 2000，也不能说明代码线程安全；要理解两个线程交替执行的一种可能序列：

```text
counter = 0
T1 读到 0 ─────────────────── 写回 1
T2         读到 0 ── 写回 1
最终 counter = 1，而不是 2
```

关键问题不是“`+1` 很慢”，而是**读—改—写多个动作不是一个不可分割的业务操作**。

## 现在才需要锁

我们真正要保证的是：在同一时刻，最多只有一个执行者进行这段读—改—写。一个直接的机制是**互斥锁（mutex）**。

```python title="实验 13B｜用互斥锁保护临界区"
import threading
import time

counter = 0
lock = threading.Lock()

def safe_increment():
    global counter
    for _ in range(1_000):
        with lock:
            old = counter
            time.sleep(0)
            counter = old + 1

threads = [threading.Thread(target=safe_increment) for _ in range(2)]
for t in threads:
    t.start()
for t in threads:
    t.join()
print("expected:", 2_000, "actual:", counter)
```

如果所有访问者都遵守同一把锁，最终值应当为 2000。锁保护的是一个**临界区**：这一段操作在业务上需要彼此互斥。并不是任何地方都应该加锁；锁有竞争和等待成本，还可能引入死锁。

## 为什么这也叫“同步”？

前面的“同步 I/O”描述操作返回结果的方式；本章的**线程同步**是约束多个执行者之间的访问关系或顺序。两个上下文都使用“同步”这个汉语词，但问题完全不同。

还有两种需要区分的目标：

- **互斥/原子性**：保护读—改—写的复合操作，避免丢失更新。
- **可见性/顺序**：在并行 CPU 与编译器优化下，执行者对共享数据的观察顺序需要满足语言内存模型和同步规则。

这里不把 Python 的 GIL 视作万能锁。其他语言如 C++、Java、Rust 各自拥有不同的内存模型和同步原语；要分析内存可见性，必须落到具体语言的规则，而不能仅凭 CPU 缓存的直觉。

## 其他方案什么时候出现？

如果多线程只是把结果发回单个统计线程，可以用消息队列减少共享可变状态；简单计数有时适合原子变量；读多写少的结构可能需要读写锁。这些是之后根据争用模式选择的替代方案，不必本章一次学完。

历史背景：Dijkstra 的信号量与后来的 Monitor 机制，都是为解决并发执行中的协调问题发展出来的。接口参考 [Python Lock](https://docs.python.org/3.13/library/threading.html#lock-objects)。

到这里我们已经分别理解了**等待时如何并发推进**、**计算时如何真正并行**、**共享数据如何保持正确**。接下来的问题是：为什么现代语言会把这些底层机制封装成完全不同的编程模型？从[第 14 章](14-async-models.md)开始。
