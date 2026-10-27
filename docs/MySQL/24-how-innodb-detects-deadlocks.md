---
title: "24｜从锁等待到死锁：MySQL 怎样发现循环等待？"
sidebar_position: 25
slug: /mysql/24-how-innodb-detects-deadlocks
description: 从两笔购买事务相反的商品更新顺序出发，构造真实死锁，沿 MySQL 8.4.0 的等待关系快照、环检测与回滚通知源码追踪 InnoDB 怎样恢复执行。
tags: [MySQL, InnoDB, 死锁, 事务, 源码精读]
---

# 从锁等待到死锁：MySQL 怎样发现循环等待？

上一章，我们已经知道：为了保护某个索引范围，InnoDB 有时不仅要锁住已有记录，还需要保护记录之间的间隙。

这些锁都在帮助事务避免错误的并发修改。但锁也带来一个新问题：**如果一个事务必须等待另一个事务释放锁，数据库怎样知道它只是暂时等待，还是永远不可能等到？**

我们仍然使用商品购买的场景。这次，一张订单需要同时扣减两种商品的库存。

```sql
-- 事务 A 的购买请求
UPDATE products SET stock = stock - 1 WHERE id = 10 AND stock > 0;
UPDATE products SET stock = stock - 1 WHERE id = 20 AND stock > 0;
```

另一个请求也购买了这两种商品，但更新顺序相反：

```sql
-- 事务 B 的购买请求
UPDATE products SET stock = stock - 1 WHERE id = 20 AND stock > 0;
UPDATE products SET stock = stock - 1 WHERE id = 10 AND stock > 0;
```

如果只考虑一个事务，以上代码没有新鲜之处。前面已经学过：`UPDATE` 会找到记录，使用必要的锁保护修改，并在事务提交或回滚时完成相应处理。

**但当两条购买请求交错执行，会发生什么？**

## 1. 单独执行时，两个事务都没有问题

为了避免干扰之前商品 `42` 的实验，我们在专用实验库中准备两件商品：

```sql
CREATE DATABASE IF NOT EXISTS mysql_source_lab;
USE mysql_source_lab;

DROP TABLE IF EXISTS deadlock_products;

CREATE TABLE deadlock_products (
    id BIGINT PRIMARY KEY,
    stock INT NOT NULL
) ENGINE = InnoDB;

INSERT INTO deadlock_products (id, stock)
VALUES (10, 5), (20, 5);
```

假设事务 A 按顺序执行：

```sql
START TRANSACTION;

UPDATE deadlock_products
SET stock = stock - 1
WHERE id = 10 AND stock > 0;

UPDATE deadlock_products
SET stock = stock - 1
WHERE id = 20 AND stock > 0;

COMMIT;
```

两个商品的库存都从 `5` 变为 `4`。没有其他事务竞争时，程序正常结束。

但数据库通常不只有一个连接。让另一个事务在 A 尚未完成时开始修改，就会产生与单用户实验完全不同的运行过程。

## 2. 两个事务如何把彼此困住？

先只看时间顺序，不讨论源码。

1. A 开始事务，修改商品 `10`，持有对应记录的排他锁。
2. B 开始事务，修改商品 `20`，持有对应记录的排他锁。
3. A 继续修改商品 `20`，但这条记录正被 B 锁住；A 等待 B。
4. B 继续修改商品 `10`，但这条记录正被 A 锁住；B 等待 A。

对应关系是：

```text
事务 A：已持有商品 10 的锁 ──── 等待商品 20 的锁（B 持有）
         ▲                                      │
         │                                      ▼
事务 B：等待商品 10 的锁（A 持有） ──── 已持有商品 20 的锁
```

把它缩成事务之间的关系：

```text
A  ──等待──>  B
^             │
│             │
└────等待─────┘
```

注意这与普通锁等待有本质不同。

普通等待是 `A → B`：只要 B 完成并释放锁，A 就可能继续执行。

现在是 `A → B → A`：A 必须等 B，B 又必须等 A。而两者都无法先完成后释放自己持有的锁。

