---
title: "18｜从多种访问路径到查询优化器：MySQL 怎样选择执行计划？"
sidebar_position: 19
slug: /mysql/18-how-optimizer-chooses-plan
description: "沿着商品名称查询，从全表扫描、二级索引回表、覆盖索引三个候选方案出发，通过成本模型、EXPLAIN、Optimizer Trace 与 MySQL 8.4.0 源码理解查询优化器。"
tags: [MySQL, InnoDB, 查询优化器, EXPLAIN, 源码精读]
---

# 从多种访问路径到查询优化器：MySQL 怎样选择执行计划？

上一章，我们把商品查询改成了按照名称查找：

```sql
SELECT stock
FROM products
WHERE name = 'Keyboard';
```

起初，商品表只有主键 `id`，数据库不能直接通过它定位 `name = 'Keyboard'` 的记录。于是我们建立了二级索引 `idx_name(name)`，让数据库可以先定位符合名称条件的索引记录，再根据其中的主键查找完整的商品记录。

我们还发现，如果建立 `(name, stock)` 这样的联合索引，那么需要返回的 `stock` 就可能直接从索引中取得，而不必访问聚簇索引。

到这里，问题反而变得更有趣了。

**同一条 SQL，已经有不止一种正确的执行办法：MySQL 应该选哪一个？**

我们先不看优化器源代码。假设数据库是由我们自己写的，就从最简单的判断办法开始。

## 1. 三种方式都能返回正确库存

为了便于比较，先假设表中有一万件商品，其中 11 件商品的名称是 `Keyboard`，另外的商品名称各不相同。

我们的查询需要返回 **所有** 名称为 `Keyboard` 的商品库存，而不只返回 ID 为 42 的那件商品。

目前至少存在三条候选路径。

**第一条：扫描整个商品表。**

从表中逐条读取记录，检查 `name` 是否等于 `Keyboard`，符合条件的就返回 `stock`。

```text
读取商品 1 → 不匹配
读取商品 2 → 不匹配
...
读取商品 42 → 匹配，返回 stock
...
读取商品 10000 → 继续检查
```

这种办法最容易实现，不需要使用 `name` 索引。它的主要问题是：即使只有 11 件商品满足条件，仍可能需要查看全部一万条记录。

**第二条：使用 `idx_name(name)`，然后回表。**

先在二级索引中定位 `Keyboard` 对应的记录。对于符合条件的索引记录，取得其中的主键 `id`，再访问聚簇索引中的完整记录，读取 `stock`。

```text
idx_name(name)
    |
    +-- ('Keyboard', 42)   --按主键--> 完整记录，读取 stock
    +-- ('Keyboard', ...)  --按主键--> 完整记录，读取 stock
    +-- ...
```

如果只有很少的商品符合条件，这种方式可能省去大量无关记录的检查；但如果符合条件的商品非常多，就会产生很多次聚簇索引读取。

**第三条：使用覆盖索引 `idx_name_stock(name, stock)`。**

现在二级索引中已经有本次查询需要的名称与库存字段。我们可以从满足名称条件的索引记录中直接得到结果。

```text
idx_name_stock(name, stock)
    |
    +-- ('Keyboard', 30, ...)
    +-- ('Keyboard', 99, ...)
    +-- ...
    |
    +-- 直接返回 stock
```

这里的 `...` 表示 InnoDB 二级索引中用于定位聚簇记录的主键信息；这不是完整的真实页布局图。

上面三条路径的共同点是：**在没有其他条件影响的正常读取场景中，它们都能实现同一条 SQL 的查询语义。** 不同的是，读取了多少索引项、访问了多少数据页、是否需要回表，以及付出了多少计算成本。

这意味着，**“能用索引”还不足以决定“应该用哪个索引”。**

## 2. 最直观的规则：有索引就用索引，可以吗？

假设我们先写一个最简单的“计划选择器”：

```python
# 概念伪代码：不是 MySQL 的实现
if has_index_on("name"):
    plan = "secondary_index_lookup"
else:
    plan = "table_scan"
```

当 `Keyboard` 只匹配 11 条记录时，这个规则看起来合理。

但现在修改业务数据。

因为商家的一次活动，商品表里有 9000 件商品的名称都变成了 `Keyboard`。

我们的 SQL 没有变化：

