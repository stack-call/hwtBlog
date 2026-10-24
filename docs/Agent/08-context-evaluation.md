---
title: 第八章：Context Evaluation
description: 用复现任务、反事实测试与故障注入衡量上下文压缩和恢复的真实效果
sidebar_position: 9
tags: [Agent, Evaluation, Context, Benchmark]
---

# 第八章：Context Evaluation——压缩率提高了，为什么修 Bug 的成功率反而下降？

> 前几章，我们不断改善 Agent 的上下文管理：大结果卸载、历史压缩、存储恢复、状态检查点、按需组装。现在，不能再靠「感觉 Agent 变快了」判断设计是否有效。

## 第一次评测：一个令人高兴的假结果

小李给优惠券 Coding Agent 加了自动 Compaction：历史从 28k tokens 压成 4k；单次调用成本降低，任务也不再触发 Context Overflow。

他宣布：「上下文管理成功了！」

直到 QA 团队发现，Agent 最后提交的代码把 `ROUND_HALF_UP` 改成了默认舍入方式。它虽然记住「按照实际支付金额退款」，但摘要漏掉了金额舍入规则。

测试环境对普通整单退款全部通过，对部分退款的边界金额却失败。

小李这才意识到：**压缩率衡量的是内容短了多少，不代表保留的信息足够完成未来任务。**

## 第一次改进：把“上下文变短”换成“任务有没有完成”

他建立了最小的评测集，不让模型凭记忆来答抽象问题，而是在同一个代码仓库里给它真实任务：

| 任务 | 初始条件 | 验证方式 |
|---|---|---|
| A：整单退款 | 优惠券抵扣 20 元 | 相关单测通过 |
| B：部分退款 | 两种商品、比例分摊 | 精度与舍入单测通过 |
| C：跨轮回忆 | 早期 History 才有业务规则 | 修复仍遵守该规则 |
| D：摘要遗漏 | 人为删除 Summary 中某条约束 | Agent 能否从历史恢复 |
| E：源码陈旧 | History 中旧代码与磁盘新代码冲突 | Agent 读取当前文件后修复 |
| F：长 Tool Result | 测试输出上万行 | 日志可回读，关键错误未丢 |
| G：崩溃恢复 | 外部操作完成、Checkpoint 未落盘 | 重试不产生重复副作用 |
| H：跨用户隔离 | 两个用户的项目规范不同 | 记忆、产物不串用 |

这几个任务共同构成一个 **Evaluation Suite（评测集合）**。

单纯做问答召回测试不够，因为 Coding Agent 的最终产出是工作区里的代码和测试结果。对这种任务，**最终环境状态**通常比 LLM 自己写的解释更可信。

