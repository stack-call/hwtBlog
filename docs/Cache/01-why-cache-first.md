---
title: "缓存体系 01：同一篇文章读了 1000 次，为什么数据库也被查了 1000 次？"
sidebar_label: "一、同一篇文章被读了 1000 次"
sidebar_position: 1
description: "从零实现一个文章详情接口，借助可执行实验一步步推导按 Key 缓存，并精读 CPython functools.cache 的真实调用路径。"
slug: /cache-system/01-why-cache-first
tags:
  - caching
  - python
  - source-code
  - system-design
---

# 同一篇文章读了 1000 次，为什么数据库也被查了 1000 次？

> 这是《缓存体系：从一条查询开始》的第 1 章。本章不需要 Redis、Docker 或 MySQL。我们先让一个只查询文章的程序真正暴露问题，再决定是否值得为它增加缓存。只有当本地缓存的局限出现时，后续章节才有理由引入 Redis。

想象一个刚上线的文章网站。此时它只有一个应用进程、一张文章表，以及一个极普通的接口：

```http
GET /posts/42
```

数据库里保存着：

```json
{"id": 42, "title": "Old Title", "version": 1}
```

用户打开页面，接口查询数据库，把结果返回。事情就结束了。

这样的系统有什么问题吗？没有。**只要负载不高、数据需要保持最新，直接查询数据库是完全合理的初始设计。** 我们不应该因为“成熟系统都用 Redis”就立刻往架构里塞一个缓存。缓存不是免费的：一旦增加副本数据，就必须面对何时更新它、何时删除它、机器之间是否共享它等后续问题。

本章要做的，是找到第一个足够具体的理由：我们什么时候开始*值得*缓存？

## 1. 先把最简单的版本运行起来

为了让每次查询到底做了多少工作可以被看见，我们先暂时不部署 MySQL，而是用一个 `FakeDatabase` 模拟文章表。它的作用只是记录 `read()` 被调用了多少次；这不是在声称 Python 字典与 MySQL 的执行成本相同。

```python
from copy import deepcopy

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
```

这里返回 `deepcopy`，只是为了把“调用方随手修改返回对象”与“数据库值真正改变”区分开。在真实数据库客户端中，序列化、对象映射和连接管理会复杂得多；这些复杂性暂时与我们要研究的问题无关。

应用接口可以简单到只有一句：

```python
# 教学化简：用 get_post 表示 HTTP 处理函数中的查询核心。
class PostService:
    def __init__(self, db):
        self.db = db

    def get_post(self, post_id):
        return self.db.read(post_id)
```

我们不是先讨论架构，而是先看一次真实可观察的运行：

```python

db = FakeDatabase()
service = PostService(db)

print(service.get_post(42))
print("数据库读取次数:", db.reads)
```

结果应该是：

```text
{'id': 42, 'title': 'Old Title', 'version': 1}
数据库读取次数: 1
```

这没有任何问题。既然只有一次请求，多加缓存反而会增加代码和状态。

于是我们不改程序，只改变一个输入条件：同一篇文章被连续访问了 1000 次。期间管理员没有修改标题，数据库返回值保持不变。

```python
# 为了只统计这一组 1000 次请求，先重建测试对象。
db = FakeDatabase()
service = PostService(db)
for _ in range(1000):
    service.get_post(42)

print(db.reads)
```

结果是 `1000`。换句话说，请求列表是：

```text
GET /posts/42 → 查数据库 → Old Title
GET /posts/42 → 查数据库 → Old Title
GET /posts/42 → 查数据库 → Old Title
...
GET /posts/42 → 查数据库 → Old Title
```

**输入重复，结果不变，昂贵的查询动作却重复发生。**

这里需要强调一个条件：它不是“数据库慢，所以缓存一定有用”，而是**同样的读取工作可以在一段时间内被复用**。如果每一次请求都必须看到刚发生的更新，或每次返回结果都依赖当前用户权限，那么我们不能直接把旧值拿来重用。

### 这个重复到底值多少钱？

假设一次数据库读取消耗 `Tdb`，进程内查找一次缓存消耗 `Tmem`，并假设在这个演示场景中 `Tmem` 远小于 `Tdb`。那么 1000 次重复访问的成本近似是：

```text
不缓存：1000 × Tdb
若只有第一次回源：1 × Tdb + 999 × Tmem
```

