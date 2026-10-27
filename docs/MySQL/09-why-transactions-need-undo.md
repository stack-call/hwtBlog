---
title: "09｜库存扣了，订单却失败：为什么需要事务和 Undo？"
sidebar_position: 10
slug: /mysql/09-why-transactions-need-undo
description: 从一次库存扣减与订单写入不一致的失败出发，以可运行的最小 Undo 实验、MySQL 双语句事务实验和 8.4.0 真实回滚源码，理解事务为什么能撤销修改。
tags: [MySQL, InnoDB, 事务, Undo, 源码精读]
---

# 库存扣了，订单却失败：为什么需要事务和 Undo？

上一章，我们让两个用户同时购买最后一件商品，观察了行锁如何协调冲突更新。对于这条语句：

```sql
UPDATE products
SET stock = stock - 1
WHERE id = 42 AND stock > 0;
```

我们已经知道，**一条条件 UPDATE 可以让“检查库存”和“修改库存”在数据库的一次语句执行中完成**。如果库存只有一件，正确处理语句执行结果，就不应该把两次竞争更新都当作成功购买。

但现在的购买流程还缺少一半。

用户买走一件商品，系统不仅需要扣库存，还需要创建订单。假设库存扣减成功，订单写入却失败。数据库中就可能留下一个令人困惑的结果：**库存少了一件，却没有对应的新订单。**

这一章仍然只研究商品 `42` 的一次购买。我们不更换案例，也不急着介绍事务的四种特性。先亲眼看到这个错误怎样出现，再想办法让两次数据修改共同成功或失败。

## 1. 两条正确的 SQL，为什么组合起来反而可能出错？

先把一次购买拆成两个步骤：

```sql
-- 第一步：库存减少一件
UPDATE products
SET stock = stock - 1
WHERE id = 42 AND stock > 0;

-- 第二步：创建订单
INSERT INTO orders (order_id, product_id, quantity)
VALUES (9002, 42, 1);
```

这两个操作分别看都很合理。库存减少表示商品被购买；插入订单表示这次购买有了可追踪的记录。

如果第二步成功，一切正常。如果第二步报错，第一步怎么办？

先别假设数据库会替我们自动处理。看一个可重复的 MySQL 实验。

### 准备实验数据

本章使用独立实验库。下面的建表语句会**删除该实验库中原有的 `orders` 表**，不要在生产或含有重要数据的环境运行。

```sql
CREATE DATABASE IF NOT EXISTS mysql_source_lab;
USE mysql_source_lab;

CREATE TABLE IF NOT EXISTS products (
    id BIGINT PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    stock INT NOT NULL
) ENGINE = InnoDB;

DELETE FROM products WHERE id = 42;
INSERT INTO products (id, name, stock)
VALUES (42, 'Keyboard', 2);

DROP TABLE IF EXISTS orders;
CREATE TABLE orders (
    order_id BIGINT PRIMARY KEY,
    product_id BIGINT NOT NULL,
    quantity INT NOT NULL
) ENGINE = InnoDB;

-- 预先占用订单号 9001，后面故意制造主键冲突
INSERT INTO orders (order_id, product_id, quantity)
VALUES (9001, 42, 1);
```

现在我们有一个商品，剩余库存为 `2`，并且已经存在订单 `9001`。

### 先使用默认的自动提交

在一个 MySQL 客户端会话中执行：

```sql
SET SESSION autocommit = 1;

UPDATE products
SET stock = stock - 1
WHERE id = 42 AND stock > 0;

SELECT ROW_COUNT() AS updated_rows;
```

预期 `updated_rows = 1`，库存从 `2` 变为 `1`。

接着故意使用已有订单号：

```sql
INSERT INTO orders (order_id, product_id, quantity)
VALUES (9001, 42, 1);
```

预期出现类似的错误：

```text
ERROR 1062 (23000): Duplicate entry '9001' for key 'orders.PRIMARY'
```

此时重新查询：

```sql
SELECT stock FROM products WHERE id = 42;
SELECT COUNT(*) AS order_9002_exists
FROM orders WHERE order_id = 9002;
```

预期得到：

```text
stock = 1
order_9002_exists = 0
```

问题出现了：订单没有创建成功，但库存已经减少。

