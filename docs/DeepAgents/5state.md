---
title: 第五章：Graph State、Reducer、DeltaChannel 与 Checkpoint
description: 从状态化执行的设计理论出发，结合 DeepAgents 与 LangGraph 源码，理解消息状态、文件状态、增量更新、持久化与恢复
tags: [DeepAgents, LangGraph, State, Reducer, DeltaChannel, Checkpoint]
sidebar_position: 5
---

# 第五章：Graph State、Reducer、DeltaChannel 与 Checkpoint——Agent 为什么需要状态化运行？

## 一、先从一个实际问题开始

我们继续使用前面的优惠券退款 Bug。

用户提出：

> 修复优惠券部分退款计算错误，并验证修改结果。

假设 Agent 已经执行了四步：

```text
用户提出修复任务
        |
        v
模型请求读取 refund.py
        |
        v
read_file 返回文件内容
        |
        v
模型请求修改 refund.py
        |
        v
edit_file 修改成功
```

现在我们希望回答三个问题：

**第一个问题：** 下一轮模型调用如何知道此前读取、修改了哪些文件？

**第二个问题：** 文件修改以后，新的文件内容究竟保存在哪里？

**第三个问题：** 假设 Agent 在修改之后等待人工审批，进程重启后如何继续？

如果我们只保存：

```python
messages = [...]
```

是否足够？

不够。

因为一个真实 Agent 至少涉及三种不同的信息：

```text
模型与工具交互历史
任务当前状态
执行环境当前状态
```

而且它们有不同的更新规则、持久化需求和恢复方式。

所以我们不能再把 Agent 理解成一个简单的：

```python
while True:
    response = model(messages)
```

需要引入状态化执行模型。

---

## 二、理论基础：什么是 State？

这一节的内容在 [第六章 Checkpoint 与中断恢复](../Agent/06-checkpoint-resume.md) 里已经用事故推过一遍：

> 历史还在，为什么 Agent 仍然不能继续？

当时的结论是：**「保存了消息历史」和「能继续执行」是两件不同的事。** 要继续执行，至少还需要知道「执行到哪一步」「下一步该做什么」。

本章要做的，是把那个结论落到源码上，看清 LangGraph 到底存了什么、怎么存、以及在什么时机存。

在普通函数中，程序状态可能是：

```python
counter = 10
messages = []
```

函数直接修改变量即可。

但是对于复杂 Agent，这会带来问题。

例如：

```python
def model_node():
    global messages
    ...

def tool_node():
    global messages
    ...
```

如果两个节点同时执行，谁先修改 `messages`？

如果一个节点执行失败，应该保留哪些修改？

如果进程重启，之前的状态在哪里？

如果同一服务正在运行一百个 Agent，会话状态又如何隔离？

这就是 LangGraph 使用显式 State 的原因。

### 2.1 State 是一次 Graph 执行的逻辑状态

可以理解为：

```python
state = {
    "messages": [...],
    "files": {...},
    "task_status": "in_progress",
}
```

其中：

`messages` 记录对话和工具执行反馈。

`files` 在使用 StateBackend 时保存虚拟文件内容。

`task_status` 可以由应用自己定义，表示当前任务进度。

但需要注意：

这个字典只是**状态值的直观表示**。

LangGraph 的底层并不是简单让所有节点共享同一个可随意修改的 Python 字典。

它通过 State Schema、Channel 和 Reducer 管理状态更新。

### 2.2 State 和运行环境不是同一回事

假设我们使用：

```python
FilesystemBackend(
    root_dir="/project"
)
```

真实文件存在宿主机：

```text
/project/refund.py
```

文件内容并不会因为存在 `Graph State` 就自动复制一份进去。

反过来，如果使用：

```python
StateBackend()
```

虚拟文件会放在 Graph State 的：

```python
state["files"]
```

字段中。

所以：

```text
                 Agent State
                      |
          +-----------+-----------+
          |                       |
          v                       v
      messages                 files
          |                       |
          |                 StateBackend
          |
          v
     模型和工具历史


               External Environment
                      |
          +-----------+-----------+
          |                       |
          v                       v
       Host Files               Database
          |                       |
          v                       v
    FilesystemBackend        External API
```

**Graph State 不等于所有外部系统的真实状态。**

这是理解 Checkpoint 和恢复语义的前提。

---

## 三、第二个核心概念：节点为什么不直接修改 State？

假设当前 State 是：

```python
state = {
    "messages": [
        HumanMessage(content="修复退款 Bug")
    ],
    "task_status": "started",
}
```

模型执行完成后产生：

```python
AIMessage(
    content="",
    tool_calls=[
        {
            "id": "read-1",
            "name": "read_file",
            "args": {
                "file_path": "/refund.py"
            }
        }
    ],
)
```

在 LangGraph 中，模型节点通常返回一个**局部状态更新**：

```python
return {
    "messages": [ai_message]
}
```

而不是：

```python
state["messages"].append(ai_message)
```

这是一项刻意的设计。

### 3.1 局部更新和完整状态的区别

当前状态：

```python
{
    "messages": [HumanMessage(...)],
    "task_status": "started",
}
```

节点只返回：

```python
{
    "messages": [AIMessage(...)]
}
```

LangGraph 需要根据对应字段的 Reducer 决定最终状态。

例如：

