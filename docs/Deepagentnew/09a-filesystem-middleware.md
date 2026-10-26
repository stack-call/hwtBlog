---
title: 第九章附：FilesystemMiddleware 到底在做什么？——State、八种工具、两个 Hook 与 Backend 的分工
description: 沿 DeepAgents 0.7.32 的 filesystem.py（3909 行）逐段拆解 FilesystemMiddleware：为什么它需要两个 state schema、DeltaChannel 的批量 reducer 与单次版本差在哪、tools= 白名单为什么让工具根本不生成、八种工具共用的三段骨架、wrap_tool_call 的三件事和它明确不碰的七个工具、wrap_model_call 的五个步骤，以及 permissions 在什么情况下直接拒绝初始化
tags: [DeepAgents, FilesystemMiddleware, Backend, DeltaChannel, Tool Eviction, Permissions, Middleware, 源码精读]
sidebar_position: 12
---

# 第九章附：FilesystemMiddleware 到底在做什么？

> **本章要回答的问题：** 第九章已经走通了一条 `edit_file` 调用：ToolNode 分发、Middleware 校验、Backend 落盘、`ToolMessage` 回流。但那一章只用了这个 Middleware 的很小一部分。它的源文件有 3909 行，而主流程真正调用的部分不到一百行。剩下的三千八百行在做什么？更根本的问题是——**这些事为什么不能写成八个普通工具函数，而必须是一个 Middleware？**

## 一、先把「Middleware」这个词拆开

第九章第五节说过一句话：`FilesystemMiddleware` 是「模型工具名」与「Backend 存储」之间的翻译层。

这个说法对，但不够用。因为「翻译」只需要八个函数，不需要 3909 行，也不需要 `state_schema`、`wrap_model_call`、`wrap_tool_call` 这些只有 Middleware 才有的东西。

要看清它为什么必须是 Middleware，得先把它承担的职责列全。

| 职责 | 具体内容 | 普通工具函数能做吗 |
|---|---|---|
| **提供工具** | 八个可调用对象，名字固定 | 能 |
| **扩展图状态** | 给 State 加 `files` 通道，并指定 reducer | **不能** |
| **按实例配置** | 同一个 `write_file`，在不同 Agent 上行为不同 | 能（但很别扭） |
| **拦截工具执行** | 在工具真正跑之前检查、之后改写结果 | **不能** |
| **拦截模型请求** | 在模型看到消息之前筛工具、缩消息 | **不能** |

最后三行是决定性的。

- **扩展图状态**：一个普通函数无法往 `StateGraph` 里加一个带自定义 reducer 的通道。只有 Middleware 的 `state_schema` 能在建图阶段做到这件事。
- **拦截工具执行**：一个工具只能管自己。`write_file` 无法知道「同一批 tool_calls 里还有谁也在改这个路径」，因为它在被调用的那一刻，只看得到自己的参数。
- **拦截模型请求**：工具根本不在模型调用的路径上。它只能被动等待被调用，不能主动修改「模型这一轮看到什么」。

所以更准确的说法是：

> **八个工具是它「对外」的全部；Middleware 这个身份，是它「对内」的全部。**

对外，模型只看见 `read_file`、`write_file` 这些名字。对内，它还要维护一个状态通道、按 Backend 能力裁剪工具列表、在工具边界上做冲突检查、在模型边界上压缩消息。这些东西没有一样能靠「多写几个函数」实现，因为它们分别发生在**建图阶段**、**工具执行阶段**、**模型调用阶段**——三个普通工具函数够不着的地方。

### 1.1 阅读版本及源码边界

本章固定：

```text
DeepAgents Python: 0.7.32
DeepAgents Commit: e85c6b9
LangChain:          承接前文研究的 1.4.7 版本
```

本章主要核查以下固定源码：

- [`deepagents/middleware/filesystem.py`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py)（3909 行）
- [`deepagents/middleware/_message_eviction.py`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/_message_eviction.py)（295 行，卸载后的占位文本在这里）

本章所有带「**真实源码节选**」的代码均可在所附固定链接中定位；允许省略不影响所讨论机制的语句，但不改变分支的核心语义。带「**教学化简**」和「**概念伪代码**」的片段用于推导，不冒充仓库原文。

### 1.2 一张全局地图：四个时间点

这个 Middleware 的代码之所以显得长，一个重要原因是它同时活在四个不同的时间点上。把这四个时间点分开，3909 行就变成了四段。

```text
create_deep_agent(...)
        |
        |  ① 装配时：配置落位
        +---------------------------------------------+
        |  FilesystemMiddleware.__init__               |
        |    backend / permissions / limits            |
        |    tools = self._create_tools()   ← 工具在此生成 |
        +---------------------------------------------+
        |
        |  ② 建图时：schema 落位
        +---------------------------------------------+
        |  state_schema = FilesystemState (或 _BlobPayloadState) |
        +---------------------------------------------+
        |
        |  ③ 每次模型调用前
        +---------------------------------------------+
        |  wrap_model_call                             |
        |    筛工具 / 拼 Prompt / 缩消息 / 处理二进制      |
        +---------------------------------------------+
        |
        |  ④ 每次工具执行时
        +---------------------------------------------+
        |  wrap_tool_call                              |
        |    冲突检查 / 卸载大结果                       |
        +---------------------------------------------+
```

| 时间点 | 发生次数 | 对应代码 |
|---|---|---|
| ① 装配 | 每个 Agent 一次 | `__init__`、`_create_tools` |
| ② 建图 | 每个 Agent 一次 | `state_schema` 类属性 |
| ③ 模型调用前 | 每轮可能几十次 | `wrap_model_call` |
| ④ 工具执行时 | 每个 tool_call 一次 | `wrap_tool_call` |

后面几节就按这个顺序展开。第一节先讲 ②，因为它是「为什么必须是 Middleware」最硬的证据。

---

## 二、State：为什么有两个 schema

### 2.1 真实源码

**真实源码节选：** [`filesystem.py#L1298-L1313`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L1298-L1313)

```python
class FilesystemState(AgentState):
    """State for the filesystem middleware."""

    files: Annotated[NotRequired[dict[str, FileData]], DeltaChannel(_file_data_delta_reducer, snapshot_frequency=50)]  # ty: ignore[invalid-argument-type]
    """Files in the filesystem. Uses DeltaChannel with snapshots every ~50 pregel steps to bound read depth."""


class _BlobPayloadState(AgentState):
    """State for `offload_binary_content`."""

    _blob_payloads: NotRequired[Annotated[dict[str, str], UntrackedValue(dict, guard=False), PrivateStateAttr]]
    """Payloads of offloaded blobs loaded in this run; never checkpointed."""


class _FilesystemBlobPayloadState(FilesystemState, _BlobPayloadState):
    """`FilesystemState` with `_BlobPayloadState`."""
```

三个类，三层关系。注意第二个类的名字带下划线——它是**私有的**，不在对外 API 里。

逐层看：

**`FilesystemState`** 只有一个新增字段 `files`。`NotRequired` 表示这个字段可以不存在（第一次运行时 State 里本来就没有它）；`Annotated` 把类型和「合并规则」绑在一起；`DeltaChannel(...)` 就是那个合并规则的载体。

**`_BlobPayloadState`** 只有一个新增字段 `_blob_payloads`，而且它是运行期的，不进 Checkpoint（下一节细说）。

**`_FilesystemBlobPayloadState`** 用继承把两者合起来——既保留 `files` 通道，也保留 `_blob_payloads`。

### 2.2 `DeltaChannel` 与那个 reducer 的真实签名

一种常见的简化写法是这样的：

```python
# 常见的简化写法（与真实签名不同）
def _file_data_delta_reducer(left, values):
    result = dict(left) if left else {}
    for writes in values:
        for path, value in writes.items():
            ...
```

这段逻辑是对的，但它省略了一个关键点。真实的函数签名是：

**真实源码节选：** [`filesystem.py#L1277-L1296`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L1277-L1296)

```python
def _file_data_delta_reducer(
    left: dict[str, FileData] | None,
    values: list[dict[str, FileData | None]],
) -> dict[str, FileData]:
    """Batch reducer for use with DeltaChannel.

    `DeltaChannel` calls `reducer(base, list(values))` where values is a list of
    all writes in the current step.

    Single dict copy + one pass over all writes.
    """
    result: dict[str, FileData] = dict(left) if left else {}
    for writes in values:
        for key, value in writes.items():
            if value is None:
                result.pop(key, None)
            else:
                result[key] = value
    return result
```

第二个参数是 `list[...]`，不是单个字典。docstring 直接写了原因：**`DeltaChannel` 会把当前步骤里的所有写入收集成一个列表，一次交给 reducer。**

这不是无关紧要的类型差异。它决定了复杂度：

| 写法 | 每步的字典拷贝次数 | 适用场景 |
|---|---|---|
| `reducer(left, right)` | 每次写入一次 | 一步只写一次 |
| `reducer(left, list_of_writes)` | **每步一次** | 一步可能写很多次 |

假设模型一次返回十条 `write_file` 调用，ToolNode 并行执行它们。用单次签名，每一条都要复制一遍 `left` 再合并，十次拷贝；用批量签名，只复制一次，然后遍历十个写入。

docstring 里那句 `Single dict copy + one pass over all writes` 就是在说这件事。

### 2.3 删除用 `None` 表示

`if value is None: result.pop(key, None)` 这一行承担了删除语义。

为什么不能靠「不写这个 key」来表示删除？因为在增量合并里，「没提到」和「被删掉」是两件完全不同的事：

- 没提到 → 保留原值。
- 想删掉 → 必须显式给一个信号。

`None` 就是这个信号。字典里出现 `"/a.txt": None`，表示「把 `/a.txt` 从结果里去掉」。这也是为什么类型写成 `dict[str, FileData | None]`——value 允许是 `None`。

顺带一个观察：同一个文件里还留着一个**单次版本**的 `_file_data_reducer`（[L1241](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L1241)），签名是 `(left, right)`，逻辑与批量版一致。它在本模块内**没有任何调用点**（`files` 通道用的是 `_file_data_delta_reducer`）。这大概率是迁移到 `DeltaChannel` 时保留下来的旧版本，从注释和命名可以看出两者的传承关系。

### 2.4 `_blob_payloads`：一个不进 Checkpoint 的字段

这个字段是三个修饰的组合：

```python
NotRequired[Annotated[dict[str, str], UntrackedValue(dict, guard=False), PrivateStateAttr]]
```

| 修饰 | 作用 |
|---|---|
| `NotRequired` | 可以不出现 |
| `UntrackedValue(dict, guard=False)` | 不参与 Checkpoint，且关闭该通道的并发更新检查 |
| `PrivateStateAttr` | 标记为内部字段，不进对外暴露的 State |

docstring 只有一句，但信息量很足：**`never checkpointed`**（从不写入 Checkpoint）。

为什么需要这样一个字段？因为「二进制内容卸载」这件事的本质是**把大块数据移出 Checkpoint**。如果卸载后的数据又落进 Checkpoint，那这次卸载就白做了。

