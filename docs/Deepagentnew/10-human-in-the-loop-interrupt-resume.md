---
title: 第十章：工具能执行，为什么还必须停下来？——HITL、权限与中断恢复
description: 从优惠券部分退款 Bug 的危险修改与 pytest 审批出发，沿 DeepAgents 0.7.32、LangChain 1.4.7 和 LangGraph interrupt/Command 实现，渐进理解工具权限、人工审核、Graph 暂停、恢复、拒绝反馈及副作用边界
tags: [DeepAgents, LangChain, LangGraph, Human-in-the-Loop, Middleware, interrupt, Command, Checkpoint, Coding Agent, 源码精读]
sidebar_position: 14
---

# 第十章：工具能执行，为什么还必须停下来？——HITL、权限与中断恢复

> **本章核心问题：** 模型已经生成合法的 `edit_file` 或测试工具调用，ToolNode 也有能力执行它。怎样在真正修改文件、运行命令之前暂停，交给人审批，再从原来那次 Agent 执行继续？

## 一、接着第九章：工具调用能落地，也就真的会产生副作用

上一章，我们跟踪了优惠券部分退款 Bug 的一条工具调用路径：

```text
模型提出 edit_file
       ↓
ToolNode 找到对应工具
       ↓
FilesystemMiddleware 处理参数
       ↓
Backend 修改文件
       ↓
ToolMessage 返回结果
       ↓
模型决定下一步
```

我们终于解决了“模型说要修改代码，文件却没有变化”的问题。

但这个能力一旦建立，马上会出现另一个问题。

我们的 Coding Agent 本来应该修复 `shop/refund.py` 里的金额精度错误。假设它检查完代码后，准备执行：

```python
# 教学示例：模型可能生成的工具调用，不代表真实模型输出
{
    "name": "edit_file",
    "args": {
        "file_path": "/shop/refund.py",
        "old_string": "refund = round(total * ratio, 2)",
        "new_string": "refund = (total * ratio).quantize(Decimal('0.01'))",
    },
    "id": "call_edit_001",
    "type": "tool_call",
}
```

这个修改看上去合理，但如果退款系统还有“尾差归最后一笔退款”“原订单优惠分摊顺序”等规则，直接改动可能引入新的资金错误。

再假设模型发现某个测试用例一直失败，于是提议删除整个测试目录：

```python
# 教学示例：危险的调用意图
{
    "name": "delete",
    "args": {"file_path": "/shop/tests"},
    "id": "call_delete_002",
    "type": "tool_call",
}
```

这里最值得注意的不是调用格式是否有效，而是：**格式合法和拥有执行权限，是两回事。**

如果 ToolNode 只根据工具名查找可执行对象，然后立即调用 Backend，就会把模型的一次判断直接转化为不可忽略的文件副作用。

我们的新需求因此非常具体：

> 在工具真正执行之前，给某些动作增加一道由程序和人共同控制的闸门。

我们先不急着接触 LangGraph 的中断机制，看看最小的 Python 设计能做到什么。

### 1.1 本章阅读基线

本章沿用系列约定：

```text
DeepAgents Python：0.7.32
DeepAgents Commit：e85c6b9
LangChain：       1.4.7（源码 Commit fbb8a35）
LangGraph：       以对应安装环境解析出的兼容版本为准
```

固定源码入口：

