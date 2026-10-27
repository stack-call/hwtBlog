---
id: modern-llm-chapter-04-long-context-attention
title: 第四章：从全量 Attention 到长上下文架构，为什么压缩 KV Cache 还不够？
sidebar_label: 第四章：长上下文与混合注意力
sidebar_position: 5
description: 从长序列的实际计算成本出发，逐步推导滑动窗口、稀疏注意力、线性注意力与混合架构，使用 PyTorch 验证机制，并精读固定版本 Gemma 3 与 Qwen3-Next 的实现。
---

# 第四章：从全量 Attention 到长上下文架构，为什么压缩 KV Cache 还不够？

上一章，我们已经解决了逐 Token 生成时的一连串问题：

- 不想每次重新计算历史前缀，于是保存 **KV Cache**。
- 不想缓存太多 K/V 头，于是从 MHA 走向 **MQA/GQA**。
- 不想一直保存展开后的完整 K/V，于是研究 **MLA 的低秩表示与矩阵吸收**。

这些机制确实能减轻推理的计算与存储压力。但它们还没有改变一个关键事实：**在标准全量因果注意力中，新 Token 仍要与所有可见历史位置计算关联。**

继续沿用我们的代码修复任务：

> 模型正在阅读一个大型项目。最开始的 `refund.py` 说明里写着“所有退款金额必须使用 `Decimal`”；中间是几百个源码文件，最后是 `pytest` 报错。模型此时需要判断修复是否遵守了开头那条约束。

如果上下文有十几万个 Token，即使 KV Cache 已经被 GQA 或 MLA 压缩，模型怎样高效地**找到、利用**开头那条信息，仍然是一个问题。

这一章不先报出所有新架构的名字。我们先从普通 Attention 真正在做的计算开始，看看瓶颈在哪一步，再逐步修改它。

## 1. 缓存变小了，为什么长上下文还是贵？

回顾因果注意力：

```text
Attention(Q, K, V) = softmax( (Q · K^T) / √d_k + M ) · V
```

假设上下文长度为 `T`、每个注意力头的维度为 `d`。

**预填充阶段（Prefill）**，模型需要处理整个输入前缀。标准全量 Attention 的 `Q · K^T` 会产生一个 `T × T` 的分数矩阵；忽略常数与头数，计算规模约为：

```text
O(T² · d)
```

**逐 Token 解码阶段（Decode）**，由于 KV Cache 已经保存历史，当前只新增一个 Query。这个 Query 不需要重新计算所有历史 Query，但仍然要与约 `T` 个历史 Key 进行匹配，并读取相应 Value：

```text
O(T · d)      # 单个新 Token 的一次全量注意力读取，忽略头数
```

两者不是同一个 `O(T²)`：预填充处理的是许多 Query，增量解码一次通常只有一个 Query。但是随着上下文变长，**每次生成的新 Token 访问的历史仍然越来越多**。

让我们先打印最简单的量级，不涉及任何模型权重。

**实验 1：单个头在不同序列长度下的注意力分数数量**

```python
lengths = [1024, 8192, 65536, 131072]
for t in lengths:
    prefill_scores = t * t
    decode_scores = t
    print(f"T={t:6d} | prefill={prefill_scores:>12,d} | one-step decode={decode_scores:>7,d}")
```

输出：

```text
T=  1024 | prefill=   1,048,576 | one-step decode=  1,024
T=  8192 | prefill=  67,108,864 | one-step decode=  8,192
T= 65536 | prefill=4,294,967,296 | one-step decode= 65,536
T=131072 | prefill=17,179,869,184 | one-step decode=131,072
```

这里计数的是**理论上的分数位置数**。实际因果注意力只使用下三角，专用内核也不一定真的把整个矩阵写到显存；不要把这些数字误认为实测显存占用或运行时间。

但问题已经很清楚：缓存机制避免了重复生成历史 K/V，却没有自动减少**需要关注多少个历史位置**。

于是出现第一个自然的想法：**既然最近的上下文往往最相关，我们能不能只看最近一段？**

## 2. 最简单的修改：把全量 Attention 改成滑动窗口

标准因果 Attention 在位置 `t` 可以看到：

```text
{0, 1, ..., t}
```

设定窗口长度为 `W` 后，只允许当前位置看到最近 `W` 个位置（包括自己）：

```text
V_t = {max(0, t - W + 1), ..., t}
```

这就是 **Sliding Window Attention（滑动窗口注意力）** 的基本约束。注意力公式并没有换掉，只是允许参与 softmax 的位置集合变小。

```text
完整因果 Attention（当前 Token = 7）

历史位置： 0  1  2  3  4  5  6  7
可见性：   ✓  ✓  ✓  ✓  ✓  ✓  ✓  ✓

滑动窗口 Attention，W = 3

历史位置： 0  1  2  3  4  5  6  7
可见性：   ·  ·  ·  ·  ·  ✓  ✓  ✓
```

