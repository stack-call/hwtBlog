---
title: 第六章：Context Management——DeepAgents 如何管理长任务的上下文
description: 从 Coding Agent 的具体问题出发，逐步推导 DeepAgents 上下文管理机制，并结合真实源码理解每个设计决策
tags: [DeepAgents, Context Engineering, Summarization, 源码精读]
sidebar_position: 6
---

# 第六章：Context Management——DeepAgents 如何让 Agent 持续执行长任务？

## 一、问题从哪里开始？

上一章，我们研究了 LangGraph State。

我们知道，Agent 运行过程中，模型消息和工具执行结果会不断写入：

```python
state["messages"]
```

现在回到优惠券部分退款 Bug。

用户要求：

> 修复 refund.py 中的优惠券部分退款精度问题，并运行测试。

Agent 开始执行：

```text
用户：修复退款 Bug
        |
        v
模型：需要先检查代码
        |
        v
read_file("refund.py")
        |
        v
工具：返回 refund.py 内容
        |
        v
模型：发现 Decimal 使用不一致
        |
        v
read_file("test_refund.py")
        |
        v
工具：返回测试代码
        |
        v
模型：决定修改代码
        |
        v
edit_file("refund.py")
        |
        v
工具：修改成功
        |
        v
execute("pytest")
        |
        v
工具：返回大量测试日志
```

这些消息会成为 Agent 的执行历史。

假设任务持续了 50 轮，已经积累了几十万字符。

此时 Agent 还没有完成任务。

它需要继续调用模型。

最自然的代码可能是：

```python
response = model.invoke(
    state["messages"]
)
```

这就出现了问题。

**模型每次调用，都需要重新接收整个消息列表吗？**

如果是，那么：

- 任务执行越久，模型输入越大。
- 大量旧日志会反复发送，增加成本。
- 最终可能超过模型的上下文窗口。

这里的“上下文窗口”，指模型一次请求能够处理的 Token 容量。Token 可以先理解为模型计量文本长度的基本单位。

我们现在遇到的第一个设计问题是：

> 如何在不停止 Agent 的前提下，控制每一轮模型输入的长度？

---

## 二、首先尝试一个最简单的方案

我们可以只传最近的 10 条消息：

```python
recent_messages = state["messages"][-10:]

response = model.invoke(
    recent_messages
)
```

这样确实减少了输入。

但考虑一种情况。

用户最初提出：

```text
退款金额必须精确到分。

多商品优惠券退款时，
每个商品的退款金额之和必须等于总退款金额。
```

这个要求已经在 30 轮之前。

最近 10 条消息只有：

```text
pytest failed
AssertionError
重新检查 refund.py
修改成功
再次运行测试
```

模型现在可能知道测试失败了，但已经不知道最初要满足什么业务约束。

因此，简单删除历史有问题。

**缩短模型输入的同时，我们还必须保留长期任务的关键信息。**

这就引出了第二种方案：摘要。

---

## 三、为什么需要 Summarization？

Summarization 的意思是**摘要压缩**。

例如已有 20 条历史消息：

```text
M1  用户提出退款修复任务
M2  模型决定读取文件
M3  工具返回源码
M4  模型分析精度问题
M5  模型读取测试
M6  工具返回测试代码
M7  模型修改文件
M8  工具返回修改成功
...
M20 最新测试失败
```

我们可以将较早的历史总结为：

```text
历史摘要：

用户要求修复优惠券部分退款 Bug。

已经确认部分金额计算存在精度问题。

refund.py 已完成初步修改。

目前正在运行测试，检查金额分摊是否正确。
```

之后，模型只接收：

```text
历史摘要
   +
最近几轮消息
```

例如：

```text
Summary(M1~M16)
M17
M18
M19
M20
```

这样保留了两类信息：

**历史摘要**：让模型知道任务目标和已经完成的工作。

**近期消息**：让模型知道当前正在处理什么。

这就是摘要的基本思想。

但有一个问题还没有解决：

