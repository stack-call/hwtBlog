---
id: modern-llm-chapter-01-moe
title: 第二章：从稠密 FFN 到 MoE，为什么参数变多却不必全部计算？
sidebar_label: 第二章：MoE 与稀疏计算
sidebar_position: 3
description: 从最简单的 SwiGLU FFN 出发，逐步实现专家路由、Top-K、稀疏执行和负载均衡，再对照 Mixtral 与 DeepSeek-V3 的真实设计。
---

# 第二章：从稠密 FFN 到 MoE，为什么参数变多却不必全部计算？

上一章用 `I love you` → `我爱你` 这个翻译任务，把 Transformer 的一次前向计算拆开看了一遍。那是一个结构清晰、长度可控的短序列任务，适合解释每个组件各自负责什么。基础架构到这里已经交代清楚，从这一章开始，我们换成一个更接近真实使用的任务，看真实大模型在规模化之后会撞上什么问题。

一个现代语言模型，可能拥有数千亿参数，但在处理一个 Token 时，只激活其中一小部分。这听起来有些矛盾：既然训练了那么多参数，为什么不把它们全部用上？

要理解这件事，我们不妨从一个很具体的任务开始。

假设你让模型阅读一个代码仓库，回答：

> `refund.py` 的优惠券退款逻辑为什么会导致 `pytest` 失败？请定位原因并给出修改。

模型需要处理 Python 语法、测试输出、业务规则，也可能需要理解自然语言中的需求描述。这些信息最终都会被转换成 Token 的隐藏向量，进入一层又一层 Transformer。

我们先只看 Transformer Block 中的一小部分：**FFN（Feed-Forward Network）**。注意力负责让不同位置的 Token 交换信息，FFN 则在每个 Token 的当前位置上进行非线性变换。接下来出现的全部设计，都从这个简单模块长出来。

## 1. 先从一个普通 FFN 开始

为了贴近现代大模型，我们使用常见的 SwiGLU 形式，而不是最早 Transformer 中的 ReLU FFN。

```python
import torch
from torch import nn
from torch.nn import functional as F


class SwiGLUFFN(nn.Module):
    def __init__(self, hidden_size: int, intermediate_size: int):
        super().__init__()
        self.gate_proj = nn.Linear(hidden_size, intermediate_size, bias=False)
        self.up_proj = nn.Linear(hidden_size, intermediate_size, bias=False)
        self.down_proj = nn.Linear(intermediate_size, hidden_size, bias=False)

    def forward(self, x):
        return self.down_proj(F.silu(self.gate_proj(x)) * self.up_proj(x))


ffn = SwiGLUFFN(hidden_size=8, intermediate_size=16)
x = torch.randn(1, 5, 8)  # batch=1，5 个 token，隐藏维度=8
y = ffn(x)
assert y.shape == x.shape
```

输入有 5 个 Token，每个 Token 都会经过**同一组** FFN 参数：

```text
Token 0 ─┐
Token 1 ─┤
Token 2 ─┼──→ 同一个 SwiGLU FFN ──→ 各自的输出
Token 3 ─┤
Token 4 ─┘
```

这里“同一个”的含义是**共享权重**，不是把 Token 混在一起计算。FFN 并不会在这一步直接读取其他位置的 Token。它处理的是每个位置已经包含上下文信息的隐藏向量。

如果隐藏维度是 `d`，中间维度是 `m`，忽略偏置，三个线性层的参数量就是：

```text
P_FFN = d·m + d·m + m·d = 3dm
```

对上面的例子来说，参数量为 `3 × 8 × 16 = 384`。

这很简单，而且足够有效。那么为什么还要改变它？

## 2. 最直接的扩容办法：把 FFN 做宽

为了提升模型容量，我们可以把中间维度从 16 扩展到 64：

