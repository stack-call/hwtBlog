---
title: "分布式系统 01：从一台机器到两个副本，为什么写入成功还会丢？"
sidebar_label: "一、从一台机器到两个副本"
sidebar_position: 2
description: "从一个文章访问计数器出发，推导数据复制的必要性，并沿 Redis 7.2.4 源码追踪 SET 到复制缓冲的路径。"
slug: /distributed-systems/01-from-one-node-to-replication
tags: [distributed-systems, replication, redis, source-code]
---

# 从一台机器到两个副本，为什么写入成功还会丢？

在系列引言中，我们留下了一个问题：文章访问次数原本只放在一台机器上。如果这台机器突然无法工作，用户还怎样继续访问数据？

先不要考虑 Redis Cluster，也不要讨论选主算法。

我们从只保存一个键开始：

```text
post:42:views = 100
```

我们的目标只有一个：

**让一台机器不可用时，另一台机器仍能提供这份数据。**

## 一、一台机器，为什么暂时够用？

假设我们实现了一个最简单的键值存储。

**教学化简：** 下面的代码只模拟内存数据，没有网络、持久化和并发处理。

```python
class KVNode:
    def __init__(self, name):
        self.name = name
        self.data = {}

    def set(self, key, value):
        self.data[key] = value

    def get(self, key):
        return self.data.get(key)


node_a = KVNode("A")

node_a.set("post:42:views", 100)

assert node_a.get("post:42:views") == 100
```

一次写入、一次读取，完全正常。

此时“再增加几台服务器”没有明确收益。对于当前需求，我们没有理由引入复杂架构。

现在假设 A 所在的机器突然失去网络连接。

用户再来读取文章访问次数，就无法得到结果了。

这时一个直觉的方案是：能否把数据保存到磁盘？

当然可以。我们可以通过持久化，让进程重新启动后有机会恢复之前的数据。

但持久化并没有解决所有问题。

假设数据已经安全地保存在 A 的磁盘上，可 A 所在的机器暂时无法启动。用户依然不能通过 A 获取数据。

这里需要区分两个问题：

- **数据恢复**：机器恢复运行后，还能不能重新找回数据？
- **服务可用性**：这台机器不可用时，其他机器能不能接管服务？

它们不是同一个问题。

我们目前真正缺少的是：

**另一台机器也拥有这份数据，并且能够在 A 不可用时提供服务。**

于是，我们第一次需要增加一个节点。

## 二、增加第二台机器，问题解决了吗？

最简单的方法是启动一台 B，然后把 A 的数据复制过去。

```python
node_b = KVNode("B")

node_b.set(
    "post:42:views",
    node_a.get("post:42:views")
)

assert node_b.get("post:42:views") == 100
```

此时，A 和 B 都有相同的数据：

```text
Node A                  Node B
  100                     100
```

如果 A 发生故障，理论上可以让用户从 B 获取这份数据。

看起来，我们已经解决了问题。

但现在网站又收到了一次访问。

A 处理新的写入：

```python
node_a.set("post:42:views", 101)
```

再检查两台机器：

```python
assert node_a.get("post:42:views") == 101
assert node_b.get("post:42:views") == 100
```

结果变成：

```text
Node A                  Node B
  101                     100
```

为什么？

因为我们只在最开始复制了一次。

B 保存的是旧状态，并不知道 A 后来又发生了什么变化。

如果 A 继续处理写入，B 的数据就会越来越落后。

**因此，只复制一次还不够。我们还需要持续把 A 的新写入传播给 B。**

这时才有必要引入一个新的机制：Replication（数据复制）。

## 三、最简单的数据复制应该怎么设计？

先不考虑真实 Redis。

既然 B 不知道 A 的更新，那最直接的方法就是让 A 每次修改数据时，都通知 B。

我们可以先设计一个简单模型。

```python
class AsyncReplicationDemo:
    def __init__(self, leader, replica):
        self.leader = leader
        self.replica = replica
        self.pending = []

    def set(self, key, value):
        self.leader.set(key, value)

        # 记录需要复制的更新
        self.pending.append((key, value))

        return "OK"

    def deliver_all(self):
        for key, value in self.pending:
            self.replica.set(key, value)

        self.pending.clear()
```

