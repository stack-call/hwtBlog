---
slug: /message-queue/rocketmq-broker-replication
title: 消息已经写入 Broker，但 Broker 宕机了怎么办？
sidebar_position: 15
description: 从订单 2002 的恢复消息出发，复现单节点存储不可用、异步复制窗口与同步复制等待，再沿 RocketMQ 5.5.1 的 HA 数据传输与复制确认源码理解收益和代价。
tags: [消息队列, RocketMQ, 源码精读, 主从复制, 同步复制, 异步复制]
---

# 消息已经写入 Broker，但 Broker 宕机了怎么办？

上一章，订单 `2002` 的死信恢复事件 `replay:msg-2002` 终于通过了本地事务检查。我们让 RocketMQ 先保存半事务消息，待本地事务成功后再决定是否让消费者看到它。

当时我们认为，消息已经成功写入 Broker，生产者也得到了成功响应。可如果就在这一刻，**保存这条消息的 Broker 所在机器彻底无法访问**，消费者还能读到这条消息吗？

先不要急着说“需要集群”。我们应该分清两个不同的事故：

- **Broker 进程退出，但磁盘完好**：以前的追加日志已经能够在进程重新启动时恢复消息，这不意味着消息必然丢失。
- **Broker 所在机器或磁盘长期不可访问，甚至永久损坏**：即使消息确实写入了这台机器的磁盘，其他机器也无法凭空读出它。

这一章只研究第二种情况带来的新需求：**消息不能只存在于一台机器上。**

依旧追踪同一条消息：

```json
{"event_id":"replay:msg-2002","order_id":2002,"event_type":"RecoveryApproved"}
```

我们先用最小 Python 实验试两种复制方式，再去 RocketMQ 源码里看它怎样按物理偏移复制数据、怎样等待副本响应。

<!-- truncate -->

## 只有一份日志时，成功写入还不够

第 02 章里我们学过，消息写到磁盘后，重启进程可以从文件中找回它。假设 Broker A 已经把这条消息追加到了自己的 CommitLog：

```text
生产者
  │ 发送 replay:msg-2002
  ▼
Broker A
  │ 追加 CommitLog
  ▼
磁盘 A: [replay:msg-2002]
  │
  └── 返回成功
```

如果只是进程重启，数据文件还在，事情没有变复杂。

但如果整块磁盘坏了，或者 Broker A 所在机器一直无法恢复，我们仍无法从其他地方取得 `replay:msg-2002`。这里真正缺少的不是更快的硬盘，而是**另一份位于独立故障域的消息副本**。

先增加第二个存储节点 B。最简单的做法是：A 接收消息后，再把消息复制一份给 B。

```text
生产者 ──消息──▶ Broker A ──复制──▶ Broker B
                    │                  │
                 磁盘 A               磁盘 B
```

图仍然很小，但出现了一个必须回答的新问题：**什么时候才能告诉生产者“发送成功”？**

## 方案一：先返回成功，稍后再复制

最直接的做法是，Broker A 把消息写入本地日志，就向生产者返回成功，复制给 B 的工作随后进行。

这叫作**异步复制**。它的好处是，客户端不需要在正常发送时等待副本追上，响应路径更短。

假设执行过程如下：

```text
时刻 1  A 保存 replay:msg-2002
时刻 2  A 返回 ACK
时刻 3  A 准备复制给 B
时刻 4  A 的磁盘永久不可访问
时刻 5  查看 B：没有 replay:msg-2002
```

注意：时刻 4 不是“程序崩溃但磁盘仍可恢复”，而是我们明确模拟了主节点存储的**永久不可访问**。在这种故障条件下，生产者明明收到成功响应，B 却还没有那条消息。

于是我们发现，**副本存在**不等于**每个成功确认的消息都已经到达副本**。真正的问题是确认时机。

## 方案二：副本追上以后，才返回成功

如果不能接受上面的窗口，我们可以改变成功确认的时机：

