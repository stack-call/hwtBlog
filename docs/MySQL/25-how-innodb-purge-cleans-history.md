---
title: "25｜从保留旧版本到 Purge：历史记录什么时候才能删除？"
sidebar_position: 26
slug: /mysql/25-how-innodb-purge-cleans-history
description: 从商品 42 的长事务一致性读取出发，逐步理解 Undo 历史为何不能立即删除，并沿 MySQL 8.4.0 源码追踪 Purge 的安全边界、任务分发和行级清理。
tags: [MySQL, InnoDB, Purge, Undo, MVCC, 源码精读]
---

# 从保留旧版本到 Purge：历史记录什么时候才能删除？

上一章，我们研究了两个购买事务相互等待时，InnoDB 怎样发现死锁，并选择回滚一个事务。

回滚之所以可能，是因为数据库保存了撤销修改所需的信息。

再往前，在第 10 章和第 22 章，我们又发现：**Undo 不仅帮助事务回滚，还能够让普通 `SELECT` 读取适当的历史版本。**

那么，同一个机制就产生了一个新的问题：

> 如果商品 42 每天被修改成千上万次，旧版本信息难道要一直保存下去吗？

如果永远保留，历史数据就可能不断积累。

如果刚提交就删除，可能还有事务需要读取修改之前的库存。

这一章只解决这一个问题：**InnoDB 怎样判断历史信息何时可以安全清理？**

## 1. 先制造一个不能丢掉的旧库存

我们继续使用商品 42，库存最初是 `100`。

```sql
SELECT stock FROM products WHERE id = 42;
-- 假设此时返回 100
```

现在有两个数据库连接。

连接 A 先开启一个 `REPEATABLE READ` 事务，并查询商品库存：

```sql
-- 会话 A
SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ;
START TRANSACTION;

SELECT stock FROM products WHERE id = 42;
-- 100
```

在这个实验中，A 第一次执行普通一致性读取，会建立后续需要使用的读取视图。A 暂时不提交，也不回滚。

连接 B 随后购买一件商品，并提交修改：

```sql
-- 会话 B（单独连接，autocommit = 1）
UPDATE products
SET stock = stock - 1
WHERE id = 42 AND stock > 0;

SELECT stock FROM products WHERE id = 42;
-- 99
```

现在的状态有点奇怪：

```text
会话 A：早先读取了库存 100，事务仍然开启
会话 B：已经提交库存 99
```

如果 A 再次执行普通 `SELECT`，在本实验的 `REPEATABLE READ` 条件下，仍应看到：

```sql
-- 会话 A
SELECT stock FROM products WHERE id = 42;
-- 100
```

但另一个新连接进行普通查询，就应该看到已经提交的 `99`。

同一条商品记录，在不同事务看来可以有不同的可见版本。

这并不是数据复制出了两张业务表，也不是会话 A 在自己的内存中缓存了完整查询结果。

我们在 MVCC 章节已经学过：InnoDB 可以通过记录中的事务信息及 Undo 链，把适当的旧版本重新构造出来。

因此此刻，商品 42 的历史信息至少具有一个现实用途：**让仍持有旧读视图的 A 继续读到 `100`。**

若 B 一提交就把相关旧版本信息全部丢掉，A 后续一致性读取就可能失去重建历史版本所需的依据。

所以“事务已经提交”不能直接推导出“它产生的历史信息都可以删除”。

## 2. 最简单的处理：暂时不删除

既然无法确定旧版本是否仍然有人使用，我们可以采用最保守的办法：先把历史全部留下。

假设 B 继续完成两次独立的购买：

```text
第一次提交：100 → 99
第二次提交： 99 → 98
第三次提交： 98 → 97
```

为了便于理解，先用一个简化的版本链表示：

```text
最新库存 97
    |
    v
旧版本 98
    |
    v
旧版本 99
    |
    v
旧版本 100
```

这里的“旧版本节点”是教学抽象；InnoDB 实际通过聚簇索引记录中的回滚指针和 Undo 信息重建版本，并不是把四份完整的商品行简单串成 Python 链表。

