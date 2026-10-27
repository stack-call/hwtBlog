---
title: "第十章：一个活动有多个属性，Redis 为什么需要 Hash？"
sidebar_label: "10 - 从 String 到 Hash"
sidebar_position: 11
description: "延续活动访问计数案例，从多个相关属性的存储问题出发，理解 Redis Hash 的字段操作，并沿 Redis 7.2.0 源码追踪 HSET、HGET、listpack 与 hashtable 的转换。"
tags:
  - Redis
  - 源码精读
  - Hash
  - listpack
  - 数据结构
---

# 第十章：一个活动有多个属性，Redis 为什么需要 Hash？

前面几章，我们把 Redis 当作一个正在不断完善的内存数据服务：它能接收客户端命令、管理大量连接、执行原子递增，还能管理过期、淘汰、持久化以及复制和集群。

现在，我们暂时不再增加新的服务端基础设施能力，而是回到一直使用的那个活动业务。

最开始，我们只保存活动 `1001` 的访问次数：

```redis
SET activity:1001:views 100
```

这个值非常简单，String 完全够用。用户访问一次，我们就执行：

```redis
INCR activity:1001:views
```

但是，活动不可能永远只有一个访问次数。

现在产品又要求显示活动标题，并在活动结束时修改活动状态。原来只有一个值的数据，变成了一个拥有多个属性的对象：

```text
活动 1001
  title  = Autumn Sale
  status = active
  views  = 100
```

**接下来要解决的不是“Redis 有哪些数据类型”，而是一个更具体的问题：这三个属于同一个活动的属性，应该怎样保存和更新？**

我们先不急着使用 Hash，看看现有的 String 能做到什么。

## 1. 方案一：继续使用 String，一项属性一个 Key

最直接的方法，是给每个属性分配独立的 Key：

```redis
SET activity:1001:title "Autumn Sale"
SET activity:1001:status active
SET activity:1001:views 100
```

这种设计并没有错误。它甚至有一个明显优点：上一章已经学过的 `INCR` 可以直接用于 `activity:1001:views`。

查询标题与状态也很自然：

```redis
GET activity:1001:title
GET activity:1001:status
```

假如我们只关心某一个属性，而且每个属性本来就拥有不同的生命周期，分开保存完全可能是更合适的方案。

但现在增加一个具体需求：**整个活动结束后，希望一起清理活动的所有临时信息。**

如果还是三个独立的 Key，我们需要知道所有 Key 的名称：

```redis
DEL activity:1001:title activity:1001:status activity:1001:views
```

这对三个固定字段不难。然而，如果后来又增加 `owner`、`category`、`region` 等属性，删除逻辑也要跟着维护。更重要的是，当我们想为“整个活动”设置一个统一的过期时间时，三个独立 Key 需要分别设置 TTL；漏掉一个，就会留下与活动相关的残余数据。

所以问题不是 String 不能存这些数据，而是**这些数据在业务上属于一个整体，但在 Redis 中却散落成多个彼此独立的顶层 Key**。

我们能否保留“按属性修改”的便利性，同时让同一个活动的数据在 Redis 中拥有一个共同的归属？

## 2. 方案二：把整个活动编码成一个 String

我们可能会想到 JSON：

```redis
SET activity:1001 '{"title":"Autumn Sale","status":"active","views":100}'
```

这次 Redis 里只有一个顶层 Key：`activity:1001`。删除活动时，只需要删除这一个 Key；给整个活动设置 TTL 也方便了。

但现在，我们希望把访问次数增加一。

对于普通 JSON 字符串，Redis 的核心 `INCR` 并不能直接修改其中的 `views` 字段。客户端不得不先获取整段 JSON，修改对象后再写回：

```python
# 教学示例：假设 client 是 redis-py 客户端
import json

key = "activity:1001"
activity = json.loads(client.get(key))
activity["views"] += 1
client.set(key, json.dumps(activity))
```

这重新引出了第三章已经遇到的风险：两个客户端读取同一个旧对象，分别修改并写回，可能覆盖彼此的更新。即使它们修改的是不同字段，也可能因为写回整段 JSON 而发生更新丢失。

