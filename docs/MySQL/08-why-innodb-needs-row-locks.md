---
title: "08｜两个用户购买最后一件商品：为什么需要锁？"
sidebar_position: 9
slug: /mysql/08-why-row-locks
description: 从商品 42 的并发库存扣减出发，复现读改写竞争，追踪 MySQL 8.4.0 中 UPDATE 的记录加锁与等待路径。
tags: [MySQL, InnoDB, UPDATE, 行锁, 并发, 源码精读]
---

# 两个用户购买最后一件商品：为什么需要锁？

上一章，我们已经追踪到商品 `42` 的库存是如何被修改的。

```sql
UPDATE products
SET stock = stock - 1
WHERE id = 42 AND stock > 0;
```

当只有一个用户购买时，事情很简单：InnoDB 定位商品记录，修改相应数据页，库存从 `100` 变成 `99`。

但电商系统不可能永远只有一个用户。

假设现在商品 `42` **只剩最后一件**，两个用户几乎同时点击购买。

我们希望最终结果是：**只有一人购买成功，另一个人得到库存不足，库存不能变成负数，也不能凭空卖出两件。**

这一次我们不换业务，也不提前介绍事务隔离级别。沿着原来的库存扣减，先看看最简单的程序为什么可能失败。

## 1. 单用户成立的判断，为什么在两个用户面前失效？

前面写过一个很容易理解的 Python 版本：

```python
products = {42: {"stock": 1}}


def buy(product_id):
    product = products.get(product_id)
    if product is None or product["stock"] <= 0:
        return False
    product["stock"] -= 1
    return True
```

单独运行一次，结果正确。可这段代码实际上包含三个动作：**读库存、判断是否大于零、写入新库存**。

如果一个用户完整执行完这三个动作，另一个用户才开始，当然没有问题。

麻烦出在两次购买的动作有可能交错。

假设起始库存是 `1`：

| 时刻 | 用户 A | 用户 B | 数据库或共享数据中的库存 |
|---|---|---|---|
| T1 | 读到库存 `1` | — | 1 |
| T2 | — | 也读到库存 `1` | 1 |
| T3 | 判断库存足够 | — | 1 |
| T4 | — | 判断库存足够 | 1 |
| T5 | 根据读到的 `1` 写入 `0`，报告成功 | — | 0 |
| T6 | — | 根据之前读到的 `1` 也写入 `0`，报告成功 | 0 |

最后，库存显示 `0`，看起来没有负数。

**但系统竟然告诉两个人“购买成功”。** 两笔订单对应的商品却只有一件，这仍然是超卖。

这里的问题不是计算 `1 - 1` 的公式错了，而是“判断库存”和“修改库存”之间被另一次购买插了进来。

我们可以不依赖线程调度，先用一个完全确定的教学实验还原这个交错。

### 实验一：复现旧值覆盖

**教学模型，不是 MySQL 源码。**

```python
product = {"stock": 1}

# 两次请求先后读取；写入尚未发生。
observed_a = product["stock"]
observed_b = product["stock"]

successful_orders = 0

# A 根据此前读到的值写入。
if observed_a > 0:
    product["stock"] = observed_a - 1
    successful_orders += 1

# B 没有重新检查共享库存，也根据旧值写入。
if observed_b > 0:
    product["stock"] = observed_b - 1
    successful_orders += 1

print("购买成功次数:", successful_orders)
print("最终库存:", product["stock"])
```

**已实际运行的输出：**

```text
购买成功次数: 2
最终库存: 0
```

这就是失败迹象：最终剩余数量表面上合理，但业务上已经承诺了两次成功购买。

注意：这段程序刻意把读取和写入拆开，不是在断言 Python 一次 `-=` 必然发生上述交错，也不是说 InnoDB 的单条 `UPDATE` 存在这个错误。它复现的是**应用层“先读—再判断—再按旧值写回”**这一危险设计。

那么，我们究竟缺少什么能力？

> 对同一件商品，检查库存是否足够与完成扣减不能被其他冲突的购买随意穿插。

这时，引入一种协调机制才有了意义。

