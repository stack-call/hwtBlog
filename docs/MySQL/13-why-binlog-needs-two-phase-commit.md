---
title: "13｜从本地提交到 Binlog：为什么需要协调两种日志？"
sidebar_position: 14
slug: /mysql/13-binlog-two-phase-commit
description: 沿着商品 42 的库存扣减，从第二台 MySQL 的同步需求出发，逐步理解 Binlog、内部两阶段提交、组提交与崩溃恢复，并对照 MySQL 8.4.0 真实源码。
tags: [MySQL, InnoDB, Binlog, 两阶段提交, 事务, 源码精读]
---

# 从本地提交到 Binlog：为什么需要协调两种日志？

上一章，我们已经解决了一个持续运行的数据库必须面对的问题：商品 42 的库存反复从 `100` 扣减到 `99`，InnoDB 可以先修改 Buffer Pool 中的数据页，利用 Redo 支持崩溃恢复，再由后台刷脏页与 Checkpoint 逐步推进恢复起点。

这意味着：**只考虑一台 MySQL 实例时，我们已经可以解释一次已提交库存修改如何经受正常运行中的延迟刷盘，以及在满足持久化前提下怎样从崩溃中恢复。**

但现在，业务提出了一个新要求。

用户购买成功后，我们希望另一台 MySQL 也能知道商品 42 的库存从 `100` 变成了 `99`，以便承接部分商品查询流量。

于是，我们的任务发生了一个很小、却很关键的变化：

```text
之前：
  在这台 MySQL 上，可靠地完成一次库存扣减。

现在：
  在这台 MySQL 上，可靠地完成库存扣减；
  还要把这次已经提交的变化交给另一台 MySQL 重现。
```

这时，前面学过的 Redo 能直接拿来使用吗？如果不能，我们需要记录什么？如果既要维护 InnoDB 的本地提交，又要维护一份供复制使用的变更记录，两者中间突然断电怎么办？

**本章只解决这一条新主问题：一次库存扣减的本地提交结果，怎样与供其他实例使用的 Binlog 保持一致？**

