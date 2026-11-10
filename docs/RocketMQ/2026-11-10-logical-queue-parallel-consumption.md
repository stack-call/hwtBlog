---
slug: /message-queue/logical-queue-parallel-consumption
title: 索引已经解决了扫描问题，为什么消费者仍然不能无限并行？
sidebar_position: 8
description: 从订单通知的共享领取状态竞争出发，用可运行的 Python 实验逐步推导逻辑队列、按键路由与消费分配，并对照 RocketMQ 5.5.1 的 MessageQueue 和 Rebalance 源码。
tags: [消息队列, RocketMQ, 源码精读, MessageQueue, 消费者组, 并行消费]
---

# 索引已经解决了扫描问题，为什么消费者仍然不能无限并行？

上一章，我们给 `msg-1001` 增加了一份消息位置索引。原来消费者每领取一次通知，都要重新解析整份消息日志；现在它先取得逻辑位置，再读取对应的索引条目，就能直接找到消息正文。

在那个实验中，处理 400 条消息中的前 40 条，JSON 解析次数由 16,000 次变成了 40 次。这解决了不必要的重复扫描，却没有证明**增加消费者就一定能增加处理能力**。

原因藏在另外一个地方。

上一章的 `claim_indexed()` 在知道消息位置以后，仍然使用同一个 SQLite 数据库记录所有消息的领取状态。无论消费者 A 要领取 `msg-1001`，还是消费者 B 要领取另一条订单通知，都要进入同一个写事务入口。

**这一章要解决的不是“如何更快找到消息”，而是“如何让彼此无关的消息拥有互不干扰的处理进度”。**

我们还是围绕订单 `1001` 展开。只有在真的遇到协调冲突时，才引入逻辑队列；只有当逻辑队列需要被不同消费者处理时，才去研究它如何分配。

<!-- truncate -->

## 第一次冲突：消息不一样，领取时却互相等待

先回到上一章的 Python 教学源码 `indexed_message_queue.py`。

定位消息的方式已经改进，但领取过程里仍然有这样一个操作：

```python title="indexed_message_queue.py（上一章教学源码节选）"
conn.execute("BEGIN IMMEDIATE")
row = conn.execute("""
    SELECT seq FROM consumption
    WHERE acked=0 AND invisible_until<=?
    ORDER BY invisible_until, seq LIMIT 1
""", (current,)).fetchone()
```

`BEGIN IMMEDIATE` 的目的非常正当。第五章我们已经发现：如果两个消费者都先读取「消息还没有被领取」，再分别更新领取状态，同一条消息就可能被两个人同时拿到。

因此，领取需要具备原子性。

但 SQLite 的 `BEGIN IMMEDIATE` 会对**同一个数据库文件**的写事务实施互斥。它不是按照我们的 `messageId` 分别加锁。于是，一位消费者持有写事务时，另一位消费者即使打算领取完全不同的消息，也可能需要等待。

```text
同一个 consumption.sqlite3
          |
     BEGIN IMMEDIATE
          |
    +-----+-----+
    |           |
 消费者 A      消费者 B
想领 msg-1001  想领 msg-1002
    |           |
拿到写事务     等待写事务
```

注意，这还不能证明实际运行中的吞吐量一定很低。第五章的事务很短，而且 SQLite 自身也有多种使用与优化方式。我们只能确定：**这份教学实现让互不相关的领取动作经过了同一个写事务入口。**

### 先复现，而不是直接声称存在瓶颈

这一章的资料包提供 `partitioned_message_queue.py`。先运行：

```bash
python partitioned_message_queue.py demo
```

程序首先创建一个旧式的、单文件 SQLite 领取状态库，故意让消费者 A 的写事务暂时不结束；接着让消费者 B 也执行 `BEGIN IMMEDIATE`。

本次实际输出的第一行是：

```text
旧方案：一个共享状态文件有写事务时，另一位领取者也被数据库写锁阻塞
```

这里没有靠 `sleep()` 猜测性能，而是让两个独立的 SQLite 连接在同一时刻竞争写事务。第二个连接触发了明确的数据库锁错误。

你可能会问：既然问题是数据库锁，为什么不缩短事务、优化 SQL，或者换一个支持更高并发的存储系统？

**这些都是合理方案。**

所以，我们不能从「有锁冲突」就断言「必须使用分区」。不过我们的目标还有一层：希望将来能够把不同的订单通知**交给不同消费者独立处理**，而不是让所有消费者始终竞争一份全局的领取状态。为此，需要一个稳定的处理范围。

