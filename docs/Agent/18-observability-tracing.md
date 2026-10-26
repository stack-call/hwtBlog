---
title: 第十八章：Observability——Agent 明明回答成功，为什么退款 Bug 还在？
sidebar_label: 第十八章：可观测性
description: 从线上退款修复失败推导事件、追踪、指标、回放及隐私控制
sidebar_position: 19
tags: [Agent, Harness, Observability, Tracing, Codex]
---


# 第十八章：Observability & Tracing——为什么 Agent 失败后很难排查？

我们的优惠券 Coding Agent 已经拥有：

- Agent Loop：连续执行。
- Tool System：调用工具。
- Execution Environment：执行代码。
- Permission：控制权限。
- Planning：管理复杂任务。
- Human-in-the-Loop：等待用户决策。
- Model Adapter：接入不同模型。
- Hooks、MCP、Skills：扩展能力。

现在，用户要求：

> 修复优惠券部分退款 Bug，不修改订单 API，并确保全部回归测试通过。

Agent 开始工作。

整个任务持续了十几分钟，执行了几十次模型调用和工具调用。

最后，它回答：

```text
修复未完成。仍有测试失败。
```

用户不满意。

开发者小李想查明原因。

但他发现，系统只留下了一份聊天记录。

## 一、第一次事故：聊天记录里有所有消息，为什么还是找不到原因？

小李打开 Session History：

```text
User：
修复优惠券部分退款 Bug。

Assistant：
我先检查退款计算逻辑。

Assistant -> grep：
calculate_refund

Tool：
找到 refund.py。

Assistant -> read_file：
refund.py

Tool：
返回文件内容。

Assistant -> edit_file：
修改退款公式。

Tool：
修改成功。

Assistant -> execute：
pytest tests/test_refund.py

Tool：
测试失败。

Assistant：
我会继续修复。
...
```

看起来流程很完整。

但他仍然回答不了几个关键问题：

1. 第一次修改前，模型看到的是哪一版代码？
2. 测试失败后，模型有没有收到完整错误信息？
3. 测试工具耗时多久？
4. 模型为什么再次修改同一个函数？
5. Context Compaction 发生在什么时候？
6. 哪次模型调用使用了哪个模型？
7. 最终失败是模型判断错误，还是执行环境出了问题？

问题不在于聊天记录没用。

而在于：

**聊天记录记录了交互内容，却不一定记录 Harness 的完整执行过程。**

例如，下面这些事情可能不会出现在普通聊天记录中：

```text
模型请求开始
模型请求重试
模型响应超时
Tool Hook 修改参数
权限审批等待
Context Compaction
Tool Output 截断
Subagent 启动
执行环境重启
```

但是，它们都可能直接影响最终结果。

于是小李决定：

> 至少先把 Agent Runtime 中发生的关键事情记录下来。

---

## 二、第一次改进：加日志

最简单的办法就是打印日志：

```python
print("model request started")
print("tool started")
print("tool completed")
print("model request completed")
```

运行后得到：

```text
10:00:01 model request started
10:00:03 model request completed
10:00:03 tool started
10:00:04 tool completed
10:00:04 model request started
10:00:08 model request completed
```

这比之前好多了。

至少能知道发生了什么。

但现在有两个 Agent 同时运行：

```text
Agent A：修复退款 Bug
Agent B：重构优惠券模块
```

日志变成：

```text
10:00:01 model request started
10:00:01 model request started
10:00:03 model request completed
10:00:03 tool started
10:00:04 model request completed
10:00:04 tool started
10:00:05 tool completed
10:00:05 tool completed
```

小李突然发现：

**他无法判断哪条日志属于哪个任务。**

于是第一个真正的可观测性设计问题出现了。

## 三、第二次改进：为每个事件添加身份信息

小李先加上 Session ID：

```text
session=A model request started
session=B model request started
session=A tool started
session=B tool started
```

这样至少不会把两个会话混在一起。

但一个 Session 里还有多个 Turn。

一个 Turn 中又有多次模型调用。

一次模型调用还可能产生多个并行工具请求。

因此仅有 Session ID 仍然不够。

我们需要建立一套执行关联关系：

```text
Session ID
    |
    v
Turn ID
    |
    v
Model Request ID
    |
    v
Tool Call ID
```

例如：

```json
{
  "event": "tool.completed",
  "session_id": "session-001",
  "turn_id": "turn-005",
  "model_request_id": "model-012",
  "tool_call_id": "call-033",
  "tool_name": "execute",
  "duration_ms": 1240,
  "exit_code": 1
}
```

这是一条教学示例事件，并不是某个开源框架的原始事件 Schema。

现在，小李可以回答：

> `call-033` 属于哪一次模型请求？属于哪个 Turn？执行了什么工具？结果是什么？

这就是结构化事件相对于普通文本日志的重要价值。

### Event 与 Log 是一回事吗？

不完全一样。

我们可以把两者理解为：

**Log**

主要用于记录诊断信息。

例如：

```text
Failed to start shell process
```

**Event**

主要表示某个明确的执行事实。

例如：

```text
tool.started
tool.completed
turn.failed
```

事件往往具有明确的类型和结构。

当然，一条结构化 Log 也可以记录事件。两者在实际系统中会重叠。