```sql
SELECT stock FROM products WHERE name = 'Keyboard';
```

可是，如果继续使用普通 `idx_name(name)`，情况就不同了。

二级索引虽然能帮我们找到所有 `Keyboard` 的索引项，但每条记录的 `stock` 仍然需要从聚簇索引取出。这意味着可能发生大量回表访问。

相反，直接扫描聚簇索引可能更划算：一次沿着数据页顺序读取大量记录，在扫描时检查名称即可。

这就是第一个真正的反例：

> **同一个索引，在只有少量记录匹配时可能非常有用；当绝大多数记录都匹配时，却可能不如直接扫描。**

所以，我们缺少的不是更多 `if` 判断，而是一个能比较不同读取方案工作量的办法。

## 3. 给候选方案估一个成本

我们先构建一个**教学成本模型**。它不是 MySQL 的成本公式，只用来说明为什么不同数据分布可能让优选路径发生变化。

假设我们把工作量分成两类：

- 读取页面的代价：一次读取要访问多少存储页面。
- 检查记录的代价：需要对多少条记录或索引项进行处理。

并且假设普通二级索引回表需要额外访问聚簇索引，而覆盖索引不需要。

为保证例子可以重现，我们使用下面的 Python 模型。

**教学化简，可独立运行，不代表 MySQL 实际成本常数：**

```python
from math import ceil

N = 10_000
PAGE_ROWS = 40
INDEX_PAGE_KEYS = 80


def compare_paths(matches):
    # 假设全表扫描需要读取聚簇索引的数据页，
    # 并检查每条记录是否匹配。
    full_scan = ceil(N / PAGE_ROWS) * 4 + N * 0.02

    # 假设先定位普通二级索引，再为每条匹配记录付出回表成本。
    secondary_then_clustered = 3 * 4 + matches * (2 + 0.02)

    # 假设索引覆盖 name、stock，只需在二级索引范围内读取。
    covering_index = (
        2 * 4
        + ceil(matches / INDEX_PAGE_KEYS) * 4
        + matches * 0.02
    )

    costs = {
        "table_scan": round(full_scan, 2),
        "secondary_lookup": round(secondary_then_clustered, 2),
        "covering_lookup": round(covering_index, 2),
    }
    return min(costs, key=costs.get), costs


for matches in (11, 9000):
    best, costs = compare_paths(matches)
    print("matches:", matches, "|", costs, "| best:", best)
```

实际运行输出：

```text
matches: 11 | {'table_scan': 1200.0, 'secondary_lookup': 34.22, 'covering_lookup': 12.22} | best: covering_lookup
matches: 9000 | {'table_scan': 1200.0, 'secondary_lookup': 18192.0, 'covering_lookup': 640.0} | best: covering_lookup
```

先不要把表中的数字理解成真实的毫秒或真实的 MySQL `EXPLAIN` 成本。它们只在这个玩具模型内部有意义。

真正重要的是中间的对比。

当只有 11 条记录匹配时，普通二级索引查找的成本估计是 `34.22`，远低于全表扫描的 `1200`。

但当匹配记录增加到 9000 条时，普通二级索引查找的估计成本增加到 `18192`，已经远高于全表扫描。

与此同时，如果有覆盖索引，它的候选成本可能仍然较低，因为不用对每条匹配记录再做一次完整的回表。

因此，**一个合理的查询计划不能只看“有无索引”，还应考虑“预计匹配多少记录、取得这些记录要做多少工作”。**

不过，这里马上产生了一个新问题。

我们作为实验作者，可以直接把 `matches=11` 或 `matches=9000` 传给 Python。

真实 MySQL 在执行查询之前，并没有先把查询跑完，它怎样知道大约有多少条记录会匹配？

它只能利用已经掌握的数据分布信息、统计信息和成本模型，**估算**候选计划的工作量。

这正是我们需要引入查询优化器的原因。

## 4. 查询优化器解决的不是“能不能执行”，而是“怎样执行”

上一章解释二级索引时，我们已经能想象一次查询沿着什么结构去找数据。

但当候选路径超过一条，就不能简单地把某个索引写死到程序里。

于是，我们在真正读取记录之前，增加一个负责选择方案的步骤：

