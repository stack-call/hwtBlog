---
title: "26｜从正常查询到表结构变更：为什么 ALTER TABLE 会被长事务阻塞？"
sidebar_position: 27
slug: /mysql/26-why-alter-table-waits-for-mdl
description: 通过三个 MySQL 会话复现 SELECT、ALTER TABLE 与后续 SELECT 的等待链，沿 MySQL 8.4.0 源码理解元数据锁的申请、冲突与释放。
tags: [MySQL, 源码精读, MDL, ALTER TABLE, 元数据锁]
---

# 从正常查询到表结构变更：为什么 ALTER TABLE 会被长事务阻塞？

上一章，我们研究了长事务如何让历史版本暂时无法被 Purge 清理。

那个问题发生在**数据内容**这一侧：一个事务仍有可能读取过去的库存版本，系统就不能贸然清理相应历史。

现在，业务提出另一个要求。商品表除了 `id`、`name`、`stock`，还需要记录分类：

```sql
ALTER TABLE products ADD COLUMN category VARCHAR(50);
```

这次我们不改变商品 42 的库存，而是改变存放库存的**表结构**。

这看上去比扣减库存简单：给表增加一个字段而已。然而在有用户持续查询和下单的系统中，它可能长时间不返回。更意外的是，`ALTER TABLE` 开始等待以后，原本毫不冲突的新 `SELECT` 也可能排队。

这一章只回答一个中心问题：

> **为什么正在运行的 SQL 可以暂时阻止表结构变化，而等待中的表结构变化又可能阻止后续 SQL？**

我们先复现这个现象，再看看 MySQL 8.4.0 怎样实现相应的协调机制。本章不展开 `INSTANT`、`INPLACE`、`COPY` 的工作原理，那是下一章的问题。

## 1. 先观察一个反直觉现象

继续沿用商品 42，不过为了不破坏前面章节的实验数据，这次使用专用表：

```sql
CREATE DATABASE IF NOT EXISTS mysql_source_lab;
USE mysql_source_lab;

DROP TABLE IF EXISTS products_mdl;
CREATE TABLE products_mdl (
    id BIGINT PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    stock INT NOT NULL
) ENGINE = InnoDB;

INSERT INTO products_mdl (id, name, stock)
VALUES (42, 'Keyboard', 99);
```

`DROP TABLE` 仅应在专门的练习库中执行，**不要对生产表运行本章的阻塞实验**。

我们准备三个相互独立的 MySQL 客户端连接，称为会话 A、B、C。

会话 A 开启事务，然后读取商品 42：

```sql
-- 会话 A
USE mysql_source_lab;
START TRANSACTION;

SELECT id, name, stock
FROM products_mdl
WHERE id = 42;
```

预期返回一行：`42 / Keyboard / 99`。

注意：**先不要提交或回滚会话 A。** 让这个连接保持打开。

接下来在会话 B 修改表结构：

```sql
-- 会话 B
USE mysql_source_lab;
SET SESSION lock_wait_timeout = 120;

ALTER TABLE products_mdl
ADD COLUMN category VARCHAR(50);
```

在会话 A 尚未结束事务时，会话 B 通常会停在等待状态，而不会立即返回。

这里很奇怪：会话 A 只是执行了一条普通 `SELECT`，没有更新库存，也没有显式 `FOR UPDATE`。为什么还能妨碍增加字段？

我们先不要急着回答，再打开会话 C：

```sql
-- 会话 C
USE mysql_source_lab;
SET SESSION lock_wait_timeout = 120;

SELECT id, stock
FROM products_mdl
WHERE id = 42;
```

在典型的默认 MDL 调度下，这条新 `SELECT` **也可能等待**。

此时发生的事情可以概括为：

```text
时间向下

会话 A                 会话 B                    会话 C
START TRANSACTION
SELECT products_mdl
    |                      |                         |
保持事务不结束           ALTER TABLE                 |
    |                   等待表结构操作条件           |
    |                      |                    SELECT products_mdl
    |                      |                       也开始等待
    |                      |                         |
COMMIT                    |                         |
    |                   ALTER 继续执行               |
    |                   ALTER 完成                    |
    |                      |                    SELECT 得到结果
```

注意这不是说 A 的读取与 C 的读取本身相互冲突。恰恰相反：**两个普通查询本来可以并发。** 新的等待来自排在中间的 DDL 请求。

