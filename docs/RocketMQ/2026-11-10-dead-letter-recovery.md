---
slug: /message-queue/dead-letter-recovery
title: 死信里的消息修好了，为什么不能直接重新投递？
sidebar_position: 12
description: 沿着订单 1001 的死信恢复，亲眼复现旧事件重放造成的状态倒退，用当前业务状态、源事件标识和恢复审计决定是否重放，并对照 RocketMQ 5.5.1 的死信路由源码。
tags: [消息队列, RocketMQ, 源码精读, 死信队列, 消息重放, 幂等性]
---

# 死信里的消息修好了，为什么不能直接重新投递？

上一章，我们终于让持续失败的 `msg-1001` 停止无限重试。它被隔离到死信区，原来的订单消息组也得以继续前进。

```text
order:1001

msg-1001       OrderCreated    [通知模板缺失 → 死信]
msg-1001-paid  OrderPaid       [后续处理成功]
```

运维同学修复了通知模板，于是提出一个非常合理的要求：**既然失败原因不存在了，能不能把死信里的 `msg-1001` 再发送一次？**

如果我们只把死信视为“可以稍后重试的任务”，答案似乎是肯定的。但先不要动手。注意上面第二条消息：在 `msg-1001` 被隔离以后，`OrderPaid` 已经成功处理了。

这意味着：**死信消息保存的是过去发生的事实，而业务状态可能已经向前走了。**

本章只解决一个核心问题：**恢复一条死信时，如何判断它现在是否仍然应该执行？**

我们先用原来的教学队列亲手造成一次状态倒退，再补上刚好足以避免这个错误的机制。之后还会遇到一个小但重要的新问题：即使恢复决策正确，重新发布这个动作本身也可能在中途被打断。

<!-- truncate -->

## 先试最直觉的方案：直接把死信重新放回队列

第 10 章把失败消息保存在 `dead_letters` 表里，留下了消息正文、失败原因和次数。修复模板后，最直觉的恢复方式就是读取这条记录，然后重新 `publish()`：

```python title="概念伪代码：尚未检查业务状态的直接重放"
dead = find_dead_letter("msg-1001")
publish_again(dead["message"])
```

这段思路本身不是语法错误。如果原来的失败只是临时性的，而且业务状态始终没变，再投递一次也许正是我们需要的。

但订单 `1001` 恰好不满足这个条件。

为了让后果可观察，我们给通知服务增加一项非常小的业务数据：它维护一个供通知展示使用的订单状态投影。处理创建事件时，记为 `OrderCreated`；处理支付事件时，记为 `OrderPaid`。

> **教学模型说明：** 这里的投影只是通知服务维护的派生状态，不是电商系统真正的订单主表。真实短信发送也不在实验范围内。我们让状态变化显式可见，便于检查过时事件被重放的危险。

假设它最初只是按消息类型直接覆盖状态：

```python title="概念伪代码：一个看似合理但不安全的消费函数"
def on_message(message):
    order_view[message["orderId"]] = message["eventType"]
```

正常按序消费时，它的行为没有问题：

```text
OrderCreated → 投影状态 OrderCreated
OrderPaid    → 投影状态 OrderPaid
```

然而死信恢复的时间顺序不再是原来的事件顺序：

```text
时间 100：msg-1001 处理失败，进入死信区
时间 102：msg-1001-paid 正常处理，状态成为 OrderPaid
时间 200：修好模板，重新投递 msg-1001
时间 201：旧创建消息再次处理，状态被覆盖为 OrderCreated

最终投影：OrderCreated   ×
实际较新的事件：OrderPaid
```

问题已经暴露了：**消息重新进入队列，不会自动回到它在业务历史中原来的位置。** 重新发布只会产生一次新的投递机会，不会倒转已经提交的其他业务效果。

所以，我们缺少的不是“一个重发按钮”，而是重发之前的**业务状态校验**。

## 给恢复动作加一个判断：这件事现在还需要执行吗？

还没有必要引入复杂的工作流引擎。先对这个贯穿案例确定一个明确的业务规则：

- 对于通知服务的 `OrderCreated` 事件，如果订单**已经出现 `OrderPaid`**，则旧创建通知不再自动补发；保留死信和人工决策记录。
- 如果还没有观察到后续状态，允许尝试重放创建通知。

这只是本系列的**示例通知策略**。实际业务可能要求补发全部通知，或必须先完成创建步骤后才允许支付相关动作。特别是扣款、库存等有强因果依赖的操作，绝不能照搬“过时就跳过”的规则。