如果 A 的读视图还需要看到 `100`，我们就不能破坏从最新记录重建 `100` 所必需的历史信息。

但如果 A 已经结束，其他事务也不再需要这些旧版本，还要继续保存它们吗？

没有必要。

于是，我们真正需要的是一个新动作：

**在确认历史版本已不再被需要之后，异步清理旧的 Undo 历史与相应的废弃索引记录。**

这个动作，在 InnoDB 中叫作 **Purge**。

注意，Purge 不是 SQL 的 `DELETE` 命令，也不是用户发起的 `ROLLBACK`。它属于数据库的后台清理工作。

## 3. 先写一个最小的“保留—清理”实验

我们暂时不模拟真实 Undo 页或 InnoDB 的线程调度。只创建一个可以观察的、保守的多版本模型。

一个版本由两个值构成：

- `commit_no`：这个教学模型中单调增加的提交编号；
- `stock`：这次提交之后的库存值。

当事务 A 开始一致性读取时，记录当前编号。后续提交不会改变 A 的编号。

**教学化简代码（可直接运行；不是 MySQL 源码）：**

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class Version:
    commit_no: int
    stock: int

class TinyHistoryStore:
    def __init__(self, initial_stock):
        self.clock = 0
        self.versions = [Version(0, initial_stock)]
        self.snapshots = {}

    def begin_snapshot(self, session):
        self.snapshots[session] = self.clock

    def read(self, session=None):
        boundary = self.snapshots[session] if session else self.clock
        return next(
            v.stock
            for v in reversed(self.versions)
            if v.commit_no <= boundary
        )

    def commit_update(self, stock):
        self.clock += 1
        self.versions.append(Version(self.clock, stock))

    def end_snapshot(self, session):
        del self.snapshots[session]

    def purge(self):
        # 教学化简：只要有更老的快照，就保守地保留
        # 从最老快照编号开始的全部版本。
        oldest = min(self.snapshots.values(), default=self.clock)
        self.versions = [
            v for v in self.versions
            if v.commit_no >= oldest
        ]

store = TinyHistoryStore(100)
store.begin_snapshot("A")

store.commit_update(99)
store.commit_update(98)

assert store.read("A") == 100
assert store.read() == 98

print("A 仍在读取旧快照:", store.read("A"))
print("新请求看到的最新库存:", store.read())

store.purge()
assert [v.stock for v in store.versions] == [100, 99, 98]
print("A 持有快照时保留的版本:",
      [v.stock for v in store.versions])

store.end_snapshot("A")
store.purge()
assert [v.stock for v in store.versions] == [98]
print("A 结束后保留的版本:",
      [v.stock for v in store.versions])

print("全部断言通过")
```

**实际运行结果：**

```text
A 仍在读取旧快照: 100
新请求看到的最新库存: 98
A 持有快照时保留的版本: [100, 99, 98]
A 结束后保留的版本: [98]
全部断言通过
```

这里出现了最重要的行为变化。

在 A 仍然持有旧快照时，我们保留了可以支持旧读取的版本信息。

当 A 结束后，模型只留下最新的库存 `98`。

这说明我们需要的不仅是一份历史数据，还需要某种**判断历史仍然被谁使用的依据**。

不过，这个程序做了大量简化：

它用整数提交编号代替真实事务 ID 与 Read View 的可见性规则；采用保守的版本保留策略；清理动作直接删除列表元素；没有考虑多行事务、Undo 页、索引删除标记、二级索引、并发后台线程及额外的安全约束。

因此它只能验证本章的设计动机，不能当作 InnoDB Purge 算法的复制品。

现在去真实源码里找证据。

---

## 4. 已提交的 Undo 为什么还会留在系统里？

固定源码版本：**MySQL 8.4.0，Git Tag `mysql-8.4.0`**。

先打开：

[`storage/innobase/trx/trx0purge.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/trx/trx0purge.cc)

前面介绍 Undo 时，我们主要关心事务尚未提交时怎样回滚修改。

