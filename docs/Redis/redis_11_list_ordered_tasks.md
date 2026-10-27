---
title: "第十一章：从 Hash 到 List，为什么有些数据需要顺序？"
sidebar_label: "11 - 从 Hash 到 List"
sidebar_position: 12
description: "延续活动报名业务，从 Hash 无法自然管理有序、可重复的任务出发，追踪 Redis 7.2.0 的 LPUSH、LRANGE、RPOP，以及 listpack 与 quicklist 的编码转换。"
tags:
  - Redis
  - 源码精读
  - List
  - listpack
  - quicklist
  - 数据结构
---

# 第十一章：从 Hash 到 List，为什么有些数据需要顺序？

上一章，我们为活动 `1001` 保存了多个属性：标题、状态和访问次数。把它们放进一个 Hash 后，读取某个字段、修改某个字段，都不必再让客户端重写整个对象。

例如：

```text
HSET activity:1001 title "Autumn Sale" status active views 100
HGET activity:1001 status
```

Hash 很适合回答“**这个活动当前是什么状态**”。

但运营团队又提出了一个新需求：用户报名成功后，系统需要把“发送报名确认通知”交给后台处理。报名会连续发生，我们想先处理先报名的用户。

假设按先后顺序产生了三项任务：

```text
报名 2001 → 通知任务 signup:2001
报名 2002 → 通知任务 signup:2002
报名 2003 → 通知任务 signup:2003
```

**我们关心的不再是某个字段当前是什么，而是任务以什么顺序进入、应该从什么位置取出。**

这是否还能用 Hash 解决？可以尝试；但先别急着换数据结构。

## 1. 先用已经学过的 Hash 实现

最直观的做法，是在一个 Hash 中给每项任务设置字段：

```text
HSET activity:1001:pending task:1 signup:2001
HSET activity:1001:pending task:2 signup:2002
HSET activity:1001:pending task:3 signup:2003
```

它确实保存了三项任务。如果知道字段名，就能获取对应任务：

```text
HGET activity:1001:pending task:1
```

预期返回：

```text
"signup:2001"
```

但是，下一个任务应该从哪个字段读取？

业务程序必须额外管理 `task:1`、`task:2` 这样的序号，知道哪个序号已经消费，哪个序号尚未使用，还要考虑多名生产者同时分配序号时的冲突。更重要的是，**Hash 的字段枚举没有“按照任务写入时间排序”这一语义保证**；不能把 `HGETALL` 或 `HSCAN` 的返回顺序当作任务顺序。

如果同一个用户报名事件需要重复记录，例如发生两次不同的通知尝试，用同一个字段执行 `HSET` 还会覆盖之前的值。我们当然可以继续为每条记录设计唯一字段，但这样就越来越像是在 Hash 外部手工实现一套队列。

问题由此变得很明确：

> 我们需要一个数据结构，让每次报名都能追加一个**独立元素**，元素可以重复，并且天然具有先后位置；后台可以按指定方向依次取出。

这时才有必要引入 List。

## 2. 先在 Python 里做一个最小 List

如果所有任务都在同一个进程中，可以用 Python 的 `deque` 完成：

```python
from collections import deque

queue = deque()

queue.appendleft("signup:2001")
queue.appendleft("signup:2002")
queue.appendleft("signup:2003")

print(list(queue))
# ['signup:2003', 'signup:2002', 'signup:2001']

print(queue.pop())
# signup:2001
```

这里我们规定：

- 新任务从**左边**进入；
- 最早进入的任务从**右边**离开。

数据的变化是：

```text
第一次入队： [2001]
第二次入队： [2002] [2001]
第三次入队： [2003] [2002] [2001]
                                      ^
                                   先从右侧取
```

这样，`2001` 先于 `2002` 被处理，`2002` 又先于 `2003`。

这不是队列唯一的方向约定；“右边进入、左边取出”也一样有效。关键在于**入队和出队使用不同的端**。

但 Python 的 `deque` 仍然只属于本地进程。我们现在已经有一个多个业务进程共同访问的 Redis 服务，自然希望把同样的顺序操作交给 Redis 执行。

## 3. 用 LPUSH 和 RPOP 代替本地 deque

Redis List 的元素具有明确的顺序，并且支持从两端操作。

目前我们只需要两个命令：

```text
LPUSH key element   # 从左边插入
RPOP  key           # 从右边取出并删除
```

