---
title: "19｜从执行计划到记录读取：执行器怎样驱动 InnoDB？"
sidebar_position: 20
slug: /mysql/19-executor-drives-innodb
description: "沿商品名称查询，逐层追踪 MySQL 8.4.0 如何把访问路径变成迭代器，调用存储引擎读取记录，并将 stock 返回客户端。"
tags: [MySQL, InnoDB, 查询执行器, RowIterator, handler, 源码精读]
---

# 从执行计划到记录读取：执行器怎样驱动 InnoDB？

上一章，我们用同一条商品查询，对比了三种可能的访问方法：全表扫描、二级索引后回表、通过覆盖索引直接读取库存。

```sql
SELECT stock
FROM products
WHERE name = 'Keyboard';
```

三条路径都可能得到正确结果，但成本不同。我们由此进入了 MySQL 的查询优化器，看到它如何利用估算结果选择访问路径。

但有一个关键问题还没回答。

**优化器选择了索引，并不等于已经从索引中读到了任何商品。**

假设它认为使用 `idx_name_stock(name, stock)` 比较合适。此时 MySQL 至多决定了应该**怎样**读取，实际读取工作还没有结束，甚至可能还没有开始。

现在谁来执行这份计划？它怎样第一次定位 `Keyboard`？怎样获得第二件、第三件同名商品的库存？读取出来的值又如何变成客户端看到的结果集？

这就是本章的唯一主问题：

> **一份已经选好的读取方案，怎样变成一次又一次真正的 InnoDB 记录访问？**

我们仍然使用前面一直在研究的商品表，不引入 JOIN、排序或事务写入的新机制。

## 1. 先明确我们的输入与预期输出

为了让一个索引查找不只返回一条记录，我们准备三个名称相同的商品。原来的商品 42 仍然在其中。

```sql
CREATE DATABASE IF NOT EXISTS mysql_source_lab;
USE mysql_source_lab;

-- 仅限独立实验库：以下语句会删除并重建 products。
DROP TABLE IF EXISTS products;
CREATE TABLE products (
    id BIGINT PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    stock INT NOT NULL,
    KEY idx_name_stock (name, stock)
) ENGINE = InnoDB;

INSERT INTO products(id, name, stock) VALUES
    (8,  'Mouse',    20),
    (17, 'Monitor',  35),
    (42, 'Keyboard', 99),
    (77, 'Keyboard', 30),
    (91, 'Keyboard', 50);
```

这里特意保留库存 `99`，因为在我们的主线中，商品 42 已经经历过一次成功购买。

执行查询：

```sql
SELECT stock
FROM products FORCE INDEX (idx_name_stock)
WHERE name = 'Keyboard';
```

我们预期返回三个库存值：`30`、`50`、`99`。没有 `ORDER BY` 时，**SQL 不保证结果顺序**，因此不要把某个观察到的行顺序当作语义承诺。

`FORCE INDEX` 在此只为了减少实验中的计划变化，方便观察指定索引；它不是强制底层一定使用我们下面分析的某个 C++ 迭代器类。**需要先检查 `EXPLAIN` 实际选出的访问类型，才有资格套用对应的源码路径。**

上一章解决了“选择哪种读取方式”。这一章要解释的是：选中之后，怎样把这些值一条条读出来。

## 2. 最直观的实现：一次把全部结果找出来

假如现在仍然是我们自己实现的小数据库，最直接的方案是让一个函数返回所有匹配记录。

**教学化简，不是 MySQL 源码：**

```python
products = [
    {"id": 8, "name": "Mouse", "stock": 20},
    {"id": 17, "name": "Monitor", "stock": 35},
    {"id": 42, "name": "Keyboard", "stock": 99},
    {"id": 77, "name": "Keyboard", "stock": 30},
    {"id": 91, "name": "Keyboard", "stock": 50},
]


def find_by_name(name):
    return [row for row in products if row["name"] == name]


result = find_by_name("Keyboard")
print([row["stock"] for row in result])
```

这个程序能够正确找到三条记录。对五行数据来说，也没有明显问题。

但是，假设同名商品有十万条，而调用者只想显示其中一部分，或者读取结果的下一步还需要执行其他操作。

我们真的要在执行下一步之前，先把十万条结果全部装进一个列表吗？

不一定。

与其一次返回“所有结果”，我们可以改成：**调用方每次向读取方要一条记录。**读取方保存自己已经读到哪里，下次继续往后。

这个小变化让执行过程具有了一种很重要的形态：

```text
初始化读取位置
      |
      v
请求下一条 -----> 得到记录 -----> 处理这条记录
      ^                              |
      |------------------------------|
      |
      +---- 没有更多记录：结束
```

这并不意味着数据库一定只读一个磁盘页或一个物理记录，也不意味着不存在预读、批量处理或内部缓冲。它只是**上层与下层之间逐行取得逻辑记录的一种接口约定**。

