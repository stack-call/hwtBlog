---
title: "分布式系统 09：一次事务需要写两份日志，崩溃后以谁为准？"
sidebar_label: "九、两份日志以谁为准"
sidebar_position: 10
description: "从文章访问事件的提交崩溃出发，逐层理解 MySQL 8.4.0 的 InnoDB Redo、Binlog、内部两阶段提交与崩溃恢复，并对照真实源码验证。"
slug: /distributed-systems/09-mysql-redo-binlog-crash-recovery
tags: [distributed-systems, mysql, innodb, binlog, source-code]
---

# 一次事务需要写两份日志，崩溃后以谁为准？

上一篇我们让文章访问系统遇到了一个新问题：当一笔操作跨越多个数据分片时，两个节点不一定能同时成功。我们由此看到，**“协调提交”不是把两次写入放在同一个函数里那么简单**。

但在继续研究更大范围的分布式事务之前，先回到一台 MySQL 服务器。即使数据库只运行在**一台机器**上，一次事务提交也已经涉及两个需要协调的角色：负责事务数据的 InnoDB，以及负责记录可供复制和增量恢复使用的 Binlog。

我们沿用文章访问这个案例，不过不再把 Redis 中的 `post:42:views` 计数值当作同一份数据直接复制到 MySQL。现在业务有了更强的要求：**每次访问要留下可追踪的事件记录**。

```sql
CREATE TABLE article_view_events (
    event_id VARCHAR(64) PRIMARY KEY,
    post_id BIGINT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB;

INSERT INTO article_view_events(event_id, post_id)
VALUES ('view-event-42-000101', 42);
```

这个 `event_id` 只是本章的唯一标识，便于辨认同一笔事务；它不是 MySQL 的事务 ID。我们暂时不讨论应用如何生成事件 ID，也不引入跨系统的事件投递。

现在假设客户端执行完 `COMMIT`，服务器立即断电。重启之后，这一条记录究竟该存在，还是不存在？如果 MySQL 后面还带着一个副本，副本应该看到哪一个结果？

这就是本章的唯一主问题：**同一笔事务需要在两种日志或状态中留下结果时，如何避免崩溃后互相矛盾？**