它们分别对应前面 Python 示例中的 `appendleft()` 和 `pop()`。

启动实验实例（如果此前已经启动了名为 `redis-source-lab` 的容器，直接复用即可）：

```bash
docker run -d \
  --name redis-source-lab \
  -p 127.0.0.1:6379:6379 \
  redis:7.2.0 \
  --save "" \
  --appendonly no
```

本系列为分析源码而固定在 Redis 7.2.0；此处关闭持久化仅供实验，不是生产配置。

先清空本章使用的队列：

```bash
docker exec redis-source-lab redis-cli DEL activity:1001:notify:queue
```

然后依次写入三个任务：

```bash
docker exec redis-source-lab redis-cli LPUSH activity:1001:notify:queue signup:2001
docker exec redis-source-lab redis-cli LPUSH activity:1001:notify:queue signup:2002
docker exec redis-source-lab redis-cli LPUSH activity:1001:notify:queue signup:2003
```

三次命令的预期返回值依次是 `1`、`2`、`3`：`LPUSH` 返回插入后列表的长度，而不是被插入的任务本身。

现在我们想**检查列表里有什么，但暂时不取走任务**。这时使用 `LRANGE`：

```bash
docker exec redis-source-lab redis-cli LRANGE activity:1001:notify:queue 0 -1
```

预期结果：

```text
1) "signup:2003"
2) "signup:2002"
3) "signup:2001"
```

`0` 表示从左侧第一个元素开始；`-1` 表示最后一个元素。由于新任务不断从左侧加入，`LRANGE` 的展示顺序自然是“最新的在前”。**展示顺序不是消费顺序**：我们约定从右侧取出任务。

执行一次：

```bash
docker exec redis-source-lab redis-cli RPOP activity:1001:notify:queue
```

预期返回：

```text
"signup:2001"
```

再次检查列表：

```text
LRANGE activity:1001:notify:queue 0 -1
```

预期只剩：

```text
1) "signup:2003"
2) "signup:2002"
```

这里有一个与 `LRANGE` 完全不同的效果：`RPOP` **不仅读取，还会移除元素**。再执行两次 `RPOP`，`2002` 和 `2003` 也会依次被取出。

到这里，消息任务的先后处理需求已经得到最小实现。

但如果只是记住两条命令，我们还不知道 Redis 内部究竟怎样完成“插入一端、删除另一端”。接下来进入真实源码。

## 4. LPUSH 为什么知道应该插入左边？

先跟踪这一条命令：

```text
LPUSH activity:1001:notify:queue signup:2001
```

上一专题已经阅读过 Redis 的命令分发路径，因此本章从命令处理函数直接开始。

源码位置：

