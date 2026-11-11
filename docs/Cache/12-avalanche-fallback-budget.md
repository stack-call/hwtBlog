---
title: "缓存体系 12：Redis 整体故障后，为什么数据库也跟着崩溃？"
sidebar_label: "十二、Redis 故障后数据库也崩了"
sidebar_position: 12
description: "从整层缓存失效引出有限回源预算，完成缓存体系架构的因果收束。"
slug: /cache-system/12-avalanche-fallback-budget
tags:
  - caching
  - redis
  - source-code
---

# Redis 整体故障后，为什么数据库也跟着崩溃？
> 阅读顺序：建议先完成第 11 章。本文所有例子继续使用 `GET /posts/:id` 与 `post:42`；独立失败场景只是修改请求时间、并发度、文章 ID 或缓存状态。


我们终于把原先的单机字典，逐步演化成了两个应用实例的多级缓存体系：L1 负责消化热点读，Redis L2 负责共享结果，MySQL 保存权威文章数据。它能处理许多正常情况下的请求，却还没有经受最后一种失败：**Redis 整体不可用**。


## 最后的故障不是一个 Key，而是一整层缓存

前面的章节每次都给一个明确的局部压力：文章标题更新、同 Key 并发 miss、很多 Key 集中过期、热点访问、应用 L1 失效。我们已经有不少保护措施，但都默认 Redis 这一层能够正常连接。

现在假设 Redis 因网络故障整体不可达，应用 A/B 对 Redis 的请求同时超时。业务代码如果简单写成 `try redis.get() except: return db.read()`，等于在故障发生的同一刻让原本被 Redis 承担的请求全部转到 MySQL。

```text
Redis 正常时：10,000 req/s → 9,800 Redis hit + 200 DB query
Redis 不可用：10,000 req/s → 最多 10,000 DB query
```

数字是故意构造的负载示意，不是实际性能预测。更关键的是请求的**形状**：从一个可缓存、低成本的读取突然变成大量并发数据库查询。我们之前的 Singleflight 能合并同 Key，但如果这 10,000 个请求分散在大量不同文章上，DB 仍可能被冲击。

这也是缓存雪崩的另一种触发条件——整层缓存不可用。第八章的 TTL 抖动针对的是“多个 Key 失效时刻一致”，对 Redis 整体网络故障没有作用。

## 先否定一个不合理的目标

当 Redis 整体不可用，而且 MySQL 只能安全处理有限并发时，我们不可能同时承诺“所有请求都成功”“延迟不增加”“数据库绝不超载”。如果试图让所有请求排队等数据库，队列本身也会增长，超时与重试还可能进一步放大负载。

因此本章最先做的取舍是**允许部分请求明确失败或降级，以保护数据库继续工作**。这不是优化失败，而是灾难情景下有界退化的设计目标。

## 给回源加一个计数明确的闸门

最小的机制是一组许可：只有拿到许可的请求可以查询 DB；其他请求马上得到 503 或业务允许的降级结果。许可数不是全局真理，只是我们给当前应用实例设定的资源预算。

```python
from threading import BoundedSemaphore
slots = BoundedSemaphore(3)

def fallback(post_id):
    if not slots.acquire(blocking=False):
        return {"status": 503, "reason": "db_budget_exhausted"}
    try:
        return db.read(post_id)
    finally:
        slots.release()
```

`finally` 尤其重要：数据库抛异常也必须释放许可，否则短暂故障会让闸门永久耗尽。这个机制只限定进程内并发；如果有十个应用实例都设为 3，那么全局最多可以有约 30 个并发回源请求，必须结合数据库总预算计算。

## “缓存不可用，就查 DB”为什么危险？

在正常读路径中，Redis miss 之后查数据库是合理的。可 Redis 请求失败并不等于缓存中“确实没有这个 Key”。如果把这两者都当成普通 miss，后果可能是：

```text
Redis 正常：
10,000 次请求 → 大部分命中 L1 / L2 → 少量访问 MySQL

Redis 整体故障：
10,000 次请求 → 大部分 L2 不可用 → 大量直冲 MySQL
```

尤其当本地 L1 只覆盖少数热点文章时，其他文章请求仍会在同一时间回源。数据库连接池、CPU 或锁竞争可能随之耗尽，从缓存故障演化成数据层故障。这是**整层缓存故障型雪崩**，与第 8 章“很多 Key 同时过期”属于不同的触发原因。

那么是否应该简单地禁止所有回源？这虽然安全，却让本来仍能由数据库服务的一小部分请求也失败。我们可以给回源设置一个**有限预算**：系统最多允许一定数量的请求同时访问数据库；超额的请求尽快失败或明确降级。

## 只增加一个机制：回源并发闸门

最简单的 Python 形式是：

```python
from threading import BoundedSemaphore

fallback_budget = BoundedSemaphore(3)

def guarded_db_fallback(post_id):
    if not fallback_budget.acquire(blocking=False):
        return {"status": 503, "reason": "degraded"}
    try:
        return db.read(post_id)
    finally:
        fallback_budget.release()
```

