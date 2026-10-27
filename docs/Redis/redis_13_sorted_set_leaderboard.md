---
title: "第十三章：排行榜为什么不能只用 Set？从成员唯一到 Sorted Set"
sidebar_label: "13 - Sorted Set 与活动排行榜"
sidebar_position: 14
description: "沿活动报名案例，从去重名单走向动态积分排行榜，渐进式精读 Redis 7.2.0 的 ZADD、ZRANGE、ZREVRANK，以及 listpack 和字典加跳表的设计取舍。"
tags:
  - Redis
  - 源码精读
  - Sorted Set
  - ZSet
  - listpack
  - skiplist
---

# 第十三章：排行榜为什么不能只用 Set？从成员唯一到 Sorted Set

上一章，我们用 Set 为活动 `1001` 建立了一份不重复的报名名单。

```redis
SADD activity:1001:members user:42
SADD activity:1001:members user:57
SADD activity:1001:members user:63
```

这份名单能回答一个明确的问题：**谁已经报名？** 无论 `user:42` 重复提交多少次，Set 中都只有一个 `user:42`。

但活动开始后，运营提出了一个新需求：用户完成任务可以获得积分，我们要展示当前排名前三的用户，并允许每个人查询自己的名次。

这个需求看起来仍然与“用户集合”有关。是不是沿用 Set 就够了？

先别急着认识新数据类型。我们沿着原有方案尝试一下，看看缺的到底是什么。

