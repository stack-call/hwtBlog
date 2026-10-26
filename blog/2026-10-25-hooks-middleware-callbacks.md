---
title: Hook、Middleware 与 Callback：三种扩展机制的边界
description: 用 Codex、OpenHands、LangChain、OpenCode 四个真实实现，对比三种「插入逻辑」方式的动作、能力上限、失败语义与执行顺序
slug: hooks-middleware-callbacks
authors: [hwt]
tags: [Agent, Harness, Hooks, Middleware]
---

给 Agent Harness 加扩展时，最容易被混用的三个词是 Hook、Middleware 和 Callback。它们看起来都是「在某个时刻插入一段逻辑」，实际能做的事、失败的后果、作用的边界完全不同。这篇用四个开源实现的真实代码来对照。

<!-- truncate -->

## 一句话区分

| 机制 | 动作 | 信息流向 | 能否改变主流程 |
| --- | --- | --- | --- |
| Callback | 通知（notify） | 事件 → 外部 | 不能，只观察 |
| Hook | 挂载（attach） | 事件 → 外部 → 决定 | 能，阻断或改写 |
| Middleware | 包装（wrap） | 请求与响应各包一层 | 能，改写且可持有状态 |

这三者不是同一维度的三种实现，而是三种**不同的侵入方式**：

- **Callback** 是旁路。主流程照常跑，跑完顺手告诉你一声。
- **Hook** 是切点。主流程在某个位置停下来问你一句，你的回答会改变它接下来的动作。
- **Middleware** 是夹层。某次调用被整个包住，你可以在前后各做一次处理，并且这次处理与那次调用处在同一条调用栈上。

这个区别看着像措辞之争，但它在四个地方会产生实际后果：能力上限、失败语义、执行顺序、可移植性。

## 四个真实实现

### Codex：hooks 是一等公民

Codex 把 hooks 做成了独立 crate，事件名是一张固定表：

```rust
pub const HOOK_EVENT_NAMES: [&str; 14] = [
    "PreModelRequest", "PostModelRequest",
    "PreToolUse", "PermissionRequest", "PostToolUse",
    "PreCompact", "PostCompact",
    "SessionStart", "SessionEnd", "UserPromptSubmit",
    "SubagentStart", "SubagentStop", "Stop", "Interrupt",
];
```

其中只有 9 个事件支持 matcher（按工具名、压缩触发原因、会话来源等过滤），其余事件的 matcher 字段会被直接忽略。这一点值得注意：**事件粒度不是平均设计的**。`PreToolUse` 需要按工具区分，`Stop` 不需要。

更有意思的是 handler 的类型不止一种：

| handler 类型 | 含义 |
| --- | --- |
| `command` | 外部进程，stdin 收 JSON、stdout 回结果 |
| `mcp_tool` | 调用一个 MCP 工具 |
| `prompt` | 用一次 LLM 补全来判断 |
| `agent` | 起一个 Agent 来判断 |

也就是说，Codex 允许「由另一个模型决定这个工具调用该不该放行」。这是 Hook 与 Policy 边界开始模糊的地方——判断逻辑本身变成了一个需要被观测、被计费、可能失败的外部依赖。

执行模式分 `sync` 与 `async`。**只有 sync 的 hook 才有资格阻断**，async 只能事后告警，因为它返回结果时操作早就发生了。

Hook 的返回值是三态，而不是布尔：

```rust
pub enum HookResult {
    Success,
    FailedContinue(Box<dyn Error + Send + Sync>),  // 我失败了，但别影响后面的 hook 和操作
    FailedAbort(Box<dyn Error + Send + Sync>),     // 我失败了，后面的 hook 不要跑，操作也别做
}
```

这两个变体的区别，就是「Hook 的错误策略必须按用途区分」的可实现版本。

`PreToolUse` 的产出结构则直接暴露了 Hook 的能力上限：

```rust
pub struct PreToolUseOutcome {
    pub hook_events: Vec<HookCompletedEvent>,
    pub should_block: bool,
    pub block_reason: Option<String>,
    pub additional_contexts: Vec<HookContext>,
    pub updated_input: Option<Value>,   // 改写工具参数
}
```

一个 hook 可以同时做到四件事：阻断、给出理由、往上下文里塞一段额外信息、**改掉工具参数**。最后一项是后面「三个坑」里第一个坑的来源。

### OpenHands：callbacks 和 hooks 同时存在

