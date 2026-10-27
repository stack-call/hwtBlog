---
title: "17｜从二级索引到回表：为什么有时需要查找两次？"
sidebar_position: 18
slug: /mysql/17-secondary-index-back-to-table
description: 沿着商品 42 的名称查询，从二级索引只能找到主键的具体问题出发，阅读 MySQL 8.4.0 的回表源码，并通过覆盖索引实验理解什么时候可以省掉第二次定位。
tags: [MySQL, InnoDB, 二级索引, 回表, 覆盖索引, 源码精读]
---

# 从二级索引到回表：为什么有时需要查找两次？

上一章，我们改变了查询商品的方式。

此前，我们始终知道商品的主键 `id = 42`，于是可以利用聚簇索引定位记录。而现在，用户只知道商品名称：

```sql
SELECT stock
FROM products
WHERE name = 'Keyboard';
```

为了不再扫描所有商品，我们为 `name` 建立了二级索引。

这解决了第一个问题：**不知道主键时，怎样根据商品名称迅速找到候选记录？**

但是，上一章有一处细节还没有解释。

假设聚簇索引中的一行是：

```text
id = 42, name = Keyboard, stock = 100
```

在我们的单列 `name` 二级索引中，与它对应的索引记录可以简化表示为：

```text
name = Keyboard, id = 42
```

这里有商品名称，也有主键，却没有 `stock`。

**那么，MySQL 已经通过二级索引找到了商品 42，为什么仍然不能直接返回库存 100？**

本章只追踪这个问题。我们先让最简单的方案暴露不足，再研究 MySQL 怎样利用主键进行第二次查找，最后看看何时可以避免这次查找。

## 1. 先弄清楚：二级索引找到了什么？

继续使用贯穿全系列的商品表：

```sql
CREATE TABLE products (
    id BIGINT PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    stock INT NOT NULL,
    INDEX idx_name (name)
) ENGINE = InnoDB;
```

为了让“名称不一定唯一”这个问题也能被观察，我们加入另外一件同名商品：

| id | name | stock |
|---:|---|---:|
| 42 | Keyboard | 100 |
| 57 | Keyboard | 80 |
| 71 | Mouse | 25 |

表里有两件叫 `Keyboard` 的商品。我们仍然最关心商品 42，但不能假设 `name` 天然唯一。

聚簇索引保存完整行数据。为便于理解，先把它想象成以下映射：

```text
聚簇索引（按 id 定位完整记录）

42 -> (Keyboard, 100)
57 -> (Keyboard,  80)
71 -> (Mouse,     25)
```

而 `INDEX idx_name(name)` 的叶子记录包含用于搜索的名称，以及对应记录的主键值。在当前单列索引和单列主键的例子里，可以画成：

```text
二级索引 idx_name（按 name 排列）

(Keyboard, 42)
(Keyboard, 57)
(Mouse,    71)
```

这个图是**教学化简**：它没有表示 InnoDB 记录头、页结构、隐藏字段及版本可见性处理，但对于理解回表已经足够。

现在执行：

```sql
SELECT id
FROM products
WHERE name = 'Keyboard';
```

只要从二级索引读取匹配的条目，我们就已经获得 `42` 和 `57`。查询只需要 `id`，不必为了获得 `id` 再专门访问聚簇索引记录。

但如果执行：

```sql
SELECT stock
FROM products
WHERE name = 'Keyboard';
```

二级索引告诉我们的只有：这两条记录的主键分别是 `42` 和 `57`。

它本身没有提供库存。

这就留下了明确的需求：**已经通过二级索引定位到主键，怎样继续取得只有完整行记录才保存的字段？**

## 2. 最直接的办法：用主键再找一次

我们其实早就学过根据主键读取商品。

前面几章围绕 `id = 42`，已经研究过聚簇索引的 B+ 树定位过程。

因此，现在没有必要引入一种完全陌生的数据结构。只要把两个已知步骤连接起来即可：

```text
查询：WHERE name = 'Keyboard'
                 |
                 v
       在二级索引中寻找名称
                 |
                 v
         得到主键 42、57
                 |
          +------+------+
          |             |
          v             v
    聚簇索引找 42   聚簇索引找 57
          |             |
          v             v
       stock=100     stock=80
          |             |
          +------+------+
                 |
                 v
            返回结果
```

