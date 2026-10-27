---
title: "05｜从页面到 B+ 树：索引怎样帮我们定位商品 42？"
sidebar_position: 6
slug: /mysql/05-btree-find-row
description: "从逐页扫描的失败场景出发，逐层构造索引引导页和 B+ 树，并对照 MySQL 8.4.0 的真实索引搜索源码。"
tags: [MySQL, InnoDB, B+树, 聚簇索引, 源码精读]
---

# 从页面到 B+ 树：索引怎样帮我们定位商品 42？

上一章，我们终于把商品记录从一个抽象的 Python 字典，放进了可以按页寻址的文件中。

对于我们的库存扣减：

```sql
UPDATE products
SET stock = stock - 1
WHERE id = 42 AND stock > 0;
```

这已经解决了一个很具体的问题：**只要知道某个页面的编号，我们就能从相应的位置读取它，再在页面里找记录。**

但上一章的教学程序仍然靠 `scan_file()` 从第 0 页开始检查，直到遇见商品 42。页面解决了“**知道页号，怎样读取**”；它没有解决“**只知道商品 ID，怎样得到页号**”。

如果只有三个页，逐页扫描完全可以接受。但当商品表拥有几百万个页时，反复读完前面所有页面就不再合理。

这一章，我们暂时不研究 SQL 优化器，也不研究 Buffer Pool 的淘汰策略。只沿着商品 42 的**主键定位**往下走，看看一个页面文件需要增加什么信息，才能不必从头扫描。

## 1. 最小方案仍然可以工作：逐页找商品

为了方便观察，把上章的教学页继续设成 **64 字节**：前 8 字节做页头，其余空间每条记录占 8 字节（4 字节商品 ID，4 字节库存），一页最多放 7 条。

现在插入 42 件商品，ID 从 1 到 42，每件库存都是 100。按 ID 顺序把记录写入六个页面：

```text
页 0：id  1 ~  7
页 1：id  8 ~ 14
页 2：id 15 ~ 21
页 3：id 22 ~ 28
页 4：id 29 ~ 35
页 5：id 36 ~ 42
```

为了完成 `id = 42` 的查询，我们当然可以从页 0 开始检查。扫描页 0，没找到；扫描页 1，仍然没有……直到第 5 页才读到商品 42。

整个过程需要读取 **6 个教学页**。

这不是一个错误算法。对于一个很小的文件，它非常直观，也能正确返回 `stock = 100`。但如果有十万页，目标商品恰好靠后，就可能要检查十万页。

此刻最需要的新能力并不是“让页变大”，而是：

> **在读取某页之前，能够判断目标商品是否可能在那一页。**

## 2. 先增加一张非常小的“页导航表”

现在假设我们已经知道，每个页面里的商品 ID 按从小到大排列，而且页面之间的 ID 范围也按顺序衔接。

能不能另外保存一张导航表？

```text
导航信息（教学模型）

最大 ID    商品页号
-------    -------
   7          0
  14          1
  21          2
  28          3
  35          4
  42          5
```

读取商品 42 时，不必立即读页 0。

先从导航表里找到**第一个最大 ID 大于等于 42** 的条目，于是得到页号 5，再直接读取页 5。

```text
输入：id = 42
       |
       v
  导航表：42 → 页 5
       |
       v
  页 5：[36, 37, 38, 39, 40, 41, 42]
       |
       v
  找到：stock = 100
```

原来要读 6 个商品页，现在只需要读目标页；如果导航表本身也在一个页中，则总共是 **2 个页**：导航页 + 商品页。

这里有个很重要的限制：导航项的“最大 ID”只是我们为教学设计的**上界规则**。真实 InnoDB 内部节点的记录布局和分隔键选择，不应直接等同于这张表。我们现在只验证“**上层信息能指向下层页面**”这一设计动作。

但这个办法很快还会出问题。

## 3. 导航表自己也会变大，怎么办？

我们的导航表每项仍占 8 字节，一个 64 字节页扣除 8 字节页头，只能放 7 项。

