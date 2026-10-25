---
title: 第七章：Context Assembly——下一次调用 LLM，究竟要带上什么？
sidebar_label: 第七章：Context Assembly
description: 从一次具体的模型请求推导上下文组装器、预算、证据新鲜度和前缀稳定性
tags: [Agent, Context, Token-Budget, Context-Engineering]
sidebar_position: 8
---

# 第七章：Context Assembly——下一次调用 LLM，究竟要带上什么？

> 前一章解决了「Agent 崩溃后能否继续」。这次问题发生得更早：每次恢复或运行到下一轮时，**由谁决定 LLM 的输入到底是什么？**

## 第 205 轮：所有信息都“有用”，但装不下

优惠券退款 Agent 修好了数据迁移问题，终于准备跑完整回归测试。小李把每类信息的 Token 粗略统计了一遍：

| 信息 | 估算 Token |
| --- | ---: |
| System / 安全与工作规则 | 3,000 |
| Tool Schema | 4,000 |
| AGENTS.md 与项目规范 | 5,000 |
| 历史 Summary | 4,000 |
| 最近消息 | 10,000 |
| 相关源码与测试证据 | 8,000 |
| 合计 | **34,000** |

模型上下文窗口只有 32k，更糟的是还要留出模型回复和下一步工具调用的空间。

小李最初想：每类各删 20% 不就行了？

可删去 20% System Prompt 与删掉重复测试日志的风险完全不同；删除刚刚读取的退款源码可能让模型修错地方，而某份老日志已经没有当前价值。

于是他第一次意识到：**Context Builder 不仅是拼接消息，还必须解决竞争预算的问题。**

## 第一次改进：按「来源和职责」而不是「一条大列表」组装

小李将模型输入划分为几个来源：

```text
Task / Constraints         用户目标与硬约束
System / Tool Contracts    系统规则及工具签名
Active State               正在做什么、阻塞在哪里
Recent History             最近的完整交互
Retrieved Evidence         当前所需证据（代码、日志）
Optional Memory / Skills   必要时加载的长期规则和能力说明
```

再做一件容易被忽略的事：**区分“不能删”的硬约束与“可以推迟加载”的可选上下文。**

例如用户要求「不能动支付清算表」，这是硬约束；一段与退款无关的部署 Skill 则不是。这个区分直接改变取舍结果。

### 关键实现：先定硬预算，再选择候选内容

下面只是**教学伪代码**，表示决策步骤，不是某个开源项目的原样函数：

```python
budget = model_context_limit - output_reserve - safety_margin
selected = pin_required(system, tools, user_constraints)
selected += choose_recent_and_evidence(candidates, budget - cost(selected))
assert cost(selected) <= budget
```

现实中 Token 估算应使用模型适配的 Token Counter，工具 schema、消息包装、多模态内容也应计入。粗略的 `len(text) // 4` 只能用于离线演示，不适合精确防溢出。

## 第二次故障：选得太“重要”，却使用了过时事实

小李设计了按重要性排序的规则：被引用次数最多的证据排在前面。

但现在模型同时看到：

```text
第 80 轮的 refund.py：refund = original_amount
第 198 轮的 refund.py：refund = paid_amount * item_ratio
```

两段代码都曾是正确的**历史观察**，却不可能同时代表当前磁盘上的实现。

这里必须引入一个新的判断：**信息是否新鲜（Freshness / Validity）**。

源码这类可变化的外部状态，应以当前工作区中的文件版本为准；旧 History 可以帮助解释「为什么曾经修改」，但不能直接冒充「当前文件内容」。

小李因此给证据加上来源和版本：

```text
path: refund.py
revision: <git-sha-or-content-hash>
observed_at: 2026-10-24T...
kind: source_snapshot
```

如果代码又被编辑，就重新读取或验证 Hash。否则，即使 Token 预算完全够用，也可能因为陈旧证据做错判断。

> **新的认识**：上下文质量不只是「相关」，还取决于正确性、时效性与可追溯性。

## 第三次故障：每轮重排所有内容，KV Cache 复用变差

小李试图把最重要的内容永远排在开头：第一轮是项目规范，第二轮是失败日志，第三轮又变成搜索结果。虽然模型似乎能更快注意到新证据，但相同前缀不断改变，会让推理服务更难复用 Prefix KV Cache。

更稳妥的顺序通常是：

```text
相对稳定：[System / Tool Schema / 基础规范]
相对动态：[任务状态 / Summary / 近期 History]
高度动态：[最新检索证据 / 新 Tool Result]
```

这里是**成本优化建议，不是语义正确性的硬规则**。如果关键风险、最新用户指令或高优先级安全约束发生变化，应优先保证模型拿到正确信息，而不是为了命中缓存维持错误前缀。

