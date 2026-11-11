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

我们终于把原先的单机字典，逐步演化成了两个应用实例的多级缓存体系：L1 负责消化热点读，Redis L2 负责共享结果，MySQL 保存权威文章数据。它能处理许多正常情况下的请求，却还没有经受最后一种失败：**Redis 整体不可用**。

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
