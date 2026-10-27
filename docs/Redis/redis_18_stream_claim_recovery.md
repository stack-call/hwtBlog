---
title: "第十八章：消费者崩溃后，消息怎样被接管？从 PEL 到 XCLAIM 与 XAUTOCLAIM"
sidebar_label: "18 - XCLAIM 与故障接管"
sidebar_position: 19
description: "沿活动报名事件处理失败的案例，从尚未确认的 PEL 记录出发，精读 Redis 7.2.0 的 XCLAIM 与 XAUTOCLAIM：超时判断、所有权转移、游标扫描、确认以及重复处理边界。"
tags:
  - Redis
  - 源码精读
  - Streams
  - 消费者组
  - XCLAIM
  - XAUTOCLAIM
---

# 第十八章：消费者崩溃后，消息怎样被接管？从 PEL 到 XCLAIM 与 XAUTOCLAIM

上一章，我们已经让活动 `1001` 的大屏服务使用消费者组接收报名事件，并理解了 **PEL（Pending Entries List）** 的作用。

每当 `dashboard-A` 从消费者组读取一条新消息，只要没有使用 `NOACK`，Redis 就会在 PEL 中记下这条消息已交付、但尚未得到完成确认。只有大屏更新成功，应用才执行 `XACK`，清除这条待确认记录。

这个设计解决了一个重要问题：**消息被送到消费者，并不意味着它已经处理完成。**

但上一章故意留下了一个问题。

假设消息 `1000-0` 是“用户 42 报名活动 1001”，大屏服务 `dashboard-A` 已经读到了它，却在真正更新统计之前崩溃：

```text
Stream: activity:1001:events
           |
           | XREADGROUP ... >
           v
      dashboard-A
           |
           | 获得 1000-0
           X 处理前崩溃

组 PEL：1000-0 -> dashboard-A（未确认）
```

如果 A 再也无法恢复，消息会不会就永远留在 PEL 里？

我们希望其他正在运行的大屏实例能够接手它。但如果接管太激进，可能会把**仍在正常处理中的消息**交给第二个实例，造成重复执行。

本章只解决这个新的问题：**怎样在保留未确认状态的前提下，让一个消费者接管另一个消费者留下的工作？**

