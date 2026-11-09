---
slug: /message-queue/message-consumption-acknowledgement
title: 消费者拿到消息，就代表处理成功了吗？
sidebar_position: 4
description: 继续追踪订单 1001 的通知消息，从一次失败的 pop(0) 出发，实现可恢复的消费确认，并对照 RocketMQ 5.5.1 的 POP ACK 源码理解消息与消费结果为何要分开管理。
tags: [消息队列, RocketMQ, 消息确认, 消费失败, 源码精读, Python]
---

# 消费者拿到消息，就代表处理成功了吗？

上一章，我们给订单 `1001` 的通知消息增加了磁盘追加日志。进程退出后重新启动，`msg-1001` 仍然能够被找回来。

然而，实验最后出现了一个反常的现象：

```text
第一次启动：读取 msg-1001 → 执行通知 → 当前内存队列为空
第二次启动：从 messages.jsonl 恢复 → msg-1001 又出现了
```

问题不再是“消息能否保存下来”，而是：**一条消息保存在磁盘上，系统怎么知道它是否已经处理完？**

这一篇只解决这个问题。我们仍然不搭建集群，不讨论消费者组，也不要求你提前理解 RocketMQ 的整个 POP 存储与重试架构。

<!-- truncate -->

## 先试最直接的办法：取到消息就删掉

上一章的内存版队列调用 `pop(0)`：

```python
# 教学化简：取消息的瞬间，消息就从列表中移除。
message = pending_messages.pop(0)
send_notification(message)
```

假设我们把这套逻辑搬到持久化队列里：一旦消费者取到 `msg-1001`，就将它从“待处理”记录中删掉。这样，下次启动时自然不会再次执行。

在通知总能成功的情况下，它似乎有效。但我们只需要改变一个条件：通知服务突然返回错误。

```text
从待处理集合删除 msg-1001
              ↓
调用通知服务
              ↓
通知服务报错，进程退出
              ↓
重新启动：找不到 msg-1001
```

这一次，重复的问题解决了，**但消息又丢了**。更准确地说，消息可能还在某个历史文件中，但我们的系统已经把它标成“不再需要处理”，因此没有机制重新投递。

问题的根源在于，我们将两个不同的事实当成了一件事：

- 消费者**拿到了**消息。
- 消费者**完成了**消息对应的业务操作。

拿到消息只能证明“可以开始处理”，不能证明短信已经发出。于是，我们真正需要记录的不是“消息被取走”，而是“消费者报告处理成功”。

## 把“取到”与“完成”分开

先不急着发明新名词。让 `receive_one()` 只返回消息，不改动磁盘日志：

```python
# 教学化简
message = queue.receive_one()  # 只读取，不删除消息
simulate_notification(message)  # 执行业务
queue.ack(message["messageId"])  # 成功后，单独记录完成状态
```

最后一行就是**消费确认（ACK，acknowledgment）**。在我们当前的例子里，它的意思非常朴素：只有业务处理成功之后，才写下一条“`msg-1001` 已完成”的记录。

如果业务调用抛出异常，`ack()` 不会执行。下一次程序启动时，`msg-1001` 依然属于待处理消息。

现在出现了一个自然的问题：我们上一章已经将原始消息写进不可覆盖的追加日志，确认状态要保存到哪里？

最小的方案是再增加一个文件：

```text title="persistent-queue-data/messages.jsonl"
{"messageId": "msg-1001", "type": "OrderCreated", "orderId": 1001}
```

```text title="persistent-queue-data/acknowledgments.jsonl"
{"messageId": "msg-1001"}
```

第一个文件证明**消息存在过**，第二个文件证明**消费者已经确认处理完成**。我们没有删除历史消息，而是在恢复时将已确认的消息排除掉。

```text
                   messages.jsonl
                   [msg-1001]
                        |
                        v
                 读取历史消息
                        |
                        v
               是否存在 ACK 记录？
                  /           \
                 否            是
                 |             |
                 v             v
             待处理消息      本轮不再交付
```

这就是本章需要的全部新机制：**把消息数据与消费完成状态分开保存**。

## 让 ACK 在进程重启之后仍然有效

先看最核心的几行 Python，都是本章实际教学程序的一部分：

```python
# 教学代码节选：AcknowledgedQueue.__init__()
self.messages = read_json_lines(MESSAGE_LOG)
self.acked_ids = {
    ack["messageId"] for ack in read_json_lines(ACK_LOG)
}
```

