---
title: "第六章：Redis 重启后数据还在吗？从 RDB 到 AOF"
sidebar_label: "06 - RDB 与 AOF 持久化"
sidebar_position: 7
description: "从一次已经返回成功的访问计数写入出发，复现进程崩溃后的数据丢失，再逐步阅读 Redis 7.2.0 的 RDB 快照、AOF 追加、刷盘、重写与启动恢复源码。"
tags:
  - Redis
  - 源码精读
  - RDB
  - AOF
  - 持久化
---

# 第六章：Redis 重启后数据还在吗？从 RDB 到 AOF

上一章，我们为 Redis 设置了内存上限，并研究了内存不足时怎样选择要淘汰的 Key。

这一次，先把内存压力放到一边。假设活动 `1001` 的访问次数不能随意丢弃，我们不会让这条统计数据因为内存淘汰而消失。Redis 中保存着：

```redis
SET activity:1001:views 100
INCR activity:1001:views
```

客户端收到 `101`。到这里，一切正常。

但是，如果 Redis 进程在这一刻突然崩溃，下一次启动之后，访问次数还会是 `101` 吗？

上一章研究的过期和内存淘汰，都只涉及 Redis **运行时**如何管理内存。现在，我们需要解决一个不同的问题：**如何把运行中的内存状态留给下一次启动的 Redis？**

本章继续沿用活动访问计数作为贯穿案例，遵循同一个顺序：先让最简单的系统失败，再只增加一个必要的机制，观察运行结果，最后对照 Redis 7.2.0 源码。

> **实验边界**：以下操作只适用于独立测试实例。文中的输出是根据 Redis 7.2.0 行为给出的**预期结果**；本文编写环境没有实际启动 Redis 容器，因此不把预期结果称作实测记录。为避免正常关闭自动保存数据掩盖问题，部分实验使用 `SIGKILL` 模拟进程突然退出。`SIGKILL` **不等价于整台机器突然断电**。

## 1. 最简单的设计：只把数据存在内存中

先建立一个完全关闭持久化的 Redis 实例。这里故意不复用前几章的实例，确保实验互不干扰。

```bash
# 仅用于实验：明确关闭 RDB 和 AOF
docker run -d \
  --name redis-persist-none \
  -p 127.0.0.1:6381:6379 \
  redis:7.2.0 \
  --save "" \
  --appendonly no
```

写入活动访问次数：

```bash
docker exec redis-persist-none redis-cli SET activity:1001:views 100
docker exec redis-persist-none redis-cli INCR activity:1001:views
```

预期第二条命令返回：

```text
(integer) 101
```

现在直接结束 Redis 进程，不给它执行正常退出逻辑的机会：

```bash
docker kill --signal=KILL redis-persist-none
docker start redis-persist-none
docker exec redis-persist-none redis-cli GET activity:1001:views
```

预期结果：

```text
(nil)
```

这里的 `GET` 可能需要在容器启动完毕后再执行；若出现连接失败，重试读取即可。**连接失败表示服务尚未就绪，不等于数据丢失的证据。**

为什么数据会消失？原因非常直接：我们只把它写进了 Redis 的内存，而且明确关闭了持久化。进程退出之后，Redis 没有可用于恢复这条 Key 的持久化状态。

到这里，我们真正缺少的是：**在内存之外保留一份能够恢复的数据。**

## 2. 最小的新机制：定期给内存数据拍一张快照

如果让我们自己设计一个恢复方案，最直觉的方法是：把当前字典中的数据整体写入文件。

例如，教学伪代码可以写成：

```python
# 概念伪代码：不是 Redis 源码，也不是 Redis 文件格式

def save_snapshot(store, filename):
    write_all_keys_and_values(store, filename)


def load_snapshot(filename):
    return read_all_keys_and_values(filename)
```

如果数据服务重新启动，就读取上次保存的文件：

```text
进程运行中                           进程重启
    |                                   |
    v                                   v
内存数据 --保存快照--> 文件 --加载快照--> 内存数据
```

这已经能够回答我们的第一个问题：**只要快照成功保存，Redis 就有机会从快照恢复数据。**

