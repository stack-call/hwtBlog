---
title: "20｜从单表查询到 JOIN：为什么需要连接算法？"
sidebar_position: 21
slug: /mysql/20-why-join-algorithms
description: "承接 RowIterator 的单表读取，沿商品 42 与订单的关联查询，从最简单的双重遍历，逐步理解索引嵌套循环、Hash Join 及 MySQL 8.4.0 的真实源码。"
tags: [MySQL, InnoDB, JOIN, NestedLoop, HashJoin, 源码精读]
---

# 从单表查询到 JOIN：为什么需要连接算法？

上一章，我们已经顺着一条单表 `SELECT`，看到执行器怎样通过 `RowIterator` 取出商品记录。

假设商品 `42` 是键盘，当前库存为 `99`。此时，我们不仅想知道它还剩多少，还想看看**哪些订单购买了这件商品，以及这些订单对应的商品名称是什么**。

我们的商品信息保存在 `products`，购买记录保存在 `orders`。现在，查询要同时使用两张表的数据：

```sql
SELECT o.id, p.name, o.quantity
FROM orders AS o
JOIN products AS p
  ON o.product_id = p.id;
```

对于一个只会逐条读取单表记录的执行器来说，这个需求带来一个新的问题：

**从订单表取出一行之后，怎样找到它对应的商品行，并把两行组合成结果？**

这一章只围绕这个问题前进。先让最简单的方法工作，再制造性能问题；等有了新需求，再回到 MySQL 8.4.0 的真实执行器源码。

## 1. 先不使用 JOIN：读出两张表，再自己配对

我们先保留之前的商品案例，增加几条订单。

```python
products = [
    {"id": 7, "name": "Mouse"},
    {"id": 19, "name": "Monitor"},
    {"id": 42, "name": "Keyboard"},
    {"id": 80, "name": "Desk"},
]

orders = [
    {"id": 101, "product_id": 42, "quantity": 1},
    {"id": 102, "product_id": 7, "quantity": 2},
    {"id": 103, "product_id": 42, "quantity": 1},
    {"id": 104, "product_id": 80, "quantity": 1},
    {"id": 105, "product_id": 999, "quantity": 1},
]
```

订单 `101` 和 `103` 都购买了商品 `42`。订单 `105` 暂时找不到对应的商品，用它来检查普通 `INNER JOIN` 是否正确排除未匹配行。

我们现在需要返回 `(订单 ID, 商品名称, 数量)`。最简单的办法是：对每一条订单，从头到尾遍历商品，找到 `order.product_id == product.id` 的商品。

```python
# 教学实现：最朴素的 INNER JOIN

def nested_loop(orders, products):
    rows = []
    comparisons = 0

    for order in orders:
        for product in products:
            comparisons += 1
            if order["product_id"] == product["id"]:
                rows.append((
                    order["id"],
                    product["name"],
                    order["quantity"],
                ))

    return rows, comparisons


rows, comparisons = nested_loop(orders, products)
print(rows)
print("比较次数:", comparisons)
```

**实际执行的输出：**

```text
[(101, 'Keyboard', 1), (102, 'Mouse', 2),
 (103, 'Keyboard', 1), (104, 'Desk', 1)]
比较次数: 20
```

结果正确：每条订单与它对应的商品组合，找不到商品的 `105` 被排除。

这里我们暂时不引入“连接算法”的术语。刚才只做了一个最直观的动作：**读一条订单，再逐条尝试匹配商品**。

但它马上暴露了和前面单表扫描非常相似的问题。

## 2. 订单越来越多，为什么双重遍历会慢？

现在有 5 条订单、4 个商品，所以进行了 `5 × 4 = 20` 次比较。

如果是 10 万条订单、1 万件商品，在没有提前过滤且每条订单都扫描全部商品的简单模型里，需要进行 10 亿次候选比较。

症结并不是 Python 使用了两层 `for`，而是：