新进程启动时，我们先恢复历史消息，再读已经确认的 `messageId`。接下来找待处理消息，不再只是“列表里的第一条”，而是“尚未确认的第一条”：

```python
# 教学代码节选：AcknowledgedQueue.receive_one()
return next(
    (m for m in self.messages if m["messageId"] not in self.acked_ids),
    None,
)
```

注意：这仍然是一个**单消费者教学模型**。一次 `receive_one()` 不会排他地锁定消息；如果有两个进程同时调用，它们可能同时拿到同一条消息。现在我们只关心“单个消费者处理成功与失败如何跨进程保留”，还没有遇到必须解决并发领取的场景。

当通知成功后，才调用：

```python
# 教学代码节选：AcknowledgedQueue.ack()
durable_append(ACK_LOG, {"messageId": message_id})
self.acked_ids.add(message_id)
```

和上一章一样，`durable_append()` 在文件写入后执行 `flush()` 与 `os.fsync()`，然后才更新本进程内的确认集合。这样，我们至少不会出现“只修改了内存里的 `acked_ids`，下一次启动又完全忘记确认结果”的普通重启问题。

这仍然**不是生产级持久化承诺**：进程中途写入损坏、磁盘故障、父目录持久化、并发操作及外部通知服务的原子性，都没有被这个小程序一次解决。

## 第一次实验：失败不确认，成功才确认

下面的完整程序可以保存为 `acknowledged_message_queue.py`。它延续上一章的数据目录 `persistent-queue-data/`，保留相同的 `messages.jsonl` 格式，并增加 `acknowledgments.jsonl`。

为了能观察“通知到底执行了几次”，我们还用 `notification-effects.jsonl` 记录模拟通知的执行次数。这个文件只是**测试观察物**：写入一条记录代表执行一次模拟通知，并不代表真正向用户发送短信。

```python title="acknowledged_message_queue.py"
"""第三章教学代码：把消息存在与消息处理完成分开记录。

Python 3.9+ 标准库即可运行。仅支持单消费者、单进程顺序演示。
与上一章共用 persistent-queue-data/messages.jsonl 的数据格式，
但新增 acknowledgments.jsonl 和 notification-effects.jsonl。

这不是 RocketMQ 源码，也不实现网络 ACK、不可见时间、并发和分布式恢复。
"""

import json
import os
import sys
from pathlib import Path

DATA_DIR = Path(__file__).with_name("persistent-queue-data")
MESSAGE_LOG = DATA_DIR / "messages.jsonl"
ACK_LOG = DATA_DIR / "acknowledgments.jsonl"
EFFECT_LOG = DATA_DIR / "notification-effects.jsonl"
ORDER_FILE = DATA_DIR / "order-record.txt"


def read_json_lines(path):
    if not path.exists():
        return []
    entries = []
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"{path.name} 第 {line_number} 行不是有效 JSON；教学示例不会自动修复"
                ) from exc
    return entries


def durable_append(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    with path.open("ab") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())


class AcknowledgedQueue:
    """消费不删除原消息；处理成功后追加确认记录。"""

    def __init__(self):
        self.messages = read_json_lines(MESSAGE_LOG)
        self.acked_ids = {
            ack["messageId"] for ack in read_json_lines(ACK_LOG)
        }

    def enqueue(self, message):
        durable_append(MESSAGE_LOG, message)
        self.messages.append(message)

    def receive_one(self):
        # 没有领取锁和不可见时间，不能供多消费者并发使用。
        return next(
            (m for m in self.messages if m["messageId"] not in self.acked_ids),
            None,
        )

    def ack(self, message_id):
        if message_id in self.acked_ids:
            return
        if not any(m["messageId"] == message_id for m in self.messages):
            raise ValueError(f"未知消息不能被确认: {message_id}")
        durable_append(ACK_LOG, {"messageId": message_id})
        self.acked_ids.add(message_id)

    def pending(self):
        return [m for m in self.messages if m["messageId"] not in self.acked_ids]


def simulate_notification(message):
    # 本地效果记录，不连接真实短信服务。
    durable_append(EFFECT_LOG, {
        "messageId": message["messageId"],
        "orderId": message["orderId"],
    })
    print(f"模拟通知已执行: {message['messageId']}", flush=True)


def reset():
    for path in (MESSAGE_LOG, ACK_LOG, EFFECT_LOG, ORDER_FILE):
        if path.exists():
            path.unlink()
    print("已清空四个实验文件")


def produce(queue):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ORDER_FILE.write_text("1001", encoding="utf-8")
    message = {"messageId": "msg-1001", "type": "OrderCreated", "orderId": 1001}
    queue.enqueue(message)
    print("保存订单: 1001（教学文件）")
    print("写入消息: msg-1001")


def inspect(queue):
    pending = queue.pending()
    effects = read_json_lines(EFFECT_LOG)
    print(f"消息日志记录数: {len(queue.messages)}")
    print(f"已确认消息数: {len(queue.acked_ids)}")
    print(f"待处理消息: {[m['messageId'] for m in pending]}")
    print(f"模拟通知执行次数: {len(effects)}")


def consume(queue, mode):
    message = queue.receive_one()
    if message is None:
        print("没有待处理消息")
        return
    print(f"取到消息: {message['messageId']}", flush=True)
    if mode == "consume-fail":
        print("模拟业务处理失败：没有执行通知，也没有写 ACK", flush=True)
        return

    simulate_notification(message)
    if mode == "consume-crash-after-effect":
        print("模拟进程骤停：通知效果已记录，ACK 尚未写入", flush=True)
        os._exit(17)  # 为演示故障窗口刻意非正常退出。

    queue.ack(message["messageId"])
    print(f"确认完成: {message['messageId']}")


def main():
    modes = {
        "reset", "produce", "inspect", "consume-fail",
        "consume-success", "consume-crash-after-effect",
    }
    mode = sys.argv[1] if len(sys.argv) == 2 else "inspect"
    if mode not in modes:
        raise SystemExit(f"未知操作: {mode}; 可用操作: {', '.join(sorted(modes))}")
    if mode == "reset":
        reset()
        return
    queue = AcknowledgedQueue()
    if mode == "produce":
        produce(queue)
    elif mode == "inspect":
        inspect(queue)
    else:
        consume(queue, mode)


if __name__ == "__main__":
    main()
```

