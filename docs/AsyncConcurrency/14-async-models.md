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

## 为什么要把“创建任务”与“开始执行”分开比较

前面一直使用 Python，所以我们容易默认：调用一个 `async def` 函数只获得协程对象，必须进一步 `await` 或交给 Task 才执行。现在换成 JavaScript，如果仍照搬这个假设，两个相似的 `fetch()` 看起来会产生完全相同的调度行为，但实际上并不是。

为了让比较可检验，我们把同一个网络场景压缩成两个延迟 100ms 的独立上游请求，统一问四个问题：**调用函数时是否已经运行？第一次等待时怎样让出执行权？谁在之后恢复？调用者怎样知道结果已经完成？**

JavaScript 的 `async` 函数被调用时，会同步执行函数体，直到遇到挂起点等条件，并返回 Promise；Promise 的后续反应依靠微任务队列等机制安排。Node.js 还有 libuv 事件循环及其底层 I/O 设施，所以这不意味着所有工作都只能由一条 OS 线程完成。

**最容易犯的错**是觉得 `Promise.all` 才“启动”了 `a` 和 `b`。在本文的示例里，两个 `fetch` 调用就已经开始执行，`Promise.all` 主要负责组合并等待它们的结果；这与 Python 直接创建两个协程对象的默认语义不同。

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

## 用运行轨迹直观看见两种语义差异

考虑下面这段跨语言对照（仅为行为示意，不是可运行的混合语言代码）：

```text
JavaScript: const a = fetch("A");
            → 通常立即打印 A started，随后返回 Promise

Python:     a = fetch("A")
            → 得到 coroutine，默认还没有打印 A started
```

这个差别会影响性能设计：在 Python 中如果连续写 `await fetch("A")`、`await fetch("B")`，B 往往等 A 完成才开始。要让两个独立请求的等待重叠，需要明确把两个协程交给 `gather`、`TaskGroup` 或 Task 调度。而在 JavaScript 中，即使随后才 `await Promise.all(...)`，前面独立调用的两个异步函数可能已经推进。

注意这里说的是常见默认调度，不是给所有版本的所有特殊模式下绝对不会变化的保证。Python 有 eager task 等优化选项，因此课程把最典型的路径作为第一条正常路径，特殊构造随后再讨论。

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

## 为什么看似相同的 Task 仍不等于一个线程

C# 的 async 方法通常先执行到第一个尚未完成的 `await`，并用编译器生成的状态机保留继续执行所需的状态。底层 I/O 的完成可能由操作系统的异步 I/O 接口通知，后续代码按照运行时及同步上下文规则被安排执行。这里 `Task` 更接近“可表示操作完成状态的对象”，不能推导出“一个 Task 占一个线程”。

更有价值的对照是：Python 的 Task 负责在 asyncio 事件循环推进协程；C# 的 Task、await 和执行上下文则服务于另一套运行时规则。它们都能够表达“先发起，之后等结果”，却并不是同一份运行时实现换了两种语法。

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

## 为什么 Rust 的 Future 看起来更像一份“等待执行的计划”

Rust 在语言层把异步操作表达为 Future。调用 `async fn` 通常得到 Future，但仅创建它一般不会推进其中的代码；需要执行器对它进行 `poll`，当不能继续时返回 `Pending`，并通过 Waker 让执行器在条件改变后重新调度。这个模型使“谁负责推进”成为显式的重要问题。

它与 Python 都存在“创建了异步对象但尚未真正执行”的阶段，但 Rust Future 的轮询与唤醒接口、所有权约束和常见的 executor 实现，与 Python asyncio 并不相同。

四种语言的故事到这里已经形成一个共同尺度：不再简单问“支不支持 async/await”，而是问**创建时有没有开始、由谁调度、等待点保存什么、结果怎样交付**。下一章引入 Go 和 Java 虚拟线程，是因为还有一种不同的设计目标：尽量让业务代码保持普通阻塞式写法，却不为每个等待任务独占 OS 线程。

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

## 把“谁启动谁”真正画成时间线

我们先把网络请求用两个计时等待模拟，不依赖任何远端服务器。对于同样的“调用两次 fetch，然后收集结果”，四种语言最值得观察的不同点并非关键字拼写，而是**调用瞬间到底发生了什么**。

```text
JavaScript
  const a = fetchA();     // 通常立即执行函数体直到挂起点，得到 Promise
  const b = fetchB();     // B 也开始推进
  await Promise.all([a,b]);

Python asyncio（默认常见行为）
  a = fetchA()            # 先得到协程对象，本身通常尚未推进
  b = fetchB()
  await asyncio.gather(a,b) # 由运行时安排两者共同执行

C#
  var a = FetchAAsync();  // 通常立即执行到首个未完成的 await，得到 Task
  var b = FetchBAsync();
  await Task.WhenAll(a,b);

Rust
  let a = fetch_a();      // 构造 Future，通常尚未被轮询
  let b = fetch_b();
  // 需要 executor / join! 等机制驱动 Future 共同推进
```

