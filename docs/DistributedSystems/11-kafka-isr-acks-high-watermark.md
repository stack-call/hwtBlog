---
title: "分布式系统 11：Leader 写入成功，为什么还不能回复 Producer？"
sidebar_label: "十一、Leader 写入成功为何还不能回复"
sidebar_position: 12
description: "从 Kafka 单分区日志的故障窗口出发，逐步推导 ISR、High Watermark、acks=all 和 min.insync.replicas，并沿 Kafka 3.9.0 源码验证写入确认的真实路径。"
slug: /distributed-systems/11-kafka-isr-acks-high-watermark
tags: [distributed-systems, kafka, replication, isr, source-code]
---

# Leader 写入成功，为什么还不能回复 Producer？——Kafka ISR、High Watermark 与 acks=all 源码精读

上一篇，我们为文章访问事件选择了 Partition。现在，同一篇文章的两条事件能够稳定地落在 `article-view-events-0` 中，并获得该分区内部的追加顺序：

```text
article-view-events-0
  offset 0 : post42#101
  offset 1 : post42#102
```

我们还没有回答一个更危险的问题：**如果 Leader 已经把 `post42#102` 放进自己的日志，但其他 Broker 还没有得到它，此时 Leader 突然故障，Producer 能不能认为写入已经成功？**

这听起来像第二篇 Redis 的异步复制问题。不过，这次我们不直接拿 Redis 的 `WAIT` 套到 Kafka 上，而是沿着 Kafka 的日志结构，从一次失败场景推导它自己需要的确认规则。