真正的区别不是输出到了哪个文件，而是：

**我们是否把某件事情定义为有稳定语义、能够被其他组件消费的执行事实。**

例如：

```text
tool.completed
```

可以被：

- UI 用来显示进度。
- Metrics 模块用来统计耗时。
- Debugger 用来构建执行时间线。
- Audit 模块用来记录操作历史。

但这里又出现一个问题。

---

## 四、第三次事故：Event 已经很多，为什么仍然难以理解一次任务？

现在我们的系统记录了：

```text
model.started
model.completed
tool.started
tool.completed
tool.started
tool.completed
model.started
model.completed
...
```

运行一次复杂任务，可能产生上百条事件。

它们有时间戳，也有 Session ID。

但小李仍然要手动计算：

- 一次模型请求耗时多少？
- 工具调用是否发生在模型请求之后？
- 哪些调用可以并行？
- 哪个子任务让整体任务变慢？

于是他意识到：

**单纯记录发生过什么，还不够。我们还需要记录执行过程之间的关系。**

这就是 Tracing 要解决的问题。

## 五、Trace 和 Span 是什么？从一次退款修复推导

先看完整任务：

```text
修复优惠券退款 Bug
    |
    +-- 搜索代码
    |
    +-- 读取文件
    |
    +-- 修改代码
    |
    +-- 执行测试
    |
    +-- 分析失败
    |
    +-- 再次修改
    |
    +-- 再次测试
```

我们希望把整次任务看作一个整体。

这就是一个 Trace 可以表达的范围。

**Trace 是一次操作链路的关联视图。**

例如：

```text
Trace: repair-refund-001
```

它内部包含多个执行片段：

```text
Trace: repair-refund-001
│
├── Span: Agent Turn
│   │
│   ├── Span: Model Request #1
│   │
│   ├── Span: Tool grep
│   │
│   ├── Span: Tool read_file
│   │
│   ├── Span: Model Request #2
│   │
│   ├── Span: Tool edit_file
│   │
│   ├── Span: Tool execute pytest
│   │
│   └── Span: Model Request #3
│
└── Span: Final Validation
```

这里的 Span 是一个具有持续时间的操作片段。

例如：

```text
Span: execute pytest

Start: 10:00:03.000
End:   10:00:04.240

Duration: 1240 ms
Exit Code: 1
```

一次 Span 通常可以有：

```text
span_id
trace_id
parent_span_id
operation
start_time
end_time
status
attributes
```

这些字段让我们不仅能看到顺序，还能看到嵌套关系和耗时。

### 为什么 parent_span_id 很重要？

假设：

```text
Model Request #5
    |
    +-- Tool read_file A
    |
    +-- Tool read_file B
```

两个工具可能并行运行。

如果只看事件时间戳，我们很难判断它们之间是否存在父子关系。

但如果两个 Tool Span 都关联到同一个模型决策或调度 Span，就可以知道：

```text
这两个工具调用来自同一轮模型决策
```

这里还要注意：

**实际的 Trace 不一定是一棵严格的树。**

例如 Subagent、消息队列和异步任务之间，有时更适合使用 Span Links 表示关联关系。

所以父子关系主要表达执行层次，而 Tool Call ID、任务交接 ID 等可以补充更具体的业务关联。

---

## 六、终于能区分三种故障了

现在，我们重新分析优惠券修复失败的问题。

假设系统收集到这样的执行链：

```text
Model Request #12
    |
    v
Tool: execute pytest
    |
    v
Process exited with code 1
    |
    v
Tool Output:
  Expected: 42.50
  Actual:   45.00
    |
    v
Model Request #13
    |
    v
Model declares task completed
```

根据这些信息，小李至少能够确定：

测试命令实际运行了，也正常返回了失败结果。

所以问题不在于 Shell 无法启动。

问题更可能发生在：

**模型没有正确处理测试失败的信息，或者模型实际上没有看到完整的错误结果。**

这两个原因仍然不同。

于是小李决定继续记录一项之前没有考虑的事实：

> Tool Runtime 产生了什么结果，以及 Model Context 最终包含了什么结果？

这是 Agent 可观测性与普通 Web 服务可观测性的一个关键区别。

---

## 七、第四次事故：工具确实返回了结果，但模型真的看到了吗？

假设 `pytest` 生成了一份非常大的日志：

```text
800 KB test output
```

Tool Runtime 成功收集了完整输出。

但 Context Manager 发现上下文快满了。

于是只保留最后一部分日志。

问题是，关键断言失败信息位于日志的前部。

最终模型看到：

```text
pytest output truncated

...
47 passed
```

模型错误地认为测试已经基本通过。

这时，开发者如果只查看原始工具日志，会发现：

```text
Expected: 42.50
Actual:   45.00
```

于是可能得出错误结论：

> 模型明明看到了失败信息，却没有遵守。

但实际上，模型根本没有收到那段内容。

这里必须区分两种证据。

**Runtime Evidence（运行时证据）**

Tool Runtime 实际产生的内容。

**Model-visible Evidence（模型可见证据）**

最终被放进模型请求中的内容。

它们可能不相同。

```text
Shell Process
      |
      v
完整测试输出
      |
      v
Tool Runtime
      |
      v
Output Truncation / Offloading
      |
      v
Context Builder
      |
      v
最终 Model Request
```