```python
small = SwiGLUFFN(8, 16)
large = SwiGLUFFN(8, 64)

print(sum(p.numel() for p in small.parameters()))  # 384
print(sum(p.numel() for p in large.parameters()))  # 1536
```

参数增加了 4 倍，FFN 中主要矩阵乘法的计算量也大致增加了 4 倍。更重要的是，**每个 Token 都要执行这个更宽的 FFN**。

无论当前隐藏向量主要编码的是 Python 代码、退款金额，还是报错日志，都会走完整个宽 FFN。我们当然可以继续扩大网络，但代价越来越明显。

真正想要的效果是：

> 让模型拥有很多可学习的参数和计算路径，但每个 Token 只使用其中少数几条。

注意，这不是说“少量参数一定能实现相同效果”，而是希望**将总参数容量与每个 Token 的计算成本部分解耦**。

## 3. 第一次尝试：做多个 FFN

既然一个 FFN 必须对所有 Token 做相同的处理，我们可以先建立多个不同的 FFN。

```text
                    ┌→ Expert 0 (FFN) ─┐
Token 的隐藏向量 ────┼→ Expert 1 (FFN) ─┼──→ 合并结果
                    ├→ Expert 2 (FFN) ─┤
                    └→ Expert 3 (FFN) ─┘
```

**Expert（专家）**在这里并不神秘：它通常就是一组独立的 FFN 参数。不同专家可以在训练中学习不同的变换。

但如果每个 Token 仍然经过所有专家，再把结果相加，就没有节省计算：一个 Token 需要执行 4 次 FFN。模型容量增加了，运算也跟着增加。

所以只“拥有多个专家”还不够。我们还缺一个机制：**为每个 Token 选择少数专家，只运行被选中的那些。**

## 4. 引入 Router：谁决定调用哪个专家？

最简单的办法，是给每个 Token 的隐藏向量 `x_t ∈ R^d` 乘一个小矩阵：

```text
z_t = W_r · x_t,    W_r ∈ R^(N × d)
```

这里 `N` 是专家数量。计算出来的 `z_t` 有 `N` 个分数，分别对应每个专家。

```python
router = nn.Linear(8, 4, bias=False)
x = torch.randn(5, 8)
logits = router(x)                 # [5, 4]：5 个 Token、4 个专家
probs = torch.softmax(logits, -1) # [5, 4]
```

这层很小的线性变换叫 **Router（路由器）**；它计算出来的分数，也常被称为 **Gate（门控）** 的输出。

例如，某个 Token 经过 Router 后得到以下分布（仅为说明而人为给定）：

```text
Expert 0 : 0.10
Expert 1 : 0.60
Expert 2 : 0.25
Expert 3 : 0.05
```

我们不必让它使用所有专家。只选择最高的两个，即 **Top-K 路由**，这里 `K = 2`：

```text
Token t
  │
  └── Router
       ├── Expert 1 : 0.60 ──┐
       └── Expert 2 : 0.25 ──┴──→ 加权合并

其余 Expert 不执行这个 Token 的 FFN 计算
```

如果采用 Mixtral 风格的归一化，被选中专家的权重会再除以它们的权重之和，得到归一化权重 `w`：

```text
w_1 = 0.60 / (0.60 + 0.25)
w_2 = 0.25 / (0.60 + 0.25)
```

因此，这个 Token 的输出是：

```text
y_t = w_1 · E_1(x_t) + w_2 · E_2(x_t)
```

把它推广到任意 `N` 个专家和 `Top-K`，可以写作：

```text
S_t = TopK(softmax(W_r · x_t), K)
y_t = Σ_{i ∈ S_t} w_{t,i} · E_i(x_t)
```

这就是一种最基本的 **Sparse Mixture of Experts（稀疏混合专家，MoE）**。稀疏的意思是：虽然存在很多专家，但一次只执行选中的部分专家。