**在哪里执行摘要？**

我们需要找到 DeepAgents 中负责模型调用的实际位置。

---

## 四、从 DeepAgents 的入口寻找摘要机制

打开：

`deepagents/graph.py`

找到：

```python
create_deep_agent(...)
```

在创建 Agent 的过程中，有这样一段代码：

```python
deepagent_middleware.extend(
    [
        _ParentNotifierMiddleware(),
        create_summarization_middleware(
            model,
            backend,
        ),
        PatchToolCallsMiddleware(),
    ]
)
```

随后，DeepAgents 会把这些 Middleware 传给 LangChain：

```python
return create_agent(
    model,
    middleware=deepagent_middleware,
    # 其他参数省略
)
```

先不要急着研究摘要内部代码。

我们应该先问：

**为什么摘要功能被放进 Middleware，而不是直接写进 Agent Loop？**

回顾第 02 章的 Agent Loop：

```text
Model
  |
  v
Tools
  |
  v
Model
  |
  v
Tools
  |
 ...
```

这个循环本身只需要决定：

- 模型是否继续调用工具。
- 工具执行完以后是否返回模型。
- Agent 什么时候结束。

而摘要是另外一件事：

> 每次调用模型之前，检查当前消息是否过长，必要时缩短输入。

它不应该改变 Agent Loop 的核心结构。

所以 DeepAgents 把这项能力实现为 Middleware。

这样，Agent Loop 保持不变：

```text
Agent Loop
    |
    v
准备调用 Model
    |
    v
SummarizationMiddleware
    |
    v
检查并调整模型输入
    |
    v
真正调用 Model
```

这里就可以理解 Middleware 的设计价值了：

**它允许我们在不重写 Agent Loop 的情况下，控制模型调用前的处理过程。**

这不是为了使用 Middleware 而使用 Middleware，而是因为摘要天然属于模型调用前的横切逻辑。

---

## 五、进入真实代码：`wrap_model_call()`

打开：

`deepagents/middleware/summarization.py`

找到：

```python
class _DeepAgentsSummarizationMiddleware(
    AgentMiddleware
):
    ...
```

其中最重要的方法是：

```python
def wrap_model_call(
    self,
    request,
    handler,
):
    ...
```

这里先解释两个参数。

### `request` 是什么？

当 Agent 准备调用模型时，LangChain 会构造模型请求。

请求中包含：

```python
request.messages
request.system_message
request.tools
request.state
```

它们分别代表：

- 本次模型请求中的消息。
- 系统指令。
- 模型可以调用的工具。
- 当前 Agent State。

### `handler` 是什么？

可以理解为：

> 继续执行后面的模型调用流程。

例如：

```python
response = handler(request)
```

最终会进入真正的模型调用。

因此：

```python
def wrap_model_call(self, request, handler):
    ...
```

就获得了一个重要能力：

**在模型调用前检查请求，必要时修改请求，再继续执行模型。**

这正好符合摘要机制的需求。

---

## 六、但这里出现一个新的设计选择

假设我们已经生成了摘要。

应该怎么办？

### 方案 A：直接改写 Graph State

把：

```python
state["messages"]
```

中的旧消息删除，替换成摘要。

这样做很直接。

但是原始消息不再存在于当前 State 中。

之后如果需要研究 Agent 为什么做出某次修改，就可能缺少执行细节。

### 方案 B：不删除原始消息，只修改本次模型输入

例如：

```text
Graph State 中保存：

M1 M2 M3 M4 M5 M6 M7 M8
```

但本轮模型接收：

```text
Summary(M1~M4)
M5 M6 M7 M8
```

这样我们将两个职责分开：

```text
Graph State
    |
    v
保存执行历史


Model Request
    |
    v
保存本轮要发给模型的内容
```

**DeepAgents 在固定版本的普通摘要流程中，主要采用方案 B。**

它使用：

```python
request.override(
    messages=modified_messages
)
```

创建修改后的模型请求。

注意：

