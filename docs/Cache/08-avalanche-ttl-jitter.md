---
title: "缓存体系 08：一千个 Key 同时过期，数据库为什么突然扛不住？"
sidebar_label: "八、一千个 Key 同时过期"
sidebar_position: 8
description: "从相同 TTL 的批量过期出发，实验比较集中到期和错峰到期。"
slug: /cache-system/08-avalanche-ttl-jitter
tags:
  - caching
  - redis
  - source-code
---

# 一千个 Key 同时过期，数据库为什么突然扛不住？

前一篇处理了不存在文章的重复查询。现在我们访问一批真实文章，例如 `GET /posts/42` 到 `GET /posts/1042`。这些 Key 都被正确缓存了，没有穿透问题；第 5 章的 Singleflight 也存在，能够合并同一 Key 的并发回源。

## 缓存里有 1000 个不同 Key，为什么仍会集中回源？

假设一次预热脚本将它们在同一秒全部写入 Redis，每个 Key 统一设置 `EX 60`。60 秒后，用户恰好继续请求这些文章；它们一起过期。

```text
第 60 秒：
  post:42    miss → DB
  post:43    miss → DB
  post:44    miss → DB
  ...
  post:1042  miss → DB
```

第 5 章的 Singleflight 能把“同一个 post:42 的 24 个请求”压成 1 次，但现在有 1000 个**不同** Key，每一个都可以合法地有自己的第一次回源。所以只靠单 Key 请求合并不够。

这类许多 Key 同时失效、造成下游集中承压的情形，常称**过期型缓存雪崩**。它和 Redis 整体故障造成的雪崩不同；后者到收官篇才处理。

## 先别改架构：把到期时刻错开

如果批量写入时统一用 `ttl=60`，我们可以只做一个改动：给不同 Key 的 TTL 加上小范围抖动。例如基础有效期 60 秒，不同 Key 取 60–89 秒。

```python
# 教学化简：预热时为每个 Key 选择不同 TTL
import random
random.seed(42)
for post in posts:
    ttl = 60 + random.randint(0, 29)
    redis.set(f"post:{post.id}", encode(post), ex=ttl)
```

这叫 TTL Jitter（过期抖动）。它没有让数据库变快，而是希望把集中到期分散成多个时间片，使回源峰值不那么尖锐。要注意：如果流量本身在某一秒全部冲向所有已到期 Key，随机 TTL 未必能分散它们的**实际查询时刻**；它主要缓解到期驱动的请求波峰，效果依赖请求模式与预热方式。

## Redis 源码如何支持不同的到期时间？

固定 Redis 7.2.4。`SET ... EX` 在 [`src/t_string.c`](https://github.com/redis/redis/blob/7.2.4/src/t_string.c) 的 `setGenericCommand` 中解析相对 TTL 并调用 `setExpire`。`EXPIRE` 命令则通过 [`src/expire.c`](https://github.com/redis/redis/blob/7.2.4/src/expire.c) 的 `expireGenericCommand` 将秒数换算为绝对时刻。源码不会擅自为应用生成一段随机 TTL：

**真实源码节选（Redis 7.2.4）：**

```c
void expireCommand(client *c) {
    expireGenericCommand(c,commandTimeSnapshot(),UNIT_SECONDS);
}
```

所以 Jitter 不是 Redis 服务端自动启用的“抗雪崩开关”，而是调用者选择多个不同的 `EX` 参数。过期逻辑本身仍由 Redis 的读取检查与 `activeExpireCycle` 处理。

## 做一个只改变到期时间的实验

为了将机制效果和请求模式分离，我们先比较**同一秒计划到期的 Key 数量**，而不是虚构某个真实系统的 QPS。脚本构造 120 个不同 Key：原设计统一在第 60 秒到期，新设计固定分成 30 个不同秒数。

```text
08: 120 distinct keys: peak same-second expiry 120 vs staggered 4
```

执行 `python3 examples/cache_labs.py 08`。这证实了“单秒到期数”从 120 降为 4。实验没有真实 Redis、没有模拟不均匀访问时间；**不能直接从该数字推断数据库 QPS 必然下降 30 倍**。要度量真实收益，还应采集每秒未命中数、回源并发数、P99 和数据库资源使用率。

## 它与缓存击穿、穿透到底差在哪？

- **穿透**：Key 对应的数据本来就不存在；同一不存在 ID 反复回源。
- **击穿**：单个热点 Key 失效；大量并发请求同一个 Key。
- **过期型雪崩**：大量不同 Key 集体失效；每个 Key 都需要独立回源。

术语只是方便沟通，排查时真正重要的是输入分布和时间线。

到这里，我们已经给缓存设置了 TTL，也把大量 Key 的到期时刻错开。可出现一个新情形：Redis 只给缓存 1 GB 内存，而当前仍未过期的对象已经占满 1 GB。Redis 不能等 TTL 才释放空间，这就是下一篇要解决的容量压力。

## 实验与验证边界

本章的最小实验可以在仓库根目录执行：

```bash
python3 examples/cache_labs.py 08
```

实验只使用 Python 标准库，已经在制作文档时实际执行。它验证当前章节描述的**教学模型**，而不是 Redis、MySQL、Go 或 Java 中间件的集成性能。真实系统的行为应以固定版本源码、配置和实际环境测试为准。

## 固定源码与参考资料

- [Redis 7.2.4 `src/t_string.c` — SET EX](https://github.com/redis/redis/blob/7.2.4/src/t_string.c)
- [Redis 7.2.4 `src/expire.c` — `expireGenericCommand`、`activeExpireCycle`](https://github.com/redis/redis/blob/7.2.4/src/expire.c)