OpenHands 是这组对比里最有说服力的例子，因为它**两套机制都有**，而且分工清楚。

观测走 callbacks：

```python
def __init__(
    self,
    ...
    callbacks: list[ConversationCallbackType] | None = None,
    token_callbacks: list[ConversationTokenCallbackType] | None = None,
    stream_callbacks: list[StreamProgressCallbackType] | None = None,
```

每个事件发生后调用一次，用来落盘、推给前端、做流式展示。源码里的组合顺序也说明了它的定位——默认回调负责持久化，排在调用方传入的回调**之前**：

```python
composed_list = [_default_callback] + callback_list + [_track_last_user_message]
```

先保证事件已经存下来，再通知外部消费者。

控制走 hooks，是另一套东西：

```python
class HookType(str, Enum):
    COMMAND = "command"   # 子进程执行
    PROMPT = "prompt"     # 单次 LLM 判断
    AGENT = "agent"       # 带工具的 Agent 判断

class HookDecision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
```

事件集合比 Codex 小得多（`PreToolUse`、`PostToolUse`、`UserPromptSubmit`、`SessionStart`、`SessionEnd`、`Stop`），没有 PostModelRequest、没有 Interrupt、没有 Subagent 系列。这不是疏漏，是取舍：事件越多，每个事件的语义就越难说清，维护成本按事件数线性增长。

一个项目同时提供 callbacks 和 hooks，等于承认了一件事：**「我想知道发生了什么」和「我想改变会发生什么」是两个需求，不该用同一个接口承载。**

### LangChain / DeepAgents：middleware 是包装链

LangChain 的 `AgentMiddleware` 不叫 hook，方法名直接说明它是包在调用链上的：

| 方法 | 时机 | 能否改写 |
| --- | --- | --- |
| `before_agent` | 整个 Agent 开始前 | 返回状态更新 |
| `before_model` | 每次模型调用前 | 返回状态更新 |
| `wrap_model_call` | 包住一次模型调用 | 改写请求、改写响应 |
| `after_model` | 模型返回后 | 返回状态更新 |
| `wrap_tool_call` | 包住一次工具调用 | 改写调用、改写结果 |
| `after_agent` | 整个 Agent 结束后 | 返回状态更新 |

还有三个字段值得单独说：

- `state_schema`：middleware 可以往 Agent 状态里加字段，于是能**跨多次调用持有状态**，而不像 hook 那样每次都是无状态的。
- `tools`：middleware 可以顺便注册新工具。Codex 的 hook 做不到这件事——hook 只能观察和干预已有工具。
- `can_jump_to`：可以声明跳转目标，改变控制流走向，而不只是「放行或阻断」的二选一。

### OpenCode：进程内插件，hook 可以直接改参数

OpenCode 的 plugin 是 JavaScript / TypeScript 模块，返回一个 hooks 对象：

```ts
export const MyPlugin = async (ctx) => {
  return {
    "tool.execute.before": async (input, output) => {
      if (input.tool === "bash") {
        output.args.command = escape(output.args.command);
      }
    },
  };
};
```

这段来自官方文档的例子，正好演示了「就地改写」：拿到 `output.args` 直接改，不需要返回什么决定。和 Codex 的 `updated_input: Option<Value>` 是同一个能力，只是表达方式不同——一个是**函数式返回新值**，一个是**命令式改可变对象**。

命令式更短，代价是**副作用不可见**：无法从返回值看出参数被改过，只能靠读代码或者依赖日志。

插件还能订阅事件，用于纯观测：

```text
session.compacted     session.error        permission.asked
message.updated       file.edited          lsp.client.diagnostics
tool.execute.after    session.idle         server.connected
```

加载顺序是全局配置 → 项目配置 → 全局插件目录 → 项目插件目录，同类 hook 按这个顺序依次执行。

## 能力矩阵

把四个实现的能力摊开，能看出三者的真实边界：

| 能力 | Callback | Hook | Middleware |
| --- | --- | --- | --- |
| 观察事件 | 是 | 是 | 是 |
| 阻断当前操作 | 否 | 是 | 是（抛异常） |
| 改写入参 | 否 | 是 | 是 |
| 改写返回值 | 否 | 部分 | 是 |
| 往上下文注入信息 | 否 | 是 | 是 |
| 新增工具 | 否 | 否 | 是 |
| 改变控制流走向 | 否 | 只能阻断 | 是 |
| 跨调用持有状态 | 否 | 否 | 是 |
| 与主循环共享类型 | 否 | 否 | 是 |
| 可跨进程 | 不必要 | 是 | 否 |

