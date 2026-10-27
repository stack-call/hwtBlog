---
title: "23｜从记录锁到间隙锁：为什么锁住记录仍然不够？"
sidebar_position: 24
slug: /mysql/23-gap-and-next-key-locks
description: 从商品库存范围查询的空结果出发，沿 MySQL 8.4.0 的真实源码理解 Gap Lock、Next-Key Lock 与插入意向锁。
tags: [MySQL, InnoDB, 源码精读, Gap Lock, Next-Key Lock]
---

# 从记录锁到间隙锁：为什么锁住记录仍然不够？

上一章，我们已经解释了同一个事务里连续两次普通 `SELECT` 为什么可能得到不同的结果。关键在于：**一致性读取关心一个版本对当前事务是否可见**。在 `REPEATABLE READ` 下，普通 `SELECT` 通常通过 Read View 维持一致的读取视图。

但我们的购买系统还有另一种业务要求。

假设系统准备处理一批商品：先找出 ID 处于某个区间的商品，在当前事务中检查这个范围，再决定后续操作。检查期间，我们不希望其他事务突然往这个范围里插入新商品。

这不是简单的“读到哪个历史版本”的问题。我们要限制的是**另一个事务接下来能不能改变这个索引范围的成员**。

因此，这一章只围绕一个问题展开：

> 如果范围里暂时没有任何记录，锁住已有记录的办法，怎样阻止其他事务往范围中插入新记录？

我们继续沿用 `products` 商品表，但把商品 ID 改为 `10、20、30`，让“记录之间的空隙”能够直接被观察。

## 1. 先用我们已经学过的记录锁

在第 08 章里，我们让两个用户同时购买商品 `42`。当两个事务都要修改同一条记录时，InnoDB 可以通过记录锁协调冲突操作。

例如：

```sql
SELECT * FROM products WHERE id = 10 FOR UPDATE;
```

当事务按唯一主键等值查找一个**确实存在**的记录时，InnoDB 通常只需要锁住该索引记录本身。

这很符合直觉：既然目标就是商品 `10`，保护这条记录就够了。

现在先看索引中的三个 ID：

```text
主键索引顺序：

10                20                30
●-----------------●-----------------●
```

事务 A 锁住 `10`，事务 B 想插入 `15`。如果 B 不修改 `10`，仅仅在它后面插入一个新的 ID，两者并不需要因那条记录锁发生冲突。

```text
事务 A：锁住记录 10
事务 B：插入记录 15

10       15       20                30
●--------●--------●-----------------●
```

所以，“锁住一条已存在的记录”和“禁止这个范围出现新记录”根本不是同一个要求。

**当前简单方案的限制已经出现：记录锁能够保护已有记录，但不能自动保护记录之间尚未被占用的位置。**

我们先不急着介绍新的锁名称。来构造一个更极端的例子。

## 2. 返回零条记录的查询，为什么仍可能需要锁？

假设数据库里只有下面几条商品：

```text
id       stock
10       10
20       10
30       10
```

事务 A 要检查：是否存在 ID 严格处于 `10` 和 `20` 之间的商品？

```sql
SELECT id
FROM products
WHERE id > 10 AND id < 20;
```

结果为空。

如果我们只考虑“给查出来的行加锁”，这次查询返回零条记录，似乎就没有东西可以锁。

但如果事务 A 的业务要求是“检查期间不允许这个区间新增商品”，此时事务 B 执行：

```sql
INSERT INTO products (id, name, stock)
VALUES (15, 'Keyboard', 10);
```

它就可能改变事务 A 刚刚检查过的区间。

```text
事务 A 检查时：

10                 20
●------------------●
      没有记录

事务 B 插入后：

10        15       20
●---------●--------●
          新记录
```

这带来了一个比“锁住结果行”更强的需求：

> 对于会影响后续操作的范围检查，我们需要限制别的事务向特定索引区间插入新记录，即使该区间当前没有任何记录。

注意，这是**加锁范围读取**需要解决的问题，而不是说每一条普通 `SELECT` 都应该阻止插入。

我们将这个新能力称为 **Gap Lock（间隙锁）**。

它保护的不是现有商品记录本身，而是索引记录之间的空隙。

