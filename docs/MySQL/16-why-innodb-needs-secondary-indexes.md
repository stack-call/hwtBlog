---
title: "16｜从主键查询到名称查询：为什么需要二级索引？"
sidebar_position: 17
slug: /mysql/16-why-secondary-indexes
description: "从商品 42 的主键查找出发，改为按名称寻找 Keyboard，沿可运行的最小模型与 MySQL 8.4.0 真实源码，推导 InnoDB 二级索引为什么存在以及它保存什么。"
tags: [MySQL, InnoDB, 二级索引, 源码精读]
---

# 从主键查询到名称查询：为什么需要二级索引？

前面十五章，我们一直沿着同一次商品购买向下追踪。

```sql
UPDATE products
SET stock = stock - 1
WHERE id = 42 AND stock > 0;
```

现在，我们已经知道 MySQL 如何接收这条 SQL，InnoDB 怎样按照主键定位记录，记录又怎样在 Buffer Pool 中被修改；也理解了事务、并发、日志和提交的基本行为。

但还有一个看似微小、实际上会改变读取路径的业务要求。

原来，购买请求直接提供商品 ID：

```sql
SELECT stock FROM products WHERE id = 42;
```

现在，用户只记得商品名称：

```sql
SELECT stock FROM products WHERE name = 'Keyboard';
```

**我们仍然要找到那件商品。唯一改变的，是手里已知的查找条件。**

为什么这个变化值得单独写一章？因为之前学会的 B+ 树查找，不能简单理解为“无论按哪个字段查询，都能直接找到记录”。

本章只回答一个问题：**已经存在按主键组织的聚簇索引，为什么按商品名称查询时，还需要另一种索引？**

我们会把“找到名字对应的主键”解释清楚，并在刚好需要取得 `stock` 的位置停下。后一段额外读取，就是下一章专门研究的“回表”。

## 1. 已有的 B+ 树，为什么帮不上这个忙？

先用一个独立实验表保存六条商品记录：

| id | name | stock |
|---:|---|---:|
| 7 | Mouse | 20 |
| 19 | Monitor | 35 |
| 42 | Keyboard | 100 |
| 80 | Desk | 50 |
| 99 | Chair | 10 |
| 107 | Keyboard | 40 |

注意，`Keyboard` 出现了两次。我们故意保留这两条记录，因为商品名称未必唯一，而商品 ID 才是这个例子中的唯一标识。

以前查询 `id = 42`，我们可以直接沿着主键索引查找。上一阶段已经研究过 InnoDB 的聚簇索引：在本例中，它按照主键组织记录，叶子页保存行数据。

但现在只有 `name = 'Keyboard'`。

假设我们沿着主键索引寻找，首先面对的依然是按 `id` 排列的记录：

```text
按主键组织的记录（教学示意）

  7 -> Mouse
 19 -> Monitor
 42 -> Keyboard
 80 -> Desk
 99 -> Chair
107 -> Keyboard
```

主键索引能够帮助我们回答：“ID 为 42 的记录在哪儿？”

它却不能直接回答：“哪些记录的名称是 Keyboard？”

因为当我们只知道 `Keyboard` 时，无法从主键 ID 的大小判断应该往哪一段继续查找。

当然，数据库并不会因此完全无法执行 SQL。最朴素的办法仍然有效：**依次读取记录，检查 `name` 是否等于 `Keyboard`。**

```python
# 教学化简：逐条检查所有商品
matches = []
for product in products:
    if product["name"] == "Keyboard":
        matches.append(product)
```

这个办法完全正确。记录少时，也完全可以接受。

问题仍然是规模：如果有一千万条商品记录，而只有很少几条叫 `Keyboard`，我们也可能不得不检查非常多的记录。

请注意，这并不是“没有索引”。InnoDB 的数据表仍然通过聚簇索引组织。更准确地说，是**没有能够利用 `name` 条件缩小查找范围的索引**。

于是，当前真正缺少的能力是：给定商品名称，快速确定相关商品的 ID。

## 2. 最简单的改进：再准备一份按名称查找的目录

我们不需要立即画第二棵 B+ 树。

先借用一个更小的想法：如果手边有一张按商品名称排列的目录呢？