**原因不是 InnoDB 不支持事务，而是我们让两条语句分别处于自动提交的执行方式中。**第一条 UPDATE 正常完成后，其事务已经提交；后面独立执行的 INSERT 即使失败，也不能再撤销刚才已经提交的 UPDATE。

这和“单条 UPDATE 可以避免特定的库存并发竞争”并不矛盾：**单条语句的正确性，不会自动变成多条语句之间的业务原子性。**

> 这里故意选用“主键冲突”，因为失败条件稳定、容易复现。不同类型的 SQL 错误可能有不同的回滚范围；这不影响我们眼前需要解决的业务问题。

## 2. 最简单的补救：订单失败，就把库存加回来？

我们可以想到一个直观办法：

```python
# 概念伪代码：不是 MySQL 真实实现
success = decrease_stock(42)
if success:
    try:
        create_order(9001, 42)
    except Exception:
        increase_stock(42)
```

看起来，我们只需要在订单创建失败时补上一条 `stock = stock + 1`。

在只有一个用户、只有这两步操作、且补偿操作保证成功的理想情况下，这样的程序可能看起来有效。

但是，**我们正是因为不想假设每一步都一定成功，才开始讨论失败处理的。**现在把可靠性寄托在第三次数据库操作上，只是把问题推迟了。

例如：订单插入失败后，负责补库存的程序也可能崩溃；网络中断还可能让客户端无法判断某条 SQL 是否真的完成。再增加更多操作后，逐项手写补偿也越来越难保证正确。

因此，我们真正需要的能力不是“出错时再试着修改回来”，而是：

> **把库存扣减和订单创建视为一个整体。在确定整组操作成功之前，保留放弃全部未提交修改的能力。**

现在才有理由引入**事务（Transaction）**。

## 3. 用 `START TRANSACTION` 把两次修改放进同一个边界

MySQL 提供了明确的事务控制语句：

```sql
START TRANSACTION;
-- 业务修改 1
-- 业务修改 2
COMMIT;
```

或者，在中途决定放弃时：

```sql
ROLLBACK;
```

我们不先背事务的完整定义。只看它对商品购买产生的行为改变：

- 两个修改属于同一笔事务；
- 当业务判断全部成功，才执行 `COMMIT`；
- 当其中一步失败，业务明确执行 `ROLLBACK`，撤销当前事务中尚未提交的修改。

### 实验：一次失败导致整笔购买撤销

先在**事务之外**把库存恢复为 `2`：

```sql
USE mysql_source_lab;
UPDATE products SET stock = 2 WHERE id = 42;
```

然后在同一个连接中执行：

```sql
START TRANSACTION;

UPDATE products
SET stock = stock - 1
WHERE id = 42 AND stock > 0;

SELECT ROW_COUNT() AS updated_rows;
SELECT stock FROM products WHERE id = 42;
```

到此，当前连接预期能看到库存为 `1`，而 `updated_rows = 1`。我们确实执行过了 UPDATE；事务并不是让修改“直到提交才第一次发生”。

接着执行会失败的订单插入：

```sql
INSERT INTO orders (order_id, product_id, quantity)
VALUES (9001, 42, 1);
```

由于 `9001` 已存在，预期出现重复主键错误。

**关键动作在下一行。**在同一个连接中，明确执行：

```sql
ROLLBACK;
```

然后检查：

```sql
SELECT stock FROM products WHERE id = 42;
SELECT COUNT(*) AS order_9002_exists
FROM orders WHERE order_id = 9002;
```

预期结果为：

```text
stock = 2
order_9002_exists = 0
```

这次，库存回到了购买前的值。

我们没有在应用程序中手写一次反向 UPDATE。我们只是告诉数据库：**放弃这笔事务。**

还要注意一个十分容易误解的细节：**这里的重复主键错误并不意味着 MySQL 自动回滚了整个事务。**通常情况下，重复键错误只使当前 INSERT 语句失败；之前成功的 UPDATE 仍在当前事务中，直到我们显式 `ROLLBACK` 或作出其他事务结束决定。不要在失败后误执行 `COMMIT`。

如果你使用的是执行一段脚本时遇错就退出的客户端，应该在可继续交互的客户端会话中逐条运行，确保 `ROLLBACK` 真正发出；生产应用则应在异常处理分支中执行回滚。

### 再做一次成功购买

这次换一个不存在的订单号：

