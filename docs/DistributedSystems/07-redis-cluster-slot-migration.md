---
title: "分布式系统 07：扩容时，数据还在旧节点怎么办？——Redis Cluster 在线迁移与 ASK/MOVED"
sidebar_label: "七、扩容时数据还在旧节点怎么办"
sidebar_position: 8
description: "延续 post:42:views 的 Hash Slot 10015，从直接切换 Slot 所有权的失败开始，逐步推导 MIGRATING、IMPORTING、ASK、ASKING 和 MOVED，并验证 Redis 7.2.4 的真实源码路径。"
slug: /distributed-systems/07-redis-cluster-slot-migration
tags: [distributed-systems, redis, cluster, resharding, source-code]
---

# 扩容时，数据还在旧节点怎么办？——Redis Cluster 在线迁移与 ASK/MOVED

上一篇我们终于不再要求每台机器保存全部文章访问次数。

通过 Redis Cluster 的 **Hash Slot**，一个 Key 先被映射到固定编号的槽，再由槽的负责节点处理命令。我们的贯穿案例 `post:42:views` 所在的 Slot 是 `10015`，按照上篇的初始分配规则，它由节点 **B** 负责：

```text
A：Slot 0–8191
B：Slot 8192–16383

post:42:views → Slot 10015 → B
```

现在网站继续增长。B 的内存也快装满了，我们准备加入第三台主节点 **C**，将一部分 Slot 从 B 移给 C。

听起来非常简单：把 `Slot 10015 → B` 改成 `Slot 10015 → C` 不就行了？

**这正是本章要亲手证明不能直接做的事情。**

我们依然只追踪一个 Slot，甚至先只追踪一个 Key。我们的目标不是一次介绍 Redis Cluster 的全部命令，而是解释：**当路由关系改变时，旧数据如何不丢，正在到来的请求如何找到它？**

## 一、只改 Slot 的归属，为什么马上出问题？

假设当前真实数据存放在 B：

```text
B：
  post:42:views = 101

C：
  （空）
```

我们急着扩容，于是先改路由表：

```text
Slot 10015：B → C
```

现在客户端执行：

```redis
GET post:42:views
```

它根据新的路由表访问 C，但 C 上根本没有这个 Key。

于是得到了一个很糟糕的现象：**数据没有被删除，却因为路由提前变化而暂时无法访问。**

既然如此，直觉方案是先把数据从 B 复制到 C，全部复制完再切换归属。

假设只有一个 Key，这显然很容易。但在真实 Slot 中，可能有成千上万个 Key；复制期间，客户端还在持续更新它们。比如复制刚刚把 `post:42:views = 101` 放到 C，B 又收到了 `SET post:42:views 102`。

等我们切换到 C，读到的也许还是 `101`。

如果选择暂停整个 Slot 的所有访问，直到完整复制结束，也可以避开部分竞态，但代价可能是很长的不可用窗口。

所以真正的新需求不是“改路由表”，而是：

> **数据分批移动时，旧节点和新节点能够同时存放同一个 Slot 的不同 Key；客户端仍然可以准确找到每个 Key。**

为了解决这个问题，我们先给迁移过程建立一个非常小的状态模型。

## 二、先让 Slot 有一个“正在搬家”的阶段

在正式迁移之前，Slot 10015 完全属于 B。客户端无论访问它的哪个 Key，都应该由 B 处理。

现在我们准备把 Slot 10015 从 B 移到 C。先约定两个状态：

```text
B：我仍然是 Slot 10015 的所有者，但正在向 C 迁出（MIGRATING）
C：我还不是 Slot 10015 的所有者，但准备从 B 迁入（IMPORTING）
```

为什么要把“当前所有者”和“迁移目的地”分开？

因为在迁移完成前，不能让所有客户端都以为 C 已经接管了整个 Slot。此时数据可能是这样的：

```text
B（仍是 Slot 所有者）       C（正在导入）
post:6281:views = 8        post:42:views = 101
```