```text
                 Current State
                       |
                       v
             messages: [Human]
                       |
                       |
                 Node Update
                       |
                       v
                messages: [AI]
                       |
                       v
                 State Reducer
                       |
                       v
             messages: [Human, AI]
```

但注意：

**不是所有字段默认都会追加。**

如果某个字段没有定义 Reducer，新的更新通常会覆盖旧值。

因此：

```text
局部更新
    ≠
简单合并字典
```

Reducer 的作用就是定义“更新”究竟意味着什么。

---

## 四、第三个核心概念：Reducer 决定状态如何演化

Reducer 可以用一个公式理解：

```text
New State Value = Reducer(Old Value, Update)
```

例如：

```python
def append_items(old, update):
    return old + update
```

如果：

```python
old = ["A"]
update = ["B"]
```

结果是：

```python
["A", "B"]
```

而默认覆盖语义相当于：

```python
def overwrite(old, update):
    return update
```

### 4.1 为什么不同字段需要不同 Reducer？

考虑下面三类数据。

| State 字段 | 理想更新方式 | 原因 |
|---|---|---|
| `task_status` | 覆盖 | 状态从 pending 变为 completed |
| `messages` | 按消息 ID 合并 | 追加新消息，也可能替换或删除消息 |
| `files` | 按路径合并 | 修改一个文件，不应该丢失其他文件 |

例如：

```python
state = {
    "files": {
        "/refund.py": "old",
        "/test_refund.py": "tests",
    }
}
```

现在修改退款文件：

```python
update = {
    "files": {
        "/refund.py": "new"
    }
}
```

如果简单覆盖：

```python
state["files"] = update["files"]
```

结果会是：

```python
{
    "/refund.py": "new"
}
```

那么：

```text
/test_refund.py
```

就意外消失了。

因此文件状态需要按路径合并。

### 4.2 使用 Annotated 定义 Reducer

LangGraph 使用 `Annotated` 将合并规则附加到 State Schema。

例如：

```python
from typing import Annotated
from typing_extensions import TypedDict


def merge_files(
    old: dict[str, str] | None,
    update: dict[str, str | None],
) -> dict[str, str]:

    result = dict(old or {})

    for path, content in update.items():
        if content is None:
            result.pop(path, None)
        else:
            result[path] = content

    return result


class MyState(TypedDict):
    files: Annotated[dict[str, str], merge_files]
    task_status: str
```

这段是独立的教学实现。

它体现了 DeepAgents 文件状态 Reducer 的核心思想：

```text
新文件路径
    -> 增加

已有文件路径
    -> 更新

值为 None
    -> 删除
```

例如：

```python
merge_files(
    {
        "/refund.py": "old",
        "/tests.py": "tests",
    },
    {
        "/refund.py": "new",
        "/unused.py": None,
    },
)
```

结果是：

```python
{
    "/refund.py": "new",
    "/tests.py": "tests",
}
```

### 设计原则

Reducer 让节点不必关心完整状态应该如何重建。

节点只需要表达：

> 我对哪个字段产生了什么更新？

至于如何合并，由 State Schema 统一负责。

这是一种**将业务更新意图与状态合并机制分离**的设计。

---

## 五、真实源码：DeepAgentState 定义了什么？

现在开始进入 DeepAgents。

源码：

[deepagents/graph.py](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py)

找到：

```python
class DeepAgentState(AgentState):
    ...
```

最关键的字段定义是：

```python
messages: Required[
    Annotated[
        list[AnyMessage],
        DeltaChannel(
            _messages_delta_reducer,
            snapshot_frequency=50,
        ),
    ]
]
```

这是对实际源码的重新排版。

这里出现三个概念：

```text
AgentState
DeltaChannel
_messages_delta_reducer
```

我们逐个分析。

### 5.1 为什么继承 AgentState？

因为 DeepAgents 没有必要重新定义通用 Agent 的全部状态协议。

LangChain 已经提供：

```python
AgentState
```

用于表示 Agent 的基本 State。

DeepAgents 通过继承扩展：

```python
DeepAgentState
```

并特别调整消息历史的 Channel。

为什么？

因为复杂 Agent 可能执行很多轮。

它的消息历史可能持续增长。

因此普通消息列表的检查点存储成本，可能成为长期运行的瓶颈。

### 5.2 为什么使用 DeltaChannel？

先看没有增量存储时的情况。

假设每一轮 Agent 都增加一条消息。

```text
Step 1:
  [M1]

Step 2:
  [M1, M2]

Step 3:
  [M1, M2, M3]

Step 4:
  [M1, M2, M3, M4]
```

如果每一个 Checkpoint 都完整保存整个消息列表，那么累计存储量大致是：

```text
1 + 2 + 3 + 4 + ... + N
```

也就是：

```text
O(N²)
```

对于执行几百轮、上千轮的 Agent，这会造成明显的重复序列化开销。

DeltaChannel 的思路则是：

```text
Step 1:
  Delta: M1

Step 2:
  Delta: M2

Step 3:
  Delta: M3

Step 4:
  Delta: M4
```

每一步主要保存增量，而不是反复保存整个累计列表。

对于消息长度相近、持续追加的理想情形，增量部分的累计存储量可以接近：

```text
O(N)
```

当然，真实开销仍受到消息大小、快照频率、Checkpoint 元数据及其他 State 字段影响。

**DeltaChannel 优化的是状态持久化表示，而不是自动减少模型上下文中的消息。**

