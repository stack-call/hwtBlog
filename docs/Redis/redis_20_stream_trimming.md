---
title: "第二十章：消息不断积累怎么办？从 Stream 保留策略到 XTRIM"
sidebar_label: "20 - Streams 保留与裁剪"
sidebar_position: 21
description: "从 XACK 后消息仍在的现象出发，沿 Redis 7.2.0 的 xtrimCommand、streamTrim 与 xaddCommand 源码，理解精确裁剪、近似裁剪和 PEL 引用的可靠性边界。"
tags:
  - Redis
  - 源码精读
  - Streams
  - XTRIM
  - XADD
  - 消费者组
---

# 第二十章：消息不断积累怎么办？从 Stream 保留策略到 XTRIM

上一章，我们解决了活动报名事件被重复交付时，如何避免重复发放积分的问题。

消费者从 `activity:1001:events` 中读取一条报名事件，更新业务数据库，确认业务已成功或曾经成功，然后执行 `XACK`：

```redis
XACK activity:1001:events points 1700000000000-0
```

这条命令让 Redis 不再把对应消息视为**这个消费者组里的未确认任务**。但现在有一个容易忽略的事实：**`XACK` 并不会删除 Stream 中的消息正文。**

假设活动每天产生十万条报名事件，即使所有消费者都及时完成处理，消息历史仍然会不断增加。

于是，新的问题不再是“消息能不能被可靠处理”，而是：

> **怎样限制 Stream 的历史数据规模，同时不让尚未处理的消息被提前清除？**

本章只围绕这个问题展开。我们将先用最小实验区分“确认”和“删除”，然后引出 `XTRIM` 与 `XADD MAXLEN`，最后从 Redis **7.2.0** 源码解释裁剪的执行方式与代价。

