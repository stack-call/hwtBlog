---
id: transformer-from-attention-to-llm
title: 第一章：Transformer 的核心架构
sidebar_label: 第一章：Transformer 的核心架构
sidebar_position: 2
description: 解决如何表示、交互、加工信息，以及如何进行自回归生成。
tags:
  - transformer
  - attention
  - llm
  - pytorch
---

# 第一章：从 Attention 到现代大模型：Transformer 为什么改变了深度学习

> 本文的核心问题不是“Transformer 有哪些组件”，而是：**一个最初用于机器翻译的序列模型，为什么最终成为大规模语言模型、多模态模型的重要基础？**
>
> 我们沿用同一个任务：将英文 `I love you` 翻译为中文 `我爱你`。从旧架构的限制出发，每次只增加一个解决问题的机制，最后再将这些机制拼成现代 Decoder-only Transformer。

## 一、为什么 Transformer 是一次重要变化？

### 1. 在 Transformer 之前，我们怎样处理不同数据？

神经网络的发展并不是一条“新架构淘汰旧架构”的直线，不同结构通常在解决不同的建模问题。

| 架构 | 主要特点 | 它擅长或试图解决的问题 |
| --- | --- | --- |
| MLP（多层感知机） | 线性变换与非线性激活的组合 | 通用函数拟合 |
| CNN（卷积神经网络） | 局部连接、参数共享 | 图像等具有局部空间结构的数据 |
| RNN（循环神经网络） | 隐藏状态沿时间步传递 | 按顺序处理文本、时间序列 |
| LSTM / GRU | 门控状态更新 | 缓解普通 RNN 的长程依赖和梯度问题 |
| Attention（注意力） | 通过查询与匹配动态聚合信息 | 让模型选择性读取其他位置的信息 |
| Transformer | 以注意力为核心，配合前馈网络、残差、归一化等 | 更易并行、可大规模训练的序列建模 |

注意：**Attention 不是 Transformer 首创的；Transformer 的关键创新之一，是在主干序列建模中不再依赖循环或卷积，而大量使用注意力。**

### 2. 从翻译任务开始：RNN 遇到了什么限制？

设输入为：

```text
I  love  you
```

早期的 Encoder–Decoder RNN 可以让编码器逐词读取输入，更新隐藏状态，再由解码器逐词输出：

```text
I  →  h1
love →  h2
 you →  h3  → Decoder → 我 → 爱 → 你
```

这个方案在短句中可以工作。但序列变长后会遇到两个问题：

- **信息路径长**：靠后的词要间接影响靠前的表示，通常需要经过多次状态更新；LSTM 能缓解，却没有消除这个顺序路径。
- **时间步串行**：计算 `h3` 依赖 `h2`，训练时难以像纯矩阵运算那样让整个序列充分并行。

于是，一个自然的问题出现了：**如果每个词都能直接查看其他词，而不必等待信息沿隐藏状态逐步传播，会怎样？**

### 3. Attention 改变了信息交互方式

Attention 让模型对当前词发起一次“查询”，找到相关位置，再把相关位置的信息加权汇总。

```text
                ┌── I
正在处理 love ────┼── love
                └── you
                     ↓
                汇总上下文
```

这不等于每个词都同等重要。模型会为不同位置计算不同权重，并根据具体输入动态调整。

Transformer 将这种机制变成了核心计算模块，使训练阶段不同位置的表示能够通过大规模矩阵运算并行计算。不过标准全注意力需要约 `O(n²)` 个位置间的分数，长序列仍有计算和显存成本。

### 4. 为什么它后来又通向了通用大模型？

Transformer 的影响来自**架构优势与训练范式的共同作用**，不能只归功于 Attention：

1. **并行训练更友好**：不必像 RNN 一样逐时间步更新隐藏状态，适合 GPU/TPU 上的大规模矩阵计算。
2. **长距离信息交互路径短**：一个注意力层就能让某个位置直接聚合远处位置的信息，但能“看到”不代表一定能“正确理解”。
3. **统一的 token 表示**：文字、图像 patch、音频片段等经过编码后都可以变成向量序列，再使用相近的注意力组件。
4. **适合大规模预训练**：借助自监督目标、海量数据、计算资源和优化工程，可以学到能够迁移到多个任务的表示与能力。
5. **具有可观察的规模收益**：在许多设置中，扩大参数、数据、计算预算能持续改善性能；具体收益取决于训练配方和资源分配。

因此，更准确的结论是：**Transformer 为大规模预训练提供了高效、灵活、可扩展的架构基础，而通用聊天、代码、推理等能力还依赖训练数据、目标、规模、对齐及推理方法。**

---