要解释这个现象，先考虑如果完全不协调表结构修改，会有什么风险。

## 2. 如果允许表结构随时改变，会出什么问题？

回忆第 19 章：执行器需要知道表有哪些字段，才能从记录中读取 `stock` 并把结果返回客户端。

假设执行一条查询时，MySQL 已经根据当前表定义准备好了要读取的列：

```text
products_mdl
  id
  name
  stock
```

查询进行到一半，另一个会话却突然完成了表结构替换。旧语句手中的表定义，与其他线程刚刚发布的新定义，可能不再一致。

我们需要的并不是让所有读操作都串行执行，而是一个更小的约束：

> 使用一张表的过程中，当前操作所依赖的表定义必须保持足够稳定；改变表定义的操作，要与正在使用该定义的操作协调。

一种最简单的教学方案是设置“读表定义”和“改表定义”两类许可：

- 读取或更新表数据的语句，可以共同持有兼容的表定义使用许可。
- 修改表定义的语句，在必须独占表定义时，要等相关使用者释放许可。

把这种许可称为锁，就是 **Metadata Lock（MDL，元数据锁）**。

这里的“元数据”主要是表名、字段、索引等数据库对象的定义信息，不是商品 42 的 `stock=99` 这个行数据值。

因此，MDL 与之前学过的 InnoDB 行锁不是同一件事：

| 对比 | InnoDB 行锁 | MySQL 元数据锁（MDL） |
|---|---|---|
| 当前关心的对象 | 被访问或修改的索引记录/范围 | 表等数据库对象的定义与使用关系 |
| 典型冲突 | 两个事务竞争更新同一条商品记录 | 长事务持续使用表定义，而 DDL 需要改变表定义 |
| 普通 `SELECT` | 通常使用 MVCC 一致性读取，不必为记录申请排他行锁 | 访问表时仍需适当的 MDL |
| 实现位置 | 主要由 InnoDB 管理 | 主要由 MySQL Server 的 MDL 子系统管理 |

这一步不能混淆：**“普通 SELECT 不加记录级排他锁”不等于“普通 SELECT 不会持有任何锁”。**

现在去真实源码核对。

## 3. 源码中的第一张许可：普通 SELECT 为什么要申请 MDL？

本章固定源码：

- 仓库：`mysql/mysql-server`
- Tag：`mysql-8.4.0`
- 主要文件：`sql/mdl.h`、`sql/sql_base.cc`、`sql/mdl.cc`、`sql/sql_table.cc`

先打开 [`sql/mdl.h`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/mdl.h)。

这里定义了 `enum_mdl_type`。针对当前问题，先只看两种：

```cpp
// 真实源码节选：sql/mdl.h，enum_mdl_type
MDL_SHARED_READ,
MDL_SHARED_WRITE,
```

源码注释将 `MDL_SHARED_READ` 用于读取表数据的语句，例如普通 `SELECT`；将 `MDL_SHARED_WRITE` 用于准备修改数据的语句，例如 `INSERT`、`UPDATE`、`DELETE`，以及 `SELECT ... FOR UPDATE`。

这两种类型**并不表示 InnoDB 的 S 行锁和 X 行锁**。它们属于另一套面向数据库对象的许可体系。

我们要研究的普通查询：

```sql
SELECT stock FROM products_mdl WHERE id = 42;
```

在打开目标表的过程中，需要取得与读取操作匹配的 MDL。

