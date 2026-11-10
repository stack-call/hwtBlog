---
slug: /message-queue/ordered-consumption
title: 同一个订单的消息进入了同一个队列，为什么还是乱序了？
sidebar_position: 9
description: 继续追踪订单 1001 的创建与支付事件，从并发领取造成的乱序出发，实现队头阻塞与失败重试，并深入 RocketMQ 5.5.1 的 ConsumeMessageOrderlyService、ProcessQueue 顺序消费源码。
tags: [消息队列, RocketMQ, 源码精读, 顺序消息, 顺序消费, ProcessQueue]
---

# 同一个订单的消息进入了同一个队列，为什么还是乱序了？

上一章，我们给消息系统增加了逻辑队列。订单 `1001` 的消息不再和所有订单竞争同一份领取状态，而是按照 `orderId` 进入确定的队列。读者已经看到：不同队列可以分别分配、分别领取，索引还可以让消费者直接定位消息正文。

现在，同一个订单又产生了第二条消息。

```json title="订单 1001 的两条消息（教学数据）"
{"messageId":"msg-1001","eventType":"OrderCreated","orderId":1001}
{"messageId":"msg-1001-paid","eventType":"OrderPaid","orderId":1001}
```

`OrderCreated` 必须先于 `OrderPaid` 被业务处理：如果下游还没有创建订单投影，就先收到「订单已支付」，它可能找不到记录，也可能把状态错误地写入数据库。

这两条消息恰好在同一个队列中：

```text
queue-1
   offset 0  ->  msg-1001       OrderCreated
   offset 1  ->  msg-1001-paid  OrderPaid
```

**看起来已经排好队了。为什么它们还会乱序？**

这一章先不讲顺序消息的全部知识。我们只解决一个问题：**前一条消息还没处理成功时，怎样避免后一条消息先被当作完成？** 然后沿着这个问题，核对 RocketMQ 真实的顺序消费实现。

<!-- truncate -->

## 一次没有违反存储顺序的乱序

先回到上一章教学代码 `partitioned_message_queue.py` 的 `claim()`。

它已经具备我们之前一步步建立的能力：消息正文可持久化，有索引，领取动作有原子性，一条消息领取后会暂时不可见，ACK 必须带有效领取凭证。

其中，决定「下一条消息是谁」的是这段查询。

```python title="partitioned_message_queue.py（第 07 章真实教学源码节选）"
row = conn.execute(
    """SELECT seq FROM deliveries
       WHERE acked=0 AND invisible_until<=?
       ORDER BY seq LIMIT 1""", (now,)
).fetchone()
```

它先排除已经 ACK 的消息，再从**当前可见**的消息里挑选最小 `seq`。

注意这个细节：`invisible_until<=now` 是在 `ORDER BY seq` **之前**生效的。

这在普通任务队列里很合理。消费者 A 已经领走一条通知，消费者 B 就可以做另一条，避免闲着。但在我们新增的订单事件场景里，问题出现了。

设现在是 `t=100`，消息的领取过程如下：

```text
时间      消费者 A                        消费者 B
-----     -----------------------        -----------------------
t=100     领取 offset 0（创建订单）
          offset 0 暂时不可见

t=100                                    再次请求可见消息
                                         跳过 offset 0
                                         领取 offset 1（支付订单）

t=101                                    处理 offset 1、ACK

t=102     处理 offset 0、ACK
```

**消息写入队列的顺序没有错，消息完成处理的顺序却反了。**

这里要区分三个不同的事实：

- **写入顺序**：队列里 `offset 0` 在 `offset 1` 之前。
- **领取顺序**：A 先拿到 `offset 0`，B 后拿到 `offset 1`。
- **业务生效顺序**：B 的业务先完成并 ACK，A 的业务后完成。

前两项按序并不能推出第三项按序。即使我们没有更改持久化、索引或路由，只是沿用之前的**并发领取策略**，最终业务效果仍然可能乱序。

> **最小反例：** 已经领走但尚未 ACK 的队头消息，被后面的可见消息「越过」了。

我们真正缺少的不是「再把两条消息放进同一个队列」，因为这件事已经做到了；而是一个新的约束：**只要队头还没有完成，后续消息就不能抢先完成。**

## 我们只改动一处选择规则

先不要引入分布式锁、消费组重平衡等复杂概念。对于我们的教学队列，一个最直接的方案是：