路由是**逐 Token、逐 MoE 层**计算的。不同 Token 可以选择相同的专家；同一个 Token 到下一层时，也可能选择另外一组专家。Router 并不是先把一句话判定为“数学任务”或“编程任务”，再固定给某个专家；它根据每一层的隐藏表示动态产生分数。

## 5. 从公式到代码：实现一个真正只计算 Top-K 的 MoE

下面是本章的最小可运行实验。只依赖 PyTorch，不需要下载预训练模型，也不需要 API Key。

我们要验证三件事：

1. 每个 Token 只选择 `K` 个专家。
2. 没选中的专家**不会参与这个 Token 的 FFN 前向计算**。
3. 稀疏执行与“计算全部专家后仅取 Top-K 结果”的参考实现，产生相同输出。

**代码性质：教学化简，可独立运行；不是从某个模型仓库直接复制的源码。**

```python
import torch
from torch import nn
from torch.nn import functional as F


class Expert(nn.Module):
    def __init__(self, d_model, d_ff):
        super().__init__()
        self.gate = nn.Linear(d_model, d_ff, bias=False)
        self.up = nn.Linear(d_model, d_ff, bias=False)
        self.down = nn.Linear(d_ff, d_model, bias=False)

    def forward(self, x):
        return self.down(F.silu(self.gate(x)) * self.up(x))


class TinyMoE(nn.Module):
    def __init__(self, d_model=8, d_ff=16, num_experts=4, top_k=2):
        super().__init__()
        assert 1 <= top_k <= num_experts
        self.num_experts = num_experts
        self.top_k = top_k
        self.router = nn.Linear(d_model, num_experts, bias=False)
        self.experts = nn.ModuleList(
            [Expert(d_model, d_ff) for _ in range(num_experts)]
        )

    def forward(self, x):
        batch, seq_len, d_model = x.shape
        flat = x.reshape(-1, d_model)             # [T, d]

        logits = self.router(flat)                # [T, N]
        probs = torch.softmax(logits.float(), -1)
        weights, expert_ids = torch.topk(probs, self.top_k, dim=-1)
        weights = weights / weights.sum(-1, keepdim=True)
        weights = weights.to(dtype=flat.dtype)

        output = torch.zeros_like(flat)

        # 按专家收集 Token：一个专家仅处理被分配给它的 Token
        for expert_id, expert in enumerate(self.experts):
            token_ids, slots = torch.where(expert_ids == expert_id)
            if token_ids.numel() == 0:
                continue

            expert_input = flat.index_select(0, token_ids)
            expert_output = expert(expert_input)
            weighted = expert_output * weights[token_ids, slots].unsqueeze(-1)

            # 一个 Token 可能选择多个专家，所以把贡献累加回同一位置
            output.index_add_(0, token_ids, weighted)

        return (
            output.reshape(batch, seq_len, d_model),
            expert_ids.reshape(batch, seq_len, self.top_k),
            weights.reshape(batch, seq_len, self.top_k),
        )


torch.manual_seed(42)
model = TinyMoE()
x = torch.randn(1, 5, 8)
output, expert_ids, weights = model(x)

print("输出形状:", tuple(output.shape))
print("每个 Token 选中的专家:", expert_ids.squeeze(0).tolist())
print("各专家被选中的次数:",
      torch.bincount(expert_ids.reshape(-1), minlength=4).tolist())

assert output.shape == (1, 5, 8)
assert expert_ids.shape == (1, 5, 2)
assert torch.allclose(weights.sum(dim=-1), torch.ones(1, 5))
assert torch.bincount(expert_ids.reshape(-1), minlength=4).sum().item() == 10

# 参考实现：故意计算全部专家，再只挑选 Top-K 结果
# 这是用来验证数值，不是高效推理写法。
flat = x.reshape(-1, 8)
all_expert_outputs = torch.stack([e(flat) for e in model.experts], dim=1)
selected = torch.gather(
    all_expert_outputs,
    dim=1,
    index=expert_ids.reshape(-1, 2).unsqueeze(-1).expand(-1, -1, 8),
)
reference = (selected * weights.reshape(-1, 2, 1)).sum(dim=1).reshape(1, 5, 8)
assert torch.allclose(output, reference, atol=1e-6)
print("稀疏执行与全量计算参考结果一致")
```