当商品数据只有 6 页时，导航表刚好能放进一个页。

可如果商品数据有 28 页呢？同样的导航项要占 28 个位置，一张导航页装不下。

最直观的办法是把导航表拆成多个页，每个导航页管理几个商品页：

```text
导航页 A → 商品页  0 ~  6
导航页 B → 商品页  7 ~ 13
导航页 C → 商品页 14 ~ 20
导航页 D → 商品页 21 ~ 27
```

但是这样一来，查询商品 42 之前，程序又得先判断该选导航页 A、B、C 还是 D。

我们是不是又回到了“逐个查找导航页”的问题？

解决办法其实和刚才一样：**在多个导航页之上，再增加一页导航**。它不直接指向商品记录，而是指向下一层导航页。

于是数据组织方式开始长成树：

```text
                   根页
                     |
            +--------+--------+
            |        |        |
          导航页   导航页   导航页
            |        |        |
          商品页   商品页   商品页
```

查找不再意味着沿着所有页面走一遍，而是从顶层开始，在当前页选定一条向下的路径。

可以继续加层吗？当然。只要上层页也放不下全部指向下一层的条目，就再构建一层。

现在才适合给它一个名字：**多层索引树**。

InnoDB 用于普通索引的数据结构通常称为 **B+ 树**；MySQL 官方文档也经常使用更宽泛的 **B-tree** 表述。对我们现在的商品表，关键特征是：

- **非叶子层**主要负责引导搜索到合适的下一层页面；
- **叶子层**保存实际的索引记录；
- 在我们的**主键聚簇索引**里，叶子记录包含商品行的数据，能够取得 `stock`；
- 查询只需沿着**一条根到叶的路径**向下查找，而不是遍历全部叶子页。

请不要把这些结论理解成“所有 B-tree 变种都有相同的精确字段布局”，也不要把上面的教学导航表当成真实 InnoDB 内部节点的字节结构。

当索引很小时，**根页本身就可以是叶子页**，根本不一定存在中间层。只有随着数据增加，才需要更多层级。

## 4. 真正运行一次：用磁盘页实现最小两层查找

上面的图已经有了直觉，但图不是证据。我们继续沿用上一章的固定大小教学文件，把“导航页”和“商品页”**都写入同一个二进制文件**。

这个模型刻意只支持：

- 固定页长 64 字节；
- 叶子页保存 `(id, stock)`；
- 一个根导航页保存 `(本子页最大 ID, 子页号)`；
- 已经有序的 42 条商品记录；
- 通过主键读取、扣减库存，不支持动态分裂、并发或崩溃恢复。

这样，我们可以亲自验证“知道商品 42 的主键，究竟少读取了哪些页面”。

**以下是可执行 Python 教学化简，不是 MySQL 的真实页格式或 B+ 树实现。**

