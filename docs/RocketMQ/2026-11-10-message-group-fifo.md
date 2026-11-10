---
slug: /message-queue/message-group-fifo
title: 一个订单卡住了，为什么其他订单也跟着等待？
sidebar_position: 10
description: 从队列级顺序消费的队头阻塞出发，用同一物理队列里的订单 1001 和 1002 推导消息分组，再对照 RocketMQ 5.5.1 中 MessageGroup 的写入映射、队列选择和 POP 顺序消费的真实边界。
tags: [消息队列, RocketMQ, 源码精读, MessageGroup, FIFO, 顺序消费]
---

# 一个订单卡住了，为什么其他订单也跟着等待？

上一章，我们终于保证了一个重要事实：订单 `1001` 的 `OrderCreated` 没有处理成功之前，`OrderPaid` 不能先完成。

为此，消费者每次只领取队列里**最早一条尚未确认的消息**。如果它正在处理或者等待重试，就暂时不领取后面的消息。

这听起来很合理。直到另一个用户也下单了。

```text
同一个逻辑队列 queue-0

offset 0   msg-1001        OrderCreated   orderId=1001
           （发送通知服务暂时故障，等待重试）

offset 1   msg-1002        OrderCreated   orderId=1002
           （通知服务正常，本来可以立即处理）

offset 2   msg-1001-paid   OrderPaid      orderId=1001
```

订单 `1001` 的消息必须保持顺序，因此 `msg-1001-paid` 需要等待 `msg-1001`。**但是订单 `1002` 和订单 `1001` 之间，并没有这个先后依赖。**

为什么 `1001` 的一次失败，要让 `1002` 也跟着等待？

这篇文章就解决这一个问题。

<!-- truncate -->

## 先让上一章的方案失败一次

上章的 `OrderedPartitionedQueue.claim_orderly()` 有一个非常严格的选择规则：

```python title="ordered_message_queue.py / claim_orderly()（第 08 章教学源码节选）"
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

这里的 `seq` 就是消息在当前逻辑队列中的位置。`ORDER BY seq LIMIT 1` 表示先确定**整条队列的队头**，再看它是否能被领取。

注意，这和更早的「从所有可见消息中挑一条」不同。后者会让支付消息越过创建消息；现在这个版本虽然不会乱序，却引入了更严格的阻塞。

为了不让实验被分区路由掩盖，我们这次特意把 `queue_count` 设置为 `1`：所有订单都进入 `queue-0`。然后写入三条消息：

```python title="message_group_queue.py / demonstration()（本章可运行教学代码）"
old = MessageGroupQueue(root / "old", queue_count=1)
old.publish("msg-1001", 1001, "OrderCreated")
old.publish("msg-1002", 1002, "OrderCreated")
old.publish("msg-1001-paid", 1001, "OrderPaid")

locked = old.claim_orderly(0, "consumer-A", now=100)
assert locked.message["messageId"] == "msg-1001"
assert old.claim_orderly(0, "consumer-B", now=100) is None
```

第一次领取成功，`msg-1001` 处于不可见时间内。第二次领取返回 `None`。

但这时队列里不是没有消息，而是后续消息全部被**同一个队列的队头**挡住了。

在这个例子中，让订单 `1002` 等待并没有业务上的必要。

我们真正需要改变的，似乎不是消息的写入顺序，而是**顺序约束的范围**。

## 先尝试增加更多队列，可以吗？

一个直觉的办法是增加逻辑队列数量，让不同订单尽量散开。

例如上一章使用过 `orderId % queue_count`：

```text
queue_count = 2

orderId 1001 -> queue-1
orderId 1002 -> queue-0
```

这时，两个订单确实不会互相阻塞。对于很多业务，按键散列到队列本来就是合理的做法。

但队列数量是有限的，订单数量通常远大于队列数量。只要两个无关订单碰巧落在同一队列，问题仍然存在。把 `queue_count` 从 `2` 增加到 `16` 或 `64`，只是降低了碰撞概率，并没有改变我们的规则：

> **当前方案要求同一队列中的所有消息都严格排队，而业务真正要求的，只是同一订单的消息严格排队。**

于是我们可以给顺序要求划定更精确的边界：同一个订单属于同一组；不同订单属于不同组。

这个标识，就可以称为**消息组（MessageGroup）**。

它不是另一份消息正文，也不是新的物理 Broker。对我们当前的问题而言，它只回答一件事：**哪些消息之间必须等待，哪些消息之间没有顺序约束？**

```text
queue-0（仍然只有一个物理队列）

  offset 0  msg-1001       group=order:1001
  offset 1  msg-1002       group=order:1002
  offset 2  msg-1001-paid  group=order:1001

