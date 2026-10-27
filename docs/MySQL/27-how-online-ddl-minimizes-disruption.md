---
title: "27｜从阻塞式修改到 Online DDL：怎样尽量减少业务中断？"
sidebar_position: 28
slug: /mysql/27-how-online-ddl-minimizes-disruption
description: 从商品表加列的真实需求出发，逐步理解 COPY、INPLACE、INSTANT 的不同代价，沿 MySQL 8.4.0 源码追踪算法协商、并发变更合并与元数据提交。
tags: [MySQL, InnoDB, 源码精读, Online DDL, MDL]
---

# 从阻塞式修改到 Online DDL：怎样尽量减少业务中断？

上一章，我们遇到了一个看似奇怪的现象。

商品表需要增加一个 `category` 字段：

```sql
ALTER TABLE products
ADD COLUMN category VARCHAR(50);
```

但是，一个查询已经结束、事务却没有提交的连接，可能让这条 `ALTER TABLE` 一直等待。更令人意外的是，一条正在等待的 DDL，还可能让后面新到的查询排队。

我们沿着元数据锁（MDL）的源码，知道了原因：**读取表和修改表结构，必须协调各自对表定义的使用。**

现在，我们已经理解“为什么要等”。但实际项目还有一个无法回避的问题：

> 如果商品表有几千万条记录，而网站仍在不断下单，我们是否只能停下业务，等整张表修改完成？

这一次，我们不更换业务场景。商品 42 仍然在 `products` 表中，购买操作仍然是：

```sql
UPDATE products
SET stock = stock - 1
WHERE id = 42 AND stock > 0;
```

唯一的新需求是：**在尽量不影响上述购买操作的前提下，为商品增加分类字段。**

要做到这一点，首先必须弄清楚：为表增加一个字段，数据库到底需要改动多少东西？

## 1. 最直观的办法：复制一张新表

我们先不用 MySQL，想象自己维护着这样一张记录表：

```text
id  name      stock
42  Keyboard  100
43  Mouse      20
```

现在，要给每条记录增加 `category`，默认值为 `general`。

最容易想到的实现是：创建一张新结构的表，把每条旧记录读出来，补充分类，再写入新表。

```text
旧表                        新表
id name     stock           id name     stock category
42 Keyboard 100     ──→    42 Keyboard 100   general
43 Mouse     20     ──→    43 Mouse     20   general
```

如果只有两条记录，这个方案没有问题。

但假设有一千万条记录，我们就要处理一千万条记录。工作量不再只取决于“增加一个字段”这几个字，还取决于原表的大小。

更严重的是：复制过程还没结束时，另一个用户购买了商品 42。

旧表的库存从 `100` 变为 `99`，而我们已经复制到新表的那条记录，仍然是 `100`。

```text
时间 T1：复制商品 42 → 新表 stock = 100
时间 T2：用户下单    → 旧表 stock =  99
时间 T3：切换到新表  → 新表 stock = 100  （错误）
```

于是，一个原本只需要改变表结构的操作，竟然可能把业务数据改回旧值。

最简单的修正办法，是在复制期间禁止写入旧表。这样能保持新旧数据的一致性，但用户的库存扣减就必须等待。

这正是我们在讨论 Online DDL 之前，需要看清的第一组矛盾：

**复制记录容易理解，也容易保证一致性；但表越大，复制越久，写入等待就可能越严重。**

MySQL 确实提供这样一条实现路线。在 `ALTER TABLE` 中，可以显式要求 `ALGORITHM=COPY`：

```sql
ALTER TABLE products
ADD COLUMN category VARCHAR(50) DEFAULT 'general',
ALGORITHM=COPY,
LOCK=SHARED;
```

这里 `LOCK=SHARED` 的含义是：在该操作支持的情况下，允许并发读，但不允许并发写。它不表示整个 DDL 过程完全没有锁，也不保证所有 `ALTER TABLE` 都能使用同样的组合。

这是一种可工作的方案，但还不够适合持续有购买请求的商品系统。

我们真正缺少的能力是：**改变表结构时，允许业务继续修改数据，并且最终不要丢掉这些修改。**

## 2. 能不能边整理数据，边接受新的购买？

回到刚才的时间线。

复制商品 42 时，它的库存是 `100`。

复制尚未结束，用户下单，旧表变成 `99`。

问题的根源，不在于“复制”这个动作本身，而在于复制过程中发生了新的修改，新结构没有同步获得它们。

那么，我们能不能先完成大部分数据整理，同时把期间发生的变更记录下来？

例如：

```text
T1：开始构建新结构，读到商品 42：stock = 100
T2：用户购买，旧表 stock = 99
T3：额外记录一项变更：商品 42 的 stock 更新为 99
T4：完成主要构建工作
T5：将期间记录的变更应用到新结构
T6：在必要的协调与切换之后，启用新结构
```