```python
from pathlib import Path
from tempfile import TemporaryDirectory
import struct

PAGE_SIZE = 64
HEADER_SIZE = 8
ENTRY_SIZE = 8
PAGE_CAPACITY = (PAGE_SIZE - HEADER_SIZE) // ENTRY_SIZE
LEAF = 1
BRANCH = 2


def write_page(file, kind, entries):
    assert len(entries) <= PAGE_CAPACITY
    data = struct.pack("<II", kind, len(entries))
    data += b"".join(struct.pack("<ii", a, b) for a, b in entries)
    file.write(data.ljust(PAGE_SIZE, b"\0"))


def read_page(file, page_no, visited):
    file.seek(page_no * PAGE_SIZE)
    data = file.read(PAGE_SIZE)
    if len(data) != PAGE_SIZE:
        raise ValueError(f"读取页 {page_no} 失败")
    visited.append(page_no)
    kind, count = struct.unpack_from("<II", data, 0)
    entries = [
        struct.unpack_from("<ii", data, HEADER_SIZE + i * ENTRY_SIZE)
        for i in range(count)
    ]
    return kind, entries


def scan(path, product_id):
    visited = []
    with path.open("rb") as file:
        for page_no in range(6):
            kind, records = read_page(file, page_no, visited)
            assert kind == LEAF
            for key, stock in records:
                if key == product_id:
                    return stock, visited
    return None, visited


def indexed_lookup(path, product_id):
    visited = []
    with path.open("rb") as file:
        kind, branches = read_page(file, 6, visited)  # 第 6 页是根页
        assert kind == BRANCH

        for upper_bound, child_page_no in branches:
            if product_id <= upper_bound:
                kind, records = read_page(file, child_page_no, visited)
                assert kind == LEAF
                for key, stock in records:
                    if key == product_id:
                        return stock, visited
                break
    return None, visited


def buy(path, product_id):
    stock, visited = indexed_lookup(path, product_id)
    if stock is None or stock <= 0:
        return False, visited

    leaf_no = visited[-1]
    # 教学模型：只改变固定大小的 stock 字段，不需要页分裂。
    with path.open("r+b") as file:
        kind, records = read_page(file, leaf_no, [])
        new_records = [
            (key, value - 1 if key == product_id else value)
            for key, value in records
        ]
        file.seek(leaf_no * PAGE_SIZE)
        write_page(file, kind, new_records)
    return True, visited


with TemporaryDirectory() as folder:
    path = Path(folder) / "products.pages"

    with path.open("wb") as file:
        # 页 0~5：每页 7 件商品，库存全部为 100
        for start in range(1, 43, PAGE_CAPACITY):
            records = [(key, 100) for key in range(start, start + PAGE_CAPACITY)]
            write_page(file, LEAF, records)

        # 页 6：6 个范围上界，各自指向一个商品页
        branches = [(7 * (i + 1), i) for i in range(6)]
        write_page(file, BRANCH, branches)

    print("扫描：", scan(path, 42))
    print("索引：", indexed_lookup(path, 42))
    print("购买：", buy(path, 42))
    print("购买后：", indexed_lookup(path, 42))
```

**本章已在 Python 环境中执行上述代码，实际输出：**

```text
扫描： (100, [0, 1, 2, 3, 4, 5])
索引： (100, [6, 5])
购买： (True, [6, 5])
购买后： (99, [6, 5])
```

现在有了可观察的差异。

扫描时，请求从页 0 一直读到页 5；使用根导航页时，它先读页 6，直接得到目标叶子页 5，然后取得商品 42。

| 操作 | 访问的教学页 | 页读取次数 |
|---|---|---:|
| 逐页扫描商品 42 | 0 → 1 → 2 → 3 → 4 → 5 | 6 |
| 通过根页定位商品 42 | 6 → 5 | 2 |
| 扣减商品 42 后再次查询 | 6 → 5 | 2 |

这里的“读取次数”仅指代码中执行了多少次 `read_page()`，**不是物理磁盘 I/O 次数**。操作系统可能缓存文件内容，而真实 InnoDB 还要经过 Buffer Pool；我们将在下一章讨论那一层。

另外，这个模型事先生成了根页，还没有实现“插入时如何分裂页面、更新父节点”。这是刻意保留的问题：本章只需要验证**为什么需要根页，以及它如何把查找引导到正确的叶子页**。

既然我们的两层结构已经能够工作，接下来就应该去 MySQL 源码里确认：InnoDB 是否也真的从根页出发，再一层层选定子页？

---

## 5. 回到真实 InnoDB：商品行为什么在主键索引叶子页？

先回到本系列固定的表：

```sql
CREATE TABLE products (
    id BIGINT PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    stock INT NOT NULL
) ENGINE = InnoDB;
```

我们一直用 `id = 42` 来定位商品，而 `id` 在这个表中是主键。

InnoDB 有一个重要设计：**表的数据行通过聚簇索引组织**。当表显式声明 `PRIMARY KEY` 时，通常就使用这组主键列作为聚簇索引键；在这个聚簇索引的叶子记录里，包含对应行的数据。对于当前 `products` 表，定位主键 42 的叶子记录后，就能够取得 `stock` 字段所需的数据。大型列的页外存储是另一种情况，我们暂时不展开。

因此，我们不能把它想成：