组内要求：
  order:1001 : msg-1001 -> msg-1001-paid
  order:1002 : msg-1002

组间要求：
  没有先后关系
```

这里不要把 `MessageGroup` 与 `ConsumerGroup` 混淆。前者描述消息之间的业务顺序关系，后者描述消费者如何组成一个消费群体。名称相似，解决的问题完全不同。

## 我们只修改“找下一条消息”的规则

上一章找的是「整队最早未确认消息」。新规则改成：

**先找每个消息组最早的未确认消息，再从这些组头中选一条当前可以领取的消息。**

如果 `order:1001` 的组头暂时不可见，只能跳过这一组，不能越过它去领取本组的 `OrderPaid`。但这不妨碍 `order:1002` 的组头被领取。

本章的 `MessageGroupQueue` 直接继承上一章的 `OrderedPartitionedQueue`，复用此前逐层建立的持久化正文、定长索引、SQLite 领取状态、不可见时间、ACK 和领取凭证。

为了先看清这个机制，教学代码将 `orderId` 解释成消息组键：

```python title="message_group_queue.py / message_group()（本章真实教学源码）"
def message_group(message: dict) -> str:
    return f"order:{message['orderId']}"
```

这里是**教学约定**，并不等于 RocketMQ 的生产者可以省略 `setMessageGroup()`。后面我们会看到真实 5.x gRPC 生产路径如何显式传递消息组。

新的领取方法是：

```python title="message_group_queue.py / claim_grouped()（本章真实教学源码节选）"
pending = conn.execute(
    """SELECT seq, invisible_until FROM deliveries
       WHERE acked=0 ORDER BY seq"""
).fetchall()

seen_groups = set()
for seq, invisible_until in pending:
    message = self._read_at(queue_id, seq)
    group = message_group(message)

    if group in seen_groups:
        continue
    seen_groups.add(group)

    if invisible_until > current:
        continue

    # 这里才给该组队头生成领取凭证并更新不可见时间
    # ...
```

先看 `seen_groups`：

- 首次遇到 `order:1001`，它就是这个组当前最早的未确认消息。
- 如果它还不可见，本次跳过这个组；之后再次遇到 `order:1001` 的 `OrderPaid`，也不会领取。
- 首次遇到 `order:1002`，它是另一组的组头。只要可见，就允许领取。

**我们没有取消顺序，而是把顺序的约束从“整个队列”收缩到了“确实需要顺序的一组消息”。**

领取时仍然先开启 `BEGIN IMMEDIATE` 事务，再在这个事务里选择消息、更新 `receipt_token` 和 `invisible_until`。因此多个进程同时领取同一份 SQLite 状态时，不会仅仅因为这段 Python 循环就丢掉原子领取约束。

不过有一个真实的代价：这里为了突出组头规则，**每次领取都扫描未 ACK 行，并通过上一章的定长索引读取对应消息的组键**。这不是高性能的分组索引实现，也不对应 RocketMQ 真实的数据结构。它保留了 `ConsumeQueue` 风格的按位置读取，却没有解决大量消息组的快速定位问题。我们先验证语义，不把这段代码当作性能方案。

## 再跑一次：卡住 1001，1002 还能走吗？

这次仍然把所有消息放进同一条逻辑队列：

```python title="message_group_queue.py / demonstration()（本章真实教学代码节选）"
q = MessageGroupQueue(root / "new", queue_count=1)
q.publish("msg-1001", 1001, "OrderCreated")
q.publish("msg-1002", 1002, "OrderCreated")
q.publish("msg-1001-paid", 1001, "OrderPaid")

first = q.claim_grouped(0, "consumer-A", now=100)
other = q.claim_grouped(0, "consumer-B", now=100)
assert first.message["messageId"] == "msg-1001"
assert other.message["messageId"] == "msg-1002"
assert q.claim_grouped(0, "consumer-C", now=100) is None
```

实际执行：

```bash
python message_group_queue.py demo
```

本地运行的输出为：

```text
旧方案：order:1001 未完成时，order:1002 也被整个队列阻塞
分组方案：A=msg-1001，B=msg-1002；msg-1001-paid 仍不能越过前一条
完成：order:1002 不受 1001 的重试影响；1001 的创建仍先于支付
说明：这是单机教学模型；并未启动 RocketMQ Broker 证明同队列组级并行
```

这次两条消息可以同时处于被领取状态，说明它们不再被一个**队列级的业务顺序约束**串行化。但必须注意：我们仍然使用 SQLite 写事务保护领取状态，不能据此声称两个数据库写事务真正同时执行。

接着，我们让订单 `1001` 的第一次处理失败，并延后 5 个时间单位重试：

```python title="message_group_queue.py / demonstration()（本章真实教学代码节选）"
assert q.ack(other, now=101)
assert q.retry_later(first, now=101, delay_seconds=5)
assert q.claim_grouped(0, "consumer-B", now=104) is None