> 版本约定：所有真实源码入口均固定为 [Redis 7.2.0](https://github.com/redis/redis/tree/7.2.0)。实验中的输出为根据该版本语义推导的**预期结果**；本文编写环境未运行 Redis 实例。尤其注意：Redis 8.2 新增的 `KEEPREF`、`DELREF`、`ACKED` 裁剪选项**不适用于 Redis 7.2.0**。

## 1. 我们明明已经 XACK，消息为什么还在？

先只保留活动报名积分这一个场景：用户 `42` 报名成功，积分服务需要处理一次加分。

假设 Stream 中保存了三条事件：

```text
activity:1001:events
  1-0  signup:1001:42:v1
  2-0  signup:1001:43:v1
  3-0  signup:1001:44:v1
```

为了便于观察，我们给消息使用固定、递增的 Stream ID。它们仅用于本地实验；生产环境通常让 `XADD` 使用 `*` 自动生成 ID。

首先创建 Stream 和消费者组：

```redis
DEL activity:1001:events

XADD activity:1001:events 1-0 event_id signup:1001:42:v1 user_id 42
XADD activity:1001:events 2-0 event_id signup:1001:43:v1 user_id 43
XADD activity:1001:events 3-0 event_id signup:1001:44:v1 user_id 44

XGROUP CREATE activity:1001:events points 0-0
```

由消费者读取第一条消息：

```redis
XREADGROUP GROUP points worker-a COUNT 1 STREAMS activity:1001:events >
```

应当返回 ID `1-0` 对应的事件。此时：

```redis
XPENDING activity:1001:events points
```

显示还有 **1 条待确认记录**。

假设积分已经在数据库中正确提交，现在执行：

```redis
XACK activity:1001:events points 1-0
```

返回：

```text
(integer) 1
```

再检查：

```redis
XPENDING activity:1001:events points
XLEN activity:1001:events
XRANGE activity:1001:events - +
```

我们预期看到的是：

- `XPENDING` 中待确认消息数量变为 **0**；
- `XLEN` 仍然是 **3**；
- `XRANGE` 仍然能读到 `1-0`、`2-0`、`3-0` 三条事件。

为什么？因为从上一章到这一章，有两个东西始终不能混为一谈：

```text
Stream 消息本体                   消费者组 PEL
┌────────────────────┐         ┌──────────────────┐
│ 1-0  报名事件 42   │         │ 1-0 → worker-a   │
│ 2-0  报名事件 43   │         └──────────────────┘
│ 3-0  报名事件 44   │                  │
└────────────────────┘                  │ XACK 1-0
         │                             v
         │                     PEL 中移除 1-0
         │
         └── 消息正文仍在
```

在 Redis 7.2.0 的 `src/t_stream.c` 中，`xackCommand()` 查找并删除组级 PEL 与消费者级 PEL 中的记录，而不是删除 Stream 数据。其关键操作分别针对：

```c
/* 真实源码节选：xackCommand() 中的两次索引删除 */
raxRemove(group->pel,buf,sizeof(buf),NULL);
raxRemove(nack->consumer->pel,buf,sizeof(buf),NULL);
```

这两行代码就是我们实验现象的证据：**消息确认和消息历史保留是两种独立操作。**

但如果它们独立，怎样清理已经不需要的历史事件？

## 2. 最直接的方案：消费成功后立即删除消息

既然 `XACK` 不删除正文，我们也许可以在每次处理成功后再执行：

```redis
XDEL activity:1001:events 1-0
```

对于只有一个积分服务、且不再需要历史回放的最简单系统，这看起来可行。

但我们之前已经引入了消费者组，意味着同一条报名事件可能同时被**积分服务**和**通知服务**处理。

假设它们分别属于两个组：

```text
Stream 1-0：用户 42 报名成功
           |
           +----> points 组：已经完成加分
           |
           +----> notify 组：还没发送通知
```

如果积分服务在完成时直接 `XDEL 1-0`，通知组就失去了之后读取该消息正文的机会。

因此，**一个消费者处理完成，不代表所有关心这条事件的消费者都处理完成。**

我们现在应当把问题分成两部分：

1. Redis 怎样高效删除一批旧消息？
2. 应用怎样判断这些消息什么时候可以安全删除？

Redis 提供了第一部分需要的机制，但第二部分不能简单地交给某个消费者的 `XACK` 决定。

下面先解决第一个问题。

## 3. 只保留最新 N 条：XTRIM 的最小模型

对于某些活动事件，我们的业务可能允许只保留最近一段历史。

比如这条消息流只是用于实时统计看板，允许保留最新三条事件，老事件会被其他存储系统归档。

最直觉的办法是：消息增加后，如果数量超过三条，就不断删除最旧的消息。

概念伪代码如下：

```python
# 概念伪代码：不代表 Redis 真实实现
while stream_length() > 3:
    delete_oldest_entry()
```

Redis 提供了 `XTRIM` 完成这种裁剪。

先建立一个没有消费者组的独立实验流：

```redis
DEL lab:trim
XADD lab:trim 1-0 event signup user_id 41
XADD lab:trim 2-0 event signup user_id 42
XADD lab:trim 3-0 event signup user_id 43
XADD lab:trim 4-0 event signup user_id 44
XADD lab:trim 5-0 event signup user_id 45
```

确认长度：

```redis
XLEN lab:trim
```

预期返回 `5`。

现在执行：

```redis
XTRIM lab:trim MAXLEN = 3
```

预期返回：

```text
(integer) 2
```

再执行：

```redis
XLEN lab:trim
XRANGE lab:trim - +
```

此时应只剩下 `3-0`、`4-0`、`5-0`。

这就解释了 `MAXLEN` 的基本作用：

```text
裁剪前：
1-0 → 2-0 → 3-0 → 4-0 → 5-0

XTRIM ... MAXLEN = 3

裁剪后：
            3-0 → 4-0 → 5-0
```

`MAXLEN` 不是按消息的业务状态删除，而是按照 **Stream 中消息的先后顺序**，从较旧的消息开始裁剪。

这里的 `=` 表示精确裁剪：在没有其他特殊限制的前提下，最终条目数量不超过指定阈值。

现在问题进一步变成：Redis 为什么能从“最旧的一端”有效地删除历史消息？

## 4. 从命令入口开始：xtrimCommand() 做了什么？

源码位置：[Redis 7.2.0 `src/t_stream.c`](https://github.com/redis/redis/blob/7.2.0/src/t_stream.c)。

通过前几章，我们已经知道一条客户端命令如何进入 `processCommand()`，再由命令分发调用实际的处理函数。

对于 `XTRIM`，实际入口是：

```c
/* 真实源码：函数签名 */
void xtrimCommand(client *c)
```

它所做的事情可以拆成三个连续动作。

首先，解析 `MAXLEN`、`MINID`、`=`、`~`、`LIMIT` 等参数：

```c
/* 真实源码中的调用语句 */
streamParseAddOrTrimArgsOrReply(c, &parsed_args, 0)
```

接着，根据 Key 查找对应的 Stream 对象；如果 Key 不存在，则返回删除数 `0`，如果类型不正确，则返回类型错误。

然后执行最关键的一步：

```c
/* 真实源码节选 */
int64_t deleted = streamTrim(s, &parsed_args);
```

最后，函数为发生实际删除的情况记录变更，并通过 `addReplyLongLong()` 返回删除条目数量。

因此，`xtrimCommand()` 并不自己遍历和删除每条消息。

它负责把用户请求转成裁剪参数，再将具体操作交给 `streamTrim()`：

```text
XTRIM lab:trim MAXLEN = 3
        |
        v
xtrimCommand()
        |
        | 解析 MAXLEN、精确/近似标记
        v
streamParseAddOrTrimArgsOrReply()
        |
        | 找到 Stream 对象
        v
streamTrim()
        |
        | 从旧消息开始裁剪
        v
返回实际移除的条目数
```

现在，剩下的核心问题就是 `streamTrim()` 怎么做。

## 5. 为什么不能把 Stream 当成一个普通数组？

我们最早学习 List 时已经看到，Redis 会根据数据规模选择不同的内部表示。

Stream 也不是一个简单的“每条消息都是独立节点”的链表。

在 Redis 7.2.0 的 `src/stream.h` 中，`stream` 对象包含：

```c
/* 真实源码节选：stream 结构体中与本章有关的字段 */
rax *rax;
uint64_t length;
streamID first_id;
streamID last_id;
```

这里先只关心 `rax` 和 `length`。

`rax` 是 Redis 使用的基数树（radix tree）。Stream 通过它索引多个紧凑存储节点，每个节点内部又使用 `listpack` 存放一批相邻的消息。

我们可以用下面这张**教学示意图**理解：

```text
Stream
  |
  v
radix tree
  |
  +----> listpack A：消息 1-0、2-0
  |
  +----> listpack B：消息 3-0、4-0
  |
  +----> listpack C：消息 5-0、6-0
```

图中的“每个 listpack 两条消息”只是演示，不代表 Redis 的固定大小。

现在想想，如果需要删除最前面的两条消息，我们有两种做法。

第一种，进入 listpack 内部，逐条标记或处理消息。

第二种，如果整个 listpack 都已经不需要了，直接释放这个节点。

显然，当一个节点中所有消息都应该删除时，整体释放往往更省事。

Redis 的 `streamTrim()` 正是围绕这种差别设计的。

## 6. 进入 streamTrim()：先尝试删除整块旧数据

源码位置：[src/t_stream.c 中的 `streamTrim()`](https://github.com/redis/redis/blob/7.2.0/src/t_stream.c#L647-L815)。

我们沿着 `XTRIM ... MAXLEN = 3` 的情况走。

`streamTrim()` 首先从 Stream 的开头创建迭代器，按较旧的节点向后遍历。

对于 `MAXLEN`，如果当前长度已经达到要求，就停止继续删除。

接下来，它会检查：当前最旧节点里的消息是否能**全部**删除，而不让剩余消息数量低于目标。

其关键判断可以概括为：

```c
/* 教学化简：MAXLEN 分支 */
remove_node = (current_length - node_entries >= maxlen);
```

如果可以整块删除，Redis 会释放该 listpack，并从基数树中移除对应节点。

这里的目标是减少逐条处理和重复调整内部结构的成本。

但不是每次都刚好能删掉整个节点。

比如，假设当前有五条消息，裁剪后希望保留三条，而最旧节点内部保存了四条。

这时，删除整个节点会只剩一条，显然删得太多。

因此，精确裁剪还需要进入当前节点内部，逐条处理旧消息。

Redis 使用 `STREAM_ITEM_FLAG_DELETED` 标记部分被裁剪的条目，同时维护 Stream 的有效条目数量；一整个节点都不再需要时，才可以直接释放整个节点。

这引出了两个区别：

**第一，`XLEN` 统计的是仍然有效的 Stream 条目数。**

**第二，`XLEN` 减少，不保证内存占用按相同的比例立刻下降。**

因为当只删除某个 listpack 的一部分消息时，底层容器可能仍然保留部分已标记删除的内容。

因此，不能仅凭 `XTRIM` 返回删除了多少条，就推断实际释放了多少字节的内存。

现在我们已经理解精确裁剪的必要成本：它有时必须深入节点内部逐条修改。

那么，如果我们的业务并不要求 Stream **恰好**保留三条，能不能省去这部分工作？

## 7. 为什么 Redis 还提供近似裁剪？

假设业务的要求不是“严格保留三条消息”，而是“尽量把消息量控制在大约一千条”。

那么，没有必要为精确的数字付出额外的节点内部修改成本。

Redis 提供了 `~`：

```redis
XTRIM lab:trim MAXLEN ~ 1000
```

这里的 `~` 表示**近似裁剪**。

在 Redis 7.2.0 的 `streamTrim()` 中，处理完是否能移除整个节点的判断后，有一条非常关键的分支：

```c
/* 真实源码节选 */
if (approx) break;
```

这行代码发生在“当前节点不能被完整删除”的情况下。

它表达的设计非常直接：

> 如果要求近似裁剪，那么就不要为了凑出精确长度，进入当前 listpack 逐条删除。

可以整块删除，就删除；不能整块删除，就停止。

这样通常可以减少裁剪时对底层结构的修改工作。

代价也很明确：**最终保留的消息条数可能高于 `MAXLEN` 指定的目标。**

例如：

```text
MAXLEN = 3       近似裁剪

旧节点 A：2 条
旧节点 B：2 条
新节点 C：2 条

总计 6 条

可以删除 A，剩余 4 条。
再删除 B 将只剩 2 条，低于目标 3。
因此停止，可能保留 4 条。
```

这是理解策略的**假设示意**，不是对具体 Redis 数据布局的实验输出。

Redis 还支持 `LIMIT`，用于限制一次近似裁剪最多进行多少工作。当达到限制时，也可能在尚未达到理想保留规模时提前停止。

因此，“近似”不是“每次裁剪出一个随机结果”，而是一个明确的性能取舍：

**允许保留略多一些的消息，换取尽量按整块节点操作、减少处理成本。**

这里仍然有个实际问题。

如果生产者不断向 Stream 中写入新事件，难道我们必须另外启动一个定时任务，周期性执行 `XTRIM` 吗？

## 8. 把裁剪放在写入时：XADD MAXLEN

一种直接的设计是：每次追加新消息后，顺便检查需要保留多少历史。

Redis 已经提供了这样的接口：

```redis
XADD activity:1001:events MAXLEN ~ 1000 * event_id signup:1001:46:v1 user_id 46
```

它表示：追加一条新消息，并尝试按约一千条的目标裁剪旧消息。

如果需要精确限制，也可以使用：

```redis
XADD activity:1001:events MAXLEN = 1000 * event_id signup:1001:46:v1 user_id 46
```

现在对照源码。

在 Redis 7.2.0 的 `src/t_stream.c` 中，`xaddCommand()` 首先解析附加参数，然后调用 `streamAppendItem()` 完成写入。

紧接着，它检查调用者是否指定了裁剪策略：

```c
/* 真实源码节选 */
if (parsed_args.trim_strategy != TRIM_STRATEGY_NONE) {
    if (streamTrim(s, &parsed_args)) {
        notifyKeyspaceEvent(NOTIFY_STREAM,"xtrim",c->argv[1],c->db->id);
    }
}
```

这说明 `XADD MAXLEN` 并不是一套与 `XTRIM` 完全不同的算法。

它是在**同一条 XADD 命令**中，先追加事件，再调用同一个 `streamTrim()` 执行裁剪。

因此我们可以把两种接口理解为：

```text
XTRIM：由业务单独发起一次历史裁剪

XADD MAXLEN：追加消息时顺便触发历史裁剪
                     |
                     v
                 streamTrim()
```

对简单的保留策略而言，`XADD MAXLEN` 通常更容易使用，因为生产者不必另外协调“写入成功后再发送裁剪命令”。

但这里也要明确：`XADD MAXLEN ~ 1000` 的目标是**大约保留一千条**，并不是对 Redis 内存使用量设置严格字节上限。

而且，这个功能仍然只解决“怎样裁剪消息数据”，没有解决“哪条消息在业务上可以安全裁剪”。

这时，我们需要回到上一章刚刚建立的 PEL 机制。

## 9. 一个危险的反例：PEL 还在，消息正文却被裁剪了

假设消费者 `worker-a` 已经收到报名事件 `1-0`，但它在业务数据库提交之前崩溃了。

Redis 的 PEL 仍然记着这条未确认任务。

按照前面学习的故障恢复机制，其他消费者本应通过 `XCLAIM` 或 `XAUTOCLAIM` 接管它，再尝试完成业务。

但是，如果我们在此期间执行了过于激进的 `XTRIM` 呢？

使用一个全新的实验流复现：

```redis
DEL lab:danger

XADD lab:danger 1-0 event_id signup:1001:42:v1 user_id 42
XADD lab:danger 2-0 event_id signup:1001:43:v1 user_id 43

XGROUP CREATE lab:danger points 0-0

XREADGROUP GROUP points worker-a COUNT 1 STREAMS lab:danger >
```

现在 `1-0` 已经交给 `worker-a`，但还没有确认。

检查：

```redis
XPENDING lab:danger points
```

预期待确认数量为 `1`。

然后我们故意执行错误的保留策略：

```redis
XTRIM lab:danger MAXLEN = 1
```

这会从头部删除旧消息 `1-0`，只留下 `2-0`。

再查看：

```redis
XRANGE lab:danger - +
XPENDING lab:danger points
```

结果会出现看似矛盾的状态：

```text
Stream 中：
  2-0  signup:1001:43:v1

PEL 中：
  1-0  归属于 worker-a，尚未确认
```

原因可以直接从源码看出来。

`streamTrim()` 操作的是 Stream 的基数树与内部消息节点；它没有在删除正文时同步调用每个消费者组的 PEL 清理流程。

而 `XACK` 则正相反：它清理 PEL，但不删除正文。

这两条机制管理着两种不同的状态：

```text
                         消息 1-0
                            |
                +-----------+-----------+
                |                       |
                v                       v
          Stream 消息正文           points 组的 PEL
                |                       |
       XTRIM 删除正文           仍记录 worker-a
                |                       |
                v                       v
          无法读取正文              仍是 Pending
```

如果此时另一个消费者尝试恢复该任务，Redis 已经无法从 Stream 中取得 `1-0` 的业务字段和值。

**所以，PEL 中还存在某个消息 ID，并不代表这条消息的正文一定还在。**

在 Redis 7.2.0 中，对于这类正文已被删除的待确认引用，可以使用 `XACK` 移除对应 PEL 记录，或者通过 `XAUTOCLAIM` 的扫描识别并清理失去正文的 PEL 项。但这些操作**无法还原已裁剪的消息内容**。

例如在本实验中：

```redis
XACK lab:danger points 1-0
```

仍预期返回：

```text
(integer) 1
```

它表示确认记录被清理了，而不表示积分任务终于被正确执行。

这里出现了本章最重要的边界：

> **裁剪消息是存储策略，确认消息是消费状态；Redis 7.2.0 不会因为某条消息仍在 PEL 中，就自动阻止 XTRIM 裁剪它。**

## 10. 如果有多个消费者组，怎样设计保留规则？

现在假设同一条报名事件需要两个不同的组处理：

```text
activity:1001:events
          |
          +---- points：加积分
          |
          +---- notify：发送通知
```

对于 `1-0`，可能出现：

| 事件 ID | points 组 | notify 组 | 可否直接删除正文？ |
| --- | --- | --- | --- |
| `1-0` | 已确认 | 已确认 | 可以考虑，取决于是否还需要历史回放 |
| `2-0` | 已确认 | 尚未交付 | 不宜删除 |
| `3-0` | 待确认 | 已确认 | 不宜删除 |
| `4-0` | 尚未交付 | 尚未交付 | 不宜删除 |

因此，仅凭 `XLEN` 大小就裁剪消息，容易误伤落后的消费者组。

但是，**不能据此推导出“所有组都 XACK 完毕后，Redis 7.2 的 XTRIM 会自动清理”**。在这个版本里，它没有这种消费者组感知的裁剪语义。

应用需要自己定义保留政策。常见方向包括：

1. **有界历史策略**：允许保留最近一段数据或最近 N 条消息，明确接受慢消费者可能错过旧消息的风险；适合可重建、可容忍丢历史的实时场景。
2. **消费完成后清理策略**：由应用维护各个消费者组已经安全处理的进度，再清理所有组都不再需要的较旧事件；需要额外协调、监控和恢复设计。
3. **归档后清理策略**：先把重要事件写入长期存储，再裁剪 Redis Stream，把 Redis 主要用于在线分发与短期重试。

其中第二种尤其不能仅靠 `XINFO GROUPS` 中的 `last-delivered-id` 或者看当前 `XPENDING = 0` 就下结论：

- `last-delivered-id` 反映组的交付进度，**不等于全部业务已确认**。
- `XPENDING = 0` 只说明当时没有待确认项，**不证明所有历史消息都已被该组处理**。
- 不同消费者可能乱序完成，因此安全水位还需要考虑未交付消息、未确认消息以及实际业务已提交的状态。

如果业务要求“任何一个消费者组尚未完成时，消息正文绝不删除”，在 Redis 7.2.0 中应当由上层应用维护足够的信息来实现这个要求，而不能直接依赖一个简单的 `XTRIM MAXLEN`。

> 版本提示：Redis **8.2** 起，`XTRIM` 和带裁剪选项的 `XADD` 才增加 `KEEPREF`、`DELREF`、`ACKED` 等与消费者组引用相关的选项。它们不属于本系列固定的 Redis 7.2.0 源码基线，本文实验不要使用。

## 11. 除了按数量，还能按消息 ID 裁剪吗？

如果我们的业务不是“只保留最新 N 条”，而是“删除某个界限之前的所有消息”，可以使用 `MINID`。

例如：

```redis
DEL lab:minid
XADD lab:minid 1-0 event signup
XADD lab:minid 2-0 event signup
XADD lab:minid 3-0 event signup
XADD lab:minid 4-0 event signup

XTRIM lab:minid MINID = 3-0
```

预期删除两条消息，只留下 `3-0` 和 `4-0`。

这与 `MAXLEN` 的区别是：

- `MAXLEN` 根据**数量**决定删除多少旧消息；
- `MINID` 根据**消息 ID 的边界**决定删除哪些旧消息。

`MINID` 很适合把保留规则表达成一个明确的 ID 水位。

但不要把它误解成“Redis 会对每条 Stream 消息自动设置 TTL”。`MINID` 需要业务计算并传入 ID 阈值；ID 可能是自动生成的，也可能由调用者指定，并不总能直接代表业务事件的真实发生时间。

此外，`MINID` 仍然不会为 Redis 7.2.0 自动检查所有消费者组是否已完成处理。

## 12. 实验：用 Python 断言确认与裁剪互相独立

如果不希望反复手动输入 Redis 命令，可以用下面这个脚本一次验证本章的关键结论。

要求：Redis 7.2.0 本地可连接，并安装 `redis-py`。

```bash
python -m pip install "redis==5.0.1"
```

保存为 `stream_trim_lab.py`：

```python
import redis

r = redis.Redis(
    host="127.0.0.1",
    port=6379,
    decode_responses=True,
)

KEY = "lab:chapter20"
GROUP = "points"

# 1. 构造确定性的事件序列。
r.delete(KEY)
for i in range(1, 6):
    r.xadd(
        KEY,
        {"event_id": f"signup:1001:{i}:v1", "user_id": str(i)},
        id=f"{i}-0",
    )

assert r.xlen(KEY) == 5
r.xgroup_create(KEY, GROUP, id="0-0")

# 2. 消费并确认第一条消息。
delivered = r.xreadgroup(
    GROUP,
    "worker-a",
    {KEY: ">"},
    count=1,
)
assert delivered[0][1][0][0] == "1-0"
assert r.xpending(KEY, GROUP)["pending"] == 1
assert r.xack(KEY, GROUP, "1-0") == 1

# XACK 只移除 PEL 记录，不删除消息正文。
assert r.xpending(KEY, GROUP)["pending"] == 0
assert r.xlen(KEY) == 5
print("XACK 后 XLEN:", r.xlen(KEY))

# 3. 精确裁剪到三条。
removed = r.xtrim(KEY, maxlen=3, approximate=False)
assert removed == 2
assert r.xlen(KEY) == 3
remaining_ids = [entry_id for entry_id, _ in r.xrange(KEY)]
assert remaining_ids == ["3-0", "4-0", "5-0"]
print("XTRIM 后 ID:", remaining_ids)

# 4. 单独制造“已裁剪正文仍被 PEL 引用”的情况。
DANGER = "lab:chapter20:danger"
r.delete(DANGER)
r.xadd(DANGER, {"event_id": "signup:1001:42:v1"}, id="1-0")
r.xadd(DANGER, {"event_id": "signup:1001:43:v1"}, id="2-0")
r.xgroup_create(DANGER, GROUP, id="0-0")

r.xreadgroup(GROUP, "worker-a", {DANGER: ">"}, count=1)
assert r.xpending(DANGER, GROUP)["pending"] == 1

assert r.xtrim(DANGER, maxlen=1, approximate=False) == 1
assert r.xrange(DANGER, min="1-0", max="1-0") == []
assert r.xpending(DANGER, GROUP)["pending"] == 1

# 仅清理残留的确认记录；这不能恢复消息正文。
assert r.xack(DANGER, GROUP, "1-0") == 1
assert r.xpending(DANGER, GROUP)["pending"] == 0
print("消息正文删除后，PEL 记录仍可独立清理")

print("本章断言全部通过")
```

脚本的预期输出：

```text
XACK 后 XLEN: 5
XTRIM 后 ID: ['3-0', '4-0', '5-0']
消息正文删除后，PEL 记录仍可独立清理
本章断言全部通过
```

其中第 4 步**刻意构造了有风险的状态**，只应在独立测试环境中执行。

本实验验证的是 Redis 7.2.0 的命令行为与状态边界，而不是一个生产级的消息保留策略。

在实际部署中，还需要根据流量、消费者延迟、故障恢复时间目标等决定保留多久，以及出现消费者落后时是否允许裁剪。

## 13. XTRIM 是不是就能解决所有内存问题？

本系列前面已经研究过 `maxmemory` 和内存淘汰。

现在需要把它们与 Stream 裁剪区分开。

`XTRIM` 是对**指定 Stream 历史消息**进行业务语义上的清理；`maxmemory` 是 Redis 实例层面的内存限制和内存淘汰策略。

二者不是同一层的机制。

即使我们给 Stream 设置了 `MAXLEN`，也不能保证该 Stream 使用的内存字节数恒定：

- 不同消息的字段和值大小可能差异很大；
- Redis 还需要维护索引和消费者组的状态；
- 部分条目可能只被标记删除，底层节点尚未整体释放；
- Redis 实例中可能还有其他 Key、连接缓冲区和管理开销。

因此，生产环境应同时观察不同指标：

```redis
XLEN activity:1001:events
XINFO STREAM activity:1001:events
XPENDING activity:1001:events points
MEMORY USAGE activity:1001:events
INFO memory
```

其中 `MEMORY USAGE` 是对象内存用量估计，可能随 Redis 内部存储布局、分配器和采样参数变化；不能把它简单等同于“每条消息占多少字节”。

本章虽然可以教会我们如何为 Stream 设定保留界限，但能否满足业务的恢复窗口与资源预算，还需要真实流量下的运行测试。

## 14. 回头看：消息可靠性为什么最终要求保留策略？

从第十五章开始，我们让活动报名事件不再只是一次即时广播，而是保存为 Stream 历史。

后来为了让多个服务协同处理，又引入了消费者组。

消费者可能崩溃，因此 Redis 需要 PEL、`XACK` 和消息接管机制。

接管又可能导致重复业务，因此我们使用业务事件 ID、数据库事务和唯一约束控制重复效果。

但这些机制共同带来一个新的事实：**消息会被长期保留，除非我们显式制定清理规则。**

这正是 `XTRIM` 存在的原因。

```text
Pub/Sub：离线可能错过事件
          |
          v
Streams：保存历史以便重读
          |
          v
消费者组：多个实例协同处理
          |
          v
PEL + XACK：区分交付和完成
          |
          v
XAUTOCLAIM：故障后重新接管
          |
          v
业务幂等：避免重复业务效果
          |
          v
历史事件越来越多
          |
          v
XTRIM / XADD MAXLEN：控制保留规模
          |
          v
新的取舍：内存成本 vs. 可恢复的历史窗口
```

我们可以据此得到三个明确结论。

**第一，`XACK` 不是删除消息。** 它清除消费者组的未确认记录，消息正文仍保存在 Stream 中。

**第二，`XTRIM` 是按位置或 ID 进行历史裁剪，不是按业务完成状态清理。** 对 Redis 7.2.0 而言，它可能裁掉仍被 PEL 引用的消息正文。

**第三，近似裁剪优先考虑整块内部节点的删除效率。** 这解释了 `MAXLEN ~ N` 为什么可以保留多于 `N` 条消息，也解释了它为什么通常比精确裁剪更适合高吞吐流。

## 15. 下一章：回到最初的内存字典

到这里，我们已经从一份简单的内存字典，走过了 Redis 命令执行、事件循环、原子操作、过期与内存管理、持久化、复制、Sentinel、Cluster，以及不同的数据结构和消息机制。

这些内容看起来很多，但如果回到最初提出的主问题，实际上都可以看成对同一个数据服务施加了新的约束。

下一章我们不再增加新的命令或源码模块，而是用已经理解过的最小案例，把各章真正解决的问题串在一起，重新梳理 Redis 的执行流、数据流和故障恢复边界。

**下一章：《Redis 源码精读总结：一个内存数据服务是怎样长成完整系统的？》**

---

## 源码与文档

本章固定基线：**Redis 7.2.0**。

- [Redis 7.2.0 `src/t_stream.c`](https://github.com/redis/redis/blob/7.2.0/src/t_stream.c)：`xackCommand()`、`xtrimCommand()`、`streamTrim()`、`xaddCommand()`、`streamParseAddOrTrimArgsOrReply()`。
- [Redis 7.2.0 `src/stream.h`](https://github.com/redis/redis/blob/7.2.0/src/stream.h)：`stream` 的索引、长度与消息 ID 字段。
- [Redis XTRIM 官方文档](https://redis.io/docs/latest/commands/xtrim/)：`MAXLEN`、`MINID`、精确与近似裁剪的语义。**注意官方当前文档同时包含较新版本特性。**
- [Redis XADD 官方文档](https://redis.io/docs/latest/commands/xadd/)：追加消息时指定保留规则。
- [Redis Streams 官方文档](https://redis.io/docs/latest/develop/data-types/streams/)：消息历史、消费者组和裁剪策略。
- [Redis XACK 官方文档](https://redis.io/docs/latest/commands/xack/)：PEL 确认语义。

> **验证边界**：文中标记“真实源码节选”的代码均针对 Redis 7.2.0 核对，少量非关键分支已经省略；“教学化简”不冒充完整实现。Redis 命令与 Python 演示尚未在当前编写环境实际执行，因此给出的是可复现实验及预期输出，并不是实测报告。