这点尤其重要。

---

## 六、DeltaChannel 与普通 Reducer 有什么不同？

普通 Reducer 的函数形式通常是：

```python
reducer(old_value, update)
```

而 DeltaChannel 使用批量 Reducer：

```python
reducer(old_value, writes)
```

其中：

```python
writes
```

是一个执行步骤中需要合并的一组更新。

例如：

```python
def reducer(
    old_messages,
    writes,
):
    ...
```

这里的：

```python
writes
```

可能相当于：

```python
[
    [AIMessage(...)],
    [ToolMessage(...)],
]
```

### 6.1 为什么要批量处理？

因为 LangGraph 是一种状态图运行时。

同一个执行阶段可能产生多个 State 更新。

如果采用增量存储与恢复，就需要保证：

```text
分别合并更新
```

和：

```text
批量合并更新
```

得到一致结果。

理论上，需要满足批处理不变性：

```text
R(R(state, A), B) == R(state, A + B)
```

对于 DeltaChannel，这是一条非常重要的 Reducer 约束。

否则某次从 Checkpoint 恢复时，如果更新被重新分批，最终状态就可能改变。

因此，一个正确的 Delta Reducer 应该具有：

- 确定性。
- 对批量分组方式不敏感。
- 稳定的消息标识处理。
- 明确的删除和覆盖语义。

这不是普通列表拼接那么简单。

### 6.2 为什么 snapshot_frequency=50？

如果只保存增量，恢复时就需要反复重放增量更新。

例如：

```text
Checkpoint 1
   |
   +-- Delta 2
   +-- Delta 3
   +-- Delta 4
   ...
   +-- Delta 500
```

如果每次读取消息历史都要重放几百条记录，读取性能可能下降。

所以 DeltaChannel 允许周期性生成完整快照。

DeepAgents 配置：

```python
snapshot_frequency=50
```

可以理解为：

```text
起始快照
    |
    +-- 若干 Delta
    |
    v
下一次周期性快照
    |
    +-- 若干 Delta
    |
    v
继续执行
```

它在：

```text
写入成本
```

和：

```text
状态重建成本
```

之间做权衡。

快照越频繁，恢复重放的成本通常越低，但持久化成本会相应增加。

这体现的是经典的：

**Snapshot + Incremental Log（快照与增量记录）设计思想。**

注意 DeltaChannel 属于有版本要求且仍在演进的接口，升级或回滚 LangGraph 时必须关注 Checkpoint 格式兼容性。

---

## 七、真实源码：_messages_delta_reducer 如何处理消息？

现在打开：

[deepagents/_messages_reducer.py](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/_messages_reducer.py)

找到：

```python
def _messages_delta_reducer(
    state,
    writes,
):
    ...
```

它不是简单地：

```python
return state + writes
```

因为消息历史需要支持更复杂的操作。

例如：

```text
追加新消息
按 ID 替换消息
移除指定消息
清空历史中的一段消息
将消息字典转换成 Message 对象
```

### 7.1 为什么需要按 ID 合并？

假设已有：

```python
AIMessage(
    id="message-1",
    content="正在调查",
)
```

后续希望将同一个消息更新成：

```python
AIMessage(
    id="message-1",
    content="已经调查完成",
)
```

如果只是追加：

```text
message-1: 正在调查
message-1: 已经调查完成
```

会产生重复的消息 ID。

更合理的是：

```text
message-1: 已经调查完成
```

因此 Reducer 会维护：

```python
index: dict[str, int]
```

根据消息 ID 判断应该追加还是替换。

### 7.2 为什么需要 RemoveMessage？

因为上下文管理可能需要删除或重置部分消息历史。

例如摘要后，不再希望模型继续使用某些旧消息。

但注意：

删除某些模型消息、修改当前 Graph State，与清除已经存在的所有历史 Checkpoint 并不是完全相同的操作。

Reducer 必须明确如何处理：

```python
RemoveMessage(...)
```

以及特殊的全量重置标记。

### 7.3 为什么消息 ID 必须稳定？

这是一个深层设计问题。

假设 Delta Reducer 每次重放历史时，都调用：

```python
uuid.uuid4()
```

重新为消息生成 ID。

那么：

```text
第一次合并：
  message-a

Checkpoint 恢复重放：
  message-b
```

同一条逻辑消息就可能被识别成两条消息。

因此，DeltaChannel 的消息 Reducer 应该使用稳定 ID，而不是在重放过程中生成随机的新 ID。

DeepAgents 固定版本的源码也专门说明：消息 ID 应在进入持久化更新前被稳定地赋予，而不是在需要重放的 Reducer 中随机生成。

### 核心思想

**消息历史不仅是列表，更是一份支持增量更新、按 ID 覆盖和删除操作的结构化日志。**

---

## 八、文件 State 又是怎样更新的？

我们回到上一章研究过的：

```python
StateBackend
```

当模型调用：

```python
write_file(
    file_path="/refund.py",
    content="...",
)
```

Backend 需要把新的文件内容写入：

```python
state["files"]
```

但它不是简单地执行：

```python
state["files"][path] = content
```

而是通过 LangGraph 的 Channel 写入机制。

### 8.1 真实源码：CONFIG_KEY_SEND

源码：

[backends/state.py](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/backends/state.py)

核心实现：

```python
send = config["configurable"][CONFIG_KEY_SEND]

send([("files", update)])
```

