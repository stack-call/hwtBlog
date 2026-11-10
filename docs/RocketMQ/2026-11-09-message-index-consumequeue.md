---
slug: /message-queue/message-index-consumequeue
title: 消息越来越多，为什么增加消费者也不一定更快？
sidebar_position: 7
description: 延续订单 1001 的通知消息，从每次领取都扫描整份日志的真实瓶颈出发，亲手实现定长消息索引，逐层对照 RocketMQ 5.5.1 的 ConsumeQueue 与 CommitLog 读取源码。
tags: [消息队列, RocketMQ, 源码精读, CommitLog, ConsumeQueue, 消息索引]
---

# 消息越来越多，为什么增加消费者也不一定更快？

上一章，我们给订单 `1001` 的通知消息 `msg-1001` 加上了原子领取、不可见时间和带凭证的 ACK。两个消费者即使同时工作，也不会在正常的领取窗口里都成功领取同一条消息；消费者崩溃后，消息还可以重新交付。

看起来，只要继续启动更多消费者，就能处理更多订单通知。

但先不要启动第三个消费者。把订单量从 2 条增加到 400 条，再回头看上一章的 `LeasedQueue.claim()`，我们会发现一个更早、更确定的问题：**即使这一次只需要取出一条消息，它也会先读取并解析整份消息日志。**

本章只解决这个问题：**已经知道自己要读哪条消息时，能不能不再从头扫描所有消息？** 我们先让旧代码的代价变得可观察，再给它增加一个最小的定位机制；最后检查 RocketMQ 的真实存储层是不是也沿着同样的需求做了设计。

<!-- truncate -->

## 旧方案仍然正确，只是付出了不必要的读取成本

先回到第五章的教学代码。它已经能在两个进程之间原子领取消息，但在真正竞争领取资格之前，做了下面这件事：

```python title="shared_claim_queue.py / LeasedQueue.claim()（上一章教学源码）"
messages = read_json_lines(MESSAGE_LOG)

conn.execute("BEGIN IMMEDIATE")
for message in messages:
    # 检查共享状态，尝试领取……
    ...
```

这段代码为什么当时合理？因为第五章只有 `msg-1001`、`msg-1002` 两条消息。先把文件读成列表，再逐条判断是否可领取，几乎不用考虑读取开销；我们也因此可以把注意力集中在「领取是否原子」上。

但 `read_json_lines()` 会**完整读取并解析**日志。即使第一条消息就是可领取的 `msg-1001`，也要先解析后面的所有消息。

把这个行为放到订单通知案例里：

```text
messages.jsonl 有 400 条通知消息
             |
             v
消费者只想领取 msg-1001
             |
             v
read_json_lines() 解析全部 400 条
             |
             v
才开始判断 msg-1001 是否可领取
```

处理一条消息要解析 400 条，处理下一条又重新解析 400 条。这里不是说读取成本一定已经超过业务处理成本，而是说我们能指出一份**与实际领取数量无关的重复工作**。即使再增加消费者，每个人也仍然会重复做这件事。

### 不凭感觉说“慢”：给旧路径计数

本章资料包里的 `indexed_message_queue.py` 保留了旧路径 `claim_scan()`。它仍然逐行解析 JSON，并且用 SQLite 保存已经领取、已确认、不可见时间等状态。我们只给「解析了多少条消息」增加计数。

```python title="indexed_message_queue.py / _scan_all()（教学代码节选）"
def _scan_all(self, metrics):
    with self.log_path.open("rb") as log:
        messages = []
        for raw in log:
            messages.append(json.loads(raw))
            metrics.decoded_messages += 1
        return messages
```

注意，这个计数不是性能测试；它不测磁盘吞吐、CPU 时间，也没有模拟网络延迟。我们只是让旧方案每次重复做了什么变得清清楚楚。

## 我们真正缺少的不是第三个消费者，而是消息的位置

假设 `msg-1001` 已经在文件开头，`msg-1002` 紧随其后，`msg-1003` 再往后。

实际上，磁盘文件当然知道它们各自处在哪个字节位置，只是前面的程序没有保存这些位置。

我们可以在写入每条消息时，顺手记下两件事：**它在消息日志中的起始字节位置，以及这条消息占多少字节。**

例如，用三条长度均为 62 字节的教学消息做实验：