不过，在进入真实 MySQL 之前，先用一个足够小的模型验证我们为什么需要这种区别。

## 3. 最小实验：现有记录锁不等于间隙保护

下面是一个故意简化的索引模型，只模拟“某个事务是否保留了区间，以及其他事务能否插入”。它**不是**完整的 InnoDB 锁管理器，也不模拟 B+ 树页、锁队列、死锁检测或真实事务调度。

```python
from bisect import insort


class ToyInventoryIndex:
    """教学模型：只演示保护空隙与阻止插入。"""

    def __init__(self, keys):
        self.keys = sorted(keys)
        self.record_locks = {}  # key -> transaction
        self.guarded_gaps = {}  # (left, right) -> transaction

    def lock_record(self, tx, key):
        assert key in self.keys
        self.record_locks[key] = tx

    def protect_empty_range(self, tx, left, right):
        assert not any(left < key < right for key in self.keys)
        self.guarded_gaps[left, right] = tx

    def insert(self, tx, key):
        if key in self.keys:
            return "DUPLICATE"

        for (left, right), owner in self.guarded_gaps.items():
            if owner != tx and left < key < right:
                return "WAIT"

        insort(self.keys, key)
        return "INSERTED"

    def commit(self, tx):
        self.record_locks = {
            key: owner for key, owner in self.record_locks.items()
            if owner != tx
        }
        self.guarded_gaps = {
            gap: owner for gap, owner in self.guarded_gaps.items()
            if owner != tx
        }


only_record = ToyInventoryIndex([10, 20, 30])
only_record.lock_record("A", 10)
assert only_record.insert("B", 15) == "INSERTED"
print("只锁 10：B 插入 15 -> INSERTED")


guarded = ToyInventoryIndex([10, 20, 30])
guarded.protect_empty_range("A", 10, 20)
assert guarded.insert("B", 15) == "WAIT"
print("保护 (10, 20)：B 插入 15 -> WAIT")

guarded.commit("A")
assert guarded.insert("B", 15) == "INSERTED"
print("A 提交后：B 插入 15 -> INSERTED")
```

**已实际运行的输出：**

```text
只锁 10：B 插入 15 -> INSERTED
保护 (10, 20)：B 插入 15 -> WAIT
A 提交后：B 插入 15 -> INSERTED
```

这段小程序只多加了一项能力：保护一个尚未存在记录的区间。

而这个新增能力恰好使结果发生变化：

- 只锁住 ID 为 `10` 的已有记录，不能阻止插入 `15`；
- 保护 `(10, 20)` 的间隙，事务 B 插入 `15` 时只能等待；
- 当持有保护的事务完成，阻塞才有机会解除。

我们的模型通过 `(left, right)` 直接保存区间，这是便于教学的抽象。

真实 InnoDB 并不是维护一张“SQL 范围表达式 → 锁”的字典。它的锁作用于**实际被扫描的索引记录及其相邻间隙**。接下来我们必须理解这个实现差异。

## 4. 间隙到底附着在哪里？

仍然看主键索引：

```text
(-∞, 10)      (10, 20)      (20, 30)      (30, +∞)
    |             |             |              |
   10            20            30        索引末端
```

这里的 `(10, 20)` 表示可以插入 `11、15、19` 等键值的空隙，不是已经存在一条叫作“间隙”的记录。

InnoDB 在锁实现中将索引记录以及它前面的空隙关联起来。这样，锁定某个记录前方的间隙，就能够限制向该间隙插入新的索引条目。

对于一个实际存在的索引记录 `20`，我们可以区分：

```text
只保护记录：       [20]
只保护前方间隙：   (10, 20)
同时保护两者：     (10, 20]
```

前两种需求现在都已经遇到了。

如果我们既要锁住 `20` 这条记录，又不允许在它前面插入新记录，难道每次都必须分开处理吗？

于是出现了第三种组合：**Next-Key Lock**。

它可以理解为：

> 一个索引记录上的记录锁，加上这个记录之前的间隙锁。

例如，对 `20` 的 Next-Key Lock 覆盖 `(10, 20]`。

