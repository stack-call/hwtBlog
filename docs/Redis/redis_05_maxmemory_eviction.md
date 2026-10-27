---
title: "第五章：Redis 内存满了怎么办？从过期删除到内存淘汰"
sidebar_label: "05 - maxmemory 与内存淘汰"
sidebar_position: 6
description: "沿用活动访问计数案例，从过期机制无法限制所有数据的内存占用出发，逐层理解 maxmemory、noeviction、随机淘汰与 Redis 7.2.0 的近似 LRU 源码。"
tags:
  - Redis
  - 源码精读
  - maxmemory
  - LRU
  - 内存淘汰
---

# 第五章：Redis 内存满了怎么办？从过期删除到内存淘汰

上一章，我们给活动数据设置了过期时间，让 Redis 在数据不再需要时能够删除它。

例如，活动 `1001` 的访问次数只保留一天：

```redis
SET activity:1001:views 100
EXPIRE activity:1001:views 86400
```

这个设计回答了一个问题：**某条数据在什么时间之后不再有效？**

但它没有回答另一个问题：**即使所有数据都还没有过期，Redis 的内存是否足够装下它们？**

本章只研究第二个问题。我们继续沿用活动系统，但这次不是某个 Key 到期，而是同时在线的活动越来越多、每个活动又产生更多统计和临时数据，Redis 的内存持续增长。

我们的目标不是一口气背下所有淘汰策略，而是沿一条清晰的因果链，逐步回答：

> 当内存预算已经用完，Redis 应当拒绝新的写入，还是主动舍弃某些已有数据？如果决定舍弃，凭什么选择其中一个 Key？

本章源码基线统一为 **Redis 7.2.0**。下面标记为“真实源码节选”的代码均与该版本对应；为保持阅读焦点，片段可能省略无关的相邻语句。

## 1. 有过期时间，为什么仍然可能占满内存？

假设一个活动持续 24 小时，系统规定活动结束一天后再删除相关数据。

我们给每个活动的数据设置 TTL：

```redis
SET activity:1001:views 100 EX 172800
SET activity:1002:views 200 EX 172800
SET activity:1003:views 300 EX 172800
```

在有效期内，这些 Key 都不应因为“已经过期”而被清理。

但如果同时开展的活动从 3 个变成 3 万个，情况就不同了。

```text
                 Redis 内存
┌──────────────────────────────────┐
│ activity:1001:views      未过期  │
│ activity:1002:views      未过期  │
│ activity:1003:views      未过期  │
│ ...                              │
│ activity:30000:views     未过期  │
└──────────────────────────────────┘

每个 Key 都有效，但内存不再无限。
```

到这里，我们才有必要区分两种此前看起来相似的删除行为：

- **过期删除（expiration）**：Key 已经到达业务设定的失效时间，Redis 使其不再有效并清理它。
- **内存淘汰（eviction）**：Key 可能**还没有过期**，但 Redis 为了控制内存占用，按配置的规则提前移除它。

因此，“每个 Key 都设置了 TTL”并不能保证 Redis 在高峰期一定不会遇到内存不足。TTL 解决的是数据生命周期，淘汰解决的是资源预算；二者不等价。

如果我们自己编写一个内存字典服务，最初也会遇到类似问题。我们不妨先用最简单的做法处理它。

## 2. 最小方案：内存预算用完，就拒绝写入

先假设我们的数据存储有一个固定的内存预算。最容易想到的设计是：

```python
# 概念伪代码，不是 Redis 内存管理的实际实现

def set_with_budget(key, value):
    if current_memory_usage() > memory_budget:
        raise MemoryError("memory limit reached")
    store[key] = value
```

这个写法只是为了建立直觉：存储服务不能无限接受新数据，所以需要一条明确的资源边界。

真实系统当然复杂得多。例如，写入之前很难准确预测某条命令最终分配多少字节，字典扩容也会产生额外开销。因此这个伪代码不是 Redis 的实际检查方式，也不能把它理解为“严格不会超过预算”的证明。

尽管如此，**当预算不足时拒绝可能增加内存使用的命令**，本身是一种完全合理的选择。

对于 Redis，这需要两个配置：

```text
maxmemory 8mb
maxmemory-policy noeviction
```

`maxmemory` 指定 Redis 的内存使用预算；`noeviction` 表示：当内存已经超限且无法继续接受受内存限制的写操作时，不主动从已有 Key 中选择牺牲者。

我们先不讨论应该删谁。因为在确认“可以删已有数据”之前，**拒绝新写入才是最小且不擅自丢弃旧数据的行为**。

