---
title: "分布式系统 12：同一套难题，为什么 Redis、MySQL、Kafka 给出了三种答案？"
sidebar_label: "十二、三种系统，三种取舍"
sidebar_position: 13
description: "以一次文章访问为贯穿案例，回到 Redis 7.2.4、MySQL 8.4.0 和 Kafka 3.9.0 的源码，比较写入成功、复制、故障接管、分片和跨节点操作的不同承诺。"
slug: /distributed-systems/12-three-systems-three-tradeoffs
tags: [distributed-systems, redis, mysql, kafka, source-code, system-design]
---

# 同一套难题，为什么 Redis、MySQL、Kafka 给出了三种答案？

上一篇，我们终于看到 Kafka 的一次 `acks=all` 写入为何不能只在 Leader 追加完成时就回复 Producer：还有一条从 Follower Fetch 到 High Watermark 前进、再到等待请求被唤醒的路径。

这让我们想到第二篇读过的 Redis：普通 `SET` 已经回复 `OK`，副本却可能还没收到更新。第九篇又研究了 MySQL：事务本地提交时，不仅要考虑 InnoDB 自己的恢复，还要协调 Binlog，但这并不代表异步副本已经应用这笔事务。

乍看是三个完全不同的实现，实际上它们都在回答同一个问题：

> **当系统向客户端说“成功”，它究竟已经完成了哪些工作？**

这一篇不急着提出新机制，也不把前三套源码按功能逐一复述。我们继续沿用同一个案例，让它分别经过三个系统，再把此前逐步产生的问题放到一起：成功确认、故障恢复、数据分布，以及一次操作能覆盖多大的范围。

这里最重要的前提是：**相同的业务事件，不代表三个系统存储的是相同的数据。** 不先区分这一点，后面的比较就会失真。

## 一、先固定同一次访问，而不是固定同一种数据结构

假设用户访问文章 42，我们给这次访问一个唯一编号：

```text
visit-e101
article_id = 42
```

为了完成网站的不同任务，三个组件可能分别接收到：

```redis
# Redis：更新当前聚合状态
INCR post:42:views
```

```sql
-- MySQL：记录一次具体访问（假设 event_id 上有 UNIQUE 约束）
INSERT INTO visit_events(event_id, article_id)
VALUES ('visit-e101', 42);
COMMIT;
```

```text
# Kafka：追加一条访问事件
Topic: article-visits
Key:   article:42
Value: {event_id: "visit-e101", article_id: 42}
```

这是**三种不同的语义**：

- Redis 计数回答“目前累计是多少”，未必知道是哪些访问贡献了这个数字。
- MySQL 事务行回答“这条访问记录是否已经作为数据库状态提交”，可以用唯一键拒绝同一个事件被重复插入。
- Kafka 消息回答“这个访问事件是否已经写进某个 Partition 的日志”，后续消费者可以按分区顺序处理或重新读取它。

如果 Redis 计数是 `101`，MySQL 里有记录 `visit-e101`，Kafka 某个分区的 Offset 为 `17`，**不能说这三个数字在分布式意义上相等**。

它们只是同一业务动作在不同存储模型中的三个结果。

现在做一个看起来最平常的事情：每个组件完成自己的写入后，都返回成功。

这时我们已经无法把三个“成功”当作同一个布尔值来理解。

## 二、第一次比较：三种成功，究竟在等什么？

我们再让上一篇的故障发生一次：主节点处理新数据，但副本可能没跟上；客户端随后收到一个响应，接着主节点不可用。

### 2.1 Redis：先完成本地更新，副本随后追赶

回到第一篇的 `SET post:42:views 101`。虽然业务实际可能使用 `INCR`，这里继续选 `SET`，因为它能直接对照我们已经读过的源码入口。

**真实源码节选：Redis 7.2.4，`src/t_string.c`，`setGenericCommand()`**：

```c
setKey(c,c->db,key,val,setkey_flags);
server.dirty++;
```

同一函数对普通成功 `SET` 准备回复：