对于匹配到的每条二级索引记录，使用其中的主键值，再在聚簇索引中定位完整行。这种额外的聚簇索引查找，通常被称为**回表**。

这里的“两次查找”指两种索引访问阶段，**不是严格的两次磁盘 I/O**。每棵 B+ 树都可能经过多层页面；相关页面也可能已经在 Buffer Pool 中。因此，回表可能需要访问多个逻辑页面，也可能不发生实际磁盘读取。

我们先用一个最小 Python 模型验证这个思路，再去 MySQL 源码里找证据。

## 3. 用一个可运行的最小模型还原回表

先将完整记录保存在以主键为键的字典里：

```python
products = {
    42: {"id": 42, "name": "Keyboard", "stock": 100},
    57: {"id": 57, "name": "Keyboard", "stock": 80},
    71: {"id": 71, "name": "Mouse", "stock": 25},
}
```

上一章已经有了名称索引。我们在教学模型中将它写成：

```python
idx_name = {
    "Keyboard": [42, 57],
    "Mouse": [71],
}
```

注意这不是 InnoDB B+ 树源码，只是保留了“名称指向主键”这一关键关系。

现在，如果只查询 ID，可以直接返回二级索引保存的值：

```python
def select_ids(name):
    return idx_name.get(name, []).copy()

print(select_ids("Keyboard"))
# [42, 57]
```

但查询库存时，就不得不进行第二次查找：

```python
def select_stocks(name):
    stocks = []
    clustered_lookups = 0

    for product_id in idx_name.get(name, []):
        product = products[product_id]
        clustered_lookups += 1
        stocks.append(product["stock"])

    return stocks, clustered_lookups

print(select_stocks("Keyboard"))
# ([100, 80], 2)
```

输出中的 `2` 不是磁盘读取次数，而是这个模型主动进行的**两次聚簇记录字典查询**。

这里我们没有做任何优化，只是复用了之前已经掌握的按主键查找。

然而，新的矛盾出现了。

查询 `id` 不需要第二次查找；查询 `stock` 却需要。问题并不在于 WHERE 条件发生变化，它始终是 `name = 'Keyboard'`。

**变化的只是我们要求返回的列。**

这说明：是否需要回表，不仅和 `WHERE` 使用什么索引有关，还和查询最终需要哪些列有关。

现在去源码中验证这条“二级索引主键 → 聚簇索引完整记录”的路径。

---

## 4. 真正的 InnoDB 怎样把二级索引记录变成主键查找键？

本章固定源码为 MySQL 8.4.0，Git Tag `mysql-8.4.0`。

这次先看一个相对独立、能够完整解释关键动作的源码位置：

[storage/innobase/row/row0row.cc（MySQL 8.4.0）](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/row/row0row.cc)

查找函数：

```cpp
row_build_row_ref(...)
```

源码注释明确说明，它从二级索引记录构造用于搜索聚簇索引记录的行引用。

为什么需要这个转换？

因为二级索引中读到的是一条**索引记录**；下次查找的目标却是以聚簇索引的键组织的完整行。如果直接把二级索引记录的全部字段作为搜索键，字段顺序和含义都不一定符合聚簇索引的要求。

对于我们的例子：

```text
二级索引记录： (Keyboard, 42)
聚簇索引查找键： (42)
```

真正需要提取的是 `42`。

### 4.1 先找到聚簇索引

**真实源码节选（MySQL 8.4.0，`row_build_row_ref()`；省略无关语句）：**

```cpp
clust_index = table->first_index();

ref_len = dict_index_get_n_unique(clust_index);

ref = dtuple_create(heap, ref_len);
```

这里只需要理解三个动作。

`table->first_index()` 取得当前表的聚簇索引。对于我们的实验表，它就是以 `id` 为主键的索引。

`dict_index_get_n_unique(clust_index)` 取得构造聚簇索引查找键需要的字段数量。

`dtuple_create()` 为查找键准备一个结构化的元组。

我们最初只知道目标是一条二级索引记录；现在，代码已经开始准备适合聚簇索引的查找键。

### 4.2 从二级索引记录中取出对应字段

