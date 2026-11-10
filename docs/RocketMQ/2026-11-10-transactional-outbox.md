---
slug: /message-queue/transactional-outbox
title: 恢复决定已提交，消息却不知道有没有发出去：从双写问题到 Outbox
sidebar_position: 13
description: 继续追踪订单 2002 的死信恢复：在业务恢复记录与消息发布之间复现崩溃窗口，用一个数据库事务引出事务性发件箱，并与 RocketMQ 5.5.1 的发送和事务消息源码对照。
tags: [消息队列, RocketMQ, 源码精读, 事务性发件箱, 双写, 消息可靠性]
---

# 恢复决定已提交，消息却不知道有没有发出去：从双写问题到 Outbox

上一章，我们终于不再把「死信修好了」理解成「直接重发」。对于订单 `1001`，较新的支付事件已经生效，因此它的创建事件不应该再覆盖现有状态；对于订单 `2002`，没有新的业务状态阻止恢复，我们决定重放 `msg-2002`。

但上一章的 `recover()` 在做出决定后，仍有两个分开的动作：

```text
写业务数据库：记录“准备恢复 msg-2002”
                ↓
调用消息队列：发送 replay:msg-2002
                ↓
写业务数据库：更新恢复状态为 QUEUED
```

只要应用可能在任意两步之间退出，这条链路就存在一个麻烦：**数据库里认为应该发送的消息，不一定已经发出；Broker 已经保存的消息，也不一定有相应的发送成功记录。**

这一章只解决一个问题：**怎样保证「决定发送一条消息」和「留下待发送的证据」不会被一次进程崩溃拆开？**

我们先观察最简单的双写为什么不够，再引入一个只增加一张表的方案。直到这时，我们才给它起名字：事务性发件箱（Transactional Outbox）。最后对照 RocketMQ 5.5.1 的真实发送链路与事务消息机制，确认它解决了什么、没有解决什么。

<!-- truncate -->

## 先不要增加新组件：把上章的故障看得更清楚

先看上一章那种完全合理、但不够可靠的写法：

```python title="概念伪代码：仍然有双写窗口"
save_recovery_job("msg-2002", status="PREPARING")  # 数据库提交
mq.publish("replay:msg-2002")                        # 网络发送
save_recovery_job("msg-2002", status="QUEUED")       # 又一次数据库提交
```

在正常情况下，三步都执行完成，当然没有问题。我们需要的不是证明它「总是错误」，而是找到它**在哪一个具体时刻失效**。

**第一种崩溃：记录了决定，还没发送。**

```text
恢复记录 PREPARING 已提交
             ↓
       [进程退出]
             ↓
Broker 没有收到 replay:msg-2002
```

如果没有可靠的待发送清单，恢复程序重新启动后无法仅凭这次函数调用找回它原本要发布的内容。上一章让 `PREPARING` 停住、等待人工对账，是谨慎的处理，但它还无法自动可靠地恢复发送。

**第二种崩溃：发送成功，确认记录还没更新。**

```text
Broker 保存 replay:msg-2002
             ↓
    [响应丢失 / 进程退出]
             ↓
本地仍是 PREPARING
```

此时「重新发送」可能产生第二次投递；「直接跳过」又可能漏掉第一种情况。两种故障只看本地 `PREPARING`，根本区分不开。

于是我们的需求必须说得更精确：**先不要试图用一个普通数据库事务包住网络发送；先确保每个已提交的恢复决定，都有一份可以在重启后重新读取的发送任务。**

## 只加一张表：把“要发送什么”也纳入本地事务

我们已有 `recovery_jobs`，它记录为什么允许恢复。现在增加一张 `outbox`，它只存放**已经决定要发送，但不一定已发送**的事件。

```sql title="教学表结构：与 recovery_jobs 位于同一个业务数据库"
CREATE TABLE outbox (
    event_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL UNIQUE,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING'
);
```

关键不是表名，而是**插入恢复决定和插入待发送消息，必须由同一个数据库事务提交**。