这两个 Key 都属于 Slot `10015`。在本文中，它们的 Redis CRC16 结果都经过了实际计算确认。

现在如果访问 `post:6281:views`，需要去 B；如果访问 `post:42:views`，却需要去 C。

**迁移期间，同一个 Slot 的 Key 暂时分散在两个节点。**

于是我们不能再简单地说“Slot 10015 一定在 C”。我们必须让仍负责该 Slot 的 B，在收到请求后进一步判断具体 Key 是否还在本地。

如果在 B，就正常处理；如果已经不在 B，就临时引导客户端去 C。

这个“临时引导”，就是 **ASK**。

## 三、为什么需要 ASK，而不是直接使用 MOVED？

我们先不看源码，用一个清晰的请求过程来区分两种响应。

### 3.1 迁移过程中，访问仍留在 B 的 Key

```text
客户端 → B：GET post:6281:views
B：本地存在，返回 8
```

没有发生特殊行为。

### 3.2 迁移过程中，访问已经搬到 C 的 Key

```text
客户端 → B：GET post:42:views
B：本地已经没有了，回复 ASK 10015 C:7002

客户端 → C：ASKING
C：OK

客户端 → C：GET post:42:views
C：101
```

这里的 `C:7002` 是示意地址和端口，不是实验中启动的真实服务。

为什么不是直接告诉客户端 `MOVED 10015 C:7002`？

因为 **MOVED 的含义更接近“这个 Slot 的归属已经改变”**。客户端收到 MOVED 后，可以更新自己缓存的 Slot 路由表，以后都直接去新节点。

但迁移尚未结束：`post:6281:views` 还在 B。如果客户端因为一次请求就永久改去 C，下一次访问仍留在 B 的 Key 就会出错。

所以 ASK 只表示：

**这一次请求请临时去 C，先不要永久改变 Slot 10015 的归属认知。**

由此，两个重定向的区别就不是死记协议单词，而是一个十分具体的设计约束：

| 响应 | 当前含义 | 客户端行为 |
| --- | --- | --- |
| `ASK 10015 C:7002` | 迁移期间，当前 Key 请临时去 C | 在**同一目标连接**上先发 `ASKING`，再重试这一次请求；不因 ASK 永久修改 Slot 路由 |
| `MOVED 10015 C:7002` | Slot 的负责节点不是这里 | 访问正确的节点，并可更新自己的 Slot 路由缓存 |

现在又出现一个小问题：**C 明明还不拥有 Slot 10015，凭什么允许客户端写它？**

如果 C 对所有请求都直接放行，任何误连到 C 的普通客户端都可能绕过仍然有效的 Slot 归属规则。

于是还需要一个最小的例外通行证：`ASKING`。它告诉 C：“我这一次请求是遵循 ASK 重定向过来的。”

C 只对这种请求暂时放行。之后，通行证会失效。

## 四、去真实源码验证：迁移状态究竟保存在哪里？

**固定源码：** Redis `7.2.4`，不是最新 Redis 的全部版本。下面涉及的源码均来自该 Tag；只摘录与当前论证有关的关键语句，省略不影响结论的分支。

