---
title: 第十章：Agent Loop：从一次工具调用到完整的执行控制
sidebar_position: 10
description: 用修复优惠券 Bug 的连续工程故事，推导 Agent Loop 的边界、模型步进、工具结果、停止条件、并发与取消，并对照 Codex 真实源码。
tags: [Agent, Harness, Runtime, AgentLoop, Codex]
---


# Agent Loop：Agent 为什么能连续执行任务？

这一章，我们不从 Agent Loop 的定义开始，而是从一个最普通的 LLM 应用出发，逐渐把它改造成能自主修复代码的 Coding Agent。

整个过程只围绕一个问题展开：

**为什么普通 LLM 只能回答问题，而 Coding Agent 却能不断读取代码、修改文件、运行测试，直到完成任务？**

## 一、从一个只能回答问题的 LLM 开始

假设我们正在开发一个电商系统。

用户发现优惠券退款金额计算错误，希望 AI 帮忙修复：

> 修复优惠券退款 Bug。你需要定位问题、修改代码、运行测试，确认正确后再结束。

我们先写一个最简单的程序：

```python
response = llm.chat([
    {"role": "user", "content": "修复优惠券退款 Bug"}
])

print(response)
```

模型可能回答：

```text
建议检查 refund.py 中的退款计算逻辑。

如果当前实现使用商品原价计算退款，
应该改为按照实际支付金额计算。
```

这个回答可能正确，但实际上模型并没有修改任何代码。

**为什么？**

因为 LLM 本质上只是接受输入、产生输出的模型。

它并不会自动获得访问文件、执行 Shell、修改代码的能力。

于是我们给它三个工具：

```python
grep(pattern)
read_file(path)
execute(command)
```

并通过模型支持的 Tool Calling 协议，把这些工具的名称、参数说明和用途告诉模型。

现在模型可以产生这样的响应：

```json
{
  "type": "function_call",
  "call_id": "call_001",
  "name": "grep",
  "arguments": {
    "pattern": "calculate_refund"
  }
}
```

注意：**模型只是输出了一个调用工具的请求，还没有真正执行 `grep`。**

这是理解 Agent Loop 的第一个关键。

模型没有直接控制操作系统。真正执行工具的是我们编写的 Harness。

## 二、第一次改进：让程序执行模型提出的动作

既然模型能告诉我们要调用什么工具，那我们就按照它的要求执行。

```python
response = llm.chat(messages)

if response.tool_calls:
    for call in response.tool_calls:
        result = execute_tool(call)
        print(result)
```

这一次，确实执行了 `grep`。

假设返回：

```text
src/order/refund.py:82
def calculate_refund(order):
```

但现在出现一个问题：

工具结果只是被程序打印了出来，模型并不知道工具执行后发生了什么。

于是我们想到：

**执行完工具后，必须把结果告诉模型。**

```python
response = llm.chat(messages)

if response.tool_calls:
    for call in response.tool_calls:
        result = execute_tool(call)

        messages.append({
            "role": "tool",
            "tool_call_id": call.id,
            "content": str(result)
        })

    response = llm.chat(messages)
```

这里是示意代码，完整实现还需要把模型发出的 assistant tool-call 消息也记录到历史中。

再调用模型后，它可能返回：

```text
现在已经找到退款计算入口。
下一步需要读取 refund.py 的具体实现。
```

随后请求：

```json
{
  "type": "function_call",
  "call_id": "call_002",
  "name": "read_file",
  "arguments": {
    "path": "src/order/refund.py"
  }
}
```

新的问题又来了。

**我们的程序只处理了一轮工具调用。第二次工具调用出现后，又需要重新编写相同的逻辑。**

既然每次都是：

1. 调用模型。
2. 检查模型的输出。
3. 执行工具。
4. 把结果交给模型。
5. 继续调用模型。

为什么不把这个过程写成一个循环？

## 三、Agent Loop 就这样产生了

我们写出第一个真正的 Agent Loop。

以下是教学实现，省略了错误处理、工具参数校验和上下文预算。