```python title="教学化简：recover() 的两条关键 INSERT"
conn.execute("BEGIN IMMEDIATE")
conn.execute(
    "INSERT INTO recovery_jobs(source_id, status) VALUES (?, ?)",
    (source_id, "READY"),
)
conn.execute(
    "INSERT INTO outbox(event_id, source_id, payload) VALUES (?, ?, ?)",
    ("replay:" + source_id, source_id, payload),
)
# 离开事务块时一起 COMMIT；任何异常一起 ROLLBACK。
```

此处唯一的新机制，是**把业务决定和待发送的消息当作同一笔本地持久化操作**。

现在如果进程在提交前崩溃，两条记录都不存在；如果它们已经成功提交，两条记录都存在。不存在「恢复决定已提交，但待发送任务没有保存」的中间状态。

但我们没有假装消息已经到达 RocketMQ。此时 `outbox.status = PENDING` 只意味着：「**系统应该发送这条消息，而且发送任务已经可恢复**」。

这才是 Outbox 真正消除的那个双写窗口。

## 谁来把 PENDING 发送出去？

最简单的答案仍然是一个普通后台进程，不需要立即引入复杂调度器。它读取最早的一条 `PENDING`，调用消息发送接口，收到成功响应后再把状态改为 `SENT`。

```text
应用数据库（一次本地事务）
  recovery_jobs: READY
  outbox:        PENDING
           |
           | 后台读取待发送事件
           v
       调用 Broker
           |
           | 收到成功响应
           v
应用数据库
  outbox:        SENT
  recovery_jobs: PUBLISHED
```

和上章相比，变化是：**即使后台进程发送前就退出，消息内容也仍在 `outbox`，重新启动就能继续处理。**

然而我们不能在这里宣布问题完全解决。

## Broker 保存了消息，但发送响应丢失了

假设后台进程从 `outbox` 取出了 `replay:msg-2002`，发送到 Broker。Broker 已经保存它，回复却在网络中丢失。

```text
outbox: PENDING
       |
       v
Broker 保存第一份消息
       |
       x  成功响应丢失
       |
后台不能确认是否发送成功
       |
重启后再次读取 PENDING
       |
       v
Broker 可能保存第二份消息
```

我们原本想避免消息丢失，却发现**可靠重试可能带来重复投递**。这是预期中的取舍：仅靠应用数据库的 Outbox，不能让两个独立系统共享一个原子提交点。

前面第 04 章已经研究过业务幂等，第 11 章也强调了恢复时要保留原始事件身份。现在这两项知识可以直接复用。

一条被重新投递的消息，在教学模型里保留两个不同的 ID：

```json title="示例消息：投递标识与源事件标识"
{
  "event_id": "replay:msg-2002",
  "source_id": "msg-2002",
  "order_id": 2002,
  "event_type": "OrderCreated",
  "message_group": "order:2002"
}
```

Broker 可能接受这份消息两次；消费者仍然以 `source_id=msg-2002` 作为本次**业务效果**的去重键。换句话说，我们不能承诺「只投递一次」，但在一个支持唯一约束的业务数据库中，可以防止重复插入**同一源事件对应的通知意图**。

注意，这里依然不代表外部短信绝对只发送一次。第三方短信、支付、邮件这样的外部副作用，还需要对方支持幂等键、查询回执或补偿机制。

## 用三个独立的 SQLite 文件复现这件事

接下来，实验只使用 Python 标准库，不启动 RocketMQ，也不依赖 API Key。为了使边界可观察，我们故意使用三个独立数据库文件：

- `app.sqlite3`：保存恢复审查结果与 Outbox，两个表可以同事务提交。
- `broker.sqlite3`：模拟消息服务已经接收的投递；它**不能**与应用数据库共同提交。
- `consumer.sqlite3`：模拟下游已产生的通知意图，按源事件 ID 去重。

这里的 `dead_letters` 是从上一章已存在的死信引入实验数据库的**教学快照**，不是声称上一章的 Broker 死信 Topic 和业务数据库天然处于同一事务。也不能把这里的 `deliveries` 表说成 RocketMQ 的 CommitLog。

