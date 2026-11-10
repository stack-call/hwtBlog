---
title: "分布式系统 08：一笔操作涉及两个分片，为什么不能一起成功？"
sidebar_label: "八、一笔操作跨两个分片"
sidebar_position: 9
description: "从文章访问量与全站统计同时更新的失败开始，沿 Redis 7.2.4 的 CROSSSLOT 检查源码推导单 Slot 原子性边界，再用 MySQL 8.4.0 XA 的 PREPARE/COMMIT 源码理解跨分片协调和恢复成本。"
slug: /distributed-systems/08-cross-shard-transactions
tags: [distributed-systems, redis, mysql, sharding, transactions, source-code]
---

# 一笔操作涉及两个分片，为什么不能一起成功？

上一篇，我们解决了扩容时一个 Key 如何从旧节点迁移到新节点的问题：Redis Cluster 用 Hash Slot 确定数据归属，用 `ASK` 处理迁移中的临时访问，用 `MOVED` 通知稳定的路由变化。

现在，单个 Key 已经能够找到自己的节点。似乎只要继续增加机器，系统就能一直变大。

但我们的网站出现了一个新需求：**每次访问文章 42，不仅要增加文章自己的访问次数，还要增加全站访问次数，而且两次更新必须一起成功。**

```text
访问文章 42 前：
  post:{42}:views     = 100
  site:{global}:views = 1000

完成一次访问后，希望：
  post:{42}:views     = 101
  site:{global}:views = 1001
```

这里的要求比之前严格了一点。我们希望在一次业务操作结束时，两份计数要么都增加，要么都不增加。否则展示文章页面的访问量和全站统计就可能发生无法解释的差异。

这篇只研究这个新问题：**当一次操作涉及多个分片时，我们还能不能像在单机上一样把它当成一次完整操作？**

## 一、先用最简单的办法：连续写两次

还没有分片时，我们可以把两个值放在同一进程的内存里。为了避免其他请求在两次写入中间插入，还可以加一把锁：

```python
# 教学化简：单进程，同一把锁保护两个键
from threading import Lock

lock = Lock()
state = {"post:42:views": 100, "site:global:views": 1000}

def record_visit():
    with lock:
        state["post:42:views"] += 1
        state["site:global:views"] += 1
```

在这个单进程模型中，如果没有异常、进程崩溃和外部副作用，其他线程看不到两个更新的中间状态。但这把锁**并不能保证掉电后的持久化事务**，它只是让我们先建立“一次操作、两个状态”的直觉。

现在我们把这两个 Key 按照第六篇的方法放进 Redis Cluster。

真实 Redis 7.2.4 的 Slot 规则是 `CRC16(key 或哈希标签) & 0x3FFF`。对下面两个 Key 实际计算后得到：

```text
post:{42}:views      -> Slot 8000
site:{global}:views  -> Slot 10408
```

它们属于不同 Slot，可能由不同主节点处理。即便两个 Slot **碰巧落在同一个物理节点**，Redis Cluster 的普通多 Key 操作仍然要求这些 Key 属于同一个 Slot。

最直觉的实现是分别发送两条命令：

```text
客户端 -> 负责 Slot 8000 的节点：INCR post:{42}:views
客户端 -> 负责 Slot 10408 的节点：INCR site:{global}:views
```

正常情况下，两次都成功，结果正确。可是如果第一条已经成功，第二条还没发送，客户端进程就崩溃了呢？

```text
T0：post=100，site=1000
T1：第一个节点执行 INCR，post=101
T2：客户端崩溃；第二条 INCR 尚未执行
T3：post=101，site=1000
```

**单个分片上的写入成功，并不能自动组成跨分片的原子操作。**

此时问题不在于客户端不会路由。它已经准确地找到了两个节点。问题在于：两个独立节点并不知道“这两次修改必须属于同一次业务操作”。

## 二、那直接使用 Redis 事务不行吗？

在单节点 Redis 中，我们可能想到 `MULTI/EXEC`：

```redis
MULTI
INCR post:42:views
INCR site:global:views
EXEC
```

