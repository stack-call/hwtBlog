---
title: "04｜从内存记录到磁盘页面：为什么数据不能随意摆放？"
sidebar_position: 5
slug: /mysql/04-why-pages
description: "沿着商品 42 的库存扣减，使用可运行的页文件模型与 MySQL 8.4.0 真实源码，理解 InnoDB 为什么把数据组织成 Page。"
tags: [MySQL, InnoDB, Page, 源码精读]
---

# 从内存记录到磁盘页面：为什么数据不能随意摆放？

上一章，我们继续追踪了这条库存扣减：

```sql
UPDATE products
SET stock = stock - 1
WHERE id = 42 AND stock > 0;
```

在 SQL Server 层确定需要读取商品记录之后，执行过程可以选择扫描或基于索引的访问路径。问题并没有因此结束：**无论采用哪条路径，InnoDB 最终必须取得承载这条记录的数据。**

之前的 Python 实验把商品放在列表或字典里：

```python
products = {42: {"stock": 100}}
```

程序可以直接访问内存对象。但真实数据库不仅需要在运行时使用数据，还要把数据保存到持久化存储中。于是，问题变成了：

> 如果商品 42 存放在磁盘上的文件中，我们怎样找到、读取并修改它？

这一章只解决**磁盘上的数据应该如何组织成可以寻址、可以读取、可以修改的单位**。我们先不研究多层 B+ 树，也不研究 Buffer Pool 的淘汰策略；它们要在新的限制出现时再登场。

## 1. 最简单的方案：每条商品记录写成一行文本

最容易想到的持久化方法是写文件。例如：

```text
7,20
19,35
42,100
80,50
```

其中 `42,100` 表示商品 42 的库存是 100。

读取时，我们可以逐行解析，直到找到商品 42；修改时，把它改成 `42,99`。

这确实能完成最初的购买任务。如果只有四条记录，完全没必要引入更复杂的设计。

但现在增加一个实际条件：假设商品数量增长到一千万条。即便我们通过上一章的查找方法知道商品 42 大概位于哪个位置，也仍然需要回答：**它在文件中的哪个字节？**

对于不等长的文本记录，这并不容易直接计算。商品名称长短不同、字段长度变化，前面一条记录发生修改，还可能改变后面记录的文件位置。

当然，也可以给每条记录建立独立的偏移量索引，或者每次修改时重新整理文件。但这表明我们真正缺少的不是一个更复杂的 `for` 循环，而是**稳定的数据存放和寻址规则**。

先尝试一个很小的改进：不再把所有记录看成一条无限延伸的字节流，而是把文件划分为固定大小的块。

## 2. 让文件变成一页一页的空间

为了便于手算，我们先假设每块只有 **64 字节**，并规定：

- 每块前 8 字节用于保存块编号和记录数；
- 每条商品记录暂时只包含两个 4 字节整数：`id` 和 `stock`；
- 余下空间存放记录，一块装满后使用下一块。

那么，一块最多能存放：

```text
(64 - 8) / 8 = 7 条记录
```

假如现在有 15 件商品，文件可以变成：

```text
products.pages （教学文件，不是 InnoDB 文件）

┌──────── 第 0 块：64 B ────────┐
│ 块号、记录数：8 B            │
│ 商品记录 × 7：56 B           │
└──────────────────────────────┘
┌──────── 第 1 块：64 B ────────┐
│ 块号、记录数：8 B            │
│ 商品记录 × 7：56 B           │
└──────────────────────────────┘
┌──────── 第 2 块：64 B ────────┐
│ 块号、记录数：8 B            │
│ 商品记录 × 1：8 B            │
│ 剩余空间                     │
└──────────────────────────────┘
```

这样做并没有自动解决“商品 42 究竟在哪一块”的问题，但解决了另一个明确的问题：**一旦知道块号，我们就能确定从文件哪里开始读取。**

例如，在这个极简、单文件、固定 64 字节块的模型中：

```python
file_offset = page_no * 64
```

要读取第 2 块，就从文件偏移 `128` 字节处读取 `64` 字节。

这时才给这种块起一个名字：**Page，页**。

数据库不再只管理抽象的“第 42 条商品记录”，而是可以先管理一个个可定位的页面，再在页面内部寻找具体记录。

不过，请注意：这里的 **64 字节是教学参数**，不是 InnoDB 的真实页面大小；上面的文件格式也不是 MySQL 数据文件格式。

