---
title: "10｜库存正在修改，其他用户能看到什么：为什么需要 MVCC？"
sidebar_position: 11
slug: /mysql/10-how-mvcc-reads-old-versions
description: 沿商品 42 的一次未提交库存扣减，从两个连接的可见性现象出发，用最小多版本实验理解 Read View，再对照 MySQL 8.4.0 的 row_search_mvcc、版本可见性判断与 Undo 旧版本构造源码。
tags: [MySQL, InnoDB, MVCC, ReadView, Undo, 源码精读]
---

# 库存正在修改，其他用户能看到什么：为什么需要 MVCC？

上一章，我们已经把一次购买拆成两个需要共同成功的操作：扣减商品库存，以及写入订单。我们知道，事务可以让这一组修改在失败时回滚；为了撤销已经发生的修改，InnoDB 需要保存必要的 Undo 信息。

现在继续使用同一个商品：

```sql
SELECT stock FROM products WHERE id = 42;
-- 初始库存：100
```

事务 A 开始购买，把库存由 `100` 扣成 `99`，但是**暂时不提交**。这时，另一个用户只是想打开商品详情页，查看库存。对应的事务 B 运行：

```sql
SELECT stock FROM products WHERE id = 42;
```

它应该得到什么结果？

- 返回 `99`？但 A 的订单可能创建失败，库存扣减还可能回滚。
- 等 A 提交之后再查询？结果安全，但热门商品上的读请求可能都被写事务拖慢。
- 立即返回 `100`？如果当前记录已经被 A 修改成了 `99`，旧值又从哪里来？

**这就是本章唯一要解决的核心问题：怎样让普通查询不必等待正在修改库存的事务，同时又不读取对方尚未提交的数据？**

我们不会先背诵 MVCC、Read View 和隔离级别的定义。先用两个连接观察现象，再为现象寻找一个最简单的实现，最后去 MySQL 8.4.0 源码中核对。

## 1. 两个会话，一次还没有提交的购买

使用专门的实验库 `mysql_source_lab`。以下准备语句会重置商品 `42` 的库存，因此**不要在生产环境执行**。

先在一个空闲的实验连接中准备数据，确保没有其他会话正修改同一商品：

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
    name = 'Keyboard', stock = 100;
```

接下来打开**两个相互独立的 MySQL 客户端连接**。为避免依赖服务器的全局隔离级别，我们在两个会话中显式选择 `REPEATABLE READ`。

### 会话 A：修改库存，先不提交

```sql
USE mysql_source_lab;
SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ;
START TRANSACTION;

UPDATE products
SET stock = stock - 1
WHERE id = 42 AND stock > 0;

SELECT stock FROM products WHERE id = 42;
```

A 自己读到的库存预期为：

```text
99
```

注意，到现在**不要执行 `COMMIT`**。

A 的 UPDATE 已经把这条记录改为 `99`，而且它能看到自己在事务中的修改。上一章所学的 Undo 信息也已经与这次事务修改有关。但对其他事务而言，这次购买还没有得到最终确认。

### 会话 B：只查询，不修改

保持 A 的事务打开，再在会话 B 执行：

```sql
USE mysql_source_lab;
SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ;
START TRANSACTION;

