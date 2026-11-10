---
slug: /message-queue/rocketmq-consumer-offset-failover
title: Broker 已经切换，消费者为什么还会重复消费或停止消费？
sidebar_position: 18
description: 从订单消息的主从切换继续，区分消息正文与消费进度，推导 Offset 持久化、复制、路由刷新和队列重分配，精读 RocketMQ 5.5.1 经典拉取消费源码。
tags: [消息队列, RocketMQ, 源码精读, 消费进度, ConsumerOffset, Rebalance, 故障恢复]
---

# Broker 已经切换，消费者为什么还会重复消费或停止消费？

上一章，Controller 已经把 Master 从 A 切换到 B，NameServer 也登记了新地址。生产者刷新路由后，终于能够继续向 B 发送订单恢复消息。

但消费侧监控出现了一个新的现象：

```text
订单 2002
Topic: OrderRecovery，Queue: 0

offset 0  replay:msg-2002
          C1 已经处理，通知意图也写入了数据库

Master A 故障，Master B 接管

offset 0  replay:msg-2002
          C2 又收到了它
```

副本 B 明明存有相同消息，为什么还会从头读？如果新消费者连不上旧节点，为什么可能暂时没有任何消息被处理？

我们先不引入一个全新的组件。前面的章节已经知道，**消息能被保存下来**与**消息有没有被业务执行**是两回事。现在只需要继续追踪另一份信息：**消费者下次应该从哪条消息开始读**。

<!-- truncate -->

## 消息还在，不代表消费进度也还在

先看一个最简单的消费者。它有一个变量 `next_offset`，记录下次要读的位置：

```python
# 教学化简，非 RocketMQ 源码
next_offset = 0

message = read_message(queue_id=0, offset=next_offset)
handle(message)
next_offset += 1
```

对于订单 `2002`，这段逻辑的状态变化非常直观：

```text
开始：next_offset = 0
读取：replay:msg-2002
业务成功：写入一条通知意图
结束：next_offset = 1
```

一个消费者一直运行时，这样做就够了。但 `next_offset` 只保存在 C1 的内存中。假设 C1 在处理完成之后崩溃，新的消费者 C2 从哪里知道 C1 已经走到了位置 1？

不能从消息正文推断：同一条消息可能被不同消费组独立处理。也不能只看数据库里有没有通知意图：消息系统不应该为每个业务的数据库表定制进度规则。

我们缺少的是**消费组自己的进度记录**。

引入最小的新状态：

```text
(group, topic, queue_id) -> next_offset

(notification-service, OrderRecovery, 0) -> 1
(report-service,       OrderRecovery, 0) -> 0
```

这里的 `next_offset=1` 指**下次从位置 1 读取**，并不是“已经消费了 ID 为 1 的消息”。

### 真实源码：Broker 如何记录消费组位置？