## 让领取范围变小：先把消息分成两组

现在有两条需要发送通知的订单：

```json title="两条通知消息（教学数据）"
{"messageId": "msg-1001", "eventType": "OrderCreated", "orderId": 1001}
{"messageId": "msg-1002", "eventType": "OrderCreated", "orderId": 1002}
```

我们先不改变上一章的追加日志和定长索引机制，只做一个最小的选择：**把消息划分成两个可以分别领取的逻辑组。**

为了让分组结果稳定，本章暂时使用：

```python title="partitioned_message_queue.py / publish()（教学源码节选）"
queue_id = order_id % self.queue_count
```

当队列数量是 `2` 时：

```text
订单 1001 -> 1001 % 2 = 1 -> queue-1
订单 1002 -> 1002 % 2 = 0 -> queue-0
订单 1003 -> 1003 % 2 = 1 -> queue-1
```

这里的 `%` 不是 RocketMQ 生产者默认采用的通用路由算法，而是我们为了让实验结果可预测而编写的**教学路由规则**。真正的生产者可以按不同策略选择目标队列。

现在，每条消息都多了一个明确的归属：`queue_id`。

对于这个系统，消息的位置就不再只有单独的 `seq`，而是：

```text
(queue_id, seq)

(1, 0) -> msg-1001
(0, 0) -> msg-1002
(1, 1) -> msg-1003
```

为什么两个队列都能出现 `seq = 0`？因为 `seq` 现在表示**某一个队列内部的逻辑位置**，不再表示整个 Topic 的全局序号。

我们给这样的分组起一个名字：**逻辑队列（MessageQueue）**。在 RocketMQ 中，一个 Topic 可以包含多个逻辑队列；每个队列拥有自己的逻辑消费位置。

但是，仅仅给消息增加 `queue_id` 字段，还不会自动改善协调范围。若所有队列仍使用同一个 `consumption.sqlite3` 和同一把数据库写锁，原来的争用依旧存在。

所以还需要让刚刚建立的边界真正进入读写路径。

## 让每个队列拥有独立的领取状态

本章教学程序保留**一份共享的消息正文日志**，但给每个逻辑队列分别保存位置索引和领取状态：

```text
                    生产通知消息
                         |
                         v
              commitlog-teaching.jsonl
                  消息正文追加存储
                         |
           +-------------+-------------+
           |                           |
           v                           v
   queue-0 位置索引             queue-1 位置索引
   consumequeue-0.bin          consumequeue-1.bin
           |                           |
           v                           v
     state-0.sqlite3              state-1.sqlite3
       领取与 ACK                   领取与 ACK
           |                           |
           v                           v
        消费者 A                    消费者 B
```

这张图并不是完整 RocketMQ 架构图。它只是刚才两个要求的汇总：

1. 消息正文仍然可以共用一份追加日志，不必复制两份内容。
2. 每个逻辑队列单独组织位置与领取状态，让领取动作不再都争夺同一个 SQLite 写事务。

为什么不把正文也拆成两份日志？因为本章尚未遇到「正文追加写入本身必须拆开」的问题。让正文保持共享，还能沿用上一章 CommitLog 与 ConsumeQueue 的知识。**我们只改当前需要改的协调范围。**

### 先看消息如何被记录到指定队列

`publish()` 写入正文时，像上一章一样记住它的物理字节位置。变化只在索引：以前所有消息都写进同一份 `.cq`；现在只往所选队列的索引文件追加一个条目。

```python title="partitioned_message_queue.py / publish()（教学源码节选）"
queue_id = order_id % self.queue_count

with self.log_path.open("ab") as log, self._index_path(queue_id).open("ab") as index:
    seq = index.tell() // ENTRY.size
    physical_offset = log.tell()
    log.write(record)
    index.write(ENTRY.pack(physical_offset, len(record), 0))
```

`physical_offset` 属于那份共享的消息正文日志；`seq` 则从当前队列的索引文件长度计算。

这解释了刚才为什么两个队列都可以有 `seq = 0`：它们对应不同的索引文件，但最终仍然能指向同一份正文日志中的不同物理位置。

我们的教学索引仍然是一条 20 字节，但这不是与 RocketMQ 完全兼容的磁盘文件格式。JSONL、SQLite 状态表、预留字段都只是为了让最小路径清楚可读。

