---
title: "14｜从一台 MySQL 到另一台：商品库存怎样复制？"
sidebar_position: 15
slug: /mysql/14-how-replication-works
description: "沿一次库存扣减的 Binlog 事件，追踪 MySQL 8.4.0 的发送、Relay Log 接收、行事件应用与复制延迟。"
tags: [MySQL, InnoDB, 源码精读, Binlog, Replication]
---

# 从一台 MySQL 到另一台：商品库存怎样复制？

上一章，我们已经让商品 42 的库存修改不只存在于 InnoDB 的本地数据文件里。

```sql
UPDATE products
SET stock = stock - 1
WHERE id = 42 AND stock > 0;
```

事务提交之后，主库的库存从 `100` 变成 `99`，与这次事务对应的变更也进入了 Binlog。我们沿着 MySQL 的两阶段提交路径，研究了为什么需要协调存储引擎的提交与 Binlog。

现在，电商系统增加了第二台 MySQL。我们希望一部分查询由它来处理。

商品 42 在两台机器上最初都是 `100`。用户购买成功后，主库已经变成 `99`。

**第二台 MySQL 怎样知道这件事？它为什么有可能暂时仍然读到 `100`？**

这就是本章唯一要回答的问题。

我们不从“复制线程有哪些”开始背名词，而是从一次已经发生的库存变化开始，追踪它离开主库、到达从库、最终修改从库记录的过程。

## 1. 最简单的方案：主库修改后，再通知从库

假设我们自己实现商品数据管理，两台服务各自保存一个商品字典。

```python
source = {42: 100}
replica = {42: 100}
```

一次购买发生在主库：

```python
source[42] -= 1
```

此时：

```text
source[42] = 99
replica[42] = 100
```

为了让从库跟上，我们可以立刻再执行：

```python
replica[42] = source[42]
```

这样很容易得到 `99`。

但现在，考虑一个具体故障：主库已经修改成功，通知从库的网络请求却失败了。

如果把通知当作一次性的操作，没有任何地方保存“商品 42 已经发生了一次修改”，那么网络恢复后，我们凭什么知道应该补发哪些修改？

我们可能尝试每隔几秒把整张表重新同步一次。这个办法也能解决某些小规模问题，但当数据量达到千万级时，为了传播一条库存变化而反复比较大量未变化的数据，代价就很高。

因此我们缺少的，不是又一次查询商品 `42` 的能力，而是：

**把已经发生的修改按顺序记录下来，并让另一台机器能够从自己的进度继续获取尚未处理的部分。**

上一章刚好引入了 Binlog。它记录可以供其他实例消费的数据变更事件。我们先用最小代码模拟这一点。

## 2. 不要复制整张表，只传递已经发生的变更

**教学化简：下面的 Python 不是 MySQL 源码，也没有实现真实 Binlog 的编码格式。**

我们暂时定义一个事件：

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class RowChange:
    txid: str
    product_id: int
    before_stock: int
    after_stock: int
```

对于这次购买，事件可以理解为：

```python
RowChange(
    txid="purchase-001",
    product_id=42,
    before_stock=100,
    after_stock=99,
)
```

其中 `txid` 是教学模型为了标识这一次修改而设置的字符串，**不是 MySQL GTID 的真实格式**。

在单纯描述库存变化时，我们真正需要的是：“商品 `42` 的库存由 `100` 变成 `99`”。把这个变化保存到日志中，网络暂时中断也不会让发送方忘记它。

这又产生了一个新问题：**从库从哪里开始读取？**

如果每次连接都从日志开头读取，之前已处理过的事件会反复出现。于是我们给从库保存一个接收进度。

```python
binlog = [
    RowChange("purchase-001", 42, 100, 99)
]

received = 0
relay_log = []

for event in binlog[received:]:
    relay_log.append(event)
    received += 1