公开评测的思路也类似：[SWE-bench](https://www.swebench.com/) 和一些 [Agent Harness 基准实现](https://github.com/microsoft/Orchard-Agentic) 使用确定的仓库版本、问题描述及隔离测试环境，对最终 Patch 进行验证。

## 第二次故障：任务成功率提高了，却不知道为什么

小李比较两版 Harness：

```text
A 方案：只保留最近 20 条消息
B 方案：工具卸载 + Summary + 历史恢复
```

B 的完成率更高，但它也运行了更多工具调用；是 Context 策略好，还是只是“花更多时间搜回丢失的信息”？

因此不能只报告一个分数，还需要区分四类指标：

| 维度 | 建议指标 | 用来识别什么 |
|---|---|---|
| 任务效果 | task success / 回归测试通过率 | 最终修复质量 |
| 上下文质量 | 关键事实保留率、错误旧事实引用次数 | Summary 是否丢失或误导 |
| 恢复能力 | 历史证据找回率、恢复后任务完成率 | Context Recovery 是否真正生效 |
| 成本效率 | 输入 Token、缓存命中、延迟、工具重试次数 | 压缩是否得不偿失 |

**不要把 Token 节省和准确率简单相加成一个万能分数。** 更稳妥的是先设置不能退让的质量门槛，再比较符合门槛的方案成本。

例如：在关键退款用例不出现回归的前提下，哪种策略的平均成本最低？

## 第三次改进：怎样检验 Summary 是否保住了重要事实？

小李发现评测必须建立在**可判定的证据**上，而不是要求 LLM Judge 泛泛地回答「摘要写得好吗」。

他为每个任务记录一些带来源的「必须保留事实」：

```yaml
case: partial-refund
required_facts:
  - id: refund_basis
    expected: paid_amount
    source: docs/refund_policy.md
  - id: rounding
    expected: ROUND_HALF_UP
    source: tests/test_refund_rounding.py
```

然后分别测：

- **Summary recall**：关键事实是否出现在摘要或者可恢复的索引里？
- **Retrieval recall**：需要精确规则时，Agent 是否能找到原始证据？
- **Task success**：找到以后有没有真正正确地修改代码？

有些事实不必强行塞进摘要，但至少应该保存有效引用，以便需要时回读。这样评测关注的不是摘要文采，而是对下游任务的贡献。

## 第四次故障：离线测试通过，线上依然失败

小李发现离线评测通常不会发生网络波动、缓存淘汰和外部服务重复回执。于是加入**故障注入**：

```text
Context Overflow
Artifact Store 读取失败
Summary 生成不完整
Provider 请求超时并重试
Checkpoint 保存中断
外部 Tool 成功但本地没有回执
```

每一次故障都要明确预期状态。例如 Artifact Store 暂时不可用，Harness 应报告证据无法恢复，而不是让 Agent 凭空声称「已经检查过完整日志」。

这样，我们从「压缩做得对不对」升级为「系统在压力下是否仍可靠」。

## 第五次改进：对同一任务改变一个条件

为了排除其他变化，小李开始用 **Ablation（消融实验）**：

| 实验 | 唯一改变的条件 | 想验证的假设 |
|---|---|---|
| Base | 不压缩、合理窗口内运行 | 能达到的任务质量基线 |
| B | 仅开启 Tool Offloading | 大输出是否是主要成本来源 |
| C | B + Conversation Summary | 摘要是否影响正确性 |
| D | C + 原始历史检索 | 可恢复性是否修复摘要损失 |
| E | D + Evidence Freshness 检查 | 旧证据污染是否下降 |

要求同一组任务、相同起始 commit、尽可能一致的模型与预算，并对随机性做多次重复。复杂长期任务的难度差异很大，不能拿两组不同 Bug 的结果直接比较。

一篇较新的研究 [AutoCompact（2026）](https://arxiv.org/abs/2610.02163) 也从长期 Coding Agent 任务成功的角度评估何时压缩、压缩什么，而不是只优化 Token 数量。它是研究思路的参考，不代表我们的实现自动获得相同效果。

## 六个不可轻易省略的测试断言

```python
# 教学性断言列表：按项目真实接口改写
assert no_context_overflow(run)
assert no_orphan_tool_result(model_messages)
assert task_tests_pass(workspace)
assert recoverable_evidence_exists(summary)
assert no_duplicate_external_effects(run)
assert no_cross_thread_leakage(run)
```

这些不是某个 Harness 提供的统一函数；它们表示必须可自动验证的系统性质。

进一步还可以验证：每次 Compaction 是否保存触发原因、Summary 版本、选取边界、原始证据路径；每次恢复是否保存 checkpoint ID 和待执行工具；每次模型请求是否记录实际 Token 预算与被排除的信息类别。记录时注意脱敏和最小保留，不要为了评测泄露用户机密。

## 本章归位

到这里，评测不再只是 Agent 开发末尾的一个分数，而是反过来约束 Context Builder 的设计。

```text
评测任务与故障注入
       ↓
揭示丢失事实 / 误用旧代码 / 重复副作用
       ↓
定位到 Summary / Recovery / Budget / Checkpoint
       ↓
修改策略并回归评测
```

| 问题 | 产生的机制 | 在体系中的位置 |
|---|---|---|
| 压缩率高但 Bug 没修好 | Outcome-based Evaluation | 整体效果的最终约束 |
| 不知道摘要漏了什么 | Evidence-based Checks | Compression 与 Recovery 的测试 |
| 不知道改动是否真的有效 | Ablation | Context 策略选择 |
| 线上崩溃才触发故障 | Fault Injection | Runtime / 外部工具边界 |

**下一章的问题**：到目前为止，我们已经会解释每个机制，但能否把它们落实到同一套可以运行的最小系统里？最后一章将真正实现一个缩小版 Context Manager，并故意保留明确的生产边界。