固定版本：Apache RocketMQ **5.5.1**，Git Commit [`efa1be59d6fc747ad220f036c86030f05f0419f9`](https://github.com/apache/rocketmq/tree/efa1be59d6fc747ad220f036c86030f05f0419f9)。

在经典 Java 消费链路中，Broker 的 `ConsumerOffsetManager` 负责维护消费进度。我们先只看它如何构造索引：

**真实源码节选（省略无关内容，不改变语义）**，文件 [`broker/src/main/java/org/apache/rocketmq/broker/offset/ConsumerOffsetManager.java`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/broker/src/main/java/org/apache/rocketmq/broker/offset/ConsumerOffsetManager.java#L1471-L1509)：

```java
String key = topic + TOPIC_GROUP_SEPARATOR + group;
this.commitOffset(clientHost, key, queueId, offset);
```

`key` 区分 Topic 与消费组，而每个 `key` 下再用 `queueId` 定位具体队列的进度。输入来自消费进度更新请求；效果是更新 Broker 中的 Offset 表，之后消费者可查询它。注意：这不是写入 CommitLog 消息正文，也不是业务操作成功的证据。

在 `queryOffset()` 中，同一套 `(topic, group, queueId)` 会被用来找回进度。RocketMQ 还会持久化这份配置数据；我们现在还不需要深入它的磁盘文件格式。

## C1 业务成功后，Broker 为什么仍可能只知道旧 Offset？

把消费进度交给 Broker 保存，听上去已经解决重启问题。但还存在一个时间窗口：

```text
C1 读取 offset 0
       ↓
业务成功，C1 内存 next_offset = 1
       ↓
C1 尚未来得及把进度提交到 Broker
       ↓
C1 崩溃
       ↓
C2 读取 Broker 中的旧进度 0
       ↓
重新得到 offset 0
```

这里的失败不要求 Broker 出故障。**进度还在消费者内存里时，消费者自身的崩溃就足以造成重复读取。**

RocketMQ 的客户端为什么会出现本地与远端两层进度？去看经典客户端的 `RemoteBrokerOffsetStore`：

**真实源码节选**，文件 [`client/src/main/java/org/apache/rocketmq/client/consumer/store/RemoteBrokerOffsetStore.java`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/client/src/main/java/org/apache/rocketmq/client/consumer/store/RemoteBrokerOffsetStore.java#L842-L875)：

```java
long brokerOffset = this.fetchConsumeOffsetFromBroker(mq);
this.updateOffset(mq, brokerOffset, false);
return brokerOffset;
```

这是从 Broker 读取已保存位置的正常路径；读取到的位置也会更新到客户端内存。反方向则由 `persist()` / `persistAll()` 把内存进度提交给 Broker。它们不是一个动作。

再沿调用者向上一层走。`MQClientInstance` 会周期调用 `persistAllConsumerOffset()`：

**真实源码关键调用**，文件 [`client/src/main/java/org/apache/rocketmq/client/impl/factory/MQClientInstance.java`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/client/src/main/java/org/apache/rocketmq/client/impl/factory/MQClientInstance.java#L2886-L2897)：

```java
MQClientInstance.this.persistAllConsumerOffset();
```

至此可以解释为什么业务已经执行，但 Broker 端进度有时仍落后：**业务操作与消费者进度提交不处在同一个原子事务里**。周期提交只是一条常见路径；队列移除和正常关闭等时机也可能触发提交，不能把它理解为“无论何时都只能定时提交”。

我们在第 04 章已经做过业务幂等。它在这里不是锦上添花，而是用来处理这个不可避免的重新交付窗口。

## Master B 已经拿到所有消息，它的 Offset 也会完全相同吗？

现在回到主从切换。

把单消费者的内存进度提交到 A，仍不意味着 B 知道了相同进度。这是因为目前出现了两份不同的数据：

```text
Master A                        Replica B

消息正文 offset 0、1、2  ──────>  消息正文 offset 0、1、2

消费进度 next_offset=2   - - ->  消费进度 next_offset=0
                                   （尚未追上）
```

A 已知道 `notification-service` 应从 2 继续，但 B 只知道从 0 继续。此时把 B 提升为 Master，新消费者查询到的就是 B 的进度。

**即使 B 的消息正文一条不缺，也可能重读 offset 0 和 1。**

这不是假设 RocketMQ 完全不复制 Offset。相反，固定版本里有一条明确的同步路径，重点恰恰在于：这条路径与普通消息数据复制不同。

**真实源码节选**，文件 [`broker/src/main/java/org/apache/rocketmq/broker/slave/SlaveSynchronize.java`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/broker/src/main/java/org/apache/rocketmq/broker/slave/SlaveSynchronize.java#L930-L955)：

```java
ConsumerOffsetSerializeWrapper offsetWrapper =
    this.brokerController.getBrokerOuterAPI().getAllConsumerOffset(masterAddrBak);
```

这个调用从 Master 读取消费进度的序列化数据，随后同步到 Slave 的 Offset 表并持久化。在 `syncAll()` 中也可以看到 `syncConsumerOffset()` 作为独立操作被调用。它的存在说明消费进度需要自己维护同步过程，**不能因为第 14 章已经验证 CommitLog 复制，就自动宣称消费进度同步也已完成**。

至于 Controller 模式下的实际同步触发、延迟与故障竞态，需要结合部署与运行日志验证。本章只验证这个必要的边界，不虚构一个固定的同步时间间隔。

## 消费者 C2 接管后，谁告诉它应该读哪一个队列？

再推进一层。A 出故障时，原先处理这个队列的消费者 C1 可能也下线，队列需要由 C2 接手。

让 C2 直接继承 C1 的内存变量肯定不可行：C1 已经不在了。我们需要两步：

1. 确定这个队列现在由哪个消费者负责；
2. 新负责人从 Broker 读取这个消费组的进度，并从那里开始拉取。

第一步叫**队列重分配（Rebalance）**。它不创造消息、不保证业务幂等，而是决定队列交给谁。第二步使用的仍是上面已经理解的 Offset。

在经典客户端侧队列分配路径中，`RebalanceImpl` 根据消费者集合和队列集合计算分配结果。接着，新增队列时才计算起始位置：

**真实源码节选**，文件 [`client/src/main/java/org/apache/rocketmq/client/impl/consumer/RebalanceImpl.java`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/client/src/main/java/org/apache/rocketmq/client/impl/consumer/RebalanceImpl.java#L2829-L2869)：

```java
nextOffset = this.computePullFromWhereWithException(mq);
```

返回值用于构造 `PullRequest` 的 `nextOffset`。这一行之所以必要，是因为**新队列的负责人不能从任意位置开始消费**。

真正决定如何计算初始位置的是 `RebalancePushImpl.computePullFromWhereWithException()`。它优先尝试读已经保存的 Offset；如果不存在，还需要根据消费起点策略决定如何初始化。

**真实源码节选**，文件 [`client/src/main/java/org/apache/rocketmq/client/impl/consumer/RebalancePushImpl.java`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/client/src/main/java/org/apache/rocketmq/client/impl/consumer/RebalancePushImpl.java#L1055-L1084)：

```java
long lastOffset = offsetStore.readOffset(mq, ReadOffsetType.READ_FROM_STORE);
if (lastOffset >= 0) {
    result = lastOffset;
}
```

所以 C2 并不是凭空知道 C1 处理到哪儿，而是通过消费组共享的进度记录恢复。

需要限定范围：RocketMQ 5.x 还存在 Broker 端分配与 POP 消费路径，不能把上述经典 Pull/Push 的 `OffsetStore` 语义套到所有消费模式。我们在这里仅沿一条已核实的经典客户端消费路径继续分析；POP 的不可见时间、ACK 已在前面章节单独学习。

### 如果根本查不到 Offset 呢？

“查不到进度”并不等同于“从零开始”，也不等同于“系统一定停机”。

在固定版本的经典 `CONSUME_FROM_LAST_OFFSET` 分支中，首次启动且 Broker 不存在该消费组进度时，普通 Topic 可以从队列当前最大 Offset 开始；如果选了 `CONSUME_FROM_FIRST_OFFSET`，则可能从最早位置开始。不同消费模型和配置还有自己的规则。

因此，**故障恢复时不能随意把已有进度清空，再假装一个全新消费组启动**：选择“从末尾开始”可能跳过尚未处理的历史消息，而选择“从头开始”会造成重放。

如果 Broker 无法连接，读取进度本身也可能失败；源码中能看到异常分支，而不是总有一个安全的数字可以使用。

## 用同一组订单事件验证这条链

我们现在需要一个足够小的实验，验证四件事：

- 消息正文可以被复制，而消费进度仍可能落后；
- 消费者处理成功只更新内存位置，不等于已写入 Broker；
- C2 接管后使用消费组进度，旧路由需要刷新；
- 重放发生时，业务去重能够避免重复写入相同效果。

为了让实验在没有 RocketMQ、没有 API Key 的情况下运行，下面使用 **SQLite 文件模拟两个 Broker 和一个业务数据库**。数据库仅用于保存实验数据，并不等于 RocketMQ 的真实存储引擎。复制由显式调用 `copy_messages_to()` 和 `copy_progress_to()` 模拟，不包含网络重传、批次调度、选主协议或生产环境的 IO 时序。

所有代码（包括测试）都在这一篇文档里，不需要下载额外的 `.py` 文件。把下一段保存为 `chapter17_lab.py`，然后执行 `python chapter17_lab.py` 即可。

```python
"""教学化简：消费位移在主从切换与消费者接管时如何恢复。
只使用 Python 标准库；不是 RocketMQ 的实现。
"""
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

TOPIC = "OrderRecovery"
GROUP = "notification-service"
QUEUE = 0


@contextmanager
def connection(filename):
    db = sqlite3.connect(filename)
    try:
        with db:
            yield db
    finally:
        db.close()


class BrokerStore:
    def __init__(self, filename):
        self.filename = str(filename)
        with connection(self.filename) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS messages (
                topic TEXT, queue_id INTEGER, offset INTEGER,
                event_id TEXT, PRIMARY KEY(topic, queue_id, offset))""")
            db.execute("""CREATE TABLE IF NOT EXISTS progress (
                group_id TEXT, topic TEXT, queue_id INTEGER, next_offset INTEGER,
                PRIMARY KEY(group_id, topic, queue_id))""")

    def append(self, event_id, topic=TOPIC, queue_id=QUEUE):
        with connection(self.filename) as db:
            next_offset = db.execute(
                "SELECT COALESCE(MAX(offset) + 1, 0) FROM messages WHERE topic=? AND queue_id=?",
                (topic, queue_id),
            ).fetchone()[0]
            db.execute("INSERT INTO messages VALUES (?, ?, ?, ?)",
                       (topic, queue_id, next_offset, event_id))
        return next_offset

    def end(self, topic=TOPIC, queue_id=QUEUE):
        with connection(self.filename) as db:
            return db.execute(
                "SELECT COALESCE(MAX(offset) + 1, 0) FROM messages WHERE topic=? AND queue_id=?",
                (topic, queue_id),
            ).fetchone()[0]

    def read(self, offset, topic=TOPIC, queue_id=QUEUE):
        with connection(self.filename) as db:
            row = db.execute(
                "SELECT event_id FROM messages WHERE topic=? AND queue_id=? AND offset=?",
                (topic, queue_id, offset),
            ).fetchone()
        return None if row is None else row[0]

    def commit(self, group, next_offset, topic=TOPIC, queue_id=QUEUE):
        with connection(self.filename) as db:
            db.execute("""INSERT INTO progress VALUES (?, ?, ?, ?)
                ON CONFLICT(group_id, topic, queue_id) DO UPDATE
                SET next_offset=excluded.next_offset""",
                (group, topic, queue_id, next_offset))

    def checkpoint(self, group, topic=TOPIC, queue_id=QUEUE):
        with connection(self.filename) as db:
            row = db.execute("""SELECT next_offset FROM progress
                WHERE group_id=? AND topic=? AND queue_id=?""",
                (group, topic, queue_id)).fetchone()
        return None if row is None else row[0]

    def copy_messages_to(self, replica):
        with connection(self.filename) as source, connection(replica.filename) as target:
            rows = source.execute("SELECT * FROM messages").fetchall()
            target.executemany("INSERT OR IGNORE INTO messages VALUES (?, ?, ?, ?)", rows)

    def copy_progress_to(self, replica):
        with connection(self.filename) as source, connection(replica.filename) as target:
            rows = source.execute("SELECT * FROM progress").fetchall()
            target.executemany("""INSERT INTO progress VALUES (?, ?, ?, ?)
                ON CONFLICT(group_id, topic, queue_id) DO UPDATE
                SET next_offset=excluded.next_offset""", rows)


class BusinessEffects:
    def __init__(self, filename):
        self.filename = str(filename)
        with connection(self.filename) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS applied (
                group_id TEXT, event_id TEXT, PRIMARY KEY(group_id, event_id))""")

    def apply(self, group, event_id):
        with connection(self.filename) as db:
            cursor = db.execute("INSERT OR IGNORE INTO applied VALUES (?, ?)", (group, event_id))
            return cursor.rowcount == 1

    def count(self, group=GROUP):
        with connection(self.filename) as db:
            return db.execute("SELECT COUNT(*) FROM applied WHERE group_id=?", (group,)).fetchone()[0]


class Cluster:
    def __init__(self, a, b):
        self.nodes = {"A": a, "B": b}
        self.master = "A"

    def access(self, cached_address):
        if cached_address != self.master:
            raise ConnectionError(f"旧路由 {cached_address} 已不能写/读主节点，当前为 {self.master}")
        return self.nodes[cached_address]


class Consumer:
    def __init__(self, cluster, effects, name, group=GROUP):
        self.cluster = cluster
        self.effects = effects
        self.name = name
        self.group = group
        self.cached_address = "A"
        self.next_offset = None
        self.deliveries = []

    def refresh_route(self):
        self.cached_address = self.cluster.master

    def assign(self, missing="error"):
        broker = self.cluster.access(self.cached_address)
        saved = broker.checkpoint(self.group)
        if saved is None:
            if missing == "earliest":
                saved = 0
            elif missing == "latest":
                saved = broker.end()
            else:
                raise ValueError("没有已保存的 Offset；必须明确初始消费策略")
        self.next_offset = saved
        return saved

    def consume_one(self):
        broker = self.cluster.access(self.cached_address)
        if self.next_offset is None:
            raise RuntimeError("尚未分配队列")
        if self.next_offset > broker.end():
            raise RuntimeError("消费进度超出可读消息范围：数据与进度不一致")
        event_id = broker.read(self.next_offset)
        if event_id is None:
            return None
        self.effects.apply(self.group, event_id)
        self.next_offset += 1  # 只更新内存，尚未写入 Broker
        self.deliveries.append(event_id)
        return event_id

    def persist(self):
        if self.next_offset is None:
            raise RuntimeError("尚未分配队列")
        self.cluster.access(self.cached_address).commit(self.group, self.next_offset)


class OffsetFailoverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.a = BrokerStore(root / "a.sqlite3")
        self.b = BrokerStore(root / "b.sqlite3")
        self.effects = BusinessEffects(root / "business.sqlite3")
        self.cluster = Cluster(self.a, self.b)
        self.events = ["replay:msg-2002", "replay:msg-2002-next", "audit:msg-2002"]
        for event in self.events:
            self.a.append(event)
        self.a.commit(GROUP, 0)
        self.a.copy_messages_to(self.b)
        self.a.copy_progress_to(self.b)

    def tearDown(self):
        self.tmp.cleanup()

    def consumer(self, name="C1", group=GROUP):
        return Consumer(self.cluster, self.effects, name, group)

    def test_01_content_survives_replication(self):
        self.assertEqual([self.b.read(i) for i in range(3)], self.events)

    def test_02_local_progress_is_not_broker_checkpoint(self):
        c = self.consumer()
        self.assertEqual(c.assign(), 0)
        c.consume_one()
        c.consume_one()
        self.assertEqual(c.next_offset, 2)
        self.assertEqual(self.a.checkpoint(GROUP), 0)

    def test_03_restart_replays_without_persist(self):
        c = self.consumer()
        c.assign()
        c.consume_one()
        recovered = self.consumer("C2")
        self.assertEqual(recovered.assign(), 0)
        self.assertEqual(recovered.consume_one(), self.events[0])
        self.assertEqual(self.effects.count(), 1)

    def test_04_committed_and_replicated_progress_recovers(self):
        c = self.consumer()
        c.assign()
        c.consume_one()
        c.consume_one()
        c.persist()
        self.a.copy_progress_to(self.b)
        self.cluster.master = "B"
        recovered = self.consumer("C2")
        recovered.refresh_route()
        self.assertEqual(recovered.assign(), 2)
        self.assertEqual(recovered.consume_one(), self.events[2])

    def test_05_offset_lag_on_replica_replays(self):
        c = self.consumer()
        c.assign()
        c.consume_one()
        c.consume_one()
        c.persist()  # A=2，B=0；消息内容已经全部同步
        self.cluster.master = "B"
        recovered = self.consumer("C2")
        recovered.refresh_route()
        self.assertEqual(recovered.assign(), 0)
        self.assertEqual(recovered.consume_one(), self.events[0])
        self.assertEqual(self.effects.count(), 2)  # 重放不增加业务效果

    def test_06_stale_route_blocks_resume_until_refresh(self):
        self.cluster.master = "B"
        c = self.consumer("C2")
        with self.assertRaises(ConnectionError):
            c.assign()
        c.refresh_route()
        self.assertEqual(c.assign(), 0)

    def test_07_reassignment_uses_group_checkpoint(self):
        c1 = self.consumer("C1")
        c1.assign()
        c1.consume_one()
        c1.persist()
        # C1 不再负责该队列，C2 开始接管；教学程序显式执行分配
        c2 = self.consumer("C2")
        self.assertEqual(c2.assign(), 1)
        self.assertEqual(c2.consume_one(), self.events[1])

    def test_08_groups_have_independent_progress(self):
        self.a.commit("report-service", 0)
        c = self.consumer("C1")
        c.assign()
        c.consume_one()
        c.persist()
        report = self.consumer("R1", "report-service")
        self.assertEqual(report.assign(), 0)
        self.assertEqual(report.consume_one(), self.events[0])
        self.assertEqual(self.effects.count("report-service"), 1)

    def test_09_missing_progress_requires_explicit_policy(self):
        new_group = self.consumer("N1", "brand-new-group")
        with self.assertRaises(ValueError):
            new_group.assign()
        self.assertEqual(new_group.assign("earliest"), 0)
        self.assertEqual(new_group.assign("latest"), 3)

    def test_10_progress_ahead_of_data_fails_explicitly(self):
        self.b.commit(GROUP, 5)
        self.cluster.master = "B"
        c = self.consumer("C2")
        c.refresh_route()
        c.assign()
        with self.assertRaises(RuntimeError):
            c.consume_one()

    def test_11_duplicate_effect_is_deduplicated_by_event_id(self):
        c = self.consumer()
        c.assign()
        c.consume_one()
        self.assertEqual(self.effects.count(), 1)
        restart = self.consumer("C2")
        restart.assign()
        restart.consume_one()
        self.assertEqual(self.effects.count(), 1)

    def test_12_no_message_returns_none_without_advancing(self):
        self.a.commit(GROUP, 3)
        c = self.consumer()
        self.assertEqual(c.assign(), 3)
        self.assertIsNone(c.consume_one())
        self.assertEqual(c.next_offset, 3)


def demo():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        a, b = BrokerStore(root / "a.db"), BrokerStore(root / "b.db")
        business = BusinessEffects(root / "business.db")
        cluster = Cluster(a, b)
        for event_id in ("replay:msg-2002", "replay:msg-2002-next"):
            a.append(event_id)
        a.commit(GROUP, 0)
        a.copy_messages_to(b)
        a.copy_progress_to(b)
        c1 = Consumer(cluster, business, "C1")
        c1.assign()
        print("C1 首次处理:", c1.consume_one())
        print("C1 内存 Offset:", c1.next_offset, "A 已提交 Offset:", a.checkpoint(GROUP))
        cluster.master = "B"
        c2 = Consumer(cluster, business, "C2")
        c2.refresh_route()
        print("C2 从 B 恢复 Offset:", c2.assign())
        print("C2 再次收到:", c2.consume_one())
        print("业务效果记录数:", business.count())


if __name__ == "__main__":
    demo()
    unittest.main(argv=["ignored"], exit=False, verbosity=2)
```

这份教学程序在最初的演示中会打印：

```text
C1 首次处理: replay:msg-2002
C1 内存 Offset: 1 A 已提交 Offset: 0
C2 从 B 恢复 Offset: 0
C2 再次收到: replay:msg-2002
业务效果记录数: 1
```

这次实验刻意让主从故障发生在消费进度尚未提交时，所以 C2 再次收到同一条消息。**第二次收到是真实发生在教学程序里的，第二次产生业务效果则被数据库唯一键拦住了。**

在测试 `test_04_committed_and_replicated_progress_recovers` 中，C1 消费了位置 0、1，把 `next_offset=2` 提交到 A，并让 B 同步这份进度，再切换 Master。此时 C2 从 2 继续，不需要重读已经确认的两条消息。

在 `test_05_offset_lag_on_replica_replays` 中，A 已经记录 `next_offset=2`，但 B 的进度仍然是 0；虽然 B 持有全部消息，C2 仍从 0 恢复。这就是我们本章真正想观察的**数据与消费进度不是同一种状态**。

在 `test_06_stale_route_blocks_resume_until_refresh` 中，B 已经接管但 C2 的缓存地址仍然指向 A，第一次 `assign()` 失败；刷新路由后才成功。这说明第 16 章解决的客户端路由问题，对消费者同样重要。

在 `test_07_reassignment_uses_group_checkpoint` 中，C1 已把位置 1 保存到 Broker，C2 接手时从位置 1 继续。这里由测试**显式模拟分配决定**；并没有把真实 RocketMQ 的 Rebalance 网络协议伪装成几行 Python。

最后，`test_09_missing_progress_requires_explicit_policy` 刻意不提供已有进度，分别选择最早位置和最新位置，演示为什么消费起点不是可以随意忽略的细节。

### 本次验证边界

使用 Python 标准库执行了本章的 12 项测试，均通过。实验覆盖文件持久化、独立复制消息/进度、重放幂等、旧路由、消费组隔离以及不存在/越界 Offset 的处理。

这不是对真实 RocketMQ Controller、NameServer 或 Broker 的集成测试；没有验证网络分区、真实 HA 同步延迟、服务端消费分配竞态以及磁盘刷盘时序。教学中的 SQLite 唯一键只说明**同一个业务库可以去重同一事件**，不等于对外发送短信之类的不可回滚副作用具备“精确一次”语义。

## 回看我们现在掌握的恢复过程

到这里，我们终于可以把第 14～17 章的机制放在同一张图里：

```text
生产者
   │
   │ 根据 NameServer 路由找到当前 Master
   ▼
Master A ──消息正文复制────────────▶ Replica B
   │                                 │
   │ 消费组进度记录                   │ 独立的进度同步
   │                                 │
   └──故障──────────────────────────▶ B 晋升 Master
                                        │
             C2 刷新路由 ───────────────┘
                      │
                      ▼
               C2 获得队列分配
                      │
                      ▼
               查询该消费组 Offset
                      │
                      ▼
               从 Offset 开始拉取
                      │
                      ▼
               执行业务与幂等校验
                      │
                      ▼
               更新并提交消费进度
```

这张图里的每条箭头都有前面章节的依据：消息保留依赖存储与复制，新的主节点依赖 Controller 选举，生产者和消费者发现它依赖路由更新，而消费恢复依赖**队列归属 + 可用消息数据 + 足够新的消费进度**。

因此，“Broker 有副本”从来不直接等于“恢复后不会重读”，也不等于“客户端立即恢复消费”。对于一个可靠的业务系统，重复交付是需要正面处理的行为；幂等性不是可以在故障恢复完成之后才考虑的附加项。

## 下一章：让一条消息完整走完这套系统

我们已经在多个章节里分别跟过一条消息的存储、确认、重试、索引、分区、顺序消费、死信、事务消息、主从复制、故障切换、路由刷新和消费进度。

最后一章，我们不再引入一个新的名词，而是从订单 `2002` 的消息产生开始，沿固定源码版本串起**真正的端到端执行链**。

我们要回答一个最终问题：一条消息从生产者发出，到消费者最终完成业务操作，期间每个组件究竟对它做了什么；如果某一步失败，前面学过的哪一种机制负责恢复，又有哪些边界仍需业务自己承担？

---

**源码与文档参考（均为可直接打开的固定版本或官方文档）**

- [RocketMQ 5.5.1 固定 Commit](https://github.com/apache/rocketmq/tree/efa1be59d6fc747ad220f036c86030f05f0419f9)
- [ConsumerOffsetManager.java](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/broker/src/main/java/org/apache/rocketmq/broker/offset/ConsumerOffsetManager.java)
- [RemoteBrokerOffsetStore.java](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/client/src/main/java/org/apache/rocketmq/client/consumer/store/RemoteBrokerOffsetStore.java)
- [SlaveSynchronize.java](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/broker/src/main/java/org/apache/rocketmq/broker/slave/SlaveSynchronize.java)
- [RebalanceImpl.java](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/client/src/main/java/org/apache/rocketmq/client/impl/consumer/RebalanceImpl.java)
- [RebalancePushImpl.java](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/client/src/main/java/org/apache/rocketmq/client/impl/consumer/RebalancePushImpl.java)
- [官方：Consumer Progress Management](https://rocketmq.apache.org/docs/featureBehavior/09consumerprogress/)