```

此时：

```text
Binlog：    [购买事务 001]
Relay Log： [购买事务 001]
received：  1
```

我们特意把收到的事件放进一个独立列表 `relay_log`，**还没有修改从库的库存**。

为什么要这么做？因为“收到了一个修改事件”和“成功把事件应用到自己的数据库”，是两个不同的动作。

如果把两者强行捆成一步，接收速度就会被数据库执行速度直接限制；而且网络读取与数据库修改的失败场景很不一样。

这正是理解 MySQL 复制的一个关键分界点。

## 3. 第一次看到复制延迟：收到事件不等于应用事件

继续刚才的模型。

主库执行完成后：

```text
主库数据：stock = 99
```

从库已经接收到事件，但还没执行：

```text
Relay Log：[100 → 99]
从库数据： stock = 100
```

这时有人连接从库，执行：

```sql
SELECT stock FROM products WHERE id = 42;
```

就可能读到 `100`。

**这并不意味着 Binlog 没有记录变更，也不意味着网络一定发生了故障。** 它可能仅仅表示从库尚未应用已经接收到的事件。

因此我们需要第二个进度：

```python
applied = 0

for event in relay_log[applied:]:
    replica[event.product_id] = event.after_stock
    applied += 1
```

完成后：

```text
主库数据：stock = 99
从库数据：stock = 99

已接收事件：1
已应用事件：1
```

到这里，我们已经把复制过程拆分成两个必要动作：

```text
      主库已经提交的变更
              |
              v
           Binlog
              |
              |  接收
              v
           Relay Log
              |
              |  应用
              v
          从库的数据页
```

这张图现在很小，每个箭头都有明确含义。

**Binlog** 是主库上的变更事件来源。**Relay Log** 是从库保存接收事件的本地日志。最终的数据修改由从库的事件应用过程完成。

但这只是我们自己的模型。真实 MySQL 是否也是这样分工？接下来进入固定版本源码验证。

---

## 4. 真实源码：主库怎样把 Binlog 发出去？

本章固定的源码版本是 **MySQL Server 8.4.0，Git Tag `mysql-8.4.0`**。所有涉及源码的片段都以该版本为准。

主库上负责发送 Binlog 事件的关键代码位于：

- 仓库：[`mysql/mysql-server`](https://github.com/mysql/mysql-server/tree/mysql-8.4.0)
- 文件：[`sql/rpl_binlog_sender.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/rpl_binlog_sender.cc)
- 入口：`Binlog_sender::run()`、`Binlog_sender::send_binlog()`、`Binlog_sender::send_events()`

先考虑接收端提出的要求：它要从某个日志位置开始获取主库的后续事件。主库不必再次执行最初的 UPDATE，而是读取已经生成的 Binlog 事件。

在 `Binlog_sender::run()` 的主循环中，可以找到：

**真实源码节选：`sql/rpl_binlog_sender.cc`，省略循环中的其他分支。**

```cpp
File_reader reader(opt_source_verify_checksum, max_event_size);

// ... 打开 Binlog 文件之后
if (send_binlog(reader, start_pos)) break;
```

这里的 `File_reader` 负责从 Binlog 文件读取事件。`start_pos` 是本次请求选择的读取起始位置。

它说明一个重要事实：**主库发送的是日志文件中的事件，不是在每次从库请求时重新执行购买 SQL。**

再进入 `send_binlog()`：

**真实源码节选：**

```cpp
if (send_events(reader, end_pos)) return 1;
```

调用发生时，已经确定了要读取的日志范围。接着进入 `send_events()`：

**真实源码节选：`Binlog_sender::send_events()`。**

```cpp
uchar *event_ptr = nullptr;
uint32 event_len = 0;

if (unlikely(read_event(reader, &event_ptr, &event_len))) return 1;
```

这一小段仅做一件事：读取下一个事件及其长度。它还没有把事件发到从库。

同一函数的发送路径中，可以找到：

```cpp
if (before_send_hook(log_file, log_pos)) return 1;
if (unlikely(send_packet())) return 1;
if (unlikely(after_send_hook(log_file, in_exclude_group ? log_pos : 0)))
  return 1;
```

这里存在发送前后的扩展钩子；真正通过连接发送数据包的关键动作是 `send_packet()`。

继续看 `send_packet()` 的网络写入调用：

```cpp
my_net_write(m_thd->get_protocol_classic()->get_net(),
             pointer_cast<const uchar *>(m_packet.ptr()),
             m_packet.length())
```

