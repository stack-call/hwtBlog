---
title: "分布式系统 06：一台机器装不下所有数据怎么办？——从完整复制到 Redis Hash Slot"
sidebar_label: "六、一台机器装不下所有数据"
sidebar_position: 7
description: "以文章访问计数器的容量问题为起点，从复制无法扩大单份数据容量推导数据分片；再沿 Redis 7.2.4 的 keyHashSlot、getNodeByQuery、processCommand 源码追踪一次键的路由。"
slug: /distributed-systems/06-redis-cluster-hash-slots
tags: [distributed-systems, redis, cluster, sharding, hash-slot, source-code]
---

# 一台机器装不下所有数据怎么办？——从完整复制到 Redis Hash Slot

上一篇我们让多个 Sentinel 对故障切换的授权达成一致，理解了为什么**多数派选举**有助于阻止彼此冲突的切换决策，也看到了它不能替代数据复制协议。

先把网络分区和选主放在一边。

我们的文章系统仍然在运行，访问计数仍然长这样：

```text
post:42:views = 101
```

但是网站里的文章越来越多。最开始只有几十篇，后来是几百万篇，再后来所有访问计数加起来已经放不进单台机器的内存。

现在的问题不是机器坏了谁来接管，而是：**即使所有机器都正常，数据也已经装不下了。**

这一篇只解决这个新问题。我们从一个极小的容量限制开始，先自己实现数据拆分，再查看 Redis Cluster 为什么让一个 Key 先经过 Hash Slot，最后才找到负责它的节点。

## 一、为什么增加副本没有扩大容量？

假设为了看清问题，我们人为规定每个节点**最多保存四个不同的 Key**。

> 这是教学化简：用“Key 个数上限”代替真实机器的内存容量。真实 Redis 的 `maxmemory`、数据结构大小、淘汰策略等均不在这个模型里。

一台节点 A 可以保存：

```text
A
  post:1:views = 100
  post:2:views = 100
  post:3:views = 100
  post:4:views = 100
```

现在尝试写入第五个 Key：

```text
SET post:5:views 100
```

我们的教学节点会拒绝它：容量已经用完。

还记得第一篇为了容错而增加的副本 B 吗？让 B 完整复制 A：

```text
A                    B（A 的完整副本）
post:1:views         post:1:views
post:2:views         post:2:views
post:3:views         post:3:views
post:4:views         post:4:views
```

表面上我们有两台机器，每台能放四个 Key，一共能放八个 Key。但因为 B 必须保存 A 的同一份数据，**系统仍然只能容纳四个不同的 Key**。

B 增加了冗余，并没有让一份完整数据集变得更小。

这里要非常明确地区分：

- **复制（Replication）**：让多台机器保存同一份逻辑数据，以便提供冗余或其他服务能力。
- **分片（Sharding）**：把不同的数据交给不同机器负责，让单台机器只保存总数据的一部分。

我们已经学过复制。现在需要的是第二种能力。

## 二、最简单的分片：奇数文章去 A，偶数文章去 B

先不引入 Redis 的任何术语。

既然每台机器最多放四个 Key，那我们可以让两台机器分别负责一半文章：

```text
A（偶数文章）         B（奇数文章）
post:2:views         post:1:views
post:4:views         post:3:views
post:6:views         post:5:views
post:8:views         post:7:views
```

这时两台机器能容纳八个不同的 Key。

为了实现它，我们只需要在写入前作出一个决定：这个 Key 应该交给哪台机器？

**概念原型：**

```python
nodes = {"A": {}, "B": {}}

def owner(post_id: int) -> str:
    return "A" if post_id % 2 == 0 else "B"

def record_visit(post_id: int, value: int):
    node = owner(post_id)
    nodes[node][f"post:{post_id}:views"] = value
```

只要所有客户端使用同一条规则，`post:42:views` 就会被送往 A，`post:43:views` 就会被送往 B。

到这一步，已经发生了一个根本变化：

**一个节点不再需要存储全部文章的访问次数。**

但是，我们很快会遇到一个小麻烦。

以前只要连接 A，就可以读写任意文章。现在一个客户端可能连接了 A，却想访问实际上归 B 管理的 Key。

例如：

```text
客户端连接 A
GET post:43:views
```

A 不应该随便返回一个“Key 不存在”。因为真正的数据可能在 B。

因此，数据拆分至少还需要另一种能力：**请求路由**——先确定谁负责这个 Key，再执行操作。

## 三、为什么真实 Redis 不直接用文章 ID 的奇偶数？

刚才的规则对文章访问计数器足够直观，但 Redis 是通用的 Key-Value 存储：

```text
post:42:views
user:17:profile
coupon:2026:inventory
order:981:status
```

不是每个 Key 都能提取出“文章 ID”，我们也不能要求 Redis 理解业务键名。

