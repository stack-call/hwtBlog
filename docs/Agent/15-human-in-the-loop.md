---
title: 第十五章：Human-in-the-Loop——用户不在线时，Agent 怎么等？
sidebar_label: 第十五章：Human-in-the-Loop
description: 从退款审批推导持久化中断、审批绑定、恢复与副作用重放
sidebar_position: 16
tags: [Agent, Harness, Human-in-the-Loop, LangGraph]
---



# 第十五章：Human-in-the-Loop——Agent 如何暂停、等待和恢复？

前面，我们已经实现了一个比较完整的 Coding Agent。

它拥有：

- Agent Loop：持续调用模型并处理行动。
- Tool System：执行模型提出的工具调用。
- Execution Environment：提供代码运行环境。
- Permission System：控制危险操作。
- Planning：组织复杂任务并追踪进度。

现在它能够自主完成很多软件工程任务。

但小李很快遇到了一个新问题。

**Agent 有时不是能力不足，而是缺少只有人类才能提供的决策。**

## 一、第一次事故：Agent 找到了两种正确方案，却不知道该选哪个

还是优惠券退款系统。

用户要求：

> 修复优惠券退款计算，并确保未来可以支持新的优惠券类型。

Coding Agent 调查后发现两种方案。

**方案 A：最小修改**

保留当前退款计算结构，只修复优惠券金额分摊公式。

优点是修改范围小、兼容性风险低。

缺点是未来每增加一种优惠券，都可能需要增加特殊逻辑。

**方案 B：重构优惠券计算模块**

引入策略模式，让不同优惠券使用独立的计算规则。

优点是扩展性较好。

缺点是修改范围更大，需要更多回归测试，可能影响交付时间。

两个方案都可行。

但应该选哪个？

这取决于一个 Agent 无法从代码里推断的业务事实：

> 当前项目是优先修复线上 Bug，还是允许投入更多时间做架构重构？

小李发现，如果让模型自己做决定，就有可能违背用户真实意图。

于是他决定：遇到这类问题时，让 Agent 向用户提问。

---

## 二、第一次改进：让模型直接输出问题，不就可以了吗？

最简单的实现方式是：

```text
Agent：
我发现两种修复方案。

A：最小修改
B：架构重构

请选择一种。
```

然后 Agent 结束当前 Turn。

等用户回复：

```text
用户：选择方案 A。
```

再开始下一轮 Turn。

对于普通对话，这完全合理。

但在我们的长任务场景中，小李发现了一个问题。

用户回答之前，Agent 已经做了大量工作：

```text
已完成：
- 阅读优惠券相关代码
- 确认退款业务规则
- 分析 API 兼容性
- 找出两种修复方案

等待：
- 用户选择方案 A 或 B

后续：
- 根据选择修改代码
- 添加回归测试
- 执行验证
```

如果直接结束 Turn，下一轮执行就需要重新理解当前进度。

虽然可以通过 History 和 Memory 恢复信息，但我们实际上希望表达的是：

**任务没有结束，只是暂时缺少继续执行的条件。**

这与正常完成任务是两回事。

于是小李想到了：

> 能不能让 Agent 保持任务状态，暂停在当前阶段，等用户回答后再继续？

这就是我们需要的 Human-in-the-Loop。

---

## 三、先区分三种人类介入

之前我们已经学习过 Permission & Approval。

但 Human-in-the-Loop 的范围更大。

例如：

| 场景 | 人类提供什么 | Agent 为什么需要等待 |
|---|---|---|
| 删除用户文件 | 批准或拒绝 | 操作需要授权 |
| 选择优惠券架构 | 方案决策 | 缺少业务偏好 |
| 测试环境缺少配置 | 环境信息 | 缺少执行所需输入 |

三种情况虽然都需要人类，但背后的问题不同。

**Approval（审批）**解决的是：Agent 是否有权执行某项操作。