```python
async def agent_loop(messages):
    while True:
        response = await llm.chat(messages)

        # 记录模型输出（包括工具调用请求）
        messages.append(response.message)

        # 模型没有再请求工具
        if not response.tool_calls:
            return response.text

        # 执行本次响应中的全部工具调用
        for call in response.tool_calls:
            result = await execute_tool(call)

            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": str(result),
            })
```

虽然非常简单，但它已经具备 Agent 的核心工作模式。

让我们完整运行一遍。

### 一次真实任务的执行过程

```text
用户：修复优惠券退款 Bug
        │
        ▼
   第 1 次 LLM 调用
        │
        └─ 请求 grep("calculate_refund")
                    │
                    ▼
               Harness 执行 grep
                    │
                    ▼
               返回代码位置
                    │
                    ▼
   第 2 次 LLM 调用
        │
        └─ 请求 read_file("refund.py")
                    │
                    ▼
               Harness 读取文件
                    │
                    ▼
               返回文件内容
                    │
                    ▼
   第 3 次 LLM 调用
        │
        └─ 请求 apply_patch(...)
                    │
                    ▼
               Harness 修改代码
                    │
                    ▼
               返回修改结果
                    │
                    ▼
   第 4 次 LLM 调用
        │
        └─ 请求 execute("pytest")
                    │
                    ▼
               Harness 运行测试
                    │
                    ▼
               返回测试结果
                    │
                    ▼
   第 5 次 LLM 调用
        │
        └─ 输出最终答复
                    │
                    ▼
                 结束
```

这里发生了一件非常重要的事情：

**我们没有预先写死“先 grep，再 read_file，再修改，再测试”的执行顺序。**

我们只是不断执行模型提出的工具调用，并把执行结果交回模型。

模型根据每次收到的新信息，决定下一步采取什么动作。

这就是 Agent 能表现出自主性的基础。

### 那么，谁真正控制这个循环？

可以把职责分成两部分：

**模型负责决策：**

- 下一步读取哪个文件？
- 是否需要修改代码？
- 是否需要运行测试？
- 是否还需要更多信息？

**Harness 负责执行与控制：**

- 模型请求的工具是否合法？
- 是否允许执行这个操作？
- 如何执行工具并记录结果？
- 是否应该再次调用模型？
- 什么时候必须停止？

因此：

> Agent Loop 的核心，不是 `while True` 这个语法，而是 Harness 将模型决策转化为真实动作，再把环境反馈交还给模型的控制过程。

---

## 四、第一次遇到真正的控制流问题：什么时候结束？

我们目前的逻辑是：

```python
if not response.tool_calls:
    return response.text
```

即没有工具调用就结束。

对于最简单的 Tool Calling Agent，这是合理的第一版设计。

但是，我们需要进一步理解它的含义。

假设模型返回：

```text
退款金额计算已经修复。
所有测试通过，任务完成。
```

这时 Harness 结束循环。

可是有个问题：

**模型说测试通过，就真的代表测试通过吗？**

不一定。

假设模型只修改了代码，没有运行测试，就直接声称任务完成。

从 Agent Loop 的角度看，它确实没有再次请求工具。

但从用户的角度看，它并没有满足任务要求。

这就暴露了两个不同的概念：

| 概念 | 含义 |
|---|---|
| Turn Completion | 当前这一轮 Agent 执行是否结束 |
| Task Success | 用户交付的任务是否真正成功 |

两者不能画等号。

一个 Agent Turn 可以正常结束，但任务结果仍然错误。

反过来，测试已经通过，模型也可能继续进行不必要的操作。

那么该怎么办？

最简单的做法，是要求模型在结束前确认测试结果；更可靠的做法，是由 Harness 或外部验证流程检查交付条件。

例如，我们可以记录：

```python
task_state = {
    "files_modified": [],
    "tests_executed": False,
    "tests_passed": False,
}
```

当模型准备完成任务时，可以检查：

```python
if not response.tool_calls:
    if not task_state["tests_passed"]:
        # 不直接把“成功完成”交付给用户
        ...
```

但这里不应该简单写成“没有通过测试就无限重试”。

测试可能无法运行，用户也可能允许报告失败结果。

因此，更完整的系统应该区分：