最后两行是选型时最容易被忽略的：

**跨进程是 Hook 的核心价值，也是它的核心成本。** 一个 `command` 类型的 hook 可以用任何语言写、可以独立升级、崩溃了也不会拖垮主进程。代价是每次调用都要序列化、要处理超时、要约定 JSON schema，而且拿不到主循环里的类型。

**Middleware 共享类型，所以写起来最舒服，但也最难移植。** 它直接操作 Agent 的状态对象，能力最强；一旦换了框架，这段代码基本要重写。

## 失败语义

三种机制在「自己出错了怎么办」上的默认行为完全不同：

| 机制 | 默认失败行为 | 是否合理 |
| --- | --- | --- |
| Callback | 应被隔离，不影响主流程 | 是，观测失败不该阻断业务 |
| Hook | 由 hook 自己声明（Codex 三态） | 是，但要按用途分别指定 |
| Middleware | 异常向上传播，默认 fail-closed | 是，但要小心把观测代码放进 middleware |

这里有一条可以直接用的判据：

> **如果这段逻辑失败后，主流程继续跑会造成不可接受的后果，它就必须 fail-closed；如果它只是让你少看到一些信息，它就应该 fail-open。**

按这条判据：

- 安全扫描、权限校验、参数合法性检查 → fail-closed，失败即拒绝执行。
- 审计日志上报、埋点、通知 → fail-open，失败就记一条本地警告，继续跑。
- 上下文压缩、摘要生成 → 需要区分。压缩失败应当降级为「不压缩、直接截断」，而不是让整个任务失败。

Codex 的 `FailedContinue` / `FailedAbort` 就是把这个判断交给配置者，而不是在框架里写死。这是比较成熟的做法。

## 顺序

三种机制的顺序规则不一样，写扩展时经常在这里出问题。

**Middleware 是嵌套。** 注册顺序 A、B、C，进入是 A→B→C，退出是 C→B→A：

```text
A.before → B.before → C.before
              ↓
         真实模型调用
              ↓
C.after  → B.after  → A.after
```

所以「谁最后改写请求」是 C，「谁最后改写响应」是 A。如果 A 是安全策略、C 是日志，那么 C 看到的是原始请求，A 看到的是被 C 改过的响应——这个反直觉的顺序需要专门记一下。

**Hook 是串联。** 按配置顺序依次执行，遇到阻断就停。Codex 用 `key_source:event:group_index:handler_index` 生成持久化的 hook key，说明它的顺序是**配置里写死的位置**，而不是运行时动态排序。

**Callback 是注册顺序。** OpenHands 明确把默认的持久化回调放在最前面，保证「事件先落盘，再通知外部」。

## 中间件能不能用 Hook 实现？

这是最容易追问、也最值得说清的一点。

**结论：能实现大部分，但有三件事做不到；而反过来，用 Middleware 或 Callback 实现 Hook 要容易得多。**

根本差异不在「谁更强」，而在**寻址方式**：

- **Hook 按「何时」寻址**——时间轴上的一个点。注册的是「工具执行前」这个时刻，至于这个时刻前后发生什么，它不掌握。
- **Middleware 按「包住什么」寻址**——调用栈上的一层。注册的是「这次工具调用」这个整体，于是它天然拥有这次调用的开始、结束、中间结果和异常。

时间轴上的点是离散的，调用栈上的层是连续的。想用前者表达后者，就必须自己把那些点重新连成一条栈。

### 用 Hook 模拟 Middleware

以 `before_tool` / `after_tool` 两个事件为例，模拟一个「计时 + 审计 + 改参数」的 middleware：

```python
# 用 hook 对模拟 middleware：状态必须外置
pending = {}   # call_id -> 开始时间 / 原始参数

def on_before_tool(call):
    pending[call.call_id] = {"start": now(), "args": call.arguments}
    call.arguments = normalize(call.arguments)   # 改写入参
    return {"updated_input": call.arguments}

def on_after_tool(result):
    st = pending.pop(result.call_id, None)
    if st is None:
        return          # 没有配对的 before，说明被取消或 before 失败了
    audit.log(result.call_id, now() - st["start"], result.status)
```