SELECT stock FROM products WHERE id = 42;
```

在正常的 InnoDB `REPEATABLE READ` 一致性读取下，B 预期会**直接返回 `100`**，而不是等待 A 提交后才返回 `99`。

此时两个会话看到的值不同：

| 观察者 | 操作 | 预期库存 |
|---|---|---:|
| A | 已修改、未提交，随后自己 `SELECT` | 99 |
| B | A 未提交时，普通 `SELECT` | 100 |

这不是数据库出现了两个互相矛盾的“真实库存”。它反映的是：**不同事务读取同一条记录时，允许看到不同的、符合各自可见性规则的版本。**

先记住这个现象。现在还不需要解释完整的 Read View 数据结构。

## 2. 最简单的办法：读请求也等待写锁，为什么不够？

上一章讲过，两个 UPDATE 同时修改最后一件商品时，后一个请求可能需要等待前一个请求释放冲突锁。

所以，一个非常直接的设计是：**不管读还是写，只要有人正在改商品 42，其他人就等待。**

设想用户 A 在一个较长的购买事务中：

```text
A：开始事务
A：扣减库存 100 → 99
A：写入订单、等待其他处理……
A：最后提交
```

这时有很多人只是打开商品详情页，执行普通 `SELECT`。如果它们全部等待 A，读请求的响应时间就会被写事务的持续时间牵着走。

但直接跳过等待、读取记录中的最新值也不行。

因为 A 可能随后 `ROLLBACK`。若 B 已经把 `99` 当作确认过的库存展示给其他业务，B 就读到了一个可能被撤销的结果。

于是，真正缺少的不是“再增加一种锁”，而是：

> 当当前记录正由其他事务修改时，读请求是否能看到这条记录过去某个符合可见性要求的版本？

上一章留下的 Undo 信息，这时开始出现第二个用途。

## 3. 旧值并没有凭空消失：Undo 的另一个作用

我们在第 09 章已经知道，如果一次购买要回滚，数据库必须有足够的信息把库存从 `99` 恢复为 `100`。

现在换个角度看：即使 A 暂时没有回滚，B 也可能需要“修改前的那份数据”。

因此，在写入新版本的同时，保留能够重建旧版本的信息，可以同时服务于两种不同目的：

1. **回滚**：A 放弃购买时，恢复需要撤销的修改。
2. **一致性读取**：B 的快照不能看到 A 的修改时，构造 B 应该看到的较早版本。

MySQL 8.4 官方文档明确说明：InnoDB 的 Undo 信息既用于事务回滚，也用于构造一致性读取所需的旧记录版本。

这并不意味着 InnoDB 每次 UPDATE 都把整条记录完整复制很多份。实际结构与修改的字段、Undo 类型和内部记录格式有关。对本章的 `stock` 更新，我们先用“当前版本指向历史版本”作为**教学模型**：

```text
商品 id = 42

当前记录：stock = 99     ← A 的未提交修改
              |
              | 可由 Undo 信息重建更早的值
              v
历史版本：stock = 100    ← B 可能需要的结果
```

不过，仅仅保存旧值仍然不够。

假设同一件商品先后经历了多次购买：

```text
stock = 100 → 99 → 98 → 97
```

某个读事务应该看 `100`，另一个可能应该看 `98`。我们不能简单地规定“凡是有旧值，就返回最旧的值”。

因此，我们还需要回答：**对于一个具体读事务，哪一个版本才允许被看到？**

这时才有必要引入快照中的可见性规则。

## 4. 最小实现：为每个版本记录“是谁修改的”

先假设一条商品记录只有两个版本：

```text
版本 V2：stock = 99，创建它的事务是 A
       ↓
版本 V1：stock = 100，来自更早已提交的事务
```

当事务 B 读取商品时，我们可以逐个检查：

- V2 的修改者 A 在 B 的读取视角里已经提交了吗？
- 如果没有，B 不能直接返回 V2。
- 那就沿着历史信息寻找更早的版本，直到找到 B 有权看到的版本。

这给出了一个最小可实现的读取过程。

**概念伪代码，不是 MySQL 源码：**

```python
version = latest_version_of_product(42)

while version is not None:
    if visible_to_reader(version, reader_snapshot):
        return version.stock
    version = version.previous

return None
```

先不要关心 `visible_to_reader()` 的全部规则。它至少需要知道：

- 每个版本是谁写出来的。
- 读事务建立读取视角时，哪些写事务还没有提交。
- 哪些更新发生在这个读取视角之后。
- 是否正在读取自己已经做过的修改。

于是，读事务需要保存一个能够支持上述判断的状态，我们暂且称之为**读取快照**。

它不是把数据库所有商品复制一遍，而是保存判断版本可见性所必需的信息。

接下来，我们把这一想法写成真正可运行的 Python 程序。

## 5. Python 实验：旧版本 + 快照，让 B 不必等待 A

这段实验故意不实现索引、行锁、Undo 二进制格式或真实事务调度，只验证一件事：**给定一个版本链和读取快照，读者能够跳过不可见的新版本，找到自己应该看到的旧值。**

**教学化简代码，可直接运行，非 MySQL 源码：**

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class Version:
    stock: int
    trx_id: int
    prev: "Version | None" = None


@dataclass(frozen=True)
class Snapshot:
    creator: int
    active_at_creation: frozenset[int]
    next_trx_id: int


def visible(version: Version, snapshot: Snapshot) -> bool:
    # 一个事务能够读取自己写出的版本
    if version.trx_id == snapshot.creator:
        return True

    # 本实验只模拟：快照创建时已提交的较早事务可见
    return (
        version.trx_id < snapshot.next_trx_id
        and version.trx_id not in snapshot.active_at_creation
    )


def consistent_read(head: Version, snapshot: Snapshot) -> int | None:
    current = head

    while current is not None:
        if visible(current, snapshot):
            return current.stock
        current = current.prev

    return None


original = Version(stock=100, trx_id=10)

# A（事务 21）把库存改成 99，但尚未提交
head = Version(stock=99, trx_id=21, prev=original)

# B（事务 22）建立快照时，A 仍在运行
snapshot_b = Snapshot(
    creator=22,
    active_at_creation=frozenset({21}),
    next_trx_id=23,
)

print("A 未提交，B 普通 SELECT：", consistent_read(head, snapshot_b))

# A 看自己的修改
snapshot_a = Snapshot(
    creator=21,
    active_at_creation=frozenset({21}),
    next_trx_id=23,
)
print("A 读取自己的修改：", consistent_read(head, snapshot_a))

# A 提交；B 之前的快照不因此被改写
print("A 提交后，B 的旧快照：", consistent_read(head, snapshot_b))

# B 后来取得一个新的读取快照；A 不再是活跃事务
snapshot_b_new = Snapshot(
    creator=22,
    active_at_creation=frozenset(),
    next_trx_id=23,
)
print("A 提交后，B 的新快照：", consistent_read(head, snapshot_b_new))

assert consistent_read(head, snapshot_b) == 100
assert consistent_read(head, snapshot_b_new) == 99
```