所以链路是这样的：

```text
大块二进制（图片 / 音频 / 视频）
        |
        |  offload
        v
Backend 里的 blobs/ 文件        ← 持久，但不在 Checkpoint 里
        |
        |  运行期需要时恢复
        v
_blob_payloads（内存）          ← 只在这次运行有效
        |
        |  下次模型调用
        v
重新从 Backend 读
```

`_blob_payloads` 是这条链上的**内存缓存**，不是存储。它存在的唯一理由，是避免同一次运行里对同一份 blob 反复做 Backend 读取。

> [!NOTE] 关于 `guard=False` 的理解边界
> `UntrackedValue` 的 `guard` 参数控制该通道是否做并发更新检查。`guard=False` 意味着这个通道把并发时序的保证交给了写入方（这里是 Middleware 自己）。本章只用到「它不做框架级的并发拦截」这一层含义，不进一步推断框架内部的实现细节。

### 2.5 `_uses_state_backend`：schema 为什么要按 Backend 切换

`state_schema` 不是写死的类，而是在 `__init__` 里算出来的：

**真实源码节选：** [`filesystem.py#L1987-L1990`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L1987-L1990)

```python
self.state_schema = cast(
    "type[FilesystemState]",
    _FilesystemBlobPayloadState if _uses_state_backend(self.backend) else _BlobPayloadState,
)
```

判断函数：

**真实源码节选：** [`filesystem.py#L1316-L1322`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L1316-L1322)

```python
def _uses_state_backend(backend: BackendProtocol) -> bool:
    """Return whether a backend stores any files in agent state."""
    if isinstance(backend, StateBackend):
        return True
    if not isinstance(backend, CompositeBackend):
        return False
    return _uses_state_backend(backend.default) or any(_uses_state_backend(route) for route in backend.routes.values())
```

注意它是一个**递归**判断，而且对 `CompositeBackend` 用的是 `or`：

- 默认 Backend 是 `StateBackend` → 需要 `files`。
- 任意一条路由是 `StateBackend` → 也需要 `files`。
- 只有全部都是外部存储 → 才不需要。

为什么要递归？因为 `CompositeBackend` 可以把 `/memories/` 路由到 `StoreBackend`，把其余路径留在 `StateBackend`。这时候只有一部分文件在 State 里，但 `files` 通道仍然必须有——否则那部分文件无处可放。

这里有一种常见的简化说法需要修正。那种说法是：

> 完全使用外部存储 Backend → 通常不需要

更准确的说法是：**`files` 通道不需要了，但 schema 并没有变成「没有额外字段」。** 它变成 `_BlobPayloadState`，仍然多一个 `_blob_payloads`。

| Backend 情况 | `state_schema` | 是否有 `files` | 是否有 `_blob_payloads` |
|---|---|---|---|
| `StateBackend` | `_FilesystemBlobPayloadState` | 有 | 有 |
| `CompositeBackend`（含 State 路由） | `_FilesystemBlobPayloadState` | 有 | 有 |
| 纯外部存储 | `_BlobPayloadState` | **无** | 有 |

所以「切换 schema」不是在「有字段」和「没字段」之间切换，而是在「两个字段」和「一个字段」之间切换。

### 2.6 两个 schema 都继承 `AgentState`

这一点容易被略过，但它很重要。

`_BlobPayloadState` 的定义是 `class _BlobPayloadState(AgentState)`——它继承 `AgentState`，不是凭空造一个新 State。原因是 Middleware 的 `state_schema` 会**替换**整个图的 State 定义，而不是叠加。如果不继承 `AgentState`，`messages` 这个基础通道就没了，整个 Agent Loop 会立刻失效。

于是继承关系构成了一个「最小公倍数」：

```text
AgentState                    ← messages 等基础通道
    ├── FilesystemState       ← + files
    └── _BlobPayloadState     ← + _blob_payloads

_FilesystemBlobPayloadState   ← + files + _blob_payloads
```

这也解释了为什么 `_FilesystemBlobPayloadState` 要写成多继承：它需要同时拿到两条支线上的字段。

---

## 三、`__init__`：十一个参数，四类决定

### 3.1 真实签名

**真实源码节选：** [`filesystem.py#L1892-L1903`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L1892-L1903)

```python
def __init__(
    self,
    *,
    backend: BackendProtocol | None = None,
    system_prompt: str | None = None,
    custom_tool_descriptions: Mapping[str, str] | None = None,
    tool_token_limit_before_evict: int | None = 20000,
    human_message_token_limit_before_evict: int | None = 50000,
    max_execute_timeout: int = 3600,
    grep_max_count: int | None = 1000,
    tools: list[FsToolName] | Literal["all"] | None = None,
    offload_binary_content: bool = False,
    permissions: list[FilesystemPermission] | None = None,
    _permissions: list[FilesystemPermission] | None = None,
) -> None:
```

全部是 keyword-only（`*` 在第一个参数前）。默认值一览：

| 参数 | 默认值 | 含义 |
|---|---|---|
| `backend` | `None` → `StateBackend()` | 文件存在哪 |
| `system_prompt` | `None` | 调用方追加的文件系统说明 |
| `custom_tool_descriptions` | `None` | 覆盖默认工具描述 |
| `tool_token_limit_before_evict` | `20000` | 工具结果超过这个 token 数就卸载 |
| `human_message_token_limit_before_evict` | `50000` | 用户消息超过这个 token 数就卸载 |
| `max_execute_timeout` | `3600` | `execute` 的单次超时上限（秒） |
| `grep_max_count` | `1000` | `grep` 返回条数上限 |
| `tools` | `None` | 启用哪些工具（`None` = 全部） |
| `offload_binary_content` | `False` | 是否把二进制内容卸载到 Backend |
| `permissions` | `None` | 文件访问规则 |
| `_permissions` | `None` | 已弃用的旧名 |

「删去配置校验和其他选项后的主线」——这种简化省略掉的，恰好是这一节最有信息量的部分。逐项看。

### 3.2 四条硬校验

**真实源码节选：** [`filesystem.py#L1957-L1977`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L1957-L1977)

```python
if permissions is not None and _permissions is not None:
    msg = "Pass only permissions, not both permissions and _permissions."
    raise ValueError(msg)
# （此处省略 0.7.31 的 `_permissions` 弃用分支，见 3.4 节）
if isinstance(tools, list) and "read_file" not in tools:
    msg = "read_file must be included in tools; it is required by FilesystemMiddleware"
    raise ValueError(msg)
if max_execute_timeout <= 0:
    msg = f"max_execute_timeout must be positive, got {max_execute_timeout}"
    raise ValueError(msg)
if grep_max_count is not None and grep_max_count <= 0:
    msg = f"grep_max_count must be positive or None, got {grep_max_count}"
    raise ValueError(msg)
```

四条校验，性质各不相同。

**第一条**：新旧参数不能同时传。这不是兼容性洁癖——如果两个都传，到底用哪个？源码选择直接拒绝，避免「看起来生效了，其实用的是另一个」。

**第二条**：`read_file` 是**唯一一个不能从 `tools=` 里排除**的工具。理由可以从别处推出来：这个 Middleware 的所有卸载机制，最终都靠 `read_file` 把内容读回来。

