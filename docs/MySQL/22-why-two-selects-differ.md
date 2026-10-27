---
title: "22｜隔离级别：为什么同一个事务中两次 SELECT 可能不同？"
sidebar_position: 23
slug: /mysql/22-isolation-levels-and-read-view
description: "从商品 42 的两次库存查询出发，用双会话实验和 MySQL 8.4.0 源码理解 READ COMMITTED、REPEATABLE READ 与 Read View 的创建、复用和版本可见性。"
tags: [MySQL, InnoDB, MVCC, ReadView, 事务隔离, 源码精读]
---

# 隔离级别：为什么同一个事务中两次 SELECT 可能不同？

上一章，我们沿着一条 `SELECT`，已经知道 MySQL 怎样从 SQL 文本走到执行器，再由 InnoDB 读取数据并返回结果。

更早的第 09、10 章，我们还认识了事务、Undo 与 MVCC：一次库存修改尚未提交时，另一个事务不一定要等待它，可以读取某个可见的旧版本。

但是，这里还留着一个问题。

假设用户打开商品详情页面，连续查看两次库存。第一次查到 `100`，第二次查到 `99`。两次查询在同一个事务里，为什么有时会变化，有时却不会？

本章只研究**普通、不加锁的 `SELECT`**。我们要把下面两条看似矛盾的行为解释清楚：

- `READ COMMITTED`：同一个事务的两次普通 `SELECT`，可以分别看到 `100` 和 `99`。
- `REPEATABLE READ`：同一个事务的两次普通 `SELECT`，可以一直看到 `100`，即使另一个事务已经把库存提交为 `99`。

我们不会从隔离级别定义表开始背诵，而是先让这个现象发生，再逐步寻找缺少的机制。

## 1. 两次完全相同的 SELECT，为什么可能返回不同库存？

仍然使用贯穿全系列的商品表：

```sql
CREATE TABLE products (
    id BIGINT PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    stock INT NOT NULL
) ENGINE = InnoDB;
```

设商品 `42` 当前库存为 `100`。

打开两个数据库连接：**会话 A** 和 **会话 B**。

会话 A 想持续查看库存；会话 B 负责完成一次购买。

我们先设想最简单的执行顺序：

```text
时间向下

会话 A                              会话 B
  |                                   |
  |  SELECT stock ...                 |
  |  得到 100                         |
  |                                   |
  |                          UPDATE stock = stock - 1
  |                          COMMIT   (新库存 99)
  |                                   |
  |  SELECT stock ...                 |
  |  应该得到什么？                    |
  |                                   |
```

如果我们只考虑“数据库里目前存放的最新值”，很容易回答 `99`。

这也是一个最简单的设计：**每次查询都读取最新已提交的数据**。

这个方案看起来很合理。会话 B 已经成功买走一件商品，会话 A 再查询时看到最新库存，似乎是理所当然的。

但这里有一个尚未显露的需求。

假设会话 A 并不是偶尔看两眼库存，而是在一次较长的业务操作中，需要先后读取多个商品、核对数据、生成一份一致的结果。

如果每次读取都允许看到其他事务刚刚提交的新状态，那么同一个事务中的不同查询可能来自不同时间点。

有时候，我们希望数据足够新；有时候，我们希望一组读取遵循同一份可见状态。

**问题不是“能不能读到 99”，而是“当前事务应该按哪个时间点决定哪些版本可见”。**

这时，我们才有必要引入隔离级别对一致性读取的不同约束。

## 2. 先真实观察：READ COMMITTED 下的两次读取

为了让结果稳定、可复现，我们不用“同时运行然后碰运气”的方式，而是明确安排两个会话的执行顺序。

实验均假设使用 **MySQL 8.4、InnoDB、独立的 `mysql_source_lab` 实验库**。不要在生产库执行重置库存的语句。

先在独立的准备连接中初始化：