- 正常完成。
- 完成但未验证。
- 任务失败。
- 因限制或取消而中止。

**Agent Loop 控制执行何时停止；任务验证负责判断交付结果是否满足目标。**

这是两个不同的模块。

---

## 五、第二个问题：一次模型调用为什么不等于一次 Agent Turn？

这里引入一个容易混淆的概念。

假设用户发来：

```text
修复优惠券退款 Bug。
```

Agent 连续调用模型 20 次、执行 15 次工具，最终完成任务。

那么这是 20 次 Turn 吗？

在我们采用的运行模型中，不是。

我们把整个过程理解成一次用户请求触发的 Agent Turn。

```text
Session（一个持续的会话）
│
├── Turn 1：用户要求修复退款 Bug
│   │
│   ├── Model Step 1：grep
│   ├── Model Step 2：read_file
│   ├── Model Step 3：apply_patch
│   ├── Model Step 4：pytest
│   └── Model Step 5：最终答复
│
└── Turn 2：用户要求添加新的测试
    │
    ├── Model Step 1：read_file
    ├── Model Step 2：apply_patch
    └── Model Step 3：最终答复
```

这里的 Model Step 是为了教学使用的名称，不同框架内部不一定使用同一个术语。

为什么要区分这几个层次？

因为它们的生命周期不同。

**Session：** 负责持续的会话历史与配置。一个 Turn 完成后，Session 仍然存在。

**Turn：** 负责处理一次任务请求。它可能经历多次模型调用和工具执行。

**Model Step：** 负责一次模型请求及其响应处理。它结束时，Turn 可能还要继续。

这个区别会直接影响错误处理。

例如第 4 个 Model Step 的测试工具失败，并不一定意味着整个 Turn 失败；Agent 完全可以根据错误信息进行修复，再运行一次测试。

如果把 Model Step 与 Turn 混为一谈，就会错误地把每次工具失败视为任务终止。

---

## 六、第三个问题：工具执行失败，Agent Loop 应不应该停止？

我们的 Coding Agent 调用了：

```text
execute("pytest tests/test_refund.py")
```

工具返回：

```text
Exit Code: 1

FAIL test_partial_refund
Expected: 42.50
Actual:   45.00
```

如果写成：

```python
result = execute_tool(call)

if result.exit_code != 0:
    raise RuntimeError("Agent failed")
```

整个 Agent Loop 就停止了。

这显然不合理。

为什么？

因为对于 Coding Agent 来说，测试失败通常不是执行异常，而是**完成任务所需要的反馈信息**。

它恰恰应该把测试失败交给模型，继续修复。

因此我们需要区分：

**工具执行得到失败结果：**

```text
pytest 正常运行，但测试未通过
```

和：

**运行环境发生了无法处理的异常：**

```text
工具执行服务不可用
无法创建 Shell 进程
会话已被取消
```

第一种应该通常作为 Tool Result 返回模型。

第二种才可能需要由 Harness 进行重试、中止或其他恢复处理。

例如：

```python
result = await execute_tool(call)

messages.append({
    "role": "tool",
    "tool_call_id": call.id,
    "content": format_tool_result(result),
})
```

即使 `pytest` 的退出码是 1，也可以正常把测试输出交回模型。

下一轮，模型可能决定：

```text
测试失败说明部分退款的折扣分摊仍有问题。
需要再次检查 refund.py。
```

然后继续行动。

到这里我们发现：

> 工具失败不必然意味着 Agent 失败。很多工具失败信息，恰恰是 Agent 下一步决策的依据。

于是 Tool Runtime 开始变成 Agent Loop 的一个独立模块，负责把不同类型的执行结果转换成模型能够理解的反馈。

---

## 七、第四个问题：模型一次提出多个工具调用怎么办？

之前我们假设模型每次只请求一个工具。

但真实的 Tool Calling API 可以允许一次响应包含多个工具调用。

例如：

```json
{
  "tool_calls": [
    {
      "id": "call_a",
      "name": "read_file",
      "arguments": {"path": "coupon.py"}
    },
    {
      "id": "call_b",
      "name": "read_file",
      "arguments": {"path": "refund.py"}
    }
  ]
}
```

