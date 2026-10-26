---
title: 第八章：模型每一轮究竟看见什么？——Context、Skills 与 Memory
description: 从优惠券部分退款 Bug 的一次 Model Request 出发，沿 DeepAgents 0.7.32 固定源码，逐步理解 State 与实际输入分离、Summarization、Skills 渐进式披露、AGENTS.md Memory，以及 Middleware 怎样修改请求
tags: [DeepAgents, LangChain, LangGraph, Context, Skills, Memory, Summarization, Middleware, 源码精读]
sidebar_position: 8
---

# 第八章：模型每一轮究竟看见什么？——Context、Skills 与 Memory

> **本章核心问题：** Coding Agent 已经能通过 Agent Loop 读文件、改代码、执行工具。但到了第十五轮，模型下一次请求究竟收到哪些内容？为什么它能记住项目约定，又不必把过去所有测试日志原封不动地重发一遍？

## 一、接着第七章：Harness 装配完成，模型却还不知道项目规则

第七章结束时，我们已经看到：

```text
create_deep_agent()
    |
    | 组装 Model、Backend、Middleware 等
    v
create_agent()
    |
    | 编译 Graph
    v
LangGraph Runtime
    |
    v
Model → Tools → Model → ...
```

但是，能循环并不等于能正确解决业务问题。

继续沿用全书的任务：

> 检查 `shop/refund.py`，修复优惠券**部分退款**的金额精度 Bug，运行相关测试，并根据反馈继续修复。

假设模型第一次看到了用户任务，随后依次调用了工具：

```text
第 1 轮：read_file("shop/refund.py")
第 2 轮：read_file("shop/coupon_service.py")
第 3 轮：read_file("shop/tests/test_refund.py")
第 4 轮：edit_file(...)
第 5 轮：execute("pytest ...")
第 6 轮：read_file(...) 检查失败位置
...
第 15 轮：准备再次请求模型
```

到第十五轮时，我们希望模型同时掌握三类信息：

1. **正在发生什么**：刚刚修改了哪些文件、测试为什么失败、接下来要做什么。
2. **项目一直要求什么**：金额必须使用 `Decimal`，不能用 `float` 处理退款分摊。
3. **解决此类问题的专门方法**：先补边界测试，再检查四舍五入和累计金额守恒。

最直接的办法，是把这三类信息都拼成一个越来越长的 Prompt。

```python
# 概念伪代码：不是 DeepAgents 的实现
prompt = (
    base_instruction
    + project_rules
    + special_workflow
    + all_messages_since_start
)
model.invoke(prompt)
```

短任务里，这么做似乎能工作。但它立即遇到两种不同的限制。

第一，`pytest` 日志和工具调用参数越来越多，**历史本身太长**。

第二，项目约定与专项流程可能散落在文件里，**模型未必知道应该加载什么**。

因此，我们不是缺少一个更大的字符串，而是需要一种能够在每次模型调用前**组织工作上下文**的机制。

本章就沿这一条因果链推进：

```text
长历史难以全部发送
        ↓
区分 State 与本轮 Model Request
        ↓
摘要旧消息，保留近况和可恢复的原文
        ↓
只公布可用的专项指令，按需读取全文（Skills）
        ↓
将持续适用的项目约定稳定加入请求（Memory）
        ↓
在同一次请求中组合这些改动
```

注意：直到最后一节，我们才会画出完整的上下文处理图。先理解一条正常路径，不在起点堆满名词。

### 1.1 阅读版本及源码边界

本章固定：

```text
DeepAgents Python: 0.7.32
DeepAgents Commit: e85c6b9
LangChain:          承接前文研究的 1.4.7 版本
```

本章主要核查以下固定源码：

- [`deepagents/middleware/summarization.py`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/summarization.py)
- [`deepagents/middleware/skills.py`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/skills.py)
- [`deepagents/middleware/memory.py`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/memory.py)
- [`deepagents/graph.py`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py)

本章所有带“**真实源码节选**”的代码均可在所附固定链接中定位；允许省略不影响所讨论机制的语句，但不改变分支的核心语义。带“**教学化简**”和“**概念伪代码**”的片段用于推导，不冒充仓库原文。未执行的框架示例标为“预期”；不把它们说成测试通过。

---

## 二、最先暴露的矛盾：State 中有历史，不代表模型要看完整历史

### 2.1 一开始，把 State 直接当模型输入很自然

最小 Agent 可以这样理解：

```python
# 概念伪代码
state = {"messages": [user_message]}

while True:
    response = model.invoke(state["messages"])
    state["messages"].append(response)

    if not response.tool_calls:
        break

    for call in response.tool_calls:
        result = execute_tool(call)
        state["messages"].append(result)
```

在三五轮交互中，这个设计很容易理解：每轮都给模型完整历史，它才能知道之前发生了什么。

但如果某次工具执行产生了几千行测试输出，或者 `edit_file` 的调用参数携带整段补丁，那么下一轮把整个 `messages` 发回模型，就会反复发送大量已经处理过的数据。

想象当前 State：

```text
State["messages"]
  [0] HumanMessage: 修复部分退款 Bug
  [1] AIMessage: 读取 refund.py
  [2] ToolMessage: 文件全文
  [3] AIMessage: 读取 coupon_service.py
  [4] ToolMessage: 文件全文
  [5] AIMessage: 修改 refund.py（长 patch）
  [6] ToolMessage: 修改成功
  [7] AIMessage: 运行 pytest
  [8] ToolMessage: 大量失败日志
  [9] AIMessage: 分析失败原因
  ...
```

下一次模型真正需要的，很可能只是：

```text
先前发现：部分退款的折扣分摊在舍入后不守恒。
已修改：refund.py 的 Decimal 计算路径。
最新测试：test_partial_refund_rounding 仍失败。
失败差额：0.01。
下一步：检查最后一笔退款的余数归集。
```

两者并不矛盾：前者是**完整工作记录**，后者是**当前决策所需的有效上下文**。

因此，我们需要先区分两个对象：