这就叫作**死锁（Deadlock）**。

这里我们终于有理由引入“等待关系图”：它并不是为了给文章增加一个新术语，而是因为只检查“某个锁有没有被占用”已经不足以回答“系统能否继续前进”。

## 3. 最小方案：把等待关系写成一张图

先不考虑真实 InnoDB 如何储存锁。我们用 Python 字典表示“哪个事务在等待哪个事务”：

```python
waits_for = {
    "A": "B",  # A 正在等待 B
    "B": "A",  # B 正在等待 A
}
```

为了判断有没有死锁，只需从一个事务开始，沿“正在等待谁”不断向后追踪。

如果最终走到了没有等待对象的事务，暂时没有发现循环。

如果在同一次追踪里重新遇到已经访问过的事务，就发现了一个环。

**教学化简：一个最小环检测器**

```python
def find_cycle(edges):
    for start in edges:
        seen = {}
        path = []
        node = start

        while node is not None:
            if node in seen:
                return path[seen[node]:]

            seen[node] = len(path)
            path.append(node)
            node = edges.get(node)

    return []


assert find_cycle({"A": "B", "B": None}) == []
assert find_cycle({"A": "B", "B": "A"}) == ["A", "B"]
```

这个模型只处理“每个等待事务最多指向一个阻塞事务”的简单关系，但已足以识别我们的两笔购买请求。

复杂数据库中，还需要处理事务状态变化、等待队列、锁兼容性及并发修改。这些能力并不是这十几行 Python 所具备的。

**最小模型揭示的需求是：数据库需要记录事务的阻塞关系，并检查它是否形成循环。**

接下来去 MySQL 8.4.0 源码中寻找证据。

## 4. 真实源码：InnoDB 从哪里获得“谁在等谁”？

本章固定源码版本：MySQL Server **8.4.0**，Git Tag **`mysql-8.4.0`**。

本章主要阅读的文件是：