这次从另一个方向看：**事务提交以后，用于历史版本访问的更新 Undo 怎样交给后续清理过程？**

在 `trx0purge.cc` 中，有这样一个函数：

```cpp
void trx_purge_add_update_undo_to_history(...)
```

它会把适当的更新 Undo 日志挂入回滚段的历史链表。与当前问题直接相关的代码是：

**真实源码节选（省略函数其余部分）：**

```cpp
flst_add_first(rseg_header + TRX_RSEG_HISTORY,
               undo_header + TRX_UNDO_HISTORY_NODE, mtr);
```

这不是“撤销一条 UPDATE”的操作。

它是在管理**已经完成的事务留下、后续仍可能有用途的历史 Undo**。

紧接着，在相应条件下还会更新计数：

```cpp
trx_sys->rseg_history_len.fetch_add(n_added_logs);
```

这里的 `rseg_history_len` 和我们稍后在 `SHOW ENGINE INNODB STATUS` 看到的 `History list length` 相关。

但不要把它误认为“历史行数”。它反映的是回滚段历史列表的长度指标，**不是商品 42 被更新了多少次，更不是当前存在多少个旧版本行对象**。

现在我们至少能解释一件事：

事务提交以后，某些更新 Undo 不会立即消失，而是进入允许后台 Purge 继续处理的历史管理结构。

于是，新问题自然出现：如果后台线程看到有历史信息，能不能马上全部清理？

当然不能。会话 A 可能仍然需要库存 `100`。

因此，Purge 必须知道它最多能清理到哪里。

## 5. 谁告诉 Purge：最老的事务还需要什么？

在第 22 章中，我们学习过 `Read View`。

它帮助一次一致性读取判断：某个记录版本是否应该对当前事务可见。

现在，我们并不是要为 A 判断“这个版本能不能看到”，而是要站在清理器的角度，判断：

**是否还有某个未结束的读视图，需要依赖这段历史？**

如果只检查最新创建的视图，很可能漏掉 A 这样的老事务。

所以，清理进度必须受到最老的相关读视图约束。

在 MySQL 8.4.0 的 `trx_purge()` 批次处理中，可以找到：

**真实源码节选：`storage/innobase/trx/trx0purge.cc`**

```cpp
trx_sys->mvcc->clone_oldest_view(&purge_sys->view);
```

这个调用发生在 Purge 批次准备阶段，它会把最老读视图的相关信息复制到 Purge 自己使用的视图中。

接着观察 `MVCC::clone_oldest_view()` 的真实实现：

