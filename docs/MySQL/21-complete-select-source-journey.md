---
title: "21｜一条 SELECT 的完整源码旅程：从请求进入到结果返回"
sidebar_position: 22
slug: /mysql/21-complete-select-source-journey
description: "沿 MySQL 8.4.0 的真实源码，将 SQL 解析、优化器、AccessPath、RowIterator、InnoDB 索引读取与客户端结果集连接成一条可验证的主线。"
tags: [MySQL, InnoDB, SELECT, 查询优化器, RowIterator, 源码精读]
---

# 一条 SELECT 的完整源码旅程：从请求进入到结果返回

前面五章，我们一直围绕商品查询推进：一开始只能通过主键找商品 42；后来用户只知道商品名称，我们添加了二级索引；接着发现查询的 `stock` 不一定在二级索引中，于是理解回表与覆盖索引；当不止一种访问方式可用时，又进入查询优化器；最后，我们观察执行器怎样通过迭代器不断取得记录，甚至让两个读取过程组合成 JOIN。

每一步都有自己的问题，但我们还欠读者一个完整的答案：

**用户只发送了一条 `SELECT`，MySQL 到底怎样让结果从磁盘或内存中的记录，变成客户端屏幕上的一行数据？**

这一次，我们不再增加新的数据结构。我们要把已经认识的机制放回同一次请求，检验它们之间的职责交接是否真的成立。

## 1. 固定贯穿 SQL：按商品名称读取库存

我们继续使用商品 42：`Keyboard`，当前库存 `99`。为了让读取循环清楚可见，另外再放入两个同名商品。

```sql
CREATE DATABASE IF NOT EXISTS mysql_source_lab;
USE mysql_source_lab;

-- 只在专用实验库执行：下面会删除并重建 products。
DROP TABLE IF EXISTS products;

CREATE TABLE products (
    id BIGINT PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    stock INT NOT NULL,
    KEY idx_name_stock (name, stock)
) ENGINE = InnoDB;

INSERT INTO products (id, name, stock) VALUES
    (8,  'Mouse',    20),
    (17, 'Monitor',  35),
    (42, 'Keyboard', 99),
    (77, 'Keyboard', 30),
    (91, 'Keyboard', 50);
```

本章追踪的 SQL 是：

```sql
SELECT stock
FROM products FORCE INDEX (idx_name_stock)
WHERE name = 'Keyboard';
```

正确结果应该包含 `30`、`50`、`99` 这三个库存值。因为没有写 `ORDER BY`，**客户端收到记录的顺序不属于 SQL 保证**。我们在教学模型里会按 `(name, stock, id)` 排列索引项，仅为了让执行轨迹稳定可观察。

`FORCE INDEX` 用于让指定索引成为更有可能被选择的访问路径；这仍不意味着可以不检查执行计划就断言某个特定迭代器一定出现。后面提到的 `RefIterator` 路径，需要以 `EXPLAIN` 实际展示的 `ref` 访问为前提。

我们只回答这次 `SELECT` 的正常读取过程。没有显式加锁读取，没有 `GROUP BY`、`DISTINCT`、子查询或复杂排序。第 20 章已经讲过 JOIN，我们会在结尾说明它怎样嵌入同一套迭代器模型，而不让主线被复杂分支淹没。

## 2. 先想一个最简单的版本：为什么不能由一个函数包办一切？

假设我们仍然自己写小数据库，很容易把全部步骤塞进一个函数：

```python
# 教学化简：完全不是 MySQL 的实现

def select_stock(products, name):
    return [p["stock"] for p in products if p["name"] == name]
```

对五条记录，这没有任何问题。可是这样一个函数把至少三件事情揉在了一起：它认定用户要做什么，它决定从哪里找数据，它还决定以什么形式返回结果。

如果查询条件稍微变化，数据库可以选全表扫描，也可以利用索引；如果结果有十万行，执行器不一定要先拼出一个十万行的 Python 列表；如果客户端通过 MySQL 协议连接，返回结果也不能仅仅是 `return [30, 50, 99]`。

我们真正需要的是若干清晰的职责边界：

- 有一处负责把 SQL 变成数据库理解的语句；
- 有一处负责确定怎样找记录；
- 有一处负责按已选方案读取记录；
- 有一处负责把取得的结果交给客户端协议。