打开 [`sql/sql_base.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/sql_base.cc)，定位 `open_table_get_mdl_lock()`。函数处理打开表时的 MDL 申请；在一般表打开路径中，可以看到：

```cpp
// 真实源码节选：sql/sql_base.cc，open_table_get_mdl_lock()
bool result =
    thd->mdl_context.acquire_lock(mdl_request, ot_ctx->get_timeout());
```

这段代码中：

- **调用者**：打开表的上层流程，已经根据 SQL 建立了相应的表引用和 MDL 请求。
- **输入**：`mdl_request` 描述目标对象、申请类型以及锁的持续范围；`ot_ctx->get_timeout()` 给出此次等待的超时约束。
- **动作**：交给当前连接的 `MDL_context` 尝试取得锁。
- **输出/副作用**：成功时，申请关联到已授予的锁票据；冲突时可能等待；失败时返回错误。

这里仍然没有读取任何商品记录。**在准备访问商品表时，Server 就需要先取得正确使用其表定义的许可。**

于是，第一次产生了两层不同的锁问题：为了读行数据而存在的 InnoDB 并发规则，以及为了稳定表定义而存在的 Server MDL 规则。我们已经能够解释为什么普通 `SELECT` 也会出现在 MDL 体系中。

但为什么会话 A 的 `SELECT` 明明已经返回数据，却还继续阻止会话 B？

## 4. SELECT 已经结束，为什么 MDL 还没释放？

在会话 A 中：

```sql
START TRANSACTION;
SELECT stock FROM products_mdl WHERE id = 42;
-- 暂时不 COMMIT
```

对于这类普通事务级表访问，MySQL 通常会让相应 MDL 持续到**事务结束**，而不是单条 `SELECT` 返回的时刻。

这个规则的必要性是什么？

事务可以跨多条 SQL。如果一条事务中的前一条语句引用了某张表，而另一会话在事务尚未结束时改变表结构，事务后续操作就可能面临表定义改变的问题。为了让事务期间相关对象的使用保持一致，服务器需要协调这种变化。

请看 [`sql/mdl.h`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/mdl.h) 中锁的持续范围：

```cpp
// 真实源码节选：sql/mdl.h
enum enum_mdl_duration {
  MDL_STATEMENT = 0,
  MDL_TRANSACTION,
  MDL_EXPLICIT,
  MDL_DURATION_END
};
```

这些名字表示不同的**释放边界**，而不是不同的表锁强度。

我们在本章主要关心的是 `MDL_TRANSACTION`：事务结束前保持对应的锁。

打开 [`sql/mdl.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/mdl.cc)，定位释放函数：

```cpp
// 真实源码节选：sql/mdl.cc，MDL_context::release_transactional_locks()
void MDL_context::release_transactional_locks() {
  DBUG_TRACE;
  release_locks_stored_before(MDL_STATEMENT, nullptr);
  release_locks_stored_before(MDL_TRANSACTION, nullptr);
}
```

源码注释明确指出，这个函数用于事务 `COMMIT` 或 `ROLLBACK` 结束时清理普通语句取得的相应元数据锁。注意它不是释放所有 `MDL_EXPLICIT` 锁的通用函数。

现在就能解释 A 的行为了：

```text
会话 A：START TRANSACTION
          |
          v
        SELECT
          |
          v
      获得读取表的 MDL
          |
          v
    SELECT 返回结果
          |
          | 事务尚未结束，仍保留事务级 MDL
          v
        COMMIT
          |
          v
    释放相应的事务级 MDL
```

这也解释了一个常见的排查误区：**“这条 SELECT 执行只花了 1 毫秒”，并不能说明它只占用了 1 毫秒的 MDL。**

如果会话进入 `Sleep`，但事务没有结束，那么它仍可能持有表的事务级 MDL。

相反，如果使用默认 `autocommit=1`，且没有显式开启多语句事务，单条 `SELECT` 结束也通常意味着该事务结束，相应事务级 MDL 可以释放。

我们已经解释了 A 为什么迟迟不释放。接下来解释 B 为什么需要等待。

## 5. ALTER TABLE 为什么需要更强的许可？

会话 B 请求：

```sql
ALTER TABLE products_mdl ADD COLUMN category VARCHAR(50);
```

这次操作不只是读取已有表结构，而是要修改它。

因此，除了让 SQL 知道怎样找到 `products_mdl`，执行到改变表定义的阶段时，还必须保证没有其他会话同时持有与该阶段不兼容的元数据锁。

在 `sql/mdl.h` 中，有一个类型：

```cpp
// 真实源码节选：sql/mdl.h
MDL_EXCLUSIVE,
```

它代表独占元数据锁。这个类型用于某些必须独占数据库对象定义的 DDL 阶段。

但我们不能说“整个 ALTER TABLE 从开始到结束都必定持有 X 锁”。不同 DDL 方式和阶段的并发能力并不一样。

为了回答本章的问题，只需要找到一个真实的**升级为独占 MDL** 的入口。