请注意，这并不是说 JSON 无法用于 Redis；Redis 的相关扩展能力也有自己的适用场景。我们在这里讨论的，仅仅是 **Redis 7.2.0 核心 String 命令对一个普通 JSON 字符串的处理能力**。

于是，我们得到一个明确的新需求：

> **把一个对象作为一个顶层 Key 保存，同时允许服务端直接读写其中某个字段，而不必由客户端重写整个对象。**

这时才需要引入 Redis 的 **Hash**。

## 3. Hash：一个 Key，里面保存多组 field-value

Hash 可以把同一个活动的多个属性放在一个顶层 Key 下：

```text
Redis 数据库
  |
  +-- Key: activity:1001
          |
          +-- field: title  -> Autumn Sale
          +-- field: status -> active
          +-- field: views  -> 100
```

这里有两个层次，不能混淆：

- `activity:1001` 是 Redis 数据库里的 **顶层 Key**。
- `title`、`status`、`views` 是这个 Hash 内部的 **field**，不是三个独立的 Redis Key。

现在可以用一条命令创建活动对象：

```redis
HSET activity:1001 title "Autumn Sale" status active views 100
```

其中，`HSET` 表示设置 Hash 的字段。它可以一次设置多个 field-value 对。

只查询标题时：

```redis
HGET activity:1001 title
```

只修改状态时：

```redis
HSET activity:1001 status ended
```

访问次数仍然需要原子递增。对应的命令是：

```redis
HINCRBY activity:1001 views 1
```

这并不是把 JSON 字符串取出来再解析，而是让 Redis 在一个 Hash 的字段层面执行整数递增。

整组数据不再需要三个顶层 Key，我们也可以把过期时间设在活动对象上：

```redis
EXPIRE activity:1001 3600
```

**在本系列固定的 Redis 7.2.0 中，这个过期时间属于整个 `activity:1001` Key，而不是只属于某一个 field。** Hash 字段级独立过期是 Redis 7.4 之后才引入的能力，不能把更新版本的行为误写进 7.2.0 源码教程。

现在，最初的问题已经得到解决。我们先通过实验确认行为，再进入真实源码。

## 4. 最小实验：字段新增、更新和递增

如果前面章节的实验容器仍在运行，可以复用；否则启动 Redis 7.2.0：

```bash
docker run -d \
  --name redis-source-lab \
  -p 127.0.0.1:6379:6379 \
  redis:7.2.0 \
  --save "" \
  --appendonly no
```

本地演示关闭持久化，避免与此前保存的数据混淆；此配置不适合要求持久化的生产环境。

进入 Redis CLI：

```bash
docker exec -it redis-source-lab redis-cli
```

先清理当前活动：

```redis
DEL activity:1001
```

创建三个字段：

```redis
HSET activity:1001 title "Autumn Sale" status active views 100
```

预期返回：

```text
(integer) 3
```

这里的 `3` 表示**本次新增加了三个 field**，不是活动对象的版本号，也不是执行成功次数。

只读取标题：

```redis
HGET activity:1001 title
```

预期返回：

```text
"Autumn Sale"
```

现在把状态从 `active` 改成 `ended`：

```redis
HSET activity:1001 status ended
```

预期返回：

```text
(integer) 0
```

为什么返回 `0`？因为字段 `status` 原本已经存在，本次只是覆盖原值，没有新增 field。

再给访问次数增加一：

```redis
HINCRBY activity:1001 views 1
```

预期返回：

```text
(integer) 101
```

检查字段数量：

```redis
HLEN activity:1001
```

预期返回 `3`。说明我们更新了状态并递增了计数，但仍然只有三个字段。

现在给整个活动设置 TTL：

```redis
EXPIRE activity:1001 3600
TTL activity:1001
```

`EXPIRE` 预期返回 `1`；`TTL` 会返回接近 3600、随后递减的秒数，而不是一个永远固定的数字。

**我们已经验证的目标应该是：一个 Redis Key 内可以独立操作不同字段，而对顶层 Key 的删除或过期会作用于整个对象。**

接下来，源码需要回答的是：Redis 如何把这些字段存进去？

## 5. 一条 HSET 命令怎样找到自己的数据？

前面的命令执行章节已经解释过通用路径：

```text
网络请求 -> RESP 解析 -> processCommand() -> call() -> 命令函数
```

