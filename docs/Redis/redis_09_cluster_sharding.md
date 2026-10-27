---
title: "第九章：一台 Redis 放不下所有数据怎么办？从单机存储到 Redis Cluster"
sidebar_label: "09 - Redis Cluster 与数据分片"
sidebar_position: 10
description: "承接主从复制与 Sentinel，从单主节点的容量上限出发，逐步理解 Redis Cluster 的哈希槽、请求路由、MOVED 与 ASK 重定向和在线槽迁移，并对照 Redis 7.2.0 源码。"
tags:
  - Redis
  - 源码精读
  - Redis Cluster
  - 数据分片
  - 哈希槽
---

# 第九章：一台 Redis 放不下所有数据怎么办？从单机存储到 Redis Cluster

在前面几章里，我们已经为活动访问计数构建了一套越来越完整的数据服务。

最开始，数据只是 Python 字典中的一个整数。为了让多个进程共同访问它，我们把数据交给 Redis；为了保证并发递增不会丢失更新，我们使用 `INCR`；为了控制过期数据和内存使用，我们研究了 `EXPIRE` 与内存淘汰；为了应对重启、单节点故障，我们又研究了 RDB、AOF、主从复制和 Sentinel。

现在，假设活动系统继续增长，所有活动的计数、活动状态和相关临时数据加起来，已经接近**一台 Redis 主节点的内存容量上限**。

问题来了：

**既然已经有多个 Redis 节点，为什么不能把数据分开放在它们上面？**

这一章，我们就从这个问题出发。

我们暂时不讨论 Redis Cluster 完整的故障选举流程，只沿着一条可以实际观察的请求，弄清楚：**一个 Key 为什么会落到特定节点？请求发错节点会怎样？如果以后要把数据搬到另一台机器，又该怎么办？**

> **源码基线**：Redis **7.2.0**。文中的“真实源码节选”来自这一版本；“教学化简”与“概念伪代码”会单独标注。实验命令和输出是可复现步骤与**预期行为**，并非本文编写时已运行的结果。

## 1. 主从复制为什么没有解决容量问题？

先回忆上一章建立的结构：

```text
                  应用程序
                     |
                 写入 INCR
                     |
                     v
                 主节点 M
                /       \
               v         v
            副本 R1    副本 R2
```

副本能保存主节点的数据副本，在故障时作为接管候选者。但在普通的主从复制方案下，**主节点仍然需要容纳它负责的数据集，并承担正常写入**。

比如，假设 M 能够为 Redis 分配的内存预算是 8 GiB，业务数据及其内部管理开销持续增长，已经无法继续满足这个预算。增加两个同样大小的副本，并不意味着主节点突然可以保存 24 GiB 的不同数据：副本主要是在复制已有数据，而不是把这些 Key 分成几份。

这时我们缺少的不是另一份备份，而是另一种能力：

**让不同主节点各自管理不同的一部分 Key，使每个主节点不必保存整个数据集。**

这种把数据拆开、放到不同存储节点上的方式，就叫**数据分片（Sharding）**。

在引入 Redis Cluster 之前，我们先尝试自己做一次最简单的分片。

## 2. 最简单的分片：让应用程序决定数据放哪里

假设有三台彼此独立的 Redis：A、B、C。

一种直觉实现是对活动 ID 取模：

```python
# 概念伪代码：这不是 Redis Cluster 的实际槽算法
nodes = ["A", "B", "C"]

def choose_node(activity_id: int):
    return nodes[activity_id % len(nodes)]

# activity 1001 -> 某台节点
# activity 1002 -> 另一台节点
```

现在，每个活动的数据可以存到不同节点：

```text
应用程序
   |
   +-- activity:1001:views --> 节点 C
   +-- activity:1002:views --> 节点 A
   +-- activity:1003:views --> 节点 B
```

这个设计真的能工作。对每个 Key 而言，客户端只要使用相同的规则，就能找到对应的节点。多台主节点也能够分别存放不同的数据。

可是，业务增长后，我们又买来一台新机器 D。

如果直接把规则改成 `activity_id % 4`，大量已有活动的计算结果都会变化。例如，一个以前属于 C 的活动，现在可能被计算到 D。

但数据**并不会因为计算规则改变而自动搬家**。如果应用立刻按新规则访问，就可能在新节点上得到空值，而旧数据仍然留在原节点。

我们的设计遇到了一个新的矛盾：

**希望增加或减少机器时，能调整数据分布；又不希望节点数量变化直接改变所有 Key 的定位规则。**

我们需要在“Key”和“节点”之间增加一层相对稳定的映射。