此时，用户在 T2 的购买没有被丢弃。

这种设计引出了一个新的机制：**在线变更期间的增量修改记录与合并**。

注意，我们只是建立直觉。真实 MySQL 并不是在任意时间把两个普通表无锁地替换，也不是所有 `INPLACE` 操作都需要相同的增量日志。哪些 DDL 可以并发写、是否重建表、如何合并变更，都要由实际的操作类型和存储引擎能力决定。

不过，至少我们已经有理由研究另一个选项：

```sql
ALTER TABLE products
ADD COLUMN category VARCHAR(50) DEFAULT 'general',
ALGORITHM=INPLACE,
LOCK=NONE;
```

这里的两个要求需要分开理解。

- `ALGORITHM=INPLACE`：要求使用存储引擎支持的原地 DDL 路线，而不是 Server 层通用的 `COPY` 路线。**INPLACE 不保证完全不重建数据。**
- `LOCK=NONE`：要求该操作在主要执行过程中允许并发读取和写入。它**不保证 DDL 从开始到结束完全不需要 MDL**。

以 MySQL 8.4 中符合条件的 InnoDB 表为例，添加普通列使用 `INPLACE` 仍然会重建表，却可以在支持的条件下允许并发 DML。这恰好说明：**是否需要重建数据，与是否允许并发写入，是两条不同的设计维度。**

但这引出了第二个问题：如果只是新增一个有默认值的字段，我们真的必须把旧记录全部重新写一遍吗？

## 3. 更小的一步：不修改旧记录，也能看到新字段吗？

观察我们的实际需求：

```sql
ALTER TABLE products
ADD COLUMN category VARCHAR(50)
NOT NULL DEFAULT 'general';
```

对于既有商品 42，我们没有要求对它的 `id`、`name`、`stock` 做任何修改。

只是希望查询时多看到一个值：

```text
修改前：42, Keyboard, 100
修改后：42, Keyboard, 100, general
```

如果数据库知道新列的定义和默认值，是否可以暂时保持旧记录的物理内容不变，在读取旧记录时按新表结构补出缺失的列？

这个思路不需要扫描并重写所有旧记录。

它将主要工作从“修改每条数据”转移为“更新表结构及其元数据”。

对于符合条件的加列操作，MySQL 8.4 的 InnoDB 支持：

```sql
ALTER TABLE products
ADD COLUMN category VARCHAR(50)
NOT NULL DEFAULT 'general',
ALGORITHM=INSTANT;
```

这就是 **Instant DDL**。

此时不应得出“数据库以后永远不需要处理新列的存储”这样的结论。已有记录和以后写入的记录可能属于不同的行版本，InnoDB 需要使用相应元数据正确解释它们；后续的重建也可能把数据整理成新的物理布局。

但对于当前这个需求，最重要的收益很明确：

**符合条件时，不必为了增加一列而遍历并重写整张表。**

于是，三种思路就从同一个问题中逐步产生了：

```text
需要增加商品分类列
         |
         v
复制整张表，可以得到新结构
         |
         +── 表太大，复制期间写入怎么办？
         |
         v
在支持的操作中允许构建与业务写入并行
         |
         +── 但如果只是补一个默认字段，还需要重建吗？
         |
         v
只改变元数据，让旧记录按新表结构被解释
```

在 MySQL 术语中，它们分别对应 `COPY`、某些 `INPLACE` 操作以及 `INSTANT`。这不是所有 DDL 的严格快慢排名，而是**针对“给当前商品表增加一列”这个具体问题的方案演化**。

接下来我们需要核实：MySQL 真的是这样选择这些方案的吗？

---

## 4. 先进入 Server：谁来决定使用哪种算法？

本章源码固定为：

