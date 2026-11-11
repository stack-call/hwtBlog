---
title: "缓存体系 02：两台应用机器，为什么还要各查一次数据库？"
sidebar_label: "二、两台机器为什么还要各查一次"
sidebar_position: 2
description: "从两个进程各自保存文章详情的重复回源开始，逐步推导共享缓存和 Cache-Aside，并沿 Redis 7.2.4 的 GET、SET 真实源码验证每一层职责。"
slug: /cache-system/02-shared-cache-aside
tags:
  - caching
  - redis
  - source-code
---

# 两台应用机器，为什么还要各查一次数据库？

> 《缓存体系：从一条查询开始》第 2 章。承接 [第 1 章：同一篇文章读了 1000 次，为什么数据库也被查了 1000 次？](./01-why-cache-first.md)。本章只增加一个新机制：**可以被多个应用实例共同访问的缓存**。源码固定为 **Redis 7.2.4**。我们暂时不研究 TTL、淘汰、缓存一致性或 Redis Cluster，因为它们还没有成为当前问题的必要答案。

上一篇我们写了一个最简单的文章详情接口：

```http
GET /posts/42
```

数据库中的文章是：

```json
{"id":42,"title":"Old Title","version":1}
```

开始时每个请求都查数据库。后来，我们发现重复请求会重复执行同样的查询，于是用一张按 `post_id` 索引的 Python 字典保存结果：

```python
# 上一章已经实现的核心逻辑（教学化简）
def get_post(post_id):
    if post_id in cache:
        return cache[post_id]

    row = db.read(post_id)
    if row is not None:
        cache[post_id] = row
    return row
```

单进程、同一个文章被顺序读取 1000 次时，这个方案可以把数据库读取次数从 1000 次降为 1 次。这不是一个口头上的“性能提升”，我们已经用计数器验证过：有 999 次请求根本没有进入 `db.read()`。

**因此，眼下不需要重新设计缓存。** 这份字典简单、便宜，而且不需要网络连接。只要应用保持单实例运行，数据在演示期间不变化，它就足以解决重复查询。

真正的变化来自一次普通的扩容。

## 一、只增加一台应用机器，为什么数据库读取又多了一次？

网站访问量上升，我们不希望所有请求只落到一个 Python 进程，于是部署两个应用实例：A 和 B。它们使用同一份应用代码、访问同一个数据库，但分别运行在两个进程中。

外部看起来还是一个接口：

```text
             GET /posts/42
                   |
               负载均衡
                /    \
               v      v
          应用 A      应用 B
```

不必先讨论负载均衡器怎么选节点。它在这里的作用只有一个：同一个接口的不同请求，可能会交给不同的应用实例处理。

现在顺序发生四个请求：

```text
请求 1 → A：GET /posts/42
请求 2 → B：GET /posts/42
请求 3 → A：GET /posts/42
请求 4 → B：GET /posts/42
```

我们先不改代码，只追踪两个独立的字典：

| 请求 | 命中的实例 | 该实例收到请求前的缓存 | 是否访问数据库 | 返回 |
| --- | --- | --- | --- | --- |
| 1 | A | `{}` | 是 | `Old Title` |
| 2 | B | `{}` | 是 | `Old Title` |
| 3 | A | `{42: ...}` | 否 | `Old Title` |
| 4 | B | `{42: ...}` | 否 | `Old Title` |

四次请求中，数据库被查询了 **两次**。

如果只看每个进程自身的命中率，似乎没有错误：A 第一次未命中，第二次命中；B 也一样。

但把它们当作整个网站来观察，就会发现一件不合理的事：**A 已经查到了文章 42，B 却仍然需要再查一次同样的数据库。**

这是本章的第一个失败场景。注意，它不是“Redis 比字典快”，也不是“单机缓存性能不好”；恰恰相反，本地字典仍然很快。它失败的原因是：**多个进程各自保存了一份互不相通的状态。**

### 为什么 B 不能直接读取 A 的 `cache[42]`？

因为 Python 字典只是 A 进程地址空间中的对象。B 无法用自己的变量名读取另一个进程的内存，更不能因为两个进程运行相同的 Python 代码，就把它们误认为共享了同一个字典。

想象以下状态：

```text
应用 A 内存：                  应用 B 内存：
cache = {42: Old Title}       cache = {}

          |                        |
          +----------+-------------+
                     |
                  数据库
              post 42: Old Title
```

我们只是多部署了一份应用，数据库就又收到一次完全相同的查询。应用实例越多、它们首次访问的热门文章越相似，这种重复工作就越明显。

但请注意推论的边界：如果每个实例都长期命中自己的本地缓存，这种重复回源未必在性能上不可接受。**只有当我们确实希望不同实例复用彼此已经加载的数据时，共享缓存才成为值得引入的设计。**

## 二、最直觉的修复：让 A 把字典复制给 B

发现两个进程的状态不互通后，我们可能会想到一个很直接的方法：A 查完数据库时，顺手把结果发送给 B。

先假设只有两个进程，写成伪代码非常简单：