## 2. 最小修复：把判断和修改放在一个受保护的区域

对于只有一个 Python 进程的教学模型，可以先使用一个互斥锁：

```python
from threading import Barrier, Lock, Thread

product = {"stock": 1}
product_lock = Lock()
start = Barrier(3)
results = []


def buy():
    start.wait()  # 两个工作线程都准备好后再开始竞争
    with product_lock:
        if product["stock"] <= 0:
            results.append(False)
            return

        product["stock"] -= 1
        results.append(True)


threads = [Thread(target=buy) for _ in range(2)]
for thread in threads:
    thread.start()

start.wait()
for thread in threads:
    thread.join()

print("购买成功次数:", results.count(True))
print("最终库存:", product["stock"])

assert results.count(True) == 1
assert product["stock"] == 0
```

**已实际运行的输出：**

```text
购买成功次数: 1
最终库存: 0
```

它为什么有效？

因为第一个线程进入 `with product_lock` 后，第二个线程不能同时进入同一段代码。等第一个线程完成库存判断和扣减，第二个线程才会看到库存已经变成 `0`，从而返回失败。

我们暂时只新增了一个机制：**互斥访问**。

不过，这种 Python 锁解决的是同一进程、共享这把锁的线程之间的协调。如果购物系统部署在两台应用服务器上，两台服务器各自有自己的 Python 锁，它们无法靠这把锁协调访问同一条数据库记录。

于是，我们真正需要的不是“给每个应用进程放一把锁”，而是：

**由所有购买请求共同访问的数据管理者，来协调对商品记录的冲突修改。**

在当前系统中，这个共同的数据管理者就是 MySQL 的 InnoDB 存储引擎。

## 3. 回到 MySQL：单条条件 UPDATE 已经解决了什么？

我们一直使用的 SQL 其实比“应用先 SELECT，然后 UPDATE 写入旧值计算结果”更稳妥：

```sql
UPDATE products
SET stock = stock - 1
WHERE id = 42 AND stock > 0;
```

这条语句把**条件判断**和**扣减动作**表达在同一次数据库操作中。

如果两个连接同时执行，InnoDB 需要协调它们对同一条商品记录的修改。对于本章这张按主键定位的 InnoDB 表，正常成功执行时，并发的冲突更新不会都依据最初那个 `stock = 1` 各自宣告扣减成功。

假设事务 A 已经成功把库存从 `1` 改成 `0`，但尚未提交；事务 B 此时也要修改 `id = 42` 的记录。

事务 B 不能绕过事务 A 正在进行的冲突修改，直接把这条记录当成可以修改的独立副本。它必须等冲突得到处理，或因超时、死锁等原因返回错误。

如果事务 A 最终提交，事务 B 正常继续处理这次条件更新时，库存已经是 `0`，不再符合 `stock > 0`，因此本次扣减不会成功。

这时就有了一个精确的业务判定规则：

```text
SQL 无错误，且实际修改了 1 行 → 本次库存扣减成功
SQL 无错误，但实际修改了 0 行 → 没有扣减库存
出现锁等待超时等错误         → 操作失败，按错误处理
```

不要把“SQL 返回成功”直接等同于“购买成功”，也不要把“等待结束”直接等同于“更新一定成功”。

接下来，我们在真实 MySQL 中观察这个行为，然后再研究源码为什么能够做到。

## 4. 双会话实验：第二次扣减被阻塞在哪里？

本章使用 MySQL 8.4 和 InnoDB。在一个**专用实验数据库**里准备数据。以下 `DROP TABLE` 会删除实验用的同名表，不要在业务库执行。

```sql
CREATE DATABASE IF NOT EXISTS mysql_source_lab;
USE mysql_source_lab;

DROP TABLE IF EXISTS products;
CREATE TABLE products (
    id BIGINT PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    stock INT NOT NULL
) ENGINE = InnoDB;

INSERT INTO products (id, name, stock)
VALUES (42, 'Keyboard', 1);
```

