---
title: "缓存体系 05：热门文章刚失效，为什么 24 个请求同时打到数据库？"
sidebar_label: "五、热点失效后请求同时打到库"
sidebar_position: 5
description: "通过并发回源引出单 Key 请求合并，并读 Go singleflight 的真实源码。"
slug: /cache-system/05-singleflight-breakdown
tags:
  - caching
  - redis
  - source-code
---

# 热门文章刚失效，为什么 24 个请求同时打到数据库？

第四章解决了某些并发交错导致的旧数据回填。我们现在假定旧值失效已经正确执行，数据库仍然是权威来源。文章 42 登上热榜，在缓存刚被 `DEL` 后，24 个请求几乎同时进入 `GET /posts/42`。

## 单个请求的正确流程，在并发下成了问题

```text
Request 1 → Redis miss ─→ DB read(42)
Request 2 → Redis miss ─→ DB read(42)
...
Request 24→ Redis miss ─→ DB read(42)
```

每一个请求单独看都没有错：未命中就查数据库。但 24 条等价请求一起回源，造成 24 次相同查询。如果单个热点 Key 的未命中发生在高峰流量中，可能瞬间放大到成百上千个回源请求。这类**单个热点 Key 失效后形成集中回源**的情形，常称缓存击穿。不要和之后的“许多不同 Key 同时过期”混为一谈。

我们现在需要的不是更大的 Redis，也不是延长所有 Key 的 TTL，而是：**同一应用进程里，对于同一个未命中 Key，同时只做一次 DB 加载，其余请求等待并共享结果。**

## 先让第一个请求当加载者

概念上可以使用一个记录当前“在途加载”的表：

```python
# 概念伪代码，inflight 按 key 存放正在进行的调用
if key in inflight:
    return wait(inflight[key])
else:
    future = start_db_load(key)
    inflight[key] = future
    try:
        return wait(future)
    finally:
        del inflight[key]
```

这里引入的机制叫 Singleflight（同 Key 重复调用抑制）。它只合并**同时进行**的相同加载。如果第一次加载已经完成并清理在途记录，稍后又发生新的 miss，仍会有新加载；它不是缓存替代品，也不替你管理 Redis 的有效期。

## 看真实 Go singleflight 源码

我们固定仓库 `golang.org/x/sync`，版本 **v0.13.0**：[`singleflight/singleflight.go`](https://github.com/golang/sync/blob/v0.13.0/singleflight/singleflight.go)。查找 `Group.Do(key, fn)`。

它的参数正好映射到我们的业务：`key="post:42"`，`fn` 是“查询 MySQL 并返回文章详情”的函数。`Group` 的内部维护一个 `map[string]*call`；每个 Key 只有一个当前进行的调用。

**真实源码节选（原文件抽取的连续关键语句）：**

```go
if c, ok := g.m[key]; ok {
    c.dups++
    g.mu.Unlock()
    c.wg.Wait()
```

调用者是应用中的 `group.Do("post:42", loadPost)`。这几行说明：发现已有相同 Key 的调用，后来的请求不再执行 `fn`，而是通过 `WaitGroup` 等已有调用结束。对应的初次请求建立 `call`、执行 `doCall`，完成后唤醒等待者并从表中移除它；等待者拿到与第一个请求相同的返回值或错误。

这里的设计理由非常明确：**让同一时刻、同一 Key 的重复回源共享一次结果**，而不是锁住整个数据库，也不是串行化所有不同文章的请求。

## 验证：24 次变成 1 次

运行：

```bash
python3 examples/cache_labs.py 05
```

实际输出：

```text
05: parallel misses without merging DB=24; with same-process singleflight DB=1
```

实验使用 Python 线程模拟 24 个并发请求，通过 `Barrier` 同时触发请求；模拟慢查询以确保请求在同一加载窗口重叠。`Flight` 用 `Event` 让同 Key 的等待者共享一次返回。**它是对机制的验证，不是对 Go 库或真实 MySQL/Redis 的压测。**

## 为什么还有“击穿”？

假设我们把应用实例从 A 扩容到 A、B、C。每个进程各自有一个 `Group`：

```text
A 上 100 个同 Key miss → A 合并为 1 次 DB
B 上 100 个同 Key miss → B 合并为 1 次 DB
C 上 100 个同 Key miss → C 合并为 1 次 DB

总共仍可能有 3 次回源，而不是全局 1 次。
```

跨实例可以进一步研究分布式锁、缓存续期、后台刷新或受控回源，但它们各有超时、持锁者故障、写入先后与等待性能问题。没有真实失败场景之前，本章不把它们一次性塞进架构。

还有一种压力与并发无关：文章 42 半年都没有人读过，却一直占着 Redis 内存。接下来需要让一些缓存自己失效。下一篇才引入 TTL。

## 实验与验证边界

本章的最小实验可以在仓库根目录执行：

```bash
python3 examples/cache_labs.py 05
```

实验只使用 Python 标准库，已经在制作文档时实际执行。它验证当前章节描述的**教学模型**，而不是 Redis、MySQL、Go 或 Java 中间件的集成性能。真实系统的行为应以固定版本源码、配置和实际环境测试为准。

## 固定源码与参考资料

- [Go `x/sync` v0.13.0：`Group.Do`、`doCall`](https://github.com/golang/sync/blob/v0.13.0/singleflight/singleflight.go)
- [Redis 7.2.4 `GET` 未命中行为](https://github.com/redis/redis/blob/7.2.4/src/t_string.c)