```python
# 概念伪代码：假设存在发送给另一个进程的网络接口

def get_post_on_a(post_id):
    if post_id in local_cache:
        return local_cache[post_id]

    row = db.read(post_id)
    local_cache[post_id] = row
    send_to_process_b(post_id, row)
    return row
```

在**只有 A 和 B、网络正常、B 一直在线**的情况下，这个想法甚至可能有效。B 已经收到文章 42，就不再需要为它回源。

这一步不应该轻易被否定；它是从现有实现出发的合理尝试。

现在只改变一个条件：我们又增加了第三个应用实例 C。

A 查到一篇文章后，应该发送给谁？B 和 C。以后增加到 10 个实例呢？是不是每次都要给另外 9 个实例发送？

更麻烦的是，一个实例可能在发送过程中重启：

```text
T0  A 查到文章 42，缓存成功
T1  A 把结果发送给 B，B 收到
T2  C 此时离线，没有收到
T3  C 重启，收到第一个 /posts/42 请求
T4  C 的字典仍为空，再次访问数据库
```

我们原本只是想省掉一次重复查询，却逐渐不得不回答：“实例成员列表从哪里来？消息丢了怎么办？新实例怎样补齐旧缓存？发送时某个实例挂了怎么办？”

**这不是在说进程间同步永远不可行。** 对某些系统而言，它确实是一种设计。但对于当前这个只想共享文章详情读取结果的需求，它引入了太多额外协调。

我们现在真正缺少的，并不是让 A 的缓存与 B 的缓存互相复制，而是更简单的一件事：

> **能否只保存一份供所有应用实例访问的缓存，让 A 和 B 使用同一个 Key 查询？**

如果做到这一点，A 就不用知道 B 存在，B 也不用知道 C 存在。应用只需要知道共享缓存的地址。

于是，新的结构才自然出现：

```text
            GET /posts/42
                  |
               负载均衡
               /      \
              v        v
           应用 A     应用 B
              \        /
               v      v
               共享 KV
                  |
              （可能未命中）
                  |
            应用回源数据库
```

这里的“共享 KV”暂时只是一个独立的键值存储。**先理解它解决什么问题，再决定选用哪种产品。**

## 三、先别用 Redis：只改一个变量，把两份字典变成一份

为了检验刚才的推论，我们先使用 Python 编写一个最小的共享缓存模型。它不需要 Docker、网络或 Redis。

```python
class SharedKV:
    def __init__(self):
        self.data = {}

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value):
        self.data[key] = value
```

现在 A 和 B 不再各自持有一份 `cache`，而是把同一个 `shared_kv` 交给它们：

```python
shared_kv = SharedKV()
app_a = PostService(db, shared_kv)
app_b = PostService(db, shared_kv)
```

这里的 `PostService` 还需要稍后补全，但你已经能看到设计变化：原来每个实例维护自己的缓存对象，现在所有实例访问相同的键值存储接口。

请特别注意，**两个 Python 对象共用同一个 `SharedKV` 对象不等于真正的跨进程共享**。它只是帮助我们证明：“如果访问的是同一份状态，B 就能复用 A 的写入结果。”真实 Redis 会把这份状态放在独立服务进程中，通过网络提供读写。

这时，服务端到底保存了多少篇文章、是否知道客户端叫 A 或 B，都不重要。它只需要提供一种非常小的接口：

```text
GET key         → 返回 value，或返回不存在
SET key value   → 保存 value
```

这与我们在第一章用字典实现的“按 Key 找结果”非常相似。区别是：它不再藏在某个应用进程的地址空间里。

### 第一次完整演算

假设缓存刚启动，里面没有任何 Key。A 收到文章 42 的请求：

```text
A 先问共享缓存：GET post:42:detail
             → 未命中
A 再查数据库：  read(42)
             → {id:42,title:Old Title,version:1}
A 写共享缓存：  SET post:42:detail <JSON>
             → 保存成功
A 返回客户端： Old Title
```

紧接着，B 收到同一个请求：

```text
B 问共享缓存：GET post:42:detail
             → 命中
B 返回客户端： Old Title
B 不需要访问数据库
```

在**这两个顺序请求**的条件下，数据库从两次读取减少到一次。

请留意我们没有声称 Redis 把查询变成了零成本：第一次读取还是要查数据库；每个应用请求也增加了一次到共享缓存的访问。我们的收益是**减少重复数据库读取**，不是消除所有网络交互。

## 四、谁在缓存未命中时查数据库？为什么不是 Redis？

上面的演算里有一个容易不自觉跳过的问题：

A 请求 `GET post:42:detail`，如果缓存没有命中，谁负责执行 `db.read(42)`？

直觉上可能会把整个过程画成：

```text
应用 → Redis → MySQL
```

但这张图容易误导读者，以为 Redis 知道 `post:42:detail` 对应 MySQL 的 `posts` 表，也知道怎样把查询结果转换成文章 JSON。

事实上，**普通 Redis 不认识你的文章表，不会因为一次 `GET` 未命中就自动查询 MySQL。** 这些业务规则都必须在应用中实现。