现在打开两个独立的 MySQL 客户端连接，分别记为 **会话 A** 和 **会话 B**。如果希望观察等待期间的内部锁，再打开第三个观察用连接 **会话 C**。

### 4.1 会话 A：先扣减，但暂时不提交

在会话 A 执行：

```sql
USE mysql_source_lab;

START TRANSACTION;

UPDATE products
SET stock = stock - 1
WHERE id = 42 AND stock > 0;

SELECT ROW_COUNT() AS affected_rows;
```

预期 `affected_rows = 1`。注意：`ROW_COUNT()` 必须紧接着目标 `UPDATE` 调用，否则会报告其他语句的结果。

**此时不要执行 `COMMIT`。**

库存的修改已经发生在事务 A 中，但事务 A 尚未结束。只要它还持有该记录上的相关排他锁，其他需要冲突锁的事务就不能直接修改同一记录。

### 4.2 会话 B：同时扣减同一件商品

在另一个连接执行：

```sql
USE mysql_source_lab;

SET SESSION innodb_lock_wait_timeout = 30;
START TRANSACTION;

UPDATE products
SET stock = stock - 1
WHERE id = 42 AND stock > 0;
```

**正常预期：这一条 `UPDATE` 会等待，而不是立即返回。**

不要在会话 B 里立刻继续输入 `SELECT ROW_COUNT()`，因为客户端还在等待上一条 UPDATE 完成。

从业务角度看，这正是我们想要的现象：两次针对相同记录的扣减，没有在毫无协调的情况下并行完成。

### 4.3 会话 C：可选地观察等待关系

趁会话 B 仍在等待，在第三个连接中执行：

```sql
SELECT
    dl.ENGINE_TRANSACTION_ID,
    dl.OBJECT_SCHEMA,
    dl.OBJECT_NAME,
    dl.INDEX_NAME,
    dl.LOCK_TYPE,
    dl.LOCK_MODE,
    dl.LOCK_STATUS,
    dl.LOCK_DATA
FROM performance_schema.data_locks AS dl
WHERE dl.OBJECT_SCHEMA = 'mysql_source_lab'
  AND dl.OBJECT_NAME = 'products'
ORDER BY dl.ENGINE_TRANSACTION_ID, dl.LOCK_STATUS;
```

重点不是死记每一列的具体输出，而是区分：

- `GRANTED`：锁已经授予。
- `WAITING`：锁请求还在等待。
- `INDEX_NAME = PRIMARY`：与主键索引有关的记录锁。
- `LOCK_MODE` 包含 `X`：排他锁模式。

为了确认是谁阻塞了谁，还可以查询：

```sql
SELECT
    w.REQUESTING_ENGINE_TRANSACTION_ID AS waiting_trx,
    w.BLOCKING_ENGINE_TRANSACTION_ID AS blocking_trx
FROM performance_schema.data_lock_waits AS w;
```

这些表是观察手段，具体显示的锁行数、锁 ID 与锁附加信息可能随服务器状态而变化；还需要账户具备相应查询权限。本章不捏造任何一次真实 MySQL 运行中的事务 ID 或锁 ID。

### 4.4 会话 A：提交，让等待继续

回到会话 A：

```sql
COMMIT;
```

如果会话 B 尚未超时，且没有其他错误，会话 B 的 `UPDATE` 将继续完成。

此时 B 所面对的库存已经是 `0`，`stock > 0` 不成立。因此，本例中会话 B 正常完成时预期实际修改 **0 行**。

在会话 B 中，等最初的 `UPDATE` 返回后，立即执行：

```sql
SELECT ROW_COUNT() AS affected_rows;
COMMIT;
```

预期 `affected_rows = 0`。

最后任选一个连接执行：

```sql
SELECT stock FROM mysql_source_lab.products WHERE id = 42;
```

预期库存是 `0`，而成功扣减的请求只有一个。

### 4.5 这个实验验证了什么？

```text
初始库存：1

事务 A：获得修改所需的锁 → stock 1 → 0 → 尚未提交
事务 B：申请冲突的锁 → 等待
事务 A：COMMIT → 释放相关锁
事务 B：恢复执行 → 条件不再成立 → 修改 0 行
最终库存：0
```

