---
slug: /message-queue/two-consumers-message-claim
title: 一个消费者忙不过来了，启动两个会发生什么？
sidebar_position: 6
description: 继续追踪订单 1001 的 msg-1001，从两个消费者同时读到相同消息的失败现场出发，引出原子领取、不可见时间与领取凭证，并对照 RocketMQ 5.5.1 的 POP 源码。
tags: [消息队列, RocketMQ, 源码精读, 消费者, 并发, POP]
---

# 一个消费者忙不过来了，启动两个会发生什么？

上一章，订单 `1001` 的通知消息 `msg-1001` 即使被重复消费，也不会在我们的 SQLite 业务数据库中产生第二条相同的通知记录。

这解决了**重复执行业务的后果**，却没有解决**谁应该处理消息**。

如果每条通知需要一秒才能处理，那么一个消费者一分钟最多处理约 60 条。订单量上来后，我们很自然会想：不如启动两个消费者，让它们一起干活？

但前几章的教学队列有一个隐藏的前提：同一时刻只有一个消费者会调用 `receive_one()`。一旦这个前提被打破，原来正确的代码就会表现出完全不同的行为。

本章继续使用订单通知这一案例，**只解决一个问题：两个消费者怎样协作领取消息，而不是反复领取同一条？**

<!-- truncate -->

## 先别改代码：直接启动两个消费者

上一章沿用的 `AcknowledgedQueue`，领取消息只做了一次判断：

```python title="acknowledged_message_queue.py / receive_one()（上一章教学代码）"
def receive_one(self):
    return next(
        (m for m in self.messages if m["messageId"] not in self.acked_ids),
        None,
    )
```

它从当前进程加载的消息列表中，找到第一条没有 ACK 的消息，返回给调用者。

一个消费者运行时，它很合理：

```text
消费者 A：读取 msg-1001 → 执行业务 → ACK
```

现在让 A、B 两个进程同时启动。两边各自创建一个 `AcknowledgedQueue`，各自读取磁盘中的消息和确认日志。

```text
                 messages.jsonl
                       |
              读取未 ACK 消息
                /            \
               v              v
          消费者 A         消费者 B
          msg-1001         msg-1001
```

注意这里不存在数据损坏，也不需要任何极端操作。只要 **A 还没 ACK，B 的读取就完全可能得到同一条消息**。上一章的幂等处理能避免重复写业务记录，却不能让 B 自动去处理另一条任务；如果业务处理很慢，两个消费者可能都被同一条任务占住。

我们真的运行一次。下载本章资料包，将 `shared_claim_queue.py` 和前章的 `acknowledged_message_queue.py` 放在同一个**独立实验目录**，执行：

```bash
python shared_claim_queue.py reset
python shared_claim_queue.py produce-one
python shared_claim_queue.py naive-two
```

`naive-two` 会启动两个**独立 Python 子进程**，通过进程屏障让它们同时开始读取，两个进程都不执行 ACK，便于观察冲突。

本次实际运行的输出：

```text
消息已写入: msg-1001
消费者 A: msg-1001
消费者 B: msg-1001
```

这说明什么？我们之前实现的只是“找到未确认的消息”，不是“从多个竞争者中确定本次由谁处理”。

于是第一个新需求出现了：**消息必须先被某个消费者成功领取，其他消费者才能跳过它。**

## 第一个补丁：给领取操作加锁

直觉上，可以在领取时加锁：

```python title="概念伪代码：先建立直觉"
with shared_lock:
    message = find_unclaimed_message()
    mark_as_claimed(message)
return message
```

这里必须注意两件事：

第一，锁要让**不同进程**都看得见。`threading.Lock()` 保护的是同一进程内的线程，不能因为我们写了一个锁，就假设两台服务或两个独立进程会相互协调。

