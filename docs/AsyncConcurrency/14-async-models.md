---
title: "异步与高并发 14：同样写 async/await，为什么不同语言行为不一样？"
sidebar_label: "十四、同样的 async/await 为何不一样"
sidebar_position: 15
description: "以任务创建、启动、暂停和恢复比较 JS、Python、C# 与 Rust。"
slug: /async-concurrency/14-async-models
---

# 同样写 `async/await`，为什么不同语言行为不一样？

前面我们自己经历了从 `recv()` 到事件循环、协程的演化。现在换个角度：如果同样要访问两个慢上游，不同语言为什么写法相似，运行语义却存在区别？

先不要背 API。我们只追问四件事：**创建任务时是否开始执行？遇到等待谁暂停？由谁恢复？结果如何交付？**

## JavaScript：Promise 和事件循环

在 JavaScript 中，调用 `async` 函数会立即开始执行函数体，直到遇到 `await` 等暂停点；调用会返回 Promise。下面程序可保存为 `.mjs`，使用 Node.js 执行。

```javascript title="实验 14A｜Node.js：两个计时等待共同推进"
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

async function fetch(name) {
  console.log('start', name);
  await sleep(100);
  console.log('end', name);
  return name;
}

async function main() {
  const start = performance.now();
  const a = fetch('A'); // 函数体立即运行到第一次 await
  const b = fetch('B');
  console.log(await Promise.all([a, b]));
  console.log('ms:', Math.round(performance.now() - start));
}

main().catch(console.error);
```

这通常约 100ms 加少量调度开销，而不是 200ms。**Promise 不是线程**。Node.js 用 libuv 提供事件循环和多种底层 I/O 支持；其某些文件系统等操作还可能使用线程池。不能因为 JavaScript 回调通常在主 JS 线程执行，就断言整个 Node.js 进程没有其他线程。

历史上 Node.js 在 2009 年开始普及事件驱动服务器写法。其事件循环阶段与 Node 版本有关，需对照 [Node.js Event Loop 官方说明](https://nodejs.org/en/learn/asynchronous-work/event-loop-timers-and-nexttick)。

## Python：协程对象不等于已经启动的 Task

相比之下，调用 Python 的 `async def` 函数会先得到一个**协程对象**。默认情况下，仅这样创建并不会运行函数体；之后必须由 `await`、`create_task` 等驱动它。

```python title="实验 14B｜Python：Task 才安排执行"
import asyncio

async def fetch(name):
    print("started", name)
    await asyncio.sleep(0.1)
    return name

async def main():
    coro = fetch("A")
    print("created coroutine; body has not run")
    task = asyncio.create_task(coro)
    print("task scheduled")
    print("result:", await task)

asyncio.run(main())
```

常见输出顺序为 `created... → task scheduled → started A → result A`。这不是所有版本、所有自定义 Task 工厂的唯一执行时机；Python 也存在 eager task 等特殊模式。但它准确展示了默认 `asyncio.create_task` 的常见语义。

这解释了为什么两个语言即使都写 `await fetch()`，也不能直接照搬“调用时已经启动”的假设。

## C#：Task 代表已开始的异步操作

C# 的 `async` 方法执行时，通常会从方法开头执行到第一个未完成的 `await`，返回 Task 给调用方；后续通过编译器生成的异步状态机恢复执行。

```csharp title="实验 14C｜.NET Console：Task.WhenAll（需 .NET SDK）"
using System;
using System.Threading.Tasks;

Task<int> a = FetchAsync(1);
Task<int> b = FetchAsync(2);
int[] results = await Task.WhenAll(a, b);
Console.WriteLine(string.Join(", ", results));

static async Task<int> FetchAsync(int number)
{
    Console.WriteLine($"start {number}");
    await Task.Delay(100);
    Console.WriteLine($"end {number}");
    return number * 10;
}
```

C# `Task` 不等于一个线程，也不是 `await` 自动创建线程。某些异步 I/O 通过 OS 完成通知推进，某些工作可能由线程池执行。这一层必须看具体 API 的实现。官方参考：[C# Async/Await](https://learn.microsoft.com/en-us/dotnet/csharp/asynchronous-programming/)。

## Rust：Future 等待执行器轮询

Rust 的 `async fn` 通常返回一个尚未被执行器推进的 `Future`。Future 的核心接口是 `poll`：返回 `Ready(value)` 表示完成，返回 `Pending` 表示暂时不能继续，并需要安排适当时机通知唤醒。

```rust title="概念代码：演示 Future 的创建，非完整运行程序"
async fn fetch() -> u32 {
    42
}

// 此时只得到一个 Future；尚未驱动它完成。
let future = fetch();
// 需要合适的 executor，或在其他 async 上下文中 await 它。
```

上面的 `let` 行是**片段而非可独立编译的完整程序**。Rust 标准语言机制提供 Future/`async`，实际网络 I/O 和任务执行常依赖 Tokio、async-std 等生态运行时。不要把 Rust 的 `Future` 当作 Python `Task` 的同义词。官方参考：[Rust Async Book: Future trait](https://rust-lang.github.io/async-book/02_execution/02_future.html)。

## 用同一把尺子再看四种语言

| 对比维度 | JavaScript | Python asyncio | C# | Rust |
|---|---|---|---|---|
| 创建后是否典型地开始推进 | async 调用会执行到挂起点 | 创建协程对象通常不会 | async 方法通常执行到首个未完成 await | 创建 Future 通常不会 |
| 未完成操作如何表示 | Promise | Coroutine / Task / Future | Task | Future |
| 谁继续推进 | 事件循环/微任务机制 | 事件循环 Task 调度 | 调度上下文、线程池及 I/O 完成机制 | Executor 轮询与 Waker |
| 是否自动创建 OS 线程 | 否 | 否 | 否 | 否 |

它们共享“把尚未完成的操作与后续代码连接起来”的目标，但具有不同的启动语义和调度约定。

## 仍未解决的问题

为什么 JavaScript、Python、Rust 的很多异步代码需要显式 `await`，而 Go 直接写一个阻塞风格的网络读取，就可以让运行时调度数万个 goroutine？现代 Java 的虚拟线程又是怎么做到的？这是[第 15 章](./15-lightweight-threads.md)要回答的问题。