```sql
CREATE DATABASE IF NOT EXISTS mysql_source_lab;
USE mysql_source_lab;

CREATE TABLE IF NOT EXISTS products (
    id BIGINT PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    stock INT NOT NULL
) ENGINE = InnoDB;

INSERT INTO products (id, name, stock)
VALUES (42, 'Keyboard', 100)
ON DUPLICATE KEY UPDATE
    name = 'Keyboard',
    stock = 100;
```

我们先给会话 A 设置隔离级别：

```sql title="会话 A"
USE mysql_source_lab;
SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED;
SELECT @@transaction_isolation;
START TRANSACTION;

SELECT stock FROM products WHERE id = 42;
-- 预期：100
```

这里必须使用一个**明确的事务**。如果每条查询都自动提交，它们自然属于不同事务，无法用来比较“同一事务里两次 SELECT”的行为。

接下来，会话 A 暂时不提交。

在会话 B 中执行一次购买：

```sql title="会话 B"
USE mysql_source_lab;
START TRANSACTION;

UPDATE products
SET stock = stock - 1
WHERE id = 42 AND stock > 0;

COMMIT;
```

会话 B 已经提交，最新库存成为 `99`。

回到**仍然没有结束事务**的会话 A：

```sql title="会话 A（继续）"
SELECT stock FROM products WHERE id = 42;
-- 预期：99

COMMIT;
```

观察结果是：

| 会话 A 的查询 | 会话 B 是否已经提交 | 预期库存 |
|---|---|---:|
| 第一次 `SELECT` | 否 | 100 |
| 第二次 `SELECT` | 是 | 99 |

这就是 `READ COMMITTED` 在该场景下的表现。

同一事务中的两次**一致性读取**，可以各自建立新的可见性快照。

会话 B 在第二次查询开始之前提交，因此会话 A 的第二次查询能够看到它的结果。

这个行为带来的直接代价也已经出现：**同一个事务重复读取同一行，可能得到不同的值。**

但是，如果业务希望一个事务内的普通查询保持一致，该怎么办？

## 3. 只改变一项规则：REPEATABLE READ

现在重新开始同样的实验。

首先在两个会话都没有未完成事务时，由准备连接把库存恢复到 `100`：

```sql
UPDATE mysql_source_lab.products
SET stock = 100
WHERE id = 42;
```

会话 A 唯一改变的条件，是隔离级别：

```sql title="会话 A"
USE mysql_source_lab;
SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ;
SELECT @@transaction_isolation;
START TRANSACTION;

SELECT stock FROM products WHERE id = 42;
-- 预期：100
```

注意，会话 A 的第一次普通 `SELECT` 已经执行。它将在这个事务中建立一致性读取所使用的快照。

会话 B 再次购买商品并提交：

```sql title="会话 B"
USE mysql_source_lab;
START TRANSACTION;

UPDATE products
SET stock = stock - 1
WHERE id = 42 AND stock > 0;

COMMIT;
```

回到会话 A：

```sql title="会话 A（继续）"
SELECT stock FROM products WHERE id = 42;
-- 预期：100

COMMIT;

SELECT stock FROM products WHERE id = 42;
-- 预期：99（此时属于新的事务）
```

第二次 `SELECT` 为什么仍然看到 `100`？

因为 `REPEATABLE READ` 下，**同一事务中的普通一致性读取复用第一次一致性读取建立的快照**。

会话 B 对库存的修改已经提交，最新记录确实变成了 `99`；但会话 A 的既有快照不认为这个新版本可见。

提交会话 A 的事务之后，下一次查询进入新的事务，才能按新的时间点读取 `99`。

现在我们有了鲜明的对照：

| 条件相同 | READ COMMITTED | REPEATABLE READ |
|---|---|---|
| A 第一次普通 SELECT | 100 | 100 |
| B 更新并提交 | 99 | 99 |
| A 第二次普通 SELECT | **99** | **100** |
| A 提交后重新查询 | 99 | 99 |

我们真正需要回答的问题变得具体了：

**InnoDB 怎样保存“第一次查询的可见性规则”？为什么 RC 会更新它，而 RR 不会？**

## 4. 最小教学模型：查询不只是读取最新版本

先不看 MySQL 的复杂数据结构。