```sql
START TRANSACTION;

UPDATE products
SET stock = stock - 1
WHERE id = 42 AND stock > 0;

SELECT ROW_COUNT() AS updated_rows;

INSERT INTO orders (order_id, product_id, quantity)
VALUES (9002, 42, 1);

COMMIT;
```

假设 UPDATE 影响一条记录，并且 INSERT 成功，提交后再查询：

```sql
SELECT stock FROM products WHERE id = 42;
SELECT COUNT(*) AS order_9002_exists
FROM orders WHERE order_id = 9002;
```

预期：

```text
stock = 1
order_9002_exists = 1
```

比较这三种运行方式：

| 执行方式 | 订单插入 | 库存最终结果 | 本次新增订单 |
| --- | --- | --- | --- |
| 两条语句各自自动提交 | 失败 | 1 | 无 |
| 一笔事务，失败后 `ROLLBACK` | 失败 | 2 | 无 |
| 一笔事务，全部成功后 `COMMIT` | 成功 | 1 | 有 |

现在我们知道事务在业务上解决了什么。但新的问题也随之出现：

**在 `ROLLBACK` 之前，库存确实已经从 2 改成了 1。InnoDB 后来凭什么知道要恢复成 2？**

## 4. 先自己实现一次撤销：如果没有旧信息，就无法准确恢复

回到最小 Python 商品系统。

```python
products = {42: {"stock": 2}}
orders = {9001: {"product_id": 42, "quantity": 1}}
```

假设我们先修改了商品：

```python
products[42]["stock"] -= 1
```

现在内存中的值已经是 `1`。

如果接着创建订单时失败，单看此刻的商品对象，我们不知道本次购买之前库存究竟是多少。它可能原来是 `2`，也可能某个操作把库存改成了别的值。

因此，在修改之前，我们至少需要记下一份足够撤销这次修改的信息：

```python
old_stock = products[42]["stock"]
products[42]["stock"] -= 1
```

当操作失败：

```python
products[42]["stock"] = old_stock
```

这个方案在最小单线程案例里已经能工作。但如果一笔事务有很多次修改，我们需要记住每一步以及它们的执行顺序。

最自然的办法，就是为本次事务维护一组**撤销记录**：修改之前先记下怎样恢复；失败时再逐步撤销。

### 一个真正可以运行的最小撤销实验

下面是完整教学代码。它只处理一个 Python 进程中、按顺序执行的两张字典操作，不模拟 InnoDB 页面、锁、并发或崩溃恢复。

```python
products = {42: {"stock": 2}}
orders = {9001: {"product_id": 42, "quantity": 1}}


def buy(order_id, use_undo):
    undo = []
    try:
        if products[42]["stock"] <= 0:
            return "OUT_OF_STOCK"

        old_stock = products[42]["stock"]
        products[42]["stock"] -= 1
        if use_undo:
            undo.append(("restore_stock", 42, old_stock))

        if order_id in orders:
            raise ValueError("duplicate order id")

        orders[order_id] = {"product_id": 42, "quantity": 1}
        if use_undo:
            undo.append(("delete_order", order_id))

        undo.clear()  # 教学模型的“提交”
        return "OK"

    except ValueError:
        for action in reversed(undo):
            if action[0] == "restore_stock":
                products[action[1]]["stock"] = action[2]
            elif action[0] == "delete_order":
                del orders[action[1]]
        return "FAILED"


print("无撤销：", buy(9001, use_undo=False), products[42]["stock"])
products[42]["stock"] = 2
print("有撤销：", buy(9001, use_undo=True), products[42]["stock"])
print("提交成功：", buy(9002, use_undo=True), products[42]["stock"])

assert products[42]["stock"] == 1
assert 9001 in orders and 9002 in orders
```

**已实际运行，输出：**

```text
无撤销： FAILED 1
有撤销： FAILED 2
提交成功： OK 1
```

第一轮没有撤销记录，订单失败时，库存已经错误地减成了 `1`。

第二轮记录了修改前的值，订单失败时，按撤销记录恢复库存到 `2`。

第三轮没有失败，两个修改都保留，库存最终变成 `1`。

这里最重要的并不是 `undo` 这个列表，而是两个设计动作：

1. **修改发生前，保留足以撤销的信息。**否则等到失败时，未必知道原状态是什么。
2. **把这些信息与当前事务关联起来。**否则回滚时不知道应该撤销哪些动作，也不知道哪些修改已经属于另一笔成功的事务。