- [DeepAgents `graph.py`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py)
- [DeepAgents `middleware/_fs_interrupt.py`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/_fs_interrupt.py)
- [DeepAgents `middleware/filesystem.py`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py)
- [LangChain `middleware/human_in_the_loop.py`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/human_in_the_loop.py)
- [LangChain `agents/factory.py`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/factory.py)
- [LangGraph `types.py`，阅读 `interrupt` 和 `Command` 的 API 机制](https://github.com/langchain-ai/langgraph/blob/main/libs/langgraph/langgraph/types.py)

**源码标注约定：** “真实源码节选”表示固定 Commit 中可定位到的短片段；“教学化简”表示保持设计语义但并非逐字原文；“概念伪代码”只描述思想；“实验预期”不代表实际调用框架并运行成功。LangGraph 的最后一个链接是 API 阅读入口，不冒称它与 DeepAgents 固定 Commit 同时发布；需要做逐行兼容验证时，请使用安装环境的 `langgraph` 版本锁定对应标签。

---

## 二、第一步：给工具执行加一个普通的 `if`

如果系统只有一个文件修改工具，最简单的办法并不复杂：

```python
# 教学化简：无需框架的审批闸门

def guarded_edit(tool_call, approved: bool, execute):
    if not approved:
        return {
            "tool_call_id": tool_call["id"],
            "status": "rejected",
            "content": "用户拒绝修改文件",
        }

    return execute(tool_call)
```

例如：

```python
call = {
    "name": "edit_file",
    "args": {"file_path": "/shop/refund.py"},
    "id": "call_edit_001",
}

result = guarded_edit(
    call,
    approved=False,
    execute=lambda x: print("执行实际修改", x),
)

assert result["status"] == "rejected"
```

只有一种工具、一次同步调用时，这个方案已经很够用了。

问题在于，真实 Agent 不是固定执行一次 `guarded_edit()` 就结束。

它可能先调用 `read_file`，再一次性提出两个 `edit_file` 和一个测试请求；审批人还可能暂时离开，等十分钟后再打开审批页面。

我们不能在服务器进程中一直阻塞等待一个布尔变量，也不能把用户离开的整段时间当作一次尚未返回的 HTTP 请求。

新的限制出现了：

1. **哪些工具需要审批？** 不能让每次 `read_file` 都弹出确认。
2. **怎样保存待审批请求？** 审批人可能不会立即回答。
3. **回答后怎样接着执行？** 不能重新调用模型，让它再生成一份可能不同的工具请求。
4. **拒绝时怎样反馈模型？** 不能假装工具已经成功。

这就迫使我们把“审批”从一个函数里的 `if` 提升为 Agent 执行流程中的一个可恢复事件。

---

## 三、第二步：先区分工具权限和人工审批

还没讨论中断之前，需要区分两种问题。

第一种问题是：

> 这个动作是否被系统规则禁止？

第二种问题是：

> 这个动作原则上可以做，但这一次是否要请人确认？

两者看似接近，行为却完全不同。

对于 `/shop/refund.py`：

- 读取文件可以自动允许。
- 修改代码可以要求审批。
- 删除 `/shop/tests` 可以直接禁止。

这意味着我们至少需要三种结果：

```text
allow      → 直接继续工具执行
interrupt  → 暂停，等待人决定
deny       → 拒绝执行，返回错误
```

### 3.1 真实源码：`FilesystemPermission`

阅读 [DeepAgents `middleware/filesystem.py` 第 342—389 行](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L342-L389)。

```python
# 真实源码节选：字段定义
@dataclass
class FilesystemPermission:
    operations: list[FilesystemOperation]
    paths: list[str]
    mode: Literal["allow", "deny", "interrupt"] = "allow"
```

这三个字段分别告诉系统：

- `operations`：规则限制读取还是写入（`read` / `write`）。
- `paths`：规则应用在哪些 POSIX 路径或匹配模式。
- `mode`：匹配时要执行哪一种处理。

例如：

```python
from deepagents.middleware.filesystem import FilesystemPermission

permissions = [
    FilesystemPermission(
        operations=["write"],
        paths=["/shop/tests/**"],
        mode="deny",
    ),
    FilesystemPermission(
        operations=["write"],
        paths=["/shop/**"],
        mode="interrupt",
    ),
]
```

这里我们把**更具体的拒绝规则放在前面**。原因不是美观，而是源码中的匹配方式。

```python
# 真实源码节选：filesystem.py，省略类型标注
for rule in rules:
    if operation not in rule.operations:
        continue
    if any(wcglob.globmatch(path, pattern, flags=_FS_WCMATCH_FLAGS) for pattern in rule.paths):
        return rule.mode
return "allow"
```

读这几行就能理解三个事实：

1. 规则按声明顺序检查，**第一个匹配规则生效**。
2. 对于没有匹配规则的路径，默认结果是 `allow`。
3. `deny` 和 `interrupt` 不能混为一谈：前者拒绝，后者要求额外授权。

所以这里的配置含义是：禁止修改测试目录；其他 `/shop/` 下的写操作要求人工审批。

但现在只解决了“怎样表达规则”，尚未解释：**`interrupt` 这个字符串怎样让正在运行的 Agent 真正停下来？**

---

## 四、第三步：权限规则为什么不能直接代替 HITL？

最直接的想法是让 `FilesystemMiddleware` 自己负责所有审批：

```python
# 概念伪代码：一种没有采用的耦合设计
if permission_mode == "interrupt":
    human_answer = wait_for_human()
    if human_answer == "approve":
        backend.edit_file(...)
```

这种实现的问题是：

- 文件工具必须知道怎样显示审批界面。
- 文件工具必须自己保存暂停的执行状态。
- 如果审批的是自定义 `run_refund_tests` 工具，就需要再写一套相同逻辑。

而我们已经在第五、六章理解：Middleware 能在生命周期边界增加行为，因此“让人审工具调用”应该尽量独立于文件工具本身。

在 DeepAgents 0.7.32 中，源码明确进行了职责划分：

- `FilesystemMiddleware` 执行文件工具，并负责工具层面的 `deny` 权限检查。
- `_fs_interrupt.py` 把 `interrupt` 文件规则转换为按工具名称组织的审核配置。
- `HumanInTheLoopMiddleware` 负责产生实际的暂停和审核流程。

### 4.1 真实源码：将文件规则转换为审批条件

打开 [DeepAgents `middleware/_fs_interrupt.py` 第 160—187 行](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/_fs_interrupt.py#L160-L187)。

```python
# 真实源码节选：保留决定性语句
if not any(r.mode == "interrupt" for r in rules):
    return {}

result: dict[str, InterruptOnConfig] = {}
for tool_name, (op, arg, scope, pattern_arg) in _FS_TOOL_PATH_ARGS.items():
    if not any(r.mode == "interrupt" and op in r.operations for r in rules):
        continue
    result[tool_name] = InterruptOnConfig(
        allowed_decisions=allowed,
        when=_make_fs_when_predicate(rules, op, arg, scope, pattern_arg, backend),
    )
return result
```

请注意其中的新概念：`when`。

它不是新的文件工具，也不是一个 Graph 节点。它是一个**针对某次具体 Tool Call 的判断函数**。

例如：

```text
edit_file("/shop/refund.py")
              ↓
when(...) 返回 True
              ↓
需要人工审批

read_file("/shop/refund.py")
              ↓
不匹配写入审批规则
              ↓
不需要人工审批
```

现在审批规则终于具备了两层判断：

```text
先按工具名找出可能需要审批的工具
                 ↓
再根据本次实际参数（如 file_path）决定是否中断
```

源文件里还有一项值得留意的设计：对于精确路径工具（如 `edit_file`），会检查目标路径；对于 `ls`、`grep` 等可能遍历子树的工具，则采用不同的重叠判断。真实实现还会考虑可解析的符号链接目标，防止简单路径别名绕过审批。这里无需深入每一种匹配边界，只要理解：**审批针对真实调用参数，而不只是工具名称**。

### 4.2 真实源码：`create_deep_agent()` 自动安装 HITL

还记得第七章的 Harness 装配层吗？

打开 [DeepAgents `graph.py` 第 878—883 行](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py#L878-L883)。

```python
# 真实源码节选
main_interrupt_on = _merge_fs_interrupt_on(
    _build_interrupt_on_from_permissions(permissions or [], backend),
    interrupt_on,
)
if main_interrupt_on is not None:
    deepagent_middleware.append(
        HumanInTheLoopMiddleware(interrupt_on=main_interrupt_on)
    )
```

调用者可以直接传入：

```python
agent = create_deep_agent(
    model=model,
    interrupt_on={"edit_file": True},
    checkpointer=checkpointer,
)
```

也可以让 `permissions` 中的 `interrupt` 规则触发自动安装。

这里还有一个严格的优先级：

```python
# 真实源码节选：graph.py，第 184—189 行
merged: dict[str, bool | InterruptOnConfig] = {**fs_interrupt_on}
if user_interrupt_on:
    merged.update(user_interrupt_on)
return merged
```

也就是说：**同一个工具名上，显式的 `interrupt_on` 配置会覆盖从文件权限生成的审批配置。**

例如显式配置 `{"edit_file": False}`，可能关闭这个工具名上原本由文件 `interrupt` 规则生成的人工审核。不要把两套配置误认为会自动进行逻辑“与”运算。

注意：`permissions` 是文件工具层面的限制；**不是 Backend 的通用安全策略**。如果其他代码直接调用 Backend，不会因为这里配置了 `permissions` 就自动走相同的文件权限 Middleware。生产环境仍需要容器、进程、文件权限等更底层隔离。

---

## 五、第四步：一次工具审核到底要交给人什么数据？

到这里，我们已经能判断“要不要审批”。但审批页面上总不能只显示一句“有危险操作”。

审批人必须看见：

- 模型准备调用的工具名称。
- 此次调用的参数。
- 可以选择哪些决定。

因此，LangChain 用结构化的审批请求表达它们。

### 5.1 真实源码：`HITLRequest`

源码：[LangChain `human_in_the_loop.py` 第 62—86 行](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/human_in_the_loop.py#L62-L86)。

```python
# 真实源码节选：类型定义，省略字段注释
class ActionRequest(TypedDict):
    name: str
    args: dict[str, Any]
    description: NotRequired[str]

class ReviewConfig(TypedDict):
    action_name: str
    allowed_decisions: list[DecisionType]

class HITLRequest(TypedDict):
    action_requests: list[ActionRequest]
    review_configs: list[ReviewConfig]
```

比如模型提出两次需要审批的修改：

```python
# 教学示例：批量审批请求的结构，不是实际调用的打印结果
{
    "action_requests": [
        {
            "name": "edit_file",
            "args": {
                "file_path": "/shop/refund.py",
                "old_string": "...",
                "new_string": "...",
            },
            "description": "修改退款计算逻辑",
        },
        {
            "name": "delete",
            "args": {"file_path": "/shop/tests"},
            "description": "删除测试目录",
        },
    ],
    "review_configs": [
        {"action_name": "edit_file", "allowed_decisions": ["approve", "reject"]},
        {"action_name": "delete", "allowed_decisions": ["approve", "reject"]},
    ],
}
```

现在程序不需要猜测人究竟批准了哪一个动作。

但这里又出现更关键的要求：**这些数据不能只被返回给模型；它们必须先返回给真正负责审批的人。**

所以 Agent 运行需要一种可以“先返回给外部客户端，再等待另一轮输入”的机制。

这时才有必要引入 LangGraph 的 `interrupt()`。

---

## 六、第五步：`interrupt()` 怎样使 Graph 暂停？

在最小 Python 程序里，我们可能会写：

```python
answer = input("是否批准修改 refund.py？")
```

这适合演示，但不适合真正的服务：

- API 请求可能已经结束。
- 服务进程可能重启。
- 审批人与运行 Agent 的机器不是同一台。
- 审批可能发生在很久之后。

因此我们需要的不只是等待输入，而是**把当前执行任务暂停成一种可恢复的状态**。

### 6.1 真实源码：`after_model()` 调用 `interrupt()`

默认的 `HumanInTheLoopMiddleware` 使用 `interrupt_mode="batched"`。

阅读 [LangChain `human_in_the_loop.py` 第 786—826 行](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/human_in_the_loop.py#L786-L826)。

```python
# 真实源码节选：只截取关键结构

def after_model(self, state, runtime):
    if self.interrupt_mode == "per_call":
        return None

    messages = state["messages"]
    # ... 找到本轮 AIMessage 的 tool_calls
    # ... 筛出需审核的调用并构造 HITLRequest

    decisions = interrupt(hitl_request)["decisions"]
    # ... 根据用户决定更新待执行的工具调用
```

这段代码的位置非常重要。

上一章我们知道：模型返回 `AIMessage.tool_calls` 后，才会由 ToolNode 真正分发工具。

而第六章已经研究过：`after_model` 是可以编译成 Graph 节点的生命周期 Hook。

因此，对于默认的批量审批模式，路径变为：

```text
Model Node
    |
    v
AIMessage.tool_calls
    |
    v
HumanInTheLoopMiddleware.after_model
    |
    +--- 没有需审批的调用 ---> ToolNode
    |
    +--- 发现需审批的调用
                  |
                  v
          interrupt(HITLRequest)
                  |
                  v
              Graph 暂停
                  |
                  v
          客户端展示给审批人
```

**在 `interrupt()` 处暂停时，待审批工具还没有进入实际执行阶段。**

这是为什么默认批量 HITL 放在 `after_model`，而不是在 Backend 已经修改文件之后才询问。

不过要准确描述：这条语义保证的是当前需要审核的工具调用先被暂停；同一轮里不受该策略保护的其他工具、已执行的之前节点或其他并行路径，其副作用不能被自动“回滚”。

### 6.2 `interrupt()` 不是普通函数返回

普通函数：

```python
value = ask_user()
```

通常一直运行到 `ask_user()` 返回。

而 LangGraph 的 `interrupt(value)` 会使当前 Graph 执行交出控制权，并把待审批信息暴露给调用方。再次恢复时，它才取得外部提供的答案并从该位置的逻辑继续推进。

阅读 [LangGraph `types.py` 的 `interrupt` 文档和实现](https://github.com/langchain-ai/langgraph/blob/main/libs/langgraph/langgraph/types.py#L823-L905)。其重要行为是：

1. 中断消息（`value`）向客户端暴露。
2. 暂停依赖 Checkpointer 保存可恢复状态。
3. 恢复使用 `Command(resume=...)`。
4. **恢复可能从当前被中断节点的起点重跑该节点逻辑**，而不是从 Python 字节码指令处接着执行。

最后一点将直接影响副作用安全性，后面会专门验证。

---

## 七、第六步：为什么 `interrupt()` 还需要 Checkpointer 和 `thread_id`？

假设 Agent 运行到这里：

```text
读取 refund.py              已完成
读取 coupon_service.py      已完成
分析尾差                   已完成
提出 edit_file              已完成
等待审批                   已暂停
```

如果审批页面关闭，我们希望第二天能够继续，而不是要求模型从最初的读文件阶段重新运行。

仅仅保存这份 `edit_file` 参数是不够的。

我们还需要知道：

- 当前是哪一次 Agent 会话。
- 消息历史进行到了哪里。
- 哪个 Graph 节点暂停了。
- 恢复后应该进入哪条执行路径。

这就是为什么需要持久化 Graph 的执行状态。

### 7.1 最小配置

```python
from langgraph.checkpoint.memory import InMemorySaver

checkpointer = InMemorySaver()
config = {
    "configurable": {
        "thread_id": "refund-fix-demo-001",
    }
}
```

这里先只理解两个东西：

- **Checkpointer**：保存可供恢复的 Graph 状态与执行位置。
- **`thread_id`**：这次调用在 Checkpointer 中使用的执行会话标识。

`InMemorySaver` 适合单进程示例；进程退出以后，内存中的内容就没有了。对于真实服务，应该使用适合部署环境的持久化 Checkpointer。

请注意：`store` 与 `checkpointer` 不是同一回事。`store` 可以用于长期数据；Graph 暂停恢复要依赖 Checkpointer，而不是仅仅给 `create_deep_agent()` 传一个 Store。

### 7.2 暂停和恢复不能换一个 `thread_id`

错误做法：

```python
# 教学示例：错误的恢复配置
first_config = {"configurable": {"thread_id": "task-A"}}
resume_config = {"configurable": {"thread_id": "task-B"}}
```

此时恢复请求指向了另一个会话标识，无法按预期找回 `task-A` 的暂停状态。

正确路径是：

```text
第一次执行：thread_id = refund-fix-demo-001
       ↓
Graph 发生 interrupt
       ↓
审批人作出决定
       ↓
Command(resume=...), thread_id 仍为 refund-fix-demo-001
```

到这里，我们解决的是“恢复哪一次任务”的问题。

接下来还差一个问题：审批人到底怎样表达决定？

---

## 八、第七步：批准、拒绝、编辑与代答，为什么不能都叫 `approved`？

最小设计只有一个布尔值：

```python
approved = True
```

但真实审批至少有四种行为。

| 决定 | 期望行为 |
|---|---|
| `approve` | 使用原请求执行工具 |
| `reject` | 不执行工具，并把拒绝原因反馈模型 |
| `edit` | 先替换原调用的部分内容，再执行修改后的动作 |
| `respond` | 不执行工具，由人提供一条相当于工具结果的响应 |

真实类型定义位于 [LangChain `human_in_the_loop.py` 第 105—168 行](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/human_in_the_loop.py#L105-L168)。

```python
# 真实源码节选：保留四种决定的关键字段
class ApproveDecision(TypedDict):
    type: Literal["approve"]

class EditDecision(TypedDict):
    type: Literal["edit"]
    edited_action: Action

class RejectDecision(TypedDict):
    type: Literal["reject"]
    message: NotRequired[str]

class RespondDecision(TypedDict):
    type: Literal["respond"]
    message: str
```

为什么要分这么细？让我们回到退款任务。

### 8.1 批准

审批人认为计算逻辑已经经过人工确认，同意更新 `refund.py`：

```python
{"type": "approve"}
```

它表示允许原工具调用继续执行，并不代表修改已经成功。

### 8.2 拒绝

审批人发现模型准备修改的是测试目录：

```python
{
    "type": "reject",
    "message": "不要删除测试目录，请保留失败用例并分析失败原因。",
}
```

这不应该悄无声息地结束整个 Agent。

Agent 需要得知：**这个工具没有执行，而且为什么没有执行。**

源码中 `_answer_message()` 会构造带原 `tool_call_id` 的 `ToolMessage`，并在拒绝情况下设置 `status="error"`。参见 [第 266—284 行](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/human_in_the_loop.py#L266-L284)。

因此：

```text
模型：我要删除 /shop/tests
        ↓
审批：reject
        ↓
ToolMessage：用户拒绝本次 delete，未执行
        ↓
模型：保留测试目录，继续分析失败日志
```

这就把人工反馈重新接入了 Agent Loop。

注意：**拒绝一次工具调用不等于结束整个 Agent，也不等于让模型永远无法再尝试类似动作。** 应用层还需持续执行政策。

### 8.3 编辑

假设模型试图改写 `/shop/refund.py` 的整段逻辑，但审批人只允许修改一行。

```python
{
    "type": "edit",
    "edited_action": {
        "name": "edit_file",
        "args": {
            "file_path": "/shop/refund.py",
            "old_string": "refund = round(total * ratio, 2)",
            "new_string": "refund = quantize_refund(total, ratio)",
        },
    },
}
```

这会引出一个微妙问题：原 `AIMessage` 中的 `tool_calls` 是模型产生的，怎样既保留模型提出过什么，又确保实际执行的是人审核后的版本？

在默认批量模式，`after_model()` 将编辑信息保存到内部状态字段 `hitl_edited_tool_calls`，后续 `wrap_tool_call()` 读取它，替换执行时的调用参数，并在结果上附加必要说明。参见 [第 838—858 行](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/human_in_the_loop.py#L838-L858) 与 [第 1009—1015 行](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/human_in_the_loop.py#L1009-L1015)。

**编辑发生在审批决策中，但实际执行的替换工作发生在工具调用包装链里。**

### 8.4 代答

如果某个工具本质上是在向人提问，那么人可以直接给它一条结果：

```python
{
    "type": "respond",
    "message": "财务负责人要求：优惠券尾差归最后一笔退款。",
}
```

在这种情况下工具不会实际执行，系统会生成一条成功状态的合成 `ToolMessage`。

`respond` 适合“由人代替工具提供答案”的情况，**不应该把它当成已经运行过 pytest 或已经完成文件修改的证据**。

---

## 九、第八步：怎样在真实 Agent 中暂停并恢复一次工具调用？

我们现在拥有必要的最小概念：

```text
Tool Call → 审批规则 → interrupt → Checkpoint
                                     ↓
                            Command(resume)
                                     ↓
                              工具执行或拒绝
```

下面把它们拼成一个真实接口层面的示例。

### 9.1 示例 A：审批 `edit_file`

```python
# 框架接口示例：需要安装兼容版本、配置真实模型与 API Key
from deepagents import create_deep_agent
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

agent = create_deep_agent(
    model="openai:gpt-5.5",  # 用你已配置的模型替换
    interrupt_on={
        "edit_file": {
            "allowed_decisions": ["approve", "reject"],
        },
    },
    checkpointer=InMemorySaver(),
)

config = {"configurable": {"thread_id": "refund-review-001"}}

first = agent.invoke(
    {
        "messages": [
            {
                "role": "user",
                "content": (
                    "检查 /shop/refund.py 的优惠券部分退款精度问题。"
                    "在任何 edit_file 之前等待我审批。"
                ),
            }
        ]
    },
    config=config,
    version="v2",
)

if first.interrupts:
    for pending in first.interrupts:
        print("待审核:", pending.value)

    # 教学示例：由真实审核流程决定，不能在生产中无条件自动批准
    resumed = agent.invoke(
        Command(resume={"decisions": [{"type": "approve"}]}),
        config=config,
        version="v2",
    )
    print(resumed)
```

这个例子演示的是 API 形状，**不声称模型一定会在本次调用中生成 `edit_file`**。模型可能先读文件，也可能因为默认 Backend 中没有对应文件而选择其他动作。只有观察到 `first.interrupts` 中确实存在审核请求后，才可以提交对应的审批决定。若同时存在多项待审批调用，则 `decisions` 数量和顺序必须与 `action_requests` 对应，不能只传一项。

这里还有一个实际问题：使用 `StateBackend` 时，不会自动拥有当前磁盘上的 `/shop/refund.py`。第九章已经研究了如何通过 State 文件或合适的 FilesystemBackend 提供真实项目文件；这里不重复展开。

### 9.2 示例 B：让运行 pytest 前必须审批

本系列第十章的目标还包括：**实现一个运行测试前需要审批的 Agent。**

与其依赖某个 Backend 是否支持 `execute`，我们可以先为演示定义一个明确的测试工具，再对这个工具加审核闸门。

```python
# 框架接口示例：实际运行需安装依赖并配置模型
from pathlib import Path
import subprocess

from deepagents import create_deep_agent
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

PROJECT_ROOT = Path("/path/to/your/ecommerce-project").resolve()


@tool
def run_refund_tests() -> str:
    """在项目目录运行 shop/tests/test_refund.py 的 pytest。"""
    result = subprocess.run(
        ["python", "-m", "pytest", "shop/tests/test_refund.py", "-q"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return (
        f"exit_code={result.returncode}\n"
        f"stdout:\n{result.stdout[-8000:]}\n"
        f"stderr:\n{result.stderr[-4000:]}"
    )


agent = create_deep_agent(
    model="openai:gpt-5.5",  # 替换为已配置的模型
    tools=[run_refund_tests],
    interrupt_on={
        "run_refund_tests": {
            "allowed_decisions": ["approve", "reject"],
        }
    },
    checkpointer=InMemorySaver(),
)

config = {"configurable": {"thread_id": "pytest-approval-001"}}

result = agent.invoke(
    {
        "messages": [
            {
                "role": "user",
                "content": (
                    "请检查优惠券部分退款精度问题。"
                    "在需要验证时调用 run_refund_tests；"
                    "运行测试前须等待人工审批。"
                ),
            }
        ]
    },
    config=config,
    version="v2",
)

for pending in result.interrupts:
    print("待审批：", pending.value)

# 真正的审批应由用户通过 UI 或 API 提交。
# 仅当 result.interrupts 中存在该工具的审批请求时，才提交对应决策。
# resumed = agent.invoke(
#     Command(resume={"decisions": [{"type": "approve"}]}),
#     config=config,
#     version="v2",
# )
```

为什么专门用自定义 `run_refund_tests`？因为它能在不假设 Backend 实现 Shell 协议的情况下，把研究焦点放在“**工具已经注册，但在它运行之前需要批准**”这件事上。

这个例子有两个重要的安全前提：

1. `PROJECT_ROOT` 必须由可信部署配置指定，不由模型随意提供；演示代码只运行固定的测试文件，而不是任意 Shell 字符串。
2. 这并不是操作系统级沙箱。pytest 可以执行项目中的 Python 代码，生产环境仍需隔离和资源限制。

测试结束后应该检查真实的 `exit_code`。工具被批准只表示**允许运行**，不表示**测试已经通过**。

---

## 十、第九步：默认批量审批为什么放在 `after_model`，而不是 `wrap_tool_call`？

第六章已经看到：

```text
after_model    → Graph 生命周期节点
wrap_tool_call → ToolNode 内部的调用包装链
```

乍看之下，把审批写在 `wrap_tool_call()` 就足够了。为什么固定版本默认选择 `after_model()`？

因为我们现在希望处理的，不一定是一个工具。

模型可能在一次回答中提出：

```text
edit_file("/shop/refund.py")
edit_file("/shop/coupon_service.py")
run_refund_tests()
```

如果审查发生在整个模型轮次结束之后，可以一次把所有需要确认的动作展示给人。

这就是默认的 `batched` 模式。

### 10.1 真实源码：`after_model()` 如何收集多个待审批调用

在 [LangChain 第 786—798 行](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/human_in_the_loop.py#L786-L798)：

```python
# 真实源码节选
for idx, tool_call in enumerate(last_ai_msg.tool_calls):
    if (config := self.interrupt_on.get(tool_call["name"])) is not None:
        if not self._should_interrupt(tool_call, config, state, runtime):
            continue
        action_request, review_config = self._create_action_and_config(
            tool_call, config, state, runtime
        )
        action_requests.append(action_request)
        review_configs.append(review_config)
        interrupt_indices.append(idx)

if not action_requests:
    # ... 清理过往编辑状态并正常继续
    ...

hitl_request = HITLRequest(
    action_requests=action_requests,
    review_configs=review_configs,
)
decisions = interrupt(hitl_request)["decisions"]
```

这段代码解决的是**一次模型回答里出现多个工具请求**的问题。

- 它只收集命中审核规则的工具调用。
- 未命中规则的调用不需要出现在 `action_requests` 中。
- 收集到的待审批动作组成一次审核请求。
- 恢复后依据每项决定逐一处理。

接着在 [第 820—858 行](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/human_in_the_loop.py#L820-L858) 检查审批决定的数量、构造合成工具消息，以及保留经过批准的工具调用。

这段源码同时解释了前面的问题：**拒绝不应该只在后台设置一个布尔值，而需要变成能反馈给模型的执行结果。**

### 10.2 批量审批的恢复格式

```python
# 教学示例：假设本轮恰好有两个待审批工具调用
from langgraph.types import Command

resume = Command(
    resume={
        "decisions": [
            {"type": "approve"},
            {
                "type": "reject",
                "message": "不要删除测试目录。",
            },
        ]
    }
)
```

**数组顺序对应本次 `HITLRequest.action_requests` 的顺序**，而不是由你凭工具名字自行排序。

如果系统同时展示多个审批项，UI 需要保留这个关联关系。

---

## 十一、第十步：为什么还提供 `per_call` 模式？

现在我们知道：批量审批适合一次审核一轮模型生成的多个动作。

但有些产品希望每个工具调用独立拥有一条审批事项，比如：

- `/shop/refund.py` 的修改交给代码 Reviewer。
- 测试执行交给 CI 管理员。
- 不同审批可以分开处理。

于是有了第二种模式：`per_call`。

固定版本的 `HumanInTheLoopMiddleware.__init__` 中有：

```python
# 真实源码节选
interrupt_mode: InterruptMode = "batched"
```

所以 **0.7.32 的默认值仍然是 `batched`**，不能把 `per_call` 描述成当前默认行为。

### 11.1 中断位置从 `after_model` 移到 `wrap_tool_call`

阅读 [LangChain 第 1009—1015 行](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/human_in_the_loop.py#L1009-L1015)。

```python
# 真实源码节选
if self.interrupt_mode == "per_call":
    resolved = self._resolve_per_call(request)
    if isinstance(resolved, ToolMessage):
        return resolved
    to_run, executed = resolved
    return self._annotate_edited_result(handler(to_run), to_run, executed)
```

与此同时，`after_model()` 在此模式下直接返回 `None`。

因此：

```text
batched（默认）：
Model → after_model → interrupt(本轮待审调用集合) → Tools

per_call：
Model → Tools → wrap_tool_call → interrupt(单次调用) → handler
```

两者都可以保证该工具调用在获准前不真正进入 `handler`，但拦截位置不同。

### 11.2 每个工具调用都有独立中断 ID

在 `per_call` 模式下，暂停值的形状是 `ToolApprovalRequest`：

```python
# 教学示例：符合真实字段结构的审批值
{
    "type": "tool_approval",
    "tool_call_id": "call_edit_001",
    "name": "edit_file",
    "args": {"file_path": "/shop/refund.py", "old_string": "...", "new_string": "..."},
    "description": "请审批修改退款计算逻辑",
}
```

每项中断都有由 LangGraph 提供的 interrupt ID。恢复时必须按 **中断 ID** 提交单项决定，而不是采用批量模式的 `{"decisions": [...]}` 格式。

```python
# 框架接口示例：需自行配置好 Agent 和 Checkpointer
from langgraph.types import Command

answers = {}
for pending in result.interrupts:
    if pending.value["name"] == "delete":
        answers[pending.id] = {
            "type": "reject",
            "message": "保留测试目录",
        }
    else:
        answers[pending.id] = {"type": "approve"}

resumed = agent.invoke(
    Command(resume=answers),
    config=config,
    version="v2",
)
```

此模式更适合独立审批与带类型校验的交互。源码中 `interrupt(value, response_schema=...)` 会使用针对当前工具构造的响应类型，限制允许的决定以及编辑参数的类型。但它仍不能替代工具自己的完整校验，也不能替代底层权限控制。

### 11.3 DeepAgents 0.7.32 中怎样启用 `per_call`？

这里有一个很容易误写的版本细节。

`create_deep_agent()` 的公开签名有 `interrupt_on` 参数，但**没有单独的 `interrupt_mode` 参数**。它自己装配 `HumanInTheLoopMiddleware` 时没有指定这个模式，所以使用默认的 `batched`。

如果你的系统明确需要 `per_call`，一种方式是自行构造 Middleware，再利用第七章已经介绍过的“按 `.name` 原位替换”的装配规则：

```python
# 框架接口示例：通过同名实例替换自动装配的 HITL Middleware
from deepagents import create_deep_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware
from langgraph.checkpoint.memory import InMemorySaver

per_call_review = HumanInTheLoopMiddleware(
    interrupt_on={
        "edit_file": {"allowed_decisions": ["approve", "reject"]},
    },
    interrupt_mode="per_call",
)

agent = create_deep_agent(
    model="openai:gpt-5.5",
    middleware=[per_call_review],
    checkpointer=InMemorySaver(),
)
```

此时是用户提供了自己的 HITL 策略，而不是 `create_deep_agent()` 自带一个新的顶层开关。若你同时使用路径权限自动生成 HITL，必须特别检查最终替换实例的配置是否完整，因为同名替换并不意味着两份审核规则会自动深度合并。

### 11.4 本章没有展开的那一半

到这里，我们只回答了“两种模式在哪儿暂停”。

但一个决定从 `interrupt()` 返回之后，还要经过好几道手：`_process_decision()` 怎么解析四个分支、被拒绝的调用怎么从 `tool_calls` 里消失、`hitl_edited_tool_calls` 为什么不直接改 `AIMessage`、`_apply_edit()` 为什么能换工具、`per_call` 的 `response_schema` 是怎么在运行时造出来的、`when` 谓词在两种模式下为什么拿到的不是同一个对象。

这些是“暂停之后发生了什么”，属于实现层的细节，放在正文里会打断阅读节奏。它们集中在 [第十章附：同一次审批，两条执行路径](./10a-human-in-the-loop-modes.md) 里逐函数拆解。

如果你只需要判断“我的项目该用哪个模式”，读完本章就够了；如果你要自己实现一个审批 Middleware，或者排查“审批明明拒绝了工具却还是执行了”这类问题，那一章会更直接有用。

---

## 十二、第十一步：恢复执行为什么必须考虑副作用重复？

现在，Agent 已经可以暂停、保存状态、接收决定，再继续执行。

但一个容易产生错误安全感的说法是：

> “用了 Checkpointer，工具就绝对不会重复执行。”

这不成立。

### 12.1 先看一个危险的最小实现

```python
# 概念伪代码：错误的副作用放置位置

def review_node(state):
    charge_refund_money()        # 外部副作用发生在审批前
    decision = interrupt({"message": "是否继续？"})
    return {"decision": decision}
```

在 LangGraph 的中断恢复语义中，被中断节点可能从开头再次执行。

如果副作用位于 `interrupt()` 之前，恢复时就可能重复执行这段操作。

这里的风险不是理论上的“模型可能犯错”，而是可直接推导的执行顺序：

```text
首次：  charge_refund_money() → interrupt() → 暂停
恢复：  charge_refund_money() → interrupt()取得 resume 值
```

这正说明：**Checkpoint 能帮助恢复执行位置，却不会自动为外部支付、文件写入或网络请求提供“精确一次”的副作用保证。**

### 12.2 调整顺序：先拿到审批，再允许副作用

```python
# 教学化简：更安全的基本方向

def review_node(state):
    decision = interrupt({"message": "是否执行退款修改？"})
    if decision != "approve":
        return {"status": "rejected"}

    perform_mutation_once_if_possible()
    return {"status": "done"}
```

这样至少保证：用户未批准之前，不会执行 `perform_mutation_once_if_possible()`。

但这里仍然不能承诺“永不重复”。因为外部动作完成后，如果程序在持久化结果之前崩溃，重试仍可能发生。

要解决更强的副作用语义，需要业务层的幂等请求 ID、执行记录、数据库事务、去重键或补偿机制。它们不属于 HITL Middleware 本身。

### 12.3 `per_call` 模式还有 Wrapper 顺序要求

`HumanInTheLoopMiddleware` 的源码文档提示：在 `per_call` 模式下，审查发生于 Tool Wrapper 链内。位于它之前的 Wrapper，在恢复时可能再次运行到中断位置。因此不要在审批之前就做不可重复的副作用。

对于退款系统，至少应把真实资金操作与日志、通知等副作用明确分离。**审核是授权边界，不是事务边界。**

---

## 十三、最小验证实验：不需要模型，先亲眼观察“拒绝不执行”

我们先用纯 Python 验证这章最核心的因果关系。实验只验证审批闸门的决策语义，**不假装复现 LangGraph Checkpointer 的内部运行**。

创建 `chapter10_minimal.py`：

```python
# 纯 Python 实验：零第三方依赖
from dataclasses import dataclass


@dataclass
class ToolCall:
    name: str
    path: str
    call_id: str


class MockBackend:
    def __init__(self):
        self.writes = []

    def edit(self, path):
        self.writes.append(path)
        return "file updated"


def review_then_execute(call, decision, backend):
    if decision == "reject":
        return {
            "tool_call_id": call.call_id,
            "status": "error",
            "content": "Reviewer rejected this edit",
        }
    if decision != "approve":
        raise ValueError("Unsupported decision")
    return {
        "tool_call_id": call.call_id,
        "status": "success",
        "content": backend.edit(call.path),
    }


def main():
    backend = MockBackend()
    call = ToolCall("edit_file", "/shop/refund.py", "call_001")

    denied = review_then_execute(call, "reject", backend)
    assert denied["status"] == "error"
    assert denied["tool_call_id"] == "call_001"
    assert backend.writes == []

    approved = review_then_execute(call, "approve", backend)
    assert approved["status"] == "success"
    assert backend.writes == ["/shop/refund.py"]

    print("OK: reject prevents mutation; approve executes once in this demo")


if __name__ == "__main__":
    main()
```

运行：

```bash
python chapter10_minimal.py
```

**预期输出：**

```text
OK: reject prevents mutation; approve executes once in this demo
```

实验要验证的是：审批结果先于实际文件修改；拒绝产生结构化反馈；只有批准才调用 Backend。

实验**没有**验证模型智能、LangGraph 的真实中断恢复、Checkpointer 的持久化、并发或真实退款资金安全。这些是不同层级的问题，不能用一次 MockBackend 断言全部覆盖。

### 13.1 框架级验证清单

准备好兼容环境与可用模型后，可以依次验证：

1. `interrupt_on={"edit_file": True}`，模型确实提出 `edit_file` 时，结果中出现待审批中断。
2. 首次中断之前，待审批的工具调用未产生实际文件修改。
3. 使用相同 `thread_id` 和 `Command(resume=...)` 批准后，才发生对应操作。
4. 拒绝后，模型看到带原 `tool_call_id` 的拒绝反馈，而不是虚假的成功消息。
5. 将模式改为 `per_call` 后，中断值的结构与恢复数据结构一起变化。
6. 故意更换 `thread_id`，观察无法按照原暂停记录继续。
7. 在自己的实验环境中尝试中断前放置计数副作用，观察恢复时的节点重入风险。**不要用真实支付调用做这个实验。**

框架环境的实验取决于实际模型是否生成相应工具调用；没有运行之前，只能把以上结果称作“应验证的行为”，不能称为已通过的测试。

---

## 十四、现在才把完整执行链拼出来

回看这一章，我们不是先背诵了 HITL、Checkpoint 和 Command 三个名词，而是被退款任务的真实问题一步步推动：

```text
最初：Model → ToolNode → Backend
                     |
                     v
                文件真实发生修改
```

当我们发现“模型能执行并不等于有权限执行”，增加了工具层面的权限规则：

```text
Model → ToolNode → Permission Check → Backend
                       |
                  allow / deny
```

接着发现有些操作不该直接允许或拒绝，而应该交给人判断：

```text
Model → HITL Review → ToolNode → Backend
             |
          interrupt
             |
           Human
```

最后发现人可能很久以后才回复，因此需要保存并恢复 Graph 执行状态：

```text
                     ┌─────────────────────────────┐
                     │        Checkpointer         │
                     │  同一个 thread_id 的状态     │
                     └──────────────┬──────────────┘
                                    |
                                    v
User → Model → after_model/HITL → interrupt → 外部审批人
                      ^                             |
                      |                       Command(resume)
                      |                             |
                      +-------- Graph 恢复 ---------+
                                    |
                             批准 / 编辑 / 拒绝
                                    |
                                    v
                         ToolNode / ToolMessage
                                    |
                           Backend / 下一轮 Model
```

这张图是**默认批量模式的控制流程概览**，不是所有模式和所有并行分支的精确调度图。若采用 `per_call`，审核中断位于 Tool Wrapper 里，而不是 `after_model` 中。

### 14.1 本章必须记住的边界

**一、工具权限不等于模型提示词。** Prompt 中写“不要删除测试”不能代替实际工具授权。

**二、HITL 不等于文件权限系统。** 文件工具有自己的 `deny` 检查；HITL 管理人工审批；外部 Backend 仍需独立权限与隔离。

**三、`interrupt_on=True` 不是无条件让整个 Agent 停止。** 它必须命中配置的工具调用，默认批量模式里还要经过工具名、可选 `when` 判断。

**四、恢复不等于重新向模型发送用户提问。** 正确恢复应使用同一 Checkpointer 会话和 `Command(resume=...)`。

**五、批准不等于执行成功。** 经过批准的 `edit_file` 仍可能由于参数错误、路径限制、Backend 错误而失败；pytest 获批仍可能返回非零退出码。

**六、Checkpoint 不等于“精确一次”事务。** 外部副作用仍要依赖幂等设计、权限隔离和必要的业务事务。

---

## 十五、下一章：既然能安全地执行，为什么还需要显式计划？

现在我们的 Coding Agent 已经能够：

1. 读取 `refund.py` 和相关实现。
2. 推导优惠券部分退款的精度风险。
3. 提出修改和测试动作。
4. 在敏感动作执行前请求人工审核。
5. 接收批准、拒绝或编辑，并继续 Agent Loop。

但是它仍然可能出现一种很常见的失败：

```text
读取 refund.py
  ↓
修改 refund.py
  ↓
运行 pytest
  ↓
测试失败
  ↓
又读取 refund.py
  ↓
又修改同一段代码
  ↓
忘记检查 coupon_service.py
```

HITL 可以控制“这一步是否被允许”，却不能保证“整次任务的步骤是否完整、有序、可追踪”。

这就引出了下一章真正需要解决的问题：

> **Agent 已经能循环、能使用工具、能暂停审批，为什么还需要把任务计划变成显式的状态？**

下一章我们将围绕 `TodoListMiddleware`、`write_todos` 和 Agent State，研究 Planning 为什么不能简单等同于让模型在自然语言里写一张待办清单。

**第十一章：复杂任务如何规划？——Planning 与 Todo。**

---

## 附：本章源码导航

| 想验证的问题 | 固定源码入口 |
|---|---|
| DeepAgents 怎样接收 `permissions`、`interrupt_on`、`checkpointer` | [`graph.py#L262-L282`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py#L262-L282) |
| DeepAgents 怎样合并权限生成的审核和用户审核配置 | [`graph.py#L174-L189`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py#L174-L189) |
| DeepAgents 何时安装 HITL Middleware | [`graph.py#L878-L883`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py#L878-L883) |
| DeepAgents 怎样给 LangChain 传递 Checkpointer | [`graph.py#L920-L933`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py#L920-L933) |
| 文件权限规则类型、按顺序匹配 | [`filesystem.py#L342-L389`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py#L342-L389) |
| 将文件规则转为带 `when` 的 `interrupt_on` | [`_fs_interrupt.py#L160-L187`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/_fs_interrupt.py#L160-L187) |
| 精确路径如何触发审批 | [`_fs_interrupt.py#L83-L99`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/_fs_interrupt.py#L83-L99) |
| HITL 请求、允许决定的类型 | [`human_in_the_loop.py#L62-L168`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/human_in_the_loop.py#L62-L168) |
| `InterruptOnConfig` 中可选的 `when` | [`human_in_the_loop.py#L356-L388`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/human_in_the_loop.py#L356-L388) |
| HITL 默认 `interrupt_mode="batched"` | [`human_in_the_loop.py#L495`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/human_in_the_loop.py#L495) |
| 批量审批如何在 `after_model` 收集并中断 | [`human_in_the_loop.py#L786-L826`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/human_in_the_loop.py#L786-L826) |
| 恢复后如何构建原调用与合成工具消息 | [`human_in_the_loop.py#L820-L858`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/human_in_the_loop.py#L820-L858) |
| `per_call` 如何在 Tool Wrapper 中审核 | [`human_in_the_loop.py#L727-L756`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/human_in_the_loop.py#L727-L756)、[`#L979-L1015`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/middleware/human_in_the_loop.py#L979-L1015) |
| `interrupt`、`Command(resume=...)` 的执行语义 | [LangGraph `types.py` 的 `Command` 与 `interrupt`](https://github.com/langchain-ai/langgraph/blob/main/libs/langgraph/langgraph/types.py#L765-L905)（该入口是 API 阅读参考；严格复现请固定依赖版本） |

**本章核心产出：** 读者不仅知道给 `create_deep_agent()` 传 `interrupt_on`，还能够沿源码解释审核规则从哪里来、为何默认在 `after_model` 中断、怎样通过 Checkpointer 和 `Command(resume=...)` 恢复、批准与拒绝如何返回 ToolNode/模型，以及为什么审批不能代替底层权限或副作用幂等设计。
