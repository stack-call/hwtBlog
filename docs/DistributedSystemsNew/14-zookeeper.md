---
title: "14｜ZooKeeper 为什么把协调状态做成独立服务？"
sidebar_label: "十四、ZooKeeper 为什么把协调做成独立服务"
sidebar_position: 15
description: "ZooKeeper 把协调状态做成独立服务：ZAB、zxid、watch 与顺序保证，以及它做不到的事。"
slug: /distributed-systems-new/14-zookeeper
---

# 14｜ZooKeeper 为什么把协调状态做成独立服务？

前 13 章我们做的都是库存数据本身的复制、提交和路由。现在库存平台还有一个附属任务：每天只允许一台 Worker 执行对账。这个任务与库存状态复制不同，却仍然面对节点宕机、网络分区和领导权变化。我们是否需要为它重新实现一遍日志复制与共识？

## 1. 从一个工作节点开始，为什么要协调？

最初只有 Worker A，每天凌晨执行一次库存对账，完全没有协调问题。增加 B、C 作为备用后，三个 Worker 都运行同样的定时器；如果不加限制，可能同时执行对账并写入重复结果。

最简单方案是硬编码 `A` 为主。但 A 故障后业务无法自动接管。我们缺少的只是少量共享状态：谁当前有执行资格？哪些 Worker 仍然有效？执行资格变化后谁应当得到通知？

这类数据量不大，却很需要正确的版本和故障语义。这正是 ZooKeeper 这种独立协调服务的典型价值。

## 2. 不要让 ZooKeeper 替我们执行库存业务

ZooKeeper 的主要数据模型是一个树形命名空间，节点叫 znode。业务可以在 `/jobs/reconcile` 下面构造领导者选举或锁协议，但 ZooKeeper 不知道库存数据库里有没有重复结算。

例如一个典型思路是：各 Worker 在 `/jobs/reconcile/election/` 下创建**临时顺序节点**；序号最小者获得执行资格，其他 Worker 观察前驱节点是否消失。会话失效后临时节点会删除，从而让等待者竞争下一轮资格。

这里需要观察“临时”和“顺序”分别修复什么问题：临时节点关联会话生命周期，避免死 Worker 永久占位；顺序编号使多个竞争者能够确定稳定的先后关系。Watch 让等待者不必不停轮询。

但 Watch 并不是一个完全可靠的消息日志，客户端需要在通知后重新读取状态，重新建立关注，不能假定每次变化都会恰好收到一次事件。

## 3. 用最小模拟复现会话过期和旧领导者问题

```python title="完整实验：协调资格与最终写入保护是两层"
class Coordinator:
    def __init__(self):
        self.next_token = 0
        self.owner = None

    def grant(self, worker):
        self.next_token += 1
        self.owner = worker
        return self.next_token

class ReconcileStore:
    def __init__(self):
        self.highest_token = 0
        self.reconciled_by = []

    def write(self, token, worker):
        if token < self.highest_token:
            return "STALE"
        self.highest_token = token
        self.reconciled_by.append(worker)
        return "ACCEPTED"

zk_like = Coordinator()  # 教学化简，不是 ZooKeeper 客户端
store = ReconcileStore()
token_a = zk_like.grant("A")
token_b = zk_like.grant("B")  # A 会话失效后重新授权
assert store.write(token_b, "B") == "ACCEPTED"
assert store.write(token_a, "A") == "STALE"
print("accepted owner:", store.reconciled_by)
```

注意：真实 ZooKeeper 不会替库存数据库自动发放全局写入 Fencing Token；这里是把协调版本的思想放在一起理解。必须由业务层构造正确的单调令牌，并让最终存储执行比较。只要 A 仍可以绕过这个检查直接写库，协调服务就不能保证业务不会重复。

## 4. ZooKeeper 自己为什么也要三台甚至五台服务器？

如果 ZooKeeper 只有一个节点，它宕机后所有 Worker 都失去协调能力。为了避免单点故障，ZooKeeper 需要复制自己的协调状态。于是我们熟悉的问题再次出现：写入顺序、主节点选举、持久化、多个副本如何认可同一条变更。

