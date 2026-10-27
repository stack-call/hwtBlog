---
title: "06｜从磁盘到内存：为什么需要 Buffer Pool？"
sidebar_position: 7
slug: /mysql/06-why-buffer-pool
description: "沿着商品 42 的主键查找，先复现重复读取相同页面的成本，再对照 MySQL 8.4.0 源码追踪 Buffer Pool 的缓存命中、缺页读取与空间复用。"
tags: [MySQL, InnoDB, Buffer Pool, B+树, 源码精读]
---

# 从磁盘到内存：为什么需要 Buffer Pool？

上一章，我们已经能够沿着一棵最小的索引树定位商品 `42`。

为了让问题足够清楚，实验里只有两层：根页是 **6 号页**，商品 `42` 在 **5 号叶子页**。查找时，我们先读取根页 6，按照主键选择子页，再读取叶子页 5，最后在页面中找到库存 `100`。

```text
查询商品 42
    │
    ▼
读取根页 6
    │  42 属于最后一个键值范围
    ▼
读取叶子页 5
    │
    ▼
找到 (id=42, stock=100)
```

相比逐页扫描，B+ 树已经显著缩小了需要访问的页面范围。

但是，假设商品 42 是一件热门商品。第一位用户访问它，要读取根页 6 和叶子页 5；下一位用户紧接着访问同一件商品，还是读取这两个页。**如果每次都重新发起页面文件读取，B+ 树虽然减少了要找多少页，却没有消除对同一页面的重复读取。**

我们今天只解决这个问题：**已读取过的页面，能不能在内存中复用？**

本章先研究“取页面”，不研究“修改页面”。当下只要继续理解这次库存扣减的前半段——找到商品记录。至于将 `100` 真正改成 `99` 后，内存与磁盘如何协调，留到下一章。

## 1. 最简单的办法：每次都从文件读

回忆上一章的教学页文件：每页 64 字节，前 8 字节为简化页头，后面保存若干 `(id, stock)` 或 `(键值上界, 子页号)` 二元组。这个格式是教学用的，**不是 InnoDB 真实页格式**。

不引入缓存时，读取一页的代码可以写成：

```python
PAGE_SIZE = 64

def read_page(path, page_no):
    with open(path, "rb") as file:
        file.seek(page_no * PAGE_SIZE)
        return file.read(PAGE_SIZE)
```

假设第一次查找商品 42，程序执行：

```python
root = read_page(path, 6)
leaf = read_page(path, 5)
```

下一次相同查找，又会执行同样的两次文件读取。

两次购买请求可能分别属于两个客户端，但只要它们访问同一棵索引树，就有可能需要同样的根页与叶子页。

| 查询 | 所需页号 | 调用文件读取的次数 |
|---|---|---:|
| 第一次查商品 42 | 6 → 5 | 2 |
| 第二次查商品 42 | 6 → 5 | 2 |
| 合计 | | 4 |

这里刻意只统计**教学程序发出的文件读取调用**。操作系统可能把文件内容缓存起来，所以调用 `read()` 不等于发生一次物理磁盘 I/O。这个区分后面仍然适用。

目前的问题也因此更加精确：我们想减少的是**重复访问页面时进入文件读取路径的次数**。

一个直观方案是：**第一次读到页面后，留一份在内存里；下一次需要同一页时，先检查内存。**

## 2. 加一个最简单的内存缓存

先不考虑空间限制，只使用一个 Python 字典：

```python
page_cache = {}

def get_page(path, page_no):
    if page_no in page_cache:
        return page_cache[page_no]

    raw = read_page(path, page_no)
    page_cache[page_no] = raw
    return raw
```

这段代码只引入一个新的决定：**先查内存，再决定要不要读文件。**

第一次调用 `get_page(path, 6)` 时，缓存里没有 6 号页，所以需要读文件，并将结果保存在字典中。

随后调用 `get_page(path, 5)`，同样需要读文件。

第二次查询商品 42 时，再次调用 `get_page(path, 6)`，可以直接得到缓存中的页面；叶子页 5 也一样。