[`storage/innobase/read/read0read.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/read/read0read.cc)

其核心动作包括：

```cpp
ReadView *oldest_view = get_oldest_view();
```

如果没有活跃的老视图，会准备相应的默认视图；如果存在，就复制它的可见性信息。

这里不需要重新展开 `Read View` 全部字段。我们只需要把它与刚才的实验联系起来：

```text
事务 A 开始读取库存 100
           |
           v
A 保留自己的 Read View
           |
           v
其他事务提交库存 99、98
           |
           v
Purge 准备清理历史
           |
           v
参考最老的相关 Read View
           |
           v
不能越过仍可能被旧读取需要的历史边界
```

这给清理过程加上了安全约束。

**它解决的不是如何恢复旧版本，而是何时可以不再保留某些旧版本信息。**

还有一个细节：在 MySQL 8.4.0 的 `MVCC::clone_oldest_view()` 中，除了复制最老读视图，还存在与 GTID 持久化有关的边界处理。因此不能把真实 Purge 的全部安全判断简化为“没有活跃读事务就可以删除所有 Undo”。

现在我们知道了，Purge 会参考最老读视图。

但它怎样把这个安全边界用到具体的历史记录上？

## 6. 从“有历史记录”到“这条记录可以处理”

继续打开 `trx0purge.cc`，定位：

```cpp
trx_purge_fetch_next_rec(...)
```

这是 Purge 从历史记录中获取下一条待处理 Undo 记录的一个内部函数。

在它的实现中，可以找到一个非常直接的边界检查：

**真实源码节选：**

```cpp
if (purge_sys->iter.trx_no >= purge_sys->view.low_limit_no()) {
  return nullptr;
}
```

读这几行不需要立刻掌握每个内部编号的数学含义。

我们先理解行为：当当前清理进度达到受读视图约束的界限时，它不会继续取出后面的 Undo 记录。

这正对应我们的需求：

如果继续向前清理，可能破坏一个尚未结束的旧事务所需的历史信息，那就应该先停下来。

但要特别注意，真实源码使用的是事务序号、Purge 游标以及 Read View 的边界信息，不是我们教学代码里的简单 `commit_no`。

不能把上面的 C++ 比较机械翻译成“比较库存版本序号”。

现在我们终于回答了本章的一半问题：

**Purge 怎样决定暂时不能清理的历史边界。**

另一半问题是：当某些 Undo 已经可以处理时，后台线程具体怎么处理？

## 7. Purge 怎样把清理任务交给后台工作线程？

我们之前自己写的教学程序只有一个 `purge()` 方法。

真实 InnoDB 需要让清理过程和用户读写请求并行运行，不能要求每个 UPDATE 都同步完成全部历史清理才能返回。

这意味着 Purge 需要分批工作，还可能把任务交给多个工作线程。

在 `trx0purge.cc` 的 `trx_purge()` 中，先看到：

**真实源码节选：**

```cpp
n_pages_handled = trx_purge_attach_undo_recs(n_purge_threads, batch_size);
```

这里的职责，是从符合安全边界的历史记录中整理当前批次要处理的 Undo。

随后，函数会把相关任务分发给工作线程，并等待本批次完成。

```cpp
srv_que_task_enqueue_low(thr);
```

任务最终通过 Purge 专用的查询执行节点进入实际行级清理。

这里不要把它理解为：`UPDATE` 线程直接在这段代码里删除历史记录。

我们现在研究的是**后台 Purge 批次路径**，它和此前同步执行库存扣减的 SQL 路径不是同一条直接调用链。

它们依靠已提交的 Undo 历史信息联系起来。

这也是为什么一个已经完成的商品购买请求，并不需要等待所有可清理的历史信息立即被回收。

## 8. 真正执行清理的代码在哪里？

刚才的 `trx_purge()` 负责安全边界与任务组织。

但删除标记的索引记录、处理旧更新信息，不应全都堆在这个调度函数里。

继续进入：

[`storage/innobase/row/row0purge.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/row/row0purge.cc)

这里可以找到：

```cpp
que_thr_t *row_purge_step(que_thr_t *thr)
```

这是行级 Purge 执行节点的一个入口。

在取出待处理 Undo 后，它会调用：

**真实源码节选：**

```cpp
row_purge(node, rec.undo_rec, thr);
```

从这里继续向下，`row_purge()` 先解释 Undo 记录中的信息，再调用具体的行级清理逻辑。

当我们进一步进入 `row_purge_record_func()`，会看到它按 Undo 记录的类型处理。

其中一个关键分支是：

```cpp
case TRX_UNDO_DEL_MARK_REC:
  purged = row_purge_del_mark(node);
```

为什么会出现“删除标记”？

我们可以在当前商品场景里稍微改变一次业务操作：商品停卖，需要删除商品 42。

```sql
DELETE FROM products WHERE id = 42;
```

从业务 SQL 的角度，删除后这条商品记录不应该再被新的一致性读取当作可见的有效商品。

但从存储结构的角度，如果仍然有旧事务可能需要看到删除之前的版本，数据库就不能不加判断地立刻清除所有相关历史和索引记录。

因此，InnoDB 的删除涉及删除标记与之后的物理清理。等旧事务不再需要它们时，Purge 可以继续完成相应的索引记录清理。

这并不意味着每次普通 UPDATE 都会触发同样的删除索引记录分支。

例如，商品 `stock` 的更新可能不改变索引键，Purge 对应的行级处理也可能不需要修改索引结构。

