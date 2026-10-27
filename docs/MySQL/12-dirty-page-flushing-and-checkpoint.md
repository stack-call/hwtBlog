---
title: "12｜数据库持续运行：为什么需要刷脏页与 Checkpoint？"
sidebar_position: 13
slug: /mysql/12-dirty-page-flushing-and-checkpoint
description: 从商品库存持续更新和 Redo 空间压力出发，借助可运行实验与 MySQL 8.4.0 源码理解脏页刷盘、Flush List、Checkpoint LSN 及其恢复边界。
tags: [MySQL, InnoDB, Buffer Pool, Redo Log, Checkpoint, 源码精读]
---

# 数据库持续运行：为什么需要刷脏页与 Checkpoint？

上一章，我们终于回答了一个让人不安的问题：商品 42 的库存已经从 `100` 扣减为 `99`，客户端也收到了提交成功的结果，但包含这条记录的**数据页还没有写回磁盘**。如果 MySQL 此时崩溃，该怎么恢复？

我们看到，InnoDB 不需要每次提交都立即写回整个数据页。它可以先可靠保存足以支持恢复的 Redo，再在合适的时候把脏页写回数据文件。

这让一次购买可以更高效地完成。但一个始终运行的数据库会不断收到新的购买请求：

```sql
UPDATE products
SET stock = stock - 1
WHERE id = 42 AND stock > 0;
```

假设商品 42 很热门，每秒都发生多次修改。它所在的页面可以反复在 Buffer Pool 中变脏；与此同时，Redo 还会持续增长。

现在出现了一种与上一章不同的风险：**一次故障可以靠 Redo 恢复，但如果几个月里永远只写 Redo、从不把脏页安全地写回磁盘，有限的日志空间迟早会用尽。**

本章就解决这一个问题：**怎样在允许数据页延后写回的同时，持续推进恢复起点，让旧 Redo 不必永远保留？**

我们仍然围绕商品库存扣减展开，不提前讨论 Binlog 或复制。