`override()` 不是直接修改整个 Graph State。

它是构造一个经过调整的 ModelRequest。

### 这里才引入 Effective Messages

为了方便区分，DeepAgents 将根据历史摘要规则构造的消息称为：

```python
effective_messages
```

可以理解为：

**当前真正应该提供给模型的有效消息。**

因此我们有：

```text
Graph State 中的 messages
             |
             v
       摘要规则处理
             |
             v
      effective_messages
             |
             v
         ModelRequest
             |
             v
         Model.invoke()
```

这个名称不是一开始就必须知道的概念。

它是因为我们需要区分：

```text
保存了哪些消息？
```

和：

```text
本轮使用哪些消息？
```

才产生的。

---

## 七、DeepAgents 怎样决定要不要摘要？

现在进入 `wrap_model_call()` 的第一部分。

源码中的关键执行顺序如下，省略了异常处理和其他细节：

```python
effective_messages = (
    self._get_effective_messages(request)
)

total_tokens = self._count_tokens(
    effective_messages,
    request.system_message,
    request.tools,
)

truncated_messages, truncate_modified = (
    self._truncate_args(
        effective_messages,
        total_tokens,
    )
)

if truncate_modified:
    total_tokens = self._count_tokens(
        truncated_messages,
        request.system_message,
        request.tools,
    )

should_summarize = (
    self._should_summarize(
        truncated_messages,
        total_tokens,
    )
    or self._over_budget(
        request,
        total_tokens,
    )
)
```

这段代码是对真实源码的重新排版。

我们按照它实际解决的问题来理解。

### 第一步：取得当前应该使用的消息

```python
effective_messages = (
    self._get_effective_messages(request)
)
```

第一次执行时，如果没有摘要记录，那么它基本上就是原始历史。

如果之前已经摘要过，就需要复用旧摘要。

为什么需要复用？

这个问题我们在后面讨论。

### 第二步：计算本次输入有多大

```python
total_tokens = self._count_tokens(
    effective_messages,
    request.system_message,
    request.tools,
)
```

为什么还要计算：

```python
request.system_message
request.tools
```

因为模型的上下文占用不只有对话内容。

工具名称、描述和参数 Schema 也需要发送给模型。

因此必须考虑整个请求，而不仅仅是消息列表。

### 第三步：先尝试缩短旧工具参数

```python
self._truncate_args(...)
```

为什么需要这一步？

看一个具体例子。

模型此前执行：

```python
write_file(
    file_path="/refund.py",
    content="数千行代码...",
)
```

这个 Tool Call 的参数可能很大。

但模型几十轮以后不一定需要反复看到那段完整的写入参数。

因此可以先裁剪旧消息里的大型参数。

这是一次相对局部的压缩。

### 第四步：如果还太长，再完整摘要

```python
self._should_summarize(...)
```

决定是否需要生成摘要。

为什么不直接每次摘要？

因为生成摘要通常还需要调用模型，会增加费用和延迟，而且摘要本身可能丢失信息。

所以 DeepAgents 采用：

```text
当前模型输入
      |
      v
计算输入大小
      |
      v
先裁剪旧工具参数
      |
      v
重新计算大小
      |
      +-- 已足够小 --> 直接调用模型
      |
      +-- 仍然过大 --> 开始摘要
```

到这里，整个执行顺序就有了合理的原因：

**先采用成本较低、影响较小的处理；必要时再使用完整摘要。**

---

## 八、真正需要摘要时，代码做什么？

继续看 `wrap_model_call()`。

当判断需要摘要时，源码会先选择一个分界位置：

```python
cutoff_index = (
    self._determine_cutoff_index(
        truncated_messages
    )
)
```

这里的 `cutoff_index` 只是消息列表中的一个位置。

例如：

```text
消息：

M1 M2 M3 M4 M5 M6 M7 M8 M9 M10
                  ^
                  |
             cutoff_index
```

假设它决定保留最近四条消息。

那么前六条进入摘要范围：

