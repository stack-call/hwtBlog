---
title: "第八章：主节点故障后谁来接管？从主从复制到 Sentinel 故障转移"
sidebar_label: "08 - Sentinel 故障转移"
sidebar_position: 9
description: "从主从复制无法自动恢复写服务的故障出发，阅读 Redis 7.2.0 Sentinel 源码，理解主观下线、客观下线、选举、副本晋升与新主节点发现。"
tags:
  - Redis
  - Sentinel
  - 高可用
  - 源码精读
---

# 第八章：主节点故障后谁来接管？从主从复制到 Sentinel 故障转移

上一章，我们为活动访问计数服务增加了一个副本。

当主节点执行：

```redis
INCR activity:1001:views
```

复制机制会将相应的写入传播给副本。现在，即使主节点发生故障，至少还可能有另一份数据可供恢复。

然而，**有数据副本，不等于故障发生后业务可以自动继续写入**。

假设当前只有两个 Redis 实例：

```text
活动服务 ── 写入 ──> Redis A（主节点）
                         |
                         | 异步复制
                         v
                      Redis B（副本）
```

A 突然退出后，活动服务仍然尝试连接 A；即便把请求改发给 B，B 此时也可能仍是只读副本，不能直接接管业务写入。

我们其实已经拥有了“第二份数据”，却还没有解决三个具体问题：

1. 谁来发现 A 已经无法正常提供服务？
2. 谁来决定由哪个副本接替 A，避免多个节点同时争抢主节点身份？
3. 选出新主节点以后，应用怎样知道应该连接谁？

本章只研究这条**主节点故障后的自动接管链路**。数据写入为什么会因异步复制而丢失、如何做磁盘恢复，仍然以此前章节的结论为基础，不把它们误认为 Sentinel 已经解决。