ZooKeeper 使用 ZAB 原子广播协议，针对协调写入形成有序的提议和提交。官方 [ZooKeeper Internals](https://zookeeper.apache.org/doc/current/zookeeperInternals) 描述 `zxid` 的全序、Quorum ACK 和 Leader Activation。普通读可能从本地副本返回旧值，不能把 ZooKeeper 任何读都当成线性一致读取。

## 5. 真实源码精读：为什么必须检查足够的 Quorum？

源码基线：**Apache ZooKeeper `release-3.9.4`**；仓库 `apache/zookeeper`，文件 [`zookeeper-server/src/main/java/org/apache/zookeeper/server/quorum/Leader.java`](https://github.com/apache/zookeeper/blob/release-3.9.4/zookeeper-server/src/main/java/org/apache/zookeeper/server/quorum/Leader.java)，方法 `tryToCommit(Proposal p, long zxid, SocketAddress followerAddr)`。

**真实源码微型节选（省略无关行）：**

```java title="Leader.tryToCommit() 的实际条件"
if (!p.hasAllQuorums()) {
    return false;
}
```

`Proposal` 保存当前待提交提议及其 ACK 集合；调用这一逻辑的场景是 Leader 收集 Follower 的提议确认并尝试推进提交。输入不仅是业务数据，还有提议的 `zxid` 与当前需要满足的 Quorum 配置。若未达到需要的 Quorum，本方法不能把提议宣布提交。后续还有按顺序提交、移出待处理集合和通知 Follower 等动作。

为什么不直接按“收到任意一个 ACK”提交？因为那样不能满足多副本协调状态的提交约束。为什么不在这里执行库存对账？因为 ZooKeeper 只负责它自己的协调状态；真正的工作进程在观察到资格后执行业务。这条职责边界恰恰是独立协调服务存在的原因。

## 6. 为什么独立协调服务既方便又有成本？

业务系统不必自己重复实现日志提交、会话与通知原语；但它增加了一个外部依赖。若 ZooKeeper 多数节点不可用，新的协调变更通常无法正常推进。业务必须定义这时是暂停调度、保留当前任务还是降级。继续让所有 Worker 无条件执行，显然会重新引入重复对账风险。

它也不适合保存海量商品记录、承担极高频库存扣减写入。协调元数据小、正确性要求高、更新频率可控，是它的典型适用条件。

## 7. 为什么 ZooKeeper 的 Watch 不是“可靠消息队列”？

假设 Worker B 正在观察 A 的临时领导节点。A 的会话失效后，该节点被删除，B 收到 Watch 通知，于是检查是否轮到自己。问题是，B 在观察和重新读取之间可能又发生了别的变化；网络断开期间也可能错过中间状态。Watch 的核心用途是提示“相关状态可能变化了，请重新检查”，而不是承诺把每个业务事件恰好投递一次。

因此经典协调程序通常遵循“注册观察—读取当前状态—检查资格—条件不满足则继续等待”的循环，而不是把收到通知直接等同于获得锁。这样设计的必要性来自状态与通知之间的竞态，和我们前面讨论的客户端超时歧义属于不同问题。

## 8. 会话过期为什么不同于进程死亡？

A 可能仍在执行库存对账，但因为长时间网络不可达，它在 ZooKeeper 的会话已经失效，对应临时节点被删除。B 因此获得新的资格。两者可能在一段时间里都还在运行本地业务代码。

这正是我们第 6 章研究 Fencing 的现实用例：**获得协调资格不自动停止旧执行者，必须在最终写入资源上落实隔离**。如果库存数据库不能识别过期资格，单靠临时节点和 Watch 不能保证对账只执行一次。

## 9. 用真实源码把“提议”和“已提交”分开

在 ZooKeeper 3.9.4 的 `Leader.tryToCommit` 中，`Proposal` 持有提议所需的 Quorum ACK 状态；只有 `hasAllQuorums()` 条件满足，才有资格进入后续提交动作。注意“所有必要的 Quorum”不是“收到所有机器的 ACK”：在动态配置转换时，可能需要同时满足新旧配置的相关确认约束。

这段代码恰好呼应第 3 章：为什么不能把“等待所有节点”和“等待具有交集的足够投票集合”混为一谈。它还呼应第 4 章：`Proposal` 作为待提交对象，与最终状态机应用不是同一动作。真实代码中还能看到按 `zxid` 顺序推进提交、维护待应用队列等逻辑，这些都来自我们自己遇过的反例，而不是凭空出现的框架组件。

## 10. ZooKeeper 为什么很适合控制面，却不适合海量库存写？

库存数据可能有数十亿条，变化频繁、需要按 SKU 分片、承受热点写入。若让这些每秒大量业务写入都经过一个主要针对协调元数据设计的有序复制系统，会受到单个协调状态机、存储与提交路径的容量约束。

而对账任务 Leader 身份、Worker 成员与少量关键配置则不同：数据少，但作出错误决定的代价高。把高价值、低容量的协调状态从业务数据中分离，是独立协调服务的主要设计动机。我们不应该把“协调服务使用多数派”误解成“所有业务数据都应该存 ZooKeeper”。

## 11. 真正的控制面数据应该有多大？

一个订单服务每天可能发生数百万次库存预留，但“当前哪个 Worker 负责夜间对账”一天可能只变化几次。后者更适合被高可靠协调服务维护，因为一次错误选主可能造成重复作业；而对每一笔业务库存都走协调服务，则会把高频数据写入推向本不需要的中心化路径。

这不是说 ZooKeeper 性能差，而是它的设计重点是有序、可靠的小规模协调状态，和高吞吐商品数据存储的访问模式不同。系统设计要把**协调元数据**与**业务负载**区分开，不然容易因为使用了一个可靠服务，就把所有业务状态都塞进去。

## 12. 如何沿真实源码追到业务效果？

在 `Leader.tryToCommit` 看到 Quorum 检查后，可以继续寻找提议进入已提交集合、Follower 收到提交通知、状态应用的路径。然后回到客户端：Worker 看到临时节点删除、重新读取并判断选举资格，最后执行对账。

这样你才真正跨过了“这个函数调用那个函数”的表面：Quorum 判断保护的是协调状态的提交；Watch 只是提醒客户端重新观察；业务对账要靠自身的资格检查与幂等约束。三者不能互相替代。

## 13. 与我们自己实现的模型有何不同？

库存复制日志关注每个订单预留的业务结果；ZooKeeper 的复制日志关注协调数据本身。两者都需要处理网络分区、选举与提交，但 ZooKeeper 让其他应用复用这些基础能力。下一章看反方向的选择：**Redis 为什么选择把集群协调集成在自己内部，却不让每次普通 SET 都走多数派日志提交？**


---

[← 上一章：13｜一个订单跨两个分片，怎样避免只成功一半？](./13-cross-shard.md) · [下一章：15｜Redis Cluster 为什么不对普通 SET 使用多数派提交？ →](./15-redis-cluster.md)
