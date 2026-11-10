---
slug: /message-queue/retry-dead-letter-queue
title: 一条消息永远处理失败，难道后面的消息要一直等下去？
sidebar_position: 11
description: 沿着订单 1001 的失败现场，从无限重试、队头阻塞出发，构建有限重试与死信隔离，再沿 RocketMQ 5.5.1 的经典顺序消费调用链对照客户端回传与 Broker 的 DLQ 转换。
tags: [消息队列, RocketMQ, 源码精读, 消息重试, 死信队列, DLQ]
---

# 一条消息永远处理失败，难道后面的消息要一直等下去？

上一章，我们给消息增加了 `MessageGroup`：订单 `1001` 的创建通知失败，不会再拖住无关的订单 `1002`。但我们故意留下了一个没有回答的问题。

假如 `msg-1001` 的失败不是临时网络抖动，而是它依赖的通知模板已经被删除，无论再执行多少次，都会报相同的错误呢？

```text
同一个逻辑队列 queue-0

msg-1001       order:1001  OrderCreated   [通知模板缺失，持续失败]
msg-1002       order:1002  OrderCreated   [可正常完成]
msg-1001-paid  order:1001  OrderPaid      [等待同组前一条完成]
```

我们已经允许 `1002` 继续执行。但对于 `1001` 来说，失败后的处理过程变成了：

```text
领取 msg-1001 → 失败 → 等待 5 秒 → 再次领取
                                     ↓
                               失败 → 再等 5 秒
                                     ↓
                                    ...
```

**重试保证了我们不会轻易放弃一条消息，却不能保证这条消息终有一天会成功。**

这篇只解决一个问题：**当某条消息持续失败时，系统如何在“继续尝试”和“让其他工作前进”之间设定一个明确的边界？**

<!-- truncate -->

## 我们先不加新组件，让旧方案自己暴露限制

第 09 章的 `MessageGroupQueue` 已经有一个方法：`retry_later()`。消费失败时，它不删除消息，也不 ACK，而是清空旧领取凭证，并设置下一次可领取的时间。

这时的设计并没有错。通知服务短暂超时时，几秒后重试完全合理。

但它没有存储“这是第几次失败”。只要用户的业务程序不断报告失败，就可以一直执行 `retry_later()`。即使我们把下一次重试设成一分钟、五分钟，也只是延长等待，没有改变终点不存在的事实。

我们真正缺少的不是“更长的重试时间”，而是一个**判定何时不再自动重试**的规则。

对于本章的教学实验，先选择一个清楚、可验证的策略：

> 初次领取允许处理一次。如果失败，还允许**两次重试**；第三次处理仍失败，就停止自动领取，把这条消息隔离到专门的失败记录中。

注意我们说的是“两次重试”，不是“两次总尝试”。因此总共最多会有三次显式失败：

| 实际处理次数 | 本次结果 | 下一步 |
| --- | --- | --- |
| 第 1 次 | 失败 | 等待后重试 |
| 第 2 次 | 失败 | 等待后重试 |
| 第 3 次 | 失败 | 不再自动重试，转入失败隔离区 |

这个数字 `2` 是为了让实验简短而自行选定，**不是 RocketMQ 经典顺序消费的默认值**。真实系统的默认与配置方式，我们稍后从固定源码核对。

## 只增加一项记录：这条消息已经失败几次？

先回到我们的教学队列。此前 SQLite 的 `deliveries` 表已经记录是否 ACK、当前领取凭证和不可见时间。现在只需要在此基础上增加一个 `failure_count`。

一条消息的状态就能被观察成：

```text
messageId = msg-1001
acked = 0
failure_count = 0
receipt_token = None
invisible_until = 0
```

首次领取失败之后，增加计数、释放凭证，并让它稍后才可领取：

```text
messageId = msg-1001
acked = 0
failure_count = 1
receipt_token = None
invisible_until = 当前时刻 + 5
```

这还不够。若 `failure_count` 已达到阈值，消息总要有一个去处。直接删除很危险：排查问题时，我们会失去消息内容、失败原因，以及究竟失败过几次的信息。

所以我们先给失败消息建立一个**可查询的隔离区**。这就是理解死信（Dead Letter）的最小直觉：