实验先看订单 `1001`：已经是 `OrderPaid`，恢复判断应当 `SKIPPED`，Outbox 不生成记录。随后看订单 `2002`：恢复判断为 `READY`，Outbox 有一条待发送事件，但 Broker 还没有收到。

我们再故意让 Broker 保存消息之后抛出 `ConnectionError`，模拟发送成功但 ACK 丢失。此时应用端仍保留 `PENDING`，再次发送会得到两条 Broker 投递记录。最后让消费者处理它们，检查只产生一条通知意图。

下面给出**完整的单文件 Python 教学程序和内置自动化测试**。代码直接写在本篇 Markdown 中；复制代码块保存为 `outbox_lab.py` 即可运行，不需要额外下载 Python 文件。

```python title="outbox_lab.py（完整可运行教学代码，含 unittest）"
"""第 12 章教学实验：应用事务性发件箱与假 Broker（非 RocketMQ 源码）。

Python 3.9+，仅标准库。三个独立的 SQLite 文件模拟三个事务边界：
app.sqlite3（恢复决策 + outbox）、broker.sqlite3、consumer.sqlite3。
"""
import json
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def connect(path):
    conn = sqlite3.connect(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


class OutboxLab:
    def __init__(self, root):
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        self.app_path = root / "app.sqlite3"
        self.broker_path = root / "broker.sqlite3"
        self.consumer_path = root / "consumer.sqlite3"
        with connect(self.app_path) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS dead_letters (
                    source_id TEXT PRIMARY KEY,
                    order_id INTEGER NOT NULL,
                    event_type TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS order_state (
                    order_id INTEGER PRIMARY KEY,
                    status TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS recovery_jobs (
                    source_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS outbox (
                    event_id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL UNIQUE,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'PENDING'
                );
            """)
        with connect(self.broker_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS deliveries (
                    delivery_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    payload TEXT NOT NULL
                )
            """)
        with connect(self.consumer_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS notification_effects (
                    source_id TEXT PRIMARY KEY,
                    order_id INTEGER NOT NULL,
                    event_type TEXT NOT NULL
                )
            """)

    def add_dead(self, source_id, order_id, event_type="OrderCreated"):
        with connect(self.app_path) as conn:
            conn.execute(
                "INSERT INTO dead_letters VALUES (?, ?, ?)",
                (source_id, order_id, event_type),
            )

    def mark_paid(self, order_id):
        with connect(self.app_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO order_state VALUES (?, 'OrderPaid')",
                (order_id,),
            )

    def recover(self, source_id, fail_before_commit=False):
        """恢复决策与待发送记录在一个本地数据库事务内提交。"""
        with connect(self.app_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT status FROM recovery_jobs WHERE source_id=?", (source_id,)
            ).fetchone()
            if existing:
                return existing[0]
            dead = conn.execute(
                "SELECT order_id, event_type FROM dead_letters WHERE source_id=?",
                (source_id,),
            ).fetchone()
            if dead is None:
                raise ValueError("源事件不是已登记的死信")
            order_id, event_type = dead
            state = conn.execute(
                "SELECT status FROM order_state WHERE order_id=?", (order_id,)
            ).fetchone()
            obsolete = event_type == "OrderCreated" and state == ("OrderPaid",)
            status = "SKIPPED" if obsolete else "READY"
            conn.execute(
                "INSERT INTO recovery_jobs(source_id, status) VALUES (?, ?)",
                (source_id, status),
            )
            if not obsolete:
                event_id = "replay:" + source_id
                payload = json.dumps({
                    "event_id": event_id,
                    "source_id": source_id,
                    "order_id": order_id,
                    "event_type": event_type,
                    "message_group": f"order:{order_id}",
                }, ensure_ascii=False)
                conn.execute(
                    "INSERT INTO outbox(event_id, source_id, payload) VALUES (?, ?, ?)",
                    (event_id, source_id, payload),
                )
            if fail_before_commit:
                raise RuntimeError("模拟：本地事务提交前崩溃")
            return status

    def dispatch_once(self, lose_broker_ack=False):
        """仅模拟一个发送工作进程；允许发送成功后丢失响应。"""
        with connect(self.app_path) as conn:
            row = conn.execute(
                "SELECT event_id, source_id, payload FROM outbox "
                "WHERE status='PENDING' ORDER BY rowid LIMIT 1"
            ).fetchone()
        if not row:
            return False
        event_id, source_id, payload = row
        # 与 app.sqlite3 不同的事务：发送成功不等于本地标记成功。
        with connect(self.broker_path) as conn:
            conn.execute(
                "INSERT INTO deliveries(event_id, source_id, payload) VALUES (?, ?, ?)",
                (event_id, source_id, payload),
            )
        if lose_broker_ack:
            raise ConnectionError("模拟：Broker 已保存消息，但 ACK 在网络中丢失")
        with connect(self.app_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE outbox SET status='SENT' WHERE event_id=? AND status='PENDING'",
                (event_id,),
            )
            conn.execute(
                "UPDATE recovery_jobs SET status='PUBLISHED' WHERE source_id=?",
                (source_id,),
            )
        return True

    def consume_all(self):
        """按源事件 ID 去重；只模拟写入通知意图，不执行实际短信发送。"""
        with connect(self.broker_path) as conn:
            deliveries = conn.execute(
                "SELECT payload FROM deliveries ORDER BY delivery_id"
            ).fetchall()
        new_effects = 0
        for (payload,) in deliveries:
            message = json.loads(payload)
            with connect(self.consumer_path) as conn:
                cursor = conn.execute(
                    "INSERT INTO notification_effects VALUES (?, ?, ?) "
                    "ON CONFLICT(source_id) DO NOTHING",
                    (message["source_id"], message["order_id"], message["event_type"]),
                )
                new_effects += cursor.rowcount
        return new_effects

    def count(self, table):
        paths = {
            "recovery_jobs": self.app_path,
            "outbox": self.app_path,
            "deliveries": self.broker_path,
            "notification_effects": self.consumer_path,
        }
        if table not in paths:
            raise ValueError(table)
        with connect(paths[table]) as conn:
            return conn.execute("SELECT count(*) FROM " + table).fetchone()[0]

    def outbox_status(self, source_id):
        with connect(self.app_path) as conn:
            row = conn.execute(
                "SELECT status FROM outbox WHERE source_id=?", (source_id,)
            ).fetchone()
            return row[0] if row else None


def demo():
    with tempfile.TemporaryDirectory(prefix="mq-ch12-") as root:
        lab = OutboxLab(root)
        lab.add_dead("msg-1001", 1001)
        lab.mark_paid(1001)
        print("订单 1001：", lab.recover("msg-1001"), "（已有支付状态，跳过）")
        lab.add_dead("msg-2002", 2002)
        print("订单 2002：", lab.recover("msg-2002"), "（outbox 已入库）")
        print("发布之前：Broker 消息数 =", lab.count("deliveries"))
        try:
            lab.dispatch_once(lose_broker_ack=True)
        except ConnectionError as exc:
            print("第一次投递：", str(exc))
        print("第一次后：Broker 消息数 =", lab.count("deliveries"),
              "；outbox 状态 =", lab.outbox_status("msg-2002"))
        lab.dispatch_once()
        print("重试后：Broker 消息数 =", lab.count("deliveries"),
              "；outbox 状态 =", lab.outbox_status("msg-2002"))
        print("消费两份投递：新增通知意图 =", lab.consume_all(),
              "；通知意图总数 =", lab.count("notification_effects"))


class OutboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mq-ch12-test-")
        self.lab = OutboxLab(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_obsolete_dead_letter_is_not_replayed(self):
        self.lab.add_dead("msg-1001", 1001)
        self.lab.mark_paid(1001)
        self.assertEqual(self.lab.recover("msg-1001"), "SKIPPED")
        self.assertEqual(self.lab.count("outbox"), 0)

    def test_job_and_outbox_roll_back_together(self):
        self.lab.add_dead("msg-2002", 2002)
        with self.assertRaises(RuntimeError):
            self.lab.recover("msg-2002", fail_before_commit=True)
        self.assertEqual(self.lab.count("recovery_jobs"), 0)
        self.assertEqual(self.lab.count("outbox"), 0)

    def test_committed_outbox_survives_restart_before_send(self):
        self.lab.add_dead("msg-2002", 2002)
        self.assertEqual(self.lab.recover("msg-2002"), "READY")
        self.assertEqual(self.lab.count("deliveries"), 0)
        restarted = OutboxLab(self.temp.name)
        self.assertEqual(restarted.outbox_status("msg-2002"), "PENDING")
        self.assertTrue(restarted.dispatch_once())
        self.assertEqual(restarted.count("deliveries"), 1)

    def test_lost_ack_causes_duplicate_delivery_but_one_effect(self):
        self.lab.add_dead("msg-2002", 2002)
        self.lab.recover("msg-2002")
        with self.assertRaises(ConnectionError):
            self.lab.dispatch_once(lose_broker_ack=True)
        self.assertEqual(self.lab.outbox_status("msg-2002"), "PENDING")
        self.assertEqual(self.lab.count("deliveries"), 1)
        self.lab.dispatch_once()
        self.assertEqual(self.lab.count("deliveries"), 2)
        self.assertEqual(self.lab.consume_all(), 1)
        self.assertEqual(self.lab.consume_all(), 0)
        self.assertEqual(self.lab.count("notification_effects"), 1)

    def test_recovery_request_is_idempotent(self):
        self.lab.add_dead("msg-2002", 2002)
        self.assertEqual(self.lab.recover("msg-2002"), "READY")
        self.assertEqual(self.lab.recover("msg-2002"), "READY")
        self.assertEqual(self.lab.count("recovery_jobs"), 1)
        self.assertEqual(self.lab.count("outbox"), 1)

    def test_source_identity_is_preserved(self):
        self.lab.add_dead("msg-2002", 2002)
        self.lab.recover("msg-2002")
        self.lab.dispatch_once()
        with connect(self.lab.broker_path) as conn:
            data = json.loads(conn.execute(
                "SELECT payload FROM deliveries LIMIT 1"
            ).fetchone()[0])
        self.assertEqual(data["event_id"], "replay:msg-2002")
        self.assertEqual(data["source_id"], "msg-2002")
        self.assertEqual(data["message_group"], "order:2002")


if __name__ == "__main__":
    import sys
    if len(sys.argv) == 2 and sys.argv[1] == "test":
        unittest.main(argv=[sys.argv[0]], verbosity=2)
    elif len(sys.argv) == 1 or sys.argv[1] == "demo":
        demo()
    else:
        raise SystemExit("用法：python outbox_lab.py [demo|test]")
```