继续查看同一个函数内的循环。

**真实源码节选（保持原有调用语义，省略循环外语句及部分处理）：**

```cpp
pos = dict_index_get_nth_field_pos(index, clust_index, i);

field = rec_get_nth_field(index, rec, offsets, pos, &len);

dfield_set_data(dfield, field, len);
```

`index` 是当前二级索引，`rec` 是已经读取到的二级索引记录。

第一行确定：聚簇索引搜索键里的第 `i` 个字段，在二级索引记录中的什么位置。

第二行从二级索引记录取出字段值。

第三行把这个值放进刚刚创建的聚簇索引查找元组中。

落到商品 42 的例子中，就是：

```text
二级索引记录：(Keyboard, 42)
                       |
                  取出主键 42
                       |
                       v
              聚簇索引查找元组：(42)
```

于是我们看到了一个真实存在的转换过程。

InnoDB 不是依赖“二级索引恰好保存了物理行地址”来找到完整记录；它可以利用二级索引携带的聚簇索引键值，重新定位。

这也解释了上一章为什么强调：InnoDB 二级索引记录需要包含主键列。

**这个字段不是只为排序存在，它还能把二级索引查找和聚簇索引查找连接起来。**

## 5. 查找键准备好了，谁负责第二次定位？

拿到聚簇索引查找元组后，下一步自然应该是去聚簇索引搜索。

在同一份源码中，可以继续找到：

```cpp
row_search_on_row_ref(...)
```

该函数接收构造好的行引用，执行聚簇索引查找。

**真实源码节选（MySQL 8.4.0，`row_search_on_row_ref()`）：**

```cpp
index = table->first_index();

pcur->open(index, 0, ref, PAGE_CUR_LE, mode, mtr, UT_LOCATION_HERE);

low_match = pcur->get_low_match();

rec = pcur->get_rec();
```

我们只研究当前正常查找所需要的含义。

第一行再次确定搜索目标是聚簇索引。

`pcur->open(...)` 把搜索游标定位到与 `ref` 相关的位置。`ref` 就是上一步从二级索引记录中提取出来的查找元组。

随后取出匹配信息和当前位置的记录。

代码还会检查游标是否处于有效的用户记录，以及匹配字段是否足够。换句话说，**定位到某个位置，不等于一定找到了合法匹配的完整行**。

现在，我们已经有了两个可明确解释的源码动作：

```text
row_build_row_ref()
  从二级索引记录提取聚簇索引键
              |
              v
row_search_on_row_ref()
  用这个键定位聚簇索引记录
```

在 `row0row.cc` 中，还有一个把两者连起来的函数：

**真实源码节选（MySQL 8.4.0，`row_get_clust_rec()`）：**

```cpp
ref = row_build_row_ref(ROW_COPY_POINTERS, index, rec, heap);

auto found = row_search_on_row_ref(&pcur, mode, table, ref, mtr);

clust_rec = found ? pcur.get_rec() : nullptr;
```

三行就足以解释这一层设计：构造搜索键，查找聚簇记录，返回找到的记录指针或空值。

需要特别说明：**`row_get_clust_rec()` 是一个真实的辅助函数，证明了这种二次定位机制的实现；不能把它直接画成每条普通 SELECT 都必经的函数。** 普通 MySQL 一致性读取还涉及 `row_search_mvcc()` 等访问流程，以及版本可见性和删除标记处理。本章不把不同代码路径拼接成一条未经确认的精确调用栈。

这不是回避源码，而是区分两种问题：

- **为什么能回表？** 上面三个真实函数已经给出了清楚的实现证据。
- **特定 SELECT 在所有情况下究竟经过哪些函数？** 还需要结合实际访问模式、快照、优化结果追踪；我们将在 SELECT 端到端章节继续核实。

至此，我们已经理解：二级索引中有主键，InnoDB 就有办法重新定位完整的聚簇索引记录。

但从 Python 实验来看，我们仍然多做了一次查找。有没有办法避免？

---

## 6. 只查询 ID 时，为什么可以不回表？

回到这条 SQL：

```sql
SELECT id
FROM products
WHERE name = 'Keyboard';
```