Redis 会将一个事务中排队的命令连续执行，不让其他客户端的命令在中间穿插。这个能力在单节点上很有用。但还要留意：**Redis 事务没有关系型数据库式的执行期回滚**。即使某条命令在 `EXEC` 时因类型错误失败，其他已经执行的命令也不会因此全部撤销。

现在试着在 Redis Cluster 中把分布在不同 Slot 的两条 `INCR` 排进同一个事务：

```redis
MULTI
INCR post:{42}:views
INCR site:{global}:views
EXEC
```

正常的 Redis Cluster 会拒绝这个跨 Slot 的事务，而不是替我们去两个节点执行并协调提交。其关键错误是：

```text
CROSSSLOT Keys in request don't hash to the same slot
```

这和上一篇的 `MOVED`、`ASK` 完全不同。

- `MOVED` / `ASK`：**Key 应该去哪个节点？**
- `CROSSSLOT`：**这个请求要求一起处理的 Key 不属于同一个 Slot，因此不能按一个单 Slot 操作执行。**

重试相同的跨 Slot 请求，或者让客户端自动跟随 `MOVED`，都不会使它自动获得跨分片事务能力。

我们去源码验证这不是客户端库的偶然限制，而是 Redis Cluster 的明确设计边界。

## 三、真实 Redis 源码：为什么在命令执行前就拒绝？

