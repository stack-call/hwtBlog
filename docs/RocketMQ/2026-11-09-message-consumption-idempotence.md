---
slug: /message-queue/message-consumption-idempotence
title: 为什么一条消息只发送一次，消费者却可能执行两次？
sidebar_position: 5
description: 继续追踪订单 1001 的 msg-1001：在业务执行完成但 ACK 尚未写入时制造崩溃，用数据库唯一约束与本地事务实现可验证的消费幂等，并回看 RocketMQ 5.5.1 的 ACK 源码边界。
tags: [消息队列, RocketMQ, 源码精读, 重复消费, 幂等性, SQLite]
---

# 为什么一条消息只发送一次，消费者却可能执行两次？

上一章，我们终于把两件事分开了：**消息存在**和**消息已经处理成功**。

订单 `1001` 的通知消息 `msg-1001` 保存在磁盘上。消费者取到消息后执行通知，成功才写 ACK；如果处理失败，下次还能把它找回来。

这看起来已经很可靠了。可上一章最后一次实验却留下了一个问题：

```text
取到 msg-1001
    ↓
模拟通知执行成功（效果记录 +1）
    ↓
进程在写 ACK 之前退出
    ↓
重启后，系统看到 msg-1001 仍未确认
    ↓
再次执行通知（效果记录又 +1）
```

最终，**同一条消息触发了两次模拟通知**。这并不是我们的 ACK 判断出了错：第一次业务效果已经产生，但队列不知道这件事。它只能按「未确认就允许重新处理」的规则行动。

我们不能简单地删掉重试，因为那会重新带来漏处理风险。本章要解决的唯一问题是：**既然同一条消息可能被执行多次，如何让同一项业务效果不被重复产生？**

<!-- truncate -->

## 一个看似无解的时间窗口

把消费者缩到最简单的三行：

```python
message = queue.receive_one()
create_notification_record(message)
queue.ack(message["messageId"])
```

这三行的先后顺序不是偶然。我们不希望业务还没做完就 ACK，否则之后的故障会让消息被误认为已完成。

但反过来，先执行业务再 ACK，中间就存在一个时间窗口：

```text
业务成功 ────[ 进程可能退出 ]────> ACK 成功
```

想要靠重新调整这两行的顺序消除风险，做不到：

- **先 ACK，后做业务：**可能在 ACK 后崩溃，业务一次也没完成。
- **先做业务，后 ACK：**可能在业务完成后崩溃，业务被再次执行。

原因是我们的模拟业务记录与队列 ACK 写在不同的存储位置。把它们放在两行代码里，不会自动产生一个横跨两者的原子操作。

既然无法只靠 ACK 判断业务是否已经执行，我们能否让业务自己认出「这件事已经做过」？

## 第一种直觉：用一个集合记住处理过的消息

最小的实现不需要新组件：

```python
# 概念伪代码：仅演示判断思路，不具备崩溃恢复能力
processed = set()

def handle(message):
    if message["messageId"] in processed:
        return
    create_notification_record(message)
    processed.add(message["messageId"])
```

同一进程内重复传入 `msg-1001` 时，它看起来有效。但我们恰恰是在**进程退出后重试**才发现问题。新进程的 `processed` 是空集合，第一次已经发生过的业务效果不会随着它一起恢复。

要记住已处理的业务，状态必须在进程之间保留。一个简单选择是数据库。

于是有了第二个版本：

```python
# 概念伪代码：仍然有严重的崩溃与并发窗口
if not database.has_processed(message["messageId"]):
    database.mark_processed(message["messageId"])
    create_notification_record(message)
```

这里我们又犯了一个更隐蔽的错误：如果 `mark_processed()` 已成功提交，但程序在 `create_notification_record()` 之前退出，下次重试会看到「已处理」，直接跳过，结果业务反而永久没有执行。

这不是多加几次重试就能解决的。**“记住已处理”和“真正产生业务效果”必须一起成功、一起失败。**

