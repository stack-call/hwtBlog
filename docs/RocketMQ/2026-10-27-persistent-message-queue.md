---
slug: /message-queue/persistent-message-queue
title: 进程重启之后，消息还能找回来吗？
description: 延续订单 1001 的通知任务，从内存列表走向磁盘追加日志，用独立进程验证消息恢复，再对照 RocketMQ 5.5.1 的存储入口理解持久化的边界。
tags: [消息队列, Python, RocketMQ, 持久化, 源码精读]
---

# 进程重启之后，消息还能找回来吗？

上一章，我们让订单 `1001` 的通知任务进入一个 Python 列表。下单函数调用 `append()`，处理函数调用 `pop(0)`，通知不再必须在下单请求里完成。

但是，当我们运行完 `produce`，再启动一个新进程执行 `inspect` 时，得到的结果是：

```text
订单已经保存在文件中
进程退出前：pending_messages = [msg-1001]
重新启动后：pending_messages = []
```

订单还在，尚未处理的通知却不见了。

问题非常明确：**我们需要让消息的存在，不再依赖创建它的那个 Python 进程。**

这一篇先不讨论网络、Broker 或者集群。我们只给上一章的列表增加一项能力：重新启动程序后，仍然能够读回 `msg-1001`。等这个目标真的实现，再去看成熟消息系统为什么需要专门的存储代码。

<!-- truncate -->

## 最容易想到的办法：把整个列表写进文件

上一章的列表已经存着我们需要的全部信息：

```python
pending_messages = [
    {"messageId": "msg-1001", "type": "OrderCreated", "orderId": 1001}
]
```

既然内存会随着进程退出而消失，我们可以在增加消息后，把当前列表序列化成 JSON，写到文件里：

```python
# 概念示例：先演示最简单的文件保存方式。
import json
from pathlib import Path

pending_messages.append(message)
Path("messages.json").write_text(
    json.dumps(pending_messages, ensure_ascii=False), encoding="utf-8"
)
```

下次启动时，再从 `messages.json` 加载列表。

对于只有一条通知任务的演示，这个办法确实可以工作。现在已经没有理由仅仅为了“能重启”就引入复杂的日志组件。

不过，假设订单 `1001` 的通知还没处理完，订单 `1002` 又来了。第二次写文件时，我们会把**整个列表重新写一遍**。以后增加第 3 条、第 4 条消息，也都要重新写之前的内容。

```text
写入 msg-1001：写 1 条记录
写入 msg-1002：重写 2 条记录
写入 msg-1003：重写 3 条记录
...
```

而且，直接覆盖旧文件的做法一旦在写入过程中被打断，文件就可能只写了一部分，甚至丢失原有内容。生产代码可以借助“写临时文件再原子替换”等策略改进，但那样我们就要先解决一套快照更新问题。

回到眼前的需求：**每次有一条新消息到来，我们真正想做的只是留下这一条消息，为什么还要反复重写旧消息？**

## 只追加新消息：我们的第一份消息日志

既然新消息只需要排在旧消息之后，最小的调整是：文件不再表示“当前完整的列表”，而是表示“曾经写入过哪些消息”。

先把每条消息转成一行 JSON：

```text title="messages.jsonl"
{"messageId": "msg-1001", "type": "OrderCreated", "orderId": 1001}
{"messageId": "msg-1002", "type": "OrderCreated", "orderId": 1002}
```

文件名的 `.jsonl` 表示 *JSON Lines*：一行是一条独立的 JSON 记录。这只是本系列自制队列的教学格式，**不是 RocketMQ 的磁盘文件格式**。

现在，添加消息不需要改写已有记录，只要打开文件并在末尾写入新的一行：

```python
# 教学化简：FileQueue.enqueue() 的关键动作。
encoded = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")

with self.log_file.open("ab") as f:
    f.write(encoded)
```

`ab` 表示以二进制追加模式打开文件。假设文件原先保存了 `msg-1001`，新消息 `msg-1002` 会追加到它后面，而不是覆盖它。

这就引出了本章第一个真正需要的存储概念：**追加日志（append-only log）**。这个名字并不神秘，它只是给刚刚的设计起了个名字。

但这里马上有一个小细节不能忽略：`write()` 返回，是否就意味着即使机器突然断电，数据也绝对不会丢？

## 写入文件，不等于已经拥有绝对的持久性

通常情况下，Python 向文件写入的数据会经过语言运行时和操作系统的缓冲。调用 `write()`，不应该直接等同于“数据已经到达稳定的物理介质”。