- 仓库：[`mysql/mysql-server`](https://github.com/mysql/mysql-server)
- Tag：[`mysql-8.4.0`](https://github.com/mysql/mysql-server/tree/mysql-8.4.0)
- Server 主要文件：[`sql/sql_table.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/sql_table.cc)
- InnoDB 主要文件：[`storage/innobase/handler/handler0alter.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/handler/handler0alter.cc)

我们已经在前几章认识了 MySQL Server 与存储引擎的分工。

现在，Server 收到 `ALTER TABLE`，知道了“为 products 增加 category 列”的需求。

但并不是所有存储引擎、所有表、所有类型的 DDL 都支持同一种算法。Server 不能仅凭“ADD COLUMN”几个字就假定一定能够瞬时完成。

因此，**它必须询问真正管理数据的存储引擎：这次修改你能够怎样做？**

在 `sql/sql_table.cc` 的 `mysql_alter_table()` 中，可以找到下面的关键调用。

**真实源码节选，MySQL 8.4.0（省略周边代码）：**

```cpp
// Ask storage engine whether to use copy or in-place
enum_alter_inplace_result inplace_supported =
    table->file->check_if_supported_inplace_alter(altered_table,
                                                  &ha_alter_info);
```

这几行代码回答了三个问题。

**谁调用？** Server 的 `mysql_alter_table()`，在分析这次表结构变更后调用。

**输入是什么？** `altered_table` 表示目标表的新定义；`ha_alter_info` 描述本次 ALTER 涉及的变更和用户要求。

**输出是什么？** 一个能力判断结果，告诉 Server 存储引擎支持哪类处理方式、对锁有什么要求，或根本不支持当前选择。

注意，这一步只是**协商能力**，还没有开始复制商品 42 的数据。

如果用户显式要求 `INSTANT`，而存储引擎表示不支持，Server 会拒绝请求。紧接着的代码是：

**真实源码节选，`sql/sql_table.cc`：**

```cpp
if (alter_info->requested_algorithm ==
        Alter_info::ALTER_TABLE_ALGORITHM_INSTANT &&
    inplace_supported != HA_ALTER_INPLACE_INSTANT &&
    inplace_supported != HA_ALTER_ERROR) {
  ha_alter_info.report_unsupported_error("ALGORITHM=INSTANT",
                                         "ALGORITHM=COPY/INPLACE");
```

为什么要允许用户明确指定算法？

因为在生产环境中，“为了执行成功，悄悄改为复制整张大表”可能不是我们愿意承担的代价。

显式指定 `ALGORITHM=INSTANT`，可以让不满足条件的操作直接报错，而不是在我们没有意识到的情况下采用更昂贵的方案。

类似地，`LOCK=NONE` 能表达“不接受长期阻止并发 DML 的执行方案”，如果存储引擎无法满足，就应该返回错误，而不是忽略要求。

这也说明：`ALGORITHM` 与 `LOCK` 并不是随意附加的性能提示，而是在约束 DDL 的执行方式。

## 5. InnoDB 怎样判断这个加列可以 INSTANT？

Server 已经询问存储引擎。

现在沿着调用进入：

`storage/innobase/handler/handler0alter.cc`

找到：

```cpp
ha_innobase::check_if_supported_inplace_alter(...)
```

虽然函数名带有 `inplace`，它也参与判断符合条件的 `INSTANT` 操作。

在该函数中，先看到：

**真实源码节选，MySQL 8.4.0：**

```cpp
Instant_Type instant_type = innobase_support_instant(
    ha_alter_info, m_prebuilt->table, this->table, altered_table);
```

这与上一节的设计形成对应：

Server 判断是否可以进入存储引擎自己的 DDL 路线；InnoDB 再根据实际的表和变更内容检查能否瞬时完成。

`innobase_support_instant()` 并不是只检查 SQL 里是否写了 `ADD COLUMN`。

它要辨别变更类型，也要考虑原表的存储条件。对于新增列，其中会检查表是否支持 instant add/drop。

**真实源码节选，`innobase_support_instant()`：**

```cpp
case INSTANT_OPERATION::INSTANT_ADD:
  if (table->support_instant_add_drop()) {
    return (Instant_Type::INSTANT_ADD_DROP_COLUMN);
  }
  break;
```

这段代码的语义非常清晰：只有满足表级条件，才把操作归类为可瞬时增加或删除列。

接下来，InnoDB 还会检查列数限制、行大小与行版本限制等条件。

当全部检查满足时，可以返回：

**真实源码节选，`ha_innobase::check_if_supported_inplace_alter()`：**

```cpp
ha_alter_info->handler_trivial_ctx = instant_type_to_int(instant_type);
return HA_ALTER_INPLACE_INSTANT;
```

为什么不对所有 ADD COLUMN 都返回 `INSTANT`？

因为 Instant 依赖一定的存储格式与元数据表达能力。它不只是“写一个新字段名”那么简单。

例如，MySQL 8.4 文档明确说明：对 `ROW_FORMAT=COMPRESSED` 表、带有 `FULLTEXT` 索引的表等，瞬时加列存在限制；瞬时加列积累的行版本也有上限。

**机制的边界是设计的一部分。** 不支持时可以根据操作与用户要求选择其他算法，或者直接报错，但不能把“不需要重建”想象成适用于全部表结构变更的普遍规律。

## 6. INSTANT 为什么不必重写商品 42？

现在存储引擎已经决定：这次操作可以走 `INSTANT`。

我们要追踪的是：此前存在的商品记录怎么办？

在 `handler0alter.cc` 中，`ha_innobase::commit_inplace_alter_table()` 有如下分支。

**真实源码节选，MySQL 8.4.0：**

```cpp
if (is_instant(ha_alter_info)) {
  Instant_ddl_impl<dd::Table> executor(
      ha_alter_info, m_user_thd, m_prebuilt->trx, m_prebuilt->table, table,
      altered_table, old_dd_tab, new_dd_tab,
      altered_table->found_next_number_field != nullptr
          ? &m_prebuilt->table->autoinc
          : nullptr);
  /* Execute Instant DDL */
  if (executor.commit_instant_ddl()) return true;
}
```

这里有一项值得注意的设计：`INSTANT` 并没有转入“复制每条旧记录”的循环，而是进入专门的 Instant DDL 元数据提交实现。

`Instant_ddl_impl` 的实现位于：

[`storage/innobase/dict/dict0inst.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/dict/dict0inst.cc)

它负责处理相应的瞬时表结构变更元数据。

这里不需要把每个数据字典字段都展开。我们只需要理解这个结果：**既有记录的物理内容不必立刻补上新的 `category` 列，而表的新定义已经知道这列及其默认值。**

通过读取时对行版本和列元数据的解释，旧记录可以在新表定义下被正确呈现。

但这种做法也有代价：不同时间创建的记录可能需要通过不同的行版本信息解释。MySQL 8.4 可以通过 `INFORMATION_SCHEMA.INNODB_TABLES.TOTAL_ROW_VERSIONS` 观察瞬时加列或删列累积的行版本数。

这一节，我们终于回答了一个关键问题：

> 为什么在某些 ADD COLUMN 场景中，数据量很大也不需要扫描和重写整张表？

不是因为 MySQL 能够无限快地重写所有记录，而是因为它**在这个场景里避免了那项工作**。

但还有一些表结构变化，确实不能只修改元数据。

接下来，看看无法避免重建时，MySQL 能否仍让购买请求继续进行。

---

## 7. INPLACE 不等于“不重建”：在线重建怎样处理新增购买？

假设这次表结构修改确实需要整理数据。

此时，InnoDB 如果支持在线执行，就可能允许构建过程与业务写入在一段时间内并行。

我们前面已经发现这里必须解决的数据一致性问题：

```text
构建开始时：商品 42 的 stock = 100
构建进行中：用户购买，stock = 99
构建结束时：新结构不能还是 stock = 100
```

MySQL 的在线重建路径中，确实存在应用构建期间变更日志的动作。

在同一个 `handler0alter.cc` 中，可以找到：

**真实源码节选，MySQL 8.4.0：**

```cpp
if (err == DB_SUCCESS && ctx->online && ctx->need_rebuild()) {
  DEBUG_SYNC_C("row_log_table_apply1_before");
  err = row_log_table_apply(ctx->thr, m_prebuilt->table, altered_table,
                            ctx->m_stage);
}
```

这里的判断有两个重要条件：

- `ctx->online`：对应当前操作的在线执行状态。
- `ctx->need_rebuild()`：这次 DDL 需要重建相关数据结构。

只有条件满足，才进入这段重建期间变更记录的应用逻辑。

源码的含义并不是“所有 INPLACE 都调用 `row_log_table_apply()`”。例如创建普通二级索引的在线增量维护有不同的内部路径；我们这里关注的是**在线重建表**这一类操作。

更进一步，在重建收尾阶段，还能找到应用剩余日志的逻辑：

```cpp
error = row_log_table_apply(
    ctx->thr, user_table, altered_table,
    static_cast<ha_innobase_inplace_ctx *>(ha_alter_info->handler_ctx)
        ->m_stage);
```

为什么不只复制一次就结束？

因为构建期间可能有新的 INSERT、UPDATE、DELETE。对于允许并发 DML 的重建操作，必须协调这些变化，才能使最终启用的结构与业务已发生的修改保持一致。

注意：这个**在线 DDL 临时修改日志**并不是我们第 11 章的 Redo Log，也不是第 13 章的 Binlog。

三者目的不同：

- Redo 主要服务于 InnoDB 崩溃恢复。
- Binlog 记录可用于复制和恢复等用途的数据库变更事件。
- 在线 DDL 日志帮助某些正在构建的新数据结构吸收构建期间发生的并发修改。

不要因为它们都叫“日志”就混成同一个东西。

同时，在线 DDL 日志的空间也不是无限的。对于受 `innodb_online_alter_log_max_size` 限制的操作，如果并发写入过多、临时日志超出上限，DDL 可能失败。它不是一种完全没有资源代价的方案。

## 8. 最容易误解的一点：`LOCK=NONE` 为什么仍然可能等待？

现在，我们已经有了一个支持并发 DML 的设计。

既然写入可以与构建工作并行，为什么上一章的长事务仍然可能阻塞 `ALTER TABLE`？

因为这里涉及两种不同的事情：

**能否在主要构建阶段接受并发 DML**，与**修改表定义时是否需要协调其他连接正在使用的旧定义**，不是同一个问题。

让我们回到第 26 章。

事务 A 查询了商品 42，但始终没有提交。

事务 B 要给商品表增加 `category`。

即使事务 B 的操作满足 `ALGORITHM=INSTANT`，也不能任由事务 A 持有旧表定义相关的锁时，直接绕过 MDL 协调。

我们再进入 `sql/sql_table.cc`，看 InnoDB 的原地 ALTER 主流程。

**真实源码节选，MySQL 8.4.0，`mysql_inplace_alter_table()`：**

```cpp
if (table->file->ha_prepare_inplace_alter_table(
        altered_table, ha_alter_info, table_def, altered_table_def)) {
  goto rollback;
}
```

准备完成后，再由相应路径调用：

```cpp
if (table->file->ha_inplace_alter_table(altered_table, ha_alter_info,
                                        table_def, altered_table_def)) {
  goto rollback;
}
```

到提交之前，又可以找到：

```cpp
// Upgrade to EXCLUSIVE before commit.
if (wait_while_table_is_used(thd, table, HA_EXTRA_PREPARE_FOR_RENAME))
  goto rollback;
```

接下来才调用：

```cpp
if (table->file->ha_commit_inplace_alter_table(
        altered_table, ha_alter_info, true, table_def, altered_table_def)) {
  goto rollback;
}
```

上述代码来自同一函数的关键位置，之间省略了条件判断与其他处理，不能理解为所有 DDL 都经过完全相同的锁变化过程。

但有一个事实已经非常明确：**Online DDL 仍然包含为发布新表定义而进行的锁协调和提交阶段。**

所以：

- `INSTANT` 的重点是减少或避免修改已有行数据，并不等于不拿 MDL。
- `INPLACE, LOCK=NONE` 主要约束允许的并发访问方式，并不等于整个 ALTER 生命周期没有短暂的排他阶段。
- 长事务没有释放相关 MDL 时，即使真正的变更工作很短，也可能等待很久。

这不是 Online DDL 的自相矛盾。

因为它解决的是**缩短或避开大规模阻塞性工作**，不是让表结构可以在任何时刻无条件改变。

## 9. 通过一个实际可运行的最小模型验证差异

为了直观观察前面三个方案，我们先不启动 MySQL，而是运行一个独立的 Python 实验。

这个模型只验证三种设计思路，并不模拟 InnoDB 的锁协议、物理页面或真实数据字典。

```python
from copy import deepcopy

# 旧表：商品 42 的库存是 100
original = {42: {"name": "Keyboard", "stock": 100}}

# 方案一：简单复制
snapshot = deepcopy(original)
original[42]["stock"] = 99  # 复制期间，用户购买
assert snapshot[42]["stock"] == 100
assert original[42]["stock"] == 99
print("COPY 快照:", snapshot[42]["stock"], "线上表:", original[42]["stock"])

# 方案二：构建新结构时，记录并重放并发变更
live = {42: {"name": "Keyboard", "stock": 100}}
new_layout = deepcopy(live)
changes = [
    ("update", 42, {"stock": 99}),
    ("insert", 43, {"name": "Mouse", "stock": 20}),
]

for kind, row_id, payload in changes:
    if kind == "update":
        live[row_id].update(payload)
    elif kind == "insert":
        live[row_id] = dict(payload)

for kind, row_id, payload in changes:
    if kind == "update":
        new_layout[row_id].update(payload)
    elif kind == "insert":
        new_layout[row_id] = dict(payload)

assert new_layout == live
print("ONLINE 合并:", new_layout)

# 方案三：旧记录物理内容不变，读取时根据元数据补新列
stored = {42: {"name": "Keyboard", "stock": 99}}
metadata_defaults = {"category": "general"}

def select_row(row_id):
    row = dict(stored[row_id])
    for column, default in metadata_defaults.items():
        row.setdefault(column, default)
    return row

assert "category" not in stored[42]
assert select_row(42)["category"] == "general"
print("INSTANT 原记录:", stored[42])
print("INSTANT 读到的记录:", select_row(42))
```

**本章已实际运行上述 Python 实验，输出如下：**

```text
COPY 快照: 100 线上表: 99
ONLINE 合并: {42: {'name': 'Keyboard', 'stock': 99}, 43: {'name': 'Mouse', 'stock': 20}}
INSTANT 原记录: {'name': 'Keyboard', 'stock': 99}
INSTANT 读到的记录: {'name': 'Keyboard', 'stock': 99, 'category': 'general'}
```

三个实验各自证明一件事：

第一，简单复制如果不协调新的业务修改，可能得到过期结果，因此传统复制通常需要限制并发写入。

第二，在受控条件下记录并应用构建期间的修改，可以让新旧结构最终保持一致。但真实 InnoDB 还必须处理并发顺序、异常、锁和磁盘持久化，这个模型没有实现这些保证。

第三，如果目标变更允许用元数据解释旧记录，那么旧记录可以不立即被重写，也能在新表定义下呈现新增列。

我们现在得到的不只是“COPY 慢、INSTANT 快”的记忆口诀，而是知道**各方案分别省掉了什么工作，又必须处理什么新的约束**。

---

## 10. 真实 MySQL 实验：同一张商品表，分别使用三种算法

下面的实验面向 MySQL 8.4 社区版、InnoDB、普通非分区测试表。

为了不影响前面章节的 `products` 表，我们使用三个独立副本。**请只在专用实验数据库中运行，不要把删除表的 SQL 直接用于生产。**

### 10.1 准备三张具有相同初始数据的表

```sql
CREATE DATABASE IF NOT EXISTS mysql_source_lab;
USE mysql_source_lab;

DROP TABLE IF EXISTS products_ddl_instant;
DROP TABLE IF EXISTS products_ddl_inplace;
DROP TABLE IF EXISTS products_ddl_copy;
DROP TABLE IF EXISTS products_ddl_base;

CREATE TABLE products_ddl_base (
    id BIGINT PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    stock INT NOT NULL
) ENGINE=InnoDB;

INSERT INTO products_ddl_base (id, name, stock) VALUES
(42, 'Keyboard', 100),
(43, 'Mouse', 20);

CREATE TABLE products_ddl_instant LIKE products_ddl_base;
CREATE TABLE products_ddl_inplace LIKE products_ddl_base;
CREATE TABLE products_ddl_copy LIKE products_ddl_base;

INSERT INTO products_ddl_instant SELECT * FROM products_ddl_base;
INSERT INTO products_ddl_inplace SELECT * FROM products_ddl_base;
INSERT INTO products_ddl_copy SELECT * FROM products_ddl_base;
```

这几张表都只有两条记录，因此不适合用耗时判断大表性能。我们只验证：**不同算法是否能够执行，以及它们在语义和元数据上的区别。**

### 10.2 实验 A：`ALGORITHM=COPY`

```sql
ALTER TABLE products_ddl_copy
ADD COLUMN category VARCHAR(50) NOT NULL DEFAULT 'general',
ALGORITHM=COPY,
LOCK=SHARED;

SELECT id, stock, category
FROM products_ddl_copy
ORDER BY id;
```

预期可以读到：

```text
42  100  general
43   20  general
```

该实验显式要求 Server 层的 `COPY` 路线。`LOCK=SHARED` 允许适用操作中的并发读取，但不允许并发写入。这里的两行数据无法体现大表复制成本，真正的对照重点是它的执行算法选择。

### 10.3 实验 B：`ALGORITHM=INPLACE`

```sql
ALTER TABLE products_ddl_inplace
ADD COLUMN category VARCHAR(50) NOT NULL DEFAULT 'general',
ALGORITHM=INPLACE,
LOCK=NONE;

SELECT id, stock, category
FROM products_ddl_inplace
ORDER BY id;
```

对符合条件的 InnoDB 表，在 MySQL 8.4 中这种加列可以使用 `INPLACE`，并且会重建表的数据结构。`LOCK=NONE` 表示允许并发 DML 的要求，但仍可能存在短暂的 MDL 协调阶段。

**不要因为它叫 INPLACE，就据此断言没有重建。** 这是最值得保留的一条认识。

### 10.4 实验 C：`ALGORITHM=INSTANT`

```sql
ALTER TABLE products_ddl_instant
ADD COLUMN category VARCHAR(50) NOT NULL DEFAULT 'general',
ALGORITHM=INSTANT;

SELECT id, stock, category
FROM products_ddl_instant
ORDER BY id;
```

预期返回的业务结果与前两种方案相同。

但是，对于满足 Instant 条件的 InnoDB 表，它不必遍历并重写已有商品记录。

还可以观察版本信息：

```sql
SELECT NAME, TOTAL_ROW_VERSIONS
FROM INFORMATION_SCHEMA.INNODB_TABLES
WHERE NAME = 'mysql_source_lab/products_ddl_instant';
```

对于这个全新创建、随后完成一次瞬时加列的实验表，预期 `TOTAL_ROW_VERSIONS` 从初始的 `0` 增为 `1`。

需要明确：这个值不是“历史行数”，也不是第 25 章中 Purge 需要清理的 Undo 版本数量。它记录的是瞬时加列或删列带来的表行格式版本累积情况。

在 MySQL 8.4 中，行版本数量有上限；达到限制后需要考虑表重建，而不是无限次执行 Instant 加删列。

### 10.5 再做一个反例：哪些操作不能 INSTANT？

下面把字段 `name` 的长度由 `VARCHAR(100)` 改为 `VARCHAR(80)`，并强制要求 Instant：

```sql
ALTER TABLE products_ddl_instant
MODIFY COLUMN name VARCHAR(80) NOT NULL,
ALGORITHM=INSTANT;
```

预期：报出不支持指定算法的错误，而不是悄悄替换成一次昂贵的表重建。

这样我们就能验证：**INSTANT 是有适用范围的能力，不是 MySQL 对任意 DDL 的“加速开关”。**

注：不同列定义、字符集、索引和约束会影响实际支持条件；这里选择的是 MySQL 8.4 文档明确不支持 Instant 的缩短 VARCHAR 场景。

### 10.6 最重要的反例：INSTANT 也会等待 MDL

继续使用 `products_ddl_instant`。前面的加列实验完成后，这张表依然可以正常查询。

打开两个 MySQL 客户端会话。

**会话 A：**

```sql
USE mysql_source_lab;
START TRANSACTION;
SELECT stock FROM products_ddl_instant WHERE id = 42;
-- 暂时不要 COMMIT 或 ROLLBACK
```

**会话 B：**

```sql
USE mysql_source_lab;
SET SESSION lock_wait_timeout = 5;

ALTER TABLE products_ddl_instant
ADD COLUMN source_tag VARCHAR(20) DEFAULT 'web',
ALGORITHM=INSTANT;
```

如果会话 A 继续持有影响该表定义变更的事务级 MDL，会话 B 可能因等待而超时。

这时回到会话 A：

```sql
COMMIT;
```

在会话 B 重新执行刚才的 ALTER（如果第一次已经超时），则在符合条件的情况下可以完成。

这个实验非常重要：它说明 **Instant 避免的是昂贵的行数据重写，不是取消 MDL 的正确性约束。**

如果需要确认等待发生在哪里，可以结合 `SHOW PROCESSLIST` 和 Performance Schema 的 `metadata_locks` 表进行观察；后者的可用性与仪表配置、权限有关。

### 实验验证边界

本章的 Python 模型已经实际运行，所有断言通过。

本节提供的 SQL 是基于 MySQL 8.4 官方能力表与固定版本源码设计的**可复现实验**，本文并未在真实 MySQL 8.4 实例中执行这些语句，因此输出与等待行为明确标记为预期，不宣称已经测得执行时间。

---

## 11. 回头回答：Online DDL 到底“在线”在哪里？

经历三个方案之后，我们可以认真回答这个问题。

最初的方案，是创建新表并复制全部数据。为了避免复制过程中出现新旧数据不一致，最简单的设计会限制写入。

随后，在支持的 DDL 场景中，InnoDB 可以让部分构建工作与业务写入并行，同时通过必要的变更记录和同步动作协调结果。

而对于符合 Instant 条件的新增列，甚至可以避免重写旧记录，主要通过元数据变化实现新的表结构。

这些机制减少的是**长时间影响业务的数据处理工作**。

但它们并没有消除：

- 发布新表定义时的元数据锁协调；
- 某些操作必须重建或复制数据的成本；
- 构建期间临时日志和磁盘空间的消耗；
- 并发 DML 与新约束冲突导致的失败风险；
- 大表 DDL 对 CPU、I/O 与系统延迟的资源影响。

因此，`Online DDL` 不是“永远不锁表”，也不是“任何 ALTER 都能秒级完成”。

**它是一组试图减少阻塞、减少数据重写、允许更多业务并发的实现能力；具体得到哪些能力，由操作类型、存储引擎和表本身的条件决定。**

从应用层的角度，还有一条实践原则：在重要的结构变更中显式写出可接受的算法与锁要求，并在上线前验证兼容性；不要把 `ALGORITHM=DEFAULT` 当成“必然不影响业务”的保证。

## 12. 把三个方案重新放回源码地图

到这里，之前零散出现的函数终于有了完整的职责关系：

```text
ALTER TABLE products ADD COLUMN category ...
                   |
                   v
       SQL Server 分析变更要求
                   |
                   v
 mysql_alter_table()   [sql/sql_table.cc]
                   |
                   | 询问 InnoDB 能力
                   v
 check_if_supported_inplace_alter()
                   |
           ┌───────┼───────────────┐
           |       |               |
           v       v               v
         COPY    INPLACE         INSTANT
           |       |               |
           |       |               +-- 更新元数据
           |       |                   不必重写符合条件的旧记录
           |       |
           |       +-- 执行支持的原地 DDL
           |           可能重建表
           |           允许的情况下处理并发变更
           |
           +-- Server 层复制记录
               通常限制并发写入

      各路径都必须遵守相应的 MDL 协调规则
      并完成适用的表定义提交/切换过程
```

这是**控制流程的教学概览**，不是一张完全精确的 C++ 函数调用图。尤其是 `COPY` 与 `INPLACE/INSTANT` 在 Server 中有不同代码分支，不应画成三个都直接调用同一个 InnoDB 函数。

为了继续自行阅读源码，可以按下面的顺序定位。

| 位置 | 重点函数或接口 | 此时回答的问题 |
|---|---|---|
| `sql/sql_table.cc` | `mysql_alter_table()` | Server 为什么需要选择算法？ |
| `handler0alter.cc` | `ha_innobase::check_if_supported_inplace_alter()` | InnoDB 为什么支持或拒绝当前算法？ |
| `handler0alter.cc` | `innobase_support_instant()` | 新增列什么时候只需元数据变化？ |
| `sql/sql_table.cc` | `mysql_inplace_alter_table()` | 如何协调准备、主要执行与提交阶段？ |
| `handler0alter.cc` | `ha_innobase::inplace_alter_table()` | 原地 DDL 的主要工作由谁执行？ |
| `handler0alter.cc` | `row_log_table_apply()` 的调用处 | 在线重建期间的增量修改怎样合并？ |
| `handler0alter.cc` | `ha_innobase::commit_inplace_alter_table()` | 为什么发布新定义需要提交阶段？ |
| `dict0inst.cc` | `Instant_ddl_impl` | Instant 元数据怎样完成相应变更？ |

我们从来没有一次性背诵这些函数的必要。

它们都是在追问同一个业务问题时，被新的约束逐步引出来的。

---

## 13. 回顾整个系列：一次购买为什么会牵出一座数据库？

本章是整个 27 章系列的最后一章。

最初，我们只想让商品 42 的库存从 `100` 变成 `99`。

一个 Python 字典就可以完成最小实验。

但是，当程序需要保存数据、支持 SQL、管理海量记录并协调多个连接时，我们渐渐需要理解 MySQL 的真实执行方式。

沿着一次 UPDATE，我们认识了请求入口、索引、页面、Buffer Pool、事务、锁、Undo、Redo、Binlog 和复制。

随后，沿着一次 SELECT，我们深入二级索引、回表、优化器、行迭代器和 JOIN。

当系统开始并发运行，我们又研究了 Read View、范围锁、死锁与 Purge。

最后，业务提出修改表结构的新需求，我们才开始研究 MDL 和 Online DDL。

这一路并不意味着所有机制之间都有严格的历史先后关系。它们在真实 MySQL 中往往共同工作，而我们的教学选择是从一个可观察的问题出发，每次只揭开当前最需要理解的一层。

最终，我们希望能够做到两件事。

第一，拿到一条 SQL，可以沿着固定版本的源码找到它的主要执行路径，并解释每一步的输入、行为与结果。

第二，当遇到性能、并发或故障问题时，不只是想起一个术语，而是能指出：**目前哪一个简单假设失效了，MySQL 的哪项机制在解决它，又付出了什么代价。**

我们不是为了画出一张庞大的架构图而学习 MySQL。

而是因为已经走过这些真实问题，到了最后，终于有能力看懂那张架构图。

**从一次库存扣减开始，我们追踪的始终是同一件事：一个看似简单的操作，怎样在真实约束下依然正确地运行。**

---

## 本章源码与文档参考

源码统一固定为 **MySQL 8.4.0 / `mysql-8.4.0`**，避免把其他版本的实现混入同一执行路径。

- [Server ALTER TABLE 实现：sql/sql_table.cc](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/sql_table.cc)
- [InnoDB ALTER TABLE 实现：handler0alter.cc](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/handler/handler0alter.cc)
- [Instant DDL 实现：dict0inst.cc](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/dict/dict0inst.cc)
- [MySQL 8.4 官方：Online DDL Operations](https://dev.mysql.com/doc/refman/8.4/en/innodb-online-ddl-operations.html)
- [MySQL 8.4 官方：Online DDL Performance and Concurrency](https://dev.mysql.com/doc/refman/8.4/en/innodb-online-ddl-performance.html)
- [MySQL 8.4 官方：Online DDL Failure Conditions](https://dev.mysql.com/doc/refman/8.4/en/innodb-online-ddl-failure-conditions.html)
- [MySQL 8.4 官方：Online DDL Space Requirements](https://dev.mysql.com/doc/refman/8.4/en/innodb-online-ddl-space-requirements.html)

**阅读边界：** 本章只深入说明普通 InnoDB 表上几种典型 DDL 路线如何解决“尽量减少业务中断”的问题。不同 DDL、分区表、特殊索引、外键、存储格式和版本的支持矩阵存在差异；所有功能性判断应以实际 MySQL 版本与对应官方文档为准。