这段代码增加了一个 `pending` 列表。

当客户端调用 `set()` 时，系统做两件事：

首先，A 修改自己的数据。

然后，它把这次更新放入待发送队列，准备交给 B。

至于什么时候真正交付，可以由 `deliver_all()` 决定。

我们用这个模型模拟一次正常执行：

```python
a = KVNode("A")
b = KVNode("B")

system = AsyncReplicationDemo(a, b)

system.set("post:42:views", 101)
system.deliver_all()

assert a.get("post:42:views") == 101
assert b.get("post:42:views") == 101
```

现在两个节点再次拥有相同的数据。

注意，`pending` 只是一个用于教学的队列，不是 Redis 的真实网络缓冲区，也没有模拟复制连接和重试协议。

但这个简单模型已经能够让我们观察一个重要行为：

**客户端收到成功响应的时间，和 B 真正收到更新的时间，可能不同。**

为什么要把这两个时刻分开？

因为如果 A 必须等 B 执行完成后才能响应，那么每次写入都要承担额外的通信和等待成本。

如果 B 暂时变慢，A 的写入延迟也会增加。

因此，一种合理的选择就是：A 先完成本地写入并回复客户端，之后再让 B 跟上。

这种方式称为异步复制。

它提高了响应速度，但也带来了新的问题。

## 四、现在让系统失败一次

假设最开始 A 和 B 的数据都是 `100`。

客户端向 A 写入 `101`。

A 已经修改了自己的数据，并把更新加入待复制队列。

然后它返回：

```text
OK
```

但是我们故意不调用 `deliver_all()`。

此时：

```text
时间 T0

A = 100
B = 100

时间 T1：客户端执行 SET 101

A = 101
B = 100

客户端收到：OK
```

现在，假设 A 在复制数据到 B 之前突然故障。

我们改为从 B 读取数据。

结果是什么？

```text
时间 T2：A 不可用

A = 101（无法访问）
B = 100

时间 T3：改为读取 B

GET post:42:views

返回：100
```

客户端明明已经收到成功响应，为什么读取结果又变回了 `100`？

因为 A 确认的是本地写入，而 B 还没有收到这次更新。

这说明：

**有副本不等于所有已经成功写入的数据都不会丢失。**

Replication 解决了数据冗余和持续同步的问题，却没有自动保证任何时刻所有节点都拥有完全相同的数据。

接下来，我们去真正的 Redis 源码里确认这种设计。

## 五、真实 Redis 是如何处理 SET 和复制的？

本系列固定研究 Redis 7.2.4：

https://github.com/redis/redis/tree/7.2.4

我们只研究最简单的命令：

```redis
SET post:42:views 101
```

暂时不研究过期时间、条件写入或事务。

### 5.1 一条 SET 命令从哪里进入？

先打开：