二级索引 `idx_name(name)` 中本来就保存着 `name` 和主键 `id`。

对当前查询而言，筛选条件用到了 `name`，返回结果需要 `id`。这两项都能从同一棵二级索引中取得。

如果系统仍然每次都根据 `id` 再去聚簇索引读取完整行，就做了没必要的工作。

所以，我们真正需要的不是“二级索引永远只查一次”，而是一个更具体的判断：

> 当前查询所需要的列，是否已经能够从选用的索引中取得？

如果可以，就有机会避免因为取列而额外访问聚簇索引。这时，称这个索引**覆盖了当前查询**。

注意这里的措辞：**覆盖索引是索引相对某条查询的属性，不是一种与 B+ 树并列的新数据结构。**

`idx_name(name)` 对下面的查询可以是覆盖索引：

```sql
SELECT id
FROM products
WHERE name = 'Keyboard';
```

但对下面的查询就不是：

```sql
SELECT stock
FROM products
WHERE name = 'Keyboard';
```

因为 `stock` 不在这个索引记录的可用列中。

同一棵索引，查询要求一变，是否覆盖也会改变。

## 7. 从源码验证：MySQL 会关心是否需要访问聚簇索引

这次查看一个与 MySQL/InnoDB 读取接口有关的数据结构：

[storage/innobase/include/row0mysql.h（MySQL 8.4.0）](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/include/row0mysql.h)

找到 `row_prebuilt_t`，其中有一个字段：

**真实源码节选：**

```cpp
unsigned need_to_access_clustered : 1;
```

这个字段的源码注释明确指出：当通过二级索引获取列，而至少一个需要的列不在二级索引中时，设置为 `true`。

这里值得注意的不是这个变量名，而是它背后的**条件判断**：

```text
已经选择二级索引
       |
       v
查询所需列是否都能从该索引得到？
       |                      |
      是                     否
       |                      |
       v                      v
可尝试索引内取值         需要考虑访问聚簇记录
```

上图是解释该字段含义的教学逻辑图，不表示 `row_prebuilt_t` 只依据这个字段决定全部访问流程。

为什么要保留这样的标志？

因为数据库并非每次都需要把完整行送到上层。既然当前索引足以提供部分查询的所有结果列，就有机会避免不必要的额外查找。

当然，真实 InnoDB 对二级索引记录还要处理可见性、删除标记及具体访问模式。**“列已经覆盖”不应被绝对化为任何隔离级别、任何瞬间都不会查看聚簇索引。** 我们这里先理解最常见的索引取列决策。

那么，如果我们确实需要返回 `stock`，有没有办法让这个查询也被索引覆盖？

## 8. 如果希望库存查询也被覆盖，可以怎样设计索引？

最直接的想法是：既然 `idx_name` 只有 `name` 与 `id`，那就把 `stock` 也放进去。

例如，新建一个复合索引：

```sql
CREATE INDEX idx_name_stock
ON products (name, stock);
```

对于当前商品表，InnoDB 的这个二级索引除了显式列 `(name, stock)`，仍然会包含主键 `id`，可以教学化简为：

```text
(name, stock, id)

(Keyboard,  80, 57)
(Keyboard, 100, 42)
(Mouse,     25, 71)
```

这里的顺序和原来的 `idx_name(name)` 不一定相同。复合索引按自己的键列顺序组织数据；SQL 如果没有 `ORDER BY`，也不能假设查询输出顺序固定。

再看查询：

```sql
SELECT stock
FROM products
WHERE name = 'Keyboard';
```

当前索引中已有：

- 过滤需要的 `name`；
- 返回需要的 `stock`。

因此，这条查询具备通过 `idx_name_stock` 完成索引覆盖读取的条件，不需要为了取 `stock` 再主动进行一次聚簇索引查找。

把两种索引方案放在一起，差别就很清楚了：

```text
只有 idx_name(name)：

(name, id) -> 取得 id -> 聚簇索引 -> 取得 stock

使用 idx_name_stock(name, stock)：

(name, stock, id) -> 直接取得 stock
```

这就是覆盖索引能够减少回表的核心原因。

但请不要急着得出“把所有查询字段都加入索引更好”的结论。

我们整个系列的核心业务还有库存扣减：

