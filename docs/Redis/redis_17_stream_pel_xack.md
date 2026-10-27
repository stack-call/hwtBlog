---
title: "第十七章：消息送达不等于处理成功——从 XREADGROUP 到 PEL 与 XACK"
sidebar_label: "17 - PEL 与 XACK"
sidebar_position: 18
description: "沿活动报名事件的消费失败场景，逐步理解 Redis Streams 为什么记录未确认消息，以及 XREADGROUP、XPENDING、XACK 在 Redis 7.2.0 中如何修改消费者组状态。"
tags:
  - Redis
  - 源码精读
  - Streams
  - PEL
  - XACK
---

# 第十七章：消息送达不等于处理成功——从 XREADGROUP 到 PEL 与 XACK

上一章，我们让活动 `1001` 的大屏服务从普通 `XREAD` 切换到了消费者组。

以前，`dashboard-A` 和 `dashboard-B` 都从同一个 ID 开始读取，可能分别处理同一批报名事件。引入 `dashboard` 消费者组之后，两台实例共享这一组的新消息交付进度：A 读走一条新消息，B 再用 `>` 读取时，会从这条消息之后继续。

这解决了**新消息如何在同一个服务的多个实例之间分工**的问题。

但如果 A 收到消息之后，还没有更新大屏统计就突然崩溃了呢？

```text
报名服务
   |
   | XADD：用户 42 报名
   v
Stream: activity:1001:events
   |
   | XREADGROUP 交付 1000-0
   v
dashboard-A
   |
   | 准备更新大屏统计
   X 进程崩溃
```

从 Redis 看，这条消息已经分配给 A；从业务看，大屏却还没有更新。

**“消息交给了谁”与“消息是否处理成功”，是两个不同的问题。**

本章只研究怎样区分这两种状态。我们会先复现问题，再推导一个最小的完成确认机制，最后沿 Redis 7.2.0 源码理解 PEL、`XPENDING` 和 `XACK` 的真实作用。至于**别的消费者怎样接管崩溃消费者留下的任务**，留到下一章。