```text
生产者发送 replay:msg-2002
           │
           ▼
       A 本地写入
           │
           ▼
       把数据交给 B
           │
           ▼
      等待 B 的确认
           │
           ▼
       返回成功 ACK
```

这可以用来建立最小的**同步复制**直觉。上一种方式的 ACK 只需要满足“主节点已按配置完成本地写入条件”；这一种需要额外满足“副本追上了要求的位置”。

但新的问题立刻出现：如果 B 无法连接，A 明明已经有了消息，生产者还应该得到成功响应吗？

对于本章要求“成功必须得到副本确认”的教学方案，答案是**不能**。我们只能返回复制未满足的状态，让调用方处理不确定性，而不是把主节点单机写入冒充双节点成功。

这也解释了同步复制的代价：它通常需要更长的确认路径，并可能在副本不可用时降低写入可用性。

## 可运行实验：只增加一台副本，不引入自动选主

下面是一份**教学化简**，不是 RocketMQ 源码。它有意只保留两个文件：

- `master/commitlog.jsonl`：模拟主节点日志。
- `replica/commitlog.jsonl`：模拟副本日志。

为了把注意力集中在复制确认时机上，实验写入每条记录后会显式调用 `fsync()`。因此，在这个**教学模型**里，同步模式的成功响应发生于两份文件都完成本地 `fsync` 之后。

**请不要据此推断 RocketMQ 的副本 ACK 必然代表副本已刷盘。** RocketMQ 的实际复制进度回报与具体刷盘策略需要分别阅读，后文会看到这个区别。

先把下面完整代码复制到 `replication_lab.py`。它只用 Python 标准库，末尾包含全部自动化测试：