> 源码版本：MySQL Server **8.4.0**，Git Tag [`mysql-8.4.0`](https://github.com/mysql/mysql-server/tree/mysql-8.4.0)。本文所有“真实源码节选”均指向这一版本；省略代码会明确说明。文中的 Python 小模型不是 InnoDB 实现。

---

## 1. 只用 Redo 恢复，为什么还不够？

回到上一章留下的状态：

```text
购买商品 42，库存：100 → 99

Buffer Pool 中的页面          磁盘数据页
    stock = 99                stock = 100
        │
        └── Redo 已按要求持久化
```

如果现在崩溃，恢复程序可以借助 Redo，把尚未反映到磁盘数据页中的修改补回来。

但把时间拉长一点：

```text
第 1 次购买： 100 → 99      产生 Redo A
第 2 次购买：  99 → 98      产生 Redo B
第 3 次购买：  98 → 97      产生 Redo C
……
第 N 次购买：               继续产生 Redo
```

假如我们完全不写回数据页，那么重启时仍可能需要很早以前的修改信息。更严重的是，磁盘上的 Redo 文件不可能无限扩张。

最简单的想法是：当 Redo 快满时，直接删除前面一半。

但这样做安全吗？

不安全。假设商品 42 的磁盘库存依然为 `100`，而让它变为 `99` 的那段日志已被删除。突然断电后，数据库就失去了补回这次修改的依据。

所以我们真正需要的不是“把日志删除得更快”，而是：

**先让某些修改确实进入磁盘数据页，再确认哪些更早的 Redo 已不再是崩溃恢复所必需的。**

这会引出两个彼此相关、却不能混为一谈的动作：

- **刷脏页**：把 Buffer Pool 中修改过的页面写回其持久化存储。
- **推进 Checkpoint**：在满足恢复安全条件后，把“恢复可以从哪里开始”的位置向前推进。

先研究第一个动作。

## 2. 既然库存已经变成 99，何时把页面写回？

我们在第 07 章知道，`UPDATE` 可以在内存中的数据页上修改记录。修改后的页面被标记为“脏”，表示其内容与持久化数据页可能不同。

一个最简单的刷盘函数可以长这样：

```python
# 教学化简：不模拟页头、并发控制、文件系统持久化等。
def flush_page(page_id, cache, disk):
    disk[page_id] = cache[page_id]
```

假设商品 42 的页面在内存里是 `99`，磁盘里还是 `100`，执行一次 `flush_page()` 后：

```text
刷盘前：缓存 99，磁盘 100
刷盘后：缓存 99，磁盘 99
```

那么，是不是每次执行 `UPDATE`，紧接着把它所在的页面刷盘就行？

这又会退回上一章已经发现的问题：多个事务可能反复修改同一页，要求每次更新后同步刷完整个页面，会带来不必要的写入开销。

因此，更合理的直觉是：**修改路径不必同步等待每个相关页面落盘；让另一个执行过程在合适的时间，分批处理待写回的页面。**

这种安排还提出了一个很具体的需求：刷页的执行过程如何知道**哪些页面脏了**，以及该先刷哪些？

如果每次都扫描 Buffer Pool 中的全部页面，再一个个检查是否修改过，缓存很大时会浪费大量工作。

于是才需要专门管理待写回页面的信息。

## 3. 一批待写回的页面：为什么需要 Flush List？

先把问题简化成三个页面：

```text
页面 P17：商品 42   已修改
页面 P31：商品 105  已修改
页面 P52：商品 901  未修改
```

我们只需要将 P17 与 P31 纳入刷盘候选集合，P52 没必要参与。

这个集合可以先用普通列表表示：

```python
# 教学化简
flush_list = ["P17", "P31"]
```

每次一个原本干净的页面首次变脏，就把它加入这个集合。刷盘完成并满足相关条件后，再从待刷集合移除。

但如果只记录页面 ID，还不够支撑后面要讨论的 Redo 回收。因为我们不知道这些页面究竟是**从哪次日志修改开始**落后于磁盘的。

例如：

```text
LSN 10：修改 P17 上的商品 42
LSN 20：修改 P31 上的商品 105
LSN 30：再次修改 P17 上的商品 42
```

此刻两个页面都脏，但存在一个差异：

| 页面 | 首次变脏对应的教学 LSN | 当前内存值 |
| --- | ---: | ---: |
| P17 / 商品 42 | 10 | 98 |
| P31 / 商品 105 | 20 | 9 |

这里第一次引入 **LSN**：它可以暂时理解为 Redo 写入进度所对应的逻辑位置。真实 InnoDB 的 LSN 是字节位置，并不是我们示例中的 10、20、30 这样的操作序号。

为什么要记住“首次变脏”的位置，而不是只记最后修改的位置？

因为如果 P17 从 LSN 10 开始一直没有刷盘，那么磁盘 P17 可能缺少从 LSN 10 开始的一系列修改。即使它又在 LSN 30 被改过，也不能认为从 LSN 30 之前的日志都不再重要。

我们去真实源码里看看：InnoDB 是否也把待写回页面和日志位置联系在一起？

### 3.1 真实源码：将修改过的页面纳入 Flush List

文件：[`storage/innobase/buf/buf0flu.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/buf/buf0flu.cc)

函数：`buf_flush_insert_into_flush_list()`。

**真实源码节选（保留函数声明，省略函数内部大量并发和链表处理）：**

```cpp
void buf_flush_insert_into_flush_list(
    buf_pool_t *buf_pool,
    buf_block_t *block,
    lsn_t lsn)
```

三个参数分别告诉我们：

- `buf_pool`：属于哪个 Buffer Pool 实例。
- `block`：哪一个缓存块中的页面被修改了。
- `lsn`：与该页较早的修改位置有关的日志位置（此处作为 `oldest modification` 传入）。

因此，Flush List 不是一个只存“脏页名字”的 Python 列表；它与页面控制结构、并发同步以及日志位置有关。

在 InnoDB 的真实更新路径中，mini-transaction 会产生物理修改记录，并在相关提交路径中将脏页纳入后续刷盘管理。**这里不是业务事务 COMMIT 的同义词。** 我们这一章不重新展开上一章的 mtr 细节，只需要确认：页面变脏后，后续刷盘过程有结构可以追踪它。

那么，真正负责从这个集合里挑页面并发起写入的，又是谁？

## 4. 刷页不必由购买请求自己完成

假设商品 42 不断被购买。

如果每次扣减库存都要停下来等待数据页完全写回，用户请求延迟就容易受页面 I/O 影响。

可以让一个后台执行过程负责脏页的批量写入。MySQL 的这一类处理由 **Page Cleaner** 相关线程协调。

对于当前问题，我们只要研究一条正常路径：后台工作线程拿到批量刷页请求后，从待刷集合里选出合适的页面。

### 4.1 真实源码：批量刷 Flush List

同样打开 [`buf0flu.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/buf/buf0flu.cc)。

在 `pc_flush_slot()` 的刷页分支中，可以找到：

**真实源码节选：**

```cpp
slot->succeeded_list = buf_flush_do_batch(
    buf_pool, BUF_FLUSH_LIST, slot->n_pages_requested,
    page_cleaner->lsn_limit, &slot->n_flushed_list);
```

这次调用包含一个刚好与我们的问题对应的参数：`BUF_FLUSH_LIST`。

它明确表示：本次批量处理针对的是待写回脏页列表，而非为了腾出内存空间进行的 LRU 刷页。

紧接着继续查找 `buf_do_flush_list_batch()`，会看到它从 Flush List 的尾部寻找候选页面。

**真实源码节选（保留循环条件，省略循环体其他部分）：**

```cpp
for (buf_page_t *bpage = UT_LIST_GET_LAST(buf_pool->flush_list);
     count < min_n && bpage != nullptr && len > 0 &&
     bpage->get_oldest_lsn() < lsn_limit;
     bpage = buf_pool->flush_hp.get(), ++scanned) {
```

现在这段代码就不再是陌生的函数名堆砌了。

我们先有了“哪些页面脏了”的问题，所以需要 `flush_list`；又因为 Redo 和脏页必须协调进度，所以挑选页面时可以使用它的较早修改位置 `get_oldest_lsn()`；因为不能无限制地一次刷完所有页面，所以还有本次处理量 `min_n` 和日志位置上限 `lsn_limit`。

真实执行中还有许多安全检查，尤其是页面正被访问、正在进行 I/O 或其他线程也在刷页的情况。当前先理解正常路径，不把所有并发分支提前展开。

还有一个值得区分的概念：**Buffer Pool 的 LRU 淘汰与 Flush List 刷脏页不等价**。前者主要与空闲缓存块的供给有关，后者可以用于推动脏页持久化和 Redo 恢复进度；真实系统会协调多种刷页来源。

但是，能够刷脏页，并不意味着我们已经知道哪些 Redo 可以丢弃。

下面用一个故意设计的失败例子，看看问题究竟在哪里。

---

## 5. 一个反例：商品 42 的页面已刷盘，全部旧日志就能删了吗？

我们把同一次购买扩展为两个商品都有人购买。这不是换了业务场景，仍然是同一个商品系统里的库存扣减，只是多出一个需要刷盘的页面。

初始磁盘状态：

```text
P17 / 商品 42： 100
P31 / 商品 105： 10
```

现在顺序执行三个已提交修改。为了方便演示，假设它们对应教学 LSN 10、20、30：

```text
LSN 10：商品 42  库存 100 → 99
LSN 20：商品 105 库存 10 → 9
LSN 30：商品 42  库存 99 → 98
```

内存状态变为：

```text
P17 / 商品 42： 98
P31 / 商品 105： 9
```

随后后台刷页过程先把 P17 写回磁盘，却还没写 P31。

```text
                    Buffer Pool          磁盘
商品 42 / P17          98                 98    ✓ 已写回
商品 105 / P31          9                 10    ✗ 尚未写回
```

因为 P17 已经落盘，有人可能会得出结论：“既然最新一次修改 LSN 30 的页面都已经刷盘，那日志位置 30 之前也都安全了。”

**这个结论是错的。**

因为 P31 的第一次未持久化修改发生在 LSN 20。若把 LSN 20 的日志一起丢掉，数据库崩溃后商品 105 又会变回 10。

这里真正应该跟踪的不是“哪一次修改最新”，也不是“商品 42 的页面是否已经落盘”，而是：

> **整个系统里，仍未安全写回的数据页中，最早缺失的那一段恢复信息在哪里？**

这才把问题从“刷页”推向 **Checkpoint**。

## 6. Checkpoint 应该代表什么？

最简单的理解：Checkpoint 是一个**可以安全作为恢复起点的日志位置**。

假设我们能证明：在某个位置以前需要反映到数据页上的变更，都已安全进入磁盘数据文件，那么崩溃恢复就可以不再依赖那个位置之前的 Redo。

对应刚才的例子：

```text
LSN 10：P17 更新  -> 已由后来刷盘的 P17 覆盖
LSN 20：P31 更新  -> P31 仍未刷盘
LSN 30：P17 更新  -> 已由 P17 刷盘覆盖
```

因此，恢复至少仍需保留 P31 在 LSN 20 的更新信息。我们的教学模型可以把 Checkpoint 放在 `20`，保留从 `20` 开始的日志。

注意：即使 LSN 30 对应的 P17 更新已经落盘，在这种连续日志管理模型里，它也可能与 LSN 20 一起暂时保留。**Redo 的可回收边界不是针对单个商品任意删掉几条日志，而是一个整体上可以安全推进的前缀边界。**

真实 MySQL 的 Redo 存储与 LSN 计算要复杂得多，我们只保留了“整个系统的最早未持久化修改会约束恢复起点”这个核心事实。

接下来，先让这个结论在真正运行的最小模型中经受验证。

## 7. 可运行实验：错误的 Checkpoint 会让库存恢复出错

下面的程序**无需 MySQL、无需第三方依赖、无需 API Key**，Python 3.10+ 即可运行。

它只包含三个持久化相关角色：

1. `disk`：磁盘上的页面状态。
2. `cache`：Buffer Pool 的教学替身。
3. `redo`：假设已经持久化的修改日志。

另外，用 `first_dirty` 记录每个尚未刷盘页面最早的修改位置，并用 `checkpoint_lsn` 记录恢复起点。

**教学化简，不是 MySQL 源码，也不模拟事务并发、物理 Redo 格式或真实断电。**

```python
from dataclasses import dataclass
from copy import deepcopy


@dataclass(frozen=True)
class Redo:
    lsn: int
    page_id: int
    stock: int


class MiniStore:
    """每次 update 都视为已提交，Redo 视为已经持久化。"""

    def __init__(self):
        self.disk = {42: 100, 105: 10}
        self.cache = {}
        self.first_dirty = {}
        self.redo = []
        self.next_lsn = 10
        self.checkpoint_lsn = 10

    def update(self, product_id):
        current = self.cache.get(product_id, self.disk[product_id])
        new_stock = current - 1
        self.cache[product_id] = new_stock

        lsn = self.next_lsn
        self.next_lsn += 10
        self.redo.append(Redo(lsn, product_id, new_stock))
        self.first_dirty.setdefault(product_id, lsn)
        return lsn

    def flush(self, product_id):
        self.disk[product_id] = self.cache[product_id]
        del self.first_dirty[product_id]
        # 真实系统在写页前还必须满足 WAL 等持久化约束。

    def checkpoint(self):
        safe = min(self.first_dirty.values(), default=self.next_lsn)
        self.checkpoint_lsn = safe
        self.redo = [r for r in self.redo if r.lsn >= safe]
        return safe

    def crash_and_recover(self):
        recovered = self.disk.copy()
        for record in self.redo:
            if record.lsn >= self.checkpoint_lsn:
                recovered[record.page_id] = record.stock
        return recovered


s = MiniStore()
print("修改 LSN:", [s.update(i) for i in (42, 105, 42)])
print("缓存:", s.cache, "磁盘:", s.disk)

s.flush(42)
print("刷商品 42 后:", "磁盘", s.disk, "仍然脏的页面", s.first_dirty)

unsafe = deepcopy(s)
unsafe.checkpoint_lsn = unsafe.next_lsn
unsafe.redo.clear()  # 错误：不顾其他脏页，直接丢弃全部日志
print("错误：仅凭商品 42 刷盘就丢弃全部日志 ->", unsafe.crash_and_recover())

cp = s.checkpoint()
print("正确检查点:", cp, "仍需保存的 Redo LSN:", [r.lsn for r in s.redo])
assert cp == 20
assert s.crash_and_recover() == {42: 98, 105: 9}
print("第一次崩溃恢复:", s.crash_and_recover())

s.flush(105)
cp = s.checkpoint()
print("全部脏页刷完后的检查点:", cp, "保留日志:", s.redo)
assert cp == 40
assert s.crash_and_recover() == {42: 98, 105: 9}
print("第二次崩溃恢复:", s.crash_and_recover())
```

本章编写时已**实际执行**这段 Python 程序，输出为：

```text
修改 LSN: [10, 20, 30]
缓存: {42: 98, 105: 9} 磁盘: {42: 100, 105: 10}
刷商品 42 后: 磁盘 {42: 98, 105: 10} 仍然脏的页面 {105: 20}
错误：仅凭商品 42 刷盘就丢弃全部日志 -> {42: 98, 105: 10}
正确检查点: 20 仍需保存的 Redo LSN: [20, 30]
第一次崩溃恢复: {42: 98, 105: 9}
全部脏页刷完后的检查点: 40 保留日志: []
第二次崩溃恢复: {42: 98, 105: 9}
```

这个实验有三个关键现象。

**第一个现象：只刷 P17 不够。** 商品 42 的库存确实落盘了，但商品 105 的磁盘数据仍然落后于内存。删掉所有 Redo 会让商品 105 恢复错误。

**第二个现象：Checkpoint 可以保守地停在 20。** 保留 LSN 20 和 30 的日志，即可在模型中恢复所有已提交修改。这里重做对 P17 的更新是冗余的；真实 InnoDB 依靠自己的日志格式和页面 LSN 等机制决定如何安全应用重做记录，不能直接照搬这里的“赋值覆盖”。

**第三个现象：P31 也刷盘后，Checkpoint 才能继续前进。** 当所有相关数据页都已持久化，模型已经不再依赖这些旧日志，便可以移动恢复起点并释放日志空间。

到这里，我们终于知道 Checkpoint 为什么不是简单的“最新日志位置”。它必须是一个**已被所有相关页面持久化进度证明安全的位置**。

现在，我们回到 MySQL 8.4.0，检查它究竟用什么信息计算这个边界。

---

## 8. 真实源码：怎样计算“可以推进到哪里”？

文件：[`storage/innobase/log/log0chkp.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/log/log0chkp.cc)

函数：`log_compute_available_for_checkpoint_lsn()`。

在我们的 Python 程序里，Checkpoint 核心判断是：

```python
safe = min(first_dirty.values(), default=next_lsn)
```

真实 InnoDB 不能这么简单。原因是修改页面和将页面加入 Flush List 可能由不同线程交错执行。

如果一个线程已经产生了 Redo，却还没来得及把对应脏页加入 Flush List，另一个线程就认为“当前没有更早的脏页”，并把 Checkpoint 推得太远，就有可能丢掉恢复所需的日志。

因此，InnoDB 不仅要观察 Flush List 中较早的修改位置，还要知道：**截至哪个 LSN，相应的脏页已经确实完成了加入 Flush List 的登记？**

### 8.1 不能只看 Flush List 当前看到的最早页面

**真实源码节选：`log_compute_available_for_checkpoint_lsn()`，省略中间的断言和注释。**

```cpp
const lsn_t dpa_lsn = log_buffer_dirty_pages_added_up_to_lsn(log);
```

这里的 `dpa_lsn` 可以理解为一种登记完成边界：在它之前的相关脏页登记已经得到保证。

随后函数读取：

```cpp
lsn_t lwm_lsn = buf_pool_get_oldest_modification_lwm();
```

这里从 Buffer Pool 获取待刷页面较早修改位置的低水位信息。

二者结合，才能避免在并发条件下过早宣布一段日志“已经没有未落盘页面需要它”。

对应的安全收缩逻辑是：

**真实源码节选：**

```cpp
if (lwm_lsn == 0) {
  lwm_lsn = dpa_lsn;
} else {
  lwm_lsn = std::min(lwm_lsn, dpa_lsn);
}
```

这几行与我们的实验形成了很好的对应。

教学模型里，我们只记得最早脏页的 LSN。真实源码还需要考虑“某些页面可能正在登记但还看不到”的并发窗口，所以额外受 `dpa_lsn` 约束。

但是，知道脏页已经刷盘也还不够。

Checkpoint 位置不能超过已经可靠持久化的 Redo 位置。函数中还有：

**真实源码节选：**

```cpp
const lsn_t flushed_lsn = log.flushed_to_disk_lsn.load();

lsn_t lsn = std::min(lwm_lsn, flushed_lsn);
```

因此，Checkpoint 的推进同时受**脏页的持久化进度**与**Redo 自身的持久化进度**限制。

这几行并非整个 Checkpoint 的全部正确性条件。源码还处理日志块边界、数据字典进度及其他内部约束。但对我们当前的问题来说，这已经解释了最重要的设计动作：

**不能因为某一个页面已经刷完，或者日志已经写到很后面，就盲目丢弃前面的 Redo。**

## 9. Checkpoint 函数自己会刷完整个 Buffer Pool 吗？

前面我们已经知道，Page Cleaner 会批量刷页。

接下来很容易产生另一个误解：“既然 Checkpoint 需要数据页落盘，那 `log_checkpoint()` 一定会先把所有脏页写完。”

真实情况并不是这样。

InnoDB 使用的是 **fuzzy checkpointing**：在系统持续运行期间，允许脏页分批写回，而不是每次 Checkpoint 都冻结工作、把整个 Buffer Pool 一次性刷干净。

这一点在源码中就能看到。

同样打开 [`log0chkp.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/log/log0chkp.cc)，找到 `log_checkpoint()`。

它的注释特别强调，该函数本身不是一个“将所有脏块刷完”的函数；它主要根据已经取得的安全进度写入 Checkpoint 信息。

**真实源码节选（非连续行，省略其他操作）：**

```cpp
const lsn_t checkpoint_lsn = log_determine_checkpoint_lsn(log);
```

随后可以看到：

```cpp
buf_flush_fsync();
```

这里要小心：`buf_flush_fsync()` 负责确保此前相关数据文件写入达到需要的持久化条件，**不能把它理解为“遍历全部脏页并全部写盘”**。

再往后：

```cpp
const dberr_t err = log_files_next_checkpoint(log, checkpoint_lsn);
```

它最终会写入相应的 Checkpoint 元信息。

现在两个执行过程的职责已经可以分开：

```text
                 持续运行中的购买请求
                          |
                          v
                  页面不断变脏、产生 Redo
                          |
              +-----------+-----------+
              |                       |
              v                       v
       Page Cleaner                 Checkpointer
       按条件刷脏页                 判断安全进度
              |                       |
              v                       v
       数据文件逐步更新         写入新的 Checkpoint
              |                       |
              +-----------+-----------+
                          |
                          v
                  旧 Redo 可以逐步复用
```

这是职责关系图，不代表二者只通过图中的箭头通信，也不代表每次刷一个页面后都立即写一次 Checkpoint。

### 9.1 Checkpoint 元信息必须真的写到可靠位置

继续进入 `log_files_next_checkpoint()`。

函数在完成相关写入后，有以下两处关键动作。

**真实源码节选，省略中间操作：**

```cpp
next_file_handle.fsync();
```

稍后：

```cpp
log.last_checkpoint_lsn.store(next_checkpoint_lsn);
```

前者对应 Checkpoint 元信息的持久化，后者更新运行时记录的最新 Checkpoint 位置。

为什么顺序重要？

如果程序只是把 `last_checkpoint_lsn` 在内存中修改成新值，却没有让恢复时可读的 Checkpoint 信息可靠落盘，那么异常重启时就不能把那个内存值当成真实的恢复边界。

这里终于可以精确地区分三件事：

1. 某个页面**已经完成修改**。
2. 这个页面的修改**已经安全持久化到数据文件**。
3. 系统**已经把相应的恢复边界写成可靠的 Checkpoint 信息**。

三者不是同一时刻发生的动作。

## 10. 刷脏页之前，还需要满足上一章的顺序约束

我们目前知道怎样选脏页、怎样推进 Checkpoint。

但这里还有一个不能漏掉的安全条件。

假设数据页上已经包含某次更新，而对应的 Redo 还只在内存里。如果先把这个数据页写到磁盘，Redo 却没有可靠保存，那么故障后可能出现难以正确恢复的状态。

上一章我们已经学过一个基本要求：**相关 Redo 必须先达到必要的持久化位置，再允许对应的修改页写回。** 这是 Write-Ahead Logging 的关键约束。

不用只记定义。我们去源码验证。

打开 [`buf0flu.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/buf/buf0flu.cc)，在页面写入准备逻辑中可以找到：

**真实源码节选（省略统计、注释等）：**

```cpp
const lsn_t flush_to_lsn = bpage->get_newest_lsn();

if (log_sys->flushed_to_disk_lsn.load() < flush_to_lsn) {
  Wait_stats wait_stats;
  wait_stats = log_write_up_to(*log_sys, flush_to_lsn, true);
```

这段代码所做的判断恰好回答了上面的疑问。

`get_newest_lsn()` 表示当前准备写回的页面内容关联的较新修改位置。如果 Redo 尚未持久化到这个位置，就等待 `log_write_up_to()` 完成相应要求。

然后才继续进行页面写入准备和实际 I/O。

所以真正的顺序不是：

```text
随便写数据页 → 随便写 Redo → 随便推进 Checkpoint
```

而是要同时保持两种关系：

```text
页面修改的 Redo 先达到要求的持久化位置
                         |
                         v
对应数据页才可以安全地写回
                         |
                         v
这些已完成的持久化进度，才允许影响 Checkpoint
```

现在，我们终于把上一章的 Redo 与本章的脏页刷盘真正连接在了同一条运行链上。

---

## 11. 为什么写入持续增长时，Checkpoint 可能跟不上？

假设商品系统流量继续增大，每秒都有大量商品发生库存修改。

产生 Redo 的速度和后台刷页速度并不总是相同。

如果修改不断产生、刷页相对较慢，**当前日志位置**就会离**最新 Checkpoint**越来越远。

它们之间的距离通常称为 *checkpoint age*，可以把它直观理解为：从最近持久化 Checkpoint 到当前 Redo 位置，仍被恢复和日志空间管理约束的跨度。

这一指标增长并不自动表示出错；它说明系统正积累更多需要协调的日志进度。Redo 空间存在容量限制，因此数据库不能让这个差距无限扩大。

真实 InnoDB 会根据运行状态和日志空间压力调整刷页行为。`log_checkpointer()` 既会考虑推进 Checkpoint，也会考虑请求更积极的脏页刷盘；Page Cleaner 则会根据建议和限制执行批量刷页。

相关函数仍在我们已经追踪过的源码位置中：

- [`log0chkp.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/log/log0chkp.cc)：`log_checkpointer()`、`log_consider_sync_flush()`、`log_consider_checkpoint()`。
- [`buf0flu.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/buf/buf0flu.cc)：`Adaptive_flush::page_recommendation()`、`pc_request()`、`pc_flush_slot()`。

从因果上看，我们需要这种协调并不神秘：

- 新购买持续产生 Redo，会占用有限日志空间。
- 刷脏页可以让部分旧修改真正体现在磁盘数据文件中。
- 这些进度满足安全条件后，Checkpoint 才能前移。
- Checkpoint 前移之后，更旧的 Redo 空间才可能被逻辑回收、复用。

**这里的“回收”不是每次 Checkpoint 都把旧日志文件当场物理删除。** MySQL 8.4 的 Redo 文件由专门的空间管理机制维护；更准确地说，Checkpoint 让先前必须保留的日志区间逐步变得可以被复用。

至此，我们已经从“如何恢复一次崩溃”推进到了“如何让有日志的数据库持续运行”。

## 12. 在真实 MySQL 中观察日志与脏页进度

前面的 Python 实验已经实际运行。现在我们也给出 MySQL 8.4 的外部观察办法，但不把未执行的结果写成真实输出。

先在**专用测试库**中准备商品数据（沿用系列中已有的 `mysql_source_lab` 数据库）：

```sql
USE mysql_source_lab;

UPDATE products
SET stock = 100
WHERE id = 42;

SELECT id, stock
FROM products
WHERE id = 42;
```

随后在一个终端使用 MySQL 命令行客户端，查看 InnoDB 状态：

```sql
SHOW ENGINE INNODB STATUS\G
```

重点寻找输出中的 `LOG` 部分，它可能包含这些字段：

```text
Log sequence number          ...
Log flushed up to            ...
Pages flushed up to          ...
Last checkpoint at           ...
```

再看 `BUFFER POOL AND MEMORY` 部分：

```text
Modified db pages            ...
Pages read ... written ...
```

这些名字可以帮助我们区分：

- **Log sequence number**：当前 Redo 逻辑位置附近的进度。
- **Log flushed up to**：Redo 已持久化到的进度。
- **Pages flushed up to**：与脏页刷盘协调的进度。
- **Last checkpoint at**：最近已记录的 Checkpoint 位置。
- **Modified db pages**：当时 Buffer Pool 中的脏页数量。

不同系统负载下这些数值会不断变化，不能预期每次查询都得到固定差值，也不能认为两个观察时刻之间所有变化都来自商品 42：整台实例上的其他修改也会参与统计。

### 12.1 制造少量更新，再观察状态

先记录一次 `SHOW ENGINE INNODB STATUS\G`，然后执行一组库存变化：

```sql
SET SESSION autocommit = 1;

UPDATE products
SET stock = stock - 1
WHERE id = 42 AND stock > 0;

UPDATE products
SET stock = stock - 1
WHERE id = 42 AND stock > 0;

UPDATE products
SET stock = stock - 1
WHERE id = 42 AND stock > 0;

SELECT stock FROM products WHERE id = 42;
```

如果开始时库存是 `100`、没有其他会话修改这个商品，预期查询为 `97`。

之后再次执行：

```sql
SHOW ENGINE INNODB STATUS\G
```

观察 Redo 位置、Checkpoint 位置以及脏页数量是否发生变化。

需要特别注意：**三条 UPDATE 很可能不足以让你直接观察到明显的 Checkpoint 差距**。后台刷页与 Checkpoint 会自行推进，演示实例也可能有其他活动；不能把这种输出当作稳定的性能基准。

本系列不建议为了“看出差异”就随意关闭刷盘、强行压低 Redo 容量或在生产环境制造高写入负载。

如果只想观察相关配置，可以安全地查询：

```sql
SHOW VARIABLES LIKE 'innodb_redo_log_capacity';
SHOW VARIABLES LIKE 'innodb_adaptive_flushing';
SHOW VARIABLES LIKE 'innodb_io_capacity';
```

这些参数涉及 Redo 容量、刷页策略及后台 I/O 能力，但本章的目标是理解机制，不是直接给出调优配方。

**验证边界：** MySQL SQL 实验是提供给读者的可复现步骤，本文没有真实启动 MySQL 8.4 实例运行它们。源码路径已按指定 Git Tag 核对；第 7 节 Python 模型则已经实际执行并通过断言。

---

## 13. 把这次库存扣减重新走一遍

现在，我们可以回到这条已经用了十二章的 SQL：

```sql
UPDATE products
SET stock = stock - 1
WHERE id = 42 AND stock > 0;
```

我们已经知道它可以通过索引找到目标记录，在 Buffer Pool 中修改所在数据页，产生所需的 Redo，并按事务持久化规则完成提交。

但提交并不是数据页生命周期的终点。

```text
用户购买商品 42
       |
       v
InnoDB 在缓存中的页面上完成相应修改
       |
       +----> 产生并按要求持久化 Redo
       |
       v
页面保持脏状态（可与磁盘旧页不同）
       |
       v
Page Cleaner 等执行过程按条件刷页
       |
       v
已安全写入的数据页逐渐追上缓存状态
       |
       v
Checkpointer 结合脏页与 Redo 进度确定安全位置
       |
       v
Checkpoint 持久化，较旧 Redo 的空间可逐步复用
```

这张图不是单线程顺序执行图。刷页和 Checkpoint 通常由后台过程推进，并可与新的 SQL 修改同时发生；它表示的是**持久化与恢复边界之间的依赖关系**。

用一句话总结本章的结果：

**Redo 让数据页不必在每次事务提交时立即落盘；刷脏页让磁盘数据逐步跟上修改；Checkpoint 则把这些已持久化的进度转换成安全的恢复起点，使旧 Redo 不必无限保留。**

我们还可以明确指出这个机制的边界：

- 刷脏页不代表某个用户事务刚刚提交；后台可以写出包含多个时刻变化的数据页。
- Checkpoint 不代表所有脏页都被刷完；InnoDB 支持 fuzzy checkpointing。
- 日志已经持久化不代表数据页已经持久化。
- 商品 42 的页面刷盘了，不代表另一个尚未落盘的商品页面对应的 Redo 可以丢弃。
- 真实 Checkpoint LSN 的计算还包含并发登记、Redo 刷盘位置、恢复兼容性等约束，我们的 Python 模型只是为了验证最关键的必要条件。

## 14. 下一章：一次提交怎样传播到另一台 MySQL？

到目前为止，我们都只研究了一台 MySQL 实例。

这台实例已经能够定位商品 42、正确扣减库存，在并发购买时协调访问，并通过 Undo、Redo、刷脏页与 Checkpoint 支持相应的事务与恢复要求。

但电商系统还会提出新的需求：除了主库，我们希望另一台 MySQL 也知道商品 42 的库存已经变化。

问题来了：

**Redo 已经记录了 InnoDB 页面修改，为什么不直接把这些日志发给另一台 MySQL？**

如果事务提交过程同时涉及 InnoDB 内部恢复信息和可供其他实例读取的变更日志，两者又怎样避免在崩溃时出现相互矛盾的结果？

下一章，我们会从这个具体要求开始，引入 Binlog，并沿事务提交路径研究两种日志的协调。

**下一章：第 13 章《从本地提交到 Binlog——为什么需要协调两种日志？》**

---

## 本章源码与资料

**固定版本：MySQL 8.4.0 / `mysql-8.4.0`。** 以下文件链接均指向指定 Tag，并非随版本变化的 `main` 分支。

| 核对点 | 固定版本源码与函数 |
| --- | --- |
| 脏页如何加入待刷集合 | [`storage/innobase/buf/buf0flu.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/buf/buf0flu.cc)，`buf_flush_insert_into_flush_list()` |
| 如何从 Flush List 挑选批量刷盘页面 | 同文件，`buf_do_flush_list_batch()`、`pc_flush_slot()` |
| 数据页写回前如何检查 Redo 持久化 | 同文件，页面写入准备逻辑中的 `log_write_up_to()` |
| 如何计算可推进的 Checkpoint LSN | [`storage/innobase/log/log0chkp.cc`](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/storage/innobase/log/log0chkp.cc)，`log_compute_available_for_checkpoint_lsn()` |
| 谁负责检查和写入 Checkpoint | 同文件，`log_checkpointer()`、`log_checkpoint()`、`log_files_next_checkpoint()` |
| 为什么允许分批刷页 | [MySQL 8.4 官方手册：InnoDB Checkpoints](https://dev.mysql.com/doc/refman/8.4/en/innodb-checkpoints.html) |
| Redo 容量与回收 | [MySQL 8.4 官方手册：Redo Log](https://dev.mysql.com/doc/refman/8.4/en/innodb-redo-log.html) |
| 观察日志与脏页状态 | [MySQL 8.4 官方手册：InnoDB Standard Monitor](https://dev.mysql.com/doc/refman/8.4/en/innodb-standard-monitor.html) |
| 刷盘相关配置 | [MySQL 8.4 官方手册：InnoDB System Variables](https://dev.mysql.com/doc/refman/8.4/en/innodb-parameters.html) |

**实验声明：** 第 7 节 Python 代码已经运行并通过断言；程序中的 LSN 是教学编号，Redo 是逻辑状态赋值模型，Checkpoint 也是被简化的安全边界。它没有模拟 MySQL 物理日志、操作系统崩溃、文件落盘保证或真实并发。真实 MySQL 观察步骤尚未执行，文中只给出可复现 SQL 与预期观察重点。