> **源码版本与边界**：固定 [Apache Kafka `3.9.0`](https://github.com/apache/kafka/tree/3.9.0)。讨论单个 Partition、非事务 Produce、普通 Leader/Follower 复制与干净的 Leader 切换。真实源码节选均从这一标签核对。Python 示例是单进程教学模拟，未部署 Kafka Broker，不能代表网络、磁盘与 Controller 的完整行为。

## 一、Leader 写入本地日志，为什么暂时还不够？

先假设一个 Partition 只有一个 Leader `A` 和一个 Follower `B`。

```text
时间 T0
  A: [post42#101]
  B: [post42#101]

时间 T1：Producer 发送 post42#102
  A: [post42#101, post42#102]
  B: [post42#101]
```

最简单的设计，是让 A 追加完成就回复成功：

```python
# 概念伪代码；不是 Kafka 实现

def produce_to_leader(event):
    leader_log.append(event)
    return "OK"
```

它并非毫无道理：不必等待其他机器，成功响应很快。如果我们的承诺只是“Leader 的本地追加已经完成”，它确实完成了这个承诺。

可是现在让 A 在 T1 后失联。如果 B 需要接管，它能拿出的日志只有 `post42#101`。Producer 曾收到的 `OK`，并不能凭空让 `post42#102` 出现在 B 上。

我们之前通过 Redis 学会了一件事：**本地写入完成与副本获得数据，是两个独立的时刻。**

因此，新的要求是：对于不能轻易丢失的 Kafka 消息，**在返回成功前，先确定哪些副本已经跟上这条消息。**

但这立刻又带来一个问题：一个 Partition 可能有三个甚至更多副本；是否必须等待所有配置过的副本？

## 二、为什么不能永远等“全部副本”？

假设 `article-view-events-0` 有三个副本：

```text
A：Leader  负责追加
B：Follower 正常追赶
C：Follower 已经离线很久
```

如果成功条件是 A、B、C 三者全部确认，那么 C 长时间失联会让写入一直无法成功，即使 A 和 B 都正常。

我们真正想等待的，不是“配置列表中全部机器”，而是**当前仍然足够接近 Leader、可以被认为同步的副本**。

这就是本章第一次需要引入的术语：**ISR（In-Sync Replicas）**。

在这个例子里，Partition 的全部副本是 `{A, B, C}`，但当前 ISR 可以是 `{A, B}`；C 仍是分区副本，只是暂时不在 ISR 中。

```text
全部副本：A  B  C
当前 ISR：A  B

不在 ISR：C（跟进不足或失去联系）
```

ISR 不是一个从部署之日起永久不变的名单。Follower 是否持续跟上，由复制进度与追赶时间等条件决定，成员变更由集群的元数据协调流程管理。

因此，Kafka 可以为 `acks=all` 选择一个实际可操作的规则：**等待当前同步副本集合所要求的复制进度，而不是永远等待每个配置过的副本。**

这当然不意味着把落后的副本删除就没有代价；如果 ISR 最后只剩 Leader，确认安全性会重新下降。我们后面会用一个具体反例迫使系统增加额外限制。

## 三、怎样判断 ISR 是否都跟上了这条消息？

上一篇已经学习 `Offset`：每个 Partition 内部拥有自己的日志位置。现在我们需要再小心区分两个位置。

假设 A 的日志中有两个事件：

```text
offset 0 : post42#101
 offset 1 : post42#102
```

追加完毕后，A 的 **Log End Offset（LEO）** 是 `2`，它是**下一条待写记录的位置**，不是最后一条记录的 offset。

如果 B 只有 offset `0` 的记录，那么 B 的 LEO 是 `1`。

```text
       A（Leader）          B（Follower）
       ──────────          ────────────
  0    post42#101          post42#101
  1    post42#102          （尚未复制）
       LEO = 2             LEO = 1
```

假设 ISR = `{A, B}`。仅看 A 的 LEO=2，无法证明 B 已经获得 offset 1 的消息。

我们可以先定义一个很简单的**复制完成边界**：只有 ISR 中相关副本都达到某个位置，这个位置之前的消息才能被认为已越过复制确认门槛。

Kafka 把这条边界称为 **High Watermark（HW）**。对于本章的简单、稳定 ISR 场景，它可以直观理解成 ISR 中各副本复制进度的下界：

```text
A 的 LEO = 2
B 的 LEO = 1
当前 HW  = 1

=> offset 0 在 HW 之前
=> offset 1 尚未跨过 HW
```

当 B 继续复制 `post42#102`：

```text
A 的 LEO = 2
B 的 LEO = 2
当前 HW  = 2
```

现在 offset `1` 的消息才越过了 HW。

为什么不直接检查“B 是否收到了 `post42#102`”这条业务字符串？因为 Kafka 的 Broker 不应该靠理解每种业务消息来判断复制是否完成，它只需比较日志进度。

这里还得到一个额外收益：在普通非事务消费路径上，Consumer 不能把 Leader 中尚未越过 HW 的尾部数据当作已经稳定可见的消息。**日志已经局部追加**和**消息已经可供正常消费**，不是同一个状态。

> 精确边界：Kafka 3.9.0 的 HW 实现会考虑 `maximalIsr`、正在进入 ISR 的副本和最小 ISR 限制，不等于在任何时刻机械地对一个固定列表取 `min(LEO)`。当前章节的两副本算例先固定 ISR，等掌握主路径再看源码中的额外保护。

## 四、先用一个最小实验观察这条边界

我们依然只处理 `post42#102`，并不模拟真正的 Kafka 网络。

**教学化简：**每个节点上的列表代表它的分区日志；`sync()` 代表 Follower 已经完成本次追赶；人为修改 `isr` 代表 ISR 变更已经得到集群协调许可。

将下面代码保存为 `kafka_isr_lab.py`，使用 Python 3 直接运行，无需 API Key、Docker 或 Kafka：

```python
class PartitionLab:
    def __init__(self, min_isr=2):
        self.logs = {
            "A": ["post42#101"],
            "B": ["post42#101"],
            "C": ["post42#101"],
        }
        self.isr = {"A", "B", "C"}
        self.min_isr = min_isr

    def leo(self, broker):
        return len(self.logs[broker])

    def hw(self):
        # 教学模型：固定 ISR 时取同步集合的最小 LEO
        if len(self.isr) < self.min_isr:
            return None
        return min(self.leo(broker) for broker in self.isr)

    def produce(self, message, acks="all"):
        if acks == "all" and len(self.isr) < self.min_isr:
            return "NOT_ENOUGH_REPLICAS", None

        self.logs["A"].append(message)
        required_offset = self.leo("A")
        if acks == "1":
            return "OK", required_offset
        return "PENDING", required_offset

    def replicate_to(self, follower):
        self.logs[follower] = self.logs["A"].copy()

    def check_all(self, required_offset):
        if len(self.isr) < self.min_isr:
            return "NOT_ENOUGH_REPLICAS_AFTER_APPEND"
        return "OK" if self.hw() >= required_offset else "PENDING"

    def visible(self):
        boundary = self.hw()
        return self.logs["A"][:boundary] if boundary is not None else []


def main():
    p = PartitionLab(min_isr=2)
    acks_1, required = p.produce("post42#102", acks="1")
    assert acks_1 == "OK"
    assert p.leo("A") == 2
    assert p.hw() == 1
    assert p.check_all(required) == "PENDING"
    print("① Leader 已追加：acks=1", acks_1,
          "LEO", p.leo("A"), "HW", p.hw())

    p.replicate_to("B")
    assert p.hw() == 1  # C 还没跟上
    assert p.check_all(required) == "PENDING"
    print("② 只有 B 跟上：ISR", sorted(p.isr),
          "HW", p.hw(), "acks=all", p.check_all(required))

    p.replicate_to("C")
    assert p.hw() == 2
    assert p.check_all(required) == "OK"
    assert p.visible()[-1] == "post42#102"
    print("③ B、C 都跟上：HW", p.hw(),
          "acks=all", p.check_all(required))

    q = PartitionLab(min_isr=2)
    _, required = q.produce("post42#102")
    q.replicate_to("B")
    q.isr.remove("C")  # 模拟已完成的 ISR 收缩
    assert q.check_all(required) == "OK"
    print("④ C 不在 ISR：ISR", sorted(q.isr),
          "HW", q.hw(), "acks=all", q.check_all(required))

    r = PartitionLab(min_isr=2)
    r.isr = {"A"}
    result, required = r.produce("post42#102")
    assert result == "NOT_ENOUGH_REPLICAS" and required is None
    assert r.leo("A") == 1  # 追加前拒绝
    print("⑤ ISR 只剩 A：", result, "LEO", r.leo("A"))

    s = PartitionLab(min_isr=2)
    _, required = s.produce("post42#102")
    assert s.check_all(required) == "PENDING"
    print("⑥ 模拟等待超时：REQUEST_TIMED_OUT；本地 LEO", s.leo("A"))


if __name__ == "__main__":
    main()
```

在这个实验里，第一步 `acks=1` 已经返回 `OK`，但 HW 仍为 `1`；第二步只有 B 赶上时，C 还在 ISR 中，所以 `acks=all` 仍不能完成；第三步 B 和 C 都赶上，HW 才前进到 `2`。

**最值得注意的是第四步**：如果 C 已被合法移出 ISR，等待条件就不再把 C 当成当前必须追赶的副本，而 A、B 继续组成 ISR。这个行为是动态同步集合存在的意义之一。

**但第五步暴露了新的危险：**如果 ISR 缩到只剩 A，那 `acks=all` 是否应该允许“只等我自己”就成功？

还不能马上回答。先去看 Kafka 的真实写入链路。

## 五、真实源码：Broker 明明追加完成，为什么还要挂起响应？

固定版本：[Kafka 3.9.0 — `ReplicaManager.scala`](https://github.com/apache/kafka/blob/3.9.0/core/src/main/scala/kafka/server/ReplicaManager.scala)。

上一篇沿 Producer → `KafkaApis.handleProduceRequest()` → `ReplicaManager` → `Partition.appendRecordsToLeader()` 看到了本地追加。我们从这个已经理解的入口继续。

### 5.1 本地追加与回复不是同一个函数动作

在 `ReplicaManager.appendRecords()` 的正常路径中：

**真实源码节选（Kafka 3.9.0）：**

```scala
val localProduceResults = appendToLocalLog(internalTopicsAllowed = internalTopicsAllowed,
  origin, entriesPerPartition, requiredAcks, requestLocal, verificationGuards.toMap)
```

这里的 `entriesPerPartition` 来自 Broker 已解析的 Produce 请求，`requiredAcks` 是请求指定的确认级别。`appendToLocalLog()` 会定位对应的 `Partition` 并执行本地追加。

但是，拿到 `localProduceResults` 后，函数并不总是立即回复。它还会执行：

**真实源码节选：**

```scala
maybeAddDelayedProduce(
  requiredAcks,
  delayedProduceLock,
  timeout,
  entriesPerPartition,
  localProduceResults,
  produceStatus,
  responseCallback
)
```

`responseCallback` 是后续形成 Produce 响应的回调。它的存在说明：**本地追加结果产生以后，Broker 可以稍后再结束这个请求。**

此处还没必要研究每个回调如何回到网络线程，我们目前只关心它为什么没有马上运行。

### 5.2 什么情况下进入等待？

继续在同一文件看 `delayedProduceRequestRequired()`。

**真实源码节选：**

```scala
requiredAcks == -1 &&
entriesPerPartition.nonEmpty &&
localProduceResults.values.count(_.exception.isDefined) < entriesPerPartition.size
```

Kafka 协议中，`acks=all` 对应 `requiredAcks == -1`。当满足该条件并且有需要处理的本地追加结果时，Broker 会建立一个 `DelayedProduce` 等待对象，随后调用：

```scala
delayedProducePurgatory.tryCompleteElseWatch(delayedProduce, producerRequestKeys)
```

它的意思不是“让处理 Produce 的线程原地睡眠”，而是：**先检查是否已经满足条件；如果还不能完成，就登记待检查的请求。**

这些等待对象会在复制进度变化等事件发生时重新检查，也会受到超时限制。

这就是从刚才 Python 模型中的 `PENDING`，长出来的第一层真实运行时机制。

## 六、到底需要等待到哪个 Offset？

如果一批记录占用了 offset `10`、`11`、`12`，我们不能只检查副本是否处理到 `10`。

我们需要等待的是**本批写入的最后一条记录也已经越过复制完成边界**。

在 `ReplicaManager.buildProducePartitionStatus()` 中，能找到：

**真实源码节选（Kafka 3.9.0）：**

```scala
ProducePartitionStatus(
  result.info.lastOffset + 1, // required offset
```

这里的 `lastOffset + 1` 正是我们前面反复强调的 LEO / HW 边界表示方式：最后一条记录的 offset 是 `12`，要覆盖它，HW 必须至少达到 `13`。

这个 `requiredOffset` 被放进 `ProducePartitionStatus`，然后由 `DelayedProduce` 持有。

接下来，等待对象就可以在任何一次进度变化后问同一个问题：**当前 Partition 是否已经满足 `requiredOffset`？**

## 七、真实源码：谁决定可以向 Producer 回 `OK`？

打开 [Kafka 3.9.0 — `DelayedProduce.scala`](https://github.com/apache/kafka/blob/3.9.0/core/src/main/scala/kafka/server/DelayedProduce.scala)，找到 `tryComplete()`。

**真实源码节选（保留核心判断）：**

```scala
case Right(partition) =>
  partition.checkEnoughReplicasReachOffset(status.requiredOffset)
```

这里的调用者是等待中的 Produce 操作；输入是上一节保存的目标位置；返回值告诉等待对象：复制条件是否达到，以及有没有错误。

再看 [Kafka 3.9.0 — `Partition.scala`](https://github.com/apache/kafka/blob/3.9.0/core/src/main/scala/kafka/cluster/Partition.scala) 的 `checkEnoughReplicasReachOffset()`：

**真实源码节选：**

```scala
val minIsr = effectiveMinIsr(leaderLog)
if (leaderLog.highWatermark >= requiredOffset) {
  if (minIsr <= curMaximalIsr.size)
    (true, Errors.NONE)
  else
    (true, Errors.NOT_ENOUGH_REPLICAS_AFTER_APPEND)
} else
  (false, Errors.NONE)
```

现在每一行都有了来由。

`leaderLog.highWatermark >= requiredOffset`：本批消息是否已经越过复制完成边界？

`minIsr <= curMaximalIsr.size`：当前同步集合的规模是否仍满足安全写入的最低要求？

如果两者都成立，返回 `(true, Errors.NONE)`，等待对象就能通过响应回调成功结束请求。

这里的 `curMaximalIsr` 是 Kafka 3.9.0 为 ISR 变更中途的保守安全判断使用的集合。正常理解时可以把它视作当前需要考虑的同步副本集合；暂时无需进一步阅读 `AlterPartition` 协调协议。

还要注意：**真实代码并没有写成“有任意两个副本复制完成就返回成功”。** 它关注的是 HW 和同步集合状态。我们稍后会用一个反例验证这点。

### 7.1 谁推动 HW 前进？

既然等待检查依赖 HW，那么真正推动 HW 的又是谁？

Kafka 的 Follower 会持续发送 Fetch 请求从 Leader 拉取数据。Leader 在处理 Follower 的 Fetch 时可以更新自己观察到的 Follower 复制进度：

在 `Partition.fetchRecords()` 的 Follower 路径中有：

**真实源码节选：**

```scala
updateFollowerFetchState(
  replica,
  followerFetchOffsetMetadata = logReadInfo.fetchedData.fetchOffsetMetadata,
```

然后，`updateFollowerFetchState()` 根据进度变化调用 `maybeIncrementLeaderHW()`。在这个方法中，可以看到：

**真实源码节选：**

```scala
val leaderLogEndOffset = leaderLog.logEndOffsetMetadata
var newHighWatermark = leaderLogEndOffset
```

它从 Leader 的尾部位置出发，再结合当前同步副本的进度，将候选 HW 限制在安全的位置，最后由 `leaderLog.maybeIncrementHighWatermark(newHighWatermark)` 尝试推进。

当 HW 前进，Kafka 会重新检查相关的延迟 Produce：

```scala
delayedProducePurgatory.checkAndComplete(requestKey)
```

到这里，一次 `acks=all` 写入的关键路径才真正连起来：

```text
Producer 发送 Produce(acks=all)
              |
              v
ReplicaManager.appendRecords()
              |
              v
本地 Leader 追加日志
              |
              v
记录 requiredOffset = lastOffset + 1
              |
              v
DelayedProduce 登记待完成请求
              |
      Follower 持续 Fetch、追赶
              |
              v
Leader 更新 Follower 进度
              |
              v
High Watermark 前进
              |
              v
checkEnoughReplicasReachOffset()
              |
      +-------+--------+
      |                |
 条件满足         未满足/超时
      |                |
 返回成功        继续等待/返回错误
```

这张图只描述普通单分区确认路径；省略了网络线程、批请求中的多个 Partition、Leader 变更与异常重试。

## 八、为什么 `acks=all` 还需要 `min.insync.replicas`？

现在回到第五章已经提示过的反例。

假设一个 Partition 配置三个副本，但连续故障后 ISR 只剩 Leader：

```text
配置副本：A、B、C
当前 ISR：{A}
```

如果成功规则仅仅是“当前 ISR 都收到”，那 A 自己一追加完成，理论上就可以对 `acks=all` 回复成功。

这与我们想通过多副本确认降低丢失风险的初衷冲突。

因此还需要一条**最低人数限制**：`min.insync.replicas`。

例如：

```properties
# Topic 级别配置示意
min.insync.replicas=2
```

生产者则设置：

```properties
acks=all
```

如果 ISR 只剩 A，一个新的 `acks=all` 写入会因同步副本数量不足而被拒绝。

这不是因为服务器完全不能写入本地日志，而是因为**它无法按约定提供足够的复制确认保障**。

真实源码在 `Partition.appendRecordsToLeader()` 中给出了非常直接的证据：

**真实源码节选（Kafka 3.9.0）：**

```scala
val minIsr = effectiveMinIsr(leaderLog)
val inSyncSize = partitionState.isr.size
if (inSyncSize < minIsr && requiredAcks == -1) {
  throw new NotEnoughReplicasException(s"The size of the current ISR ${partitionState.isr} " +
    s"is insufficient to satisfy the min.isr requirement of $minIsr for partition $topicPartition")
}
```

这段代码出现在 `leaderLog.appendAsLeader(...)` **之前**。因此它描述的是一种“已知副本数量不足，拒绝这次追加”的情况。

但还有另一种情况：开始追加时 ISR 数量足够，消息已经在 Leader 上追加，后来等待过程中 ISR 规模下降，系统仍不能满足安全条件。前面 `checkEnoughReplicasReachOffset()` 中的 `NOT_ENOUGH_REPLICAS_AFTER_APPEND`，就是为了区分这种阶段。

这也让我们第一次看清 `acks=all` 与 `min.insync.replicas` 的不同职责：

- `acks=all`：要求当前 ISR 的复制进度达到本批消息的确认边界。
- `min.insync.replicas`：指定可接受写入的同步副本数量下限。

**千万不要把 `acks=all` 理解成“只要有 `min.insync.replicas` 台机器写完就立刻回复”。**

一个具体反例：复制因子是 3，`min.insync.replicas=2`，但 ISR 目前仍是 `{A, B, C}`。如果 A 和 B 追上新消息，C 还没追上，HW 通常仍受 C 限制，`acks=all` 还不能成功。只有 C 追上，或者 C 被合法移出 ISR 且其余条件满足，才可能完成。

这与“任意两票构成多数派就提交”并非同一种协议。本章的 Produce ACK 不应与 KRaft 元数据日志所用的 Raft 共识混为一谈。

## 九、超时之后，Producer 究竟应该相信什么？

让实验中的第六种情况发生：

```text
A（Leader）已经追加 offset 1
B、C 仍没有追到 offset 1
ISR = {A, B, C}
HW = 1
```

`DelayedProduce` 没有等到 HW 前进，达到请求超时时间后，Produce 可能返回 `REQUEST_TIMED_OUT`。

但是，这时 offset 1 的消息已经在 A 的本地日志里。

所以**超时不能解释成“这条消息一定没有写入 Kafka”**。它真正表达的是：这次请求在规定时间内没有得到所需的成功确认。

之后可能发生不同结果：

1. Follower 继续追赶，消息后来越过 HW；
2. Leader 在消息被充分复制前故障，该消息没有被保留；
3. Producer 重试，但第一次请求后来也完成，导致业务需要面对重复处理或结果不确定性。

Kafka Producer 有幂等生产机制可以处理相应范围内的重试重复问题，但它并不自动为外部业务数据库副作用提供精确一次保证。本章先不展开 PID、Sequence Number 或事务型 Producer。

另一个边界也必须说明：**副本复制确认不等于每个副本已经对每条消息执行了磁盘 `fsync`。** Kafka 的耐久性建立在复制、存储和故障恢复条件的组合上；不能将 `acks=all` 直接翻译成“所有机器都同步落盘”。

即便满足 `acks=all`，如果允许落后副本通过不干净选举（unclean leader election）接管，仍可能面临数据丢失。Kafka 3.9 文档中 `unclean.leader.election.enable` 的默认值是 `false`，它代表系统在部分严重故障情况下更愿意保持不可用，而不是让明显落后的副本继续提供服务。

## 十、和 Redis WAIT 放在一起，差别就清楚了

我们在第二篇里问：“Redis 普通 `SET` 已返回成功，副本到底有没有跟上？”于是研究了额外的 `WAIT` 命令。

现在问：“Kafka Producer 发送这批消息时，成功回复应该具备什么复制条件？”于是我们研究了 Produce 请求携带的 `acks`，以及 Broker 的 ISR、HW、`DelayedProduce`。

两者当然都关心复制进度，但设计上的落点不同：

| 问题 | Redis（第二篇） | Kafka（本篇） |
| --- | --- | --- |
| 正常写入后何时有成功回复？ | 普通 `SET` 可在本地写入后准备 `OK` | 由 Producer 的 `acks` 决定 |
| 谁要求更强的复制确认？ | 客户端在同连接中额外执行 `WAIT` | Producer 在 Produce 请求中指定 `acks=all` |
| 等待什么？ | 指定数量副本达到客户端写入复制位置 | 通过 HW 检查当前 ISR 的复制完成边界 |
| 等待不足的结果 | `WAIT` 返回实际确认的副本数量 | Produce 返回错误/超时等结果 |
| 是否自动保证永不丢失？ | 不保证 | 也依赖 ISR、选举、故障类型与存储条件 |

它们没有简单的“哪个更高级”的关系。Redis 优先服务低延迟的键值操作；Kafka 要在一个追加日志里确定哪些消息完成了分区复制、可被安全地提供给正常消费者。

**共同的是问题，具体的确认合同却不同。**

## 十一、实验的验证结果与诚实边界

对于第四节的完整 Python 脚本，执行：

```bash
python3 kafka_isr_lab.py
```

我已执行这个 Python 教学模型，实际输出如下（不代表 Kafka Broker 实测）：

```text
① Leader 已追加：acks=1 OK LEO 2 HW 1
② 只有 B 跟上：ISR ['A', 'B', 'C'] HW 1 acks=all PENDING
③ B、C 都跟上：HW 2 acks=all OK
④ C 不在 ISR：ISR ['A', 'B'] HW 2 acks=all OK
⑤ ISR 只剩 A： NOT_ENOUGH_REPLICAS LEO 1
⑥ 模拟等待超时：REQUEST_TIMED_OUT；本地 LEO 2
```

它验证了当前简化模型中的四件事：Leader LEO 与 HW 可以不同；当前 ISR 规模和成员影响写入完成条件；`min.insync.replicas` 能阻止单副本伪装成安全成功；超时与本地是否已经追加不能画等号。

不能由此声称已经验证了 Kafka 3.9.0 的真实 Broker、Follower Fetch 网络时序、ISR 元数据变更、日志恢复或故障选主。源码部分是按 `3.9.0` 固定版本核对的实现分析；实验没有直接运行 Scala/Kafka。

## 十二、下一章：同一个问题，为什么出现三种答案？

现在我们已经从单机键值存储一路走到了分区日志。

从 Redis 的复制缓冲，到 MySQL 的事务日志，再到 Kafka 的 High Watermark，三种系统都要回答一个相似的问题：

**当一台机器告诉客户端“成功”，它究竟承诺了什么？**

它们还共同碰到另外几个问题：数据怎样分到多台机器；多份数据怎样跟上；原来的主节点消失时谁接管；出现故障时应该牺牲一部分吞吐、可用性还是数据保证。

下一篇我们不继续引入新术语，而是把这 11 篇已经逐步推导出来的机制重新放在同一张设计桌上，回答：**为什么 Redis、MySQL、Kafka 会对相似问题给出不同答案？**

**下一篇：《同一套分布式难题，为什么 Redis、MySQL、Kafka 给出了三种答案？》**

---

## 源码索引与核对记录

固定标签：[`apache/kafka` `3.9.0`](https://github.com/apache/kafka/tree/3.9.0)。下列路径均为实际存在的源码位置：

1. [`core/src/main/scala/kafka/server/ReplicaManager.scala`](https://github.com/apache/kafka/blob/3.9.0/core/src/main/scala/kafka/server/ReplicaManager.scala)：`appendRecords()`、`buildProducePartitionStatus()`、`maybeAddDelayedProduce()`、`delayedProduceRequestRequired()`。谁调用：Broker Produce 请求处理路径；输入：分区批数据、`requiredAcks`、超时；输出：本地追加结果与延迟响应。
2. [`core/src/main/scala/kafka/server/DelayedProduce.scala`](https://github.com/apache/kafka/blob/3.9.0/core/src/main/scala/kafka/server/DelayedProduce.scala)：`tryComplete()`、`onComplete()`。谁调用：延迟操作检查/完成流程；输入：目标 Offset 和分区状态；输出：将错误或成功状态交给响应回调。
3. [`core/src/main/scala/kafka/cluster/Partition.scala`](https://github.com/apache/kafka/blob/3.9.0/core/src/main/scala/kafka/cluster/Partition.scala)：`appendRecordsToLeader()`、`checkEnoughReplicasReachOffset()`、`fetchRecords()`、`updateFollowerFetchState()`、`maybeIncrementLeaderHW()`。输入：本地日志与 Follower 进度；输出：追加结果、HW 更新与写入确认判断。
4. [Kafka 3.9 Producer 配置：`acks`](https://kafka.apache.org/39/configuration/producer-configs/#acks)：区分 `0`、`1`、`all`，以及幂等生产的配置前提。
5. [Kafka 3.9 Topic 配置：`min.insync.replicas`、`unclean.leader.election.enable`](https://kafka.apache.org/39/configuration/topic-level-configs/)：最小 ISR 要求与不干净选举的数据风险。

**代码标注说明**：标明“真实源码节选”的代码只摘录固定标签中的关键行，省略上下文，不改变相关判断语义；Python 代码明确属于教学模型；上面的系统运行追踪是由源码和教学实验推导的解释，不冒充真实集群执行日志。