例如逻辑上提交：

```python
{
    "/refund.py": new_file_data
}
```

而不是提交完整的：

```python
{
    "/refund.py": ...,
    "/test_refund.py": ...,
    "/readme.md": ...,
}
```

### 8.2 这和 ToolMessage 是什么关系？

工具执行后可能同时发生两种更新。

```text
                     write_file()
                          |
               +----------+----------+
               |                     |
               v                     v
           ToolMessage          Backend.write()
               |                     |
               v                     v
       messages Channel          files Channel
               |                     |
               v                     v
       Message Reducer          File Reducer
               |                     |
               +----------+----------+
                          |
                          v
                    Updated State
```

这两个 Channel 的职责完全不同。

`messages` 记录：

> 文件写入成功或失败。

`files` 保存：

> 当前文件内容是什么。

### 8.3 为什么 files 需要支持删除标记？

假设删除：

```python
delete("/unused.py")
```

不能只从当前 Python 字典中临时移除。

必须向状态系统表达：

> 删除这个路径。

因此，文件合并协议允许：

```python
{
    "/unused.py": None
}
```

表示删除。

这是一种 Tombstone（删除标记）思想。

它在增量状态管理、数据库复制和事件日志中都很常见。

---

## 九、LangGraph 在什么时候应用这些更新？

现在我们把视角从 DeepAgents 转向 LangGraph Runtime。

前面说过，Graph Node 不直接负责全局状态的最终合并。

那么谁负责？

答案是 LangGraph 的执行调度机制。

LangGraph 的运行模型受到 Pregel 计算模型启发。

可以把它简化理解为：

```text
                  Current State
                       |
                       v
                 Select Tasks
                       |
                       v
                  Execute Nodes
                       |
          +------------+------------+
          |                         |
          v                         v
       Model Node               Other Node
          |                         |
          v                         v
      State Update             State Update
          |                         |
          +------------+------------+
                       |
                       v
                  Apply Reducers
                       |
                       v
                   New State
                       |
                       v
                Next Super-step
```

这里的一个执行阶段称为 Super-step。

### 9.1 为什么不允许节点随意共享可变状态？

因为如果两个节点并行执行：

```text
Node A
  修改 messages

Node B
  修改 messages
```

然后都直接操作同一个全局列表，就容易出现：

- 更新顺序不明确。
- 状态竞争。
- 部分修改难以回滚。
- 恢复时结果不一致。

现在节点提交更新，Runtime 统一处理状态合并。

但这不代表所有并发问题都被自动解决。

例如两个工具同时修改同一份外部文件，仍然可能发生实际写入冲突。

**State Reducer 解决的是 Graph State 的更新语义，不是所有外部副作用的事务一致性。**

---

## 十、Checkpoint 到底是什么？

现在进入本章的另一半。

这一节的参照物还是 [第六章](../Agent/06-checkpoint-resume.md)。那一章有三个递进的故障：

```text
把状态存起来
    ↓ 但仍然重复执行
有 Checkpoint，为什么还会重复执行？
    ↓ 恢复和重放不是同一件事
恢复和重放，真的是同一件事吗？
```

源码这一节要回答的是**第一个故障之后的部分**：一个 Checkpoint 里到底要装多少东西，才能既不丢状态，又不重复执行。

假设 Agent 执行到这里：

```text
用户任务
    |
    v
读取 refund.py
    |
    v
修改 refund.py
    |
    v
等待人工批准运行测试
```

此时用户可能几个小时以后才批准。

如果程序只在内存中保存：

```python
messages
files
task_status
```

进程退出后，状态就丢失了。

因此需要持久化。

这就是 Checkpoint 解决的问题。

### 10.1 Checkpoint 不是简单保存 messages

一个可恢复的 Graph，除了当前 State，还需要知道：

```text
当前属于哪个 Thread？
已经执行到哪一步？
下一步应该执行哪些任务？
当前有哪些待处理的中断或任务？
状态对应哪个 Checkpoint？
```

因此，Checkpoint 的概念不只是：

```python
json.dump(messages)
```

而更接近：

```text
                 Checkpoint
                      |
          +-----------+-----------+
          |           |           |
          v           v           v
       State       Metadata    Execution Position
          |           |           |
          v           v           v
       messages     step         next tasks
       files        source       pending work
       task data    writes       interrupts
```

这是概念示意；实际持久化结构还包括版本、Channel 记录和 Checkpoint 标识等运行时数据。

### 10.2 为什么需要记录下一步？

假设 Agent 已经成功读取文件。

然后被中断。

如果恢复时只恢复：

```python
messages
```

但不知道下一步该做什么，就可能重新从入口执行所有操作。

这可能导致：

```text
重复调用工具
重复修改文件
重复提交 API 请求
```

所以状态恢复还需要保存执行位置和调度信息。

不过要注意：

**Checkpoint 不能保证任意外部副作用都恰好执行一次。**

在故障恢复和重放场景中，某些节点仍可能重新执行。

如果工具操作有副作用，必须考虑幂等性与去重。

---

## 十一、Checkpointer 和 Store 有什么区别？

这个问题非常重要。

LangGraph 提供两类不同的持久化机制。

### Checkpointer

保存某个 Thread 的 Graph 执行状态。

例如：

```text
Thread A
    |
    +-- messages
    +-- files
    +-- task state
    +-- checkpoints
```