```sql
UPDATE products
SET stock = stock - 1
WHERE id = 42 AND stock > 0;
```

如果把 `stock` 放进二级索引，那么每次 `stock` 改变，相关二级索引记录也可能需要维护。索引越宽，还会带来额外的空间和缓存开销。

**覆盖索引减少了特定读取路径上的工作，但往往让索引本身更昂贵。**

这是一种权衡，而不是免费优化。

---

## 9. 用真实 MySQL 的 EXPLAIN 比较两种情况

下面给出一个可重复的实验，不修改原先用于库存扣减的 `products` 表，而是在同一实验数据库中创建一个独立表。

这样既能保留商品 42 的业务背景，又不影响前几章的后续复现。

> **实验状态**：以下 SQL 已按 MySQL 8.4 的语法与官方行为设计，但本章没有在真实 MySQL 服务上执行，因此结果均标记为“预期”。

### 9.1 准备表和数据

```sql
CREATE DATABASE IF NOT EXISTS mysql_source_lab;
USE mysql_source_lab;

DROP TABLE IF EXISTS products_cover_lab;

CREATE TABLE products_cover_lab (
    id BIGINT NOT NULL PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    stock INT NOT NULL,
    INDEX idx_name (name)
) ENGINE = InnoDB;

INSERT INTO products_cover_lab (id, name, stock)
VALUES
    (42, 'Keyboard', 100),
    (57, 'Keyboard', 80),
    (71, 'Mouse', 25);
```

`DROP TABLE` 会删除同名实验表，切勿直接用于含有真实业务数据的库。

### 9.2 先查二级索引中已经有的列

执行：

```sql
EXPLAIN FORMAT=TRADITIONAL
SELECT id
FROM products_cover_lab FORCE INDEX (idx_name)
WHERE name = 'Keyboard';
```

这里使用 `FORCE INDEX (idx_name)`，只是为了让实验集中比较同一个索引的取列行为。在真实业务中，不应为了展示效果而长期固定索引提示。

重点观察 `EXPLAIN` 的 `key` 与 `Extra`：

| 关注字段 | 预期现象 | 原因 |
|---|---|---|
| `key` | `idx_name` | 强制选择名称索引进行相关访问 |
| `Extra` | 包含 `Using index` | 查询需要的 `id` 已包含在二级索引记录中 |

`Using index` 在这里表达索引覆盖取列，不等同于“只读了一个索引页”。

### 9.3 只修改返回列：从 id 换成 stock

```sql
EXPLAIN FORMAT=TRADITIONAL
SELECT stock
FROM products_cover_lab FORCE INDEX (idx_name)
WHERE name = 'Keyboard';
```

WHERE 条件没有变，还是 `name = 'Keyboard'`。

变化的只是返回列：由 `id` 变成 `stock`。

此时 `idx_name` 不包含 `stock`，预期 `Extra` 不再因为索引覆盖而显示 `Using index`。如果访问按这个索引执行，为了获得库存值，需要进一步访问完整记录。

请注意：`Extra` 不显示 `Using index`，不能单独作为“真实执行了多少次聚簇索引定位”的精确计数证据。执行计划只描述访问策略，不是内部函数调用统计。

### 9.4 加入 stock，让查询具备覆盖条件

```sql
CREATE INDEX idx_name_stock
ON products_cover_lab (name, stock);
```

然后显式选用它：

```sql
EXPLAIN FORMAT=TRADITIONAL
SELECT stock
FROM products_cover_lab FORCE INDEX (idx_name_stock)
WHERE name = 'Keyboard';
```

现在，预期 `key = idx_name_stock`，而 `Extra` 可以显示 `Using index`。

同样的查询需求，只因为索引包含的列不同，取列方式就发生了变化。

可以再执行一次真正的查询：

```sql
SELECT id, name, stock
FROM products_cover_lab
WHERE name = 'Keyboard'
ORDER BY id;
```

预期结果：

```text
+----+----------+-------+
| id | name     | stock |
+----+----------+-------+
| 42 | Keyboard |   100 |
| 57 | Keyboard |    80 |
+----+----------+-------+
```

这个 `ORDER BY id` 只用于让展示结果稳定，不是用来证明覆盖索引的必要条件。

