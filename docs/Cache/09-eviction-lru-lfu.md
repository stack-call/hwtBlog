---
title: "缓存体系 09：还没过期的缓存，为什么也会被 Redis 删除？"
sidebar_label: "九、没过期为什么也会被删掉"
sidebar_position: 9
description: "区别 TTL 和容量淘汰，读 Redis 内存压力与采样淘汰源码。"
slug: /cache-system/09-eviction-lru-lfu
tags:
  - caching
  - redis
  - source-code
---

# 还没过期的缓存，为什么也会被 Redis 删除？

第八章让大量 Key 的到期时间错开，从而减少某一秒集中的逻辑失效。但一个新问题出现了：Redis 缓存的预算只有 1 GB，而新写入的文章详情总共占到 1.2 GB。它们大都还没有到期。

## 只等待 TTL，内存问题不会消失

TTL 解决“什么时候不再视作有效数据”，并没有保证一段时间内缓存总量不超过预算。如果所有 Key 都设置 1 小时 TTL，却在 5 分钟内插入了比预算更多的数据，Redis 仍然要作出选择。

最简单的选择是不再接收新写入。它保留旧 Key，但新缓存无法创建。对于纯缓存场景，我们也可以选择删掉部分旧缓存，为新数据腾空间。现在的问题变成：**应该牺牲哪些 Key？**

如果总是淘汰最早写入的 Key，可能误删频繁访问的热门文章。试着引入一个局部规则：最近被访问的 Key 更可能继续被访问。

## 用三格缓存亲手推导 LRU

容量只有三个条目，输入的访问序列是：`A → B → C → A → D`。

```text
A → [A]
B → [A, B]
C → [A, B, C]
A → [B, C, A]    # A 刚被访问，移动到最近
D → [C, A, D]    # B 最久未被访问，被淘汰
```

这就是 LRU（Least Recently Used）的核心直觉：**让最近使用的条目留下**。我们之前学过的 TTL 仍然存在；一个 Key 可能因为过期而失效，也可能在还未过期时由于内存压力被淘汰。这两种删除原因不同。

我们没有必要立即展示所有替代策略。只在遇到“某篇文章长期频繁访问，但中间短暂没被访问，仍可能被 LRU 淘汰”的情况时，才考虑根据访问频率估计价值的 LFU（Least Frequently Used）。

## Redis 的 LRU 和教科书算法有什么区别？

我们打开固定版本 Redis 7.2.4 的 [`src/evict.c`](https://github.com/redis/redis/blob/7.2.4/src/evict.c)。`performEvictions()` 在内存限制检查后，按照配置的 `maxmemory-policy` 决定是否需要挑选 Key；如果配置为 `noeviction`，它不会为了腾出空间而随意删除缓存键，可能使需要增加内存的写命令报错。

**真实源码节选（Redis 7.2.4 `src/evict.c`）：**

```c
if (server.maxmemory_policy == MAXMEMORY_NO_EVICTION) {
    result = EVICT_FAIL;
```

为什么不直接维护每个 Key 的严格全局 LRU 双向链表？因为给所有对象的每次访问都更新全局顺序，会增加维护开销。Redis 的 `evictionPoolPopulate(...)` 会对候选 Key 进行采样，按照估计的空闲时间或频率等信息选择淘汰对象。

在同一文件中，这条关键调用展示了抽样的存在：

```c
count = dictGetSomeKeys(sampledict,samples,server.maxmemory_samples);
```

它的调用上下文是构建用于淘汰选择的候选集合；输入是相应数据库中的字典、候选池和采样数量；输出是一批用于打分的 Key。随后实际淘汰逻辑处理这些候选。**这说明 Redis 的 LRU/LFU 是近似、基于采样的策略**，不能拿我们教学模型的精确淘汰顺序去预测真实 Redis 在任意负载下必然删除哪一个 Key。

配置示例：

```conf
maxmemory 1gb
maxmemory-policy allkeys-lru
```

`allkeys-lru` 会在全部 Key 中选择淘汰候选；`volatile-lru` 只在设置了过期时间的 Key 中选择。如果选择空间里没有合适 Key，新写入仍可能失败。因此配置本身必须与业务存储用途匹配，不要对一个混放重要持久数据的 Redis 实例随意启用淘汰。

## 实验：严格 LRU 的教学结果

执行 `python3 examples/cache_labs.py 09`：

```text
09: capacity=3, trace=A B C A D: evicted=B, resident=['C', 'A', 'D']
```

它只验证了 **OrderedDict 模拟的精确 LRU**。与 Redis 真实近似采样算法不同，真实 Redis 受采样率、值大小、内存计算方式以及命令时序影响。想验证特定 Redis 配置的表现，应另外启动固定版本实例并记录 `INFO stats` 中的 `evicted_keys`、命中率和内存使用曲线。

## 小结与新的压力

现在缓存既有逻辑过期时间，也可以在超过内存预算时主动淘汰。可是我们观察到 Redis 整体命中率达到 99%，仍有一个很奇怪的问题：文章 42 在热搜期间每秒受到海量请求，**即使几乎都命中 Redis，拥有这个 Key 的节点仍可能变成瓶颈**。下一篇我们不再优化 DB，而是优化 Redis 前面的请求路径。

## 实验与验证边界

本章的最小实验可以在仓库根目录执行：

```bash
python3 examples/cache_labs.py 09
```

实验只使用 Python 标准库，已经在制作文档时实际执行。它验证当前章节描述的**教学模型**，而不是 Redis、MySQL、Go 或 Java 中间件的集成性能。真实系统的行为应以固定版本源码、配置和实际环境测试为准。

## 固定源码与参考资料

- [Redis 7.2.4 `src/evict.c` — `performEvictions`、`evictionPoolPopulate`](https://github.com/redis/redis/blob/7.2.4/src/evict.c)
- [Redis 官方 Key Eviction 文档](https://redis.io/docs/latest/develop/reference/eviction/)
