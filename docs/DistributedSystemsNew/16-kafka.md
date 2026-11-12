---
title: "16｜Kafka 为什么把元数据共识和消息复制分开？"
sidebar_label: "十六、Kafka 为什么把共识和复制分开"
sidebar_position: 18
description: "Kafka 把元数据共识与消息复制分开：KRaft 管元数据，ISR 与高水位决定消息何时可见。"
slug: /distributed-systems-new/16-kafka
---

# 16｜Kafka 为什么把元数据共识和消息复制分开？

现在库存预留已经成功，系统需要发布一条事件：`StockReserved(order=o-1, sku=book, qty=2)`。支付、物流和审计服务订阅这条事件。与缓存不同，这条事件若丢失，可能导致下游永远不知道订单曾成功预留。我们要理解 Kafka 的复制策略，首先不要把两种完全不同的日志混在一起。

## 1. 一个 Partition 就是一条有顺序的业务日志

假设所有与订单 `o-1` 相关的事件进入同一个 Partition：

```text
Partition-0:
  offset=0  StockReserved(o-1,book,2)
  offset=1  PaymentAuthorized(o-1)
  offset=2  ShipmentCreated(o-1)
```

这些消息有 Partition 内的顺序；不同 Partition 之间通常没有天然的统一业务全序。这与我们之前学的“同一状态机按相同日志顺序应用命令”很相似，但 Kafka 的主要产品能力是持久化和传递消息日志，不是替库存数据库执行库存事务。

## 2. Partition Leader 和副本复制解决业务消息故障

假设 Partition-0 在 Broker A 上有 Leader，B、C 保持副本。客户端发送一条事件，Leader 将其写入分区日志，Follower 复制。

Kafka 有 ISR（In-Sync Replicas），表示当前符合同步条件的副本集合。生产者的 `acks` 决定何时向客户端确认：`acks=1` 允许 Leader 根据本地写入回复；`acks=all` 要求满足 ISR 的复制确认约束，并配合 `min.insync.replicas` 指定成功写入所需的最低副本数量。

注意：**`min.insync.replicas=2` 不等于 ISR 有三个成员时只等两个确认就马上返回。** `acks=all` 要满足当前 ISR 中副本的确认条件。如果其中一个副本落后并已被移出 ISR，剩余 ISR 满足最小值时才可能继续成功写入。

```python title="完整实验：根据 ISR 与最小副本数量判断是否可确认"
def can_ack_all(isr, acknowledged, min_isr):
    isr = set(isr)
    acknowledged = set(acknowledged)
    return len(isr) >= min_isr and isr.issubset(acknowledged)

assert not can_ack_all({"A", "B", "C"}, {"A", "B"}, 2)
assert can_ack_all({"A", "B", "C"}, {"A", "B", "C"}, 2)
assert can_ack_all({"A", "B"}, {"A", "B"}, 2)
assert not can_ack_all({"A"}, {"A"}, 2)
print("ISR acknowledgment conditions shown")
```