这只是帮助建立直觉的成本模型，不是压测结论。它没有计算 CPU 竞争、数据库内部缓冲、网络抖动，也没有承诺某个具体的毫秒数。但有一件事已经足够明确：**如果我们能记住刚刚读到的内容，就能跳过后面很多重复工作。**

于是，第一个新机制有了出现的理由。

## 2. 第一次优化：记住上次查询结果

如果访问量几乎都来自 `post:42`，甚至不需要马上使用字典。最小思路是：把上次读取的文章 ID 和结果放在两个变量里。

```python
# 教学化简：只缓存最近一次请求
last_id = None
last_row = None

def get_post(post_id):
    global last_id, last_row

    if last_id == post_id:
        return deepcopy(last_row)

    row = db.read(post_id)
    last_id = post_id
    last_row = deepcopy(row)
    return row
```

第一次调用 `get_post(42)`，`last_id` 不是 `42`，于是查询数据库并记住结果。后面连续 999 次查询同一个 ID，都可以跳过数据库。

这个方案非常朴素，却没有错。对于“**连续访问同一篇文章**”这一组输入，它已经满足目标。

但注意，我们实际开发的是文章网站，不是只服务一篇文章的接口。

现在给它一个同样合法的请求序列：

```text
GET /posts/42
GET /posts/43
GET /posts/42
```

按当前实现逐步走一遍：

| 第几次 | 请求 ID | last_id（进入时） | 是否回源 | 结束后的 last_id |
| --- | --- | --- | --- | --- |
| 1 | 42 | None | 是 | 42 |
| 2 | 43 | 42 | 是 | 43 |
| 3 | 42 | 43 | 是 | 42 |

三次请求，三次数据库读取。虽然第一次已经读到过文章 42，第三次仍然不得不重新查询，因为文章 43 把唯一一个缓存位置占掉了。

我们现在碰到的**不是**“缓存容量不够大”的笼统问题，而是一个很精确的限制：

> 当前方案只记得上一条请求，无法同时记住**不同文章各自对应的结果**。

因此，新需求只需要一句话：**给每篇文章一个独立的缓存位置，让 42 与 43 不互相覆盖。**

这才是我们需要字典的时刻。

## 3. 第二次优化：让文章 ID 成为缓存的 Key

把 `last_id` 和 `last_row` 换成一张以文章 ID 为索引的表：

```python
cache = {}

def get_post(post_id):
    if post_id in cache:
        return deepcopy(cache[post_id])

    row = db.read(post_id)
    if row is not None:
        cache[post_id] = deepcopy(row)
    return row
```

与上一步相比，真正新增的思想只有一个：

**原本只能保存一条结果；现在可以通过 Key 找回不同输入各自对应的结果。**

当 `GET /posts/42` 到来时，程序先问 `42` 是否在 `cache` 中。如果在，直接返回；如果不在，才调用 `db.read(42)`，然后把结果放进去。

这里的两条路径，就是缓存体系中最基础的两个行为：

```text
GET /posts/42
      |
      v
  cache[42] 存在吗？
     /       \
   是         否
   |          |
返回缓存     DB.read(42)
                |
                v
           写入 cache[42]
                |
                v
              返回
```

没有网络、没有 Redis、没有分布式锁。到目前为止，我们只是用一个可索引的字典，避免重复执行同一个查询。

现在重放刚才的问题输入：

```text
get_post(42)  # 未命中，查 DB，缓存 42
get_post(43)  # 未命中，查 DB，缓存 43
get_post(42)  # 命中 42，不再查 DB
```

数据库读取次数变成了 **2**，而不是 3。

如果连续读 1000 次文章 42，数据库读取次数会由 **1000** 降为 **1**。这两个数字并不说明缓存命中后总能获得同样程度的性能收益；它们证明的只是：**在给定的顺序输入和稳定数据假设下，数据库查询次数确实减少了。**

### 为什么用 `post_id in cache`，而不是直接写 `cache.get(post_id)`？

当前的业务接口允许文章不存在，数据库返回 `None`。在最小示例里，我们暂时选择**只缓存存在的文章**。因此 `cache.get` 在这里也能工作，但当我们将来需要区分“Key 根本不存在”和“Key 存在、值却是空结果”时，`if post_id in cache` 的语义更明确。

