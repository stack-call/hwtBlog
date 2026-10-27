---
title: "第十二章：为什么 List 不适合做报名去重？从顺序存储到 Set 集合"
sidebar_label: "12 - Set 与报名去重"
sidebar_position: 13
description: "沿用活动报名案例，从 List 的重复任务与查询困境出发，理解 Redis Set 的唯一性语义，追踪 Redis 7.2.0 的 SADD、SISMEMBER 及 intset、listpack、hashtable 三种编码。"
tags:
  - Redis
  - 源码精读
  - Set
  - intset
  - listpack
  - hashtable
---

# 第十二章：为什么 List 不适合做报名去重？从顺序存储到 Set 集合

上一章，我们为活动 `1001` 建立了一个报名通知队列。用户报名后，应用将通知任务写入 Redis List，后台程序按照顺序取出任务。

```redis
LPUSH activity:1001:notify:queue signup:2001
LPUSH activity:1001:notify:queue signup:2002
RPOP activity:1001:notify:queue
```

List 回答的是：**有哪些等待处理的任务？它们应该按什么顺序取出？**

但是，活动报名接口又提出了一个要求：**同一个用户只能报名一次。**

这听起来和前面的问题很接近，实际上已经发生了变化。我们不再关心通知任务排在队列的第几个位置，而是要判断一个用户是否已经属于该活动的报名名单。

本章只解决这个问题。我们先保留 List，让它继续承担“通知任务”的职责，再找一种更合适的方式保存“已经报名的用户”。

