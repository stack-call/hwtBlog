---
title: "异步与高并发 15：Go 和 Java 虚拟线程，为什么仍能写顺序代码？"
sidebar_label: "十五、Go 与虚拟线程为何还能顺序写"
sidebar_position: 16
description: "比较运行时调度的轻量执行单元与事件循环协程。"
slug: /async-concurrency/15-lightweight-threads
---

# Go 和 Java 虚拟线程，为什么仍能写顺序代码？

我们已经能通过事件循环和 `await` 让等待中的任务暂停。但这带来一个开发体验问题：业务代码必须显式标记异步边界、传播异步接口。有没有另一种方案，让程序员继续写普通的等待式代码，而把大量任务的调度交给语言运行时？

Go 的 goroutine 与 Java 虚拟线程提供了值得对比的答案。它们不是同一实现，但共同展示了**用户级执行单元不必与 OS 线程一一对应**。

## Go：把一个请求放进一个 goroutine

```go title="实验 15A｜Go：多个等待任务（Go 1.x）"
package main

import (
    "fmt"
    "sync"
    "time"
)

func fetch(id int, wg *sync.WaitGroup) {
    defer wg.Done()
    fmt.Println("begin", id)
    time.Sleep(100 * time.Millisecond) // 模拟等待上游
    fmt.Println("end", id)
}

func main() {
    var wg sync.WaitGroup
    for i := 0; i < 5; i++ {
        wg.Add(1)
        go fetch(i, &wg)
    }
    wg.Wait()
}
```

不需要在每个等待处写 `await`。`go fetch(...)` 创建 goroutine，由 Go 运行时安排执行。打印顺序不确定，但所有任务都能完成。

如果顺着 Go 运行时官方的 [runtime/HACKING](https://go.dev/src/runtime/HACKING) 读，会遇到 G、M、P：

- G：goroutine 的运行时表示。
- M：承载执行的操作系统线程。
- P：运行 Go 代码所需的调度资源。

这是对真实运行时设计的**术语解释**，不是内核数据结构。Go 在网络 I/O 场景下还有运行时网络轮询器，能把部分阻塞风格的网络 API 和 OS 的就绪机制衔接起来。不能把所有 `time.Sleep`、cgo、系统调用和普通网络读取混成同一种运行路径。

## Java：把请求交给一个虚拟线程

Java 21 标准化的虚拟线程也让程序员用熟悉的线程式代码表达很多等待任务。

```java title="实验 15B｜Java 21+：每任务一个虚拟线程"
import java.util.concurrent.Executors;

public class Main {
    public static void main(String[] args) throws Exception {
        try (var executor = Executors.newVirtualThreadPerTaskExecutor()) {
            for (int i = 0; i < 5; i++) {
                final int id = i;
                executor.submit(() -> {
                    System.out.println("begin " + id);
                    Thread.sleep(100); // 这里只做可控制的等待实验
                    System.out.println("end " + id);
                    return null;
                });
            }
        } // 关闭 ExecutorService，会等待已提交任务终止
    }
}
```

这里 `Thread.sleep` 体现顺序风格。实际 Java 虚拟线程执行支持的阻塞 I/O 时，Java 运行时可挂起虚拟线程，并释放其承载的平台线程去执行其他任务；具体行为受 API、版本和某些不可卸载场景影响。

根据 [Oracle Java 21 Virtual Threads 文档](https://docs.oracle.com/en/java/javase/21/core/virtual-threads.html)，它们旨在改善大量 I/O 等待任务的扩展性，**不是让单个计算任务运行得更快**。JEP 444 也是一个固定的设计参考：[JEP 444](https://openjdk.org/jeps/444)。

## 它们与线程池有什么不同？

普通 OS 线程池的每一个工作线程通常在阻塞式 I/O 调用里被占用，直到调用返回。运行时管理的轻量执行单元在特定可挂起的等待点，可以让承载它的 OS 线程去执行别的任务。这样，大量等待任务不必都持有独占的 OS 线程。

```text
传统线程池：一个正在阻塞的任务 ↔ 一个被占用的工作线程
轻量执行单元：很多可暂停任务 → 运行时调度 → 少量/适量 OS 线程
```

这张图是一种典型场景的抽象，并非所有系统调用、native 代码或 CPU 密集工作都能自动卸载。语言运行时最终仍要服从 OS 调度，不能绕过 CPU 核心数量限制。

## 与第 14 章的 `async/await` 做一次真正的比较

| 设计问题 | 事件循环/显式 async | Go goroutine | Java 虚拟线程 |
|---|---|---|---|
| 如何书写等待 | `await` 等显式暂停边界 | 看起来像同步调用 | 看起来像同步调用 |
| 由谁保管暂停后的执行状态 | 协程/Future、执行器 | Go Runtime | JVM |
| 是否必须一任务一 OS 线程 | 否 | 否 | 否 |
| CPU 密集时是否自动多核加速 | 否，需相应执行策略 | 可利用多个 OS 线程/核心 | 可利用平台线程/核心，受实际调度与负载限制 |

哪一个更好？要根据生态、调试体验、阻塞 API 兼容性、内存占用、延迟与吞吐测量。不存在“goroutine 完全取代协程”或“虚拟线程从此无需背压”的普遍结论。

我们现在已经能把“任务如何表示”与“OS 线程如何执行”分开。最后一章把它们全部带回最初的数据查询服务：[一个真正端到端的并发请求究竟怎样流动？](./16-system-design.md)