**Clarification（澄清）**解决的是：Agent 是否准确理解用户要求。

**Decision（决策）**解决的是：多个可行方向中，应该选择哪个。

它们可以共享一套暂停与恢复机制，但输入内容、校验方式和后续执行逻辑不同。

到这里，我们可以给 HITL 一个更准确的理解：

> Human-in-the-Loop 是将人类输入纳入 Agent 执行控制流，使任务能够在需要人工介入时暂停，并根据新的信息继续执行。

注意，HITL 的关键不是问问题本身，而是**问题与原来尚未完成的执行任务之间的关联**。

---

## 四、第二次改进：为什么不能直接使用 input() 等待？

小李写下了一个很简单的实现。

```python
def choose_strategy():
    answer = input("选择方案 A 还是 B？")
    return answer
```

当 Agent 执行到需要决策的位置，就调用这个函数。

对于本地 CLI Demo，确实可以工作。

但上线以后，问题出现了。

用户打开 Web 页面提交任务，Coding Agent 运行在服务器上。

当 Agent 请求人类输入时：

```text
Web Client
     |
     v
Agent Server
     |
     v
input() 等待
```

谁来向服务器终端输入？

更麻烦的是，用户可能几个小时后才回答。

如果每个等待的任务都占据一个执行线程，随着用户数量增长，系统会浪费大量资源。

而且进程可能在等待期间重启。

小李意识到：

**等待用户输入，不应该等同于让某个 Python 函数一直阻塞。**

对于长时间等待的任务，系统更需要的是：

```text
Agent 正在执行
      |
      v
发现需要人工输入
      |
      v
保存当前执行状态
      |
      v
释放当前执行资源
      |
      v
等待用户回答
      |
      v
重新加载执行状态
      |
      v
继续任务
```

我们由此引入两个概念：

**Interrupt（中断）**：当前任务暂停，等待外部输入。

**Resume（恢复）**：外部输入到达后，继续尚未完成的执行。

这与异常中断不同。

这里的 Interrupt 是一种正常的控制流状态。

---

## 五、第三次改进：暂停时究竟需要保存什么？

现在小李已经决定把等待过程改成非阻塞的任务状态。

但下一个问题自然出现：

> 用户回答后，系统怎么知道应该恢复哪个任务？

假设有两个任务正在等待。

```text
Task A：
  用户需要选择退款方案。

Task B：
  用户需要决定是否保留旧订单 API。
```

两个任务都属于同一个用户。

如果用户提交：

```text
选择方案 A。
```

我们不能仅根据用户 ID 判断这条答案属于哪个任务。

于是需要一份明确的等待记录。

```json
{
  "request_id": "req-101",
  "thread_id": "refund-task-001",
  "type": "user_decision",
  "status": "waiting",
  "question": "选择哪种修复方案？",
  "options": ["minimal_fix", "refactor"]
}
```

这个结构是我们推导出来的教学设计，不是某个框架的原始接口。

其中几个字段很重要：

`request_id` 表示当前这一次人工输入请求。

`thread_id` 将请求关联到原来的 Agent 执行状态。

`status` 表示任务当前处于等待状态。

当用户返回：

```json
{
  "request_id": "req-101",
  "answer": "minimal_fix"
}
```

系统才能把这个答案交还给正确的执行任务。

这一步看起来简单，但实际涉及：

**请求关联（Correlation）**。

它与之前工具调用使用 `tool_call_id` 关联工具结果，是相似的设计思想。

不过我们还没有解决最关键的事情：

**真正的 Agent 执行状态保存在哪里？**

---

## 六、源码对照：LangGraph 的 interrupt() 为什么重要？

现在看 LangGraph。