## 二、Self-Attention：一个词怎样读取其他词的信息？

现在让输入 `I love you` 的三个 token 都得到上下文表示。

### 1. 为什么要有 Q、K、V？

首先通过 Embedding 把 token ID 映射成向量。设隐藏状态组成矩阵：

```text
X ∈ R^(n × d_model)
```

如果直接拿一个向量同时负责“我要找什么”和“我要提供什么”，模型的自由度会受限制。于是把每个位置的隐藏状态投影成三种角色：

```text
Q = X · W_Q
K = X · W_K
V = X · W_V
```

- **Query（Q）**：当前位置想检索什么。
- **Key（K）**：每个位置用来被匹配的特征。
- **Value（V）**：匹配成功后实际汇入输出的信息。

这三个向量都由可学习矩阵产生，不是人工指定的语义标签。可以把 Q/K 看成决定信息流向的机制，把 V 看成被传递的内容表示。

### 2. 如何决定关注谁？

位置 `i` 的 Query 和位置 `j` 的 Key 做点积：

```text
score_ij = q_i^T · k_j
```

分数越大，说明当前投影空间中两者越匹配。考虑到维度增大时点积的数值尺度可能增长，除以 `√d_k`：

```text
S = (Q · K^T) / √d_k
```

再对每一行做 Softmax，得到注意力权重：

```text
A = softmax(S)        # 按行做：第 i 行是位置 i 对所有位置的权重
```

最后用这些权重聚合 Value：

```text
Attention(Q, K, V) = softmax( (Q · K^T) / √d_k ) · V
```

因此一个位置的最终输出，不再只与自身有关，而是输入序列中相关位置的信息加权和。

### 3. 用最小 PyTorch 代码观察它

下面是**教学化简**，只演示单头、没有位置编码和掩码的注意力；使用固定向量代替训练出来的 Q/K/V：

```python
import math
import torch

# 3 个 token，每个 token 的 Q/K/V 维度都是 3
q = torch.eye(3)
k = torch.eye(3)
v = torch.tensor([
    [1.0, 0.0, 0.0],
    [0.0, 2.0, 0.0],
    [0.0, 0.0, 3.0],
])

scores = q @ k.T / math.sqrt(k.shape[-1])
weights = torch.softmax(scores, dim=-1)
out = weights @ v

print("weights:", weights.round(decimals=3))
print("output:", out.round(decimals=3))
assert weights.shape == (3, 3)
assert torch.allclose(weights.sum(-1), torch.ones(3))
```

观察 `weights[i, j]`：它表示第 `i` 个 token 在更新自身表示时，从第 `j` 个 token 的 Value 中读取多少比例的信息。

这解决了“词之间如何交流”，但马上又出现一个新问题：**如果模型要从左到右生成文字，位置 `i` 不能偷看尚未生成的未来 token。**

### 4. Causal Mask：生成时为什么不能看未来？

训练一个从左到右预测下一个 token 的模型时，允许前一个位置看见后面的标准答案，会发生信息泄漏。因此在 Softmax 之前，把未来位置的分数设成负无穷：

```text
CausalAttention(Q, K, V) = softmax( (Q · K^T) / √d_k + M ) · V
```

其中 `M_ij = 0`（当 `j ≤ i`），否则 `M_ij = -∞`。

```text
             Key 0   Key 1   Key 2
Query 0        ✓       ×       ×
Query 1        ✓       ✓       ×
Query 2        ✓       ✓       ✓
```

PyTorch 中的教学化简：

```python
n = q.shape[0]
future_positions = torch.triu(
    torch.ones(n, n, dtype=torch.bool), diagonal=1
)
masked_scores = scores.masked_fill(future_positions, float("-inf"))
causal_weights = torch.softmax(masked_scores, dim=-1)
assert torch.all(causal_weights[future_positions] == 0)
```

**双向 Encoder 的自注意力**通常允许查看整个可见输入；**自回归 Decoder 的自注意力**通常需要 Causal Mask。这是两者一个非常关键的区别。

---

## 三、Multi-Head Attention：只有一种关联方式够吗？

一组 Q/K/V 可以学到一种匹配与聚合方式。但同一个句子里，我们可能希望同时关注词法、句法、指代或其他不同的特征关系。

于是将隐藏空间拆成多个注意力头（Head），每个头使用自己的投影：

```text
head_i = Attention(X · W_i^Q, X · W_i^K, X · W_i^V)
```

各个 Head 独立计算，再拼接并投影：

```text
MHA(X) = Concat(head_1, ..., head_H) · W_O
```