主要用于：

```text
多轮对话连续性
中断恢复
时间旅行
故障恢复
```

### Store

保存应用级、跨 Thread 的数据。

例如：

```text
User Knowledge
    |
    +-- 退款业务规范
    +-- 项目编码约定
    +-- 长期偏好
```

主要用于：

```text
跨会话知识
长期记忆
共享数据
```

它们之间的关系是：

```text
                   Application
                        |
             +----------+----------+
             |                     |
             v                     v
        Checkpointer             Store
             |                     |
             v                     v
        Thread State          Cross-thread Data
             |                     |
             v                     v
       短期执行状态             长期共享知识
```

这也说明：

**Memory 不等于 Checkpoint。**

Memory 是应用希望长期保存和重用的信息。

Checkpoint 是 Graph 执行状态的持久化机制。

两者可以配合，但职责不同。

---

## 十二、真实源码：如何开启 Checkpoint？

对于 LangGraph：

```python
from langgraph.checkpoint.memory import InMemorySaver

checkpointer = InMemorySaver()
```

然后在构建 Graph 时传入：

```python
graph = builder.compile(
    checkpointer=checkpointer,
)
```

对于 DeepAgents：

```python
from deepagents import create_deep_agent
from langgraph.checkpoint.memory import InMemorySaver

agent = create_deep_agent(
    model=model,
    checkpointer=InMemorySaver(),
)
```

这里 `model` 是已经配置好的模型实例。

### 为什么 invoke 时还要指定 thread_id？

例如：

```python
config = {
    "configurable": {
        "thread_id": "refund-task-001"
    }
}
```

执行：

```python
agent.invoke(
    {
        "messages": [
            {
                "role": "user",
                "content": "修复优惠券退款 Bug",
            }
        ]
    },
    config=config,
)
```

`thread_id` 用于标识这条执行线程。

后续再次执行：

```python
agent.invoke(
    {
        "messages": [
            {
                "role": "user",
                "content": "继续检查刚才的修复结果",
            }
        ]
    },
    config=config,
)
```

LangGraph 就可以从同一个 Thread 已保存的状态继续。

### InMemorySaver 能跨进程恢复吗？

不能。

它只把检查点保存在当前进程内存。

如果要支持进程重启后的恢复，需要使用数据库等持久化 Checkpointer。

因此：

```text
配置 Checkpointer
     ≠
一定实现持久化到磁盘
```

还必须看使用的是哪一种 Checkpointer。

---

## 十三、最小实验：自己实现一个状态化 Agent Loop

前面讲了理论，也分析了真实源码。

现在用一个最小实验把整个过程连接起来。

这次我们不使用真实 LLM。

因为目标是验证：

```text
State
Reducer
ToolMessage
Checkpoint
```

这些 Runtime 机制。

模型节点采用受控逻辑，模拟模型决定调用工具。

但 Graph 执行、Reducer 和 Checkpointer 都使用真实 LangGraph。

### 13.1 安装

```bash
pip install "langgraph>=1.2" langchain-core
```

建议与 DeepAgents 0.7.32 的依赖版本保持一致。

### 13.2 完整代码