从第 10 章我们已经知道，数据库可以保留旧版本，使读取不必只能访问当前值。

我们构造一个简化的版本历史：

```text
版本 V1：stock = 100，提交序号 0
版本 V2：stock =  99，提交序号 1
```

现在约定：读者持有一个“可见截至哪个提交序号”的数字。

如果读者的快照是 `0`，就只能读取 V1；如果快照是 `1`，就能够读取 V2。

这不是 InnoDB Read View 的真实字段设计，仅用于观察“固定快照”和“每次刷新快照”的差别。

### 实验：一个可运行的简化读取器

**教学化简代码（非 MySQL 源码）：**

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class Version:
    value: int
    committed_at: int

class Database:
    def __init__(self):
        self.clock = 0
        self.versions = [Version(100, self.clock)]

    def commit_purchase(self):
        self.clock += 1
        self.versions.append(Version(99, self.clock))

    def read_as_of(self, snapshot):
        return next(
            v.value for v in reversed(self.versions)
            if v.committed_at <= snapshot
        )

class Reader:
    def __init__(self, db, isolation):
        self.db = db
        self.isolation = isolation
        self.snapshot = None

    def select_stock(self):
        if self.isolation == 'READ COMMITTED' or self.snapshot is None:
            self.snapshot = self.db.clock
        return self.db.read_as_of(self.snapshot)

for level in ('REPEATABLE READ', 'READ COMMITTED'):
    db = Database()
    session_a = Reader(db, level)
    first = session_a.select_stock()
    db.commit_purchase()
    second = session_a.select_stock()
    print(f'{level}: first={first}, second={second}, '
          f'committed={db.versions[-1].value}')
    assert (first, second) == (
        (100, 100) if level == 'REPEATABLE READ' else (100, 99)
    )

print('All assertions passed')
```

**已在 Python 中实际运行，输出为：**

```text
REPEATABLE READ: first=100, second=100, committed=99
READ COMMITTED: first=100, second=99, committed=99
All assertions passed
```

这里最值得观察的是：两个读取器使用了**完全相同的版本历史**，不同的只是快照何时更新。

`READ COMMITTED` 在每次读取时使用最新已提交进度。

`REPEATABLE READ` 在第一次读取时记住进度，后续继续使用它。

但是，这个模型仍然过于理想化。

真实 MySQL 中，事务可以在另一个事务尚未提交时启动，也可能存在多个同时活跃的事务。我们不能简单地用“最新提交序号”准确判断所有记录版本是否可见。

例如，创建快照时：

- 事务 B 已经修改库存，但**尚未提交**；
- 事务 C 在快照创建之前**已经提交**；
- 之后 B 才提交。

同一个快照应该看见 C 的修改，却不能因为 B 后来提交了，就让 B 的改动突然变得可见。

因此，仅仅记住一个时间点还不够直观。系统需要知道：**创建这个视图时，哪些修改事务仍然活跃？**

这正是 MySQL `ReadView` 要处理的问题。

---

## 5. 真实源码：第一次普通 SELECT 何时获取 Read View？

从这里开始，统一使用以下源码版本：

- 项目：MySQL Server Community
- 标签：[`mysql-8.4.0`](https://github.com/mysql/mysql-server/tree/mysql-8.4.0)
- 本章聚焦：InnoDB 的普通非锁定一致性读取

第 19、21 章我们已经知道，MySQL 执行器可以通过 handler 接口驱动 InnoDB 读取记录。

现在沿这个路径继续进入：

[`storage/innobase/row/row0sel.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/row/row0sel.cc)

在 `row_search_mvcc()` 中，当准备进行一条新的读取语句时，会区分普通一致性读取和加锁读取。

**真实源码节选（省略了相邻分支）：**

```cpp
} else if (prebuilt->select_lock_type == LOCK_NONE) {
  /* This is a consistent read */
  /* Assign a read view for the query */
  if (!srv_read_only_mode) {
    trx_assign_read_view(trx);
  }
```