```text
输入 X
  ├─ Wq1, Wk1, Wv1 → Attention Head 1 ─┐
  ├─ Wq2, Wk2, Wv2 → Attention Head 2 ─┼→ Concat → Wo → 输出
  └─ Wq3, Wk3, Wv3 → Attention Head 3 ─┘
```

`W_O` 的作用是把拼接后的多头结果重新映射到模型隐藏维度，让不同头的结果得以混合，并方便后续残差连接。注意，多头**不保证**每个头一定学习到人类容易命名的独立语法功能。

到这里，注意力已经能按内容聚合信息。但又有一个基本问题：**如果调换词的顺序，模型怎样知道位置发生了变化？**

---

## 四、位置编码：为什么只有 Attention 还不够？

如果没有位置表示，普通自注意力对输入位置的置换具有相应的等变性，无法仅凭集合中的内容区分词序。

例如：

```text
A loves B
B loves A
```

两句话用到了相同的 token，却表达不同关系。我们必须让模型知道位置。

### 1. 最直观的方案：把位置向量加到词向量上

原始 Transformer 使用正弦/余弦位置编码：

```text
PE(pos, 2i)   = sin( pos / 10000^(2i/d) )
```

```text
PE(pos, 2i+1) = cos( pos / 10000^(2i/d) )
```

然后将其加到 token 表示上：

```text
X' = X + PE
```

这样做简单有效，也可以使用**可学习的位置嵌入**，例如 GPT-2。注意，加法通常会改变向量的数值和范数，但不是说“正余弦位置编码本身的长度发生了变化”：正余弦向量的范数与加入后表示的范数是两回事。

但对于注意力来说，我们往往还特别关心：两个 token **相隔多远**。RoPE 提供了一种很自然的几何方案。

### 2. RoPE 的想法：不相加，而是旋转 Q/K

二维向量 `(x_0, x_1)` 旋转角度 `θ`：

```text
[ x'_0 ]   [  cosθ  -sinθ ] [ x_0 ]
[ x'_1 ] = [  sinθ   cosθ ] [ x_1 ]
```

即：

```text
x'_0 = x_0 · cosθ - x_1 · sinθ
x'_1 = x_0 · sinθ + x_1 · cosθ
```

旋转不会改变向量的欧氏范数。因此，如果能根据位置对 Q/K 旋转，就能编码位置信息，同时保持原始向量长度（这里指标准、未额外缩放的 RoPE）。

为什么通常只作用在 Q/K 上？因为 Q/K 的内积决定注意力权重，位置可以直接影响“关注谁”；V 保留作为被加权汇总的内容。**这是主流设计选择，并非数学上绝对不能旋转 V。**

### 3. 高维向量怎样旋转？

设 `head_dim = 8`，位置为 `pos`。先把维度分为四组二维平面：

```text
相邻配对表示（interleaved）：
(x0, x1) (x2, x3) (x4, x5) (x6, x7)
```

每一组都旋转，但使用不同频率：

```text
θ(pos, i) = pos · ω_i
ω_i       = 10000^(-2i/d)
i         = 0, 1, ..., d/2-1
```

因此，位置越靠后，同一维度组的旋转角度累计越大；不同维度组以不同频率旋转。

下面是一个**可直接运行的教学实现**，使用相邻维度配对，`position` 长度必须与序列长度相同：

```python
import torch


def rope_interleaved(x: torch.Tensor, position: torch.Tensor):
    """x: [batch, seq_len, head_dim]; position: [seq_len]"""
    batch, seq_len, dim = x.shape
    assert dim % 2 == 0
    assert position.shape == (seq_len,)

    half_dim = dim // 2
    inv_freq = 1.0 / (
        10000 ** (
            torch.arange(half_dim, device=x.device, dtype=torch.float32)
            / half_dim
        )
    )
    angles = position.to(x.device, dtype=torch.float32)[:, None] * inv_freq[None, :]
    cos = angles.cos().to(dtype=x.dtype)[None, :, :]
    sin = angles.sin().to(dtype=x.dtype)[None, :, :]

    even = x[..., 0::2]
    odd = x[..., 1::2]

    even_new = even * cos - odd * sin
    odd_new = even * sin + odd * cos
    return torch.stack((even_new, odd_new), dim=-1).flatten(-2)


x = torch.tensor([[
    [0.5, 0.2, 0.1, 0.8, 0.3, 0.4, 0.7, 0.6],
    [0.6, 0.1, 0.2, 0.7, 0.4, 0.5, 0.8, 0.9],
    [0.9, 0.2, 0.2, 0.9, 0.2, 0.6, 0.2, 0.4],
]])

rotated = rope_interleaved(x, torch.arange(3))
assert rotated.shape == x.shape
assert torch.allclose(x.norm(dim=-1), rotated.norm(dim=-1), atol=1e-6)
assert torch.allclose(rotated[:, 0], x[:, 0])  # 位置 0 的旋转角度全为 0
```