```text
第一次查询
  get_page(6) → 缓存没有 → 读取文件 → 保存页 6
  get_page(5) → 缓存没有 → 读取文件 → 保存页 5

第二次查询
  get_page(6) → 缓存命中 → 返回页 6
  get_page(5) → 缓存命中 → 返回页 5
```

此前的“根据键值找到页号”不需要改变：还是从根页走向叶子页。改变的是**拿到页号之后，取得页面内容的方式**。

这个设计很重要，因为它把两个职责分开了：

- **索引树**回答“商品 42 在哪个页面、应该沿哪条路径找”。
- **页面缓存**回答“6 号页或 5 号页已经在内存里了吗；如果不在，怎样获取”。

它们不是两种彼此替代的索引。缓存没有改变 B+ 树的键值顺序，也不知道商品 `42` 的业务含义。它只管理已经确定的页面。

不过，这个字典实现还没有接受真正的考验：**它的内存会一直增长。**

如果数据库持续读取新的商品页面，最终会有越来越多的页面进入字典。内存不可能无限大。我们需要让缓存既能复用热门页面，又能在空间不足时腾出位置。

此时再引入“替换页面”的规则才有必要。

## 3. 一个新限制：缓存只能保存两页

暂时给缓存设置非常小的容量：**最多只能保存两个页面**。

这是教学限制，故意让淘汰行为容易观察，不代表真实 InnoDB 只有两页缓存。

先连续两次查询商品 42。

```text
第一次：页 6 → 页 5
缓存：[6, 5]

第二次：页 6 → 页 5
缓存：[6, 5]
```

到这里，缓存始终能满足要求。

接下来，突然有用户查询商品 `1`。它也经过根页 6，但目标记录在叶子页 0。

缓存里已经有页 6 和页 5，没有空间放页 0。必须决定移除哪一个。

如果我们把最近最少使用的页面优先移除，就会发现：刚刚查询商品 1 时，根页 6 才被访问过，而页 5 比它更久没被访问。可以移除页 5，再放入页 0。

```text
读取商品 1 的根页 6：缓存命中
读取商品 1 的叶页 0：缓存未命中

需要腾位置：移除页 5
缓存变成：[6, 0]
```

现在再次查询商品 42：根页 6 仍然命中，但叶子页 5 已经不在了，需要重新读一次文件，并用它替换页 0。

这说明了缓存的另一条必要原则：

**“曾经读过”不保证“永远在内存里”。是否还能命中，还取决于容量与页面替换过程。**

我们刚才使用的“优先淘汰最近最少使用页面”通常称为 LRU（Least Recently Used）。现在这个名字才有必要出现，因为我们确实遇到了“空间不够时如何选择移除谁”的问题。

接下来，我们把这些行为写成一个可以运行的完整实验。

## 4. 实验：让两次主键查找复用真实的教学页文件

下面的程序仍使用上一章的 **64 字节教学页**、**6 个叶子页**和 **1 个根页**，从而保证继续追踪同一个商品 42。

与上一章的区别是：所有页面读取现在统一经过 `BufferPoolDemo.get()`。我们分别记录逻辑页面请求、命中次数、文件读取次数和被替换的页面。

**可运行 Python 教学化简；不是 MySQL Buffer Pool 的真实实现。**

