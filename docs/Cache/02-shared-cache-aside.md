---
title: "缓存体系 02：两台应用机器，为什么还要各查一次数据库？"
sidebar_label: "二、两台机器为什么还要各查一次"
sidebar_position: 2
description: "从进程内缓存扩容问题，引入 Redis 共享缓存和 Cache-Aside。"
slug: /cache-system/02-shared-cache-aside
tags:
  - caching
  - redis
  - source-code
---

# 两台应用机器，为什么还要各查一次数据库？

上一章把 `GET /posts/42` 的 1000 次重复查询压缩成了 1 次。那时只有一个进程，字典缓存已经足够。现在访问量上涨，我们部署了应用实例 A 和 B，负载均衡把用户请求分别送到不同的实例。

## 原方案什么时候不再够用？

```text
Client ─→ Application A ─→ local dict A ─→ MySQL
       └→ Application B ─→ local dict B ─→ MySQL
```

A 第一次访问文章 42 时，要查一次 DB；B 也要查一次。即使 A 已经知道结果，B 仍然无法使用。这个问题并不意味着两个本地缓存“错了”，而是说明我们新增了一个需求：**多个应用实例需要共享命中结果**。

直觉做法是让 A 把它的字典同步给 B。但如果应用有 10 个实例，每个实例都要了解其他 9 个实例，加入、退出、重连会越来越复杂。我们能否只引入一个独立的键值服务，让所有实例按同一个 Key 读写？

## 于是出现共享缓存

```text
         Application A ─┐
                        ├─→ Redis ─(miss)─→ MySQL
         Application B ─┘
```

**这张图表示业务意图，不表示 Redis 会主动查询 MySQL**。真正的请求路径是：应用先 `GET post:42`；命中则返回；未命中由应用查数据库，再 `SET post:42`。这种把“何时回源”留在业务层的方式，叫做 **Cache-Aside（旁路缓存）**。

```python
# 概念伪代码：redis 是外部客户端对象

def get_post(post_id):
    key = f"post:{post_id}"
    raw = redis.get(key)
    if raw is not None:
        return json.loads(raw)
    row = db.read(post_id)
    redis.set(key, json.dumps(row))
    return row
```

这段代码只适用于目前的案例：文章 42 确实存在，而且尚未发生更新。暂时不要把 `None`（未命中）与业务上的 `NOT_FOUND` 混为一谈；这会在第 7 章成为一个独立问题。

## 对照 Redis 的真实写入路径

固定 Redis 7.2.4。通过 [`src/t_string.c`](https://github.com/redis/redis/blob/7.2.4/src/t_string.c) 的 `setCommand`，简单 `SET` 最终进入 `setGenericCommand`。在条件检查后，关键的数据修改发生在 `setKey`；它使用客户端指定的数据库、Key 和 Value。

**真实源码节选：**

```c
setKey(c,c->db,key,val,setkey_flags);
server.dirty++;
```

调用者是 `setCommand`，输入来自客户端发出的 `SET post:42 <JSON>`。`setKey` 修改当前 Redis 的键值，`server.dirty++` 标记数据修改；之后命令会准备正常回复。**这个函数既不知道请求来自 A 还是 B，也不知道 MySQL 里有“文章”这种表。**这恰恰让它能成为独立的共享缓存。

下一次 B 执行 `GET post:42` 时，它进入同一 Redis 实例的查找路径。A/B 共享的是一个网络服务的状态，而不是同步彼此的进程字典。

## 实际实验：A 和 B 首次读取

运行 `python3 examples/cache_labs.py 02`。实验先让两份独立本地字典各自访问一次，再换为一个共享 `KV` 对象，让两次请求使用同一个缓存键。

```text
02: two local caches DB=2; one shared cache DB=1
```

输出验证的是“共同访问同一缓存键”可以省掉第二次 DB 查询。它**没有运行真实 Redis**，也没有覆盖多个应用同时发现空缓存时的竞争。并发未命中是以后才会遇到的压力。

## Redis 不是免费的

与进程内字典相比，Redis 需要一次网络交互，还增加了容量、连接、可用性和运维问题。因此“共享缓存”是为了避免多实例重复回源，而不是因为 Redis 总是比本地缓存更快。

到目前为止，文章在 MySQL 中保持不变，所以这套方案能正常工作。下一篇只改变一个条件：管理员将文章 42 从版本 1 更新为版本 2。Redis 里还存着什么？

## 实验与验证边界

本章的最小实验可以在仓库根目录执行：

```bash
python3 examples/cache_labs.py 02
```

实验只使用 Python 标准库，已经在制作文档时实际执行。它验证当前章节描述的**教学模型**，而不是 Redis、MySQL、Go 或 Java 中间件的集成性能。真实系统的行为应以固定版本源码、配置和实际环境测试为准。

## 固定源码与参考资料

- [Redis 7.2.4 `src/t_string.c` — `setCommand`、`setGenericCommand`、`getCommand`](https://github.com/redis/redis/blob/7.2.4/src/t_string.c)
- [Redis 7.2.4 `src/db.c` — 键查找与修改](https://github.com/redis/redis/blob/7.2.4/src/db.c)
