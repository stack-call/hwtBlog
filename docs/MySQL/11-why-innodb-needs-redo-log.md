---
title: "11｜提交成功之后突然断电：为什么需要 Redo Log？"
sidebar_position: 12
slug: /mysql/11-why-redo-log
description: 沿商品 42 的库存扣减，从脏页尚未落盘的故障窗口出发，理解 MySQL 8.4.0 的 Redo 生成、写盘、事务持久化与崩溃恢复。
tags: [MySQL, InnoDB, Redo Log, 源码精读, 崩溃恢复]
---

# 提交成功之后突然断电：为什么需要 Redo Log？

上一章，我们研究了这样一个问题：事务 A 已把商品 42 的库存从 `100` 改成 `99`，但还没有提交，事务 B 的普通 `SELECT` 为什么仍可能读取到 `100`？

我们沿着 Undo 和历史版本理解了这个现象。现在，暂时把并发读取放在一边，回到这次购买本身：

```sql
UPDATE products
SET stock = stock - 1
WHERE id = 42 AND stock > 0;
```

假设这次操作已经提交，客户端也得到了成功结果。此刻服务器突然断电，重新启动后，商品库存应该是多少？

直觉告诉我们：**既然已经告知购买成功，就不能重新变回 100。**

可是在第 07 章，我们已经知道：InnoDB 可以先在 Buffer Pool 中修改数据页，磁盘上的数据页不必同步写回。也就是说，在某个时刻，系统中可能存在这样的状态：

```text
                       同一个商品 42

Buffer Pool 中的数据页                 磁盘上的数据页
       stock = 99                         stock = 100
          （新）                              （旧）
```

如果此时整个服务器崩溃，内存中的新页面消失，磁盘上只剩下旧页面。数据库怎么知道商品曾被成功扣减过一次？

这就是本章唯一需要解决的核心问题：**如何允许数据页延后落盘，同时在故障发生后重建已经确认的修改？**

我们先设计一个最简单的方案，再进入 MySQL 8.4.0 的真实源码。

## 1. 最简单的办法：每次提交都把数据页写回磁盘

如果怕内存数据丢失，为什么不要求每次购买都立刻保存整个数据页？

沿用此前的简化页面模型，我们可以想象这样一个顺序：

```text
找到商品 42 所在的数据页
       ↓
在内存中将 stock 从 100 改成 99
       ↓
把修改后的数据页写回磁盘并完成必要的持久化操作
       ↓
告诉用户购买成功
```

这个方案在只有一个商品、一个小文件时，可以成立。它避免了“刚刚确认的修改只停留在进程内存中”的问题。

但为什么真实数据库不愿意把它作为每次事务提交的唯一办法？

回想之前的页面结构：一个页面里不止一件商品，而且一次事务可能修改多个不相邻的页面。假设有三个购买请求，分别修改位于不同磁盘位置的页面：

```text
请求 A → 商品 42  → 页面 17
请求 B → 商品 105 → 页面 809
请求 C → 商品 901 → 页面 33
```

如果每个事务都必须等所有相关数据页完成持久化之后才返回，数据库就不得不在提交时频繁处理分散的数据页写入。这不仅涉及磁盘 I/O，页面还可能已经在 Buffer Pool 中反复修改，过早地多次写回同一页也会浪费工作。

这里真正需要的是一种新能力：

> **不要求每次事务提交时立即写回所有被修改的数据页，但必须留下足够可靠的信息，让故障后的数据库能够补上那些尚未落盘的修改。**

注意，我们没有要求把全部页面复制一份。页面里可能有大量没有改变的数据。与其反复保存整个页面，能不能先保存“页面发生了什么修改”所需的信息？

这时，才有必要引入 **Redo Log（重做日志）**。

## 2. 先只记录一次库存变化

为了理解 Redo 的意义，我们先不模拟 InnoDB 的复杂物理日志格式。