```python title="replication_lab.py（完整教学实现与 9 项测试）"
"""教学化简：两节点追加日志与两种复制确认时机（仅 Python 标准库）。"""

import json
import os
import tempfile
import unittest
from pathlib import Path


EVENT = {
    "event_id": "replay:msg-2002",
    "order_id": 2002,
    "event_type": "RecoveryApproved",
}


def read_log(path):
    path = Path(path)
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as file:
        return [json.loads(line) for line in file if line.strip()]


def append_durably(path, event):
    """仅保证本地文件的写入及显式 fsync；不模拟物理断电。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        file.flush()
        os.fsync(file.fileno())


class TwoNodeLog:
    def __init__(self, directory):
        directory = Path(directory)
        self.master = directory / "master" / "commitlog.jsonl"
        self.replica = directory / "replica" / "commitlog.jsonl"

    def send(self, event, *, mode="async", replica_online=True):
        """返回教学状态，不模拟 RocketMQ 客户端返回码。"""
        if mode not in ("async", "sync"):
            raise ValueError("mode must be async or sync")
        append_durably(self.master, event)
        if mode == "async":
            # 主节点记录完就应答；副本可能尚未收到。
            return "ACK"
        if not replica_online:
            # 主节点可能有数据，但生产者不能把它当作复制成功。
            return "REPLICA_UNAVAILABLE"
        self.catch_up()
        return "ACK"

    def catch_up(self):
        """按位置补齐副本，要求副本日志是主节点日志的前缀。"""
        master_events = read_log(self.master)
        replica_events = read_log(self.replica)
        if master_events[:len(replica_events)] != replica_events:
            raise ValueError("replica is not a prefix of master")
        pending = master_events[len(replica_events):]
        for event in pending:
            append_durably(self.replica, event)
        return len(pending)

    def lose_master_disk(self):
        """模拟主节点的磁盘永久不可访问，不等同普通进程崩溃。"""
        self.master.unlink(missing_ok=True)


class ReplicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = TwoNodeLog(self.temp.name)

    def test_01_async_ack_can_precede_replication(self):
        self.assertEqual(self.store.send(EVENT, mode="async"), "ACK")
        self.assertEqual(read_log(self.store.master), [EVENT])
        self.assertEqual(read_log(self.store.replica), [])

    def test_02_async_ack_can_be_lost_with_master_storage(self):
        self.store.send(EVENT, mode="async")
        self.store.lose_master_disk()
        self.assertEqual(read_log(self.store.replica), [])

    def test_03_async_replica_can_catch_up(self):
        self.store.send(EVENT, mode="async")
        self.assertEqual(self.store.catch_up(), 1)
        self.store.lose_master_disk()
        self.assertEqual(read_log(self.store.replica), [EVENT])

    def test_04_sync_ack_waits_for_both_files_in_this_lab(self):
        self.assertEqual(self.store.send(EVENT, mode="sync"), "ACK")
        self.assertEqual(read_log(self.store.master), [EVENT])
        self.assertEqual(read_log(self.store.replica), [EVENT])
        self.store.lose_master_disk()
        self.assertEqual(read_log(self.store.replica), [EVENT])

    def test_05_sync_replica_unavailable_means_no_success_ack(self):
        self.assertEqual(
            self.store.send(EVENT, mode="sync", replica_online=False),
            "REPLICA_UNAVAILABLE",
        )
        self.assertEqual(read_log(self.store.master), [EVENT])
        self.assertEqual(read_log(self.store.replica), [])

    def test_06_pending_message_can_catch_up_after_recovery(self):
        self.store.send(EVENT, mode="sync", replica_online=False)
        self.assertEqual(self.store.catch_up(), 1)
        self.assertEqual(read_log(self.store.replica), [EVENT])

    def test_07_catch_up_twice_does_not_duplicate_records(self):
        self.store.send(EVENT, mode="async")
        self.assertEqual(self.store.catch_up(), 1)
        self.assertEqual(self.store.catch_up(), 0)
        self.assertEqual(read_log(self.store.replica), [EVENT])

    def test_08_replication_preserves_append_order(self):
        paid = {"event_id": "replay:msg-2002:checked", "order_id": 2002}
        self.store.send(EVENT, mode="async")
        self.store.send(paid, mode="async")
        self.assertEqual(self.store.catch_up(), 2)
        self.assertEqual(read_log(self.store.replica), [EVENT, paid])

    def test_09_divergent_replica_cannot_be_blindly_merged(self):
        self.store.send(EVENT, mode="async")
        append_durably(self.store.replica, {"event_id": "unexpected"})
        with self.assertRaises(ValueError):
            self.store.catch_up()


if __name__ == "__main__":
    unittest.main(verbosity=2)
```

运行：

```bash
python replication_lab.py
```

本次实际运行得到：

```text
Ran 9 tests in 0.028s

OK
```

测试名称各自说明了发生什么。尤其要看三个有因果关系的实验：

1. `test_02_async_ack_can_be_lost_with_master_storage`：异步模式已返回 ACK，却还没复制；模拟主节点磁盘永久不可访问后，仅剩副本中没有这条消息。
2. `test_04_sync_ack_waits_for_both_files_in_this_lab`：教学同步模式在两个文件完成写入后才 ACK；即使删除主节点文件，副本仍能读到 `replay:msg-2002`。
3. `test_05_sync_replica_unavailable_means_no_success_ack`：副本不可用时，主节点虽然有记录，但返回 `REPLICA_UNAVAILABLE`，不能宣称复制成功。

这些测试没有启动真正的 RocketMQ Broker，也没有模拟物理断电。`lose_master_disk()` 直接删除教学文件来代表磁盘不可恢复；它不代表所有 Broker 进程故障都会丢数据。也没有测试吞吐或响应时间，更没有实现故障选主。

## 去 RocketMQ 源码验证：副本到底复制什么？

现在我们才进入真实实现。以下源码统一固定为：