Harness 不能只执行第一个调用。

它需要正确处理两个请求，并且保证结果对应正确的调用 ID。

```text
LLM Response
    |
    +-- call_a --> read_file(coupon.py)
    |                  |
    |                  v
    |              result_a
    |
    +-- call_b --> read_file(refund.py)
                       |
                       v
                   result_b
```

最终应记录类似：

```text
assistant:
  call_a = read_file(coupon.py)
  call_b = read_file(refund.py)

tool:
  call_id = call_a
  result = coupon.py 的内容

tool:
  call_id = call_b
  result = refund.py 的内容
```

为什么 `call_id` 重要？

因为工具执行可能并发进行，返回顺序不一定等于请求顺序。

如果只根据数组位置匹配结果，就可能把 `refund.py` 的内容错误关联到 `coupon.py` 的调用。

此外，并非所有工具都适合并行。

两个只读工具可以并行执行，但两个同时修改同一个文件的工具，可能产生写入冲突。

所以这一次我们不仅需要一个循环，还需要工具调度能力：

- 识别所有工具调用。
- 关联每个调用与结果。
- 决定串行还是并行。
- 等待工具执行完成。
- 把结果加入下一轮模型输入。

在这个过程中，Agent Loop 不必自己实现文件锁和并发控制，它可以委托给 Tool Runtime。

**Agent Loop 负责决定什么时候调度工具，Tool Runtime 负责怎么安全地执行它们。**

---

## 八、第五个问题：模型已经输出文字，为什么 Agent 还在运行？

这是 Agent Loop 很容易理解错误的地方。

假设模型在同一次响应中产生：

```text
我已经定位到退款金额错误的原因。
接下来会修改代码。
```

随后又产生工具调用：

```text
apply_patch(...)
```

如果 Harness 看到模型输出文字，就认为任务完成，会发生什么？

它会提前退出，而修改代码的工具根本没有机会执行。

因此：

**模型生成文字，不等于模型已经完成当前 Turn。**

更进一步，使用流式 API 时，还会出现：

```text
模型开始输出文字
        |
        v
收到一部分 Text Delta
        |
        v
收到 Function Call
        |
        v
模型本次响应结束
        |
        v
Harness 执行工具
        |
        v
下一次模型请求
```

所以必须区分：

**模型响应完成：** 当前这一次 LLM API 调用结束。

**Agent Turn 完成：** Harness 判断当前任务不需要继续执行，并完成相关收尾工作。

它们不是同一个事件。

这也正是为什么 Codex 中需要显式维护 `needs_follow_up`。

---

## 九、回到真实源码：Codex 的 Agent Loop

前面所有设计，都是我们从工程问题中逐步推导出来的。

现在去看 Codex。

官方仓库：

https://github.com/openai/codex

我们重点阅读两个文件：

- `codex-rs/core/src/session/turn.rs`
- `codex-rs/core/src/stream_events_utils.rs`

源码入口：

https://github.com/openai/codex/blob/main/codex-rs/core/src/session/turn.rs

### 9.1 run_turn：负责整个 Turn 的控制流

Codex 的 `run_turn()` 是核心入口之一。

我们先不要研究它的全部参数，只关心最核心的结构：

```rust
loop {
    // 收集待处理输入
    // 捕获本次执行所需的上下文
    // 构造模型请求
    // 调用模型并处理响应
    // 判断是否继续下一次模型调用
}
```

这是对源码控制结构的简化，不是原样代码。

为什么 Codex 需要一个 Loop？

因为一条用户请求可能经过多个模型调用和工具执行。

这里有一个重要调用过程：

```text
run_turn()
    |
    ├── 获取当前 Context
    |
    ├── run_sampling_request()
    |       |
    |       ├── 发送模型请求
    |       ├── 消费模型响应流
    |       ├── 识别工具调用
    |       └── 调度并处理工具结果
    |
    ├── 检查是否需要继续
    |
    ├── 如果需要：再次执行循环
    |
    └── 如果不需要：处理结束阶段
```

这里的 `run_sampling_request()` 并不等于整个 Agent Turn。

