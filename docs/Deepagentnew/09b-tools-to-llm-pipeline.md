---
title: 第九章附二：工具是怎么走到模型面前的？——注册、暴露、执行三段链路
description: 沿 LangChain 1.4.7 的 factory.py 与 LangGraph 1.2.12 的 ToolNode，把「工具从 Middleware 到 LLM 再到执行」拆成注册、暴露、执行三段：middleware.tools 何时被收集、ToolNode 的 tools_by_name 到底是什么、ModelRequest.tools 为什么每轮都要改、bind_tools 的三条分支与未知工具校验、Send 分发与 ToolCallRequest.override，并对照 model_node 与 ToolNode 的职责边界，最后回答「注册集合与本轮可见集合到底谁大」
tags: [DeepAgents, LangChain, LangGraph, ToolNode, ModelNode, bind_tools, ModelRequest, Middleware, 源码精读]
sidebar_position: 13
---

# 第九章附二：工具是怎么走到模型面前的？

> **本章要回答的问题：** 第九章和第九章附一都在追同一条 `edit_file` 的路径：模型生成 Tool Call、ToolNode 找到工具、Middleware 校验、Backend 落盘。但这两章都默认了一件事——**模型已经知道 `edit_file` 存在**。这个前提本身是怎么成立的？一个 Middleware 在 `__init__` 里造出来的 Python 对象，究竟经过几道手，才变成模型 API 请求里的一段 JSON Schema？

## 一、三个被混在一起的问题

这条链之所以难追，不是因为它长，而是因为**三个不同的过程被同一个名词盖住了**。

我们平时说「工具有没有给模型」，实际上可能在说三件完全不同的事：

1. `FilesystemMiddleware.self.tools` 里那八个对象，是谁、在什么时候收集的？
2. `ToolNode` 注册了工具，是不是它把工具信息交给了 LLM？
3. 为什么 `wrap_model_call()` 也能增删工具？它和 `bind_tools()` 谁说了算？
4. `bind_tools()` 到底什么时候调用？是不是构建 Agent 时就调用一次？
5. `_get_bound_model()` 和 `model_node()` 是什么关系？前者是 Graph 节点吗？

这五个问题看起来分散，其实只涉及三个过程。

| 过程 | 回答的问题 | 由谁负责 | 发生在什么时候 |
|---|---|---|---|
| **注册** | 这个 Agent 一共能执行哪些工具？ | `create_agent()` + `ToolNode` | 建图阶段，一次 |
| **暴露** | 这一轮模型能看见哪些工具？ | `ModelRequest.tools` + `wrap_model_call()` + `bind_tools()` | 每次模型调用 |
| **执行** | 模型开口要的这个工具，谁来真正跑？ | `ToolNode` + `wrap_tool_call()` | 每个 Tool Call |

三者的关系可以先用一句话钉住：

> **注册决定「能不能执行」，暴露决定「看不看得见」，执行决定「跑不跑得成」。**

而最容易记错的一点是：

> **`ToolNode` 从头到尾没有把任何工具信息交给模型。** 它只在模型已经开口要某个工具之后才出场。

### 1.1 为什么这三个过程特别容易混

因为它们收的是**同一个对象**。

```python
# 给 ToolNode：注册执行能力
tool_node = ToolNode([read_file])

# 给模型：提供工具定义
bound_model = model.bind_tools([read_file])
```

同一个 `read_file` 对象，进了两个完全不同的容器。容器 A 关心它「怎么被调用」，容器 B 关心它「长什么样」。而两边的代码都写 `[read_file]`，读起来几乎一样。

### 1.2 阅读版本及源码边界

本章固定：

```text
DeepAgents Python: 0.7.32
DeepAgents Commit: e85c6b9
LangChain:          1.4.7
LangChain Commit:   fbb8a35
LangGraph:          1.2.12
```

这条链横跨三个仓库，所以核查范围也比前两章宽：

