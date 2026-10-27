---
title: "07｜从找到记录到修改记录：库存究竟在哪里改变？"
sidebar_position: 8
slug: /mysql/07-how-update-modifies-page
description: "承接 Buffer Pool 和 B+ 树的查找过程，从商品 42 的一次 UPDATE 出发，追踪 InnoDB 如何修改内存中的记录，并将被修改的页面纳入后续刷盘管理。"
tags: [MySQL, InnoDB, Buffer Pool, UPDATE, 源码精读]
---

# 从找到记录到修改记录：库存究竟在哪里改变？

上一章，我们沿着商品 `42` 的索引查找过程，看到 InnoDB 如何通过 Buffer Pool 复用已经读入内存的页面。

现在，先假设包含商品 `42` 的页面已经在 Buffer Pool 中，索引搜索也已经找到了相应记录。用户仍然执行同一条 SQL：

```sql
UPDATE products
SET stock = stock - 1
WHERE id = 42 AND stock > 0;
```

商品库存原来是 `100`。我们希望它变成 `99`。

找到记录之前，需要解决的是“去哪儿读”。找到记录之后，问题却变成了：**新值究竟写在哪里？**

是不是执行 `UPDATE` 就立刻找到磁盘文件，把磁盘上的 `100` 改成 `99`？还是先改 Buffer Pool 中的记录，之后再处理磁盘？

这章只跟踪一次更新中最容易观察的一条路径：**已经定位到商品记录，修改记录内容，再让系统知道这个页面需要在未来写回磁盘。**

锁、Undo、Redo、事务提交都已经参与真实 MySQL 的更新过程，但我们只在必须说明行为边界时提到它们，不在本章提前拆开研究。

## 1. 最简单的想法：找到商品后，直接改内存

回到这个系列最开始的 Python 程序：

```python
products = {42: {"stock": 100}}

product = products[42]
if product["stock"] > 0:
    product["stock"] -= 1

assert products[42]["stock"] == 99
```

这段代码很好理解：`products[42]` 定位商品，减一直接修改内存中的字典。

刚才我们已经知道，在 InnoDB 里，商品记录所在的页面也可能已经缓存于内存。于是一个很自然的想法是：**能否像修改字典一样，直接修改缓存页面中的记录？**

可以。这也是我们理解数据库记录更新的合理起点。

但多了一个问题：字典没有对应的磁盘副本，而 Buffer Pool 中的页面有。

假设页面最初从文件读取到内存：

```text
磁盘中的商品页                 Buffer Pool 中的商品页
┌────────────────┐          ┌────────────────┐
│ id=42 stock=100│  读取    │ id=42 stock=100│
└────────────────┘ ───────→ └────────────────┘
```

如果这时在内存页面中把库存改成 `99`：

```text
磁盘中的商品页                 Buffer Pool 中的商品页
┌────────────────┐          ┌────────────────┐
│ id=42 stock=100│          │ id=42 stock=99 │
└────────────────┘          └────────────────┘
```

两份内容暂时不同。这不是笔误，而是**缓存修改与磁盘页面写回分离**带来的正常情况。

问题是：如果系统不记住哪些页面被改过，日后要写回磁盘时，就分不清哪些是旧内容、哪些是新内容。

于是，我们现在真正需要的新能力，不是立刻把每次修改写回磁盘，而是：

**在修改缓存页面后，记录“这个页面还有尚未写回数据文件的变更”。**

这样的页面通常称为**脏页（dirty page）**。

术语只需要记到这里：脏页不是“数据坏了”，而是指页面在内存中发生了修改，相关变更尚未全部写入数据文件。

## 2. 先做一个能看见差异的最小实验

为了明确区分“改了内存”和“改了磁盘”，我们先不用 MySQL，实现一个非常小的按页存储模型。

它只有一个页面，页面中只有我们关心的商品 `42`。为了让实验可重复，页面大小选用 `4096` 字节；**这只是教学选择，不代表 InnoDB 默认页大小（默认通常为 16 KiB）**。

文件中约定从第 128 字节开始存放两个整数：商品 ID 和库存。