> 不再参加正常自动领取，但保留原消息与失败信息，等待排查、修复或人工处理。

这里还不需要启动第二个 Broker，也没有必要提前介绍复杂的消息转储架构。我们先在现有的 SQLite 状态库中建立 `dead_letters` 表，验证隔离操作究竟应该改变什么。

## 第一个容易出错的地方：死信不等于 ACK

第 03 章我们已经区分了“拿到消息”和“确认业务处理成功”。这里必须继续坚持。

如果 `msg-1001` 永远没有发送成功，却被标记成 `acked=1`，我们就会失去一种重要的信息：**它为什么不再被消费？因为成功了，还是因为被放弃了？**

因此本章保留原来的 `acked` 字段，另外增加 `dead`：

```text
                acked  dead
正常待处理        0      0
业务执行成功      1      0
隔离到死信        0      1
```

`dead=1` 不是“已经完成业务”的同义词，只表示“退出正常自动领取”。这样可以继续利用过去已经建立的 ACK、不可见时间和领取凭证机制，不必重写消息正文与索引结构。

在配套文件 `dead_letter_message_group_queue.py` 的 `claim_grouped()` 中，我们只对取待处理消息的条件做了一处关键收紧：

```python title="dead_letter_message_group_queue.py / claim_grouped()（本章教学源码节选）"
pending = conn.execute(
    """SELECT seq, invisible_until FROM deliveries
       WHERE acked=0 AND dead=0 ORDER BY seq"""
).fetchall()
```

原来只检查 `acked=0`；现在还排除 `dead=1`。后面计算消息组组头、验证不可见时间、发放新的 `receipt_token` 的路径没有改变。

这样，死信消息会退出正常的领取候选集；该消息组中位于它后面的消息，也就有机会成为新的组头。

但要注意：**“变为可领取”不等于“现在业务上就应该执行”。** 这个区别我们在实验结束后还要回来讨论。

## 再增加一个动作：报告失败时决定重试还是隔离

我们新增 `report_failure()`，仍沿用第 05 章引入的领取凭证。

为什么失败处理还要验证凭证？想象这样的时间线：

```text
A 领取 msg-1001
A 卡住，领取超时
B 重新领取 msg-1001
A 恢复，迟到地报告“本次失败”
```

如果 A 的报告还能修改状态，它可能覆盖 B 正在执行的新一轮领取，甚至提前用完重试预算。此前用于拒绝迟到 ACK 的凭证检查，现在也应该保护失败报告。

**所以处理失败之前，先确认报告者仍然持有有效领取。**

```python title="dead_letter_message_group_queue.py / report_failure()（本章教学源码节选）"
row = conn.execute(
    """SELECT failure_count FROM deliveries
       WHERE seq=? AND acked=0 AND dead=0
         AND receipt_token=? AND invisible_until>?""",
    (receipt.seq, receipt.token, current),
).fetchone()

if row is None:
    conn.commit()
    return FailureResult("STALE", 0)
```

这段代码在 `BEGIN IMMEDIATE` 事务内部执行：

- `receipt.seq` 定位本轮领取的是哪条消息；
- `receipt.token` 必须与当前数据库凭证一致；
- `invisible_until > current` 防止过期的领取者回头修改状态；
- `acked=0 AND dead=0` 排除已经成功或已经被隔离的消息。

只有验证通过，才允许增加失败计数。

然后就是最小的重试分支：

```python title="dead_letter_message_group_queue.py / report_failure()（本章教学源码节选）"
failures = row[0] + 1
if failures <= self.max_retries:
    conn.execute(
        """UPDATE deliveries SET failure_count=?, receipt_token=NULL,
           invisible_until=? WHERE seq=?""",
        (failures, current + retry_delay_seconds, receipt.seq),
    )
    conn.commit()
    return FailureResult("RETRY", failures)
```

假设 `max_retries=2`：第一次、第二次显式失败会落入这个分支。状态保存后，下一次领取要等到约定时间，且旧凭证已被释放。

直到第三次失败，条件不再满足，才进入下面的隔离分支。

## 不能先移出原队列，再尝试保存死信

看起来我们只需：

```python title="概念伪代码：危险的提交顺序，不可直接用于可靠消息处理"
remove_from_active_queue(message)
save_to_dead_letter(message)
```