### 再看消费者按什么范围领取

之前的调用形式是：

```python title="上一章的教学接口"
receipt = queue.claim_indexed("A", metrics)
```

现在领取必须知道自己负责哪个队列：

```python title="本章的教学接口"
receipt = queue.claim(queue_id=1, consumer_id="B", now=100)
```

对应的关键代码并不复杂：

```python title="partitioned_message_queue.py / claim()（教学源码节选）"
conn = self._connect(queue_id, timeout=timeout)
conn.execute("BEGIN IMMEDIATE")

row = conn.execute(
    """SELECT seq FROM deliveries
       WHERE acked=0 AND invisible_until<=?
       ORDER BY seq LIMIT 1""", (now,)
).fetchone()
```

`_connect(queue_id)` 现在会打开 `state-0.sqlite3` 或 `state-1.sqlite3`。同一队列的领取请求仍然必须在事务里更新状态，因此第五章的「不要重复领取」还在；只是不同队列之间不再共享**这把数据库写锁**。

领取后返回的凭证也增加了 `queue_id`：

```python title="partitioned_message_queue.py / Receipt（教学源码节选）"
@dataclass(frozen=True)
class Receipt:
    queue_id: int
    seq: int
    token: str
    deadline: float
    message: dict
```

为什么凭证必须记住队列？因为两个队列都有 `seq = 0`。如果 ACK 只凭 `seq` 找领取记录，可能会定位到完全不同的一条消息。

调用 `ack(receipt)` 时，程序根据 `receipt.queue_id` 打开相应状态库，再按 `receipt.seq`、`receipt.token` 和有效时间检查这一轮领取是否仍然有效。

**分队列没有废除 ACK、不可见时间和领取凭证，只是把它们的作用范围变得更精确。**

## 新方案能否真正让两个队列互不等待？

运行刚才的 `demo`，旧方案的锁冲突之后，程序继续做第二个实验：

1. 将 `msg-1001` 放到 `queue-1`，将 `msg-1002` 放到 `queue-0`。
2. 让 `queue-0` 的 SQLite 写事务保持打开。
3. 此时尝试让另一个消费者从 `queue-1` 领取 `msg-1001` 并发送 ACK。
4. 最后尝试从仍被锁住的 `queue-0` 领取消息。

本次实际输出：

```text
msg-1001 -> queue-1；msg-1002 -> queue-0
新方案：queue-0 写锁未释放，queue-1 依然领取并 ACK msg-1001
同一队列内部仍然需要原子领取：queue-0 的并发写请求被阻塞
```

这组结果说明：同一队列内的互斥没有消失，但 `queue-0` 的写锁不再阻塞 `queue-1` 的领取与 ACK。

我们并没有把一条消息同时分给两个人，也没有为了提高并行度而放弃确认机制。

接着，程序让两个线程分别处理两个队列。它使用一个 `Barrier`，要求两个线程都到达业务处理点，才能继续执行 ACK。于是可以排除「线程 A 处理结束后，线程 B 才开始」这种假并行。

实际输出：

```text
并发实验：两个队列的处理线程同时进入业务阶段，且都成功 ACK
说明：这里只验证允许并行，不是 RocketMQ 吞吐量测试
```

要特别注意，这并不意味着完整系统的性能已经翻倍。共享正文日志、磁盘设备、网络、生产者发送、消费业务本身，都可能成为新的限制。

而且，**划分两个队列，也不等于消费者一定只有两个线程**。我们现在证明的是队列能够成为独立的消费分配单元，而不是规定所有消息队列实现都必须按「一队列一线程」工作。

## 两个队列已经独立了，第三个消费者负责什么？

假设现在有两个逻辑队列：

```text
queue-0：订单 1002、1004、1006 ...
queue-1：订单 1001、1003、1005 ...
```

我们最初只启动一个消费者 `A`，它负责两个队列：

```text
A -> queue-0, queue-1
```

后来发现处理仍然来不及，于是增加了消费者 `B`：

```text
A -> queue-0
B -> queue-1
```

在这种**按队列分配消费任务**的模型里，两个消费者就可以分别管理两个队列的领取进度。

再启动第三个消费者 `C` 呢？

```text
A -> queue-0
B -> queue-1
C -> （没有分到队列）
```

这不是运行故障。因为我们当前采用的分配规则，是让一个队列在同一时刻分给消费组中的一个消费者实例。可分配的队列只有两个，增加第三个实例不会凭空产生第三个队列。