这不是为了先设计复杂架构，而是因为**用户提交的是文本，底层保存的是记录，客户端期望的是结果集**。三者之间必须有人负责转换。

现在我们沿源码找到这些转换发生的位置。

## 3. 第一次职责交接：文本请求变成 SELECT 命令

本章固定使用 [MySQL 8.4.0 源码标签 `mysql-8.4.0`](https://github.com/mysql/mysql-server/tree/mysql-8.4.0)。与之前一样，我们只追踪普通客户端通过 `COM_QUERY` 发送文本 SQL 的正常路径，预处理语句协议另有细节。

从 `sql/sql_parse.cc` 开始，前面已核对过的入口是：

```text
客户端的 COM_QUERY
      │
      ▼
do_command()
      │
      ▼
dispatch_command()
      │
      ▼
dispatch_sql_command()
      │
      ▼
parse_sql()
```

这个阶段的输入是文本 SQL，不是记录。`parse_sql()` 继续通过解析器得到相应语法树，再由语法树创建语句命令对象。第 02 章我们已经见过 `PT_update`，现在换成 SELECT：

**真实源码节选，`sql/parse_tree_nodes.cc`，`PT_select_stmt::make_cmd()`：**

```cpp
return new (thd->mem_root) Sql_cmd_select(thd->lex->result);
```

[查看 MySQL 8.4.0 对应源码](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/parse_tree_nodes.cc)

这里并没有读取商品。它只是把已经识别为 `SELECT` 的语句转成后续可以执行的命令对象。在 `sql/sql_parse.cc` 的 `mysql_execute_command()` 中，`SQLCOM_SELECT` 等语句类型会走到命令对象的 `execute(thd)`。

为什么要多这一步？因为**识别一条合法 SELECT**与**真正执行 SELECT**不是同一件事。比如 SQL 语法可以正确，却引用了一张不存在的表；也可能表存在，但访问方法还没有决定。源码把准备与执行留在后续步骤，避免让词法、语法逻辑直接承担记录读取。

这就把问题推进了一层：`Sql_cmd_select` 已经存在，下一步怎样决定使用 `idx_name_stock`？

## 4. 第二次职责交接：SELECT 命令变成访问计划

普通 SELECT 在 `sql/sql_select.cc` 中复用 `Sql_cmd_dml` 的通用执行流程。这里的名字容易引起误解：`Sql_cmd_dml` 并不意味着这条 SELECT 会修改数据。

在 `Sql_cmd_dml::execute()` 中，服务器会处理语句准备、表的打开和相关检查，然后进入 `execute_inner()`。其中有我们需要的关键动作。

**真实源码节选，`sql/sql_select.cc`，`Sql_cmd_dml::execute_inner()`：**

```cpp
if (unit->optimize(thd, /*materialize_destination=*/nullptr,
                   /*create_iterators=*/true, /*finalize_access_paths=*/true))
  return true;
```

[查看 MySQL 8.4.0 对应源码](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/sql_select.cc)

现在关注输入和输出。`unit` 是当前语句关联的查询表达式；优化动作不是获取最终的三条库存，而是为查询准备它的执行访问路径，并根据需要创建执行器迭代器。

`Query_expression::optimize()` 位于 `sql/sql_union.cc`。它会依次处理其中的查询块；对当前单表 SELECT，优化会进入对应查询块的处理，进一步使用 `JOIN::optimize()` 等优化逻辑。上一章研究的 `JOIN` 一词在源码里也用于普通单表查询的优化上下文，**不要看到 `JOIN` 就认定 SQL 一定关联了多张表**。

执行计划为什么必须在取记录之前存在？因为同一条语句至少可能有以下访问方案：

```text
SELECT stock WHERE name = 'Keyboard'
                    │
       ┌────────────┼────────────┐
       ▼            ▼            ▼
    全表扫描    idx_name 后回表   idx_name_stock 覆盖
```

这三种方案可能都得到相同数据，但读取记录的成本不同。优化器需要依据当前的统计信息、索引与 SQL 结构估算方案代价，再形成选中的访问路径。它并非把所有方案都真实执行一遍，测完耗时才选择最快的。

对我们的 `idx_name_stock(name, stock)`，由于筛选字段 `name` 和返回字段 `stock` 都在该索引中，存在使用覆盖索引完成读取的可能。但**是否真的用到它、采用什么访问类型，必须看实际计划**；绝不能仅仅根据建表语句就写死运行中的函数路径。

现在可以把第一段调用链连接起来：

```text
Sql_cmd_select
    │
    ▼
Sql_cmd_dml::execute()
    │
    ▼
Sql_cmd_dml::execute_inner()
    │
    ▼
Query_expression::optimize()
    │
    ▼
查询块优化（包括 JOIN::optimize 等）
    │
    ▼
形成选定的 AccessPath
```

图中省略了命令对象的虚函数分派、准备阶段和优化器内部的具体分支。它表示本次需要理解的职责关系，不是逐帧调用栈。

但 `AccessPath` 只是告诉系统**应该怎样访问**，它自己并不替客户端不断读取行。于是进入下一处交接。

## 5. 第三次职责交接：AccessPath 变成能读取数据的 RowIterator

想象我们自己写的程序已经决定“使用名称索引”。下一步不能只把“使用名称索引”写在一张纸上，必须有真正的读取函数来执行它。

真实 MySQL 在 `sql/sql_union.cc` 的 `Query_expression::optimize()` 后半段处理这一问题：

**真实源码节选：**

```cpp
m_root_iterator = CreateIteratorFromAccessPath(
    thd, m_root_access_path, join, /*eligible_for_batch_mode=*/true);
```

[查看 MySQL 8.4.0 `sql/sql_union.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/sql_union.cc)

`m_root_access_path` 是已经形成的访问路径；`m_root_iterator` 是用它创建出来的可执行读取对象。`CreateIteratorFromAccessPath()` 位于 `sql/join_optimizer/access_path.cc`。

它会根据访问路径类型构建不同的迭代器。例如：

| AccessPath 类型 | 可能对应的迭代器 | 适合当前理解的含义 |
|---|---|---|
| `TABLE_SCAN` | `TableScanIterator` | 顺序获取表中的记录并处理条件 |
| `REF` | `RefIterator` | 使用非唯一或唯一索引的键值匹配，可能得到多条记录 |
| `EQ_REF` | `EQRefIterator` | 对联接等场景中的唯一键做精确匹配 |
| `INDEX_RANGE_SCAN` | `IndexRangeScanIterator` | 按指定索引范围获取记录 |

表格列出源码中存在的对应关系，不意味着当前 SQL 同时走这四条路径。

如果 `EXPLAIN` 表明当前 `name = 'Keyboard'` 采用 `ref` 访问，我们可以进一步研究：

```text
AccessPath::REF
      │
      ▼
RefIterator<false>
      │
      ▼
Init() / Read()
```

这里的 `false` 表示常见的正向读取实例化形式；其他方向和访问方法也有对应实现。

重要的变化是：执行器得到了一个可以反复调用的接口。它不必知道二级索引页面怎样分裂，也不必把 Buffer Pool 当作自己负责管理的缓存。

它只需要反复请求：“给我下一条符合访问条件的记录。”

## 6. 第四次职责交接：迭代器通过 handler 调用 InnoDB

让我们假设本次执行计划确实选择了 `REF` 访问。

`sql/iterators/ref_row_iterators.cc` 中的 `RefIterator<false>::Read()` 区分第一次获取记录与后续读取：

- **第一次**：构造索引查找键，调用 handler 的 `ha_index_read_map()`，定位符合 `name = 'Keyboard'` 的起始记录。
- **之后**：调用 `ha_index_next_same()`，继续取同一键值范围内的后续记录。
- **结束**：底层报告没有更多符合条件的记录时，迭代器转换为结束状态。

相关的真实代码可以在 [MySQL 8.4.0 `sql/iterators/ref_row_iterators.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/iterators/ref_row_iterators.cc) 中找到。

我们为什么还需要 `handler` 这一层？因为执行器在描述“根据键找记录”，而具体存储引擎负责理解索引的实际组织。两者不应该互相硬编码内部数据结构。

`handler::ha_index_read_map()` 的实现位于 `sql/handler.cc`。它进一步进入当前存储引擎的索引读取接口。对于本章的 InnoDB 表，会进入相应的 `ha_innobase` 实现，转换查找键并调用底层记录搜索。

**真实源码节选，`storage/innobase/handler/ha_innodb.cc`：**

```cpp
ret = row_search_mvcc(buf, mode, m_prebuilt, match_mode, 0);
```

[查看 MySQL 8.4.0 `ha_innodb.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/handler/ha_innodb.cc)

这行出现在 `ha_innobase::index_read()` 的普通 InnoDB 表读取路径中；同一函数还有内部表等特殊分支，不应把这行说成任何场景下都会无条件调用。

此前我们已经学过，这个搜索过程会涉及索引记录定位，以及必要时通过 Buffer Pool 获取页面。如果需要读取二级索引中没有的字段，可能还要通过聚簇索引取得完整记录。但当前查询的 `stock` 可以由 `(name, stock)` 索引提供，避免了那一种典型回表需求。

所以，执行器与 InnoDB 的边界可以画成：

```text
RefIterator::Read()
        │
        ▼
handler::ha_index_read_map() / ha_index_next_same()
        │
        ▼
InnoDB handler 的对应索引接口
        │
        ▼
row_search_mvcc() 等存储引擎读取逻辑
        │
        ▼
索引、Buffer Pool、记录版本可见性
        │
        ▼
按接口约定把结果放入行缓冲区
```

这里为了直观，把首次定位与后续读取并列画出。后续读取不会每次都重新执行完全相同的首次定位流程。

还有一个特别重要的细节：

**`RowIterator::Read()` 不是每次返回一个 C++ 行对象。**

常见实现使用状态码表示读取成功、结束或失败，记录值存放在表的行缓冲区等位置，供上层随后计算与发送。正因如此，不能把这条调用链粗暴画成：`row = innodb.read(); socket.send(row)`。

但我们仍然没有到达客户端。现在只是“存储引擎已经按要求提供记录”。谁负责把 `stock = 99` 变成结果集的一行？

## 7. 第五次职责交接：读取结果怎样通过协议发回客户端？

回到 `sql/sql_select.cc`：`Sql_cmd_dml::execute_inner()` 在完成优化之后，对于普通执行而非 `EXPLAIN`，会调用查询表达式的执行函数。`Query_expression::execute()` 再进入 `Query_expression::ExecuteIteratorQuery()`。

**真实源码节选，`sql/sql_union.cc`，`ExecuteIteratorQuery()`：**

```cpp
int error = m_root_iterator->Read();
```

此处上层会判断：

- 返回值大于零，或会话存在执行错误：停止并处理错误；
- 返回值小于零：当前结果遍历结束；
- 返回值为零：当前成功得到一条可交付的逻辑记录。

当读取成功，上层会调用当前结果处理对象的 `send_data()`。对于普通向客户端发送结果的 SELECT，使用 `Query_result_send`。

[查看 `sql/sql_union.cc` 的 `ExecuteIteratorQuery()`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/sql_union.cc)

再进入 `sql/query_result.cc`，可以看到 `Query_result_send::send_data()`：它从当前连接取得 `Protocol`，开始构造一行结果，调用 `THD::send_result_set_row()` 处理查询所选的字段值，随后完成这一行。

因此，我们还必须区分：

**读到一条存储引擎记录**，只是准备好了可使用的数据；

**输出一条结果集记录**，还需要按 SELECT 列表取得字段值，通过客户端协议编码发送。

对于本章的 `SELECT stock`，发送的是 `stock` 列，而不是把整条底层 InnoDB 记录原封不动地发给客户端。

执行循环在没有更多结果时，还会通过结果对象完成正常结束通知。

到这里，才真正连接起“数据库取到记录”和“客户端看见结果”。

## 8. 用一个可运行模型复现这条链路

为了避免把多个真实 C++ 文件一次贴满屏幕，我们编写一个小型 Python 版本。它模拟三个动作：初始化索引读取位置、逐条交付结果、按 SELECT 字段输出。

**下面是教学化简，不是 MySQL 源码。它只验证调度与数据传递，不验证真实 InnoDB 页读取、MVCC 或网络协议。**

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class Product:
    id: int
    name: str
    stock: int


products = [
    Product(8, "Mouse", 20),
    Product(17, "Monitor", 35),
    Product(42, "Keyboard", 99),
    Product(77, "Keyboard", 30),
    Product(91, "Keyboard", 50),
]


class RefRead:
    def __init__(self, products, target_name):
        self.entries = sorted(
            products, key=lambda p: (p.name, p.stock, p.id)
        )
        self.target_name = target_name
        self.pos = 0
        self.row_buffer = None
        self.events = []

    def init(self):
        self.pos = 0
        while (self.pos < len(self.entries) and
               self.entries[self.pos].name < self.target_name):
            self.pos += 1
        self.events.append("Init: 定位名称 Keyboard 的起始位置")

    def read(self):
        if (self.pos >= len(self.entries) or
                self.entries[self.pos].name != self.target_name):
            self.row_buffer = None
            self.events.append("Read: -1 结束")
            return -1

        self.row_buffer = self.entries[self.pos]
        self.pos += 1
        self.events.append(
            f"Read: 0 id={self.row_buffer.id}, "
            f"stock={self.row_buffer.stock}"
        )
        return 0


class ResultSink:
    def __init__(self):
        self.rows = []

    def send_data(self, row):
        self.rows.append(row.stock)


iterator = RefRead(products, "Keyboard")
sink = ResultSink()

iterator.init()
while True:
    status = iterator.read()
    if status == -1:
        break
    assert status == 0
    sink.send_data(iterator.row_buffer)

assert sink.rows == [30, 50, 99]
assert [p.stock for p in products if p.id == 42] == [99]
assert iterator.events[-1] == "Read: -1 结束"

print("\n".join(iterator.events))
print("发出的 stock 列:", sink.rows)
print("断言：全部通过")
```

**本章实际运行输出：**

```text
Init: 定位名称 Keyboard 的起始位置
Read: 0 id=77, stock=30
Read: 0 id=91, stock=50
Read: 0 id=42, stock=99
Read: -1 结束
发出的 stock 列: [30, 50, 99]
断言：全部通过
```

我们现在可以把每一步和真实源码里的责任联系起来：

| 教学模型 | MySQL 中相应的职责 |
|---|---|
| `RefRead.init()` | 初始化访问迭代器，准备首次读取 |
| `RefRead.read()` | 迭代器通过 handler 与 InnoDB 获取下一条记录 |
| `row_buffer` | 成功读取后供执行阶段访问的记录值；真实实现更复杂 |
| `ResultSink.send_data()` | 查询结果对象提取所选列并交给协议层 |
| 返回 `-1` | 迭代器报告正常结束，结果集完成发送 |

这个实验的重要性不在于 5 条记录有多快，而在于**结构化说明了谁负责决定访问方法、谁负责提供下一条记录、谁负责对客户端输出**。如果删掉 `ResultSink`，执行器可以读到数据，却没有人负责发送；如果删掉迭代器，结果处理器也不能凭空产生记录。

## 9. 在真实 MySQL 中观察这条路径

准备好本章开头的实验表后，可以执行：

```sql
EXPLAIN FORMAT=TREE
SELECT stock
FROM products FORCE INDEX (idx_name_stock)
WHERE name = 'Keyboard';
```

关注计划树是否表现为利用 `idx_name_stock` 的索引查找。因为样本数据量很小，成本和实际计划可能受版本、统计信息及提示策略影响；**以本机输出为准**。不要把教学模型的 `RefRead` 等同于每台 MySQL 必然实例化的 `RefIterator`。

再执行：

```sql
EXPLAIN ANALYZE
SELECT stock
FROM products FORCE INDEX (idx_name_stock)
WHERE name = 'Keyboard';
```

它会真正执行查询，并展示有关迭代器运行的计时、行数等观测信息。预期匹配到三条商品记录，但具体计划行文与时间由实际实例决定，本文不虚构输出。

接着执行查询本身：

```sql
SELECT stock
FROM products FORCE INDEX (idx_name_stock)
WHERE name = 'Keyboard';
```

核对返回的库存值集合是否为 `{30, 50, 99}`。没有 `ORDER BY` 的情况下，不应断言行顺序。

最后比较没有匹配记录的情况：

```sql
SELECT stock
FROM products FORCE INDEX (idx_name_stock)
WHERE name = 'Missing Product';
```

预期返回空结果集。它并非“查询没有执行”，而是执行器完成了读取过程，结果处理器没有收到可发送的数据行，并正常结束本次查询。

### 如何进一步验证源码调用？

`EXPLAIN` 验证访问计划，`EXPLAIN ANALYZE` 验证执行过程的可观察统计，但两者都不直接证明某个 C++ 函数在某一次请求中被调用。

如果你自己编译了 MySQL 8.4.0 的调试版本，可以按本章索引的源码入口设置断点，例如 `Query_expression::ExecuteIteratorQuery()`、`Query_result_send::send_data()`，并结合实际计划选择 `RefIterator<false>::Read()` 或相应范围扫描的断点。不同优化计划可能走不同迭代器，断点未命中不能单独证明源码错误。

本章**没有运行真实 MySQL 服务端，也没有声称取得真实 GDB 调用栈**。已实际运行并完成断言的是上面的 Python 教学模型。

## 10. JOIN 并没有另起一套结果发送系统

上一章，我们研究过：

```sql
SELECT o.id, p.name, o.quantity
FROM orders AS o
JOIN products AS p
  ON o.product_id = p.id;
```

它比单表 SELECT 多了两张表之间的匹配工作。但从整体框架看，前面的分工并没有被推翻：

1. 解析阶段仍需弄清楚 SELECT 列表、FROM、JOIN 条件；
2. 优化阶段仍需决定访问路径和关联方式；
3. 执行阶段会把所需的单表迭代器与连接迭代器组合起来；
4. 最外层仍然以“下一条结果”的方式向结果处理器提供逻辑行；
5. `Query_result_send` 仍然负责把选中的列输出给客户端。

如果执行计划选了嵌套循环，连接迭代器会按照外侧行驱动内侧读取；如果选了 Hash Join，内部又会有构建与探测阶段。**它们的差异发生在迭代器树里面，而不是需要重新发明一套 SQL 接收和客户端返回协议。**

这就是迭代器分层的一个实际收益：不同访问算法可以拥有不同内部机制，同时服从统一的上层交付约定。

## 11. 现在才画出完整的 SELECT 架构图

最开始我们只知道：

```text
SQL → 查询结果
```

研究到这一章，我们终于有资格把图补充完整：

```text
客户端：SELECT stock WHERE name = 'Keyboard'
                       │
                       ▼
       Server：接收 COM_QUERY
                       │
                       ▼
       语法解析，形成 SELECT 命令
                       │
                       ▼
       Sql_cmd_dml::execute_inner()
                       │
                       ▼
       Query_expression::optimize()
                       │
                       ▼
       访问路径：AccessPath
                       │
                       ▼
       CreateIteratorFromAccessPath()
                       │
                       ▼
       RowIterator::Init() / Read()
                       │
                       ▼
       handler 存储引擎接口
                       │
                       ▼
       InnoDB：索引定位、记录读取
                       │
                       │ 将行数据交回执行器
                       ▼
       Query_result_send::send_data()
                       │
                       ▼
       Protocol：将查询列编码发送
                       │
                       ▼
客户端获得 stock 结果集
```

**读图边界：** 这是把本章关键阶段串联起来的教学调用图，省略了准备、授权、存储引擎内部递归搜索、异常分支等。它不是无条件适用于所有 SELECT 的唯一 C++ 调用栈。特别是不同 `AccessPath` 对应不同迭代器；某些计划可能在优化阶段就完成常量表处理、过滤与子查询处理，不能照搬 `REF` 一条分支。

另一个边界是：我们写了“InnoDB 读取记录”，**不等于每读一行都去磁盘进行物理 I/O**。之前已经解释过 Buffer Pool 缓存；命中缓存时，数据可以直接来自内存。结果返回客户端同样不意味着底层把一个数据页原样发送到网络。

## 12. 从这条 SELECT 中，我们真正理解了什么？

现在可以沿一个连续的问题链条复述整个设计：

客户端给的是文本，所以需要识别 SQL 的结构，并构造可以执行的命令。

有了命令，仍然存在多种正确的取数方案，所以需要在获取结果之前决定访问路径。

有了访问路径，不代表已经取到了数据，因此需要把计划变成能够不断读取记录的迭代器。

迭代器知道上层需要什么访问方式，却不应把 InnoDB 的页面结构写死在 Server 执行代码中，所以通过 handler 进入存储引擎。

InnoDB 获取记录后，还不能直接认为客户端收到了数据；Server 需要根据 SELECT 列表提取要输出的值，再通过结果处理器和协议层完成传输。

于是，最初的一条 SQL 才真正从文本变成结果：

```sql
SELECT stock FROM products WHERE name = 'Keyboard';
```

我们并不是把 Parser、Optimizer、Executor、Handler、InnoDB、Protocol 这些名词按列表背下来，而是逐段发现：**如果缺少某一次职责交接，整条查询就无法完成。**

这也是 SELECT 源码主线的终点：读者已经可以从请求入口沿源码到存储引擎，再返回到客户端输出。

## 13. 下一步：读取有了正确结果，但“哪一个时间点”才算正确？

我们此前研究过 MVCC 的基本机制，也知道读取历史版本可能避免与未提交的写入直接互相等待。

现在有一个仍未完整回答的问题：

事务 A 连续执行两次：

```sql
SELECT stock FROM products WHERE id = 42;
```

两次 SELECT 之间，事务 B 将商品 42 的库存从 `99` 修改为 `98` 并提交。

事务 A 第二次到底应该看到 `99`，还是 `98`？

前面掌握的索引、Buffer Pool 和 SELECT 执行器都能解释如何取得一条记录，却不能单独决定**哪个版本应该对当前事务可见**。

这就是下一个专题要研究的问题：

**第 22 章《从快照可见性到隔离级别：为什么两次 SELECT 可能不同？》**

---

## 本章源码地图与验证边界

固定源码版本：**MySQL 8.4.0 / Git Tag `mysql-8.4.0`**。

| 文件 | 建议查找的入口 | 本章用途 |
|---|---|---|
| [`sql/sql_parse.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/sql_parse.cc) | `dispatch_sql_command`、`mysql_execute_command` | 从 COM_QUERY 进入 SQL 命令 |
| [`sql/parse_tree_nodes.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/parse_tree_nodes.cc) | `PT_select_stmt::make_cmd` | 生成 SELECT 命令对象 |
| [`sql/sql_select.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/sql_select.cc) | `Sql_cmd_dml::execute`、`execute_inner` | 准备、优化和发起执行 |
| [`sql/sql_union.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/sql_union.cc) | `Query_expression::optimize`、`ExecuteIteratorQuery` | 建立迭代器并逐行取结果 |
| [`sql/sql_optimizer.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/sql_optimizer.cc) | `JOIN::optimize` | 查询块的成本估算与计划选择 |
| [`sql/join_optimizer/access_path.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/join_optimizer/access_path.cc) | `CreateIteratorFromAccessPath` | 从访问路径生成执行器迭代器 |
| [`sql/iterators/ref_row_iterators.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/iterators/ref_row_iterators.cc) | `RefIterator<false>::Read` | 以 `ref` 访问为例说明实际读取 |
| [`sql/handler.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/handler.cc) | `handler::ha_index_read_map` | Server 与存储引擎之间的接口 |
| [`storage/innobase/handler/ha_innodb.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/handler/ha_innodb.cc) | `ha_innobase::index_read` | 将键值查找交给 InnoDB |
| [`sql/query_result.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/query_result.cc) | `Query_result_send::send_data` | 将 SELECT 字段发送到客户端协议 |

- **已完成的运行验证**：Python 教学迭代器实际执行，依次产生 `30、50、99`，全部断言通过。
- **已核对的静态事实**：以上列出的关键函数及相互衔接的代码位置来自 MySQL 8.4.0 固定标签。
- **尚未执行的真实环境实验**：本文提供 `EXPLAIN`、`EXPLAIN ANALYZE` 和查询语句，但没有运行真实 MySQL 8.4 实例或 GDB，因此不虚构执行计划输出、毫秒耗时和调用栈。

**到这里，第二篇 SELECT 主线结束。接下来不是继续堆更多查询算法，而是研究同一条 SELECT 在并发事务中应该读取哪个版本。**