这段代码确实能工作，覆盖了观测、计时、审计、改写入参、改写结果。**但它暴露了模拟的全部成本**：`pending` 这张表就是被拆散的调用栈。middleware 里这些状态是函数局部变量，随栈帧自动存活和销毁；用 hook 就必须手动管理生命周期，还要处理三种异常情况：

- `before` 执行了、`after` 没来（工具被取消、进程崩溃）；
- `before` 自己失败了（`after` 仍可能被调用，此时 `pending` 里没有记录）；
- 嵌套调用（子 Agent 的工具调用与父级交错，`call_id` 必须全局唯一）。

### 三件做不到的事

即使把状态管理写得很完善，下面三件事仍然是 Hook 的结构性缺口：

**1. 短路并返回替代结果。** middleware 可以实现缓存：命中缓存就不调用真实工具，直接返回缓存值。Hook 做不到——它只能「阻断」，不能「代替」。看 Codex 的 `PreToolUseOutcome` 就明白：

```rust
pub should_block: bool,
pub block_reason: Option<String>,
pub updated_input: Option<Value>,
```

有 `block`，没有「结果」。阻断的语义是「这次调用不执行了，把原因告诉模型」，而不是「这次调用的结果是 X」。想在 Hook 里做缓存，只能让模型再发一次调用，或者干脆放弃缓存。

**2. 控制调用这个动作本身。** middleware 包住的是一个**可调用对象**，所以它可以：调用零次（跳过）、调用两次（重试）、换一个参数再调（降级）、调用另一个工具（fallback）。Hook 拿到的是一个**事件**，事件没有「调用」这个能力。重试和降级只能由主循环提供支持，Hook 顶多建议一下。

**3. 在流式过程中插入。** middleware 包住的是 stream，可以在 chunk 之间做处理——截断超长输出、脱敏、累计 Token。Hook 只有 start 和 end 两个点，中间过程它看不见。第 16 章讲流式解析时，Adapter 必须在 chunk 级别工作，这就是为什么它只能是 middleware 而不能是 hook。

### 还有两个隐性成本

**after 的顺序要自己反转。** middleware 的退出顺序是注册顺序的逆序，这是嵌套结构的自然结果。Hook 是平铺串联，`after` 默认按注册顺序跑——想让「A 的 after 在 B 的 after 之后」就必须维护两套顺序，或者让框架提供「after 反序」的约定。

**多个 Hook 改同一份参数时，合并规则必须显式定义。** Codex 的 `updated_input` 是单个 `Option<Value>`，如果两个 hook 都改参数，谁赢？middleware 不存在这个问题——外层改写的是请求，内层看到的就是改写后的请求，嵌套顺序天然决定了覆盖关系。

### 反方向：用 Middleware 实现 Hook 很容易

反过来几乎没有成本。middleware 在 `wrap_tool_call` 里发两个事件出去，就是一对 hook 触发点；框架甚至可以同时暴露两套 API，内部用同一套实现。

OpenHands 就是这么做的。它的 hook 系统通过一个 callback 接进事件流：

```python
from openhands.sdk.hooks import HookConfig, HookEventProcessor, create_hook_callback
```

hook 的处理器本身就是一个 callback，挂在事件流上观察 `PreToolUse`、`PostToolUse` 这些事件，再决定 `allow` / `deny`。**hooks 架在 callbacks 之上**——这正好印证了前面的判断：控制能力更强的机制，可以向下模拟控制能力更弱的机制。

### 所以怎么判断

如果你发现自己在做这件事：

```text
before_tool  →  存一张表（key = call_id）
after_tool   →  查表、清理、补上下文
```

那你其实需要的是 middleware。这张表就是被你手动拆开的调用栈。

反过来，如果你需要的是跨进程、跨语言、热更新、独立部署——那就别硬套 middleware。这些恰恰是 Hook 的价值，而 middleware 一条都给不了。

一句话总结：**表达能力取决于扩展点，不取决于名字。** 一个只有 `before_tool` / `after_tool` 的 hook 系统，和一个有 `PreCompact`、`SessionStart`、`Interrupt` 的 hook 系统，名字一样但能力差得远；同样，一个只有 `wrap_tool_call` 的 middleware 系统也观测不到压缩事件。选型时应该问「它暴露了哪些扩展点」，而不是「它叫 hook 还是 middleware」。

## DeepAgents 的 middleware 是怎么实现的

前面一直把 middleware 当成一个整体概念，但拆开看会发现：**它不是一种机制，而是两种机制被同一个基类藏了起来。** DeepAgents 的 middleware 是理解这一点最好的样本，因为它足够大、足够真实。