retry = q.claim_grouped(0, "consumer-B", now=106)
assert retry.message["messageId"] == "msg-1001"
assert not q.ack(first, now=107)  # 旧领取凭证失效
assert q.ack(retry, now=107)

paid = q.claim_grouped(0, "consumer-A", now=108)
assert paid.message["messageId"] == "msg-1001-paid"
assert q.ack(paid, now=109)
```

因此，本章没有丢掉前面已经建立的安全约束：

`msg-1001` 第一次失败后，`msg-1001-paid` 依旧不能被越过；旧领取凭证不能确认新一轮领取；订单 `1002` 的 ACK 也不会因为订单 `1001` 的失败而失效。

这就是消息分组在**教学模型**里带来的可观察变化。

## 进入 RocketMQ 5.5.1：生产者怎样表达消息组？

接下来必须回答：这个设计在真实系统中对应什么？

我们继续固定版本：Apache RocketMQ `5.5.1`，Commit [`efa1be59d6fc747ad220f036c86030f05f0419f9`](https://github.com/apache/rocketmq/commit/efa1be59d6fc747ad220f036c86030f05f0419f9)。下面所有称为「真实源码节选」的 Java 代码均来自这个固定 Commit，而上面的 Python 是**教学实现**。

先只看**单条消息通过 5.x gRPC Proxy 发送**这条路径。不要先跳到消费者，也不要同时讲所有通信协议。

### 一条消息怎样带上自己的分组？

在 [`proxy/src/main/java/org/apache/rocketmq/proxy/grpc/v2/producer/SendMessageActivity.java`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/proxy/src/main/java/org/apache/rocketmq/proxy/grpc/v2/producer/SendMessageActivity.java) 中，Proxy 把请求里的 `messageGroup` 映射为内部消息属性：

```java title="SendMessageActivity.java / 消息属性构造（真实源码节选）"
String messageGroup = message.getSystemProperties().getMessageGroup();
if (StringUtils.isNotEmpty(messageGroup)) {
    validateMessageGroup(messageGroup);
    MessageAccessor.putProperty(messageWithHeader,
        MessageConst.PROPERTY_SHARDING_KEY, messageGroup);
}
```

这几行代码回答了一个具体问题：**业务侧说的“订单 1001 属于同一组”，如何进入服务端的消息表示？**

调用时机是在 Proxy 处理生产者的发送请求、构造内部消息属性时。输入是请求中的 `messageGroup`；这段代码先校验，再将它写成内部的 `PROPERTY_SHARDING_KEY`。下游的发送与队列选择路径会读取相关分组信息。

它并没有在这里直接创建一个物理队列，也没有在这里把消息交给消费者。我们应该继续沿着**发送路径**，而不是凭术语自行补出一个不存在的“组调度器”。

### 同一组为什么能进入同一个队列？

同一个文件内的 `SendMessageQueueSelector.select()` 对单条请求中的 `messageGroup` 进行队列选择：

```java title="SendMessageActivity.java / SendMessageQueueSelector.select()（真实源码节选）"
if (request.getMessagesCount() == 1) {
    shardingKey = message.getSystemProperties().getMessageGroup();
    // ... 其他与本问题无关的分支省略
}