**已实际运行的输出：**

```text
A 未提交，B 普通 SELECT： 100
A 读取自己的修改： 99
A 提交后，B 的旧快照： 100
A 提交后，B 的新快照： 99
```

这个实验展示了三个很重要的行为。

第一，**A 修改了当前版本，不代表 B 必须读取这个版本**。在 B 的快照里，A 仍属于不应看见的事务，于是 B 沿着版本链找到 `100`。

第二，**A 能看到自己写出的 `99`**。一致性读取不是简单地“所有事务都读取旧值”；读者自己的修改是一个重要例外。

第三，**A 后来提交，不会回头修改 B 已经建立的旧快照**。如果 B 换成新的快照，就有机会看到 `99`。

这里特意没有模拟 MySQL 的全部规则，例如事务 ID 分配时机、视图上界与下界、历史版本具体格式、插入删除语义和隔离级别。代码里 `Version.prev` 也是直接的 Python 对象引用，**并不是** InnoDB `DB_ROLL_PTR` 的物理布局。

它只是使读者亲眼看到：为了实现不等待的旧值读取，我们需要版本和可见性判断两个部分。

下一步就去真实源码中寻找它们。

---

## 6. 到真实记录中寻找“版本从哪里来”

源码固定为：

- 仓库：[`mysql/mysql-server`](https://github.com/mysql/mysql-server)
- 标签：`mysql-8.4.0`
- 主要存储引擎：InnoDB
- 当前研究路径：通过主键访问 `products.id = 42`，普通 `SELECT` 在 `REPEATABLE READ` 下执行一致性读取

先不看 Read View 的复杂字段，只问一个问题：InnoDB 根据什么信息知道一条记录被哪个事务修改过，又怎样回到旧版本？

官方文档介绍，在聚簇索引记录中，InnoDB 使用隐藏的系统字段，包括：

- `DB_TRX_ID`：记录最后插入或更新这条记录的事务标识。
- `DB_ROLL_PTR`：指向 Undo 信息的回滚指针，可用于构造以前的记录版本。

现在，上一节 Python 模型中的两个抽象就有了真实对应物：

```text
教学模型                     InnoDB 中的对应信息

version.trx_id       ←→       DB_TRX_ID
version.prev         ←→       借助 DB_ROLL_PTR 和 Undo 重建旧版本
```

注意右边不是简单的一次内存指针解引用。InnoDB 需要读取和解释 Undo 记录，重建出符合旧版本语义的记录内容。

这里也解释了一个容易误会的地方：**MVCC 并不是 InnoDB 把每个版本都作为一条独立的、永久存在的完整表记录保存下来。** 它结合当前记录和历史 Undo 信息，在读取需要时构造合适的旧版本。

到此，我们知道“旧版本从哪里来”，还不知道“什么时候去找旧版本”。

所以接下来，需要回到实际的查询读取函数。

## 7. 普通 SELECT 到底走哪条源码路径？

前面章节已经研究过，SQL 执行过程会通过存储引擎接口查找记录。现在我们聚焦 InnoDB 这一层。

打开 [MySQL 8.4.0 `storage/innobase/row/row0sel.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/row/row0sel.cc)，定位函数：

```cpp
row_search_mvcc(...)
```

它是 InnoDB 从索引读取记录的重要入口之一。名字中虽然包含 `mvcc`，但**不能据此认为所有经过这个函数的读取都是普通快照读**：源码仍然会根据读取模式分别处理加锁读取与一致性读取。

本章先研究普通 `SELECT`。

在 `row_search_mvcc()` 中，源码会检查当前读取是否需要行锁。如果不是加锁读取，就进入一致性读取所需的处理分支。

**真实源码关键行：**

```cpp
} else if (prebuilt->select_lock_type == LOCK_NONE) {
```

这是前面其他分支之后的 `else if`；`LOCK_NONE` 在这里表示当前记录读取不要求加行锁。

接着，正常情况下调用：

```cpp
trx_assign_read_view(trx);
```

这一次调用让事务能够获得用于一致性读取的 Read View。

请注意调用关系：

```text
普通 SELECT 查找商品 42
            |
            v
       row_search_mvcc()
            |
            v
     判断是否为一致性读取
            |
            v
     trx_assign_read_view(trx)
```

前面 Python 实验里，我们通过手工创建 `Snapshot` 来模拟这一阶段。真实 MySQL 则由事务和 MVCC 管理逻辑创建或复用对应的视图。

因此，Read View 并不是 SQL 语法分析时随手创建的对象，它与**实际读取记录时需要什么可见性**直接相关。

我们已经知道调用点，现在去看看 `trx_assign_read_view()` 到底负责什么。

## 8. Read View：不复制全部数据，而是记录可见性边界

打开 [MySQL 8.4.0 `storage/innobase/trx/trx0trx.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/trx/trx0trx.cc)，找到：

```cpp
ReadView *trx_assign_read_view(trx_t *trx)
```

这个函数首先检查当前事务是否已经拥有一个仍然有效的读取视图；如果没有，就让 MVCC 管理器打开视图。

**真实源码关键行：**

```cpp
trx_sys->mvcc->view_open(trx->read_view, trx);
```

这里最重要的不是 `trx_sys` 这个名字，而是行为：

- 输入是当前事务 `trx`。
- 修改的是事务关联的 `read_view`。
- 下游读取记录时，会使用这个视图检查版本是否可见。

如果想知道视图建立时保存了什么，可以继续打开 [MySQL 8.4.0 `storage/innobase/read/read0read.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/read/read0read.cc)，定位 `ReadView::prepare()`。

它会记录创建者事务的标识、建立视图时的事务 ID 边界，并保留相关活跃读写事务的集合。

其中有一行：

**真实源码关键行：**

```cpp
m_low_limit_id = trx_sys_get_next_trx_id_or_no();
```

这提供了判断哪些事务标识属于快照边界之外的信息。源码还会整理活跃事务 ID 集合和另一个可见性边界。

这时，我们终于可以给 Read View 一个严格服务于当前问题的最小定义：

> **Read View 是读事务判断“哪些事务产生的记录版本对我可见”所使用的一组边界和活跃事务信息。它不是所有商品数据的副本。**

还记得 Python 模型中：

```python
Snapshot(
    creator=22,
    active_at_creation=frozenset({21}),
    next_trx_id=23,
)
```

它的三个字段只是把这个意思教学化简。真实 InnoDB 的 `ReadView` 使用更精细的上下界和活跃事务集合，并需要处理并发状态、事务自身修改等情况。

但问题又来了：有了这些信息，究竟怎样判断记录 `stock = 99` 对 B 不可见？

## 9. 源码怎样判断“我能不能看到这个版本”？

打开 [MySQL 8.4.0 `storage/innobase/include/read0types.h`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/include/read0types.h)，找到：

```cpp
ReadView::changes_visible(...)
```

这个函数接收某个记录版本对应的事务 ID，根据当前视图决定这次事务修改是否可见。

为了不一次堆出整个函数，我们先看两个最容易理解的边界。

**真实源码节选，保留一个判定分支：**

```cpp
if (id < m_up_limit_id || id == m_creator_trx_id) {
  return (true);
}
```

这里可以直接看到两个条件。

如果版本对应的事务早于可见性的下侧边界，这个版本可以被视图看到。

如果修改记录的就是创建视图的事务自身，也可以看到。

后一个条件，正好解释了我们在双会话实验中看到的现象：

```text
A 修改为 99，A 自己 SELECT → 99
```

但是，仅有这两个条件不够。如果某个事务 ID 位于快照的中间范围内，可能是快照建立时仍然活跃的事务，也可能已经提交。

源码于是还会使用 `m_ids` 中的活跃事务 ID 进行判断。

**真实源码关键行：**

```cpp
return (!std::binary_search(p, p + m_ids.size(), id));
```

如果该 ID 出现在相关活跃事务列表中，它的修改对当前视图不可见。

如果不在其中，且已经通过前面的边界条件检查，才可能被认定可见。

此时，我们已经可以解释 B 看不到 `99` 的第一层原因：

```text
当前商品版本：stock = 99
             修改它的事务：A
                    |
                    v
         B 的 Read View 判断
                    |
             A 的版本不可见
```

不过，**判断当前版本不可见，并不等于已经找到能返回的正确结果。**

B 还想要一条库存记录。它不能仅仅因为最新版本不可见，就回答“商品不存在”。

因此，需要再走一步：利用 Undo 信息去找能够被 B 看见的旧版本。

## 10. 当前版本不可见时，怎样返回库存 100？

回到 [MySQL 8.4.0 `storage/innobase/row/row0sel.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/row/row0sel.cc)，继续阅读 `row_search_mvcc()` 的普通一致性读取分支。

当它在聚簇索引记录中发现当前版本不符合读取视图时，会进入构造旧版本的路径。

**真实源码调用名：**

```cpp
row_sel_build_prev_vers_for_mysql(...)
```

这个辅助函数接收读取视图、聚簇索引、当前记录以及保存结果所需的上下文。它继续调用：

```cpp
row_vers_build_for_consistent_read(...)
```

后一个函数位于 [MySQL 8.4.0 `storage/innobase/row/row0vers.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/row/row0vers.cc)。

从名字就可以看到，程序已经进入“为一致性读取构造记录版本”的实际实现。

但我们不能只停留在函数名称。进入这个函数后，会发现它确实会循环寻找以前的记录版本，并对每个候选版本检查是否可以被当前视图看到。

核心动作是：

1. 根据记录和 Undo 信息尝试构造前一版本。
2. 如果前一版本不存在，处理在当前快照中不存在这条记录的情况。
3. 如果前一版本存在，用当前 Read View 判断它是否可见。
4. 可见则复制这一版本作为读取结果；不可见则继续寻找更早版本。

与这些动作相对应的真实源码包含：

**真实源码关键行：**

```cpp
if (view->changes_visible(trx_id, index->table->name)) {
```

这里真正发生了我们在 Python 模型中写过的动作：

```python
if visible(version, snapshot):
    return version.stock
```

只是实际 InnoDB 不会直接返回 Python 的 `stock` 整数，而是重建并返回适合存储引擎读取接口消费的记录版本。

现在可以把整个过程拼起来了：

```text
B 执行普通 SELECT：查询商品 42
                 |
                 v
          定位聚簇索引记录
                 |
                 v
          当前版本 stock=99
                 |
                 v
        Read View：A 不可见
                 |
                 v
        根据 Undo 构造前一版本
                 |
                 v
          旧版本 stock=100
                 |
                 v
          Read View：可见
                 |
                 v
              返回 100
```

这张图是**对当前场景的教学化简**，不是完整的函数调用栈。它忽略了页面 latch、异常处理、删除标记、二级索引路径和复杂记录格式，但已经能解释本章最初的现象。

现在我们明白：B 并不是“从 A 手里抢到了未提交数据”，而是根据自己的视图找到一份符合条件的旧记录。

这就是本章需要理解的多版本并发控制（MVCC）的核心作用。

---

## 11. A 已经提交，为什么 B 第二次还可能看到 100？

回想前面 Python 实验的最后两行：

```text
A 提交后，B 的旧快照：100
A 提交后，B 的新快照：99
```

这里留下了一个非常自然的问题。

如果 A 已经提交，那么 `99` 不再是未提交数据。B 为什么不能立刻看到 `99`？

因为**版本是否可见，不只与“现在 A 是否已提交”有关，还与 B 当前使用的是哪一个读取视图有关**。

MySQL 提供不同的事务隔离级别，它们在普通一致性读取时获得快照的时机可以不同。

先只研究与当前问题最相关的两种。

### 11.1 REPEATABLE READ：同一个事务复用已建立的读取视图

在 InnoDB 默认的 `REPEATABLE READ` 下，事务第一次进行普通一致性读取时建立快照，后面的普通一致性读取继续使用这一视图。

于是可以出现：

```text
A                         B（REPEATABLE READ）

开始事务
UPDATE 库存 100→99
                          开始事务
                          SELECT → 100
                          （建立读取视图）
COMMIT
                          SELECT → 100
                          （继续用原来的读取视图）
                          COMMIT
                          SELECT → 99
                          （新事务/新视图）
```

在这个顺序中，**A 的提交并不会把 B 已经建立的视图自动改成新视图**。

所以 B 的两次普通 `SELECT` 可以保持同样的库存结果。

注意一个容易写错的细节：并不是只要执行 `START TRANSACTION`，后续所有普通 SELECT 的快照就一定固定在那一刻。对这里讨论的常规 `REPEATABLE READ` 一致性读取，关键时刻通常是**第一次一致性读取建立视图时**。`START TRANSACTION WITH CONSISTENT SNAPSHOT` 则属于需要单独考虑的显式快照开始方式。

### 11.2 READ COMMITTED：下一条普通 SELECT 可以拿到新视图

现在只改变 B 的隔离级别，业务仍然是购买商品 42。

在 `READ COMMITTED` 下，每条普通一致性读取通常获取自己的新快照。

因此，B 的表现可能变成：

```text
A                         B（READ COMMITTED）

开始事务
UPDATE 库存 100→99
                          开始事务
                          SELECT → 100
COMMIT
                          SELECT → 99
                          （新的语句视图）
```

区别并不在于 A 是否允许回滚，而是 B 的两条读取语句使用的可见性视角是否仍相同。

用一个小表概括：

| 场景 | B 第一次 SELECT | A 提交后 B 再 SELECT |
|---|---:|---:|
| REPEATABLE READ，同一事务、此前已建立快照 | 100 | 100 |
| READ COMMITTED，同一事务 | 100 | 99 |

这张表限定在普通一致性 `SELECT`，并且假设没有其他事务再修改同一商品。

因此，隔离级别不是“给 SELECT 设置一个缓存时间”。它影响的是读取视图的建立和使用规则。

## 12. 必须区分：普通 SELECT 和 UPDATE 不是同一种读取

现在回到我们全系列的核心 SQL：

```sql
UPDATE products
SET stock = stock - 1
WHERE id = 42 AND stock > 0;
```

如果普通 SELECT 可以为了避免等待而读旧版本，那么我们是否也应该让 UPDATE 按同一个旧版本扣库存？

答案是：**不能直接这么推断。**

考虑商品只剩一件的情况。

A 正在把库存 `1` 修改成 `0`，暂时不提交。B 如果用旧版本 `1` 直接决定并执行另一次扣减，就可能错误地把旧状态当成可以安全修改的当前状态。

上一章我们已经看到，冲突更新需要数据库协调锁与条件检查。

这说明：

- `SELECT stock FROM products WHERE id = 42`：在本章讨论的隔离级别下，通常是**普通一致性读取**，可以使用旧版本。
- `SELECT ... FOR UPDATE`：是**加锁读取**，其目的之一是为后续可能发生的修改协调访问。
- `UPDATE ... WHERE ...`：是数据修改操作，不能理解为“读一个任意快照，然后无条件写回”。

这三类操作可能最终都会经过 InnoDB 的记录读取代码，但具体加锁方式、版本可见性要求和访问行为不同。

在源码里，前面看到的 `row_search_mvcc()` 对此也有区分：当 `select_lock_type` 不是 `LOCK_NONE` 时，会进入相关加锁处理路径；当普通一致性读取不要求行锁时，才使用 Read View 判断是否需要获取历史版本。

**因此 MVCC 没有代替行锁，行锁也没有使旧版本读取失去意义。**

它们在不同的读取和修改需求中协同工作。

这一点对库存业务尤其重要：

> 商品详情页可以接受按隔离规则得到的快照库存，但真正的购买成功必须以更新操作的实际结果为依据，而不能只依赖用户此前看到的一个库存数值。

## 13. 动手验证：两种隔离级别下，B 第二次能看到什么？

下面给出完整的双会话实验。请使用**两个独立连接**，按提示的顺序交替执行。不要将 A、B 的 SQL 全部连续粘贴到同一个会话里。

### 实验 A：REPEATABLE READ 下，旧视图继续看到 100

先在没有未完成实验事务的连接中重置库存：

```sql
USE mysql_source_lab;
UPDATE products SET stock = 100 WHERE id = 42;
COMMIT;
```

会话 A：

```sql
USE mysql_source_lab;
SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ;
START TRANSACTION;
UPDATE products SET stock = stock - 1
WHERE id = 42 AND stock > 0;
-- 暂停：不要提交
```

会话 B：

```sql
USE mysql_source_lab;
SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ;
START TRANSACTION;
SELECT stock FROM products WHERE id = 42;
-- 预期：100
```

返回会话 A：

```sql
COMMIT;
```

继续会话 B：

```sql
SELECT stock FROM products WHERE id = 42;
-- 预期：100（仍在原事务中）

COMMIT;

SELECT stock FROM products WHERE id = 42;
-- 预期：99（新的自动提交读取）
```

如果 B 的第二次查询仍然是 `100`，就能直观看到：**旧的读取视图并不会随着别人的提交自动更新。**

### 实验 B：READ COMMITTED 下，下一次查询读取新快照

完成上一实验并退出两个事务后，重置商品库存：

```sql
USE mysql_source_lab;
UPDATE products SET stock = 100 WHERE id = 42;
COMMIT;
```

会话 A：

```sql
SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ;
START TRANSACTION;
UPDATE products SET stock = stock - 1
WHERE id = 42 AND stock > 0;
-- 暂停：暂不提交
```

会话 B：

```sql
SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED;
START TRANSACTION;
SELECT stock FROM products WHERE id = 42;
-- 预期：100
```

返回会话 A：

```sql
COMMIT;
```

继续会话 B：

```sql
SELECT stock FROM products WHERE id = 42;
-- 预期：99
COMMIT;
```

对比实验 A 和 B，我们只改变了 B 的隔离级别，没有改变 UPDATE 的业务逻辑。

同一条普通 `SELECT`，因为读取视图的生命周期不同，就可能在第二次查询时返回不同结果。

### 实验 C：普通 SELECT 与加锁读取行为不同

这个实验需要第三次重置库存：

```sql
UPDATE products SET stock = 100 WHERE id = 42;
COMMIT;
```

会话 A 再次开始未提交更新：

```sql
START TRANSACTION;
UPDATE products SET stock = stock - 1
WHERE id = 42 AND stock > 0;
-- 暂不提交
```

会话 B 先使用普通 SELECT：

```sql
START TRANSACTION;
SELECT stock FROM products WHERE id = 42;
-- 预期：100，普通一致性读取
```

然后尝试：

```sql
SELECT stock FROM products WHERE id = 42 FOR UPDATE;
-- 预期：需要等待 A 释放冲突锁
```

这条语句可能阻塞。此时回到会话 A 执行：

```sql
COMMIT;
```

B 的加锁读取随后可以继续，正常情况下预期取得更新后的 `99`。最后在 B 执行：

```sql
ROLLBACK;
```

如果想避免为了教学实验一直等待，可以在 B 测试前适当设置会话级 `innodb_lock_wait_timeout`，或者用 `NOWAIT` 研究立即返回锁冲突错误的行为；但这些属于下一步实验策略，不是 MVCC 自动处理加锁读取的证据。

### 实验的真实性边界

本章 **Python 多版本教学实验已在当前环境运行**，上文四行输出来自实际执行，断言通过。

本章 **MySQL 双会话 SQL 未在这里连接真实的 MySQL 8.4.0 实例执行**。因此，本节只提供官方行为语义支持的预期结果和可复现步骤，不把预期表格当作实测日志。不同的全局配置、意外未提交事务以及并发干扰会影响你的实际实验；实验前需要确保会话状态清晰。

上文源码分析是对固定版本的静态核对，不代表已经对 C++ 调用路径做过逐步调试。

---

## 14. 回看：本章机制解决了什么，没有解决什么？

现在我们可以完整解释开头的现象。

商品 `42` 的库存最初是 `100`。

A 发起购买，执行 UPDATE，把当前记录修改为 `99`，但没有提交。

B 运行普通 SELECT，不需要为了读取库存而取得与 A 冲突的行锁。它持有一个用于判断可见性的 Read View。

当 InnoDB 读取到当前聚簇索引记录时，发现这个 `99` 是由 B 暂时看不见的事务修改出来的。于是，系统根据 Undo 信息重建更早的记录版本，并检查它是否对 B 可见。

在我们的例子里，B 最终读到 `100`。

所以，本章的执行路径可以归纳为：

```text
A：UPDATE 商品 42，库存 100 → 99（未提交）
                    |
                    v
          当前记录含有新库存
          Undo 可用于重建旧版本
                    |
                    v
B：普通 SELECT 商品 42
                    |
                    v
      Read View 判断当前版本不可见
                    |
                    v
          从历史信息构造旧版本
                    |
                    v
                  返回 100
```

这套机制的重要收益，是在很多普通读取场景下，读事务不需要因为冲突写事务尚未提交就等待它。

但要避免把 MVCC 神化：

- **它不是所有 SELECT 都不加锁。** `FOR UPDATE`、`FOR SHARE` 和某些特殊语句有不同的读取要求。
- **它不负责保证库存扣减不会超卖。** 竞争写入仍需正确使用条件更新与数据库的并发控制。
- **它不是把数据库复制一份给每个事务。** 版本可见性和历史重建基于事务信息与 Undo。
- **它不是零成本的。** 为了满足仍在运行的旧快照，部分历史 Undo 不能过早清理；长时间不结束的读事务可能延长历史版本的保留时间。
- **它也不等于故障恢复。** B 能读取旧版本，并不意味着 A 提交后的新库存已经具备我们要求的崩溃持久性。

最后一点，是下一章将面对的问题。

## 15. 下一章：用户已经购买成功，数据库突然断电怎么办？

到现在，我们已经研究了同一次购买的两个关键要求。

第一，多个用户同时扣减商品 `42` 时，冲突修改必须被正确协调。

第二，有人正在修改库存时，其他用户的普通查询可以利用旧版本与 Read View 得到符合隔离规则的结果。

但还有一个问题，我们之前已经留下了很久。

第 07 章知道，UPDATE 修改的数据页可能首先留在 Buffer Pool 中，并不立即写回磁盘。

假设商品库存从 `100` 扣成 `99`，事务已经提交，MySQL 也向客户端确认成功，但数据页还没有被刷回磁盘。

此时数据库突然断电。

内存里的 `99` 消失了，磁盘数据页可能还停留在旧状态。

**数据库该凭什么保证这次已经确认的购买能够在重启后恢复？**

注意，这不是 MVCC 进一步演化出来的问题，而是同一笔购买对故障恢复提出的另一项独立要求。

下一章，我们会重新回到“内存中的记录已被修改、数据页尚未落盘”的时刻，先尝试最简单的持久化策略，让它遇到明确的失败或性能限制，然后再研究 InnoDB 的 Redo Log。

**下一章：第 11 章《提交成功之后突然断电——为什么需要 Redo Log？》**

---

## 源码索引与参考资料

以下源码均固定在 **MySQL 8.4.0，Git Tag `mysql-8.4.0`**。链接指向固定版本，不使用持续变化的 `trunk` 分支。

| 文件 | 本章关注的真实入口 | 作用 |
|---|---|---|
| `storage/innobase/row/row0sel.cc` | `row_search_mvcc()` | 区分一致性读取与加锁读取；判断聚簇索引记录是否对视图可见 |
| `storage/innobase/row/row0sel.cc` | `row_sel_build_prev_vers_for_mysql()` | 需要时进入旧版本构造 |
| `storage/innobase/trx/trx0trx.cc` | `trx_assign_read_view()` | 为当前事务取得或复用 Read View |
| `storage/innobase/read/read0read.cc` | `ReadView::prepare()`、`MVCC::view_open()` | 准备视图状态并管理视图生命周期 |
| `storage/innobase/include/read0types.h` | `ReadView::changes_visible()` | 判断版本对应事务的修改是否对视图可见 |
| `storage/innobase/row/row0vers.cc` | `row_vers_build_for_consistent_read()` | 沿 Undo 历史寻找一致性读取可见的版本 |

**固定版本源码：**

- [row0sel.cc](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/row/row0sel.cc)
- [trx0trx.cc](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/trx/trx0trx.cc)
- [read0read.cc](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/read/read0read.cc)
- [read0types.h](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/include/read0types.h)
- [row0vers.cc](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/row/row0vers.cc)

**官方行为说明：**

- [InnoDB Multi-Versioning](https://dev.mysql.com/doc/refman/8.4/en/innodb-multi-versioning.html)
- [Consistent Nonlocking Reads](https://dev.mysql.com/doc/refman/8.4/en/innodb-consistent-read.html)
- [Locking Reads](https://dev.mysql.com/doc/refman/8.4/en/innodb-locking-reads.html)

本章有意不展开二级索引下的版本判断、Undo 记录的二进制布局、Purge 清理算法以及多种异常读场景。那些问题值得研究，但不是解释“B 为什么能在 A 未提交时读到 100”所必需的前置知识。