> 找到一条订单后，我们对商品表中**与这条订单无关的每一条记录**，又检查了一遍。

此前我们已经学习过主键索引。这里恰好可以利用这个旧知识，而不需要立刻发明新的数据结构。

既然 `products.id` 是主键，给定 `order.product_id = 42`，为什么还要扫描其他所有商品？

我们真正需要的是一种操作：

```text
给定订单 101 的 product_id = 42
                  |
                  v
通过商品主键寻找 id = 42
                  |
                  v
只读取 Keyboard，组合订单结果
```

换句话说，外侧每获得一条订单，内侧就执行一次**带有当前订单参数的索引查找**。

这依然是一个“外侧一条、内侧匹配”的循环，但内侧已经不再是全表遍历。

## 3. 第一处最小替换：把内侧扫描换成索引查找

先用 Python 字典模拟“根据主键快速找到一条商品”。它不是 InnoDB B+ 树，只用于直观展示**避免重复扫描**的收益。

```python
# 教学实现：先建立商品 ID -> 商品 的查找结构

def indexed_lookup(orders, products):
    by_id = {p["id"]: p for p in products}
    rows = []
    lookups = 0

    for order in orders:
        lookups += 1
        product = by_id.get(order["product_id"])
        if product is not None:
            rows.append((
                order["id"],
                product["name"],
                order["quantity"],
            ))

    return rows, lookups


indexed_rows, lookups = indexed_lookup(orders, products)
assert indexed_rows == rows
print("查询次数:", lookups)
```

**实际执行的输出：**

```text
查询次数: 5
```

结果仍然是四条成功关联的订单。

比较时要诚实地区分统计口径：前面的 `20` 是逐条比较次数，而这里的 `5` 是字典查找次数。它们不是同一种机器指令，也不能直接推断真实 MySQL 快了四倍。构建字典本身还需要一次遍历和额外内存。

这个实验只证明一件事：**如果已经有了按商品 ID 定位记录的结构，每条订单就不必重新扫描全部商品**。

真实 MySQL 恰好已经拥有商品主键索引。现在，我们去验证执行器怎样把两张表的读取过程组合起来。

---

## 4. 回到 MySQL：优化器如何把两个读取过程连起来？

本章统一使用：

