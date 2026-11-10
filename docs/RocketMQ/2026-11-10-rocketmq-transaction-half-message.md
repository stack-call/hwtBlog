---
slug: /message-queue/rocketmq-transaction-half-message
title: RocketMQ 事务消息：为什么先发送一条不能被消费的消息？
sidebar_position: 14
description: 从订单 2002 的恢复决定出发，先复现普通消息与本地事务的矛盾，再推导半事务消息、二次确认和事务回查；结合 RocketMQ 5.5.1 源码与可复现的 SQLite 实验理解其边界。
tags: [消息队列, RocketMQ, 源码精读, 事务消息, 半事务消息, 事务回查]
---

# RocketMQ 事务消息：为什么先发送一条不能被消费的消息？

上一章，我们已经为订单 `2002` 的死信恢复解决了一个问题：一旦业务数据库认定 `msg-2002` 可以重新发布，就把这项决定和待发送内容写进**同一个本地数据库事务**。后台进程再从 Outbox 中发送 `replay:msg-2002`。

那套方案有一个清楚的特点：**业务数据库先记下发送意图，Broker 稍后收到消息**。我们也已经接受：如果 Broker 收到了消息但应答丢失，后台可能再次发布，所以消费端仍然需要幂等。

但 RocketMQ 还提供一条不同的路。它不要求应用先建立 Outbox 表，而是问了一个看似反直觉的问题：

> **如果把消息先交给 Broker，却暂时不允许任何消费者看到它，会发生什么？**

这篇只研究这个问题。仍然使用原来的 `msg-2002` 和恢复事件 `replay:msg-2002`，不另外发明购物车、积分、库存三个案例。

<!-- truncate -->

## 先试一种更直接的写法

我们需要两件事最终对应起来：本地数据库批准恢复 `msg-2002`，下游最终收到这条恢复事件。

一种直觉是：既然担心本地事务提交后消息没有发出，不如**先发送消息，再执行数据库事务**。

```python title="概念伪代码：先发普通消息"
mq.send({"event_id": "replay:msg-2002", "order_id": 2002})
approve_recovery_in_database("msg-2002")
```

正常运行时，这当然可能成功。但请把程序停在两行之间：

```text
Broker 已经收到恢复消息
           ↓
消费者可能立刻开始恢复订单
           ↓
    [本地事务失败]
           ↓
数据库并没有批准这次恢复
```

现在消息已经被消费，数据库却没有相应的业务决定。我们只是把“本地事务成功、消息没发出去”的风险，换成了“消息发出去了、本地事务却没成功”的风险。

真正需要的不是**先发**或**后发**，而是：

> **消息可以先被 Broker 保存，但只有本地事务确定成功，才允许它对消费者可见。**

于是，我们给消息增加一个最小状态：`PREPARED`。它意味着 Broker 已经保存消息，但还没有获准交付。我们先把它叫作**半事务消息**。

## 第一次新增机制：消息先保存，但不能被消费

此时的流程很短：

```text
生产者                       Broker
   |                            |
   |----发送待确认消息----------->|
   |                            | 保存 PREPARED
   |<---保存成功-----------------|
   |                            | 消费者暂不可见
   |
   | 开始执行本地业务事务
```

为什么 Broker 必须先返回保存结果，生产者才继续执行本地事务？因为如果连“这条待确认消息”都没有成功保留下来，就没有后续可以提交的对象。这里讨论的是协议正常路径；发送应答丢失等异常，仍需要真实客户端的错误处理策略。

### 去真实源码里看：半消息究竟保存到哪里？

本章固定：

