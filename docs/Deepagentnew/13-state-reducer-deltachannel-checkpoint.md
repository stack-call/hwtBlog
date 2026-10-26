---
title: 第十三章：Agent 如何保持状态并从中断处继续？——State、Reducer、DeltaChannel 与 Checkpoint
description: 从优惠券部分退款 Bug 的跨轮状态一致性开始，沿 DeepAgents 0.7.32 的真实源码，渐进理解节点更新、字段合并、消息与文件 Reducer、DeltaChannel 增量重放、Checkpointer、Thread、恢复与外部副作用边界。
tags: [DeepAgents, LangChain, LangGraph, State, Reducer, DeltaChannel, Checkpointer, StateBackend, 源码精读]
sidebar_position: 18
---

# 第十三章：Agent 如何保持状态并从中断处继续？——State、Reducer、DeltaChannel 与 Checkpoint

> **本章核心问题：** Coding Agent 已经能够读取文件、修改代码、运行测试、维护 Todo、等待审批并委派子任务。但是，当它经历几十轮 Model → Tool → Model 循环，甚至在一次审批时暂停，**哪些信息应当留下？多处更新怎样合并？重新运行时又怎样从之前的状态继续？**

## 一、从第十二章留下的一个问题开始

上一章，我们让主 Agent 把优惠券规则调查交给子 Agent。主 Agent 收到结论后，继续推进退款修复：

```text
用户：修复优惠券部分退款精度问题
  ↓
主 Agent：读取 /shop/refund.py
  ↓
子 Agent：研究优惠券尾差归集规则
  ↓
主 Agent：收到子 Agent 的分析结果
  ↓
主 Agent：修改 /shop/refund.py
  ↓
主 Agent：运行 pytest
  ↓
测试失败：最后一次退款仍有 0.01 的尾差
  ↓
主 Agent：再次修改、测试……
```

如果在这条链上，我们只使用一个普通 Python `while` 循环，那么一开始确实可以写得非常简单：

```python
# 概念伪代码：说明问题，不是 DeepAgents 实现
messages = [user_message]
files = {}
todos = []

while True:
    response = model(messages)
    messages.append(response)

    if not response.tool_calls:
        break

    for call in response.tool_calls:
        result = run_tool(call, files, todos)
        messages.append(result)
```

在单进程、单线程、不暂停、工具顺序执行的情况下，这样已经能工作。

所以我们暂时不需要谈 Checkpoint，也不需要谈 DeltaChannel。先研究一个更基础、马上会遇到的问题：**谁决定一轮工具完成后，`messages`、`files` 和 `todos` 分别变成什么？**

### 1.1 本章的固定源码基线

沿用系列课程之前的版本：

```text
DeepAgents Python：0.7.32
DeepAgents Commit：e85c6b9
LangChain 最低依赖：1.4.7
LangChain 阅读基线：1.4.7 / fbb8a35
LangGraph：使用与上述依赖兼容、包含 DeltaChannel 的版本
```

重要说明：DeepAgents 的 [`pyproject.toml`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/pyproject.toml) 声明 `langchain>=1.4.7,<2.0.0`，**没有在这个文件里钉死一个唯一的 LangGraph Commit**。因此本章将 DeepAgents 自身的实现严格固定在 `e85c6b9`；涉及 LangGraph Channel 与 Checkpoint 的内部解释，以其公开实现和 API 契约为依据，而不把 LangGraph `main` 分支中的每一个内部细节冒称为某个已固定的依赖版本。

本章阅读的核心入口：