```python
from collections import OrderedDict
from pathlib import Path
from struct import pack, unpack_from
from tempfile import TemporaryDirectory

PAGE_SIZE = 64
ROOT_PAGE = 6


def encode_page(kind, entries):
    raw = pack("<II", kind, len(entries))
    raw += b"".join(pack("<ii", a, b) for a, b in entries)
    if len(raw) > PAGE_SIZE:
        raise ValueError("Page overflow")
    return raw.ljust(PAGE_SIZE, b"\x00")


def create_index_file(path):
    with path.open("wb") as f:
        # 0~5 号页：按主键有序排列的叶子记录
        for page_no in range(6):
            start = page_no * 7 + 1
            entries = [(i, 100) for i in range(start, start + 7)]
            f.write(encode_page(1, entries))

        # 6 号页：最小的内部导航页
        branches = [
            ((page_no + 1) * 7, page_no)
            for page_no in range(6)
        ]
        f.write(encode_page(2, branches))


def decode_page(raw):
    kind, count = unpack_from("<II", raw, 0)
    entries = [
        unpack_from("<ii", raw, 8 + i * 8)
        for i in range(count)
    ]
    return kind, entries


class BufferPoolDemo:
    def __init__(self, path, capacity):
        self.path = path
        self.capacity = capacity
        self.cache = OrderedDict()
        self.requests = 0
        self.hits = 0
        self.file_reads = 0
        self.evictions = []

    def get(self, page_no):
        self.requests += 1

        if page_no in self.cache:
            self.hits += 1
            self.cache.move_to_end(page_no)
            return self.cache[page_no]

        with self.path.open("rb") as f:
            f.seek(page_no * PAGE_SIZE)
            raw = f.read(PAGE_SIZE)
        self.file_reads += 1

        if len(raw) != PAGE_SIZE:
            raise ValueError(f"Missing page {page_no}")

        if len(self.cache) >= self.capacity:
            evicted, _ = self.cache.popitem(last=False)
            self.evictions.append(evicted)

        page = decode_page(raw)
        self.cache[page_no] = page
        return page

    def stock(self, product_id):
        kind, branches = self.get(ROOT_PAGE)
        assert kind == 2

        leaf_no = next(
            page_no
            for high, page_no in branches
            if product_id <= high
        )

        kind, rows = self.get(leaf_no)
        assert kind == 1

        return next(
            stock
            for item_id, stock in rows
            if item_id == product_id
        )


with TemporaryDirectory() as tmp:
    path = Path(tmp) / "products.pages"
    create_index_file(path)

    cached = BufferPoolDemo(path, capacity=2)

    print("首次查询商品42:", cached.stock(42))
    print("首次：请求/命中/文件读 =",
          cached.requests, cached.hits, cached.file_reads)

    print("再次查询商品42:", cached.stock(42))
    print("两次合计：请求/命中/文件读 =",
          cached.requests, cached.hits, cached.file_reads)

    print("再查询商品1:", cached.stock(1))
    print("又查询商品42:", cached.stock(42))
    print("四次合计：请求/命中/文件读 =",
          cached.requests, cached.hits, cached.file_reads)
    print("被替换的页:", cached.evictions)
    print("缓存中的页(LRU->MRU):", list(cached.cache))
```

**实际运行输出：**

```text
首次查询商品42: 100
首次：请求/命中/文件读 = 2 0 2
再次查询商品42: 100
两次合计：请求/命中/文件读 = 4 2 2
再查询商品1: 100
又查询商品42: 100
四次合计：请求/命中/文件读 = 8 4 4
被替换的页: [5, 0]
缓存中的页(LRU->MRU): [6, 5]
```

我们只关注这组输出实际证明了什么。

第一次查询商品 42，需要访问两个页面。两页都不在缓存里，因此发生两次文件读取调用。

第二次查询完全相同，逻辑页面请求增加了两次，**文件读取次数却没有增加**。两次主键定位仍然经过根页与叶子页，但页面内容直接从内存缓存取得。

第三次查询商品 1，用到了新的叶子页 0。由于容量只有两页，缓存需要淘汰页 5。

第四次查询商品 42，又需要把页 5 读回来，页 0 被淘汰。

可以进一步概括为：

| 完成到哪次查询 | 累计页面请求 | 累计命中 | 累计文件读取调用 |
|---|---:|---:|---:|
| 商品 42，第一次 | 2 | 0 | 2 |
| 商品 42，第二次 | 4 | 2 | 2 |
| 商品 1，第三次 | 6 | 3 | 3 |
| 商品 42，第四次 | 8 | 4 | 4 |

这个实验没有模拟真实磁盘硬件，也没有读取 InnoDB 的 `.ibd` 文件。所谓“文件读”，只是 `open + seek + read` 的调用次数，不能直接称作“物理磁盘 I/O 次数”。

但它已经说明了当前设计为什么必要：**索引树不变；在取页路径上增加一个内存缓存，就能避免重复走文件读取代码。**

现在，带着这个模型，我们去 MySQL 8.4.0 源码核对。

---

## 5. 真实源码：B+ 树取页为什么先经过 Buffer Pool？

这一章继续使用 MySQL Server **8.4.0**、Git Tag **`mysql-8.4.0`**。