现在我们新增一个很小的恢复记录 `recovery_jobs`，每条死信只对应一次经过审查的恢复决定：

```text
source_id      = msg-1001
operator       = oncall-1
decision       = SKIP_OBSOLETE
status         = RESOLVED
```

它没有抹掉第 10 章的 `dead_letters`。我们仍能追溯原始失败；恢复记录只解释：**运维后来对它做出了什么判断**。

看配套源码 `dead_letter_recovery.py` 的 `recover()`，关键代码是：

```python title="dead_letter_recovery.py / recover()（真实教学源码节选）"
row = conn.execute(
    "SELECT rank FROM order_view WHERE order_id=?", (message["orderId"],)
).fetchone()
obsolete = kind == "OrderCreated" and row is not None and row[0] >= 2
decision = "SKIP_OBSOLETE" if obsolete else "REPLAY"
status = "RESOLVED" if obsolete else "PREPARING"
```

这里的 `rank` 暂时只有两种：`OrderCreated=1`、`OrderPaid=2`。这个数字是为了复现实验而设计的简单规则，**不是 RocketMQ 的 Offset，也不是所有订单状态天然都能这样排序**。

是谁调用 `recover()`？不是 Broker，而是我们的恢复操作员或恢复管理程序。它的输入是死信中的原始事件 ID 和操作员身份；它读取本地死信、查询当前业务投影，并把恢复决策写入审计表。输出是 `SKIP_OBSOLETE` 或 `REPLAY`，由后面的流程消费。

如果最后得到 `SKIP_OBSOLETE`，它根本不需要重新发布消息。这样，订单 `1001` 的通知投影就不会因为补发旧消息而倒退。

## 让订单 1001 真的经历一次“禁止重放”

本章完整实验仍使用第 07～10 章的队列模型，初始化时将 `max_retries=0`，这样可以在一次显式失败后就看到死信状态，不必为演示反复运行三次。

```python title="dead_letter_recovery.py / demo()（教学源码节选）"
q.publish("msg-1001", 1001, "OrderCreated")
q.publish("msg-1001-paid", 1001, "OrderPaid")

created = q.claim_grouped(0, "worker-A", now=100)
q.report_failure(created, "模板缺失", now=101)

paid = q.claim_grouped(0, "worker-B", now=102)
lab.handle(paid, now=103)

print(lab.projection(1001))  # OrderPaid
decision = lab.recover("msg-1001", operator="oncall-1")
print(decision.decision)     # SKIP_OBSOLETE
```

在本地运行中，`msg-1001` 被隔离后，`msg-1001-paid` 确实能够完成。恢复判断看到较新的 `OrderPaid`，没有再次发布旧创建消息；原死信记录仍在，投影保持 `OrderPaid`。

现在我们只解决了“**不该重放时如何拒绝重放**”。但如果死信并没有被后续事件覆盖呢？

## 另一种情况：没有后继状态，确实需要恢复

换一个订单编号，但保持同一条故事线：订单 `2002` 的创建通知也因模板缺失进入死信。不同的是，这次没有支付通知或其他更新事件。

```text
order:2002

msg-2002       OrderCreated   [死信]
当前业务投影：无
```

此时恢复校验不会发现“旧事件已经落后于新状态”，于是返回 `REPLAY`。这正是前面那个判断不可省略的原因：**不是所有死信都应该跳过，也不是所有死信都应该重发。**

我们仍使用前面已经实现的 `publish()` 发送一条新消息，但马上遇到一个以前在第 04 章讨论过的老问题。

### 重新发布后，消息 ID 还是原来的 ID 吗？

我们的教学队列要求 `message_id` 唯一，而且原来的 `msg-2002` 仍留在死信和历史存储中。因此新投递需要一个新的**投递标识**：

```text
原事件身份： msg-2002
新的投递标识：replay:msg-2002
```

如果消费者的幂等键使用 `replay:msg-2002`，那么每恢复一次就可能变成新的业务动作；新投递和原事件也失去关联。

我们真正需要区分两个身份：

- **原始业务事件身份**：`msg-2002`，决定这件事是否已经产生过业务效果。
- **本次投递身份**：`replay:msg-2002`，帮助定位这次恢复生成的消息。

在真实系统中，业务事件 ID 应作为独立业务字段或属性传播，不应依赖 Broker 的物理位置或假设新的消息 ID 必须与旧消息相同。

为了不修改第 07 章的教学 `publish()` 函数签名，这章使用固定前缀和 `recovery_jobs` 表维护二者的映射。消费者拿到重放消息时，会先通过这项记录找回原事件身份；如果找不到，拒绝执行。

