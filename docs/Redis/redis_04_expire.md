---
title: "第四章：数据保存后怎样自动消失？从 EXPIRE 到过期删除机制"
sidebar_label: "04 - EXPIRE 与过期删除"
sidebar_position: 5
description: "沿着活动访问计数这个贯穿案例，从手动清理临时数据出发，逐步阅读 Redis 7.2.0 的过期时间记录、惰性过期与主动过期源码。"
tags:
  - Redis
  - 源码精读
  - EXPIRE
  - TTL
  - 过期删除
---

# 第四章：数据保存后怎样自动消失？从 EXPIRE 到过期删除机制

上一章，我们已经解决了活动访问次数的并发更新问题。

假设活动 `1001` 的访问计数保存在 Redis 中：

```redis
SET activity:1001:views 100
INCR activity:1001:views
```

两个客户端即使同时发出 `INCR`，Redis 也会在各条命令中分别完成读取、计算和写回，不会发生客户端自己组合 `GET + SET` 时的计数丢失。

但现在业务又提出了一个很普通的要求：**活动结束一天后，这个计数就不再需要了。**

如果旧活动越来越多，而计数始终留在内存中，那么我们的数据服务就会保存越来越多没有业务价值的数据。

这一次，我们不研究并发，也不研究新的数据结构。只讨论一个问题：**数据保存之后，怎样在指定时间后失效，同时又不让过期检查拖慢整个服务？**

## 1. 最简单的方案：到时间了就 DEL

先不急着使用 Redis 的过期命令。

如果让我们自己维护临时数据，最直接的实现方式，是在活动结束后执行：

```redis
DEL activity:1001:views
```

这条命令能够删除指定 Key。

如果活动数量很少、结束时间由业务系统明确控制，这甚至可能已经足够。比如活动结束接口里主动调用 `DEL`，就是合理的选择。

但假设活动每天创建数万次，并且每条计数需要保留不同的时长。

我们就必须再写一个负责定期清理的程序：

```python
# 概念伪代码：不是 Redis 内部的实现
while True:
    for activity in list_ended_activities():
        redis.delete(f"activity:{activity.id}:views")
    sleep(60)
```

这个方案也能工作，不过问题开始变多：清理程序必须持续运行；每次扫描多少活动、哪些活动已经清理，都需要由我们管理；如果清理程序故障，过期计数仍然可能长期占据内存。

这时真正缺少的，不是另一个更快的 `DEL`，而是让**数据本身带有失效时间**。

换句话说，在设置或更新计数后，我们希望告诉 Redis：

> 这个 Key 最多还应有效多久。之后的读取不应该再得到旧值。

Redis 提供了 `EXPIRE`。

## 2. 用 EXPIRE 描述数据的生命周期

先把计数设为 `100`，再要求它在 5 秒后过期：

```redis
SET activity:1001:views 100
EXPIRE activity:1001:views 5
```

第二条命令成功时返回整数 `1`，表示成功设置了过期时间；如果 Key 不存在，通常返回 `0`。

现在执行：

```redis
TTL activity:1001:views
```

可以得到剩余的有效秒数。这个数字会随着时间变化，不能把某个固定值当作测试断言。

超过有效时间后，再执行：

```redis
GET activity:1001:views
```

就不应再返回原来的 `100`，而是空结果。

`TTL` 还有两个很有用的特殊返回值：

| 返回值 | 含义 |
| --- | --- |
| `-2` | Key 不存在，或已被视为过期 |
| `-1` | Key 存在，但没有设置过期时间 |
| 非负整数 | 剩余的有效秒数（存在秒级取整） |

看起来，我们已经不需要额外编写清理程序了。

但这里有一个值得追问的问题：**Redis 执行 `EXPIRE key 5` 时，是不是创建了一个“5 秒后自动运行 DEL”的定时任务？**

如果一百万个 Key 都有不同的过期时间，难道 Redis 就要维护一百万个独立计时器吗？

我们先进入真实源码，看看 Redis 究竟保存了什么。

## 3. EXPIRE 的入口：相对时间怎样变成到期时间？

以下源码统一对应 **Redis 7.2.0**。

