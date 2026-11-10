---
title: "分布式系统 10：一条消息为什么必须先选 Partition？"
sidebar_label: "十、一条消息为什么先选 Partition"
sidebar_position: 11
description: "从文章访问事件的顺序错乱出发，逐步推导 Kafka 的分区日志、按 Key 路由与分区内顺序，并沿 Kafka 3.9.0 源码追踪 Producer 到 Broker 的写入路径。"
slug: /distributed-systems/10-kafka-partition-log-order
tags: [distributed-systems, kafka, partition, log, source-code]
---

# 一条消息为什么必须先选 Partition？——Kafka 分区日志与顺序的源码精读

上一篇，我们在 MySQL 中研究了一笔文章访问记录如何安全提交：即使服务器恰好在写两份日志之间崩溃，也要有足够的恢复证据来判断事务的最终状态。

现在，业务有了新要求。我们不仅希望在数据库中保存访问记录，还希望让多个异步任务处理这些事件：一类任务统计访问趋势，另一类任务生成分析报表。

先不急着引入 Kafka。我们继续使用同一篇文章的两条事件：

```text
view-event-42-000101   post_id=42
view-event-42-000102   post_id=42
```

这里的 `000101` 和 `000102` 是本章案例人为指定的事件序号，不是 Kafka 自动生成的 Offset。为了方便观察，我们约定这两条事件应该依次进入处理流程。

现在出现一个问题：**如果事件越来越多，需要让多台机器并行处理，如何避免同一篇文章的事件顺序被打乱？**