## 3. 为什么在 Key 和节点之间增加哈希槽？

先不使用 Redis 的术语。想象我们预先划出固定数量的编号桶：

```text
Key -----------------> 编号桶 -----------------> Redis 节点
activity:1001:views      37                     A
activity:1002:views      84                     B
activity:1003:views      15                     C
```

现在不再计算“Key 应该属于三台机器中的哪台”，而是分两步：

1. 先由 Key 计算一个**固定范围的编号**。
2. 再查询这个编号当前由哪台机器负责。

当 D 加入时，只需把**一部分编号桶的归属**从 A、B、C 调整为 D，并迁移那些桶里的实际数据。Key 到编号桶的计算规则无需改变。

Redis Cluster 采用的就是这一思路，只是它把这些桶称为**哈希槽（hash slot）**。

在 Redis 7.2.0 的 `src/cluster.h` 中，槽数量被定义为：

**真实源码节选：**

```c
#define CLUSTER_SLOTS 16384
```

也就是说，Redis Cluster 将 Key 映射到 **0～16383** 的某个槽位。

例如，一个简单示意分配是：

```text
             16384 个哈希槽

  0               5500 5501          11000 11001        16383
  |------------------| |------------------| |------------------|
          A                    B                    C

                     节点 D 加入
                          |
                          v
               从 A/B/C 迁出部分槽
                     并迁移数据
```

> 图中的范围仅用于解释槽与节点的关系，不代表我们后面实际创建的实验集群一定采用这些边界。

这种设计有一个重要收益：**增加节点时，不需要修改 Key 的哈希规则；主要调整槽归属和对应的数据位置。**

但这里还留下两个必须弄清楚的问题：

- Key 怎样计算自己属于哪个槽？
- 节点怎样知道哪个槽归谁负责？

接下来就沿着一次 `INCR activity:1001:views` 请求进入源码。

## 4. 一个 Key 怎样算出自己的槽？

先看 Redis Cluster 对外提供的命令：

```redis
CLUSTER KEYSLOT activity:1001:views
```

它返回这个 Key 所属的哈希槽编号。槽编号不依赖当前集群有几台主节点。

对应源码在 `src/cluster.c` 的 `keyHashSlot()`。

**真实源码节选（仅展示没有有效哈希标签时的基本路径）：**

```c
if (s == keylen) return crc16(key,keylen) & 0x3FFF;
```

这里有两个关键点。

第一，Redis 使用 **CRC16** 计算哈希值。

第二，`0x3FFF` 的二进制低 14 位都是 1，所以这个按位与操作等价于保留低 14 位，得到的编号在 0～16383 之间。对于 CRC16 的结果，也可理解为对 16384 取模。

以本章的活动计数 Key 为例：

```text
activity:1001:views
        |
        v
      CRC16
        |
        v
     低 14 位
        |
        v
      某个槽位
```

值得注意的是，**Redis Cluster 并不是对节点使用简单的 `hash(key) % 节点数量`，也不是在这里直接采用一致性哈希环**。它先计算固定编号空间中的槽，再查看槽到节点的映射。

但 `keyHashSlot()` 中还有一个分支，为什么要特别寻找 `{` 和 `}` 呢？

因为我们还会遇到一个新的场景：同一个活动的访问次数和活动状态，有时需要由同一条命令一起操作。

如果两个 Key 被分散到不同主节点，单节点的操作就无法直接覆盖它们。Redis 因此支持在 Key 中指定一个**哈希标签（hash tag）**。

例如：

```redis
CLUSTER KEYSLOT activity:{1001}:views
CLUSTER KEYSLOT activity:{1001}:status
```

这两个 Key 都使用有效标签 `{1001}`，因此计算哈希时都只取 `1001` 作为输入，结果**必然是同一个槽**。

Redis 7.2.0 对字符串 `1001` 的该算法结果为槽 **15391**。这可以由下面的标准库计算复核：

```python
import binascii

slot = binascii.crc_hqx(b"1001", 0) & 0x3FFF
print(slot)  # 15391
```

这并不意味着所有带大括号的 Key 都会自动分布到一起：**只有按规则提取出来的标签内容相同，才保证落在同一个槽**。例如 `{}` 是空标签，Redis 会退回对完整 Key 计算哈希。

到这里，我们只知道 Key 属于哪个槽，仍然不知道应该把命令发给哪个主节点。

## 5. 谁保存“槽属于哪台节点”的对应关系？

如果每次接到命令都要遍历所有机器询问“槽 15391 在谁那里”，那么读写会增加不必要的网络通信。

