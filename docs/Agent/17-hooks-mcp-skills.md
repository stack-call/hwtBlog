---
title: 第十七章：Hooks、MCP 与 Skills——Harness 如何扩展？
sidebar_label: 第十七章：Hooks、MCP 与 Skills
description: 从业务能力膨胀推导扩展点、工具协议与渐进式技能加载
sidebar_position: 18
tags: [Agent, Harness, Hooks, MCP, Skills]
---

# 第十七章：Hooks、MCP 与 Skills——为什么三者不是同一种东西？

## 事故：每个团队都要往 Agent Loop 里加一个 if

退款团队想在每次修改代码后扫描安全问题；测试团队想新增一个数据库检查工具；支付团队想让 Agent 按固定规范生成退款复盘。

小李发现，三个需求都叫“扩展 Agent”，但它们真正修改的对象不同。

先看每个需求：

- “写完文件后自动扫描”关注**生命周期时机**；
- “调用支付服务查询订单”关注**外部可执行能力**；
- “生成规范化复盘”关注**任务领域知识与工作流说明**。

于是系统自然分化出三种机制：**Hooks / MCP / Skills**。

## 第一次改进：Hooks 管理生命周期

在执行工具前后，Harness 提供可注册的钩子。例如 `before_tool` 可以检查操作，`after_tool` 可以触发补充验证。

```python
# 教学代码：强调事件边界
async def execute_tool(call):
    await hooks.emit("before_tool", call)
    result = await runtime.execute(call)
    await hooks.emit("after_tool", {"call": call, "result": result})
    return result
```

紧接着就会出现两个设计问题：Hook 失败是否阻止原操作？多个 Hook 的顺序如何确定？安全相关的验证如果允许 `fail_open`，会产生风险；但日志上传失败通常不应该让用户的代码修改失败。**Hook 的错误策略必须按用途区分**。

真实世界的生命周期 Hooks 可能是脚本、插件函数或远程调用，而不仅仅是上面的 Python 回调。

## 第二次改进：MCP 扩展外部工具

退款查询是另一个进程甚至另一家公司提供的能力。硬编码 HTTP API 到 Harness 里会导致注册、鉴权、工具描述和错误转换重复实现。

**MCP（Model Context Protocol）**为工具和资源发现与调用提供标准化接口。Agent 不应把“某个 MCP Server 宣称自己有权限”当作足够的执行授权；本地工具策略仍需决定是否暴露、允许使用哪些连接和结果。

```text
Agent Loop → Tool Registry → MCP Client → MCP Server → 外部系统
                 ↑
          描述/Schema/权限策略
```

MCP 解决的是工具和资源连接，**不等于 A2A**：A2A 更偏向独立 Agent 间任务生命周期协作。

## 第三次改进：Skills 按需加载领域知识

“退款复盘报告必须包含折扣分摊、幂等、舍入规则”不是一个新执行工具。小李最初把几百页业务规范塞进 System Prompt，引起 Context 增长。

随后改用 Skills：默认只展示技能元信息；模型在需要时再读取对应 `SKILL.md` 与辅助文件。这就是 **Progressive Disclosure（渐进披露）**。

[DeepAgents `middleware/skills.py`](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/middleware/skills.py) 明确通过 Backend 加载 Skill，并把技能索引注入模型可见信息；正文按需读。来源还可以有先后顺序，后面的同名 Skill 覆盖前面的同名项。

而 [DeepAgents `graph.py`](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/graph.py) 展示 `SkillsMiddleware` 和其他 Middleware 的装配位置：这也提醒我们，**扩展点的执行顺序会影响模型实际看到的内容**。

## 三者该怎样分工？

| 机制 | 扩展了什么 | 典型例子 | 不负责什么 |
|---|---|---|---|
| Hooks | 运行时生命周期 | 文件改动后触发检查 | 不天然提供外部工具目录 |
| MCP | 工具/资源连接能力 | 订单查询服务 | 不管理独立 Agent 的任务生命周期 |
| Skills | 专业知识与流程说明 | 退款复盘规范 | 不自动提供安全执行权限 |

真正的生产系统可能组合三者：先从 Skill 了解流程，经 MCP 调用查询工具，再由 Hook 对结果执行审计。但这些能力都应受 Policy、Context Budget 与可观测性机制约束。

| 五个问题 | 回答 |
|---|---|
| 是什么 | 三种不同扩展：生命周期、工具协议、任务知识。 |
| 为什么需要 | 避免所有新需求都修改 Agent Loop 核心。 |
| 没有它 | 运行时代码膨胀、连接重复造轮子、Prompt 无限制增长。 |
| 实际怎么做 | Hook Registry、MCP Client/Server、Skill Catalog + 按需读取。 |
| 处于哪里 | Harness 扩展层，横跨工具执行与上下文组装。 |

### 实践验证

要求不修改 Agent Loop，只通过配置新增：(1) 修改文件后记录审计事件；(2) 一个返回模拟退款明细的 MCP 工具；(3) 一个退款复盘 Skill。关闭各扩展后，核心循环应保持相同行为。

**下一章的问题**：现在系统能扩展，但线上任务失败时，我们究竟怎么知道失败发生在哪一层？