这段脚本只是确认条件的教学模型，不代表 Kafka 完整的写入可见性、日志持久化、Leader 选举以及具体错误码规则。生产环境的保证还需要考虑干净选主、磁盘故障与配置等条件。官方参考：[Kafka topic 配置](https://kafka.apache.org/40/generated/topic_config.html)。

## 3. 谁决定 Partition Leader 是哪台 Broker？

Kafka 集群有很多 Partition。如果 Broker A 宕机，集群必须决定 Partition-0 由 B 还是 C 接管，还要更新客户端可查询的集群元数据。

过去 Kafka 依赖 ZooKeeper 保存这类协调元数据。现代 Kafka 使用 KRaft：Controller Quorum 维护集群元数据日志，通过基于 Raft 的机制选举并提交元数据变更。Kafka 4.0 已不再支持 ZooKeeper 模式。

我们现在可以把它分成两条路径：

```text
控制面：Controller Quorum → KRaft 元数据日志
          维护 Broker、Topic、Partition Leader 等信息

数据面：Producer → Partition Leader → ISR Followers
          保存 StockReserved 等业务消息
```

**KRaft 的 Raft 多数派不是每条 Kafka 业务消息的复制确认机制。** 这句话是理解 Kafka 设计的关键。

## 4. 真实源码精读：为什么 KRaft 刚成为 Leader 就要写控制记录？

源码基线固定为 **Apache Kafka 4.0.0**，仓库 `apache/kafka`，文件 [`raft/src/main/java/org/apache/kafka/raft/KafkaRaftClient.java`](https://github.com/apache/kafka/blob/4.0.0/raft/src/main/java/org/apache/kafka/raft/KafkaRaftClient.java)，方法 `onBecomeLeader(long currentTimeMs)`。

**真实源码微型节选（省略上下文）**：

```java title="KafkaRaftClient.onBecomeLeader()"
LeaderState<T> state = quorum.transitionToLeader(endOffset, accumulator);
log.initializeLeaderEpoch(quorum.epoch());
state.appendStartOfEpochControlRecords(quorum.localVoterNodeOrThrow(), currentTimeMs);
```

这三行分别对应：将本地 Quorum 状态变成 Leader、在日志中记录新 Leader 的 epoch、写入本任期的控制记录。源码中的注释解释了一个重要原因：KRaft 在新 Leader 任期尚未写入记录之前，不能只靠旧任期的进度直接推进新的 High Watermark；因此尽快写入本任期控制记录，帮助后续提交推进。

输入中的 `endOffset` 来自当前日志末尾，`quorum.epoch()` 是选举后的 epoch，`accumulator` 负责积累待追加的日志批次。后续由复制/拉取与 High Watermark 机制决定哪些元数据记录可对监听者可见。**这里描述的是真实 KRaft 元数据复制链，而非业务 Partition 的 Producer `acks` 路径。**

## 5. 为什么 KRaft 的复制又和教科书 Raft 有差异？

教科书式 Raft 常以 Leader 向 Follower 主动推送 `AppendEntries` 解释复制。Kafka Raft 的实现结合了 Kafka 已有的日志拉取模型，Follower 可以通过 Fetch 拉取元数据记录。源码类注释明确指出了这种由 Follower Fetch 驱动的复制、日志调和和与标准 Raft 路线的差别。

这恰恰说明：**协议的安全目标可以相同，消息传输和工程接口未必完全一样。** 真正需要核实的是任期、选举安全、日志匹配、提交和恢复是否保持所需不变量，而不是类名是否叫 `AppendEntries`。

## 6. Kafka 也不能自动解决库存与消息的原子关系

假设库存数据库已经提交预留，但事件还没发送到 Kafka，订单服务就崩溃：库存变成 8，下游却没有收到 `StockReserved`。即使 Kafka 自己复制得再可靠，它也不知道这条事件原本应该存在。

所以跨边界业务仍需要补充设计：本地事务中记录 Outbox 事件、异步可靠投递，以及消费者按事件 ID 幂等处理。不要把 Kafka 的幂等生产者或事务功能理解成任意数据库与 Kafka 之间都天然存在一个全局原子事务。

## 7. 用全系列的一个故障问题检验差异

假设某个 Leader 写入后立即宕机：

| 系统及操作 | 客户端收到成功意味着什么 |
| --- | --- |
| ZooKeeper 协调写入 | 按 ZAB 的有序复制与提议确认规则提交 |
| Redis Cluster 普通 `SET` | 主节点可能已返回，但副本仍可能落后，存在丢写窗口 |
| Kafka Partition `acks=all` | 在符合 ISR、min ISR 和安全选主等约束下，提供配置所描述的消息复制保护 |
| Kafka KRaft 元数据更新 | 由 Controller Quorum 的 Raft 元数据日志提交约束保护 |

我们终于能准确理解：**不同产品不是不知道如何实现共识，而是在不同数据路径上刻意提供不同的确认、复制和可用性语义。**

## 8. 为什么 Kafka 的两个日志不应该混成一条？

KRaft 元数据日志记录的是集群怎样运行：哪些 Broker 在线、哪些 Partition 存在、哪个 Broker 担任哪个 Partition 的 Leader。业务消息日志保存的是用户真正发送的事件，例如 `StockReserved`。两者都使用顺序日志，却承担不同的职责。

如果把每条业务消息都放进一个全局 Controller 元数据共识日志，所有 Topic Partition 的写入都会在不必要的全局路径上协调，难以利用 Partition 的并行吞吐优势。Kafka 让大量业务消息分散到 Partition Leader 管理的日志上，让 Controller 主要负责元数据决策，体现了控制面与数据面的分离。

## 9. 同样的“谁说了算”，在 Kafka 中出现两次

第一层是 Controller Quorum 选出当前 KRaft Controller Leader，负责管理集群元数据。第二层是每个 Partition 有自己的 Leader，负责接受该分区的生产者写入、维护复制与 ISR 状态。两者都涉及领导权，却不能用一个“Kafka Leader”概念混写。

当 Broker A 故障，Controller 可以协调把 Partition 的 Leader 切到符合条件的副本上；但这项元数据决策不等于替生产者重放消息，也不等于下游消费者完成了处理。状态变化跨越多个组件时，应分别追踪控制信息、数据日志和客户端可见结果。

## 10. 为什么 `acks=all` 仍不能直接承诺“整个订单不丢”？

即使 Kafka 消息写入满足 ISR 确认，库存数据库中的预留事务与发送 Kafka 消息的动作仍处于两个独立系统。可能先提交库存事务、后发送消息时崩溃；也可能消息已经被 Kafka 确认，但客户端没收到订单最终响应，于是重复发送业务请求。

因此实际系统需要在订单与事件之间建立可靠关系，例如事务性 Outbox 把待发送事件与库存预留记录放在同一数据库事务中，再由发布者读取 Outbox 并重试发送。消费者也要根据事件 ID 幂等处理，不能因为“Kafka 保证事件写入”就放弃业务侧防重。

## 11. 真实源码为什么先关注元数据 epoch，而不是 Producer 的 acks？

`KafkaRaftClient.onBecomeLeader()` 操作的是 Controller 元数据复制状态，不是某个普通 Topic Partition 的消息生产请求。它把本地状态切到 Leader、初始化 Leader Epoch，并尽快追加本 epoch 的控制记录。这个动作与我们第 7 章讨论的“旧任期历史不能随意当作新任期已提交依据”有直接联系。

若要研究普通业务消息，需要转而阅读 Kafka Broker 中 Partition 的追加、Follower Fetch、ISR 更新和 High Watermark 相关路径。**切换源码层次必须由问题推动**：先研究谁决定分区领导权，再研究一个业务消息如何被复制和确认。把 KRaft Controller 的提交规则直接当成 Producer `acks=all` 的实现，属于跨层错误。

## 12. 最后一组架构取舍：哪个状态值得为它支付共识成本？

ZooKeeper 面向少量高价值协调状态，倾向严格有序复制；Redis Cluster 对普通可重建 Key 默认使用异步复制，并单独协调故障转移的槽位所有权；Kafka 对 Controller 元数据使用 KRaft，对各 Partition 消息使用 ISR 与可配置的确认策略。

于是学习成果不再是“这三个产品分别用了哪些算法”，而是能回答：**它们在什么操作上需要哪种安全保证？又为什么不把最昂贵的保证强加到所有路径上？** 这个问题比背诵 Raft/ZAB/Gossip 的名称更接近真实系统设计。

## 13. 如果 Controller 失败，普通 Partition 消息会全部停掉吗？

Controller 管理元数据和领导权变化，并不意味着每条业务消息都必须在写入时与 Controller 进行一次 Raft 交互。已有健康 Partition Leader 的正常消息写入，主要按照分区的数据复制与 ISR 规则推进。Controller 不可用会影响某些元数据变更和故障转移能力，但不能粗暴地理解成“每条 Produce 都要等待 Controller 多数派”。

这个区分帮助我们定位 Kafka 的性能：大量 Partition 可以分散业务日志写入，Controller 负责必要的元数据协调。若误把两条路径画成一条，就无法解释 Kafka 为什么具备分区并行吞吐能力。

## 14. Kafka 消息顺序的边界在哪里？

同一 Partition 的 Offset 提供有序日志位置。跨不同 Partition 通常不存在一个全局业务顺序：两个 SKU 事件落在两个 Partition，消费者不能只比较 Offset 数字就判断哪个事件在整个系统里先发生。

这意味着使用 Kafka 做库存事件时，应选择合适的分区键，让需要保持同一业务实体顺序的事件尽量进入同一 Partition；对于跨商品的业务事务，仍要依赖上层协调或事件工作流。这与前面分片键如何影响原子业务边界，是同一个结构性问题。

## 15. 全系列收束：拿到陌生系统时如何分析？

看到一个新的分布式系统，先不要寻找 `Raft`、`Gossip`、`Leader` 的类名。从一个真实写入出发依次追问：谁有权接收？先持久化到哪里？什么时候把结果回复客户端？哪些节点保存了什么？故障转移后谁能获选？旧 Leader 怎样被隔离？落后副本怎样恢复？读取是否允许陈旧？重复请求怎样去重？数据量扩大后如何迁移？

这些问题就是本系列从单机库存服务一步一步推导出的共同设计框架。协议名称是答案的一部分，不是推导的起点。

参考：[Kafka KRaft 官方运维文档](https://kafka.apache.org/40/operations/kraft/)、[Kafka 4.0.0 Raft 客户端源码](https://github.com/apache/kafka/blob/4.0.0/raft/src/main/java/org/apache/kafka/raft/KafkaRaftClient.java)、[Raft 论文](https://raft.github.io/raft.pdf)。


---

[← 上一章：15｜Redis Cluster 为什么不对普通 SET 使用多数派提交？](./15-redis-cluster.md) · [下一章：机制全景｜四种真实系统对照 →](./17-mechanisms-overview.md)
