---
title: "第十五章：订阅者离线后，怎样补读错过的事件？从 Pub/Sub 到 Streams"
sidebar_label: "15 - Streams 与消息续读"
sidebar_position: 16
description: "沿活动报名事件，先复现 Pub/Sub 离线消息无法补读，再从 XADD、消息 ID 和 XREAD 逐步构建可续读事件流，精读 Redis 7.2.0 的源码与运行路径。"
tags:
  - Redis
  - 源码精读
  - Streams
  - XADD
  - XREAD
---

# 第十五章：订阅者离线后，怎样补读错过的事件？从 Pub/Sub 到 Streams

上一章，我们已经把活动报名事件交给 Redis Pub/Sub 广播。

用户 `42` 报名活动 `1001` 后，报名服务向频道发布 `signed-up:user:42`。在线的通知服务、大屏服务都会收到它，而报名服务不必知道到底有哪些订阅者。

这解决了一个真实问题：**同一件事发生后，多个服务都要知道，而不是像 List 那样争抢同一条任务。**

但上一章留下了一个没有解决的问题：

> 大屏服务在维护期间断开连接。此时有三个用户完成报名。等大屏服务重新上线，它怎么知道自己错过了哪些事件？

本章只围绕这个问题推进。我们先尝试最小补救方案，再研究为什么需要保存事件及其位置，最后进入 Redis 7.2.0 源码，追踪 `XADD` 写入、`XREAD` 从指定位置读取的真实路径。