为了让这次教学实验的写入步骤更明确，我们在返回“入队成功”之前，再做两件事：

```python
# 教学代码：这是本例的实际实现片段。
with self.log_file.open("ab") as f:
    f.write(encoded)
    f.flush()
    os.fsync(f.fileno())

self.pending.append(message)
```

`flush()` 将 Python 文件对象的缓冲内容交给操作系统；`os.fsync()` 请求操作系统同步该文件已修改的数据和相关元数据。之后才把消息加入当前进程的列表。

这样设计是为了让“函数返回成功”有更清晰的写入前提，而不是为了证明一个不可能从这段代码直接得出的结论：**任何类型的断电、磁盘控制器故障或文件系统异常都不会造成数据丢失**。

例如，新创建文件时的父目录持久化、存储设备是否履行同步语义，以及写到一半留下的不完整记录，都不是这几行代码能够一并解决的。我们这次也不会假装模拟了物理断电。

本章的验证范围很清楚：**正常结束一个 Python 进程，再运行新进程，能否恢复尚未处理的消息？**

## 新进程怎样找回消息？

写入一行只是前半段。我们还需要在新进程启动时，从文件中重建那份内存列表：

```python
# 教学化简：FileQueue._load_from_disk() 的核心路径。
messages = []
with self.log_file.open("r", encoding="utf-8") as f:
    for line in f:
        if line.strip():
            messages.append(json.loads(line))
return messages
```

这几行代码里，没有隐藏任何网络协议：从头读文件，每读到一条合法 JSON，就把对应消息加入新进程的 `pending`。

于是，同一条 `msg-1001` 的生命过程发生了变化：

```text
进程 A（produce）                  磁盘                 进程 B（inspect）

place_order(1001)
       |
       v
追加 msg-1001 -----------------> messages.jsonl
       |                              |
       v                              |
返回下单成功                          |
                                      |
进程 A 结束                           |
                                      v
                              读取 messages.jsonl
                                      |
                                      v
                               恢复 [msg-1001]
```

注意，我们并没有“复活”第一个进程的列表。第二个进程创建了一个**全新的列表**，只不过它的内容来自之前留下的文件。这才是跨进程恢复的关键。

## 完整可运行实验

下面是本章完整的 Python 程序。保存为 `persistent_message_queue.py`，使用 Python 3 即可运行，不需要 API Key，也不需要启动 RocketMQ。

它仍然只模拟订单 `1001` 和通知打印。订单保存在普通文本文件中，用来延续上一篇的观察；它不是数据库，订单写入与消息写入之间也没有事务保护。

```python title="persistent_message_queue.py"
"""教学化简：用追加日志让消息在正常进程重启后可恢复。

运行（依次执行，每条命令都是一个新进程）：
    python persistent_message_queue.py reset
    python persistent_message_queue.py produce
    python persistent_message_queue.py inspect
    python persistent_message_queue.py consume
    python persistent_message_queue.py inspect

注意：这不是 RocketMQ 源码，也不是生产级消息队列。
"""

import json
import os
import sys
from pathlib import Path

DATA_DIR = Path(__file__).with_name("persistent-queue-data")
ORDER_FILE = DATA_DIR / "order-record.txt"
LOG_FILE = DATA_DIR / "messages.jsonl"


class FileQueue:
    """本进程用列表取消息；启动时从追加日志恢复。"""

    def __init__(self, log_file):
        self.log_file = log_file
        self.pending = self._load_from_disk()

    def _load_from_disk(self):
        if not self.log_file.exists():
            return []

        messages = []
        with self.log_file.open("r", encoding="utf-8") as f:
            for line_number, line in enumerate(f, start=1):
                if not line.strip():
                    continue
                try:
                    messages.append(json.loads(line))
                except json.JSONDecodeError as e:
                    raise ValueError(
                        f"消息日志第 {line_number} 行损坏，当前教学程序不会自动修复"
                    ) from e
        return messages

    def enqueue(self, message):
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        encoded = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")

        # 先把消息追加到文件，再让当前进程认为它已入队。
        with self.log_file.open("ab") as f:
            f.write(encoded)
            f.flush()  # Python 的缓冲数据 -> 操作系统
            os.fsync(f.fileno())  # 请求操作系统同步这个文件的更改

        self.pending.append(message)

    def consume_once(self):
        if not self.pending:
            return None
        # 教学保留上一章的 pop(0) 语义：本进程取出，但磁盘日志不变。
        return self.pending.pop(0)


def save_order(order_id):
    # 仍然用普通文件模拟订单记录；与消息写入不构成原子事务。
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ORDER_FILE.write_text(str(order_id), encoding="utf-8")
    print(f"订单已保存（教学文件）: {order_id}")


def place_order(queue, order_id):
    save_order(order_id)
    message = {
        "messageId": f"msg-{order_id}",
        "type": "OrderCreated",
        "orderId": order_id,
    }
    queue.enqueue(message)
    print(f"消息已写入追加日志: {message['messageId']}")
    return {"status": "success", "orderId": order_id}


def reset():
    for file in (ORDER_FILE, LOG_FILE):
        if file.exists():
            file.unlink()
    print("实验数据已清空")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "inspect"

    if mode == "reset":
        reset()
        return

    queue = FileQueue(LOG_FILE)

    if mode == "produce":
        result = place_order(queue, 1001)
        print(f"下单函数返回: {result}")
        print(f"当前进程队列长度: {len(queue.pending)}")
    elif mode == "inspect":
        order = ORDER_FILE.read_text(encoding="utf-8") if ORDER_FILE.exists() else "不存在"
        print(f"磁盘订单记录: {order}")
        print(f"从日志恢复的消息数: {len(queue.pending)}")
        for msg in queue.pending:
            print(f"恢复的消息: {msg['messageId']}")
    elif mode == "consume":
        message = queue.consume_once()
        if message is None:
            print("队列为空，没有通知任务")
        else:
            print(f"模拟通知已执行: 订单 {message['orderId']}")
            print(f"当前进程队列长度: {len(queue.pending)}")
            print("注意：磁盘消息日志未删除或修改")
    else:
        raise SystemExit(
            "用法: python persistent_message_queue.py "
            "[reset|produce|inspect|consume]"
        )


if __name__ == "__main__":
    main()
```