这种接口模式，就是我们现在才需要引入的：**行迭代器（Row Iterator）**。

在继续看真实源码前，我们先用一个小程序把这种协作方式跑通。

## 3. 最小可运行实验：读取方每次交付一条记录

上一章已经介绍过覆盖索引 `idx_name_stock(name, stock)`。为了模拟按名字从该索引读取，我们先准备一组按 `(name, stock, id)` 排序的索引项。这里的 `id` 代表 InnoDB 二级索引记录中可用于定位聚簇记录的主键信息。

**教学化简：这里使用 Python 排序列表模拟索引，不是 InnoDB B+ 树，也不模拟真实的行格式。**

```python
from bisect import bisect_left

rows = [
    {"id": 8, "name": "Mouse", "stock": 20},
    {"id": 17, "name": "Monitor", "stock": 35},
    {"id": 42, "name": "Keyboard", "stock": 99},
    {"id": 77, "name": "Keyboard", "stock": 30},
    {"id": 91, "name": "Keyboard", "stock": 50},
]

secondary = sorted(
    (row["name"], row["stock"], row["id"])
    for row in rows
)


class RefIteratorDemo:
    def __init__(self, items, lookup, record_buffer):
        self.items = items
        self.lookup = lookup
        self.record_buffer = record_buffer
        self.pos = 0
        self.read_calls = 0

    def init(self):
        self.pos = bisect_left(self.items, (self.lookup, -1, -1))
        self.read_calls = 0
        print("Init: 定位到名称范围", self.lookup)

    def read(self):
        self.read_calls += 1

        if (self.pos >= len(self.items)
                or self.items[self.pos][0] != self.lookup):
            print("Read: EOF")
            return -1

        name, stock, pk = self.items[self.pos]
        self.pos += 1

        # 复用一个记录缓冲区，而不是每次返回新的行对象。
        self.record_buffer.clear()
        self.record_buffer.update(
            {"name": name, "stock": stock, "id": pk}
        )
        print(f"Read: record[0] <- (id={pk}, stock={stock})")
        return 0


buffer = {}
iterator = RefIteratorDemo(secondary, "Keyboard", buffer)
iterator.init()

result = []
while True:
    status = iterator.read()
    if status == -1:
        break
    if status != 0:
        raise RuntimeError("读取失败")

    # 只提取这条 SQL 需要的 stock。
    result.append(buffer["stock"])
    print("Send: stock =", buffer["stock"])

print("Result:", result)
print("Read 调用次数:", iterator.read_calls)
assert result == [30, 50, 99]
assert iterator.read_calls == 4
```

这段 Python 已实际运行，输出为：

```text
Init: 定位到名称范围 Keyboard
Read: record[0] <- (id=77, stock=30)
Send: stock = 30
Read: record[0] <- (id=91, stock=50)
Send: stock = 50
Read: record[0] <- (id=42, stock=99)
Send: stock = 99
Read: EOF
Result: [30, 50, 99]
Read 调用次数: 4
```

只需要观察三个关键事实。

第一，`init()` 只在开头执行一次。它负责让读取方准备开始读取的状态。

第二，`read()` 可以调用多次。每次成功读取都把当前记录放到同一个 `record_buffer` 中，调用方随后从中提取 `stock`。

第三，虽然只有三条匹配记录，但 `read()` 被调用了四次。最后一次没有返回记录，而是告诉调用方“已经没有下一条了”。

这比“一次性返回所有行”多了一套交互协议，但现在我们能解释它为什么存在：**读取方和消费方可以逐条协作，而不需要把整个结果集预先构造出来。**

现在去 MySQL 8.4.0 源码中验证：它是否采用了类似的接口模式？

## 4. 从执行计划到迭代器：谁把计划变成可执行对象？

本章源码统一固定为：