因此，应用需要先尝试缓存，未命中才由自己访问数据库，再把结果回填给 Redis：

```python
# 教学化简：cache 具有 get/set，db 具有 read

def get_post(post_id):
    key = f"post:{post_id}:detail"
    raw = cache.get(key)

    if raw is not None:
        return json.loads(raw)

    row = db.read(post_id)
    if row is not None:
        cache.set(key, json.dumps(row))
    return row
```

这段代码与第一章的实现相比，有三点很具体的变化。

**第一，缓存查询由 Python 字典查找变成了调用 `cache.get(key)`。** 在真实 Redis 中，它对应一条网络命令 `GET key`。

**第二，回源仍然写在应用代码里。** 没有新的 Redis 组件来替你构造 SQL、检查文章权限或查找 MySQL。

**第三，缓存中的值变成了序列化后的内容。** 因为我们不再能假定其他进程直接共享同一个 Python 字典对象。对于文章详情，先用 JSON 表示对象是合理的最小选择。

这种“读缓存，未命中由应用回源，再由应用回填”的使用方式，通常称为 **Cache-Aside（旁路缓存）**。

注意我们没有先背一个模式定义，再找项目来套。我们先遇到了**跨实例共享和未命中后谁负责加载**两个问题；解决它们的代码长成上面的样子，然后才给这种模式起名字。

### 为什么 `raw is not None` 不能随手写成 `if raw`？

对于普通文章 JSON，Redis 返回的字符串非空，两个判断碰巧效果相同。但 `GET` 的“Key 不存在”是一种专门的结果，不等于所有可解析的值都必须为真。

例如，在其他业务场景里，空字符串、数字 `0` 或 JSON `false` 都可能是合法值。把值本身的真假和“Key 是否存在”混在一起，会产生错误的命中判断。

因此，这里把 `None` 当作缓存不存在的标记；真正的 Redis 客户端返回值还可能是 `bytes`，需要根据客户端配置解码。

### 为什么暂时不缓存数据库返回的 `None`？

因为我们正在研究的是**存在的文章详情如何被多个实例共同复用**。对于不存在的文章 ID，如果直接把 `None` 当作“未命中”放进去，下一次读取仍然会回源。怎样区分“Key 没有保存”与“确实查询过，但文章不存在”，是第 7 章缓存穿透要解决的独立问题。

当前直接跳过空结果，既保持最小实现，也为后面留下真实可观察的问题。

## 五、共享存储之前，还有一个不能省略的设计：Key 必须表示输入

现在 A、B 已经能够访问同一份缓存。我们可能以为只要换成 Redis，所有问题都解决了。

但请把请求从文章 42 改为文章 43：

```text
GET /posts/42
GET /posts/43
```

如果我们偷懒，让两个请求都使用：

```python
key = "post:detail"
```

会发生什么？

第一次请求 42，数据库返回 `{id:42,...}`，然后把它存到 `post:detail`。

第二次请求 43，缓存发现 `post:detail` 已经存在，于是直接把文章 **42** 返回给请求 **43** 的用户。

这一次甚至不是“多查了一次数据库”，而是**返回错误数据**。

在第一章，我们已经知道应当按文章 ID 缓存。这一章换成共享服务后，不能丢掉这个前提。

正确的 Key 应当包含足以区分缓存结果的输入：

```python
key = f"post:{post_id}:detail"
```

现在两篇文章对应：

```text
post:42:detail → {id:42,title:Old Title,version:1}
post:43:detail → {id:43,title:Another Post,version:1}
```

**Key 的设计不是附属的命名美学，而是缓存正确性的一部分。**

对于当前公开文章详情接口，`post_id` 足以标识结果。如果未来响应还依赖登录用户、语言、发布状态或权限，那么仅用 `post_id` 可能不足以区分缓存结果。我们暂时不引入这些维度，因为当前贯穿案例没有这个需求。

### 为什么还要把对象变成 JSON？

原来的 `cache[42] = row`，保存的是 Python 进程中的对象。把对象交给独立的网络服务时，客户端和服务端不共享内存地址，所以需要约定一个可传输的表示形式。

JSON 让缓存中保存的是文章的**数据内容**，而不是某个 Python 对象的引用。

```python
import json

row = {"id": 42, "title": "Old Title", "version": 1}
raw = json.dumps(row, ensure_ascii=False)
restored = json.loads(raw)
assert restored == row
```

序列化不是零成本：它增加编码、解码和数据传输开销。当前选择它，不是因为 JSON 总比别的格式好，而是因为它足够清楚，能够跨语言，并且适合演示本文的文章详情对象。

## 六、现在才有必要引入 Redis

我们已经确定新组件必须满足什么：

它是一个独立进程，A 和 B 都能通过网络访问它；它能按 Key 保存值；能把同一个 Key 的值返回给不同客户端；不能要求它理解 `posts` 表或数据库回源规则。

Redis 正好提供这种能力。对当前章节来说，我们只需要最基础的两个命令：

```redis
SET post:42:detail '{"id":42,"title":"Old Title","version":1}'
GET post:42:detail
```

