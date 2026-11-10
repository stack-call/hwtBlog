---
slug: /message-queue/rocketmq-controller-failover
title: 副本里明明有消息，为什么 Broker 故障后仍然不能继续写入？
sidebar_position: 16
description: 沿订单 2002 的消息复制路径，从主节点不可用、旧主失联与双主写入风险推导安全选主、SyncStateSet、masterEpoch 和角色切换，对照 RocketMQ 5.5.1 Controller 源码。
tags: [消息队列, RocketMQ, 源码精读, Controller, 故障切换, 主从选举]
---

# 副本里明明有消息，为什么 Broker 故障后仍然不能继续写入？

上一章，我们终于把订单 `2002` 的恢复事件 `replay:msg-2002` 从 Master A 复制到了副本 B。即使 A 的机器永远无法访问，B 的日志里也还保存着这条消息。

看起来，我们可以继续工作了：让生产者向 B 发消息就好。

然而，此时 B 只是一台**保存了数据的副本**。它还不知道自己是否应该接受新的写入。如果 A 只是网络断开、并没有真正关闭，A 可能还认为自己是 Master。

于是，新问题变成了：**谁有权宣布 B 成为新的 Master？宣布之后，怎样避免旧 Master 继续写入？**

这一章沿用相同的消息，不换业务故事：

```json
{"event_id":"replay:msg-2002","order_id":2002,"event_type":"RecoveryApproved"}
```

我们先构造最小的故障现场，再逐步增加真正必要的机制，最后阅读 RocketMQ 5.5.1 的 Controller 源码。

<!-- truncate -->

## 副本存着消息，却没有接管资格

在上一章结束时，我们的系统只有两个存储节点：

```text
生产者 ──写入──▶ A（Master）──复制──▶ B（Replica）
                     │                      │
             replay:msg-2002        replay:msg-2002
```

如果 A 彻底宕机，B 可以读到原来的消息，但我们还没有规定：**收到新的 `OrderPaid` 或恢复结果消息时，应该写到哪台机器？**

最直觉的办法是：“A 不通，就把 B 设置为 Master。”

```python
# 概念伪代码：不是 RocketMQ 源码
if not reachable("A"):
    broker_b.role = "MASTER"
```

这在“所有参与者都一致认定 A 已永久死亡”的理想条件下似乎够用。但真实网络会制造另一种情况。

A 与负责管理集群的控制节点失去了联系，却仍然可以被一部分生产者访问；B 则与控制节点保持连接。

```text
                  控制侧
                 /    \
           A 不可达     B 可达
              │          │
           ┌──┴──┐       │
生产者 X ─▶│ A   │       │
           │自认主│       │
           └─────┘    ┌──┴──┐
生产者 Y ───────────▶│  B  │
                      │新主？│
                      └─────┘
```

如果 B 直接自我升级，而 A 继续接收写入，同一条业务日志就可能形成两个分支：

```text
A: [replay:msg-2002, old-branch]
B: [replay:msg-2002, new-branch]
```

这不是普通的“消费者重复执行”，也不是靠业务幂等就能彻底掩盖的问题。两个节点对于**哪份日志代表后续正确历史**已经产生分歧。

所以我们真正缺少的不是“在 B 上修改一个角色字符串”，而是**所有节点能够据以判断当前主节点身份的共同依据**。

## 第一次增加新机制：由 Controller 决定谁是 Master

对本章的教学系统，我们可以增加一个保存主节点身份的控制者：

```text
                       Controller
                      当前主：A
                          │
              ┌───────────┴───────────┐
              ▼                       ▼
            A（主）                  B（备）
              └──────复制数据─────────▶
```

请注意，Controller **不替 Broker 保存业务消息正文**。它要回答的是“哪台 Broker 能担任主节点”，而不是“`replay:msg-2002` 的 JSON 存在哪里”。