### 第一层：middleware 本身只是 LangChain 的基类

DeepAgents 没有自己发明 middleware。它的所有 middleware 都继承 LangChain 的 `AgentMiddleware`：

```python
class FilesystemMiddleware(AgentMiddleware[FilesystemState, ContextT, ResponseT]):
class MemoryMiddleware(AgentMiddleware[MemoryState, ContextT, ResponseT]):
class SkillsMiddleware(AgentMiddleware[SkillsState, ContextT, ResponseT]):
```

`create_deep_agent` 做的主要事情，就是**按顺序拼一个 middleware 列表**，然后交给 `create_agent`：

```python
deepagent_middleware: list[AgentMiddleware] = [
    FilesystemMiddleware(backend=backend, permissions=permissions),
]
if inline_subagents:
    deepagent_middleware.append(SubAgentMiddleware(...))
deepagent_middleware.extend([
    _ParentNotifierMiddleware(),
    create_summarization_middleware(model, backend),
    PatchToolCallsMiddleware(),
])
# ... 再追加 profile、Skills、prompt caching、Memory、HITL、UnsupportedContent
```

顺序不是随手排的。源码注释里写得很清楚：profile middleware 必须排在 memory 之前，否则 memory 更新会改变 system prompt，让 Anthropic 的 prompt cache 前缀失效；Skills 必须排在 caching 内侧，这样它看到的是压缩后的对话。

**这是 middleware 列表的第一个含义：顺序即语义。** 后面会看到，这个顺序在不同 hook 上产生完全不同的效果。

### 第二层：装配时，六种 hook 被分成两类

真正关键的地方在 LangChain 的 `create_agent`。它遍历 middleware 列表，用「方法是否被重写」来判断这个 middleware 用到了哪些 hook：

```python
middleware_w_before_model = [
    m for m in middleware
    if m.__class__.before_model is not AgentMiddleware.before_model
    or m.__class__.abefore_model is not AgentMiddleware.abefore_model
]
```

然后分两路处理，**这就是 middleware 的秘密**：

**第一类：`before_*` / `after_*` 被编译成图节点。**

```python
graph.add_node(f"{m.name}.before_model", before_node, input_schema=resolved_state_schema)
```

每个 middleware 的每个 node hook 都变成 LangGraph 里一个真实节点，名字形如 `MemoryMiddleware.before_agent`。然后按列表顺序连边：

```python
entry_node      = f"{middleware_w_before_agent[0].name}.before_agent"
loop_entry_node = f"{middleware_w_before_model[0].name}.before_model"
exit_node       = f"{middleware_w_after_agent[-1].name}.after_agent"
```

注意 `before` 取第一个、`after` 取最后一个。所以在 LangGraph 的可视化里，middleware 的 node hook 是**平铺的一串节点**，不是嵌套。

**第二类：`wrap_*` 被闭包组合成一条链。**

```python
wrap_model_call_handler = _chain_model_call_handlers(sync_handlers)
```

不创建任何节点。`_chain_model_call_handlers` 把 N 个 handler 折叠成一个，第一个在最外层：

```text
wrapper = _chain_tool_call_wrappers([auth, cache, retry])
# Request flows:  auth -> cache -> retry -> tool
# Response flows: tool -> retry -> cache -> auth
```

这段注释来自 `_chain_tool_call_wrappers` 的 docstring，和前面讲的「洋葱」完全一致。

**所以 DeepAgents 的 middleware 是两套东西的合称**：

| hook 类型 | 实现方式 | 在图中可见 | 顺序 |
| --- | --- | --- | --- |
| `before_agent` / `before_model` | LangGraph 节点 | 是 | 正序 |
| `after_model` / `after_agent` | LangGraph 节点 | 是 | 正序（取最后一个为出口） |
| `wrap_model_call` | 闭包链 | 否 | 嵌套，首个最外 |
| `wrap_tool_call` | 闭包链 | 否 | 嵌套，首个最外 |

同一份 middleware 列表，一半编译成图结构，一半编译成函数调用链。这解释了一个常见困惑：为什么 middleware 里既有「返回状态更新」的写法，又有「调用 handler」的写法——它们根本不是同一套调度器。

### 那 before 算 hook，wrap 算 middleware 吗

**算。而且框架自己的命名就支持这个判断。**

`before_model` / `after_model` 在源码里被直接称作 hook：