上述调用表达了我们需要的核心机制：已经读出的 Binlog 事件被组织到网络数据包中，沿当前复制连接发出。

至于连接如何协商起始位置、何时发送心跳、哪些事件需要跳过，属于这条正常链路的进一步边界，本章先不展开。

现在我们已经确认了主库侧：

```text
Binlog 文件
    |
    v
Binlog_sender::run()
    |
    v
send_binlog()
    |
    v
send_events()  -- 读取事件
    |
    v
send_packet()  -- 网络发送
```

接下来，从库收到这个数据包之后做什么？

## 5. 真实源码：为什么从库要先写 Relay Log？

这次打开从库侧的核心文件：

[`sql/rpl_replica.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/rpl_replica.cc)

其中的复制接收线程入口为：

```cpp
extern "C" void *handle_slave_io(void *arg)
```

这里源码名称仍保留了历史上的 `slave` 字样；MySQL 8.4 用户可见的命令和文档通常使用 `source`、`replica`、`receiver`、`applier` 等名称。我们以源码实际名称为准，不虚构一个 `handle_replica_io()` 函数。

在 `handle_slave_io()` 内部，先能看到从库向主库请求日志流：

**真实源码节选：**

```cpp
if (request_dump(thd, mysql, &rpl, mi, &suppress_warnings) ||
    DBUG_EVALUATE_IF("simulate_reconnect_after_failed_binlog_dump", 1,
                     0)) {
  // ... 错误与重连处理
}
```

这一段解释“谁主动请求”的问题：普通 MySQL 复制中，由从库接收端连接主库，并请求所需的 Binlog 事件流，而不是业务 UPDATE 主动给所有从库分别发送 SQL。

之后进入读取循环，可以找到：

```cpp
event_len = read_event(mysql, &rpl, mi, &suppress_warnings);
```

这是从复制连接接收事件的动作。

但是，**收到数据包并不等于库存已经被扣减**。

再往下，代码会把事件交给：

```cpp
QUEUE_EVENT_RESULT queue_res = queue_event(mi, event_buf, event_len);
```

`queue_event()` 负责把接收到的事件加入从库的 Relay Log 处理流程。它还需要考虑事件合法性、日志轮换以及复制元数据进度，因此不能简单理解成 Python 列表的 `append()`。

现在回头看教学模型：

```python
relay_log.append(event)
received += 1
```

虽然真实实现更复杂，但这个模型准确保留了当前最重要的职责：**接收线程先把从主库取得的变更记录下来，让后续应用过程处理。**

为什么这里不直接修改商品 `42`？

因为读取网络流与在 InnoDB 中修改本地数据是两种不同的工作。拆分后，接收线程可以继续收取日志，应用线程则可以独立推进执行进度。于是就允许我们在实验中观察到：

```text
事件已接收：是
事件已应用：否
主库 stock：99
从库 stock：100
```

下一步的问题就非常明确了：Relay Log 里已经有了事件，谁来消费它？

## 6. 真实源码：Relay Log 事件怎样变成从库的实际修改？

仍然查看 `sql/rpl_replica.cc`。

单线程应用路径的入口是：

```cpp
extern "C" void *handle_slave_sql(void *arg)
```

它会为应用过程建立执行上下文，并从 Relay Log 中取得事件。在正常的单线程应用循环中，可找到：

**真实源码节选：**

```cpp
ev = applier_reader.read_next_event();
```

这与接收线程中的 `read_event(mysql, ...)` 不一样。

- 接收线程中的 `read_event(mysql, ...)`：从主库的复制连接接收事件。
- 应用线程中的 `applier_reader.read_next_event()`：从从库本地的 Relay Log 读取待应用事件。

读出事件后，单线程应用循环接着调用：

```cpp
switch (exec_relay_log_event(thd, rli, &applier_reader, ev)) {
  // ... 执行结果与错误处理
}
```

这就是我们需要继续追踪的实际应用入口。

在 `exec_relay_log_event()` 调用的相关执行处理中，`apply_event_and_update_pos()` 会负责应用事件并推进处理进度。源码注释给出了它的核心语义：

**源码注释表达的逻辑，以下是概念伪代码，不是逐字源码：**

```cpp
ev->apply_event(rli);
ev->update_pos(rli);
```

为什么需要两个动作？

`apply_event` 是按事件类型真正处理数据变更；`update_pos` 则关系到事件处理进度。两者必须正确协调，否则重启后可能不知道应该从哪里继续。

在真实实现中，还包含事务分组、跳过事件、错误处理、重试和多线程调度等条件；不能把上面两行误解为所有情况下连续执行的全部代码。

至此，我们已经看到接收和应用是两条不同的执行路径：

```text
主库 Binlog ──网络──> 从库 Relay Log ──应用──> 从库数据
                      ↑                    ↑
                 handle_slave_io     handle_slave_sql
```

但还剩下最后一个关键问题：对于这次 `UPDATE products`，应用线程究竟怎样修改一行数据？

## 7. 从库不是把原始 UPDATE 再执行一遍

如果没有弄清这个细节，复制容易被误解成：

```text
主库执行：UPDATE products SET stock = stock - 1 ...
从库执行：UPDATE products SET stock = stock - 1 ...
```

这并非 MySQL 8.4 默认的行格式复制所采取的方式。

MySQL 8.4 的 `binlog_format` 默认值是 `ROW`。在行格式下，记录的是行数据变化事件，而不是要求从库重新计算原始 SQL 的完整逻辑。实际事件会以二进制形式保存，并包含表映射等必要信息；它并不是一条人类可直接阅读的 `UPDATE` 文本。

为了简化当前问题，我们可以把商品 42 的行更新事件理解成：

```text
目标记录：id = 42
修改前：stock = 100
修改后：stock = 99
```

真实事件所包含的列由行镜像配置和所需标识信息决定，因此不能断言每个事件一定包含整行的所有字段。

为什么要特别强调这一点？

因为这次库存扣减涉及 `stock = stock - 1`。如果复制时只是毫无边界地重新执行原始 SQL，就会让同一操作在不同实例上重复计算。行事件的核心是记录源端已经发生的行变化，然后在从库上重现对应结果。

现在进入 MySQL 的行事件源码。

文件：[`sql/log_event.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/log_event.cc)