```text
名称目录（教学示意）

Chair     -> 99
Desk      -> 80
Keyboard  -> 42
Keyboard  -> 107
Monitor   -> 19
Mouse     -> 7
```

这张目录不重复保存整条商品记录，只保存“名称”和“能够找到对应商品的 ID”。

现在要查找 `Keyboard`，就可以先在名称目录中缩小范围，找到两条匹配的目录项：

```text
Keyboard -> 42
Keyboard -> 107
```

这样，问题被拆成两个动作：

1. 按名称定位匹配的目录项。
2. 从目录项得到商品 ID；如果还需要其他字段，再利用 ID 找到原始记录。

第一个动作，就是本章的重点。

我们不妨暂时把这张目录称为“按名称建立的第二条查找路径”。到了真实 InnoDB 中，**聚簇索引之外的普通索引，就是二级索引（secondary index）**。

这里有两个重要细节。

首先，**二级索引不是第二份完整商品表**。它为某种查找条件维护必要的索引条目；条目不一定包含查询要求的全部字段。

其次，名称允许重复，因此不能把目录简单实现为 `name -> 单个 id` 的字典。我们必须保存一个名称对应的所有记录，或者保存多条可区分的索引条目。

接下来，用最小程序实际跑通这个想法。

## 3. 最小可运行实验：一个主键表，加一份名称目录

先准备商品数据：

```python
rows = [
    {"id": 7, "name": "Mouse", "stock": 20},
    {"id": 19, "name": "Monitor", "stock": 35},
    {"id": 42, "name": "Keyboard", "stock": 100},
    {"id": 80, "name": "Desk", "stock": 50},
    {"id": 99, "name": "Chair", "stock": 10},
    {"id": 107, "name": "Keyboard", "stock": 40},
]
```

先编写完整扫描：

```python
def full_scan(target):
    matches = []
    checked = 0

    for row in rows:
        checked += 1
        if row["name"] == target:
            matches.append(row["id"])

    return matches, checked
```

现在创建两份不同用途的结构：

```python
# 教学模型：用字典模拟根据主键定位行
primary = {row["id"]: row for row in rows}

# 教学模型：用有序元组模拟名称二级索引条目
secondary = sorted((row["name"], row["id"]) for row in rows)
```

在这个实验中，`primary` 是为了简化后续读取行数据；真实 InnoDB 的聚簇索引不是 Python 哈希字典。

`secondary` 则是我们真正要研究的结构：

```text
('Chair', 99)
('Desk', 80)
('Keyboard', 42)
('Keyboard', 107)
('Monitor', 19)
('Mouse', 7)
```

同一个名称之下，仍然可以有不同的主键。目录项之间依然可以保持确定的顺序。

现在实现一个只比较 `name` 的二分定位过程：

```python
def secondary_search(target):
    left, right = 0, len(secondary)
    comparisons = 0

    # 先找到第一个 name >= target 的位置
    while left < right:
        mid = (left + right) // 2
        comparisons += 1

        if secondary[mid][0] < target:
            left = mid + 1
        else:
            right = mid

    # 再取出连续匹配的所有条目
    ids = []
    visited = 0
    while left < len(secondary) and secondary[left][0] == target:
        ids.append(secondary[left][1])
        left += 1
        visited += 1

    return ids, comparisons, visited
```

把这几段代码放在同一个 Python 文件中，再执行：

```python
from_scan, scans = full_scan("Keyboard")
from_index, compares, entries = secondary_search("Keyboard")

assert from_scan == from_index == [42, 107]
assert [primary[i]["stock"] for i in from_index] == [100, 40]

print("扫描结果 ID:", from_scan)
print("全表检查行数:", scans)
print("索引定位比较:", compares)
print("索引匹配条目:", entries)
print("匹配库存:", [primary[i]["stock"] for i in from_index])
```

**已实际运行的输出：**

```text
扫描结果 ID: [42, 107]
全表检查行数: 6
索引定位比较: 3
索引匹配条目: 2
匹配库存: [100, 40]
```

这组很小的数据并不能证明索引一定更快。它能证明的是：我们把“逐条检查所有行”，变成了“先定位名称对应的条目，再读取连续的匹配项”。

当记录很多、目标名称匹配很少时，这个变化就有可能大幅减少不必要的检查。

这里也要诚实地区分验证边界：