```text
消息日志 messages.jsonl：

字节 0               62                124
 |--------------------|-------------------|-------------------|
 | msg-1001           | msg-1002          | msg-1003          |
 |--------------------|-------------------|-------------------|

另外保存一份“位置表”：

逻辑位置 0 -> 日志字节位置 0   ，长度 62
逻辑位置 1 -> 日志字节位置 62  ，长度 62
逻辑位置 2 -> 日志字节位置 124 ，长度 62
```

这时才需要引入一个新概念：**消息索引**。它不是存储消息正文，而是帮助我们定位消息正文。

为了区分两个容易混淆的数字，我们现在给它们起名字：

- **逻辑位置（logical offset）**：这是本教学队列里第几条消息，从 0 开始。
- **物理位置（physical byte offset）**：这条消息在消息日志文件中从第几个字节开始。

它们不是同一个东西。第 2 条消息的逻辑位置是 `1`，但物理位置是 `62`。今后消息长度不同，两者之间更不会存在简单的固定比例。

有了这张表，想读第 2 条消息，就不再需要从第一条开始解析，可以直接跳到物理位置 `62` 读取指定长度的字节。

### 第一版可以用字典，为什么实验还要写定长文件？

最小可用方案当然可以是：

```python title="概念伪代码：只强调定位思想"
positions = {0: (0, 62), 1: (62, 62), 2: (124, 62)}
start, size = positions[1]
message_bytes = read_at(messages_file, start, size)
```

这一步在单进程里完全有效。但我们的消息必须在进程重启后仍可找回；如果索引只存在内存，就要在每次重启时重新扫描消息日志，才能恢复所有位置。

所以，本章再做**一个直接服务于同一目标的小改动**：把位置表保存为一个定长的二进制文件。每个条目大小相同，便能用 `逻辑位置 × 条目长度` 直接算出该条目在哪里。

为了让它稍后能够对照 RocketMQ 的经典文件式 `ConsumeQueue`，教学文件每条记录也使用 20 字节：

| 字段 | 字节数 | 本章用途 |
|---|---:|---|
| 日志物理位置 | 8 | 消息正文从哪里开始 |
| 消息长度 | 4 | 读取多少字节 |
| 标签哈希预留位 | 8 | 教学中固定写 0，暂不使用 |

这**不是 RocketMQ 文件格式的兼容实现**：我们用 Python JSONL 保存正文，二进制编码、读写和恢复逻辑都做了教学简化。之所以选择 20 字节，是为了让读者更容易对照它的设计目的，不是为了声称可以用这个文件启动真实 RocketMQ。

## 代码只改一个关键动作：写消息时记录位置

生产订单通知时，我们先让原来的追加日志继续承担「存储消息正文」的任务。

```python title="indexed_message_queue.py / publish_many()（教学代码节选）"
position = log.tell()
log.write(record)
idx.write(ENTRY.pack(position, len(record), 0))
```

`log.tell()` 是本次消息开始前的日志字节位置；`len(record)` 是写入的字节数；`ENTRY.pack(...)` 把这三个字段写进索引。**消息正文仍然只存在 `messages.jsonl` 里**，索引不负责保存完整内容。

在这个阶段，新系统只有两份与消息定位有关的数据：

```text
写入通知消息
      |
      v
messages.jsonl   ← 完整消息正文
      |
      +---- 记录物理位置和长度
                   |
                   v
             message_positions.cq
             ← 20 字节定长条目
```

这里没有必要提前引入分区、副本、消费者组再均衡。它们都解决不了当前「必须扫描所有消息才能定位某一条」的基本问题。

## 真正读取时，索引到底省掉了什么？

过去，每次调用 `claim()` 都先把 `messages.jsonl` 全部解析出来。现在，我们把领取分成两个小步骤：

第一步仍然保留上一章的共享消费状态：在 SQLite 中确定**哪一个逻辑位置现在可以领取**，并在同一个写事务内标记本次领取。不可见时间和 ACK 都继续生效。

第二步有了逻辑位置 `seq` 以后，不再读取整个日志，而是：

```python title="indexed_message_queue.py / _read_indexed()（教学代码节选）"
with self.index_path.open("rb") as idx:
    idx.seek(seq * ENTRY.size)
    raw = idx.read(ENTRY.size)
position, length, _tag_hash = ENTRY.unpack(raw)

with self.log_path.open("rb") as log:
    log.seek(position)
    body = log.read(length)
```