在通用行事件执行路径里，可以找到：

```cpp
int Rows_log_event::do_apply_event(Relay_log_info const *rli)
```

它负责为行事件准备需要使用的表和执行环境，进一步应用事件。

对于 UPDATE 行事件，关键函数是：

```cpp
int Update_rows_log_event::do_exec_row(const Relay_log_info *const rli)
```

在它的源码注释里，能看到两个很重要的名称：

- **BI（Before Image）**：更新之前的行镜像，用于识别或比对待更新的记录。
- **AI（After Image）**：更新之后的行镜像，用于提供新值。

这两个名字此时才需要引入。因为我们终于遇到了“从库怎样知道修改前后是什么”的问题。

看这个函数的一段真实源码：

```cpp
store_record(m_table, record[1]);

m_curr_row = m_curr_row_end;
if ((error = unpack_current_row(rli, &m_cols_ai, true /*is AI*/)))
  return error;
```

这里已经定位到了相应的旧记录，并开始解出更新后的行镜像。

紧接着可以看到：

**真实源码节选：`Update_rows_log_event::do_exec_row()`。**

```cpp
error = m_table->file->ha_update_row(m_table->record[1], m_table->record[0]);
if (error == HA_ERR_RECORD_IS_THE_SAME) error = 0;
```

这一次，我们终于重新遇见了前面章节熟悉的**存储引擎行修改接口**。

调用的输入是更新前和更新后的记录表示；调用产生的主要副作用是请求存储引擎完成行更新。底层 InnoDB 会继续处理具体记录与页面修改，最终由事务执行路径协调提交。

这也回答了“从库是不是重新跑原始 SQL”的问题：**在 ROW 复制模式下，从库按行事件重现已发生的变更，而不是从头解析并执行用户最初输入的那条 UPDATE 文本。**

需要注意，存在 STATEMENT、MIXED 等历史格式和特殊事件类型；本章固定 MySQL 8.4 的常见 ROW 复制路径，不将这一结论扩展到所有复制配置。