这里的 3 只是教学数字，不是生产配置建议。它限制**当前应用实例**同时进行的 DB 回源数量，超额明确拒绝；不保证每个请求成功。真实系统还需要考虑总实例数、DB 连接池、查询耗时、优先级、超时和故障检测，不能把每个实例配置 3 就宣称全局上限为 3。

我们之所以需要这种机制，是因为**当下游已经处于资源压力下，额外尝试未必提高成功率，反而可能把它拖垮**。设计目标从“尽量所有请求成功”改成“有限且可预测地退化”。


## 为什么在应用层读限并发库源码？

我们要保护的是 Redis 失效以后的数据库访问，所以核心机制不可能在 Redis 的 `GET` 处理函数中。真实应用库 Resilience4j 的 SemaphoreBulkhead 用信号量管理进入资源边界的权限。固定版本 v2.2.0，观察 `tryEnterBulkhead` 如何尝试获取许可，以及 `releasePermission` / `onComplete` 如何释放许可。

调用者是应用的“准备进行数据库回源”路径；输入是配置的最大同时调用数与等待策略；输出是是否允许执行这次下游操作。成功进入以后才能执行 DB 查询，失败则按照应用策略直接降级。

这对源码阅读有一个通用提醒：**先确定问题发生在哪一层，再去那一层寻找真实实现**。如果为了凑 Redis 源码精读，把所有防护都写成 Redis 命令，反而会误导读者。

## 并发预算和请求速率不是同一回事

允许同时三个回源，并不等于每秒最多三个查询。如果每次查询只需 1 毫秒，三个许可在 1 秒内可以被重复使用很多次；如果每次查询花 2 秒，吞吐又会显著下降。

因此，保护数据库时还需要观察总查询速率、连接池占用、超时、排队长度和重试行为。但本章先完成一个清晰机制：**Redis 故障时数据库同时在执行的回源数不能超过设定值**。一旦这个约束能被真实实验验证，我们才有基础讨论更复杂的限流或熔断策略。

## 真实库怎么处理“许可”？

