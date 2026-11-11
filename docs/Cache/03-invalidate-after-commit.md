---
title: "缓存体系 03：管理员修改标题，为什么缓存依旧返回旧值？"
sidebar_label: "三、改了标题却还返回旧值"
sidebar_position: 3
description: "由旧数据复现，推导数据库提交后删除缓存的策略与时序。"
slug: /cache-system/03-invalidate-after-commit
tags:
  - caching
  - redis
  - source-code
---

# 管理员修改标题，为什么缓存依旧返回旧值？

前两章的前提是文章内容不变。现在管理员把文章 42 的标题改为 `New Title`，数据库的 `version` 从 1 增长到 2。我们不改变读路径，仍然使用 `GET Redis → miss → MySQL → SET Redis`。

## 一条成功的更新，产生了两份相反的事实

```text
T0  MySQL: v1     Redis: v1
T1  管理员提交更新
T2  MySQL: v2     Redis: v1
T3  用户 GET /posts/42 → Redis 命中 v1
```

旧程序完全按设计执行，没有报错，却向用户返回了过期数据。这时我们才需要讨论“缓存一致性”：**当权威数据改变时，派生的缓存值什么时候失效？**

最直觉的做法是更新数据库后，再更新 Redis：

```text
UPDATE mysql v2 → SET Redis v2
```

这种双写在低并发时可用，但要承担两份数据更新的顺序、重试、部分失败；尤其是两个管理员几乎同时更新时，如果 Redis 的写入顺序与 DB 的事务提交顺序不同，就可能让 Redis 倒退到旧值。

我们可以先减少一件必须做对的事情：**不写新缓存，只让旧缓存失效**。下一次读取自然会从数据库加载当前值。

```python
# 教学化简：提交成功后再删缓存

def update_post(post_id, new_title):
    db.update_and_commit(post_id, new_title)
    redis.delete(f"post:{post_id}")
```

这里的 `commit` 不能省略。还没有提交就删除缓存，另一位读者可能在读库时重新加载旧版本。把删除安排在数据库提交之后，是一种清楚的正常路径，但并不代表任何并发下都绝对一致——下一章专门看残余竞态。

## 对照 Redis 的 DEL 代码

这次我们只需要知道 `DEL` 对缓存键做了什么。在 Redis 7.2.4 [`src/db.c`](https://github.com/redis/redis/blob/7.2.4/src/db.c) 中，`delCommand` 将客户端请求交给 `delGenericCommand`。后者遍历要删除的 Key，根据配置使用同步或异步释放方式，并对成功删除的键记录修改事件。

**真实源码节选（Redis 7.2.4）：**

```c
void delCommand(client *c) {
    delGenericCommand(c,server.lazyfree_lazy_user_del);
}
```

`delGenericCommand` 在普通删除路径里调用 `dbSyncDelete`，启用延迟释放配置时可调用 `dbAsyncDelete`。需要注意：`UNLINK` 的内存释放行为与 `DEL` 配置相关，但从读取语义上看，成功移除键后下一次正常 `GET` 不会继续返回那个键的旧值。这里的关键动作是**删掉键关联**，不必深入对象内存释放细节。

下一次 `getGenericCommand` 调用 `lookupKeyReadOrReply` 发现 Key 不存在，应用就重新查询 MySQL。这体现了命令分工：**DEL 只删除缓存，重新填充由业务读路径完成。**

## 实验：一次更新，三次读取

运行 `python3 examples/cache_labs.py 03`：

```text
03: cached=1; after DB update without invalidation=1; after DEL=2
```

第一次查询建立 v1 缓存；随后 DB 变为 v2，但 Redis 不删则仍读 v1；删除以后再读，DB 的 v2 被重新填入缓存。实验是单线程确定性运行，正好验证这一章所需的最小机制，没有隐含地解决并发。

## 一个看似已经完成的方案，还有细小窗口

如果更新 DB 已提交，但删除 Redis 命令失败，会怎样？旧缓存可能继续存在。可以通过重试、事务外盒、变更日志等措施改善可靠性，但这些都需要引入新的系统组件；我们目前先记录这个边界，不提前扩展。

更微妙的是：即便 `DEL` 确实成功，另一个读请求可能在删除之前就从数据库读出了 v1，然后在删除之后才执行 `SET`。这会让旧值**复活**。这是下一篇的唯一主问题。

## 实验与验证边界

本章的最小实验可以在仓库根目录执行：

```bash
python3 examples/cache_labs.py 03
```

实验只使用 Python 标准库，已经在制作文档时实际执行。它验证当前章节描述的**教学模型**，而不是 Redis、MySQL、Go 或 Java 中间件的集成性能。真实系统的行为应以固定版本源码、配置和实际环境测试为准。

## 固定源码与参考资料

- [Redis 7.2.4 `src/db.c` — `delCommand`、`delGenericCommand`](https://github.com/redis/redis/blob/7.2.4/src/db.c)
- [Redis 7.2.4 `src/t_string.c` — `getGenericCommand`、`setGenericCommand`](https://github.com/redis/redis/blob/7.2.4/src/t_string.c)
