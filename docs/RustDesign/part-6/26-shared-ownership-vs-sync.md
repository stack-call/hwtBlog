---
title: "26 为什么共享所有权与同步访问是两个不同问题？"
sidebar_position: 26
description: "为什么共享所有权与同步访问是两个不同问题？沿问题、代码、运行机制与设计取舍逐步推导。"
slug: /rust-design/26-shared-ownership-vs-sync
---

# 26 为什么共享所有权与同步访问是两个不同问题？

> **第 6 篇 · 为什么所有权规则会影响并发程序？**  
> 本文聚焦一个因果问题：为什么共享所有权与同步访问是两个不同问题？；代码示例以稳定 Rust 的通用语法为主（适用于 2021/2024 Edition），除非明确注明为示意或故意无法编译的例子。

一个订单统计器被多个线程长期持有，需要确保对象不会过早释放；同时多个线程都要增加计数，又必须避免写冲突。仅靠引用计数不能保证计数操作原子，只有锁也不能让多个线程长期共同拥有同一个对象。

## 共享所有权并不提供互斥

`Arc` 的原子操作仅保护引用计数本身，不自动保护其内部字段。`Mutex<T>` 通过锁保护对 `T` 的受控访问；`RwLock<T>` 允许多个读者或独占写者；`AtomicU64` 等适合简单原子操作。究竟选择哪种，取决于被保护的不变量和需要的操作粒度。

给并发状态建模时可以依次问：谁持有？谁修改？哪些操作必须一起发生？只看“一个字段需要加一”会误选原子变量，而库存预扣与订单状态更新可能需要同一个事务或更大范围的同步。

## Arc 只负责持有，不负责保护所有字段

`Arc<T>` 让多个线程共同拥有同一份分配；它的引用计数修改是线程安全的，但其内部 `T` 如何被读写仍受类型约束。`Mutex<T>` 提供排他访问，使拿到守卫的线程能安全修改数据。把二者组合不是固定咒语：它们分别回答“谁能活着持有数据”和“谁能在此刻修改数据”。只有在真正需要两个性质时才同时使用。

```rust
use std::sync::{Arc, Mutex};
use std::thread;
fn main() {
    let total = Arc::new(Mutex::new(0_u64));
    let mut tasks = vec![];
    for _ in 0..4 {
        let shared = Arc::clone(&total);
        tasks.push(thread::spawn(move || {
            let mut guard = shared.lock().unwrap();
            *guard += 10;
        }));
    }
    for task in tasks { task.join().unwrap(); }
    assert_eq!(*total.lock().unwrap(), 40);
}
```

四个线程可以共同拥有 `total`，但每次只有取得锁守卫的线程能访问需要受互斥保护的数据。这里 `unwrap()` 简化了中毒错误处理；生产代码应根据业务上下文考虑恢复或传播。

## Arc 和 Mutex 解决两个正交问题

```rust
use std::sync::{Arc, Mutex};
use std::thread;
fn main() {
    let value = Arc::new(Mutex::new(0_u64));
    let mut handles = Vec::new();
    for _ in 0..4 {
        let v = Arc::clone(&value);
        handles.push(thread::spawn(move || {
            let mut g = v.lock().unwrap();
            *g += 1;
        }));
    }
    for h in handles { h.join().unwrap(); }
    assert_eq!(*value.lock().unwrap(), 4);
}
```

每个线程拥有一个 `Arc`，共享底层 `Mutex`。加一操作发生在获取 `MutexGuard` 后的临界区内，守卫析构时自动解锁。`lock` 可能阻塞，也可能因锁毒化返回错误；示例使用 `unwrap` 简化讲解，生产代码应根据策略处理。

## 缩小临界区不等于随便切碎业务操作

为了减少锁竞争，开发者常把昂贵计算放在锁外。这通常是正确方向，但不能把一个必须原子的检查-修改事务拆开。例如先锁读出库存，再解锁计算“是否允许”，最后重新锁写库存，中间库存可能已经被其他线程消耗。优化之前要明确业务不变量：哪一段逻辑必须对外表现为不可分割？

对只读配置，`Arc<Config>` 不需要额外 Mutex；对于简单计数器，`AtomicU64` 可以避免互斥锁，但要理解它的内存排序与溢出语义；对复杂写多读少的共享字典，Mutex 可能更简单可靠。`RwLock` 允许多个并发读者，但在写竞争下性能未必总优。

## 临界区如何定义业务正确性

并发 API 的返回值常是守卫类型，而不直接返 `&mut T`，因为锁必须和借用生命周期一起维护。这个设计把同步机制、RAII 和借用关系连接起来。对于大量只读负载，`RwLock` 可能更合适；而原子操作适合无需锁保护复杂多字段不变量的情况。