```python
from typing import Annotated

from typing_extensions import TypedDict

from langgraph.graph import (
    StateGraph,
    START,
    END,
)

from langgraph.graph.message import add_messages

from langgraph.checkpoint.memory import InMemorySaver

from langchain_core.messages import (
    HumanMessage,
    AIMessage,
    ToolMessage,
)


# =============================================
# 1. 定义文件状态的 Reducer
# =============================================

def merge_files(
    old: dict[str, str] | None,
    updates: dict[str, str | None],
) -> dict[str, str]:

    result = dict(old or {})

    for path, content in updates.items():

        if content is None:
            result.pop(path, None)

        else:
            result[path] = content

    return result


# =============================================
# 2. 定义 State Schema
# =============================================

class RefundState(TypedDict):

    messages: Annotated[
        list,
        add_messages,
    ]

    files: Annotated[
        dict[str, str],
        merge_files,
    ]

    task_status: str


# =============================================
# 3. 模拟模型节点
# =============================================

def model_node(state: RefundState):

    messages = state["messages"]

    last_message = messages[-1]

    # 如果刚刚收到了 ToolMessage，
    # 就完成当前任务。
    if isinstance(last_message, ToolMessage):

        return {
            "messages": [
                AIMessage(
                    content=(
                        "已根据工具结果定位退款精度问题。"
                    )
                )
            ],
            "task_status": "completed",
        }

    # 同一个 Thread 的后续用户提问：
    # 利用已经保存的工具历史回答。
    if any(
        isinstance(m, ToolMessage)
        for m in messages
    ):

        return {
            "messages": [
                AIMessage(
                    content=(
                        "之前已经检查退款计算。"
                        "历史记录和文件状态仍然存在。"
                    )
                )
            ],
            "task_status": "completed",
        }

    # 第一次模型执行：
    # 模拟产生工具调用。
    return {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "refund-call-1",
                        "name": "inspect_refund",
                        "args": {},
                    }
                ],
            )
        ],
        "task_status": "waiting_for_tool",
    }


# =============================================
# 4. 模拟工具节点
# =============================================

def tools_node(state: RefundState):

    print("[TOOL] Inspecting refund calculation")

    # 通过局部更新模拟文件状态变化。
    # 并没有真正修改宿主机文件。
    return {
        "messages": [
            ToolMessage(
                name="inspect_refund",
                tool_call_id="refund-call-1",
                content=(
                    "Current code loses decimal precision."
                ),
            )
        ],
        "files": {
            "/refund.py": (
                "def refund(amount):\n"
                "    return round(amount, 2)\n"
            )
        },
        "task_status": "tool_finished",
    }


# =============================================
# 5. 定义 Agent Loop 路由
# =============================================

def route_after_model(state: RefundState):

    last_message = state["messages"][-1]

    if (
        isinstance(last_message, AIMessage)
        and last_message.tool_calls
    ):
        return "tools"

    return END


# =============================================
# 6. 构建 Graph
# =============================================

builder = StateGraph(RefundState)

builder.add_node("model", model_node)
builder.add_node("tools", tools_node)

builder.add_edge(START, "model")

builder.add_conditional_edges(
    "model",
    route_after_model,
)

builder.add_edge("tools", "model")


# =============================================
# 7. 加入 Checkpointer
# =============================================

checkpointer = InMemorySaver()

agent = builder.compile(
    checkpointer=checkpointer,
)


# =============================================
# 8. 第一次执行
# =============================================

config = {
    "configurable": {
        "thread_id": "refund-task-001"
    }
}

result = agent.invoke(
    {
        "messages": [
            HumanMessage(
                content="修复优惠券退款精度问题"
            )
        ],
        "files": {
            "/refund.py": (
                "def refund(amount):\n"
                "    return round(amount)\n"
            ),
        },
        "task_status": "started",
    },
    config=config,
)

print("\n=== First Run ===")

print("Status:", result["task_status"])

print("Files:", result["files"])

print(
    "Messages:",
    [type(m).__name__ for m in result["messages"]],
)


# =============================================
# 9. 读取 Checkpoint
# =============================================

snapshot = agent.get_state(config)

print("\n=== Checkpoint ===")

print("State:", snapshot.values)
print("Next Nodes:", snapshot.next)


# =============================================
# 10. 第二次执行：同一 Thread
# =============================================

result2 = agent.invoke(
    {
        "messages": [
            HumanMessage(
                content="刚才修改的 refund.py 还在吗？"
            )
        ]
    },
    config=config,
)

print("\n=== Second Run ===")

print("Messages:", len(result2["messages"]))
print("Files:", result2["files"])

assert "/refund.py" in result2["files"]

assert "round(amount, 2)" in result2["files"]["/refund.py"]

assert result2["task_status"] == "completed"

print("\nState persistence verified.")
```

### 13.3 实验实际验证什么？

第一次运行：

```text
HumanMessage
      |
      v
Model Node
      |
      v
AIMessage(tool_calls)
      |
      v
Tools Node
      |
      +-- ToolMessage
      |
      +-- files update
      |
      v
State Reducers
      |
      v
Model Node
      |
      v
Final AIMessage
      |
      v
Checkpoint
```

第二次运行：

```text
Same thread_id
      |
      v
Load Checkpoint
      |
      v
Existing Messages + Files
      |
      v
Append New HumanMessage
      |
      v
Model Node
      |
      v
Final AIMessage
```

这里的消息使用 LangGraph 的 `add_messages`，便于观察消息按 ID 合并的行为。

**它没有使用 DeepAgents 专用的 DeltaChannel，因此不能用这个实验直接证明 DeltaChannel 的磁盘存储优化效果。**

想验证 DeltaChannel，还需要进一步比较实际 Checkpoint 的存储结构与大小。

另外，本示例使用真实 API 组织，但未在当前环境完成依赖安装后的执行验证。

---

## 十四、查看 Checkpoint 历史

前面的实验使用：

```python
snapshot = agent.get_state(config)
```

读取当前状态。

但 Checkpointer 保存的通常不只有最后一份状态。

我们还可以：

```python
history = list(
    agent.get_state_history(config)
)

for snapshot in history:
    print(
        snapshot.metadata,
        snapshot.next,
    )
```

通过这些历史快照，可以研究：

```text
哪个节点执行完了？
当时有哪些 State 值？
下一步准备执行什么？
是否存在待恢复任务？
```

这也是 LangGraph 支持 Time Travel 的基础。

### 14.1 Time Travel 不等于简单回滚数据库

假设：

```text
Checkpoint A
     |
     v
Model Node
     |
     v
ToolNode 调用外部 API
     |
     v
Checkpoint B
```

如果从 Checkpoint A 重放，后续节点可能重新执行。

那就有可能再次调用外部 API。

因此 Time Travel 的语义是：

**从保存的 Graph 状态和执行位置重新开始一条执行路径。**

不是：

**自动撤销所有外部副作用，再精确恢复整个世界。**

这是生产 Agent 必须注意的设计边界。

---

## 十五、从 Checkpoint 理论引出 Interrupt / Resume

下一次我们研究 HITL 时，还会遇到：

```python
interrupt(...)
```

以及：

```python
Command(resume=...)
```

这两个机制为什么能够工作？

因为 LangGraph 可以保存：

```text
当前 State
当前执行位置
待继续的任务
中断信息
```

一个典型过程是：

```text
                   Agent Loop
                        |
                        v
                    Tool Call
                        |
                        v
                  Approval Required
                        |
                        v
                     interrupt()
                        |
                        v
                 Save Checkpoint
                        |
                        v
                   Return Control
                        |
                        v
                    Human Decision
                        |
                        v
                  Command(resume=...)
                        |
                        v
                 Restore Execution
                        |
                        v
                    Continue Graph
```