**先找队列里最早的未确认消息，再判断它能不能领取。**

如果最早的那条还处于不可见时间，就返回「暂时没有可领取的消息」，而不是寻找下一条。

新文件 `ordered_message_queue.py` 继承上一章的 `PartitionedQueue`，消息正文、索引、队列路由、ACK 与领取凭证全部复用，只替换领取时的选择规则：

```python title="ordered_message_queue.py（本章可运行教学源码节选）"
row = conn.execute(
    """SELECT seq, invisible_until FROM deliveries
       WHERE acked=0 ORDER BY seq LIMIT 1"""
).fetchone()

if row is None:
    conn.commit()
    return None

seq, invisible_until = row
if invisible_until > current:
    conn.commit()
    return None
```

对比之前的查询，你会发现我们没有增加一个全新的消息存储结构，也没有推翻 ACK。只把原来的：

```text
在所有「已可见」的未完成消息中，选择最早一条
```

改成：

```text
先选「最早的未完成消息」
          |
          v
它现在可见吗？
   /             \
 可以             不可以
  |                 |
领取它           暂时不领取任何消息
```

这个小变化足以消除刚才的反例：

1. A 领取 `offset 0`，它的 `invisible_until` 被设置到未来。
2. B 查询最早未确认消息，查到的仍然是 `offset 0`。
3. B 发现它尚未可见，只能等待，不能转而领取 `offset 1`。
4. A 处理成功并 ACK 后，`offset 0` 被标记为完成。
5. B 再次查询，才能拿到 `offset 1`。

在这条正常路径上，我们已经实现了一个**队列范围的处理顺序约束**。它用的是上一章就有的队列级 SQLite 写事务，因而两个消费者同时查询队头时，仍然只有一个能够成功领取，并不会因新增顺序约束而退回到重复领取的旧问题。

不过，一个新限制立刻出现了。

## 如果第一条处理失败，第二条要等多久？

假设 `OrderCreated` 的下游数据库暂时不可用。消费者 A 取到了第一条消息，但业务处理失败了。

此时我们不能 ACK；否则下一条 `OrderPaid` 会立即被领取，前面的消息却被当成已经成功处理。

直接放弃不 ACK 虽然不会破坏我们刚建立的顺序约束，但必须等领取时设置的不可见时间自然到期。更合理的办法是让消费者明确告诉队列：**这次失败了，过一段时间重新尝试当前消息。**

由于此前已经设计了领取凭证，失败通知也应该检查这个凭证。否则一个已经过期的旧消费者，可能覆盖另一个消费者刚刚建立的领取状态。

这就是 `retry_later()`：

```python title="ordered_message_queue.py（本章可运行教学源码节选）"
changed = conn.execute(
    """UPDATE deliveries
       SET receipt_token=NULL, invisible_until=?
       WHERE seq=? AND acked=0 AND receipt_token=? AND invisible_until>?""",
    (current + delay_seconds, receipt.seq, receipt.token, current),
).rowcount
```

这几行并不是简单地「睡五秒」。它把**队头下一次可领取的时刻**写入共享状态，同时使原来的领取凭证失效。其他消费者可以轮询，但在截止时间之前都不能绕过队头。

沿着刚才的失败过程追踪一次：

```text
queue-1:
   offset 0 = OrderCreated    未 ACK
   offset 1 = OrderPaid       未 ACK

A 领取 offset 0
        |
        v
订单创建处理失败
        |
        v
retry_later(delay=5s)
        |
        v
offset 0 暂时不可领取
        |
        +----> B 请求：返回 None，不能领取 offset 1
        |
   5 秒后（示例时钟）
        |
        v
B 重新领取 offset 0
        |
        v
处理成功并 ACK
        |
        v
B 才能领取 offset 1
```

现在我们已经得到了本章需要的最小机制：**失败不会使后续消息越过队头；处理成功之后才推进可以消费的位置。**

这仍然只是教学实现。RocketMQ 的顺序消费实现有自己的客户端缓存、消费状态回调、进度管理与队列锁。接下来不先猜这些组件的职责，而是问一个具体问题：一个成熟的消息客户端，怎样确保同一队列的不同消费任务不会同时跑进业务回调？

## 到 RocketMQ 5.5.1 源码里寻找对应动作