在命令行进入脚本所在目录，依次执行：

```bash
python acknowledged_message_queue.py reset
python acknowledged_message_queue.py produce
python acknowledged_message_queue.py inspect
python acknowledged_message_queue.py consume-fail
python acknowledged_message_queue.py inspect
```

这是我在本章实际执行的结果（各行 `python` 都会启动独立进程；下面仅合并展示输出）：

```text
已清空四个实验文件
保存订单: 1001（教学文件）
写入消息: msg-1001
消息日志记录数: 1
已确认消息数: 0
待处理消息: ['msg-1001']
模拟通知执行次数: 0
取到消息: msg-1001
模拟业务处理失败：没有执行通知，也没有写 ACK
消息日志记录数: 1
已确认消息数: 0
待处理消息: ['msg-1001']
模拟通知执行次数: 0
```

这回发生了一个关键变化：即使消费者已经**取到过** `msg-1001`，只要业务没有成功，我们就不写 ACK；再次启动时，消息仍然待处理。

现在让它处理成功：

```bash
python acknowledged_message_queue.py consume-success
python acknowledged_message_queue.py inspect
```

实际输出：

```text
取到消息: msg-1001
模拟通知已执行: msg-1001
确认完成: msg-1001
消息日志记录数: 1
已确认消息数: 1
待处理消息: []
模拟通知执行次数: 1
```

从输出可以直接判断：**消息仍保存在历史日志中，但已确认，所以这次启动不再把它当作待处理任务。** 我们终于把“保存过”与“处理完”这两个事实分开了。

## 对照 RocketMQ 5.5.1：Broker 为什么需要专门处理 ACK？

前面的 Python 代码是我们自己写的教学模型，绝不是 RocketMQ 的简化版源码。现在来看真实项目对“确认结果”这个需求是怎么处理的。

源码固定为：