```python
def _get_can_jump_to(middleware: AgentMiddleware, hook_name: str) -> list[JumpTo]:
    """Get the `can_jump_to` list from either sync or async hook methods.

    Args:
        hook_name: The name of the hook (`'before_model'` or `'after_model'`).
    """
```

而 `wrap_*` 的组装函数被称作 middleware stack：

```python
"""Compose wrappers into middleware stack (first = outermost)."""
```

两者用的是不同的词，而且不是笔误——`hook` 指向「某个时刻」，`middleware stack` 指向「一层层包起来的栈」。

但要注意，这不意味着它们和前面讲的「跨进程 command hook」是一回事。更准确的描述要分两个维度：

| | 寻址方式 | 实现方式 | 结论 |
| --- | --- | --- | --- |
| `before_*` / `after_*` | 时间点 | 进程内 Python，编译成图节点 | hook 语义，进程内实现 |
| `wrap_*` | 调用栈 | 进程内 Python，组合成闭包链 | middleware 语义，进程内实现 |
| Codex `command` hook | 时间点 | 跨进程，JSON over stdin/stdout | hook 语义，跨进程实现 |

所以「before 是 hook」说的是**寻址方式**：它在时间轴上占一个点，拿不到「这次调用」这个整体。而 Codex 那种 hook 额外还有跨进程的好处，LangChain 的 `before_model` 没有。

判据可以压缩成一句话：

> **看它有没有收到 `handler`。**
>
> - `wrap_model_call(self, request, handler)` → 有 handler，能决定调用几次 → middleware。
> - `before_model(self, state, runtime)` → 没有 handler，只能在固定的点上改状态 → hook。

这也是为什么 `before_model` 无法实现缓存命中：它拿不到「执行」这个动作，只有「即将执行」这个时刻。

**不过 LangChain 自己也没把这个区分贯彻到底。** 同一个类里，`trace_policy` 的注释又把两者都叫 hook：

```python
"""Optional trace policy for this middleware's hook spans (`wrap_model_call`/
`wrap_tool_call` and the `before_*`/`after_*` node hooks).
```

这里 `wrap_*` 也被算进了 "hook spans"。所以更准确的说法是：**框架在调度层面把两类分得很清楚（一个是图节点，一个是闭包链），但在命名层面没有统一**。`before_*` / `after_*` 有时被叫 hook，有时被叫 node hook；`wrap_*` 有时被叫 middleware，有时被叫 hook span。

这个混乱是有代价的：如果只看 API，两类方法长得一样（都是 `AgentMiddleware` 上的一个 def），但顺序规则、失败语义、能力上限全都不同。**唯一可靠的区分方式是看签名里有没有 `handler`。**

### 第三层：状态是怎么合并的

`state_schema` 让 middleware 能往 Agent 状态里加字段，实现方式是把所有 schema 合并成一个 `TypedDict`：

```python
state_schemas = [*(m.state_schema for m in middleware), base_state]
resolved_state_schema, input_schema, output_schema = _resolve_schemas(state_schemas)
```

注释说明得很明确：**middleware 的 schema 在前，`base_state` 在最后，所以字段冲突时以调用方显式传入的为准。** 这是个合理的默认值——框架不该覆盖使用者声明的类型。

`FilesystemMiddleware` 就是这么加字段的，而且用了一个值得注意的通道：

```python
class FilesystemState(AgentState):
    files: Annotated[
        NotRequired[dict[str, FileData]],
        DeltaChannel(_file_data_delta_reducer, snapshot_frequency=50),
    ]
```

`DeltaChannel` 带一个 reducer 和「每 50 步存一次快照」的参数。**middleware 不只是加字段，还能指定这个字段在 LangGraph 里如何累积、如何 checkpoint。** 这是 hook 完全没有的能力——hook 的 payload 是一次性的 JSON，没有「通道」这个概念。

### 三个真实 middleware，三种写法

**`PatchToolCallsMiddleware`：纯 node hook，最短的一个**

全文不到 60 行，只实现 `before_agent`。作用是扫描历史里「有 tool_call 但没有对应 ToolMessage」的悬空调用，补一条错误消息：

```python
content = (
    f"Tool call {name} with id {tool_call_id} did not complete - no result was recorded. "
    "It may have been cancelled or interrupted."
)
patched_messages.append(ToolMessage(content=content, name=name, tool_call_id=tool_call_id, status="error"))
return {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *patched_messages]}
```