> **源码版本**：Redis **7.2.0**，主要文件 [`src/sentinel.c`](https://github.com/redis/redis/blob/7.2.0/src/sentinel.c)。下文的代码分为“真实源码节选”和“教学化简”；后者用于解释分支，不冒充逐字源码。实验步骤是可复现方案，**本章没有在当前写作环境中实际执行 Docker 故障演练**。

## 一、先尝试最简单的接管办法

既然已经有副本，我们可以编写一个外部监控程序。

它每隔一段时间连接主节点，尝试执行 `PING`。如果连续多次失败，就把副本升级成新的主节点：

```python
# 概念伪代码：没有解决网络分区、竞争选主等问题
while True:
    if primary_unreachable():
        promote_replica()
        update_client_address()
    sleep_a_while()
```

对一个完全可控、没有网络故障的简单环境，它似乎足够。

但我们先不要急着增加复杂组件。只提出一个反例：

```text
             网络中断
                X
监控程序 S -------- Redis A（实际上仍在运行）

活动服务 ---------- Redis A（还能够正常写入）

监控程序 S -------- Redis B（副本）
```

对于 S 来说，A 连接失败；但从另一侧看，A 并没有宕机，仍然可能接受写入。

如果 S 立刻把 B 提升为主节点，就可能出现两个都认为自己能够写入的节点。Redis 复制不是为这两个独立写入分支提供自动合并的多主复制协议；后续重新连接时，某一侧的数据可能被覆盖。

因此，**仅凭一个观察者的一次连接失败，就改变整个系统的主从关系，风险太高。**

现在，我们需要一种机制，能够持续观察实例状态，并在准备做故障切换时让多个观察者协调决策。

这就是 Redis Sentinel 要解决的主要问题。

## 二、先增加一个观察者：Sentinel 到底负责什么？

Sentinel 不是第二个用于存储活动访问计数的 Redis 副本。它是一个独立运行的监控和协调进程。

正常情况下，主节点 A 负责写入，B、C 复制 A 的数据。Sentinel 则在旁边持续观察这些节点：

```text
活动服务 ──────────> A（主节点）
                       | \
                    复制  复制
                       |     \
                       v      v
                    B（从） C（从）

                   Sentinel S
                     / | \
                 监控 监控 监控
                   /   |   \
                  A    B    C
```

最小配置可以写成：

```conf
port 26379
sentinel monitor activity redis-a 6379 1
sentinel down-after-milliseconds activity 3000
```

`activity` 是这组主从关系的名字，后面的地址指向当前主节点。最后一个数字 `1` 是判断故障时所需的 **quorum（认可故障的 Sentinel 数量门槛）**；此处只用于演示一个观察者如何监控，不是建议的高可用部署配置。

配置 `down-after-milliseconds 3000` 表示：在满足相关状态判断条件时，Sentinel 可以在持续约 3 秒没有获得可接受响应后，将目标标记为它认为的故障状态。**它不是承诺主节点宕机 3 秒后业务必然完成切换**：发现、投票、晋升、重配置和客户端重连还需要时间。

Sentinel 要判断实例是否正常，就必须主动发送探测命令。Redis 7.2.0 中，入口包括：

- 文件：`src/sentinel.c`
- 函数：`sentinelTimer()`、`sentinelHandleRedisInstance()`、`sentinelSendPeriodicCommands()`

Sentinel 随周期性调度运行；`sentinelHandleRedisInstance()` 会尝试维持连接、发送周期命令，并检查实例状态。`sentinelSendPeriodicCommands()` 使用 `PING`、`INFO` 等命令取得活跃状态与复制角色信息。

这里的 `INFO` 也很重要：Sentinel 不仅要知道副本是否能响应，还要知道它仍然是谁的副本、复制进度到哪里，以便将来必要时挑选接管者。

但到这里，我们只回答了“如何观察”。下一步是：**Sentinel 怎样把探测失败变成故障判断？**

## 三、连接不通就算宕机吗？先看“主观下线”

假设 Sentinel S 连续一段时间没有从 A 收到可接受的回应。

它可以记录一种局部结论：**从我这个观察者的视角看，A 已经不可用**。

Redis 把这类状态称为 `SDOWN`（Subjectively Down，主观下线）。

真实源码入口：[`sentinelCheckSubjectivelyDown()`](https://github.com/redis/redis/blob/7.2.0/src/sentinel.c#L4203)。

**真实源码节选（仅截取超时判断中的一项）：**

```c
elapsed > ri->down_after_period
```

实际函数还会处理连接状态、角色报告不符及主节点重启等情形，并不只检查这一项。

它的核心逻辑可以用下面的**教学化简**表示：

```c
if (不可达时间超过阈值 || 其他满足下线条件) {
    标记这个实例为 S_DOWN;
} else {
    清除 S_DOWN 标记;
}
```

状态变化时，可以在 Sentinel 日志中看到类似：

```text
+sdown master activity ...
```

**不要把这个日志解释成“Redis A 确定已经崩溃”。** 它只代表当前 Sentinel 的观察结论。网络问题、节点过载、长时间无响应，都可能形成这种结果。

这就引出了我们刚才的反例：单个观察者的判断仍然不够可靠。我们需要让多个 Sentinel 交换自己对主节点的看法。

## 四、多个 Sentinel 都认为主节点有问题，才能触发下一步

假设部署三个 Sentinel：S1、S2、S3，监控同一组主从节点。

```text
             A（主节点）
             /   |   \
           S1   S2   S3
```

如果只有 S1 访问不到 A，而 S2 和 S3 仍能正常访问，我们不应马上启动主节点切换。

因此，在尝试故障转移之前，Sentinel 会询问其他 Sentinel 对 A 的判断。

Redis 7.2.0 中，可从 [`sentinelAskMasterStateToOtherSentinels()`](https://github.com/redis/redis/blob/7.2.0/src/sentinel.c#L4348) 找到发送询问的路径。它使用 Sentinel 间的 `SENTINEL is-master-down-by-addr` 请求；响应由 `sentinelReceiveIsMasterDownReply()` 处理。

当当前 Sentinel 收集到足够多的故障报告，就可以将主节点标记为 `ODOWN`（Objectively Down，客观下线）。

关键源码在 [`sentinelCheckObjectivelyDown()`](https://github.com/redis/redis/blob/7.2.0/src/sentinel.c#L4273)。

**真实源码节选：**

```c
quorum = 1; /* the current sentinel. */
```

随后会计入其他已报告主节点不可用的 Sentinel；达到配置的 `master->quorum` 时，才产生 `ODOWN` 状态。

这一段最重要的不是记住 `SDOWN` 和 `ODOWN` 两个缩写，而是理解为什么需要区分它们：

```text
S1 发现 A 不可达
        |
        v
S1 记录本地判断：SDOWN
        |
        v
S1 向其他 Sentinel 询问
        |
        +-- 认可故障的人不够 -> 不能形成 ODOWN
        |
        +-- 达到 quorum --------> ODOWN
```

例如，三个 Sentinel 的 `quorum=2`。如果 S1 与 S2 都认为 A 不可用，S1 就可能将 A 标记为 `ODOWN`。

但**达到 quorum 仍不等于某个 Sentinel 现在就能任意提升副本**。

原因很具体：S1 和 S2 可能都认为自己应该执行切换。如果它们在网络异常时各自操作，仍会发生协调冲突。

因此还需要另一个决定：**到底授权谁来执行这一次故障转移？**

## 五、为什么达到 quorum 后，还要选出一个执行者？

现在我们假设：三个 Sentinel 都已经互相发现，S1 和 S2 认为主节点出现故障。

这已经足够触发故障转移的尝试，但一次切换需要有人统一执行：

- 选哪个副本作为新主节点？
- 什么时候通知这个副本改变角色？
- 什么时候改造其他副本的复制关系？

如果每个 Sentinel 都独立执行，可能得到相互冲突的方案。因此，需要在当前故障转移轮次内，授权一个 Sentinel 作为执行者（通常称为 leader）。

这里的门槛与前面的 `quorum` 不完全一样。

以三个 Sentinel、`quorum=2` 为例：

| 阶段 | 需要什么 | 当前例子的门槛 |
| --- | --- | --- |
| 判定 `ODOWN` | 足够多的 Sentinel 报告主节点不可用 | 2 份故障判断 |
| 授权故障转移 | 选举候选者得到多数授权，同时满足 quorum | 至少 2 票 |

由于本例有 3 个 Sentinel、quorum 也是 2，数字碰巧相同；但两种判断**职责不同**。例如 5 个 Sentinel 且 quorum 为 2 时，形成 `ODOWN` 需要 2 份故障判断，而授权切换通常需要至少 3 票的多数支持。

这一约束还意味着：如果 3 个 Sentinel 中只有一个仍能与其他 Sentinel 通信，它无法独自完成正常自动故障转移。

为什么宁愿暂时不可写，也不允许少数一侧随意选主？因为在网络分区中，少数一侧看不到其他节点，不能证明另一侧没有仍在服务的主节点。多数授权是在可用性与多主冲突风险之间做出的明确取舍。

Redis 7.2.0 的 [`sentinelGetLeader()`](https://github.com/redis/redis/blob/7.2.0/src/sentinel.c#L4453) 会汇总选票，计算需要达到的多数门槛，并同时检查 `quorum`。[`sentinelVoteLeader()`](https://github.com/redis/redis/blob/7.2.0/src/sentinel.c#L4401) 记录当前轮次的投票。

**教学化简：**

```text
收到故障报告
      |
      v
尝试发起一个新的选举轮次（epoch）
      |
      v
其他 Sentinel 对候选执行者投票
      |
      +-- 票数不足 -> 当前尝试不能继续
      |
      +-- 达到多数且满足 quorum
                 |
                 v
             获得执行授权
```

源码中的 `epoch` 是配置和投票轮次的版本标识，帮助多个 Sentinel 区分不同的切换尝试。此处不需要提前讨论完整的分布式一致性理论；先理解它为何存在：**旧轮次的授权不应被误当成新轮次的决定。**

现在我们终于有了一个被授权执行切换的 Sentinel。

新的问题是：**主节点 A 已经不可用，那么到底选 B 还是 C？**

## 六、副本不是随便选的：如何减少接管风险？

假设有两个副本：

```text
A（原主节点）
  |
  +------> B（副本，复制进度较新）
  |
  +------> C（副本，复制进度较旧）
```

如果 B 和 C 都健康，它们不一定拥有完全相同的数据。上一章已经知道，Redis 主从复制默认是异步的。

直接随机选择一个副本，就可能错过更适合接管的候选者。

因此 Sentinel 需要筛选和比较副本。这时才有必要引入两个最小概念：

- **`replica-priority`**：副本的晋升优先级；数字越小越优先，`0` 表示不允许被选为新主节点。
- **复制偏移量**：副本处理复制数据的进度；在其他优先级条件相同的情况下，通常更高的偏移量更受偏好。

Redis 7.2.0 的 [`sentinelSelectSlave()`](https://github.com/redis/redis/blob/7.2.0/src/sentinel.c#L4690) 会先排除不可达、数据状态不够新、优先级不允许晋升等候选者，再通过 [`compareSlavesForPromotion()`](https://github.com/redis/redis/blob/7.2.0/src/sentinel.c#L4662) 对可用副本排序。

排序优先考虑更低的副本优先级，再比较更大的复制偏移量，最后使用运行 ID 作为确定性的进一步比较依据。

因此，需要特别避免一个误解：**Sentinel 不是简单地“谁的数据最新就一定选谁”**。它先检查候选者是否具备晋升资格，再按既定排序条件作选择。

假设 B、C 的优先级都相同，而 B 的复制进度更靠前，那么 B 更可能被选中。

但即使选中了 B，B 当前仍是一个副本。

选中候选者并不能自动让它开始承担写入。下一步，才是实际的角色切换。

## 七、选中了副本，怎样让它真正成为主节点？

此时，我们已经拥有：

1. 对 A 的故障判断；
2. 一个获得切换授权的 Sentinel；
3. 一个通过健康状况和复制信息筛选出的副本 B。

现在要做的事情反而很明确：**告诉 B 不要再复制原主节点，让它成为新的主节点。**

对一个 Redis 副本，我们可以手动执行：

```redis
REPLICAOF NO ONE
```

Sentinel 的真实源码采用相应的兼容命令形式进行切换。入口位于 [`sentinelFailoverSendSlaveOfNoOne()`](https://github.com/redis/redis/blob/7.2.0/src/sentinel.c#L4780)。

**真实源码节选：**

```c
retval = sentinelSendSlaveOf(ri->promoted_slave,NULL);
```

这里传入 `NULL` 的地址，表示让选中的副本不再追随旧主节点。

但发出命令还不够。假设命令根本没有被成功处理，Sentinel 却提前宣布 B 已经是主节点，就又会产生错误。

所以 Sentinel 要等待观察到 B 的角色确实发生了变化。

Redis 7.2.0 不只是检查切换命令有没有发送出去，而是通过后续的 `INFO` 信息确认副本报告自己已经成为 master。相关处理在 [`sentinelRefreshInstanceInfo()`](https://github.com/redis/redis/blob/7.2.0/src/sentinel.c#L2326) 的角色变化分支中。

当被选中的副本被确认提升后，Sentinel 会更新故障转移阶段，并记录新的配置版本。

于是运行状态变成：

```text
切换前：

A（主，故障）
  |
  +-- B（从）
  +-- C（从）

切换中：

Sentinel -> 选中 B
         -> 通知 B 脱离 A
         -> 观察 B 报告 master 角色

切换后：

B（新主）
  |
  +-- C（逐步重新指向 B）

A（故障，尚未恢复）
```

此时业务的写入口已经可以逐步恢复，但 C 可能还保持旧复制关系。

为了让系统重新成为一个一致的主从拓扑，Sentinel 还需要让其余副本改为复制新主节点。

源码入口是 [`sentinelFailoverReconfNextSlave()`](https://github.com/redis/redis/blob/7.2.0/src/sentinel.c#L4871)，它会对尚未重新配置的副本发送新的复制目标，并根据 `parallel-syncs` 限制同时进行的重新配置数量。

至此，故障转移不只是“把 B 标记成 master”，还包括逐步恢复围绕 B 的复制关系。

## 八、为什么源码里有那么多 failover_state？

前面我们按照问题顺序推导了故障转移需要的动作。现在再看源码，才会发现 `sentinelFailoverStateMachine()` 其实很自然。

它在 Redis 7.2.0 中负责依据当前状态推进故障转移；核心分支包括：

```text
WAIT_START
    |
    | 获得本轮切换授权
    v
SELECT_SLAVE
    |
    | 找到合适副本
    v
SEND_SLAVEOF_NOONE
    |
    | 向目标副本发送晋升命令
    v
WAIT_PROMOTION
    |
    | 在 INFO 中观察到角色改变
    v
RECONF_SLAVES
    |
    | 让其余副本追随新主节点
    v
UPDATE_CONFIG
    |
    | 切换监控对象的主节点地址
    v
新主节点配置对外可见
```

这张图是**教学化的正常路径**，不是源码中唯一可能发生的路径：选举失败、找不到候选副本、角色提升超时等情况可能导致中止或重试。

为什么要把切换过程拆成多个状态？因为其中存在大量跨网络的异步操作。

例如，Sentinel 向 B 发出晋升命令后，不能假设下一行 C 代码就能立刻得到“B 已成为主节点”的事实。它需要等待接下来的 `INFO` 报告，再进入后续阶段。

这与我们在第二章学习的事件循环并不矛盾：Sentinel 可以在不同事件回调和定时调度之间持续推进一个尚未完成的故障转移，而不必用一个长时间阻塞的函数守着所有步骤。

我们现在回头看 [`sentinelFailoverStateMachine()`](https://github.com/redis/redis/blob/7.2.0/src/sentinel.c#L4936)，就不会觉得 `WAIT_START`、`WAIT_PROMOTION` 等名字是突然出现的抽象；它们分别对应刚才遇到的等待或决策边界。

再往前追溯一层，定时入口 [`sentinelHandleRedisInstance()`](https://github.com/redis/redis/blob/7.2.0/src/sentinel.c#L4979) 会在检测主节点状态后调用故障转移启动判断以及状态机推进。

这就是“检测故障”和“执行接管”最终在源码中连到一起的位置。

## 九、主节点换了，应用怎么知道连接谁？

假设 B 已经成功成为新主节点。

但活动服务最初写死的是：

```python
# 教学伪代码
redis_client = Redis(host="redis-a", port=6379)
```

只要它一直向 A 发请求，业务就不会因为 B 成功晋升而自动恢复。

所以，完成 Redis 实例之间的角色切换后，还要解决**客户端发现新主节点**的问题。

Sentinel 对外提供了查询当前主节点地址的命令：

```redis
SENTINEL get-master-addr-by-name activity
```

它返回当前 `activity` 这组服务对应的主节点地址和端口。

支持 Sentinel 的客户端库通常会先向一个或多个 Sentinel 查询主节点地址，再建立数据连接；发生故障后重新发现并连接新主节点。已有连接中的请求可能失败，客户端仍需要处理重连、超时和不确定写入结果，不能假设 Sentinel 让整个切换对每个请求完全无感。

这里还要分清两个概念：

- **发现新主节点**：客户端知道现在应该连接哪个地址。
- **保证业务写入不重复、不丢失**：需要结合请求重试语义、复制延迟和业务幂等性单独考虑。

Sentinel 主要帮助完成前者，并不自动完成后者。

## 十、实验：用 1 主 2 从 + 3 个 Sentinel 观察一次切换

接下来用一个隔离的 Docker Compose 环境复现实验。

实验仍然使用活动访问计数 `activity:1001:views`。为了只观察 Sentinel 的控制过程，三个 Redis 数据节点关闭 RDB 和 AOF；这使得**实验不能用于证明故障后的数据持久性**。

下面的容器没有向宿主机发布 Redis 或 Sentinel 端口，只允许同一 Compose 网络内的服务访问。配置中的 `protected-mode no` 和无认证仅适用于这种受控本地实验，不适合作为生产部署模板。

### 10.1 创建实验目录

```bash
mkdir redis-sentinel-lab
cd redis-sentinel-lab
```

创建 `sentinel.conf`：

```conf
port 26379
bind 0.0.0.0
protected-mode no
dir /data

sentinel resolve-hostnames yes
sentinel monitor activity redis-master 6379 2
sentinel down-after-milliseconds activity 3000
sentinel failover-timeout activity 20000
sentinel parallel-syncs activity 1
```

这里的 `quorum=2` 与三个 Sentinel 搭配，能够同时展示故障判断和多数授权的差别。

创建 `compose.yaml`：

```yaml
services:
  redis-master:
    image: redis:7.2.0
    command:
      - redis-server
      - --save
      - ""
      - --appendonly
      - "no"
      - --protected-mode
      - "no"

  redis-r1:
    image: redis:7.2.0
    command:
      - redis-server
      - --replicaof
      - redis-master
      - "6379"
      - --save
      - ""
      - --appendonly
      - "no"
      - --protected-mode
      - "no"
    depends_on:
      - redis-master

  redis-r2:
    image: redis:7.2.0
    command:
      - redis-server
      - --replicaof
      - redis-master
      - "6379"
      - --save
      - ""
      - --appendonly
      - "no"
      - --protected-mode
      - "no"
    depends_on:
      - redis-master

  sentinel-1:
    image: redis:7.2.0
    command: ["sh", "-c", "test -f /data/sentinel.conf || cp /config/sentinel.conf /data/sentinel.conf; exec redis-server /data/sentinel.conf --sentinel"]
    volumes:
      - ./sentinel.conf:/config/sentinel.conf:ro
      - sentinel1-data:/data
    depends_on:
      - redis-master
      - redis-r1
      - redis-r2

  sentinel-2:
    image: redis:7.2.0
    command: ["sh", "-c", "test -f /data/sentinel.conf || cp /config/sentinel.conf /data/sentinel.conf; exec redis-server /data/sentinel.conf --sentinel"]
    volumes:
      - ./sentinel.conf:/config/sentinel.conf:ro
      - sentinel2-data:/data
    depends_on:
      - redis-master
      - redis-r1
      - redis-r2

  sentinel-3:
    image: redis:7.2.0
    command: ["sh", "-c", "test -f /data/sentinel.conf || cp /config/sentinel.conf /data/sentinel.conf; exec redis-server /data/sentinel.conf --sentinel"]
    volumes:
      - ./sentinel.conf:/config/sentinel.conf:ro
      - sentinel3-data:/data
    depends_on:
      - redis-master
      - redis-r1
      - redis-r2

volumes:
  sentinel1-data:
  sentinel2-data:
  sentinel3-data:
```

Sentinel 会重写自己的配置文件，记录发现的 Sentinel、配置纪元和新主节点信息。因此这里把模板复制到各自可写的 `/data/sentinel.conf`，并给三个 Sentinel 使用独立数据卷；不能把一份只读配置文件直接当作运行时需要重写的文件。

### 10.2 启动并确认拓扑

```bash
docker compose up -d
```

先检查主节点状态：

```bash
docker compose exec -T redis-master redis-cli INFO replication
```

关注其中的：

```text
role:master
```

再查看两个副本：

```bash
docker compose exec -T redis-r1 redis-cli INFO replication
docker compose exec -T redis-r2 redis-cli INFO replication
```

理想的稳定状态下，二者都报告 `role:slave`，并显示与主节点的复制连接状态。`depends_on` 只控制启动顺序，不保证复制已经完成，因此需要等到副本真正同步后再进行故障实验。

检查 Sentinel 是否发现了其他 Sentinel，且达到故障转移所需的通信条件：

```bash
docker compose exec -T sentinel-1 \
  redis-cli -p 26379 SENTINEL sentinels activity

docker compose exec -T sentinel-1 \
  redis-cli -p 26379 SENTINEL CKQUORUM activity
```

在三个 Sentinel 互相发现、可正常通信后，`CKQUORUM` 应报告可以达到相应门槛。不要把启动后立即查到的暂时结果当成最终状态。

### 10.3 写入活动计数，并确认副本已有数据

```bash
docker compose exec -T redis-master \
  redis-cli SET activity:1001:views 100
```

接着分别读取两个副本：

```bash
docker compose exec -T redis-r1 \
  redis-cli GET activity:1001:views

docker compose exec -T redis-r2 \
  redis-cli GET activity:1001:views
```

预期在复制追上后，两者都读取到 `100`。

如果某次立即读取仍然为空，先检查复制状态并稍后重试；这体现的是异步复制的时间差，而不是应该随意假设副本立刻与主节点完全一致。

### 10.4 模拟主节点故障

停止数据主节点 A：

```bash
docker compose stop redis-master
```

注意：这里只停止原主节点容器，三个 Sentinel 及两个副本继续运行。

现在从 Sentinel 视角观察：

```bash
docker compose logs --since 2m sentinel-1 sentinel-2 sentinel-3
```

在条件满足且切换成功的情况下，日志中可以依次观察到部分如下事件（出现在哪个 Sentinel、事件间隔和具体顺序会变化）：

```text
+sdown
+odown
+try-failover
+elected-leader
+selected-slave
+promoted-slave
+switch-master
```

**这些是预期可能出现的关键事件名，不是实际运行日志截图。** 不同 Sentinel 只会看到自己相关的部分日志；例如执行 leader 的完整状态推进，不一定会在每个进程中以相同顺序出现。

也不要把 `+sdown` 当成切换已完成，真正对业务有意义的是：新的主节点已经被确认、Sentinel 报告的新地址正确，并且客户端能够连到它。

### 10.5 查询新主节点并尝试写入

查询当前主节点：

```bash
docker compose exec -T sentinel-1 \
  redis-cli --raw -p 26379 \
  SENTINEL get-master-addr-by-name activity
```

结果应是两个字段：主节点 IP（或按配置返回的主机名）以及端口 `6379`。新主节点可能是 `redis-r1`，也可能是 `redis-r2`，**不要预设必须提升哪一个**。

为了避免手工猜测容器地址，可以在 Sentinel 容器内先查询、再连接：

```bash
docker compose exec -T sentinel-1 sh -c '
  set -- $(redis-cli --raw -p 26379 \
    SENTINEL get-master-addr-by-name activity)
  redis-cli -h "$1" -p "$2" \
    INCR activity:1001:views
'
```

只有当选主完成、复制数据已达到实验前观察的状态时，首次 `INCR` 才有望从 `100` 变为 `101`。若复制尚未跟上，或者切换期间仍在进行，结果和暂时错误可能不同；这正是需要区分“可用性恢复”和“数据无损”的原因。

随后检查新旧副本角色：

```bash
docker compose exec -T redis-r1 redis-cli ROLE
docker compose exec -T redis-r2 redis-cli ROLE
```

正常切换完成后，其中一个应报告 `master`，另一个最终应成为它的副本。

### 10.6 原主节点恢复后会怎样？

```bash
docker compose start redis-master
```

Sentinel 会继续观察旧主节点。稳定后，它应被重新配置为跟随当前新主节点，而不是仅因为最初服务名叫 `redis-master` 就自动夺回主节点身份。

再次查询：

```bash
docker compose exec -T redis-master redis-cli INFO replication
```

等待重新配置后，旧主节点通常会变成 `role:slave`。

这里有一个实验环境特有的边界：我们使用了命令行参数固定副本初始上游，且没有为数据节点配置可重写的配置文件。因此容器被重新创建或重启后，角色和复制关系可能回到启动参数指定的状态。它适合观察**一次运行中的故障切换**，不代表生产环境完整的配置持久化方案。

如果只是 `start` 停止过的原主节点，旧节点在被 Sentinel 重新配置前也可能短暂报告 `master`。实验中不要在这个阶段向旧节点直接写入业务数据。生产部署需要配合 Sentinel-aware 客户端及节点访问控制，避免旧地址被当成可用主节点继续使用。

### 10.7 清理实验

```bash
docker compose down -v
```

`-v` 会同时删除这次 Compose 实验创建的 Sentinel 数据卷；不要在复用真实数据卷的环境中照搬。

## 十一、Sentinel 解决了故障接管，但不等于零数据丢失

现在回顾整条链路：

```text
活动访问计数服务
      |
      v
A（主） ──异步复制──> B、C（副本）
      |
      | A 不再响应
      v
Sentinel 发现 SDOWN
      |
      | 收集足够的故障报告
      v
ODOWN
      |
      | 获得多数授权
      v
故障转移 leader
      |
      | 选出可用副本
      v
B 晋升为新主
      |
      | 其余副本重新配置
      v
Sentinel 发布新的主节点信息
      |
      v
客户端发现新地址并恢复请求
```

这条链路回答了本章最开始的三个问题：谁判断故障、谁执行接管、应用如何找到新的写入口。

但它不能保证：

**第一，所有已返回成功的写入都必然出现在新主节点上。**

主从复制默认是异步的。如果 A 在把最后一次 `INCR` 传播给 B、C 前故障，即使 `INCR` 曾向客户端返回成功，新主节点也可能没有收到这条写入。

**第二，故障切换没有服务中断。**

从主观下线、达成共识、领导者选举、晋升到客户端重连，都需要时间；期间请求可能失败或超时。

**第三，网络分区下所有客户端都永远只可能看到一个可写节点。**

Sentinel 利用多数授权减少危险的并发切换，但如果原主节点仍在网络另一侧接收旧客户端写入，不能把 Sentinel 理解成一个跨网络分区的全局写入栅栏。客户端路由与重试必须遵循当前已发布的主节点配置。

**第四，应用的业务操作自动实现幂等。**

例如 `INCR` 在新主节点已经执行成功，但客户端没有收到响应，重试仍可能再次递增。这是业务请求确认和重试语义问题，不能仅靠 Sentinel 解决。

因此，本章应该得到的结论不是“加上 Sentinel 就万无一失”，而是更具体的：

> **主从复制提供可接管的数据副本；Sentinel 负责观察故障、协调一次主节点切换，并对外提供新的主节点位置信息。**

这已经是从“一个独立内存数据服务”走向“可发生节点故障并尝试自动恢复写服务”的重要一步。

## 十二、这一章的源码路径如何串起来？

读源码时，可以顺着下面这条最小正常路径定位，而不要一开始试图看完 `sentinel.c` 的所有状态与数据结构。

| 当前问题 | 真实源码入口 | 这一层的作用 |
| --- | --- | --- |
| Sentinel 如何持续观察实例？ | `sentinelTimer()` → `sentinelHandleRedisInstance()` | 周期调度监控和动作 |
| 怎样发送探测请求？ | `sentinelSendPeriodicCommands()` | 取得 PING、INFO 等状态 |
| 一个观察者认为主节点不可达怎么办？ | `sentinelCheckSubjectivelyDown()` | 设置或撤销 `SRI_S_DOWN` |
| 怎样询问其他 Sentinel？ | `sentinelAskMasterStateToOtherSentinels()` | 请求其他观察者的判断 |
| 多份故障报告怎样形成结论？ | `sentinelCheckObjectivelyDown()` | 检查 quorum，维护 `SRI_O_DOWN` |
| 谁获得切换授权？ | `sentinelGetLeader()`、`sentinelVoteLeader()` | 按 epoch 汇总投票与授权 |
| 哪个副本值得晋升？ | `sentinelSelectSlave()` | 筛选健康候选者并比较优先级与进度 |
| 怎样推进真实角色切换？ | `sentinelFailoverStateMachine()` | 在多个异步阶段间推进 |
| 怎样发送晋升命令？ | `sentinelFailoverSendSlaveOfNoOne()` | 通知候选副本脱离旧主 |
| 怎样确认晋升？ | `sentinelRefreshInstanceInfo()` | 根据 INFO 观察新的角色 |
| 怎样重新组织其他副本？ | `sentinelFailoverReconfNextSlave()` | 让其余副本追随新主 |
| 怎样更新当前主节点配置？ | `sentinelFailoverSwitchToPromotedSlave()` | 完成主节点地址切换 |

所有函数都位于 Redis 7.2.0 的 [`src/sentinel.c`](https://github.com/redis/redis/blob/7.2.0/src/sentinel.c)。需要强调：函数名相近不代表可以省略调用者、状态和返回条件。阅读时应在每一阶段观察相应的状态标志、投票结果和角色报告，不能只背这些函数名。

## 十三、从 Sentinel 自然走向下一个问题

我们现在有了一套主从复制与故障转移机制。

活动访问计数被写入主节点，再复制到副本。主节点故障时，Sentinel 可以尝试提升一个副本，帮助客户端找到新的写入口。

但假设业务继续增长，活动越来越多：

```text
activity:1001:views
activity:1002:views
activity:1003:views
...
activity:999999:views
```

无论准备多少个完整副本，**每个副本保存的仍然是这组数据的完整复制，而不是把数据拆开放在不同主节点上**。

如果单个主节点已经无法容纳所有数据，或者一台机器的处理能力无法承担全部请求，那么继续添加相同数据的副本，并不能直接解决主节点的存储容量问题。

这就留下了新的问题：

**能不能让不同的 Key 分别存储在不同的主节点上，让整个数据服务的容量和请求处理能力不再全部集中在一台机器上？**

下一章，我们从这个容量限制出发，研究 Redis Cluster 为什么需要把 Key 分散到多个主节点，数据如何定位，以及为什么一个简单的 `GET` 也可能收到 `MOVED` 响应。

**下一章：《一台 Redis 放不下所有数据怎么办？从单主节点到 Redis Cluster》**

---

## 参考资料与验证边界

- [Redis 7.2.0 源码：`src/sentinel.c`](https://github.com/redis/redis/blob/7.2.0/src/sentinel.c)
- [Redis 7.2.0 的 `sentinel.conf`](https://github.com/redis/redis/blob/7.2.0/sentinel.conf)
- [Redis 官方 Sentinel 指南](https://redis.io/docs/latest/operate/oss_and_stack/management/sentinel/)
- [Redis 官方主从复制文档](https://redis.io/docs/latest/operate/oss_and_stack/management/replication/)

**源码核对范围**：本章已定位并阅读 Redis 7.2.0 中的故障探测、`ODOWN` 形成、投票、候选副本选择、故障转移状态机、晋升确认和配置切换路径。代码块中明确标注为“教学化简”的内容只是解释性表达。

**实验验证范围**：本文提供 Docker Compose 配置、命令、观察点和预期状态，但未在当前写作环境实际启动容器执行切换。因此日志中的事件名及切换后的状态均应作为实验观察目标，不是声称已经得到的测试结果。