执行方式：

```bash
# 从上面的完整 Python 代码块复制内容，保存为 outbox_lab.py
python outbox_lab.py demo
python outbox_lab.py test
```

在本章实际执行的环境中，演示输出如下：

```text
订单 1001： SKIPPED （已有支付状态，跳过）
订单 2002： READY （outbox 已入库）
发布之前：Broker 消息数 = 0
第一次投递： 模拟：Broker 已保存消息，但 ACK 在网络中丢失
第一次后：Broker 消息数 = 1 ；outbox 状态 = PENDING
重试后：Broker 消息数 = 2 ；outbox 状态 = SENT
消费两份投递：新增通知意图 = 1 ；通知意图总数 = 1
```

自动化测试共 **6 项，全部通过**。测试覆盖了：过时死信不发布、恢复记录和 Outbox 一起回滚、应用进程重新启动后继续发送、ACK 丢失后的重复交付与消费幂等、恢复请求幂等，以及源事件身份保留。

这里验证的是 SQLite 本地事务、模拟 Broker 的独立事务以及本地去重的**教学行为**。没有启动 RocketMQ Broker，没有实际发生断电，也没有验证多个发送工作进程并发工作或外部短信的精确一次执行。

## 回到 RocketMQ：Outbox 是它的哪个模块？