注意区别：锁负责协调冲突访问；`stock > 0` 负责表达业务约束。**不是只靠锁就能凭空知道“不能超卖”**。如果我们写的是不带库存条件的 `UPDATE ... SET stock = stock - 1`，即便更新受到正确的互斥协调，也仍然可能依次扣成负数。

本节 SQL 提供了可复现步骤和预期行为，但**没有在本次环境启动真实 MySQL 8.4 实例运行**。只有前面的 Python 教学实验确实运行过。

---

## 5. 源码：UPDATE 为什么不是普通的不加锁 SELECT？

前几章，我们已经从 SQL 执行流程一路追踪到 InnoDB 的记录读取和修改。现在要补上一个此前没有深入的动作：**为了修改记录，数据库必须协调谁有权执行冲突操作。**

本章固定源码版本：

- 仓库：[mysql/mysql-server](https://github.com/mysql/mysql-server)
- 版本：**MySQL 8.4.0**
- Git Tag：`mysql-8.4.0`
- 主要文件：`storage/innobase/handler/ha_innodb.cc`、`storage/innobase/row/row0sel.cc`、`storage/innobase/lock/lock0lock.cc`

以下均为在这个固定版本核对过的真实源码节选。我们只抽取当前问题所需要的行，不复制整段大型函数。

### 5.1 第一步：告诉 InnoDB，这次读取是为了修改

打开 [`ha_innodb.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/handler/ha_innodb.cc)，搜索 `ha_innobase::external_lock()`。

在这条方法的相关分支中能找到：

```cpp
if (lock_type == F_WRLCK) {
  m_prebuilt->select_lock_type = LOCK_X;
  m_stored_select_lock_type = LOCK_X;
}
```

**源码位置：** [`ha_innodb.cc`，约 L17542–L17547](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/handler/ha_innodb.cc#L17542-L17547)。

这里 `LOCK_X` 表示要按排他方式访问记录。`m_prebuilt` 则保存当前表访问过程在 InnoDB 中使用的相关状态。

这段代码并不是“此处已经把商品 42 锁住”。它只是把本次表访问的锁定需求配置到后续记录搜索会读取的状态中。

为什么需要这一步？

因为“读取记录的值”与“为了修改而读取记录”并不完全相同。

普通 `SELECT` 在常见的 `READ COMMITTED`、`REPEATABLE READ` 隔离级别下，通常走一致性非锁定读取；而 `UPDATE` 面对的目标记录，需要按相应规则获取修改所需的锁。这里先区分这两类访问，MVCC 和旧版本读取留到第 10 章。

### 5.2 第二步：找到目标索引记录时，请求对应的记录锁

打开 [`row0sel.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/row/row0sel.cc)，找到：

```cpp
row_search_mvcc(...)
```

函数在处理需要加锁的记录时，有这样的分支：

```cpp
if (prebuilt->select_lock_type != LOCK_NONE) {
  // ... 判断当前记录与访问范围的关系 ...
  err = sel_set_rec_lock(
      pcur, rec, index, offsets,
      use_semi_consistent ? SELECT_SKIP_LOCKED : prebuilt->select_mode,
      prebuilt->select_lock_type, lock_type, thr, &mtr);
}
```

**源码性质：** 真实源码节选，省略了原函数中间的范围判断和分支；省略处以注释明确标识，不是声称原本只有这些行。

**源码位置：** [`row0sel.cc`，约 L4823–L4850](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/row/row0sel.cc#L4823-L4850)。

这里最重要的信息不是所有参数的名称，而是三个动作：

第一，只有当前读取需要加锁，才会进入这个分支。第二，已经通过索引搜索定位到正在处理的记录。第三，系统把记录、索引、锁模式和当前查询线程交给 `sel_set_rec_lock()`。

换句话说：**上层要求修改，执行路径定位到具体记录，InnoDB 才针对这条索引记录提出锁请求。**

前面 B+ 树章节已经学过怎样定位记录。现在我们正在使用同一次定位过程，只是在读取过程中增加了并发协调这一层。

### 5.3 第三步：为什么锁与“索引记录”绑定？

仍然在 `row0sel.cc`，找到 `sel_set_rec_lock()`：

```cpp
if (index->is_clustered()) {
  err = lock_clust_rec_read_check_and_lock(
      lock_duration_t::REGULAR, block, rec, index, offsets, sel_mode,
      static_cast<lock_mode>(mode), type, thr);
}
```

**真实源码节选：** [`row0sel.cc`，约 L1074–L1077](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/row/row0sel.cc#L1074-L1077)。

我们现在使用的 `products` 表通过 `PRIMARY KEY (id)` 组织聚簇索引。对商品 `42` 的主键定点查找，正常会落到聚簇索引记录上，因此这里展示的是聚簇索引分支。

这也解释了为什么 InnoDB 的“行锁”经常在源码中被称作**记录锁**：它在实现上与索引记录的定位紧密结合，而不是一个独立于索引结构的“商品 ID → 锁”Python 字典。

其他访问路径可能涉及二级索引、索引范围或额外的锁。现在只讨论这次主键精确匹配，不把所有锁类型一次讲完。

### 5.4 第四步：已有冲突锁时，为什么第二个事务会等待？

打开 [`lock0lock.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/lock/lock0lock.cc)，查找：

```cpp
dberr_t lock_clust_rec_read_check_and_lock(...)
```

在这个函数中，获取目标记录在页内的编号并处理相关锁状态后，会调用：

```cpp
err = lock_rec_lock(false, sel_mode, mode | gap_mode, block, heap_no, index,
                    thr);
```

**真实源码节选：** [`lock0lock.cc`，约 L5326–L5327](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/lock/lock0lock.cc#L5326-L5327)。

我们已经走到了更底层的锁管理逻辑：它接收锁模式、数据页、页内记录位置以及当前事务所属执行线程。

继续看 `lock_rec_lock()`：

```cpp
switch (lock_rec_lock_fast(impl, mode, block, heap_no, index, thr)) {
  case LOCK_REC_SUCCESS:
    return (DB_SUCCESS);
  case LOCK_REC_SUCCESS_CREATED:
    return (DB_SUCCESS_LOCKED_REC);
  case LOCK_REC_FAIL:
    return (lock_rec_lock_slow(impl, sel_mode, mode, block, heap_no, index, thr));
  default:
    ut_error;
}
```

**真实源码节选：** [`lock0lock.cc`，约 L1825–L1835](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/lock/lock0lock.cc#L1825-L1835)。

这里有一个设计上的分层。

常见、简单的情况先走快速路径；快速路径不能完成时，再走更一般的慢路径。慢路径需要处理更多锁队列与冲突情况。

这并不是说“遇到其他事务就一定立即返回失败”。对于普通加锁模式，如果现有锁与当前请求冲突，InnoDB 可以建立等待请求，并在锁被释放后继续处理；如果等待超时或发生死锁，则可能以错误结束。

在我们的两个购买请求中，事务 A 持有记录上的排他锁时，事务 B 对同一记录的冲突排他请求无法立即得到授予。**这个竞争关系就是之前 SQL 实验里 B 等待的来源。**

注意，底层函数可以产生 `DB_LOCK_WAIT`，但客户端不会把每次内部等待直接作为最终的 SQL 执行结果。运行线程和事务调度还需要处理等待、恢复以及错误传播。这些细节在本章先保留为边界，下一次研究锁等待与死锁专题时再展开。

## 6. 为什么按主键更新通常只锁这一条，而不是整张表？

现在回到我们已经理解过的索引结构。

```sql
UPDATE products
SET stock = stock - 1
WHERE id = 42 AND stock > 0;
```

`id` 是主键，`id = 42` 是精确等值条件。如果优化器采用主键定点查找，InnoDB 可以确定要访问的目标记录。

根据 MySQL 8.4 官方手册：当加锁语句使用**唯一索引上的唯一搜索条件**时，通常只需要锁定找到的索引记录，而不需要锁定记录前面的间隙。

这与前一阶段学习的 B+ 树存在直接关系：索引不仅帮助我们减少无关数据的查找，也影响修改语句需要访问、因而可能锁定的记录范围。

例如：

```sql
UPDATE products
SET stock = stock - 1
WHERE id = 42 AND stock > 0;
```

通常可以利用 `PRIMARY` 做精确定位。

但如果使用一个缺少合适索引的条件，MySQL 可能需要扫描更多索引记录；在相应隔离级别和执行计划下，被扫描记录的锁定范围可能扩大。不是“SQL 只修改一行，就必然只锁一行”。

这里不要误解成 InnoDB 完全没有表级锁。更新还涉及表级**意向锁**等协调状态；但它并不等于以排他表锁把整张表的所有普通读取与其他商品更新都阻塞住。

这个例子暂时只需要理解：**查找路径越精确，越可能把需要协调的修改范围限定在目标索引记录上。**

间隙锁、Next-Key Lock 和复杂范围更新留给后续专题。

## 7. 关键边界：有行锁，不等于业务代码可以随意写

我们已经解释了为什么这条 SQL 可以在两个购买请求并发到来时，做到只扣减一次：

```sql
UPDATE products
SET stock = stock - 1
WHERE id = 42 AND stock > 0;
```

但它依赖两个不同条件。

**第一，库存约束写进同一条 UPDATE。**

这样，操作不依赖应用程序几毫秒前读到的旧值，而是由数据库在执行当前修改时判断是否满足 `stock > 0`。

**第二，数据库协调针对同一记录的冲突更新。**

这样，两个事务不能在不处理彼此冲突的情况下都完成同一记录的修改。

反过来，如果应用程序先单独读取库存：

```sql
SELECT stock FROM products WHERE id = 42;
```

再把那个早已读取的数字作为常量写回：

```sql
UPDATE products SET stock = 0 WHERE id = 42;
```

即使后面的 UPDATE 正确加锁，**也不会自动修复应用层此前基于旧库存作出的成功购买决定**。

因为锁能够协调实际记录修改，但无法替你重新审查数据库外部已经做出的业务承诺。

同样，这一章没有解决“扣减库存成功后，订单写入失败”怎么办。现在的 SQL 最多能帮我们完成一次原子性的条件库存扣减，却不能自动保证另外一条创建订单的语句也成功。

这个问题需要我们在下一章继续扩展业务操作，而不是强行让“锁”承担本不属于它的全部责任。

## 8. 回看：从单用户程序到数据库记录锁

本章从一个只有一件库存的例子开始。

最初，单用户的 `if stock > 0` 加 `stock -= 1` 已经足够。

当两个请求交错执行，我们明确复现了这样的失败：**两个人都依据同一个旧库存判断成功，却只剩一件商品可卖**。

于是，我们先使用 Python `Lock` 把判断和修改保护起来，验证互斥能使成功购买次数从 `2` 变成 `1`。

但应用锁无法自然跨越多个应用实例。于是，我们把协调冲突修改的责任放回共享数据的管理者：InnoDB。

在真实 SQL 中，我们使用同一条条件更新表达购买要求，设计了双会话实验观察：一个事务先修改但不提交，另一个事务请求修改同一记录时等待；前者提交后，后者因为库存条件不再满足而修改 `0` 行。

最后，我们在 MySQL 8.4.0 源码中找到了与这件事对应的关键路径：

```text
UPDATE 商品 42
       |
       v
InnoDB 以修改意图访问记录
       |
       v
row_search_mvcc() 定位索引记录
       |
       v
sel_set_rec_lock()
       |
       v
lock_clust_rec_read_check_and_lock()
       |
       v
lock_rec_lock()
       |
       +-- 能获得所需锁 → 继续处理
       |
       +-- 与其他事务冲突 → 等待 / 可能报错
```

这是围绕“记录锁请求”抽出的**关键路径图**，而不是 UPDATE 从 SQL 文本到提交的完整调用栈。

现在，我们已经能够回答本章的六个问题：

1. **具体问题是什么？** 两个用户都看到最后一件库存，并都声称购买成功。
2. **最简单的方案为什么不够？** 判断和写入分开，旧值可能在并发中失效。
3. **需要什么新能力？** 对同一记录的冲突修改必须受协调。
4. **引入什么机制？** 在数据管理者内部进行记录级加锁与等待。
5. **真实源码在哪里？** `ha_innodb.cc` 的锁意图、`row0sel.cc` 的记录锁请求和 `lock0lock.cc` 的冲突处理。
6. **运行行为改变了什么？** 冲突 UPDATE 不再毫无协调地并行完成；结合 `stock > 0`，本例只有一个请求实际扣减成功。

## 9. 下一章：库存扣减成功，但订单写入失败怎么办？

现在，最后一件商品不会因为两次并发的条件扣减而被卖出两次。

可真实的购买动作不止扣库存。

我们还必须创建订单：

```sql
UPDATE products
SET stock = stock - 1
WHERE id = 42 AND stock > 0;

INSERT INTO orders (product_id, quantity)
VALUES (42, 1);
```

假设第一条 SQL 已经成功，第二条却因为订单数据不合法而失败。

库存已经扣了，却没有相应订单。

这一回，加锁没有解决问题。它协调的是冲突修改，并不能让两条独立执行的 SQL 自动成为不可分割的整体。

**我们需要一种办法：两步都成功才确认购买；任何一步失败，就撤销此前未提交的库存修改。**

下一章，我们就从这个真实失败场景出发，研究事务为什么必须支持回滚，以及 InnoDB 如何借助 Undo 恢复修改前的数据。

**下一章：第 09 章《库存扣了，订单却失败——为什么需要事务和 Undo？》**

---

## 源码版本、参考资料与实验状态

### 源码版本

- 固定版本：[MySQL 8.4.0（`mysql-8.4.0`）](https://github.com/mysql/mysql-server/tree/mysql-8.4.0)
- [`ha_innodb.cc`：`ha_innobase::external_lock()`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/handler/ha_innodb.cc#L17542-L17547)
- [`row0sel.cc`：`row_search_mvcc()` 申请记录锁](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/row/row0sel.cc#L4823-L4850)
- [`row0sel.cc`：`sel_set_rec_lock()` 聚簇索引分支](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/row/row0sel.cc#L1074-L1077)
- [`lock0lock.cc`：`lock_clust_rec_read_check_and_lock()`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/lock/lock0lock.cc#L5291-L5337)
- [`lock0lock.cc`：`lock_rec_lock()`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/lock/lock0lock.cc#L1788-L1836)

### 官方文档

- [InnoDB Locking](https://dev.mysql.com/doc/refman/8.4/en/innodb-locking.html)
- [Locks Set by Different SQL Statements in InnoDB](https://dev.mysql.com/doc/refman/8.4/en/innodb-locks-set.html)
- [Transaction Isolation Levels](https://dev.mysql.com/doc/refman/8.4/en/innodb-transaction-isolation-levels.html)
- [Performance Schema `data_locks`](https://dev.mysql.com/doc/refman/8.4/en/performance-schema-data-locks-table.html)
- [Performance Schema `data_lock_waits`](https://dev.mysql.com/doc/refman/8.4/en/performance-schema-data-lock-waits-table.html)

### 验证边界

- **已实际执行**：Python 旧值覆盖实验；结果为购买成功 `2` 次、库存 `0`。
- **已实际执行**：Python 线程锁实验；结果为购买成功 `1` 次、库存 `0`，并通过断言。
- **已核对**：固定 MySQL 8.4.0 版本的 `ha_innodb.cc`、`row0sel.cc`、`lock0lock.cc` 关键函数、分支与代码片段。
- **未在当前环境执行**：真实 MySQL 双会话事务等待与 Performance Schema 查询；因此本章只给出预期行为，不伪造锁 ID、耗时或终端输出。

本章只研究**通过主键精确定位的一次单行条件更新**。范围锁定、死锁检测、MVCC 可见性、事务回滚与崩溃恢复都不属于当前的唯一主问题。