但如果第一步成功，第二步因磁盘错误失败呢？消息既不再正常消费，也没有留下死信记录。

这不是凭空制造的异常——前几章我们已经看到，“两个独立的写入动作”不会因为写在同一个 Python 函数里，就自动成为一个原子操作。

所以本章做了一个**刻意受限但真实可测试**的选择：正常领取状态和死信记录放在同一个 SQLite 数据库文件里、同一个事务中更新。先写入保留原消息与错误信息的 `dead_letters` 表，再将这条消息标记为 `dead=1`，最后提交。

```python title="dead_letter_message_group_queue.py / report_failure()（本章教学源码节选）"
message = self._read_at(receipt.queue_id, receipt.seq)
conn.execute(
    """INSERT INTO dead_letters
       (seq, message_id, group_key, failure_count, reason, message_json, dead_at)
       VALUES (?, ?, ?, ?, ?, ?, ?)""",
    (receipt.seq, message["messageId"], message_group(message), failures,
     reason, json.dumps(message, ensure_ascii=False), current),
)
changed = conn.execute(
    """UPDATE deliveries SET dead=1, failure_count=?, receipt_token=NULL,
       invisible_until=0 WHERE seq=? AND acked=0 AND dead=0""",
    (failures, receipt.seq),
).rowcount
if changed != 1:
    raise RuntimeError("死信标记失败，必须回滚死信记录")
conn.commit()
```

这里的重点不是 SQL 语法，而是提交边界：**死信记录存在**和**原消息退出正常领取**必须一起成功，或者一起失败。

我们甚至在测试中人为创建了一个禁止向 `dead_letters` 插入数据的 SQLite 触发器，用它模拟死信写入失败。结果是整个事务回滚，原消息 `dead=0`、`failure_count` 不被意外增加，仍可在租约结束后重新处理。

这一设计只在**同一个 SQLite 数据库的事务边界内**成立。我们的消息正文仍保存在独立追加文件中，索引也在其他文件内；它们不与 SQLite 组成跨文件原子事务。本章更没有证明“写入另一个 RocketMQ Topic 与原消费进度提交天然具有同一事务”。这两种情况都不能直接套用刚才的结论。

## 让 msg-1001 连续失败三次

本章继续使用上章建立的 `MessageGroupQueue`，并保留 `queue_count=1`：三个事件仍处于同一逻辑队列。

```python title="dead_letter_message_group_queue.py / demonstration()（本章实际运行的教学代码节选）"
q = DeadLetterMessageGroupQueue(Path(td), queue_count=1, max_retries=2)
q.publish("msg-1001", 1001, "OrderCreated")
q.publish("msg-1002", 1002, "OrderCreated")
q.publish("msg-1001-paid", 1001, "OrderPaid")
```

我们让 `msg-1001` 在 `now=100`、`106`、`112` 分别被领取，每次都报告“模拟通知模板缺失”。时间是注入的**教学逻辑时间**，不是让测试真的等待 12 秒。

完整示例可以直接运行：

```bash
python dead_letter_message_group_queue.py demo
```

本地实际执行的输出为：

```text
msg-1001 第1次失败 -> RETRY（累计失败 1 次）
msg-1001 第2次失败 -> RETRY（累计失败 2 次）
msg-1001 第3次失败 -> DLQ（累计失败 3 次）
死信隔离后：组内下一条 msg-1001-paid 变为可领取
注意：队列能继续不等于创建通知已成功；死信仍需人工或补偿处理
```

再看三个关键时刻的状态：

| 时刻 | `msg-1001` | `msg-1002` | `msg-1001-paid` |
| --- | --- | --- | --- |
| 第一次失败 | 等待重试 | 可以独立领取并 ACK | 仍需等待前一条 |
| 第二次失败 | 再次等待重试 | 已完成 | 仍需等待前一条 |
| 第三次失败 | **进入死信区，不再自动领取** | 已完成 | 成为该组新的可领取消息 |

最后一行非常重要。现在 `OrderPaid` 可以被领取，但我们依然不知道业务上是否允许“创建通知缺失，先发送支付通知”。

**DLQ 解决的是正常消费路径长期阻塞的问题，而不是自动修复业务因果依赖。**

