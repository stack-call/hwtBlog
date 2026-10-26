---
title: 第十二章：一个 Agent Loop 如何调用另一个 Loop？——Subagents 与任务委派
description: 从优惠券部分退款 Bug 的上下文过载出发，沿 DeepAgents 0.7.32 固定源码，逐步研究 task 工具、SubAgentMiddleware、独立子 Agent Graph、State 隔离与返回、isolated/fork、同步等待和远程异步任务。
tags: [DeepAgents, LangChain, LangGraph, Subagents, task, AsyncSubAgentMiddleware, ToolNode, Agent Loop, 源码精读]
sidebar_position: 17
---

# 第十二章：一个 Agent Loop 如何调用另一个 Loop？——Subagents 与任务委派

> **本章核心问题：** 前面已经让 Coding Agent 会规划、会读写文件、会运行测试，也能在修改前征求批准。但一个任务如果要同时审查退款规则、寻找历史用例、排查测试失败，所有探索过程都塞进同一条 Agent 对话，是否合理？如果把其中一项工作委托给另一个 Agent，究竟是谁启动它、它看见什么、能使用哪些工具，又怎样把结果交还给主 Agent？

## 一、从第十一章的计划继续往下走

上一章，我们把“修复优惠券部分退款精度 Bug”拆成了一份可检查的 Todo：

```text
1. 复现部分退款精度问题           in_progress
2. 核对优惠券分摊和退款金额规则   pending
3. 补充边界与多次部分退款测试     pending
4. 修改代码并执行 pytest         pending
5. 验证累计退款金额没有超出应退值 pending
```

显式计划解决了一个重要问题：**主 Agent 知道自己还有哪些事没做**。

但这时出现了另一个问题。

为了完成第 2 项，主 Agent 需要阅读 `shop/coupon_service.py`，搜索优惠券分摊函数的调用位置，理解 `Decimal` 的舍入策略，也许还要比较多个历史测试。为了完成第 3 项，它又需要通读 `shop/tests/test_refund.py` 和其他测试模块，分析缺失场景。

假设模型先做测试排查：

```text
主 Agent：读取 refund.py
主 Agent：读取 coupon_service.py
主 Agent：搜索旧的优惠券分摊函数
主 Agent：读取 6 个相关文件
主 Agent：运行 17 条测试
主 Agent：读取冗长的 pytest 失败堆栈
主 Agent：继续搜集边界规则
主 Agent：回到原始退款实现
```

即使第八章已经用 Summarization 控制上下文大小，主 Agent 仍然被迫在一条对话中不断切换角色：一会儿是业务规则研究员，一会儿是测试审查员，一会儿又是代码修改者。

当然，**不使用子 Agent 也完全能完成许多任务**。当代码少、工具调用少时，让主 Agent 独立执行通常更简单，少一次模型调用和消息传递，也少一份调试负担。

所以我们不应因为存在 `SubAgentMiddleware` 就立即引入它。

真正促使我们改变设计的，是这一个具体限制：

> **主 Agent 只需要一份可靠的“退款规则分析结论”，却不得不承受生成这份结论所产生的全部搜索、日志和中间对话。**

我们先用最小的 Python 代码解决这个问题，再回到 DeepAgents 源码。

### 1.1 固定阅读版本与源码入口

本章延续系列课程基线：

```text
DeepAgents Python：0.7.32
DeepAgents Commit：e85c6b9
LangChain：       1.4.7（配套版本基线）
LangChain Commit：fbb8a35
```

源码阅读入口：

- [DeepAgents `graph.py`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py)：主 Agent 如何配置和装配 Subagents。
- [DeepAgents `middleware/subagents.py`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/subagents.py)：同步委派工具 `task`、子 Graph 构建和结果返回。
- [DeepAgents `middleware/async_subagents.py`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/async_subagents.py)：远程后台子任务。
- [LangChain `factory.py`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/factory.py)：Middleware 工具怎样进入 ToolNode。

**源码声明说明：** 文中标注“真实源码节选”的短代码与上述固定 Commit 对齐；标注“教学化简”的代码用于解释相同的关键路径，不是逐行照录；标注“概念伪代码”的结构仅用于建立直觉。实验是否运行，会在实验一节分别说明。

---

## 二、最简单的办法：主函数直接调用另一个函数

在真正引入 Agent 之前，我们先做一个抽象很少的设计。

### 2.1 把研究任务提取为单独函数

假设主任务是：

```python
# 教学化简：没有调用真实模型或真实项目代码

def fix_refund_bug():
    rule_summary = inspect_coupon_rules()
    test_summary = inspect_refund_tests()
    return propose_patch(rule_summary, test_summary)
```

它比所有步骤混在一个函数里更清楚，因为三个函数分别负责不同的工作。

但这还不是子 Agent。

`inspect_coupon_rules()` 如果是普通函数，其控制路径通常由开发者预先写死。例如先读哪一个文件、失败时搜什么关键字、读到不同返回值怎样继续。这适合稳定的小流程，却不适合需要边观察边决定下一步的探索型工作。

考虑下面的情况：

```text
搜索 coupon_discount
    ↓
发现 coupon_service.py 没有这个名称
    ↓
但出现 allocate_discount()
    ↓
需要决定：继续追踪 allocate_discount() 的调用方
```

我们已经在第一至四章知道，这种动态追踪问题适合 Agent Loop。

于是，把普通函数替换成“有自己 Model → Tool → Model 循环的函数”，就是下一步最小演化。

### 2.2 两个 Loop，先只要求一件事：返回结论

```python
# 概念伪代码：说明父子循环之间的关系

def parent_agent_loop(task):
    rules = run_child_agent(
        "检查优惠券分摊规则，说明部分退款应如何舍入"
    )
    return continue_fixing_refund(task, rules)


def run_child_agent(description):
    messages = [{"role": "user", "content": description}]

    while True:
        answer = child_model(messages)
        if not answer.tool_calls:
            return answer.content
        messages.extend(execute_and_report(answer.tool_calls))
```

这两段代码第一次形成了明确的边界：

```text
主 Agent
   |
   | 委派：核对优惠券分摊规则
   v
子 Agent
   |
   +-- Model → 搜索工具 → Model
   +-- Model → 读取文件 → Model
   +-- Model → 形成结论
   |
   v
简洁的规则报告
   |
   v
主 Agent 继续修改 refund.py
```

