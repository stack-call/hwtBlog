---
id: modern-llm-chapter-05-post-training
title: "第五章：从预训练到 SFT、DPO 与 GRPO：模型怎样从预测文本走向解决问题？"
sidebar_label: "第五章：SFT、DPO 与 GRPO"
sidebar_position: 6
description: "从下一 Token 预测出发，通过同一个代码修复任务，逐步理解 SFT、偏好数据、DPO、RLHF、GRPO 与可验证奖励，并对照 TRL 固定版本源码。"
---

# 第五章：从预训练到 SFT、DPO 与 GRPO：模型怎样从预测文本走向解决问题？

前四章一直在改造模型**处理信息的方式**：MoE 让参数容量与每 Token 计算部分解耦；KV Cache、GQA 和 MLA 减少重复计算与缓存；长上下文架构试图以更低的成本访问历史信息。

但现在，把这样一个模型交给真正的软件开发任务：

> 读取订单退款模块，修复小数精度问题，并说明如何验证修改没有破坏已有功能。

模型即使拥有足够的参数和上下文，也可能只生成一段貌似合理的建议，而不是提交可验证的修复。它可能写出漂亮的解释，忽略实际测试；也可能直接猜一个补丁，却未检查边界条件。

这暴露了一种与模型架构不同的困难：**模型有能力表示和生成文本，不代表训练目标恰好鼓励它完成我们想要的任务。**

本章不再为 Transformer 增加新的网络层，而是沿同一个代码修复任务，逐步改变训练信号。

```text
预训练：观察海量文本，学习预测下一个 Token
    ↓
SFT：给出任务和示范答案，学习怎样回答
    ↓
偏好数据：发现多个答案有好有坏，而非只有标准答案
    ↓
DPO：直接根据“更好 / 更差”调整模型概率
    ↓
在线强化学习：让模型自己生成候选，再根据反馈改进
    ↓
GRPO：对同一问题采样一组答案，使用组内相对奖励更新
```

这些阶段是本章的**教学推导顺序**，并非所有模型都必须照这个顺序训练；例如，DPO 与基于 PPO 的 RLHF 是不同路线，GRPO 也不要求事先做 DPO。

## 一、最初只有一个目标：预测下一个 Token

假设预训练语料包含一段代码和修复说明：

```text
文件 refund.py 存在金额精度问题。
修复方式可以使用 Decimal，并为边界情况添加测试。
```

语言模型看到前缀后，会给词表中的每个候选 Token 一个概率。训练时，真实的下一个 Token 就是监督信号。

给定一段 Token 序列 `x_0, x_1, ..., x_T`，模型的典型训练目标是：

```text
L_pretrain = - Σ_t log P(x_t | x_0, ..., x_(t-1))
```

直觉上，就是让模型给训练语料里实际出现的 Token 分配更高的概率。

为了避免突然进入庞大 Transformer，我们先只研究最后一步：模型已经产生了预测分数 `logits`，怎样计算损失？

### 实验 1：最小的下一 Token 损失

**教学化简：只展示训练目标，不包含 Transformer 前向网络。**

```python
import torch
import torch.nn.functional as F

# batch=1, 序列长度=4, 词表大小=5
torch.manual_seed(7)
logits = torch.randn(1, 4, 5)

# 第 t 个位置的 logits 预测第 t+1 个 Token
input_ids = torch.tensor([[0, 1, 2, 3]])

loss = F.cross_entropy(
    logits[:, :-1].reshape(-1, 5),
    input_ids[:, 1:].reshape(-1),
)
print("loss:", round(loss.item(), 4))
```

本次实际运行输出：

```text
loss: 1.7403
```

这里最容易忽视的是**错一位**：位置 `t` 的预测，用位置 `t+1` 的真实 Token 来监督。

这种训练可以让模型吸收大量语言、代码和世界知识，但它并没有显式区分：

- 代码示例是正确还是有缺陷；
- 修复建议是否通过测试；
- 用户要求的是解释、补丁还是实际验证。

数据本身可能间接包含这些信息，强大的预训练模型也可能学会一些相关能力，但**损失函数不会直接奖励“任务已经真正解决”**。

于是需要一个更明确的训练信号。

## 二、给模型示范：从预训练走向 SFT

我们准备了一条监督数据：