本文固定分析 [Redis 7.2.0](https://github.com/redis/redis/tree/7.2.0)。实验展示的是可复现步骤及**预期结果**；本文编写环境没有运行 Redis 服务，不把预期结果描述为实测。

## 1. 为什么增加第二个消费者，还不够？

先回忆一下上一章的消费过程。

假设当前 Stream 中只有一条报名事件：

```text
消息 ID  事件              用户
1000-0  signed-up         42
```

消费者组叫 `dashboard`。`dashboard-A` 执行：

```redis
XREADGROUP GROUP dashboard dashboard-A COUNT 1 STREAMS activity:1001:events >
```

收到 `1000-0` 后，Redis 做了两件不同的事：

```text
组 last_id：推进到 1000-0
组 PEL：新增 1000-0 -> dashboard-A
```

`last_id` 用来确定这个组接下来从哪里读取**尚未交付的新消息**；PEL 用来保存**已经交付、尚未确认的消息**。

现在 A 崩溃，启动 `dashboard-B`：

```redis
XREADGROUP GROUP dashboard dashboard-B COUNT 1 STREAMS activity:1001:events >
```

B 不会自动得到 `1000-0`。如果组里没有更新的消息，它会收到空结果。

原因不是 Stream 删除了 `1000-0`，而是 `>` 表示读取组内**尚未交付的新消息**。这条消息已经交付过，仍然归在 A 的 PEL 名下。

因此：

> **消费者组的正常读取路径解决“谁来接收下一条新消息”，并不自动解决“谁来接手上一个消费者未完成的消息”。**

我们需要的是另外一个动作：不改变消息正文，也不把它当作全新事件，而是修改这条未确认消息的负责人。

## 2. 最小设计：把未完成任务的负责人换掉

先不急着阅读 Redis 源码。假设我们自己维护一个最小的待处理记录：

```python
# 概念伪代码：不是 Redis 的内部实现
pending = {
    "1000-0": {
        "owner": "dashboard-A",
        "last_delivered_at": 10000,
        "deliveries": 1,
    }
}
```

如果确定 A 已经不能继续处理，那么可以把记录改成：

```python
# 概念伪代码
pending["1000-0"]["owner"] = "dashboard-B"
pending["1000-0"]["last_delivered_at"] = now_ms()
pending["1000-0"]["deliveries"] += 1
```

但如果任何消费者都能随时“抢走”任务，也会有问题。

例如，A 正在调用一个稍慢的大屏统计服务，任务需要 3 秒才能完成。B 在第 1 秒就把任务抢走，可能导致两个实例同时处理同一事件。

因此我们增加一个最小限制：**只有一条消息距离上一次交付已经过去足够长的时间，才允许另一个消费者尝试接手。**

这不是严格的故障检测。它只是用“长时间没有被重新交付或确认”近似判断这条任务可能需要恢复。

Redis 把这项操作实现为 `XCLAIM`。

最简形式：

```redis
XCLAIM key group consumer min-idle-time id [id ...]
```

在当前案例中：

```redis
XCLAIM activity:1001:events dashboard dashboard-B 60000 1000-0
```

它的意思是：

- 在 `dashboard` 组中查找待确认的 `1000-0`；
- 如果距上次交付至少经过 `60000` 毫秒（60 秒）；
- 就让 `dashboard-B` 接手，并把消息内容返回给它。

**`min-idle-time` 的单位是毫秒，不是秒。**

这里需要谨慎选择阈值。太短容易抢走还在处理的任务；太长又会推迟真正故障后的恢复。

接下来先用一段可控实验，让状态变化变得可见。

## 3. 实验一：用 XCLAIM 手动接管一条消息

我们使用上一章相同的活动事件键 `activity:1001:events`，但会先清理它以保持实验结果独立。

如果已经有 Redis 7.2.0 容器，可以复用；否则启动：

```bash
docker run -d \
  --name redis-source-lab \
  -p 127.0.0.1:6379:6379 \
  redis:7.2.0 \
  --save "" \
  --appendonly no
```

以下命令可逐行输入：

```bash
docker exec -it redis-source-lab redis-cli
```

先创建一条具有**固定 ID** 的事件：

```redis
DEL activity:1001:events
XADD activity:1001:events 1000-0 event signed-up user 42
XGROUP CREATE activity:1001:events dashboard 0-0
```

预期：`XADD` 返回 `1000-0`；`XGROUP CREATE` 返回 `OK`。

让 A 获取消息，但**故意不执行 `XACK`**：

```redis
XREADGROUP GROUP dashboard dashboard-A COUNT 1 STREAMS activity:1001:events >
```

预期消息包括：

```text
1000-0
  event = signed-up
  user  = 42
```

此时模拟 A 故障：不再让它执行任何消费或确认命令。你可以关闭充当 A 的终端；Redis 里的消费者名和 PEL 记录不会因为 TCP 连接断开就自动消失。

观察未确认消息：

```redis
XPENDING activity:1001:events dashboard - + 10
```

预期有一条记录：

```text
ID           owner          idle(ms)    deliveries
1000-0       dashboard-A    (变化中)    1
```

`idle(ms)` 与执行时间有关，所以不应写死一个具体值。这里要记住的是 `owner=dashboard-A`，交付次数为 `1`。

让 B 尝试读取下一条新消息：

```redis
XREADGROUP GROUP dashboard dashboard-B COUNT 1 STREAMS activity:1001:events >
```

由于没有新消息，预期为空。这样就重现了本章最初的困难。

为了让本地实验不必等待 60 秒，我们临时把接管阈值设为 `0`：

```redis
XCLAIM activity:1001:events dashboard dashboard-B 0 1000-0
```

`0` 表示不要求最小空闲时长，**只适合在这个受控演示里使用**；生产环境不能不加判断地设置为 0。

这次预期返回 `1000-0` 的消息字段，表明 B 已经成功接管。

再次观察：

```redis
XPENDING activity:1001:events dashboard - + 10
```

预期状态变成：

```text
ID           owner          idle(ms)    deliveries
1000-0       dashboard-B    (较小)      2
```

这里出现了三个可以直接核对的变化：

1. 消息 ID 还是 `1000-0`，没有生成另一条 Stream 事件；
2. PEL 的负责人从 A 变成了 B，待确认消息总数仍为 1；
3. 最近交付时间被更新，通常交付次数从 1 增加为 2。

最后让 B **在业务处理成功后**确认：

```redis
XACK activity:1001:events dashboard 1000-0
XPENDING activity:1001:events dashboard
XRANGE activity:1001:events - +
```

预期 `XACK` 返回 `1`，PEL 数量变为 `0`，但 `XRANGE` 仍然能查到 `1000-0` 的事件正文。

**接管修改的是待确认状态，确认删除的是待确认记录，它们都不等于删除 Stream 消息本体。**

## 4. 为什么 XCLAIM 不重新写入 Stream？

现在可以回到源码验证刚才的状态变化。

固定版本：Redis 7.2.0。

源码入口：[`src/t_stream.c` 中的 `xclaimCommand()`](https://github.com/redis/redis/blob/7.2.0/src/t_stream.c#L2910-L3097)。

函数首先拿到 Stream 和消费者组，并解析 `min-idle-time`、消息 ID 与可选参数。接管操作不能只凭一个消息 ID 就盲目执行：Redis 必须确认当前组是否真有这条待确认记录，以及消息正文是否仍然存在。

**真实源码节选（Redis 7.2.0，非连续片段）：**

```c
streamNACK *nack = raxFind(group->pel,buf,sizeof(buf));
```

这里的 `group->pel` 是组级别的 PEL。`raxFind()` 根据消息 ID 查找对应的待确认条目。

我们在上一章见过 `streamNACK`。在这个结构中，Redis 记录最近一次交付时间、交付次数和当前所属消费者。

**真实源码节选：[`src/stream.h`](https://github.com/redis/redis/blob/7.2.0/src/stream.h#L84-L90)**

```c
typedef struct streamNACK {
    mstime_t delivery_time;
    uint64_t delivery_count;
    streamConsumer *consumer;
} streamNACK;
```

现在可以对照实验中的值：

```text
认领前：

streamNACK(1000-0)
  delivery_time = A 最后收到消息的时刻
  delivery_count = 1
  consumer       = dashboard-A
```

**为什么 Redis 不直接删除原来的 PEL，再为 B 新建一条？**

因为这并不是一条全新的未确认消息，而是同一条消息由另一名消费者接手。保留同一份 `streamNACK` 并更新所属关系，就可以保留它的交付历史信息。Redis 还必须保证组级 PEL 与消费者级 PEL 的归属一致。

## 5. XCLAIM 怎样判断消息可以被接管？

假设 A 只离线了 10 毫秒，而我们要求至少等待 60 秒。

B 此时就不能贸然接管。

在 `xclaimCommand()` 中，Redis 根据当前时间和 `nack->delivery_time` 计算消息的空闲时长。

**真实源码节选（同一函数）：**

```c
mstime_t this_idle = now - nack->delivery_time;
if (this_idle < minidle) continue;
```

它的判断很直接：

```text
当前时刻 - 上次交付时刻 = idle

idle < min-idle-time  -> 跳过
idle >= min-idle-time -> 可以继续尝试接管
```

这也是为什么上一节的 `min-idle-time=0` 会使实验不需要等待：只要记录符合其他接管条件，就不会被空闲时长拦住。

如果你希望观察超时限制，可以**确实创建并交付**第二条消息，然后立即尝试接管：

```redis
XADD activity:1001:events 1001-0 event signed-up user 57
XREADGROUP GROUP dashboard dashboard-A COUNT 1 STREAMS activity:1001:events >
XCLAIM activity:1001:events dashboard dashboard-B 60000 1001-0
```

因为 `1001-0` 刚刚被交付给 A，距离上次交付还没有达到 60 秒，最后一条 `XCLAIM` 预期返回空数组。这里已确认消息确实进入 PEL，因此空结果才能对应到超时判断，而不是“ID 根本不存在”。

本章稍后的 Python 实验会直接创建与交付消息，再先尝试高阈值、后尝试合理等待后的接管，把这个条件做成可验证的断言。

我们还要留意一个边界：**空闲时间并不是消费者存活探针。**

Redis 看见的是“这条待确认消息距离上次交付过去了多久”，而不是“消费者进程是否真的崩溃”。如果 A 正在执行一个超过阈值的长任务，B 也可能成功接管。

因此阈值应该结合实际业务耗时、超时策略和重复处理保护设计。

## 6. XCLAIM 真正修改的是哪几处数据？

到这里，我们已经知道 Redis 会先检查 PEL 与空闲时长。接下来才是接管的核心动作。

回忆上一章的 PEL 结构：

```text
消费者组 dashboard
  |
  +-- group->pel
  |     1000-0 -> 同一个 streamNACK
  |
  +-- dashboard-A->pel
  |     1000-0 -> 同一个 streamNACK
  |
  +-- dashboard-B->pel
        （空）
```

Redis 不能只修改 `nack->consumer` 这个指针，否则旧消费者 A 的 PEL 仍然包含这条消息，而新消费者 B 的 PEL 却找不到它。

在 `xclaimCommand()` 的成功接管分支中，实际逻辑可以概括为：

**教学化简（保留关键语义，不是可直接编译的 Redis 源码）：**

```c
if (nack->consumer != new_consumer) {
    remove_from_old_consumer_pel(message_id);
}

nack->delivery_time = now;
if (!justid) nack->delivery_count++;

if (nack->consumer != new_consumer) {
    insert_into_new_consumer_pel(message_id, nack);
    nack->consumer = new_consumer;
}

reply_with_original_stream_entry(message_id);
```

真实源码中，对应的容器操作使用 `raxRemove()` 和 `raxInsert()`，相关位置在 [`t_stream.c#L3044-L3087`](https://github.com/redis/redis/blob/7.2.0/src/t_stream.c#L3044-L3087)。

于是，接管之后变成：

```text
消费者组 dashboard
  |
  +-- group->pel
  |     1000-0 -> 同一个 streamNACK
  |
  +-- dashboard-A->pel
  |     （空）
  |
  +-- dashboard-B->pel
        1000-0 -> 同一个 streamNACK

streamNACK(1000-0)
  delivery_time = B 接管时刻
  delivery_count = 2
  consumer       = dashboard-B
```

这解释了 `XPENDING` 为什么会显示消息从 A 转移到 B，并且消息总数没有变化。

另外，`XCLAIM` **不会因为接管而推进消费者组的新消息读取游标 `last_id`**。正常接管是在处理已经交付的消息；`last_id` 负责下一条新消息的位置，两者不能混为一谈。（命令的 `LASTID` 特殊选项属于另外的用途，本章不使用。）

`XCLAIM` 也不会替应用执行任务。Redis 只是返回消息正文、修改所有权；B 仍然必须完成实际业务，再调用 `XACK`。

## 7. 我们已经能接管，但前提是知道具体消息 ID

至此，只要我们知道待恢复的消息 ID，就能直接使用 `XCLAIM`。

例如：

```redis
XCLAIM activity:1001:events dashboard dashboard-B 60000 1000-0
```

然而，真实的活动可能产生几十万条报名消息。一个消费者可能在一次故障前领走多条消息，而应用事先并不知道所有未完成的 ID。

我们当然可以先执行 `XPENDING`，找出长时间没有确认的消息，然后逐条调用 `XCLAIM`。

这个方案是可行的。但它意味着应用必须自己维护分页扫描、筛选和接管过程，而且要处理扫描期间 PEL 状态变化的问题。

**我们现在缺少的，不是新的确认机制，而是一个能“边扫描 PEL，边接管满足条件的消息”的命令。**

这就是 Redis 6.2 开始提供、并且在 Redis 7.2.0 中可用的 `XAUTOCLAIM`。

最简格式：

```redis
XAUTOCLAIM key group consumer min-idle-time start [COUNT count]
```

例如：

```redis
XAUTOCLAIM activity:1001:events dashboard dashboard-B 60000 0-0 COUNT 10
```

与 `XCLAIM` 的主要区别是：

- `XCLAIM`：应用已经知道 ID，明确指定要接管哪些消息。
- `XAUTOCLAIM`：应用给出扫描起点，Redis 遍历组 PEL，找到空闲时间满足条件的消息并接管。

注意名字中的 `AUTO` **不代表 Redis 会在后台自己定时运行这个命令**。调用者仍然需要主动发出 `XAUTOCLAIM`，如果要持续恢复，则需要自己的定时任务或恢复工作循环。

## 8. XAUTOCLAIM 为什么需要一个游标？

如果一个消费者组积累了十万条未确认消息，直接要求 Redis 在一次命令中扫描并接管全部消息，可能导致单次命令运行时间过长，影响其他请求。

因此，`XAUTOCLAIM` 采用类似增量扫描的接口：

```text
客户端第一次调用：start = 0-0
       |
       v
扫描一部分 PEL
       |
       +-- 返回本批接管的消息
       |
       +-- 返回下次继续扫描的位置
                         |
                         v
客户端下一次调用：start = 上次返回的游标
```

当扫描到 PEL 末尾时，游标返回 `0-0`。

但这里的 `0-0` 只表示**本轮扫描走到了末尾**，不是“从此没有需要恢复的消息”。那些本轮还没达到空闲阈值的消息，过一段时间可能变成可接管状态，因此恢复循环之后仍然需要从 `0-0` 重新开始新的一轮。

Redis 7.2.0 的 `XAUTOCLAIM` 返回三个部分：

```text
1. next_cursor：下一次扫描的位置
2. claimed_entries：本次成功接管的消息
3. deleted_ids：消息正文已不存在、因此清理了 PEL 记录的 ID
```

第三个返回值很重要：如果某条未确认的消息本体已经被 `XDEL` 删除，或被 Stream 裁剪掉，它虽然可能还留在 PEL 中，但已经没有正文可供业务重试。

Redis 7.0 及之后的 `XAUTOCLAIM` 可以在扫描时清理这些悬空待确认记录，并将相关 ID 返回给调用者。

**清理 PEL 不等于成功恢复业务事件**。消息正文已丢失，就需要依赖其他业务数据、审计或补偿机制排查。

## 9. 沿 Redis 7.2.0 源码看 XAUTOCLAIM 的扫描

源码位置：[`src/t_stream.c` 的 `xautoclaimCommand()`](https://github.com/redis/redis/blob/7.2.0/src/t_stream.c#L3098-L3273)。

函数首先查找 Stream 和消费者组，解析最小空闲时间、扫描起点与可选的 `COUNT`。

接着，在组级 PEL 上建立迭代器。这里必须扫描**组级 PEL**，而不能只扫描旧消费者 A 的 PEL：

- A 可能已经彻底离线，恢复程序不应该必须了解它的名字；
- 同一个组也可能有多个故障消费者；
- 组级 PEL 包含这个组里全部待确认消息。

在源码中可以看到：

**真实源码节选（Redis 7.2.0）：**

```c
raxStart(&ri,group->pel);
raxSeek(&ri,">=",startkey,sizeof(startkey));
```

这意味着从指定 ID 开始扫描待确认消息索引，而不是遍历整个 Stream 正文。

随后，Redis 对候选记录判断空闲时间。满足条件的消息，与 `XCLAIM` 一样完成旧消费者 PEL 到新消费者 PEL 的转移、更新时间和交付次数，并把消息返回给调用者。

需要留意一个实现取舍。

**真实源码节选（同一函数）：**

```c
const unsigned attempts_factor = 10;
```

这个常量配合传入的 `COUNT`，限制一次命令允许检查的 PEL 条目数。

具体地，Redis 7.2.0 使用 `COUNT × 10` 作为单次调用的扫描尝试上限。`COUNT` 还限制本次返回/处理的条目数量；因此 `COUNT 10` **不意味着一定接管十条消息**。

例如：

```text
PEL 中有 100 条待确认消息

其中前 80 条：刚交付，idle 不够
后 20 条：idle 已足够
```

如果只扫描前面的一小段，可能一条都没有接管，尽管后面还有符合条件的消息。

因此，客户端要根据返回的游标继续扫描，不能把“一次调用返回空消息列表”直接解释成“整个组没有需要恢复的任务”。

最后，Redis 在扫描结束时生成新的游标，并在 Redis 7.2.0 的回复中附上本次因正文缺失而清理的消息 ID。

这就解释了为什么 `XAUTOCLAIM` 的返回值比 `XCLAIM` 更复杂：

它不仅完成所有权转移，还承担了**可分批继续扫描**和**反馈异常记录**的职责。

## 10. 实验二：不预先知道消息 ID，也能恢复任务

为了不受上一个实验状态影响，重新初始化队列。下面故意添加三条固定 ID 的事件：

```redis
DEL activity:1001:events
XADD activity:1001:events 1000-0 event signed-up user 42
XADD activity:1001:events 1001-0 event signed-up user 57
XADD activity:1001:events 1002-0 event signed-up user 63
XGROUP CREATE activity:1001:events dashboard 0-0
```

让 A 读取全部三条，但不确认：

```redis
XREADGROUP GROUP dashboard dashboard-A COUNT 3 STREAMS activity:1001:events >
```

此时：

```text
组 PEL
1000-0 -> dashboard-A
1001-0 -> dashboard-A
1002-0 -> dashboard-A
```

现在让 B 从 `0-0` 开始扫描，每次最多接管一条：

```redis
XAUTOCLAIM activity:1001:events dashboard dashboard-B 0 0-0 COUNT 1
```

在本实验里 `min-idle-time=0`，可以不依赖等待时间。返回值会包含游标和本次接管的消息；由于 `COUNT 1`，每次正常至多接管一条消息。

继续使用上次返回的游标重复调用，直到游标返回 `0-0`。

当三条消息都被 B 接管之后，检查：

```redis
XPENDING activity:1001:events dashboard - + 10
```

预期三条消息的 `owner` 均为 `dashboard-B`，交付次数通常为 2；但它们仍在 PEL 中，尚未被确认。

确认成功处理的消息：

```redis
XACK activity:1001:events dashboard 1000-0 1001-0 1002-0
```

预期返回 `3`，再查询 `XPENDING` 应显示待确认总数为 `0`。

### 对照实验：消息正文被删除会怎么样？

再单独初始化一次：

```redis
DEL activity:1001:events
XADD activity:1001:events 2000-0 event signed-up user 71
XGROUP CREATE activity:1001:events dashboard 0-0
XREADGROUP GROUP dashboard dashboard-A COUNT 1 STREAMS activity:1001:events >
```

此时 `2000-0` 已进入 PEL。

然后故意删除正文：

```redis
XDEL activity:1001:events 2000-0
```

执行扫描：

```redis
XAUTOCLAIM activity:1001:events dashboard dashboard-B 0 0-0 COUNT 10
```

在 Redis 7.2.0 下，本次扫描会发现 `2000-0` 对应的 Stream 条目已不存在，因此将它的待确认记录清理掉，并把 `2000-0` 列在**第三个返回部分**中。

这不是一次成功的重试。`dashboard-B` 没有得到报名事件的内容，也不能仅靠这个 ID 就重建用户 71 的完整业务上下文。

这个实验提醒我们：**Stream 的消息保留策略必须与最长可能处理时间和恢复窗口协调。**

## 11. 用 Python 串起“超时 → 接管 → 业务成功 → 确认”

命令行实验让我们看清楚了 PEL 的变化。

现在将这个过程写成一个最小 Python 脚本，模拟 `dashboard-A` 崩溃后由 `dashboard-B` 接管。

安装客户端：

```bash
python -m pip install "redis==5.0.1"
```

保存为 `stream_recovery_demo.py`：

```python
import time
import redis

STREAM = "activity:1001:events:recovery"
GROUP = "dashboard"
EVENT_ID = "1000-0"

r = redis.Redis(
    host="127.0.0.1",
    port=6379,
    decode_responses=True,
)

# 实验初始化：只清理本脚本专用的 Key
r.delete(STREAM)
r.xadd(
    STREAM,
    {"event": "signed-up", "user": "42"},
    id=EVENT_ID,
)
r.xgroup_create(STREAM, GROUP, id="0-0")

# A 读取消息，但模拟业务执行前崩溃，因此不 XACK
read_by_a = r.xreadgroup(
    GROUP, "dashboard-A", {STREAM: ">"}, count=1
)
assert read_by_a[0][1][0][0] == EVENT_ID
print("A 收到消息，未确认：", EVENT_ID)

# B 只获取新消息，无法读取已经交付给 A 的旧消息
new_for_b = r.xreadgroup(
    GROUP, "dashboard-B", {STREAM: ">"}, count=1
)
assert new_for_b == []
print("B 查询新消息：空")

# 刚交付完，用明显大于实际经过时间的阈值尝试接管
not_yet = r.xclaim(
    STREAM, GROUP, "dashboard-B",
    min_idle_time=60000,
    message_ids=[EVENT_ID],
)
assert not_yet == []
print("空闲未满 60 秒，不能接管")

# 再等待至少 1 秒，用较短阈值进行本地演示
# 生产环境应按真实处理耗时选择阈值
time.sleep(1.2)
claimed = r.xclaim(
    STREAM, GROUP, "dashboard-B",
    min_idle_time=1000,
    message_ids=[EVENT_ID],
)
assert len(claimed) == 1
assert claimed[0][0] == EVENT_ID
print("B 接管成功：", claimed[0])

# 这里仅模拟业务处理成功，未真正更新大屏数据库
processed = True
if processed:
    acknowledged = r.xack(STREAM, GROUP, EVENT_ID)
    assert acknowledged == 1
    print("B 已确认完成")

assert r.xpending(STREAM, GROUP)["pending"] == 0
assert len(r.xrange(STREAM)) == 1  # XACK 不删除事件正文
print("PEL 已清空；Stream 消息仍然存在")
```

预期输出的关键信息是：

```text
A 收到消息，未确认： 1000-0
B 查询新消息：空
空闲未满 60 秒，不能接管
B 接管成功： ('1000-0', {'event': 'signed-up', 'user': '42'})
B 已确认完成
PEL 已清空；Stream 消息仍然存在
```

其中 `claimed` 的具体显示形式取决于 redis-py 的返回值表示；本实验真正重要的是 `assert` 所检查的事实。

这个例子验证了一个最小的恢复路径：

```text
A 取得消息，不确认
          |
          v
       PEL 留下记录
          |
          v
B 使用 XCLAIM 判断 idle
          |
          v
       归属从 A 变 B
          |
          v
B 完成业务，调用 XACK
          |
          v
       PEL 记录消失
```

需要强调：代码中的 `processed=True` **只模拟业务成功**，不等于真实的大屏统计更新成功。实际应用必须在业务更新成功之后才能确认。

如果要把它扩展成持续运行的消费者，不能只在程序启动时接管一次。应用还需要定期扫描超时 PEL，并在处理新消息的同时持续执行恢复逻辑。可用 `XAUTOCLAIM` 完成扫描，但重试间隔、失败次数、监控与幂等策略都需要由应用明确制定。

## 12. 一个更难的问题：A 也许没有崩溃

现在我们已经能让 B 接管 `1000-0`，是否意味着系统已经“可靠地处理一次”这条报名事件？

还不能。

设想一个并不罕见的情况：

```text
A 取得消息 1000-0
       |
       v
A 开始更新大屏数据库
       |
       | 操作很慢，超过了恢复阈值
       v
B 通过 XCLAIM 接管 1000-0
       |
       v
B 也开始更新大屏数据库
       |
       v
A 的数据库更新终于成功
       |
       v
B 的数据库更新也成功
```

Redis 能保证一次成功的接管会修改 PEL 所有权，但它**无法撤销 A 已经开始的外部业务操作**。

另外还有另一条可能的执行路径：

```text
A 已经成功更新大屏统计
       |
       X 尚未来得及 XACK 就崩溃
       |
       v
B 接管消息，重新更新统计
```

这时，即使没有两个消费者同时工作，业务仍然可能被执行两次。

所以我们必须区分：

| 能力 | Redis 本章的命令是否提供 |
|---|---|
| 保留待确认消息的归属 | PEL 提供 |
| 将待确认消息交给另一个消费者 | `XCLAIM` / `XAUTOCLAIM` 提供 |
| 发现某条消息空闲超过阈值 | 命令在执行时判断 |
| 自动定时启动恢复任务 | **不提供**，需要应用主动调度 |
| 保证原消费者已经彻底停止执行 | **不保证** |
| 保证外部业务操作只执行一次 | **不保证** |
| 接管后自动确认业务处理完成 | **不提供**，仍需按业务成功结果执行 `XACK` |

这也解释了为什么 `delivery_count` 值得关注：同一条消息被反复接管，可能意味着业务一直失败，或者恢复阈值太激进。

不过，交付次数只是线索，不是“业务已经失败几次”的精确计数。`JUSTID` 等选项还会改变计数更新行为，不能拿它直接等同于业务失败次数。

如果确实需要重试，可以基于交付次数及业务错误信息设置处理上限，但超限之后怎么处理，需要应用自行设计；Redis 7.2 的 Streams 不会自动把消息转移到一个所谓的“死信队列”。

还有两个容易误用的操作值得说明：

- **不要把 `XGROUP DELCONSUMER` 当作安全的故障接管。** 在 Redis 7.2.0 中，这个命令删除消费者时也会清理其关联的待确认记录，不能替代先接管、处理再确认的流程。
- **不要假定 `XAUTOCLAIM` 可以恢复已删除的正文。** 如果消息已被 `XDEL` 或裁剪，扫描只能清理悬空记录并报告 ID，不能重建原始事件内容。

## 13. 回看演化路径：我们究竟增加了什么？

上一章，我们已经理解消费者组的三份状态：

```text
Stream 正文         记录发生过什么
组 last_id          记录新消息交付到哪里
PEL                 记录哪些交付尚未确认
```

本章并没有创造第四份消息正文，也没有新增一套独立队列。

我们只发现，PEL 中除了保存“有没有完成”，还必须管理“当前由谁负责”和“上次交付是什么时候”。

Redis 已经在 `streamNACK` 中保存了这些信息，因而可以在不重新写入 Stream 的情况下实现接管。

```text
PEL 已经能记录未完成任务
             |
             | A 永久离线，任务无人处理
             v
       需要转移任务归属
             |
             v
          XCLAIM
             |
             | 必须先知道消息 ID
             v
       XPENDING + XCLAIM
             |
             | 大量消息需要分页扫描
             v
         XAUTOCLAIM
             |
             | 扫描并转移超时任务
             v
       B 执行业务并 XACK
             |
             | A 可能仍在执行，或已执行完
             v
     下一章：重复处理与幂等性
```

到这里，我们可以用自己的话描述整个过程，而不必机械背命令名称：

**未确认消息仍然保存在组级 PEL 中。Redis 可以根据最近交付时间判断它是否适合接管，再把它从旧消费者的 PEL 移到新消费者的 PEL。`XAUTOCLAIM` 则把“扫描待确认消息”和“转移归属”放进同一条可增量执行的命令。**

但是，接管只能重新赋予任务处理资格，不能证明旧处理者没有产生过外部副作用。

因此，下一章我们必须面对新的要求：

> 同一条报名事件可能由两个消费者先后执行，怎样保证活动统计不会重复增加？

**下一章：《消息恢复后为什么可能重复执行？从至少一次处理到业务幂等》**

---

## 源码与官方文档

本文基于 **Redis 7.2.0**，不混用后续版本新增的消费者组管理命令。

- [`src/stream.h`：`streamCG`、`streamConsumer`、`streamNACK`](https://github.com/redis/redis/blob/7.2.0/src/stream.h#L49-L90)：组级与消费者级 PEL、待确认记录字段。
- [`src/t_stream.c`：`xpendingCommand()`](https://github.com/redis/redis/blob/7.2.0/src/t_stream.c#L2675-L2843)：查询待确认消息的所属消费者、空闲时间和交付次数。
- [`src/t_stream.c`：`xclaimCommand()`](https://github.com/redis/redis/blob/7.2.0/src/t_stream.c#L2910-L3097)：按明确 ID 检查并接管待确认记录。
- [`src/t_stream.c`：`xautoclaimCommand()`](https://github.com/redis/redis/blob/7.2.0/src/t_stream.c#L3098-L3273)：增量扫描组级 PEL、转移归属与清理悬空记录。
- [`src/t_stream.c`：`xackCommand()`](https://github.com/redis/redis/blob/7.2.0/src/t_stream.c#L2623-L2674)：从组级和消费者级 PEL 删除已确认记录。
- [Redis 官方 XCLAIM 文档](https://redis.io/docs/latest/commands/xclaim/)
- [Redis 官方 XAUTOCLAIM 文档](https://redis.io/docs/latest/commands/xautoclaim/)
- [Redis 官方 XPENDING 文档](https://redis.io/docs/latest/commands/xpending/)
- [Redis 官方 Redis 7.2 命令参考](https://redis.io/docs/latest/commands/redis-7-2-commands/)

> **验证声明**：关键源码名称、字段和状态修改路径经过 Redis 7.2.0 静态核对。文中命令行和 Python 示例提供的是**可复现步骤与预期行为**，不是当前环境已完成的 Redis 实测。对线上最新文档中的新增参数，应以 Redis 7.2.0 的命令语法为准。