## 3. 真正运行一次：把 15 件商品写进三个页

下面的 Python 代码可以直接复制运行，不需要安装 MySQL，也不需要额外依赖。

它不假装自己是 B+ 树：我们还不知道商品 42 在哪个页面，因此仍然逐页扫描。这样，便能把“页解决了什么”和“页还没有解决什么”分清楚。

**教学化简代码（非 MySQL 源码）：**

```python
from pathlib import Path
from tempfile import TemporaryDirectory
import struct

PAGE_SIZE = 64                       # 仅为教学演示
HEADER = struct.Struct(">II")       # 页号、记录数
ROW = struct.Struct(">II")          # 商品 ID、库存
CAPACITY = (PAGE_SIZE - HEADER.size) // ROW.size


def encode_page(page_no, rows):
    if len(rows) > CAPACITY:
        raise ValueError("page full")

    raw = bytearray(PAGE_SIZE)
    HEADER.pack_into(raw, 0, page_no, len(rows))

    for slot, row in enumerate(rows):
        ROW.pack_into(raw, HEADER.size + slot * ROW.size, *row)

    return raw


def build_file(path, rows):
    with path.open("wb") as f:
        for page_no, start in enumerate(range(0, len(rows), CAPACITY)):
            page_rows = rows[start:start + CAPACITY]
            f.write(encode_page(page_no, page_rows))


def scan_file(path, wanted):
    pages_read = 0

    with path.open("rb") as f:
        while chunk := f.read(PAGE_SIZE):
            if len(chunk) != PAGE_SIZE:
                raise ValueError("truncated page")

            pages_read += 1
            page_no, count = HEADER.unpack_from(chunk, 0)

            for slot in range(count):
                row_id, stock = ROW.unpack_from(
                    chunk, HEADER.size + slot * ROW.size
                )
                if row_id == wanted:
                    return stock, page_no, slot, pages_read

    return None


rows = [(i, 100) for i in range(28, 43)]  # 15 条，最后一条是商品 42

with TemporaryDirectory() as tmp:
    path = Path(tmp) / "products.pages"
    build_file(path, rows)

    print("每页最多记录:", CAPACITY)
    print("文件字节数:", path.stat().st_size)
    print("查找结果:", scan_file(path, 42))
```

**这段 Python 已实际运行，输出为：**

```text
每页最多记录: 7
文件字节数: 192
查找结果: (100, 2, 0, 3)
```

输出 `(100, 2, 0, 3)` 的含义是：

- 找到的库存是 `100`；
- 目标位于第 `2` 页；
- 它是该页的第 `0` 条商品记录；
- 本次扫描读取了 `3` 个教学页面。

我们亲眼看到，15 条记录被写进了 `3 × 64 = 192` 字节的文件。商品 42 的位置不再只是模糊的“文件后面某一行”，而是可以用**页号 + 页内位置**描述。

假设稍后已经知道商品 42 位于 `(page_no=2, slot=0)`，就可以先读取第 2 页，再计算它在页面中的位置。修改 `stock` 时，也能够定位对应的数据字节。

这里的固定长度记录很简单；真实数据库还要管理变长字段、记录删除、空闲空间和页内顺序。但那些复杂性还不是本章需要一次性引入的。

现在，最关键的进展是：**我们得到了一种可以独立寻址、独立读取、独立组织内部记录的存储单位。**

那么，真实 InnoDB 是否也这样组织数据？

---

## 4. 回到 InnoDB：它的页有多大？

本章源码固定为：