> [!TIP] 上下文管理与 KV Cache 的关系
> Context Management 决定模型看什么，KV Cache 决定哪些 Token 的计算可以复用。
>
> - 上下文长度 ≠ 实际计算量：KV Cache 可以减少重复计算，但不会减少上下文占用的 Token 数量。
> - 前缀稳定有利于缓存命中：如果每轮只在末尾追加消息，推理引擎通常可以复用已有前缀的 KV Cache。
> - 修改历史可能导致缓存失效：Compaction、修改 System Prompt 或替换旧消息，会改变 Token 前缀，使变化位置之后的缓存难以复用。
> - 压缩与缓存需要权衡：Compaction 虽然可能降低缓存命中率，却能减少上下文长度，避免溢出并降低后续推理成本。
>
> 设计原则：尽量保持前缀稳定、增量追加新消息，在必要时再进行上下文压缩。不要为了缓存命中率牺牲上下文质量。

## 第四次改进：Context Builder 应该位于哪里？

小李考虑在每个 Tool 里分别裁剪自己的输出，结果策略很快散落一地：`read_file` 有一个阈值、测试命令有另一个阈值、摘要又绕过这些规则。

他转而把机制放在模型调用边界：

```text
Tool / Agent State
      ↓
统一的 Model Request 构建入口
      ↓
选择 / 排序 / 摘要 / 预算检查
      ↓
Model Provider
```

工具自身仍可以在返回时先进行**大结果卸载**，但决定一次请求到底包含什么，要有统一入口，才能验证整体预算与消息合法性。

### 源码对照一：DeepAgents 的 Middleware 装配

打开 [DeepAgents `graph.py`](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/graph.py)，能看到它把 `FilesystemMiddleware`、`create_summarization_middleware(...)`、`SkillsMiddleware`、`MemoryMiddleware` 等放入 Agent 的 Middleware 链，再交给底层 `create_agent`。

它没有一个恰好叫做 `ContextBuilder` 的统一公共类；真正的上下文组装是**多个 Middleware 对模型请求共同作用**的结果。

有一个值得关注的实际细节：新版 DeepAgents 的 `DeepAgentState.messages` 使用 `DeltaChannel` 减少长历史 Checkpoint 的重复存储。这是在优化**状态持久化开销**，而不是减少模型每轮看到的 Token；两者不能混为一谈。

### 源码对照二：DeepAgents 的压缩事件构建视图

[DeepAgents `summarization.py`](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/middleware/summarization.py) 中的 `_apply_event_to_messages` 核心逻辑可概括为：

```python
# 源码关键逻辑节选，省略类型检查及异常保护
result = [event["summary_message"]]
result.extend(messages[event["cutoff_index"]:])
return result
```

它说明：**原始状态消息**与**这次模型实际看到的消息**可以不同。`cutoff_index` 决定摘要接管哪一段历史，后面的近期消息仍按原样保留。

### 源码对照三：OpenCode 的保留窗口与 Buffer

[OpenCode V2 Compaction 文档](https://opencode.ai/v2/docs/compaction) 把 `keep.tokens`（最近多少历史保留精确内容）与 `buffer`（为模型调用预留多少空间）分开配置。前者回答「保留什么」，后者回答「什么时候需要腾空间」。

这也告诉我们：**保留最近消息的窗口**，不应与**输出预算保留**混成一个参数。

## 最后一次故障：消息集合对了，但消息顺序不合法

小李先裁掉旧消息，然后发现某条 `tool` 响应还在，对应的 assistant `tool_call` 却被删掉了。

模型 Provider 可能拒绝这种不完整消息链。于是 Context Builder 除了 Token 上限，还需要验证**结构正确性**：工具调用与结果配对、消息角色顺序、不能将一组互相依赖的交互随意截开。

压缩时要在安全边界处截断，不能只看字符或 Token 下标。[LangChain Summarization Middleware 源码](https://github.com/langchain-ai/langchain/blob/master/libs/langchain_v1/langchain/agents/middleware/summarization.py) 中就有选择安全截断点的相关逻辑。

## 本章归位

| 要解决的问题 | 自然产生的设计 | 为什么不是别的机制 |
| --- | --- | --- |
| 固定窗口无法容纳所有信息 | Budget Manager | KV Cache 不会扩大逻辑上下文 |
| 来源多且重要性不同 | Context Assembly | 纯 Sliding Window 不理解来源 |
| 旧代码误导模型 | Evidence Freshness | 摘要也可能复述过期事实 |
| 重排内容增加成本 | Stable Prefix | 只是优化条件，不能代替准确性 |
| 裁剪后消息不合法 | Message Invariants | 单纯按 Token 裁剪不保证配对 |

**下一章的问题**：我们现在的 Context Builder 似乎设计得很合理，但真实完成任务的成功率是否提高了？压缩率更高，是否意味着工程表现更好？答案不能靠感觉，要靠评测。