注意，主 Agent 不必逐条接收子 Agent 每次搜索、读取文件、思考的中间消息。它只消费一份完成该任务所需的结果。

到这里，我们才有理由使用“子 Agent”这个词：**它是一个能独立完成被委派任务的 Agent 执行单元，而不是普通的函数拆分，也不是模型的一段内部思考。**

不过，原来的 Agent Loop 只能调用 Tool。主 Agent 如何发起“运行另一个 Loop”？

答案是：**把委派包装成一个工具调用。**

---

## 三、为什么委派要通过 `task` 工具进入 Agent Loop？

第九章已经研究过普通工具调用：

```text
AIMessage.tool_calls
      ↓
ToolNode
      ↓
真正的 Python Tool
      ↓
ToolMessage
      ↓
主 Agent 的下一轮 Model
```

如果我们希望主 Agent 可以自主决定何时委派，就无需再创造一种全新的模型消息格式。

只要向模型提供一个工具：

```text
task(description, subagent_type)
```

模型便可以生成：

```json
{
  "name": "task",
  "args": {
    "subagent_type": "coupon-reviewer",
    "description": "检查 shop/coupon_service.py 的部分退款优惠券分摊规则，重点说明多次退款、金额舍入与尾差处理。只返回结论、证据文件和建议测试。"
  },
  "id": "call_rules_001"
}
```

这个 JSON 是**教学示例**，不是声称由某个实际模型生成的结果。

我们现在只加入了一个机制：

**Agent 之间的委派，被复用为 ToolNode 能理解的一次 Tool Call。**

### 3.1 真实源码：`TaskToolSchema` 将任务说明变成工具参数