- `3` 是这个 Python 二分循环的**字符串比较次数**，不是 MySQL 的页面读取次数。
- `2` 是匹配到的**索引条目数**，不是一定发生两次磁盘 I/O。
- `primary` 是便于说明的哈希字典，不是 InnoDB 聚簇索引的真实实现。
- 实验只模拟索引的查找目的，没有模拟事务可见性、页分裂、锁或 Buffer Pool。

我们只是让“为什么需要第二条查找路径”变成了一件可以观察到的事情。

那么，InnoDB 的真实二级索引是不是也会保存 `(name, id)` 这样的信息？

## 4. 真实 MySQL：为商品名称建立二级索引

先创建一张**独立实验表**，避免覆盖前面购买事务示例中的 `products` 数据。

```sql
CREATE DATABASE IF NOT EXISTS mysql_source_lab;
USE mysql_source_lab;

DROP TABLE IF EXISTS products_lookup_lab;

CREATE TABLE products_lookup_lab (
    id BIGINT PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    stock INT NOT NULL
) ENGINE = InnoDB;

INSERT INTO products_lookup_lab (id, name, stock) VALUES
    (7, 'Mouse', 20),
    (19, 'Monitor', 35),
    (42, 'Keyboard', 100),
    (80, 'Desk', 50),
    (99, 'Chair', 10),
    (107, 'Keyboard', 40);
```

这里先只有主键索引。

执行：

```sql
EXPLAIN FORMAT=TRADITIONAL
SELECT id
FROM products_lookup_lab
WHERE name = 'Keyboard';
```

对于这个只有主键、没有名称索引的普通实验表，预期 `type` 为 `ALL`、`key` 为 `NULL`，表示没有通过名称索引定位记录。

**这里是预期行为，而不是已在本环境的 MySQL 实例中执行得到的输出。**

然后加上一条名称索引：

```sql
CREATE INDEX idx_products_name
ON products_lookup_lab(name);
```

为什么只写 `name`？因为当前新增的查找条件就是名称。

我们没有必要为整个商品表的所有字段都建索引。额外索引既占用空间，也会增加相关数据修改时的维护工作。

索引建好之后，再看看表的索引信息：

```sql
SHOW INDEX FROM products_lookup_lab;
```

你应当能看到 `PRIMARY` 和 `idx_products_name` 两种索引。

再次执行：

```sql
EXPLAIN FORMAT=TRADITIONAL
SELECT id
FROM products_lookup_lab
WHERE name = 'Keyboard';
```

由于实验表只有六条记录，优化器可能认为直接扫描更便宜，因此**创建了索引，并不保证这条 SQL 一定使用索引**。

为了专门观察按名称访问的可用路径，可以单独运行：

```sql
EXPLAIN FORMAT=TRADITIONAL
SELECT id
FROM products_lookup_lab FORCE INDEX (idx_products_name)
WHERE name = 'Keyboard';
```

重点检查：

| 字段 | 如何理解 |
|---|---|
| `possible_keys` | 当前查询可能考虑的索引候选 |
| `key` | 实际选择的索引，预期是 `idx_products_name` |
| `type` | 按索引查找时常见的是 `ref` |
| `rows` | 优化器估计要检查的行数，不是精确实测值 |

`FORCE INDEX` 在这里用于构造可观察的对照实验，**不是日常查询都应该强制索引的建议**。

到这里，从外部看，我们已经能够给 `name` 建立一条独立查找路径。

但一个新的细节值得追问：

我们在 SQL 中只写了 `INDEX(name)`。那么，为什么这条索引能够帮助我们找到某条具体的商品记录？它只是保存了名称，还是还保存了别的信息？

这时，我们才进入真实源码。

## 5. 源码证据一：二级索引为什么要带上聚簇索引的键？

本章源码固定为：

- 项目：`mysql/mysql-server`
- Tag：`mysql-8.4.0`
- 文件：`storage/innobase/dict/dict0dict.cc`
- 函数：`dict_index_build_internal_non_clust()`

[查看 MySQL 8.4.0 原始源码：dict0dict.cc](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/dict/dict0dict.cc)

这个函数负责建立非聚簇索引的内部字典表示。我们不需要先学习整个数据字典系统，只看“它怎样确定索引应包含哪些列”。