这里的 Rust 部分是**概念片段**，`join!` 不是 Rust 标准库通用函数；真实示例需选择具体运行时或 futures 库。C# 的状态机与继续执行的位置还受到同步上下文等因素影响，因此不能把它简化成“必定切到另一线程”。

一个常见的跨语言迁移事故是：开发者在 JS 中形成“调用 async 函数就启动任务”的直觉，转到 Python 后创建了两个协程对象却没有安排执行；或者在 Rust 中创建 Future 却以为它已经向网络发出请求。这样的 bug 不是“异步难”，而是没有区分**对象创建、任务注册、实际推进、结果完成**四个阶段。

因此写跨语言异步教程时，不应该用一张“关键字一览表”直接比较它们，而应像我们这里这样控制相同的业务动作和观测点。等读者能独立预测输出顺序，再去深入各自运行时源码，才能真正理解设计差异。

## 用同一把尺子再看四种语言

| 对比维度 | JavaScript | Python asyncio | C# | Rust |
|---|---|---|---|---|
| 创建后是否典型地开始推进 | async 调用会执行到挂起点 | 创建协程对象通常不会 | async 方法通常执行到首个未完成 await | 创建 Future 通常不会 |
| 未完成操作如何表示 | Promise | Coroutine / Task / Future | Task | Future |
| 谁继续推进 | 事件循环/微任务机制 | 事件循环 Task 调度 | 调度上下文、线程池及 I/O 完成机制 | Executor 轮询与 Waker |
| 是否自动创建 OS 线程 | 否 | 否 | 否 | 否 |

它们共享“把尚未完成的操作与后续代码连接起来”的目标，但具有不同的启动语义和调度约定。

## 为什么不应在比较语言时直接比谁写得短

前面统一控制了输入：两个独立的慢上游请求。你可能会发现 JavaScript 只需要 `Promise.all`，Python 可以用 `gather`，C# 有 `Task.WhenAll`，Rust 通过执行器与 join 机制组合 Future。哪个更好？仅看代码行数无法回答。

我们必须先对齐系统条件：是否使用真实网络 I/O；请求是否需要 DNS；有没有 TLS 握手；运行时是否把某些调用提交给线程池；是否有默认的并发限制；失败时另一个子任务如何处理；最终是否需要处理客户端断连。任意改变一项，就可能影响结果。

有两个尤其重要的对照：

**对照一：`await` 的执行位置。** 在 C# 等生态里，后续执行可能与同步上下文、线程池等调度政策有关；不要把 `await` 描述成“永远在同一个线程”或“永远创建新线程”。Python asyncio 默认的单线程事件循环模型又有自己的线程安全边界。

**对照二：操作的启动语义。** JavaScript 与 C# 的典型 async 调用会较早开始执行，而 Python 协程对象和 Rust Future 常常需要后续安排推进。一个跨语言应用如果把“创建对象”等同于“已经发送网络请求”，可能在迁移后得到完全不同的并发度。这种行为差异比语法细节更值得写进博客。

最后要提醒：语言的“异步接口”不规定 OS 必须使用 `epoll`。Windows 的完成式 I/O、Linux 的就绪通知与 `io_uring`、运行时线程池等，都可能成为具体实现的一部分。正确的研究方法是固定语言与运行时版本，再沿一次真正调用的路径核对，而不是凭 `async` 关键词猜测底层实现。

## 仍未解决的问题

为什么 JavaScript、Python、Rust 的很多异步代码需要显式 `await`，而 Go 直接写一个阻塞风格的网络读取，就可以让运行时调度数万个 goroutine？现代 Java 的虚拟线程又是怎么做到的？这是[第 15 章](./15-lightweight-threads.md)要回答的问题。

## 回到操作系统层，不要被相似语法欺骗

不管语言接口叫 Promise、Task 还是 Future，实际网络 I/O 仍需要某种底层机制支持等待、就绪或完成通知。Node.js 的网络 I/O、Python 的 Selector 事件循环、.NET 的完成通知方案、Rust 的不同异步运行时可以走不同具体路径。不能把所有 `await` 都等同于“调用 epoll”，也不能把“有 await”理解成“不使用线程”。

这一章的成果是一把可复用的比较尺子；它没有宣称哪种语言更快。下章我们刻意换一个设计方向，看能不能把**等待的复杂性藏在运行时调度器内部**。