```text
用户：请修复 refund.py 的金额精度问题，并给出验证步骤。

助手：先定位使用浮点数计算金额的位置；
      在金额计算边界统一使用 Decimal；
      然后覆盖部分退款、累计退款和舍入边界执行回归测试。
```

这时，不再只是从任意网页预测下一个词，而是明确给模型“指令”和“理想回答”。这种方法叫 **监督微调（Supervised Fine-Tuning，SFT）**。

注意，SFT 通常**没有换掉下一 Token 预测这个数学目标**。改变的是训练数据如何组织，以及哪些 Token 参与损失。

### 为什么需要掩码？

设想把样本拼成：

```text
[用户指令] [助手示范回答]
```

如果我们真正希望模型学会**给定指令后怎样回答**，就可以只针对助手示范回答计算损失，忽略用户指令部分。

训练数据可以表示成：

```text
input_ids : [用户, 指令, 助手, 正确, 修复]
labels    : [-100, -100, -100, 正确, 修复]
```

`-100` 是 PyTorch 交叉熵默认忽略的标签值。注意：这是**损失掩码**，不是禁止模型在 Attention 中看到用户指令。用户指令依然是预测答案时的上下文。

### 实验 2：损失只落在回答部分

**教学化简：模拟一个已经前向计算完毕的语言模型。**

```python
import torch
import torch.nn.functional as F

torch.manual_seed(0)
logits = torch.randn(1, 5, 8, requires_grad=True)

# 前三个位置是提示部分，最后两个位置是答案部分
labels = torch.tensor([[-100, -100, -100, 4, 5]])

# logits[t] 对齐 labels[t+1]
loss = F.cross_entropy(
    logits[:, :-1].reshape(-1, 8),
    labels[:, 1:].reshape(-1),
    ignore_index=-100,
)
loss.backward()

print("loss:", round(loss.item(), 4))
print("各位置 logits 梯度和:",
      [round(v, 6) for v in logits.grad.abs().sum(dim=-1)[0].tolist()])
```

本次实际运行输出：

```text
loss: 2.4873
各位置 logits 梯度和: [0.0, 0.0, 0.865547, 0.948592, 0.0]
```

观察点：只有用于预测**非忽略标签**的输出位置，会直接产生非零的 logits 梯度。对于真正的 Transformer，早期提示 Token 的嵌入和隐藏状态仍可能通过后续注意力路径接收到梯度，不能把这里的现象理解成“提示永远不会被学习”。

### 从教学代码走到真实源码

固定源码版本：`huggingface/trl` 的 `v0.24.0`。

