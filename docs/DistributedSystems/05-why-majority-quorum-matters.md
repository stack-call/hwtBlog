---
title: "分布式系统 05：为什么必须超过半数？——从 Sentinel 投票到共识的边界"
sidebar_label: "五、为什么必须超过半数"
sidebar_position: 6
description: "从五个 Sentinel 在网络分区中的相互冲突出发，推导多数派相交，沿 Redis 7.2.4 源码区分故障认定与切换授权，再用一个反例说明多数票为什么还不是完整的日志共识。"
slug: /distributed-systems/05-why-majority-quorum-matters
tags: [distributed-systems, redis, sentinel, quorum, consensus, raft, source-code]
---

# 为什么必须超过半数？——从 Sentinel 投票到共识的边界

上一篇我们观察到了一个危险场景：网络将 Redis 主节点 A 与部分 Sentinel 隔开，另一侧将副本 B 提升为主节点，而旧 A 可能仍在接受写入。我们因此让 Sentinel 为每次故障切换分配新的 Epoch，并要求协调者获得授权。

但是这个规则中有一个数字，一直没有真正解释：**为什么一定要得到超过半数的投票？两票、三票的差别从哪里来？**

先不讲 Raft，也不背 Quorum 的定义。继续沿用 `post:42:views` 的故事，不过这次只观察**谁有权决定下一次切换**。等这个问题讲清楚，再问“获得投票”是否还能保证数据本身正确。