---

## 8. 一个可以实际运行的复制模型

前面分段写了许多教学代码。现在将它们合成一个脚本，完整观察三个时间点：**主库完成修改、从库已接收、从库已应用**。

保存为 `replication_demo.py`，使用 Python 3.10+ 执行。

**教学化简：这段代码只验证分阶段复制行为，不能验证真实 MySQL 网络协议、Relay Log 持久性或事务提交语义。**

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class RowChange:
    txid: str
    product_id: int
    before_stock: int
    after_stock: int


class MiniReplication:
    def __init__(self):
        self.source = {42: 100}
        self.replica = {42: 100}
        self.binlog = []
        self.relay_log = []
        self.received = 0
        self.applied = 0
        self.applied_ids = set()

    def buy(self, product_id, txid):
        old = self.source[product_id]
        assert old > 0
        self.source[product_id] = old - 1
        self.binlog.append(
            RowChange(txid, product_id, old, old - 1)
        )

    def receive(self):
        for event in self.binlog[self.received:]:
            self.relay_log.append(event)
            self.received += 1

    def apply(self):
        for event in self.relay_log[self.applied:]:
            if event.txid not in self.applied_ids:
                assert self.replica[event.product_id] == event.before_stock
                self.replica[event.product_id] = event.after_stock
                self.applied_ids.add(event.txid)
            self.applied += 1


m = MiniReplication()

m.buy(42, "purchase-001")
print("主库已提交:", m.source[42])
print("从库未接收:", m.replica[42])

m.receive()
print("已接收事件数:", m.received)
print("从库尚未应用:", m.replica[42])

m.apply()
print("从库应用后:", m.replica[42])

m.apply()
assert m.source[42] == m.replica[42] == 99
assert m.applied == 1
print("重复应用扫描后:", m.replica[42])
```

**实际运行结果（Python 教学模型）：**

```text
主库已提交: 99
从库未接收: 100
已接收事件数: 1
从库尚未应用: 100
从库应用后: 99
重复应用扫描后: 99
```

这个实验值得观察的不是最后的 `99`，而是中间明确存在的一种状态：

```text
Binlog 已记录变更
Relay Log 已接收变更
从库的数据仍未修改
```

它解释了为什么复制延迟可以发生在**已经收到日志之后、尚未应用完毕之前**。

教学模型通过 `received` 和 `applied` 两个下标表示进度，通过内存集合 `applied_ids` 忽略已应用事务。这只是为了说明“记录进度、避免重复处理”的需要。它不是 MySQL GTID 的实现，更不意味着这样一个内存集合可以处理真实故障恢复或者保证外部操作精确一次。

到这里，我们已经能解释复制的主路径。接下来，应该到真实 MySQL 环境中观察同样的现象。

## 9. 真实 MySQL 双实例实验：让从库先接收、暂不应用

为了避免演示中的复杂初始化掩盖本章的核心问题，下面**假设已经有一套配置完成的 MySQL 8.4 主从环境**：

- 主库与从库都存在 `mysql_source_lab.products` 表；
- 商品 `42` 的初始库存相同；
- 主库启用 Binlog，从库正在正常复制；
- 本实验在专用环境中进行，不是线上业务数据库；
- 假设只研究一个复制通道。若启用了多个通道，应指定 `FOR CHANNEL`。

建立复制关系本身涉及初始化数据快照、复制账号、认证、安全传输、日志位置或 GTID 自动定位。它们需要严格配置，但不是本章要解释的核心机制，不能用几条未经验证的 `CHANGE REPLICATION SOURCE TO` 命令草率覆盖已有环境。

### 9.1 先确认源端使用 ROW 格式

在主库执行：

```sql
SHOW VARIABLES LIKE 'log_bin';
SHOW VARIABLES LIKE 'binlog_format';
```

期望 Binlog 已启用，复制使用行格式。MySQL 8.4 默认使用 `ROW`，但必须以实验实例上的实际配置为准。

随后在主库准备数据：

```sql
USE mysql_source_lab;

UPDATE products
SET stock = 100
WHERE id = 42;

