---
title: "第十六章：同一个服务有多个消费者，消息该交给谁？从 XREAD 到消费者组"
sidebar_label: "16 - Streams 消费者组"
sidebar_position: 17
description: "沿活动报名事件，从两个大屏实例重复读取相同消息出发，引入消费者组，精读 Redis 7.2.0 的 XGROUP、XREADGROUP、组游标和消息分配路径。"
tags:
  - Redis
  - 源码精读
  - Streams
  - 消费者组
  - XREADGROUP
---

# 第十六章：同一个服务有多个消费者，消息该交给谁？从 XREAD 到消费者组

上一章，我们让活动报名事件不再只存在于 Pub/Sub 的一次性推送中，而是写入 Redis Streams。每一条事件拥有自己的 ID，大屏服务即使离线，也可以根据上次读取的位置补读历史。

这解决了一个真实问题：**消息不必恰好在消费者在线时发布，消费者仍有机会读到已保存的历史。**

现在活动越来越火，大屏服务需要同时维护实时统计、展示新报名用户名、刷新参与人数。一个进程处理不过来，我们启动两个大屏实例：`dashboard-A` 和 `dashboard-B`。

两个实例都监听同一个 Stream：`activity:1001:events`。

但它们只是想**分担大屏服务的处理工作**，并不希望把每条报名事件处理两遍。与此相对，通知服务依然需要独立收到完整的报名事件历史。

新的问题出现了：

> 同一条 Stream 里既要让通知服务和大屏服务各自看到事件，又要让大屏服务的两个实例只分担各自的一部分消息。Redis 怎么知道应该把消息交给谁？

本章只解决这一个问题。先观察普通 `XREAD` 的行为，再从最小共享进度的直觉设计出发，进入 Redis 7.2.0 的 `XGROUP` 和 `XREADGROUP` 源码，最后用可以重复执行的实验验证消息在同组、不同组之间的分配方式。