在 RocketMQ 中，这种模式是需要配置启用的 **Controller 自动主从切换模式**，并非任意部署、任意版本的 Broker 都默认自动选主。Controller 可以独立部署，也可以嵌入 NameServer；二者的职责仍要区分。[官方部署文档](https://rocketmq.apache.org/zh/docs/deploymentOperations/03autofailover/) 对这些配置有明确说明。

但这里又出现了一个限制：Controller 不能随便从所有活着的机器中挑一个，因为**活着不代表日志足够新**。

假设我们多加一台刚刚启动、尚未追上的 C：

```text
A: [replay:msg-2002]   旧主，失联
B: [replay:msg-2002]   已追上
C: []                  活着，但没有这条消息
```

如果 Controller 为了恢复可用性选择 C，那么上一章好不容易复制并确认的数据就可能无法在新主上看到。

因此需要第二个条件：候选者不仅要存活，还应当属于**当前被认为同步的副本集合**。

RocketMQ 把这一集合叫作 `SyncStateSet`。只在这一刻引入它，因为现在我们终于遇到了“怎样排除过期副本”的具体问题。

### 进入真实源码：候选集合从哪里来？

本系列固定源码版本：**Apache RocketMQ 5.5.1**，Commit：`efa1be59d6fc747ad220f036c86030f05f0419f9`。

文件：[`controller/.../manager/ReplicasInfoManager.java` 的 `electMaster()`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/controller/src/main/java/org/apache/rocketmq/controller/impl/manager/ReplicasInfoManager.java#L175-L249)。调用它的是 Controller 的选主执行路径。

下面两行是**真实源码节选**，省略了周边的错误检查和策略调用：

```java
final Set<Long> syncStateSet = syncStateInfo.getSyncStateSet();
Set<Long> allReplicaBrokers = controllerConfig.isEnableElectUncleanMaster() ? brokerReplicaInfo.getAllBroker() : null;
```

第一行取出被认为同步的副本集合。第二行说明系统还留了一个**显式的高风险分支**：如果开启 `enableElectUncleanMaster`，选主策略可以考虑集合外的副本。

这不是“更高级的自动恢复”，而是**在没有合格候选时允许用数据完整性风险换取恢复机会**。如果业务不能接受已确认消息在切换后消失，就不应该随意启用它。

需要强调：`SyncStateSet` 不是教学代码中的“对每条 JSON 做一次严格内容比对”。实际集合由 Broker 根据复制追赶情况维护、向 Controller 汇报；同时，是否允许成功写入还受确认策略与副本数量配置影响。我们稍后写的 Python 只用前缀检查模拟安全直觉，不冒充这条真正的维护路径。

## 第二次增加新机制：选主决定不能只写在一台 Controller 的内存里

现在假设 Controller 选中了 B，但它自己立刻重启，刚才的主节点变更只保存在内存里，那么别的控制节点可能无法重现同一个结果。

于是，“谁能发布主节点身份”也需要有稳定、一致的控制状态。

RocketMQ 5.5.1 的 DLedger Controller 实现里，选主不是直接改 Broker 的 `role` 字段。调用路径可以顺着下面这条线走：

```text
Broker 检测不到主节点 / Controller 检测失联
               │
               ▼
ControllerRequestProcessor.handleControllerElectMaster()
               │
               ▼
DLedgerController.electMaster()
               │
               ▼
ReplicasInfoManager.electMaster() 计算候选与事件
               │
               ▼
Controller 将事件写入 DLedger
               │
               ▼
状态机应用事件，更新主节点元数据
               │
               ▼
Broker 收到通知或定期拉取新的角色信息
```

`DLedgerController.electMaster()` 的入口位于 [`controller/.../impl/DLedgerController.java`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/controller/src/main/java/org/apache/rocketmq/controller/impl/DLedgerController.java#L172-L199)。它调用 `ReplicasInfoManager.electMaster()` 得到包含事件的结果，随后通过事件调度器提交到 DLedger。

在事件写入处，可以看到**真实源码节选**：

```java
request.setBatchMsgs(eventBytes);
appendSuccess = appendToDLedgerAndWait(request);
```

[位置：`DLedgerController.java` 第 429–447 行](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/controller/src/main/java/org/apache/rocketmq/controller/impl/DLedgerController.java#L429-L447)

这里的输入是前面生成的选主事件，而不是消息队列的业务消息；输出是控制事件能否完成 DLedger 提交。随后状态机消费事件，修改复制组的主节点信息。

**不要把两个日志混在一起**：Controller 的 DLedger 用于控制元数据；Broker 的 CommitLog 用于业务消息。我们这章只需要理解“选主决策也要有可靠的状态来源”，不展开 DLedger 的全部共识算法。

## 第三次增加新机制：防止过期角色覆盖新角色

Controller 经过选举让 B 成为 Master，是否意味着问题彻底解决？还没有。

假设 A 隔了一段时间重新连上控制侧；由于消息或网络延迟，它可能先收到一个过时的“你是 Master”通知，然后才收到 B 已当选的最新信息。

如果谁的通知后到就按谁的通知覆盖，角色还会倒退。

我们因此需要给每次主节点任命一个**可比较的新旧标识**，它就是 `masterEpoch`，可以理解为“主节点任期”。

在 [`ReplicasInfoManager.electMaster()`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/controller/src/main/java/org/apache/rocketmq/controller/impl/manager/ReplicasInfoManager.java#L216-L234) 中，新选举结果对应新的 `masterEpoch`。真正应用选主事件时，状态机也更新主节点元数据。[事件应用入口](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/controller/src/main/java/org/apache/rocketmq/controller/impl/manager/ReplicasInfoManager.java#L451-L526)

Broker 收到角色变化后，进入 [`ReplicasManager.changeBrokerRole()`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/broker/src/main/java/org/apache/rocketmq/broker/controller/ReplicasManager.java#L206-L216)。真实源码里有一条关键条件：

```java
if (newMasterBrokerId != null && newMasterEpoch > this.masterEpoch) {
```

**新任期必须更大**，才允许沿这条路径更新角色。这个限制可以防止旧的角色通知轻易覆盖新的角色决定。

如果新角色是本机，`changeToMaster()` 会调整 HA 服务与存储角色；如果新角色不是本机，`changeToSlave()` 会收回本机的主节点职责。阅读 [`ReplicasManager` 第 217–290 行](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/broker/src/main/java/org/apache/rocketmq/broker/controller/ReplicasManager.java#L217-L290) 时要关注这些实际副作用，而不只是“给 role 变量赋值”。

此外，`ReplicasManager` 还有定期向 Controller 获取复制组信息并纠正本机角色的任务：[第 594–624 行](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/broker/src/main/java/org/apache/rocketmq/broker/controller/ReplicasManager.java#L594-L624)。因此，主动通知丢失不代表永久无法获知新的主节点。

不过必须说清楚一个边界：**上述版本检查证明了过期角色通知不能覆盖较新任期，却不等于 RocketMQ 在每一条业务写请求之前都会向 Controller 查询并校验任期。** 旧主在网络隔离期间能做什么，还受 Broker 当前隔离状态、读写服务切换、HA 协议及客户端路由影响，不能仅凭这一处 `if` 就宣布“绝无双主写入”。

为了理解设计目标，接下来我们会在 Python 教学模型中显式加一个*权威写入门禁*，把它当作理想化的 fencing（旧主隔离）约束。**这不是对 RocketMQ 每次写入都 RPC Controller 的实现描述。**

## 可运行实验：一台旧主和一台新主，谁能继续写入？

代码只用 Python 标准库。它把已经复制的消息保存在**内存列表**里，因为上一章已经用磁盘文件验证了复制确认；本章要单独研究控制权，避免重复引入 I/O 代码。

实验起始状态是：A、B 都拥有 `replay:msg-2002`；C 尚未追上；初始 `SyncStateSet={A,B}`；A 失去 Controller 连通性，但客户端仍可能访问它。

我们只模拟选主和角色约束，不模拟真实 Controller 多节点共识、网络心跳、HA 握手、路由刷新或 RocketMQ Broker。完整代码与 **12 项测试**全部内嵌如下，复制成 `failover_lab.py` 即可执行：

```python title="failover_lab.py（完整教学代码与 12 项测试）"
"""教学化简：从复制副本到安全选主。不连接真实 RocketMQ。"""

from copy import deepcopy
from dataclasses import dataclass, field
import unittest


EVENT = {
    "event_id": "replay:msg-2002",
    "order_id": 2002,
    "event_type": "RecoveryApproved",
}


class NoSafeMaster(Exception):
    """没有符合当前安全规则的可用主节点。"""


class StaleMaster(Exception):
    """尝试以旧任期或非当前主节点身份写入。"""


@dataclass
class Replica:
    name: str
    messages: list = field(default_factory=list)
    alive: bool = True
    connected_to_controller: bool = True
    role: str = "SLAVE"
    observed_epoch: int = 0

    def apply_role(self, role, epoch):
        """过时的角色通知不能覆盖较新的任期。"""
        if epoch <= self.observed_epoch:
            return False
        self.role = role
        self.observed_epoch = epoch
        return True

    def naive_append(self, message):
        """错误示范：只看本地角色，不能隔离失联旧主。"""
        if not self.alive or self.role != "MASTER":
            raise StaleMaster(f"{self.name}: not a local master")
        self.messages.append(deepcopy(message))


def has_prefix(records, prefix):
    return records[: len(prefix)] == prefix


class TinyController:
    """教学控制面；不模拟 DLedger 共识、心跳、真实 HA 协议。"""

    def __init__(self, replicas, *, initial_master="A", in_sync=("A", "B")):
        self.replicas = {replica.name: replica for replica in replicas}
        self.master = initial_master
        self.master_epoch = 1
        self.sync_state_set = set(in_sync)
        # 教学前提：这里代表前一章已确认、至少 A/B 都有的消息前缀。
        self.confirmed_prefix = [deepcopy(EVENT)]
        for replica in replicas:
            replica.role = "MASTER" if replica.name == initial_master else "SLAVE"
            replica.observed_epoch = self.master_epoch

    def elect(self, *, allow_unclean=False):
        old = self.replicas[self.master]
        if old.alive and old.connected_to_controller:
            raise NoSafeMaster("当前主节点仍可联系，不应无故切主")

        eligible = [
            replica for replica in self.replicas.values()
            if replica.name != self.master
            and replica.alive
            and replica.connected_to_controller
            and replica.name in self.sync_state_set
            and has_prefix(replica.messages, self.confirmed_prefix)
        ]
        if not eligible and allow_unclean:
            # 高风险演示：可选择落后的存活副本，不保证包含已确认前缀。
            eligible = [
                replica for replica in self.replicas.values()
                if replica.name != self.master
                and replica.alive
                and replica.connected_to_controller
            ]
        if not eligible:
            raise NoSafeMaster("没有可用且含已确认数据的安全候选者")

        # 仅为实验可重复性：按名字选择；不代表 RocketMQ 的真实选主策略。
        promoted = sorted(eligible, key=lambda replica: replica.name)[0]
        self.master = promoted.name
        self.master_epoch += 1
        self.sync_state_set = {promoted.name}
        # 新任期通知可能无法传到失联的旧主；它仍自认为 MASTER。
        for replica in self.replicas.values():
            if replica.alive and replica.connected_to_controller:
                replica.apply_role(
                    "MASTER" if replica.name == self.master else "SLAVE",
                    self.master_epoch,
                )
        return self.master, self.master_epoch

    def guarded_append(self, target, observed_epoch, message):
        """教学化简：统一的权威写入门禁，不是 RocketMQ 的真实每次写入 RPC。"""
        replica = self.replicas[target]
        if not replica.alive:
            raise StaleMaster("节点不可用")
        if not (
            target == self.master
            and observed_epoch == self.master_epoch
            and replica.role == "MASTER"
            and replica.observed_epoch == self.master_epoch
        ):
            raise StaleMaster(f"{target}: stale master / epoch")
        replica.messages.append(deepcopy(message))
        return "WRITTEN_LOCALLY"  # 不是已满足复制条件的生产者 ACK

    def resync_role(self, target):
        replica = self.replicas[target]
        if not replica.connected_to_controller or not replica.alive:
            return False
        return replica.apply_role(
            "MASTER" if target == self.master else "SLAVE", self.master_epoch
        )


def build_cluster():
    return TinyController([
        Replica("A", messages=[deepcopy(EVENT)]),
        Replica("B", messages=[deepcopy(EVENT)]),
        Replica("C", messages=[]),  # 尚未追上，也不在初始 SyncStateSet
    ])


class FailoverTests(unittest.TestCase):
    def setUp(self):
        self.cluster = build_cluster()

    def isolate_old_master(self):
        self.cluster.replicas["A"].connected_to_controller = False

    def test_01_initial_copy_only_b_is_safe(self):
        self.assertEqual(self.cluster.sync_state_set, {"A", "B"})
        self.assertEqual(self.cluster.replicas["B"].messages, [EVENT])
        self.assertEqual(self.cluster.replicas["C"].messages, [])

    def test_02_elect_b_when_a_loses_controller_link(self):
        self.isolate_old_master()
        self.assertEqual(self.cluster.elect(), ("B", 2))
        self.assertEqual(self.cluster.replicas["B"].role, "MASTER")
        self.assertEqual(self.cluster.replicas["A"].role, "MASTER")

    def test_03_naive_local_role_check_can_split_writes(self):
        self.isolate_old_master()
        self.cluster.elect()
        self.cluster.replicas["A"].naive_append({"event_id": "old-branch"})
        self.cluster.replicas["B"].naive_append({"event_id": "new-branch"})
        self.assertNotEqual(self.cluster.replicas["A"].messages, self.cluster.replicas["B"].messages)

    def test_04_fencing_rejects_old_master(self):
        self.isolate_old_master()
        self.cluster.elect()
        with self.assertRaises(StaleMaster):
            self.cluster.guarded_append("A", 1, {"event_id": "old-write"})
        self.assertEqual(self.cluster.replicas["A"].messages, [EVENT])

    def test_05_new_master_can_write_but_not_claim_replica_ack(self):
        self.isolate_old_master()
        self.cluster.elect()
        result = self.cluster.guarded_append("B", 2, {"event_id": "new-write"})
        self.assertEqual(result, "WRITTEN_LOCALLY")
        self.assertEqual(len(self.cluster.replicas["B"].messages), 2)

    def test_06_no_clean_candidate_means_no_failover(self):
        self.isolate_old_master()
        self.cluster.replicas["B"].alive = False
        with self.assertRaises(NoSafeMaster):
            self.cluster.elect()
        self.assertEqual(self.cluster.master, "A")
        self.assertEqual(self.cluster.master_epoch, 1)

    def test_07_unclean_election_can_lose_confirmed_message(self):
        self.isolate_old_master()
        self.cluster.replicas["B"].alive = False
        self.assertEqual(self.cluster.elect(allow_unclean=True), ("C", 2))
        self.assertNotIn(EVENT, self.cluster.replicas["C"].messages)

    def test_08_old_epoch_role_update_cannot_overwrite_new_role(self):
        self.isolate_old_master()
        self.cluster.elect()
        b = self.cluster.replicas["B"]
        self.assertFalse(b.apply_role("SLAVE", 1))
        self.assertEqual((b.role, b.observed_epoch), ("MASTER", 2))

    def test_09_old_master_rejoins_and_becomes_slave(self):
        self.isolate_old_master()
        self.cluster.elect()
        a = self.cluster.replicas["A"]
        a.connected_to_controller = True
        self.assertTrue(self.cluster.resync_role("A"))
        self.assertEqual((a.role, a.observed_epoch), ("SLAVE", 2))
        with self.assertRaises(StaleMaster):
            a.naive_append({"event_id": "old-write"})

    def test_10_do_not_elect_if_old_master_still_healthy_and_connected(self):
        with self.assertRaises(NoSafeMaster):
            self.cluster.elect()

    def test_11_divergent_in_sync_candidate_is_not_safe(self):
        self.isolate_old_master()
        self.cluster.replicas["B"].messages = [{"event_id": "other"}]
        with self.assertRaises(NoSafeMaster):
            self.cluster.elect()

    def test_12_old_epoch_cannot_write_to_current_master(self):
        self.isolate_old_master()
        self.cluster.elect()
        with self.assertRaises(StaleMaster):
            self.cluster.guarded_append("B", 1, {"event_id": "wrong-epoch"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
```

运行：

```bash
python failover_lab.py -v
```

在本次 Python 环境中，完整执行结果的末尾是：

```text
Ran 12 tests in 0.001s

OK
```

这是**教学代码的实际测试结果**，不是 RocketMQ 集群压测或 Controller 故障演练。时间因机器而异，请以测试断言是否通过为准。

### 先看失败：两个节点都认为自己是 Master

`test_03_naive_local_role_check_can_split_writes` 特意保留了一个**错误实现**：

```python
# 教学化简：只检查本地角色
if replica.role == "MASTER":
    replica.messages.append(message)
```

A 与 Controller 失联后，B 当选新主；但是 A 还没收到角色变更，仍然保持 `role="MASTER"`。两个节点都能通过这段本地检查，于是各自追加不同消息。

这个实验说明“通知 B 升主”并不自动等于“旧 A 立即停止写”。网络分区下，**角色通知有传播延迟**本身就会影响设计。

### 再看修复：主身份与任期一起决定写入资格

`test_04_fencing_rejects_old_master` 使用 `guarded_append()`。在这个*理想化教学模型*中，写入必须同时满足：目标是当前权威 Master、请求任期与当前任期相等、本地角色也已经接受新任期。

```text
选主前：Master=A，epoch=1
A 与 Controller 失联
选主后：Master=B，epoch=2

A 仍自认 MASTER（epoch=1）
B 已认领 MASTER（epoch=2）

A 携带旧任期写入  → REJECT
B 携带新任期写入  → WRITTEN_LOCALLY
```

注意最后返回的是 `WRITTEN_LOCALLY`，**不是“消息已经得到足够副本确认”的生产者成功 ACK**。这一章只证明控制身份的变化，不重新证明第 14 章的持久化与复制条件。现实系统里，即便新主已经接管，仍然需要考虑当前同步副本数量不足时应否拒绝成功确认。

### 再试一个更危险的“优化”：没有安全候选，就选一个活着的

`test_06_no_clean_candidate_means_no_failover` 把 B 也设为不可用，只剩空日志的 C。

默认情况下，教学 Controller 会拒绝选主：宁可暂时没有新 Master，也不应该默默把包含已确认消息的历史抛弃。

`test_07_unclean_election_can_lose_confirmed_message` 则显式打开 `allow_unclean=True`，让 C 当选。测试随即确认：C 的日志中没有 `replay:msg-2002`。

这与 RocketMQ 的 `enableElectUncleanMaster` 风险方向一致，但**不是在真实 Broker 上执行了不安全选主**。这里所谓的“安全副本前缀”也只是教学判定；真实 RocketMQ 的候选者管理和日志修复机制要复杂得多。

## 回到源码：一轮主从切换到底经过了什么？

现在可以只用已经理解的机制把图补完整：

```text
                 Controller / DLedger
          记录 Master 身份与 masterEpoch
                       │
                选举：B，epoch=2
                       │
       ┌───────────────┴───────────────┐
       │                               │
       ▼                               ▼
 A：旧主，可能失联                 B：得到新角色
 需禁止继续当主写入                    │
                                       ▼
                                HA 服务切换角色
                                       │
                                       ▼
                               向 NameServer 注册
                                       │
                                       ▼
                              后续生产者路由更新
```

注意这里的三个不同层次：

- **Controller 决策层**决定哪个副本是主节点，并管理主节点任期。
- **Broker 执行层**根据收到的角色变化更新 HA 服务和本机读写职责。
- **消息数据层**仍然依赖上一章的复制、确认和日志一致性机制。

这不是说 Broker 切换之后就可以随便写任意数据。例如，当旧主重新加入时，日志可能与新主出现分叉；正确的日志对齐、截断与补齐需要由专门的 HA 协议处理，不能把两份分叉的 JSON 列表直接拼接。相关的主从切换设计可继续参考 [RocketMQ Controller Design](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/docs/en/controller/design.md)。

## 本章机制解决了什么，又没有解决什么？

**解决的目标**：把“某个副本保存了消息”提升到“有依据地选出一台可以接管职责的副本”，并通过任期和角色变更减少过期控制信息带来的风险。选主不保证永远成功；安全候选不存在时，拒绝切换可能是正确结果。

**还没有解决的限制**：

- 教学 Controller 是单进程内存对象，不具备 RocketMQ DLedger 控制集群的故障恢复能力。
- `guarded_append()` 使用权威状态即时校验，是用于说明 *fencing 约束* 的教学抽象，不是 RocketMQ 5.5.1 的真实业务写入 RPC 调用链。
- 此处 `SyncStateSet` 不代表“每条记录已经在每个副本上 `fsync()`”；具体复制确认、刷盘与同步集合维护仍受部署模式和参数控制。
- 选主成功不等于已消除旧客户端的路由缓存，也不等于网络隔离的旧 Broker 已瞬时停写。
- 不安全选举可能丢数据；即便安全选举，业务消息的交付与消费幂等问题依然存在。

## 下一章：新 Master 已经产生，生产者为什么还在向旧地址发送？

Controller 已经确认 B 是新 Master，并且 B 的本地角色也已完成切换。

可是订单服务里的生产者可能仍然缓存着 A 的地址，继续把 `replay:msg-2002` 的后续消息发给旧节点。

这次我们真正缺少的能力不是“再选一次 Master”，而是**让生产者及时知道新的路由位置，并在发送失败后重新发现可用 Broker**。

下一章我们就从这一次发往旧 Master 的请求失败开始，阅读 **RocketMQ NameServer、Broker 注册和客户端路由刷新** 的真实调用链。

---

**源码阅读索引（全部固定在 RocketMQ 5.5.1 / `efa1be59`）：**

1. [`ReplicasInfoManager.electMaster()`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/controller/src/main/java/org/apache/rocketmq/controller/impl/manager/ReplicasInfoManager.java#L175-L249)：候选者、SyncStateSet、选举结果与事件。
2. [`DLedgerController.electMaster()`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/controller/src/main/java/org/apache/rocketmq/controller/impl/DLedgerController.java#L172-L199) 与 [事件写入](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/controller/src/main/java/org/apache/rocketmq/controller/impl/DLedgerController.java#L400-L461)：选主控制事件如何提交。
3. [`ReplicasInfoManager.applyEvent()`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/controller/src/main/java/org/apache/rocketmq/controller/impl/manager/ReplicasInfoManager.java#L451-L526)：事件如何修改控制元数据。
4. [`ReplicasManager.changeBrokerRole()`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/broker/src/main/java/org/apache/rocketmq/broker/controller/ReplicasManager.java#L206-L290)：Broker 如何根据较新的任期切换角色。
5. [`ReplicasManager.schedulingSyncBrokerMetadata()`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/broker/src/main/java/org/apache/rocketmq/broker/controller/ReplicasManager.java#L594-L624)：如何定期发现角色变化。