Redis 对应的机制叫 **RDB**。在当前问题中，可以先把 RDB 理解成 Redis 某个时间点的数据快照文件，不需要立即研究它的所有二进制编码细节。

Redis 提供两条容易观察的命令：

- `SAVE`：在当前进程中同步执行保存，保存期间会影响正常请求处理。
- `BGSAVE`：启动后台子进程生成 RDB，主进程可以继续服务客户端。

我们先使用 `BGSAVE`，因为真实数据服务不能为了保存文件就长时间停止正常处理请求。

## 3. 实验：RDB 可以恢复快照，但不能恢复快照之后的一切

启动一个独立的 RDB 实验实例。仍然关闭自动快照规则，仅通过手动 `BGSAVE` 来明确控制快照生成的时间点。

```bash
docker run -d \
  --name redis-persist-rdb \
  -p 127.0.0.1:6382:6379 \
  redis:7.2.0 \
  --save "" \
  --appendonly no
```

先保存 `100`：

```bash
docker exec redis-persist-rdb redis-cli SET activity:1001:views 100
docker exec redis-persist-rdb redis-cli BGSAVE
```

预期 `BGSAVE` 返回类似：

```text
Background saving started
```

**注意：收到这句话只表示后台保存已经开始，不能说明 RDB 已经写完。** 我们还要检查保存结果：

```bash
docker exec redis-persist-rdb redis-cli INFO persistence
```

确认其中的字段达到：

```text
rdb_bgsave_in_progress:0
rdb_last_bgsave_status:ok
```

在刚启动后立刻读取，这两个字段不一定已经表示本次快照完成；应在 `BGSAVE` 已启动后等待任务结束再检查。也可以检查 `LASTSAVE` 或容器内 `/data/dump.rdb` 的存在情况，辅助确认。

现在制造快照与最新内存状态之间的差异：

```bash
docker exec redis-persist-rdb redis-cli INCR activity:1001:views
```

内存中的值变成 `101`，但**本次快照记录的是此前的 `100`**。由于我们关闭了自动快照，不会有新的自动 RDB 覆盖它。

现在模拟进程突然退出：

```bash
docker kill --signal=KILL redis-persist-rdb
docker start redis-persist-rdb
docker exec redis-persist-rdb redis-cli GET activity:1001:views
```

当服务启动完毕之后，预期返回：

```text
"100"
```

这是一条很重要的观察：

```text
时间线

SET 100 ------ BGSAVE 完成 ------ INCR 得到 101 ------ 进程崩溃
                  |
                  v
               RDB = 100

重启后：从 RDB 恢复 100，而不是 101
```

**RDB 解决了“能否恢复某个时间点的内存数据”，却没有保证“能恢复每一次已经成功的写入”。**

这个限制并不是实现错误，而是快照方案自然带来的时间窗口。

接下来我们进入真实源码，先回答一个问题：Redis 如何在生成快照的同时，继续处理客户端写入？

## 4. Redis 为什么要用子进程生成 RDB？