```text
M1 M2 M3 M4 M5 M6 | M7 M8 M9 M10
       旧历史             最近历史
```

随后源码调用：

```python
messages_to_summarize, preserved_messages = (
    self._partition_messages(
        truncated_messages,
        cutoff_index,
    )
)
```

现在得到两组消息。

第一组：

```python
messages_to_summarize
```

需要生成摘要。

第二组：

```python
preserved_messages
```

仍然保留原样，直接给模型。

为什么必须保留近期消息？

因为当前任务需要最新信息。

例如：

```text
pytest 最近一次失败的错误
```

应该优先完整保留，而不只是给出模糊摘要。

另外，切分时还需要考虑模型工具调用消息的合法边界，不能任意把关联的 Tool Call 和 ToolMessage 分开。

---

## 九、摘要会丢失信息，所以 DeepAgents 又做了一件事

到这里我们已经能把旧消息压缩成摘要了。

但摘要有一个无法回避的问题：

**它不可能完美保留全部原始细节。**

例如工具返回：

```text
FAILED test_partial_refund

Expected: Decimal("42.50")
Actual: Decimal("42.49")
```

摘要模型可能总结成：

```text
退款计算还有一分钱误差。
```

但具体数值、测试名称可能没有保留。

假设之后模型又需要检查原始日志，怎么办？

这时才需要引入 Backend 归档。

所谓归档，就是：

> 在旧消息从模型当前输入中被移除之前，尝试把原始内容保存到可以再次读取的位置。

DeepAgents 的 Backend 已经在第 04 章讲过。

它提供统一的文件读写接口。

因此不需要在这里重新发明一个历史数据库。

### 看真实代码

同样位于：

`deepagents/middleware/summarization.py`

在 `wrap_model_call()` 中：

```python
session_id = self._get_session_id(
    request.state
)

file_path = self._offload_to_backend(
    backend,
    offloaded_media_messages,
    session_id,
)

summary = self._create_summary(
    offloaded_media_messages
)
```

注意执行顺序：

```text
准备摘要的旧消息
        |
        v
尝试保存到 Backend
        |
        v
生成摘要
```

为什么先保存，再摘要？

因为摘要是有损的。

提前保存原始内容，之后才有机会按需重新读取。

### 实际保存到哪里？

默认形式为：

```text
/conversation_history/{session_id}.md
```

例如：

```text
/conversation_history/session-123.md
```

源码中的 `_offload_to_backend()` 会：

1. 找到当前历史文件路径。
2. 读取已有归档内容。
3. 将本轮需要归档的消息追加进去。
4. 通过 Backend 的写入或编辑方法保存。

之后摘要消息会包含该文件路径。

模型如果需要细节，可以通过 `read_file` 重新读取相关部分。

这里的设计不是“再存一份数据”这么简单。

它是让：

```text
默认不发送完整历史
```

与：

```text
必要时仍能查阅历史
```

能够同时成立。

需要注意：归档可能失败。固定源码会记录错误并允许摘要继续，此时不能假设原始历史一定可通过 Backend 恢复。

---

## 十、现在我们终于可以完成一次摘要了

到这里，DeepAgents 已经得到：

```python
summary
```

以及：

```python
preserved_messages
```

于是它构造本次模型输入：

```python
new_messages = (
    self._build_new_messages_with_path(
        summary,
        file_path,
    )
)

modified_messages = [
    *new_messages,
    *preserved_messages,
]
```

这里是对实际源码的简化，省略了特定模型 Reasoning 内容的处理。

然后：

```python
modified_request = request.override(
    messages=modified_messages
)
```

把请求交给模型调用链。

此时：

```text
Graph State:

M1 M2 M3 M4 M5 M6 M7 M8 M9 M10
```

本轮模型却只看到：

```text
Summary(M1~M6)
M7 M8 M9 M10
```

我们终于解决了最初的问题：

**既保留任务执行历史，又降低了本轮模型输入的长度。**