## 这时才需要引入幂等性

对当前订单通知案例，我们先明确一个业务规则：

> 每个订单的 `OrderCreated` 事件，只允许生成一条「下单通知记录」。

如果对相同的业务事件执行两次，最终数据库状态与执行一次相同，我们就说这个处理操作对这个业务效果具有**幂等性（Idempotence）**。

注意，这不等于代码只执行一次，也不等于消息只投递一次。我们的消费函数完全可能进来两次；要控制的是它对业务状态产生的影响。

那么，该用哪个字段判断「同一件事」？

消息中目前有：

```json
{
  "messageId": "msg-1001",
  "type": "OrderCreated",
  "orderId": 1001
}
```

直接用 `messageId` 去重可以应对相同消息的再次交付。但同一个业务事件也可能被重新发布成一条**新消息 ID**：例如 `msg-1001-copy`。只比较消息 ID，就会把同一个下单通知当成两件事。

因此，我们依据刚刚定义的业务规则，用：

```text
业务键 = type + ":" + orderId

OrderCreated:1001
```

这个业务键不是 RocketMQ 提供的“天然唯一键”，而是应用根据业务语义做出的选择。如果未来同一订单可以合法产生多个不同版本的创建事件，这个键也需要调整，不能机械照搬。

现在我们只差最后一步：怎样让“去重标记”和“模拟通知记录”一起提交？

## 用同一个数据库事务，把两件事绑在一起

本章沿用第三章的 `AcknowledgedQueue`，**不修改消息读取与 ACK 的逻辑**。只替换原来的 `simulate_notification()`：让业务效果进入 SQLite，并由数据库保证重复的业务键不会再次插入。

建立两张小表：

```sql
-- 教学代码节选：idempotent_notification_consumer.py / open_business_db()
CREATE TABLE processed_events (
    business_key TEXT PRIMARY KEY,
    first_message_id TEXT NOT NULL
);

CREATE TABLE notification_records (
    business_key TEXT PRIMARY KEY,
    order_id INTEGER NOT NULL
);
```

`processed_events` 记录哪些业务事件已经执行过；`notification_records` 表示本次创建的**模拟通知业务记录**。这两个表必须在同一个本地事务里修改。注意：写下一条通知记录，**不等于已经发送真实短信**，这一点稍后会专门讨论。

来看最关键的真实教学代码节选：

```python title="idempotent_notification_consumer.py：commit_business_effect_once()（节选）"
conn.execute("BEGIN IMMEDIATE")
inserted = conn.execute(
    "INSERT OR IGNORE INTO processed_events (business_key, first_message_id) "
    "VALUES (?, ?)",
    (key, message["messageId"]),
).rowcount

if not inserted:
    conn.commit()
    return False

conn.execute(
    "INSERT INTO notification_records (business_key, order_id) VALUES (?, ?)",
    (key, message["orderId"]),
)
conn.commit()
return True
```

这几行共同保证了一件事：在当前 SQLite 数据库里，**一个业务键的去重标记与模拟通知记录作为同一事务提交**。

- `business_key` 是主键。相同业务键不能插入第二行。
- `INSERT OR IGNORE` 不先做一次独立的 `SELECT`，而是让数据库约束决定本次是否拿到了“第一次处理”的资格。
- `BEGIN IMMEDIATE` 在 SQLite 中预先进入写事务；本实验即使多个进程尝试写入，同一数据库也会协调写入互斥。但这不意味着我们的教学队列有了多消费者领取锁。
- 只有成功插入新标记的分支，才创建模拟通知记录；两者最后一起 `commit()`。

这样，第二次处理同一业务键时，`rowcount` 为 `0`，业务效果会被跳过；**不过仍要 ACK 当前收到的消息**，否则它会一直留在待处理集合里。

最终顺序是：

