---
title: "缓存体系 04：缓存删除了，为什么旧标题又被填回来了？"
sidebar_label: "四、删了缓存为什么旧值又回来了"
sidebar_position: 4
description: "利用读写交错复现旧值回填，并引入缓存版本门闩及其保证边界。"
slug: /cache-system/04-stale-backfill-guard
tags:
  - caching
  - redis
  - source-code
---

# 缓存删除了，为什么旧标题又被填回来了？

上一篇我们学会了数据库提交后删缓存。在没有并发时，它能让下一次请求读到新版本。现在只增加一个条件：读请求 A 恰好比更新请求 B 慢。

## 把“偶发脏读”变成可复现的时间线

```text
T0  Redis 没有 post:42，MySQL 为 v1
T1  读 A: Redis miss，读取 MySQL 得到 v1，暂停
T2  写 B: MySQL 提交 v2
T3  写 B: DEL post:42 成功
T4  读 A: 恢复执行，把之前的 v1 SET 回 Redis
T5  新读 C: Redis 命中 v1（错误）
```

这一反例很重要：**缓存删得没错，但未完成的旧读把旧数据填了回来。**再执行一次 `DEL` 当然可能缓解问题，可如果读 A 更慢、删除的重试也没有覆盖它的完成时间，仍然存在窗口。

我们真正缺少的是：填缓存之前，要知道“从开始读数据库到现在，是否发生过使这份结果失效的更新”。

## 最小新机制：为缓存回填设置一扇门

可以为每个 Key 建一个“失效代次”，这里称为 `epoch`：

- 读 A 准备回源时，读到 `epoch=0`，记下这个代次。
- 写 B 提交 v2 后，把 Key 删除并将 `epoch` 增加为 1。
- 读 A 想回填 v1 时发现自己的 `epoch=0` 已经过时，拒绝回填。

```python
# 概念伪代码：只表达核心顺序，不是真实 Redis 命令
observed_epoch = read_epoch("post:42")
row = db.read(42)  # 可能阻塞很久
conditional_fill_if_epoch_unchanged(
    "post:42", observed_epoch, row
)
```

这个方案不需要提前给每个请求做分布式锁，新增的机制只有**提交新值的资格校验**。但检查和填充必须原子化，否则“检查完代次”和“写入旧值”之间仍能穿插一次删除。

## Redis 怎样提供原子条件检查？

上面的 `conditional_fill_if_epoch_unchanged` 是**教学化简的应用函数**，Redis 并不存在同名内置 API。若选择通过 Redis Lua 脚本实现同槽原子操作，可以在 Redis 中读取代次、比较，再写缓存：

```lua
-- 教学化简：原子校验 Redis 内的代次后写入数据
local e = redis.call('GET', KEYS[2]) or '0'
if e == ARGV[1] then
  redis.call('SET', KEYS[1], ARGV[2])
  return 1
end
return 0
```

应用把缓存 Key 与代次 Key 传入脚本；在 Redis Cluster 中应设计相同 hash tag，使脚本涉及的 Key 位于同一个 Slot。这段脚本与 `GET`/`SET` 的行为，可以沿 Redis 7.2.4 [`src/t_string.c`](https://github.com/redis/redis/blob/7.2.4/src/t_string.c) 的 `getGenericCommand`、`setGenericCommand` 核查。**这段 Lua 是教学设计，不是 Redis 项目自带源码。** 对照真实 Redis 7.2.4 `src/t_string.c`，每次 `GET` 会通过 `getGenericCommand` 查出当前 Key，`SET` 再通过 `setKey` 修改相应对象：

**真实源码节选（Redis 7.2.4 `src/t_string.c`）：**

```c
setKey(c,c->db,key,val,setkey_flags);
server.dirty++;
```

这是一次 `SET` 命令的数据修改动作。对照我们的 Lua：`redis.call('GET', ...)` 获取门闩，判断相等后才有机会执行 `SET`。**原子性来自 Lua 脚本在 Redis 内的执行语义，而不是单独这两行 C 代码**；实际落地还应对 Cluster Key 路由、脚本运行时间与故障恢复做验证。

写入方应在 DB 提交成功后，原子化地提高同一代次并删掉旧缓存。请注意：**DB 事务提交与 Redis 代次更新不是同一个原子事务**；如果 DB 已提交、Redis 代次仍旧，或更新失败，旧数据仍可能被接受。因此版本门闩只能保证“已完成的失效事件之后，较早代次的回填不能复活”，**不能保证 DB/Redis 之间线性一致性**。

## 运行交错实验

执行 `python3 examples/cache_labs.py 04`：

```text
04: unprotected stale backfill v=1; guard accepted=False; final v=2
```

实验把“慢读暂停”变成显式的执行步骤；无保护回填确实写回 v1；启用代次门闩后拒绝这个旧结果，再从数据库重新读到 v2。它验证的是已完成失效后的条件回填机制，**没有运行真实 Lua 脚本，也未模拟 DB 与 Redis 双写原子提交**。

## 为什么不直接上复杂事务？

因为本章唯一需要解决的，是一次明确的读写交错。代次门闩、版本号和回填保护有很多实现，但它们有条件才成立。可用方案包括把变更序号写入 Redis、给记录加递增版本、周期性校验或从数据库变更日志重建；在一致性要求很高时，也应该考虑不走缓存读取或使用权威存储的读一致性策略。

现在我们至少避免了一种“旧读把删除后的缓存重新填旧”的窗口。不过在缓存 Key 真正被删掉的那一瞬间，如果**有 24 个请求同时看到 miss**，会发生什么？下一篇把时间线从两个并发请求扩大到几十个。

## 实验与验证边界

本章的最小实验可以在仓库根目录执行：

```bash
python3 examples/cache_labs.py 04
```

实验只使用 Python 标准库，已经在制作文档时实际执行。它验证当前章节描述的**教学模型**，而不是 Redis、MySQL、Go 或 Java 中间件的集成性能。真实系统的行为应以固定版本源码、配置和实际环境测试为准。

## 固定源码与参考资料

- [Redis 7.2.4 `src/t_string.c` — `GET`/`SET` 命令路径](https://github.com/redis/redis/blob/7.2.4/src/t_string.c)
- [Redis 7.2.4 `src/db.c` — 删除路径](https://github.com/redis/redis/blob/7.2.4/src/db.c)
- [Redis Lua 脚本文档](https://redis.io/docs/latest/develop/programmability/eval-intro/)