打开 [`sql/sql_base.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/sql_base.cc)，找到 `wait_while_table_is_used()`：

```cpp
// 真实源码节选：sql/sql_base.cc，wait_while_table_is_used()
if (thd->mdl_context.upgrade_shared_lock(table->mdl_ticket, MDL_EXCLUSIVE,
                                         thd->variables.lock_wait_timeout))
  return true;
```

源码中的该函数会在取得独占许可后执行表缓存相关处理，以便让旧表实例不再被其他线程继续使用。本章暂时只需要看这次升级。

**是谁调用它？**

在 [`sql/sql_table.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/sql_table.cc) 的 `ALTER TABLE` 相关路径中，可以看到：

```cpp
// 真实源码节选：sql/sql_table.cc，in-place ALTER 提交前的相关路径
// Upgrade to EXCLUSIVE before commit.
if (wait_while_table_is_used(thd, table, HA_EXTRA_PREPARE_FOR_RENAME))
  goto rollback;
```

这个调用说明：至少在这里展示的 in-place ALTER 提交流程中，在发布变更前仍有需要升级为独占锁的阶段。

但此时会话 A 还持有读取表的 MDL。二者不兼容，B 的升级无法立即成功，所以它必须等。

同样值得注意：`upgrade_shared_lock()` 并不是“无条件把锁类型改成 EXCLUSIVE”；它仍然要遵守冲突检查和等待规则。

于是 B 的等待就解释清楚了。

但我们的第三个会话 C 为什么也会停下来？它明明只想读取库存，不想修改表结构。

## 6. 为什么排队的 ALTER 还能挡住后续 SELECT？

回到时间线：

1. A 持有读取表的 MDL。
2. B 申请独占 MDL，因 A 尚未释放而等待。
3. C 又申请读取表的 MDL。

如果只看 A 和 C，二者的普通读取许可是兼容的。

但如果服务器在有独占请求排队时，仍无限制地允许新的读取请求插队，那么业务查询只要不断到来，B 可能一直得不到机会修改表结构。

所以，锁调度不能仅检查**已经授予**的锁，还要考虑**等待队列中优先级更高的请求**。

打开 `sql/mdl.cc`，查看 `MDL_lock::can_grant_lock()` 的真实注释：

> New lock request can be satisfied iff ...

它概括了两个必要条件：没有来自其他上下文的、不兼容的已授予锁；也没有优先级更高的等待请求阻挡这次申请。

下面是对应的关键实现片段：

```cpp
// 真实源码节选：sql/mdl.cc，MDL_lock::can_grant_lock()
const bitmap_t waiting_incompat_map =
    incompatible_waiting_types_bitmap()[type_arg];
const bitmap_t granted_incompat_map =
    incompatible_granted_types_bitmap()[type_arg];
```

随后的代码会结合 `m_waiting`、已授予的锁及相关快速路径状态，判断当前申请能否获得许可。

从设计上看，MySQL 需要同时考虑：

```text
当前已经持有的锁
         +
等待队列中更高优先级的锁请求
         |
         v
当前新请求能否立即获得锁？
```

对于我们构造的典型场景，B 的独占请求已经进入等待队列，C 后到的普通读请求就可能不能直接越过 B。

从外部看，这会形成一条等待链：

```text
A：正在使用表定义，事务还没结束
         |
         v
B：需要独占表定义，等待 A
         |
         v
C：原本能并发读取，但在 B 后面排队
```

这里有一个重要边界：**这是一种等待队列和优先级导致的典型行为，而不是“任何情况下后来 SELECT 都必然阻塞”的绝对规则。** 实际授予还受锁类型、当前队列、优先级与配置影响，例如 `max_write_lock_count`。

现在我们可以在不研究任何 InnoDB 行锁的情况下，解释这个现象了：

- A 的事务不结束，读取表的事务级 MDL 还在。
- B 要修改表定义，需要在相关阶段取得独占 MDL。
- C 的读请求虽然与 A 兼容，却可能因为 B 的待处理独占请求而排队。

下一步，我们把这个简化成一个可以亲眼看见的实验。

## 7. 最小可运行实验：一个三会话 MDL 调度模型

以下实验**只模拟一张表的共享/独占许可和等待队列**，不模拟真实 MySQL 的完整兼容矩阵、锁升级、死锁检测或线程调度。

但它足以验证本章最反直觉的那部分关系。