这段程序的输入是一个已经选定的逻辑位置 `seq`。它先到索引里读取**一个条目**，再利用条目中的物理位置到消息文件读取**一条正文**，最后才解析 JSON 并返回给消费者。

我们没有重新设计 ACK，也没有允许两个消费者不经过事务就同时领取同一条消息。**本章只替换消息定位的方式。**

```text
上一章：

寻找可领取消息
      |
      v
解析完整消息文件
      |
      v
逐条检查领取状态
      |
      v
返回一条消息

本章：

从共享消费状态选出可领取的逻辑位置
      |
      v
读取对应的 20 字节索引条目
      |
      v
到消息日志的物理位置直接读取正文
      |
      v
返回一条消息
```

注意：**索引解决的是「如何定位消息正文」，不是「哪些消息可以被领取」。** 本章教学程序用 SQLite 的消费状态索引做后者，消息位置文件做前者；这两层不要混为一谈。

### 实际执行一次定位实验

将 `indexed_message_queue.py` 放入独立目录（Python 3.9+，只使用标准库），运行：

```bash
python indexed_message_queue.py explain
```

本次实际执行输出：

```text
逻辑位置=0 -> 索引字节位置=0 -> 日志字节位置=0 -> 长度=62 -> 消息=msg-1001
逻辑位置=1 -> 索引字节位置=20 -> 日志字节位置=62 -> 长度=62 -> 消息=msg-1002
逻辑位置=2 -> 索引字节位置=40 -> 日志字节位置=124 -> 长度=62 -> 消息=msg-1003
```

我们已经能看到：`msg-1002` 不需要再从 `msg-1001` 的 JSON 文本开始解析，只需读取第 1 个逻辑位置对应的索引条目，拿到物理位置 `62`。

## 到这里再进入 RocketMQ 5.5.1 源码

现在才有理由看 RocketMQ 的两个名字：`CommitLog` 和 `ConsumeQueue`。