```python title="dead_letter_recovery.py / _source_id_for()（真实教学源码节选）"
with self._connect() as conn:
    row = conn.execute(
        "SELECT source_id FROM recovery_jobs WHERE replay_id=? AND decision='REPLAY'",
        (message_id,),
    ).fetchone()
    if not row:
        raise ValueError("没有找到重放消息的源事件记录；拒绝消费")
    return row[0]
```

这不是 RocketMQ 的重放协议，只是我们为了验证**源事件身份必须保留**而添加的最小教学实现。

### 为什么恢复后还需要原来的幂等判断？

接下来只需沿用第 04 章已有的原理：业务效果的去重键是**源事件 ID**，而不是“我第几次收到这条消息”。

在 `handle()` 中，我们先用 `source_id` 查询 `processed_events`，如果已经存在，就返回 `DUPLICATE`；否则在同一 SQLite 事务中更新通知投影、保存通知意图记录，并记下这一源事件的处理结果。

```python title="dead_letter_recovery.py / handle()（真实教学源码节选）"
existing = conn.execute(
    "SELECT outcome FROM processed_events WHERE source_id=?", (source_id,)
).fetchone()
if existing:
    result = "DUPLICATE"
else:
    # 检查当前订单投影是否比新消息更靠后；
    # 只有允许处理时才更新 order_view 和 notification_effects。
    ...  # 此注释为省略标记；完整分支见配套文件
```

此外，业务提交和队列 ACK 是两个动作。我们故意让第一次重放在**业务数据库提交后、ACK 之前**停止，然后等不可见时间过期、再次领取同一条重放消息。

```text
领取 replay:msg-2002
    ↓
恢复原事件身份 msg-2002
    ↓
业务投影和去重记录写入成功
    ↓
[模拟崩溃：没有 ACK]
    ↓
租约过期，另一消费者重新领取
    ↓
检测到 msg-2002 已经处理
    ↓
跳过重复业务效果 → ACK
```

这里“重复交付”是真实触发的教学队列行为；“没有多写一条通知意图”也是 SQLite 表里实际检查的结果。它**不能证明外部短信恰好发送一次**：真实短信供应商可能在返回超时之前已经发送成功，需要对外部副作用另外设计幂等键、查询或补偿机制。

## 把两种恢复决策放在一起看

到这里，我们对同样来自死信区的两条创建事件，给出了不同结果：

| 死信 | 当前较新状态 | 决策 | 效果 |
| --- | --- | --- | --- |
| `msg-1001` | `OrderPaid` 已处理 | `SKIP_OBSOLETE` | 不重发，保留审计 |
| `msg-2002` | 没有后继状态 | `REPLAY` | 生成新投递，保留源事件 ID |

第一种避免了过时事件的副作用，第二种让原来无法完成的工作得到新机会。它们都不依赖“死信一定重放”的简单假设。

### 这里还有一个必须说清的范围

我们的 `OrderCreated=1`、`OrderPaid=2` 是为两种通知事件设计的教学规则。更复杂的业务通常应从**可信订单主数据**检查当前状态，必要时还要核对每个实体的事件版本、前置条件和补偿记录。单纯依赖一个消费者维护的投影，不能防止它本身已经滞后；必要时应拒绝自动恢复并转交人工复核。

## 现在出现第二个问题：重放命令本身也可能失败

我们已经决定重放订单 `2002`，准备写入一条新消息。但在这一步中，恢复管理程序可能恰好崩溃：

```text
保存“准备重放 msg-2002”的审计记录
                 ↓
发布 replay:msg-2002 成功
                 ↓
          [恢复程序崩溃]
                 ↓
尚未把审计记录改成 QUEUED
```

运维重新打开页面，只看见“准备中”。如果这时再点一次“重放”，可能会重复发布同一个业务事件。

因此，我们再补一个很小的机制：**同一条源事件的恢复申请有唯一记录；遇到不确定状态时，先对账，而不是盲目发布第二次。**

本章的 `recover()` 先插入 `recovery_jobs`。如果操作员重复发起相同请求，就返回已存在的恢复记录，而不会直接重新 `publish()`。

```python title="dead_letter_recovery.py / recover()（真实教学源码节选）"
existing = conn.execute(
    "SELECT decision, status, replay_id FROM recovery_jobs WHERE source_id=?",
    (source_id,),
).fetchone()
if existing:
    return RecoveryDecision(*existing)  # 不盲目二次发布
```

