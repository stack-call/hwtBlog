---
title: "分布式系统 02：客户端收到 OK，到底意味着什么？"
sidebar_label: "二、客户端收到 OK 意味着什么"
sidebar_position: 3
description: "从 Redis 异步复制的丢失窗口出发，亲手实现写入确认，并沿 Redis 7.2.4 的 WAIT 源码理解复制偏移量、ACK、超时和保证边界。"
slug: /distributed-systems/02-write-acknowledgements
tags: [distributed-systems, redis, replication, source-code]
---

上一篇我们发现了一件反直觉的事：

```text
主节点 A：post:42:views = 101
副本   B：post:42:views = 100
客户端：SET 返回 OK
```

如果此时 A 不能继续服务，我们只能从 B 读到 `100`。原来，**A 告诉客户端“写入成功”，不等于 B 已经收到写入。**

于是出现一个新需求：对于更重要的更新，我们希望在告诉业务“可以放心继续”之前，至少确认一个副本已经跟上。

这篇只追一个问题：**怎样知道副本已经处理到哪次写入？**

## 一、最直接的办法：等副本写完再返回

先不看 Redis。假设我们可以同步调用 B，那么最直觉的修改是：

```python
# 概念伪代码：不是真实 Redis 实现
def set_and_confirm(key, value):
    A.set(key, value)
    B.set(key, value)      # 等待 B 完成
    return "OK"
```

对于一台始终可达且响应迅速的 B，这确实能避免“明明已经回复 OK，B 却还没得到更新”的窗口。

但现在假设 B 偶尔需要 3 秒才响应，或者网络突然中断。A 是不是也要一直等？如果 B 永远没有回应，我们是否永远不能回复客户端？

我们真正缺少的不是“必须等副本”的死规定，而是**一个有界的等待过程，以及一个让客户端知道等到了多少副本的结果**。

例如我们可以约定：最多等 100 毫秒；期间如果有至少一个副本确认了这次写入，就把确认数量返回。若期限到达，仍返回已经确认的实际数量。

注意这与“失败就回滚”不同。A 可能早已完成本地写入，只是没有等到 B 的确认。

## 二、要确认哪一次写入？

又有一个小问题：当 B 回复“我收到了”，A 怎么知道 B 收到的是 `100` 还是 `101`？

假设复制过程依次产生三次更新：

```text
更新 1：post:42:views = 100
更新 2：post:42:views = 101
更新 3：post:42:views = 102
```

如果 B 只说“我收到过更新”，这个回答没有意义。

一种简单设计是为更新安排一个递增位置。我们暂且把它叫作 **offset（偏移量）**。在教学模型里，每条更新增加 1；真实 Redis 的复制 offset 计量的是复制流字节位置，**不是命令条数**。

```text
主节点已经写到 offset=3
副本 B 确认处理到 offset=2

当前客户端要等 offset=3
=> B 还不能算确认了这次写入
```

于是，等待逻辑不必检查整个数据集是否相同，只需要检查：**副本确认的 offset 是否达到本次写入的目标位置。**

## 三、写一个不需要 Redis 的最小实验

下面是完整实验中的关键部分（教学化简，不模拟网络、磁盘或自动故障切换）：

```python
class ReplicationLab:
    def __init__(self, replica_names):
        self.leader = {}
        self.replicas = {name: {} for name in replica_names}
        self.offset = 0
        self.log = []
        self.acked_offset = {name: 0 for name in replica_names}
        self.client_offset = {}

    def set(self, client, key, value):
        self.offset += 1
        self.leader[key] = value
        self.log.append((self.offset, key, value))
        self.client_offset[client] = self.offset
        return "OK"

    def deliver(self, replica_name):
        for offset, key, value in self.log:
            if offset > self.acked_offset[replica_name]:
                self.replicas[replica_name][key] = value
        self.acked_offset[replica_name] = self.offset

    def ack_count(self, client):
        target = self.client_offset.get(client, 0)
        return sum(offset >= target for offset in self.acked_offset.values())
```

`client_offset` 为什么是按客户端记录，而不只是拿全局最新 offset？

因为我们想回答的是：“**这个客户端之前的写入**，有多少副本已经处理到了？”如果别的客户端随后又执行了更新，不应该强迫当前客户端去等待与自己无关的新写入。

接下来用一个人为安排的时间表模拟等待：

- 场景 A：B 在 30 毫秒时完成复制，我们最多等 100 毫秒，确认数量为 `1`。
- 场景 B：B 要到 250 毫秒才完成复制，但等待期限是 100 毫秒，返回 `0`；A 本地的新值仍然存在。
- 场景 C：即使上次等待超时，只要 A 还在运行，复制仍可能继续，B 后来依然可以得到新值。

