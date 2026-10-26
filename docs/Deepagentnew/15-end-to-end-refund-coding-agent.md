---
title: 第十五章：一次退款 Bug 修复怎样贯穿整个 Agent Harness？——DeepAgents 端到端源码走读
description: 以优惠券部分退款 0.01 元尾差为唯一案例，从失败测试出发，沿 DeepAgents 0.7.32 的 create_deep_agent 装配入口、LangChain Agent Loop、ToolNode、Filesystem、Backend、Middleware、HITL、Subagents、State 和 Streaming 追踪一次完整修复；给出真实通过的纯 Python 业务回归实验和需要模型环境的框架集成示例。
tags: [DeepAgents, LangChain, LangGraph, Agent Harness, Coding Agent, ToolNode, Middleware, Backend, Checkpoint, 源码精读]
sidebar_position: 20
---

# 第十五章：一次退款 Bug 修复怎样贯穿整个 Agent Harness？——DeepAgents 端到端源码走读

> **本章核心问题：** 前十四章已经分别解释了 Agent Loop、Middleware、Backend、Human-in-the-Loop、Subagents、State 和 Streaming。可是当用户真正说出“修复优惠券部分退款 Bug，并运行测试”时，这些组件究竟**按什么顺序发生作用**？哪一层负责决策，哪一层执行工具，哪一层保存状态，最终怎样判断任务真的完成？

## 一、先不要画总架构图：从一个能复现的 Bug 开始

系列最初，我们只有一句任务描述：

```text
修复 shop/refund.py 中优惠券部分退款的精度问题。
请检查相关代码、修改实现、补充回归测试、运行 pytest，
失败时继续修复，最后说明实际测试结果。
```

直到这一章，我们才有足够的知识把它完整走一遍。

但如果一上来就把十几个框架组件摆到图上，读者又会回到“记忆术语”的状态。因此我们先把框架暂时放在一边，做一件很小的事：**让退款 Bug 在 Python 中真实失败。**

### 1.1 一个具体的金额反例

假设：

- 订单原金额为 `0.30` 元。
- 订单使用优惠券抵扣 `0.10` 元。
- 消费者实际支付 `0.20` 元。
- 消费者分三次退回原金额，每次 `0.10` 元。

如果每次都独立计算“实付金额 × 本次退款比例”，并在每次退款时四舍五入到分：

```text
第一次：0.20 × (0.10 / 0.30) → 0.07
第二次：0.20 × (0.10 / 0.30) → 0.07
第三次：0.20 × (0.10 / 0.30) → 0.07
                                   ─────
                                   0.21
```

系统最终退款 `0.21` 元，但用户实际只支付 `0.20` 元。

这里有两类容易混淆的问题：

1. **二进制浮点数误差**：使用 `float` 表示十进制货币可能失去精确性。
2. **逐次舍入的累计尾差**：即使全程使用 `Decimal`，分别计算并舍入多次退款仍可能多退或少退一分钱。

我们的例子故意使用 `Decimal`，是为了证明：**换成精确十进制表示仍然不够，业务上还需要累计分摊规则。**

### 1.2 本章的源码边界

框架阅读继续固定在以下版本：

```text
DeepAgents Python    0.7.32
DeepAgents Commit    e85c6b9
LangChain            1.4.7（系列阅读基线）
LangGraph            与上述依赖兼容的安装版本
```

本章会引用以下真实入口：