> **源码基线**：Redis 7.2.0，主要阅读 [`src/t_set.c`](https://github.com/redis/redis/blob/7.2.0/src/t_set.c)、[`src/intset.c`](https://github.com/redis/redis/blob/7.2.0/src/intset.c)。本章代码明确标注“真实源码节选 / 教学化简 / 概念伪代码”。实验结果为根据源码推导的**预期输出**，不冒充实测。

## 1. 为什么沿用 List 会出现问题？

假设活动 `1001` 收到了以下报名请求：

```text
10:00:01  user:42 报名
10:00:02  user:57 报名
10:00:03  user:42 再次点击报名
```

如果系统只是把请求写入 List：

```redis
DEL activity:1001:notify:queue
LPUSH activity:1001:notify:queue notify:user:42
LPUSH activity:1001:notify:queue notify:user:57
LPUSH activity:1001:notify:queue notify:user:42
LRANGE activity:1001:notify:queue 0 -1
```

最后一条命令的预期结果是：

```text
1) "notify:user:42"
2) "notify:user:57"
3) "notify:user:42"
```

这不是 Redis 的错误。对于 List 来说，**两次插入相同元素也是两次有效插入**。如果我们把这些元素理解为报名通知任务，那么两次 `notify:user:42` 就代表两个待处理任务。

有人可能会提出：写入 List 之前，先用 `LRANGE` 查一遍。如果列表里已经有 `notify:user:42`，就不再插入。

这个方案有两处明显的不足。

第一，List 是按位置组织的。为了查找一个成员，可能需要扫描很多元素；报名名单越大，这样做越不合适。

第二，也是更根本的：**通知任务一旦被 `RPOP` 取出，就从队列里消失了。** 即使 List 当前没有 `notify:user:42`，也不能证明他从未报名。我们不应该用一个会不断被消费的任务队列保存长期的报名事实。

因此，这里需要的不是“换一种方式操作 List”，而是增加一份职责不同的数据：

```text
报名名单：哪些用户已经报名？
通知队列：哪些通知尚未处理？
```

现在，先只研究报名名单。

## 2. 一个最小的集合应该具备什么能力？

先用 Python 实现它，暂时不涉及 Redis：

```python
registered = set()

print("user:42" in registered)  # False
registered.add("user:42")
registered.add("user:57")
registered.add("user:42")

print(len(registered))          # 2
print("user:42" in registered)  # True
```

它有两个恰好符合需求的性质：

1. 相同成员只保存一次。
2. 可以直接判断某个成员是否存在，而不需要关心它在第几个位置。

与之对应的代价是：我们不再要求记录报名顺序。

这个抽象就是 **Set（集合）**。Redis 也提供 Set 数据类型，其成员具有唯一性，并且不保证返回顺序。

对于活动 `1001`，可以使用一个单独的 Redis Key：

```text
activity:1001:members
```

其中的成员是用户 ID：

```text
{ "user:42", "user:57" }
```

此时，List 和 Set 没有谁取代谁的问题，它们保存的是不同的状态：

```text
活动 1001
   |
   +-- Set  activity:1001:members
   |       “谁已经报名？”
   |
   +-- List activity:1001:notify:queue
           “哪些通知等待处理？”
```

只要业务需求发生了这样的变化，引入一个新数据结构就有了充分理由。

## 3. 第一个实验：重复添加为什么返回 0？

启动一个用于实验的 Redis 7.2.0 实例：

```bash
# 如果已有同名容器，请使用现有实验实例或先自行清理旧容器。
docker run -d \
  --name redis-source-lab \
  -p 127.0.0.1:6379:6379 \
  redis:7.2.0 \
  --save "" \
  --appendonly no
```

为了使实验不受上一轮操作影响，先删除对应 Key：

```bash
docker exec redis-source-lab redis-cli DEL activity:1001:members
```

依次执行：

```bash
docker exec redis-source-lab redis-cli SADD activity:1001:members user:42
docker exec redis-source-lab redis-cli SADD activity:1001:members user:57
docker exec redis-source-lab redis-cli SADD activity:1001:members user:42
```

三条命令的预期结果依次为：

```text
(integer) 1
(integer) 1
(integer) 0
```

为什么第三次返回 `0`？

因为 `SADD` 返回的是**本次实际新增的成员数量**，而不是执行成功或失败的布尔值。

- 第一次：原集合没有 `user:42`，新增一个成员。
- 第二次：新增 `user:57`。
- 第三次：`user:42` 已经存在，集合没有发生变化。

再检查报名人数：

```bash
docker exec redis-source-lab redis-cli SCARD activity:1001:members
```

预期：

```text
(integer) 2
```

最后检查某个人是否报名：

```bash
docker exec redis-source-lab redis-cli SISMEMBER activity:1001:members user:42
docker exec redis-source-lab redis-cli SISMEMBER activity:1001:members user:99
```

预期分别返回 `1` 和 `0`。

到这里，我们已经拿到最重要的外部语义：**同一个用户重复提交报名，集合中仍然只有一个对应成员。**

但为什么 Redis 能区分“新增”和“重复”呢？接下来进入源码。

## 4. SADD 怎么找到自己的处理函数？

前面的章节已经走通过一条 Redis 命令的入口：

```text
客户端发送请求
    ↓
networking.c：读入与解析
    ↓
server.c：processCommand() / call()
    ↓
具体命令处理函数
```

因此，这一章不再重新解释 RESP 或事件循环，而是从命令处理函数直接开始。

Redis 7.2.0 的 `SADD` 命令入口位于：

- 文件：[`src/t_set.c`](https://github.com/redis/redis/blob/7.2.0/src/t_set.c#L571-L593)
- 函数：`saddCommand(client *c)`

假设客户端发送：

```redis
SADD activity:1001:members user:42
```

在调用 `saddCommand()` 时，参数可以这样理解：

```text
c->argc    = 3
c->argv[0] = SADD
c->argv[1] = activity:1001:members
c->argv[2] = user:42
```

`argv` 中的元素实际是 Redis 对象指针；这里用文本表示只是为了让参数流向清晰。

`saddCommand()` 首先查找 Key，然后检查它是不是一个 Set。如果 Key 不存在，它会创建一个新的 Set 对象并加入当前 Redis 数据库。如果已经存在，则在原有对象中增加成员。

**教学化简（根据 Redis 7.2.0 的 `saddCommand()` 重写，不是可直接编译的源码）：**

```c
set = lookupKeyWrite(database, key);

if (set == NULL) {
    set = create_a_set(first_member, member_count);
    add_key_to_database(key, set);
}

added = 0;
for (each member in request) {
    if (add_member_if_absent(set, member)) {
        added++;
    }
}
reply_integer(added);
```

这里最值得注意的不是“创建 Set 对象”，而是 `added` 变量。

`added` 并不等于客户端传来了几个参数，而是有几个成员**真正第一次进入集合**。

对应的真实调用点是：

**真实源码节选 — `src/t_set.c`：**

```c
if (setTypeAdd(set,c->argv[j]->ptr)) added++;
```

`setTypeAdd()` 返回新增成功与否，`saddCommand()` 据此累计返回值。实际代码还负责数据库修改通知与脏数据计数，但它们不影响我们当前的唯一性问题。

这就解释了实验里第一次返回 `1`、第三次返回 `0` 的原因。

可是，`setTypeAdd()` 怎么知道元素已经存在？这时才有必要研究 Redis Set 的内部表示。

## 5. 集合为什么不能简单地一直用一个数组？

从刚才的 Python `set` 例子看来，Set 只有一个看似简单的要求：新成员不能重复。

我们先试着用普通数组实现：

```python
# 概念演示：用数组表示一个集合
members = []

def add_member(member):
    if member in members:
        return 0
    members.append(member)
    return 1
```

对几个成员来说，它没有问题。

但是，`member in members` 需要按元素逐个查找。若有十万名报名用户，每次添加前都线性扫描整个数组，就会产生明显成本。

一个自然的思路是使用哈希表：让成员本身充当索引，判断它是否存在时不再需要顺序扫描整个列表。

但哈希表为了维持快速查找，通常需要额外的桶、指针和管理开销。对于只有两三个成员的小集合，这些开销可能并不划算。

因此，Redis 面临一个新的取舍：

> **小集合能否优先紧凑存储，大集合再使用更适合成员查找的哈希表？**

Redis 7.2.0 的答案不是“Set 底层永远只有一种结构”，而是按照数据特征选择编码。

## 6. Redis 7.2.0 的 Set 有三种底层编码

这里必须固定版本。Redis 7.2.0 的 Set 可以使用三种编码：

| 底层编码 | 适合的数据形态 | 当前关心的特点 |
|---|---|---|
| `intset` | 少量可以精确表示为整数的成员 | 紧凑地保存有序整数，检查成员时可使用搜索 |
| `listpack` | 少量且较短的字符串成员 | 紧凑存储，加入成员时需要检查是否重复 |
| `hashtable` | 成员较多、较长或不适合紧凑编码的集合 | 使用哈希表进行成员查找与插入 |

这三种是**内部编码**，不是三个对外不同的 Redis 数据类型。

无论内部使用哪一种，客户端看到的类型仍然是：

```redis
TYPE activity:1001:members
```

```text
set
```

一个非常重要的区别是：**底层 `intset` 按整数大小保存数据，不代表 Redis Set 对外承诺排序；底层 `listpack` 按某种内存顺序排列元素，也不代表 `SMEMBERS` 承诺插入顺序。**

我们现在先看 Redis 怎么选择编码。

### 6.1 新建集合时如何选？

在 [`src/t_set.c`](https://github.com/redis/redis/blob/7.2.0/src/t_set.c#L37-L53) 中，`setTypeCreate(sds value, size_t size_hint)` 根据第一个成员及预计成员数量选择创建方式。

**教学化简：**

```text
如果首个成员可表示为整数，而且预计成员数不超过 intset 限制：
    创建 intset
否则，如果预计成员数不超过 listpack 限制：
    创建 listpack
否则：
    创建 hashtable
```

这两个阈值可以在 Redis 配置中看到。Redis 7.2.0 的示例默认值为：

```conf
set-max-intset-entries 512
set-max-listpack-entries 128
set-max-listpack-value 64
```

其中，`set-max-listpack-value` 限制适合紧凑编码的单个成员长度。注意：创建函数在做初步选择后，实际插入时仍会检查成员长度等条件，必要时转换成哈希表。

这也解释了为什么我们之前写入 `user:42`，它不会按整数集合处理：它包含 `user:` 前缀，并不是一个可表示为整数的成员。

对于少量这样的字符串，在默认配置下会使用 `listpack`。

但如果我们直接保存纯数字用户 ID：

```redis
SADD activity:1001:members:numeric 42
```

就更有可能看到 `intset` 编码。下一节用命令验证。

### 6.2 intset 为什么能够避免重复？

`intset` 的实现位于：[`src/intset.c`](https://github.com/redis/redis/blob/7.2.0/src/intset.c)。

它保存的是一个排序的整数序列。插入新整数之前，`intsetAdd()` 会调用 `intsetSearch()` 寻找这个值。

如果已经存在，则保持原集合不变，并把“没有新增”的结果返回调用者；如果不存在，才为新成员留出位置并写入。

对当前实验：

```text
初始成员：{ 42, 57 }
再次 SADD 42
    ↓
intsetSearch() 找到 42
    ↓
不插入；新增数为 0
```

这里第一次引入 `intsetSearch()` 就够了；至于它如何进行二分查找、整数从 16 位升级到 32/64 位，我们可以在真正需要研究紧凑整数集合的章节再深入。

### 6.3 listpack 为什么也能避免重复？

我们的主案例使用 `user:42` 这样的字符串。

对于 `listpack` 编码，`setTypeAddAux()` 在插入前，会先使用 `lpFind()` 检查成员是否已经存在。

**教学化简：**

```text
寻找字符串成员
    |
    +-- 找到了：不追加，返回 0
    |
    +-- 没找到：
            |
            +-- 仍满足紧凑编码限制：追加，返回 1
            |
            +-- 超出限制：转换为 hashtable，再添加
```

这再次解释了 `SADD` 的语义。

**唯一性并不是“集合里看起来没有重复”这样一个偶然结果，而是插入路径主动检查成员是否已经存在。**

但这里也体现了一个取舍：小集合采用紧凑存储节省空间，而成员查找需要扫描相应的存储内容。它不保证始终像哈希表那样具有平均常数时间的查找特点。

当这个取舍不再合适时，就需要转换编码。

## 7. 成员变多时，Redis 为什么转换为 hashtable？

假设活动开始时只有三名报名用户。

用 listpack 保存，额外的结构开销较少。

但随着报名人数增加，集合可能增长到数万名用户。如果每次插入之前都需要在紧凑结构中线性查找，操作成本就会不断增加。

这时 Redis 不再坚持一直使用紧凑编码，而是将数据转换为哈希表。

在 [`setTypeAddAux()`](https://github.com/redis/redis/blob/7.2.0/src/t_set.c#L115-L217) 的 listpack 分支中，Redis 会检查元素数量、长度及存储安全限制。

当不再满足相应条件时，调用 `setTypeConvertAndExpand()` 转换成 `OBJ_ENCODING_HT`，再添加新成员。

在哈希表分支，Redis 会寻找成员可以插入的位置：已存在则不插入，不存在才写入新成员。

这意味着上层的 `SADD` 无论面对哪种编码，都不需要重新设计一套接口：

```text
                 saddCommand()
                       |
                       v
                  setTypeAdd()
                       |
                       v
                 setTypeAddAux()
                       |
           +-----------+-----------+
           |           |           |
           v           v           v
         intset      listpack    hashtable
           |           |           |
           +-----------+-----------+
                       |
                       v
              返回 1（新增）或 0（重复）
```

这不是为了“把数据结构讲全”而增加的三个名词，而是为了回答刚才的问题：**为什么小集合可以节省空间，而集合增长后，Redis 又能切换到另一种更适合成员查找的实现？**

### 7.1 转换到底做了什么？

进入 [`setTypeConvertAndExpand()`](https://github.com/redis/redis/blob/7.2.0/src/t_set.c#L476-L524)。

转换到哈希表时，可以概括成：

**教学化简：**

```text
创建目标哈希表
    ↓
为成员数量预留空间
    ↓
遍历原集合中的成员
    ↓
逐个加入新哈希表
    ↓
释放原来的底层存储
    ↓
把同一个 Set 对象的 encoding 改成 hashtable
```

注意，转换的是一个 Redis Set 对象的**内部表示方式**，不是把这个 Key 改成 Redis Hash 数据类型。

因此，转换之后：

```redis
TYPE activity:1001:members
```

仍然返回 `set`。

而：

```redis
OBJECT ENCODING activity:1001:members
```

则可能从 `listpack` 变成 `hashtable`。

## 8. 实验：亲眼观察三种编码与转换

这次不需要往 Redis 填充成千上万条数据。我们使用隔离的本地实验实例，临时调低编码阈值来观察变化。

> 下面的 `CONFIG SET` 会修改**当前 Redis 实例**的配置；只在专用实验环境执行，不要在生产实例直接调整。

先看默认配置：

```bash
docker exec redis-source-lab redis-cli CONFIG GET set-max-intset-entries
docker exec redis-source-lab redis-cli CONFIG GET set-max-listpack-entries
docker exec redis-source-lab redis-cli CONFIG GET set-max-listpack-value
```

再清理测试 Key：

```bash
docker exec redis-source-lab redis-cli DEL lab:set:integer lab:set:string
```

### 8.1 数字成员：从 intset 开始

```bash
docker exec redis-source-lab redis-cli SADD lab:set:integer 42 57
docker exec redis-source-lab redis-cli OBJECT ENCODING lab:set:integer
```

在 Redis 7.2.0 默认配置下，编码预期为：

```text
"intset"
```

再次添加相同整数：

```bash
docker exec redis-source-lab redis-cli SADD lab:set:integer 42
```

预期返回 `(integer) 0`。

### 8.2 少量字符串成员：使用 listpack

```bash
docker exec redis-source-lab redis-cli SADD lab:set:string user:42 user:57
docker exec redis-source-lab redis-cli OBJECT ENCODING lab:set:string
```

在默认配置下，编码预期为：

```text
"listpack"
```

Set 的唯一性规则并没有因为底层编码不同而变化。

### 8.3 超过阈值：转换成 hashtable

为便于观察，先把 listpack 允许的成员数量限制改成 2：

```bash
docker exec redis-source-lab redis-cli CONFIG SET set-max-listpack-entries 2
```

然后给刚才已经有两个成员的 Set 增加第三个成员：

```bash
docker exec redis-source-lab redis-cli SADD lab:set:string user:99
docker exec redis-source-lab redis-cli OBJECT ENCODING lab:set:string
```

预期编码变成：

```text
"hashtable"
```

接着检查：

```bash
docker exec redis-source-lab redis-cli SCARD lab:set:string
docker exec redis-source-lab redis-cli SISMEMBER lab:set:string user:42
```

预期结果分别为 `3` 和 `1`。转换编码没有使已有成员丢失。

### 8.4 intset 超出数量限制

同样地，可以临时调低整数集合阈值：

```bash
docker exec redis-source-lab redis-cli CONFIG SET set-max-intset-entries 2
docker exec redis-source-lab redis-cli SADD lab:set:integer 99
docker exec redis-source-lab redis-cli OBJECT ENCODING lab:set:integer
```

在前面已有 `42`、`57` 的前提下，添加第三个整数后预期为：

```text
"hashtable"
```

实验后将配置恢复到本章使用的 Redis 7.2.0 默认值：

```bash
docker exec redis-source-lab redis-cli CONFIG SET set-max-listpack-entries 128
docker exec redis-source-lab redis-cli CONFIG SET set-max-intset-entries 512
```

**实验边界**：这里观察的是不同编码及其转换的预期行为；没有实际在本文编写环境中运行 Redis 服务。配置恢复不会自动把已转换为 hashtable 的集合变回紧凑编码，因此不要期望最后一次 `OBJECT ENCODING` 又恢复成原来的结果。

## 9. SISMEMBER 为什么不用遍历客户端拿到的全部成员？

现在我们可以判断 `user:42` 是否报名：

```redis
SISMEMBER activity:1001:members user:42
```

与 `SMEMBERS` 不同，它不要求 Redis 将全部成员返回给客户端，再由客户端自行查找。

`SISMEMBER` 在 Redis 服务端内部直接完成成员检查。

对应源码位置：[`src/t_set.c` 中的 `sismemberCommand()`](https://github.com/redis/redis/blob/7.2.0/src/t_set.c#L672-L681)。

它先找到 Set 对象，再通过 `setTypeIsMember()` 判断成员是否存在，最后返回整数 `1` 或 `0`。

真正的成员检查由 `setTypeIsMemberAux()` 根据底层编码处理：

```text
intset    → intsetFind()
listpack  → lpFind()
hashtable → dictFind()
```

也就是说，对客户端而言，只有一个命令：

```redis
SISMEMBER activity:1001:members user:42
```

对 Redis 内部而言，它会根据当前数据如何存储选择不同的查找操作。

这又是一次“对外行为不变、内部实现可以变化”的设计。

不过，不应该把所有编码下的 `SISMEMBER` 都理解成严格 `O(1)`：小集合的紧凑编码可能需要搜索或扫描；哈希表则通常提供平均常数时间的查找。

## 10. 两个客户端同时报名，会不会都成功？

上一章主线已经研究过 Redis 单条命令的原子性。

现在正好将它用在这个场景里。

假设 `user:42` 尚未报名，两个客户端几乎同时执行：

```redis
SADD activity:1001:members user:42
```

在当前单实例、正常执行路径下，这两条命令会依次完成。

假设 A 先执行：

```text
客户端 A：SADD user:42 → 返回 1
客户端 B：SADD user:42 → 返回 0
集合中只有一个 user:42
```

如果 B 先执行，返回值归属对调，但集合里依然只存在一个成员。

这不是因为两个客户端事先协调好了谁能报名，而是 `SADD` 在**一条 Redis 命令内**完成“检查成员是否存在”和“按需要插入”。

我们可以用一个很小的 Python 实验来验证返回值。

安装依赖：

```bash
python -m pip install "redis==5.0.1"
```

创建 `set_signup_race.py`：

```python
import threading
import redis

KEY = "lab:signup:members"
client = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)
client.delete(KEY)

barrier = threading.Barrier(2)
results = []
lock = threading.Lock()


def register():
    local = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)
    barrier.wait()
    added = local.sadd(KEY, "user:42")
    with lock:
        results.append(added)


threads = [threading.Thread(target=register) for _ in range(2)]
for t in threads:
    t.start()
for t in threads:
    t.join()

print("SADD 返回值:", sorted(results))
print("报名总数:", client.scard(KEY))

assert sorted(results) == [0, 1]
assert client.scard(KEY) == 1
```

预期输出：

```text
SADD 返回值: [0, 1]
报名总数: 1
```

这说明 Set 能够让重复报名请求对**这一个 Redis 集合**只产生一次新增。

但我们还没有完成整个报名业务。

## 11. Set 可以防重复报名，却不自动保证通知只生成一次

回到活动系统，报名成功后还有一个动作：向 List 写入通知任务。

一个很自然的写法是：

```python
# 教学伪代码：两个 Redis 命令之间不是原子操作

if redis.sadd("activity:1001:members", "user:42") == 1:
    redis.rpush("activity:1001:notify:queue", "notify:user:42")
```

这已经比“先 `SISMEMBER` 再 `SADD`”更适合判断本次是否首次报名。

但想一想：如果 Redis 已经成功执行 `SADD`，应用进程却在 `RPUSH` 之前崩溃呢？

会出现：

```text
Set：user:42 已报名
List：没有 user:42 的通知任务
```

下次用户重新提交报名时，`SADD` 返回 `0`，于是也不会补发通知任务。

所以：

> **单条 SADD 原子，并不意味着 SADD 与 RPUSH 两条命令合起来也原子。**

如果业务要求“登记成功”和“产生待通知任务”必须作为一个整体执行，就需要额外的机制，例如在同一个 Redis 实例中使用合适的 Lua 脚本或事务组织操作。

但即使将两个 Redis 写操作绑定为一个原子执行单元，也不能自动保证外部短信必然发送成功；那涉及消息消费、失败重试与外部副作用的可靠性问题。

另外，Redis 的 Set 只是在 Redis 当前状态下维持成员唯一性。如果报名资格是不能丢失的权威业务数据，还需要考虑 Redis 的持久化、淘汰策略、故障恢复，以及是否应以业务数据库的唯一约束作为最终判定依据。前面关于 RDB、AOF 和内存淘汰的章节正是这些取舍的基础。

本章先停在这里：我们已经解决“集合成员不能重复”这一层，不为此提前展开跨命令或跨系统事务。

## 12. 再看一个边界：去重不是排序

当前报名名单可以回答：

```text
user:42 报名了吗？
报名总人数是多少？
```

但运营现在想做一件新事：按用户在活动中的积分排序，展示前十名。

例如：

```text
user:42 → 120 分
user:57 →  90 分
user:99 → 150 分
```

Set 的元素没有分数，也不对外保证顺序。即使使用 `SMEMBERS` 拿到全部成员，再到其他地方查询分数后排序，也需要额外传输和计算。

在这一刻，我们缺少的不再是“成员唯一性”，而是：

**同一个用户只出现一次，同时为每个用户关联一个分数，并能按分数查询。**

这会自然引出 Redis 的下一种数据类型：**Sorted Set（有序集合，ZSet）**。

我们将在下一章沿用活动 `1001`，从排行榜需求出发，先尝试 Set + 客户端排序，再研究 `ZADD`、`ZRANGE` 及其背后的有序索引实现。

不会因为 List、Set 和 ZSet 都属于“集合”就直接罗列命令；新数据结构必须由新的访问需求推动出现。

---

## 本章回顾

整章的演化只有一条线：

```text
List 保存按顺序处理的通知任务
                 |
                 | 重复点击报名 → 重复任务
                 v
    需要单独保存“已报名”事实
                 |
                 | 成员不能重复，也无需排列位置
                 v
                Set
                 |
                 | SADD 如何知道本次是否新增？
                 v
           saddCommand()
                 |
                 v
            setTypeAdd()
                 |
                 v
          setTypeAddAux()
                 |
       +---------+----------+
       |         |          |
     intset   listpack   hashtable
       |         |          |
       +---------+----------+
                 |
                 v
             新增 1 / 重复 0
                 |
                 | 需要检查成员是否存在
                 v
             SISMEMBER
                 |
                 | 需要按积分排序
                 v
          下一章：Sorted Set
```

读完本章，应该能够不靠死记硬背解释四个问题：

1. 为什么报名通知 List 不能作为完整的已报名名单？因为任务可能重复、被消费，而且它表达的是处理顺序而不是成员身份。
2. 为什么 `SADD` 可以去重？因为实际插入前，底层编码对应的路径会判断成员是否已存在。
3. 为什么 Set 需要不同编码？因为少量数据的内存紧凑性与大量数据的查找效率之间存在取舍。
4. 为什么 `SADD` 成功不等于整个报名工作流可靠完成？因为集合写入、队列写入及外部业务操作是不同的执行边界。

这一章并没有要求 Set 替代 List。相反，我们为它们划清了职责：**Set 管成员身份，List 管有序任务。**

**下一章：《排行榜为什么不能只用 Set？从成员唯一到 Sorted Set》**

## 源码与参考资料

固定基线：**Redis 7.2.0**。

- [`src/t_set.c` — `saddCommand()`](https://github.com/redis/redis/blob/7.2.0/src/t_set.c#L571-L593)：查找或创建 Set，统计本次实际新增的成员数。
- [`src/t_set.c` — `setTypeCreate()`](https://github.com/redis/redis/blob/7.2.0/src/t_set.c#L37-L53)：依据值与数量提示选择初始编码。
- [`src/t_set.c` — `setTypeAddAux()`](https://github.com/redis/redis/blob/7.2.0/src/t_set.c#L115-L217)：按 intset、listpack、hashtable 编码分支完成检查与插入。
- [`src/t_set.c` — `sismemberCommand()` 和 `setTypeIsMemberAux()`](https://github.com/redis/redis/blob/7.2.0/src/t_set.c#L270-L307)：根据实际编码判断成员是否存在。
- [`src/t_set.c` — `setTypeConvertAndExpand()`](https://github.com/redis/redis/blob/7.2.0/src/t_set.c#L476-L524)：转换底层编码并迁移成员。
- [`src/intset.c` — `intsetAdd()`、`intsetFind()`](https://github.com/redis/redis/blob/7.2.0/src/intset.c)：整数集合的插入、查询与去重。
- [`redis.conf` — Set 编码阈值](https://github.com/redis/redis/blob/7.2.0/redis.conf#L1820-L1831)。
- [Redis 官方：Set 数据类型](https://redis.io/docs/latest/develop/data-types/sets/)。
- [Redis 官方：OBJECT ENCODING](https://redis.io/docs/latest/commands/object-encoding/)。