若发生了“发布成功、审计尚未更新”的模拟异常，我们可以运行 `reconcile_published()`，检查本地投递元数据中是否已经存在 `replay:msg-2002`。存在则把审计更新成 `QUEUED`；不存在则返回 `NEEDS_RECONCILIATION`，交由人工检查，而不是擅自再发送。

要特别注意：这里的检查**不能覆盖所有崩溃窗口**。第 07 章的 `publish()` 分别修改追加日志、索引文件、SQLite 状态文件，本就不是跨文件原子提交。如果只写成了日志尾部，状态数据库却没有对应记录，仅靠 `deliveries` 查询还不足以判断消息是否真正持久化或可消费。

本章的策略是**宁可承认状态不确定，也不声称实现了精确一次重放**。更完善的发布状态机、事务性发件箱或 Broker 幂等投递，还需要专门讨论。

## 回到 RocketMQ 5.5.1：Broker 为什么不能替我们决定是否重放？

之前十章已经固定源码版本，这里继续沿用：

- 仓库：[apache/rocketmq](https://github.com/apache/rocketmq)
- 版本：**RocketMQ 5.5.1**
- Commit：[`efa1be59d6fc747ad220f036c86030f05f0419f9`](https://github.com/apache/rocketmq/commit/efa1be59d6fc747ad220f036c86030f05f0419f9)

这部分我们只追踪**经典重试消息进入 Broker 后转为 DLQ 的处理位置**，而不是虚构一个 RocketMQ Broker 内置的“检查订单支付状态再重放”函数。

### 进入死信的那一刻，源码保存了什么？

在 `broker/src/main/java/org/apache/rocketmq/broker/processor/SendMessageProcessor.java` 中，`sendMessage()` 会在处理消息请求时调用 `handleRetryAndDLQ()`。它检查重试 Topic、消费组和重试次数等信息，决定是否将消息改写到死信 Topic。

以下来自固定 Commit，属于**真实源码节选**：

```java title="SendMessageProcessor.java / handleRetryAndDLQ()（真实源码节选）"
newTopic = MixAll.getDLQTopic(groupName);
// ... 省略 Topic 配置与其它语句
msg.setTopic(newTopic);
```

[查看对应固定源码](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/broker/src/main/java/org/apache/rocketmq/broker/processor/SendMessageProcessor.java)

这个处理过程的调用者是 Broker 的消息发送处理路径。输入来自重试消息、请求头和消费组配置；它完成的是**更改消息要写入哪个 Topic**，而不是执行任何下单业务代码。后续消息仍需经过 Broker 的消息写入流程。

由此能解释一个设计边界：当它把消息标记为死信时，Broker 并不知道 `OrderPaid` 是否已经处理，也不知道重放创建通知是不是违反了业务策略。Broker 的 DLQ 与我们的 `recovery_jobs` 解决的是不同问题。

### 真正的业务恢复从哪里开始？

RocketMQ 5.x 官方[消费重试文档](https://rocketmq.apache.org/zh/docs/featureBehavior/10consumerretrypolicy/)明确将 DLQ 视为失败重试的最终兜底机制，也提到可以通过消费死信来开展业务恢复。但这**并不等于“死信中的每条消息都应该原样重新投递到原 Topic”**。

要把一条死信送回正常业务流程，恢复端至少需要回答：原始业务事件是什么？当前业务状态是否允许重做？应该保留哪个幂等键？重新发出去的消息应使用什么 Topic、消息组和业务标识？谁记录本次操作结果？

RocketMQ 5.x 官方[消息领域模型文档](https://rocketmq.apache.org/zh/docs/domainModel/05message/)还说明：消息中转时建议建立新的消息对象，不能依赖修改原消息视图。重新发送一条业务事件，和在原队列中把历史记录“重新移动到过去”，不是一回事。

**源码与实验的边界必须清楚：**

- `SendMessageProcessor.handleRetryAndDLQ()` 是已核对的 RocketMQ 5.5.1 Broker 真实实现，用来解释死信路由；它**不负责**本章教学恢复表里的业务状态校验。
- `recovery_jobs`、`order_view`、`processed_events` 是 Python/SQLite 的教学代码，不是 RocketMQ 自带的数据表，也不是 RocketMQ Dashboard 的内部实现。
- 我们没有启动实际 RocketMQ Broker，无法据此声称已经端到端验证 RocketMQ 的人工重发命令、代理端 FIFO 或不同客户端 SDK 的重放行为。

## 动手验证：这次实验真正证明了什么？

将本章 ZIP 解压到同一目录，使用 Python 3.9 或更新版本即可运行，无需 API Key，也无需 Docker：

```bash
python dead_letter_recovery.py demo
python -m unittest -v test_dead_letter_recovery.py
```

本章实际运行的演示输出：

```text
订单 1001：创建事件进死信，支付通知已处理；业务投影=OrderPaid
如果直接重放并覆盖投影：OrderPaid -> OrderCreated（状态倒退）
审查后：SKIP_OBSOLETE；订单 1001 投影仍为 OrderPaid
订单 2002：无后继状态，安排 replay:msg-2002
ACK 前故障再交付：首次 APPLIED，第二次 DUPLICATE；效果记录仅 1 条
注意：这里存的是通知意图，不是短信发送回执；没有验证外部短信精确一次。
```

另外执行了 **15 项自动化测试，全部通过**。它们不仅检查正常恢复，也覆盖了：

| 验证场景 | 实验结果 |
| --- | --- |
| `msg-1001` 已有较新支付状态 | 审查后跳过，投影不倒退，死信审计仍在 |
| `msg-2002` 没有后续状态 | 成功创建一条新的重放投递 |
| 同一个恢复请求提交两次 | 不会在正常顺序调用下再次发布 |
| 业务提交成功，但 ACK 前终止 | 再次领取时识别同一源事件，没有增加第二条效果记录 |
| 业务 SQLite 事务写入失败 | 整体回滚，消费者不 ACK |
| 发布成功、审计尚未更新就中断 | `PREPARING` 可对账到 `QUEUED`，不盲目二次发送 |
| 发布根本没有形成投递记录 | 返回 `NEEDS_RECONCILIATION`，保留人工处理空间 |
| 恢复程序重启 | 业务投影与恢复决策仍然存在 |
| 未登记来源的重放消息 | 被拒绝，不能直接触发业务 |

没有执行实际短信发送，没有声称外部副作用精确一次；也没有做断电实验、跨系统事务测试或生产环境的 RocketMQ 压测。

## 回看：死信恢复实际上比“重发”多做了什么？

现在可以回头看这条链路为什么会演化到今天：

```text
第 10 章：消费失败到达上限
          ↓
       死信隔离
          ↓
第 11 章：修复失败原因，准备恢复
          ↓
   读取当前业务状态
       /        \
   已过时       仍需处理
      |            |
 记录跳过       创建新投递
      |            |
 保留死信       保留原事件身份
                   |
               幂等业务处理
                   |
                确认 ACK
                   |
                恢复审计
```

我们没有因为恢复需求重写消息持久化、ConsumeQueue 风格索引、消息领取或 ACK；只是在上一章的死信隔离外增加了**恢复决策、源事件关联和操作记录**，同时复用了前面已经理解的业务幂等机制。

本章最值得记住的不是某个 API，而是这个判断：

> **消息被成功重新投递，只说明它又获得一次执行机会；它是否还应该执行，取决于当前业务状态和业务规则。**

但是还有一个问题没解决：我们已经知道“重放消息的写入”和“记录恢复进度”并不是同一个原子事务。如果恢复程序在这两个动作之间宕机，光靠重放按钮上的禁用状态并不足以形成可靠恢复流程。

**下一章：《恢复指令已经提交，消息却不知道有没有发出去：怎样处理发布与业务状态之间的双写问题？》**

我们会继续围绕订单 `2002`，从当前 `PREPARING` 的不确定状态出发，让事务性发件箱这样的机制由真实故障推动出现，而不是一开始就罗列分布式事务名词。

---

### 本章核对的源码与文档

1. [RocketMQ 5.5.1 固定 Commit](https://github.com/apache/rocketmq/commit/efa1be59d6fc747ad220f036c86030f05f0419f9)
2. [`SendMessageProcessor.java`：`sendMessage()` / `handleRetryAndDLQ()`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/broker/src/main/java/org/apache/rocketmq/broker/processor/SendMessageProcessor.java)
3. [官方文档：消费重试与死信](https://rocketmq.apache.org/zh/docs/featureBehavior/10consumerretrypolicy/)
4. [官方文档：消息领域模型与消息中转](https://rocketmq.apache.org/zh/docs/domainModel/05message/)

> **说明：** 文中 Java 片段是固定 Commit 的真实源码节选；`dead_letter_recovery.py` 是可运行的教学实现；两个系统的行为对照用于解释设计因果，并不宣称 Python 源码就是 RocketMQ 的实际执行代码。
