---
title: 什么是上下文管理
description: 从一个持续运行的 Coding Agent 出发，理解上下文选择、工具输出卸载、对话压缩与历史恢复，并对照开源 Harness 源码
tags: [Agent]
sidebar_position: 2
---

# 上下文管理



## 第一章：一个 Coding Agent 如何被迫学会上下文管理

### 1. 第 30 次工具调用：历史太长了

小李正在开发一个 Coding Agent。用户给它的任务是：**修复电商系统中优惠券参与退款时金额计算错误的问题**。

Agent 已经搜索代码、读取文件、运行测试，并发现一条业务规则：

> 优惠券抵扣部分不能作为用户实际支付金额再次退还。退款计算要遵守项目已有的折扣分摊和舍入规则。

但模型只有 **32k tokens** 的上下文窗口。运行到第 30 次工具调用时，历史消息累积得太多，下一次请求触发了 Context Overflow。

最直觉的办法是：**只保留最近的记录**。

```python
# 教学示例：最简单的窗口裁剪
context = [system_prompt, user_task] + messages[-20:]
```

Agent 能继续调用模型了，可第 35 轮测试又失败：

```text
FAIL test_coupon_with_refund
Expected refund: 80
Actual refund:   100
```

这次，模型只看到了最新的测试失败，却没看到十几轮前找到的退款规则。它可能重复调查，甚至再次修改已经修对的代码。

小李意识到：**消息的新旧，不等于未来是否有用。**

这里诞生了第一种机制——**Sliding Window（滑动窗口裁剪）**：按最近的消息保留一部分历史。它非常便宜，但可能丢掉仍有价值的旧证据。在真正的实现中，还需要按 Token 而不是仅按消息条数估算大小，并保护 Tool Call 与 Tool Result 的配对关系。

### 2. 第一次补救：只保留重要信息行不行？

小李开始给历史信息划分重要程度：用户要求、业务规则、已修改文件优先保留；文件列表、重复日志优先删除。

但问题马上来了：**谁能提前确定一条消息对未来是否重要？**

假设 Agent 第 8 轮读到：

```python
refund = order.original_amount
```

这行代码当时看上去普通，到第 50 轮却可能成为故障根因。提前按“当前重要程度”永久删除，依然可能犯错。

小李得到一个新的判断：**不能指望在信息刚产生时，就准确预测它在未来的价值。**

与其在“保存”和“删除”之间二选一，不如问一个更根本的问题：

> 信息必须保存在模型上下文窗口里，才能算被保留吗？

### 3. 第二次改进：把大块数据移出上下文

小李发现，项目源码本来就在文件系统里；完整测试日志也可以写入磁盘。模型看过这些内容，并不意味着之后每轮都要带着它们。

于是他把两件事分开：

- **保存数据**：完整 Tool Result 放到外部存储。
- **提供上下文**：给模型保留简短预览、结果摘要和可回读的路径。

```text
原来：User Task + History + 17k 测试日志 → LLM

现在：User Task + History + 错误摘要 + 日志路径 → LLM
                                   |
                                   v
                         Artifact / File Storage
                         保存完整 17k 测试日志
```

下一轮模型只需要看到：

```text
FAIL test_coupon_with_refund
Expected: 80
Actual:   100

完整日志：/artifacts/test-run-31.log
```

如果随后要查看具体堆栈，再调用 `read_file` 按范围读取即可。

这个机制叫 **Tool Output Offloading（工具输出卸载）**。

#### 源码对照：DeepAgents 如何做卸载

阅读这两个文件：

- [`filesystem.py`](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/middleware/filesystem.py)：`FilesystemMiddleware` 的集成逻辑。
- [`_message_eviction.py`](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/middleware/_message_eviction.py)：卸载和生成预览消息的核心辅助函数。

其中 `_offload_tool_message_content()` 的关键路径是（**真实源码节选，省略路径生成等细节**）：

```python
result = backend.write(file_path, content_str)
if result is None or result.error:
    return None
```