定位到 [`src/expire.c`](https://github.com/redis/redis/blob/7.2.0/src/expire.c)，可以找到 `expireCommand()`。

**真实源码节选：**

```c
void expireCommand(client *c) {
    expireGenericCommand(c,commandTimeSnapshot(),UNIT_SECONDS);
}
```

这里的 `expireGenericCommand()` 并不只为 `EXPIRE` 服务，也处理毫秒级以及指定绝对过期时间的相关命令。但当前不需要比较它们的全部选项，只看普通 `EXPIRE`。

对这条命令：

```redis
EXPIRE activity:1001:views 5
```

调用时提供两个重要参数：

- `commandTimeSnapshot()`：这次命令使用的当前时间基准，单位为毫秒。
- `UNIT_SECONDS`：客户端给出的 `5` 是秒，需要转换成毫秒。

在 `expireGenericCommand()` 中，源码先解析命令参数，再进行单位换算，并将相对时长加到时间基准上。

**教学化简（不是完整源码）：**

```c
long long seconds = 5;
long long now_ms = commandTimeSnapshot();
long long expire_at_ms = now_ms + seconds * 1000;
```

所以 Redis 记录的核心信息不是“每秒减一”，而是**一个绝对的到期时间戳**。

假设命令执行时的时间为 `12:00:00.000`，那么五秒后的时间就是 `12:00:05.000`。之后只需要比较当前时间与这个时间戳，就可以判断数据是否还有效。

源码还会检查时间参数是否合法、是否溢出以及 Key 是否存在。对于正常存在的 Key，最终会调用：

```c
setExpire(c,c->db,key,when);
```

这里的 `when` 已经是计算好的绝对到期时间，单位是毫秒。

于是问题又变成了：**这个到期时间放在哪里？**

## 4. 为什么 Redis 没有把所有过期时间直接放进 Value？

上一章我们已经知道，Redis 的一个逻辑数据库使用 `db->dict` 保存普通的键值关系。

如果仍然只使用一个最简单的字典，我们可能会把值和过期时间放在一起：

```python
# 教学化简：自行设计的数据模型
store = {
    "activity:1001:views": {
        "value": 100,
        "expire_at_ms": 1234567890000,
    }
}
```

这样可以工作。但 Redis 并不是每个 Key 都有过期时间。长期保存的 Key 如果也必须维护相同的过期字段，就会增加无谓的管理成本。

Redis 7.2.0 的设计是：**普通数据和过期时间分别放在两张字典里。**

```text
redisDb
   |
   +-- dict：保存数据
   |     "activity:1001:views" -> 100
   |
   +-- expires：保存需要过期的 Key 及到期时间
         "activity:1001:views" -> 到期时间戳（毫秒）
```

没有设置过期时间的 Key，只需出现在数据字典中；需要过期的 Key，才需要对应的过期记录。

这不是两个互相独立的业务对象，而是同一个 Key 在两个不同维度上的信息：一个表示值是什么，一个表示何时失效。

我们从 [`src/db.c`](https://github.com/redis/redis/blob/7.2.0/src/db.c) 的 `setExpire()` 里核对这一点。

**真实源码关键语句：**

```c
de = dictAddOrFind(db->expires,dictGetKey(kde));
dictSetSignedIntegerVal(de,when);
```

第一行在过期字典中找到或建立记录；第二行保存绝对过期时间。函数还会从主数据字典取得对应的 Key，以复用 Key 字符串，而不是再制作一份无意义的副本。

**这里真正值得理解的是设计动作：**`EXPIRE` 没有立即删除数据，也没有为这个 Key 创建一个独立线程。它只保存了“何时应该失效”的元数据。

但记录了到期时间，不等于数据就会自动消失。

到了第五秒，如果没有人访问这个 Key，谁负责发现它已经过期？

我们先研究一个更容易理解的场景：**恰好有人在过期之后访问它。**

## 5. 过期后有人来读：从 GET 进入过期检查

假设 Key 的到期时间已经过去。

此时客户端发送：

```redis
GET activity:1001:views
```

如果 Redis 只是到 `db->dict` 中查找，就可能看到尚未从内存中删除的旧值 `100`。

但从业务语义上讲，这个 Key 已经过期，不应该再返回给客户端。

所以，**数据是否有效，必须在读取时得到判断**。

上一章我们已经见过 Redis 的数据库查找函数。在 Redis 7.2.0 的 `src/db.c` 中，通用查找入口 `lookupKey()` 会在找到 Key 后检查过期状态。

其中的真实关键调用是：

```c
if (expireIfNeeded(db, key, expire_flags)) {
    val = NULL;
}
```

我们先不研究复制角色和额外查找标志。对于本章的**普通主节点读取路径**，这段逻辑表达了一个很直接的规则：如果 Key 已过期，就不能把先前找到的旧值正常交给上层命令。

继续进入 `expireIfNeeded()`：它会通过 `keyIsExpired()` 判断当前 Key 是否失效。

核心判断可以教学化地概括为：

```c
// 教学化简：忽略加载、复制和其他特殊执行场景
long long deadline = getExpire(db, key);
bool expired = deadline >= 0 && now_ms > deadline;
```

`getExpire()` 从 `db->expires` 读取到期时间；没有过期设置时返回 `-1`。

对普通主节点而言，如果 Key 已过期，并且当前操作允许实际删除，`expireIfNeeded()` 接下来会调用删除逻辑，释放无效数据并传播相关变更。

这条路径就是 Redis 的**惰性过期删除**：

```text
GET activity:1001:views
          |
          v
在数据字典中查找 Key
          |
          v
检查过期时间
          |
     +----+----+
     |         |
   未过期     已过期
     |         |
     v         v
 返回原值    视为不存在
               |
               v
       主节点按条件删除
```

请注意两个不同的事实：

1. **逻辑过期**：当前时间已经超过有效期，读操作不应再返回旧值。
2. **物理删除**：Redis 真正把该 Key 从内存数据结构中移除。

访问时过期检查让我们能够正确回答“这条数据现在还有效吗”。但是，如果某个 Key 从此再也无人访问，它可能一直没有机会走到这条查找路径。

这就产生了下一个需要解决的问题。

## 6. 无人访问的过期 Key，谁来清理？

假设每天有数万条活动计数设置了过期时间。很多活动结束后，再也没有客户端读取对应的 Key。

只依靠惰性过期，会出现这样的状态：

```text
时间已经过去
     |
     v
Key 在逻辑上过期
     |
     | 没有 GET / SET 等访问
     v
可能仍占据 Redis 内存
```

这样虽然不会让正常读取返回过期数据，但也不能有效回收内存。

我们因此需要另一个机制：**即使无人访问，也能在后台逐步检查并清理过期 Key。**

但最直觉的方案——每一毫秒扫描所有 Key——也不合理。

假设 Redis 保存了一百万个 Key，只有其中的一小部分设置了过期时间，每次扫描全部 Key 都会消耗大量 CPU。扫描太频繁，会影响正常命令的响应时间；扫描太少，过期数据又会占用较多内存。

所以，我们需要的不是“把所有 Key 每次都检查一遍”，而是一个**有节制的、增量式的过期回收过程**。

这就是 Redis 的主动过期删除。

## 7. 主动过期：为什么是抽样，而不是全量扫描？

在 [`src/expire.c`](https://github.com/redis/redis/blob/7.2.0/src/expire.c) 中，负责这件事的核心函数是：

```c
activeExpireCycle(int type)
```

它不是从每个 Key 的 Value 开始工作，而是围绕数据库的 `expires` 字典展开。

这很重要：**只有设置了过期时间的 Key，才是主动过期检查的候选者。**

在源码中，`activeExpireCycle()` 会限制每轮处理的工作量，并对待检查的过期记录进行增量扫描。它通过 `expireScanCallback()` 检查抽中的记录，再由 `activeExpireCycleTryExpire()` 尝试删除到期的 Key。

这段逻辑可以用下面的教学伪代码理解：

```python
# 概念伪代码，不是 Redis 原始算法
while still_have_time_budget():
    candidates = sample_keys_from_expire_dictionary()
    expired_count = 0

    for key in candidates:
        if is_expired(key):
            delete_key(key)
            expired_count += 1

    if too_few_expired(candidates, expired_count):
        break
```

这个模型解释了 Redis 为什么要同时考虑两件事：

- **回收收益**：这轮抽样中有多少 Key 已经过期？
- **时间成本**：本轮过期扫描已经占用了多少 CPU 时间？

如果抽样结果表明仍有较多过期 Key，Redis 可以继续投入一定的工作量；如果已经消耗了本轮时间预算，就需要停下来，把执行机会留给正常请求。

因此，主动过期不是一场必须立即完成的全量清理，而是一种在运行期间不断推进的增量工作。

这也解释了一个容易误解的行为：

> Key 的 TTL 到期，不意味着那个瞬间就必须从内存中被物理删除。

正常读取的正确性由过期检查保证；内存回收则由访问时删除和主动过期扫描共同推进。

## 8. 这个过期扫描由谁触发？

我们已经知道 Redis 的主动过期函数叫 `activeExpireCycle()`，但它并不会自己在另一个无限循环中不停运行。

这时可以复用第二章研究过的**事件循环**。

Redis 7.2.0 在 `src/server.c` 的 `databasesCron()` 中，有一条定期触发的慢速过期检查路径：

```c
activeExpireCycle(ACTIVE_EXPIRE_CYCLE_SLOW);
```

另外，在事件循环准备进入等待之前，`beforeSleep()` 还有一个快速过期检查入口：

```c
activeExpireCycle(ACTIVE_EXPIRE_CYCLE_FAST);
```

快速过期检查并不是每次都真正扫描大量数据。源码会根据上一次扫描是否触及时间限制、估计的过期数据比例以及时间间隔等条件，判断是否有必要执行。

现在我们可以把两章的知识连接起来：

```text
Redis 事件循环
     |
     +-- 客户端请求就绪
     |       |
     |       v
     |   执行 GET / INCR 等命令
     |       |
     |       v
     |   访问时检查过期
     |
     +-- 定期维护 / 进入等待前
             |
             v
       主动过期抽样
             |
             v
       逐步回收过期 Key
```

这里没有突然出现一套全新的并发架构。Redis 利用了我们已经学过的执行和调度机制，在服务正常处理请求的同时安排数据维护工作。

这也带来一个清晰的取舍：主动过期越积极，通常越有利于及时回收过期内存，但也可能占用更多 CPU 时间。因此 Redis 并不承诺每个 Key 都在某个精确毫秒被物理移除。

## 9. 实验一：观察逻辑过期

下面使用独立的 Redis 7.2.0 实验容器，避免误删已有数据库中的数据。

启动：

```bash
docker run -d \
  --name redis-expire-lab \
  -p 127.0.0.1:6381:6379 \
  redis:7.2.0 \
  --save "" \
  --appendonly no
```

准备 Key：

```bash
docker exec redis-expire-lab redis-cli SET activity:1001:views 100
docker exec redis-expire-lab redis-cli EXPIRE activity:1001:views 2
```

预期第二条命令返回 `(integer) 1`。

立即查看剩余时间：

```bash
docker exec redis-expire-lab redis-cli TTL activity:1001:views
```

预期得到一个接近 `2`、但随时间变化的非负整数。

等待超过两秒后再读取：

```bash
sleep 3
docker exec redis-expire-lab redis-cli GET activity:1001:views
docker exec redis-expire-lab redis-cli TTL activity:1001:views
```

预期 `GET` 返回空结果，`TTL` 返回 `-2`。

**这个实验能够验证过期后的可见行为，却不能单凭 `GET` 的空结果证明 Redis 在无人访问时就已把数据清理出内存。** 因为这次 `GET` 本身就可能触发惰性过期。

我们需要一个不同的实验来观察主动回收。

## 10. 实验二：不读取 Key，观察主动过期回收

这一实验要回答的是：**如果没有客户端去访问某个过期 Key，Redis 会不会仍然尝试回收它？**

我们使用 Python 和 `DBSIZE` 来观察数据库中的实际 Key 数量。

`DBSIZE` 在 Redis 7.2.0 中读取的是数据库字典的条目数量，它不会逐个访问所有 Key 并触发惰性过期。因此，它比反复 `GET` 或 `EXISTS` 更适合这个特定实验。

先安装客户端依赖：

```bash
python -m pip install "redis==5.0.1"
```

创建 `observe_expire.py`：

```python
import time
import redis

client = redis.Redis(
    host="127.0.0.1",
    port=6381,
    decode_responses=True,
)

# 只针对上面专门创建的测试容器操作，勿指向生产实例。
client.flushdb()
client.set("lab:unused:activity", "100", ex=1)

print("创建后，DBSIZE =", client.dbsize())

deadline = time.monotonic() + 10
while time.monotonic() < deadline:
    count = client.dbsize()  # 不读取 lab:unused:activity 本身
    if count == 0:
        print("已观察到过期 Key 被回收，DBSIZE = 0")
        break
    time.sleep(0.1)
else:
    print("本次观察窗口内尚未看到回收，可延长时间重新测试")
```

运行：

```bash
python observe_expire.py
```

在正常运行、主动过期机制开启的独立 Redis 实例中，预期会先观察到 `DBSIZE = 1`，随后逐步变成 `0`。具体回收时刻并不保证与一秒的 TTL 精确重合。

本实验的关键不是用一次采样测出“Redis 每隔多少毫秒删除一次 Key”，而是区分两种不同现象：

- `GET` 返回空值：说明当前数据不再对客户端有效。
- 没有人读取目标 Key，而数据库字典的条目数最终下降：说明存在独立于该 Key 访问操作的回收路径。

这与前面在源码中看到的 `activeExpireCycle()` 对应。

本章给出的结果是**根据 Redis 7.2.0 代码推导的预期表现**，本文编写时未实际执行 Redis 容器，不把预期输出描述成已经验证的实验数据。

测试结束后，可清理独立容器：

```bash
docker rm -f redis-expire-lab
```

## 11. EXPIRE 解决了什么，没有解决什么？

现在，我们终于可以回答最初的问题了。

当活动计数不再需要永久保存时，应用可以设置它的有效期，Redis 负责记录截止时间，并在后续读取或主动扫描时处理过期状态。

但有三个边界必须明确。

**第一，过期时间不是一个保证准时执行的任务调度器。**

我们可以依赖它使过期数据不再被正常读取，却不应依赖 Redis 在精确的到期毫秒执行某项外部业务动作。比如“活动到期立刻发一封邮件”不应该只靠等待 Key 被删除来保证业务执行。

**第二，过期删除不能自动解决所有内存问题。**

只有设置了过期时间的 Key，才会进入对应的过期管理路径。长期没有过期时间的 Key 仍然可能不断增长。即使大量 Key 都设置了 TTL，过期到实际回收之间也可能存在一定延迟。

**第三，数据失效不等于数据必须持久化或不持久化。**

过期时间定义的是数据在业务上的有效性；持久化关注的是 Redis 重启后能否恢复数据。它们解决的是不同问题，不应该混为一谈。

还有一个容易踩坑的细节：普通 `SET key value` 通常会清除该 Key 以前设置的过期时间；`INCR` 这种修改整数值的命令则不会因为递增本身自动清除已有 TTL。实际业务需要明确哪些操作会保留或重置生命周期。

## 12. 回头看：过期机制是怎样长出来的？

这一次，我们没有从一个庞大的 Redis 内存管理架构图开始。

相反，每增加一步设计，都是由上一个方案的具体限制推动的：

```text
活动访问计数需要保存
          |
          v
普通 Key-Value 存储
          |
          | 活动结束后数据不再需要
          v
手动 DEL
          |
          | 清理时机需要由外部程序维护
          v
EXPIRE：记录绝对到期时间
          |
          | 读取时不能返回旧值
          v
访问时检查与惰性过期
          |
          | 无人访问的 Key 可能长期占内存
          v
主动过期扫描
          |
          | 全量扫描太贵
          v
对过期字典增量抽样 + 时间预算
```

最终，Redis 用两个互补的机制处理过期数据：

- **惰性过期**：保证访问时能识别过期数据。
- **主动过期**：逐步清理长期无人访问的过期数据，回收内存。

两者配合，才能同时照顾读取语义和资源管理成本。

### 下一章：如果数据没有过期时间，内存满了怎么办？

这时活动系统有了新的情况。

为了做长期统计，有些活动计数不能在一天后自动删除。它们没有 TTL，需要一直保留。随着活动越来越多，Redis 的内存占用仍然持续增长。

即使我们已经具备过期机制，也无法保证这些**没有过期时间、但业务仍然写入的数据**不会耗尽内存。

那么，当 Redis 可使用的内存接近上限时应该怎么办？

是拒绝新的写入，还是自动选择部分旧数据删除？如果需要选择，按照最近访问时间、访问频率或其他规则，又该怎样取舍？

这就是下一章要研究的主问题：**Redis 如何在有限内存中决定哪些数据可以保留、哪些数据应该被淘汰？**

---

## 源码参考与核对范围

本文固定源码版本为 Redis **7.2.0**，重点核对以下真实入口：

- [`src/expire.c`：`expireCommand()`、`expireGenericCommand()`、`ttlGenericCommand()`](https://github.com/redis/redis/blob/7.2.0/src/expire.c)
- [`src/db.c`：`setExpire()`、`getExpire()`、`lookupKey()`、`expireIfNeeded()`](https://github.com/redis/redis/blob/7.2.0/src/db.c)
- [`src/expire.c`：`activeExpireCycle()`、`expireScanCallback()`、`activeExpireCycleTryExpire()`](https://github.com/redis/redis/blob/7.2.0/src/expire.c)
- [`src/server.c`：`databasesCron()`、`beforeSleep()`](https://github.com/redis/redis/blob/7.2.0/src/server.c)
- [Redis 官方 EXPIRE 命令文档](https://redis.io/docs/latest/commands/expire/)
- [Redis 官方 TTL 命令文档](https://redis.io/docs/latest/commands/ttl/)

文中的源码节选只保留支撑当前结论的关键语句；所有教学化简与概念伪代码均已单独标注。源码在不同配置、复制角色和特殊执行上下文中还存在额外分支，本章没有把单机主节点的正常路径冒充为所有情况。