- 仓库：[`mysql/mysql-server`](https://github.com/mysql/mysql-server)
- 版本：**MySQL 8.4.0**
- 固定标签：`mysql-8.4.0`
- 关键文件：`sql/join_optimizer/access_path.cc`、`sql/iterators/composite_iterators.cc`、`sql/iterators/ref_row_iterators.cc`

上一章我们学过，优化器选出的访问方案会成为 `AccessPath`，而 `CreateIteratorFromAccessPath()` 将方案转换成执行器真正使用的 `RowIterator`。

之前只读一张表，执行器拿到一个能逐条输出记录的迭代器就够了。

现在的 JOIN 需要**两个输入**：订单读取器和商品读取器；还需要一个负责协调它们的上层读取器。

在 [`sql/join_optimizer/access_path.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/join_optimizer/access_path.cc) 的 `CreateIteratorFromAccessPath()` 中，`AccessPath::NESTED_LOOP_JOIN` 分支会创建真实的 `NestedLoopIterator`。

**真实源码中的关键调用，保留核心构造表达式：**

```cpp
iterator = NewIterator<NestedLoopIterator>(
    thd, mem_root, std::move(job.children[0]),
    std::move(job.children[1]), param.join_type, param.pfs_batch_mode);
```

这个调用说明：新的迭代器不是直接从某个表获取所有结果，而是**持有两个已经准备好的子迭代器**。

其中外侧、内侧各自怎样读取表，可以由下面的访问路径决定。

用我们已经理解的概念画出当前的小图：

```text
             NestedLoopIterator
                /          \
               /            \
     订单读取迭代器      商品读取迭代器
                               |
                        按 product_id 查找
```

注意：这是教学化简图。真实优化器不一定选择 `orders` 为外侧，也不保证所有 JOIN 都使用嵌套循环。后面我们会看到另一种执行策略。

而且 `JOIN` 在 MySQL 源码中也可能是一个表示查询块优化、执行状态的 C++ 类名，**不能把它与 SQL 的 `JOIN` 关键字或 `NestedLoopIterator` 混为一谈**。

接下来，先沿着“优化器确实选择了嵌套循环”这条正常路径往下追踪。

## 5. `NestedLoopIterator` 是怎样轮流读取两边的？

打开 [`sql/iterators/composite_iterators.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/iterators/composite_iterators.cc)，找到：

```cpp
bool NestedLoopIterator::Init()
int NestedLoopIterator::Read()
```

这里最值得观察的不是函数长度，而是**谁在什么时候调用子迭代器**。

`Init()` 首先准备外侧输入，设置读取状态。此后，每次上层调用 `Read()`，它都会根据当前状态决定下一步。

在需要一条新的外侧记录时，关键调用是：

```cpp
m_source_outer->Read();
```

读取成功后，再初始化内侧：

```cpp
m_source_inner->Init();
```

最后从内侧取得匹配记录：

```cpp
m_source_inner->Read();
```

这些短片段均来自固定版本的 `NestedLoopIterator::Read()`；已省略错误处理、批模式和其他 JOIN 类型的分支。

为什么要对内侧再次调用 `Init()`？

因为当外侧订单从 `101` 变成 `102` 时，原来寻找 `product_id = 42` 的内侧查找，必须换成寻找 `product_id = 7`。如果继续使用上一条订单的读取状态，就会得到错误的配对结果。

把源码动作和我们的数据连起来：

```text
外侧 Read() -> 订单 101，product_id = 42
    内侧 Init() -> 使用当前订单的关联键
    内侧 Read() -> 商品 42，Keyboard
    输出 (101, Keyboard, 1)

外侧 Read() -> 订单 102，product_id = 7
    内侧 Init() -> 换成新的关联键
    内侧 Read() -> 商品 7，Mouse
    输出 (102, Mouse, 2)

...
```

对于普通内连接，当当前订单不再有可匹配的商品记录时，迭代器继续读取下一条订单。若所有订单也都读完，上层收到读取结束状态。

这就解决了**两个独立的表读取器怎样协作产生一条 JOIN 结果**的问题。

但现在还有一个疑问：前面说内侧可以“按商品主键查找”，源码里是哪一个读取器做这件事？

## 6. 内侧如果使用主键，谁负责精确定位？

回到 `CreateIteratorFromAccessPath()`，它不仅能创建 `NESTED_LOOP_JOIN`，也能处理 `AccessPath::EQ_REF`。

对应的迭代器叫做 `EQRefIterator`，定义及实现可在 [`sql/iterators/ref_row_iterators.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/iterators/ref_row_iterators.cc) 中找到。

对于适合 `eq_ref` 的关联条件——例如商品 ID 是唯一非空主键，且每条外侧订单都用 `product_id` 等值查找它——内侧可以采用这样的读取方式。

`EQRefIterator::Read()` 中先构造当前的查找键：

```cpp
m_ref->key_err = construct_lookup(thd(), table(), m_ref);
```

随后，在需要实际读取记录时，通过存储引擎接口执行精确索引查找：

```cpp
table()->file->ha_index_read_map(
    table()->record[0], key_buff_and_map.first,
    key_buff_and_map.second, HA_READ_KEY_EXACT);
```

我们已经在之前的章节研究过 `handler` 和 InnoDB 的索引读取，因此这里无需重新展开 B+ 树源码。

只要把两步连接起来：

1. 外侧订单使当前关联键变成 `product_id = 42`。
2. 内侧 `EQRefIterator` 构造相应键值，通过 `handler` 的索引读取接口向存储引擎获取商品记录。

于是，我们的执行结构可以进一步展开：

```text
            NestedLoopIterator
              /          \
             /            \
     扫描订单             EQRefIterator
        |                       |
  当前 product_id          ha_index_read_map()
        |                       |
        +------ 关联键 ---------+
                                |
                          InnoDB 主键索引
```

这里有两个不能混淆的点。

首先，**Nested Loop 描述的是两边怎样组合，不等于内侧必须全表扫描**。在这个案例里，内侧可以通过主键索引查找；这也解释了为什么索引能够显著改变 JOIN 的成本。

其次，`EQRefIterator` 中存在单行结果复用等实现细节，因此“每条外侧记录都调用内侧迭代器”不等于“每次必定发生一次全新的磁盘读取”。有些结果可能复用或命中缓存；不能把迭代器调用次数直接当成物理 I/O 次数。

到这里，我们已经拥有一条可理解的多表查询路径。

但它依赖一个重要前提：**内侧具有适合关联条件的索引**。

如果没有这个索引，又会发生什么？

---

## 7. 如果没有适合关联的索引呢？

假设商品表不是根据 `product_id` 建立可用索引，而我们仍然要按照商品编号和订单关联。

此时，每次拿到一条订单，都无法通过对应索引快速找出商品。

最直观的办法就又回到了本章开头：对每个订单扫描所有商品。

对于 1000 条订单和 1000 个商品，教学模型中需要大约一百万次比较。

但观察这些操作，会发现大量工作是重复的：

我们反复把同一批商品记录拿出来，只是为了检查它们的 ID 是否等于当前订单的 `product_id`。

如果我们已经知道所有商品 ID，能不能**先把商品记录整理成适合快速匹配的结构**，再扫描订单？

这会带来一个新的思路：

```text
先读取商品，建立按商品 ID 查找的结构
                |
                v
           再读取订单
                |
                v
每读到一条订单，用 product_id 去查找对应商品
```

此前我们使用过 Python 字典作为现成的辅助结构。现在可以把这种方案写成一个独立的连接过程。

## 8. 第二处最小替换：先建立哈希结构，再匹配订单

下面仍然是教学实现，不是 MySQL 的哈希表代码。

```python
# 教学实现：内存中的等值 Hash Join

def hash_join(orders, products):
    hash_table = {}
    for product in products:
        hash_table.setdefault(product["id"], []).append(product)

    rows = []
    probes = 0

    for order in orders:
        probes += 1
        matches = hash_table.get(order["product_id"], [])
        for product in matches:
            rows.append((
                order["id"],
                product["name"],
                order["quantity"],
            ))

    return rows, probes


hash_rows, probes = hash_join(orders, products)
assert hash_rows == rows
print("探测次数:", probes)
```

**实际执行的输出：**

```text
探测次数: 5
```

这一次，我们先把商品记录组织起来，再读取订单。每读一条订单，就用它的 `product_id` 探测已经建立的哈希表。

这里使用列表保存同一个键下的多条记录，是为了表达一般等值连接可能存在“一条记录匹配多条记录”的情况；当前商品主键唯一，因此每次至多得到一条匹配商品。

我们可以粗略对比两个教学模型的工作量：

| 方案 | 主要工作 | 本例观测 |
| --- | --- | ---: |
| 双重遍历 | 对每笔订单检查所有商品 | 20 次字段比较 |
| 预建字典后查找 | 建立 4 条映射，再逐单查找 | 4 条建表记录 + 5 次查找 |
| Hash Join 教学实现 | 建立 4 条哈希映射，再逐单探测 | 4 条建表记录 + 5 次探测 |

这些计数反映的是**教学模型中的操作形态**，不是 MySQL 优化器的成本值。真实执行还涉及哈希计算、内存分配、记录转换、I/O 和缓存等成本。

如果两侧都很大、没有适合 JOIN 条件的索引，那么先建立哈希结构可能比重复扫描内侧表更合适。

这时，我们才正式引入名字：**Hash Join（哈希连接）**。

但新方案也增加了一个限制：如果构建侧的数据非常大，哈希表也可能消耗大量内存。它不是一种没有代价的万能替换。

现在回到 MySQL，看看真实执行器怎样表达这个方案。

## 9. MySQL 8.4.0 的 Hash Join 源码

在 [`sql/join_optimizer/access_path.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/join_optimizer/access_path.cc) 中，除了刚才的 `NESTED_LOOP_JOIN`，还能找到 `AccessPath::HASH_JOIN` 分支。

这条路径会根据连接条件等信息创建 `HashJoinIterator`。具体执行逻辑位于：

[`sql/iterators/hash_join_iterator.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/iterators/hash_join_iterator.cc)

现在我们已经有了从教学实验得到的直觉，所以只需要找两个主要动作。

### 9.1 先把构建侧记录放入哈希结构

在 `HashJoinIterator::BuildHashTable()` 中，真实代码通过构建侧迭代器不断读取记录，并把符合要求的记录存入行缓冲结构。

**真实源码中的两个关键调用，省略外围循环与错误处理：**

```cpp
m_build_input->Read();
m_row_buffer.StoreRow(thd(), reject_duplicate_keys);
```

第一行负责从构建侧获取下一条记录。

第二行将记录放入 Hash Join 使用的行缓冲结构中。这个结构会结合连接键进行哈希组织，而不是我们在 Python 里直接建立一个普通 `dict`。

我们可以把它对应到刚才的实验：

```python
hash_table.setdefault(product["id"], []).append(product)
```

两者的具体实现不同，但设计动作相同：**先读取一侧记录，建立能够根据关联键寻找匹配记录的结构**。

### 9.2 再用另一侧记录探测

源码中的 `HashJoinIterator::ReadRowFromProbeIterator()` 负责从探测侧获取记录；`LookupProbeRowInHashTable()` 则根据该记录的连接键在哈希结构中查找候选匹配。

上层 `HashJoinIterator::Read()` 根据当前状态调用不同处理过程。在有匹配结果时，它返回可供上层消费的一条组合记录。

要特别注意，MySQL 的 `Read()` 返回的是状态码，并通过表记录缓冲区呈现本次读取结果，而不是像教学代码那样直接返回 Python 元组。

这里也有和 `NestedLoopIterator` 不同的行为：

```text
Nested Loop（索引内侧）

读取一条订单
    -> 根据该订单的键执行内侧查找
    -> 返回组合记录
    -> 再读下一条订单

Hash Join（内存中可容纳的简化正常路径）

读取构建侧商品
    -> 建立哈希结构
    -> 读取探测侧订单
    -> 根据订单关联键探测哈希结构
    -> 返回组合记录
```

Hash Join 的真实实现还有内存不足、分区和写临时文件等分支。源码中的 `BUFFER_FULL` 分支会考虑是否允许溢写到磁盘，以及如何继续完成连接。

不过，这些细节在本章只需要说明边界，不必立刻分析全部状态机。我们的主问题仍然是：**没有合适索引时，怎样避免对每条订单都从头扫描一遍商品表**。

MySQL 8.4 官方手册也明确说明，服务器可以在适合的查询上选择 Hash Join，尤其是在 JOIN 条件上没有适用索引时；最终使用哪种方案仍由优化器及实际查询条件决定。

---

## 10. 在真实 MySQL 中观察 JOIN

前面的 Python 实验已经实际执行。现在，我们需要能在 MySQL 8.4 中复现的 SQL，验证相同的数据组合结果，并观察优化器计划。

下面使用独立的实验表名，不修改前面章节的 `products` 和订单业务表。

> 以下 `DROP TABLE` 仅适用于专门的实验数据库。不要在生产环境执行。

```sql
CREATE DATABASE IF NOT EXISTS mysql_source_lab;
USE mysql_source_lab;

DROP TABLE IF EXISTS join_orders;
DROP TABLE IF EXISTS join_products;

CREATE TABLE join_products (
    id BIGINT PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    stock INT NOT NULL
) ENGINE = InnoDB;

CREATE TABLE join_orders (
    id BIGINT PRIMARY KEY,
    product_id BIGINT NOT NULL,
    quantity INT NOT NULL,
    KEY idx_product_id (product_id)
) ENGINE = InnoDB;

INSERT INTO join_products (id, name, stock) VALUES
(7, 'Mouse', 20),
(19, 'Monitor', 35),
(42, 'Keyboard', 99),
(80, 'Desk', 50);

INSERT INTO join_orders (id, product_id, quantity) VALUES
(101, 42, 1),
(102, 7, 2),
(103, 42, 1),
(104, 80, 1),
(105, 999, 1);
```

### 10.1 先验证业务结果

```sql
SELECT o.id, p.name, o.quantity
FROM join_orders AS o
JOIN join_products AS p
  ON o.product_id = p.id
ORDER BY o.id;
```

预期结果：

```text
+-----+----------+----------+
| id  | name     | quantity |
+-----+----------+----------+
| 101 | Keyboard |        1 |
| 102 | Mouse    |        2 |
| 103 | Keyboard |        1 |
| 104 | Desk     |        1 |
+-----+----------+----------+
```

订单 105 不出现，是因为普通 `INNER JOIN` 只返回两侧关联条件匹配的组合结果。若业务希望保留没有匹配商品的订单，需要另行讨论 `LEFT JOIN`；本章先不增加这层复杂度。

### 10.2 观察索引关联的执行计划

为了少一个排序节点干扰观察，先去掉 `ORDER BY`：

```sql
EXPLAIN FORMAT=TREE
SELECT o.id, p.name, o.quantity
FROM join_orders AS o
JOIN join_products AS p
  ON o.product_id = p.id;
```

在这组表结构中，商品 ID 是主键，因而存在按键定位的机会。

**观察重点**不是记住一个固定的 EXPLAIN 文本，而是看执行计划中是否出现这些行为：

1. 外侧从某张表读取记录。
2. 内侧使用另一张表的索引做查找（例如主键上的 single-row index lookup）。
3. 上层以 Nested Loop 的方式组合记录。

由于表非常小，优化器可能选择不同的访问顺序或策略。不能断言只要写了这个 SQL，就必定选择 `join_orders` 为外侧，`join_products` 为内侧。

执行：

```sql
EXPLAIN ANALYZE
SELECT o.id, p.name, o.quantity
FROM join_orders AS o
JOIN join_products AS p
  ON o.product_id = p.id;
```

进一步关注各节点的 `loops`、实际行数与时间。这里的 `loops` 是执行器节点被启动的次数，不等于实际物理磁盘读次数。

### 10.3 去掉 JOIN 列索引，观察 Hash Join 的机会

为了构造“没有合适 JOIN 索引”的情况，建立两个不在关联字段上建索引的实验表：

```sql
DROP TABLE IF EXISTS join_orders_noidx;
DROP TABLE IF EXISTS join_products_noidx;

CREATE TABLE join_products_noidx (
    product_id BIGINT NOT NULL,
    name VARCHAR(100) NOT NULL
) ENGINE = InnoDB;

CREATE TABLE join_orders_noidx (
    order_id BIGINT NOT NULL,
    product_id BIGINT NOT NULL,
    quantity INT NOT NULL
) ENGINE = InnoDB;

INSERT INTO join_products_noidx (product_id, name)
SELECT id, name FROM join_products;

INSERT INTO join_orders_noidx (order_id, product_id, quantity)
SELECT id, product_id, quantity FROM join_orders;
```

两张表都没有为 `product_id` 定义可用于关联的显式索引；这不意味着 InnoDB 内部不存在其用于组织数据的聚簇索引。

执行：

```sql
EXPLAIN FORMAT=TREE
SELECT o.order_id, p.name, o.quantity
FROM join_orders_noidx AS o
JOIN join_products_noidx AS p
  ON o.product_id = p.product_id;
```

在 MySQL 8.4 中，这种缺少可用连接索引的等值 JOIN，是观察 Hash Join 的合适案例。

如果计划中出现 `Inner hash join`、`Hash` 等节点，我们就可以把真实执行计划与刚才的“构建侧 / 探测侧”教学实验对应起来。

**但这里不预设一定出现 Hash Join。** 最终计划受行数、统计信息及优化器选择影响。在几行数据的实验表上，具体选择不必与大数据场景相同。若要比较性能，应使用更大的可控数据集，并以实际 `EXPLAIN ANALYZE` 输出为准。

### 10.4 我们究竟验证了什么？

`EXPLAIN FORMAT=TREE` 展示的是优化器选择的执行结构。

`EXPLAIN ANALYZE` 会真正执行查询并报告执行节点的观测信息，因此它不仅是纯静态解释计划。

不过，我们不能只凭其中的节点名称就断言某个底层函数的每一次调用顺序。想证明函数级行为，可以在固定源码构建的 Debug 版本中设置断点，例如：

```text
NestedLoopIterator::Read()
EQRefIterator::Read()
HashJoinIterator::BuildHashTable()
HashJoinIterator::Read()
```

只对计划实际选择的路径设置断点，并根据调用栈验证对应的执行器行为。如果计划选了 Hash Join，却去断言 `NestedLoopIterator::Read()` 必然被调用，就会得出错误结论。

**验证声明：** 本章 Python 教学代码已经实际运行并通过断言；上面的 MySQL 8.4 SQL 是可复现实验步骤，**未在真实 MySQL 实例中执行**。所有 MySQL 表格和计划描述均属于预期行为或观察方法，不伪装为实际执行日志。

---

## 11. 为什么不总用 Hash Join，或者总用索引查找？

既然我们现在知道两种连接方式，就很容易想问：究竟哪一种更快？

答案取决于实际数据和可用访问路径。

假设一个请求只查询一笔订单：

```sql
SELECT o.id, p.name, o.quantity
FROM join_orders AS o
JOIN join_products AS p
  ON o.product_id = p.id
WHERE o.id = 101;
```

外侧可能只有一条订单，内侧又能通过商品主键快速找到对应记录。

为了这笔订单先扫描大量商品并构建哈希表，未必合算。

反过来，如果需要关联很多条记录，而且关联字段没有合适索引，反复全表扫描的代价就可能远高于一次建立哈希结构。

因此，真正需要比较的是：

- 外侧究竟会产生多少条记录？
- 内侧能否通过索引高效定位？
- 构建和维护哈希结构需要多少内存和额外处理？
- 两边记录的分布、数据类型以及过滤条件是什么？
- 结果是否要求特定顺序，是否还有其他操作？

这些不是某一种 JOIN 算法单独能够决定的，而是上一章查询优化器需要综合考虑的问题。

这也解释了为什么**优化器和执行器是两个不同的职责**：优化器估算并选择访问路径，执行器依照选定的路径逐条读取、组合记录。

这一次，我们不再把它们当成两个互不相干的概念。我们已经亲眼看到了同一条 SQL 在不同执行策略下，为什么会产生完全不同的记录读取过程。

## 12. 把这一章重新接回 SELECT 主线

从第 16 章开始，我们研究过：

- 按名称查商品，需要怎样的二级索引？
- 索引里没有 `stock`，什么时候还要回表？
- 多条访问路径都能得到正确结果，优化器怎样选择？
- 执行器如何使用 `RowIterator` 驱动真实数据读取？

本章只增加了一个新的业务要求：**一条订单还需要取得对应商品的信息。**

于是，单表迭代器不再足够。我们需要两个输入之间的匹配与组合。

最简单的双重遍历可以得到正确结果，却在数据增长时重复检查大量无关记录。

已有主键索引时，内侧可以通过 `EQRefIterator` 进行按键查找，配合 `NestedLoopIterator` 输出组合结果。

当缺少合适索引、需要处理大量等值关联数据时，我们又自然想到先建立哈希结构，再用另一侧进行匹配；MySQL 的 `HashJoinIterator` 提供了相应的执行机制，并处理真实工程中的内存限制等复杂情况。

到这里，我们可以把之前的单表执行图拓展一层：

```text
                   SELECT 查询
                       |
                       v
                  查询优化器
                       |
                       v
                 选择 AccessPath
                  /          \
                 /            \
         Nested Loop          Hash Join
          /      \            /       \
      订单读   商品索引读   构建侧读   探测侧读
          \      /            \       /
           组合记录             组合记录
                 \              /
                  v            v
                   返回结果行
```

这是**执行策略的教学结构图**，不是说一次查询会同时执行两条路径；真实计划通常选择其中适合的一条，且可能包含额外过滤、排序或其他迭代器。

这一章结束，我们已经知道：

> JOIN 并不是把两张表“神奇地拼起来”；它是执行器在两侧数据读取结果之间组织匹配、生成新结果的过程，而使用哪种匹配方式，会显著影响需要做多少工作。

## 13. 下一章：一条 SELECT 的完整源码旅程

现在，第二篇的必要零件已经齐了。

我们已经研究过索引、回表、优化器、单表执行器，以及两个读取器之间怎样协作。

下一章不再额外增加一个新的复杂业务场景，而是回到这条查询：

```sql
SELECT o.id, p.name, o.quantity
FROM join_orders AS o
JOIN join_products AS p
  ON o.product_id = p.id;
```

我们将沿 MySQL 8.4.0 的一次实际执行路径，从客户端发送 SQL 出发，经过解析、优化、构建迭代器、InnoDB 读取记录、JOIN 配对和结果发送，把此前分散学到的机制重新连起来。

**下一章：第 21 章《一条 SELECT 的完整源码旅程——从请求进入到结果返回》。**

---

## 本章源码与参考资料

固定源码：MySQL 8.4.0，Git Tag `mysql-8.4.0`。

| 文件 / 文档 | 入口 | 本章实际用途 |
| --- | --- | --- |
| [`sql/join_optimizer/access_path.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/join_optimizer/access_path.cc) | `CreateIteratorFromAccessPath()` | 将 JOIN 访问路径转换为执行迭代器 |
| [`sql/iterators/composite_iterators.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/iterators/composite_iterators.cc) | `NestedLoopIterator::Init()`、`Read()` | 外侧取一行，内侧重新初始化并读取 |
| [`sql/iterators/ref_row_iterators.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/iterators/ref_row_iterators.cc) | `EQRefIterator::Read()` | 按当前关联键进行唯一索引查找 |
| [`sql/iterators/hash_join_iterator.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/iterators/hash_join_iterator.cc) | `BuildHashTable()`、`ReadRowFromProbeIterator()`、`LookupProbeRowInHashTable()`、`Read()` | 构建哈希结构、探测并输出匹配行 |
| [MySQL 8.4：Nested-Loop Join Algorithms](https://dev.mysql.com/doc/refman/8.4/en/nested-loop-joins.html) | 官方手册 | 嵌套循环连接的工作方式 |
| [MySQL 8.4：Hash Join Optimization](https://dev.mysql.com/doc/refman/8.4/en/hash-joins.html) | 官方手册 | Hash Join 的适用场景和内存限制 |
| [MySQL 8.4：EXPLAIN Output Format](https://dev.mysql.com/doc/refman/8.4/en/explain-output.html) | 官方手册 | 查看优化器执行计划 |

> 本章的 C++ 片段是经过固定源码核对的关键行节选；Python 代码属于实际运行的教学实现；SQL 计划属于供读者验证的实验步骤。三者不能互相替代。