```text
先查一个完全独立的主键索引文件
          ↓
得到另一份独立表文件的行号
          ↓
再到“表数据文件”找整行
```

对于这次**主键聚簇索引查找**，更准确的教学示意是：

```text
聚簇索引非叶子页：选择下一层
                ↓
聚簇索引叶子页：商品行记录（id、name、stock 等）
```

这也是为什么我们前面的教学模型把商品 `(id, stock)` 存放在叶子页，而不是额外增加一个独立的“行数据仓库”。

这里还没有引入二级索引。按照 `name` 查找商品是未来的专题问题，不必提前把回表、覆盖索引等术语挤到本章。

官方文档可以直接对照：

- [MySQL 8.4：Clustered and Secondary Indexes](https://dev.mysql.com/doc/refman/8.4/en/innodb-index-types.html)
- [MySQL 8.4：The Physical Structure of an InnoDB Index](https://dev.mysql.com/doc/refman/8.4/en/innodb-physical-structure.html)

## 6. 真实源码第一步：从根页开始

固定源码：**MySQL Server 8.4.0，Git Tag `mysql-8.4.0`**。

文件：[`storage/innobase/btr/btr0cur.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/btr/btr0cur.cc)。

本章关注：

```cpp
btr_cur_search_to_nth_level(...)
```

它的职责是：**在索引树中搜索，并把树游标定位到指定层级**。传入的 `index` 表示目标索引，`tuple` 是用于比较的查找键，`level` 表示想定位到的层级；普通记录查找通常最终要到叶子层 `level = 0`。调用者通过前面建立的存储引擎访问路径间接到达这里，并使用定位结果继续取得记录。不同读取模式、哈希快捷路径和特殊索引类型可以改变具体调用细节，所以这里先研究普通 B-tree 向下搜索的主干。

先看它从哪里开始。

**真实源码节选，省略无关语句：**

```cpp
const space_id_t space = dict_index_get_space(index);

/* Start with the root page. */
page_id_t page_id(space, dict_index_get_page(index));
```

`dict_index_get_space(index)` 给出该索引所属的表空间 ID，`dict_index_get_page(index)` 给出索引根页号。两者组成从哪个页开始读取的位置信息。

这与我们的教学模型很像：`indexed_lookup()` 一开始就知道根导航页是第 6 页。

真实 InnoDB 不会把所有索引都假定为页 6；每个索引需要依靠自己的元数据确定根页。

得到根页的标识以后，下一步就要获取页面。

在同一个函数的普通搜索路径上可以看到：

**真实源码节选（只保留页面获取调用）：**

```cpp
block = buf_page_get_gen(
    page_id, page_size, rw_latch,
    (height == ULINT_UNDEFINED ? index->search_info->root_guess : nullptr),
    fetch, {file, line}, mtr);
```

此刻不需要把 `rw_latch`、`mtr`、`root_guess` 全部拆开讲。我们当前关心的是：**根据已经决定的 `page_id` 获取一个页面，再继续在这个页面上搜索。**

`buf_page_get_gen()` 是真实的缓冲页访问入口；它不意味着每次都会直接从磁盘执行一次物理读取。这个区别会在下一章研究 Buffer Pool 时变得关键。

接下来，怎样知道当前读到的是根、中间页还是叶子页？

**真实源码节选：**

```cpp
height = btr_page_get_level(page);
root_height = height;
cursor->tree_height = root_height + 1;
```

这里的 `height` 来源于实际页面的层级信息。索引**叶子页的层级为 0**；如果根页本来就只有一层，那么根页也同时是叶子页，无需再找子页。

现在，我们在源码中确认了两件事情：

1. 一次普通的索引树向下搜索，从目标索引的根页出发。
2. 页面自身携带层级信息，搜索可以判断是否已经到达叶子层。

还差最关键的一步：**在当前非叶子页上，根据商品 42 选择哪个子页？**

## 7. 第二步：在当前页里找到通向子页的记录

我们自己的根页只有六个条目：

```text
(7,0) (14,1) (21,2) (28,3) (35,4) (42,5)
```

查找 42 时，程序比较这些边界，最后选择子页 5。

InnoDB 在非叶子页上同样需要根据查找键定位适当的记录，但其真实内部节点并不是我们这种固定的 `(max_id,page_no)` 二元组。

在 `btr_cur_search_to_nth_level()` 的普通分支，可以找到对页内搜索函数的调用：

**真实源码节选：**

```cpp
page_cur_search_with_match(block, index, tuple, page_mode, &up_match,
                           &low_match, page_cursor,
                           need_path ? cursor->rtr_info : nullptr);
```

它接收当前页（`block`）、索引描述（`index`）和要比较的键（`tuple`），把**当前页内合适的记录位置**放入 `page_cursor`。

注意，`page_cursor` 是“页内记录位置”，不是“已经拿到商品 42 的完整业务结果”。如果当前页是中间层，被选中的记录还会告诉程序下一层该去哪个页面。

源码继续在需要向下搜索的分支中取得被选中的记录：

```cpp
node_ptr = page_cur_get_rec(page_cursor);
```

然后用这条内部节点记录中的子页号，更新下一轮要访问的页面：

```cpp
page_id.reset(space, btr_node_ptr_get_child_page_no(node_ptr, offsets));
```

这就是整章最关键的一次动作。

我们开始时只有“商品 ID = 42”，并不知道商品行在哪个页面。借助当前索引页中的导航记录，程序得到了**下一层子页的页号**。

随后再次访问这个子页，重复“页内定位 → 选下一层”的过程，直到需要的层级。

可以把真实源码的核心思想浓缩成下面的**概念伪代码**（不是函数原文）：

```python
page = index.root_page

while page.level > 0:
    entry = find_route_in_page(page, key=42)
    page = get_page(entry.child_page_no)

record = find_record_in_leaf(page, key=42)
```

回头比较我们的实验：

```text
教学模型：根页 6 → 导航信息 → 叶子页 5 → 商品 42
真实机制：索引根页 → 页内定位 → 子页号 → ... → 叶子记录
```

两者在“逐层引导查找”这一点上相同，但页面格式、比较模式、记录编码以及锁存处理远不止教学模型这些代码。

> 特别提醒：源码的 `btr_cur_search_to_nth_level()` 存在自适应哈希索引命中等快捷路径，也包含重试、锁存和特殊索引类型分支。因此这里说明的是**普通 B-tree 下降搜索路径**，而不是声称每次主键访问都必然逐行执行以上片段。

## 8. 第三步：到达叶子页之后，为什么还需要页内目录？

我们现在已经知道如何到达商品所在的叶子页。

可一个页面通常能装下多条记录。如果只知道页号，不知道记录位置，仍然需要在页内查找商品 42。

在我们只有 7 条记录的教学模型中，遍历页内记录非常简单：

```python
for key, stock in records:
    if key == 42:
        return stock
```

7 次比较的上界可以接受。但真实 InnoDB 一个索引页可以存放许多记录，因此我们希望尽量不对整页逐条搜索。

这时，再回到真正的页内搜索源码：

[`storage/innobase/page/page0cur.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/page/page0cur.cc)。

函数：

```cpp
page_cur_search_with_match(...)
```

源码注释明确说明，它先对**页目录**进行二分查找，再在相应目录槽位所管理的小段记录中进行线性搜索。

**真实源码节选：**

```cpp
low = 0;
up = page_dir_get_n_slots(page) - 1;
```

随后查找目录中间位置：

```cpp
mid = (low + up) / 2;
slot = page_dir_get_nth_slot(page, mid);
mid_rec = page_dir_slot_get_rec(slot);
```

为什么它没有直接把整页记录当成 Python 数组，通过 `records[mid]` 取值？

因为真实 InnoDB 记录具有自己的变长布局和页内链接组织，不能简单假设“第 N 条记录”有一个固定可计算的字节偏移。页目录提供一组稀疏的页内定位点，帮助缩小范围，之后再检查目标小段里的记录。

这再次体现了同一个设计思想：

**如果一次搜索可以利用少量导航信息排除大量不可能的位置，就不必从头检查全部候选对象。**

只不过现在存在两个层次：

- **页与页之间**：树的内部页面决定下一层子页。
- **同一页内部**：页目录帮助定位记录所在的页内范围。

切勿把两种导航混为一谈。**树的内部页**是索引结构中的独立页面；**页目录**是一个索引页内部的组成部分。

到这里，我们已经能解释 InnoDB 为什么既需要“索引树的层级”，也需要“页面内部记录的定位结构”。

---

## 9. 返回 UPDATE：商品 42 已经定位，库存修改了吗？

回到本系列唯一的主线 SQL：

```sql
UPDATE products
SET stock = stock - 1
WHERE id = 42 AND stock > 0;
```

现在我们能够解释，它在选择主键访问路径时，底层可以怎样找到商品记录：

```text
MySQL 执行 UPDATE
        |
        v
按照主键访问 products
        |
        v
聚簇索引根页
        |
        v
通过非叶子页逐层选择子页
        |
        v
到达叶子页
        |
        v
在页内找到 id = 42 的记录
        |
        v
取得候选行，供条件检查与后续修改使用
```

这是**索引定位机制示意**，不是逐函数调用栈；可能存在单层树、快捷搜索路径和不同执行计划。

还需要明确两件事情。

第一，**找到记录并不代表允许扣减**。`WHERE stock > 0` 仍然必须被正确处理；如果库存是 0，本次修改就不应该发生。

第二，**找到记录并不等于已经更新了它**。索引搜索提供的是位置和相应记录；写入新库存还涉及页面内容修改以及后续机制。那些将是接下来的章节。

我们的 Python 示例为了验证两层查找，还顺手把叶子页里的 `stock` 改成了 99，但这不意味着 InnoDB 的更新过程也只需要一个文件 `seek()` 加一次写入。

这一章我们只证明：**给定商品主键，索引树有办法把查找从根页引导到正确的叶子记录。**

## 10. 在真实 MySQL 上观察什么？

下面给出两个不需要直接解析 `.ibd` 文件的 SQL 实验。这里的 SQL **尚未在真实 MySQL 8.4.0 服务器上执行**，因此只描述预期观察点，不虚构服务器输出。

### 实验 A：查询是否使用主键

```sql
USE mysql_source_lab;

EXPLAIN FORMAT=TRADITIONAL
SELECT stock
FROM products
WHERE id = 42;
```

重点看执行计划中的 `key`：对于本系列的 `PRIMARY KEY (id)`，在常见条件下预期选择 `PRIMARY`。如果你想直接观察 UPDATE 的访问计划，可以再执行：

```sql
EXPLAIN FORMAT=TRADITIONAL
UPDATE products
SET stock = stock - 1
WHERE id = 42 AND stock > 0;
```

`EXPLAIN` 本身不是库存扣减的执行，它不会因为展示计划而把库存减一。

注意：**执行计划能说明选择了什么访问方式，不能直接证明一次运行访问了哪个具体页号**，更不能用它测量精确的物理磁盘读次数。

### 实验 B：索引确实拥有根页元数据

如果在 MySQL 8.4 中有权限查看 `INFORMATION_SCHEMA.INNODB_INDEXES` 和 `INNODB_TABLES`，可以查询：

```sql
SELECT
    i.NAME AS index_name,
    i.SPACE AS space_id,
    i.PAGE_NO AS root_page_no
FROM INFORMATION_SCHEMA.INNODB_TABLES AS t
JOIN INFORMATION_SCHEMA.INNODB_INDEXES AS i
    ON i.TABLE_ID = t.TABLE_ID
WHERE t.NAME = 'mysql_source_lab/products'
  AND i.NAME = 'PRIMARY';
```

这里返回的 `SPACE` 和 `PAGE_NO` 是索引的元数据。`PAGE_NO` 表示索引树根页号；它不是商品 42 的叶子页号，也不能据此推断树高。对应的具体数字应以你自己的服务器输出为准。

这个实验帮助我们把源码中的：

```cpp
page_id_t page_id(space, dict_index_get_page(index));
```

与实际数据库保存的索引根页信息联系起来。

但它仍不足以直接验证完整的根到叶运行路径。要观测具体源码分支，需要带符号的 MySQL 构建、调试器或其他低层观测手段；本章没有声称已经记录这样的调试轨迹。

## 11. 新机制解决了什么，留下了什么？

回顾这一章的推导，我们从一个没有捷径的办法开始：

```text
知道商品 ID，却不知道在哪个页面
              ↓
从第一页扫描到最后一页
              ↓
数据量增长，扫描页数增长
              ↓
保存“键值范围 → 子页”的导航信息
              ↓
一张导航页放不下，就再建上一层
              ↓
形成由根页逐层引导到叶子页的树
```

对今天的库存修改而言，B+ 树主要解决的是：**商品记录数量增长时，不必为了主键定位而遍历所有商品页。**

它没有自动解决所有问题：

- **索引不是免费的。** 新增和删除记录可能需要维护树结构；动态分裂和合并不是本章模型实现的内容。
- **索引查找不等于零磁盘访问。** 每层要获取页面，页是否已经在内存中，会影响实际代价。
- **索引定位不是库存扣减的正确性保证。** 并发更新、事务提交、崩溃恢复仍需要其他机制。
- **树高也不是固定的。** 一条记录的小表可能只有一个根叶页，数据量增加后才形成多层结构。

这些边界反而为后面的源码学习留下了自然位置。

特别是第二个问题。

我们现在已经知道，搜索一条商品记录可能经过根页、中间页、叶子页。假设商品 42 很热门，每秒有许多请求查询或修改它。

**即使索引每次都只需要很少几个页，难道每个请求都要把这些页重新从磁盘读取一次吗？**

这时，我们才真正有必要继续研究内存中的页缓存。

**下一章：第 06 章《从磁盘到内存——为什么需要 Buffer Pool？》**

---

## 源码版本、证据和验证边界

固定版本：**MySQL Server 8.4.0，Git Tag `mysql-8.4.0`**。

| 真实源码文件 | 本章使用的职责 |
|---|---|
| [`storage/innobase/btr/btr0cur.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/btr/btr0cur.cc) | `btr_cur_search_to_nth_level()`：从根页取得页面、根据页内记录定位子页 |
| [`storage/innobase/page/page0cur.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/page/page0cur.cc) | `page_cur_search_with_match()`：页内目录二分定位，再在记录小段搜索 |
| [`storage/innobase/include/btr0btr.h`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/include/btr0btr.h) | 索引游标搜索与层级相关声明 |

官方文档：

1. [InnoDB：聚簇索引与二级索引](https://dev.mysql.com/doc/refman/8.4/en/innodb-index-types.html)
2. [InnoDB：索引的物理结构](https://dev.mysql.com/doc/refman/8.4/en/innodb-physical-structure.html)
3. [InnoDB INFORMATION_SCHEMA 索引元数据](https://dev.mysql.com/doc/refman/8.4/en/innodb-information-schema-system-tables.html)
4. [MySQL：EXPLAIN 输出](https://dev.mysql.com/doc/refman/8.4/en/explain-output.html)

**已实际执行**：64 字节教学页的 Python 实验，得到扫描 `[0,1,2,3,4,5]`、索引 `[6,5]`、库存 `100 → 99` 的结果。它验证的是教学模型中的逻辑页访问与按键导航，不证明真实 InnoDB 的 I/O 次数。

**已核对源码**：`btr_cur_search_to_nth_level()` 的根页定位、页面获取、页层级读取、页内搜索与子页号取得；`page_cur_search_with_match()` 中的页目录查找过程。

**未实际执行**：本章列出的 MySQL `EXPLAIN`、`INFORMATION_SCHEMA` 查询以及 C++ 调试追踪。因此本文不声称已经在实际服务器上确认商品 42 的叶子页号、树高或物理磁盘 I/O 次数。