本章固定源码版本为 **Apache RocketMQ 5.5.1**，固定 Commit 为 [`efa1be59d6fc747ad220f036c86030f05f0419f9`](https://github.com/apache/rocketmq/commit/efa1be59d6fc747ad220f036c86030f05f0419f9)。只分析经典**文件式 ConsumeQueue** 对应的普通存储读取路径；RocketMQ 5.x 还存在 BatchConsumeQueue、RocksDB ConsumeQueue 等实现与配置分支，不能用这篇文章代替所有路径。

### 先看索引本身：为什么一条恰好是 20 字节？

源码位置：[`store/src/main/java/org/apache/rocketmq/store/ConsumeQueue.java`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/store/src/main/java/org/apache/rocketmq/store/ConsumeQueue.java#L45-L60)

类 `ConsumeQueue` 明确规定了一个固定的条目大小：

```java title="ConsumeQueue.java（RocketMQ 5.5.1 真实源码节选）"
public static final int CQ_STORE_UNIT_SIZE = 20;
```

这一类开头的注释将条目分为 8 字节的 CommitLog 物理位置、4 字节的长度、8 字节的 Tag 哈希。它的作用不是再保存一次消息正文，而是为一个 Topic 中的某个队列组织**逻辑消费索引**。

这时读者已经理解为什么需要「位置 + 长度」；多出的 Tag 哈希先知道它用于过滤相关处理就足够了，本章不会展开消息过滤。

### 再看读取一个逻辑位置：怎么找到索引条目？

还是同一文件：[`ConsumeQueue.getIndexBuffer()`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/store/src/main/java/org/apache/rocketmq/store/ConsumeQueue.java#L851-L861)。它的输入 `startIndex` 是逻辑读取位置，最关键的一行是：

```java title="ConsumeQueue.java / getIndexBuffer()（真实源码节选）"
long offset = startIndex * CQ_STORE_UNIT_SIZE;
```

是不是很眼熟？这和刚才我们在 Python 中写的 `seq * ENTRY.size` 解决的是同一个问题：**逻辑位置可以直接换算成索引文件中的字节位置。**

接下来源码调用 `mappedFileQueue.findMappedFileByOffset(offset)` 找到相应的映射文件，再通过 `selectMappedBuffer(...)` 获得该处数据。实际文件管理比我们的单个 `.cq` 文件复杂，但这一小段已经证明了「定长条目使位置可计算」的核心设计。

调用者是谁？`ConsumeQueue.iterateFrom(startOffset)` 会用 `getIndexBuffer(startOffset)` 创建迭代器，把按逻辑位置读取到的索引记录交还给消息读取路径。此处返回的**是索引条目迭代器，不是完整的业务消息正文**。

### 最后追到正文：谁拿索引结果去读 CommitLog？

源码位置：[`DefaultMessageStore.getMessage()`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/store/src/main/java/org/apache/rocketmq/store/DefaultMessageStore.java#L796-L905)。

这层的调用者是 Broker 的消息读取流程；传入消费组、Topic、队列 ID 和消费位置等参数后，存储层需要找到与这次请求对应的消息数据。

在普通读取分支中，`getMessage()` 先找到对应的 `ConsumeQueue`，然后迭代索引记录。拿到其中的物理位置和长度之后，才真正读取消息正文：

```java title="DefaultMessageStore.java / getMessage()（真实源码节选）"
ConsumeQueueInterface consumeQueue = findConsumeQueue(topic, queueId);
```

随后迭代得到 `CqUnit`，取出 `getPos()`、`getSize()`，再调用：

```java title="DefaultMessageStore.java / getMessage()（真实源码节选）"
SelectMappedBufferResult selectResult = this.commitLog.getMessage(offsetPy, sizePy);
```

注意我们没有把两句拼接成一个假装连续的完整函数：中间还有边界校验、批量控制、过滤、缺失数据处理等逻辑，本章只截取「定位索引 → 获得物理位置 → 读取正文」的证据。

我们现在可以从这两份源码准确地还原出一个局部读取链：

```text
Broker 发起存储层读取
            |
            v
DefaultMessageStore.getMessage(...)
            |
            v
findConsumeQueue(topic, queueId)
            |
            v
ConsumeQueue.iterateFrom(逻辑位置)
            |
            v
索引条目 CqUnit：物理位置、消息长度
            |
            v
CommitLog.getMessage(物理位置, 长度)
            |
            v
返回读取结果给上层
```

这一章没有研究客户端到底如何发送拉取请求、POP 与传统拉取的服务端分支如何选择；我们只取到了**存储读取层这一条可核实的实际调用链**。这也是渐进式阅读源码的好处：目前只需要证明刚刚引入的机制确实存在，不必为了完整而把所有组件一次性端出来。

### 索引从哪里来：是消费者读的时候才计算的吗？

如果每次读取时还要扫描 `CommitLog` 再算出位置，那我们只是把原来的开销藏起来了。

所以，还有一个紧贴当前设计的问题：位置索引应该在什么阶段建立？

RocketMQ 在 `DefaultMessageStore` 中注册了 `CommitLogDispatcherBuildConsumeQueue`。在相关消息类型的分发路径里，它会调用 `putMessagePositionInfo(request)`，再交给 `consumeQueueStore.putMessagePositionInfoWrapper(dispatchRequest)`。这说明位置索引的建立处于消息存储后的**分发/构建过程**中，不是每个消费者读取时才临时从头扫描 `CommitLog`。相关的 `ReputMessageService` 和分发回调负责推动处理过程。

源码：[`DefaultMessageStore.java / CommitLogDispatcherBuildConsumeQueue`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/store/src/main/java/org/apache/rocketmq/store/DefaultMessageStore.java#L2051-L2065) · [`putMessagePositionInfo()`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/store/src/main/java/org/apache/rocketmq/store/DefaultMessageStore.java#L1891-L1903)。

不过这里不能得出「消息写入成功的同一瞬间，所有消费索引就同步且原子地完成持久化」这一过强结论。生产、分发、刷盘与恢复之间还有时序和一致性问题，本章暂不展开。

## 真实运行：把重复解析次数摆出来

执行：

```bash
python indexed_message_queue.py compare --messages 400 --claims 40
```

实验程序先生成 400 条不同订单的通知消息，并建立位置索引。第一轮使用旧的全量扫描方式成功领取并 ACK 前 40 条；随后仅为了对照实验清空消费状态，第二轮用位置索引处理相同的 40 条消息。两轮的 `messageId` 序列必须一致，否则实验判失败。

本次实际输出：

```text
消息总数: 400；已处理: 40；首条: msg-1001
旧路径：JSON 解析次数=16000，索引读取次数=0
新路径：JSON 解析次数=40，索引读取次数=40
两条路径处理的 messageId 序列一致：True
注意：以上是操作计数，不是吞吐量或耗时性能测试。
```

旧路径的 `16000` 不是猜出来的：`40 次领取 × 每次解析 400 条 = 16000 次 JSON 解析`。新路径每次领取只解析对应的一条消息正文，因而是 40 次。

从这个实验我们能确认：**在同一批消息、同一个领取与 ACK 规则下，按位置索引读取可以消除每次领取都重复解析整份日志的问题。**

我们还不能据此声称 RocketMQ 比 Python 快多少倍，也不能说增加消费者后吞吐必然翻倍。SQLite 查找可领取消息、数据库写锁、文件系统缓存与业务通知耗时，都不在这份计数里。

## 新方案有没有留下新的风险？

当然有，而且这次风险正是从「多维护一份位置索引」自然产生的。

假设生产者按下面的顺序操作：

```text
1. 消息正文已经写入 messages.jsonl
2. 正准备写入 message_positions.cq
3. 进程崩溃
```

这时消息正文可能存在，但索引缺少它。若仍然只靠索引消费，就可能看不到这条消息；反过来，索引指向的正文如果不完整，也不应被当成有效消息返回。

本章的教学程序只检查了「索引条目是否截断」「读取长度是否正确」「消息 ID 是否符合当前教学数据」等可见错误；**没有实现跨文件与 SQLite 的原子提交，没有实现自动重建索引，也没有保证异常断电下的完整恢复**。这些是下一步研究真实存储恢复机制时应当严肃解决的问题，而不是用一句「已经 fsync」就能略过的细节。

另外，**索引不是消费确认记录**。同一条通知可以在 `CommitLog` 中长期存在，而消费组的处理状态会变化；RocketMQ 的 `ConsumeQueue` 本身也不能等同于我们 SQLite 中保存的 `acked`、`receipt_token`、`invisible_until`。第五章研究的 POP 确认机制仍然负责自己的消费状态逻辑。

## 用测试守住我们已经掌握的东西

运行：

```bash
python -m unittest -v test_indexed_message_queue.py
```

本章实测 7 项测试全部通过，覆盖：

1. 20 字节定长条目和索引位置换算。
2. 100 条消息、7 次领取，两条读取路径获得相同消息但解析次数不同。
3. 已领取消息在不可见时间内不可被重新领取，同时其他消息仍可领取。
4. 单条消息到期重新交付，旧凭证 ACK 失败，新凭证 ACK 成功。
5. 扫描路径与索引路径共用同一份 ACK 状态。
6. 索引文件被截断时明确报错，而不是返回伪造的正文。
7. 重建 Python 对象后仍可读取已有的消费确认状态。

这些都是本地 Python 与 SQLite 实验，没有启动 RocketMQ Broker，没有真实网络消费，也没有进行生产集群性能测量。

## 现在再看我们是怎样走到这里的

```text
最初：一条消息放在内存列表
             |
             v
程序重启会丢 → 追加到磁盘消息日志
             |
             v
处理状态会丢 → 增加消费确认
             |
             v
故障会重投 → 业务幂等
             |
             v
多人会同时领取 → 共享原子领取与不可见时间
             |
             v
消息太多，每次领取都解析整份日志
             |
             v
保存逻辑位置与物理位置的映射
             |
             v
ConsumeQueue 式定位：索引找位置，日志找正文
```

到现在，我们终于有了一条相对高效的**单条消息定位路径**。但把程序放大到更多消费者，仍然有一个没有解决的问题：在当前教学实现里，无论消费者想领取哪条消息，都要竞争**同一个 SQLite 写事务**，也要共享同一套待领取状态。

消息正文不用全量扫描了，并不等于所有消费者都能无限并行。

下一章，我们就让两个消费者分别处理大量订单通知，观察同一个协调点怎样限制并行，再去寻找最小的拆分方法：**既然不同订单的消息可以分别处理，为什么所有领取操作都必须争用同一个工作范围？** 这时，我们才真正有理由研究 RocketMQ 的多个消息队列与并行消费。