假设磁盘上有一个数据页文件，里面的库存仍是 `100`；另一个文件用于保存一条恢复信息：

```text
数据页文件（还未写回）：
    商品 42，库存 = 100

持久化的日志：
    对页面中的商品 42，恢复后的库存应为 99
```

只要这条恢复信息已经可靠保存，即使内存中更新过的页面突然消失，重启时也可以把磁盘上的旧状态补成新状态。

这看起来像是把“写整个页面”改成了“先写少量描述修改的信息”。**关键不只是日志更小，而是让日志能够先被持久化，数据页以后再写。**

如果我们只是在内存里维护一张日志列表，机器崩溃时日志同样会消失，什么问题都没有解决。因此，需要把下面三件事情严格分开：

```text
A. 内存中的数据页已经变成 99
B. 描述修改的 Redo 已经可靠持久化
C. 数据页本身已经持久化为 99
```

在我们的目标设计中，允许 **A、B 已完成而 C 尚未完成**；但是不能在缺少必要持久化保证时，就把“提交已经可靠完成”告诉用户。

还要区分：写入操作系统文件缓存，与要求操作系统把相关数据真正刷到持久介质，是不同的动作。后面查看源码时，我们会看到 MySQL 对这两步作了明确区分。

## 3. 一个可运行的最小崩溃恢复实验

先用纯 Python 重现“日志已持久化，数据页尚未写回，进程突然退出”的场景。

这个实验不需要 MySQL，也不需要破坏任何数据库文件。它只在临时目录中写入两个教学文件：

- `page.json`：模拟磁盘上的商品数据页；
- `redo.jsonl`：模拟能够帮助恢复的修改记录。

与真实 InnoDB 不同，这里为了让结果容易阅读，日志记录了“更新后的库存绝对值”，而非真实 InnoDB 的页级重做记录编码。我们还用 `applied_lsn` 标记哪些教学日志已经被应用，以避免重复恢复。**这是一种教学模型，不是 MySQL Redo 的文件格式。**

把以下代码保存为 `redo_toy_test.py`，直接使用 Python 3 运行：

```python
"""教学化简：演示 redo 文件先落盘，数据页尚未落盘时的恢复。"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def durable_save(path: Path, value: dict) -> None:
    with path.open('w', encoding='utf-8') as file:
        json.dump(value, file, ensure_ascii=False)
        file.flush()
        os.fsync(file.fileno())


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding='utf-8'))


def worker(root: Path) -> None:
    page = load(root / 'page.json')
    page['stock'] -= 1   # 修改内存中的页面副本；故意不写回 page.json
    with (root / 'redo.jsonl').open('a', encoding='utf-8') as log:
        log.write(json.dumps({'lsn': 1, 'stock_after': page['stock']}) + '\n')
        log.flush()
        os.fsync(log.fileno())
    print('工作进程：内存库存=99，Redo 已刷盘，数据页未写回', flush=True)
    os._exit(17)  # 仅终止教学进程，模拟进程突然退出


def recover(root: Path) -> int:
    page_path = root / 'page.json'
    page = load(page_path)
    for line in (root / 'redo.jsonl').read_text(encoding='utf-8').splitlines():
        record = json.loads(line)
        if record['lsn'] > page['applied_lsn']:
            page['stock'] = record['stock_after']
            page['applied_lsn'] = record['lsn']
    durable_save(page_path, page)
    return page['stock']


def demo() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        durable_save(root / 'page.json', {'stock': 100, 'applied_lsn': 0})
        (root / 'redo.jsonl').write_text('', encoding='utf-8')
        print('初始磁盘库存:', load(root / 'page.json')['stock'])
        result = subprocess.run(
            [sys.executable, __file__, 'worker', str(root)],
            capture_output=True, text=True, check=False,
        )
        print(result.stdout.strip())
        print('工作进程退出码:', result.returncode)
        print('崩溃后磁盘库存:', load(root / 'page.json')['stock'])
        print('第一次恢复后库存:', recover(root))
        print('重复恢复后库存:', recover(root))
        assert load(root / 'page.json') == {'stock': 99, 'applied_lsn': 1}


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'worker':
        worker(Path(sys.argv[2]))
    else:
        demo()

```