先打开 [`src/cluster.c` 的 `clusterCommand()`](https://github.com/redis/redis/blob/7.2.4/src/cluster.c#L5614-L5663)。

当外部管理工具执行下面两条命令时：

```text
发送给 C：CLUSTER SETSLOT 10015 IMPORTING <B-node-id>
发送给 B：CLUSTER SETSLOT 10015 MIGRATING <C-node-id>
```

`clusterCommand()` 在通过对应的所有者、目标节点等检查后，会更新两个状态表。

**真实源码节选：**

```c
server.cluster->migrating_slots_to[slot] = n;
```

以及：

```c
server.cluster->importing_slots_from[slot] = n;
```

这里的 `slot` 来自 `CLUSTER SETSLOT` 的参数；`n` 是根据提供的 Node ID 找到的集群节点。

为什么不直接写 `server.cluster->slots[slot] = C`？

因为 `slots[slot]` 表示当前 Slot 归属；迁移期间这个归属仍然保留为 B。新增的数组只描述过渡状态。

- 在 B 看，`migrating_slots_to[10015] = C`：如果本地缺少指定 Key，可能要提示去 C。
- 在 C 看，`importing_slots_from[10015] = B`：如果请求带有临时授权，可以在这里执行。

**它解决了什么？** 系统现在可以表示“所有权尚未改变，但部分 Key 正在迁移”这一中间阶段。

**它还没解决什么？** 仅仅记录状态，并不会自动搬运任何一个 Key，也不会自动决定用户请求该去哪儿。下面继续追踪请求路径。

## 五、真实源码：是谁决定返回 ASK？

延续上篇的入口，我们打开 [`src/cluster.c` 的 `getNodeByQuery()`](https://github.com/redis/redis/blob/7.2.4/src/cluster.c#L6692-L6887)。

这段函数由 Redis 命令执行前的集群路由检查调用。输入包含当前客户端、命令和参数；输出是能处理请求的节点，以及必要时附带的重定向错误类型。

它先找到 Key 的 Slot，判断是不是自己负责，以及是否处在迁移阶段。

当一个 Slot 正在从本节点迁出，而**当前请求涉及的 Key 在本地都不存在**时，会进入下面的关键分支。

**真实源码节选（省略其他分支）：**

```c
if (migrating_slot && missing_keys) {
    if (existing_keys) {
        if (error_code) *error_code = CLUSTER_REDIR_UNSTABLE;
        return NULL;
    } else {
        if (error_code) *error_code = CLUSTER_REDIR_ASK;
        return server.cluster->migrating_slots_to[slot];
    }
}
```

对于我们目前的单 Key 请求，`missing_keys` 的含义很好理解：B 本地没有这个 Key。

于是 B 设置 `CLUSTER_REDIR_ASK`，并返回 C 的节点信息。

再往下看，`clusterRedirectClient()` 根据重定向类型格式化响应，最终返回类似：

```text
-ASK 10015 C:7002
```

这里的负号表示 Redis RESP 错误响应的形式。它不是一个表示数据写入失败的普通业务错误，而是要求 Cluster 客户端按协议重新发送请求。

**为什么 B 只检查本地 Key 是否存在，而不是查询 C？**

因为在这个迁移模型里，B 已经知道 C 是目标节点；对于已经离开 B 的 Key，转交下一跳即可，不需要同步再访问 C 并等待一次远程检查。正常支持 Redis Cluster 的客户端会处理这个重定向。

还有一个容易忽略的细节：上面 `existing_keys` 分支返回的不是 ASK，而是 `TRYAGAIN`。这涉及一个请求同时操作多个 Key、但部分 Key 已搬走、部分仍在 B 的情况。现在只要记住：**这种请求不能像单 Key 一样安全地简单重定向。** 完整讨论放到下一篇。

## 六、真实源码：C 为什么只接受带 ASKING 的请求？

继续看 [`getNodeByQuery()` 的导入分支](https://github.com/redis/redis/blob/7.2.4/src/cluster.c#L6857-L6869)。

**真实源码节选（只保留单 Key 正常路径相关条件）：**

```c
if (importing_slot &&
    (c->flags & CLIENT_ASKING || cmd_flags & CMD_ASKING))
{
    /* ...其他分支检查... */
    return myself;
}
```

这里 `importing_slot` 表示 C 已经处于导入状态；`CLIENT_ASKING` 表示当前客户端曾发送 `ASKING`，得到了一次临时访问许可。

这个标记是从哪里来的？就在同一个文件的 [`askingCommand()`](https://github.com/redis/redis/blob/7.2.4/src/cluster.c#L6628-L6639)：

```c
c->flags |= CLIENT_ASKING;
addReply(c,shared.ok);
```

`ASKING` 在当前客户端连接的状态上设置标志，并回复 `OK`。

如果没有这个标志，C 还没有成为 Slot 所有者，就会按常规归属将请求 `MOVED` 回 B。这正是防止客户端过早把全部请求永久路由到 C 的关键。

但“临时授权”为什么不会一直有效？

打开 [`src/networking.c` 的 `resetClient()`](https://github.com/redis/redis/blob/7.2.4/src/networking.c#L1903-L1927)。正常执行完接下来的命令时，在不处于事务等特殊情形下，Redis 会清掉标记：

```c
if (!(c->flags & CLIENT_MULTI) && prevcmd != askingCommand)
    c->flags &= ~CLIENT_ASKING;
```

于是我们的正常调用链已经可以完整解释：

```text
客户端向 B 发送 GET post:42:views
  ↓
B 的 getNodeByQuery() 发现 Key 已不在本地
  ↓
B 返回 ASK 10015 C:7002
  ↓
客户端在与 C 的同一连接上执行 ASKING
  ↓
C 的 askingCommand() 设置 CLIENT_ASKING
  ↓
客户端向 C 重发 GET
  ↓
C 的 getNodeByQuery() 临时允许执行
  ↓
执行 GET，返回 101
  ↓
resetClient() 清理 ASKING 状态
```

**这次访问完成以后，Slot 仍然属于 B。**

这正是 ASK 与 MOVED 的本质区别。

## 七、迁移状态会自己把数据搬过去吗？

不会。

我们已经让 B 与 C 能够处理“有些 Key 已经搬走、有些还没搬走”的中间阶段，但真正的数据搬运仍然需要发生。

Redis 7.2.4 的常规 `redis-cli --cluster reshard` 流程，可以从 [`src/redis-cli.c` 的 `clusterManagerMoveSlot()`](https://github.com/redis/redis/blob/7.2.4/src/redis-cli.c#L4875-L4952) 读起。

**真实源码节选：**

```c
success = clusterManagerSetSlot(target, source, slot,
                                "importing", err);
...
success = clusterManagerSetSlot(source, target, slot,
                                "migrating", err);
...
success = clusterManagerMigrateKeysInSlot(source, target, slot, timeout,
                                          pipeline, print_dots, err);
```

请注意函数的调用者：这段代码存在于 **redis-cli 的集群管理工具** 中，而不是普通 `GET`/`SET` 命令执行时自动调用。

它先通知目标 C 进入导入状态，再通知源端 B 进入迁出状态，最后才开始逐批搬迁 Key。正常迁移并不是“增加一台机器后 Redis 自动把所有 Slot 均匀迁移”。

那么 `clusterManagerMigrateKeysInSlot()` 做了什么？

在同一文件中，它会反复向源节点发送：

```text
CLUSTER GETKEYSINSLOT 10015 <batch-count>
```

拿到一批 Key 后，再用 `MIGRATE` 将这些 Key 移向目标节点，直到源节点对这个 Slot 不再返回待搬运的 Key。

如果只看关键路径，可将其理解为：

```text
从 B 取出一批 Slot 10015 的 Key 名称
        ↓
在 B 上调用 MIGRATE，目标为 C
        ↓
迁移成功：这些 Key 从 B 消失，在 C 出现
        ↓
重复，直到 B 的这个 Slot 没有待迁移 Key
```

`MIGRATE` 内部依赖序列化、目标节点恢复以及源节点删除，具体可沿 [`src/cluster.c` 的 `migrateCommand()`](https://github.com/redis/redis/blob/7.2.4/src/cluster.c#L6000-L6624) 和 [MIGRATE 官方文档](https://redis.io/docs/latest/commands/migrate/) 继续查看。本章不展开其网络缓冲与异常重试细节。

需要保留一个真实边界：**MIGRATE 在成功路径上可以完成 Key 的搬迁，但在网络 I/O 失败或超时时，Key 可能同时存在于源与目标两侧。** 不能把“正常路径最终从源删除”误解成所有失败情况下都是严格的单副本原子切换；排障时要检查实际状态。

到这里，所有 Key 已经移到 C，是否终于可以把 Slot 的归属改为 C？

现在可以了，但必须避免在 B 还存有 Key 的情况下提前执行切换。

## 八、真实源码：什么时候才允许正式切换所有权？

再回到 [`clusterCommand()` 对 SETSLOT NODE 的处理](https://github.com/redis/redis/blob/7.2.4/src/cluster.c#L5664-L5694)。

当源 B 准备把 Slot 10015 交给 C 时，Redis 会检查本地是否仍留着这个 Slot 的 Key。

**真实源码节选：**

```c
if (server.cluster->slots[slot] == myself && n != myself) {
    if (countKeysInSlot(slot) != 0) {
        /* ...拒绝切换... */
        return;
    }
}
```

为什么要检查这个条件？

因为我们在开篇已经见过：**如果先改所有权再搬数据，路由会立刻指向一个尚未拥有完整数据的节点。**

因此，正常的 `redis-cli` 迁移流程在完成搬运后，才安排 `CLUSTER SETSLOT ... NODE ...`，并先通知目标 C 自己已经成为所有者，然后通知源 B 和其他已知主节点。

`clusterManagerMoveSlot()` 的源码注释还说明了“先通知目标”的原因：若其他节点先切换路由，而目标还没承认自己是所有者又恰好宕机，可能出现无主 Slot 或重定向循环。这个先后顺序不是装饰性的实现细节。

迁移完成后的状态终于变成：

```text
B：不再拥有 Slot 10015
C：正式拥有 Slot 10015

post:42:views → Slot 10015 → C
```

这时客户端如果仍按旧缓存去 B，B 不再回复 ASK，而是回复：

```text
-MOVED 10015 C:7002
```

客户端可以更新 Slot 路由表，以后将这个 Slot 的请求直接送到 C。

我们终于理解了两种重定向为什么必须同时存在：

**ASK 服务于“数据已部分迁移，但归属尚未改变”；MOVED 服务于“归属已经改变，请更新路由”。**

## 九、动手实验：观察同一个 Key 在三个时刻的路由

本实验不需要启动 Redis。我们用纯 Python 把刚才讨论的正常路径做成可执行模型。

为了与上篇保持一致，仍然固定 `Slot 10015`，并让 B 为原所有者、C 为新节点。

脚本使用 Python 标准库的 `binascii.crc_hqx` 为普通 Key 计算 Redis Cluster 风格的 CRC16 Slot；真实 Redis 还支持 Hash Tag，此处刻意不涉及。

**教学化简：** 下面的 `move_key()` 只是字典转移，没有实现 Redis `MIGRATE` 的网络协议、超时处理、并发写入或故障恢复。`asking=True` 是一次调用参数，不代表真的建立了客户端连接。测试的重点是不同迁移阶段的**路由决策**，不是分布式一致性。

将下面代码保存为 `slot_migration_lab.py`：

```python
"""Pure Python teaching model of Redis Cluster single-slot migration (not Redis)."""
import binascii

SLOT_COUNT = 16384
KEY_MOVED = 'post:42:views'
KEY_PENDING = 'post:6281:views'
KEY_NEW = 'post:46141:views'


def hash_slot(key):
    return binascii.crc_hqx(key.encode('utf-8'), 0) % SLOT_COUNT


class ToyCluster:
    def __init__(self):
        self.nodes = {'B': {KEY_MOVED: 101, KEY_PENDING: 8}, 'C': {}}
        self.owner = 'B'
        self.migrating_to = None
        self.importing_from = None

    def start(self):
        self.migrating_to = 'C'
        self.importing_from = 'B'

    def route(self, target, key, *, asking=False):
        assert hash_slot(key) == 10015, 'lab only covers slot 10015'
        if target == 'B' and self.owner == 'B':
            if self.migrating_to and key not in self.nodes['B']:
                return 'ASK C'
            return 'SERVE B'
        if target == 'C' and self.owner == 'B':
            return 'SERVE C' if self.importing_from and asking else 'MOVED B'
        return f'SERVE {target}' if target == self.owner else f'MOVED {self.owner}'

    def move_key(self, key):
        assert self.migrating_to == 'C' and self.importing_from == 'B'
        self.nodes['C'][key] = self.nodes['B'].pop(key)

    def finish(self):
        assert not any(hash_slot(key) == 10015 for key in self.nodes['B'])
        self.owner = 'C'
        self.migrating_to = None
        self.importing_from = None


def main():
    assert {hash_slot(k) for k in (KEY_MOVED, KEY_PENDING, KEY_NEW)} == {10015}
    c = ToyCluster()
    c.start()
    print('T0 迁移开始，B 仍持有全部旧 Key')
    print('  B GET post:42:views ->', c.route('B', KEY_MOVED))
    c.move_key(KEY_MOVED)
    print('T1 已搬走 post:42:views，另一个旧 Key 仍在 B')
    print('  B GET post:42:views ->', c.route('B', KEY_MOVED))
    print('  C GET（不带 ASKING） ->', c.route('C', KEY_MOVED))
    print('  C ASKING + GET ->', c.route('C', KEY_MOVED, asking=True))
    print('  B GET post:6281:views ->', c.route('B', KEY_PENDING))
    print('  B SET 新 Key ->', c.route('B', KEY_NEW))
    assert c.route('B', KEY_MOVED) == 'ASK C'
    assert c.route('C', KEY_MOVED, asking=True) == 'SERVE C'
    assert c.route('C', KEY_MOVED) == 'MOVED B'
    assert c.nodes['C'][KEY_MOVED] == 101
    assert c.nodes['B'][KEY_PENDING] == 8
    c.nodes['C'][KEY_NEW] = 1  # 教学模型：代表 ASKING 后目标成功创建
    c.move_key(KEY_PENDING)
    c.finish()
    print('T2 Slot 所有权切到 C')
    print('  B GET post:42:views ->', c.route('B', KEY_MOVED))
    print('  C GET post:42:views ->', c.route('C', KEY_MOVED))
    assert c.route('B', KEY_MOVED) == 'MOVED C'
    assert c.nodes['C'][KEY_MOVED] == 101
    print('断言全部通过')


if __name__ == '__main__':
    main()
```

运行：

```bash
python3 slot_migration_lab.py
```

这段脚本已在 Python 环境中真实运行，输出如下：

```text
T0 迁移开始，B 仍持有全部旧 Key
  B GET post:42:views -> SERVE B
T1 已搬走 post:42:views，另一个旧 Key 仍在 B
  B GET post:42:views -> ASK C
  C GET（不带 ASKING） -> MOVED B
  C ASKING + GET -> SERVE C
  B GET post:6281:views -> SERVE B
  B SET 新 Key -> ASK C
T2 Slot 所有权切到 C
  B GET post:42:views -> MOVED C
  C GET post:42:views -> SERVE C
断言全部通过
```

它给我们三个可直接观察的结论：

1. **迁移刚开始：** 原有 Key 留在 B，所以 B 正常执行。
2. **迁移进行中：** 搬走的 Key 会触发一次性的 `ASK`，还没搬走的 Key 继续由 B 服务；C 在 `ASKING` 后可以处理导入 Slot 的请求。
3. **迁移完成后：** Slot 的所有者正式改为 C，旧节点 B 返回 `MOVED`，客户端可以更新路由。

你还可以尝试一个反例：删除模型中 `finish()` 对 B 是否还有本 Slot Key 的检查，并在只迁移第一个 Key 后立刻结束。此时 `post:6281:views` 仍在 B，但所有请求已经被路由向 C。这正是我们在第一节拒绝“先改归属”的原因。

这个反例属于教学模型中的人为破坏；真实 Redis 的 `CLUSTER SETSLOT ... NODE` 在相应条件下会检查源节点是否还有本 Slot 的 Key。

## 十、这次引入的机制解决了什么？

回头看，前后其实只有四个必要变化。

最开始，Slot 有一个稳定所有者。它已经足以完成正常读写，但扩容时如果直接修改所有权，会让原来的 Key 暂时无法访问。

于是我们允许 Slot 进入迁移中间状态。原所有者 B 继续保持 Slot 的正式归属，新节点 C 只接受迁入的 Key。

这时部分 Key 已搬走、部分还留在 B，单靠一张 Slot 路由表又不够了。于是出现 ASK 和一次性的 ASKING，让客户端能够为**当前这一个 Key**找到正确节点，而不提前改变未来所有请求的路线。

等到全部 Key 迁移完成，最后才修改 Slot 所有权。此时 MOVED 告诉还在使用旧映射的客户端：**这个 Slot 已经永久改由 C 负责。**

这套设计让我们可以逐步完成扩容，但并不意味着迁移过程零成本、零错误或保证所有写入都不会受影响。`MIGRATE` 的 I/O 失败、多 Key 操作、客户端对 ASK/MOVED 的正确处理、集群状态不一致等都需要额外考虑。

现在我们留下一个自然的问题。

刚才我们的操作都是一次访问一个 Key：

```redis
GET post:42:views
```

可实际业务可能需要**一次同时操作两个 Key**。例如，扣减某文章的优惠额度，同时增加已使用计数。

如果两个 Key 被分配到了不同 Slot，应该由哪一个节点执行？

即使两个 Key 位于同一个 Slot，但迁移时其中一个已经到了 C，另一个还留在 B，又怎么办？

这将引出下一篇：

**《一次操作跨越两个分片怎么办？——从 CROSSSLOT 到事务边界》**。

---

## 固定源码与验证范围

- **Redis 版本：** [`redis/redis` Tag `7.2.4`](https://github.com/redis/redis/tree/7.2.4)。
- **迁移状态与归属切换：** [`src/cluster.c` — `clusterCommand()`](https://github.com/redis/redis/blob/7.2.4/src/cluster.c#L5614-L5739)。
- **请求路由与重定向：** [`src/cluster.c` — `getNodeByQuery()`、`clusterRedirectClient()`](https://github.com/redis/redis/blob/7.2.4/src/cluster.c#L6692-L6920)。
- **临时授权：** [`src/cluster.c` — `askingCommand()`](https://github.com/redis/redis/blob/7.2.4/src/cluster.c#L6628-L6639) 与 [`src/networking.c` — `resetClient()`](https://github.com/redis/redis/blob/7.2.4/src/networking.c#L1903-L1927)。
- **管理工具的数据搬运：** [`src/redis-cli.c` — `clusterManagerMoveSlot()`、`clusterManagerMigrateKeysInSlot()`](https://github.com/redis/redis/blob/7.2.4/src/redis-cli.c#L4718-L4952)。
- **协议解释：** [Redis Cluster Specification](https://redis.io/docs/latest/operate/oss_and_stack/reference/cluster-spec/)、[CLUSTER SETSLOT](https://redis.io/docs/latest/commands/cluster-setslot/)、[MIGRATE](https://redis.io/docs/latest/commands/migrate/)。

**验证说明：** 已核对上述 Redis 7.2.4 固定版本的函数与关键分支，已实际执行纯 Python 实验并核对输出。**没有**启动 Redis 7.2.4 多节点集群、执行真实 `redis-cli --cluster reshard` 或测试实际网络故障。文章中所有节点地址及 Redis 协议响应均为有源码依据的示例，不冒充 Redis 实测输出。Redis 8.4 起新增的原子 Slot 迁移命令不属于本文固定版本，不应混用其实现细节。