- [`langchain/agents/factory.py`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/factory.py)（2164 行）——收集、建图、`model_node`、`_get_bound_model`
- [`langchain/agents/middleware/types.py`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/types.py)（2201 行）——`ModelRequest`、`ModelResponse`
- [`langgraph/prebuilt/tool_node.py`](https://github.com/langchain-ai/langgraph/blob/1.2.12/libs/prebuilt/langgraph/prebuilt/tool_node.py)（2030 行）——`ToolNode`、`ToolCallRequest`
- [`deepagents/middleware/filesystem.py`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py)——一个真实的过滤样本
- [`deepagents/middleware/skills.py`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/skills.py) 与 `_skill_tools.py`——一个真实的追加样本
- [`deepagents/middleware/_tool_exclusion.py`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/_tool_exclusion.py)——一个真实的剔除样本

本章所有带「**真实源码节选**」的代码均可在所附固定链接中定位；允许省略不影响所讨论机制的语句，但不改变分支的核心语义。带「**教学化简**」和「**概念伪代码**」的片段用于推导，不冒充仓库原文。

> [!NOTE] 一个路径上的坑
> 如果你按 `langchain.tools.tool_node` 去找 `ToolNode` 的实现，会只看到 20 行转发代码。真正的实现在 LangGraph 仓库的 `libs/prebuilt/langgraph/prebuilt/tool_node.py`。LangChain 只是把 `ToolNode`、`ToolCallRequest`、`ToolCallWrapper` 重新导出，方便调用方少装一个依赖。

### 1.3 一张全局地图：四个时间点

```text
create_deep_agent(...)
        |
        |  ① 装配：Middleware 造出工具实例
        +--------------------------------------------------+
        |  FilesystemMiddleware.__init__                    |
        |      self.tools = self._create_tools()            |
        +--------------------------------------------------+
        |
        |  ② 建图：create_agent 收集 → ToolNode 注册 → 条件边
        +--------------------------------------------------+
        |  middleware_tools + regular_tools                 |
        |      → ToolNode(tools=...)                        |
        |      → tool_node.tools_by_name                    |
        |      → default_tools                              |
        |  graph.add_node("tools", tool_node)               |
        |  graph.compile()                                  |
        +--------------------------------------------------+
        |
        |  ③ 每轮模型调用
        +--------------------------------------------------+
        |  model_node()                                     |
        |      ModelRequest(tools=default_tools)            |
        |      wrap_model_call 链（可增删 tools）            |
        |      _execute_model_sync()                        |
        |          _get_bound_model() → bind_tools()        |
        |          model.invoke()                           |
        +--------------------------------------------------+
        |
        |  ④ 模型返回 tool_calls 之后
        +--------------------------------------------------+
        |  条件边 → Send("tools", [tool_call])              |
        |  ToolNode._run_one()                              |
        |      tools_by_name.get(name) → ToolCallRequest    |
        |      wrap_tool_call 链（可 override(tool=...)）    |
        |      tool.invoke()                                |
        +--------------------------------------------------+
```

| 时间点 | 发生次数 | 这一阶段产出的东西 |
|---|---|---|
| ① 装配 | 每个 Agent 一次 | 工具**实例**（Python 对象） |
| ② 建图 | 每个 Agent 一次 | 注册表 `tools_by_name` + 默认可见列表 `default_tools` |
| ③ 模型调用 | 每轮一次（一轮可能有多次） | 一次 `bind_tools()` 调用 + 一次网络请求 |
| ④ 工具执行 | 每个 Tool Call 一次 | 一条 `ToolMessage` |

后面五节按这个顺序走。第二节先讲一个前置问题：为什么同一个对象能同时服务两个容器。

---

## 二、同一个 Tool 对象，两种用途

### 2.1 `BaseTool` 到底装了什么

`BaseTool` 是一个「带元数据的可调用包装」。对它来说，真正重要的只有四样东西：

| 成员 | 类型 | 谁在用 |
|---|---|---|
| `name` | `str` | 两方都用：注册表的键、Schema 的函数名 |
| `description` | `str` | **主要是模型**：它就是模型看到的工具说明 |
| `args_schema` | Pydantic 模型 | 两方都用：ToolNode 用它校验参数，`bind_tools` 用它生成 JSON Schema |
| `invoke()` | 方法 | **只有 ToolNode**：真正执行 |

四样东西里，只有 `invoke()` 是「执行」专用的。其余三样都是「描述」，谁想读都能读。

这解释了一个常见困惑：为什么改了工具的 docstring，模型的行为会变？

因为 docstring 会被 `create_tool` 转成 `description`，然后随 `bind_tools` 一起进请求。模型看到的不是代码，是这段文字。

反过来，为什么给工具加了一行 `print`，模型完全不知道？

因为函数体从来不出现在请求里。

> [!NOTE] 模型看不到函数实现
> 模型 API 的 `tools` 参数只接受名字、描述、参数 Schema 三样。它不会、也无法接收 Python 源码。所以「模型读了工具源码所以会用」这个说法是错的——它只是读了一段写得很好的说明。

### 2.2 两个容器各取所需

```text
                  ┌──────────────────────────────┐
                  │  同一个 BaseTool 实例         │
                  │  name / description / schema │
                  │  invoke()                    │
                  └───────────┬──────────────────┘
                              │
              ┌───────────────┴────────────────┐
              ▼                                ▼
   ┌────────────────────┐          ┌────────────────────────┐
   │ ToolNode            │          │ bind_tools()           │
   │  取 invoke()        │          │  取 name/desc/schema   │
   │  取 args_schema     │          │  序列化成 JSON Schema  │
   │  用途：执行          │          │  用途：告诉模型有什么   │
   └────────────────────┘          └────────────────────────┘
```

所以「ToolNode 注册了工具，就等于模型知道这些工具」这个推论，在逻辑上是不成立的：注册表里存的是**执行能力**，而模型看到的是**另一份序列化结果**。这两份东西由两段代码分别生成。

### 2.3 一个几乎所有人都问过的问题：工具定义是拼进 System Prompt 的吗

不是。

`bind_tools()` 的产物会随请求走模型 API 的 `tools` 参数（各家字段名不同，但语义一致）。System Prompt 是另一个字段，里面不该有工具 Schema。

这个区分在排查问题时很关键：

| 症状 | 说明问题在哪 |
|---|---|
| 模型说「我没有这个工具」 | `tools` 参数里没有它 → 暴露环节 |
| 模型知道工具名，但参数总写错 | `tools` 里有它，但 `args_schema` 描述不清 |
| 模型在正文里「假装调用」工具 | `tools` 参数为空，模型只能靠文字模拟 |

有一类例外需要单独记住：**Skill 的 inline disclosure**。DeepAgents 的 `SkillsMiddleware` 在某些模型上会把工具说明直接插进消息流，而不是走 `tools` 参数。这条路我们在第八章附 5.4 节会看到它的源码，它也是本章唯一一处「工具定义不在 `tools` 里」的地方。

---

## 三、时间点一：装配——`middleware.tools` 从哪来

### 3.1 工具实例的出生

**真实源码节选：** [`filesystem.py#L2027`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L2027)、[`#L2029-L2044`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L2029-L2044)

```python
self.tools = self._create_tools()

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

三件事在这里一次定死：

1. **工具是实例，不是类。** `factory()` 每次返回一个新的 `BaseTool`，闭包捕获的是**这个** Middleware 实例的 `self`。所以同一个 `write_file` 名字，在两个 Agent 上指向两个不同对象、两套不同 Backend。
2. **`tools=` 白名单在生成阶段生效。** 不在名单里的工具根本不创建。注释写得很直白：它们「never reaches the dispatchable tool node」——连 ToolNode 都到不了。这条在第九章附一 3.7 节已经展开过，这里只需要记住它对本章的意义：**注册集的上限，在装配时就已经被削掉一层。**
3. **装配结束时，工具对象已经全部就位。** 后面所有环节都只是「搬运」和「筛选」，不会再造新工具（Skill 的动态工具是唯一例外，它是运行期由 resolver 现造的）。

### 3.2 `create_deep_agent()` 侧的两道处理

Middleware 造完工具之后，`create_deep_agent` 还要做两件事，然后才交给 `create_agent`。

**真实源码节选：** [`graph.py#L666-L669`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py#L666-L669)

```python
# Copy of `tools` with any harness-specific description rewrites.
# (Tool exclusion is handled by _ToolExclusionMiddleware which filters
# all tools (user-supplied and middleware-injected) in one place.)
_tools = _apply_tool_description_overrides(
    tools,
    _profile.tool_description_overrides,
)
```

第一道是**改描述**。注意它只动 `description`，不动成员资格：一个工具不会因为描述被改写就从注册表里消失。

**真实源码节选：** [`graph.py#L1008-L1011`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py#L1008-L1011)

```python
return create_agent(
    model,
    system_prompt=final_system_prompt,
    tools=_tools,
    middleware=deepagent_middleware,
```

第二道是**交接**。`deepagent_middleware` 是一个列表，里面装着 `FilesystemMiddleware`、`SkillsMiddleware`、`MemoryMiddleware`、`SummarizationMiddleware` 等等。`create_agent` 拿到的就是这个列表，外加用户自己传的 `tools`。

到这一刻为止，模型还什么都没看见。

### 3.3 装配结束时的状态

| 问题 | 答案 |
|---|---|
| 工具对象存在吗 | 存在，在 `middleware.tools` 里 |
| 在哪个容器里 | 一个普通 Python list |
| 模型看得见吗 | **看不见**，还没有任何序列化发生 |
| 能执行吗 | 还不能，没有 ToolNode |
| 谁持有它们 | 各自的 Middleware 实例 |

这张表的意义是：**装配阶段只解决「存在」，不解决「可见」和「可执行」。** 后两件事分别由第四节（注册，决定可执行）和第五节（每轮筛选，决定可见）接管。

---

## 四、时间点二：建图——收集与注册

### 4.1 收集：三类工具，两个去向

**真实源码节选：** [`factory.py#L1089`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/factory.py#L1089)、[`#L1130-L1137`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/factory.py#L1130-L1137)

```python
middleware_tools = [t for m in middleware for t in getattr(m, "tools", [])]

# Setup tools
tool_node: ToolNode | None = None
# Extract built-in provider tools (dict format) and regular tools (BaseTool/callables)
built_in_tools = [t for t in tools if isinstance(t, dict)]
regular_tools = [t for t in tools if not isinstance(t, dict)]

# Tools that require client-side execution (must be in ToolNode)
available_tools = middleware_tools + regular_tools
```

`getattr(m, "tools", [])` 这个写法说明：`tools` 不是 `AgentMiddleware` 的必备属性。没有工具的 Middleware（比如 `MemoryMiddleware`）根本不需要定义它。这也是为什么 `FilesystemMiddleware` 要在 `__init__` 里显式赋值，而不是用 `@property`——它需要的是「装配时生成一次」的语义。

三类工具的归宿完全不同：

| 来源 | 类型 | 进 ToolNode 吗 | 进模型请求吗 |
|---|---|---|---|
| `middleware.tools` | `BaseTool` | 进 | 进（作为默认值） |
| 用户 `tools`（普通工具） | `BaseTool` / 可调用对象 | 进 | 进（作为默认值） |
| 用户 `tools`（provider 内置） | `dict` | **不进** | 进 |

第三行值得单独说。像 `{"type": "web_search"}` 这种字典，是交给 provider 自己执行的工具。它不需要本地函数，所以不进 ToolNode；但它必须出现在 `tools` 参数里，所以进模型请求。

这就提前预告了第九节的结论：**「模型可见集合」和「ToolNode 注册集合」本来就不要求相等**，源码从第一行就在区分它们。

### 4.2 `ToolNode` 什么时候创建

**真实源码节选：** [`factory.py#L1139-L1152`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/factory.py#L1139-L1152)

```python
# Create ToolNode if we have client-side tools OR if middleware defines wrap_tool_call
# (which may handle dynamically registered tools)
tool_node = (
    ToolNode(
        tools=available_tools,
        wrap_tool_call=wrap_tool_call_wrapper,
        awrap_tool_call=awrap_tool_call_wrapper,
    )
    if available_tools or wrap_tool_call_wrapper or awrap_tool_call_wrapper
    else None
)
```

条件是三个「或」：

1. 有本地工具；
2. 或者有同步 `wrap_tool_call`；
3. 或者有异步 `awrap_tool_call`。

第 2、3 条容易被忽略。它意味着：**即使一个 Agent 一个工具都没有，只要某个 Middleware 实现了 `wrap_tool_call`，ToolNode 仍然会被创建。** 理由写在注释里——那个 Middleware 可能就是靠它来执行「运行期才出现」的工具的。

反过来，如果三个条件都不满足，`tool_node` 就是 `None`，后面建图时不会加 `"tools"` 节点，条件边也不会加。Agent 退化成一个纯粹的「模型 → 结束」循环。

### 4.3 注册的细节：`tools_by_name` 到底是什么

**真实源码节选：** [`tool_node.py#L779-L791`](https://github.com/langchain-ai/langgraph/blob/1.2.12/libs/prebuilt/langgraph/prebuilt/tool_node.py#L779-L791)

```python
for tool in tools:
    if not isinstance(tool, BaseTool):
        tool_ = create_tool(cast("type[BaseTool]", tool))
    else:
        tool_ = tool
    self._tools_by_name[tool_.name] = tool_
    # Build injected args mapping once during initialization in a single pass
    self._injected_args[tool_.name] = _get_all_injected_args(tool_)

@property
def tools_by_name(self) -> dict[str, BaseTool]:
    """Mapping from tool name to BaseTool instance."""
    return self._tools_by_name
```

四个事实：

**第一，`tools_by_name` 是字典，键是工具名。** 不是列表，不是集合。这意味着同名工具会互相覆盖。

**第二，覆盖方向是可推导的。** `available_tools = middleware_tools + regular_tools`，普通工具排在后面，循环按顺序赋值，所以**同名时用户工具会盖掉 Middleware 工具**。源码没有做重名检查——`create_agent` 只对 Middleware 实例本身的重名抛 `AssertionError`（`factory.py#L1160-L1162`），对工具名没有。

这个行为平时不显眼，但在定制 DeepAgents 时会遇到：如果你想用自定义的 `read_file` 替换掉 `FilesystemMiddleware` 的那一个，直接传同名工具就能生效。如果你只是想「多一个」，那同名会静默失败。

**第三，普通可调用对象在这里被转换。** `create_tool` 会给裸函数推断出 `args_schema` 和 `description`。所以「ToolNode 里存的是 BaseTool」这句话，对用户传的裸函数也成立。

**第四，`_injected_args` 是预计算的。** 哪些参数要注入 `state`、`store`、`runtime`，在注册时就分析好，执行时不用再算。这是纯粹的性能取舍。

### 4.4 `default_tools`：默认可见集合从哪来

**真实源码节选：** [`factory.py#L1154-L1158`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/factory.py#L1154-L1158)

```python
if tool_node:
    default_tools = list(tool_node.tools_by_name.values()) + built_in_tools
else:
    default_tools = list(built_in_tools)
```

这一行是本章最值得停下来看的地方。

一个自然的猜想是：默认可见工具 = 用户传进来的 `tools` + Middleware 的 `tools`。也就是「原始输入」。

源码不是这样。它从 **ToolNode 的注册表**里取值。

差别在哪？

| 猜想 | 源码 |
|---|---|
| `default_tools = middleware_tools + regular_tools + built_in_tools` | `default_tools = list(tool_node.tools_by_name.values()) + built_in_tools` |
| 裸函数以原样出现在 `request.tools` | 裸函数已被 `create_tool` 转成 `BaseTool` |
| 同名工具会出现两次 | 同名工具只出现一次（后到者胜） |
| 顺序等于输入顺序 | 顺序等于字典插入顺序，去重后的顺序 |

源码注释把理由写清楚了：

```text
# Default tools for ModelRequest initialization
# Use converted BaseTool instances from ToolNode (not raw callables)
# Include built-ins and converted tools (can be changed dynamically by middleware)
# Structured tools are NOT included - they're added dynamically based on response_format
```

四条理由，逐条看：

- **「Use converted BaseTool instances from ToolNode (not raw callables)」**——`request.tools` 里如果混着裸函数，`wrap_model_call` 就没法用统一方式读 `tool.name`。而 Middleware 的全部工作都建立在「能按名字识别工具」上（第九章附一 5.5 节的 `_tool_name` 就是这个模式）。
- **「Include built-ins」**——`dict` 型 provider 工具不在 ToolNode 里，得单独加回来。
- **「can be changed dynamically by middleware」**——`default_tools` 只是初值，不是终值。
- **「Structured tools are NOT included」**——结构化输出的工具不是真工具，是 schema 载体，要等到 `_get_bound_model` 里按 `response_format` 决定加不加。这一点第六节会看到。

> [!NOTE] 一个容易被误读的细节
> 因为 `default_tools` 来自 `tools_by_name.values()`，**同名工具在默认可见集合里也只保留一个**。所以「用户工具覆盖 Middleware 工具」不只是执行时覆盖，模型看到的描述也一起被换掉了。两条链路在这里是同步的，因为它们本来就指向同一个对象。

### 4.5 建图：节点、条件边、`Send`

工具注册完之后，图是这样接起来的。

**真实源码节选：** [`factory.py#L1611-L1615`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/factory.py#L1611-L1615)

```python
graph.add_node("model", RunnableCallable(model_node, amodel_node, trace=False))

# Only add tools node if we have tools
if tool_node is not None:
    graph.add_node("tools", tool_node)
```

**真实源码节选：** [`factory.py#L1744-L1786`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/factory.py#L1744-L1786)

```python
if tool_node is not None:
    tools_to_model_destinations = [loop_entry_node]
    if (
        any(tool.return_direct for tool in tool_node.tools_by_name.values())
        or structured_output_tools
    ):
        tools_to_model_destinations.append(exit_node)

    graph.add_conditional_edges(
        "tools",
        RunnableCallable(
            _make_tools_to_model_edge(...),
            trace=False,
        ),
        tools_to_model_destinations,
    )

    model_to_tools_destinations = ["tools", exit_node]
    if response_format or loop_exit_node != "model" or middleware_w_wrap_model_call:
        model_to_tools_destinations.append(loop_entry_node)

    graph.add_conditional_edges(
        loop_exit_node,
        RunnableCallable(
            _make_model_to_tools_edge(...),
            trace=False,
        ),
        model_to_tools_destinations,
    )
```

两个方向各有一条条件边，而且都跟「有没有工具」绑在一起。

- 没有工具 → 不加 `"tools"` 节点，`model` 直接连到结束。
- 有工具 → `model` 的出口由 `_make_model_to_tools_edge` 决定。

**真实源码节选：** [`factory.py#L2008-L2043`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/factory.py#L2008-L2043)

```python
last_ai_message, tool_messages = _fetch_last_ai_and_tool_messages(state["messages"])

# 2. if no AIMessage exists (e.g., messages were cleared), exit the loop
if last_ai_message is None:
    return end_destination

tool_message_ids = [m.tool_call_id for m in tool_messages]

# 3. If the model hasn't called any tools, exit the loop
# this is the classic exit condition for an agent loop
if len(last_ai_message.tool_calls) == 0:
    return end_destination

pending_tool_calls = [
    c
    for c in last_ai_message.tool_calls
    if c["id"] not in tool_message_ids and c["name"] not in structured_output_tools
]

# 4. If there are pending tool calls, jump to the tool node.
# The tool node hydrates ToolRuntime.state from channels via
# CONFIG_KEY_READ at execution time, so we no longer inline the
# full state into each Send (previously O(N^2) in TASKS writes).
if pending_tool_calls:
    return [Send("tools", [tool_call]) for tool_call in pending_tool_calls]

# 5. If a fresh structured response was produced this call, exit the loop
if state.get("structured_response") is not None:
    return end_destination

# 6. AIMessage has tool calls, but there are no pending tool calls which suggests
# the injection of artificial tool messages. Jump to the model node
return model_destination
```

注意第 4 步返回的是**一个 `Send` 列表，每个 `Send` 只带一个 tool_call**。不是把整条 `AIMessage` 丢给 ToolNode。

这个设计有两个后果：

1. **同一轮的多个 tool_call 会并发执行。** LangGraph 会为每个 `Send` 起一个任务。第九章讲过的「并行同路径修改检查」正是为了这个。
2. **在 `create_agent` 里，`ToolNode` 一次只看到一个 call。** 所以它拿到 `state` 靠的不是输入里的快照，而是运行期从通道读（注释里那句 `CONFIG_KEY_READ`）。

第 6 步那句注释也值得记：如果 `AIMessage` 里有 tool_calls，但每一个都已有对应 `ToolMessage`，说明这些 ToolMessage 是「人造的」（比如 HITL 拒绝时补的），那就回模型继续，而不是再执行一遍。

### 4.6 建图完成时的三个集合

```text
assembled_tools   = middleware.tools + user tools        （对象层面，装配产出）
tools_by_name     = {name: BaseTool}                     （注册表，建图产出）
default_tools     = tools_by_name.values() + built_ins   （默认可见列表，建图产出）
```

| 集合 | 数量关系 | 用途 |
|---|---|---|
| `assembled_tools` | 最多（含被覆盖的同名项） | 无，只是中间量 |
| `tools_by_name` | 去重后 | 执行时的查找表 |
| `default_tools` | 等于 `tools_by_name` + `built_ins` | 每轮 `ModelRequest` 的初值 |

到这一刻，模型依然什么都没看见。`bind_tools` 还没被调用过一次。

---

## 五、时间点三：模型调用前——`ModelRequest` 与 `wrap_model_call`

### 5.1 `ModelRequest` 的真实字段

**真实源码节选：** [`types.py#L88-L105`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/types.py#L88-L105)

```python
class ModelRequest(Generic[ContextT]):
    model: BaseChatModel
    messages: list[AnyMessage]  # excluding system message
    system_message: SystemMessage | None
    tool_choice: Any | None
    tools: list[BaseTool | dict[str, Any]]
    response_format: ResponseFormat[Any] | None
    state: AgentState[Any]
    runtime: Runtime[ContextT]
    model_settings: dict[str, Any] = field(default_factory=dict)
```

三个细节值得单独指出：

- **`messages` 不含 system message。** 系统消息是独立字段，拼接发生在 `_execute_model_sync` 里（第六节会看到）。这也是为什么 `wrap_model_call` 改系统提示要用 `override(system_message=...)`，而不是往 `messages[0]` 里塞。
- **`tools` 的类型是 `list[BaseTool | dict[str, Any]]`。** 那个 `dict` 分支就是 provider 内置工具。写 Middleware 时如果只处理 `BaseTool`，遇到 `dict` 会崩——`_ToolExclusionMiddleware` 里专门写了一个 `_tool_name` 来兼容两种形态，原因就在这里。
- **`state` 和 `runtime` 都在里面。** 所以 `wrap_model_call` 能读到完整 State。Skill 的「读过哪些 skill」就是从 `request.state` 里读的。

### 5.2 `model_node` 的三步

**真实源码节选：** [`factory.py#L1520-L1547`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/factory.py#L1520-L1547)

```python
def model_node(state: AgentState[Any], runtime: Runtime[ContextT]) -> list[Command[Any]]:
    """Sync model request handler with sequential middleware processing."""
    messages = _patch_invalid_tool_calls(state["messages"])
    request = ModelRequest(
        model=model,
        tools=default_tools,
        system_message=system_message,
        response_format=initial_response_format,
        messages=messages,
        tool_choice=None,
        state=state,
        runtime=runtime,
    )

    has_structured_output = initial_response_format is not None
    if wrap_model_call_handler is None:
        model_response = _execute_model_sync(request)
        return _build_commands(...)

    result = wrap_model_call_handler(request, _execute_model_sync)
    return _build_commands(
        result.model_response,
        result.commands,
        has_structured_output=has_structured_output,
        repaired_messages=messages if messages != state["messages"] else None,
    )
```

三步，每步都有理由。

**第一步：`_patch_invalid_tool_calls`。**

**真实源码节选：** [`factory.py#L692-L707`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/factory.py#L692-L707)

```python
def _patch_invalid_tool_calls(messages: Sequence[AnyMessage]) -> list[AnyMessage]:
    answered_ids = {
        message.tool_call_id for message in messages if isinstance(message, ToolMessage)
    }
    patched_messages: list[AnyMessage] = []
    for message in messages:
        patched_messages.append(message)
        if not isinstance(message, AIMessage):
            continue
        for tool_call in message.invalid_tool_calls:
            if tool_call.get("id") in answered_ids:
                continue
            if tool_message := _invalid_tool_call_message(tool_call):
                patched_messages.append(tool_message)
                answered_ids.add(tool_message.tool_call_id)
    return patched_messages
```

它在发送前把「不合法的 tool_call」补上对应的错误 `ToolMessage`。为什么要做这件事？因为多数模型 API 要求**每个 tool_call 都有对应的 tool 结果**，否则直接报 400。历史里如果残留了这种残缺对（比如参数校验失败的调用被上层截断了），不补就会让整轮请求失败。

注意它的返回值最后被当作 `repaired_messages` 传给 `_build_commands`——只有真的修补过，才写回 State。所以这是「发送副本」而不是「污染历史」。

**第二步：构造 `ModelRequest`。**

这里 `tools=default_tools`。记住这个赋值：它是**引用**，不是拷贝。

**第三步：交给 handler 链。**

有 `wrap_model_call_handler` 就交给它，没有就直接 `_execute_model_sync`。

这里回答了本章开头第五个问题的一半：**`model_node` 是 Graph 节点；`_get_bound_model` 不是。** 后者是闭包里的辅助函数，只在 `_execute_model_sync` 内部被调用。

### 5.3 一个必须说清的坑：`default_tools` 是共享列表

**真实源码节选：** [`types.py#L146-L156`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/types.py#L146-L156)

```python
with warnings.catch_warnings():
    warnings.simplefilter("ignore", category=DeprecationWarning)
    self.model = model
    self.messages = messages
    self.system_message = system_message
    self.tool_choice = tool_choice
    self.tools = tools if tools is not None else []
    self.response_format = response_format
    self.state = state if state is not None else {"messages": []}
    self.runtime = runtime  # type: ignore[assignment]
    self.model_settings = model_settings if model_settings is not None else {}
```

`self.tools = tools if tools is not None else []`——没有 `list(tools)`。

于是：

```text
建图时：  default_tools = [ls, read_file, write_file, ...]      ← 一个 list 对象
第 1 轮： ModelRequest(tools=default_tools)                      ← 同一个 list 对象
第 2 轮： ModelRequest(tools=default_tools)                      ← 还是同一个 list 对象
```

如果某个 Middleware 这样写：

```python
request.tools.append(extra_tool)   # 错：改的是共享列表
return handler(request)
```

那么第 1 轮加进去的工具，会永久留在 `default_tools` 里，第 2 轮、第 3 轮全都带着它。而且因为列表只增不减，多轮之后可能出现重复项。

正确写法是：

```python
request = request.override(tools=[*request.tools, extra_tool])
return handler(request)
```

**真实源码节选：** [`types.py#L167-L197`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/types.py#L167-L197)

```python
def __setattr__(self, name: str, value: Any) -> None:
    # Special handling for system_prompt - convert to system_message
    if name == "system_prompt":
        warnings.warn(...)
        ...
        return

    warnings.warn(
        f"Direct attribute assignment to ModelRequest.{name} is deprecated. "
        f"Use request.override({name}=...) instead to create a new request "
        f"with the modified attribute.",
        DeprecationWarning,
        stacklevel=2,
    )
    object.__setattr__(self, name, value)
```

源码把「直接赋值」标成 deprecated，表面上是个风格约定，实际上是在防这个坑：`request.tools = [...]` 只改当前 request，不改共享列表，所以「看起来对了」；而 `request.tools.append(...)` 不改 `__setattr__` 的路径，warning 根本不会触发。

这就是为什么本章反复强调 `override`：它不是更优雅，而是**唯一不会跨轮泄漏的写法**。

### 5.4 handler 链的顺序

**真实源码节选：** [`factory.py#L281-L291`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/factory.py#L281-L291)

```python
def _chain_model_call_handlers(
    handlers: Sequence[_ModelCallHandler[ContextT]],
) -> _ComposedModelCallHandler[ContextT] | None:
    """Compose multiple `wrap_model_call` handlers into single middleware stack.

    Composes handlers so first in list becomes outermost layer. Each handler receives a
    handler callback to execute inner layers. Commands from each layer are accumulated
    into a list (inner-first, then outer) without merging.

    Args:
        handlers: List of handlers.

            First handler wraps all others.
```

两个规则：

| 规则 | 含义 | 后果 |
|---|---|---|
| 列表第一个 = 最外层 | 最先拿到 request，最后拿到 response | 它看到的是「原始」请求 |
| Commands 内层先、外层后累积 | 状态更新按从内到外排列 | 外层可以覆盖内层的同名字段 |

**真实源码节选：** [`factory.py#L1207-L1214`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/factory.py#L1207-L1214)

```python
# Compose wrap_model_call handlers into a single middleware stack (sync)
wrap_model_call_handler = None
if middleware_w_wrap_model_call:
    sync_handlers = [
        traceable(name=f"{m.name}.wrap_model_call", **_wrap_trace_kwargs(m))(m.wrap_model_call)
        for m in middleware_w_wrap_model_call
    ]
    wrap_model_call_handler = _chain_model_call_handlers(sync_handlers)
```

顺序就是 `middleware` 列表的顺序。而 `middleware` 列表的顺序，是 `create_deep_agent` 按固定规则拼出来的（第九章附一 7.1 节给过对照表）。

### 5.5 三个真实的「改 tools」样本

理论说完，看三个实际实现。它们分别对应「减」「减」「加」。

#### 样本一：`FilesystemMiddleware` 按 Backend 能力过滤

**真实源码节选：** [`filesystem.py#L3376-L3391`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L3376-L3391)

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
```

它做了两件事，而且是分开的两次 `override`：

1. **移除**：Backend 不支持 `execute` / `delete` 时，把它们从 `request.tools` 里删掉。
2. **改写描述**：`grep` 的描述里有一段讲「可以用 execute 跑真正的正则」，当 `execute` 不在时这段必须换掉，否则模型会去调一个看不见的工具。

注意第 2 步用 `is not` 判断，而不是 `!=`。`_with_filtered_grep_description` 在没有改动时返回原列表对象，所以这个比较是「有没有新建列表」，不是「内容是否相等」。这是性能优化，也是真实源码里常见的一种模式。

这个样本正好印证了第九章附一 4.6 节的结论：**`tools=` 的排除在装配阶段，能力门槛的过滤在模型调用前。** 两套过滤都在「让模型看不见」，但一个改的是「工具存不存在」，一个改的是「这一轮给不给看」。

#### 样本二：`_ToolExclusionMiddleware` 整体剔除

**真实源码节选：** [`_tool_exclusion.py#L47-L56`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/_tool_exclusion.py#L47-L56)

```python
def wrap_model_call(
    self,
    request: ModelRequest[Any],
    handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
) -> ModelResponse[Any]:
    """Filter excluded tools before they reach the model."""
    if self._excluded:
        filtered = [t for t in request.tools if _tool_name(t) not in self._excluded]
        request = request.override(tools=filtered)
    return handler(request)
```

极简：读名字、过滤、`override`、放行。

它的价值不在实现，而在**位置**。`graph.py#L986-L989` 把它 append 在最后：

```text
# Tool exclusion runs after custom middleware so excluded tool names are
# stripped last and cannot be restored by a custom wrap_model_call.
if _profile.excluded_tools:
    deepagent_middleware.append(_ToolExclusionMiddleware(excluded=_profile.excluded_tools))
```

「append 在最后」意味着它在 handler 链里是**最内层**，也就是最晚执行的那个。外层的自定义 Middleware 就算把工具加回来，也会被它最后削掉。

这是一个值得记住的设计原则：

> **在 `wrap_model_call` 链里，「最后说话的人」是最内层，不是最外层。** 因为每一层都是在「已经改好的 request」上继续改。

顺带说明：这个 Middleware 还有 `wrap_tool_call` 侧的拒绝逻辑（`_rejection`），因为「模型看不见」不等于「模型不会调」。历史消息里可能残留对已排除工具的调用，或者模型被诱导着硬写一个名字。执行边界必须再拦一次。

#### 样本三：`SkillsMiddleware` 追加动态工具

**真实源码节选：** [`skills.py#L1300-L1321`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/skills.py#L1300-L1321)

```python
def _disclose_skill_tools(
    self, request: ModelRequest[ContextT], reads: list[_SkillRead], resolved: Mapping[str, Sequence[BaseTool]]
) -> tuple[ModelRequest[ContextT], dict[str, str]]:
    """Disclose what the read skills' include names produce."""
    disclosure = _plan_disclosure(reads, request.tools, resolved)
    _discard_rejected_schemas(disclosure, request.model)
    record = disclosure.record()
    if not disclosure.anchors:
        return request, record
    build = _inline_block_builder(request.model)
    if build is None:
        return request.override(tools=_bind_disclosures(request.tools, disclosure)), record
    return request.override(messages=_insert_disclosures(request.messages, disclosure, build)), record
```

这里出现了两条路，第八章附 5.4 节埋的伏笔在这里兑现：

- **有 inline builder**（某些模型支持把工具说明写进消息）→ 改 `messages`，工具定义不进 `tools` 参数。
- **没有** → 改 `tools`，走正常的 `bind_tools` 路径。

**真实源码节选：** [`_skill_tools.py#L342-L352`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/_skill_tools.py#L342-L352)

```python
def _bind_disclosures(request_tools: Sequence[BaseTool | dict[str, Any]], disclosure: _Disclosure) -> list[BaseTool | dict[str, Any]]:
    """Return `request_tools` with deferred disclosures undeferred and gated ones appended."""
    tools: list[BaseTool | dict[str, Any]] = []
    for tool in request_tools:
        if isinstance(tool, BaseTool) and disclosure.deferred.get(tool.name) is tool:
            # A copy without `defer_loading`, so its schema is sent up front.
            extras = {key: value for key, value in (tool.extras or {}).items() if key != _DEFER_LOADING}
            tools.append(tool.model_copy(update={"extras": extras}))
        else:
            tools.append(tool)
    return [*tools, *(disclosure.gated[name] for name in sorted(disclosure.gated))]
```

两个动作：

1. **把 `defer_loading` 摘掉。** 有些工具本来就注册了，但带着「先别发 schema」的标记。Skill 读过之后，把标记去掉，它就会在下一轮正常出现。
2. **把 gated 的工具 append 到末尾。** 这些工具**从来没进过 ToolNode**——它们是 resolver 在运行期现造的。

第 2 点就是本章最重要的分水岭：

> **模型可见集合可以比 ToolNode 注册集合更大。**

因为 `_bind_disclosures` 往 `request.tools` 里塞的对象，`tools_by_name` 里根本没有。

那模型调它的时候怎么办？第七节讲 `wrap_tool_call` 怎么补票。

---

## 六、时间点三后半：`bind_tools()` 到底什么时候发生

### 6.1 `_execute_model_sync` 与 `_get_bound_model` 的关系

**真实源码节选：** [`factory.py#L1493-L1514`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/factory.py#L1493-L1514)

```python
def _execute_model_sync(request: ModelRequest[ContextT]) -> ModelResponse:
    """Execute model and return response.

    This is the core model execution logic wrapped by `wrap_model_call` handlers.

    Raises any exceptions that occur during model invocation.
    """
    # Get the bound model (with auto-detection if needed)
    model_, effective_response_format = _get_bound_model(request)
    messages = request.messages
    if request.system_message:
        messages = [request.system_message, *messages]

    output = model_.invoke(messages)
    if name:
        output.name = name

    # Handle model output to get messages and structured_response
    handled_output = _handle_model_output(output, effective_response_format)
    messages_list = handled_output["messages"]
    structured_response = handled_output.get("structured_response")

    return ModelResponse(
        result=messages_list,
        structured_response=structured_response,
    )
```

调用关系一目了然：

```text
model_node()                          ← Graph 节点
  └─ wrap_model_call_handler(...)     ← Middleware 链
       └─ _execute_model_sync()       ← 链的最内层，闭包函数
            ├─ _get_bound_model()     ← 辅助函数，不是节点
            │    └─ model.bind_tools()
            └─ model.invoke()         ← 真正的网络请求
```

三个容易记错的点：

- **`_get_bound_model` 不是 Graph 节点。** 它是 `create_agent` 内部的闭包，作用域只在 factory 里。
- **system message 在 `_execute_model_sync` 里拼。** `request.messages` 始终不含它。所以 `wrap_model_call` 改系统提示只能用 `override(system_message=...)`。
- **`bind_tools` 在 `invoke` 之前、在 handler 链之后。** 也就是「所有 Middleware 都改完了，才序列化」。

### 6.2 三条分支

`_get_bound_model` 的主体是三个 `bind_tools` 调用加一个 `bind`。

**真实源码节选：** [`factory.py#L1434-L1490`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/factory.py#L1434-L1490)

```python
# Build final tools list including structured output tools
final_tools = list(request.tools)
if isinstance(effective_response_format, ToolStrategy):
    narrowed_tool_names = {spec.name for spec in effective_response_format.schema_specs}
    structured_tools = [
        info.tool
        for name, info in structured_output_tools.items()
        if name in narrowed_tool_names
    ]
    final_tools.extend(structured_tools)

# Bind model based on effective response format
if isinstance(effective_response_format, ProviderStrategy):
    kwargs = effective_response_format.to_model_kwargs()
    bind_kwargs: dict[str, Any] = {**kwargs, **request.model_settings}
    if _is_openai_compatible_model(request.model) and not getattr(
        request.model, "use_responses_api", False
    ):
        bind_kwargs["strict"] = True
    return (
        request.model.bind_tools(final_tools, **bind_kwargs),
        effective_response_format,
    )

if isinstance(effective_response_format, ToolStrategy):
    for tc in effective_response_format.schema_specs:
        if tc.name not in structured_output_tools:
            msg = (
                f"ToolStrategy specifies tool '{tc.name}' "
                "which wasn't declared in the original "
                "response format when creating the agent."
            )
            raise ValueError(msg)

    # Force tool use if we have structured output tools
    tool_choice = "any" if structured_output_tools else request.tool_choice
    return (
        request.model.bind_tools(
            final_tools, tool_choice=tool_choice, **request.model_settings
        ),
        effective_response_format,
    )

# No structured output - standard model binding
if final_tools:
    return (
        request.model.bind_tools(
            final_tools, tool_choice=request.tool_choice, **request.model_settings
        ),
        None,
    )
return request.model.bind(**request.model_settings), None
```

整理成表：

| 情况 | 调用 | `tool_choice` |
|---|---|---|
| `ProviderStrategy`（模型原生支持结构化输出） | `bind_tools(final_tools, **provider_kwargs)` | 由策略决定 |
| `ToolStrategy`（用工具模拟结构化输出） | `bind_tools(final_tools, tool_choice="any")` | 强制 `"any"` |
| 普通工具调用 | `bind_tools(final_tools, tool_choice=request.tool_choice)` | 通常 `None` |
| 完全没工具 | `bind(**model_settings)` | 不适用 |

最后一行很容易被忽略，但很重要：**没有工具时走的是 `bind`，不是 `bind_tools([])`。** 两个调用在多数 provider 上不等价——`bind_tools([])` 可能仍然在请求里带一个空的 `tools: []` 字段，有些服务端会因此报参数错误。源码选了更保守的那条。

第一行的 `strict=True` 也值得记：它是给 OpenAI 兼容模型补的兼容性补丁，注释说得很清楚——「older versions of langchain-openai do not auto-set it in bind_tools」。

### 6.3 为什么每轮都要重新 bind

这是本章开头第四个问题的答案。

**因为 `request.tools` 每轮都可能不同。**

具体有三个来源：

1. `FilesystemMiddleware` 按 Backend 能力过滤（可能这一轮少了 `execute`）；
2. `_ToolExclusionMiddleware` 按 harness profile 剔除；
3. `SkillsMiddleware` 按「这一轮读过哪些 skill」追加。

如果建图时绑一次，第 3 条就永远不会生效——因为「读过哪些 skill」是运行期才有的信息。

那每次重新 `bind_tools` 会不会很贵？

不会。`bind_tools` 在 LangChain 里的语义是**返回一个新的 Runnable 包装**，不是修改模型实例、也不是发请求。它做的事情主要是把 `BaseTool` 序列化成 JSON Schema。真正的网络请求发生在随后的 `invoke()`。

所以成本结构是：

| 操作 | 成本 |
|---|---|
| `bind_tools()` | 序列化工具 Schema，纯本地 |
| `model.invoke()` | 一次网络请求 |

序列化的开销跟工具数量线性相关。一个 Agent 有二十个工具时，这仍然远小于一次网络请求。所以「每轮重绑」是划算的。

> [!NOTE] 一个可以验证的推论
> 因为 `bind_tools` 返回新对象，`request.model` 本身永远不会被改动。所以你在 Middleware 里 `request.model.bind_tools(...)` 而不使用返回值，是**完全无效**的——它不会影响这次调用。工具集合只能通过 `request.override(tools=...)` 影响。

### 6.4 未知工具校验：为什么它只在特定条件下触发

`_get_bound_model` 开头有一段校验，是这条链上最容易被误解的代码。

**真实源码节选：** [`factory.py#L1369-L1397`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/factory.py#L1369-L1397)

```python
# Validate ONLY client-side tools that need to exist in tool_node
# Skip validation when wrap_tool_call is defined, as middleware may handle
# dynamic tools that are added at runtime via wrap_model_call
has_wrap_tool_call = wrap_tool_call_wrapper or awrap_tool_call_wrapper

# Build map of available client-side tools from the ToolNode
# (which has already converted callables)
available_tools_by_name = {}
if tool_node:
    available_tools_by_name = tool_node.tools_by_name.copy()

# Check if any requested tools are unknown CLIENT-SIDE tools
# Only validate if wrap_tool_call is NOT defined (no dynamic tool handling)
if not has_wrap_tool_call:
    unknown_tool_names = []
    for t in request.tools:
        # Only validate BaseTool instances (skip built-in dict tools)
        if isinstance(t, dict):
            continue
        if isinstance(t, BaseTool) and t.name not in available_tools_by_name:
            unknown_tool_names.append(t.name)

    if unknown_tool_names:
        available_tool_names = sorted(available_tools_by_name.keys())
        msg = DYNAMIC_TOOL_ERROR_TEMPLATE.format(
            unknown_tool_names=unknown_tool_names,
            available_tool_names=available_tool_names,
        )
        raise ValueError(msg)
```

两个过滤条件，各自解决一类误报：

**条件一：跳过 `dict`。** provider 内置工具本来就不在 ToolNode 里，校验它们必然误报。

**条件二：`not has_wrap_tool_call`。** 只要链上任何一个 Middleware 实现了 `wrap_tool_call`，校验就整体跳过。

第二个条件看起来很粗暴——「有人实现了就完全不校验」。但它是唯一可行的选择：factory 无法预知某个 Middleware 会不会在运行期补上这个工具，而 `wrap_tool_call` 恰恰是补票的唯一机制。如果仍要校验，Skill 的动态工具就永远过不去。

**真实源码节选：** [`factory.py#L127-L157`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/factory.py#L127-L157)

```python
DYNAMIC_TOOL_ERROR_TEMPLATE = """
Middleware added tools that the agent doesn't know how to execute.

Unknown tools: {unknown_tool_names}
Registered tools: {available_tool_names}

This happens when middleware modifies `request.tools` in `wrap_model_call` to include
tools that weren't passed to `create_agent()`.

How to fix this:

Option 1: Register tools at agent creation (recommended for most cases)
    Pass the tools to `create_agent(tools=[...])` or set them on `middleware.tools`.
    This makes tools available for every agent invocation.

Option 2: Handle dynamic tools in middleware (for tools created at runtime)
    Implement `wrap_tool_call` to execute tools that are added dynamically:

    class MyMiddleware(AgentMiddleware):
        def wrap_tool_call(self, request, handler):
            if request.tool_call["name"] == "dynamic_tool":
                # Execute the dynamic tool yourself or override with tool instance
                return handler(request.override(tool=my_dynamic_tool))
            return handler(request)
""".strip()
```

这段错误文本本身就是一份文档。它把「两种解法」写在了报错里：

1. **注册期解决**：把工具放进 `create_agent(tools=[...])` 或 `middleware.tools`。这样每轮都有，ToolNode 也能执行。
2. **运行期解决**：实现 `wrap_tool_call`，用 `request.override(tool=...)` 补上工具对象。

这个错误的分界线值得单独记住：

> **它拦的不是「模型会不会调」，而是「Middleware 造了一个没人能执行的东西」。**

也就是说，它保护的是**执行能力**与**可见性**的一致性——你可以让模型看见任何工具，但你必须同时提供执行它的路径。

### 6.5 边界：`bind_tools` 成功 ≠ 模型一定看见

最后一道关卡不在 LangChain 手里，在 provider 手里。

有些 provider 会拒绝某些 JSON Schema。最典型的是 Anthropic：它不接受工具 `input_schema` 根部的 `oneOf` / `anyOf` / `allOf`。

**真实源码节选：** [`_skill_tools.py#L354-L380`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/_skill_tools.py#L354-L380)

```python
def _discard_rejected_schemas(disclosure: _Disclosure, model: object) -> None:
    """Withhold each disclosed tool whose schema `model`'s provider rejects, failing the whole request.

    Anthropic rejects a root `oneOf`, `anyOf` or `allOf` in a tool's input schema,
    in `tools` and inline alike. `bind_tools` drops such a tool from `tools`
    (bar `allOf`), but an inline disclosure never passes through it, and
    withholding it on every path keeps the record equal to what the model was
    shown.
    """
    if not is_anthropic_chat_model(_unwrap_bound(model)):
        return
    ...
    for name, tool in disclosure.tools().items():
        schema = convert_to_anthropic_tool(tool).get("input_schema")
        keys = [key for key in _ANTHROPIC_ROOT_COMBINATORS if isinstance(schema, dict) and key in schema]
        if keys:
            logger.warning(
                "Not disclosing tool '%s': its input_schema has a top-level %s, which the Anthropic API does not support", name, "/".join(keys)
            )
```

注释里那句话是本节的结论：

> `bind_tools` drops such a tool from `tools` (bar `allOf`), but an inline disclosure never passes through it.

意思是：走 `tools` 参数的路径，`bind_tools` 会静默丢掉不合法的工具；走 inline 的路径，没有 `bind_tools` 兜底，所以必须自己丢，否则「记录里说披露了，模型其实没看见」。

于是「暴露」这件事的完整分层是：

| 层 | 谁在做 | 会不会静默失败 |
|---|---|---|
| 1. 注册集 | `ToolNode` | 不会（重名静默覆盖，但那是另一回事） |
| 2. 默认可见集 | `default_tools` | 不会 |
| 3. 本轮可见集 | `wrap_model_call` + `override` | 不会 |
| 4. 序列化 | `bind_tools` | **会**（provider 不支持时丢弃） |
| 5. 实际请求 | `model.invoke` | 可能被服务端再裁一次 |

这五层里，只有前四层是你能在本地读到的。第五层只能靠日志和 provider 文档。

---

## 七、时间点四：模型返回后——`ToolNode` 执行

### 7.1 从 `tool_calls` 到 `Send`

第四节 4.5 已经看过 `_make_model_to_tools_edge`。这里补一个细节：它返回的是**每个 call 一个 `Send`**。

```text
AIMessage(tool_calls=[
    {id: "a", name: "read_file", args: {...}},
    {id: "b", name: "grep",      args: {...}},
])
        │
        ▼
[Send("tools", [call_a]), Send("tools", [call_b])]
        │
        ├──────────────┐
        ▼              ▼
   ToolNode          ToolNode      ← 两个任务并发
   (call_a)          (call_b)
```

这也是为什么 `ToolNode._func` 的输入类型里有一种是「单个 tool_call 的列表」。它不需要处理「一条 AIMessage 里的多个 call」，因为上游已经拆好了。

### 7.2 `_run_one`：查表，然后构造请求

**真实源码节选：** [`tool_node.py#L1015-L1051`](https://github.com/langchain-ai/langgraph/blob/1.2.12/libs/prebuilt/langgraph/prebuilt/tool_node.py#L1015-L1051)

```python
def _run_one(
    self,
    call: ToolCall,
    input_type: Literal["list", "dict", "tool_calls"],
    tool_runtime: ToolRuntime,
) -> ToolMessage | Command | list[Command | ToolMessage]:
    """Execute single tool call with wrap_tool_call wrapper if configured."""
    # Validation is deferred to _execute_tool_sync to allow interceptors
    # to short-circuit requests for unregistered tools
    tool = self.tools_by_name.get(call["name"])

    # Create the tool request with state and runtime
    tool_request = ToolCallRequest(
        tool_call=call,
        tool=tool,
        state=tool_runtime.state,
        runtime=tool_runtime,
    )

    config = tool_runtime.config

    if self._wrap_tool_call is None:
        # No wrapper - execute directly
        return self._execute_tool_sync(tool_request, input_type, config)

    # Define execute callable that can be called multiple times
    def execute(req: ToolCallRequest) -> ToolMessage | Command:
        """Execute tool with given request. Can be called multiple times."""
        return self._execute_tool_sync(req, input_type, config)

    # Call wrapper with request and execute callable
    try:
        return self._wrap_tool_call(tool_request, execute)
```

关键在第二行：

```python
tool = self.tools_by_name.get(call["name"])
```

`.get()` 而不是 `[]`。**查不到就是 `None`，不报错。**

于是 `ToolCallRequest.tool` 有了三种状态：

| `request.tool` | 含义 | 谁能处理 |
|---|---|---|
| `BaseTool` | 注册表里有，直接执行 | `_execute_tool_sync` |
| `None` | 注册表里没有，但可能是动态工具 | 某个 `wrap_tool_call` 补票 |
| `None`（无人补票） | 真的不存在 | `_validate_tool_call` 报 invalid |

**真实源码节选：** [`tool_node.py#L133-L148`](https://github.com/langchain-ai/langgraph/blob/1.2.12/libs/prebuilt/langgraph/prebuilt/tool_node.py#L133-L148)

```python
class ToolCallRequest:
    """Tool execution request passed to tool call interceptors.

    Attributes:
        tool_call: Tool call dict with name, args, and id from model output.
        tool: BaseTool instance to be invoked, or None if tool is not
            registered with the `ToolNode`. When tool is `None`, interceptors can
            handle the request without validation. If the interceptor calls `execute()`,
            validation will occur and raise an error for unregistered tools.
        state: Agent state (`dict`, `list`, or `BaseModel`).
        runtime: LangGraph runtime context (optional, `None` if outside graph).
    """

    tool_call: ToolCall
    tool: BaseTool | None
    state: Any
    runtime: ToolRuntime
```

文档字符串里那句「When tool is `None`, interceptors can handle the request without validation」是整段设计的关键：`None` 不是错误状态，是**「还没决定」状态**。

### 7.3 校验为什么必须推迟

**真实源码节选：** [`tool_node.py#L942-L950`](https://github.com/langchain-ai/langgraph/blob/1.2.12/libs/prebuilt/langgraph/prebuilt/tool_node.py#L942-L950)

```python
call = request.tool_call
tool = request.tool

# Validate tool exists when we actually need to execute it
if tool is None:
    if invalid_tool_message := self._validate_tool_call(call):
        return invalid_tool_message
    # This should never happen if validation works correctly
    msg = f"Tool {call['name']} is not registered with ToolNode"
    raise TypeError(msg)
```

校验发生在 `_execute_tool_sync` 里，也就是**所有拦截器都放弃之后**。

如果校验提前到 `_run_one` 开头，那么 Skill 披露的工具会在第一秒就被判成 invalid，`wrap_tool_call` 根本没有机会补票。

**真实源码节选：** [`tool_node.py#L1265-L1278`](https://github.com/langchain-ai/langgraph/blob/1.2.12/libs/prebuilt/langgraph/prebuilt/tool_node.py#L1265-L1278)

```python
def _validate_tool_call(self, call: ToolCall) -> ToolMessage | None:
    requested_tool = call["name"]
    if requested_tool not in self.tools_by_name:
        all_tool_names = list(self.tools_by_name.keys())
        content = INVALID_TOOL_NAME_ERROR_TEMPLATE.format(
            requested_tool=requested_tool,
            available_tools=", ".join(all_tool_names),
        )
        return ToolMessage(
            content, name=requested_tool, tool_call_id=call["id"], status="error"
        )
    return None
```

**真实源码节选：** [`tool_node.py#L108-L110`](https://github.com/langchain-ai/langgraph/blob/1.2.12/libs/prebuilt/langgraph/prebuilt/tool_node.py#L108-L110)

```python
INVALID_TOOL_NAME_ERROR_TEMPLATE = (
    "Error: {requested_tool} is not a valid tool, try one of [{available_tools}]."
)
```

注意这个返回值是 `ToolMessage`，不是异常。所以模型会**看到**这条错误，并有机会改。这和 `_get_bound_model` 里的 `ValueError` 完全不同——后者直接让整轮崩掉。

两类错误的分工很清晰：

| 错误 | 触发点 | 后果 | 谁能修 |
|---|---|---|---|
| `ValueError: Middleware added tools...` | 模型调用前 | 整轮失败 | 写 Middleware 的人 |
| `ToolMessage: xxx is not a valid tool` | 工具执行时 | 模型看到错误，可重试 | 模型自己 |

### 7.4 Skill 的补票

**真实源码节选：** [`skills.py#L1366-L1393`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/skills.py#L1366-L1393)

```python
def _with_disclosed_skill_tool(self, request: ToolCallRequest, include_name: str, tools: Sequence[BaseTool]) -> ToolCallRequest:
    """Return `request` running the tool it calls from `tools`, or unchanged to fail as invalid if that's missing."""
    name = request.tool_call["name"]
    tool = next((t for t in tools if t.name == name), None)
    if tool is None:
        logger.warning("Skill tool '%s' was disclosed via '%s', but the resolver no longer returns it", name, include_name)
        return request
    return request.override(tool=tool)

def wrap_tool_call(
    self,
    request: ToolCallRequest,
    handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
) -> ToolMessage | Command[Any]:
    """Run a disclosed skill tool; calls to any other unregistered tool fail as invalid."""
    include_name = _disclosed_record(request.state).get(request.tool_call["name"])
    if request.tool is not None or include_name is None:
        return handler(request)
    tools = _call_resolver(self._skill_tool_resolver, include_name, get_runtime())
    return handler(self._with_disclosed_skill_tool(request, include_name, tools))
```

四行逻辑，每个判断都有意义：

1. **`request.tool is not None` → 直接放行。** 已经注册的工具不归它管，而且如果外层 Middleware 已经补过票，它也不能覆盖。
2. **`include_name is None` → 直接放行。** 这个名字不在「已披露记录」里，说明它压根不是 Skill 工具，交给下游按 invalid 处理。
3. **重新调 resolver。** 注意是「重新调」，不是「用上一轮缓存的对象」。因为 `wrap_model_call` 和 `wrap_tool_call` 之间可能隔了一次完整的模型往返，工具对象可能已经变了（比如 skill 文件被改）。
4. **找不到就只 `logger.warning`，不改 request。** 于是这个请求会带着 `tool=None` 继续往下，最终由 `_validate_tool_call` 报 invalid。这个选择很克制：宁可让模型收到一条「工具不存在」，也不抛异常中断整轮。

**真实源码节选：** [`tool_node.py#L170-L196`](https://github.com/langchain-ai/langgraph/blob/1.2.12/libs/prebuilt/langgraph/prebuilt/tool_node.py#L170-L196)

```python
def override(
    self, **overrides: Unpack[_ToolCallRequestOverrides]
) -> ToolCallRequest:
    """Replace the request with a new request with the given overrides.

    Returns a new `ToolCallRequest` instance with the specified attributes replaced.
    This follows an immutable pattern, leaving the original request unchanged.
    ...
    """
    return replace(self, **overrides)
```

`dataclasses.replace`。一行，但它是整条「动态工具」链路的枢纽：`wrap_tool_call` 能改工具，靠的就是这个不可变替换。

### 7.5 执行与回流

补票之后，`_execute_tool_sync` 拿到一个有 `tool` 的 request：

```python
injected_call = self._inject_tool_args(call, request.runtime, tool)
call_args = {**injected_call, "type": "tool_call"}
response = tool.invoke(call_args, config)
return self._normalize_tool_response(response, request.tool_call, input_type)
```

三个动作：

1. **注入参数。** `state`、`store`、`runtime` 这些不在模型输出里的参数，在这里补上。`_injected_args` 在注册时就分析好了，所以这里只是按图索骥。
2. **调用。** `tool.invoke()`。注意它内部还会用 `args_schema` 校验一遍——这就是为什么模型给的参数不合法时，错误发生在工具内部而不是 ToolNode。
3. **归一化。** 工具可能返回字符串、`ToolMessage`、`Command`，统一成节点能处理的形式。

错误处理有一条重要例外：

```python
except GraphBubbleUp:
    raise
```

`GraphBubbleUp` 是 LangGraph 用来「穿透」的信号。HITL 的 `interrupt` 就是靠它从工具内部一路冒到图执行器的。所以它必须绕过 `handle_tool_errors`，否则「暂停」会被当成「工具失败」吞掉，变成一条错误消息。

这条和第九章附一 5.5 节、第十章都会连起来——下一章的主角就是这个异常。

最后，多个 `Send` 的结果由 `_combine_tool_outputs` 汇总，写回 `messages` 通道。一条 `ToolMessage` 带着 `tool_call_id` 回到 State，条件边下一轮就能算出「还有没有 pending」。

循环闭合了。

---

## 八、两个节点，两种确定性

前七节一直在追「工具怎么走到模型面前」。现在把镜头拉回来，看这条链的两端各是一个 Graph 节点。

### 8.1 先给两个节点各下一句定义

- **`model_node`**：准备一次模型请求，执行它，把结果变成 State 更新。
- **`ToolNode`**：拿到模型已经决定好的 `tool_call`，找到对应工具，执行它，把结果变成 State 更新。

两句话里唯一相同的是「把结果变成 State 更新」。其余每一环都不同。

| 对比项 | `model_node` | `ToolNode` |
|---|---|---|
| 处理对象 | 一次**模型请求** | 一个或一批**工具调用** |
| 主要输入 | `AgentState`（以 `messages` 为主）+ `Runtime` | `tool_call` 列表 + `Runtime` |
| 主要输出 | `list[Command]` | `{"messages": [...]}` 或列表，也可能含 `Command` |
| 通常产出的消息 | `AIMessage` | `ToolMessage` |
| 是否调用 `bind_tools()` | **是**（`_execute_model_sync` → `_get_bound_model`） | 否 |
| 被哪个 Hook 包住 | `wrap_model_call` | `wrap_tool_call` |
| 每轮执行次数 | 1 次 | 每个 `tool_call` 1 次 |
| 会不会碰 Backend | 不会 | 间接会（工具内部调） |
| 失败时的典型表现 | provider 报错、Schema 被拒 | `ToolMessage(status="error")` |

最后一行是这张表里最实用的区分。同样是「出错了」，模型侧的错通常让整轮崩掉（异常上抛），工具侧的错通常变成一条消息交给模型自己处理。这个不对称在第七节 7.3 已经见过，这里只是把它放到节点层面。

### 8.2 `model_node` 的收尾，最容易被略过的那一步

第五节 5.2 把 `model_node` 拆成三步：修补历史、构造请求、交给 handler 链。前两步都很短，第三步的收尾——把 `ModelResponse` 变成 `list[Command]`——反而最容易被略过。

先把构造请求时的一个细节补上。

**构造 `ModelRequest`：`messages` 不含 system message。** 系统消息是独立字段，拼接发生在 `_execute_model_sync` 里。这是为什么 `wrap_model_call` 改系统提示必须用 `override(system_message=...)`，而不能改 `messages[0]`。

**第三步的收尾：把模型响应变成 `Command` 列表。**

**真实源码节选：** [`factory.py#L222-L278`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/factory.py#L222-L278)

```python
def _build_commands(
    model_response: ModelResponse,
    middleware_commands: list[Command[Any]] | None = None,
    *,
    has_structured_output: bool = False,
    repaired_messages: list[AnyMessage] | None = None,
) -> list[Command[Any]]:
    """Build a list of Commands from a model response and middleware commands.

    The first Command contains the model response state (messages and optional
    structured_response). Middleware commands are appended as-is.
    ...
    """
    messages = model_response.result
    if repaired_messages is not None:
        messages = [
            RemoveMessage(id=REMOVE_ALL_MESSAGES),
            *repaired_messages,
            *model_response.result,
        ]
    state: dict[str, Any] = {"messages": messages}

    if model_response.structured_response is not None:
        state["structured_response"] = model_response.structured_response
    elif has_structured_output:
        state["structured_response"] = None

    for cmd in middleware_commands or []:
        if cmd.goto:
            msg = "Command goto is not yet supported in wrap_model_call middleware. ..."
            raise NotImplementedError(msg)
        ...

    commands: list[Command[Any]] = [Command(update=state)]
    commands.extend(middleware_commands or [])
```

四个要点：

1. **返回值是 `list[Command]`，不是 `dict`。** 这是 LangGraph 1.x 的写法：节点可以返回多条 `Command`，图会按顺序应用。普通 `dict` 更新只是其中一种特例。
2. **第一个 `Command` 承载模型结果，后面的都是 Middleware 攒下来的。** 顺序是「内层先、外层后」（第五节 5.4 讲过），所以外层的 `Command` 排在后面：在没有 reducer 的字段上后写者胜，而 `messages` 走 `add_messages`，是追加语义。
3. **修补过历史时，用 `RemoveMessage(REMOVE_ALL_MESSAGES)` 整体替换。** 这一条很容易看漏：`repaired_messages` 不是「追加」，而是「清空重写再追加本轮结果」。原因是 `add_messages` 默认是追加语义，要让一份完整的新历史替换旧历史，必须先发一条「删除全部」。
4. **有 `response_format` 但本轮没产出结构化结果时，显式写 `None`。** 注释说明了理由：避免上一轮 checkpoint 里的旧值残留，被误认为本轮结果。

还有一个限制值得单独记住：`wrap_model_call` 里的 Middleware 可以返回 `Command`，但**不能用 `goto` / `resume` / `graph`**，源码直接抛 `NotImplementedError`。想让 Middleware 改路由，得用 `jump_to` 字段配合 `before_model` / `after_model`，而不是在 `wrap_model_call` 里 `goto`。

**真实源码节选：** [`types.py#L349-L354`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/types.py#L349-L354)

```python
class AgentState(TypedDict, Generic[ResponseT]):
    """State schema for the agent."""

    messages: Required[Annotated[list[AnyMessage], add_messages]]
    jump_to: NotRequired[Annotated[JumpTo | None, EphemeralValue, PrivateStateAttr]]
    structured_response: NotRequired[Annotated[ResponseT, OmitFromInput]]
```

`jump_to` 带两个注解：`EphemeralValue`（只在当前 superstep 有效，不持久化）和 `PrivateStateAttr`（不出现在输入/输出）。所以「用 State 字段做一次跳转」这件事不会污染对外可见的 State。

### 8.3 `ToolNode` 的六步

**第一步：解析输入。**

**真实源码节选：** [`tool_node.py#L1224-L1266`](https://github.com/langchain-ai/langgraph/blob/1.2.12/libs/prebuilt/langgraph/prebuilt/tool_node.py#L1224-L1266)

```python
input_type: Literal["list", "dict", "tool_calls"]
if isinstance(input, list):
    if isinstance(input[-1], dict) and input[-1].get("type") == "tool_call":
        input_type = "tool_calls"
        tool_calls = cast("list[ToolCall]", input)
        return tool_calls, input_type
    input_type = "list"
    messages = input
...
```

`ToolNode` 接受三种输入形态：整份 State（`dict`）、消息列表（`list`）、直接给 `tool_call`（`tool_calls`）。这个宽容度是为了让它既能当图节点用，也能被单独调用做测试。

**它不决定调用哪个工具。** 这一点必须说清：工具名、参数、id 全部来自模型输出的 `AIMessage`。`ToolNode` 只是执行者，不是决策者。

**第二步：为每个 call 构造 `ToolRuntime`。**

**真实源码节选：** [`tool_node.py#L799-L817`](https://github.com/langchain-ai/langgraph/blob/1.2.12/libs/prebuilt/langgraph/prebuilt/tool_node.py#L799-L817)

```python
tool_calls, input_type = self._parse_input(input)
config_list = get_config_list(config, len(tool_calls))

# Construct ToolRuntime instances at the top level for each tool call
tool_runtimes = []
for call, cfg in zip(tool_calls, config_list, strict=False):
    state = self._extract_state(input, cfg)
    tool_runtime = ToolRuntime(
        state=state,
        tool_call_id=call["id"],
        config=cfg,
        context=runtime.context,
        store=runtime.store,
        stream_writer=runtime.stream_writer,
        tools=list(self.tools_by_name.values()),
        execution_info=runtime.execution_info,
        server_info=runtime.server_info,
    )
    tool_runtimes.append(tool_runtime)
```

为什么要单独造这个东西？因为**模型只提供参数**，而一个真实工具经常还需要别的东西：

| 工具可能需要 | 谁提供 | 模型知道吗 |
|---|---|---|
| `state`（历史消息、文件表） | `ToolRuntime` | 不知道 |
| `store`（长期记忆） | `ToolRuntime` | 不知道 |
| `tool_call_id`（回填结果用） | `ToolRuntime` | 不知道 |
| `config`（回调、递归限制） | `ToolRuntime` | 不知道 |
| 业务参数（`file_path`、`content`） | 模型 | 知道 |

前四行就是「参数注入」的全部内容。第七章 7.5 提到的 `_inject_tool_args`，读的就是这个对象。

注意 `tools=list(self.tools_by_name.values())` 这一行：工具内部也能拿到**注册集**。它和「本轮模型可见集」不是一回事（第九节会展开），但工具作者通常不关心这个区别——他拿到的就是「这个 Agent 装了哪些工具」。

**第三步：查表并构造 `ToolCallRequest`。** 第七节 7.2 已讲：`.get()` 而不是 `[]`，查不到给 `None`。

**第四步：过 `wrap_tool_call` 链。** 如果链存在，`_run_one` 把「执行」包成一个可重复调用的 `execute` 闭包交出去。这个设计支持重试——Middleware 可以调 `execute` 多次。

**第五步：执行与异常处理。** 校验推迟到这一步（第七节 7.3），注入参数、`tool.invoke()`、归一化结果，`GraphBubbleUp` 直接上抛。

**第六步：汇总回 State。**

**真实源码节选：** [`tool_node.py#L862-L885`](https://github.com/langchain-ai/langgraph/blob/1.2.12/libs/prebuilt/langgraph/prebuilt/tool_node.py#L862-L885)

```python
def _combine_tool_outputs(
    self,
    outputs: list[ToolMessage | Command | list[ToolMessage | Command]],
    input_type: Literal["list", "dict", "tool_calls"],
) -> list[Command | list[ToolMessage] | dict[str, list[ToolMessage]]]:
    # Flatten list entries from tools that returned multiple items
    flat_outputs: list[ToolMessage | Command]
    if any(isinstance(output, list) for output in outputs):
        ...
    # preserve existing behavior for non-command tool outputs for backwards
    # compatibility
    if not any(isinstance(output, Command) for output in flat_outputs):
        # TypedDict, pydantic, dataclass, etc. should all be able to load from dict
        return (
            flat_outputs
            if input_type == "list"
            else {self._messages_key: flat_outputs}
        )
```

返回形态由 `input_type` 决定：

| 输入形态 | 输出形态 |
|---|---|
| `list`（消息列表） | `[ToolMessage, ...]` |
| `dict`（整份 State） | `{"messages": [ToolMessage, ...]}` |
| `tool_calls`（直接给调用） | `{"messages": [ToolMessage, ...]}` |

在 `create_agent` 里走的是第三种——条件边发的是 `Send("tools", [tool_call])`。所以返回的是 `{"messages": [...]}`，由 `add_messages` reducer 追加到 State。

如果工具有的返回 `ToolMessage`、有的返回 `Command`，`_combine_tool_outputs` 会进入下半段，把 `Command` 和普通消息混着返回。其中有一个特殊处理：多个 `Command(graph=Command.PARENT, goto=[Send...])` 会被合并成一条，避免父图收到一堆并行的跳转指令。

**这一步之后，`ToolNode` 就结束了。** 它不生成最终回答，也不决定下一步去哪——那是条件边的事。

### 8.4 一次 `read_file` 的完整往返

把六步放到一个具体例子上。

```text
用户：读取 /test.txt 并告诉我内容
```

**第 1 轮 · `model_node`**

```text
输入 State:
  messages = [HumanMessage("读取 /test.txt 并告诉我内容")]

内部：
  ModelRequest(tools=[ls, read_file, write_file, ...])
  → wrap_model_call 链
  → bind_tools()
  → model.invoke()

输出 Command:
  update = {messages: [AIMessage(
      content="",
      tool_calls=[{name: "read_file",
                   args: {"file_path": "/test.txt"},
                   id: "call_1"}],
  )]}
```

注意 `content` 是空字符串。模型这一步没有说任何话，只发起了调用。

**路由**

`_make_model_to_tools_edge` 看到 `last_ai_message.tool_calls` 非空、且 `call_1` 还没有对应的 `ToolMessage`，于是返回：

```python
[Send("tools", [{"name": "read_file", "args": {...}, "id": "call_1"}])]
```

**第 2 步 · `ToolNode`**

```text
输入：[{name: "read_file", args: {"file_path": "/test.txt"}, id: "call_1"}]

内部：
  _parse_input          → input_type = "tool_calls"
  构造 ToolRuntime      → state / config / store / tool_call_id="call_1"
  tools_by_name.get()   → read_file 这个 BaseTool
  ToolCallRequest(...)
  → wrap_tool_call 链（并行冲突检查 → handler → 大结果卸载）
  → _inject_tool_args   → 补 runtime 参数
  → tool.invoke()       → 内部调 backend.read("/test.txt")

输出：
  {"messages": [ToolMessage(
      content="Hello World",
      tool_call_id="call_1",
      name="read_file",
  )]}
```

`ToolMessage` 里那个 `tool_call_id` 是整条链的接头。没有它，下一轮模型无法把结果和调用对上。

**第 3 步 · 路由**

`_make_tools_to_model_edge` 检查两件事：

- 这个工具是不是 `return_direct=True`？不是。
- 执行的是不是结构化输出工具？不是。

于是默认回 `model`。

**第 4 步 · 第 2 轮 `model_node`**

```text
输入 State:
  messages = [
      HumanMessage("读取 /test.txt 并告诉我内容"),
      AIMessage(tool_calls=[call_1]),
      ToolMessage("Hello World", tool_call_id="call_1"),
  ]

内部：同上，只是 tools 可能已经变了（比如某一轮 Skill 披露了新工具）

输出 Command:
  update = {messages: [AIMessage("文件 /test.txt 的内容是 Hello World。")]}
```

**第 5 步 · 路由**

`tool_calls` 为空 → 返回 `end_destination`。循环结束。

整条路径里，`model_node` 执行两次，`ToolNode` 执行一次。这两个数字就是「Agent 循环」最朴素的度量。

### 8.5 为什么要拆成两个节点

这个问题不是修辞。如果只写一个函数，`while` 循环里先调模型、再调工具，功能上完全做得到。拆开换来三样东西。

**第一，两种 Hook 的作用对象不同，拆开才不会互相污染。**

`wrap_model_call` 拿到的是 `ModelRequest`，能改 `tools`、`messages`、`system_message`、`model_settings`。`wrap_tool_call` 拿到的是 `ToolCallRequest`，能改 `tool_call`、`tool`、`state`。

一个 Middleware 可以同时实现两者，但处理的完全是两件事。`FilesystemMiddleware` 就是例子：`wrap_model_call` 负责筛工具、拼 Prompt、缩消息；`wrap_tool_call` 负责冲突检查和结果卸载。第九章附一第五、六节把这两块分得很清楚。

如果模型和工具在同一个函数里，这两类逻辑就得挤在同一个 Hook 里，用「现在走到哪一步了」来判断——那才是真正的耦合。

**第二，循环写在图里，而不是写在函数里。**

`while True:` 里跑模型和工具，你没法在中间暂停、没法单步、没法看到「当前是第几轮」。拆成两个节点后，每一轮都是图上的两次 superstep，循环由条件边表达。

这也解释了 `create_agent` 里那句 `recursion_limit: 9_999`：循环上限由图的执行器管，不需要函数自己数。

**第三，中断需要一个「边界」才能落地。**

`interrupt()` 在工具内部被调用时，抛的是 `GraphBubbleUp`。它必须一路冒到图执行器，图才能把当前状态存进 checkpoint、把控制权交出去。

如果工具调用被包在模型节点内部，这个异常就得先穿过模型节点的函数体、再穿过 `wrap_model_call` 的 handler 链才能出去；拆开之后，`ToolNode` 是一个干净的边界：异常从这里出去，就是「工具这一步被中断了」，语义明确。

这也正是下一章要展开的东西。

**一个诚实的补充：** 上面三条是「拆开之后得到了什么」，不是「当初为什么这么设计」。源码里没有一段注释解释这个决定，所以这里只陈述结果，不替作者编动机。

### 8.6 两段伪代码，和五个角色

最后用两段伪代码收拢。它们和真实源码的差距，标在注释里。

**概念伪代码：`model_node` 的骨架**

```python
def model_node(state, runtime):
    # 真实源码还多一步：修补历史里残缺的 tool_call
    request = ModelRequest(
        model=model,
        messages=state["messages"],      # 不含 system message
        tools=default_tools,             # 建图时的默认值，不是终值
        system_message=system_message,
        state=state,
        runtime=runtime,
    )
    result = wrap_model_call_handler(    # 真实名称；无 Middleware 时直接调 _execute_model_sync
        request,
        _execute_model_sync,             # 链的最内层：bind_tools + invoke
    )
    return _build_commands(              # 返回 list[Command]，不是 dict
        result.model_response,
        result.commands,
    )
```

**概念伪代码：`ToolNode` 的骨架**

```python
def tool_node(tool_calls, runtime):
    results = []
    for call in tool_calls:              # create_agent 里通常只有一个（Send 已拆分）
        tool = tools_by_name.get(call["name"])   # 真实源码用 .get()，不是 []
        request = ToolCallRequest(
            tool_call=call,
            tool=tool,                   # 可能是 None，等 wrap_tool_call 补票
            state=runtime.state,
            runtime=runtime,
        )
        results.append(wrap_tool_call_chain(request, execute))
    return {"messages": results}         # 形态取决于 input_type
```

两段代码放在一起看，差别就很清楚了：

| | `model_node` | `ToolNode` |
|---|---|---|
| 处理对象 | 一次模型请求 | 一个 `tool_call` |
| 对象名从哪来 | 无——调哪个工具由模型在输出里决定 | `call["name"]`，模型已经写好 |
| 输入从哪来 | `state["messages"]` | `call["args"]`，模型已经写好 |
| 不确定性 | 高：同一输入可能不同输出 | 低：同名同参同环境 → 同一条 Python 路径 |
| 谁决定下一步 | 模型（通过有没有 `tool_calls`） | 条件边（读 State 判断） |

最后一行值得多看一眼：**`ToolNode` 不决定下一步。** 它只管执行，执行完就交回去。真正决定「继续循环还是结束」的是 `_make_tools_to_model_edge` 和 `_make_tools_to_tools_edge` 这两个纯函数——它们只读 State，不调模型、不调工具。

于是整个 Agent 的分工可以压成一张表：

| 角色 | 负责什么 | 不负责什么 |
|---|---|---|
| `model_node` | 思考下一步做什么 | 不执行任何副作用 |
| `ToolNode` | 把模型决定的操作执行出来 | 不决定调用哪个工具、不决定下一步 |
| `Middleware` | 在这两个环节前后插入规则与能力 | 不直接改路由（`goto` 被禁） |
| `StateGraph` | 状态流转、节点路由、执行循环、checkpoint | 不理解工具和模型 |
| `Backend` | 为工具和 Middleware 提供底层数据与执行能力 | 不参与决策 |

**这五行就是前面所有章节的地图。** 第九章讲 `ToolNode` 与 `Backend` 的接口，第九章附一讲 `FilesystemMiddleware` 怎么在两层之间做编排，第九章附二讲 `model_node` 与 `ToolNode` 之间那段「工具怎么被看见」的路。

---

## 九、两个集合到底什么关系

现在可以正面回答这个问题了。

### 9.1 三个集合，不是两个

| 集合 | 定义 | 谁决定 |
|---|---|---|
| **注册集** | `tool_node.tools_by_name` | 建图时：`middleware.tools` + 用户工具 |
| **默认暴露集** | `default_tools` | 建图时：注册集 + provider 内置 |
| **本轮暴露集** | `request.tools`（`bind_tools` 的入参） | 每轮：`wrap_model_call` 链 |

平时说「注册了几个、模型看见几个」，实际上是在比较第一和第三。

### 9.2 五种真实组合

| # | 场景 | 注册集 | 本轮暴露集 | 关系 | 兜底机制 |
|---|---|---|---|---|---|
| 1 | 什么都不做 | 6 | 6 | 相等 | 无 |
| 2 | FS Backend 不支持 `execute` | 6 | 5 | 暴露 ⊂ 注册 | 无（模型看不见就不会调） |
| 3 | harness profile 排除某工具 | 6 | 4 | 暴露 ⊂ 注册 | `wrap_tool_call` 再拒一次 |
| 4 | Skill 披露动态工具 | 3 | 4 | 暴露 ⊃ 注册 | **必须**有 `wrap_tool_call` |
| 5 | provider 内置工具 | 0 | 1 | 暴露 ⊃ 注册 | 不需要（provider 侧执行） |

前两行符合直觉。后三行各有各的道理。

**第 3 行**值得单独说：既然模型看不见，为什么执行边界还要拦一次？

因为「模型看不见」只对**当前这一轮**成立。历史消息里可能残留对已排除工具的调用（比如上一轮还没排除，或者被手工注入了 ToolMessage 之外的东西），模型也可能被 prompt injection 诱导着硬写一个名字。`_ToolExclusionMiddleware` 的文档字符串把这件事写得很直白：

> Excluded names are also rejected at the tool-call boundary, since the executor still has them registered and dispatches on the name the model emits. **That keeps execution consistent with what was advertised; it is not a security surface.**

最后半句很重要：它不是安全边界，是一致性边界。真正的安全边界在 Backend 和 `permissions`（第九章附一第八节）。

**第 4 行**是本章最反直觉的一条。Skill 披露的工具在 `tools_by_name` 里根本不存在，但模型能看见、也能调。代价是：**每一个这样的 Agent 都必须有一个能补票的 `wrap_tool_call`**，否则调用会变成 invalid tool message。

这也解释了 `_get_bound_model` 里那个「有 `wrap_tool_call` 就跳过校验」的分支——它不是偷懒，它是在给第 4 行留门。

**第 5 行**是另一条「暴露 ⊃ 注册」，但机制完全不同：这些工具根本不打算本地执行。它们由 provider 在自己那边跑完，结果直接回到对话里，不会以本地 `tool_call` 的形式落到 ToolNode。

所以「暴露 ⊃ 注册」有两种，代价完全不同：

| 类型 | 谁执行 | 需要 `wrap_tool_call` 吗 |
|---|---|---|
| 动态本地工具（Skill） | 本地 ToolNode | **需要** |
| provider 内置工具 | 远端 provider | 不需要 |

### 9.3 结论

把两个集合的职责分开，整章就收拢了：

```text
                 ┌─────────────────────────────┐
                 │  ToolNode.tools_by_name     │
                 │  「我能执行哪些」             │
                 └──────────────┬──────────────┘
                                │ 作为默认值
                                ▼
                 ┌─────────────────────────────┐
                 │  ModelRequest.tools         │
                 │  「这一轮我给模型看哪些」      │
                 └──────────────┬──────────────┘
                                │ wrap_model_call 增删
                                ▼
                 ┌─────────────────────────────┐
                 │  bind_tools() 的 final_tools │
                 │  「实际发出去的 Schema」       │
                 └─────────────────────────────┘
```

三句话：

1. **`ToolNode` 管执行能力，`ModelRequest.tools` 管模型可见性。** 两者不需要相等。
2. **可见性可以比可执行性小**（过滤），代价是模型可能「不知道该用某个能力」。
3. **可见性也可以比可执行性大**（动态工具），代价是**必须有对应的执行兜底**，否则模型会收到一条 invalid tool message。

最重要的不是数量，而是**不一致时有没有对应机制**。数量相等只是默认状态，不是设计约束。

---

## 十、失败模式表

这张表按「现象」索引，因为排查时你手上通常只有一个现象。

| 现象 | 真实原因 | 定位位置 |
|---|---|---|
| 模型说「我没有这个工具」 | 被 `wrap_model_call` 过滤掉了 | 打印 `request.tools` 的 name 列表 |
| 模型说「我没有这个工具」，但过滤逻辑没动它 | `tools=` 白名单在装配时就没生成 | `FilesystemMiddleware._create_tools` |
| 模型知道工具但参数总错 | `args_schema` 描述不足 | 工具的 `args_schema` |
| 模型在正文里「假装调用」 | 这一轮 `tools` 为空，走了 `model.bind()` | `_get_bound_model` 的最后一个 return |
| `ValueError: Middleware added tools that the agent doesn't know how to execute` | 动态加了工具，但链上没有 `wrap_tool_call` | `factory.py#L1369-L1397` |
| `Error: xxx is not a valid tool, try one of [...]` | 调了不在 `tools_by_name` 里的工具，且无人 `override(tool=...)` | `_validate_tool_call` |
| Skill 工具时好时坏 | resolver 每次重新调用，返回结果不稳定 | `_with_disclosed_skill_tool` 的 `logger.warning` |
| 第 2 轮突然多出工具 / 工具重复出现 | 有人 `request.tools.append(...)`，污染了共享的 `default_tools` | 找 `wrap_model_call` 里的原地修改 |
| 同名工具行为不对 | `tools_by_name` 后到者胜，用户工具盖掉了 Middleware 工具 | `available_tools` 的顺序 |
| 工具描述和 docstring 不一致 | 描述在装配期（`_apply_tool_description_overrides`）或请求期（`_with_filtered_*_description`）被重写 | `graph.py#L666`、`filesystem.py#L3383-L3388` |
| Anthropic 上某个工具「消失」 | 根级 `oneOf`/`anyOf`，被 `bind_tools` 或 `_discard_rejected_schemas` 丢弃 | `_skill_tools.py#L354-L380` |
| 加了 `print` 但模型不知道 | 模型只收到 Schema，收不到函数体 | 无需定位，这是设计如此 |

最后一行不是 bug，是本章最基础的一条事实。放在表里，是因为它被问到的频率最高。

---

## 十一、最小验证：四个阶段

理论可以争论，观察不会。下面四个阶段从「不需要模型」到「需要模型」，可以逐级做。

### 阶段一：只看 ToolNode（不需要模型，不需要 API Key）

```python
from langchain_core.tools import tool
from langchain_core.messages import AIMessage
from langgraph.prebuilt.tool_node import ToolNode

@tool
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b

node = ToolNode([add])

# 1. 注册表长什么样
print(node.tools_by_name.keys())
# dict_keys(['add'])

# 2. 模型看到的 Schema 从哪来
print(node.tools_by_name["add"].args_schema.model_json_schema())

# 3. 调一个没注册的工具
out = node.invoke({
    "messages": [
        AIMessage(
            content="",
            tool_calls=[{"name": "sub", "args": {}, "id": "1", "type": "tool_call"}],
        )
    ]
})
print(out["messages"][-1].content)
# Error: sub is not a valid tool, try one of [add].
```

第三行输出能验证三件事：

- `tools_by_name` 是名字到对象的映射；
- `args_schema` 是 Pydantic 模型，可以转 JSON Schema；
- 未注册的工具返回的是 `ToolMessage`，不是异常。

### 阶段二：看 `request.tools`（需要一个能 `bind_tools` 的模型）

**教学化简：** 下面这个模型是测试替身，只记录它收到了什么，不产生真实请求。

```python
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

SEEN: list[list] = []

class SpyModel(BaseChatModel):
    @property
    def _llm_type(self) -> str:
        return "spy"

    def bind_tools(self, tools, **kwargs):
        SEEN.append([getattr(t, "name", None) for t in tools])
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="ok"))])
```

然后加一个什么都不改的 Middleware，观察它收到的 `request.tools`：

```python
from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware

class Spy(AgentMiddleware):
    def wrap_model_call(self, request, handler):
        print("model sees:", [getattr(t, "name", None) for t in request.tools])
        return handler(request)

agent = create_agent(SpyModel(), tools=[add], middleware=[Spy()])
agent.invoke({"messages": [{"role": "user", "content": "hi"}]})
```

两次打印应该一致——这就是「默认暴露集 = 注册集」。

### 阶段三：让两个集合不一致

把 `Spy` 换成真的会改的版本：

```python
class Remover(AgentMiddleware):
    def wrap_model_call(self, request, handler):
        kept = [t for t in request.tools if getattr(t, "name", None) != "add"]
        return handler(request.override(tools=kept))
```

- 注册集仍然是 `['add']`；
- `bind_tools` 收到的是 `[]`；
- 因为 `final_tools` 为空，走的是 `model.bind()`，不是 `bind_tools([])`。

再把方向反过来：

```python
class Adder(AgentMiddleware):
    def wrap_model_call(self, request, handler):
        return handler(request.override(tools=[*request.tools, add]))
```

这一次会看到 `ValueError: Middleware added tools that the agent doesn't know how to execute`——因为 `add` 已经被 `Remover` 式的逻辑排除在注册集之外（或者你从一开始就没传 `tools=[add]`），而链上没有 `wrap_tool_call`。

给它补一个 `wrap_tool_call`，错误就消失：

```python
class Adder(AgentMiddleware):
    def wrap_model_call(self, request, handler):
        return handler(request.override(tools=[*request.tools, add]))

    def wrap_tool_call(self, request, handler):
        if request.tool is None and request.tool_call["name"] == "add":
            return handler(request.override(tool=add))
        return handler(request)
```

这一步同时验证了两件事：**校验被跳过**，以及**执行靠补票**。

### 阶段四：观察真实 DeepAgents

前三阶段用的是构造出来的 Middleware。要在真实项目里看，最省事的是在 `wrap_model_call` 里打一行日志，然后把三件事对照起来：

| 观察点 | 怎么拿 |
|---|---|
| 注册集 | `agent.nodes["tools"].bound.tools_by_name`（`ToolNode` 是 `RunnableCallable`，需要按版本取内层） |
| 本轮暴露集 | 在 Middleware 的 `wrap_model_call` 里打印 `request.tools` |
| 实际请求 | provider 侧日志，或 `LANGCHAIN_TRACING_V2=true` 看 trace 里的 `tools` 字段 |

第三个观察点最容易漏。前两个都在本地，第三个只有 provider 知道——这也呼应了第六章 6.5 节的五层结构。

---

## 十二、把问题与源码位置对起来

### 12.1 五个问题的直接回答

| 问题 | 一句话答案 | 源码 |
|---|---|---|
| `middleware.tools` 什么时候被收集 | 建图阶段，`create_agent` 用一个双层列表推导扫一遍所有 Middleware | `factory.py#L1089` |
| `ToolNode` 是不是把工具给了 LLM | **不是**。它只提供执行能力，Schema 由 `bind_tools` 生成 | `factory.py#L1493-L1514` |
| 为什么 `wrap_model_call` 也能加工具 | 因为它改的是 `ModelRequest.tools`，而 `bind_tools` 读的就是这个字段 | `types.py#L101` |
| `bind_tools` 什么时候调用 | 每次模型调用，在 handler 链最内层、`invoke` 之前 | `factory.py#L1350-L1490` |
| `_get_bound_model` 和 `model_node` 什么关系 | 后者是 Graph 节点，前者是它间接调用的闭包辅助函数 | `factory.py#L1520`、`#L1493` |

### 12.2 三个仍然成立的边界

**第一，`ToolNode` 不是「工具的真相」，只是「可执行工具的真相」。** 模型看见什么，由 `ModelRequest.tools` 决定；这两份清单可以不一致，而且源码鼓励它们不一致。

**第二，`bind_tools` 是「序列化」，不是「授权」。** 它不检查工具能不能执行，也不保证 provider 一定接受。真正的一致性检查在 `_get_bound_model` 开头的未知工具校验，而那个校验还会被 `wrap_tool_call` 的存在整体跳过。

**第三，动态工具的代价是执行期补票。** 你可以在 `wrap_model_call` 里加任何东西，但每加一个 ToolNode 不认识的工具，就欠一次 `request.override(tool=...)`。欠着不还，模型就会收到 `is not a valid tool`。

### 12.3 回到第十章

到这里，工具侧的三段链路已经完整：

```text
第九章      工具调用怎样落到文件上      ToolNode + Backend
第九章附一   这个落地过程由谁编排        FilesystemMiddleware
第九章附二   工具怎样走到模型面前        注册 / 暴露 / 执行
```

现在你能完整解释：为什么 `ToolNode` 注册了八个工具，模型却只看见五个；为什么 Skill 能让模型看见一个从未注册的工具；为什么加了 `wrap_tool_call` 之后那个 `ValueError` 就消失了。

但这条链还有一个默认前提：**工具一旦被调用，就会真的执行完。**

`execute` 会跑 `pytest`，`delete` 会删文件，`write_file` 会覆盖代码。它们在结构上都合法，Backend 也确实有能力。

**能执行，不等于应该直接执行。**

谁来在副作用发生之前踩一脚刹车？踩了之后 Agent 怎样暂停、怎样等一个人类看完、批准之后又怎样接着跑而不是从头再来？

这就是下一章的内容：

**第十章：Human-in-the-Loop——工具执行前如何中断、审批与恢复？**

---

## 附：本章源码导航

以下 LangChain 链接固定在 Commit `fbb8a35`，LangGraph 链接固定在 Tag `1.2.12`，DeepAgents 链接固定在 Commit `e85c6b9`：

| 想确认的问题 | 固定源码 |
|---|---|
| Middleware 工具收集 | [`factory.py#L1089`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/factory.py#L1089) |
| 三类工具分类与 `available_tools` | [`factory.py#L1130-L1137`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/factory.py#L1130-L1137) |
| `ToolNode` 创建条件 | [`factory.py#L1139-L1152`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/factory.py#L1139-L1152) |
| `default_tools` 的四条理由 | [`factory.py#L1154-L1158`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/factory.py#L1154-L1158) |
| `model_node` 返回 `list[Command]` 的收尾 | [`factory.py#L222-L278`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/factory.py#L222-L278) |
| `_handle_model_output`：结构化结果分流 | [`factory.py#L1246-L1303`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/factory.py#L1246-L1303) |
| `AgentState` 与 `jump_to` 两个注解 | [`types.py#L349-L354`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/types.py#L349-L354) |
| `wrap_tool_call` 链的组装 | [`factory.py#L1100-L1128`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/factory.py#L1100-L1128) |
| `wrap_model_call` 链的组装 | [`factory.py#L1195-L1224`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/factory.py#L1195-L1224) |
| handler 链顺序与 Commands 累积 | [`factory.py#L281-L340`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/factory.py#L281-L340) |
| 未知工具错误模板（含两种解法） | [`factory.py#L127-L157`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/factory.py#L127-L157) |
| 未知工具校验的两个跳过条件 | [`factory.py#L1369-L1397`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/factory.py#L1369-L1397) |
| `final_tools` 与三条 `bind_tools` 分支 | [`factory.py#L1434-L1490`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/factory.py#L1434-L1490) |
| `_execute_model_sync` 全貌 | [`factory.py#L1493-L1514`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/factory.py#L1493-L1514) |
| `model_node` 的三步 | [`factory.py#L1520-L1547`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/factory.py#L1520-L1547) |
| `_patch_invalid_tool_calls` | [`factory.py#L692-L707`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/factory.py#L692-L707) |
| `add_node("tools")` | [`factory.py#L1611-L1615`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/factory.py#L1611-L1615) |
| 两条条件边 | [`factory.py#L1744-L1786`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/factory.py#L1744-L1786) |
| `model → tools` 的 pending 判定与 `Send` | [`factory.py#L1991-L2043`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/factory.py#L1991-L2043) |
| `tools → model` 的 `return_direct` 判定 | [`factory.py#L2072-L2111`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/factory.py#L2072-L2111) |
| `compile()` 的位置 | [`factory.py#L1909`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/factory.py#L1909) |
| `ModelRequest` 字段与 `override` | [`types.py#L88-L197`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/types.py#L88-L197) |
| `ToolCallRequest` 与 `tool=None` 语义 | [`tool_node.py#L133-L196`](https://github.com/langchain-ai/langgraph/blob/1.2.12/libs/prebuilt/langgraph/prebuilt/tool_node.py#L133-L196) |
| `ToolNode.__init__` 与 `tools_by_name` | [`tool_node.py#L779-L791`](https://github.com/langchain-ai/langgraph/blob/1.2.12/libs/prebuilt/langgraph/prebuilt/tool_node.py#L779-L791) |
| `_parse_input` 的三种输入形态 | [`tool_node.py#L1224-L1266`](https://github.com/langchain-ai/langgraph/blob/1.2.12/libs/prebuilt/langgraph/prebuilt/tool_node.py#L1224-L1266) |
| 每个 call 一个 `ToolRuntime` | [`tool_node.py#L799-L817`](https://github.com/langchain-ai/langgraph/blob/1.2.12/libs/prebuilt/langgraph/prebuilt/tool_node.py#L799-L817) |
| `_combine_tool_outputs` 的返回形态 | [`tool_node.py#L862-L885`](https://github.com/langchain-ai/langgraph/blob/1.2.12/libs/prebuilt/langgraph/prebuilt/tool_node.py#L862-L885) |
| `_run_one` 与 `execute` 闭包 | [`tool_node.py#L1015-L1051`](https://github.com/langchain-ai/langgraph/blob/1.2.12/libs/prebuilt/langgraph/prebuilt/tool_node.py#L1015-L1051) |
| 校验推迟与 invalid 模板 | [`tool_node.py#L942-L950`](https://github.com/langchain-ai/langgraph/blob/1.2.12/libs/prebuilt/langgraph/prebuilt/tool_node.py#L942-L950)、[`#L1265-L1278`](https://github.com/langchain-ai/langgraph/blob/1.2.12/libs/prebuilt/langgraph/prebuilt/tool_node.py#L1265-L1278) |
| `GraphBubbleUp` 必须上抛 | [`tool_node.py#L969-L971`](https://github.com/langchain-ai/langgraph/blob/1.2.12/libs/prebuilt/langgraph/prebuilt/tool_node.py#L969-L971) |
| 对照组：按能力过滤工具 | [`filesystem.py#L3362-L3391`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L3362-L3391) |
| 对照组：整体剔除工具 | [`_tool_exclusion.py#L47-L56`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/_tool_exclusion.py#L47-L56) |
| 对照组：追加动态工具 | [`skills.py#L1300-L1321`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/skills.py#L1300-L1321)、[`_skill_tools.py#L342-L352`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/_skill_tools.py#L342-L352) |
| 对照组：执行期补票 | [`skills.py#L1366-L1393`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/skills.py#L1366-L1393) |
| 对照组：provider 拒绝的 Schema | [`_skill_tools.py#L354-L380`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/_skill_tools.py#L354-L380) |
| 对照组：DeepAgents 侧的装配与交接 | [`graph.py#L666-L669`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py#L666-L669)、[`#L1008-L1011`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py#L1008-L1011) |

**本章核心产出：** 读者现在能解释三段链路各自的负责人：装配阶段造出工具实例，建图阶段把它们登记成 `tools_by_name` 并抄一份到 `default_tools`，每次模型调用时 `wrap_model_call` 改写 `request.tools`、`bind_tools` 序列化、`invoke` 发出，模型返回 `tool_calls` 后由 `Send` 分发到 `ToolNode`、经 `wrap_tool_call` 补票、`tool.invoke()` 执行。也能说清两个节点为什么必须分开：`model_node` 负责不确定的决策，返回 `list[Command]`；`ToolNode` 负责相对确定的执行，返回消息更新。还能说清两个集合为什么不要求相等：**`ToolNode` 管执行能力，`ModelRequest.tools` 管模型可见性**；不一致时，缩小的一侧不需要兜底，放大的一侧必须有。