完整脚本是本系列配套的 `examples/wait_ack_lab.py`（可以放在博客项目根目录下的 `examples/` 中）。在项目根目录执行：

```bash
python3 examples/wait_ack_lab.py
```

**已实际运行的输出：**

```text
场景 A：30ms 复制完成，WAIT 返回 1，副本值 101
场景 B：100ms 截止，WAIT 返回 0，主节点值 102，副本值 101
场景 C：超时后继续复制，副本最终值 102
```

这里的毫秒数是脚本里的逻辑时间，不是计时器测出的真实网络延迟；脚本的 `WAIT` 也只是教学化简。它验证的只是“用复制进度和超时判断副本确认数量”这一设计。

现在去真实 Redis 中寻找对应的实现。

## 四、Redis 为什么需要 WAIT？

Redis 普通 `SET` 不会默认等待副本确认。但如果业务需要额外确认，可以**在同一客户端连接**上，先执行 `SET`，再执行：

```redis
SET post:42:views 101
WAIT 1 1000
```

`WAIT 1 1000` 的含义是：等待当前连接此前的写入，被至少 1 个副本确认；最多等待 1000 毫秒。

返回结果是**实际已经确认的副本数量**，不是布尔值。例如：

```text
SET   -> OK
WAIT  -> (integer) 1   # 达到一个副本
```

也可能是：

```text
SET   -> OK
WAIT  -> (integer) 0   # 到期仍没有副本确认
```

所以正确的业务判断是检查 `WAIT` 返回值是否达到目标，而不能把“WAIT 正常返回”本身当作确认成功。并且 `WAIT` 返回 `0` **不会撤销**之前已经执行的 `SET`。

