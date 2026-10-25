---
title: 第二章：Context 没满，Agent 为什么还是会“变笨”？
sidebar_label: 第二章：信息利用质量
description: 从一个持续运行的 Coding Agent 出发，理解上下文选择、工具输出卸载、对话压缩与历史恢复，并对照开源 Harness 源码
tags: [Agent]
sidebar_position: 3
---

# 第二章：Context 没满，Agent 为什么还是会“变笨”？

> 本章接续第一章：上下文已经装得下了，Agent 却还是「变笨」。我们要把「信息存在」「信息相关」「信息有效」三件事分开，推导上下文清理、工作状态重建与知识按需加载，并给出可以复现的验证方法。

## 1. 第 200 轮：Token 预算明明足够

上一次修复之后，小李的 Coding Agent 已经能卸载测试日志、压缩旧对话，也能通过文件路径找回原始历史。按照 Token 监控，它这次发给模型的上下文只有 **24k / 32k tokens**，还没有达到窗口上限。

可 Agent 开始出现奇怪的行为：

```text
第 183 轮：确认 refund.py 已改为基于 paid_amount 计算。
第 191 轮：pytest 测试通过 38 项。
第 200 轮：Agent 再次搜索 refund.py，并提议重做同一处修改。
```

小李的第一反应是：“明明没超限，模型为什么像忘了一样？”

这次不是简单的 **Context Overflow**。过去的记录可能仍在输入里，但**信息能被发送给模型**，不等于**模型一定能正确找到、判断并使用它**。

这种长上下文下信息利用质量下降的现象，常被宽泛地称为 **Context Rot**。需要注意，这不是某个模型统一的故障码，也不能仅凭一次重复搜索就证明发生了 Context Rot；工具策略、摘要错误、工作状态不一致同样可能造成这种症状。