在 `partitioned_message_queue.py` 中，我们写了一段非常简化的静态分配逻辑：

```python title="partitioned_message_queue.py / allocate_evenly()（教学源码节选）"
result = {consumer: [] for consumer in sorted(consumers)}
for i, qid in enumerate(range(queue_count)):
    result[sorted(consumers)[i % len(consumers)]].append(qid)
```

运行结果：

```text
2 个队列 / 3 个消费者的静态分配： {'A': [0], 'B': [1], 'C': []}
```

这段代码只负责在给定的消费者名单中分配队列，没有实现在线加入退出、故障转移、队列所有权切换，也不等于 RocketMQ 的真实分配算法。使用它只是为了让「有三个消费者，但只有两个队列」这个事实变得可观察。

现在我们终于遇到了一个值得去源码里验证的问题：**真实 RocketMQ 如何表示一个逻辑队列？又如何决定由哪些消费者处理它？**

## 进入 RocketMQ 5.5.1：先找到真正的逻辑队列

本系列继续固定 **Apache RocketMQ 5.5.1**，Commit：[`efa1be59d6fc747ad220f036c86030f05f0419f9`](https://github.com/apache/rocketmq/commit/efa1be59d6fc747ad220f036c86030f05f0419f9)。下面所有标为「真实源码节选」的内容均对照这一 Commit 核对；省略的分支不会冒充成连续代码。

先看这个文件：[`common/src/main/java/org/apache/rocketmq/common/message/MessageQueue.java`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/common/src/main/java/org/apache/rocketmq/common/message/MessageQueue.java#L466-L497)。

这里的 `MessageQueue` 不是「一份装满消息的 Java 集合」。它是用来标识消息队列的对象，核心字段十分简单：

```java title="MessageQueue.java（真实源码节选）"
private String topic;
private String brokerName;
private int queueId;
```

三个字段合起来才能说明这是哪一个队列：属于哪个 Topic、位于哪个 Broker、该 Broker 内的队列 ID 是多少。

我们在 Python 中只用了整数 `queue_id`，是因为教学程序始终只有一个固定 Topic 和一份本地存储；在真实分布式环境中，只有 `queueId = 0` 显然不足以表示消息属于哪台 Broker、哪个 Topic。

### 生产者为什么需要选择一个队列？

我们刚才在 Python 中直接做了 `order_id % queue_count`。真实消息系统在发送时，同样需要确定消息最终属于哪个目标队列。

位置：[`DefaultMQProducerImpl.java / invokeMessageQueueSelector()`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/client/src/main/java/org/apache/rocketmq/client/impl/producer/DefaultMQProducerImpl.java#L3325-L3373)。

这条**显式指定 `MessageQueueSelector` 的发送路径**里，生产者先从 Topic 的发布路由信息得到候选队列，再调用选择器：

```java title="DefaultMQProducerImpl.java（真实源码关键表达式）"
selector.select(messageQueueList, userMessage, arg)
```

调用者是生产者发送流程；`messageQueueList` 是可选的目标队列，`userMessage` 是消息，`arg` 则可以承载应用传来的路由依据。选择出的队列会继续用于后续发送。

这一行证明的是：**生产者可以在发送时明确选择逻辑队列。** 它并不能证明 RocketMQ 默认会自动按订单号取模，更不意味着每一种发送 API 都经过同一个选择器分支。

我们现在只需要这一层证据。为什么同一个订单的消息可能希望路由到同一队列，要等下一章真正遇到顺序问题时再展开。

### Broker 读取时，队列 ID 怎样影响索引定位？

第六章我们已经阅读了 `ConsumeQueue`，当时注意的是「逻辑 Offset 怎样变成正文的物理位置」。现在有了多个逻辑队列，就可以回头解释另一个之前没有必要展开的参数：`queueId`。

位置：[`DefaultMessageStore.java / getMessage()`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/store/src/main/java/org/apache/rocketmq/store/DefaultMessageStore.java#L796-L905)。

这个存储读取函数接收 `group`、`topic`、`queueId`、`offset` 等信息。在普通存储读取分支，最关键的查找是：

```java title="DefaultMessageStore.java / getMessage()（真实源码节选）"
ConsumeQueueInterface consumeQueue = findConsumeQueue(topic, queueId);
```

它的含义现在就非常具体了：不能只说「取出 Offset 为 0 的消息」，因为每个队列都可能存在 Offset `0`；必须先确定 Topic 和队列 ID，再在**那一个逻辑队列**的索引中定位。

索引返回物理位置和长度后，读取路径再到 `CommitLog` 读取消息正文。这和我们刚才的结构相呼应：共享正文日志，分别建立消费索引。

这里要区分职责：RocketMQ 中的 `ConsumeQueue` 组织逻辑索引，并不是本章 Python 代码里的 `state-0.sqlite3`、`state-1.sqlite3` 这类 ACK/不可见时间数据库。我们没有证明 RocketMQ 用 SQLite 保存消费状态，更没有把消费进度、消费确认和消息位置索引混成同一件事。

### 两个队列，三个消费者：源码如何分配？

队列存在了、消息能够路由过去、读取能够按队列定位，但还缺少一个动作：谁负责消费哪一个队列？

以经典的**客户端集群消费队列分配分支**为例，进入 [`RebalanceImpl.java / rebalanceByTopic()`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/client/src/main/java/org/apache/rocketmq/client/impl/consumer/RebalanceImpl.java#L2224-L2348)。

这条路径会取得当前 Topic 的队列集合 `mqSet` 和消费组中的消费者 ID 列表 `cidAll`，排序后调用分配策略：

```java title="RebalanceImpl.java / rebalanceByTopic()（真实源码节选）"
allocateResult = strategy.allocate(
    this.consumerGroup,
    this.mQClientFactory.getClientId(),
    mqAll,
    cidAll);
```

这段调用中：

- `mqAll` 表示待分配的队列集合；
- `cidAll` 表示参与分配的消费者实例列表；
- `getClientId()` 指出当前是谁在查询自身的分配结果；
- 返回的 `allocateResult` 是**分给当前消费者的队列列表**。

下一步该方法将结果整理为集合，并交给 `updateProcessQueueTableInRebalance(...)`。也就是说，分配策略不等于实际处理消息；分配结果还需要进入消费者本地的队列处理状态。

我们继续只阅读当前问题最相关的一种策略：[`AllocateMessageQueueAveragely.java`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/client/src/main/java/org/apache/rocketmq/client/consumer/rebalance/AllocateMessageQueueAveragely.java#L338-L383)。

```java title="AllocateMessageQueueAveragely.java（真实源码节选）"
int index = cidAll.indexOf(currentCID);
int mod = mqAll.size() % cidAll.size();
```

它用当前消费者在有序消费者列表中的位置，以及队列数与消费者数的余数，确定每个消费者应取得的队列区间；之后将对应队列加入返回列表。

从中可以解释「2 个队列、3 个消费者」的情形：在这种平均分配策略下，不可能给三个消费者各分配一个互不重复的队列，因为队列总共只有两个。

但请注意两点差异：**真实 `AllocateMessageQueueAveragely` 使用连续区间的平均分配，而不是我们 Python 示例里的轮转 `i % consumer_count`；两者在两队列/三消费者的实验中结论相同，不代表实现相同。**

另外，RocketMQ 5.5.1 的 `RebalanceImpl.doRebalance()` 还存在通过 `getRebalanceResultFromBroker()` 获取 Broker 侧队列分配的路径；POP 消费与经典拉取/Push 消费的状态处理也并不相同。本章展示的是一条已经核对的**客户端集群分配路径**，不能把它当作 5.x 所有消费模式的通用执行流程。

源码入口：[`RebalanceImpl.doRebalance()`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/client/src/main/java/org/apache/rocketmq/client/impl/consumer/RebalanceImpl.java#L2161-L2215)。

## 把刚才理解的机制拼起来

现在才有理由画出比第六章稍大的图：

```text
订单通知生产者
       |
       | 选择逻辑队列
       v
 Topic: OrderCreated
       |
  +----+---------------------------+
  |                                |
  v                                v
MessageQueue 0                 MessageQueue 1
  |                                |
  v                                v
ConsumeQueue 0                 ConsumeQueue 1
  |                                |
  +-------------+------------------+
                |
                v
         共享 CommitLog

消费组中按队列分配（经典分支示意）：

消费者 A <- MessageQueue 0
消费者 B <- MessageQueue 1
消费者 C <- 无队列可分
```

第一部分是消息定位关系，不是说读取消息的客户端一定先访问 ConsumeQueue 再直接访问文件；客户端发起消费请求，由 Broker 侧的读取路径访问存储层。

第二部分是消费组内**队列分配关系**，不是新建三个消息副本。队列里保存的消息，与谁负责消费，是两个不同层次的问题。

这时我们可以回答本章开头的疑问：

**索引降低了找到消息的成本；逻辑队列提供了能够分别组织、分配和处理消息的范围。** 两者解决的问题不同，所以改进其中一个，不能自动获得另一个。

## 最小验证：保持可靠性，再验证可并行

完整教学代码只使用 Python 标准库，建议 Python 3.9+。

把 `partitioned_message_queue.py` 和 `test_partitioned_message_queue.py` 放到同一个目录，然后依次运行：

```bash
python partitioned_message_queue.py demo
python -m unittest -v test_partitioned_message_queue.py
```

本章实际执行测试结果：

```text
Ran 13 tests in 0.961s

OK
```

其中比较重要的断言包括：

- `queue-0` 的写事务未结束时，`queue-1` 依旧可以领取和 ACK；
- 两个消费者同时领取同一条消息，仍然只有一个获得有效领取凭证；
- 不可见时间到期可以重新交付，旧凭证仍不能确认新一轮领取；
- 重启教学程序后，队列里的消息与已经写入的 ACK 状态仍可恢复；
- 两个队列的线程能够在同一轮进入业务阶段；
- 两个队列静态分配给三个消费者时，有一个消费者没有任务可分。

为了让验证真正可以重复运行，实验使用临时目录，不需要启动 RocketMQ、MySQL 或外部服务。以上是**Python 教学模型的运行结果**，并非真实 RocketMQ Broker 的性能或故障恢复实测。

### 这版教学程序仍然不能用于生产

它把正文放在一个 JSONL 日志，把索引放在两个二进制文件，把确认状态放在两个 SQLite 数据库。即使每份文件单独调用了 `fsync()`，**多个文件之间依然不是一个原子事务**。崩溃时可能出现正文已经追加、某个索引或状态却没有同步完成的情况。

此外，`publish()` 只为单个 Python 进程里的写操作提供互斥，不保证多个独立生产进程安全地同时追加；静态 `allocate_evenly()` 不会自动处理消费者上下线；固定 `queue_count` 之后也不能直接修改取模参数，否则旧消息的路由和新消息的路由可能不一致。

这些缺口都是真实问题，但它们并不妨碍本章验证一个明确结论：**不同逻辑队列可以为不同消费者提供独立的领取与消费进度范围。**

还有一个边界尤其容易误解：在经典集群队列分配模型下，超过队列数量的消费者实例可能没有队列；但这不等于「整个 RocketMQ 系统的最大处理线程数等于队列数」。一个消费者实例内部可以有多个处理线程，某些模式也有不同的投递与并发机制。关于并发性能的结论必须结合具体消费模式，而不能从这张教学图直接推出。

## 最后再运行一条同一订单的消息

我们已经让 `msg-1001` 进入 `queue-1`。如果同一个订单又产生一条消息：

```json title="新的订单事件（教学数据）"
{"messageId": "msg-1001-paid", "eventType": "OrderPaid", "orderId": 1001}
```

因为它使用相同的 `orderId`，教学路由规则会把它也放进 `queue-1`：

```text
queue-1:
  offset 0 -> msg-1001       (OrderCreated)
  offset 1 -> msg-1001-paid  (OrderPaid)
```

两个事件已经按产生时的先后顺序写入同一队列。这是不是就保证了业务处理顺序？

我们可以用第五章就有的不可见时间反驳这个直觉：

1. 消费者 A 领取 `OrderCreated`，尚未完成处理。
2. `OrderCreated` 因处于不可见时间而暂不可再领。
3. 消费者 A 或其他领取者取到了下一条 `OrderPaid`，并且先把它处理成功、写入 ACK。
4. `OrderCreated` 的领取超时，稍后被重新交付。

本章测试 `test_more_than_one_message_per_queue_does_not_imply_ordered_processing` 已实际验证这种状态：**后写入队列的消息可以先完成确认，先写入队列的消息之后才重新出现。**

于是，一个新的工程问题自然出现了：

> **如果订单支付消息已经处理完成，订单创建消息却还在等待重试，我们还能说这两条消息是顺序消费的吗？**

下一章，我们就在这个失败场景里区分「写入顺序」「投递顺序」和「业务完成顺序」，逐步研究为什么仅有同一队列还不足以保证完整的顺序消费语义，以及 RocketMQ 的顺序消息机制究竟额外做了什么。