SELECT stock FROM products WHERE id = 42;
```

确认主库返回 `100`。此时还不能立即开始下一步，要先让从库正常应用完这次准备数据的更新，确认从库的商品库存也已经是 `100`。

### 9.2 只停止从库的应用线程

在**从库**执行：

```sql
STOP REPLICA SQL_THREAD;
```

注意：这里停止的是应用线程，而不是接收线程。这个命令会影响当前复制拓扑，必须仅在隔离实验环境中操作，并确认命令完成；如果启用了并行应用，应确保应用停止后再观察状态。

再执行：

```sql
SHOW REPLICA STATUS\G
```

典型观察重点：

```text
Replica_IO_Running: Yes
Replica_SQL_Running: No
```

具体字段值以实验实例为准。如果接收线程本来就有连接问题，就不能把后面的现象全部归因于暂停应用。

此时让接收线程继续工作：它仍然能够从主库取得新的 Binlog 事件并写入 Relay Log，但暂时没有应用线程把新事件变成数据变更。

### 9.3 在主库完成购买

回到**主库**执行：

```sql
UPDATE mysql_source_lab.products
SET stock = stock - 1
WHERE id = 42 AND stock > 0;

SELECT ROW_COUNT() AS affected_rows;

SELECT stock
FROM mysql_source_lab.products
WHERE id = 42;
```

正常情况下，这一次 UPDATE 应该影响一条记录，主库库存变成 `99`。

此时再去从库执行：

```sql
SELECT stock
FROM mysql_source_lab.products
WHERE id = 42;
```

**预期仍然得到 `100`。**

这是本章最重要的观察。因为我们人为暂停了复制应用阶段，即使主库早已提交成功，从库的数据也不会因为主库提交而瞬间同步修改。

如果从库接收线程仍正常运行，新事件可以继续进入 Relay Log。可结合 `SHOW REPLICA STATUS\G` 中的接收位置、应用位置等信息辅助排查；但不要把单个位置字段或 `Seconds_Behind_Source` 简单当成“数据已一致”的证明。

### 9.4 恢复应用线程

在从库执行：

```sql
START REPLICA SQL_THREAD;
```

确认应用线程已经开始工作，并等到这次 UPDATE 对应的变更成功应用后，再执行：

```sql
SELECT stock
FROM mysql_source_lab.products
WHERE id = 42;
```

此时预期得到：

```text
99
```

至此，我们在真实 MySQL 中应该可以观察到同一条商品记录的三个状态：

| 阶段 | 主库库存 | 从库库存 | 原因 |
|---|---:|---:|---|
| 购买前 | 100 | 100 | 两端已同步 |
| 主库已提交、从库停止应用 | 99 | 100 | 变更尚未被从库应用 |
| 从库恢复应用并追上事务后 | 99 | 99 | Relay Log 中的更新事件已应用 |

**验证边界：** 本节是可复现的 MySQL 8.4 双实例实验设计。本章没有实际启动双 MySQL 实例，也没有执行 `STOP REPLICA` 等命令，因此上述数据库结果均为基于限定条件的预期值，不冒充真实环境的运行日志。

### 9.5 可选：查看实际行事件

如果要进一步确认主库记录的是行变更，可以在有权限且保留对应 Binlog 的实验实例上，使用 `mysqlbinlog` 查看所需时间范围内的 Binlog 文件。例如：

```bash
mysqlbinlog --base64-output=DECODE-ROWS -vv \
  /path/to/actual/binlog-file
```

路径必须替换为实验机器上真实的 Binlog 文件路径，不要照抄到生产环境。工具输出可能包含业务数据，应只在受控环境使用。

如果配置为 `ROW` 且事件未被过滤，预期可以看到与表映射以及更新行事件有关的信息；`-vv` 会尽可能显示可读的行变更表示。输出中的 `### UPDATE ...` 是工具为帮助人阅读生成的表示形式，并不意味着从库重新执行了这一段原始 SQL。

---

## 10. 为什么主库成功不意味着从库立刻可读？

到这里，已经不需要再引入新的大组件。因为问题的答案就在刚才的执行流程里。