我们暂时不缓存 `None`。它所造成的后果，会在第 7 章“缓存穿透”中用一个真实输入暴露出来。现在不需要提前引入负缓存。

### 这份缓存被放在哪里？

它位于当前 Python 进程的内存中，属于 `get_post` 所在应用实例。进程退出，字典就会消失。它不是数据库副本，没有独立持久化，也不会自动被其他进程共享。

把这一点记住就够了。我们还没有遇到需要共享的失败场景，因此不在这里引入 Redis。

## 4. 代码能跑通还不够：证明它真的在复用结果

手写缓存时很容易出现一种自欺欺人的测试：只检查最终返回值等于 `Old Title`，却没有验证是否真的减少了数据库读取。

例如下面两个函数都能返回一样的结果：

```python
return db.read(42)   # 每次都查询
return cache[42]     # 不查询数据库
```

如果断言只比较返回值，它们看起来没有差别。

所以测试必须同时观察**返回值是否正确**和**数据库读了几次**。再往前一步，我们可以给缓存自身也加两个计数器：命中多少次、未命中多少次。

```python
class CachedPostService:
    def __init__(self, db):
        self.db = db
        self.cache = {}
        self.hits = 0
        self.misses = 0

    def get_post(self, post_id):
        if post_id in self.cache:
            self.hits += 1
            return deepcopy(self.cache[post_id])

        self.misses += 1
        row = self.db.read(post_id)
        if row is not None:
            self.cache[post_id] = deepcopy(row)
        return row
```

这个设计比上一版只多了两项观察指标，不改变缓存的工作原理。

在一个新的 `FakeDatabase` 上运行：

```python
service = CachedPostService(db)
for _ in range(1000):
    assert service.get_post(42)["title"] == "Old Title"

assert service.hits == 999
assert service.misses == 1
assert db.reads == 1
```

此时 `hits=999` 的含义很具体：有 999 次读取没有走 `db.read()`。`misses=1` 则代表第一次读取需要回源。

**不要把命中率当成最终目标。** 如果 999 次命中返回的是错误版本，百分之 99.9 的命中率也没有意义。性能收益要建立在结果可接受的前提下。这一章暂时假定文章数据不变化；后面会逐步放开这个假设。

## 5. 现在才去读真实源码：标准库如何实现同样的模式？

我们刚才手写的逻辑可以归结为四步：

1. 根据输入参数找到一个可重复使用的 Key；
2. 查这个 Key 是否已有结果；
3. 命中则直接返回；
4. 未命中则执行原函数并保存结果。

如果在很多函数里都重复写这一套，应用代码会充满类似的缓存判断。那么，能否把这一套行为交给一个通用包装器？

Python 标准库提供了一个非常贴近当前需求的例子：`functools.cache`。

我们选择它作为**本章的真实源码证据**，而不是此时硬插一个 Redis `GET`。原因很简单：Redis 是共享的网络键值服务；本章还只有单进程函数级复用。真正的源码对照应该与正在解决的问题处于同一层。

### 5.1 从调用方开始：`@cache` 看起来做了什么？

对于一份**保证不会变化**的文章快照，我们可以这么写：

```python
from functools import cache

snapshot = {42: (42, "Old Title", 1)}

@cache
def load_stable_snapshot(post_id):
    return snapshot[post_id]

for _ in range(1000):
    load_stable_snapshot(42)

print(load_stable_snapshot.cache_info())
```

这里特意用了一个固定不变的 `snapshot`，**不是**建议对会被管理员更新的数据库查询直接永久添加 `@cache`。我们只是借助一个稳定函数，观察通用缓存包装器如何工作。

在这个例子中，`post_id=42` 是缓存键的输入。第一次调用执行函数体，后续相同参数的调用返回缓存值。

`cache_info()` 能给出命中与未命中计数。它是一个真实可调用的标准库接口，不是我们自定义的统计函数。

### 5.2 固定版本，先看最小入口

源码固定为 **CPython v3.12.8**。

