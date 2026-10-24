---
title: 第十章：Agent Loop——从 while 循环到可控运行时
sidebar_label: 第十章：Agent Loop
description: 通过优惠券 Coding Agent 的连续故障，推导模型调用、工具分发、并发、结束、取消与事件流
sidebar_position: 11
tags: [Agent, Harness, Runtime, Codex]
---

# 第十章：Agent Loop——Agent 为什么能连续行动？

> 延续同一个工程：小李正在开发修复优惠券退款 Bug 的 Coding Agent。本章关注负责推动整个系统前进的 **Agent Loop**。所有 Python 小片段都是教学用最小设计；Codex 的真实函数和路径会明确标出。

## 事故一：一次模型回答不等于任务完成

最初的版本很简单：模型收到「修复优惠券退款」任务，调用 `grep`；Harness 执行工具；再把结果发回模型。

```python
# 教学示例：忽略流式、异常和并发
while True:
    response = model.generate(context())
    if response.final:
        return response.text
    result = call_tool(response.tool_call)
    history.append(result)
```

测试一开始就暴露问题：模型先说「我将检查退款代码」，随后给出一个工具调用。它的文本说明不是最终答案；如果靠“出现 assistant 文本”判断完成，Agent 会过早退出。

**为什么会这样？** 模型返回的是*一次采样的输出项*，它可能包含说明文字、多个工具调用，或最终答复。Harness 要根据协议中的结构化输出以及本轮是否仍有工具调用决定是否继续，而不是猜测自然语言。

我们的第一个改进是把结果分类：`assistant_message`、`tool_call`、`turn_complete`、`error`。这就是**运行时事件**的雏形。

## 事故二：模型一次请求三个工具怎么办？

下一轮，模型同时要求读取 `coupon.py`、`refund.py` 和 `tests/test_refund.py`。旧循环只支持 `response.tool_call` 一个对象。

小李尝试全部并行。但 `read_file` 可以并行，两个 `edit_file` 同时修改相同路径却可能互相覆盖。

这次改进不能简单等于 `asyncio.gather`。需要把工具标成“是否允许并行”，区分只读与有副作用操作，并把多个结果与原来的 `call_id` 正确配对。如果模型要求三个工具，通常应先收齐相应结果，再构造满足当前模型 API 协议的下一轮输入。

```python
# 教学示例：表达控制逻辑，不代表具体模型协议
calls = response.tool_calls
if all(registry.can_parallelize(c) for c in calls):
    results = await execute_parallel(calls)
else:
    results = [await execute_one(c) for c in calls]
history.extend(pair_with_call_ids(calls, results))
```

## 事故三：工具执行很久，用户却点击了取消

优惠券测试命令卡住了五分钟。如果 `run_turn` 一直等待工具结束，用户点击“停止”没有反应。

小李终于意识到，Agent Loop 不只是一个顺序迭代器，而是**一个可取消的异步任务**。它需要：

- 可传递的 Cancellation Token；
- 每个工具各自的超时与清理逻辑；
- 明确的 `running / waiting_for_tool / interrupted / completed / failed` 状态；
- 在每个边界发布事件，让 CLI 或 IDE 展示当前进度。

取消并不一定能够撤销已经发生的数据库写入。因此 Loop 的取消和工具的幂等、补偿机制必须分开讨论。

## 与 Codex 的真实源码对照

阅读 [Codex `session/turn.rs`](https://github.com/openai/codex/blob/main/codex-rs/core/src/session/turn.rs)，定位 **`run_turn`**。源码函数签名中有：

```rust
pub(crate) async fn run_turn(
    sess: Arc<Session>,
    turn_context: Arc<TurnContext>,
    mut input: Vec<TurnInput>,
    // ...
    cancellation_token: CancellationToken,
)
```

这里选取的是源码关键参数的节选，省略了其余参数。注意 `Session`、`TurnContext` 和 `CancellationToken`：它们体现了持久会话、单次 Turn 配置与取消生命周期的分层，而不是单个 `messages` 数组包办所有职责。

继续沿着代码中的 `ToolRouter` 和 `ToolCallRuntime`，就能看到模型输出如何进入工具分发。工具注册与执行分发分别可以从 [tools/router.rs](https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/router.rs) 和 [tools/registry.rs](https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/registry.rs) 追踪。**建议按调用方向阅读，而不要从文件第一行开始逐行读。**

## 事故四：怎么定义“这轮结束”？

任务可能以四种方式停止：模型提交最终答复、达到步数上限、用户取消、不可恢复错误。它们的语义不同。

尤其要区分 **Turn** 和 **Session**：一次用户请求的 Turn 已结束，并不意味着整个 Session 被删除；用户还可以继续追问，甚至恢复中断任务。运行时应该发出明确的终止原因，而不只是 `break`。

一个稳妥的最小状态机：

```text
READY → MODEL_CALL → TOOL_DISPATCH → TOOL_WAIT ┐
             ↑                                │
             └──────── TOOL_RESULTS ─────────┘
          ↓             ↓              ↓
       COMPLETED     CANCELLED       FAILED
```

每一跳都记录事件、消费时间预算，并在外部副作用边界设置可恢复点。

## 到这里，我们推导出了什么？

| 五个问题 | 回答 |
|---|---|
| 是什么 | Agent Loop 是驱动模型与工具交替执行的运行时控制流。 |
| 为什么需要 | 模型输出不是最终答案的同义词；还需要处理调用、重试、取消和结束。 |
| 没有它 | 容易过早结束、漏掉工具调用、无法取消或留下悬空工具结果。 |
| 实际怎么做 | 显式状态、结构化输出处理、Tool Registry、事件流、取消和超时。 |
| 处于哪里 | 位于 Harness 中枢，向上服务 Session/UI，向下调度 Model Adapter 与 Tool Runtime。 |

### 独立验证

为一个模拟模型写测试：第一轮同时返回两个只读工具调用，第二轮返回文本加工具调用，第三轮才返回最终回答。要求：不能提前结束；每个 Tool Result 与 `call_id` 对齐；取消中途命令后不再启动新工具。

**下一章的问题**：工具调用已经产生，但工具名称、参数与权限应该由谁统一处理？