在函数中，先取得这个表的聚簇索引：

**真实源码节选：**

```cpp
clust_index = UT_LIST_GET_FIRST(table->indexes);
```

这里的 `table->indexes` 是 InnoDB 在内存中维护的索引列表。对于这个表，第一项就是聚簇索引。

然后，函数会先复制用户声明的索引字段。也就是说，我们写下的 `name` 会成为二级索引定义的一部分。

但代码还没有结束。

它接着遍历聚簇索引中用于唯一确定记录的字段，把尚未包含在二级索引中的必要字段加入进去。

对应源码的核心动作是：

**真实源码节选（只保留条件及添加动作，省略循环和空间索引分支）：**

```cpp
if (!indexed[field->col->ind]) {
  dict_index_add_col(new_index, table, field->col,
                     field->prefix_len, field->is_ascending);
}
```

这段代码在 `dict_index_build_internal_non_clust()` 遍历聚簇索引唯一键字段的过程中执行。

现在再回头看我们的表：

```sql
PRIMARY KEY (id)
INDEX idx_products_name (name)
```

在这个例子中，InnoDB 构造的名称二级索引，其内部条目可理解为：

```text
(name, id)
```

`name` 帮助我们按照名称寻找条目。

`id` 帮助我们区分同名的不同商品，并为以后定位完整记录提供依据。

例如：

```text
('Keyboard', 42)
('Keyboard', 107)
```

这并不是我们凭空设计出来的目录结构。真实 InnoDB 在建立非聚簇索引的内部描述时，就会补入确定聚簇索引记录所需的字段。

这里也有几个边界：

1. 不能机械地说所有二级索引都额外重复追加一份完整主键。如果索引已经包含所需的主键字段，内部表示不会无条件重复添加。
2. 如果一个表没有显式主键，InnoDB 的聚簇索引选择规则会有所不同。本系列这个表明确有 `PRIMARY KEY(id)`，所以先不扩展。
3. 二级索引不是磁盘地址的简单列表。它的条目通过聚簇索引键标识对应行，而不是给出一个固定不变的“物理文件偏移”。

到这里，我们已经回答了本章最重要的一半：**二级索引怎样提供与主键不同的查找顺序，以及如何保存指向具体行的依据。**

接下来，还需要在源码中找到这些主键信息怎样被使用。

## 6. 源码证据二：从二级索引记录中，怎样取出主键？

这一次打开：

[MySQL 8.4.0 — `storage/innobase/row/row0row.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/row/row0row.cc)

搜索：

```cpp
row_build_row_ref
```

这个函数的职责在源码注释中写得非常明确：根据二级索引记录构造用于查找聚簇索引记录的行引用。

在 `row_build_row_ref()` 内部，它取得表的聚簇索引，再决定需要提取几个键字段：

**真实源码节选：**

```cpp
clust_index = table->first_index();
ref_len = dict_index_get_n_unique(clust_index);
ref = dtuple_create(heap, ref_len);
```

这里的 `ref` 不是商品完整数据，而是为了继续寻找原记录而构造的键值元组。

对于我们的表，聚簇索引唯一键是 `id`，所以这个行引用的关键内容就是对应的主键值。

例如，从下面这条二级索引记录：

```text
('Keyboard', 42)
```

可以构造出指向聚簇索引记录的查找键：

```text
id = 42
```

实际源码中还要处理字段位置、内部类型、前缀长度等问题。但本章需要理解的动作只有一个：

**二级索引提供了名称对应的主键值；InnoDB 可以把主键值组织成进一步查找的条件。**

这也解释了为什么二级索引不需要复制完整的 `stock` 值，就能够帮助我们找到它属于哪条商品记录。

你可能会继续问：只有主键，不是还缺少库存吗？

是的。这恰好是我们下一章要解决的问题。

不过，为了证明它确实不是一张孤立的目录，我们还需要再往真实读取路径看一步。

## 7. 源码证据三：二级索引读取为什么可能继续访问聚簇索引？

打开：

[MySQL 8.4.0 — `storage/innobase/row/row0sel.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/row/row0sel.cc)

这是 InnoDB 行读取实现的重要文件。之前的主键读取和 MVCC 章节已经遇到过这里的读取逻辑。

