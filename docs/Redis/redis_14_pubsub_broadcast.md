---
title: "第十四章：一条活动事件，怎样通知多个服务？从 List 到 Pub/Sub"
sidebar_label: "14 - Pub/Sub 与事件广播"
sidebar_position: 15
description: "沿活动报名案例，从 List 任务竞争引出广播需求，精读 Redis 7.2.0 的 SUBSCRIBE、PUBLISH、订阅关系与消息推送，并验证离线订阅者为何会错过消息。"
tags:
  - Redis
  - 源码精读
  - Pub/Sub
  - 事件广播
---

# 第十四章：一条活动事件，怎样通知多个服务？从 List 到 Pub/Sub

上一章，我们给活动 `1001` 建立了积分排行榜。活动系统已经能保存报名名单、处理报名任务，并根据积分返回实时名次。

现在，产品提出一个新要求：**报名成功后，除了给报名者发送通知，还要更新活动大屏上的报名人数。**

这两个工作可以交给独立程序：通知服务负责消息发送，大屏服务负责刷新展示。它们都关心同一件事——`user:42` 成功报名了 `activity:1001`。

直觉上，上一章学习过的 List 似乎就能完成消息传递。我们先从它开始，而不是立即引入新术语。

> **源码与实验约定**：本章固定为 [Redis 7.2.0](https://github.com/redis/redis/tree/7.2.0)；主要阅读 [`src/pubsub.c`](https://github.com/redis/redis/blob/7.2.0/src/pubsub.c)。文中标明“真实源码节选”的代码摘自该版本；“教学化简”只用于展示因果关系，不是可以直接替换的源码。命令结果为**预期输出**：本文制作环境没有运行 Redis 服务，不将预期结果称为已实测。

## 1. 我们已经有 List，为什么还要换思路？

活动报名完成时，业务程序往 List 中放一条任务：

```redis
LPUSH activity:1001:tasks 'signed-up:user:42'
```

通知服务用 `RPOP` 读取：

```redis
RPOP activity:1001:tasks
```

如果只有通知服务，这种方案很好理解：把一个待处理任务交给另一个程序去执行。

但现在大屏服务也开始读取同一个 List。假设队列中只有一条报名任务：

```text
                 activity:1001:tasks
                         |
                [signed-up:user:42]
                         |
                +--------+--------+
                |                 |
          通知服务 RPOP      大屏服务 RPOP
```

第一个成功执行 `RPOP` 的消费者会取走并删除这条元素。后执行的消费者拿不到它。

因此，**List 的竞争消费适合“这件任务交给谁处理”，不适合直接表达“这件事让所有感兴趣的服务都知道”。**

我们当然可以给通知和大屏各建一个 List，然后由报名服务写入两份：

```redis
LPUSH activity:1001:notify-tasks 'signed-up:user:42'
LPUSH activity:1001:screen-tasks 'signed-up:user:42'
```

两个 List 在简单场景中是合理的方案，且保留各自待处理的任务。但如果以后又增加审计服务、实时分析服务，报名服务就得知道越来越多的目标队列，自己决定向哪些队列写入。

而眼下的新需求其实更简单：

> **报名服务只报告“报名成功”这一事实。至于哪些服务感兴趣，让那些服务自行声明。**

这时，我们才需要一种不同于任务争抢的消息传递方式。

## 2. 先设计一个最小广播机制

假设我们自己实现一个很小的内存广播中心。它只需要保存：

```python
# 概念伪代码，不是 Redis 源码
subscribers = {
    "activity:1001:events": [notify_client, screen_client]
}
```

报名服务不再知道通知服务、大屏服务的名字，只向一个约定的频道发布事件：

```text
PUBLISH activity:1001:events signed-up:user:42
```

广播中心查找这个频道，逐个向当前订阅的连接发送消息：

```python
# 概念伪代码
for client in subscribers.get(channel, []):
    client.send(message)
```

注意，这里并没有从某个队列中弹出消息。两个订阅者分别收到同一份事件，而不是共同争抢事件的处理权。

```text
                  报名服务
                      |
                      | 发布报名事件
                      v
               activity:1001:events
                      |
              +-------+-------+
              |               |
              v               v
           通知服务         大屏服务
         发送报名通知      更新报名人数
```

这种发布者只关心频道、订阅者自行选择频道的通信方式，称为 **发布/订阅（Publish/Subscribe，简称 Pub/Sub）**。

Redis 已提供这套机制：

- `SUBSCRIBE channel`：当前连接订阅一个频道；
- `PUBLISH channel message`：向频道发布一条消息。

此刻只需要这两个命令。我们先把效果跑出来，再去源码中解释为什么可以做到。

## 3. 实验：两名订阅者同时收到同一条消息

如果沿用前面章节的 `redis-source-lab` 容器，可直接使用。如果没有，启动独立实验实例：

```bash
# 如果已有同名容器，不要重复创建。
docker run -d \
  --name redis-source-lab \
  -p 127.0.0.1:6379:6379 \
  redis:7.2.0 \
  --save "" \
  --appendonly no
```

> 此配置关闭持久化，仅用于本地实验。本章的 Pub/Sub 消息本身不会因为启用 RDB/AOF 就变成可以补读的历史记录。

现在准备 **三个终端**。

**终端 A：模拟通知服务。**

```bash
docker exec -it redis-source-lab redis-cli SUBSCRIBE activity:1001:events
```

订阅成功后，客户端会收到一条订阅确认消息，包含 `subscribe`、频道名和当前订阅数量。终端 A 会继续等待推送。

**终端 B：模拟大屏服务。**

```bash
docker exec -it redis-source-lab redis-cli SUBSCRIBE activity:1001:events
```

终端 B 同样保持订阅状态。

**终端 C：模拟报名服务。** 先确认当前有两个直接订阅者：

```bash
docker exec redis-source-lab redis-cli PUBSUB NUMSUB activity:1001:events
```

预期可看到频道名以及订阅数量 `2`（前提是只有 A、B 两个客户端订阅了该频道）。

然后发布事件：

```bash
docker exec redis-source-lab redis-cli \
  PUBLISH activity:1001:events signed-up:user:42
```

预期发布命令返回：

```text
(integer) 2
```

两个订阅终端会各自出现一条类型为 `message` 的消息，频道都是 `activity:1001:events`，内容都是 `signed-up:user:42`。

到这里，广播的最小目标已经实现。

但这里产生一个新的问题：

**Redis 怎样知道这个频道当前有哪些客户端？**

如果每次 `PUBLISH` 都遍历所有已连接客户端，逐个询问是否订阅了频道，那么客户端越来越多时，这个实现就很浪费。

我们现在需要的是一份能够直接定位订阅者的索引。

## 4. SUBSCRIBE 不只是等待，而是在建立订阅关系

先从源码里的命令入口看起：

- 仓库：[`redis/redis`](https://github.com/redis/redis)
- 版本：[`7.2.0`](https://github.com/redis/redis/tree/7.2.0)
- 文件：[`src/pubsub.c`](https://github.com/redis/redis/blob/7.2.0/src/pubsub.c)
- 函数：`subscribeCommand()`、`pubsubSubscribeChannel()`

客户端发送：

```redis
SUBSCRIBE activity:1001:events
```

与前面学习过的 `SET`、`ZADD` 一样，请求先通过网络读取和命令分发，最后进入当前命令的处理函数。

**真实源码节选 — `subscribeCommand()`：**

```c
pubsubSubscribeChannel(c,c->argv[j],pubSubType);
```

这里：

- `c` 是当前订阅客户端；
- `c->argv[j]` 是需要订阅的频道；
- `pubSubType` 表示这里走的是普通频道订阅机制，而不是分片频道机制。

但它具体修改了什么？继续进入 `pubsubSubscribeChannel()`。

如果我们只用一个 `channel -> clients` 的表，发布消息时就能够直接找到订阅者：

```text
activity:1001:events
    |
    +-- 通知客户端 A
    +-- 大屏客户端 B
```

这是广播所需的索引。但客户端主动取消订阅，或客户端连接关闭时，还需要知道这个客户端此前订阅了哪些频道，才能把它从各频道中移除。

因此，Redis 7.2.0 同时维护两个方向的关系：

```text
客户端视角                       服务端视角

A -> activity:1001:events     activity:1001:events -> [A, B]
B -> activity:1001:events
```

**真实源码节选 — `pubsubSubscribeChannel()` 中的关键操作（非连续摘录）：**

```c
dictAdd(type.clientPubSubChannels(c),channel,NULL)
```

这是先记录“客户端订阅了该频道”。

函数随后在服务端维护的频道映射中查找该频道。若还没有记录，就为这个频道创建订阅者列表；然后将客户端加入列表。

**真实源码节选 — 将客户端加入频道的订阅者列表：**

```c
listAddNodeTail(clients,c);
```

这里的 `list` 是 Redis 服务器内部的通用链表容器，并不是我们使用 `LPUSH`、`RPOP` 操作的 Redis **List 数据类型**。两者名称相近，但用途和层次不同。

最后，`pubsubSubscribeChannel()` 会向客户端加入订阅确认回复。该函数的返回值表示这次是否建立了**新的**订阅关系：如果同一客户端原本已经订阅同一频道，Redis 不会再向该频道列表重复增加它。

现在我们能回答“为什么发布时不用扫描所有客户端”：因为 **Redis 在订阅发生的那一刻，已经建立了从频道到订阅者的索引**。

而且，这些订阅关系属于服务器的 Pub/Sub 状态，不是当前逻辑数据库中的普通 Key。

例如 `activity:1001:events` 是一个频道名，不意味着 Redis 自动创建了一个同名 String 或 List Key。`GET activity:1001:events` 并不能获取此前发布的事件。

不过，还差最关键的一步：发布消息时，Redis 怎样用上这张表？

## 5. PUBLISH 怎样找到所有订阅者？

现在我们已经知道频道下保存了 A、B 两个客户端。

接着，报名服务发送：

```redis
PUBLISH activity:1001:events signed-up:user:42
```

请求经过 Redis 正常的命令解析和分发流程后，会进入 `publishCommand()`。

**真实源码节选 — `src/pubsub.c`：**

```c
int receivers = pubsubPublishMessageAndPropagateToCluster(c->argv[1],c->argv[2],0);
```

这一层会先处理当前节点上的本地发布，并在 Redis Cluster 场景下执行相应的集群传播。当前实验是一个独立实例，因此我们先只追踪本地分发路径。

接下来，调用会进入 `pubsubPublishMessage()`，最终进入：

```text
pubsubPublishMessageInternal(channel, message, type)
```

在 `pubsubPublishMessageInternal()` 中，Redis 首先根据频道名查找订阅者列表。

**真实源码节选 — 频道查找：**

```c
de = dictFind(*type.serverPubSubChannels, channel);
```

如果不存在这个频道的订阅者，直接匹配的分发就无事可做。

如果找到了，就遍历频道里的客户端列表，逐个调用消息回复函数。

**真实源码节选 — 向一个订阅者构造消息：**

```c
addReplyPubsubMessage(c,channel,message,*type.messageBulk);
```

这个调用非常重要。

Redis 不是执行两次 `RPOP`，也不是从某个待处理队列中“取走”事件。它是**为每个订阅客户端分别准备一份消息回复**。

把这部分实现教学化简，可以写成：

```python
# 教学化简；不是 Redis 源码

def publish(channel, message):
    clients = subscriptions.get(channel, [])
    matched = 0

    for client in clients:
        enqueue_pubsub_reply(client, channel, message)
        matched += 1

    return matched
```

对当前场景，数据与调用可以这样观察：

```text
PUBLISH activity:1001:events signed-up:user:42
                      |
                      v
      查找频道 activity:1001:events
                      |
                      v
             订阅客户端列表
                      |
               +------+------+
               |             |
               v             v
          客户端 A          客户端 B
               |             |
          加入消息回复    加入消息回复
               |             |
               v             v
           通知服务        大屏服务
```

这样，就能解释为什么两个客户端会收到同一个事件。

### 为什么 PUBLISH 返回 2？

`publishCommand()` 会把当前节点匹配到的接收数量作为整数回复给发布者。

在我们这个隔离实验中，有两个直接订阅者，所以预期返回 `2`。

但是，**返回 2 不代表两项业务都执行成功**。

从源码可以看出，计数发生在 Redis 生成并排队推送回复的过程中，不是通知服务实际发送成功后才计数。

假设通知服务接到了数据，却在调用外部短信接口时崩溃，Redis 的 `PUBLISH` 并不知道短信有没有发出去。

此外，Redis 7.2.0 还支持按模式匹配的订阅（`PSUBSCRIBE`），其分发与计数也会参与发布路径；一个客户端同时通过普通订阅和匹配的模式订阅同一频道时，可能收到不止一份通知。因此不能将返回值无条件理解成“已经有这么多不同业务服务处理成功”。

本章只需要理解：它是 **Redis 发布阶段的匹配/推送计数**，不是业务确认机制。

## 6. 消息是怎样被“推”到订阅者连接上的？

现在还剩一个小问题。

前面我们说 `PUBLISH` 给客户端 A、B 分别准备消息回复。它会不会等待 A 和 B 主动再发出一个 `GET` 才发送？

不需要。

Redis 的 Pub/Sub 会在订阅连接上主动推送消息。当前订阅客户端不需要重复轮询某个 Key。

在 `src/pubsub.c` 的 `addReplyPubsubMessage()` 中，Redis 会为客户端组织一个包含三个元素的 Pub/Sub 消息：

```text
["message", "activity:1001:events", "signed-up:user:42"]
```

其中：

1. `message` 标明这是一条频道消息；
2. `activity:1001:events` 是频道；
3. `signed-up:user:42` 是正文。

这里的“推送”并不是调用业务程序里的一段回调代码。Redis 做的是向客户端连接准备并发送协议数据；业务客户端需要持续读取连接，收到消息之后自己决定如何处理。

这恰好能复用我们前面已经研究过的 Redis 网络输出机制：命令处理时将数据加入客户端回复，网络写入逻辑负责把准备好的字节发送出去。

**重要边界**：一旦消费者处理得太慢，网络发送或客户端输出缓冲区可能成为压力点。Redis 并没有因为采用了“推送”就自动保证客户端永远不会掉线。消息甚至可能在进入客户端应用逻辑之前就因为连接问题丢失。

因此，Pub/Sub 适合需要立即通知在线客户端的场景；它不应该被误读成“有完整任务确认与重试机制的队列”。

## 7. 如果一个服务取消订阅，Redis 怎样清理关系？

现在假设大屏服务临时下线。原来记录的是：

```text
activity:1001:events -> [通知服务 A, 大屏服务 B]
```

如果连接 B 不再订阅，发布时就不应该继续把消息加入 B 的回复。

Redis 提供了 `UNSUBSCRIBE`，连接断开时也会清理该客户端原有订阅关系。

沿着源码，当前问题并不需要新增数据结构：只要把订阅时维护的两份关系反向删掉即可。

源码位置：`src/pubsub.c` 中的 `pubsubUnsubscribeChannel()`。

它大致完成：

```text
客户端 B 不再订阅
        |
        +--> 从 B 的频道集合中删除 activity:1001:events
        |
        +--> 从 activity:1001:events 的订阅者列表中删除 B
        |
        +--> 如果该频道再无订阅者，就移除频道映射
```

这也回答了为什么在订阅时 Redis 不只维护 `channel -> clients`：客户端退出时，还要能快速知道它关联了哪些频道。

但这个设计同时揭示了一个更严重的限制：

**如果 B 下线时恰好发生一次报名，它能在重新上线后收到当时错过的通知吗？**

我们来直接验证。

## 8. 实验：订阅者离线时发布的消息去了哪里？

保持终端 A（通知服务）持续订阅 `activity:1001:events`。

先停止终端 B（大屏服务）的订阅进程，确保它已与 Redis 断开。此时在终端 C 中运行：

```bash
docker exec redis-source-lab redis-cli PUBSUB NUMSUB activity:1001:events
```

若只剩 A，一个正常的预期结果是频道的订阅数量变为 `1`。

现在发布第二条事件：

```bash
docker exec redis-source-lab redis-cli \
  PUBLISH activity:1001:events signed-up:user:57
```

预期返回：

```text
(integer) 1
```

A 会收到 `signed-up:user:57`。

随后重新在终端 B 运行：

```bash
docker exec -it redis-source-lab redis-cli SUBSCRIBE activity:1001:events
```

B 只会收到本次订阅的确认消息；**它不会因为重新订阅而自动收到刚才的 `signed-up:user:57`**。

为了区分“之前的消息丢了”与“B 仍然不能接收新消息”，在终端 C 再发布：

```bash
docker exec redis-source-lab redis-cli \
  PUBLISH activity:1001:events signed-up:user:63
```

这次 A、B 都能收到 `signed-up:user:63`。

把结果排列起来，就能清楚看到差异：

| 时间 | A：通知服务 | B：大屏服务 | 发布事件 |
| --- | --- | --- | --- |
| T1 | 已订阅 | 已订阅 | `user:42` → A、B 收到 |
| T2 | 已订阅 | **已断开** | `user:57` → 只有 A 收到 |
| T3 | 已订阅 | 重新订阅 | **不会自动补发 `user:57`** |
| T4 | 已订阅 | 已订阅 | `user:63` → A、B 收到 |

注意，这并不是 Redis 的一个 Bug。

这是 Pub/Sub 选择的通信语义：

**它维护的是当前订阅关系，不是为每个频道保存一份可反复读取的事件历史。**

没有订阅者时发布消息，常见情况下会返回 `0`，而不是默默保存它等待将来的订阅者。

## 9. 这个限制能否用已有机制绕过去？

我们现在需要区分两种业务。

**场景 A：活动大屏实时刷新。**

如果大屏离线一分钟，恢复后可以重新查询最新报名人数，过去那一分钟的每一条“刷新提示”并不一定需要重新播放。

那么 Pub/Sub 很合适：在线时尽快通知；离线后重新拉取最新状态。

但这依赖一个前提：**真正的报名人数或报名记录，已经可靠地保存在别处。** Pub/Sub 消息只是提示“数据发生变化”，不是唯一的数据事实来源。

**场景 B：每一条报名都必须有处理记录。**

如果需求变成“审计服务恢复后要补齐离线期间的所有报名事件”，仅有 Pub/Sub 就不够了。

我们可能想把每条事件再写入 List 保存下来。这样能够保留尚未被取走的任务，但一个消费者弹出消息后，其他消费者无法依靠同一个 List 再读一遍；如果要给多个独立服务各自追踪进度，还需要自行管理多个队列、消费位置和清理规则。

我们真正需要的能力开始变得清晰：

1. 新事件到来时，在线服务能尽快看到它；
2. 事件不能因为某个订阅者暂时离线就从系统中消失；
3. 不同服务需要能够从自己的位置读取事件历史。

注意，**这不是说 Pub/Sub 必须“升级”为另一种类型**。如果大屏只需要实时刷新提示，Pub/Sub 仍是合理选择。

只是当业务需要“保留事件并允许重读”时，我们需要研究另一个机制。

这将引出下一章的 Redis Streams。

## 10. 重新审视：Pub/Sub 与 List 并不互相替代

本章从 List 遇到的限制出发引出 Pub/Sub，但回头来看，这两种机制解决的并不是同一个问题。

| 我们想解决的问题 | List | Pub/Sub |
| --- | --- | --- |
| 一个任务交给某个消费者处理 | 适合，出队后元素移除 | 不适合按任务争抢分配 |
| 多个当前在线服务都收到同一事件 | 需要额外复制任务 | 支持频道广播 |
| 消费者暂时离线 | 元素尚未弹出时可保留 | 离线期间消息不会补发 |
| 读取过的消息再读取 | `RPOP` 已删除，需要另行保存 | 无消息历史可读取 |
| 业务处理成功确认 | 需自行设计 | 无 ACK 机制 |
| 主要数据组织 | Redis List Key | 订阅关系与客户端推送状态 |

对于某项具体业务，不需要因为 Redis 提供了更多命令，就强迫系统采用更复杂的机制。

如果只是临时通知多个在线服务，Pub/Sub 的简单性就是它的优势。

如果处理失败必须恢复，那么需要保留消息、维护进度或使用其他可靠性方案。是否采用 Streams，应由业务对历史和恢复能力的要求决定。

此外，本章分析的是 Redis **普通 Pub/Sub** 的单节点正常路径。Redis 7.2.0 还支持模式订阅 `PSUBSCRIBE`，以及 Cluster 场景中的分片 Pub/Sub（`SSUBSCRIBE` / `SPUBLISH`）。这些能力会引入模式匹配与跨节点传播等额外问题，当前最小案例还没有遇到相应限制，所以暂不展开。

## 11. 本章回顾：广播机制是怎样从问题中长出来的？

```text
活动报名成功
     |
     | 需要发送通知
     v
List 任务队列
     |
     | 大屏服务也需要同一事件
     | 同一条任务只会被一个 RPOP 取走
     v
每个服务一个 List？
     |
     | 生产者必须维护所有目标队列
     v
发布到一个频道
     |
     | 谁感兴趣由谁自行订阅
     v
SUBSCRIBE 建立订阅关系
     |
     | 频道 -> 订阅者列表
     v
PUBLISH 找到并推送给当前订阅者
     |
     | 在线多个服务都能收到
     v
Pub/Sub 广播成立
     |
     | 订阅者离线会错过事件
     v
下一章：怎样保存并重新读取事件？
```

这条路径最后可以落到三个源码证据上：

- `pubsubSubscribeChannel()` 将“客户端订阅了什么”和“频道有哪些客户端”两份关系维护起来；
- `pubsubPublishMessageInternal()` 通过频道查找当前订阅者，逐个生成 Pub/Sub 消息回复；
- `pubsubUnsubscribeChannel()` 在取消订阅时清理双向关系。

现在我们可以用一句话准确描述它：

> **Redis Pub/Sub 是基于当前订阅关系进行消息推送的广播机制，而不是能够补读历史和确认业务完成的可靠任务队列。**

### 下一章

我们保留活动 `1001` 的同一个事件：

```text
signed-up:user:57
```

唯一改变的是要求：审计服务可能离线数分钟，但重新上线后必须能够读取错过的报名事件。

既然 Pub/Sub 没有事件历史，那就需要寻找一种**追加记录、保存消息 ID、支持从指定位置读取**的数据组织方式。

**下一章：《订阅者离线后，怎样补读错过的事件？从 Pub/Sub 到 Redis Streams》**

---

## 源码与文档参考

- [Redis 7.2.0 — `src/pubsub.c`：订阅、发布与取消订阅](https://github.com/redis/redis/blob/7.2.0/src/pubsub.c)
- [Redis 7.2.0 — `subscribeCommand()` 与订阅关系维护](https://github.com/redis/redis/blob/7.2.0/src/pubsub.c#L227-L251)
- [Redis 7.2.0 — `pubsubPublishMessageInternal()`](https://github.com/redis/redis/blob/7.2.0/src/pubsub.c#L435-L490)
- [Redis 7.2.0 — `publishCommand()`](https://github.com/redis/redis/blob/7.2.0/src/pubsub.c#L557-L576)
- [Redis 官方 Pub/Sub 指南](https://redis.io/docs/latest/develop/pubsub/)
- [Redis 官方 `PUBLISH` 命令](https://redis.io/docs/latest/commands/publish/)
- [Redis 官方 `SUBSCRIBE` 命令](https://redis.io/docs/latest/commands/subscribe/)
- [Redis 官方 `PUBSUB NUMSUB` 命令](https://redis.io/docs/latest/commands/pubsub-numsub/)