它主要负责一次模型采样请求及其响应、工具处理。

而 `run_turn()` 负责把这些请求组织成连续的 Agent 执行过程。

### 9.2 Tool Call 如何让 Loop 继续？

在 Codex 的 `stream_events_utils.rs` 中，处理模型输出的逻辑包含一个关键字段：

```rust
output.needs_follow_up = true;
```

这行代码出现于识别并安排工具调用的流程中。

它背后的含义是：

**模型请求执行工具之后，当前任务通常不能就此结束。**

因为工具结果还需要返回模型，让模型决定下一步。

例如：

```text
Step 1：模型请求 grep
        |
        v
needs_follow_up = true
        |
        v
执行 grep
        |
        v
Step 2：模型读取 grep 结果继续推理
```

这就是我们之前用 `if response.tool_calls` 推导出来的机制。

区别在于真实的 Codex 还要处理流式消息、异步工具执行、取消、错误状态等问题。

### 9.3 run_turn 如何判断最终结束？

在 `turn.rs` 中，有一段很关键的真实代码：

```rust
let needs_follow_up =
    model_needs_follow_up || has_pending_input;
```

我们先看两个变量：

`model_needs_follow_up` 表示模型响应和工具处理流程是否要求后续模型调用。

`has_pending_input` 表示当前还存在等待处理的新输入，例如执行期间送入的用户消息或其他输入。

因此，是否继续，并不完全由模型的工具请求决定。

假设第 5 次模型调用已经给出了最终回答。

此时：

```text
model_needs_follow_up = false
has_pending_input     = false
```

那么：

```text
needs_follow_up = false
```

当前 Turn 就可以进入结束流程。

但如果执行期间用户又发来了：

```text
不要只修复退款金额，还要加上回归测试。
```

新的输入可能被纳入当前 Turn。

于是：

```text
model_needs_follow_up = false
has_pending_input     = true
```

最终：

```text
needs_follow_up = true
```

Harness 仍然需要继续执行。

这里简化了 Codex 对输入队列的接收时机和资格判断；实际实现还包括 Stop Hooks、Compaction、取消和其他结束阶段处理。

**我们终于理解了：Agent Loop 不仅是执行模型发出的 Tool Call，更是统一调度模型输出、工具反馈与外部输入的控制流程。**

---

## 十、最后一个问题：为什么 Agent Loop 还要管理取消？

假设 Agent 正在运行：

```text
execute("pytest")
```

测试预计运行很久。

用户这时点击了停止按钮。

如果我们只检查：

```python
while True:
    ...
```

的循环条件，可能还不够。

因为程序正阻塞在工具调用里，还没有机会进入下一次循环。

所以取消不应该只存在于 Loop 的最外层。

它还需要传给工具执行层：

```text
用户请求停止
       |
       v
Agent Runtime
       |
       v
Cancellation Token
       |
       +--> 当前模型请求
       |
       +--> 正在执行的工具
       |
       +--> 等待中的异步任务
```

Codex 的 `run_turn()` 接收 `CancellationToken`，并在执行流程中向下传递。

这不是多加一个 `if cancelled` 那么简单。

对于正在运行的 Shell 进程，真正的取消还涉及子进程终止、资源清理，以及取消后怎样记录工具调用状态。

**这时我们才真正理解为什么 Agent Loop 要从简单同步循环演进为异步 Runtime。**

---

## 十一、最终把 Agent Loop 放回整个 Harness 体系

我们已经建立了一条完整的演进路径：

| 遇到的问题 | 自然产生的机制 |
|---|---|
| LLM 只能返回文字 | Tool Calling |
| 模型请求工具但不能亲自执行 | Tool Execution |
| 执行结果需要交回模型 | Tool Result Feedback |
| 任务需要连续多个动作 | Agent Loop |
| 没有工具调用但任务未必成功 | Turn Completion / Task Validation |
| 某个工具失败但任务仍可继续 | Error Handling |
| 一次产生多个工具调用 | Tool Scheduling |
| 流式输出文字后可能还有工具调用 | Response Lifecycle |
| 用户在执行途中发送新消息 | Pending Input |
| 用户中途停止任务 | Cancellation |

