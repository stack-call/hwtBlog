---
title: "第 61 章｜为什么有些并发系统倾向于传递消息？"
sidebar_position: 61
description: "从订单系统的一次具体失败出发，理解为什么有些并发系统倾向于传递消息？，以及由此带来的语法、运行行为与设计取舍。"
slug: /programming-languages/61-message-passing
---

# 第 61 章：为什么有些并发系统倾向于传递消息？

工作线程直接改共享订单列表，需要锁。后来各工作线程只负责计算自己的订单，再将结果交给一个汇总者，我们发现可以少共享可变数据。

## 直接共享一个容器在简单任务里可行

直接共享一个容器在简单任务里可行，锁也能正确保护它。但工作者实际上只需要把结果交出去，未必需要持有容器的修改权限。

**教学实现：**

```python
totals = []
# 多个线程都直接 append 到共享列表
# 需要考虑对列表的访问边界与同步语义
```

## 过多组件共享同一个可变状态

过多组件共享同一个可变状态，使得影响范围超出当前任务。通过消息交接结果，可以缩小状态所有者的数量。

**用于触发限制的代码／输入（实验场景，不伪称来自框架）：**

```text
工作者 A、B、C 都能修改相同订单状态。
结果是谁先写、谁覆盖、失败后谁负责重试，难以追踪。
```

**因此需要的能力。** 采用消息传递：工作者产出不可变或受控的数据消息，由消费者集中汇总。队列提供生产者和消费者之间的边界。

## 把新机制落实成代码

**改进后的教学实现：**

```python
from queue import Queue
q = Queue()
for amount in [100, 250, 300]:
    q.put(amount)
total = 0
while not q.empty():  # 单线程教学场景
    total += q.get()
print(total)  # 650
```

这个单线程示例先演示消息“发送—接收—处理”的协议。真正并发程序不能依赖 `empty()` 作为无竞态的结束判断；需要固定消息数量、完成标记或队列 join 机制。

## 生产者依次放入 100、250、300

生产者依次放入 100、250、300；消费者三次 get，每次只有自己修改 total；队列耗尽后得到 650。共享范围从整个总额状态缩小到受控消息边界。

### 为什么 Channel 运行时需要等待队列？

若接收方在消息到达前就请求读取，它可能需要等待；仅有一个存数据的容器还不够。Go 1.24.5 在 [`src/runtime/chan.go`](https://github.com/golang/go/blob/go1.24.5/src/runtime/chan.go) 中定义了等待队列：

**真实源码节选 · Go go1.24.5 · `src/runtime/chan.go`：**

```go
type waitq struct {
    first *sudog
    last  *sudog
}
```

`first`、`last` 是等待节点队列的两端。这个具体数据结构说明了 channel 不只是语法上的 `<-`：运行时需要记录尚未完成的发送或接收。它没有单独解释所有同步和调度语义；沿 `sendq`、`recvq` 的使用位置继续追踪，才能建立完整的执行路径。

## Go channel 为 goroutine 间的发送和接收提供语

Go channel 为 goroutine 间的发送和接收提供语言级操作。

**Go 示例（教学代码，非所引用仓库的源码节选）：**

```go
ch := make(chan int64, 3)
ch <- 100; ch <- 250; ch <- 300
close(ch)
var total int64
for x := range ch { total += x }
```

channel 可以提供同步与数据交接，但不意味着通过 channel 传递指针后双方就绝不共享对象。消息内容的所有权仍需设计。

## 不共享列表，能不能交换工作结果？

订单工作线程各自处理一批数据，如果都向同一个全局列表追加结果，需要考虑同步与完成时机。另一种思路是让工作者通过消息通道把结果交给单独的聚合者。

```go
package main
import "fmt"
func main() {
    results := make(chan int, 2)
    go func() { results <- 100 }()
    go func() { results <- 250 }()
    total := <-results + <-results
    fmt.Println(total) // 350
}
```

这里容量 2 的 channel 可以暂存两条消息，但它不会帮你检查业务是否存在重复订单，也不会自动确保数据库提交只执行一次。真实系统需要定义消息顺序、结束信号、错误传播与取消。

Go channel 的发送/接收具有特定同步语义；Rust 的标准或生态消息队列、Java BlockingQueue、Python asyncio.Queue 都提供类似的协作手段，但容量、关闭、取消和内存保证不完全一样。

消息传递把共享可变状态缩小到更少位置，却并非抹掉所有共享：例如消费者仍可能共同持有缓存或数据库连接。

## 对照规范和真实实现

**源码/规范阅读路线（供读者核对，并非声称已经逐行验证）：** [Go 1.24.5 `src/runtime/chan.go`](https://github.com/golang/go/blob/go1.24.5/src/runtime/chan.go)。

已核对固定版本中 `hchan`、发送/接收等待队列等实现入口。先沿一次成功 send/recv 路径观察队列和唤醒，再考虑阻塞、关闭与 select。

## 实验：把 Python 示例改为两个生产线程、一个消费线

把 Python 示例改为两个生产线程、一个消费线程，用每个生产者发送结束标记；解释为什么 `q.empty()` 不够可靠。

**应当观察到（自行运行前属于预期）：** 消费者收到约定数量的结束标记后可确定所有生产者完成；只观察当前队列为空并不能判断未来是否还会有新消息。

## 边界：消息传递减少直接共享状态的范围

消息传递减少直接共享状态的范围，但可能带来拷贝、排队延迟、背压和循环依赖。没有任何一种并发原语可以自动消除业务死锁。

**跨语言迁移时，** Go channel、Rust mpsc、Java BlockingQueue、Python queue/asyncio.Queue、C/C++ 并发队列、Zig channel-like 抽象都能表达生产消费模式。

**下一层问题：** 为什么等待 IO 不一定需要占用一个线程？

[上一章：为什么不同线程看到的内存变化可能不同？](60-memory-model.md) · [下一章：为什么等待 IO 不一定需要占用一个线程？](62-async-io-event-loop.md)