上一章，我们在 `btr_cur_search_to_nth_level()` 中看到了树的搜索怎样获取根页、逐层选择子页，其中会走到通用的缓冲页获取入口：

`storage/innobase/buf/buf0buf.cc` 中的 `buf_page_get_gen()`。

[查看 MySQL 8.4.0：buf0buf.cc](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/buf/buf0buf.cc)

先回答一个很小的问题：**调用方已经知道“我需要某个页号”，怎样得到页内容？**

### 5.1 调用方传给缓冲层的是什么？

`buf_page_get_gen()` 的关键参数包括：

- `page_id`：需要哪个页面。它包含表空间 ID 和页号，不能只凭一个全局页号来定位所有表的页。
- `page_size`：该页面的大小信息。
- `rw_latch`：本次获取页面所需的保护方式。
- `mode`：页面获取模式，比如正常获取或只在缓存中尝试获取。
- `mtr`：与本次内部页面操作相关的 mini-transaction 上下文。它不是我们 SQL 级别的购买事务，暂时无需深入。

这些参数来自 InnoDB 的调用过程，并不是客户端在 SQL 里指定了“读第 5 页”。客户端给的是 `id = 42`；索引搜索逐层决定需要访问哪一页，然后交给缓冲层获取页面。

在正常页面获取路径中，`buf_page_get_gen()` 通过 `Buf_fetch_normal` 继续处理。这个函数并不等于“一进入就从磁盘读”。

### 5.2 先找内存：`Buf_fetch_normal::get()`

同一个文件中的 `Buf_fetch_normal::get()`，正常路径里有两个紧挨着的关键动作。真实源码的核心调用是：

```cpp
block = lookup();
```

当它找不到已经缓存的块时，才走：

```cpp
read_page();
```

这与我们的 `BufferPoolDemo.get()` 对应得非常直接。

| 教学代码 | 真实源码中的对应动作 |
|---|---|
| `if page_no in self.cache` | `lookup()` 查询缓存中是否已有目标页 |
| `return self.cache[page_no]` | 命中后固定页面并返回可访问的缓冲块 |
| 文件 `seek/read` | 未命中时由 `read_page()` 发起后续取页过程 |

但是，这里不能把 MySQL 简化成“一个没有并发的字典”。真实源码在命中后还需要检查页面状态、保护缓冲块，必要时等待进行中的 I/O。仅仅在哈希表里找到了一个指针，并不等于任何线程都可以立刻无条件使用这个页面。

对我们当前的正常主键读取而言，先抓住最重要的行为：**命中可复用的页，就不必重新为同一个页发起磁盘读取。**

### 5.3 `lookup()` 又是怎样按页号查找的？

现在的问题与前面有点像：缓存中可能保存很多页，如果为了找到页 5，又要逐个扫描内存里的所有缓存页，缓存自身的查找也会变慢。

因此，缓存需要一种能根据页面身份快速定位缓冲块的组织方式。

在 `Buf_fetch<T>::lookup()` 中，源码使用 `buf_page_hash_get_low()` 从 Buffer Pool 的页哈希结构查找对应的缓冲块。

这里我们终于看到了教学字典与真实实现之间的对应关系：我们的字典以 `page_no` 为键，而 InnoDB 通过页面标识在页哈希中查找缓冲块；实际还要结合缓冲池实例、同步保护和块状态。

请不要把这个页哈希与“给 `products.id` 建立业务索引”混在一起：

- `products.id` 的 **B+ 树索引**：按商品主键定位叶子记录。
- Buffer Pool 的 **页哈希**：按页面标识查找某个磁盘页当前是否已经缓存。

前者负责找记录；后者负责拿页面。两者解决的问题不同，也不会互相替代。

### 5.4 缓存未命中后，真正的页面读取从哪里发起？

沿着 `Buf_fetch<T>::read_page()`，可以看到实际的底层请求被交给 `buf_read_page()`：

```cpp
buf_read_page(m_page_id, m_page_size)
```

此时，要读取的不是“任意文件中的 64 个字节”，而是具有表空间、页大小、页状态等信息的 InnoDB 页面。读取过程中还涉及缓冲块准备、I/O 状态与可能的预读等操作。