更直接的办法，是让集群节点在内存中维护槽归属信息。

Redis 7.2.0 的 `src/cluster.h` 中，`clusterState` 包含一张这样的映射表：

**真实源码节选：**

```c
clusterNode *slots[CLUSTER_SLOTS];
```

可以先把它理解为一张数组：

```text
slots[0]     -> 主节点 A
slots[1]     -> 主节点 A
...
slots[15391] -> 主节点 C
...
slots[16383] -> 主节点 C
```

这张表不存储 Key 的值，而是存储**槽的负责节点**。

在 `src/cluster.c` 的 `clusterAddSlot()` 中，给某个节点分配一个槽时，有一条直接对应的语句。

**真实源码节选：**

```c
server.cluster->slots[slot] = n;
```

其中 `slot` 是槽编号，`n` 是对应的 `clusterNode` 指针。该函数还会维护节点自身记录的槽位集合，并防止把已经分配的槽直接重复分配。

因此，一个节点要定位 `activity:{1001}:views`，可以先算出槽 15391，再从 `slots[15391]` 取得目前负责的节点。

但这又产生一个实际问题。

**如果客户端连接的节点不是槽 15391 的所有者，会发生什么？**

一个可能的方案是让收到请求的节点替客户端去其他节点读取数据，然后再转发结果。

不过 Redis Cluster 的普通请求路由没有采用这种透明代理方式；它选择把目标节点告诉客户端，让客户端直接重新发起请求。

于是，我们要引入下一项机制：**重定向**。

## 6. 命令发错节点时，为什么返回 MOVED？

上一章已经研究过普通命令执行路径：

```text
客户端请求 -> processCommand() -> call() -> INCR 对应处理函数
```

现在，Cluster 模式下需要在执行 `INCR` 之前多做一件事：判断当前节点是否应该处理这个 Key。

在 `src/server.c` 的 `processCommand()` 中，可以找到路由检查入口：

**真实源码节选：**

```c
clusterNode *n = getNodeByQuery(c,c->cmd,c->argv,c->argc,
                                &c->slot,&error_code);
```

这个函数位于 `src/cluster.c`。它会从命令中找出 Key，计算槽，然后查表确定应该由哪个节点处理。

**真实源码节选（`getNodeByQuery()` 中的关键一步）：**

```c
n = server.cluster->slots[slot];
```

现在设想：客户端把 `INCR activity:{1001}:views` 发给主节点 A，但槽 15391 实际属于 C。

Redis 在普通稳定槽的路径中，不会在 A 上直接执行这次递增，而是标记需要重定向。

**真实源码节选：**

```c
if (n != myself && error_code) *error_code = CLUSTER_REDIR_MOVED;
```

随后，`processCommand()` 调用 `clusterRedirectClient()` 构造回复。

客户端看到的原始错误类似：

```text
-MOVED 15391 172.18.0.4:7002
```

这里的 IP 和端口**只是演示格式**，真实地址取决于实验环境和节点分配。

这条回复的意思是：

- `MOVED`：当前节点不是该槽的正确处理节点。
- `15391`：当前 Key 的哈希槽编号。
- `172.18.0.4:7002`：应该重新发送请求的目标节点地址（示例）。

重要的是：**Redis 并没有在 A 上代替 C 执行命令。**

客户端需要跟随提示，向目标节点重新发送请求。

```text
客户端
   |
   | INCR activity:{1001}:views
   v
节点 A
   |
   | 计算槽 15391
   | 发现所属节点是 C
   v
返回 MOVED 15391 C
   |
   v
客户端重新连接 C
   |
   | INCR activity:{1001}:views
   v
节点 C 执行 INCR
```

这样，原来的一条 Redis 命令执行路径，就多了一个**执行前的归属检查**。

而这也说明使用 Redis Cluster 时，客户端最好能够理解集群拓扑和重定向协议。

普通客户端只负责向单个地址发送命令；Cluster-aware 客户端还需要处理槽到节点的映射变化。

## 7. 实验：用六个 Redis 进程创建集群

现在把刚才的设计落到一个可复现的环境中。

我们使用 **3 个主节点 + 3 个副本节点**。每个主节点负责一部分槽，每个主节点拥有一个副本作为故障接管候选者。

> 本实验只在独立 Docker 网络内运行，不向宿主机发布集群端口。Redis Cluster 节点互相通信既需要客户端服务端口，也需要集群总线端口（默认在服务端口基础上加 10000）。在 Docker NAT/端口重映射环境里，集群对外公布的地址可能无法从宿主机直接访问，因此后续命令都使用 `docker exec` **在同一个 Docker 网络里**执行。不建议直接复制到生产环境。