- **Agent State**：Graph 在执行过程中管理的数据，包括消息及 Middleware 添加的状态字段。
- **Model Request**：当前这一轮实际交给模型调用处理的请求，其中包含 `messages`、`system_message`、工具定义等信息。

它们可以相关，但不要求逐字相同。

### 2.2 为什么上一章的 Wrapper 此时恰好派上用场？

第六章研究过：

```python
wrap_model_call(request, handler)
```

它包裹模型调用，可以在 `handler(request)` 之前生成一个修改后的请求。

如果问题只是“这一轮模型应当看更短的历史”，那么没有必要先修改 Graph 的完整持久化记录；只要在模型调用边界构造不同的 `request.messages` 即可。

最小形式是：

```python
# 教学化简：展示 ModelRequest 修改思想

def wrap_model_call(request, handler):
    effective_messages = select_messages(request.state["messages"])
    modified_request = request.override(messages=effective_messages)
    return handler(modified_request)
```

这里的 `select_messages` 只是我们假设的教学函数，不是 DeepAgents 的真实 API。

真正重要的是：

```python
request.override(messages=...)
```

它改变**此次请求传给下游处理器的消息**，而不是要求直接覆盖 State 里的全部历史。

这就给长上下文管理留下了实现空间。

---

## 三、第一层机制：先减少旧消息的重复负担，再考虑摘要

### 3.1 为什么不直接删除所有旧消息？

最简单的压缩方法：只保留最近几轮。

```python
# 概念伪代码
short_history = state["messages"][-6:]
```

但退款任务会立即暴露问题。用户起初说的是“**优惠券部分退款精度**”，最新几条消息可能只有：

```text
pytest 失败
第 43 行 AssertionError
重新打开 refund.py
```

一旦开头的业务目标和早期调查被丢弃，模型可能不知道自己为什么修改这一处计算。

我们想保留的不是所有字节，而是**旧历史里仍然有用的事实**。

这时才有必要引入**摘要（Summarization）**。

不过，在直接让模型摘要整段历史之前，DeepAgents 还先做了一种更便宜的尝试。

### 3.2 为什么要先裁剪旧工具调用的巨大参数？

在 Coding Agent 中，消息过长不一定是因为“推理内容很多”，也可能是因为过去一次 `write_file` 把几百行代码放进了工具参数。

例如：

```python
# 教学示例：假设旧消息包含如下 Tool Call
{
    "name": "edit_file",
    "args": {
        "file_path": "shop/refund.py",
        "old_string": "...大量旧代码...",
        "new_string": "...大量新代码...",
    },
}
```

模型已经知道修改成功，下一步只需关注测试结果；重复发送那次 `edit_file` 的全部字符串参数未必有价值。

因此 DeepAgents 的摘要 Wrapper 在检查压缩之前，会尝试裁剪旧消息中部分大工具调用参数。