现在我们有了新的问题：Redis 是在哪个时机判断自己已经超出预算，并决定拒绝写入的？

## 3. 在执行 SET 之前，Redis 先做了什么？

上一章以前，我们已经追踪过 `SET` 命令的执行路径：

```text
processCommand()
    ↓
call()
    ↓
setCommand()
    ↓
更新数据库中的 Value
```

这次我们不需要重新介绍网络读取，也不需要重复解释 `setCommand()`。

只在这条熟悉的路径中增加一个检查：**在进入实际命令处理函数之前，如果配置了内存限制，Redis 需要先尝试解决超限问题。**

查看 [Redis 7.2.0：`src/server.c` 的 `processCommand()`](https://github.com/redis/redis/blob/7.2.0/src/server.c#L3532)。

**真实源码节选：**

```c
if (server.maxmemory && !isInsideYieldingLongCommand()) {
    int out_of_memory = (performEvictions() == EVICT_FAIL);
```

注意：`performEvictions()` 并不等于“一定执行删除”。它先检查当前内存状态；需要且允许淘汰时才进一步行动。

如果 Redis 判断内存不足，且当前命令属于需要在 OOM 状态下拒绝的命令类别，就会走到同一个函数后面的拒绝分支。

**真实源码节选（与上面不是连续代码）：**

```c
if (out_of_memory && reject_cmd_on_oom) {
    rejectCommand(c, shared.oomerr);
    return C_OK;
}
```

这里出现了一个重要的设计边界：**不是所有命令都会因为 `maxmemory` 超限而一律失败。**

例如，正常的 `GET` 不需要因为当前内存不足而被当作新增数据的写入命令拒绝。Redis 会依据命令属性、事务上下文等决定是否走 OOM 拒绝路径。

从调用关系来看，结果就是：

```text
客户端 SET activity:1001:cache ...
             |
             v
      processCommand()
             |
             v
     已配置 maxmemory？
             |
             v
      performEvictions()
             |
       +-----+-----+
       |           |
    内存允许     超限且无法腾出
       |           |
       v           v
  执行 SET     可能拒绝此写命令
```

我们还没进入任何淘汰策略，但已经能解释：**Redis 不把内存资源检查交给 `SET` 自己负责，而是在统一的命令执行入口附近进行控制。**

这也使不同类型的命令能够共用这一资源限制机制。

## 4. Redis 如何知道内存“超了多少”？

刚才看到的 `performEvictions()` 位于 `src/evict.c`。

它需要先知道：

1. Redis 当前使用多少内存？
2. 配置的 `maxmemory` 是多少？
3. 如果已经超限，大约需要腾出多少？

因此它先调用：

**真实源码节选 — [`src/evict.c`](https://github.com/redis/redis/blob/7.2.0/src/evict.c)：**

```c
if (getMaxmemoryState(&mem_reported,NULL,&mem_tofree,NULL) == C_OK) {
    result = EVICT_OK;
    goto update_metrics;
}
```

此处 `getMaxmemoryState()` 会计算 Redis 从 `maxmemory` 的角度看是否超限，并按需要提供 `mem_tofree`，即为了回到预算内大致需要释放的字节数。

继续看 `getMaxmemoryState()` 的关键部分。

**真实源码节选（非连续片段）：**

```c
mem_reported = zmalloc_used_memory();
```

```c
size_t overhead = freeMemoryGetNotCountedMemory();
mem_used = (mem_used > overhead) ? mem_used-overhead : 0;
```

```c
mem_tofree = mem_used - server.maxmemory;
```

这三段代码揭示了一个容易讲错的地方：**`maxmemory` 不是进程实际占用内存的绝对硬上限。**

Redis 会从用于判定淘汰的内存使用量中扣除某些复制和 AOF 缓冲区开销，避免因为淘汰动作本身引发更多缓冲区内存，形成持续淘汰的反馈循环。

再加上命令执行期间可能临时分配内存、分配器碎片以及其他进程级开销，不能把 `maxmemory 8mb` 理解成操作系统中 Redis 进程绝不会占用超过 8 MiB。

这也解释了为什么生产环境设置 `maxmemory` 时仍然需要为系统、缓冲区和其他运行开销留出余量。

不过，我们当前只需要知道：Redis 已经能判断内存是否不足，并计算出大致需要释放多少内存。

如果策略是 `noeviction`，它接下来会怎样做？

## 5. `noeviction`：确实没有候选 Key 可以删除

回到 `performEvictions()`。

**真实源码节选 — `src/evict.c`：**

```c
if (server.maxmemory_policy == MAXMEMORY_NO_EVICTION) {
    result = EVICT_FAIL; /* We need to free memory, but policy forbids. */
    goto update_metrics;
}
```

这一分支的含义很明确：

Redis 发现内存超限，但配置不允许主动淘汰已有 Key，于是返回失败状态。

对于需要在超限时拒绝的写命令，上一节的 `processCommand()` 会据此产生 OOM 响应。

至此，方案一已经完成：

```text
内存接近/超过预算
       |
       v
尝试检查当前内存状态
       |
       v
策略不允许淘汰
       |
       v
拒绝受 OOM 限制的写命令
```

**这个设计有一个重要优点：它不会为了接纳新数据，静默删除已有的 Key。**

但现在，假设活动的部分数据只是可从数据库重新生成的页面缓存。对于这些 Key，我们宁可丢弃一些旧缓存，也希望新请求依然能够写入。

这时，拒绝所有相关新写入就不再是最合适的处理方式。

新的需求变成：

> 当空间不够时，允许 Redis 移除一部分已有 Key，给新数据腾出位置。

但应该先删哪一个？

## 6. 从最简单的选择开始：随机淘汰

我们先不急着引入 LRU 或 LFU。

如果已经允许删除缓存，最简单的策略就是：**从可删除的 Key 中随机选一个。**

这可能不够聪明，却很容易理解，也不需要追踪每个 Key 的访问历史。

Redis 提供两类随机淘汰策略：

- `allkeys-random`：从数据库的所有 Key 中选择候选。
- `volatile-random`：只从**设置了过期时间**的 Key 中选择候选。

之所以需要两个范围，是因为业务可能允许删除所有缓存，也可能只允许提前删除部分临时数据。

这时才需要回忆上一章的一个数据结构：Redis 不仅有保存所有 Key 的 `db->dict`，还维护了记录过期信息的 `db->expires`。

在 `performEvictions()` 的随机分支中，Redis 根据策略选择候选字典。

**真实源码节选 — `src/evict.c`：**

```c
dict = (server.maxmemory_policy == MAXMEMORY_ALLKEYS_RANDOM) ?
       db->dict : db->expires;
```

随后调用：

```c
de = dictGetRandomKey(dict);
bestkey = dictGetKey(de);
```

这几行代码回答了两个问题。

**第一，`allkeys` 和 `volatile` 的区别，首先体现在选择候选 Key 的范围。** Redis 并不是给每个 Key 标记“可以淘汰”或者“不可以淘汰”，而是可以从不同的字典中寻找候选。

**第二，随机淘汰无需给 Key 维护完整的访问排序。** 它能以简单方式找出牺牲者。

假设我们有三个缓存 Key：

```text
activity:1001:cache
activity:1002:cache
activity:1003:cache
```

随机选择其中任何一个，都可能释放出一些空间。但业务很快会遇到新的矛盾：

活动 `1001` 每秒被请求数千次，而活动 `1003` 已经很久没人访问。如果随机选择，Redis 仍然可能把最热门的缓存删掉。

**我们真正缺少的不是“能否删除”，而是“如何尽量删除不太需要的数据”。**

## 7. 热门缓存应该留下：为什么需要 LRU？

面对热门活动和冷门活动，一个很直观的判断是：

> 最近刚被访问过的 Key，之后可能还会被访问；长时间没人使用的 Key，可以优先考虑淘汰。

这种依据“最近一次使用时间”选择数据的思路，称为 **LRU（Least Recently Used）**。

但我们不能直接把 LRU 理解成 Redis 一定会找到全数据库里绝对最久没访问的那个 Key。

先思考一种最容易理解的精确方案：

每次访问任意 Key，都将它移动到一条“最近使用顺序”的链表头部；内存不足时，从链表尾部淘汰最久未使用的 Key。

```text
最新使用                                    最久未使用
    |                                           |
    v                                           v
[1001] --> [1002] --> [1004] --> [1003]
                                         淘汰候选
```

这个思路在某些缓存设计中完全可行，但需要额外维护一份全局访问顺序。大量 Key 的每一次访问都可能引发链表更新，数据结构和操作成本也随之增加。

Redis 想要的效果是“尽量保留近期使用的数据”，而不是为每次访问都维护完全精确的全局顺序。

因此，Redis 的 LRU 淘汰采用**抽样近似**思路。

简单说：不用每次都搜索全部 Key，而是在一部分随机样本中寻找更值得淘汰的候选。

我们到源码里验证。

### 7.1 先从哪些 Key 里抽样？

对于 LRU、LFU 等策略，`performEvictions()` 同样先确定候选范围。

**真实源码节选 — `src/evict.c`：**

```c
dict = (server.maxmemory_policy & MAXMEMORY_FLAG_ALLKEYS) ?
       db->dict : db->expires;
```

然后将抽样与候选评分工作交给：

```c
evictionPoolPopulate(i, dict, db->dict, pool);
```

现在才需要认识一个新的内部函数 `evictionPoolPopulate()`。

它不是命令入口，而是 `performEvictions()` 在挑选淘汰候选时调用的辅助函数。输入包括当前数据库编号、抽样字典、Key 字典和候选池；它的作用是补充一批更值得淘汰的 Key。

### 7.2 为什么不遍历所有 Key？

进入 `evictionPoolPopulate()`，看到：

**真实源码节选：**

```c
dictEntry *samples[server.maxmemory_samples];
count = dictGetSomeKeys(sampledict,samples,server.maxmemory_samples);
```

这两行比抽象地背“Redis 采用近似 LRU”更有解释力。

Redis 没有在此扫描整个数据库，而是由 `dictGetSomeKeys()` 从候选字典中获取一批样本。样本数与 `server.maxmemory_samples` 有关。

这样做有明确的取舍：

- 样本少：寻找候选的成本相对较低，但可能错过真正很久没访问的 Key。
- 样本多：通常更接近想要的 LRU 效果，但筛选成本也会上升。

因此，`maxmemory-samples` 不是“精确性开关”，而是抽样质量与 CPU 开销之间的调节参数。

### 7.3 Redis 如何为样本评分？

对 LRU 策略，源码继续使用：

**真实源码节选：**

```c
if (server.maxmemory_policy & MAXMEMORY_FLAG_LRU) {
    idle = estimateObjectIdleTime(o);
}
```

这里的 `idle` 可以理解为距离最近使用已经过去的时间估计值。

时间越长，该 Key 通常越适合被淘汰。

`evictionPoolPopulate()` 会把候选按评分放入一个有限大小的候选池，`performEvictions()` 再从较优候选中选择真正要移除的 Key。

这样一来，系统没有维护一份精确的全量访问顺序，却仍然能倾向于优先淘汰较久没用的缓存。

**这正是 Redis 近似 LRU 的工程取舍：用有限的统计与抽样工作，换取通常足够好的淘汰判断。**

当然，某个冷 Key 并非必然先于所有热 Key 被删除。由于采用随机抽样，具体牺牲者和访问历史、采样结果、内存状态有关。

LFU（按访问频率近似判断）、`volatile-ttl`（优先选择剩余 TTL 更短的候选）也会复用相关候选池路径。但本章不深入其计数衰减和评分细节，因为它们不是理解当前 LRU 取舍所必需的内容。

## 8. 选出牺牲者之后，Redis 如何真正删除它？

我们已经知道 Redis 如何从候选中找出 `bestkey`。

但只选出 Key 不会释放任何内存。最终还得修改数据库的键值存储。

回到 `performEvictions()`，在确定 `bestkey` 后，可以看到：

**真实源码节选 — `src/evict.c`：**

```c
dbGenericDelete(db,keyobj,server.lazyfree_lazy_eviction,DB_FLAG_KEY_EVICTED);
```

这一步才真正发起对该 Key 的删除。

`DB_FLAG_KEY_EVICTED` 明确标示这是一次内存淘汰导致的删除。`server.lazyfree_lazy_eviction` 还会影响内存释放是否采用延迟释放路径。

再看相邻的一个统计更新：

```c
server.stat_evictedkeys++;
```

因此，我们不仅可以从业务读写结果看到某些 Key 消失，还能通过 Redis 的统计信息查看已经发生了多少次淘汰。

在 `INFO stats` 中，对应指标为 `evicted_keys`。

到这里，一次内存淘汰的主要链路已经闭合：

```text
命令进入 processCommand()
             |
             v
      performEvictions()
             |
             v
      getMaxmemoryState()
             |
       内存已超出预算
             |
             v
          根据策略
             |
    +--------+---------+
    |                  |
随机抽取候选       LRU 抽样与评分
    |                  |
    +--------+---------+
             |
             v
           bestkey
             |
             v
      dbGenericDelete()
             |
             v
        Key 被淘汰
```

这也是为什么我们不能把“内存淘汰”简单理解成 `DEL` 命令：它不是客户端指名某个 Key，而是 Redis 在内存压力下，自行决定候选并执行删除。

## 9. 实验一：`noeviction` 为什么会拒绝新的写入？

下面的实验只在独立的本地 Redis 实例中执行，**不要直接对生产 Redis 设置很小的 `maxmemory` 或执行 `FLUSHDB`**。

为了避免和前几章的默认实例混淆，我们使用宿主机端口 `6381`，容器名 `redis-eviction-lab`。

```bash
# 如果本地已经有同名容器，请先确认是否为实验容器后再处理。
docker run -d \
  --name redis-eviction-lab \
  -p 127.0.0.1:6381:6379 \
  redis:7.2.0 \
  --save "" \
  --appendonly no \
  --maxmemory 8mb \
  --maxmemory-policy noeviction
```

确认配置：

```bash
docker exec redis-eviction-lab redis-cli CONFIG GET maxmemory
docker exec redis-eviction-lab redis-cli CONFIG GET maxmemory-policy
```

因为 Redis 进程本身也占用内存，所以我们不把“恰好写入 N 条”作为断言。我们只关心内存压力持续增加后，出现了什么行为。

安装 Python 客户端：

```bash
python -m pip install "redis==5.0.1"
```

创建 `eviction_lab.py`：

```python
import redis
from redis.exceptions import ResponseError

HOST = "127.0.0.1"
PORT = 6381
PREFIX = "activity:1001:cache:"

client = redis.Redis(host=HOST, port=PORT, decode_responses=False)


def reset(policy):
    # 仅在上面的独立实验实例使用，切勿对生产环境执行。
    client.flushdb()
    client.config_set("maxmemory-policy", policy)
    client.config_resetstat()
    client.set("activity:1001:views", 100)


def fill_cache(limit=2000):
    payload = b"x" * (16 * 1024)
    success = 0
    failures = 0

    for i in range(limit):
        try:
            client.set(f"{PREFIX}{i}", payload)
            success += 1
        except ResponseError as exc:
            if "OOM" not in str(exc):
                raise
            failures += 1
            break

    stats = client.info("stats")
    return {
        "success": success,
        "failures": failures,
        "evicted_keys": stats.get("evicted_keys"),
        "views_exists": client.exists("activity:1001:views"),
    }


if __name__ == "__main__":
    reset("noeviction")
    print("noeviction:", fill_cache())

    reset("allkeys-lru")
    print("allkeys-lru:", fill_cache())
```

执行：

```bash
python eviction_lab.py
```

第一次运行 `noeviction` 阶段时，预期会发生：

- 连续写入缓存，内存逐步增长。
- 当内存超过预算且不允许淘汰时，某次写入收到 OOM 错误。
- `evicted_keys` 仍为 0，因为该策略不会主动牺牲已有 Key。
- 已有的活动计数不会仅仅因为设置了 `noeviction` 就被淘汰。

脚本会输出具体成功条数和失败次数，但**条数并不固定**：容器、内存分配器、Redis 运行状态都会影响什么时候达到预算，因此不要把某个数字写死为标准答案。

## 10. 实验二：换成 `allkeys-lru` 后发生什么？

同一个脚本的第二个阶段会重置实验数据库，并把策略换成：

```text
allkeys-lru
```

然后再写入 2000 条约 16 KiB 的缓存值。

总写入数据量明显大于设置的 8 MiB 预算，因此预期会出现淘汰行为。

我们主要观察两件事：

1. `evicted_keys` 是否变成大于 0 的数字。
2. 相同的内存预算下，Redis 是否能够通过移除部分已有 Key，继续接收后来的缓存写入。

注意：不要断言“缓存 `activity:1001:cache:0` 一定最先被删”，因为这是**近似 LRU** 而非精确排序算法；也不要断言 `activity:1001:views` 一定存在，因为 `allkeys` 意味着活动计数本身也可能成为牺牲者。

这正是实验要让我们意识到的设计代价。

**一旦选择 `allkeys-lru`，我们实际上允许 Redis 为了接纳新数据，提前删除任何 Key，包括尚未过期或根本没有设置 TTL 的 Key。**

对于纯缓存，它可能是合理取舍：丢了以后可以重新构建。

但如果 `activity:1001:views` 是唯一保存的权威计数，允许 Redis 任意淘汰它就可能造成业务数据丢失。

因此，**淘汰策略不能脱离数据的业务价值独立选择。**

如果我们希望只有临时缓存可以被提前删除，可以进一步考虑 `volatile-lru`：它从带过期时间的 Key 中选择候选。但如果所有可淘汰的 Key 都已经被移除，其他 Key 仍然占满内存，那么受 OOM 限制的写入仍然可能失败。`volatile` 并不等于“无论如何都能继续写”。

## 11. 我们究竟解决了什么？

回顾本章完整的因果路径：

```text
过期机制能够清理已经到期的数据
             |
             | 但高峰期大量 Key 尚未过期
             v
        内存预算可能不足
             |
             | 最小、保守的做法
             v
      maxmemory + noeviction
             |
             | 新缓存写入被拒绝
             v
      允许删除已有缓存
             |
             | 最简单的挑选方法
             v
         random 淘汰
             |
             | 可能删除最热门的缓存
             v
        需要近似判断冷与热
             |
             v
       LRU 随机抽样候选池
             |
             v
        删除候选并记录统计
```

现在我们可以明确回答每个机制的收益和边界：

- **TTL** 让不再有效的数据退出系统；但它不保证有效期内的全部数据都能装下。
- **`maxmemory`** 为 Redis 提供内存使用预算；但它不是进程 RSS 的严格上限。
- **`noeviction`** 避免主动牺牲已有 Key；代价是可能拒绝受 OOM 限制的写操作。
- **随机淘汰** 让 Redis 在预算不足时有机会继续接收数据；代价是可能误删热门 Key。
- **近似 LRU** 倾向于选择很久没访问的 Key，降低无差别删除的概率；代价是抽样判断并不保证找到绝对最冷的 Key。

另外，Redis 7.2.0 还支持 LFU 等策略，但它们应当在业务确实需要区分“最近访问”和“长期访问频率”时再进一步研究，而不是当作必须背诵的清单。

本章已经完成了对**有限内存下如何取舍数据**的核心解释。

但还留下了一个完全不同的问题。

## 12. 下一章：即使没有淘汰，Redis 崩溃后数据还在吗？

假设我们没有使用 `allkeys-lru`，而是选择保留活动访问计数，不允许它被提前淘汰。

我们执行：

```redis
SET activity:1001:views 100
INCR activity:1001:views
```

Redis 返回了成功结果。

过期时间还没到，内存也没有超限，Redis 也没有主动删除这个 Key。

但就在这时，Redis 所在机器突然断电。

**重新启动后，`activity:1001:views` 还会存在吗？**

我们前面研究的所有机制，都只讨论 Redis *正在运行时* 怎样管理内存中的数据。

然而，进程退出以后，内存里的状态不能理所当然地保留下来。

如果这份访问计数需要在故障后恢复，就必须研究另一层问题：如何把内存中的数据转化为能够在下次启动时重新加载的持久化状态。

下一章，我们就从“一次写入已经返回成功，但 Redis 进程突然终止”这个具体场景出发，逐步研究 Redis 的 RDB 与 AOF 机制。

**下一章：《Redis 重启后数据还在吗？从内存状态到 RDB 与 AOF》**

---

## 实验与源码核对说明

- 版本：Redis **7.2.0**，与前面章节保持一致。
- 本文实验展示的是**可复现的步骤与预期现象**，未在本文编写环境实际启动 Redis 容器，因而不声称成功条数或 `evicted_keys` 的具体数值已经经过实测。
- 代码中的清库与内存压力实验**只能用于独立实例**。
- 真实源码节选与教学伪代码已在对应位置区分；省略的检查、错误分支与后台释放细节不影响本章所讨论的主要因果路径。

## 源码与资料

- [Redis 7.2.0：`src/server.c`](https://github.com/redis/redis/blob/7.2.0/src/server.c) —— `processCommand()` 中的内存检查与 OOM 拒绝
- [Redis 7.2.0：`src/evict.c`](https://github.com/redis/redis/blob/7.2.0/src/evict.c) —— `getMaxmemoryState()`、`performEvictions()`、`evictionPoolPopulate()`
- [Redis 7.2.0：`src/db.c`](https://github.com/redis/redis/blob/7.2.0/src/db.c) —— `dbGenericDelete()`
- [Redis 7.2.0：`redis.conf`](https://github.com/redis/redis/blob/7.2.0/redis.conf) —— `maxmemory`、`maxmemory-policy` 与 `maxmemory-samples`
- [Redis 官方文档：Key eviction](https://redis.io/docs/latest/develop/reference/eviction/)

完成实验后，如果不再需要这个实例，可以执行：

```bash
docker rm -f redis-eviction-lab
```
