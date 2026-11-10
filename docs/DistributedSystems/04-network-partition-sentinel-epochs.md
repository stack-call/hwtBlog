---
title: "分布式系统 04：两边都认为自己是主，谁的决定算数？"
sidebar_label: "四、两边都是主，谁的决定算数"
sidebar_position: 5
description: "从 Redis Sentinel 的一次网络分区出发，理解为什么需要按轮次授权切换、为什么旧主仍可能写入，以及配置纪元和写入保护的真实边界。"
slug: /distributed-systems/04-network-partition-sentinel-epochs
tags: [distributed-systems, redis, sentinel, network-partition, epoch, source-code]
---

# 两边都认为自己是主，谁的决定算数？

上一篇，我们让文章访问计数器从人工切换走到了 Redis Sentinel 自动故障切换：监视 A，发现主节点不可用，选择一个合适的副本 B，把 B 提升为主节点，并通知客户端使用新地址。

但我们在结尾留下了一个问题：**如果 A 根本没坏，只是网络把它与部分 Sentinel 隔开了，B 又被提升成新的主节点，那么 A 和 B 是否会同时接受写入？**

先不急着讨论 Raft。我们继续沿用 `post:42:views`，让一次网络分区逼出两个不同的需求：**同一轮切换不能有两个协调者**，以及**即使协调者选对了，旧主节点也可能继续写入**。