它返回的是一个**状态更新**，不是执行结果。这正是 node hook 的形态：不参与调用，只修改状态，然后交给下一个节点。

这个 middleware 存在本身说明了一件事：**中断、取消、崩溃会留下不完整的消息历史，而模型协议要求 tool_call 和 tool_result 必须配对。** 修复这件事没有更优雅的位置，只能放在 Agent 开始之前。

**`MemoryMiddleware`：node hook + wrap hook 混用**

`before_agent` 负责加载 `AGENTS.md` 内容并写进状态：

```python
def before_agent(self, state: MemoryState, runtime: Runtime, config: RunnableConfig):
    if "memory_contents" in state:
        return None      # 已加载就跳过
    ...
    return MemoryStateUpdate(memory_contents=contents)
```

`wrap_model_call` 负责把这份内容注入到 system prompt。分成两个 hook 不是随意的：**加载是「一次」，注入是「每次」。** 如果都放在 `wrap_model_call` 里，每轮模型调用都会重新读一遍文件。

这是一个可以直接借用的模式：**把「读」和「用」拆到不同的 hook 上。** `before_agent` 只跑一次，适合做昂贵的 IO；`wrap_model_call` 每轮都跑，只做内存里的拼装。

**`FilesystemMiddleware`：只实现 wrap，但把 wrap 用到了极致**

170KB 的文件，`wrap_model_call` 和 `wrap_tool_call` 各一个，没有任何 node hook。`wrap_tool_call` 的内容很值得看：

```python
if error := _parallel_file_mutation_error(request):
    return error                                    # ① 短路，不调用 handler
with _tool_blob_store(...):
    tool_result = handler(request)                   # ② 真正执行
if self._offload_binary_content and request.tool_call["name"] == "read_file":
    tool_result = _offload_tool_result(...)          # ③ 改写结果
if self._tool_token_limit_before_evict is None or ...:
    return tool_result
return self._intercept_large_tool_result(tool_result)  # ④ 再改写：超大结果落盘
```

四步全齐：**短路、执行、改写结果、再次改写**。这就是前面说 hook 做不到的那几件事，在真实代码里的样子。

`wrap_model_call` 更极端，它在 `handler` 外面套了 try：

```python
try:
    response = handler(request)
except ModelInvalidRequestError:
    messages = _replace_rejected_file_content(request.messages)
    if messages == request.messages:
        raise
    response = handler(request.override(messages=messages))   # 换参数重试一次
```

**调用 handler 两次**，第二次换了消息内容。这在 hook 里无法表达——hook 拿到的是事件，事件不能「再执行一次」。

它最后返回 `ExtendedModelResponse(model_response=response, command=state_command)`，把「模型响应」和「状态更新」一起带出去。这是 wrap hook 的一个特殊约定：包在里面的东西，可以在返回时顺手改状态。

### 那它到底算 hook 还是 middleware

按前面的判据看：

| 判据 | DeepAgents 的 middleware |
| --- | --- |
| 能短路吗 | 能（`wrap_tool_call` 直接 return error） |
| 能改写结果吗 | 能（offload、evict、truncate） |
| 能控制调用次数吗 | 能（`wrap_model_call` 里重试） |
| 能持有状态吗 | 能（`state_schema` + `DeltaChannel`） |
| 能跨进程吗 | 不能，全是进程内 Python |
| 能独立升级吗 | 不能，与框架版本强绑定 |

它满足了 middleware 的全部特征，一条 hook 的优势都不占。**但它内部的 `before_*` 部分，本质上就是被编译成了图节点的 hook。**

所以更准确的表述是：

> **DeepAgents 的 middleware 是一个统一的门面，下面藏着两套调度：node hook 编译成图节点，wrap hook 组合成闭包链。**

这个设计的好处是使用者只需要记住一个概念、继承一个基类；代价是这两类 hook 的语义差异被藏起来了——顺序规则不同、能否短路不同、能否重试不同，而 API 看起来一样。写 middleware 时如果没意识到这一点，很容易在 `before_model` 里做本该在 `wrap_model_call` 里做的事，然后发现「拿不到结果」或者「没法重试」。

### 为什么 middleware 里能写「观测」

回到前面那条判据：middleware 默认 fail-closed，所以不适合放观测代码。DeepAgents 的解法是 `trace_policy`：

```python
class PatchToolCallsMiddleware(AgentMiddleware):
    trace_policy = TracePolicy(process_inputs=omit_payload)
```