### 7.1 启动六个独立实例

```bash
# 使用一个隔离的 Docker bridge 网络
docker network create redis-cluster-lab

# 六个容器都连接到同一个网络，无需发布宿主机端口
for port in 7000 7001 7002 7003 7004 7005; do
  docker run -d \
    --name "rc-$port" \
    --network redis-cluster-lab \
    redis:7.2.0 \
    redis-server \
    --port "$port" \
    --bind 0.0.0.0 \
    --protected-mode no \
    --cluster-enabled yes \
    --cluster-config-file nodes.conf \
    --cluster-node-timeout 5000 \
    --appendonly no \
    --save ""
done
```

这里的 `--protected-mode no` 仅用于隔离的本地 Docker 实验网络，且未发布任何宿主机端口。生产环境不能以关闭保护模式代替网络隔离、访问控制和认证。

每个 Redis 都开启了 Cluster 模式，但此时六个实例还没有自动组成一个集群。

### 7.2 把节点组成一个集群

```bash
docker exec rc-7000 redis-cli --cluster create \
  rc-7000:7000 \
  rc-7001:7001 \
  rc-7002:7002 \
  rc-7003:7003 \
  rc-7004:7004 \
  rc-7005:7005 \
  --cluster-replicas 1 \
  --cluster-yes
```

`--cluster-replicas 1` 表示每个主节点安排一个副本；`--cluster-yes` 表示自动接受命令行工具的集群布局确认提示。

成功时，输出末尾通常包含类似信息：

```text
[OK] All 16384 slots covered.
```

这句话不是说六个节点都存着同样的数据，而是表示 0～16383 的每个槽都已经分配给某个负责的主节点。

查看集群状态：

```bash
docker exec rc-7000 redis-cli -p 7000 CLUSTER INFO
```

正常完成集群初始化并稳定后，应该能看到：

```text
cluster_state:ok
cluster_slots_assigned:16384
cluster_slots_ok:16384
```

如果暂时不是 `ok`，应检查所有实例是否正常启动、容器间是否能连通，以及集群创建命令是否成功，而不是直接假设节点已就绪。

再查看槽的分配：

```bash
docker exec rc-7000 redis-cli -p 7000 CLUSTER SLOTS
```

具体的槽范围、容器 IP、主从配对和节点 ID 由实际创建结果决定，不应将示例 IP 写死到程序里。

### 7.3 从客户端亲眼看到槽和请求路由

先计算活动计数的槽位：

```bash
docker exec rc-7000 redis-cli -p 7000 \
  CLUSTER KEYSLOT 'activity:{1001}:views'
```

预期结果：

```text
15391
```

随后用**不自动处理重定向**的命令写入：

```bash
docker exec rc-7000 redis-cli -p 7000 \
  SET 'activity:{1001}:views' 100
```

这里有两种可能：

- 如果 rc-7000 刚好负责槽 15391，返回 `OK`。
- 如果槽 15391 由其他主节点负责，返回 `MOVED` 错误和目标地址。

两种结果都正确。为了稳定复现重定向，可以先用 `CLUSTER SLOTS` 查出槽 15391 的实际所有者，再挑选**另一台主节点**发出上面的 `SET`。

接下来使用具备 Cluster 跳转功能的 `redis-cli -c`：

```bash
docker exec rc-7000 redis-cli -c -p 7000 \
  SET 'activity:{1001}:views' 100

docker exec rc-7000 redis-cli -c -p 7000 \
  INCR 'activity:{1001}:views'

docker exec rc-7000 redis-cli -c -p 7000 \
  GET 'activity:{1001}:views'
```

预期三条命令依次完成，最终 `GET` 的结果为：

```text
101
```

如果最初连接的节点不是槽的所有者，`redis-cli -c` 通常还会打印重定向提示。这里能验证的是：**业务上仍然使用熟悉的 `SET` / `INCR` / `GET`，但客户端需要按照槽归属找到真正负责执行命令的节点。**

> 这里使用的是多个独立的 `redis-cli` 进程。每次调用都会创建新连接，因此不能把它当作“某个长连接已经缓存所有槽拓扑”的验证。真正的 Cluster 客户端通常会维护和更新槽到节点的映射，避免每个请求都绕一次错误节点。

## 8. 为什么两个 Key 不一定可以一起操作？

到这里，我们已经知道集群会把 Key 分散到不同主节点。

现在业务增加一个小需求：创建活动时，希望同时记录访问次数和活动状态。

```redis
MSET activity:1001:views 0 activity:1001:status open
```