```c
addReply(c, ok_reply ? ok_reply : shared.ok);
```

谁在调用它？客户端执行 `SET`，Redis 的命令执行框架经 `call()` 调用 `setCommand()`，再进入 `setGenericCommand()`。

这些参数来自哪里？`key` 和 `val` 是客户端本次命令的参数，`c->db` 是当前实例要修改的数据库。

这段代码做了什么？它修改**当前节点**的数据，并准备客户端回复。`server.dirty++` 使后续命令传播能够识别本次修改。普通 `SET` 没有在这里等待某个副本的应用确认。

修改还怎样到达副本？上一篇已沿 `src/server.c` 中的执行与传播路径追到 `src/replication.c` 的 `replicationFeedSlaves()`。在其中可找到：

```c
feedReplicationBufferWithObject(argv[j]);
```

这将命令参数编码进复制数据流，供副本接收。**进入复制缓冲 ≠ 网络已传输 ≠ 副本已执行。**

于是下面这个时间表并不矛盾：

```text
T0  A 和 B 的计数都是 100
T1  A 执行 SET 101
T2  A 准备回复 OK
T3  B 还在 100，A 不可用
T4  如果切换到 B，可能再次观察到 100
```

第二篇的 `WAIT` 允许同一个客户端连接额外询问已有多少副本跟上此前写入的位置，但它**不自动把 Redis 变成不会丢失任何已确认写入的强一致数据库**。`WAIT` 计的是复制进度确认，并不自动指定接下来一定由哪个副本接管，也不等价于所有副本已完成磁盘 `fsync`。

所以，**Redis 默认写入成功的承诺，首先落在当前主节点完成这次操作上**。

### 2.2 MySQL：本地事务能恢复，不代表副本已经应用

现在换成 `visit-e101` 的数据库记录。

为了避免另一种常见误解，我们要同时观察两个不同的失败：

1. MySQL **源库进程重启**，事务还能不能恢复？
2. 源库完全不可用、改由一个**尚未追上的异步副本**接管，这笔事务还在不在？

这两件事不能因为都叫“故障”而合在一起。

第九篇已经看到 MySQL 8.4.0 的事务协调入口：`sql/handler.cc` 的 `ha_commit_trans()`。在满足内部两阶段提交条件的正常路径中：

**真实源码节选：**

```cpp
if (!trn_ctx->no_2pc(trx_scope) && (trn_ctx->rw_ha_count(trx_scope) > 1))
  error = tc_log->prepare(thd, all);
```

随后进入协调器提交：

```cpp
if (error || (error = tc_log->commit(thd, all))) {
    ha_rollback_trans(thd, all);
    error = 1;
    goto end;
}
```

谁调用这条路径？SQL 层要提交当前事务时，进入 `ha_commit_trans()`，传入与本次会话相关的 `THD` 上下文。`tc_log` 在启用 Binlog 的相应路径中负责协调 Binlog 与存储引擎的提交。

第九篇继续追踪到 `sql/binlog.cc` 的 `MYSQL_BIN_LOG::ordered_commit()`：它协调 Binlog 的 Flush、按配置执行 Sync，以及存储引擎最终提交阶段。其目的之一是让 Binlog 与 InnoDB 的事务结果在崩溃恢复后能够对齐。

因此，在**存储、日志同步配置和硬件可靠性条件满足**时，源库已经确认的事务可以依赖 InnoDB Redo、Binlog 与恢复协议在本机崩溃后恢复。这是 MySQL 本地事务耐久性的核心设计之一。

但是，普通 MySQL Source → Replica 复制**默认是异步的**。源库的 `COMMIT` 成功，并不要求每个副本都已经接收、应用并提交该事务。

```text
T0  Source 和 Replica 都没有 visit-e101
T1  Source COMMIT 成功，本地可恢复
T2  Replica 尚未应用 visit-e101
T3  Source 故障

路径 A：原 Source 从持久化日志恢复 → 可以恢复这笔已提交事务
路径 B：立刻切换到落后的 Replica → 它暂时没有这笔事务
```