第二，锁保护的不应只是 `find_unclaimed_message()`，还必须包含紧接着的 `mark_as_claimed()`。如果两步之间释放锁，A 和 B 仍然可能先后查到“尚未领取”，再各自声称领取成功。

现在先不启动 RocketMQ，也不自己实现跨进程文件锁。我们已经在上一章用过 SQLite，所以可以让它作为**教学实验的共享协调存储**。这不是 RocketMQ 的内部实现，只是为了用标准库复现两个进程间的原子领取。

本章仍使用以前的 `messages.jsonl` 存储消息正文；改变的是**消费状态的存储方式**：单消费者时代的 `acknowledgments.jsonl` 不再是这一章的确认依据，而由 SQLite 的一张状态表同时保存领取与 ACK。这样领取和确认可以在同一份共享状态里完成。

第一版状态先想成这样：

| message_id | acked | owner |
|---|---:|---|
| msg-1001 | 0 | 空 |

谁先成功把 `owner` 从空改为自己，谁就获得了这次领取权。

让我们看看实际的教学实现：

```python title="shared_claim_queue.py / LeasedQueue.claim()（教学代码节选）"
conn.execute("BEGIN IMMEDIATE")

changed = conn.execute("""
    UPDATE consume_state
       SET receipt_token=?, owner=?, invisible_until=?
     WHERE consumer_group=? AND message_id=?
       AND acked=0 AND invisible_until<=?
""", (token, consumer, until, self.group, message_id, current)).rowcount

if changed:
    conn.commit()
    return Receipt(message, consumer, token, until)
```

暂时只看这段代码的**一个设计动作**：`BEGIN IMMEDIATE` 让多个连接不能在同一时刻进入竞争的写事务；`UPDATE ... WHERE ...` 只有在消息仍满足可领取条件时才会更新一行。`rowcount == 1` 表示这次成功获得领取资格，否则继续寻找其他消息。其余的 `token`、`invisible_until` 我们马上解释。

这样一来，A、B 即使同时启动，也不能都成功领取一条尚未到期的消息。运行：

```bash
python shared_claim_queue.py reset
python shared_claim_queue.py produce-one
python shared_claim_queue.py safe-two
python shared_claim_queue.py inspect
```

本次真实运行中：

```text
消息已写入: msg-1001
消费者 A: msg-1001
消费者 B: 无可领取消息
消息日志条数: 1
已确认消息数: 0
领取后尚未确认的消息: ['msg-1001']
记录过的消费状态数: 1
```

这一次，一条消息只有一个领取者。具体由 A 还是 B 抢先是不确定的；**正确性不依赖谁赢，而依赖只有一个能赢**。

如果把订单 `1002` 的消息也写入：

```bash
python shared_claim_queue.py reset
python shared_claim_queue.py produce-two
python shared_claim_queue.py safe-two
```

两个进程就可以分别领取 `msg-1001`、`msg-1002`，而不是总盯着同一条消息。

### 在这里进入 RocketMQ 5.5.1：Broker 怎样协调领取？


现在，我们先去真实源码中验证一件事：多个消费者争抢消息时，领取动作到底由谁协调？