> **固定源码版本**：[`apache/kafka`，标签 `3.9.0`](https://github.com/apache/kafka/tree/3.9.0)。本章只研究普通 Producer 写入一条非事务事件、Broker 将其追加到分区日志的正常路径。Key 分区映射、批处理与 Broker append 都在该版本中核对。后文 Python 实验是可执行的教学模型，**不是 Kafka 集成测试**。

## 一、只有一条日志时，问题很简单

最容易理解的实现是一份不断追加的列表：

```python
# 最小教学模型
log = []

log.append("post42#101")
log.append("post42#102")
log.append("post43#201")
```

它有个非常直观的性质：每条事件都占据一个确定的位置。

```text
位置 0：post42#101
位置 1：post42#102
位置 2：post43#201
```

处理程序只要按列表的位置从前往后读取，就会看到这份列表中的追加顺序。我们暂且不考虑并发写入、重试、网络和磁盘故障；这个简化模型只能说明**追加顺序**，不能自动推出所有客户端请求都有统一的业务时间顺序。

但当所有文章的访问事件都必须经过同一条日志、由同一个顺序追加点处理时，这个点可能成为容量或写入吞吐瓶颈。让更多机器独立工作，似乎是自然的下一步。

于是我们试着把一条日志拆成两条。

## 二、拆成两条日志，为什么同一篇文章的顺序反而乱了？

假设我们采用最直觉的轮转分配：第一条事件去日志 A，第二条去日志 B，第三条又回到 A。

```text
分配之前：
  post42#101 → post42#102

分配之后：
  日志 A：post42#101
  日志 B：post42#102
```

此时两份日志都能正常工作。日志 A 内部没有乱序，日志 B 内部也没有乱序。

但如果处理日志 B 的机器先完成读取：

```text
实际观察到：
  post42#102
  post42#101
```

同一篇文章的两条事件，居然以与原先追加意图相反的顺序进入了下游处理流程。

这并不是“哪台机器程序写错了”。真正的原因是：**我们把原本在同一序列中才能直接比较先后的两条事件，分散到了两个相互独立的序列里。**

因此，我们缺少的不是“让两条日志都更快”，而是一条约束：

> 对于需要保留相对顺序的同一业务实体，让它的事件始终进入同一条日志。

至此，我们才需要一种机制：**根据事件的 Key 来决定它应该进入哪条日志。**

## 三、让同一篇文章始终进入同一条日志

我们把 `post_id` 当作业务 Key。

在最小模型中，可以先用一个容易读懂的分配规则：

```python
# 教学化简：这不是 Kafka 的分区算法
def partition_for_article(post_id, partition_count):
    return post_id % partition_count
```

有两个日志时：

```text
post_id=42 → 42 % 2 = 0
post_id=43 → 43 % 2 = 1
```

于是：

```text
日志 0：
  offset 0：post42#101
  offset 1：post42#102

日志 1：
  offset 0：post43#201
```

现在处理日志 1 的机器可以比日志 0 更早开始、也可以更晚结束；这不影响日志 0 内两条 `post42` 事件的先后位置。

但我们需要明确：**这并没有恢复整个系统的全局顺序。**

`日志 0 / offset 1` 与 `日志 1 / offset 0` 之间，没有一个由这两个数字自然决定的全局先后关系。它们只是不同日志中的局部位置。

当一条消息先被分配到某条日志，再在该日志中取得位置时，就出现了 Kafka 中两个核心但并不复杂的概念：

- **Partition**：一个 Topic 下独立的、有序的追加日志。
- **Offset**：消息在所属 Partition 日志中的位置，不是整个 Topic 的全局序号。

这也解释了为什么 Kafka 不以“整个 Topic 上有一个唯一顺序”为基本扩展单位，而是选择在**单个 Partition 内建立顺序**，让多个 Partition 能够分别追加和处理消息。

接下来，我们要在真实源码里确认这两个动作：**Producer 先选择 Partition；Broker 再把数据追加到该 Partition 的日志并分配 Offset。**

## 四、Kafka Producer 在哪里决定 Partition？

先看一条最小的 Java 生产消息代码。假设 Topic 名称为 `article-view-events`：

```java
// 应用示意：需要已有 Kafka Broker 与正确配置的 KafkaProducer
ProducerRecord<String, String> record = new ProducerRecord<>(
    "article-view-events",
    "post:42",               // Key
    "view-event-42-000101"  // Value
);
producer.send(record);
```

我们没有在这段代码里指定 Partition 编号，只提供了 Key。

这意味着 Producer 必须自己决定消息应该送去哪条日志。打开固定版本的 [`KafkaProducer.java`](https://github.com/apache/kafka/blob/3.9.0/clients/src/main/java/org/apache/kafka/clients/producer/KafkaProducer.java)，找到 `send(ProducerRecord, Callback)`。

**真实源码节选：**

```java
return doSend(interceptedRecord, callback);
```

调用者是应用线程；输入是 `ProducerRecord` 及回调；`doSend()` 先拿到 Topic 元数据，序列化 Key 和 Value，然后决定目标 Partition，并尝试放入 Producer 的发送缓冲区。这里 `send()` 返回 `Future` 不能被理解成 Broker 已经完成了持久化或复制；那是另一个阶段的问题。

在同一文件中继续找 `partition(...)`，正常默认、提供 Key 且没有设置自定义 Partitioner 的路径是：

**真实源码节选：**

```java
return BuiltInPartitioner.partitionForKey(serializedKey, cluster.partitionsForTopic(record.topic()).size());
```

看这行就足以回答两个问题。

第一，参与计算的是**序列化后的 Key 字节**，不是 Java 对象的 `hashCode()`，也不是我们演示的 `post_id % 2`。

第二，计算还依赖当前 Topic 的 Partition 数量。

因此，我们需要继续查看实际分配公式。

打开 [`BuiltInPartitioner.java`](https://github.com/apache/kafka/blob/3.9.0/clients/src/main/java/org/apache/kafka/clients/producer/internals/BuiltInPartitioner.java)，找到 `partitionForKey(...)`。

**真实源码节选：**

```java
return Utils.toPositive(Utils.murmur2(serializedKey)) % numPartitions;
```

这个函数用 Murmur2 对 Key 字节求哈希，再映射到分区数量范围内。

这就把我们刚才的教学规则替换成了真实实现：

```text
应用提供 Key = "post:42"
        ↓
KafkaProducer 序列化 Key
        ↓
BuiltInPartitioner.partitionForKey()
        ↓
获得 Partition 编号
```

只要序列化规则、分区数量和默认分区策略不变，同一 Key 就会得到同一分区编号。**这保证的是路由稳定性，不保证 Key 唯一占用一个分区**；不同 Key 完全可能哈希到同一分区。

另外两条路径暂时只需要知道它们存在：如果显式指定 `record.partition()`，就按指定编号；如果使用自定义 Partitioner，就由自定义实现决定。对于未指定 Partition 且没有有效 Key 的默认路径，Kafka 3.9.0 还会在 `RecordAccumulator` 内使用内置的粘性/自适应分配逻辑，不能把它误写成“所有消息都按 Key 哈希”。

## 五、选好了 Partition，消息就已经写入 Broker 了吗？

还没有。

我们继续沿着 `KafkaProducer.doSend()` 追踪。

它会将记录交给：

```java
// 真实调用的核心形式；长参数列表由下文说明，非完整源码截取
accumulator.append(...);
```

严格说，上面带省略号的是**教学化简**，不是原样源码；完整参数包括 Topic、Partition、时间戳、序列化后的 Key/Value、Headers、回调及缓冲等待参数。

打开 [`RecordAccumulator.java`](https://github.com/apache/kafka/blob/3.9.0/clients/src/main/java/org/apache/kafka/clients/producer/internals/RecordAccumulator.java)，找到 `append(...)`。它会在确定的 Topic-Partition 对应队列中，把记录放进一个批次（`ProducerBatch`），或创建新批次。

为什么需要这一层？如果每次应用 `send()` 都立即发一个独立网络请求，大量小事件会让网络开销非常高。先在 Producer 内按 Partition 聚合，再批量发出，才有机会提高吞吐。

但这里还有一个容易忽略的边界：**“加入 RecordAccumulator”只是 Producer 侧缓冲，不等于消息已到 Broker。**

接下来由 [`Sender.java`](https://github.com/apache/kafka/blob/3.9.0/clients/src/main/java/org/apache/kafka/clients/producer/internals/Sender.java) 的发送线程负责网络阶段。沿 `sendProducerData()` → `sendProduceRequests()` → `sendProduceRequest()`，我们能看到它会将多个 Partition 的待发送批次组织成面向目标 Broker 的 Produce Request。

为什么可以按 Broker 组织网络请求？因为 Kafka 的集群元数据已经告诉 Producer：每个 Partition 当前的 Leader 在哪个 Broker 上。**Partition 是逻辑日志；Broker 是承载它的服务器。** 两者不是一回事。

在这一章，我们先让一条消息顺利到达该 Partition 的 Leader；Leader 故障或副本落后时该如何确认写入，留到下一章。

## 六、Broker 接到请求以后，为什么还要进入 Partition 对象？

打开 [`KafkaApis.scala`](https://github.com/apache/kafka/blob/3.9.0/core/src/main/scala/kafka/server/KafkaApis.scala)，找到 `handleProduceRequest()`。

它接收客户端的 Produce Request，解析每个 Topic-Partition 的消息批次，检查访问权限及请求有效性，然后调用 ReplicaManager。

**真实源码节选：**

```scala
replicaManager.handleProduceAppend(
```

`ReplicaManager` 为什么存在？因为一个 Broker 可以承载许多 Topic 的多个 Partition，且这些 Partition 不一定都由本机担任 Leader。通用请求处理层不应该自己维护每条分区日志的追加状态。

接着进入 [`ReplicaManager.scala`](https://github.com/apache/kafka/blob/3.9.0/core/src/main/scala/kafka/server/ReplicaManager.scala)。正常非事务生产请求的路径会继续调用 `appendRecords(...)`，进入本地追加逻辑 `appendToLocalLog(...)`。

关键动作是先查找目标 Partition 对象：

**真实源码节选：**

```scala
val partition = getPartitionOrException(topicPartition)
```

随后对这个 Partition 调用 `appendRecordsToLeader(...)`。

注意这里的动词 **ToLeader**：即使消息到达了某台 Broker，也不能随意附加到那台服务器上的任意副本。普通 Produce 请求需要在目标 Partition 的本地 Leader 日志上执行追加。如果它已经不是 Leader，应返回相应错误，而不是偷偷写入 Follower。

继续打开 [`Partition.scala`](https://github.com/apache/kafka/blob/3.9.0/core/src/main/scala/kafka/cluster/Partition.scala)，找到 `appendRecordsToLeader(...)`。

这里存在关键的 Leader 检查和下游调用：

**真实源码节选：**

```scala
leaderLogIfLocal match {
  case Some(leaderLog) =>
```

当本机拥有相应的 Leader 日志时，才会继续调用 `leaderLog.appendAsLeader(...)`；否则会进入 `NotLeaderOrFollowerException` 的错误路径。

这让我们看到一个重要分层：

```text
KafkaApis：理解网络请求与权限校验
     ↓
ReplicaManager：找到目标 Topic-Partition
     ↓
Partition：确认本机是否可作为 Leader 处理
     ↓
UnifiedLog：实际追加日志
```

**这些层并不是为了“增加架构复杂度”而出现。** 它们分别回答：请求是否合法？该找哪份日志？这台服务器有没有资格写？具体怎么把消息接到日志尾部？

## 七、Offset 在哪里产生？

最后打开 [`UnifiedLog.scala`](https://github.com/apache/kafka/blob/3.9.0/core/src/main/scala/kafka/log/UnifiedLog.scala)，找到 `appendAsLeader(...)`。

它会调用统一追加逻辑 `append(...)`。在持有日志追加锁、进入正常 Leader 分配 Offset 路径后，可以找到：

**真实源码节选：**

```scala
val offset = PrimitiveRef.ofLong(localLog.logEndOffset)
appendInfo.setFirstOffset(offset.value)
```

这里 `localLog.logEndOffset` 表示这份**Partition 日志**的下一处日志位置。后续通过记录验证与 Offset 分配逻辑，把本批次中的记录追加到日志，并返回首尾 Offset 等信息。

这能解释前面的模型：

```text
article-view-events-0
  offset 0 → post42#101
  offset 1 → post42#102

article-view-events-1
  offset 0 → post43#201
```

`Offset=0` 可以同时出现在不同 Partition 中，不会冲突，因为一条消息的位置必须连同 **Topic + Partition + Offset** 才能被唯一描述。

另一个关键细节：上面看到的 Leader append，不表示这条消息已经达到可供所有消费者读取的提交位置。Kafka 在副本复制和 High Watermark 方面还有独立的约束；本章只证明写入如何选日志并建立该日志内部的位置。

## 八、实际运行一次：哪里有序，哪里没有序？

本章提供一个可直接运行的实验：

```bash
python3 examples/kafka_partition_order_lab.py
```

不需要 Kafka、Docker 或 API Key。实验中 `article_id % partition_count` 只是教学映射，不能与真实 Kafka Murmur2 算法混同。

**实际运行输出：**

```text
单一日志: [(0, 'post42#101'), (1, 'post42#102'), (2, 'post43#201')]
逐条轮转+先读分区1: ['post42#102', 'post42#101']
按文章路由 分区0: [(0, 'post42#101'), (1, 'post42#102')]
按文章路由 分区1: [(0, 'post43#201')]
教学映射扩容 post44: 2分区->0, 3分区->2
验证完成：分区内有独立 offset；跨分区观察顺序不固定。
```

第一种输出说明：单日志有一个统一的追加位置。

第二种输出故意让不同分区以相反的顺序被观察，说明把同一 Key 轮转分散到多个分区，会失去局部先后关系。

第三种输出说明：按稳定 Key 路由后，同一篇文章的两条事件仍处在同一分区，并按该分区的追加顺序排列。

最后一行则暴露了一个新的边界：**当 Partition 数量变化时，简单哈希取模可能把同一个 Key 的后续事件路由到另一分区。** Kafka 的默认 Key 分区计算也依赖 `numPartitions`，因此为已有 Topic 增加 Partition 时，不能简单假设 Key 的历史映射始终不变。

需要特别说明：这个程序只模拟按分区追加和按分区读取的行为。它没有启动 Kafka、没有模拟 Producer 的重试和幂等性，也没有验证 Broker 吞吐或实际 Offset 分配的所有边界条件。

## 九、分区内顺序到底保证了什么？

现在我们可以准确说出 Kafka 分区设计的收益与限制。

**它解决的问题：** 将一个 Topic 的消息分摊到多个独立日志中，允许不同分区在不同 Leader 上并行追加，同时在**单个 Partition** 内为被接受的消息建立日志顺序。

**它没有自动解决的问题：**

首先，同一个 Topic 的不同 Partition 之间没有统一的全局 Offset，也没有天然的跨分区消费顺序。

其次，同一个 Key 能被路由到同一分区，不等于“来自多个并发 Producer 的业务事件必然按真实世界发生时间排列”。Broker 建立的是消息抵达并被追加的顺序；业务上的因果先后仍要通过生产端约束或业务序号来处理。

再次，如果更改分区数量、Key 序列化方式或分区器，过去与未来的同 Key 记录就可能进入不同 Partition。对严格依赖同 Key 顺序的系统，这需要专门的迁移策略，而不是把分区数量随意加大。

最后，顺序也不等于持久性。即使 Leader 给一条记录分配了 Offset，我们还没有解释：当 Leader 在其他副本赶上之前故障时，这条记录能不能安全保留？

## 十、下一篇自然出现的问题

回看这次演化：

```text
一条单机日志
  → 只有一个追加点，扩展受限
两条独立日志
  → 同一文章的事件可能分散并被乱序观察
按 Key 选择 Partition
  → 同一文章在稳定映射下进入同一分区
Leader 追加日志、分配 Offset
  → 每个 Partition 有自己的顺序
```

到这里，我们终于理解了 Kafka 为什么要先选 Partition，才能把消息交给该 Partition 的 Leader 日志。

但这里故意没有回答第九章之后另一个重要问题：**Kafka 的 Leader 把消息追加成功，能不能马上告诉 Producer“成功”？**

假设 `article-view-events-0` 的 Leader 已经收到了 `post42#102`，但它的 Follower 还只有 `post42#101`。如果 Leader 此时故障，新 Leader 又该如何决定哪条消息能够保留？

这和我们在 Redis 第二篇遇到的问题很相似，但 Kafka 的答案会明显不同。

**下一篇：《Kafka 为什么要等待 ISR？——从 Leader 本地追加到 acks=all》**。我们将沿用本章同一条 Partition 日志，在一个具体的故障窗口中引入副本跟进与写入确认，而不是先背诵配置参数。

---

### 固定版本源码参考

- [Kafka 3.9.0 `KafkaProducer.java`](https://github.com/apache/kafka/blob/3.9.0/clients/src/main/java/org/apache/kafka/clients/producer/KafkaProducer.java) — `send()`、`doSend()`、`partition()`；输入来自应用消息，输出进入 Producer 缓冲。
- [Kafka 3.9.0 `BuiltInPartitioner.java`](https://github.com/apache/kafka/blob/3.9.0/clients/src/main/java/org/apache/kafka/clients/producer/internals/BuiltInPartitioner.java) — `partitionForKey()`；从 Key 字节算出分区编号。
- [Kafka 3.9.0 `RecordAccumulator.java`](https://github.com/apache/kafka/blob/3.9.0/clients/src/main/java/org/apache/kafka/clients/producer/internals/RecordAccumulator.java) — `append()`；按 Partition 暂存并批量组织记录。
- [Kafka 3.9.0 `Sender.java`](https://github.com/apache/kafka/blob/3.9.0/clients/src/main/java/org/apache/kafka/clients/producer/internals/Sender.java) — `sendProducerData()`、`sendProduceRequest()`；生成网络请求。
- [Kafka 3.9.0 `KafkaApis.scala`](https://github.com/apache/kafka/blob/3.9.0/core/src/main/scala/kafka/server/KafkaApis.scala) — `handleProduceRequest()`；请求解码、校验后进入 ReplicaManager。
- [Kafka 3.9.0 `ReplicaManager.scala`](https://github.com/apache/kafka/blob/3.9.0/core/src/main/scala/kafka/server/ReplicaManager.scala) — `appendToLocalLog()`；定位具体 Partition。
- [Kafka 3.9.0 `Partition.scala`](https://github.com/apache/kafka/blob/3.9.0/core/src/main/scala/kafka/cluster/Partition.scala) — `appendRecordsToLeader()`；检查本地 Leader 并调用日志追加。
- [Kafka 3.9.0 `UnifiedLog.scala`](https://github.com/apache/kafka/blob/3.9.0/core/src/main/scala/kafka/log/UnifiedLog.scala) — `appendAsLeader()` / `append()`；日志追加与 Offset 分配。

**验证说明**：上面的真实源码入口和关键语句已按 Kafka `3.9.0` 标签核对；Python 教学模型已运行并核对输出。未启动 Kafka 集群，未进行真实生产、消费与故障切换集成测试。