- 仓库：[TRL v0.24.0](https://github.com/huggingface/trl/tree/v0.24.0)
- 文件：[trl/trainer/sft_trainer.py](https://github.com/huggingface/trl/blob/v0.24.0/trl/trainer/sft_trainer.py)
- 类：`DataCollatorForLanguageModeling`
- 关键方法：`torch_call`

**真实源码节选（保留核心语义）：**

```python
output["labels"][completion_mask == 0] = -100
```

理解这一行的重点不在于 `-100` 本身，而在于整个调用链：

```text
训练数据中的 prompt / completion
    ↓
SFTTrainer 判断数据格式，并准备 completion_mask
    ↓
DataCollatorForLanguageModeling 整理批次
    ↓
把不属于 completion 的位置设为 -100
    ↓
模型进行 teacher forcing 前向
    ↓
交叉熵忽略 -100，只监督选定位置
    ↓
优化器更新参数
```

这个实现还允许不同的数据格式、打包策略与掩码设置；这里研究的是**正常的 prompt-completion 路径**。`SFTTrainer` 在这一版本中对 prompt-completion 数据的默认行为是 completion-only loss，对一般语言建模数据则不是。真实项目必须同时核对数据集格式和配置。

SFT 带来的改变很重要：模型不只看到了“世界上人们如何写代码”，还看到了“面对用户任务，助手应该怎样回应”。

但新问题也出现了：**谁来写每个任务的完美示范？**

## 三、没有唯一标准答案时，能否只说哪个更好？

同一段退款修复任务可能有两个候选回答：

```text
候选 A：
用 Decimal 在金额计算边界统一处理精度，
补充部分退款、累计退款和舍入边界测试。

候选 B：
直接将所有金额计算结果 round(x, 2) 即可，不需要运行测试。
```

我们未必能立刻给出完美的“唯一标准答案”，但通常可以比较这两个回答：在这个任务设定下，候选 A 更谨慎，且提出了验证步骤。

这里多出一种数据形式：

```text
prompt   = 修复退款金额精度问题
chosen   = 候选 A
rejected = 候选 B
```

这就是**偏好数据（Preference Data）**。它只说明，在给定上下文中，`chosen` 比 `rejected` 更符合评价标准；它不保证 chosen 的每一句话都正确，也不保证 rejected 在任何语境都糟糕。

接下来有两条典型路线。

### 路线 A：训练奖励模型，再用强化学习优化

经典的 RLHF（Reinforcement Learning from Human Feedback，人类反馈强化学习）可以分为：

```text
人工比较答案
    ↓
训练 Reward Model：估计哪个回答更受偏好
    ↓
让当前模型生成答案
    ↓
Reward Model 对生成答案打分
    ↓
使用 PPO 等强化学习方法调整生成策略
```

为了让优化后的模型不过度偏离原模型，通常还会加入相对于参考模型的 KL 约束。PPO 类方案往往还涉及价值函数或 Critic，用于估计优势、降低训练方差。

这里要区分两个概念：

- **奖励模型**回答“这个生成结果有多好”；
- **策略模型（Policy）**回答“下一步生成什么 Token”。

这条路线能够通过模型自己采样的输出持续改进，但维护生成模型、参考模型、奖励模型、价值估计等组件，训练复杂度很高。

于是有人提出：既然我们已经有了成对偏好，能不能直接训练策略，而不显式训练奖励模型？

## 四、DPO：直接让模型更偏向好的回答

**直接偏好优化（Direct Preference Optimization，DPO）** 试图把带 KL 约束的偏好学习问题转写成一个直接优化的分类式目标。

先只看直觉：对于同一个 prompt，希望模型对 `chosen` 相对 `rejected` 的偏好增加。

但如果直接最大化 `chosen` 的概率，模型可能偏离原有语言能力。DPO 引入一个**参考模型**，比较训练前后对这两个答案的相对倾向。

对于同一个 `prompt = x`、更优答案 `w` 和较差答案 `l`，记：

```text
logp_policy_w = log P_policy(w | x)
logp_policy_l = log P_policy(l | x)

logp_ref_w    = log P_ref(w | x)
logp_ref_l    = log P_ref(l | x)

policy_gap = logp_policy_w - logp_policy_l
ref_gap    = logp_ref_w    - logp_ref_l

DPO_loss = -log sigmoid( β × (policy_gap - ref_gap) )
```

这里的序列对数概率应针对**回答 Token**求和，在相同 prompt 条件下进行比较。`β` 控制相对于参考模型的偏好调整尺度。实际训练还需要处理 padding、EOS、截断及序列长度偏差等问题。

当训练策略相对于参考模型更偏向 `chosen` 时，`policy_gap - ref_gap` 增大，损失减小。

### 实验 3：不需要奖励模型，也能优化偏好

**教学化简：把两个完整回答的 logit 视为待优化参数。它验证 DPO 损失方向，不是训练真实语言模型。**

```python
import torch
import torch.nn.functional as F

# 模型只在 A（更优）与 B（较差）中选择
policy_logits = torch.nn.Parameter(torch.tensor([0.0, 0.0]))
ref_logits = torch.tensor([0.0, 0.0])
optimizer = torch.optim.SGD([policy_logits], lr=0.8)
beta = 0.5

for step in range(20):
    policy_logp = F.log_softmax(policy_logits, dim=0)
    ref_logp = F.log_softmax(ref_logits, dim=0)

    policy_gap = policy_logp[0] - policy_logp[1]
    ref_gap = ref_logp[0] - ref_logp[1]

    loss = -F.logsigmoid(beta * (policy_gap - ref_gap))
    optimizer.zero_grad()
    loss.backward()
    optimizer.step()

print("P(A), P(B):", F.softmax(policy_logits, dim=0).tolist())
```

本次实际运行输出（四舍五入）：

```text
P(A) ≈ 0.9816
P(B) ≈ 0.0184
```

预期观察：`P(A)` 变大，`P(B)` 变小。

这里没有运行生成、没有奖励模型、没有 PPO；训练信息直接来自已有的 chosen/rejected 对。这是 DPO 相比经典在线 RLHF 流水线更简洁的关键。

### 对照真实项目

固定源码：`huggingface/trl`，`v0.24.0`。

- [trl/trainer/dpo_trainer.py](https://github.com/huggingface/trl/blob/v0.24.0/trl/trainer/dpo_trainer.py)
- 类：`DPOTrainer`
- 输入：带 `prompt`、`chosen`、`rejected` 的偏好样本
- 执行过程：整理两条回答的 Token，计算策略模型和参考模型的对数概率，根据选择的 DPO 损失变体更新策略模型

可以沿下面的方向阅读源码，而不必一次读完整个类：

```text
偏好样本
    ↓
DataCollatorForPreference
    ↓
策略模型计算 chosen/rejected 的对数概率
    ↓
参考模型计算 chosen/rejected 的对数概率
    ↓
计算相对偏好优势与损失
    ↓
反向传播，更新策略参数
```

**概念伪代码（不是项目源码原文）：**

```python
policy_chosen, policy_rejected = get_sequence_logps(policy, batch)
ref_chosen, ref_rejected = get_sequence_logps(reference, batch)

margin = (policy_chosen - policy_rejected) - (ref_chosen - ref_rejected)
loss = -log_sigmoid(beta * margin).mean()
```

注意：这里的 `get_sequence_logps` 和 `log_sigmoid` 是解释用的抽象名称，不是声称 TRL 的真实函数签名。固定版本的实现还提供其他 loss 变体，不能把上面最简公式误认为所有模式。

DPO 解决了“只有比较数据、没有完美示范”时如何继续训练的问题。不过它大多依赖**事先收集好的偏好数据**。如果现在的模型已经超越了旧数据，新的困难就出现了。

**能否让当前模型自己提出候选补丁，再通过验证结果不断改进？**

## 五、从离线偏好转向在线强化学习

回到我们的退款精度任务。我们可以让模型生成多个修复方案：

```text
模型生成方案 A → 补丁能通过测试 → 奖励 1
模型生成方案 B → 补丁不能通过测试 → 奖励 0
模型生成方案 C → 补丁能通过测试 → 奖励 1
模型生成方案 D → 补丁不能通过测试 → 奖励 0
```

为了直观，这里把奖励简化为 `0/1`。真正的软件任务通常还有补丁是否能应用、测试是否真实执行、是否修改了不允许修改的测试、沙箱权限、超时与安全约束等问题。**模型生成的“测试通过”这几个字不等于测试实际通过。**

当反馈可以从程序运行结果计算出来，而不是完全依赖人类打分时，它属于**可验证奖励（Verifiable Reward）**场景，常见于数学答案校验、代码测试或格式规则检查。

```text
当前策略模型
    ↓
对同一个任务采样多个答案
    ↓
执行可信的评分 / 验证
    ↓
将反馈变成训练信号
    ↓
更新策略
    ↓
再次采样
```

这里真正新增的是**在线数据分布**：训练答案来自当前或最近版本的模型，而不是完全固定的历史数据集。

但是，如果我们仅仅对所有得分为 1 的答案增加概率，是否足够？

不完全足够。一个任务可能非常简单，八个答案全部正确；另一个任务可能很难，八个答案只有一个正确。不同任务中的绝对分数具有不同信息量。并且经典策略梯度通常需要基线来降低方差。

这时才需要理解 GRPO。

## 六、GRPO：同一道题内部，比较一组候选答案

**组相对策略优化（Group Relative Policy Optimization，GRPO）** 的核心想法可以先用一句话描述：

> 对同一问题生成一组答案，用组内奖励作为相对基线，而不必额外训练一个价值网络。

假设同一退款任务生成四个补丁，奖励分别为：

```text
rewards = [1, 0, 1, 0]
mean    = 0.5
std     = 0.5

advantages = [(r - mean) / std for r in rewards]
           = [1, -1, 1, -1]
```

这组 `advantages`（优势值）告诉我们：在这次采样中，第一、三个答案高于组内平均水平；第二、四个答案低于平均水平。

另一种情况：

```text
rewards = [1, 1, 1, 1]
```

所有答案都一样好时，组内差异消失，这组答案不能提供“谁相对更好”的学习信号。实现中需要处理标准差为零的情况；如果所有奖励完全相同，一种常见处理是给出零优势（具体行为依实现和配置而定）。

### 实验 4：先实现组内相对优势

**教学化简：只实现优势归一化，不包含策略更新。**

```python
import torch


def group_advantages(rewards):
    rewards = torch.as_tensor(rewards, dtype=torch.float32)
    centered = rewards - rewards.mean()
    std = rewards.std(unbiased=False)
    if std.item() == 0:
        return torch.zeros_like(rewards)
    return centered / std


print(group_advantages([1, 0, 1, 0]).tolist())
print(group_advantages([1, 1, 1, 1]).tolist())
```

本次实际运行输出：

```text
[1.0, -1.0, 1.0, -1.0]
[0.0, 0.0, 0.0, 0.0]
```

注意，`std(unbiased=False)` 是本节为了说明概念采用的总体标准差；不同训练库、不同版本的归一化配置可能采用不同约定。实际 GRPO 还可以选择是否除以标准差，不能只凭这四行推断所有框架。

### 从优势值走向策略更新

得到优势之后，还不能直接把它当作梯度。我们需要知道当前策略给已采样 Token 分配了多大概率，以及这些 Token 被旧策略采样时的概率。

核心的 PPO 风格目标可以先写成：

```text
ratio_t = exp(logp_current_t - logp_old_t)

unclipped_t = ratio_t × advantage
clipped_t   = clip(ratio_t, 1 - eps, 1 + eps) × advantage

policy_objective_t = min(unclipped_t, clipped_t)
loss = -mean(policy_objective_t)
```

其中：

- `logp_current_t` 是当前策略对**已经采样的**这个 Token 的对数概率；
- `logp_old_t` 是采样时的旧策略对这个 Token 的对数概率；
- `ratio_t` 衡量概率相对变化；
- `clip` 限制单次更新对概率比的推动，减少更新过猛的风险。

实践中的 GRPO 不只这么几行：还涉及回答 Token 掩码、序列长度归一化、采样与更新的版本差异，以及可能存在的参考模型 KL 正则项。KL 权重并不在所有版本中都默认为非零。

### 实验 5：一个无需语言模型的在线 GRPO 小实验

我们让一个极小的策略在四个“修复候选”中选一个；候选 `0`、`2` 被人为标成可通过测试。它只是**模拟奖励的四臂 Bandit 实验**，用来验证在线采样、组内优势和策略更新，不是大语言模型训练，也没有真的执行 pytest。

**教学化简：每一轮都重新采样，做一次 PPO 风格的裁剪更新。**

```python
import torch
import torch.nn.functional as F

torch.manual_seed(42)

# 四个候选动作，0 和 2 在玩具任务中得到奖励 1
policy_logits = torch.nn.Parameter(torch.zeros(4))
optimizer = torch.optim.Adam([policy_logits], lr=0.08)
rewards_table = torch.tensor([1.0, 0.0, 1.0, 0.0])


def advantage_of(rewards):
    centered = rewards - rewards.mean()
    std = rewards.std(unbiased=False)
    if std.item() == 0:
        return torch.zeros_like(rewards)
    return centered / std


for step in range(300):
    # 在更新前，旧策略负责采样
    with torch.no_grad():
        old_logp = F.log_softmax(policy_logits, dim=0)
        actions = torch.distributions.Categorical(logits=policy_logits).sample((16,))
        sampled_rewards = rewards_table[actions]
        advantages = advantage_of(sampled_rewards)
        sampled_old_logp = old_logp[actions]

    # 当前策略重新计算这些相同动作的概率
    new_logp = F.log_softmax(policy_logits, dim=0)[actions]
    ratio = torch.exp(new_logp - sampled_old_logp)
    unclipped = ratio * advantages
    clipped = ratio.clamp(0.8, 1.2) * advantages
    loss = -torch.minimum(unclipped, clipped).mean()

    optimizer.zero_grad()
    loss.backward()
    optimizer.step()

print("最终动作概率:", [round(x, 3) for x in F.softmax(policy_logits, dim=0).tolist()])
```

本次实际运行输出：

```text
最终动作概率: [0.28, 0.001, 0.719, 0.001]
```

两个有奖励的动作（0 和 2）占据绝大多数概率，但具体分配取决于随机采样。这不是“哪个补丁更好”的结论，因为在这个玩具任务里两个成功动作的奖励相同。

这段代码的作用是观察：经过多轮在线采样和奖励反馈，策略是否更倾向于获得奖励的动作。

它特意没有包括真实 GRPO 的 Token 级序列损失、参考模型 KL、分布式生成、复杂的 reward 函数以及多次复用同一批样本的设置。只有一步更新时，`ratio` 的数值起点为 1，但它对当前参数**仍然有梯度**；这正是此处能够学习的原因。

### 真正的 GRPO 源码对应到哪里？

固定版本 `huggingface/trl` 的 `v0.24.0`：

- [trl/trainer/grpo_trainer.py](https://github.com/huggingface/trl/blob/v0.24.0/trl/trainer/grpo_trainer.py)
- 类：`GRPOTrainer`
- 与采样有关的对象：`RepeatSampler`
- 核心配置：`num_generations`、`reward_funcs`、`beta`、`epsilon` 等

**真实源码节选（采样器参数中的关键一行）：**

```python
mini_repeat_count=self.num_generations,
```

为什么要重复一条 prompt？因为 GRPO 必须比较**同一任务下的一组回答**。对于我们的退款任务，不能拿不同项目上产生的两个补丁直接当作一组进行相对奖励标准化。

阅读真实运行路径时，可以沿着这个顺序追踪：

```text
包含 prompt 的训练数据
    ↓
RepeatSampler 为同一 prompt 准备多次生成机会
    ↓
当前 / 旧策略采样多个 completion
    ↓
reward_funcs 或 Reward Model 对 completion 打分
    ↓
每个 prompt 的一组 reward 计算相对优势
    ↓
模型重新计算已采样 completion 的 Token 概率
    ↓
用裁剪目标与可选正则计算 loss
    ↓
backward + optimizer.step 更新策略
```

源码中的细节比这条主线更复杂，包括生成与训练批次如何复用、不同损失类型、计算分布、奖励标准化方式和高性能生成后端。第一遍精读时，先确认**哪些数据由模型生成、哪些由验证器提供、哪些只是优化器内部的数值**。

## 七、GRPO 为什么在推理模型里变得重要？

到现在，我们已经把学习问题从“模仿标准答案”转变成“根据生成结果的好坏更新策略”。

对于数学或代码问题，比较理想的训练场景是：

```text
问题：一个确定的数学题 / 一组可执行的代码测试
    ↓
模型尝试多条解题路径
    ↓
验证器检查最终答案 / 执行真实测试
    ↓
正确答案得到较高奖励
    ↓
模型提高产生成功轨迹的倾向
```

如果训练任务要求多步推导才能拿到正确结果，强化学习可能在一定条件下推动模型学习更长的中间计算、验证和修正行为。这是现代推理模型的重要发展方向。

但必须把下面几个说法分开：

1. **奖励最终答案正确**不等于证明每一步中间推理都真实可靠。
2. **生成更长的推理文本**不必然意味着答案更正确，也可能浪费计算或产生冗余。
3. **能通过公开测试**不保证模型没有利用奖励漏洞，也不保证泛化到未见任务。
4. **RL 改善可验证任务表现**不意味着可以不做预训练或不用高质量数据。

DeepSeekMath 提出了 GRPO；DeepSeek-R1 的公开报告进一步展示了将强化学习用于推理能力训练的路线，并指出纯强化学习方案的可读性、语言混用等问题，因而结合冷启动数据和后续训练阶段。这里我们只讨论训练目标怎样变化，不提前深入下一章的推理时计算扩展。

## 八、现在可以准确区分这些名词了

| 方法 | 需要的数据 | 核心动作 | 典型限制 |
| --- | --- | --- | --- |
| 预训练 | 大规模 Token 序列 | 预测下一个 Token | 目标不直接等价于完成用户任务 |
| SFT | prompt 与示范 completion | 模仿给定答案 | 高质量示范昂贵，容易继承数据局限 |
| 奖励建模 | 偏好比较或评分 | 学习一个评分函数 | 奖励模型可能有偏差，可能被策略利用 |
| PPO 风格 RLHF | 模型采样、奖励反馈等 | 在线更新生成策略 | 训练链条复杂，常需额外价值估计 |
| DPO | chosen / rejected 偏好对 | 直接优化相对偏好 | 多为离线数据，依赖比较数据质量 |
| GRPO | 多个生成结果与奖励 | 使用组内相对优势更新策略 | 需要采样和可靠奖励，同组全同奖励时信号弱 |

这里的 DPO 与 GRPO **不是简单的上下级关系**：DPO 主要学习给定偏好数据；GRPO 侧重用当前策略采样和奖励来更新，两者可以选择性组合，也可以单独使用。

### 最容易混淆的四条边界

**第一，训练能力与推理能力不是一回事。** SFT、DPO 和 GRPO 描述怎样更新参数；“推理模型”描述模型在复杂任务中的行为与能力表现。后者还受到数据、模型规模、测试时计算等因素影响。

**第二，RLHF 不等于 PPO。** RLHF 是利用人类反馈改进模型的广义训练路线；PPO 是可用于这一流程的一种强化学习优化算法。DPO 也是处理偏好反馈的一条不同优化路径。

**第三，RLVR 不等于 GRPO。** RLVR 指利用可验证奖励开展强化学习的场景，GRPO 是一种具体的策略优化方法。可验证奖励也可以配合其他优化器。

**第四，任务成功不能只由模型自己宣称。** 在我们的代码修复任务里，真实的验证必须发生在模型文本生成之外：应用补丁、执行测试、检查退出码与日志，才能构造有意义的奖励。

## 九、回头看：训练目标是怎样一步步长出来的？

这章始终围绕同一项任务：“修复退款精度问题，并验证结果”。

```text
预训练
  学会代码、语言、错误信息中常见的模式
      ↓
  不保证按用户要求给出完整修复流程
      ↓
SFT
  学会面对指令模仿正确的修复说明
      ↓
  每种任务都准备完美示范太昂贵
      ↓
偏好数据 + DPO
  从两个候选中学习哪一个更符合目标
      ↓
  静态偏好数据难以覆盖模型新生成的方案
      ↓
在线奖励与强化学习
  自己生成补丁，使用验证结果反馈
      ↓
GRPO
  比较同一问题中的多个补丁，用相对优势优化策略
```

最后得到一个重要认识：**现代大模型的进步，不只是让 Transformer 算得更多或存得更少，也包括把训练信号从“文字长什么样”，逐步推进到“怎样的行为更符合任务目标”。**

这一章回答的是训练阶段怎样改变模型。下一章还留下一个新问题：**即使模型参数已经训练好了，为什么让它在回答之前进行更多有效计算，也可能提升复杂问题的成功率？**

接下来我们将从一次失败的复杂代码修复出发，研究推理时计算（Test-Time Scaling）、多次采样、验证与搜索：它们究竟解决了什么，又为什么不能简单等同于“让模型多写几句思考过程”。

---

## 源码、论文与版本说明

以下链接均可直接访问，不需要在 Docusaurus 内配置特殊引用插件：

- [TRL v0.24.0：SFTTrainer 源码](https://github.com/huggingface/trl/blob/v0.24.0/trl/trainer/sft_trainer.py)：`DataCollatorForLanguageModeling` 如何构造仅监督回答部分的标签。
- [TRL v0.24.0：DPOTrainer 源码](https://github.com/huggingface/trl/blob/v0.24.0/trl/trainer/dpo_trainer.py)：偏好数据的整理、参考模型与优化器。
- [TRL v0.24.0：GRPOTrainer 源码](https://github.com/huggingface/trl/blob/v0.24.0/trl/trainer/grpo_trainer.py)：重复采样、奖励、组相对优势与策略优化。
- [Training language models to follow instructions with human feedback](https://arxiv.org/abs/2203.02155)：InstructGPT / RLHF 的经典训练链路。
- [Direct Preference Optimization](https://arxiv.org/abs/2305.18290)：DPO 的偏好优化理论。
- [DeepSeekMath: Pushing the Limits of Mathematical Reasoning](https://arxiv.org/abs/2402.03300)：GRPO 的提出。
- [DeepSeek-R1: Incentivizing Reasoning Capability in LLMs via Reinforcement Learning](https://arxiv.org/abs/2501.12948)：推理训练与冷启动、强化学习阶段。

> [!NOTE]
> 本章的实验 1 至实验 5 均使用本地 PyTorch CPU 环境实际运行并记录输出；它们是教学化简。实验 5 的奖励来自固定表格，不能证明真实代码修复能力。真实源码节选均对应固定 TRL `v0.24.0`；其他示意代码会明确标注。尚未运行 Docusaurus 构建。