这里的括号含义非常重要：`(` 表示不包含端点，`]` 表示包含端点。所以 `(10, 20]` 保护的是 `10` 之后到 `20` 这一段，其中 `20` 本身也受到保护。

真正的 InnoDB 还会使用页面边界的伪记录处理索引最前、最后的区间。但我们暂时只研究两个相邻实际主键之间的情况，先把正常路径理解清楚。

## 5. 回到同一次商品查询：普通 SELECT 和 FOR UPDATE 有什么不同？

上一章，我们已经知道，在 `REPEATABLE READ` 下：

```sql
SELECT id
FROM products
WHERE id > 10 AND id < 20;
```

通常是一致性读取。它根据 Read View 判断哪些记录版本应该可见，**不会仅因使用了 REPEATABLE READ 就自动加 Gap Lock 阻止别人插入**。

所以，在事务 A 用普通 `SELECT` 看到了空结果以后，事务 B 仍可插入 ID 为 `15` 的新商品。事务 A 随后再次执行普通一致性读取，可能因为复用快照而依然看不到这条新记录。

这里要区分两件事：

- **让我读到的结果保持一致**：可以通过快照与版本可见性实现；
- **阻止别人向我检查的范围插入记录**：需要加锁读取或其他并发控制方式。

对当前“先检查范围，之后依赖检查结果执行操作”的业务，可以使用：

```sql
SELECT id
FROM products FORCE INDEX(PRIMARY)
WHERE id > 10 AND id < 20
FOR UPDATE;
```

`FOR UPDATE` 使这次读取走加锁路径。`FORCE INDEX(PRIMARY)` 用来让实验尽量稳定地沿主键索引范围扫描，便于观察锁在哪里。

在 InnoDB 默认的 `REPEATABLE READ` 隔离级别下，范围加锁读取可能对相关索引间隙加锁，使另一个事务插入 `15` 的请求等待。

这解释了为什么一个**返回零条记录的加锁查询**，仍然可能阻止另一个事务插入一条记录。

现在，我们去 MySQL 8.4.0 的源码里核实两件事：

1. InnoDB 是否真的区分“锁住记录”和“锁住记录前的间隙”？
2. 当另一个事务插入新记录时，它是否真的会检查这些间隙锁？

---

## 6. 源码第一站：锁的三种范围是如何表达的？

**固定版本：MySQL 8.4.0，Git Tag `mysql-8.4.0`**。