> **源码范围**：固定 [Redis 7.2.4](https://github.com/redis/redis/tree/7.2.4)。主线追踪 `src/sentinel.c` 的切换授权和配置传播，最后只补看 `src/replication.c` 与 `src/server.c` 的写入保护。文中“真实源码节选”仅引用已核对的关键行；Python 代码是教学模型，不是 Redis 实现。

## 一、我们以为 A 已经故障，实际上只是网络断了

沿用上篇的三台数据节点与三个 Sentinel：

```text
正常时

Client ---> A（主节点：100）
             | \
             |  \----> C（副本：100）
             +-------> B（副本：100）

S1、S2、S3 都能与 A 通信
```

现在发生了一次网络分区：A 和 S1 被留在一侧；B、C、S2、S3 在另一侧。为方便观察，旧侧客户端仍可以访问 A，新侧客户端能访问 B、C。

```text
网络分区后

[ 左侧 ]                      [ 右侧 ]
Client-L -> A                  B、C <- S2、S3
            ^
            S1

       两侧暂时不能互相通信
```

S2、S3 看不到 A。根据上一章已经学过的 SDOWN、ODOWN 判断，它们可能在满足条件后启动故障切换，并选择 B 接管。

但 A 没有停机。A 不知道另一侧发生了什么，仍然可能认为自己是主节点。

这里先不要说“Sentinel 算错了”。**从 S2、S3 能观察到的事实出发，A 故障与 A 被网络隔离可能无法区分。** Sentinel 的工作不是证明机器物理死亡，而是在预定条件下做出可恢复服务的决定。

现在出现了第一个更具体的矛盾：如果 S2 和 S3 都想执行故障切换，谁有权提升副本？

## 二、让每个 Sentinel 自己切换，为什么不够？

假设我们用最简单的逻辑：

```python
# 概念伪代码：错误地把“发现故障”等同于“有权切换”
if sentinel_sees_master_down():
    promote_best_replica()
```

它在只有一个 Sentinel 时看起来可行。一旦有多个 Sentinel，同时切换就可能冲突：

```text
S2：我认为 A 不可用，提升 B
S3：我认为 A 不可用，提升 C

结果：B 和 C 都可能认为自己应该接管
```

注意：这里只是在演示**没有协调规则的错误方案**，不是说 Redis Sentinel 7.2.4 会这样执行。

为了避免同一轮切换被重复执行，我们最先需要的不是“再找一台机器”，而是一个简单的授权规则：

**同一轮故障切换，Sentinel 对一个被监控主节点只能把协调权投给一个候选者。**

不过，还需要解决“同一轮”的含义。否则 S2 可能记住了上周投给 S3 的决定，导致今天永远无法重新授权。反过来，如果忘掉以前投过票，又可能让旧的切换请求重新生效。

所以这时才引入一个新概念：**Epoch（配置轮次 / 纪元）**。

在这一篇，先把它理解为切换决策的递增编号，而不是时间戳。

```text
第 7 轮：S2、S3 给 S2 授权
第 8 轮：如果未来需要新的切换，可以重新授权

同一轮不能把票改给两个候选者
```

这只解决“协调者的授权”问题。它还没有让 A 自动停止写入。我们先去真实源码里确认前半部分。

## 三、Redis Sentinel 怎样给一次切换编号？

打开 [`src/sentinel.c`](https://github.com/redis/redis/blob/7.2.4/src/sentinel.c#L4584-L4595)，找到 `sentinelStartFailover()`。

上一篇我们已经看到：满足 ODOWN 等条件后，Sentinel 可以开始一次故障切换尝试。

**真实源码节选（Redis 7.2.4）：**

```c
master->failover_epoch = ++sentinel.current_epoch;
```

读这行时，先区分两件事：

- `sentinel.current_epoch`：该 Sentinel 当前知道的轮次编号。
- `master->failover_epoch`：此次针对该主节点发起的故障切换所使用的轮次。

谁调用它？`sentinelStartFailoverIfNeeded()` 在监控流程里检查满足启动条件后调用。输入是当前被监控主节点的状态；副作用是标记一次新的切换尝试及其轮次，**还不是已经选出协调者或完成提主**。

为什么需要编号？因为后续投票必须回答：你到底在为哪一次故障切换授权？

### 一轮只能投给一个协调者

继续找到 [`sentinelVoteLeader()`](https://github.com/redis/redis/blob/7.2.4/src/sentinel.c#L4396-L4424)。函数接收请求轮次 `req_epoch`、请求者 ID `req_runid` 和所监控的主节点状态。

**真实源码节选：**

```c
if (master->leader_epoch < req_epoch && sentinel.current_epoch <= req_epoch)
```

只有满足这条轮次条件，Sentinel 才可能为本轮记录一个新的被授权者；记录使用 `master->leader` 和 `master->leader_epoch`。否则会返回已有的投票信息，而不是随意覆盖已投出的票。

这里我们没有贴整个函数，但有一件事不能忽略：**源码在更新轮次和投票状态时会调用 `sentinelFlushConfig()`，尝试把配置写回本地配置文件。** 这是为了让 Sentinel 重启后尽可能保留关键决策状态；它并不意味着所有配置落盘失败都被自动解决。

现在可以回答一个问题：如果 S3 在第 7 轮已经投给 S2，同一轮晚到的另一个请求不会让它重新改投。下一个问题才是：**拿到一票就够了吗？**

## 四、为什么不能只有一票就执行切换？

假设三个 Sentinel 都知道彼此：S1、S2、S3。

如果第 7 轮 S2 给自己投票，而 S3 给自己投票，我们得到：

```text
S2：1 票
S3：1 票
S1：处于另一侧，暂时不可达
```

此时两者都说“我有一票”，却都不应该立刻执行切换。否则我们仍然可能有两个协调者。

因此，一次切换需要**足够多的 Sentinel 授权**。在三名已知 Sentinel、`quorum=2` 的例子里，S2 需要至少两票才能继续。

假设 S3 最终也在第 7 轮投给 S2：

```text
S1：未参与（隔离）
S2：投给 S2
S3：投给 S2

S2 获得 2/3，S3 不能再获得 2/3
```

因为每个 Sentinel 在同一轮只能投一票，所以两个不同候选者不可能同时拿到三名投票者中的多数。

Redis Sentinel 对这里有两层不同的判断，请不要混淆：

1. **ODOWN 判断**：有多少 Sentinel 报告 A 不可用？由被监控主节点的 `quorum` 控制。
2. **切换授权**：哪个 Sentinel 有资格协调本轮切换？必须得到已知 Sentinel 的严格多数，并且不少于 `quorum`。

例如有五个 Sentinel 且 `quorum=2`，两个故障报告可以使 ODOWN 条件成立，但实际进行普通自动故障切换，仍需要至少三个 Sentinel 授权。

### 真实源码：谁统计选票？

打开 [`sentinelGetLeader()`](https://github.com/redis/redis/blob/7.2.4/src/sentinel.c#L4447-L4511)。它读取各 Sentinel 报告的候选者与轮次，汇总候选者的票数，并计算授权门槛。

**真实源码节选：**

```c
voters_quorum = voters/2+1;
```

随后的判断同时检查多数门槛和 `master->quorum`：任何一个未达到，都不会返回胜出的协调者。

谁消费结果？[`sentinelFailoverWaitStart()`](https://github.com/redis/redis/blob/7.2.4/src/sentinel.c#L4733-L4762) 会检查返回的协调者是不是自己。普通自动切换中，如果不是，就不能进入选择、提升副本的阶段；超时还可能放弃本轮尝试。

我们现在终于补齐了上一篇里的那句“假设协调者已获得授权”：它不是凭空产生的布尔值，而来自**同一轮单票记录 + 对足够多 Sentinel 的选票统计**。

这套机制解决的是“多个 Sentinel 不应同时以合法协调者身份发起相互冲突的普通自动故障切换”。但新的问题并没有消失。

## 五、协调者选对了，为什么仍然可能出现两个主节点？

恢复到我们最开始的网络分区：

```text
左侧                           右侧
A：原主节点                   B：被提升的新主节点
S1：仍未收到新配置            S2、S3：已授权本轮切换
Client-L                       Client-R
```

S2 获得授权后，可以向 B 发送提升命令；但它现在**无法向隔离中的 A 发出命令**。

所以我们必须区分两种“唯一”：

- **唯一的切换协调者**：由 Sentinel 的轮次与投票规则约束。
- **某一时刻只有一台 Redis 实例会接受写入**：需要数据节点本身或客户端访问路径上的额外约束。

前者不能自动推出后者。

让两侧客户端各写入一次：

```text
最初两侧都看见 100

Client-L -> A：SET post:42:views 101   -> OK
Client-R -> B：SET post:42:views 102   -> OK

左侧 A=101
右侧 B=102
```

这两次写入属于不同分支。此时不存在一种由 Redis Sentinel 自动执行的、把 `101` 与 `102` 合并成正确访问次数的规则。

如果你把它想象成两个用户分别对计数器做加一，问题只会更明显：哪一边的操作最终被保留，不能由各自返回过 `OK` 来保证。

所以“分裂成两个主节点”并不一定是因为选举出了两个 Sentinel 协调者；**也可能是旧 Redis 主节点还活着，并继续接受旧客户端的写入。**

Redis 官方 Sentinel 文档专门描述了这一故障场景：[High availability with Redis Sentinel](https://redis.io/docs/latest/operate/oss_and_stack/management/sentinel/)。

## 六、网络恢复后，旧主节点为什么会被改成副本？

假设网络恢复，S1 终于能与 S2、S3 交换配置。我们想让整个系统重新对“谁是新主节点”达成相同认识。

最简单的办法是广播：

```text
我的主节点是 B
```

但广播可能乱序，旧消息可能晚到。只比对消息到达时间，很可能把新配置覆盖回旧配置。

我们已经有了 Epoch：为什么不顺便给**已生效的主节点配置**附上版本号？

这时要引入的，不是另一套选举算法，而是区分：

- `failover_epoch`：本次切换尝试的轮次。
- `config_epoch`：某个 Sentinel 当前认可的主节点配置版本。

在 Redis 7.2.4 中，当 Sentinel 后续通过 `INFO` 确认被提升的副本确实成为主节点时，`sentinelRefreshInstanceInfo()` 会把该主节点的 `config_epoch` 更新到本轮成功切换使用的 `failover_epoch`。代码见 [`src/sentinel.c` 对角色变化的处理](https://github.com/redis/redis/blob/7.2.4/src/sentinel.c#L2475-L2501)。

为什么不在“准备提升 B”时立即更新？因为发送提升指令，不等于 B 真正已经成为主节点。必须先观察实际角色改变。

### 真实源码：晚到的旧配置为什么不会覆盖新配置？

Sentinel 通过 Hello 消息传播自己知道的主节点地址和配置轮次。处理入口是 [`sentinelProcessHelloMessage()`](https://github.com/redis/redis/blob/7.2.4/src/sentinel.c#L2639-L2744)。消息格式中包含 `master_config_epoch`。

**真实源码节选：**

```c
if (si && master->config_epoch < master_config_epoch) {
```

只有消息里的主节点配置版本比本地新时，才进入采用新配置的路径。接下来如果主节点地址不同，调用 `sentinelResetMasterAndChangeAddress()` 更新监控对象，并触发相关通知。

这也解释了网络恢复后的一种正常收敛过程：

```text
S1 只知道旧配置：A，config_epoch=6
S2 广播新配置：B，config_epoch=7

S1 发现 7 > 6
    -> 更新自己认识的主节点为 B
```

`sentinelResetMasterAndChangeAddress()` 不只是改一个指针。它会把旧主节点地址纳入待监控的副本集合，以便后续发现并重新配置旧 A。对应实现见 [`src/sentinel.c`](https://github.com/redis/redis/blob/7.2.4/src/sentinel.c#L1472-L1529)。

当旧 A 再次可达，Sentinel 会尝试把它重新配置为 B 的副本。旧 A 因而可能接受新的同步状态，之前只存在于 A 的 `101` 不会自动合并回 B。

**这就是为什么网络分区期间，旧 A 已返回 `OK` 的写入，仍可能在重新加入时消失。**

> **一个必须严谨区分的名称**：上面是 *Redis Sentinel* 的 `config_epoch`。Redis Cluster 也有同名的 `configEpoch`，用于分片归属配置冲突的处理，但它属于另一套机制。不能把 Redis Cluster 的 epoch 冲突规则直接说成 Sentinel 在这里运行的算法。

## 七、有没有办法让旧 A 尽早拒绝写入？

现在我们已经看到真正危险的窗口：A 与其他节点失去联系，但仍旧接受客户端写入。

如果我们能让 A 发现“最近没有任何副本跟得上我”，就暂时不再接受写请求，至少可以缩小分叉窗口。

Redis 提供了下面这组配置：

```conf
min-replicas-to-write 1
min-replicas-max-lag 10
```

可以先把它理解为：如果主节点没有至少一个符合近期 ACK 活跃度要求的在线副本，就拒绝写入。

这与上一章的 `WAIT 1 1000` 不同：

- `WAIT` 是某个客户端在写入之后，主动询问**这次写入的复制位置**被多少副本确认。
- `min-replicas-to-write` 是主节点在接收写命令前，根据**符合条件的副本数量与最近 ACK 时间**决定是否允许写入。

后者并不要求当前每一条写入都已到达副本。因此，**它是降低风险的写入保护，不是强一致或零丢失保证。**

### 真实源码：在哪里阻止写入？

在 [`src/replication.c`](https://github.com/redis/redis/blob/7.2.4/src/replication.c#L3159-L3187) 中，`refreshGoodSlavesCount()` 根据在线状态及最近 ACK 时间维护满足条件的副本数量，`checkGoodReplicasStatus()` 读取这一状态并检查阈值。

然后，在 [`src/server.c`](https://github.com/redis/redis/blob/7.2.4/src/server.c#L3735-L3740) 的命令执行前检查中，写命令如果不满足要求，就被拒绝，而不是先执行再去补偿。

为什么这能缓解分区问题？

在我们画出的拓扑里，A 被隔离到只有自己和 S1 的一侧，没有可通信的 B 或 C。随着副本 ACK 过期，A 能观察到的“合格副本数”下降到零。若启用了上述配置，它会停止接受新写入。

但代价也非常具体：如果只是副本全部出故障，而 A 自己健康，这个配置仍可能迫使 A 拒绝写入。我们是用一部分可用性，换取更小的分叉写入窗口。

还有一个关键限制：发生分区后的短时间内，A 可能尚未发现副本 ACK 已经过期，因此仍存在写入窗口。它也不保证旧主在所有网络拓扑下立即被隔离或拒写。

## 八、这是否已经是 Fencing（隔离旧主）？

我们先不急着给上一节贴这个标签。因为它提供的是“根据副本健康程度拒绝写入”，而不是一套对每次写入都进行**当前领导权校验**的严格机制。

考虑另一种设计：每次切换分配一个新的递增令牌，并让**真正执行写入的存储或受保护资源**检查令牌。

```text
旧 A 持令牌 6 -> 请求写入资源
新 B 持令牌 7 -> 请求写入资源

资源只接受当前有效令牌：7
旧 A 的写入被拒绝
```

这是一种常见的 **Fencing Token** 设计直觉：即使旧协调者或旧工作进程后来恢复，资源也不会接受它持有的过期授权。

然而这要有一个真实前提：**最终写入目标必须执行令牌校验，而且取得有效令牌的过程本身必须可靠。** 仅仅让 Sentinel 维护一个 `config_epoch`，并不能证明一个被网络隔离的旧 Redis 主节点会主动拒绝所有旧客户端写入。

Redis Sentinel 在这里并没有自动把 `config_epoch` 变成所有普通 `SET` 命令必须携带、并由数据节点强制验证的 fencing token。

因此，本章只用 Fencing 来帮助我们看清“协调者获授权”和“旧数据路径被真正隔离”的差别，不把它冒充 Redis Sentinel 的内建强隔离保证。

## 九、最小可运行实验：选举唯一，数据仍然分叉

下面的 Python 实验没有 Redis 进程、网络或真正的 Sentinel 状态机。它固定了一个可观察时间表，仅验证本章两个结论：

1. 如果每个投票者在同一轮只投一票，两个候选者不能同时取得同一组投票者的多数。
2. 就算右侧正确选出切换协调者并提升 B，左侧旧 A 仍可以在没有数据路径保护的情况下接受写入，之后产生丢失。

将代码保存为 `network_partition_lab.py`：

```python
from dataclasses import dataclass, field

@dataclass
class Voter:
    name: str
    votes: dict[int, str] = field(default_factory=dict)

    def vote(self, epoch: int, candidate: str) -> str:
        if epoch not in self.votes:
            self.votes[epoch] = candidate
        return self.votes[epoch]

def authorized(voters, candidate, epoch, quorum=2):
    count = sum(v.votes.get(epoch) == candidate for v in voters)
    return count >= len(voters) // 2 + 1 and count >= quorum

s1, s2, s3 = Voter("S1"), Voter("S2"), Voter("S3")
voters = [s1, s2, s3]

# 第 7 轮：S1 隔离；S2、S3 都给 S2 授权。
s2.vote(7, "S2")
s3.vote(7, "S2")
assert s3.vote(7, "S3") == "S2"  # 同轮不能改投
assert authorized(voters, "S2", 7)
assert not authorized(voters, "S3", 7)
print("T1: S2 获得 2/3 授权，S3 不能同时胜出")

# B 被提升，但隔离中的 A 并没有收到降级命令。
a = {"post:42:views": 100}
b = {"post:42:views": 100}
print("T2: B 被提升；A 仍自认为主节点")

# 两侧分别接受写入。
a["post:42:views"] = 101
b["post:42:views"] = 102
print(f'T3: A={a["post:42:views"]}, B={b["post:42:views"]}')

# 用教学化简模拟 A 重新跟随 B：覆盖旧分支，不做合并。
a.clear()
a.update(b)
assert a["post:42:views"] == 102
print("T4: A 跟随 B；旧分支独有的 101 没有被保留")
```

我另外把同样的因果过程整理成完整脚本 `examples/network_partition_lab.py` 并在当前环境执行过。脚本输出：

```text
T1: epoch=7, coordinator=S2 (2/3 votes); S3 cannot also win
T2: Sentinel promotes B; isolated A still thinks it is master
T3: old branch A=101, new branch B=102
T4: A follows B; old-only write 101 is lost; visible value=102
T5: with min-replicas-to-write=1 and zero fresh replicas, reject writes
```

**实验范围必须说明清楚**：这不是 Redis Sentinel 集成测试；第 7 轮的投票顺序是我们预先安排的；`A` 改为跟随 `B` 是直接修改 Python 字典，不是在模拟 Redis 的全量/增量同步协议；第 T5 行也是人为设定“零个健康副本”后的策略判断，没有模拟 ACK 超时或真实 `NOREPLICAS` 错误。

此外，正文中精简展示的代码只覆盖 T1–T4；完整实验脚本还包含 T5 的条件判断。不要把这两种形式当作相同的逐字输出。

## 十、回看这一章：到底解决了哪两件不同的事？

最初，我们只想在 A 不可用时让 B 接管。引入 Sentinel 后，系统终于能够自动检测和切换。

网络分区逼我们继续追问：如果不止一个 Sentinel 想切换，怎么办？于是我们需要按 Epoch 记录投票，在同一轮给一个协调者足够授权，然后才允许它执行普通自动故障切换。

但当我们观察另一侧的旧主节点 A 时，发现一个更深的边界：**对控制面的切换决策达成一致，不等于数据面已经立即停止所有过期写入。**

Redis Sentinel 会通过配置版本传播使各 Sentinel 最终收敛到新的主节点，并尝试把旧主重新配置为副本。可网络分区期间写到旧主上的数据仍可能丢失。`min-replicas-to-write` 能降低风险，却要付出可用性代价，也不能提供绝对保证。

现在一个新问题自然浮现：我们一直使用“超过半数”来决定谁有权切换，但为什么非要超过一半？如果是五个节点、七个节点，又该如何证明两个互相冲突的决定不能同时得到合法授权？

下一篇我们将暂时离开 Redis Sentinel 的大量工程细节，用一个更小的模型研究：**为什么分布式系统经常需要多数派？** 然后再回到真实系统，区分 Sentinel 的切换授权与真正的复制状态机共识。

---

## 真实源码入口与阅读顺序

1. [`sentinelStartFailover()`](https://github.com/redis/redis/blob/7.2.4/src/sentinel.c#L4584-L4595)：分配本轮 `failover_epoch`；只是开始尝试。
2. [`sentinelVoteLeader()`](https://github.com/redis/redis/blob/7.2.4/src/sentinel.c#L4396-L4424)：按轮次记录协调者投票，并持久化配置。
3. [`sentinelGetLeader()`](https://github.com/redis/redis/blob/7.2.4/src/sentinel.c#L4447-L4511)：汇总投票，检查严格多数与 `quorum`。
4. [`sentinelFailoverWaitStart()`](https://github.com/redis/redis/blob/7.2.4/src/sentinel.c#L4733-L4762)：只有获得授权的普通自动切换协调者才继续提主。
5. [`sentinelRefreshInstanceInfo()`](https://github.com/redis/redis/blob/7.2.4/src/sentinel.c#L2475-L2501)：观察到提主成功后，更新主节点配置轮次。
6. [`sentinelProcessHelloMessage()`](https://github.com/redis/redis/blob/7.2.4/src/sentinel.c#L2639-L2744)：接收并采用更新版本的主节点配置。
7. [`sentinelResetMasterAndChangeAddress()`](https://github.com/redis/redis/blob/7.2.4/src/sentinel.c#L1472-L1529)：切换本地监控视图，并保留旧主节点为待重配置对象。
8. [`refreshGoodSlavesCount()` / `checkGoodReplicasStatus()`](https://github.com/redis/redis/blob/7.2.4/src/replication.c#L3159-L3187) 和 [`src/server.c` 写命令保护](https://github.com/redis/redis/blob/7.2.4/src/server.c#L3735-L3740)：限制不满足副本条件时的写入。

参考：[Redis Sentinel 官方文档](https://redis.io/docs/latest/operate/oss_and_stack/management/sentinel/)；[Redis 7.2.4 固定版本源码](https://github.com/redis/redis/tree/7.2.4)。

**核验边界：** 本章源码函数、关键条件与调用关系已按 Redis 7.2.4 核对；Python 教学实验已执行；没有运行真实 Redis 多进程网络分区测试。本文所说的“旧主写入可能丢失”是 Sentinel 官方文档明确讨论的行为，不是作者声称自己实际制造了真实网络分区。