这里最关键的并不是公式，而是这两行：

```python
token_ids, slots = torch.where(expert_ids == expert_id)
expert_input = flat.index_select(0, token_ids)
```

前一行查找哪些 Token 选择了当前专家；后一行只把这些 Token 交给专家。随后通过 `index_add_` 把多个专家的加权结果累加回原来的 Token 位置。

输入和输出仍然是相同形状：

```text
输入隐藏状态 [batch, seq, d]
        ↓
      Router
        ↓
      Top-K
        ↓
把 Token 分派给选中的 Expert
        ↓
 Expert 对各自收到的 Token 计算 FFN
        ↓
按路由权重合并，放回原 Token 位置
        ↓
输出隐藏状态 [batch, seq, d]
```

**验证边界：** 这个实验能验证路由、稀疏执行和数值等价；不能证明专家已经学会不同技能，也不能证明这个 Python `for` 循环在 GPU 上一定比稠密矩阵乘法更快。工程性能需要专门的稀疏内核、批处理和通信优化。

## 6. MoE 究竟节省了多少计算？

现在终于可以比较参数量了。

假设：隐藏维度 `d = 8`，每个专家的中间维度 `m = 16`，有 `N = 4` 个专家，每个 Token 选择 `K = 2` 个专家。忽略偏置：

| 方案 | FFN / 专家参数 | Router 参数 | 单 Token 使用的主要 FFN 参数 |
| --- | ---: | ---: | ---: |
| 普通 SwiGLU，`m = 16` | 384 | 0 | 384 |
| 宽 SwiGLU，`m = 64` | 1536 | 0 | 1536 |
| MoE：4 个专家，每次选 2 个 | 1536 | 32 | 768 |

MoE 拥有 **1536 个专家参数**，另外有 **32 个 Router 参数**；但处理一个 Token 时，只经过两个专家，所用专家的权重总量是 **768**。Router 本身也需要运算，不能当成完全没有成本。

这个例子说明了一件很重要的事：与**专家参数总量相近的宽稠密 FFN**相比，MoE 可以让每个 Token 执行更少的专家矩阵乘法。

但如果拿它与最初那个 `m = 16` 的小 FFN 比，MoE 处理一个 Token 反而要运行两个这样的 FFN，计算更贵。**MoE 不是“同等一切条件下必然更省算力”，而是一种提高总模型容量与每 Token 计算量比例的方式。**

更一般地，对这个三投影 SwiGLU 例子：

```text
P_MoE_total ≈ 3N·d·m + N·d
P_selected_per_token = 3K·d·m
```

其中 `N·d` 是 Router 的参数量。这只是一个 MoE FFN 子层的分析，不等于整台大模型的“激活参数”统计；完整模型还有 Attention、Embedding、归一化、可能存在的共享专家等部分。

**还必须区分计算与显存：** 没被某个 Token 选中的专家，仍然拥有需要存储的权重。单卡加载全部专家时，权重显存不会因为 Top-K 自动降到 `K/N`。分布式部署又会带来跨设备通信成本。

## 7. 一个新问题：如果所有 Token 都选择同两个专家呢？

我们已经省掉了多余的专家计算，但 Router 的选择可能不均衡。

用上面的教学模型，故意把 Router 权重全部设为零：

```python
with torch.no_grad():
    model.router.weight.zero_()

_, collapsed_ids, _ = model(torch.randn(1, 20, 8))
counts = torch.bincount(collapsed_ids.reshape(-1), minlength=4)
print("人为构造的路由集中情况:", counts.tolist())
assert (counts > 0).sum().item() <= 2
```

