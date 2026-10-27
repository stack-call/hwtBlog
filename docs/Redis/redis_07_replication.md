---
title: "第七章：Redis 单实例故障后怎么办？从主从复制到数据同步"
sidebar_label: "07 - 主从复制与数据同步"
sidebar_position: 8
description: "沿用活动访问计数，从单实例故障引出副本需求，在 Redis 7.2.0 源码中追踪 REPLICAOF、PSYNC、全量同步、命令传播与部分重同步，厘清复制与自动故障转移的边界。"
tags:
  - Redis
  - 源码精读
  - 主从复制
  - PSYNC
  - 高可用
---

# 第七章：Redis 单实例故障后怎么办？从主从复制到数据同步

上一章，我们研究了 RDB 和 AOF。它们回答的是：**Redis 退出后，如何利用此前保存的数据恢复一个实例？**

但活动系统又遇到了新的情况：即使数据最终能够恢复，Redis 进程退出到重新启动之间，应用程序仍然无法连接它。假设活动 `1001` 正在进行，访问次数保存在：

```text
activity:1001:views = 101
```

Redis 突然不可用。现在，用户访问活动页面时，谁来接收新的计数请求？

我们暂时不尝试“一键自动切换”，也不提前介绍 Sentinel。先解决更小的一个问题：**如果主实例出了故障，能否提前准备一份持续更新的数据副本？**

这一章将只沿着这个需求前进：复制现有数据 → 继续同步后续修改 → 断线后补齐缺失修改 → 认识复制仍不能自动完成的事。