这里还有一个重要细节：

**Resume 并不是简单调用原函数的下一行代码。**

LangGraph 的恢复执行具有自己的节点和任务重放语义，因此包含副作用的代码需要考虑幂等性。

第 09 章会从源码深入研究这一点。

---

## 十六、State、Checkpoint、Memory、Context 的根本区别

前面四章，加上本章，现在可以把几个容易混淆的概念彻底区分。

| 概念 | 本质 | 核心问题 |
|---|---|---|
| State | 当前 Graph 的逻辑状态 | 程序现在是什么状态？ |
| Reducer | State 字段的合并规则 | 新更新应该怎样应用？ |
| Channel | LangGraph 的状态承载与更新机制 | 状态如何在执行步骤之间传递？ |
| DeltaChannel | 增量持久化 Channel | 大型累计状态如何减少重复保存？ |
| Checkpoint | 可恢复的执行状态记录 | 中断或重启后怎样继续？ |
| Store | 跨 Thread 的应用数据存储 | 长期知识在哪里保存？ |
| Working Context | 本轮实际传给模型的信息 | 模型这次应该看到什么？ |

最后一个尤其重要。

例如：

```text
Graph State 中保存：
    200 条消息

本次模型输入：
    最近 20 条消息
    + 历史摘要
    + 相关知识
```

这种情况完全合理。

State 可以保存更完整的执行历史，而 Context Management 决定本次模型实际看到什么。

因此：

**State Persistence 与 Context Compaction 不是同一个机制。**

这正是下一章的主题。

---

## 十七、再回到 DeepAgents 的真实调用链

现在我们将本章知识放回真正的 Agent Loop。

```text
                       Agent Loop
                           |
                           v
                      Model Node
                           |
                           v
                        AIMessage
                           |
                           v
                    Command / Update
                           |
                           v
                      Graph State
                           |
                           v
                     Message Channel
                           |
                           v
                 _messages_delta_reducer
                           |
                           v
                     Updated Messages
                           |
                           v
                        ToolNode
                           |
                           v
                     Tool Execution
                           |
               +-----------+-----------+
               |                       |
               v                       v
          ToolMessage             StateBackend
               |                       |
               v                       v
         Message Channel           files Channel
               |                       |
               v                       v
         Message Reducer            File Reducer
               |                       |
               +-----------+-----------+
                           |
                           v
                    Updated Graph State
                           |
                           v
                      Checkpointer
                           |
                           v
                    Saved Execution
                           |
                           v
                      Next Agent Step
                           |
                           v
                        Model Node
```

需要注意：这是一张逻辑流程图，并不意味着 LangGraph 严格在每个普通工具函数之后立即生成一份完全独立的磁盘 Checkpoint。

具体持久化与执行边界由 LangGraph Runtime 和所选 Checkpointer 管理。

这张图真正体现的是：

**State 更新与状态持久化由 Runtime 统一管理，而不是分别交给每个 Model Node 或 ToolNode 自行处理。**

---

## 十八、设计上的四个重要取舍

### 18.1 为什么采用增量更新，而不是共享可变 State？

因为增量更新更适合：

```text
并发节点
明确的更新协议
可恢复执行
状态追踪
```

代价是需要认真设计 Reducer，并处理冲突和合并顺序。

### 18.2 为什么 DeltaChannel 不只保存完整快照？

因为长 Agent 的累计消息历史会导致重复序列化。

代价是恢复状态时可能需要重放增量。

所以需要周期性 Snapshot。

### 18.3 为什么不把外部环境全部放进 State？

因为真实数据库、进程和文件系统可能很大，而且存在自身的事务和生命周期。

Agent State 不应该试图完整复制所有外部环境。

代价是恢复 Graph State 时，必须考虑外部系统可能已经发生变化。

### 18.4 为什么 Checkpoint 不等于精确一次执行？

因为 Checkpoint 只能管理它能够持久化的 Graph 执行信息。

无法自动为任意外部 API、Shell 命令或第三方服务提供分布式事务。

因此重要操作仍然需要：

```text
Idempotency Key
去重机制
执行状态记录
事务或补偿策略
```

这属于生产级 Agent Harness 的可靠性设计。

---

## 十九、本章最值得记住的五个源码位置

| 源码位置 | 关键机制 |
|---|---|
| `deepagents/graph.py` | `DeepAgentState`，定义消息 DeltaChannel |
| `deepagents/_messages_reducer.py` | `_messages_delta_reducer`，消息合并 |
| `deepagents/middleware/filesystem.py` | `FilesystemState` 和文件 Reducer |
| `deepagents/backends/state.py` | `CONFIG_KEY_READ` / `CONFIG_KEY_SEND` |
| `langgraph/graph/state.py` | StateGraph 和 State Schema 处理 |

进一步进入 LangGraph：

```text
langgraph/channels/
    Channel 与 DeltaChannel

langgraph/pregel/
    Graph 执行与状态推进

langgraph/checkpoint/
    Checkpoint 协议和存储实现
```

阅读这些源码时，不需要一口气搞懂所有 Checkpointer 的数据库实现。

优先追踪：