```text
收到 msg-1001
      |
      v
尝试提交业务事务
      |
      +---- 第一次处理 ----> 写去重标记 + 通知记录，一起提交
      |
      +---- 重复处理 -----> 数据库发现业务键已存在，跳过写业务
      |
      v
不管首次还是重复，只要业务状态已经满足要求
      |
      v
ACK 当前消息
```

这正好回应了上一章的故障现场：**重投递可以继续存在，但不会再导致这个数据库业务效果翻倍。**

## 运行实验一：业务提交成功，ACK 前突然退出

下载本章的示例文件，放在同一目录：

```text
acknowledged_message_queue.py       # 第三章沿用的队列与 ACK
idempotent_notification_consumer.py # 本章增加的业务幂等
```

只需要 Python 3.9+ 标准库，不需要 API Key、Docker、RocketMQ Broker 或短信服务。

以下每一条命令都会启动一个独立 Python 进程。**请在专门的实验目录执行**，因为 `reset` 会删除教学用的历史日志与模拟数据库。

```bash
python idempotent_notification_consumer.py reset
python idempotent_notification_consumer.py produce
python idempotent_notification_consumer.py consume-crash-after-effect
```

最后一条命令预期以退出码 `17` 结束，是脚本刻意模拟的崩溃，不是程序意外损坏。

我实际执行（本实验从全新临时目录开始）得到：

```text
写入消息: msg-1001, 订单: 1001
收到消息: msg-1001
新增模拟通知记录: OrderCreated:1001
模拟进程骤停：业务事务已提交，但还没有 ACK
```

重启后检查：

```bash
python idempotent_notification_consumer.py inspect
```

实际输出的关键部分是：

```text
已确认消息数: 0
待处理消息: ['msg-1001']
业务处理尝试次数: 1
已提交去重标记数: 1
已提交模拟通知记录数: 1
```

它跟上一章的故障状态一样：业务效果已经存在，但 ACK 尚未写入。现在让消费者再次处理它：

```bash
python idempotent_notification_consumer.py consume-success
python idempotent_notification_consumer.py inspect
```

本次实际输出：

```text
收到消息: msg-1001
去重命中，跳过业务效果: OrderCreated:1001
已确认消息: msg-1001
消息日志记录数: 1
已确认消息数: 1
待处理消息: []
业务处理尝试次数: 2
已提交去重标记数: 1
已提交模拟通知记录数: 1
已提交业务键: ['OrderCreated:1001']
```

**处理尝试次数为 2，但数据库业务效果只有 1 条。** 这就是本章最核心的可观察结果。第三章只能避免“处理失败却 ACK”；第四章进一步让“处理成功但 ACK 丢失”不再重复插入同一业务记录。

## 运行实验二：崩在数据库事务的正中间呢？

我们的新方案会不会在“写完去重标记，尚未写入通知记录”时，又掉进之前那个坑？

这次我们把崩溃点提前：

```bash
python idempotent_notification_consumer.py reset
python idempotent_notification_consumer.py produce
python idempotent_notification_consumer.py consume-crash-before-commit
```

预期退出码为 `19`。此时插入标记的 SQL 已执行，但事务还没有提交，也没有写入通知记录。

再次启动检查数据库，两个数都应是 `0`：

```text
已提交去重标记数: 0
已提交模拟通知记录数: 0
```

这不是“SQLite 帮我们自动执行了通知”，而是它把**未提交的修改回滚**了。下一次正常处理，才能同时写下去重标记与通知记录。

这里我们看到了为什么“先在数据库标记已处理，然后调用业务函数”依然不够：真正需要的是**把被保护的业务状态变更与标记放在同一事务中**。

## 再改一个输入：新消息 ID，还是同一个订单

先让 `msg-1001` 正常处理并确认，然后再生产一条新消息：

