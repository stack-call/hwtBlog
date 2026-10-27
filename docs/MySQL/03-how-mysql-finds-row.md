---
title: "03｜从逐条扫描到索引：MySQL 怎样找到商品 42？"
sidebar_position: 4
slug: /mysql/03-how-mysql-finds-row
description: "接着单表 UPDATE 的真实执行路径，比较全表扫描与主键定位，阅读 MySQL 8.4.0 的行读取迭代器和 InnoDB 接口。"
tags: [MySQL, InnoDB, 索引, UPDATE, 源码精读]
---

# 从逐条扫描到索引：MySQL 怎样找到商品 42？

上一章，我们跟着这条 SQL：

```sql
UPDATE products
SET stock = stock - 1
WHERE id = 42 AND stock > 0;
```

找到了 MySQL 处理单表更新的入口：

```cpp
Sql_cmd_update::update_single_table(THD *thd)
```

此前，我们回答了一个问题：**MySQL 怎样把 SQL 文本变成一次能够执行的 UPDATE 命令？**

现在，命令已经可以执行了。但它还没有完成购买任务。

如果 `products` 表里有一千万件商品，MySQL 应该怎样找到 `id = 42` 的那一件？

它当然可以依次检查所有商品。但我们很快就会发现，这不是一个适合所有数据规模的方案。

本章的目标不是一次学完 B+ 树，而是沿着这次 UPDATE 的真实执行过程，弄清三个问题：

1. **如果逐条扫描，谁负责读取下一条记录？**
2. **如果已经知道商品 ID，MySQL 能否通过索引直接缩小查找范围？**
3. **这两种读取方式，在真实源码里怎样接上 UPDATE？**

## 1. 最简单的办法：逐条检查商品

先忘掉 MySQL，回到我们自己的小程序。

假设商品记录保存在列表里：

```python
products = [
    {"id": 7, "stock": 20},
    {"id": 19, "stock": 35},
    {"id": 42, "stock": 100},
    {"id": 80, "stock": 50},
]
```

要购买商品 42，我们可以直接遍历：

```python
def buy(products, product_id):
    for product in products:
        if product["id"] == product_id:
            if product["stock"] <= 0:
                return 0
            product["stock"] -= 1
            return 1
    return 0

assert buy(products, 42) == 1
assert products[2]["stock"] == 99
```

这段程序完全正确。它先读取记录，再检查是否为目标商品；只有找到符合条件的记录，才执行修改。

对应到数据库，我们需要把两件事分清：

- **寻找记录**：哪些记录需要被读取、检查？
- **修改记录**：找到且满足条件之后，怎样把库存从 100 改为 99？

本章先研究第一件事。修改数据页和日志暂时不展开。

四条商品记录时，逐条遍历并不麻烦。但如果目标记录在一百万条商品的末尾，程序就要做一百万次 ID 比较。

因此，当前缺少的能力不是“更快地执行减一”，而是**更快地排除不可能是商品 42 的记录**。

## 2. 一个最小实验：比较次数到底差多少？

假设商品按 ID 排序。我们保留最初的逐条遍历，同时增加一种利用有序性缩小范围的查找方式。

```python
products = [
    {"id": i, "stock": 100}
    for i in range(1, 100_001)
]


def linear_find(rows, target_id):
    comparisons = 0
    for row in rows:
        comparisons += 1
        if row["id"] == target_id:
            return row, comparisons
    return None, comparisons


def binary_find(rows, target_id):
    left, right = 0, len(rows) - 1
    comparisons = 0
    while left <= right:
        mid = (left + right) // 2
        comparisons += 1
        current_id = rows[mid]["id"]
        if current_id == target_id:
            return rows[mid], comparisons
        if current_id < target_id:
            left = mid + 1
        else:
            right = mid - 1
    return None, comparisons


row1, count1 = linear_find(products, 100_000)
row2, count2 = binary_find(products, 100_000)

assert row1 == row2
print("逐条扫描:", count1)
print("二分查找:", count2)
```

**实际执行结果：**

```text
逐条扫描: 100000
二分查找: 17
```

为什么同样是十万条记录，差距会这么大？

逐条扫描时，每检查一条记录，通常只能排除这一条。

而二分查找利用了数据的有序性。比较中间记录的 ID 后，就可以排除大约一半的候选范围。

因此，真正产生收益的是：**数据的组织方式允许我们跳过大量无关记录。**