```python
from collections import deque


class TinyMDL:
    """只模拟同一张表的 S/X 授予和等待队列；不是 MySQL MDL 实现。"""

    def __init__(self):
        self.granted = []
        self.waiting = deque()

    def request(self, name, mode):
        incompatible = (mode == "X" and bool(self.granted)) or (
            mode == "S" and any(m == "X" for _, m in self.granted)
        )
        higher_priority_waiter = mode == "S" and any(
            m == "X" for _, m in self.waiting
        )

        if incompatible or higher_priority_waiter:
            self.waiting.append((name, mode))
            print(f"{name}: WAIT {mode}")
            return False

        self.granted.append((name, mode))
        print(f"{name}: GRANTED {mode}")
        return True

    def release(self, name):
        self.granted = [
            (n, mode) for n, mode in self.granted if n != name
        ]
        print(f"{name}: RELEASE")

        while self.waiting:
            candidate, mode = self.waiting[0]
            incompatible = (mode == "X" and bool(self.granted)) or (
                mode == "S" and any(m == "X" for _, m in self.granted)
            )
            if incompatible:
                break
            self.waiting.popleft()
            self.granted.append((candidate, mode))
            print(f"{candidate}: GRANTED {mode}")


mdl = TinyMDL()
assert mdl.request("A SELECT", "S")
assert not mdl.request("B ALTER", "X")
assert not mdl.request("C SELECT", "S")
assert len(mdl.waiting) == 2

mdl.release("A SELECT")
assert mdl.granted == [("B ALTER", "X")]

mdl.release("B ALTER")
assert mdl.granted == [("C SELECT", "S")]
print("所有断言通过")
```

**已实际运行的输出：**

```text
A SELECT: GRANTED S
B ALTER: WAIT X
C SELECT: WAIT S
A SELECT: RELEASE
B ALTER: GRANTED X
B ALTER: RELEASE
C SELECT: GRANTED S
所有断言通过
```

实验最重要的结论不是“共享锁和独占锁不能同时出现”——这一点非常容易理解。

真正容易忽略的是：

**即使 C 想要的共享许可与 A 已有的共享许可兼容，只要一个需要独占的 B 已在等待，C 也可能不得不等。**

这就解释了为什么线上一次尚未完成的 `ALTER TABLE`，有时会让后续请求大量积压。

现在还有最后一个问题：在真实 MySQL 上，怎样知道谁正在持有 MDL，谁正在等待？

## 8. 真实 MySQL 实验：找到持有者与等待者

重新按第 1 节执行 A、B、C 三个会话，确保 B 和 C 尚在等待。

再打开**第四个独立会话 D**，执行：

```sql
SELECT
    ml.OBJECT_SCHEMA,
    ml.OBJECT_NAME,
    ml.LOCK_TYPE,
    ml.LOCK_DURATION,
    ml.LOCK_STATUS,
    t.PROCESSLIST_ID AS connection_id,
    t.PROCESSLIST_INFO AS statement
FROM performance_schema.metadata_locks AS ml
LEFT JOIN performance_schema.threads AS t
    ON t.THREAD_ID = ml.OWNER_THREAD_ID
WHERE ml.OBJECT_TYPE = 'TABLE'
  AND ml.OBJECT_SCHEMA = 'mysql_source_lab'
  AND ml.OBJECT_NAME = 'products_mdl'
ORDER BY ml.LOCK_STATUS, t.PROCESSLIST_ID;
```

我们希望重点观察两种状态：

- `GRANTED`：已经授予的锁。
- `PENDING`：正在等待授予的锁。

结合 `LOCK_TYPE` 和 `connection_id`，可以进一步判断哪个连接持有了读取表的许可，以及哪个连接正在申请更强的许可。

典型情况下，会看到 A 对应的 `SHARED_READ / GRANTED`，B 的 `EXCLUSIVE / PENDING`，以及 C 的 `SHARED_READ / PENDING`。**这只是预期模式，不是实际抓取的运行输出**；而且 DDL 可能同时持有其他类型的 MDL，具体记录数量和顺序以实际观察为准。

如果查不到任何行，请先确认：