```python
import os
import struct
import tempfile
from pathlib import Path

PAGE_SIZE = 4096           # 教学页，不是 InnoDB 页格式
RECORD_OFFSET = 128        # 仅用于本实验
RECORD = struct.Struct("<II")  # 商品 ID、库存


class MiniPageStore:
    def __init__(self, filepath):
        self.path = Path(filepath)

        data = bytearray(PAGE_SIZE)
        RECORD.pack_into(data, RECORD_OFFSET, 42, 100)
        self.path.write_bytes(data)

        # 模拟“从文件读取页面到内存缓存”
        self.page = bytearray(self.path.read_bytes())
        self.dirty = False

    def cache_stock(self):
        product_id, stock = RECORD.unpack_from(
            self.page, RECORD_OFFSET
        )
        assert product_id == 42
        return stock

    def disk_stock(self):
        # 直接读取文件，不经过 self.page
        data = self.path.read_bytes()
        product_id, stock = RECORD.unpack_from(data, RECORD_OFFSET)
        assert product_id == 42
        return stock

    def buy(self):
        stock = self.cache_stock()
        if stock <= 0:
            return False

        RECORD.pack_into(self.page, RECORD_OFFSET, 42, stock - 1)
        self.dirty = True
        return True

    def flush_page(self):
        if not self.dirty:
            return

        with self.path.open("r+b") as f:
            f.seek(0)
            f.write(self.page)
            f.flush()
            os.fsync(f.fileno())

        self.dirty = False


with tempfile.TemporaryDirectory() as directory:
    store = MiniPageStore(Path(directory) / "products.page")

    print(
        f"最初: 内存={store.cache_stock()}, "
        f"文件={store.disk_stock()}, dirty={store.dirty}"
    )

    assert store.buy()
    print(
        f"扣减后: 内存={store.cache_stock()}, "
        f"文件={store.disk_stock()}, dirty={store.dirty}"
    )

    assert store.cache_stock() == 99
    assert store.disk_stock() == 100

    store.flush_page()
    print(
        f"写回后: 内存={store.cache_stock()}, "
        f"文件={store.disk_stock()}, dirty={store.dirty}"
    )

    assert store.disk_stock() == 99
```

**实际运行输出：**

```text
最初: 内存=100, 文件=100, dirty=False
扣减后: 内存=99, 文件=100, dirty=True
写回后: 内存=99, 文件=99, dirty=False
```

这个实验中最值得注意的不是最后一行，而是第二行：

```text
扣减后: 内存=99, 文件=100, dirty=True
```

此时，商品库存的最新值已经存在于内存页面中，但文件还保留旧值。只有在我们显式调用 `flush_page()` 后，磁盘文件才更新。

我们还用断言确认了这不是单纯的打印效果。

这个实验只建立三个概念的直觉：**缓存页面、页面修改、脏页写回**。它没有实现 MySQL 的事务、锁、Redo、Undo 或安全的崩溃恢复协议。调用 `fsync()` 也不能使这个教学系统自动具有数据库的事务保证。

现在可以进入真实源码，寻找对应的记录修改动作。

## 3. 真实 InnoDB 修改的不是 Python 字典

本系列固定使用：