// ...
if (StringUtils.isNotEmpty(shardingKey)) {
    List<AddressableMessageQueue> writeQueues =
        messageQueueView.getWriteSelector().getQueues();
    int bucket = Hashing.consistentHash(
        shardingKey.hashCode(), writeQueues.size());
    targetMessageQueue = writeQueues.get(bucket);
} else {
    targetMessageQueue =
        messageQueueView.getWriteSelector().selectOneByPipeline(false);
}
```

这里的关键动作不是“为订单创建新的队列”，而是**让相同的分组键选择同一个队列位置**。在相同的可写队列集合下，`order:1001` 的创建和支付消息就能落在同一个逻辑队列中；`order:1002` 可能碰巧落在同一个队列，也可能落到别处。

注意源码前面的 `request.getMessagesCount() == 1`。我们正在解释的是**单条发送请求的这条已核对路径**，不能把它不加检查地推广到所有批量发送情况。再加上官方对生产顺序的要求——同组消息需要在能够确定顺序的生产路径上串行发送——**设置了组键，也不等于所有并发生产者天然有全局业务顺序**。

这两处源码分别证明了“组信息如何进来”和“组键如何参与选队列”。还没有证明“两个组在同一个队列时，消费一定可以彼此绕开”。这是另一个问题，必须继续往下看。

## 真正需要谨慎的地方：固定版本的 POP 路径并不等于我们的教学模型

RocketMQ 的[官方顺序消息文档](https://rocketmq.apache.org/zh/docs/featureBehavior/03fifomessage/)把 FIFO 顺序关系描述在 `MessageGroup` 层面：同组消息需要保持先后，不同组消息之间没有这样的业务顺序要求。它还提醒，顺序消费失败后的重试会阻挡后续相关消息，并且重试次数需要有界。

这正是我们刚才推导的**目标语义**。

但当我们真正打开固定 Commit 的 [`PopMessageProcessor.java / popMsgFromQueue()`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/broker/src/main/java/org/apache/rocketmq/broker/processor/PopMessageProcessor.java) 时，会看到这样一段检查：

```java title="PopMessageProcessor.java / popMsgFromQueue()（真实源码节选）"
if (isOrder) {
    if (brokerController.getConsumerOrderInfoManager().checkBlock(
        attemptId, topic, requestHeader.getConsumerGroup(),
        queueId, requestHeader.getInvisibleTime())) {
        future.complete(restNum);
        return future;
    }
}
```

这个位置的调用者是 Broker 端的 POP 取消息流程。它把 `topic`、消费者组、`queueId` 等传入 `checkBlock()`，如果目前需要阻塞，就在继续读消息之前结束当前分支。

这里有一个值得停下来认真对照的事实：**参数里并没有我们教学模型中的 `orderId` 或 `messageGroup`。**

继续追踪这个版本中队列级实现 [`QueueLevelConsumerManager.checkBlock()`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/broker/src/main/java/org/apache/rocketmq/broker/pop/orderly/QueueLevelConsumerManager.java)：

```java title="QueueLevelConsumerManager.java / checkBlock()（真实源码节选）"
String key = buildKey(topic, group);
ConcurrentHashMap<Integer/*queueId*/, OrderInfo> qs = table.get(key);
// ... 初始化与判空代码省略
OrderInfo orderInfo = qs.get(queueId);
if (orderInfo == null) {
    return false;
}
return orderInfo.needBlock(attemptId, invisibleTime);
```

这个实现把状态组织为 `topic + consumerGroup -> queueId -> OrderInfo`。类名也清楚说明：**QueueLevel**，即队列级顺序管理。与此相呼应，该类的 `getOrderedConsumptionLevel()` 返回 `OrderedConsumptionLevel.QUEUE`。

所以，我们必须区分三个命题：

1. **业务要求**：同一个订单的消息按序，两个无关订单没有业务上的先后关系。
2. **生产端已核对的实现**：单条 5.x gRPC 发送路径能够用 `messageGroup` 参与队列选择，同一组映射到同一队列。
3. **消费端已核对的具体实现**：上面这一条 POP 顺序分支使用 `queueId` 进行阻塞检查，不能拿它作为“同一个物理队列里不同 MessageGroup 一定可以独立解锁”的源码证据。

这是本章最重要的源码阅读结论之一。**我们不能因为官方文档用 MessageGroup 解释业务顺序，就假装所选的所有旧接口、POP 路径和队列级管理实现都已经变成了按组独立调度。**

本章 Python 里的 `claim_grouped()` 是为展示更细粒度顺序约束而设计的**教学化简**，不是 `QueueLevelConsumerManager` 的翻译版，也不是对 RocketMQ 同队列组间并发能力的端到端测试。要讨论具体部署中的实际并发粒度，必须再核对所使用的 SDK、Proxy/Broker 消费路径、订阅类型和配置，并运行真实 Broker 实验。

这不是“源码没有对上理论，所以略过不讲”，而是恰恰相反：**源码为我们划清了哪些结论已经被证实，哪些仍然只是设计目标。**

## 运行验证：我们能肯定什么？

下载本章配套的 Python 文件后，在同一目录执行：

```bash
python message_group_queue.py demo
python -m unittest -v test_message_group_queue.py
```

本章实际运行结果：**15 项自动化测试通过**。其中既有前几章继承来的可靠性验证，也有本章新增的分组约束验证。

| 实验 | 本次验证到的行为 |
| --- | --- |
| 旧的整队队头规则 | `msg-1001` 不可见时，`msg-1002` 也不能领取 |
| 两个消息组落在同一物理队列 | `msg-1001` 领取中，`msg-1002` 仍可领取 |
| 相同订单创建和支付 | `msg-1001-paid` 不会越过未确认的 `msg-1001` |
| 同组失败延迟重试 | 只影响该组后续消息的领取 |
| 超时恢复 | 到达不可见时间后，原组头可以重新领取 |
| 过期领取凭证 | 旧 ACK 被拒绝，新领取凭证仍可 ACK |
| 重启恢复 | 消息、ACK 状态及不可见时间继续生效 |
| 两个独立 Python 进程竞争同一条消息 | 只有一个领取成功 |
| 两个线程领取不同组 | 各自拿到不同消息，且不重号 |

这些测试说明：**在本章的 SQLite 教学实现和当前测试条件下**，细粒度组头约束与之前的 ACK、重领机制可以一起工作。

它们**不**说明我们已经压测了 RocketMQ、证明了磁盘断电恢复、验证了 Proxy 的批量消息分组，或确认了某条 Broker POP 路径可以跨组并行。

还有两个边界没有被这章解决。

首先，`claim_grouped()` 仍然遍历未完成的消息以计算组头。即使正文有按位置读取的索引，也没有生产级的组索引；消息数量增长时，领取成本仍会增加。

其次，我们之前已经发现：**业务幂等和顺序消费是两件不同的事。** 即使组内不乱序，消费者也可能在业务成功后、ACK 前崩溃，然后重做同一条消息。第 04 章建立的幂等边界仍然必须保留。

## 回看这条演化链

从最初的队列，到现在我们只改变了一个核心问题的答案：**谁必须等待谁？**

```text
第 07 章：不同逻辑队列可以独立处理

   queue-0                 queue-1
      |                       |
      v                       v
    消费者 A                消费者 B

