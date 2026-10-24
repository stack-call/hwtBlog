---
title: 第十一章：Tool System——模型说要运行工具之后发生了什么？
sidebar_label: 第十一章：Tool System
description: 从读取源码到工具注册、参数校验、执行路由、结果配对及错误隔离
sidebar_position: 12
tags: [Agent, Harness, Tools, Codex]
---

# 第十一章：Tool System——为什么不能直接执行模型返回的命令？

## 新故障：模型调用了一个不存在的工具

优惠券 Coding Agent 已能循环执行。但某次模型返回：

```json
{"name": "search_repository", "arguments": {"query": "refund"}}
```

而 Harness 只有 `grep`。小李临时写了一个 `if name == ...`。随后更多工具接入：`read_file`、`apply_patch`、`execute`、数据库查询、MCP 远程工具。`if/elif` 越长，错误越难排查。

我们先尝试统一登记每个工具：名称、描述、参数 Schema、执行器、并发属性和权限能力。

这里出现了第一层抽象：**Tool Registry（工具注册表）**。但它不负责决定是否批准高风险操作，也不应该直接拥有整个 Agent Loop。

## 第二个故障：名称正确，参数不合法

模型发起：`read_file({"path": 42})`。如果执行器直接把 `42` 当路径，错误可能在文件系统层才暴露，甚至造成不安全的隐式转换。

于是每次调用必须先走一条完整通道：

```text
Model Tool Call
   → 识别工具及版本
   → 校验输入 Schema
   → 检查权限与策略
   → 选择 Runtime / Backend
   → 执行、超时与取消
   → 标准化成功或错误结果
   → 使用 call_id 回传给模型
```

**Tool Definition**（向模型说明能调用什么）与 **Tool Execution**（真正发生的副作用）需要分开。模型看到的 JSON Schema 不是执行时权限的替代品。

## 第三个故障：工具成功了一半

Agent 要把 `refund.py` 中的修改应用到工作区。执行器输出：

```text
patch applied to coupon.py
patch failed at refund.py: context mismatch
```

如果 Harness 只返回 `success=True`，模型可能误以为两个文件都修改成功。工具结果必须区分 `completed / partial / failed / cancelled`，并保留足够证据供下一轮判断。

```python
# 教学接口：将执行状态与文本内容分离
@dataclass
class ToolResult:
    call_id: str
    status: str  # completed | partial | failed | cancelled
    output: str
    artifact_ref: str | None = None
```

这里还要考虑重复请求：因网络抖动重试一个查询通常问题不大；但重复执行“发放退款”就可能产生实际损失。重试策略应由工具语义决定，而不应所有异常一律重试。

## 真实代码：Codex 的 Registry 和 Dispatch

在 [Codex `tools/registry.rs`](https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/registry.rs) 中，真实类型的关键字段是：

```rust
pub struct ToolRegistry {
    tools: IndexMap<ToolName, RegisteredTool>,
    // ...
    pub(crate) tool_policy: Arc<ToolPolicy>,
}
```

这段节选说明注册表保存的是被登记的工具条目，不是由模型随意执行一段字符串。向下继续追踪 `dispatch_any_with_state(...)`，会看到 `ToolInvocation`、`ToolCallState` 和具体执行器之间的连接。

[Codex `tools/router.rs`](https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/router.rs) 还区分模型可见的 Tool Spec 与内部实际注册工具，并能判断工具是否支持并行。这样的设计让工具可以被延迟发现，减少每次 Prompt 暴露的工具数量。

## 为什么还需要工具结果标准化？

假设本地 `grep` 返回文本，远程 MCP 工具返回结构化内容，Shell 返回 stdout/stderr 与退出码。没有统一结果层，Agent Loop 必须认识所有工具实现，耦合越来越严重。

因此 Registry 负责“**有什么工具**”，Policy 负责“**能否运行**”，Runtime 负责“**如何运行**”，Result Adapter 负责“**怎么回传**”。

| 五个问题 | 回答 |
|---|---|
| 是什么 | 工具系统把模型的结构化调用转为受控执行。 |
| 为什么需要 | 统一名称解析、Schema、策略、路由和工具结果。 |
| 没有它 | `if/elif` 膨胀，参数错误晚暴露，工具之间权限和返回值不一致。 |
| 实际怎么做 | Registry + Validator + Policy + Executor + Result Normalizer。 |
| 处于哪里 | Agent Loop 与外部系统之间的边界。 |

### 实践验证

模拟三个工具：只读查询、写文件、退款请求。输入非法 JSON、未知工具和重复退款 `operation_id`，验证报错可读、写入不会越权、重复退款不会重复产生副作用。注意“幂等”需要外部服务配合，不能只靠本地 Registry。

**下一章的问题**：即使调用参数合法，让 Agent 在宿主机器直接执行 Shell 真的安全吗？