> **源码基线**：MySQL Server **8.4.0**，Git Tag [`mysql-8.4.0`](https://github.com/mysql/mysql-server/tree/mysql-8.4.0)。所有真实源码入口均以此 Tag 为准。本文只研究**开启 Binlog、使用 InnoDB 的普通内部事务提交路径**；不将外部 `XA START`/`XA PREPARE` 与本章的内部提交协调混为一谈。
>
>
> **实验状态**：Python 最小故障模型已实际执行并通过断言；MySQL SQL 实验是可复现步骤及预期观察，**未在真实 MySQL 服务上执行**。文中每处“教学化简”均不代表 MySQL 的源代码或完整故障保证。

---

## 1. Redo 已经能恢复库存，为什么还不够？

先回到最初的 SQL：

```sql
UPDATE products
SET stock = stock - 1
WHERE id = 42 AND stock > 0;
```

在同一台 MySQL 中，这条修改会进入 InnoDB；修改相关记录、产生 Undo/Redo，并通过事务提交机制形成最终结果。

上一章我们已经知道，Redo 关心的是 **InnoDB 的页面修改与崩溃恢复**。它记录了支持物理恢复的信息，并不是按业务表设计、供其他数据库实例直接消费的一套通用变更协议。

假设第二台 MySQL 收到一段来自第一台的 InnoDB Redo。我们立即会碰到问题：

- 第二台实例的数据文件、表空间及内部页面布局未必与第一台完全相同。
- Redo 面向 InnoDB 内部结构的恢复，不是面向“复制一次事务”的业务事件格式。
- 复制还需要知道哪些事务已经成为可对外传播的提交结果，而不仅仅是哪一页曾被修改。

这不是说 Redo 的设计不好，而是它最初解决的就不是这个问题。

于是，我们需要一种新的记录：**能表达数据库发生了哪些可复制的数据变更，并让其他实例按事务边界重现这些变更。**

在 MySQL 中，这就是 **Binary Log，简称 Binlog**。

MySQL 官方文档明确指出，Binlog 记录描述数据库变更的事件，用于复制，也用于从备份时间点开始继续恢复后续变更。对 InnoDB 这类事务表，在事务尚未结束时，相关 Binlog 事件可以先存放在会话对应的缓存中，到提交时再作为一个事务写入 Binlog。

需要注意：**Binlog 并不是“另一份 Redo”**。

| 对比问题 | Redo Log | Binlog |
|---|---|---|
| 主要服务于什么 | InnoDB 的崩溃恢复 | 数据变更传播与按时间点恢复 |
| 表达的内容 | 面向 InnoDB 内部页面修改的恢复信息 | 数据变更事件与事务边界 |
| 是否适合另一台普通 MySQL 直接重放 | 不作为通用复制协议 | 是 MySQL 复制的重要输入 |
| 是否能独立解决本章的双日志一致性 | 不能 | 也不能 |

最后一行尤其重要。

新增 Binlog 之后，我们得到了向其他实例传播变更的基础，但同时引入了一个新的风险：**同一次购买，现在会影响两个需要协调的持久化结果。**

## 2. 最简单的方案：先提交库存，再记录 Binlog

如果由我们自己实现，最直接的写法可能是：

```python
# 概念伪代码：错误的、未经协调的提交顺序
commit_innodb("purchase-42")
append_binlog("purchase-42", stock=99)
```

在没有故障时，这似乎完全可行。

数据库先把商品库存提交为 `99`，然后向 Binlog 写下这次变更。另一台 MySQL 只要消费日志，就能得到同样结果。

但假设程序在两行代码之间崩溃：

```text
InnoDB 已经提交
    商品 42：stock = 99
          |
          X 服务器突然崩溃
          |
Binlog 还没有记录该事务
```

重启之后，第一台 MySQL 的库存仍然是 `99`。但供另一台实例消费的 Binlog 没有这笔购买。

如果另一台 MySQL 原本是 `100`，它就可能继续保留 `100`。

我们得到一个具体、可观察的不一致：

```text
主库：stock = 99
从库：stock = 100
```

问题不是 SQL 算错了，也不是 Redo 没有发挥作用。

**恰恰因为本地提交已经成功，这个“只差一条 Binlog”的状态才更加危险。**

我们必须确保：一个应当传播的已提交事务，不能随意出现“存储引擎已确认，但 Binlog 缺少记录”的状态。

很自然地，我们会想到换一下顺序。

## 3. 那就先写 Binlog，再提交 InnoDB？

新的写法变成：

```python
# 概念伪代码：仍然是不完整的设计
append_binlog("purchase-42", stock=99)
commit_innodb("purchase-42")
```

这解决了刚才的失败窗口吗？

没有。

现在如果服务器在两行代码之间崩溃，就会出现另一个方向的不一致：

```text
Binlog 已记录购买成功
         |
         X 服务器崩溃
         |
InnoDB 尚未完成提交
```

如果重启时 InnoDB 把尚未提交的修改撤销，而外部复制又依据 Binlog 应用了这笔购买，那么主库可能仍是 `100`，从库却已经变成 `99`。

两个顺序都存在一个不能回避的事实：

**我们不能把两个独立完成的写入动作，仅凭“谁先谁后”，变成一个不可分割的原子动作。**

这时才真正产生了一个新需求：

> 如果在最终提交之前，InnoDB 能先把事务准备好，但暂时不把它宣布为已提交；那么在 Binlog 完整记录之后，是否还能让 InnoDB 根据这一结果完成提交？

这个中间状态，就是 **Prepare（准备）**。

而负责根据 Binlog 的最终结果协调后续行为的，是 MySQL 内部的事务协调逻辑。

## 4. 第一个必要的新机制：先 Prepare，再作出提交决定

先不看真实 MySQL，我们构造一个最小的协调协议。

假设事务编号为 `purchase-42-001`。它要完成的修改是：商品 42 库存 `100 → 99`。

第一步，存储引擎先进入可恢复的准备状态：

```text
InnoDB：
  事务 purchase-42-001 已准备好
  修改目标：stock = 99
  但尚未成为最终提交结果
```

这样即使进程中断，存储引擎在恢复时也能够识别这笔尚待完成的事务。在真实系统中，达到“可恢复”的条件还取决于日志持久化和相关提交协议约束，不能只在内存中设置一个 `prepared=True`。

第二步，协调方把这笔事务的变更与事务结束标记写入 Binlog，并按配置完成所需的持久化。

此时，我们有了一个能供重启后辨认的事实：**Binlog 中已经存在完整的事务提交记录。**

第三步，通知 InnoDB 完成提交。

于是，主路径变成：

```text
一次库存扣减
     |
     v
InnoDB Prepare
     |
     v
写入并按要求持久化 Binlog
     |
     v
InnoDB Commit
     |
     v
返回成功
```

注意，我们尚未证明它可以安全应对故障。

这只是提供了一个重要机会：**在崩溃之后，我们可能仍然知道有哪些事务正处于准备状态，并依据持久化的协调信息决定后续命运。**

下面先用一个可以实际运行的实验检验这个想法。

## 5. 最小可运行实验：故障分别发生在三个位置

我们不立即编译完整 MySQL，而是先用几十行 Python 显式模拟三个状态：

1. 已提交的库存值；
2. 尚未完成的准备事务；
3. Binlog 中已经持久化的提交证据。

这三个状态都是**教学模拟**。它们在同一个 Python 对象里，并不等价于操作系统崩溃后真正能够幸存的磁盘内容。我们只是把这个对象当成“重启后还能拿到的持久化镜像”，验证协调规则的逻辑。

建立 `two_phase_commit_demo.py`：

```python
from dataclasses import dataclass, field


@dataclass
class DiskImage:
    stock: int = 100
    prepared: dict[str, int] = field(default_factory=dict)
    binlog_commits: set[str] = field(default_factory=set)


class Crash(Exception):
    pass


def transaction(image: DiskImage, crash_after: str | None = None):
    xid = "purchase-42-001"

    # 第一阶段：准备，先不改变对外确认的库存
    image.prepared[xid] = 99
    if crash_after == "prepare":
        raise Crash("after prepare")

    # 教学假设：此记录已经可靠保存，重启后可以读取
    image.binlog_commits.add(xid)
    if crash_after == "binlog":
        raise Crash("after binlog")

    # 第二阶段：存储引擎完成提交
    image.stock = image.prepared.pop(xid)
    if crash_after == "engine_commit":
        raise Crash("after engine commit")


def restart(image: DiskImage):
    for xid, new_stock in list(image.prepared.items()):
        if xid in image.binlog_commits:
            # Binlog 已有完整提交记录：补完提交
            image.stock = new_stock
        # 否则撤销尚未决议的修改（本模型只需删除 pending）
        del image.prepared[xid]
    return image.stock


for crash_after, expected in [
    ("prepare", 100),
    ("binlog", 99),
    ("engine_commit", 99),
    (None, 99),
]:
    image = DiskImage()
    try:
        transaction(image, crash_after)
    except Crash:
        pass

    actual = restart(image)
    assert actual == expected
    assert not image.prepared
    print(
        f'{crash_after or "normal":>13} | '
        f'stock={actual} | binlog={bool(image.binlog_commits)}'
    )
```

我实际运行了这段 Python，并获得：

```text
      prepare | stock=100 | binlog=False
       binlog | stock=99 | binlog=True
engine_commit | stock=99 | binlog=True
       normal | stock=99 | binlog=True
```

四组断言全部通过。

现在逐个解释结果。

**第一种：Prepare 后崩溃，Binlog 尚无完整提交记录。**

重启时读到准备状态，却找不到对应的提交证据，因此撤销准备事务。库存保持为 `100`。

**第二种：Binlog 已持久化后崩溃，InnoDB 尚未完成最后提交。**

重启时仍然可以找到准备状态，同时 Binlog 有这笔事务的提交证据，因此恢复过程补完提交。库存变为 `99`。

**第三种：InnoDB 已经提交后崩溃。**

库存已经成为 `99`，对应的提交信息也已经记录，重启后保持一致。

这正是我们原来做不到的事情：**中断发生在不同位置，恢复之后仍然能给出一致的事务结果。**

不过，教学模型还有明显不足：

- 它没有模拟文件系统缓存、`fsync`、断电和损坏的日志尾部；
- 它将“Binlog 中有完整记录”直接当作提交证据，真实 MySQL 还必须校验事务边界和 XID；
- 它没有处理并发提交、GTID、复制线程和外部 XA；
- 它把商品库存直接写成 `99`，没有模拟真实页修改、Undo、Redo 的执行过程。

所以它只能验证**协议思路**，不能证明 MySQL 的崩溃安全性。

接下来，我们必须回到真实源码。

---

## 6. MySQL 怎样进入 Prepare？

固定源码：MySQL **8.4.0**。

入口文件：[`sql/handler.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/handler.cc)。

上一章我们已经知道，客户端的 UPDATE 最终会交给存储引擎完成修改。修改结束后，事务还需要经过提交过程。

在 Server 层，可以找到：

```cpp
int ha_commit_trans(THD *thd, bool all, bool ignore_global_read_lock)
```

这是**真实源码函数签名**。它负责组织一次事务提交，`thd` 是本次会话上下文，`all` 用于区分完整事务提交与语句级处理。这里不能简单把 `all=false` 理解为“永远不会真实提交”：在自动提交模式下，语句结束也可能结束完整事务。

对于参与两阶段提交的事务，`ha_commit_trans()` 会通过事务协调对象进入 Prepare/Commit 流程。在当前基线中，相关判断包含：

```cpp
!trn_ctx->no_2pc(trx_scope)
```

**真实源码节选，来自 `ha_commit_trans()` 的判断条件。** 另一项重要条件是参与的读写处理器数量；不是所有事务都会无条件走同一个 2PC 分支。

这里的新名词 `tc_log`，可以先理解为**事务协调器接口**。它的用途不是查找商品，也不是修改库存，而是在提交阶段协调参与者。

当 Binlog 启用且事务满足对应条件时，Binlog 可以承担内部事务协调器的角色。

那么准备动作究竟在哪里发生？

打开 [`sql/binlog.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/binlog.cc)，查找 `MYSQL_BIN_LOG::prepare()`。

该函数在准备过程中调用：

```cpp
int error = ha_prepare_low(thd, all);
```

**真实源码节选：`MYSQL_BIN_LOG::prepare()`。**

我们再顺着这个函数回到 `sql/handler.cc`：

```cpp
int ha_prepare_low(THD *thd, bool all)
```

它会遍历当前事务登记的存储引擎参与者，并在需要准备的参与者上调用各自的 `prepare` 回调。

到这里，我们已经理解为什么要多一层 `ha_prepare_low()`：

MySQL Server 本身不应该去猜测每个存储引擎内部如何准备事务。它负责发出“请进入可恢复的准备状态”的协调要求，具体动作由存储引擎实现。

### 6.1 Prepare 最终怎样到达 InnoDB？

打开 [`storage/innobase/handler/ha_innodb.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/handler/ha_innodb.cc)。

在 InnoDB 初始化回调的代码中，可以找到：

```cpp
innobase_hton->prepare = innobase_xa_prepare;
```

**真实源码节选：InnoDB handlerton 回调注册。**

现在就可以建立一条准确的职责链：

```text
Server 发起 Prepare
        |
        v
ha_prepare_low()
        |
        | 通过 handlerton 回调
        v
innobase_xa_prepare()
        |
        v
trx_prepare_for_mysql()
```

这里的 `innobase_xa_prepare()` 名称虽然含有 `xa`，但它也是 InnoDB 通过存储引擎回调参与内部事务准备的入口。它不意味着用户执行了 `XA PREPARE` SQL。

源码中的 `trx_prepare_for_mysql()` 负责推进 InnoDB 事务的准备状态。具体日志持久化时点还受提交组的设计控制：**不要把“执行 Prepare 函数”错误理解为“所有准备记录都已经单独 fsync 完成”。**

实际上，MySQL 8.4.0 的 Binlog 组提交会把准备阶段的部分日志同步工作推迟到批量处理阶段，以减少同步成本。这是后面马上要看到的设计。

现在我们先得到一个可靠结论：

**Prepare 是 Server 发出的协调动作，真正把 InnoDB 事务推进准备状态的，是 InnoDB 自己的事务实现。**

## 7. Prepare 之后，Binlog 怎样接手？

回到 [`sql/binlog.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/binlog.cc)。

事务协调的提交入口是：

```cpp
TC_LOG::enum_result MYSQL_BIN_LOG::commit(THD *thd, bool all)
```

它不是一个“立刻调用 InnoDB commit 就返回”的简单函数。

对于需要写 Binlog 的事务，它首先整理当前事务对应的 Binlog 缓存，为日志写入作准备，再进入有序组提交逻辑。

为什么先在缓存中组织事件？

回想我们的购买事务可能包含：

```sql
START TRANSACTION;

UPDATE products
SET stock = stock - 1
WHERE id = 42 AND stock > 0;

INSERT INTO orders (product_id, quantity)
VALUES (42, 1);

COMMIT;
```

复制不能只拿到库存扣减，却漏掉同一事务中的订单写入。因此，日志处理需要保留事务边界，不能把每一条修改毫无关联地散落出去。

在普通内部 2PC 的事务路径中，Binlog 末尾可以使用包含事务 XID 的结束事件标记这个事务已经完整写入。

源码中对应的类型名是：

```cpp
Xid_log_event
```

这里的 **XID** 可以先理解为用于匹配一次事务提交与恢复状态的标识。

我们没有必要现在深入 XID 的二进制编码，也不需要提前展开 GTID 的分配规则。当前只需要知道：

> 恢复时不能仅凭“Binlog 文件末尾有一些字节”，就认为某个事务一定提交成功；需要识别一笔**完整事务**及其对应标识。

这也解释了我们 Python 教学模型里为什么不是只记录一个 `binlog_has_bytes=True`，而是维护 `binlog_commits` 集合。

集合表示的是：哪些事务存在可供恢复决策使用的完整提交证据。

## 8. 为什么有了 Prepare，还需要 Flush、Sync、Commit 三段？

到此为止，我们可以用单线程最小协议解释双日志协调：

```text
Prepare → Binlog 持久化 → InnoDB Commit
```

但真实 MySQL 并不是一次只处理一个购买请求。

假设三个用户几乎同时购买商品：

```text
事务 A：购买商品 42
事务 B：购买商品 51
事务 C：购买商品 87
```

如果每个事务都独立进行一轮 Binlog 文件同步，数据库可能为每次提交付出一次昂贵的持久化等待。

既然多个事务都在同一个时间窗口进入提交过程，是否可以把它们的日志写入和同步工作组织在一起？

这时才有必要引入 **Group Commit（组提交）**。

它没有改变刚才解释的正确性目标，只是让一组事务共同推进其中某些日志阶段，减少重复同步开销，并协调必要的提交顺序。

MySQL 8.4.0 在 `MYSQL_BIN_LOG::ordered_commit()` 中组织这一流程。该源码在 [`sql/binlog.h`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/binlog.h) 中也有详细的阶段说明。

对于我们关注的常见主路径，可以分成以下几个观察点。

### 8.1 Flush：把这批事务的 Binlog 缓存写向日志文件

源码中 `MYSQL_BIN_LOG::ordered_commit()` 会安排这批事务进入 Binlog Flush 阶段，再调用 `process_flush_stage_queue()` 等函数处理队列中的事务。

在 `process_flush_stage_queue()` 中能看到循环执行 `flush_thread_caches()`，把各会话已经完成整理的事件从事务缓存写向 Binlog 输出路径。

注意：

**把缓存内容写向 Binlog 文件，不等于存储设备已经确认这些数据完成持久化。**

这和我们之前研究 Redo 时区分“写日志”和“刷日志”是同一个道理。

### 8.2 Sync：按策略同步 Binlog

接下来，提交组进入 Sync 阶段。

在源码中能找到：

```cpp
sync_binlog_file(false);
```

**真实源码节选：`MYSQL_BIN_LOG::ordered_commit()` 的 Sync 阶段。**

是否为当前提交组执行实际文件同步，受 `sync_binlog` 等配置影响。

`sync_binlog=1` 通常意味着对每个 Binlog 提交组执行同步；这与“一条事务就做一次 fsync”不是同一句话，因为组里可以有多个事务。

如果把 `sync_binlog` 设置成较宽松的值，性能与断电后丢失 Binlog 尾部的风险之间就会发生取舍。

所以我们不能说“只要使用 Binlog，就必然拥有任何断电场景下的完整持久化保证”。

### 8.3 Commit：让存储引擎完成提交

只有准备、日志写入以及相应的同步环节推进之后，才轮到存储引擎完成最终的提交阶段。

源码进入 `process_commit_stage_queue()` 后，会沿存储引擎提交回调完成这一动作；底层能够进一步追到 `ha_commit_low()` 和 InnoDB 注册的 `innobase_commit()`。

可以把这条主要路径记为：

```text
InnoDB 准备事务
      |
      v
Binlog Flush 阶段
      |
      v
Binlog Sync 阶段
      |
      v
存储引擎 Commit 阶段
      |
      v
提交后的清理与结果处理
```

**重要边界**：这是便于理解的正常主路径，不是逐个函数、不分分支的完整运行栈。实际实现还考虑 Binlog 未启用、会话无待写事件、组提交并发、错误处理、复制线程及 `binlog_order_commits` 配置等情况。

现在，关于两阶段提交的主要问题已经可以回答：

- Prepare 让存储引擎可以在恢复时辨认尚待决议的事务；
- 完整 Binlog 提供内部协调所需的提交证据；
- Commit 则完成存储引擎里的事务提交；
- Group Commit 进一步让一批事务高效地推进 Binlog Flush/Sync/Commit 流程。

但我们还欠最后一块证据：**服务器真的崩溃以后，凭什么知道应该提交哪一个准备事务？**

---

## 9. 崩溃恢复时，谁来对照 XID？

先看最关键的故障窗口。

```text
事务 purchase-42-001

InnoDB Prepare 完成
         |
Binlog 已有完整事务记录
         |
         X 进程崩溃
         |
InnoDB Commit 尚未完成
```

在没有协调恢复机制时，系统无法知道该怎样处理这笔尚未完成提交的事务。

但如果 InnoDB 能恢复它的准备状态，而且 Binlog 中能够找到相同事务的完整提交记录，就有了明确的决定依据。

在 MySQL 8.4.0 中，可以从 [`sql/binlog.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/binlog.cc) 的启动恢复逻辑找到 `binlog::Binlog_recovery` 的使用。

接着打开 [`sql/binlog/recovery.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/binlog/recovery.cc)，查看 `Binlog_recovery::recover()`。

它的流程包括处理 Binlog 中的事件，并在适用条件下调用 Server 层的存储引擎恢复接口，将收集到的事务标识用于完成协调恢复。

当前版本源码中有一个很有代表性的调用：

```cpp
ha_recover(&this->m_internal_xids, &xa_list);
```

**真实源码节选：`Binlog_recovery::recover()`。**

这里的 `m_internal_xids` 就与我们一直研究的内部事务协调路径相关。

接下来，InnoDB 还注册了恢复时需要的回调，包括 `innobase_xa_recover()`、`innobase_commit_by_xid()` 和 `innobase_rollback_by_xid()`。

回到我们已经理解的最小模型，可以用下面的**教学化简伪代码**表达决策：

```python
for trx in recovered_prepared_transactions:
    if trx.xid in complete_committed_binlog_xids:
        engine.commit_prepared(trx)
    else:
        engine.rollback_prepared(trx)
```

真实 MySQL 的恢复并不是直接运行这段 Python；它还要检查日志是否合法、找到有效日志尾部、处理不同的 XA 状态，并协调存储引擎回调。

但上面的判断准确概括了普通内部两阶段提交要达成的核心效果：

**一个恢复中的准备事务，最终是否完成提交，需要与持久化的协调记录相匹配。**

因此，在标准的持久化前提下，本章最初两个看似不可避免的失败窗口，终于可以得到统一的解释：

| 崩溃位置 | 关键事实 | 恢复方向 |
|---|---|---|
| Prepare 已发生，Binlog 尚无完整提交证据 | 无法确认应提交 | 回滚尚未决议的内部事务 |
| Binlog 已完整且可靠持久化，InnoDB 尚未 Commit | 提交决定已有依据 | 完成对应内部事务提交 |
| InnoDB 已 Commit | Binlog 与提交结果均成立 | 保留已提交状态 |

这三行讨论的是**可恢复的内部事务准备状态、有效完整的 Binlog 与受支持的存储持久化条件**，不能扩展成“任意硬件损坏、任意日志丢失后都一定能恢复”。

MySQL 8.4 官方文档也明确说明了 InnoDB 内部两阶段提交与 Binlog 恢复的协作，并强调 `sync_binlog=1` 及 InnoDB 日志同步配置的重要性。

## 10. 一个容易混淆的问题：Redo 和 Binlog 的写入顺序

读到这里，可能会产生一种过度简化的理解：

> 先写 Redo，再写 Binlog，然后提交，问题就解决了。

这句话仍然不够准确。

因为“写 Redo”至少可以指不同的动作：

- 页面修改生成了对应的 Redo 记录；
- Redo 内容进入日志缓冲及写入流程；
- Redo 已经写到文件；
- Redo 已按要求同步到稳定存储；
- InnoDB 事务进入可恢复的 Prepare 状态；
- InnoDB 最终完成 Commit。

它们不能随意画等号。

同样，“写 Binlog”也至少要区分：

- 事务变更事件进入 Binlog Cache；
- 事务结束事件形成完整的日志事务；
- Binlog Cache 内容写向文件；
- 按 `sync_binlog` 策略完成必要同步。

所以真实协议不是一句“先写哪种日志”就能讲清楚。

我们实际在 MySQL 8.4 源码中看到的是：

1. Server 通过 Prepare 回调协调存储引擎；
2. 在组提交 Flush 阶段协调存储引擎日志与 Binlog 输出；
3. 在 Sync 阶段按策略同步 Binlog；
4. 随后推进存储引擎 Commit；
5. 崩溃后通过完整 Binlog 事务标识与恢复出的准备状态协调完成或回滚。

这也回答了一个具体问题：

**为什么两阶段提交并不要求每次先单独 fsync 一次 InnoDB Redo，再单独 fsync 一次 Binlog？**

因为实际实现会进行组提交和持久化动作的批量协调。正确性约束与每个系统调用何时执行，不是同一个抽象层次。

我们需要尊重源码里的阶段边界，不能把所有日志相关动作压缩为三句口号。

---

## 11. 真实 MySQL 实验：一次库存扣减在 Binlog 中留下了什么？

现在回到商品 42，观察 Binlog 是否真的记录了这次变更。

这次实验不需要暴力断电，也不需要在生产环境中修改配置。

建议使用**独立测试 MySQL 8.4 实例**。示例假设服务器已经开启 Binlog，且会话有查看 Binlog 状态与事件的权限。实际需要的权限应由实验环境管理员配置；如果权限不足，相关查询可能报错。

### 11.1 确认关键配置

```sql
SELECT @@version AS mysql_version;

SHOW VARIABLES WHERE Variable_name IN (
    'log_bin',
    'binlog_format',
    'sync_binlog',
    'innodb_flush_log_at_trx_commit'
);
```

我们需要关注：

- `log_bin` 是否为 `ON`；
- `binlog_format` 是否为 `ROW`；
- `sync_binlog` 的设置；
- `innodb_flush_log_at_trx_commit` 的设置。

MySQL 8.4 默认采用基于行的 Binlog 格式。为了让实验中的数据变化更容易观察，建议保留 `ROW`，不要为了教学在生产环境修改服务器持久化配置。

### 11.2 准备独立实验数据

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

这一步本身也可能写入 Binlog。因此我们要在**准备工作完成之后**，记录新的起始位置。

### 11.3 记录 Binlog 起点

```sql
SHOW BINARY LOG STATUS;
```

记下输出的 `File` 和 `Position`。它们因实例而异，这里不虚构具体值。

如果 `log_bin=OFF`，就不能通过这个实验观察当前连接的事务 Binlog；应换到启用 Binlog 的测试实例。

### 11.4 执行一次真正的购买事务

```sql
START TRANSACTION;

UPDATE products
SET stock = stock - 1
WHERE id = 42 AND stock > 0;

SELECT ROW_COUNT() AS affected_rows;

COMMIT;

SELECT stock
FROM products
WHERE id = 42;
```

对于初始化库存为 `100` 且没有其他写入干扰的实验环境，预期 `affected_rows=1`，最终库存为 `99`。

再次执行：

```sql
SHOW BINARY LOG STATUS;
```

如果实验期间没有切换 Binlog 文件，可对比前后的 `Position`；若文件切换，则需要按文件顺序定位对应事件。

### 11.5 检查该事务的 Binlog 事件

先列出日志文件：

```sql
SHOW BINARY LOGS;
```

也可以执行下面的示例查询，其中日志文件名需要替换成**你的实例实际返回的文件名**：

```sql
SHOW BINLOG EVENTS IN 'binlog.000001' LIMIT 30;
```

这里没有提供固定输出，因为不同的 GTID、行事件、日志文件和实例配置会改变事件序列。

如果使用的是默认行格式，事件中通常会包含与 `products` 表修改相关的表映射和行更新事件，以及事务结束标记。不要期待每一次行格式更新都在日志中原样显示 `UPDATE products SET stock = stock - 1 ...` 这段 SQL。

有服务器权限并能够使用 `mysqlbinlog` 工具时，还可以在测试环境中查看行事件的可读表示：

```bash
# 文件名换成 SHOW BINARY LOGS 看到的真实值；
# 需要对日志文件有读取权限。
mysqlbinlog --base64-output=DECODE-ROWS -vv binlog.000001
```

这是用于**观察事件**的命令，而不是让用户直接执行恢复。`DECODE-ROWS` 的输出并不适合不加检查地重新导入数据库。

### 11.6 这次实验能验证什么？

它可以帮助我们确认：

1. UPDATE 确实修改了商品库存；
2. 提交以后，Binlog 的事件记录中能够找到相应的数据变更；
3. 在默认 ROW 格式下，传播的是行变更相关事件，而不是只能依赖原始 SQL 字符串；
4. Binlog 有明确的事务边界。

但这次实验**不能**证明两个真正的故障窗口都被覆盖。因为我们没有精确地在 InnoDB Prepare 后或 Binlog 同步后强制让服务器崩溃。

若要验证那些窗口，需要受控的调试构建、故障注入以及恢复后完整的持久化检查，不能仅凭 `SHOW BINLOG EVENTS` 宣称“崩溃恢复已经验证通过”。

## 12. 为什么这不是让应用开发者显式执行两次 COMMIT？

再看我们日常写的事务：

```sql
START TRANSACTION;

UPDATE products
SET stock = stock - 1
WHERE id = 42 AND stock > 0;

INSERT INTO orders (product_id, quantity)
VALUES (42, 1);

COMMIT;
```

开发者只发送一次 `COMMIT`。

但这并不意味着 MySQL 内部只进行了一个简单的“提交按钮”动作。

MySQL Server 会根据当前事务、相关存储引擎参与情况及 Binlog 配置，选择相应的提交协调路径。

本章讲的 **内部两阶段提交**，是 Server 与存储引擎之间的事务协调机制，用来处理包括 Binlog 与存储引擎结果一致在内的问题。

它不要求应用程序自己依次发送：

```sql
-- 不是本章正常应用提交方式
XA START ...;
XA END ...;
XA PREPARE ...;
XA COMMIT ...;
```

后者属于**外部 XA 事务接口**，虽然底层机制有联系，但使用场景和协议状态并不与本章完全等同。

理解这一边界很重要，否则容易错误地推断“只要 MySQL 开了 Binlog，应用就必须使用 XA SQL”。

## 13. 这个机制解决了什么，没有解决什么？

经过本章的推导，我们终于可以精确说出 Binlog 与内部两阶段提交各自承担的职责。

**Binlog 解决了什么？**

它为数据库变更提供了可用于复制和按时间点恢复的事件记录，使同一次商品库存修改有机会被其他实例重新应用。

**内部两阶段提交解决了什么？**

它为存储引擎提交与 Binlog 记录建立了可恢复的协调关系，避免仅靠两个独立写入顺序所产生的常见中断不一致。

**组提交又解决了什么？**

它在不放弃必要协调约束的前提下，让一批事务共享部分日志写入和同步工作，降低每笔事务都独立同步带来的成本。

但我们必须明确它们**没有自动解决**以下问题。

首先，它们不保证客户端在所有网络异常下都能确切知道提交结果。比如 COMMIT 的应答包丢失，应用可能不知道事务究竟是否成功。这仍需要业务幂等、请求标识及查询确认等机制。

其次，它们不保证复制实例与主库在任意时刻都拥有完全相同的库存。**Binlog 存在，不等于从库已经收到和应用事件。**

再次，它们也不代表在错误的刷盘配置、损坏的存储设备或文件系统不兑现同步语义时，数据一定不会丢失。

对于一般希望强化本机提交持久性及 Binlog 一致性的 InnoDB 配置，通常会重点关注：

```ini
[mysqld]
innodb_flush_log_at_trx_commit=1
sync_binlog=1
```

这并不是要求你现在修改正在运行的服务器，而是说明我们前面那些“可靠持久化”的推论具有配置与存储系统前提。

至此，主库内部的一次库存扣减，终于与 Binlog 建立了正确的提交关系。

那么现在，一个新的问题才真正有必要出现：**另一台 MySQL 怎样读取这些 Binlog 事件、应用它们，并让自己的库存变成 `99`？**

---

## 14. 回看：我们这一章真正完成了什么？

先看本章开始时的最简单方案：

```text
InnoDB 提交 → 追加 Binlog
```

当服务器在两步之间崩溃，主库可能完成了库存扣减，Binlog 却缺少这笔购买。

我们交换顺序：

```text
追加 Binlog → InnoDB 提交
```

还是不行，因为崩溃后可能出现 Binlog 有变更、存储引擎却尚未完成提交的状态。

真正缺失的不是一个更巧妙的写入顺序，而是**允许事务先进入可恢复的中间状态，再用持久化的协调信息确定最终结果**的协议。

因此，我们才引入：

```text
       单次购买提交
             |
             v
       InnoDB Prepare
             |
             v
       Binlog 完整记录
             |
             v
       Binlog 所需同步
             |
             v
       InnoDB Commit
             |
             v
         返回结果
```

这张图不等同于完整源码调用栈；组提交、事务缓存和异常分支都在其中被省略。

但到目前为止，我们已经能从真实源码说明它为什么成立：

- `ha_commit_trans()` 组织事务提交与协调器调用；
- `MYSQL_BIN_LOG::prepare()` 通过 `ha_prepare_low()` 要求存储引擎准备；
- InnoDB 的 `innobase_xa_prepare()` 进入自身的事务准备逻辑；
- `MYSQL_BIN_LOG::commit()` 组织事务 Binlog 与提交流程；
- `MYSQL_BIN_LOG::ordered_commit()` 协调 Flush、Sync 与 Commit；
- 崩溃恢复代码通过 Binlog 的完整事务信息和存储引擎准备事务状态协调完成恢复。

我们最终获得的不再是“Redo 用于恢复，Binlog 用于复制”这一句孤立的定义。

而是能够解释：

> 为什么第一次库存扣减在只有本机提交时可以成功，但一旦需要把提交结果传播到其他实例，就会产生新的失败窗口；MySQL 又是怎样通过 Prepare、Binlog 事务记录与恢复时的 XID 协调，缩小这些窗口带来的不一致风险。

下一章，我们继续沿用同一笔购买：商品 42 在主库已经提交为 `99`，Binlog 也包含了这次变更。

现在，第二台 MySQL 从哪里得到这些事件？为什么它可能暂时仍然看到 `100`？

**下一章：第 14 章《从一台 MySQL 到另一台——商品库存怎样复制？》**

---

## 源码阅读地图与参考资料

本章真实源码固定为 **MySQL 8.4.0 / `mysql-8.4.0`**。

| 源码文件 | 函数 / 位置 | 本章所需的证据 |
|---|---|---|
| [`sql/handler.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/handler.cc) | `ha_commit_trans()` | Server 组织事务提交及 2PC 选择 |
| [`sql/handler.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/handler.cc) | `ha_prepare_low()` / `ha_commit_low()` | Server 向存储引擎发出 Prepare / Commit |
| [`sql/binlog.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/binlog.cc) | `MYSQL_BIN_LOG::prepare()` | 进入存储引擎 Prepare |
| [`sql/binlog.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/binlog.cc) | `MYSQL_BIN_LOG::commit()` | Binlog 事务整理与提交入口 |
| [`sql/binlog.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/binlog.cc) | `MYSQL_BIN_LOG::ordered_commit()` | 组提交的 Flush / Sync / Commit 阶段 |
| [`sql/binlog.h`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/binlog.h) | `ordered_commit()` 声明旁的注释 | 各阶段职责的官方源码描述 |
| [`storage/innobase/handler/ha_innodb.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/handler/ha_innodb.cc) | `innobase_xa_prepare()` / `innobase_commit()` | InnoDB 的事务回调 |
| [`sql/binlog/recovery.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/binlog/recovery.cc) | `Binlog_recovery::recover()` | 崩溃时通过 XID 与引擎恢复协作 |

官方手册：

- [MySQL 8.4 — The Binary Log](https://dev.mysql.com/doc/refman/8.4/en/binary-log.html)
- [MySQL 8.4 — Binary Logging Formats](https://dev.mysql.com/doc/refman/8.4/en/binary-log-formats.html)
- [MySQL 8.4 — mysqlbinlog 工具](https://dev.mysql.com/doc/refman/8.4/en/mysqlbinlog.html)
- [MySQL 8.4 — SHOW BINARY LOG STATUS](https://dev.mysql.com/doc/refman/8.4/en/show-binary-log-status.html)
- [MySQL 8.4 — XA Transactions 限制](https://dev.mysql.com/doc/refman/8.4/en/xa-restrictions.html)

> 阅读建议：先找 `ha_commit_trans()` 与 `MYSQL_BIN_LOG::prepare()` 之间的调用，再读 `ordered_commit()` 的 Flush/Sync/Commit 阶段，最后打开 `Binlog_recovery::recover()` 对照中断后的决定依据。不要从 `binlog.cc` 文件开头顺序阅读一万多行代码。
