---
title: 第九章：从零实现 Context Manager——把前八章的机制真正连起来
sidebar_label: 第九章：Context Manager
description: 把历史事件、工具输出卸载、压缩视图、检查点与测试整合成最小可运行系统
sidebar_position: 10
tags: [Agent, Context, Architecture, Python]
---

# 第九章：从零实现 Context Manager——把前八章的机制真正连起来

> 这次不是再认识一个新名词，而是检验自己能否从约束推导出结构。
>
> 随本章提供 `examples/mini_context_manager.py` 与 `examples/test_mini_context_manager.py`。**这是独立编写的教学实现，不是 DeepAgents / Codex 源码复制，也不是生产可用 Agent。**

## 故事的终点：小李要把原型交给另一支团队

另一支团队要接管优惠券退款 Coding Agent，提出了五个最低要求：

1. 能查到原始执行历史，不能因为摘要而丢失。
2. 大型 Tool Result 必须完整保存，但不能每次都进入模型。
3. 只允许向模型发送预算内的内容，超限时应明确报错，而不是偷偷丢掉关键事实。
4. 任务崩溃后，可以定位最近存下的运行状态。
5. 对外部危险操作的重试必须有稳定标识，不能把「曾发起」误判为「已成功」。

小李不再从「需要哪些类」出发，而是先画出数据流。

```text
                      ┌── Artifact Store（原始日志）
工具结果 → 事件写入 ────┤
                      └── Event Log（可审计历史）
                                     │
                          Compaction State（摘要 + 边界）
                                     │
                             Context Builder
                                     │
                             Model Request

运行时进度 ──→ Checkpoint（下一步在哪）
危险操作 ──→ Operation Ledger（执行状态与重试标识）
```

其中最关键的职责边界是：**存储事实不等于把事实全部放进模型；保存当前执行进度也不等于记录了所有副作用。**

## 第一次落地：以 Event Log 为基座

最早只有一个 `messages` 数组。现在我们把它换成一个按 Thread 隔离、追加写入的事件表：

```text
Event(thread_id, seq, role, content)
Summary(thread_id, cutoff_seq, summary)
Checkpoint(thread_id, state_json)
Operation(thread_id, operation_id, status)
```

为什么是 `cutoff_seq` 而不是 `messages = [summary] + ...`？因为摘要只是一个**替代视图的边界**。原始 Event 仍然存在，必要时还能查第 3 轮的退款舍入规则。

这与 [DeepAgents `SummarizationEvent`](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/middleware/summarization.py) 的 `cutoff_index` / `summary_message` 抽象相近，但教学版本采用 SQLite 与全局递增事件编号，接口并不相同。

## 第二次落地：大工具结果在“进入 History 时”就卸载

我们的 `record_tool_output()` 做两件事：保存完整输出到 Artifact Store，再把引用和预览写入 Event Log。

```python
# 教学实现的核心流程（已在随附代码中实现）
if len(output) > self.offload_threshold:
    ref = self.save_artifact(thread_id, output)
    visible = f"Tool output: {ref}\nPreview:\n{output[:120]}"
else:
    visible = output
self.append(thread_id, "observation", visible)
```

注意：预览是帮助模型判断「要不要回读」的，不是原始日志的替代品。读取必须通过 `ref` 明确进行。

这一机制的真实项目对照是 [DeepAgents `FilesystemMiddleware`](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/middleware/filesystem.py)。框架可以选择不同 Backend 来存储文件；我们的小实现仅用本地目录。

## 第三次落地：Context 是构建出来的，不是历史本身

每次调用 LLM 之前，从 Summary 和其后追加的事件构造模型消息。如果总预算不足，就报错，让上层先进行压缩或改变检索范围。

```python
# 教学实现，省略函数体细节
messages = store.build_context(
    thread_id="coupon-fix-001",
    system_prompt="你是 Coding Agent；禁止修改清算表。",
    max_input_tokens=32000,
    output_reserve=4000,
)
```

真实工程里，组装器还要处理工具签名、多模态输入、模型特定 Tokenizer、工具调用结果成对、最新用户约束与证据有效期。我们的例子只用纯文本消息演示**状态分离与溢出保护**，不能把它的字符近似计数器当作生产 Token 预算器。

对照 [OpenHands `CondenserBase.condense`](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-sdk/openhands/sdk/context/condenser/base.py) 的 `View | Condensation` 接口，可以看出一个更成熟的设计：**压缩逻辑与事件视图构建解耦，且不允许随意修改共享视图对象。**