在单实例 Redis 中，这条命令可以直接操作两个 Key。

但在 Cluster 中，如果这两个 Key 被计算到不同的槽，命令无法由同一个主节点按照普通多 Key 命令的语义一次完成。

此时 Redis 会返回类似错误：

```text
CROSSSLOT Keys in request don't hash to the same slot
```

这是此前“按 Key 分片”设计带来的直接限制。

我们不能简单地认为：**既然集群里有多台 Redis，就能把任意跨节点操作自动变成一个原子操作。**

现在，回头看刚才引入的哈希标签：

```redis
CLUSTER KEYSLOT activity:{1001}:views
CLUSTER KEYSLOT activity:{1001}:status
```

两个 Key 都使用 `{1001}`，因此属于同一个槽。

这样，下面的命令就具备在同一个主节点上执行的槽条件：

```bash
docker exec rc-7000 redis-cli -c -p 7000 \
  MSET 'activity:{1001}:views' 0 'activity:{1001}:status' open
```

再分别读取：

```bash
docker exec rc-7000 redis-cli -c -p 7000 \
  GET 'activity:{1001}:views'

docker exec rc-7000 redis-cli -c -p 7000 \
  GET 'activity:{1001}:status'
```

预期分别为 `0` 和 `open`。

为什么这个限制存在？我们去源码中确认。

在 `getNodeByQuery()` 中，Redis 会从命令参数中识别 Key，并逐一计算哈希槽。如果一个普通多 Key 请求涉及不同槽，就会返回 `CLUSTER_REDIR_CROSS_SLOT`。

这里真正维护的是一个清楚的边界：**在当前稳定槽的正常执行路径中，一条操作多个 Key 的命令需要满足同槽要求。**

但是，为所有有关联的 Key 添加同一个标签也不是没有代价。大量热点 Key 如果全部进入同一槽，反而可能让相应主节点成为热点；选择哈希标签应以确实需要同槽操作的数据为边界，而不是让整个业务都使用一个标签。

## 9. 增加节点后，Key 怎样迁移而不丢失？

我们前面已经解决了两个问题：

1. 使用固定槽编号，避免节点数量变化直接修改 Key 到槽的映射算法。
2. 使用槽归属表和 `MOVED`，让请求到达正确主节点。

但只改变槽归属表还不够。

假设槽 15391 原来由主节点 C 负责，里面保存了：

```text
activity:{1001}:views = 101
```

现在我们准备把这个槽迁给新节点 D。

如果简单地让所有节点把 `slots[15391]` 改成 D，会发生什么？

客户端按新映射向 D 读取时，D 可能还没有对应的 Key；真实数据仍保存在 C。

所以扩容需要两个动作：

**改变槽归属，还要迁移该槽包含的实际 Key。**

而在迁移过程中，还可能有客户端不断读写这些 Key。我们不能假设所有客户端都会先停下来，等待搬迁结束。

Redis Cluster 因此引入了槽迁移的中间状态：

- **MIGRATING**：原节点正在把某个槽交出去。
- **IMPORTING**：目标节点正在接收某个槽。

在 Redis 7.2.0 的 `clusterState` 中可以找到它们：

**真实源码节选 — `src/cluster.h`：**

```c
clusterNode *migrating_slots_to[CLUSTER_SLOTS];
clusterNode *importing_slots_from[CLUSTER_SLOTS];
```

这些字段没有替代普通槽归属表，而是额外标记**正在进行的迁移**。

在 `src/cluster.c` 的 `clusterCommand()` 中，`CLUSTER SETSLOT ... MIGRATING` / `IMPORTING` 会分别设置这些状态；数据搬迁则可以借助 `MIGRATE` 命令完成。

现在考虑一个微妙的情况：

- 迁移刚开始时，Key 还在 C。
- 搬迁过程中，一部分 Key 已经移动到 D。
- 整个槽的正式所有者此时仍可能是 C。

如果客户端继续向 C 请求已搬走的 Key，就需要让它**临时改去 D**，但又不能提前宣布整个槽以后都归 D。

因此，Redis Cluster 提供另一种重定向：`ASK`。

### 9.1 ASK 为什么不同于 MOVED？

在稳定状态下：

```text
请求发给错误节点
       |
       v
MOVED：这个槽应由另一节点处理
       |
       v
客户端可以更新槽归属缓存
```

而在迁移状态下：

```text
请求到达原槽所有者 C
       |
       | Key 已不在 C，槽正在迁移
       v
ASK：这次请求暂时去 D
       |
       v
客户端向 D 发送 ASKING
       |
       v
客户端向 D 重发原命令
```