如果实际实现只计算允许的连接，那么：

- 全量注意力在预填充中约需要 `O(T²·d)` 的成对计算。
- 固定窗口注意力约需要 `O(T·W·d)` 的成对计算。
- 若推理缓存也被正确限制在最近 `W` 个位置，局部层的长期 KV 存储可从随 `T` 增长变为与 `W` 有关。

这里有一个重要前提：**写出一个滑动窗口 Mask，不等于底层实现已经节省计算或真的丢弃旧缓存。** 如果仍用一个稠密的 `T × T` 矩阵乘法，再把大部分分数填成负无穷，那么数学结果是局部的，但计算不一定是局部的。

先验证语义，再谈内核效率。

## 3. 最小实验：只改 Mask，看看输出发生了什么

下面的代码刻意不加入线性投影、RoPE、GQA 或复杂的缓存。我们只想观察：**允许访问哪些历史位置，会怎样影响结果？**

**教学化简：一个头的因果 Attention。** 这不是 Gemma 或 Qwen 的源码。

```python
import math
import torch


def attention(q, k, v, window=None, anchors=()):
    """
    q, k, v: [T, D]（这里演示 q/k/v 序列长度相同）
    window: None 表示全量因果注意力；整数表示最近窗口大小
    anchors: 额外允许访问的固定历史位置
    """
    t, d = q.shape
    rows = torch.arange(t, device=q.device)[:, None]
    cols = torch.arange(t, device=q.device)[None, :]

    # 标准因果约束：不能看未来
    allowed = cols <= rows
    if window is not None:
        allowed = allowed & (cols >= rows - window + 1)

    # 对每个位置额外开放少量固定位置，但仍然不能看未来
    for anchor in anchors:
        allowed |= (cols == anchor) & (cols <= rows)

    scores = q @ k.T / math.sqrt(d)
    scores = scores.masked_fill(~allowed, float("-inf"))
    weights = scores.softmax(dim=-1)
    return weights @ v, allowed


# 把 Key 与 Query 设置为 0，使每个“可见位置”的权重相同。
# 只有最早的 Token 携带一个非零的 Value。
t = 8
q = torch.zeros(t, 1)
k = torch.zeros(t, 1)
v = torch.zeros(t, 1)
v[0, 0] = 1.0

full, full_mask = attention(q, k, v)
local, local_mask = attention(q, k, v, window=3)

print("全量注意力：最后一个位置的输出 =", full[-1, 0].item())
print("滑动窗口：最后一个位置的输出 =", local[-1, 0].item())
print("滑动窗口可见的历史位置 =", torch.where(local_mask[-1])[0].tolist())
```

结果：

```text
全量注意力：最后一个位置的输出 = 0.125
滑动窗口：最后一个位置的输出 = 0.0
滑动窗口可见的历史位置 = [5, 6, 7]
```

为什么？

- 全量 Attention 看到了 8 个位置。因为分数全部相同，每个位置权重都是 `1/8`，于是输出为 `1/8`。
- 窗口长度为 3 时，最后一个位置只能看见位置 `5、6、7`；位置 `0` 的信息被当前层的直接注意力连接排除，所以输出是 0。

**我们确实减少了允许访问的位置，但同时也丢失了远距离直接检索的能力。**

在代码修复任务中，这意味着：如果开头的 `Decimal` 约束没有被传递到最近窗口中，当前层的注意力就不能直接找到它。

滑动窗口不等于模型整体永远看不到远处的信息，但这里明确暴露了**单层局部注意力的直接访问限制**。

## 4. 多堆几层局部 Attention，能不能传递远处的信息？

可能有人会问：Transformer 不只有一层。虽然每层只能看最近 `W` 个位置，但较早的层已经把局部上下文融合进隐藏状态了，后面的层是否可以继续向前传递？

答案是：**可以逐层传播，但传播范围不是无限的。**

忽略跨层特殊连接，假设每层都是窗口大小为 `W` 的因果局部注意力，那么经过 `L` 层后，位置 `t` 的表示最多可以从约

```text
1 + L · (W - 1)
```

个连续历史位置内获得信息；这里说的是**理论可达范围**，不是“必然学会保留其中所有信息”。

以 `W = 3` 为例：

```text
位置 7 的表示，经过：

第 1 层：最多依赖位置 5～7
第 2 层：最多依赖位置 3～7
第 3 层：最多依赖位置 1～7
第 4 层：最多依赖位置 0～7
```

用几行代码计算边界：

```python
sequence_end = 7
window = 3
for layers in range(1, 5):
    left = max(0, sequence_end - layers * (window - 1))
    print(f"{layers} 层局部注意力：理论上可从 [{left}, {sequence_end}] 获得信息")
```