这并不否定 MySQL 的事务保证，只是说明**“源库本地事务原子与持久”不等于“所有复制节点同步提交”**。

MySQL 还有半同步复制、Group Replication 等不同方案；它们各有具体的确认和故障恢复合同。不能把这些方案全部归纳为“普通 MySQL 主从会自动保证零丢失”。

### 2.3 Kafka：Producer 的成功需要满足配置的复制完成条件

最后，让 `visit-e101` 成为 `article-visits` 某个 Partition 中的一条消息。

上一篇固定 Kafka 3.9.0，沿 `ReplicaManager.appendRecords()` 看到 Leader 本地追加后，并不一定马上回复。对于 `acks=all`，它可能登记一个 `DelayedProduce`，等待副本复制进度满足要求。

**真实源码节选：Kafka 3.9.0，`core/src/main/scala/kafka/cluster/Partition.scala`，`checkEnoughReplicasReachOffset()`**：

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

谁调用它？`DelayedProduce.tryComplete()` 在检查等待中的 Produce 请求时调用这段逻辑。

输入从哪里来？`requiredOffset` 对应本次批写入必须覆盖的日志位置；`leaderLog.highWatermark` 反映分区复制已完成的边界。`minIsr` 是当前配置要求的同步副本数下限。

输出是什么？返回是否完成以及错误状态，由延迟写入流程进一步组织 Producer 响应。**只有 Leader 本地追加，并不自动让 `acks=all` 成功。**

这里必须记住上一章反复验证的区别：

- `acks=all` 等待当前 ISR 所要求的复制完成条件，而不是只凑够任意 `min.insync.replicas` 个节点。
- `min.insync.replicas` 是写入成功需要满足的**同步副本数量下限**，不能直接当成“实际需要等待几个副本”的替代定义。
- 如果设置 `acks=1`，确认合同就变了；如果有不干净选举、严重相关故障或持久化问题，成功确认的实际安全性还受这些条件影响。

假设这里的 ISR 是 `{A, B}`，`min.insync.replicas=2`，并且不允许落后副本通过不干净选举接管：

```text
T0  A 作为 Leader 收到 visit-e101
T1  A 已追加，B 尚未追上
    acks=all 不能据此返回成功
T2  B 追上，High Watermark 进到目标位置
    acks=all 可以成功
T3  A 故障，B 作为合格的同步副本接管
    B 保有该日志记录
```

注意：最后的故障结果以 B 实际存活、同步集合及选举规则满足前提为条件。它不等于 Kafka 对任意硬件故障、任意配置都保证绝不丢消息。

### 到这里，第一道题已经有三个答案

| 问题：什么时候回复“成功”？ | 典型设置下的完成条件 | 不应误认为 |
| --- | --- | --- |
| Redis 7.2 普通 `SET` | 当前主节点完成命令执行并准备回复 | 所有副本已执行；故障切换绝不丢 |
| MySQL 8.4 普通 InnoDB `COMMIT`，异步复制 | 本地事务按其提交路径完成；耐久性受日志配置与硬件条件约束 | 异步副本已经应用；切到落后副本仍有全部事务 |
| Kafka 3.9 Producer `acks=all` | 本地追加后，等待符合 ISR、HW 与最小同步副本约束的确认 | 每台机器都 `fsync`；跨 Partition 全局顺序；外部业务副作用精确一次 |

这张表不是“谁最可靠”的排行榜。我们刻意固定了不同的**具体配置与写入语义**；如果调整配置，结论也要相应调整。

## 三、亲手执行：让三个“成功”经过同一类故障

表格可以帮助我们比较，但还不能让人真正看到时间差。下面我们把刚才的三条正常路径缩成一个标准库 Python 实验，不需要启动 Redis、MySQL 或 Kafka。

为了避免模拟器看起来过于逼真，我们只保留这章必须观察的状态：