## 第四次落地：Checkpoint 和 Operation Ledger 独立存在

我们让 `save_checkpoint()` 记录当前进度；危险外部操作则另外使用 `operation_id` 记录发起与完成状态。

```python
store.save_checkpoint("coupon-fix-001", {"next_action": "pytest"})

status = store.prepare_operation("coupon-fix-001", "migration-v2")
# status == "prepared" 只说明已准备/曾请求，不表示迁移完成
# 真正成功且核对外部状态后：
store.complete_operation("coupon-fix-001", "migration-v2")
```

为什么不直接把 `migration_done = True` 放进 Checkpoint？因为「外部工具真正成功」和「本地状态记录成功」不保证原子一致。`prepared` 状态可能表示「尚未执行」，也可能表示「外部已经执行成功，但回执丢失」。**恢复时必须查询外部服务或执行安全的幂等重试，绝不能直接当作未执行。**

这和 [LangGraph 的 Checkpointer / Pending Writes](https://langchain-ai.github.io/langgraph/reference/checkpoints/) 解决的是互补问题。我们的 Operation Ledger 只是教学用本地账本，不保证分布式 exactly-once。

## 第五次落地：让关键架构判断变成自动化测试

你可以直接在示例目录运行：

```bash
python -m unittest discover -s examples -p 'test_*.py' -v
python examples/mini_context_manager.py
```

单元测试至少覆盖：

| 测试 | 预期 |
| --- | --- |
| Compaction 后再查看 History | 原始事件未删除 |
| 构造压缩后 Context | 只保留 Summary 与 cutoff 之后的事件 |
| Tool Result 过大 | 全文仍能通过 Artifact 引用找回 |
| 不同 Thread | 事件、摘要和状态互不混淆 |
| Context 超过预算 | 明确抛错，不静默裁剪 |
| 外部操作处于 prepared | 恢复时不能伪装成 done |

接下来最重要的不是再加十个类，而是把这个小实现与真实 Harness 的职责对应起来：

| 教学模块 | 成熟框架中的相近职责 | 还缺少什么 |
| --- | --- | --- |
| `events` | OpenHands Event History、Codex Rollout | 多模态、异步、重放语义 |
| `summary + cutoff` | DeepAgents SummarizationEvent、OpenHands Condensation | 自动摘要、安全截断、摘要版本 |
| `artifacts` | DeepAgents Backend / File Tools | 权限隔离、远程存储、生命周期 |
| `build_context` | Middleware 对 ModelRequest 的转换 | 精确 Token、工具消息合法性、检索排序 |
| `checkpoint` | LangGraph Checkpointer | 图状态、节点调度、Pending Writes |
| `operations` | Tool Side-Effect Guard | 与外部服务的幂等协议 / 状态核对 |

## 独立推导的最后一道题：换一个新场景

现在不再修优惠券 Bug。你接手一个**自动化数据分析 Agent**：用户上传几百 MB CSV，让 Agent 清洗数据、调用 SQL、生成报告；任务可能在工具执行中断，且每天会有新的数据版本。

不看前文概念名称，先根据约束推导：

- 大 CSV 不能永久放在 Prompt：完整数据存哪里，摘要与采样放哪里？
- 昨天 SQL 查询结果会过期：怎样确定当前证据对应哪个数据快照？
- 报告导出可能运行 20 分钟：崩溃后如何避免重复计费和重复交付？
- 一个用户有多个分析任务：怎样防止 History、Artifact、Memory 串用？
- 如何证明优化有效：哪些是最终结果指标，哪些只是性能辅助指标？

你应当能够从这些问题自然推导出 History、Artifact Store、Context Builder、Version/Freshness、Checkpoint、Idempotency 和 Evaluation。能做到这一步，才真正掌握了 Agent Harness 的上下文管理，而不只是记住各类术语。

## 本章归位：用四条边界检验架构

**数据边界**：哪些原始事实被保留、哪些只能通过引用访问？

**输入边界**：每次模型请求由谁组装，如何保证预算与正确性？

**执行边界**：恢复到哪一步，外部副作用能否安全重试？

**验证边界**：完成率、证据保留、恢复能力与成本是否经得起复现？

四条边界清楚之后，DeepAgents、OpenHands、Codex 和 OpenCode 的实现差异就不再是孤立知识，而是不同工程取舍的结果。

**下一章的问题**：上下文管理解决了「模型看什么」，但 Agent 要连续行动，下一步究竟该由谁推动？