对于当前普通二级索引读取路径，`row_search_mvcc()` 在需要读取聚簇记录时，有这样一个判断：

**真实源码节选：**

```cpp
if (index != clust_index && prebuilt->need_to_access_clustered) {
```

它表达的意思是：

- 当前访问的索引不是聚簇索引；
- 本次读取又需要访问聚簇记录；
- 那么就需要进一步进行聚簇索引记录的定位与读取。

在相关的行引用查找辅助逻辑中，InnoDB 会从二级索引记录构造聚簇索引查询键，然后在聚簇索引中定位记录。例如同一文件的 `Row_sel_get_clust_rec_for_mysql::operator()` 中，就能看到：

**真实源码节选：**

```cpp
row_build_row_ref_in_tuple(prebuilt->clust_ref, rec, sec_index, *offsets);
```

这里的输入包括已经拿到的二级索引记录 `rec` 和索引描述 `sec_index`。

得到的结果写入 `prebuilt->clust_ref`，交给后面的聚簇索引搜索使用。

这次，我们终于能把查找过程连起来：

```text
SELECT stock
WHERE name = 'Keyboard'
          |
          v
按 name 找到二级索引条目
          |
          v
('Keyboard', 42)
('Keyboard', 107)
          |
          v
得到各条目的主键 id
          |
          v
必要时再访问聚簇索引中的行记录
```

这依然是一张**概念化的数据定位图**，不是一条省略所有条件后仍保证逐行执行的 C++ 调用栈。

尤其需要注意：在一些读取场景中，二级索引中已有的字段足以满足查询，未必需要再次取得聚簇记录；而在一致性读取、记录版本检查等情况下，实际过程也可能更加复杂。

这些差异现在不需要展开，因为它们并不会改变本章已经建立的核心事实：

> 二级索引通过另一组排序键定位条目，而这些条目保存了能够关联到聚簇索引记录的键值信息。

我们只需要暂时停在“已经拿到主键”这一步。

## 8. 回到我们的 SELECT：为什么不一定要走名称索引？

现在再次观察：

```sql
SELECT id
FROM products_lookup_lab
WHERE name = 'Keyboard';
```

我们刚建立了 `idx_products_name(name)`，并且已经知道它内部会带上本表的主键 `id`。

看起来，使用它非常合理。

但是，数据库还有另外一种可行策略：直接扫描整个表。

如果只有六条记录，全表扫描需要检查的记录非常少。为了访问另一棵索引树而付出的工作，并不一定比直接扫描更省。

如果这张表有一百万条记录，而且 `Keyboard` 只出现两次，那么按名称索引定位就更有吸引力。

因此，**“存在适用索引”与“优化器决定使用它”是两个不同问题。**

我们前面用 `FORCE INDEX` 是为了把名称索引这条路径单独展示出来，而不是证明它在每个规模下都是最优选择。

暂时不用深入优化器是怎样估算代价的。那个问题会留到专门的查询优化器章节。

现在我们的核心任务依然是理解索引在数据读取中提供了什么能力。

## 9. 二级索引的收益与代价

假设业务越来越依赖按名称查找商品。

增加 `INDEX(name)` 之后，数据库就获得了一条新的查找路径，不必所有名称查询都从按主键排列的第一条记录开始检查。

但这条路径不是免费的。

例如，新增一件商品时，除了把商品行写入聚簇索引，还需要维护相关的二级索引条目。

如果商品名称被修改，相关名称索引信息也要调整。

如果索引字段占用较多空间，或者主键很长，二级索引中的记录也会变得更大，因为它需要保留用于定位聚簇行的键列。

因此，不能因为 `INDEX(name)` 有效，就推导出“应该给每一个字段都建索引”。

真正合理的判断应该来自查询需求：

- 某个条件是否经常用于筛选记录？
- 命中记录在表中占比是多少？
- 新增索引带来的写入和空间成本是否值得？

这里只建立概念上的取舍关系，不提前展开统计信息、索引选择率和优化器代价模型。

## 10. 把这一章的逻辑接回前十五章

回看我们之前的商品购买主线：

```sql
UPDATE products
SET stock = stock - 1
WHERE id = 42 AND stock > 0;
```

这条操作提供了主键值，所以 MySQL 有机会通过聚簇索引直接定位目标行。