- Redis：主节点计数、副本计数。
- MySQL：源库已经完成且假定可本地恢复的事件、异步副本已应用的事件。
- Kafka：Leader 日志、Follower 日志、是否越过本次写入的复制边界。

**教学化简：以下代码不是三个开源项目的源码，也没有实现真实网络、持久化、自动选主或协议。**

```python
EVENT_ID = "visit-e101"
KEY = "post:42:views"


def redis_default_set():
    leader, replica = {KEY: 100}, {KEY: 100}
    leader[KEY] = 101
    ack = "OK"
    # 故意跳过复制，人工把 replica 当作接管节点
    observed = replica[KEY]
    assert (ack, observed) == ("OK", 100)
    return f"Redis SET: ack={ack}, failover_view={observed}"


def mysql_async_commit():
    source_durable = set()   # 假设本地提交已持久化
    replica_applied = set()
    source_durable.add(EVENT_ID)
    ack = "COMMIT OK"
    # 故意跳过复制应用
    recovered = EVENT_ID in source_durable
    promoted = EVENT_ID in replica_applied
    assert recovered and not promoted
    return (f"MySQL COMMIT: ack={ack}, source_restart={recovered}, "
            f"replica_failover={promoted}")


def kafka_acks_all():
    leader_log, follower_log = [], []
    isr = {"leader", "follower"}
    min_isr = 2
    leader_log.append(EVENT_ID)
    required_offset = len(leader_log)

    def high_watermark():
        return min(len(leader_log), len(follower_log))

    def acked():
        return len(isr) >= min_isr and high_watermark() >= required_offset

    before = acked()            # 仅 Leader 追加
    follower_log.extend(leader_log)
    after = acked()             # Follower 已追上
    preserved = EVENT_ID in follower_log
    assert not before and after and preserved
    return (f"Kafka acks=all: before_fetch={before}, "
            f"after_fetch={after}, failover_preserves={preserved}")


if __name__ == "__main__":
    for scenario in (redis_default_set, mysql_async_commit, kafka_acks_all):
        print(scenario())
    print("模型断言：全部通过")
```

保存为 `three_ack_contracts_lab.py`，运行：

```bash
python3 three_ack_contracts_lab.py
```

**已实际运行的输出：**

```text
Redis SET: ack=OK, failover_view=100
MySQL COMMIT: ack=COMMIT OK, source_restart=True, replica_failover=False
Kafka acks=all: before_fetch=False, after_fetch=True, failover_preserves=True
模型断言：全部通过
```

我们没有把三种系统写成“各自都有一个复制队列，然后谁等得更久谁就更好”的统一实现，因为它们的“成功”根本不是同一个事件。

这个实验真正验证的是**我们为每个教学模型明确写下的确认合同会产生怎样的可见后果**。它没有执行真实 Redis、MySQL 或 Kafka 服务器，不证明任何集群的性能与故障概率。

现在，从这三个结果中抽象出的问题是：

> **发生故障时，是否保留已经确认的状态，取决于确认条件、复制状态、恢复途径和接管规则的组合。**

绝不能只数有几台机器。

## 四、第二次比较：为什么增加机器，扩容方式却不一样？

前面讨论的是“同一份逻辑状态怎样保存得更安全”。可是第六篇我们已经遇到另一种失败：数据实在太多，一台机器装不下。

如果 Redis 的每个副本都存放完整键集合，那么加一个副本主要增加冗余和一定的读能力，并不会提高**一份完整数据集**能占用的总内存上限。

于是第六篇引入 Redis Cluster 的 Hash Slot：先把 Key 映射到固定数量的 Slot，再将 Slot 分配到不同 Master。

一部分关键路径来自 Redis 7.2.4 的 `src/cluster.c`：

```text
Key → keyHashSlot() → Slot → 负责这个 Slot 的节点
```

这与 Kafka 的 Partition 看起来很像：Producer 根据消息的 Key/分区器选择 Partition，Broker 将记录追加到对应 Partition 的日志。

但它们解决的问题并不完全一样。

### 4.1 Redis 的分片，是让 Key 的归属可以分散