我们此时选择 Redis，不是因为它的集群、持久化或淘汰能力已经用上了，而是因为它作为**独立的网络键值服务**，让不同应用进程访问同一份状态。

整个系统现在可以准确画成：

```text
                    /posts/42
                        |
                    负载均衡
                    /      \
                   v        v
                 应用 A    应用 B
                   |        |
                   +---+----+
                       |
                 Redis GET / SET
                       |
            +----------+----------+
            |                     |
          命中                  未命中
            |                     |
          返回                   应用查 MySQL
                                  |
                                  v
                              应用 SET 回填
                                  |
                                  v
                                返回
```

这张图不是 Redis 内部架构图，而是**业务请求和数据流**。真实 Redis 自己的调用链，我们接下来用源码验证。

## 七、源码精读一：B 的 GET 为什么能读到 A 写入的值？

固定版本：**Redis 7.2.4**。为了避免把后来版本的改动带入当前分析，以下函数名、签名和代码都以这个标签为准。

仓库：[redis/redis 7.2.4](https://github.com/redis/redis/tree/7.2.4)。

现在先只看一条正常请求：

```redis
GET post:42:detail
```

A 或 B 发出这条命令时，Redis 并不知道发送者是网站的哪个应用实例。它看到的是一个客户端连接，以及命令参数 `post:42:detail`。

打开 [`src/t_string.c`](https://github.com/redis/redis/blob/7.2.4/src/t_string.c)，找到 `getCommand(client *c)`。这里 `c` 是 Redis 的客户端上下文，它携带已经解析的命令参数。

**真实源码节选（Redis 7.2.4）：**

```c
void getCommand(client *c) {
    getGenericCommand(c);
}
```

这几行看起来简单得有些过分。为什么 `getCommand()` 不自己写一遍查找和回复？因为实际读取路径被放在 `getGenericCommand()` 里，其他需要相同读取语义的命令也可以复用。

继续在同文件中找到 `getGenericCommand()`。它会通过 `lookupKeyReadOrReply()` 查找当前数据库中的 Key。若没有找到，就沿客户端回复路径发送空值；找到后，检查类型，并把字符串值放入回复。

与当前问题直接相关的源码是：

```c
/* 真实源码节选：src/t_string.c，省略无关分支 */
if ((o = lookupKeyReadOrReply(c,c->argv[1],shared.null[c->resp])) == NULL)
    return C_OK;
```

这里要弄清楚输入和输出：

- `c->argv[1]`：客户端传入的 Key，也就是 `post:42:detail`。
- `c->db`：当前 Redis 客户端所选中的逻辑数据库。
- `lookupKeyReadOrReply()`：尝试查找该 Key，找不到时负责回复空值。
- `getGenericCommand()`：若找到字符串值，就把它作为响应返回给客户端。

我们接着沿调用链进入 [`src/db.c`](https://github.com/redis/redis/blob/7.2.4/src/db.c)。这里的 `lookupKeyReadOrReply()` 会调用 `lookupKeyRead()`，再沿 `lookupKeyReadWithFlags()` 到达底层 `lookupKey()`。

`lookupKey()` 最核心的一步是按照 Key 到数据库内部的字典中查找条目。

**真实源码节选（Redis 7.2.4）：**

```c
dictEntry *de = dictFind(db->dict,key->ptr);
```

在当前案例里，`db->dict` 是 Redis 自己管理的键空间，不是 MySQL 的表，也不是应用 A 或 B 的 Python 字典。

现在回看我们关心的问题：为什么 B 能读到 A 的缓存结果？

因为 A 和 B 的 `GET` 都被发送到**同一个 Redis 服务中的同一份键空间**，它们使用同一个 Key。Redis 不需要在 A、B 的进程内存之间复制对象。两者从一开始就把值保存在同一处。

这正是共享缓存给我们带来的最小新能力。

### GET 未命中以后，为什么源码没有继续查询 MySQL？

我们刚刚看到，找不到 Key 时 `lookupKeyReadOrReply()` 会生成 Redis 的空值回复，`getGenericCommand()` 就结束了。

Redis 的这个命令处理函数没有“去数据库找文章”的下一步。

于是应用层需要自己写：

```python
raw = redis.get(key)
if raw is None:
    row = db.read(post_id)   # 这是业务代码，不是 Redis 内部代码
```

**这是一条重要的设计边界：Redis 负责按 Key 存取值，应用负责决定怎样加载业务数据。**

到这里，我们已经从源码验证了 Cache-Aside 的前半段：读缓存，未命中则由应用自己处理。

## 八、源码精读二：A 的 SET 怎样让 Redis 保存结果？

现在沿上一节的同一次未命中往下走。

A 查询 MySQL 得到文章 42，然后执行：

```redis
SET post:42:detail '{"id":42,"title":"Old Title","version":1}'
```

打开同一个 [`src/t_string.c`](https://github.com/redis/redis/blob/7.2.4/src/t_string.c)，找到 `setCommand(client *c)`。

在 Redis 7.2.4 中，它负责解析 `SET` 的命令选项、处理传入的值对象，然后进入 `setGenericCommand()`。我们现在没有使用任何可选参数，所以只跟踪最普通的写入路径。

为什么不让 `setCommand()` 自己直接修改整个数据库？原因和 GET 类似：一些字符串写入命令能够共享真正的赋值处理逻辑，因此具体入口与通用实现被分开。

`setGenericCommand()` 在完成必要的检查后，调用：

```c
/* 真实源码节选：src/t_string.c */
setKey(c,c->db,key,val,setkey_flags);
```

我们接着进入 [`src/db.c`](https://github.com/redis/redis/blob/7.2.4/src/db.c) 的 `setKey()`。

这个函数处理“Key 原来不存在，应该新增”与“Key 已存在，应该覆盖”两种情况。对于我们的第一次回填，`post:42:detail` 还不存在，于是会走新增 Key 的路径；后续相同 Key 的 `SET` 则可能覆盖旧值。

**真实源码节选（Redis 7.2.4）：**

```c
if (!keyfound) {
    dbAdd(db,key,val);
}
```

`dbAdd()` 最终让这个 Key 进入 Redis 当前数据库的数据结构，而不是写入 A 的 Python 字典。

所以，从 Redis 自身来看，最简写入链是：

```text
A 发出 SET
    |
    v
Redis 接收并分派命令
    |
    v
setCommand()
    |
    v
setGenericCommand()
    |
    v
setKey()
    |
    +-- Key 原来不存在 → 新增
    |
    +-- Key 已存在     → 更新
    |
    v
Redis 返回命令结果
```

这里的核心设计动作只有一个：**把 Key 和序列化后的 Value 放入共享服务管理的数据集合中。**

我们不在本章展开 `server.dirty`、AOF、复制、过期和淘汰。它们真实存在，但在此刻理解“为什么 A 写入后 B 能读取”并不必需。

### 把应用层和 Redis 层两条链合在一起

现在已经有足够证据整理一次完整请求：

```text
A.get_post(42)
    |
    +--> Redis GET post:42:detail
    |         |
    |         +--> getCommand → lookupKey → 未命中
    |
    +--> A 调用 db.read(42)
    |         |
    |         +--> 返回文章 version=1
    |
    +--> Redis SET post:42:detail <JSON>
    |         |
    |         +--> setCommand → setKey → 写入键空间
    |
    +--> A 返回文章

B.get_post(42)
    |
    +--> Redis GET post:42:detail
              |
              +--> getCommand → lookupKey → 命中
              |
              +--> B 解析 JSON 并返回

B 本次没有调用 db.read(42)
```

这一次，我们不是先画出架构，再强迫读者接受它；图中的每一个动作都已经在业务代码或固定版本源码中有了对应位置。

## 九、可运行实验：让同一个 Key 经历七种情况

源码可以证明 Redis 具备什么行为，但不能单独证明我们的文章服务写得正确。所以接下来，我们把**应用层请求过程**完整跑一遍。

为了不要求你先安装 Docker 或配置 Redis，实验先用 `SharedKV` 模拟共享服务，并让它统计 `GET`、`SET` 次数。它与真实 Redis 的共同点只有 Key/Value 接口和共享状态；它**没有模拟网络、复制、TTL、持久化、异步 I/O 或实际 Redis 内部实现**。

实验文件名：`ch02_shared_cache_aside.py`。

完整脚本见文末附录。这里先沿着输入一步步看最重要的观察。

### 实验 A：两份本地缓存

```python
db = FakeDatabase()
local_a = LocalPostService(db, "A")
local_b = LocalPostService(db, "B")

local_a.get_post(42)
local_b.get_post(42)

assert db.reads == 2
```

A 的首次读取让它拥有自己的缓存，但 B 看不到，因此再次回源。这一断言就是本章开始时的失败。

### 实验 B：共享缓存

只改变存储位置，业务还是查询同一篇文章：

```python
db = FakeDatabase()
kv = SharedKV()
app_a = SharedPostService(db, kv, "A")
app_b = SharedPostService(db, kv, "B")

app_a.get_post(42)
app_b.get_post(42)

assert db.reads == 1
assert kv.gets == 2
assert kv.sets == 1
```

这三个数字各有含义：

- `DB reads=1`：只有 A 的首次未命中查数据库。
- `GET=2`：A、B 都向共享缓存查询。它们不是“零代价”。
- `SET=1`：只有 A 回填缓存，B 命中后不重复写入。

观察指标与机制之间是直接对应的。我们不必用“系统扩展性显著提升”这样的抽象口号来代替结果。

### 实验 C：应用实例重新创建

```python
app_a_restarted = SharedPostService(db, kv, "A-new")
app_a_restarted.get_post(42)
assert db.reads == 1
```

应用 A 的对象被重建了，但共享缓存对象仍然存在，因此不需要再次访问数据库。

这只是教学模型的生命周期演示，**不等于 Redis 一定会在自己重启后保存该 Key**。Redis 服务本身的故障与持久化属于另一个问题。

### 实验 D：错误的 Key

错误地使用一个固定的 `post:detail`：

```text
第一次：请求 42 → 缓存 post:detail = 文章 42
第二次：请求 43 → 命中 post:detail → 错误返回文章 42
```

**这个实验故意要求第二次返回错误值，以验证这个缺陷确实存在。** 然后改成 `post:{id}:detail`，相同输入就能得到正确结果。

### 实验 E：为什么返回后随手修改对象不会污染缓存？

示例中共享缓存保存的是 JSON 字符串。应用每次命中后使用 `json.loads()` 得到新的对象。于是调用方修改本次返回的 `dict`，不会直接改变共享缓存中已存储的 JSON。

这不是说 JSON 会帮我们处理一切数据一致性；它只解决当前这一个**进程内对象引用与共享数据表示**的问题。

### 实验 F：管理员修改文章以后呢？

最后我们做一个关键但暂时不修复的动作：

```text
缓存：文章 42，version=1，Old Title
数据库：文章 42，version=1，Old Title

管理员修改数据库：
数据库：文章 42，version=2，New Title

再次请求：
缓存仍返回 version=1，Old Title
```

这不是测试脚本的 bug，而是我们给缓存引入一份独立状态后自然得到的结果。

**缓存现在能够被 A/B 共享了，却也同样会共享同一份旧数据。**

这才是下一章要解决的问题。现在如果直接塞入 TTL、消息队列或者双写策略，只会把当前因果链打断。

### 实验 G：输出核对

执行：

```bash
python3 ch02_shared_cache_aside.py
```

我已在 Python 3.13.5 环境中实际运行完整脚本，输出如下：

```text
A 两份本地缓存：A/B 各首次读取，DB reads=2
B 共享缓存：A 回源并写缓存，B 命中；DB reads=1, GET=2, SET=1
C 重建应用 A：共享缓存仍保存文章 42；DB reads=1
D 错误 Key：请求文章 43，却返回 id=42（故意复现的缺陷）
E 按文章 ID 生成 Key：42、43 不再冲突；DB reads=2
F JSON 反序列化：修改返回对象不会直接修改共享缓存
G 管理员更新：DB version=2，缓存返回 version=1（留到第 3 章）
所有断言通过：只验证教学模型，不代表 Redis/MySQL 集成测试
```

这些输出不是估算值，而是执行脚本所得。它们证明的是**教学模型在给定顺序输入下的行为**，不代表真实 Redis 的延迟、吞吐量或者可用性。

## 十、如果有 Redis，可以亲自观察两个终端共享同一个 Key

上面的 Python 实验没有真实网络。如果你已经有 Redis 7.2.4，可以在两个终端连接到**同一个** Redis 实例，复现共享键空间。

本节是**可选操作指南**。我在当前环境中没有 Redis Server 可执行文件，因此没有把以下命令冒充已运行的 Redis 集成测试。

如果使用本地 Docker，可以在仅绑定回环地址的开发环境中启动：

```bash
# 需要本机 Docker。该命令是复现说明，不是本文已执行的验证。
docker run --rm --name cache-ch02 -p 127.0.0.1:6379:6379 redis:7.2.4
```

终端 A：

```bash
redis-cli -h 127.0.0.1 -p 6379 SET \
  tutorial:post:42:detail \
  '{"id":42,"title":"Old Title","version":1}'
```

预期 Redis 返回：

```text
OK
```

终端 B（单独新开连接）：

```bash
redis-cli -h 127.0.0.1 -p 6379 --raw GET tutorial:post:42:detail
```

预期返回刚才的 JSON 字符串。此时可以得出的结论非常有限、但很准确：**不同客户端连接访问同一个 Redis 实例的同一个 Key，可以读取同一份值。**

清理演示 Key：

```bash
redis-cli -h 127.0.0.1 -p 6379 DEL tutorial:post:42:detail
```

注意这仍没有模拟 MySQL，因为 Redis 不会自动回源。若要观察整个 Cache-Aside 过程，需要应用自己连接 Redis 和数据库并执行前面的读取逻辑。

## 十一、Redis 共享缓存真的总比本地缓存好吗？

现在可以认真回答这个问题：**不是。**

第一章的本地缓存一次命中，主要是当前进程里的字典访问。第二章改成 Redis 后，命中通常意味着至少一次客户端到 Redis 的网络往返，外加协议编码与数据序列化。

对于每个实例经常读取、几乎不变化的极少数热点文章，本地缓存的访问成本可能更低。我们为什么还要考虑 Redis？因为当前新增的要求不是“单次读取尽可能少走网络”，而是**多个应用实例可以复用同一份已加载的数据**。

本章引入 Redis 解决了：

- A 查过文章 42 后，B 能使用同一份缓存结果；
- 新增应用实例不必先与所有老实例交换缓存字典；
- Key/Value 的存取成为独立于业务实例的服务边界。

但它也产生了代价：

- 即使缓存命中，应用也要访问共享服务；
- 新增了 Redis 网络连接、服务容量和可用性等运行条件；
- 多个实例会共同依赖这份共享缓存；
- 一份缓存数据一旦过旧，A、B 可能一起读到过旧结果。

不要把这一节理解为“下一步就应该加本地 L1、Redis Cluster、分布式锁和 TTL”。这些设计不是当前场景的必然要求。我们只是承认当前机制的收益与边界。

### 多个实例同时未命中怎么办？

再看一个尚未解决的输入：

```text
T0 Redis 中没有文章 42
T1 A 执行 GET 42 → miss
T1 B 也执行 GET 42 → miss
T2 A 查数据库
T2 B 也查数据库
```

即使已经使用 Redis，A 和 B 仍可能同时访问数据库。共享缓存并没有自动让“检查缓存 → 查询数据库 → 写入缓存”三个步骤变成跨客户端原子操作。

本章不继续追这个并发问题，因为现在我们只验证了**顺序请求的跨实例复用**。在第 5 章，热门文章失效造成大量并发回源时，我们再研究为什么需要请求合并，以及它能解决什么、不能解决什么。

## 十二、下一步不是更快，而是更正确

现在再把整个过程从头复述一次：

```text
最初只有一个应用进程
    |
    v
按文章 ID 放进本地字典
    |
    v
重复读取不再反复查询数据库
    |
    v
应用部署为 A 和 B
    |
    v
A 的字典不在 B 的内存中
    |
    v
A、B 都需要第一次回源
    |
    v
尝试互相同步字典
    |
    v
实例加入、离线、同步成本不断增加
    |
    v
改为访问一个独立的共享键值服务
    |
    v
由应用实现 Cache-Aside：GET miss → 查 DB → SET
    |
    v
A 回填后，B 可以直接命中同一 Key
    |
    v
管理员把文章 42 更新为 version=2
    |
    v
Redis 仍保存 version=1
    |
    v
下一章：什么时候该删除缓存？
```

本章的中心问题从来不是“Redis 的所有功能是什么”。我们真正解决的是：**缓存原来属于某个应用进程，怎样让多个独立实例复用它？**

而我们付出的代价是，应用与数据库之外又出现了一份独立的文章数据。

当管理员把 `Old Title` 改成 `New Title`，这份缓存不知道数据库已经变化。因为共享缓存比本地缓存更容易被多个实例命中，过旧结果还可能被更多用户同时看到。

所以自然的下一问是：

**既然 Redis 不知道 MySQL 什么时候更新，那么管理员修改文章后，应用究竟该怎样处理已经存在的缓存？**

**下一篇：《管理员已经修改标题，为什么用户仍然看到旧文章？——从双写困境到更新数据库后删除缓存》**。

---

## 附录 A：完整可执行 Python 实验

下面是本章实际运行过的完整脚本。只依赖 Python 标准库，无需 Redis、MySQL、Docker 或额外 Python 包。建议先预测 A～G 的输出，再执行并逐条核对。

<details>
<summary>展开 ch02_shared_cache_aside.py 完整代码</summary>

```python
from copy import deepcopy
import json


class FakeDatabase:
    def __init__(self):
        self.rows = {
            42: {"id": 42, "title": "Old Title", "version": 1},
            43: {"id": 43, "title": "Another Post", "version": 1},
        }
        self.reads = 0

    def read(self, post_id):
        self.reads += 1
        return deepcopy(self.rows.get(post_id))

    def rename(self, post_id, new_title):
        self.rows[post_id]["title"] = new_title
        self.rows[post_id]["version"] += 1


class LocalPostService:
    def __init__(self, db, name):
        self.db, self.name = db, name
        self.cache = {}

    def get_post(self, post_id):
        if post_id in self.cache:
            return deepcopy(self.cache[post_id])
        row = self.db.read(post_id)
        if row is not None:
            self.cache[post_id] = deepcopy(row)
        return row


class SharedKV:
    """教学模型：同一份 KV 状态被多个服务对象引用。"""
    def __init__(self):
        self.data = {}
        self.gets = 0
        self.sets = 0

    def get(self, key):
        self.gets += 1
        return self.data.get(key)

    def set(self, key, value):
        if not isinstance(value, str):
            raise TypeError("本实验约定用字符串保存 JSON")
        self.sets += 1
        self.data[key] = value


class SharedPostService:
    def __init__(self, db, kv, name, *, bad_key=False):
        self.db, self.kv, self.name = db, kv, name
        self.bad_key = bad_key

    def get_post(self, post_id):
        key = "post:detail" if self.bad_key else f"post:{post_id}:detail"
        raw = self.kv.get(key)
        if raw is not None:
            return json.loads(raw)

        row = self.db.read(post_id)
        if row is not None:
            self.kv.set(key, json.dumps(row, ensure_ascii=False, sort_keys=True))
        return row


def main():
    db = FakeDatabase()
    local_a = LocalPostService(db, "A")
    local_b = LocalPostService(db, "B")
    assert local_a.get_post(42)["version"] == 1
    assert local_b.get_post(42)["version"] == 1
    assert local_a.get_post(42)["version"] == 1
    assert local_b.get_post(42)["version"] == 1
    assert db.reads == 2
    print("A 两份本地缓存：A/B 各首次读取，DB reads=2")

    db = FakeDatabase()
    kv = SharedKV()
    app_a = SharedPostService(db, kv, "A")
    app_b = SharedPostService(db, kv, "B")
    assert app_a.get_post(42)["version"] == 1
    assert app_b.get_post(42)["version"] == 1
    assert (db.reads, kv.gets, kv.sets) == (1, 2, 1)
    print("B 共享缓存：A 回源并写缓存，B 命中；DB reads=1, GET=2, SET=1")

    app_a_restarted = SharedPostService(db, kv, "A-new")
    assert app_a_restarted.get_post(42)["version"] == 1
    assert db.reads == 1
    print("C 重建应用 A：共享缓存仍保存文章 42；DB reads=1")

    db = FakeDatabase()
    kv = SharedKV()
    bad = SharedPostService(db, kv, "bad", bad_key=True)
    assert bad.get_post(42)["id"] == 42
    wrong = bad.get_post(43)
    assert wrong["id"] == 42
    assert db.reads == 1
    print("D 错误 Key：请求文章 43，却返回 id=42（故意复现的缺陷）")

    db = FakeDatabase()
    kv = SharedKV()
    good = SharedPostService(db, kv, "good")
    assert good.get_post(42)["id"] == 42
    assert good.get_post(43)["id"] == 43
    assert db.reads == 2
    assert set(kv.data) == {"post:42:detail", "post:43:detail"}
    print("E 按文章 ID 生成 Key：42、43 不再冲突；DB reads=2")

    returned = good.get_post(42)
    returned["title"] = "tampered by caller"
    assert good.get_post(42)["title"] == "Old Title"
    print("F JSON 反序列化：修改返回对象不会直接修改共享缓存")

    db.rename(42, "New Title")
    stale = good.get_post(42)
    assert stale == {"id": 42, "title": "Old Title", "version": 1}
    assert db.rows[42] == {"id": 42, "title": "New Title", "version": 2}
    print("G 管理员更新：DB version=2，缓存返回 version=1（留到第 3 章）")
    print("所有断言通过：只验证教学模型，不代表 Redis/MySQL 集成测试")


if __name__ == "__main__":
    main()
```

</details>

## 附录 B：固定源码、调用者和验证边界

本章所有“真实源码节选”均以 **Redis 7.2.4** 为准，未混用 `unstable` 分支或其他版本的函数签名。

| 实现位置 | 调用者 / 入口 | 当前需要观察的输入与动作 | 输出或后续 |
| --- | --- | --- | --- |
| [`src/t_string.c`：`getCommand()`](https://github.com/redis/redis/blob/7.2.4/src/t_string.c) | Redis 命令分派 | 接收当前客户端 `c`，调用通用 GET 处理函数 | 进入 `getGenericCommand()` |
| [`src/t_string.c`：`getGenericCommand()`](https://github.com/redis/redis/blob/7.2.4/src/t_string.c) | `getCommand()` | 使用 `c->argv[1]` 查询 Key、检查是否字符串 | 将值或空回复交给客户端回复路径 |
| [`src/db.c`：`lookupKeyReadOrReply()`](https://github.com/redis/redis/blob/7.2.4/src/db.c) | `getGenericCommand()` | 查询 `c->db` 的 Key，未找到时准备空回复 | 返回 Redis 数据对象或 `NULL` |
| [`src/db.c`：`lookupKey()`](https://github.com/redis/redis/blob/7.2.4/src/db.c) | `lookupKeyRead()` 等 | 使用 Key 在 Redis 键空间中查找 | 返回对象或空结果；其余访问统计等细节不在本章展开 |
| [`src/t_string.c`：`setCommand()`](https://github.com/redis/redis/blob/7.2.4/src/t_string.c) | Redis 命令分派 | 解析 `SET key value` 并调用通用 SET | 进入 `setGenericCommand()` |
| [`src/t_string.c`：`setGenericCommand()`](https://github.com/redis/redis/blob/7.2.4/src/t_string.c) | `setCommand()` 等 | 检查条件并使用 `setKey()` 写入 | 准备成功回复，命令执行结束 |
| [`src/db.c`：`setKey()`](https://github.com/redis/redis/blob/7.2.4/src/db.c) | `setGenericCommand()` | 对不存在的 Key 新增，对已有 Key 更新 | 更新 Redis 当前数据库的键值 |

补充资料：[Redis 官方 GET 文档](https://redis.io/docs/latest/commands/get/)、[Redis 官方 SET 文档](https://redis.io/docs/latest/commands/set/)。

**实验边界：** 已执行 Python 教学模型，已对照 Redis 7.2.4 的指定源码路径；**尚未执行真实 Redis/MySQL 双实例集成测试**。本章不声称 Cache-Aside 在并发未命中时只有一次回源，也不声称 Redis 自动保证缓存与数据库一致。文章只分析普通字符串 GET/SET 正常路径，不涉及权限认证、Redis Cluster 路由、故障恢复、TTL 与内存淘汰。