现在，我们切换到读取同一业务数据的另一个入口：

```sql
SELECT stock
FROM products_lookup_lab
WHERE name = 'Keyboard';
```

它没有提供主键值，因此之前的主键定位路径不能直接用来缩小 `name` 条件的搜索范围。

我们先接受全表扫描这个最简单的方案；随着记录数量增长，发现它可能需要检查大量无关行；于是尝试维护一张按名称组织的目录。

最小实验里，目录条目是 `(name, id)`。

进入 MySQL 8.4.0 源码后，我们又找到了三个彼此对应的证据：

| 当前推论 | 真实源码证据 |
|---|---|
| 建立二级索引时需要保存关联到聚簇行的键值 | `dict_index_build_internal_non_clust()` |
| 可以从二级索引记录中构造聚簇索引查找键 | `row_build_row_ref()` |
| 在需要时，会进入聚簇记录读取路径 | `row_search_mvcc()` 与相关行引用查找逻辑 |

这些代码证明的不是“索引总比扫描快”，而是：**InnoDB 不同索引承担不同的查找顺序；二级索引把非主键条件与可定位的聚簇行联系起来。**

最后再强调一点：我们一直使用同一商品业务案例，第二篇 SELECT 专题只是改变“寻找商品所依据的信息”。它仍然属于已经建立的 MySQL 读取路径，而不是无关的知识点扩展。

## 11. 下一章：找到名称之后，为什么还要再找一次记录？

现在，我们已经通过名称索引找到了：

```text
('Keyboard', 42)
('Keyboard', 107)
```

但原始查询要求返回的并不是商品 ID，而是库存：

```sql
SELECT stock
FROM products_lookup_lab
WHERE name = 'Keyboard';
```

我们的二级索引只声明了 `name`，内部补充的是主键 `id`。

那么，`stock` 在哪里？

如果二级索引条目本身不包含 `stock`，MySQL 怎么把真正的库存读出来？

为什么有些 SQL 只查索引就够了，另一些 SQL 却需要继续访问聚簇索引？

**下一章，我们就在这一步继续：第 17 章《从二级索引到回表——为什么有时需要查找两次？》**

---

## 源码版本、实验状态与参考资料

本文的真实源码定位均以 `mysql/mysql-server` 的 **`mysql-8.4.0` Tag** 为准。

| 文件 | 函数或代码入口 | 对应问题 |
|---|---|---|
| `storage/innobase/dict/dict0dict.cc` | `dict_index_build_internal_non_clust()` | 内部二级索引条目为什么包含聚簇索引键列 |
| `storage/innobase/row/row0row.cc` | `row_build_row_ref()` | 怎样从二级索引记录取得行引用 |
| `storage/innobase/row/row0sel.cc` | `row_search_mvcc()` | 哪些情况下需要继续访问聚簇索引 |
| `storage/innobase/row/row0sel.cc` | `Row_sel_get_clust_rec_for_mysql::operator()` | 行引用怎样交给聚簇索引搜索 |

参考链接：

- [MySQL 8.4.0：dict0dict.cc](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/dict/dict0dict.cc)
- [MySQL 8.4.0：row0row.cc](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/row/row0row.cc)
- [MySQL 8.4.0：row0sel.cc](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/row/row0sel.cc)
- [MySQL 8.4：Clustered and Secondary Indexes](https://dev.mysql.com/doc/refman/8.4/en/innodb-index-types.html)
- [MySQL 8.4：Use of Index Extensions](https://dev.mysql.com/doc/refman/8.4/en/index-extensions.html)
- [MySQL 8.4：How MySQL Uses Indexes](https://dev.mysql.com/doc/refman/8.4/en/mysql-indexes.html)

**已实际执行：** 本文第 3 节的 Python 教学实验。结果为扫描 6 条记录，名称目录定位进行 3 次比较，再读取 2 条匹配的索引条目；断言全部通过。

**未实际执行：** 本文提供的 MySQL DDL、`EXPLAIN` 和 SQL 查询。相应访问类型均标注为预期或可能结果，没有伪造运行截图与执行时间。

**已核对：** 上述 MySQL 8.4.0 源码函数、关键分支与 InnoDB 二级索引保存聚簇索引键列的设计。本文没有声称完成调试器级别的逐指令运行验证。