**真实源码节选：** [`summarization.py#L1476-L1495`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/summarization.py#L1476-L1495)

```python
# 以下只截取关键语句，省略注释及其他分支

effective_messages = self._get_effective_messages(request)
total_tokens = self._count_tokens(
    effective_messages, request.system_message, request.tools
)

truncated_messages, truncate_modified = self._truncate_args(
    effective_messages,
    total_tokens,
)

if truncate_modified:
    total_tokens = self._count_tokens(
        truncated_messages, request.system_message, request.tools
    )

should_summarize = (
    self._should_summarize(truncated_messages, total_tokens)
    or self._over_budget(request, total_tokens)
)
```

这里的因果顺序非常清楚：

```text
当前有效消息
    |
    v
估算 Token
    |
    v
可否裁剪旧工具调用参数？
    |
    v
重新估算（如有变化）
    |
    v
是否仍需要摘要？
```

它并非无条件清空所有工具参数。该版本的 `_truncate_args` 主要针对旧消息里的 `write_file`、`edit_file` 调用，并保留近期窗口；具体触发条件由配置决定。

**真实源码节选：** [`summarization.py#L1059-L1077`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/summarization.py#L1059-L1077)

```python
for tool_call in msg.tool_calls:
    if tool_call["name"] in {"write_file", "edit_file"}:
        truncated_call = self._truncate_tool_call(tool_call)
        ...
```

这一步解决的是：**减少明显冗余的历史参数**。

但测试日志、长时间调查和多轮推理仍可能造成上下文超限。我们仍需要真正的摘要。

### 3.3 何时触发摘要？由模型可用上下文信息决定

直觉上可以按“消息超过 20 条”触发。但消息条数不等于 Token 数：一条巨大的 `pytest` 日志可能比几十条短消息还长。

DeepAgents 因此优先利用模型提供的上下文窗口信息。

**真实源码节选：** [`summarization.py#L253-L287`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/summarization.py#L253-L287)

```python
def compute_summarization_defaults(model: BaseChatModel) -> SummarizationDefaults:
    ...
    if has_profile:
        return {
            "trigger": ("fraction", 0.85),
            "keep": ("fraction", 0.10),
            "truncate_args_settings": {
                "trigger": ("fraction", 0.85),
                "keep": ("fraction", 0.10),
            },
        }

    return {
        "trigger": ("tokens", 170000),
        "keep": ("messages", 6),
        "truncate_args_settings": {
            "trigger": ("messages", 20),
            "keep": ("messages", 20),
        },
    }
```

这段代码回答的不是“所有模型都在第几轮摘要”，而是**默认阈值怎样被计算**。

当模型的 `profile` 提供 `max_input_tokens`，默认触发比例为 85%，并以 10% 的上下文容量作为近期保留目标。若没有相应 Profile 信息，则退回固定 Token/消息数的设置。

这里两个事实不能混淆：

1. `trigger` 是启动摘要的条件。
2. `keep` 是决定保留近期上下文的策略。

`keep=("fraction", 0.10)` **不是**“只留下全历史的 10% 字符”。实际保留边界需要结合模型可用 Token 容量和消息划分规则计算。

另外，源码使用的默认 `token_counter` 是近似计数函数；真正能否被提供商接受，仍取决于实际模型请求。

---

## 四、第二层机制：摘要旧历史，但不要轻易丢失原始证据

### 4.1 只把历史改成摘要，为什么仍然不够？

现在假设历史被概括为：

```text
用户正在修复部分退款精度问题。
已检查 coupon_service.py。
测试发现退款尾差 0.01。
```

这已经大幅缩短了上下文。

但模型下一步可能需要回答：

> 之前是哪一条断言首先证明折扣分摊不守恒？

如果旧消息已经被永久删除，摘要又省略了断言细节，那就无法从原来的测试输出中重新取证。

因此，更稳妥的需求是：

**模型暂时不看全部原文，但有需要时仍应能够找回原文。**

第七章已经认识 Backend：它为文件内容提供统一的读写接口。此刻它有了另一个具体用途——存放被摘要的历史记录。

### 4.2 真实源码：先卸载（offload），再生成摘要

**真实源码节选：** [`summarization.py#L1507-L1536`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/summarization.py#L1507-L1536)

```python
messages_to_summarize, preserved_messages = self._partition_messages(
    truncated_messages, cutoff_index
)
backend = self._backend

offloaded_media_messages, failed_media = self._offload_inline_media(
    backend, messages_to_summarize
)

session_id = self._get_session_id(request.state)
file_path = self._offload_to_backend(
    backend, offloaded_media_messages, session_id
)

summary = self._create_summary(offloaded_media_messages)
new_messages = self._build_new_messages_with_path(summary, file_path)
```

这几行串起了一条完整因果链：

```text
将旧历史与近期消息分开
          |
          v
处理待卸载消息中的内联媒体
          |
          v
先把旧历史写入 Backend
          |
          v
生成摘要
          |
          v
在摘要消息里附上历史文件路径
```

这不是普通的“删除前几条消息”。它让摘要后的模型输入可以保留一个**返回原始历史的路径线索**。

默认保存位置采用：

```text
/conversation_history/{session_id}.md
```

这里的 `session_id` 是内部生成、作用于本次 Graph 调用的历史标识，而不是用户提供的 `thread_id` 字符串。历史内容按摘要事件追加到文件；重复摘要时可接续同一个文件。参见 [`summarization.py` 模块说明及 `_offload_to_backend()`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/summarization.py#L1204-L1273)。

值得注意，外部文件**存入哪个物理位置**取决于当前 Backend：虚拟路径不等于宿主机上的绝对路径。

### 4.3 为什么 offload 失败不能悄悄当作成功？

Backend 可能暂时无法写入，或者权限不允许保存历史。

源码在 offload 失败时返回 `None` 并发出警告；摘要流程仍可能继续，但历史路径无法用于恢复。

因此我们不能把以下两句话混为一谈：

- “摘要后模型输入变短了。”
- “摘要前每一条消息都已经可靠归档，随时可取回。”

后者依赖 Backend 实际写入成功。对包含测试证据或重要执行日志的任务，应当观察并处理失败告警。

### 4.4 真正改变的是 Model Request，还是 State？

这是本章最关键的一处源码。

如果摘要已经生成，最直觉的设计是直接执行：

```python
# 概念伪代码：一种常见但非本版本的实现方式
state["messages"] = [summary_message, *recent_messages]
```

但 DeepAgents 0.7.32 使用了另一种方式：**保留原始消息记录，通过一个摘要事件描述下一轮应该怎样重构模型可见历史。**

**真实源码节选：** [`summarization.py#L1538-L1567`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/summarization.py#L1538-L1567)

```python
new_event: SummarizationEvent = {
    "cutoff_index": state_cutoff_index,
    "summary_message": new_messages[0],
    "file_path": file_path,
    "created_at_index": len(request.messages),
}

modified_messages = [
    *new_messages,
    *map(_drop_bound_reasoning, preserved_messages),
]
modified_request = request.override(messages=modified_messages)

response, new_state_tail = self._call_with_budget(
    modified_request,
    handler,
    error=overflow_error,
    rejected=request.override(messages=truncated_messages),
)

update: dict[str, Any] = {
    SUMMARIZATION_EVENT_KEY: new_event,
    SUMMARIZATION_SESSION_ID_KEY: session_id,
}
if new_state_tail:
    update["messages"] = list(new_state_tail)

return ExtendedModelResponse(
    model_response=response,
    command=Command(update=update),
)
```

这里有两个不同的输出。

**输出 A：交给模型的请求。**

```python
modified_request = request.override(messages=modified_messages)
```

它让模型此次看到：

```text
[摘要消息, 近期保留消息]
```

**输出 B：交给 Agent 状态机制的更新。**

```python
ExtendedModelResponse(
    model_response=response,
    command=Command(update=update),
)
```

其中更新包含 `_summarization_event` 和 `_summarization_session_id`，而不是默认用删除全部旧消息的操作替换 State 中的原始历史。只有某些预算处理需要补写消息时，才会额外出现 `messages` 更新。

于是系统形成：

```text
                 Graph State
       messages = 仍保留原始历史
       _summarization_event = 摘要边界与内容
                      |
                      v
              构造有效历史
                      |
                      v
         Model Request.messages
            摘要 + 近期消息
```

这解释了一个经常被讲错的现象：

> **在这个固定版本中，“保留 State 原始历史”与“缩短模型输入”不是互相排斥的。**

当然，保留原始历史意味着 State 仍需管理消息增长；持久化代价另有机制处理。第十三章将单独研究 State、Reducer、DeltaChannel 与 Checkpoint，本章先不展开。

### 4.5 第二次摘要时，怎样避免把之前的摘要当作真正原文？

这就需要 `_summarization_event` 的另一个作用。

**真实源码节选：** [`summarization.py#L843-L865`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/summarization.py#L843-L865)

```python
if event is None:
    return list(messages)

summary_msg = event["summary_message"]
cutoff_idx = event["cutoff_index"]
...
result: list[AnyMessage] = [summary_msg]
for index, message in enumerate(messages[cutoff_idx:], start=cutoff_idx):
    result.append(
        _drop_bound_reasoning(message) if index < kept_from else message
    )
return result
```

这里展示的语句是源码关键分支的节选（异常兼容处理略）。

第一次摘要后：

```text
State.messages:     [M0, M1, M2, M3, M4, M5, M6]
摘要事件:            cutoff_index = 4, summary = S0
模型有效消息:        [S0, M4, M5, M6]
```

第十六轮如果又新增：

```text
M7, M8
```

那么再次请求时，并不重新从最早 `M0` 开始发送，而是先把事件应用到 State：

```text
[S0, M4, M5, M6, M7, M8]
```

以上编号和内容是**教学示意**，不是运行实测。真实实现还要处理 `created_at_index`、Provider 推理块绑定、重复摘要边界和异常事件数据。

现在我们已经解决一个问题：**怎样让任务历史既不过度占用上下文，又不轻易丢失证据。**

但即使历史问题解决了，模型仍未必知道“这个项目应该怎样写金额代码”。这不是历史过长，而是**知识尚未进入请求**。

---

## 五、第三层机制：一个项目有很多专项流程，为什么不能全部塞进 System Prompt？

### 5.1 先把专项规则写进 Prompt，可以吗？

退款 Bug 修复时，我们可能给 Agent 这样一段提示：

```text
遇到金额问题：使用 Decimal，检查舍入策略。
遇到数据库迁移：检查向后兼容性。
遇到前端改版：检查无障碍与响应式。
遇到性能问题：优先做性能基准。
遇到测试失败：缩小重现范围。
...
```

当只有三五条规则时，这完全可行。

但随着项目增大，所有专项工作流都放进 System Prompt，会出现两个问题：

1. 当前只修复退款 Bug，却反复发送数据库、前端、部署等无关指令。
2. 专项指令可能本来就存在项目文件里，手动同步到 Prompt 容易失效。

我们需要一个中间方案：

> **先告诉模型有哪些专项能力，以及各自适用于什么情况；只在需要时再读取完整操作说明。**

这才有必要引入 DeepAgents 的 **Skills**。

### 5.2 一个最小 Skill 文件长什么样？

假设项目 Backend 中有：

```text
/skills/project/
    refund-precision/
        SKILL.md
```

`SKILL.md` 可以写成：

```markdown
---
name: refund-precision
description: 修复优惠券、部分退款、金额分摊和 Decimal 舍入问题时使用。
---

# Refund Precision

1. 先阅读 refund.py 与 coupon_service.py。
2. 为金额分摊守恒补充最小失败用例。
3. 使用 Decimal，显式检查 quantize 的舍入策略。
4. 运行相关 pytest，并检查多次部分退款的累计结果。
```

这是**用户可以自行创建的教学示例 Skill**，并非 DeepAgents 内置文件。

此时，模型并不需要在每一轮都读完它。模型先知道：

```text
refund-precision：
修复优惠券、部分退款、金额分摊和 Decimal 舍入问题时使用。
完整说明位于 /skills/project/refund-precision/SKILL.md
```

如果当前任务正好符合描述，再调用文件工具读取对应 `SKILL.md`。

这叫**渐进式披露（progressive disclosure）**：先给目录和描述，再按需取得详细内容。

### 5.3 真实源码：Skills 首先载入的只是元数据

既然 Skill 来自 Backend，就要在 Agent 启动时知道有哪些 Skill 可用。

**真实源码节选：** [`skills.py#L1059-L1098`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/skills.py#L1059-L1098)

```python
def before_agent(self, state, runtime, config):
    if state.get("skills_metadata") is not None:
        return None

    backend = self._backend
    all_skills = {}
    skills_load_errors = []

    for source_path in self.sources:
        source_skills, source_error = _list_skills_with_errors(
            backend, source_path
        )
        if source_error is not None:
            skills_load_errors.append(source_error)
        for skill in source_skills:
            all_skills[skill["name"]] = skill

    skills = list(all_skills.values())
    return SkillsStateUpdate(
        skills_metadata=skills,
        skills_load_errors=skills_load_errors,
    )
```

这段代码是**真实分支的教学式摘录**：参数类型标注、日志语句、注释被省略，其余关键操作沿真实源码顺序展示。

回答三个问题：

**谁调用它？** 第六章中已经研究过，LangChain Factory 将实现了 `before_agent` 的 Middleware 接入 Agent 初次执行路径；Graph Runtime 在该阶段调用它。

**它读取什么？** 根据 `sources` 中指定的位置，从 Backend 搜索并解析 Skill 的元数据。

**它返回什么？** 一个 State Update，其中包含 `skills_metadata` 和加载警告；并不把所有 Skill 正文一次性塞进 `messages`。

同名 Skill 按来源顺序处理，**后面的来源覆盖前面的同名条目**。而当 `skills_metadata` 已存在时，`before_agent` 跳过重新加载；要主动刷新，可以把该字段设为 `None`。这是固定版本明确支持的行为。

因此，Skill 的生命周期可以先画成：

```text
before_agent
     |
     v
从 Backend 读取 Skill 元数据
     |
     v
State.skills_metadata
     |
     v
等待下次 Model Request 使用
```

### 5.4 元数据怎样进入实际的 System Message？

仅仅在 State 里存有 `skills_metadata`，模型本身并不知道。

我们必须把适合模型阅读的说明放进这次请求。

**真实源码节选：** [`skills.py#L1032-L1058`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/skills.py#L1032-L1058)

```python
def modify_request(self, request):
    if self.system_prompt_template is None:
        return request

    skills_metadata = request.state.get("skills_metadata") or []
    skills_load_errors = request.state.get("skills_load_errors", [])
    skills_locations = self._format_skills_locations()
    skills_list = self._format_skills_list(skills_metadata)
    skills_load_warnings = self._format_skills_load_warnings(
        skills_load_errors
    )
    skills_section = self.system_prompt_template.format(
        skills_locations=skills_locations,
        skills_load_warnings=skills_load_warnings,
        skills_list=skills_list,
    )

    new_system_message = append_to_system_message(
        request.system_message, skills_section
    )
    return request.override(system_message=new_system_message)
```

仍是省略类型标注和部分注释的**教学式源码摘录**。

它并不读取 Skill 的整个正文，而是先格式化：

- Skill 来源位置。
- Skill 名称、描述及读取路径。
- 读取元数据时出现的告警。

模型因此知道什么时候可以采用专项 Skill。

`wrap_model_call()` 随后调用 `modify_request(request)`，再把修改后的请求交给 `handler`。在 0.7.32 中，该 Wrapper 还负责根据已经读取的 Skill 决定部分工具的披露；这是更进阶的工具访问机制，本章只保留这个准确边界，不将“出现 Skill”错误地等同于“所有工具立即可执行”。参见 [`skills.py#L1193-L1212`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/skills.py#L1193-L1212)。

### 5.5 读 Skill 正文是谁的工作？

我们已经看到：

```text
模型看到 Skill 简介 + SKILL.md 路径
```

如果模型决定：

```text
这就是部分退款精度任务，我要阅读 refund-precision。
```

它可以通过 FilesystemMiddleware 提供的 `read_file` 工具读取 Skill 正文。这里沿用了第七章装配的能力，并不意味着 SkillsMiddleware 重新实现文件读取工具。

预期交互是：

```text
System Message:
  可用 Skill: refund-precision
  路径: /skills/project/refund-precision/SKILL.md

Model:
  tool_call: read_file(path="/skills/project/refund-precision/SKILL.md")

ToolNode / Backend:
  返回完整 SKILL.md 内容

下一轮 Model:
  根据具体修复步骤调查 refund.py
```

这只是说明运行协议的**预期轨迹**；模型是否真的选择该 Skill 取决于模型行为与指令设置，不是构建时必然发生的事实。

还有一种显式路径：如果调用方希望模型**一定收到**某个 Skill 的正文，可以使用 `pinned_skills`。该版本在 `SkillsMiddleware.before_model()` 中读取被指定的 Skill，并将正文作为消息追加，再清空待处理的 `pinned_skills`。参见 [`skills.py#L1139-L1176`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/skills.py#L1139-L1176)。

于是区分出两种模式：

| 情况 | 具体行为 |
|---|---|
| 一般 Skill | 先展示简介，模型按需读全文 |
| 指定 `pinned_skills` | 在模型调用前把指定 Skill 正文追加为消息 |

这一节只保留了主干。完整的机制——模型读过 `SKILL.md` 之后系统怎样反推、`include_tools` 怎样变成可调用的工具、披露记录为什么每次都要重写——见 [第八章附：SkillsMiddleware 怎样发现 Skill、披露指令并放行工具？](./08a-skills-middleware.md)。

**Skills 解决的是“何时引入专项工作流”。**

但现在又出现另一个问题：金额必须使用 `Decimal`，这不是可选工作流，而是项目每次修改金额代码都应遵守的长期约定。难道也要等待模型主动选中一个 Skill 才知道？

---

## 六、第四层机制：项目的稳定约定为什么不应依赖模型主动发现？

### 6.1 退款项目有些信息必须持续可见

假设我们在项目中有一份：

```text
/AGENTS.md
```

内容是：

```markdown
# Shop Coding Rules

- 金额计算统一使用 Decimal，不使用 float。
- 优惠券分摊的舍入差额必须有明确的归集规则。
- 修改退款逻辑前后均运行相关单元测试。
- 禁止在生产数据库上执行破坏性测试。
```

这些不是一次任务临时生成的操作日志，也不是模型可能选择的某个专项流程；它们是项目持续有效的**工作规则**。

如果依赖模型每次主动读取 `AGENTS.md`，它可能忘记读取。我们需要另一种策略：

> **执行开始时加载项目约定，在每次模型请求中稳定提供这些内容。**

在 DeepAgents 中，负责这一行为的是 `MemoryMiddleware`。

这里的 **Memory** 特指固定版本中基于配置文件（常见为 `AGENTS.md`）读取、组合并注入请求的 Middleware；不要直接把它理解为模型参数更新，也不要把它等同于 LangGraph Checkpointer。

### 6.2 真实源码：第一次运行时读取 Memory 文件

**真实源码节选：** [`memory.py#L250-L279`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/memory.py#L250-L279)

```python
def before_agent(self, state, runtime, config):
    if "memory_contents" in state:
        return None

    backend = self._backend
    contents = {}
    results = backend.download_files(list(self.sources))

    for path, response in zip(self.sources, results, strict=True):
        if response.error is not None:
            if response.error == "file_not_found":
                continue
            raise ValueError(
                f"Failed to download {path}: {response.error}"
            )
        if response.content is not None:
            contents[path] = response.content.decode("utf-8")

    return MemoryStateUpdate(memory_contents=contents)
```

上述代码省略了日志与类型标注，保留真实的读取和错误处理路径。

注意三个细节。

第一，`memory_contents` 已存在时不会重复加载。这是一种状态缓存；并不是每轮模型调用都会重新从磁盘读取全部 Memory 文件。

第二，配置了多个 `sources` 时，Middleware 按顺序加载；不存在的文件可以跳过，但其他下载错误会抛异常。

第三，读取结果先进入 State：

```python
memory_contents = {
    "/AGENTS.md": "# Shop Coding Rules\n...",
}
```

这时模型仍然没有看到内容。因为**State 字段不是自动拼到模型消息里的**。

我们仍需在调用模型前改写 Model Request。

### 6.3 真实源码：把 Memory 放进 System Message

**真实源码节选：** [`memory.py#L312-L325`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/memory.py#L312-L325)

```python
def modify_request(self, request):
    if self.system_prompt is None:
        new_system_message = request.system_message
    else:
        contents = request.state.get("memory_contents", {})
        agent_memory = self._format_agent_memory(
            contents, self.system_prompt
        )
        new_system_message = append_to_system_message(
            request.system_message, agent_memory
        )
    ...
```

接着，`wrap_model_call` 使用改写后的请求。

**真实源码节选：** [`memory.py#L347-L361`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/memory.py#L347-L361)

```python
def wrap_model_call(self, request, handler):
    modified_request = self.modify_request(request)
    return handler(modified_request)
```

这与 SkillsMiddleware 有相似之处：都在 Model Request 边界扩展 `system_message`。

但它们解决的是不同问题：

```text
Skills：
  先提供有哪些专项流程，必要时再读完整指令。

Memory：
  加载持续适用的项目约定，每轮将其纳入请求。
```

Memory 使用模板组织来源文件。固定版本会合并多个 Memory 内容，并在注入前去掉 HTML 注释。缺少内容时会生成“未加载 Memory”的占位说明。参见 [`memory.py#L228-L248`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/memory.py#L228-L248)。

### 6.4 Memory 是否意味着每轮都重新打开文件？

不是。

真实路径是：

```text
首次 before_agent
       |
       v
Backend 读取 AGENTS.md
       |
       v
State.memory_contents
       |
       +----------------------------+
                                    |
之后每轮 wrap_model_call              |
       |                            |
       v                            |
取出 memory_contents <---------------+
       |
       v
拼入 System Message
       |
       v
handler(modified_request)
```

如果后台文件发生变化，但 State 中的 `memory_contents` 没有更新，模型本轮可能仍然看到旧的已加载版本。因此，“Memory 文件已经被修改”不必然等于“现有会话下一轮已重新加载”。

这也说明 Memory 不是自动检索任意项目知识的 RAG 系统：此 Middleware 的正常路径是在配置位置读取文件、在 State 中缓存、在 Model Request 中注入。

### 6.5 为什么“长期规则”仍不能盲信？

`AGENTS.md` 来源于 Backend 文件。它可能过时，甚至可能包含被其他人写入的恶意指令。

固定源码的 `MEMORY_SYSTEM_PROMPT` 明确提醒模型：来自文件的内容应作为参考材料，发生冲突时应优先考虑当前用户要求、适用安全规则和工具验证得到的证据。参见 [`memory.py#L96-L105`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/memory.py#L96-L105)。

在我们的案例里，如果 `AGENTS.md` 写着“使用 `float`”，而现有退款代码和测试证明必须用 `Decimal` 保证业务正确性，Agent 仍应调查事实，不能因为一段文件文本出现在 System Message 中，就把它当作不可质疑的事实。

Memory 扩展了上下文覆盖范围，**没有替代验证过程**。

---

## 七、这些机制最后怎样同时作用于一次模型调用？

我们已经分别解决：

- 长历史为什么需要裁剪和摘要。
- 专项工作流为什么按需披露。
- 项目稳定规则为什么预先加载。

但真实运行时，它们不是三个互不相干的 Prompt 生成器，而是同一条 Middleware 执行链的不同环节。

### 7.1 回看第七章：构建阶段决定了谁能参与

**真实源码节选：** [`graph.py#L843-L877`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py#L843-L877)

```python
deepagent_middleware.extend(
    [
        _ParentNotifierMiddleware(),
        create_summarization_middleware(model, backend),
        PatchToolCallsMiddleware(),
    ]
)

# ...省略其他 Middleware 装配...

if skills is not None:
    deepagent_middleware.append(
        SkillsMiddleware(backend=backend, sources=skills)
    )

append_prompt_caching_middleware(deepagent_middleware)

if memory is not None:
    deepagent_middleware.append(
        MemoryMiddleware(
            backend=backend,
            sources=memory,
            add_cache_control=True,
        )
    )
```

这里复习第七章的一个关键结论：

**`create_deep_agent()` 只是把这些能力装进 Middleware 列表；是否执行、何时执行、具体改动哪个请求，要看相应 Hook 的实际实现。**

还要注意：

- `SummarizationMiddleware` 位于核心栈中，默认参与装配；实际摘要不是每轮都发生。
- `SkillsMiddleware` 只在 `skills is not None` 时加入。
- `MemoryMiddleware` 只在 `memory is not None` 时加入。
- Profile 可能另外插入 Middleware，用户自定义 Middleware 还支持替换、调整；不能凭上述摘录推断所有配置下绝对不变的完整调用顺序。

### 7.2 一个 Model Request 是怎样逐层被修改的？

第六章已经证明：多个 `wrap_model_call()` 的执行方式是**嵌套包装**，不是三个独立 Graph Node。

当没有其他替换或改变顺序的 Middleware 干预时，我们可以把当前关注的三个组件教学化简为：

```text
ModelNode 收到 State
       |
       v
构造 ModelRequest
       |
       v
Summarization.wrap_model_call
  - 根据摘要事件重建有效历史
  - 需要时裁剪或摘要
  - 修改 request.messages
       |
       v
Skills.wrap_model_call
  - 从 State 取得 Skills 元数据
  - 添加 Skill 目录与路径说明
  - 必要时披露已读 Skill 的工具
       |
       v
Memory.wrap_model_call
  - 从 State 取得已加载 Memory
  - 将项目约定拼入 system_message
       |
       v
实际模型 Handler
       |
       v
模型响应沿 Wrapper 链返回
```

这张图是**只保留本章三种 Wrapper 的教学视图**。真实 Harness 中仍有其他 Middleware（例如工具调用消息修补、缓存与内容兼容处理），实际顺序还受自定义配置影响。

此处尤其应注意两种变更位置：

| 修改的对象 | 典型执行者 | 解决的问题 |
|---|---|---|
| `request.messages` | Summarization | 这轮是否发送完整旧历史 |
| `request.system_message` | Skills | 这轮有哪些专项流程可用 |
| `request.system_message` | Memory | 这轮必须知道哪些项目约定 |
| `request.tools` 或工具披露记录 | Skills 高级路径 | 已读 Skill 如何影响工具可见性 |
| `State` 中的 Middleware 字段 | 对应生命周期 Hook / 返回 Command | 后续轮次如何延续这些决定 |

同一份 `State` 可以生成一个**按需裁剪、附加项目知识、带有相应工具定义的 Model Request**。

### 7.3 一次具体请求的前后对照

假设第十五轮开始时：

```text
State.messages:
    过去十五轮完整交互，包含长 pytest 日志。

State._summarization_event:
    曾在第十轮完成摘要。

State.skills_metadata:
    refund-precision → /skills/project/refund-precision/SKILL.md

State.memory_contents:
    /AGENTS.md → 使用 Decimal，金额舍入须守恒。
```

模型调用前，Middleware 生成的内容可能是：

```text
System Message:
    你是负责修复代码的 Agent。

    可用 Skill：refund-precision
    适用：优惠券、部分退款与 Decimal 舍入
    完整说明路径：/skills/project/refund-precision/SKILL.md

    项目约定：
    - 金额必须使用 Decimal
    - 退款分摊必须保证累计金额守恒

Messages:
    [旧历史摘要]
    [最近的修改动作]
    [最新 pytest 失败日志]
    [模型上一轮分析]
```

这只是**预期结构示意**，不是对某个真实 Provider 请求的抓包，也没有声明 `pytest` 已实际运行。

不过到这里，我们已经可以正确回答本章核心问题：

**模型不是直接“看见整个 Agent Harness”，也不是自动读取 Backend 全部文件；模型每一轮真正接收的工作上下文，是 Graph 当前状态、配置和 Middleware 改写共同作用后的结果。**

---

## 八、最小可验证实验：不接 API，也能看到 State 与请求分离

前面讨论的真实框架机制比较多。为了让结论不只是文字，我们先用一个很小的纯 Python 实验验证最核心的设计思想。

此实验不需要 API Key。

### 8.1 实验 A：保留历史，同时只给模型有效消息

将以下内容保存为 `context_separation_demo.py`：

```python
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class ModelRequest:
    messages: tuple[str, ...]
    system_message: str

    def override(self, **changes):
        return replace(self, **changes)


state = {
    "messages": [
        "用户：修复优惠券部分退款精度 Bug",
        "工具：refund.py 全文",
        "工具：coupon_service.py 全文",
        "工具：pytest 失败日志 1000 行",
        "助手：尾差可能在最后一次退款归集",
    ],
    "summary": "已调查退款与优惠券逻辑；测试发现 0.01 尾差。",
    "project_rule": "金额计算必须使用 Decimal。",
}


def build_effective_request(state):
    request = ModelRequest(
        messages=tuple(state["messages"]),
        system_message="你是一个 Coding Agent。",
    )
    request = request.override(
        messages=(state["summary"], *state["messages"][-1:])
    )
    request = request.override(
        system_message=request.system_message + "\n" + state["project_rule"]
    )
    return request


request = build_effective_request(state)

assert len(state["messages"]) == 5
assert len(request.messages) == 2
assert "Decimal" in request.system_message
assert "pytest 失败日志 1000 行" in state["messages"][3]
assert all("1000 行" not in message for message in request.messages)

print("State 历史条数:", len(state["messages"]))
print("本轮请求消息条数:", len(request.messages))
print("项目规则已进入 System Message:", "Decimal" in request.system_message)
```

**这个实验验证什么？**

验证两件事可以同时成立：

1. State 中仍保留完整历史。
2. 当前请求可仅使用摘要与近期消息，并加入项目规则。

**它不验证什么？**

它不验证 DeepAgents 的实际摘要算法、Token 阈值、工具调用协议或模型推理能力。它也没有真正执行 `pytest`。这里的 `ModelRequest` 是我们写的简化数据类，与 LangChain 同名类型不是同一个类。

运行方式：

```bash
python context_separation_demo.py
```

我已使用当前环境的 Python **实际执行**这一独立示例，所有断言通过，输出为：

```text
State 历史条数: 5
本轮请求消息条数: 2
项目规则已进入 System Message: True
```

这仅验证上述简化示例的执行结果，不构成对 DeepAgents 源码分支的集成测试。

### 8.2 实验 B：检查真实框架怎样装配 Skills 与 Memory

下面是一个**框架接口示意**。由于需要安装固定依赖并准备与 Backend 对应的文件数据，本章没有声称已经在本机成功执行这一段。

```python
from deepagents import create_deep_agent
from deepagents.backends import StateBackend

# 教学示意：StateBackend 里的路径不是宿主机文件路径。
# 需要先按 Backend API 将相应 Skill 与 Memory 文件写入状态可访问的位置。
backend = StateBackend()

agent = create_deep_agent(
    model=my_model,  # 显式提供已配置的模型实例
    backend=backend,
    skills=["/skills/project/"],
    memory=["/AGENTS.md"],
)
```

这段配置主要用于检查：

- 构建期是否在 Middleware 栈中装入 Skills/Memory。
- 首次执行时是否读取配置来源。
- Model Request 是否出现相应的 Skill 目录与 Memory 内容。

**不要**仅凭 `agent = create_deep_agent(...)` 成功就认定项目 `AGENTS.md` 已加载，或认定模型已经读取了某个 Skill 的完整正文。真正的加载和请求改写发生在对应运行阶段。

### 8.3 如果要深入验证真实请求，应记录哪些观察值？

实际接入模型或自定义测试 Handler 时，建议观察：

| 观察项 | 要回答的问题 |
|---|---|
| `State.messages` 条数 | Graph 是否仍保留完整消息记录 |
| `_summarization_event` | 是否出现摘要事件及截止位置 |
| `ModelRequest.messages` | 这轮发给模型的是否是摘要 + 近期消息 |
| Backend 历史文件 | 旧消息是否真的 offload 成功 |
| `skills_metadata` | 是否仅加载了 Skill 的名称、描述与路径 |
| `memory_contents` | `AGENTS.md` 是否已经加载到 State |
| `ModelRequest.system_message` | Skills 目录和项目规则是否被注入 |

不要在日志里无条件打印完整模型请求：它可能包含业务代码、机密信息或大量用户内容。验证时可以先打印长度、键名、片段和是否包含预期的标记。

---

## 九、从最小 Agent 到上下文增强 Harness：把图拼完整

现在我们终于有足够的因果基础，能够画出本章完整视图。

```text
用户：修复优惠券部分退款 Bug
                |
                v
          LangGraph State
      ┌────────────────────┐
      │ messages           │  真实历史与工具反馈
      │ skills_metadata    │  可用专项流程的目录
      │ memory_contents    │  已加载项目约定
      │ _summarization_event│ 摘要边界与摘要消息
      └────────────────────┘
                |
                v
      每一轮准备 Model Request
                |
                v
     SummarizationMiddleware
       - 截断旧工具调用参数
       - 需要时摘要旧消息
       - 尝试 offload 到 Backend
       - 生成有效 request.messages
                |
                v
          SkillsMiddleware
       - 列出专项 Skill
       - 按需读取完整说明
       - 按配置披露工具
                |
                v
          MemoryMiddleware
       - 拼入 AGENTS.md 约定
                |
                v
              Model
                |
      产生回答或 Tool Call
                |
                v
             Agent Loop
```

这是一张**职责数据流图**，只表达本章聚焦的三种能力，并不代表完整 Harness 中所有 Middleware 的精确调用顺序或 Graph 节点集合。

回看最初的设计：

```python
model.invoke(all_messages)
```

为什么它会演化成今天这样？

**第一步**，我们发现历史增长，完整重发越来越昂贵。

因此先在模型调用边界区分 **State 与 Model Request**。

**第二步**，直接丢弃旧历史会丢失调查证据。

因此使用 **Summarization**，并将旧内容尝试 **offload 到 Backend**，同时保留摘要事件供后续请求重建有效上下文。

**第三步**，不是所有任务都需要同一套专项指令。

因此使用 **Skills**：目录先进入 Prompt，完整工作流按需读取，特殊情况下还可显式 pin。

**第四步**，有些项目规则必须一直适用，不能依赖模型每次临时发现。

因此使用 **Memory**：首次读取 `AGENTS.md`，在后续请求中从 State 获取并注入。

最终我们得到的不是“会记忆一切的模型”，而是一个能够**在模型调用前主动整理工作材料**的 Harness。

### 9.1 本章设计边界

| 常见误解 | 本章得到的准确结论 |
|---|---|
| State 保存了消息，模型必然看到全部消息 | Middleware 可以构造不同的 `request.messages` |
| 摘要就是删除 State 里的旧消息 | 本版本主要通过 `_summarization_event` 重构有效上下文，保留原始消息记录 |
| offload 就保证历史永远可以恢复 | 还取决于 Backend 写入、路径有效性与读取权限 |
| 有 Skills 就会把全部 `SKILL.md` 自动注入 | 默认先披露元数据；正文通常由模型按需读取，或显式 pin |
| Skill 提到某工具就必然可以执行 | 实际工具是否可见、可用、获准执行是不同的问题 |
| Memory 等于模型长期学习 | 这里是配置文件读取、缓存与请求注入，不是参数训练 |
| 修改了 `AGENTS.md`，已有 State 自动同步 | `MemoryMiddleware` 有加载缓存；需考虑显式重新加载或新会话 |
| Summary 的 Token 计算完全等于 Provider 计费 | 默认存在近似计数，Provider 仍可能拒绝过长输入 |
| 文件里的指令自动高于用户或验证结果 | 文件内容需校验可信度，不替代用户要求与事实核查 |

---

## 十、下一章：模型知道该怎么做，工具调用如何安全落地？

到现在为止，我们已经解决：

> **模型在每一轮运行前，怎样得到既够用又不过分膨胀的工作上下文？**

它可能已经知道：

```text
当前目标：修复部分退款尾差。
项目约定：使用 Decimal。
专项方法：先定位最小失败用例，再检查舍入差额。
最新状态：pytest 在断言处差 0.01。
```

接下来，模型可能决定：

```python
{
    "name": "edit_file",
    "args": {
        "file_path": "shop/refund.py",
        "old_string": "...",
        "new_string": "...",
    },
}
```

可是，一条格式正确的 Tool Call 还不是一次真实的文件修改。

我们还要追问：

- 谁从模型消息中提取工具调用？
- `ToolNode` 在哪里查找对应工具？
- 工具怎样拿到 `ToolRuntime` 与 Backend？
- `FilesystemMiddleware` 怎样把文件操作映射到不同存储环境？
- 当工具输出太长、存在权限限制或执行失败时，会怎样处理？

这些问题属于下一章：

**第九章：工具执行如何被增强？——ToolNode、Filesystem 与 Backend。**

---

## 附：本章主要源码索引

所有 DeepAgents 链接均锁定 Commit `e85c6b9`。

| 本章问题 | 固定源码 |
|---|---|
| Harness 如何安装摘要、Skills、Memory | [`graph.py#L821-L884`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py#L821-L884) |
| 摘要默认阈值 | [`summarization.py#L253-L287`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/summarization.py#L253-L287) |
| 将已存在的摘要事件应用到消息 | [`summarization.py#L843-L865`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/summarization.py#L843-L865) |
| 旧 Tool Call 参数的裁剪 | [`summarization.py#L964-L989`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/summarization.py#L964-L989) |
| 历史消息 offload | [`summarization.py#L1204-L1273`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/summarization.py#L1204-L1273) |
| 摘要决定、Model Request 替换、返回 State 更新 | [`summarization.py#L1444-L1567`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/summarization.py#L1444-L1567) |
| 摘要工厂与默认值 | [`summarization.py#L1700-L1782`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/summarization.py#L1700-L1782) |
| Skill 元数据与状态字段 | [`skills.py#L259-L345`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/skills.py#L259-L345) |
| 首次载入 Skill 元数据 | [`skills.py#L1059-L1098`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/skills.py#L1059-L1098) |
| Skill 清单写入模型请求 | [`skills.py#L1032-L1058`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/skills.py#L1032-L1058) |
| Pin Skill 与请求时披露 | [`skills.py#L1139-L1212`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/skills.py#L1139-L1212) |
| Memory 文件读取 | [`memory.py#L250-L279`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/memory.py#L250-L279) |
| Memory 文本格式化 | [`memory.py#L228-L248`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/memory.py#L228-L248) |
| Memory 注入请求 | [`memory.py#L312-L361`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/memory.py#L312-L361) |

**本章核心产出：** 可以从源码解释为什么 Agent State 不等于 Model Request，以及 DeepAgents 如何通过摘要、Skills 和 Memory 三层渐进式机制，把不断变化的 Coding Agent 工作过程整理成模型每轮真正使用的上下文。