`ASK` 通常是**针对当前请求的临时跳转**，不表示槽的最终所有权已经变更；`MOVED` 则告诉客户端当前完整的槽归属信息发生了变化，应修正路由。

这不是任意命名的约定，可以在 Redis 7.2.0 的 `getNodeByQuery()` 中找到对应逻辑。

**真实源码节选（迁出节点缺少所请求 Key 的相关分支）：**

```c
*error_code = CLUSTER_REDIR_ASK;
return server.cluster->migrating_slots_to[slot];
```

目标节点如果处于导入状态，还会检查客户端是否带有 `ASKING` 标志，符合条件才允许处理这次导入请求。

这里终于可以解释：为什么 Redis 没有只使用一种重定向？

因为**稳定路由变化**和**搬迁途中某个 Key 的暂时位置变化**是两件不同的事。

### 9.2 如何实际观察槽迁移？

先从前面的实验集群中查看主节点 ID：

```bash
docker exec rc-7000 redis-cli -p 7000 CLUSTER NODES
```

然后选择两个**不同的主节点**，记下它们的 40 字符节点 ID。用实际 ID 替换以下占位符：

```bash
SRC_ID="<实际源主节点ID>"
DST_ID="<实际目标主节点ID>"
```

接着，从源主节点向目标主节点迁移一个槽：

```bash
docker exec rc-7000 redis-cli --cluster reshard rc-7000:7000 \
  --cluster-from "$SRC_ID" \
  --cluster-to "$DST_ID" \
  --cluster-slots 1 \
  --cluster-yes
```

注意这里 **`--cluster-slots 1` 表示迁移一个槽，并没有指定槽 15391**。命令行工具会选择源主节点的某个槽进行迁移。不要宣称它一定会移动我们之前写入的 `activity:{1001}:views`。

迁移完成后，可以检查：

```bash
docker exec rc-7000 redis-cli --cluster check rc-7000:7000
```

再查看：

```bash
docker exec rc-7000 redis-cli -p 7000 CLUSTER SLOTS
```

目标是观察**槽归属范围变化**，并确认集群仍能覆盖全部 16384 个槽。因为这里仅迁移一个槽，且实验请求并不持续写入目标槽，所以不能据此宣称已经复现了真实高负载情况下的 `ASK` 竞态；那需要有针对性的并发读写与迁移时序实验。

如果要继续深入 `ASK` 的行为，可以使用 `CLUSTER SETSLOT ... MIGRATING/IMPORTING` 配合 `MIGRATE` 构造可控的单槽迁移，但这种实验会修改底层集群拓扑，应在独立临时集群中进行，且需要先明确源和目标节点的实际槽归属及 Key 状态。

## 10. 为什么 Redis 需要知道“一个槽里有哪些 Key”？

既然移动的是槽，Redis 就必须找到该槽包含的实际 Key。

一个朴素做法是：每迁移一个槽，都扫描数据库中的全部 Key，算一遍它们属于哪个槽。

但如果数据库里有大量 Key，这种遍历代价就会很高。

因此，Redis 7.2.0 在集群模式下还维护**槽到 Key 的索引**。

对应源码仍在 `src/cluster.c`，包括：

- `slotToKeyInit()`：初始化槽到 Key 的索引。
- `slotToKeyAddEntry()`：当字典新增条目时，把该条目纳入所属槽的索引。
- `slotToKeyDelEntry()`：删除键时，维护索引。
- `countKeysInSlot()`：查询一个槽当前有多少 Key。

这意味着 Redis 不只是维护“槽 → 节点”，还要让本节点能够有效找到“槽 → Key 集合”。

两个映射有不同职责：

```text
                       activity:{1001}:views
                                  |
                                  | keyHashSlot()
                                  v
                              槽 15391
                              /      \
                             /        \
                            v          v
              槽 → 负责节点            槽 → 本地 Key
                  (路由)                 (迁移)
```

前者回答“请求应该交给谁”；后者回答“如果要移动这个槽，本地有哪些实际数据需要处理”。

这时才有必要看到这第二层索引，因为我们已经遇到了真实的槽迁移问题。

## 11. 节点之间怎样知道彼此的槽信息？

我们已经知道每个节点可以维护槽到节点的表。

但如果节点 A 认为槽 15391 属于 C，而节点 B 认为它属于 D，客户端就可能在不同节点间反复跳转。

因此，Cluster 还需要让各节点交换自己的状态和槽归属信息。

Redis 节点通过集群总线进行节点间通信。在 `src/cluster.c` 中，可以找到：