- 仓库：[`mysql/mysql-server`](https://github.com/mysql/mysql-server)
- 版本标签：[`mysql-8.4.0`](https://github.com/mysql/mysql-server/tree/mysql-8.4.0)
- 存储引擎：InnoDB

MySQL 8.4 的官方文档明确指出：InnoDB 表空间由数据页组成，**默认页大小是 16 KiB，即 16,384 字节**；实例初始化时也可以选择 4、8、32 或 64 KiB，之后不能在原实例上随意更改。[官方文档：File Space Management](https://dev.mysql.com/doc/refman/8.4/en/innodb-file-space.html)、[innodb_page_size](https://dev.mysql.com/doc/refman/8.4/en/innodb-parameters.html)。

先在自己的 MySQL 中检查这一点：

```sql
SELECT @@GLOBAL.innodb_page_size AS page_size_bytes;
```

如果实例使用默认配置，**预期**看到：

```text
+-----------------+
| page_size_bytes |
+-----------------+
|           16384 |
+-----------------+
```

这段 SQL 本文尚未在真实 MySQL 8.4 实例上执行，所以这里只给出默认配置下的预期值，不能据此断言你本地的实例一定是 16 KiB。

现在，我们不再把“页”当成一个仅用于解释的比喻。继续去真实源码中寻找页的身份信息。

## 5. 一页数据，为什么必须知道“自己是谁”？

回到刚才的教学文件，第 2 页可以通过 `page_no=2` 定位。

但在 MySQL 中，数据库可能有很多表空间，只有“第 2 页”还不够：不同表空间都可能有自己的第 2 页。

因此，读取一个页面时，至少需要区分：**哪个表空间？这个表空间中的第几页？**

我们在 MySQL 8.4.0 的 [`storage/innobase/include/buf0types.h`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/include/buf0types.h) 中，可以找到下面的类型。

**真实源码节选：`page_id_t`（仅截取构造函数与访问器）：**

```cpp
page_id_t(space_id_t space, page_no_t page_no)
    : m_space(space), m_page_no(page_no) {}

inline space_id_t space() const { return (m_space); }
inline page_no_t page_no() const { return (m_page_no); }
```

现在第一次出现 `space_id_t`，但它并不神秘：它只是用来标识表空间的类型。

`page_no_t` 则用于表示页面在对应表空间中的页号。

这一对值合在一起，才能标识 InnoDB 的一个页面。

例如：

```text
(space=17, page=2)
(space=23, page=2)
```

这是两个不同的页面，即使页号相同。这里的 `17` 和 `23` 只是教学示意值，不是我们的 `products` 表实际表空间 ID。

**它为什么存在？** 后续代码需要请求或管理一个具体页面，不能只提供商品 ID，也不能只提供一个没有表空间归属的页号。

**谁消费它？** 例如，InnoDB 的页面访问接口会接收 `page_id_t`，决定使用哪个页面。我们马上去源码验证。

### 5.1 如何向 InnoDB 请求一个页面？

打开 [`storage/innobase/include/buf0buf.h`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/include/buf0buf.h)，可以找到：

**真实源码节选：`buf_page_get()` 的关键调用：**

```cpp
return buf_page_get_gen(id, size, latch, nullptr, Page_fetch::NORMAL,
                        location, mtr);
```

其上层 `buf_page_get(...)` 接受页面 ID、页大小、锁存要求、调用位置以及 mini-transaction 上下文，返回相应的缓冲页块指针。

这一段证明：InnoDB 的底层页面访问并不是“给我商品 42 这四个字节”，而是围绕**已知身份的页面**展开。

但是要特别注意两个边界：

1. `buf_page_get()` 属于 InnoDB 页面访问/缓存层接口，并不等于每次调用都会发生磁盘读取。如果页面已经在内存中，就不必重复从磁盘读取。这里我们只是观察接口所接收的存储单位；缓存命中与替换机制留到第 06 章。
2. 这段接口是**页面读取这一层的真实源码证据**，不是宣称上一章的 `update_single_table()` 会直接调用它。UPDATE 与页面读取之间还有索引搜索等中间环节，我们会在下一章沿着索引结构补上。

也就是说，到这里，我们建立了一条可信的联系：

```text
一次库存 UPDATE
       |
       v
需要取得商品 42 的记录
       |
       v
记录所在的页面必须被访问
       |
       v
页面可以通过 (space_id, page_no) 标识
```

**尚未回答的恰恰是中间那步：如何从商品 ID 找到它所在的页面？** 这将自然引出下一章的 B+ 树。

---

## 6. 页面并非一整块裸数据：真实页头保存了什么？

我们的教学页面头部只保存两个整数：页号和记录数。

真实数据库的页面还要携带更丰富的信息，以便引擎识别、解释和管理它。

在 MySQL 8.4.0 的 [`storage/innobase/include/fil0types.h`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/include/fil0types.h) 中，有以下常量。

**真实源码节选：`fil0types.h`（选择与当前问题有关的字段）：**

```cpp
constexpr uint32_t FIL_PAGE_OFFSET = 4;
constexpr uint32_t FIL_PAGE_LSN = 16;
constexpr uint32_t FIL_PAGE_TYPE = 24;
constexpr uint32_t FIL_PAGE_ARCH_LOG_NO_OR_SPACE_ID = 34;
constexpr uint32_t FIL_PAGE_SPACE_ID = FIL_PAGE_ARCH_LOG_NO_OR_SPACE_ID;
constexpr uint32_t FIL_PAGE_DATA = 38;
```

这些常量描述的是**页内字节偏移**，不是函数调用，也不是商品记录的字段名。

我们先只关心其中三个：

- `FIL_PAGE_OFFSET = 4`：从页面起始处偏移 4 字节的位置，保存页号。
- `FIL_PAGE_TYPE = 24`：从页面起始处偏移 24 字节的位置，保存页面类型。
- `FIL_PAGE_SPACE_ID = 34`：保存该页面所属表空间的 ID。

为什么页面还要保存页类型？

因为同一个 InnoDB 表空间中，并不只是普通的商品记录页，还可能有管理用途的页面。引擎不能看到一段字节，就默认里面全是商品 `id` 和 `stock`。

为什么需要在页面自身保存标识？

因为从磁盘取得一段页面字节后，引擎有必要理解这段数据究竟属于哪个页面，并进行相应的检查和处理。

那 `FIL_PAGE_LSN` 呢？它记录的是页面有关的日志序列位置，后面研究数据修改与崩溃恢复时才需要详细讨论。此刻只要知道：真实页面除了记录，还有服务于数据库管理的元信息。

到这里先停下来，不必一次性记住整个页头布局。

### 6.1 商品记录数放在哪里？

外层页头解决了“这个页面是什么”的问题，但对于商品记录所在的**索引页**，还需要知道页内部有多少用户记录。

打开 [`storage/innobase/include/page0types.h`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/include/page0types.h)。

**真实源码节选：索引页内部页头的字段位置：**

```cpp
constexpr uint32_t PAGE_HEADER = FSEG_PAGE_DATA;
constexpr uint32_t PAGE_N_DIR_SLOTS = 0;
constexpr uint32_t PAGE_N_RECS = 16;
constexpr uint32_t PAGE_LEVEL = 26;
```

这些常量的一个关键区别是：`PAGE_N_RECS` 等字段偏移**相对于 `PAGE_HEADER`**；它们不是全都相对于文件起始处，也不是页起始处的绝对偏移。

`PAGE_N_RECS` 表示该索引页上的用户记录数。对于我们当前关心的商品记录，至少可以看出真实 InnoDB 的索引页也需要维护页内部的记录元数据。

`PAGE_LEVEL` 则表示页面位于索引树的哪一层；叶子层级为 0。现在只认识这个字段即可，不展开树的层级结构。

把已知信息拼起来，真实 InnoDB 索引页可以**概念性地**画成：

```text
┌──────────────────────────────────┐
│ File Header：页号、类型、表空间等 │
├──────────────────────────────────┤
│ Page Header：页内记录数等        │
├──────────────────────────────────┤
│ infimum / supremum（边界记录）  │
├──────────────────────────────────┤
│ User Records（商品记录所在区域）│
│ ...                              │
├──────────────────────────────────┤
│ Free Space（剩余可用空间）      │
├──────────────────────────────────┤
│ Page Directory（页内目录）      │
├──────────────────────────────────┤
│ File Trailer                     │
└──────────────────────────────────┘
```

**此图为教学示意，不是逐字节精确比例图。** 它描述的是常见 InnoDB 索引页的逻辑组成；不能把它套用为所有 InnoDB 页面类型的统一结构。

我们尚未研究 `infimum`、`supremum` 与 `Page Directory` 的内部搜索细节，因为此刻读者只需要理解：一页能够保存多条记录，而且页面具有自己的元数据。下一章讲到在页内查找和在多个页之间跳转时，这些结构才会变得必要。

## 7. 页解决了什么，没有解决什么？

回到我们自己构造的 `products.pages` 文件。

它相比逐行文本文件，已经有了三项明确能力：

**第一，页面可以定位。** 在极简单文件模型里，知道第 2 页，就能从固定偏移读取它。真实 InnoDB 则使用表空间 ID、页号以及相应的存储管理信息；实际文件定位不能简单无条件套用 `page_no * 16384`。

**第二，页面可以携带元数据。** 读取页面时，除了商品记录，还能取得页类型、页号、页内记录数等信息。

**第三，页面成为可独立管理的单位。** 后续访问、缓存与写回，可以围绕页面进行；这并不意味着一次 SQL 一定对应一次物理磁盘 I/O，也不意味着一条记录只能占据一个页面。大字段还可能涉及页外存储。

但最重要的问题仍然没有解决。

我们自己的 `scan_file()` 函数为了找到商品 42，依然读取了全部三个页面。

如果文件从三个页面增长到三百万个页面，那么即便每页的布局都很清楚，为了找到最后一条记录，我们仍然可能逐页扫描。

于是，下一项需求已经非常明确：

> **已知 `id = 42`，能否不读完前面的页面，就知道应该到哪一页去找？**

我们已经有了页这个可定位单位，现在需要的是一种**在多个页面之间有效缩小查找范围的组织方式**。

这正是下一章研究 B+ 树的真正起点，而不是因为 MySQL 源码里出现了 `B-tree` 这个词，就要求读者先背诵整棵树。

---

## 8. 本章验证与边界

我们今天只做了两类验证。

**已实际运行的实验**：前面的 Python 程序确实生成了一个由三个 64 字节教学页组成的文件，并在第 2 页第 0 个槽位找到商品 42。它验证的是“固定页大小 + 页头 + 页内记录”的简化存储模型，不等同于验证了 InnoDB 的物理文件格式。

**已核对的真实实现**：MySQL 8.4.0 中，`page_id_t` 使用表空间 ID 和页号表示页面；`fil0types.h` 定义了页头信息的位置；`page0types.h` 定义了索引页内部元数据的位置；`buf_page_get()` 将页 ID 与页大小交给底层页面访问过程。这些源码都可以通过本章对应的 Git Tag 链接查看。

**尚未执行的 MySQL 实验**：`SELECT @@GLOBAL.innodb_page_size` 是读者可以在自己的服务器上运行的检查语句。本章没有虚构一个真实 MySQL 实例的执行记录。

我们也还没有证明“商品 42 实际位于某个编号的 InnoDB 页面”，没有读取或解析它的 `.ibd` 文件，更没有声称 Python 的 64 字节页与 InnoDB 页面逐字段对应。

这些边界很重要：教学模型负责建立直觉，真正的架构结论必须由对应版本的源码支持。

## 9. 从本章走向下一章

这一章，我们沿着库存扣减请求提出了一个新的问题：记录不能永远只存在 Python 字典里，真实数据库必须管理磁盘中的数据。

先按行写文本，已经能够保存数据；但随着记录变多、字段长度变化，定位和修改会越来越麻烦。

于是，我们把文件划分为能够独立定位的固定大小页面。随后，我们通过 MySQL 8.4.0 的真实源码看到了页面 ID、页头、索引页内部元数据和页面访问接口。

现在，系统已经具备“**知道页号就能请求对应页面**”的能力。

但是，我们还不知道“**根据商品 ID 如何得到应该访问的页号**”。

对于只有三个页面的文件，逐页找也没什么大不了；对于数百万个页面，逐页找就无法接受。

**下一章：第 05 章《从页面到 B+ 树——索引怎样帮助我们定位记录？》**

我们将从一个只有几个页面的最小结构开始，让商品 42 的查找需求迫使我们增加“引导查找方向”的信息，再一步步对照 InnoDB 的真实索引搜索源码。

---

## 源码与参考资料

固定版本：MySQL Server `mysql-8.4.0`。

1. [`storage/innobase/include/buf0types.h`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/include/buf0types.h)：`page_id_t` 页面标识类型。
2. [`storage/innobase/include/fil0types.h`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/include/fil0types.h)：`FIL_PAGE_OFFSET`、`FIL_PAGE_TYPE`、`FIL_PAGE_SPACE_ID` 等文件页字段偏移。
3. [`storage/innobase/include/page0types.h`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/include/page0types.h)：`PAGE_HEADER`、`PAGE_N_RECS` 等索引页元数据定义。
4. [`storage/innobase/include/buf0buf.h`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/include/buf0buf.h)：`buf_page_get()` 页面访问接口。
5. [MySQL 8.4：File Space Management](https://dev.mysql.com/doc/refman/8.4/en/innodb-file-space.html)：表空间由页面组成及默认页面大小。
6. [MySQL 8.4：innodb_page_size](https://dev.mysql.com/doc/refman/8.4/en/innodb-parameters.html)：可配置的页面大小以及实例初始化限制。