当前位置对应源码约 [L4470–L4475](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/row/row0sel.cc#L4470-L4475)。

这几行带来一个具体结论：在我们研究的普通 `SELECT` 路径中，InnoDB 会向事务取得一致性读取视图。

函数名是：

```cpp
trx_assign_read_view(trx)
```

但这里并不能立刻断言“每次 SELECT 都创建新的 Read View”。

它只说明：读取路径会调用**取得视图**的函数。到底新建还是复用，需要继续追踪。

## 6. `trx_assign_read_view()`：为什么 RR 可以复用视图？

打开：

[`storage/innobase/trx/trx0trx.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/trx/trx0trx.cc)

找到 `trx_assign_read_view()`。

**真实源码节选：**

```cpp
} else if (!MVCC::is_view_active(trx->read_view)) {
  trx_sys->mvcc->view_open(trx->read_view, trx);
}

return (trx->read_view);
```

对应源码约 [L2139–L2151](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/trx/trx0trx.cc#L2139-L2151)。

这段代码恰好解决了我们刚才的问题。

它先判断：当前事务是否已经拥有一个活跃的读取视图？

如果没有，就通过 `view_open()` 打开视图。

如果已经有活跃视图，就直接返回。

这意味着，`trx_assign_read_view()` 不是每次调用都无条件创建一个全新的快照。

而是：**同一个事务中的多次一致性读取，可以复用已有的活跃 Read View。**

这能解释 RR 的一半行为。

会话 A 第一次读取库存时，得到一个视图。

会话 B 提交库存修改后，会话 A 再次读取，仍然可能使用之前的视图，因此第二次结果仍然是 `100`。

但一个新问题立刻出现：

**既然这个函数会复用已存在的活跃视图，READ COMMITTED 又凭什么每条语句都能读到更新的数据？**

答案不在 `trx_assign_read_view()` 自己内部。

我们需要去看另一段负责语句边界处理的源码。

## 7. `READ COMMITTED` 怎样让下一条语句得到新视图？

打开：

[`storage/innobase/handler/ha_innodb.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/handler/ha_innodb.cc)

这里有 `ha_innobase::external_lock()`，MySQL Server 会在操作表时通过该接口与存储引擎协调语句相关的状态。

在语句结束、表使用计数归零的处理分支中，可以找到：

**真实源码节选（省略锁和其他事务处理）：**

```cpp
} else if (trx->isolation_level <= TRX_ISO_READ_COMMITTED &&
           MVCC::is_view_active(trx->read_view)) {
  mutex_enter(&trx_sys->mutex);
  trx_sys->mvcc->view_close(trx->read_view, true);
  mutex_exit(&trx_sys->mutex);
}
```