这两个断言验证了标准旋转的重要性质：**输出形状不变、每个 token 向量的范数不变。**

### 4. 为什么 RoPE 会自然表达“相对距离”？

设 `R(m)` 表示位置 `m` 对所有二维组组成的块对角旋转矩阵。分别旋转两个位置的 Query 和 Key：

```text
q'_m = R(m) · q_m
k'_n = R(n) · k_n
```

注意力分数中的点积成为：

```text
(q'_m)^T · k'_n
  = (R(m) q_m)^T · (R(n) k_n)
  = q_m^T · R(m)^T · R(n) · k_n
  = q_m^T · R(n-m) · k_n
```

原因是旋转矩阵满足：

```text
R(m)^T · R(n) = R(n-m)
```

**这就是 RoPE 最值得理解的一步：我们明明分别按绝对位置 `m`、`n` 旋转 Q/K，但内积中却出现了相对位置 `n-m`。**

要注意，注意力分数**不只取决于距离**，它仍然取决于内容向量 `q_m`、`k_n`。RoPE 也不保证模型可以无限可靠地处理超出训练长度的上下文。

### 5. 为什么 Llama 的 `rotate_half` 看起来不一样？

很多人第一次读 Llama 的代码会困惑：它不是按 `(0,1)、(2,3)` 相邻维度旋转，而是将向量分成前后两半：

```python
# 真实源码节选；Hugging Face Transformers v4.57.1

def rotate_half(x):
    """Rotates half the hidden dims of the input."""
    x1 = x[..., : x.shape[-1] // 2]
    x2 = x[..., x.shape[-1] // 2 :]
    return torch.cat((-x2, x1), dim=-1)
```

这是另一种排列约定：

```text
相邻配对：   [x0, y0, x1, y1, x2, y2, x3, y3]
前后半区：   [x0, x1, x2, x3, y0, y1, y2, y3]
               └────────┘     └────────┘
                 对应配对维度：0↔4, 1↔5, 2↔6, 3↔7
```

Llama 的旋转写法为：

```python
# 真实源码核心表达式（省略广播与接口参数）
q_embed = q * cos + rotate_half(q) * sin
k_embed = k * cos + rotate_half(k) * sin
```

这里 `cos/sin` 的排列是各维度组频率**在前后两半各重复一次**。因此，它与相邻维度旋转在适当重排坐标后是数学等价的，但**不能在不重排向量与 cos/sin 的情况下直接混用两种实现**。

对应真实源码入口：