所有 Token 都得到相同的 Router 分数；在固定的 `topk` 选取规则下，它们只会选中相同的两名专家。其他专家再大，也没有得到这批 Token 的计算任务。

这只是**人为构造的路由集中反例**，不是训练中自然发生专家坍缩的实验证据。不过它暴露了真实风险：

- 一部分专家持续过载，其他专家使用不足；
- 热门专家可能成为分布式推理和训练的瓶颈；
- 缺少训练信号的专家难以形成有用的分工。

我们因此需要在“让 Router 自由选择合适专家”和“不要把计算全部压到少数专家”之间取得平衡。这就是**专家负载均衡**问题。

### 一种经典思路：辅助负载均衡损失

以简化的 Top-1 路由为例，定义：

- `f_i`：实际分派给专家 `i` 的 Token 比例；
- `p_i`：Router 对专家 `i` 给出的平均概率。

可以添加如下鼓励负载平衡的损失：

```text
L_balance = N · Σ_(i = 1..N) f_i · p_i
```

它可以作为语言建模损失之外的一项辅助训练信号。实际 Top-K 版本还需要处理多个选中槽位、Padding 等细节。

这样的设计也有代价：如果强行让路由均匀，可能干扰语言模型原本希望学习的分工。辅助损失权重并不是越大越好。

到这里，一个基本 MoE 的设计问题已经完整出现了：**如何路由、如何只执行选中专家，以及如何避免负载极端失衡。**

## 8. 对照真实源码：Mixtral 如何实现这条链？

现在再看真实项目的实现，我们不会突然面对一大堆陌生结构。

本节固定阅读：