到这里，`Undo` 才有明确的存在理由。

不过，我们的教学列表距离真正的 InnoDB 还有很大差距：真实记录可能存在索引中，修改也可能影响多个索引项；并发事务不能简单地通过赋值旧值来恢复。这些都需要更精确的记录格式与回滚过程。

现在进入源码，看 InnoDB 怎样做。

## 5. InnoDB 为什么在修改时要生成 Undo 信息？

本章固定源码版本为 **MySQL 8.4.0，Git 标签 `mysql-8.4.0`**。

InnoDB 官方文档把 Undo Log 描述为与读写事务关联的一组撤销日志记录。对于聚簇索引记录的修改，它保存足以帮助撤销改动、以及在需要时还原历史版本的数据。它并不是“把整个数据库复制一份快照”。

本章先只用它解释**回滚**。它与一致性读取的关系留到下一章，因为读者还没有碰到“两个事务看到不同版本”的问题。

从上一章的数据页修改继续考虑一次 `UPDATE`：

```text
找到商品 42
    ↓
准备更新记录
    ↓
需要留下本次修改的撤销信息
    ↓
库存字段发生修改
```

这里的因果关系是：**如果修改后还要保留放弃本次修改的能力，那么保存撤销信息不能等到出错以后才做。**

在 MySQL 8.4 的索引记录更新路径中，可以沿着 `storage/innobase/btr/btr0cur.cc` 研究 `btr_cur_upd_lock_and_undo()`：该函数处理更新时的锁检查和 Undo 记录写入，并将回滚指针传给后续更新过程。进一步涉及的 Undo 记录写入函数为 `trx_undo_report_row_operation()`（相关实现在 `storage/innobase/trx/trx0rec.cc`）。

这里先不展开它们的完整参数列表。我们只需要看到“更新记录时，必须把撤销信息与当前事务的修改关联”这个设计动作。真实实现还需要更新记录中的事务相关系统字段，以便在后续处理时找到相应的版本信息。

但仅仅知道修改时会留下信息还不够。

**用户执行 `ROLLBACK` 后，谁真正把这些信息拿出来，并用它们撤销更新？**

## 6. 从 `ROLLBACK` 进入真实 InnoDB 回滚路径

在 MySQL Server 层，事务回滚请求最终需要交给事务性存储引擎处理。对 InnoDB 来说，我们可以从以下文件定位：

```text
storage/innobase/trx/trx0roll.cc
```

对应源码：[MySQL 8.4.0 · trx0roll.cc](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/trx/trx0roll.cc)

这里有一个明确的入口：

```cpp
// 真实函数名：MySQL 8.4.0
trx_rollback_for_mysql(trx_t *trx)
```

调用它的，是上层进入 InnoDB 事务回滚处理的路径。它接收的 `trx` 是当前需要回滚的事务对象，而不是 SQL 文本，也不是客户端自己保存的旧库存。

继续顺着函数进入 `trx_rollback_low()`，在活动事务的正常完整回滚路径中，会调用 `trx_rollback_for_mysql_low()`。我们只截取内部最关键的一句：

**真实源码节选：`trx0roll.cc`，`trx_rollback_for_mysql_low()`**

```cpp
trx_rollback_to_savepoint_low(trx, nullptr);
```

这里的第二个参数是 `nullptr`。

为什么值得注意？因为这个内部函数既可以处理回滚到指定位置，也可以处理完整回滚。这里传入空的保存点指针，表示**回滚整笔事务，而不是只撤销一部分修改**。

现在，InnoDB 已经知道了要回滚的是哪笔事务，以及这次需要回滚到哪里。

但它还必须找出那笔事务曾经做过什么修改。

## 7. 真正取出撤销记录：`row_undo()`

继续打开：