对于我们的键：

```text
post:42:views
post:43:views
post:44:views
```

Redis Cluster 可以把这些 Key 所对应的 Slot 分别交给不同 Master。一次针对某个 Key 的命令应当由负责其 Slot 的节点处理。

这让独立 Key 的存储和处理负载能够横向分摊。

不过，第八篇已经看到：如果一条多 Key 命令涉及不同 Slot，Redis Cluster 通常会拒绝这种跨 Slot 操作。Hash Tag 能让相关 Key 强制落入同一个 Slot，但这等于主动把相关操作聚集到相同分片，其容量和热点代价仍要考虑。

**所以 Redis 的分片边界，往往也是直接执行多 Key 原子操作的边界。**

### 4.2 MySQL 的复制，不会自动变成分库分表

普通 MySQL Source-Replica 复制的主要对象是数据库变更。再增加 Replica，并不自动把一张表的不同数据行切给不同主库。

如果我们希望把 `visit_events` 按 `article_id` 分成不同数据库，需要另外设计路由和分片策略（或使用具备相关能力的数据库方案）。

如果随后发生“同时修改两个分片上的数据库记录”的业务要求，原本单实例的事务边界不再自动覆盖两个独立数据库。第八篇讨论过外部 XA 协调及其失败窗口：**多台数据库上的一组动作，不会因为每台数据库都支持 `COMMIT`，就自动变成一个全局事务。**

这是为什么我们不能把“数据库复制”与“数据库分片”当成同一个功能开关。

### 4.3 Kafka 的 Partition，还承担日志顺序边界

第十篇中，我们不是单纯为了节省一台机器的磁盘空间才引入 Partition。

最初只有一条访问事件日志，所有事件都存在一个序列中；但单一序列难以无限扩展吞吐。拆成多个 Partition 后，不同 Partition 可以并行追加和消费。

代价是：**Kafka 不承诺所有 Partition 之间存在一个统一的全局消费顺序。**

于是我们让同一篇文章的事件使用相同 Key，让它们在通常分区配置不变的情况下进入同一个 Partition，进而利用分区内顺序。

也要注意：如果未来改变 Partition 数量、默认分区算法或自定义分区器，同一 Key 的映射就可能变化；不能仅凭 Key 相同就推断跨配置变更的永久严格顺序。

### 再次比较，不要只背“都支持分片”

| 新问题 | Redis Cluster | MySQL 常规复制 / 另建分片方案 | Kafka |
| --- | --- | --- | --- |
| 怎样分布数据？ | Key → Hash Slot → Master | 复制本身不做分片；按业务路由规则或分片中间层拆分 | Record → Topic Partition → Partition Leader |
| 增加节点后要搬什么？ | Slot 所属 Key，以及 Slot 归属信息 | 依分片方案迁移表/行、修改路由 | 调整 Partition 副本分配或增加 Partition（两者不是同一操作） |
| 哪些操作天然局部化？ | 单 Slot 内的操作 | 单数据库/事务管理范围内的操作 | 单 Partition 的日志顺序与 Offset |
| 主要新代价 | Key 路由、Slot 迁移、跨 Slot 操作限制 | 分片路由、跨库事务、查询聚合 | 跨 Partition 无全局顺序、分区级负载不均衡 |

当你再看到一个系统宣称“支持横向扩展”，应该继续问：**扩的是数据容量、读吞吐、写吞吐，还是并行处理能力？它以什么为最小切分单位？**

这个问题比问“有没有 Cluster”更有用。

## 五、第三次比较：主节点坏了，为何不是都采用同一种选主方式？

现在我们假设一次写入已经完成，负责提供服务的节点突然失去联系。

第三至第五篇，我们为 Redis 的一组主从节点逐渐增加了 Sentinel：先从人工故障切换开始，再让多个 Sentinel 判断主节点是否不可用，最后研究授权切换需要的多数票以及 Epoch 的作用。

它解决的是一个具体问题：**谁有权决定现在要把哪个 Replica 提升为新的 Master？**