> **源码与验证约定**：固定版本为 [Redis 7.2.0](https://github.com/redis/redis/tree/7.2.0)。关键源码在 [`src/t_stream.c`](https://github.com/redis/redis/blob/7.2.0/src/t_stream.c) 与 [`src/stream.h`](https://github.com/redis/redis/blob/7.2.0/src/stream.h)。文中明确区分**真实源码节选**与**教学化简**；命令结果是依据这一版本代码整理的**预期现象**，不冒充本地实测。

## 1. 上一章的分工为什么还不够？

假设 Stream 里有两条报名事件：

```text
activity:1001:events

1000-0  ->  signed-up, user=42
1001-0  ->  signed-up, user=57
```

`dashboard` 消费者组的初始交付位置是 `0-0`。

现在 `dashboard-A` 执行：

```redis
XREADGROUP GROUP dashboard dashboard-A COUNT 1 STREAMS activity:1001:events >
```

它收到 `1000-0`。上一章已经知道，此时组的 `last_id` 会推进到 `1000-0`。

假设 A 立即崩溃，`dashboard-B` 随后执行相同的读取操作，只是消费者名换成 B：

```redis
XREADGROUP GROUP dashboard dashboard-B COUNT 1 STREAMS activity:1001:events >
```

B 得到的是下一条**尚未交付的新消息** `1001-0`，而不是 A 留下的 `1000-0`。

```text
Stream 消息：       1000-0          1001-0
                      |               |
第一次交付：       dashboard-A         |
                      X 崩溃          |
第二次交付：                         dashboard-B

组 last_id：0-0 -> 1000-0 -> 1001-0
```

这不是 Redis 把 `1000-0` 删除了。事实上，消息本体仍然在 Stream 中。真正发生变化的是：**组的“已交付位置”已经越过它**。

如果只保存一个 `last_id`，Redis 能回答“接下来应该交付什么新消息”，却无法回答：

> 哪些消息已经交给了消费者，但还没有收到“处理完毕”的确认？

因此，上一章共享游标的最小设计还缺少一份状态。

## 2. 自己设计一个最小的“待完成任务表”

仍然先不看 Redis 源码。

假设我们维护两样东西：

- `last_id`：这个组最后交付的新消息 ID。
- `pending`：已经交付、但还没有得到处理完成确认的消息。

在交付 `1000-0` 时，不只更新 `last_id`，还写入：

```text
pending:
    1000-0 -> dashboard-A
```

大屏处理成功以后，A 再告诉服务器：

```text
1000-0 已完成
```

服务器就能移除这条待确认记录。

如果 A 在处理前崩溃：

```text
pending:
    1000-0 -> dashboard-A
```

这条记录会留下来。

于是，我们至少可以分清三件事：

```text
Stream 中有消息
       |
       | 被消费者组交付
       v
消息处于待确认状态
       |
       | 消费者报告处理完成
       v
待确认记录被清除
```

这里容易产生一个误会：**待确认表不是消息本体的另一份拷贝。**

我们已经有 Stream 负责保存事件内容。待确认表只需要记录“哪个事件还没完成，以及它归哪个消费者处理”。这样既避免复制整条消息，也方便随后查找失败任务。

现在再回到 Redis：它已经提供了这个机制，名字是 **Pending Entries List（PEL，待确认条目列表）**。

名称中虽然有 `List`，但它并不是上一专题学习过的 Redis List 数据类型；在 Redis 7.2.0 的实现里，它使用基数树 `rax` 来按消息 ID 索引待确认记录。

接下来先观察 PEL 对外表现出来的行为。

## 3. 最小实验：消息取到了，但先不确认

建议使用一个独立实验实例，以免混入上一章已经存在的消费者组状态：

```bash
# 如果已有同名容器，直接复用；否则启动实验容器
docker run -d \
  --name redis-source-lab \
  -p 127.0.0.1:6379:6379 \
  redis:7.2.0 \
  --save "" \
  --appendonly no
```

> 如果提示容器名已被占用，请先检查 `docker ps -a`，不要在有重要数据的实例上执行下面的 `DEL`。本章实验只操作专用 Key `activity:1001:events`。

连接 CLI：

```bash
docker exec -it redis-source-lab redis-cli
```

下面的命令在 `redis-cli` 交互终端中依次执行。为让结果可复现，我们使用固定的 Stream ID，而不是 `XADD *` 自动生成 ID。

先准备一条消息和一个消费者组：

```redis
DEL activity:1001:events
XADD activity:1001:events 1000-0 type signed-up user 42
XGROUP CREATE activity:1001:events dashboard 0-0
```

预期 `XADD` 返回 `"1000-0"`，`XGROUP CREATE` 返回 `OK`。

消费者 A 取走这条消息：

```redis
XREADGROUP GROUP dashboard dashboard-A COUNT 1 STREAMS activity:1001:events >
```

返回结果中应包含 `1000-0`、`type=signed-up`、`user=42`。

现在我们**故意不执行任何确认操作**，模拟 A 收到消息后退出。

先查看消费者组还有多少条待确认消息：

```redis
XPENDING activity:1001:events dashboard
```

预期摘要中的第一项为：

```text
待确认总数 = 1
最小 ID    = 1000-0
最大 ID    = 1000-0
消费者列表 = dashboard-A 有 1 条
```

`redis-cli` 实际显示的是嵌套数组，此处按字段含义整理，避免把输出格式误认为另一种协议。

这个实验已经告诉我们一件事：**消息被 `XREADGROUP` 返回之后，即使应用还没有完成任何业务处理，Redis 也已经保存了待确认状态。**

想看到它究竟交给了谁，可以执行详细查询：

```redis
XPENDING activity:1001:events dashboard - + 10
```

每个条目的输出包含四项：

```text
消息 ID           1000-0
当前消费者        dashboard-A
距上次交付的毫秒数  由实际执行时刻决定
交付次数          1
```

第三项随时间变化，所以这里不把它写成某个固定毫秒数。

此时，我们对 PEL 已经有了直观认识。接下来找真实源码，看看这条记录是什么时候、由谁创建的。

## 4. 源码一：PEL 为什么存在于两种对象中？

打开 [`src/stream.h`](https://github.com/redis/redis/blob/7.2.0/src/stream.h)。上一章我们已经见过 `streamCG`，它保存了组的读取进度：

**真实源码节选（省略无关字段）：**

```c
/* streamCG 中的关键字段 */
streamID last_id;
rax *pel;
rax *consumers;
```

这里新增的关注点是 `pel`。

组的 PEL 负责回答：**这个消费者组还有哪些消息未确认？**

但我们现在还需要回答另一件事：**其中哪些消息属于 `dashboard-A`？**

因此 `streamConsumer` 也有自己的 PEL：

**真实源码节选（省略无关字段）：**

```c
/* streamConsumer 中的关键字段 */
sds name;
rax *pel;
```

这就形成了两种查询入口：

```text
                   dashboard 消费者组
                           |
                     group->pel
                           |
                     ID: 1000-0
                           |
                           v
                     待确认记录
                           ^
                           |
                    consumer->pel
                           |
                     dashboard-A
```

但 Redis 并没有为一条待确认消息复制两份完整记录。组 PEL 与消费者 PEL 会指向**同一个**待确认记录对象。

记录本身叫 `streamNACK`：

**真实源码节选 — `src/stream.h`：**

```c
typedef struct streamNACK {
    mstime_t delivery_time;
    uint64_t delivery_count;
    streamConsumer *consumer;
} streamNACK;
```

它回答三个问题：

- `delivery_time`：最近一次交付发生在什么时候？
- `delivery_count`：这条消息被交付过多少次？
- `consumer`：目前由哪个消费者持有？

例如，在第一次交付 `1000-0` 后，可以把内部状态教学化地画成：

```text
Stream 消息本体
  1000-0 -> {type: signed-up, user: 42}

消费者组 dashboard
  last_id = 1000-0
  pel:
    1000-0 ------+
                 |
                 v
          streamNACK
          delivery_count = 1
          consumer = dashboard-A
                 ^
                 |
消费者 dashboard-A |
  pel:            |
    1000-0 -------+
```

这就解释了为什么 PEL 不直接保存在 Stream 的消息正文中。

消息正文属于 Stream，多个消费者组可以分别阅读；而**待确认状态属于某个消费者组**。同一条事件对于大屏组可能仍待确认，对通知组却可能已经确认成功。

不同组之间不应共用这份状态。

## 5. 源码二：XREADGROUP 在哪一刻创建待确认记录？

上一章已经跟踪过一段调用路径：

```text
XREADGROUP GROUP dashboard dashboard-A ... >
       |
       v
xreadCommand()
       |
       v
streamReplyWithRange()
       |
       v
返回新消息，并推进组 last_id
```

现在我们只沿这条路径向下推进一层：**在返回新消息的同时，Redis 怎样建立 PEL 记录？**

定位到 [`src/t_stream.c` 的 `streamReplyWithRange()`](https://github.com/redis/redis/blob/7.2.0/src/t_stream.c#L1564-L1671)。

函数的输入已经包含：当前 Stream、读取范围、消费者组 `group`，以及当前消费者 `consumer`。这说明它在遍历消息时，不只是读取正文，还知道**应该为哪个消费者组和哪个消费者登记交付记录**。

在对外返回一条消息的过程中，首先会推进组的已交付位置：

**教学化简（根据真实源码移除 `entries_read` 分支）：**

```c
if (group && streamCompareID(&id,&group->last_id) > 0) {
    group->last_id = id;
}
```

实际函数还会维护 `entries_read` 等字段。这里暂时只关注“组交付位置推进”这一动作。

然后，函数检查是否需要跟踪确认状态。

**教学化简（只保留条件与新建记录动作）：**

```c
if (group && !noack) {
    streamNACK *nack = streamCreateNACK(consumer);
    // 后续还需写入组 PEL 与消费者 PEL
}
```

这里的条件有两层：

1. `group` 存在：我们正在执行消费者组读取，而不是普通 `XREAD`。
2. `!noack`：调用者没有要求跳过确认跟踪。

随后函数把待确认记录同时登记到组 PEL 与消费者 PEL。为了看清动作，下面只摘取负责插入的两行，省略了“此 ID 已有记录”时的转移与修正分支。

**真实源码节选：**

```c
int group_inserted =
    raxTryInsert(group->pel,buf,sizeof(buf),nack,NULL);
int consumer_inserted =
    raxTryInsert(consumer->pel,buf,sizeof(buf),nack,NULL);
```

这两行中的 `buf` 是按固定字节布局编码后的消息 ID。

为什么按消息 ID 保存？因为之后消费者确认完成时，客户端不会把消息正文再发一遍，而是告诉 Redis：“我已经处理完 **ID 为 `1000-0`** 的消息。”

Redis 只需要使用 ID 就能定位待确认记录。

我们再向下跟一步：`streamCreateNACK()` 如何建立这个对象？

**真实源码节选 — `src/t_stream.c`：**

```c
streamNACK *streamCreateNACK(streamConsumer *consumer) {
    streamNACK *nack = zmalloc(sizeof(*nack));
    nack->delivery_time = commandTimeSnapshot();
    nack->delivery_count = 1;
    nack->consumer = consumer;
    return nack;
}
```

这里的 `delivery_count` 初始为 `1`，因为新消息已经交付给这个消费者一次。

这与刚才 `XPENDING ... - + 10` 看到的“交付次数 = 1”正好对应。

现在我们可以把 `XREADGROUP` 的一次新消息交付描述为：

```text
找到下一条未交付的新消息
            |
            v
         返回消息
            |
            +-- 推进 group.last_id
            |
            +-- 创建 streamNACK
            |       |
            |       +-- 时间 = 当前命令时间
            |       +-- 次数 = 1
            |       +-- 归属 = dashboard-A
            |
            +-- 登记到 group->pel
            |
            +-- 登记到 dashboard-A->pel
```

这里最重要的并不是要记住 `raxTryInsert()` 的参数，而是看到两个原本容易混淆的状态，终于在源码里有了独立存储位置：

- `group.last_id` 解决**新消息发到哪里了**。
- PEL 解决**哪些已交付消息尚未得到完成确认**。

## 6. XPENDING 为什么不需要重新扫描整个 Stream？

现在读者可能会问：

> 如果我想知道还有多少任务没完成，Redis 是否要从头扫描整个 Stream，逐条判断它有没有处理成功？

不需要。

因为刚才的 PEL 本身就是按消息 ID 建立的待确认索引。

打开 [`xpendingCommand()`](https://github.com/redis/redis/blob/7.2.0/src/t_stream.c#L2675-L2843)。

对于只提供 Key 和组名的摘要命令：

```redis
XPENDING activity:1001:events dashboard
```

源码直接查询组 PEL 的大小。

**真实源码节选：**

```c
addReplyLongLong(c,raxSize(group->pel));
```

因此 Redis 不需要重新遍历所有报名事件才能得到未确认数量。

如果需要列出详细记录，`xpendingCommand()` 会根据请求参数遍历对应的 PEL。没有指定消费者时使用组 PEL；指定消费者时使用该消费者的 PEL。

**教学化简（保持实际选择逻辑）：**

```c
rax *pel = consumer ? consumer->pel : group->pel;

// 在 pel 中按 ID 范围遍历；
// 输出 ID、当前消费者、空闲时间、交付次数。
```

这样，我们就能够检查刚才故意留下的未确认任务。

不过到这里，Redis 仍然只能知道“还没有确认”，不能知道“业务真的成功还是失败”。

这一点需要消费者主动告诉它。

## 7. 为什么必须由消费者主动确认？

假设 Redis 在发送完消息后就直接把待确认记录删除。

那么 A 收到消息后立即崩溃时，Redis 就没有任何记录能说明任务尚未完成。

另一种想法是，Redis 等待客户端确认“已经收到消息”，再清理记录。

但“收到了网络消息”仍然不等于“大屏数据更新成功”。

真正能够判断业务动作是否完成的是消费程序，而不是 Redis 的网络层。

因此，合理的职责分界是：

```text
Redis：把报名事件交给 dashboard-A
   |
   | 记录 PEL：尚未确认
   v
应用：更新大屏状态
   |
   | 业务处理成功
   v
应用：发送 XACK
   |
   v
Redis：清理 PEL 中的对应记录
```

对应命令是：

```redis
XACK activity:1001:events dashboard 1000-0
```

它有三个关键参数：

- Stream Key：`activity:1001:events`。
- 消费者组：`dashboard`。
- 要确认的消息 ID：`1000-0`。

注意，命令没有要求指定消费者名。

因为它的目标是确认**组内**某个消息 ID。Redis 可以通过组 PEL 找到当前持有该消息的消费者，然后同时清理两处索引。

接下来去源码验证。

## 8. 源码三：XACK 删除的究竟是什么？

定位到 [`src/t_stream.c` 的 `xackCommand()`](https://github.com/redis/redis/blob/7.2.0/src/t_stream.c#L2623-L2674)。

函数首先使用传入的 Stream Key 和组名找到消费者组，再将客户端提供的消息 ID 转换为内部格式。

随后，在当前组的 PEL 中查找这个 ID。

**真实源码节选：**

```c
streamNACK *nack = raxFind(group->pel,buf,sizeof(buf));
```

如果找到了，说明这条 ID 在这个消费者组里仍处于待确认状态。

下一步就是删除待确认记录。

**真实源码节选：**

```c
raxRemove(group->pel,buf,sizeof(buf),NULL);
raxRemove(nack->consumer->pel,buf,sizeof(buf),NULL);
streamFreeNACK(nack);
acknowledged++;
```

我们现在能够理解为什么必须有两次 `raxRemove()`：

- 第一次从消费者组的总 PEL 移除这条记录。
- 第二次从当前所属消费者的 PEL 移除同一条记录。
- 随后释放共享的 `streamNACK` 对象。

如果不同时清理两个索引，就会让组与消费者看到不一致的未确认状态。

函数最后将 `acknowledged` 返回给客户端。

所以，对于仍在 PEL 中的 `1000-0`：

```redis
XACK activity:1001:events dashboard 1000-0
```

预期返回：

```text
(integer) 1
```

如果再次确认相同的消息：

```redis
XACK activity:1001:events dashboard 1000-0
```

这时组 PEL 中已经没有它，预期返回：

```text
(integer) 0
```

这里的 `1` 和 `0` 表示本次成功清理了多少条**待确认记录**，不是消息本体删除了多少条。

至此，本章终于可以回答最开始的问题：

**`XACK` 改变的是消费者组对这条消息的处理状态，而不是 Stream 保存的事件正文。**

## 9. 再做一次实验：ACK 前后到底发生了什么？

接着第 3 节的实验，当前组有一条未确认消息：

```text
Stream 正文： 1000-0 -> user=42
组 PEL：     1000-0 -> dashboard-A
```

执行：

```redis
XACK activity:1001:events dashboard 1000-0
```

预期返回 `(integer) 1`。

再看待确认摘要：

```redis
XPENDING activity:1001:events dashboard
```

预期显示未确认总数变成 `0`。

此时检查 Stream 消息正文：

```redis
XRANGE activity:1001:events - +
```

**仍然能够读到** `1000-0` 以及 `type=signed-up`、`user=42`。

两种状态的前后对比如下：

| 状态 | 执行 XACK 前 | 执行 XACK 后 |
|---|---|---|
| Stream 正文里的 `1000-0` | 存在 | 仍然存在 |
| `dashboard` 组 PEL | 包含 `1000-0` | 不再包含 |
| `dashboard-A` 的 PEL | 包含 `1000-0` | 不再包含 |
| `dashboard` 组 `last_id` | `1000-0` | 仍是 `1000-0` |
| 同一个 ID 再次 XACK | 首次返回 1 | 再次返回 0 |

这张表特别重要，因为它区分了我们之前学过的三个概念：

- **消息本体**：事件仍然保存在 Stream 中，可以按 ID 查询。
- **组游标**：记录该组已向前交付到哪里，ACK 不会把它倒退。
- **PEL**：记录已交付但尚未确认的消息，ACK 清理的是它。

如果还用 List 取出任务的直觉来理解 Streams，很容易把这三者混为一谈。

## 10. 消费者自己重启后，能不能看到自己的未确认消息？

有了 PEL，一个自然问题是：

> `dashboard-A` 重启了。如果它继续用 `>` 读取，能否重新得到刚才没处理完的 `1000-0`？

答案是：**不能只靠 `>`。**

`>` 表示请求当前消费者组**尚未交付的新消息**。它不会因为当前消费者名字与之前相同，就自动把历史待确认记录插回来。

为了验证这一点，我们重新准备一条消息：

```redis
DEL activity:1001:events
XADD activity:1001:events 1000-0 type signed-up user 42
XGROUP CREATE activity:1001:events dashboard 0-0
XREADGROUP GROUP dashboard dashboard-A COUNT 1 STREAMS activity:1001:events >
```

模拟 A 在确认前退出。

再由同名消费者读取**它自己的待确认历史**：

```redis
XREADGROUP GROUP dashboard dashboard-A COUNT 1 STREAMS activity:1001:events 0
```

注意最后一个参数不再是 `>`，而是明确的起始 ID `0`。

预期重新读到 `1000-0`。

这说明，消费者组读取实际上有两条相关但不同的路径：

```text
XREADGROUP ... >
    |
    +-- 获取组中尚未交付的新消息

XREADGROUP ... 0
    |
    +-- 读取当前消费者名下的未确认历史
```

但是，如果改由 `dashboard-B` 使用 `0` 去读：

```redis
XREADGROUP GROUP dashboard dashboard-B COUNT 1 STREAMS activity:1001:events 0
```

它不会直接读出 A 的待确认记录，因为该记录仍归属于 A。

这就给下一章留下了一个明确问题：**如果 A 永远无法重启，B 怎样接手 A 的消息？**

在讲接管机制之前，我们先确认“重读自己的 PEL”究竟会修改什么状态。

## 11. 源码四：重读历史消息，为什么交付次数会增加？

前面已经看到，正常的 `XREADGROUP ... >` 会进入 `streamReplyWithRange()`。

当 `XREADGROUP` 指定一个普通 ID（不是 `>`）时，Redis 7.2.0 会沿历史读取路径处理当前消费者的 PEL。

相关函数是：

[`streamReplyWithRangeFromConsumerPEL()`](https://github.com/redis/redis/blob/7.2.0/src/t_stream.c#L1686-L1719)。

这个函数的输入包含当前消费者 `consumer`，于是它可以直接遍历：

**真实源码节选：**

```c
raxStart(&ri,consumer->pel);
```

函数根据消息 ID 找到已经交付给该消费者的记录，然后尝试从 Stream 中取得对应正文。

如果正文仍然存在，它会更新这条待确认记录的交付时间和交付次数。

**真实源码节选：**

```c
nack->delivery_time = commandTimeSnapshot();
nack->delivery_count++;
```

这与重新执行以下命令的行为对应：

```redis
XREADGROUP GROUP dashboard dashboard-A COUNT 1 STREAMS activity:1001:events 0
XPENDING activity:1001:events dashboard - + 10
```

预期能看到 `1000-0` 的交付次数从 `1` 变为 `2`。

这里的交付次数不能直接理解成“业务处理失败次数”。

例如，我们还没有执行任何大屏更新逻辑，只是同一个消费者重复读取了自己的待确认消息，它的交付次数就会增加。

因此，这个字段只能说明**消息被交付的次数**，不是 Redis 已经验证过的业务失败次数。

还要注意一个边界：如果 Stream 正文已经被 `XDEL` 删除或因裁剪而消失，PEL 中可能仍保留对应 ID；此时历史读取可能只返回 ID 和空的字段内容。**保留待确认记录，不等于保证消息正文永远存在。**

## 12. 写一个真正遵循“处理成功后再确认”的消费者

前面的实验都是手工执行命令。现在回到活动业务，写一个最小 Python 消费者。

我们不调用真实的短信或数据库接口，而是模拟“刷新大屏统计”这一业务动作。这样实验不需要 API Key 或其他外部服务。

安装客户端：

```bash
python -m pip install "redis==5.0.1"
```

创建 `stream_ack_worker.py`：

```python
import sys
import redis
from redis.exceptions import ResponseError

STREAM = "activity:1001:events:ack-lab"
GROUP = "dashboard"
CONSUMER = "dashboard-A"

r = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)


def prepare():
    # 使用独立实验 Key，不覆盖之前 CLI 中的活动事件。
    r.delete(STREAM)
    r.xadd(STREAM, {"type": "signed-up", "user": "42"}, id="1000-0")
    r.xgroup_create(STREAM, GROUP, id="0-0")
    print("已准备活动事件与消费者组")


def process_event(message_id, fields):
    # 模拟大屏业务处理；并非真实数据库写入。
    print(f"模拟更新大屏: id={message_id}, user={fields['user']}")


def receive_one(history=False, ack=True):
    # ">" 领取新消息；"0" 重读当前消费者的待确认历史。
    position = "0" if history else ">"
    result = r.xreadgroup(
        GROUP,
        CONSUMER,
        {STREAM: position},
        count=1,
    )
    if not result:
        print("当前没有符合条件的消息")
        return

    _, messages = result[0]
    message_id, fields = messages[0]
    print("收到消息:", message_id, fields)

    if not ack:
        print("模拟进程在业务执行前退出：本次不处理，也不 XACK")
        return

    process_event(message_id, fields)
    print("XACK 返回:", r.xack(STREAM, GROUP, message_id))


def show_pending():
    print("XPENDING:", r.xpending(STREAM, GROUP))


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else ""

    if action == "prepare":
        prepare()
    elif action == "crash":
        receive_one(history=False, ack=False)
    elif action == "resume":
        receive_one(history=True, ack=True)
    elif action == "pending":
        show_pending()
    else:
        print("用法: python stream_ack_worker.py prepare|crash|pending|resume")
```

其中 `crash` 是一种**人为控制的失败注入**：脚本获取消息后直接结束，没有真正操作外部大屏，也没有提交 `XACK`。它验证的是 Redis 待确认记录的行为，不是实际进程崩溃时网络连接断开的全部表现。

依次执行：

```bash
python stream_ack_worker.py prepare
python stream_ack_worker.py crash
python stream_ack_worker.py pending
python stream_ack_worker.py resume
python stream_ack_worker.py pending
```

预期观察到：

1. `prepare` 写入固定 ID `1000-0`，创建 `dashboard` 组。
2. `crash` 收到 `1000-0` 但不执行确认。
3. 第一次 `pending` 显示待确认数量为 `1`。
4. `resume` 通过**同名消费者**的历史读取路径拿回 `1000-0`，模拟业务处理后执行 `XACK`，得到 `1`。
5. 第二次 `pending` 显示待确认数量为 `0`。

如果你把恢复阶段改成另一个消费者名，使用普通 ID `0` 并不能直接接管原消费者的 PEL。这是下一章要增加新机制的原因。

## 13. NOACK 能不能省掉这些状态？

既然维护 PEL 需要额外的状态，有人可能会问：

> 如果我们的消息不重要，只想提高消费过程的简单程度，能不能不保留这些待确认记录？

Redis 7.2.0 的 `XREADGROUP` 支持 `NOACK` 选项。

例如：

```redis
XREADGROUP GROUP dashboard dashboard-A NOACK COUNT 1 STREAMS activity:1001:events >
```

在这种情况下，Redis 可以推进该组的新消息交付位置，但不会为这次交付建立 PEL 条目。

这恰好对应我们在 `streamReplyWithRange()` 中见过的条件：

```c
if (group && !noack) {
    /* 创建待确认记录 */
}
```

这并不是更可靠的模式，而是**主动放弃这次交付的待确认跟踪**。

因此，对于必须确认处理完成的活动通知或统计任务，不能因为想少写一个 `XACK`，就随意改用 `NOACK`。

此外，`NOACK` 和 `XACK` 都不负责决定 Stream 消息正文何时删除。消息的删除或裁剪是另一组独立操作。

## 14. XACK 已经返回 1，就意味着业务一定成功了吗？

需要再明确一层边界。

Redis 看到的是：

```text
客户端发送 XACK activity:1001:events dashboard 1000-0
```

它无法自行检查消费者是否真的更新了大屏数据库。

如果应用**先 ACK，再执行业务**：

```text
收到消息 -> XACK -> 更新大屏
                      X 崩溃
```

Redis 已经清除了待确认记录，但业务更新可能还没发生。

如果应用**先执行业务，再 ACK**：

```text
收到消息 -> 更新大屏成功 -> XACK
                |              X 网络断开
                v
             业务已完成
```

Redis 可能仍然保留这条待确认记录；后续恢复机制就可能让它再次执行。

因此，正确的基本顺序通常是：**完成业务操作以后再确认消息**，同时让业务能够安全处理可能的重复交付。

但这不意味着 `XACK` 本身具有“业务恰好执行一次”的保证。

本章也不要把“处理成功后 ACK”误读成 Redis 与外部数据库建立了一个跨系统事务；两者仍然是独立操作。

这一章最重要的进展是：**Redis 现在能够记住“哪些消息还没有被消费者确认”，让恢复流程有记录可查。**

它仍然无法自动判断一个消费者是真的崩溃、处理很慢，还是只是暂时断开连接。

## 15. 解决确认之后，下一个问题自然出现

回到最开始的场景：

```text
用户 42 报名
    |
    v
Stream 记录 1000-0
    |
    v
XREADGROUP 交给 dashboard-A
    |
    v
组 PEL / A 的 PEL 都记录 1000-0
    |
    X A 永远无法恢复
```

我们现在已经能通过 `XPENDING` 看见它，知道它归属于 `dashboard-A`，还知道它距上次交付经过了多长时间。

但即使 `dashboard-B` 正常运行，使用：

```redis
XREADGROUP GROUP dashboard dashboard-B STREAMS activity:1001:events >
```

也不会自动拿到原本属于 A 的待确认消息。

这说明，**发现有一条未完成消息，与让另一个消费者接管这条消息，仍然是两个不同动作。**

接下来需要增加的能力不再是记录状态，而是**转移处理责任**。

我们已经在 PEL 中保存了消息 ID、所属消费者以及最近交付时间；这些信息正好可以用于寻找长时间没有确认的任务。

下一章，将从 `dashboard-A` 永久离线的实验开始，研究 Redis 为什么提供 `XCLAIM` 和 `XAUTOCLAIM`，以及另一个消费者如何在合理的超时条件下接手待确认消息。

**下一章：《消费者崩溃后，消息怎样被接管？从 PEL 到 XCLAIM 与 XAUTOCLAIM》**

---

## 本章回顾

```text
XREADGROUP 已经可以在组内分工
               |
               | 收到消息后崩溃
               v
          组游标已推进
          但业务没有完成
               |
               | 需要记住哪些消息尚未完成
               v
        PEL：待确认记录
               |
         +-----+-----+
         |           |
         v           v
      组 PEL      消费者 PEL
         |           |
         +-----+-----+
               |
               | 处理成功后由应用确认
               v
              XACK
               |
               v
        删除待确认记录
        Stream 正文仍保留
               |
               | 消费者永久离线？
               v
     下一章：任务接管与恢复
```

这一章最终建立了三个**互不等价**的状态概念：

| 状态 | 数据位置 | 回答的问题 |
|---|---|---|
| Stream 事件正文 | Stream 自身 | 发生过什么事件？ |
| 组的 `last_id` | `streamCG` | 这个组的新消息交付到了哪里？ |
| 组与消费者的 PEL | `streamCG` / `streamConsumer` | 哪些已交付事件还没有确认？ |

与前一章相比，我们没有改变消息历史的基本存储方式，只是为“交付后但尚未确认”增加了一个可查询、可清理的状态。

这份状态让我们能够解释 `XREADGROUP`、`XPENDING` 和 `XACK` 各自负责什么：

```text
XREADGROUP ... >
    -> 获取新消息
    -> 推进组交付位置
    -> 默认创建 PEL 记录

XPENDING
    -> 查询 PEL 中的待确认状态

XACK
    -> 从组 PEL 与消费者 PEL 中移除对应记录
    -> 不删除 Stream 消息正文
```

下次再看到一个消费者“收到消息却没有执行完”的问题，我们就知道：首先要确认这条消息是否留在 PEL 中，而不是仅仅检查 Stream 里是否还存在这条事件。

## 源码与文档参考

本章源码版本固定为 **Redis 7.2.0**。

- [`src/stream.h`：`streamCG`、`streamConsumer`、`streamNACK`](https://github.com/redis/redis/blob/7.2.0/src/stream.h#L49-L90) —— PEL 的两级索引与待确认记录字段。
- [`src/t_stream.c`：`streamReplyWithRange()`](https://github.com/redis/redis/blob/7.2.0/src/t_stream.c#L1564-L1671) —— 新消息交付时更新组位置并建立 PEL 记录。
- [`src/t_stream.c`：`streamCreateNACK()`](https://github.com/redis/redis/blob/7.2.0/src/t_stream.c#L2284-L2293) —— 初始化交付时间、次数和所属消费者。
- [`src/t_stream.c`：`xpendingCommand()`](https://github.com/redis/redis/blob/7.2.0/src/t_stream.c#L2675-L2843) —— 读取组或消费者 PEL 信息。
- [`src/t_stream.c`：`xackCommand()`](https://github.com/redis/redis/blob/7.2.0/src/t_stream.c#L2623-L2674) —— 同时删除两级 PEL 索引，返回确认数量。
- [`src/t_stream.c`：`streamReplyWithRangeFromConsumerPEL()`](https://github.com/redis/redis/blob/7.2.0/src/t_stream.c#L1686-L1719) —— 消费者重读自身未确认消息的路径。
- [XREADGROUP 官方文档](https://redis.io/docs/latest/commands/xreadgroup/)
- [XPENDING 官方文档](https://redis.io/docs/latest/commands/xpending/)
- [XACK 官方文档](https://redis.io/docs/latest/commands/xack/)

> **验证声明**：以上源码位置、关键语句与命令语义依据 Redis 7.2.0 静态核查；Docker、CLI 与 Python 示例提供可复现步骤及预期现象。由于本篇编写时未运行 Redis 7.2.0 实例，因此不宣称实验结果已经实测。线上最新命令文档可能包含高于 7.2 的参数，本篇只使用 Redis 7.2.0 支持的命令形式。