- [DeepAgents `graph.py`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py)：`DeepAgentState` 以及 `create_deep_agent()` 如何把状态和 Checkpointer 交给 LangChain。
- [DeepAgents `_messages_reducer.py`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/_messages_reducer.py)：消息增量合并的确切规则。
- [DeepAgents `middleware/filesystem.py`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py)：`FilesystemState` 与文件更新 Reducer。
- [DeepAgents `backends/state.py`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/backends/state.py)：`StateBackend` 如何读写 Graph 的 `files` 字段。
- [LangGraph `graph/state.py`](https://github.com/langchain-ai/langgraph/blob/main/libs/langgraph/langgraph/graph/state.py)：`StateGraph` 如何根据 State Schema 组织节点更新。
- [LangGraph `channels/delta.py`](https://github.com/langchain-ai/langgraph/blob/main/libs/langgraph/langgraph/channels/delta.py)：`DeltaChannel` 的批量 Reducer 与重放语义。此链接是机制参考，不宣称它是 DeepAgents Commit 内的固定文件。

本章所有代码块区分三类：**真实源码节选**、**教学化简**、**概念伪代码**。涉及框架接口的示例若未在当前环境安装并运行，会明确标注。文末的无依赖 Python 实验则已实际执行。

---

## 二、为什么不能让每个节点直接修改同一个字典？

### 2.1 最简单的方案：直接修改共享变量

先假设我们只有两个步骤：

```python
# 教学化简：纯 Python 共享可变字典
state = {
    "messages": ["用户要求修复 refund.py"],
    "status": "investigating",
}


def read_refund(state):
    state["messages"].append("工具返回 refund.py 内容")


def finish_analysis(state):
    state["status"] = "ready_to_patch"
```

顺序执行时，它没有明显问题：

```text
初始 State
  messages = [用户消息]
  status = investigating

read_refund(state)
  ↓
messages 增加一条工具结果

finish_analysis(state)
  ↓
status = ready_to_patch
```

但是一旦把工具执行交给 Graph Runtime，就要面对另一种情形。

主 Agent 可能在同一轮中触发两项工作：

```text
                当前 State
                    |
           +--------+--------+
           |                 |
           v                 v
       读取 refund.py     读取 coupon_service.py
           |                 |
           v                 v
       更新 messages      更新 messages
           |                 |
           +--------+--------+
                    |
                    v
                 下一轮
```

若两个分支都“拿一份旧 State，直接生成一份新 State”，那么最后写入者可能覆盖另一分支的结果。

例如：

```python
# 教学化简：有覆盖风险的设计
old = {"messages": ["用户消息"]}

left = {"messages": old["messages"] + ["refund.py 结果"]}
right = {"messages": old["messages"] + ["coupon_service.py 结果"]}

state = left
state = right  # 左分支结果消失
```

关键矛盾不是“Python 字典不能保存状态”，而是：

> **一个字段可能收到来自不同节点的多份更新，但字段自身究竟应该“覆盖”“追加”“合并”还是“删除”，没有统一规则。**

这才把我们带到 LangGraph 的 State 设计。

### 2.2 只返回更新，而不是在节点里决定最终 State

我们先把节点改成另一种形态：

```python
# 教学化简：节点不修改输入，而是提交部分更新

def read_refund(state):
    return {"messages": ["refund.py 结果"]}


def read_coupon(state):
    return {"messages": ["coupon_service.py 结果"]}
```

注意，两份返回值都只有 `messages` 字段，没有完整复制其他 State。

这时职责变成：

```text
节点：负责计算本次更新
  ↓
Runtime：接收更新
  ↓
字段合并规则：决定新值
  ↓
下一轮节点：读到合并后的 State
```

在 LangGraph 的 `StateGraph` 中，节点的基本接口可以概括为 **State → Partial State Update**。这正是 [`StateGraph`](https://github.com/langchain-ai/langgraph/blob/main/libs/langgraph/langgraph/graph/state.py) 的设计入口。

此时还不用关心更新怎样存盘。我们先要解决**合并规则**。

---

## 三、Reducer：同一字段收到两次更新时，究竟发生什么？

### 3.1 普通字段：覆盖足够用了

假设 `status` 表示当前退款任务阶段：

```text
investigating → patching → verifying → done
```

在没有并发写入的情况下，新值覆盖旧值，是合理选择：

```python
# 教学化简
state["status"] = "patching"
```

但是 `messages` 并不适合采用同样的规则。

如果模型刚刚发出：

```text
AIMessage：我要读取 refund.py
```

工具随后返回：

```text
ToolMessage：读取结果……
```

我们真正希望的状态是：

```text
[HumanMessage, AIMessage, ToolMessage]
```

而不是：

```text
[ToolMessage]
```

因此，不同字段需要不同的更新语义。

这时才引入一个新概念：**Reducer**。

Reducer 是给某个 State 字段指定的“旧值 + 新写入 → 新值”规则。对于普通的 `Annotated[..., reducer]` 字段，Reducer 通常按**两两合并**的契约工作。

### 3.2 一个最小、可理解的 Reducer

```python
# 教学化简：普通两参数 Reducer

def append_items(old: list[str], new: list[str]) -> list[str]:
    return old + new
```

例如：

```python
assert append_items(
    ["用户：修复退款精度"],
    ["模型：读取 refund.py"],
) == [
    "用户：修复退款精度",
    "模型：读取 refund.py",
]
```

在普通 `StateGraph` 里，我们可以声明：

```python
# 框架级示例：未在当前环境执行
from typing import Annotated, TypedDict
from langgraph.graph import StateGraph


def append_items(old: list[str], new: list[str]) -> list[str]:
    return old + new


class RepairState(TypedDict):
    events: Annotated[list[str], append_items]
    status: str
```

从概念上看：

```text
events：多个写入需要合并
status：默认更新语义，单轮多写入可能冲突
```

这里有一个必须提前说清的边界：**定义了 Reducer，并不意味着并发写入就一定没有业务冲突。**

例如两条分支都修改 `refund.py` 的同一行，字典层面合并成功，不代表两份修改在业务上可以共存。Reducer 定义的是**如何聚合数据**，不是**如何解决任意并发协作问题**。

### 3.3 为什么直接追加消息仍然不够？

回到真实退款任务：模型先收到一条工具消息，后来运行阶段需要根据同一 `tool_call_id` 修正这条消息；又或者，需要删除不应该继续保留的消息。

如果只有 `old + new`，会产生重复：

```text
ToolMessage(id=t1, content="运行失败")
ToolMessage(id=t1, content="已重新读取完整失败日志")
```

但我们的期望可能是：

```text
ToolMessage(id=t1, content="已重新读取完整失败日志")
```

也就是说，**消息合并不能仅靠列表追加，而必须理解消息 ID。**

这就进入 DeepAgents 的第一份关键源码。

---

## 四、真实源码：DeepAgents 怎样合并 `messages`？

### 4.1 `DeepAgentState` 只是在 `AgentState` 上改了一个关键字段

阅读 [`deepagents/graph.py`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py#L73-L76)。

**真实源码节选**（仅去掉行尾类型检查注释）：

```python
class DeepAgentState(AgentState):
    messages: Required[Annotated[list[AnyMessage], DeltaChannel(_messages_delta_reducer, snapshot_frequency=50)]]
```

这一行其实同时透露了两个事实：

1. `DeepAgentState` 继承通用 `AgentState`，并没有另造一套 Agent 消息协议。
2. `messages` 字段使用 `_messages_delta_reducer`，同时由 `DeltaChannel` 管理增量状态。

先暂时忽略 `DeltaChannel`。我们只关心 `_messages_delta_reducer` 解决什么问题。

源码位置：[`deepagents/_messages_reducer.py`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/_messages_reducer.py)。

### 4.2 Reducer 怎样识别同一条消息？

它接收两类输入：

```text
state：现有消息列表
writes：本轮接收到的一批消息写入
```

与上一节的普通两参数 Reducer 不同，这里的 `writes` 是**写入批次的列表**。这是 `DeltaChannel` 的批量 Reducer 契约，稍后会解释为什么采用它。

先用教学化简还原其核心语义：

```python
# 教学化简：只展示 ID 合并规则，不是原函数完整实现

def merge_messages_by_id(current, writes):
    result = list(current)

    for message in writes:
        index = {msg.id: i for i, msg in enumerate(result)}
        if message.id in index:
            result[index[message.id]] = message
        else:
            result.append(message)

    return result
```

现在看一组具体输入：

```text
旧 State：
  h1: 用户要求修复退款 Bug
  a1: 模型要求读取 refund.py
  t1: 工具结果：读取失败

新写入：
  t1: 工具结果：读取成功，发现 float 计算
```

同 ID 的 `t1` 会替换先前的 `t1`，而不是再追加一条。

真实 `_messages_delta_reducer()` 还处理更多情况：

- 把字典、字符串等消息表示转换为标准消息对象。
- 将多份批次写入展开。
- 按消息 ID 替换已有消息。
- 使用 `RemoveMessage` 删除指定 ID 的消息。
- 使用 `REMOVE_ALL_MESSAGES` 标记清空之前的消息，再接入后续写入。

这些不是“不断 append”可以完成的。

### 4.3 消息 ID 从哪里来？

如果两个消息没有稳定 ID，恢复执行时再次合并，很难判断是“同一条消息的修正”还是“全新消息”。

在固定版本的 `_messages_reducer.py` 文件说明中，DeepAgents 明确指出：**消息 ID 的赋值不是在此 Reducer 内完成，而是依赖 LangGraph 在消息写入进入 Checkpoint 序列化之前提供稳定 ID。**

因此，不能把 Reducer 误解为一个“任意生成 UUID 后合并列表”的函数。

更重要的是：Reducer 可能在**状态重放**时再次运行。倘若每次重放都在 Reducer 里随机生成 ID，同一份写入历史便可能恢复成不同结果。

我们在本章后半部分会看到，这正是 DeltaChannel 为什么强调 Reducer 必须**确定性**的原因。

### 4.4 本轮结束后的可观察结果

```text
输入 messages：
  [h1, a1, t1(旧)]

写入：
  [t1(新)]

Reducer 输出：
  [h1, a1, t1(新)]
```

现在，消息的合并问题解决了。

但是 Coding Agent 不只有消息。它还会修改 `/shop/refund.py`。**文件内容也能按消息 ID 的规则合并吗？**

显然不能。

---

## 五、第二种状态：`files` 为什么需要另一种 Reducer？

### 5.1 文件不是对话消息

假设 Agent 第一次读取：

```text
/shop/refund.py

amount = float(value)
```

然后修改为：

```text
/shop/refund.py

amount = Decimal(value)
```

我们希望 `files` 中只有最新版本，而不是：

```text
[
  {path: "/shop/refund.py", content: "amount = float(value)"},
  {path: "/shop/refund.py", content: "amount = Decimal(value)"}
]
```

因为文件天然以**路径**为键。

最简单的数据结构是：

```python
# 教学化简
files = {
    "/shop/refund.py": "amount = Decimal(value)",
}
```

下一次对相同路径的写入，应当替换内容；对另一条路径的写入，应当保留已有文件；删除则应当移除该路径。

于是得到另一种字段更新规则：

```text
相同 path → 更新值
新 path   → 增加文件
值为 None → 删除文件
```

### 5.2 真实源码：`FilesystemState`

打开 [`deepagents/middleware/filesystem.py`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L1170-L1193)。

**真实源码节选**（省略行尾注释）：

```python
class FilesystemState(AgentState):
    files: Annotated[NotRequired[dict[str, FileData]], DeltaChannel(_file_data_delta_reducer, snapshot_frequency=50)]
```

这里，`files` 使用的 Reducer 不再是 `_messages_delta_reducer`，而是 `_file_data_delta_reducer`。

它的关键逻辑可以用下面的**教学化简**表达：

```python
# 教学化简：省略 FileData 的具体元数据

def merge_files(current, batches):
    result = dict(current or {})
    for changes in batches:
        for path, file_data in changes.items():
            if file_data is None:
                result.pop(path, None)
            else:
                result[path] = file_data
    return result
```

例如：

```python
old_files = {
    "/shop/refund.py": "amount = float(value)",
    "/shop/coupon_service.py": "coupon = Decimal(value)",
}

writes = [
    {"/shop/refund.py": "amount = Decimal(value)"},
    {"/shop/tests/test_refund.py": "def test_refund(): pass"},
]
```

合并后：

```text
/shop/refund.py              → 新值
/shop/coupon_service.py      → 保留
/shop/tests/test_refund.py   → 新增
```

如果后续写入：

```python
{"/shop/tests/test_refund.py": None}
```

那么对应路径会被删除。

到这一步，我们终于能够清楚区分两种 State：

| 字段 | 标识对象的关键 | 合并方式 |
|---|---|---|
| `messages` | 消息 ID | 追加、替换、删除、清空 |
| `files` | 文件路径 | 按路径合并、覆盖或删除 |

**这两个字段都在 State 中，但并不共享同一种 Reducer。**

### 5.3 `FilesystemMiddleware` 为什么能让 Agent 拥有 `files` 字段？

上一章我们看到：Middleware 不仅能提供 Hook，还能声明自己的 State Schema。

`FilesystemMiddleware` 的 `state_schema` 指向 `FilesystemState`。LangChain `create_agent()` 在构建 Graph 时，会综合基础状态结构与 Middleware 声明的状态结构。

结果就是：

```text
DeepAgentState       → messages 的合并语义
FilesystemState      → files 的合并语义
其他 Middleware State → 各自需要的额外字段
          |
          v
    Agent Graph State
```

因此，不要把 `DeepAgentState` 理解成“包含所有 Middleware 字段的单一巨大类”。在这套设计里，**不同扩展组件可以声明自己的字段要求，再由 Agent Factory 组织起来**。

现在还有一个尚未回答的问题：

**模型调用的是 `edit_file` 工具，它并没有直接调用 Reducer。文件变更到底怎样进入 `files` 状态通道？**

---

## 六、从 `edit_file` 到 State：`StateBackend` 真正提交了什么？

### 6.1 一条看起来很普通的工具调用

考虑模型产生：

```json
{
  "name": "edit_file",
  "args": {
    "file_path": "/shop/refund.py",
    "old_string": "amount = float(value)",
    "new_string": "amount = Decimal(value)"
  }
}
```

我们在第九章已经知道：

```text
ToolNode
   ↓
FilesystemMiddleware 提供的 edit_file 工具
   ↓
Backend.edit(...)
```

但这里我们刻意使用默认 `StateBackend`，而不是主机真实文件系统。

这意味着：**工具名叫 `edit_file`，不代表它会修改服务器磁盘上的 `/shop/refund.py`。**

### 6.2 最简单但不正确的猜想：直接修改 `runtime.state`

很容易把它想成：

```python
# 错误的机制想象：不是固定版本 StateBackend 的真实写法
runtime.state["files"][path] = new_content
```

但这会绕开 Graph 的 Channel 更新管理，可能破坏并发、合并与持久化语义。

真实 `StateBackend` 的方法不同。

### 6.3 真实源码：通过 Runtime Config 读写 Channel

阅读 [`deepagents/backends/state.py`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/backends/state.py#L75-L109)。

它通过 LangGraph 配置中的两个内部能力完成状态访问：

```text
CONFIG_KEY_READ → 读取 files 当前值
CONFIG_KEY_SEND → 提交 files 字段的待合并更新
```

其中提交更新的核心语句是（**真实源码节选**）：

```python
send([("files", update)])
```

为了理解这行代码，可以把完整的工作路径简化成：

```python
# 教学化简：解释框架分工，并非可调用的 SDK 代码

def edit_file(path, old_text, new_text):
    files = read_channel("files")
    old_file = files[path]
    edited = replace_text(old_file, old_text, new_text)
    send_channel_update("files", {path: edited})
    return "edit_file completed"
```

状态变化可以画成：

```text
ToolNode 调用 edit_file
          |
          v
StateBackend.edit()
          |
          v
读取 files Channel
          |
          v
根据旧内容生成新 FileData
          |
          v
发送 ("files", {path: new_file_data})
          |
          v
LangGraph Channel 应用字段 Reducer
          |
          v
下一轮读取到修改后的文件内容
```

这就是 `StateBackend` 和 Reducer 真正连接的地方。

### 6.4 为什么只提交变化的一个文件？

因为整个 `files` 字典可能很大。

假设项目中有 200 个文件。一次 `edit_file` 只修改 `/shop/refund.py`，就没有必要让工具重新返回其余 199 个文件。

所以发送的是局部更新：

```python
{"/shop/refund.py": new_file_data}
```

随后由文件 Reducer 把它合并进已有状态。

真实 `StateBackend` 的 `_read_files()` 还会请求 `fresh=True` 的读取语义，使同一执行阶段中已经排队的相关写入能够被后续读取看见；但**这不等于任意并行写同一文件都获得事务隔离**。两个工具同时读旧内容再修改同一路径，依然可能产生业务冲突。

现在，节点更新与字段合并都讲清楚了。

可是继续修复退款 Bug 时，新问题出现：**历史越来越长，我们是否要在每一步都保存整份 State？**

---

## 七、为什么“每轮保存完整状态”最终会很昂贵？

### 7.1 最直观的 Checkpoint 实现

假设我们为防止崩溃，在每次工具执行后完整保存当前 State：

```python
# 概念伪代码：最直观的全量保存方法

while not finished:
    state = do_one_step(state)
    save_checkpoint(state)
```

如果一个退款任务只运行三轮，代价很小。

但是如果有 1,000 轮，每轮都追加一条长度相近的消息，消息数量随轮数增长：

```text
第 1 次：保存 1 条消息
第 2 次：保存 2 条消息
第 3 次：保存 3 条消息
……
第 N 次：保存 N 条消息
```

所有 Checkpoint 累计保存的消息条目数量近似：

```text
1 + 2 + 3 + ... + N = N(N+1) / 2
```

因此，在“每轮完整保存持续增长的消息列表”这个**简化模型**中，累计写入规模是 `O(N^2)`。

而真正增长的并不只有 `messages`，还可能包括 `files` 中存放的文本内容。

需要强调：**这是解释存储成本的模型，不等于所有 Checkpointer、所有存储后端都会精确写入这么多字节。** 压缩、去重、Blob 实现等会影响真实成本。

### 7.2 最自然的优化：只记下这一步改了什么

我们已经让工具只提交局部更新：

```python
{"files": {"/shop/refund.py": new_file_data}}
```

那存储时能不能也只保存增量？

例如：

```text
初始快照 S0
   ↓
增量 Δ1：追加 AIMessage
   ↓
增量 Δ2：追加 ToolMessage
   ↓
增量 Δ3：更新 /shop/refund.py
   ↓
增量 Δ4：追加 pytest 失败日志
```

恢复时从旧快照开始，一步步应用更新即可。

这正是下一种机制要解决的问题：

> **在 Runtime 中需要看到完整当前值，但在存储中不必为每一步都复制完整值。**

这时才有必要引入 `DeltaChannel`。

---

## 八、DeltaChannel：保存写入历史，读取时恢复完整值

### 8.1 第一次看 `DeltaChannel`，只记住一件事

普通累积字段强调：

```text
本轮写入 → 立即合并为新字段值
```

`DeltaChannel` 更关注：

```text
本轮写入 → 作为增量记录
读取 / 恢复 → 从快照与增量重建字段值
```

它不是另一个会回答用户的 Agent，也不是新的 Middleware。

**它是 LangGraph 的一种 State Channel，用来组织字段值及其更新的存储和恢复方式。**

### 8.2 为什么 DeltaChannel 的 Reducer 签名不一样？

我们先前看到普通的二元 Reducer：

```python
def append_items(old, new):
    return old + new
```

但 `DeltaChannel` 需要能够把**多次写入作为一批**重放：

```python
# 概念伪代码：DeltaChannel 的批量 Reducer 契约

def bulk_reducer(old_state, writes: list):
    ...
    return new_state
```

原因是：恢复时，Runtime 不一定一条一条按原始调用方式进入 Reducer。为了效率，它可能将多条历史写入合并成一批进行重放。

因此需要满足一种非常重要的性质：**分批应用与整体应用，结果必须一致。**

写成公式：

```text
R(R(S, W_1), W_2) = R(S, W_1 + W_2)
```

其中：

- `S`：某个已知状态。
- `W_1`、`W_2`：两批按顺序发生的写入。
- `R`：批量 Reducer。

这个要求通常称为 *batching-invariant*（分批不变性）。

### 8.3 一个错误 Reducer 会导致什么？

假设开发者写了这样一个 Reducer：

```python
# 错误示例：结果依赖写入分批次数

def bad_reducer(state, writes):
    return state + sum(writes) + 1
```

那么：

```python
bad_reducer(bad_reducer(0, [2]), [3])
# 7

bad_reducer(0, [2, 3])
# 6
```

同一份写入：

```text
2, 3
```

分两次重放得到 `7`，合并成一次重放得到 `6`。

**恢复结果竟然取决于 Runtime 怎样分批！**

对退款任务而言，这会是非常危险的状态错误。

所以 Reducer 的确定性与分批不变性，并不是形式上的数学要求，而是**能否可靠恢复状态的必要条件**。

### 8.4 为什么 `_messages_delta_reducer()` 与 `_file_data_delta_reducer()` 都是批量 Reducer？

回看两份真实源码：

```text
deepagents/_messages_reducer.py
    _messages_delta_reducer(state, writes)

deepagents/middleware/filesystem.py
    _file_data_delta_reducer(left, values)
```

它们都接收**一批写入**，然后按次序合并。

消息合并按 ID 处理；文件合并按路径处理。虽然业务语义不同，却都满足 DeltaChannel 对“按顺序重放写入”的接口需求。

这正好解释了为什么第 4、5 节我们要先研究字段合并规则：**没有正确的 Reducer，就无法安全地建立增量重放机制。**

### 8.5 快照为什么仍然不可少？

如果每次恢复都要从第 1 条历史写入开始重放到第 10 万条，读取成本会逐渐提高。

于是需要在增量之间插入一个完整快照：

```text
S0
 |-- Δ1
 |-- Δ2
 |-- Δ3
 |-- Δ4
 v
S4（完整快照）
 |-- Δ5
 |-- Δ6
 v
恢复时：S4 + Δ5 + Δ6
```

这就是 `snapshot_frequency` 存在的原因。

回到 DeepAgents 固定版本：

```python
# 真实源码中的参数片段
DeltaChannel(_messages_delta_reducer, snapshot_frequency=50)
```

以及：

```python
# 真实源码中的参数片段
DeltaChannel(_file_data_delta_reducer, snapshot_frequency=50)
```

这里的 `50` 是 **Channel 的快照频率配置**，不能机械地解释为“每执行 50 次模型调用就保存一次”，也不能简单宣称“每 50 个 Token”。实际快照条件与 Channel 更新、Runtime 进度和具体实现有关。

`DeltaChannel` 的权衡是：

| 策略 | 写入代价 | 恢复读取代价 |
|---|---|---|
| 每步完整快照 | 随累积值增大 | 读取当前快照较直接 |
| 只存增量、永不快照 | 通常较小 | 历史越长，重放越多 |
| 增量 + 周期快照 | 两者之间折中 | 从最近快照开始重放 |

这也是为什么不能把 `DeltaChannel` 说成“免费的无限压缩”。它节省了重复保存完整值的成本，却引入了历史重放、快照频率和存储格式兼容性等问题。

---

## 九、增量已经能重建 State，为什么还需要 Checkpointer？

到这里，很容易产生误解：

> “既然 DeltaChannel 记录增量了，是否它自己就负责把 Agent 保存到数据库，还能从中断处恢复？”

答案是：**不能把这两层混为一谈。**

`DeltaChannel` 决定一个**状态字段如何管理、聚合及重建更新**。

但我们还缺少一个能够把不同执行时刻的状态、版本与待恢复信息保存起来的基础设施。

这一层叫 **Checkpointer**。

### 9.1 一个具体的中断场景

假设主 Agent 已经走到了：

```text
1. 已读取退款代码
2. 已找到 Decimal 舍入风险
3. 已产生补丁
4. 即将执行 edit_file
5. HITL 审批：等待用户确认
```

审批界面可能在十分钟后才收到用户点击。

如果整个 Graph 的进度只存在某个 Python 函数的局部变量里，那么进程重启后就难以恢复。

真正需要保存的，不只是 `messages` 和 `files` 当前值，还包括：

- 此前哪些步骤已经完成。
- 当前在哪一个执行位置暂停。
- 等待恢复的任务或中断信息。
- 继续执行时应使用的状态版本。

这些信息是**执行快照**，并非简单的文本历史。

### 9.2 Checkpointer 与 DeltaChannel 的分工

```text
                  Graph Runtime
                       |
              计算节点/工具更新
                       |
                       v
                   State Channels
                 /             \
           messages            files
           DeltaChannel        DeltaChannel
                 \             /
                  \           /
                   v         v
                  Checkpointer
                       |
                 持久化 Checkpoint
                       |
              指定 Thread 进行恢复
```

这张图现在才合理，因为我们已经分别理解了：

1. 节点为什么需要提交更新。
2. Reducer 怎样合并更新。
3. DeltaChannel 为什么要记录增量并支持重放。
4. Checkpointer 为什么要保存 Graph 执行进度。

### 9.3 `create_deep_agent()` 怎样接入 Checkpointer？

打开 [`deepagents/graph.py`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py#L920-L933)。

固定版本的函数把调用方的 `checkpointer` 直接传给 `create_agent()`：

```python
# 真实源码中的参数片段
checkpointer=checkpointer,
store=store,
state_schema=state_schema if state_schema is not None else DeepAgentState,
```

这段代码非常重要，因为它回答了“由谁负责持久化”：

- DeepAgents 负责装配 `DeepAgentState`、Middleware 与 Backend。
- LangChain `create_agent()` 负责构建 Agent Graph，并接收 Checkpointer 配置。
- LangGraph Runtime 负责按照图的执行语义读写 Checkpoint。

**DeepAgents 没有在 `create_deep_agent()` 中另写一套检查点数据库。**

---

## 十、Thread：为什么同一个 Agent 可以同时服务多个任务？

### 10.1 一个 Agent 实例不等于只有一份状态

我们可以在应用启动时创建一次：

```python
# 框架级示例：未在当前环境运行
agent = create_deep_agent(
    model=model,
    checkpointer=checkpointer,
)
```

随后可能有两个人同时使用这个 Agent：

```text
用户 A：修复订单退款精度
用户 B：排查库存预留超时
```

如果只用一个全局 `state` 变量，两个会话就可能相互污染。

我们真正需要的是**逻辑执行线程**。

通常通过 `thread_id` 区分：

```python
# 框架级示例：未在当前环境运行
config_a = {"configurable": {"thread_id": "refund-bug-001"}}
config_b = {"configurable": {"thread_id": "inventory-bug-002"}}
```

有 Checkpointer 时，Graph 可以使用相应线程标识组织它的持久化状态。

这里的 Thread **不是操作系统线程**，也不是 `asyncio.Task`。它是用于归属 Graph 执行历史的逻辑标识。

### 10.2 一个最小的接线示例

以下代码用于展示 API 形状，**本章未实际执行框架级实验**：

```python
from deepagents import create_deep_agent
from langgraph.checkpoint.memory import InMemorySaver

# 需要配置可用的模型与相应 API Key。
# 省略 model 的具体初始化。
checkpointer = InMemorySaver()

agent = create_deep_agent(
    model=model,
    checkpointer=checkpointer,
)

config = {"configurable": {"thread_id": "refund-bug-001"}}

result = agent.invoke(
    {
        "messages": [
            {"role": "user", "content": "检查优惠券部分退款精度问题"}
        ]
    },
    config=config,
)

snapshot = agent.get_state(config)
print(snapshot.values.keys())
```

注意两件事：

**第一，`InMemorySaver` 只适合本地实验或有限的内存场景。** 它不会在 Python 进程终止后神奇地保存数据。实际持久化部署需要选取支持对应持久化介质的 Checkpointer。

**第二，`thread_id` 提供状态的执行归属，不是访问控制策略。** 多租户系统还需要单独处理身份验证、授权和 Thread 所属关系。

### 10.3 从已有 Thread 继续，不等于重新执行全部历史

当一个 Thread 已经有 Checkpoint 时，后续对同一个 `thread_id` 的调用能够使用之前的持久化上下文。

但“继续对话”“恢复一次中断”“回放某个历史快照”并不是完全相同的操作：

- **继续对话**：给现有 Thread 提交新的用户消息。
- **恢复中断**：提供被 `interrupt()` 等机制等待的恢复值。
- **历史回放或时间旅行**：按指定 Checkpoint/配置执行，需要考虑重新执行某些步骤的风险。

我们在第十章已经见过中断恢复，接下来把它和 Checkpointer 连接起来。

---

## 十一、从 HITL 中断恢复：恢复的是执行进度，不只是文字

### 11.1 `edit_file` 被批准前，Graph 停在哪里？

例如 Agent 发起：

```text
edit_file("/shop/refund.py", ...)
```

第十章的 `HumanInTheLoopMiddleware` 可能要求先进行审批。

在固定版本的默认 batched 审批模式中，人工审批发生在工具执行之前，Graph 进入中断状态；恢复时，调用方提交审批结果，再让 Graph 继续处理。

概念上的过程为：

```text
主 Agent 生成 edit_file Tool Call
              |
              v
       HITL Middleware
              |
              v
         interrupt()
              |
              v
      Checkpointer 保存进度
              |
       等待人工批准 / 拒绝
              |
              v
      Command(resume=...)
              |
              v
       Runtime 从中断处恢复
              |
              v
    进入工具执行或拒绝路径
```

其中 Checkpointer 保存的是 Graph 执行上下文，不能仅用一条“用户已批准”的普通聊天消息替代。

### 11.2 `Command(resume=...)` 的位置

一个说明恢复接口形状的示例：

```python
# 框架级伪示例：请使用第十章实际中断 payload 构造审批数据
from langgraph.types import Command

resume_input = {
    "decisions": [
        {"type": "approve"},
    ]
}

continued = agent.invoke(
    Command(resume=resume_input),
    config=config,
)
```

**注意：** 这里展示的是第十章 batched 审批路径的典型输入结构；并不意味着所有中断、所有 per-call 工具审批都使用这个 payload。恢复值必须匹配此前暂停的具体中断协议。

### 11.3 Checkpoint 无法代替审批本身

Checkpoint 解决的是：

```text
我停在了哪里、当时有什么 State、后面该如何继续？
```

审批解决的是：

```text
这个有风险的操作是否允许发生？
```

**前者是运行时恢复机制，后者是执行策略。**

混淆两者，会让人误以为配置了 Checkpointer 就具备工具权限管理。事实并非如此。

---

## 十二、State、Checkpoint、Store 与 Backend：看起来都“保存东西”，为什么不是一回事？

现在我们已经亲自遇到了四种存储需求，可以把它们排在一起。

### 12.1 State：当前这次 Agent 执行有哪些数据？

例如：

```python
{
    "messages": [...],
    "files": {...},
    "todos": [...],
}
```

它是 Graph 各节点读取和更新的**逻辑工作状态**。

### 12.2 Checkpointer：如何在执行过程之间保留 Graph 进度？

例如：

```text
refund-bug-001
  checkpoint-1
  checkpoint-2
  checkpoint-3（等待审批）
```

它承担的是**执行历史与恢复**。

### 12.3 Store：需要跨 Thread 使用的持久化数据放哪里？

例如：

```text
团队项目约定
用户长期偏好
复用的工程规范
```

Graph Store 为长期数据提供存储接口，不等于自动把所有 Agent 消息同步成长期记忆。

上一章中子 Agent 可以使用独立的状态；如果希望不同 Thread 共享某类数据，就需要明确设计 Store 的读写方式、命名空间和访问控制。

### 12.4 Backend：文件工具访问的到底是什么存储或执行环境？

例如：

```text
read_file / edit_file
        |
        v
    BackendProtocol
        |
   +----+----------------+
   |                     |
StateBackend       FilesystemBackend / 其他 Backend
   |                     |
State.files          对应的文件系统或环境
```

`StateBackend` 把文件内容放进 `files` State 字段；其他 Backend 可以直接访问主机、沙箱或者路由后的存储系统。

**Backend 决定文件工具访问哪里；Checkpointer 决定 Graph 的执行状态怎样保存。**

两者可能有关联，但不是同一个组件。

### 12.5 对照表

| 机制 | 它主要回答的问题 | 典型作用范围 |
|---|---|---|
| State | 当前任务有什么数据？ | 当前 Graph / Thread 的执行上下文 |
| Reducer / Channel | 多份字段更新怎样合并、重建？ | 某个 State 字段 |
| Checkpointer | 执行进度怎样保存和恢复？ | Thread 中的 Checkpoint 历史 |
| Store | 哪些信息需要跨任务长期保存？ | 显式设计的长期命名空间 |
| Backend | 文件、资源或命令在哪里执行？ | 某个文件或执行环境 |

到这里，我们已经能解释：**为什么这几个名词不能统统归类成 Middleware。**

Middleware 可以声明状态或在生命周期里使用上述能力，但 State、Channel、Checkpointer、Store 和 Backend 各有独立职责。

---

## 十三、一个必要的边界：恢复 Graph 不保证外部动作只发生一次

这是本章最容易被忽略、却非常重要的工程问题。

### 13.1 仍然使用退款 Bug，但换一个危险场景

假设 Agent 不仅修改代码，还能向外部服务提交一次退款测试请求：

```text
Graph 执行步骤 N：
    调用 payment_service.refund(order_id, amount)
```

外部服务已经成功退款，但就在工具返回结果写入 Checkpoint **之前**，应用进程崩溃了。

恢复后，Graph 看到的可能仍然是“这一步尚未完成”。

如果再次调用退款接口：

```text
第一次：真实退款成功
崩溃：执行结果未持久化
恢复：再次请求退款
```

就可能发生重复退款。

**Checkpointer 记录 Graph 状态，不会自动把任意外部系统操作变成 exactly-once（精确一次）。**

### 13.2 真正需要补的是外部副作用控制

一种常见思路，是为危险操作使用稳定的幂等键：

```python
# 概念伪代码：需要外部服务支持幂等能力

def submit_refund(order_id, amount, operation_id):
    return payment_service.refund(
        order_id=order_id,
        amount=amount,
        idempotency_key=operation_id,
    )
```

`operation_id` 必须在重试时保持稳定，不能每次调用重新生成随机值。外部服务还必须正确实现按键去重。

对于文件修改，也要关注：

- 相同补丁重复应用会怎样？
- 基础文件版本是否变化？
- 能否校验内容哈希或版本号？
- 修改和提交测试结果是否需要额外的审计记录？

所以 State 与 Checkpoint 让 Agent **能够恢复计算**，却不天然保证**业务副作用能够安全重演**。

这个边界应该在掌握正常恢复路径后理解，而不是在初次解释 State 时就塞入所有分布式事务术语。

---

## 十四、最小可运行实验：同一批写入，无论分批还是重放都得到相同 State

前面已经说明多个概念，现在需要一次真正可观察的实验。

为了不要求 API Key，也不把模拟结果冒充真实 LangGraph，我们先写一个纯 Python 实验，验证两个最重要的推论：

1. 同 ID 消息按合并规则更新；文件按路径合并和删除。
2. 用不同批次重放同一份有序写入，最终状态保持一致。

本实验**已在本文档生成环境中使用 Python 实际执行并通过断言**。它验证的是合并与重放的设计性质，不证明 LangGraph Checkpointer 的数据库持久化、真实模型调用或中断恢复已经运行。

### 14.1 完整实验代码

保存为 `chapter13_minimal.py`：

```python
"""Chapter 13: pure-Python teaching experiment; not LangGraph's implementation."""
from copy import deepcopy


def merge_messages(current, batches):
    """A simplified deterministic ID-based bulk reducer."""
    result = [dict(x) for x in current]
    for batch in batches:
        for msg in batch:
            positions = {old['id']: i for i, old in enumerate(result)}
            if msg.get('op') == 'remove':
                if msg['id'] in positions:
                    result.pop(positions[msg['id']])
            elif msg['id'] in positions:
                result[positions[msg['id']]] = dict(msg)
            else:
                result.append(dict(msg))
    return result


def merge_files(current, batches):
    """Apply incremental path writes; None denotes deletion."""
    result = dict(current)
    for batch in batches:
        for path, content in batch.items():
            if content is None:
                result.pop(path, None)
            else:
                result[path] = content
    return result


messages_seed = [{'id': 'h1', 'role': 'human', 'content': '修复退款精度'}]
message_writes = [
    [{'id': 'a1', 'role': 'assistant', 'content': '读取 refund.py'}],
    [{'id': 't1', 'role': 'tool', 'content': '旧实现：float'}],
    [{'id': 't1', 'role': 'tool', 'content': '旧实现：float；存在舍入风险'}],
]
final_messages = merge_messages(messages_seed, message_writes)
assert [m['id'] for m in final_messages] == ['h1', 'a1', 't1']
assert '舍入风险' in final_messages[-1]['content']
assert merge_messages(merge_messages(messages_seed, message_writes[:1]), message_writes[1:]) == final_messages

files_seed = {'/shop/refund.py': 'amount = float(value)'}
file_writes = [
    {'/shop/refund.py': 'amount = Decimal(value)'},
    {'/shop/tests/test_refund.py': 'def test_partial_refund(): pass'},
    {'/shop/tests/test_refund.py': None},
]
final_files = merge_files(files_seed, file_writes)
assert final_files == {'/shop/refund.py': 'amount = Decimal(value)'}
assert merge_files(merge_files(files_seed, file_writes[:2]), file_writes[2:]) == final_files

# Teaching-only checkpoint idea: persist a seed plus ordered writes,
# then reconstruct with the same deterministic reducers.
saved = deepcopy({'messages_seed': messages_seed, 'messages_writes': message_writes,
                  'files_seed': files_seed, 'files_writes': file_writes})
replayed_messages = merge_messages(saved['messages_seed'], saved['messages_writes'])
replayed_files = merge_files(saved['files_seed'], saved['files_writes'])
assert replayed_messages == final_messages
assert replayed_files == final_files

print('messages:', [m['id'] for m in replayed_messages])
print('files:', sorted(replayed_files))
print('batch invariant: PASS')
print('replay: PASS')
```

执行：

```bash
python chapter13_minimal.py
```

### 14.2 实际运行结果

```text
messages: ['h1', 'a1', 't1']
files: ['/shop/refund.py']
batch invariant: PASS
replay: PASS
```

这个输出可以逐项解释：

- `messages` 只有一个 `t1`，说明同 ID 更新替换了旧消息。
- `files` 只剩 `/shop/refund.py`，说明新建测试文件后又通过 `None` 删除了它。
- `batch invariant: PASS` 说明这里的两种 Reducer 在测试输入下满足分批合并与整体合并结果一致。
- `replay: PASS` 说明用保存的初始值与有序写入可以重建相同结果。

但必须明确：**这里的 `saved` 只是纯 Python 数据拷贝，不是 LangGraph Checkpoint。** 它没有实现 Thread ID、持久化存储、Graph 调度、中断恢复或 DeltaChannel 的真实序列化机制。

### 14.3 将实验升级到真实框架需要验证什么？

若在安装 `deepagents==0.7.32` 且配置兼容 LangGraph 的环境中继续，可以安排以下验证：

1. 创建 `StateGraph`，为两个字段分别配置 Reducer，观察 `invoke()` 输出。
2. 使用 `create_deep_agent()` 和 `InMemorySaver`，在同一 `thread_id` 上多次调用并读取 `get_state()`。
3. 使用真实 `StateBackend` 写入 `files`，确认下一轮工具能够读取更新后的文本。
4. 配置 HITL 并触发一次真实 `interrupt()`，再使用匹配的 `Command(resume=...)` 恢复。
5. 选用支持持久化的 Checkpointer，测试进程重启后能否重载同一 Thread。

这些属于**后续真实框架集成实验**；本文没有把它们标成“已经通过”。

---

## 十五、回看整条演化路线：从 Python 字典到可恢复 Agent

现在再看本章的所有设计，就不会觉得它们是突然堆在一起的术语了。

### 第一步：一个普通变量已经能维持最小 Loop

```text
messages = []
files = {}
```

当一切顺序执行、不中断时，它足够简单。

### 第二步：Graph 中多个节点可能同时提交更新

仅靠“最后一次赋值覆盖旧值”不够，于是需要：

```text
Node → Partial State Update
```

### 第三步：不同字段需要不同的合并语义

消息按 ID 合并；文件按路径合并；普通状态字段在不冲突时可以直接覆盖。

于是引入：

```text
Reducer / Channel
```

### 第四步：字段持续增长，完整 Checkpoint 产生重复成本

仅仅把当前值合并正确还不够，于是需要保存增量，并在读取时重建。

于是引入：

```text
DeltaChannel + 周期快照
```

### 第五步：Agent 需要暂停并在之后继续

只有 State 值而没有执行位置，无法完整描述“恢复”。

于是引入：

```text
Checkpointer + Thread
```

### 第六步：外部动作可能发生在 Checkpoint 之外

即使能恢复 Graph，也不能直接保证退款、文件写入等外部副作用精确一次。

因此仍然需要：

```text
权限、版本校验、幂等操作与业务级防重
```

现在可以画出完整、但每个元素都已被问题引出的架构：

```text
用户：修复退款精度 Bug
          |
          v
      Agent Graph
          |
     Model / Tools
          |
          v
   Partial State Update
          |
          v
  +-----------------------+
  | State Channels        |
  |                       |
  | messages              |
  |   ID 合并 Reducer     |
  |   DeltaChannel        |
  |                       |
  | files                 |
  |   路径合并 Reducer    |
  |   DeltaChannel        |
  |                       |
  | todo / 其他字段       |
  +-----------------------+
          |
          v
     Checkpointer
          |
    Thread / Checkpoint
          |
   中断 / 恢复 / 历史

另一条独立路径：

File Tools → Backend → StateBackend 或外部文件环境

长期数据：

Store → 跨 Thread 显式共享的数据
```

### 15.1 本章必须真正掌握的五个结论

1. **State 不是必须由一个全局可变字典手工维护。** LangGraph 节点通过更新值协作，字段 Channel 负责组织这些更新。
2. **Reducer 是字段级语义。** 消息和文件虽然都属于 Agent State，却分别采用 ID 合并和路径合并。
3. **DeltaChannel 解决的是增长型 State 的增量存储与重放问题。** 它要求 Reducer 对写入批次保持确定性与分批不变性。
4. **Checkpointer 负责执行状态的持久化与恢复。** 它与 DeltaChannel、Backend、Store 不能互相替代。
5. **能恢复执行不等于副作用安全重演。** 涉及真实退款、代码覆盖或其他不可逆操作时，仍要设计幂等性和权限边界。

---

## 十六、自然进入下一章：状态已经可靠，怎样看见执行过程？

到这里，退款修复 Agent 已经具有：

```text
会调用工具
会规划
能委派子任务
能暂停并审批
能保存和恢复 State
```

但是如果它运行了五十轮，我们还会遇到新的问题：

> 用户怎样知道它正在读取哪个文件、模型发起了哪个工具调用、哪一步产生了状态更新、为什么卡在某个节点，以及如何区分模型 Token 输出和 Graph 执行事件？

这不是再增加一套状态存储就能解决的问题。我们需要观察执行过程。

**下一章：第十四章——Agent 执行过程怎样被观察？Streaming、Tracing 与 Debugging。**

---

## 附：源码阅读顺序与自测题

建议按下列顺序打开源码，不要一口气阅读 LangGraph 全部 Runtime：

1. [`DeepAgentState`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py#L73-L76)：先看 `messages` 字段怎样声明。
2. [`_messages_delta_reducer()`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/_messages_reducer.py#L28-L83)：研究相同 ID、`RemoveMessage` 和清空标记。
3. [`FilesystemState`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L1170-L1193)：研究文件按路径合并。
4. [`StateBackend`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/backends/state.py#L75-L109)：追踪写入怎样进入 `files` Channel。
5. [`create_deep_agent()` 返回处](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py#L920-L943)：确认 `state_schema`、`checkpointer`、`store` 传给了谁。
6. [`DeltaChannel`](https://github.com/langchain-ai/langgraph/blob/main/libs/langgraph/langgraph/channels/delta.py)：理解为什么要保存增量、何时使用快照以及为何要求分批不变性。

读完后，不看正文，试着回答：

- 如果两个节点在同一轮都返回 `{"messages": [...]}`，为什么不能仅靠普通赋值？
- 如果 ToolMessage 有相同 ID，DeepAgents 为什么不是简单 append？
- `edit_file` 使用 `StateBackend` 时，文件究竟存在哪里？
- 为什么 DeltaChannel Reducer 的第二个参数是一批写入，而不是一个值？
- `snapshot_frequency=50` 为什么不等于每 50 次 LLM 调用？
- 为什么配置了 Checkpointer，还可能发生重复外部退款？

如果这些问题都能沿着真实源码和运行路径说明，而不是只背术语，就已经理解了本章的核心。