源码文件：[`storage/innobase/include/lock0lock.h`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/include/lock0lock.h#L889-L909)

**真实源码节选：**

```cpp
constexpr uint32_t LOCK_ORDINARY = 0;
constexpr uint32_t LOCK_GAP = 512;
constexpr uint32_t LOCK_REC_NOT_GAP = 1024;
```

这三个标记正好对应我们刚才推导出的三种范围。

`LOCK_REC_NOT_GAP` 表示只锁索引记录本身，不锁前方间隙。

`LOCK_GAP` 表示只保护记录前方的间隙，不等于拥有对该记录执行修改的权利。

`LOCK_ORDINARY` 表示普通的 Next-Key Lock，即记录与前方间隙组合起来的范围。

注意，这些值是锁请求的**精确范围标记**，不是独立的 S 锁、X 锁种类。一个锁请求还带有共享或排他等模式。

这样，当我们需要保护 `(10, 20)` 时，数据库不必发明一个以 SQL 字符串为键的“区间对象”。它可以通过在相应索引记录上记录间隙锁的方式，表达保护插入位置的要求。

这也解释了为什么索引对于范围锁如此重要：没有有序索引位置，就很难明确表达“在某两个键值之间新增记录”的物理位置。

不过，源码里的常量只是表示方式，还没有回答“什么时候选哪一种”。

## 7. 源码第二站：扫描记录时，为什么会选择不同的锁范围？

继续看：[`storage/innobase/row/row0sel.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/row/row0sel.cc#L4823-L4850)

我们已经在之前章节认识了 `row_search_mvcc()`：它是 InnoDB 从索引中读取记录的重要入口之一。

这次不研究如何从 B+ 树寻找页面，也不研究如何构造旧版本。只看**加锁读取遇到一条索引记录时，怎样判断需要保护哪些部分**。

在 `row_search_mvcc()` 的加锁分支里，可以找到如下真实代码：

```cpp
if (row_to_range_relation.gap_can_intersect_range) {
  lock_type = LOCK_ORDINARY;
} else {
  lock_type = LOCK_REC_NOT_GAP;
}
```

这是函数某个分支的**真实源码连续节选**，省略了外围条件。

先解释 `row_to_range_relation`。

它来自 `row_compare_row_to_range()`，用来判断当前索引记录和扫描区间的关系。

如果记录本身可能属于查询范围，而且它前面的间隙也可能与扫描范围相交，那么选择 `LOCK_ORDINARY`，也就是包含间隙的 Next-Key Lock。

如果记录可能属于范围，但它前面的间隙不可能影响本次范围查询，就可能选择 `LOCK_REC_NOT_GAP`，不必额外保护间隙。

另外，当记录本身不属于查询范围，**但它前面的间隙仍可能与扫描范围相交**时，同一个函数还可能选择 `LOCK_GAP`。这就解释了一个看似反直觉的行为：

> 迭代器已经遇到了不满足 WHERE 条件的边界记录，仍然可能需要在该记录前的间隙上设锁。

因为数据库保护的不是“查询返回了哪些行”，而是“沿着实际索引扫描路径，哪些位置出现新记录会改变这个范围检查”。

同一个函数随后调用：

```cpp
err = sel_set_rec_lock(
    pcur, rec, index, offsets,
    use_semi_consistent ? SELECT_SKIP_LOCKED : prebuilt->select_mode,
    prebuilt->select_lock_type, lock_type, thr, &mtr);
```

这是 MySQL 8.4.0 的**真实源码节选**。

调用参数中的 `lock_type`，就是刚才选择的精确锁范围。

输入是一条当前扫描到的索引记录，以及需要使用的锁模式和范围；输出则是加锁是否成功，或者是否需要等待等状态。

而 `sel_set_rec_lock()` 还会根据当前索引属于聚簇索引还是普通二级索引，把请求交给对应的记录加锁函数。这也呼应了前面章节的一个结论：**InnoDB 的记录锁作用在索引记录上，锁住哪些范围受实际索引扫描方式影响。**

我们已经看到锁范围如何被选择，但还有一个容易遗漏的条件：这次扫描是否应该跳过间隙锁？

## 8. 源码第三站：为什么 READ COMMITTED 的行为可能不同？

打开：[`storage/innobase/include/trx0trx.h`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/include/trx0trx.h#L1019-L1030)

`trx_t` 提供了：

**真实源码节选：**

```cpp
bool skip_gap_locks() const {
  switch (isolation_level) {
    case READ_UNCOMMITTED:
    case READ_COMMITTED:
      return (true);
    case REPEATABLE_READ:
    case SERIALIZABLE:
      return (false);
  }
```

这里省略了函数末尾的调试断言和兜底返回。

现在我们能直接从源码看出：InnoDB 会根据事务隔离级别，决定普通索引搜索是否跳过相应的间隙锁逻辑。

在 `row_search_mvcc()` 的相关逻辑里，`trx->skip_gap_locks()` 也参与锁范围选择。

这意味着，在相同的索引结构和范围条件下，`READ COMMITTED` 与 `REPEATABLE READ` 的加锁行为可能不同。

典型地：

- `REPEATABLE READ` 下的范围加锁读取会使用 Next-Key / Gap Lock 保护相关索引区间；
- `READ COMMITTED` 下，普通搜索与索引扫描不使用这些范围间隙锁，但外键检查和重复键检查等仍有例外。

不能据此得出“READ COMMITTED 完全不使用间隙锁”这样的结论。

更不能得出“REPEATABLE READ 下所有 SELECT 都加间隙锁”的结论，因为普通一致性读取和加锁读取本来就是不同路径。

现在我们已经知道 InnoDB 怎样在读取时决定锁住什么。

最后还有一步：事务 B 插入商品 `15` 时，为什么会等待？

## 9. 源码第四站：INSERT 怎样遇到间隙锁？

打开：[`storage/innobase/lock/lock0lock.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/lock/lock0lock.cc#L4941-L5042)

找到：

```cpp
lock_rec_insert_check_and_lock(...)
```

这个函数负责检查其他事务持有的锁是否阻止当前索引插入立即完成。

为了理解这一层，我们不需要把函数中的锁队列管理全部展开。

关键的真实源码语句是：

```cpp
const rec_t *next_rec = page_rec_get_next_const(rec);
ulint heap_no = page_rec_get_heap_no(next_rec);
```

这里定位的是插入位置后面的索引记录。在我们的小例子中，插入 `15` 时的后继记录可以理解为 `20`。

为什么需要关注 `20`？

因为此前我们已经知道，“记录 `20` 前方的间隙”正好可以表达 `(10, 20)` 这个范围。

该函数随后构造用于检查插入冲突的锁模式：

```cpp
const ulint type_mode = LOCK_X | LOCK_GAP | LOCK_INSERT_INTENTION;
```

这次出现了一个新的标记：`LOCK_INSERT_INTENTION`。

它表示**插入意向锁**，用于表达事务准备向某个间隙插入新索引记录的意图。

如果另一个事务持有阻止插入的间隙锁，插入请求就可能进入等待队列。

如果没有冲突，就可以继续完成插入。

这里还有一个值得注意的设计取舍：不同事务向同一个大间隙插入**不同位置**的记录，插入意向锁本身并不必然互相阻塞。

例如：

```text
已有记录：10 ------------------------ 20

事务 B 想插入 13
事务 C 想插入 17
```

如果没有其他范围保护或唯一键等冲突，B 和 C 没必要仅仅因为处于同一个大间隙，就全部串行执行。

但如果事务 A 已经通过 Gap Lock 保护了 `(10, 20)`，B 和 C 都可能因为 A 的范围保护而等待。

这正是插入意向锁与间隙锁之间的区别：

**前者表示我想插入，后者表示其他事务暂时不能向这个间隙插入。**

二者不是同一个概念，也不是所谓“所有插入都要先拿到一个互斥的间隙大锁”。

到这里，本章的设计链条就完整了：

```text
事务 A：加锁读取索引范围
        |
        v
row_search_mvcc()
        |
        v
根据记录与扫描范围关系决定锁范围
        |
        v
sel_set_rec_lock()
        |
        v
相关索引记录 / 前方间隙被保护

事务 B：尝试 INSERT 新记录
        |
        v
lock_rec_insert_check_and_lock()
        |
        v
检查插入位置附近的既有锁
        |
        +-- 无冲突 --> 继续插入
        |
        +-- 有冲突 --> 排队等待
```

这是**关键机制图**，不是 `SELECT` 与 `INSERT` 两条 SQL 从客户端进入数据库的完整逐帧调用栈。

---

## 10. 真实 MySQL 双会话实验：零行结果却阻止插入

前面的 Python 模型只验证了我们提出的设计需求。下面把同一个现象搬到真正的 MySQL。

**实验环境要求：** MySQL 8.4.x、InnoDB、两个独立客户端会话。建议使用专用实验库；不要在生产表上运行。

### 10.1 初始化数据（只执行一次）

```sql
CREATE DATABASE IF NOT EXISTS mysql_source_lab;
USE mysql_source_lab;

DROP TABLE IF EXISTS gap_lock_products;

CREATE TABLE gap_lock_products (
    id BIGINT NOT NULL PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    stock INT NOT NULL
) ENGINE = InnoDB;

INSERT INTO gap_lock_products (id, name, stock)
VALUES
    (10, 'Mouse', 10),
    (20, 'Monitor', 10),
    (30, 'Desk', 10);
```

我们另建一张 `gap_lock_products`，是为了避免修改前几章一直使用的 `products` 数据，从而保持整个系列实验互不干扰。

### 10.2 会话 A：锁住一个没有记录的范围

在**会话 A**中执行：

```sql
USE mysql_source_lab;

SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ;
START TRANSACTION;

SELECT id
FROM gap_lock_products FORCE INDEX(PRIMARY)
WHERE id > 10 AND id < 20
FOR UPDATE;
```

预期返回空结果。

**此时不要提交或回滚会话 A 的事务。**

我们不是依赖查询结果本身，而是在 `REPEATABLE READ` 下要求加锁读取这个主键范围。

### 10.3 会话 B：插入这个范围里的新商品

打开**第二个会话 B**：

```sql
USE mysql_source_lab;

SET SESSION innodb_lock_wait_timeout = 30;

INSERT INTO gap_lock_products (id, name, stock)
VALUES (15, 'Keyboard', 10);
```

在没有外键、额外触发器或其他干扰，并且会话 A 的范围锁仍然有效时，**预期会话 B 的插入进入锁等待，而不是立刻完成**。

不必为了观察而一直等到超时。可以打开第三个客户端观察等待状态，然后在会话 A 中结束事务。

### 10.4 会话 C：检查正在等待的锁

在**会话 C**中执行：

```sql
SELECT
    ENGINE_TRANSACTION_ID,
    OBJECT_SCHEMA,
    OBJECT_NAME,
    INDEX_NAME,
    LOCK_TYPE,
    LOCK_MODE,
    LOCK_STATUS,
    LOCK_DATA
FROM performance_schema.data_locks
WHERE OBJECT_SCHEMA = 'mysql_source_lab'
  AND OBJECT_NAME = 'gap_lock_products'
ORDER BY ENGINE_TRANSACTION_ID, LOCK_STATUS, INDEX_NAME;
```

在相关权限与 Performance Schema 可用的条件下，可以观察索引记录上的已授予锁，以及插入请求的等待状态。

对于 `performance_schema.data_locks`，应重点理解：

- `INDEX_NAME`：锁关联的索引；
- `LOCK_MODE`：包含 `X`、`GAP` 等模式信息；
- `LOCK_STATUS`：是 `GRANTED` 还是 `WAITING`；
- `LOCK_DATA`：与索引键有关的展示值，可能因页面缓存状态而为空。

不要要求输出必须与某份截图逐行完全一致。物理记录、查询范围边界、锁合并情况以及其他内部细节，都可能影响实际呈现。

若想更明确地看谁在等待谁，还可以查看：

```sql
SELECT *
FROM performance_schema.data_lock_waits;
```

它展示请求锁与阻塞锁之间的关系，适合用来验证“插入被哪个已有事务阻塞”。

### 10.5 会话 A：释放范围保护

回到**会话 A**：

```sql
COMMIT;
```

如果 B 尚未达到锁等待超时时间，预期 B 的插入可以继续完成。

然后在 B 中确认：

```sql
SELECT id, name
FROM gap_lock_products
WHERE id = 15;
```

预期看到新增的商品 `15`。

**实验解释：**

范围查询本身没有返回任何记录，但对这个范围进行加锁读取后，另一个事务向范围内部插入新索引键仍可能需要等待。

这正是“锁住查询结果行”和“保护索引间隙”之间的关键差别。

### 10.6 对照实验：改成 READ COMMITTED

为了确保前一个实验不是偶然的客户端问题，重置表数据：

```sql
DELETE FROM gap_lock_products WHERE id = 15;
```

在**会话 A**开启新的事务：

```sql
SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED;
START TRANSACTION;

SELECT id
FROM gap_lock_products FORCE INDEX(PRIMARY)
WHERE id > 10 AND id < 20
FOR UPDATE;
```

同样返回空结果，但对于通常的索引范围搜索，`READ COMMITTED` 不采用 `REPEATABLE READ` 那样的间隙锁保护。

此时在**会话 B**执行：

```sql
INSERT INTO gap_lock_products (id, name, stock)
VALUES (15, 'Keyboard', 10);
```

在没有其他冲突的情况下，预期插入能够直接完成，而不必等待会话 A 提交。

实验结束后记得在 A 中执行：

```sql
ROLLBACK;
```

这说明：同样是 `FOR UPDATE`，锁的范围仍与隔离级别、索引访问路径和具体条件有关。

**验证边界：** 以上 MySQL 双会话步骤已按官方锁行为设计，但本章没有在真实 MySQL 8.4 服务器上执行，不将预期等待或 `data_locks` 输出冒充实测结果。Python 最小模型的三个断言已实际运行并通过。

---

## 11. 为什么不是所有范围都应该锁住？

我们已经看到，Gap Lock 能阻止其他事务在某些索引范围里插入记录。

但这并不意味着它总是好事。

回到商品系统，如果大量业务事务都长时间锁住一片范围，就可能出现：

```text
事务 A：检查商品 ID 10 到 20 的范围，迟迟不提交
事务 B：尝试插入商品 15，等待
事务 C：尝试插入商品 17，也等待
```

A 的事务持续越久，B、C 可能等待得越久。

因此，保护范围提供了业务正确性上的某些约束，也会牺牲相关插入的并发能力。

还有三个经常被误解的边界需要分清。

### 11.1 唯一索引精确命中通常不必锁间隙

```sql
SELECT id
FROM gap_lock_products
WHERE id = 10
FOR UPDATE;
```

当通过唯一主键精确查到现有记录时，通常可以只锁这条记录本身。这正是前面 `LOCK_REC_NOT_GAP` 的典型使用场景。

但是，如果查询的是**不存在的唯一键**，情况不应简单套用“所有唯一索引等值查询都不锁 Gap”的口诀，因为查找未命中时也可能需要保护相应位置。

### 11.2 “WHERE 条件像一个范围”不代表只锁逻辑范围

InnoDB 锁定的是索引访问过程中涉及的记录和区间，实际范围与使用的索引、搜索边界及执行计划有关。

如果没有合适索引，数据库可能扫描更多记录，甚至带来比预想大得多的锁影响。

因此，在分析真实阻塞时，不能只盯着 `WHERE id > 10 AND id < 20` 的字面含义；还应检查 `EXPLAIN` 和 `performance_schema.data_locks`。

### 11.3 Gap Lock 主要限制插入，不是给空隙里的每个值加实体行锁

没有商品 `15` 时，数据库并没有一条可以锁住的商品 `15` 记录。

Gap Lock 的意义，是限制在相应索引位置创建新的记录。不同事务持有的纯间隙锁甚至可以彼此共存，因为它们共同表达的是“暂时不允许其他事务向这里插入”的限制。

对于两个插入请求，只要没有其他冲突，它们也不应仅仅因为想插入同一大间隙中的不同键就无谓地彼此阻塞。这是插入意向锁设计所保留的并发空间。

## 12. “幻读被解决”究竟应该怎样理解？

很多入门资料把 Gap Lock 和 Next-Key Lock 概括为“解决幻读”。这个说法需要加上条件。

如果事务 A 在 `REPEATABLE READ` 下进行普通一致性读取，它通常依靠同一个 Read View 看到一致的历史版本；这不是因为它锁住了所有可能出现的新行。

如果事务 A 在 `REPEATABLE READ` 下进行适当的**范围加锁读取**，InnoDB 才可能通过 Next-Key / Gap Lock 阻止其他事务在相关索引间隙中插入满足该范围的新记录。

这两条路径针对的是两种不同的需求：

```text
普通 SELECT
    |
    +--> 我应该看到哪个版本？
         关注 Read View / MVCC

SELECT ... FOR UPDATE
    |
    +--> 我需要保护哪些现有记录及插入位置？
         关注记录锁 / 间隙锁 / Next-Key Lock
```

因此不能说“只要设置了 REPEATABLE READ，所有范围查询都会锁住插入”，也不能说“有了 MVCC 就再也不需要范围锁”。

有些业务更适合通过唯一约束、单条条件更新或重新设计事务逻辑解决竞争问题，不一定需要长时间执行大范围的 `FOR UPDATE`。是否值得承担间隙锁带来的并发成本，取决于业务到底需要防止哪一种异常。

## 13. 回看：从已有记录，到记录之间的位置

本章最开始，我们只会锁住一条已经存在的商品记录。

对于：

```sql
SELECT * FROM products WHERE id = 10 FOR UPDATE;
```

这种理解通常已经够用。

但当需求变成“我检查过 `(10, 20)`，事务结束前不能有人向这里插入新商品”时，锁住已有记录就不够了。

因为 `(10, 20)` 里可能根本没有记录。

于是我们引入了保护索引空隙的能力。

接下来发现，读取范围时往往还需要同时保护记录本身，于是出现 Next-Key Lock，把记录与它前面的间隙组合起来。

最后，当另一个事务尝试向这个范围插入新记录时，它需要检查是否存在冲突的范围保护，并在必要时等待。

```text
已有记录保护
    |
    v
无法阻止在记录之间插入
    |
    v
需要保护索引间隙
    |
    v
Gap Lock
    |
    v
有时既要保护记录，又要保护前方间隙
    |
    v
Next-Key Lock
    |
    v
INSERT 在插入位置检查冲突
    |
    v
必要时等待，事务结束后继续
```

最重要的不是背出三个锁的定义，而是理解：

> **在有序索引里，一次范围加锁读取可能需要保护的，不只是当前存在的记录，还包括未来可能插入新记录的位置。**

这正是我们沿着实际业务问题推导到 Gap Lock 和 Next-Key Lock 的原因。

## 14. 下一章：锁可以解决竞争，为什么又会带来死锁？

现在，我们已经知道 InnoDB 可以把某些冲突操作安排为等待。

但这也引出了新的问题。

假设一个订单需要同时扣减商品 `10` 和商品 `20` 的库存。

事务 A 先锁住商品 `10`，再尝试修改商品 `20`。

与此同时，事务 B 先锁住商品 `20`，再尝试修改商品 `10`。

结果可能变成：

```text
事务 A 持有商品 10 的锁
事务 B 持有商品 20 的锁

事务 A 等待商品 20
事务 B 等待商品 10
```

两个事务都在等待对方释放锁。

单纯“等待到锁释放”，已经无法保证系统继续前进。

下一章，我们将继续沿订单与商品库存案例，研究 MySQL 如何发现循环等待、选择回滚其中一个事务，以及为什么应用程序仍然需要处理死锁重试。

**下一章：第 24 章《从锁等待到死锁：MySQL 怎样发现循环等待？》**

---

## 本章源码与参考资料

统一源码版本：**MySQL 8.4.0**，Git Tag：`mysql-8.4.0`。

| 位置 | 本章实际研究的作用 |
|---|---|
| [`storage/innobase/include/lock0lock.h`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/include/lock0lock.h#L889-L909) | 定义 `LOCK_ORDINARY`、`LOCK_GAP`、`LOCK_REC_NOT_GAP`、`LOCK_INSERT_INTENTION` |
| [`storage/innobase/include/trx0trx.h`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/include/trx0trx.h#L1019-L1030) | `trx_t::skip_gap_locks()` 体现隔离级别差异 |
| [`storage/innobase/row/row0sel.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/row/row0sel.cc#L3944-L4000) | `row_compare_row_to_range()` 判断记录、间隙与扫描区间关系 |
| [`storage/innobase/row/row0sel.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/row/row0sel.cc#L4823-L4850) | `row_search_mvcc()` 选择并提交记录/间隙加锁请求 |
| [`storage/innobase/row/row0sel.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/row/row0sel.cc#L1042-L1095) | `sel_set_rec_lock()` 将锁请求交给聚簇或二级索引路径 |
| [`storage/innobase/lock/lock0lock.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/lock/lock0lock.cc#L4941-L5042) | `lock_rec_insert_check_and_lock()` 检查插入位置是否被其他事务阻塞 |

官方文档：

- [MySQL 8.4 — InnoDB Locking](https://dev.mysql.com/doc/refman/8.4/en/innodb-locking.html)
- [MySQL 8.4 — Locks Set by Different SQL Statements](https://dev.mysql.com/doc/refman/8.4/en/innodb-locks-set.html)
- [MySQL 8.4 — Phantom Rows](https://dev.mysql.com/doc/refman/8.4/en/innodb-next-key-locking.html)
- [MySQL 8.4 — Performance Schema data_locks](https://dev.mysql.com/doc/refman/8.4/en/performance-schema-data-locks-table.html)

**验证说明：** 本章 Python 教学模型已经在本次撰写过程中运行，三个断言通过；MySQL 双会话 SQL 为可复现方案，未在本次环境中连接 MySQL Server 实测。正文源码片段均以 MySQL 8.4.0 固定标签为准；概念图与 Python 实现均明确为教学简化。
