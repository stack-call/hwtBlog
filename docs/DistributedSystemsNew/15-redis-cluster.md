---
title: "15｜Redis Cluster 为什么不对普通 SET 使用多数派提交？"
sidebar_label: "十五、Redis Cluster 为什么不用多数派提交"
sidebar_position: 16
description: "Redis Cluster 对普通 SET 不用多数派提交：异步复制、槽位归属与故障转移之间的取舍。"
slug: /distributed-systems-new/15-redis-cluster
---

# 15｜Redis Cluster 为什么不对普通 SET 使用多数派提交？

上章的 ZooKeeper 为少量协调状态选择有序、多数派参与的提交路径。现在回到库存平台的缓存：商品详情、商品热度或可重建的库存展示数据需要每秒大量读取和更新。若所有缓存 Key 的写入都等待一个全局共识组，会付出额外延迟与协调成本。Redis Cluster 做出了不同的选择。

## 1. 先让一个 Redis 主节点服务缓存

假设应用写入：

```text
SET book:display_stock 10
```

主节点直接处理并返回结果，访问路径短，延迟低。但是主节点宕机后缓存不可用了。加副本并异步复制，可以让副本在一定条件下接管。

这正是我们在第 2 章已经观察过的路径：**主节点返回成功，不保证此刻所有副本都收到同一次更新**。如果 A 返回成功后立刻宕机，而副本尚未收到，那么已确认的缓存写入可能丢失。对可重建缓存，这个取舍可能有价值；对不可重建的资金账本，就很危险。

## 2. 容量问题使 Redis 进一步需要分片

一个节点装不下所有 Key。Redis Cluster 将键空间划分成 16,384 个哈希槽，各主节点负责部分槽。客户端根据槽位路由；当目标节点不负责某槽时，可以收到 `MOVED`，在迁移过程中还可能出现 `ASK`。

```text
Key → CRC16 / Hash Tag → Slot → 当前槽位主节点
```

上面这条路由链和我们第 11、12 章自建的稳定哈希、路由版本和迁移状态一一对应。但 Redis 的具体键槽计算、Hash Tag 和重定向协议应以官方文档为准，不能用我们实验中的 SHA-256 路由代替真实实现。

## 3. 节点之间怎样知道集群变化？

Redis Cluster 节点通过 Cluster Bus 交换 Ping/Pong 和 Gossip 信息。节点首先根据本地超时产生 `PFAIL`（怀疑失效），再根据其他主节点的故障报告达到条件时转为 `FAIL`。这将“我怀疑 A 挂了”和“集群已形成足够故障认知”区分开。

当一个负责槽位的主节点故障时，满足条件的副本尝试晋升，向其他主节点请求投票。获得多数主节点授权后，新主节点通过更高的 `configEpoch` 宣告自己的槽位所有权；其他节点根据配置纪元收敛到更新的归属视图。

这里要注意：**投票的对象是副本晋升和槽位归属，而不是某个 `SET` 命令的业务数据复制。** 不能看到“多数派”三个字，就推断 Redis Cluster 的普通写入与 Raft 日志一样受到多数提交保护。

## 4. 用同一个 A 宕机的时间线比较策略

```text
T0  客户端对 A 执行 SET book:display_stock=8
T1  A 修改自己的数据并返回 OK
T2  复制消息尚未到 A 的副本 A1
T3  A 宕机
T4  A1 被合法晋升并接管槽位
T5  A1 仍可能只有旧值 10
```