写入成功后，代码会构造包含**预览与文件路径**的替代 ToolMessage；如果写入失败，返回 `None`，让调用方保留原消息，而不是假装内容已经安全保存。

这段判断很重要：**先确保原文能找回，再减少模型看到的内容。** 这也是“卸载”和“简单截断”的根本区别。

不过，卸载并不保证零信息损失：模型仍然需要知道何时重新读取，以及如何找到正确的文件。

### 4. 第三次改进：日志卸载以后，为什么还是会满？

大型日志的问题解决了。Agent 可以继续运行到一百多轮，但消息历史仍然持续增长：

```text
第 12 轮：找到优惠券计算入口
第 18 轮：确认退款业务规则
第 24 轮：修改 coupon.py
第 31 轮：退款测试失败
第 35 轮：修改 refund.py
第 42 轮：回滚部分修改
...
第 120 轮：仍有两个边界测试失败
```

这次占空间的不再是单个巨大的 Tool Result，而是**许多小消息累积起来的执行过程**。

小李发现，下一轮继续工作并不需要逐字阅读一百多轮对话。它更需要一份任务交接说明：

```text
目标：修复优惠券退款金额错误。
已确认：退款以实际支付金额为基础；遵守项目舍入规则。
已修改：coupon.py、refund.py。
测试：38 个通过，2 个边界测试失败。
下一步：排查部分退款中的优惠分摊公式。
```

这就是 **Conversation Summarization / Compaction（对话摘要压缩）**：把一段冗长的旧交互变成能帮助 Agent 继续工作的精简状态说明，同时尽量保留近期消息。

**为什么不一开始就只使用摘要？** 因为摘要是有损的：它可能遗漏公式、报错原文、精确标识符或者失败原因。大 Tool Result 更适合先卸载；累积的对话过程才更适合摘要。这是一种有用的工程策略，而不是所有 Harness 必须采用的固定顺序。

#### 源码对照：OpenCode 的摘要写什么？

