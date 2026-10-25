---
title: 第十三章：Permission 与 Approval——Sandbox 之外还需要什么？
sidebar_label: 第十三章：权限与审批
description: 从自动退款事故推导权限策略、运行时强制检查与审批绑定
sidebar_position: 14
tags: [Agent, Harness, Security, Approval, Codex]
---

# 第十三章：Permission 与 Approval——Sandbox 之外还需要什么？

> 本章接续第十二章：Sandbox 解决了「能不能执行」，却没有解决「该不该执行」。Agent 完全可以在权限之内发起一笔真实退款。我们从这里推导权限策略、运行时强制检查，以及审批为什么必须绑定到具体参数。

## 事故：Sandbox 内的命令也可能不合业务要求

优惠券 Agent 在隔离环境里运行得很稳定。某次模型提出：为了验证退款规则，调用真实支付服务给一个订单退款。

这条调用可能完全满足网络和工具权限，但业务上绝不能无条件执行。

小李发现：**Sandbox 解决能力边界，Approval 解决风险承担与授权。** 两者不能互相替代。

## 第一版：模型自己决定是否危险

他写入 Prompt：`涉及付款、删除和外部发送，请先询问用户`。

这很容易漏掉间接操作。例如工具叫 `execute(command)`，命令内部通过脚本触发了外部退款。权限必须在真正执行前由代码强制，而非依赖模型自我约束。

小李把规则从 Prompt 移到 Harness：

```python
# 教学简化：策略决策应由受信任代码完成
decision = policy.evaluate(user, tool_name, args, workspace)
if decision == "deny":
    return ToolError("operation denied")
if decision == "approve":
    return await approval.wait_for_decision(tool_name, args)
return await runtime.execute(tool_name, args)
```

但立即出现一个竞争条件：Agent 在请求批准时展示的是“退款订单 A”，执行时参数却变成“订单 B”。所以批准必须绑定到**具体参数、调用 ID、操作范围和有效期**，不能给一个模糊的全局“同意”。

## 第二个事故：批准后仍然失败

用户批准“读取生产数据库中某条退款记录”。工具实际访问时沙箱拒绝网络连接。这并不是 Approval 失效，而是两个机制同时生效。

我们需要独立记录：审批结果、Sandbox 结果、实际执行结果。审批成功 ≠ 执行成功；Sandbox 允许 ≠ 已取得用户授权。

从产品体验看，应该把“请求用户提升权限”和“直接拒绝”区分开。如果确实不允许提升权限，没必要重复询问用户。

## 源码对照：Codex 的工具编排与 DeepAgents 的策略入口

阅读 [Codex `tools/orchestrator.rs`](https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/orchestrator.rs) 的 `run<Rq, Out, T>(...)`，其泛型工具执行与批准逻辑在一个受控通道内编排。再从工具处理器看它如何构建执行请求。

在 [DeepAgents `graph.py`](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/graph.py) 中，`create_deep_agent` 暴露 `permissions` 和 `interrupt_on`。`FilesystemMiddleware` 对内置文件工具执行规则检查，`HumanInTheLoopMiddleware` 用于需要人工决策的调用。**不要误以为这些规则自动约束用户直接调用 Backend 的所有操作；代码文档明确指出内置文件工具层的权限边界。**

这与 Codex 并不意味着同一套实现，而是两种系统都把操作策略放在执行边界附近。

## 第三个事故：批准请求被网络重试了两次

UI 重连后，旧批准消息再次到达。如果 Harness 直接把第二次“批准”当新操作，就可能重复退款。

因此审批是一个持久化状态转换：`requested → approved/rejected/expired → consumed`。一次审批应该有唯一 ID，消费动作要有审计记录；执行仍需独立幂等键。

```text
Tool Call → Policy Decision ┬→ allow → sandbox → execute
                            ├→ deny  → error
                            └→ ask   → durable interrupt
                                         ↓
                                     user decision
                                         ↓
                                  verify exact action
```

## 本章归位

| 五个问题 | 回答 |
| --- | --- |
| 是什么 | Policy 决定动作可否执行，Approval 将部分决策交给人类。 |
| 为什么需要 | 真实风险不只来自 OS 权限，也来自业务和用户授权。 |
| 没有它 | Prompt 被绕过后，Agent 可能执行未经授权的副作用。 |
| 实际怎么做 | 可信策略引擎 + 审批状态机 + 参数绑定 + 受控执行。 |
| 处于哪里 | Tool Registry 和 Runtime 之间，与 Sandbox 协同。 |

## 实践验证

模拟“查看退款信息”“修改测试文件”“发出真实退款”三种工具；验证允许、拒绝、人工审批。再故意修改审批前后的参数、重复提交审批、审批过期，确保无法执行非原定操作。

**下一章的问题**：安全边界建立后，一个长达几小时的修复任务，Agent 如何知道自己做到哪一步？