- 仓库：[apache/rocketmq](https://github.com/apache/rocketmq)
- 版本：`rocketmq-all-5.5.1`
- Commit：[`efa1be59d6fc747ad220f036c86030f05f0419f9`](https://github.com/apache/rocketmq/commit/efa1be59d6fc747ad220f036c86030f05f0419f9)
- 文件：[`broker/src/main/java/org/apache/rocketmq/broker/processor/AckMessageProcessor.java`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/broker/src/main/java/org/apache/rocketmq/broker/processor/AckMessageProcessor.java)

**先限定阅读路径：**本章对照的是 RocketMQ 5.x 的 **POP 消费确认**，也就是 SimpleConsumer 这类以 `receive` 和 `ack` 表达消费结果的机制。它不等同于旧式 PullConsumer 的消费位移提交，也不能把 PushConsumer 的回调语义直接当作它的全部实现。

在这条路径里，业务应用的基本顺序是：通过客户端获取消息 → 执行通知逻辑 → 处理成功后调用 ACK。RocketMQ 官方[普通消息文档](https://rocketmq.apache.org/docs/featureBehavior/01normalmessage/)提供了 `simpleConsumer.receive(...)` 和 `simpleConsumer.ack(messageView)` 的用法。

消费者提交确认后，服务端得先理解“这条确认属于哪条消息、哪个队列、哪个消费组”，才能更新后续的消费处理逻辑。因此 Broker 有专门的 ACK 请求处理入口：`AckMessageProcessor.processRequest()`。

### 第一层：ACK 并不是删除消息正文的命令

查看 [`AckMessageProcessor.processRequest()` 的 ACK 分支](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/broker/src/main/java/org/apache/rocketmq/broker/processor/AckMessageProcessor.java#L1533-L1608)。该方法收到 `RequestCode.ACK_MESSAGE` 后，先解析 `AckMessageRequestHeader`，校验 Topic 与消息范围等条件；最终按配置将请求交给 `appendAckNew(...)` 或 `appendAck(...)`。

这意味着 ACK 进入 Broker 后，处理对象是**消费确认信息**，而不是“按 messageId 从 CommitLog 找到消息并删除正文”。这与我们的教学模型有相同的设计动机：记录完成状态，不靠物理删掉历史消息表示成功。

但它们的底层数据结构完全不同；不要将 `acknowledgments.jsonl` 当作 RocketMQ 真正使用的文件。

### 第二层：确认还需要携带消息的定位信息

继续看 [`appendAck(...)`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/broker/src/main/java/org/apache/rocketmq/broker/processor/AckMessageProcessor.java#L1653-L1803)。这条**真实源码节选**来自方法中构造 ACK 对象的相邻代码行：

```java
ackMsg.setConsumerGroup(consumeGroup);
ackMsg.setTopic(topic);
ackMsg.setQueueId(qId);
```

它们为什么存在？因为真实系统不能只记录 `msg-1001` 这一个业务 ID：一条消费确认还与消费组、Topic、队列位置、消息位置和领取时刻等信息有关。同一消息可能由不同消费组独立处理，不应该共享我们教学代码中那一个全局 `acked_ids` 集合。

在这个方法中，上述位置字段来自 ACK 请求头及其携带的额外信息；构造出的 `AckMsg` 继续交给 POP 消费确认与恢复机制处理。

### 第三层：ACK 的保存方式也有实现分支

在 [`processRequest()`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/broker/src/main/java/org/apache/rocketmq/broker/processor/AckMessageProcessor.java#L1600-L1607) 中，源码会检查 `popConsumerKVServiceEnable` 配置。启用时走 `appendAckNew(...)`，否则走 `appendAck(...)`。

这不是两个功能清单，而是一个对源码结论很重要的限制：**不能只读到 `appendAck()` 里构造 `AckMsg`，就断言 5.5.1 在所有配置下都用同一种方式保存 ACK。**

例如，旧路径在 [`appendAck()` 后半段](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/broker/src/main/java/org/apache/rocketmq/broker/processor/AckMessageProcessor.java#L1805-L1867)，会尝试将 ACK 合并到 POP 缓冲处理服务中；若不能合并，才构造发往 revive Topic 的内部 ACK 消息。另一条路径会调用 `PopConsumerService.ackAsync(...)`。我们现在只验证**ACK 有独立的服务端处理链**，不在本章深挖两条路径的恢复协议。

到这里，三件事终于对上了：

```text
本章教学实现                    RocketMQ 5.5.1 的 POP 路径

receive_one()            ←→     客户端 receive
simulate_notification()  ←→     业务代码处理消息
ack(messageId)           ←→     客户端 ack → Broker ACK 处理器
独立保存 ACK 状态         ←→     服务端处理消费确认信息
```

箭头表示**设计动作上的对照**，不是相同的源码调用链。真实 RocketMQ 还会涉及收取请求后的消息暂不可见、确认超时及重新投递等机制；本章教学程序没有实现这些能力。官方[消费重试文档](https://rocketmq.apache.org/docs/featureBehavior/10consumerretrypolicy/)介绍了 SimpleConsumer 的不可见时间与未确认后的重试行为。

## 第二次实验：通知做完了，ACK 还没来得及写

我们已经解决了一种故障：通知失败时不确认，消息可以重新处理。

但现在请考虑更刁钻的时机：

```text
取到 msg-1001
      ↓
通知实际执行成功
      ↓
进程突然退出！
      ↓
还没来得及写 ACK
```

这不是凭空想象。业务副作用与写入 ACK 是两个独立动作，我们的程序无法靠一句 `queue.ack()` 把它们变成原子事务。

为了让这个失败时机可以观察，程序提供 `consume-crash-after-effect` 模式：它先将模拟通知效果追加到文件，然后通过 `os._exit(17)` **有意以非正常状态结束进程**，跳过 ACK。

重新清空数据，独立执行：

```bash
python acknowledged_message_queue.py reset
python acknowledged_message_queue.py produce
python acknowledged_message_queue.py consume-crash-after-effect
```

最后一条命令**预期退出码为 `17`**，这是教学程序主动模拟的中断，并非无意的代码错误。我实际执行得到：

```text
取到消息: msg-1001
模拟通知已执行: msg-1001
模拟进程骤停：通知效果已记录，ACK 尚未写入
```

现在再启动新进程检查：

```bash
python acknowledged_message_queue.py inspect
```

实际输出：

```text
消息日志记录数: 1
已确认消息数: 0
待处理消息: ['msg-1001']
模拟通知执行次数: 1
```

看出矛盾了吗？**业务已经执行一次，但确认状态仍然是“未完成”。**

既然系统只能看到未确认消息，再次处理就符合我们刚刚建立的规则：

```bash
python acknowledged_message_queue.py consume-success
python acknowledged_message_queue.py inspect
```

实际输出：

```text
取到消息: msg-1001
模拟通知已执行: msg-1001
确认完成: msg-1001
消息日志记录数: 1
已确认消息数: 1
待处理消息: []
模拟通知执行次数: 2
```

`msg-1001` 的模拟通知执行次数变成了 **2**。这不是 ACK 写错了，而是一个更根本的权衡：

- 如果提前确认，再执行通知，失败时可能**漏通知**。
- 如果先执行通知，再确认，确认前崩溃时可能**重复通知**。

本章选择了后者，因为我们此刻首先希望“失败时消息还有机会重新处理”。但它没有自动得到“业务恰好执行一次”。

注意，我们验证的是 Python 教学程序在刻意中断时的行为，**不是 RocketMQ Broker 的故障注入实验**；真实 RocketMQ 重新投递还受到不可见时间、重试策略和消费状态等因素影响，不能直接套用这个脚本的即时重试时间。

## 回看本章：消息还在，消费状态变了

我们从上章的疑问出发，只引入了一项新机制：**消费完成后再确认，并持久化确认结果**。

```text
第二版：原始消息追加到磁盘
           |
           | 处理完成后重启，旧消息仍被当成待处理
           v
第三版：原始消息 + 独立 ACK 记录
           |
           | 失败不 ACK：可以再次处理
           | 成功写 ACK：重启后不再当成待处理
           v
新的矛盾：业务执行成功与 ACK 成功之间仍有故障窗口
```

现在读者已经能够解释：为什么“消费者已经拿到消息”不等于“系统能够安全地认定消息处理成功”；为什么 ACK 需要独立存在；以及为什么可靠重试与重复执行可能同时出现。

本章的 Python 实验已经实际运行，分别验证了失败不确认、成功确认后重启以及效果写入后骤停导致重复执行；RocketMQ 部分核对了固定 Commit 中 `AckMessageProcessor` 的入口和 ACK 处理分支，但**没有实际部署 RocketMQ 5.5.1 或声称通过了真实 Broker 的消费测试**。

下一章仍然使用订单 `1001` 的通知消息，接着追问：

> **既然同一条消息可能被重新交付，我们怎样让它即使被处理两次，也不会让用户收到两条通知？**

下一篇，我们才需要引入**幂等处理**。