这不是副本选举机制本身犯了一个简单错误，而是普通数据复制的成功语义与集群配置晋升的成功语义不同。Redis Cluster 官方规范明确承认某些情况下已确认写入可能丢失。[Redis Cluster Specification](https://redis.io/docs/latest/operate/oss_and_stack/reference/cluster-spec/)。

## 5. 真实源码精读：FAIL 并非凭一个节点的怀疑就产生

固定代码基线：**Redis 8.0**，仓库 [`redis/redis` 的 `src/cluster_legacy.c`](https://github.com/redis/redis/blob/8.0/src/cluster_legacy.c)。可以搜索 `markNodeAsFailingIfNeeded`、`clusterHandleSlaveFailover`、`clusterProcessGossipSection` 和 `clusterFailoverReplaceYourMaster`。这些入口分别对应我们前面的故障认知、晋升选举、成员状态传播和角色/槽位切换问题。

我们不贴整个大函数，只讨论最关键的条件。源文件中的 `markNodeAsFailingIfNeeded` 先检查节点本身是否已经超时，再统计有效故障报告；不足主节点多数时不会把节点推进到 FAIL 状态。以下是 **Redis 8.0 真实源码的关键行节选（去除行尾注释及不相关行，保持原意）**：

```c title="cluster_legacy.c / markNodeAsFailingIfNeeded()"
int needed_quorum = (server.cluster->size / 2) + 1;
if (!nodeTimedOut(node)) return;
failures = clusterNodeFailureReportsCount(node);
if (clusterNodeIsMaster(myself)) failures++;
if (failures < needed_quorum) return;
```

这几行的调用上下文是节点已经收集到别人的故障报告、正在判断是否应将某个节点标为 `FAIL`。`clusterNodeFailureReportsCount` 返回报告数量；如果自己是主节点，本地怀疑也计入投票；达不到 `needed_quorum` 则直接退出，不产生后续 `FAIL` 广播。源码后面才更新故障标记并通知其他节点。可在 [Redis 8.0 原文件对应函数](https://github.com/redis/redis/blob/8.0/src/cluster_legacy.c) 中逐行核对。

它回答的是“凭什么可以向其他节点宣告这个主节点失败”，而不是“写入 `book:display_stock` 是否已经在副本持久化”。后者由普通复制路径负责。理解了这条分工，就不容易把 Gossip、选举与数据强一致性混成同一个协议。

## 6. 为什么 Redis 不选择和 ZooKeeper 完全一样的策略？

对于大量可重建缓存，低延迟和分片扩展是非常重要的目标。同步多数写入可能提高某些已确认数据的故障安全性，却增加写入等待、失败条件和部署成本。

Redis 提供 `WAIT` 等命令让调用方在需要时等待一定数量的副本确认，但这仍不能简单变成“所有 Redis 写入都获得完整共识日志同级保证”。尤其是故障转移、持久化和极端故障条件，需要按真实语义分析。

因此，Redis 的选举多数派用于集群管理，不应误读成每条普通键操作都要经过全局提交。选择它时必须清楚业务是否允许丢失可重建的数据。

## 7. Redis 的 Gossip 传播了什么，没传播什么？

Cluster Bus 心跳消息不仅表示“我还活着”，还携带节点 ID、角色、配置纪元、槽位声明以及其他节点的故障观察。一个节点可能先在本地认为 A 超时，即 `PFAIL`；随后通过 Gossip 收集足够其他主节点的故障报告，将其推进为 `FAIL`。

这些信息是控制面状态。它们不等于正常 Key 的业务值复制。比如节点通过 Gossip 知道“Shard A 当前不可达”，并不会因此知道 `book:display_stock` 的最后一次写入具体是什么。这就是为什么“集群成员状态传播”和“业务数据复制”在 Redis 中走不同的数据路径。

## 8. 为什么配置纪元不能当成每个 Key 的写入版本？

当 A 的副本 A1 晋升并取得更高 `configEpoch` 时，其他节点可以相信它对原槽位的所有权声明更新。但是 A1 可能没有收到 A 在故障前最后一条缓存写入。更高的所有权版本表示“此后谁负责这批槽位”，不表示“旧主节点的所有已确认业务写入都被继承”。

这与第 7 章的 Raft 领导者完整性形成鲜明对照。Redis Cluster 不把普通 Key 更新放进一个经多数派提交的复制日志，因此配置纪元并不承担保护每条已确认写入的职责。工程师不能因为看到选举用了多数票，就推断普通 `SET` 一定不会在故障切换后消失。

## 9. 用一条业务轨迹比较“可重建缓存”和“库存真相”

假设 MySQL 是库存的权威记录，Redis 存 `book:display_stock=8` 作为展示缓存。如果 Redis 主节点故障丢失缓存更新，后台可以重新从数据库计算并填充正确的展示值。用户可能短暂看到旧库存，但库存预留的最终判定仍在数据库事务中进行。

如果我们反过来把 Redis 普通异步复制写入当作不可丢失的库存账本，那么客户端已确认成功后，主节点故障仍可能让账本丢失变更。风险不是靠加大内存、增加副本个数自动消除的，而取决于承诺的确认语义和故障恢复协议。

## 10. 真实源码中的“多数”究竟用在什么地方？

查看 Redis 8.0 的 `cluster_legacy.c` 时，建议先从 `clusterProcessGossipSection` 理解节点如何获得别人的观察，再看故障报告累计与 `markNodeAsFailingIfNeeded`，最后跟进 `clusterHandleSlaveFailover` 和晋升后的槽位声明。

这样沿调用因果链读，你会发现多数主节点的故障报告和副本晋升授权主要针对**集群权威变更**。普通 Key 的复制路径不在这些函数里完成。这比只背“Redis 用 Gossip”更能解释其架构取舍。

另外，Redis Cluster 某些配置和客户端模式会影响网络分区下的行为，不能泛化成“只要 Redis 有三个节点就能容忍一个节点故障”。主节点、对应副本和多数主节点的可达性都需要满足特定条件。

## 11. 如果业务必须保留刚刚确认的库存预留，Redis 应该放在哪一层？

一种更清楚的边界是让事务数据库承担库存真相，Redis 保存可重建的热点展示缓存。应用完成真实预留后，再触发缓存失效或更新；即使缓存主节点故障丢失最新值，也可以从权威数据恢复。你仍须处理缓存短暂不一致和查询穿透，但避免把 Redis 普通异步复制的故障窗口直接放大成不可恢复的业务丢单。

当然，也存在以 Redis 为关键数据存储的业务，但那就必须明确其持久化、复制、故障切换与可接受风险，不能仅靠“Redis Cluster 有副本，所以数据一定安全”作设计结论。

## 12. configEpoch 与普通 Key 版本为何必然不同？

`configEpoch` 只告诉其他节点哪份槽位归属声明更新。它可以帮助多个节点在故障转移后收敛到同一个槽位所有者，但不会记录该槽位内每个 Key 最近写到了哪一条业务日志。新的所有者能否拥有最后一笔写入，是另一条复制路径的结果。

这也解释了 Redis Cluster 为什么可以在控制面使用多数投票，而普通数据面仍保持异步复制。不同状态承受不同级别的正确性保证，是有意的性能与故障语义取舍。

## 13. 为什么下章要看 Kafka？

缓存写入能容忍某些丢失，不代表订单事件也能接受。库存预留成功后，我们要向支付、物流和审计系统发布 `StockReserved` 消息。消息需要稳定顺序和可配置的复制确认。Kafka 既要管理集群元数据，又要管理大量业务日志，因此选择把这两条路径分开。


---

[← 上一章：14｜ZooKeeper 为什么把协调状态做成独立服务？](./14-zookeeper.md) · [下一章：16｜Kafka 为什么把元数据共识和消息复制分开？ →](./16-kafka.md)
