---
title: "缓存体系 10：缓存命中率已经很高，为什么 Redis 仍被一个 Key 打满？"
sidebar_label: "十、命中率很高 Redis 还是被打满"
sidebar_position: 10
description: "从单个热点 Key 的读取路径出发，为应用引入 L1 本地缓存。"
slug: /cache-system/10-hotkey-l1-cache
tags:
  - caching
  - redis
  - source-code
---

# 缓存命中率已经很高，为什么 Redis 仍被一个 Key 打满？

此前，所有应用实例都会查询共享的 Redis。这个结构很好地保护了 MySQL：只要 Key 命中，就不会回源。但当文章 42 极度热门时，我们发现 Redis 的读取量也可能成为瓶颈。

## 命中率高，不代表链路没有压力

想象这样一个系统：

```text
10 个 Application 实例
       |
       | 大量 GET post:42
       v
    Redis (该 Key 所在节点)
       |
     MySQL
```

Redis Cluster 能把**不同 Key** 放到不同 Slot/节点，却不会自动把同一个 `post:42` 的所有请求均匀分散给每个 Master。我们已经没有太多 DB miss，却仍有每个请求到 Redis 的网络往返和热点节点 CPU/网络开销。

最直接的改动不是把 Redis 再复制一遍给客户端，而是恢复第一章学过的进程内缓存。不过这次它不是唯一缓存，而是在共享 Redis 前面加一层：

```text
GET /posts/42
      |
Application A 的 L1
      | miss
     Redis L2
      | miss
     MySQL
```

这里的 L1 是应用本地内存；L2 才是共享缓存。只有 L1 没命中时才需要请求 Redis。于是热点读流量可能被同一个应用进程内消化。

## 用一个最小 Java API 表达 L1

如果应用使用 Java，可以用 Caffeine 这样的本地缓存库。下面是**基于公开 API 的教学示例**，不是库内部源码：

```java
Cache<Long, Post> l1 = Caffeine.newBuilder()
    .maximumSize(10000)
    .expireAfterWrite(Duration.ofSeconds(3))
    .build();

Post p = l1.getIfPresent(42L);
if (p == null) {
    p = loadFromRedisOrDb(42L);
    l1.put(42L, p);
}
```

现在只需要理解两件事：`getIfPresent` 如果本地命中就立即返回，`put` 保存一次结果。Caffeine 的 `Cache.get(key, mappingFunction)` 也能把同 Key 的计算合并在一个原子加载操作中，相关 API 可以参考 [Caffeine Population 文档](https://github.com/ben-manes/caffeine/wiki/Population)。不要把它误认为 Redis 自己提供了应用内存缓存。

Redis 仍保留之前的 `GET` 路径。固定 Redis 7.2.4 的 [`src/t_string.c`](https://github.com/redis/redis/blob/7.2.4/src/t_string.c) 里，`getCommand` 仍将请求交给 `getGenericCommand`。本章的优化**并不是改进了这几行 Redis 源码**，而是让一些请求根本不再调用它。

**真实 Redis 源码节选：**

```c
void getCommand(client *c) {
    getGenericCommand(c);
}
```

这也是一种重要的源码阅读方法：一个性能改进未必来自底层函数变快，有时来自**上层不再频繁调用该函数**。

## 实验：同样 1000 次读取，Redis 少了多少请求？

运行 `python3 examples/cache_labs.py 10`：

```text
10: 1000 hot-key reads Redis QPS count: without L1=1000, with L1=1
```

模拟程序用整数计数 Redis 访问次数，全部读取同一个稳定版本的文章。L1 第一次未命中后保存文章，后续 999 次都在本地获得值。注意它没有测量实际 QPS 或网络延迟；只是验证**请求路径的访问次数**。真实服务的收益取决于负载均衡的粘性、进程数量、L1 的命中率、对象大小和 TTL。

## 让系统“更快”，也让一致性更难

在只有一个应用实例时，L1 缓存更新可以由当前实例的业务路径直接触发。但现在有两个应用实例 A/B，各自拥有自己的 L1：

```text
Application A: L1 post:42 = v1
Application B: L1 post:42 = v1
Redis L2:      post:42 = v1
MySQL:         post:42 = v1
```

管理员请求刚好由 A 处理，将 MySQL 更新为 v2，再删除 Redis 与 A 的 L1。**B 的 L1 没有收到任何消息**，仍可能返回 v1。这不是 Redis 的错误，而是多级缓存新增的一份状态需要失效管理。下一篇只解决这个跨实例传播问题。

## 实验与验证边界

本章的最小实验可以在仓库根目录执行：

```bash
python3 examples/cache_labs.py 10
```

实验只使用 Python 标准库，已经在制作文档时实际执行。它验证当前章节描述的**教学模型**，而不是 Redis、MySQL、Go 或 Java 中间件的集成性能。真实系统的行为应以固定版本源码、配置和实际环境测试为准。

## 固定源码与参考资料

- [Redis 7.2.4 `src/t_string.c` — `getCommand`](https://github.com/redis/redis/blob/7.2.4/src/t_string.c)
- [Caffeine 官方 Wiki — Population / `getIfPresent`、`put`、`invalidate`](https://github.com/ben-manes/caffeine/wiki/Population)
- [Caffeine 3.1.8 发布记录（本章仅使用其公开 API 示例）](https://github.com/ben-manes/caffeine/releases/tag/v3.1.8)