| 职责 | 固定版本或机制入口 |
| --- | --- |
| Harness 装配 | [`deepagents/graph.py`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py) |
| 文件工具 | [`middleware/filesystem.py`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py) |
| 本地文件 Backend | [`backends/filesystem.py`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/backends/filesystem.py) |
| 命令执行 Backend | [`backends/local_shell.py`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/backends/local_shell.py) |
| 同步子 Agent | [`middleware/subagents.py`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/subagents.py) |
| 消息合并 | [`_messages_reducer.py`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/_messages_reducer.py) |
| 通用 Graph 构建器 | [LangChain 1.4.7 `factory.py`](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/factory.py) |
| 工具调度 | [LangChain `tool_node.py`（机制参考）](https://github.com/langchain-ai/langchain/blob/main/libs/langchain_v1/langchain/tools/tool_node.py) |
| LangGraph 执行与流 | [LangGraph Streaming 文档](https://docs.langchain.com/oss/python/langgraph/streaming) |

需要注意：**锁定 DeepAgents Commit 不等于锁定 LangGraph 的全部内部实现。** 文中引用 LangGraph 或依赖仓库 `main` 分支时，只用来指示机制入口；不把它冒称为 `e85c6b9` 内部的源码。

文中所有代码分成三类：

- **真实源码节选**：来自上述固定代码，并明确说明位置；只截取支持推论的部分。
- **教学化简 / 概念伪代码**：用来说明执行顺序，不冒充真实实现。
- **可复现实验**：业务代码和 `pytest` 真正在本章编写环境运行；这不等于真实大模型的 Agent 运行已被验证。

---

## 二、第一步：最小 Python 项目为什么会失败？

先建立只有两个模块和一个测试文件的项目。

```text
refund-demo/
├── shop/
│   ├── __init__.py
│   ├── coupon_service.py
│   └── refund.py
└── tests/
    └── test_refund.py
```

### 2.1 `coupon_service.py`：只负责计算实付金额

**可复现实验源码：**

```python
# shop/coupon_service.py
from decimal import Decimal


def payable_amount(original: Decimal, coupon: Decimal) -> Decimal:
    if original <= 0 or coupon < 0 or coupon > original:
        raise ValueError("invalid original amount or coupon")
    return original - coupon
```

这个文件现在没有复杂机制。它的职责只有一个：根据订单原金额和优惠券金额计算实付金额。

### 2.2 `refund.py`：一个看起来合理、其实有尾差的实现

**可复现实验源码，故意保留 Bug：**

```python
# shop/refund.py
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from shop.coupon_service import payable_amount

CENT = Decimal("0.01")


def cents(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass
class RefundLedger:
    original: Decimal
    coupon: Decimal
    refunded_original: Decimal = Decimal("0.00")
    refunded_paid: Decimal = Decimal("0.00")


def refund_partial(ledger: RefundLedger, portion: Decimal) -> Decimal:
    paid = payable_amount(ledger.original, ledger.coupon)
    if portion <= 0 or ledger.refunded_original + portion > ledger.original:
        raise ValueError("invalid refund portion")

    # BUG：每次退款都独立按比例舍入。
    refund = cents(paid * portion / ledger.original)

    ledger.refunded_original += portion
    ledger.refunded_paid += refund
    return refund
```

这份实现不是完全胡乱编造的错误代码。

它使用了 `Decimal`，有金额校验，也保存了已经退款的原金额和实付金额。面对一次性整单退款，它甚至能得到正确结果。

问题只在于：**本次应退金额没有参考此前已经完成的退款。**

因此一笔单独退款在局部看起来合理，三笔合在一起就不合理了。

### 2.3 写出能抓住问题的测试

```python
# tests/test_refund.py
from decimal import Decimal
import pytest
from shop.refund import RefundLedger, refund_partial


def test_three_partial_refunds_preserve_total():
    ledger = RefundLedger(
        original=Decimal("0.30"),
        coupon=Decimal("0.10"),
    )
    amounts = [refund_partial(ledger, Decimal("0.10")) for _ in range(3)]

    assert amounts == [Decimal("0.07"), Decimal("0.06"), Decimal("0.07")]
    assert ledger.refunded_paid == Decimal("0.20")


def test_full_refund_once():
    ledger = RefundLedger(
        original=Decimal("100.00"),
        coupon=Decimal("20.00"),
    )
    assert refund_partial(ledger, Decimal("100.00")) == Decimal("80.00")


def test_over_refund_rejected_without_state_change():
    ledger = RefundLedger(
        original=Decimal("10.00"),
        coupon=Decimal("1.00"),
    )
    with pytest.raises(ValueError):
        refund_partial(ledger, Decimal("10.01"))
    assert ledger.refunded_original == Decimal("0.00")
    assert ledger.refunded_paid == Decimal("0.00")
```

在项目根目录执行：

```bash
python -m pytest -q tests/test_refund.py
```

**实际执行结果（本章实验环境）：**

```text
FAILED tests/test_refund.py::test_three_partial_refunds_preserve_total
1 failed, 2 passed
```

失败信息表明，实际得到的列表为：

```python
[Decimal("0.07"), Decimal("0.07"), Decimal("0.07")]
```

而测试期望第二笔退款只有 `0.06` 元。

这就构成了后面 Agent 需要面对的**具体、可观察的失败事实**。

> 这里的测试是真实运行的普通 Python 业务测试，并没有使用 DeepAgents，也没有大模型参与。它证明 Bug 存在，不证明 Agent 能自动找到它。

---

## 三、第二步：让一个 Agent 只回答建议，为什么不够？

假设现在把错误代码复制给模型，并问它：

```text
请解释 refund_partial() 的问题，并给出修复建议。
```

模型可能给出合理建议，比如“使用累计退款目标金额计算本次差额”。

但是我们的任务不是收获一段建议，而是要：

```text
在真实项目里读取文件
  ↓
修改退款实现
  ↓
运行 pytest
  ↓
如果失败，继续调整
```

只使用一次模型调用，至少缺少两件事情。

第一，模型必须能够对外部项目产生实际操作；第二，操作完成后必须把结果反馈给模型，让它决定下一步。

我们可以先写出最小的概念循环：

```python
# 概念伪代码：不是 DeepAgents 的实现
messages = [user_message]

while True:
    response = model(messages)
    messages.append(response)

    if not response.tool_calls:
        break

    for call in response.tool_calls:
        result = execute_tool(call)
        messages.append(result)
```

现在 Agent 可以形成：

```text
用户任务
   ↓
Model：我需要先读取 refund.py
   ↓
Tool：返回 refund.py 内容
   ↓
Model：我需要修改计算逻辑
   ↓
Tool：返回修改结果
   ↓
Model：我需要运行 pytest
   ↓
Tool：返回失败或成功信息
   ↓
Model：继续修复 / 给出最终回复
```

**这就是我们在第一章引入 Agent Loop 的真正原因。**

但手写循环一旦接入多种工具、审批和状态，就容易越来越复杂。第六章已经解释 LangChain 如何通过 `create_agent()` 将 Model Node、ToolNode、Middleware Hook 和条件路由编译成可执行 Graph。

所以在本章，我们不重写 Agent Loop，而是开始使用 DeepAgents 的 Harness。

---

## 四、第三步：如何从任务描述走到 `create_deep_agent()`？

这时才打开真实源码：

[`deepagents/graph.py`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py)。

### 4.1 先只关心最小入口

```python
from deepagents import create_deep_agent

agent = create_deep_agent(model=model)
```

这与直接调用 LangChain 的 `create_agent(model=model, tools=...)` 有什么不同？

前面第七章已经解释过：`create_deep_agent()` 的主要职责是**装配**，不是亲自执行一条新的模型循环。

因此不要急于寻找 `while True`。直接看 `graph.py` 的函数尾部。

### 4.2 真实源码节选：DeepAgents 交给 LangChain

**位置：** [`graph.py` 第 920—942 行](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py#L920-L942)。

```python
return create_agent(
    model,
    system_prompt=final_system_prompt,
    tools=_tools,
    middleware=deepagent_middleware,
    response_format=response_format,
    context_schema=context_schema,
    checkpointer=checkpointer,
    store=store,
    debug=debug,
    name=name,
    cache=cache,
    state_schema=state_schema if state_schema is not None else DeepAgentState,
).with_config(
    {
        "recursion_limit": 9_999,
        "metadata": {
            "ls_integration": "deepagents",
            "lc_versions": {"deepagents": _lc_version()},
            "lc_agent_name": name,
        },
    }
)
```

这一段已经给出框架边界：

- **调用者**：我们的应用调用 `create_deep_agent()`。
- **输入**：模型、普通工具、Middleware、State Schema、Checkpointer 等配置。
- **本层动作**：组装这些配置，调用 LangChain `create_agent()`。
- **输出**：一个可以 `invoke()`、`stream()` 的编译后 Agent Graph，并附带运行配置。
- **下游**：执行时才由 LangGraph 调度 Agent Loop。

需要特别区分：`recursion_limit=9_999` 是 Graph 的执行步数限制，不是模型“最多修复 9,999 次”的业务保证，也不是成本控制策略。

### 4.3 `create_deep_agent()` 并不默认自动写 Todo

这个版本的 `graph.py` 没有默认安装 `TodoListMiddleware()`。

如果希望当前退款任务拥有 `write_todos`，需要显式传入：

```python
# 框架级示例：需要对应版本依赖
from langchain.agents.middleware import TodoListMiddleware
from deepagents import create_deep_agent

agent = create_deep_agent(
    model=model,
    middleware=[TodoListMiddleware()],
)
```

加入 Todo 后，模型**可以**创建和更新计划，但不保证一定会使用它，也不保证计划中的“已完成”是经过实际测试验证的业务事实。

这是“具有能力”和“已经正确执行”之间的第一条界线。

---

## 五、第四步：模型说“读取文件”，谁把这句话变成真实操作？

假设模型第一次返回一个工具调用：

```python
# 教学示意：AIMessage 中的标准化工具调用，不是模型真实输出
{
    "name": "read_file",
    "args": {"file_path": "/shop/refund.py"},
    "id": "call-read-refund-001",
    "type": "tool_call",
}
```

只看到这条数据，还没有任何文件被读取。

工具调用只是模型产生的**执行请求**。

### 5.1 为什么需要 ToolNode？

如果这里只有一个函数，手工做映射即可：

```python
# 教学化简
if tool_call["name"] == "read_file":
    result = read_file(**tool_call["args"])
```

但实际 Graph 可能有多个工具请求、错误反馈、工具调用 ID、Middleware 包装以及状态更新。

LangChain 使用 ToolNode 负责工具分发，并把执行结果放回消息协议。概念上可以理解为：

```text
AIMessage(tool_calls=[...])
          ↓
       ToolNode
          ↓
按工具名定位可执行工具
          ↓
调用工具及其 Tool Wrapper
          ↓
ToolMessage(tool_call_id=...)
          ↓
下一轮 Model
```

这里 `tool_call_id` 的意义非常具体：**它让模型知道哪一份工具结果对应哪一项请求**。这并不表示该工具结果必然成功；失败结果同样可以通过 `ToolMessage` 返回。

### 5.2 但 `read_file` 工具又从哪里来？

现在转到 DeepAgents：

[`middleware/filesystem.py`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py)。

`FilesystemMiddleware` 会为 Agent 提供文件相关工具。例如：

```text
ls
read_file
glob
grep
write_file
edit_file
delete
```

工具可用性还会受到当前 Middleware 工具过滤、HarnessProfile 和 Backend 能力的影响。特别是 `execute` 不应与上面这些基础文件操作一概而论：是否暴露命令执行工具，取决于所选 Backend 是否满足执行能力要求。

### 5.3 文件路径不应该决定底层存储方式

如果文件工具内部直接写死：

```python
# 教学化简：不适合支持多种存储环境
with open("/shop/refund.py", encoding="utf-8") as f:
    text = f.read()
```

就假设了文件一定存在于当前机器的实际路径中。

但同一个 Agent 可能在不同环境运行：

- 示例环境中，文件存放在 Graph State 里。
- 本地开发环境中，文件来自实际项目目录。
- 隔离执行环境中，文件来自远程 Sandbox。

我们不希望为每一种环境重写 `read_file` 工具。因此需要**Backend 抽象**。

```text
read_file(file_path="/shop/refund.py")
                   ↓
           FilesystemMiddleware
                   ↓
              Backend.read
                   ↓
       当前配置对应的文件存储
```

这条路径解决的是**文件访问方式可替换**，而不是模型智能、自动权限管理或安全隔离。

### 5.4 本章选择哪一种 Backend？

如果只是研究 Graph 中的虚拟文件，默认 `StateBackend` 很适合。

但我们的退款任务明确要求：

```text
修改工作区中的真实 Python 文件
运行 python -m pytest
```

这就产生新的需求：**不仅能读写文件，还必须能够在指定工作区执行命令。**

DeepAgents 的固定版本包含：

```python
from deepagents.backends import LocalShellBackend

backend = LocalShellBackend(root_dir="/path/to/refund-demo")
```

源码见 [`backends/local_shell.py`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/backends/local_shell.py)。这个类继承 `FilesystemBackend`，并实现命令执行能力。

但是：

> **`LocalShellBackend` 不是安全沙箱。** 它直接在主机执行 Shell，文件操作的虚拟路径规则不构成命令的进程隔离或权限边界。真实生产系统应使用专门隔离的执行环境，并审核高风险动作。

换句话说：能够运行 `pytest`，是因为 Backend 具备执行能力，不是因为 `create_deep_agent()` 自动创造了一个隔离容器。

---

## 六、第五步：为什么读完一个文件，模型还会继续读另一个文件？

现在 Agent 已经得到了 `refund.py` 的内容。

里面有：

```python
from shop.coupon_service import payable_amount
```

如果模型只看当前文件，就可能不知道 `payable_amount()` 有没有处理边界条件。

所以它很自然地再发出一个请求：

```python
# 教学示意
{
    "name": "read_file",
    "args": {"file_path": "/shop/coupon_service.py"},
    "id": "call-read-coupon-002",
    "type": "tool_call",
}
```

第二轮并不需要一个名叫“阅读依赖文件”的硬编码工作流节点。

**下一步读什么，是模型根据工具反馈重新判断的。**

这正是 Agent Loop 与固定工作流的区别。

但是，`refund.py`、`coupon_service.py` 和测试日志逐渐变多，又出现了一个问题：每一轮都把全部历史原样塞给模型，可能造成上下文膨胀。

第八章的 SummarizationMiddleware 解决的就是这个问题。重要的是：

- Graph 中保留的 State 与模型本轮**实际看到的有效消息**可以不同。
- Summarization 的目标是整理模型上下文，不是简单清空所有历史状态。
- 文件或大段工具输出可能通过 Backend 另行保留；摘要并不意味着源数据已被删除。

因此从模型调用视角看：

```text
Graph State：较完整的历史与状态
                ↓
      Middleware 构造 ModelRequest
                ↓
模型看到：必要的系统指令、当前任务、摘要与近期工具反馈
```

注意：不是每个任务都会触发摘要；是否触发取决于相应 Middleware 的条件和模型上下文预算。

---

## 七、第六步：发现了 Bug，应该怎样真正修改？

根据第一节的失败测试，我们已经知道独立舍入的问题。

最小的修复并不是把每一笔退款都向下舍入，也不是在第三笔硬编码减去一分钱，而是改变计算对象：

**先确定截至本次的累计应退实付金额，再减去之前已经退过的实付金额。**

### 7.1 为什么累计计算能解决问题？

仍以原金额 `0.30` 元、实付 `0.20` 元为例：

```text
第一次累计原金额 0.10
累计应退实付：round(0.20 × 0.10 / 0.30) = 0.07
本次应退：0.07 - 0.00 = 0.07

第二次累计原金额 0.20
累计应退实付：round(0.20 × 0.20 / 0.30) = 0.13
本次应退：0.13 - 0.07 = 0.06

第三次累计原金额 0.30
累计应退实付：0.20（已经是最后一笔，直接收齐剩余）
本次应退：0.20 - 0.13 = 0.07
```

最终：

```text
0.07 + 0.06 + 0.07 = 0.20
```

### 7.2 实际修复的关键代码

用下面逻辑替换原实现中的独立舍入部分：

```python
# 可复现实验源码：替换 refund_partial() 内计算 refund 的两行

# 根据“累计原金额”计算累计应退实付金额。
cumulative_original = ledger.refunded_original + portion

target_paid = (
    paid
    if cumulative_original == ledger.original
    else cents(paid * cumulative_original / ledger.original)
)

# 本次只退“累计目标 - 历史已退”。
refund = target_paid - ledger.refunded_paid
```

保持原来的状态更新：

```python
ledger.refunded_original += portion
ledger.refunded_paid += refund
return refund
```

这个改变很小，却表达了一个重要业务规则：**退款总额由累计目标约束，而不是多次独立舍入结果的简单相加。**

现在重新运行：

```bash
python -m pytest -q tests/test_refund.py
```

**实际执行结果（本章实验环境）：**

```text
3 passed in 0.03s
```

和修复前的 `1 failed, 2 passed` 相比，这是一条真正可观察的证据链。

但还有两个边界：

1. 本实验只验证这三个回归测试，不证明所有退款业务场景都正确，例如币种、订单行拆分、跨事务并发和优惠券特殊规则。
2. 这里的修改是教学过程中直接写入文件并测试的，不是由实际部署的大模型 Agent 自动提交，因此不能写成“DeepAgents 已成功自动修复”。

### 7.3 在 Agent Loop 中，这一轮会怎样表现？

实际的大模型如果选择修改文件，可能产生类似调用：

```python
# 教学示意：不是本次模型生成的记录
{
    "name": "edit_file",
    "args": {
        "file_path": "/shop/refund.py",
        "old_string": "refund = cents(paid * portion / ledger.original)",
        "new_string": "...累计目标减去历史已退...",
    },
    "id": "call-edit-refund-003",
    "type": "tool_call",
}
```

注意示意中的 `new_string` 不是可执行补丁内容；真实调用需要把完整、正确的代码片段提供给工具。

此时：

```text
Model 产生编辑请求
         ↓
ToolNode 准备分发
         ↓
HITL / 文件权限规则可能拦截
         ↓
FilesystemMiddleware 处理 edit_file
         ↓
Backend 执行对应文件修改
         ↓
ToolMessage 返回修改成功 / 失败信息
         ↓
Model 决定运行测试
```

到这里我们新增了一个自然问题：**模型提出修改，不等于应该立即允许它修改。**

---

## 八、第七步：在修改发生之前，谁能要求人工批准？

我们的退款文件属于资金相关代码，直接执行任何模型补丁都有风险。

最简单的策略是在工具执行边界设置审批：

```python
# 框架级示例：审批配置由 create_deep_agent() 传给 HITL Middleware
agent = create_deep_agent(
    model=model,
    backend=backend,
    interrupt_on={
        "edit_file": {"allowed_decisions": ["approve", "reject"]},
        "execute": {"allowed_decisions": ["approve", "reject"]},
    },
    checkpointer=checkpointer,
)
```

为什么要同时配置 `checkpointer`？

因为审批不是一个普通的 Python `input()`：它可能发生在外部界面、甚至数分钟后。Graph 必须能够保存暂停点，并在得到决定之后继续。

这里形成一条重要的控制链：

```text
AIMessage：申请 edit_file
             ↓
HITL 识别需要审核的 Tool Call
             ↓
Graph 产生 interrupt
             ↓
Checkpointer 保留可恢复状态
             ↓
人工查看补丁及目标路径
         /              \
       批准             拒绝
        |                 |
        v                 v
继续工具执行         返回拒绝反馈
        |                 |
        +--------→ Model 后续决策
```

### 8.1 审批不等于 Backend 安全边界

这一点必须特别强调。

DeepAgents 中的文件权限主要由 FilesystemMiddleware 在**工具层**执行。如果使用 `LocalShellBackend`，模型一旦被允许执行一般 Shell 命令，该命令本身可能不遵守 `edit_file` 的文件权限规则。

因此：

- `edit_file` 审批不能代替对 `execute` 的审核。
- `LocalShellBackend` 的 `root_dir` 不是进程级沙箱。
- 已允许的 Shell 命令仍可能读取秘密或修改工作区以外的内容。
- 对生产级代码应采用隔离执行环境、最小权限和外部审计。

HITL 的价值是让人参与高风险决策，**不是从架构上抹除工具执行风险**。

### 8.2 `Command(resume=...)` 如何继续？

固定版本默认使用批量审批。应用从 Graph 返回的中断信息中取得真实待审操作后，应提交与其一一对应的决定。

单个待审操作的恢复数据通常形如：

```python
# 框架级示意：只有确实拿到相应中断后才可执行
from langgraph.types import Command

agent.invoke(
    Command(resume={"decisions": [{"type": "approve"}]}),
    config={"configurable": {"thread_id": "refund-fix-001"}},
)
```

如果同一批中存在多项待审批操作，就必须根据实际 `action_requests` 提供相应数量、顺序的决定，不能机械复制这个单项示例。

**禁止在接收到中断前无条件自动 `approve`。** 那样虽然走了恢复 API，却失去了人工检查资金代码改动的意义。

---

## 九、第八步：编辑成功以后，怎样证明它真的修好了？

现在 Agent 可能收到：

```text
ToolMessage：文件编辑成功
```

但是编辑成功只说明工具完成了某种文件变更，并不说明行为正确。

我们真正要验证的业务目标是：

```text
相同订单三次部分退款的金额之和
               ==
         订单实际支付金额
```

因此模型应该继续调用：

```python
# 教学示意：实际工具名和参数取决于当前 Backend
{
    "name": "execute",
    "args": {"command": "python -m pytest -q tests/test_refund.py"},
    "id": "call-pytest-004",
    "type": "tool_call",
}
```

这一步与 `read_file` 有本质区别：

- `read_file` 请求从 Backend 读取内容。
- `edit_file` 请求修改 Backend 管理的文件。
- `execute` 请求实际运行命令，只有具备执行协议的 Backend 才能提供。

如果采用前面的 `LocalShellBackend`，命令在工作目录内执行，但依然是宿主系统 Shell 进程，而不是天然安全的容器。

### 9.1 测试失败时，Agent Loop 怎样发挥作用？

假设模型第一次补丁仍然失败：

```text
ToolNode 执行 execute
           ↓
pytest 退出码非 0
           ↓
工具返回错误 / 输出
           ↓
模型看到失败断言
           ↓
模型再次阅读 refund.py
           ↓
模型修改累计尾差处理
           ↓
模型再次请求 pytest
```

这正是最初那个只调用一次模型的简单方案做不到的事。

但注意：工具调用失败后的实际消息格式、命令退出码如何被显示，会依具体 Backend 和 Middleware 的包装实现而不同。**不能把上面的流程图当成真实模型一定执行过的完整 Trace。**

### 9.2 最终回复需要引用“做过的验证”

Agent 的最终答复至少应包含：

```text
修改内容：将每次退款独立舍入改为累计目标差额计算。
验证范围：tests/test_refund.py。
实际结果：3 passed。
尚未覆盖：并发退款、多个订单行、不同币种等场景。
```

其中“3 passed”只有在获取到对应真实工具结果时才有资格写入。

**Graph 正常结束 ≠ 测试通过。模型声称通过 ≠ 工具确实通过。**

---

## 十、一个 Agent 的上下文装不下全部调查时怎么办？

现在再给任务加一个合理变化：

用户不仅要求修改代码，还要求调查现有优惠券规则，例如：

```text
哪些退款场景允许把尾差归集到最后一笔？
是否有其他模块依赖当前舍入顺序？
```

如果主 Agent 把相关的多个模块、测试和日志全部读进自身上下文，既昂贵又容易干扰主要补丁任务。

这时才有必要考虑第十二章的 **Subagents**。

### 10.1 最小委派：让子 Agent 只研究一件事

```text
主 Agent：负责整体修复与最终测试
   |
   +---- task("审查优惠券退款尾差规则和关联测试")
                 |
                 v
            子 Agent Loop
                 |
           阅读相关文件
                 |
           给出简明结论
                 |
                 v
主 Agent 收到 task 的工具结果，继续修复
```

子 Agent 不是一个新工具函数内部的普通文本补全。对于声明式同步子 Agent，DeepAgents 会通过 `task` 工具执行另一个 Agent 图，并将结果送回父 Agent。

### 10.2 真实源码边界

固定版本在 [`graph.py` 第 829—842 行](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py#L829-L842) 中根据同步子 Agent 配置装配 `SubAgentMiddleware`；具体 `task` 工具及其执行逻辑在 [`middleware/subagents.py`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/subagents.py)。

并且默认情况下，若 HarnessProfile 没有禁用且用户没有提供同名替代项，`create_deep_agent()` 还会添加 `general-purpose` 子 Agent。

这不表示模型每次都要委派，也不表示父子 Agent 天然隔离文件系统。

**同步的 `task` 委派是父 Agent 等待子 Agent 得到结果。** 即使内部使用 `ainvoke()`，它也不是后台任务调度服务。真正的后台委派在固定版本中属于另一条 `AsyncSubAgentMiddleware` 路径。

### 10.3 为什么不能把子 Agent 视为强隔离沙箱？

子 Agent 有自己的运行消息上下文和执行循环，但它们可以共享或继承 Backend 配置。

所以“对话上下文隔离”不能推导出“文件副作用隔离”。

如果子 Agent 也拥有修改同一个退款文件的能力，父子 Agent 之间仍可能产生协调问题。对于我们的示例，最容易理解的安排是：

- 子 Agent 仅负责调查规则，报告结论。
- 主 Agent 负责最终代码修改与测试。

它不是框架强制的全局规则，而是当前任务更容易审计的一种应用层设计。

---

## 十一、经历多轮修改与审批，State 究竟保存了什么？

到现在为止，我们已经有很多事件：

```text
HumanMessage：用户要求修复
AIMessage：调用 read_file
ToolMessage：返回 refund.py
AIMessage：调用 read_file
ToolMessage：返回 coupon_service.py
AIMessage：调用 edit_file
审批 interrupt
人工 approve
ToolMessage：修改结果
AIMessage：调用 execute
ToolMessage：pytest 结果
AIMessage：最终回复
```

如果所有信息都依赖一个模型端的消息列表，审批恢复与多个并发节点更新就会比较困难。

因此 LangGraph 需要管理当前的 State、节点提交的更新，以及必要时的 Checkpoint。

### 11.1 真实源码节选：消息使用 DeltaChannel

**位置：** [`graph.py` 第 73—76 行](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py#L73-L76)。

```python
class DeepAgentState(AgentState):
    """AgentState with `DeltaChannel` on messages to reduce checkpoint growth from O(N²) to O(N)."""

    messages: Required[
        Annotated[
            list[AnyMessage],
            DeltaChannel(_messages_delta_reducer, snapshot_frequency=50),
        ]
    ]
```

这里为排版把单行注解换成多行，核心语义不变。

为什么不直接给 `messages` 一个普通列表？

因为在长时间运行的 Agent 中，消息会不断增长；Checkpoint 若重复保存整段完整历史，持久化代价也会增长。`DeltaChannel` 允许按照增量保存与重放的语义管理消息变化。

不过它有一个关键前提：**增量重放要遵守 Reducer 的合并规则。**

也就是说，状态并不是“随便覆盖一个字典字段”。对于消息，固定版本由 `_messages_delta_reducer` 负责按消息协议处理追加、替换或删除等增量。

### 11.2 State、Checkpoint 和 Backend 不是同一个东西

把我们的退款案例拆开会更清楚：

| 对象 | 回答的问题 | 退款任务中的例子 |
| --- | --- | --- |
| Agent State | 当前 Graph 知道什么？ | `messages`、`todos` 等状态 |
| Reducer / Channel | 多项更新怎样合并？ | 新 ToolMessage 如何进入消息历史 |
| Checkpointer | 执行暂停或结束后怎样恢复 Graph 状态？ | 审批中断后使用同一 `thread_id` 继续 |
| Backend | 工具到哪里读取 / 写入文件？ | `shop/refund.py` 所在工作区 |
| Store | 跨执行存储什么长期数据？ | 应用配置的持久知识或其他键值数据 |

尤其不要将 `LocalShellBackend` 修改的磁盘文件，与 Checkpointer 保存的 Graph 状态混为一谈。

Graph 恢复不代表先前的文件变更被自动撤销，也不意味着外部命令的副作用天然具有“精确一次”语义。

### 11.3 具体的重复执行风险

假设：

```text
edit_file 已经修改磁盘文件
         ↓
某处发生进程故障
         ↓
系统尝试恢复或重新调用
```

如果应用不知道该编辑动作是否已经发生，就可能重复执行。对于增量字符串编辑，第二次调用未必还能找到同样的 `old_string`；对于支付或退款接口，重复外部副作用则可能带来更严重后果。

因此：

> **Checkpoint 保障的是执行状态可恢复的一部分能力，外部业务副作用仍需要幂等键、事务约束、审计记录或应用层去重。**

这也是为什么我们选择在资金示例中用测试和明确的 Ledger 业务状态来约束正确性，而不是把一致性完全交给 Agent Runtime。

---

## 十二、最后还缺一件事：用户怎样知道 Agent 已经进行到哪里？

假设一次完整修复用了十二轮模型调用，最终 `invoke()` 可能会返回 State，但用户在执行期间无法直接看到中间进展。

第十四章已经引入 Streaming。放在端到端案例中，最实用的入门方式是：

```python
# 框架级示意；需要配置真正的模型和 Backend
for update in agent.stream(
    {"messages": [{"role": "user", "content": "修复退款尾差，并运行测试"}]},
    config={"configurable": {"thread_id": "refund-fix-001"}},
    stream_mode="updates",
):
    print(update)
```

`updates` 适合观察 Graph 节点提交了什么更新。

但它不是“命令执行百分比”。例如 `pytest` 运行很久，如果执行工具本身没有提供细粒度的主动进度事件，仅靠打开 `updates` 不能推导出“已完成 75%”。

因此应该区分：

```text
updates  —— 哪个节点提交了什么？
messages —— 模型正在生成什么？
tracing  —— 这一轮调用了哪个 Model、Tool、子 Agent？
```

当我们要调查为什么模型声称测试通过却没有执行 `pytest`，Tracing 和消息 / 工具记录的关联通常比最终回复更有价值。

**Streaming 让过程可见，但不会让任务自动正确。**

---

## 十三、现在才把整个架构图拼起来

我们已经逐一遇到并解决了下面这些问题：

1. 单次模型回答不能直接修改文件，所以引入工具和循环。
2. 循环内工具数量与状态增多，所以交给 LangChain / LangGraph 调度。
3. 不同任务需要统一的工程能力，所以 DeepAgents 装配 Harness。
4. 文件可能位于不同环境，所以通过 FilesystemMiddleware 与 Backend 解耦。
5. 多轮工具历史可能太长，所以需要上下文 Middleware。
6. 修改资金代码风险高，所以在工具边界增加 HITL。
7. 复杂调查会干扰主任务，所以可将子任务委派给另一个 Agent Loop。
8. 暂停与多轮更新必须保持状态，所以需要 State、Reducer 和 Checkpoint。
9. 长任务不能只看最终回复，所以需要 Streaming 与 Tracing。

现在完整链路就能从前面的推导中自然长出来：

```text
应用层：用户提出“修复优惠券部分退款 Bug”
                          |
                          v
                  create_deep_agent()
                （初始化 Harness）
                          |
            Model / Backend / Middleware
            State Schema / Checkpointer
                          |
                          v
                  LangChain create_agent()
                    （构建 Agent Graph）
                          |
                          v
                  LangGraph Runtime
                          |
                  before_agent 等 Hook
                          |
                          v
             before_model / Model Wrappers
                          |
                          v
                         Model <-------------------------+
                          |                              |
                    AIMessage.tool_calls ?              |
                     /                \                 |
                   否                  是                |
                   |                    |               |
                   v                    v               |
             after_agent          HITL（可能中断）       |
                   |                    |               |
                   v                    v               |
                  END               ToolNode            |
                                        |               |
                                   Tool Wrappers        |
                                        |               |
                               Filesystem / 其他工具     |
                                        |               |
                                  Backend / task        |
                                        |               |
                                   ToolMessage          |
                                        |               |
                                        +---------------+

横向贯穿：
  State / Reducer / DeltaChannel —— 管理 Graph 的状态更新
  Checkpointer                  —— 保存并恢复 Graph 执行状态
  Streaming / Tracing            —— 对外呈现可观察的执行事实
```

为了避免混淆，这张图有三点简化：

- `after_model` 的图内节点顺序、条件路由和 HITL 的具体接入位置由 LangChain Factory 按 Middleware 配置确定，图中以概念链展示，不对应唯一的 Graph 拓扑。
- `task` 是工具路径上的同步子 Agent 委派；如果没有调用它，该次修复不会额外运行子 Agent。
- State、Checkpoint 和 Streaming 不是放在工具执行之后的串行节点，而是贯穿 Graph 的状态与观察机制。

**重要的最终认识：DeepAgents 的 Harness 不取代 Agent Loop，而是让已有的 Loop 在工程环境中可配置、可扩展、可观察，并在必要时可以暂停和恢复。**

---

## 十四、把所有机制写成一个框架级集成示例

前面各节分散讲了 Model、Backend、Todo 与 HITL。现在可以给出一个完整的装配示例。

它的目标是让读者能够看清这些参数在哪一层传入；**这不是一个在本章环境中已经自动完成退款修复的模型运行记录**。

### 14.1 安装与环境准备

根据已锁定的课程版本，在隔离 Python 环境中安装兼容依赖：

```bash
python -m pip install "deepagents==0.7.32" "langchain==1.4.7" pytest
```

还需要根据选用的模型提供其供应商集成包和合法的 API 配置。具体模型配置请参考 [DeepAgents Models 文档](https://docs.langchain.com/oss/python/deepagents/models)。

本章不把某一家供应商或模型标识写成必须使用的唯一选项。

### 14.2 先明确工作区

假设上述 `refund-demo` 是一次性的本地实验项目，并且当前环境就是为实验准备的隔离开发机。

```python
# run_agent.py
from pathlib import Path

from deepagents import create_deep_agent
from deepagents.backends import LocalShellBackend
from langchain.agents.middleware import TodoListMiddleware
from langgraph.checkpoint.memory import InMemorySaver

# 只允许在受信任的独立实验环境使用 LocalShellBackend。
PROJECT_ROOT = Path(__file__).resolve().parent

backend = LocalShellBackend(
    root_dir=PROJECT_ROOT,
    inherit_env=True,
)

checkpointer = InMemorySaver()

# 需要先初始化一个真实可调用的 ChatModel。
# 例如使用供应商集成创建的 model 实例。
model = ...

agent = create_deep_agent(
    model=model,
    backend=backend,
    middleware=[TodoListMiddleware()],
    interrupt_on={
        "edit_file": {"allowed_decisions": ["approve", "reject"]},
        "execute": {"allowed_decisions": ["approve", "reject"]},
    },
    checkpointer=checkpointer,
    name="refund-coding-agent",
)

config = {
    "configurable": {"thread_id": "refund-fix-001"}
}

request = {
    "messages": [
        {
            "role": "user",
            "content": (
                "检查 shop/refund.py 与 shop/coupon_service.py，"
                "修复优惠券部分退款 0.01 元累计尾差，"
                "补充或执行 tests/test_refund.py，"
                "不要声称未运行的测试已通过。"
            ),
        }
    ]
}

result = agent.invoke(request, config=config)

# 若 result 含有中断，应把实际 action_requests 展示给人审查。
# 审核通过后，再使用相同 thread_id 调用 Command(resume=...)。
print(result)
```

这个示例中 `model = ...` 是**明确的待配置占位符**，读者需要替换为真实模型；如果直接原样执行，不会凭空得到模型服务。

同时，`LocalShellBackend` 以 `inherit_env=True` 创建，只是为了本地实验环境中能找到 `python`、`pytest` 等命令。它也可能让 Shell 进程继承更多环境变量，**绝不能把它当成安全默认值**。实际项目应优先使用隔离环境和明确的执行凭据。

### 14.3 为什么不在这里写“审批后自动继续”一行？

因为我们必须先知道实际收到的中断是什么。

如果模型第一轮只是 `read_file`，根本没有审批请求。此时直接调用 `Command(resume=...)` 并不符合当前执行状态。

如果同一批次有两个待审核动作，必须分别检查两项命令与参数，并提交与它们对应的决定。

这也是为什么本章刻意把“人工审核 UI”留给调用方：**框架提供中断和恢复协议，应用负责把真实待审核内容交给人。**

### 14.4 如果想观察运行进度

在不需要暂停或已有相应中断处理逻辑的情况下，可以进一步用 `agent.stream(..., stream_mode="updates")` 观察节点更新。

如果启用了 HITL，流式消费方同样必须正确处理中断；不能将流结束简单解释为“任务全部完成”。

---

## 十五、如何用真实结果审计一条 Agent 修复记录？

最终用户问：“Agent 修好了吗？”

不要直接判断最后一条 AIMessage 是否自称成功，而应沿证据链倒查。

### 15.1 最小审计表

| 检查问题 | 需要什么证据 | 不能用什么替代 |
| --- | --- | --- |
| 确实阅读了源码吗？ | `read_file` 对应请求和工具结果 | 模型说“我已阅读” |
| 确实改了文件吗？ | `edit_file` / 文件差异及 Backend 状态 | 模型贴出一段建议代码 |
| 修改经过审批吗？ | 中断请求与真实审核决定 | 默认假定工具安全 |
| 真的运行测试了吗？ | `execute` 对应的命令及执行结果 | 模型说“我认为测试会通过” |
| 失败后有继续尝试吗？ | 失败 ToolMessage 之后的新模型 / 工具轮次 | 最终总结中的笼统叙述 |
| 退款金额符合规则吗？ | 真实断言及支付金额约束 | Graph 正常结束 |
| 能恢复执行吗？ | 可读取的 Checkpoint 与同一线程恢复记录 | 只提供 Store 或文件备份 |

### 15.2 一份合理的最终报告

如果依靠本章**实际执行的业务测试**，我们可以写：

```text
问题：多笔部分退款分别独立舍入，导致三次退款合计超出实付。

修复：改用“累计应退实付金额 - 已退实付金额”作为本次退款值；
      最后一笔按剩余实付归集尾差。

回归测试：
- 三次部分退款金额与总额
- 一次性整单退款
- 超额退款被拒绝且 Ledger 不变

修复前：1 failed, 2 passed
修复后：3 passed

未验证：并发退款、持久化事务、跨币种与多订单行规则。
```

但我们**不能**据此写：

```text
DeepAgents 自动读取、编辑、审批并运行完整任务，全部成功。
```

因为本章没有实际调用模型服务执行这一完整 Agent Graph；我们运行的是业务示例的 `pytest`，验证范围必须如实区分。

---

## 十六、回看十五章：我们究竟学会了什么？

现在从第一章到最后一章，可以用同一个问题说明整个系统为何逐步变复杂。

最初，我们只想让模型修复退款 Bug。

**单次回答不够。** 因为它无法在真实项目中操作和验证，所以增加 Tool。

**工具调用一次也不够。** 因为第一次测试可能失败，所以增加 Agent Loop。

**手写循环开始难以扩展。** 因为需要统一消息协议、状态与路由，所以由 LangChain 构建 Graph，并由 LangGraph 执行。

**只会循环仍不够。** 因为真实 Coding Agent 需要文件能力、上下文管理、审批、规划和委派，所以引入 Middleware 与 DeepAgents Harness。

**文件工具不能只依赖一个存储环境。** 所以引入 Backend，并将 `read_file` / `edit_file` 这样的上层工具与不同存储实现解耦。

**资金代码不能不经检查就修改。** 所以引入 HITL，并通过 Checkpointer 支持暂停与恢复。

**长任务会积累状态、历史和日志。** 所以需要 State、Reducer、增量持久化和上下文整理。

**最终结果不足以解释失败过程。** 所以需要 Streaming、Tracing 和 Debugging。

最终可以把整个系列压缩成一个认识：

> **Agent Loop 决定“下一步做什么”，Middleware 决定“调用边界上要增加什么行为”，Backend 决定“操作落到哪里”，LangGraph Runtime 决定“如何调度、更新和恢复”，Harness 则负责将这些机制装配成可使用的系统。**

它们相互配合，但并不相互替代。

### 16.1 必须留下的八条边界

1. `create_deep_agent()` 构建的是 Harness，并不意味着一个独立于 LangChain 的第二套 Agent Loop。
2. `bind_tools` 或模型输出 `tool_calls` 不等于工具已实际执行。
3. `FilesystemMiddleware` 的文件操作权限不是 Shell 进程沙箱。
4. `StateBackend` 中的文件状态与本地磁盘不是同一个存储对象。
5. `TodoListMiddleware` 在 DeepAgents `0.7.32` 中需要显式启用；Todo 完成不等于业务正确。
6. 同步 `task` 子 Agent 不等于后台任务，也不等于文件副作用强隔离。
7. Checkpointer 不保证外部操作精确执行一次。
8. 测试通过需要真实执行证据，不能由模型总结或 Graph 结束状态推断。

---

## 十七、给读者的最终实验：从失败到修复

如果希望把本章压缩成一个最有价值的动手练习，建议按照下面顺序：

**实验 A：只用 Python 复现 Bug。**

建立 `shop/coupon_service.py`、`shop/refund.py` 和 `tests/test_refund.py`，使用本章第二区域的代码，然后运行：

```bash
python -m pytest -q tests/test_refund.py
```

预期且已在本章实验环境验证的结果为：

```text
1 failed, 2 passed
```

**实验 B：只修改一处金额计算。**

用第七节的累计退款目标逻辑替换原来的独立舍入逻辑，再运行相同命令。

已在本章实验环境验证的结果为：

```text
3 passed
```

**实验 C：接入一个真实 Agent Harness。**

配置可调用模型、隔离工作区、Backend、Todo 与 HITL。在真实项目中按以下顺序观察：

```text
read_file 是否发生？
    ↓
模型是否识别逐次舍入问题？
    ↓
edit_file 前是否出现真实审核？
    ↓
文件内容是否真的改变？
    ↓
execute 是否真实运行 pytest？
    ↓
失败后是否继续修复？
    ↓
最终回复能否与工具记录对应？
```

这个实验需要模型服务、相关依赖、审核交互和安全工作区。**本章未运行这个框架级实验，因此不宣称这些观察全部已经通过。**

---

## 附录 A：一条最短源码阅读路线

如果现在重新阅读源码，不必按模块目录从头到尾扫描。建议只追踪一次任务的真实路径：

1. [`deepagents/graph.py`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py)：从 `create_deep_agent()` 到 `create_agent()`，核对 Harness 的装配边界。
2. [LangChain `factory.py` 固定阅读基线](https://github.com/langchain-ai/langchain/blob/fbb8a35/libs/langchain_v1/langchain/agents/factory.py)：寻找 Model Node、ToolNode 与 Middleware 连接方式。
3. [`middleware/filesystem.py`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/filesystem.py)：沿 `read_file` 或 `edit_file` 的工具执行入口追踪 Backend 调用。
4. [`backends/filesystem.py`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/backends/filesystem.py)：看文件路径和本地 IO 怎样处理。
5. [`backends/local_shell.py`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/backends/local_shell.py)：看 `execute()` 的工作目录、进程执行与安全边界。
6. [`middleware/subagents.py`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/middleware/subagents.py)：仅当实际调用 `task` 时再追踪父子 Agent 交接。
7. [`_messages_reducer.py`](https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/_messages_reducer.py)：检查工具反馈怎样合并入消息状态。
8. [LangGraph Streaming 文档](https://docs.langchain.com/oss/python/langgraph/streaming)：用事件流验证你对整条调用链的理解。

每次追踪都问同样六个问题：**谁调用？输入从哪来？它做了什么？产生什么结果或副作用？更新了什么状态？下游由谁消费？**

## 附录 B：收官自测题

1. 本章中 `Decimal` 为什么仍然会出现一分钱尾差？
2. 为什么第一次 `pytest` 失败比模型直接给出“修复完成”更有证据价值？
3. `create_deep_agent()` 返回 Graph 的核心构建工作最后交给了谁？
4. 模型输出 `edit_file` 工具调用与磁盘文件真正改变之间，还有哪些执行环节？
5. 为什么默认 `StateBackend` 不足以支持直接运行本地 `pytest`？
6. `LocalShellBackend(root_dir=...)` 是否构成安全沙箱？为什么？
7. `TodoListMiddleware` 在本系列固定版本中是否默认安装？
8. 主 Agent 调用同步 `task` 时，是不是立即返回一个远程后台任务 ID？
9. 用户批准 `edit_file` 后，是否可以推断后续所有 `execute` 命令都安全？
10. Checkpoint 能否保证同一个退款接口不会被外部重复调用？
11. 最终 State 里有“测试通过”的文字，是否就可以认为 `pytest` 实际通过？
12. 如果一次 Agent 执行失败，你准备通过哪些工具调用记录、消息 ID、状态更新和 Trace 来定位原因？

如果能够用源码入口、真实输入输出和本章可复现实验回答这些问题，就不再只是记住 DeepAgents 的组件名称，而是理解了：**一个简单的 Agent Loop，为什么会被真实任务一步步推动，长成完整的 Agent Harness。**

---

**全系列完。**