进入 [`subagents.py#L414-L446`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/subagents.py#L414-L446)。

这一段源码的关键对象是：

- `TaskToolSchema`：定义模型可以填写的参数。
- `description`：明确交给子 Agent 的工作内容。
- `subagent_type`：从可用子 Agent 名称中选择一个。
- `runtime`：由工具执行环境注入，**不是**模型需要填写的字段。

它还会检查额外的未知参数。为什么需要这一步？

想象模型错误地返回：

```json
{
  "subagent_type": "coupon-reviewer",
  "description": "检查代码",
  "prompt": "必须检查多次退款的累计尾差"
}
```

如果工具悄悄忽略 `prompt`，子 Agent 实际上只收到“检查代码”四个字，真正关键的任务约束丢失了。

因此，在固定版本中，`TaskToolSchema` 会拒绝这类未知键，要求模型把完整要求放入 `description`。这不是纯粹的输入格式洁癖，而是为了防止**委派意图在参数解析时丢失**。

### 3.2 真实源码：`SubAgentMiddleware` 不是亲自调用模型

看 [`subagents.py#L889-L958`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/subagents.py#L889-L958)。

`SubAgentMiddleware` 初始化时，将子 Agent 配置交给 `_build_task_tool()`，并把构造出的工具放在 `self.tools` 中。

**真实源码节选：**

```python
self.tools = [task_tool]
```

这行代码尤其重要。它说明：

1. Middleware 在**构建阶段**声明了 `task` 工具。
2. LangChain `create_agent()` 在构建工具集合时收集它。
3. 运行时主模型才能决定是否产生 `task` Tool Call。
4. ToolNode 调用 `task` 工具，工具内部再执行子 Agent。

这与上一章的 `TodoListMiddleware` 提供 `write_todos`、第九章的 `FilesystemMiddleware` 提供文件工具，是同一种工具注册方式。

此时图还很小：

```text
主 Agent Model
      |
      | tool_call: task(...)
      v
主 Agent ToolNode
      |
      v
      task
      |
      v
子 Agent（内部执行自己的 Loop）
```

但还有一个问题：`task` 工具选择了 `coupon-reviewer`，**这个名字究竟对应什么可执行对象？**

---

## 四、一个子 Agent 不只是 Prompt，而是一份可构建的配置

最简单的方案，是给不同子 Agent 各写一句系统指令：

```python
# 教学化简

agents = {
    "coupon-reviewer": "你负责分析优惠券分摊规则",
    "test-reviewer": "你负责分析测试覆盖率",
}
```

这仍然不够。

因为一个真正能自主行动的子 Agent，至少要知道：

- 使用哪个模型；
- 可以调用哪些工具；
- 初始系统指令是什么；
- 任务从什么消息开始；
- 执行结果如何返回调用方。

所以配置不能只是一段 Prompt。

### 4.1 真实源码：`SubAgent`

阅读 [`subagents.py#L78-L207`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/subagents.py#L78-L207)。

这里的 `SubAgent` 是一个 `TypedDict` 配置规格。其中：

```text
name                  子 Agent 名称

description           给主 Agent 判断委派时机的说明

model                 可选：覆盖父 Agent 模型

tools                 可选：覆盖可用工具

system_prompt         子 Agent 专属指令

middleware            子 Agent 的额外 Middleware

interrupt_on          子 Agent 自身的人工审批配置

skills / permissions  专项技能及文件权限

response_format       结构化结果配置

mode                  isolated / fork
```

这里要区分两个描述：

`description` 主要告诉**主 Agent 什么时候应该选择它**；`system_prompt` 主要告诉**子 Agent 自己应该如何工作**。

例如，对退款问题，我们可以声明一个审查者：

```python
# 框架级配置示例：需要正确安装依赖并配置模型凭据

coupon_reviewer = {
    "name": "coupon-reviewer",
    "description": (
        "检查优惠券分摊、金额精度和多次部分退款的业务约束。"
        "适合在修改 refund.py 前定位金额规则。"
    ),
    "system_prompt": (
        "你只负责审查金额规则与已有实现。"
        "输出证据路径、风险点和建议测试。"
        "不要直接修改业务代码。"
    ),
    "tools": [],  # 如需读真实项目文件，应配置适用的 Backend/文件工具
}
```

这里特意没有把 `tools=[]` 伪装成“能够读取文件”。

在 `create_deep_agent()` 的装配流程里，子 Agent 会获得 DeepAgents 的文件系统 Middleware，但是否能读取真正的项目文件，取决于传入的 Backend、文件路径和工具配置。后面会给出可用于项目工作区的组合方式。

### 4.2 谁把配置变成另一个可执行的 Agent Graph？

查看 [`subagents.py#L521-L570`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/subagents.py#L521-L570)。

固定版本中的核心函数名是：

```python
create_sub_agent(...)
```

其职责不是在当前 Graph 中增添一个“子 Agent 节点”，而是根据规格调用 LangChain 的 `create_agent()`，得到一个**单独可运行的 Agent Graph**。

下面是等价的核心结构。

**教学化简：**

```python
def compile_child(spec):
    child = create_agent(
        model=spec["model"],
        tools=spec["tools"],
        system_prompt=spec.get("system_prompt", ""),
        middleware=spec.get("middleware", []),
        name=spec["name"],
    )
    return child
```

实际代码还处理 `response_format`、`state_schema`、`interrupt_on`、`UnsupportedContentMiddleware` 等配置。

`create_sub_agent()` 会要求传入已经补齐 `model` 和 `tools` 的规格；原始声明可以省略二者，是因为 `create_deep_agent()` 会先从父 Agent 的模型、工具和 Harness 配置中补全。

这个装配区别尤其重要：

```text
用户声明子 Agent 配置
          |
          v
create_deep_agent()
          | 补全默认 model / tools / middleware
          v
create_sub_agent()
          |
          v
LangChain create_agent()
          |
          v
可运行的子 Agent Graph
```

所以，**子 Agent 不是同一个主 Graph 里被改名的一个模型节点**；它有自己的 `create_agent()` 构建结果和自己的执行循环。

但这并不意味着它必然拥有独立的物理机器、独立进程、独立文件目录或独立安全沙箱。**执行循环隔离**与**运行环境隔离**是两回事。

---

## 五、委派时，子 Agent 为什么不应该直接继承全部对话？

现在子 Agent 可以执行了，我们却马上会遇到一个新的设计问题。

主 Agent 目前可能已经有几百条消息，其中包括：

```text
用户最初的问题
refund.py 读取结果
coupon_service.py 搜索结果
pytest 的失败堆栈
Todo 更新记录
一次修改审批的结果
模型对其他文件的探索
```

当主 Agent 只想委派“检查优惠券分摊规则”时，应该把所有消息原封不动地复制过去吗？

直觉上，这看起来省事，但很快会产生问题：

- 子 Agent 会看到与自己的任务无关的长日志。
- 它可能将主 Agent 的计划当成自己的计划。
- 它可能受到主 Agent 尚未解决的推测影响。
- 长上下文削弱了我们委派的主要收益。

所以更简单且通常更合适的设计是：**给子 Agent 一条足够完整的任务说明，让它自己执行调查。**

在固定版本里，这个行为叫 **`isolated`**，是 `SubAgent` 的默认上下文模式。

### 5.1 真实源码：准备子 Agent 的初始 State

阅读 [`subagents.py#L730-L761`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/subagents.py#L730-L761)。

当子 Agent 使用默认的 isolated 模式时，核心操作相当于：

**教学化简：**

```python
child_state = {
    # 允许继承的其他状态字段
    "messages": [HumanMessage(content=description)],
}
```

这意味着：

```text
父 Agent messages：
    用户任务、搜索日志、测试堆栈、Todo……

委派请求 description：
    “核对 coupon_service.py 中的分摊规则，
     说明多次部分退款的舍入和累计尾差处理。”

子 Agent messages：
    HumanMessage(description)
```

它不是简单把父 Agent 的全部消息拷贝进去。

这里有一个需要认真把握的边界：**isolated 主要隔离的是对话上下文，不意味着所有 State 字段都被丢弃，更不意味着底层 Backend 必然隔离。** 源码会排除一组特定状态键和声明为 private 的字段，其余可以传给子 Agent。

为什么要额外排除 `todos`、`structured_response` 等字段？

因为主 Agent 的 Todo 说的是整个“修复退款 Bug”的总体计划，不应该误当成子 Agent 的内部计划；子 Agent 自己生成的结构化结果，也不应该直接覆盖父 Agent 的结果。

这就是源码中 `_EXCLUDED_STATE_KEYS` 存在的具体原因。可查看 [`subagents.py#L367-L397`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/subagents.py#L367-L397)。

### 5.2 什么时候需要继承上下文？这才引出 `fork`

默认 isolated 解决了大部分探索任务，但还会碰到另一个反例。

主 Agent 已经做了一次精确的金额计算：

```text
原始订单：99.99
优惠券优惠：10.00
第一次部分退款涉及商品金额：33.33
第一次分摊优惠：3.33
```

现在子 Agent 必须根据这些**已经确认的数字**继续分析。如果主 Agent 写的委派说明只有“核对第一次退款尾差”，isolated 子 Agent 将缺少必要前提。

一种解决办法是把数字写入 `description`。如果信息量不多，这依然是优先方案。

只有当父 Agent 已积累大量子 Agent 必须复用的有效上下文时，复制部分父上下文才有意义。

这时固定版本支持：

```python
"mode": "fork"
```

**注意：在 0.7.32 中 `fork` 明确属于实验性功能。**

它会从父 Agent 的有效对话中构造子 Agent 的初始消息，并在末尾追加委派任务。`_fork_messages()` 还会考虑父 Agent 的 Summarization 事件，避免把已压缩掉的旧内容全部重新送给子 Agent。阅读 [`subagents.py#L346-L361`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/subagents.py#L346-L361)。

**教学化简：**

```python
def fork_messages(parent_messages, summary_event, description):
    effective = apply_summary(parent_messages, summary_event)
    return effective + [HumanMessage(content=description)]
```

实际实现会先处理父对话尾部可能存在的待执行 Tool Calls，并为委派任务添加专门的前导说明。

两个模式放在一起：

| 需求 | `isolated`（默认） | `fork`（实验性） |
|---|---|---|
| 初始消息 | 主要是委派说明 | 父 Agent 的有效历史 + 委派说明 |
| 上下文污染风险 | 较低 | 更需要留意 |
| 适合的任务 | 独立代码搜索、专项测试审查 | 必须延续父 Agent 已确认的复杂分析 |
| 前提 | 委派说明足够具体 | 继承历史确实必要 |

`fork` 在固定实现中还会重建相应的 Prompt Middleware 配置，并通过运行期标记限制递归委派；不能把它理解成普通的“复制 messages 参数”开关。

---

## 六、工具真正执行后，主 Agent 为什么只收到一条结果？

现在已经有：

```text
主 Agent 的 task Tool Call
            |
            v
选择子 Agent
            |
            v
构造子 Agent State
            |
            v
运行子 Agent Graph
```

接下来要解决的是：**如何把子 Agent 的结果放回主 Agent Loop？**

假设子 Agent 内部经历了：

```text
Child Model
   ↓
read_file(coupon_service.py)
   ↓
Child Model
   ↓
grep("ROUND_HALF_UP")
   ↓
Child Model
   ↓
read_file(test_coupon.py)
   ↓
Child Model
   ↓
最终结论
```

如果把这些消息全部直接拼到主 Agent 的历史里，刚刚通过委派获得的上下文隔离优势就消失了。

所以这里需要一个**结果边界**：

- 子 Agent 内部历史由子 Agent 自己消费。
- 主 Agent 只需要看到一条对当前 `task` Tool Call 的反馈。
- 如果子 Agent 的执行合法地产生了额外的共享状态更新，再按规则返回。

### 6.1 真实源码：子 Agent 的末条有效回复进入 `ToolMessage`

阅读 [`subagents.py#L678-L715`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/subagents.py#L678-L715)。

关键函数为 `_return_command_with_state_update()`。

其主要动作依次是：

1. 检查子 Agent 返回的 State 含有 `messages`。
2. 如果结果有非空 `structured_response`，将它序列化成 JSON。
3. 否则从最后往前找**最后一条含非空文本的 `AIMessage`**。
4. 构造与主 `task` Tool Call 对应的 `ToolMessage`。
5. 在 `Command(update=...)` 中附上允许传播的其他状态变化。

**教学化简：**

```python
def child_result_to_parent(result, tool_call_id):
    final_text = find_last_nonempty_ai_text(result["messages"])
    return Command(
        update={
            "messages": [
                ToolMessage(final_text, tool_call_id=tool_call_id)
            ]
        }
    )
```

注意：这里展示的代码省略了结构化响应处理、状态过滤和未修改字段过滤，因此不能当作完整的源码替换。

为什么返回的是 `ToolMessage`，而不是直接把子 Agent 的最终 `AIMessage` 放进父状态？

因为在父 Agent 看来，刚才发生的是一个 **`task` 工具调用**。Tool Call 与 Tool Result 必须在消息协议上对应起来。

```text
父 AIMessage
  tool_calls = [{
      id: "call_rules_001",
      name: "task",
      args: {...}
  }]
        |
        v
子 Agent 完成
        |
        v
父 ToolMessage
  tool_call_id = "call_rules_001"
  content = "优惠券分摊规则报告……"
```

第二区别也要记住：**返回一条 `ToolMessage` 不等于子 Agent 只能影响这一个字段**。它可能通过工具和 Backend 修改文件，也可能返回允许合并的 State 更新。所以结果摘要的隔离，并不自动提供副作用隔离。

### 6.2 为什么还要过滤未变化的 State？

假设主 Agent 的 State 含有：

```python
{"audit_events": ["user_request_received"]}
```

子 Agent 在运行前继承了它，但完全没有改动。

如果它在返回时又把这个字段完整写回父 Agent，而父 State 对该字段使用追加 Reducer，则可能得到：

```python
[
    "user_request_received",
    "user_request_received",
]
```

如果没有 Reducer，两个并行子任务返回同一个普通字段，还可能发生并发写入冲突。

所以源码中的 `_is_unchanged()` 会帮助忽略与输入相同的返回字段。这解决的是一个很具体的问题：**继承的 State 不应该因为子 Agent 读过一次就再次写入父 State。**

但若多个子 Agent 确实同时修改同一字段，仍须根据字段 Reducer 和业务语义决定如何合并；不能因此推断并发写入天然安全。这也是第十三章继续研究 Reducer 的动机。

---

## 七、谁在什么阶段执行真正的子 Agent Loop？

现在可以沿源码向下追踪最重要的一次真实调用。

### 7.1 真实源码：`task` 调用子 Agent

查看 [`subagents.py#L762-L790`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/subagents.py#L762-L790)。

在完成参数校验、确定子 Agent 和构造 State 之后，同步的 `task()` 会调用子 Agent 的 `invoke()`。

**真实源码节选：**

```python
result = subagent.invoke(subagent_state, subagent_config)
```

它的下一步不是直接给用户发送消息，而是让 `_return_command_with_state_update()` 将结果变成父 Agent 的工具反馈。

因此，**真正触发子 Agent Loop 的，是父 Agent 的 ToolNode 在运行期执行 `task` 工具**。`SubAgentMiddleware` 在构建期准备工具；`task` 工具在运行期调用子 Graph；LangGraph 运行子 Graph 的循环。

将这一路径完全展开：

```text
                 父 Agent Graph

START ──────────────→ Parent Model
                            |
                            | AIMessage: task(...)
                            v
                        Parent ToolNode
                            |
                            v
                         task Tool
                            |
                            | 选择 subagent_type
                            | 准备 child_state
                            v
                    Child Agent Graph
                            |
                            v
                        Child Model  ←─────────┐
                            |                 |
                            | tool_calls      |
                            v                 |
                        Child ToolNode ───────┘
                            |
                         ...若干轮
                            |
                            v
                       Child Final AIMessage
                            |
                            v
                    Command(update={...})
                            |
                            | ToolMessage(call_rules_001)
                            v
                       Parent State
                            |
                            v
                       Parent Model
                            |
                    继续修改和测试退款代码
```

这张图中出现了两个 Loop，但**只有一个是父 Agent 的当前工具执行路径，另一个是工具内部启动的独立可运行 Graph**。

### 7.2 同步调用与 `ainvoke()`：不要将“异步 API”误认成“后台运行”

同一个 `task` 工具同时提供同步函数和异步协程：

```text
同步路径：task()  → child.invoke(...)
异步路径：atask() → await child.ainvoke(...)
```

在两条路径中，父 Agent 都要拿到子 Agent 的最终结果，才能形成这一轮 `task` 的工具反馈。

所以两者的差别主要是 Python 执行模型：是否通过异步协程等待并配合异步 I/O。

它们**都不等于**“发起后台任务，然后主 Agent 不等结果就去干别的事”。

这就是为什么 DeepAgents 0.7.32 还提供另一套机制：`AsyncSubAgentMiddleware`。但在讨论后台任务前，我们先看看普通同步委派如何被接入 `create_deep_agent()`。

---

## 八、`create_deep_agent()` 如何把子 Agent 装配进主 Agent？

经过前面几节，我们知道需要四样东西：

1. 主 Agent 可以看到 `task` 工具。
2. `task` 能按名称选择子 Agent。
3. 子 Agent 必须有自己的可运行 Graph。
4. 子 Agent 的结果应当回到主 Agent 的 ToolNode 协议。

谁负责把它们连接起来？

答案是第七章研究过的 Harness 装配入口：`create_deep_agent()`。

### 8.1 真实源码：先把用户声明拆成两类

查看 [`graph.py#L628-L755`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py#L628-L755)。

固定版本允许 `subagents=` 接收：

- **`SubAgent`**：声明式配置，由框架补全模型、工具和 Middleware 后再编译。
- **`CompiledSubAgent`**：用户已经准备好的可运行对象，框架按照该对象现有配置调用。
- **`AsyncSubAgent`**：用于远程或后台执行的 Agent Protocol 配置，走另外一套 Middleware。

为什么要区分？

因为前两种都可以由 `task` 工具在当前调用中**等待执行完成**；第三种需要的是**远程任务 ID 与状态管理**。

### 8.2 真实源码：只有存在 inline 子 Agent 时才装配 `SubAgentMiddleware`

关键位置：[`graph.py#L821-L853`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py#L821-L853)。

主要装配顺序是：

```text
FilesystemMiddleware
        ↓
[如果有 inline_subagents]
SubAgentMiddleware
        ↓
ParentNotifierMiddleware
        ↓
SummarizationMiddleware
        ↓
PatchToolCallsMiddleware
        ↓
[如果有 async_subagents]
AsyncSubAgentMiddleware
```

后面还会根据 HarnessProfile、Skills、Memory、Permissions 和用户传入的 Middleware 继续装配，完整过程见第七章。

这里有一个微妙而重要的版本事实：**在 `0.7.32` 中，`SubAgentMiddleware` 不是无条件实例化；它依赖是否存在可用于 `task` 的 inline 子 Agent。**

而 inline 子 Agent 既可能来自用户传入的 `subagents=`，也可能来自 HarnessProfile 启用的内置 `general-purpose` 子 Agent。

因此，不能简单断言“所有 DeepAgents 实例必有 `task`”；更准确的说法是：**当最终装配的 inline 子 Agent 集合非空时，才会提供同步 `task` 委派能力**。

### 8.3 为什么有一个 `general-purpose` 子 Agent？

查看 [`subagents.py#L472-L478`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/subagents.py#L472-L478) 与 [`graph.py#L797-L820`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py#L797-L820)。

它用于承接一般的检索、文件探索和多步骤调查，而不是每次都要求调用者自行定义一个子 Agent。

例如，主 Agent 可以把“在仓库里找出优惠券分摊的所有相关实现”交给通用子 Agent，等它返回路径和结论后再继续修改。

但是通用子 Agent 也不保证一定启用。该版本的 HarnessProfile 可以关闭它，尤其要避免将“框架有默认通用子 Agent 配置”误说成“任何配置下都必定有通用子 Agent”。

### 8.4 实际创建一个专项子 Agent

下面用前面所有机制组合一个框架级示例。

**使用示例（未在本章实际调用真实模型）：**

```python
from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend

agent = create_deep_agent(
    model="openai:gpt-5.5",  # 示例模型，运行前需安装 provider 并设置凭据
    backend=FilesystemBackend(root_dir="."),
    subagents=[
        {
            "name": "coupon-reviewer",
            "description": (
                "审查优惠券部分退款的分摊规则、Decimal 精度与累计尾差。"
                "用于主 Agent 修改 refund.py 之前的专项调查。"
            ),
            "system_prompt": (
                "只进行规则分析；报告必须包含文件路径、金额示例和建议测试。"
                "不要修改项目文件。"
            ),
            "tools": [],
        },
        {
            "name": "test-reviewer",
            "description": (
                "审查部分退款相关 pytest 用例，找出多次退款、舍入和边界测试缺口。"
            ),
            "system_prompt": (
                "提供测试缺口与建议断言，不直接编辑源文件。"
            ),
            "tools": [],
        },
    ],
)

result = agent.invoke({
    "messages": [{
        "role": "user",
        "content": (
            "修复优惠券部分退款精度 Bug。"
            "先委派规则审查和测试覆盖率审查，再决定修改方式；"
            "最终运行相关 pytest。"
        ),
    }]
})
```

这里 `FilesystemBackend(root_dir=".")` 只是让工具能够面向某个本地根目录访问文件，**不是安全沙箱**。此外，`system_prompt` 中要求子 Agent“不修改文件”只是模型指令；若需要强制执行只读规则，应使用权限或定制仅有只读工具的 FilesystemMiddleware，而不能只依赖提示词。

因为这两个子 Agent 的职责都是审查，所以在生产使用时，建议将“只能读不能写”落实为实际权限配置。

上面的 `result` 示例并未真实运行：执行需要安装相容依赖、提供模型凭据，以及准备真实项目文件。

---

## 九、主 Agent 可以连续委派，也可以同一轮委派多个子 Agent 吗？

只要模型能够生成多个工具调用，ToolNode 就可能处理多个 `task` Tool Call。

例如模型可能决定：

```text
同一轮 Parent AIMessage
    |
    +-- task(coupon-reviewer, "审查规则")
    |
    +-- task(test-reviewer, "检查测试缺口")
```

这很容易让人以为：只要有子 Agent，就获得了“完全安全的并行工作”。

事实上，独立 Agent Loop 并不自动消除共享资源竞争。

### 9.1 具体失败场景：两个子 Agent 同时改 `refund.py`

假设两个子 Agent 共享文件 Backend，而且都拥有编辑权限：

```text
规则 Agent：根据业务规则修改 refund.py 第 40 行
测试 Agent：同时为测试方便修改 refund.py 第 42 行
```

即使各自的 `messages` 隔离了，也可能在文件层面发生覆盖、冲突或难以预测的修改结果。

这说明存在两种不同的隔离：

```text
对话隔离：子 Agent 不直接把中间消息混进父历史
资源隔离：不同 Agent 是否共享同一文件和执行环境
```

**DeepAgents 的对话隔离不自动意味着资源隔离。**

因此，通常更稳健的方式是：

- 子 Agent 负责**独立调查和报告**。
- 主 Agent 负责**统一决定和实施代码修改**。
- 如果确实要并行写文件，先设计独立工作区、合并策略和冲突检查。

这里没有必要提前展开 Reducer 和 Checkpoint 的所有细节，只需记住：**子 Agent 多了，不代表共享状态的并发问题消失了。**

### 9.2 Todo 和子 Agent 不是同一层能力

上一章的 `write_todos` 保存的是“要完成什么”；这一章的 `task` 是“将某一步交给哪个独立 Agent Loop”。

它们可以一起使用，但彼此不替代：

```text
Todo：审查优惠券分摊规则（in_progress）
          |
          v
主 Agent 调用 task(coupon-reviewer, ...)
          |
          v
子 Agent 完成审查并返回报告
          |
          v
主 Agent 根据报告更新 Todo 状态
```

`task` 工具成功返回，并不自动将主 Agent 的 Todo 标记为已完成；仍然需要主 Agent 或相应业务机制判断报告是否满足验收条件。

---

## 十、仅靠当前 `task`，还不能解决什么问题？

我们现在遇到一个不同于“上下文隔离”的需求。

假设对退款 Bug 的测试矩阵特别庞大，专项测试 Agent 需要运行长时间的集成测试。而主 Agent 在测试运行期间还想修改文档、分析另一条异常路径。

对于同步委派：

```text
Parent Model
   ↓
task(test-reviewer)
   ↓
等待 Child Graph 完成
   ↓
收到最终报告
   ↓
Parent Model 继续
```

对于普通的 async `atask()`，逻辑依然是“`await` 子 Agent 完成”，只是使用异步 I/O。

这时缺少的不是第二个 Loop，而是：

> **把子 Agent 运行变成一项可以在后台追踪、查询和更新的任务。**

于是才有必要引入另一个机制：`AsyncSubAgentMiddleware`。

### 10.1 固定版本里的 AsyncSubAgent 是另一种规格

查看 [`async_subagents.py#L51-L145`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/async_subagents.py#L51-L145)。

它与前面的本地 inline `SubAgent` 不同，关键配置含有：

```text
name          子 Agent 名称

description   用途说明

graph_id      Agent Protocol 服务器上的 Graph / Assistant 标识

url           可选：目标服务器地址

headers       可选：请求头

webhook       可选：完成时的回调目标
```

因此，它不是在 `task()` 内直接执行一个本地 `Runnable`，而是通过 LangGraph SDK 与支持 Agent Protocol 的服务交互。

### 10.2 为什么它不能仍然只返回最终字符串？

因为一旦任务在后台运行，启动时它还没有最终结果。

主 Agent 只能先得到：

```text
已启动：task_id = <某个实际任务 ID>
```

以后才能根据 `task_id` 查询进度或获取结果。

这迫使系统产生一份新的状态：

```text
async_tasks
    |
    +-- task_id
    +-- agent_name
    +-- thread_id
    +-- run_id
    +-- status
    +-- timestamps
```

源码中定义 `AsyncTask` 与 `AsyncSubAgentState`，并为 `async_tasks` 提供合并函数。参考 [`async_subagents.py#L164-L229`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/async_subagents.py#L164-L229)。

为什么必须放进 State？

如果任务 ID 只出现在一次模型输出里，主 Agent 在上下文压缩或执行恢复后可能失去可操作的任务记录。把任务元数据保存到状态字段后，框架可以独立于模型对话保存和更新它。

### 10.3 真实工具集合：不是 `task`，而是一组后台任务工具

查看 [`async_subagents.py#L1318-L1338`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/async_subagents.py#L1318-L1338)。

`AsyncSubAgentMiddleware` 暴露五个工具：

| 工具名 | 执行动作 |
|---|---|
| `start_async_task` | 启动后台任务并取得 `task_id` |
| `check_async_task` | 取得某个任务的状态与结果 |
| `update_async_task` | 发送后续要求 |
| `cancel_async_task` | 取消任务 |
| `list_async_tasks` | 列出已追踪任务及其当前状态 |

这是一种与同步 `task` 明显不同的交互协议：

```text
Parent Model
    |
    v
start_async_task(...)
    |
    v
返回 task_id
    |
    +---------------------------+
    |                           |
    v                           v
父 Agent 做其他工作        子 Agent 在远程执行
    |                           |
    v                           |
check_async_task(task_id) <-----+
    |
    v
取得完成 / 失败 / 等待审批等状态
```

这里有两个边界要特别说明：

1. **`async` 子 Agent 是后台运行机制，不只是 Python 的 `async def`。**
2. 远程 Agent 可以有自己的审批和线程状态；`waiting` 并不等于最终完成，可能代表它暂停并等待人工处理。

固定版本支持在同一 `create_deep_agent(subagents=...)` 参数中声明 `AsyncSubAgent` 规格，由装配逻辑将其路由到 `AsyncSubAgentMiddleware`。但你需要一个实际可用的 Agent Protocol 服务，而不只是一个模型 API Key。

### 10.4 一个真实配置的形状

**框架级示例（未连接远程服务运行）：**

```python
from deepagents import create_deep_agent

agent = create_deep_agent(
    model="openai:gpt-5.5",
    subagents=[
        {
            "name": "long-tests",
            "description": "在独立执行环境中运行较长的退款回归测试。",
            "graph_id": "refund-test-agent",
            "url": "https://your-agent-server.example",
        }
    ],
)
```

此处 URL 是明确的**占位地址**，不是一个真实可访问的服务；它只是说明参数结构。

对应的模型动作可能是：

```text
start_async_task(
    subagent_type="long-tests",
    description="运行退款相关完整测试矩阵，并报告失败用例"
)
```

随后再通过 `check_async_task(task_id=...)` 查看结果。

这不是同步 `task` 的简单换名，而是涉及远程线程、运行状态和任务生命周期的另一种设计。

---

## 十一、一个不需要模型 API Key 的最小实验

我们现在已经理解正常路径，可以用标准库做一个可运行的微型实验，验证**委派前后消息边界如何变化**。

注意：这个实验验证的是我们前面推导的调用与状态隔离结构，**不是**在运行 DeepAgents 的真实 `SubAgentMiddleware`，也不验证模型的自主工具选择能力。

将下面的代码保存为 `chapter12_minimal.py`：

```python
from dataclasses import dataclass


@dataclass
class Message:
    kind: str
    text: str
    tool_call_id: str | None = None


def child_agent(task: str) -> list[Message]:
    """模拟一个拥有内部多步探索过程的子 Agent。"""
    return [
        Message("human", task),
        Message("tool", "读取 coupon_service.py 的分摊函数"),
        Message("tool", "检查多次部分退款的测试边界"),
        Message(
            "ai",
            "检查完成：应统一 Decimal 精度，并验证累计优惠分摊尾差。",
        ),
    ]


def delegate(task: str, tool_call_id: str) -> Message:
    child_history = child_agent(task)
    last_answer = next(
        m.text for m in reversed(child_history)
        if m.kind == "ai" and m.text.strip()
    )
    return Message("tool", last_answer, tool_call_id)


def main() -> None:
    parent_history = [
        Message("human", "修复优惠券部分退款精度 Bug"),
        Message("ai", "调用 task(coupon-reviewer)", "call_001"),
    ]
    result = delegate(
        "只检查优惠券分摊规则和建议测试",
        tool_call_id="call_001",
    )
    parent_history.append(result)

    assert len(parent_history) == 3
    assert parent_history[-1].kind == "tool"
    assert parent_history[-1].tool_call_id == "call_001"
    assert "读取 coupon_service.py" not in "\n".join(
        m.text for m in parent_history
    )
    assert "累计优惠分摊尾差" in parent_history[-1].text

    print("PASS: 主 Agent 收到最终结果，没有直接继承子 Agent 的中间消息")


if __name__ == "__main__":
    main()
```

运行：

```bash
python chapter12_minimal.py
```

**本章已在本地 Python 运行该脚本，断言全部通过，输出上述 PASS 文本。** 这验证了一个足够小但关键的性质：

**从父 Agent 的角度，复杂的子流程可以被压缩为与某次工具调用关联的一条结果消息。**

它没有验证以下内容：

- `SubAgentMiddleware` 是否在真实模型中成功注册。
- LangGraph 是否真的执行了嵌套 Graph。
- 两个子 Agent 能否在特定 Backend 下安全并行写文件。
- 远程 AsyncSubAgent 能否启动与恢复。

那些结论必须使用相应框架环境和服务做独立验证。

### 11.1 进一步的框架验证建议

准备真实依赖和模型凭据后，可以按这个顺序验证：

```text
验证 1：仅注册一个 coupon-reviewer
    → 让主 Agent 调用 task
    → 观察父 Agent messages 是否包含 task ToolMessage

验证 2：让子 Agent 连续读取两个文件
    → 检查子 Agent trace 中确实出现多轮 Model / Tools
    → 比较父 Agent messages，不应直接混入全部子消息

验证 3：把相同任务改成 mode="fork"
    → 验证它能利用父历史中的已确认金额
    → 比较输入 Token 和结果是否发生变化

验证 4：配置一个远程 AsyncSubAgent
    → start_async_task 得到 task_id
    → check_async_task 获取状态 / 结果
```

不要将“工具函数模拟通过”表述为“真实框架委派已经验证通过”。

---

## 十二、现在才回头看完整架构

到这里，我们已经一步一步遇到了足以解释这些组件存在的实际问题：

1. 主 Agent 有了计划，但专项调查会挤占上下文。
2. 把调查放进另一个 Agent Loop，可以让它自行探索。
3. 为了让模型自主决定委派，把委派做成 `task` 工具。
4. 为了让 `task` 知道运行谁，需要 `SubAgent` 规格和可运行子 Graph。
5. 为了不把全部历史混回父 Agent，需要输入上下文选择与输出结果边界。
6. 为了对齐父工具调用协议，需要 `ToolMessage(tool_call_id=...)`。
7. 为了在后台运行较长任务，才引入任务 ID 与 `AsyncSubAgentMiddleware`。

现在展示整体结构，图上的每一层都已经有了来历：

```text
用户：修复退款精度 Bug
            |
            v
       Parent Agent Graph
            |
         Parent Model
            |
      选择后续执行动作
            |
    +-------+----------------------------+
    |                                    |
    v                                    v
read_file / edit_file                 task(...)
(普通文件工具)                           |
    |                                    v
    |                              SubAgentMiddleware
    |                                提供 task 工具
    |                                    |
    |                             选择子 Agent 名称
    |                                    |
    |                          准备 isolated / fork State
    |                                    |
    |                                    v
    |                               Child Graph
    |                                    |
    |                         Child Model ↔ Child Tools
    |                                    |
    |                               最终回复
    |                                    |
    |                          Command / ToolMessage
    |                                    |
    +------------------------------------+
                     |
                     v
              Parent Model 继续
                     |
             修改代码 / 执行测试
                     |
                     v
                 最终回答

如需后台任务：
  Parent Model
       |
       v
  AsyncSubAgentMiddleware
       |
       +-- start_async_task → task_id
       +-- list_async_tasks
       +-- check_async_task
       +-- update_async_task
       +-- cancel_async_task
       |
       v
  Agent Protocol Server / Remote Agent
```

这同时回答了第十二章的中心问题：

> **DeepAgents 没有重新发明一种“父 Agent 直接控制子 Agent 思考”的特殊循环。它用 Middleware 向主 Agent 提供工具，用工具启动另一个可运行的 Agent Graph，再用 State 更新和 ToolMessage 把执行结果送回主 Agent Loop。需要后台执行时，它改用远程任务工具和任务状态追踪。**

---

## 十三、这种设计解决了什么，又没有解决什么？

### 13.1 它解决了什么

**第一，减少主 Agent 的中间探索负担。** 子 Agent 可以独立搜索代码、读取文件、分析测试，只把有用结论带回来。

**第二，允许针对不同任务配置不同能力。** 例如用审查型子 Agent 只读取文件，用实现型 Agent 修改代码；工具、模型、指令和审批方式都可以不同。

**第三，复用已有消息和 ToolNode 机制。** 主 Agent 仍按照熟悉的 `AIMessage.tool_calls → ToolNode → ToolMessage` 协议运行。

**第四，允许两种不同委派语义。** 普通 `task` 适合需要等待结果的工作；后台 AsyncSubAgent 适合远程长任务。

### 13.2 它没有自动解决什么

**不会自动保证结果正确。** 子 Agent 可能遗漏测试场景，也可能给出未经验证的推测。主 Agent 仍要根据证据验收。

**不会自动保证更便宜或更快。** 子 Agent 增加模型调用和工具调用，有时反而会提高 Token、延迟及调试成本。

**不会自动拥有安全沙箱。** Prompt 指令、对话隔离、Backend 的本地根目录设置，都不能替代真实权限和执行环境隔离。

**不会自动解决并发写入。** 多个子 Agent 共享的 Backend 或 State 字段仍可能冲突。

**不会自动继承正确的审批策略。** 子 Agent 的权限与审批配置需要结合父子规格和 Harness 装配结果检查，而不能因为主 Agent 配置了审批，就假定所有子 Agent 操作都被等价保护。

**不会让 `ainvoke()` 自动变成后台运行。** 本章讨论的普通 `atask()` 仍会等待 `ainvoke()` 完成；只有独立的远程任务机制才提供先返回 `task_id` 的行为。

---

## 十四、本章总结：从一个 Loop 生长出两个 Loop

回到我们最初的退款 Bug：

```text
用户要求修复退款精度
        ↓
主 Agent 用 Todo 跟踪全局步骤
        ↓
主 Agent 发现需要专项规则审查
        ↓
调用 task(coupon-reviewer, ...)
        ↓
子 Agent 独立搜索与分析
        ↓
返回规则结论和建议测试
        ↓
主 Agent 修改 refund.py
        ↓
运行 pytest，根据实际结果继续修复
```

现在读者应该能够不靠背术语，就解释：

- 为什么复杂任务需要**委派**，而不只是更长的 Prompt。
- 为什么委派要以一个名为 `task` 的工具暴露给模型。
- 为什么子 Agent 有自己的 Graph，而不是主 Graph 的一个普通节点。
- 为什么子 Agent 的默认上下文模式是 isolated，以及 fork 要解决哪一种不同问题。
- 为什么最终回复需要变成父 Agent 的 `ToolMessage`。
- 为什么普通 `task` 的异步协程不是后台任务。
- 为什么真正的后台子 Agent 需要 `task_id`、远程运行状态和专门的 Middleware。

但本章也留下了一个自然的新问题。

主 Agent 与子 Agent 各有自己的消息历史；多个工具和子 Agent 还可能在同一轮更新 State。我们已经看到源码会主动排除部分字段、忽略未修改字段，甚至为异步任务使用特殊的合并函数。

**为什么 Graph 必须这么谨慎地合并状态？**

当执行被中断、恢复、分叉或重试时，什么数据应该保留？什么数据可以增量写入？哪些外部操作不能仅靠恢复机制保证不重复？

这就是下一章要研究的内容：

**第十三章：Agent 如何记住过程并可靠恢复？——State、Reducer、DeltaChannel 与 Checkpoint。**

---

## 附：固定版本源码导航

| 要回答的问题 | 固定源码入口 |
|---|---|
| 子 Agent 配置包含哪些字段 | [`subagents.py#L78-L207`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/subagents.py#L78-L207) |
| 预编译子 Agent 与声明式子 Agent 的区别 | [`subagents.py#L210-L270`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/subagents.py#L210-L270) |
| `isolated` / `fork` 的上下文说明 | [`subagents.py#L194-L207`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/subagents.py#L194-L207) |
| `fork` 怎样组织有效历史 | [`subagents.py#L346-L361`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/subagents.py#L346-L361) |
| 哪些 State 字段被排除 | [`subagents.py#L367-L408`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/subagents.py#L367-L408) |
| `task` 工具参数与校验 | [`subagents.py#L414-L446`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/subagents.py#L414-L446) |
| `create_sub_agent()` 怎样调用 `create_agent()` | [`subagents.py#L521-L570`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/subagents.py#L521-L570) |
| `_build_task_tool()` 怎样编译子 Agent 配置 | [`subagents.py#L585-L673`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/subagents.py#L585-L673) |
| 子 Agent 如何形成 `Command` + `ToolMessage` | [`subagents.py#L678-L715`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/subagents.py#L678-L715) |
| 子 Agent 的输入 State 如何构造 | [`subagents.py#L730-L761`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/subagents.py#L730-L761) |
| `task()` 和 `atask()` 的执行边界 | [`subagents.py#L762-L833`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/subagents.py#L762-L833) |
| Middleware 如何向主 Agent 提供工具 | [`subagents.py#L889-L958`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/subagents.py#L889-L958) |
| `create_deep_agent()` 如何补全声明式子 Agent | [`graph.py#L628-L755`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py#L628-L755) |
| 通用子 Agent 如何被配置 | [`graph.py#L797-L820`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py#L797-L820) |
| 主 Agent 的 Subagent Middleware 装配位置 | [`graph.py#L821-L853`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py#L821-L853) |
| 远程 `AsyncSubAgent` 的规格 | [`async_subagents.py#L51-L145`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/async_subagents.py#L51-L145) |
| `async_tasks` 状态与合并规则 | [`async_subagents.py#L164-L229`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/async_subagents.py#L164-L229) |
| 异步子任务工具清单 | [`async_subagents.py#L1318-L1338`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/async_subagents.py#L1318-L1338) |

**本章核心产出：** 读者能够追踪一次 `task` 从父模型产生 Tool Call、进入父 ToolNode、选取并运行子 Graph、构造子 State、产生 `ToolMessage` 与状态更新、再返回父模型的完整路径；同时能够分清普通异步调用和远程后台任务两种本质不同的执行方式。