源码版本固定为 [Redis 7.2.0](https://github.com/redis/redis/tree/7.2.0)，先看：

- `src/rdb.c`：`bgsaveCommand()`、`rdbSaveBackground()`、`rdbSave()`、`rdbSaveRio()`、`rdbSaveDb()`。

客户端执行 `BGSAVE` 后，`bgsaveCommand()` 在条件允许时调用 `rdbSaveBackground()`。在该函数中，我们能找到一个关键动作。

**真实源码节选（Redis 7.2.0，`src/rdb.c`，省略相邻无关逻辑）：**

```c
if ((childpid = redisFork(CHILD_TYPE_RDB)) == 0) {
    int retval;
    /* Child */
    redisSetProcTitle("redis-rdb-bgsave");
    redisSetCpuAffinity(server.bgsave_cpulist);
    retval = rdbSave(req, filename,rsi,rdbflags);
    if (retval == C_OK) {
        sendChildCowInfo(CHILD_INFO_TYPE_RDB_COW_SIZE, "RDB");
    }
    exitFromChild((retval == C_OK) ? 0 : 1);
}
```

这里最需要理解的是 `redisFork(CHILD_TYPE_RDB)`。

`fork` 会创建一个子进程。在这次操作之后：

- **子进程**调用 `rdbSave()`，把它看到的数据状态序列化为快照。
- **父进程**继续执行事件循环，处理来自客户端的新命令。

为什么子进程能在父进程修改数据时保存一份相对稳定的快照？这依赖操作系统的 **写时复制（Copy-on-Write，COW）** 机制。可以先理解为：父子进程起初共享相应的物理内存页；当父进程修改某个共享页时，操作系统会为这次修改提供独立页面，让子进程继续读取原先的内容。

因此，RDB 不需要让主进程在整段磁盘写入过程中停止处理所有请求。

但这并不是“零成本”：`fork` 自身需要工作，写时复制可能增加内存占用，快照写盘也会产生 I/O 压力。数据集很大或者系统资源紧张时，仍然可能出现延迟与失败。

我们现在只追踪一条正常路径：子进程如何把 `activity:1001:views = 100` 写进 RDB。

## 5. RDB 快照到底保存了什么？

先进入 `rdbSave()`。它不会直接覆盖已有的正式 RDB 文件，而是先生成临时文件。

**真实源码节选（`src/rdb.c`，`rdbSave()`）：**

```c
snprintf(tmpfile,256,"temp-%d.rdb", (int) getpid());
if (rdbSaveInternal(req,tmpfile,rsi,rdbflags) != C_OK) {
    stopSaving(0);
    return C_ERR;
}
```

这里的“先写临时文件”很好理解：如果保存到一半失败，不能把原本可用的快照轻易破坏。

保存完成之后，函数还会调用 `rename(tmpfile,filename)`，将成功生成的临时文件替换为正式文件。真实实现还包含文件同步、目录同步及失败处理。**重命名可以避免用未完成的快照覆盖原文件，但不能把它等同于整个存储系统在所有故障下绝对可靠。**

继续沿 `rdbSaveInternal()` 进入 `rdbSaveRio()`。后者负责写入 RDB 格式内容，并逐个处理数据库：

**真实源码节选（`src/rdb.c`，`rdbSaveRio()`）：**

```c
for (j = 0; j < server.dbnum; j++) {
    if (rdbSaveDb(rdb, j, rdbflags, &key_counter) == -1) goto werr;
}
```

这里的 `server.dbnum` 是逻辑数据库数量，`rdbSaveDb()` 则负责保存某个数据库的数据。

我们在最初的章节里已经见过 Redis 用字典组织 Key-Value。现在，这个字典再次出现：

**真实源码节选（`src/rdb.c`，`rdbSaveDb()`）：**

```c
redisDb *db = server.db + dbid;
dict *d = db->dict;
if (dictSize(d) == 0) return 0;
di = dictGetSafeIterator(d);
```

随后，`rdbSaveDb()` 遍历字典的各个条目。对一个 Key，它会读取 Key、Value 以及相应的过期时间，并调用 `rdbSaveKeyValuePair()` 序列化。

我们不必现在就研究具体的二进制编码；本章只需要证明一件事：**快照保存的是数据库中实际存在的数据状态，而不只是“曾经执行过哪些命令”的文字记录。**

对应到案例：

```text
内存 db->dict                      RDB 快照

activity:1001:views = 100  --->  序列化的 Key、Value、必要元信息
```

当 Redis 启动时，`src/server.c` 的 `loadDataFromDisk()` 可以走 RDB 恢复路径，调用 `rdbLoad()`；`rdbLoad()` 再通过 `rdbLoadRio()` 把文件中的数据恢复到内存。

这解释了上一节为什么能从快照恢复 `100`。

但是，`101` 为什么没有回来？因为那次 `INCR` 发生在快照完成之后，尚未被新的快照记录。

我们现在面临的矛盾非常明确：**如果不能接受两次快照之间的更新丢失，就不能只依赖周期性快照。**

## 6. 不再反复保存整个状态，而是记录每次写入

想象一下，如果我们有一份活动计数：

```text
最初：100
INCR -> 101
INCR -> 102
INCR -> 103
```

为了保存最终状态，除了定期重新生成完整快照，还可以尝试记录每一次使数据发生变化的操作。

例如，概念上的日志：

```text
SET activity:1001:views 100
INCR activity:1001:views
INCR activity:1001:views
INCR activity:1001:views
```

重新启动时，从初始状态按顺序执行这些命令，就能够重建 `103`。

这就是我们这一步需要的新机制：**将写操作追加到持久化日志中，启动时通过日志恢复数据状态。**

Redis 对应的机制叫 **AOF（Append Only File）**。

它和 RDB 的区别可以先用两句话概括：

- RDB 关注：**某个时间点，数据库是什么状态？**
- AOF 关注：**哪些写操作能够重建数据库状态？**

注意，AOF 并不意味着把所有客户端发送的命令不加区别地原样写入文件。Redis 有命令传播与重写逻辑；我们现在只研究普通写入的最小路径。

## 7. AOF 写入的关键源码：先组织命令，再写文件

回想第三章：`INCR` 会在 Redis 服务端完成修改，并返回新值。

问题是：执行了数据修改之后，Redis 怎样让持久化层也知道这条写命令？

在 `src/server.c` 的 `propagateNow()` 中，存在这样的逻辑：

**真实源码节选：**

```c
if (server.aof_state != AOF_OFF && target & PROPAGATE_AOF)
    feedAppendOnlyFile(dbid,argv,argc);
```

这里表示：当 AOF 处于需要记录的状态、当前操作也需要传播到 AOF 时，把命令交给 `feedAppendOnlyFile()`。

进入 `src/aof.c`，可以找到两行核心代码。

**真实源码节选（`feedAppendOnlyFile()`，省略中间处理及状态判断的相邻代码）：**

```c
buf = catAppendOnlyGenericCommand(buf,argc,argv);
```

以及在允许写入 AOF 缓冲区的分支中：

```c
server.aof_buf = sdscatlen(server.aof_buf, buf, sdslen(buf));
```

第一行将命令组织成可以持久化的协议表示。第二行把内容追加到 Redis 的 AOF 内存缓冲区。

**注意：加入 `server.aof_buf`，还不是完成磁盘持久化。**

这只是把“需要写入日志的内容”准备好了。真正将缓冲区内容写入文件，还需要另一个阶段。

在 `src/server.c` 的 `beforeSleep()` 中，可以看到：

**真实源码节选：**

```c
if (server.aof_state == AOF_ON || server.aof_state == AOF_WAIT_REWRITE)
    flushAppendOnlyFile(0);
```

这个位置很重要：Redis 在返回事件循环等待和处理客户端待发送回复的过程中，安排 AOF 缓冲区的写入，而不是让每个命令处理函数直接完整承担文件 I/O。

进入 `src/aof.c` 的 `flushAppendOnlyFile()`，可以看到真正的写入调用：

**真实源码节选：**

```c
nwritten = aofWrite(server.aof_fd,server.aof_buf,sdslen(server.aof_buf));
```

至此，AOF 的正常写入路径已经可以连起来了：

```text
INCR 修改内存
    |
    v
命令传播到 AOF
    |
    v
feedAppendOnlyFile()
    |
    | 生成可记录的命令表示
    v
server.aof_buf
    |
    | 在后续事件循环阶段刷新
    v
flushAppendOnlyFile()
    |
    v
aofWrite() -> 文件系统
```

但是，这条链还缺少一件很关键的事：**写入文件，不必然等于数据已经在断电后仍能恢复。**

## 8. 为什么“写入 AOF 文件”和“刷盘完成”不是一回事？

把数据写入文件时，操作系统通常还会使用自己的文件缓存。一次 `write` 成功，可能表示数据已经进入内核管理的缓存，并不总是表示底层存储设备已经可靠完成写入。

因此，如果我们的故障模型包含整机掉电，还需要关注什么时候调用 `fsync` 一类的同步操作。

Redis 为 AOF 提供了三种常见策略：

| 配置 | 直观含义 | 代价与边界 |
|---|---|---|
| `appendfsync always` | 尽可能在写入批次后同步文件，再确认对应写入 | 延迟与 I/O 成本更高；也不等于无视硬件故障的绝对不丢失 |
| `appendfsync everysec` | 大致每秒发起一次后台同步 | 性能与安全的折中；故障时可能丢失最近的一段写入 |
| `appendfsync no` | Redis 不主动按写入周期执行 fsync | 依赖操作系统刷盘时机，故障恢复窗口更加不可控 |

这三种方案没有一个能同时拥有最低的延迟与最强的持久性保证。

我们去 `flushAppendOnlyFile()` 的源码中验证它们为什么表现不同。

**真实源码节选（`src/aof.c`）：**

```c
if (server.aof_fsync == AOF_FSYNC_ALWAYS) {
    if (redis_fsync(server.aof_fd) == -1) {
        serverLog(LL_WARNING,"Can't persist AOF for fsync error when the "
          "AOF fsync policy is 'always': %s. Exiting...", strerror(errno));
        exit(1);
    }
```

这个分支会在当前写入路径执行文件同步；出现同步错误时，会做强硬的失败处理。

而 `everysec` 的正常分支采用的是另一种方式：

**真实源码节选（省略外围状态检查）：**

```c
} else if (server.aof_fsync == AOF_FSYNC_EVERYSEC &&
           server.unixtime > server.aof_last_fsync) {
    if (!sync_in_progress) {
        aof_background_fsync(server.aof_fd);
        server.aof_last_incr_fsync_offset = server.aof_last_incr_size;
    }
    server.aof_last_fsync = server.unixtime;
}
```

可以看到，`everysec` 会使用后台同步任务，降低同步操作对主执行路径的直接影响。

不过，“每秒”是一种策略与时间尺度，不是对任何故障情况下**恰好只丢一秒**的数学保证。磁盘故障、系统暂停、同步延迟、异常退出方式等都会影响结果。

而且，还需要记住一个此前已经强调过的区分：

**`SET` / `INCR` 的命令原子性，不能替代 AOF 的持久化保证；AOF 的持久化保证，也不能替代跨节点的数据复制。**

## 9. 实验：开启 AOF 后重新启动 Redis

为了减少实验受同步时机影响的程度，我们使用 `appendfsync always`，并禁用自动 AOF 重写，仅检查一次正常写入后的进程重启恢复。

```bash
docker run -d \
  --name redis-persist-aof \
  -p 127.0.0.1:6383:6379 \
  redis:7.2.0 \
  --save "" \
  --appendonly yes \
  --appendfsync always \
  --auto-aof-rewrite-percentage 0
```

写入并递增两次：

```bash
docker exec redis-persist-aof redis-cli SET activity:1001:views 100
docker exec redis-persist-aof redis-cli INCR activity:1001:views
docker exec redis-persist-aof redis-cli INCR activity:1001:views
```

当前值应为 `102`。

先检查 Redis 确实开启了 AOF：

```bash
docker exec redis-persist-aof redis-cli INFO persistence
```

其中应包含：

```text
aof_enabled:1
```

然后模拟 Redis 进程突然退出并重新启动：

```bash
docker kill --signal=KILL redis-persist-aof
docker start redis-persist-aof
docker exec redis-persist-aof redis-cli GET activity:1001:views
```

服务就绪后，**预期返回**：

```text
"102"
```

这个实验能验证一个较小的结论：在实验所设置的 AOF 条件下，重启的 Redis 能够根据持久化记录恢复活动计数。

**它不能验证整机掉电时绝对不会丢失数据**：Docker 的 `SIGKILL` 只终止容器中的进程，通常不会清空宿主机操作系统已经缓存的数据。想测量真正的掉电丢失窗口，需要更严格的故障注入环境。

现在还有一个值得注意的现象：我们只是不断修改同一个 Key，但如果把每次写入都保留下来，日志会不会越来越大？

## 10. AOF 日志不断增长：为什么需要重写？

继续沿用计数场景。假设活动访问数连续增加一百万次。

如果日志始终保留每一次递增操作，那么恢复到最终的计数时，需要处理非常多的历史写入。

但如果当前最终状态仅仅是：

```text
activity:1001:views = 1000100
```

我们就会发现：**恢复当前状态，并不一定需要保留所有历史操作的原始形式。**

因此，Redis 提供了 AOF 重写：根据当前数据状态生成能够重建状态的新基线，避免日志无限累积。

但这里必须纠正一个很常见的旧版印象：**Redis 7.2 使用的是多文件 AOF（Multi-Part AOF），而不只是反复替换一个 `appendonly.aof`。**

Redis 7.2 的 AOF 可以由以下文件协同组成：

- **BASE**：最近一次重写形成的基础状态，可以采用 RDB 格式或 AOF 格式。
- **INCR**：基础状态之后的增量写入日志，可以有多个。
- **Manifest**：记录哪些 BASE 与 INCR 文件组成当前有效的 AOF 数据集。

这里的文件名会随重写次数变化，下面只是结构示意，并不是一次实际执行后的固定文件名：

```text
appendonlydir/
  appendonly.aof.manifest
  appendonly.aof.1.base.rdb
  appendonly.aof.1.incr.aof
```

这个设计是怎样被工程问题推动出来的？

设想 AOF 正在后台重写，但主进程此时又收到了新写入。如果父进程暂停接收写命令等待重写结束，业务延迟会明显增加。

Redis 7.2 的思路是：**后台子进程生成新的 BASE，主进程继续把新写操作写进新的 INCR。**

在 `src/aof.c` 的 `rewriteAppendOnlyFileBackground()` 中，可以看到：

**真实源码节选（非连续片段，仅截取关键动作）：**

```c
flushAppendOnlyFile(1);
if (openNewIncrAofForAppend() != C_OK) {
    server.aof_lastbgrewrite_status = C_ERR;
    return C_ERR;
}
```

之后才启动子进程：

```c
if ((childpid = redisFork(CHILD_TYPE_AOF)) == 0) {
```

这个顺序很重要：主进程先做好继续记录增量写入的准备，再让后台子进程生成新基线。

重写成功结束后，`backgroundRewriteDoneHandler()` 会处理 BASE 文件与 Manifest 的更新，并清理不再需要的历史文件。这样，新的基础数据与重写期间产生的增量写入能够组合成完整的可恢复状态。

读完这几个动作，我们就能解释为什么重写并不要求 Redis 长时间停止接收新的写入请求。

如果你想观察实际的文件组织方式，可以在测试实例中手动触发：

```bash
docker exec redis-persist-aof redis-cli BGREWRITEAOF
```

等 `INFO persistence` 中的 `aof_rewrite_in_progress` 变为 `0`，并确认 `aof_last_bgrewrite_status:ok` 后，查看：

```bash
docker exec redis-persist-aof sh -c 'find /data/appendonlydir -maxdepth 1 -type f -print'
```

文件名序号会根据此前的写入和重写历史而变化，不能把上面的示意名称作为必须逐字匹配的断言。

## 11. Redis 启动时，先恢复 RDB 还是 AOF？

现在我们已经研究了两种不同的持久化机制：

```text
RDB：从某个时间点的状态恢复
AOF：从基础状态和增量操作恢复
```

那么，如果一个 Redis 实例同时开启了两种机制，启动时究竟读取哪一种？

这个问题不能只靠直觉回答，因为如果读取了较旧的 RDB，可能会忽略 AOF 中已经记录的更新。

在 `src/server.c` 的 `loadDataFromDisk()` 中，Redis 7.2.0 明确检查 AOF 状态：

**真实源码节选：**

```c
if (server.aof_state == AOF_ON) {
    int ret = loadAppendOnlyFiles(server.aof_manifest);
    if (ret == AOF_FAILED || ret == AOF_OPEN_ERR)
        exit(1);
} else {
    /* 省略：随后进入 RDB 加载路径 */
}
```

这里需要强调：上面 `else` 中的注释是**教学说明**，不是照抄源码；完整函数还会处理加载结果及其他状态。

对于同时启用了 RDB 和 AOF、并以 AOF 模式启动的常规配置，Redis 优先根据 AOF 恢复，而不是再把两者不加区别地叠加读取。

如果进入 RDB 路径，则调用 `rdbLoad()`。AOF 路径调用 `loadAppendOnlyFiles()`，依据 Manifest 读取 BASE 和相应的 INCR 文件。

在 `src/aof.c` 中，`loadAppendOnlyFiles()` 的正常路径就是先加载基础文件，再按顺序加载增量文件：

```c
/* 真实源码节选：加载 INCR 文件的循环体 */
listRewind(am->incr_aof_list, &li);
while ((ln = listNext(&li)) != NULL) {
    aofInfo *ai = (aofInfo*)ln->value;
    /* ... */
    ret = loadSingleAppendOnlyFile(aof_name);
```

对于记录写命令的 AOF 文件，Redis 在加载阶段会构造一个内部使用的客户端上下文，解析并执行日志中的命令，以恢复内存数据。这与我们前几章学习的命令执行机制是连贯的：日志不是“恢复到一个神秘的内存地址”，而是有明确的文件读取与数据重建路径。

## 12. 到这里，RDB 与 AOF 的取舍应该怎样理解？

现在我们不再按“哪个更高级”比较，而是回到最初的问题：我们愿意为更小的数据丢失窗口付出多少代价？

| 问题 | RDB 快照 | AOF 日志 |
|---|---|---|
| 记录的核心内容 | 某个时间点的数据状态 | 可用于重建状态的写操作，以及可能的基础文件 |
| 一次故障后的恢复范围 | 通常到最近一次成功快照 | 取决于 AOF 文件内容、刷盘策略及故障情况 |
| 写入时的主要额外工作 | 周期性创建快照，涉及 fork、COW 和磁盘 I/O | 持续追加日志及按策略同步文件 |
| 长期运行要考虑什么 | 快照频率、文件管理、保存耗时和内存峰值 | 文件增长、AOF 重写、刷盘延迟与写入错误 |
| 是否意味着永不丢数据 | 否 | 否 |

两者不是只能二选一；Redis 可以同时开启 RDB 与 AOF，但应结合数据重要性、恢复目标、磁盘资源和故障模型进行选择。

对于只用作可重建缓存的数据，我们可能愿意接受重启后重新填充；对于不能容忍丢失的核心业务记录，则不应把 Redis 的单机持久化视为完整的可靠性方案。

尤其要区分：

1. **命令执行成功**：Redis 在当前实例中完成了此次数据操作。
2. **AOF 已写入文件系统**：日志内容已经经过写入阶段，但可能尚未同步到稳定存储。
3. **刷盘已完成**：按配置进行了同步操作，但仍受硬件、文件系统及故障模型约束。
4. **服务能够恢复**：重启时存在可读取、可校验的持久化文件，并成功加载。

这些不是同一件事。

## 13. 一次完整的最小运行链

现在再回头看，我们已经有能力说明数据是怎样从一次 `INCR` 活到下一次启动的。

**使用 RDB：**

```text
客户端 INCR
    |
    v
Redis 更新内存计数
    |
    | 在某次 BGSAVE 时
    v
fork 子进程
    |
    v
序列化内存数据状态
    |
    v
写入并完成 RDB 快照
    |
    | Redis 退出、重新启动
    v
rdbLoad()
    |
    v
恢复到快照中的数据状态
```

**使用 AOF：**

```text
客户端 INCR
    |
    v
Redis 更新内存计数
    |
    v
feedAppendOnlyFile()
    |
    v
server.aof_buf
    |
    v
flushAppendOnlyFile()
    |
    +--> aofWrite()
    |
    +--> 根据策略执行 fsync
    |
    | Redis 退出、重新启动
    v
loadAppendOnlyFiles()
    |
    v
加载 BASE 与 INCR
    |
    v
恢复内存数据状态
```

这里最重要的不是记住函数表，而是理解设计因果：

**只有内存，就无法跨越进程故障；有了快照，就能恢复到某个时间点；想缩小快照间的丢失窗口，就需要更连续地记录写入；日志持续增长，又需要重写与文件管理。**

## 14. 解决了进程重启，下一次该研究什么？

到目前为止，我们一直只研究一个 Redis 实例。

假设这个实例能够通过 RDB 或 AOF 恢复数据，但它所在的服务器突然发生硬件故障，一时无法重新启动。

即使磁盘上的数据完整，用户请求仍然无法连接到这个 Redis 实例。

我们真正缺少的已经不是“如何将数据写进磁盘”，而是：**当正在提供服务的实例暂时不可用时，能否由另一台 Redis 接手数据服务？**

自然的想法是再启动一个 Redis，让它跟随主实例的数据变化。但这又引出一个新问题：

**如果主实例刚执行了一次 `INCR` 就崩溃，而另一个 Redis 还没有收到这次更新，接管后会读到什么？**

这说明持久化与复制虽然都关系到数据安全，却解决不同层面的问题。下一章我们将从一个主实例、一个副本开始，研究 Redis 怎样同步数据，以及为什么“有副本”仍然不等于“任何故障下零数据丢失”。

**下一章：《Redis 单实例故障后怎么办？从主从复制到数据同步》**

---

## 本章回顾

本章只沿着一条递进路径增加机制：

```text
INCR 返回成功
      |
      | 进程突然退出，内存状态消失
      v
RDB：保存某个时刻的数据快照
      |
      | 快照之间仍可能丢失新的写入
      v
AOF：追加写操作、启动时重建状态
      |
      | 文件写入与稳定刷盘不完全相同
      v
appendfsync：在性能与故障恢复窗口之间取舍
      |
      | 日志持续累积
      v
AOF 重写：BASE + INCR + Manifest
      |
      | 单台机器故障时仍可能无法提供服务
      v
下一章：复制
```

源码中最值得记住的不是所有分支，而是几个设计动作：

- `rdbSaveBackground()` 通过子进程生成快照，避免长时间让主进程承担整个快照写盘过程。
- `rdbSaveRio()` / `rdbSaveDb()` 序列化当前数据库数据，`rdbLoad()` 在恢复时读取文件。
- `feedAppendOnlyFile()` 组织待记录的写命令，`flushAppendOnlyFile()` 处理 AOF 缓冲区写入与相应刷盘策略。
- `rewriteAppendOnlyFileBackground()` 与 `backgroundRewriteDoneHandler()` 配合处理 Redis 7.2 的多文件 AOF 重写。
- `loadDataFromDisk()` 决定启动时进入 AOF 还是 RDB 恢复路径。

**持久化不是“让内存永远不丢”，而是为特定的故障场景提供可恢复的磁盘状态；能恢复到什么时间点，取决于我们选择的机制与配置。**

## 源码及官方资料

本章源码基线统一为 **Redis 7.2.0**：

- [`src/rdb.c`：`rdbSaveBackground()`、`rdbSave()`、`rdbSaveRio()`、`rdbSaveDb()`、`rdbLoad()`](https://github.com/redis/redis/blob/7.2.0/src/rdb.c)
- [`src/aof.c`：`feedAppendOnlyFile()`、`flushAppendOnlyFile()`、`loadAppendOnlyFiles()`、AOF 重写](https://github.com/redis/redis/blob/7.2.0/src/aof.c)
- [`src/server.c`：`propagateNow()`、`beforeSleep()`、`loadDataFromDisk()`](https://github.com/redis/redis/blob/7.2.0/src/server.c)
- [Redis 官方文档：Persistence](https://redis.io/docs/latest/management/persistence/)
- [Redis 官方命令：BGSAVE](https://redis.io/docs/latest/commands/bgsave/)
- [Redis 官方命令：BGREWRITEAOF](https://redis.io/docs/latest/commands/bgrewriteaof/)

实验结束后，可以分别删除本章的三个独立容器：

```bash
docker rm -f redis-persist-none redis-persist-rdb redis-persist-aof
```

它们专供本章实验使用，不会操作前几章的其他 Redis 容器。本文所有故障实验仅展示**预期行为**，并不声称在当前环境中已实际运行。