查看 [`packages/core/src/session/compaction.ts`](https://github.com/anomalyco/opencode/blob/dev/packages/core/src/session/compaction.ts) 中的 `SUMMARY_TEMPLATE` 和 `SUMMARY_RULES`。

它不要求模型泛泛地“概括一下聊天”，而是组织成明确的交接结构：

| 摘要字段 | 对当前退款任务的作用 |
| --- | --- |
| `Objective` | 保留要完成的目标 |
| `Requirements` | 保留用户约束、验收标准 |
| `Decisions` | 保留已经做过的决定及原因 |
| `Work State` | 区分完成、进行中和阻塞事项 |
| `Next Move` | 让下一轮知道该做什么 |
| `Relevant Files` | 指向下一步需要查看的文件 |
| `Important Context` | 保存不容易从文件中重新推导的关键事实 |

这个设计揭示了摘要的真正目的：**不是复述过去，而是为下一次行动提供足够的工作状态。**

同类问题也可以看 [Codex 的 `compact.rs`](https://github.com/openai/codex/blob/main/codex-rs/core/src/compact.rs)。其中 `build_compacted_history`、`build_compaction_replacement_history` 和 `replace_compacted_history` 体现了将摘要与上下文重建纳入会话生命周期的方式。Codex 的实现会替换当前使用的压缩后工作历史；不要将它简单理解成始终保留一份独立、不可变的 `messages` 数组。

### 5. 第四次改进：摘要忘记了精确细节怎么办？

到第 135 轮，出现了新的失败：

```text
FAIL test_partial_refund_with_coupon
Expected: 42.50
Actual:   45.00
```

Agent 需要回看当时使用的分摊公式和舍入方式。可之前的摘要只写着：

> 已修复优惠券折扣分摊逻辑。

摘要无法凭空恢复没记下来的具体计算步骤。

小李想：既然 Tool Result 可以卸载，**被摘要替代的旧对话也可以另外保存**。因此形成了两条路径：

```text
                     完整旧 History
                         |
            +------------+------------+
            |                         |
            v                         v
      保存原始消息                 生成精简 Summary
            |                         |
            v                         v
  /conversation_history/...       Working Context
            ^                         |
            +---- 按需检索/读取 <------+
```

这里有两种互补的能力：

- **Summary**：用较少 Token 保留任务进度和必要事实。
- **History Recovery**：在需要精确细节时，重新检索原始记录。

#### 源码对照：DeepAgents 如何留下回读入口

在 [DeepAgents `summarization.py`](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/middleware/summarization.py) 中，重点找：

- `_build_new_messages_with_path()`：生成摘要消息，并在原始历史保存成功时附上存储路径。
- `_get_session_id()`：确定写入原始历史文件所使用的会话标识。
- `_filter_summary_messages()`：避免再次压缩时把以前的摘要当成未保存的原文重复卸载。

当前实现会把被卸载的历史按会话保存在类似 `/conversation_history/{session_id}.md` 的路径下。**注意，只有 Backend 真正保存成功，回读入口才有效**；如果保存失败，摘要消息不会附上虚假的文件路径。

这一步才让我们明白：**摘要不是历史的替代品，而是历史的一种紧凑表示。**

### 6. 第五次改进：压缩上下文，是否必须修改原始历史？

现在小李准备整合这些机制，却发现一开始的数据结构有问题：

```python
# 教学示例：同一个变量承担两种职责
messages.append(tool_result)
response = llm.chat(messages)
```

`messages` 既是“所有发生过的事”，又是“本轮发给模型的内容”。一旦直接写：

```python
messages = [summary] + recent_messages
```

如果原文没有另存，就丢失了旧消息。后来想审计、调试、恢复细节，可能都做不到。

于是，小李将两个概念拆开：

| 概念 | 解决的问题 | 是否必须完整 |
| --- | --- | --- |
| **History（原始历史）** | 实际发生过哪些对话、动作、观察 | 理想上完整，便于追溯 |
| **Working Context / View（工作视图）** | 这一次 LLM 应该看到哪些消息 | 不需要完整，只需足够有效 |
| **Compaction State（压缩状态）** | 哪段旧历史被什么摘要代替 | 需要能够正确重建工作视图 |

关键并不是工程中绝对不能修改 `messages`，而是**改变工作视图时，不应该无意中丢掉唯一一份可追溯的原始信息**。

#### 源码对照一：DeepAgents 从压缩事件构造有效消息

仍看 [`summarization.py`](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/middleware/summarization.py)。当前 `_get_effective_messages()` 的核心调用是：

```python
# 真实源码节选

event = request.state.get(SUMMARIZATION_EVENT_KEY)
return self._apply_event_to_messages(request.messages, event)
```

`_apply_event_to_messages()` 根据压缩事件中的 `summary_message`、`cutoff_index`，组合出**摘要 + 截断点之后的消息**，还处理异常事件和部分 reasoning block 的清理。由此，完整状态中的消息与本轮模型看到的有效消息不必相同。

#### 源码对照二：OpenHands 把 View 做成明确的抽象

阅读 [OpenHands SDK 的 `CondenserBase`](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-sdk/openhands/sdk/context/condenser/base.py)：

```python
# 真实接口声明
def condense(self, view: View, agent_llm: LLM | None = None) -> View | Condensation:
```

这里的两个返回类型表达了两种不同动作：

- 返回 `View`：直接给出模型下一步使用的历史视图。
- 返回 `Condensation`：产生一条压缩事件，把它纳入事件历史；之后再由事件构造新的 `View`。

OpenHands 还明确要求实现**不要原地修改传入的 `View`**，否则可能破坏缓存的视图。这不是文字游戏，而是在保护事件记录与模型可见视图之间的边界。

这样，History 与 View 的关系就清楚了：

```text
工具执行 / 模型回复
        |
        v
   追加执行事件
        |
        v
   History / State
        |
        +-----> 保存供审计和恢复的记录
        |
        v
  Context Builder / Condenser
        |
        v
  本轮 Working Context
        |
        v
       LLM
```

> **实现上的区别**：DeepAgents、OpenHands、OpenCode 和 Codex 的状态结构、持久化范围并不完全一样。有的保留原始事件并生成视图，有的替换工作历史并另行记录摘要或事件。我们提炼的是共同的**设计目标**，并不是说它们用了完全相同的类或存储模型。

## 回头看：这套设计是怎样自然长出来的？

| 场景中的问题 | 最自然的机制 | 没有它会怎样 |
| --- | --- | --- |
| 历史太长，超过模型窗口 | Sliding Window | 请求直接溢出 |
| 旧消息可能还有价值 | 重要信息选择 | 按时间裁剪会遗漏重要证据 |
| 单次工具输出非常大 | Tool Output Offloading | 大量 Token 被日志占据 |
| 普通对话仍不断累积 | Summarization / Compaction | 长会话迟早再次溢出 |
| 摘要省略了精确细节 | History Preservation + Recovery | 后续无法找回原始依据 |
| 压缩与执行记录混在一起 | History / View 分离 | 难以审计、调试和切换压缩策略 |

这些不是六项平行的功能，而是有限上下文与长任务执行之间的矛盾，推动架构逐层演化出来的结果。

### 五个问题复盘

**是什么？** 上下文管理是决定什么信息在何时进入模型，以及原始信息怎样保存、压缩和恢复的机制。

**为什么需要？** 模型的上下文窗口有限，而 Agent 的执行过程可以持续增长。

**没有它会怎样？** 轻则重复读文件、增加 Token 和延迟；重则溢出、遗忘约束、错误修改代码。

**实际怎么做？** 对工具输出卸载，对长期对话压缩，维护可回读的存储路径，并在模型调用前组装工作视图。

**处于体系哪里？** 它位于 Agent Runtime 的模型调用路径上，连接 Tool Execution、History/State、Backend 和 LLM Request。

## 源码阅读顺序

不用逐行啃大文件。带着刚才故事里的问题定位相应函数：

1. **为什么 Tool Result 可以变短但不直接丢失？** 看 DeepAgents [`_message_eviction.py`](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/middleware/_message_eviction.py) 的 `_offload_tool_message_content()`，以及 [`filesystem.py`](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/middleware/filesystem.py)。
2. **摘要怎样帮助下一轮继续工作？** 看 OpenCode [`compaction.ts`](https://github.com/anomalyco/opencode/blob/dev/packages/core/src/session/compaction.ts) 的 `SUMMARY_TEMPLATE`、`buildPrompt()`。
3. **摘要后怎样找回旧细节？** 看 DeepAgents [`summarization.py`](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/middleware/summarization.py) 的 `_build_new_messages_with_path()`。
4. **原始 History 与 LLM 输入究竟如何分开？** 看同文件的 `_get_effective_messages()`、`_apply_event_to_messages()`，再看 OpenHands [`CondenserBase`](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-sdk/openhands/sdk/context/condenser/base.py)。
5. **另一种压缩后工作历史更新方式是什么？** 看 Codex [`compact.rs`](https://github.com/openai/codex/blob/main/codex-rs/core/src/compact.rs) 的 `build_compaction_replacement_history()` 及 `replace_compacted_history()` 调用。

> [!NOTE] 源码版本
> 本页按 **2026-10-24** 所核对的公开仓库内容组织。链接指向仍在演进的 `main` / `dev` 分支；后续函数名、默认阈值与文件路径可能变化。以具体仓库版本为准。

## 下一章：Context 没满，Agent 为什么仍然“变笨”？

到第 200 轮，我们的 Agent 终于能控制 Token 预算，也能按需找回历史。然而即使工作上下文没有超限，它仍然开始重复搜索、忽略先前结论，甚至反复修改同一段代码。

新的问题不再只是“**能不能装下**”，而是“**装进去的信息是否真的能被有效利用**”。下一章我们会在同一个优惠券项目里研究 **Context Rot、上下文选择质量与验证方法**。