| 属性 | 固定值 |
|---|---|
| 仓库 | [`mysql/mysql-server`](https://github.com/mysql/mysql-server) |
| Tag | [`mysql-8.4.0`](https://github.com/mysql/mysql-server/tree/mysql-8.4.0) |
| 版本 | MySQL 8.4.0 |
| 当前正常路径 | 单表、普通文本 `SELECT`、选定索引的读取计划 |

先打开 [sql/join_optimizer/access_path.cc](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/join_optimizer/access_path.cc)。

在这里可以找到：

```cpp
CreateIteratorFromAccessPath(...)
```

这个名字中的 `AccessPath` 就是访问路径。它描述**应该怎样取得记录**，但描述本身还不能完成读取。

在该函数处理不同访问类型的分支中，有这样的真实源码节选（只截取关键几行）：

```cpp
case AccessPath::TABLE_SCAN: {
  const auto &param = path->table_scan();
  iterator = NewIterator<TableScanIterator>(
      thd, mem_root, param.table, path->num_output_rows(), examined_rows);
  break;
}
```

接下来还有针对 `REF` 访问类型的分支：

```cpp
case AccessPath::REF: {
  const auto &param = path->ref();
  if (param.reverse) {
    iterator = NewIterator<RefIterator<true>>(
        thd, mem_root, param.table, param.ref, param.use_order,
        path->num_output_rows(), examined_rows);
  } else {
    iterator = NewIterator<RefIterator<false>>(
        thd, mem_root, param.table, param.ref, param.use_order,
        path->num_output_rows(), examined_rows);
  }
  break;
}
```

这两个片段说明：

- 如果选中 `TABLE_SCAN` 访问路径，MySQL 会创建面向扫描的 `TableScanIterator`。
- 如果选中正向 `REF` 访问路径，会创建 `RefIterator<false>`；反向访问则使用另一个模板实例。

`REF` 是用于特定索引键值匹配的一类访问方式。我们的 `WHERE name = 'Keyboard'` 在出现多个同名记录且选定相应索引时，是研究这条路径的一个合适案例。

但要留意两个边界。

一是 **`EXPLAIN` 显示的访问类型与具体执行器对象之间不能在任意查询上机械地一一对应**。复杂查询可能叠加过滤、排序、连接等迭代器，范围查找也有自己的访问路径。

二是这里创建的还只是执行器侧的迭代器对象，并没有证明它已经读取到 `Keyboard`。

我们现在得到的结论是：

**优化阶段提出“采用怎样的读取方法”，执行阶段将这个方法变成可调用的读取对象。**

接下来的问题就具体多了：创建好的迭代器，到底由谁调用？

## 5. 谁在不停地要求“下一条”？

打开 [sql/sql_union.cc](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/sql_union.cc)。

不要被文件名 `sql_union.cc` 误导。这里不只有 UNION 的执行代码，也有查询表达式通用的迭代器执行逻辑。

搜索函数：

```cpp
bool Query_expression::ExecuteIteratorQuery(THD *thd)
```

我们先看一段很短的真实源码：

```cpp
if (m_root_iterator->Init()) {
  return true;
}
```

它先初始化根迭代器。

而后面的核心循环是：

```cpp
for (;;) {
  int error = m_root_iterator->Read();

  if (error > 0 || thd->is_error())
    return true;
  else if (error < 0)
    break;
```

这些行就是循环的关键条件。完整函数中间还包括请求中止检查、结果计数和结果发送，下一节会逐项连接。

这里的 `Read()` 有一个非常重要的约定：

- 返回 `0`：本次读到了一条记录，可以继续处理。
- 返回负值：读到结束状态，循环退出。
- 返回正值：出现错误，走错误处理。

可以把这一段缩成我们之前的教学逻辑：

```python
# 教学化简：不是 MySQL 真实源码
iterator.init()
while True:
    status = iterator.read()
    if status == -1:
        break
    if status != 0:
        raise RuntimeError()
    consume_current_row()
```

回忆刚才 Python 实验：三条商品库存记录，四次 `read()` 调用，最后一次是结束标志。

这和真实 MySQL 的**接口协作模式**是对应的。

但是，有一个地方很容易误解：

`Read()` **不是**“返回一个 C++ 行对象”的接口。

它返回状态码，当前记录通常由下层读取后放进已经约定好的记录缓冲区。后续代码再从这些缓冲区所代表的字段中生成结果。

为什么这样设计？

执行器与存储引擎之间不需要为了每行结果都创建一整套独立的对象来完成交接。它们可以使用已有的表记录缓冲区，同时通过返回值表达成功、结束和错误。

我们再回到 `RefIterator` 看实际的读取过程。

## 6. `RefIterator` 第一次怎样找到 Keyboard？

打开 [sql/iterators/ref_row_iterators.cc](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/iterators/ref_row_iterators.cc)。

搜索：

```cpp
bool RefIterator<Reverse>::Init()
```

真实源码有如下关键行：

```cpp
m_first_record_since_init = true;
```

然后在需要时调用：

```cpp
if (init_index(table(), table()->file, m_ref->key, m_use_order)) {
  return true;
}
```

它会对存储引擎的索引读取接口进行初始化。

这里为什么需要一个 `m_first_record_since_init`？

因为第一次查找与后面的连续读取并不相同。

第一次必须依据查询条件定位到正确的索引位置，而后续读取可以沿着当前索引位置继续向前。

我们在 Python 中就是这么做的：

```python
# 教学化简
iterator.init()       # 定位范围
iterator.read()       # 第一条
iterator.read()       # 下一条
```

现在看真实 `RefIterator<false>::Read()`。它包含如下分支：

**真实源码节选：第一次读取时**

```cpp
if (m_first_record_since_init) {
  m_first_record_since_init = false;
  // ...略去空值拒绝等特殊处理...
  if (construct_lookup(thd(), table(), m_ref)) {
    table()->set_no_row();
    return -1;
  }
```

`construct_lookup()` 的主要作用是准备这次索引查找所使用的键值。对于我们的查询，直观上就是把 `Keyboard` 这个等值条件转换成存储引擎需要的查找键表示。

不过，它并不是通用“重新解析 SQL”的函数：SQL 已经在前面被解析和准备，这里处理的是当前访问所需的具体查找值。

紧接着，源码调用：

```cpp
pair<uchar *, key_part_map> key_buff_and_map = FindKeyBufferAndMap(m_ref);
int error = table()->file->ha_index_read_map(
    table()->record[0], key_buff_and_map.first, key_buff_and_map.second,
    HA_READ_KEY_EXACT);
```

这一段是本章最重要的调用之一。

调用者是执行器中的 `RefIterator`。

输入包括：

- `table()->record[0]`：记录缓冲区，读取成功时用于接收当前记录的数据。
- `key_buff_and_map`：构造好的查找键以及对应的键部分信息。
- `HA_READ_KEY_EXACT`：表示本次希望按给定键执行精确匹配查找。

这里的“精确”指的是**匹配给定索引键值**，不代表整个查询必然只返回一行。`name='Keyboard'` 不是唯一条件，因此完全可能匹配多件商品。

这就解释了为什么后面还需要“读下一条”。

我们已经让执行器把 `Keyboard` 交给了存储引擎。但单看这几行，仍无法知道 InnoDB 怎样真正找到索引记录。

为了回答这个问题，接下来只沿着这次函数调用往下走，不另开一条无关主题。

## 7. Server 和 InnoDB 之间究竟怎样交接？

此前我们多次使用“存储引擎接口”这个说法。现在终于能在真实调用链上准确定位这层接口的位置。

打开 [sql/handler.cc](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/handler.cc)。

可以找到：

```cpp
int handler::ha_index_read_map(uchar *buf, const uchar *key,
                                key_part_map keypart_map,
                                enum ha_rkey_function find_flag)
```

这不是 InnoDB 特有的函数名，而是 MySQL Server 面向存储引擎的一层统一接口。

在这个函数内，有这样一条关键语句：

```cpp
result = index_read_map(buf, key, keypart_map, find_flag);
```

再打开 [sql/handler.h](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/handler.h)。

基类默认的 `index_read_map()` 实现为：

```cpp
virtual int index_read_map(uchar *buf, const uchar *key,
                           key_part_map keypart_map,
                           enum ha_rkey_function find_flag) {
  const uint key_len = calculate_key_len(table, active_index, keypart_map);
  return index_read(buf, key, key_len, find_flag);
}
```

因此，对于我们这条普通 InnoDB 索引读取路径，交接关系可以概括为：

```text
RefIterator<false>::Read()
       |
       | 需要按 Keyboard 定位记录
       v
handler::ha_index_read_map()
       |
       | 统一存储引擎调用入口
       v
handler::index_read_map()
       |
       | 根据 keypart_map 计算键长度
       v
ha_innobase::index_read()
```

这是本章研究的正常分支；特殊引擎或特殊优化路径可能有自己的实现，不要把这里的基类默认实现当作所有引擎的唯一可能。

为什么 MySQL 不让 `RefIterator` 直接调用 B+ 树内部函数？

因为 `RefIterator` 的职责是执行**选好的访问方法**，而不是掌握 InnoDB 的页格式、索引游标和历史版本构造细节。

它向下层提出的是“请用这个键给我一条记录”的请求。

至于 InnoDB 使用怎样的页面组织、是否命中 Buffer Pool、读到哪个可见版本，是下层负责的事情。

这样，**执行器的读取策略与存储引擎的具体记录访问实现被隔开了**。

这不是抽象越多越好，而是两层职责确实不同：前者决定按哪个已选路径要数据，后者负责按自己的存储组织交付数据。

现在我们可以进入 InnoDB 对这次请求的实际处理。

## 8. InnoDB 收到键值后怎样取得记录？

打开 [storage/innobase/handler/ha_innodb.cc](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/handler/ha_innodb.cc)。

搜索：

```cpp
int ha_innobase::index_read(...)
```

本章暂时不追踪整个函数的异常分支，只保留支持当前结论的关键动作。

当传入的查找键不为空时，InnoDB 会转换键表示：

```cpp
row_sel_convert_mysql_key_to_innobase(
    m_prebuilt->search_tuple, m_prebuilt->srch_key_val1,
    m_prebuilt->srch_key_val_len, index, key_ptr, key_len);
```

我们终于有了一个明确的跨层数据流：

```text
SQL 条件 name = 'Keyboard'
       |
       v
RefIterator 构建查找键
       |
       v
Handler 接口转发
       |
       v
InnoDB 转换为内部搜索键
```

随后，InnoDB 会根据匹配要求设置搜索模式：

```cpp
if (find_flag == HA_READ_KEY_EXACT) {
  match_mode = ROW_SEL_EXACT;
```

这里的含义并不是“永远只允许返回一条”。它表示搜索结果需要满足对应的精确匹配要求。我们这个非唯一名称条件仍可能找到多条记录。

在普通 InnoDB 表的相关分支中，继续看到：

```cpp
ret = row_search_mvcc(buf, mode, m_prebuilt, match_mode, 0);
```

到了这里，就与前面章节研究过的 InnoDB 记录搜索、索引和 MVCC 机制接起来了。

- 第 05 章解释过，InnoDB 如何使用 B+ 树缩小页面搜索范围。
- 第 06 章解释过，所需页面如何通过 Buffer Pool 提供。
- 第 10 章解释过，一致性读取为什么还需要考虑记录版本的可见性。

本章不需要把上述底层实现重新讲一遍，因为我们现在有了新的核心任务：弄清**执行器与存储引擎怎样在每次读取上协作**。

`row_search_mvcc(...)` 的结果会经 `ha_innobase::index_read()` 转换为存储引擎接口使用的返回码，并通过同一记录缓冲区交还给上层。

因此，看到 `row_search_mvcc()` 返回并不意味着所有 SQL 结果已经发送完成。

到这一步，只是本次读取可能得到了一条记录。

如果还有另一件名称为 `Keyboard` 的商品，执行器又怎样取得它？

## 9. 第二次读取为什么不再从头定位？

回到 `RefIterator<false>::Read()`。

我们刚才看到，第一次读取会执行 `ha_index_read_map(...)`。

当迭代器已经成功读取过一条记录、第二次再调用 `Read()` 时，会进入另一个分支：

```cpp
} else {
  int error = 0;
  do {
    error = table()->file->ha_index_next_same(
        table()->record[0], m_ref->key_buff, m_ref->key_length);
  } while (error == HA_ERR_KEY_NOT_FOUND && m_is_mvi_unique_filter_enabled);
```

这个分支用于继续获取具有相同查找键值的记录。循环里的特殊处理与多值索引相关，本章只关注普通二级索引的正常路径。

现在，为什么不再调用一次 `ha_index_read_map()`？

因为第一次读取时，InnoDB 已经按 `Keyboard` 定位了索引游标。后续读取可以继续沿游标向前，并检查结果是否仍属于匹配范围。

真实 InnoDB 的对应实现是：

**真实源码节选：`ha_innobase::index_next_same()`**

```cpp
return (general_fetch(buf, ROW_SEL_NEXT, m_last_match_mode));
```

`ROW_SEL_NEXT` 表示继续向前获取记录，`m_last_match_mode` 保留先前查找的匹配要求。

这正好对应教学实验中最重要的两种操作：

```text
第一次 Read：按 Keyboard 定位，返回一条
第二次 Read：沿当前位置继续，返回另一条
第三次 Read：继续，返回另一条
第四次 Read：没有更多匹配记录，结束
```

不过，下面两种说法要区分清楚。

“每次 `Read()` 可能让上层得到一条逻辑记录”，是执行器接口层的行为。

“底层每次必定只访问一个物理记录、一个页面，或者一次磁盘 I/O”，**不是**它的保证。InnoDB 可能利用 Buffer Pool、页内游标、预读或其他内部处理；回表、版本检查也会影响实际工作量。

我们这一章研究的是从 SQL 执行器观察到的交付协议，不是对底层 I/O 次数作无根据的断言。

这时，`Read()` 已经把当前记录准备好了。下一步是谁取出 `stock` 并传回客户端？

## 10. 读到记录以后，谁把 `stock` 发送出去？

再次回到 [sql/sql_union.cc](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/sql_union.cc)。

刚才我们只看了 `Query_expression::ExecuteIteratorQuery()` 里的读取循环开头。

现在看成功读取后紧接着发生的动作。源码中有如下关键语句：

```cpp
++*send_records_ptr;

if (query_result->send_data(thd, *fields)) {
  return true;
}
```

`send_records_ptr` 记录发送结果行数量的相关统计。`query_result` 则代表本次查询的结果接收者。

对于普通需要将 SELECT 结果返回客户端的场景，接下来可以看 [sql/query_result.cc](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/query_result.cc) 中的：

```cpp
bool Query_result_send::send_data(
    THD *thd, const mem_root_deque<Item *> &items)
```

其关键实现为：

```cpp
Protocol *protocol = thd->get_protocol();
protocol->start_row();
if (thd->send_result_set_row(items)) {
  protocol->abort_row();
  return true;
}
thd->inc_sent_row_count(1);
return protocol->end_row();
```

这一层又在解决什么新问题？

存储引擎读取到的记录，和客户端收到的一行结果，不是同一种东西。

我们查询的是：

```sql
SELECT stock
FROM products
WHERE name = 'Keyboard';
```

执行器需要取得符合条件的记录，但最终结果只包含一个字段 `stock`。它不会自动把数据库表中所有字段都发给客户端。

`fields` 表示本次查询要输出的表达式列表。对于当前 SQL，它包含所需的库存字段表达式。

`send_result_set_row(items)` 负责按结果列读取并编码当前值，再由 `Protocol` 组织成客户端可接收的一行数据。

所以，`RefIterator` 负责提供当前记录，而结果发送层负责把当前记录中**应该展示的值**转成协议结果行。

两者不是同一个职责。

可以把正常读取与发送过程整合起来：

```text
Query_expression::ExecuteIteratorQuery()
             |
             +-- m_root_iterator->Init()
             |
             +-- m_root_iterator->Read() -----> 记录缓冲区更新
             |                                    |
             +-- query_result->send_data() <-----+
             |           |
             |           v
             |       协议输出 stock
             |
             +-- m_root_iterator->Read() -----> 下一条记录
             |           ...
             +-- Read 返回结束状态
             |
             +-- send_eof()
```

这是一张**聚焦当前正常路径的控制流与结果交付示意图**。真实查询的根迭代器可能还包含其他执行器节点，不应将 `m_root_iterator` 恒等于 `RefIterator`。

## 11. 一个容易忽略的细节：不是迭代器返回了一个“完整结果集”

我们现在可以对比两种接口。

最初，我们写的是：

```python
# 直接生成列表
rows = find_by_name("Keyboard")
```

这个版本把“定位、读取、收集结果”都包进一个函数。

后来，我们拆成：

```python
# 教学化简
iterator.init()
while iterator.read() == 0:
    send_current_row()
```

这个拆分不是纯粹为了代码风格好看。

它为查询执行提供了明确的协作边界：

**底层负责供给记录，上层负责决定如何消费记录。**

只要这个边界稳定，上层以后可以逐步加入更多消费规则，而不需要让每种存储引擎都直接理解所有 SQL 输出形式。

例如，我们将来要研究 JOIN 时，就会发现一个执行节点需要向另一个节点不断请求行；还可能出现过滤、排序和聚合等步骤。

但当前不必讲这些节点的内部实现。只要理解：每个需要上游数据的节点，都可以在相应的执行结构中向下层请求记录。

也不能把这套机制神化为“流式执行总是零内存”——排序、物化、聚合、哈希连接等执行节点可能缓存大量数据。行迭代器只是统一了相邻执行节点之间的主要读取协作方式，并不意味着所有查询都无需物化。

## 12. 回头看看：如果没有索引，会怎样执行？

为了确保我们理解的不是某个函数名字，而是一套可替换的执行方案，现在让查询不能使用索引：

```sql
EXPLAIN FORMAT=TREE
SELECT stock
FROM products IGNORE INDEX (idx_name_stock)
WHERE name = 'Keyboard';
```

对于这张只有主键和 `idx_name_stock` 的简单表，这个实验用来观察表扫描路径。具体计划仍请以你运行的 `EXPLAIN` 为准。

回到之前的 [sql/join_optimizer/access_path.cc](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/join_optimizer/access_path.cc)：

```cpp
case AccessPath::TABLE_SCAN: {
  const auto &param = path->table_scan();
  iterator = NewIterator<TableScanIterator>(
      thd, mem_root, param.table, path->num_output_rows(), examined_rows);
  break;
}
```

现在读取对象换成了 `TableScanIterator`。

打开 [sql/iterators/basic_row_iterators.cc](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/iterators/basic_row_iterators.cc)，可以看到它在初始化时调用：

```cpp
int error = table()->file->ha_rnd_init(true);
```

在正常表扫描读取分支中，调用：

```cpp
while ((tmp = table()->file->ha_rnd_next(m_record))) {
```

这里的读取方式与前面的 `RefIterator` 不同。

- `RefIterator`：依靠索引键定位，后续读取同一个键的匹配记录。
- `TableScanIterator`：沿表的扫描顺序请求下一条记录，上层还需要对相应的 `WHERE` 条件进行处理。

但它们都可以放在执行计划对应的位置上，向上层遵守相似的迭代器读取协议。

对于当前普通 InnoDB 表，所谓表扫描通常仍是在聚簇索引组织的记录上遍历，不是抛开 InnoDB 的索引和页面结构另读一份“无序表文件”。

真正发生变化的是**怎样取得候选记录**，而不是整个结果发送逻辑必须重新发明一次。

这正是执行器与存储引擎之间职责分离的工程收益：

> **上层可以使用不同的访问方法来提供记录，而结果处理过程仍有共同的接口可以调用。**

## 13. MySQL 实验：观察计划与真正执行的区别

前面的 Python 最小实验已实际运行。接下来我们给出可以在 MySQL 8.4 实例上复现的操作。**这些 SQL 没有在本次写作环境中连接真实 MySQL Server 执行，以下结果属于预期观察项。**

### 13.1 先看计划，而不实际运行查询

```sql
USE mysql_source_lab;

EXPLAIN FORMAT=TRADITIONAL
SELECT stock
FROM products FORCE INDEX (idx_name_stock)
WHERE name = 'Keyboard';
```

重点看 `type`、`key` 与 `Extra`。

对于这张小表和给定索引提示，理想的观察目标是 `key = idx_name_stock`，并看到一个可使用索引定位的访问类型（例如 `ref`）。当执行计划确认是 `ref` 时，就可以将它与本章的 `RefIterator` 正常读取路径对应起来。

但必须以实际运行结果为准：即便出现同样的 SQL 文字，版本、统计信息、索引提示和优化器选择也可能影响最终访问路径。

如果 `Extra` 中显示 `Using index`，通常表示本次查询可利用覆盖索引取得需要的列。它**不是**“命中了 Buffer Pool”的意思，也不代表只访问了一棵索引树中的一个页面。

### 13.2 再执行实际查询

```sql
SELECT stock
FROM products FORCE INDEX (idx_name_stock)
WHERE name = 'Keyboard';
```

这里应该得到三个库存值 `30`、`50` 和 `99`（无显式排序时不要求行顺序）。

你还可以进行不依赖输出顺序的核对：

```sql
SELECT COUNT(*) AS matched,
       SUM(stock) AS total_stock
FROM products
WHERE name = 'Keyboard';
```

以本章给出的实验数据为准，预期 `matched = 3`，`total_stock = 179`。

这个实验能够验证查询语义和结果数量，**不能单独证明服务器走过哪一段 C++ 函数**。

### 13.3 把 EXPLAIN 与 EXPLAIN ANALYZE 放在一起

```sql
EXPLAIN FORMAT=TREE
SELECT stock
FROM products FORCE INDEX (idx_name_stock)
WHERE name = 'Keyboard';
```

`EXPLAIN FORMAT=TREE` 用于观察计划形状；它本身不以普通 SELECT 的方式执行并返回业务结果行。

而：

```sql
EXPLAIN ANALYZE
SELECT stock
FROM products FORCE INDEX (idx_name_stock)
WHERE name = 'Keyboard';
```

会实际执行查询并收集执行节点的统计信息，便于观察估算与实际行数、耗时等差异。不要把它当作只做静态计划分析的命令，也不要用其输出代替直接验证业务 SELECT 的结果。

对于这里只有五条记录的表，耗时数值很小，不适合拿来推导现实业务中的性能收益。我们更关心的是：执行计划里实际出现了什么访问节点，节点输出了多少行。

### 13.4 对照全表扫描

```sql
EXPLAIN FORMAT=TREE
SELECT stock
FROM products IGNORE INDEX (idx_name_stock)
WHERE name = 'Keyboard';
```

然后再去掉 `IGNORE INDEX` 比较访问计划。两条 SQL 的目标输出并未变化，但候选记录的获取方式可能不同。

这正是从上一章过渡到本章的核心：**优化器负责决定访问方式，执行器负责运行选定的方式。**

## 14. 重新画出我们已经验证的关键读取链路

现在可以把散落在各个源码文件中的关键函数串起来。

对于 `EXPLAIN` 确认选用了正向 `REF` 访问的普通单表查询，下面是一条值得记住的**正常路径关键节点图**：

```text
                   SELECT stock
              WHERE name='Keyboard'
                           |
                           v
                 优化阶段选出 REF 路径
                           |
                           v
             CreateIteratorFromAccessPath()
                           |
                           v
                 RefIterator<false>
                           |
          +----------------+----------------+
          |                                 |
        Init()                             Read()
          |                                 |
     初始化索引读取                     首次按键定位
                                            |
                                            v
                                handler::ha_index_read_map()
                                            |
                                            v
                                  ha_innobase::index_read()
                                            |
                                            v
                                    row_search_mvcc()
                                            |
                                 当前记录写入记录缓冲区
                                            |
                                            v
                                  query_result->send_data()
                                            |
                                            v
                                       客户端一行
                                            |
                              再次请求 RefIterator::Read()
                                            |
                                            v
                              handler::ha_index_next_same()
                                            |
                                            v
                                 ha_innobase::general_fetch()
                                            |
                                           ...
                                            |
                                     没有更多同名记录
                                            |
                                            v
                                     结束结果集
```

这张图刻意省略了通用接口的一些中间调用、结果列求值、额外过滤以及异常分支。它表示本章已经核对的关键调用关系，**不是对所有 SELECT 都成立的唯一完整调用栈**。

其中最需要记住的是三层职责。

第一层：**访问路径**。

告诉 MySQL 应该通过哪种方法取得记录。

第二层：**行迭代器**。

将访问方法变成一次次可以调用的初始化和读取动作。

第三层：**存储引擎与结果发送**。

向下通过 `handler` 让 InnoDB 返回当前记录，向上从当前记录中取得本次 SELECT 所需列，并交给结果协议发送。

至此，我们能够回答“选好计划之后谁真正读取数据”这个问题，而不再只说一句笼统的“执行器调用存储引擎”。

## 15. 本章的边界：哪些事还没有解释？

本章解释的主要是 **一个普通索引等值读取计划怎样被执行**。

它没有声称所有查询都只需一个 `RefIterator`。实际执行器可能有范围扫描、聚簇主键常量查找、过滤、排序、聚合、连接、物化等多种节点。

它也没有声称每次 `Read()` 都对应一次物理磁盘读取。第 06 章已经说明，页可能被 Buffer Pool 缓存；第 10 章也说明，一致性读取还可能涉及历史版本可见性。执行器抽象并不会替代这些机制。

我们也没有把 `Query_result_send::send_data()` 误写成“把存储引擎的完整物理记录直接发送出去”。输出的是 SQL 查询要求的结果列，不是 InnoDB 页格式。

最后，源码的静态链路可以支持“函数在特定分支下怎样调用”的结论，但要证明某一次具体查询确实调用了 `RefIterator<false>`，应结合该实例的 `EXPLAIN`、调试器或相应运行时证据。

这样的区分很重要：**源码证明机制存在，实验验证行为发生；不能用其中一方代替另一方。**

## 16. 下一章：从一张表到两张表，执行器会遇到什么新问题？

我们已经能够查询商品 `Keyboard` 的库存。

现在业务要把订单和商品放在一起显示：

```sql
SELECT
    o.id,
    p.name,
    o.quantity
FROM orders AS o
JOIN products AS p
    ON o.product_id = p.id;
```

对每条订单，我们都需要找到它对应的商品。

最直观的做法是：读一条订单，再去商品表查一次。

只有十张订单、十件商品时，这个方法完全可以工作。

但如果订单数量增长到一百万条，每个订单都需要单独发起一次商品查找，代价又会怎样？如果两张表都很大，查询优化器该怎样决定先读哪张表、用什么方式把两边的记录连接起来？

好在这一章刚刚建立的行迭代器模型，不需要丢弃。接下来可以让一个迭代器读取订单，再让另一个迭代器按照订单里的 `product_id` 查找商品，并研究这种组合在什么时候有效、什么时候不够。

这就是下一章的自然起点：

**下一章：第 20 章《从单表查询到 JOIN——为什么需要连接算法？》**

---

## 本章源码索引与验证说明

固定版本：**MySQL 8.4.0 / Git Tag `mysql-8.4.0`**。

| 源码位置 | 函数 / 类 | 在本章中解决的问题 |
|---|---|---|
| [`sql/join_optimizer/access_path.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/join_optimizer/access_path.cc#L455) | `CreateIteratorFromAccessPath()` | 将访问路径转换为具体读取迭代器 |
| [`sql/sql_union.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/sql_union.cc#L1562) | `Query_expression::ExecuteIteratorQuery()` | 初始化根迭代器，循环读取，并发送结果 |
| [`sql/iterators/ref_row_iterators.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/iterators/ref_row_iterators.cc#L312) | `RefIterator<Reverse>::Init()` | 准备索引读取状态 |
| [`sql/iterators/ref_row_iterators.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/iterators/ref_row_iterators.cc#L332) | `RefIterator<false>::Read()` | 第一次按键定位、以后连续读取同键记录 |
| [`sql/handler.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/handler.cc#L3028) | `handler::ha_index_read_map()` | Server 到存储引擎的公共读索引入口 |
| [`sql/handler.h`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/handler.h#L5189) | `handler::index_read_map()` | 默认把索引键的部分信息转换为长度，再调用具体引擎读取 |
| [`storage/innobase/handler/ha_innodb.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/handler/ha_innodb.cc#L9640) | `ha_innobase::index_read()` | 准备 InnoDB 搜索键并进入记录搜索 |
| [`storage/innobase/handler/ha_innodb.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/handler/ha_innodb.cc#L10055) | `ha_innobase::index_next_same()` | 沿索引游标继续读取相同查找键的记录 |
| [`sql/query_result.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/query_result.cc#L89) | `Query_result_send::send_data()` | 把当前结果列按客户端协议发送 |
| [`sql/iterators/basic_row_iterators.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/iterators/basic_row_iterators.cc#L246) | `TableScanIterator` | 对照不走索引键定位时的表扫描读取 |

**验证状态：**

- Python 教学实验：**实际执行，全部断言通过**，结果为 `[30, 50, 99]`，并观察到三次成功读取与一次结束读取。
- MySQL 源码：关键类名、函数签名、分支和调用已根据固定的 `mysql-8.4.0` 标签核对。
- MySQL SQL 实验：提供了可复现脚本和预期观察项；**本次未在运行中的 MySQL Server 上执行**，不虚构实际执行计划、耗时与调用记录。

本章的重点不是记住 `RefIterator` 的名字，而是让读者亲自复述：**一条 SQL 选择了索引以后，谁负责初始化读取、谁负责不断索要下一条、谁把键交给 InnoDB、当前记录存在哪里，又是谁把 `stock` 真正送到客户端。**