打开 [`Lib/functools.py` 中的 `cache()`](https://github.com/python/cpython/blob/v3.12.8/Lib/functools.py)，可以看到其核心语义只有一句：

```python
# 真实源码节选：CPython v3.12.8, Lib/functools.py
return lru_cache(maxsize=None)(user_function)
```

这行代码并不需要你马上理解 LRU 淘汰算法。`maxsize=None` 在这里恰恰意味着**不设置条目数量上限，不进行按容量的 LRU 淘汰**。也就是说，`cache()` 只是创建了一个能按参数记住函数结果的包装器。

这里可以回答一个设计问题：为什么 `@cache` 不需要我们手写 `cache[post_id]`？因为它把“以参数作为 Key，命中就返回、未命中就执行原函数”的逻辑放在包装器里了。

### 5.3 真实运行路径并不止这一行

`functools.py` 中还定义了 Python 版本的 `_lru_cache_wrapper`。但在标准 CPython 构建中，它又尝试从 `_functools` 导入 C 实现覆盖该名称。因此不能把 Python 的后备实现误说成所有 CPython 调用的实际底层路径。

为了让源码证据准确，我们继续到 [`Modules/_functoolsmodule.c`](https://github.com/python/cpython/blob/v3.12.8/Modules/_functoolsmodule.c) 中查看 `infinite_lru_cache_wrapper()`：这是无容量上限缓存的核心处理路径。

为了避免大段贴源码，我们只观察这三个动作在函数里的对应位置：

```c
/* 真实源码节选：CPython v3.12.8, Modules/_functoolsmodule.c */
result = _PyDict_GetItem_KnownHash(self->cache, key, hash);
```

这里的 `self->cache` 对应包装器维护的缓存字典；`key` 来自调用时的参数；`hash` 则用于字典查找。命中时，函数直接把已有结果返回给调用者。

如果没有找到结果，代码会调用原函数：

```c
/* 真实源码节选 */
result = PyObject_Call(self->func, args, kwds);
```

计算完成后，再将结果保存到内部字典。它与我们手写的 `db.read()` 后 `cache[post_id] = row` 是同一个**设计模式**，但不是同一个实现：一个是标准库的 C 包装器，另一个是我们应用层的 Python 字典。

因此，沿着真实运行过程，可以把关系写成：

```text
load_stable_snapshot(42)
     |
     v
functools.cache 创建的包装器
     |
     v
根据参数生成 Key
     |
     v
字典查找
     |                    \
     | 命中                | 未命中
     v                     v
返回已保存结果      调用原函数 load_stable_snapshot
                            |
                            v
                       保存结果并返回
```

这不是最终的缓存系统架构图，只是本章已经逐步推导出的那条单进程调用路径。

### 5.4 为什么没有直接把 `@cache` 用在正式文章接口上？

因为它的无上限缓存有一个明确前提：**相同参数应该允许复用旧结果，而且缓存规模在使用场景中可接受。**

在真实文章网站中，管理员可能修改标题。`get_post(42)` 的参数没变，但数据库内容可能已经变了。单靠 `@cache` 看不出数据库的变化，也没有自动帮我们失效缓存。

同样，`@cache` 不设置最大条目数；如果读者不停请求新的文章 ID，保存的结果可能持续占用内存。我们今天只是用它验证“命中—未命中—保存”的通用原理；过期和淘汰分别留给真正遇到相应限制时再讨论。

## 6. 第一次真正碰到缓存的代价：结果变旧了

到这里，缓存的收益已经能够测量。那我们可以放心把它放进线上了吗？还不能。

现在只改变一个条件：网站管理员把文章 42 的标题从 `Old Title` 改为 `New Title`。

数据库状态变成：

```text
DB[42] = {title: "New Title", version: 2}
```

但缓存里之前保存的是：

```text
cache[42] = {title: "Old Title", version: 1}
```

再次执行 `get_post(42)` 时，我们刚才写好的代码会优先命中 `cache[42]`，于是继续返回 `Old Title`。

这是一个非常重要的因果转折：

> 我们为了减少重复查询，保存了一份旧结果；但既然它是独立的一份数据，就不能自动知道数据库后来发生了什么变化。

这不是“Python 字典不够专业”的问题。换成 Redis，缓存也不会凭空知道你的 MySQL 行被修改了。缓存与原始数据的更新关系，需要业务来设计。

先不要急着给所有 Key 增加 TTL，也不要直接引入消息队列。现在只能确定**旧值会出现**，还没有决定缓存是否应该由多个应用进程共享、更新时如何失效。我们会在第 3 章，利用管理员改标题这个具体反例，认真推导缓存失效；但在那之前，应用部署方式先带来一个更基础的变化。

## 7. 如果有两个应用进程呢？

上线一段时间以后，网站流量上升，我们增加了第二个应用实例。现在请求可能到达 A，也可能到达 B。

请注意：我们这里还没有引入新的缓存组件。A、B 都运行上一节写过的 `CachedPostService`，各自拥有一张 Python 字典。

```text
请求 1：GET /posts/42 → 应用 A → A.cache 未命中 → DB
请求 2：GET /posts/42 → 应用 B → B.cache 未命中 → DB
请求 3：GET /posts/42 → 应用 A → A.cache 命中
```

如果两个应用实例从同一份数据库读，那么这三次请求发生了**两次**数据库读取。即使 A 早已缓存文章 42，B 仍然不知道。

为什么不能简单让 B 直接使用 A 的字典？因为这份字典只在 A 的进程内存里。B 不是 A 的另一个函数调用，而是一个独立进程，甚至可能位于另一台机器。

此时我们才真正有了下一章的需求：

**能不能把缓存放到 A 与 B 都能访问的位置，让它们不再各自回源？**

这会自然引出共享缓存与 Redis。但我们还不急着解答，因为本章已经把第一个机制——**按 Key 记住重复查询结果**——完整讲清楚了。

## 8. 完整可运行实验：自己复现七个观察结果

下面的脚本只依赖 Python 3.9+ 标准库。复制到 `ch01_cache_from_scratch.py`，执行：

```bash
python3 ch01_cache_from_scratch.py
```

它不是性能测试工具，也没有连接真实 MySQL。它用确定性的断言验证**数据库调用次数、缓存命中、旧值和实例隔离**，让每一段设计结论都有对应的输入与输出。

<details>
<summary>展开完整实验代码（可直接复制运行）</summary>

```python
from copy import deepcopy
from functools import cache


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


class PostService:
    def __init__(self, db, enabled):
        self.db = db
        self.enabled = enabled
        self.cache = {}
        self.hits = 0
        self.misses = 0

    def get(self, post_id):
        if self.enabled and post_id in self.cache:
            self.hits += 1
            return deepcopy(self.cache[post_id])

        self.misses += 1
        row = self.db.read(post_id)
        if self.enabled and row is not None:
            self.cache[post_id] = deepcopy(row)
        return row


def one_slot(db, ids):
    last_id, last_row = None, None
    for post_id in ids:
        if last_id == post_id:
            row = deepcopy(last_row)
        else:
            row = db.read(post_id)
            last_id, last_row = post_id, deepcopy(row)
        assert row["id"] == post_id


def main():
    db = FakeDatabase()
    service = PostService(db, enabled=False)
    for _ in range(1000):
        assert service.get(42)["title"] == "Old Title"
    assert db.reads == 1000
    print(f"A 无缓存：请求=1000，数据库读取={db.reads}")

    db = FakeDatabase()
    one_slot(db, [42, 43, 42])
    assert db.reads == 3
    print(f"B 单槽缓存：请求序列=42,43,42，数据库读取={db.reads}")

    db = FakeDatabase()
    service = PostService(db, enabled=True)
    for post_id in [42, 43, 42]:
        assert service.get(post_id)["id"] == post_id
    assert (db.reads, service.hits, service.misses) == (2, 1, 2)
    print(f"C 按 Key 缓存：请求序列=42,43,42，读取={db.reads}，命中={service.hits}")

    db = FakeDatabase()
    service = PostService(db, enabled=True)
    for _ in range(1000):
        service.get(42)
    assert (db.reads, service.hits, service.misses) == (1, 999, 1)
    print(f"D 按 Key 缓存：请求=1000，读取={db.reads}，命中={service.hits}")

    snapshot = {42: (42, "Old Title", 1)}
    calls = {"read": 0}

    @cache
    def load_stable_snapshot(post_id):
        calls["read"] += 1
        return snapshot[post_id]

    for _ in range(1000):
        assert load_stable_snapshot(42) == (42, "Old Title", 1)
    info = load_stable_snapshot.cache_info()
    assert (info.hits, info.misses, calls["read"]) == (999, 1, 1)
    print(f"E functools.cache：hits={info.hits}，misses={info.misses}")

    db = FakeDatabase()
    service = PostService(db, enabled=True)
    first = service.get(42)
    db.rename(42, "New Title")
    second = service.get(42)
    assert first["title"] == second["title"] == "Old Title"
    assert db.rows[42]["title"] == "New Title"
    print(f"F 数据更新：DB={db.rows[42]['title']}，缓存返回={second['title']}")

    db = FakeDatabase()
    app_a = PostService(db, enabled=True)
    app_b = PostService(db, enabled=True)
    app_a.get(42)
    app_b.get(42)
    assert db.reads == 2
    print(f"G 两个进程各自缓存：数据库读取={db.reads}")
    print("全部断言通过（教学模型，不是 Redis / MySQL 集成测试）")


if __name__ == "__main__":
    main()
```

</details>

这份脚本我在撰写时**实际执行过**，得到的输出如下：

```text
A 无缓存：请求=1000，数据库读取=1000
B 单槽缓存：请求序列=42,43,42，数据库读取=3
C 按 Key 缓存：请求序列=42,43,42，读取=2，命中=1
D 按 Key 缓存：请求=1000，读取=1，命中=999
E functools.cache：hits=999，misses=1
F 数据更新：DB=New Title，缓存返回=Old Title
G 两个进程各自缓存：数据库读取=2
全部断言通过（教学模型，不是 Redis / MySQL 集成测试）
```

你可以修改请求列表，例如把 `[42, 43, 42]` 改成 `[42, 43] * 500`，重新观察“单槽缓存”和“按 Key 缓存”的 DB 调用次数。不要只看最终打印的数值；试着在纸上先预测每一步的命中与未命中，再运行代码验证。

## 9. 回看这条演化路径

现在我们不需要背缓存定义，也可以复述这章的设计为什么一步步变成现在这样：

```text
只有一次读取
    ↓
直接查数据库：简单而正确
    ↓
同一文章被重复读取 1000 次
    ↓
重复工作太多 → 记住上一条结果
    ↓
请求变成 42 → 43 → 42
    ↓
单槽结果被覆盖 → 按文章 ID 存多条结果
    ↓
缓存命中，数据库调用显著减少
    ↓
管理员修改数据库 → 缓存可能变旧
    ↓
先不解决一致性；又新增第二个应用进程
    ↓
A 的内存不能被 B 直接复用
    ↓
下一章的问题：是否需要共享缓存？
```

这条路径的价值不是证明“所有接口都应该加缓存”，而是让我们知道**什么条件成立时，缓存能够省掉哪一段工作**。本章的设计边界也很清楚：

- 单进程内存缓存减少的是**重复查询**，不是数据库所有开销；
- 1000 次请求只读 1 次的结果建立在**顺序执行且同一 Key 的值不变化**的条件上；并发同时未命中的情况现在没有验证；
- `functools.cache` 证明函数级缓存包装器的真实实现方式，但它不是完整的业务缓存一致性方案；
- 如果部署多个独立应用进程，字典缓存天然隔离，不能指望它们互相命中。

下章我们不凭空介绍 Redis，而是直接让 **应用 A 和 B 同时收到 `GET /posts/42`**。当各自的本地缓存都未命中、两台机器再次重复查询数据库时，共享缓存才有一个真正必须回答的理由。

**下一篇：《两台应用机器，为什么还要各查一次数据库？——从进程内字典到 Redis 共享缓存》**。

---

### 固定源码与验证说明

- [CPython v3.12.8：`Lib/functools.py` — `cache()`、`lru_cache()`](https://github.com/python/cpython/blob/v3.12.8/Lib/functools.py)
- [CPython v3.12.8：`Modules/_functoolsmodule.c` — `infinite_lru_cache_wrapper()`](https://github.com/python/cpython/blob/v3.12.8/Modules/_functoolsmodule.c)
- [Python 文档：`functools.cache` 与 `lru_cache`](https://docs.python.org/3/library/functools.html)

**证据边界**：本文中标为“真实源码节选”的代码只来自固定 CPython 标签，省略了错误处理、参数编码和引用计数等与本章因果链无关的细节；Python 实验只验证教学模型与本机标准库行为，没有执行真实数据库、Redis 或多进程网络集成测试。`functools.cache` 在并发首次未命中时可能重复执行被包装函数，不能把本章的顺序实验当作并发去重保证。
