---
title: "第十九章：消息恢复后为什么可能重复执行？从至少一次处理到业务幂等"
sidebar_label: "19 - 消息重试与业务幂等"
sidebar_position: 20
description: "承接 Redis Streams 未确认消息的接管流程，复现消费者提交积分后、XACK 前崩溃导致的重复加分；沿 Redis 7.2.0 源码确定消息确认的边界，再用数据库唯一约束和事务实现可验证的幂等消费。"
tags:
  - Redis
  - 源码精读
  - Streams
  - 消费者组
  - 幂等性
  - XACK
---

# 第十九章：消息恢复后为什么可能重复执行？从至少一次处理到业务幂等

上一章，我们让 `dashboard-B` 使用 `XAUTOCLAIM` 接管了 `dashboard-A` 留在 PEL 中的未确认消息。

现在，活动 `1001` 的报名事件终于可以在消费者崩溃后继续处理：

```text
Stream：activity:1001:events
    |
    | XREADGROUP
    v
消费者 A 收到报名事件
    |
    X 崩溃，未 XACK
    |
    v
消息保留在 PEL
    |
    | XAUTOCLAIM
    v
消费者 B 接管并重新处理
```

这看起来已经足够可靠。但我们上次只讨论了一种故障：**A 在真正执行业务之前崩溃**。

如果换一个崩溃时刻呢？

假设报名事件的业务含义是：用户 `42` 成功报名活动 `1001`，活动系统要给他增加 `10` 个积分。A 已经把积分写入业务数据库，却在执行 `XACK` 前突然退出。B 无法从 PEL 判断 A 是否完成过加分，只会看到一条还没有确认的消息。

于是 B 接管了消息，又执行了一次加分。

**消息被成功恢复了，但用户获得了 20 个积分，而不是 10 个。**

这是本章唯一的核心问题：**允许消息在故障后重新交付的同时，怎样避免同一个业务事件造成重复的实际效果？**

本章沿用 Redis **7.2.0** 作为源码基线。源码分析是静态核对；文中的 Redis/Python 实验是可复现步骤及**预期结果**，不宣称已在当前编写环境实测。

## 1. 先看清楚：消息为什么会被处理两次？

活动事件被写入 Redis Stream：

```redis
XADD activity:1001:events * event_id signup:1001:42:v1 user_id 42 points 10
```

其中有两种不同的 ID，需要从一开始就分清：

- Redis 在 `XADD` 时生成的 **Stream ID**，用于标识 Stream 中的某条消息和 PEL 记录。
- 业务提供的 **`event_id`**，表示“用户 42 的这一次报名成功事件”。后续即使因为生产者重试而重新 `XADD`、生成另一条 Stream ID，业务上仍可能是同一个事件。

设这条消息的 Stream ID 为 `1700000000000-0`（仅用于讲解，真实实验由 Redis 生成）。

消费者 A 读取：

```redis
XREADGROUP GROUP points points-A COUNT 1 STREAMS activity:1001:events >
```

Redis 将消息交给 A，并把它记录到该消费者组的 PEL。接着 A 在数据库里执行了加分：

```sql
UPDATE account_points
SET points = points + 10
WHERE user_id = '42';
```

数据库提交成功后，A 本应执行：

```redis
XACK activity:1001:events points 1700000000000-0
```

但 A 恰好在这两步之间崩溃了：

```text
Redis                        业务数据库
  |                               |
  | XREADGROUP                    |
  |------> A                      |
  |         |                     |
  |         | UPDATE points + 10  |
  |         +-------------------->|
  |         |                  提交成功
  |         X 崩溃                 |
  |                               |
  | 未收到 XACK                   | 已加 10 分
  | PEL 仍然保留该消息            |
```

B 接管后看到的是 Redis 中**未确认**的事实，而不是业务数据库中**未执行**的事实。

这两个事实不等价。

`XACK` 是 Redis 消费者组内部的确认，业务数据库提交是另一个系统中的操作。二者之间没有天然的共同事务。

我们先不要急着给它起名字，只记住这一点：**Redis 需要在无法确定业务结果时允许重试，而重试就可能碰到已经生效的业务操作。**