[`storage/innobase/lock/lock0wait.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/lock/lock0wait.cc)

先看函数 `lock_wait_snapshot_waiting_threads()`。它并不是直接扫描 `products` 表，也不读取商品库存；它关心的是**哪些事务正在等待，以及谁在阻塞它们**。

源码中，每条等待信息由 `waiting_trx_info_t` 保存，关键字段分别是：

- `trx`：正在等待的事务。
- `waits_for`：造成等待的事务。
- `slot`：当前等待槽位。
- `reservation_no`：帮助确认槽位是否已经重新分配的序号。

在取得等待信息时，源码读取等待事务的 `blocking_trx`，再把等待方和阻塞方组成一条记录。

[查看快照函数的真实源码](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/lock/lock0wait.cc#L516-L554)

这一层回答了前面的第一个问题：

> MySQL 不是根据两条 SQL 的文字顺序猜测死锁，而是根据当前实际存在的事务等待状态分析它。

不过，快照里的记录还不是我们画出的 `A → B → A` 图。

需要把事务对应起来，才能知道这些等待关系是否形成环。

### 4.1 为什么还需要构造等待图？

接下来是同一个文件中的 `lock_wait_build_wait_for_graph()`。

它接收等待快照，并构造一个数组 `outgoing`。如果等待列表中的第 `i` 个事务被列表中第 `j` 个事务阻塞，那么 `outgoing[i]` 就指向 `j`。

如果阻塞者不在当前等待事务集合中，则使用 `-1` 表示没有下一条可追踪的边。

例如：

```text
infos[0] = 事务 A，等待 B
infos[1] = 事务 B，等待 A

outgoing[0] = 1
outgoing[1] = 0
```

我们现在可以把源码中的数组重新翻译成读者熟悉的关系：

```text
0 → 1 → 0

等价于

A → B → A
```

这里有一个容易误解的细节：此处构建的是针对当前等待事务的**等待关系子图**，不是把 InnoDB 的所有锁对象、所有兼容性关系完整展开成一个全局图。

该函数的源码说明明确指出：对每个等待事务，构造的关系至多有一条向外的边。

[查看 `lock_wait_build_wait_for_graph()`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/lock/lock0wait.cc#L596-L641)

为什么这样仍然有价值？

因为在当前阻塞关系中，沿着每个事务的实际阻塞对象继续追踪，就有机会发现使它们无法自行前进的等待环。

**此时才有必要进行环检测。**

## 5. 真实源码：怎样在等待图中找到环？

继续追踪 `lock_wait_find_and_handle_deadlocks()`。

它读取前面建立的 `outgoing` 关系，遍历尚未处理过的事务，再顺着边向后查找。

其中的 `colors` 用于标记当前遍历过程是否已经访问过某个节点。

当一条遍历路径再次遇到当前路径中的节点，就得到一个候选环。

这个思想与上面的 Python `seen` 集合很接近：

```text
从 A 出发：

A → B → A
        ↑
        已经访问过 A

发现候选环
```

这里用“候选”两个字，而不是直接称为最终死锁，是因为**真实数据库的等待状态会并发变化**。

例如：

1. 死锁检测线程刚取得等待关系快照。
2. 某个事务提交，释放了锁。
3. 原本被阻塞的事务已经可以继续运行。
4. 检测线程仍然可能在旧快照中看到之前的环。

如果只要快照里出现环，就不加检查地回滚事务，就可能把已经自然解除的等待误判成真实死锁。

因此，在检测到候选环之后，MySQL 还会进入 `lock_wait_check_candidate_cycle()`。

它会检查：

- 等待槽位是否仍属于快照中的原事务，避免槽位回收重用带来的误判。
- 相关事务是否仍处于等待锁的状态。
- 如果等待确实仍然存在，再进一步选择要回滚的事务。

[查看候选环检测与再次确认](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/lock/lock0wait.cc#L1094-L1158)

这一步不仅仅是“代码更严谨”。它回答了一个真实并发系统必须面对的问题：

**观察到的等待关系可能在观察完成后已经发生变化。**

现在，我们有了可靠一些的环判断。

但即使发现了死锁，数据库又该怎么做？

## 6. 发现死锁后，为什么不是让两个事务都继续等？

假设确认存在：

```text
A → B → A
```

如果继续等待，A 和 B 都无法完成。

如果同时回滚两者，当然可以解除死锁，但可能浪费不必要的工作。

因此，最直接的办法是：**选择其中一个事务放弃，让另一个事务继续。**

例如，选择事务 B 为回滚对象：

```text
原来：

A → B → A

B 被回滚，释放商品 20 的锁

A 获得商品 20 的锁

A 继续完成两个商品的扣减
```

这就引入了“死锁牺牲者（victim）”的概念。

但是，谁应该成为 victim？

### 6.1 真实 MySQL 怎样选择？

在 `lock0wait.cc` 中，`lock_wait_choose_victim()` 负责从死锁环中的事务里选择回滚对象。

它会参考事务权重，并在相关情况下考虑高优先级事务的仲裁逻辑。其主要目标之一，是尽量减少回滚代价；官方文档也指出，InnoDB 倾向于回滚较小的事务。

[查看 `lock_wait_choose_victim()`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/lock/lock0wait.cc#L853-L898)

所以，不能把“最后申请锁的事务必然被回滚”当作 MySQL 的规则。

在我们的双会话实验中，事务 A 或 B 都可能收到死锁错误，**不要事先假定一定是 B**。

### 6.2 选择 victim 是否意味着在检测线程中直接执行完整回滚？

这里特别值得精读。

InnoDB 的 `lock_wait_rollback_deadlock_victim()` 不是简单地在死锁检测线程里同步完成所有 Undo 处理。

它会标记被选中的事务，并取消相关等待，使被选中的执行线程能够以死锁错误继续处理后续的事务回滚路径。

**真实源码中的关键赋值：**

```cpp
chosen_victim->lock.was_chosen_as_deadlock_victim = true;
```

[查看 victim 通知代码](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/lock/lock0wait.cc#L642-L655)

在 `lock_wait_suspend_thread()` 及等待线程被唤醒的相关路径里，这个标记会使事务进入 `DB_DEADLOCK` 错误处理。

[查看等待线程如何获知死锁](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/lock/lock0wait.cc#L188-L325)

这里解决了一个很实际的设计问题：

死锁检测负责判断并发关系、选择需要中止的事务；真正撤销这个事务先前的修改，则由事务错误处理和回滚路径完成。

这也把本章和第 09 章的 Undo 联系起来了：

**死锁检测决定谁必须放弃，Undo 支持撤销这个事务已经完成但尚未提交的修改。**

现在，我们可以亲手验证一次完整的解除过程。

## 7. 一个可运行的最小死锁模型

下面这段 Python 是**教学模拟**，不是 InnoDB 源码。它故意省去了真实记录锁队列、线程调度、日志和 MVCC，只保留本章需要观察的行为：持锁、等待、形成环、选择一个事务回滚、另一个继续。

```python
class ToyDB:
    def __init__(self):
        self.stock = {10: 5, 20: 5}
        self.owner = {}
        self.undo = {}

    def lock(self, tx, product):
        owner = self.owner.get(product)
        if owner is None or owner == tx:
            self.owner[product] = tx
            return True
        return False

    def dec(self, tx, product):
        assert self.owner[product] == tx
        self.undo.setdefault(tx, []).append(
            (product, self.stock[product])
        )
        self.stock[product] -= 1

    def release(self, tx):
        for product, owner in list(self.owner.items()):
            if owner == tx:
                del self.owner[product]

    def rollback(self, tx):
        for product, previous in reversed(self.undo.pop(tx, [])):
            self.stock[product] = previous
        self.release(tx)

    def commit(self, tx):
        self.undo.pop(tx, None)
        self.release(tx)


def find_cycle(edges):
    for start in edges:
        seen = {}
        path = []
        node = start
        while node is not None:
            if node in seen:
                return path[seen[node]:]
            seen[node] = len(path)
            path.append(node)
            node = edges.get(node)
    return []


assert find_cycle({"A": "B", "B": None}) == []
assert find_cycle({"A": "B", "B": "A"}) == ["A", "B"]

db = ToyDB()
assert db.lock("A", 10)
db.dec("A", 10)
assert db.lock("B", 20)
db.dec("B", 20)

# A 想拿 20，但 B 持有；B 想拿 10，但 A 持有。
assert not db.lock("A", 20)
assert not db.lock("B", 10)

print("等待关系: A -> B, B -> A")
print("环路:", " -> ".join(find_cycle({"A": "B", "B": "A"}) + ["A"]))

# 为了可重复验证，教学模型固定选择 B 为 victim。
db.rollback("B")
assert db.lock("A", 20)
db.dec("A", 20)
db.commit("A")

assert db.stock == {10: 4, 20: 4}
assert db.owner == {}
print("回滚 B 后 A 提交:", db.stock)
print("全部断言通过")
```

这段代码已实际运行，输出为：

```text
等待关系: A -> B, B -> A
环路: A -> B -> A
回滚 B 后 A 提交: {10: 4, 20: 4}
全部断言通过
```

理解这个过程时，最容易忽略的是 `rollback("B")` 的效果。

B 最初已经把商品 `20` 从 `5` 暂时改成 `4`，但 B 没有提交；它被回滚后，商品 `20` 恢复到 `5`，并释放锁。

随后 A 才获得商品 `20` 的锁，把库存减到 `4`，最后提交。

所以最终两个商品都是 `4`，并不是因为 A、B 各自在部分商品上成功提交，而是因为**只有 A 的完整购买事务提交成功**。

这个模型验证了等待环检测与撤销后继续执行的逻辑，但不验证 InnoDB 在真实并发环境中的线程调度或 victim 选择策略。

## 8. 真正的 MySQL 双会话实验

现在切换到真实 MySQL 8.4。以下操作需要两个独立客户端连接，分别称为会话 A 和会话 B，最好再准备第三个观察会话 C。

仅在实验数据库中运行；不要在生产表里执行本节测试。

**准备数据（任选一个会话，确保没有旧的未提交事务）：**

```sql
CREATE DATABASE IF NOT EXISTS mysql_source_lab;
USE mysql_source_lab;

DROP TABLE IF EXISTS deadlock_products;
CREATE TABLE deadlock_products (
    id BIGINT PRIMARY KEY,
    stock INT NOT NULL
) ENGINE=InnoDB;
INSERT INTO deadlock_products VALUES (10, 5), (20, 5);

SHOW VARIABLES LIKE 'innodb_deadlock_detect';
```

本实验需要 `innodb_deadlock_detect = ON`；它通常是默认值。如果不同，请在有权限、且不影响其他业务的隔离实验实例中调整，不建议在共享实例随意改动全局配置。

**会话 A：**

```sql
USE mysql_source_lab;
START TRANSACTION;
UPDATE deadlock_products SET stock=stock-1 WHERE id=10 AND stock>0;
```

此时 A 已经修改商品 `10`，事务尚未提交，相关排他锁仍被持有。

**会话 B：**

```sql
USE mysql_source_lab;
START TRANSACTION;
UPDATE deadlock_products SET stock=stock-1 WHERE id=20 AND stock>0;
```

此时 B 修改商品 `20`，也尚未提交。

**再回到会话 A，执行：**

```sql
UPDATE deadlock_products SET stock=stock-1 WHERE id=20 AND stock>0;
```

不要等待其返回才执行下一步。这条语句会因为 B 持有商品 `20` 的冲突锁而等待；保留 A 的请求在等待状态。

**立即切到会话 B，执行：**

```sql
UPDATE deadlock_products SET stock=stock-1 WHERE id=10 AND stock>0;
```

现在 A 等 B，B 又等 A，形成刚才研究的环。

如果检测正常开启，预期其中一个会话收到类似错误：

```text
ERROR 1213 (40001): Deadlock found when trying to get lock; try restarting transaction
```

这条错误并不必然出现在会话 B。实际 victim 由数据库选择。

另一个事务在对方回滚并释放锁后，通常能够继续完成等待的 UPDATE。此时，需要**在存活的会话明确执行 `COMMIT`**；在收到 `1213` 的会话中，整笔 InnoDB 事务已经被回滚，不要把它前面执行的 UPDATE 当作已提交。

最后重新查询：

```sql
SELECT id, stock FROM deadlock_products ORDER BY id;
```

如果存活的事务已经顺利完成两次更新并提交，而失败事务没有重新执行，则预期两个库存都从 `5` 变为 `4`。

此实验需要人手协调两个终端的执行顺序。这里没有在真实 MySQL 服务器中实际运行，因此错误出现的具体会话、等待时长和监控输出都没有伪造为既成结果。

## 9. 怎么看 MySQL 记录的死锁原因？

如果我们只看到 `ERROR 1213`，仍然不知道数据库检测到了怎样的等待关系。

使用第三个会话，执行：

```sql
SHOW ENGINE INNODB STATUS\G
```

观察 `LATEST DETECTED DEADLOCK` 部分。它通常会展示涉及死锁的事务、持有的锁、等待的锁以及最终回滚哪个事务。

可以重点寻找：

```text
LATEST DETECTED DEADLOCK
...
*** WE ROLL BACK TRANSACTION (...)
```

这里是**输出格式示意**，不是本章实际运行产生的死锁报告。报告中的事务编号、索引、记录和等待细节需要以你的实例输出为准。

如果没有观察到目标报告，可能是它已经被后续死锁记录覆盖；还应注意查看完整 InnoDB 状态通常需要相应权限。

对于持续排查，可以在有权限的测试环境中考虑 `innodb_print_all_deadlocks`。它能够将检测到的死锁信息写入错误日志，但不应为了本教程在生产环境中随意开启或修改全局诊断配置。

相关官方文档：[Deadlocks in InnoDB](https://dev.mysql.com/doc/refman/8.4/en/innodb-deadlocks.html)。

## 10. 死锁与普通锁等待超时，为什么不是一回事？

现在，考虑另一种情形。

A 持有商品 `10` 的锁，B 等待商品 `10`，但 A 并没有等待 B。

```text
B → A
```

这只是单向等待。

如果 A 一直不提交，B 可能等到超过 `innodb_lock_wait_timeout`，然后收到锁等待超时错误。

但这并不表示存在死锁，因为 A 并未被 B 挡住，仍可能继续执行并提交。

两种错误需要区分：

| 情况 | 典型错误 | InnoDB 默认回滚范围 |
|---|---|---|
| 检测到死锁 | `1213` | 被选中事务整体回滚 |
| 锁等待超时 | `1205` | 通常仅回滚当前失败语句 |

锁等待超时的回滚范围可以受 `innodb_rollback_on_timeout` 影响。不能因为一次 UPDATE 返回了 `1205`，就假设同一事务里之前的修改已经全部撤销。

另外，死锁检测可以通过 `innodb_deadlock_detect` 关闭。关闭后，即使形成循环，InnoDB 也不再依靠这一路径主动检测，而需要依赖锁等待超时等机制打破僵局。

**这说明死锁检测解决的是循环等待识别与解除，不是所有慢查询或长时间等待的通用解决方案。**

## 11. 为什么应用程序仍然必须处理死锁？

既然数据库会自动选出一个事务回滚，应用程序是否可以完全忽略死锁？

不能。

对某个请求来说，数据库确实解除了死锁，但这个请求的购买事务可能已经失败。

例如 A 和 B 都代表真实订单，其中 B 被回滚。B 对应的用户仍然需要知道购买未完成，或者让应用在合适条件下重新执行购买逻辑。

这里需要重试的是**完整业务事务**，而不是拿某一条失败的 SQL 接着执行。

可以把应用侧流程理解为：

```python
# 概念伪代码：演示业务边界，并非可直接运行的驱动代码。
for attempt in range(MAX_RETRIES):
    try:
        begin_transaction()
        deduct_product(10)
        deduct_product(20)
        create_order()
        commit()
        break
    except DeadlockError:
        rollback_if_needed()
        # 整笔业务从头重试；需设置次数上限与合理退避。
else:
    report_purchase_failure()
```

真实代码需要识别驱动返回的 `1213 / SQLSTATE 40001`、处理异常传播，注意外部支付、消息发送等副作用不能因为数据库重试而重复执行。

可以采取的一项简单预防措施，是让不同购买请求按**相同的商品 ID 顺序**访问记录：

```text
事务 A：先 10，再 20
事务 B：先 10，再 20
```

此时 B 可能在获取商品 `10` 的锁时等待 A；但 B 尚未锁住商品 `20`，因此不会形成刚才那种“各自拿着一把锁等待对方”的环。

统一加锁顺序能够减少这类死锁，但不是所有死锁的万能解法。复杂范围操作、二级索引维护和其他表操作仍可能产生新的等待关系。

所以正确的工程策略是：**尽量减少死锁发生，同时始终能正确处理死锁发生后的整个事务重试。**

## 12. 源码路径回看：这次死锁怎样被发现和解除？

到这里，我们已经能够把整个过程重新放回一条因果链。

```text
事务 A 已更新商品 10          事务 B 已更新商品 20
         │                             │
         └── A 等 B；B 等 A ───────────┘
                         │
                         ▼
                锁等待关系被记录
                         │
                         ▼
       lock_wait_snapshot_waiting_threads()
                         │
                         ▼
         lock_wait_build_wait_for_graph()
                         │
                         ▼
       lock_wait_find_and_handle_deadlocks()
                         │
                         ▼
         lock_wait_check_candidate_cycle()
                         │
                         ▼
             lock_wait_choose_victim()
                         │
                         ▼
       lock_wait_rollback_deadlock_victim()
                         │
                         ▼
          victim 获得死锁错误并回滚
                         │
                         ▼
           另一个事务可以继续执行
```

这是围绕死锁检测逻辑整理的**教学主流程图**，不是逐帧、无分支的 C++ 调用栈；其中还省略了等待线程唤醒、回滚传播及调度状态更新。

另外，MySQL 8.4.0 中 `lock_wait_timeout_thread()` 会调用 `lock_wait_update_schedule_and_check_for_deadlocks()`，后者组织等待快照、图构造与死锁查找。这一流程会受到 `innobase_deadlock_detect` 开关影响。

[查看检测线程相关源码](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/lock/lock0wait.cc#L1298-L1378)

我们也应该回答这个机制**没有**解决什么：

- 它不会阻止所有死锁发生，只是在发现死锁时尝试解除。
- 它不会帮应用补做被回滚的订单，业务需要重试或返回失败。
- 它不能保证每次都回滚“刚进来的事务”。
- 它的检测范围受存储引擎及锁信息可见性约束；涉及其他存储引擎或特定表锁的等待，不应假定总能被 InnoDB 检测到。

理解这些边界后，死锁就不再只是数据库报出的一个神秘错误码。

它是多个事务的等待关系形成了环，而 InnoDB 必须主动牺牲一个事务，才能让其余事务重新前进。

## 13. 下一章：旧版本信息为什么不能一直留着？

本章的 victim 被回滚后，数据库利用事务的撤销机制恢复先前修改。

而在第 10 章和第 22 章，我们还看到 Undo 相关的历史信息可以帮助一致性读取构造旧版本。

于是，新的问题出现了：

假设商品 `42` 一天之内被修改很多次。

如果每次修改留下的历史信息都永久保存，数据库的历史数据只会越来越多。

但如果立即删除旧版本，又可能使某个尚未结束的长事务无法读取自己需要的快照。

**数据库怎样判断：哪些旧版本已经不可能再被任何活跃的一致性读取需要？**

下一章，我们沿着这个问题研究 InnoDB 的 Purge。

**下一章：第 25 章《从保留旧版本到 Purge——历史记录什么时候才能删除？》**

---

## 本章源码与实验说明

固定代码版本：MySQL 8.4.0，Git Tag `mysql-8.4.0`。

主要文件：[`storage/innobase/lock/lock0wait.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/lock/lock0wait.cc)。