[LangGraph Interrupts 官方文档](https://docs.langchain.com/oss/python/langgraph/interrupts)

LangGraph 提供一个非常关键的函数：

```python
interrupt(value)
```

它的作用不是普通地向终端打印一条问题。

而是告诉 Graph Runtime：

> 当前节点需要暂停，把指定信息返回给外部调用方，等待恢复输入。

我们先实现最小版本。

### 6.1 一个真正能够暂停和恢复的 Graph

```python
from typing import TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt, Command
from langgraph.checkpoint.memory import InMemorySaver


class State(TypedDict):
    strategy: str | None


def choose_strategy(state: State):
    answer = interrupt({
        "question": "选择退款修复方案",
        "options": ["minimal_fix", "refactor"],
    })

    if answer not in {"minimal_fix", "refactor"}:
        raise ValueError("无效的方案选择")

    return {"strategy": answer}


builder = StateGraph(State)
builder.add_node("choose", choose_strategy)
builder.add_edge(START, "choose")
builder.add_edge("choose", END)

graph = builder.compile(
    checkpointer=InMemorySaver()
)
```

这是使用真实 LangGraph API 的最小示例。

为了便于理解，当前只实现了一个决策节点；实际 Coding Agent 会在后面继续执行代码修改和测试。

现在运行：

```python
config = {
    "configurable": {
        "thread_id": "refund-task-001"
    }
}

result = graph.invoke(
    {"strategy": None},
    config,
)

print(result["__interrupt__"])
```

此时 Graph 不会正常走到 END。

而是暂停在 `interrupt()`。

外部程序能收到问题内容。

当用户选择：

```text
minimal_fix
```

再次调用：

```python
result = graph.invoke(
    Command(resume="minimal_fix"),
    config,
)

print(result["strategy"])
# minimal_fix
```

Graph 就继续运行，最终完成。

这里有三个细节值得深入理解。

### 6.2 为什么需要 Checkpointer？

因为 Graph 必须记住任务此前执行到了什么状态。

```text
Thread refund-task-001
    |
    v
Checkpoint
    |
    +-- 当前 State
    +-- 当前执行位置
    +-- Interrupt 信息
```

注意，这是一张概念图，不代表底层 Checkpoint 就以三个字段存储。

`InMemorySaver` 只能用于简单演示，它不提供跨进程重启的持久性。生产环境应该使用持久化 Checkpointer。

### 6.3 为什么 Resume 必须使用相同的 thread_id？

因为：

```text
thread_id = refund-task-001
```

就是定位原来执行状态的重要标识。

如果改成：

```text
thread_id = refund-task-002
```

系统就不能将新的答案当作原任务的恢复输入。

这和之前研究 Checkpoint 时的 Thread 恢复机制一致。

### 6.4 为什么使用 Command(resume=...)？

因为用户答案不只是一个新的普通聊天消息。

它需要成为之前 `interrupt()` 调用的返回值。

也就是说：

```python
answer = interrupt(...)
```

在恢复后：

```python
answer == "minimal_fix"
```

然后节点可以继续根据答案执行后续逻辑。

这个设计很巧妙：

**从节点的代码视角看，我们像是在等待一个函数返回；但从 Runtime 视角看，执行已经经历了暂停、状态保存和重新调用。**

---

## 七、第四次事故：为什么 Resume 可能重复执行代码？

小李理解了 Interrupt，于是写出：

```python
def choose_strategy(state):
    audit_log.write("开始询问用户")

    answer = interrupt({
        "question": "选择方案 A 或 B"
    })

    return {"strategy": answer}
```

第一次调用：

```text
写入审计日志
    |
    v
interrupt()
    |
    v
暂停
```

用户回答后，Graph 恢复。

但日志里出现了两条：

```text
开始询问用户
开始询问用户
```

为什么？

因为 LangGraph 的动态 Interrupt 并不是保存 Python 函数在某个机器指令处的完整调用栈。

**恢复时，包含 `interrupt()` 的节点会从头重新执行。**

这意味着 `interrupt()` 之前的代码会再次运行。

这正是 LangGraph 源码和官方文档特别强调的行为。

### 为什么要采用这种实现？

因为保存任意 Python 调用栈并在任意机器指令位置恢复，远比恢复 Graph 的节点状态复杂。

LangGraph 选择在 Graph State 和节点执行边界实现可恢复执行。

这是一种明确的工程取舍。

但它也带来要求：

**不要在 Interrupt 前执行无法安全重复的副作用。**

例如：

```python
def choose_strategy(state):
    charge_customer()  # 危险：恢复后可能再次执行

    answer = interrupt("是否继续？")

    return {"answer": answer}
```

这样设计可能造成重复扣款。

### 怎么改进？

最简单的办法是把人工决策放在副作用之前：

```python
def choose_strategy(state):
    answer = interrupt("选择方案 A 或 B")

    return {"strategy": answer}
```

之后再由独立节点执行需要的修改。

```text
choose_strategy
      |
      | Interrupt / Resume
      v
apply_strategy
      |
      v
run_tests
```

如果确实需要在中断前执行外部操作，就需要使用幂等键、可恢复 Task、事务等机制保证安全重放。

这再次连接了前面学过的：

- Checkpoint
- Durable Execution
- Idempotency

**可恢复执行不意味着任意代码都不会重复执行。**

---

## 八、第五次改进：用户回答以后，如何真正影响 Agent 后续行动？

现在用户选择了：

```text
minimal_fix
```

Graph 恢复并保存：

```json
{
  "strategy": "minimal_fix"
}
```

但原来的 Coding Agent 还需要继续调用 LLM。

那么问题出现了：

> Agent 怎样知道用户刚才做出了这个决定？

如果用户答案只保存在应用层数据库里，而没有进入当前模型可见的上下文，模型仍然可能不知道应该采取哪种方案。

所以恢复执行至少涉及两件事。

第一，**更新执行状态**。

```text
strategy = minimal_fix
```

第二，**让后续模型调用能够看到这项决策**。

例如 Context Builder 可以加入：

```text
用户已确认：

选择最小修复方案。

约束：
- 不重构优惠券架构
- 不修改订单 API
- 修复现有退款计算
- 补充必要的回归测试
```

注意：

**恢复 Graph 执行，与把用户的新决定传递给 LLM，是两个相关但不同的问题。**

HITL 不仅要恢复控制流，还要确保人类提供的信息成为下一步决策的有效依据。

这也是 Human-in-the-Loop 与 Context Management 的直接连接点。

---

## 九、源码对照：OpenCode 为什么把提问实现成 Tool？

现在看 OpenCode。

[OpenCode V2 Tools 文档](https://opencode.ai/v2/docs/tools)

OpenCode 提供 `question` 工具。

模型在执行过程中，可以调用它询问用户。

例如：

```text
question:
  请选择退款修复方案。

  A. 最小修改
  B. 架构重构
```

用户在 TUI、Web 或其他支持交互的客户端中作出选择。

### 9.1 OpenCode 的 Question Service

阅读：

[OpenCode question.ts](https://github.com/anomalyco/opencode/blob/dev/packages/core/src/question.ts)

当前实现有四个关键操作：

```typescript
ask(...)
reply(...)
reject(...)
list(...)
```

为什么需要这四个操作？

`ask` 负责创建问题并等待回答。

`reply` 负责提交用户答案。

`reject` 负责处理用户取消。

`list` 负责查看当前未完成的请求。

内部还使用：

```typescript
const pending = new Map<ID, Pending>()
```

每个 Pending Request 包含请求信息和一个 Deferred。

可以将其理解成：

```text
Question Tool
      |
      v
Question Service
      |
      +-- 创建 request_id
      +-- 保存 Pending
      +-- 发布 question.asked
      |
      v
UI 显示问题
      |
      v
用户回答
      |
      v
Question Service.reply()
      |
      v
完成对应 Deferred
      |
      v
Tool 返回结果给 Agent
```

实际 `ask` 的关键结构是：

```typescript
const deferred = yield* Deferred.make()
pending.set(id, { request, deferred })
```

这两行体现了核心设计：使用请求 ID 和异步等待对象，将用户回答关联回原来的工具调用。

当用户回答时，服务发布 `question.replied` 事件，再完成对应的 Deferred。

当用户取消时，则发布 `question.rejected` 并拒绝相应等待。

### 9.2 为什么 OpenCode 不直接使用 LangGraph Interrupt？

因为 OpenCode 有自己的 Session Runtime、Tool System 和 UI 事件机制。

它可以把等待用户答案实现成一个普通的异步工具调用。

模型请求 `question`，Tool Runtime 等待答案，得到结果后继续执行 Agent Loop。

这与 LangGraph 的设计不同：

| | LangGraph Interrupt | OpenCode Question |
|---|---|---|
| 核心抽象 | Graph 执行中断 | 异步 Tool + Question Service |
| 等待机制 | Checkpoint + Resume | Pending Request + Deferred |
| 用户回答 | `Command(resume=...)` | `reply(requestID, answers)` |
| 执行恢复 | Graph 根据保存状态恢复 | 等待中的工具调用获得返回值 |
| 主要用途 | 通用可恢复控制流 | 任务执行中向用户提问 |

这里有一个重要区别：

**OpenCode Question 的 Deferred 不能直接等同于 LangGraph 的持久化 Checkpoint。**

根据当前 `QuestionV2` 的源码，Pending Request 保存在服务内存 Map 中，并在服务生命周期结束时执行清理。

所以不能因为它支持异步等待，就认定正在等待的 Question 在进程重启后一定还能恢复。

这说明 HITL 可以有两种不同的实现需求：

- 同一进程内，异步等待用户回答。
- 即使执行进程退出，之后仍能从持久状态恢复。

二者的工程复杂度不同。

---

## 十、源码对照：Codex 如何向用户提问？

Codex 也支持向用户请求输入。

阅读：

[Codex request_user_input.rs](https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/handlers/request_user_input.rs)

其中关键代码是：

```rust
let accepted = session
    .request_user_input(turn.as_ref(), call_id.clone(), args)
    .await
    .ok_or_else(|| {
        FunctionCallError::RespondToModel(
            "request_user_input was cancelled".to_string()
        )
    })?;
```

这是对真实源码的精简，省略了部分错误文本。

我们重点关注：

```rust
session.request_user_input(...)
```

为什么由 Session 来处理？

因为 Codex 的用户输入不仅关系到某个 Tool Handler，还关系到：

- 当前 Turn。
- 当前 Tool Call。
- 客户端交互。
- 用户回答的关联。
- 取消和模式限制。

尤其值得注意的是，源码明确检查当前 Agent 是否为 Root Agent。

当前实现不允许非 Root Agent 直接使用这个工具请求用户输入。

此外，工具的可用性也受 Collaboration Mode 和运行时配置约束，并不是每个 Codex Turn 都能无条件调用。

为什么要限制子 Agent？

一个合理的架构考虑是：

如果多个 Subagent 可以随时直接向用户提问，用户可能收到大量无法判断上下文的问题。

因此可以由主 Agent 统一管理人类交互。

不过，这里要区分设计推导和源码事实：源码明确实现了 Root Agent 的调用限制，而具体产品为何如此取舍，还不能仅凭这一判断推断出全部设计动机。

### Codex 与 OpenCode 的共同点

两者都可以将用户输入建模为 Agent 执行过程中的异步交互。

```text
Agent 发起请求
      |
      v
Runtime 建立等待
      |
      v
客户端向用户展示
      |
      v
用户提交答案
      |
      v
Runtime 恢复 Tool Result
      |
      v
Agent 继续
```

但这类异步请求的存在，并不自动等同于完整的 Durable Execution。

是否能在进程重启后恢复，必须另外查看对应的 Session 持久化和等待请求恢复实现。

---

## 十一、DeepAgents 怎么处理 Human-in-the-Loop？

这里需要区分两个层面。

**DeepAgents 自身最直接提供的是工具审批型 HITL。**

例如：

```python
agent = create_deep_agent(
    model=model,
    interrupt_on={"edit_file": True},
    checkpointer=checkpointer,
)
```

当 Agent 准备修改文件时，底层 `HumanInTheLoopMiddleware` 会暂停执行，等待审批。

而我们本章讨论的：

```text
请选择方案 A 还是 B
```

属于更一般的人类业务决策。

它可以借助 LangGraph 的 `interrupt()` 实现，也可以设计为专门的 Ask User Tool。

这两种模式不需要完全相同的接口。

### HITL Middleware 与通用 Interrupt 的区别

**HITL Middleware** 更适合：

```text
模型提出工具调用
        |
        v
执行前请求人工审核
```

**通用 Interrupt** 更适合：

```text
Graph 执行到某个业务决策点
        |
        v
暂停并请求外部信息
```

两者都可以复用 LangGraph 的持久化执行机制。

因此，DeepAgents 的启发在于：

**权限审批可以作为 Middleware 统一处理，而任意业务决策则需要由具体任务或工具明确表达。**

---

## 十二、第六次事故：用户一直不回答怎么办？

现在系统已经能够等待用户。

但某个任务已经等待了三天。

这时出现新问题：

> 一个 Human-in-the-Loop 请求应该允许无限期等待吗？

答案取决于任务。

有些业务决策可以长期等待。

有些任务则有明确时间限制，例如部署窗口、测试环境租期或外部凭据有效期。

所以小李决定增加等待状态管理。

```text
RUNNING
   |
   v
WAITING_FOR_HUMAN
   |
   +--> ANSWERED
   |
   +--> REJECTED
   |
   +--> TIMED_OUT
   |
   +--> CANCELLED
```

这是我们设计的逻辑状态机，不是某个框架的固定枚举。

### 为什么超时后不能直接使用默认答案？

假设用户未回答：

```text
A：保留兼容性
B：允许破坏性 API 修改
```

如果系统超时后自动选择 B，就可能做出用户从未授权的决定。

因此，超时处理应该由业务策略决定。

例如：

- 低风险偏好可以使用预先声明的默认值。
- 高风险操作应保持阻塞或终止。
- 已失效的任务应该重新询问。
- 不可恢复的任务应明确报告失败。

**超时不是隐含批准。**

另外，还必须考虑用户在等待期间改变了项目状态。

例如，用户回答之前，其他开发者已经修改了代码。

恢复后不应该直接按照几天前的文件内容执行补丁，而应该重新验证当前环境。

这实际上连接了前面研究的：

- Checkpoint
- Workspace
- Permission
- Task Validation

---

## 十三、最后一个问题：用户回答会改变计划，还是只影响下一步工具？

回到我们的优惠券 Coding Agent。

用户选择：

```text
minimal_fix
```

Agent 当前计划是：

```text
1. 定位问题
2. 重构优惠券模块
3. 修改退款逻辑
4. 添加测试
```

很明显，第二步已经不符合用户的新决定。

如果只是把答案加进 Context，却不调整 Planning State，Agent 仍然可能执行过时计划。

所以 HITL 之后的流程不应该简单写成：

```text
收到答案 -> 原样继续
```

更合理的是：

```text
收到答案
    |
    v
验证答案与请求关联
    |
    v
更新 Agent State
    |
    v
检查计划是否仍然有效
    |
    +--> 有效：继续
    |
    +--> 失效：重新规划
    |
    v
Context Builder 组装最新信息
    |
    v
Agent Loop 继续执行
```

这里我们发现：

**Human-in-the-Loop 不只是给 Agent 增加用户输入，还可能改变任务后续的控制流。**

例如，选择最小修复方案后，原先的架构重构任务就应该被取消或替换。

这也是 HITL 与 Planning 的直接联系。

---

## 十四、最终整理：Human-in-the-Loop 在 Harness 中的位置

现在小李已经推导出比较完整的设计。

```text
                   Agent Loop
                       |
                       v
                 LLM / Tool
                       |
                       v
              Need Human Input?
                       |
              +--------+--------+
              |                 |
              No               Yes
              |                 |
              v                 v
           继续执行       Human Request
                                |
                                v
                        等待请求管理
                                |
                     +----------+----------+
                     |                     |
                     v                     v
               保存执行状态           通知用户
                     |                     |
                     +----------+----------+
                                |
                                v
                           用户回答
                                |
                                v
                        校验与关联输入
                                |
                                v
                          Resume Runtime
                                |
                                v
                       更新 State / Plan
                                |
                                v
                            Agent Loop
```

这张图是逻辑模型，不要求所有 Harness 都采用独立的 Request Manager 类。

根据实际需求，可以选择：

**简单模式**：Agent 使用 Question Tool 异步等待当前进程内的用户回答。

**持久模式**：Agent 暂停执行，保存 Checkpoint，释放执行资源，之后重新加载状态并恢复。

两者都属于 HITL 的实现方式，但具有不同的恢复能力。

---

## 十五、三个 Harness 的实现对比

| 关注点 | LangGraph / DeepAgents | OpenCode | Codex |
|---|---|---|---|
| 典型入口 | `interrupt()`、HITL Middleware | `question` Tool | `request_user_input` |
| 人类输入方式 | Resume Value | Question Answer | Session Input Response |
| 请求关联 | Thread / Interrupt ID | Request ID / Session ID | Turn / Tool Call ID |
| 暂停机制 | Graph Interrupt | Deferred 异步等待 | Session 请求等待 |
| 状态持久化 | Checkpointer | 需区分问答等待与 Session 持久化 | 需区分问答等待与 Rollout 持久化 |
| 典型用途 | 审批和通用工作流中断 | 执行中澄清需求 | 任务执行中的用户决策 |

从架构上看，三者的共同点是：

**人类输入必须进入 Agent 的执行控制流，而不是仅仅成为一条与当前任务无关的新聊天消息。**

但各自的实现边界不同。

LangGraph 更明确地提供持久化 Graph 中断与恢复原语。

OpenCode 更强调通过工具与 UI 事件完成交互。

Codex 将用户输入请求与 Session、Turn 和客户端通信相结合。

---

## 十六、如何验证 HITL 真的正确？

小李最后准备了一组测试。

| 故障场景 | 预期结果 |
|---|---|
| 用户选择方案 A | Agent 按方案 A 继续 |
| 用户拒绝提供答案 | 不假装已有有效决策 |
| 同一用户有两个等待任务 | 回答不会串到错误任务 |
| 同一请求收到两次回答 | 不重复执行后续副作用 |
| 任务等待期间进程重启 | 只有持久模式承诺恢复 |
| 恢复前代码已经变化 | 重新验证执行前提 |
| 用户改变原始需求 | 必要时重新规划 |
| 用户等待期间取消任务 | 清理待处理请求 |
| Interrupt 前存在副作用 | 验证恢复时是否重复执行 |

最重要的一个测试是：

**证明恢复后不会意外重复执行已经发生的外部副作用。**

对于 LangGraph，还必须专门验证节点从头重新执行的行为。

对于基于 Deferred 的问答工具，则必须验证等待请求在服务退出、取消及连接断开时的清理策略。

---

## 十七、本章的五个核心问题

### 1. Human-in-the-Loop 是什么？

它是允许 Agent 在执行过程中暂停、请求人类信息，并利用人类反馈继续执行的控制机制。

### 2. 为什么需要？

因为 Agent 无法仅靠模型和工具解决所有不确定性。有些信息属于用户偏好、业务决策或授权范围，必须由人类提供。

### 3. 没有它会怎样？

Agent 只能猜测、直接结束任务，或者在缺少关键信息时继续执行，导致目标偏移或错误操作。

### 4. 实际怎么做？

简单场景可以使用 Question Tool 异步等待回答。

需要跨进程持久恢复时，可以使用 Interrupt、Checkpoint 和 Resume。

恢复后还需要验证答案、更新 State，并根据需要重新规划。

### 5. 在整个 Harness 中处于什么位置？

HITL 是横跨 Agent Loop、Tool System、Session、State、Context 和 Checkpoint 的控制机制。

它不是一个孤立的提问组件。

---

## 本章总结

最开始，Coding Agent 遇到业务决策时只能直接问用户，然后结束任务。

随着任务变长、用户回答变慢、系统需要跨进程恢复，我们逐步引入了：

```text
Human Request
      |
      v
Interrupt / Async Wait
      |
      v
Request Correlation
      |
      v
Checkpoint（需要持久恢复时）
      |
      v
Human Response
      |
      v
Resume
      |
      v
State / Plan Update
      |
      v
Agent Loop
```

最终要理解的是：

> **Human-in-the-Loop 的核心，不是让 Agent 会问问题，而是让人类决策成为 Agent 可管理、可关联、必要时可持久恢复的执行事件。**

人类输入可能改变下一步工具调用、整个任务计划，甚至决定任务应该继续还是取消。

这正是 HITL 应该属于 Agent Runtime，而不只是聊天 UI 的原因。

## 真实源码阅读路线

### LangGraph：重点研究真正的暂停与恢复

- [LangGraph Interrupts 官方文档](https://docs.langchain.com/oss/python/langgraph/interrupts)
- [LangGraph types.py](https://github.com/langchain-ai/langgraph/blob/main/libs/langgraph/langgraph/types.py)
- [LangGraph Pregel Loop](https://github.com/langchain-ai/langgraph/blob/main/libs/langgraph/langgraph/pregel/_loop.py)

重点：`interrupt()`、`Command(resume=...)`、Checkpoint、节点重执行。

### DeepAgents / LangChain：重点研究审批型 HITL

- [DeepAgents graph.py](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/graph.py)
- [HumanInTheLoopMiddleware](https://github.com/langchain-ai/langchain/blob/master/libs/langchain_v1/langchain/agents/middleware/human_in_the_loop.py)

重点：工具调用如何被暂停，以及审批如何与 Graph 恢复结合。

### OpenCode：重点研究异步 Question Tool

- [OpenCode Question Tool](https://github.com/anomalyco/opencode/blob/dev/packages/core/src/tool/question.ts)
- [OpenCode Question Service](https://github.com/anomalyco/opencode/blob/dev/packages/core/src/question.ts)
- [OpenCode V2 Tools](https://opencode.ai/v2/docs/tools)

重点：`ask()`、`reply()`、`reject()`、Deferred、Question Events。

### Codex：重点研究 Session 如何管理用户请求

- [Codex request_user_input.rs](https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/handlers/request_user_input.rs)
- [Codex Collaboration Modes](https://github.com/openai/codex/tree/main/codex-rs/collaboration-mode-templates/templates)

重点：`request_user_input()`、Tool Call ID、模式约束和用户答案回传。

---

**下一章：Model Adapter——为什么 Agent Loop 不应该直接依赖某个模型提供商的 API？**

我们的 Coding Agent 目前只支持一种模型。

现在公司希望同时支持 OpenAI、Anthropic 和其他模型。

但不同模型的消息格式、Tool Calling、流式响应、推理信息和上下文限制并不完全相同。

我们将从一次模型切换失败开始，逐步推导 Model Adapter、Provider Abstraction、Stream Normalization 和 Capability Negotiation，并比较 Codex、OpenCode 与 LangChain 的实际实现。