为避免上次实验残留影响结果，在脚本所在目录依次执行以下命令。**每执行一次 `python ...`，都是一个新的 Python 进程。**

### 实验一：生产消息，然后退出进程

```bash
python persistent_message_queue.py reset
python persistent_message_queue.py produce
```

实际执行输出：

```text
实验数据已清空
订单已保存（教学文件）: 1001
消息已写入追加日志: msg-1001
下单函数返回: {'status': 'success', 'orderId': 1001}
当前进程队列长度: 1
```

此时程序已经退出，`persistent-queue-data/messages.jsonl` 仍留在磁盘上，里面存放着 `msg-1001`。

### 实验二：用新进程恢复

```bash
python persistent_message_queue.py inspect
```

实际执行输出：

```text
磁盘订单记录: 1001
从日志恢复的消息数: 1
恢复的消息: msg-1001
```

对照上一章的 `inspect`：此前新进程队列长度为 `0`，现在变成了 `1`。

我们已经通过两个独立进程验证：**在正常退出和重新启动的场景下，未处理的消息能够从磁盘文件恢复。**

这比单纯看到文件存在更有意义，因为它验证了“写入”和“加载”两条实际代码路径能够接上。

## 到 RocketMQ 5.5.1 源码里看一眼

我们的 Python 代码证明，持久化消息至少需要一条写入路径和一种能够重启后读取的数据形式。

成熟的 RocketMQ 会怎么组织写入呢？现在不需要研究它的全部存储架构，只看**消息进入存储层**的关键交接点。

本系列将以下源码版本固定为阅读基线：