关键阅读位置：

| 函数 | 本章用它回答什么问题？ |
|---|---|
| `lock_wait_snapshot_waiting_threads()` | 当前哪些事务正在等待、被谁阻塞？ |
| `lock_wait_build_wait_for_graph()` | 怎样把等待信息转成可追踪的图？ |
| `lock_wait_find_and_handle_deadlocks()` | 怎样沿等待图寻找候选循环？ |
| `lock_wait_check_candidate_cycle()` | 为什么发现候选环后还需要确认？ |
| `lock_wait_choose_victim()` | 为什么必须选一个事务放弃？ |
| `lock_wait_rollback_deadlock_victim()` | 怎样通知等待事务中止并进入回滚路径？ |
| `lock_wait_timeout_thread()` | 死锁检测如何被后台等待管理流程驱动？ |

官方资料：

- [Deadlocks in InnoDB](https://dev.mysql.com/doc/refman/8.4/en/innodb-deadlocks.html)
- [Deadlock Detection](https://dev.mysql.com/doc/refman/8.4/en/innodb-deadlock-detection.html)
- [How to Minimize and Handle Deadlocks](https://dev.mysql.com/doc/refman/8.4/en/innodb-deadlocks-handling.html)
- [InnoDB Error Handling](https://dev.mysql.com/doc/refman/8.4/en/innodb-error-handling.html)

**验证边界：** Python 教学实验已在本次编写过程中运行，三个核心行为断言通过；数据库双会话实验、死锁错误文本及 InnoDB 监控内容均作为可复现步骤和预期现象呈现，未声称在真实 MySQL 实例上执行。