## 2. 能不能反过来，先 XACK 再更新积分？

一种直觉是：既然“先更新积分、后 XACK”可能重复加分，那么交换顺序是否就可以解决？

```python
# 概念伪代码：不要这样用于要求可靠处理的业务
redis.xack(stream, group, message_id)
update_points_in_database(user_id, 10)
```

这次我们换一个崩溃位置：

```text
XACK 成功
   |
   X 进程崩溃
   |
数据库尚未加分
```

Redis 已经清除了对应的 PEL 记录，正常的待确认消息恢复流程也就不会再接管这条消息。

**重复加分变成了漏加分。**

现在可以把两种顺序放在一起比较：

| 操作顺序 | 崩溃窗口 | 后果 |
|---|---|---|
| 数据库提交 → XACK | 提交成功，确认前崩溃 | 消息可能被再次处理 |
| XACK → 数据库提交 | 确认成功，提交前崩溃 | 业务操作可能永远没执行 |

所以，单纯改变顺序并不能同时消除重复和遗漏。

如果我们优先保证“未确认的消息可以再次处理”，更合理的顺序仍然是：

```text
读取消息 → 执行业务并提交 → XACK
```

但它要求业务本身能够承受重复交付。

这时才有必要引入一个新概念：**幂等性（Idempotency）**。

## 3. 我们真正缺少的是业务幂等性

先不要把幂等性理解成某个 Redis 命令。

对于“用户 42 因报名活动 1001 获得 10 积分”这件事，我们真正期望的结果是：

```text
第一次处理 event_id = signup:1001:42:v1
    → 增加 10 分

第二次处理同一个 event_id
    → 不再增加积分

第三次处理同一个 event_id
    → 仍然不增加积分
```

重复执行同一个业务事件时，最终业务状态与成功执行一次时一致，这就是我们在当前场景需要的幂等效果。

注意，它并不要求每一次处理都返回完全相同的日志、耗时或 Redis PEL 状态；我们关心的是**业务副作用**不能重复发生。

最直接的思路是，额外记住已经处理过的事件：

```python
# 概念伪代码
if event_id not in processed_events:
    add_points(user_id, 10)
    processed_events.add(event_id)
```

但这样又有一个新问题。

如果两个消费者几乎同时检查 `processed_events`，都看到“尚未处理”，然后分别执行加分，不就又重复了吗？

即使把这个集合放进 Redis，用 `SISMEMBER` 检查、再用 `SADD` 标记，也无法自动保证它们与**业务数据库中的加分**构成同一个原子操作。

因此，我们真正需要的是：

> **把“记录事件已处理”和“修改积分余额”放在同一个业务数据库事务中，并让数据库的唯一约束决定哪一次执行有资格生效。**

这次不必让 Redis 负责它无法观察的外部数据库状态。

## 4. 用最小数据库表实现幂等积分

为了让实验不依赖 MySQL 或云服务，我们先用 Python 标准库自带的 SQLite 演示。SQLite 是本地关系型数据库；这里使用它只是为了展示**唯一约束与本地事务**如何保护积分变更。生产系统可按实际数据库的事务和约束语义实现。

准备两张表。

第一张保存用户当前积分：

```sql
CREATE TABLE account_points (
    user_id TEXT PRIMARY KEY,
    points INTEGER NOT NULL DEFAULT 0
);
```

第二张保存已经成功应用的业务事件：

```sql
CREATE TABLE applied_events (
    event_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL
);
```

关键不是两张表的名字，而是第二张表上的：

```sql
event_id TEXT PRIMARY KEY
```

它意味着**同一个事件 ID 最多只能成功插入一次**。

当消费者处理报名事件时，我们让两项操作在同一个数据库事务中完成：

```sql
BEGIN IMMEDIATE;

INSERT OR IGNORE INTO applied_events(event_id, user_id)
VALUES ('signup:1001:42:v1', '42');

-- 只有当上面的 INSERT 确实插入新行时，才执行：
UPDATE account_points
SET points = points + 10
WHERE user_id = '42';

COMMIT;
```

这里的条件“只有 INSERT 真正新增记录才 UPDATE”非常重要；这段 SQL 是业务操作的步骤说明，不是可以不加判断直接粘贴运行的完整存储过程。