- 仓库：[`apache/rocketmq`](https://github.com/apache/rocketmq)
- Release Tag：[`rocketmq-all-5.5.1`](https://github.com/apache/rocketmq/releases/tag/rocketmq-all-5.5.1)
- Commit：[`efa1be59d6fc747ad220f036c86030f05f0419f9`](https://github.com/apache/rocketmq/commit/efa1be59d6fc747ad220f036c86030f05f0419f9)

先看 [`store/src/main/java/org/apache/rocketmq/store/DefaultMessageStore.java`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/store/src/main/java/org/apache/rocketmq/store/DefaultMessageStore.java) 的 `asyncPutMessage(MessageExtBrokerInner msg)`。

在完成消息合法性检查等前置步骤后，方法会执行下面这一行：

```java
// 真实源码节选：RocketMQ 5.5.1，DefaultMessageStore.asyncPutMessage()。
// 注释为本文添加；代码行保持原样。
CompletableFuture<PutMessageResult> putResultFuture = this.commitLog.asyncPutMessage(msg);
```

这行代码的调用者是 `DefaultMessageStore` 中的消息写入流程；输入 `msg` 是进入存储层的消息对象，而不是我们 Python 示例里的字典。这里把真正的写入工作交给 `CommitLog`，结果以 `CompletableFuture<PutMessageResult>` 的形式继续交给上层处理。原方法随后还会记录耗时和失败次数，最后返回这个 Future。

此时，我们只需要确认一件事：**RocketMQ 的存储入口没有把消息保存在一个普通的临时列表里，而是交给专门的日志存储组件继续处理。** 这一点与我们从内存列表演化到磁盘追加记录的动机是一致的，但**实现方式绝不相同**。

刚才我们还问过“写入与刷盘是不是同一个动作”。继续看 [`store/src/main/java/org/apache/rocketmq/store/CommitLog.java`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/store/src/main/java/org/apache/rocketmq/store/CommitLog.java) 的 `flush()`：

```java
// 真实源码节选：RocketMQ 5.5.1，CommitLog.flush()。
// 注释为本文添加；方法体保持原样。
public long flush() {
    this.mappedFileQueue.commit(0);
    this.mappedFileQueue.flush(0);
    return this.mappedFileQueue.getFlushedWhere();
}
```

这里的 `mappedFileQueue` 管理 RocketMQ 的一组映射文件；`commit(0)`、`flush(0)` 和返回已刷盘位置，说明日志内容的追加、提交与刷盘进度在实现中是需要明确处理的环节。

**特别注意：**这里展示的 `CommitLog.flush()` 并不能单独证明每次发送消息都会同步执行这个方法。真实消息发送的完成时机还受到写入结果、刷盘策略等条件影响。本章只核对“存储层存在专门的日志写入入口，以及明确的刷盘动作”，不提前给出未追踪完整调用链的结论。

我们暂时也不展开 `ConsumeQueue`、文件映射细节或主从复制。等消息读取开始遇到新的限制时，再逐层追进去。

## 一个意外：消息明明处理了，为什么还会出现？

现在，我们已经可以在另一个进程里找回 `msg-1001`。既然能找回来，不妨顺手执行一次通知：

```bash
python persistent_message_queue.py consume
```

实际执行输出：

```text
模拟通知已执行: 订单 1001
当前进程队列长度: 0
注意：磁盘消息日志未删除或修改
```

看起来任务已经完成了。当前进程的列表确实为空。

但请注意：这个 `consume_once()` 仍然只是从**当前进程内存列表**里执行 `pop(0)`。我们从来没有修改过磁盘上的消息日志。

于是，再启动一个新进程：

```bash
python persistent_message_queue.py inspect
```

实际执行输出：

```text
磁盘订单记录: 1001
从日志恢复的消息数: 1
恢复的消息: msg-1001
```

同一条消息又出现了。

这一次，问题与上一章完全相反：

- 上一章只有内存列表，**没处理的消息会丢**。
- 这一章保存了追加日志，**已经处理的消息也会在重启后重新出现**。

我们确实解决了“消息能不能留下来”，却还不知道“消息究竟处理到了哪里”。

有人可能会提议：处理成功后，直接从日志删除那一行不就好了？这当然是一种候选方案。但如果是处理前就删除、执行时发生故障，就可能再次丢失消息；如果等处理后才删除、却恰好在两者之间崩溃，又会出现重复执行的可能。

现在还不要急着引入消息确认、消费位移或重试策略。先记住刚才亲眼观察到的矛盾：**消息是否存在，与消息是否已被成功处理，是两件不同的事情。**

## 回看这次演化

从上一章到现在，我们只往前走了一小步：

```text
第一版：Python 内存列表
    |
    | 进程退出后，未处理消息消失
    v
第二版：把消息追加到磁盘文件
    |
    | 新进程能够重建消息列表
    v
新的矛盾：处理成功后，重启仍会读到旧消息
```

本章的 Python 实验已经实际运行，验证了**正常跨进程恢复**和**重启后再次出现**这两个行为；但没有验证物理断电、文件中途损坏、并发写入或分布式副本恢复。源码部分核对的是 RocketMQ 5.5.1 的写入交接点和刷盘方法，**没有在本章实际部署并运行 RocketMQ**。

下一章，我们仍然追踪 `msg-1001`，先解决一个具体问题：

> **消费者把消息取出来了，系统凭什么知道它已经成功处理？如果处理到一半发生异常，应该怎样重新交付，而不是直接丢弃？**

这时，我们才真正需要讨论消费完成状态与消息确认。