- 仓库：[`apache/rocketmq`](https://github.com/apache/rocketmq)
- 版本：**RocketMQ 5.5.1**
- Commit：[`efa1be59d6fc747ad220f036c86030f05f0419f9`](https://github.com/apache/rocketmq/commit/efa1be59d6fc747ad220f036c86030f05f0419f9)
- 观察路径：经典 Java 客户端 + Broker 的队列式事务消息实现；**不是**将所有 5.x gRPC 客户端、RocksDB 事务存储分支都视为完全相同的代码路径。

在 [`SendMessageProcessor.java`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/broker/src/main/java/org/apache/rocketmq/broker/processor/SendMessageProcessor.java) 中，发送处理器检查事务消息标识。如果是待确认的事务消息，它走 `TransactionalMessageService` 的 `prepareMessage()`（或异步对应方法），而不是普通消息的存储入口。

```java title="真实源码节选：SendMessageProcessor.java（同步分支）"
putMessageResult = this.brokerController.getTransactionalMessageService().prepareMessage(msgInner);
```

这行代码的**调用者**是 Broker 收到生产者消息请求后的发送处理器；**输入**是解析得到的 `msgInner`；**作用**是把“待确认的消息”交给专用的事务消息服务；**结果**是一次待确认消息的写入结果，随后由发送处理器构造响应返回给生产者。

接着看 [`TransactionalMessageServiceImpl.prepareMessage()`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/broker/src/main/java/org/apache/rocketmq/broker/transaction/queue/TransactionalMessageServiceImpl.java)：

```java title="真实源码节选：TransactionalMessageServiceImpl.java"
return transactionalMessageBridge.putHalfMessage(messageInner);
```

再向下一层，[`TransactionalMessageBridge.putHalfMessage()`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/broker/src/main/java/org/apache/rocketmq/broker/transaction/queue/TransactionalMessageBridge.java) 调用 `parseHalfMessageInner()`，保留原目标 Topic 和队列信息，调整事务标志，并把消息改写到事务半消息的内部存储位置，然后调用消息存储接口。该文件存在 RocksDB 配置相关分支；不能把所有部署都描述为只写入同一个固定 Topic。

这就能解释前面的设计：**半消息不是普通 Topic 中一条已经可以交付的消息，只是 Broker 留下了一个等待最终决定的持久化对象。**

它解决了“消息先发会抢跑”的问题，却留下了下一道关：**谁来告诉 Broker 本地事务到底成功了没有？**

## 第二次新增机制：本地事务结束后，再告诉 Broker 一次

既然 Broker 此时还不允许消费者读取，就可以安全地执行本地事务。

对订单 `2002`，我们仍然只做原来那件事：把“允许恢复 `msg-2002`”这个决定持久化。

本地事务结束，存在两种确定结果：

```text
           本地事务执行
              /   \
           成功    失败
            |       |
         COMMIT  ROLLBACK
            |       |
            v       v
         交付消息  不交付消息
```

这个“再发送一次决定”的动作，叫**二次确认**。它不是再次发送同一条业务消息，而是告诉 Broker 原来那条 `PREPARED` 消息应该如何结束。

经典 Java 客户端入口位于 [`TransactionMQProducer.sendMessageInTransaction()`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/client/src/main/java/org/apache/rocketmq/client/producer/TransactionMQProducer.java)：它要求配置 `TransactionListener`，并把发送交给生产者内部实现。这里传入的监听器负责执行本地事务、检查本地事务状态；不是消费者的业务回调。

从 Broker 的角度，二次确认由 [`EndTransactionProcessor.processRequest()`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/broker/src/main/java/org/apache/rocketmq/broker/processor/EndTransactionProcessor.java) 处理。

```java title="真实源码节选：EndTransactionProcessor.java（提交分支）"
result = this.brokerController.getTransactionalMessageService().commitMessage(requestHeader);
```

这次输入已经不是完整的业务消息正文，而是带有事务消息定位信息和提交意图的 `requestHeader`。处理器先查找并校验先前保存的半消息，然后恢复正常消息并调用 `sendFinalMessage(msgInner)` 写入可消费路径；成功后再处理半消息对应的完成记录。**这不等于简单修改一个 `visible=true` 字段。**

如果是回滚分支，Broker 处理原半消息的终止信息，不将其转为普通可消费消息。

于是我们得到一个更准确的语义：

- `PREPARED`：Broker 已保存等待决定的消息；业务消费者不可见。
- `COMMIT`：要求 Broker 将它转为可以正常投递的消息；实际实现需要完成写入及记录处理。
- `ROLLBACK`：终止这条事务消息，不向普通消费者投递。

这解决了正常路径。但现在，只要让程序在一个位置退出，新的问题立刻出现。

## 本地事务已经成功，但生产者来不及发 COMMIT

把程序停在这里：

```text
Broker：半消息保存成功
          ↓
业务数据库：批准恢复 msg-2002，提交成功
          ↓
       [生产者退出]
          ↓
Broker 仍只知道：PREPARED
```

这与上一章的 Outbox 故障长得很像，但关键区别是：**消息已经在 Broker 手里，只是 Broker 不知道该不该把它交给消费者。**

最简单的改进不是重新发布一条业务消息，而是让 Broker 在迟迟收不到结论时，向生产者询问：

> 对 `replay:msg-2002`，你本地数据库的最终事务结果是什么？

这才需要引入**事务回查**。生产者必须根据数据库里已经提交的事实作答，而不能靠一个随进程重启就丢失的内存变量。

在固定版本的 [`TransactionalMessageCheckService`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/broker/src/main/java/org/apache/rocketmq/broker/transaction/TransactionalMessageCheckService.java)，周期任务触发事务服务的 `check()`；[`TransactionalMessageServiceImpl.check()`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/broker/src/main/java/org/apache/rocketmq/broker/transaction/queue/TransactionalMessageServiceImpl.java) 扫描待确认消息并通过监听器发起检查请求。

生产者一侧，[`DefaultMQProducerImpl.checkTransactionState()`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/client/src/main/java/org/apache/rocketmq/client/impl/producer/DefaultMQProducerImpl.java) 调用事务检查监听器，并把检查结果转为提交/回滚指令。

```java title="真实源码节选：DefaultMQProducerImpl.java（检查监听器调用）"
localTransactionState = transactionListener.checkLocalTransaction(message);
```

这里的**调用者**是 Broker 回查请求触发的客户端执行任务；**输入**是待确认事务消息；**输出**是本地事务的判断；**下游**会将判断转换为结束事务的请求，再回到 Broker 的 `EndTransactionProcessor`。

问题又细化了一层：如果回查时数据库里根本没有结果呢？

我们不能简单把“没查到成功记录”等价为“事务失败”，因为本地事务可能仍在执行、状态尚未确定。因此还需要一个最小的第三种回答：`UNKNOWN`。它表示**现在不能安全地下结论**，不是成功，也不是回滚。

真实 RocketMQ 对回查次数、超时和丢弃行为存在配置限制，并不会无限期无限次回查。后面的实验只演示 `UNKNOWN` 继续等待，不冒充它的完整超时回收实现。

## 把这条链路完整跑一遍：一个 Python 文件即可

下面的代码是**教学化简**，不是 RocketMQ 源码。它用两个独立 SQLite 文件模拟两个持久化系统：

```text
business.sqlite                       broker.sqlite
业务批准记录 / 本地结果               待确认消息 / 可见消息
            |                              |
            +--执行本地事务--------------->|
            |                              |
            +--事务回查结果 COMMIT-------->|
                                           |
                                     允许普通消费
```

为了让“进程重启后还能回查”真正可以验证，所有关键状态都写入磁盘，而不是存在一个 Python 字典里。代码只模拟核心状态，不模拟 RocketMQ 的内部 Topic、CommitLog、事务 OP 记录、客户端网络协议和定时扫描。

把下面完整代码复制到 `half_message_lab.py`。**测试代码已经包含在同一个文件的最后，不需要另建测试脚本，也不需要 API Key 或 RocketMQ 服务。**

```python title="half_message_lab.py（完整教学实现，含 12 项测试）"
"""教学化简：半事务消息 + 业务数据库 + 事务回查。只依赖 Python 标准库。"""
import argparse
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TX_ID = "replay-tx:msg-2002"
SOURCE_ID = "msg-2002"
PAYLOAD = {"event_id": "replay:msg-2002", "source_id": SOURCE_ID,
           "order_id": 2002, "event_type": "OrderCreated"}


def connect(path):
    db = sqlite3.connect(path)
    db.execute("PRAGMA busy_timeout=5000")
    return db


class MiniBroker:
    """用数据库状态模拟 Broker；不是 RocketMQ 的真实物理存储。"""
    def __init__(self, path):
        self.path = str(path)
        with connect(self.path) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS tx_messages (
                tx_id TEXT PRIMARY KEY, payload TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN
                ('PREPARED', 'COMMITTED', 'ROLLED_BACK'))
            )""")

    def prepare(self, tx_id, payload):
        """保存待确认消息；消费者还不可见。"""
        data = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with connect(self.path) as db:
            row = db.execute("SELECT payload FROM tx_messages WHERE tx_id=?",
                             (tx_id,)).fetchone()
            if row is not None:
                if row[0] != data:
                    raise ValueError("相同 tx_id 不能对应不同的消息")
                return
            db.execute("INSERT INTO tx_messages VALUES (?, ?, 'PREPARED')",
                       (tx_id, data))

    def state(self, tx_id):
        with connect(self.path) as db:
            row = db.execute("SELECT state FROM tx_messages WHERE tx_id=?",
                             (tx_id,)).fetchone()
        return row[0] if row else None

    def finish(self, tx_id, decision):
        if decision not in ("COMMIT", "ROLLBACK"):
            raise ValueError("尚未确定的事务不能完成二次确认")
        target = "COMMITTED" if decision == "COMMIT" else "ROLLED_BACK"
        with connect(self.path) as db:
            row = db.execute("SELECT state FROM tx_messages WHERE tx_id=?",
                             (tx_id,)).fetchone()
            if row is None:
                raise ValueError("找不到半事务消息")
            if row[0] == target:
                return  # 同一结果重复确认：幂等
            if row[0] != "PREPARED":
                raise ValueError("拒绝冲突的二次确认")
            db.execute("UPDATE tx_messages SET state=? WHERE tx_id=?",
                       (target, tx_id))

    def visible(self):
        with connect(self.path) as db:
            rows = db.execute(
                "SELECT payload FROM tx_messages WHERE state='COMMITTED' "
                "ORDER BY tx_id").fetchall()
        return [json.loads(row[0]) for row in rows]


class LocalBusiness:
    """独立于 MiniBroker 的业务数据库。"""
    def __init__(self, path):
        self.path = str(path)
        with connect(self.path) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS approvals (
                tx_id TEXT PRIMARY KEY, source_id TEXT NOT NULL UNIQUE
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS outcomes (
                tx_id TEXT PRIMARY KEY, decision TEXT NOT NULL
                CHECK (decision IN ('COMMIT', 'ROLLBACK'))
            )""")

    def execute(self, tx_id, source_id, decision, fail_midway=False):
        if decision not in ("COMMIT", "ROLLBACK"):
            raise ValueError("本地事务必须明确成功或失败")
        with connect(self.path) as db:
            row = db.execute("SELECT decision FROM outcomes WHERE tx_id=?",
                             (tx_id,)).fetchone()
            if row:
                if row[0] != decision:
                    raise ValueError("本地事务结果已经确定，不能反转")
                return
            if decision == "COMMIT":
                db.execute("INSERT INTO approvals VALUES (?, ?)",
                           (tx_id, source_id))
            if fail_midway:
                raise RuntimeError("模拟本地事务中断，整个数据库事务应回滚")
            db.execute("INSERT INTO outcomes VALUES (?, ?)",
                       (tx_id, decision))

    def outcome(self, tx_id):
        with connect(self.path) as db:
            row = db.execute("SELECT decision FROM outcomes WHERE tx_id=?",
                             (tx_id,)).fetchone()
        return row[0] if row else "UNKNOWN"

    def approved(self, tx_id):
        with connect(self.path) as db:
            row = db.execute("SELECT 1 FROM approvals WHERE tx_id=?",
                             (tx_id,)).fetchone()
        return row is not None


class TransactionService:
    def __init__(self, directory):
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        self.broker = MiniBroker(root / "broker.sqlite")
        self.business = LocalBusiness(root / "business.sqlite")

    def prepare(self):
        self.broker.prepare(TX_ID, PAYLOAD)

    def local(self, decision, fail_midway=False):
        if self.broker.state(TX_ID) != "PREPARED":
            raise ValueError("必须先收到半事务消息写入成功的确认")
        self.business.execute(TX_ID, SOURCE_ID, decision, fail_midway)

    def check_and_finish(self):
        """生产者根据持久化的业务结果回复 Broker 回查。"""
        decision = self.business.outcome(TX_ID)
        if decision != "UNKNOWN":
            self.broker.finish(TX_ID, decision)
        return decision


class TransactionLabTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.svc = TransactionService(self.root)

    def test_prepared_is_invisible(self):
        self.svc.prepare()
        self.assertEqual(self.svc.broker.state(TX_ID), "PREPARED")
        self.assertEqual(self.svc.broker.visible(), [])

    def test_local_commit_alone_does_not_publish(self):
        self.svc.prepare()
        self.svc.local("COMMIT")
        self.assertTrue(self.svc.business.approved(TX_ID))
        self.assertEqual(self.svc.broker.visible(), [])

    def test_second_confirmation_makes_it_visible(self):
        self.svc.prepare()
        self.svc.local("COMMIT")
        self.assertEqual(self.svc.check_and_finish(), "COMMIT")
        self.assertEqual(self.svc.broker.visible(), [PAYLOAD])

    def test_local_rollback_never_delivered(self):
        self.svc.prepare()
        self.svc.local("ROLLBACK")
        self.assertEqual(self.svc.check_and_finish(), "ROLLBACK")
        self.assertEqual(self.svc.broker.visible(), [])

    def test_unknown_must_not_be_guessed_as_rollback(self):
        self.svc.prepare()
        self.assertEqual(self.svc.check_and_finish(), "UNKNOWN")
        self.assertEqual(self.svc.broker.state(TX_ID), "PREPARED")

    def test_crash_after_local_commit_then_restart_and_check(self):
        self.svc.prepare()
        self.svc.local("COMMIT")  # 故意不发二次确认，模拟进程退出
        restarted = TransactionService(self.root)
        self.assertEqual(restarted.check_and_finish(), "COMMIT")
        self.assertEqual(restarted.broker.visible(), [PAYLOAD])

    def test_failed_business_transaction_rolls_back_both_local_writes(self):
        self.svc.prepare()
        with self.assertRaises(RuntimeError):
            self.svc.local("COMMIT", fail_midway=True)
        self.assertFalse(self.svc.business.approved(TX_ID))
        self.assertEqual(self.svc.business.outcome(TX_ID), "UNKNOWN")
        self.assertEqual(self.svc.check_and_finish(), "UNKNOWN")

    def test_duplicate_confirmation_is_idempotent(self):
        self.svc.prepare()
        self.svc.local("COMMIT")
        self.svc.check_and_finish()
        self.svc.check_and_finish()
        self.assertEqual(len(self.svc.broker.visible()), 1)

    def test_conflicting_confirmation_rejected(self):
        self.svc.prepare()
        self.svc.broker.finish(TX_ID, "COMMIT")
        with self.assertRaises(ValueError):
            self.svc.broker.finish(TX_ID, "ROLLBACK")

    def test_local_requires_prepared_message(self):
        with self.assertRaises(ValueError):
            self.svc.local("COMMIT")
        self.assertEqual(self.svc.business.outcome(TX_ID), "UNKNOWN")

    def test_same_tx_id_cannot_change_payload(self):
        self.svc.prepare()
        with self.assertRaises(ValueError):
            self.svc.broker.prepare(TX_ID, {"event_id": "other"})

    def test_real_subprocess_restart(self):
        script = Path(__file__).resolve()
        def invoke(action):
            return subprocess.check_output(
                [sys.executable, str(script), "--dir", str(self.root), action],
                text=True)
        self.assertIn("PREPARED", invoke("prepare"))
        self.assertIn("COMMIT", invoke("local-commit"))
        self.assertIn("可见消息数: 0", invoke("inspect"))
        self.assertIn("回查结果: COMMIT", invoke("check"))
        self.assertIn("可见消息数: 1", invoke("inspect"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", default="./half-message-demo")
    parser.add_argument("action", choices=["prepare", "local-commit",
        "local-rollback", "check", "inspect", "test"])
    args = parser.parse_args()
    if args.action == "test":
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(TransactionLabTests)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        raise SystemExit(0 if result.wasSuccessful() else 1)

    svc = TransactionService(args.dir)
    if args.action == "prepare":
        svc.prepare()
        print("Broker 事务状态:", svc.broker.state(TX_ID))
    elif args.action == "local-commit":
        svc.local("COMMIT")
        print("本地业务事务:", svc.business.outcome(TX_ID))
    elif args.action == "local-rollback":
        svc.local("ROLLBACK")
        print("本地业务事务:", svc.business.outcome(TX_ID))
    elif args.action == "check":
        print("回查结果:", svc.check_and_finish())
    else:
        print("Broker 事务状态:", svc.broker.state(TX_ID))
        print("本地业务状态:", svc.business.outcome(TX_ID))
        print("可见消息数:", len(svc.broker.visible()))
        print("可见消息:", json.dumps(svc.broker.visible(), ensure_ascii=False))


if __name__ == "__main__":
    main()

```

## 实验一：只发送半消息，消费者看到什么？

在一个空目录里运行：

```bash
python half_message_lab.py --dir ./half-message-demo prepare
python half_message_lab.py --dir ./half-message-demo inspect
```

实际运行时我们得到：

```text
Broker 事务状态: PREPARED
Broker 事务状态: PREPARED
本地业务状态: UNKNOWN
可见消息数: 0
可见消息: []
```

这是本章要验证的第一个事实：**Broker 已经有消息，普通消费者仍然一条也看不到。**

如果删掉 `PREPARED` 这个中间状态，刚才的问题就会回来：消费者可能早于本地事务完成而执行恢复。

## 实验二：本地事务已经提交，但确认故意不发送

继续用同一个目录，运行：

```bash
python half_message_lab.py --dir ./half-message-demo local-commit
python half_message_lab.py --dir ./half-message-demo inspect
```

观察到：

```text
本地业务事务: COMMIT
Broker 事务状态: PREPARED
本地业务状态: COMMIT
可见消息数: 0
可见消息: []
```

这两条命令分别通过**独立 Python 进程**执行，因此已经模拟了“本地事务提交以后，负责二次确认的生产者进程没有继续运行”。业务结果存在 `business.sqlite`，半事务消息存在 `broker.sqlite`。

我们并没有在这里宣称事务消息已经完成：本地业务成功，与消费者真正能取到消息，是两个不同阶段。

## 实验三：重新启动后，用数据库事实回答回查

执行：

```bash
python half_message_lab.py --dir ./half-message-demo check
python half_message_lab.py --dir ./half-message-demo inspect
```

实际输出：

```text
回查结果: COMMIT
Broker 事务状态: COMMITTED
本地业务状态: COMMIT
可见消息数: 1
可见消息: [{"event_id": "replay:msg-2002", "event_type": "OrderCreated", "order_id": 2002, "source_id": "msg-2002"}]
```

新进程查询了本地数据库：它发现 `replay-tx:msg-2002` 对应的事务结果是 `COMMIT`，于是向教学 Broker 发出二次确认，消息才从不可见变成可见。

这说明**事务回查不需要原来那个 Python 进程还活着**；但需要一个仍可访问的业务数据源，以及能够回答该事务状态的生产者实例。真实 RocketMQ 会通过生产者侧的事务检查监听器完成该请求；这里用 `check` 命令手动触发，未实现 Broker 的定时任务。

## 实验四：如果本地结果还没确定呢？

使用一个新的目录：

```bash
python half_message_lab.py --dir ./half-message-unknown prepare
python half_message_lab.py --dir ./half-message-unknown check
python half_message_lab.py --dir ./half-message-unknown inspect
```

此时回查结果是 `UNKNOWN`，Broker 仍保持 `PREPARED`，可见消息数仍为 `0`。如果此时把 `UNKNOWN` 当成 `ROLLBACK`，就可能提前终止一笔尚未完成的本地事务。

教学代码**故意没有**在未知状态下擅自提交或回滚。现实中，事务检查需要配合超时、最大检查次数、异常处理和业务对账等完整策略。对于一直无法查询到结果的事务，不能承诺一定最终提交；同样，事务消息不是无限期保留的“保险箱”。

## 自动化验证：一次运行全部 12 项测试

所有测试函数就在上面的 `half_message_lab.py` 中。运行：

```bash
python half_message_lab.py test
```

本章已实际运行，结果为：

```text
Ran 12 tests in 4.329s

OK
```

耗时受机器环境影响，重点是 `12 tests` 和 `OK`。这些测试覆盖了半消息不可见、本地事务回滚、业务提交后确认丢失、重启后检查、未知状态、重复确认、冲突确认以及业务数据库事务中途异常；其中有一项使用真正的 Python 子进程验证跨进程恢复。

注意验证边界：**这里只验证 Python + SQLite 教学机制，不是启动 RocketMQ Broker 的端到端测试，也没有模拟真实网络断连、物理断电或 RocksDB 事务存储分支。**

## 现在回头看：为什么不能直接用上章的 Outbox 代替？

上章的 Outbox 并没有过时；它与事务消息是在两个不同位置保存“将来应该发生什么”的证据。

| 观察点 | Transactional Outbox | RocketMQ 事务消息 |
|---|---|---|
| 第一个持久化动作 | 在业务数据库中同时写业务决定与待发送事件 | Broker 先存放待确认的半事务消息 |
| 本地事务何时执行 | 与 Outbox 插入位于同一个数据库事务 | 在半事务消息被确认保存后执行 |
| 消息何时可见 | 后台发送成功进入普通消息通路后 | 收到并成功完成 COMMIT 后 |
| 发送确认不明确 | 后台继续扫描待发送记录，可能重复发布 | Broker 可以回查生产者的本地事务状态 |
| 依赖的恢复证据 | 本地 Outbox 表中的待发送记录 | Broker 保存的半消息 + 生产者可查询的本地事务结果 |
| 能否代替消费端幂等 | 不能 | 不能 |

这个对照不是性能排名。Outbox 的优势在于把业务决定与发送意图合并在同一个应用数据库事务中；事务消息的特点是让 Broker 主动保留一条待确认的消息，再通过生产者的本地事务结果决定是否使它可见。两者的耦合方式、运维代价和异常恢复责任并不相同。

还有三条边界必须保留：

1. **事务消息不等于跨系统数据库的 XA 原子提交。** 它依赖半消息、二次确认和状态回查，目标是协调本地事务与消息发布的最终结果，而不是把 Broker 与业务数据库强行合并成一个数据库事务。
2. **回查不是凭空知道业务结果。** 如果生产者无法连接数据库、找不到事务记录，或者所有可用生产者都无法正确回答，Broker 不能代替业务作出可靠判断；真实实现的超时与次数上限也会影响最终处理。
3. **消息成功发布不等于下游业务成功，更不等于精确一次。** 这仍要用前面学过的 ACK、重试和幂等机制来处理。

## 从头回看，这套协议是怎样长出来的？

最开始，我们只是想把 `replay:msg-2002` 交给下游。但“先发送普通消息”可能让消费者早于本地事务运行。为了不让消息抢跑，我们只增加了一个不可见的 `PREPARED` 阶段。

半消息解决了抢跑，却必须等待本地业务决定，因此我们增加 `COMMIT / ROLLBACK` 二次确认。确认可以解决正常流程，但生产者可能在本地提交后退出，于是 Broker 需要主动询问状态。为了不把进行中的事务误判为失败，回查结果又必须允许 `UNKNOWN`。

```text
普通消息先发：可能早于本地事务被消费
                     ↓
             半消息 PREPARED
                     ↓
           本地事务执行并记录结果
                     ↓
            二次确认 COMMIT / ROLLBACK
                     ↓
          如果确认丢失：Broker 回查
                     ↓
        查持久化业务结果，不凭内存猜测
                     ↓
             决定交付、回滚或待定
```

到这里，读者可以自己回答开篇的问题：**“不能被消费”的半消息，不是绕远路，而是在 Broker 已经保留消息、业务尚未确定成功之间，建立一个安全的等待位置。**

## 下一章：半事务消息已经保存在 Broker，它自己宕机了怎么办？

这篇我们假设 Broker 写入半消息后能够保存住它、之后也能够恢复未决事务的检查工作。但如果**半事务消息所在节点本身发生故障**，又该怎样保证消息和事务处理进度仍然可用？

下篇我们会从这个真实故障出发，研究消息副本与故障恢复。依然只追踪 `replay:msg-2002`，先复现单节点的问题，再进入 RocketMQ 对应的复制源码。

---

**源码参考（固定版本）**

- [TransactionMQProducer.java](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/client/src/main/java/org/apache/rocketmq/client/producer/TransactionMQProducer.java)
- [SendMessageProcessor.java](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/broker/src/main/java/org/apache/rocketmq/broker/processor/SendMessageProcessor.java)
- [TransactionalMessageServiceImpl.java](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/broker/src/main/java/org/apache/rocketmq/broker/transaction/queue/TransactionalMessageServiceImpl.java)
- [TransactionalMessageBridge.java](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/broker/src/main/java/org/apache/rocketmq/broker/transaction/queue/TransactionalMessageBridge.java)
- [EndTransactionProcessor.java](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/broker/src/main/java/org/apache/rocketmq/broker/processor/EndTransactionProcessor.java)
- [TransactionalMessageCheckService.java](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/broker/src/main/java/org/apache/rocketmq/broker/transaction/TransactionalMessageCheckService.java)
- [DefaultMQProducerImpl.java](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/client/src/main/java/org/apache/rocketmq/client/impl/producer/DefaultMQProducerImpl.java)
- [RocketMQ 官方事务消息文档](https://rocketmq.apache.org/zh/docs/featureBehavior/04transactionmessage/)

**阅读标记说明**：文中标记“真实源码节选”的 Java 行来自上述固定 Commit；“概念伪代码”仅用于解释故障；`half_message_lab.py` 为可实际运行的教学化简。实验已在 Python 中执行，未声称模拟实现和 RocketMQ 的存储布局、完整异常处理、超时参数完全一致。