我们先只保留已经足以解释当前问题的主干：

```text
B+ 树搜索发现下一页号
          │
          ▼
   buf_page_get_gen()
          │
          ▼
 Buf_fetch_normal::get()
          │
          ▼
       lookup()
       /      \
     命中      未命中
      │          │
      ▼          ▼
   复用页    read_page()
                 │
                 ▼
           buf_read_page()
                 │
                 ▼
          页面进入缓存
```

这是**从已知页号取页面的教学调用图**，不是包含所有并发、压缩页和预读分支的完整调用栈。与前面的 Python 实验相比，它多出了页面保护和 I/O 状态管理，但主干意图一致。

这里的源码已经为我们的设计推论提供了直接证据：InnoDB 确实先尝试查找缓存中的页面，找不到才进入页面读取路径。

## 6. 缓存空间不足：哪段源码负责腾位置？

前面的 Python 实验还暴露了第二个问题：容量有限。我们用普通 LRU 直接移除了最久没有使用的页面。

真实 MySQL 不可能每次增加一个新页面就无限分配内存。它需要复用可用的缓冲块。

回到同一个源码文件，可以找到 `buf_page_init_for_read()`。在为普通未压缩页面准备读取缓冲块的分支中，它会调用：

```cpp
buf_LRU_get_free_block(buf_pool)
```

这个函数实现位于：

[MySQL 8.4.0：storage/innobase/buf/buf0lru.cc](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/buf/buf0lru.cc)

它的作用可以先用一句话解释：**需要新缓冲块时，优先取得空闲块；没有空闲块，再尝试从当前缓存中回收可复用的块。**

真实源码中，`buf_LRU_get_free_block()` 先尝试：

```cpp
buf_LRU_get_free_only(buf_pool)
```

没有拿到可用块时，它还可能进入 LRU 扫描，尝试回收符合条件的缓冲块。

这里最重要的不是记住每个函数名，而是理解为什么不能简单地“找到 LRU 尾部就删除”：

**有些页面正在被其他操作使用，不能直接复用；有些页面已被修改但尚未安全写回，也不能像干净的只读缓存那样随便丢弃。**

我们当前的教学模型只有只读页面，因此 `popitem(last=False)` 就够了。真正的 InnoDB 需要额外状态与同步手段，以避免错误地复用正在使用或需要保存修改的页面。

“正在被使用”的状态保护并不是本章的重点；“已修改尚未写回”的问题，则将在下一章围绕库存 `100 → 99` 的写入自然出现。

### 6.1 为什么 MySQL 不直接使用最朴素的 LRU？

我们的两页缓存使用严格的最近最少使用顺序，便于运行验证。但 InnoDB 实际使用的是 **改进的 LRU 策略**：把链表区分成偏新的页面和偏旧的页面，新的读取页面通常先进入旧区域附近，并依据后续访问情况决定是否提升。

为什么要额外做这件事？

假设系统突然执行一次很大的报表扫描，依次读入成千上万个只使用一次的页面。如果所有新页都立即占据最有利的位置，原来经常被访问的商品 42 的索引页就可能被大量一次性读取挤出缓存。

这时，我们想保护的不是“最近新读入的页面”，而是**值得长期保留的热门页面**。因此 InnoDB 采用区别对待新读入页面和被反复访问页面的方式，减少一次性扫描对缓存的冲击。

源码中可以从 `buf_LRU_add_block()`、`buf_LRU_make_block_young()` 等入口继续研究这些行为。当前不展开它们全部的边界与条件，因为本章首先要回答的是“为什么页面会被缓存、如何命中”。