在 `row_purge_record_func()` 中，针对普通旧记录更新和涉及外部存储字段的更新，还有其他分支。这正说明：**Purge 不是简单地把每一条 Undo 记录都翻译成一次删除索引记录的操作。**

这样，我们已经把清理的两部分职责接起来了：

```text
可供清理的历史 Undo
          |
          v
Purge 判断安全边界
          |
          v
收集本批次 Undo 任务
          |
          v
后台工作线程
          |
          v
row_purge_step()
          |
          v
解析 Undo、按操作类型清理
```

到这里，“为什么要有 Purge”和“为什么要独立的行级清理过程”已经能够互相解释。

## 9. History list length 会一直增长吗？

如果没有任何能够阻碍清理的旧读视图，并且后台清理能够跟上修改速度，那么已不再需要的历史 Undo 可以逐步处理和回收。

但假设有一个长事务 A，一直不结束。

在它持有旧视图期间，业务不断更新库存：

```text
100 → 99 → 98 → 97 → 96 → 95 → ...
```

新的修改不断产生日志历史，Purge 却受到最老视图的安全边界约束。

于是，历史列表可能增长。

这也是为什么一个“看起来只是读数据”的长事务，可能给高写入数据库带来额外压力：它并不一定阻塞其他人的每一次写入，却可能延后某些历史信息的回收。

MySQL 提供了观察这一现象的手段：

```sql
SHOW ENGINE INNODB STATUS\G
```

可以关注 `TRANSACTIONS` 部分的：

```text
History list length ...
```

也可以查看相应的 InnoDB 指标：

```sql
SELECT NAME, COUNT, STATUS
FROM INFORMATION_SCHEMA.INNODB_METRICS
WHERE NAME = 'trx_rseg_history_len';
```

**必须明确：** 这个指标是实例级的历史列表长度，不是某条商品记录的版本计数，也不是字节数。后台线程、其他业务事务以及当前服务器负载都会影响它的变化。

因此，真实实验中不能机械要求“执行五次 UPDATE，指标就必然增加五”。

它更适合用于观察历史处理进度及长期趋势。

## 10. 真实 MySQL 双会话实验：长事务与历史积累

下面给出一组可复现实验。请在**专用测试数据库**中执行，不要在生产实例上人为保持长事务。

### 10.1 准备独立商品表

```sql
CREATE DATABASE IF NOT EXISTS mysql_source_lab;
USE mysql_source_lab;

DROP TABLE IF EXISTS purge_products;

CREATE TABLE purge_products (
    id BIGINT PRIMARY KEY,
    stock INT NOT NULL
) ENGINE = InnoDB;

INSERT INTO purge_products(id, stock)
VALUES (42, 100);
```

为了保持原来的业务案例，我们仍然使用商品 `42`，只是采用独立表，避免覆盖其他章节中的测试数据。

### 10.2 会话 A：创建并保持旧快照

打开第一个数据库连接：

```sql
USE mysql_source_lab;

SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ;
START TRANSACTION;

SELECT stock FROM purge_products WHERE id = 42;
-- 预期：100
```

执行了第一次普通一致性读取后，暂时**不要**提交，也不要关闭该连接。

这一步的目的，是让 A 持有一个后续更新之前的读视图。

### 10.3 会话 B：持续提交库存更新

打开第二个数据库连接，确认自动提交：

```sql
USE mysql_source_lab;
SET SESSION autocommit = 1;

UPDATE purge_products SET stock = stock - 1 WHERE id = 42;
UPDATE purge_products SET stock = stock - 1 WHERE id = 42;
UPDATE purge_products SET stock = stock - 1 WHERE id = 42;
UPDATE purge_products SET stock = stock - 1 WHERE id = 42;
UPDATE purge_products SET stock = stock - 1 WHERE id = 42;

SELECT stock FROM purge_products WHERE id = 42;
-- 预期：95
```

这些独立语句已经提交，其他新事务应该能够看到当前库存 `95`。

### 10.4 再回会话 A：为什么还是 100？

```sql
SELECT stock FROM purge_products WHERE id = 42;
-- 预期：100
```