最后，把两种 `EXPLAIN` 放在一起比较，而不是只看一次运行耗时：

| 查询 | 使用索引 | 是否包含 stock | 预期覆盖标志 |
|---|---|---|---|
| `SELECT id ... WHERE name = ...` | `idx_name` | 不需要 stock | `Using index` |
| `SELECT stock ... WHERE name = ...` | `idx_name` | 否 | 非覆盖 |
| `SELECT stock ... WHERE name = ...` | `idx_name_stock` | 是 | `Using index` |

这就从外部验证了本章最核心的设计差异。

## 10. 再用 Python 验证“额外定位”到底减少了多少

为了让数据流更加直观，下面我们给教学模型增加一个计数器，专门统计**为了取库存而进行的聚簇索引查询**。

这不等于磁盘 I/O 统计，也不等于 MySQL 优化器的成本模型。

**教学化简，可独立运行：**

```python
from collections import defaultdict

products = {
    42: {"id": 42, "name": "Keyboard", "stock": 100},
    57: {"id": 57, "name": "Keyboard", "stock": 80},
    71: {"id": 71, "name": "Mouse", "stock": 25},
}

idx_name = defaultdict(list)
idx_name_stock = defaultdict(list)

for product in products.values():
    idx_name[product["name"]].append(product["id"])
    idx_name_stock[product["name"]].append(
        (product["stock"], product["id"])
    )

for ids in idx_name.values():
    ids.sort()
for entries in idx_name_stock.values():
    entries.sort()


def find_by_name_return_stock(name):
    result = []
    clustered_lookups = 0

    for product_id in idx_name.get(name, []):
        clustered_lookups += 1
        result.append(products[product_id]["stock"])

    return result, clustered_lookups


def find_by_name_return_id(name):
    return idx_name.get(name, []).copy(), 0


def covered_stock(name):
    return [
        stock
        for stock, product_id in idx_name_stock.get(name, [])
    ], 0


print("原索引查 stock:", find_by_name_return_stock("Keyboard"))
print("原索引查 id:", find_by_name_return_id("Keyboard"))
print("覆盖索引查 stock:", covered_stock("Keyboard"))

assert find_by_name_return_stock("Keyboard") == ([100, 80], 2)
assert find_by_name_return_id("Keyboard") == ([42, 57], 0)
assert covered_stock("Keyboard") == ([80, 100], 0)

print("全部断言通过")
```

**本章已实际运行，得到：**

```text
原索引查 stock: ([100, 80], 2)
原索引查 id: ([42, 57], 0)
覆盖索引查 stock: ([80, 100], 0)
全部断言通过
```

注意第二种索引下 `stock` 的返回顺序是 `80, 100`，而不是 `100, 80`。这是因为教学模型按照 `(stock, id)` 对同名条目排序。SQL 如果没有指定 `ORDER BY`，也不能把返回顺序当作语义保证。

这次实验的结论很明确：

- 使用只含名称与主键的索引，查询两个商品的库存时，模型需要再访问两次完整记录。
- 查询商品 ID 时，索引本身就能提供结果，不需要额外定位。
- 把库存列也放入索引后，同一库存查询可以在索引模型中直接完成。

但我们不能说“真实 MySQL 因此减少两次磁盘 I/O”——这需要真实存储引擎运行数据支持。

---

## 11. 还有一个容易误会的地方：`Using index` 不等于 `Using index condition`

如果继续使用 `EXPLAIN`，可能遇到两个看起来很像的输出：

```text
Using index
```

以及：

```text
Using index condition
```

它们不是一个意思。

`Using index` 主要表达：当前查询需要的列，可以从索引中直接取得，即索引覆盖。

`Using index condition` 则与另一种优化有关：先在索引层判断能够判断的条件，减少不必要的完整记录读取。它通常对应 **Index Condition Pushdown（ICP）**。

为什么需要区分？

因为即使不能完全避免回表，也可以先用二级索引字段排除一部分候选记录，从而减少回表次数。

不过，ICP 不是本章必须掌握的新机制。我们目前只需要知道：

**覆盖索引是让某些查询不必为了取列而回表；索引条件下推是让不能完全覆盖的查询尽量少回表。**