如果 Agent 只执行这一轮，到这里似乎就结束了。

但是 Agent 还要继续运行。

新的问题因此出现。

---

## 十一、下一轮怎么办？为什么需要 SummarizationEvent？

假设摘要完成后，模型继续执行：

```text
M11: 模型再次分析退款逻辑
M12: 工具返回新的测试结果
```

Graph State 变为：

```text
M1 M2 M3 M4 M5 M6 M7 M8 M9 M10 M11 M12
```

下一轮模型调用时，如果直接读取 State，它又会看到全部历史。

那么前面那次摘要就没有持续生效。

所以必须记录：

**哪些旧消息已经由哪条摘要替代？**

这时才需要引入 `SummarizationEvent`。

它不是一个凭空出现的新概念。

它是为了让摘要结果可以跨轮次复用。

### 看实际数据结构

源码定义：

```python
class SummarizationEvent(TypedDict):
    cutoff_index: int
    summary_message: HumanMessage
    file_path: str | None
    created_at_index: NotRequired[int]
```

对于我们现在的例子，可以暂时只关注前三个字段。

例如：

```python
event = {
    "cutoff_index": 6,
    "summary_message": summary_message,
    "file_path": "/conversation_history/session-123.md",
}
```

这条记录表达：

```text
前六条原始消息已经被压缩。

摘要内容保存在 summary_message。

完整历史尝试归档到 file_path。
```

这样下一轮只需要：

```text
读取原始 State
        |
        v
读取 SummarizationEvent
        |
        v
用摘要替换前六条的模型可见内容
        |
        v
继续保留 M7 之后的新消息
```

就可以重新构造模型输入。

---

## 十二、真实代码怎样实现跨轮次复用？

源码中有一个很小但非常重要的方法：

```python
def _get_effective_messages(
    self,
    request: ModelRequest,
) -> list[AnyMessage]:

    event = request.state.get(
        SUMMARIZATION_EVENT_KEY
    )

    return self._apply_event_to_messages(
        request.messages,
        event,
    )
```

这段代码对应的逻辑是：

```text
原始消息
    +
摘要记录
    |
    v
模型本轮应看到的消息
```

继续进入：

```python
_apply_event_to_messages(...)
```

其中有两个基本分支。

### 之前没有摘要

```python
if event is None:
    return list(messages)
```

直接使用原始消息。

### 之前已经摘要

读取：

```python
summary_msg = event["summary_message"]
cutoff_idx = event["cutoff_index"]
```

再根据 cutoff，将摘要与其后的消息组合起来。

基本逻辑可以理解为：

```python
result = [
    summary_msg,
    *messages[cutoff_idx:],
]
```

这段是便于理解的简化版本。

实际方法还负责处理错误的摘要事件，以及某些模型提供商对 Reasoning 内容的限制。

### 这里解决了什么问题？

假设：

```text
原始消息：

M1 M2 M3 M4 M5 M6 M7 M8 M9 M10
```

第一次调用生成摘要：

```text
Summary(M1~M6)
M7 M8 M9 M10
```

之后新增：

```text
M11 M12
```

下一轮只需要应用原来的摘要事件：

```text
Summary(M1~M6)
M7 M8 M9 M10 M11 M12
```

而不需要重新摘要 M1～M6。

**SummarizationEvent 的目的，就是让已经完成的压缩决策可以在后续调用中继续生效。**

---

## 十三、摘要记录又是怎样保存到 Graph State 的？

现在还剩最后一个问题。

我们已经创建了：

```python
new_event
```

它应该保存到哪里？

既然下一轮仍然需要它，那么它必须进入 Agent State。

但是：

```python
wrap_model_call()
```

本来应该返回模型执行结果。

它怎样同时返回：

```text
模型响应
```

和：

```text
状态更新
```

呢？

DeepAgents 使用 LangChain 提供的：

```python
ExtendedModelResponse
```

在 `wrap_model_call()` 结尾：

```python
update = {
    "_summarization_event": new_event,
    "_summarization_session_id": session_id,
}
```

