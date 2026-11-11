---
title: "缓存体系 07：文章根本不存在，为什么每次请求都查询数据库？"
sidebar_label: "七、文章不存在为什么还查库"
sidebar_position: 7
description: "复现缓存穿透，加入短期空结果缓存并指出新建数据的可见性风险。"
slug: /cache-system/07-negative-cache-penetration
tags:
  - caching
  - redis
  - source-code
---

# 文章根本不存在，为什么每次请求都查询数据库？

上一章让缓存具有 TTL。今天同一个文章详情接口出现一种特殊输入：`GET /posts/999999`。数据库返回 `NOT_FOUND`，因为根本没有这篇文章。

## “没有数据”为什么也会形成负载？

我们的 Cache-Aside 实现是“Redis 没命中 → MySQL 查询 → 有结果就填缓存”。对真实存在的文章，这个路径能被第二次请求命中；对于不存在的文章，它每次都返回空，导致每一次相同的错误 ID 请求都去数据库：

```text
GET /posts/999999 → Redis miss → MySQL: NOT_FOUND
GET /posts/999999 → Redis miss → MySQL: NOT_FOUND
GET /posts/999999 → Redis miss → MySQL: NOT_FOUND
```

这里的问题不是热点真数据失效，而是**不存在的数据根本没有形成可复用的缓存命中**。实践中通常称缓存穿透。

我们需要的是：让“查过，确认不存在”也可以在短时间里复用。

## 只添加一个结果类型：NOT_FOUND

关键是区分两层 `None`：一是 Redis 里**不存在缓存条目**；二是 MySQL 里**不存在文章记录**。如果直接用 Redis `nil` 表示两者，读取侧永远无法区分。

于是我们引入一个明确的负缓存哨兵值：

```python
# 概念伪代码：redis.get 返回 None 表示没有缓存条目
raw = redis.get("post:999999")
if raw == "__NOT_FOUND__":
    return None
if raw is not None:
    return json.loads(raw)

row = db.read(999999)
if row is None:
    redis.set("post:999999", "__NOT_FOUND__", ex=2)
    return None
```

我们只对“不存在”缓存 2 秒。为什么不是永不过期？因为管理员可能随后创建 ID=999999 的文章。刚写好的数据库记录，可能被旧的“NOT_FOUND”遮挡。短 TTL 并不解决这个一致性窗口，但给它一个明确上界（前提是故障时 TTL 机制正常工作）。

## Redis 源码提供的是存值能力，不是“负缓存算法”

这是源码阅读非常需要守住的边界。Redis 7.2.4 [`src/t_string.c`](https://github.com/redis/redis/blob/7.2.4/src/t_string.c) 的 `setGenericCommand` 接受我们提交的字符串值和过期参数，内部通过 `setKey` 保存并通过 `setExpire` 处理有效期。

**真实源码节选：**

```c
setKey(c,c->db,key,val,setkey_flags);
server.dirty++;
```

这两行不会“识别不存在的文章”。`"__NOT_FOUND__"` 的语义是**应用协议自定义的**；Redis 只是原样保存一段字符串。当请求再次到来，`getCommand` 找到这段字符串，应用把它解释为“不需要查 DB，直接返回 404”。所以如果有人告诉你“Redis 内部的穿透防护是 `setGenericCommand`”，那是把存储能力误当成业务策略。

需要生产化时，最好使用有明确结构的 JSON 或二进制标记，避免普通业务字符串碰巧等于哨兵。业务层还应区分“数据库故障/查询超时”与“记录确实不存在”：**不能把错误缓存成不存在**。

## 可运行实验：100 次请求与一次创建

运行 `python3 examples/cache_labs.py 07`：

```text
07: 100 repeated nonexistent-ID requests DB=1; newly created row hidden until negative TTL expires
```

实验把逻辑时间设为 0 和 1、2 秒：前 100 次同 ID 查询只查 DB 1 次；在第 1 秒数据库创建文章后，短负缓存仍返回不存在；第 2 秒过期，重新查 DB 后才读到新文章。这不是吞吐性能测量，而是可见性语义的复现。

## 为什么这还不是“穿透的完整防御”？

如果请求者每次产生一个从未出现过的新 ID：

```text
GET /posts/900001
GET /posts/900002
GET /posts/900003
...
```

负缓存对**单一 ID 重复查询**很有效，但对不断变换的 ID 无法在第一次命中之前减轻 DB 查询，还可能增加 Redis 的 Key 数量。这时要研究参数校验、鉴权、API 限流，或在适当场景引入布隆过滤器；但本章没有对应的输入规模与误判试验，所以不提前把这些变成必备组件。

下一章换一种输入：这些文章都真实存在、以前也缓存过，只是它们的 TTL 被设置成了**同一个到期时刻**。这会产生另一种完全不同的回源冲击。

## 实验与验证边界

本章的最小实验可以在仓库根目录执行：

```bash
python3 examples/cache_labs.py 07
```

实验只使用 Python 标准库，已经在制作文档时实际执行。它验证当前章节描述的**教学模型**，而不是 Redis、MySQL、Go 或 Java 中间件的集成性能。真实系统的行为应以固定版本源码、配置和实际环境测试为准。

## 固定源码与参考资料

- [Redis 7.2.4 `src/t_string.c` — SET/GET 的字符串与过期参数路径](https://github.com/redis/redis/blob/7.2.4/src/t_string.c)
- [Redis 7.2.4 `src/expire.c` — TTL/EXPIRE](https://github.com/redis/redis/blob/7.2.4/src/expire.c)