主库的事务提交、从库接收 Binlog、从库实际应用事件，是**三个不同的进度**。

默认的 MySQL 复制是异步复制。主库不需要等待所有从库都完成事件应用才向客户端返回购买成功。这样可以减少主库被其他实例处理速度拖慢的情况，但也意味着从库可能暂时落后。

现在设想购买成功的用户立刻查询库存：

```text
用户购买 → 主库 UPDATE 成功 → 应用把下一次 SELECT 路由到从库
```

即使购买操作确实已经提交，下一次从从库读取，仍可能暂时看到旧库存。

因此，“读写分离”不自动提供“写后立即从任意副本读到最新值”的保证。如果业务必须读到刚刚提交的购买结果，就需要额外设计读写路由或等待特定复制进度。例如，可以让写后的一段关键查询继续访问主库，或者在已启用 GTID 的条件下等待从库应用到目标事务，再进行读取。

**GTID 此时才值得提一下**：它标识一个已提交事务，可帮助复制双方确认有哪些事务已经发生和应用。它不是复制线程本身，也不会仅因为开启 GTID 就消除复制延迟。GTID 的生成、自动定位和等待细节适合另设专题，这里不超前展开。

半同步复制也不能与“所有从库已经应用完成”画等号。常见的半同步确认语义涉及副本接收并记录事务事件，而不要求其 SQL 应用线程已经更新本地商品库存；因此，半同步不是任意副本即时读取的一般保证。

## 11. 这条复制路径的边界

经过这章，我们可以回答一次库存扣减怎样到达第二台 MySQL，但还应明确几个边界。

**第一，Relay Log 已接收不等于事务已应用。** 我们已经用教学实验和暂停应用线程的 MySQL 实验设计观察了这个区别。

**第二，主库有 Binlog 不意味着从库一定最终追上。** 网络故障、复制权限、事件缺失、源端过早清理日志、从库应用错误、复制过滤和长期性能瓶颈，都可能使复制停止或落后。

**第三，从库通常也会使用 InnoDB 的完整修改能力。** 它不会因为是“副本”就绕过存储引擎、事务和数据页更新。我们在 `Update_rows_log_event::do_exec_row()` 中已经看到了 `ha_update_row()`，它会再次进入熟悉的数据修改路径。

**第四，ROW 模式与原始 SQL 重放不同。** 本章只把行事件中与商品库存相关的部分教学化为“100 → 99”。真实 Binlog 事件包含协议头、表映射、列位图和事务边界等必要信息。

**第五，不要把复制当成强一致数据库的自动开关。** 主从复制能够传播提交的修改，默认异步模式下的副本读仍可能滞后；高可用、故障切换以及严格一致读需要额外的设计和条件。

这些边界并不削弱复制的作用。相反，它们明确了本章真正解释的是哪一项机制：**怎样将主库已经记录的变化，按可跟踪的接收与应用进度，变成另一台实例上的数据修改。**

## 12. 回看：一次库存扣减经过了哪些真实源码？

现在终于可以把本章出现的真实函数按**发送、接收、应用**三段拼起来。

```text
主库已经提交购买事务
  商品 42：stock 100 → 99
                 |
                 v
            主库 Binlog
                 |
                 v
sql/rpl_binlog_sender.cc
  Binlog_sender::run()
  Binlog_sender::send_binlog()
  Binlog_sender::send_events()
  send_packet()
                 |
              网络传输
                 |
                 v
sql/rpl_replica.cc
  handle_slave_io()
  read_event(mysql, ...)
  queue_event(...)
                 |
                 v
            从库 Relay Log
                 |
                 v
sql/rpl_replica.cc
  handle_slave_sql()
  applier_reader.read_next_event()
  exec_relay_log_event()
  apply_event_and_update_pos()
                 |
                 v
sql/log_event.cc（ROW 更新事件）
  Rows_log_event::do_apply_event()
  Update_rows_log_event::do_exec_row()
                 |
                 v
  m_table->file->ha_update_row(...)
                 |
                 v
          从库 InnoDB 修改库存
```