本章仍固定 **RocketMQ `5.5.1`**，Commit [`efa1be59d6fc747ad220f036c86030f05f0419f9`](https://github.com/apache/rocketmq/commit/efa1be59d6fc747ad220f036c86030f05f0419f9)。

这次只阅读**普通非顺序消息的传统 POP 处理分支**，入口是：

[`broker/src/main/java/org/apache/rocketmq/broker/processor/PopMessageProcessor.java`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/broker/src/main/java/org/apache/rocketmq/broker/processor/PopMessageProcessor.java)

必须先说清版本边界：同一个 `processRequest()` 在 `popConsumerKVServiceEnable` 开启时，会转给 `PopConsumerService.popAsync()`。以下读的是**未进入该分支**的路径，不把它说成 RocketMQ 5.5.1 所有 POP 请求的唯一实现。[查看分支源码](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/broker/src/main/java/org/apache/rocketmq/broker/processor/PopMessageProcessor.java#L347-L352)。

#### 入口：从客户端请求到 Broker


`PopMessageProcessor.processRequest()` 接收客户端 POP 请求，解析 `PopMessageRequestHeader`。请求里包含消费组、Topic、队列选择、最大消息数和不可见时间等信息。

经过必要校验，在这条分支上，请求按实际选择的 Topic/队列继续调用 `popMsgFromTopic()` 或 `popMsgFromQueue()`。前者在多个队列之间组织尝试，后者负责一个队列上的实际读取。这不是“客户端在自己内存中加锁”，而是**Broker 在处理领取请求时协调同一份服务端状态**。

[查看 `processRequest()`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/broker/src/main/java/org/apache/rocketmq/broker/processor/PopMessageProcessor.java#L204-L221) · [查看读取队列的分发](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/broker/src/main/java/org/apache/rocketmq/broker/processor/PopMessageProcessor.java#L489-L499)

#### 临界区：为什么要保护同一队列的领取进度？


进入 `popMsgFromQueue()` 后，可以找到下面这段真实源码（**逐字节节选，省略无关语句**）：

```java title="PopMessageProcessor.java / popMsgFromQueue()（RocketMQ 真实源码节选）"
String lockKey =
    topic + PopAckConstants.SPLIT + requestHeader.getConsumerGroup() + PopAckConstants.SPLIT + queueId;
```

这个键由 `topic`、`consumerGroup`、`queueId` 组成：**协调的边界是某个消费组在某个队列上的领取状态**，并不是给每一个消费者创建一份互不相干的队列。

紧接着，源码调用 `queueLockManager.tryLock(lockKey)`。拿不到锁的请求会提前返回；拿到锁的路径重新读取领取位置，随后请求消息存储层 `getMessageAsync(...)` 读取消息。

[查看锁及调用上下文](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/broker/src/main/java/org/apache/rocketmq/broker/processor/PopMessageProcessor.java#L627-L670) · [查看存储层读取](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/broker/src/main/java/org/apache/rocketmq/broker/processor/PopMessageProcessor.java#L699-L704)

为什么不只看这把锁？因为**锁只保护队列读取与推进中的临界区，并不是消息业务处理期间一直持有的锁**。消息返回后，消费者可能处理几秒甚至更久；Broker 不能一直靠持有这把锁来等待业务完成。

这就把我们带回第二个问题：服务端如何记住“已交出去、尚未确认”的消息？

到这里，似乎只需要一个共享锁就够了。但新的失败场景马上出现。

## 如果领取者崩溃了，消息还属于它吗？

假设 A 已经领到了 `msg-1001`，还没处理完进程就退出。

如果我们的规则是“领到就永远属于 A”，那 B 永远没有机会处理它。

```text
A 成功领取 msg-1001
        |
        v
      A 崩溃
        |
        v
owner 永远是 A
        |
        v
B 永远领不到   ← 新问题
```

我们真正需要的不是永久所有权，而是：**在一段合理时间内，先让其他消费者看不见这条消息；如果领取者迟迟没有确认，再允许别人重新领取。**

这时才需要给领取权增加一个期限。为了便于说明，我们把它叫作**不可见时间（Invisible Duration）**。

例如，A 在 `t=100` 领取消息，指定不可见时间 10 秒：

```text
t=100         t=105           t=110          t=111
  |-------------|---------------|---------------|
  A 领取        B 来领取        期限到          B 可重新领取
  截止 t=110    被拒绝                          （若尚未 ACK）
```

在教学程序中，我们记录的是 `invisible_until`：

```python title="shared_claim_queue.py / LeasedQueue.claim()（教学代码节选）"
current = time.time() if now is None else float(now)
until = current + float(invisible_seconds)
```

并要求领取时满足 `invisible_until <= current`。如果 A 及时 ACK，消息就变成已完成；如果没有 ACK，期限过去后又有可能被领取。

这里存在一个故意保留的事实：**A 并不一定真的崩溃。** 它也可能只是执行得太慢。只要超过领取期限，B 就有可能拿到相同的消息。我们并没有因此得到 exactly-once，而只是避免了“无限占有”。

所以，上一章的业务幂等并没有过时。**领取协调降低正常情况下的重复处理，业务幂等限制故障和超时重叠时的重复业务后果。** 两者解决的是不同问题。

### 在源码里验证：消息交出去后，Broker 怎样记住它？


在普通非顺序 POP 分支中，成功取得消息后会调用 `appendCheckPoint()`。它创建 `PopCheckPoint`，记录本次 POP 的关键信息。最直观的两行真实源码是：

```java title="PopMessageProcessor.java / appendCheckPoint()（RocketMQ 真实源码节选）"
ck.setPopTime(popTime);
ck.setInvisibleTime(requestHeader.getInvisibleTime());
```

源码还记录消费组、Topic、队列 ID、起始 Offset、消息位置差值等，然后交给 `popBufferMergeService.addCk(...)` 等后续机制处理。

[查看 `appendCheckPoint()`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/broker/src/main/java/org/apache/rocketmq/broker/processor/PopMessageProcessor.java#L883-L911) · [查看调用位置](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/broker/src/main/java/org/apache/rocketmq/broker/processor/PopMessageProcessor.java#L748-L759)

这一步解释了：**返回消息本身不是全部工作，Broker 还需要维护能与后续 ACK、未确认恢复相联系的领取记录。**

但是，这不等于把我们教学代码里的 `owner`、`receipt_token`、`invisible_until` 原封不动写成一张 RocketMQ 的 SQLite 表。RocketMQ 有自己的 POP CheckPoint、缓冲与恢复路径；不同配置分支的服务端实现也不同。本章已经足够确认设计需求，尚未完整追踪 CheckPoint 在故障后的恢复细节。

如果要追究“到期后具体怎样再次可见”，后续还必须检查 ACK 与恢复机制；不能仅凭上面两行 `setInvisibleTime()` 就声称完整解释了恢复流程。RocketMQ [消费重试官方文档](https://rocketmq.apache.org/docs/featureBehavior/10consumerretrypolicy/) 给出的 SimpleConsumer 语义是：未确认时，消息在不可见时间结束后可以进入后续重试处理。

## 期限到了，A 迟到的 ACK 还算数吗？

假设发生下面的顺序：

```text
A 领取 msg-1001，期限 t=110
        ↓
A 处理很慢
        ↓
t=111，B 重新领到 msg-1001
        ↓
A 的旧处理终于结束
        ↓
A 发来 ACK
```

如果系统只接收 `ACK(messageId)`，它根本不知道这是 A 上一轮领取的 ACK，还是 B 这一轮领取的 ACK。

所以，我们还缺少一项能力：**区分同一条消息的不同领取轮次。**

我们在教学程序中给每次成功领取创建一个随机 `receipt_token`。消费者成功处理后必须携带这次领取的凭证，才能 ACK；如果消息已经重新分配给 B，A 手中的旧凭证就不再有效。

```python title="shared_claim_queue.py / LeasedQueue.ack()（教学代码节选）"
updated = conn.execute("""
    UPDATE consume_state
       SET acked=1, receipt_token=NULL, owner=NULL, invisible_until=0
     WHERE consumer_group=? AND message_id=?
       AND acked=0 AND receipt_token=? AND invisible_until>?
""", (self.group, receipt.message["messageId"], receipt.token, current)).rowcount

return updated == 1
```

这里最关键的是 `receipt_token=?` 和 `invisible_until>?`：旧凭证与当前数据库保存的凭证不一致时，ACK 不生效；期限已经过了，旧领取也不能再借这个凭证直接确认成功。

不必真的等待 10 秒。程序支持注入**教学逻辑时钟**，让故障顺序可以确定地复现：

```bash
python shared_claim_queue.py reset
python shared_claim_queue.py produce-one
python shared_claim_queue.py lease-demo
python shared_claim_queue.py inspect
```

已实际运行的结果：

```text
t=100: A 领取 msg-1001
t=105: B 领取结果 = None
t=111: B 重新领取 msg-1001
t=112: A 的旧令牌 ACK = False
t=112: B 的新令牌 ACK = True
消息日志条数: 1
已确认消息数: 1
领取后尚未确认的消息: []
记录过的消费状态数: 1
```

这一段实验只验证**我们自己写的领取状态机**，不代表运行了真实 RocketMQ Broker，也不代表 RocketMQ 直接使用本示例的随机 UUID 做消费凭证。

## 为什么两个消费者可以协作，但不代表吞吐量翻倍？

回头看我们的实验，一条消息只给一个消费者；有两条消息时，可以分别给两个消费者。这是协作所必需的，但还不能得出“消费者数量翻倍，吞吐量就翻倍”。

当前教学实现就有一个明显瓶颈：每次领取都扫描整个 `messages.jsonl`，再通过 SQLite 写事务串行地判断可领取状态。如果消息达到几十万条，单次扫描和共享写入的成本会越来越高。

RocketMQ 也不是靠“无限增加消费者线程”获得无限吞吐。真实系统还要考虑队列数、分配方式、存储读取成本、业务处理速度及有序消费约束。我们尚未验证任何吞吐量指标，本章也没有运行 RocketMQ Broker 做性能对比。

另外，领取协调不是业务幂等的替代品：如果 A 在不可见时间到期前已经完成了外部业务效果，却没来得及 ACK，B 仍可能重新处理。**第五章减少抢占冲突，第四章保护业务效果，这两层机制缺一不可。**

## 用测试回看这一章的变化

资料包里包含 `test_shared_claim_queue.py`。运行：

```bash
python -m unittest -v test_shared_claim_queue.py
```

我实际执行了六项测试，全部通过：

| 实验 | 可观察结果 |
|---|---|
| 两个旧版消费者同时读取一条消息 | A、B 都得到 `msg-1001` |
| 共享领取状态，两人竞争一条消息 | 只有一个领取成功 |
| 共享领取状态，两人竞争两条消息 | 各自得到不同消息 |
| 不可见时间尚未到期 | B 不能重复领取 |
| 到期后重新领取 | B 可以领取，A 的旧 ACK 被拒绝 |
| ACK 已提交后再启动进程 | 已确认状态仍可读取 |

这些验证都运行在本地 Python 子进程和 SQLite 上。**没有运行 RocketMQ Broker、没有测量集群吞吐量，也没有发送真实短信**。实际日志采用真实时钟，并发竞争谁先赢不固定；逻辑时钟只用于确定性地验证超时状态迁移。

从这条消息一路走来，我们现在能够解释：

```text
单个消费者：只需寻找未 ACK 的消息
                ↓
两个消费者：可能同时读到 msg-1001
                ↓
共享原子领取：同一时刻只有一个领取成功
                ↓
领取者可能崩溃：永久占有会阻塞恢复
                ↓
不可见时间：到期未确认可以再次领取
                ↓
不同轮次会交叠：需要识别旧领取凭证
                ↓
带凭证 ACK：旧领取不能确认新一轮领取
```

我们终于能让两个消费者有控制地分工了。但也看见了下一层瓶颈：**当消息数量持续增长，读取每条消息时都遍历整个日志，并在同一个状态点竞争，为什么会越来越慢？**

下一章，我们沿着这个具体性能限制，逐步研究如何组织消息的读取位置、拆分并行处理的工作范围，再对照 RocketMQ 的队列与消费进度设计。