第 08 章：同一队列不能越过未完成的队头

   queue-0
      |
      +-- OrderCreated(1001)  [待重试]
      +-- OrderCreated(1002)  [等待]
      +-- OrderPaid(1001)     [等待]

第 09 章：教学模型中，只有同组消息保持顺序约束

   queue-0
      |
      +-- group order:1001
      |      OrderCreated(1001) [待重试]
      |      OrderPaid(1001)    [等待]
      |
      +-- group order:1002
             OrderCreated(1002) [可继续]
```

我们没有改变消息正文必须可靠保存的要求，没有取消领取凭证，也没有放弃失败重试。只是把顺序的作用范围，从整条队列调整成了业务真正需要的一组消息。

同时，RocketMQ 5.5.1 的固定源码也给了我们一个重要提醒：**概念上的顺序分组、生产端的分组路由、具体消费路径里的顺序检查，不是同一个机制。** 必须逐段核实它们如何衔接。

现在还有一个问题没有解决。

假如 `msg-1001` 永远处理不成功，我们可以不再让订单 `1002` 陪着等，但订单 `1001` 自己的 `OrderPaid` 岂不是永远无法前进？

下一章，我们就让同一条消息连续失败十几次，看看究竟该无限重试，还是需要一个**明确的失败终点**。到那时，再引入有限重试与死信处理，才有真正的因果依据。

---

### 源码与资料索引

- 固定版本：[Apache RocketMQ 5.5.1 / Commit efa1be59](https://github.com/apache/rocketmq/commit/efa1be59d6fc747ad220f036c86030f05f0419f9)
- [官方文档：顺序消息](https://rocketmq.apache.org/zh/docs/featureBehavior/03fifomessage/)：MessageGroup、发送顺序、消费顺序和失败重试语义。
- [官方文档：消费者负载均衡](https://rocketmq.apache.org/docs/featureBehavior/08consumerloadbalance/)：消息负载均衡与有序消费之间的关系。
- [`SendMessageActivity.java`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/proxy/src/main/java/org/apache/rocketmq/proxy/grpc/v2/producer/SendMessageActivity.java)：gRPC 生产路径的消息组属性与写队列选择。
- [`PopMessageProcessor.java`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/broker/src/main/java/org/apache/rocketmq/broker/processor/PopMessageProcessor.java)：`popMsgFromQueue()` 中的顺序阻塞入口。
- [`QueueLevelConsumerManager.java`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/broker/src/main/java/org/apache/rocketmq/broker/pop/orderly/QueueLevelConsumerManager.java)：队列级顺序状态的具体组织方式。

> 本文 Java 源码为固定 Commit 的**真实源码节选**，省略处均有明确说明。Python 代码是基于第 07、08 章逐步演化的**可运行教学实现**，未冒充 RocketMQ 原码。运行结果来自本地 Python 测试，不是 RocketMQ Broker 端到端实验。