如果一切符合本实验条件，A 仍然看到 `100`。

这意味着它还需要与这个旧版本相对应的历史重建能力。

在会话 B 或其他监控连接中观察：

```sql
SHOW ENGINE INNODB STATUS\G

SELECT NAME, COUNT, STATUS
FROM INFORMATION_SCHEMA.INNODB_METRICS
WHERE NAME = 'trx_rseg_history_len';
```

可以重复观察指标；实际数值不作固定预设。

### 10.5 让 A 结束，再观察清理进度

在会话 A：

```sql
COMMIT;
```

此时旧读视图不再由这个事务持有。

回到监控连接，间隔一段时间再次执行：

```sql
SHOW ENGINE INNODB STATUS\G
```

在相对安静的测试实例中，Purge 可能逐渐追上此前积累的历史。不过历史列表变化受后台调度和其他事务影响，**不能要求提交 A 后指标立即变为零**。

从实验中我们需要确认的，不是某个精确历史列表数字，而是：

- A 未结束时，普通一致性读取仍能看到旧库存；
- B 的新事务已经能看到更新后的库存；
- A 结束后，这个旧视图不再限制那些历史信息的安全清理；
- 历史清理本身是后台工作，不是 A 提交时同步删除所有历史记录。

**实验说明：** 上述 SQL 提供的是 MySQL 8.4 的可复现步骤与预期现象。本章没有在真实 MySQL 服务器上执行该双会话实验，也没有捏造 `History list length` 的数值。

## 11. Purge、Undo、Redo 和 Checkpoint 到底有什么不同？

现在，我们学过多个带有“日志”“清理”“恢复”含义的机制。

它们容易混淆，但出发点不同。

回到商品 42 的库存扣减：

```sql
UPDATE products
SET stock = stock - 1
WHERE id = 42 AND stock > 0;
```

当事务需要放弃修改时，**Undo** 提供撤销所需的信息；当另一个事务需要读取合适的旧版本时，**Undo** 也参与历史版本重建。

当已经确认提交的修改所涉及的数据页还没全部写回磁盘，系统发生崩溃时，**Redo** 支持恢复数据库的物理状态。

随着数据页持续刷盘，**Checkpoint** 为 Redo 恢复与日志空间管理提供进度边界。

当某些已提交事务留下的历史记录和相关删除标记不再被需要时，**Purge** 负责后台清理。

可以简单对照：

| 机制 | 本系列用它回答的问题 |
|---|---|
| Undo | 改错了怎样回滚？旧事务怎样读到合适的历史版本？ |
| Redo | 数据页尚未落盘，崩溃后怎样恢复已确认的修改？ |
| Checkpoint | 怎样推进安全的恢复起点并管理日志空间？ |
| Purge | 哪些历史 Undo 和废弃索引记录已经可以清理？ |

它们会共同参与真实数据库运行，但并不互相替代。

尤其不要把 Purge 写成“清理 Redo Log”，也不要认为一旦商品页被 Checkpoint 覆盖，Undo 的历史就可以立即丢弃。

**Purge 的约束来自历史版本是否仍可能被需要；Checkpoint 的约束主要来自数据页持久化与日志恢复的安全进度。**

## 12. 回看：为什么数据库必须有 Purge？

这一章从一次库存扣减留下的旧值开始。

如果只有一个事务，并且没有人需要查看过去的版本，我们很容易想到：保留最新库存就足够了。

但是，当会话 A 已经看到库存 `100`，而会话 B 又提交了 `99` 和 `98` 时，A 可能仍然需要读取 `100`。

于是，“提交后立即删除全部旧信息”失败了。

我们可以保留所有历史，但持续写入又让它不断累积。

因此，数据库需要同时做到两件事：

第一，保留仍可能被旧读视图使用的历史。

第二，及时清理已经不再必要的历史信息和废弃的索引记录。

在真实 MySQL 8.4.0 中，我们沿源码找到了这条路径：

