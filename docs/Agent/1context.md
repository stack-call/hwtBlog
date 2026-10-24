---
title: 上下文管理
description: Agent 的上下文窗口里放什么、怎么取舍，以及常见的压缩与记忆策略
tags: [Agent]
sidebar_position: 2
---

# 上下文管理

参考 codex、deepagents、opencode、openhands。

## 第一关：Agent 为什么不能一直保留全部历史？

场景 1 · 最小可行 Agent

你正在为团队开发一个 Coding Agent。

用户提出任务：

> 修复电商项目中优惠券叠加导致订单金额计算错误的问题。需要定位代码、修改实现、运行测试，最后解释修复方案。

你的 Agent 已经能使用三个工具：

```text
read_file(path)
grep(pattern)
execute(command)
```

你先实现了一个最简单的 Agent Loop：

```python
messages = [system_prompt, user_task]

while True:
    response = llm.chat(messages)
    messages.append(response)

    if response.is_final:
        break

    result = execute_tool(response.tool_call)
    messages.append({
        "role": "tool",
        "content": result
    })
```

所有历史消息原样保存，每轮全部发送给 LLM。

最开始，一切正常。

### 问题出现了

Agent 已经执行了 20 多轮工具调用。模型上下文窗口是 32,000 tokens，当前各部分的占用如下：

| 组成 | 占用 | 占窗口 | 用量 |
| --- | ---: | ---: | :--- |
| 系统指令与任务 | 3,000 | 9% | ███ |
| 已有对话与工具记录 | 10,000 | 31% | ██████████ |
| 最新测试日志 | 17,000 | 53% | █████████████████ |
| 下一轮输出预留 | 4,000 | 13% | ██▒▒ |
| **合计** | **34,000** | **106%** | |
| 窗口上限 | 32,000 | 100% | |

（`█` 为预算内的部分，`▒` 为超出上限的部分；1 格 = 1,000 tokens）

（`█` 为预算内的部分，`▒` 为超出上限的部分；1 格 = 1,000 tokens）

前 30,000 tokens 已经把窗口填到只剩 2,000，而下一轮输出至少要留 4,000。也就是说，**模型还没开始回答，预算就已经超了**。

现在 Agent 无法按照原来的方式继续执行。

而且：

- 历史里包含用户的关键要求，不能随便丢。
- 测试日志里包含定位 Bug 的重要报错。
- Agent 已经修改了几个文件，不能忘记之前做过什么。
- 你不能通过换一个更大上下文窗口的模型解决问题。

### 轮到你设计

假设你就是这个 Agent Harness 的架构师，你会怎样修改当前实现，让 Agent 继续完成任务？