[`src/t_string.c`](https://github.com/redis/redis/blob/7.2.4/src/t_string.c)

找到函数：

```c
void setCommand(client *c)
```

这里的 `c` 是当前客户端的上下文，里面包含了本次命令的参数。

当 Redis 识别到客户端执行的是 `SET`，对应的命令处理函数就会被调用。

它会先解析命令选项，再调用：

```c
setGenericCommand(...)
```

为什么不直接在 `setCommand()` 中完成全部写入？

因为 Redis 还有其他相关命令，它们可以复用底层的字符串写入逻辑。

不过，这不是本章的重点。

我们现在只需要知道：

**SET 的实际数据修改发生在 `setGenericCommand()` 中。**

### 5.2 SET 在什么时候修改数据？

继续阅读同一个文件中的：

```c
void setGenericCommand(...)
```

跳过当前不需要的条件写入和过期时间处理。

可以找到下面两行。

**真实源码节选（Redis 7.2.4）：**

```c
setKey(c,c->db,key,val,setkey_flags);
server.dirty++;
```

这两行非常关键。

`setKey()` 修改当前 Redis 实例中的数据。

这里的 `key` 和 `val` 来自客户端命令，`c->db` 是本次操作的数据库。

而 `server.dirty++` 记录数据发生了修改。

为什么需要这个标记？

因为 Redis 后面需要知道：这条命令是否改变了数据，从而决定是否要将修改传播出去。

但我们现在先把注意力放在另一件事上。

客户端的 `OK` 是什么时候产生的？

继续往下，可以看到：

**真实源码节选：**

```c
addReply(c, ok_reply ? ok_reply : shared.ok);
```

对于没有使用 `GET` 等特殊选项的普通成功 `SET`，Redis 会通过这条路径准备成功回复。

注意，这里是把响应加入客户端回复路径，并不意味着此时网络字节已经到达客户端。

但更重要的是：

**这段代码没有要求副本先执行完成，才能生成 `OK`。**

那么，把更新传播给副本的工作发生在哪里？

### 5.3 为什么 setGenericCommand() 没有直接等待副本？

打开另一个文件：

[`src/server.c`](https://github.com/redis/redis/blob/7.2.4/src/server.c)

找到：

```c
void call(client *c, int flags)
```

`call()` 是 Redis 执行命令的重要入口。

它会调用当前命令真正的处理函数。

**真实源码节选：**

```c
c->cmd->proc(c);
```

对于普通 `SET`，最终就会执行刚才看到的 `setCommand()`。

命令执行完以后，Redis 会检查本次操作是否改变了数据。

如果确实发生变化，并满足传播条件，就需要安排后续传播工作。

在 `call()` 中可以看到：

**真实源码节选：**

```c
if (dirty) propagate_flags |= (PROPAGATE_AOF|PROPAGATE_REPL);
```

这里出现了两个不同的传播目标：

`PROPAGATE_AOF` 表示需要考虑把修改传播给 AOF 持久化路径。

`PROPAGATE_REPL` 表示需要把修改传播给副本。

我们本章只关心第二个。

在普通传播路径上，Redis 会通过 `alsoPropagate()` 记录需要传播的命令。

执行单元结束时，`afterCommand()` 触发后续处理，最终通过 `propagatePendingCommands()` 和 `propagateNow()` 进入复制流程。

用我们目前已经理解的函数，可以将这条路径整理为：

```text
call()
  |
  +-- 执行当前命令
  |     |
  |     +-- setCommand()
  |           |
  |           +-- setGenericCommand()
  |                 |
  |                 +-- setKey()
  |                 |     修改当前节点
  |                 |
  |                 +-- addReply()
  |                       准备 OK
  |
  +-- 判断数据是否变化
        |
        +-- alsoPropagate()
              |
              +-- 执行单元结束
                    |
                    +-- propagatePendingCommands()
                          |
                          +-- propagateNow()
                                |
                                +-- replicationFeedSlaves()
```

这是根据真实源码整理的正常路径示意，省略了传播条件和其他执行分支。

为什么 Redis 要这样设计？

因为不只是 `SET` 会改变数据。

大量命令都可能产生需要复制的修改。

如果每个命令都单独实现一套复制逻辑，代码会出现大量重复。

因此，Redis 将具体命令的数据修改逻辑与通用的命令传播机制分开。

但这还没有回答：数据真正到了哪里？

### 5.4 复制流是怎样产生的？

打开：

[`src/replication.c`](https://github.com/redis/redis/blob/7.2.4/src/replication.c)

找到：

```c
void replicationFeedSlaves(...)
```

这个函数负责将需要传播的命令组织为复制数据流。

其中有一行：

**真实源码节选：**

```c
feedReplicationBufferWithObject(argv[j]);
```

`argv[j]` 是待复制命令的一个参数。

Redis 会将命令参数按照复制协议编码，放入用于复制的数据缓冲中。

随后还需要经过连接传输，副本接收、解析并执行对应命令。

所以现在我们可以把两个阶段区分开：

```text
阶段 A：主节点执行 SET
        修改本地数据
        准备 OK 回复

阶段 B：复制机制传播更新
        写入复制缓冲
        通过网络发送
        副本接收并执行
```

把命令放进复制缓冲，并不等于副本已经执行成功。

**Redis 默认的异步复制，并不要求普通 SET 等待阶段 B 全部完成后才向客户端返回成功。**

这就解释了我们之前观察到的故障窗口。

而且现在这个结论不是单纯来自一个教学模型，而是可以由真实 Redis 的命令执行和复制路径共同支持。

## 六、用 Python 实际复现数据丢失窗口

我们把前面写过的两个类放到同一个文件中，再执行一次完整实验。

文件名：

```text
examples/replication_gap.py
```

核心执行过程如下：

```python
key = "post:42:views"

a = KVNode("A")
b = KVNode("B")

system = AsyncReplicationDemo(a, b)

# 最初先同步到 100
system.set(key, 100)
system.deliver_all()

# 新写入返回 OK
reply = system.set(key, 101)

# 故意不交付最新复制消息
# 模拟 A 无法继续服务后切换到 B
new_leader = b

assert reply == "OK"
assert a.get(key) == 101
assert new_leader.get(key) == 100
```

这个实验的完整脚本已经实际运行，结果如下：

```text
T0 同步后：Leader=100, Replica=100
T1 客户端收到：OK
T1 尚未复制：Leader=101, Replica=100
T2 模拟故障切换：新 Leader=100
验证：已确认的 101 未出现在新 Leader 上。
```

这里的“故障切换”是手动选择 B 作为新的服务节点。

它没有实现真正的自动选主，也没有模拟 Redis 的真实网络连接、持久化恢复或复制偏移量。

因此，它验证的是：

**在先确认本地写入、后交付副本的模型中，已确认写入可能不在接管服务的副本上。**

这和 Redis 官方文档所描述的异步复制风险一致，但不能把这个 Python 实验当成 Redis 集成测试。

## 七、回到最初的问题

现在我们重新观察整个演化过程。

最开始，我们只有一台机器。

它可以完成读写，却无法在自己不可用时继续提供服务。

于是，我们增加了第二台机器。

但是只复制一次数据并不够，因为新的写入会让两台机器的状态逐渐不同。

于是我们又增加了持续复制机制。

系统终于能够让 B 不断跟上 A 的新写入。

可新的限制也出现了：

如果 A 不等待 B 就返回成功，那么客户端收到 `OK` 时，B 可能还没有收到这条更新。

因此，我们得到本章最重要的结论：

**Replication 解决的是数据冗余和传播问题，而不是自动保证所有已确认写入在故障后都能保留。**

这不意味着异步复制是错误设计。

它可以减少写入等待时间，让系统保持较低延迟，并使副本有机会在主节点故障时接管服务。

但它的收益伴随着明确的限制。

现在一个新的问题自然出现了。

假设我们的业务不允许已经确认的写入轻易丢失。

那么是不是应该修改写入流程：

不再让 A 写完本地就返回，而是等到 B 也确认收到更新之后，才告诉客户端成功？

如果这样做，B 很慢或者突然失去连接时，又应该怎么办？

**下一篇：《客户端收到 OK，到底意味着什么？——从异步复制走向写入确认》**

我们会从这个问题出发，逐步研究不同写入确认策略，并对照 Redis `WAIT`、MySQL 和 Kafka 的设计选择。

---

**固定源码与参考资料**

- [Redis 7.2.4：t_string.c](https://github.com/redis/redis/blob/7.2.4/src/t_string.c)
- [Redis 7.2.4：server.c](https://github.com/redis/redis/blob/7.2.4/src/server.c)
- [Redis 7.2.4：replication.c](https://github.com/redis/redis/blob/7.2.4/src/replication.c)
- [Redis 官方：Replication](https://redis.io/docs/latest/manual/replication/)
- [Redis Cluster Specification — Write safety](https://redis.io/docs/latest/operate/oss_and_stack/reference/cluster-spec/#write-safety)