但是，投票授权本身不是数据复制。Sentinel 合法完成切换，也不能凭空把旧主节点上尚未复制的数据补进新主节点。

把这个问题带到其他系统，答案也不能直接照搬：

**普通 MySQL 异步复制**：副本有自己的复制应用进度，但“自动选择哪台副本、如何阻断旧源库写入、客户端如何改连接”通常还需要额外的高可用协调方案。InnoDB Cluster / Group Replication 具备另一套组成员及主节点管理能力，不能和普通主从架构混称为一个“自动选主机制”。

**Kafka 3.9**：每个 Partition 可以有自己的 Leader；集群的控制平面维护分区领导者、ISR 等元数据。在 KRaft 模式下，控制平面使用元数据仲裁机制。**控制平面决定谁是 Leader**，与**分区数据怎样复制到 Follower**是两层不同的工作。

再次看见同一个原则：

```text
“选出了新的负责人”
      不等于
“它拥有每一条此前被确认的数据”
```

为什么我们在前五篇反复区分多数派授权、旧主节点写入保护、复制落后？因为这些因素决定的是不同的安全条件。

如果没有这种区分，读者可能会以为“有 Raft / 有 Sentinel / 有 Controller”就等于“数据绝不会丢失”。那实际上把**控制权正确性**误当成了**数据本身的持久与可恢复性**。

## 六、现在可以把 11 篇文章的架构拼起来了

如果在第一篇开头，我们直接画出下面这幅图，里面大部分术语都只是陌生标签：

```text
业务请求：记录一次文章访问
        |
        +--> 当前状态：Redis Key
        |      |
        |      +-- 复制到 Replica
        |      +-- 故障时确定新的 Master
        |      +-- Key 映射 Slot，必要时迁移
        |
        +--> 持久事务记录：MySQL Row
        |      |
        |      +-- InnoDB Redo 支持本地恢复
        |      +-- Binlog 与引擎提交需要协调
        |      +-- Replica 异步应用 / 可选其他复制模式
        |      +-- 需要分片时另设数据路由与事务边界
        |
        +--> 可重读访问事件：Kafka Record
               |
               +-- Key 决定 Partition
               +-- Partition 内追加顺序与 Offset
               +-- Leader / ISR / High Watermark
               +-- acks 决定 Producer 的确认合同
```

但到了现在，这些模块已经不再需要机械记忆：

- **为什么有 Replica？** 因为单节点失去服务能力后，我们需要另一份数据。
- **为什么要关心 ACK？** 因为主节点回复成功和副本追上不是同一个时刻。
- **为什么需要协调切换？** 因为多个观察者可能对故障作出不一致判断。
- **为什么有 Slot / Partition？** 因为完整复制不能无限扩展单份数据的容量与吞吐。
- **为什么 MySQL 要协调两份日志？** 因为本地引擎事务结果和供复制使用的 Binlog 不能在崩溃后相互矛盾。
- **为什么 Kafka 要等 HW？** 因为追加到 Leader 和达到分区复制完成边界是两件事。

这就是整套系列想建立的理解：**架构不是名词拼图，而是一组对失败场景作出的可检查承诺。**

## 七、如果真的要设计这个文章访问系统，应该怎么选？

现在有了一个更实用的问题。

如果我们的系统同时使用 Redis、MySQL 和 Kafka，是不是就能同时获得缓存低延迟、数据库事务和消息高可靠，然后自动得到“精确一次、零丢失、永不故障”的系统？

当然不能。

想象这段应用伪代码：

```python
# 概念伪代码，不是推荐的生产级实现
mysql.insert_visit("visit-e101", article_id=42)
kafka.publish("visit-e101", article_id=42)
redis.incr("post:42:views")
return "OK"
```

这时即使每个组件在**自己的范围内**都正确完成了操作，我们仍可能在第二步之前崩溃：MySQL 已写入，Kafka 没有发布；或者 Kafka 已发布，Redis 计数尚未更新。

