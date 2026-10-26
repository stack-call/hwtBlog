---
title: 第十章附：同一次审批，两条执行路径——batched 与 per_call 的内部实现对照
description: 沿 LangChain 1.4.7（fbb8a35）的 human_in_the_loop.py 逐函数拆解两种审批模式：batched 的 after_model 为何要改写消息账本、_process_decision 的四个分支、拒绝文案为何要写 Do not retry、hitl_edited_tool_calls 为什么不直接改 AIMessage、_apply_edit 的同名替换与改名重定向、notice 注入的两种内容形状、per_call 的 _resolve_per_call 四步、_decision_schema 的运行时 Pydantic 建模、Literal 锁死工具名、单决策为何退化、_edit_args 为什么不在审批时跑 validator，以及 when 谓词在两种模式下拿到的根本不是同一个对象
tags: [DeepAgents, LangChain, Human-in-the-Loop, Middleware, interrupt, batched, per_call, Pydantic, 源码精读]
sidebar_position: 15
---

# 第十章附：同一次审批，两条执行路径

> **本章要回答的问题：** 第十章说明了 `batched` 在 `after_model()` 里一次收齐、`per_call` 在 `wrap_tool_call()` 里逐个中断。但"审批"这两个字背后还有一整套动作：决定怎么被解析、被拒绝的调用怎么从 `tool_calls` 里消失、编辑后的参数在什么时刻才真正替换进去、模型又怎么知道自己看到的调用并不是真正执行的那一个。同一个 `approve`，在两种模式里走的是同一条路吗？

## 一、先把两条调用栈并排摆出来

上一章（[第十章](./10-human-in-the-loop-interrupt-resume.md)）停在了一个位置差异上：

```text
batched（默认）：
ModelNode → after_model → interrupt(本轮待审调用集合) → ToolNode

per_call：
ModelNode → ToolNode → wrap_tool_call → interrupt(单次调用) → handler
```

这个对比是对的，但它只回答了"在哪儿停"，没有回答"停下来之后怎么处理"。

我们先给一个具体的场景。模型一次提出两个调用，只有第二个需要审批：

```python
# 教学示例：模型可能生成的一轮 tool_calls
tool_calls = [
    {"name": "read_file", "args": {"file_path": "/shop/refund.py"}, "id": "call_1"},
    {"name": "write_file", "args": {"file_path": "/shop/refund.py", "content": "v1"}, "id": "call_2"},
]
```

审批者选择 `edit`，把 `content` 从 `v1` 改成 `v2`。

接下来，两种模式要回答同一个问题：

> 这个 `v2`，是从哪一步开始生效的？

`batched` 的回答是：**从 `ToolNode` 调用 `handler` 的那一刻开始生效，而在 `after_model` 返回时，`AIMessage.tool_calls` 里仍然是 `v1`。**

`per_call` 的回答是：**就在同一个 `wrap_tool_call` 里生效，`interrupt` 返回之后立刻把 request 换掉。**

这两句回答的差别，就是本章要展开的全部内容。先把两条调用栈写全：

```text
batched：
after_model()
  ├── ① 判定：本轮有没有需要审批的调用
  ├── ② 收集：ActionRequest + ReviewConfig + 原始下标
  ├── ③ 暂停：interrupt(HITLRequest) → decisions
  ├── ④ 校验：decisions 数量是否等于待审数量
  ├── ⑤ 重建：逐个 _process_decision()，重写 tool_calls
  └── ⑥ 落账：写入 hitl_edited_tool_calls，补人造 ToolMessage
        ↓
ToolNode._run_one()
  └── wrap_tool_call()
        ├── _reviewer_edit()      从 State 取回编辑
        ├── _apply_edit()         替换 request
        ├── handler(request)      真正执行
        └── _annotate_edited_result()  在结果前注入说明
```

```text
per_call：
after_model()
  └── 直接 return None

ToolNode._run_one()  （多个调用并发调度）
  └── wrap_tool_call()
        └── _resolve_per_call()
              ├── 查 interrupt_on
              ├── when(request) 判定
              ├── _tool_approval_request()  组装审批值
              ├── _decision_schema()        运行时造响应类型
              ├── interrupt(value, response_schema=...)
              └── 三种返回：
                    ├── (request, None)        批准
                    ├── (edited_request, act)  编辑
                    └── ToolMessage            拒绝 / 代答
        ↓
  若返回 ToolMessage：直接 return，handler 不被调用
  否则：_annotate_edited_result(handler(to_run), to_run, executed)
```

两条栈在形状上差别很大，但有一个共同的收尾动作：`_annotate_edited_result()`。这个函数后面会单独讲，因为它解决的是一个很隐蔽的问题——**模型会记得自己提交过什么，而实际执行的可能是另一回事**。

### 1.1 一句话概括两种策略

| 维度 | `batched` | `per_call` |
|---|---|---|
| 处理的对象 | 这一轮的**消息账本** | 这一次的**执行请求** |
| 处理时机 | `after_model()`，ToolNode 之前 | `wrap_tool_call()`，工具执行之前 |
| 编辑参数的落点 | 旁路 State 字段 | 当场替换 `ToolCallRequest` |
| 拒绝结果的落点 | 人造 `ToolMessage` 塞回 messages | `wrap_tool_call` 的返回值 |
| 审批规则的表达 | `ReviewConfig.allowed_decisions` | 运行时 Pydantic 类型的 union |

这张表里最后一行值得多说一句。`batched` 把"这个工具允许哪些决定"写进 `ReviewConfig`，交给外部审批界面去读；`per_call` 把同一份信息编进一个运行时生成的 Pydantic 类型，交给 LangGraph 去校验。

**一个是声明式的提示，一个是强制性的校验。** 这个差别会引出后面整整两节的内容。

---

## 二、`batched` 的 `after_model()`：六段，而不是一段

常见的简化说法会把它写成：

```python
# 概念伪代码：过度简化的 after_model
def after_model(self, state, runtime):
    actions = [call for call in state["messages"][-1].tool_calls
               if self._requires_approval(call)]
    if not actions:
        return None
    decisions = interrupt({"action_requests": actions})
    return self._apply_decisions(decisions)
```

这段伪代码抓住了主干，但它隐藏了三件关键的事：`_apply_decisions()` 这个方法在源码里并不存在；真正的实现是内联循环；而且它**会原地修改 `AIMessage`**。