若要单独深入扫描对缓存的影响，可以参考官方文档：[Making the Buffer Pool Scan Resistant](https://dev.mysql.com/doc/refman/8.4/en/innodb-performance-midpoint_insertion.html)。

---

## 7. 在真实 MySQL 中观察缓存的作用

教学程序能够准确统计我们编写的 `get()` 和文件读取调用，但它不能替代真实 MySQL 的观测。现在，我们回到同一个商品表。

假设已按前文建立：

```sql
USE mysql_source_lab;

SELECT stock
FROM products
WHERE id = 42;
```

为了观察 Buffer Pool，不必先执行 UPDATE。我们仍然沿着同一条购买请求的“定位商品 42”阶段，只做重复的主键读取，不改变库存。

MySQL 提供了两个有用的全局状态计数：

```sql
SHOW GLOBAL STATUS
WHERE Variable_name IN (
  'Innodb_buffer_pool_read_requests',
  'Innodb_buffer_pool_reads'
);
```

它们含义不同：

- `Innodb_buffer_pool_read_requests`：逻辑页面读取请求次数。
- `Innodb_buffer_pool_reads`：未能由 Buffer Pool 满足、需要从存储读取的逻辑请求次数。

注意，**两个数不是“查了几次 SQL”**。一次主键查询可能访问多个页面，并且内部执行过程还涉及其他操作。

可以在专用测试实例中做一个小实验：

1. 记录上面两个全局计数。
2. 多次执行 `SELECT stock FROM products WHERE id = 42;`。
3. 再记录计数，计算前后差值。

如果相关索引页已经被缓存，重复查询仍然会产生逻辑访问，但不一定需要再次读取存储中的页面。

这里不建议用“第二次一定比第一次快多少毫秒”作为验证标准。SQL 解析、连接调度、操作系统页缓存以及服务器中其他活动，都可能影响耗时。

更不能假设这两个全局计数在自己的查询期间只会被一个客户端修改。它们会受到其他连接和后台活动影响。因此建议在**独立实验实例**、尽可能没有其他负载时观察趋势。

还要注意：对于只有一条记录的表，相关页很可能在实验开始前就已经驻留 Buffer Pool。因此“第一次 SQL 必然带来一次磁盘读”并不是可靠结论。

官方对这些计数的定义见：[MySQL 8.4 Server Status Variables](https://dev.mysql.com/doc/refman/8.4/en/server-status-variables.html)。

**验证状态：** 本章未在真实 MySQL 8.4.0 实例执行上述 SQL，不展示虚构计数或耗时；它们是给读者在本地独立验证的观察步骤。已实际运行的实验只有前面的教学页文件模型。

## 8. 三个容易混淆的边界

在结束之前，有必要澄清三个与当前主线直接相关的概念，不继续扩展新模块。

### 8.1 Buffer Pool 缓存的是页，不是 SQL 查询答案

商品 42 的 `SELECT` 结果可能只是一个整数 `100`，但 InnoDB 缓存的通常是包含索引信息和记录数据的**页面**。

下一次读取同一页面上的其他记录，也可能复用已缓存的页。Buffer Pool 并不等于“把 `SELECT stock ...` 的结果字符串保存起来”的查询结果缓存。

### 8.2 文件读取调用不等于物理磁盘 I/O

我们的 Python 实验中，`file_reads=4` 表示程序执行了四次文件读取路径。操作系统仍可能直接从自己的页缓存提供文件内容。

真实 MySQL 还有自己的 Buffer Pool；不同平台、文件系统以及 I/O 配置也会影响操作系统缓存的参与方式。

所以不能把“本章教学模型减少两次文件读取”直接换算成“真实机械磁盘减少两次寻道”或“查询提高 N 倍”。

### 8.3 缓存命中不意味着库存修改已经持久化

本章故意只实现读取。因为页面可以复用，并不代表数据已经被修改，更不代表事务已提交或者数据已安全落盘。

到这里，我们掌握的是：**已知页号，InnoDB 可以优先访问内存中的缓存页，必要时再从存储取页。**

当下一步对商品 42 执行真正的 UPDATE 时，内存中这个页面将发生变化。届时就必须解释：修改的是哪个副本？文件中的旧内容何时改变？为什么不能随意淘汰一个已被修改的页？

这才是下一章该引入的新问题。

## 9. 回到商品 42：这一层设计究竟增加了什么？

我们从上一章得到一个已经很有价值的设计：

```text
商品 ID → B+ 树根页 → 叶子页 → 商品记录
```

但它仍然没有回答：这些页面每次是从哪里取得的？

本章首先允许直接从文件读取，发现热门商品会不断重复请求相同页面。

于是，我们加入最简单的字典缓存。第一次读取时加载页面，下一次按页号直接复用。这个设计在空间充足时已经有效。

当缓存容量受限，我们又发现页面不能一直保留，因而增加了一个最小的替换策略，并在教学文件里实际观察到了命中、未命中和页面替换。

最后，沿 MySQL 8.4.0 源码验证，InnoDB 的确将这些动作放在页面获取层中：

```text
                    商品 42
                       │
                B+ 树选择页号
                       │
                       ▼
               Buffer Pool 查找
                  /         \
             缓存命中       缓存未命中
                │             │
                │          准备缓冲块
                │             │
                │         从存储读页
                │             │
                └───────┬─────┘
                        ▼
                   在页内找记录
```

这张图现在比上一章多出了 Buffer Pool，但它不是凭空添加的新模块，而是我们为了消除重复页面读取不得不引入的能力。

本章真正完成的事情是：**在不改变 B+ 树查找方式的前提下，让取页过程能够复用已加载的页面，并在容量不足时管理可用空间。**

至于库存扣减，我们还没有执行真实的页面修改。

现在，假设 Buffer Pool 中的叶子页 5 已经包含：

```text
(id=42, stock=100)
```

用户执行：

```sql
UPDATE products
SET stock = stock - 1
WHERE id = 42 AND stock > 0;
```

InnoDB 找到了这条记录，并准备把库存改成 `99`。

**是直接修改内存中的页，还是必须马上写回磁盘？如果允许内存和磁盘暂时不一致，怎样知道哪些页已经改变？**

这次我们不是为了讲一个新术语而制造问题，而是沿着同一次 UPDATE，真正走到了读取之后必须发生的下一步。

**下一章：第 07 章《从找到记录到修改记录——库存究竟在哪里改变？》**

---

## 源码与实验核对

**源码版本：** MySQL Server **8.4.0**，Git Tag **`mysql-8.4.0`**。本章涉及的函数名与关键调用已对照该标签的原始源码核对。

| 源码文件 | 本章关注的真实入口 | 作用 |
|---|---|---|
| [`storage/innobase/buf/buf0buf.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/buf/buf0buf.cc) | `buf_page_get_gen()` | 按页标识和获取模式取得页面 |
| 同上 | `Buf_fetch_normal::get()`、`Buf_fetch<T>::lookup()` | 优先查找缓存页；未命中进入读取流程 |
| 同上 | `Buf_fetch<T>::read_page()` | 通过 `buf_read_page()` 发起底层页面读取 |
| 同上 | `buf_page_init_for_read()` | 为读取准备缓冲页及缓冲块 |
| [`storage/innobase/buf/buf0lru.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/buf/buf0lru.cc) | `buf_LRU_get_free_block()` | 从空闲块或可回收的缓存块取得可用空间 |
| 同上 | `buf_LRU_add_block()`、`buf_LRU_make_block_young()` | 将页面纳入 LRU 管理，并按访问情况调整位置 |

官方文档：

- [Buffer Pool](https://dev.mysql.com/doc/refman/8.4/en/innodb-buffer-pool.html)
- [Making the Buffer Pool Scan Resistant](https://dev.mysql.com/doc/refman/8.4/en/innodb-performance-midpoint_insertion.html)
- [Server Status Variables](https://dev.mysql.com/doc/refman/8.4/en/server-status-variables.html)

**已实际执行：** Python 教学页文件实验。首次查询 2 次文件读取，重复查询后累计仍为 2 次；查询另一商品并返回后累计 4 次文件读取，替换页顺序为 `[5, 0]`。这是教学模型的实际输出，不代表 InnoDB 内部页面读取计数。

**已核对源码：** `buf_page_get_gen()` 的普通获取分支、`Buf_fetch_normal::get()` 的先查后读、`lookup()` 的页哈希定位、`read_page()` 的底层读调用，以及 `buf_page_init_for_read()` 与 `buf_LRU_get_free_block()` 的空间准备关系。

**未执行：** MySQL Server 8.4.0 上的 SQL 状态变量实验，以及带调试符号的 C++ 调用追踪。本文不声称已经观测到真实服务器中的商品 42 页号、缓存命中次数或精确磁盘 I/O 次数。
