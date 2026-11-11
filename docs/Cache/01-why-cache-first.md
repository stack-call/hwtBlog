---
title: "缓存体系 01：同一篇文章读取 1000 次，为什么数据库被查了 1000 次？"
sidebar_label: "一、同一篇文章被读了 1000 次"
sidebar_position: 1
description: "从重复查询出发，写出最小本地缓存，再对照 Redis GET 的真实执行路径。"
slug: /cache-system/01-why-cache-first
tags:
  - caching
  - redis
  - source-code
---

# 同一篇文章读取 1000 次，为什么数据库被查了 1000 次？

这是这个系列的第一篇。我们还没有 Redis、集群和复杂架构，只有一个文章详情接口：`GET /posts/42`。数据库存放 `{id:42,title:"Old Title",version:1}`。如果读者觉得缓存是一个人人知道的组件，不妨先暂时忘掉它：真正重要的是，**什么观测结果逼我们必须多存一份数据？**

## 只有一次请求时，最简单的方案是对的

应用收到请求，通过 `db.read(42)` 查数据库，然后序列化结果。对于只有一个请求的服务，这种实现直接、可调试，不会遇到缓存与数据库不一致。

```python
# 概念伪代码：db.read 代表一次数据库查询

def get_post(post_id):
    return db.read(post_id)
```

现在把输入从一条请求改成连续 1000 条同样的请求。数据库里的文章没有变化，但每个请求都再次执行查询。这里我们的可观察指标不是“感觉很慢”，而是 `db_queries = 1000`（注意这里的计数是教学模型的计数，不是数据库的实际 QPS）。重复计算的来源已经很清楚：**相同的输入、相同的稳定结果，重复穿过昂贵的读路径。**

最小的改动是在当前应用进程中放一个字典，第一次查询后记住结果：

```python
# 教学化简：只有一个进程，数据暂时不变
cache = {}

def get_post(post_id):
    if post_id in cache:
        return cache[post_id]
    row = db.read(post_id)
    cache[post_id] = row
    return row
```

这个方案没有任何网络协议，甚至没有所谓的“分布式缓存”。只要进程还活着，1000 次请求只有第一次会查数据库。这是一个合理的最小设计；现阶段没有理由把它改造成集群。

## 真实代码在哪里体现“读缓存”这件事？

我们没有理由声称上面的 Python 字典就是 Redis 源码。但可以先到一个真实缓存引擎中观察：一次键值读取到底如何执行。

固定源码版本为 **Redis 7.2.4**。打开 [`src/t_string.c`](https://github.com/redis/redis/blob/7.2.4/src/t_string.c)，找到 `getCommand(client *c)`。它是 Redis 字符串 `GET` 命令的处理函数，内部调用 `getGenericCommand(c)`。后者再调用 `lookupKeyReadOrReply(...)`。简化后的调用链是：

```text
Redis 命令分发 → getCommand(c)
                 → getGenericCommand(c)
                 → lookupKeyReadOrReply(c, key, null_reply)
                 → 有值则 addReplyBulk(c, value)
```

**真实源码节选（Redis 7.2.4，`src/t_string.c`）：**

```c
void getCommand(client *c) {
    getGenericCommand(c);
}
```

为什么 `GET` 不直接查询 MySQL？因为 Redis 本身就是一个键值数据系统，它只知道自己的内存字典等内部结构。业务层的“Redis 未命中以后查 MySQL”**不是这个函数的职责**。这个边界在下一章非常重要：缓存服务负责保存、读取和删除键；**何时回源**由应用决定。

`lookupKeyReadOrReply` 位于 [`src/db.c`](https://github.com/redis/redis/blob/7.2.4/src/db.c)，其入口接收当前 DB、Key 与未命中时的回复；下游读取字符串对象后把结果写入客户端回复缓冲。它不会代替业务访问关系数据库。把真实代码和我们的 Python 示例对照，可以发现二者共享的是“先查现有值”，并不是同一个函数调用链。

## 运行一次，看看行为真的改变了吗？

执行 `python3 examples/cache_labs.py 01`。这段实验构造连续 1000 次请求，分别对比有无进程内字典缓存的数据库读次数。

实际执行输出：

```text
01: without cache DB=1000; with local cache DB=1
```

现在我们可以精确说出收益：不是“缓存一定快十倍”，而是**在数据稳定且命中率高的这段工作负载中，应用减少了重复查询**。真实延迟还会受到网络、序列化、数据大小和命中率影响。

## 我们还没有解决什么？

这份缓存只在一个进程里。如果应用进程重启，字典消失；如果部署两个进程，它们各自保存一份字典。我们不能为了“完整”现在就引入 TTL、淘汰策略和一致性算法——当前反例只支持一个问题：**新增应用实例后，第一次相同文章的请求是否还要再查数据库？**

下一篇就让 A/B 两个应用进程同时处理 `GET /posts/42`。

## 实验与验证边界

本章的最小实验可以在仓库根目录执行：

```bash
python3 examples/cache_labs.py 01
```

实验只使用 Python 标准库，已经在制作文档时实际执行。它验证当前章节描述的**教学模型**，而不是 Redis、MySQL、Go 或 Java 中间件的集成性能。真实系统的行为应以固定版本源码、配置和实际环境测试为准。

## 固定源码与参考资料

- [Redis 7.2.4 `src/t_string.c` — `getCommand`、`getGenericCommand`](https://github.com/redis/redis/blob/7.2.4/src/t_string.c)
- [Redis 7.2.4 `src/db.c` — `lookupKeyReadOrReply`](https://github.com/redis/redis/blob/7.2.4/src/db.c)