```bash
python idempotent_notification_consumer.py reset
python idempotent_notification_consumer.py produce
python idempotent_notification_consumer.py consume-success
python idempotent_notification_consumer.py produce-duplicate
python idempotent_notification_consumer.py consume-success
python idempotent_notification_consumer.py inspect
```

`produce-duplicate` 生成 `msg-1001-copy`，但它携带的仍然是 `OrderCreated` 和 `orderId=1001`。

实测：两个消息 ID 都已确认，但模拟通知记录仍只有一条。

```text
消息日志记录数: 2
已确认消息数: 2
待处理消息: []
业务处理尝试次数: 2
已提交去重标记数: 1
已提交模拟通知记录数: 1
已提交业务键: ['OrderCreated:1001']
```

这说明**消息唯一性和业务唯一性不是同一个问题**。如果仅以 `messageId` 为业务幂等键，这个反例就会失败。反过来，若我们生产 `OrderCreated:1002`，则应该允许生成另一条通知记录，因为那是另一项合法业务。

## 回到 RocketMQ 5.5.1：为什么 Broker 的 ACK 解决不了这个问题？

到这里，我们一直在自己的 Python 教学代码里验证行为。现在用真实源码划清边界：**RocketMQ 负责管理消息的消费状态，但它并不知道你的业务数据库里究竟插入了几条通知记录。**

本系列固定版本与上一章相同：