因此，业务不能只问“这些中间件分别可靠吗”。还需要先决定：

**哪一份结果是系统的事实来源？哪些只是可以延迟更新或重建的派生结果？**

如果业务要求每次访问都能审计，那么可以把具有唯一 `event_id` 的持久记录视为事实来源，Redis 计数视为可重建的派生统计。Kafka 则可以承担异步分发和重放事件的职责。

但这只确定了职责，还没有解决“数据库事务提交与消息发布之间的原子衔接”。如果需要保证已提交事务最终可靠地转化为事件，就要进一步设计相应的交付协议；如果消息可能重复到达，消费者也要能根据 `event_id` 安全地重试。

这些属于**跨组件的端到端一致性问题**，不能从 Redis `WAIT`、MySQL `COMMIT` 或 Kafka `acks=all` 中任选一个就自动得到答案。

从这个业务案例，至少能得出一条实用准则：

> **先确定业务不能接受哪种失败，再选择能作出相应承诺的组件配置与交付流程。**

反过来说，如果只是一个允许短时间不精确的页面热度计数，我们就没有必要为了看起来“更分布式”，把每次计数都变成跨库强协调事务。

## 八、给自己做最后一次设计推演

不用记命令，只用已经理解的因果关系，试着回答三道题。

### 题目一：为什么 Redis 有三个副本，仍可能丢失最近一次写入？

因为“有三个副本”只说明存在复制目标。若某次更新尚未送达后来接管的副本，副本数量本身不能让它凭空拥有这次更新。是否使用 `WAIT`、副本实际进度、旧主节点故障与切换过程，都会影响结果。

### 题目二：为什么 MySQL 的 COMMIT 成功，不等于异步副本已提交？

因为 InnoDB 本地事务提交及崩溃恢复，与 Source → Replica 发送和应用 Binlog 是不同的处理阶段。二者有联系，但默认异步复制不会把副本应用完成纳入普通事务的成功条件。

### 题目三：为什么 Kafka `acks=all` 不是“任意两个副本收到就成功”？

因为在该版本的正常路径中，Broker 通过 High Watermark 判断当前同步副本集合所覆盖的日志进度，再检查 ISR 数量下限。`min.insync.replicas=2` 并不意味着 ISR 仍有三个节点时，只等任意两个即可成功。

如果这三题都能用一段失败时间线解释，而不是只背术语，那么这一轮学习已经完成了最重要的部分。

## 九、结语：分布式系统并不存在唯一的“正确架构”

我们从最初的一行代码开始：

```python
views["post:42:views"] += 1
```

它没有副本、选主、分片、事务日志或同步集合。在一台机器够用的时候，这不是问题。

但当我们逐步提出新的要求，它就不得不变化：

```text
单节点故障后仍想访问数据
  → 数据复制

副本没有及时追上，却已经返回成功
  → 写入确认及其边界

原 Leader 不可用，需要另一个节点接管
  → 故障检测、切换协调、任期与授权

每台机器都放完整数据，容量仍不够
  → 分片、路由、数据迁移

一笔操作落在两个分片
  → 跨分片协调，或重新调整业务边界

数据库事务需要崩溃后恢复，还要提供复制记录
  → InnoDB Redo、Binlog 与提交协调

一条可重读日志需要水平扩展，还要给写入安全定义完成条件
  → Kafka Partition、ISR、High Watermark 与 ACK
```

每一个设计都有明确收益，也有不能自动覆盖的边界。

**Redis、MySQL、Kafka 的共通之处，不是它们采用了完全相同的算法，而是它们面对相似的故障与扩展问题时，都必须把“成功”“复制”“恢复”和“归属”定义得足够精确。**

它们的不同之处，恰恰来自它们各自最重视什么：

Redis 处理当前键值状态；MySQL 维护有事务语义的数据库状态；Kafka 维护能够分区追加、复制并供消费者读取的事件日志。

读完这个系列，你不必能够从记忆中复述每一条源码调用链，但应该已经可以对陌生系统提出一组有力量的问题：

