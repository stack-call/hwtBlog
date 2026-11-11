---
title: "缓存体系 06：没人再看的文章，为什么还占用缓存？"
sidebar_label: "六、没人看的文章还占用缓存"
sidebar_position: 6
description: "从无上限的保留时间引出 TTL，并阅读 Redis 的过期判断与扫描路径。"
slug: /cache-system/06-ttl-expiration
tags:
  - caching
  - redis
  - source-code
---

# 没人再看的文章，为什么还占用缓存？

前五章都在优化回源次数。现在文章 42 的访问量回到零，但 Redis 里仍然留着这条缓存。随着文章数量增加，长期未访问的文章会积累。我们不能只问“缓存命中时有多快”，还要问：**一个值应该被保留多久？**

## 从永久保存到有失效时间

最简单的方案是在写入缓存时记录它的过期时刻。比如写入时间为第 0 秒，保留 10 秒：

```text
post:42 -> v1
expires_at = 10s

请求发生在 t=9:  命中 v1
请求发生在 t=10: 未命中，重新访问 DB
```

这叫 TTL（Time To Live）。我们此刻的需求是**限制条目的有效期**，而不是在物理内存满的时候决定删谁；后者要到第九篇才出现。

应用与 Redis 可以使用：

```redis
SET post:42 '{"id":42,"title":"Old Title","version":1}' EX 10
TTL post:42
```

`EX 10` 让当前 Key 在 10 秒后过期，`TTL` 能读剩余秒数。对于读端，超过有效期的 Key 不应再作为正常命中返回。

## Redis 真正在什么时候判断过期？

我们固定 Redis 7.2.4。在 [`src/t_string.c`](https://github.com/redis/redis/blob/7.2.4/src/t_string.c) 的 `setGenericCommand`，设置值之后，如果命令带有过期参数，会调用 `setExpire` 保存过期时间；也可以通过 [`src/expire.c`](https://github.com/redis/redis/blob/7.2.4/src/expire.c) 的 `expireCommand` 走到 `expireGenericCommand`，给一个已存在的 Key 设置到期时刻。

**真实源码节选（Redis 7.2.4 `src/expire.c`）：**

```c
void expireCommand(client *c) {
    expireGenericCommand(c,commandTimeSnapshot(),UNIT_SECONDS);
}
```

`expireCommand` 的输入是客户端传来的 Key 和秒数；`expireGenericCommand` 验证参数，把相对时间换算成绝对到期时间，再关联到键。这里记录的是“逻辑上何时失效”，还不是把未来的删除任务逐条放进一个定时器。

下一次 `GET` 进入 `getGenericCommand`，它会通过数据库键查找函数检查 Key 是否仍有效。Redis 7.2.4 的 [`src/db.c`](https://github.com/redis/redis/blob/7.2.4/src/db.c) 在查找链路中处理 `expireIfNeeded`；已过期的键不会被当成正常命中。

但这引出一个有趣的疑问：如果从此再也没人访问 `post:42`，是不是过期 Key 就会永远留在内存中？

Redis 不只依赖请求时的检查，也会主动抽样清理过期键。在 [`src/expire.c`](https://github.com/redis/redis/blob/7.2.4/src/expire.c) 里，`activeExpireCycle(int type)` 分批进行过期检查，并受时间预算等条件控制。它的存在理由不是“马上清掉所有刚过期 Key”，而是**让没有后续读请求的过期键也有机会被回收，同时控制清理成本**。

## 逻辑过期不等于每个字节立即回收

这点容易理解错。一个 Key 到达过期时刻后，后续读取不再视其为有效数据；但不保证在那一瞬间所有内部内存都同步被清理。过期扫描与删除/释放可能分步进行。我们的最小实验刻意把“逻辑命中”与“物理条目数”分开：

```text
06: t=9 cache hit; t=10 cache miss; stored physical entries may remain until cleanup
```

执行：`python3 examples/cache_labs.py 06`。实验的简化缓存不实现物理回收，以便读者直接看到“过期却仍在字典里”的区别；真实 Redis 具有更复杂的回收机制。

## TTL 改善了什么，又引入了什么？

它让不用或久未更新的值不再永久保持有效，也有助于缓解失效通知遗漏留下的长期旧值。但 TTL 不等于“缓存永远与 DB 一致”：在 TTL 内，数据库可能已经更新，仍可能读到旧值。TTL 也不是 Redis 的容量淘汰算法。

而且新的读路径现在有一个细节：应用看到 Redis 缺 Key 会去 MySQL。假设 `GET /posts/999999` 查到的文章根本不存在，应用就无法填入一个正常的文章详情。下一次请求又要去 MySQL。下一篇专门研究这种看似不起眼的输入。

## 实验与验证边界

本章的最小实验可以在仓库根目录执行：

```bash
python3 examples/cache_labs.py 06
```

实验只使用 Python 标准库，已经在制作文档时实际执行。它验证当前章节描述的**教学模型**，而不是 Redis、MySQL、Go 或 Java 中间件的集成性能。真实系统的行为应以固定版本源码、配置和实际环境测试为准。

## 固定源码与参考资料

- [Redis 7.2.4 `src/expire.c` — `expireGenericCommand`、`activeExpireCycle`](https://github.com/redis/redis/blob/7.2.4/src/expire.c)
- [Redis 7.2.4 `src/t_string.c` — `setGenericCommand`](https://github.com/redis/redis/blob/7.2.4/src/t_string.c)
- [Redis 7.2.4 `src/db.c` — 过期键的读取检查](https://github.com/redis/redis/blob/7.2.4/src/db.c)
