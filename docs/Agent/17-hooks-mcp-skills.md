---
title: 第十七章：Hooks、MCP 与 Skills——为什么三者不是同一种东西？
sidebar_label: 第十七章：扩展机制
description: 从业务能力膨胀推导扩展点、工具协议与渐进式技能加载
sidebar_position: 18
tags: [Agent, Harness, Hooks, MCP, Skills]
---


# 第十七章：Hooks、MCP 与 Skills——Agent Harness 为什么需要扩展机制？

前面，我们已经构建了一个功能比较完整的 Coding Agent。

它能够读取文件、修改代码、运行测试、管理计划，并通过 Model Adapter 使用不同模型。

但当这个 Agent 真正进入企业开发环境后，小李发现：

每个团队都有自己的工具、流程和规范。

退款团队要求所有代码修改都留下审计记录。

业务团队有一套内部订单规则查询系统。

测试团队还有一份专门的退款回归测试操作手册。

如果每遇到一项新需求，就修改 Agent Loop 或 Tool System 的核心源码，整个 Harness 很快会变得难以维护。

我们需要允许外部团队扩展 Agent 的能力。

但扩展的方式应该是什么？

这一章，我们从三个问题逐步推导。

---

## 一、第一次需求：每次修改退款代码，都要记录审计日志

Coding Agent 正在执行：

```text
read_file("refund.py")
edit_file("refund.py", ...)
execute("pytest tests/test_refund.py")
```

团队希望记录：

```text
2026-10-25 10:00
Session: refund-task-001
Tool: edit_file
Status: completed
```

最直接的做法，是修改 `edit_file` 的实现：

```python
def edit_file(path, old, new):
    audit_log("edit_file started")

    result = do_edit(path, old, new)

    audit_log("edit_file completed")
    return result
```

这个方案没问题。

但过了一周，团队提出：

> 不只是 edit_file，所有工具执行都需要记录日志。

于是我们还得修改：

```text
read_file
write_file
execute
grep
git_diff
...
```

小李意识到：

**审计日志不是某个工具的业务功能，而是所有工具共享的执行行为。**

如果每个工具都单独实现，会产生大量重复代码。

### 能不能只改 Tool Dispatcher？

还记得第十一章的 Tool System 吗？

```python
async def dispatch(call):
    tool = registry.get(call.name)
    return await tool.execute(call.arguments)
```

所有工具最终都会经过 `dispatch()`。

那么我们可以这样修改：

```python
async def dispatch(call):
    audit_log("started", call)

    tool = registry.get(call.name)
    result = await tool.execute(call.arguments)

    audit_log("completed", call)

    return result
```

这一次，我们不用修改各个工具。

但新的问题又来了。

其他团队也提出了自己的需求：

- 安全团队：执行前检查工具参数。
- 数据团队：查询工具失败时有限重试。
- 测试团队：执行完补丁后通知测试系统。
- 平台团队：记录每次工具调用的耗时。

难道每增加一种需求，都要重新修改 `dispatch()`？

于是我们自然想到：

**让 Dispatcher 保持稳定，将这些逻辑作为可注册的执行扩展。**

这就引出了 Hook。

---

## 二、什么是 Hook？它究竟插入到哪里？

最简单的 Hook，就是在特定执行时机调用外部注册的函数。

例如：

```python
async def dispatch(call):
    await run_hooks("before_tool", call)

    result = await execute_tool(call)

    await run_hooks("after_tool", call, result)

    return result
```

现在：

```text
Tool Dispatcher
      |
      v
Before Tool Hooks
      |
      v
Actual Tool Execution
      |
      v
After Tool Hooks
      |
      v
Tool Result
```

安全团队可以注册一个检查 Hook。

审计团队可以注册一个日志 Hook。

平台团队可以注册一个耗时统计 Hook。

这些团队不需要修改工具本身。

### Hook 真正解决的问题是什么？

它解决的是：

> 在 Harness 已经定义好的执行时机，如何插入自定义行为，而不修改核心执行流程？

所以 Hook 通常需要两个东西：

**Hook Point**

系统允许扩展的位置。

例如：

```text
before_model
after_model
before_tool
after_tool
session_start
session_end
```

**Hook Handler**

扩展方注册的函数。

例如：

```python
def audit_tool_execution(event):
    ...
```

Hook Point 是 Harness 的一部分。

Hook Handler 是可扩展代码。

但是这里马上出现了一个新问题。

---

## 三、第二次事故：一个 Hook 报错，会发生什么？

假设我们注册了三个 Hook：

```text
Hook A：记录日志
Hook B：记录耗时
Hook C：通知测试平台
```

当 Coding Agent 修改文件时：

```text
before_tool
     |
     v
Hook A
     |
     v
Hook B
     |
     v
Hook C
```

结果 Hook C 的外部服务不可用。

那么，工具调用应该失败吗？

答案并不固定。

这取决于 Hook 的职责。

例如：

**审计指标上报失败**

我们可能允许工具继续执行，并记录观测错误。

**强制权限检查失败**

系统则可能需要拒绝执行。

于是我们发现：

**不是所有 Hook 都应该拥有相同的失败语义。**

一个成熟的 Hook System 需要考虑：