- 项目：`apache/rocketmq`
- 版本：**RocketMQ 5.5.1**
- Commit：[`efa1be59d6fc747ad220f036c86030f05f0419f9`](https://github.com/apache/rocketmq/tree/efa1be59d6fc747ad220f036c86030f05f0419f9)
- 本章主路径：经典主从 HA 复制（`DefaultHAService`），不把 Controller 自动切换或 DLedger 实现混为同一条路径。

前面学过 CommitLog 负责保存消息正文。现在，当一条新的消息要复制给副本时，**主节点并不需要把业务对象重新解释成“下单”或者“恢复事件”**，而是沿已经存在的 CommitLog 物理位置发送数据。

### 第一步：主节点找到要复制的日志片段

在 [`DefaultHAConnection.java`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/store/src/main/java/org/apache/rocketmq/store/ha/DefaultHAConnection.java) 的 `WriteSocketService.run()` 中，主节点根据下一次传输的位置获取 CommitLog 数据。

**真实源码节选（仅保留关键行）：**

```java
SelectMappedBufferResult selectResult =
    DefaultHAConnection.this.haService.getDefaultMessageStore().getCommitLogData(this.nextTransferFromWhere);
```

这里的 `nextTransferFromWhere` 不是订单 ID，也不是第 07 章里的逻辑队列编号，而是接下来要传输的**物理日志偏移**。

调用者是 HA 主节点的写 Socket 服务，输入是下一段物理位置；它读出字节并通过网络发送。下游接收者是副本节点的 HA 客户端。

### 第二步：副本按物理偏移追加，而不是执行消费业务

在 [`DefaultHAClient.java`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/store/src/main/java/org/apache/rocketmq/store/ha/DefaultHAClient.java) 的 `dispatchReadRequest()` 中，副本读取传输头中的物理偏移，检查与本地日志末尾的位置关系，然后调用：

**真实源码节选：**

```java
this.defaultMessageStore.appendToCommitLog(
    masterPhyOffset, bodyData, dataStart, bodySize);
```

这一步的输入是主节点提供的物理偏移和字节数据，副作用是向副本本地 CommitLog 追加内容。它不代表通知已发送、订单已恢复或消费者已经 ACK。

副本随后通过 `reportSlaveMaxOffsetPlus()` 回报自己的日志进度；在主节点的 `DefaultHAConnection.ReadSocketService.processReadEvent()` 中，这个回报会被记录为 `slaveAckOffset` 并通知 HA 服务。

这里需要非常谨慎：**源码里的物理偏移回报不是“业务处理完成”的 ACK，也不能不经检查就等同于副本磁盘完成同步刷盘。** 消费 ACK、复制 ACK 和磁盘刷盘分别解决不同的问题。

### 第三步：主节点如何判断副本追上了？

在 [`DefaultHAService.java`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/store/src/main/java/org/apache/rocketmq/store/ha/DefaultHAService.java) 中，`notifyTransferSome()` 在从节点反馈位置推进时唤醒等待复制的服务。

真正的等待逻辑在 [`GroupTransferService.java`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/store/src/main/java/org/apache/rocketmq/store/ha/GroupTransferService.java) 的 `doWaitTransfer()`。

在经典的单副本确认分支，源码使用如下判断：

**真实源码节选：**

```java
transferOK = haService.getPush2SlaveMaxOffset().get() >= req.getNextOffset();
```

其中 `req.getNextOffset()` 表示这一轮写入希望副本至少追上的位置，`getPush2SlaveMaxOffset()` 是主节点记录的已回报复制位置。只有后者达到前者，等待才可以认为对应复制条件满足。

如果直到请求期限都没达到条件，`GroupTransferService` 会以 `FLUSH_SLAVE_TIMEOUT` 等状态结束请求；它不会无限等待。

这个类还有按确认副本数量以及 Controller 同步状态集判断的其他分支。**不能把上面的一行判断概括成 RocketMQ 5.5.1 所有复制模式的完整协议。**

至此，我们可以把前面三段代码连起来：

```text
主节点 CommitLog
       │ 读取待复制物理区间
       ▼
DefaultHAConnection.WriteSocketService
       │ 通过网络发送数据
       ▼
副本 DefaultHAClient.dispatchReadRequest
       │ 按偏移追加到 CommitLog
       ▼
副本报告自身日志偏移
       │
       ▼
主节点 ReadSocketService 更新 slaveAckOffset
       │
       ▼
DefaultHAService 通知复制进度变化
       │
       ▼
GroupTransferService 检查目标偏移是否追上
       │
       ▼
请求满足确认条件 / 超时未满足
```

注意，这张链路图直到读者理解每个箭头的目的才出现：写出字节、复制字节、回报位置、决定何时完成响应。没有引入自动选主或路由发现，因为本章还不需要它们。

## 复制成功究竟保证了什么？

回看同一条 `replay:msg-2002`，我们现在能区分以下四个问题：

| 问题 | 本章的回答 |
| --- | --- |
| 主节点进程重启，磁盘还在 | 本地持久化与恢复可能足够，不需要据此推断消息丢失 |
| 主节点磁盘永久无法访问，副本没追上 | 已返回的异步确认消息存在无法从副本恢复的窗口 |
| 主节点等待副本追上再 ACK | 可以缩小上述窗口，但会引入等待及副本不可用时的写入限制 |
| 副本报告了复制偏移 | 代表复制进度信息，不等于消费成功，也不自动证明物理落盘 |

还要注意，实验把同步模式设置为“主副本各自完成 `fsync` 才返回成功”，这是为了建立清晰的对照条件。实际 RocketMQ 中，**复制确认策略**和**刷盘策略**是不同维度；如果生产环境需要特定的故障保证，必须同时核对所选 Broker 模式、刷盘配置、确认副本数及故障切换策略。

本章也没有得到以下结论：

- 有两份副本就一定没有数据损失。相关故障可能同时影响多个副本。
- 同步复制就等于精确一次消费。业务副作用仍需要幂等设计。
- 副本上的日志已经追上，就一定可以立即当主节点接收新写入。谁有资格成为新主节点，是另一类控制问题。
- 两台机器的同步复制就自动实现了共识协议。我们没有设计选举、任期和旧主节点隔离机制。

## 留下一个新的问题

我们终于让主节点写入消息时，把副本追上的位置纳入确认条件。现在假设 `replay:msg-2002` 已经在主节点 A 和副本 B 上都有一份。

突然，A 不再响应请求。

B 虽然保存着数据，但**生产者如何知道下一条消息应该发给谁？** 如果 A 只是网络隔离而并未真正关机，A 和 B 同时认为自己可以写入，又会怎样？

这才是下一章真正要解决的问题：

**《副本里明明有消息，为什么 Broker 故障后仍然可能无法继续写入？》**

我们将以这次故障为起点，理解主节点身份、切换与旧主隔离的必要性，然后再进入 RocketMQ 5.5.1 的 Controller 模式相关源码。

---

**固定版本源码导航：**

- [`DefaultHAConnection.WriteSocketService`：主节点传输 CommitLog](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/store/src/main/java/org/apache/rocketmq/store/ha/DefaultHAConnection.java)
- [`DefaultHAClient.dispatchReadRequest()`：副本接收并追加](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/store/src/main/java/org/apache/rocketmq/store/ha/DefaultHAClient.java)
- [`DefaultHAService.notifyTransferSome()`：复制进度通知](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/store/src/main/java/org/apache/rocketmq/store/ha/DefaultHAService.java)
- [`GroupTransferService.doWaitTransfer()`：复制确认等待](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/store/src/main/java/org/apache/rocketmq/store/ha/GroupTransferService.java)
- [`DefaultMessageStore`：根据存储和 Controller 配置选择 HA 服务](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/store/src/main/java/org/apache/rocketmq/store/DefaultMessageStore.java)

**标注说明：** Java 片段是固定 Commit 的真实源码关键行；`replication_lab.py` 为教学化简；图为教学执行轨迹。Python 代码已经运行，未启动真正的 RocketMQ Broker，未实际测试磁盘断电、网络分区或 Controller 自动切换。
