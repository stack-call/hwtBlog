---
slug: /message-queue/rocketmq-message-lifecycle
sidebar_label: 第18章：一条消息的完整生命周期
title: 从生产到消费：一条消息在 RocketMQ 中经历了什么？
sidebar_position: 19
description: 从订单 2002 的恢复消息出发，串联 RocketMQ 5.5.1 的事务确认、CommitLog、ConsumeQueue、消费进度、主从复制、Controller 选主与路由刷新，完成一条可观察、可测试的端到端调用链。
tags: [消息队列, RocketMQ, 源码精读, 消息生命周期, 分布式系统]
---

# 从生产到消费：一条消息在 RocketMQ 中经历了什么？

上一章，Broker B 已经接替 A，消费者也终于可以从新的主节点继续读取消息。至此，消息发送、存储、读取、失败重试、事务确认和故障恢复似乎都已经讲过了。

但如果现在把所有章节放到一边，只给我们一条消息：

```json
{"event_id":"replay:msg-2002","order_id":2002,"event_type":"RecoveryApproved"}
```

我们能不能从头解释它**什么时候被创建、什么时候真正对消费者可见、存在哪里、如何被找出来、何时算处理完成，以及故障之后从哪里恢复**？

这就是收官篇要完成的唯一任务。我们不再引入新机制，只把之前分散的因果链重新走一遍；遇到分支时，说明触发条件，而不是假装每条消息都会经过事务、死信、主从切换。