这个脚本刻意让工作进程通过 `os._exit(17)` 直接退出，不运行正常的 Python 清理流程；父进程仍然存活，可以检查文件并执行恢复。`os.fsync()` 用于在进程退出前请求持久化日志文件的数据。

**本章已实际执行这段 Python 程序**，得到：

```text
初始磁盘库存: 100
工作进程：内存库存=99，Redo 已刷盘，数据页未写回
工作进程退出码: 17
崩溃后磁盘库存: 100
第一次恢复后库存: 99
重复恢复后库存: 99
```

观察这几个时刻：

1. 工作进程在内存中把库存修改成 `99`。
2. 工作进程将用于恢复的记录写入日志文件，并请求刷盘。
3. 工作进程非正常退出，没有把新库存写回数据页文件。
4. 父进程读取磁盘上的数据页，发现它依然是 `100`。
5. 恢复程序读取日志，把磁盘页补成 `99`。
6. 再执行一次恢复，不会把库存扣成 `98`。

最后一点特别重要。恢复过程可能需要在异常后重新开始，所以它不能盲目地把“扣一件”再执行一次。我们的教学模型用递增编号和已应用编号避免重复应用；真实 InnoDB 则结合日志位置、页面状态及相关恢复规则来判断如何安全重做。

到这里，我们首次亲眼看到 Redo 的价值：**即使数据页文件仍然是旧值，只要保留了必要的已持久化修改信息，就可能重建新值。**

当然，这个实验没有模拟真正的操作系统崩溃、电源故障、部分日志记录损坏、并发事务或 InnoDB 的物理页面格式。尤其是 `fsync()` 返回的语义仍依赖操作系统与硬件，而示例也省略了目录项持久化等细节。它只验证本章的核心设计思想。

接下来，我们必须回到真实源码中确认：MySQL 是怎样把修改的信息放进 Redo 的？

---

## 4. MySQL 真正记录的不是一条 SQL 字符串

固定版本：