真实实现从 [第 758 行](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/human_in_the_loop.py#L758-L859) 开始，一共六段。我们逐段看。

### 2.1 第一段：四种提前返回

```python
# 真实源码节选：human_in_the_loop.py L773-L783
if self.interrupt_mode == "per_call":
    # Interrupts are raised per tool call, in `wrap_tool_call`.
    return None
messages = state["messages"]
if not messages:
    return None

last_ai_msg = next((msg for msg in reversed(messages) if isinstance(msg, AIMessage)), None)
if not last_ai_msg or not last_ai_msg.tool_calls:
    return None
```

第一行就是两种模式的分水岭。`per_call` 下这个 Hook 什么都不做——不是"没找到需要审批的调用"，而是**主动放弃这个 Hook 的全部职责**。

后面三个判断看起来是防御性编程，其实各有具体含义：

| 判断 | 真实场景 |
|---|---|
| `not messages` | Graph 刚初始化，还没跑过模型 |
| 找不到 `AIMessage` | 状态被外部改写过，或这一轮是纯文本回复 |
| `not last_ai_msg.tool_calls` | 模型只说了话，没提工具 |

注意这里用的是 `next((msg for msg in reversed(messages) ...), None)`，也就是**从后往前找最近的 `AIMessage`**，而不是直接取 `messages[-1]`。原因很实际：`messages[-1]` 可能是上一条 `ToolMessage`，也可能被别的 Middleware 追加过东西。用"最近的一条 AI 消息"更稳。

### 2.2 第二段：收集时顺手记下原始下标

```python
# 真实源码节选：L786-L798
action_requests: list[ActionRequest] = []
review_configs: list[ReviewConfig] = []
interrupt_indices: list[int] = []

for idx, tool_call in enumerate(last_ai_msg.tool_calls):
    if (config := self.interrupt_on.get(tool_call["name"])) is not None:
        if not self._should_interrupt(tool_call, config, state, runtime):
            continue
        action_request, review_config = self._create_action_and_config(
            tool_call, config, state, runtime
        )
        action_requests.append(action_request)
        review_configs.append(review_config)
        interrupt_indices.append(idx)
```

三个平行列表，长度必须一致。这是 `batched` 整个设计的地基。

`interrupt_indices` 是最容易被忽略的那个。它记录的不是"第几个待审调用"，而是"这个待审调用在原始 `tool_calls` 里的下标"。为什么需要它？

因为审批结果只覆盖待审的那些调用，而重建 `tool_calls` 时要按**原始顺序**逐个决定"保留还是丢弃"。没有原始下标，就无法把 `decisions[0]` 对回到正确的调用上。

这就是为什么第十章特别强调：**`decisions` 数组的顺序对应的是 `action_requests` 的顺序，不是工具名，也不是你界面上看到的顺序。**

### 2.3 第三段：没有待审调用时，为什么要主动清空 State

这一段很短，但它是整个文件里最容易被误读的地方：

```python
# 真实源码节选：L800-L806
# If no interrupts needed, return early, dropping any earlier turn's edits so
# they cannot be applied to this turn's tool calls.
if not action_requests:
    if state.get(_EDITED_TOOL_CALLS_KEY):
        return {_EDITED_TOOL_CALLS_KEY: {}}
    return None
```

按直觉，"这一轮不需要审批"应该直接 `return None` 就完了。为什么要多此一举，返回一个空字典？

因为 `hitl_edited_tool_calls` 是**跨轮次残留**的 State 字段。

设想这样一串执行：

```text
第 1 轮：模型调用 write_file(call_2)，审批者 edit，记录 call_2 → v2
         ToolNode 执行，wrap_tool_call 读到 call_2 的编辑
第 2 轮：模型又调用了一个工具，但这次没有任何审批
```

第 2 轮如果什么都不返回，`state["hitl_edited_tool_calls"]` 里那条 `call_2 → v2` 还在。它不会误伤第 2 轮（因为 tool_call_id 不同），但它会一直留在 State 里，随着 Checkpoint 越存越多。

更糟的情况是 tool_call_id 撞车。虽然 ID 理论上唯一，但如果你的模型适配层自己生成 ID、或者测试里手工构造调用，重复的 ID 就会让一个陈旧的编辑记录命中一次全新的调用。

所以源码选择**主动清空**：

```python
# 教学化简：清空语义
if 这一轮没有任何待审批调用:
    if State 里还有旧编辑:
        return {"hitl_edited_tool_calls": {}}   # 显式覆盖为空
    return None                                 # 本来就没有，不用动
```

注意 `{}` 和 `None` 的区别。`None` 表示"我这次不更新任何字段"；`{}` 表示"我要把这个字段显式设成空"。前者不会清除旧值，后者会。

这个细节和[第十三章](./13-state-reducer-deltachannel-checkpoint.md)讲的 reducer 语义是同一套东西：**State 更新不是赋值，是"提交一个补丁"**。补丁里没提到的字段，保持原样。

### 2.4 第四段：`interrupt()` 与数量校验

```python
# 真实源码节选：L808-L826
hitl_request = HITLRequest(
    action_requests=action_requests,
    review_configs=review_configs,
)

decisions = interrupt(hitl_request)["decisions"]

if (decisions_len := len(decisions)) != (interrupt_count := len(interrupt_indices)):
    msg = (
        f"Number of human decisions ({decisions_len}) does not match "
        f"number of hanging tool calls ({interrupt_count})."
    )
    raise ValueError(msg)
```

`interrupt(hitl_request)` 返回的是外部传进来的 resume 值。源码直接对它取 `["decisions"]`——如果审批者传了别的形状，这里会抛 `KeyError`，而不是给一句友好的错误提示。这是一个刻意的选择：**resume 值的形状是契约，不是输入校验的对象。**

紧接着的数量校验才是真正的校验。它为什么必须存在？

因为 `decisions` 是按位置匹配的。少一个，后面的决定就会整体错位——`decisions[0]` 本来是给 `write_file` 的，可能被当成给 `delete` 的。这种错位不会报错，只会**安静地批准一个本该被拒绝的删除**。

所以源码选择在这里 fail loud。

### 2.5 第五段：重建 `tool_calls`

```python
# 真实源码节选：L828-L852
revised_tool_calls: list[ToolCall] = []
artificial_tool_messages: list[ToolMessage] = []
edited_tool_calls: dict[str, Action] = {}
decision_idx = 0

for idx, tool_call in enumerate(last_ai_msg.tool_calls):
    if idx in interrupt_indices:
        config = self.interrupt_on[tool_call["name"]]
        decision = decisions[decision_idx]
        decision_idx += 1

        revised_tool_call, tool_message = self._process_decision(
            decision, tool_call, config
        )
        if revised_tool_call is not None:
            revised_tool_calls.append(revised_tool_call)
            if decision["type"] == "edit" and (edited_id := revised_tool_call.get("id")):
                edited_tool_calls[edited_id] = decision["edited_action"]
        if tool_message:
            artificial_tool_messages.append(tool_message)
    else:
        revised_tool_calls.append(tool_call)
```

这段代码的结构值得单独画出来：

```text
遍历原始 tool_calls
  │
  ├── idx 在 interrupt_indices 里（受控调用）
  │     ├── 取下一个 decision（decision_idx 自增）
  │     ├── _process_decision()
  │     │     ├── approve → 返回 (原调用, None)      → 保留
  │     │     ├── edit    → 返回 (原调用, None)      → 保留 + 记一条编辑
  │     │     ├── reject  → 返回 (原调用, ToolMessage) → 保留 + 记一条人造消息
  │     │     └── respond → 返回 (原调用, ToolMessage) → 保留 + 记一条人造消息
  │     └── 若返回的调用不是 None：加入 revised_tool_calls
  │
  └── idx 不在里面（未受控调用）
        └── 原样保留
```

这里有个反直觉的地方，也是整章最值得停下来看一眼的地方：**`reject` 和 `respond` 也会返回 `tool_call`**。

也就是说，`revised_tool_calls` 里其实**一条都没少**。被拒绝的调用仍然留在 `AIMessage.tool_calls` 里。

那它为什么不会被执行？

答案不在这个循环里，而在第三节要讲的 `_process_decision()` 和 LangChain 的路由边：`reject` 除了返回调用本身，还额外返回一条 `ToolMessage`，其 `tool_call_id` 正好等于那个调用。路由边看到"这个调用已经有结果了"，就不再把它派给 `ToolNode`。

**拒绝不是删除，是"补一条结果，让调度器认为它已经完成"。**

这个设计比"从列表里删掉"更稳，因为它不需要修改模型写下的历史——只追加一条消息。而修改历史恰恰是 `edit` 分支刻意避免的事（见 3.1 节）。

### 2.6 第六段：返回值的三部分

```python
# 真实源码节选：L854-L859
# Update the AI message to only include approved tool calls
last_ai_msg.tool_calls = revised_tool_calls

# `wrap_tool_call` reads this back to substitute and annotate the call. Always
# written, so an earlier turn's edits cannot survive into this one.
return {
    "messages": [last_ai_msg, *artificial_tool_messages],
    _EDITED_TOOL_CALLS_KEY: edited_tool_calls,
}
```

三件事：

1. **原地修改 `last_ai_msg.tool_calls`**。这不是创建一个新的 `AIMessage`，而是改 State 里那个对象。
2. **把 `last_ai_msg` 放回 `messages`**。配合上一条，reducer 会用这个被改过的对象覆盖历史里那条消息。
3. **`edited_tool_calls` 总是写入**，哪怕是空字典。注释说得很清楚：这样上一轮的编辑就不可能活到这一轮。

第 1 点和第 2 点合起来，就是 `batched` 最本质的动作：**它不是在执行工具，它是在改写这一轮的消息账本。**

为什么敢原地改？因为在单线程的图执行里，`state["messages"]` 里的对象没有被别人共享。但这确实是一个"能省则省"的写法——如果哪天图执行变成并发的，这行就会出问题。读源码时看到这种写法，最好在脑子里标一个记号。

---

## 三、`_process_decision()`：四个分支和一条兜底

现在来看真正解析决定的地方，[第 636 行](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/human_in_the_loop.py#L636-L660)：

```python
# 真实源码节选
def _process_decision(
    decision: Decision,
    tool_call: ToolCall,
    config: InterruptOnConfig,
) -> tuple[ToolCall | None, ToolMessage | None]:
    """Process a single decision and return the revised tool call and optional tool message."""
    allowed_decisions = config["allowed_decisions"]

    if decision["type"] == "approve" and "approve" in allowed_decisions:
        return tool_call, None
    if decision["type"] == "edit" and "edit" in allowed_decisions:
        # Keep the model's own call in the message; `wrap_tool_call` substitutes the
        # reviewer's at execution time.
        return tool_call, None
    if decision["type"] in allowed_decisions and (
        decision["type"] == "reject" or decision["type"] == "respond"
    ):
        return tool_call, _answer_message(tool_call, decision)
    msg = (
        f"Unexpected human decision: {decision}. "
        f"Decision type '{decision.get('type')}' "
        f"is not allowed for tool '{tool_call['name']}'. "
        f"Expected one of {allowed_decisions} based on the tool's configuration."
    )
    raise ValueError(msg)
```

四个分支，一张表就能说清：

| `decision["type"]` | 是否在 `allowed_decisions` 里 | 返回的第一个值 | 返回的第二个值 | 最终是否进 `ToolNode` |
|---|---|---|---|---|
| `approve` | 是 | `tool_call` | `None` | 是 |
| `edit` | 是 | `tool_call` | `None` | 是（执行时换成编辑版） |
| `reject` | 是 | `tool_call` | 拒绝 `ToolMessage` | 否 |
| `respond` | 是 | `tool_call` | 代答 `ToolMessage` | 否 |
| 任意 | 否 | —— 抛 `ValueError` | —— | —— |

注意第一列和第四列的关系：**四条合法分支返回的第一个值全都是 `tool_call`**。区别只在第二个值。

所以"被拒绝的调用会被移除"这个说法，在 `after_model` 里并不成立。真正的过程是：

```text
模型原始 AIMessage.tool_calls：
  [0] read_file   call_1
  [1] delete      call_2   ← 受控，被拒绝
  [2] write_file  call_3

after_model 之后：
  AIMessage.tool_calls = [read_file(call_1), delete(call_2), write_file(call_3)]
                         ↑ 一条都没少
  messages 追加：ToolMessage(tool_call_id="call_2", status="error",
                            content="User rejected ...")

路由边看到 trailing ToolMessage 里有 call_2：
  pending_tool_calls = [call_1, call_3]
  → 只给这两个发 Send
```

从 `ToolNode` 的视角看，`call_2` 从来没有发生过。但从模型的视角看，`call_2` 发生了，并且得到了一条错误结果。

**这就是"拒绝"在 LangChain 里的实现方式：不是让工具返回错误，也不是删掉调用，而是给这个调用补一条"已经有过结果"的证据，让调度器跳过它。**

### 3.1 为什么 `approve` 和 `edit` 的返回值一模一样

这是全文件最值得琢磨的一处。

两者都返回 `(tool_call, None)`，也就是说，`after_model` 完全不区分"批准"和"编辑"——至少在 `tool_calls` 这个账本上不区分。

真正的区分发生在两处：

1. `after_model` 里那个 `if decision["type"] == "edit"`，把 `edited_action` 记进 `edited_tool_calls`。
2. `wrap_tool_call` 里的 `_reviewer_edit()`，把它读出来。

源码注释直接说明了动机：

```text
Keep the model's own call in the message; `wrap_tool_call` substitutes the
reviewer's at execution time.
```

这句话值得展开成三条理由。

**第一，模型需要看到自己原本提了什么。**

`_EDIT_NOTICE` 的原文是：

```python
# 真实源码节选：L54-L58
_EDIT_NOTICE = (
    "Note: a human reviewer replaced this tool call before it ran. The call recorded in "
    "your message is the one you produced, not the one that executed. This was "
    "intentional and authorized. Do not re-issue your original call."
)
```

注意第二句："The call recorded in your message is the one you produced, not the one that executed."

如果 `after_model` 直接把 `tool_calls` 里的 `content` 从 `v1` 改成 `v2`，这句话就变成了谎言——消息里记录的已经是 `v2` 了。模型会以为自己本来就打算写 `v2`，从而学不到"人改了参数"这件事。

**第二，历史消息是审计记录，不该被悄悄篡改。**

一个 Agent 的运行记录，事后要能回答"模型当时想做什么"。如果审批流程把 `AIMessage` 改得面目全非，这条记录就失去了价值。

**第三，`batched` 和 `per_call` 因此能共用同一个 `_apply_edit()`。**

`per_call` 的 request 来自 ToolNode，那里的 `tool_call` 本来就是模型原始的调用。两种模式都在"执行前一刻"替换，所以替换逻辑可以复用。

### 3.2 兜底分支为什么必须抛异常

如果审批者返回了一个不在 `allowed_decisions` 里的决定——比如配置只允许 `["approve", "reject"]`，但 UI 发来了 `{"type": "edit", ...}`——源码不做一个"降级为拒绝"的宽容处理，而是直接 `ValueError`。

理由和 `__init__` 里拒绝空的 `allowed_decisions` 是同一条：

```python
# 真实源码节选：L579-L596（__init__ 中的校验）
else:
    msg = (
        f"Invalid `interrupt_on` config for tool '{tool_name}': "
        "`allowed_decisions` must be a non-empty list of decision types "
        "(e.g. ['approve', 'reject']). Got config with keys "
        f"{sorted(tool_config.keys())} and "
        f"allowed_decisions={tool_config.get('allowed_decisions')!r}."
    )
    raise ValueError(msg)
```

文档字符串里写明了动机：

> An interrupt config without decisions would otherwise be silently dropped, disabling the approval gate for that tool.

**一个配置错误如果被静默吞掉，结果就是"审批门禁失效"——而失效是看不见的。** 这比直接崩溃危险得多。所以这里的策略是：宁可让 Agent 起不来，也不要让它带着一扇假门运行。

---

### 3.3 真正决定谁去执行的是路由边

上面那个"跳过"发生在哪里？不在 Middleware 里，而在 `create_agent()` 装配出来的条件边。

[factory.py 第 1970—1988 行](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/factory.py#L1970-L1988)：

```python
# 真实源码节选
def _fetch_last_ai_and_tool_messages(
    messages: list[AnyMessage],
) -> tuple[AIMessage | None, list[ToolMessage]]:
    """Return the last AI message and any subsequent tool messages."""
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], AIMessage):
            last_ai_message = cast("AIMessage", messages[i])
            tool_messages = [m for m in messages[i + 1 :] if isinstance(m, ToolMessage)]
            return last_ai_message, tool_messages

    return None, []
```

它从后往前找到最近一条 `AIMessage`，然后收集它**之后**的所有 `ToolMessage`。`after_model` 追加的那条人造消息，正好落在这个范围里。

然后是真正的过滤，[第 2014—2040 行](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/factory.py#L2014-L2040)：

```python
# 真实源码节选
tool_message_ids = [m.tool_call_id for m in tool_messages]

if len(last_ai_message.tool_calls) == 0:
    return end_destination

pending_tool_calls = [
    c
    for c in last_ai_message.tool_calls
    if c["id"] not in tool_message_ids and c["name"] not in structured_output_tools
]

if pending_tool_calls:
    return [Send("tools", [tool_call]) for tool_call in pending_tool_calls]

if state.get("structured_response") is not None:
    return end_destination

# 6. AIMessage has tool calls, but there are no pending tool calls which suggests
# the injection of artificial tool messages. Jump to the model node
return model_destination
```

三件事在这里同时完成：

**第一，`c["id"] not in tool_message_ids` 是拒绝生效的机制。** 只要某个 `tool_call` 已经有一条同 ID 的 `ToolMessage`，它就不再是 "pending"。`after_model` 造的那条拒绝消息，就是靠这个把自己"注销"掉。

**第二，`Send("tools", [tool_call])` 是逐个派发的。** 不是把整个列表交给 `ToolNode`，而是每个 pending 调用一个 `Send`。这也解释了第九章附二为什么说 `ToolNode` 有时只看到一个调用——它收到的是单元素列表。

**第三，全部被拒时会回到模型，而不是结束。** `pending_tool_calls` 为空时，`return model_destination`。注释写得很直白：

```text
AIMessage has tool calls, but there are no pending tool calls which suggests
the injection of artificial tool messages. Jump to the model node
```

所以三个调用全被拒绝时，流程是：

```text
Model → after_model → 三条拒绝 ToolMessage → 路由边发现没有 pending
      → 回到 Model（带着三条错误结果）
```

模型于是看到"我提的三个动作都被拒绝了"，可以重新规划。这正是第十章说的"拒绝要变成能反馈给模型的执行结果"的落地方式。

### 3.4 这条机制对 `edit` 意味着什么

现在再看 `edit` 分支，会发现它和人造消息是同一套逻辑的两种用法：

| 决定 | 有没有人造 `ToolMessage` | 路由边怎么处理 |
|---|---|---|
| `approve` | 没有 | `call` 没有对应结果 → 派发 |
| `edit` | 没有 | `call` 没有对应结果 → 派发（执行时换参数） |
| `reject` | 有 | `call` 有对应结果 → 跳过 |
| `respond` | 有 | `call` 有对应结果 → 跳过 |

`edit` 之所以不需要人造消息，是因为它**希望**这个调用被派发——只是派发时用另一份参数。

`reject` 之所以需要，是因为它**不希望**被派发。

**人造 `ToolMessage` 在这套架构里的角色，不是"错误提示"，而是一个"已完成"的标记。** 它同时承担两件事：给模型一个可读的结果，给路由边一个可判定的信号。

这也解释了为什么 `respond` 的 `status` 是 `success` 而不是 `error`——它和 `reject` 在路由层面的作用完全一样（都是"已完成，别派发"），区别只在给模型看的语义。

## 四、`_answer_message()`：拒绝与代答的差别在 `status`

`reject` 和 `respond` 都要造一条 `ToolMessage`，但它们的语义完全不同。[第 266 行](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/human_in_the_loop.py#L266-L284)：

```python
# 真实源码节选
def _answer_message(tool_call: ToolCall, decision: RejectDecision | RespondDecision) -> ToolMessage:
    """The message the model gets in place of the tool's result."""
    if decision["type"] == "respond":
        # Skip tool execution; the human answers on behalf of the tool.
        content = decision["message"]
    elif reason := decision.get("message"):
        content = f"User rejected the tool call for `{tool_call['name']}` with reason: {reason}"
    else:
        content = (
            f"User rejected the tool call for `{tool_call['name']}` with id "
            f"{tool_call['id']}. The tool was not executed. Do not retry this tool "
            "call unless the user explicitly requests it."
        )
    return ToolMessage(
        content=content,
        name=tool_call["name"],
        tool_call_id=tool_call["id"],
        status="success" if decision["type"] == "respond" else "error",
    )
```

三种文案，两个状态，一张表：

| 决定 | 有没有 `message` | 生成的 `content` | `status` |
|---|---|---|---|
| `respond` | 必有 | 原样使用审批者的话 | `success` |
| `reject` | 有 | ``User rejected the tool call for `X` with reason: ...`` | `error` |
| `reject` | 无 | ``User rejected the tool call for `X` with id ... The tool was not executed. Do not retry ...`` | `error` |

### 4.1 拒绝文案里的 "Do not retry" 不是客套话

看没有 `message` 时的完整文案：

```text
User rejected the tool call for `delete` with id call_2. The tool was not executed.
Do not retry this tool call unless the user explicitly requests it.
```

这句话是写给模型看的，而且是有具体作用的。

在 Agent Loop 里，模型收到一条 `status="error"` 的 `ToolMessage` 之后，最常见的反应是"换个参数再试一次"。对一次网络超时来说这是对的；对一次人工拒绝来说，这是**绕过审批**。

如果模型把 `delete("/shop/tests")` 改成 `delete("/shop/tests/")` 再试一次，审批门禁会不会拦住它？会——`when` 谓词还会再跑一次，路径匹配大概率仍然命中。但如果是 `delete` 目录下的每个文件呢？那就是 N 次新的审批请求，审批者要拒绝 N 次。

所以文案必须明确告诉模型：**这不是失败，这是决定。**

注意 `_answer_message` 还顺手把 `tool_call["id"]` 写进了正文。这看起来多余（`tool_call_id` 字段里已经有了），但对模型来说是必要的——模型在上下文里看到的是一串文本，不是结构化的 `tool_call_id` 字段。把 ID 写进正文，模型才能在多个并行调用之间对上号。

### 4.2 `respond` 为什么是 `success`

`respond` 的语义是"人代替工具回答了"。对模型来说，这就是一次正常的工具返回。

如果把它标成 `error`，模型会以为工具失败了，可能会重试，或者进入错误处理分支。而实际上审批者已经给出了它想要的答案。

这也解释了第十章正文那句话：**`respond` 不应该拿来代替敏感操作的拒绝。** 因为它会被当成成功的工具响应，模型会基于这个"结果"继续往下走。用它来拒绝 `delete`，模型会以为"删除成功了"，然后继续执行后续步骤。

---

## 五、`hitl_edited_tool_calls`：为什么不直接改 `AIMessage`

现在回到那个旁路字段。它的定义只有几行，[第 389 行](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/human_in_the_loop.py#L389-L394)：

```python
# 真实源码节选
class _HumanInTheLoopState(AgentState[ResponseT]):
    """State schema for `HumanInTheLoopMiddleware`."""

    hitl_edited_tool_calls: NotRequired[Annotated[dict[str, Action], PrivateStateAttr]]
    """Track tool call edits from `after_model`, so they can be used by `wrap_tool_call`."""
```

三个修饰词，每个都有作用：

| 修饰 | 含义 |
|---|---|
| `NotRequired` | 这个字段可以不存在，老 Checkpoint 恢复时不会报错 |
| `Annotated[..., PrivateStateAttr]` | 标记为私有字段，不进入模型可见的 State 视图 |
| `dict[str, Action]` | 键是 `tool_call_id`，值是 `{name, args}` |

`PrivateStateAttr` 这一条尤其重要。第八章附二讲 Memory 时提过类似的机制：State 里有些字段是给框架内部用的，不应该被序列化进提示词。如果 `hitl_edited_tool_calls` 泄漏到模型上下文里，模型会看到一份"我原本想写 v1，但被人改成 v2"的额外账本——这会和 `_EDIT_NOTICE` 的说明重复，甚至冲突。

### 5.1 读取端只有五行

```python
# 真实源码节选：L875-L884
def _reviewer_edit(self, request: ToolCallRequest) -> Action | None:
    """The reviewer's replacement for this call, if an `edit` decision replaced it."""
    tool_call_id = request.tool_call.get("id")
    if not tool_call_id:
        return None
    edited = request.state.get(_EDITED_TOOL_CALLS_KEY) or {}
    if tool_call_id in edited:
        executed: Action = edited[tool_call_id]
        return executed
    return None
```

注意它读的是 `request.state`，不是 `runtime.state`，也不是闭包里的某个变量。这意味着**编辑记录是通过 State 传递的**，`wrap_tool_call` 和 `after_model` 之间没有任何直接引用。

这正是 `batched` 的架构特征：两个 Hook 之间只通过 State 通信。

```text
after_model                     ToolNode / wrap_tool_call
    │                                   │
    │  {"hitl_edited_tool_calls":       │
    │     {"call_2": {"name": ...,      │
    │                "args": ...}}}     │
    └──────────► State ────────────────►┘
                    │
                    └── 由 Checkpointer 持久化，跨暂停点保留
```

### 5.2 一个容易忽略的细节：`if not tool_call_id`

`_reviewer_edit` 用的是 `if not tool_call_id`，而 `_tool_approval_request`（`per_call` 那边）用的是 `if tool_call_id is None`。两处写法不一样，而且源码里专门加了注释解释：

```python
# 真实源码节选：L708-L710
# Only `None`: an empty ID still runs, so per-call keeps working where batched does.
if tool_call_id is None:
```

这句话的意思是：`batched` 在 ID 为空字符串时会**放弃应用编辑**（`not ""` 为真，直接返回 `None`），而 `per_call` 会**拒绝这次审批**（只有 `None` 才报错，空字符串放行）。

为什么两边的严格程度不一样？

因为 `batched` 的编辑是"可选增强"——ID 为空时，最坏结果是审批者的编辑没生效，但工具仍然用原始参数执行。这是一个安全降级。

而 `per_call` 的编辑是"审批结果本身"。如果 ID 为空，`ToolMessage` 就无法和调用匹配，整个审批流程就失去了锚点。所以这里只有"完全无法匹配"（`None`）才报错，空字符串仍然能走通（因为 `ToolMessage.tool_call_id` 可以接受空字符串，只要两边一致）。

这是一个典型的"在两个约束之间取最不坏的那个"的设计。

---

## 六、`_apply_edit()`：同名替换与改名重定向

编辑记录读出来之后，就要替换 request 了。[第 886 行](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/human_in_the_loop.py#L886-L911)：

```python
# 真实源码节选
def _apply_edit(self, request: ToolCallRequest, executed: Action) -> ToolCallRequest:
    """Point the request at the reviewer's call, resolving a redirected tool.

    Raises:
        ValueError: If the reviewer named a tool the agent does not have.
    """
    tool_call: ToolCall = {
        **request.tool_call,
        "name": executed["name"],
        "args": executed["args"],
    }
    if executed["name"] == request.tool_call["name"]:
        return request.override(tool_call=tool_call)

    # `tool_call["name"]` and `tool` must stay in agreement.
    available = request.runtime.tools
    tool = next((t for t in available if t.name == executed["name"]), None)
    if tool is None:
        names = ", ".join(sorted(t.name for t in available))
        msg = (
            f"Reviewer edited tool call {request.tool_call['id']!r} to "
            f"{executed['name']!r}, which is not an available tool. "
            f"Available tools: {names}."
        )
        raise ValueError(msg)
    return request.override(tool_call=tool_call, tool=tool)
```

两段，一条分界线：**工具名变没变。**

### 6.1 同名：只换 `tool_call`

`request.override(tool_call=tool_call)` 只替换了 `tool_call` 字段，`tool` 保持不变。

这是对的，因为工具对象没变，只是参数变了。`tool_call` 里那个 `{**request.tool_call, ...}` 也保留了 `id`、`type` 等字段——**只覆盖 `name` 和 `args`**。

为什么保留 `id`？因为 `ToolMessage.tool_call_id` 要能对上，`_annotate_edited_result` 也要靠它匹配。改 ID 会让整条链断掉。

### 6.2 改名：两个字段必须同时换

这里有一句注释值得抄下来：

```text
`tool_call["name"]` and `tool` must stay in agreement.
```

`ToolCallRequest` 有两个描述"要执行什么"的字段：

| 字段 | 类型 | 谁在用 |
|---|---|---|
| `tool_call["name"]` | `str` | 日志、`ToolMessage.name`、结果匹配 |
| `tool` | `BaseTool \| None` | 真正被 `handler` 调用的对象 |

如果只改 `tool_call["name"]` 而不改 `tool`，`handler` 会拿旧工具对象执行新名字的参数——参数校验大概率失败，但更糟的情况是恰好通过，于是**你以为执行了 `write_file`，实际执行的是 `read_file`**。

反过来，只改 `tool` 不改名字，`ToolMessage.name` 会写错，模型看到的工具名和实际执行的不一致。

所以改名时两个都要换，而且 `tool` 必须从 `request.runtime.tools` 里**重新查**，不能凭空造一个。

### 6.3 这解释了 `batched` 与 `per_call` 的能力差异

现在可以给第十章那个说法一个源码级解释：

> `batched` 允许在编辑时切换到其他已注册工具，而 `per_call` 的 Schema 会锁定原工具名称。

`batched` 能做到，是因为 `_apply_edit` 里那段 `next((t for t in available if t.name == executed["name"]), None)`——它主动去工具列表里找了新工具。

`per_call` 做不到，不是因为 `_apply_edit` 不行（它们共用这个函数），而是因为 **`per_call` 的审批 schema 在类型层面就不接受改名**。这个我们放到第九节讲 `Literal[name]` 时再展开。

### 6.4 报错信息为什么要列出可用工具名

```python
names = ", ".join(sorted(t.name for t in available))
```

如果审批者写错了一个工具名（比如把 `write_file` 写成 `write`），错误信息会告诉他有哪些工具可选。这是一个很小的体验设计，但它反映了一个态度：**这个错误是给人看的，不是给程序看的。**

对比一下 `after_model` 里那个 `decisions` 数量校验——那个错误是给开发者看的，因为它只会在集成方写错 resume 格式时出现。

同一个文件里，两种错误信息的读者不同。读源码时留意这种差别，能帮你判断"哪一层应该负责处理这个错误"。

---

## 七、`_annotate_edited_result()`：让模型知道"你看到的那次调用没执行"

这是两条路径共同的收尾动作，也是整个文件里最容易被跳过、却最影响实际效果的部分。

三个函数，一层套一层：

```text
_annotate_edited_result(result, request, executed)
  └── _prepend_notice(message, executed)
        └── _notice(executed, has_content=...)
```

### 7.1 `_notice()`：把"实际执行了什么"写清楚

```python
# 真实源码节选：L913-L919
def _notice(self, executed: Action, *, has_content: bool) -> str:
    """The notice text, stating the call that actually ran."""
    notice = (
        f"{self.edit_notice} Executed instead: {executed['name']} with arguments "
        f"{json.dumps(executed['args'], default=str)}."
    )
    return f"{notice}\n\nTool response:" if has_content else notice
```

拼出来是这样一段：

```text
Note: a human reviewer replaced this tool call before it ran. The call recorded in
your message is the one you produced, not the one that executed. This was intentional
and authorized. Do not re-issue your original call. Executed instead: write_file with
arguments {"file_path": "/shop/refund.py", "content": "v2"}.

Tool response:
<工具真正的返回内容>
```

三个设计点：

**第一，`json.dumps(..., default=str)`。** 参数里可能有 `Path`、`Decimal`、`datetime` 这类对象，直接 `json.dumps` 会抛 `TypeError`。`default=str` 保证任何东西都能被序列化成字符串。这在文件路径场景里很实用——`Path("/a/b")` 会变成 `"/a/b"`。

**第二，`has_content` 控制要不要加 "Tool response:" 这个标签。** 如果工具返回空内容（比如 `write_file` 只返回一个空字符串），加标签反而让模型困惑："响应在哪儿？"所以空内容时只给 notice，不加标签。

**第三，`edit_notice` 可配置。** 构造函数里 `edit_notice: str | None = _EDIT_NOTICE`，传 `None` 就完全不加任何说明。

这最后一条其实是个陷阱：关掉 notice 之后，模型会以为自己提交的 `v1` 已经写入文件。如果你的业务里"人改了参数"这件事必须让模型知道（比如它接下来要基于文件内容做判断），就不要关。

### 7.2 `_prepend_notice()`：两种内容形状，两条注入路径

```python
# 真实源码节选：L921-L944
def _prepend_notice(self, message: ToolMessage, executed: Action) -> ToolMessage:
    """Return `message` with the reviewer-edit notice prepended to its content."""
    if not self.edit_notice:
        return message
    edit_notice = self._notice(executed, has_content=bool(message.content))

    content: str | list[str | dict[Any, Any]]
    if isinstance(message.content, str):
        if edit_notice in message.content:
            return message
        separator = "\n" if message.content else ""
        content = f"{edit_notice}{separator}{message.content}"
    else:
        if any(edit_notice in str(block) for block in message.content):
            return message
        # Match the surrounding block shape; providers may reject mixed lists.
        notice: str | dict[Any, Any] = (
            edit_notice
            if message.content and all(isinstance(b, str) for b in message.content)
            else {"type": "text", "text": edit_notice}
        )
        content = [notice, *message.content]

    return message.model_copy(update={"content": content})
```

`ToolMessage.content` 有两种形状，这是 LangChain 里一个长期存在的双轨制：

| 形状 | 例子 | 什么时候出现 |
|---|---|---|
| `str` | `"file written"` | 绝大多数普通工具 |
| `list[str \| dict]` | `[{"type": "text", "text": "..."}]` | 多模态工具、返回图片/文件的工具 |

注入逻辑因此分成两条路。

**字符串路径**简单：`f"{edit_notice}{separator}{message.content}"`。`separator` 在内容非空时是 `"\n"`，内容为空时是 `""`——避免出现一个孤零零的换行。

**列表路径**要小心得多。注释写得很直接：

```text
Match the surrounding block shape; providers may reject mixed lists.
```

意思是：如果原内容全是字符串（`["a", "b"]`），就注入一个字符串；如果原内容里有 dict block，就注入一个 `{"type": "text", "text": ...}` block。

为什么？因为某些 provider 的 API 要求 `content` 数组里所有元素类型一致。往 `list[str]` 里塞一个 dict，请求会被拒绝。

这个细节和第九章附二讲的 `bind_tools` 分支、第八章附一讲的工具补票，属于同一类问题：**框架内部可以随便定义结构，但一旦要跨过 provider 的边界，就必须遵守对方的形状约束。**

### 7.3 幂等检查

两条路径都有一个提前返回：

```python
if edit_notice in message.content:
    return message
```

```python
if any(edit_notice in str(block) for block in message.content):
    return message
```

为什么要做幂等？

因为 `wrap_tool_call` 可能被多次调用。如果外层还有别的 Middleware 也调了 `handler`，或者重试逻辑重新执行了工具，notice 就可能被注入两遍。模型看到两段一模一样的说明，会开始怀疑人生。

注意列表路径用的是 `str(block)`，而不是直接比较。因为 `block` 可能是 dict，`edit_notice in dict` 会检查键而不是值。`str(block)` 把整个 dict 转成字符串再查，虽然粗糙但够用。

### 7.4 `Command` 分支：结果不在返回值里

这是最容易被漏掉的一段，[第 946 行](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/human_in_the_loop.py#L946-L977)：

```python
# 真实源码节选
def _annotate_edited_result(
    self,
    result: ToolMessage | Command[Any],
    request: ToolCallRequest,
    executed: Action | None,
) -> ToolMessage | Command[Any]:
    """Tell the model a reviewer replaced the call, and with what."""
    if not self.edit_notice or executed is None:
        return result

    if isinstance(result, ToolMessage):
        return self._prepend_notice(result, executed)

    # A `Command` carries the `ToolMessage` in its state update.
    if not isinstance(result, Command) or not isinstance(result.update, dict):
        return result
    messages = result.update.get("messages")
    if not isinstance(messages, list):
        return result
    tool_call_id = request.tool_call.get("id")
    return replace(
        result,
        update={
            **result.update,
            "messages": [
                self._prepend_notice(message, executed)
                if isinstance(message, ToolMessage) and message.tool_call_id == tool_call_id
                else message
                for message in messages
            ],
        },
    )
```

工具可以返回 `ToolMessage`，也可以返回 `Command`。后者用来同时更新 State 里的其他字段——比如 `write_todos` 会返回一个 `Command` 来更新 todo 列表。

当工具返回 `Command` 时，它的 `ToolMessage` 藏在 `result.update["messages"]` 里。所以要：

1. 确认 `result.update` 是 dict（`Command` 也允许其他形状）；
2. 确认 `update["messages"]` 是 list；
3. 遍历，按 `tool_call_id` 匹配，只改那一条；
4. 用 `dataclasses.replace` 造一个新的 `Command`，而不是原地改。

第 3 步的 `message.tool_call_id == tool_call_id` 很关键。一个 `Command` 可以携带多条消息（比如子 Agent 的完整对话），你只想改属于这次调用的那一条。

第 4 步用 `replace` 而不是 `result.update["messages"] = ...`，是因为 `Command` 是 `dataclass`，`replace` 会走 `__init__` 做字段校验。原地改会绕过校验，也可能污染其他引用。

---

## 八、`per_call`：`_resolve_per_call()` 的四步

现在换到另一条路。[第 727 行](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/human_in_the_loop.py#L727-L756)：

```python
# 真实源码节选
def _resolve_per_call(
    self, request: ToolCallRequest
) -> tuple[ToolCallRequest, Action | None] | ToolMessage:
    """Ask the reviewer about one tool call, if it's gated.

    Returns the request to run, with the reviewer's call if they edited it, or the
    message to return instead of running the tool.
    """
    tool_call = request.tool_call
    config = self.interrupt_on.get(tool_call["name"])
    if config is None:
        return request, None
    when = config.get("when")
    if when is not None and not when(request):
        return request, None

    value = self._tool_approval_request(request, config)
    response_schema = _decision_schema(
        config["allowed_decisions"], tool_call["name"], request.tool
    )
    # LangGraph parses the answer against `response_schema` before saving it, so it
    # comes back as one of this tool's allowed decisions.
    decision = interrupt(value, response_schema=response_schema)
    if decision["type"] == "approve":
        return request, None
    if decision["type"] == "edit":
        # The schema pinned the tool name, so this is always the same tool.
        executed = decision["edited_action"]
        return self._apply_edit(request, executed), executed
    return _answer_message(tool_call, decision)
```

返回类型是 `tuple[ToolCallRequest, Action | None] | ToolMessage`。这个 union 就是 `wrap_tool_call` 里那个 `isinstance` 分支的全部依据。

常见的简化说法会写"返回 request, None"，这是对的，但不完整——**它有三种返回形状**：

| 返回值 | 含义 | `handler` 是否执行 |
|---|---|---|
| `(request, None)` | 不需要审批 / 批准 / `when` 判定跳过 | 是 |
| `(edited_request, executed)` | 编辑 | 是（用编辑后的） |
| `ToolMessage` | 拒绝 / 代答 | 否 |

第三种是"裸"的 `ToolMessage`，不是一个 tuple。`wrap_tool_call` 靠 `isinstance(resolved, ToolMessage)` 把它挑出来。

### 8.1 第一步：查配置

```python
config = self.interrupt_on.get(tool_call["name"])
if config is None:
    return request, None
```

注意 `self.interrupt_on` 已经在 `__init__` 里被清洗过了：

```python
# 真实源码节选：L579-L596
for tool_name, tool_config in interrupt_on.items():
    if isinstance(tool_config, bool):
        if tool_config is True:
            resolved_configs[tool_name] = InterruptOnConfig(
                allowed_decisions=["approve", "edit", "reject", "respond"]
            )
    elif tool_config.get("allowed_decisions"):
        resolved_configs[tool_name] = tool_config
    else:
        ...
```

所以 `self.interrupt_on` 里不会有 `False` 的条目——`False` 在清洗时就被丢掉了，效果是"这个工具自动批准"。

`True` 则被展开成四件套。这个细节值得记住：**`interrupt_on={"delete": True}` 和 `interrupt_on={"delete": {"allowed_decisions": ["approve", "edit", "reject", "respond"]}}` 完全等价。**

### 8.2 第二步：`when` 收到的是真实 request

```python
when = config.get("when")
if when is not None and not when(request):
    return request, None
```

这里的 `request` 是 ToolNode 传进来的那个，带着 `tool` 和完整的 `ToolRuntime`。

这和 `batched` 里的 `_should_interrupt()` 完全不同。我们放到第十一节专门讲，因为这是两种模式最容易踩坑的差异。

### 8.3 第三步：`_tool_approval_request()`

```python
# 真实源码节选：L696-L725
def _tool_approval_request(
    self, request: ToolCallRequest, config: InterruptOnConfig
) -> ToolApprovalRequest:
    """Build the per-call interrupt value for one gated tool call.

    Raises:
        ValueError: If the tool call has no ID. It can't run without one, so the
            reviewer isn't asked to approve it.
    """
    tool_call = request.tool_call
    tool_call_id = tool_call["id"]
    # Only `None`: an empty ID still runs, so per-call keeps working where batched does.
    if tool_call_id is None:
        msg = (
            f"Tool call `{tool_call['name']}` has no ID, so its result can't be matched "
            "to it. Make sure the chat model returns tool call IDs."
        )
        raise ValueError(msg)
    # A description factory gets the graph `Runtime`, as in batched mode.
    runtime: Runtime[ContextT] = get_runtime()
    action_request, _ = self._create_action_and_config(
        tool_call, config, request.state, runtime
    )
    return ToolApprovalRequest(
        type="tool_approval",
        tool_call_id=tool_call_id,
        name=tool_call["name"],
        args=tool_call["args"],
        description=action_request.get("description", ""),
    )
```

这里有两个细节。

**第一个：`action_request, _ = ...` 丢掉了 `ReviewConfig`。**

`_create_action_and_config()` 是两种模式共用的，它返回 `(ActionRequest, ReviewConfig)`。`batched` 两个都用（一个进 `action_requests`，一个进 `review_configs`），`per_call` 只留前者。

为什么 `per_call` 不需要 `ReviewConfig`？因为 `allowed_decisions` 已经编进 `response_schema` 了。外部审批界面拿到的是一个 Pydantic 类型（的 JSON Schema），"哪些决定合法"直接写在类型里，不需要再单独给一份 `ReviewConfig` 让界面自己去遵守。

**第二个：`get_runtime()` 而不是传入 `runtime`。**

`after_model()` 的签名里有 `runtime: Runtime[ContextT]` 参数，直接用就行。但 `wrap_tool_call()` 没有这个参数——它的签名是 `(request, handler)`。所以 `_tool_approval_request()` 用 `get_runtime()` 从上下文里取。

这是 `per_call` 的一个结构性代价：它在工具调用链里运行，拿不到节点级的 `Runtime`，只能靠 contextvar。这也是为什么 `description` 工厂函数的签名虽然一样，但在两种模式下取到的 `runtime` 来源不同。

### 8.4 第四步：`interrupt(value, response_schema=...)`

```python
decision = interrupt(value, response_schema=response_schema)
```

`response_schema` 是 `per_call` 和 `batched` 最大的机制差异。注释说：

```text
LangGraph parses the answer against `response_schema` before saving it, so it
comes back as one of this tool's allowed decisions.
```

关键在 "before saving it"。校验发生在**答案被写入 checkpoint 之前**。如果校验失败：

- 答案不会被保存；
- 同一个 interrupt 可以重新回答；
- 抛出的是 `pydantic.ValidationError`。

这个"校验失败可以重答"的能力，`batched` 是没有的。`batched` 的答案校验只有那个数量检查——数量对了就往下走，内容对不对全靠外部审批界面自觉。

### 8.5 `wrap_tool_call` 里的分派

```python
# 真实源码节选：L1009-L1015
if self.interrupt_mode == "per_call":
    resolved = self._resolve_per_call(request)
    if isinstance(resolved, ToolMessage):
        return resolved
    to_run, executed = resolved
    return self._annotate_edited_result(handler(to_run), to_run, executed)
```

三行，把三种返回形状处理完：

```text
resolved 是 ToolMessage
  → 直接返回，handler 根本不调用
  → 注意：这里连 _annotate_edited_result 都跳过了，因为没有工具结果可注释

resolved 是 (request, None)
  → handler(request)，executed 为 None
  → _annotate_edited_result 看到 executed is None，原样返回

resolved 是 (edited_request, executed)
  → handler(edited_request)，然后注入 notice
```

第二条路径值得注意：`executed` 是 `None` 时，`_annotate_edited_result` 第一行就 `return result`。所以批准和未受控的调用不会有任何多余的 notice。

---

## 九、`_decision_schema()`：运行时造一个 Pydantic 类型

这是 `per_call` 最精巧的部分，也是理解"为什么 `per_call` 不能换工具"的关键。

先看整体，[第 235 行](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/human_in_the_loop.py#L235-L263)：

```python
# 真实源码节选
def _decision_schema(
    allowed: Sequence[DecisionType], name: str, tool: BaseTool | None = None
) -> type[Decision]:
    """The per-call `response_schema`: today's decision types, limited to `allowed`.

    The edit branch is built for this tool: its name is pinned, so an edit can't switch
    tools, and its args follow the tool's own schema. An answer is checked only against
    the branch its `type` names, so a bad one gets a single error about what's wrong.
    A one-decision tool gets that decision's plain object schema.

    Returns a type rather than a JSON schema because LangGraph checks answers only
    against Python types; it publishes the type's JSON schema to clients.
    """
    by_type: dict[DecisionType, object] = {
        "approve": ApproveDecision,
        "reject": RejectDecision,
        "respond": RespondDecision,
    }
    if "edit" in allowed:
        by_type["edit"] = _edit_decision(name, tool)
    # Drop duplicates, keeping order: a union of one type can't take a `Discriminator`.
    members = tuple(by_type[d] for d in dict.fromkeys(allowed))
    # Type checkers can't follow a type built at runtime. At runtime, `interrupt()` checks
    # every answer against it, so what it returns is one of these decisions.
    if len(members) == 1:
        return cast("type[Decision]", members[0])
    # Check only the branch the answer's `type` names, so a bad answer gets one precise
    # error rather than one per branch.
    return cast("type[Decision]", Annotated[Union[members], Discriminator("type")])
```

这段代码做了四件在普通业务代码里很少见的事。

### 9.1 为什么返回"类型"而不是"JSON Schema"

文档字符串最后一句说得很清楚：

> Returns a type rather than a JSON schema because LangGraph checks answers only against Python types; it publishes the type's JSON schema to clients.

LangGraph 的 `interrupt(response_schema=...)` 只接受 Python 类型，不接受 JSON Schema。它内部会用 Pydantic 的 `TypeAdapter` 去校验，然后把类型对应的 JSON Schema 发布给客户端。

所以这里必须造一个真正的 `type`。这解释了为什么整个函数用 `create_model` 而不是手写 dict。

### 9.2 `_edit_decision()`：两层 `create_model`

```python
# 真实源码节选：L220-L232
def _edit_decision(name: str, tool: BaseTool | None) -> object:
    """`EditDecision` for one tool: `name` pinned to it, `args` from `_edit_args`."""
    edited_action = create_model(
        "EditedAction",
        name=(Literal[name], ...),
        args=(_edit_args(tool), ...),
    )
    decision = create_model(
        "EditDecision",
        type=(Literal["edit"], ...),
        edited_action=(edited_action, ...),
    )
    return Annotated[decision, WrapValidator(_as_sent)]
```

外层是 `EditDecision`，内层是 `EditedAction`。拼出来的形状对应这个 JSON：

```json
{
  "type": "edit",
  "edited_action": {
    "name": "write_file",
    "args": {"file_path": "/shop/refund.py", "content": "v2"}
  }
}
```

和 `EditDecision` 那个 `TypedDict` 定义（第 122 行）完全一致，只是 `name` 和 `args` 的约束变成了运行时生成的具体类型。

### 9.3 `Literal[name]`：一行代码决定了能力边界

```python
name=(Literal[name], ...),
```

`Literal["write_file"]` 在 Pydantic 里的含义是：**这个字段只能是字符串 `"write_file"`，别的都不行。**

如果审批者提交：

```json
{"type": "edit", "edited_action": {"name": "read_file", "args": {...}}}
```

Pydantic 会报错，LangGraph 拒绝保存这个答案，审批者必须重新提交。

这就是第十章那个说法的源码依据：

> `batched` 允许在编辑时切换到其他已注册工具，而 `per_call` 的 Schema 会锁定原工具名称。

而且源码在 `_resolve_per_call` 里还专门加了一句注释确认这一点：

```python
# The schema pinned the tool name, so this is always the same tool.
executed = decision["edited_action"]
return self._apply_edit(request, executed), executed
```

注意它仍然调用了 `_apply_edit()`，而 `_apply_edit()` 里那段"改名重定向"的代码在 `per_call` 下永远不会执行——因为 `executed["name"]` 必然等于 `request.tool_call["name"]`，第一个分支就返回了。

这是一个"防御性冗余"：两个模式共用同一个替换函数，但 `per_call` 的 schema 保证了只会走简单路径。

### 9.4 单决策为什么要退化

```python
if len(members) == 1:
    return cast("type[Decision]", members[0])
```

注释解释了原因：

```text
Drop duplicates, keeping order: a union of one type can't take a `Discriminator`.
```

`Annotated[Union[X], Discriminator("type")]` 在 Pydantic 里是非法的。`Discriminator` 的作用是"根据某个字段的值，决定该用 union 里的哪个成员"。只有一个成员时，没有"选择"这回事，判别器无从下手，Pydantic 会报错。

所以必须特判。这也意味着一个只有一种决定的工具（比如 `allowed_decisions: ["approve"]`）拿到的 schema 是**裸的 `ApproveDecision`**，而不是一个 union。

这个行为有实际影响：外部审批界面拿到的 JSON Schema 会不一样。如果有单决策的工具有多决策的工具，界面代码要能处理两种形状。

### 9.5 `Discriminator("type")` 的收益：一条精确的错误

注释说：

```text
Check only the branch the answer's `type` names, so a bad answer gets one precise
error rather than one per branch.
```

不用 `Discriminator` 的话，Pydantic 会尝试把答案套进 union 里的每一个成员，然后报告"N 个错误"。用了之后，它先看 `type` 字段，只校验对应的那个分支。

对审批者来说，差别是：

```text
# 没有 Discriminator
ValidationError: 4 validation errors
  approve: ...
  edit: ...
  reject: ...
  respond: ...

# 有 Discriminator
ValidationError: 1 validation error
  edit.edited_action.args.content: Input should be a valid string
```

后者才是人能用的错误信息。

### 9.6 `_as_sent()`：校验之后，原样返回

```python
# 真实源码节选：L214-L217
def _as_sent(answer: object, check: ValidatorFunctionWrapHandler) -> object:
    """Check `answer`, then pass it on as sent, so the tool validates its args once."""
    check(answer)
    return answer
```

这五行是整个文件里最容易被误读的地方。

`WrapValidator` 的常规用法是：调用 `check(answer)` 拿到校验后的模型实例，然后返回它。但这里返回的是**原始的 `answer`**，一个 dict。

文档字符串给了理由：

```text
Check `answer`, then pass it on as sent, so the tool validates its args once.
```

拆开看：

**第一，如果返回校验后的模型实例，工具会收到一个 Pydantic 对象，而不是 dict。** 工具的参数注入逻辑期待 dict。虽然 LangChain 可能能处理，但那是一条没被测试过的路径。

**第二，校验会做类型转换。** 如果工具的 schema 里 `count: int`，审批者提交 `"5"`，Pydantic 会把它转成 `5`。如果这里返回校验后的实例，工具收到的是 `5`；如果返回原始 dict，工具收到的是 `"5"`，然后工具自己的校验会再转一次。

**第三，也是最重要的一点：工具自己的 `args_schema` 上的 validator 不在这里跑。** 这一点下一节展开。

所以 `_as_sent` 的语义是：**我检查一遍，确认形状对，然后原样放行，让真正的执行者去做它该做的校验。** 这是一种"我只当门卫，不当改造工"的设计。

---

## 十、`_edit_args()`：只校验模型看得见的那部分

```python
# 真实源码节选：L196-L211
def _edit_args(tool: BaseTool | None) -> object:
    """What an edit's `args` must look like: the tool's Pydantic schema, if it has one.

    That's the schema the model sees (`tool_call_schema`): it checks arg types and
    required args. Args it doesn't declare pass through, and the tool ignores them as
    it would from the model.
    Validator methods on the tool's `args_schema` aren't part of it; they run when the
    tool does.
    A JSON-schema tool's schema is shown but not enforced, and with no tool any object
    is accepted.
    """
    schema = tool.tool_call_schema if tool else None
    if not (isinstance(schema, type) and issubclass(schema, BaseModel)):
        shown = schema if isinstance(schema, dict) else {"type": "object"}
        return Annotated[dict[str, Any], WithJsonSchema(shown)]
    return schema
```

四种情况：

| 工具类型 | `tool_call_schema` 是什么 | 审批时的校验强度 |
|---|---|---|
| 普通 `@tool`（有 Pydantic args_schema） | `type[BaseModel]` | 类型 + 必填 |
| JSON Schema 型工具 | `dict` | 只展示，不校验 |
| 没有工具（`tool=None`） | `None` | 任意 object 都接受 |
| 工具没有 `tool_call_schema` | `None` | 任意 object 都接受 |

### 10.1 `tool_call_schema` 不是 `args_schema`

这是最关键的一点。

| 属性 | 内容 | 谁看 |
|---|---|---|
| `args_schema` | 完整的参数模型，含 validator | 工具执行时 |
| `tool_call_schema` | 模型看到的那个 schema | 模型、审批者 |

LangChain 的 `BaseTool` 会把 `args_schema` 转成 `tool_call_schema`，这个过程会**去掉一些东西**——最典型的就是 Pydantic 的 `@field_validator` 和 `@model_validator`。

源码文档字符串说得很直接：

```text
Validator methods on the tool's `args_schema` aren't part of it; they run when the
tool does.
```

为什么？因为模型生成参数时也不跑 validator——模型只看到 JSON Schema，validator 是 Python 层的逻辑，无法表达成 JSON Schema。如果审批时跑了 validator，就会出现"模型提交同样的参数能过，审批者提交同样的参数不能过"的不一致。

**审批者的校验强度，必须和模型的校验强度一致。** 这是一个很公平的设计原则。

### 10.2 "Args it doesn't declare pass through"

如果审批者提交了工具 schema 里没有的参数，会怎样？

```text
Args it doesn't declare pass through, and the tool ignores them as it would from
the model.
```

放行。因为模型提交多余参数时，也是放行的。

这在 Pydantic 里对应 `model_config = ConfigDict(extra="ignore")` 或默认行为。所以审批者可以往 `args` 里塞一些自定义字段（比如给审计日志用的 `_reviewer_note`），工具会忽略它们。

**但这意味着审批不能用来"注入额外信息给工具"。** 如果工具真的需要那个参数，它必须声明在 schema 里。

### 10.3 JSON Schema 型工具："shown but not enforced"

```text
A JSON-schema tool's schema is shown but not enforced
```

这类工具用 `args_schema={"type": "object", "properties": {...}}` 这种 dict 声明参数，而不是 Pydantic 模型。

`_edit_args` 对它的处理是：

```python
shown = schema if isinstance(schema, dict) else {"type": "object"}
return Annotated[dict[str, Any], WithJsonSchema(shown)]
```

`WithJsonSchema` 只影响**生成的 JSON Schema 长什么样**，不影响校验。实际校验用的是 `dict[str, Any]`——任何字典都通过。

所以审批界面会看到正确的参数结构提示，但提交不合规的参数不会被拦。

这是一个已知的能力缺口，源码选择明说而不是假装支持。

---

## 十一、`when` 在两种模式下拿到的不是同一个东西

这是全章最容易踩坑的地方，而且它在两种模式下的行为差异，会导致很难排查的 bug。

先看 `InterruptOnConfig` 的文档（[第 356—388 行](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/human_in_the_loop.py#L356-L388)）：

```python
# 真实源码节选
when: NotRequired[Callable[[ToolCallRequest], bool]]
"""Optional predicate controlling whether to interrupt for a given tool call.

Receives a `ToolCallRequest` and returns `True` to interrupt or `False` to
auto-approve. The predicate is called during `after_model` before the tool
call is added to the batched human-in-the-loop request.

The request is constructed with `tool=None` and a new `ToolRuntime`. The
`ToolRuntime` copies `context`, `store`, `stream_writer`, `execution_info`,
and `server_info` from the node-level `Runtime`, while `tool_call_id` is
populated from the current tool call. The `tools` argument is not supplied,
so it uses its default empty list.

In `per_call` mode the predicate runs in `wrap_tool_call` and receives the real
`ToolCallRequest`, with `tool` and the tool's `ToolRuntime` set.
...
"""
```

### 11.1 `batched`：一个手工拼出来的假 request

```python
# 真实源码节选：L662-L694
def _should_interrupt(
    self,
    tool_call: ToolCall,
    config: InterruptOnConfig,
    state: AgentState[Any],
    runtime: Runtime[ContextT],
) -> bool:
    """Return False if the `when` predicate rejects this tool call, True otherwise."""
    when = config.get("when")
    if when is None:
        return True
    runnable_config: RunnableConfig
    try:
        runnable_config = get_config()
    except RuntimeError:
        runnable_config = {}
    tool_runtime = ToolRuntime(
        state=state,
        context=runtime.context,
        config=runnable_config,
        stream_writer=runtime.stream_writer,
        tool_call_id=tool_call["id"],
        store=runtime.store,
        execution_info=runtime.execution_info,
        server_info=runtime.server_info,
    )
    req = ToolCallRequest(
        tool_call=tool_call,
        tool=None,
        state=state,
        runtime=tool_runtime,
    )
    return when(req)
```

这里没有 ToolNode，所以没有现成的 `ToolCallRequest`。源码手工造了一个，但有三个字段是"残缺"的：

| 字段 | `batched` 里的值 | `per_call` 里的值 |
|---|---|---|
| `tool` | `None` | 真实的 `BaseTool` 或 `None`（未注册时） |
| `runtime.tools` | `[]`（默认空列表） | 全部已注册工具 |
| `runtime.config` | `get_config()` 的兜底结果 | 该工具调用的 config |

这意味着：**在 `batched` 模式下，`when` 谓词不能读 `req.tool`。**

如果写了：

```python
# 错误示例：在 batched 模式下会 AttributeError
when=lambda req: req.tool.args_schema.__name__ == "DeleteArgs"
```

`req.tool` 是 `None`，直接崩。而如果这段代码在 `per_call` 下测试过，它可能是正常的——于是你会得到一个"切换模式就崩"的 bug。

### 11.2 `per_call`：真实 request，直接传

```python
when = config.get("when")
if when is not None and not when(request):
    return request, None
```

没有包装，没有重建，就是 ToolNode 给的那个 `request`。

所以 `per_call` 下的 `when` 可以做更细的判断——比如根据工具对象的 schema 决定是否审批。

### 11.3 DeepAgents 的 `when` 为什么两种模式都能跑

第十章讲过，DeepAgents 会把文件权限规则转成带 `when` 的 `interrupt_on`（见[第十章 4.1 节](./10-human-in-the-loop-interrupt-resume.md#41-真实源码将文件规则转换为审批条件)）。那个 predicate 长什么样？

```python
# 真实源码节选：_fs_interrupt.py L91-L108
def _make_exact_when_predicate(
    rules: list[FilesystemPermission],
    operation: FilesystemOperation,
    path_arg_name: str,
    backend: BackendProtocol | None = None,
) -> Callable[[ToolCallRequest], bool]:
    def when(req: ToolCallRequest) -> bool:
        raw_path = req.tool_call.get("args", {}).get(path_arg_name)
        if not isinstance(raw_path, str):
            return False
        try:
            normalized = validate_path(raw_path)
        except ValueError:
            return False
        decisions = {_check_fs_permission(rules, operation, path) for path in _candidate_paths(backend, normalized)}
        return "interrupt" in decisions and "deny" not in decisions

    return when
```

它只读 `req.tool_call.get("args", {})`，完全不碰 `request.tool` 和 `request.runtime.tools`。

这正是它能同时兼容两种模式的原因。`_fs_interrupt.py` 里还有另一个 `_make_bulk_when_predicate`，处理 `ls`、`glob`、`grep`、`delete` 这类"批量"工具——它同样只读 `args`，只是多检查了一个 `pattern` 参数，防止 `glob(pattern="/secrets/**", path="/workspace")` 用绝对 pattern 绕开对 `path` 的判定。

**如果你要写自己的 `when`，也请遵守这条纪律：只依赖 `tool_call`，不要依赖 `tool`。**

### 11.4 最危险的一条：resume 时谓词会再跑一次

文档里这句话值得单独抄出来：

```text
In both modes the predicate runs again when the run resumes, so it must return the
same answer for the same call. If it returns `False` on resume, the reviewer's
answer is skipped and the tool runs, even if they rejected it. In `batched` mode,
if it flips for only some of the reviewed calls, the decisions no longer line up
with the calls and resuming raises a `ValueError`.
```

拆成三条：

**第一条：谓词会被调用两次。** 一次在暂停前，一次在恢复后。

**第二条：恢复时返回 `False`，审批结果被跳过，工具直接执行——哪怕审批者拒绝了它。**

这是一个安全漏洞级别的行为。设想你的谓词是：

```python
# 危险示例：依赖可变外部状态
when=lambda req: business_hours()  # 只在工作时间审批
```

暂停发生在 17:59，审批者点了"拒绝"，恢复发生在 18:01。谓词返回 `False`，`_should_interrupt` 认为"不需要审批"，于是这个被拒绝的删除操作**直接执行了**。

正确写法是把判定依据固化在调用本身：

```python
# 安全示例：只依赖 tool_call 里的不可变数据
when=lambda req: req.tool_call["args"].get("file_path", "").startswith("/etc/")
```

**第三条：`batched` 下部分翻转会导致 `ValueError`。**

假设三个待审调用，谓词在恢复时对其中一个翻转了：

```text
暂停时：3 个待审 → interrupt 发出去 3 个 action_requests
恢复时：谓词对第 2 个返回 False → 只有 2 个待审
       → 但 decisions 还是 3 个
       → len(decisions) != len(interrupt_indices) → ValueError
```

这是好事——至少它炸了，而不是静默错位。但 `per_call` 下没有这个保护：每个调用独立判定，翻转的那个直接跳过审批执行，其他正常。

---

## 十二、两种模式的能力差异

现在可以把所有差异收拢成一张表。

### 12.1 机制层面对照

| 维度 | `batched` | `per_call` |
|---|---|---|
| 审批 Hook | `after_model()` | `wrap_tool_call()` |
| 暂停粒度 | 一次模型轮次 | 一次工具调用 |
| 暂停值类型 | `HITLRequest` | `ToolApprovalRequest` |
| 恢复格式 | `{"decisions": [...]}` | `{interrupt_id: decision}` |
| 决定与调用的匹配方式 | 数组位置 | interrupt ID |
| 决定的数量校验 | 显式 `len()` 比较 | 由 LangGraph 按 ID 匹配 |
| 决定的内容校验 | 无（只有数量校验） | `response_schema` |
| 校验失败能否重答 | 不适用（没有内容校验） | 能，答案不会被保存 |
| 编辑参数的落点 | 旁路 State 字段 | 当场替换 request |
| 编辑能否换工具 | 能 | 不能（`Literal[name]`） |
| 拒绝结果的产生者 | `after_model()` | `wrap_tool_call()` |
| 是否需要 `_HumanInTheLoopState` | 需要 | 不需要 |

### 12.2 关于"未受控工具"的一个精确化

常见的对照表会写：

| | `batched` | `per_call` |
|---|---|---|
| 未受控工具 | 跟随本轮图执行流程 | 可独立执行 |

这个说法容易被读成"`batched` 里未受控工具可以先跑"。**实际上不行。**

`after_model()` 是节点级 Hook。它在 `interrupt()` 处暂停时，**整张图都停住了**——`ToolNode` 一次都还没进。所以 `read_file` 虽然不需要审批，它也得等这一轮审批结束。

```text
batched：
  ModelNode 返回
    → after_model 暂停
      → ToolNode 还没开始
        → 所有调用（含未受控的 read_file）都在等
```

而 `per_call` 是真的可以并行。回忆[第九章附二](./09b-tools-to-llm-pipeline.md)讲过 `ToolNode._run_one()` 的调度：

```python
# 真实源码节选：LangGraph tool_node.py L821-L824
with get_executor_for_config(config) as executor:
    outputs = list(
        executor.map(self._run_one, tool_calls, input_types, tool_runtimes)
    )
```

异步版本用 `asyncio.gather`。多个 `_run_one` 并发执行，每个都可能在自己的 `wrap_tool_call` 里 `interrupt()`。

所以 `read_file` 确实可以在两个审批还挂着的时候执行完。

但它跑完之后，`ToolNode` 这个节点还没返回（因为另外两个还在等），所以 **State 还没更新**。

精确的表述是：

| | `batched` | `per_call` |
|---|---|---|
| 未受控工具何时执行 | 本轮所有审批结束后，与受控工具一起 | 自己的 `_run_one` 一开始就执行 |
| 未受控工具的结果何时进 State | `ToolNode` 节点返回时 | `ToolNode` 节点返回时（仍需等其他调用） |

**这个差别带来了 `per_call` 的"部分执行"风险**，也就是下一节要讲的内容。

### 12.3 什么时候该选哪个

| 场景 | 推荐 | 理由 |
|---|---|---|
| 一次审批要看到全貌 | `batched` | 一轮一个请求，审批者能对比多个动作 |
| 不同动作交给不同审批人 | `per_call` | 每个调用独立 ID，可分别路由 |
| 需要严格的参数校验 | `per_call` | `response_schema` 强制 |
| 审批界面很简单 | `batched` | 只需处理 `{"decisions": [...]}` |
| 需要"编辑时换工具" | `batched` | `Literal[name]` 不锁名 |
| 工具执行耗时长、想并行 | `per_call` | 未受控调用可先跑 |

注意最后一行不是"更好"，而是"更快但有代价"。如果 `read_file` 先跑完了，而 `write_file` 被拒绝，那么 `read_file` 的结果已经产生了——它可能已经被模型看到（如果 State 更新了），也可能只是白跑一趟。

---

## 十三、源码里几条容易被忽略的警告

### 13.1 `per_call` 计划成为默认

类文档字符串里有一句：

```text
`"per_call"` is planned to become the default in the next major release.
```

这意味着：**今天写的 `batched` 假设，未来可能需要迁移。**

具体要改什么？

| 现在依赖 `batched` 的地方 | 迁移时要改 |
|---|---|
| 恢复格式 `{"decisions": [...]}` | 改成 `{interrupt_id: decision}` |
| 依赖"编辑可以换工具" | 改成"先拒绝再让模型重新提" |
| 依赖"一次审批看到全部" | 审批界面要支持多次独立请求 |
| 自定义 `when` 读了 `req.tool` | `batched` 下本来就是 `None`，不用改 |

最后一行是个好消息：如果你的 `when` 一直只读 `tool_call`，迁移时它不用动。

### 13.2 一次提交多个答案的部分执行

```text
!!! warning "Answering several calls at once"

    If one of several answers sent together is invalid, the others'
    tools may already have run even if they still show as pending,
    and answering them again would run them twice. Check answers
    against `response_schema` before sending them together, or send
    one at a time.
```

这段警告描述了一个很具体的故障：

```text
1. ToolNode 并发调度 3 个调用：write_a、write_b、delete_c
2. 三个 interrupt 都挂起
3. 审批者一次提交三个答案：{id_a: approve, id_b: 非法格式, id_c: approve}
4. LangGraph 逐个校验：
   - id_a 通过 → 保存 → write_a 恢复执行 → 跑完了
   - id_b 校验失败 → 抛 ValidationError → 整个 invoke 失败
   - id_c 还没轮到
5. 审批者看到报错，以为三个都没生效，重新提交 id_b 和 id_c
6. id_b 通过 → write_b 执行
   id_c 通过 → delete_c 执行
7. 但 write_a 已经跑过一次了
```

结果是 `write_a` 执行了两次。

源码给的两种规避方式：

1. **提交前自己按 `response_schema` 校验一遍。**
2. **一次只提交一个。**

第二种最稳，但用户体验差。第一种需要客户端拿到 `response_schema` 并本地校验——LangGraph 会把它发布给客户端，所以技术上可行。

### 13.3 Middleware 顺序

```text
!!! warning "Middleware order in `per_call` mode"

    The review happens as the tool call starts, inside the tool-call
    middleware chain. List this middleware before tool retry or
    error-handling middleware so it wraps them, and don't enable it
    on subclasses that raise their own interrupts in `after_model`.
    On resume, each paused tool call runs the middleware listed
    before this one again up to the pause, so that middleware must
    not have side effects before calling `handler`.
```

拆成三条：

**第一条：HITL 要放在重试/错误处理 Middleware 之前（也就是外层）。**

为什么？因为 `per_call` 的审批失败靠抛 `pydantic.ValidationError` 往上传。如果错误处理 Middleware 在外层，它会把 `ValidationError` 当成"工具执行出错"，转成一条 `ToolMessage` 返回给模型。

于是审批者提交了非法答案，得到的不是"请重新填写"，而是"工具执行失败"。而且模型会看到这条错误消息，继续往下走。

`ToolNode._run_one()` 里就有这样的机制：

```python
# 真实源码节选：LangGraph tool_node.py L1054-L1067
try:
    return self._wrap_tool_call(tool_request, execute)
except Exception as e:
    if not self._handle_tool_errors:
        raise
    content = _handle_tool_error(e, flag=self._handle_tool_errors)
    return ToolMessage(
        content=content,
        name=tool_request.tool_call["name"],
        tool_call_id=tool_request.tool_call["id"],
        status="error",
    )
```

默认的 `_default_handle_tool_errors` 只处理 `ToolInvocationError`，所以 `ValidationError` 会正常往上抛。但如果有人显式设了 `handle_tool_errors=True`，所有异常都会被吞成 `ToolMessage`。

**第二条：不要在 `after_model` 里自己 `interrupt()` 的子类上启用 `per_call`。**

因为 `per_call` 下 `after_model()` 直接 `return None`，子类如果重写了它并抛中断，就会出现"两个地方都在暂停"的混乱。

**第三条：恢复时，HITL 前面的 Middleware 会重跑到暂停点。**

这解释了第十章 12.3 节说的那句话。`per_call` 的中断发生在工具调用链内部，恢复时要从链头重新进入，走到 `interrupt()` 那个位置。

所以 HITL 前面的 Middleware 必须满足：**在调用 `handler` 之前没有副作用。**

第八章附一讲过 [SkillsMiddleware 的 `wrap_tool_call`](./08a-skills-middleware.md) 会动态补票——它是纯内存操作，没有副作用，所以安全。

但如果你的 Middleware 在 `handler` 之前写了日志、发了通知、扣了配额，恢复时就会重复。

### 13.4 `interrupt_mode` 的非法值

```python
# 真实源码节选：L572-L577
if interrupt_mode not in (modes := get_args(InterruptMode)):
    allowed = " or ".join(repr(mode) for mode in modes)
    msg = f"`interrupt_mode` must be {allowed}, got {interrupt_mode!r}."
    raise ValueError(msg)
```

`InterruptMode = Literal["batched", "per_call"]`，`get_args()` 把它拆成 `("batched", "per_call")`。

所以 `interrupt_mode="per-call"`（连字符）会直接报错，而不是静默回退到 `batched`。这是好事。

---

## 十四、把两条链路重放一遍

最后，用两段完整伪代码把这一章串起来。它们不是可以直接运行的代码，但每一行都对应真实源码里的一个动作。

### 14.1 `batched` 的完整一轮

```python
# 概念伪代码：batched 模式下，一轮工具调用的完整生命周期

def after_model(state, runtime):
    # ---------- 第一段：判定 ----------
    if interrupt_mode == "per_call":
        return None
    last_ai = 最近一条 AIMessage
    if last_ai is None or not last_ai.tool_calls:
        return None

    # ---------- 第二段：收集 ----------
    actions, configs, indices = [], [], []
    for idx, call in enumerate(last_ai.tool_calls):
        cfg = interrupt_on.get(call["name"])
        if cfg is None:
            continue                      # 未受控，自动批准
        if not should_interrupt(call, cfg):   # 造一个 tool=None 的假 request
            continue
        actions.append(make_action_request(call, cfg))
        configs.append(make_review_config(call, cfg))
        indices.append(idx)

    # ---------- 第三段：无待审时清空 ----------
    if not actions:
        return {"hitl_edited_tool_calls": {}} if state.get("hitl_edited_tool_calls") else None

    # ---------- 第四段：暂停 + 数量校验 ----------
    decisions = interrupt({"action_requests": actions, "review_configs": configs})["decisions"]
    if len(decisions) != len(indices):
        raise ValueError("数量不匹配")

    # ---------- 第五段：重建 ----------
    revised, artificial, edits = [], [], {}
    d = 0
    for idx, call in enumerate(last_ai.tool_calls):
        if idx not in indices:
            revised.append(call)          # 未受控，原样保留
            continue
        decision = decisions[d]; d += 1
        new_call, msg = process_decision(decision, call, interrupt_on[call["name"]])
        if new_call is not None:          # 四种决定都会返回调用，一条都不少
            revised.append(new_call)
            if decision["type"] == "edit":
                edits[new_call["id"]] = decision["edited_action"]
        if msg is not None:               # reject / respond 额外补一条"已完成"证据
            artificial.append(msg)

    # ---------- 第六段：落账 ----------
    last_ai.tool_calls = revised           # 原地改
    return {"messages": [last_ai, *artificial], "hitl_edited_tool_calls": edits}


def route_after_model(state):
    # 概念伪代码：真正决定派发谁的，是这条条件边
    last_ai, tool_msgs = 最近一条 AIMessage, 它之后的 ToolMessage 列表
    done = [m.tool_call_id for m in tool_msgs]
    pending = [c for c in last_ai.tool_calls if c["id"] not in done]
    if pending:
        return [Send("tools", [c]) for c in pending]
    return "model"        # 全被拒绝时回到模型，而不是结束


def wrap_tool_call(request, handler):
    executed = request.state.get("hitl_edited_tool_calls", {}).get(request.tool_call["id"])
    if executed is not None:
        request = apply_edit(request, executed)   # 可能换工具
    result = handler(request)
    return annotate_edited_result(result, request, executed)   # executed 为 None 时原样返回
```

### 14.2 `per_call` 的完整一轮

```python
# 概念伪代码：per_call 模式下，单个工具调用的完整生命周期

def after_model(state, runtime):
    return None        # 什么都不做


def wrap_tool_call(request, handler):
    # ---------- 第一步：查配置 ----------
    cfg = interrupt_on.get(request.tool_call["name"])
    if cfg is None:
        return handler(request)           # 未受控，直接执行

    # ---------- 第二步：谓词 ----------
    if cfg.get("when") and not cfg["when"](request):   # 真实 request，tool 有值
        return handler(request)

    # ---------- 第三步：组装审批值 ----------
    if request.tool_call["id"] is None:
        raise ValueError("没有 ID，无法匹配结果")
    value = {
        "type": "tool_approval",
        "tool_call_id": request.tool_call["id"],
        "name": request.tool_call["name"],
        "args": request.tool_call["args"],
        "description": make_description(request),
    }

    # ---------- 第四步：暂停，带类型 ----------
    schema = build_decision_schema(
        allowed=cfg["allowed_decisions"],
        name=request.tool_call["name"],    # Literal[name] 锁死
        tool=request.tool,                 # args 跟随工具 schema
    )
    decision = interrupt(value, response_schema=schema)

    # ---------- 第五步：分派 ----------
    if decision["type"] == "approve":
        return handler(request)
    if decision["type"] == "edit":
        edited = decision["edited_action"]
        new_request = apply_edit(request, edited)
        return annotate_edited_result(handler(new_request), new_request, edited)
    return answer_message(request.tool_call, decision)   # 裸 ToolMessage，handler 不执行
```

### 14.3 同一件事，两张时序表

假设审批者选择 `edit`，把 `content` 从 `v1` 改成 `v2`。

`batched`：

| 时刻 | `AIMessage.tool_calls[1].args["content"]` | `hitl_edited_tool_calls` | 实际写入文件 |
|---|---|---|---|
| 模型返回 | `"v1"` | 不存在 | —— |
| `interrupt` 前 | `"v1"` | 不存在 | —— |
| `interrupt` 返回 | `"v1"` | 不存在 | —— |
| `after_model` 返回 | `"v1"` | `{"call_2": {"args": {"content": "v2"}}}` | —— |
| `wrap_tool_call` 进入 | `"v1"` | 同上 | —— |
| `_apply_edit` 之后 | `"v1"`（request 是新对象） | 同上 | —— |
| `handler` 执行 | `"v1"` | 同上 | `"v2"` |
| 结果返回模型 | `"v1"` + notice 说明 | 同上 | `"v2"` |

`per_call`：

| 时刻 | `AIMessage.tool_calls[1].args["content"]` | `hitl_edited_tool_calls` | 实际写入文件 |
|---|---|---|---|
| 模型返回 | `"v1"` | 不存在 | —— |
| `wrap_tool_call` 进入 | `"v1"` | 不存在 | —— |
| `interrupt` 返回 | `"v1"` | 不存在 | —— |
| `_apply_edit` 之后 | `"v1"`（request 是新对象） | 不存在 | —— |
| `handler` 执行 | `"v1"` | 不存在 | `"v2"` |
| 结果返回模型 | `"v1"` + notice 说明 | 不存在 | `"v2"` |

两张表的差别只有一列：`hitl_edited_tool_calls` 有没有被用到。

**但它们背后是两种完全不同的架构：一个用 State 连接两个 Graph 阶段，一个在单个调用链里就地完成。**

---

## 十五、小结

这一章拆了七个函数，可以收成五句话。

**一、`batched` 不是在执行工具，它是在改写消息账本。**

`after_model()` 做的是：把编辑记录到旁路 State，给被拒绝和被代答的调用补一条人造 `ToolMessage`，然后原地修改 `AIMessage`。

**注意它并没有删掉任何调用。** 被拒绝的调用仍然留在 `tool_calls` 里，是路由边根据"这个 ID 已经有 `ToolMessage` 了"把它过滤掉的。拒绝不是删除，是"补一条已完成证据"。

**二、`edit` 在 `batched` 里不改 `tool_calls`，是刻意的。**

因为 `_EDIT_NOTICE` 需要指向"模型原本提交的那个调用"。改了之后，notice 就变成了谎言，模型也学不到"人改了参数"。

**三、`_apply_edit` 的同名/改名分支，决定了两种模式的能力差异。**

`batched` 能换工具，是因为 `_apply_edit` 会去 `runtime.tools` 里找新工具；`per_call` 不能换，是因为 `Literal[name]` 在类型层就锁死了。两者共用同一个替换函数，但只有一条路径会走到改名分支。

**四、`per_call` 的 `response_schema` 是运行时造出来的 Pydantic 类型。**

`create_model` 造两层模型，`Literal[name]` 锁工具名，`_edit_args` 只校验模型看得见的那部分，`_as_sent` 校验完原样放行。这套组合的目的是：**审批者的校验强度必须和模型的校验强度一致。**

**五、`when` 谓词在两种模式下拿到的不是同一个对象。**

`batched` 里 `tool=None`、`runtime.tools=[]`；`per_call` 里都是真的。而且 resume 时谓词会再跑一次——返回 `False` 会跳过审批结果直接执行，哪怕审批者拒绝了。**谓词只能依赖 `tool_call`，不能依赖外部可变状态。**

### 15.1 和第十章正文的关系

| 第十章正文讲了 | 本章补充 |
|---|---|
| 两种模式的位置差异 | 两种模式的完整调用栈 |
| `interrupt()` 会暂停 | 暂停前后各自发生了什么 |
| 四种决定的行为 | 四个分支的源码与返回类型 |
| 拒绝会生成 `ToolMessage` | 文案的三个变体和 `status` 的含义 |
| 编辑记录存在 State 里 | 为什么不直接改 `AIMessage` |
| `per_call` 有 `response_schema` | schema 是怎么被造出来的 |
| `per_call` 锁定工具名 | `Literal[name]` 的实现 |

如果你只记一件事，记这个：

> **`batched` 把审批拆成"决策阶段"和"执行阶段"，用 State 连接；`per_call` 把审批压进单个工具的执行链，用一个 union 返回值决定要不要调 `handler`。**

前者适合"一次看全、统一决策"，后者适合"逐个处理、带类型校验"。而它们共同的那条尾巴——`_annotate_edited_result`——解决的是同一个问题：**让模型知道自己看到的那次调用，并不是真正执行的那一次。**

---

## 附：本章源码导航

| 想验证的问题 | 固定源码入口 |
|---|---|
| `hitl_edited_tool_calls` 这个键叫什么 | [`human_in_the_loop.py#L51-L52`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/human_in_the_loop.py#L51-L52) |
| `_EDIT_NOTICE` 的默认文案 | [`human_in_the_loop.py#L54-L58`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/human_in_the_loop.py#L54-L58) |
| 四种决定的 TypedDict 定义 | [`human_in_the_loop.py#L115-L165`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/human_in_the_loop.py#L115-L165) |
| `ToolApprovalRequest` 的五个字段 | [`human_in_the_loop.py#L172-L193`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/human_in_the_loop.py#L172-L193) |
| `_edit_args` 只校验模型看得见的 schema | [`human_in_the_loop.py#L196-L211`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/human_in_the_loop.py#L196-L211) |
| `_as_sent` 校验后原样放行 | [`human_in_the_loop.py#L214-L217`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/human_in_the_loop.py#L214-L217) |
| `_edit_decision` 用 `Literal[name]` 锁工具名 | [`human_in_the_loop.py#L220-L232`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/human_in_the_loop.py#L220-L232) |
| `_decision_schema` 的单决策退化与 `Discriminator` | [`human_in_the_loop.py#L235-L263`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/human_in_the_loop.py#L235-L263) |
| 拒绝与代答的文案和 `status` | [`human_in_the_loop.py#L266-L284`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/human_in_the_loop.py#L266-L284) |
| `when` 在两种模式下的行为差异 | [`human_in_the_loop.py#L356-L388`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/human_in_the_loop.py#L356-L388) |
| 私有 State 字段的定义 | [`human_in_the_loop.py#L389-L394`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/human_in_the_loop.py#L389-L394) |
| `interrupt_mode` 的校验与 `True` 的展开 | [`human_in_the_loop.py#L489-L598`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/human_in_the_loop.py#L489-L598) |
| `_process_decision` 的四个分支 | [`human_in_the_loop.py#L636-L660`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/human_in_the_loop.py#L636-L660) |
| `_should_interrupt` 手工造 request | [`human_in_the_loop.py#L662-L694`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/human_in_the_loop.py#L662-L694) |
| `_tool_approval_request` 与无 ID 报错 | [`human_in_the_loop.py#L696-L725`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/human_in_the_loop.py#L696-L725) |
| `_resolve_per_call` 的三种返回 | [`human_in_the_loop.py#L727-L756`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/human_in_the_loop.py#L727-L756) |
| `after_model` 的六段实现 | [`human_in_the_loop.py#L758-L859`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/human_in_the_loop.py#L758-L859) |
| `_reviewer_edit` 从 State 读回编辑 | [`human_in_the_loop.py#L875-L884`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/human_in_the_loop.py#L875-L884) |
| `_apply_edit` 的同名与改名分支 | [`human_in_the_loop.py#L886-L911`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/human_in_the_loop.py#L886-L911) |
| notice 的拼装与 `Tool response:` 标签 | [`human_in_the_loop.py#L913-L919`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/human_in_the_loop.py#L913-L919) |
| 字符串与 block 列表两种注入路径 | [`human_in_the_loop.py#L921-L944`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/human_in_the_loop.py#L921-L944) |
| `Command` 结果里按 `tool_call_id` 匹配 | [`human_in_the_loop.py#L946-L977`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/human_in_the_loop.py#L946-L977) |
| `wrap_tool_call` 两种模式的分派 | [`human_in_the_loop.py#L979-L1023`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/human_in_the_loop.py#L979-L1023) |
| ToolNode 并发调度多个 `_run_one` | [`tool_node.py#L793-L824`](https://github.com/langchain-ai/langgraph/blob/1.2.12/libs/prebuilt/langgraph/prebuilt/tool_node.py#L793-L824) |
| `_run_one` 用 try/except 包住 wrapper 链 | [`tool_node.py#L1054-L1067`](https://github.com/langchain-ai/langgraph/blob/1.2.12/libs/prebuilt/langgraph/prebuilt/tool_node.py#L1054-L1067) |
| DeepAgents 把权限规则转成带 `when` 的配置 | [`_fs_interrupt.py#L174-L203`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/_fs_interrupt.py#L174-L203) |
| 精确路径谓词只读 `tool_call["args"]` | [`_fs_interrupt.py#L91-L108`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/_fs_interrupt.py#L91-L108) |
| 批量工具谓词额外检查 `pattern` 参数 | [`_fs_interrupt.py#L111-L155`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/_fs_interrupt.py#L111-L155) |

**本章核心产出：** 读者不仅知道两种模式"在哪儿暂停"，还能沿源码解释一个 `Decision` 从 `interrupt()` 返回到工具真正执行之间经过哪些函数、编辑为什么不能直接改 `AIMessage`、`Literal[name]` 如何决定能力边界、`when` 为什么必须只依赖 `tool_call`，以及 `per_call` 的部分执行风险从何而来。