到这里可能会产生一个问题：我们讲的是 RocketMQ 源码精读，为什么要写一张业务数据库表？

因为 Outbox 的核心不是 RocketMQ 中一个叫作 `Outbox` 的 Broker 类。**它是应用侧协调业务数据库与消息发布的设计模式**。这张表属于恢复服务或订单服务，而不属于 RocketMQ 的消息存储引擎。

真正进入 RocketMQ 时，Outbox 发送工作进程只承担 **Producer** 的角色。它读取待发送记录，然后调用客户端发送接口。我们仍然需要知道：客户端负责发送，Broker 负责接收和存储；客户端收到的响应决定这次发送调用的可见结果，但不能使应用数据库与 Broker 形成同一笔本地事务。

先看固定版本的普通消息发送路径。

- 仓库：[apache/rocketmq](https://github.com/apache/rocketmq)
- 固定版本：**RocketMQ 5.5.1**
- Commit：[`efa1be59d6fc747ad220f036c86030f05f0419f9`](https://github.com/apache/rocketmq/commit/efa1be59d6fc747ad220f036c86030f05f0419f9)
- 源码：[`DefaultMQProducerImpl.java`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/client/src/main/java/org/apache/rocketmq/client/impl/producer/DefaultMQProducerImpl.java)
- 函数：`sendDefaultImpl()`

`sendDefaultImpl()` 是经典 Java 客户端普通发送的一条关键执行路径。它检查消息与 Topic 路由，选出 `MessageQueue`，通过 `sendKernelImpl()` 发起实际的发送，并根据通信模式、返回状态或异常决定返回或重试。

这里最关键的并不是背诵它的重试次数，而是明确**调用边界**：它可以再试一次 Broker 发送，但没有权限替我们的 `app.sqlite3` 提交或回滚 `recovery_jobs`、`outbox`。

比如它的同步发送循环确实会计算发送尝试次数（**真实源码节选**）：

```java title="DefaultMQProducerImpl.java / sendDefaultImpl()（真实源码节选）"
int timesTotal = communicationMode == CommunicationMode.SYNC ? 1 + this.defaultMQProducer.getRetryTimesWhenSendFailed() : 1;
```

这个机制能解释：重试是消息发送端用来处理发送不确定性的一种策略，但不是跨系统原子事务。如果首次发送已经被 Broker 保存，只是客户端没有收到成功结果，后续再次发送就必须考虑重复；我们的实验正是在独立系统之间复现这个窗口。

### RocketMQ 自己不是也有“事务消息”吗？

有。但现在才是引出它的合适时机：因为我们已经发现，应用侧 Outbox 解决的是「业务决定与待发送任务共同提交」，而 Broker 是否收到消息仍是另一段需要重试的链路。

RocketMQ 提供另一条实现最终一致性的路线：**先在 Broker 端保留一条暂不可消费的半事务消息，再执行本地事务，最后向 Broker 确认提交或回滚；当确认结果不明确时，由 Broker 发起事务状态回查。** 这与“先写本地 Outbox，再由后台投递”并非同一个算法。

在固定版本中，经典 Java 事务消息的入口之一是：

[`TransactionMQProducer.sendMessageInTransaction()`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/client/src/main/java/org/apache/rocketmq/client/producer/TransactionMQProducer.java)

它会检查事务监听器，再将调用交给生产者实现（**真实源码节选**）：

```java title="TransactionMQProducer.java / sendMessageInTransaction()（真实源码节选）"
return this.defaultMQProducerImpl.sendMessageInTransaction(msg, null, arg);
```

这个入口告诉我们，事务消息并不只是普通发送 API 换了个名称，而是走专门的事务发送路径。生产者执行本地事务并报告结果；如果结果不确定，事务检查逻辑需要能够根据**持久化的本地业务事实**回答 Broker，而不能仅凭内存中的布尔值猜测。

接着看 Broker 端真正处理提交或回滚的入口：

[`EndTransactionProcessor.processRequest()`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/broker/src/main/java/org/apache/rocketmq/broker/processor/EndTransactionProcessor.java)

当请求表示提交时，它会查询之前的准备消息并执行提交处理；成功路径中还要将最终可消费的消息送入正常消息存储流程。以下两句均取自固定 Commit，属于**真实源码节选，省略了中间的检查与消息转换**：

```java title="EndTransactionProcessor.java / processRequest()（真实源码节选）"
result = this.brokerController.getTransactionalMessageService().commitMessage(requestHeader);
// ……省略准备消息校验、属性处理等代码
RemotingCommand sendResult = sendFinalMessage(msgInner);
```

这段代码的调用者是 Broker 接收事务结束请求的处理链；输入中包含事务结束命令及关联准备消息的信息；输出是本次事务结束处理的响应。它改变的是 Broker 中消息是否进入正常可消费流程，不负责解释订单 `2002` 是否应该恢复，也不替应用完成通知服务的业务幂等。

对照 [RocketMQ 官方事务消息文档](https://rocketmq.apache.org/zh/docs/featureBehavior/04transactionmessage/) 可以得到一个精确的区别：

| 对比点 | 本章 Outbox 教学方案 | RocketMQ 事务消息 |
| --- | --- | --- |
| 首先持久化什么 | 应用数据库中的恢复决定与待发送事件 | Broker 内部的半事务消息 |
| 核心原子边界 | 同一个应用数据库事务 | Broker 的事务消息协议 + 本地业务事务状态 |
| 谁负责处理后续不确定性 | Outbox 派发器重试、对账 | Producer 事务结束/状态回查与 Broker 协调 |
| 消费者是否仍需幂等 | 需要 | 仍需要考虑重试和重复业务效果 |
| 本章是否实际运行 | Python/SQLite 实验已运行 | 只核对文档与源码，未启动真实 Broker |

不能据此断言两种方式谁全面优于谁。它们在应用存储、故障恢复、Broker 协议、运维成本等方面各有取舍。**尤其不能把我们的 `outbox` 表说成 RocketMQ 事务消息在 Broker 里使用的存储结构。**

## 回看：这一章究竟新增了什么？

我们没有推翻之前的死信恢复、业务状态校验或消费幂等。只是在原来的恢复决策与发送之间，增加了一个最小的持久化桥梁：

```text
死信：msg-2002
      ↓
恢复前检查业务状态
      ↓
同一个本地事务
  ├─ recovery_jobs：允许重放
  └─ outbox：PENDING（含完整事件内容）
      ↓
后台进程读取并发送
      ↓
Broker 保存消息
      ↓
收到发送响应 → outbox 标记 SENT
      ↓
消费者按原始 source_id 幂等处理
```

这条链路至少使我们能清楚地区分三个事实：**业务决定已经提交、消息已经发送并获得确认、消费者已经产生业务效果**。它们不是同一回事，也不能用一个模糊的「成功」状态代替。

Outbox 在本章解决的是**不漏记待发送任务**；它没有消除网络不确定性，也没有自动保证精确一次投递。我们接受重复投递的可能性，再用第 04 章已经掌握的业务幂等机制限制重复业务效果。

但留给下一章的问题非常自然：**既然 RocketMQ 还提供事务消息，为什么它不直接采用我们刚刚写出的 Outbox？半事务消息、二次确认和事务状态回查究竟在解决哪一段不确定性？**

下一篇我们将暂时放下 Outbox 表，沿 `TransactionMQProducer` 到 `EndTransactionProcessor` 的真实调用链，复现并理解 RocketMQ 的半事务消息机制。

---

**源码与实验边界说明**：文中 Python 是自行编写并实际运行的教学化简，不是 RocketMQ 实现；三处 Java 函数调用片段基于 RocketMQ 5.5.1 固定 Commit 核对，展示的是关键真实源码节选；RocketMQ 官方事务消息文档用于解释对应协议语义。整个实验不包含真实 Broker、真实短信或物理断电测试。