| 项目 | 值 |
| --- | --- |
| 仓库 | [mysql/mysql-server](https://github.com/mysql/mysql-server) |
| Tag | [`mysql-8.4.0`](https://github.com/mysql/mysql-server/tree/mysql-8.4.0) |
| Redo 生成相关文件 | `storage/innobase/mtr/mtr0mtr.cc` |
| 事务提交相关文件 | `storage/innobase/trx/trx0trx.cc` |
| Redo 写入与刷盘 | `storage/innobase/log/log0write.cc` |
| 恢复相关文件 | `storage/innobase/log/log0recv.cc` |

需要先纠正一个很容易产生的印象：Redo Log **不是简单地把 `UPDATE products SET stock = stock - 1 ...` 这段 SQL 保存下来，重启时再执行一遍**。

InnoDB 的 Redo 服务于存储引擎内部的数据结构和页面修改恢复。它记录的是足以重做相关低层修改的信息，而不是直接保存供 SQL 执行器重新执行的业务语句。

为什么这样设计合理？

因为我们要恢复的是数据库内部哪些页面的修改尚未完整体现在数据文件里，而不是要求数据库重走一次包含 SQL 解析、优化、加锁、条件判断的业务执行路径。重走 SQL 也可能把库存再次减一，甚至面对与故障前不同的数据条件。

上一章已经认识 Undo 记录，这里需要区分：

- **Undo**：支持撤销尚未完成的事务修改，也用于构造一致性读取所需的旧版本。
- **Redo**：支持在崩溃恢复时重做必要的存储修改，让应保留的变化不因数据页尚未落盘而丢失。

它们不是彼此的替代品。实际上，崩溃恢复中也可能需要先重做包括未完成事务相关的页面修改，再处理未完成事务的回滚。因此，不能说“Redo 里只有已提交事务的记录”。

现在，我们从上一章已经学过的页面修改过程继续。

## 5. 页面修改的信息怎样进入 Redo Buffer？

在 InnoDB 中，一些底层页面修改会由一个 *mini-transaction*（通常缩写为 `mtr`）组织。为了避免术语空降，先记住它在此处的最小作用：**协调一组底层页面操作及其对应的 Redo 记录**。

`mtr` 不是用户发起的 SQL 事务。`mtr_t::commit()` 也**不等于** SQL 的 `COMMIT`。一条用户事务可能涉及多个底层 `mtr`。

打开 [mtr0mtr.cc](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/mtr/mtr0mtr.cc)，定位 `mtr_t::commit()`。在包含日志或相应修改的正常分支中，有这样的调用：

**真实源码节选，MySQL 8.4.0：**

```cpp
Command cmd(this);

if (has_any_log_record() ||
    (has_modifications() && m_impl.m_log_mode == MTR_LOG_NO_REDO)) {
  // 原函数这里还有断言
  cmd.execute();
}
```

这里省略了原函数的部分断言与 `else` 逻辑；它展示的是有日志或相应修改时的关键路径。

`cmd.execute()` 会怎样处理修改日志？继续找到 `mtr_t::Command::execute()`。

**真实源码节选，保留关键语句，省略中间校验代码：**

```cpp
auto handle = log_buffer_reserve(*log_sys, len);

m_impl->m_log.for_each_block(write_log);

add_dirty_blocks_to_flush_list(handle.start_lsn, handle.end_lsn);

log_buffer_close(*log_sys, handle);
```

这一小段足以解释本章要理解的三个动作。

**第一，`log_buffer_reserve()` 为当前底层修改生成的 Redo 在日志缓冲区保留空间。**

**第二，`m_log.for_each_block(write_log)` 把本次积累的日志内容写入这个缓冲区。** `write_log` 内部的 `mtr_write_log_t::operator()` 会调用：

```cpp
end_lsn =
    log_buffer_write(*log_sys, block->begin(), block->used(), start_lsn);
```

这里的 `log_buffer_write()` 是**写入内存中的 Redo Log Buffer**，不要一看到 `write` 就认定日志已经持久化到磁盘。

**第三，`add_dirty_blocks_to_flush_list()` 将本次修改涉及的数据页纳入脏页管理。** 这使日志位置与需要在未来刷盘的数据页产生了联系。

从这个微型路径，我们可以得到一个初步的、尚未包含物理写盘的图：

```text
商品 42 所在的数据页已经被修改
                 |
                 v
底层修改形成 Redo 记录
                 |
                 v
    mtr_t::Command::execute()
                 |
         +-------+-------+
         |               |
         v               v
  写入 Redo Buffer   修改页加入刷盘管理
```

这解释了**日志怎样产生和进入缓冲区**，却还没回答：什么时候才能说日志已经安全地写到磁盘？

## 6. 为什么写入 Redo Buffer 还不够？

回到刚才的 Python 实验。我们要求日志文件在进程退出前完成写入与 `fsync`，因为如果恢复信息也只存在内存中，崩溃时就没有任何可靠的补救依据。

真实 MySQL 同样需要区分这几个状态：

```text
生成 Redo 记录
      |
      v
写入 InnoDB 内存中的 Redo Buffer
      |
      v
写入操作系统管理的 Redo 文件
      |
      v
请求将 Redo 刷到持久存储
```

这里的 `write` 和 `flush` 不是一个含义。

为了说明“日志进展到哪里”，InnoDB 使用一个逐步前进的日志位置，称为 **LSN（Log Sequence Number）**。

第一次遇到 LSN，只需要把它理解为 Redo 日志流中的一个位置编号。它帮助系统表达：已经产生、写出或持久化到了哪个位置。

比如下面的数字只是教学示意：

```text
Redo 已经生成到：     LSN 1200
Redo 已经写入文件到： LSN 1150
Redo 已经刷到磁盘到： LSN 1100
```

它们不一定实时完全相同。

于是，如果某个提交需要 Redo 至少持久化到 `LSN 1180`，单凭“日志已经生成到 1200”还不能认为条件满足。

这时，我们真正需要的是：**提交过程能够等待 Redo 写入或刷盘达到指定的 LSN。**

接下来去事务提交源码找这一步。

## 7. 事务提交时，MySQL 怎样等待 Redo？

打开 [trx0trx.cc](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/trx/trx0trx.cc)，找到：

```cpp
static void trx_flush_log_if_needed_low(lsn_t lsn)
```

它与用户事务的日志持久化要求有关。先看最相关的 `switch` 分支。

**真实源码节选，MySQL 8.4.0：**

```cpp
switch (srv_flush_log_at_trx_commit) {
  case 2:
    flush = false;
    [[fallthrough]];
  case 1:
    wait_stats = log_write_up_to(*log_sys, lsn, flush);
    return;
  case 0:
    return;
}
```

这里省略了统计更新和注释，保持了控制分支的语义。需要注意：`flush` 变量在这个 `switch` 之前已经初始化，其最终行为还会受到平台和文件刷盘方式影响。

现在我们才需要引入一个配置项：

```sql
SHOW GLOBAL VARIABLES LIKE 'innodb_flush_log_at_trx_commit';
```

在默认配置 `1` 下，事务提交路径要求把相关 Redo 写入并刷盘。配置为 `2` 时，提交通常要求把 Redo 写到文件，但不必在每次提交时完成磁盘刷盘；配置为 `0` 时，提交时不执行这一步同步等待，写入与刷盘主要由后台流程进行。官方文档也提醒，`0` 和 `2` 会增大异常退出时事务丢失的风险。

这一章不建议为了验证而把生产库参数改成 `0` 或 `2`。

回看函数名：

```cpp
log_write_up_to(*log_sys, lsn, flush)
```

三个信息都很关键：

- `*log_sys`：当前 Redo 日志系统；
- `lsn`：本次需要达到的位置；
- `flush`：是否要求刷到持久介质。

它不是让提交线程逐个把所有已修改的数据页写回磁盘，而是为这次提交建立相应的**日志写入/刷盘完成条件**。

再往前一点，`trx_commit_complete_for_mysql()` 在需要延后刷盘的路径中，会调用：

```cpp
trx_flush_log_if_needed(trx->commit_lsn, trx);
```

为什么要允许延后处理？源码中提到配合组提交，避免在不合适的锁保护范围内把多个事务的日志刷盘串行化。这提醒我们：实际提交链路还有协调和批量化细节。

但就当前问题而言，已经得到了最重要的证据：**事务持久化判断与 Redo 的 LSN 进度有关，而不是强制每个数据页在返回之前全部刷盘。**

这里还不能把整个 Server 层的事务提交归结为这一个函数：启用 Binlog 时，MySQL 还要协调 Server 和 InnoDB 的提交阶段。这部分会在后续 Binlog 章节展开。

## 8. `log_write_up_to()`：写入和刷盘的分界

继续打开 [log0write.cc](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/log/log0write.cc)，找到：

```cpp
Wait_stats log_write_up_to(log_t &log, lsn_t end_lsn, bool flush_to_disk)
```

这里我们不读几百行线程协调实现，只看它要等待什么。

当要求刷盘时，函数会检查：

**真实源码节选：**

```cpp
if (log.flushed_to_disk_lsn.load() >= end_lsn) {
  return wait_stats;
}
```

如果 `flushed_to_disk_lsn` 已经不小于目标位置，说明不必再等待同一个目标的刷盘完成。

否则，在后台日志线程工作的路径中，它会等待相应进度：

```cpp
wait_stats += log_wait_for_flush(log, end_lsn, &interrupted);
```

而当只要求写入、不要求刷盘时，对应检查使用的是写入位置 `log.write_lsn`，必要时调用 `log_wait_for_write()`。

我们现在已经可以把提交要求表示成两个不同的条件：

```text
仅要求日志写入文件：write_lsn >= 目标 LSN

要求日志刷到持久介质：flushed_to_disk_lsn >= 目标 LSN
```

在 MySQL 8.4.0 的实现中，正常高并发情况下还存在独立的 `log_writer()`、`log_flusher()` 等后台处理流程。提交线程可以等待它们推进相关位置，而不必自己始终完成全部 I/O。

因此，不要把下面三句话混为一谈：

1. 数据页已经在 Buffer Pool 里被修改。
2. 这次修改的 Redo 已写入日志文件。
3. 这次修改的 Redo 已经按要求刷盘。

**它们可能在不同时刻成立。** 这正是本章理解崩溃恢复的关键。

## 9. 崩溃以后，InnoDB 怎么知道该从哪里恢复？

现在假设：客户端收到成功结果，相关 Redo 已按持久化策略保留，但某些商品数据页仍是旧版本。

服务器重启时不能简单假定数据文件已经完整。它需要读取日志，判断哪些尚未体现在数据页上的修改需要重做。

在 MySQL 8.4.0 的 [log0recv.cc](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/log/log0recv.cc) 中，可以找到恢复入口：

```cpp
dberr_t recv_recovery_from_checkpoint_start(log_t &log, lsn_t flush_lsn)
```

函数中有这样一段关键逻辑：

**真实源码节选：**

```cpp
Log_checkpoint_location checkpoint;
if (!recv_find_max_checkpoint(log, checkpoint)) {
  ib::error(ER_IB_MSG_RECOVERY_CHECKPOINT_NOT_FOUND);
  return DB_ERROR;
}
```

为什么恢复要先寻找 Checkpoint？

如果 MySQL 已经运行数天，期间产生了大量 Redo，而许多较早的页面修改早已写回数据文件，那么每次恢复都从数据库创建之初开始处理全部日志没有必要。

Checkpoint 用来记录可作为恢复起点的重要进度。它帮助数据库确定哪些历史 Redo 不再是本次恢复所必需的。

本章先只掌握这个作用；下一章会专门研究 Checkpoint 怎样与脏页刷盘和日志空间联系起来。

接下来，恢复逻辑会读取、解析必要的 Redo 记录，并根据对应页面的情况应用修改。

在同一源码文件中，我们还可以找到：

```cpp
static void recv_parse_log_recs()
```

以及：

```cpp
void recv_apply_hashed_log_recs(log_t &log, bool allow_ibuf)
```

其中处理具体页面恢复任务的代码会进一步进入 `recv_apply_log_rec()` 等函数，根据表空间与页号定位要应用日志的页面。

把本章真正需要理解的路径压缩一下：

```text
服务器异常退出
       |
       v
重新启动 InnoDB
       |
       v
查找可用的 Checkpoint
       |
       v
读取并解析必要的 Redo
       |
       v
定位相关页面并应用需要的修改
       |
       v
处理尚未完成的事务等恢复工作
       |
       v
逐步恢复正常服务
```

请注意，这不是“先读 Redo，然后所有事务都自动算作已提交”。Redo 中也可能包含未完成事务产生的修改。**恢复数据页与确定事务最终是否应该保留，是相互关联但不同的工作。**

MySQL 官方恢复文档明确区分了 Redo 应用与未完成事务回滚。后者会用到上一章研究的 Undo，而不会靠简单地把 Redo 逆向执行解决。

## 10. 用真实 MySQL 观察日志进度，而不是伪造断电实验

我们已经通过 Python 教学模型实际复现了进程突然退出和恢复。现在回到真正的 MySQL。

为避免破坏现有数据库，本章的 MySQL 实验**只观察状态，不人为杀死 MySQL 或切断电源**。

在专用实验库执行：

```sql
USE mysql_source_lab;

UPDATE products
SET stock = 100
WHERE id = 42;

SHOW GLOBAL VARIABLES LIKE 'innodb_flush_log_at_trx_commit';
```

为了避免客户端退出时还有未提交事务，以下操作使用自动提交模式：

```sql
SET SESSION autocommit = 1;

UPDATE products
SET stock = stock - 1
WHERE id = 42 AND stock > 0;

SELECT ROW_COUNT() AS affected_rows;

SELECT stock FROM products WHERE id = 42;
```

预期修改一条记录，库存由 `100` 变成 `99`。

接着查看 InnoDB 的状态：

```sql
SHOW ENGINE INNODB STATUS;
```

如果使用 `mysql` 命令行客户端，可以写成：

```sql
SHOW ENGINE INNODB STATUS\G
```

在输出的 `LOG` 部分，重点识别这些字段：

```text
Log sequence number
Log written up to
Log flushed up to
Pages flushed up to
Last checkpoint at
```

这些字段帮助我们区分日志产生进度、写入进度、刷盘进度，以及页面和检查点的推进情况。实际数值取决于实例已有的运行负载，不应照搬任何固定数字。

也可以查看相关全局状态：

```sql
SHOW GLOBAL STATUS LIKE 'Innodb_redo_log_%lsn';
```

其中的 `Innodb_redo_log_current_lsn`、`Innodb_redo_log_flushed_to_disk_lsn` 和 `Innodb_redo_log_checkpoint_lsn` 等状态，可以从不同角度观察日志进度。

注意三点：

- 这些是**实例级**状态，不仅包括商品 42 的一次 UPDATE，还可能包含其他连接或后台活动。
- 两次取样之间，后台刷盘线程可能已经推进了位置；不应期待每次查询都能稳定捕获“页未刷、日志已刷”的短暂窗口。
- `SHOW ENGINE INNODB STATUS` 不能取代一个受控的物理崩溃测试；正常退出或正常重启也不能等同于突然断电。

**验证边界：** 本章已实际执行 Python 教学实验；MySQL SQL 观察步骤未在真实 MySQL 8.4.0 实例上执行，所述库存结果属于预期行为，不声称已完成真实数据库故障注入测试。

## 11. Redo 到底解决了什么，没有解决什么？

现在我们可以更精确地回答开头的问题。

**它解决的是：** 当已确认的修改尚未全部写回数据页时，数据库仍可以依靠按要求保存的 Redo，在故障后重做所需的物理修改。

**它不等于：**

- **Undo。** Redo 面向重做与恢复；事务撤销和旧版本读取还有 Undo 的职责。
- **Binlog。** Redo 服务 InnoDB 内部恢复；Binlog 服务 Server 层的数据变更记录与复制等功能。它们如何在提交时协调，要到后续章节再讨论。
- **事务已经提交的唯一标志。** Redo 里也可能包含尚未提交事务的底层修改，恢复后必须结合事务状态正确处理。
- **每次事务提交都要刷所有数据页。** 恰好相反，本章引入它的主要动机，就是把事务需要的日志持久化与数据页的延后写回区分开。
- **无条件、无边界地抵抗任何故障。** `innodb_flush_log_at_trx_commit` 配置、文件系统、操作系统与存储设备对刷盘的真实保证，都会影响最终持久性。默认配置 `1` 提供最强的常规提交刷盘要求，但硬件谎报持久化、严重介质损坏等并不能靠一个参数彻底排除。

这也解释了为什么上一章的事务回滚和本章的崩溃恢复不能合并成一句“有日志就安全了”。二者保护的对象和面对的失败窗口不同。

## 12. 从最简单的库存扣减，走到今天的结果

回看我们的商品 42。

最开始，只需要：

```python
products[42]["stock"] -= 1
```

后来，我们把数据放进 MySQL，利用索引定位记录，通过 Buffer Pool 中的页面完成更新。

但由于数据页不会每次立即写回磁盘，出现了一个新的故障窗口：

```text
内存数据页：99
磁盘数据页：100
```

我们先尝试“每次提交都同步保存整个页面”，发现频繁更新不同页面可能带来很高的写入成本。

于是引入 Redo：

```text
修改内存中的页面
       |
       v
产生用于恢复的 Redo
       |
       v
根据提交持久性要求等待日志写入/刷盘
       |
       v
数据页允许稍后刷盘
       |
       v
如果异常退出，启动恢复重做必要修改
```

接着，我们用 MySQL 8.4.0 的真实源码验证了三个关键阶段：

1. `mtr_t::Command::execute()` 把底层修改的 Redo 放入 Log Buffer，并管理相关脏页。
2. 事务提交相关的 `trx_flush_log_if_needed_low()` 与 `log_write_up_to()` 根据要求推进或等待日志写入、刷盘位置。
3. `recv_recovery_from_checkpoint_start()` 等恢复入口从检查点和 Redo 出发，重建必要的页面修改。

这些机制共同说明：**让数据页延后落盘，不等于允许已经确认的提交随意丢失；关键是先保留足以在故障后恢复的可靠信息。**

但现在，我们又有了下一层具体问题。

如果商品 42 一天之内被修改几十万次，相关页面就会持续变脏，Redo 也会持续产生。数据库不可能永远保存无限量日志，也不能让脏页始终不写回磁盘。

**什么时候应该把数据页写回？什么时候可以认为一段旧 Redo 不再需要？**

下一章，我们就从这个问题出发，研究刷脏页和 Checkpoint。

**下一章：第 12 章《数据库持续运行——为什么需要刷脏页与 Checkpoint？》**

---

## 本章源码与资料

源码固定为 MySQL 8.4.0，Git Tag `mysql-8.4.0`，源码中未展开的控制条件或辅助逻辑均已标明为省略。

| 研究内容 | 位置 |
| --- | --- |
| mini-transaction 提交与 Redo 缓冲区写入 | [`storage/innobase/mtr/mtr0mtr.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/mtr/mtr0mtr.cc) |
| 用户事务提交时的日志持久化判断 | [`storage/innobase/trx/trx0trx.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/trx/trx0trx.cc) |
| 日志写盘、刷盘及 LSN 等待 | [`storage/innobase/log/log0write.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/log/log0write.cc) |
| 从 Checkpoint 开始恢复并解析、应用 Redo | [`storage/innobase/log/log0recv.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/log/log0recv.cc) |
| Redo 设计与存储 | [MySQL 8.4 官方文档：Redo Log](https://dev.mysql.com/doc/refman/8.4/en/innodb-redo-log.html) |
| 故障恢复步骤 | [MySQL 8.4 官方文档：InnoDB Recovery](https://dev.mysql.com/doc/refman/8.4/en/innodb-recovery.html) |
| 提交刷盘参数 | [MySQL 8.4 官方文档：innodb_flush_log_at_trx_commit](https://dev.mysql.com/doc/refman/8.4/en/innodb-parameters.html) |
| 日志状态的观察方式 | [MySQL 8.4 官方文档：InnoDB Standard Monitor](https://dev.mysql.com/doc/refman/8.4/en/innodb-standard-monitor.html) |

特别说明：本章中的 `redo.jsonl` 是教学模拟，不是 InnoDB 的 `#innodb_redo` 日志文件；`os._exit()` 模拟教学进程异常退出，不是对 MySQL 的真实断电测试。