- `clusterCron()`：周期性处理集群维护工作。
- `clusterSendPing()`：发送节点间的 PING 类消息。
- `clusterProcessPacket()`：接收并处理集群总线消息。
- `clusterUpdateState()`：根据槽覆盖等条件更新集群状态。

节点会交换自己的角色、配置纪元和槽声明等信息。配置变更经过传播后，集群成员逐渐更新各自的槽映射。

但现在还不必把 Cluster 的完整故障检测、选举、配置纪元冲突处理都展开。那些机制涉及另一个主问题：**当某个负责一批槽的主节点真正不可用了，哪个副本有资格接替它？**

这一章的目的，是先建立正确的数据路由模型；与上一章 Sentinel 对比时，要特别注意：

- Sentinel 主要监督一组主从实例并协调主节点故障转移，本身**不负责把一个 Key 空间切成多个主节点存储**。
- Redis Cluster 则把数据分给多个主节点，每个主节点通常仍可配置副本；Cluster 也具有自己的故障检测和故障转移机制，**不需要在 Cluster 外再用 Sentinel 进行同一组节点的故障转移**。

两者并不是完全叠加后才能工作的两个层次，而是解决需求范围不同的部署模式。

## 12. Redis Cluster 解决了什么，没有解决什么？

通过本章的因果推导，我们已经看到 Redis Cluster 带来的能力：

| 已经遇到的限制 | 新机制 | 直接效果 |
| --- | --- | --- |
| 单个主节点装不下全部不同 Key | 多主节点数据分片 | 不同 Key 分散存储 |
| 节点数量变化使取模规则混乱 | 固定的 16384 个哈希槽 | Key 到槽的规则保持稳定 |
| 客户端不知道应访问哪台节点 | 槽归属表与 `MOVED` | 找到实际负责命令的节点 |
| 一条命令需要操作多个相关 Key | 哈希标签 | 使相关 Key 落入同一槽 |
| 扩容要改变数据放置位置 | 槽迁移与 `ASK` | 在迁移过程中维持请求路由 |

但 Redis Cluster 并不会自动保证所有应用场景都变得更简单。

首先，**一条涉及不同槽的普通多 Key 命令，仍受跨槽限制**。使用哈希标签能够把相关 Key 放到一起，但这也意味着这些 Key 无法再被均匀地分散到多个主节点。

其次，**分片不等于所有写入都自动获得强一致性**。Redis Cluster 的主从复制仍可能存在异步复制窗口，发生主节点故障与切换时，最近成功返回的写入仍有丢失风险。

再次，**增加节点并不代表数据会自动按照理想比例重新平衡**。实际扩容需要安排槽归属调整和数据迁移，具体可以由管理命令与运维工具执行。

最后，**一个热点活动的所有相关计数如果集中在同一个槽，就仍然可能形成单主节点热点**。集群可以增加总体容量，但不能把一条普通 `INCR` 命令自动拆成多个节点共同执行。

所以，Redis Cluster 解决的是**Key 空间如何分配到多个主节点，以及如何管理这种分布**，不是一个对业务透明的“无限大单机 Redis”。

## 13. 最后才回看本章完整执行路径

最开始，我们只有一条熟悉的命令：

```redis
INCR activity:{1001}:views
```

在单实例 Redis 中，上一章已经建立了它的执行路径。

启用 Cluster 模式后，真正新增的判断发生在执行 `INCR` 之前：

```text
                  客户端发出 INCR
                         |
                         v
                  processCommand()
                         |
                         v
                  getNodeByQuery()
                         |
                         | 识别 Key
                         v
                    keyHashSlot()
                         |
                         v
                       槽号
                         |
                         v
              cluster->slots[slot]
                         |
              +----------+-----------+
              |                      |
              v                      v
         当前节点负责             其他节点负责
              |                      |
              v                      v
           call()               MOVED 重定向
              |                      |
              v                      v
       incrDecrCommand()          客户端重试
              |                      |
              v                      v
         更新本地数据             正确的主节点
```

如果槽正在迁移，路由判断还可能涉及 `MIGRATING`、`IMPORTING`、`ASK` 与 `ASKING`，由临时重定向把单次请求导向数据当前可被处理的位置。

这张图有一个很重要的特点：**我们以前学过的命令实现并没有被推翻。**

Cluster 为命令执行增加的是**数据归属判断与分布式路由**。找到正确节点之后，实际的 `INCR` 仍然通过这个节点的普通命令实现修改它管理的数据。

这也解释了为什么先理解单实例 Redis 的网络、命令执行和数据库存储，再研究 Cluster，是一条更自然的源码阅读路径。

