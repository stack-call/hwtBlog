---
title: 第十九章：从零实现一个最小 Agent Harness
sidebar_label: 第十九章：最小 Harness
description: 以优惠券测试为例整合 Loop、工具、策略、执行、适配、事件与验证
sidebar_position: 20
tags: [Agent, Harness, Python, Architecture]
---

# 第十九章：从零实现一个最小 Agent Harness

> 这一章交付一个**可运行的教学级 Agent Harness**。不接真实 LLM、不执行任意 Shell、不接真实支付服务，目的是让运行时边界清晰、故障可测试。示例代码位于合集的 `examples/mini_agent_harness.py`，测试位于 `examples/test_mini_agent_harness.py`。

## 第一幕：不再把所有问题都丢给 Agent Loop

小李已经用几十轮故事弄懂了各个模块。现在他决定重新实现优惠券 Bug 的最小处理链：

```text
用户任务
  → Session / Event Log
  → Context Builder
  → Model Adapter (这里用 Fake Model)
  → Agent Loop
  → Tool Registry → Policy → Tool Runtime
  → Tool Result → Event Log
  → Context Builder → 下一个模型请求
```

这套设计不试图一次做成完整 Codex。关键在于**每个模块只负责一个清晰的边界**。

## 第二幕：把“模型想做什么”和“实际允许做什么”分开

模拟模型第 1 轮要求运行 `read_file`，第 2 轮要求 `run_refund_test`，第 3 轮返回最终答复。Harness 不应该因为模型自称“测试通过”就相信它；真正的工具运行结果由 Tool Runtime 产生。

示例中的核心函数非常短：

```python
# 来自本合集教学实现 mini_agent_harness.py 的精简调用关系
async def run(self, task):
    self.events.append("task_started", task)
    for _ in range(self.max_steps):
        output = await self.model.next(self.events.snapshot())
        if output.final is not None:
            self.events.append("task_completed", output.final)
            return output.final
        result = await self.dispatch(output.tool_call)
        self.events.append("tool_result", result)
    raise RuntimeError("step budget exceeded")
```

注意这段是核心控制流的阅读示意，实际 `run()` 请看文件，包含具体的数据结构和错误处理。

## 第三幕：依靠不可变事件和明确结果判断，而不是堆 Prompt

我们给工具结果定义 `call_id`、`status` 和 `content`，再把模型决策与执行结果记录下来。Tool Registry 拒绝未知工具；Policy 对有副作用的调用进行控制。最终模型是否完成由结构化输出判断。

在真实 Harness 中，还需要更严谨地处理 Tool Call/Tool Result 的模型协议、并行写入、恢复后的工具幂等，以及流式参数拼接。本教学实现特意保留“工程可以看清的最小内核”，不会假装已经覆盖所有生产特性。

## 第四幕：运行与验证

从 ZIP 中解压，在 `examples` 目录执行：

```bash
python -m unittest -v test_mini_agent_harness.py
python mini_agent_harness.py
```

测试包括：正常两次工具调用、未知工具拒绝、危险工具拒绝、工具异常回传、步数预算、事件关联。实际通过情况以你本地运行输出为准。

## 第五幕：回到真实代码对照

我们的最小 Harness 中，各模块对应的真实源码阅读入口：

| 自己实现的边界 | 对照源码 | 真实项目体现的额外复杂度 |
| --- | --- | --- |
| `AgentHarness.run` | [Codex `run_turn`](https://github.com/openai/codex/blob/main/codex-rs/core/src/session/turn.rs) | 流式、取消、多 Tool Call、事件流与重试 |
| `Registry` | [Codex ToolRegistry](https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/registry.rs) | Tool Spec、延迟加载、并发属性与路由 |
| `Policy` | [Codex ToolOrchestrator](https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/orchestrator.rs) | Sandbox、批准、工具类型策略 |
| `Model` | [OpenCode LLM Adapter](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/llm.ts) | 流事件、Provider 差异、Transport |
| `Events` | [Codex Tool Events](https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/events.rs) | UI 协议事件、跟踪、文件变更 |
| `Context` / `Checkpoint` | 前九章源码 | 压缩、持久化、恢复与长期记忆 |

## 第六幕：怎样把教学 Harness 升级到生产级？

按风险而非炫技程度推进：

1. **先确保安全边界**：Schema 校验、权限策略、隔离环境、危险操作不自动执行。
2. **再确保执行一致性**：Tool Call 与结果严格配对、超时和取消、错误分类、可恢复状态。
3. **再提升模型兼容性**：流式解析、多 Provider、统一 Capability 与 Retry 策略。
4. **再实现长任务**：Context Budget、Compaction、持久化 Checkpoint、人工中断、Subagent。
5. **最后系统评估**：正确性、延迟、Token 成本、缓存、可观测性与故障注入。

### 最终独立题：不是优惠券，而是库存补偿

新需求：用户支付成功后库存扣减失败，Agent 需要定位异常、提出修复、在隔离环境验证、等待审批后提交变更。

不提示术语，独立推导出：任务边界、模型输出协议、执行隔离、审批恢复、幂等工具、事件轨迹、验收测试。若新项目仍能被你的模块结构容纳，说明你真正掌握了 Harness 的可迁移设计。

---

**结束语：** 上下文管理解决“模型看什么”；Agent Runtime 解决“下一步怎么走”；Tool System 和 Policy 解决“允许做什么”；Checkpoint 与事件流解决“怎么继续和追查”。它们合在一起，才构成一个可以长期运行、可控、可恢复的 Agent Harness。