```text
已提交事务的 update Undo
             |
             v
       History List
             |
             v
  Purge 参考最老 Read View
             |
             v
   选择安全范围内的 Undo
             |
             v
  将记录分配给后台 Purge 工作
             |
             v
  row_purge_step() / row_purge()
             |
             v
   按 Undo 类型执行行级清理
```

我们终于可以回答本章的问题：

**旧版本什么时候可以删除？**

不是在每次 `COMMIT` 之后立刻删除；而是在对应历史信息不再被活跃一致性读取等机制需要，并满足 InnoDB 其他安全条件之后，由后台 Purge 逐步处理。

长期不结束的读事务，可能会使这个清理边界迟迟无法向前推进。

这也是为什么在生产环境里，一个长期挂起的只读事务仍然值得关注。

## 13. 下一章：为什么修改表结构也可能被事务阻塞？

到这里，我们研究的主要是数据记录：如何修改、如何并发读取、如何在不破坏旧快照的前提下清理历史信息。

但业务系统还会发生另一种变化。

假设我们想给商品表增加一个字段：

```sql
ALTER TABLE products
ADD COLUMN category VARCHAR(50);
```

看起来，这只是一次表结构修改。

但如果另外一个事务仍在读取或使用这张表，MySQL 能不能直接把表的定义改掉？

如果不能，谁负责协调这些操作？

为什么有时一条很简单的 `ALTER TABLE` 会等待很久，甚至让后续请求也开始排队？

下一章，我们仍然从商品表出发，转向另一个与事务和并发相关、但职责不同的问题：**元数据锁（MDL）**。

**下一章：第 26 章《从数据访问到表结构变更——为什么需要 MDL？》**

---

## 本章源码与验证说明

固定源码版本：**MySQL 8.4.0，Git Tag `mysql-8.4.0`**。

| 源码文件与函数 | 本章对应的设计动作 |
|---|---|
| `trx/trx0purge.cc` — `trx_purge_add_update_undo_to_history()` | 提交后需要保留的更新 Undo 怎样进入历史管理结构 |
| `read/read0read.cc` — `MVCC::clone_oldest_view()` | 怎样获得用于约束 Purge 的最老读视图信息 |
| `trx/trx0purge.cc` — `trx_purge_fetch_next_rec()` | 怎样阻止清理越过安全边界 |
| `trx/trx0purge.cc` — `trx_purge()` | 怎样收集与分发当前批次的清理任务 |
| `row/row0purge.cc` — `row_purge_step()` | 后台执行节点怎样取得待清理 Undo |
| `row/row0purge.cc` — `row_purge_record_func()` | 怎样根据 Undo 记录类型选择清理操作 |
| `row/row0purge.cc` — `row_purge_del_mark()` | 满足条件后怎样处理删除标记的记录 |

实际源码链接：

- [trx0purge.cc — MySQL 8.4.0](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/trx/trx0purge.cc)
- [read0read.cc — MySQL 8.4.0](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/read/read0read.cc)
- [row0purge.cc — MySQL 8.4.0](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/row/row0purge.cc)

MySQL 官方文档：

- [InnoDB Multi-Versioning](https://dev.mysql.com/doc/refman/8.4/en/innodb-multi-versioning.html)
- [Purge Configuration](https://dev.mysql.com/doc/refman/8.4/en/innodb-purge-configuration.html)
- [Optimizing InnoDB Transaction Management](https://dev.mysql.com/doc/refman/8.4/en/optimizing-innodb-transaction-management.html)
- [InnoDB INFORMATION_SCHEMA Metrics Table](https://dev.mysql.com/doc/refman/8.4/en/innodb-information-schema-metrics-table.html)

**验证边界：** 本章 Python 多版本教学模型已在编写时实际执行，所有断言通过，输出与正文一致；涉及数据库双会话、后台 Purge 进度以及 History list length 的实验是可复现步骤和预期结果，尚未在真实 MySQL 8.4.0 实例中执行；源码片段按固定版本核对，省略了不影响当前推论的分支。教学模型不等于 InnoDB 的实际版本回收算法。