- 仓库：[redis/redis](https://github.com/redis/redis)
- 固定版本：**7.2.0**
- 文件：[`src/t_list.c`](https://github.com/redis/redis/blob/7.2.0/src/t_list.c)
- 函数：`lpushCommand()` → `pushGenericCommand()` → `listTypePush()`

**真实源码节选**（`lpushCommand()`）：

```c
pushGenericCommand(c,LIST_HEAD,0);
```

这一行里真正新出现的参数是 `LIST_HEAD`，即从列表头部插入。`0` 则表示普通 `LPUSH` 可以在 Key 不存在时创建 List，而不是只在 Key 已存在时插入。

为什么 Redis 不直接在 `lpushCommand()` 中完成插入？因为 `RPUSH` 的行为与它非常接近，只是方向相反：

```c
pushGenericCommand(c,LIST_TAIL,0);
```

两条命令复用同一段处理逻辑，只把“从哪一端插入”作为参数。这正是我们在 Python 版本中已经理解的区别。

现在继续跟踪 `pushGenericCommand()`。

## 5. List 不存在时，Redis 先做什么？

`pushGenericCommand()` 接收已经解析好的 `client *c`。对于当前例子，可以教学化地理解为：

```text
c->argv[0] = LPUSH
c->argv[1] = activity:1001:notify:queue
c->argv[2] = signup:2001
```

函数先通过 `lookupKeyWrite()` 查找目标 Key，再检查现有对象是否为 List。如果当前 Key 实际上是 Hash 或 String，就返回类型错误，而不是暗中把它改为 List。

如果 Key 不存在，普通 `LPUSH` 会新建 List 对象。

**真实源码节选**（`pushGenericCommand()`）：

```c
lobj = createListListpackObject();
dbAdd(c->db,c->argv[1],lobj);
```

第一行创建一个 List 类型的 Redis 对象；第二行将该对象和顶层 Key 联系起来。这解释了为什么实验不需要提前运行一条“创建 List”命令。

这两行还有一个值得注意的名字：`createListListpackObject()`。

它告诉我们：**在 Redis 7.2.0 中，新建的小 List 首先使用 `listpack` 编码**。

先不要立刻研究 `listpack` 的每个字节。对当前问题，只需要理解它是一种紧凑存放多个元素的内存表示形式，并且支持在两端添加元素。

List 对象准备好后，Redis 才真正执行插入。

## 6. 真正的插入发生在哪里？

在 `pushGenericCommand()` 中，Redis 会先判断当前编码是否需要转换，再遍历本次请求中的待插入元素。关键调用分别是：

```c
listTypeTryConversionAppend(lobj,c->argv,2,c->argc-1,NULL,NULL);
listTypePush(lobj,c->argv[j],where);
```

这里的 `where` 来自之前的 `LIST_HEAD`。`c->argv[j]` 则是当前要插入的具体任务。

**为什么要先尝试转换编码，再插入？**

因为底层如果正在使用紧凑的 `listpack`，当元素数量或存储规模即将超过配置阈值时，继续把所有元素塞在同一块紧凑内存中可能不再合适。

所以 Redis 先确认适合采用哪种编码，然后把实际插入工作交给统一的 `listTypePush()`。

进入 `listTypePush()` 后，它会判断对象的编码。

- 对 `OBJ_ENCODING_LISTPACK`，字符串元素按方向调用 `lpPrepend()` 或 `lpAppend()`；
- 对 `OBJ_ENCODING_QUICKLIST`，由 `quicklistPush()` 完成头部或尾部插入。

这是**教学化简**，对应的真实分支位于 [`t_list.c` 的 `listTypePush()`](https://github.com/redis/redis/blob/7.2.0/src/t_list.c#L153-L176)。真正的函数还要处理整数编码的输入，不能直接用这个文字描述代替完整源码。

这层抽象解决了一个真实问题：对外只需要“头部插入”这个语义，但内部可根据数据规模选择更合适的结构。客户端不会因为 List 底层编码改变，就必须从 `LPUSH` 改用另一个命令。

完成插入后，`pushGenericCommand()` 会将列表长度作为回复，并更新相应的修改状态和通知。

现在我们能解释第一次 `LPUSH` 的完整效果了：

```text
LPUSH activity:1001:notify:queue signup:2001
       |
       v
lpushCommand()
       |
       v
pushGenericCommand()
       |
       +-- lookupKeyWrite() 查找 Key
       |
       +-- Key 不存在，创建 List
       |
       +-- 检查是否需要编码转换
       |
       v
listTypePush(..., LIST_HEAD)
       |
       v
把 signup:2001 插入左端
       |
       v
回复列表长度 1
```

但此时还有两个问题：

1. `LRANGE` 为什么能把任务按位置读出来，却不删除？
2. `RPOP` 又为什么会删除最早进入的任务？

我们沿着同一个列表继续向下看。

## 7. LRANGE：观察顺序，不改变队列

继续使用刚才的三个任务：

```text
左端                                      右端
[signup:2003] [signup:2002] [signup:2001]
```

执行 `LRANGE key 0 -1` 后，所有元素仍然存在。因为它走的是读取路径，而不是删除路径。

源码位置：`src/t_list.c`，`lrangeCommand()`。

这个函数先解析 `start` 和 `stop` 参数，再通过 `lookupKeyReadOrReply()` 查找列表对象；找到之后调用：

```c
addListRangeReply(c,o,start,end,0);
```

这里并没有调用列表删除函数。

`addListRangeReply()` 会先取得列表长度，并把 `-1` 这样的负索引换算成实际位置。例如长度为 3 时，`-1` 就指向下标 `2`。随后它按实际编码选择读取方式：

- 对 `listpack`，使用对应的定位与遍历接口取得元素；
- 对 `quicklist`，使用 quicklist 的迭代逻辑取得元素。

每取得一个元素，就把它加入客户端回复。**读取后列表没有被修改**，这正是我们能反复执行 `LRANGE` 而不丢掉任务的原因。

此处需要注意一个成本边界：`LRANGE` 返回的元素越多，Redis 需要遍历和发送的数据就越多。`LRANGE key 0 -1` 很适合检查本章的三个任务，不意味着在一个拥有数百万元素的 List 上频繁取出全部内容也同样轻量。

## 8. RPOP：为什么一次取走就从列表消失？

我们的目标是先处理最早进入的 `signup:2001`，所以消费者执行：

```text
RPOP activity:1001:notify:queue
```

源码仍在 `src/t_list.c`。

**真实源码节选**（`rpopCommand()`）：

```c
popGenericCommand(c,LIST_TAIL);
```

这和 `LPUSH` 的入口很相似：具体命令只指定方向，公共函数负责实际操作。

对于没有 `count` 参数的单元素 `RPOP`，`popGenericCommand()` 会查找 List，然后调用：

```c
value = listTypePop(o,where);
```

这里的 `where` 是 `LIST_TAIL`，即从右端取出。

进入 `listTypePop()`，可以看到两类编码各有自己的实现：

- 如果是 `listpack`，Redis 用 `lpLast()` 定位尾部元素，提取其内容，再调用 `lpDelete()` 将这个元素移除；
- 如果是 `quicklist`，Redis 通过 `quicklistPopCustom()` 从指定端取出元素。

注意，这里不存在“先给客户端看一眼，等它处理成功后再删除”的步骤。元素在 `listTypePop()` 执行过程中就已经从 List 移除了。

之后，`popGenericCommand()` 将取到的值加入响应，并调用 `listElementsRemoved()` 完成删除之后的维护。如果最后一个元素也被取走了，该函数还会删除空 List 对应的顶层 Key。

因此，三次出队的过程是：

```text
初始： [2003] [2002] [2001]
RPOP： 取出 2001 → [2003] [2002]
RPOP： 取出 2002 → [2003]
RPOP： 取出 2003 → 空
```

现在我们终于能分清：

- `LRANGE`：读取一段列表内容，**不移除元素**；
- `RPOP`：读取并移除右端元素；
- `LPUSH`：在左端添加元素。

三者组合已经满足当前最小的有序任务场景。

## 9. 如果任务越来越多，为什么不始终使用 listpack？

这个问题不是凭空出现的。

刚开始的活动可能只有几条报名通知，使用紧凑内存表示很合适。但假设短时间内积累了几十万条任务，每次插入都要继续操作同一块越来越大的紧凑存储区域，就可能造成不必要的数据移动与内存管理成本。

现在我们需要一个机制：**保留 List 的前后顺序与两端操作语义，同时让底层存储能够随数据规模增长而组织成多个片段。**

Redis 7.2.0 的一种解决方式是 `quicklist`。

先用一个教学示意图理解两种表示：

```text
少量元素：listpack

[2001 | 2002 | 2003]
   一块紧凑存储

元素变多：quicklist（示意）

[节点 A] <--> [节点 B] <--> [节点 C]
   |            |            |
 listpack     listpack     listpack
```

这并不是 quicklist 内部对象的完整定义，只强调它能把元素组织在多个双向连接的节点中；节点通常承载一批紧凑保存的元素，也可以根据内容与配置采取其他表示方式。

前面在 `pushGenericCommand()` 中已经见过转换前的检查：

```c
listTypeTryConversionAppend(lobj,c->argv,2,c->argc-1,NULL,NULL);
```

它会进入 `listTypeTryConversionRaw()`；当对象目前使用 `listpack`，且追加后可能超过配置的容量/数量限制时，再尝试将它转换成 `quicklist`。

具体转换位于 `listTypeTryConvertListpack()`，其设计动作是：创建 quicklist，将已有 listpack 放入 quicklist 节点，随后更新对象的编码。

注意，`quicklist` 不是 Redis List 对外提供的第二套命令。**它是同一个 List 类型在不同规模下的一种内部编码选择**。即使对象从 `listpack` 转换为 `quicklist`，客户端仍然使用相同的 `LPUSH`、`LRANGE`、`RPOP`。

这就是“对外的数据操作语义”与“对内的存储表示”分开设计的价值。

### 一个能够观察编码转换的实验

为了不靠猜测判断 List 当前的内部表示，我们使用 `OBJECT ENCODING` 命令观察。

> 下面会修改实验实例的 `list-max-listpack-size` 配置，请只在本地独立 Redis 实例上操作。正数表示 quicklist 节点允许的元素数量限制；我们把它设为 2，以便用很少的元素触发转换。

先清理专用测试 Key，然后设置阈值：

```bash
docker exec redis-source-lab redis-cli DEL demo:list:encoding
docker exec redis-source-lab redis-cli CONFIG SET list-max-listpack-size 2
```

先放入两个元素：

```bash
docker exec redis-source-lab redis-cli RPUSH demo:list:encoding one two
docker exec redis-source-lab redis-cli OBJECT ENCODING demo:list:encoding
```

在 Redis 7.2.0 对应的实现下，预期输出：

```text
"listpack"
```

再放入第三个元素：

```bash
docker exec redis-source-lab redis-cli RPUSH demo:list:encoding three
docker exec redis-source-lab redis-cli OBJECT ENCODING demo:list:encoding
```

预期输出：

```text
"quicklist"
```

检查数据是否仍保持原来的顺序：

```bash
docker exec redis-source-lab redis-cli LRANGE demo:list:encoding 0 -1
```

预期结果仍是：

```text
1) "one"
2) "two"
3) "three"
```

最后恢复实验配置并清理测试数据：

```bash
docker exec redis-source-lab redis-cli CONFIG SET list-max-listpack-size -2
docker exec redis-source-lab redis-cli DEL demo:list:encoding
```

这个实验要验证的是：**改变的是内部编码，不是 List 对外可见的顺序。**

还有一个细节：当 List 后续缩小时，源码中也包含尝试从 quicklist 转回 listpack 的路径，且缩小时采用更谨慎的转换阈值，避免长度在边界附近变化就反复转换。当前只需要知道存在这项权衡；不必在本章展开其每个分支。

## 10. 回到任务队列：List 的能力边界在哪里？

目前这套设计适合演示简单的任务排队。`LPUSH + RPOP` 在同一个 Redis 实例上的正常命令路径中能够按顺序完成插入和移除；两个消费者也不会同时成功取走同一个已被移除的元素。

但我们不能把“能够按顺序取出”误认为“任务一定可靠完成”。

假设后台消费者执行：

```text
RPOP activity:1001:notify:queue
```

获得 `signup:2001` 后，还没来得及发送通知，进程就崩溃了。

此时列表已经删除该元素，Redis 并不知道通知是否成功。**RPOP 的原子性保护的是这一次列表修改，并不保证 Redis 之外的业务副作用已经完成。**

因此，Redis List 本身是一种有序数据类型；“任务队列”是对它的一种使用方式，消息确认与故障恢复是进一步的系统设计问题。

这些问题值得作为单独的 Redis 消息机制专题研究，但不需要在本章突然引入完整的消息系统架构。

我们仍然聚焦于当前得到的认识：

- Hash 管理一个对象的多个**命名属性**；
- List 管理一串允许重复、具有**位置顺序**的元素；
- 两者服务的是不同的数据组织和操作需求，而不是谁全面优于谁。

### 列表和业务顺序也不是一回事

本章的“先报名先处理”建立在一个明确前提上：任务提交给 Redis 的先后顺序符合我们希望采用的顺序。

如果多个应用进程同时生成任务，Redis 能确定实际完成入队的命令顺序，却不知道业务上谁“更早报名”；它也不会自动根据外部数据库中的报名时间重新排列任务。

因此，List 提供的是**元素在列表中的顺序**，不是对所有外部业务事件的全局时间保证。

## 11. 用两个进程验证任务传递

如果希望不只运行 `redis-cli`，还可以让活动服务生产任务、让通知服务消费任务。

安装客户端：

```bash
python -m pip install "redis==5.0.1"
```

先清空本章队列：

```bash
docker exec redis-source-lab redis-cli DEL activity:1001:notify:queue
```

创建 `producer.py`：

```python
import redis

client = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)
key = "activity:1001:notify:queue"

for registration_id in (2001, 2002, 2003):
    task = f"signup:{registration_id}"
    size = client.lpush(key, task)
    print(f"任务入队：{task}；列表长度：{size}")
```

创建 `consumer.py`：

```python
import redis

client = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)
key = "activity:1001:notify:queue"

while True:
    task = client.rpop(key)
    if task is None:
        print("队列已空，本轮结束")
        break
    print(f"模拟发送报名通知：{task}")
```

先运行生产者，再运行消费者：

```bash
python producer.py
python consumer.py
```

消费者的预期输出：

```text
模拟发送报名通知：signup:2001
模拟发送报名通知：signup:2002
模拟发送报名通知：signup:2003
队列已空，本轮结束
```

这并不是一个持续驻留的工作进程：为了让最小实验能有明确的结束条件，消费者在列表为空时直接退出。后续如果真正实现长期运行的队列消费者，我们才需要研究如何等待新任务，以及任务取出之后出现故障怎么办。

本实验中的“发送通知”只是打印日志，没有真正调用外部短信接口；它证明的是多个进程能够通过 Redis 共享一个有序任务列表，不是通知服务的端到端可靠性。

**实验声明**：文中输出均为按照 Redis 7.2.0 源码及命令语义推导的预期值；本文编写环境未启动 Docker/Redis 进行实测，不将预期结果称为已经运行通过。

## 12. 回看演化：为什么这次需要 List？

从上一章到本章，出现的并不是“Redis 还有一个数据类型需要学习”，而是业务访问数据的方式真正改变了。

```text
活动当前属性
    |
    | 按字段查找和更新
    v
Hash
    |
    | 新需求：报名任务不断产生，必须保留出现顺序
    v
如果继续用 Hash，需要自己管理序号和出队位置
    |
    v
List：按位置组织元素
    |
    | 从一端写入，从另一端取出
    v
LPUSH + RPOP
    |
    | 需要先检查队列而不删除任务
    v
LRANGE
    |
    | 元素越来越多，单块紧凑存储不再总是合适
    v
listpack / quicklist 编码选择
```

沿着 Redis 7.2.0 的源码，我们已经知道：

1. `lpushCommand()` 把“从头部插入”的方向传给 `pushGenericCommand()`。
2. `pushGenericCommand()` 查找或创建 List，并在需要时先尝试转换底层编码，再调用 `listTypePush()`。
3. `lrangeCommand()` 把指定范围的元素加入响应，并不删除它们。
4. `rpopCommand()` 通过 `popGenericCommand()` 和 `listTypePop()` 移除尾部元素；最后一个元素移除后，空 List 的 Key 也会删除。
5. 小 List 可使用 `listpack`，需要时可转为 `quicklist`；这种编码变化不改变命令语义。

这一章的主要目标已经完成：**我们不需要靠字段名伪造顺序，而是让数据结构本身承担顺序语义。**

## 13. 下一章：如果只想知道“用户是否已经报名”呢？

现在，活动系统有了按顺序处理报名通知的能力。

但报名接口又提出了一个新要求：同一个用户不能重复报名。

如果我们把所有已报名用户 ID 都放在 List 中，当然能存下来。不过，要判断 `user:42` 是否已经出现过，就不能只看列表的某一端；对于一个很长的列表，查找成本可能越来越高。

而且，用户是否已报名与通知任务是否仍在队列里，是两种不同的状态。已经处理并弹出的通知任务，不能作为“用户已经报名”的长期凭据。

于是问题变成：

> **我们不关心用户报名的排列位置，只关心一个用户是否属于已报名集合，而且不希望重复添加同一个用户。**

这正是下一章引出 Redis Set 的自然起点。

**下一章：《为什么 List 不适合做报名去重？从顺序存储到 Set 集合》**

---

## 源码与参考资料

本章源码基线固定为 **Redis 7.2.0**，不混用更新版本。

- [`src/t_list.c` — `pushGenericCommand()`、`lpushCommand()`、`listTypePush()`](https://github.com/redis/redis/blob/7.2.0/src/t_list.c#L448-L481)
- [`src/t_list.c` — `lrangeCommand()`、`popGenericCommand()`、`listTypePop()`](https://github.com/redis/redis/blob/7.2.0/src/t_list.c)
- [`src/t_list.c` — `listTypeTryConvertListpack()`、`listTypeTryConvertQuicklist()`](https://github.com/redis/redis/blob/7.2.0/src/t_list.c#L33-L135)
- [`src/object.c` — `createListListpackObject()`](https://github.com/redis/redis/blob/7.2.0/src/object.c#L222-L227)
- [`src/quicklist.c` — quicklist 底层组织与操作](https://github.com/redis/redis/blob/7.2.0/src/quicklist.c)
- [Redis 官方：List 数据类型](https://redis.io/docs/latest/develop/data-types/lists/)
- [Redis 官方：OBJECT ENCODING](https://redis.io/docs/latest/commands/object-encoding/)