Redis 需要一套对任意字节串 Key 都适用、且各节点能够独立算出相同结果的规则。

最直接的方式是对 Key 求一个稳定的哈希值。这样不用理解 Key 的业务含义，就能把大量 Key 分散开来。

但如果规则直接写成：

```text
node = hash(key) % 2
```

将来新增第三台机器时，我们就需要把 `% 2` 改成 `% 3`。大量既有 Key 的计算结果可能改变。

我们现在不急着处理迁移，但这个担忧已经足以提示：**“Key 属于哪个分组”最好不要与“目前集群有几台机器”直接绑死。**

于是我们可以插入一个稳定的中间层。

先把 Key 分配到一组**固定编号的桶**，再让机器负责其中一部分桶。

这就是 Redis Cluster 的 Hash Slot（哈希槽）设计。

## 四、先把 Key 映射到固定的 Slot

Redis Cluster 在一个集群中定义了固定的 **16384 个 Slot**，编号是 `0` 到 `16383`。

对普通的、没有特殊 Hash Tag 的 Key，规则可以写作：

```text
slot = CRC16(key) mod 16384
```

这里的 CRC16 是一种确定性的 16 位计算方法：同一段 Key 字节总会得到相同的计算结果。我们暂时不研究 CRC 算法本身，只关心它能够让不同节点算出一致的 Slot 编号。

比如，贯穿本系列的 Key：

```text
post:42:views
```

按 Redis Cluster 使用的 CRC16 规则计算，得到：

```text
slot = 10015
```

假设这次**从空集群开始**，我们把 Slot 这样分给两个 Master：

```text
Slot 0 ~ 8191       → A
Slot 8192 ~ 16383   → B
```

那么 `post:42:views` 的 Slot 是 `10015`，它自然归 B 负责。

注意，**Slot 不等于物理节点**。一个节点会负责许多 Slot；在稳定状态下，每个 Slot 有确定的负责节点。

这条规则让数据路由分成两步：

```text
Key
  |
  v
计算 Slot（固定范围 0~16383）
  |
  v
查 Slot → Master 的映射表
  |
  v
向目标 Master 发送命令
```

为什么要多一层看似没有必要的 Slot？

因为即使未来节点数量发生变化，**Key 计算出来的 Slot 编号也不必跟着变化**。需要调整的主要是哪些节点负责哪些 Slot。

至于“只改负责节点的表，原来的 Key 怎样搬过去”，是下一篇必须认真解决的问题。这里不能假装换一下表就完成了扩容。

现在先去 Redis 7.2.4 源码验证：Slot 究竟在哪儿算出来？

## 五、真实源码：`keyHashSlot()` 为什么只关心 Key？