> **源码基线**：Redis **7.2.0**，以 [`src/replication.c`](https://github.com/redis/redis/blob/7.2.0/src/replication.c) 为主。文中的源码短片段均针对这一版本核对；标注为“教学化简”的代码只帮助理解，不应冒充真实实现。实验命令为可复现步骤，展示的是**预期行为**，不声称已在编写环境实际运行。实验使用无密码的隔离 Docker 网络、仅发布到宿主机回环地址，不能照搬到生产环境。

## 1. 为什么有 RDB/AOF，还要另一台 Redis？

回想上一章，AOF 可能记录类似的写入：

```redis
SET activity:1001:views 100
INCR activity:1001:views
```

主实例重新启动时，可以根据持久化数据恢复状态。但恢复过程本身需要时间；如果机器或磁盘损坏，数据文件甚至可能无法在原位置读取。

我们可以想到一个最朴素的备份办法：每隔一段时间，把主实例的 RDB 拷贝到另一台机器。出现问题时，再用备份启动新的 Redis。

这比什么也不做更好，但它有明显缺口。假设：

```text
10:00  备份中保存的计数为 100
10:01  主实例上的计数增长到 125
10:02  主实例发生故障
```

备份机器上仍然只有 `100`。它既不知道后来的二十五次递增，也不知道故障发生前最新的状态。

因此，我们缺少的不是另一份**偶尔更新**的数据，而是一份**持续跟随主实例变化**的数据。

这时才有必要引入“副本（replica）”：

```text
             写入
活动服务 ───────────> Redis 主实例
                         |
                         | 复制数据与后续修改
                         v
                    Redis 副本
```

注意：这张图中**仍由主实例接收业务写入**。副本的首要职责，是按复制协议维护一份与主实例尽量一致的数据集。我们还没有解决谁检测故障、谁决定切换、应用如何找到新主实例。

## 2. 最小实验：让两个 Redis 实例保存相同的计数

为了不影响前几章可能运行在 `6379` 端口的实验，这里使用宿主机端口 `6390` 和 `6391`，并通过 Docker 内部网络让两个容器互相通信。

首先创建实验网络：

```bash
docker network create redis-repl-lab-net
```

启动主实例（关闭持久化，以便专注观察复制；强制使用**基于磁盘的全量复制**，方便与上一章 RDB 联系起来）：

```bash
docker run -d \
  --name redis-repl-master \
  --network redis-repl-lab-net \
  -p 127.0.0.1:6390:6379 \
  redis:7.2.0 redis-server \
  --save "" \
  --appendonly no \
  --protected-mode no \
  --repl-diskless-sync no \
  --repl-backlog-size 1mb
```

启动第二个实例：

```bash
docker run -d \
  --name redis-repl-replica \
  --network redis-repl-lab-net \
  -p 127.0.0.1:6391:6379 \
  redis:7.2.0 redis-server \
  --save "" \
  --appendonly no \
  --protected-mode no
```

此时它们只是两个独立的 Redis，互相并没有复制关系。先在主实例中写入活动计数：

```bash
docker exec redis-repl-master redis-cli \
  SET activity:1001:views 100
```

然后让第二个实例跟随第一个实例：

```bash
docker exec redis-repl-replica redis-cli \
  REPLICAOF redis-repl-master 6379
```

`REPLICAOF` 返回 `OK`，**不代表全量同步已经完成**；它表示配置复制关系的命令已被接受。可以继续查看：

```bash
docker exec redis-repl-replica redis-cli INFO replication
```

等到副本显示：

```text
role:slave
master_link_status:up
master_sync_in_progress:0
```

再读取：

```bash
docker exec redis-repl-replica redis-cli \
  GET activity:1001:views
```

预期得到 `"100"`。如果刚建立关系就立刻执行 `GET`，则可能由于尚未完成同步而读不到期望值，应先检查 `INFO replication`，不要依赖固定的 `sleep 1`。

我们已经从现象上看到：**第二个 Redis 不需要客户端对它再次执行 `SET`，也可以得到主实例中的数据。**

但问题来了：副本第一次连接时，主实例究竟把什么发送给了它？

## 3. 一条 REPLICAOF 如何启动连接？

此前讲 `SET` 时，我们知道 Redis 会在命令分发后调用对应处理函数。`REPLICAOF` 也不例外。

真实入口位于 `src/replication.c` 的 `replicaofCommand()`。普通配置分支中，核心的一行是：

**真实源码节选（Redis 7.2.0）：**

```c
replicationSetMaster(c->argv[1]->ptr, port);
```

它只负责把“要跟随谁”交给复制子系统。继续进入 `replicationSetMaster()`，会看到它记录 `server.masterhost` 和 `server.masterport`，随后进入连接过程。

其中关键的一步是：

**真实源码节选：**

```c
connectWithMaster();
```

为什么不是在 `REPLICAOF` 命令函数中一直等待同步完成？

因为创建连接、认证、协商能力、请求数据、接收大批量数据可能需要相当长的时间。如果在命令处理函数里持续等待，就会妨碍实例处理其他事件。

我们在第二章已经认识了 Redis 的事件循环：遇到需要等待网络的操作，可以先登记回调，等连接状态变化或数据就绪后再继续。

在 `connectWithMaster()` 中，Redis 创建连接并发起非阻塞连接请求，连接回调指向 `syncWithMaster()`。简化其职责，可写为：

```text
REPLICAOF redis-repl-master 6379
                |
                v
        replicaofCommand()
                |
                v
       replicationSetMaster()
                |
                v
        connectWithMaster()
                |
                v
      连接就绪后进入 syncWithMaster()
```

`syncWithMaster()` 是副本侧的同步握手过程。它先通过 `PING` 检查主实例能否响应，必要时完成认证，随后通过 `REPLCONF` 告知自身信息和能力，最后发送 `PSYNC` 请求。

这不是为了记住一串指令名称，而是因为副本还缺两份信息：**主实例能否接受同步，以及双方是否已有共同的数据历史。**

## 4. 第一次连接：没有共同历史，只能先拿到一份完整数据

对于一个刚加入的副本，它没有主实例的现有数据，也不知道已经执行过多少条写入。仅复制后续的新写命令并不足够。

想象主实例已有：

```text
activity:1001:views = 100
activity:1002:views = 75
```

如果从这一刻起只转发后续 `INCR`，新的副本不知道 `activity:1002:views` 原先是多少，自然无法构建完整状态。

所以第一次同步至少要完成两件事：

1. 获得主实例**某个时间点**的完整数据集。
2. 不漏掉该时间点**之后**继续发生的修改。

第一件事可以复用上一章认识的 RDB；第二件事稍后再说。

在副本的 `slaveTryPartialResynchronization()` 中，如果没有可用于恢复历史的 `cached_master`，它会向主实例发送概念上这样的请求：

```text
PSYNC ? -1
```

这里的 `?` 和 `-1` 表示：副本没有可用的旧复制 ID 与偏移量，不能声称自己可以从先前的位置继续。

主实例收到 `PSYNC` 时，会进入 `syncCommand()`；如果无法部分重同步，就走全量同步路径。

在 `startBgsaveForReplication()` 中，Redis 7.2.0 会根据配置与副本能力，选择把 RDB 生成到磁盘文件，或由子进程直接通过 socket 发送。我们在实验中显式设置了 `--repl-diskless-sync no`，因此对于这次普通全量复制，使用的是**基于磁盘的 RDB 生成与传输路径**。

这一段源码中，磁盘路径对应：

**真实源码节选：**

```c
retval = rdbSaveBackground(req,server.rdb_filename,rsiptr,RDBFLAGS_KEEP_CACHE);
```

这与上一章学过的 `rdbSaveBackground()` 接上了：同一种 RDB 格式不只能用于重启恢复，也能用于**给另一台 Redis 提供数据快照**。

不过，Redis 7.2.0 的 `redis.conf` 默认启用了 `repl-diskless-sync yes`；所以**不能把“全量复制一定先写本地 RDB 文件”当作通用结论**。在无盘复制配置下，RDB 可以由子进程直接发送到副本连接；本章为了便于观察，选择了磁盘路径。

## 5. 只传一份 RDB，为什么仍然会漏数据？

假设主实例在生成快照时，活动计数是 `100`。

```text
时刻 T0：开始生成 RDB，计数为 100
时刻 T1：客户端执行 INCR，计数变为 101
时刻 T2：客户端又执行 INCR，计数变为 102
时刻 T3：副本加载 T0 的 RDB，看到 100
```

如果只传输快照，副本就会落后两次递增。

因此，在制作和传输快照期间，主实例还必须**保存后续产生的写入变化**，等副本加载快照后继续发送。

Redis 并不是在完成全量复制后才开始考虑这些变化。主实例会在设置全量同步状态时，安排后续复制命令进入对应的传播和缓冲流程。

`replicationSetupSlaveForFullResync()` 的一个关键动作是记录全量同步起点的偏移量，并将副本置为等待快照结束的状态。响应中还会告诉副本复制 ID 和起点偏移量，格式是：

```text
+FULLRESYNC <replication-id> <offset>
```

这里的复制 ID 与 offset 不是业务 Key，而是 Redis **复制历史**的标识。

这样，主从同步不再只是“复制一次文件”：

```text
主实例
   |
   | ① 生成某个时点的 RDB
   +------------------------------> 副本加载快照
   |
   | ② 快照产生之后继续接受写入
   |    并积累相应复制流
   +------------------------------> 副本继续应用修改
   |
   | ③ 后续写入持续传播
   +------------------------------> 副本不断追赶
```

这一步解决了一个非常实际的问题：**主实例在同步期间不必为了等副本复制完成，而完全停止处理普通写入。**

但副本完成初始追赶后，后续的每一次 `INCR` 又是怎样传过去的？

## 6. 全量同步完成后，如何持续传播新的写入？

副本已同步至 `100` 后，我们在主实例执行：

```bash
docker exec redis-repl-master redis-cli \
  INCR activity:1001:views
```

预期主实例返回 `101`。稍后从副本 `GET`，也应该能够读到 `101`。

这次不需要重新生成完整 RDB，因为副本已经有了旧数据，只缺新变化。

Redis 在正常写命令执行后的传播路径中，会通过 `src/server.c` 的相关逻辑调用 `replicationFeedSlaves()`。该函数位于 `src/replication.c`，负责把需要传播的命令以 Redis 协议形式追加到复制流及对应的副本输出链路。

**教学化简（不是实际调用代码）：**

```c
// 主实例在命令执行并决定传播后
serialize_write_command();
append_to_replication_stream();
make_data_available_to_replicas();
```

这里最容易误解的一点是：复制的不是“客户端看见的任意请求原文”。Redis 会根据命令执行结果决定传播内容，必要时还会使用不同于原始请求的、能够重现效果的写操作。对普通 `INCR`，我们可以把传播效果理解为让副本也应用相同递增。

副本一侧如何消费这些内容？

全量同步加载完成后，Redis 使用 `replicationCreateMasterClient()` 建立一个代表上游主实例的特殊 `client` 对象，并为它设置熟悉的 `readQueryFromClient` 读取入口。这样的设计复用了前面学过的“从网络解析命令并执行”的路径。

所以，从结构上理解，副本并不需要为每一种 `SET`、`INCR` 重写一套专用的复制执行函数。

```text
主实例执行 INCR
        |
        v
形成需要传播的写命令
        |
        v
    复制连接
        |
        v
副本读取并解析命令
        |
        v
副本更新自己的数据集
```

不过，主实例不必在每次写入后等副本完成执行才回复原客户端。Redis **默认异步复制**。

这也是为什么“主实例 `INCR` 已返回成功”和“副本一定已经看到最新值”不是同一个命题。副本一般会持续追上主实例，但在复制传输与应用存在延迟时，读取副本可能暂时得到旧值。

## 7. 如果副本短暂断线，难道要重新复制全部数据？

目前我们知道：第一次同步可以传 RDB，之后的新修改可以持续传播。

但假设副本在活动计数为 `100` 时短暂断网。断网期间主实例执行：

```text
INCR  -> 101
INCR  -> 102
INCR  -> 103
```

副本重新连接时，仍然是 `100`。如果每次短暂断线都重新传输整个数据库，成本显然过高。

最小的新需求其实是：**副本应该告诉主实例自己追到了哪里；如果主实例还保存着后面的变化，就只补发缺失部分。**

这时才需要引入两个概念：

- **复制 ID（replication ID）**：表明这些偏移量属于哪一段复制历史。
- **复制偏移量（offset）**：标记已产生或已处理到复制流的哪个字节位置。注意它是**字节位置**，不是“第几条业务命令”。

例如，下表只是用于建立直觉的数字：

| 实例 | 复制 ID | 偏移量 |
| --- | --- | ---: |
| 主实例 | R1 | 1200 |
| 副本（断线前） | R1 | 1000 |

它们属于相同复制历史，副本落后了一段字节流。只要主实例还保存着这段内容，就有机会从偏移量 `1001` 开始继续。

Redis 7.2.0 在 `slaveTryPartialResynchronization()` 中，若存在旧的 `cached_master`，会使用旧 ID 和已处理偏移量加一构造 `PSYNC` 请求。

**真实源码节选：**

```c
snprintf(psync_offset,sizeof(psync_offset),"%lld", server.cached_master->reploff+1);
```

注意这也解释了为什么请求的是**下一字节**的位置，而不是“上一次执行的 INCR 编号”。

但还有一个困难：主实例没有无限内存，难道需要永远保存所有历史复制数据吗？

显然不现实。

因此，Redis 需要一段容量有限的近期复制历史。这就是 **replication backlog（复制积压缓冲区）**。

## 8. 复制积压缓冲区：为什么只保留近期历史？

第一次理解 backlog，可以把它想成一个有容量上限的“近期变化记录窗口”：

```text
主实例复制流的总历史：

[很早的数据......][仍保留在 backlog 中的近期数据]
                  ^                              ^
                  |                              |
             最早可补的位置                   当前末尾
```

断线时间短、写入量小，副本所缺的字节还在这个窗口内，就能部分重同步。

断线很久、期间写入量很大，所缺数据已经被清理，就无法仅凭近期记录还原过去的差异，需要重新进行全量同步。

在 Redis 7.2.0 的 `createReplicationBacklog()` 中，Redis 初始化积压缓冲区及其索引，并记录其中第一字节对应的偏移量。

**真实源码节选：**

```c
server.repl_backlog->offset = server.master_repl_offset+1;
```

这里还需要纠正一种常见的旧版本印象：**Redis 7.2.0 的复制积压缓冲区不是必须想象成独立、固定的环形字符数组**。这一版本使用复制缓冲块以及相应索引，在主从复制与 backlog 保留历史之间共享相关数据结构。理解本章时，只需要掌握“保留一段有限字节历史”的语义即可，内部块管理暂时不展开。

真正决定能否补发的判断发生在主实例收到 `PSYNC` 之后。

在 `syncCommand()` 中，Redis 调用 `masterTryPartialResynchronization()` 尝试部分同步。

这个函数至少要确认两件事：

1. 请求携带的复制 ID 是否对应主实例认可的历史（包括特定条件下仍被保留的第二复制 ID）。
2. 请求的偏移量是否落在 backlog 仍然覆盖的范围内。

判断通过后，主实例返回 `+CONTINUE`，使用 `addReplyReplicationBacklog()` 将缺少的字节流交给副本继续应用。

判断不通过时，主实例会转入全量同步，而不是假装丢失的历史还存在。

```text
副本重新连接
     |
     v
PSYNC <旧ID> <下一字节偏移量>
     |
     v
主实例核对复制历史
     |
     +--- ID 不匹配或历史不足 ---> 全量同步
     |
     +--- 历史仍可用 -----------> +CONTINUE
                                      |
                                      v
                                补发缺失字节
                                      |
                                      v
                                 继续实时传播
```

这样，断线重连就不一定意味着重新传输整个数据库。

但也别过度推论：**“存在 backlog”不代表一定能够部分同步**。复制历史不匹配、请求位置超出窗口或主实例重启后相关状态变化，都可能导致全量同步。

## 9. 实验：观察初始全量同步与断线后的追赶

为了让实验有可解释的观察量，我们先看两项统计指标：

```bash
docker exec redis-repl-master redis-cli INFO stats
```

在输出中关注：

```text
sync_full:<数字>
sync_partial_ok:<数字>
sync_partial_err:<数字>
```

其中 `sync_full` 统计全量同步次数，`sync_partial_ok` 统计被接受的部分同步请求，`sync_partial_err` 统计被拒绝的部分同步请求。它们是**累计计数器**，不能只凭某次 `GET` 的值就判断使用了哪种同步方式。

第一次执行 `REPLICAOF` 后，正常情况下可以在主实例日志中观察到全量同步相关信息，例如：

```bash
docker logs redis-repl-master --tail 100
```

不要假设某句日志文本在所有配置下完全一致；更可靠的证据是结合 `INFO stats` 的变化、`INFO replication` 的链路状态和实际读取结果。

现在尝试制造一段受控的短暂网络中断。先确保主从已同步，再执行：

```bash
# 断开副本所在的实验网络，但不停止容器进程
docker network disconnect redis-repl-lab-net redis-repl-replica

# 主实例仍正常接收写入
docker exec redis-repl-master redis-cli INCR activity:1001:views
docker exec redis-repl-master redis-cli INCR activity:1001:views
docker exec redis-repl-master redis-cli INCR activity:1001:views

# 将副本重新接入同一网络
docker network connect redis-repl-lab-net redis-repl-replica
```

随后等待副本重新连接：

```bash
docker exec redis-repl-replica redis-cli INFO replication
```

当 `master_link_status:up` 且 `master_sync_in_progress:0` 时，检查：

```bash
docker exec redis-repl-master redis-cli \
  GET activity:1001:views

docker exec redis-repl-replica redis-cli \
  GET activity:1001:views
```

如果实验开始时是 `100`，断网前已经执行过一次 `INCR` 得到 `101`，然后断网期间又执行三次，那么两个实例最终都应看到 `104`。如果起始计数不同，应该比较**两边最终是否一致**，不要把 `104` 当作硬编码断言。

当复制 ID 保持一致、所缺流仍在 backlog 中时，**预期** `sync_partial_ok` 增加，表示通过部分重同步完成了追赶。实际结果受网络断开时机、连接状态与 backlog 保留范围影响；如果观察到全量同步，应查看主实例日志和对应统计计数，分析哪一个条件不满足，不应强行称为“部分同步验证成功”。

> **实验边界**：`docker network disconnect/connect` 是网络故障模拟，不是主机断电或 Redis 崩溃模拟；Docker 网络命令可能因本机环境而失败。实验没有自动化重试或严格同步屏障，因此它展示的是验证方法和预期轨迹，而非在所有机器上保证同一个日志顺序。

## 10. 复制成功后，副本就能自动代替主实例吗？

现在让我们做一个更直接的故障思考实验。

```text
主实例：activity:1001:views = 104
副本：  activity:1001:views = 104
```

主实例突然停止服务。副本可能仍然保存着此前同步的数据。

但如果活动服务仍把写请求发送到原先的主实例地址，那么请求还是会失败。

即使应用已经知道副本的地址，也不能默认向副本写入。Redis 的副本**默认只读**。如果向它执行：

```redis
INCR activity:1001:views
```

通常会收到 `READONLY` 错误，表示不能在只读副本上写入。

你可以在完成前面同步实验后，单独观察这一限制：

```bash
docker exec redis-repl-replica redis-cli \
  INCR activity:1001:views
```

不要为了演示自动切换，就直接在本章把副本执行 `REPLICAOF NO ONE` 作为生产级故障恢复方案。虽然这个命令可以使副本脱离上游成为可写主实例，但它**不负责**自动检测故障、在多个候选副本中选择正确节点、避免网络分区带来的双主风险，也不负责通知应用更新连接目标。

主从复制解决的是：**让另一个实例尽可能持续拥有主实例的数据。**

自动故障转移还需要回答：

- 主实例是真的宕机，还是只有某个监控节点暂时无法连接？
- 如果有多个副本，哪一个适合提升为主实例？
- 原主实例恢复后，怎样避免它继续独立接受写入？
- 应用客户端如何知道当前新的主实例在哪里？

这些不是给 `PSYNC` 增加一个参数就能解决的，而是新的协调问题。

还有一个容易忽视的事实：复制默认异步。主实例可能已经回复客户端写入成功，但尚未将对应修改复制给副本就发生故障。因此，**有副本也不能直接推出“所有已确认写入都绝不丢失”**。`WAIT` 等机制可以让应用等待副本确认一定复制进度，但它也不会自动把 Redis 变成严格强一致、故障切换绝不丢写的系统。

所以，复制是高可用架构的重要基础，**不是高可用方案的全部**。

## 11. 回看这一章：每个机制为什么出现？

直到现在，才适合回看我们认识的完整链路：

```text
主实例发生故障，需要另一份数据
                |
                v
          准备 Redis 副本
                |
                | 副本最初没有完整数据
                v
           RDB 全量同步
                |
                | 快照生成后主实例仍在写入
                v
        保存并传播后续修改
                |
                | 副本短暂断线，不想全部重传
                v
      replication ID + offset
                |
                | 需要保留近期可补发数据
                v
         replication backlog
                |
                | 缺失历史可用？
          +-----+-----+
          |           |
         是           否
          |           |
          v           v
       +CONTINUE    全量重同步
          |
          v
       副本追赶
          |
          | 主实例故障后谁决定接管？
          v
     下一章：Sentinel
```

这一章没有把主从复制解释成“再启动一个 Redis”这么简单。真正重要的是完整回答以下几个问题：

- `REPLICAOF` 只是启动复制关系，为什么不等于同步完成？
- 第一次同步为什么需要完整数据，以及快照之后的修改如何继续追上？
- 复制流为什么要使用 ID 和字节偏移量？
- 为什么需要只保留近期历史的 backlog？
- 什么情况下部分同步失败并退回全量同步？
- 为什么数据副本已经存在，仍然不等于业务能自动故障切换？

本章解决了“**如何持续维护另一份数据**”，却留下了一个自然的新问题：**当主实例不可用时，谁来判断它已经故障，并决定让哪个副本接替它？**

下一章，我们才进入 Sentinel，从故障判断、主观下线和客观下线开始，逐步研究一次故障转移为什么需要多个监控节点协调，以及客户端如何感知主实例的变化。

**下一章：《Redis 主节点故障后谁来接管？从故障检测到 Sentinel 选主》**

---

## 源码阅读索引

本文所有函数位置均为 Redis **7.2.0**：

| 当前问题 | 真实源码入口 | 本章关注点 |
| --- | --- | --- |
| 如何配置上游主实例？ | [`replicaofCommand()`](https://github.com/redis/redis/blob/7.2.0/src/replication.c) | 解释 `REPLICAOF` 命令与同步任务的区别 |
| 如何开始连接？ | `replicationSetMaster()`、`connectWithMaster()` | 记录上游地址、启动非阻塞连接 |
| 如何协商同步？ | `syncWithMaster()`、`slaveTryPartialResynchronization()` | PING、REPLCONF、PSYNC 握手 |
| 主实例怎样决定同步方式？ | `syncCommand()`、`masterTryPartialResynchronization()` | 部分同步条件判断与全量回退 |
| 如何发起全量数据同步？ | `startBgsaveForReplication()`、`replicationSetupSlaveForFullResync()` | 发送 RDB、记录同步起点及后续差异 |
| 副本如何接收完整数据？ | `readSyncBulkPayload()` | 接收、装载 RDB |
| 后续写入怎样传播？ | [`src/server.c`](https://github.com/redis/redis/blob/7.2.0/src/server.c) 的传播路径、`replicationFeedSlaves()` | 把写入效果放进复制流 |
| 为什么能断线续传？ | `createReplicationBacklog()`、`addReplyReplicationBacklog()` | 保留近期复制历史，按偏移量补发 |
| 断线后如何尝试恢复？ | `replicationCacheMaster()`、`replicationCron()` | 保留旧进度、重新连接 |

**官方参考：**

- [Redis Replication](https://redis.io/docs/latest/operate/oss_and_stack/management/replication/)
- [PSYNC 命令](https://redis.io/docs/latest/commands/psync/)
- [INFO 命令](https://redis.io/docs/latest/commands/info/)
- [Redis 7.2.0 配置文件（复制相关默认值）](https://github.com/redis/redis/blob/7.2.0/redis.conf)

### 清理实验环境

只删除本章所创建的容器和网络：

```bash
docker rm -f redis-repl-master redis-repl-replica
docker network rm redis-repl-lab-net
```