再返回：

```python
return ExtendedModelResponse(
    model_response=response,
    command=Command(
        update=update
    ),
)
```

这里出现的 `Command`，我们在第 05 章已经接触过。

它可以把更新交给 LangGraph Runtime。

因此本次模型调用产生两种结果：

```text
             SummarizationMiddleware
                       |
             +---------+---------+
             |                   |
             v                   v
         模型执行结果         摘要状态更新
             |                   |
             v                   v
        ModelResponse       Command(update)
             |                   |
             +---------+---------+
                       |
                       v
              ExtendedModelResponse
                       |
                       v
               LangChain Agent Node
                       |
                       v
                 LangGraph State
```

同时，Middleware 声明了它需要的 State 字段：

```python
class SummarizationState(AgentState):
    _summarization_event: ...
    _summarization_session_id: ...
```

这项声明会通过 LangChain 的 `create_agent()` 集成到 Agent State Schema。

现在你就能把前面学过的知识串起来了。

- 第 01 章的 `create_deep_agent()` 负责装配摘要 Middleware。
- 第 02 章的 `create_agent()` 把 Middleware 接入模型调用流程。
- 第 05 章的 Graph State 保存摘要事件。
- 本章的 `wrap_model_call()` 根据摘要事件调整每轮模型输入。

这不是四套独立机制。

而是同一个 Agent 运行过程中的不同责任层。

---

## 十四、最小实验：运行 DeepAgents 的真实消息重建逻辑

前面讲的是设计和真实调用链。

现在我们直接调用 DeepAgents 的内部方法，验证最关键的结论：

**原始消息没有被这个重建操作删除，但模型可见消息可以更短。**

安装固定版本：

```bash
pip install "deepagents==0.7.32"
```

新建：

`test_summarization_event.py`

```python
from deepagents.middleware.summarization import (
    SummarizationMiddleware,
)

from langchain_core.messages import HumanMessage


# 1. 模拟 Graph State 中的完整消息
messages = [
    HumanMessage(
        content=f"M{i}",
        id=f"m{i}",
    )
    for i in range(1, 11)
]

# 2. 模拟 DeepAgents 已经生成过一次摘要
summary = HumanMessage(
    content="Summary(M1~M6)",
    id="summary-1",
)

event = {
    "cutoff_index": 6,
    "summary_message": summary,
    "file_path": "/conversation_history/demo.md",
    "created_at_index": 10,
}

# 3. 调用真实 DeepAgents 内部方法
effective = (
    SummarizationMiddleware
    ._apply_event_to_messages(
        messages,
        event,
    )
)

print("原始消息：")
print([m.content for m in messages])

print("模型有效消息：")
print([m.content for m in effective])

assert len(messages) == 10

assert [m.content for m in effective] == [
    "Summary(M1~M6)",
    "M7",
    "M8",
    "M9",
    "M10",
]

# 4. 模拟 Agent 继续执行，新增 M11
messages.append(
    HumanMessage(
        content="M11",
        id="m11",
    )
)

# 5. 重复使用此前的摘要事件
next_effective = (
    SummarizationMiddleware
    ._apply_event_to_messages(
        messages,
        event,
    )
)

assert [m.content for m in next_effective] == [
    "Summary(M1~M6)",
    "M7",
    "M8",
    "M9",
    "M10",
    "M11",
]

print("摘要跨轮次复用验证成功")
```

这个实验使用真实的 `_apply_event_to_messages()` 实现，不需要模型 API Key。

它不会触发完整摘要、Backend 写入或 Agent Loop；它验证的是整套设计中最重要的一环：**如何通过摘要事件重建模型输入。**

代码依据固定版本内部接口编写，未在当前环境完成依赖安装后的实际运行验证。

---

## 十五、最后再走一遍完整流程

现在我们从 `create_deep_agent()` 开始，不再单独看某个函数。