```rust
use std::sync::atomic::{AtomicU64, Ordering};
fn main() {
    let count = AtomicU64::new(0);
    count.fetch_add(1, Ordering::Relaxed);
    count.fetch_add(2, Ordering::Relaxed);
    assert_eq!(count.load(Ordering::Relaxed), 3);
}
```

`Relaxed` 保证对该原子变量的相关操作具有原子性，但不提供用于其他数据之间同步的额外排序保证。统计计数器可以是其适用场景之一，但不要把它直接用作发布/读取其他共享数据的同步协议。内存排序属于需要专门学习的并发基础。

## 守卫也是一个具有生命周期的资源

```rust
use std::sync::Mutex;
fn main() {
    let total = Mutex::new(10);
    {
        let mut guard = total.lock().unwrap();
        *guard += 5;
    } // 守卫离开作用域，自动释放锁
    println!("{}", *total.lock().unwrap());
}
```

锁守卫利用 RAII 在 Drop 时释放互斥锁。这是所有权设计对并发库 API 的直接影响：不是调用 `unlock()` 才算完成一次访问，而是通过拥有守卫值控制互斥权限的生存期。反过来，如果把守卫保存过久，就会扩大临界区并可能造成阻塞甚至死锁。

## 一次锁的获取既是同步，也是资源拥有

线程获取 `MutexGuard` 后拥有一份守卫值；守卫的生命周期就是其可访问受保护数据的时间窗口，离开作用域时 Drop 释放锁。这个 RAII 模型让异常控制流、早退和正常返回都能够自动释放锁。但它不会自动判断持锁期间的业务是否合理：如果在锁里执行网络请求，其他线程可能长时间无法访问共享状态。

```rust
use std::sync::Mutex;
fn snapshot_then_work(lock: &Mutex<Vec<u64>>) -> u64 {
    let snapshot = {
        let guard = lock.lock().unwrap();
        guard.iter().sum::<u64>()
    }; // 这里立即释放锁
    snapshot * 2 // 在锁外进行纯计算
}
fn main() {
    let lock = Mutex::new(vec![1, 2, 3]);
    assert_eq!(snapshot_then_work(&lock), 12);
}
```

先计算好快照，再释放锁，是缩短临界区的一种方式。对于较大数据，获取快照本身可能需要复制；如果快照不包含一致的整体状态，计算也可能失去业务意义。应先定义操作的线性化点，再决定哪些步骤可放在锁外。

## 为什么可能需要 RwLock 或原子类型

在读远多于写的配置中心，`RwLock` 可以允许多读者并行；但高频读锁获取仍有成本，写入可能等待。简单的独立数值更新可以考虑原子类型，例如 `AtomicU64::fetch_add`；然而多个数值之间的不变量仍可能需要互斥或事务。不要为了追求“无锁”而放弃可解释的正确性边界。

实验记录不同线程数下的吞吐与延迟，并标注代码中取得/释放锁的位置。只有这样才能知道瓶颈来自计数、缓存访问还是业务 IO。

## 读多写少与原子类型的不同选择

C++ 的 `shared_ptr` 加锁、Java 的 `AtomicLong`/锁、Go 的 `sync.Mutex`/atomic 都提供相似分层。Rust 借助所有权、自动 Trait 和守卫生命周期帮助约束错误组合，但性能和正确同步方案仍取决于应用。

锁可能造成死锁、优先级反转和争用；原子操作也可能遭遇逻辑竞态。不要在持锁期间执行未知时间的外部 IO；也不要以为换用 `RwLock` 就一定比 `Mutex` 快。实际选择应基于访问模式、粒度和测量。

## 评估同步方案时画三张图

第一张是所有者图：哪些任务持有 `Arc`，何时放弃共享所有权；第二张是锁时序图：每把锁在哪里获取和释放，是否存在不同顺序的嵌套；第三张是业务状态图：哪些状态转换必须原子执行。只有三张图一致，才谈得上“并发设计正确”。

尝试把计数场景改成 `AtomicU64::fetch_add`，再把业务变成“库存不足则拒绝扣减”。比较为什么一个简单原子递增与一个条件事务不是同一问题。

## 把锁的成本放进性能模型

1. 把 Mutex 计数器改为 AtomicU64，说明这能保证什么、不能保证什么。
2. 设计“检查库存且扣减库存”的单一临界区，避免先读后写之间被插入操作。
3. 在持锁时调用另一个可能再锁同一 Mutex 的函数，分析潜在死锁。

共享所有权和同步访问解决的是不同层次的事情。下一章转向另一种并发组织方式：通过通道传递消息，让资源的流动更容易看清。

**相关阅读**：[book](https://doc.rust-lang.org/book/); [ref](https://doc.rust-lang.org/reference/)。

---

[← 上一篇](25-send-and-sync.md) · [系列目录](../00-introduction.md) · [下一篇：为什么消息传递适合表达并发任务之间的协作？](27-message-passing.md)
