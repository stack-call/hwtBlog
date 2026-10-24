---
title: 第十五章：Human-in-the-Loop——Agent 如何暂停和恢复？
sidebar_label: 第十五章：Human-in-the-Loop
description: 从退款审批推导持久化中断、审批绑定、恢复与副作用重放
sidebar_position: 16
tags: [Agent, Harness, Human-in-the-Loop, LangGraph]
---

# 第十五章：Human-in-the-Loop——用户不在线时，Agent 怎么等？

## 事故：审批弹窗一出现，进程就不能停

优惠券 Agent 已经知道生产退款操作需要审批。但第一次实现只是：

```python
# 教学反例：等待绑定在同一个进程里
approved = await ui.wait_for_click("是否批准退款？")
```

问题随即发生：用户离开电脑，前端断线；服务为了升级重启。这次 `await` 的协程状态消失了。审批请求也找不到了。

小李发现，**等待用户不是一次慢速函数调用，而是一个可能跨进程、跨设备、跨天的运行时状态。**

## 第一层改进：把“等待审批”变成显式中断

Harness 在执行高风险工具之前，先保存：

```text
thread_id: coupon-refund-01
interrupt_id: approval-17
status: waiting_for_approval
requested_tool: refund_payment
arguments_hash: sha256(...)
requested_at: ...
```

再向 UI 发送 `approval_requested` 事件；此后可以释放进程资源。用户点击批准时，客户端提交 `interrupt_id` 与决定，服务检查原请求是否仍有效，并恢复原 Thread。

这样“用户是否在线”就从进程内存状态变成了一个持久化工作流问题。

## 第二层故障：恢复时重复执行了前半个节点

小李把“查询退款 + 等审批 + 执行退款”写在同一个节点里。恢复后，他发现查询又执行了一次。

这不是偶然。以 LangGraph 为例，恢复中断时会从**被中断节点的开头重新执行**。因此中断之前的外部副作用可能被重复执行。

一个更好的结构是：

```text
prepare_refund  →  approval_interrupt  →  commit_refund
     只读/准备        等人决策           幂等写入
```

不能依赖“恢复时从 Python 函数 `interrupt()` 的下一行继续”。在不可重复的副作用之前，应设置安全边界或独立节点。

## 真实源码：LangGraph 的 `interrupt` 与 `Command`

官方 [Interrupts 文档](https://docs.langchain.com/oss/python/langgraph/interrupts) 与 [源码类型定义](https://github.com/langchain-ai/langgraph/blob/main/libs/langgraph/langgraph/types.py) 说明了中断与恢复的契约。

```python
# 实际 API 的简化使用片段；省略 StateGraph 构建
from langgraph.types import interrupt, Command

def approval_node(state):
    decision = interrupt({"action": state["proposed_action"]})
    return {"approved": decision == "approve"}

# 第一次 graph.invoke(...) 暂停；之后用同一个 thread_id：
graph.invoke(Command(resume="approve"), config=config)
```

要点是：使用持久化 Checkpointer、保持同一个 `thread_id`、客户端知道中断请求、恢复值经过校验。文档还明确说明恢复后会从节点开头重新运行。

### 与 Permission 模块的区别

上一章的 Permission 负责**判定需要谁批准**；本章 HITL 负责**如何暂停、保存等待、收到决定后恢复**。

也可以用相同的中断机制做“请求更多信息”“让人修改模型参数”“让审核员选择测试分支”。因此 HITL 并不局限于危险操作。

## 第三个故障：用户批准之后，环境已经变了

审批请求显示“退款 80 元”，但等待两天后，订单已经被人工退款。直接执行旧操作仍然危险。

恢复时还必须重新核对：资源版本、请求有效期、操作身份、幂等键和权限。**审批同意的是某个具体版本的动作，不是永久授权 Agent 在未来做相似事情。**

| 五个问题 | 回答 |
|---|---|
| 是什么 | HITL 是把外部人工决定纳入可持久化的 Agent 控制流。 |
| 为什么需要 | 审批可能持续很久，Agent 进程不能一直保持活动。 |
| 没有它 | 掉线丢失等待状态，恢复后容易重复执行或误用旧审批。 |
| 实际怎么做 | Interrupt + Checkpoint + 审批 ID + Resume + 幂等提交。 |
| 处于哪里 | Agent Runtime 的流程控制层，与 Policy、Session、Checkpoint 协作。 |

### 实践验证

让 Agent 生成一个模拟退款请求并进入等待状态，关闭程序后重新打开，再批准它。测试在这段时间订单发生版本变化、审批重复提交、被拒绝、超时等情况。确保无一次意外退款。

**下一章的问题**：Harness 准备好了这些机制，换一个模型供应商时为什么仍然可能不能运行？