| 问题 | 为什么重要 |
|---|---|
| 执行顺序 | 多个 Hook 可能修改同一份数据 |
| 异常传播 | Hook 报错后是否继续 |
| 超时 | Hook 不能无限阻塞 Agent |
| 修改能力 | 能否修改工具参数或结果 |
| 阻断能力 | 能否阻止工具执行 |
| 可重入性 | 重试时 Hook 是否再次执行 |
| 生命周期 | 何时注册、何时移除 |

例如，有些 Hook 只能观察事件。

有些 Hook 能够改写输入。

有些 Hook 能够阻断执行。

这些能力不能因为都叫 Hook 就被视为等价。

### 三种常见扩展方式

**事件观察型**

```text
工具执行完成
      |
      v
发布 ToolCompleted Event
      |
      v
Listener 记录日志
```

事件处理者通常只观察已经发生的事情。

**执行拦截型**

```text
Tool Call
    |
    v
Before Hook
    |
    +--> Allow
    |
    +--> Reject
    |
    +--> Modify
```

执行之前，Hook 有机会影响后续操作。

**Middleware 包裹型**

```text
Middleware A
    |
    v
Middleware B
    |
    v
Tool Executor
    |
    v
Middleware B
    |
    v
Middleware A
```

Middleware 可以在执行前后同时工作。

因此：

> Hook 是在特定时机扩展执行逻辑的总思路；事件监听、执行拦截和 Middleware 包裹是不同的具体实现方式。

我们现在去看真实源码。

---

## 四、源码对照：OpenCode、DeepAgents、Codex 的 Hook 有什么区别？

### 4.1 OpenCode：Plugin Hooks

OpenCode 的插件接口非常适合研究 Hook。

源码：