> **源码版本**：[`mysql/mysql-server`，标签 `mysql-8.4.0`](https://github.com/mysql/mysql-server/tree/mysql-8.4.0)。下文所有“真实源码节选”均以这个固定标签为准。实验是纯 Python 教学模型，不是 MySQL 集成测试。

## 一、如果只有数据页，为什么恢复不了刚提交的事务？

先想象一个过于简单的数据库：它只把最终数据写入磁盘中的“数据页”。每次访问事件到来，程序先改内存中的页面，过一段时间才把整个页面写到磁盘。

```python
# 概念伪代码：仅用于说明磁盘页面可能落后
page_on_disk = {"post:42:views": 100}
page_in_memory = {"post:42:views": 101}
```

此时业务看起来已经把计数更新成 `101`。如果进程意外退出，而新页面还没写到磁盘，重新启动只能读回 `100`。

最直接的补丁是：每次事务提交都立刻写完整个数据页。

在极小数据集上它能工作。但随着页面数量增长，一次修改可能牵涉多个位置；还要考虑写入中途断电，磁盘上留下不完整状态。我们真正需要的是：**即使数据页暂时没来得及落盘，也有一份能够用于重做已确认修改的持久记录**。

这时才引入 **Redo Log**。它属于 InnoDB 的恢复机制：先让必要的修改记录具备持久性，再允许数据页稍后批量刷盘。重启后，InnoDB 根据 Redo 恢复尚未体现在数据文件中的修改，并处理未完成事务。

注意，Redo 的内容并不等于“一行 SQL 字符串”。它主要描述 InnoDB 内部页面等结构的修改；其语义和面向复制的 Binlog 不同。

### 到源码里找“写 Redo”的位置

打开 [`storage/innobase/mtr/mtr0mtr.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/mtr/mtr0mtr.cc#L775-L813)，找到 `mtr_t::Command::execute()`。这是 **InnoDB mini-transaction（内部微事务）** 提交修改记录时的一条底层路径，*不是*客户端 SQL 事务 `COMMIT` 函数本身。

**真实源码节选（省略中间检查）：**

```cpp
auto handle = log_buffer_reserve(*log_sys, len);

m_impl->m_log.for_each_block(write_log);

add_dirty_blocks_to_flush_list(handle.start_lsn, handle.end_lsn);

log_buffer_close(*log_sys, handle);
```

这几行分别做了什么？

`log_buffer_reserve()` 为本次 Redo 记录预留位置；`for_each_block(write_log)` 将内部微事务积累的记录写入 Redo 缓冲；`add_dirty_blocks_to_flush_list()` 记录这批已修改页面及其日志位置；随后关闭这段缓冲区写入。

调用者是 `mtr_t::commit()`，它会在需要记录修改时调用 `cmd.execute()`。这里的输入是 InnoDB 已经积累的内部修改记录，输出之一是提交位置 `m_commit_lsn`，下游由 Redo 写盘、刷盘和恢复机制使用。

**重要边界**：这段代码展示的是 *Redo 缓冲记录的组织*，不能据此推断“此刻已经 `fsync` 到磁盘”或“整个 SQL 事务已经提交”。真实持久化还依赖后续刷盘与事务提交路径。

再看 [`storage/innobase/log/log0recv.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/log/log0recv.cc#L3564-L3582) 的 `recv_recovery_from_checkpoint_start()`。这是崩溃恢复阶段从检查点开始处理 Redo 的入口之一，而不是普通 `INSERT` 的实时调用者。

所以目前我们只建立一个必要结论：**Redo 让本机能够修复“数据页没及时落盘”的问题。**

但是，Redo 并没有自动解决另一个需求。

## 二、如果后面有副本，为什么不直接把 Redo 发过去？

现在假设我们希望另一台 MySQL 服务器也保存文章访问事件。我们需要能在另一台数据库上重现已经提交的变更。

一开始可能会想：既然 Redo 已经记录了修改，直接把它给副本不就好了？

问题在于，Redo 是紧贴 InnoDB 存储布局的恢复材料；它不是为普通 MySQL 异步复制设计的、可由另一台 SQL 服务器独立重放的事务事件接口。

我们需要另一种记录：**描述源库已提交的数据变化，供副本重放，也能在备份后做时间点恢复。**

这就是 **Binary Log（Binlog）** 的主要职责。

MySQL 官方文档将 Binlog 的两个核心用途明确列为：*复制* 和 *按时间点恢复*。在 `ROW` 格式下，Binlog 记录行级变更事件，而不是把 InnoDB Redo 原样发给副本。MySQL 8.4 默认启用 Binlog，但也可以通过配置关闭；本章讨论的是 **InnoDB + Binlog 已启用** 的正常事务路径。

现在，一笔文章访问事务在本机就出现了两处不同的结果记录：

```text
                    INSERT 访问事件
                          |
                    同一台 MySQL
                     /          \
             InnoDB 事务结果     Binlog 事务事件
             （本机恢复）        （复制/增量恢复）
```

直到这里，我们仍然不需要新术语。只需要问一个具体的问题：

**如果服务器恰好在只写完其中一处时崩溃，会怎样？**

## 三、按顺序写两份，不就完成了吗？

考虑两个最简单、但都不可靠的提交顺序。

### 方案 A：先提交 InnoDB，再写 Binlog

```python
# 概念伪代码
engine.commit(tx)
binlog.append(tx)
```

如果在两行之间崩溃，InnoDB 已经承认这次访问事件，但 Binlog 中没有。

```text
InnoDB：有 view-event-42-000101
Binlog：没有这个事务
```

对本机而言记录已经提交；但依赖 Binlog 的副本和增量恢复可能再也看不到它。

### 方案 B：先写 Binlog，再提交 InnoDB

```python
# 概念伪代码
binlog.append(tx)
engine.commit(tx)
```

如果两行之间崩溃，情况反过来：

```text
InnoDB：事务未提交
Binlog：已有这个事务
```

副本可能重放一个源库存储引擎最终并未提交的事件。

把顺序调换，无法同时消除这两种失败。我们缺的不是更小心地调用两个函数，而是：**崩溃以后，双方还能根据可恢复的证据，对这笔事务作出同一个最终决定。**

此时才有必要引入 **准备阶段（Prepare）**。

## 四、先让 InnoDB 进入“准备好，但尚未最终提交”的状态

我们把协议改成这样：

```text
1. InnoDB Prepare：保存可恢复的待决事务状态
2. Binlog 写入并按配置持久化事务事件
3. InnoDB Commit：完成存储引擎事务提交
```

最重要的变化发生在第一步。

原来 InnoDB 要么立即提交，要么没有提交；现在它可以先保存“我已经准备好，一旦知道最终决定，就能继续”的状态。

于是，即使 MySQL 在步骤 2 和步骤 3 之间崩溃，重启时仍然能够检查：

- InnoDB 是否留下了准备中的事务？
- Binlog 中是否存在与之匹配、有效的事务记录？

如果 Binlog 有效地包含这笔事务，则恢复路径可以使对应的已准备事务完成提交；如果没有，则可以回滚这笔待决事务。

这在 MySQL 中称为用于协调 **Binlog 与存储引擎** 的内部两阶段提交。它与上一篇介绍的、由外部事务管理器协调多台数据库的 `XA START / XA PREPARE / XA COMMIT` 不是同一个业务操作流程，虽然二者共享“两阶段协调”的设计思想。

### 真实源码：是谁决定要不要 Prepare？

打开 [`sql/handler.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/handler.cc#L1649-L1674)，找到 `ha_commit_trans()`。在正常需要内部两阶段提交的事务路径中，可见：

**真实源码节选：**

```cpp
if (!trn_ctx->no_2pc(trx_scope) && (trn_ctx->rw_ha_count(trx_scope) > 1))
  error = tc_log->prepare(thd, all);
```

为什么判断 `rw_ha_count > 1`？这里计入参与事务的读写引擎角色；当 Binlog 参与事务协调时，它作为事务相关参与者之一。并不是说 SQL 一定跨越两张表或两台机器才要进入这条路径。反过来，空事务、只读事务、关闭 Binlog 等情形也不应被强行套进本章的三步模型。

同一个函数接着调用协调器的提交接口：

**真实源码节选：**

```cpp
if (error || (error = tc_log->commit(thd, all))) {
    ha_rollback_trans(thd, all);
    error = 1;
    goto end;
}
```

这里的 `tc_log` 是事务协调器接口；在本章的 **Binlog 已启用** 场景，它将提交工作交给 Binlog 协调路径。`THD *thd` 是本次 SQL 会话与事务上下文，`all` 表示提交整个事务还是语句级事务作用域。

也就是说，`ha_commit_trans()` 不需要自己写一套“Binlog 文件怎么同步”的逻辑：它负责协调是否准备、是否进入最终提交，具体细节由下游完成。

### 真实源码：Prepare 究竟把请求交给了谁？

打开 [`sql/binlog.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/binlog.cc#L7557-L7589)，找到 `MYSQL_BIN_LOG::prepare()`。

**真实源码节选：**

```cpp
thd->durability_property = HA_IGNORE_DURABILITY;
int error = ha_prepare_low(thd, all);
```

真正遍历参与的存储引擎、调用准备回调的是 [`ha_prepare_low()`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/handler.cc#L2146-L2179)：

**真实源码节选：**

```cpp
auto ht = ha_info.ht();
const int err = ht->prepare(ht, thd, all);
```

这里的设计分层很清楚：协调器决定 **现在需要 Prepare**，`ha_prepare_low()` 负责调用参与引擎的 `prepare` 回调，实际存储引擎决定如何表示可恢复的准备状态。

还有一个容易误读的细节：`HA_IGNORE_DURABILITY` 并不是“可以随便丢掉 Prepare”。在 MySQL 8.4.0 的源码注释中，它是为 *group commit* 延迟并合并准备记录的持久化工作；实际持久化时序不能仅凭这一个字段推断。

现在还差后两步：Binlog 写入、同步，然后完成引擎提交。

## 五、为什么 Binlog 不直接调用一次 write() 就返回？

打开 [`sql/binlog.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/binlog.cc#L8341-L8550)，找到 `MYSQL_BIN_LOG::ordered_commit()`。

它是 Binlog 组提交的关键入口。为避免一次抛出所有并发机制，我们暂时只顺着一笔正常事务看三个阶段：

**第一阶段：将事务写入 Binlog 文件。**

**真实源码节选（位于 Flush 阶段）：**

```cpp
flush_error =
    process_flush_stage_queue(&total_bytes, &do_rotate, &wait_queue);
if (flush_error == 0 && total_bytes > 0)
  flush_error = flush_cache_to_file(&flush_end_pos);
```

为什么不是事务一产生就直接落入 Binlog 正式文件？事务中的变更会先积累在事务 Binlog cache 中；当它准备提交时，再把整笔事务安排进入写入阶段。

**第二阶段：按 `sync_binlog` 配置同步 Binlog。**

**真实源码节选（位于 Sync 阶段）：**

```cpp
if (flush_error == 0 && total_bytes > 0) {
  std::pair<bool, bool> result = sync_binlog_file(false);
  sync_error = result.first;
}
```

这一步不能与上一阶段混为一谈。`write` 到文件，不等于掉电以后一定能够恢复；是否同步、同步频率由配置控制。

**第三阶段：安排存储引擎提交。**

**真实源码节选（位于 Commit 阶段）：**

```cpp
process_commit_stage_queue(thd, commit_queue);
```

这个队列处理过程会继续进入存储引擎提交相关逻辑。`ordered_commit()` 还包含组提交排队、错误处理、提交顺序等机制，我们在本章先不拆开。

它之所以存在，不是为了把一个 `COMMIT` 复杂化，而是为了同时满足：**Binlog 与引擎提交有正确关系**，以及**多笔并发事务可以合并部分 I/O 工作**。

完整的正常逻辑因此可以用本章已经认识的部件串起来：

```text
客户端 COMMIT
      |
      v
ha_commit_trans()                 sql/handler.cc
      |
      +--> tc_log->prepare()
      |        |
      |        +--> MYSQL_BIN_LOG::prepare()
      |                    |
      |                    +--> ha_prepare_low()
      |                              |
      |                              +--> InnoDB prepare 回调
      |
      +--> tc_log->commit()
                |
                +--> MYSQL_BIN_LOG::commit()
                           |
                           +--> ordered_commit()
                                   |
                                   +--> Flush Binlog
                                   +--> Sync Binlog（依配置）
                                   +--> Commit Engine
```

这是**教学整理的正常路径**，不是所有存储引擎、SQL 命令和错误分支的完整调用图。特别是 `XA PREPARE`、不写 Binlog 的事务、故障注入和组提交的并发细节另有分支。

## 六、真正断电之后，怎么把两边重新对齐？

到这一步，最值得问的不是“什么叫两阶段提交”，而是“**崩溃刚好发生在三个步骤之间，系统凭什么知道要 Commit 还是 Rollback？**”

让我们列出一次事务的四个可能断点：

| 崩溃位置 | InnoDB 待决状态 | Binlog 中有效且已持久化的事务 | 恢复时的教学判断 |
| --- | --- | --- | --- |
| Prepare 之前 | 无 | 无 | 不应出现已提交事务 |
| Prepare 之后、Binlog 之前 | 有 | 无 | 回滚该待决事务 |
| Binlog 持久化之后、Engine Commit 之前 | 有 | 有 | 完成提交 |
| Engine Commit 之后 | 已提交 | 有 | 保持提交 |

这张表以“日志确实按所需策略持久化、文件与硬件没有损坏、恢复能够识别完整事务”为前提。它不代表任何磁盘故障下都能自动无损恢复。

### 真实源码：崩溃恢复时谁来协调？

打开 [`sql/binlog/recovery.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/binlog/recovery.cc#L46-L56)，找到 `binlog::Binlog_recovery::recover()`。

**真实源码节选：**

```cpp
process_logs(m_reader);
if (!this->is_log_malformed() && total_ha_2pc > 1) {
  Xa_state_list xa_list{this->m_external_xids};
  this->m_no_engine_recovery = ha_recover(&this->m_internal_xids, &xa_list);
}
```

`process_logs()` 扫描 Binlog 恢复相关信息；在满足条件时，`ha_recover()` 进一步参与存储引擎事务恢复。这里的 `m_internal_xids` 用于内部事务的标识匹配；`m_external_xids` 则是另一类外部 XA 恢复信息，我们在本章不展开。

这段代码终于回答了刚才的问题：**恢复不只看 InnoDB 的页面，也会利用 Binlog 的有效事务记录，对准备中的事务作出最终处理。**

MySQL 8.4 官方 Binlog 文档也明确描述了这一机制：崩溃后扫描最新 Binlog 的有效事务信息，协调 InnoDB 完成与 Binlog 一致的准备事务，必要时截断无效的 Binlog 尾部。

与此同时，InnoDB 还需要根据自身 Redo 重做数据页修改、回滚未完成的普通事务。**Redo 恢复与 Binlog—InnoDB 事务协调是两层相关但不同的工作。**

## 七、动手复现：不靠 MySQL，也能验证两个“半成功”为什么危险

本章提供了可运行脚本：`examples/mysql_commit_recovery_lab.py`。下面保留足够完整的核心模型；完整版本随系列源码目录提供。

> **教学化简**：把“已持久化的状态”用 Python 集合表示，不实现真正的 `fsync`、Redo 格式、组提交、事务锁、Binlog 事件编码或 InnoDB 恢复算法。模拟的是**崩溃断点上的决策关系**，不是复刻 MySQL 的内部状态机。

```python
from dataclasses import dataclass, field

TX = "view-event-42-000101"

@dataclass
class TxModel:
    prepared: set[str] = field(default_factory=set)
    committed: set[str] = field(default_factory=set)
    binlog: set[str] = field(default_factory=set)

    def prepare(self, tx):
        self.prepared.add(tx)

    def sync_binlog(self, tx):
        assert tx in self.prepared
        self.binlog.add(tx)

    def engine_commit(self, tx):
        assert tx in self.prepared and tx in self.binlog
        self.prepared.remove(tx)
        self.committed.add(tx)

    def recover(self):
        for tx in list(self.prepared):
            if tx in self.binlog:
                self.committed.add(tx)
            self.prepared.remove(tx)

for cutoff in ("before prepare", "after prepare",
               "after binlog sync", "after engine commit"):
    db = TxModel()
    if cutoff != "before prepare":
        db.prepare(TX)
    if cutoff in ("after binlog sync", "after engine commit"):
        db.sync_binlog(TX)
    if cutoff == "after engine commit":
        db.engine_commit(TX)

    db.recover()
    assert (TX in db.committed) == (TX in db.binlog)
    print(cutoff, "=>", "commit" if TX in db.committed else "rollback")
```

实际运行后，这四种断点得到：

```text
before prepare => rollback
after prepare => rollback
after binlog sync => commit
after engine commit => commit
```

同一个实验文件还先演示了 Redo 对“磁盘数据页尚未刷盘”的恢复效果，以及两种朴素顺序留下的矛盾：

```text
redo: disk page=100, recovered page=101
naive engine-first: data=yes, binlog=no
naive binlog-first: data=no, binlog=yes
```

以上是**本章 Python 教学模型实际执行的结果**。没有启动 MySQL 8.4.0，也没有通过真实停机、断电、主从复制实验验证 MySQL；不能把这些断言当成 MySQL 的集成测试通过报告。

## 八、写入成功就绝对不丢了吗？

现在终于可以重新解释“`COMMIT` 成功”这句话的边界。

上述事务协调保证的是：**在正确的持久化和恢复前提下，Binlog 与 InnoDB 的事务结果能够被协调为一致。**

它**不自动保证**：

1. **副本已完成复制。** Binlog 在源库持久化，不代表另一个 MySQL 实例已读取、更不代表已经应用该事务。普通异步主从复制仍有延迟和切换时的丢失风险。
2. **磁盘永远可靠。** MySQL 官方建议需要持久性时使用 `sync_binlog=1` 和 `innodb_flush_log_at_trx_commit=1`；即使如此，仍依赖底层存储诚实执行同步请求。
3. **跨分片 XA 自动完成。** 本章的内部两阶段提交协调的是同一 MySQL 服务器内部的事务参与者，不会自动让两个独立 MySQL 分片变成一个全局事务。
4. **所有查询都具有全局顺序。** 单实例提交顺序、复制应用顺序和跨分片的业务顺序，是需要分别讨论的问题。

因此，对比前几章的 Redis，我们现在看到了一个新的重点：**Redis 的副本确认关注复制进度；MySQL 的 InnoDB + Binlog 协调还必须处理本机两套持久化状态在崩溃时的提交一致性。** 它们都涉及“成功确认”，但确认的对象并不相同。

## 九、回看：为什么 MySQL 要长成这样？

我们没有从“Redo、Binlog、2PC、Group Commit”四个术语开始背定义，而是让一次文章访问事务不断撞上新的限制：

```text
只修改内存数据页
    |
    +-- 崩溃后无法恢复刚提交的修改
    v
InnoDB Redo
    |
    +-- 本机能恢复，但还需要向副本表达事务变化
    v
Binlog
    |
    +-- 两份事务记录可能只完成一份
    v
Prepare + Binlog + Engine Commit
    |
    +-- 中途崩溃后必须知道该提交还是回滚
    v
Binlog 扫描 + InnoDB 恢复协调
```

到这里，整套机制不再像数据库中凭空多出来的“复杂模块”。每个部分都有一个具体存在理由。

但是我们的文章访问系统还有一个新需求没有解决：**如果每次访问都生成一个事件，多个服务需要异步消费这些事件，为什么不直接把所有事件写进一个全局有序队列？**

这个问题会把我们带到下一章：

**《分布式系统 10：一条日志不够快怎么办？——从单队列到 Kafka Partition》**。

届时仍沿用文章访问事件，但它在 Kafka 中代表的是可供消费者处理的消息，而不是数据库中已经提交的一行记录。我们会先让单队列遇到吞吐与顺序的矛盾，再去读 Kafka 的真实分区与追加日志实现。

---

## 固定版本源码与官方文档

- [MySQL 8.4.0 `sql/handler.cc`：`ha_commit_trans` / `ha_prepare_low`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/handler.cc)
- [MySQL 8.4.0 `sql/binlog.cc`：`MYSQL_BIN_LOG::prepare` / `commit` / `ordered_commit`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/binlog.cc)
- [MySQL 8.4.0 `sql/binlog/recovery.cc`：`Binlog_recovery::recover`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/binlog/recovery.cc)
- [MySQL 8.4.0 `storage/innobase/mtr/mtr0mtr.cc`：Redo 缓冲路径](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/mtr/mtr0mtr.cc)
- [MySQL 8.4.0 `storage/innobase/log/log0recv.cc`：Redo 恢复入口](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/log/log0recv.cc)
- [MySQL 8.4 官方文档：The Binary Log](https://dev.mysql.com/doc/refman/8.4/en/binary-log.html)
- [MySQL 8.4 官方文档：InnoDB Recovery](https://dev.mysql.com/doc/refman/8.4/en/innodb-recovery.html)
- [MySQL 8.4 官方文档：Binary Logging Options and Variables](https://dev.mysql.com/doc/refman/8.4/en/replication-options-binary-log.html)

**可验证性声明**：源码文件、标签和所展示的函数/语句已逐项核对；本文 Python 模型已运行。未编译 MySQL 源码，未执行真正的 MySQL 崩溃恢复或主从复制测试。
