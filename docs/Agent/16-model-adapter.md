---
title: 第十六章：Model Adapter——更换模型后，Agent 为什么坏了？
sidebar_label: 第十六章：Model Adapter
description: 从多模型输出差异推导统一消息、工具事件、流式解析与能力约束
sidebar_position: 17
tags: [Agent, Harness, Model-Adapter, OpenCode, Codex]
---

# 第十六章：Model Adapter——更换模型后，Agent 为什么坏了？

> 本章接续第十五章：运行时已经能暂停和恢复，却仍然和某一家模型 SDK 绑死。我们从「换了模型，工具调用格式就变了」开始，推导统一事件、能力描述与流式解析这层适配边界。

## 事故：原来识别的工具调用格式不见了

小李最初只接一个模型 API，直接把其 JSON 响应传给 Agent Loop。半年后他想让退款 Agent 使用另一个模型：新的 SDK 把工具调用分成流式片段，结束标记也和原先不同。

结果 Agent Loop 误把半截 JSON 当成完整参数，调用 `read_file` 时出现解析错误。

问题不是工具系统，而是**不同模型 Provider 的交互协议泄露到了运行时核心**。

## 第一层改进：把 Provider 响应变成统一事件

小李在 Agent Loop 与厂商 SDK 中间放一个 Model Adapter：

```text
Agent Loop ← Canonical Events ← Adapter ← Provider SDK
                       ↑
          text_delta / tool_call_delta / tool_call_done /
          usage / completed / error
```

只要 Agent Loop 认识统一事件，就不用了解各个 Provider 的原始 JSON 字段。

```python
# 教学类型：不是 OpenCode/Codex 的原样 API
@dataclass
class ToolCallComplete:
    call_id: str
    name: str
    arguments: dict

@dataclass
class ModelFinish:
    reason: str   # final | tool_calls | length | error
```

但仅仅归一化字段还不够。不同模型可能不支持并行 Tool Call、特定图片格式、系统消息角色，或者不同的结构化输出模式。Adapter 还需声明**Capabilities（能力描述）**，在构造请求时主动选择兼容策略。

## 第二层故障：流式消息里 JSON 还没闭合

模型输出可能先发送：

```text
chunk 1: {"path": "refund
chunk 2: .py"}
```

如果每个 chunk 都直接交给 Tool Registry 执行，就会出现半成品调用。Harness 必须把流式传输与调用提交分开：流式片段可以实时展示，但执行器只接受被确认完成且通过校验的结构化参数。

类似地，文本流可向 UI 提前展示，但最终状态应等 Provider 的完成事件才能确定。

## 第三个故障：重试让工具重复执行

模型流式连接断了，小李重新请求。新请求可能重新生成已有 Tool Call，也可能提供与上次不同的内容。**模型请求重试和工具操作重试不能被无条件等同**。

Adapter 应清楚表示“模型生成是否完成”，Agent Loop 在确认完整工具调用前不能触发副作用。对断流、超时和部分响应，需要明确恢复/舍弃策略，而不是随意拼接两个不同请求的输出。

## 源码对照：OpenCode 的统一流事件

[OpenCode `session/llm.ts`](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/llm.ts) 的流式路径支持不同推理 Runtime，并将输出适配成统一的 `LLMEvent` 流。源码中的注释明确表达了“不同 runtime 暴露相同事件流”的适配边界。

而 [Codex `client.rs`](https://github.com/openai/codex/blob/main/codex-rs/core/src/client.rs) 将 `ModelClient` 与 `ModelClientSession` 区分：前者保存 Session 生命周期的 Provider/Auth 相关状态，后者是 Turn 级的流式会话，可承载连接与本轮粘性路由状态。

这两个项目技术栈不同，但都说明：**模型通讯层拥有自己独立于 Agent Loop 的生命周期和错误处理责任。**

## 本章归位

| 五个问题 | 回答 |
| --- | --- |
| 是什么 | Adapter 将不同 Provider 的消息与流转换为 Harness 统一协议。 |
| 为什么需要 | 避免 Runtime 绑定某个厂商的具体响应格式。 |
| 没有它 | 换模型时修改 Agent Loop，流式片段导致半成品工具调用。 |
| 实际怎么做 | Request Builder + Capability Profile + Stream Parser + Normalized Events。 |
| 处于哪里 | Agent Loop 与 Model Provider API 之间。 |

## 实践验证

写两个 `FakeProvider`：一个返回完整结构化调用；另一个分三段流式发送同一调用。Harness 对两个 Provider 的工具执行轨迹应一致。再注入第 2 段后断线，验证工具不会执行不完整参数。

**下一章的问题**：核心运行时越来越稳定，但每接一种新能力都要改代码，能否把扩展从核心中解耦？