> **版本与证据约定**：源码固定为 [Redis 7.2.0](https://github.com/redis/redis/tree/7.2.0)，以 [`src/t_stream.c`](https://github.com/redis/redis/blob/7.2.0/src/t_stream.c) 和 [`src/stream.h`](https://github.com/redis/redis/blob/7.2.0/src/stream.h) 为主。文中“真实源码节选”只引用核对过的关键语句；“教学化简”与“概念伪代码”均不冒充源码。所有命令输出均是 **预期结果**，本文制作环境没有运行 Redis 7.2.0 服务。

## 1. Pub/Sub 已经能广播，为什么服务恢复后仍看不到过去？

假设活动报名服务发布三条事件：

```redis
PUBLISH activity:1001:events signed-up:user:42
PUBLISH activity:1001:events signed-up:user:57
PUBLISH activity:1001:events signed-up:user:63
```

现在通知服务始终在线，大屏服务在第二条事件之前断线：

```text
时间  →

报名服务       42 成功报名     57 成功报名     63 成功报名
                  |              |              |
                  v              v              v
Pub/Sub 频道    event-42       event-57       event-63
                  |              |              |
通知服务       收到 42        收到 57        收到 63
大屏服务       收到 42        [ 离线 ]        [ 离线 ]
                                                  |
                                               重新订阅
```

当大屏服务再次执行 `SUBSCRIBE` 时，它只能等待此后发布的消息。`PUBLISH` 不会替离线订阅者保留消息历史。

此时有一种朴素的补救方式：通知服务收到报名消息时，顺手写入一个 List；大屏上线后再读取这个 List。

但这并没有真正让**每一个消费方**都能自由补读历史：

- 如果大屏和审计服务都用 `RPOP` 读这个 List，消息又会变成竞争消费。
- 如果为每个服务分别建 List，生产方就得维护越来越多的目标列表，或者另写一个可靠分发程序。
- 如果消费方想问“我上次读到用户 42，此后发生了什么”，普通 List 没有一个天然的、与消息绑定的读取位置。

所以，当前缺少的并不只是“把消息保存下来”。

**我们还需要给每一条事件一个可定位的位置，让不同消费者能够记住自己读到了哪里。**

这就把问题从“即时广播”变成了“可重新读取的事件记录”。

## 2. 先不用 Redis Streams，自己设计一个最小事件记录

如果只有一个 Python 进程，完全可以先这样写：

```python
# 概念伪代码：用于推导设计，不是 Redis 源码
history = []

def append_event(event):
    event_id = len(history) + 1
    history.append((event_id, event))
    return event_id

def read_after(last_id):
    return [item for item in history if item[0] > last_id]
```

假设保存三条报名事件：

```text
(1, signed-up:user:42)
(2, signed-up:user:57)
(3, signed-up:user:63)
```

通知服务可以从 `0` 开始读取全部事件。大屏服务则把自己最后处理完成的事件位置保存为 `1`，重新上线后请求 `read_after(1)`：

```text
大屏最后处理到 1
        |
        v
read_after(1)
        |
        +--> (2, signed-up:user:57)
        +--> (3, signed-up:user:63)
```

这时即使大屏服务在事件发生时不在线，**只要历史数据仍然存在，且它知道上次的位置，就可以重新读取。**

这个最小设计已经清楚地暴露出两个必要机制：

1. 写入时，事件必须留下来，而不是只发送给在线客户端。
2. 读取时，消费者必须能根据上次的位置请求后续事件。

下面才有必要引入 Redis Streams。

## 3. 用 XADD 保存报名事件

Redis Streams 可以在一个 Stream Key 下保存按 ID 排序的多条记录。每条记录包含一个 ID，以及若干字段和值。

我们把刚才的抽象映射到 Redis：

```text
append_event(event)   ->   XADD
read_after(last_id)   ->   XREAD
```

先做一个最小实验。为了让读者的实验输出可以逐条对照，我们**暂时手动指定 ID**；这些 `1000-*` 只是教学用标识，不代表实际事件时间。

打开 Redis CLI：

```bash
# 首次运行时启动；若已有同名容器，请复用而不是重复创建
docker run -d \
  --name redis-source-lab \
  -p 127.0.0.1:6379:6379 \
  redis:7.2.0 \
  --save "" \
  --appendonly no

docker exec -it redis-source-lab redis-cli
```

这里关闭 RDB 和 AOF 仅为了隔离本章实验。前面已经学过：这意味着 Redis 进程故障时，不能期待未持久化的事件被恢复。

在 `redis-cli` 中先清理实验 Key：

```redis
DEL activity:1001:stream
```

依次追加三个事件：

```redis
XADD activity:1001:stream 1000-0 kind signed_up user_id 42
XADD activity:1001:stream 1000-1 kind signed_up user_id 57
XADD activity:1001:stream 1001-0 kind signed_up user_id 63
```

预期每条 `XADD` 返回对应的 ID：

```text
"1000-0"
"1000-1"
"1001-0"
```

现在用 `XRANGE` 看历史记录：

```redis
XRANGE activity:1001:stream - +
```

预期会按 ID 顺序看到三条事件及其字段：

```text
1000-0 -> kind=signed_up, user_id=42
1000-1 -> kind=signed_up, user_id=57
1001-0 -> kind=signed_up, user_id=63
```

上面采用**教学化简的输出排版**。实际 `redis-cli` 会使用嵌套数组格式展示 ID 和字段值。

对比上一章的 `PUBLISH`：这里的 `XADD` 不要求通知服务或大屏服务必须先在线。即使没有消费者连接，记录仍然留在 Stream 中。

但光是保存历史还不够。我们得能回答：“大屏服务上次读到 `1000-0`，它接下来应当读到哪些事件？”

## 4. 用 XREAD 从上一次的位置接着读取

现在执行：

```redis
XREAD STREAMS activity:1001:stream 1000-0
```

`XREAD` 最后一个参数不是“本次从哪个 ID 开始（包含）”，而是：**我已经读到哪个 ID，请返回比它更大的记录。**

因此，这条命令会返回：

```text
1000-1 -> user_id=57
1001-0 -> user_id=63
```

仍然是教学化简的输出排版。

如果想从最开始读取：

```redis
XREAD STREAMS activity:1001:stream 0-0
```

预期返回三条记录。

如果只想一次取一条：

```redis
XREAD COUNT 1 STREAMS activity:1001:stream 0-0
```

预期只返回 `1000-0`。

接下来将位置更新成刚刚返回的 ID，再读取：

```redis
XREAD COUNT 1 STREAMS activity:1001:stream 1000-0
```

预期这一次返回 `1000-1`。

注意，普通 `XREAD` **不会因为读取消息就把消息从 Stream 删除**。所以，如果通知服务从 `0-0` 读取，大屏服务也从 `0-0` 读取，两者都能看到同一段历史。

这正好满足当前需求：不同服务可以有自己的进度。

```text
                    activity:1001:stream

            1000-0 -> 1000-1 -> 1001-0
               ^         ^          ^
               |         |          |
          通知服务      大屏服务     最新事件
          读到 1000-0  读到 1000-1
```

但此时有个问题值得追问：为什么 Stream 能准确定位“比这个 ID 更大的记录”？我们去源码里看 ID 是怎样产生和保存的。

## 5. 为什么每条消息必须有 ID？先看内部结构

本章第一次需要查看 Stream 的数据结构。

源码位置：`src/stream.h`，`streamID`。

**真实源码节选：**

```c
typedef struct streamID {
    uint64_t ms;
    uint64_t seq;
} streamID;
```

一个 Stream ID 由两个 64 位整数构成：

- `ms`：毫秒部分，自动生成时通常来源于 Redis 服务器时间。
- `seq`：序号部分，用来区分同一毫秒内的不同记录。

所以我们在命令行看到的 `1000-0`、`1000-1`，正好对应了这两个部分。

这里不应误解为“Stream ID 一定等于业务事件的真实发生时间”。例如客户端可以显式指定合法的 ID，或者服务器时钟回拨时，Redis 仍然需要保证新 ID 的递增顺序。

**真正重要的性质是：在一个普通 Redis Stream 中，新加入的消息 ID 必须大于此前已经使用的最大 ID。**

因此客户端可以只记住一个 ID，不需要记住“第几个数组下标”或“某个内存地址”。

当同一个 Stream 的多条记录按 ID 排序时，`XREAD` 才能够稳定表达“继续读取上一个位置之后的记录”。

## 6. 源码：XADD 怎样找到或创建 Stream？

现在追踪这条请求：

```redis
XADD activity:1001:stream 1000-0 kind signed_up user_id 42
```

上一章已经研究过命令分发，因此这里直接从 `xaddCommand()` 开始。

源码位置：`src/t_stream.c`，`xaddCommand()`。

在解析参数后，它首先需要找到对应的 Stream；如果 Key 不存在，普通 `XADD` 应当可以创建它。

**真实源码节选：**

```c
if ((o = streamTypeLookupWriteOrCreate(c,c->argv[1],parsed_args.no_mkstream)) == NULL) return;
```

`streamTypeLookupWriteOrCreate()` 的职责非常明确：

- 查找 Key 对应的现有对象；
- 确认它是 Stream 类型；
- 如果 Key 不存在、且没有指定 `NOMKSTREAM`，就创建新的 Stream 对象。

在这里，上一章学过的“一个 Redis Key 对应某个数据对象”仍然成立。只是这次，Value 不再是普通的 String 或 Hash，而是 Stream 对象。

当 Key 不存在时，内部通过 `createStreamObject()` 创建对象，并用 `dbAdd()` 放入 Redis 数据库。这一层结构与此前的 Hash、List 章节能够直接对照。

新的问题是：**Stream 创建好了，新消息具体写到哪里？**

## 7. 源码：XADD 为什么要调用 streamAppendItem()？

在 `xaddCommand()` 中，完成查找或创建之后，实际追加消息的入口是 `streamAppendItem()`。

为了避免把整段分支函数当作源码堆砌，我们只追踪当前普通 `XADD` 的正常路径：

```text
xaddCommand()
    |
    +-- 解析 ID、字段及可选参数
    |
    +-- streamTypeLookupWriteOrCreate()
    |       |
    |       +-- 已存在 -> 返回 Stream
    |       +-- 不存在 -> 创建 Stream
    |
    +-- streamAppendItem()
            |
            +-- 确定新消息 ID
            +-- 检查 ID 必须递增
            +-- 追加字段与值
            +-- 更新 Stream 状态
```

`streamAppendItem()` 位于同一个文件 `src/t_stream.c`。我们重点关注其 ID 校验逻辑：

**真实源码节选：**

```c
if (streamCompareID(&id,&s->last_id) <= 0) {
    errno = EDOM;
    return C_ERR;
}
```

为什么这个检查必不可少？

假设当前最新消息是 `1001-0`。如果之后又允许插入 ID 为 `1000-9` 的新消息，那么消费者记住 `1001-0` 后，使用 `XREAD` 就不会再发现这条被插入到旧位置的记录。

这会直接破坏“从上次 ID 接着读”这一设计。

所以 ID 递增不是装饰性元数据，而是续读语义能够成立的关键条件。

可以验证这个边界：

```redis
XADD activity:1001:stream 1000-9 kind signed_up user_id 99
```

由于先前已经写入 `1001-0`，这里预期返回类似错误：

```text
(error) ERR The ID specified in XADD is equal or smaller than the target stream top item
```

这条失败写入不会为 Stream 增加新记录。

### 如果没有指定 ID，Redis 又怎样生成？

真正的业务中，通常不会手动为每条报名事件指定 Stream ID，而是执行：

```redis
XADD activity:1001:stream * kind signed_up user_id 70
```

源码中的 `streamNextID()` 负责计算自动 ID：

```text
如果当前毫秒数大于上一个 ID 的毫秒部分：
    新 ID = 当前毫秒数 - 0
否则：
    在上一个 ID 的基础上递增序号
```

**以上是教学化简，不是原始 C 源码。** 源码位置：`src/t_stream.c`，`streamNextID()`。

这样即使同一毫秒内连续产生多条消息，或者系统时间发生回拨，ID 也能维持单个 Stream 的递增关系。

这里先不要深入时间溢出、手动指定部分 ID 等分支；它们是 ID 生成的边界条件，不影响当前的正常路径理解。

## 8. 为什么 Stream 没有直接保存成一个普通 List？

到这里，我们已经知道 Redis 为消息分配了 ID，并且要求 ID 递增。

假如把所有历史消息简单追加到一个 List，然后从头遍历查找指定 ID，理论上也能实现 `read_after(last_id)`。

但现在考虑一个更大的活动：Stream 中已经保存数百万条报名事件。大屏服务昨天读取到了某个很靠后的 ID，今天希望从这里继续。

如果每次都从头遍历，就会反复经过大量已经读过的记录。

于是出现了新的需求：**保留追加顺序的同时，还要能够根据 ID 高效定位附近的记录。**

Redis 7.2.0 并没有把整个 Stream 直接实现成一个普通 List。

在 `src/stream.h` 的 `stream` 结构中，重要的字段包括：

```text
rax         -> 按 ID 组织数据块的基数树
length      -> 当前有效记录数
last_id     -> 最近一次追加使用的最大 ID
first_id    -> 当前第一条有效记录 ID
cgroups     -> 消费者组信息（下一章研究）
```

这里的 `rax` 是 radix tree（基数树）。

但也不要误认为：“每条 Stream 事件就是基数树里的一个独立节点。”

Redis 的实际做法是：**基数树索引一批批存储消息的数据块，每个数据块内部使用紧凑的 `listpack` 编码。**

可以教学化地画成：

```text
Stream
  |
  v
rax（按 ID 定位数据块）
  |
  +-- 块 A（起始 ID：1000-0）
  |       |
  |       +-- listpack: 1000-0、1000-1
  |
  +-- 块 B（起始 ID：1001-0）
          |
          +-- listpack: 1001-0、...
```

注意，上图是概念示意：实际一个块包含多少条消息取决于大小和条目数量限制，并不表示前面三个实验事件一定会被拆成两块。

为什么不让每一条消息都成为独立的树节点？

因为很多业务事件很小，并且具有相似的字段结构。将多条消息紧凑存放在 `listpack` 中，可以降低大量小消息分别分配对象的开销；外层的基数树仍然提供按 ID 范围查找的能力。

而在 `streamAppendItem()` 中，Redis 还会比较当前消息的字段名与同块的字段模板。如果字段名一致，存储时可以省略重复的字段名，只保存相关标记和字段值。

我们最开始的报名事件：

```text
kind=signed_up, user_id=42
kind=signed_up, user_id=57
kind=signed_up, user_id=63
```

恰好具有相同的字段名。这让这种紧凑存储方式变得容易理解。

本章不继续深入 `listpack` 的全部字节布局。我们只需要建立一个关键认识：

**Stream 的内部结构既要适合持续追加，也要适合根据消息 ID 定位和读取历史。**

## 9. 源码：XREAD 怎样读“这个 ID 之后”的消息？

现在转到消费者。

假设大屏服务上次保存的 ID 是 `1000-0`，它发送：

```redis
XREAD STREAMS activity:1001:stream 1000-0
```

源码入口是 `src/t_stream.c` 的 `xreadCommand()`。

注意，这个函数同时处理 `XREAD` 和 `XREADGROUP`。后者涉及消费者组，这一章只沿普通 `XREAD` 路径前进。

首先，它会解析 `STREAMS` 后面的 Key 列表，以及与每个 Key 对应的起始 ID。

然后找到 Stream，并判断当前最大有效 ID 是否大于客户端传入的 ID。

对于我们的例子：

```text
客户端提供：1000-0
Stream 最大：1001-0

1001-0 > 1000-0
=> 有可能立即返回新记录
```

接着出现一个很重要的细节：内部范围读取函数接受的是**包含起点**的范围，但普通 `XREAD` 应返回**严格大于**客户端所给 ID 的事件。

因此，`xreadCommand()` 需要先把起始 ID 往前推进一个位置。

**真实源码节选：**

```c
streamID start = *gt;
streamIncrID(&start);
```

对 `1000-0` 来说，得到的起点是 `1000-1`。然后由 `streamReplyWithRange()` 读取并返回从这个位置开始的历史记录。

再往下，`streamReplyWithRange()` 利用 Stream 迭代器扫描满足条件的消息，并构造回复；`streamIteratorStart()` 负责把请求中的 ID 范围映射成基数树的查找范围。

于是，我们已经能把“按 ID 续读”与真实实现连接起来：

```text
XREAD STREAMS activity:1001:stream 1000-0
                |
                v
           xreadCommand()
                |
                +-- 解析 Stream Key 和 ID
                |
                +-- 查找 Stream
                |
                +-- 判断是否存在更大的 ID
                |
                +-- 将起点推进到 1000-1
                |
                v
       streamReplyWithRange()
                |
                v
        streamIteratorStart()
                |
                v
          按 ID 查找数据块
                |
                v
           逐条读取字段值
                |
                v
       返回 1000-1、1001-0
```

更重要的是，这条纯 `XREAD` 路径主要完成查询和构造回复，**不会像 `RPOP` 那样删除返回的消息。**

因此，大屏读到某条消息，不会妨碍通知服务从自己的进度读取同一条消息。

## 10. 没有新消息时，消费者要反复查询吗？

现在大屏服务已经追上了最新位置 `1001-0`。

如果立即执行：

```redis
XREAD STREAMS activity:1001:stream 1001-0
```

由于当前没有更大的 ID，普通 `XREAD` 会返回空结果。

如果程序每隔一小段时间重新发送一次，就又出现了以前 List 章节遇到的空轮询问题。

但在上一轮阅读 Redis 网络与阻塞机制后，我们已经知道：客户端等待数据不一定要让整个 Redis 进程等待。

这时 `XREAD` 提供 `BLOCK` 选项：

```redis
XREAD BLOCK 5000 STREAMS activity:1001:stream 1001-0
```

含义是：如果目前没有满足条件的新事件，最多等待 5000 毫秒；期间一旦有新的、ID 更大的消息进入 Stream，就尝试返回。

在 Redis 7.2.0 的 `xreadCommand()` 中，如果当前无法立即满足请求、且指定了阻塞超时，就通过 `blockForKeys()` 注册等待；而 `xaddCommand()` 成功追加之后，会通过 `signalKeyAsReady()` 标记这个 Stream Key 有新数据。

这里不再完整展开 `blocked.c` 的调度细节，因为本章的唯一主问题是“如何补读离线期间的事件”。只需要确认：**Streams 的消费既可以读历史，也可以在追上历史后等待未来的新事件。**

还有一个很容易误用的特殊 ID：`$`。

```redis
XREAD BLOCK 5000 STREAMS activity:1001:stream $
```

`$` 的意思是“从执行这一调用时的 Stream 当前末尾开始，只等未来的新事件”。这对只关心实时更新的程序很方便，但**它不会补读过去已经存在的历史**。

所以，如果我们的目标是恢复离线期间错过的报名事件，就不能在每次重连时都重新使用 `$`。

正确的续读思路是：

```text
首次启动：使用明确的起点（例如 0-0）
读到消息：记录最后成功处理的 ID
断线重连：从保存的 ID 再次 XREAD
```

如果每次都使用 `$`，这个系统会悄悄重新退化成“只看上线后的新消息”，失去我们本章想获得的补读能力。

## 11. 实验：消费者离线后，重新上线补读历史

现在我们用固定 ID 做一个可以直接复制的完整实验。

### 终端 A：模拟报名服务

在 `redis-cli` 中重置 Stream，并写入第一条报名事件：

```redis
DEL activity:1001:stream
XADD activity:1001:stream 1000-0 kind signed_up user_id 42
```

### 终端 B：模拟大屏服务第一次读取

执行：

```redis
XREAD STREAMS activity:1001:stream 0-0
```

预期读取到用户 `42` 的报名事件，ID 为 `1000-0`。

把这个 ID 记在纸上即可——这里故意不先引入额外的数据库来保存消费进度。

随后**停止终端 B 的消费操作**，模拟大屏服务离线。

### 终端 A：大屏离线期间继续发布

```redis
XADD activity:1001:stream 1000-1 kind signed_up user_id 57
XADD activity:1001:stream 1001-0 kind signed_up user_id 63
```

### 终端 B：恢复后从旧位置续读

重新连接 Redis，执行：

```redis
XREAD STREAMS activity:1001:stream 1000-0
```

预期返回 `1000-1` 和 `1001-0` 两条记录，分别对应用户 `57` 和 `63`。

再检查一下读取是否删除了历史：

```redis
XLEN activity:1001:stream
```

预期结果：

```text
(integer) 3
```

这三个事件依然存放在 Stream 中。

现在使用另一台终端模拟通知服务，从头读取：

```redis
XREAD STREAMS activity:1001:stream 0-0
```

通知服务仍然能够得到全部三条事件。

最后，试试看如果重新连接时错误地使用 `$`：

```redis
XREAD BLOCK 1000 STREAMS activity:1001:stream $
```

在此期间不再 `XADD` 的话，预期约 1 秒后返回空结果。即使 Stream 中仍然有三条历史消息，`$` 也不会把它们补给消费者。

**现在我们终于能通过实验区分“保存了历史”和“消费方是否正确选择了读取位置”。**

## 12. 把“上次处理到哪里”放进一个真正的消费者

手工记住 `1000-0` 可以证明机制成立，但实际大屏程序还需要在重启后保留上次位置。

下面给一个最小 Python 实验。它会使用本地文件保存最后处理完成的 Stream ID。这个文件只是教学用的进度记录，不是分布式消费协调方案。

安装客户端：

```bash
python -m pip install "redis==5.0.1"
```

创建 `stream_screen.py`：

```python
from pathlib import Path
import redis

STREAM = "activity:1001:stream"
CURSOR_FILE = Path(".screen-stream-cursor")

client = redis.Redis(
    host="127.0.0.1",
    port=6379,
    decode_responses=True,
)


def load_cursor():
    if not CURSOR_FILE.exists():
        return "0-0"
    return CURSOR_FILE.read_text(encoding="utf-8").strip()


def save_cursor(last_id):
    temporary = CURSOR_FILE.with_suffix(".tmp")
    temporary.write_text(last_id, encoding="utf-8")
    temporary.replace(CURSOR_FILE)


def update_screen(event_id, fields):
    # 模拟大屏更新；这里没有真实外部副作用
    print(f"更新大屏：id={event_id}, user={fields['user_id']}")


last_id = load_cursor()
print("从位置恢复：", last_id)

try:
    while True:
        result = client.xread(
            {STREAM: last_id},
            count=10,
            block=2000,
        )

        for stream_name, entries in result:
            for event_id, fields in entries:
                update_screen(event_id, fields)
                save_cursor(event_id)
                last_id = event_id
except KeyboardInterrupt:
    print("消费者退出，最后进度：", last_id)
```

这个程序的核心不是 Python 语法，而是三步：

```text
读取上次位置
    |
    v
XREAD 从该位置之后继续
    |
    v
处理一条事件
    |
    v
保存刚处理的事件 ID
```

运行它：

```bash
python stream_screen.py
```

首次运行会从 `0-0` 开始，因此能读取目前保留的全部事件。

程序运行时在另一个终端继续写入：

```bash
docker exec redis-source-lab redis-cli \
  XADD activity:1001:stream '*' kind signed_up user_id 70
```

此处 `'*'` 加了 shell 引号，避免在 shell 中被当作通配符展开；Redis 收到的仍是 `*`。

消费者应该打印新消息的 ID 和用户 `70`。结束程序后，`.screen-stream-cursor` 文件中会记录最近一次处理后的 ID。

接下来停止大屏程序，再发布一条消息：

```bash
docker exec redis-source-lab redis-cli \
  XADD activity:1001:stream '*' kind signed_up user_id 81
```

然后重新运行 `python stream_screen.py`。

它应当从文件保存的上一次 ID 继续，补读用户 `81` 的事件，而不是从头重复打印全部历史，也不是只等待未来事件。

> **实验前提**：在上述程序运行与重启期间，Redis 中的 Stream Key 不能被删除、裁剪到丢失需要补读的记录，也不能因为我们关闭了持久化而在 Redis 自身异常退出后丢失。这些是补读能力成立所需的条件。

### 这段消费者已经“可靠”了吗？

还没有。

假设 `update_screen()` 已经真正更新了外部业务系统，但进程在 `save_cursor()` 之前崩溃。

重启后程序仍会从旧 ID 读取，导致这条事件再次处理。

反过来，如果我们先写进度，再执行外部业务操作，进程也可能在保存进度后、执行操作前崩溃，从而跳过一条实际没有处理的事件。

这说明：**保存读取位置解决的是“我怎样找到错过的记录”，并不自动保证“每条业务事件恰好处理一次”。**

这里我们先明确边界，不提前引入完整的业务事务、幂等或消费者确认体系。

因为接下来还有一个更直接的问题：多个大屏处理实例同时工作时，应该怎样分工？

## 13. 已能恢复历史，为什么还需要下一层机制？

现在报名事件能够保存在 Stream 中，通知服务与大屏服务也能够各自保存进度。

但假设活动越来越火，大屏服务需要启动两个工作实例共同处理事件：

```text
                 activity:1001:stream
                         |
                +--------+--------+
                |                 |
            大屏实例 A        大屏实例 B
```

如果两个实例都使用相同的最后 ID 执行普通 `XREAD`，会发生什么？

它们会各自读到这段历史记录。

这对**不同服务都需要完整事件**的场景很有用；但对**同一个服务的多个实例想分担工作**的场景来说，就会造成重复劳动。

我们需要给这个需求增加一个新约束：

- 通知服务与大屏服务应当分别读取自己需要的全部事件；
- 但在大屏服务内部，实例 A、B 应当能够分担新消息；
- 某个实例拿到消息后，还需要区分“已经收到”和“真正处理完成”。

普通 `XREAD` 并不负责为多个消费者协调这种任务归属。

这才是 Redis Streams **消费者组**值得出现的地方。

下一章，我们将从两个实例读取相同事件的具体反例出发，研究 `XGROUP` 和 `XREADGROUP` 如何让多个消费者分工；在读者理解分工之后，才进一步研究未确认消息与故障恢复。

**下一章：《同一个服务有多个消费者，消息该交给谁？从 XREAD 到消费者组》**

---

## 本章回顾

我们的设计仍然只比上一章多走了一步：

```text
Pub/Sub 能把事件广播给当前在线订阅者
                    |
                    | 订阅者离线时错过消息
                    v
需要保存事件历史
                    |
                    | 只保存历史还不够
                    v
每条事件需要稳定、递增的 ID
                    |
                    v
XADD 追加记录并分配 ID
                    |
                    | 消费方要补读某个位置之后的事件
                    v
XREAD 按 ID 读取后续记录
                    |
                    | 没有新记录时无需持续空查询
                    v
XREAD BLOCK 等待新事件
                    |
                    | 断线重连不能每次都从 $ 开始
                    v
消费者保存上次处理的 ID
                    |
                    | 同一服务的多个实例会读到重复消息
                    v
下一章：Streams 消费者组
```

本章真正理解了四件事：

1. **Pub/Sub 与 Streams 不只是“旧方案升级成新方案”**：Pub/Sub 是在线广播；Streams 保存可重读的事件历史，适用需求不同。
2. **ID 是续读的前提**：消费者可以用一个位置表达“返回后面的事件”，而无需重新从头扫描所有逻辑记录。
3. **读取不等于删除**：普通 `XREAD` 不会消费掉 Stream 数据，不同服务可以各自维护读取位置。
4. **可补读不等于端到端可靠处理**：历史可能被裁剪或丢失，游标可能没有保存，业务副作用也可能在重试时重复执行。

我们已经让“大屏离线后能追上事件历史”成为了一个有明确前提、可以用命令验证的能力。下一章才考虑在同一服务内协调多个消费者。

## 源码与文档参考

本文固定使用 Redis **7.2.0**，避免引用新版本才支持的 Streams 扩展选项。

- [Redis 7.2.0：src/t_stream.c](https://github.com/redis/redis/blob/7.2.0/src/t_stream.c) — `xaddCommand()`、`streamAppendItem()`、`streamNextID()`、`xreadCommand()`、`streamReplyWithRange()`、`streamIteratorStart()`。
- [Redis 7.2.0：src/stream.h](https://github.com/redis/redis/blob/7.2.0/src/stream.h) — `streamID`、`stream` 结构与 Stream 迭代器。
- [XADD 命令参考](https://redis.io/docs/latest/commands/xadd/) — 写入记录、消息 ID 与追加行为；注意在线文档可能包含高于 7.2 的新选项。
- [XREAD 命令参考](https://redis.io/docs/latest/commands/xread/) — 读取 ID 之后的记录、`COUNT`、`BLOCK`、`$` 的语义；注意新版本扩展选项。
- [Redis Streams 官方介绍](https://redis.io/docs/latest/develop/data-types/streams/) — Stream 的用途、保留和消费模型。

**验证声明**：本章的命令行为与源码入口已按 Redis 7.2.0 进行静态核对，所有预期输出均未冒充为已实测；如果需要复现，请按文中 Docker 实验运行。