> **源码基线**：本章固定使用 [Redis 7.2.0](https://github.com/redis/redis/tree/7.2.0)，主要阅读 [`src/t_zset.c`](https://github.com/redis/redis/blob/7.2.0/src/t_zset.c)、[`src/object.c`](https://github.com/redis/redis/blob/7.2.0/src/object.c) 和 [`src/server.h`](https://github.com/redis/redis/blob/7.2.0/src/server.h)。真实源码节选来自该版本，非连续摘取会明确标注；概念伪代码不冒充真实实现。命令输出均为**预期结果**，本章编写环境没有实际执行 Redis 服务。

## 1. Set 能保证唯一，为什么不能直接做积分榜？

活动中现在有三位用户：

| 用户 | 积分 |
| --- | ---: |
| user:42 | 90 |
| user:57 | 70 |
| user:63 | 90 |

如果继续只使用 Set，我们可能把积分拼进成员的字符串中：

```redis
SADD activity:1001:rank "user:42:90"
SADD activity:1001:rank "user:57:70"
SADD activity:1001:rank "user:63:90"
```

但当 `user:57` 获得了 25 分，我们直接插入 `"user:57:95"`，Set 会把它当成另一个不同的字符串。旧的 `"user:57:70"` 仍然存在。

这不是 Redis 出错，而是我们的建模出了问题：**成员是否唯一，应该根据用户 ID 判断，而不是根据“用户 ID + 当前分数”判断。**

我们可以改用已经学习过的 Hash：

```redis
HSET activity:1001:scores user:42 90 user:57 70 user:63 90
```

这样就能通过 `HGET` 按用户查分，用 `HINCRBY` 更新积分。

如果业务只需要按用户 ID 获取积分，Hash 已经足够合理。

但运营要的是：

```text
谁排在前 3 名？
user:57 当前是第几名？
```

普通 Hash 不会替我们维护“按分数排列”的有序索引。可以调用 `HGETALL` 把所有数据取回应用端，再排序、截取前三名；当成员越来越多、排行榜又被频繁查询时，这意味着重复传输和排序整个集合。

因此，我们真正缺少的并不是一个更复杂的“去重”操作，而是：

**同一个用户只出现一次，同时系统始终知道用户应当按积分排在什么位置。**

这才引出 Redis 的 Sorted Set，通常简称 **ZSet**。

在当前系统中，Set 仍然保存报名事实，ZSet 则负责积分排行榜。两种数据承担的是不同职责，并不是后者把前者完全替换掉。

## 2. 先实现最小的排行榜

不使用 Redis 的话，最简单的 Python 写法可能是：

```python
# 教学化简：表达所需行为，不是 Redis 的源码
scores = {
    "user:42": 90,
    "user:57": 70,
    "user:63": 90,
}

leaders = sorted(
    scores.items(),
    key=lambda pair: (pair[1], pair[0]),
    reverse=True,
)[:3]
print(leaders)
# [('user:63', 90), ('user:42', 90), ('user:57', 70)]
```

这段代码正确，也很直观。问题只是在每次查询时都重新排序。

我们希望数据服务不仅保存用户和分数的映射，还能直接查询有序的结果。

ZSet 对外提供的基本模型是：

```text
member      score
user:42     90
user:57     70
user:63     90
```

这里有两条规则：

- 每个 **member** 在一个 ZSet 中是唯一的；
- **score** 可以重复，允许多个不同成员拥有相同分数。

我们只需要写入：

```redis
DEL activity:1001:rank
ZADD activity:1001:rank 90 user:42 70 user:57 90 user:63
```

第二条命令的预期返回是：

```text
(integer) 3
```

它表示新增了三个成员，不是积分之和。

现在获取积分从高到低的前三名：

```redis
ZRANGE activity:1001:rank 0 2 REV WITHSCORES
```

在默认 `redis-cli` 输出格式下，预期结果依次是：

```text
1) "user:63"
2) "90"
3) "user:42"
4) "90"
5) "user:57"
6) "70"
```

这里的 `REV` 表示反向，即从高分到低分；`WITHSCORES` 表示同时返回分数。

为什么同为 90 分的 `user:63` 会排在 `user:42` 前面？Redis 在分数相同时，会按成员的字节字典序决定先后；使用 `REV` 时，同分成员按反向字典序返回。

这是一条**确定的排序规则**，不等于比赛中的“并列第一”。如果业务规定同分必须并列显示，就不能直接把 Redis 的位置索引当作竞赛名次。

## 3. 更新分数后，排行榜会发生什么？

现在 `user:57` 完成任务，分数从 70 变成 95：

```redis
ZADD activity:1001:rank 95 user:57
```

预期返回：

```text
(integer) 0
```

它不是说更新失败了。普通 `ZADD` 不带 `CH` 选项时，默认返回的是**新增成员数量**。`user:57` 原本已经存在，只更新分数，因此新增成员数为零。

再次执行：

```redis
ZRANGE activity:1001:rank 0 2 REV WITHSCORES
```

预期返回：

```text
1) "user:57"
2) "95"
3) "user:63"
4) "90"
5) "user:42"
6) "90"
```

现在 `user:57` 位于最前面。查询它的排名：

```redis
ZREVRANK activity:1001:rank user:57
```

预期结果：

```text
(integer) 0
```

`ZREVRANK` 返回的是降序的、从零开始的**索引**；应用程序如果要显示“第 1 名”，可以把结果加一。`ZRANK` 则是从低分到高分计算索引。

到这里，我们已经拥有了能够保存用户分数、随着更新维护顺序、直接查询 Top N 和名次的集合。

但最关键的问题还没回答：**Redis 怎样同时保证成员唯一，并在分数改变时维护它的位置？**

这需要进入真实源码。

## 4. ZADD 从哪里开始执行？

上一章已经见过 `SADD` 的命令入口，因此这一章不再重复网络收包和命令分发。

在 Redis 7.2.0 的 [`src/t_zset.c`](https://github.com/redis/redis/blob/7.2.0/src/t_zset.c) 中，`ZADD` 对应：

**真实源码节选：**

```c
void zaddCommand(client *c) {
    zaddGenericCommand(c,ZADD_IN_NONE);
}
```

它并不直接操作底层数据，而是交给 `zaddGenericCommand()`。

为什么把逻辑提取到一个通用函数？因为另外一条 `ZINCRBY` 命令也需要定位成员并更新分数，只是它更新的是“在旧分数基础上增加指定值”。

**真实源码节选：**

```c
void zincrbyCommand(client *c) {
    zaddGenericCommand(c,ZADD_IN_INCR);
}
```

现在只沿普通 `ZADD` 的正常路径往下看。

首先，Redis 要解析客户端传来的分数。由于 ZSet 的 score 是双精度浮点值，因此源码会调用：

**真实源码节选：**

```c
if (getDoubleFromObjectOrReply(c,c->argv[scoreidx+j*2],&scores[j],NULL)
    != C_OK) goto cleanup;
```

这一步解决的是“客户端给的 score 能否解释为数值”。例如 `NaN` 不能作为有效的 ZSet score；使用双精度浮点也意味着积分设计需要考虑大整数精度边界。对于一般整数积分示例，使用较小数值即可。

接下来，Redis 查找排行榜对应的顶层 Key：

**真实源码节选：**

```c
zobj = lookupKeyWrite(c->db,key);
if (checkType(c,zobj,OBJ_ZSET)) goto cleanup;
if (zobj == NULL) {
    if (xx) goto reply_to_client;
    zobj = zsetTypeCreate(elements, sdslen(c->argv[scoreidx+1]->ptr));
    dbAdd(c->db,key,zobj);
} else {
    zsetTypeMaybeConvert(zobj, elements);
}
```

这就把 Redis 顶层数据库与 ZSet 内部成员区分开了：

```text
Redis 数据库
   |
   +-- activity:1001:rank --> 一个 ZSet 对象
                                  |
                                  +-- user:42 / 90
                                  +-- user:57 / 70
                                  +-- user:63 / 90
```

`lookupKeyWrite()` 查找的是整个 `activity:1001:rank`，不是其中的 `user:57`。不存在时，`zsetTypeCreate()` 创建一个 ZSet 值对象，再通过 `dbAdd()` 挂到顶层数据库。

接下来，`zaddGenericCommand()` 对每组 `score/member` 调用：

**真实源码节选：**

```c
int retval = zsetAdd(zobj, score, ele, flags, &retflags, &newscore);
```

这个 `zsetAdd()` 才是真正维护成员唯一性和排序的关键函数。

但源码接下来并不是一条固定路径，而是先判断 ZSet 当前采用哪种内部编码。

为什么要有不同编码？先回到只有三个人的排行榜。

## 5. 只有三个成员时，是否需要两套索引？

三个成员的排行榜很小。我们完全可以用一块紧凑的存储区域按顺序保存成员和分数：

```text
[user:57, 70, user:42, 90, user:63, 90]
```

如果有人更新分数，就扫描找到该用户，然后重新把它放到正确位置。

这在几十万成员时可能很慢，但对于三个成员，扫描几个条目的成本不高；反而维护许多指针和索引结构可能浪费内存。

Redis 7.2.0 对较小的 ZSet 采用 **listpack** 编码。我们可以先把它理解为一种用于紧凑保存多条记录的连续内存结构。

在 [`src/object.c`](https://github.com/redis/redis/blob/7.2.0/src/object.c) 中：

**真实源码节选：**

```c
robj *createZsetListpackObject(void) {
    unsigned char *lp = lpNew(0);
    robj *o = createObject(OBJ_ZSET,lp);
    o->encoding = OBJ_ENCODING_LISTPACK;
    return o;
}
```

什么时候使用这种编码？`src/t_zset.c` 的 `zsetTypeCreate()` 会使用成员数量和成员长度的提示来选择初始编码。

**真实源码节选：**

```c
if (size_hint <= server.zset_max_listpack_entries &&
    val_len_hint <= server.zset_max_listpack_value)
{
    return createZsetListpackObject();
}
```

当前只有三个短成员名，默认配置下通常就会创建 listpack 编码的 ZSet。

现在回到 `zsetAdd()`。

**真实源码节选：**

```c
if (zobj->encoding == OBJ_ENCODING_LISTPACK) {
    unsigned char *eptr;
    if ((eptr = zzlFind(zobj->ptr,ele,&curscore)) != NULL) {
```

`zzlFind()` 会在紧凑结构中寻找成员。如果找到，就得到当前的旧分数 `curscore`；找不到才考虑新增。

对我们的 `user:57` 来说，原分数是 `70`，更新后的分数是 `95`。如果分数发生变化，源码执行：

**真实源码节选：**

```c
if (score != curscore) {
    zobj->ptr = zzlDelete(zobj->ptr,eptr);
    zobj->ptr = zzlInsert(zobj->ptr,ele,score);
    *out_flags |= ZADD_OUT_UPDATED;
}
```

为什么不是直接把 `70` 改为 `95`？因为这个 ZSet 本身保存着**按 score 排好的顺序**。

如果只替换数字，不移动成员，就会破坏排行榜的排序结果。Redis 先从旧位置删除，再按照新分数插入到正确位置。

这一段代码正好解释前面的可见变化：

```text
user:57 原来是 70 分
          |
          v
zzlFind() 找到旧记录
          |
          v
zzlDelete() 移除旧的成员/分数对
          |
          v
zzlInsert() 按 95 分重新插入
          |
          v
user:57 移到榜首
```

在 `zzlInsert()` 中，Redis 不仅比较分数，还会在相同分数时比较成员。

**真实源码中的两个关键条件（非连续摘取）：**

```c
if (s > score) {
    /* ... */
} else if (s == score) {
    if (zzlCompareElements(eptr,(unsigned char*)ele,sdslen(ele)) > 0) {
        /* ... */
    }
}
```

这两处比较对应我们观察到的排序规则：先按分数升序保存，同分再按成员的字典序决定位置。查询时用 `REV` 就能得到对应的降序结果。

到这里，完全不需要跳表，我们就能解释**小型 ZSet 如何新增成员、调整分数、维护顺序**。

但这个实现有一个自然的规模限制。

当活动报名人数从三人增长到几十万人时，我们不可能仍然把线性扫描和插入移动当成唯一的解决办法。

## 6. 成员增多以后，为什么一种索引不够了？

对于一个大型排行榜，我们至少需要两种不同的查找：

**按用户查分**：运营给出 `user:57`，系统尽快找到其当前分数。

**按积分查排名**：运营要求前一百名，或者询问 `user:57` 前面有多少人。

仅用普通哈希表，适合按成员查找，但不天然维护全局排序。

仅用普通有序链表，便于顺序遍历，但按成员查找和按名次定位成本可能很高。

Redis 7.2.0 为更大的 ZSet 使用一个组合结构：**字典（dict） + 跳表（skiplist）**。

在 [`src/server.h`](https://github.com/redis/redis/blob/7.2.0/src/server.h) 中，对应真实结构为：

```c
typedef struct zset {
    dict *dict;
    zskiplist *zsl;
} zset;
```

现在只给两个字段最小定义：

- `dict`：按成员快速查找它是否存在、当前分数是多少。
- `zsl`：维护按分数排列的有序结构，支持范围查找和排名定位。

它们不是两个独立的排行榜，而是同一批成员的两种索引视图。

```text
                 一个 ZSet 对象
                      |
             +--------+--------+
             |                 |
             v                 v
        字典 dict           跳表 zsl
             |                 |
        按成员查找         按分数排序
             |                 |
        user:57 -> 95      70 -> 90 -> 95
```

源码里两种结构还共享相应成员使用的 SDS 字符串，避免在两份索引中无谓地复制成员内容；具体内存所有权约定见 `t_zset.c` 文件开头的注释。

为什么是跳表，而不是普通链表？这里先知道一点即可：跳表通过多层前向链接，在查找有序位置时能够跳过一段节点，而不是每次都从头逐个扫描。它的具体层高策略不是当前理解 ZSet 的必要前置知识。

现在回到写入路径，看两套索引怎样配合。

## 7. 从源码验证：字典与跳表怎样同时更新？

继续阅读 `zsetAdd()`。如果当前对象已经采用 `OBJ_ENCODING_SKIPLIST`，源码会先执行：

**真实源码节选 — `src/t_zset.c`：**

```c
if (zobj->encoding == OBJ_ENCODING_SKIPLIST) {
    zset *zs = zobj->ptr;
    zskiplistNode *znode;
    dictEntry *de;

    de = dictFind(zs->dict,ele);
```

与小 ZSet 的 `zzlFind()` 相比，这里改成了 `dictFind()`，先通过字典查找成员。

当 `user:57` 已经存在时，Redis 从字典条目获得旧分数。若新旧分数不同，继续执行：

**真实源码节选：**

```c
if (score != curscore) {
    znode = zslUpdateScore(zs->zsl,curscore,ele,score);
    dictSetVal(zs->dict, de, &znode->score); /* Update score ptr. */
    *out_flags |= ZADD_OUT_UPDATED;
}
```

这几行说明了为什么不能只修改字典里的分数。

`zslUpdateScore()` 先处理有序位置：如果新分数并未破坏与相邻节点的顺序，可以原位修改；否则需要调整节点所在的位置。

随后 `dictSetVal()` 更新字典条目，使其继续指向当前跳表节点上的分数。

否则就会出现一种错误：按成员查询已经得到 95 分，按排名读取却仍然认为它在 70 分的位置。

现在，输入 `(user:57, 95)` 的结果很明确：同一个 ZSet 中，**按 member 查找的索引和按 score 排序的索引同时得到一致的更新**。

再看新增一个此前不存在的用户时发生什么。

**真实源码节选：**

```c
ele = sdsdup(ele);
znode = zslInsert(zs->zsl,score,ele);
serverAssert(dictAdd(zs->dict,ele,&znode->score) == DICT_OK);
```

这段代码告诉我们：

1. 为新成员准备需要保存的字符串。
2. `zslInsert()` 在跳表中找到分数对应的位置，创建节点。
3. `dictAdd()` 再为这个成员建立字典条目，其值指向跳表节点的分数。

这里的设计不是“用跳表代替字典”，而是为了两个不同的查询需求**维护两种索引**。

因此，在成员量足够大时，Redis 选择的不是某一种万能数据结构，而是将各自擅长的操作组合起来。

## 8. 要查第几名，为什么还需要 span？

现在我们可以快速找到用户，也可以按分数维护顺序。但还有最后一个问题：

```redis
ZREVRANK activity:1001:rank user:57
```

这要求返回用户的位置索引，而不只是分数。

如果跳表只是一个普通的链表，我们可能需要从开头开始数，有多少个成员在目标之前。

Redis 的跳表为每一层前向链接记录了一个额外的信息：`span`，也就是**跨度**。

**真实源码节选 — `src/server.h`：**

```c
typedef struct zskiplistNode {
    sds ele;
    double score;
    struct zskiplistNode *backward;
    struct zskiplistLevel {
        struct zskiplistNode *forward;
        unsigned long span;
    } level[];
} zskiplistNode;
```

这里不需要一下理解整个跳表算法，只需要把 `span` 理解为：顺着当前这一层的前向链接，可以跨过多少个底层节点。

在 `src/t_zset.c` 的 `zslGetRank()` 中，真正推进排名的关键两行是：

**真实源码节选：**

```c
rank += x->level[i].span;
x = x->level[i].forward;
```

它体现了一个很直接的思路：**如果一条索引链接跨过五个成员，那计算名次就能一次累加五，而不必依次走过那五个底层节点。**

Redis 还通过 `zsetRank()` 将这种内部排名换成客户端使用的位置索引。

对于跳表编码，`zsetRank()` 先从字典查到该成员的分数，再调用：

**真实源码关键行（非连续摘取）：**

```c
de = dictFind(zs->dict,ele);
/* ... */
rank = zslGetRank(zsl,score,ele);
```

获得内部排名后，Redis 再根据升序或降序返回：

**真实源码节选：**

```c
if (reverse)
    return llen-rank;
else
    return rank-1;
```

为什么升序需要减一？因为 `zslGetRank()` 的内部计数从 **1** 开始，而 `ZRANK` / `ZREVRANK` 的客户端索引从 **0** 开始。

为什么降序使用 `llen-rank`？因为内部按升序组织，反向排名可以用总成员数减去升序排名得到。

比如当前从低到高是：

```text
user:42(90) -> user:63(90) -> user:57(95)
```

`user:57` 的内部升序排名是 3，总成员数也是 3，因此降序索引 `3 - 3 = 0`，对应榜首。

这正好与我们先前的 `ZREVRANK` 预期结果吻合。

对于紧凑的 listpack 编码，`zsetRank()` 会遍历有序条目计数；命令语义保持一致，内部执行成本却不同。

至此，我们才真正解释了为何跳表不仅要维护有序关系，还需要为链接记录跨度。

## 9. 什么时候从 listpack 转为字典 + 跳表？

我们已经知道，小排行榜可以采用紧凑编码，大排行榜需要更适合成员定位和排序访问的结构。

Redis 什么时候进行转换？

继续看 `zsetAdd()` 中原来使用 listpack 的分支。

**真实源码节选：**

```c
if (zzlLength(zobj->ptr)+1 > server.zset_max_listpack_entries ||
    sdslen(ele) > server.zset_max_listpack_value ||
    !lpSafeToAdd(zobj->ptr, sdslen(ele)))
{
    zsetConvertAndExpand(zobj, OBJ_ENCODING_SKIPLIST, zsetLength(zobj) + 1);
}
```

在新增成员会突破 listpack 允许的元素数量、成员字符串尺寸，或无法安全继续扩充 listpack 时，Redis 会转换编码。

要注意，Redis 报告的 `skiplist` 编码实际上包含**字典和跳表**，并非只有跳表节点。

在 `zsetConvertAndExpand()` 内部可以直接看到这两项资源的创建。

**真实源码节选 — `src/t_zset.c`：**

```c
zs = zmalloc(sizeof(*zs));
zs->dict = dictCreate(&zsetDictType);
zs->zsl = zslCreate();
```

随后，Redis 将原本保存在 listpack 中的成员与分数依次迁移到新结构。

**真实源码节选：**

```c
node = zslInsert(zs->zsl,score,ele);
serverAssert(dictAdd(zs->dict,ele,&node->score) == DICT_OK);
```

最后修改 ZSet 对象的存储指针与编码：

**真实源码节选：**

```c
zobj->ptr = zs;
zobj->encoding = OBJ_ENCODING_SKIPLIST;
```

回头看这条路径，就知道为什么 Redis 没有一开始为每一个排行榜都构建两套索引。

只有少量成员时，紧凑存储的额外索引开销更低；当成员变多后，按成员查找和按分数维护排序的成本更值得优化。

Redis 根据具体规模采取不同实现，但用户看到的 `ZADD`、`ZRANGE`、`ZREVRANK` 命令语义没有改变。

## 10. 实验：观察编码从 listpack 变成 skiplist

接下来，用一个独立的 Redis 容器验证编码转换。

为了只插入几条记录就能触发阈值，我们将 `zset-max-listpack-entries` 设为 4。这个实例仅用于实验，避免影响已有应用。

```bash
docker run -d \
  --name redis-zset-lab \
  -p 127.0.0.1:6383:6379 \
  redis:7.2.0 \
  --save "" \
  --appendonly no \
  --zset-max-listpack-entries 4
```

先检查连接：

```bash
docker exec redis-zset-lab redis-cli PING
```

预期：

```text
PONG
```

清空一个只用于本次实验的 Key，再写入四个成员：

```bash
docker exec redis-zset-lab redis-cli DEL activity:1001:rank:encoding

docker exec redis-zset-lab redis-cli \
  ZADD activity:1001:rank:encoding 10 u1 20 u2 30 u3 40 u4
```

预期第二条命令返回 `(integer) 4`。

查看编码：

```bash
docker exec redis-zset-lab redis-cli \
  OBJECT ENCODING activity:1001:rank:encoding
```

预期：

```text
"listpack"
```

继续添加第五个成员：

```bash
docker exec redis-zset-lab redis-cli \
  ZADD activity:1001:rank:encoding 50 u5
```

再次查看编码：

```bash
docker exec redis-zset-lab redis-cli \
  OBJECT ENCODING activity:1001:rank:encoding
```

预期：

```text
"skiplist"
```

数据结构现在经历了这样一次转换：

```text
listpack：紧凑保存四个成员
            |
            | 再添加一个成员，数量超过阈值 4
            v
skiplist：字典 dict + 有序跳表 zsl
```

最后再查询排行榜：

```bash
docker exec redis-zset-lab redis-cli \
  ZRANGE activity:1001:rank:encoding 0 -1 REV WITHSCORES
```

预期结果依次为 `u5/50`、`u4/40`、`u3/30`、`u2/20`、`u1/10`。

**这才是本实验最有价值的观察：底层存储结构发生变化，但客户端无需改变查询方式。**

> 验证边界：以上 Docker 操作是可复现的实验说明，命令输出根据 Redis 7.2.0 的实现推导。当前写作环境无法启动 Redis 服务，不将预期输出写成实测结果。

完成后删除实验容器：

```bash
docker rm -f redis-zset-lab
```

## 11. 再回到活动积分：增加积分与直接改分数的区别

前面的 `ZADD` 是直接把 `user:57` 的分数设置成 `95`。但如果业务语义是“用户完成一次任务，**在已有积分上增加 25**”，客户端先 `ZSCORE`、计算，再 `ZADD` 又会带来上一专题研究过的多命令竞争问题。

因此 Redis 还提供 `ZINCRBY`，将积分增加放进同一次命令执行中：

```redis
DEL activity:1001:rank
ZADD activity:1001:rank 90 user:42 70 user:57 90 user:63
ZINCRBY activity:1001:rank 25 user:57
```

预期最后一条命令返回新分数：

```text
"95"
```

再次查询：

```redis
ZRANGE activity:1001:rank 0 0 REV WITHSCORES
ZREVRANK activity:1001:rank user:57
```

预期榜首是 `user:57/95`，降序索引为 `0`。

这里可以再次对应源码：`zincrbyCommand()` 也通过 `zaddGenericCommand()` 进入 `zsetAdd()`；内部读取旧 score 并计算增量，之后同样更新有序结构。

不过，**原子增加积分不等于同一个业务奖励只能领取一次**。

如果网络断开前 Redis 已经成功执行了 `ZINCRBY`，客户端没有收到响应就再次重试，积分可能再次增加。是否发奖、是否已经发过奖，仍需业务级去重或幂等控制。

如果排行榜只是展示缓存，它也可能与权威业务积分记录存在短暂差异；ZSet 本身不会自动保证数据库积分和排行榜同时一致。

这些限制不是 ZSet 设计缺陷，而是它提供的能力边界：**负责成员与分数的排序操作，不负责全部业务事务语义。**

## 12. 回看：同一种集合，为什么有两套底层实现？

现在回到最初的问题：Set 已经能保存唯一的报名用户，为什么还需要 ZSet？

因为“是否报名”和“按积分排名”是两个不同的问题。

```text
activity:1001:members
  Set：记录 user:42、user:57、user:63 是否已报名

activity:1001:rank
  ZSet：记录 user:42/90、user:57/95、user:63/90
        并能够按分数查询有序位置
```

本章的推导不是“Redis 刚好还有 ZSet，所以顺便介绍它”，而是：

```text
报名名单已经可以去重
           |
           | 新需求：按积分查询 Top N 和个人排名
           v
仅有 Set / Hash 需要应用端额外排序
           |
           v
新增一个有分数的唯一成员集合：ZSet
           |
           | 成员很少，额外索引反而占空间
           v
         listpack
           |
           | 成员增长，查找和移动成本上升
           v
       dict + skiplist
           |             |
           |             +-- 维持 score 顺序
           +---------------- 按 member 定位
           |
           | 查找个人名次需要跨越多个节点
           v
       跳表 span 累计排名
```

对外仍是同一组命令，对内则按规模选择不同的编码。

在小 ZSet 中，`zzlFind()` 与 `zzlInsert()` 负责定位和维护有序记录；在大 ZSet 中，`dictFind()`、`zslInsert()`、`zslUpdateScore()` 共同维护双索引；`zsetRank()` 则按编码分别计算名次。

我们也因此理解了一个更一般的 Redis 设计思路：**对外保持稳定的数据结构语义，对内根据数据规模选择不同的存储组织方式。**

## 13. 下一章：排行可以按分数查询，但怎样通知其他服务？

目前，活动系统已经有几种职责不同的数据：

- Hash 保存活动属性。
- List 保存按队列顺序处理的报名通知任务。
- Set 保存不重复的报名用户。
- ZSet 保存动态积分排行榜。

现在提出一个新情况：用户 `57` 积分超过了 `90`，排行榜已经更新，但活动大屏、通知服务等程序希望**尽快知道“用户积分发生变化”这件事**，而不是不断地重新查询排行榜。

一个直觉办法是让这些程序轮询 `ZSCORE` 或 Top N。但每个程序都持续发出查询，即使积分很久没有变化，也会产生额外请求。

我们真正缺少的似乎是一个不同于“查询当前状态”的能力：**当某件事情发生时，主动通知正在关注它的程序。**

这时，我们才有必要进入 Redis 的消息通信专题。

下一章先只解决最简单的“向当前在线的多个订阅者发送通知”，研究 `PUBLISH` / `SUBSCRIBE` 和 Redis 7.2.0 的订阅关系实现。等读者亲眼看到离线订阅者会错过通知之后，再考虑为什么需要保存可重读的事件历史。

**下一章：《Redis 如何把活动事件通知给多个服务？从 Pub/Sub 的最小广播开始》**

---

## 源码与文档参考

- [Redis 7.2.0 源码](https://github.com/redis/redis/tree/7.2.0)
- [`src/t_zset.c`：`zaddGenericCommand()`、`zsetAdd()`、`zzlInsert()`、`zsetConvertAndExpand()`、`zsetRank()`](https://github.com/redis/redis/blob/7.2.0/src/t_zset.c)
- [`src/object.c`：`createZsetListpackObject()`、`createZsetObject()`](https://github.com/redis/redis/blob/7.2.0/src/object.c)
- [`src/server.h`：`zset`、`zskiplistNode` 与 `span`](https://github.com/redis/redis/blob/7.2.0/src/server.h)
- [Redis ZADD 官方文档](https://redis.io/docs/latest/commands/zadd/)
- [Redis ZRANGE 官方文档](https://redis.io/docs/latest/commands/zrange/)
- [Redis Sorted Sets 数据类型](https://redis.io/docs/latest/develop/data-types/sorted-sets/)