第一次处理时，事件 ID 不存在：

```text
插入 applied_events 成功
      ↓
增加 10 积分
      ↓
事务提交
```

第二次处理相同事件时：

```text
同一个 event_id 已存在
      ↓
INSERT OR IGNORE 没有插入新行
      ↓
不再增加积分
      ↓
结束事务
```

最重要的是，如果插入事件记录成功，但后续积分更新失败，整个事务应该回滚；不能留下“已经处理”的标记却没有真正加分。

接下来，我们把它连接到真实的 Redis Streams 消费流程。

## 5. Redis 源码到底知道多少业务状态？

写 Python 消费者之前，先用上一章已经认识的 Redis 源码明确边界。

源码基线：[`redis/redis@7.2.0`](https://github.com/redis/redis/tree/7.2.0)。

### 5.1 XREADGROUP 记录的是“交付”，不是“完成”

我们上一章读过 `src/t_stream.c` 的 `streamReplyWithRange()`。

**真实源码节选，省略相邻分支：**

```c
streamNACK *nack = streamCreateNACK(consumer);
int group_inserted =
    raxTryInsert(group->pel,buf,sizeof(buf),nack,NULL);
int consumer_inserted =
    raxTryInsert(consumer->pel,buf,sizeof(buf),nack,NULL);
```

这段代码位于以消费者组模式交付消息、且没有使用 `NOACK` 的路径中。

`streamCreateNACK()` 创建待确认记录；`group->pel` 和 `consumer->pel` 分别保存组级与消费者级的待确认状态。

它们的职责是让 Redis 回答：**这条消息交付给了谁，是否仍然等待确认？**

它们没有保存“SQLite 的事务是否提交”“用户积分有没有增加”等外部业务事实。

### 5.2 XACK 为什么也无法替我们判断积分是否成功？

继续看 `src/t_stream.c` 中的 `xackCommand()`。

**真实源码节选：**

```c
streamNACK *nack = raxFind(group->pel,buf,sizeof(buf));
if (nack != raxNotFound) {
    raxRemove(group->pel,buf,sizeof(buf),NULL);
    raxRemove(nack->consumer->pel,buf,sizeof(buf),NULL);
    streamFreeNACK(nack);
    acknowledged++;
    server.dirty++;
}
```

调用者是客户端发送的 `XACK` 命令；Redis 使用消息 ID 在组级 PEL 找到待确认记录，再从组级和消费者级 PEL 中删除它。

这段代码**没有访问业务数据库**，也没有校验短信或积分业务是否成功。`XACK` 的可信前提来自**调用它的业务程序**。

因此，“业务提交成功才 XACK”是我们必须实现的应用约定，并非 Redis 能自动替我们保证的检查。

### 5.3 XAUTOCLAIM 为什么可能带来重复执行？

接管时进入 `src/t_stream.c` 的 `xautoclaimCommand()`。

**真实源码节选，省略相邻分支：**

```c
nack->delivery_time = now;
if (!justid)
    nack->delivery_count++;
```

当满足接管条件时，Redis 更新这条待确认记录的交付时间；普通接管还会增加交付次数，接着维护消费者归属并将消息返回给新的消费者。

它只能说明“现在由新的消费者承担处理责任”，**不能证明旧消费者没有提交过数据库事务**。

所以从源码层面看，重复执行不是一条额外隐藏的 Redis 特性，而是两个系统之间必然存在的确认窗口：

```text
Redis：PEL / XACK         业务数据库：积分事务
        |                          |
        +--- 无共同原子提交边界 ---+
```

Redis 的命令原子性解决不了跨两个存储系统的事务提交问题。我们必须在正确的位置约束业务副作用。

## 6. 可运行实验：提交数据库后，故意在 XACK 前崩溃

这个实验分两轮：

- 第一轮**不做幂等保护**，观察消息接管后积分重复增加。
- 第二轮加入 **`event_id` 唯一约束 + 本地数据库事务**，观察同一事件再次交付时积分保持不变。

为保证实验可控，两轮分别使用独立的 Stream 和 SQLite 文件。

### 6.1 准备环境

启动 Redis 7.2.0：

```bash
docker run -d \
  --name redis-idempotency-lab \
  -p 127.0.0.1:6379:6379 \
  redis:7.2.0 \
  --save "" \
  --appendonly no
```

如果本地 `6379` 端口已被占用，先停止冲突的实验容器，或调整容器端口与下方脚本连接设置。

安装 Python 客户端：

```bash
python -m pip install "redis==5.0.1"
```

SQLite 使用 Python 标准库 `sqlite3`，不需要额外安装。

> 这里关闭 Redis 持久化是为了聚焦“消费者崩溃、数据库已经提交”的场景。我们**不会**重启 Redis，也不研究 Redis 自身崩溃后的消息可靠性——相关边界已在持久化与复制章节单独讨论。

### 6.2 编写完整消费者脚本

在空目录创建 `stream_idempotency_demo.py`：

```python
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import redis

GROUP = "activity-points"
EVENT_ID = "signup:1001:42:v1"
USER_ID = "42"
POINTS = 10


def keys(mode):
    return f"lab:activity:events:{mode}", Path(f"points_{mode}.sqlite3")


def redis_client():
    return redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)


def init(mode):
    stream, db_path = keys(mode)
    r = redis_client()

    # 仅删除本实验的 Key，不清理其他 Redis 数据
    r.delete(stream)
    r.xgroup_create(stream, GROUP, id="0", mkstream=True)
    r.xadd(stream, {
        "event_id": EVENT_ID,
        "user_id": USER_ID,
        "points": str(POINTS),
    })

    db_path.unlink(missing_ok=True)
    with closing(sqlite3.connect(db_path)) as db:
        with db:
            db.execute("""
                CREATE TABLE account_points (
                    user_id TEXT PRIMARY KEY,
                    points INTEGER NOT NULL
                )
            """)
            db.execute("""
                CREATE TABLE applied_events (
                    event_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL
                )
            """)
            db.execute(
                "INSERT INTO account_points(user_id, points) VALUES (?, 0)",
                (USER_ID,),
            )
    print(f"[{mode}] 初始化完成：1 条事件，积分 0，PEL 为空")


def apply_event(mode, fields):
    _, db_path = keys(mode)
    event_id = fields["event_id"]
    user_id = fields["user_id"]
    points = int(fields["points"])

    with closing(sqlite3.connect(db_path, timeout=5)) as db:
        # with db 保证两个数据库操作同提交或同回滚
        with db:
            if mode == "safe":
                cursor = db.execute(
                    "INSERT OR IGNORE INTO applied_events(event_id, user_id) "
                    "VALUES (?, ?)",
                    (event_id, user_id),
                )
                if cursor.rowcount == 0:
                    return "已处理过，跳过加分"

            cursor = db.execute(
                "UPDATE account_points SET points = points + ? "
                "WHERE user_id = ?",
                (points, user_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("用户不存在，数据库事务将回滚")

    return f"新增 {points} 积分并提交"


def crash_after_commit(mode):
    stream, _ = keys(mode)
    r = redis_client()
    result = r.xreadgroup(
        groupname=GROUP,
        consumername="points-A",
        streams={stream: ">"},
        count=1,
    )
    if not result:
        raise RuntimeError("没有可供 A 读取的新消息")

    message_id, fields = result[0][1][0]
    print("A 收到:", message_id, fields, flush=True)
    print("A 业务处理:", apply_event(mode, fields), flush=True)
    print("A 已提交数据库，现在故意在 XACK 之前退出", flush=True)

    # 模拟真实进程意外退出：故意不 XACK，也不做后续清理
    os._exit(23)


def recover(mode):
    stream, _ = keys(mode)
    r = redis_client()

    # 仅实验使用 0ms：A 已明确终止，无需等待真实故障超时
    result = r.xautoclaim(
        name=stream,
        groupname=GROUP,
        consumername="points-B",
        min_idle_time=0,
        start_id="0-0",
        count=10,
    )
    messages = result[1]
    if len(messages) != 1:
        raise RuntimeError(f"预期接管 1 条消息，实际得到 {len(messages)} 条")

    message_id, fields = messages[0]
    print("B 接管:", message_id, fields)
    print("B 业务处理:", apply_event(mode, fields))

    acked = r.xack(stream, GROUP, message_id)
    print("B XACK 返回:", acked)
    assert acked == 1


def status(mode):
    stream, db_path = keys(mode)
    with closing(sqlite3.connect(db_path)) as db:
        points = db.execute(
            "SELECT points FROM account_points WHERE user_id = ?",
            (USER_ID,),
        ).fetchone()[0]
        applied = db.execute(
            "SELECT COUNT(*) FROM applied_events"
        ).fetchone()[0]

    pending = redis_client().xpending(stream, GROUP)["pending"]
    print(f"[{mode}] 当前积分={points}，处理记录={applied}，PEL待确认={pending}")


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in ("naive", "safe"):
        raise SystemExit(
            "用法：python stream_idempotency_demo.py <naive|safe> "
            "<init|crash|recover|status>"
        )

    mode, action = sys.argv[1:]
    actions = {
        "init": init,
        "crash": crash_after_commit,
        "recover": recover,
        "status": status,
    }
    if action not in actions:
        raise SystemExit("未知操作：" + action)
    actions[action](mode)
```

这段代码的关键是 `apply_event()`。

在 `naive` 模式中，它直接在数据库内执行：

```sql
UPDATE account_points SET points = points + 10 WHERE user_id = '42';
```

在 `safe` 模式中，它先尝试把 `event_id` 插入 `applied_events`。只有插入成功，才在**同一个数据库事务**内更新积分。已存在的 `event_id` 会被跳过。

注意：数据库中的 `event_id` 是业务事件 ID，而不是 Redis 消息的 Stream ID。这使得“生产者把同一业务事件写成两条不同 Stream 消息”的情况下，去重规则仍然有效，前提是生产者正确复用同一业务事件 ID。

### 6.3 第一轮：不幂等，观察重复加分

先初始化：

```bash
python stream_idempotency_demo.py naive init
python stream_idempotency_demo.py naive status
```

预期状态：

```text
[naive] 当前积分=0，处理记录=0，PEL待确认=0
```

再运行消费者 A：

```bash
python stream_idempotency_demo.py naive crash
```

它会读到消息、提交 `+10` 积分，然后使用 `os._exit(23)` **故意结束进程且不发送 XACK**。因此这条命令以退出码 `23` 结束是实验设计的一部分，不表示 Python 代码意外出错。

接下来查询状态：

```bash
python stream_idempotency_demo.py naive status
```

预期为：

```text
[naive] 当前积分=10，处理记录=0，PEL待确认=1
```

这里最值得注意的是最后两个数字：

- SQLite 已经提交了 `+10`。
- Redis PEL 仍有一条未确认消息。

现在让 B 接管：

```bash
python stream_idempotency_demo.py naive recover
python stream_idempotency_demo.py naive status
```

预期：

```text
B 业务处理: 新增 10 积分并提交
B XACK 返回: 1
[naive] 当前积分=20，处理记录=0，PEL待确认=0
```

同一个业务事件只发生了一次，但积分增加了两次。

这不是 Redis “重复创建”了两条 Stream 消息；**同一条消息因为缺少 XACK 被重新交付，业务程序重复产生了副作用。**

### 6.4 第二轮：加上唯一约束与数据库事务

重新以 `safe` 模式初始化独立实验：

```bash
python stream_idempotency_demo.py safe init
python stream_idempotency_demo.py safe crash
```

A 仍然会提交 `+10` 后在 XACK 前退出。

查询：

```bash
python stream_idempotency_demo.py safe status
```

预期：

```text
[safe] 当前积分=10，处理记录=1，PEL待确认=1
```

现在让 B 接管：

```bash
python stream_idempotency_demo.py safe recover
python stream_idempotency_demo.py safe status
```

预期结果：

```text
B 业务处理: 已处理过，跳过加分
B XACK 返回: 1
[safe] 当前积分=10，处理记录=1，PEL待确认=0
```

这一次 Redis 的行为没有改变：消息依然从 A 的待确认状态转交给 B，B 仍然收到了该消息，也仍然需要在处理成功后 `XACK`。

改变的是业务数据库：它通过唯一约束识别同一个业务事件已经生效，因此没有重复增加积分。

此时我们可以准确描述实验成果：

> 对于**这张 SQLite 数据库中的积分更新**，在相同 `event_id` 被多次交付时，我们利用唯一约束与事务实现了一次性应用的效果。

这不是整个分布式系统的“精确一次投递”保证，也不代表 Redis 只交付了一次消息。

### 6.5 再用 Redis 查看消息状态

也可以在两个实验阶段分别执行：

```bash
docker exec redis-idempotency-lab \
  redis-cli XPENDING lab:activity:events:safe activity-points
```

A 崩溃后，待确认数量预期是 `1`；B 接管并 `XACK` 后，预期变为 `0`。

还可以检查 Stream 中的原始消息仍然存在：

```bash
docker exec redis-idempotency-lab \
  redis-cli XRANGE lab:activity:events:safe - +
```

**`XACK` 清除的是 PEL 中的待确认记录，并不会删除 Stream 正文。**

这个区别正好再次验证 Redis 与业务数据库各自承担的职责。

## 7. 为什么不能只在 Redis 里放一个去重标记？

我们已经学过 Set，不难想到：

```redis
SADD activity:1001:processed signup:1001:42:v1
```

`SADD` 对单个 Set 成员具有去重语义。如果返回 `1`，说明这次新增成功；返回 `0`，说明此前已经存在该成员。

但把它和关系型数据库的积分更新连起来，会遇到与 XACK 相似的跨系统窗口：

```text
先 SADD → 再数据库加分：
标记写入成功后崩溃 → 积分没加，但重试被判定为已处理

先数据库加分 → 再 SADD：
积分提交成功后崩溃 → 标记没写，重试仍会重复加分
```

即使把 Redis 中的多个操作通过 Lua 脚本组合执行，也无法让外部 SQLite/MySQL 事务天然参与同一次 Redis 原子执行。

因此，当**积分余额本身保存在业务数据库**中时，最自然的方案就是在那个数据库内完成去重记录和积分修改的原子提交。

如果业务状态全部都存储在同一个 Redis 实例中，设计选择会不同，可以考虑原子命令或脚本；但不能把一种存储边界内的原子性误当成跨系统事务。

## 8. 幂等性的边界：短信等外部副作用怎么办？

对于数据库积分更新，我们有本地事务和唯一约束。

但如果业务操作变成“发送活动报名成功短信”，事情会困难一些。

假设短信提供商已经收到请求并成功发送，消费者还没记录发送结果就崩溃了。恢复时如果再次调用短信接口，用户可能收到两条短信。

反过来，如果我们先在数据库里记录“已发送”，然后再请求短信提供商，崩溃可能导致**根本没有发出短信，却永远不再重试**。

因此，幂等性必须落在**真实副作用发生的边界**上。

如果外部服务支持幂等请求键，可以为同一个事件传入固定的 `event_id`，并依据提供商的保证设计重试。

如果不支持，就需要结合业务状态、服务商回执、对账或人工补偿机制；仅靠 Redis Streams、PEL 和一个本地标记，无法在所有故障窗口下证明短信“严格只发送一次”。

同样，超时接管不意味着旧消费者已经停止。它可能只是处理慢或网络暂时中断。旧消费者和新消费者甚至可能短时间并发执行同一个业务事件。

这也是为什么前面的实验不能只靠“检测 A 已死亡”，而要通过数据库约束保护真正需要保持一次性效果的业务更新。

## 9. 实际消费者还需要处理哪些失败？

现在我们的最小实现已经解决了**同一积分事件重复交付导致重复加分**。

但如果业务数据库持续不可用，消费者每次重试都会失败，Redis 仍然不会自动让业务成功。

比较合理的处理原则是：

1. **数据库事务没有提交成功，就不要 XACK。** 保留待确认状态，让恢复程序稍后重试。
2. **数据库明确确认该事件早已成功处理，也可以 XACK。** 因为再次执行不会产生新副作用，这次交付已经完成了“确认结果”的工作。
3. **区分暂时性错误与永久性错误。** 网络抖动可以延迟重试；无效数据、错误字段等需要记录原因，并根据业务策略隔离处理。
4. **不要无限高速重试同一条坏消息。** 可以设置重试次数、退避间隔、告警或应用级失败事件流；Redis Streams 7.2 不会自动替你建立死信队列。

这里暂时不展开完整的重试调度器。它不是理解业务幂等所必需的第一层机制。

另一个容易忽略的边界是：`XAUTOCLAIM` 可能发现 PEL 中某条消息的 Stream 正文已经被 `XDEL` 或裁剪删除。Redis 7.2 会清理这类悬空待确认记录，并在回复中报告其 ID，但无法重建已经删除的正文。

这正好把我们带到下一章。

## 10. 回看：我们到底在什么位置增加了新机制？

回忆前两章，我们已经得到以下路径：

```text
XREADGROUP 交付活动事件
             |
             v
PEL 记录尚未 XACK 的消息
             |
             | 消费者故障
             v
XAUTOCLAIM 接管消息
             |
             | 旧消费者可能已经提交业务
             v
业务可能重复执行
             |
             | 先 XACK 会漏处理，后 XACK 会有重试窗口
             v
需要业务幂等
             |
             | 在业务数据库中
             v
唯一 event_id + 同一事务内更新积分
             |
             | 业务已成功或确认此前已成功
             v
XACK 清理 PEL
```

这次我们没有要求 Redis 在内部增加一个新的交付机制。

我们真正补上的，是 Redis 无法代替业务判断的那部分语义：**某个报名事件是否已经对积分余额产生过效果。**

Redis 负责保存事件、分发事件、记录未确认状态和支持接管；数据库事务与唯一约束负责保证积分更新不会因为同一业务事件的重复交付而重复生效。

它们共同组成一个更可靠的业务处理流程，但并不能合并成一个天然的跨系统“精确一次事务”。

## 11. 下一章：消息可以恢复，为什么还要考虑删除？

到现在，我们已经能够让某个报名事件被交付、接管、幂等处理和确认。

但 `XACK` 并不会自动删除 Stream 中的消息正文。

如果活动每天产生大量报名事件，而我们永远不清理消息历史，那么 Stream 就会不断增长。

最简单的想法是：消费者处理完就删除消息。

可是，如果同一条事件还有其他消费者组尚未处理呢？如果消息虽被交付，但仍然留在 PEL 中呢？删除正文之后，故障恢复还能拿到什么？

于是，又出现了一个新的问题：

> **怎样在控制 Stream 内存占用的同时，不破坏我们刚刚建立的消息恢复机制？**

**下一章：《消息不断积累怎么办？从 Stream 保留策略到 XTRIM》**

---

## 源码与文档

本文以 **Redis 7.2.0** 为固定基线，不使用后续 Redis 版本新增的消息确认/裁剪命令。

- [`src/t_stream.c` — `streamReplyWithRange()`](https://github.com/redis/redis/blob/7.2.0/src/t_stream.c#L1564-L1672)：消费者组交付消息时更新组级与消费者级 PEL。
- [`src/t_stream.c` — `xackCommand()`](https://github.com/redis/redis/blob/7.2.0/src/t_stream.c#L2623-L2674)：删除待确认记录而不删除消息正文。
- [`src/t_stream.c` — `xautoclaimCommand()`](https://github.com/redis/redis/blob/7.2.0/src/t_stream.c#L3098-L3273)：接管未确认消息与维护交付次数。
- [Redis XREADGROUP 官方说明](https://redis.io/docs/latest/commands/xreadgroup/)：新消息、PEL 与显式确认。
- [Redis XACK 官方说明](https://redis.io/docs/latest/commands/xack/)：消息确认的边界。
- [Redis XAUTOCLAIM 官方说明](https://redis.io/docs/latest/commands/xautoclaim/)：接管的超时判断、游标和回复结构。

> **验证说明**：文中的 Redis 源码片段来自 Redis 7.2.0，可省略相邻无关分支但不改变核心语义；Python 演示为应用层实现而非 Redis 内部源码。本文没有在当前编写环境运行 Redis 服务，命令输出为推导的预期行为。实际生产系统应使用真实数据库事务、合理的消息重试阈值、日志和监控进一步验证。