```text
State Schema
    |
    v
Node Update
    |
    v
Reducer / Channel
    |
    v
Checkpoint
    |
    v
Resume
```

这条主线即可。

---

## 二十、本章总结

我们从：

```python
agent.invoke(...)
```

开始研究 State 为什么必须存在。

然后逐步发现：

**第一，State 是 Agent Runtime 的逻辑数据模型。**

它不只是聊天消息，还可以包含文件、任务状态及其他执行信息。

**第二，Reducer 定义状态的更新协议。**

节点提交的是增量，Runtime 根据 Reducer 决定如何合并。

**第三，DeltaChannel 解决大型累计状态的重复持久化问题。**

它通过增量记录和周期性快照，在写入成本与状态重建成本之间做权衡。

**第四，Checkpoint 保存可恢复的执行状态。**

它不仅涉及 State 值，还包括 Thread、执行位置和待处理任务信息。

**第五，Checkpoint 不等于 Memory，也不等于外部系统事务。**

恢复 Graph 不意味着任意外部副作用都能自动回滚或保证精确一次执行。

最终可以用一句话概括：

> **LangGraph 将 Agent 从“调用模型和工具的循环”，提升为“以状态更新为核心、能够持久化和恢复的执行系统”。**

### 回到理论：第六章的三个故障，源码补上了哪一块

[第六章 Checkpoint 与中断恢复](../Agent/06-checkpoint-resume.md) 用四个递进的故障推出恢复机制该有什么：

| 理论中的故障 | 理论结论 | 本章源码落点 |
|---|---|---|
| 历史还在，却不能继续 | 只存 messages 不够 | Checkpoint 还存 `next`、待处理任务 |
| 有 Checkpoint，却重复执行 | 要记录「执行到哪一步」 | 快照绑定在节点边界上 |
| 恢复和重放不是同一件事 | 恢复 ≠ 重放副作用 | **源码无解，见下** |
| 恢复时要重新构建 Context | Context 不随状态自动恢复 | 由 `ModelRequest` 重新组装（下一章） |

前两条，源码给出了明确机制。第三条是这一章必须说清楚的地方。

**LangGraph 能恢复 Graph State，但恢复不了外部世界的副作用。**

如果被中断的那一步是 `execute("pytest")`，恢复时重新执行这个工具调用，测试会再跑一遍。如果被中断的是 `edit_file`，恢复会再写一次。LangGraph 保证的是「状态机从哪继续」，不是「副作用只发生一次」。

这解释了源码里一个看起来很奇怪的设计：`PatchToolCallsMiddleware` 的 `before_agent`。

```python
answered_ids = {msg.tool_call_id for msg in messages if msg.type == "tool"}
```

它在 Agent 启动前扫描「有 tool_call 但没有对应 ToolMessage」的悬空调用，然后补一条错误消息：

> `Tool call {name} with id {tool_call_id} did not complete - no result was recorded. It may have been cancelled or interrupted.`

这条消息的存在本身就是证据：**框架承认中断会留下不完整的历史，而且不打算替你重试。** 它只是把「这个调用没有结果」如实告诉模型，让模型自己决定要不要重做。

> [!IMPORTANT] 这是本章最值得记住的一处
> 恢复能力的边界不是「能不能恢复状态」，而是「恢复后副作用会不会重复」。
> 后者必须由你自己处理：给写操作加幂等键、给命令执行加结果缓存、或者在恢复前先检查世界状态。

---

# 下一章：Context Management——Graph State 如何变成模型实际看到的上下文？

本章我们解决了：

```text
状态如何更新？
状态如何保存？
状态如何恢复？
```

但新的问题是：

假设 State 中已经保存了：

```text
300 条消息
20 份文件
10 次工具执行日志
若干任务摘要
```

模型的上下文窗口并不能无限增长。

这时：

**模型应该看到哪些内容？哪些可以压缩？哪些应该卸载到 Backend？**

下一章我们进入：

```text
                 Graph State
                      |
                      v
              Context Management
                      |
          +-----------+-----------+
          |           |           |
          v           v           v
       History     Summarize    Offloading
       Select      / Compact     / Retrieval
          |           |           |
          +-----------+-----------+
                      |
                      v
                 ModelRequest
                      |
                      v
                 Model.invoke()
```

重点源码：

- `SummarizationMiddleware`
- `create_summarization_middleware()`
- FilesystemMiddleware 的大内容卸载机制
- `ModelRequest.messages`
- State History 与模型可见消息的区别

要回答的核心问题是：

> **为什么 Agent 可以保留较完整的执行状态，却不必在每一轮把全部历史重新发送给 LLM？**

这也是 Context Engineering 与 Agent Runtime 真正连接起来的位置。

---

## 本章源码与参考

DeepAgents 固定版本：`e85c6b9`

- [DeepAgentState：graph.py](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py)
- [消息 Delta Reducer](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/_messages_reducer.py)
- [FilesystemMiddleware](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py)
- [StateBackend](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/backends/state.py)
- [LangGraph StateGraph](https://github.com/langchain-ai/langgraph/blob/main/libs/langgraph/langgraph/graph/state.py)
- [LangGraph DeltaChannel](https://github.com/langchain-ai/langgraph/blob/main/libs/langgraph/langgraph/channels/delta.py)
- [LangGraph Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api)
- [LangGraph Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)
- [LangGraph Time Travel](https://docs.langchain.com/oss/python/langgraph/use-time-travel)
