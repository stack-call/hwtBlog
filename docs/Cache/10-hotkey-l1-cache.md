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
> 阅读顺序：建议先完成第 09 章。本文所有例子继续使用 `GET /posts/:id` 与 `post:42`；独立失败场景只是修改请求时间、并发度、文章 ID 或缓存状态。


此前，所有应用实例都会查询共享的 Redis。这个结构很好地保护了 MySQL：只要 Key 命中，就不会回源。但当文章 42 极度热门时，我们发现 Redis 的读取量也可能成为瓶颈。


## 命中率 99%，为什么仍然要优化？

一路走来，我们通过 Cache-Aside 让大部分文章详情不再访问 MySQL，又引入 TTL、失效与容量淘汰。监控显示 Redis 命中率很高，数据库负载已经平稳。看起来缓存体系可以告一段落。

但现在文章 42 突然上了热搜。所有人都在访问同一个 URL。10 万个请求中，绝大多数都直接命中 Redis，所以数据库几乎没有新增压力，Redis 的一个处理路径却变得异常繁忙：大量网络连接和命令都在反复读取同一个小对象。

这里需要澄清：**缓存命中率衡量的是有没有避开数据库，不衡量 Redis 的单 Key QPS 是否超过某个节点可承受的处理能力**。所以“命中率高”不等于“所有层都足够快”。

## 我们以前已经写过一个更近的缓存

第一章的进程内字典并没有错，只是第二章出现多个应用实例时，它们无法共享首次加载的结果。现在需求反过来了：热点文章 42 被同一个实例反复请求，是否可以让它在应用自己的内存里短暂命中，避免每次再走一次到 Redis 的网络请求？

```text
原路径：Client → App → Redis → App → Client
新路径：Client → App → 本地命中 → Client
                   ↓ 本地未命中
                   Redis → MySQL（必要时）
```

这不是突然引入陌生组件，而是**把第一章最简单的本地缓存重新作为额外的一层**。原有 Redis 并没有被替换，仍然负责跨进程共享缓存；本地缓存只接收经过筛选的热点小对象。

## 先算清楚收益是在哪里出现的

设应用 A 每秒收到文章 42 的 2000 次请求。没有 L1，每个请求都需要访问 Redis，约 2000 次/秒。加入 L1 后，在同一段有效期里第一次请求从 Redis 加载，之后 1999 次如果都能在本地命中，那么 Redis 对这部分流量只有极少量请求。

这并不是免费的。假设我们有 20 个应用实例，每个实例都可能保留这份文章对象，内存被重复占用；更重要的是，管理员更新文章后，**A 的 L1 与 B 的 L1 会不会继续返回旧版本？** 目前我们没有回答，这恰好是下一章的主问题。

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

## 把 L1 与 L2 的命中责任分开

本章最简单的策略是：只有热点文章 42 允许进入 L1，其他文章继续走 Redis；L1 的 TTL 明显短于 L2，并限制条目数。这样虽然不能自动解决一致性，但至少可以限制旧值的保留时间和进程内内存增长。

一个读请求的判定顺序必须清楚：先查本地；本地未命中才查 Redis；Redis 未命中才查 MySQL。**只有 Redis 中已经拿到可信的正向文章结果，才考虑放入本地**。对于第七章的 NOT_FOUND，也需要单独决定是否可进入 L1，不能无差别复制所有字符串。

真实 Caffeine 的 `getIfPresent`、`put`、`invalidate` 等接口分别对应本地查找、加入和失效；它们不负责访问 Redis，也不替业务做数据库一致性协调。我们在此只用它们解释本地缓存的真实接口边界，不伪造 Caffeine 内部 Java 方法实现。

另外，Local Cache 的竞争不是免费的：每个应用实例会有独立内存预算、对象序列化成本和 GC 影响。如果热点 Key 数量很多，不能让每个热点都无限期存入进程内；否则只是把 Redis 的内存压力搬进应用机器。

## 再把本章主张写成可反驳的测试

在读源码之前或之后，我们都应该能用一句话陈述本章能够验证的命题。这里的命题不是“缓存体系变得更好”，而是 **L1 减少 Redis GET**。如果实验不能区分旧方案与新方案，就说明我们引入的机制没有得到证据支持。

具体操作是：只让文章 42 进入 L1，模拟 1000 次顺序请求，比较无 L1 与有 L1 的 Redis GET 计数；另测文章 43 不入 L1。

为了避免只挑对自己有利的结果，运行时至少记录四类信息：请求的输入 ID、每次缓存命中或未命中、数据库实际被调用的次数或并发数、请求最终拿到的版本或状态。遇到并发问题时，另外记录明确的先后顺序，而不能用“偶尔发生”代替复现。

**验证边界**：高命中率不保证 L1 多实例之间及时失效；本地对象也占内存。 这条限制应该和主张并排出现，而不是隐藏在文末。现实中是否值得采用，还必须基于请求分布、数据大小与业务容忍度做权衡。

你可以把本章的最小实验复现为一个反向练习：先故意关掉新机制，确认失败重新出现；再开启新机制，确认预期行为改变。只有能解释“关掉以后为什么退化”，才算真的理解了它。

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


## 应该怎样验证“热点被 L1 接住了”？

固定 1000 次访问文章 42 的请求，把 Redis `GET` 的调用次数作为指标；无 L1 时接近 1000，有 L1 且未过期时可降到 1 左右。另做一组随机文章请求，验证它们没有因为“只缓存热点”而无限占用 L1。

不要拿这个模型的数字预测真实网络延迟。生产收益还要测 L1 查找成本、Redis 网络往返、内存占用、GC 和更新频率。但机制级实验足以验证“请求先被本地命中便不会继续到 Redis”这条因果链。

问题随之出现：应用 A 的本地缓存已经被失效了，但应用 B 还保存旧标题。我们不可能每次更新都手动登录所有实例执行删除。下一章才需要讨论跨实例的失效通知。

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