如果这条消息只是给用户发送创建通知，可能可以按产品策略继续发送支付通知，同时单独补发缺失通知；如果它承担的是订单状态推进、资金扣减等强业务依赖，就不能在失败后机械地放行后续事件。业务可能需要暂停该订单、人工审核，或者执行补偿流程。

隔离是一种明确的失败结论，不是成功的伪装。

## 现在才进入 RocketMQ 源码：它怎样给重试设置终点？

我们继续固定 **Apache RocketMQ 5.5.1**，Commit [`efa1be59d6fc747ad220f036c86030f05f0419f9`](https://github.com/apache/rocketmq/commit/efa1be59d6fc747ad220f036c86030f05f0419f9)。

这里必须先声明阅读范围：我们选择的是**经典 Java PushConsumer 的顺序消费路径，以及发送重试消息时 Broker 的处理路径**。上一章演示的 SQLite 组级领取、5.x gRPC `MessageGroup` 和此处的经典顺序消费，不是同一个内部机制。我们不会把它们拼接成一个未经核实的“真实 RocketMQ 调用栈”。

我们只是验证同一个设计问题在成熟工程中怎样得到处理：**失败后到底继续留在正常重试路径，还是转入独立的失败存储？**

### 先看消费结果：失败之后到底由谁决定重试？

在 [`client/src/main/java/org/apache/rocketmq/client/impl/consumer/ConsumeMessageOrderlyService.java`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/client/src/main/java/org/apache/rocketmq/client/impl/consumer/ConsumeMessageOrderlyService.java) 的 `processConsumeResult()` 中，顺序消费回调返回 `SUSPEND_CURRENT_QUEUE_A_MOMENT` 时，会调用 `checkReconsumeTimes(msgs)`。

如果它返回 `true`，客户端把这些消息放回待处理集合，并通过 `submitConsumeRequestLater()` 延后下一次消费；如果返回 `false`，当前自动提交分支可以推进消费进度。

这说明新增的重试上限不是一个孤立的数字，它真正改变的是**失败消息是否继续挡住原来的顺序消费路径**。

紧接着看同一个类里的 `checkReconsumeTimes()`。以下为固定版本的**真实源码节选**：

```java title="ConsumeMessageOrderlyService.java / checkReconsumeTimes()（真实源码节选）"
if (msg.getReconsumeTimes() >= getMaxReconsumeTimes()) {
    MessageAccessor.setReconsumeTime(msg, String.valueOf(msg.getReconsumeTimes()));
    if (!sendMessageBack(msg)) {
        suspend = true;
        msg.setReconsumeTimes(msg.getReconsumeTimes() + 1);
    }
} else {
    suspend = true;
    msg.setReconsumeTimes(msg.getReconsumeTimes() + 1);
}
```

这里先只关注两个结果：

- **未达到上限**：设置 `suspend=true`，增加本地重试次数，继续等待后再次尝试。
- **已经达到上限**：尝试 `sendMessageBack(msg)`。如果回传失败，仍然暂缓并继续处理；不能因为想进入死信就直接忽略回传失败。

源码中还有一个很容易被忽视的默认值：`getMaxReconsumeTimes()` 在未显式配置（值为 `-1`）时，返回 `Integer.MAX_VALUE`。也就是说，**不能把本章教学实现的“最多重试 2 次”说成经典顺序消费的默认行为**。需要有限的上限，应当由具体使用的消费模式和配置来确定。

### 下一步：`sendMessageBack()` 为什么没有直接写 `%DLQ%`？

看同一文件里的 `sendMessageBack()`：

```java title="ConsumeMessageOrderlyService.java / sendMessageBack()（真实源码节选）"
Message newMsg = new Message(
    MixAll.getRetryTopic(this.defaultMQPushConsumer.getConsumerGroup()),
    msg.getBody());
```

即使代码注释谈到了死信，这里构造的仍是重试 Topic，而不是直接调用一个本地的 `writeDlq()`。

继续读可以看到，它会保留原消息标识及属性、设置 `RETRY_TOPIC`、重试次数和允许的上限，再调用内部 Producer 发送消息。这里的 `sendMessageBack()` 返回成功，只表示这条**回传请求**没有在该方法中抛错，不能将其当作原来的通知业务终于执行成功。

谁最终决定消息真正属于死信 Topic 呢？要继续沿这条重试消息的发送路径，走到 Broker。

### Broker 怎样把重试 Topic 改成死信 Topic？

在 [`broker/src/main/java/org/apache/rocketmq/broker/processor/SendMessageProcessor.java`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/broker/src/main/java/org/apache/rocketmq/broker/processor/SendMessageProcessor.java) 中，`sendMessage()` 会调用 `handleRetryAndDLQ()`。该函数检查请求 Topic 是否以重试前缀开头，取出消费者组、重试次数及上限，再判断是否需要转到 DLQ。

关键分支如下，省略了统计指标、Topic 创建与其它无关处理：

```java title="SendMessageProcessor.java / handleRetryAndDLQ()（真实源码节选，省略中间代码）"
if (reconsumeTimes > maxReconsumeTimes || sendRetryMessageToDeadLetterQueueDirectly) {
    // ... 统计指标、设置属性、创建或查找 DLQ Topic 等代码省略
    newTopic = MixAll.getDLQTopic(groupName);
    // ...
    msg.setTopic(newTopic);
    // ...
}
```

这个分支有两个需要注意的事实。

第一，客户端的 `checkReconsumeTimes()` 使用 `>=` 判断什么时候尝试回传；这个 Broker 分支使用 `>` 判断重试次数是否超限。**不要孤立比较这两个运算符就断言实现存在 off-by-one Bug**：回传消息时的计数处理、请求头，以及 Broker 的条件分支要连起来看。本章仅据这条已核对路径解释逻辑，不对所有客户端模式的次数作一刀切的推断。

第二，除了超过上限，这里还有 `sendRetryMessageToDeadLetterQueueDirectly` 分支。它依赖 Broker 对相关顺序消费锁状态的判断。也就是说，**真实源码中的死信转移决策并不只有我们的单一计数条件**。为了学清核心机制，我们先只研究“超过重试预算”的正常路径，不能把教学版 `report_failure()` 冒充完整实现。

最后还应注意：这个函数负责**决定消息 Topic 的归属**，后续还要经过正常的消息存储路径。进入 DLQ 与通知业务成功是两回事；发生失败时也仍要面对数据保存和故障恢复边界。

### 5.x 新消费模型是否与经典顺序消费完全一样？

**不是。** RocketMQ 的 [5.x 官方 Consumption Retry 文档](https://rocketmq.apache.org/docs/featureBehavior/10consumerretrypolicy/)描述了 PushConsumer 和 SimpleConsumer 的不同重试状态与间隔，并说明消费者组的最大重试策略。文档以“首次投递 + 后续重试”的方式解释次数，例如最多重试三次，可以有四次投递机会。

本章的教学设计也是这个计数直觉，但**并不意味着它直接对应经典 Java `DefaultMQPushConsumer.setMaxReconsumeTimes()` 的全部内部行为**，更不能把传统 `ConsumeMessageOrderlyService`、POP 以及 5.x gRPC Proxy 的运行分支当成同一种实现。

前几章已经看到：RocketMQ 的消息分组语义与固定版本的特定 POP 队列级检查之间可能存在差异。这个边界在讨论死信后依然存在。要对某种生产环境下的 FIFO+DLQ 行为下结论，仍需固定 SDK、消费类型、Broker/Proxy 配置并做端到端测试。

## 测试不只是“第三次能进 DLQ”

配套源码使用 Python 3.9+ 标准库，无需 API Key，也无需启动 RocketMQ。解压文件后运行：

```bash
python dead_letter_message_group_queue.py demo
python -m unittest -v test_dead_letter_message_group_queue.py
```

本章实际执行了 **19 项自动化测试，全部通过**。其中包括：

| 验证目标 | 测试看到的结果 |
| --- | --- |
| 临时故障后重试成功 | 原消息 ACK 成功，没有死信记录 |
| `max_retries=2` | 第三次显式失败后转入死信区 |
| 死信保留哪些信息 | 保留原消息正文、消息组、失败次数和原因 |
| 同组失败重试期间 | `OrderPaid` 不会越过 `OrderCreated` |
| 不同订单 | `1001` 重试时，`1002` 仍可以继续 |
| 失败到达死信上限 | 原消息退出自动领取，后续同组消息变为候选 |
| 迟到的失败报告与 ACK | 旧领取凭证不会改写新状态 |
| 重启恢复 | 已发生的失败次数与死信记录保持不变 |
| 死信记录插入失败 | 事务回滚，原消息不会被静默移出 |
| 两个进程竞争同一条消息 | 只有一个成功领取 |
| 两个线程同时报告最终失败 | 只产生一条死信记录 |

这些实验验证的是**本章 Python + SQLite 教学模型的语义**。我们没有在本章运行 RocketMQ Broker，也没有做断电测试、消息风暴压测、跨系统事务验证或真实短信发送测试。

本章还有一个主动保留的限制：`failure_count` 统计的是**带有效领取凭证、由消费者显式报告的失败次数**。如果消费者反复崩溃、只发生不可见时间到期，而一直没有报告失败，本模型允许它继续重新领取，**不会因租约超时自动消耗重试预算**。真实 RocketMQ 在不同消费类型下对超时与重新投递的计数规则需要单独核对。我们没有假装用这个有限重试实现涵盖所有失败路径。

## 回看这一层到底新增了什么？

我们没有改变消息的正文保存方式，没有替换 ConsumeQueue 风格索引，也没有推翻之前的消息组、领取凭证和 ACK。

相较上一章，真正的新机制只有两件事：

```text
第 09 章：按消息组保证局部顺序

order:1001  msg-1001 [失败] → 等待 → 重试 → 等待 → ...
order:1002  msg-1002 [完成]

第 10 章：给显式失败建立终点

order:1001  msg-1001
                |
                +-- 第一次失败 → 重试
                +-- 第二次失败 → 重试
                +-- 第三次失败 → 死信隔离
                                      |
                                      +-- 保留原消息与失败信息
                                      +-- 退出正常自动领取

             msg-1001-paid [变为可领取]
order:1002  msg-1002 [独立完成]
```

- **有限重试**回答：“自动尝试什么时候停止？”
- **死信隔离**回答：“停止后，失败消息的信息怎样保留下来？”

但结束自动重试并不是结束业务问题。

`msg-1001` 被放进隔离区后，它的通知仍然没有成功。现在我们知道它在哪里、为什么失败，却还不知道谁来修复它。假如工作人员修改了通知模板，是否可以把它重新放回正常队列？如果直接重放，是否会重复发送？同组的 `OrderPaid` 已经处理了，又怎样保证后续行为正确？

**下一章：《死信里的消息修好了，为什么不能直接重新投递？》**

到那时我们再研究死信恢复、重新投递、业务幂等以及顺序恢复之间的冲突，而不在这里提前把它们全部展开。

---

### 固定源码与资料

- [Apache RocketMQ 5.5.1 / Commit `efa1be59d6fc747ad220f036c86030f05f0419f9`](https://github.com/apache/rocketmq/commit/efa1be59d6fc747ad220f036c86030f05f0419f9)
- [`ConsumeMessageOrderlyService.java`：`processConsumeResult()`、`checkReconsumeTimes()`、`sendMessageBack()`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/client/src/main/java/org/apache/rocketmq/client/impl/consumer/ConsumeMessageOrderlyService.java)
- [`SendMessageProcessor.java`：`sendMessage()` 与 `handleRetryAndDLQ()`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/broker/src/main/java/org/apache/rocketmq/broker/processor/SendMessageProcessor.java)
- [RocketMQ 5.x 官方文档：Consumption Retry](https://rocketmq.apache.org/docs/featureBehavior/10consumerretrypolicy/)
- [RocketMQ 4.x 官方文档：Push 消费、经典顺序消费的重试差异](https://rocketmq.apache.org/zh/docs/4.x/consumer/02push/)

> **源码真实性与实验边界：** 文中的 Java 片段是固定 Commit 的真实源码节选，省略处均标记；Python 代码是自第 07、08、09 章逐步演化的独立教学实现。`dead_letters` 是本地 SQLite 表，**不是 RocketMQ 的 `%DLQ%` Topic**。本文没有把客户端经典顺序消费、5.x POP/MessageGroup 或官方语义混为同一条未经核对的源码路径。