相关研究 [*Lost in the Middle: How Language Models Use Long Contexts*（TACL 2024）](https://aclanthology.org/2024.tacl-1.9/) 发现，在其测试任务中，相关信息处于输入的不同位置时，模型的利用效果可能明显不同，尤其是放在较长上下文中部时。**窗口容量**和**信息使用质量**是两个不同的问题。

## 2. 第一次排查：是不是无关信息太多？

小李打开这次 LLM 请求，发现虽然最大的测试日志早已卸载，但仍留着大量小型工具输出：多次 `ls`、重复 `grep`、过时的文件片段、早就分析完成的正常测试结果。

它们每一条都不大，累积起来却占据了工作视图的大部分空间。

小李最初只在“达到 32k 时”才触发压缩。现在他意识到：**即使尚未超限，陈旧、重复、与当前步骤无关的观察结果也可能降低上下文质量。**

于是他决定先做一件比“总结所有历史”更便宜的事：**清理旧工具输出，保护近期执行结果和真正需要保留的内容。**

### 源码对照：OpenCode 怎样清理旧 Tool Result？

看 OpenCode 的 [`packages/opencode/src/session/compaction.ts`](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/compaction.ts)，重点追踪 `prune()` 和消息序列化逻辑。

下面是文件中的两个关键判断（**真实源码的短节选，省略循环及更新操作**）：

```ts
if (turns < 2) continue
if (total <= PRUNE_PROTECT) continue
```

它从较新的消息往前检查工具输出，跳过近期轮次，并在受保护的近期输出预算内不清理。对于需要清理的历史 Tool Part，代码会打上 `compacted` 标记；后续构造模型消息时，使用短占位文本替代原来的输出。

```ts
const output = part.state.time.compacted
  ? "[Old tool result content cleared]"
  : truncate([part.state.output, ...attachments].join("\n"))
```

这里要分清：**从模型输入中清理内容**，不等于**已经从底层存储物理删除内容**。代码将“这段输出是否进入当前上下文”的决定显式化了。

我们从这个场景引入一个新概念：**Context Pruning（上下文修剪）**。它处理的是已经存在、但不值得持续占据工作视图的信息；与第一章的 History Preservation 并不矛盾。

## 3. 第二次排查：清理完噪声，为什么还是读错代码？

清理旧日志后，Agent 不再频繁重复搜索；但另一个错误出现了。

第 180 轮它读取过一段代码：

```python
refund = order.original_amount
```

第 183 轮工程师已经把真实文件改成：

```python
refund = order.paid_amount
```

可是第 200 轮，模型在总结 Bug 时仍引用了第 180 轮的旧片段。

小李发现：**一条历史记录只说明“某个时间点看到过什么”，未必说明“现在代码是什么”。**

即使原始 History 完整无损，里面也可能同时包含相互矛盾、但都曾经正确的观察结果。简单增加更多历史、把旧片段移到 Prompt 前面，不能保证解决问题。

他因此给信息增加了两个不同的角色：

| 信息类型 | 用途 | 当前退款任务里的例子 |
| --- | --- | --- |
| **历史证据** | 解释当时为什么这样判断 | 第 180 轮读到的旧代码 |
| **当前事实来源** | 决定下一步实际该如何修改 | 此刻磁盘上的 `refund.py` 和最新测试结果 |

当模型需要决定“现在是否还要修改 `refund.py`”时，应该先对照**最新文件内容**，而不是直接相信历史摘要。对于代码这类可变数据，文件系统和测试执行结果往往比旧观察更接近当前事实。

这引出了 **Freshness / Source of Truth（时效性与事实来源）** 的问题：上下文管理不仅要选择“相关”的信息，还要区分“仍然有效”的信息。

> 工程取舍：并非每次都重新读取整个仓库。只在关键决策前，重新核实可能已变化的文件、配置和测试状态；历史摘要保留“为什么这样做”，最新工具结果确认“现在是什么”。

## 4. 第三次改进：给模型一份当前工作状态，而不是扔给它一堆记录

小李进一步检查发现，工作上下文虽然保留了许多“做过什么”，却没有清楚地告诉模型：**哪些问题已经解决，哪些仍待处理，下一步具体该做什么。**

于是他在 Context Builder 中把“当前任务状态”作为显式的组成部分：

```text
当前目标：修复优惠券参与部分退款的计算错误。
确定约束：退款以实际支付金额为依据，舍入规则为 ROUND_HALF_UP。
当前文件：refund.py 已改用 paid_amount（需以最新文件为准）。
已验证：38 项测试通过。
待解决：2 个部分退款边界用例失败。
下一步：先重现失败用例，再核对分摊公式。
证据入口：测试日志路径、相关源码路径、原始历史路径。
```

它不像原始 History 那样忠实记录每一步，而是回答一个更直接的问题：**如果一位新工程师现在接手任务，继续工作最少需要知道什么？**

这里不必发明一个神秘的“记忆模型”，仍然可以利用第一章已经学过的 Summary 和 Working Context。变化在于：摘要的目标从“缩短历史”进一步转向“维护**可执行的任务状态**”。

### 源码对照：OpenHands 为什么区分 Event 与 View？

在 [OpenHands SDK 的 `CondenserBase`](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-sdk/openhands/sdk/context/condenser/base.py) 中，最重要的是这个接口（真实源码）：

```python
def condense(self, view: View, agent_llm: LLM | None = None) -> View | Condensation:
```

`View` 代表供当前决策使用的事件视图，而 `Condensation` 可以作为事件进入执行历史。`CondenserBase` 还要求实现不要原地修改传入的 `View`，因为它可能是缓存的投影。

这意味着我们能够在**不改变过去发生过什么**的前提下，调整**当前要让模型关注什么**。当然，框架提供了这个边界，并不会自动保证摘要总是正确；任务状态仍需与实际文件和测试进行核对。

## 5. 第四次改进：知识越多越好吗？

到这里，Agent 的上下文干净多了。随后团队为它增加了几十个技能文档：Python 测试规范、数据库迁移规范、前端构建指南、退款业务手册……

小李本想把所有技能全文塞进 System Prompt，让 Agent “什么都知道”。但这又制造了大量与当前任务无关的说明。

他改用另一种策略：**先告诉模型有什么能力，再在需要时读取完整说明。**

### 源码对照：DeepAgents 的 SkillsMiddleware

阅读 [DeepAgents `skills.py`](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/middleware/skills.py) 的 `SKILLS_SYSTEM_PROMPT` 和 `SkillsMiddleware`。

代码中的 Skill 指引采用 **Progressive Disclosure（渐进披露）**：模型先看到技能的名称、描述和文件路径；当发现当前任务需要某个技能时，再通过 `read_file` 加载对应 `SKILL.md` 的内容。

```text
知道有哪些 Skills
       ↓
识别当前任务需要哪一个
       ↓
read_file(对应 SKILL.md)
       ↓
使用该 Skill 的完整操作说明
```

这和第一章的 Tool Output Offloading 有相似思想，但作用阶段不同：

- **Offloading**：信息已经产生，决定哪些内容离开上下文。
- **Progressive Disclosure**：信息尚未使用，决定哪些知识应该按需进入上下文。

小李终于发现，上下文管理不只是“满了怎么办”，还包括“**一开始就别把暂时不需要的内容全部放进去**”。

## 6. 怎样证明这次改进真的有效？

现在他手里有几套策略：

| 策略 | LLM 输入方式 | 预期要检验的问题 |
| --- | --- | --- |
| A：完整上下文 | 尽量保留所有可装入的历史 | 信息多是否真的更好？ |
| B：清理工具噪声 | 修剪旧 Tool Result，保留近期结果 | 重复搜索是否减少？ |
| C：聚焦任务状态 | B + 当前状态 + 必要证据回读 | 旧结论误用、重复修改是否减少？ |

这不是凭直觉比较谁的 Token 更少。小李把相同的代码快照、用户任务和模型配置分别交给三种策略，重复运行，记录：

1. **任务正确性**：最终测试通过数量、是否满足业务约束。
2. **无效重复动作**：重复读取文件、重复修改已经完成的代码的次数。
3. **事实新鲜度**：是否根据修改前的旧文件片段作出决定。
4. **资源成本**：输入 Token、额外工具调用、耗时。
5. **恢复能力**：遇到摘要遗漏时，是否找得回原始证据。

还可以专门构造一个**单变量对照实验**：给两组完全相同的资料，只改变关键退款规则在上下文中的位置，观察模型是否还能正确引用它。这样是在验证“信息利用能力”，而不是只观察上下文长度。

不要预先断言 C 必须获胜：Context Builder 的选择也可能删掉关键证据。**压缩率只是手段，任务完成率与可验证性才是结果。**

## 本章归位：信息的存在、相关与有效是三回事

**是什么？** Context Rot 泛指长上下文中信息利用质量下降的现象；它与 Context Overflow 不同，也不是唯一的故障原因。

**为什么需要应对？** 模型即使收到了某条信息，也可能因冗余、位置、冲突或过期而没有正确利用。

**没有这些机制会怎样？** Agent 容易重复工具调用、采纳过期代码片段，或者被大量无关指令分散注意力。

**实际怎么做？** 修剪陈旧工具输出，显式维护当前任务状态，重要决策前核实最新事实，Skills 按需加载，并用任务测试与对照实验验证。

**处于体系哪里？** 第一章解决“哪些历史能被保存与压缩”，第二章进一步处理“在每次模型调用前，怎样挑出可靠、相关、足够当前行动的信息”。

到这里，我们的 Context Builder 已经不再是一个单纯的 Token 截断函数，而是一个具有**选择、验证、组织、按需加载**能力的工作视图构造器。

## 源码阅读索引

1. OpenCode [`session/compaction.ts`](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/compaction.ts)：看 `prune()`，理解为什么清理旧工具输出与摘要压缩不是同一操作。
2. OpenHands [`condenser/base.py`](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-sdk/openhands/sdk/context/condenser/base.py)：看 `View | Condensation`，理解为什么可以重新组织模型输入而不把它等同于原始事件历史。
3. DeepAgents [`middleware/skills.py`](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/middleware/skills.py)：看 `SKILLS_SYSTEM_PROMPT`，理解“元信息先进入、完整知识按需进入”的策略。
4. 学术对照：[Lost in the Middle](https://aclanthology.org/2024.tacl-1.9/)：理解为什么要实际测量长上下文的信息利用能力。

**下一章的问题**：任务结束以后，Agent 应该记住什么？

优惠券 Bug 最终修好了。第二天，新用户让 Agent 修复另一个订单问题。它需要沿用项目退款规范，却不应该把昨天的完整工具调用全部塞到今天的对话里。

下一章会从这里自然引出 **短期任务状态、跨会话 Memory、项目规则与 Skills** 的边界，以及“哪些信息值得长期记忆、如何更新和纠错”。