[MySQL 8.4.0 · `storage/innobase/row/row0undo.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/row/row0undo.cc)

在 `row_undo_step()` 中，会调用 `row_undo()`，让它处理一次撤销步骤。

`row_undo()` 中最关键的行为，是从当前事务可撤销的日志记录中取得下一条需要处理的信息。

**真实源码节选（省略了换行、无关参数说明）：**

```cpp
node->undo_rec = trx_roll_pop_top_rec_of_trx(
    &trx, trx.roll_limit, &roll_ptr, node->heap);
```

我们自己的教学代码用的是：

```python
for action in reversed(undo):
    ...
```

InnoDB 并没有使用 Python 列表，但两者都在回答同一个问题：**当前事务还有哪些修改需要撤销？下一条应处理哪份撤销信息？**

这里的 `trx` 来自当前事务，`undo_rec` 保存取出的 Undo 记录，`roll_ptr` 与记录定位相关；`roll_limit` 则与回滚范围有关。

如果没有更多需要处理的 Undo 记录，`row_undo()` 就会结束对应的回滚步骤。

但有一个新问题：从 Undo Log 中取出的记录，可能对应不同类型的数据库操作。

例如，更新了库存是一种操作；插入了订单则是另一种操作。恢复它们的方式显然不可能完全一样。

源码也确实对这两类情况进行了区分。

**真实源码关键分支：`row0undo.cc`，`row_undo()`**

```cpp
if (trx_undo_roll_ptr_is_insert(roll_ptr)) {
    node->state = UNDO_NODE_INSERT;
} else {
    node->state = UNDO_NODE_MODIFY;
}
```

对于插入，后续进入 `row_undo_ins()` 路径；对于修改，则进入 `row_undo_mod()` 路径。

**这正是我们需要的设计差异：**

- 如果一条尚未提交的记录被插入，撤销时需要处理这条插入记录。
- 如果一条记录被修改，撤销时需要依据保存的版本/修改信息恢复相应状态。

回到当前失败案例，订单 `9001` 因为主键冲突而没有成功插入新记录；需要被撤销的关键业务改动，是前面已经成功执行的库存 UPDATE。

因此，在这个例子中，我们重点关注修改类 Undo 的恢复路径，而不必提前把插入撤销的所有索引细节都展开。

现在，真实源码已经给出了一条可解释的回滚主干：

```text
用户明确执行 ROLLBACK
          │
          ▼
上层把事务回滚交给 InnoDB
          │
          ▼
trx_rollback_for_mysql()
          │
          ▼
trx_rollback_low()
          │
          ▼
trx_rollback_for_mysql_low()
          │
          ▼
trx_rollback_to_savepoint_low(trx, nullptr)
          │
          ▼
调度 Undo 处理步骤
          │
          ▼
row_undo_step() → row_undo()
          │
          ▼
获取 Undo 记录
          │
          ▼
按插入 / 修改类型撤销
```

**图示性质：已核对的关键调用关系的教学化简。**它省略了回滚任务组织、内部执行线程以及各类分支，不能冒充调试器打印的完整调用栈。

经过这一轮源码阅读，我们不再只是说“事务失败会回滚”。我们已经知道：**回滚必须找到当前事务的修改历史，再通过真正的 Undo 执行逻辑去撤销这些修改。**

## 8. 三个容易混淆的边界

### 8.1 `ROLLBACK` 不是把内存页面简单丢掉

上一章已经知道，`UPDATE` 可能改动 Buffer Pool 中的记录所在页面。

回滚时，不能直接扔掉整个缓存页。因为同一个页面可能包含许多条记录，其他数据也可能需要保留。

InnoDB 需要针对当前事务留下的修改信息执行撤销，而不是粗暴地恢复整页到某个旧快照。

### 8.2 Undo 不负责解决所有崩溃恢复问题

我们当前关心的是：**事务决定放弃时，怎样恢复未提交的修改。**

这与“客户端收到 COMMIT 成功后，突然断电怎样保留已提交结果”不是同一个问题。后一个问题需要理解 Redo、日志持久化和崩溃恢复路径，后续再展开。

Undo 和 Redo 有关联，但不能混为一谈：前者提供撤销与历史版本所需的信息，后者主要服务于 InnoDB 物理修改的重做与崩溃恢复。

### 8.3 数据库事务不会撤销外部世界的一切

如果事务中修改了 InnoDB 的两张表，`ROLLBACK` 可以撤销这笔事务的相应数据库修改。

但如果应用程序在 `COMMIT` 前已经发出短信、调用外部支付接口、发送不可撤回的消息，这些外部动作不会因为 MySQL `ROLLBACK` 就自动消失。

另外，本章实验只使用 InnoDB 表。对于不支持事务的存储引擎或部分会触发隐式提交的 SQL 语句，不能照搬相同保证。

因此，**我们今天解决的是一笔 InnoDB 事务内数据库操作的共同提交或撤销，不是跨所有外部系统的全局原子性。**

## 9. 把这一章放回整次购买请求

最初，一次购买只有一句：

```sql
UPDATE products
SET stock = stock - 1
WHERE id = 42 AND stock > 0;
```

它可以在单条语句层面正确协调库存扣减。

随后，我们加入创建订单：

```sql
INSERT INTO orders (order_id, product_id, quantity)
VALUES (9002, 42, 1);
```

这时，新的业务约束出现了：**要么有库存扣减和对应订单，要么两者都不成为这次购买的最终结果。**

我们先尝试让两条语句各自自动提交，亲眼看到库存已经减少而订单失败的情况。

之后，再引入事务边界，使用 `COMMIT` 表示整组操作成功、使用 `ROLLBACK` 放弃未提交修改。

但既然真正的记录可能已经发生改变，回滚就必须有依据。于是，在最小模型中，我们先保存修改前的信息；在 InnoDB 中，又找到了 Undo 记录与回滚处理的真实源码路径。

整条因果链可以简化为：

```text
只有库存 UPDATE
      │
      ▼
单条语句可以正确扣减
      │
      ▼
增加订单 INSERT
      │
      ▼
第二步失败，第一步却已提交
      │
      ▼
需要共同成功或失败的事务边界
      │
      ▼
事务内的修改已经发生，怎样撤销？
      │
      ▼
修改时保留 Undo 信息
      │
      ▼
ROLLBACK 找到并撤销对应记录
```

这就是本章唯一需要掌握的新机制：**事务提供业务操作的共同提交/回滚边界，而 Undo 让对已修改数据的撤销有据可依。**

## 10. 下一章：订单还没提交，另一个用户能读到什么？

现在，再次想象两个用户同时访问商品 `42`。

事务 A 已经扣减库存：

```sql
START TRANSACTION;
UPDATE products
SET stock = stock - 1
WHERE id = 42 AND stock > 0;
-- 暂时不提交
```

事务 B 此时只想执行：

```sql
SELECT stock FROM products WHERE id = 42;
```

事务 B 应该看到扣减前的库存，还是扣减后的库存？是否必须等待事务 A？

刚才我们知道，InnoDB 为修改保留了能够恢复旧状态的信息。

一个自然的问题随之出现：**如果事务 A 还没有提交，事务 B 能否利用这些历史信息读取它应该看到的数据，而不必一直等待写操作结束？**

下一章，我们将围绕这个可观察的双会话场景，引入一致性读取与 MVCC。我们会先从两个会话的 `SELECT` 结果出发，再到真实源码里解释记录版本是怎样被判断和重建的。

**下一章：《库存正在修改，其他用户能看到什么——为什么需要 MVCC？》**

---

## 源码与实验说明

**固定源码：** MySQL 8.4.0，Git Tag `mysql-8.4.0`。

| 源码位置 | 在本章回答的问题 |
| --- | --- |
| [`storage/innobase/trx/trx0roll.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/trx/trx0roll.cc) | 回滚请求怎样进入事务回滚主路径？ |
| [`storage/innobase/row/row0undo.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/row/row0undo.cc) | 怎样提取 Undo 记录，并分派插入/修改的撤销动作？ |
| [`storage/innobase/btr/btr0cur.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/btr/btr0cur.cc) | 聚簇索引更新时为何需要记录撤销信息？ |
| [`storage/innobase/trx/trx0rec.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/trx/trx0rec.cc) | Undo 记录如何生成？（本章仅定位入口，不展开编码细节） |

**官方行为文档：**

- [MySQL 8.4 — START TRANSACTION、COMMIT 与 ROLLBACK](https://dev.mysql.com/doc/refman/8.4/en/commit.html)
- [MySQL 8.4 — InnoDB Undo Logs](https://dev.mysql.com/doc/refman/8.4/en/innodb-undo-logs.html)

**验证状态：**

- Python 教学实验：已在本章写作环境中执行，输出和断言通过。它验证的是撤销记录的教学机制，不是 MySQL 内部实现。
- MySQL SQL 实验：提供了可复现步骤及预期结果，**没有在真实 MySQL 8.4.0 服务中运行**，因此没有把预期输出冒充成实际执行记录。
- 源码阅读：已对照 MySQL 8.4.0 标签核对回滚主函数、获取 Undo 记录以及修改/插入撤销分支；其他超出本章范围的内部调用细节没有臆造。