`omit_payload` 表示：这个 hook 的 span 照常记录，但**输入 payload 不落盘**。`FilesystemMiddleware` 和 `MemoryMiddleware` 也都声明了这一行。

这是一个很细的设计：**观测逻辑本身不 fail-closed，但它的数据采集可以单独降级。** 与其把观测挪到 callback，不如让 middleware 声明「我这段不想被完整追踪」。

## 三个坑

### 坑一：hook 改写参数后，审批绑定失效

审批必须绑定到具体参数，否则会出现「批准的是订单 A、执行的是订单 B」。但如果 hook 在审批**之前**改写了参数，审批看到的是改写后的值；如果在审批**之后**改写，审批就形同虚设。

Codex 的 `PreToolUseOutcome` 同时有 `updated_input` 和 `should_block`，两个字段出现在同一个结构里，说明这个顺序问题在设计时就被考虑过：改写和阻断发生在同一个决策点，审批在这个点之后进行。

设计时应该明确写出这条不变量：

```text
最终执行参数 = hook 改写后的参数
审批对象     = 最终执行参数（含 hash）
执行前校验   = 重新计算 hash，与审批记录比对
```

如果做不到，就禁止在审批之后改写参数。

### 坑二：异步 hook 与取消

`async` 执行模式的 hook 无法阻断操作，因为它的结果在操作结束之后才回来。这类 hook 只能做**事后审计**：记录「这次操作不符合规则」，供人工复查。

危险的是把它当成防护。一个配置成 async 的安全扫描，看起来在跑，实际上一次都没拦住过。Codex 把 `sync` / `async` 做成显式配置项而不是默认行为，就是为了让这个取舍无法被忽略。

### 坑三：用 callback 做控制，或用 hook 做观测

这两个方向都会出问题，但表现相反：

- **用 callback 做控制**：回调抛异常想阻止操作，但主流程没有为此设计，异常可能被吞掉，或者被当成「观测失败」忽略。更糟的是它在操作**之后**才被调用，根本来不及拦。
- **用 hook 做观测**：每次观测都要走一次协议往返，白白增加延迟和失败面；而且观测逻辑一旦 fail-closed，日志系统抖动会直接变成业务故障。

判断方法很简单：**这段逻辑如果删掉，主流程的行为会不会变？** 会变就是控制，用 hook 或 middleware；不会变就是观测，用 callback。

## 怎么选

按顺序问三个问题：

1. **需要改变行为吗？**
   - 不需要 → Callback。
   - 需要 → 下一题。
2. **需要跨进程、跨语言、独立部署吗？**
   - 需要 → Hook。
   - 不需要 → 下一题。
3. **需要持有状态、新增工具、或者改变控制流走向吗？**
   - 需要 → Middleware。
   - 不需要 → Hook 也可以，取决于你更看重解耦还是开发效率。

再补一条经验判断：**Hook 适合「规则」，Middleware 适合「能力」。** 规则通常是少数几条、变更频繁、由不同团队维护，适合放在进程外；能力通常与主循环的类型深度耦合，适合放在进程内。

## 参考实现

- Codex hooks 的事件表与导出：[`codex-rs/hooks/src/lib.rs`](https://github.com/openai/codex/blob/main/codex-rs/hooks/src/lib.rs)
- Codex `PreToolUse` 的产出结构：[`codex-rs/hooks/src/events/pre_tool_use.rs`](https://github.com/openai/codex/blob/main/codex-rs/hooks/src/events/pre_tool_use.rs)
- Codex hook 的返回三态：[`codex-rs/hooks/src/types.rs`](https://github.com/openai/codex/blob/main/codex-rs/hooks/src/types.rs)
- OpenHands hooks 的类型与决定：[`openhands/sdk/hooks/types.py`](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-sdk/openhands/sdk/hooks/types.py)
- OpenHands callbacks 的组合顺序：[`conversation/impl/local_conversation.py`](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-sdk/openhands/sdk/conversation/impl/local_conversation.py)
- LangChain `AgentMiddleware` 的方法定义：[`agents/middleware/types.py`](https://github.com/langchain-ai/langchain/blob/master/libs/langchain_v1/langchain/agents/middleware/types.py)
- OpenCode 插件文档：[Plugins](https://opencode.ai/docs/plugins/)

源码以 2026-10-25 读到的版本为准。这几个项目的扩展接口改动都很快，路径和字段名可能已经变了。