[OpenCode Plugin Hooks](https://github.com/anomalyco/opencode/blob/dev/packages/plugin/src/index.ts)

其中定义了：

```typescript
"tool.execute.before"?: (
  input: {
    tool: string
    sessionID: string
    callID: string
  },
  output: { args: any },
) => Promise<void>
```

注意两个参数：

```text
input
output
```

`input` 提供本次工具调用的信息。

`output` 允许插件修改指定的可变数据。

例如：

```typescript
import type { Plugin } from "@opencode-ai/plugin"

export const RefundAudit: Plugin = async () => {
  return {
    "tool.execute.before": async (input, output) => {
      console.log("Tool started:", input.tool)

      // 可以在适当情况下修改 output.args
    },

    "tool.execute.after": async (input, output) => {
      console.log("Tool completed:", input.tool)
    },
  }
}
```

这是一个可参考的插件示例。

最值得注意的是：

**OpenCode 的 `tool.execute.before` 是专门的插件回调，不是普通 Event Bus 中的事件类型。**

这意味着：

```typescript
event: async ({ event }) => {
  if (event.type === "tool.execute.before") {
    // 这里不会实现预期的工具拦截
  }
}
```

不能替代上面的专用 Hook。

此外，OpenCode 的这个 Hook 可以修改参数，但不能据此假设所有版本都支持通过任意返回值直接中止工具调用。

如果需要可靠的强制权限控制，仍然应该使用实际的 Permission / Policy 执行边界。

### 4.2 DeepAgents：Middleware 组合

DeepAgents 主要复用 LangChain 的 Middleware 机制。

源码：

[LangChain Middleware Types](https://github.com/langchain-ai/langchain/blob/master/libs/langchain_v1/langchain/agents/middleware/types.py)

其中最重要的接口之一是：

```python
def wrap_tool_call(self, request, handler):
    return handler(request)
```

它和 OpenCode 的 `before/after` 很不一样。

为什么？

因为这里的 `handler(request)` 表示：

**继续执行被包裹的后续逻辑。**

我们可以写：

```python
from langchain.agents.middleware import AgentMiddleware


class ToolAuditMiddleware(AgentMiddleware):
    def wrap_tool_call(self, request, handler):
        tool_name = request.tool_call["name"]

        print("Start:", tool_name)

        try:
            return handler(request)
        finally:
            print("Finish:", tool_name)
```

此时，中间件可以：

```text
进入工具调用
     |
     v
执行审计逻辑
     |
     v
handler(request)
     |
     v
执行真实工具
     |
     v
执行完成后逻辑
```

它甚至可以不调用 `handler`，而直接返回符合协议的工具结果。

这就是 Middleware 相比普通事件监听更强的一点：

**它能够控制执行链是否继续。**

但仍要注意：对有副作用的工具，不能无条件多次调用 `handler`，否则可能重复修改外部系统。

### 4.3 Codex：Lifecycle Hooks

Codex 也支持生命周期 Hooks。

参考：

[Codex Hooks 文档](https://developers.openai.com/codex/hooks)

[Codex Hook Discovery 源码](https://github.com/openai/codex/blob/main/codex-rs/hooks/src/engine/discovery.rs)

当前 Hooks 包括：

```text
SessionStart
UserPromptSubmit
PreToolUse
PostToolUse
PreCompact
PostCompact
Stop
```

以及其他生命周期事件。

例如，`PreToolUse` 可以在工具执行前检查操作。

某些受支持的工具调用可以通过 Hook 返回明确的拒绝决定：

```json
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": "Operation blocked"
  }
}
```

它还可以在受支持的情况下修改工具输入。

这里与 OpenCode、DeepAgents 的区别很明显：

| 项目 | 主要扩展形式 |
|---|---|
| OpenCode | Plugin API 中的回调 |
| DeepAgents | LangChain Middleware 执行链 |
| Codex | 生命周期 Hook 配置和 Handler |

三个项目都支持执行扩展，但**阻断方式、执行顺序和错误语义不相同**。

尤其不要把 Hook 当作操作系统安全沙箱。

例如 Codex 的 Hook 文档就明确提醒：Hook 异常、超时或返回格式错误不一定会阻止工具执行。

因此对必须强制执行的安全策略，应优先使用有可靠执行保证的权限与沙箱机制。

---

## 五、第三次需求：Agent 需要查询公司的订单业务系统

现在退款团队提出新的需求：

> 退款计算规则不只存在于代码里。某些优惠券类型的退款约束保存在内部订单规则系统中。

我们的 Agent 需要查询：

```text
get_refund_policy(coupon_type)
```

但这个能力不在本地项目里。

订单团队拥有独立的业务服务。

小李最简单的方案是写一个 HTTP 工具：

```python
def get_refund_policy(coupon_type):
    response = requests.get(
        "https://internal.example.com/refund-policy",
        params={"coupon_type": coupon_type},
    )

    return response.json()
```

这完全可以工作。

但随后：

财务团队希望接入财务查询系统。

质量团队希望接入测试管理系统。

项目管理团队希望接入任务平台。

如果每接入一个服务，都要编写一份新的 SDK 适配代码和 Tool Schema 转换逻辑，维护成本会持续增长。

问题发生了变化：

**我们需要的已经不是一个新工具，而是一种让不同服务以统一方式向 Agent 暴露能力的协议。**

这就是 MCP 要解决的问题。

---

## 六、MCP 是什么？为什么它不等于普通 Tool？

MCP，全称 Model Context Protocol。

它是一种让 AI 应用与外部服务交换上下文、发现和调用能力的协议。

我们之前已经学习 Tool System：

```text
Agent Loop
    |
    v
Tool Registry
    |
    v
Tool Executor
```

现在希望支持外部服务：

```text
Agent Loop
    |
    v
Tool Registry
    |
    v
MCP Client
    |
    v
MCP Server
    |
    v
Order Policy Service
```

这里最重要的不是网络请求本身。

而是外部服务能够以协议规定的方式告诉 Agent：

> 我有哪些能力？每个能力需要什么参数？调用后返回什么结果？

### MCP 的三个角色

```text
            Host Application
          （Agent Harness）
                  |
                  |
          +-------+-------+
          |               |
          v               v
      MCP Client A    MCP Client B
          |               |
          v               v
      MCP Server A    MCP Server B
          |               |
          v               v
      Order System    Test Platform
```

**Host**

负责运行 Agent、管理工具暴露、用户权限和上下文。

**MCP Client**

负责与具体 MCP Server 建立协议通信。

**MCP Server**

对外提供工具、资源或提示模板等能力。

注意：

**MCP Server 通常不是一个自主运行的 Agent。**

它主要提供可调用的服务能力。

它也不应该默认获得 Agent 完整的对话历史。

Host 应控制向不同 MCP Server 提供哪些信息。

---

## 七、MCP 到底标准化了什么？

我们继续看订单系统。

过去，需要手动编写：

```python
registry.register(
    "get_refund_policy",
    custom_http_tool,
)
```

现在订单团队提供一个 MCP Server。

它暴露一个工具：

```text
get_refund_policy
```

MCP Client 可以通过标准协议发现这个工具。

### 7.1 Tool Discovery

Client 发送：

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/list",
  "params": {}
}
```

Server 返回工具定义。

为了突出核心结构，这里省略当前 MCP 规范所要求的部分协议元数据。

响应可以简化理解为：

```json
{
  "tools": [
    {
      "name": "get_refund_policy",
      "description": "查询优惠券退款规则",
      "inputSchema": {
        "type": "object",
        "properties": {
          "coupon_type": {
            "type": "string"
          }
        },
        "required": ["coupon_type"]
      }
    }
  ]
}
```

现在 Harness 知道外部服务提供什么工具。

注意：

**发现一个工具，不代表模型一定能使用它。**

还需要由 Host 决定：

```text
是否注册？
是否向当前模型暴露？
当前用户是否具有执行权限？
```

### 7.2 Tool Invocation

当模型提出：

```text
get_refund_policy("full_reduction")
```

Tool Runtime 通过 MCP Client 发送：

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "tools/call",
  "params": {
    "name": "get_refund_policy",
    "arguments": {
      "coupon_type": "full_reduction"
    }
  }
}
```

同样，这里省略部分协议元数据。

MCP Server 再去查询真实业务系统，返回结果。

最后，由 Harness 将结果转换成模型可使用的 Tool Result。

完整过程是：

```text
LLM
 |
 | get_refund_policy(...)
 v
Agent Tool Runtime
 |
 v
MCP Client
 |
 | tools/call
 v
MCP Server
 |
 v
Order Service
 |
 v
Policy Data
 |
 v
MCP Tool Result
 |
 v
Agent Loop
```

到这里，我们能清楚地区分：

**Tool System 是 Harness 内部的工具执行机制。**

**MCP 是连接外部能力的标准协议。**

MCP 工具最终仍然需要经过 Harness 的工具注册、权限和执行处理。

### 7.3 MCP 不只有 Tools

MCP 还定义了其他能力：

| 能力 | 作用 | 例子 |
|---|---|---|
| Tools | 执行操作或查询 | 查询退款规则 |
| Resources | 提供可读取的上下文资源 | 退款规则文档 |
| Prompts | 提供参数化的提示模板 | 退款分析模板 |

这三个概念不能完全混用。

例如：

```text
get_refund_policy()
```

是主动查询工具。

而：

```text
refund-policy://current
```

可以被设计成一个 Resource URI，供客户端读取对应资源。

MCP 规范定义了这些能力，但具体 Harness 不一定以完全相同的 UI 或工具形式暴露全部能力。

---

## 八、源码对照：OpenCode 和 Codex 如何接入 MCP？

### 8.1 OpenCode：将外部 MCP Tool 转换成模型可调用的 Tool

OpenCode 官方支持本地和远程 MCP Server。

文档：

[OpenCode MCP Servers](https://opencode.ai/docs/mcp-servers/)

在 OpenCode 仓库的 MCP 工具转换实现中，可以看到：

[OpenCode MCP Catalog](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/mcp/catalog.ts)

其中一个关键调用是：

```typescript
const result = await client.callTool(
  {
    name: mcpTool.name,
    arguments: args,
  },
  // ...
)
```

这是根据源码简化的关键片段。

为什么需要 `convertTool()` 这样的转换？

因为 MCP 提供的是 MCP Tool Definition。

而 OpenCode 内部的模型调用与 Tool Runtime，需要使用自己的工具表示方式。

于是：

```text
MCP Tool Definition
         |
         v
     convertTool()
         |
         v
OpenCode Internal Tool
         |
         v
  Model Tool Schema
```

这恰好连接了第十一章研究的 Tool Registry。

值得注意的是，OpenCode 同时存在不同代际的 MCP 实现。这里引用的是仓库中对应路径的实现，不能直接套用到所有 V2 版本。

### 8.2 Codex：MCP Client 层

Codex 也有独立的 MCP 集成。

源码：

[Codex MCP Client](https://github.com/openai/codex/blob/main/codex-rs/codex-mcp/src/rmcp_client.rs)

这个模块负责处理 MCP 服务连接、工具发现和相关通信能力。

为什么不直接把 MCP 网络通信写进 Agent Loop？

因为这些操作属于不同层次：

```text
Agent Loop
  决定何时调用工具

Tool Registry
  识别并分发工具

MCP Client
  处理与 MCP Server 的协议通信

MCP Server
  执行实际外部能力
```

它们的生命周期也不同。

一个 MCP Server 连接可能服务多个 Tool Call。

因此没有必要每次调用模型时都重新实现 MCP 初始化和发现逻辑。

### MCP 连接后还需要考虑什么？

比如：

```text
Server A -> get_order
Server B -> get_order
```

两个 MCP Server 都提供同名工具。

Harness 应该避免工具命名冲突。

另外还要考虑：

- Server 断线与重连。
- Tool List 更新。
- 工具超时与取消。
- 用户和服务端身份认证。
- 工具结果中的敏感信息。
- 大量 Tool Schema 占用上下文。

尤其最后一点很容易忽略。

假设接入五个 MCP Server，总共暴露一百多个工具。

模型每次请求都看到全部工具定义，Token 成本会很高。

所以 MCP Tool Discovery 和 Model Tool Exposure 不能完全画等号。

Host 需要根据当前任务选择真正应该暴露的工具集合。

---

## 九、第四次事故：有了 MCP，Agent 还是不知道退款团队的工作规范

现在，Coding Agent 已经能通过 MCP 查询退款规则。

但小李发现，Agent 的工作流程仍然不稳定。

例如一次修复过程：

```text
1. 修改 refund.py
2. 运行一个测试
3. 输出完成
```

退款团队却要求：

```text
1. 先确认实际支付金额和优惠券分摊规则
2. 检查普通退款与部分退款
3. 修改代码
4. 添加回归测试
5. 确认原有 API 没有变化
6. 输出验证证据
```

为什么有了 MCP 还会出现这个问题？

因为：

**MCP 解决的是 Agent 能调用什么能力，不负责教 Agent 如何完成某类业务任务。**

MCP Server 可以提供：

```text
get_refund_policy()
```

但它不自动保证 Agent 会在修改代码之前调用这个工具，也不保证 Agent 按团队规定执行验证。

小李最初把这些流程写进 System Prompt。

但随着团队增加，System Prompt 越来越长。

最终变成：

```text
退款开发规范
订单开发规范
支付开发规范
数据库开发规范
发布流程
测试流程
代码审查规范
...
```

即使当前任务只是修复退款 Bug，模型也要阅读大量无关内容。

这显然不理想。

小李开始思考：

> 能不能把特定任务的流程保存成独立文件，需要时才交给 Agent？

这就是 Skills 的核心思路。

---

## 十、Skill 究竟是什么？

Skill 可以理解为：

**面向特定任务的可复用工作说明与配套资源。**

例如我们创建：

```text
skills/
  coupon-refund/
    SKILL.md
    references/
      refund-rules.md
    scripts/
      verify_refund.py
```

其中最重要的是：

```text
SKILL.md
```

它可以这样写：

```markdown
---
name: coupon-refund
description: Use when fixing coupon refund calculations,
  coupon discount allocation, or partial-refund bugs.
---

# Coupon Refund Skill

## Workflow

1. Locate the coupon allocation and refund calculation code.
2. Read the current refund policy.
3. Identify the actual paid amount and refund allocation rules.
4. Make the smallest change consistent with the user's goal.
5. Add regression tests for full and partial refunds.
6. Run the relevant tests.
7. Report code changes and verification evidence.

## Constraints

- Do not change public APIs without user authorization.
- Do not assume that the original price equals the paid amount.
- Do not declare success without checking test results.

## References

Read `references/refund-rules.md` when business rules are unclear.
```

这是我们为教学场景编写的示例 Skill，不是任何开源项目内置的退款规则。

这里有三个重要部分。

**Metadata**

告诉 Agent 这个 Skill 是什么、什么时候适用。

**Instructions**

告诉 Agent 应该如何处理这一类任务。

**Supporting Resources**

提供参考文档、脚本和模板。

### Skill 与普通 Prompt 有什么区别？

普通 System Prompt 通常在模型请求时直接加入上下文。

而 Skill 可以先只提供简短的元信息。

当任务真正需要时，再加载完整内容。

例如：

```text
Agent 当前任务：
  修复优惠券退款 Bug
          |
          v
发现 coupon-refund Skill
          |
          v
读取 SKILL.md
          |
          v
按照退款工作流程执行
```

这就是 **Progressive Disclosure（渐进式披露）**。

核心目的：

**不要让模型在每次执行任务时，都携带所有领域的操作手册。**

---

## 十一、源码对照：DeepAgents、OpenCode、Codex 如何加载 Skills？

### 11.1 DeepAgents：SkillsMiddleware

源码：

[DeepAgents skills.py](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/middleware/skills.py)

里面有：

```python
class SkillsMiddleware(
    AgentMiddleware[SkillsState, ContextT, ResponseT]
):
    ...
```

最重要的不是这个类名，而是它的加载机制。

它使用 Backend 读取 Skills，并将可用 Skill 的元信息加入模型上下文。

当 Agent 判断某个 Skill 与当前任务相关时，可以读取完整 Skill 内容。

逻辑上：

```text
Backend Storage
      |
      v
SkillsMiddleware
      |
      v
Skill Metadata
      |
      v
LLM
      |
      | 发现相关 Skill
      v
read_file(SKILL.md)
      |
      v
Full Skill Instructions
      |
      v
LLM
```

这样就把 Skill Discovery 与 Skill Loading 分开了。

DeepAgents 当前还有一个值得研究的设计：

`SKILL.md` 可以通过 `metadata.include_tools` 关联某些工具，相关工具在 Skill 被读取或固定加载后再披露给模型。

这比单纯把 Skill 内容加入 Prompt 更进一步。

但注意：

**披露工具不意味着绕过权限。**

工具真正执行时仍然要经过相应的 Tool Runtime 和授权控制。

### 11.2 OpenCode：Skill Tool

OpenCode 也支持通过 `SKILL.md` 定义可复用工作流程。

官方文档：

[OpenCode Agent Skills](https://opencode.ai/docs/skills/)

源码：

[OpenCode skill.ts](https://github.com/anomalyco/opencode/blob/dev/packages/core/src/tool/skill.ts)

当前实现有一个显式的：

```typescript
export const name = "skill"
```

工具。

它的输入 Schema 可以简化为：

```typescript
{
  name: string
}
```

模型调用：

```text
skill("coupon-refund")
```

后，工具加载对应 Skill 内容并返回给模型。

其实际输出构造包含：

```typescript
`<skill_content name="${skill.name}">`
```

以及 Skill 内容和目录信息。

这说明：

**在 OpenCode 的设计中，加载 Skill 本身也可以被建模成一次 Tool Call。**

而且 Skill Tool 会使用 Permission Service 检查是否允许读取对应 Skill。

这是一个很重要的边界：

Skill 被发现，不等于用户一定授权加载或使用其中的所有资源。

### 11.3 Codex：Skills 与渐进式加载

Codex 也使用 `SKILL.md` 描述可复用任务流程。

参考：

[Codex Skills 文档](https://developers.openai.com/codex/skills)

Codex 通常会先向模型提供可用 Skill 的名称、描述和位置。

当任务需要时，再读取完整 Skill。

Skill 还可以包含：

```text
SKILL.md
scripts/
references/
assets/
```

这里我们看到三者的共同思路：

```text
发现 Skill
    |
    v
判断是否相关
    |
    v
加载说明
    |
    v
使用现有工具执行工作
```

但具体实现形式并不相同。

DeepAgents 偏向 Middleware 与 Backend。

OpenCode 提供显式 Skill Tool。

Codex 将 Skill 发现和使用融合在自身的指令与运行机制中。

---

## 十二、一个特别重要的问题：Skill 能不能直接执行代码？

假设我们的 Skill 里包含：

```text
scripts/verify_refund.py
```

模型阅读后，决定运行：

```bash
python scripts/verify_refund.py
```

这里要问：

**是 Skill 自己执行了 Python 脚本吗？**

不是。

执行链仍然是：

```text
Skill Instructions
       |
       v
LLM 决定运行脚本
       |
       v
Tool Call
       |
       v
Permission Check
       |
       v
Execution Environment
       |
       v
Python Process
```

这说明：

**Skill 主要是知识与流程扩展，不是执行权限扩展。**

当然，一个 Skill 可以引导 Agent 调用工具，也可以携带脚本。

但最终执行仍然需要已有工具和对应权限。

例如：

```text
SKILL.md：
  修复完成后运行 verify_refund.py
```

它不能自动授予：

```text
读取生产数据库
修改用户目录
执行不受限制的 Shell
```

这些能力。

### 为什么这个区别特别重要？

因为 Skill 文件可能来自外部仓库或第三方作者。

如果把其中的任意说明都当成高优先级系统指令，就可能让 Skill 成为绕过安全控制的渠道。

因此：

**Skill 内容应按其来源和信任级别处理，不能天然覆盖系统权限策略。**

---

## 十三、终于可以把 Hooks、MCP 和 Skills 连在一起了

现在回到我们的优惠券 Coding Agent。

用户提出：

> 修复部分退款金额错误，遵守团队退款规范，并验证结果。

系统中已经有：

**一个 Hook**

负责记录工具执行审计日志。

**一个 MCP Server**

负责查询内部退款规则。

**一个 Skill**

负责告诉 Agent 按照什么顺序调查、修改和测试。

完整流程：

```text
User Task
    |
    v
Agent Loop
    |
    v
发现 coupon-refund Skill
    |
    v
读取 SKILL.md
    |
    v
模型了解退款修复流程
    |
    v
决定查询实际退款规则
    |
    v
MCP Tool Call
    |
    v
Before Tool Hook
    |
    v
Permission Check
    |
    v
MCP Client
    |
    v
Order Policy MCP Server
    |
    v
业务规则结果
    |
    v
After Tool Hook
    |
    v
Agent Loop
    |
    v
修改代码、运行测试
    |
    v
验证与交付
```

注意，这里的 Hook 与权限检查顺序是教学逻辑图。

实际执行顺序由具体 Harness 决定，不能假设所有框架都按照图中顺序运行。

现在，我们可以给三个机制一个非常准确的定位：

| 机制 | 核心问题 |
|---|---|
| Hook | 在执行的哪个时机插入扩展逻辑？ |
| MCP | 如何通过标准协议接入外部能力？ |
| Skill | 怎样让 Agent 学会完成某类任务？ |

这三个问题并不相同。

---

## 十四、为什么它们不能互相替代？

### 14.1 用 Hook 代替 MCP？

如果只是记录日志、修改参数，Hook 很合适。

但如果要让 Agent 主动查询订单业务规则，单纯添加一个执行前 Hook 就不够自然。

Agent 需要的是一个可以自主调用的能力。

MCP Tool 更合适。

### 14.2 用 Skill 代替 MCP？

Skill 可以写：

```text
查询内部退款规则。
```

但如果没有对应的 Tool，Agent 仍然无法访问业务系统。

Skill 只能说明怎么做，不能凭空创造外部执行能力。

### 14.3 用 MCP 代替 Skill？

MCP Server 可以提供：

```text
get_refund_policy
```

但不一定知道：

```text
先检查优惠券分摊
再修改退款计算
再验证部分退款
```

这属于任务流程知识。

Skill 更合适。

### 14.4 用 Skill 代替 Hook？

Skill 可以提示：

```text
每次修改文件后记录日志。
```

但模型可能忘记执行。

如果日志记录是系统级要求，就应该放在更可靠的执行扩展或审计基础设施里。

这就是核心区别：

**Prompt 中的工作建议，不等于 Runtime 强制执行的逻辑。**

---

## 十五、如果自己实现 Agent Harness，扩展层应该怎么设计？

现在我们设计一个最小扩展架构。

```text
                 Agent Runtime
                       |
         +-------------+-------------+
         |             |             |
         v             v             v
    Hook Manager    MCP Manager   Skill Manager
         |             |             |
         v             v             v
    Lifecycle      Tool Discovery  Skill Discovery
    Extensions         |             |
         |             v             v
         |         External Tools  Instructions
         |             |             |
         +-------------+-------------+
                       |
                       v
                   Agent Loop
                       |
                       v
                   Tool Runtime
                       |
                       v
               Execution Environment
```

这是一张职责划分图，不代表所有框架都需要实现这些同名模块。

### 15.1 Hook Manager

核心职责：

```text
注册 Hook
匹配执行时机
控制执行顺序
处理 Hook 异常
管理超时和生命周期
```

### 15.2 MCP Manager

核心职责：

```text
连接 MCP Server
发现 Tools / Resources / Prompts
管理协议版本和能力
维护 Tool 映射
处理连接异常
控制访问权限
```

### 15.3 Skill Manager

核心职责：

```text
发现 SKILL.md
解析 Metadata
匹配当前任务
按需加载内容
管理来源和版本
控制加载权限
```

三个模块可以独立演进。

如果未来团队增加一个新的业务 Skill，不应该要求修改 Agent Loop。

如果增加一个新的 MCP Server，也不应该要求重新实现工具调度。

如果新增一个审计 Hook，则不应该修改所有工具的源码。

这才是扩展机制真正带来的架构价值。

---

## 十六、一个更难的问题：扩展机制会产生哪些新的风险？

扩展提高了灵活性，但也扩大了系统边界。

### Hook 的风险

Hook 可能：

- 修改工具参数。
- 阻塞执行。
- 产生额外副作用。
- 在失败时影响 Agent。
- 重复执行导致重复操作。

因此需要明确执行权限、失败语义和可观测性。

### MCP 的风险

MCP Server 可能：

- 暴露敏感业务能力。
- 返回带有恶意指令的文本。
- 使用不可信的工具描述。
- 连接未经授权的外部系统。
- 返回大量数据挤占上下文。

因此需要认证、最小权限、输入输出校验，以及对外部数据保持清晰的信任边界。

特别注意：

**MCP Tool Result 属于外部数据，不应该因此获得系统指令的优先级。**

### Skill 的风险

Skill 可能：

- 包含过期的业务规则。
- 引导 Agent 运行不可信脚本。
- 要求访问不必要的数据。
- 与用户当前任务冲突。
- 因描述不准确被错误触发。

因此需要版本管理、来源检查和实际任务验证。

另外，Skill 中引用的脚本也应在受控执行环境中运行。

### 一个共同的安全原则

**扩展组件可以增加 Agent 的工作方式，但不应该天然扩大它的授权范围。**

尤其不能因为某段文字来自 Skill 或 MCP Tool，就让它绕过 Permission、Sandbox 或用户授权。

---

## 十七、如何验证这些扩展机制真的有效？

继续使用退款 Bug 场景。

### Hook 测试

我们希望确认：

- 修改文件时，审计 Hook 被调用。
- Hook 收到正确的 Session 和 Tool Call 标识。
- 工具执行失败时，相关结果仍被记录。
- 审计 Hook 异常不会被误认为工具正常成功。
- 必须强制执行的策略不会依赖一个可能静默失败的 Hook。

### MCP 测试

我们希望确认：

- 可以连接订单规则 MCP Server。
- 能够正确发现 `get_refund_policy`。
- 模型能使用正确的 Tool Schema。
- 调用结果能与原始 Tool Call 关联。
- Server 断线后得到明确错误。
- 未授权的用户无法获取敏感退款数据。
- 工具列表更新或缓存过期后不会长期暴露错误能力。

### Skill 测试

我们希望确认：

- 退款任务能发现 `coupon-refund` Skill。
- 无关任务不会加载退款 Skill。
- 读取 Skill 后，模型能按照流程工作。
- Skill 不会自动获得额外执行权限。
- Skill 中的过期规则不会覆盖当前业务系统返回的权威数据。
- 最终结果有代码和测试证据，而不只是模型声称遵循了 Skill。

注意最后一点。

**Skill 被成功加载，不代表 Agent 成功完成了任务。**

评估仍然应该以真实任务结果为准。

---

## 十八、三个开源 Harness 的扩展设计对比

| 关注点 | DeepAgents | OpenCode | Codex |
|---|---|---|---|
| 生命周期扩展 | LangChain Middleware | Plugin Hooks | Lifecycle Hooks |
| 工具拦截 | `wrap_tool_call` | `tool.execute.before/after` | `PreToolUse/PostToolUse` |
| 外部工具协议 | 可通过 LangChain MCP 集成 | 内置 MCP 接入 | 内置 MCP Client |
| Skills | `SkillsMiddleware` | `skill` Tool | Skill 发现与加载 |
| Skill 资源访问 | Backend | 本地 Skill Service / Tool | Skill 路径与工具 |
| 主要设计风格 | Middleware 组合 | 插件和服务扩展 | Runtime 生命周期扩展 |

三个项目的共同点是：

**都不希望每增加一种外部能力，就修改核心 Agent Loop。**

但它们提供扩展的方式不同。

DeepAgents 更强调 Middleware 与 Backend 的组合。

OpenCode 更强调 Plugin、MCP 与 Skill Tool。

Codex 则把生命周期 Hooks、MCP Client 和 Skills 纳入自己的 Runtime 执行体系。

这些不同方式分别体现了不同的架构取舍，并不能简单判断哪一种一定最好。

---

## 十九、本章的五个核心问题

### 1. Hooks、MCP、Skills 分别是什么？

Hooks 是执行生命周期扩展机制。

MCP 是连接外部能力的协议。

Skills 是任务知识与操作流程的可复用载体。

### 2. 为什么需要它们？

因为真实 Agent 必须适配不同团队的执行要求、外部工具和工作规范。

如果所有需求都进入 Agent 核心代码，系统会快速变得难以维护。

### 3. 没有它们会怎样？

简单 Agent 仍然可以工作。

但随着集成数量增长，会出现大量重复代码、耦合严重、Prompt 膨胀、外部能力难以复用等问题。

### 4. 实际怎么做？

将三个扩展职责分开：

```text
Lifecycle Extension -> Hooks

External Capability -> MCP

Task Knowledge -> Skills
```

并让它们通过 Agent Loop、Tool System 与 Context Manager 协作。

### 5. 在整个 Harness 中处于什么位置？

它们不是一套替代 Agent Loop 的新运行时。

而是分别扩展：

```text
Agent Runtime
Tool System
Context / Instructions
```

真正的工具执行、权限判断与任务生命周期，仍然由 Harness 管理。

---

## 二十、独立验证：把这套扩展机制迁移到另一个 Agent

假设现在开发一个财务分析 Agent。

用户要求：

> 查询上个月的异常费用，生成报告，并按照财务团队流程完成验证。

我们不需要重新设计整个 Harness。

只需要判断三类需求。

**第一类：每次数据库查询都要审计**

适合 Hook 或 Middleware。

**第二类：访问财务数据库和报表平台**

适合通过 MCP 或其他标准化工具接口连接。

**第三类：遵守财务异常分析流程**

适合通过 Skill 提供操作说明与检查规则。

最后，再由 Agent Loop 控制执行，Tool System 负责工具调用，Permission System 判断授权，Execution Environment 负责实际操作。

能够将这种结构迁移到其他领域，才说明我们真正理解了这三类机制。

---

## 本章总结

最初，我们拥有一个已经能够执行任务的 Agent。

但每增加一个团队需求，就需要修改核心代码。

于是，我们从三类工程问题中逐步推导出了三种扩展机制：

```text
需要扩展执行行为
       |
       v
      Hooks


需要访问外部系统
       |
       v
       MCP


需要复用任务经验
       |
       v
      Skills
```

它们最终构成 Agent Harness 的可扩展能力体系。

最重要的结论是：

> **Hooks 扩展执行流程，MCP 扩展可访问的外部能力，Skills 扩展 Agent 对特定任务的知识与操作方法。**

三者既可以独立使用，也可以组合使用。

但它们不能互相替代，更不能自动取代 Permission、Sandbox 和真实任务验证。

## 真实源码阅读路线

### DeepAgents / LangChain

1. [LangChain Middleware Types](https://github.com/langchain-ai/langchain/blob/master/libs/langchain_v1/langchain/agents/middleware/types.py)
   - 重点：`wrap_tool_call`、`wrap_model_call`、`handler(request)`。

2. [DeepAgents SkillsMiddleware](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/middleware/skills.py)
   - 重点：Skill Discovery、Metadata、按需加载。

3. [DeepAgents graph.py](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/graph.py)
   - 重点：Middleware 如何接入 Agent。

### OpenCode

1. [Plugin Hooks](https://github.com/anomalyco/opencode/blob/dev/packages/plugin/src/index.ts)
   - 重点：`tool.execute.before`、`tool.execute.after`。

2. [MCP Catalog](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/mcp/catalog.ts)
   - 重点：`convertTool()`、`client.callTool()`。

3. [Skill Tool](https://github.com/anomalyco/opencode/blob/dev/packages/core/src/tool/skill.ts)
   - 重点：Skill 如何被加载并作为工具结果返回。

4. [MCP Servers 文档](https://opencode.ai/docs/mcp-servers/)
   - 重点：本地、远程 MCP 服务器及工具暴露。

### Codex

1. [Hooks 官方文档](https://developers.openai.com/codex/hooks)
   - 重点：Hook Event、Matcher、阻断与输入改写。

2. [Hook Discovery](https://github.com/openai/codex/blob/main/codex-rs/hooks/src/engine/discovery.rs)
   - 重点：Hook 配置加载与匹配。

3. [Codex MCP Client](https://github.com/openai/codex/blob/main/codex-rs/codex-mcp/src/rmcp_client.rs)
   - 重点：MCP 工具发现和客户端管理。

4. [Codex Skills 文档](https://developers.openai.com/codex/skills)
   - 重点：Skill Metadata、SKILL.md 与渐进式加载。

### MCP 标准

- [MCP Architecture](https://modelcontextprotocol.io/specification/2026-07-28/architecture)
- [MCP Tools](https://modelcontextprotocol.io/specification/2026-07-28/server/tools)
- [MCP Resources](https://modelcontextprotocol.io/specification/2026-07-28/server/resources)

阅读持续更新的源码时，应固定具体 Commit，以免混用不同版本的 API。

---

**下一章：Observability & Tracing——Agent 已经能执行复杂任务，为什么失败后仍然很难解释它究竟做错了哪一步？**

我们的优惠券 Coding Agent 某次执行了 80 次工具调用、切换了两次模型、加载了三个 Skills，最后测试仍然失败。

用户只看到一句“修复失败”。

但开发者需要知道：

- 哪一步开始偏离目标？
- 哪一次模型调用产生了错误工具参数？
- 哪个 Hook 改写了工具请求？
- MCP 调用为何超时？
- Context Compaction 是否丢失了关键约束？
- 失败到底来自模型决策还是执行环境？

这些问题将引出 Agent Harness 的 Event、Span、Trace、Metrics 和 Replay 设计。