这不是 Redis `GET` 的内部能力，而是应用调用 DB 之前的资源保护逻辑。一个真实 Java 实现是 Resilience4j 的 SemaphoreBulkhead。固定 **v2.2.0**，打开 [`SemaphoreBulkhead.java`](https://github.com/resilience4j/resilience4j/blob/v2.2.0/resilience4j-bulkhead/src/main/java/io/github/resilience4j/bulkhead/internal/SemaphoreBulkhead.java)。它维护一个 `Semaphore`；判断能否进入资源边界时调用 `tryEnterBulkhead()`，后者用配置的最长等待时间获取许可。

**真实源码节选（Resilience4j v2.2.0）：**

```java
long timeout = config.getMaxWaitDuration().toMillis();
return semaphore.tryAcquire(timeout, TimeUnit.MILLISECONDS);
```

对照教学模型：`acquire` 成功就是获得一次回源许可；未获得许可时不应继续请求 DB；调用完成后必须释放。这里引用的是**真实已有的限并发机制**，但怎样定义“Redis 不可用时走 DB”、怎样返回 HTTP 503、是否允许读过期值，仍属于应用层策略，Resilience4j 不会自动替你决定。

也可以对照 Python 3.12.8 的 [`Lib/threading.py`](https://github.com/python/cpython/blob/v3.12.8/Lib/threading.py)，`BoundedSemaphore` 对释放超过初始许可数的情况进行检查；我们的实验便用标准库实现。

## 最后一次故障注入实验

执行 `python3 examples/cache_labs.py 12`。实验同时启动 30 个请求，假定 Redis 不可用，把它们送到回源闸门。最多允许 3 个并发 DB 查询，其他请求立即降级。

这次实际执行的一次结果：

```text
12: 30 requests during Redis outage: DB peak concurrency=3; fallback=3, degraded=27
```

数字只来自脚本中人为构造的并发场景；不同调度下成功数量可能变化，但断言**最大回源并发不超过 3、一定有请求明确被拒绝**。该实验不验证 Redis 实例故障的网络检测，也不测量真实 MySQL 连接池；不能由此推出生产系统“配置成 3 就不会宕机”。

## 从第 1 章回看整个系统

现在可以按因果次序画出我们最终认识的结构，而不是先抛给读者一张复杂总图：

```text
                  GET /posts/42
                         |
             Application A / B
                         |
                    L1 Cache
                         | miss
                         v
                     Redis L2
                         | miss / degraded
                         v
                Fallback Budget Gate
                         |
                  MySQL (source of truth)

MySQL 更新后：删除 L2 → 通知/失效各实例 L1
应用侧辅助：同 Key 请求合并、版本门闩、负缓存、TTL Jitter
Redis 侧机制：TTL、键淘汰与内存预算
```

这个系统不是“缓存越多越好”的证明。它包含的机制是根据**我们确实见过的失败**逐层增加的：重复 DB 查询、两实例重复查询、更新后旧值、并发旧值回填、热点 miss 放大、冷 Key 长期占用、不存在 ID 穿透、批量过期、内存上限、热点 Redis 请求、多个 L1 的旧值以及整层缓存故障。

## 最后，哪些保证仍然没有获得？

- Cache-Aside 加 `DEL` 不是强一致事务；DB 与 Redis 之间仍有部分失败和时序窗口。
- Singleflight 合并的是同进程同 Key 的重叠加载，不能保证全局只回源一次。
- 负缓存可能在短 TTL 内遮挡新创建的数据。
- Jitter 减少集中到期，不保证数据库绝对不超载。
- Redis 的容量淘汰不保证总留下“最重要”的业务对象。
- L1 可能因消息丢失或回填交错而短暂甚至长期落后，具体取决于额外防护。
- 回源预算保护资源，也意味着一些合法请求会被拒绝或降级。

这正是完整缓存体系的工程取舍：**性能、成本、可用性、一致性和故障下的行为需要被明确设计，而不是靠一个 Redis Cluster 开关自动获得。**

如果以后继续阅读源码，可以再研究 Redis 的实际过期与内存抽样策略、Caffeine 的缓存维护机制、真实连接池与限流实现、CDC 驱动的失效流程。但这些是后续独立问题，不必为了“覆盖所有名词”塞进当前的因果链。


## 用三个故障测试约束最终设计

第一组：Redis 正常。绝大多数请求由 L1/L2 缓存承担，回源闸门很少被触发。第二组：Redis 整体故障且 30 个请求同时到达。允许 3 个回源，其他请求明确拒绝，断言最大并发不大于 3。第三组：在某个获得许可的 DB 查询内部抛异常，确认 `finally` 能归还许可，否则后续正常请求也无法通过。

这里有两个不能夸大的结论：教学模型不测试真实 MySQL 的承受能力，且“最大并发不超过 3”只约束一个应用进程。用户体验是否可接受，还取决于业务是否允许返回过期内容、哪些接口优先服务、是否具备重试风暴保护。

## 从一条文章查询回看整个系列

第 1 章的系统只有 `SELECT MySQL`；重复读取促使我们加本地缓存。第 2 章实例扩容让本地状态无法复用，于是加共享 Redis。第 3、4 章把数据库更新后的失效与慢读回填分开研究。第 5 章遇到单 Key 并发 miss。第 6、7、8 章分别解决时间边界、不存在 ID 的重复回源和多个 Key 集中过期。第 9 章研究容量预算。第 10、11 章把本地缓存重新放回链路，并处理跨实例失效。最后，Redis 整体故障迫使我们承认：**缓存体系不只追求“正常情况下尽可能快”，还要设计“故障情况下哪些代价可被控制”**。

没有哪一个机制可以替代其他所有机制。它们依次出现，是因为前一个最简单方案在新条件下暴露了具体限制。将来面对别的业务时，不必机械部署全部组件；重新从请求分布、正确性需求、容量限制和故障概率出发即可。


## 再把本章主张写成可反驳的测试

在读源码之前或之后，我们都应该能用一句话陈述本章能够验证的命题。这里的命题不是“缓存体系变得更好”，而是 **整层缓存故障时的回源闸门**。如果实验不能区分旧方案与新方案，就说明我们引入的机制没有得到证据支持。

具体操作是：在 Redis 不可用的前提下，让 30 个线程同时回源并使用容量为 3 的信号量；统计允许与降级数量，断言并发峰值≤3。

为了避免只挑对自己有利的结果，运行时至少记录四类信息：请求的输入 ID、每次缓存命中或未命中、数据库实际被调用的次数或并发数、请求最终拿到的版本或状态。遇到并发问题时，另外记录明确的先后顺序，而不能用“偶尔发生”代替复现。

**验证边界**：限并发以牺牲一部分成功率保护数据库；它不自动限制每秒速率。 这条限制应该和主张并排出现，而不是隐藏在文末。现实中是否值得采用，还必须基于请求分布、数据大小与业务容忍度做权衡。

你可以把本章的最小实验复现为一个反向练习：先故意关掉新机制，确认失败重新出现；再开启新机制，确认预期行为改变。只有能解释“关掉以后为什么退化”，才算真的理解了它。

## 实验与验证边界

本章的最小实验可以在仓库根目录执行：

```bash
python3 examples/cache_labs.py 12
```

实验只使用 Python 标准库，已经在制作文档时实际执行。它验证当前章节描述的**教学模型**，而不是 Redis、MySQL、Go 或 Java 中间件的集成性能。真实系统的行为应以固定版本源码、配置和实际环境测试为准。

## 固定源码与参考资料

- [Resilience4j v2.2.0 — `SemaphoreBulkhead.tryEnterBulkhead`](https://github.com/resilience4j/resilience4j/blob/v2.2.0/resilience4j-bulkhead/src/main/java/io/github/resilience4j/bulkhead/internal/SemaphoreBulkhead.java)
- [CPython 3.12.8 — `Lib/threading.py` 的 `Semaphore`、`BoundedSemaphore`](https://github.com/python/cpython/blob/v3.12.8/Lib/threading.py)
- [Redis 7.2.4 `src/t_string.c` — 缓存命中/未命中的底层行为](https://github.com/redis/redis/blob/7.2.4/src/t_string.c)