## 14. 实验清理与验证边界

本章的 Docker 集群用于学习，不包含生产部署所需的持久化、鉴权、网络策略和高可用运维配置。

实验结束后，可删除六个容器和专用网络：

```bash
for port in 7000 7001 7002 7003 7004 7005; do
  docker rm -f "rc-$port"
done

docker network rm redis-cluster-lab
```

实验分别能验证：

1. `CLUSTER KEYSLOT` 返回固定槽编号。
2. 普通客户端发错节点时可能收到 `MOVED`。
3. `redis-cli -c` 可以跟随重定向完成操作。
4. 跨槽多 Key 命令可能报 `CROSSSLOT`，有效哈希标签能够满足同槽要求。
5. 迁移槽后，可以观察槽分配的变化。

这些验证**不能证明**集群没有故障丢失、迁移全程无抖动，或者跨节点操作具有原子事务语义。实验输出和集群拓扑应以本地实际运行结果为准。

## 本章回顾：从一台主节点走向分片

我们并没有一开始就介绍 Redis Cluster 的所有命令，而是让设计随着新的约束逐步生长：

```text
主从复制已经能保存副本
              |
              | 但不同数据仍集中在单个主节点
              v
            数据分片
              |
              | 直接按节点数取模，扩容时映射变化过大
              v
          固定哈希槽
              |
              | Key 能算出槽，但不知道节点
              v
         槽到节点的映射
              |
              | 请求可能发到错误节点
              v
          MOVED 重定向
              |
              | 多 Key 命令可能跨槽
              v
           哈希标签
              |
              | 槽重新分配时数据还要搬迁
              v
      MIGRATING / IMPORTING
              |
              | 搬迁过程中需要临时请求路径
              v
          ASK + ASKING
```

现在，我们已经可以清楚区分三个概念：

- **Key**：业务实际保存的数据条目。
- **哈希槽**：Redis Cluster 为 Key 计算出的、范围固定的逻辑编号。
- **节点**：当前负责保存某些槽中的数据并执行命令的 Redis 实例。

**Key 到槽的算法是稳定的；槽到节点的映射可以变化。**

正是这层间接映射，让 Redis Cluster 能在多个主节点之间分配数据，并在集群扩缩容时调整数据位置。

---

## 下一步：从系统主干走向数据结构专题

从第一条 `SET` 命令开始，我们已经走过 Redis 这个数据服务的主要运行问题：请求如何进入服务端、如何被执行、并发操作怎样保持正确、过期数据怎样清理、内存怎样受控、数据怎样恢复、主节点怎样被副本接管，以及 Key 怎样分布到不同节点。

但到目前为止，我们实际深入的业务数据大多只是一个整数计数。

如果接下来要保存的不再是一个数字，而是**一个活动包含的多个字段**，继续把所有字段序列化成一个大字符串就会暴露新的问题：只修改活动标题时，为什么要读写整个对象？

这时，才自然有必要研究 Redis 的 **Hash**，以及为什么 Redis 并不只有一种 Value 组织方式。

**下一专题：《Redis 为什么需要不同的数据结构？从一个活动对象开始》**。

我们将用新的、明确的数据组织问题开启专题，而不是把 Hash、List、Set、ZSet 当作没有上下文的名词列表。

---

## 源码参考（Redis 7.2.0）

- [`src/cluster.h`](https://github.com/redis/redis/blob/7.2.0/src/cluster.h)：`CLUSTER_SLOTS`、`clusterState` 与槽归属、迁移状态结构。
- [`src/cluster.c`](https://github.com/redis/redis/blob/7.2.0/src/cluster.c)：`keyHashSlot()`、`clusterAddSlot()`、`getNodeByQuery()`、`clusterRedirectClient()`、`clusterCommand()`、`clusterCron()`、`migrateCommand()`、`slotToKeyAddEntry()`。
- [`src/server.c`](https://github.com/redis/redis/blob/7.2.0/src/server.c)：`processCommand()` 中的 Cluster 路由检查。
- [Redis Cluster 官方规格说明](https://redis.io/docs/latest/operate/oss_and_stack/reference/cluster-spec/)：哈希槽、`MOVED`、`ASK`、迁移状态与配置传播。
- [Redis Cluster 部署与扩缩容](https://redis.io/docs/latest/management/scaling/)：集群创建、槽分配、重分片和网络要求。
- [`CLUSTER KEYSLOT` 官方说明](https://redis.io/docs/latest/commands/cluster-keyslot/)：调试 Key 的槽编号及哈希标签。