看一下卸载后的占位文本（[`_message_eviction.py#L31-L38`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/_message_eviction.py#L31-L38)）：

```text
Tool result too large, the result of this tool call {tool_call_id} was saved in the filesystem at this path: {file_path}

You can read the result from the filesystem by using the read_file tool, but make sure to only read part of the result at a time.

You can do this by specifying an offset and limit in the read_file tool call. For example, to read the first 100 lines, you can use the read_file tool with offset=0 and limit=100.
```

这段提示词明确让模型「用 `read_file` 读回来」。如果 `read_file` 不存在，卸载就等于把数据丢进了一个没人能打开的地方。所以这不是偏好问题，是**机制依赖**。

**第三条、第四条**：数值必须为正。`grep_max_count` 额外允许 `None`——表示不限条数。

### 3.3 `backend`：默认值，以及一个被移除的旧用法

**真实源码节选：** [`filesystem.py#L1979-L1986`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L1979-L1986)

```python
self.backend = backend if backend is not None else StateBackend()
if callable(self.backend) and not isinstance(self.backend, BackendProtocol):
    msg = (
        "backend must be an initialized backend instance. Backend factories "
        "were removed in deepagents 0.7; pass StateBackend(), "
        "CompositeBackend(...), or another BackendProtocol instance instead."
    )
    raise TypeError(msg)
```

两件事：

1. **不传 Backend 就得到 `StateBackend()`。** 也就是说，默认情况下文件存在 Graph State 里，不碰磁盘。这个默认值的选择本身就是一个态度：先让 Agent 能读写文件，但不让它有能力碰真实项目。
2. **Backend 工厂被移除了。** 旧版本允许传一个函数（每次调用时新建 Backend），0.7 之后不行了。源码用一个 `callable` 检查兜住这种写法，并给出一条明确的迁移提示。

第二条值得留意：它说明「Backend 的生命周期」在这套设计里是**属于 Agent 的**，不是属于某次调用的。一个 Agent 有一个 Backend 实例，工具都闭包在这个实例上。

### 3.4 `permissions` 与 `_permissions`

**真实源码节选：** [`filesystem.py#L1960-L1968`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L1960-L1968)

```python
if _permissions is not None:
    warn_deprecated(
        since="0.7.31",
        removal="0.9.0",
        name="FilesystemMiddleware(_permissions=...)",
        alternative="FilesystemMiddleware(permissions=...)",
        package="deepagents",
    )
    permissions = _permissions
```

旧名 `_permissions` 在 0.7.31 被标记弃用，计划 0.9.0 移除。这解释了为什么类内部一直用 `self._permissions`：**内部的私有字段名没变，变的只是构造参数名。** 读源码时看到 `self._permissions`，不要误以为它是那个弃用参数。

### 3.5 三个路径前缀

**真实源码节选：** [`filesystem.py#L1993-L1997`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L1993-L1997)

```python
artifacts_root = self.backend.artifacts_root if isinstance(self.backend, CompositeBackend) else "/"
_root = artifacts_root.rstrip("/")
self._large_tool_results_prefix = f"{_root}/large_tool_results"
self._conversation_history_prefix = f"{_root}/conversation_history"
self._blobs_prefix = f"{_root}/blobs"
```

三个前缀，对应三种「被卸载的东西」：

| 前缀 | 存什么 | 谁写进去的 |
|---|---|---|
| `/large_tool_results` | 超大的工具结果全文 | `wrap_tool_call` |
| `/conversation_history` | 超大的用户消息全文 | `wrap_model_call` |
| `/blobs` | 卸载的二进制内容 | `wrap_model_call` / `wrap_tool_call` |

`artifacts_root` 只在 `CompositeBackend` 上存在，作用是把这些「中间产物」统一放到某个路由下（比如 `/artifacts/`），避免和用户文件混在一起。非 Composite 的情况下根就是 `/`。

这是一个容易被忽略的设计细节：**卸载不是随便找个地方扔，而是有固定的三个目录。** 因为模型需要能猜出「我的完整结果在哪」，而固定前缀让提示词可以只说一次。

### 3.6 `offload_binary_content` 的自动降级

**真实源码节选：** [`filesystem.py#L1998-L2003`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L1998-L2003)

```python
if offload_binary_content and _routes_to_state_backend(self.backend, f"{self._blobs_prefix}/"):
    msg = f"`offload_binary_content` has no effect: `{self._blobs_prefix}/` routes to a `StateBackend`, which keeps files in checkpointed state."
    warnings.warn(msg, stacklevel=2)
    offload_binary_content = False
self._offload_binary_content = offload_binary_content
```

这段代码做了一件很值得学的事：**它没有报错，而是把配置降级并警告。**

原因在消息里写得很清楚：`offload_binary_content` 的目的是把大块二进制移出 Checkpoint。如果 `blobs/` 路由到 `StateBackend`，那些「卸载后」的文件反而进了 Checkpoint——目的被彻底抵消。

这时候有两种处理方式：

| 方式 | 后果 |
|---|---|
| 抛异常 | 用户必须改配置才能启动 |
| 降级 + 警告 | 功能退化成「不卸载」，但 Agent 能跑 |

源码选了第二种。理由是：`offload_binary_content=True` 加上 `StateBackend`，并不产生**错误**，只是产生**无效果**。这和一个非法参数不同——非法参数会让程序处于未定义状态，无效果的开关只是浪费了一个配置。

`_routes_to_state_backend` 和 `_uses_state_backend` 的区别也值得记一下：

| 函数 | 判断对象 | 用途 |
|---|---|---|
| `_uses_state_backend` | 整个 Backend 是否**含** State 存储 | 决定要不要 `files` 通道 |
| `_routes_to_state_backend` | 某个**具体路径**是否路由到 State | 决定某个功能是否有效 |

前者是全局的，后者是按路径的。所以「有 State 路由」和「`blobs/` 落在 State 上」是两个不同的问题——后者才是这段代码关心的。

### 3.7 `tools=`：被排除的工具根本不生成

这是一个在简化版里通常被略过、但实际很关键的参数。

**真实源码节选：** [`filesystem.py#L2029-L2044`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L2029-L2044)

```python
def _create_tools(self) -> list[BaseTool]:
    """Bind enabled filesystem tools to this middleware instance."""
    tool_factories: tuple[tuple[str, Callable[[], BaseTool]], ...] = (
        ("ls", self._create_ls_tool),
        ("read_file", self._create_read_file_tool),
        ("write_file", self._create_write_file_tool),
        ("edit_file", self._create_edit_file_tool),
        ("delete", self._create_delete_tool),
        ("glob", self._create_glob_tool),
        ("grep", self._create_grep_tool),
        ("execute", self._create_execute_tool),
    )
    # Excluded tools are omitted here entirely, not just hidden from the
    # model's schema, so a tool name outside `tools=` never reaches the
    # dispatchable tool node
    return [factory() for name, factory in tool_factories if self._enabled_tools is None or name in self._enabled_tools]
```

那行注释是整个 `tools=` 机制的核心：

> Excluded tools are omitted here entirely, not just hidden from the model's schema, so a tool name outside `tools=` never reaches the dispatchable tool node.

翻译成一句话：**排除发生在「生成」阶段，不是「展示」阶段。**

这两者差别很大：

| 排除方式 | 模型看不见 | 模型伪造调用能执行吗 |
|---|---|---|
| 只从 schema 里隐藏 | 是 | **能**（ToolNode 仍认识这个名字） |
| 根本不生成 | 是 | **不能**（ToolNode 里没有这个工具） |

如果只是在模型请求里过滤掉 `delete`，模型仍然可以生成一个 `delete` 的 tool_call（比如从历史里学到的，或者被提示词诱导）。ToolNode 会按名字查找，如果工具还在注册表里，它就真的会执行。

所以源码选择了「不生成」。这也意味着 `_create_tools` 的产物是**唯一的真相来源**：`self.tools` 里没有的名字，在这个 Agent 里就不存在。

三种取值的语义：

| `tools=` | `self._enabled_tools` | 效果 |
|---|---|---|
| `None` | `None` | 全部八种 |
| `"all"` | `frozenset(全部八种)` | 全部八种 |
| `["read_file", "write_file"]` | 对应的 frozenset | 只有这两种 |

`None` 和 `"all"` 效果相同，但内部表示不同。这个差别在别处有用：`_enabled_tools is not None` 表示「用户明确指定过」，而 `None` 表示「用户没管」。`_create_execute_tool` 里就用了这个判断来决定工具描述里该不该提 `grep` / `glob`（第 4.5 节）。

---

## 四、八种工具，和它们的共同骨架

### 4.1 清单与能力门槛

**真实源码节选：** [`filesystem.py#L1608-L1612`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L1608-L1612)

```python
FsToolName = Literal["ls", "read_file", "write_file", "edit_file", "delete", "glob", "grep", "execute"]
"""Names of the built-in filesystem tools that can be passed to `FilesystemMiddleware(tools=...)`."""

_FS_TOOL_ORDER: tuple[str, ...] = ("ls", "read_file", "write_file", "edit_file", "delete", "glob", "grep")
_ALL_FS_TOOL_NAMES: frozenset[str] = frozenset(_FS_TOOL_ORDER) | {"execute"}
```

八种工具，分成三组：

| 组 | 工具 | 共同点 |
|---|---|---|
| 读 | `ls`、`read_file`、`glob`、`grep` | 不产生副作用 |
| 写 | `write_file`、`edit_file`、`delete` | 产生副作用，且是并行冲突检查的对象 |
| 执行 | `execute` | 需要 Backend 支持命令执行 |

`execute` 被单独拿出来放进 `_ALL_FS_TOOL_NAMES`，因为它不在 `_FS_TOOL_ORDER` 里——这个顺序元组控制的是「默认工具列表」的排序，而 `execute` 是能力门槛最高的一个。

**能力门槛**有两处，都在模型调用前才真正生效（第 6.2 节）：

| 工具 | 门槛 | 不满足时 |
|---|---|---|
| `execute` | `supports_execution(backend)` | 从模型请求里过滤掉 |
| `delete` | `_supports_delete(backend)` | 从模型请求里过滤掉 |

注意这两个工具**仍然会被 `_create_tools()` 生成**（除非被 `tools=` 排除）。过滤发生在模型调用前，因为 Backend 的能力要到运行时才确定——尤其 `CompositeBackend` 的默认 Backend 可能在别处被替换。

### 4.2 一次 `write_file` 的完整骨架

**真实源码节选：** [`filesystem.py#L2334-L2373`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L2334-L2373)

```python
def _create_write_file_tool(self) -> BaseTool:
    """Create the write_file tool."""
    tool_description = self._custom_tool_descriptions.get("write_file") or WRITE_FILE_TOOL_DESCRIPTION

    def sync_write_file(
        file_path: str,
        content: str,
        runtime: ToolRuntime[None, FilesystemState],
    ) -> ToolMessage:
        """Synchronous wrapper for write_file tool."""
        resolved_backend = self.backend
        try:
            validated_path = validate_path(file_path)
        except ValueError as e:
            return ToolMessage(
                content=f"Error: {e}",
                name="write_file",
                tool_call_id=runtime.tool_call_id,
                status="error",
            )

        if (error := _permission_error(self._permissions, "write", self.backend, validated_path)) is not None:
            return ToolMessage(
                content=error,
                name="write_file",
                tool_call_id=runtime.tool_call_id,
                status="error",
            )
        res: WriteResult = resolved_backend.write(validated_path, content)
        if res.error:
            return ToolMessage(
                content=res.error,
                name="write_file",
                tool_call_id=runtime.tool_call_id,
                status="error",
            )
        return ToolMessage(
            content=f"Updated file {res.path}",
            name="write_file",
            tool_call_id=runtime.tool_call_id,
            status="success",
        )
```

外面再包一层：

```python
return StructuredTool.from_function(
    name="write_file",
    description=tool_description,
    func=sync_write_file,
    coroutine=async_write_file,
    infer_schema=False,
    args_schema=args_schema,
)
```

这个骨架在八种工具里高度重复。把它抽象出来，就是三段：

```text
① validate_path(file_path)        → 归一化路径，失败就返回 error
② _permission_error(...)          → 判权限，失败就返回 error
③ backend.write(path, content)    → 真正干活，失败就返回 error
④ 返回 ToolMessage
```

### 4.3 三个统一动作

**动作一：`validate_path`。** 它把模型给的路径归一化（去掉冗余的 `..`、补全相对路径等），失败时抛 `ValueError`。所有工具的第一步都是它。

顺序很重要：**先归一化，再判权限。** 如果先判权限，模型可以写 `/data/../etc/passwd`——权限规则匹配的是 `/data/...`，看起来合法，但实际落点是 `/etc/passwd`。

**动作二：`_permission_error`。** 返回 `None` 表示放行，返回字符串表示拒绝理由。注意它是「返回错误文本」而不是「抛异常」——因为工具执行失败不应该中断 Agent Loop，而应该让模型看到错误并自行调整。

**动作三：调 Backend。** 这里有一个容易被忽略的细节：

```python
resolved_backend = self.backend
```

先取到一个局部变量，再调用。源码没有解释这一步，但从写法上能看出两个好处：

1. **一次调用里只解析一次。** 同一个函数体里 `_permission_error(...)` 和 `res = resolved_backend.write(...)` 都要用 Backend。绑定到局部变量，就不必两次走属性查找。
2. **async 版本里更稳妥。** `async_write_file` 里有 `await`；如果 `self.backend` 在等待期间被别处改动，每次重新读属性就会让同一个函数内的两次调用指向不同对象。先取局部变量就不依赖这个假设。

代价几乎为零，收益是「这次调用看到的是同一个 Backend」。

### 4.4 `ToolRuntime` 为什么在参数里

`runtime: ToolRuntime[None, FilesystemState]` 这个参数**不由模型生成**。它由工具执行系统在调用时注入。

它携带的关键信息是 `runtime.tool_call_id`。这个 ID 必须出现在返回的 `ToolMessage` 里，否则消息无法与原始调用配对——第九章第三节讲过这件事。

所以工具函数的真实签名和模型看到的 schema 是**两套**：

| 视角 | `write_file` 的参数 |
|---|---|
| 模型看到的 schema | `file_path`、`content` |
| 实际的 Python 函数 | `file_path`、`content`、`runtime` |

`StructuredTool` 负责把 `runtime` 从 schema 里摘掉，交给执行系统注入。这也是为什么每个工具都要 `args_schema=...` 和 `infer_schema=False`——schema 是手写的，不是从函数签名推断的。

### 4.5 工具描述本身也是运行期生成的

这是一个很精巧的设计，容易在通读时被跳过。

`execute` 的描述里会包含「用 `grep` / `glob` 配合搜索」的示例。但如果用户通过 `tools=` 把 `grep` 排除了，描述里就不该再提它——否则模型会尝试调用一个不存在的工具。

于是描述变成四个变体：

**真实源码节选：** [`filesystem.py#L2967-L2985`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L2967-L2985)

```python
def _execute_tool_description(self, *, visible_search_tools: set[str]) -> str:
    """Return the execute description for the visible search tools."""
    custom_description = self._custom_tool_descriptions.get("execute")
    if custom_description:
        return custom_description
    if "grep" in visible_search_tools and "glob" in visible_search_tools:
        return EXECUTE_TOOL_DESCRIPTION
    if "grep" in visible_search_tools:
        return _EXECUTE_TOOL_DESCRIPTION_WITH_GREP_ONLY
    if "glob" in visible_search_tools:
        return _EXECUTE_TOOL_DESCRIPTION_WITH_GLOB_ONLY
    return _EXECUTE_TOOL_DESCRIPTION_WITHOUT_SEARCH
```

`grep` 那边也有对应的一层：`_with_filtered_grep_description` 会根据 `execute` 是否活跃，切换描述里「你也可以用 shell 命令搜索」这类引导。

这两个函数在 `_filter_unsupported_tools_and_apply_prompt` 里被调用，而且有一个共同点：**只在描述还是默认值时才改写。** 如果用户通过 `custom_tool_descriptions` 自己写了描述，源码不碰它（`if self._custom_tool_descriptions.get("grep"): return tools`）。

这是一个合理但值得记住的边界：**自定义描述会失去「随可见工具自动调整」的能力。**

### 4.6 这些工具怎么进入 Agent

第九章第三节已经讲过 factory 的收集逻辑，这里只补一个视角。

```python
middleware_tools = [
    t
    for m in middleware
    for t in getattr(m, "tools", [])
]
```

`self.tools` 是 `list[BaseTool]`，而且是在 `__init__` 末尾就生成好的（`self.tools = self._create_tools()`）。所以：

| 阶段 | 发生了什么 |
|---|---|
| `__init__` | 八个工具**实例**被创建，闭包在 `self` 上 |
| factory | 从 `middleware.tools` 收集，汇总进 ToolNode |
| 每次模型调用 | `bind_tools` 把 schema 给模型；同时 `wrap_model_call` 可能再过滤一次 |

注意最后一行的「再过滤一次」：`tools=` 的排除已经在生成阶段完成，但**能力门槛**（`execute` / `delete`）要到模型调用前才算。两套过滤发生在不同阶段，原因不同：

- `tools=` 是**用户意图**，装配时就知道，所以尽早排除。
- 能力门槛是**运行时事实**，取决于 Backend，所以放在模型调用前。

---

## 五、`wrap_tool_call`：三件事，以及一件它明确不做的事

### 5.1 真实源码

**真实源码节选：** [`filesystem.py#L3850-L3878`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L3850-L3878)

```python
def wrap_tool_call(
    self,
    request: ToolCallRequest,
    handler: Callable[[ToolCallRequest], ToolMessage | Command],
) -> ToolMessage | Command:
    """Provide on-demand media access and evict oversized tool results."""
    if error := _parallel_file_mutation_error(request):
        return error
    with _tool_blob_store(self.backend if self._offload_binary_content else None, self._blobs_prefix):
        tool_result = handler(request)
    if self._offload_binary_content and request.tool_call["name"] == "read_file":
        tool_result = _offload_tool_result(tool_result, self.backend, self._blobs_prefix, {})

    if self._tool_token_limit_before_evict is None or request.tool_call["name"] in TOOLS_EXCLUDED_FROM_EVICTION:
        return tool_result

    return self._intercept_large_tool_result(tool_result)
```

短短十几行，四个动作。前三个一眼可见，第四个藏在一个 `in` 判断里——而它恰好是最重要的。

### 5.2 第一件：并行同路径修改

**真实源码节选：** [`filesystem.py#L200-L226`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L200-L226)

```python
def _parallel_file_mutation_error(request: ToolCallRequest) -> ToolMessage | None:
    """Reject later same-path file mutations in one model response."""
    tool_call = request.tool_call
    if tool_call["name"] not in _FILE_MUTATION_TOOLS:
        return None
    path = tool_call["args"].get("file_path")
    if not isinstance(path, str):
        return None
    try:
        file_path = validate_path(path)
    except ValueError:
        return None
    messages = request.state.get("messages") if isinstance(request.state, Mapping) else None
    ai_message = next((message for message in reversed(messages or []) if isinstance(message, AIMessage)), None)
    for call in ai_message.tool_calls if ai_message else []:
        if call["id"] == tool_call["id"]:
            return None
        path = call["args"].get("file_path")
        if call["name"] not in _FILE_MUTATION_TOOLS or not isinstance(path, str):
            continue
        try:
            duplicate = validate_path(path) == file_path
        except ValueError:
            continue
        if duplicate:
            return _tool_error(tool_call["name"], tool_call["id"], "Error: parallel file mutations to the same path are not allowed.")
    return None
```

涉及的集合在文件顶部：

**真实源码节选：** [`filesystem.py#L183`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L183)

```python
_FILE_MUTATION_TOOLS: Final = frozenset({"write_file", "edit_file", "delete"})
```

三个工具，不是两个。直觉上容易只想到 `write_file` 和 `edit_file`，但 `delete` 同样在列——这很合理：同一批里先 `write_file` 再 `delete`，结果取决于执行顺序。

算法本身很简单，但有四个细节值得看清：

| 细节 | 代码 | 含义 |
|---|---|---|
| **只看最近一条 AIMessage** | `next(... reversed(messages) ...)` | 冲突只在「同一批 tool_calls」里算 |
| **遇到自己就停** | `if call["id"] == tool_call["id"]: return None` | 只拒绝**后面**的调用，第一个放行 |
| **归一化后比较** | `validate_path(path) == file_path` | `/a.txt` 和 `/./a.txt` 算同一个 |
| **返回错误消息，不抛异常** | `_tool_error(...)` | 让模型看到失败并自行调整 |

第二条最容易被忽略：**它不是「检测到冲突就全拒绝」，而是「保留第一个，拒绝后续」。** 顺序由 `ai_message.tool_calls` 的排列决定，也就是模型生成调用的顺序。

这个选择是合理的：模型既然先写了 A 再改 A，说明它的意图是「先写再改」，按顺序执行是符合意图的。真正危险的是并行——如果 ToolNode 同时启动这两个调用，最终内容就不确定了。

`_tool_error` 生成的是一条 `status="error"` 的 `ToolMessage`：

```python
return ToolMessage(content=content, name=name, tool_call_id=tool_call_id, status="error")
```

所以模型收到的不是异常，而是一条「这次调用失败了，原因是并行冲突」的消息。它可以重试，比如拆成两轮。

### 5.3 第二件：二进制内容卸载

```python
with _tool_blob_store(self.backend if self._offload_binary_content else None, self._blobs_prefix):
    tool_result = handler(request)
if self._offload_binary_content and request.tool_call["name"] == "read_file":
    tool_result = _offload_tool_result(tool_result, self.backend, self._blobs_prefix, {})
```

两段，作用不同：

**`_tool_blob_store`** 是一个 context manager，只在 `_offload_binary_content` 为真时启用。它通过 contextvar 给工具执行期间提供一个「blob 存取器」，让工具内部（比如 `read_file` 读取图片）能把大块内容直接写到 Backend，而不是塞进返回值。

**`_offload_tool_result`** 是后处理：`read_file` 返回的结果里如果含二进制块，把它们移到 `blobs/` 下，返回值里只留引用。

为什么只对 `read_file` 做？因为只有它会**主动**把文件内容带进上下文。其他工具（`ls`、`grep`）返回的是摘要，`write_file` 返回的是确认信息。

### 5.4 第三件：超大工具结果

```python
if self._tool_token_limit_before_evict is None or request.tool_call["name"] in TOOLS_EXCLUDED_FROM_EVICTION:
    return tool_result

return self._intercept_large_tool_result(tool_result)
```

阈值判断在 `_process_large_message` 里：

**真实源码节选：** [`filesystem.py#L3553-L3569`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L3553-L3569)

```python
if not self._tool_token_limit_before_evict:
    return message, False

content_str = _extract_text_from_message(message)

# Check if content exceeds eviction threshold
if len(content_str) <= NUM_CHARS_PER_TOKEN * self._tool_token_limit_before_evict:
    return message, False

processed_message = _offload_tool_message_content(
    message,
    content_str,
    resolved_backend,
    self._large_tool_results_prefix,
)
```

`NUM_CHARS_PER_TOKEN = 4`（[L1093](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L1093)）。所以默认阈值是 `20000 × 4 = 80000` 字符。

注意这是**字符数估算**，不是真的 tokenize。用 4 字符/token 这个经验值，好处是不用引入分词器，代价是中文、代码、JSON 的实际比例各不相同。这是一个有意的近似。

卸载后写入的路径是 `{prefix}/{sanitized_tool_call_id}`（[`_message_eviction.py#L274`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/_message_eviction.py#L274)），也就是用 tool_call_id 当文件名。这样模型可以从占位消息里直接读出路径。

替换后的消息保留头尾各 5 行的预览（`_create_content_preview` 的默认 `head_lines=5, tail_lines=5`），并在中间说明省略了多少行。这个设计的目的是让模型判断「这份结果值不值得读全文」，而不是盲读。

### 5.5 第四件，也是最重要的一件：它不处理自己的七个工具

**真实源码节选：** [`filesystem.py#L1749-L1757`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L1749-L1757)

```python
TOOLS_EXCLUDED_FROM_EVICTION = (
    "ls",
    "glob",
    "grep",
    "read_file",
    "edit_file",
    "write_file",
    "delete",
)
```

七个名字，正好是这个 Middleware 自己的七个文件工具（`execute` 不在列）。

这直接推翻了一个很自然的猜测：

> 假设一个 `grep` 返回了 20 万字符。……所以它可以将结果保存到文件系统：`/large_tool_results/...`

**实际上 `grep` 永远不会被卸载。** 它在排除列表里，`wrap_tool_call` 的第三段逻辑直接 `return tool_result`。

源码注释给了三条理由（[L1726-L1748](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L1726-L1748)）：

| 类别 | 工具 | 理由 |
|---|---|---|
| **自带截断** | `ls`、`glob`、`grep` | 它们自己会在结果过多时截断；截断通常说明查询需要收窄，而不是结果需要完整保留 |
| **截断会坏事** | `read_file` | 如果截断 `read_file` 的结果，模型会再读一次，仍然读不到——问题出在单行过长，不是总量过大 |
| **不会超限** | `edit_file`、`write_file`、`delete` | 它们只返回一行确认信息 |

第一条的理由值得细读：`grep` 截断后剩下的匹配，在源码看来**更像噪声而不是信息**。所以正确的反应是「让模型收窄查询」，而不是「把完整结果存起来让它慢慢看」。

于是这个 Hook 的真实定位变得很清楚了：

> **它不给自己那七个文件工具的结果做兜底；它给 `execute` 和所有「别的」工具的结果做兜底。**

「别的工具」包括 `task`（子 Agent 调用）、用户自定义工具、MCP 工具。这些工具的结果大小完全不受 `FilesystemMiddleware` 控制，所以需要一个统一的出口来拦截。

而 `execute` 是个有意思的例外：它**是**这个 Middleware 自己的工具，但它的输出不受控制（一条 `pytest` 就能刷出几万行），所以它留在卸载范围内。

这个认知修正很重要，因为它改变了你对 `tool_token_limit_before_evict` 的理解：**这个参数不是「文件工具的结果上限」，而是「除七个文件工具之外所有工具的结果上限」。**

### 5.6 为什么冲突检查必须放在这里

把冲突检查放在 `wrap_tool_call`，而不是放在 `write_file` 工具内部，理由有三条：

**第一，它需要看到整批调用。** `write_file` 在执行时只拿得到自己的参数。要判断「同一批里有没有别人也改这个路径」，必须能访问 `request.state["messages"]` 里那条 `AIMessage`。

**第二，它必须覆盖多个工具。** 冲突可能发生在 `write_file` 和 `delete` 之间。如果检查写在每个工具内部，就要写三份，而且三份必须保持一致。

**第三，它必须在所有工具执行之前。** `wrap_tool_call` 是每个 tool_call 都会经过的入口，天然满足这个要求。

这三条恰好也是「为什么需要 Middleware」的一个缩影：**跨工具、跨调用、需要看全局状态**——这三件事，单个工具函数都做不到。

---

## 六、`wrap_model_call`：五个步骤

### 6.1 真实源码

**真实源码节选：** [`filesystem.py#L3410-L3470`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L3410-L3470)

```python
def wrap_model_call(
    self,
    request: ModelRequest[ContextT],
    handler: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
) -> ModelResponse[ResponseT] | ExtendedModelResponse:
    """Update the system prompt, filter tools, and evict oversized HumanMessages."""
    request = self._filter_unsupported_tools_and_apply_prompt(request)

    request_messages = _move_media_results_after_tool_results(list(request.messages))
    if request_messages != list(request.messages):
        request = request.override(messages=request_messages)

    state_command = None
    eviction_result = self._evict_and_truncate_messages(request)
    if eviction_result is not None:
        messages, state_command = eviction_result
        request = request.override(messages=messages)
    if self._offload_binary_content:
        cached = request.state.get(_BLOB_PAYLOADS_KEY) or {}
        payloads = dict(cached)
        offloaded = _offload_history_messages(
            request.state.get("messages", []), _command_messages(state_command), self.backend, self._blobs_prefix, payloads
        )
        request = request.override(messages=_hydrate_messages(request.messages, self.backend, self._blobs_prefix, payloads))
        updates: dict[str, Any] = {"messages": offloaded} if offloaded else {}
        if payloads.keys() != cached.keys():
            updates[_BLOB_PAYLOADS_KEY] = payloads
        state_command = _with_update(state_command, updates)
    try:
        response = handler(request)
    except ModelInvalidRequestError:
        messages = _replace_rejected_file_content(request.messages)
        if messages == request.messages:
            raise
        response = handler(request.override(messages=messages))
        replacements = [message for original, message in zip(request.messages, messages, strict=True) if message is not original]
        update = dict(cast("dict[str, Any]", state_command.update)) if state_command is not None else {}
        update["messages"] = [*update.get("messages", []), *replacements]
        state_command = replace(state_command, update=update) if state_command is not None else Command(update=update)
    if state_command is not None:
        return ExtendedModelResponse(model_response=response, command=state_command)
    return response
```

一种常见的简化版是：

```python
request = self._filter_unsupported_tools_and_apply_prompt(request)
eviction_result = self._evict_and_truncate_messages(request)
...
response = handler(request)
```

它漏掉了三步：媒体消息重排、二进制卸载与恢复、模型拒绝内容后的重试。其中第三步尤其容易和「处理模型不接受的文件内容」混为一谈，但那实际上是两个不同的机制：前者是**改写历史**，后者是**失败后重试**。

按执行顺序拆成五步。

### 6.2 第一步：过滤能力不支持的工具，并拼 Prompt

**真实源码节选：** [`filesystem.py#L3377-L3406`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L3377-L3406)

```python
tool_names: set[str | None] = {self._tool_name(tool) for tool in request.tools}
unsupported, execution_active, backend = self._unsupported_tools_and_execution_state(tool_names)
visible_tools = [tool for tool in request.tools if self._tool_name(tool) not in unsupported]
visible_fs = {name for name in (tool_names - unsupported) if name is not None}
if unsupported:
    request = request.override(tools=visible_tools)

described_tools = self._with_filtered_grep_description(visible_tools, include_execution=execution_active)
described_tools = self._with_filtered_execute_description(
    described_tools,
    visible_search_tools=visible_fs,
)
if described_tools is not visible_tools:
    request = request.override(tools=described_tools)

prompt_parts = [self._custom_system_prompt] if self._custom_system_prompt else []
if execution_active:
    route_prompt = _route_host_path_prompt(cast("BackendProtocol", backend))
    if route_prompt:
        prompt_parts.append(route_prompt)
system_prompt = "\n\n".join(prompt_parts).strip()

if system_prompt:
    new_system_message = append_to_system_message(request.system_message, system_prompt)
    request = request.override(system_message=new_system_message)
```

能力判断只算两个工具：

**真实源码节选：** [`filesystem.py#L3057-L3089`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L3057-L3089)

```python
unsupported: set[str | None] = set()
execution_active = False
backend = None
has_execute_tool = "execute" in tool_names
has_delete_tool = "delete" in tool_names
if not has_delete_tool and not has_execute_tool:
    return unsupported, execution_active, backend

backend = self.backend
if has_execute_tool and "execute" not in unsupported:
    execution_active = supports_execution(backend)
    if not execution_active:
        unsupported.add("execute")
if has_delete_tool and "delete" not in unsupported and not _supports_delete(backend):
    unsupported.add("delete")
return unsupported, execution_active, backend
```

注意最前面那个 early return：如果 `request.tools` 里既没有 `execute` 也没有 `delete`，连 Backend 都不去探测。这是一个廉价的优化——大多数 Agent 的请求里只有文件工具，不需要每次模型调用都做能力检查。

然后是 Prompt 部分。这里有一个容易说反的主次关系。

常见的说法是：

> ### 2. 给模型增加必要的文件系统提示
> 例如在某些 CompositeBackend 配置下，虚拟文件路径与命令执行环境中的真实路径之间存在映射。Middleware 可以通过 System Prompt 提供相关说明。
> 但它不会每次都生成一大段通用文件工具使用教程，因为工具本身已经有名称、描述和参数 Schema。

第二段是对的，第一段说反了主次。真实源码里，Prompt 只有两个来源：

| 来源 | 何时加入 |
|---|---|
| `self._custom_system_prompt` | 调用方通过 `system_prompt=` 传入时 |
| `_route_host_path_prompt(backend)` | `execute` 活跃且 Backend 是 `CompositeBackend` 时 |

没有第三个。源码注释把这件事写得很直白：

**真实源码节选：** [`filesystem.py#L3391-L3396`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L3391-L3396)

```python
# `system_prompt` (default `None`) is the caller's tool-usage prose; no
# built-in tool-usage guidance is generated, since it would duplicate the
# tools' own schema descriptions. The host-path routing section is
# essential per-backend config (virtual->host path mapping for the `execute`
# shell), not prose, so it is appended when the execute tool is active
# regardless of the prose. Routing is empty for non-composite backends.
```

所以 `system_prompt` 默认是 `None`，默认情况下**这个 Middleware 不往 System Prompt 里加任何东西**（除非 `execute` 活跃且需要路径映射）。

`_route_host_path_prompt` 解决的问题很具体：`execute` 在默认 Backend 的 shell 里跑，而 `CompositeBackend` 的虚拟路径（比如 `/common/`）在 host 上可能不存在。与其改写 shell 命令，不如给模型一张前缀替换表：

```text
## Shell paths vs. virtual paths

The `execute` tool runs commands in the host shell and can only access files that exist on the host filesystem.

Some paths returned by the file tools are virtual mounts:
...
Host path mappings:
- `/common/` -> `/data/` (e.g. `/common/dir/x.py` -> `/data/dir/x.py`)
```

注意它是**表**，不是教程。模型拿到「`/common/` 要换成 `/data/`」这一条映射就够了，剩下的推理它自己做。

### 6.3 第二步：把媒体消息挪到工具结果之后

**真实源码节选：** [`filesystem.py#L234-L263`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L234-L263)

```python
def _move_media_results_after_tool_results(messages: list[AnyMessage]) -> list[AnyMessage]:
    """Keep synthetic media messages after the tool-result batch they describe.

    Tool-call providers require every `ToolMessage` for an assistant tool-call
    batch to arrive before any non-tool message. Video reads attach sampled
    frames as a synthetic `HumanMessage`; when multiple tools run in the same
    turn this helper keeps those attachments behind the full batch.
    """
```

这是一个纯粹的消息**顺序**修正，动机是 API 契约：

```text
正确顺序：
  AIMessage(tool_calls=[A, B])
  ToolMessage(A)
  ToolMessage(B)
  HumanMessage(视频帧)        ← 合成消息，放在最后

错误顺序：
  AIMessage(tool_calls=[A, B])
  ToolMessage(A)
  HumanMessage(视频帧)        ← 插在中间，破坏「同批结果必须连续」
  ToolMessage(B)
```

为什么会有合成消息？因为 `read_file` 读视频时，会把抽帧结果作为一条**合成的 `HumanMessage`** 附在工具结果后面（`_is_read_file_media_result` 靠 `additional_kwargs` 里的标记识别它）。如果同一轮里还调用了别的工具，这条合成消息可能被插进 `ToolMessage` 批次中间。

`_move_media_results_after_tool_results` 做的事就是把这一批里的 `ToolMessage` 全部前置，合成媒体消息全部后置。

注意它只在**顺序真的变了**时才 `request.override(messages=...)`：

```python
if request_messages != list(request.messages):
    request = request.override(messages=request_messages)
```

这是 6.2 节末尾那个 early return 的同一个模式——避免制造一个内容相同的副本。

### 6.4 第三步：超大 `HumanMessage` 的卸载

这一段比看上去复杂，因为它是**两步**：先写文件、再打标记。

**真实源码节选：** [`filesystem.py#L3603-L3626`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L3603-L3626)

```python
def _check_eviction_needed(
    self,
    messages: list[AnyMessage],
) -> tuple[bool, bool]:
    """Check whether any message processing is needed.

    Returns:
        Tuple of `(has_tagged, new_eviction_needed)`.
    """
    if not self._human_message_token_limit_before_evict:
        return False, False

    threshold = NUM_CHARS_PER_TOKEN * self._human_message_token_limit_before_evict
    has_tagged = any(isinstance(msg, HumanMessage) and msg.additional_kwargs.get("lc_evicted_to") for msg in messages)
    new_eviction_needed = False
    if messages and isinstance(messages[-1], HumanMessage):
        last = messages[-1]
        if not last.additional_kwargs.get("lc_evicted_to") and len(_extract_text_from_message(last)) > threshold:
            new_eviction_needed = True
    return has_tagged, new_eviction_needed
```

两个布尔值，含义不同：

| 返回值 | 含义 | 触发什么 |
|---|---|---|
| `has_tagged` | 历史里已有被卸载过的消息 | 需要重新截断它们（每轮都要） |
| `new_eviction_needed` | **最后一条** HumanMessage 超限且未标记 | 需要新写一次文件 |

这里有一个容易误判的边界：**只有最后一条会被检查。** 历史里那些没有被标记过的旧大消息，永远不会被卸载。

为什么可以这样？因为一条 `HumanMessage` 只可能在它刚进入对话时超限。后续轮次里，它要么已经被标记（`has_tagged` 会处理），要么本来就不超限。所以「只查最后一条」不是遗漏，而是基于「用户消息只会追加在末尾」这个前提的优化。

标记的写入在 `_apply_eviction_and_truncate` 里：

**真实源码节选：** [`filesystem.py#L3651-L3666`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L3651-L3666)

```python
if write_result is not None and file_path is not None and not write_result.error:
    last = messages[-1]
    tagged = last.model_copy(
        update={
            "id": last.id if last.id is not None else str(uuid.uuid4()),
            "additional_kwargs": {
                **last.additional_kwargs,
                "lc_evicted_to": file_path,
            },
        }
    )
    state_command = Command(update={"messages": [tagged]})
    messages = [*messages[:-1], tagged]
```

关键点：**写回 State 的是「打了标记的原消息」，不是「截断后的消息」。**

| 位置 | 内容 |
|---|---|
| State 里的 `HumanMessage` | 原文 + `lc_evicted_to` 标记 |
| 这次请求里的 `HumanMessage` | 截断预览 + 文件路径 |

这是一个很重要的取舍：State 保留原文，意味着信息不丢失（后续要恢复还有可能）；请求里只给预览，意味着这一轮省了 token。

而 `state_command` 的存在说明：**这个修改必须通过 `Command` 提交回图**，不能只改 `request`。第 6.7 节会讲这件事。

「仅仅调用 `request.override()`，并不意味着原始 Graph State 会被直接替换」——这句话是对的，而且正是这里的关键。

### 6.5 第四步：二进制历史的卸载与恢复

```python
if self._offload_binary_content:
    cached = request.state.get(_BLOB_PAYLOADS_KEY) or {}
    payloads = dict(cached)
    offloaded = _offload_history_messages(
        request.state.get("messages", []), _command_messages(state_command), self.backend, self._blobs_prefix, payloads
    )
    request = request.override(messages=_hydrate_messages(request.messages, self.backend, self._blobs_prefix, payloads))
```

这里有两个方向的动作，容易看混：

| 函数 | 方向 | 作用 |
|---|---|---|
| `_offload_history_messages` | State → Backend | 把历史里的大块二进制移到 `blobs/` |
| `_hydrate_messages` | Backend → 请求 | 把请求里需要的二进制从 Backend 读回内存 |

为什么两个方向都要做？因为它们服务于两个不同的目标：

- **卸载**是为了减小 Checkpoint 和 State 的体积。历史里的图片、音频如果一直躺在 State 里，每存一次快照就复制一遍。
- **恢复**是为了这一轮模型真的需要看图时，能把内容放进请求里。

而 `payloads`（也就是 `_blob_payloads`）是这两者之间的缓存：卸载时记下「这个引用对应哪个 payload」，恢复时直接查。

注意 `updates[_BLOB_PAYLOADS_KEY] = payloads` 这个条件：

```python
if payloads.keys() != cached.keys():
    updates[_BLOB_PAYLOADS_KEY] = payloads
```

只有缓存**键集合**变了才写回 State。如果只是值变了（同一批 blob 被重新加载），不写。这是又一次「避免无意义的 State 更新」。

### 6.6 第五步：模型拒绝内容时的重试

**真实源码节选：** [`filesystem.py#L3458-L3470`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L3458-L3470)

```python
try:
    response = handler(request)
except ModelInvalidRequestError:
    messages = _replace_rejected_file_content(request.messages)
    if messages == request.messages:
        raise
    response = handler(request.override(messages=messages))
    replacements = [message for original, message in zip(request.messages, messages, strict=True) if message is not original]
    update = dict(cast("dict[str, Any]", state_command.update)) if state_command is not None else {}
    update["messages"] = [*update.get("messages", []), *replacements]
    state_command = replace(state_command, update=update) if state_command is not None else Command(update=update)
```

这是一个**自愈**机制：模型服务端拒绝了请求（比如「这个模型不支持图片」），Middleware 把图片换掉，重试一次。

`_replace_rejected_file_content` 的替换范围很窄：

**真实源码节选：** [`filesystem.py#L265-L276`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L265-L276)

```python
def _replace_rejected_file_content(messages: list[AnyMessage]) -> list[AnyMessage]:
    """Replace only multimodal reads since the latest model response."""
    last_response = next((index for index in range(len(messages) - 1, -1, -1) if isinstance(messages[index], AIMessage)), -1)
    return [
        message.model_copy(update={"content": "Unsupported content. The file may be invalid, too large, or of an unsupported mime-type."})
        if index > last_response
        and (_is_read_file_media_result(message) or (isinstance(message, ToolMessage) and message.name == "read_file"))
        and any(block["type"] in _MULTIMODAL_BLOCK_TYPES for block in message.content_blocks)
        else message
        for index, message in enumerate(messages)
    ]
```

三个条件必须同时满足：

| 条件 | 作用 |
|---|---|
| `index > last_response` | 只替换**最近一次模型回复之后**的消息 |
| 是 `read_file` 的结果或它的合成媒体消息 | 只替换文件读取产生的多模态内容 |
| 含多模态块 | 纯文本的 `read_file` 结果不动 |

第一个条件最关键：它保证只动「这一轮新读出来的东西」，不会去改历史里已经成功的图片。如果改了历史，那就等于让模型忘记之前看过的东西。

如果替换后没有任何变化（`messages == request.messages`），说明问题不在文件内容上，直接 `raise` 把原异常抛出去——不要用一个无效的重试掩盖真实错误。

### 6.7 `ExtendedModelResponse`：为什么不能只 `override`

这是整个 `wrap_model_call` 里最容易被忽略、也最重要的一处。

函数的返回类型是：

```python
-> ModelResponse[ResponseT] | ExtendedModelResponse
```

`ExtendedModelResponse` 是 `ModelResponse` 加一个 `command` 字段。为什么要多这一个？

因为 `request.override(...)` 改的是**这一次调用**的请求，它不影响 Graph State。而第 6.4 节里那个 `lc_evicted_to` 标记，必须写回 State——否则下一轮 `_check_eviction_needed` 看不到它，会重复卸载同一条消息。

```text
request.override(messages=...)     → 只影响这次 handler 调用
ExtendedModelResponse(command=...) → 影响 Graph State
```

两者是两条路，不能互相替代。简化版里通常看不到 `ExtendedModelResponse`，于是「状态怎么被更新」这件事就消失了——而它在真实实现里是显式处理的：

```python
if state_command is not None:
    return ExtendedModelResponse(model_response=response, command=state_command)
return response
```

没有 State 变更时，返回普通的 `ModelResponse`。有变更时，包一层带上 `command`。这个分支设计让「绝大多数模型调用不带 State 更新」成为常态，只在真的发生卸载时才付出额外成本。

---

## 七、为什么它没有 `before_agent`

### 7.1 三个 Middleware 的 Hook 对照

| | `MemoryMiddleware` | `SkillsMiddleware` | `FilesystemMiddleware` |
|---|---|---|---|
| `before_agent` | 有（读 `AGENTS.md`） | 有（扫 `SKILL.md`） | **无** |
| `wrap_model_call` | 有（注入记忆） | 有（注入目录、披露工具） | 有（筛工具、缩消息） |
| `wrap_tool_call` | 无 | 有（放行 Skill 工具） | 有（冲突、卸载） |
| `state_schema` | `MemoryState` | `SkillsState` | `FilesystemState` |
| `self.tools` | 无 | 无（动态披露） | **有（八个）** |

这个表说明一件有意思的事：三个 Middleware 的「能力组合」几乎没有重叠。

### 7.2 三种「加载」不是一回事

`MemoryMiddleware` 和 `SkillsMiddleware` 都需要 `before_agent`，因为它们的共同点是：**要在 Agent 启动时，把 Backend 里的内容搬到上下文里。**

`FilesystemMiddleware` 不需要，因为它的内容**不预先搬运**。

| | Memory | Skills | Filesystem |
|---|---|---|---|
| 启动时要读什么 | `AGENTS.md` 全文 | 所有 `SKILL.md` 的 frontmatter | **什么也不读** |
| 读来做什么 | 每轮注入 System Prompt | 生成目录 + 建立索引 | — |
| 正文何时进上下文 | 每轮 | 模型主动 `read_file` | 模型主动 `read_file` |
| 谁触发读取 | Middleware | 模型 | 模型 |

关键区别在最后两行：Memory 和 Skills 都有「Middleware 主动去读」的动作；Filesystem 没有——它的读取**只由模型的工具调用触发**。

这个区别不是风格问题，而是必要性：

- Memory 必须在每一轮模型调用前就有内容，所以必须在 `before_agent` 提前读好（否则每轮都要读，那正是第 8 章附二讨论的取舍）。
- Skills 必须在模型第一次调用前就有目录，否则模型不知道有哪些 Skill 可用。
- Filesystem 什么都不需要预先知道。模型要看哪个文件，它自己会调 `read_file`。

所以 `FilesystemMiddleware` 是一个**纯被动**的 Middleware：它只提供能力，不主动搬运内容。

这也解释了为什么它的 `state_schema` 里那个 `files` 字段是「存储」而不是「缓存」——它存的不是「读进来的副本」，而是「文件本身」（在 `StateBackend` 的情况下）。

---

## 八、安全边界：`permissions` 能做什么，不能做什么

### 8.1 规则的三要素

**真实源码节选：** [`filesystem.py#L368-L392`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L368-L392)

```python
@dataclass
class FilesystemPermission:
    """A single access rule for filesystem operations."""

    operations: list[FilesystemOperation]
    paths: list[str]
    mode: Literal["allow", "deny", "interrupt"] = "allow"
```

一条规则由三部分组成：

| 要素 | 例子 | 含义 |
|---|---|---|
| `operations` | `["read", "write"]` | 管哪些操作 |
| `paths` | `["/secrets/**"]` | 管哪些路径（glob） |
| `mode` | `"deny"` | 匹配后怎么办 |

匹配逻辑很直白（`_check_fs_permission`）：**按顺序遍历，第一条命中的规则决定结果，没有命中就默认 `allow`。**

```python
for rule in rules:
    if operation not in rule.operations:
        continue
    if any(wcglob.globmatch(path, pattern, flags=_FS_WCMATCH_FLAGS) for pattern in rule.paths):
        return rule.mode
return "allow"
```

最后那行 `return "allow"` 定义了整个系统的默认姿态：**白名单为空等于全放开。** 这符合「permissions 是可选的加固」这个定位，但也意味着忘记配置就等于没有保护。

### 8.2 三种模式

| `mode` | 行为 | 谁来执行 |
|---|---|---|
| `allow` | 放行 | — |
| `deny` | 工具返回权限错误 | `_permission_error` |
| `interrupt` | 暂停，等待人工批准 | `HumanInTheLoopMiddleware` |

`interrupt` 不是 `FilesystemMiddleware` 自己实现的——它只是把规则**声明**出来，实际的暂停由 `HumanInTheLoopMiddleware` 完成。这就是下一章的主题。

源码里对 `interrupt` 有一条实践建议，写在 docstring 里：

> Best paired with patterns that have a literal leading anchor (e.g., `/secrets/**`, `/projects/*/secrets/**`). Bulk tools (`ls`/`glob`/`grep`) fire the interrupt based on whether their search subtree could overlap the rule's anchored prefix, so a fully unanchored pattern (`/**/secrets`) collapses to `/` and conservatively over-fires for any bulk call.

意思是：`/**/secrets` 这种没有固定前缀的模式，会让**每一次** `ls` / `grep` 都触发审批，因为系统无法确定搜索范围是否碰到它。这是「宁可多问」的保守策略，但用起来会很烦。

### 8.3 为什么 `execute` 存在时不能配 `permissions`

**真实源码节选：** [`filesystem.py#L2053-L2063`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L2053-L2063)

```python
def _set_permissions(self, permissions: list[FilesystemPermission] | None) -> None:
    """Validate and copy rules against this middleware's backend."""
    if permissions and supports_execution(self.backend) and not _all_paths_scoped_to_routes(permissions, self.backend):
        msg = (
            "FilesystemMiddleware does not yet support permissions with backends that "
            "provide command execution (SandboxBackendProtocol). Tool-level permissions "
            "for the execute tool are not implemented. Either remove permissions or use "
            "a backend without execution support."
        )
        raise NotImplementedError(msg)
```

条件有三个，必须同时成立才抛：

| 条件 | 含义 |
|---|---|
| `permissions` 非空 | 用户确实配了规则 |
| `supports_execution(self.backend)` | Backend 能跑命令 |
| `not _all_paths_scoped_to_routes(...)` | 规则没有全部限定在路由前缀内 |

第三个条件是一个逃生口：如果所有规则都限定在某个 `CompositeBackend` 路由里（比如 `/secrets/`），而 `execute` 只跑在默认 Backend 上，那么规则和命令执行的作用域不重叠，可以放行。

为什么默认要拒绝？因为 `permissions` 是**工具级**的：它在 `read_file`、`write_file` 这些函数的入口检查路径。而 `execute` 跑的是任意 shell 命令——`cat /secrets/key` 完全绕过工具级检查。

这是一个诚实的设计：**做不到的事就明确说做不到，而不是给一个看起来有效实则漏风的保护。** 报错信息里那句 `Tool-level permissions for the execute tool are not implemented` 就是在说这件事。

### 8.4 路径校验与符号链接

`FilesystemPermission` 的 docstring 里有一句需要注意：

> With `FilesystemBackend` (directly or behind a `CompositeBackend` route), rules are matched against both the requested path and its symlink-resolved target; a deny on either blocks the call.

也就是说，检查做两次：一次对模型给的路径，一次对**解析符号链接之后的真实路径**。

为什么需要两次？因为符号链接是一条绕过路径规则的捷径：

```text
/project/data -> /etc/shadow
```

规则写着「`/project/**` 允许」，模型读 `/project/data` 看起来合法，但实际读的是 `/etc/shadow`。解析后再检查一次，就能拦住这条路径。

这个检查只对 `FilesystemBackend` 生效，因为只有它有真实的符号链接概念。`StateBackend` 里的文件是字典项，没有链接。

---

## 九、失败模式表

把这一章讨论过的坑排成一张表。它比 Skills 和 Memory 那两张表更杂，因为这个 Middleware 的功能面更宽。

| 症状 | 可能原因 | 发生在哪一层 |
|---|---|---|
| 模型看不到 `delete` | Backend 不支持删除（`_supports_delete` 为假） | `wrap_model_call` 过滤 |
| 模型看不到 `execute` | Backend 不支持执行（`supports_execution` 为假） | `wrap_model_call` 过滤 |
| 模型看不到某个工具，且伪造调用也失败 | 该工具被 `tools=` 排除，**根本没有生成** | `_create_tools` |
| 启动时抛 `ValueError` | `tools=` 里没有 `read_file` | `__init__` |
| 启动时抛 `ValueError` | 同时传了 `permissions` 和 `_permissions` | `__init__` |
| 启动时抛 `TypeError` | `backend` 传的是工厂函数而不是实例 | `__init__` |
| 启动时抛 `NotImplementedError` | 配了 `permissions`，且 Backend 支持 `execute`，且规则未全部限定在路由内 | `_set_permissions` |
| `offload_binary_content=True` 但没效果 | `blobs/` 路由到 `StateBackend`，被自动降级并警告 | `__init__` |
| 模型抱怨路径不存在 | `CompositeBackend` 的虚拟路径没有 host 映射，`execute` 看不到 | `_route_host_path_prompt` |
| 同一批里改同一文件的调用失败 | 并行冲突检查（保留第一个，拒绝后续） | `wrap_tool_call` |
| `grep` 返回被截断而不是被存成文件 | `grep` 在 `TOOLS_EXCLUDED_FROM_EVICTION` 里，**永不卸载** | `wrap_tool_call` |
| 大工具结果没被卸载 | 它是本 Middleware 自己的七个工具之一 | 同上 |
| 大工具结果没被卸载 | `tool_token_limit_before_evict=None` | 同上 |
| 卸载后的文件读不回来 | `tools=` 里排除了 `read_file`（但这条会先被 `__init__` 拦住） | 机制依赖 |
| 大用户消息没被卸载 | 它不是**最后一条** HumanMessage | `_check_eviction_needed` |
| 每轮都重复卸载同一条消息 | State 里的标记没写回去（缺 `ExtendedModelResponse`） | `wrap_model_call` |
| 模型报「不支持的内容」后 Agent 崩溃 | 替换后没有变化，异常被重新抛出 | `_replace_rejected_file_content` |
| 权限规则没生效 | 规则列表为空 → 默认全放行 | `_check_fs_permission` |
| 权限规则被绕过 | 用了 `execute`（工具级检查管不到 shell） | 设计限制 |
| 符号链接指向被禁目录 | 只配了 `StateBackend`（没有符号链接概念），或路径未解析 | `_check_fs_permission` |

有三条值得单独拎出来。

**第一，「模型看不到」有三种不同的原因。** 能力门槛、`tools=` 排除、描述重写，三者发生在不同阶段，症状却都是「模型不知道有这个工具」。排查时要分清是哪一种：能不能伪造调用成功，是区分 `tools=` 排除的关键判据。

**第二，「大结果没被卸载」在文件工具上是正常行为，不是 bug。** 这一条大概是本章最反直觉的结论。如果你的 `grep` 返回了海量内容，正确的解法是收窄查询或调低 `grep_max_count`，而不是期待卸载机制接管。

**第三，权限系统的边界是「工具」，不是「进程」。** 只要 `execute` 在场，任何文件都能被 shell 碰到。所以真正的隔离必须在 Backend 层面（沙箱、容器、只读挂载），而不是靠 `permissions`。

---

## 十、最小验证：四个阶段

把整章的推理压成一段伪代码，用来确认因果顺序。

**教学伪代码：未执行，仅用于说明控制流，不是仓库代码**

```python
# ---------- 阶段 1：__init__ 决定生成哪些工具 ----------
ALL_TOOLS = ["ls", "read_file", "write_file", "edit_file", "delete", "glob", "grep", "execute"]

def create_tools(enabled):
    # enabled 为 None 表示全部；否则是白名单
    return [name for name in ALL_TOOLS if enabled is None or name in enabled]

assert create_tools(None) == ALL_TOOLS
assert create_tools(["read_file", "write_file"]) == ["read_file", "write_file"]
assert "delete" not in create_tools(["read_file", "write_file"])   # 根本不生成

# ---------- 阶段 2：state_schema 由 Backend 决定 ----------
class StateBackend:
    pass

class StoreBackend:
    pass

class CompositeBackend:
    def __init__(self, default, routes):
        self.default = default
        self.routes = routes

def uses_state_backend(backend):
    if isinstance(backend, StateBackend):
        return True
    if not isinstance(backend, CompositeBackend):
        return False
    return uses_state_backend(backend.default) or any(
        uses_state_backend(route) for route in backend.routes.values()
    )

assert uses_state_backend(StateBackend()) is True
assert uses_state_backend(StoreBackend()) is False
assert uses_state_backend(CompositeBackend(StoreBackend(), {"/mem/": StoreBackend()})) is False
assert uses_state_backend(CompositeBackend(StoreBackend(), {"/mem/": StateBackend()})) is True   # 任一命中

# ---------- 阶段 3：并行冲突检查 ----------
MUTATION_TOOLS = {"write_file", "edit_file", "delete"}

def normalize(path):
    # 真实实现还处理 .. 与相对路径；这里只演示比较语义
    return path.replace("/./", "/")

def parallel_error(call, batch):
    if call["name"] not in MUTATION_TOOLS:
        return None
    target = normalize(call["args"]["file_path"])
    for other in batch:
        if other["id"] == call["id"]:
            return None                     # 遇到自己就停：只拒绝后面的
        if other["name"] not in MUTATION_TOOLS:
            continue
        if normalize(other["args"]["file_path"]) == target:
            return "Error: parallel file mutations to the same path are not allowed."
    return None

batch = [
    {"id": "1", "name": "write_file", "args": {"file_path": "/a.txt"}},
    {"id": "2", "name": "edit_file",  "args": {"file_path": "/./a.txt"}},
    {"id": "3", "name": "write_file", "args": {"file_path": "/b.txt"}},
]
assert parallel_error(batch[0], batch) is None      # 第一个放行
assert parallel_error(batch[1], batch) is not None  # 第二个被拒（归一化后同路径）
assert parallel_error(batch[2], batch) is None      # 不同路径

# ---------- 阶段 4：卸载的两个门槛 ----------
NUM_CHARS_PER_TOKEN = 4
EXCLUDED = {"ls", "glob", "grep", "read_file", "edit_file", "write_file", "delete"}

def should_evict(tool_name, content, token_limit=20000):
    if token_limit is None:
        return False
    if tool_name in EXCLUDED:
        return False
    return len(content) > NUM_CHARS_PER_TOKEN * token_limit

assert should_evict("grep", "x" * 200_000) is False        # grep 永不卸载
assert should_evict("read_file", "x" * 200_000) is False   # read_file 永不卸载
assert should_evict("execute", "x" * 200_000) is True      # execute 在卸载范围内
assert should_evict("task", "x" * 200_000) is True         # 其他工具会
assert should_evict("task", "x" * 10_000) is False         # 未超阈值
assert should_evict("task", "x" * 200_000, token_limit=None) is False   # 关闭卸载
```

这些断言可以归成六条结论：

| 断言 | 结论 |
|---|---|
| `create_tools(["read_file","write_file"]) == [...]` | `tools=` 是生成阶段的白名单 |
| `"delete" not in create_tools(...)` | 被排除的工具根本不存在 |
| `uses_state_backend(...) is True`（路由命中） | `files` 通道由递归判断决定 |
| `parallel_error(batch[0], batch) is None` | 冲突检查保留第一个 |
| `should_evict("grep", ...) is False` | 七个文件工具不参与卸载 |
| `should_evict("execute", ...) is True` | `execute` 与其他工具仍在范围内 |

**这不是 DeepAgents 的运行结果。** 真实的实现还包含 `validate_path` 的完整规则、权限检查、`_tool_blob_store` 的 contextvar、异步分支等，这里为了看清主干全部省略了。

如果要验证真实行为，需要在安装了固定依赖的环境里做三件事：

1. 装配 `FilesystemMiddleware(tools=["read_file"])`，确认 `self.tools` 里真的只有一个工具，并且伪造一个 `write_file` 调用会被 ToolNode 拒绝。
2. 装配 `FilesystemMiddleware(backend=StateBackend())` 与 `FilesystemMiddleware(backend=FilesystemBackend(root_dir=...))`，对比 `state_schema` 与 State 里有没有 `files`。
3. 一次返回两条改同一文件的 `write_file` 调用，确认第二条收到 `status="error"` 的 `ToolMessage`。

---

## 十一、把理论问题和源码位置对起来

| 理论问题 | 源码位置 | 解决方式 |
|---|---|---|
| 为什么必须是 Middleware | `state_schema` + 两个 Hook | 加通道、拦模型、拦工具，三件事工具函数做不到 |
| 文件状态存在哪 | `FilesystemState.files` | `DeltaChannel` + 批量 reducer |
| 为什么用 `DeltaChannel` | `snapshot_frequency=50` | 增量合并 + 周期性快照，限制读深度 |
| 删除怎么表示 | reducer 里的 `if value is None` | `None` 是删除标记 |
| 为什么 reducer 收列表 | `values: list[...]` | 一步多次写入只拷贝一次 |
| 二进制缓存放哪 | `_BlobPayloadState._blob_payloads` | `UntrackedValue` + `PrivateStateAttr`，不进 Checkpoint |
| 为什么有两个 schema | `_uses_state_backend` | 有 State 存储才需要 `files` 通道 |
| 默认 Backend 是什么 | `__init__` 的 `StateBackend()` | 默认不碰磁盘 |
| 为什么拒绝 Backend 工厂 | `callable(...)` 检查 | 0.7 移除了工厂用法 |
| `permissions` 和 `_permissions` | 弃用路径 | 0.7.31 弃用，0.9.0 移除 |
| 卸载产物放哪 | 三个 `_prefix` | `large_tool_results` / `conversation_history` / `blobs` |
| 为什么卸载会失效 | `_routes_to_state_backend` | `blobs/` 落在 State 上就自动降级 |
| 怎么少给模型几个工具 | `tools=` + `_create_tools` | 生成阶段排除，不是展示阶段隐藏 |
| 为什么 `read_file` 不能排除 | `__init__` 校验 + 卸载提示词 | 卸载机制依赖它读回来 |
| 工具的统一骨架 | `_create_*_tool` 的四个动作 | `validate_path` → 权限 → Backend → `ToolMessage` |
| 工具描述为什么会变 | `_execute_tool_description` | 按可见的 `grep`/`glob` 切换变体 |
| 工具怎么进入 Agent | `self.tools` | factory 收集 → ToolNode |
| 并行冲突怎么查 | `_parallel_file_mutation_error` | 只看最近一条 AIMessage，保留第一个 |
| 冲突为什么在工具边界 | `wrap_tool_call` | 需要跨工具、跨调用、看全局 |
| 大工具结果怎么卸 | `_intercept_large_tool_result` | 超 `4 × token_limit` 字符就写文件 |
| 为什么 `grep` 不卸载 | `TOOLS_EXCLUDED_FROM_EVICTION` | 自带截断，截断后更像噪声 |
| 模型请求怎么改 | `wrap_model_call` 五步 | 筛工具 → 排消息 → 缩用户消息 → 二进制 → 重试 |
| 为什么默认不加 Prompt | 源码注释 | 会与工具 schema 重复；只加 host 路径映射 |
| 媒体消息为什么重排 | `_move_media_results_after_tool_results` | 同批 `ToolMessage` 必须连续 |
| 大用户消息怎么处理 | `_check_eviction_needed` | 只查最后一条；State 留原文，请求给预览 |
| 为什么需要 `ExtendedModelResponse` | `state_command` 分支 | `override` 改请求，`Command` 才改 State |
| 模型拒绝内容怎么办 | `ModelInvalidRequestError` 分支 | 替换最近一轮的多模态读取后重试一次 |
| 权限怎么判 | `_check_fs_permission` | 按顺序匹配，未命中默认 `allow` |
| 为什么 `execute` 不能配权限 | `_set_permissions` | 工具级检查管不到 shell |
| Hook 输入为什么不进 trace | `trace_policy` | `TracePolicy(process_inputs=omit_payload)` |

### 11.1 三个仍然成立的边界

**第一，`tools=` 是「有没有」，能力门槛是「能不能」。** 前者在装配时决定，后者在每次模型调用时决定。两者都会让模型看不见工具，但只有前者能让工具彻底不存在。

**第二，卸载机制的适用范围比名字暗示的窄得多。** 它叫「工具结果卸载」，但它对本 Middleware 自己的七个文件工具完全不生效；`execute` 和所有外部工具才是它的实际服务对象。

**第三，`permissions` 保护的是工具，不是文件系统。** 一旦 `execute` 在场，路径规则就形同虚设——源码用 `NotImplementedError` 承认了这一点，而不是给一个漏风的实现。

### 11.2 回到第十章

到这里，文件侧的机制已经完整：

```text
第九章      工具调用怎样落到文件上     ToolNode + Backend
第九章附    这个落地过程由谁编排       FilesystemMiddleware
```

我们已经能完整解释一条 `edit_file` 从生成到落盘的全过程，也能说清这个 Middleware 为什么必须存在、它的八种工具共用什么骨架、它的两个 Hook 各自拦什么。

但这条链上还欠着一个前提。

前面所有分析都默认「模型已经知道 `edit_file` 存在」。可一个 Middleware 在 `__init__` 里造出来的 Python 对象，究竟经过几道手，才变成模型 API 请求里的一段 JSON Schema？`ToolNode` 注册了八个工具，为什么模型有时只看见五个，有时又能看见一个从未注册过的工具？

这个问题单独成篇：

**第九章附二：工具是怎么走到模型面前的？——注册、暴露、执行三段链路**

补上这个前提之后，还有一个更根本的问题。

假设模型认为最快的修法是删掉整个测试目录，再跑一遍 pytest。这个调用在结构上合法，Backend 也确实有删除能力——`delete` 工具会照常执行。

**能执行，不等于应该直接执行。**

谁来在工具真正产生副作用之前踩一脚刹车？如果踩了，Agent 怎样暂停、怎样等一个人类看完再决定、批准之后又怎样接着跑而不是从头再来？

这就是第十章的内容：

**第十章：Human-in-the-Loop——工具执行前如何中断、审批与恢复？**

---

## 附：本章源码导航

以下 DeepAgents 链接均固定在 Commit `e85c6b9`：

| 想确认的问题 | 固定源码 |
|---|---|
| 模块定位与公开 API | [`filesystem.py#L1-L140`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L1-L140) |
| 并行冲突检查 | [`filesystem.py#L183-L226`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L183-L226) |
| 媒体消息重排、拒绝内容替换 | [`filesystem.py#L229-L276`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L229-L276) |
| 权限规则与匹配 | [`filesystem.py#L368-L420`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L368-L420) |
| 单次版 reducer（本模块无调用点） | [`filesystem.py#L1241-L1276`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L1241-L1276) |
| 批量 reducer 与三个 State 类 | [`filesystem.py#L1277-L1331`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L1277-L1331) |
| 工具名常量与 `tools=` 取值范围 | [`filesystem.py#L1608-L1612`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L1608-L1612) |
| host 路径映射 Prompt | [`filesystem.py#L1615-L1702`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L1615-L1702) |
| 卸载排除列表及三条理由 | [`filesystem.py#L1727-L1757`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L1727-L1757) |
| 类定义、`trace_policy`、`state_schema` | [`filesystem.py#L1820-L1890`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L1820-L1890) |
| `__init__` 全貌（参数、校验、前缀、降级） | [`filesystem.py#L1892-L2027`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L1892-L2027) |
| `_create_tools` 与 `tools=` 排除 | [`filesystem.py#L2029-L2044`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L2029-L2044) |
| `_set_permissions` 的 `NotImplementedError` | [`filesystem.py#L2053-L2063`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L2053-L2063) |
| 八种工具的工厂 | [`filesystem.py#L2065-L3358`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L2065-L3358) |
| `write_file` 的完整骨架 | [`filesystem.py#L2334-L2373`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L2334-L2373) |
| 工具描述按可见性重写 | [`filesystem.py#L2924-L3043`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L2924-L3043) |
| 能力门槛判断 | [`filesystem.py#L3057-L3089`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L3057-L3089) |
| `wrap_model_call` 五步 | [`filesystem.py#L3362-L3470`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L3362-L3470) |
| 大消息判定与截断 | [`filesystem.py#L3527-L3676`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L3527-L3676) |
| 大结果拦截 | [`filesystem.py#L3759-L3848`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L3759-L3848) |
| `wrap_tool_call` 与 async 版本 | [`filesystem.py#L3850-L3909`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L3850-L3909) |
| 卸载后的占位文本与预览 | [`_message_eviction.py#L31-L44`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/_message_eviction.py#L31-L44)、[`#L253-L295`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/_message_eviction.py#L253-L295) |
| 对照组：Memory 的 `before_agent` | [`memory.py#L278-L310`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/memory.py#L278-L310) |
| 对照组：Skills 的 `before_agent` | [`skills.py#L1188-L1190`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/skills.py#L1188-L1190) |

**本章核心产出：** 读者已经可以解释这个 Middleware 的四个时间点（装配、建图、模型调用前、工具执行时），也能说清它的三个关键取舍——`tools=` 在生成阶段排除而不是展示阶段隐藏、卸载机制对七个文件工具完全不生效、`permissions` 只保护工具而不保护进程。这三点共同说明了同一件事：它的定位是「编排文件能力」，不是「实现文件存储」，也不是「保证文件安全」。