- 仓库：[`mysql/mysql-server`](https://github.com/mysql/mysql-server)
- 版本：MySQL 8.4.0
- Git Tag：[`mysql-8.4.0`](https://github.com/mysql/mysql-server/tree/mysql-8.4.0)

上一章的查找路径最终把我们带到 InnoDB 索引中的目标记录。对本次商品更新来说，接下来需要把 `stock` 的新值写入实际的记录表示。

但在真实 InnoDB 内部，不会直接出现这样一行代码：

```python
products[42]["stock"] = 99
```

因为 InnoDB 管理的是页面中的物理记录布局，而不是 Python 对象；它也不应该要求页面修改函数重新理解 `SET stock = stock - 1` 这段 SQL 文本。

于是产生了一个小小的接口问题：

**负责执行 SQL 的上层，怎样告诉负责修改记录的底层：哪些字段应该替换成什么值？**

真实代码会使用更新信息结构，来表达要修改的字段和值。这里我们只需要认识 `upd_t`：它代表传给 InnoDB 行更新代码的更新向量，并不等同于 SQL 原文。

与之对应的物理记录，使用 `rec_t` 一类内部表示。我们现在只需知道：它关联的是 InnoDB 索引页面中的记录，不是客户端接收到的结果行。

接下来找到负责**在字段长度不变等条件下原地替换记录值**的函数：

[`storage/innobase/row/row0upd.cc` — `row_upd_rec_in_place()`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/row/row0upd.cc)

函数签名的关键参数如下：

```cpp
void row_upd_rec_in_place(
    rec_t *rec,
    const dict_index_t *index,
    const ulint *offsets,
    const upd_t *update,
    page_zip_des_t *page_zip);
```

这几个参数恰好回答了本章必须知道的事情：

| 参数 | 当前需要理解的作用 |
| --- | --- |
| `rec` | 要更新的物理记录，作为输入也会被修改 |
| `index` | 该记录所属的索引信息，用于解释记录布局 |
| `offsets` | 字段在记录中的偏移等布局信息 |
| `update` | 哪些字段要换成哪些新值 |
| `page_zip` | 压缩页面相关的信息；普通未压缩页路径不需要深入 |

特别注意，这个函数不是“无条件处理每条 UPDATE”的万能入口。InnoDB 还需要根据字段尺寸、索引维护和其他约束选择不同的记录更新路径。我们选择它，是因为它足够小，可以直接观察**记录内容真正改变的动作**。

### 3.1 真正的写入发生在哪里？

**真实源码节选，MySQL 8.4.0，`row_upd_rec_in_place()`：**

```cpp
n_fields = upd_get_n_fields(update);
```

函数先获得这次更新向量中的字段数量。接下来遍历待更新的字段，读取新值。

在处理某个字段时，关键写入调用是：

```cpp
rec_set_nth_field(index, rec, offsets, field_no,
                  dfield_get_data(new_val), dfield_get_len(new_val));
```

这一行有明确的输入和副作用：

- `field_no` 告诉它要改哪一个**物理记录字段**。不能简单认为它永远等于 SQL 表中字段的序号。
- `dfield_get_data(new_val)` 提供新字段内容。
- `dfield_get_len(new_val)` 提供新字段内容的长度。
- `rec_set_nth_field(...)` 把相应内容写入记录表示。

这次，我们不是只看到“函数调用了另一个函数”，而是看到了底层**真正替换字段内容**所需要的动作。

对商品 `42` 的最小教学路径，可以先这样理解：

```text
已定位到商品 42 的聚簇索引记录
               │
               ▼
上层计算这次库存更新应写入的新值
               │
               ▼
InnoDB 获得记录、字段布局与更新向量
               │
               ▼
rec_set_nth_field(...)
               │
               ▼
页面内对应记录的库存值发生变化
```

**这是一条教学化简的数据流，不是逐函数完整调用栈。**

还要避免两个误解。

第一，`stock = stock - 1` 这段 SQL 表达式不是由 `rec_set_nth_field()` 重新解析和计算的；底层接收到的已是准备好的字段更新信息。

第二，修改字段并不意味着事务已经提交。真实更新路径还包含 Undo、锁、日志和索引维护等工作。这些并没有因为我们把讲解聚焦在一行代码而消失。

### 3.2 为什么这里只选择“原地更新”路径？

我们的 `stock` 是 `INT` 字段，从 `100` 变成 `99` 不改变存储该字段所需的固定长度。

因此，它适合帮助我们理解“无需因为这个字段的新值而改变记录尺寸”的更新情形。

但如果更新的是一个长字符串，原来的字段内容是 `Keyboard`，后来变成一段更长的文本，记录尺寸可能改变。InnoDB 就需要面对页面空间、记录重组甚至不同更新策略的问题。

这不是本章必须解决的困难。

现在，我们只需记住：**在符合原地更新条件的路径中，物理记录内容能够直接在页面中被替换；并不是每次 SQL 更新都需要重建整个数据页。**

不过，仅仅修改了记录内容还不够。回忆 Python 实验：内存已经是 `99`，磁盘文件可能仍然是 `100`。

真实 InnoDB 必须知道，哪些缓存页面发生过修改，之后需要被写回。

## 4. 修改完成后，系统怎样记住这个脏页？

如果我们只维护一个普通字典：

```python
cache[page_id] = page_bytes
```

它最多能帮助我们找到页面，却不能告诉后台写盘逻辑“当前哪些页包含未写回的数据”。

我们的 Python 实验通过：

```python
self.dirty = True
```

解决了只有一个页面的情况。

但真实 InnoDB 要同时管理大量页面，而且多个操作可能连续修改同一个页面。因此，不能简单地为所有页面加一个全局布尔变量。

现在才需要理解：**脏页管理不只是状态判断，还需要把有待写回的页面组织起来。**

MySQL 8.4.0 中，我们可以从下面的源码入口观察这一点：

[`storage/innobase/mtr/mtr0mtr.cc` — `mtr_t::Command::add_dirty_blocks_to_flush_list()`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/mtr/mtr0mtr.cc)

**真实源码节选：**

```cpp
Add_dirty_blocks_to_flush_list add_to_flush(start_lsn, end_lsn,
                                           m_impl->m_flush_observer);
```

随后，它遍历本次操作涉及的记录项，把符合条件的已修改页面加入后续刷盘管理：

```cpp
m_impl->m_memo.for_each_block_in_reverse(iterator);
```

这里先理解一个最小的新术语：**mini-transaction（mtr）**。它是 InnoDB 内部协调一组低层页面操作、锁存与相关日志动作的机制，**不是用户执行的 `BEGIN ... COMMIT` 事务**。

这个区别十分重要。我们看到的函数负责低层页面修改相关的工作，并不表示业务购买事务已经提交成功。

继续看同文件的 `mtr_t::Command::execute()`，它在正常生成 Redo 的路径中，把相关操作日志写入日志缓冲后，会调用：

```cpp
add_dirty_blocks_to_flush_list(handle.start_lsn, handle.end_lsn);
```

这表明页面写入与后续刷盘管理之间有明确的协调关系。

暂时只需要知道这里的 `start_lsn`、`end_lsn` 是描述相应日志位置范围的信息。它们为什么能帮助恢复、以及日志写入和持久化的条件，将留到研究 Redo Log 时再解释。

现在可以把本章看到的两类职责放在一起：

```text
记录更新路径
    │
    ├─ 替换索引记录中的字段内容
    │
    └─ 对涉及的页面记录修改信息
                │
                ▼
       纳入后续脏页刷盘管理
                │
                ▼
        之后在合适时机写回
```

图的最后一步表示页面刷盘是**后续**可能发生的工作，不表示当前 `UPDATE` 调用栈立即执行了磁盘页面写入。

源码证实了：页面内容修改与将页面纳入刷盘管理，是可以分开识别的动作。

这正是我们在教学模型中写下 `self.dirty = True` 时想表达的设计需求。

## 5. 一条 SQL 的成功结果，能证明物理页面已经落盘吗？

回到真实 MySQL：

```sql
USE mysql_source_lab;

UPDATE products
SET stock = 100
WHERE id = 42;

SET SESSION autocommit = 1;

UPDATE products
SET stock = stock - 1
WHERE id = 42 AND stock > 0;

SELECT ROW_COUNT() AS affected_rows;
SELECT stock FROM products WHERE id = 42;
```

在已有商品 `42`、更新正常完成的前提下，预期影响行数为 `1`，最终查询到的库存为 `99`。

但**这个 SQL 结果不能单独证明包含该记录的数据页已经写回数据文件**。

数据库可以让后续查询看到新值，而相应数据页仍然留在 Buffer Pool 中等待刷盘。这里“新值可见”和“数据页已经写回”是两个不同的事实。

如果希望进一步观察 Buffer Pool，可以查看：

```sql
SHOW GLOBAL STATUS LIKE 'Innodb_buffer_pool_pages_dirty';
```

这个状态变量表示当前 Buffer Pool 中脏页的数量。也可以查看：

```sql
SHOW ENGINE INNODB STATUS\G
```

重点关注 `BUFFER POOL AND MEMORY` 部分中的 `Modified db pages`。

不过，这些都是**实例级、动态变化的指标**。其他连接和后台线程也可能改变它们。尤其是后台刷盘随时可能发生，因此执行一条 UPDATE 后，脏页数量不一定恰好增加一页，也不一定维持不变。

请不要使用“指标增加 1”作为本章成立的必要断言，更不要把脏页数变化直接当作这条 SQL 物理页面写入时间的证据。

**实验状态说明：** 前面的 Python 页面实验已经实际执行并通过断言；本节 MySQL SQL 和监控命令提供了可复现步骤，但本章没有实际连接 MySQL 8.4.0 实例，也未声称获得了实际服务器的监控输出。

## 6. 为什么不在每次更新时立即写回页面？

到这里，读者可能会问：如果立即写回文件，我们岂不是连“脏页”都不需要了？

先承认这个方案在简单场景中可以工作。

例如，我们的教学程序完全可以把：

```python
store.buy()
store.flush_page()
```

固定放在一起。每次库存减一，就立即写回整个页面。

但是假设商品 `42` 很热门，一秒钟内连续发生多次购买。页面可能被改成：

```text
100 → 99 → 98 → 97 → 96
```

如果每次都要求立刻写回整个数据页，至少会产生多次页面写入工作。

如果把改动先留在内存中，之后在安全、合适的条件下一次写回，便有机会合并同一页面上的多次修改，减少不必要的数据页 I/O。

因此，**把页面修改和页面刷盘分离，本身就具有性能价值**。

但这绝不是没有代价的设计。

我们刚刚制造了一个更加严重的问题：

> 如果 Buffer Pool 中库存已经从 100 变成 99，数据页还没写回磁盘，MySQL 就突然崩溃，那么怎么办？

这是一个真正的故障恢复问题。

真实 InnoDB 并不是只修改页面、标记 dirty 就把成功结果交给客户端。它还需要依靠事务与日志机制来处理持久性和恢复；脏页标记**不能替代**这些机制。

我们之前已经在 `mtr_t::Command::execute()` 里看到日志缓冲与脏页管理之间存在协调关系。但现在读者还没有经历需要 Redo 来解决的具体崩溃实验，因此这里不展开其内部协议。

在系列后面的持久化阶段，我们会专门构造“事务已提交、页面尚未写回、系统发生故障”的问题，再追踪 Redo Log 的真实实现。

## 7. 回到这次 UPDATE：三个时间点不要混淆

现在，把整个章节放回同一条 SQL：

```sql
UPDATE products
SET stock = stock - 1
WHERE id = 42 AND stock > 0;
```

上一步，我们已经通过 B+ 树及 Buffer Pool 找到记录所在的页面。

本章继续认识了三个不同的时间点：

**第一：记录被修改。** 在符合条件的底层更新路径中，新字段值写进索引页面中的物理记录。`row_upd_rec_in_place()` 中的 `rec_set_nth_field(...)` 为此提供了直接的源码证据。

**第二：页面进入脏页管理。** InnoDB 需要跟踪哪些页面包含尚未写回数据文件的变更。`mtr_t::Command::add_dirty_blocks_to_flush_list()` 体现了内部操作如何将相关页面纳入刷盘管理。

**第三：事务完成、页面写回。** 这不是一个动作，也未必同时发生。事务提交属于更高层的事务语义；脏页写回属于页面持久化过程。两者需要相应机制协调，但不能互相替代。

把这三个时间点混成“UPDATE 一成功，磁盘页就立刻改好了”，会让后续的 Redo Log、Checkpoint 和崩溃恢复全部变得难以理解。

我们还需区分两个名称相似但含义不同的东西：**行锁（lock）**与页面**锁存（latch）**。这章没有展开它们。真实记录更新中二者可能各有职责，不能因为源码出现了页面 latch，就把它当作业务事务持有的行锁。下一章研究并发购买时，我们再处理事务间冲突。

## 8. 下一章：两个人同时购买最后一件商品

到目前为止，我们只研究了一个用户的正常库存扣减。

用户提交 UPDATE，数据库找到商品 `42`，将库存从 `100` 改为 `99`；修改的页面可能先停留在内存中，等待后续刷盘。

如果只有一个购买请求，这条理解路径已经足够帮助我们进入下一阶段。

但真实电商系统里，可能有两个用户同时购买，而且商品只剩最后一件。

最简单的应用代码会怎样处理？

```python
if read_stock(42) > 0:
    write_stock(42, read_stock(42) - 1)
```

假如两个请求都读到 `1`，它们可能各自认为购买条件成立。这时问题不再是页面存在哪里，而是：**多个操作对同一条记录的读取和修改，应该怎样协调？**

下一章，我们会先让这个简单写法在一个可观察的并发调度中暴露问题，再研究 MySQL 怎样协调对同一条商品记录的并发 UPDATE。

**下一章：第 08 章《两个用户购买最后一件商品——为什么需要锁？》**

---

## 本章源码与资料

本章研究范围固定为 **MySQL 8.4.0 / `mysql-8.4.0`**。代码引用只保留支持当前推论的极短片段。

| 位置 | 当前使用它回答的问题 |
| --- | --- |
| [`storage/innobase/row/row0upd.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/row/row0upd.cc) | `row_upd_rec_in_place()` 如何将新字段值写入物理记录？ |
| [`storage/innobase/btr/btr0cur.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/btr/btr0cur.cc) | B+ 树记录更新入口负责什么？它与原地记录修改有什么关系？ |
| [`storage/innobase/mtr/mtr0mtr.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/mtr/mtr0mtr.cc) | 低层页面修改如何与日志、脏页刷盘管理协调？ |
| [MySQL 8.4 — Buffer Pool](https://dev.mysql.com/doc/refman/8.4/en/innodb-buffer-pool.html) | 为什么页面缓存于内存？怎样理解 `Modified db pages`？ |
| [MySQL 8.4 — Buffer Pool Flushing](https://dev.mysql.com/doc/refman/8.4/en/innodb-buffer-pool-flushing.html) | 为什么脏页允许之后再写回？ |

**阅读边界：** 本章只确认“一条记录在缓存页面内被修改”及“修改页面会纳入刷盘管理”的机制。没有把内存更新简化为完整的 InnoDB 事务更新，也没有声称教学模型具备 MySQL 的崩溃恢复能力。