- 仓库：[huggingface/transformers](https://github.com/huggingface/transformers)
- 固定版本：[`v4.57.1`](https://github.com/huggingface/transformers/tree/v4.57.1)
- 主要文件：[`src/transformers/models/mixtral/modeling_mixtral.py`](https://github.com/huggingface/transformers/blob/v4.57.1/src/transformers/models/mixtral/modeling_mixtral.py)
- 对应的模块化源文件：[`src/transformers/models/mixtral/modular_mixtral.py`](https://github.com/huggingface/transformers/blob/v4.57.1/src/transformers/models/mixtral/modular_mixtral.py)

`modeling_mixtral.py` 在该版本中由模块化源码生成。以下类名、位置和运行机制均对应固定版本；为避免无关实现细节遮蔽主线，大部分代码采用**教学化简**而非逐字摘录，真正的仓库入口可以通过链接核对。

### 8.1 一个 Expert 到底是什么？

查看 `MixtralBlockSparseTop2MLP`。它里面有三个线性投影：`w1`、`w2`、`w3`。其计算等价于：

```python
# 教学化简：对应 MixtralBlockSparseTop2MLP.forward 的数学结构
hidden = activation(w1(x)) * w3(x)
output = w2(hidden)
```

和本章最开始的 `SwiGLUFFN` 并没有本质区别。**MoE 的 Expert 不是另一种神秘神经网络；这里就是 SwiGLU 风格的 FFN。**

### 8.2 Router 与 Top-K 在哪里？

查看 `MixtralSparseMoeBlock`：

```python
# 教学化简：省略 dtype、shape 和噪声等细节
router_logits = gate(hidden_states)
routing_weights = softmax(router_logits)
weights, selected_experts = topk(routing_weights, k=2)
weights = weights / weights.sum(dim=-1, keepdim=True)
```

对应真实源码中的 `self.gate`、`F.softmax`、`torch.topk` 和重新归一化。Mixtral 的配置由 `num_local_experts` 和 `num_experts_per_tok` 决定专家总数与 Top-K 数量。

输入是当前层的 `hidden_states`；输出不仅包括合并后的隐藏状态，也包括 `router_logits`。后者能供负载均衡等训练逻辑使用。

### 8.3 为什么还有一个按 Expert 处理 Token 的循环？

如果对所有 Expert 都运行整批 Token，就失去稀疏计算的意义。真实实现先形成选中专家的索引掩码，再找出各专家需要处理的 Token，最后将加权结果用 `index_add_` 累加到目标位置。

这正是我们在 `TinyMoE` 里手工实现的事情。实际工程中，它还能进一步使用专门的稀疏算子、专家并行等方式优化。

### 8.4 MoE 放在 Transformer 的哪一层？

沿调用路径继续看 `MixtralDecoderLayer.forward`，主要顺序是：

```text
进入 Decoder Layer
        ↓
      RMSNorm
        ↓
   Self-Attention
        ↓
    残差连接
        ↓
      RMSNorm
        ↓
 MixtralSparseMoeBlock
        ↓
    残差连接
        ↓
      返回下一层
```

**对这个架构来说，MoE 主要替换的是原本稠密的 FFN 分支，不是用来替换 Self-Attention。** 因此模型仍然可以通过注意力交换 Token 间的信息，而在 FFN 分支上使用条件计算。

源码位置：[`MixtralDecoderLayer`](https://github.com/huggingface/transformers/blob/v4.57.1/src/transformers/models/mixtral/modeling_mixtral.py#L270-L310)。

训练时，`MixtralForCausalLM` 可以结合 Router 信息计算辅助损失；对应的 `load_balancing_loss_func` 也在同一文件中。需要注意：**MoE 前向层负责路由并产生结果，不等于它自己单独完成全部训练损失计算。**

## 9. 为什么后来还要发展出 DeepSeekMoE？

到 Mixtral 这里，我们已经理解了 Top-K 稀疏专家。但新的取舍随之出现。

如果每个专家都很大，选中两个仍然要执行两套较大的 FFN；如果专家非常细，Router 和分布式调度又会更复杂。此外，并非所有信息都适合强迫专家彼此分工：许多 Token 可能需要一些共通的基础变换。

DeepSeekMoE 在这两个问题上继续演化：

**第一，细粒度专家。** 把专家划分得更细，使模型能够在更多、更小的专家之间组合选择。这里不应理解为“专家越多越好”，因为专家数量增加也会提高路由和通信难度。

**第二，共享专家（Shared Experts）。** 除了 Router 动态选择的 Routed Experts，再保留始终参与计算的共享 FFN 路径。于是输出概念上变成：

```text
y_t = E_shared(x_t) + Σ_{i ∈ S_t} w_{t,i} · E_i(x_t)
```

这个公式是**说明共享 + 路由结构的教学化简**；实际模型可能有多个共享专家以及额外的缩放或实现细节。

以 **DeepSeek-V3 技术报告（2024 年 12 月）** 为例：其 MoE 层包含 1 个共享专家、256 个路由专家，每个 Token 激活 8 个路由专家；报告给出的全模型规模为 **671B 总参数、每 Token 约 37B 激活参数**。前 3 层仍使用稠密 FFN，不能用“每层都是完全一样的 MoE”来概括它。

来源：[DeepSeek-V3 Technical Report](https://arxiv.org/html/2412.19437v1)。

### 9.1 负载均衡也不只有辅助损失一条路

DeepSeek-V3 报告提出使用**动态偏置**辅助决定路由选择。对于专家 `i`，可以用

```text
score_(t,i) + b_i
```

参与 Top-K 选择。被分配过多任务的专家，其路由偏置可以降低；分配不足的专家，偏置可以提高。

关键点是：**偏置参与“选谁”，但不直接改变用于加权专家输出的原始亲和分数。** 这样可以减少单纯依赖大权重均衡辅助损失带来的干扰。

这里必须准确区分：报告把主要负载均衡策略称为 *auxiliary-loss-free*，但仍使用了一个权重很小的序列级补充均衡损失。因此不能把它理解成“整个训练完全没有任何负载均衡辅助项”。

动态偏置的训练更新、专家分组和跨设备调度都比最小 MoE 更复杂，本章先不展开；它们已经不影响对 MoE 核心前向数据流的理解。

## 10. 为什么不是所有模型都采用 MoE？

从 Transformer FFN 走到这里，我们可以同时看到 MoE 的收益与代价。

**它解决了什么？**

它允许增加可学习专家的总容量，却不必让每个 Token 运行所有专家；路由器还可以根据隐藏表示动态选择计算路径。

**它没有免费解决什么？**

- **权重存储**：未激活的专家参数仍要被存储，可能需要多卡分布式部署。
- **通信与调度**：分布在不同设备上的专家，需要 Token 的分发与结果回传。
- **负载不均**：如果很多 Token 同时选择相同专家，热点专家会拖慢整个批次。
- **训练难度**：Top-K 选择是离散的；选中路径的权重仍可学习，但专家分配、训练稳定性和数据分布都会影响效果。
- **小批次性能**：理论上减少矩阵乘法，不代表在所有硬件、输入规模和实现方式上都更快。

因此，MoE 的正确理解不是“稀疏一定比稠密先进”，而是：**它用更复杂的路由、存储和调度，换取更大的总参数容量与更可控的每 Token 专家计算量。** 具体是否值得，必须看训练条件、硬件、延迟和模型质量。

## 11. 回看：这一章的架构是怎样长出来的？

从一开始那个普通 FFN 出发，我们并不需要提前知道 MoE 的所有缩写：

```text
普通 SwiGLU FFN
    │
    ├─ 扩大宽度能增加容量，但每个 Token 都要完整计算
    ↓
多个独立 FFN（Experts）
    │
    ├─ 如果每个 Token 都跑所有专家，计算仍然很贵
    ↓
Router 计算每个 Token 对各专家的分数
    │
    ├─ 只选择 Top-K，减少不必要的专家计算
    ↓
按专家分发 Token，计算后加权合并
    │
    ├─ 可能所有 Token 挤向少数专家
    ↓
负载均衡与训练约束
    │
    ├─ 大规模模型还面临通用知识与专家分工的取舍
    ↓
细粒度专家 + 共享专家 + 更精细的路由
```

现在如果看到一段真实的 MoE 源码，我们可以问出有意义的问题：这个 Router 根据什么打分？每个 Token 选几个专家？没选中的专家真的没执行吗？选中专家的输出如何合并？Router 的均衡如何训练？专家分布到多张 GPU 后，通信成本会不会吃掉节省的计算？

这比单纯记住“MoE = 混合专家”更接近理解一个现代大模型。

下一章我们将转向另一种越来越明显的成本：**模型生成长回答时，为什么越往后生成，过去 Token 的 Key / Value 缓存就越大？** 从一次普通的自回归生成开始，再逐层引出 KV Cache、GQA、MLA 等设计。

---

## 本章源码和论文

1. [Hugging Face Transformers `v4.57.1`：Mixtral 实现](https://github.com/huggingface/transformers/blob/v4.57.1/src/transformers/models/mixtral/modeling_mixtral.py)
2. [Hugging Face Transformers `v4.57.1`：Mixtral 模块化源码](https://github.com/huggingface/transformers/blob/v4.57.1/src/transformers/models/mixtral/modular_mixtral.py)
3. [Mixtral of Experts（2024）](https://arxiv.org/abs/2401.04088)
4. [DeepSeek-V3 Technical Report（2024）](https://arxiv.org/abs/2412.19437)
5. [Switch Transformers（2021）](https://arxiv.org/abs/2101.03961)

> **复现说明**：本章使用固定版本的真实源码核对设计；`TinyMoE` 是独立的教学化简，不与官方预训练权重绑定。实验的 `assert` 验证的是张量形状、Top-K 分配和与全量参考计算的一致性，而非模型质量或推理速度。