对应约 [L17668–L17691](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/handler/ha_innodb.cc#L17668-L17691)。

这是理解 RC 与 RR 差异的关键证据。

对于 `READ COMMITTED` 等较低隔离级别，InnoDB 会在相应语句结束时关闭活跃的读取视图。

因此下一条普通一致性读取到来时，`trx_assign_read_view()` 发现没有活跃视图，就会重新打开一个。

而 `REPEATABLE READ` 的显式事务不会因为一次普通 SELECT 结束，就按这条条件关闭视图。它可以在后续查询中复用原来的视图，直到事务结束等生命周期事件发生。

此外，在同文件 `ha_innobase::store_lock()` 中，也可以找到新语句处理时对较低隔离级别活跃视图的关闭逻辑，注释说明其目标正是让每次一致性读取建立自己的快照。参见 [L18226–L18243](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/handler/ha_innodb.cc#L18226-L18243)。

现在我们已经能用真实代码解释差异：

```text
READ COMMITTED

第一次 SELECT
  → 获取 Read View A
  → 读取 stock = 100
  → 本条语句结束，关闭视图 A

会话 B 修改并提交 stock = 99

第二次 SELECT
  → 获取新的 Read View B
  → 读取 stock = 99
```

```text
REPEATABLE READ（同一显式事务）

第一次 SELECT
  → 获取 Read View A
  → 读取 stock = 100

会话 B 修改并提交 stock = 99

第二次 SELECT
  → 复用 Read View A
  → 仍读取 stock = 100
```

注意，这里说的是**按语句建立或复用一致性读取视图**，不是遍历一百万条记录时每读取一行就重新建立一个视图。否则单条 SQL 自身的读取结果也无法保持一致。

但我们还有一层没有解释：一个 Read View 到底根据什么判断 `99` 是否可见？

## 8. Read View 怎样判断某个版本能不能看见？

现在回到最开始的商品 42。

假设某个时刻它存在两份可用于构造的记录版本：

```text
较新版本：stock = 99，来自事务 B
       |
       | 版本历史
       v
较旧版本：stock = 100
```

如果会话 A 的快照创建时，事务 B 仍然活跃，那么即使 B 随后提交，会话 A 原有的 RR 视图也不应该突然接受这个版本。

我们需要的是一组能够稳定判断事务可见性的规则。

打开：

[`storage/innobase/include/read0types.h`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/include/read0types.h)

其中的 `ReadView` 类包含 `changes_visible()`。

**真实源码节选（保留关键分支）：**

```cpp
if (id < m_up_limit_id || id == m_creator_trx_id) {
  return (true);
}

if (id >= m_low_limit_id) {
  return (false);
}
```

完整判断还会检查在创建视图时仍活跃的事务 ID 集合。参见 [L146–L169](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/include/read0types.h#L146-L169)。

为避免一次引入过多字段，先理解三个实际作用：

1. 一部分足够早的事务修改，可以直接判定为可见。
2. 一部分足够晚的事务修改，可以直接判定为不可见。
3. 对中间范围的事务，需要进一步判断它是否属于创建视图时尚未完成的事务集合。

还有一个例外：**当前事务自己的修改应该对自己可见**。源码中的 `m_creator_trx_id` 就参与这种判断；实际运行还涉及读操作与事务自身写入的协调。

这比我们 Python 实验中的简单 `committed_at` 数字更加精确。

因为在真正的并发数据库里，事务 ID 的先后、事务开始的时间和事务提交的时间不能简单混为一谈。

那么，这些判断范围和活跃事务集合从哪里来？

继续打开：

[`storage/innobase/read/read0read.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/read/read0read.cc)

`ReadView::prepare()` 负责准备视图内容。它会取得有关事务的边界信息，并拷贝当前相关的活跃读写事务 ID：

**真实源码节选：**

```cpp
m_low_limit_id = trx_sys_get_next_trx_id_or_no();

if (!trx_sys->rw_trx_ids.empty()) {
  copy_trx_ids(trx_sys->rw_trx_ids);
}
```

参见 [`ReadView::prepare()`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/read/read0read.cc#L416-L439)。

这里的重点不是某个成员变量的名称，而是视图必须保存**创建时的事务状态信息**。

现在我们终于可以解释：

**Read View 不是简单保存一份商品表副本，而是保存判断记录版本是否可见的规则。**

因此，即使商品最新版本已经变成 `99`，会话 A 仍然可以根据自己的 Read View 判断：这个版本不属于当前一致性读取可以看到的范围。

问题又向下延伸了一层。

既然新版本不可见，MySQL 怎么拿到之前的 `100`？

## 9. 新版本不可见，旧库存从哪里来？

之前我们研究 Undo 时，已经知道数据库为事务回滚保存必要的旧版本信息。

在 MVCC 中，这些版本信息还能够帮助一致性读取取得需要的历史记录。

但数据库不会先把所有历史版本全部展开，再交给 SQL 执行器逐个选择。

在 InnoDB 的记录读取过程中，会先检查当前记录的可见性。

先看：

[`storage/innobase/lock/lock0lock.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/lock/lock0lock.cc)

这里的 `lock_clust_rec_cons_read_sees()` 会取得聚簇索引记录中的事务 ID，然后询问 Read View：

**真实源码节选：**

```cpp
trx_id_t trx_id = row_get_rec_trx_id(rec, index, offsets);
return (view->changes_visible(trx_id, index->table->name));
```

参见 [L206–L233](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/lock/lock0lock.cc#L206-L233)。

函数返回 `true`，表示当前版本符合该视图的可见性要求。

如果返回 `false`，不等于整条查询失败，而是可能需要回到旧版本。

在 [`row_search_mvcc()`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/row/row0sel.cc#L4914-L4946) 的聚簇索引一致性读取路径中，可以看到相应处理：

**真实源码节选（仅保留触发旧版本构造的部分）：**

```cpp
if (srv_force_recovery < 5 &&
    !lock_clust_rec_cons_read_sees(rec, index, offsets,
                                   trx_get_read_view(trx))) {
  rec_t *old_vers;
```

紧接着，该分支会调用 `row_sel_build_prev_vers_for_mysql()` 来取得需要的旧版本。

而这个函数最终会进入版本构造逻辑：

[`row_vers_build_for_consistent_read()`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/row/row0vers.cc#L1128-L1220)

它会沿着记录的历史版本尝试向前构造，直到得到当前 Read View 可以看见的版本，或者判断记录在视图里还不存在。

现在，同一个查询的核心行为终于连接起来了：

```text
读取商品 42 的当前记录
          |
          v
  检查记录所属事务 ID
          |
          v
 ReadView::changes_visible()
          |
      +---+---+
      |       |
    可见     不可见
      |       |
      |       v
      |   沿版本历史寻找
      |   可见的旧记录
      |       |
      +---+---+
          |
          v
  把可见的 stock 返回
```

这张图描述的是**普通一致性读取的版本选择**，不是记录加锁的完整执行图。

读到这里，我们已经能够把前两章的知识连接起来：

- Undo 为历史版本的构造提供必要信息。
- Read View 提供判断版本可见性的规则。
- `row_search_mvcc()` 在读取记录时决定是否需要向旧版本回溯。

不同隔离级别改变的关键之一，是同一事务内 Read View 的建立与复用时机。

---

## 10. 一个容易误解的细节：快照一定在 START TRANSACTION 时生成吗？

不是。

对于默认的 `REPEATABLE READ`，普通情况下，快照由事务内**第一次一致性读取**建立，而不是一执行 `START TRANSACTION` 就必然固定。

这会产生一个可以直接验证的反例。

首先将库存恢复为 `100`，两个会话都确保没有未结束的事务。

会话 A：

```sql title="会话 A"
SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ;
START TRANSACTION;
-- 此处还不执行 SELECT
```

现在让会话 B 修改并提交：

```sql title="会话 B"
UPDATE mysql_source_lab.products
SET stock = stock - 1
WHERE id = 42 AND stock > 0;
-- 假定本会话 autocommit = 1；新库存提交为 99
```

然后会话 A 才执行第一次普通 `SELECT`：

```sql title="会话 A（继续）"
SELECT stock
FROM mysql_source_lab.products
WHERE id = 42;
-- 预期：99

COMMIT;
```

为什么这次是 `99`？

因为会话 A 虽然先开始了事务，却是在会话 B 提交以后才建立一致性读取的视图。

如果希望事务开始时就建立一致性读取快照，MySQL 还支持：

```sql
START TRANSACTION WITH CONSISTENT SNAPSHOT;
```

它与普通 `START TRANSACTION` 的区别，必须结合具体隔离级别和语义理解。此处不展开其他隔离级别下的特殊情况。

这个反例非常重要，因为它纠正了一个流行但不准确的说法：

> “RR 总能看到事务开始那一刻的数据。”

更准确的表述是：**对于这里讨论的普通一致性读取，RR 在事务内通常复用第一次一致性读取建立的视图。**

## 11. 另一个边界：普通 SELECT 与 SELECT FOR UPDATE 不是同一种读取

前面的实验一直使用：

```sql
SELECT stock FROM products WHERE id = 42;
```

它在我们讨论的 RC、RR 场景下属于普通一致性读取。

但第 08 章已经介绍过，修改库存需要协调并发冲突。

如果改为：

```sql
SELECT stock FROM products WHERE id = 42 FOR UPDATE;
```

它属于**加锁读取**，不能直接套用前面“两次普通 SELECT 使用同一个 Read View”的结论。

我们可以构造这样的顺序：

1. 会话 A 在 RR 事务中普通 `SELECT`，看到 `100`，建立旧视图。
2. 会话 B 扣减库存并提交，最新库存变成 `99`。
3. 会话 A 再使用 `SELECT ... FOR UPDATE`，在没有其他冲突锁阻挡时，读取到最新可加锁的记录值 `99`。
4. 如果 A 随后再做普通一致性 `SELECT`，而且 A 没有修改这条记录，仍可能根据旧视图看到 `100`。

这不是说同一条记录可以随意出现两个互相矛盾的值。

而是两类 SQL **选择记录版本的规则不同**。

尤其需要注意：当前事务自己修改过的记录通常对自己可见。因此，如果 A 在步骤 3 之后又实际修改了库存，后续普通 SELECT 的现象还会受到“读自己的写入”规则影响，不能机械地认为总能返回 `100`。

同样，`UPDATE` 和 `DELETE` 也不等同于普通的一致性 SELECT。

**MVCC 不会替代写操作所需要的冲突协调，也不意味着所有读取都不加锁。**

下一章研究范围锁时，我们会再次碰到这种区别。

## 12. 回到源码：我们到底证明了什么？

现在可以把一个很小、但已经有确切证据的执行关系拼出来：

```text
普通 SELECT 开始读取记录
             |
             v
       row_search_mvcc()
             |
             v
      trx_assign_read_view()
             |
             v
      当前事务已有活跃视图？
           /       \
         是         否
         |          |
       复用       view_open()
           \       /
             v
    得到事务的 Read View
             |
             v
 检查当前记录版本是否可见
             |
        +----+----+
        |         |
       可见      不可见
        |         |
      直接读    沿 Undo 构造
                可见旧版本
```

但这张图还需要补上一条**发生在语句边界的路径**：

```text
一条 SQL 结束
      |
      v
ha_innobase::external_lock()
      |
      +-- READ COMMITTED：按条件关闭活跃视图
      |
      +-- REPEATABLE READ：显式事务内保留既有视图
```

这里把读行路径和语句生命周期分开画，是为了避免将“关闭 Read View”误解成逐行读取时的一部分。

从源码看，本章的证据链是：

| 核心问题 | 真实源码位置 | 作用 |
|---|---|---|
| 普通 SELECT 从哪里取得视图？ | `row0sel.cc`：`row_search_mvcc()` | 在一致性读取路径调用 `trx_assign_read_view()` |
| 视图什么时候新建、什么时候复用？ | `trx0trx.cc`：`trx_assign_read_view()` | 活跃视图存在时复用，否则打开 |
| RC 为什么下一条 SELECT 可以使用新视图？ | `ha_innodb.cc`：`external_lock()`、`store_lock()` | 在对应语句边界关闭旧视图 |
| Read View 根据什么判断可见？ | `read0types.h`：`ReadView::changes_visible()` | 检查事务 ID 与视图记录的边界和活跃集合 |
| 判断不可见之后怎么办？ | `row0sel.cc`、`row0vers.cc` | 按历史版本构造可见记录 |

这已经足够解释最初的两个结果：

`READ COMMITTED`：`100 → 99`。

`REPEATABLE READ`：`100 → 100`（事务结束后的新查询再看到 `99`）。

没有必要为了这一结论继续深入 `ReadView` 的每一个成员，或者提前展开所有事务锁类型。

## 13. 本章实验验证到什么程度？

本章的 Python 多版本教学模型**已实际运行**，并通过了 RC 与 RR 两组断言。

MySQL 的双会话 SQL 是基于 MySQL 8.4 官方行为定义与对应源码编写的**可复现步骤**；本章未连接实际 MySQL 8.4.0 服务器，因此没有把表中的预期值写成已经采集的数据库运行输出。

如果自行复现实验，请特别检查以下条件：

- **一定使用两个独立连接。** 在同一连接中切换执行两方事务，不会复现真正的并发读取关系。
- **隔离级别在事务开始前设置。** 不要在 A 已经开始事务后试图改变当前事务的隔离级别。
- **A 的事务不能提前结束。** 一旦 A 执行 `COMMIT`，再读到 `99` 就不属于“同一事务两次 SELECT”的比较。
- **让 B 在 A 的两次 SELECT 之间完成提交。** 如果 B 未提交，RC 的第二次查询也不应该读取它尚未提交的修改。
- **只比较普通 SELECT。** 使用 `FOR UPDATE` 或让 A 自己修改记录，会改变观察条件。
- **每组实验前恢复库存。** RC 实验和 RR 实验都从 `100` 开始，避免上次实验遗留结果。

实验完成后，在每个连接中显式 `COMMIT` 或 `ROLLBACK`，不要留下长期未结束的事务。

## 14. 本章总结：隔离级别改变了什么？

我们从一条最简单的 SQL 开始：

```sql
SELECT stock FROM products WHERE id = 42;
```

当会话 B 在两次查询之间将库存从 `100` 修改为 `99` 并提交，我们观察到两个不同的读取结果。

最开始，只考虑“读取当前最新数据”的模型，无法解释 RR 为什么依然返回 `100`。

于是，我们引入按视图判断版本可见性的需求。

通过简化实验，先理解了“每次查询重新取快照”和“事务内复用快照”的区别。

随后，我们在 MySQL 8.4.0 的源码里确认：

1. 普通一致性读取会请求事务的 Read View。
2. 已有活跃视图可以复用。
3. 较低隔离级别会在语句边界关闭视图，从而让后续查询取得新视图。
4. Read View 会依据创建时的事务状态判断记录版本是否可见。
5. 如果当前记录不可见，InnoDB 可以沿历史版本找到应该返回的记录。

于是，最初看起来矛盾的行为变得可以解释：

**同一事务的两次普通 SELECT，结果是否变化，关键取决于第二次读取使用的是新的可见性视图，还是之前的视图。**

不过，我们一直只查询一个确定的主键。

它很容易定位，也不会涉及“查询范围中后来出现了新记录”的问题。

如果业务要按库存范围挑选商品，并且希望在操作期间防止其他事务向这个范围插入符合条件的新记录，该怎么办？

仅仅讨论一条记录的可见性，还不足以解释这种需要保护查询范围的加锁操作。

这会引出下一章的新问题：

**既然记录锁只能保护已有记录，MySQL 在什么场景下需要连记录之间的空隙一起保护？**

**下一章：第 23 章《从记录锁到间隙锁——为什么锁住记录仍然不够？》**

---

## 本章源码与官方参考

**固定源码版本：MySQL 8.4.0（Git Tag `mysql-8.4.0`）**。

- [`row_search_mvcc()`：取得 Read View 与按版本读取](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/row/row0sel.cc#L4457-L4478)
- [`trx_assign_read_view()`：返回或打开事务视图](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/trx/trx0trx.cc#L2135-L2152)
- [`ha_innobase::external_lock()`：语句结束与视图关闭](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/handler/ha_innodb.cc#L17661-L17691)
- [`ha_innobase::store_lock()`：低隔离级别的视图处理](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/handler/ha_innodb.cc#L18226-L18243)
- [`ReadView::changes_visible()`：可见性判断](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/include/read0types.h#L146-L169)
- [`ReadView::prepare()`：准备事务视图](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/read/read0read.cc#L412-L439)
- [`lock_clust_rec_cons_read_sees()`：检查聚簇索引记录](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/lock/lock0lock.cc#L206-L233)
- [`row_vers_build_for_consistent_read()`：构造可见旧版本](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/row/row0vers.cc#L1128-L1220)
- [MySQL 8.4 官方手册：事务隔离级别](https://dev.mysql.com/doc/refman/8.4/en/innodb-transaction-isolation-levels.html)
- [MySQL 8.4 官方手册：一致性非锁定读取](https://dev.mysql.com/doc/refman/8.4/en/innodb-consistent-read.html)

本章刻意不展开全部隔离级别、锁范围、串行化规则与 DDL 兼容性。这些内容只有在新的具体问题出现时，才需要继续引入。