```text
                 SELECT stock
             WHERE name='Keyboard'
                       |
                       v
                候选访问路径
                 /     |     \
                /      |      \
             全表扫描  普通索引  覆盖索引
                \      |      /
                 \     |     /
                 估计各自成本
                       |
                       v
                 选择执行计划
                       |
                       v
                 开始读取记录
```

这是**概念数据流图**，不是 MySQL 优化器内部所有阶段的精确调用关系。真实 MySQL 还会进行等价条件处理、连接顺序优化、排序处理等许多工作。

但对本章来说，只需要抓住一个机制：

**查询优化器在实际执行之前评估候选访问方式，并选择它认为代价合适的执行计划。**

注意“认为”两个字。

优化器通常是依据统计信息和模型做决策，不是把所有候选计划真正运行一遍再计时。因此，它的估计可能不准确，选择结果也可能不是某次运行中真实耗时最短的方案。

现在，我们可以进入 MySQL 源码核对这个判断。

---

## 5. 真实源码入口：`JOIN::optimize()` 在解决什么？

本章固定源码版本：

| 项目 | 固定值 |
|---|---|
| 仓库 | [`mysql/mysql-server`](https://github.com/mysql/mysql-server) |
| Git Tag | `mysql-8.4.0` |
| MySQL 版本 | 8.4.0 |
| 主要文件 | `sql/sql_optimizer.cc` |
| 相关实现 | `sql/range_optimizer/range_optimizer.cc` |

打开 [MySQL 8.4.0 的 `sql/sql_optimizer.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/sql_optimizer.cc)，可以找到：

**真实源码节选：**

```cpp
bool JOIN::optimize(bool finalize_access_paths) {
```

源文件对这个函数的注释说明：它是将一个查询块优化为查询执行计划的入口，包含逻辑变换、基于成本的访问路径选择和后续的执行计划准备。

我们不需要现在理解类名 `JOIN` 的全部历史和职责。它在这里不仅仅表示 SQL 中写了 `JOIN` 的多表查询；单表 `SELECT` 也会使用相应的查询优化结构。

在默认的传统优化路径中，可以找到：

**真实源码节选：`JOIN::optimize()`（无关分支已省略）**

```cpp
// Set up join order and initial access paths
THD_STAGE_INFO(thd, stage_statistics);

if (make_join_plan()) {
  if (thd->killed) thd->send_kill_message();
  DBUG_PRINT("error", ("Error: JOIN::make_join_plan() failed"));
  return true;
}
```

这里的 `make_join_plan()` 是优化阶段组织表访问计划的一个重要步骤。

对于我们当前的单表查询，它不需要解决复杂的多表连接顺序问题，但仍然必须考虑怎样访问 `products`。

值得强调的是：`JOIN::optimize()` **不是对每一种 SQL 都无条件执行同一条旧优化器分支**。MySQL 8.4.0 的源码还包含另一套 Hypergraph 优化路径，受相应开关与适用条件影响。本章只沿通常的传统优化路径，研究“比较扫描与索引访问成本”这一局部机制。

在这一层，仍看不见为什么 `idx_name` 会输给全表扫描。

为了回答这个问题，我们继续深入真正计算候选访问成本的代码。

## 6. 从表扫描的成本开始

打开：

[`sql/range_optimizer/range_optimizer.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/range_optimizer/range_optimizer.cc)

搜索：

```cpp
int test_quick_select(
```

这个函数是传统范围优化器中一个重要的入口。源码注释说明，它尝试寻找适合指定条件的索引访问方式，并结合成本决定候选范围方案是否值得使用。

先看一段非常直接的代码。

**真实源码节选：`test_quick_select()`**

```cpp
const Cost_model_server *const cost_model = thd->cost_model();

ha_rows records = table->file->stats.records;

if (!records) records++; /* purecov: inspected */

double scan_time =
    cost_model->row_evaluate_cost(static_cast<double>(records)) + 1;

Cost_estimate cost_est = table->file->table_scan_cost();
```

这几行恰好回答了我们之前留下的问题的一部分。

**第一，优化器没有先读取全部记录以得出真实数量。**

这里的 `table->file->stats.records` 是从存储引擎表统计信息中取得的记录数量估计，并不一定等于逐条实测的准确行数。

**第二，扫描成本不只是一个“扫描多少行”的数字。**

源码会使用 `row_evaluate_cost(...)` 估算记录条件处理的成本，还会调用 `table_scan_cost()` 获取存储引擎层面的扫描成本估计。

为什么这两个部分都需要？

因为扫描一张表既需要获取数据，也需要检查查询条件。访问数据和判断记录是否匹配是两项不同的工作。

这和我们教学模型里的思想对应：

```python
# 教学化简：和 MySQL 的具体公式不同
scan_cost = read_pages_cost + evaluate_rows_cost
```

真实 MySQL 的成本模型要复杂得多，不能把上面的 Python 式子当作一份源码翻译。

但这已经给我们提供了第一条明确证据：

**MySQL 会为表扫描建立成本基准，而不是默认“有索引就一定优先索引”。**

## 7. 当存在覆盖索引时，源码怎样比较？

回到查询：

```sql
SELECT stock
FROM products
WHERE name = 'Keyboard';
```

如果有 `(name, stock)` 联合索引，它包含本次读取所需要的字段。

但“覆盖”仍然不意味着一定最优。假设索引的扫描代价也很大，优化器仍需要比较其他方案。

继续看 `test_quick_select()` 的代码。

**真实源码节选：比较全索引扫描与已有成本基准（节选中保留同一分支内的关键语句）**

```cpp
if (!table->covering_keys.is_clear_all()) {
  int key_for_use = find_shortest_key(table, &table->covering_keys);

  Cost_estimate key_read_time = param.table->file->index_scan_cost(
      key_for_use, 1, static_cast<double>(records));

  key_read_time.add_cpu(
      cost_model->row_evaluate_cost(static_cast<double>(records)));

  bool chosen = false;
  if (key_read_time < cost_est) {
    cost_est = key_read_time;
    chosen = true;
  }
```

请注意：这段代码讨论的是 **覆盖索引的完整索引扫描** 与当前成本基准的比较，并不是直接断言我们当前的 `WHERE name = 'Keyboard'` 一定会使用全索引扫描。

它所展示的设计动作是：

1. 检查是否存在覆盖当前查询所需字段的索引；
2. 为候选覆盖索引扫描估算代价；
3. 将这个候选成本与现有基准比较；
4. 如果更便宜，就更新目前认可的成本基准。

可以只把最后几行理解为：

```python
# 教学化简，不是 MySQL 原始代码
if covering_scan_cost < current_best_cost:
    current_best_cost = covering_scan_cost
```

这正是我们在 Python 成本模型中做的事情。

但是，还有一个问题没有回答。

我们不想扫描 `idx_name_stock` 中全部的名称，只需要读取 `Keyboard` 对应的索引范围。

是否存在比“完整索引扫描”更便宜的索引范围访问？

## 8. 从覆盖索引扫描继续比较索引范围访问

在同一个 `test_quick_select()` 中，可以找到下面这段代码：

**真实源码节选：`range_optimizer.cc`**

```cpp
AccessPath *range_path = get_key_scans_params(
    thd, &param, tree, false, true, interesting_order,
    skip_records_in_range, best_cost, /*ror_only=*/false, needed_reg);

if (range_path) {
  best_path = range_path;
  best_cost = best_path->cost();
}
```

这一段出现了两个新的东西：

`tree`：由查询条件等信息整理出来、用于分析索引访问范围的内部表示。

`range_path`：找到的候选范围访问路径。

我们暂时不需要研究 `tree` 的内部节点结构，也不需要进入范围优化器的所有复杂分支。

对于本章，只需要理解：

**优化器现在不只是比较“全表扫描”与“完整覆盖索引扫描”，还会针对 WHERE 条件尝试构造更有针对性的索引范围访问方式。**

源码里 `get_key_scans_params(...)` 接收 `best_cost`，说明它是在现有成本上界约束下寻找合适的范围访问方案。

这里不应把 `get_key_scans_params()` 误读成 MySQL 唯一的索引访问选择算法。单表 `ref` 查找、其他范围方案和多表访问计划还可能涉及不同路径。

我们沿着这一局部源码已经足以验证本章的重要推论：

> MySQL 不是固定写死“存在索引就选索引”，而是在候选路径之间使用成本估计进行判断。

至此，可以把本章的主要源码关系总结为：

```text
查询已经解析为语句结构
           |
           v
     JOIN::optimize()
           |
           v
     make_join_plan()
           |
           v
    评估候选访问路径
           |
           +-- 估算表扫描代价
           |
           +-- 估算部分索引扫描代价
           |
           +-- 估算符合条件的范围访问代价
           |
           v
     形成选定的访问计划
```

这是一张**职责与候选方案示意图**，不是把所有箭头都表示为直接函数调用。尤其不能把 `test_quick_select()` 当作 `make_join_plan()` 对所有查询直接调用的唯一下一步。

下面回到真实 MySQL，让执行计划告诉我们它最后选择了什么。

---

## 9. 真实 MySQL 实验：同一条 SQL，改变数据分布与索引

我们仍然查询商品名称为 `Keyboard` 的库存，但为了防止影响前面章节的事务、库存和订单数据，创建一个单独的实验库。

> 本节 SQL 为**可复现实验方案，尚未在真实 MySQL 8.4.0 服务器执行**。本文不会伪造 `EXPLAIN` 输出或具体执行时间。请以你本地执行后的结果为准。

### 9.1 准备一个独立的实验库

```sql
CREATE DATABASE IF NOT EXISTS mysql_optimizer_lab;
USE mysql_optimizer_lab;

-- 只在专用实验库中执行：重建示例表。
DROP TABLE IF EXISTS products;

CREATE TABLE products (
    id BIGINT NOT NULL PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    stock INT NOT NULL,
    KEY idx_name (name)
) ENGINE = InnoDB;
```

现在插入一万条商品记录。

为了不依赖存储过程，我们先准备一张只有十条数字的辅助表，再做四次笛卡尔组合。

```sql
CREATE TABLE IF NOT EXISTS digits (
    d TINYINT UNSIGNED PRIMARY KEY
);

INSERT IGNORE INTO digits (d)
VALUES (0),(1),(2),(3),(4),(5),(6),(7),(8),(9);

INSERT INTO products (id, name, stock)
SELECT
    seq.id,
    CASE
        WHEN seq.id = 42 OR MOD(seq.id, 1000) = 0
            THEN 'Keyboard'
        ELSE CONCAT('Item-', seq.id)
    END,
    100
FROM (
    SELECT
        a.d * 1000 + b.d * 100 + c.d * 10 + d.d + 1 AS id
    FROM digits AS a
    CROSS JOIN digits AS b
    CROSS JOIN digits AS c
    CROSS JOIN digits AS d
) AS seq;

ANALYZE TABLE products;
```

核对数据分布：

```sql
SELECT COUNT(*) AS total_rows,
       SUM(name = 'Keyboard') AS keyboard_rows
FROM products;
```

按上述生成规则，理论上应有 `10000` 条记录，其中 `11` 条记录名称为 `Keyboard`：编号 `42` 和十个能被 `1000` 整除的编号。

### 9.2 候选方案 A：不使用名称索引

先人为排除 `idx_name`：

```sql
EXPLAIN FORMAT=TRADITIONAL
SELECT stock
FROM products IGNORE INDEX (idx_name)
WHERE name = 'Keyboard';
```

重点观察：

- `type`：是否出现 `ALL`；
- `key`：是否为 `NULL`；
- `rows`：优化器预计需要访问多少行。

因为本次只建立了 `PRIMARY` 和 `idx_name`，并且过滤条件在 `name` 上，所以忽略名称索引后，通常会得到表扫描路径。

这里的 `rows` 是**估计值**，不能把它当作真实执行时访问的准确行数。

### 9.3 候选方案 B：使用普通二级索引

现在允许选择 `idx_name`：

```sql
EXPLAIN FORMAT=TRADITIONAL
SELECT stock
FROM products
WHERE name = 'Keyboard';
```

对于当前仅有少量记录匹配的分布，我们预期优化器会倾向于使用 `idx_name`，但实际选择应以输出为准。

如果 `key = idx_name`，说明优化器使用了这个索引。

但它不包含 `stock`，所以对于这个查询通常还需要读取聚簇索引中的完整记录。

这恰好是上一章学过的回表路径。

### 9.4 候选方案 C：增加覆盖索引

现在执行：

```sql
ALTER TABLE products
ADD INDEX idx_name_stock (name, stock);

ANALYZE TABLE products;
```

再次查看同一条 SQL 的执行计划：

```sql
EXPLAIN FORMAT=TRADITIONAL
SELECT stock
FROM products
WHERE name = 'Keyboard';
```

观察 `key` 是否改为 `idx_name_stock`，以及 `Extra` 是否出现 `Using index`。

`Using index` 是传统 EXPLAIN 输出中的覆盖索引读取提示之一。但不要把它理解成“优化器必定选择这个索引”：候选计划仍受统计信息、成本、其他索引及查询细节影响。

至此，同一条 SQL 具备了多条候选访问路径，而 `EXPLAIN` 告诉我们这一次优化器最终选了哪条。

接下来验证：如果匹配记录突然变多，计划是否可能改变？

### 9.5 将 `Keyboard` 从稀有名称变成常见名称

仅在专用实验库中执行：

```sql
UPDATE products
SET name = CASE
    WHEN id <= 9000 THEN 'Keyboard'
    ELSE CONCAT('Item-', id)
END;

ANALYZE TABLE products;

SELECT COUNT(*) AS total_rows,
       SUM(name = 'Keyboard') AS keyboard_rows
FROM products;
```

现在预期有 `9000` 条记录匹配。

继续查看原 SQL：

```sql
EXPLAIN FORMAT=TRADITIONAL
SELECT stock
FROM products
WHERE name = 'Keyboard';
```

对于这个数据分布，普通二级索引回表可能很昂贵。覆盖索引则不需要做同样规模的回表，优化器可能仍然偏好它。

为了分别观察候选路径，可以再使用提示进行对照：

```sql
-- 排除两个名称索引，观察扫描路径
EXPLAIN FORMAT=TREE
SELECT stock
FROM products IGNORE INDEX (idx_name, idx_name_stock)
WHERE name = 'Keyboard';

-- 将候选名称索引限制为 idx_name，观察非覆盖方案
EXPLAIN FORMAT=TREE
SELECT stock
FROM products FORCE INDEX (idx_name)
WHERE name = 'Keyboard';

-- 将候选名称索引限制为覆盖索引
EXPLAIN FORMAT=TREE
SELECT stock
FROM products FORCE INDEX (idx_name_stock)
WHERE name = 'Keyboard';
```

`FORCE INDEX` 会影响优化器对索引使用和表扫描的偏好，**不是一个保证指定物理执行步骤绝对发生的开关**；应继续看 `EXPLAIN` 来确认实际计划。

如果第二条仍出现表扫描，不是实验失败，而是在提醒我们：真实优化器会综合多种约束，提示本身也有适用边界。

这一组实验的目的，不是保证在任意机器上出现一模一样的 `rows` 和 `cost`，而是训练我们观察：**同样 SQL 的最终访问路径如何依赖数据分布、索引覆盖情况和统计信息。**

## 10. 优化器依据什么知道 `Keyboard` 是常见还是稀有？

回忆源码：

```cpp
ha_rows records = table->file->stats.records;
```

这给了优化器关于表规模的一个估计。

但只有总记录数还不够。

假设商品表有一万条记录：

- 情况 A：只有 11 条名称为 `Keyboard`；
- 情况 B：9000 条名称为 `Keyboard`。

总行数完全一样，可是使用普通二级索引的预期成本可能相差极大。

因此，优化器还需要利用索引统计信息等手段，估算某个条件的选择性，也就是它预计能够筛选出多少记录。

我们已经在实验中执行了：

```sql
ANALYZE TABLE products;
```

它会更新相关表和索引统计信息，帮助优化器了解数据分布。MySQL 还提供直方图等统计机制，但本章不需要把所有统计方法一次讲完。

当前只需理解：

**优化器不是神奇地预知了查询结果。它依赖可获得的统计信息进行估算。**

而统计信息可能过时，估计也可能出现偏差。

这就引出了我们看到执行计划不合理时的基本排查方法：先观察计划，再观察估算行数与真实返回行数是否相近，必要时检查或更新统计信息。

## 11. 不只看选了什么，还要看为什么选：Optimizer Trace

到这里，`EXPLAIN` 已经能告诉我们最终选择了哪个索引。

但如果我们看到 `key = idx_name`，仍然可能想问：

为什么没有选 `idx_name_stock`？

是不是全表扫描的估计代价更低？

前面的 `EXPLAIN` 主要展示最终计划和部分估算，而不一定列出全部候选方案的取舍过程。

这时，才有必要引入 **Optimizer Trace**。

在 MySQL 8.4 中，可以尝试：

```sql
SET SESSION optimizer_trace = 'enabled=on';
SET SESSION optimizer_trace_max_mem_size = 1048576;

EXPLAIN FORMAT=JSON
SELECT stock
FROM products
WHERE name = 'Keyboard';

SELECT TRACE
FROM INFORMATION_SCHEMA.OPTIMIZER_TRACE\G

SET SESSION optimizer_trace = 'enabled=off';
```

在传统优化路径中，Trace 可能包含诸如：

- `range_analysis`：范围访问分析；
- `table_scan`：扫描相关估算；
- `analyzing_range_alternatives`：候选范围访问方案；
- `best_covering_index_scan`：覆盖索引扫描候选；
- `chosen`、`cause`：某些候选的选取与放弃理由。

这些名称并非随意编造；例如 `range_optimizer.cc` 中确实有创建 `range_analysis` 与 `best_covering_index_scan` Trace 项的代码。

但并非每个查询都会出现上述全部字段，Trace 的结构也可能因查询形态、优化器路径及版本而变化。因此，这里不伪造一份具体的 JSON 运行输出。

**先看 `EXPLAIN` 选了什么，再用 Trace 查为什么选，是研究优化器源码非常有效的一条路线。**

## 12. `EXPLAIN` 的成本，不等于实际执行时间

我们还没有真正执行过刚才那些候选计划。

目前看到的主要是优化器预测。

如果我们希望比较“预计会返回多少行”和“实际返回多少行”，可以继续使用：

```sql
EXPLAIN ANALYZE
SELECT stock
FROM products
WHERE name = 'Keyboard';
```

在 MySQL 8.4 中，`EXPLAIN ANALYZE` 会**实际执行**符合支持条件的查询，并以树形输出展示部分执行节点的估算成本、估算行数、实际行数、耗时与循环次数。

与普通 `EXPLAIN` 不同，这条命令不只是展示一个静态估计。

不过，即使拿到了真实耗时，也不能只跑一次就得出“这个索引永久最快”的结论。

原因包括：Buffer Pool 是否预热、数据是否已经缓存、服务器当时的负载、索引大小、数据分布以及统计估计等都会影响结果。

另外，优化器的 `cost` 值是模型内部使用的比较指标，不是毫秒；不能把 `cost = 120` 理解成查询会运行 `120ms`。

这也解释了为什么 MySQL 明明有索引，却可能主动选择全表扫描：

**索引只提供了一种访问可能性，优化器选择的是它估计更合适的方案。**

---

## 13. 用源码重新检查我们最初的直觉

回到我们最开始的错误规则：

```python
# 概念伪代码：存在索引就使用索引
if has_index_on("name"):
    plan = "secondary_index_lookup"
```

它的问题不是不知道索引，而是忽略了访问代价。

经过一系列最小实验，我们得到一个更合理的直觉：

```python
# 概念伪代码：只表达决策思想，不是 MySQL 的真实 API
candidates = [
    estimate_table_scan(),
    estimate_secondary_index_lookup(),
    estimate_covering_index_lookup(),
]

plan = min(candidates, key=lambda p: p.estimated_cost)
```

然后，我们用 MySQL 8.4.0 源码验证到具体机制：

| 刚才推导出的需要 | 对应源码证据 | 它在解决什么 |
|---|---|---|
| 在读取前形成计划 | `JOIN::optimize()` | 进入查询块的优化阶段 |
| 建立扫描成本基准 | `table->file->table_scan_cost()` | 估算表扫描候选代价 |
| 考虑处理记录的工作量 | `cost_model->row_evaluate_cost(...)` | 估算行条件检查的 CPU 代价 |
| 比较覆盖索引扫描 | `index_scan_cost(...)` 与 `key_read_time < cost_est` | 将索引候选和已有成本作比较 |
| 尝试按条件缩小索引访问范围 | `get_key_scans_params(...)` | 评估符合条件的范围访问候选 |

注意，这是一份**本章涉及的局部源码地图**。优化器还具有其它计划搜索和选择机制；我们没有把整个 MySQL 查询优化过程简化成一个单独的 `min()` 函数。

同样需要区分：

- **优化器负责选路径。** 它得到的是一个用于后续执行的访问方案。
- **执行器负责按路径读取。** 它才会实际驱动相应的数据访问操作。
- **存储引擎负责完成底层读取。** 之前我们研究的 InnoDB 索引和 Buffer Pool 在这里发挥作用。

这三个职责不能混成“优化器自己读取数据”。

## 14. 一个经常出现的误区：为什么 SQL 写法相同，计划却不同？

假设你在开发环境看到：

```text
key = idx_name
```

但上线之后，同一条 SQL 却选择了表扫描。

这不一定是优化器失效。

你首先应该比较：

**数据量是否相同？** 开发环境只有几百条记录时，扫描可能已经很便宜。

**匹配比例是否相同？** 测试库可能只有几件 `Keyboard`，线上却可能有大量同名商品。

**索引结构是否相同？** 一张表可能只有 `idx_name`，另一张表多了 `(name, stock)` 覆盖索引。

**统计信息是否相同？** 过时的统计估计可能使优化器误判匹配行数。

**运行条件是否相同？** 缓存、成本参数和查询细节都可能影响代价估算。

因此，排查计划问题应当结合真实数据和执行计划，而不是把 `type = ALL` 一概视为错误，也不能把 `Using index` 一概视为最优。

优化器追求的是在它知道的信息和采用的模型下得到合理计划，而不是保证每次都选择真实耗时最短的实现。

## 15. 本章验证与边界

为了避免把三种证据混在一起，这里明确记录本章完成了什么。

**已经实际运行：** Python 的教学成本模型。输入 11 和 9000 两种匹配数量，输出了三条候选路径的估算成本，并通过断言确认：普通二级索引在高匹配数量时可能比表扫描更贵，而覆盖索引可以改变成本比较结果。

**已经核对：** MySQL 8.4.0 `mysql-8.4.0` 标签中的 `JOIN::optimize()`、`test_quick_select()`、表扫描成本估算、覆盖索引扫描成本估算和 `get_key_scans_params()` 调用。

**尚未执行：** 本节给出的真实 MySQL 8.4 SQL 建表、数据分布更新、`EXPLAIN`、`EXPLAIN ANALYZE` 与 Optimizer Trace 实验。由于没有在本地真实服务器完成执行，因此本文没有提供虚构的执行计划、耗时或 Trace JSON。

**当前没有解决：** 当优化器真正选中了 `idx_name` 或 `idx_name_stock` 后，执行器怎样把“选好的计划”变成一次次底层读取。

这就是下一章的问题。

## 16. 下一章：执行计划决定了，谁真正去读取数据？

现在我们已经能够解释为什么同一条 SQL 可能出现不同的访问计划。

例如，优化器判断：

```text
使用 idx_name_stock 查找 name='Keyboard'
```

这只是一个决定。

商品库存还没有因此被自动读取。

那么，真正执行时，谁拿到这个决定？

它怎样初始化索引访问？

怎样获取第一条符合条件的记录，再继续读取下一条？

读取到的数据，又是怎样返回给 SQL 查询结果的？

下一章，我们将继续沿着同一条 SELECT，研究：

**《第 19 章：从执行计划到记录读取——执行器怎样驱动 InnoDB？》**

到这里，SELECT 的完整源码旅程才会逐渐闭合。

---

## 源码与参考资料

本章固定 MySQL 8.4.0（Git Tag：`mysql-8.4.0`）。

- [MySQL 8.4.0 `sql/sql_optimizer.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/sql_optimizer.cc)：`JOIN::optimize()`。
- [MySQL 8.4.0 `sql/range_optimizer/range_optimizer.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/range_optimizer/range_optimizer.cc)：`test_quick_select()`、扫描与索引候选成本比较。
- [MySQL 8.4 EXPLAIN 官方文档](https://dev.mysql.com/doc/refman/8.4/en/explain.html)。
- [MySQL 8.4 优化器成本模型](https://dev.mysql.com/doc/refman/8.4/en/cost-model.html)。
- [MySQL 8.4 为什么可能选择全表扫描](https://dev.mysql.com/doc/refman/8.4/en/table-scan-avoidance.html)。
- [MySQL 8.4 Optimizer Trace](https://dev.mysql.com/doc/refman/8.4/en/optimizer-tracing.html)。

> 本章展示的 Python 成本单位、页面容量和回表单价都是教学假设，不能用于推导 MySQL 8.4.0 的真实 `cost` 数值。源码节选均针对固定标签核对，省略的分支没有被描述为不存在。