这是正常单线程、ROW 格式路径的**关键调用与数据流汇总**，不是完整的逐帧调用栈；其中存在事件分发、表映射、事务分组和执行准备等中间步骤。并行应用还会引入协调器和工作线程，本章没有假装所有事件都必然在单线程中完成。

每段代码回答了一个具体问题：

- `Binlog_sender`：主库怎样读取已经记录的日志，并发送给请求者？
- `handle_slave_io()` 与 `queue_event()`：从库收到事件后，怎样先保存下来？
- `handle_slave_sql()` 与 `exec_relay_log_event()`：谁来消费本地 Relay Log 中的事件？
- `Update_rows_log_event::do_exec_row()`：行事件最终怎样作用于真实的数据记录？

从最初的：

```python
replica[42] = source[42]
```

到现在的这条真实源码路径，我们不是为了“完整”而增加组件，而是依次解决了：网络失败后如何知道补什么、怎样把接收与执行分开、如何追踪应用进度、如何把行变更交给存储引擎。

至此，**一次库存扣减从单台 MySQL 的提交，到另一台 MySQL 的复制应用，已经形成了一条完整的理解路线。**

## 13. 下一章：回到最初的 UPDATE，重新走完整条执行路径

我们从最初的商品购买开始，一路学习了请求进入、SQL 执行、主键定位、磁盘页面、Buffer Pool、记录修改、并发锁、事务与 Undo、MVCC、Redo、Checkpoint、Binlog 以及复制。

现在，所有机制都已经在自己的问题背景下出现过。

下一章不再急着加入新的重要术语，而是重新执行：

```sql
UPDATE products
SET stock = stock - 1
WHERE id = 42 AND stock > 0;
```

我们将从客户端请求开始，沿 MySQL Server 与 InnoDB 的实际调用路径，一直追踪到更新完成与事务提交，并把此前理解过的日志和并发机制放回它们真正发挥作用的位置。

最后，再用这次购买的关键失败场景，检查每一种机制究竟保证了什么、没有保证什么。

**下一章：第 15 章《一次库存扣减的完整源码旅程》。**

---

## 本章源码与参考资料

**代码版本：** MySQL Server 8.4.0，Git Tag [`mysql-8.4.0`](https://github.com/mysql/mysql-server/tree/mysql-8.4.0)。下表中的函数名和文件路径按该版本核对。

| 文件 | 真实源码入口 | 当前用途 |
|---|---|---|
| [`sql/rpl_binlog_sender.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/rpl_binlog_sender.cc) | `Binlog_sender::run()`、`send_binlog()`、`send_events()` | 主库读取并发送 Binlog |
| [`sql/rpl_replica.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/rpl_replica.cc) | `handle_slave_io()`、`queue_event()` | 从库接收并写入 Relay Log |
| [`sql/rpl_replica.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/rpl_replica.cc) | `handle_slave_sql()`、`exec_relay_log_event()`、`apply_event_and_update_pos()` | 从库读取并应用事件 |
| [`sql/log_event.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/log_event.cc) | `Rows_log_event::do_apply_event()`、`Update_rows_log_event::do_exec_row()` | 将 ROW 更新事件落实到存储引擎 |

**官方文档：**

- [MySQL 8.4：Replication](https://dev.mysql.com/doc/refman/8.4/en/replication.html)
- [MySQL 8.4：Replication Threads](https://dev.mysql.com/doc/refman/8.4/en/replication-threads.html)
- [MySQL 8.4：Replication Formats](https://dev.mysql.com/doc/refman/8.4/en/replication-formats.html)
- [MySQL 8.4：Relay Log 与复制元数据](https://dev.mysql.com/doc/refman/8.4/en/replica-logs.html)
- [MySQL 8.4：STOP REPLICA](https://dev.mysql.com/doc/refman/8.4/en/stop-replica.html)
- [MySQL 8.4：GTID Life Cycle](https://dev.mysql.com/doc/refman/8.4/en/replication-gtids-lifecycle.html)

**验证说明：** 已实际执行 Python 教学实验，并检查其输出与断言；源码片段已经按固定版本核对；真实 MySQL 双实例实验提供了明确的前置条件、操作步骤和预期结果，但没有实际执行，不能视为对真实服务器的故障或复制性能测试。