最终我们得到的架构：

```text
                 用户任务
                    |
                    v
              Agent Runtime
                    |
                    v
              Agent Loop
                    |
            +-------+-------+
            |               |
            v               |
       Context Builder      |
            |               |
            v               |
         LLM Call           |
            |               |
            v               |
      Response Handler      |
            |               |
       +----+-----+         |
       |          |         |
       v          v         |
   Tool Call   No Tool Call |
       |          |         |
       v          v         |
   Tool Runtime 结束判断    |
       |          |         |
       v          |         |
   Tool Result    |         |
       |          |         |
       +----------+---------+
                    |
              继续或结束
```

这里的 Agent Loop 是控制中心，但它不应该负责所有具体工作。

Context Builder 负责提供模型输入，Tool Runtime 负责工具执行，Session 负责会话生命周期，Policy Engine 负责权限约束，Checkpoint 负责恢复执行。

### Agent Loop 的五个核心问题

现在我们可以正式回答这个概念应该回答的五个问题。

**1. 是什么？**

Agent Loop 是 Harness 中负责反复调用模型、处理模型输出、执行工具并判断是否继续的执行控制机制。

**2. 为什么需要？**

因为复杂任务需要多次模型决策和环境交互，单次 LLM 调用无法独立完成完整任务。

**3. 没有它会怎样？**

开发者需要预先固定工具执行顺序，或者在每次模型返回后手动重新调用模型，无法实现通用的自主执行过程。

**4. 实际怎么做？**

通过循环调度 Model Request，识别工具调用，执行工具，记录结果，处理异常和新输入，并根据运行状态决定继续或停止。

**5. 在整个 Harness 中处于什么位置？**

它是 Agent Runtime 的核心控制流，连接 Model Adapter、Context Manager、Tool Runtime、Policy Engine、Session 与 Checkpoint。

---

## 十二、独立验证：你是否真正理解了 Agent Loop？

假设你正在开发一个数据库分析 Agent。

用户要求：

> 查询过去七天订单异常情况，生成一份报告。

Agent 的实际执行过程是：

```text
Step 1：
模型请求 query_database()

Step 2：
数据库返回查询错误

Step 3：
模型修改 SQL，再次请求 query_database()

Step 4：
数据库返回正常数据

Step 5：
模型请求 generate_report()

Step 6：
报告生成成功

Step 7：
模型输出最终总结
```

我们不需要预先告诉程序每一步怎么做。

只要 Agent Loop 能够：

1. 理解模型发出的工具请求。
2. 执行工具并将结果正确反馈。
3. 在工具失败时允许模型继续调整。
4. 在模型不再请求行动时判断是否结束。
5. 正确处理运行时的异常、取消和限制。

同一套控制机制就能够支持这项全新的任务。

这说明 Agent Loop 与具体业务解耦了。

### 本章总结

最开始，我们只有一次：

`LLM Input → LLM Output`

后来，我们让模型能够请求工具，得到环境反馈，再继续思考。

最终演化为：

**Observe → Decide → Act → Observe → ...**

但真正的重点是：

> **LLM 决定下一步希望做什么，Agent Loop 决定怎样让这个决定变成真实行动，以及何时继续或停止。**

理解这一点后，再研究 Tool Runtime、Permission、Planning 和 Checkpoint，就会发现它们并不是与 Agent Loop 平行、毫无关系的概念，而是在同一个执行闭环中，为解决不同工程问题逐渐形成的模块。

## 参考源码

以下为官方源码入口。由于项目持续更新，建议以具体提交版本为准。

- [Codex `run_turn()`：Agent Turn 的主控制流](https://github.com/openai/codex/blob/main/codex-rs/core/src/session/turn.rs)
- [Codex `handle_output_item_done()`：处理模型输出与工具调用](https://github.com/openai/codex/blob/main/codex-rs/core/src/stream_events_utils.rs)
- [Codex `ToolRegistry`：工具调度与执行分发](https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/registry.rs)
- [Codex `TurnState`：Turn 级别运行状态](https://github.com/openai/codex/blob/main/codex-rs/core/src/state/turn.rs)