至于优化器什么时候启用这些优化、怎样比较不同执行路径的成本，我们下一阶段会结合真实查询计划研究。

## 12. 把本章重新放回商品购买的主线

我们一直在研究商品 42。

最开始，我们用主键读取它：

```sql
SELECT stock FROM products WHERE id = 42;
```

前面的章节已经解释了如何通过聚簇索引定位完整记录。

后来，用户不知道商品 ID，只知道名称：

```sql
SELECT stock FROM products WHERE name = 'Keyboard';
```

这迫使我们建立另一条以商品名称为入口的查找路径。

但二级索引通常只保存索引列和聚簇索引键，而不保存所有业务列。

因此，当需要返回 `stock` 时，我们可能不得不先获得 `id`，再通过聚簇索引找到完整记录。

如果查询所需的列本来都在索引中，那么这次额外读取便可能省掉；如果经常需要库存，也可以把 `stock` 纳入适当设计的复合索引。

从这条连续的因果关系中，我们现在可以解释三个概念：

```text
没有名称索引：
    可能需要扫描商品记录

有 idx_name(name)：
    能按名称快速定位主键，但取 stock 可能回表

有 idx_name_stock(name, stock)：
    按名称读取 stock 有机会直接由索引覆盖
```

这里最重要的认识是：

> 一个索引既决定“怎样找到候选记录”，也影响“找到候选记录之后，还需不需要读取完整行”。

而这两项收益不一定在每个查询里同时成立。

## 13. 下一章：如果有多个索引，MySQL 应该选哪一个？

我们现在为商品表建立了两个二级索引：

```sql
INDEX idx_name (name)
INDEX idx_name_stock (name, stock)
```

为了比较两者，我们在实验中使用 `FORCE INDEX`，明确要求 MySQL 选择哪个索引。

但真实业务 SQL 通常不会每次都这样写。

如果用户提交：

```sql
SELECT stock
FROM products
WHERE name = 'Keyboard';
```

MySQL 必须自己判断：

应该扫描整张表，使用 `idx_name` 后回表，还是使用能够覆盖查询的 `idx_name_stock`？

如果 `Keyboard` 对应一条记录和一百万条记录，选择会一样吗？

如果复合索引特别大，覆盖索引是不是一定比其他方式更快？

到这里，我们终于遇到了一个无法仅靠“某个索引能不能使用”回答的问题：**存在多条合法访问路径时，该选择哪条？**

因此，下一章才有必要进入查询优化器。

**下一章：第 18 章《从多种访问路径到查询优化器——MySQL 怎样选择执行计划？》**

---

## 本章源码与参考资料

固定版本：MySQL Server 8.4.0，Git Tag `mysql-8.4.0`。

### 真实源码定位

1. [`storage/innobase/row/row0row.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/row/row0row.cc)：`row_build_row_ref()`、`row_search_on_row_ref()`、`row_get_clust_rec()`；用于核对“二级索引记录 → 聚簇索引查找键 → 聚簇记录”的具体实现。
2. [`storage/innobase/include/row0mysql.h`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/include/row0mysql.h)：`row_prebuilt_t::need_to_access_clustered`；用于核对是否需要访问聚簇索引的字段条件。
3. [`storage/innobase/row/row0sel.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/row/row0sel.cc)：`row_search_mvcc()` 所在文件；后续研究普通 SELECT 的实际执行链、版本检查及聚簇记录读取时继续深入。

### 官方文档

- [MySQL 8.4：聚簇索引与二级索引](https://dev.mysql.com/doc/refman/8.4/en/innodb-index-types.html)
- [MySQL 8.4：EXPLAIN 输出含义](https://dev.mysql.com/doc/refman/8.4/en/explain-output.html)
- [MySQL 8.4：二级索引对主键列的扩展](https://dev.mysql.com/doc/refman/8.4/en/index-extensions.html)
- [MySQL 8.4：索引条件下推](https://dev.mysql.com/doc/refman/8.4/en/index-condition-pushdown-optimization.html)

**验证边界**：Python 模型已实际运行、断言通过；MySQL SQL 仅给出可复现步骤与预期现象；真实源码引用来自固定版本，教学结构图与实际执行栈明确区分。