因此，一套真正有用的 Agent Trace 应能回答：

- 工具原始输出是什么？
- 哪些内容被截断或卸载了？
- 最终传给模型的是什么？
- 模型基于哪个请求生成了后续行动？

这里有一个很值得研究的真实实现。

## 八、源码对照：Codex 为什么专门实现 Rollout Trace？

Codex 的代码仓库中有一个独立模块：

[Codex Rollout Trace README](https://github.com/openai/codex/blob/main/codex-rs/rollout-trace/README.md)

它的设计非常贴近我们刚才的问题。

文档强调一个思路：

**先记录原始证据，再离线解释。**

其工作过程可以概括为：

```text
Codex Runtime
     |
     +-- Model Request / Response
     |
     +-- Tool Dispatch
     |
     +-- Terminal Operation
     |
     +-- Context Compaction
     |
     +-- Subagent Events
     |
     v
Raw Trace Bundle
     |
     v
Offline Reducer
     |
     v
Semantic Execution Graph
```

### 8.1 为什么先记录 Raw Events？

因为 Runtime 在执行当下，不一定能立即知道事件之间的全部语义关系。

例如，一个工具调用可能：

- 来自模型直接发出的工具请求。
- 来自 Code Mode 中的嵌套执行。
- 来自子 Agent 的任务处理。

如果立即把它们扁平化成聊天记录，就可能丢失执行来源。

所以 Codex 的 Rollout Trace 先保存原始事件和相关数据引用。

然后由离线 Reducer 构建更容易分析的语义图。

### 8.2 Trace Bundle 保存什么？

源码文档给出的结构包括：

```text
trace-bundle/
  manifest.json
  trace.jsonl
  payloads/
    ...
  state.json
```

其中：

`manifest.json` 保存 Trace 身份等元信息。

`trace.jsonl` 保存按序记录的原始事件。

`payloads/` 保存请求、响应、工具输入输出等原始证据。

`state.json` 可以作为离线归约之后的结果。

可以通过：

```bash
codex debug trace-reduce <trace-bundle>
```

将原始 Bundle 归约为语义结构。

### 8.3 为什么要区分 trace_id 和 session_id？

Codex 文档明确区分诊断 Trace 的身份与被观察的 Session / Rollout 身份。

原因是：

一次诊断记录属于某次观察活动。

Session 则是应用实际运行的会话实体。

它们不必一一对应。

同一个 Session 也可能通过不同方式被记录、分析或恢复。

这和我们前面为 Session、Turn、Tool Call 分别设计 ID 的思路一致。

### 8.4 一个特别重要的细节

Codex Rollout Trace 区分：

```text
ConversationItem
```

和：

```text
ToolCall
TerminalOperation
InferenceCall
Compaction
```

前者代表模型可见对话。

后者代表 Runtime 发生过的执行事实。

这恰好回答了我们刚才的疑问：

**工具确实产生过某段内容，不代表模型一定看到了相同内容。**

### 8.5 它与普通 Telemetry 一样吗？

不一样。

Codex 文档明确说明：

Rollout Trace 是一个需要显式启用的本地诊断机制。

它由：

```text
CODEX_ROLLOUT_TRACE_ROOT
```

控制。

默认不会因为这个功能而把本地 Bundle 上传出去。

但 Bundle 可能包含 Prompt、工具输入输出、终端内容及文件路径，因此必须作为敏感数据保护。

这就引出了另一个概念。

---

## 九、第五次改进：为什么还需要 Metrics？

Trace 可以帮助小李理解一次失败。

但产品已经有几千个任务在运行。

负责人问：

> 最近一周，Agent 的成功率有没有下降？

我们不可能人工打开几千份 Trace。

小李需要把大量执行记录聚合成指标。

这就是 Metrics 的用途。

例如：

```text
agent_turn_total
agent_turn_failed_total
model_request_duration_ms
tool_execution_duration_ms
tool_error_total
model_token_usage
context_compaction_total
```

这些是教学示例中的指标名称，不是某个框架官方要求的固定名称。

### Trace 与 Metrics 有什么区别？

可以这样理解：

**Trace 回答：**

> 这一次任务为什么失败？

**Metrics 回答：**

> 最近一段时间，系统整体发生了什么变化？

例如：

```text
某次 Trace：
  MCP 查询耗时 18 秒。

聚合 Metrics：
  最近一小时 MCP 查询 P95 延迟持续上升。
```

Metrics 适合趋势分析和监控告警。

Trace 适合单次问题的详细诊断。

### 为什么只看平均值不够？

假设：

```text
95% 的工具调用耗时 100 ms
5% 的工具调用耗时 20 秒
```

平均耗时可能掩盖少量严重慢请求。

所以对于延迟，通常还应该观察：

```text
P50
P95
P99
```

但这些只是系统性能指标。

对于 Agent，还有一些更重要的任务指标：

```text
任务完成率
任务验证通过率
平均工具调用次数
重复工具调用率
无效修改率
用户中途取消率
```

尤其需要区分：

**Runtime Success Rate**

Agent Turn 有没有正常结束？

**Task Success Rate**

任务是否满足用户要求？

一个 Turn 正常结束，并不代表退款 Bug 真正修复成功。

这些指标的计算规则必须明确定义，不能简单把模型最后说了“完成”当作任务成功。

---

## 十、源码对照：Codex 的 OpenTelemetry 与 Rollout Trace 有什么不同？

前面研究的是 Codex 的本地 Rollout Trace。

现在看另一部分：

[Codex OpenTelemetry README](https://github.com/openai/codex/blob/main/codex-rs/otel/README.md)

Codex 有专门的：

```text
codex-rs/otel/
```

模块。

它提供：

- Logs。
- Traces。
- Metrics。
- Session-scoped Telemetry。

其中一个关键类型是：

```rust
SessionTelemetry
```

源码文档中的使用形式包括：

```rust
let manager = SessionTelemetry::new(
    conversation_id,
    model,
    slug,
    account_id,
    account_email,
    auth_mode,
    originator,
    log_user_prompts,
    terminal_type,
    session_source,
);

manager.user_prompt(&prompt_items);
```

这是文档给出的真实调用形式。

它强调一个设计：

**业务事件应该自动携带一致的 Session 元数据。**

否则不同模块打出的事件很难关联起来。

例如模型调用与工具调用可能分别记录：

```text
model.request
tool.execution
```

如果缺少会话、Turn 等关联字段，就难以分析它们之间的关系。

### 为什么要分别研究两套机制？

因为它们关注的目标不同。

**OpenTelemetry**

更适合收集、关联和导出运行指标与诊断信息。

**Rollout Trace**

更强调记录一次 Agent Session 的详细原始证据，离线还原其执行和信息流动过程。

简单说：

```text
OpenTelemetry
    关注系统运行状态、趋势、链路诊断

Rollout Trace
    关注某次 Agent 执行的原始证据与语义还原
```

两者互补，但不能直接互相替代。

尤其要注意，Telemetry 的内容收集也需要遵守隐私配置。

例如 Codex 的部分最终回答日志采用显式选择开启的机制，不能认为所有模型消息都会默认完整导出。

---

## 十一、源码对照：DeepAgents 为什么通常借助 LangSmith Tracing？

现在看 DeepAgents。

DeepAgents 建立在 LangChain 与 LangGraph 之上。

它没有必要重新开发整套模型调用追踪协议。

官方项目说明它可以与 LangSmith 配合进行 Tracing、Evaluation 和 Monitoring。

参考：

[LangChain Observability 官方文档](https://docs.langchain.com/oss/python/langchain/observability)

### 11.1 如何启用 Tracing？

官方常见配置是：

```bash
export LANGSMITH_TRACING=true
export LANGSMITH_API_KEY="<your-api-key>"
```

这里需要已配置 LangSmith 账户和有效 API Key。

启用后，受支持的 LangChain Agent 执行过程可以被追踪。

例如我们的 DeepAgents Coding Agent：

```python
from deepagents import create_deep_agent

agent = create_deep_agent(
    model=model,
    tools=tools,
)

result = agent.invoke({
    "messages": [
        {
            "role": "user",
            "content": "修复优惠券部分退款 Bug",
        }
    ]
})
```

`model` 和 `tools` 是预先配置的模型与工具。

Tracing 的目的不是改变 Agent Loop 的决策，而是收集执行信息。

### 11.2 Trace 中为什么会有嵌套 Run？

因为一次 Agent 执行不只是一个函数调用。

例如：

```text
Agent Run
    |
    +-- Model Run
    |
    +-- Tool Run: read_file
    |
    +-- Model Run
    |
    +-- Tool Run: execute
    |
    +-- Model Run
```

LangChain 的 Runnable、Callbacks 与 Tracing 基础设施会帮助形成这些关系。

这样开发者可以定位某次模型调用或工具执行，而不是只能查看最终回答。

### 11.3 如果只想实时观察 Agent 状态呢？

LangGraph 还有另一套机制：

```python
graph.stream(...)
```

它支持不同的 Stream Mode，例如：

```text
messages
updates
values
tasks
checkpoints
debug
```

当前文档还提供了新的 Event Streaming 接口。

这些模式能够帮助客户端观察：

- 模型消息增量。
- Graph State 的变化。
- 节点或任务执行状态。
- Checkpoint 事件。
- 调试信息。

例如：

```python
for chunk in graph.stream(
    input_data,
    stream_mode="updates",
    version="v2",
):
    print(chunk)
```

但一定要区分：

**Streaming 主要服务于执行过程中的实时消费。**

**Tracing 主要服务于关联、诊断、分析和必要的持久记录。**

Stream 中出现过某条消息，不意味着这条消息一定已经被持久保存为可回放的 Trace。

### DeepAgents 给我们的启发

如果已经使用成熟的 Agent Runtime，就可以尽量复用其 Tracing 生态，而不是在每个 Tool 和 Middleware 里手写独立的日志系统。

但仍需要根据业务定义真正的成功标准。

LangSmith 能帮助你观察测试工具调用，却不能自动证明退款金额符合全部业务规则。

---

## 十二、源码对照：OpenCode 为什么区分 Live Event 和 Durable Event？

现在看 OpenCode。

重点阅读：

- [OpenCode V2 Session 设计](https://github.com/anomalyco/opencode/blob/dev/specs/v2/session.md)
- [OpenCode Bus](https://github.com/anomalyco/opencode/blob/dev/packages/core/src/bus.ts)
- [OpenCode Session](https://github.com/anomalyco/opencode/blob/dev/packages/core/src/session.ts)
- [OpenCode Message Updater](https://github.com/anomalyco/opencode/blob/dev/packages/core/src/session/message-updater.ts)

OpenCode V2 的事件系统与前面两个项目又有区别。

它不仅用于调试，还承担 Session 状态变更、客户端同步和持久化事件处理等职责。

### 12.1 一次执行会产生怎样的事件？

例如：

```text
session.execution.started
session.execution.succeeded
session.execution.failed
session.execution.interrupted
```

这些事件可以描述 Session 执行状态的变化。

在 OpenCode 的 `message-updater.ts` 中，可以看到针对这些事件的状态投影逻辑。

例如：

```typescript
"session.execution.started": () => Effect.void,

"session.execution.succeeded": () =>
  idle("succeeded"),

"session.execution.failed": (event) =>
  idle("failed", event.data.error),
```

这是当前源码中的关键映射，做了少量排版调整。

为什么需要这样一个投影组件？

因为系统可能以 Event 作为事实输入，再将其转换为 UI 或业务查询需要的状态表示。

例如：

```text
Execution Event
       |
       v
Message Updater / Projector
       |
       v
Session View
       |
       v
UI
```

客户端不一定直接根据所有原始事件自己计算最终状态。

### 12.2 为什么事件不能全部只放在内存里？

假设用户正在观看 Agent 输出：

```text
模型正在生成...
正在执行测试...
测试完成...
```

网络突然断开。

用户重新连接。

如果所有事件都只存在于实时连接中，就可能丢失之前的执行状态变化。

因此 OpenCode V2 明确区分：

**Live Events**

只面向实时订阅者，不承诺断线后回放。

**Durable Events**

能够按 Session 范围持久读取和回放的事件。

例如，V2 Session 接口支持：

```typescript
sessions.log({
  sessionID,
  after,
  follow,
})
```

这里的 `after` 用于指定读取事件的位置。

`follow` 则决定是否继续订阅新事件。

最重要的一点是：

**并非所有 Live Event 都进入 Durable Log。**

OpenCode V2 的设计文档明确指出，文本增量、推理增量、工具输入增量等实时数据，并不一定可通过 Durable Session Log 回放。

这意味着：

```text
实时 UI 看见过的内容
```

不一定等于：

```text
断线重连后可以逐字回放的内容
```

### 12.3 为什么这是很好的工程设计？

因为不同类型的事件具有不同价值。

例如：

```text
Token Delta:
  "我"
  "发现"
  "问题"
```

这类事件数量大，适合实时展示。

而：

```text
session.execution.failed
```

代表一次重要的状态变化，适合持久保存。

如果把所有 Token Delta 都长期保存，会产生显著的存储和传输成本。

如果完全不保存关键状态事件，又不利于恢复和分析。

因此：

**实时体验所需的数据，与可靠恢复和诊断所需的数据，不一定应该采用相同的存储策略。**

### OpenCode 与分布式 Tracing 的关系

需要注意：

OpenCode 的 Event Bus 不直接等于 OpenTelemetry 的 Span Trace。

前者主要记录和传递执行事件。

后者通常强调有持续时间的操作片段，以及跨服务调用的关联关系。

Event Log 可以成为生成 Trace 的数据来源，但两者不是同一个抽象。

---

## 十三、第六次事故：有了 Trace，为什么还是不能“重放”同一次任务？

小李现在已经收集到完整 Trace。

他想：

> 能不能把这些事件重新播放一遍，让 Agent 再次执行出完全相同的结果？

这里出现了一个非常重要的误解。

**诊断回放，不等于重新执行。**

### 13.1 诊断回放

根据已经记录的数据，还原当时发生过什么。

例如：

```text
模型请求 A
    |
    v
工具调用 B
    |
    v
工具结果 C
    |
    v
模型请求 D
```

它通常不需要真的再次运行 Shell 命令。

### 13.2 真实重新执行

重新调用模型、执行工具、访问外部系统。

这时很多条件可能已经变化：

- 模型版本发生变化。
- Provider 推理结果不完全确定。
- 代码仓库已经更新。
- 外部数据库状态不同。
- 第三方 API 返回不同结果。
- 工具产生了不可逆副作用。

因此，即使使用同一个 Prompt，也未必能得到完全一致的执行结果。

更不能把原来的：

```text
delete_order(...)
```

再次执行一遍来“复现问题”。

### 13.3 如果我们真的需要可靠测试怎么办？

可以考虑构造：

**Recorded Model Responses**

测试时注入已经保存的模型响应。

**Mock Tool Results**

模拟工具输出，而不是重复真实副作用。

**Isolated Test Environment**

在独立测试环境中重新执行。

**Deterministic Evaluator**

对最终结果进行可重复的断言。

例如：

```text
Recorded Model Response:
    调用 read_file("refund.py")

Mock Tool Result:
    返回指定版本的 refund.py

Recorded Model Response:
    调用 execute("pytest")

Mock Tool Result:
    返回测试失败信息
```

这样可以测试 Agent Loop 如何处理特定输出。

但它验证的是 Harness 在给定输入下的执行行为，并不能完全模拟真实模型在动态环境中的所有决策。

所以需要区分：

```text
Trace Replay
Execution Replay
Evaluation
```

三者可以配合，却不是同一个东西。

---

## 十四、现在真正定位优惠券 Bug 的失败原因

回到最初那次失败。

我们有了完整的执行关联信息。

可以按照下面的顺序调查。

### 第一步：找到失败的 Turn

```text
Session: refund-session-001

Turn: turn-007

Outcome: failed
```

### 第二步：检查失败前的工具调用

```text
Tool Call: call-033
Tool: execute
Command: pytest tests/test_refund.py

Exit Code: 1
```

说明测试执行成功，但测试本身未通过。

### 第三步：检查 Tool Runtime 原始输出

```text
FAIL test_partial_refund

Expected: 42.50
Actual: 45.00
```

说明错误信息确实产生了。

### 第四步：检查下次 Model Request

发现模型收到的是：

```text
pytest output truncated
...
47 passed
```

关键失败信息不见了。

### 第五步：检查 Context Manager 事件

```text
tool_output.truncated
```

这次截断发生在模型调用之前。

### 第六步：得出结论

现在我们可以把故障定位到：

**工具结果进入模型上下文的转换过程。**

而不是：

- Shell 没运行。
- Tool Registry 分发错误。
- 模型完全无视了自己实际看到的失败信息。

这次故障说明：

**Agent Trace 的最大价值，是帮助开发者区分不同执行层的责任。**

不是简单统计 Agent 做了多少次工具调用。

---

## 十五、如果自己实现 Observability，应该从哪里开始？

小李不想在第一版就搭建一套庞大的监控平台。

他决定从最小可行设计开始。

首先定义一个结构化事件。

```python
from dataclasses import dataclass
from typing import Any


@dataclass
class AgentEvent:
    event_type: str
    session_id: str
    turn_id: str
    timestamp: str
    data: dict[str, Any]
```

然后定义几类关键事件：

```text
turn.started
turn.completed
turn.failed

model.request.started
model.request.completed

tool.started
tool.completed
tool.failed

context.compacted
approval.requested
approval.resolved
```

注意，这些是教学设计中的事件类型。

### 15.1 先解决 ID 关联

每次执行至少能关联：

```text
session_id
turn_id
model_request_id
tool_call_id
```

如果实现 Span Tracing，再增加：

```text
trace_id
span_id
parent_span_id
```

多 Agent 场景还可能需要：

```text
parent_thread_id
subagent_id
task_id
```

但不需要把所有 ID 都放进每条事件。

应该根据事件来源保留足够的关联信息。

### 15.2 再解决时间与状态

每个可计时的操作都记录：

```text
start_time
end_time
duration
status
```

这样就能区分：

```text
模型请求慢
工具执行慢
审批等待慢
```

例如，整个 Turn 用了 10 分钟，不代表模型推理就用了 10 分钟。

也许其中 8 分钟都在等待用户审批。

如果不拆开时间，性能优化就可能完全找错方向。

### 15.3 最后再增加 Trace 与 Metrics

一开始可以只用结构化 JSONL 日志。

任务增多后，再接入 OpenTelemetry 或其他 Tracing 后端。

有了稳定事件模型，再构建指标。

这样系统能够逐步演进。

---

## 十六、一个最小的可运行 Tracing 示例

为了更直观，我们用 Python 标准库实现一个简单的 Span Recorder。

这只是教学实现，不包含分布式上下文传播、异步上下文隔离和数据脱敏等完整生产能力。

```python
import json
import time
import uuid
from contextlib import contextmanager


def new_id():
    return uuid.uuid4().hex[:12]


@contextmanager
def trace_span(
    name,
    trace_id,
    parent_span_id=None,
    **attributes,
):
    span_id = new_id()
    started = time.perf_counter()

    print(json.dumps({
        "event": "span.started",
        "trace_id": trace_id,
        "span_id": span_id,
        "parent_span_id": parent_span_id,
        "name": name,
        "attributes": attributes,
    }))

    status = "ok"

    try:
        yield span_id

    except Exception:
        status = "error"
        raise

    finally:
        duration_ms = round(
            (time.perf_counter() - started) * 1000,
            2,
        )

        print(json.dumps({
            "event": "span.ended",
            "trace_id": trace_id,
            "span_id": span_id,
            "name": name,
            "status": status,
            "duration_ms": duration_ms,
        }))


trace_id = new_id()

with trace_span("agent.turn", trace_id) as turn_span:

    with trace_span(
        "model.request",
        trace_id,
        parent_span_id=turn_span,
        model="example-model",
    ):
        time.sleep(0.05)

    with trace_span(
        "tool.execute",
        trace_id,
        parent_span_id=turn_span,
        tool="pytest",
    ):
        time.sleep(0.02)
```

输出大致为：

```text
span.started: agent.turn
span.started: model.request
span.ended:   model.request
span.started: tool.execute
span.ended:   tool.execute
span.ended:   agent.turn
```

每条记录还包含对应的 Trace ID、Span ID 和耗时。

这样就可以从同一个 Trace 中还原多次操作。

但真实的异步 Agent 还需要确保上下文跨 `await`、并行工具调用和 Subagent 正确传播。

这也是成熟 Tracing SDK 的价值所在。

---

## 十七、为什么不能把所有 Prompt 和 Tool Output 都上传到监控平台？

小李现在已经实现了完整 Trace。

为了方便调试，他打算记录：

```text
所有用户 Prompt
所有模型响应
所有 Tool Arguments
所有 Tool Results
所有环境变量
```

但安全团队立即提出反对。

原因很简单：

这些内容可能包含：

- 用户私有代码。
- API Key。
- 数据库连接串。
- 个人信息。
- 订单数据。
- 企业内部文档。
- 未公开的业务规则。

因此：

**可观测性不是记录越多越好。**

我们要区分不同的采集级别。

### Level 1：基础运行指标

例如：

```text
model_request_count
tool_error_count
latency
token_usage
```

通常不需要完整 Prompt。

### Level 2：结构化执行事件

例如：

```text
tool_name
duration
status
exit_code
```

能够诊断许多运行时问题。

### Level 3：详细调试证据

例如：

```text
完整 Tool Result
模型请求内容
Context Snapshot
```

这类信息应受到更严格的访问控制。

### 还需要注意什么？

- 机密字段脱敏。
- 数据保留期限。
- 对 Trace 的访问审计。
- 采样策略。
- 多租户隔离。
- 本地存储与远程导出的差异。
- Trace Bundle 的清理与删除策略。

尤其是使用 LangSmith 或 OTLP Exporter 时，应清楚数据会流向哪个服务。

不能因为 Tracing 很有用，就默认所有执行内容都适合上传到第三方平台。

---

## 十八、最终把 Observability 放回 Agent Harness

现在，我们可以整理出整体架构。

```text
                    Agent Loop
                        |
            +-----------+-----------+
            |           |           |
            v           v           v
       Model Call    Tool Call   Context Manager
            |           |           |
            +-----------+-----------+
                        |
                        v
               Observability Layer
                        |
              +---------+---------+
              |         |         |
              v         v         v
            Events     Traces    Metrics
              |         |         |
              v         v         v
           Event Log  Debugger  Dashboard
              |
              v
         Replay / Analysis
```

需要强调：

Observability 不应该完全依赖某一个模块。

例如：

Model Adapter 可以记录模型请求耗时和 Token Usage。

Tool Runtime 可以记录工具执行结果。

Context Manager 可以记录 Compaction、Offloading 和截断。

Permission System 可以记录审批请求与结果。

Execution Environment 可以记录进程退出码和资源消耗。

最终，它们通过关联 ID 被组织成一条可诊断的执行链。

---

## 十九、三个开源 Harness 的设计对比

| 关注点 | DeepAgents / LangChain | OpenCode V2 | Codex |
|---|---|---|---|
| 主要观测机制 | LangSmith Tracing、Graph Streaming | Event Bus、Session Event Log | OpenTelemetry、Rollout Trace |
| 典型数据 | 模型、工具、Graph Run | Session 状态与执行事件 | Logs、Metrics、Spans、原始运行证据 |
| 实时事件 | LangGraph Stream | Live Event Stream | Runtime / Client Events |
| 持久历史 | Tracing 后端与 Checkpoint 等各自承担 | Durable Session Events | Rollout 与可选诊断 Bundle |
| 执行链关联 | Runnable/Graph Run 层次 | Event ID、Session ID、序号 | Trace/Span ID、Turn ID、Tool Call ID |
| 重点优势 | 现成的 Agent 观测与评估生态 | Event-driven Session 状态管理 | 运行时诊断与模型可见信息流追踪 |

### DeepAgents 值得学什么？

复用 LangChain / LangGraph 的执行观测能力。

尤其适合分析模型调用、工具执行和 Graph 运行过程。

### OpenCode 值得学什么？

区分实时事件与持久事件。

并把事件作为 Session 状态更新和客户端同步的重要基础设施。

### Codex 值得学什么？

明确区分普通 Telemetry 和详细 Rollout Trace。

尤其重视模型可见内容与真实运行证据之间的关系。

这三个设计方向不是互斥的。

一个成熟 Harness 完全可以同时使用：

```text
Structured Events
+
OpenTelemetry
+
Detailed Diagnostic Traces
```

关键在于不要重复采集敏感内容，也不要让观测逻辑过度干扰正常执行。

---

## 二十、如何验证可观测性真的有效？

小李准备了几组故障注入测试。

| 注入的故障 | 可观测性应该回答 |
|---|---|
| 模型 API 返回 429 | 哪次请求失败？重试几次？ |
| pytest 测试失败 | 命令是否运行？退出码是什么？ |
| Tool Output 被截断 | 原始输出与模型可见输出有何不同？ |
| MCP Server 超时 | 耗时发生在哪一层？ |
| Permission 审批等待两分钟 | 时间耗费在模型、工具还是人工等待？ |
| Agent 错误地重复修改代码 | 哪次模型决策触发重复修改？ |
| Subagent 执行失败 | 父子任务和消息怎样关联？ |
| Context Compaction 丢失约束 | 哪次 Compaction 改变了模型输入？ |
| Hook 修改工具参数 | 修改发生在哪个执行边界？ |

其中最重要的一项是：

**能否从某个错误的最终结果，追溯到产生它的模型输入、工具反馈和 Runtime 状态变化。**

但要记住：

Tracing 提供的是证据。

它不自动给出唯一正确的根因。

例如，模型误读测试日志，可能是模型能力不足，也可能是日志上下文不完整，还可能是提示中存在相互矛盾的要求。

开发者仍然需要基于证据进行分析。

---

## 二十一、本章的五个核心问题

### 1. Observability 是什么？

Observability 是通过执行数据理解 Agent 内部行为、运行状态和失败原因的能力。

### 2. 为什么需要？

因为 Agent 执行路径由模型动态决定，可能经过多次模型调用、工具执行、中断和上下文变化。

普通聊天记录不足以表达所有关键运行事实。

### 3. Event、Trace、Span、Metrics 分别是什么？

**Event**

描述某个时刻发生的明确事实。

**Trace**

关联一次完整执行链路。

**Span**

描述链路中某个具有持续时间的操作。

**Metrics**

对大量执行数据进行聚合，用于趋势分析和监控。

### 4. 实际怎么做？

先记录结构化事件与关联 ID。

再为模型请求、工具执行等关键操作增加 Span。

随后构建 Metrics 和必要的详细诊断 Trace。

同时明确采集范围、访问权限和保留策略。

### 5. 在整个 Harness 中处于什么位置？

Observability 是跨模块的基础能力。

它贯穿 Agent Loop、Model Adapter、Tool Runtime、Context Manager、Permission、Session 与 Execution Environment。

---

## 本章总结

我们最初只有一份聊天记录。

但复杂任务失败后，它无法准确解释整个执行过程。

于是，我们逐步引入：

```text
Chat History
     |
     v
Structured Logs
     |
     v
Execution Events
     |
     v
Correlation IDs
     |
     v
Trace / Span
     |
     v
Metrics
     |
     v
Diagnostic Replay
```

最终建立了一个关键认识：

> **Agent Observability 不只是记录模型说过什么，而是追踪模型看到了什么、Harness 执行了什么、外部环境返回了什么，以及这些事实如何共同影响下一步决策。**

尤其需要记住：

**Runtime 实际发生的事，不一定等于模型最终看见的内容。**

这是分析 Agent 行为时最容易忽视、也最有价值的一条边界。

## 真实源码阅读路线

### 1. DeepAgents / LangChain / LangGraph

- [LangChain Observability](https://docs.langchain.com/oss/python/langchain/observability)
  - 重点：LangSmith Tracing 与 Agent 执行链路。

- [LangGraph Streaming](https://docs.langchain.com/oss/python/langgraph/streaming)
  - 重点：`updates`、`messages`、`tasks`、`checkpoints`、`debug`。

- [LangGraph StreamMode](https://github.com/langchain-ai/langgraph/blob/main/libs/langgraph/langgraph/types.py)
  - 重点：不同 Stream Mode 的类型与数据边界。

### 2. OpenCode

- [V2 Session Contract](https://github.com/anomalyco/opencode/blob/dev/specs/v2/session.md)
  - 重点：Durable Events 与 Live Events 的区别。

- [OpenCode Bus](https://github.com/anomalyco/opencode/blob/dev/packages/core/src/bus.ts)
  - 重点：`publish()`、`subscribe()`、`log()`。

- [OpenCode Session](https://github.com/anomalyco/opencode/blob/dev/packages/core/src/session.ts)
  - 重点：`sessions.log({ sessionID, after, follow })`。

- [OpenCode Message Updater](https://github.com/anomalyco/opencode/blob/dev/packages/core/src/session/message-updater.ts)
  - 重点：执行事件如何转换成 Session View。

### 3. Codex

- [OpenTelemetry README](https://github.com/openai/codex/blob/main/codex-rs/otel/README.md)
  - 重点：`SessionTelemetry`、Logs、Traces、Metrics。

- [Rollout Trace README](https://github.com/openai/codex/blob/main/codex-rs/rollout-trace/README.md)
  - 重点：Raw Events、Payloads、Reducer、Semantic Graph。

- [Rollout Trace 实现](https://github.com/openai/codex/blob/main/codex-rs/rollout-trace/src/lib.rs)
  - 重点：`TraceWriter`、`ThreadTraceContext`、`replay_bundle`。

- [Rollout Recorder](https://github.com/openai/codex/blob/main/codex-rs/rollout/src/recorder.rs)
  - 重点：Session Rollout 持久记录，与详细诊断 Trace 的区别。

以上为持续演进的源码入口。正式研究时建议固定具体 Commit。

---

**下一章：从零整合一个最小 Agent Harness。**

我们将把之前研究过的模块放进同一条真实调用链：

`Session → Agent Loop → Context Builder → Model Adapter → Tool Runtime → Permission → Execution Backend → Event Recorder`

不追求一开始就复刻 Codex、DeepAgents 或 OpenCode，而是先实现一个结构清晰、可测试、可以逐步扩展的最小 Harness。

最重要的是验证：一次工具调用如何从模型响应开始，经过注册、校验、授权、执行、记录，最终变成下一次模型请求的上下文。