本系列继续固定 Apache RocketMQ **5.5.1**，Commit：[`efa1be59d6fc747ad220f036c86030f05f0419f9`](https://github.com/apache/rocketmq/commit/efa1be59d6fc747ad220f036c86030f05f0419f9)。

本章只追踪仓库 `client` 模块中**经典 `DefaultMQPushConsumer` 的 `MessageListenerOrderly` 消费路径**，而不是把 5.x 所有 FIFO 消费模式合并为同一种实现。所有标为「真实源码节选」的 Java 片段均已在该 Commit 的相应文件核对；本章 Python 代码则是教学化简。

### 第一道约束：同一队列的消费任务不能互相穿插

入口是 [`ConsumeMessageOrderlyService.java` 的 `ConsumeRequest.run()`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/client/src/main/java/org/apache/rocketmq/client/impl/consumer/ConsumeMessageOrderlyService.java)。

一个 `ConsumeRequest` 对应一个待调度的逻辑队列及其本地 `ProcessQueue`。多个消费任务可以被放入线程池，但对同一 `MessageQueue`，`run()` 会先获取关联的本地锁对象：

```java title="ConsumeMessageOrderlyService.java / ConsumeRequest.run()（真实源码节选）"
final Object objLock = messageQueueLock.fetchLockObject(this.messageQueue);
synchronized (objLock) {
```

这层锁**不是用来禁止整个消费者处理其他队列**，而是让同一队列的消费任务进入一段受保护的执行区域。

回到订单 `1001`：如果两个工作线程都提交了 `queue-1` 的消费任务，它们不能各自直接进入业务回调，让一个线程执行 `OrderCreated`、另一个线程同时执行 `OrderPaid`。

但这里还有跨进程的问题。上一章已经讨论过，一个队列可能因消费组成员变化而重新分配。只靠 JVM 里的 `synchronized`，无法阻止另一台机器上的消费者同时处理同一队列。

在这条经典**集群顺序消费**路径里，`ConsumeMessageOrderlyService.start()` 会定期触发 `lockMQPeriodically()`；后者委托 Rebalance 组件向 Broker 协调队列锁。执行消费任务时，代码还检查 `ProcessQueue` 的 `isLocked()` 和 `isLockExpired()` 状态；锁不可用或过期时，延后执行。

这个设计区分了两个层面：

```text
进程内：同一个 MessageQueue 的消费任务使用本地锁串行化

进程间：经典集群顺序消费还需要协调当前队列的消费锁
```

这些锁并不等同于我们的 `receipt_token`。前者是在控制**谁能推进队列级消费路径**，后者是上一章教学程序里针对**一次消息领取**的有效凭证。职责相近的部分可以类比，但绝不能在源码层面画等号。

### 第二道约束：按队列位置取消息，而不是随意跳过失败项

任务能够进入队列内的临界区之后，下一步是从 `ProcessQueue` 中取得消息。

[`ProcessQueue.java / takeMessages()`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/client/src/main/java/org/apache/rocketmq/client/impl/consumer/ProcessQueue.java) 的关键动作是：

```java title="ProcessQueue.java / takeMessages()（真实源码节选）"
Map.Entry<Long, MessageExt> entry = this.msgTreeMap.pollFirstEntry();
```

为什么是 `pollFirstEntry()`？因为 `msgTreeMap` 按队列位移组织待处理消息。顺序消费从较小的位移开始取，取出的消息放进 `consumingMsgOrderlyTreeMap`，暂时记录为「正在处理」。

这里读者可能会问：取出来以后，不就从 `msgTreeMap` 消失了吗？如果业务失败，怎么再处理？

这就需要看消息处理结果，而不能只看「取消息」这一小段。

### 第三道约束：成功才能推进，失败必须回来

`ConsumeRequest.run()` 调用业务监听器 `messageListener.consumeMessage(...)` 得到一个 `ConsumeOrderlyStatus`，再把这个结果传给 [`ConsumeMessageOrderlyService.processConsumeResult()`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/client/src/main/java/org/apache/rocketmq/client/impl/consumer/ConsumeMessageOrderlyService.java)。

在该路径默认的自动提交分支中：

- **`SUCCESS`**：调用 `ProcessQueue.commit()`，得到可以推进的消费位移，再交给消费位移存储逻辑更新。
- **`SUSPEND_CURRENT_QUEUE_A_MOMENT`**：当本次仍应在当前队列重试时，通过 `ProcessQueue.makeMessageToConsumeAgain(msgs)` 将消息放回待处理集合，并通过 `submitConsumeRequestLater(...)` 安排稍后再次消费。

后者正是我们在教学实验中「前一条没成功，不能跳到后一条」的真实对应机制。**它不是把失败消息随意扔进另一个普通并发队列，然后马上处理后面的消息。**

再回到 [`ProcessQueue.java`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/client/src/main/java/org/apache/rocketmq/client/impl/consumer/ProcessQueue.java)。`makeMessageToConsumeAgain()` 把正在处理的消息从 `consumingMsgOrderlyTreeMap` 移回 `msgTreeMap`；`commit()` 则清理已完成的消费集合，返回可提交的下一消费位置。

把我们已经理解的操作串起来，才形成了真正有意义的执行链：

```text
ConsumeRequest.run()
        |
        v
按 MessageQueue 获取本地执行锁
        |
        v
检查队列锁状态（经典集群分支）
        |
        v
ProcessQueue.takeMessages()
        |
        v
MessageListenerOrderly.consumeMessage(...)
        |
        v
processConsumeResult(status)
          /                \
       SUCCESS         SUSPEND...
         |                 |
   ProcessQueue.commit   makeMessageToConsumeAgain
         |                 |
   updateOffset(...)    submitConsumeRequestLater(...)
         |                 |
    继续后面消息         暂停当前队列，稍后重试
```

这里还要强调两个源码边界：

第一，经典顺序消费支持**批量获取消息**，不是每一条消息都独立 ACK。我们 Python 教学程序逐条 ACK，是为了把「队头未完成时不能越过」展示得最清楚，不能据此推断 `ConsumeMessageOrderlyService` 内部也使用逐消息 `receipt_token`。

第二，失败处理存在**重试上限和消息回退/死信等分支**。上面的图只描绘普通失败后仍应在原队列重试的路径，并不声称失败消息永远会卡住同一队列。特别是不同客户端和不同版本的策略，不能直接套用这一张图。

## 现在运行一遍：旧方法会越队头，新方法不会

把本章资料包里的三个文件放在同一目录：

```text
partitioned_message_queue.py       # 第 07 章原始教学实现，作为依赖
ordered_message_queue.py           # 本章新增的顺序领取与失败重试
test_ordered_message_queue.py       # 自动化实验
```

要求 Python 3.9 或更高版本，全部只用标准库，无需 API Key、Broker 或外部数据库服务。

```bash
python ordered_message_queue.py demo
python -m unittest -v test_ordered_message_queue.py
```

`demo` 的实际输出如下：

```text
写入顺序: offset 0=OrderCreated, offset 1=OrderPaid (均在 queue-1)
旧领取顺序: A=OrderCreated、B=OrderPaid
旧完成顺序: B=OrderPaid -> A=OrderCreated (产生乱序风险)
新方案：创建消息未确认时，另一个领取者拿不到支付消息
新完成顺序: OrderCreated (重试成功) -> OrderPaid
本章结论：同队列可以顺序提交进度，但不能替代业务幂等或原子事务
```

请注意，这里用可控的 `now=100、101、...` 代表实验时钟，**不需要真实等待五秒**。这样实验可以稳定地重复运行，但它不代表已对真实网络延迟或 Broker 计时机制做过压测。

自动化测试已在当前 Python 环境实际运行：

```text
Ran 14 tests in 0.793s

OK
```

其中包括：上一章的普通领取会跳过正在处理的队头；新方案不会；旧凭证不能确认新一轮领取；超时之后先重领旧消息；重启对象后确认状态仍在；不同队列可以独立继续；双线程竞争同一队头时只有一个领取成功。

运行通过证明的是**这份 Python 教学模型在这些输入下的行为**，不是 RocketMQ 5.5.1 的集群顺序保证已经通过端到端实测。RocketMQ 源码部分是针对固定 Commit 的静态阅读和路径分析。

## 顺序消费究竟保证了什么，没保证什么？

现在我们可以更准确地描述本章成果：

**当同一队列内的前一条消息还没有被确认为完成时，不允许后一条消息越过它推进处理。**

但这个机制并不意味着「所有业务都自动严格有序」。至少还有几个边界不能混淆。

**写入顺序本身必须正确。** 如果生产者 A 和 B 并发发送两条订单事件，第二个事件先到 Broker，那么消费者再严格按队列顺序处理，也只能遵循已经形成的存储顺序。消费者不能倒推出业务真正发生的时间先后。

**提交顺序不等于外部副作用一定按序完成。** 如果业务回调内部把通知交给另一个异步线程然后立即返回 `SUCCESS`，即使队列位移依次提交，异步线程仍可能先完成后面的通知。业务层必须在正确时刻反馈处理结果。

**顺序消费不等于精确一次。** 处理了 `OrderCreated`，但还没有成功提交消费进度时，故障恢复后它仍可能再次出现。第四章已经用数据库唯一约束与事务验证过业务幂等的重要性，本章的顺序机制没有替代它。

**教学接口必须被正确使用。** `OrderedPartitionedQueue` 为了复用上一章的代码，仍然保留了父类 `claim()`；调用旧入口依旧可能跳过未确认的队头。本章只有 `claim_orderly()` 执行顺序约束。成熟系统需要从消息类型、消费模式和客户端入口共同限制这种绕过。

**教学中的“一个队列所有消息互相等待”不是 RocketMQ 5.x 所有 FIFO 模式的统一限制。** RocketMQ 官方 5.x 的顺序消息使用 `MessageGroup` 描述需要保持先后关系的一组消息，不同消息组可以存在独立的处理进度。官方文档还说明：顺序消息重试时，后续同组消息需要等待前面的消息完成。本章为了理解经典队列级路径，刻意先使用更简单的**整队队头约束**；不能把它冒充成所有 5.x `MessageGroup` / POP 消费路径的真实源码。参见 [Apache RocketMQ 官方顺序消息文档](https://rocketmq.apache.org/docs/featureBehavior/03fifomessage/) 与 [消费者负载均衡文档](https://rocketmq.apache.org/docs/featureBehavior/08consumerloadbalance/)。

还有上一章遗留的存储边界：Python 的消息正文、索引与 SQLite 状态属于不同文件，不能因为我们把每一次写入都同步到磁盘，就认为这些文件构成了一个原子事务。这与本章的顺序保证是两种不同问题。

## 回看这一步为什么不可省略

我们一路追踪的并不是抽象的「保证顺序」四个字，而是订单 `1001` 的两条消息：

```text
第 07 章：同一个 orderId -> 同一个 MessageQueue
                               |
                               v
                          写入顺序正确
                               |
                        但能并发领取
                         /          \
              OrderCreated        OrderPaid
                未完成              先完成
                         \          /
                          业务状态乱序
                               |
                               v
第 08 章：先找最早未确认消息
                               |
                          未完成就不越过
                               |
                      失败则等待并重新处理
                               |
                               v
                       前一条成功后再推进
```

因此这一步带来了明确的收益，也带来了新的成本：**消息可以按序完成，但一个较慢或反复失败的队头，会让排在后面的消息一起等待。**

这正好成为下一章的真实问题。

## 下一章：一个订单卡住了，为什么其他订单也跟着等待？

现在 `queue-1` 里不仅有订单 `1001`，还可能有订单 `1003`、`1005` 等其他订单的事件。假设订单 `1001` 的 `OrderCreated` 因外部数据库故障持续失败；订单 `1003` 的创建通知其实完全独立，为什么它也要被当前队列的队头挡住？

**下一章，我们要解决的不是“如何取消顺序”，而是“怎样只在真正有关联的消息之间保持顺序，让无关订单继续并行”。** 到那时，再去研究消息分组与更细粒度的 FIFO 约束，才有实际意义。

---

**源码阅读索引（固定 Commit）**

- [`ConsumeMessageOrderlyService.java`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/client/src/main/java/org/apache/rocketmq/client/impl/consumer/ConsumeMessageOrderlyService.java)：`start()`、`lockMQPeriodically()`、`ConsumeRequest.run()`、`processConsumeResult()`、`submitConsumeRequestLater()`。
- [`ProcessQueue.java`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/client/src/main/java/org/apache/rocketmq/client/impl/consumer/ProcessQueue.java)：`takeMessages()`、`makeMessageToConsumeAgain()`、`commit()`。
- [Apache RocketMQ 官方：Ordered Message](https://rocketmq.apache.org/docs/featureBehavior/03fifomessage/)：5.x 顺序消息、消息分组、重试中的先后约束。