> **源码范围**：固定 [Redis 7.2.4](https://github.com/redis/redis/tree/7.2.4) 的 [`src/sentinel.c`](https://github.com/redis/redis/blob/7.2.4/src/sentinel.c)。Raft 部分只引用 [Raft 论文](https://raft.github.io/raft.pdf) 的安全性原则作对照，不将论文概念伪装成 Redis 的实现。下面的 Python 实验已执行，但不是 Redis 或 Raft 集成测试。

## 一、两个人都说“我可以切换”，为什么不能都算数？

现在有五个 Sentinel：`S1`、`S2`、`S3`、`S4`、`S5`。它们监视同一组 Redis 数据节点 A、B、C；**Sentinel 是做监控和协调的进程，不是复制数据的五个副本**。

假设主节点 A 被网络隔离：

```text
原来：Client -> A（post:42:views=100）
                    |--复制--> B
                    +--复制--> C

网络分区以后，Sentinel 形成两个互相不可达的区域：

左侧：S1、S2             右侧：S3、S4、S5
```

最粗糙的规则是“有两个 Sentinel 同意，就授权切换”。它看上去很宽松：左侧两个 Sentinel 已经能够行动，而不是等其他节点。

但现在让 S1、S2 都支持“由 S1 负责切换”，同时 S3、S4 支持“由 S3 负责切换”。

```text
切换候选者 S1：得到 S1、S2 两票 -> 通过
切换候选者 S3：得到 S3、S4 两票 -> 通过

同一轮出现两个合法的切换协调者
```

注意，这只是一个**错误规则的反例**，不是 Redis Sentinel 会这样批准两个协调者。

它说明的矛盾是：**如果一组投票足以做出决定，那么两组互不重叠的投票者就可能分别支持冲突的决定。**

我们想保留“节点有故障时仍能继续做决定”的能力，又不希望发生两组同时成功的情况。缺少的最小约束是什么？

## 二、让所有成功的投票集合都必须有交集

不妨暂时把门槛从两票提升到三票。

在五个投票者中，任意两组各含三个人的集合，不可能完全互不相交。

例如：

```text
第一组：S1 S2 S3
第二组：      S3 S4 S5
共同成员：    S3
```

如果两组完全不相交，就需要六个不同的人；但整个系统只有五个。

因此我们得到一个极其简单但强有力的性质：**两个满足超过半数门槛的集合，必然相交。**

如果规定“一个投票者在同一个 Epoch 中只能把票投给一个候选者”，那么互相冲突的两个候选者就无法同时获得同一批固定投票者的严格多数。

现在才给这个机制起名字：**Majority Quorum（多数派）**。对于固定的 `N` 个投票者，严格多数的门槛是：

```text
Q = floor(N / 2) + 1
```

当 `N=5`，`Q=3`。当 `N=4`，`Q=3`；刚好一半不能算超过半数。

这里关键的不是“3 比 2 更可靠”这个模糊印象，而是一个可以验证的条件：

```text
|A| + |B| > N   =>   A ∩ B ≠ ∅
```

只有在**投票者集合固定、一轮内一人一票、相应投票记录不会随意丢失**等前提下，才能把集合相交转化为“冲突候选者无法同时获授权”。若系统变更成员名单，则需要额外的配置变更规则；我们这章不展开。

但多数派也并非没有代价。五个节点分为 `2+3` 时，三节点的一侧能够取得多数；两节点的一侧不能。**少数派宁愿不继续作出新的授权，也不能与多数派各自作出互相冲突的决定。**

## 三、一个容易混淆的问题：两个 Sentinel 认为 A 不可用，不就是 Quorum 吗？

回到 Redis Sentinel。假设我们把配置里的故障判断阈值设为 `2`：

```text
sentinel monitor mymaster 127.0.0.1 6379 2
```

这条配置中的 `2`，是确认主节点**客观下线**（ODOWN）所需的意见数量，并不表示“只需两票就能授权普通自动故障切换”。

例如五个 Sentinel 的监控配置中，`quorum=2`：

- S1 认为 A 不可用，S2 也报告 A 不可用，于是可能满足 ODOWN 的判断条件。
- 但如果只有 S1、S2 支持某个切换协调者，它仍没有取得五个 Sentinel 的严格多数（三票）。

为什么要分成两步？

因为这其实是两个不同的问题：

**问题一：有足够多的观察者怀疑 A 已经不可用吗？** 这是进入故障切换流程前的判断，可以配置其灵敏度。

**问题二：究竟允许哪一个协调者去执行切换？** 这必须避免同一轮授权冲突，因此要满足更强的多数派约束。

这样的区分并不是我们凭空编造的。去源码里看。

## 四、真实源码：ODOWN 判断为什么只检查配置的 quorum？

打开 [Redis 7.2.4 `src/sentinel.c` 第 4267 行附近](https://github.com/redis/redis/blob/7.2.4/src/sentinel.c#L4267-L4304)，找到：

```c
void sentinelCheckObjectivelyDown(sentinelRedisInstance *master)
```

这个函数由 Sentinel 的周期性处理逻辑调用。它先确认本地已经主观认为主节点下线（SDOWN），再统计其他 Sentinel 对主节点状态的报告。

**真实源码节选（Redis 7.2.4；仅保留关键条件）：**

```c
if (quorum >= master->quorum) odown = 1;
```

这里的 `quorum` 是本次判断中计数的“认为主节点不可用”的 Sentinel 数量；`master->quorum` 是配置门槛。

下游代码据此置位或清除 `SRI_O_DOWN`，使后续故障切换逻辑知道本地已满足客观下线条件。

**此处没有要求 `quorum > N/2`。** 源码注释甚至明确把 ODOWN 叫作弱形式的意见门槛：网络消息会有延迟，不能把它理解为某个瞬间所有观察者达成了严格同步的结论。

所以，ODOWN 只能回答“是否有足够理由开始考虑切换”，不能回答“谁已经合法获得切换权”。

## 五、真实源码：为什么授权切换仍然需要超过半数？

现在来到真正授权协调者的位置。[`sentinelGetLeader()`](https://github.com/redis/redis/blob/7.2.4/src/sentinel.c#L4447-L4511) 会汇总同一轮 Epoch 的投票。

它的输入是被监控主节点 `master` 和本轮 `epoch`；它首先根据已知 Sentinel 数量计算投票者总数，然后统计候选者获得的投票数。

**真实源码节选（Redis 7.2.4）：**

```c
voters = dictSize(master->sentinels)+1;
```

加一是把当前 Sentinel 自己也算上。

找到票数最多的候选者后，核心判断是：

**真实源码节选：**

```c
voters_quorum = voters/2+1;
if (winner && (max_votes < voters_quorum || max_votes < master->quorum))
    winner = NULL;
```

这三行说明了 Redis 的实际要求：**候选者需要同时满足严格多数门槛，以及配置的 `master->quorum` 门槛。**

用我们的五节点例子计算：

```text
已知 Sentinel 总数：5
配置的故障判断 quorum：2
严格多数：5 // 2 + 1 = 3

一个候选者获得 2 票：不能授权
一个候选者获得 3 票：同时满足两道门槛
```

如果配置门槛高于多数，则还必须满足更高的配置值。本文只比较五节点、配置值为 2 的正常示例。

函数返回的不是 Redis 数据副本，也不会在这里立刻把 B 提升为主节点；它返回的是**获得授权的 Sentinel 候选者标识**，如果没人满足条件则返回空。

谁消费这个结果？

[`sentinelFailoverWaitStart()`](https://github.com/redis/redis/blob/7.2.4/src/sentinel.c#L4733-L4762) 会调用 `sentinelGetLeader()`，再判断得到的协调者是否是自己。对于普通自动故障切换，若未获授权，就留在等待状态，之后可能因超时中止；若已获授权，才进入选择副本的下一阶段。

这里需要特意排除一个例外：**人工触发的 `SENTINEL FAILOVER` 有强制切换路径**。上面的严格授权讨论针对普通自动故障切换，不应把人工操作也说成必须经过完全相同的投票流程。

我们终于能解释前一章的代码为什么要“投票成功以后再选择副本”，而不是“有两人报告主节点故障就开始提主”。

## 六、只有多数派还不够：同一轮必须限制重复投票

再观察一个容易漏掉的前提。

假设 S3 已经给候选者 S1 投过票，稍后又给候选者 S4 投票。那么即使每个候选者都需要三票，仍有机会在不同时间凑出两个冲突的成功集合。

多数派交集中的那个成员，不能被重复使用在同一轮的不同授权中。

因此 Sentinel 需要记录：**本轮 Epoch 我已经投给谁了？**

回到 [`sentinelVoteLeader()`](https://github.com/redis/redis/blob/7.2.4/src/sentinel.c#L4396-L4424)。这段源码在收到更高轮次请求时会推进 `current_epoch` 并刷新配置；随后检查该被监控主节点的 `leader_epoch`，只有还没有在当前请求轮次投过票时，才更新 `master->leader` 和 `master->leader_epoch`，并持久化配置。

**真实源码节选：**

```c
if (master->leader_epoch < req_epoch && sentinel.current_epoch <= req_epoch)
```

这是保存一轮内投票约束的关键判断。`req_epoch` 来自切换协调过程；`leader_epoch` 记录已经投票的轮次。如果相同轮次再次来请求，函数返回已经记录的投票，不会随意改投另一个候选者。

这正好把前面的数学条件落实成工程机制：不仅要有多数派，还要有**稳定的轮次与投票记录**。

这套机制也有工程边界：如果多个 Sentinel 对“当前已知投票者集合”认知不一致，或发生复杂的成员变更，不能只靠那一行公式自动推出所有情形的绝对安全性。数学证明依赖的是同一固定集合，工程实现还需要管理成员视图与配置传播。

## 七、用一个最小实验验证“相交”到底意味着什么

我们不启动 Redis，先写一个不依赖第三方库的 Python 实验，穷举固定投票者集合中是否存在两组互不相交的合法票数。

**教学代码：**

```python
from itertools import combinations

voters = "ABCDE"

def groups(size):
    return [set(x) for x in combinations(voters, size)]


def can_split(size):
    return any(not (a & b) for a in groups(size) for b in groups(size))

assert can_split(2)      # 2 票可以产生互不相交的两组
assert not can_split(3)  # 3 票的任意两组必定相交
```

它验证了五个固定投票者下的集合性质，但并没有模拟消息乱序、宕机后的投票持久化、Redis 的实际网络协议。

完整实验还模拟 `quorum=2`、五个 Sentinel 的 ODOWN 与切换授权区别，并枚举 `N=1..9` 时多数派的交集性质。执行：

```bash
python3 examples/majority_quorum_lab.py
```

**本次已实际运行，输出：**

```text
T1: five voters, threshold=2 -> disjoint winning groups exist
T2: five voters, threshold=3 -> every pair of winning groups intersects
T3: majority intersection holds for n=1..9 in this finite enumeration
T4: ODOWN=True with 2 reports; failover authorized=False with only 2 votes
T5: failover authorized=True after 3 matching votes (fixed five voters)
T6: commit-like majority A/B/C intersects voters C/D/E at C
T7: naive election could pick stale D; intersection alone cannot protect its log
```

前五行和我们刚刚推导的授权规则一致。后两行则故意揭示另一个问题。

## 八、为什么多数派能避免双重授权，却不能自动保证数据正确？

我们一直讨论的是：由哪个 Sentinel 负责切换。这是**控制面的授权**。

但现在把问题改成：新主节点上有没有客户端之前成功写入的数据？

假设五个数据副本分别是 A、B、C、D、E。某次 `post:42:views = 101` 的更新已经存在于 A、B、C 上。

```text
A：101
B：101
C：101
D：100
E：100
```

下一轮，C、D、E 三个节点支持让 D 当新主节点。从纯数学角度看：

```text
保存 101 的多数派：A B C
支持新主 D 的多数派：C D E
相交节点：C
```

**多数派确实相交了，但是 D 自己仍然没有 `101`。**

如果 D 成为新主节点后直接覆盖其他节点，之前的更新仍可能丢失。

这个反例不是对 Raft 的否定，恰恰是为了说明：真正的复制状态机共识，**不能只有多数派投票**。

至少还需要回答两个问题：

1. 允许候选者成为新主节点之前，怎样确认它的日志足够新？
2. 何时能说一条日志记录已经安全提交，并且未来的新主节点必须保留它？

这时才有必要接触 **Raft**。

Raft 不只是“投票选一个 Leader”。它还规定候选者日志的新旧比较、日志匹配和提交规则。例如在 RequestVote 阶段，投票者会根据候选者最后日志条目的任期与索引，拒绝明显落后的候选者。Raft 对“旧任期日志何时算提交”也有额外约束，不能把“写入出现在任意多数节点上”简单套用到所有情况。

这些规则共同服务于一个目标：**已经提交的日志记录，应当在未来被选出的 Leader 中保留。**

详见 [Raft 扩展论文 §5.4（Safety）](https://raft.github.io/raft.pdf)。这里只说明为什么需要这些规则，不在本章突然展开完整 Raft 状态机。

尤其要注意：Redis Sentinel 不是 Raft 复制状态机。Sentinel 投票决定的是谁去协调 Redis 主从切换；它并没有为 Redis 中每一个普通 `SET` 建立 Raft 式多数派日志提交承诺。第四篇旧主可能继续写入的风险，也不会因为本章推导了“多数派相交”就自动消失。

## 九、这一章新增了什么能力？

回看我们的系统：

最初，只要某个 Sentinel 认为主节点不可用，就可以尝试切换。多个 Sentinel 同时这么做，可能产生冲突。

我们于是给切换过程加上 Epoch，约定同一轮只投一次票。

但如果只需要少数票，网络分区仍可能让两个互不相交的区域各自授权一个协调者。

所以我们将授权门槛提升到严格多数。

严格多数保证成功投票集合必然相交，而每轮一次的稳定投票记录使交集上的节点不能同时支持两个冲突决定。

Redis Sentinel 的源码也印证了两层不同职责：**ODOWN 的配置门槛负责“够不够理由怀疑故障”，严格多数加配置门槛负责“有没有资格协调普通自动切换”。**

但我们随后又发现一个新的边界：**多数派选举解决的是决策冲突，不自动保证候选者持有全部正确数据。** 要讨论后一件事，必须把投票规则与日志新旧、复制和提交规则联系起来。

这不是说下一篇必须立即讲完 Raft，而是提醒我们：中间件中那些相似的“Leader”“ACK”“Quorum”，要看它们具体在为哪一种决策提供保证。

## 十、下一步：服务可以故障切换了，为什么容量还是上不去？

到现在为止，我们始终让所有 Redis 数据副本围绕同一份数据工作。`post:42:views` 的写入可以复制到其他节点，主节点故障时也有机会切换。

假设现在有十亿篇文章，每篇都要保存访问次数。我们给 Redis 增加再多副本，也不代表**单个主节点可以存储无限大的完整数据集**。复制带来冗余，却没有自动让总数据被拆开。

下一个自然的问题不再是“谁来接管”，而是：**如果单份数据集已经超过一台机器的容量，怎样让不同机器分别负责不同部分的数据？**

下一篇：《一台机器装不下所有数据怎么办？——从完整复制走向 Redis Cluster 分片》。我们会继续沿用文章访问计数器，先写一个最简单的数据路由，再让它在扩容时暴露新限制，最后阅读 Redis Hash Slot 的实际实现。

---

## 真实源码索引与核验范围

- [Redis 7.2.4 `src/sentinel.c`：`sentinelCheckObjectivelyDown()`](https://github.com/redis/redis/blob/7.2.4/src/sentinel.c#L4267-L4304)：调用者为 Sentinel 周期处理；输入是本地 SDOWN 和其他 Sentinel 的故障报告；副作用是更新 ODOWN 标志。
- [Redis 7.2.4 `src/sentinel.c`：`sentinelVoteLeader()`](https://github.com/redis/redis/blob/7.2.4/src/sentinel.c#L4396-L4424)：处理指定 Epoch 的投票请求；记录本轮支持的协调者并写回配置；后续供投票统计使用。
- [Redis 7.2.4 `src/sentinel.c`：`sentinelGetLeader()`](https://github.com/redis/redis/blob/7.2.4/src/sentinel.c#L4447-L4511)：汇总同轮票数；检查严格多数和配置门槛；返回获授权者标识或空值。
- [Redis 7.2.4 `src/sentinel.c`：`sentinelFailoverWaitStart()`](https://github.com/redis/redis/blob/7.2.4/src/sentinel.c#L4733-L4762)：消费授权结果，决定普通自动切换是否进入副本选择阶段。
- [Redis Sentinel 官方文档](https://redis.io/docs/latest/operate/oss_and_stack/management/sentinel/)：关于 ODOWN、quorum 和多数派授权的说明。
- [Raft 论文 §5.4](https://raft.github.io/raft.pdf)：解释为何只有多数派交集还不足以保证已提交日志不会被新 Leader 丢弃。

**验证边界：** Redis 7.2.4 函数、关键条件及调用关系已与固定标签的原始源码逐项核对。`examples/majority_quorum_lab.py` 已在当前环境实际运行；实验只穷举有限集合和预设状态，没有运行 Redis Sentinel、Raft 或真实网络分区。Raft 日志示例是反例模型，不是某个真实 Raft 实现的执行轨迹。