这一语义由 Redis 官方 [WAIT 文档](https://redis.io/docs/latest/commands/wait/)明确说明。

## 五、真实源码：Redis 怎么知道“当前客户端的写入位置”？

固定版本：[Redis 7.2.4](https://github.com/redis/redis/tree/7.2.4)。先看 [`src/server.c`](https://github.com/redis/redis/blob/7.2.4/src/server.c) 中的 `call()`。

上一篇已经看到，`call()` 负责调用当前命令处理函数，然后处理命令传播。现在在它的收尾部分，能找到：

**真实源码节选（Redis 7.2.4）：**

```c
if (old_master_repl_offset != server.master_repl_offset)
    c->woff = server.master_repl_offset;
```

这里 `server.master_repl_offset` 表示当前主节点复制流的位置；`c->woff` 记住**当前客户端最近一次产生传播的写入位置**。

为什么记录在 `client` 上？因为即将执行的 `WAIT` 需要知道“这条连接刚才写到了哪里”。

这也解释了一个容易犯的实验错误：如果用两个独立的 `redis-cli` 进程分别执行 `SET` 和 `WAIT`，它们不是同一个连接，不能靠第二个连接的 `WAIT` 去确认第一个连接此前的写入。

## 六、真实源码：WAIT 如何决定是否继续等待？

打开 [`src/replication.c`](https://github.com/redis/redis/blob/7.2.4/src/replication.c)，找到 `waitCommand(client *c)`。

**真实源码节选（只保留关键语句）：**

```c
long long offset = c->woff;
ackreplicas = replicationCountAcksByOffset(c->woff);
```

输入来自客户端：需要等待的副本数和超时时间；而目标 offset 来自 Redis 已经为该连接记录的写入位置。

接着函数先检查：已经有多少副本确认到这个 offset？

如果数量已经够了，它立即回复数量；否则在允许阻塞的执行上下文中调用：

```c
blockForReplication(c,timeout,offset,numreplicas);
replicationRequestAckFromSlaves();
```

这两个调用各自解决一个问题。

`blockForReplication()` 位于 [`src/blocked.c`](https://github.com/redis/redis/blob/7.2.4/src/blocked.c)：它记录这个客户端在等哪个 offset、需要多少副本，以及等待期限，并把客户端放进等待队列。**这里阻塞的是这条客户端命令，不是把整个 Redis 服务停住。**

`replicationRequestAckFromSlaves()` 并不立即逐个等待副本。它设置一个请求标志；随后 [`src/server.c`](https://github.com/redis/redis/blob/7.2.4/src/server.c) 的 `beforeSleep()` 在事件循环中统一发送 `REPLCONF GETACK`，让副本尽快报告处理进度。

为什么不在 `WAIT` 函数里同步读网络，一直等到 B 回消息？

因为 Redis 还需要继续处理网络事件、其他客户端请求和副本的 ACK。等待应当被登记为一种**客户端状态**，而不是一个占住整个服务器的循环。

## 七、真实源码：副本回复后，谁唤醒客户端？

副本处理复制流后，会通过 `REPLCONF ACK` 告诉主节点自己的进度。

在 `src/replication.c` 的 `replconfCommand()` 中，主节点处理 ACK，并更新该副本的 `repl_ack_off`。这意味着主节点知道了“B 已经处理到哪个复制位置”。

那么 Redis 怎样统计已确认的副本数？同一文件中的 `replicationCountAcksByOffset()` 对在线副本逐个检查，其中的关键判断是：

```c
if (slave->repl_ack_off >= offset) count++;
```

只要副本报告的进度达到等待目标，就计入确认数量。

随后，`processClientsWaitingReplicas()` 检查等待队列中的客户端。对于已经达到目标数量的客户端，它回复确认数量并解除阻塞；如果迟迟没有达到目标，超时处理也会让客户端得到当时的实际确认数量。

于是这条真实运行路径终于完整了：

```text
客户端连接：SET post:42:views 101
    ↓
call() 执行并传播写入
    ↓
记录这个客户端的 c->woff
    ↓
同一客户端连接：WAIT 1 1000
    ↓
waitCommand() 检查副本确认位置
    ├── 已满足 → 返回副本数量
    └── 未满足 → blockForReplication()
                       ↓
                 请求副本报告 ACK
                       ↓
                 副本处理并回复 REPLCONF ACK
                       ↓
                 主节点更新 repl_ack_off
                       ↓
                 processClientsWaitingReplicas()
                       ↓
                 达标返回数量，或到期返回当前数量
```

注意这是一条**正常路径的教学化调用与状态流示意**：它略去了事件循环调度细节、连接异常和其他命令分支，但关键函数都可以在 Redis 7.2.4 中核对。

## 八、WAIT 解决了什么，又没有解决什么？

回头看我们最初的问题：原来只能知道 A 写成功，现在还能知道至少几个副本已经处理到此次写入。

这当然提高了对复制进度的可见性，也能够缩小“副本还没有收到已确认写入”的故障窗口。

但它没有自动变成“数据永远不会丢”。至少还有三个边界：

1. **确认不等于落盘。** `WAIT` 统计副本对复制流处理进度的确认；它不等价于要求每个副本把数据安全 `fsync` 到磁盘。Redis 7.2 还提供了专门针对 AOF 同步确认的 `WAITAOF`，那是另一个问题。
2. **超时不等于写入回滚。** `WAIT` 返回 `0` 时，A 的写入可能已经完成，副本以后也可能追上。盲目重试非幂等操作，可能造成重复写入。
3. **收到多个副本 ACK 也不保证故障切换后一定选中包含该写入的副本。** Redis 官方明确说明，`WAIT` 不会把 Redis 变成强一致的存储系统，故障切换仍有可能丢失已确认写入。

这最后一点很值得继续追问：

我们已经知道有一个副本保存了更新，但如果 A 真的不可用，**到底由谁决定让哪一个副本接管？**

现在我们还没有实现“自动选择新主节点”的机制。

下一篇，我们就让 A 真正失去服务能力，从人工切换开始，逐步理解故障检测与 Failover 为什么出现，并对照 Redis Sentinel 源码验证。

---

### 源码与文档

- [Redis 7.2.4 `src/server.c`](https://github.com/redis/redis/blob/7.2.4/src/server.c)：`call()`、`beforeSleep()`。
- [Redis 7.2.4 `src/replication.c`](https://github.com/redis/redis/blob/7.2.4/src/replication.c)：`waitCommand()`、`replicationCountAcksByOffset()`、`replconfCommand()`、`processClientsWaitingReplicas()`。
- [Redis 7.2.4 `src/blocked.c`](https://github.com/redis/redis/blob/7.2.4/src/blocked.c)：`blockForReplication()` 与阻塞客户端的处理。
- [Redis 官方 WAIT 文档](https://redis.io/docs/latest/commands/wait/)：语法、超时、当前连接语义及一致性边界。
- [Redis 官方复制文档](https://redis.io/docs/latest/manual/replication/)：默认异步复制与 WAIT 的保证范围。

> **验证说明：** `examples/wait_ack_lab.py` 已执行并核对输出；本文未运行 Redis 7.2.4 双节点集成测试。示例中的逻辑时间、偏移量和失败场景均为教学化简，不是 Redis 服务器的实际测量结果。