**固定版本：Redis 7.2.4**，仓库：[`redis/redis@7.2.4`](https://github.com/redis/redis/tree/7.2.4)。

先看 [`src/server.c` 的 `processCommand()`](https://github.com/redis/redis/blob/7.2.4/src/server.c#L3634-L3655)。客户端发送命令后，在真正执行命令之前，Redis 在开启 Cluster 的情况下会调用：

**真实源码节选（Redis 7.2.4）：**

```c
clusterNode *n = getNodeByQuery(c,c->cmd,c->argv,c->argc,
                                &c->slot,&error_code);
```

这里的输入是：当前客户端 `c`、要执行的命令 `c->cmd`、命令参数 `argv/argc`。返回结果表示这条请求应当由哪个节点处理；`error_code` 用于报告不能正常执行的原因。

为什么在执行命令前询问它？因为如果命令涉及的 Key 属于别的 Slot，本节点就不能先把其中一半修改了，再发现另一半应该交给别的节点。

继续进入 [`src/cluster.c` 的 `getNodeByQuery()`](https://github.com/redis/redis/blob/7.2.4/src/cluster.c#L6692-L6793)。这里的核心不是网络转发，而是**检查一次请求中涉及的 Key**。

函数通过 `getKeysFromCommand()` 找出命令使用了哪些 Key，再逐个用 `keyHashSlot()` 计算 Slot。

**真实源码节选（Redis 7.2.4，省略无关路径）：**

```c
if (slot != thisslot) {
    getKeysFreeResult(&result);
    if (error_code)
        *error_code = CLUSTER_REDIR_CROSS_SLOT;
    return NULL;
}
```

这里的 `slot` 是第一个 Key 的 Slot；`thisslot` 是当前遍历到的另一个 Key 的 Slot。

如果不同，就直接返回错误。没有“自动找到两个节点并协调”的后续动作。

注意 `getNodeByQuery()` 还对 `EXEC` 作了特殊处理：普通命令时，它检查本次命令；对于 `EXEC`，它会遍历当前客户端已经排队的事务命令。这就是为什么把两个不同 Slot 的 `INCR` 放进同一个 `MULTI/EXEC`，也不能绕过限制。

最后，回到 [`clusterRedirectClient()`](https://github.com/redis/redis/blob/7.2.4/src/cluster.c#L6895-L6903)，可以看到对外错误回复的真实代码：

```c
if (error_code == CLUSTER_REDIR_CROSS_SLOT) {
    addReplyError(c,"-CROSSSLOT Keys in request don't hash to the same slot");
}
```

到这里，调用链已经形成：

```text
客户端提交一个涉及多个 Key 的命令 / EXEC
            |
            v
server.c: processCommand()
            |
            v
cluster.c: getNodeByQuery()
            |
            +-- getKeysFromCommand() 找出涉及的 Key
            |
            +-- keyHashSlot() 逐个计算 Slot
            |
            +-- 发现 Slot 不同
                    |
                    v
             CLUSTER_REDIR_CROSS_SLOT
                    |
                    v
             clusterRedirectClient()
                    |
                    v
                  CROSSSLOT
```

这些函数解决的是**执行前的路由和约束检查**；它们不是分布式事务协调器。Redis 选择拒绝复杂的跨 Slot 原子操作，换取每个 Slot 更简单的执行模型和横向扩展能力。

## 四、第一次修正：能不能让相关 Key 放进同一个 Slot？

回看业务，如果我们暂时只要求**同一篇文章内部**的多个状态一起更新，例如：

```text
文章 42 的访问量
文章 42 的最后访问者
```

那么可以把它们命名成：

```text
post:{42}:views
post:{42}:last-viewer
```

这两个 Key 的 Slot 都是 `8000`。

我们并没有增加协调协议，只调整了 Key 的布局，使需要一起操作的数据重新落到一个分片。

可以用 `CLUSTER KEYSLOT` 检查：

```redis
CLUSTER KEYSLOT post:{42}:views
CLUSTER KEYSLOT post:{42}:last-viewer
```

对应两条命令返回的 Slot 应相同。对于这个局部范围，单 Slot 的 `MULTI/EXEC` 或符合限制的 Lua 脚本就有机会完成连续执行、避免其他客户端命令在中间插入。

但请不要把“没有其他客户端插入”误解成“所有运行时错误都能回滚”或者“机器宕机时绝对不会丢失”；那是其他保证。

**我们已经解决了同一篇文章内部的多 Key 操作，却还没有解决全站计数问题。**

因为 `site:{global}:views` 必须汇总全部文章。假设我们强行把所有文章的计数也放进 `{global}` 这个标签：

```text
post:{global}:42:views
post:{global}:43:views
post:{global}:44:views
site:{global}:views
```

它们当然都能落进同一 Slot。但这样一来，所有文章的访问计数都会集中到**同一个 Slot、同一个负责该 Slot 的主节点**，此前通过分片分散负载的效果就丧失了相当一部分。

所以“把所有 Key 放到一起”不是一个没有代价的万能答案。

现在我们面临一个真正的选择：

**要么改变业务要求，让全站统计允许稍后汇总；要么保留强耦合要求，并引入跨节点协调。**

## 五、如果全站统计允许稍后更新，最小方案其实更简单

很多访问量统计并不要求每次页面打开，都立即同步更新全站总数。

如果我们接受这样的语义：

> 文章计数应尽快更新；全站数字可以略有延迟，之后重新计算或补偿。

那就不必强迫 Redis 执行跨 Slot 事务。

我们可以只将文章访问记录作为**可信的主事实**，再由一个独立的统计过程汇总全站数字。这里必须注意，若需要在进程崩溃后不漏记、可重试，单靠“更新 Redis 后顺手发一条事件”仍可能在两步之间失败；需要进一步设计持久化事件、去重或可重算机制。

本章不展开消息系统，因为我们还没有遇到必须研究 Kafka 的具体失败场景；它将在后面自然出现。

这个方案的收益是简单、吞吐更容易扩展。代价是全站数据在一段时间内可能不是最新值。

但是，假设产品经理坚持：**有些业务动作不允许只成功一半。**

这时，我们不能用最终一致的汇总方案来代替一个明确要求原子提交的业务事务。于是才需要考虑新的协调机制。

## 六、第二次修正：如何让两个分片共同决定提交？

我们先不要看 MySQL，仍然回到 `post=100 / site=1000`。

前面的失败发生在：A 已经修改，B 还没来得及修改，协调者就崩溃了。

能不能先不让任何一边真正提交，而是先询问两个分片：“你们是否**准备好**完成这次更新？”

假设我们把操作编号为 `visit-9002`，规定每个分片可以先把本地修改保存为一个尚未最终提交的状态：

```text
准备阶段：
  A：visit-9002 已准备好 +1，但还没有正式提交
  B：visit-9002 已准备好 +1，但还没有正式提交

决策阶段：
  协调者确定 COMMIT，再通知 A 和 B 提交
```

如果某个分片在准备阶段就失败，协调者可以让已经准备好的其他分片放弃这次操作。

这就是**两阶段提交（Two-Phase Commit，2PC）** 的基本直觉：

1. **Prepare**：参与者各自确认自己能把事务留在可提交状态。
2. **Commit / Rollback**：协调者根据准备结果形成全局决策，再让参与者执行。

但 2PC 并不是“先问一句能不能提交”这么简单。

如果协调者在两个分片都准备成功后崩溃了，分片已经保留了未决事务，却不知道最后应该提交还是回滚。它们可能需要等待恢复后的协调者给出决定，并在此期间保留资源和锁。

**所以 2PC 把“部分提交”问题转化为“必须可靠记住并恢复全局决策”的问题。**

新的能力伴随着新的复杂度，并不是免费的。

## 七、真实 MySQL 源码：PREPARE 究竟留下了什么状态？

Redis Cluster 并不提供把普通跨 Slot 操作直接升级成 XA 事务的通用接口。为了观察真正的事务参与者如何支持两阶段操作，我们换一个更适合的源码对象：**MySQL 8.4.0 的外部 XA 事务**。

这里需要特别强调：下面是**另一种数据存储与事务设计方案**。它不是“调用 MySQL XA 就能给 Redis Cluster 增加跨 Slot 原子性”。

假设文章计数和全站计数存放在两个**独立 MySQL 实例**中，由应用里的事务协调者管理两条连接。

我们希望做的一次操作是：

```text
MySQL 实例 A：UPDATE post_views SET views=views+1 WHERE post_id=42
MySQL 实例 B：UPDATE site_stats SET views=views+1 WHERE site_id=1
```

两个实例分别执行一个 XA 分支，使用同一个全局事务标识 `gtrid` 和不同分支标识 `bqual`。

以下为**教学 SQL 顺序**，假设两张表均为 InnoDB，且行已经存在；协调者负责生成唯一事务 ID，记录状态并处理异常：

```sql
-- 连接 A：文章分片
XA START 'visit-9002', 'post';
UPDATE post_views SET views = views + 1 WHERE post_id = 42;
XA END 'visit-9002', 'post';
XA PREPARE 'visit-9002', 'post';

-- 连接 B：全站统计分片
XA START 'visit-9002', 'site';
UPDATE site_stats SET views = views + 1 WHERE site_id = 1;
XA END 'visit-9002', 'site';
XA PREPARE 'visit-9002', 'site';

-- 两边都准备成功，并且协调者可靠记录 COMMIT 决策后
XA COMMIT 'visit-9002', 'post';  -- 在连接 A 上
XA COMMIT 'visit-9002', 'site';  -- 在连接 B 上
```

如果准备阶段失败，协调者应向已经准备成功的分支发送 `XA ROLLBACK`，不能盲目继续提交。

为什么需要单独的 `XA PREPARE`？直接把两个普通 `COMMIT` 串起来不行吗？

因为如果第一个 `COMMIT` 已经不可逆地完成，第二个分片却拒绝提交，那么协调者很难再把第一边恢复成原来的事务状态。

`PREPARE` 的用途就是让参与者在最终提交前暴露“我是否已进入可提交的中间状态”。

### 7.1 固定源码入口

仓库：[`mysql/mysql-server@mysql-8.4.0`](https://github.com/mysql/mysql-server/tree/mysql-8.4.0)。

打开 [`sql/xa/sql_xa_prepare.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/xa/sql_xa_prepare.cc)，找到 `Sql_cmd_xa_prepare::execute()`。

**真实源码节选（MySQL 8.4.0）：**

```cpp
bool Sql_cmd_xa_prepare::execute(THD *thd) {
  bool st = trans_xa_prepare(thd);
```

`THD` 表示当前连接 / 执行上下文。SQL 命令对象将处理交给 `trans_xa_prepare()`。这是 `XA PREPARE` 的执行入口，不是全局协调者。

接着在 `trans_xa_prepare()` 中，MySQL 先确认该事务处于允许准备的状态，并确认事务标识匹配。

**真实源码节选（MySQL 8.4.0）：**

```cpp
if (!xid_state->has_state(XID_STATE::XA_IDLE)) {
    my_error(ER_XAER_RMFAIL, MYF(0), xid_state->state_name());
    return true;
}
```

为什么要检查状态？因为准备一个还没正常结束、或者已经进入其他阶段的事务，会打乱事务的合法状态流转。

当底层准备处理成功后，代码会执行：

```cpp
xid_state->set_state(XID_STATE::XA_PREPARED);
```

这行是真实源码。它的上游是 `process_xa_prepare(thd)`，后者会协调 MySQL 服务器内部相关存储引擎 / 事务日志的准备工作。特别注意：**外部 XA 的两个独立 MySQL 实例并不会因为这一行代码自动互相通信。**

本地 `PREPARED` 状态对外的意义是：这个实例已经准备好了，下一步等待外部协调者发出提交或回滚决定。

MySQL 8.4 官方文档还说明，`XA RECOVER` 可以列出仍处于 `PREPARED` 状态的 XA 事务，这为协调者在故障恢复后寻找未决分支提供了入口。

### 7.2 COMMIT 是怎样接续 PREPARE 的？

打开 [`sql/xa/sql_xa_commit.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/xa/sql_xa_commit.cc)，找到 `Sql_cmd_xa_commit::trans_xa_commit()`。

其中会根据事务是否附着在当前会话中，进入不同处理路径。对于附着在当前会话上的正常两阶段提交分支，`process_attached_xa_commit()` 需要看到相应准备状态：

**真实源码节选（MySQL 8.4.0）：**

```cpp
} else if (xid_state->has_state(XID_STATE::XA_PREPARED) &&
           m_xa_opt == XA_NONE) {
```

接下来才继续执行真正的提交处理。MySQL 8.4 默认支持在 `XA PREPARE` 后将事务与原连接分离，因此在其他连接继续提交时还可能进入 `process_detached_xa_commit()` 路径。为了看懂本章的设计，我们先只需要掌握：**准备与最终提交是两个可区分的状态阶段。**

把职责整理起来，只有这么一条链：

```text
应用中的外部协调者
       |
       +-- MySQL A：XA START / 更新 / XA END / XA PREPARE
       |
       +-- MySQL B：XA START / 更新 / XA END / XA PREPARE
       |
       +-- 两个分支都成功准备？
              |
              +-- 否：回滚已准备分支
              |
              +-- 是：可靠记录 COMMIT 决策
                       |
                       +-- MySQL A：XA COMMIT
                       +-- MySQL B：XA COMMIT
```

**外部协调者是不可省略的角色。** MySQL 提供可被协调的事务分支，但不会因为两个实例都使用相同的 `gtrid` 就自动完成跨实例决策。

## 八、最重要的失败：协调者在一边提交后崩溃

我们现在终于能够回到最开始的反例。

```text
A：PREPARED
B：PREPARED
协调者：记录 COMMIT 决定
A：COMMITTED
协调者：崩溃
B：仍然 PREPARED
```

如果协调者重启以后**不知道之前做过什么决定**，它就可能错误地要求 B 回滚。那样，两个分片又不一致了。

因此，为了让 2PC 在协调者故障后仍然成立，至少需要：

- 协调者在发出提交指令前可靠记录全局决策；
- 故障恢复后查询参与者仍处于什么状态（例如 MySQL 的 `XA RECOVER`），重发必要的决定；
- 区分“提交请求没有得到回复”和“事务没有提交”，避免盲目重做业务操作；
- 处理长期 `PREPARED` 带来的锁、等待、人工干预与运维风险。

这也解释了为什么分布式事务通常比一次本地事务复杂得多。

它不是简单地“多调用两个函数”，而是将**失败后的决策恢复**也纳入系统设计。

## 九、最小实验：让两个分片在中间失败一次

为了验证上述几个场景，我们写一个不依赖 Redis、MySQL、Docker 的 Python 教学实验。

它包含三个可观察现象：

1. 两次普通写入中途失败，出现半完成状态；
2. 准备阶段失败，所有分支都能放弃未提交操作；
3. 两边准备完成后，协调者在第一次提交后崩溃；模拟恢复逻辑根据已记录的决定完成另一边。

运行文件：`examples/cross_shard_tx_lab.py`。

下面附上**完整可复制的实验源码**：

```python
"""Chapter 08: cross-shard write failure and a simplified 2PC coordinator.

Pure teaching model: not Redis nor MySQL, no persistence/network/locks.
"""
from binascii import crc_hqx


def redis_slot(key: str) -> int:
    start = key.find("{")
    if start >= 0:
        end = key.find("}", start + 1)
        if end > start + 1:
            key = key[start + 1:end]
    return crc_hqx(key.encode("utf-8"), 0) & 0x3FFF


class Participant:
    def __init__(self, name: str, initial: int):
        self.name = name
        self.value = initial
        self.prepared = {}  # xid -> delta
        self.committed = set()

    def direct_increment(self):
        self.value += 1

    def prepare(self, xid: str, delta: int, *, accepted: bool = True):
        if not accepted:
            raise RuntimeError(f"{self.name}: prepare rejected")
        if xid in self.committed:
            raise ValueError("cannot prepare a committed transaction")
        self.prepared[xid] = delta

    def commit(self, xid: str):
        if xid in self.committed:
            return
        if xid not in self.prepared:
            raise ValueError(f"unknown prepared transaction: {xid}")
        self.value += self.prepared.pop(xid)
        self.committed.add(xid)

    def rollback(self, xid: str):
        self.prepared.pop(xid, None)


def main():
    article = 'post:{42}:views'
    global_total = 'site:{global}:views'
    same_article = 'post:{42}:last-viewer'
    print(f"Slot: {article} -> {redis_slot(article)}")
    print(f"Slot: {global_total} -> {redis_slot(global_total)}")
    print(f"Slot: {same_article} -> {redis_slot(same_article)}")
    assert redis_slot(article) == redis_slot(same_article)
    assert redis_slot(article) != redis_slot(global_total)

    # Counterexample: a crash after the first independent update.
    post, site = Participant('post', 100), Participant('site', 1000)
    post.direct_increment()
    # Simulated failure: site.direct_increment() never runs.
    assert (post.value, site.value) == (101, 1000)
    print(f"Independent writes interrupted: post={post.value}, site={site.value}")

    # Prepare phase fails before either branch can commit.
    post, site = Participant('post', 100), Participant('site', 1000)
    xid = 'visit-9001'
    post.prepare(xid, +1)
    try:
        site.prepare(xid, +1, accepted=False)
    except RuntimeError:
        post.rollback(xid)
        site.rollback(xid)
    assert (post.value, site.value) == (100, 1000)
    print(f"Prepare failed -> abort: post={post.value}, site={site.value}")

    # Both branches prepared. Durable coordinator decision is *simulated*.
    post, site = Participant('post', 100), Participant('site', 1000)
    xid = 'visit-9002'
    coordinator_decision = {}  # This dict is NOT durable in real life.
    post.prepare(xid, +1)
    site.prepare(xid, +1)
    coordinator_decision[xid] = 'COMMIT'
    post.commit(xid)
    # Simulated coordinator crash before sending COMMIT to site.
    assert (post.value, site.value) == (101, 1000)
    assert xid in site.prepared
    print(f"Coordinator interrupted: post={post.value}, site={site.value}, site=PREPARED")

    # Recovery: read the already recorded decision and finish outstanding branch.
    if coordinator_decision[xid] == 'COMMIT':
        post.commit(xid)  # idempotent in this teaching model
        site.commit(xid)
    assert (post.value, site.value) == (101, 1001)
    print(f"Decision replayed: post={post.value}, site={site.value}")
    print('Model checks passed (not a real Redis/MySQL integration test).')


if __name__ == '__main__':
    main()
```

在本系列的执行环境中，运行 `python3 examples/cross_shard_tx_lab.py` 得到：

```text
Slot: post:{42}:views -> 8000
Slot: site:{global}:views -> 10408
Slot: post:{42}:last-viewer -> 8000
Independent writes interrupted: post=101, site=1000
Prepare failed -> abort: post=100, site=1000
Coordinator interrupted: post=101, site=1000, site=PREPARED
Decision replayed: post=101, site=1001
Model checks passed (not a real Redis/MySQL integration test).
```

这次验证了**教学模型的状态变迁和断言**。它没有运行 Redis Cluster 的真实 `MULTI/EXEC`，也没有启动两个 MySQL 实例、模拟真实网络故障、持久化协调者日志或数据库锁。模型里用一个 Python 字典记录协调决策，只是为了观察恢复逻辑，**不具备生产级崩溃持久化能力**。

真正做 MySQL XA 集成测试，需要启动两台支持 XA 的 MySQL 实例，分别打开连接，并对准备成功、连接中断、协调者进程退出和重启恢复逐项验证。本章不声称已经完成这些集成验证。

## 十、回看：为什么不是所有跨分片操作都应该上 2PC？

我们最开始的问题只是“文章访问次数 + 全站访问次数同时更新”。

沿着失败场景推导，实际上得到三种不同的答案：

| 业务真正需要什么 | 可以优先考虑什么 | 新的限制 |
| --- | --- | --- |
| 同一文章相关状态需要一次处理 | Redis Hash Tag，将 Key 放进同一 Slot | 相关数据集中在一个分片；不等于持久化事务回滚 |
| 全站统计允许暂时落后 | 文章计数作为主事实，异步聚合或可重算 | 需要处理延迟、漏事件、重复事件 |
| 两个独立事务资源必须共同决定提交 | 外部协调者 + 支持 XA 的事务参与者（例如 MySQL） | 协调日志、恢复流程、准备期占用资源、复杂故障处理 |

它们不是一个方案的三个等级，而是根据**业务对一致性与可用性的真实要求**作出的不同选择。

对纯粹的访问量展示而言，为每一次访问支付跨分片 2PC 的协调成本，往往不值得。但对于确实不能只成功一半的状态变更，就必须正视跨节点协调与恢复问题，不能用“已经执行两条命令”冒充原子性。

最后，我们还留下了一个问题：

在 MySQL 的 `XA PREPARE` 源码中，已经看到它会让存储引擎和事务日志进入准备流程。可是**数据库为什么需要这些日志？进程或机器崩溃以后，它又凭什么知道哪些事务已经提交、哪些还没有？**

下一篇我们会顺着这个未解决的问题，进入 MySQL 的持久化与恢复：**《一次事务写入成功后，数据库怎样在崩溃中找回它？——从 Redo 到 Binlog》**。

---

### 固定版本、源码与核对边界

**Redis：7.2.4**

- [`src/server.c`：`processCommand()`](https://github.com/redis/redis/blob/7.2.4/src/server.c#L3634-L3655) — 在命令执行前调用 `getNodeByQuery()`，收到重定向或错误则拒绝本次执行。
- [`src/cluster.c`：`keyHashSlot()`](https://github.com/redis/redis/blob/7.2.4/src/cluster.c#L1246-L1269) — CRC16 与 `{...}` Hash Tag。
- [`src/cluster.c`：`getNodeByQuery()`](https://github.com/redis/redis/blob/7.2.4/src/cluster.c#L6692-L6793) — 收集命令 Key、检查 Slot，包含 `EXEC` 事务队列处理。
- [`src/cluster.c`：`clusterRedirectClient()`](https://github.com/redis/redis/blob/7.2.4/src/cluster.c#L6895-L6903) — 返回 `CROSSSLOT`。
- [Redis Cluster Specification](https://redis.io/docs/latest/operate/oss_and_stack/reference/cluster-spec/)；[Redis Transactions](https://redis.io/docs/latest/develop/using-commands/transactions/)。

**MySQL：8.4.0**

- [`sql/xa/sql_xa_prepare.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/xa/sql_xa_prepare.cc) — `Sql_cmd_xa_prepare::execute()`、`trans_xa_prepare()` 和本地事务状态转换。
- [`sql/xa/sql_xa_commit.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/xa/sql_xa_commit.cc) — `trans_xa_commit()`、附着/分离事务的提交路径。
- [MySQL 8.4 XA SQL Statements](https://docs.oracle.com/cd/E17952_01/mysql-8.4-en/xa-statements.html)；[XA Transaction States](https://docs.oracle.com/cd/E17952_01/mysql-8.4-en/xa-states.html)；[XA Restrictions](https://docs.oracle.com/cd/E17952_01/mysql-8.4-en/xa-restrictions.html)。

> 源码节选与所列固定版本的对应文件已核对；正文 SQL 是跨两台 MySQL 实例的教学示意，不是已经运行过的集成测试。实验输出只来自本文 Python 脚本。