- 仓库：[`apache/rocketmq`](https://github.com/apache/rocketmq)
- 版本：`rocketmq-all-5.5.1`
- Commit：[`efa1be59d6fc747ad220f036c86030f05f0419f9`](https://github.com/apache/rocketmq/commit/efa1be59d6fc747ad220f036c86030f05f0419f9)
- 文件：[`broker/src/main/java/org/apache/rocketmq/broker/processor/AckMessageProcessor.java`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/broker/src/main/java/org/apache/rocketmq/broker/processor/AckMessageProcessor.java)
- 阅读方法：`processRequest()`，下钻 `appendAckNew()` 的**非顺序 POP ACK** 路径

为什么读 ACK 代码？因为我们正在确认一个设计边界：如果 Broker 自己已经负责业务去重，消费者就不必为订单通知添加这张业务表。实际情况并非如此。

当 Broker 收到 `RequestCode.ACK_MESSAGE` 请求时，`processRequest()` 会解析请求头、检查 Topic/队列位置等条件，然后按配置选择处理路径。

以下是**RocketMQ 5.5.1 真实源码节选**（仅省略之前的请求校验）：

```java title="AckMessageProcessor.java / processRequest()"
if (brokerController.getBrokerConfig().isPopConsumerKVServiceEnable()) {
    appendAckNew(requestHeader, null, response, channel, null);
} else {
    appendAck(requestHeader, null, response, channel, null);
}
```

[直接查看固定 Commit 的对应代码](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/broker/src/main/java/org/apache/rocketmq/broker/processor/AckMessageProcessor.java#L1600-L1608)

它说明这条 ACK 请求的处理要分配置分支；所以不能把 `appendAck()` 说成 5.5.1 全部消费确认的唯一实现。

继续沿着启用 `popConsumerKVServiceEnable` 的路径往下走。在 `appendAckNew()` 的普通非顺序消息分支中，服务端会调用：

```java title="AckMessageProcessor.java / appendAckNew()（真实源码节选）"
this.brokerController.getPopConsumerService().ackAsync(
    popTime, invisibleTime, groupId, topicId, queueId, ackOffset);
```

[直接查看固定 Commit 的对应代码](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/broker/src/main/java/org/apache/rocketmq/broker/processor/AckMessageProcessor.java#L1873-L1903)

这几个参数来自消息领取与 ACK 的上下文：什么时候领取、不可见时间、消费组、Topic、队列 ID、队列位置。它们足以让 Broker 处理对应的消费确认，却**没有“订单 1001 的通知是否已写入业务数据库”这一事实**。

至于 `PopConsumerService.ackAsync()` 内部如何更新消费状态，以及未确认消息如何重新可见，属于我们以后分析 POP 恢复路径的内容；本章只读到足以证明边界的这层源码。

RocketMQ 官方[消费重试文档](https://rocketmq.apache.org/docs/featureBehavior/10consumerretrypolicy/)也明确说明 SimpleConsumer 未确认时会在不可见时间结束后重试；官方[最佳实践](https://rocketmq.apache.org/docs/bestPractice/01bestpractice/)则建议对重复敏感的业务在消费者侧做幂等处理，并且要注意数据库操作的原子性。

因此，两边的职责应该这样分开理解：

```text
RocketMQ POP / Broker                 订单通知消费者

提供消息给客户端    ------------->   读取 msg-1001
                                        |
                                        v
                                   以业务键检查并
                                   提交业务数据库事务
                                        |
                                        v
接收 ACK 并更新消费状态  <-------------   业务结果满足要求后 ACK
```

这张图是**职责与数据流对照**，不是从网络入口到 SDK 的完整真实调用链。我们的 SQLite 表也不是 RocketMQ 的内部设计。

## 一个必须说清楚的边界：这还不等于短信只发送一次

前面我们让「通知记录」保存在本地 SQLite，并与去重标记一起事务提交，所以可以验证**本地数据库中的效果最多创建一次**。

可是，如果把代码改成真正的外部短信调用：

```python
# 概念伪代码：这里刻意强调两个系统之间的原子性缺口
send_sms(order_id=1001)        # 外部短信服务已经接受请求
mark_processed_in_database()  # 这里可能还没执行就崩溃
```

我们的数据库事务管不到短信服务。如果短信已经发出，但数据库没记住，重试仍可能再发；如果先在数据库标记、再发短信，又可能出现标记成功但短信根本没发出的情况。

这意味着：**“本地业务状态的幂等”不自动等于“跨系统外部副作用恰好发生一次”。** 真正处理外部短信，需要额外考虑下游是否支持幂等键、如何设计可靠投递与状态核对等机制。本章的模拟程序没有验证真实短信的端到端一次性保证。

还有三个限制同样应该记住：当前程序的 `AcknowledgedQueue.receive_one()` 没有多消费者领取锁；本地 SQLite 不负责分布式数据库之间的原子提交；本实验也没有运行 RocketMQ Broker 进行故障注入。我们只验证了**单消费者教学队列 + SQLite 本地事务**在指定崩溃窗口中的行为。

## 回看这一路，我们到底多解决了一件什么事？

从第三章到这一章，我们并没有改变消息系统的交付规则，依然允许未确认消息被再次处理。变化发生在**业务消费者这一侧**：

```text
第三章：消息持久化 + 成功后 ACK
           |
           v
     业务完成但 ACK 前崩溃
           |
           v
     重试导致业务效果重复
           |
           v
第四章：选择稳定的业务键
           |
           v
     去重标记与业务效果同事务提交
           |
           v
     消息可重复处理，数据库效果不重复插入
```

本章完整示例是 `idempotent_notification_consumer.py`，跨进程回归测试在 `test_idempotent_notification_consumer.py`。我实际运行了四个测试：ACK 前崩溃、事务提交前崩溃、不同消息 ID 的相同业务事件，以及不同订单的合法通知，四项均通过。代码使用 Python 3 标准库，无需连接 RocketMQ。

**下一章的问题已经出现了：**到目前为止，我们始终只有一个消费者。如果订单瞬间增长到几万笔，一个消费者忙不过来，最自然的办法是启动两个消费者。但我们当前的 `receive_one()` 根本没有领取锁——两个消费者可能同时得到同一条消息。

我们已经知道业务数据库可以挡住某一种重复效果，可让两个消费者无控制地争抢同一条消息，仍然会浪费资源、产生重试混乱。

下一章，就让第二个消费者真正加入进来，看看单消费者模型第一次遇到并发时会发生什么。