> 本篇源码固定在 **Apache RocketMQ 5.5.1**，Commit [`efa1be59d6fc747ad220f036c86030f05f0419f9`](https://github.com/apache/rocketmq/tree/efa1be59d6fc747ad220f036c86030f05f0419f9)。正文的 Java 片段标注为“真实源码节选”，Python 为“教学化简”。Python 只验证设计约束，不是启动 RocketMQ Broker 进行集成测试。

<!-- truncate -->

## 先只问一个问题：消息发出后，是不是马上就能消费？

对于最普通的消息，生产者找到目标 Broker，发送请求，Broker 接受并保存消息，之后消费者才能读取。

但我们的贯穿案例是“恢复死信消息”。上两章之前，我们已经知道：**本地业务是否允许恢复，必须先有一个确定的结果**。如果仍然用普通消息抢先发送，可能出现“消费者已经执行恢复，但业务数据库最终拒绝恢复”的错误。

于是我们沿用了第 13 章的事务消息路径。它与普通消息的差别，只发生在消息变得可见之前：

```text
生产者提交半事务消息
          ↓
Broker 保存待确认消息（此时不可正常消费）
          ↓
业务数据库决定是否批准恢复
          ↓
生产者发送 COMMIT / ROLLBACK
          ↓
COMMIT → 生成可正常消费的消息
ROLLBACK → 不进入正常消费路径
```

如果二次确认丢失，则需要由事务回查重新确认本地事务事实；**回查不是让 Broker 自己猜测业务成功与否**。

### 到真实源码核对：谁决定消息可见？

固定版本的 [`EndTransactionProcessor`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/broker/src/main/java/org/apache/rocketmq/broker/processor/EndTransactionProcessor.java) 处理事务结束请求。其中 COMMIT 分支包含以下关键代码：

```java title="真实源码节选：EndTransactionProcessor，省略不影响当前结论的代码"
result = this.brokerController.getTransactionalMessageService().commitMessage(requestHeader);
// ... 校验半事务消息与本次请求 ...
MessageExtBrokerInner msgInner = endMessageTransaction(result.getPrepareMessage());
```

上游是生产者发来的事务结束请求；输入是本次请求头和此前保存的半事务消息；动作是核对待提交消息并构造可正常投递的消息；后面还有真正将其写回消息存储的步骤。我们不能把这里的 `commitMessage()` 误读成“应用已经消费成功”，它处理的是**消息从事务待定状态进入可消费状态的决定**。

现在可以把“可见性”暂时放下。`replay:msg-2002` 已被允许消费，接下来它需要真正写进 Broker 的存储结构。

## 一条可消费消息究竟保存在哪里？

第 02 章只有一个追加日志。读者已经知道，只要消息正文被持续追加，我们就不需要每次重写整份文件。但到了第 06 章又出现问题：当一个 Topic 中的消息很多时，按逻辑消费顺序逐条扫描整个日志太昂贵。

所以在 RocketMQ 中，我们先追踪**写入正文**，再追踪**按队列读取时怎样定位它**。这不是两个独立的业务消息，而是同一条消息的两种位置：

```text
逻辑位置：OrderRecovery / Queue 0 / Offset 0
                             │
                             ▼
                         ConsumeQueue
                             │ 记录物理位置
                             ▼
物理位置：               CommitLog
                             │
                             ▼
                     replay:msg-2002 正文
```

在 [`DefaultMessageStore.asyncPutMessage()`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/store/src/main/java/org/apache/rocketmq/store/DefaultMessageStore.java) 中可以找到：

```java title="真实源码节选：DefaultMessageStore.asyncPutMessage()"
CompletableFuture<PutMessageResult> putResultFuture = this.commitLog.asyncPutMessage(msg);
```

调用方来自 Broker 接收写请求后的存储路径；这里把消息交给 CommitLog，并将异步写入结果向上返回。是否要求同步刷盘或等待副本响应，属于上一章已解释的配置和确认条件，不能把一个普通的 `CompletableFuture` 简化成“磁盘与所有副本都成功了”。

而在 [`DefaultMessageStore.getMessage()`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/store/src/main/java/org/apache/rocketmq/store/DefaultMessageStore.java) 中，读取端能看到另一半关系：

```java title="真实源码节选：DefaultMessageStore.getMessage()，省略中间的迭代和检查"
ConsumeQueueInterface consumeQueue = findConsumeQueue(topic, queueId);
// ... 从 ConsumeQueue 取得 CqUnit，得到 offsetPy 和 sizePy ...
SelectMappedBufferResult selectResult = this.commitLog.getMessage(offsetPy, sizePy);
```

输入中的 `topic`、`queueId` 和 `offset` 来自消费方的读取请求。这里先定位逻辑队列，再取消息的物理位置与大小，最终去 CommitLog 拿到正文。被消费的不是“从磁盘中删掉的一条任务”，而是从日志读取出来的一条记录。

现在消息能被准确找到，但还不能说已经被成功处理。下一步是消费者。

## 消费者读取了这条消息，怎样知道下次从哪里开始？

我们在第 03 章已经区分了“读取”与“确认”；在第 17 章又区分了“消息被复制到 B”与“消费进度也已经同步到 B”。

对于这里选用的**经典拉取消费路径**，最小的正常流程可以写成：

```text
Consumer C1 获取 Queue 0 的消费位置 0
          ↓
向 Broker 拉取 Offset 0 的消息
          ↓
执行恢复后的通知业务
          ↓
业务成功，提交下一次读取位置 1
```

在固定版本的 [`DefaultMQPushConsumerImpl.pullMessage()`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/client/src/main/java/org/apache/rocketmq/client/impl/consumer/DefaultMQPushConsumerImpl.java) 中，客户端从 `PullRequest` 提取当前队列的 `ProcessQueue`，再进入实际拉取路径；Broker 侧的 [`ConsumerOffsetManager.queryOffset()`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/broker/src/main/java/org/apache/rocketmq/broker/offset/ConsumerOffsetManager.java) 根据消费组、Topic 和队列 ID 查询持久化消费位置：

```java title="真实源码节选：ConsumerOffsetManager.queryOffset()"
String key = topic + TOPIC_GROUP_SEPARATOR + group;
ConcurrentMap<Integer, Long> map = this.offsetTable.get(key);
```

这不是说每处理完一条消息都会立刻执行一次上面的查询。它解释的是：**消费进度不是消息本身的一部分**，需要按消费组和队列单独维护。

如果 C1 的业务数据库已经写入通知意图，消费者却在提交 Offset 前崩溃，新消费者 C2 仍可能再次读到 `replay:msg-2002`。这正是第 04 章为什么要让业务数据库基于事件身份做幂等处理。

## 如果就在这时，Master A 故障了呢？

先只考虑数据副本：A 已经把消息同步给 B，B 能读到相同日志。第 14 章的复制路径由 [`DefaultHAConnection`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/store/src/main/java/org/apache/rocketmq/store/ha/DefaultHAConnection.java) 等类承载，副本的复制偏移用来描述它追到了哪里。

但“A 不可访问”并不自动意味着“B 可以合法接受所有写入”。第 15 章让我们理解，需要 Controller 根据安全候选与角色任期完成选主；第 16 章又发现，Controller 的决定并不会神奇地覆盖生产者内存中的旧地址。

因此，故障恢复至少涉及三份可能在不同时间更新的信息：

```text
① Controller：谁是当前 Master？
② NameServer：Topic 路由现在指向谁？
③ 客户端缓存：我手里保存的 Broker 地址是谁？
```

固定版本的 [`RouteInfoManager.pickupTopicRouteData()`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/namesrv/src/main/java/org/apache/rocketmq/namesrv/routeinfo/RouteInfoManager.java) 从 Topic 对应的队列与 Broker 元数据组装路由；客户端的 [`MQClientInstance.updateTopicRouteInfoFromNameServer()`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/client/src/main/java/org/apache/rocketmq/client/impl/factory/MQClientInstance.java) 负责刷新本地路由。

即使路由恢复，**消费者也还需要消费进度**。B 可以拥有完整消息而只知道旧 Offset，于是 C2 会重复读取；如果重复消息被业务幂等识别，就不会重复写入同一条通知意图。反过来，副本落后、路由仍旧或没有合法新主，消费也可能暂时停止。系统必须接受恢复过程中的窗口，而不是通过一张“自动切换”箭头图抹掉它。

现在，我们已经把每个机制放回了它应出现的位置。下面使用一个完整的 Python 文件，把上述关键状态变化实际跑一遍。

## 一次运行，观察整条链路

为了避免读者需要复制十几个小片段，下面只有**一段完整可运行的 Python 代码**。它使用标准库、无需 API Key、不需要安装 RocketMQ。将整段保存为 `chapter18_lifecycle.py`，运行 `python chapter18_lifecycle.py` 即可执行演示和测试。

注意它是一份**教学化简**：`list` 不是磁盘 CommitLog，`dict` 不是 NameServer 或 Controller 共识实现，选主前的列表比较不是 `SyncStateSet` 或选举协议，`set` 仅模拟业务库唯一键效果。代码特意把这些边界显式留在注释里；它验证的是我们一路推导得到的行为，而不是声称复刻了 RocketMQ 内部实现。

```python title="教学化简：完整可运行的消息生命周期实验"
"""第18章教学化简：一个消息的写入、读取、消费进度和故障恢复。
没有网络、磁盘、真实 RocketMQ，也不模拟严格的复制/一致性协议。
"""
from __future__ import annotations

from copy import deepcopy
import unittest

EVENT = {"event_id": "replay:msg-2002", "order_id": 2002,
         "event_type": "RecoveryApproved"}
NEXT = {"event_id": "replay:msg-2002-next", "order_id": 2002,
        "event_type": "NotificationRequested"}


class Broker:
    def __init__(self, name):
        self.name = name
        self.alive = True
        self.prepared = {}       # 事务消息尚未可见：教学用状态
        self.log = []           # 物理存储：教学用内存数组
        self.index = []         # 逻辑队列位置 -> log 下标
        self.offsets = {}       # (consumer_group) -> 下次读取位置

    def append(self, event):
        if not self.alive:
            raise ConnectionError(f"Broker {self.name} is unavailable")
        physical = len(self.log)
        self.log.append(deepcopy(event))
        self.index.append(physical)
        return len(self.index) - 1

    def read(self, group):
        if not self.alive:
            raise ConnectionError(f"Broker {self.name} is unavailable")
        queue_offset = self.offsets.get(group, 0)
        if queue_offset >= len(self.index):
            return None
        return queue_offset, deepcopy(self.log[self.index[queue_offset]])

    def ack_offset(self, group, next_offset):
        if not self.alive:
            raise ConnectionError(f"Broker {self.name} is unavailable")
        if not 0 <= next_offset <= len(self.index):
            raise ValueError("Invalid next offset")
        self.offsets[group] = next_offset

    def copy_to(self, replica, *, with_offsets=False):
        # 只复制已提交、可消费的模拟日志；真实系统的复制更复杂。
        replica.log = deepcopy(self.log)
        replica.index = list(self.index)
        if with_offsets:
            replica.offsets = dict(self.offsets)


class System:
    def __init__(self):
        self.brokers = {"A": Broker("A"), "B": Broker("B")}
        self.master = "A"          # Controller 决定的主节点
        self.route = "A"           # NameServer 发布的地址
        self.cached_route = "A"    # Producer 当前缓存的地址
        self.decisions = {}        # 本地业务事务结果
        self.effects = set()       # 模拟数据库已应用的事件 ID

    def prepare(self, event):
        self.brokers[self.master].prepared[event["event_id"]] = deepcopy(event)

    def decide(self, event_id, decision):
        assert decision in {"COMMIT", "ROLLBACK"}
        self.decisions[event_id] = decision

    def resolve_transaction(self, event_id):
        broker = self.brokers[self.master]
        event = broker.prepared.get(event_id)
        if event is None:
            return "ALREADY_RESOLVED"
        decision = self.decisions.get(event_id)
        if decision is None:
            return "UNKNOWN"
        del broker.prepared[event_id]
        if decision == "ROLLBACK":
            return "ROLLED_BACK"
        broker.append(event)
        return "COMMITTED"

    def publish(self, event):
        # 普通消息发送；事务消息使用 prepare + resolve_transaction。
        target = self.cached_route
        if target != self.master:
            raise ConnectionError("Stale route: not the current master")
        return self.brokers[target].append(event)

    def publish_new_route(self):
        self.route = self.master

    def refresh_producer_route(self):
        self.cached_route = self.route

    def promote_replica(self):
        primary, replica = self.brokers["A"], self.brokers["B"]
        # 只作为可观察的教学前置条件，不是 RocketMQ 的选主算法。
        if replica.log != primary.log:
            raise RuntimeError("Replica is not caught up")
        primary.alive = False
        self.master = "B"

    def consume_one(self, group="notification", crash_after_effect=False):
        broker = self.brokers[self.master]
        item = broker.read(group)
        if item is None:
            return "EMPTY"
        offset, event = item
        event_id = event["event_id"]
        result = "DUPLICATE" if event_id in self.effects else "APPLIED"
        self.effects.add(event_id)    # 模拟唯一键保障的本地业务效果
        if crash_after_effect:
            return "CRASH_AFTER_EFFECT"  # 故意不提交消费进度
        broker.ack_offset(group, offset + 1)
        return result


def demo():
    s = System()
    print("1  PREPARE:", end=" ")
    s.prepare(EVENT)
    print("visible=", len(s.brokers["A"].index))
    print("2  BUSINESS COMMIT AND CHECK:", end=" ")
    s.decide(EVENT["event_id"], "COMMIT")
    print(s.resolve_transaction(EVENT["event_id"]))
    s.brokers["A"].copy_to(s.brokers["B"], with_offsets=False)
    print("3  REPLICA:", len(s.brokers["B"].index), "visible record(s)")
    print("4  CONSUMER CRASH:", s.consume_one(crash_after_effect=True))
    print("5  PROMOTE:", end=" ")
    s.promote_replica()
    print("master=", s.master)
    print("6  REDELIVERY:", s.consume_one(), "business effects=", len(s.effects))
    try:
        s.publish(NEXT)
    except ConnectionError:
        print("7  STALE ROUTE: send failed")
    s.publish_new_route()
    s.refresh_producer_route()
    s.publish(NEXT)
    print("8  ROUTE REFRESHED: sent to", s.cached_route)
    print("9  NEW EVENT:", s.consume_one(), "business effects=", len(s.effects))
    return s


class MessageLifecycleTests(unittest.TestCase):
    def test_prepared_message_is_not_visible(self):
        s = System()
        s.prepare(EVENT)
        self.assertIsNone(s.brokers["A"].read("notification"))
        self.assertEqual(s.resolve_transaction(EVENT["event_id"]), "UNKNOWN")

    def test_commit_makes_message_visible_through_index(self):
        s = System()
        s.prepare(EVENT)
        s.decide(EVENT["event_id"], "COMMIT")
        self.assertEqual(s.resolve_transaction(EVENT["event_id"]), "COMMITTED")
        self.assertEqual(s.brokers["A"].index, [0])
        self.assertEqual(s.brokers["A"].read("notification")[1], EVENT)

    def test_rollback_is_not_consumed(self):
        s = System()
        s.prepare(EVENT)
        s.decide(EVENT["event_id"], "ROLLBACK")
        self.assertEqual(s.resolve_transaction(EVENT["event_id"]), "ROLLED_BACK")
        self.assertIsNone(s.brokers["A"].read("notification"))

    def test_effect_before_ack_causes_redelivery_not_double_effect(self):
        s = System()
        s.publish(EVENT)
        self.assertEqual(s.consume_one(crash_after_effect=True), "CRASH_AFTER_EFFECT")
        self.assertEqual(s.consume_one(), "DUPLICATE")
        self.assertEqual(len(s.effects), 1)
        self.assertEqual(s.brokers["A"].offsets["notification"], 1)

    def test_replica_with_stale_offset_repeats_message(self):
        s = System()
        s.publish(EVENT)
        s.brokers["A"].copy_to(s.brokers["B"], with_offsets=False)
        self.assertEqual(s.consume_one(), "APPLIED")
        s.promote_replica()
        self.assertEqual(s.consume_one(), "DUPLICATE")
        self.assertEqual(len(s.effects), 1)

    def test_replica_with_synced_offset_starts_after_consumed(self):
        s = System()
        s.publish(EVENT)
        self.assertEqual(s.consume_one(), "APPLIED")
        s.brokers["A"].copy_to(s.brokers["B"], with_offsets=True)
        s.promote_replica()
        self.assertEqual(s.consume_one(), "EMPTY")

    def test_producer_needs_refreshed_route(self):
        s = System()
        s.publish(EVENT)
        s.brokers["A"].copy_to(s.brokers["B"])
        s.promote_replica()
        with self.assertRaises(ConnectionError):
            s.publish(NEXT)
        s.publish_new_route()
        with self.assertRaises(ConnectionError):
            s.publish(NEXT)
        s.refresh_producer_route()
        self.assertEqual(s.publish(NEXT), 1)

    def test_lagging_replica_not_promoted_in_model(self):
        s = System()
        s.publish(EVENT)
        with self.assertRaises(RuntimeError):
            s.promote_replica()
        self.assertEqual(s.master, "A")

    def test_end_to_end_demo(self):
        s = demo()
        self.assertEqual(s.master, "B")
        self.assertEqual(s.brokers["B"].offsets["notification"], 2)
        self.assertEqual(s.effects, {EVENT["event_id"], NEXT["event_id"]})


if __name__ == "__main__":
    unittest.main(verbosity=2)
```

### 运行结果与断言

在本文提供的代码中，`demo()` 会打印一条确定性的事件轨迹：

```text
1  PREPARE: visible= 0
2  BUSINESS COMMIT AND CHECK: COMMITTED
3  REPLICA: 1 visible record(s)
4  CONSUMER CRASH: CRASH_AFTER_EFFECT
5  PROMOTE: master= B
6  REDELIVERY: DUPLICATE business effects= 1
7  STALE ROUTE: send failed
8  ROUTE REFRESHED: sent to B
9  NEW EVENT: APPLIED business effects= 2
```

这九行对应了前面的一整条故事：半事务消息初始不可见；本地业务批准之后成为可读记录；副本拿到日志；消费者的业务已生效但确认前崩溃；B 接管后发生重新交付；旧路由阻塞新消息；刷新路由后才继续发送。请注意第 6 行：重复交付的事件仍只有一份**模拟的数据库效果**。这不等于真实短信等外部动作具有精确一次保证。

九个 `unittest` 断言覆盖：半消息不可见、提交与回滚、索引定位、ACK 前崩溃、进度滞后与同步、旧路由发送失败、不安全候选拒绝接管，以及整条链路最终能继续。

这个实验没有测试真实磁盘断电、同步刷盘、网络分区、DLedger 选主、Broker 的 RPC、真实 POP ACK 或真实外部通知接口。它也**不能**用来评估 RocketMQ 的生产吞吐量、时延或端到端投递保证。

## 现在才能画出完整架构图

在第一篇，我们只有一个 Python 列表。现在每个组件都能找到它对应的失败场景，因此可以把已经理解的部分拼在一起：

```text
                     ┌────────────────────────────┐
                     │     本地业务决策/事务       │
                     └─────────────┬──────────────┘
                                   │
生产者 ──查询/缓存路由── NameServer │
   │                               │
   ├─普通消息───────────────────────┼─────┐
   └─事务半消息 → COMMIT/ROLLBACK ─┘     │
                                         ▼
                              Broker（当前 Master）
                                 │         │
                                 ▼         ▼
                             CommitLog  ConsumeQueue
                                 │          │
                                 │    逻辑队列位置索引
                                 │          │
                                 ├──────────┘
                                 ▼
                          通过 Offset 读取正文
                                 │
                                 ▼
                              消费者 C1
                                 │
                             业务处理成功
                                 │
                         更新消费进度 / 确认
                                 │
                                 ▼
                        消费组下次读取的位置

故障恢复支线（仅故障时发生）：
Master 的日志 ──复制──▶ Replica
             Controller 选主
                  ▼
             新 Master 注册
                  ▼
            NameServer 路由变化
                  ▼
            生产者/消费者刷新并恢复
```

这仍然不是 RocketMQ 所有部署与所有消费模式的统一运行图。**图中主路径以经典拉取模式说明消费位移，事务、POP、FIFO、死信和 Controller 选主是不同条件下的机制**。例如 POP 的领取和 ACK 与经典 Offset 提交并不是同一个 API，MessageGroup 的语义也不能推出全部消费分支都按组独立解锁。

## 如果只能记住三句话

第一，**消息被写入，不代表已经被业务处理**。真正处理成功与否必须观察业务结果和消费侧的确认状态。

第二，**消息副本和消费进度是两种信息**。即使副本完整，消费者在故障恢复后仍可能重复读取；要降低重复副作用，需要业务幂等，而不是寄希望于“消息只来一次”。

第三，**选主成功不等于全系统瞬间完成切换**。NameServer 的路由、客户端缓存、队列归属和消费进度仍会有独立的更新过程。

这也是整套课程最重要的收获：真正的系统复杂性不是来自某个类名，而是来自**一个机制解决了当前问题，却仍然保留了它没有能力解决的边界**。

## 系列结束之后，如何继续阅读源码？

到这里，我们已经从“下单后不要等待通知”走到了“事务消息、持久化、索引、交付、幂等、重试、顺序、分组、复制、选主、路由与恢复”。

如果以后需要单独深入 RocketMQ 的定时消息、过滤、权限控制或不同的 5.x 消费协议，可以沿用同样的办法：先提出一个会失败的最小案例，再找出真实调用链，而不是直接从功能清单开始。

**主线到这里收束。我们现在能说清楚一条消息为什么要经过这些设计，也知道这些设计各自不能保证什么。**

---

## 固定版本源码入口（按本章实际追踪顺序）

| 环节 | 源码文件/入口 | 验证目标 |
| --- | --- | --- |
| 生产端路由与发送 | [`DefaultMQProducerImpl.sendKernelImpl`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/client/src/main/java/org/apache/rocketmq/client/impl/producer/DefaultMQProducerImpl.java) | 发送请求使用实际 Broker 地址 |
| 事务结束 | [`EndTransactionProcessor`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/broker/src/main/java/org/apache/rocketmq/broker/processor/EndTransactionProcessor.java) | COMMIT / ROLLBACK 决策与消息转化 |
| Broker 写入 | [`DefaultMessageStore.asyncPutMessage`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/store/src/main/java/org/apache/rocketmq/store/DefaultMessageStore.java) | 消息进入 CommitLog |
| Broker 读取 | [`DefaultMessageStore.getMessage`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/store/src/main/java/org/apache/rocketmq/store/DefaultMessageStore.java) | ConsumeQueue 定位正文 |
| 经典拉取消费 | [`DefaultMQPushConsumerImpl.pullMessage`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/client/src/main/java/org/apache/rocketmq/client/impl/consumer/DefaultMQPushConsumerImpl.java) | 消费者拉取与本地待处理队列 |
| Broker 消费进度 | [`ConsumerOffsetManager.queryOffset`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/broker/src/main/java/org/apache/rocketmq/broker/offset/ConsumerOffsetManager.java) | 按组/Topic/队列维护 Offset |
| 主从数据复制 | [`DefaultHAConnection`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/store/src/main/java/org/apache/rocketmq/store/ha/DefaultHAConnection.java) | 复制数据与副本进度 |
| NameServer 路由 | [`RouteInfoManager.pickupTopicRouteData`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/namesrv/src/main/java/org/apache/rocketmq/namesrv/routeinfo/RouteInfoManager.java) | 查找 Topic 可用 Broker |
| 客户端路由更新 | [`MQClientInstance.updateTopicRouteInfoFromNameServer`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/client/src/main/java/org/apache/rocketmq/client/impl/factory/MQClientInstance.java) | 本地缓存重新获取地址 |

官方背景资料：[事务消息](https://rocketmq.apache.org/zh/docs/featureBehavior/04transactionmessage/)、[消费重试](https://rocketmq.apache.org/zh/docs/featureBehavior/10consumerretrypolicy/)、[顺序消息](https://rocketmq.apache.org/zh/docs/featureBehavior/03fifomessage/)。