因此，这次不必从网络读取重新讲一遍。我们直接进入 `HSET` 的处理函数。

**源码基线：Redis 7.2.0**

- 仓库：[`redis/redis`，标签 `7.2.0`](https://github.com/redis/redis/tree/7.2.0)
- 文件：[`src/t_hash.c`](https://github.com/redis/redis/blob/7.2.0/src/t_hash.c)
- 入口：`hsetCommand(client *c)`

对于刚才的命令：

```redis
HSET activity:1001 title "Autumn Sale" status active views 100
```

我们暂时只需要这样理解参数：

```text
argv[0] = HSET
argv[1] = activity:1001
argv[2] = title
argv[3] = Autumn Sale
argv[4] = status
argv[5] = active
argv[6] = views
argv[7] = 100
```

这是**教学化的字符串展示**；实际的 `argv` 元素是 Redis 对象指针，不是普通 C 字符串。

在 `hsetCommand()` 中，首先会检查字段和值是否成对出现，接着调用 `hashTypeLookupWriteOrCreate()`。

为什么需要这个函数？因为和普通 `SET` 一样，Redis 必须先确认当前顶层 Key 是什么状态：

- Key 不存在：创建一个 Hash。
- Key 已存在而且是 Hash：向它写入字段。
- Key 已存在，但类型不是 Hash：返回 WRONGTYPE 错误，而不是把原有数据悄悄改成 Hash。

可以在 `hashTypeLookupWriteOrCreate()` 找到下面两条语句（**真实源码节选，位于 Key 不存在的分支**）：

```c
o = createHashObject();
dbAdd(c->db,key,o);
```

第一行创建 Hash 对象，第二行将它放入数据库的顶层键值字典。

这里的分层很关键：`dbAdd()` 只负责把 `activity:1001` 这个**顶层 Key**关联到一个 Hash 对象；`title` 等字段并不因此变成顶层 Key。

于是，我们的模型已经长成：

```text
redisDb->dict
    |
    +-- "activity:1001" --> Hash 对象
                               |
                               +-- 尚待写入的字段集合
```

现在对象已经存在，但三个字段还没写进去。它们在哪里被处理？

## 6. HSET 为什么没有直接操作 C 语言的字典？

继续看 `hsetCommand()`，它会先尝试判断对象是否需要转换编码，然后逐对写入字段。

**真实源码节选：**

```c
hashTypeTryConversion(o,c->argv,2,c->argc-1);
```

而真正逐对执行写入的是 `hashTypeSet()`。我们把该函数调用循环简化为下列结构：

```c
// 教学化简：保留遍历 field/value 和统计新增字段的语义
for (int i = 2; i < argc; i += 2) {
    int updated = hashTypeSet(hash, field[i], value[i], HASH_SET_COPY);
    if (!updated) created++;
}
```

注意：上述代码是解释执行关系的**教学化简**，不是可直接替换 Redis 源码的 C 代码。

`hashTypeSet()` 的返回值有一个有用的约定：

- 返回 `0`：此前不存在这个 field，本次是新增。
- 返回 `1`：此前已经存在这个 field，本次是更新。

因此，`hsetCommand()` 统计新增字段数量并将它作为 `HSET` 的返回值。这正好解释了实验中“新建三个字段返回 3，更新 status 返回 0”的现象。

但我们还没解释为什么要先调用 `hashTypeTryConversion()`，以及 `hashTypeSet()` 为什么没有直接固定使用哈希表。

**因为 Redis 的 Hash 是一种对外的数据类型，却不必始终对应同一种内部内存布局。**

对于很小的对象，如果每个字段都使用独立哈希表条目，指针和分配器元数据也会占用不少内存。Redis 选择对小 Hash 使用一种更紧凑的表示。

这时才有必要认识 `listpack`。

## 7. 小 Hash 为什么默认使用 listpack？

进入另一个真实源码位置：

- 文件：[`src/object.c`](https://github.com/redis/redis/blob/7.2.0/src/object.c)
- 函数：`createHashObject()`

它创建的对象对外类型是 `OBJ_HASH`，但默认的底层编码是 `OBJ_ENCODING_LISTPACK`。

可以用一个简化示意图理解 listpack：

```text
Redis Hash：activity:1001
         |
         v
   OBJ_ENCODING_LISTPACK
         |
         v
[title][Autumn Sale][status][active][views][100]
```

注意：这张图只表示 **field 与 value 交替存放的逻辑顺序**，不等于 listpack 的实际字节级编码；真实 listpack 还包含元素长度等编码信息。

对于字段少、字段和值都比较短的对象，这种紧凑表示能减少内存中的额外结构开销。

但它也有代价。

假设我们需要查找 `views`。对于使用 listpack 编码的 Hash，Redis 需要在压缩存储的元素序列中查找目标 field；这并不是哈希表那种直接通过哈希值定位条目的过程。

现在去 `src/t_hash.c` 的 `hashTypeSet()` 看它怎样更新小 Hash。

它首先会在 `OBJ_ENCODING_LISTPACK` 分支中寻找对应 field；如果已找到，就使用 `lpReplace()` 替换紧跟着的值；如果没找到，就把 field 和 value 追加进去。

**真实源码节选：**

```c
zl = lpAppend(zl, (unsigned char*)field, sdslen(field));
zl = lpAppend(zl, (unsigned char*)value, sdslen(value));
```

这两行位于**新增字段**的分支中，而不是所有更新都会执行的公共路径。

它们正好说明了：小 Hash 新增 `views=100` 时，Redis 会向 listpack 追加一个 field，再追加对应的 value。

所以，`HSET activity:1001 views 100` 在外部只是一条普通的 Hash 写入，内部却可能通过紧凑内存布局来完成。

然而，这种方案是否对任何大小的 Hash 都合适？

## 8. 字段越来越多时，为什么要改用哈希表？

假设活动对象的字段从 3 个增长到几百个，或某个字段值从几十字节增长到几 KB。

此时，如果仍然使用紧凑顺序结构，查找和修改字段时需要遍历的内容就可能增加，修改操作也可能带来更多数据移动。

我们现在遇到了一个新的权衡：

> 小对象希望节省内存；大对象希望字段查找和更新不要因线性扫描而持续变慢。

Redis 7.2.0 为 Hash 提供了另一种编码：`hashtable`。

```text
同一个 Hash 类型
       |
       +-- 小、短字段为主 --> listpack
       |
       +-- 超过阈值 ------> hashtable
```

在源码里，`hashTypeTryConversion()` 和 `hashTypeSet()` 都可能触发编码转换；实际转换由 `hashTypeConvert()` / `hashTypeConvertListpack()` 等函数完成。

当转换为哈希表时，Redis 会遍历已有的 field-value 对，把它们插入新建的字典，再更新对象的 `encoding` 和底层数据指针。

这里要明确三个事实：

1. **对外类型不变。** `TYPE activity:1001` 仍然得到 `hash`。
2. **内部编码变化。** `OBJECT ENCODING activity:1001` 可以从 `listpack` 变为 `hashtable`。
3. **业务语义不变。** `HGET`、`HSET` 仍按 field-value 的语义工作。

这种设计没有规定所有 Hash 都必须是一个 C 语言哈希表。上层命令面对的是统一的 Hash 操作接口，底层可以按规模选择更适合的数据结构。

在 Redis 7.2.0 的默认配置中，相关阈值包括：

```text
hash-max-listpack-entries 512
hash-max-listpack-value   64
```

它们分别约束紧凑编码可承载的字段数量，以及字段名或值的大小。实际转换还会考虑 listpack 自身的安全容量条件。

这不是说“第 513 个字段一定能在任意配置和任意输入下触发唯一一种转换路径”。真正的判断还取决于当前编码、写入参数和运行配置。

接下来用一个小实验，让这种内部变化变得可观察。

## 9. 实验：外部仍是 Hash，内部从 listpack 变成 hashtable

**注意：`CONFIG SET` 会影响整个测试 Redis 实例。下面只应该在独立实验环境中执行，不要直接修改生产实例配置。**

我们不修改刚才的业务对象，而是创建一个独立的测试 Key：

```redis
DEL lab:hash
```

先查看配置：

```redis
CONFIG GET hash-max-listpack-entries
```

对于未修改的 Redis 7.2.0 默认配置，预期值为 `512`。

为了用很少的字段复现编码转换，暂时把阈值调低：

```redis
CONFIG SET hash-max-listpack-entries 2
```

预期返回 `OK`。

现在创建只有两个字段的 Hash：

```redis
HSET lab:hash title sale status active
```

预期返回 `(integer) 2`。

检查数据类型和底层编码：

```redis
TYPE lab:hash
OBJECT ENCODING lab:hash
```

预期分别为：

```text
hash
listpack
```

现在增加第三个字段：

```redis
HSET lab:hash views 100
```

预期返回 `(integer) 1`。

再检查：

```redis
TYPE lab:hash
OBJECT ENCODING lab:hash
HGET lab:hash views
```

预期分别为：

```text
hash
hashtable
"100"
```

我们只执行了普通的 `HSET`，却观察到了底层编码转换。

并且转换没有改变 API：新增的 `views` 依旧能够被 `HGET` 正常读取。

最后恢复这个实验实例的默认阈值，并清理测试 Key：

```redis
CONFIG SET hash-max-listpack-entries 512
DEL lab:hash
```

如果你使用的实例在实验前已经配置成其他值，应恢复为**实验前的实际配置值**，而不是盲目设为 `512`。

本节展示的是依据 Redis 7.2.0 逻辑推导的**预期输出**。文档编写时没有在本环境中实际运行 Redis 服务；读者可以在自己的独立容器中复现。

## 10. HGET 为什么不需要知道对象使用哪一种编码？

我们已经知道同一个 Hash 可能使用 listpack，也可能使用 hashtable。

但调用者仍然只需要执行：

```redis
HGET activity:1001 status
```

它为什么不需要添加一个参数说明底层编码？

进入 `src/t_hash.c` 的 `hgetCommand()`，可以看到正常路径先使用 `lookupKeyReadOrReply()` 查找顶层 Key，检查类型后再调用 `addHashFieldToReply()`。

后者继续调用 `hashTypeGetValue()`，按对象实际编码选择读取方式：

```c
// 教学化简：真实代码见 hashTypeGetValue()
if (encoding == LISTPACK) {
    return lookup_field_in_listpack(field);
} else if (encoding == HASHTABLE) {
    return lookup_field_in_dict(field);
}
```

对于 listpack 分支，真实函数会使用 `hashTypeGetFromListpack()`；对于哈希表分支，则使用 `hashTypeGetFromHashTable()`。

因此：

- 命令入口负责理解客户端的 `HGET key field`。
- Hash 类型的读取接口负责按照当前编码查找字段。
- 网络回复逻辑负责把找到的 value 返回客户端。

这就解释了为什么内部编码可以改变，而客户端代码不用跟着改。

这种设计并不神秘：Redis 把“字段读取是什么”与“字段具体放在哪里”分开了。

## 11. Hash 与 String 的边界：它解决了什么，没有解决什么？

现在回顾活动对象：

```text
activity:1001
  title  = Autumn Sale
  status = ended
  views  = 101
```

Hash 确实解决了本章遇到的问题：这些属性有了共同的顶层 Key，并且可以对单个字段执行 `HGET`、`HSET`，对整数字段执行 `HINCRBY`。

但这不意味着 Hash 能替代所有 String 场景。

如果业务只保存一个访问计数，`SET` / `INCR` 可能已经是最简单、最合适的方案。并不是为了“结构化”就一定要把单个计数包进 Hash。

另外，Hash 也没有自动提供关系型数据库的模式约束、跨多个 Redis Key 的事务一致性或任意层级的嵌套对象语义。不同 field 保存的依然是 Redis 规定的字段值，而不是可以无限递归展开的 Redis 数据结构树。

还要结合上一个基础篇的知识看待生命周期：**Redis 7.2.0 的 `EXPIRE` 针对顶层 Key，而不是 Hash 的某个 field。** 如果不同字段必须独立过期，应该重新审视数据建模，而不是假定这个版本支持字段级 TTL。

最后，`listpack` 到 `hashtable` 的转换不是免费操作。转换时需要遍历原有字段并构建新字典，因此相关写命令可能承担一次额外工作量。不能仅凭“一般情况下查找很快”就断言每次 `HSET` 的耗时都相同。

把这些边界说清楚，才能真正理解 Redis 为什么同时提供 String 和 Hash：它们适用于不同的数据组织和操作需求。

## 12. 回看这条演化路径

我们最开始只需要统计一个活动的访问次数：

```text
一个数字
  |
  v
SET activity:1001:views 100
  |
  | 访问次数增加
  v
INCR activity:1001:views
  |
  | 活动开始拥有标题、状态等相关属性
  v
每个属性一个 String Key
  |
  | 整组字段需要共同清理、统一生命周期
  v
尝试把对象编码成 JSON String
  |
  | 修改某字段需要整体读取与写回
  v
需要服务端按字段读写
  |
  v
Hash：HSET / HGET / HINCRBY
  |
  | 小对象希望减少额外存储开销
  v
listpack
  |
  | 字段规模或长度增长
  v
hashtable
```

这条路径不是 Redis 功能的历史演化顺序，而是我们围绕一个具体业务需求推导的设计过程。

沿真实源码，我们已经知道：

1. `hsetCommand()` 接收已经解析好的字段参数，并统计新增 field 数量。
2. `hashTypeLookupWriteOrCreate()` 找到或创建整个 Hash 对象，顶层 Key 由 Redis 数据库保存。
3. `hashTypeSet()` 真正执行字段写入，并根据编码采用不同的底层操作。
4. 对象较小时，默认使用 `listpack`；超过相应条件后可转换为 `hashtable`。
5. `hgetCommand()` 通过统一的 Hash 读取接口访问字段，不要求客户端知道底层编码。

**本章最重要的收获，并不是记住 `HSET` 语法，而是看清两个层次：业务对外需要的操作语义，与 Redis 内部为实现这些语义选择的存储结构，可以分开演化。**

## 13. 下一章：如果数据不再是一组属性，而是一组有顺序的任务呢？

目前的活动对象适合按字段查找：给出 `status`，读取或更新对应的值。

但现在活动运营提出一个不同的需求：活动报名成功后，需要把报名通知任务交给后台程序依次处理。

这些任务没有天然的 `title`、`status` 这样的固定字段。我们更关心的是它们**进入的先后顺序**，以及应该从哪一端加入、从哪一端取出。

能不能继续把所有任务塞进一个 Hash，然后自己维护 `task:1`、`task:2`、`task:3` 的序号？当然可以尝试，但很快就要自己处理入队位置、出队位置和序号更新。

这说明：当业务关心的操作方式从“按字段读取”变成“按顺序插入和取出”时，Hash 未必是最自然的模型。

下一章，我们就从一个最小的活动报名任务队列开始，研究 Redis List 为什么存在，以及 `LPUSH`、`RPOP` 如何在 Redis 7.2.0 源码中工作。

**下一章：《从 Hash 到 List：为什么有些数据更适合按顺序保存？》**

---

## 源码与资料

本章的源码基线固定为 **Redis 7.2.0**，以下链接均指向该版本，不混用更新版本的 Hash 功能。

- [`src/t_hash.c`：`hsetCommand()`、`hashTypeSet()`、`hashTypeTryConversion()`、`hgetCommand()`、`hashTypeConvertListpack()`](https://github.com/redis/redis/blob/7.2.0/src/t_hash.c)
- [`src/object.c`：`createHashObject()`](https://github.com/redis/redis/blob/7.2.0/src/object.c)
- [`src/db.c`：Redis 顶层键值的数据库操作](https://github.com/redis/redis/blob/7.2.0/src/db.c)
- [Redis 官方：Hash 数据类型](https://redis.io/docs/latest/develop/data-types/hashes/)
- [Redis 官方：内存优化与 listpack 阈值](https://redis.io/docs/latest/operate/oss_and_stack/management/optimization/memory-optimization/)
- [Redis 官方：Hash 字段级过期的版本说明](https://redis.io/faq/doc/29bj4ewcad/how-can-i-take-advantage-of-the-expiration-of-individual-hash-fields)

> **实验声明**：文中的 Redis 命令及预期输出已与 Redis 7.2.0 源码行为对应，但本文编写环境未实际执行 Docker / Redis 实验；不把预期结果表述成已经完成的实测。
