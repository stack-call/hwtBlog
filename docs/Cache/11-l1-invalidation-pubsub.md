---
title: "缓存体系 11：A 的本地缓存更新了，B 为什么还返回旧标题？"
sidebar_label: "十一、A 更新了 B 还返回旧标题"
sidebar_position: 11
description: "从多实例 L1 的版本分叉引出失效通知，并检验消息丢失的边界。"
slug: /cache-system/11-l1-invalidation-pubsub
tags:
  - caching
  - redis
  - source-code
---

# A 的本地缓存更新了，B 为什么还返回旧标题？

上一章引入了应用本地 L1，让热点读不必每次访问 Redis。现在我们已经有 A、B 两个应用实例、一个共享 Redis L2 和数据库 MySQL。管理员把文章 42 更新为 v2。

## 正常删除缓存，不等于删掉所有本地副本

假设更新请求由 A 接收：

```text
T0  MySQL = v1; Redis L2 = v1; A.L1 = v1; B.L1 = v1
T1  A 提交 MySQL v2
T2  A 删除 Redis L2 的 post:42
T3  A 删除自己的 A.L1
T4  B 收到 GET /posts/42 → B.L1 命中 v1
```

这次错误和第三章的“没删 Redis”不同：Redis 已经失效了，**旧值躲在另一台应用的进程内**。A 直接调用 B 的进程字典并不方便，实例数量变多后更不现实。我们需要把“某个 Key 已经发生更新”的消息广播给其他实例。

## 最小机制：广播失效通知，而不是同步新数据

在 A 完成业务写入和必要的共享缓存失效动作后，发布一条短消息：

```json
{"kind":"invalidate","key":"post:42","version":2}
```

B 收到后执行 `L1.invalidate("post:42")`。下一次请求本地 miss，再走 Redis/DB 读取最新文章。

为什么只发“删除”而不广播完整文章？因为复制全量内容还需要处理消息顺序、序列化及回填冲突；只广播失效可以让接收方按已有读路径重建内容。它减少了需要携带的信息，但**不能保证通知必达**。

## 看 Redis Pub/Sub 的真实发布路径

这里选择 Redis Pub/Sub 作为最简单的广播机制。固定 Redis 7.2.4，打开 [`src/pubsub.c`](https://github.com/redis/redis/blob/7.2.4/src/pubsub.c)。应用发出：

```redis
PUBLISH cache:invalidate '{"key":"post:42","version":2}'
```

命令分发后进入 `publishCommand(client *c)`；它从 `c->argv` 取得频道名和消息，通过 `pubsubPublishMessageAndPropagateToCluster(...)` 向订阅者发布，并返回接收者数量。

**真实源码节选（Redis 7.2.4）：**

```c
int receivers = pubsubPublishMessageAndPropagateToCluster(c->argv[1],c->argv[2],0);
addReplyLongLong(c,receivers);
```

下游 `pubsubPublishMessage` 遍历当前订阅者，向它们的客户端回复路径发送消息；**没有业务事务、没有远端本地字典的强制删除，也不会为掉线的 B 永久保留消息**。`PUBLISH` 返回接收者数不是“B 的本地缓存已经成功失效”的业务级确认。Redis 的 Pub/Sub 是 at-most-once：订阅者当时离线，消息可能永久错过。

B 真正执行什么？如果使用 Caffeine，消费端收到通知后调用公开 API `cache.invalidate(42L)`；如果是 Python 字典则 `local.pop(42, None)`。这是**应用代码**，不是 Redis 服务器端调用 B 的内部方法。

## 运行两个应用实例的失效实验

执行 `python3 examples/cache_labs.py 11`：

```text
11: without event B=v1; after event/reload B=v2; missed event leaves B=v2 while DB=v3
```

实验第一段把 A 本地失效、B 保留旧值重现出来；第二段模拟 B 成功收到通知后重新加载 v2；第三段再让数据库更新到 v3，但**故意丢掉通知**，于是 B 仍读 v2。

这次实验让我们不能轻易说“加了 Pub/Sub，一致性就解决了”。

## 消息可能丢、可能晚到，接下来怎么办？

如果通知只作为优化手段，可以让 L1 设置较短 TTL，使漏消息的不一致窗口有限；接收端定期检查共享版本号，也能让旧值最终被发现。若业务需要更强可靠性，可以使用持久化变更日志或事务外盒，再由消费者重试和跟踪处理进度。

即便通知必达，也不能只看消息版本：如果本地正在回填老数据，通知与回填交错仍可能产生第四章的旧值复活。已有的版本门闩或条件回填措施仍然有价值。

我们至此已经得到了多级缓存系统：L1 减少 Redis 请求，L2 减少 MySQL 请求，数据库保存权威数据，更新时需要跨层失效。但如果 Redis 这个 L2 整体不可用，所有还没被 L1 缓存的 Key 都可能直接回源。这是收官篇要面对的最后一种故障。

## 实验与验证边界

本章的最小实验可以在仓库根目录执行：

```bash
python3 examples/cache_labs.py 11
```

实验只使用 Python 标准库，已经在制作文档时实际执行。它验证当前章节描述的**教学模型**，而不是 Redis、MySQL、Go 或 Java 中间件的集成性能。真实系统的行为应以固定版本源码、配置和实际环境测试为准。

## 固定源码与参考资料

- [Redis 7.2.4 `src/pubsub.c` — `publishCommand`、`pubsubPublishMessage`](https://github.com/redis/redis/blob/7.2.4/src/pubsub.c)
- [Redis Pub/Sub 官方交付语义说明](https://redis.io/docs/latest/develop/interact/pubsub/)
- [Caffeine API：`invalidate` 的应用侧用法](https://github.com/ben-manes/caffeine/wiki/Population)