1. 连接确实访问的是 `mysql_source_lab.products_mdl`。
2. A 的事务还没有结束；B、C 也仍在等待。
3. Performance Schema 的 `wait/lock/metadata/sql/mdl` 插桩已经开启（MySQL 8.4 通常默认启用），且当前账号有读取相关视图的权限。

还可以查看：

```sql
SHOW FULL PROCESSLIST;
```

在等待阶段，某些连接的状态可能显示为：

```text
Waiting for table metadata lock
```

**不要把这一状态与 `performance_schema.data_lock_waits` 混为一谈。** 后者主要用于观察存储引擎数据锁等待；本章重点应该看 `performance_schema.metadata_locks`。

### 释放锁，确认等待链怎样结束

切回会话 A：

```sql
COMMIT;
```

然后观察：

1. B 是否从等待中恢复并完成 `ALTER TABLE`。
2. C 是否在 B 完成后返回商品 42 的查询结果。
3. D 再次读取 `metadata_locks`，原来的等待记录是否消失。

最后确认列是否存在：

```sql
SHOW COLUMNS FROM mysql_source_lab.products_mdl;
```

预期出现 `category` 列。

如果 B 因 `lock_wait_timeout` 超时而退出，就不能声称观察到了完整的“等待 → 获得锁 → ALTER 完成”路径。需要重置实验后再次验证。

**实验清理**：一旦 B、C 长时间等待，请优先结束会话 A 的事务；不要随意关闭线上数据库实例。教学库需要复原时，可以在确认没有其他实验会话使用它以后删除 `products_mdl`。

## 9. 重新沿着真实源码走一遍

现在，我们已经拥有三类证据：

- 从业务场景推导出的需求：执行 SQL 时表定义应保持稳定。
- 实验现象：长事务阻止 DDL，等待中的 DDL 又可能阻止后续查询。
- 真实源码：申请、冲突判断、等待、升级与释放均有明确入口。

把它们放回执行过程：

```text
会话 A：SELECT
  |
  v
打开表，申请读取类型 MDL
  |  sql/sql_base.cc
  |  open_table_get_mdl_lock()
  v
MDL_context::acquire_lock()
  |
  v
获得许可，执行查询
  |
  v
查询返回，但显式事务尚未结束
  |
  +------------------------------------+
                                       |
会话 B：ALTER TABLE                    |
  |                                    |
  v                                    |
DDL 的相关阶段申请升级为独占 MDL        |
  |  wait_while_table_is_used()        |
  v                                    |
不能立即授予，进入等待                  |
  |                                    |
  +-----------+                        |
              |                        |
会话 C：SELECT                          |
  |           |                        |
  v           |                        |
需要读取类型 MDL                        |
  |           |                        |
  v           v                        |
兼容性与等待优先级检查                  |
  |                                    |
  v                                    |
可能也进入等待                          |
                                       |
会话 A：COMMIT  <-----------------------+
  |
  v
release_transactional_locks()
  |
  v
B 有机会取得独占许可并完成 DDL
  |
  v
B 释放相应 MDL
  |
  v
C 获得读取许可，继续 SELECT
```

这张图是**按三个会话交织整理的控制与等待关系图**，并不是一条线程中的函数调用栈。

我们还可以从这里认识一个非常重要的设计边界：

**MDL 解决的是元数据并发安全，不是记录版本可见性，更不是更新同一库存行时的并发原子性。**

我们在第 08 章分析行锁、第 10 和第 22 章分析 MVCC，它们与本章的 MDL 各司其职。

## 10. 为什么这对实际系统非常重要？

假设一台 MySQL 正在处理商品查询。

某个连接因为业务代码忘记提交，留下了一个时间很长的事务。这个连接已经不再运行 SQL，但它曾访问过 `products` 表，因此仍可能保留事务级 MDL。

开发人员随后执行：

```sql
ALTER TABLE products ADD COLUMN category VARCHAR(50);
```

DDL 开始等待。

与此同时，新的商品查询持续到来。由于等待中的 DDL 可能阻挡后续共享 MDL 请求，原本毫秒级完成的查询开始积压。

这时，问题看起来就像“数据库突然变慢了”，但根源未必是慢 SQL、索引失效或磁盘 I/O。

它可能只是一条迟迟没有结束的事务，加上一个正在等待的 DDL。

处理这类问题时，应该先确认等待关系：