但是，请不要由此得出“MySQL 索引就是一个有序 Python 数组”的结论。这个实验只解释为什么需要支持快速定位的数据组织方式；真实 InnoDB 的主键索引使用 B+ 树，涉及磁盘页面、插入维护等问题，我们下一章再研究。

现在的问题是：MySQL 真实代码里，是否也存在“逐条读”和“按键找”这两种路径？

## 3. 回到 UPDATE：它需要先取得候选记录

源码统一固定为 **MySQL 8.4.0，Git Tag `mysql-8.4.0`**。

打开 [`sql/sql_update.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/sql_update.cc#L346)，找到 `Sql_cmd_update::update_single_table()`。

这个函数很长，但我们不需要一开始就解释权限、触发器、排序和错误处理。

先找一个最关键的动作：**为了更新记录，代码必须先把记录读出来。**

在常见的单表更新路径中，如果不需要特殊排序，也没有修改用于搜索的索引键，它会初始化一个读取迭代器：

**真实源码节选**（`sql/sql_update.cc`，第 767–773 行）：

```cpp
iterator =
    init_table_iterator(thd, table, range_scan,
                        /*table_ref=*/nullptr, /*position=*/nullptr,
                        /*ignore_not_found_rows=*/false,
                        /*count_examined_rows=*/false);
```

这段调用保留了 MySQL 8.4.0 的原始参数，省略的是它周围的其他执行分支。

这里出现一个新词：**迭代器（Iterator）**。

现在只需要把它理解成一个非常简单的接口：

> 调用一次 `Read()`，尝试取得下一条候选记录。

它不等于某种固定的磁盘读取算法。不同读取方式可以使用不同类型的迭代器。

接下来，同一个函数里存在这样一行真实源码：

```cpp
error = iterator->Read();
```

位置：[`sql/sql_update.cc` 第 810–812 行](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/sql_update.cc#L810-L812)。

这行代码由 `update_single_table()` 在读取循环中调用。它并不负责将库存减一，而是先请求一条候选记录。

取得记录后，剩余的 `WHERE` 条件还可能需要被检查。如果记录不符合条件，代码会跳过它；若符合更新要求，后续才会计算新值并进入存储引擎的修改接口。

我们可以用下面的**概念伪代码**理解这个阶段：

```python
reader = choose_reader()  # 这里只是教学表达

while True:
    product = reader.read()
    if product is None:
        break
    if product["id"] != 42 or product["stock"] <= 0:
        continue
    product["stock"] -= 1
```

这里的 `choose_reader()` 并非 MySQL 真实函数，也不是说源码会逐字执行这个 Python 循环。真实 SQL 执行可能把部分筛选条件提前用于索引定位。

但它帮助我们把问题缩小到了最关键的地方：**读取迭代器究竟以什么方式找到候选记录？**

## 4. 如果选择表扫描，读取的是哪一个函数？

现在假设我们放弃通过 `id = 42` 定位，而是扫描整张商品表。

MySQL Server 需要一种可以连续读取记录的接口。

打开 [`sql/iterators/basic_row_iterators.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/iterators/basic_row_iterators.cc#L246-L280)。

先看 `TableScanIterator::Init()`。它会调用：

```cpp
int error = table()->file->ha_rnd_init(true);
```

这一步负责准备扫描。

之后，`TableScanIterator::Read()` 会调用：

```cpp
while ((tmp = table()->file->ha_rnd_next(m_record))) {
```

这两行是从 MySQL 8.4.0 对应函数中分别截取的真实源码。

其中 `table()->file` 代表当前表使用的存储引擎处理接口；在我们的实验表中，底层引擎是 InnoDB。

两次调用承担不同任务：

- `ha_rnd_init(true)`：初始化表扫描；
- `ha_rnd_next(...)`：从当前扫描位置取得记录。

注意，`ha_rnd_next()` 并不负责判断库存是否大于零。它的主要职责是把读取到的记录交给上层。

继续打开 [`storage/innobase/handler/ha_innodb.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/handler/ha_innodb.cc#L10245-L10272)，查找 `ha_innobase::rnd_next()`。

它区分“首次扫描”和“继续扫描”。我们只摘取两个关键动作：

```cpp
error = index_first(buf);
```

以及：

```cpp
error = general_fetch(buf, ROW_SEL_NEXT, 0);
```

第一次需要找到扫描起始记录；后续调用则沿当前扫描方向继续读取。

到这里，我们终于能把扫描这条路径连接起来：

```text
update_single_table()
        |
        v
读取迭代器的 Read()
        |
        v
TableScanIterator::Read()
        |
        v
handler::ha_rnd_next()
        |
        v
InnoDB ha_innobase::rnd_next()
        |
        v
返回一条候选记录
```

这是真实源码中**表扫描分支**的关键调用关系，并不代表我们的 UPDATE 一定会选择该分支。

还有一个值得注意的细节：在 InnoDB 的表扫描实现中，首次读取居然调用了 `index_first()`。

为什么“扫描整张表”也出现了索引？

因为 **InnoDB 的表数据本身就组织在聚簇索引中**。对于存在主键的 `products` 表，记录按主键组织在聚簇索引里。全表扫描可以沿该结构逐条读取，而不是必须先把索引丢掉、再去扫描一份独立的无序数据文件。

所以，“全表扫描”和“使用索引定位”的区别，不是底层有没有接触到索引结构，而是：

**能否利用 `id = 42` 这一已知条件直接缩小需要访问的记录范围。**

## 5. 既然知道 ID，为什么还要遍历？

回到当前 SQL：

```sql
UPDATE products
SET stock = stock - 1
WHERE id = 42 AND stock > 0;
```

条件里有一个非常明确的信息：`id = 42`。

而且，我们建表时已经声明：

```sql
id BIGINT PRIMARY KEY
```

这意味着 `id` 是主键。MySQL 可以利用主键索引定位符合该键值的记录。

但要留意两层不同的问题：

1. **索引存在吗？** 对于当前表，存在，名字是 `PRIMARY`。
2. **本次访问是否选择了它？** 这需要根据语句和优化结果来判断，不能只因为有索引就自动宣称使用了索引。

对于此处简单的主键等值条件，利用主键定位是非常自然的访问方式。

现在，我们不先讲查询优化器的全部设计。只追踪一个已经决定使用索引范围访问的情况。

## 6. 真正的索引读取从哪里发生？

回到 `Sql_cmd_update::update_single_table()`。

它并不是只会创建 `TableScanIterator`。函数会考虑 `WHERE` 条件，并通过 `test_quick_select()` 等逻辑准备可能的索引范围访问路径。

相关源码：[`sql/sql_update.cc` 第 469–495 行](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/sql_update.cc#L469-L495)。

这里出现 `range_scan`：可以暂时把它理解成**已经准备好的某种索引范围读取方案**。当前阶段不研究它的内部结构。

在无需特殊排序的普通分支中，`init_table_iterator()` 使用这个信息构造迭代器。

继续查看 [`sql/sql_executor.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/sql_executor.cc#L4434-L4492)。

这里有一个非常直接的分叉：

- 如果已有 `range_scan`，使用对应的访问路径；
- 如果没有相关路径且不属于其他特殊情况，可以建立表扫描访问路径。

也就是说，`update_single_table()` 继续调用 `Read()` 的形式可以保持不变，**真正变化的是底下那种读取迭代器**。

这正是迭代器这一层抽象在当前问题上的作用：上层只需要不断取候选记录，不必把所有读取算法都写进库存修改循环。

当索引范围读取路径被采用时，可以进入 [`IndexRangeScanIterator::Read()`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/range_optimizer/index_range_scan.cc#L328-L357)。

它的关键调用是：

```cpp
int result = file->ha_multi_range_read_next(&dummy);
```

这个函数向存储引擎请求索引范围中的记录。

为什么叫“范围”，我们明明只查商品 42？

因为在统一的索引访问接口里，`id = 42` 可以被表示成一个起点、终点都受到约束的查找范围。具体实现还需要处理精确匹配、前缀和其他范围条件；这些复杂情形暂时不展开。

沿默认的范围读取实现继续向下，可以在 [`sql/handler.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/handler.cc#L6019-L6061) 看到 `handler::multi_range_read_next()` 调用 `read_range_first()` 取得某个范围的第一条记录。

而 [`handler::read_range_first()`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/handler.cc#L6823-L6848) 在给定起始键时，会通过 `ha_index_read_map()` 去定位索引记录。

最后，我们来到 [`ha_innobase::index_read()`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/handler/ha_innodb.cc#L9640-L9760)。

这个函数接收查找键、键长度和查找模式，然后将 MySQL Server 一侧的键值转换成 InnoDB 内部搜索能够使用的格式，并继续调用底层记录搜索逻辑。

它真正解决的是：**把“请用这个键查找”转换成 InnoDB 内部可以执行的记录定位操作。**

因此，在相关默认范围读取分支中，我们能够观察到这样的源码路径：

```text
update_single_table()
        |
        v
init_table_iterator()
        |
        v
IndexRangeScanIterator::Read()
        |
        v
ha_multi_range_read_next()
        |
        v
read_range_first()
        |
        v
ha_index_read_map()
        |
        v
InnoDB index_read()
        |
        v
按索引条件取得候选记录
```

**这是一条有条件的源码分支，不应被误读为任意 UPDATE 在任意配置下都严格经过这些函数。** MySQL 有多种索引访问实现，范围读取也可能采取其他策略。这里核对的是能清楚说明“按键定位”机制的一条路径。

同时，我们没有把 `index_read()` 后面更深层的记录搜索、页面访问和 B+ 树导航全部展开，因为那会提前回答后续章节的问题。

现在已经足够说明：MySQL 不需要在 `UPDATE` 里手写一整段“从第 1 条记录逐个找 ID”的循环；它可以选择适当的访问路径，由读取迭代器和存储引擎共同完成记录定位。

## 7. 两条读取路径，怎样回到同一次库存扣减？

现在，我们已经研究了两个分支。

一个是扫描：

```text
Read() → ha_rnd_next() → 下一条候选记录
```

一个是索引访问：

```text
Read() → 索引范围读取 → 按键定位候选记录
```

它们最终都要把候选记录交还给 `update_single_table()` 的读取循环。

上层随后处理尚未完成的条件检查，判断是否允许更新，再计算并写入新值。

例如，我们仍然执行：

```sql
UPDATE products
SET stock = stock - 1
WHERE id = 42 AND stock > 0;
```

即使某种方式找到了 `id = 42`，也不表示一定能够执行扣减。

如果 `stock = 0`，就必须让这一条记录不满足更新条件。

因此，**索引定位解决的是“到哪里找”，而 `WHERE` 条件与更新逻辑共同决定“这一行是否应当被修改”。**

这两件事不能混为一谈。

在源码里，它们也并不是同一个动作。`iterator->Read()` 提供候选记录；更新循环在适当的阶段检查条件，随后才可能调用存储引擎更新接口，例如 `ha_update_row()`。

本章不深入 `ha_update_row()` 的内部实现。我们只需要确认：**修改动作发生在记录被取得、并经过相应判断之后。**

## 8. MySQL 实验：不修改库存，也能比较访问路径

前面用 Python 确认了“逐条扫描”与“利用有序性缩小范围”的算法差别。

现在，我们想在 MySQL 中观察：对于**同一条库存 UPDATE**，是否能看到不同的候选访问路径？

只在第一章创建的独立实验库中运行以下 SQL：

```sql
USE mysql_source_lab;

SHOW INDEX FROM products;
```

在我们定义的表结构中，应该能看到名为 `PRIMARY` 的索引，列为 `id`。

接下来，使用 `EXPLAIN` 观察 UPDATE 的执行计划。它不会真正扣减库存：

```sql
EXPLAIN FORMAT=TRADITIONAL
UPDATE products
SET stock = stock - 1
WHERE id = 42 AND stock > 0;
```

关注下面几列：

| 字段 | 在本章主要看什么 |
| --- | --- |
| `type` | 计划选择的访问类型 |
| `possible_keys` | 哪些索引可能用于访问 |
| `key` | 最终选择了哪个索引 |
| `rows` | 估计需要检查多少行，并非真实逐行计数 |

我们最关心 `key` 是否为 `PRIMARY`，以及计划如何访问该表。不要预先把 `type` 固定成某个值；真实结果应以当前 MySQL 版本、数据规模及优化器的计划为准。

为了观察“不能拿主键作为查找索引”时的方案，我们可以在实验里临时添加索引提示：

```sql
EXPLAIN FORMAT=TRADITIONAL
UPDATE products IGNORE INDEX (PRIMARY)
SET stock = stock - 1
WHERE id = 42 AND stock > 0;
```

`IGNORE INDEX (PRIMARY)` 告诉优化器：不要把 `PRIMARY` 作为这个查询的候选查找索引。对于只有这一条索引的简单实验表，它通常会让扫描路径更直观。

还可以显式提示优先使用主键：

```sql
EXPLAIN FORMAT=TRADITIONAL
UPDATE products FORCE INDEX (PRIMARY)
SET stock = stock - 1
WHERE id = 42 AND stock > 0;
```

注意，`FORCE INDEX` 不是保证任何情况下都绝不扫描；它是改变优化器对访问方式的考虑，具体语义应以官方文档为准。

**请比较执行计划，而不是直接比较三次真实 UPDATE 的耗时。** 真实执行会改变库存，多次执行还会受到缓存、事务状态等因素影响；这不是本章需要解决的问题。

我们也不需要为了演示访问路径，直接改动生产库的索引或表结构。

另外，只有一条商品记录时，扫描与索引的性能差距通常不明显。此处的目标是观察**访问方案是否变化**，不是证明“加索引必然快 1000 倍”。

如果需要更明显的性能实验，可以在下一次独立实验中准备大量商品，再比较扫描和索引访问。但性能测量不应替代本章对真实源码的解释。

### 验证状态

本章的 Python 比较次数实验已实际运行，结果为 **100000 次与 17 次**。

MySQL `EXPLAIN` 实验给出的是可复现步骤；本文**没有**在真实 MySQL 8.4.0 实例中执行这些语句，因此不伪造计划输出。请根据自己环境中的 `key`、`type` 和 `rows` 观察结论。

源码函数及上述关键调用关系已经根据 MySQL 8.4.0 对应源文件逐项核对，但没有使用调试器记录这条 UPDATE 的实际运行栈。源码静态路径与一次实际运行的调试轨迹应明确区分。

## 9. 回看：这一次我们只增加了什么理解？

上一章结束时，我们知道：

```text
SQL 文本 → UPDATE 命令 → update_single_table()
```

却不知道它怎样找到商品 42。

这章先尝试逐条遍历，发现列表很小时完全可行，但当目标位于十万条记录的末尾，查找成本会迅速增长。

于是我们利用数据的有序性，证明一种能够排除大量候选记录的访问方式可以显著减少比较次数。

随后回到真实 MySQL 源码，我们没有直接跳到 B+ 树，而是先定位 UPDATE 如何**获取候选记录**：

```text
                 update_single_table()
                          |
                          v
                    iterator->Read()
                          |
                +---------+---------+
                |                   |
              表扫描             索引范围读取
                |                   |
          ha_rnd_next()      索引访问接口
                |                   |
                +---------+---------+
                          |
                          v
                     候选记录
                          |
                          v
                 条件检查与后续更新
```

这是一张教学化简的**控制流与访问方式示意图**，不是同一条 SQL 会并行执行两个分支，也不是所有底层函数的完整调用图。

现在我们能够清楚地区分：

- `UPDATE` **做什么**：决定如何处理满足条件的商品记录；
- 读取迭代器**怎样取得候选记录**：扫描，或根据索引条件定位；
- InnoDB **怎样完成底层读取**：通过具体的存储引擎接口访问内部数据结构。

但是，新的问题已经出现。

我们一直在说“根据主键索引找到记录”，却没有解释：**这个索引在磁盘里究竟是什么样子？**

既然二分查找已经能减少比较次数，MySQL 为什么不直接把所有记录保存成一个巨大的有序数组？

当商品记录增长到无法全部放进内存时，一次查找又需要多少次磁盘访问？

下一章，我们就从这个尚未解决的问题出发，研究真实 InnoDB 怎样组织磁盘上的数据。

**下一章：从内存记录到磁盘页面——为什么数据不能随意摆放？**

---

## 本章源码索引

固定版本：**MySQL 8.4.0 / `mysql-8.4.0`**。

| 源码位置 | 本章观察的职责 |
| --- | --- |
| [`sql/sql_update.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/sql_update.cc#L346) | 单表 UPDATE 如何选择并使用读取迭代器 |
| [`sql/sql_executor.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/sql_executor.cc#L4434) | 根据已准备的访问路径创建读取迭代器 |
| [`sql/iterators/basic_row_iterators.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/iterators/basic_row_iterators.cc#L246) | 表扫描如何逐条获取记录 |
| [`sql/range_optimizer/index_range_scan.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/range_optimizer/index_range_scan.cc#L328) | 索引范围读取迭代器 |
| [`sql/handler.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/handler.cc#L6019) | 范围读取如何连接索引访问接口 |
| [`storage/innobase/handler/ha_innodb.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/handler/ha_innodb.cc#L9640) | InnoDB 接收索引查找条件 |

相关官方文档：

- [MySQL 8.4：EXPLAIN Output Format](https://dev.mysql.com/doc/refman/8.4/en/explain-output.html)
- [MySQL 8.4：Index Hints](https://dev.mysql.com/doc/refman/8.4/en/index-hints.html)
- [MySQL 8.4：UPDATE Statement](https://dev.mysql.com/doc/refman/8.4/en/update.html)