**源码基准：** [Redis 7.2.4](https://github.com/redis/redis/tree/7.2.4)，文件 [`src/cluster.c`](https://github.com/redis/redis/blob/7.2.4/src/cluster.c#L1243-L1269)，函数 `keyHashSlot(char *key, int keylen)`。

在这个函数里，最简单的普通 Key 分支只有一行核心代码。

**真实源码节选：**

```c
if (s == keylen) return crc16(key,keylen) & 0x3FFF;
```

这里 `s == keylen` 表示没有在 Key 中找到左花括号 `{`。

`crc16(key,keylen)` 为 Key 字节计算 CRC16。

`0x3FFF` 的二进制低 14 位全是 `1`。与这个掩码进行按位与，相当于对 16384 取余，因此得到 `0 ~ 16383` 的 Slot 编号。

**输入**是 Key 字节和长度；**输出**是一个整数 Slot；**副作用**是无——这个函数既不访问节点，也不写入数据。

这恰好证明了一个关键设计：**计算 Key 属于哪个 Slot，与 Slot 现在由哪台机器负责，是两个分离的步骤。**

源码还会处理带 `{...}` 的特殊 Key，只计算括号中非空的部分。它是为了让相关 Key 有机会落在同一 Slot 的 *Hash Tag* 规则。我们当前所有示例都不包含花括号，所以先不展开；等讨论跨 Key 操作时再需要它。

## 六、真实源码：有了 Slot，怎样找到负责的节点？

继续看同一个文件中的 [`getNodeByQuery()`](https://github.com/redis/redis/blob/7.2.4/src/cluster.c#L6660-L6887)。

它不仅用于 `GET`，也用于 `SET` 等针对 Key 的命令。它的任务是根据命令涉及的 Key，判断应当由哪一个集群节点处理。

我们先只跟踪一条普通单 Key 命令：

```redis
GET post:42:views
```

`getNodeByQuery()` 会通过命令的 Key 位置提取 Key，再调用 `keyHashSlot()` 得到它的 Slot。

**真实源码节选：**

```c
int thisslot = keyHashSlot((char*)thiskey->ptr,
                           sdslen(thiskey->ptr));
```

然后，对于请求遇到的第一个 Key，它把 Slot 转成负责节点：

**真实源码节选：**

```c
slot = thisslot;
n = server.cluster->slots[slot];
```

`server.cluster->slots` 就是我们刚才推导出来的“Slot → 节点”映射表。

所以这次运行过程是：

```text
请求：GET post:42:views
       |
       v
keyHashSlot("post:42:views")
       |
       v
slot = 10015
       |
       v
server.cluster->slots[10015]
       |
       v
负责节点 B
```

在这个过程中，函数的输入不只是 Key，还有当前客户端、命令及参数；它还会为异常路由设置错误码。我们暂时不研究多个 Key、Slot 迁移和集群异常，只看最基本的一条命令。

**输出**是应处理命令的 `clusterNode *`。下游根据它是否是本机，决定本机执行还是让客户端去别处。

现在自然产生一个问题：如果客户端已经连到了 A，但计算结果指向 B，会发生什么？

## 七、真实源码：为什么 A 不直接替客户端把请求发给 B？

打开 [`src/server.c`](https://github.com/redis/redis/blob/7.2.4/src/server.c#L3634-L3656)，看 `processCommand()` 中集群模式的路由检查。

在常规客户端请求进入真正命令执行流程之前，Redis 会调用 `getNodeByQuery()`。

**真实源码节选（省略调用参数的展示仅用于排版；完整调用见链接）：**

```c
clusterNode *n = getNodeByQuery(c,c->cmd,c->argv,c->argc,
                                &c->slot,&error_code);
```

随后判断负责节点是不是自己。如果不是，`processCommand()` 会通过 `clusterRedirectClient()` 生成重定向响应，并结束当前命令处理，而不是在 A 本地执行那个 `GET`。

在我们当前**没有迁移中的 Slot**这一正常场景中，如果请求打到了错误的节点，`getNodeByQuery()` 会设置 `CLUSTER_REDIR_MOVED`。之后，`clusterRedirectClient()` 将 Slot 编号和目标地址放入响应。

例如，客户端误把 `post:42:views` 发给 A，实际负责的是 B，示意响应如下：

```text
-MOVED 10015 B的主机地址:端口
```

这不是“Key 不存在”，而是在告诉客户端：**这个 Slot 的负责节点不是我，请改去目标节点。**

一个支持 Redis Cluster 的客户端收到 `MOVED` 后，可以重新向 B 发出请求，并更新或刷新自己的 Slot 路由信息。

Redis Cluster 的普通命令路由以客户端重定向为主，A 不会把每个请求当作代理请求转发给 B。

这层设计有什么用？

它让客户端最终能够直接与正确的节点通信，避免所有请求都必须通过一个统一的转发节点。

但是我们又增加了一个责任：**客户端必须能够正确处理重定向**。只支持单机 Redis 连接方式的简单客户端，并不能自动处理完整的 Redis Cluster 拓扑变化。

## 八、运行一个真正能看到分片效果的实验

理论到这里足够了。现在让我们用一个纯 Python 程序验证它。

完整代码在：`examples/sharding_slots_lab.py`。

它不需要 Redis、不需要 Docker，执行命令：

```bash
python3 examples/sharding_slots_lab.py
```

关键部分使用 Python 标准库 `binascii.crc_hqx`，对普通 Key 计算与 Redis Cluster 相同的 CRC16/XMODEM 结果：

**教学实现：**

```python
from binascii import crc_hqx

def hash_slot(key: str) -> int:
    return crc_hqx(key.encode("utf-8"), 0) & 0x3FFF

def owner(key: str) -> str:
    return "A" if hash_slot(key) < 8192 else "B"
```

注意：这个片段只覆盖**没有 Hash Tag 的 Key**。真实 `keyHashSlot()` 还要处理 `{...}`。

实验做三件事。

首先，证明两个完整副本各自最多能装四个 Key，并不意味着可以保存八个不同 Key。

然后，创建一个**新的空分片布局**，把 `post:1:views` 到 `post:8:views` 分布到 A 和 B。由于这八个 Key 在实验所设定的 Slot 分界上刚好各占四个，两个节点都能装得下。

最后，故意把 `post:1:views` 发送给 A。它属于 Slot `12947`，负责节点是 B，所以模型返回 `MOVED`，客户端再改发给 B。

**已实际运行的输出：**

```text
1. Full replica: A=4, replica=4, fifth key rejected
2. Two shards: A=4, B=4, total=8
3. Wrong node: MOVED 12947 B; retry on B -> OK; value=101
4. Original key post:42:views -> slot=10015, owner=B
```

这些断言可以验证我们当前的核心结论：

1. 复制并不增加可容纳的**不同 Key**数量上限。
2. 分片让不同 Key 可以由不同节点保存。
3. 由 Key 算出的 Slot 加上 Slot 所有权表，可以得到负责节点。
4. 联系错误节点时，客户端可以按重定向信息重试。

不过要特别说明：`MOVED 12947 B` 是实验自定义的简写，真实 Redis 的 `MOVED` 包含目标地址和端口。本实验也没有启动 Redis、检查真实 `processCommand()` 的网络输出，或者测试节点故障后的行为。

## 九、分片并没有替代前面学过的复制与故障切换

现在我们终于能把两种“多节点”设计放在一起，而不混淆职责。

最初，A 和 B 保存**相同的 Key**：

```text
A：全部文章计数
B：全部文章计数（A 的副本）
```

这是一组完整复制关系。

现在，A 和 B 保存**不同的 Key**：

```text
A：Slot 0~8191 对应的 Key
B：Slot 8192~16383 对应的 Key
```

这是分片关系。

如果只部署这两个分片 Master，其中一台故障，那么它负责的那一部分数据仍可能不可用。

如果还需要容错，就要给**各个分片**分别配置副本，让对应副本在必要时接管自己负责的 Slot。

因此，分片和复制不是互相替代的技术，而是解决不同维度的问题，可以组合使用。

还要注意：本章研究的是 **Redis Cluster** 的 Slot 路由，不是把前几章的 Redis Sentinel 直接改名成 Cluster。Redis Cluster 有自己的节点发现和故障切换机制；我们这里只研究其中的分片部分，不提前展开另一套选主实现。

此外，真实生产环境不能仅凭“两台 Master 已能装下数据”就断言整个集群满足高可用；我们在这一章故意只证明**逻辑容量扩展**。

## 十、下一台机器加入时，问题又出现了

现在假设 A 和 B 已经运行了一段时间：

```text
A：负责 Slot 0~8191
B：负责 Slot 8192~16383
```

我们又买来第三台机器 C，希望把一部分数据转移过去。

看起来可以简单地改一张表：

```text
A：负责一部分 Slot
B：负责一部分 Slot
C：负责剩余 Slot
```

但这样做可能立刻造成错误。

因为 **Slot 的所有权变化，并不意味着这个 Slot 对应的 Key 已经物理移动到了 C**。

例如某个 Key 原本保存在 A，新的映射却告诉客户端去 C。如果我们只更改映射、不移动数据，C 会认为这个 Key 不存在。更麻烦的是，迁移期间客户端还可能继续读写它。

至此，我们又遇到了一个必须解决的新限制：

**怎样一边提供服务，一边把 Slot 所包含的 Key 从旧节点移动到新节点？**

下一篇：《增加一台机器后，数据怎么不停机搬家？——Redis Cluster 的 Slot 迁移与 MOVED/ASK》。

我们会继续使用 `post:42:views`，从一次天真的“直接改 Slot 所有权”开始，亲眼观察它怎样读不到旧数据，再一步步引入迁移状态。届时，今天没有详细解释的 `ASK` 才真正有了出场理由。

---

## 源码索引与验证边界

**固定版本：Redis 7.2.4**

- [`src/cluster.c`：`keyHashSlot()`](https://github.com/redis/redis/blob/7.2.4/src/cluster.c#L1243-L1269)：输入 Key 和长度，计算 Slot；普通 Key 使用 CRC16 的低 14 位，特殊 `{...}` Key 有不同的取哈希范围规则。
- [`src/cluster.c`：`getNodeByQuery()`](https://github.com/redis/redis/blob/7.2.4/src/cluster.c#L6660-L6887)：由命令的 Key 计算 Slot，查询 `server.cluster->slots`，返回负责节点，并按需设置重定向错误码。
- [`src/cluster.c`：`clusterRedirectClient()`](https://github.com/redis/redis/blob/7.2.4/src/cluster.c#L6895-L6920)：将 `MOVED` 等集群路由结果编码为客户端回复。
- [`src/server.c`：`processCommand()`](https://github.com/redis/redis/blob/7.2.4/src/server.c#L3634-L3656)：在实际执行命令前调用集群路由判断，必要时返回重定向。
- [Redis 官方 Cluster Specification](https://redis.io/docs/latest/operate/oss_and_stack/reference/cluster-spec/)：16384 个 Slot、CRC16/Hash Tag、客户端重定向与迁移约定。
- [Redis 官方扩容说明](https://redis.io/docs/latest/management/scaling/)：分片与 Slot 到节点的分配关系。

**验证范围：**源码函数、关键语句和调用关系已按 Redis 7.2.4 标签核对。`examples/sharding_slots_lab.py` 已用 Python 3 在本地实际运行并通过断言；其容量上限、节点状态、重定向和 Key 存储都是教学模型，不是 Redis Cluster 的集成测试。没有执行 Docusaurus 站点构建。
