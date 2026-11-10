---
title: "分布式系统 03：主节点挂了，谁来接管？"
sidebar_label: "三、主节点挂了谁来接管"
sidebar_position: 4
description: "从人工切主的局限出发，逐步引出故障检测、自动故障切换，并追踪 Redis 7.2.4 Sentinel 的真实执行路径。"
slug: /distributed-systems/03-redis-sentinel-failover
tags: [distributed-systems, redis, sentinel, failover, source-code]
---

上一篇，我们学会了让客户端等待副本确认某次写入。通过 Redis 的 `WAIT`，客户端能知道：至少有几个副本处理到了指定复制位置。

但现在假设主节点 A 真的坏了。我们仍然得回答一个问题：**谁决定 B 可以接管，客户端又怎么知道新的主节点是谁？**

这一篇只解决这一个问题：从人工故障切换出发，让自动切换的设计逐步长出来。多方投票在网络分区下为什么困难，我们留到下一篇深入研究。

> **源码范围**：Redis [7.2.4](https://github.com/redis/redis/tree/7.2.4)，主要是 [`src/sentinel.c`](https://github.com/redis/redis/blob/7.2.4/src/sentinel.c)。文中会区分“真实源码节选”“教学化简”和“概念流程”；源码入口按该固定版本核对。

## 一、先让人来切换：其实已经能够工作

继续使用文章访问计数器：

```text
客户端   ──写入──> A（主节点：101）
                     |
                     +──复制──> B（副本：101）
                     +──复制──> C（副本：100）
```

A 负责写入；B、C 持有从 A 复制过来的数据。为了让过程可观察，我们假设 B 的复制进度比 C 更新。

如果我们已经知道 A 永久不可用，最简单的恢复操作是：

1. 让 B 停止跟随 A，成为新的主节点。
2. 让 C 改为跟随 B。
3. 告诉客户端：后续写入请连接 B。

在 Redis 中，第一步所需的具体命令是 `REPLICAOF NO ONE`；在 Redis Sentinel 7.2.4 的内部实现中，仍可看到历史命令名 `SLAVEOF NO ONE`。两者表达的都是解除复制关系、提升为独立主节点的操作。

```text
故障前：Client -> A；B -> A；C -> A
故障后：Client -> B；C -> B；A 暂时不可用
```

这已经是一次完整的**人工故障切换**。如果故障很少、业务能够接受人工介入，这个方案并不荒谬。

可是当故障发生在凌晨、值班人员暂时无法处理时，恢复时间就不再由系统决定，而由人什么时候发现并操作决定。

所以新的需求不是“再做一份数据副本”，而是：**系统要能够自己发现 A 不可用，并执行我们原本会手动完成的步骤。**

## 二、谁来发现 A 不可用？

我们最先想到的方法，是增加一个独立的小程序，每隔一段时间向 A 发送 `PING`。

**概念伪代码**：

```python
while True:
    if A.ping() fails for long_enough:
        mark_A_as_unavailable()
```

注意这里必须有 `long_enough`。网络请求可能偶尔超时，单次 `PING` 没有回应，不足以证明 Redis 进程真的停止工作。

这种独立于数据节点、持续监视系统状态的程序，就是我们现在需要的最小新组件。Redis 给它起的名字是 **Sentinel（哨兵）**。

Sentinel 不是保存 `post:42:views` 的另一个数据库节点。它主要负责监视、协调故障切换，并向客户端提供当前主节点地址。

现在系统只是多了一个旁观者：

```text
Sentinel S1 --PING--> A
                    B、C 仍从 A 复制数据
```

### 对照真实源码：监控从哪里开始？

固定版本的 `src/sentinel.c` 中，周期入口是 [`sentinelTimer()`](https://github.com/redis/redis/blob/7.2.4/src/sentinel.c#L5064-L5077)。它通过 `sentinelHandleDictOfRedisInstances()` 遍历已知实例，进入 [`sentinelHandleRedisInstance()`](https://github.com/redis/redis/blob/7.2.4/src/sentinel.c#L4979-L5009)。

读这个函数，不要先关注所有分支，只看我们此刻需要的前半段：

- `sentinelReconnectInstance(ri)`：必要时尝试恢复与目标实例的连接。
- `sentinelSendPeriodicCommands(ri)`：向 Redis 实例周期发送监控命令，包括 `PING` 和获取实例信息的命令。
- `sentinelCheckSubjectivelyDown(ri)`：依据已收集到的心跳与连接状态，更新本 Sentinel 对实例可用性的判断。

发送 `PING` 的逻辑在 [`sentinelSendPeriodicCommands()`](https://github.com/redis/redis/blob/7.2.4/src/sentinel.c#L2875-L2935) 附近。回复由 [`sentinelPingReplyCallback()`](https://github.com/redis/redis/blob/7.2.4/src/sentinel.c#L2584-L2603) 处理；可接受的回复会更新 `last_avail_time` 并清除当前未完成 `PING` 标记。

为什么不是每次直接判断 `ping() == False`？因为 Redis Sentinel 用的是持续收集的时间信息，而不是一次同步调用的布尔结果。

现在找到 [`sentinelCheckSubjectivelyDown()`](https://github.com/redis/redis/blob/7.2.4/src/sentinel.c#L4202-L4266)。它考察未得到有效回复的持续时间，及其他少量实例状态条件。满足下线条件时，会设置 `SRI_S_DOWN` 标记，并产生 `+sdown` 事件。

这一状态叫 **SDOWN（主观下线）**：**从 S1 自己的观察角度来看，A 已经不可用。** 它并不是全网共同认定的事实。

这里我们第一次看到 Sentinel 的工作方式：**周期执行监控动作，把观察结果记录为状态；后续切换流程再依据状态推进。**

## 三、一个 Sentinel 说 A 坏了，够不够？

假设这次并不是 A 停机，而是 S1 到 A 的网络恰好断了。

```text
S1 --X--> A       S1：我访问不到 A
S2 -----> A       S2：A 正常
S3 -----> A       S3：A 正常
```

如果只根据 S1 的判断就立刻提升 B，那么原来的 A 可能仍然向其他客户端处理写入。

我们想解决自动切换，却可能创造两个都在接受写入的主节点。

于是，只由一名观察者决定不够可靠。我们增加另外两个 Sentinel，让它们分别观察 A 的状态。

最小的新规则是：**S1 不仅要自己认为 A 下线，还要收集到足够数量的其他 Sentinel 对 A 不可达的判断。**

例如，监视 `mymaster` 时设置 `quorum = 2`：

```text
S1：A 不可达
S2：A 可达
S3：A 可达

只有 1 个报告不可达 -> S1 保持 SDOWN，不进入 ODOWN
```

如果 S2 也判断 A 不可达：

```text
S1：A 不可达
S2：A 不可达
S3：A 可达

达到 quorum=2 -> S1 可将 A 标记为 ODOWN
```

**ODOWN（客观下线）** 不是证明 A 在物理上已经关机，而是足够多观察者在有限时间范围内给出了故障报告。Redis 官方明确称这种判断为较弱的 quorum 判断：网络消息可能延迟，不能据此断言所有 Sentinel 在完全相同的时刻都同意。

### 对照真实源码：S1 如何收集他人的观察？

`sentinelHandleRedisInstance()` 对主节点还会调用 [`sentinelAskMasterStateToOtherSentinels()`](https://github.com/redis/redis/blob/7.2.4/src/sentinel.c#L4343-L4390)，向其他 Sentinel 发送 `SENTINEL IS-MASTER-DOWN-BY-ADDR` 请求。

回复进入 [`sentinelReceiveIsMasterDownReply()`](https://github.com/redis/redis/blob/7.2.4/src/sentinel.c#L4305-L4341)，对端的下线判断被记录在对应实例状态里（`SRI_MASTER_DOWN`）。过期的报告会被清理，避免永远使用旧观察。

随后 [`sentinelCheckObjectivelyDown()`](https://github.com/redis/redis/blob/7.2.4/src/sentinel.c#L4267-L4304) 汇总本地判断和其他 Sentinel 的报告。

**真实源码节选（Redis 7.2.4）：**

```c
if (quorum >= master->quorum) odown = 1;
```

`master->quorum` 来自该主节点的 Sentinel 监控配置；`quorum` 是当前统计到的“认为 A 已下线”的 Sentinel 数量。

只有本 Sentinel 已经处于 SDOWN，才会进入这一统计路径。满足条件时，Sentinel 设置 `SRI_O_DOWN` 并记录 `+odown` 事件。

至此，故障发现从“我 ping 不通”前进到了“根据多名观察者的报告，我有理由触发后续动作”。

但触发条件满足，不代表任何一个 Sentinel 都能擅自改写集群配置。下一步，我们先把切换工作交给一个获得授权的协调者。

## 四、决定切换之后，谁来实际操作？

如果 S1、S2、S3 都发现了故障，它们可能同时尝试切换。我们最不希望看到的是 S1 提升 B，S2 却提升 C。

因此，**一轮故障切换应由一个获得足够授权的 Sentinel 来协调**。注意这个 Sentinel 是“故障切换协调者”，不是最终承担读写的 Redis 主节点。

真实 Sentinel 的这个过程包含选举和配置版本。此处只使用足够理解下一步的结论：正常自动故障切换需要满足故障判断条件，并获得足够数量 Sentinel 的授权；授权阈值至少涉及已知 Sentinel 的多数，也不能低于配置的 quorum。具体为什么需要多数、配置纪元如何限制旧决定，下一篇再推导。

我们可以先把它当作一个已经满足的前置条件：

```text
故障条件满足 + 某个 Sentinel 获得切换授权
                          |
                          v
                现在才允许选择 B 或 C
```

### 对照真实源码：满足条件后如何开始？

[`sentinelStartFailoverIfNeeded()`](https://github.com/redis/redis/blob/7.2.4/src/sentinel.c#L4596-L4633) 首先检查主节点是否处于 ODOWN、是否已有故障切换、最近是否已经尝试切换。

**真实源码节选（Redis 7.2.4）：**

```c
if (!(master->flags & SRI_O_DOWN)) return 0;
```

满足启动条件，才进入 `sentinelStartFailover()`，设置“正在切换”的标记并进入等待启动状态。随后 [`sentinelFailoverWaitStart()`](https://github.com/redis/redis/blob/7.2.4/src/sentinel.c#L4733-L4762) 检查是否赢得该轮切换的协调资格；没有获得授权的候选者不能正常推进提主步骤。

这里很重要：**`sentinelStartFailover()` 是开始一次切换尝试，不是已经完成选举，也不是已经有了新主节点。**

## 五、如果 B 和 C 都活着，应该选谁？

现在恢复到贯穿案例：

```text
A（原主节点）  不可用
B（副本）      post:42:views = 101，复制位置 2301
C（副本）      post:42:views = 100，复制位置 2299
```

如果 B 和 C 都健康、优先级相同，直觉上我们希望选 B，因为它处理过更新的复制流。

但是不能只看位置：如果 B 已失联很久，甚至没法接收提主命令，选它也无法恢复服务。所以选择要先排除明显不可用或不适合提主的副本，再比较谁更合适。

这时的最小规则可以概括为：**先检查候选者是否合格，再按预先约定的优先级和复制进度排序。**

### 对照真实源码：如何选择？

在 [`sentinelSelectSlave()`](https://github.com/redis/redis/blob/7.2.4/src/sentinel.c#L4690-L4730) 中，Redis 会过滤已下线、连接中断、心跳或 INFO 太旧、与原主节点断联太久、`slave_priority == 0` 的副本。

再由 [`compareSlavesForPromotion()`](https://github.com/redis/redis/blob/7.2.4/src/sentinel.c#L4665-L4689) 对候选者排序：

- 先比较 `slave_priority`，数值较小者优先；`0` 表示不允许提主。
- 优先级相同时，复制 offset 更大者优先。
- 如果仍然并列，使用 run ID 作为稳定的后续比较依据。

这意味着：若 B 和 C 都合格且优先级相同，B 会因复制位置 `2301 > 2299` 而优先入选；但如果优先级不同，**并不保证数据最新者一定胜出**。

真实 Redis 的 offset 是复制流中的字节位置，本例的数字仅为说明候选副本相对进度，不是一次真实运行的 Redis offset 输出。

## 六、选择 B 后，怎样真正完成切换？

“选中 B”和“B 已经成为主节点”仍不是一回事。

如果系统只是修改内存中的 `current_master = B`，B 可能仍然处于只复制 A 的角色；客户端被引导过去写入时可能失败。

所以协调者还必须把新的角色和复制关系真正应用到 Redis 实例上。

真实 Sentinel 使用 [`sentinelFailoverStateMachine()`](https://github.com/redis/redis/blob/7.2.4/src/sentinel.c#L4936-L4956) 逐步推进。这里我们只追正常路径，不一次性研究所有超时、重试和兼容性分支：

1. 已获切换协调资格后，`sentinelFailoverSelectSlave()` 调用 `sentinelSelectSlave()`，得到 B。
2. `sentinelFailoverSendSlaveOfNoOne()` 通过 `sentinelSendSlaveOf(B, NULL)` 排队发送解除复制关系的命令。
3. Sentinel 并不只相信“命令已排队发送”，还会通过后续 `INFO` 观察 B 的 `role` 是否真正变成主节点。
4. 确认 B 已提升后，`sentinelFailoverReconfNextSlave()` 让其他副本改为跟随 B。
5. `sentinelFailoverSwitchToPromotedSlave()` 更新 Sentinel 记录的主节点地址，并产生 `+switch-master` 事件。

第 2 步的调用者可见于 [`sentinelFailoverSendSlaveOfNoOne()`](https://github.com/redis/redis/blob/7.2.4/src/sentinel.c#L4780-L4811)。第 3 步“观察 `INFO` 里的角色变化”对应 [`sentinelRefreshInstanceInfo()` 中处理角色变化的分支](https://github.com/redis/redis/blob/7.2.4/src/sentinel.c#L2475-L2501)，不是同步等待一条网络命令回复。

第 4 步可见 [`sentinelFailoverReconfNextSlave()`](https://github.com/redis/redis/blob/7.2.4/src/sentinel.c#L4871-L4921)；第 5 步可见 [`sentinelFailoverSwitchToPromotedSlave()`](https://github.com/redis/redis/blob/7.2.4/src/sentinel.c#L4923-L4934)。

这样设计的原因很朴素：**Sentinel 向 B 发出提主指令只是动作发起，后续观察 B 的实际角色，才能确认动作发生了。**

故障切换期间并非所有节点在同一瞬间完成重配置；旧主节点恢复之后，也可能需要被重新配置为新主节点的副本。

## 七、新主节点已经是 B，客户端怎么发现？

原来应用程序可能写死了 A 的地址。如果只是 Sentinel 把 B 提升成功，业务仍在不断尝试连接 A，服务也没有真正恢复。

我们现在还缺少最后一步：**客户端在连接失败后，重新发现当前主节点。**

Sentinel 提供了一个主节点地址查询接口：

```redis
SENTINEL get-master-addr-by-name mymaster
```

支持 Sentinel 的客户端通常会从一组 Sentinel 地址中找到可用的服务，询问 `mymaster` 的当前主节点地址，并验证目标实例确实是主节点。在连接失败后，它需要重新发现地址，而不是永远复用旧的 A。

这套流程在 [Redis Sentinel client specification](https://redis.io/docs/latest/develop/reference/sentinel-clients/) 中有明确说明。

现在可以把本篇逐步建立的链路汇总：

```text
S1、S2、S3 周期观察 A
          |
          v
S1 判断 A 主观下线（SDOWN）
          |
          v
收集其他 Sentinel 的判断
          |
          v
达到故障判断阈值（ODOWN）
          |
          v
某个 Sentinel 获得切换协调授权
          |
          v
过滤并比较 B、C -> 选择 B
          |
          v
B 解除旧复制关系，成为主节点
          |
          v
C 改为跟随 B；Sentinel 更新主节点地址
          |
          v
客户端重新发现 B -> 继续访问
```

这张图没有在文章开头出现，因为它的每一步现在都有了一个具体问题作为理由。

## 八、一个可以直接运行的最小实验

接下来用纯 Python 模拟这个演化过程。它故意只验证两个观察：

1. 只有 S1 认为 A 失联时，不能直接切换。
2. 故障判断条件已满足、且**假设**某个协调者已获得授权后，选择合格副本，并更新主节点指向。

**教学化简（不是 Redis Sentinel 源码）：** 此实验没有真实网络、真实 PING、Sentinel 选举协议、配置纪元、INFO 验证或自动客户端重连。`coordinator_authorized = True` 是人为设置的假设，绝不等同于实现了选举。

将下面代码保存为 `failover_lab.py`，执行 `python3 failover_lab.py`：

```python
"""A deterministic teaching model of external failure detection and promotion.
Not Redis Sentinel and not a leader election implementation.
"""
from dataclasses import dataclass


@dataclass
class Replica:
    name: str
    value: int
    offset: int
    priority: int = 100
    healthy: bool = True
    role: str = "replica"
    follows: str = "A"


def detection(replies: dict[str, bool], quorum: int = 2) -> tuple[bool, bool]:
    """True reply means the observer can reach A; S1 is our local observer."""
    sdown = not replies["S1"]
    odown = sdown and sum(not reachable for reachable in replies.values()) >= quorum
    return sdown, odown


def choose_replica(replicas: list[Replica]) -> Replica | None:
    candidates = [r for r in replicas if r.healthy and r.priority > 0]
    if not candidates:
        return None
    return min(candidates, key=lambda r: (r.priority, -r.offset, r.name))


def main() -> None:
    b = Replica("B", value=101, offset=2301)
    c = Replica("C", value=100, offset=2299)
    replicas = [b, c]
    current_master = "A"

    # S1 is cut off from A, but S2 and S3 still see A.
    sdown, odown = detection({"S1": False, "S2": True, "S3": True})
    assert sdown and not odown
    assert current_master == "A"
    print(f"T1: SDOWN={sdown}, ODOWN={odown}; no failover")

    # Simulate a real outage from all three observers' points of view.
    sdown, odown = detection({"S1": False, "S2": False, "S3": False})
    assert sdown and odown
    print(f"T2: SDOWN={sdown}, ODOWN={odown}")

    # Sentinel leader election is deliberately NOT implemented here.
    coordinator_authorized = True
    if odown and coordinator_authorized:
        chosen = choose_replica(replicas)
        assert chosen is not None
        chosen.role = "master"
        chosen.follows = ""
        current_master = chosen.name
        for replica in replicas:
            if replica is not chosen:
                replica.follows = chosen.name
        print(f"T3: elected coordinator promotes {chosen.name} (offset={chosen.offset})")

    assert current_master == "B"
    assert c.follows == "B"
    print(f"T4: resolve mymaster -> {current_master}; GET post:42:views -> {b.value}")


if __name__ == "__main__":
    main()
```

本实验已经在 Python 3.13.5 下执行，实际输出为：

```text
T1: SDOWN=True, ODOWN=False; no failover
T2: SDOWN=True, ODOWN=True
T3: elected coordinator promotes B (offset=2301)
T4: resolve mymaster -> B; GET post:42:views -> 101
```

这不是宣称“Redis Sentinel 集成测试通过”。它只证明：在这个明确约定的教学模型中，单个观察者误报不会立刻切主，而已获授权的切换协调者能够选择一个合格副本，模拟完成后续地址变化。

如果想进一步验证真实 Sentinel，应另行部署 Redis 数据节点与 Sentinel 进程，并观察 `+sdown`、`+odown`、`+elected-leader`、`+selected-slave`、`+switch-master` 等事件。本文没有运行该真实部署，因而不提供虚构的 Sentinel 运行日志。

## 九、Failover 解决了什么，没有解决什么？

从上一章到现在，我们获得的能力发生了明确变化：

- 之前：已经有 B、C，但需要人判断 A 是否可用，再手动切换。
- 现在：Sentinel 能持续观察节点，协作判断故障，由获得授权的协调者选择和提升一个副本，并向客户端提供新主节点地址。

但我们还不能得出“现在数据绝对安全”的结论。

第一，**自动切换不等于零中断**。故障检测、确认、选举、提主和客户端重连都需要时间。

第二，**自动切换不等于零数据丢失**。如果已确认的更新没有进入最终被提升的副本，或者复制与持久化保证不足，仍可能丢失写入。上一章的 `WAIT` 也不会直接改变 Sentinel 的全部故障处理语义。

第三，**ODOWN 是可用的故障判断，不是对物理世界的证明**。网络分区时，A 可能仍在某些客户端眼中正常，Sentinel 却已经开始切换。真正复杂的问题不是“如何发出提主命令”，而是“怎么避免冲突的主节点同时接受写入，以及旧节点回来后如何处理它的写入”。

于是我们有了下一篇非常自然的问题：

**如果原来的 A 并没有坏，只是它和部分 Sentinel 之间断网，而 B 又被提升了，那么系统里会不会同时出现两个主节点？**

下一篇：《两边都认为自己是主：网络分区下，谁的决定算数？》

---

## 固定版本与核验边界

- [Redis 7.2.4 源码标签](https://github.com/redis/redis/tree/7.2.4)
- [`src/sentinel.c`：监控循环、SDOWN / ODOWN、选主及故障切换状态机](https://github.com/redis/redis/blob/7.2.4/src/sentinel.c)
- [Redis Sentinel 官方文档](https://redis.io/docs/latest/operate/oss_and_stack/management/sentinel/)
- [Redis Sentinel 客户端协议](https://redis.io/docs/latest/develop/reference/sentinel-clients/)

**核验说明：** 函数名、入口及正常路径已与 Redis 7.2.4 源码对照。本文仅将少量源码行标为“真实源码节选”，其余 Python 实验和示意步骤均明确属于教学化简。Python 实验已执行；Redis Sentinel 真实多进程故障切换未执行。固定版本中的历史 `slave` 命名按源码保留，正文中的“副本”与之对应。