```text
                  create_deep_agent()
                           |
                           v
            create_summarization_middleware()
                           |
                           v
                  LangChain create_agent()
                           |
                           v
                    构建 Agent Graph
                           |
                           v
                       agent.invoke()
                           |
                           v
                       Model Node
                           |
                           v
             SummarizationMiddleware
                   wrap_model_call()
                           |
                           v
               _get_effective_messages()
                           |
                           v
                   计算输入 Token
                           |
                           v
                  裁剪旧工具参数
                           |
                           v
                   是否需要摘要？
                           |
                +----------+----------+
                |                     |
                v                     v
               否                    是
                |                     |
                |                     v
                |            切分旧消息和近期消息
                |                     |
                |                     v
                |             _offload_to_backend()
                |                     |
                |                     v
                |                _create_summary()
                |                     |
                |                     v
                |             构造 Summary Event
                |                     |
                +----------+----------+
                           |
                           v
                    request.override()
                           |
                           v
                      handler(request)
                           |
                           v
                     Model Response
                           |
                           v
              保存必要的 Summary Event
                           |
                           v
                     Graph State
                           |
                           v
                      下一轮调用
```

到这里，我们可以解释流程中的每一步为什么存在。

| 代码或机制 | 为什么需要它 |
|---|---|
| `wrap_model_call()` | 在模型执行前调整本轮输入，不重写 Agent Loop |
| `_get_effective_messages()` | 上次摘要后，下一轮要继续使用摘要 |
| `_count_tokens()` | 不能盲目发送无限增长的历史 |
| `_truncate_args()` | 先用较低成本缩短旧工具参数 |
| `_determine_cutoff_index()` | 必须决定压缩哪些旧消息、保留哪些近期消息 |
| `_offload_to_backend()` | 摘要可能丢失原始细节，需要按需找回的路径 |
| `_create_summary()` | 将较长历史转换成紧凑的任务背景 |
| `request.override()` | 只改变本轮模型输入 |
| `SummarizationEvent` | 让摘要结果跨轮次持续生效 |
| `ExtendedModelResponse` | 在返回模型结果的同时保存摘要状态 |

这里的每一个设计都是为了解决前一步产生的实际问题。

---

## 十六、本章最终应该掌握什么？

我们并不是在研究一个简单的“消息摘要工具”。

实际上，DeepAgents 在解决的是：

**长时间运行的 Agent，如何同时管理完整执行历史与有限的模型输入。**

它采用了三个层次：

```text
                   Agent Runtime
                         |
                         v
                    Graph State
                         |
                         | 保存执行历史
                         v
                 SummarizationMiddleware
                         |
                         | 决定本轮使用什么
                         v
                   Model Request
                         |
                         | 输入模型
                         v
                         LLM


              被压缩的历史内容
                         |
                         v
                       Backend
                         |
                         | 需要时重新读取
                         v
                    File Tools
```

最终的设计思想可以总结为：

> **不要为了缩短模型输入，就简单破坏 Agent 的执行历史。应该让历史保存与模型输入选择成为两个独立的职责，再用摘要记录和 Backend 归档把它们连接起来。**

这既解释了 DeepAgents 为什么选择 `wrap_model_call()`，也解释了为什么需要 `_summarization_event`、Backend 和 `ExtendedModelResponse`。

---

## 源码阅读顺序

固定版本：DeepAgents `0.7.32`，Commit `e85c6b9`

1. `deepagents/graph.py`：定位 `create_summarization_middleware()` 的注册。
2. `deepagents/middleware/summarization.py`：阅读 `wrap_model_call()`，理解模型调用前的主流程。
3. 同一文件：阅读 `_get_effective_messages()` 和 `_apply_event_to_messages()`，理解摘要如何跨轮次复用。
4. 同一文件：阅读 `_offload_to_backend()`，理解为什么需要保存原始历史。
5. 同一文件：阅读 `SummarizationEvent` 和 `ExtendedModelResponse`，理解摘要状态怎样回到 Graph State。

源码：

https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py

https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/summarization.py