- 是否存在未结束、已访问目标表的事务？
- 哪个连接申请了 `EXCLUSIVE` 或正在升级 MDL？
- 新查询是否因为排队而等待？
- 改变表结构是否真的需要在当前业务高峰执行？

不要一上来就杀掉所有查询，更不要把它当成 InnoDB 行锁问题。先从 `performance_schema.metadata_locks` 还原现场，通常能更快找到原因。

此外，执行 `ALTER TABLE` 时可以设置合适的 `lock_wait_timeout`，避免 DDL 无限制地排队；但超时只能控制等待时间，并不能消除锁冲突的根源。

## 11. 回看：我们为什么终于需要 MDL？

这章仍然没有脱离商品系统。

我们只是给商品增加一个 `category` 字段，就遇到了新的并发约束。

最初的想法是：既然新字段不影响当前 `SELECT stock` 的业务结果，应该可以直接修改表结构。

但数据库必须保证已经开始使用表定义的语句和事务，不会遭遇未经协调的定义变化。

于是，我们需要一种不同于 InnoDB 行锁的对象级协调手段：MDL。

在正常查询阶段，Server 通过表打开路径申请适当的共享 MDL；在 DDL 必须独占对象定义的阶段，申请更强的 MDL；事务结束时，再释放相应的事务级 MDL。

最终，我们理解了最开始的反直觉现象：

**一个已经执行完 SELECT、却没有结束事务的连接，仍可能阻塞 DDL；而等待中的 DDL，还可能让后来的 SELECT 一起等待。**

这也让我们自然走向下一章。

## 12. 下一章：有 MDL 就意味着 DDL 必须全程阻塞读写吗？

本章说明了为什么表结构变化必须与正在使用表的事务协调。

但如果一张商品表拥有几千万行，增加一个字段可能需要很长时间。

难道这整个过程都必须持续阻止用户购买商品？

更具体地说：

- 一次表结构修改，是否一定需要复制全部数据？
- 能否在部分阶段仍允许商品查询和库存更新？
- 如果中间阶段允许并发，最终发布新表定义时又怎样保证安全？

这就是下一章的中心问题。

**第 27 章：从阻塞式修改到 Online DDL——MySQL 怎样减少业务中断？**

---

## 源码定位与参考资料

本章源码均针对 **MySQL 8.4.0，Tag `mysql-8.4.0`**。源码片段仅保留支撑当前论证的关键行；省略错误处理、统计、调试等分支，并不意味着这些分支在真实运行中不存在。

| 源码位置 | 用于回答的问题 |
|---|---|
| [`sql/mdl.h`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/mdl.h) | 共享/独占 MDL、持续范围分别是什么？ |
| [`sql/sql_base.cc`：`open_table_get_mdl_lock()`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/sql_base.cc#L2471) | 普通表打开过程怎样申请 MDL？ |
| [`sql/sql_base.cc`：`wait_while_table_is_used()`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/sql_base.cc#L2347) | DDL 怎样在需要时升级为独占许可？ |
| [`sql/sql_table.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/sql_table.cc) | ALTER TABLE 的相关阶段怎样使用上面的升级操作？ |
| [`sql/mdl.cc`：`MDL_lock::can_grant_lock()`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/mdl.cc#L2204) | 为什么除了已授予锁，还要看等待队列？ |
| [`sql/mdl.cc`：`MDL_context::acquire_lock()`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/mdl.cc#L3088) | 冲突时怎样转入等待？ |
| [`sql/mdl.cc`：`release_transactional_locks()`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/mdl.cc#L4144) | 为什么事务结束后 ALTER 才能继续？ |

官方手册：

- [MySQL 8.4：Metadata Locking](https://dev.mysql.com/doc/refman/8.4/en/metadata-locking.html)
- [MySQL 8.4：Performance Schema metadata_locks](https://dev.mysql.com/doc/refman/8.4/en/performance-schema-metadata-locks-table.html)
- [MySQL 8.4：ALTER TABLE](https://dev.mysql.com/doc/refman/8.4/en/alter-table.html)

**实验状态：** 第 7 节的 Python 简化调度模型已实际运行并通过断言；第 1、8 节的 MySQL 多会话 SQL 为可复现实验方案，尚未在真实 MySQL 实例上执行。本文没有虚构 MySQL 实测的等待时长或 `metadata_locks` 输出。