**它承诺什么？在哪个边界内承诺？谁负责判断成功？发生什么故障时承诺会减弱？源码中的哪段状态变化能够证明这一点？**

这比背下所有分布式术语更接近真实的工程理解。

本系列到这里结束。下一次面对一个新的分布式组件，可以从同样的一台机器、一次写入、一次故障开始，沿着问题继续阅读它为什么会长成今天的样子。

---

## 固定源码索引与验证边界

为避免把不同版本行为混成一套“通用实现”，本章沿用前十一篇固定的源码版本。各处节选均来自对应固定版本，省略与当前推论无关的分支；调用链和时间线是教学整理，而非完整函数体或真实集群跟踪日志。

### Redis 7.2.4

- [`src/t_string.c` — `setGenericCommand()`](https://github.com/redis/redis/blob/7.2.4/src/t_string.c)：普通 SET 的本地修改与回复准备。
- [`src/server.c` — `call()`](https://github.com/redis/redis/blob/7.2.4/src/server.c)：命令执行、是否需要传播的判定。
- [`src/replication.c` — `replicationFeedSlaves()`、`waitCommand()`](https://github.com/redis/redis/blob/7.2.4/src/replication.c)：复制数据流与客户端复制位置确认。
- [Redis `WAIT` 文档](https://redis.io/docs/latest/commands/wait/)：确认数量、超时与保证边界。
- [Redis Cluster Specification](https://redis.io/docs/latest/operate/oss_and_stack/reference/cluster-spec/)：Slot、路由、故障切换和写入安全背景。

### MySQL 8.4.0

- [`sql/handler.cc` — `ha_commit_trans()`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/handler.cc)：判断是否进入事务协调的准备与提交路径。
- [`sql/binlog.cc` — `MYSQL_BIN_LOG::prepare()`、`ordered_commit()`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/binlog.cc)：协调引擎 Prepare、Binlog Flush/Sync 与提交。
- [`sql/binlog/recovery.cc` — `Binlog_recovery::recover()`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/binlog/recovery.cc)：崩溃恢复时扫描 Binlog 并协调引擎待决事务。
- [MySQL 8.4 Replication](https://dev.mysql.com/doc/refman/8.4/en/replication.html)：普通 Source-Replica 默认异步；其他复制方案需要分别理解。
- [MySQL 8.4 Binary Logging Options](https://dev.mysql.com/doc/refman/8.4/en/replication-options-binary-log.html)：`sync_binlog` 及耐久性建议。

### Kafka 3.9.0

- [`core/src/main/scala/kafka/server/ReplicaManager.scala` — `appendRecords()`](https://github.com/apache/kafka/blob/3.9.0/core/src/main/scala/kafka/server/ReplicaManager.scala)：本地追加与延迟 Produce 的衔接。
- [`core/src/main/scala/kafka/server/DelayedProduce.scala` — `tryComplete()`](https://github.com/apache/kafka/blob/3.9.0/core/src/main/scala/kafka/server/DelayedProduce.scala)：等待副本复制完成的请求何时结束。
- [`core/src/main/scala/kafka/cluster/Partition.scala` — `checkEnoughReplicasReachOffset()`](https://github.com/apache/kafka/blob/3.9.0/core/src/main/scala/kafka/cluster/Partition.scala)：HW 与最小 ISR 约束下的确认判断。
- [Kafka 3.9 Producer 配置](https://kafka.apache.org/39/configuration/producer-configs/) 与 [Topic 配置](https://kafka.apache.org/39/configuration/topic-level-configs/)：`acks`、`min.insync.replicas` 等配置的精确定义。

**实验说明：** `three_ack_contracts_lab.py` 已在 Python 3 环境实际执行，三个断言均通过。实验只检验教学模型的确定性失败窗口；未启动真实 Redis/MySQL/Kafka 集群，未测试 Redis Sentinel 选主、MySQL 物理恢复、Kafka Broker 网络调度或性能指标。源码分析与实验结果属于不同的证据层级，不应互相替代。