> **阅读约定**：本章锁定 [Redis 7.2.0](https://github.com/redis/redis/tree/7.2.0)。真实源码均来自这一版本的 [`src/t_stream.c`](https://github.com/redis/redis/blob/7.2.0/src/t_stream.c) 或 [`src/stream.h`](https://github.com/redis/redis/blob/7.2.0/src/stream.h)；代码若作简化，会明确注明。命令结果是根据协议和源码推导的**预期结果**，不是声称在写作环境中运行过的截图或实测记录。

## 1. 两个 XREAD 消费者，为什么会处理同一条消息？

沿用活动 `1001` 的报名事件。假设 Redis 中已经存有三条记录：

```text
Stream: activity:1001:events

1000-0 -> signed-up, user=42
1001-0 -> signed-up, user=57
1002-0 -> signed-up, user=63
```

这些 ID 为了实验可重复，采用固定值。真实生产环境通常会让 `XADD *` 自动生成 ID。

大屏服务的两个实例都从 `0-0` 开始读取：

```redis
XREAD COUNT 3 STREAMS activity:1001:events 0-0
```

`dashboard-A` 读到了三条；`dashboard-B` 也执行相同的命令，同样会读到三条。

这不是 Redis 出现了错误，而是普通 `XREAD` 的语义决定的：**它按调用者提供的 ID 返回后续记录，并不主动为不同调用者分配任务。**

```text
                     Redis Stream
             1000-0  1001-0  1002-0
                       |
            +----------+----------+
            |                     |
            v                     v
      dashboard-A            dashboard-B
       读取 3 条               读取 3 条
```

在上一章，这是优点。通知服务与大屏服务各自维护自己的读取位置，互不干扰。

但现在两个大屏实例**属于同一个服务**，它们本来是为了分担任务而扩容。如果每个实例都读三条，同一个业务动作就可能重复执行。

因此我们现在需要区分两层关系：

- **服务之间**：通知服务和大屏服务都要各自得到完整事件。
- **同一服务的实例之间**：`dashboard-A` 和 `dashboard-B` 要分担大屏服务应处理的新事件。

这不是单纯的广播问题，也不是简单地把数据从 Stream 删除就能解决的问题。

## 2. 先尝试一个最小设计：共享一个读取位置

先不使用 Redis 消费者组，看看我们自己会如何解决。

普通 `XREAD` 的行为取决于“从哪个 ID 之后开始读取”。如果两个大屏实例使用同一个位置，而不是各存一份游标，会怎样？

设想我们为大屏服务保存一个共享的 `last_id`：

```python
# 概念伪代码：不是 Redis 源码，也不是正确的并发实现

group_last_id = "0-0"

def take_next_event():
    global group_last_id
    event = read_one_after(group_last_id)
    if event is None:
        return None
    group_last_id = event.id
    return event
```

假设 `dashboard-A` 第一个调用，它读到 `1000-0`，把共享位置推进到 `1000-0`。随后 `dashboard-B` 调用时，就从 `1000-0` 之后读取，得到 `1001-0`。

```text
共享位置：0-0
    |
    | dashboard-A 取走 1000-0
    v
共享位置：1000-0
    |
    | dashboard-B 取走 1001-0
    v
共享位置：1001-0
```

直觉上已经接近我们想要的分工模式。

但这段代码有一个明显漏洞：如果两个实例同时读取共享的 `group_last_id = 0-0`，它们就可能都选中 `1000-0`，然后都推进到 `1000-0`。

要让这个设计成立，我们必须让**“找到下一条记录”与“推进组读取位置”在服务端作为同一轮受协调的操作完成**。

还有一个问题：通知服务也在读这条 Stream。如果大屏服务和通知服务共用这个位置，它们就会相互抢消息。所以这个位置不能属于整个 Stream，而应该属于**某一组协同工作的消费者**。

于是，新的抽象终于变得必要：

> 为每一个独立的业务服务创建一个消费者组（Consumer Group），组内多个实例共享该组的新消息交付进度；不同组保留独立进度。

现在我们才需要 `XGROUP` 和 `XREADGROUP`。

## 3. 第一次引入消费者组：让“服务”拥有自己的进度

还是同一条 Stream：

```text
activity:1001:events
    |
    +-- dashboard 组
    |      |
    |      +-- dashboard-A
    |      +-- dashboard-B
    |
    +-- notify 组
           |
           +-- notify-A
```

`dashboard` 是大屏服务的组，`notify` 是通知服务的组；两个组都关联同一条 Stream。

这里要注意一个细节：**消费者组不会把 Stream 复制成多份。** 它在原来的 Stream 旁边增加了各组自己的读取状态。

在已有的 Stream 上创建两个组，可以执行：

```redis
XGROUP CREATE activity:1001:events dashboard 0-0
XGROUP CREATE activity:1001:events notify 0-0
```

两条命令都预期返回：

```text
OK
```

最后的 `0-0` 表示把组的已交付位置放在最初位置，使之后的 `XREADGROUP ... >` 能取得现存的历史消息。

如果把它改成 `$`：

```redis
XGROUP CREATE activity:1001:events dashboard $
```

那么创建组时，Redis 会将该组的已交付位置初始化为当时 Stream 的最后 ID；此后按 `>` 读取，主要面向**建组之后新增**的消息。对于已经存在的三条事件，它们不会被这个新组当作尚未交付的新消息返回。

这和上一章 `XREAD ... $` 容易混淆，但不是同一个语法位置：

- `XGROUP CREATE ... 0-0` / `$`：**创建组时**初始化共享的组进度。
- `XREADGROUP ... >`：**读取时**请求这个组尚未交付的新消息。

我们先固定使用 `0-0`，避免跳过实验数据。

现在问题是：Redis 把这个“组”保存在哪里？

## 4. 源码一：Stream 为什么要额外保存 cgroups？

上一章已经知道，Redis Streams 并不是一个简单的字符串列表；它有自己的 Stream 对象。

现在打开 [`src/stream.h`](https://github.com/redis/redis/blob/7.2.0/src/stream.h#L13-L21)。在 `stream` 结构中，除了消息本身的存储和最后 ID，还有一个字段：

**真实源码节选 — `stream.h`：**

```c
rax *cgroups;
```

这行代码说明：Stream 对象可以额外保存与自己关联的消费者组。`rax` 是 Redis 使用的一种基数树结构；当前只需要把它理解为可根据组名找到组对象的索引。

为什么不是把最后读取位置直接写进 Stream 的 `last_id`？

因为 `stream.last_id` 表示 Stream 自身最后追加的消息位置，而不同业务组的交付位置并不相同。

例如：

```text
Stream 最后一条 ID       1002-0

dashboard 组最后交付 ID  1001-0
notify 组最后交付 ID     1002-0
```

Stream 的追加位置只有一个，但不同组可以进展到不同的位置。

这就解释了 `cgroups` 存在的原因：**同一份消息历史，需要容纳多份彼此独立的消费进度。**

下一步去找创建组的入口。

## 5. 源码二：XGROUP CREATE 怎样创建一份组状态？

定位到 [`src/t_stream.c`](https://github.com/redis/redis/blob/7.2.0/src/t_stream.c#L2394-L2507) 的 `xgroupCommand()`。

当客户端执行：

```redis
XGROUP CREATE activity:1001:events dashboard 0-0
```

Redis 首先定位对应 Stream，解析组名和初始 ID。随后沿 `CREATE` 分支调用：

**真实源码节选 — `xgroupCommand()`：**

```c
streamCG *cg = streamCreateCG(s,grpname,sdslen(grpname),&id,entries_read);
```

这里的 `s` 是当前 Stream；`grpname` 是 `dashboard`；`id` 是已经解析好的 `0-0`。

真正建立组对象的是 `streamCreateCG()`。在同一个文件中，它的关键动作包括：

**真实源码节选 — [`streamCreateCG()`](https://github.com/redis/redis/blob/7.2.0/src/t_stream.c#L2314-L2325)，省略检查和初始化的其他语句：**

```c
cg->last_id = *id;
```

这个字段记住了该组已经交付到的最后位置。

与上一章普通 `XREAD` 需要由客户端自行记住读取位置相比，现在位置属于 Redis 服务端维护的组对象。

用**教学化简**表示整个创建过程：

```c
// 教学化简：不是可直接编译的 Redis 实现
streamCG *cg = allocate_group();
cg->last_id = requested_start_id;
stream->cgroups[group_name] = cg;
```

这里有一个特别重要的前提：初始 ID 并不是“当前所有消息都已经业务处理成功”的证明。它只是指定**这组从哪里开始交付新消息**。

如果组从 `$` 创建，已有消息会被跳过；如果从 `0-0` 创建，已有历史可以被该组作为新消息领取。选择哪个位置，是业务语义的一部分。

至此，一个组已经存在，却还没有消费者，也没有实际读取消息。

下一步：让 `dashboard-A` 和 `dashboard-B` 分别领取任务。

## 6. 两个实例第一次 XREADGROUP：谁先读，谁先领

消费者组使用 `XREADGROUP` 读取：

```redis
XREADGROUP GROUP dashboard dashboard-A COUNT 1 STREAMS activity:1001:events >
```

这条命令可以拆成三个部分：

- `GROUP dashboard dashboard-A`：以 `dashboard` 组内 `dashboard-A` 消费者的身份读取。
- `COUNT 1`：本次最多返回一条消息。
- `STREAMS activity:1001:events >`：读取本组尚未交付过的新消息。

假设组创建时的位置是 `0-0`，当前 Stream 有 `1000-0`、`1001-0`、`1002-0` 三条消息。

那么第一次调用预计返回 `1000-0`。

接着另一个实例执行：

```redis
XREADGROUP GROUP dashboard dashboard-B COUNT 1 STREAMS activity:1001:events >
```

它预计得到 `1001-0`。

再由 `dashboard-A` 请求一次：

```redis
XREADGROUP GROUP dashboard dashboard-A COUNT 1 STREAMS activity:1001:events >
```

预计得到 `1002-0`。

于是，**在这组串行演示命令的前提下**，观察到：

| 调用顺序 | 组 | 消费者 | 取得的消息 ID | 调用后组的最后交付 ID |
|---|---|---|---|---|
| 1 | dashboard | dashboard-A | `1000-0` | `1000-0` |
| 2 | dashboard | dashboard-B | `1001-0` | `1001-0` |
| 3 | dashboard | dashboard-A | `1002-0` | `1002-0` |

这不是 Redis 采用了固定的 A、B、A 轮询算法。我们**人为安排了命令顺序**，所以看起来交替分配。

真正的规则是：

> 哪个消费者先成功发起一轮新消息读取，就会从组的当前交付位置之后领取本次返回的消息；组进度随着交付前进。

如果 `dashboard-A` 连续请求三次，或者一次请求 `COUNT 3`，它同样有可能拿走全部三条。

消费者组提供的是**共同分工的读取语义**，并不保证消息在各消费者之间平均分配，也不保证每个实例一定有相同的工作量。

## 7. 源码三：为什么 XREAD 和 XREADGROUP 共用一条读取路径？

在 [`src/t_stream.c`](https://github.com/redis/redis/blob/7.2.0/src/t_stream.c#L2014-L2235) 中，`XREAD` 和 `XREADGROUP` 都通过 `xreadCommand()` 处理。

它没有重新写一套从 Stream 中遍历消息的底层逻辑，而是复用读消息的主要代码，只在是否指定组时引入不同状态。

**真实源码节选 — `xreadCommand()`：**

```c
int xreadgroup = sdslen(c->argv[0]->ptr) == 10;
```

这行代码的含义很具体：Redis 根据当前命令名是 `XREAD` 还是 `XREADGROUP`，决定是否启用组读取相关语义。

对 `XREADGROUP`，Redis 还会解析组名和消费者名，查找该 Stream 中对应的组：

```c
(group = streamLookupCG(o->ptr,groupname->ptr)) == NULL
```

上面是源码中“组不存在”检查条件的关键部分；如果查找失败，Redis 会返回 `NOGROUP`，而不是擅自创建一个新组。

为什么非要先创建组？

因为组的初始位置是业务选择。Redis 不能在第一次收到 `XREADGROUP` 时，就自行决定该服务是要读全部历史，还是只要新事件。

解析成功后，Redis 还需要区分请求中的 ID：

- 普通 `XREAD` 使用一个具体 ID（或 `$`）来表达读取边界。
- `XREADGROUP` 使用 `>` 来请求**本组尚未交付的新消息**。
- `XREADGROUP` 使用普通的具体 ID 时，含义不是“读任意历史”，而是重新读取**当前消费者自己尚未确认的已交付消息**。

第三种情况涉及失败恢复，本章先不深入。当前只沿着 `>` 的正常读取路径走。

## 8. 最关键的一步：读取起点为什么不再由客户端维护？

上一章的普通 `XREAD`，要让消费者续读，它必须提供上次保存的 ID。

使用消费者组后，我们希望所有 `dashboard` 实例共享一份进度，因此 `>` 不应被简单解释成 Stream 的最后 ID，也不应解释成消费者自己的某个游标。

在 `xreadCommand()` 中，Redis 根据组的最后交付位置决定新消息读取起点。

**真实源码节选 — `xreadCommand()`：**

```c
*gt = *last;
```

这里位于处理组的新消息（`>`）分支中：`last` 指向 `groups[i]->last_id`，所以读取边界取自**组状态**，而非调用者本地保存的游标。

随后，Redis 调用 `streamIncrID()`，让真正的范围读取从“最后交付 ID 的下一个可能位置”开始。

从业务角度看，它相当于：

```python
# 教学化简：展示组读取位置的语义，不是 Redis 源码

def read_new(group_name, consumer_name, count):
    group = groups[group_name]
    entries = stream.read_after(group.last_id, count)

    if entries:
        group.last_id = entries[-1].id

    return entries
```

但这段伪代码还没有完整表现 Redis 的实际行为：它只解释“为什么同组消费者不会都把某条新消息当作从未交付过”。真实实现还要维护消费者身份以及已交付消息的状态。

现在，两个 `dashboard` 实例的请求可以被序列化地处理，组游标会随着新消息的交付向前推进。

因此：

```text
Stream：1000-0   1001-0   1002-0
                 |
                 v
       dashboard.last_id
                 |
           从这里往后
          领取新消息
```

但 `notify` 组不会因为 `dashboard` 组前进而丢掉事件，因为它有自己的 `notify.last_id`。

这正是我们希望得到的两个层次的隔离。

## 9. Redis 真正在哪里推进组进度？

还差最后一环：Redis 找到消息后，究竟何时更新 `last_id`？

`xreadCommand()` 会调用 `streamReplyWithRange()` 枚举符合条件的消息，并构造返回数据。

定位到 [`src/t_stream.c` 的 `streamReplyWithRange()`](https://github.com/redis/redis/blob/7.2.0/src/t_stream.c#L1564-L1671)。遍历消息时，它会检查当前消息 ID 是否比组最后交付 ID 更新。

**真实源码节选 — `streamReplyWithRange()`：**

```c
group->last_id = id;
```

这行代码位于这样的条件之下：当前确实在组读取路径中，而且当前消息 ID 比组的 `last_id` 更大。

换句话说，**组进度不是消费者处理完业务之后才推进，而是在 Redis 将新消息交付给消费者时推进。**

这件事非常关键。

假设 `dashboard-A` 领取了 `1000-0`，Redis 把组进度推进到 `1000-0`。但 A 收到消息后，还没更新大屏统计就崩溃了。

`dashboard-B` 再用 `>` 读取，并不会自然拿到 `1000-0`；它会继续领取组中尚未交付过的新消息。

我们因此碰到了一个新的矛盾：

**组进度解决了“新消息如何分工”，但它本身无法证明已交付的消息已经处理成功。**

Redis 必须保留额外的“已交付、尚待确认”状态，否则消费者一崩溃，任务归属就无法恢复。

这也是 Redis 消费者组实际数据结构里还有其他字段的原因。不过，本章只看这个状态如何出现，不立刻深入恢复算法。

## 10. 源码四：为什么同一个组还要记住消费者身份？

我们刚才把 `dashboard-A` 和 `dashboard-B` 写进 `XREADGROUP` 命令：

```redis
XREADGROUP GROUP dashboard dashboard-A COUNT 1 STREAMS activity:1001:events >
```

组名用来定位共享进度，那消费者名字做什么？

如果组只保存一个 `last_id`，Redis 能知道下一条要发什么，却不知道之前的某条消息交给了谁。

Redis 7.2.0 的 [`streamCG`](https://github.com/redis/redis/blob/7.2.0/src/stream.h#L49-L68) 同时维护组的读取位置、消费者集合和未确认记录。消费者集合中的每个消费者有自己的名字及相关状态。

当一个消费者第一次以某个名字读取时，`xreadCommand()` 会先查找它，不存在则调用 `streamCreateConsumer()` 建立消费者记录。

**真实源码关键调用：**

```c
consumer = streamLookupConsumer(groups[i],consumername->ptr);
```

创建消费者的函数同样位于 `src/t_stream.c`。这些消费者名字是**组内部的身份标识**，并不是网络连接的永久 ID。

如果同一个组里的两个真实工作进程都使用同一个消费者名，Redis 就无法按预期区分它们的独立待处理状态。因此，应该为各个实际工作实例提供不同的消费者名称，并妥善设计重启后的身份策略。

现在再补上一条源码事实：在没有使用 `NOACK` 的正常 `XREADGROUP` 新消息读取路径中，Redis 会为消息创建“已交付但未确认”的记录，并关联到组和消费者。

**真实源码关键调用 — `streamReplyWithRange()`：**

```c
streamNACK *nack = streamCreateNACK(consumer);
```

`nack` 在这里是 Redis 内部表示待确认消息状态的结构名称，并不是说 Redis 已经给客户端发送了“否定确认”。

目前只需要理解：

- 新消息已经交付，组进度会前进。
- Redis 同时可以记住这条消息暂时归哪个消费者处理。
- 这个状态不能代替业务真正完成的证明。

下一章才详细研究这类记录如何被确认、查询与恢复。

## 11. 实验一：先证明两个 XREAD 会读到相同历史

以下实验可在本地 Docker 环境中复现。为了不影响前面章节留下的实例，我们使用单独的容器名和宿主端口。

```bash
docker run -d \
  --name redis-stream-group-lab \
  -p 127.0.0.1:6381:6379 \
  redis:7.2.0 \
  --save "" \
  --appendonly no
```

先删除同名实验 Stream：

```bash
docker exec redis-stream-group-lab redis-cli DEL activity:1001:events
```

依次写入三条带固定 ID 的报名事件：

```bash
docker exec redis-stream-group-lab redis-cli XADD activity:1001:events 1000-0 type signed-up user_id 42
docker exec redis-stream-group-lab redis-cli XADD activity:1001:events 1001-0 type signed-up user_id 57
docker exec redis-stream-group-lab redis-cli XADD activity:1001:events 1002-0 type signed-up user_id 63
```

每次 `XADD` 的预期返回 ID 分别是：

```text
1000-0
1001-0
1002-0
```

用同一条 `XREAD` 命令模拟两个独立的读取者：

```bash
# 模拟 dashboard-A
docker exec redis-stream-group-lab redis-cli \
  XREAD COUNT 3 STREAMS activity:1001:events 0-0

# 模拟 dashboard-B
docker exec redis-stream-group-lab redis-cli \
  XREAD COUNT 3 STREAMS activity:1001:events 0-0
```

**预期现象**：两次返回均包含 `1000-0`、`1001-0`、`1002-0`。原始 `redis-cli` 的 RESP2 多层数组格式较长，这里不虚构具体缩进，而以消息 ID 为观察重点。

现在我们确认：普通 `XREAD` 不为两个调用者做分工。

## 12. 实验二：同组实例分担，不同组独立读取

继续使用刚才那条 Stream，不删除已有三条数据。

创建两个组，都从历史起点开始：

```bash
docker exec redis-stream-group-lab redis-cli \
  XGROUP CREATE activity:1001:events dashboard 0-0

docker exec redis-stream-group-lab redis-cli \
  XGROUP CREATE activity:1001:events notify 0-0
```

预期均返回 `OK`。

由大屏的两个实例依次领取：

```bash
# 第一次：dashboard-A 领一条
docker exec redis-stream-group-lab redis-cli \
  XREADGROUP GROUP dashboard dashboard-A COUNT 1 \
  STREAMS activity:1001:events '>'

# 第二次：dashboard-B 领一条
docker exec redis-stream-group-lab redis-cli \
  XREADGROUP GROUP dashboard dashboard-B COUNT 1 \
  STREAMS activity:1001:events '>'

# 第三次：dashboard-A 再领一条
docker exec redis-stream-group-lab redis-cli \
  XREADGROUP GROUP dashboard dashboard-A COUNT 1 \
  STREAMS activity:1001:events '>'
```

**按上述执行顺序**，预期分别取得：

```text
dashboard-A -> 1000-0
dashboard-B -> 1001-0
dashboard-A -> 1002-0
```

继续再让 `dashboard-B` 从本组领取新消息：

```bash
docker exec redis-stream-group-lab redis-cli \
  XREADGROUP GROUP dashboard dashboard-B COUNT 1 \
  STREAMS activity:1001:events '>'
```

这时已经没有尚未交付给 `dashboard` 组的新消息，预期返回空结果。**注意：这不代表 Redis 删除了之前三条 Stream 消息。**

我们马上用另一个组证明这一点。

让通知服务读取：

```bash
docker exec redis-stream-group-lab redis-cli \
  XREADGROUP GROUP notify notify-A COUNT 3 \
  STREAMS activity:1001:events '>'
```

`notify` 组仍然预期能够取得三条：`1000-0`、`1001-0`、`1002-0`。

为什么？因为这两个组有各自的 `last_id`：

```text
同一条 Stream
  1000-0      1001-0      1002-0
      |           |           |
      +-----------+-----------+
                  |
       +----------+----------+
       |                     |
   dashboard 组           notify 组
       |                     |
   A / B 分担             notify-A
   三条新事件             仍可取得三条
```

这个现象才是消费者组带来的核心变化：**同组协作、跨组独立。**

## 13. 实验三：不删除消息，也能看到组进度在变化

上一章我们已经见过：普通 `XREAD` 不会从 Stream 删除记录。这一章也一样。

运行：

```bash
docker exec redis-stream-group-lab redis-cli \
  XLEN activity:1001:events
```

预期返回：

```text
(integer) 3
```

尽管 `dashboard` 组和 `notify` 组都已经领取过这些记录，Stream 本身仍然保留三条消息。

如果想观察消费者组的状态，可以执行：

```bash
docker exec redis-stream-group-lab redis-cli \
  XINFO GROUPS activity:1001:events
```

这里重点关注每个组的：

- `name`：组名。
- `consumers`：组中已记录的消费者数量。
- `last-delivered-id`：该组最后交付的消息 ID。
- `pending`：当前组中已交付但还没有确认的消息数量。

按实验过程，两个组的 `last-delivered-id` 都应推进到 `1002-0`，而 `dashboard` 组登记了两个消费者，`notify` 组登记了一个消费者。

由于我们**没有执行 `XACK`**，默认的组消费会产生待确认记录。这也是为什么即使 `dashboard` 的 `>` 读取返回空，它仍然可能存在待处理消息。

可以用下面的命令查看计数：

```bash
docker exec redis-stream-group-lab redis-cli \
  XPENDING activity:1001:events dashboard
```

按照当前实验，预期概要中的待确认总数为 `3`。更详细的确认机制留到下一章讲解。

如果担心实验状态混乱，可以在下一次复现前删除整个测试 Stream，再从头写入消息、重新建组。不要在生产环境中为了“清理 pending”随意删除业务 Stream。

### 补充验证：`$` 为什么容易跳过已有历史

你也可以创建一个新组，故意把初始位置设为 `$`：

```bash
docker exec redis-stream-group-lab redis-cli \
  XGROUP CREATE activity:1001:events fresh-dashboard '$'
```

再执行：

```bash
docker exec redis-stream-group-lab redis-cli \
  XREADGROUP GROUP fresh-dashboard dashboard-C \
  STREAMS activity:1001:events '>'
```

预期是空结果，因为新组创建时就把已有的最后 ID `1002-0` 当成初始交付位置。

现在追加新消息：

```bash
docker exec redis-stream-group-lab redis-cli \
  XADD activity:1001:events 1003-0 type signed-up user_id 88
```

再读取：

```bash
docker exec redis-stream-group-lab redis-cli \
  XREADGROUP GROUP fresh-dashboard dashboard-C \
  STREAMS activity:1001:events '>'
```

这次预期拿到 `1003-0`。

**结论**：`XGROUP CREATE` 的初始 ID 会影响这个组能否将已有历史作为尚未交付的消息读取，因此上线时必须明确选择，不能无意识地总用 `$`。

## 14. 最小 Python 消费者：两个工作实例怎样复用同一个组？

命令行实验已经能证明分工语义。为了与实际活动服务衔接，再给出一个尽可能小的 Python 消费者。

先安装客户端：

```bash
python -m pip install "redis==5.0.1"
```

创建 `dashboard_worker.py`：

```python
"""教学演示：只观察消息分配，不代表生产级可靠消费者。"""
import sys
import redis

STREAM = "activity:1001:events"
GROUP = "dashboard-live"

if len(sys.argv) != 2:
    raise SystemExit("usage: python dashboard_worker.py <consumer-name>")

consumer_name = sys.argv[1]
client = redis.Redis(
    host="127.0.0.1",
    port=6381,
    decode_responses=True,
)

# 首次建组从已有历史开始；已有组则直接复用。
try:
    client.xgroup_create(STREAM, GROUP, id="0-0", mkstream=True)
except redis.exceptions.ResponseError as exc:
    if "BUSYGROUP" not in str(exc):
        raise

print(f"[{consumer_name}] 开始领取新消息")

while True:
    result = client.xreadgroup(
        groupname=GROUP,
        consumername=consumer_name,
        streams={STREAM: ">"},
        count=1,
        block=2000,
    )

    if not result:
        continue

    for stream_name, messages in result:
        for message_id, fields in messages:
            print(f"[{consumer_name}] {message_id} {fields}", flush=True)
            # 这里只展示领取结果，没有 XACK。
            # 下一章才实现“业务成功后确认”的流程。
```

在两个终端中分别启动：

```bash
python dashboard_worker.py dashboard-1
```

```bash
python dashboard_worker.py dashboard-2
```

然后持续用 `XADD` 写入新的活动报名事件。可以观察到：它们使用相同的组名、不同的消费者名，共同领取 `dashboard-live` 组的新消息。

但**不要期望必然严格交替**。哪个进程先拿到就绪消息、一次读取了多少条，会影响每个实例最终领取的数量。这个示例也故意不调用 `XACK`，因此读到的消息会保留待确认状态。

当你终止进程后重新启动，如果只用 `>`，它不会自动帮你恢复该消费者已经领取但尚未确认的历史消息。下一章将专门解决这一点。

## 15. 这一步解决了什么，没有解决什么？

现在，我们有了新的系统能力：

```text
                报名服务
                    |
                    | XADD
                    v
            activity:1001:events
                    |
         +----------+----------+
         |                     |
         v                     v
     dashboard 组            notify 组
         |                     |
    +----+----+                |
    |         |                |
    v         v                v
实例 A      实例 B           通知实例
分担新消息  分担新消息       独立读取全部
```

它恰好对应我们本章的两个需求：

1. 不同业务服务各自需要一套事件读取进度。
2. 同一个业务服务的多个实例需要共享新消息交付进度，避免每个实例都处理全部新事件。

同时，我们也必须保留几个边界。

**第一，消费者组不是 Redis 自动均衡调度器。** 它不会周期性轮询选择下一名消费者；消费者自己请求消息，组进度按实际交付推进。

**第二，组中的“交付过一次”不等于“业务只会执行一次”。** 当消费者故障、消息重试或组状态被管理员修改时，消息仍可能重新交付。业务去重和幂等不能省略。

**第三，`XREADGROUP ... >` 不负责自动恢复之前领取但未确认的任务。** 它的目标是本组尚未交付的新消息，恢复旧任务有专门的机制。

**第四，消费者组不是无限保存消息的保证。** Stream 消息可能因为显式删除、裁剪或 Redis 故障而不可用；组状态与消息保留策略必须一起设计。

本章没有把这些异常分支全部展开，因为读者刚刚理解正常的新消息分工路径。现在最自然的问题，是观察一次**消息已经交付、业务却没有执行完成**的失败。

## 16. 下一章：消息已经交给消费者，就算处理成功了吗？

假设大屏服务现在有两个消费者：

```text
dashboard-A
    |
    | XREADGROUP 得到 1000-0
    v
准备更新大屏统计
    |
    X 进程突然崩溃
```

此时从 Redis 视角看，`1000-0` 已经交付给 `dashboard-A`，组游标也向前推进了。

但实际业务并没有执行成功。

当 `dashboard-B` 使用 `>` 继续读取时，它可能只看到后面的新消息，不会自动重读刚才分配给 A 的这条旧消息。

问题又出现了：

> Redis 如何区分“已经发送给消费者”和“消费者已经完成业务”？如果消费者拿到消息后崩溃，Redis 怎么知道哪些消息还没有真正完成？

我们已经在源码中隐约看到 `pel` 和 `streamNACK`，但现在才出现理解它们的必要条件。

下一章，我们将从消费者拿到消息后立即崩溃的实验开始，研究 Redis 为什么要记录待确认消息，以及 `XACK` 到底改变了哪一份状态。

**下一章：《消息送达不等于处理成功：从 XREADGROUP 到 PEL 与 XACK》**

---

## 本章回顾

```text
普通 XREAD 允许各个调用者独立补读历史
                        |
                        | 同一服务扩容后，两实例读到同一批消息
                        v
              需要共享读取位置
                        |
                        | 共享位置要被 Redis 协调更新
                        v
                 XGROUP CREATE
                        |
                        | 不同服务不能共用同一个进度
                        v
               每个服务使用自己的组
                        |
                        | 同组消费者需要领取不同的新消息
                        v
                  XREADGROUP ... >
                        |
                        v
           streamCG.last_id 随消息交付推进
                        |
                        | 消费者可能在处理前崩溃
                        v
           下一章：PEL 与 XACK 的确认机制
```

与上一章相比，我们没有改变 Stream 中事件的基本存储方式，而是新增了一份由 Redis 管理的消费者组状态。

这份状态让我们可以不再依赖各个工作实例自行协调新消息的读取位置，并允许不同业务组独立读取同一条 Stream。

从源码看，本章最需要记住的调用关系是：

```text
XGROUP CREATE
     |
     v
xgroupCommand()
     |
     v
streamCreateCG()
     |
     +--> 在 Stream 下保存组
     +--> 初始化 group.last_id

XREADGROUP ... >
     |
     v
xreadCommand()
     |
     +--> streamLookupCG()
     +--> streamLookupConsumer() / streamCreateConsumer()
     +--> 使用 group.last_id 作为新消息边界
     |
     v
streamReplyWithRange()
     |
     +--> 返回新消息
     +--> 推进 group.last_id
     +--> 记录待确认的消息交付状态
```

最后这条“待确认状态”正是下一章的问题起点，而不是本章要一次性讲完的所有机制。

## 源码与文档参考

本章版本严格固定为 **Redis 7.2.0**。

- [src/stream.h：Stream、消费者组与消费者的数据结构](https://github.com/redis/redis/blob/7.2.0/src/stream.h) —— `stream.cgroups`、`streamCG.last_id`、`streamConsumer`。
- [src/t_stream.c：streamCreateCG()](https://github.com/redis/redis/blob/7.2.0/src/t_stream.c#L2314-L2325) —— 创建并记录消费者组。
- [src/t_stream.c：xgroupCommand()](https://github.com/redis/redis/blob/7.2.0/src/t_stream.c#L2399-L2553) —— 解析组创建命令和初始位置。
- [src/t_stream.c：xreadCommand()](https://github.com/redis/redis/blob/7.2.0/src/t_stream.c#L2022-L2269) —— 普通读取和组读取的入口、`>` 处理、组位置选择。
- [src/t_stream.c：streamReplyWithRange()](https://github.com/redis/redis/blob/7.2.0/src/t_stream.c#L1564-L1671) —— 枚举消息并更新组交付位置。
- [XGROUP CREATE 官方文档](https://redis.io/docs/latest/commands/xgroup-create/) —— `0-0`、`$` 和 `MKSTREAM` 等参数。
- [XREADGROUP 官方文档](https://redis.io/docs/latest/commands/xreadgroup/) —— 组读取、`>`、`COUNT`、消费者身份与待确认状态。**请留意在线文档含高于 7.2 的新功能，本章仅使用 7.2.0 已支持的选项。**

**验证声明**：核心函数、字段及命令语义已经根据 Redis 7.2.0 源码和对应命令文档静态核对；Docker 与 Python 实验均为可复现步骤，输出是预期现象，不冒充实测结果。