这让滑动窗口看起来没有那么糟糕：信息有机会通过多层局部交换逐渐传播。

但真实任务又引出两个新困难：

1. 远处的一条精确约束可能需要经过许多层中转，不能保证被完整保留。
2. 如果所有层都严格局部，那么“当前 Token 直接定位十万 Token 以前的一段源码”并不容易。

我们真正想要的是：**大部分位置采用便宜的局部处理，但仍保留某种跨越远距离的路径。**

接下来先看一个实际模型如何折中。

## 5. 真实源码：Gemma 3 为什么交替安排局部层与全局层？

Gemma 3 使用一个很直观的想法：**多数层处理局部上下文，隔一定层数安排一层全局注意力。** 这种混合方式不要求每一层都对完整历史做全量 Attention。

官方公开资料描述了 **5 个局部层 + 1 个全局层** 的重复模式。我们去固定版本的 Hugging Face Transformers 源码里核对这种设计怎样被表达。

- **仓库**：`huggingface/transformers`
- **版本**：`v4.57.1`
- **配置入口**：[`src/transformers/models/gemma3/configuration_gemma3.py`](https://github.com/huggingface/transformers/blob/v4.57.1/src/transformers/models/gemma3/configuration_gemma3.py)
- **执行相关位置**：[`src/transformers/models/gemma3/modeling_gemma3.py`](https://github.com/huggingface/transformers/blob/v4.57.1/src/transformers/models/gemma3/modeling_gemma3.py)

**真实源码节选（`Gemma3TextConfig`，省略外围校验逻辑）：**

```python
self.layer_types = [
    "sliding_attention" if bool((i + 1) % self._sliding_window_pattern) else "full_attention"
    for i in range(self.num_hidden_layers)
]
```

配置中默认的 `sliding_window_pattern` 为 `6`；也就是每 6 层中的前 5 层为 `sliding_attention`，第 6 层为 `full_attention`（实际加载的模型也可能提供显式 `layer_types`，以配置为准）。

这还只是**决定每一层是什么类型**，并没有真的执行注意力。进入模型前向过程后，代码还会根据层类型准备不同的 Mask。

**真实源码节选（模型前向中的 Mask 选择，省略其他参数准备）：**

```python
causal_mask_mapping = {
    "full_attention": create_causal_mask(**mask_kwargs),
    "sliding_attention": create_sliding_window_causal_mask(**sliding_mask_kwargs),
}
```

理解这两段代码就足够建立正常路径：

```text
Gemma3TextConfig.layer_types
             ↓
当前 Decoder Layer 属于哪种类型？
       ┌─────┴─────┐
       ↓           ↓
sliding_attention  full_attention
       ↓           ↓
滑动窗口因果 Mask    全量因果 Mask
       └─────┬─────┘
             ↓
     该层执行 Attention
```

这并不是在两种 Attention 中随机选择一个，而是**在构建模型时确定各层职责**。

这个方案的收益与限制可以同时看清：

- **收益**：局部层不必在语义上读取所有历史位置，全局层提供远距离直接交互的机会。
- **限制**：全局层依然具有长上下文成本；并且可见性只是能力的前提，不保证模型一定能精准回忆远处细节。
- **工程边界**：这里读到的是层类型与 Mask 的核心代码；不同注意力后端、缓存类型是否按窗口物理裁剪，还需要沿其对应实现继续核对，不能只凭一个 Mask 就断言获得了线性加速。

现在我们已经拥有一种简单的混合结构：局部 + 全局。

但当上下文特别长时，全局层仍然昂贵。下一步能不能更加精细：**不是固定地只看最近窗口，而是只访问历史中真正有用的少数位置？**

## 6. 第二种思路：不必看所有历史，只查重要的位置

假设项目里有十万个 Token。当前问题是“退款金额为什么不精确？”从内容看，最值得访问的可能是：

- `refund.py` 中涉及金额转换的几行。
- `tests/test_refund.py` 中关于精度的断言。
- 很久以前写下的 `Decimal` 约束。

其余大量 Token 未必都需要在**当前这一层、当前这次查询**中参与注意力。

如果能够找出候选集合 `𝒮_t`，只针对这些位置计算精细的注意力：

```text
Attention_t = softmax( (q_t · K_𝒮_t^T) / √d_k ) · V_𝒮_t
```

这就是**稀疏注意力（Sparse Attention）**背后的一个核心想法：允许的连接不再覆盖所有历史位置。

关键是，`𝒮_t` 怎样决定？

最简单的集合可以是固定模式，例如：

```text
最近 3 个 Token + 一些始终可访问的全局锚点
```

继续使用刚才的实验，不改变 Q/K/V，只给位置 `0` 一个“全局锚点”身份：

```python
sparse, sparse_mask = attention(q, k, v, window=3, anchors=(0,))
print("稀疏模式：最后一个位置的输出 =", sparse[-1, 0].item())
print("稀疏模式可见的历史位置 =", torch.where(sparse_mask[-1])[0].tolist())
```

输出：

```text
稀疏模式：最后一个位置的输出 = 0.25
稀疏模式可见的历史位置 = [0, 5, 6, 7]
```

因为此时最后一个位置可以看到 `0、5、6、7`，四个位置权重相同，只有位置 0 的 Value 为 1，所以输出为 `1/4`。

我们只开放了一个远处位置，就重新接触到了很久以前的信息。

但这个教学例子故意回避了最难的问题：**谁知道应该开放位置 0？**

如果是在真实代码仓库中，关键约束可能出现在任意文件、任意位置。固定锚点不一定命中它。

于是，下一层需求从“减少连接”变成了：**根据当前 Query 的内容，动态寻找值得访问的历史片段。**

这就出现了内容驱动的稀疏选择，例如基于轻量打分器、索引、块选择等方法先筛选，再执行较昂贵的注意力。近年来一些模型还采用微块（micro-block）而非单个 Token 作为筛选单位，以降低选择器本身的开销。例如 Qwen 团队在 2026 年公开的 Qwen3.8-Flash-Next 中介绍了 **Qwen Sparse Attention（QSA）** 的块级选择思路。参见[官方架构说明](https://huggingface.co/Qwen/Qwen3.8-Flash-Next)与[技术介绍](https://www.alibabacloud.com/blog/qwen3-8-flash-next-a-new-architecture-towardsultimate-cost-efficiency_603501)。

但这里必须区分两种成本：

1. **筛选成本**：怎样在很长的历史里找到候选块？
2. **精细注意力成本**：怎样只对候选块真正执行计算？

如果筛选器本身仍要完整扫描所有历史，或者虽然设置了稀疏 Mask、底层却仍执行稠密矩阵乘法，那么理论上选择的 Token 变少，并不意味着端到端延迟一定下降。

稀疏注意力减少的是**显式访问的连接数量**，但选择器可能漏掉关键内容，也会引入实现和训练上的复杂性。

到这里，我们还保留着普通 softmax 注意力，只是决定不要看全部历史。

另一个方向更大胆：**可不可以不保存逐个历史位置，而是把它们不断汇总进一个状态？**

## 7. 第三种思路：线性注意力为什么能把历史写进状态？

先把普通 softmax 的一个 Query 写成求和：

```text
y_t = ( Σ_(j ≤ t) exp(q_t^T · k_j) · v_j )
    / ( Σ_(j ≤ t) exp(q_t^T · k_j) )
```

这里暂时省略 `1/√d_k`，只看结构。分子、分母都需要访问历史 Key，计算当前 Query 与它们的关系。

如果我们改用一种**可分解的非负核相似度**：

```text
sim(q, k) = φ(q)^T · φ(k)
```

其中 `φ(·)` 把输入映射成非负特征，那么：

```text
y_t = ( Σ_(j ≤ t) [φ(q_t)^T · φ(k_j)] · v_j )
    / ( Σ_(j ≤ t) φ(q_t)^T · φ(k_j) )
```

由于矩阵乘法的结合律，可以把与当前 Query 无关的历史部分先聚合：

```text
S_t = Σ_(j ≤ t) φ(k_j) · v_j^T
z_t = Σ_(j ≤ t) φ(k_j)
```

于是输出变为：

```text
y_t = ( φ(q_t)^T · S_t ) / ( φ(q_t)^T · z_t )
```

最重要的变化不是表面上出现了新公式，而是 **`S_t` 与 `z_t` 可以递推更新**：

```text
S_t = S_(t-1) + φ(k_t) · v_t^T
z_t = z_(t-1) + φ(k_t)
```

```text
新 Token 的 Key / Value
           │
           ▼
   更新 S_t 与 z_t  ← 上一步的 S_(t-1) 与 z_(t-1)
           │
           ▼
用当前 Query 读取状态
           │
           ▼
         输出
```

这类思路通常称为 **线性注意力（Linear Attention）** 或可递归核注意力。

如果 `φ` 的特征维度与 Value 维度固定，那么用于增量解码的历史状态尺寸不再随着序列长度 `T` 线性增长；每个新 Token 也不必和所有历史 Key 做一次显式的点积。

不过请特别注意：

**这个式子不是 softmax Attention 的一般恒等变形。** 我们把 `exp(q^T · k)` 换成了一个可分解的核。正是因为改变了相似度定义，才得到了易于递推的状态。

接下来必须用实验验证两件事：

1. 这个递推式是否和它自身的“全序列核注意力”完全一致？
2. 它是否也和普通 softmax 注意力一致？

## 8. 实验：核注意力的全量写法与递推写法等价

继续只用 PyTorch，固定一组小输入。

**教学实现：使用 `φ(x) = ELU(x) + 1` 的简单正值特征映射。** 它不是 Qwen3-Next 的 Gated DeltaNet 实现。

```python
import torch
from torch.nn import functional as F


def phi(x):
    return F.elu(x) + 1.0


def causal_kernel_attention_full(q, k, v):
    # 参考算法：显式构造 [T, T] 核相似度矩阵
    t = q.shape[0]
    scores = phi(q) @ phi(k).T
    causal = torch.tril(torch.ones(t, t, dtype=torch.bool, device=q.device))
    scores = scores.masked_fill(~causal, 0.0)
    return (scores @ v) / scores.sum(-1, keepdim=True)


def causal_kernel_attention_recurrent(q, k, v):
    # 等价递推算法：只维护累积状态，不构造 [T, T]
    key_dim = phi(k).shape[-1]
    value_dim = v.shape[-1]
    state = torch.zeros(key_dim, value_dim, dtype=q.dtype, device=q.device)
    normalizer = torch.zeros(key_dim, dtype=q.dtype, device=q.device)
    outputs = []

    for q_t, k_t, v_t in zip(q, k, v):
        k_feature = phi(k_t)
        q_feature = phi(q_t)
        state = state + torch.outer(k_feature, v_t)
        normalizer = normalizer + k_feature
        y_t = (q_feature @ state) / (q_feature @ normalizer)
        outputs.append(y_t)
    return torch.stack(outputs)


torch.manual_seed(7)
q = torch.randn(6, 4, dtype=torch.float64)
k = torch.randn(6, 4, dtype=torch.float64)
v = torch.randn(6, 3, dtype=torch.float64)

full = causal_kernel_attention_full(q, k, v)
recurrent = causal_kernel_attention_recurrent(q, k, v)
assert torch.allclose(full, recurrent, atol=1e-10)
print("全量核注意力与递推实现一致:", torch.allclose(full, recurrent, atol=1e-10))
print("输出形状:", tuple(recurrent.shape))
```

输出：

```text
全量核注意力与递推实现一致: True
输出形状: (6, 3)
```

这次实验验证了一个**精确的局部数学结论**：对当前定义的核注意力，累积状态的递推计算不必显式构造所有历史位置之间的相似度矩阵。

对于固定特征维度 `d_φ` 和 Value 维度 `d_v`，状态 `S` 大小约为 `d_φ · d_v`，而不是和上下文长度 `T` 成正比。但如果特征维度本身非常大，状态也可能不小；具体成本依然取决于设计和硬件。

我们还没有验证模型在代码修复上是否更准确，甚至还没有比较它与 softmax Attention。下一步正需要这个反例。

## 9. 为什么不能把整个 Transformer 都换成这种递推状态？

把历史压缩到固定大小的状态，听起来几乎完美：不会有越来越长的显式 KV Cache，也不用逐个访问所有 Key。

但压缩必然涉及取舍。不同的历史序列，可能被映射成相同的 `S_t`、`z_t`，从而使模型难以区分一些需要精确回忆的上下文。

我们可以构造一个非常小的反例，而不是笼统地说“线性注意力可能不擅长检索”。

考虑一维 Key、Value，以及正值区域中 `φ(k) = k + 1` 的特征映射（对应上面 ELU+1 在非负值时的行为）：

```text
历史 A：
key   = [0, 2]
value = [1, 0]

历史 B：
key   = [1, 1]
value = [0.5, 0]
```

对于简单核注意力，二者的累积状态完全相同：

```text
S_A = 1 · 1 + 3 · 0 = 1,    z_A = 1 + 3 = 4
S_B = 2 · 0.5 + 2 · 0 = 1,  z_B = 2 + 2 = 4
```

无论 Query 是什么，只要仍使用同一个一维特征映射，递推状态对这两段历史就无法给出不同结果。

而普通 softmax Attention 可以区分它们：两段历史的 Key 排列不同，Query 与 Key 的关系也不同。

**实验：相同递推状态，不同 softmax 输出**

```python
import math
import torch


def softmax_read(q, keys, values):
    weights = torch.softmax(q * keys, dim=0)
    return (weights * values).sum().item()


keys_a = torch.tensor([0.0, 2.0], dtype=torch.float64)
values_a = torch.tensor([1.0, 0.0], dtype=torch.float64)
keys_b = torch.tensor([1.0, 1.0], dtype=torch.float64)
values_b = torch.tensor([0.5, 0.0], dtype=torch.float64)
q = torch.tensor(1.0, dtype=torch.float64)

# 对这两个历史，phi(k)=k+1，累计分子=1，累计分母=4
linear_a = 1.0 / 4.0
linear_b = 1.0 / 4.0
print("核递推输出 A/B:", linear_a, linear_b)
print("softmax 输出 A:", round(softmax_read(q, keys_a, values_a), 6))
print("softmax 输出 B:", round(softmax_read(q, keys_b, values_b), 6))
```

输出：

```text
核递推输出 A/B: 0.25 0.25
softmax 输出 A: 0.119203
softmax 输出 B: 0.25
```

这说明了一个很实际的问题：**递推状态擅长汇总大量历史，却不一定保留原始历史中所有可逐项检索的细节。**

这只是某个简化核注意力的反例，不能据此断言所有线性注意力架构都无法长程检索。现代方法还会加入门控、选择性更新、卷积或更复杂的状态结构。它们恰恰是在努力缓解这里暴露的限制。

但回到代码修复任务，我们已经有一个合理的工程需求：

- 大部分层不必每次都查询所有历史；它们可以用固定大小或更便宜的状态总结上下文。
- 少数层仍应提供从原始历史中直接找回具体信息的途径。

于是，**混合线性注意力与全量注意力**变成了一个自然的折中。

## 10. 真实源码：Qwen3-Next 如何在不同层选择两种计算路径？

Qwen3-Next 是这条思路的一个明确实例。公开的架构资料介绍，它把 **Gated DeltaNet** 与标准 Attention 混合使用，常见模式是 **3 个线性注意力层 + 1 个全量注意力层**。

先不要把 Gated DeltaNet 等同于上一节的 ELU+1 核注意力。我们只借助最小实验建立“历史可压缩为递推状态”的直觉，然后再看真实模型用什么实现这一路径。

- **仓库**：`huggingface/transformers`
- **版本**：`v4.57.1`
- **配置文件**：[`src/transformers/models/qwen3_next/configuration_qwen3_next.py`](https://github.com/huggingface/transformers/blob/v4.57.1/src/transformers/models/qwen3_next/configuration_qwen3_next.py)
- **模型文件**：[`src/transformers/models/qwen3_next/modeling_qwen3_next.py`](https://github.com/huggingface/transformers/blob/v4.57.1/src/transformers/models/qwen3_next/modeling_qwen3_next.py)

**真实源码节选（`Qwen3NextDecoderLayer.__init__`，省略无关层）：**

```python
self.layer_type = config.layer_types[layer_idx]
if self.layer_type == "linear_attention":
    self.linear_attn = Qwen3NextGatedDeltaNet(config, layer_idx)
elif self.layer_type == "full_attention":
    self.self_attn = Qwen3NextAttention(config, layer_idx)
```

这几行把架构选择写得很清楚：**当前层要么走 Gated DeltaNet，要么走标准全量 Attention**。然后在 `Qwen3NextDecoderLayer.forward` 中，按照 `self.layer_type` 调用对应模块，再通过残差连接继续进入 MLP/MoE。

配置如何生成这组层类型？`Qwen3NextConfig` 在没有显式给出 `layer_types` 时，会按 `full_attention_interval` 形成规律性的混合模式，默认间隔为 4。

我们用一个纯 Python 的**教学复现**生成同样的层类型规则：

```python
def make_layer_types(num_layers, full_interval=4):
    return [
        "linear_attention" if (i + 1) % full_interval else "full_attention"
        for i in range(num_layers)
    ]

print(make_layer_types(8))
```

输出：

```text
['linear_attention', 'linear_attention', 'linear_attention', 'full_attention', 'linear_attention', 'linear_attention', 'linear_attention', 'full_attention']
```

```text
Qwen3-Next（以 8 层示意）

输入 Token 隐藏状态
          │
          ▼
    Gated DeltaNet  ──┐
          ▼           │ 主要使用压缩/递推的历史状态
    Gated DeltaNet    │
          ▼           │
    Gated DeltaNet  ──┘
          ▼
      Full Attention ─── 直接访问 KV 历史
          ▼
    Gated DeltaNet
          ▼
    Gated DeltaNet
          ▼
    Gated DeltaNet
          ▼
      Full Attention
          ▼
        输出
```

此时还需要追问一个真正的运行时问题：**既然两种层的历史表示不同，缓存对象怎么处理？**

在固定版本的 `Qwen3NextDynamicCache` 中，可以看到分别保存的 `conv_states`、`recurrent_states`、`key_cache` 和 `value_cache`。前两类为线性/递推路径服务，后两类为全量注意力路径服务；实际前向会依据层类型与缓存状态使用对应内容。

这比“Qwen3-Next 使用了混合注意力”更有解释力：

1. **配置**决定一层走哪条路径。
2. **Decoder Layer** 调用对应的注意力模块。
3. **Cache** 保存该模块运行所需的历史信息。
4. 当前层的输出通过残差与后续 FFN/MoE 流向下一层。

Gated DeltaNet 的具体门控、Delta Rule 更新和分块并行算法属于下一层复杂度。此处先把“为什么要混合、在哪里切换、保存什么历史状态”讲清楚，而不是在尚未理解混合动机时一次展开全部数学细节。

这也是现代架构与原始 Transformer 的一个明显不同：**不同层可以用不同方式混合 Token 信息，而不必让每一层都执行完全相同的全量 self-attention。**

## 11. 这和 FlashAttention 是一回事吗？

不是。这里非常容易把名字相近的技术混在一起。

我们到目前为止研究的是：

- **Sliding Window**：限制能看到哪些位置。
- **Sparse Attention**：只选择部分连接，可能是固定模式，也可能是动态检索。
- **Linear / Recurrent Attention**：改变汇总历史的数学方式，使之可被递推状态表示。
- **Hybrid Attention**：在不同层或路径上组合这些机制。

而 **FlashAttention** 主要回答的是另一个问题：**当我们仍想精确计算既定的 softmax Attention 时，怎样组织 GPU 上的数据读写，避免昂贵地物化完整注意力矩阵？**

它利用分块计算与在线 softmax 等机制减少数据搬运与中间结果存储。对标准全量 Attention，FlashAttention 不会仅因名称中有“Flash”就把数学计算的 `O(T²)` 变成 `O(T)`。

这并不妨碍两个方向结合：滑动窗口可以搭配支持局部注意力的高效内核；全量层也可以采用 FlashAttention 类实现。

用一句话区分：

> **改变 Attention 的连接或数学形式**，与**高效执行已有 Attention 计算**，是两个不同的设计层次。

我们会在后续专门讨论推理引擎的章节里，再完整讲 FlashAttention、PagedAttention、量化和批处理。

## 12. 长上下文不只是一道复杂度题

现在我们似乎已经找到了很多减少成本的方法，是否意味着上下文可以任意拉长？

还不行。**模型能够接收一个很长的输入，与模型能够可靠使用其中的关键信息，是两回事。**

至少还存在三类独立的问题：

**第一，位置表示和训练分布。** 即使底层注意力可以处理更长的序列，RoPE 的频率、位置范围与模型实际训练时见过的上下文长度，也会影响外推行为。修改 RoPE 参数并不会自动保证长文本理解准确。

**第二，信息检索与遗忘。** 局部注意力可能缺少长距离直接连接；稀疏选择器可能漏掉目标；递推状态可能压缩掉某些细节；全量注意力即使允许看全部历史，也不代表模型一定能找到正确证据。

**第三，系统资源与端到端吞吐。** 预填充、逐 Token 解码、KV/状态缓存、并发请求、GPU 内核、通信开销都会影响实际可用性。降低理论 FLOPs，并不直接等于延迟更低。

因此，评价一个长上下文方案至少要分别问：

1. 它能**接收**多长输入？
2. 在目标任务上，它能否**可靠检索、理解并使用**远处信息？
3. 在目标硬件、批量和上下文长度下，它的**实际延迟、吞吐与显存成本**如何？

这三项不能互相替代。

## 13. 回看演化路径：四种机制究竟改变了什么？

我们从一个非常具体的事实开始：新 Token 想使用历史信息，就需要某种形式的历史访问。

```text
标准全量因果 Attention
        │
        │ 上下文变长，显式历史交互越来越贵
        ▼
滑动窗口 Attention
        │
        │ 只看近处，但远处关键证据可能直接不可见
        ▼
局部 + 全局层
        │
        │ 保留长距离路径，但全局层仍然昂贵
        ▼
稀疏 Attention
        │
        │ 有选择地看远处；选择器与遗漏成为新问题
        ▼
线性 / 递推 Attention
        │
        │ 历史压缩为状态；精确检索与信息保真仍有取舍
        ▼
混合架构
        │
        └── 局部处理 / 递推压缩 / 全局或稀疏检索各司其职
```

上图是**教学上的问题推进路径**，并不表示技术严格按这个顺序被发明，也不表示线性注意力总是稀疏注意力的“升级版”。它们在不同条件下可能并行使用，也可能彼此竞争。

| 方法 | 主要思路 | 解决的瓶颈 | 必须付出的代价 |
| --- | --- | --- | --- |
| 全量因果 Attention | 访问全部可见历史 | 保留逐位置直接匹配 | 长序列交互昂贵 |
| 滑动窗口 Attention | 固定只看附近 | 局部层连接数少 | 远处位置不可直接访问 |
| 局部 + 全局层 | 多数局部、少数全局 | 成本和长距离路径之间折中 | 全局层仍贵 |
| 稀疏 Attention | 按规则或内容选历史子集 | 少算不重要的连接 | 选择成本、漏检、内核实现 |
| 线性 / 递推 Attention | 把历史汇总为可更新状态 | 避免逐位置显式历史访问 | 状态压缩与表达取舍 |
| 混合架构 | 不同层选不同机制 | 让不同机制互补 | 训练、缓存、内核更复杂 |

请特别记住这几个**不要混淆**的边界：

- **GQA/MLA** 主要关注每个历史位置需要保存怎样的 K/V 信息；本章讨论的是是否还必须逐位置访问全部历史。
- **Mask 的稀疏**不等于**实际稀疏执行**，需要对应计算内核与缓存管理。
- **固定状态尺寸**不等于**无损记忆**，也不等于在任何任务上优于 softmax Attention。
- **FlashAttention** 是执行层的重要优化，不是所有长上下文算法的统称。

## 14. 下一章：计算架构之后，模型能力又是怎么训练出来的？

到目前为止，我们的技术演化集中在 Transformer 的**内部计算结构**：

- 第一章建立基础 Attention 与 Transformer Block。
- 第二章从稠密 FFN 走向 MoE，降低每 Token 的激活计算比例。
- 第三章从 KV Cache 走向 GQA/MLA，减少重复计算和长期缓存成本。
- 本章从全量历史访问走向局部、稀疏、递推与混合注意力，降低长序列交互成本。

但回到真实任务——修复 `refund.py` 的金额精度问题——即使模型已经能够便宜地阅读大量源码，也不意味着它一定能给出可通过测试的修复。

接下来问题不再是“模型如何更高效地处理信息”，而是：

> **一个只通过下一个 Token 预测训练的模型，为什么能回答问题？它为什么还需要指令微调、偏好优化和强化学习，才能更稳定地遵循要求、解决可验证的任务？**

下一章我们将从预训练目标出发，沿着一次代码修复任务中的失败与反馈，逐步理解 **SFT、偏好优化、DPO、RLHF、GRPO 与基于可验证奖励的强化学习**。这些是训练层面的机制，不应与 MoE、MLA 或注意力内核混为一谈。

---

## 真实源码与论文索引

1. [Hugging Face Transformers `v4.57.1`：Gemma 3 配置](https://github.com/huggingface/transformers/blob/v4.57.1/src/transformers/models/gemma3/configuration_gemma3.py)：`Gemma3TextConfig` 如何生成 `sliding_attention` 与 `full_attention` 的层类型。
2. [Hugging Face Transformers `v4.57.1`：Gemma 3 模型](https://github.com/huggingface/transformers/blob/v4.57.1/src/transformers/models/gemma3/modeling_gemma3.py)：两种因果 Mask 的构建及使用。
3. [Google：What's New in Gemma 3](https://developers.googleblog.com/en/gemma-explained-whats-new-in-gemma-3/)：5 局部 + 1 全局的架构背景。
4. [Hugging Face Transformers `v4.57.1`：Qwen3-Next 配置](https://github.com/huggingface/transformers/blob/v4.57.1/src/transformers/models/qwen3_next/configuration_qwen3_next.py)：`layer_types` 和 `full_attention_interval`。
5. [Hugging Face Transformers `v4.57.1`：Qwen3-Next 模型](https://github.com/huggingface/transformers/blob/v4.57.1/src/transformers/models/qwen3_next/modeling_qwen3_next.py)：`Qwen3NextDecoderLayer`、`Qwen3NextGatedDeltaNet` 与 `Qwen3NextDynamicCache`。
6. [Qwen3-Next 官方技术介绍](https://qwen.ai/blog?id=qwen3-next)：Gated DeltaNet + 全量注意力混合架构的动机与权衡。
7. [Qwen3.8-Flash-Next 公开说明](https://huggingface.co/Qwen/Qwen3.8-Flash-Next)：块级动态稀疏注意力方向的实例。
8. [Transformers are RNNs: Fast Autoregressive Transformers with Linear Attention](https://arxiv.org/abs/2006.16236)：核形式线性注意力与递推表示的经典研究。
9. [FlashAttention: Fast and Memory-Efficient Exact Attention with IO-Awareness](https://arxiv.org/abs/2205.14135)：精确 softmax Attention 的 IO 感知高效实现。

> **实验和源码边界**：正文标注为“教学化简”的 PyTorch 代码仅用于验证局部注意力的可见性、简单稀疏锚点、核注意力的递推等价性及其表达取舍；不是 Gemma 3、Qwen3-Next 或 QSA 的完整实现。真实源码节选固定为 Transformers `v4.57.1`，文中对这些项目的描述限于核对过的层类型、缓存类别和关键数据流。文中没有把理论复杂度当作实际吞吐、也没有声称完成大规模模型的端到端运行。