- [`rotate_half` 与 `apply_rotary_pos_emb`（v4.57.1）](https://github.com/huggingface/transformers/blob/v4.57.1/src/transformers/models/llama/modeling_llama.py)
- [`LlamaRotaryEmbedding`（同文件）](https://github.com/huggingface/transformers/blob/v4.57.1/src/transformers/models/llama/modeling_llama.py)

---

## 五、原始 Transformer：为什么最初需要 Encoder 和 Decoder？

我们回到机器翻译：输入 `I love you`，希望输出 `我爱你`。

模型实际上在执行两个职责不同的任务：

1. **理解输入**：为英文序列中的每个位置形成上下文化表示。
2. **生成输出**：每次根据已经生成的中文以及英文输入，预测下一个中文 token。

因此，2017 年原始 Transformer 采用 Encoder–Decoder 结构：

```text
英文 token ─→ Embedding + Position
                   ↓
             Encoder Block × N
                   ↓
             编码器输出 C
                   │
                   ├──────────────┐
                   ↓              ↓
                Cross-Attention 中的 K、V
                                  ↑
中文前缀 ─→ Embedding + Position    │
                  ↓                │
            Masked Self-Attention  │
                  ↓                │
             Cross-Attention  ─────┘
                  ↓
                 FFN
                  ↓
             Decoder Block × N
                  ↓
               Linear
                  ↓
               Softmax
                  ↓
              下一个 token
```

> 图为教学化简。原始论文中每个子层还含有残差连接和 LayerNorm，Encoder 和 Decoder 都是多层堆叠。上图的 `K、V` 来自编码器输出 `C` 的投影，`Q` 来自解码器当前隐藏状态。

### 1. Encoder 的自注意力

Encoder 可以让每个英文位置查看整个英文输入，从而得到：

```text
C = Encoder(X)
```

`C` 不是一个单独向量，而是每个输入 token 对应的上下文化隐藏状态组成的矩阵。

### 2. Decoder 的两种注意力

Decoder 需要分别解决两个问题：

- **Masked Self-Attention**：根据已经看到的中文前缀建立当前解码状态，不能提前查看未来输出。
- **Cross-Attention**：用解码器状态产生 Query，用 Encoder 输出产生 Key/Value，读取英文输入中相关的信息。

跨注意力的核心形式仍然是：

```text
Q = H_dec · W_Q
K = C · W_K
V = C · W_V
```

因此 Cross-Attention 不是一种全新公式，改变的主要是 Q、K、V **从哪里来**。

### 3. 那为什么后来的很多语言大模型只保留 Decoder？

如果任务不再局限于严格成对的“英文句子 → 中文句子”，而是给模型一段前缀，让它预测后续文本：

```text
输入：I love
目标：you
```

那么训练数据可以直接从大量原始文本中构造，不必为每条语料都准备人工配对的翻译目标。

这使 **Decoder-only + 下一个 token 预测** 成为特别实用的语言模型路线：

```text
p(x_1, ..., x_T) = Π_{t=1..T} p(x_t | x_<t)
```

经过大规模预训练，模型不仅能续写文本，也可能通过提示完成问答、翻译、摘要与其他任务。

但这不意味着 Encoder 或 Encoder–Decoder 被淘汰：BERT 类表征模型、T5 类序列到序列模型、ViT 类视觉编码器等仍然重要。**现代大语言模型常见 Decoder-only，现代 Transformer 并不全是 Decoder-only。**

---

## 六、只有 Attention 为什么还不够？Transformer Block 怎样一步步长出来？

现在我们已经具备：token 向量、位置表示、多头注意力和因果掩码。接下来逐步补齐一个真正可训练的 Transformer Block。

### 1. Attention 负责交流，FFN 负责逐位置加工

Attention 的核心是**跨 token 聚合信息**。但如果每次聚合完就直接进入下一次注意力，模型缺少对每个位置表示进行足够非线性加工的能力。

因此，原始 Transformer 在注意力后增加一个逐位置应用的前馈网络（Feed-Forward Network, FFN）：

```text
FFN(x) = W_2 · σ(W_1 · x + b_1) + b_2
```

典型 GPT-2 风格的维度变化：

```text
每个 token 的 h 维向量
          ↓
        Linear
       h → 4h
          ↓
         GELU
          ↓
        Linear
       4h → h
          ↓
  返回同一个 token 的 h 维向量
```

FFN 对不同 token 使用**同一组参数**，但在 FFN 内部并不会跨 token 交流。注意，所谓“两层 MLP”这里通常是指**两次线性变换，中间有一次非线性激活**；它并非只能被称作“一层 MLP”。

教学化简代码：

```python
import torch.nn as nn

ffn = nn.Sequential(
    nn.Linear(128, 512),
    nn.GELU(),
    nn.Linear(512, 128),
)
```

### 2. 堆很多层时怎么办？Residual + Normalization

如果不断堆叠注意力与 FFN，深层网络的优化会更加困难。于是加入两种机制：

- **Residual（残差连接）**：保留输入，并让子层学习对其进行修正。
- **Normalization（归一化）**：调整隐藏状态的数值尺度，帮助稳定训练。

原始 Transformer 的 Post-LN：

```text
y = LN(x + Sublayer(x))
```

GPT-2 和很多现代语言模型采用的 Pre-LN 风格：

```text
y = x + Sublayer(LN(x))
```

直观差别是：归一化位于残差相加**之后**，还是位于子层计算**之前**。Pre-LN 在深层训练中通常更容易优化，但不是唯一正确设计，也不是所有模型都完全相同。

### 3. GPT-2 Block：把刚刚的组件组合起来

```text
                  输入 x
                    │
               LayerNorm
                    │
            Masked Self-Attention
                    │
               + Residual  ←──── x
                    │
               LayerNorm
                    │
              FFN / MLP
                    │
               + Residual  ←──── 上一步的隐藏状态
                    │
                   输出
```

对照 Hugging Face Transformers **v4.57.1** 的 `GPT2Block.forward`，主要执行顺序正是：

```python
# 教学化简：保留实际顺序，省略缓存、可选 Cross-Attention 等分支
residual = hidden_states
hidden_states = self.ln_1(hidden_states)
attn_output, _ = self.attn(hidden_states, ...)
hidden_states = residual + attn_output

residual = hidden_states
hidden_states = self.ln_2(hidden_states)
feed_forward_hidden_states = self.mlp(hidden_states)
hidden_states = residual + feed_forward_hidden_states
```

- **位置**：[`modeling_gpt2.py` 中的 `GPT2Block`](https://github.com/huggingface/transformers/blob/v4.57.1/src/transformers/models/gpt2/modeling_gpt2.py)
- **调用者**：`GPT2Model` 按顺序执行堆叠的 Block。
- **输入**：上一个 Block 的隐藏状态，以及注意力掩码、缓存等可选上下文。
- **输出**：与输入隐藏维度相同、经过上下文交互和逐位置加工的新隐藏状态。
- **下游**：下一个 Block，最后再经输出归一化、语言模型输出头得到词表 logits。

GPT-2 中还有 Dropout；一些大规模现代 LLM 的训练配置会把特定 Dropout 概率设为 0，但不能据此认为 Transformer 理论上不需要 Dropout。

### 4. 为什么现代模型又把 LayerNorm 换成 RMSNorm？

LayerNorm 会减去均值，再按方差进行缩放。RMSNorm 主要根据均方根进行缩放，通常不做均值中心化：

```text
RMSNorm(x) = x / sqrt( (1/d) · Σ_{i=1..d} x_i^2 + ε ) ⊙ γ
```

它能以相对简洁的计算实现有效的数值归一化，因此常见于 Llama 等模型中。具体是否更合适，仍要看模型与训练设置。

### 5. 为什么现代 FFN 经常出现 SwiGLU？

传统 FFN 通常是：

```text
FFN(x) = W_down · σ(W_up · x)
```

SwiGLU 引入一个门控分支，典型形式为：

```text
SwiGLU(x) = W_down( SiLU(W_gate · x) ⊙ W_up · x )
```

直觉上，一路产生候选特征，另一路通过非线性门控调节这些特征：

```text
                   x
                /     \
         gate_proj   up_proj
               |       |
              SiLU     |
                \     /
               逐元素相乘
                    |
                down_proj
                    |
                   输出
```

注意，这里有 **gate、up、down 三个线性投影**。中间维度是模型配置项，不固定为 `3h`，也并非所有现代模型都使用 SwiGLU。

Hugging Face 的 Llama 源码中，`LlamaMLP.forward` 核心是：

```python
# 真实源码节选：Transformers v4.57.1，LlamaMLP.forward
down_proj = self.down_proj(self.act_fn(self.gate_proj(x)) * self.up_proj(x))
return down_proj
```

---

## 七、随着生成越来越长，还需要哪些优化？

这里的出发点不再是“能不能建模”，而是“能不能以可接受的显存和延迟持续生成”。

### 1. KV Cache：为什么不每次重新计算整段前缀？

自回归生成一次只增加一个新 token。已有 token 的 Key/Value，在因果注意力中不会因为未来追加 token 而改变，因此推理时通常缓存先前计算过的 K/V：

```text
第 1 步：计算 token 0 的 K、V → 存入 Cache
第 2 步：只计算新 token 1 的 Q、K、V → 读取旧 Cache
第 3 步：只计算新 token 2 的 Q、K、V → 读取旧 Cache
```

这样避免重复计算历史位置的 K/V；但新 token 仍需关注缓存中的历史 K/V，因此长上下文并不是零成本。

### 2. GQA：为什么 Query Head 多，KV Head 可以少？

标准 Multi-Head Attention（MHA）通常每个 Query Head 对应自己的 K/V Head。KV Cache 显存可能随层数、上下文长度及 KV Head 数量增长得很快。

Grouped-Query Attention（GQA）让多个 Query Head 共享一组 K/V Head：

```text
Q head 0 ─┐
Q head 1 ─┴── K/V head 0
Q head 2 ─┐
Q head 3 ─┴── K/V head 1
```

例如 8 个 Query Head 和 2 个 KV Head，每组有 4 个 Query Head 共享一组 K/V。这减少了 KV Cache 的容量和读取带宽需求，同时保留多个 Query Head 的表达能力，具体质量与效率取决于模型配置。

源码中的 `repeat_kv` 正对应这个逻辑：在执行常规注意力计算时，将共享的 K/V 以逻辑上可匹配 Query Head 的方式展开；优化内核不一定真的物理复制这些数据。

### 3. MoE：FFN 也一定要每个 token 用同一套参数吗？

传统 Dense FFN 对每个 token 执行相同的大网络。如果希望增加模型的总参数容量，又不让每个 token 都执行全部参数，可以使用 Mixture of Experts（MoE）：由路由器选择部分专家 FFN 处理当前 token。

**MoE 主要是对 FFN/专家计算路径的扩展，不是 Attention 或 Transformer 的必备组成。**它还会带来路由、负载均衡、通信等工程复杂度。

---

## 八、把所有组件拼回现代 Decoder-only Transformer

至此，现代常见的 Llama 风格 Decoder-only 数据流就能自然推导出来：

```text
输入 token IDs
      │
  Token Embedding
      │
      x ─────────────────────────────┐
      │                              │
   RMSNorm                           │
      │                              │
   Q/K/V 投影                        │
      │                              │
   Q、K 应用 RoPE                    │
      │                              │
   Causal Self-Attention             │
   （可使用 GQA / KV Cache）         │
      │                              │
     W_O                             │
      │                              │
      └──────── + Residual  ◀────────┘
                    │
                    x ───────────────┐
                    │                │
                 RMSNorm             │
                    │                │
           MLP / SwiGLU              │
                    │                │
                    └─ + Residual ◀──┘
                           │
                    下一个 Decoder Block
                           │
                          ...
                           │
                       Final Norm
                           │
                       LM Head
                           │
                     词表 logits
                           │
                  预测下一个 token
```

注意这里有两个容易弄混的层级：

- **Embedding 和最终 LM Head 属于整个语言模型的首尾**，通常不在每一个 Decoder Block 内重复执行。
- **RoPE 在注意力内部作用于 Q/K**，不是在每层输入处简单把某个位置向量加到 Embedding 上。

### 1. 与真实 Llama Decoder Layer 对照

`LlamaDecoderLayer.forward` 的主路径（教学化简）：

```python
# 教学化简：省略具体函数参数、注意力缓存和后端分发
residual = hidden_states
hidden_states = self.input_layernorm(hidden_states)
hidden_states, _ = self.self_attn(hidden_states, ...)
hidden_states = residual + hidden_states

residual = hidden_states
hidden_states = self.post_attention_layernorm(hidden_states)
hidden_states = self.mlp(hidden_states)
hidden_states = residual + hidden_states

return hidden_states
```

真实调用流程：

```text
LlamaForCausalLM
    └─ LlamaModel
        ├─ embed_tokens
        ├─ rotary_emb（生成 cos / sin）
        ├─ layers[i]: LlamaDecoderLayer
        │    ├─ input_layernorm
        │    ├─ self_attn: LlamaAttention
        │    │    ├─ q_proj / k_proj / v_proj
        │    │    ├─ apply_rotary_pos_emb
        │    │    ├─ KV Cache（若启用）
        │    │    ├─ attention 实现（如 eager / SDPA 等）
        │    │    └─ o_proj
        │    └─ post_attention_layernorm → LlamaMLP
        └─ norm
    └─ lm_head → logits
```

这不是“展示组件列表”，而是前面每一步问题的答案在源码中的落点：

| 为什么需要 | 实际机制 | 主要源码入口 |
| --- | --- | --- |
| 把 token 变成向量 | Embedding | `LlamaModel.embed_tokens` |
| 让注意力包含位置信息 | RoPE | `LlamaRotaryEmbedding`、`apply_rotary_pos_emb` |
| 跨 token 交互并禁止偷看未来 | Causal Self-Attention | `LlamaAttention`、注意力掩码 |
| 多个视角读取信息 | 多头 / GQA | `q_proj`、`k_proj`、`v_proj`、`repeat_kv` |
| 融合注意力头 | 输出投影 | `o_proj` |
| 逐 token 非线性加工 | SwiGLU FFN | `LlamaMLP` |
| 稳定深层训练 | Pre-RMSNorm 与 Residual | `LlamaDecoderLayer` |
| 复用历史信息 | KV Cache | `past_key_values` 更新与读取 |

真实源码版本：[`huggingface/transformers v4.57.1 — modeling_llama.py`](https://github.com/huggingface/transformers/blob/v4.57.1/src/transformers/models/llama/modeling_llama.py)。不同版本可能会改变函数签名、Attention 后端和缓存管理；上述表格描述的是该版本主要逻辑，不表示每种后端都执行同一份 Python Attention 实现。

---

## 九、最终回顾：Transformer 的重要性究竟在哪里？

现在可以把整条演化路径压缩为一张图：

```text
要处理序列
    │
    ▼
RNN：使用隐藏状态传递历史
    │
    ├─ 长距离路径长、时间步依赖强
    ▼
Attention：让一个位置直接检索其他位置
    │
    ├─ 可以并行计算序列表示，但不知道词序
    ▼
Position Encoding / RoPE：提供位置与相对距离信息
    │
    ├─ 单一关联空间不够丰富
    ▼
Multi-Head Attention：并行学习多种匹配与聚合方式
    │
    ├─ 需要逐位置非线性变换
    ▼
FFN / SwiGLU：加工每个 token 的特征
    │
    ├─ 网络堆深后优化难
    ▼
Residual + LayerNorm / RMSNorm：帮助训练深层网络
    │
    ├─ 从左到右生成不能偷看未来
    ▼
Causal Mask + Decoder-only：支持自回归语言建模
    │
    ├─ 自回归推理有重复计算、KV 内存负担
    ▼
KV Cache + GQA：改善推理效率
    │
    ├─ 希望增加参数容量而控制每 token 计算量
    ▼
MoE（可选）：稀疏激活部分专家
```

这里的箭头表达的是**教学中的需求递进**，不是严格的技术发明年表。例如 Attention 在 Transformer 之前就已存在；原始 Transformer 一开始就含有多头注意力、FFN、残差与位置编码。

如果只记住三点：

1. **Attention 决定信息如何在 token 之间流动，FFN 决定单个 token 的表示如何被非线性加工。**
2. **RoPE 通过旋转 Q/K，让注意力点积自然出现相对位置差 `n-m`。**
3. **Transformer 的历史突破不只是一个公式，而是它与并行计算、海量数据、自监督预训练和规模化工程的结合。**

接下来阅读具体模型源码时，就可以沿着一条正常前向路径追踪：

```text
input_ids → embeddings → decoder layers
          → attention / FFN → logits → next token
```

先能解释一次完整的前向计算，再去看后面那些推理优化与规模化设计，会更容易理解它们为什么出现。

## 十、从 Transformer 到现代大模型：架构已经建立，新的问题才刚刚开始

到这里，我们已经从最初的 RNN，一步步构建出了现代 Decoder-only Transformer。

现在，一个模型已经能够完成从输入 Token 到预测下一个 Token 的完整计算。Attention 负责信息交互，FFN 负责非线性加工，RoPE 提供位置信息，残差连接和归一化帮助模型训练得更深。

但我们目前回答的，其实只是一个问题：

**怎样设计一个能够有效学习和处理序列的神经网络？**

当 Transformer 被真正用于训练越来越大的语言模型时，研究者开始面对另一个问题：

**既然扩大模型规模能够在许多任务上带来性能收益，那么我们应该怎样继续扩大模型？**

最直接的办法当然是增加 Transformer 层数、隐藏维度和 FFN 的中间维度。

这些办法确实能够增加模型容量，但也会带来计算和存储成本。

例如，一个普通的 SwiGLU FFN 包含三个线性投影：

```text
FFN(x) = W_down · ( SiLU(W_gate · x) ⊙ W_up · x )
```

如果隐藏维度为 `d`，中间维度为 `m`，忽略偏置，其参数量为：

```text
P_FFN = 3dm
```

当我们把中间维度扩大四倍时，FFN 的参数量和主要矩阵乘法的计算量也大约扩大四倍。

而且，这种计算会发生在每一个 Token 上。

这就出现了一个新的矛盾：

我们希望模型拥有更多参数，以便学习更复杂的表示和处理模式；但又不希望每个 Token 都必须执行所有新增的计算。

于是，一个新的问题出现了：

**能不能让模型拥有大量参数，但每次只使用其中的一小部分？**

注意，这不是让参数凭空消失。没有参与当前计算的参数依然需要存储，可能也需要跨 GPU 分布。

我们真正想做的是将两个原本紧密关联的概念部分解耦：

- 模型总参数量：整个模型拥有多少可学习参数。
- 每 Token 激活参数量：处理当前 Token 时，实际参与计算的参数有多少。

如果能让模型总容量持续增加，而每 Token 的计算量增长得更慢，就可能获得一种新的规模扩展方式。

这就是下一章要研究的 **Mixture of Experts（MoE，混合专家）**。

我们不会直接从 Router、Top-K 或复杂的专家并行开始，而是先回到一个最普通的 FFN，看它究竟在哪一步遇到了限制，又怎样一步步演化出专家路由机制。

## 参考资料与源码版本

- Vaswani et al. (2017), [Attention Is All You Need](https://arxiv.org/abs/1706.03762)：原始 Encoder–Decoder Transformer。
- Su et al. (2021), [RoFormer: Enhanced Transformer with Rotary Position Embedding](https://arxiv.org/abs/2104.09864)：RoPE 的数学机制。
- Hugging Face Transformers **v4.57.1**，[GPT-2 实现](https://github.com/huggingface/transformers/blob/v4.57.1/src/transformers/models/gpt2/modeling_gpt2.py)：Pre-LN、Attention、MLP 与 Residual。
- Hugging Face Transformers **v4.57.1**，[Llama 实现](https://github.com/huggingface/transformers/blob/v4.57.1/src/transformers/models/llama/modeling_llama.py)：RoPE、GQA、RMSNorm、SwiGLU 和 Decoder Layer。
