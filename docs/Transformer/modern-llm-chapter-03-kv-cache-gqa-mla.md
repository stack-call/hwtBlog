---
id: modern-llm-chapter-03-kv-cache-gqa-mla
title: 第三章：从 MHA 到 GQA、MLA，为什么 KV Cache 会成为推理瓶颈？
sidebar_label: 第三章：KV Cache、GQA 与 MLA
sidebar_position: 4
description: 从最简单的自回归生成出发，逐步理解 KV Cache、MHA/MQA/GQA 的权衡、MLA 的低秩缓存和解耦 RoPE，并对照固定版本的真实源码。
---

# 第三章：从 MHA 到 GQA、MLA，为什么 KV Cache 会成为推理瓶颈？

上一章，我们从普通 FFN 出发，遇到了一个问题：**模型需要更多参数，但不希望每个 Token 都执行全部参数对应的计算。** MoE 的做法是把 FFN 拆成许多专家，并让每个 Token 只选择少数专家。

但 Transformer Block 不只有 FFN，还有 Attention。即使 FFN 的计算成本有所下降，模型在长对话中逐 Token 生成答案时，仍然可能越来越慢、越来越占显存。

这次我们继续使用同一个任务：

> 模型已经读过项目中的 `refund.py`、相关测试和 `pytest` 报错，正在逐字生成退款精度问题的修复说明。

模型可能已经读过几千个 Token，现在只需要再输出下一个 Token。**它为什么还需要处理那么多历史信息？**

这一章从这个问题开始。我们先不引入 GQA 或 MLA，而是从一段最简单、能够真实运行的自回归 Attention 代码出发。

## 1. 为什么生成一个新 Token，还要重新处理历史 Token？

在第一章中，我们已经知道自注意力的核心公式：

```text
Attention(Q, K, V) = softmax( (Q · K^T) / √d_k + M ) · V
```

其中 `M` 是因果掩码，让位置 `t` 只能看到位置 `0, 1, ..., t`。

假设现在模型已经看到：

```text
refund.py 的问题在于金额被提前四舍五入
```

接下来要生成“，”。从最后一个位置的 Query 出发，模型需要与所有可见历史位置的 Key 计算相关性，并使用对应的 Value 汇总信息：

```text
当前 Query（最后一个 Token）
              │
              ├─ 与历史 Key 0 比较 ──┐
              ├─ 与历史 Key 1 比较 ──┤
              ├─ 与历史 Key 2 比较 ──┤ → 注意力加权求和 → 新的隐藏向量
              └─ 与历史 Key ... 比较 ┘
                       ↑
                 读取历史 Value
```

最直接的推理方式是：每生成一个新 Token，都把全部前缀送入模型，重新计算整个前缀的 Q、K、V 和 Attention。

这样做在数学上没有问题，甚至是检查小模型实现的一个好办法。但它浪费了大量重复计算。

在**因果注意力、固定模型参数、推理模式且不改变已有 Token 的位置**等条件下，前面位置的隐藏状态不会因为后面追加了新 Token 而改变。既然历史位置的 K/V 不变，我们为什么还要重新计算它们？

于是，我们首先需要的不是一种新的 Attention，而是一个缓存。

## 2. 最小机制：只计算新 Token，把历史 K/V 保存起来

我们先把位置编码、Dropout 和完整的 Transformer Block 放在一边，只验证缓存是否改变计算结果。

下面是**教学实现**，不是某个模型仓库的源码。只需要安装 PyTorch，不需要模型权重或 API Key。

```python
import math
import torch
from torch import nn


class TinyCausalMHA(nn.Module):
    def __init__(self, hidden_size=8, num_heads=2):
        super().__init__()
        assert hidden_size % num_heads == 0
        self.num_heads = num_heads
        self.head_dim = hidden_size // num_heads
        self.q_proj = nn.Linear(hidden_size, hidden_size, bias=False)
        self.k_proj = nn.Linear(hidden_size, hidden_size, bias=False)
        self.v_proj = nn.Linear(hidden_size, hidden_size, bias=False)
        self.o_proj = nn.Linear(hidden_size, hidden_size, bias=False)

    def project(self, x, layer):
        # [B, T, D] -> [B, H, T, Dh]
        b, t, _ = x.shape
        return layer(x).view(b, t, self.num_heads, self.head_dim).transpose(1, 2)

    def forward_full(self, x):
        q = self.project(x, self.q_proj)
        k = self.project(x, self.k_proj)
        v = self.project(x, self.v_proj)
        t = x.shape[1]
        scores = q @ k.transpose(-2, -1) / math.sqrt(self.head_dim)
        future = torch.triu(torch.ones(t, t, dtype=torch.bool), diagonal=1)
        scores = scores.masked_fill(future.to(x.device), float('-inf'))
        weights = scores.softmax(dim=-1)
        output = weights @ v
        return self.o_proj(output.transpose(1, 2).reshape(x.shape))

    def decode_one(self, x_new, cache=None):
        # x_new 必须是一个新位置：[B, 1, D]
        assert x_new.shape[1] == 1
        q = self.project(x_new, self.q_proj)
        k_new = self.project(x_new, self.k_proj)
        v_new = self.project(x_new, self.v_proj)

        if cache is None:
            k_all, v_all = k_new, v_new
        else:
            old_k, old_v = cache
            k_all = torch.cat([old_k, k_new], dim=2)
            v_all = torch.cat([old_v, v_new], dim=2)

        # 当前新位置可以看到全部缓存及自身，不必再屏蔽未来位置
        scores = q @ k_all.transpose(-2, -1) / math.sqrt(self.head_dim)
        output = scores.softmax(dim=-1) @ v_all
        result = self.o_proj(output.transpose(1, 2).reshape(x_new.shape))
        return result, (k_all, v_all)


torch.manual_seed(0)
model = TinyCausalMHA().eval()
x = torch.randn(1, 5, 8)

with torch.no_grad():
    full = model.forward_full(x)
    cache = None
    pieces = []
    for t in range(x.shape[1]):
        y_t, cache = model.decode_one(x[:, t:t+1], cache)
        pieces.append(y_t)
    incremental = torch.cat(pieces, dim=1)

assert torch.allclose(full, incremental, atol=1e-6)
print('全量与增量计算一致:', torch.allclose(full, incremental, atol=1e-6))
print('K Cache 形状:', tuple(cache[0].shape))
print('V Cache 形状:', tuple(cache[1].shape))
```

预期结果：

```text
全量与增量计算一致: True
K Cache 形状: (1, 2, 5, 4)
V Cache 形状: (1, 2, 5, 4)
```

这是一次重要的设计变化。旧方案每次都重新计算前缀的 K/V；新方案只投影新 Token 的 K/V，再把它们拼接到缓存中。

```text
不使用缓存：
前缀 [0]       → 计算 K0、V0
前缀 [0,1]     → 再算 K0、V0、K1、V1
前缀 [0,1,2]   → 再算 K0、V0、K1、V1、K2、V2

使用 KV Cache：
Token 0 → 计算 K0、V0 → 保存
Token 1 → 计算 K1、V1 → 追加；读取 K0..K1、V0..V1
Token 2 → 计算 K2、V2 → 追加；读取 K0..K2、V0..V2
```

这就是 **KV Cache**：在自回归推理中缓存历史 Token 的 Key 和 Value，使后续位置可以复用它们。这里没有缓存历史 Query，因为生成当前 Token 时需要的是**当前 Query 对历史 K/V 的查询**；历史 Query 对历史位置的注意力结果已经用于计算历史隐藏状态。

注意，缓存没有让历史 Token 消失。它只是把“重复计算历史 K/V”的代价，换成了“保存并反复读取历史 K/V”的代价。

因此，新的问题出现了：**历史越长，KV Cache 到底有多大？**

## 3. KV Cache 的显存究竟花在哪里？

先看标准 Multi-Head Attention（MHA）。假设一层 Attention 使用：

- `B`：同时处理的请求数或批大小；
- `T`：每个请求已缓存的 Token 数（为便于计算，假设相同）；
- `H_kv`：Key/Value 的头数；
- `d_k`、`d_v`：每个 Key/Value 头的维度；
- `s`：每个元素占用的字节数；
- `L`：Transformer 层数。

那么，标准按元素连续存储的 KV Cache 容量约为：

```text
M_KV = B · T · L · H_kv · (d_k + d_v) · s
```

当 `d_k = d_v = d_h` 时：

```text
M_KV = 2 · B · T · L · H_kv · d_h · s
```

它和模型权重是**两块不同的显存开销**。模型权重在请求之间共享，而 KV Cache 随请求数、历史长度持续增长。实际运行时还可能出现分页、分块、对齐、预分配、量化等额外因素，这个式子只算理想化缓存主体。

例如，一套用于**教学比较**的配置：

```python
batch = 1
layers = 32
seq_len = 8192
q_heads = 32
head_dim = 128
bytes_per_element = 2  # BF16 / FP16


def gib(kv_heads):
    nbytes = 2 * batch * layers * seq_len * kv_heads * head_dim * bytes_per_element
    return nbytes / 1024**3

print('MHA:', gib(32), 'GiB')
print('GQA:', gib(8), 'GiB')
print('MQA:', gib(1), 'GiB')
```

结果是：

| KV 头数 | 对应方式 | 仅 KV Cache 主体的理论占用 |
| ---: | --- | ---: |
| 32 | MHA | 4 GiB |
| 8 | GQA | 1 GiB |
| 1 | MQA | 0.125 GiB（128 MiB） |

这些数字不是某个具体模型的实测显存，也不包含其他运行开销。它们说明一个重要事实：**KV 头数不仅是 Attention 的内部细节，还会线性影响每个请求需要保留的历史缓存。**

现在，光靠缓存已经不够了。我们需要减少每个历史 Token 的缓存体积。

## 4. 第一个尝试：为什么每个 Query 头都要有自己的 K/V 头？

回忆 MHA：多个 Query 头从不同角度查询历史信息，各自有对应的 Key 和 Value 头。

```text
MHA（4 个 Q 头、4 个 KV 头）

Q0 ─→ K0, V0
Q1 ─→ K1, V1
Q2 ─→ K2, V2
Q3 ─→ K3, V3
```

在很多任务上，多 Query 头是有价值的。但**不同 Query 头是否一定需要不同的 K/V？**

一个大胆而简单的尝试是：保留多个 Query 头，让它们共享同一组 K/V。

```text
MQA（4 个 Q 头、1 个 KV 头）

Q0 ─┐
Q1 ─┤
Q2 ─┼→ K0, V0
Q3 ─┘
```

这叫 **Multi-Query Attention（MQA）**。它减少了历史 K/V 的存储与读取量，特别针对逐 Token 解码时的内存带宽问题。但共享程度太强也可能限制模型质量，因此不能把 MQA 理解成“无代价压缩”。

自然的折中是：不是所有 Q 头共享一组，而是**每几个 Q 头共享一组**。

```text
GQA（4 个 Q 头、2 个 KV 头）

Q0 ─┐
Q1 ─┴→ K0, V0
Q2 ─┐
Q3 ─┴→ K1, V1
```

这就是 **Grouped-Query Attention（GQA）**。

如果 Query 头数为 `H_q`，KV 头数为 `H_kv`，且 `H_q` 能被 `H_kv` 整除，那么每组 Q 头数为：

```text
G = H_q / H_kv
```

GQA 没有把 Attention 变成 FFN，也没有改变“当前 Q 与历史 K/V 做注意力”这件事。它调整的是 **Q 头与 K/V 头之间的共享关系**。

因此它同时影响：K/V 投影矩阵大小、KV Cache 大小和解码阶段的缓存读带宽。注意力得分计算本身依然要为各个 Query 头产生结果，**GQA 不是把所有 Attention 计算都按相同比例缩小**。

## 5. 用代码看懂 GQA：为什么 `repeat_kv` 不等于重复缓存？

下面沿用原来的张量维度，演示 4 个 Q 头、2 个 KV 头。为了阅读清楚，这里采用 `[B, H, T, D]` 格式。

```python
import torch

# 4 个 Query 头，每头维度 8
q = torch.randn(1, 4, 1, 8)

# 只有 2 个 K/V 头，已经缓存了 5 个 Token
k_cache = torch.randn(1, 2, 5, 8)
v_cache = torch.randn(1, 2, 5, 8)

# 每个 KV 头服务 2 个 Q 头
repeat_factor = q.shape[1] // k_cache.shape[1]

# 教学实现：在参与注意力计算时扩展至 Q 头数
def repeat_kv(x, n_rep):
    b, h, t, d = x.shape
    return x[:, :, None, :, :].expand(b, h, n_rep, t, d).reshape(b, h * n_rep, t, d)

k_for_attention = repeat_kv(k_cache, repeat_factor)
v_for_attention = repeat_kv(v_cache, repeat_factor)

print('缓存中的 K:', tuple(k_cache.shape))
print('计算时的 K:', tuple(k_for_attention.shape))
print('缓存中的 V:', tuple(v_cache.shape))

scores = q @ k_for_attention.transpose(-2, -1) / (8**0.5)
output = scores.softmax(dim=-1) @ v_for_attention
assert output.shape == (1, 4, 1, 8)
```

结果的形状是：

```text
缓存中的 K: (1, 2, 5, 8)
计算时的 K: (1, 4, 5, 8)
缓存中的 V: (1, 2, 5, 8)
```

最重要的是第一行和第二行的区别：**缓存里只有 2 个 KV 头；计算时为了和 4 个 Q 头对齐，可以逻辑上扩展到 4 个头。**

`expand` 本身允许广播视图，但后续 `reshape` 是否物化数据取决于布局和执行路径。更高效的内核可以直接按分组关系读取 K/V，不一定真的复制完整张量。所以，**不要根据注意力计算时的临时形状推断持久化 KV Cache 的大小**。

### 去固定版本的 Llama 源码里验证

源码范围：

- 仓库：[`huggingface/transformers`](https://github.com/huggingface/transformers)
- 固定版本：[`v4.57.1`](https://github.com/huggingface/transformers/tree/v4.57.1)
- 文件：[`src/transformers/models/llama/modeling_llama.py`](https://github.com/huggingface/transformers/blob/v4.57.1/src/transformers/models/llama/modeling_llama.py)
- 关键入口：`LlamaAttention.__init__`、`LlamaAttention.forward`、`eager_attention_forward`、`repeat_kv`

**真实源码节选（只保留一个关键赋值）：**

```python
self.num_key_value_groups = config.num_attention_heads // config.num_key_value_heads
```

这是 `LlamaAttention.__init__` 在模型构造时确定共享组数的地方。Q/K/V 的投影分别根据 `num_attention_heads` 和 `num_key_value_heads` 计算输出维度，因此 **K/V 从投影出来时就是较少的头数**，不是算完全部 K/V 再丢弃。

沿着 `LlamaAttention.forward` 看调用路径，可以读成：

```text
hidden_states
    │
    ├─ q_proj → Query 头
    ├─ k_proj → 较少的 Key 头 ─→ RoPE ─→ past_key_values.update(...)
    └─ v_proj → 较少的 Value 头 ───────→ past_key_values.update(...)
                                                   │
                                                   ▼
                                           Attention Kernel
                                                   │
                                                   ▼
                                                 o_proj
```

其中 `past_key_values.update(...)` 负责按层更新历史缓存，返回包含过去位置的 K/V。随后 `eager_attention_forward` 使用 `repeat_kv` 让 K/V 与 Q 头数对应；其他 Attention 后端可以走各自的实现路径。

**为什么先更新缓存，再参与注意力计算？** 因为当前位置的 Query 不只要看旧缓存，还要看到当前位置自己刚产生的 K/V。

真实代码还会处理 RoPE、位置偏移、Mask、缓存实现差异等问题。这些工程细节不改变刚才的主线：**GQA 缓存的是较少的 K/V 头**。

## 6. GQA 已经压缩了 KV Cache，为什么还需要 MLA？

至此，缓存成本已经显著下降。不过，随着批量请求数、层数和上下文长度增加，GQA 的缓存仍然随 `T` 线性增长：

```text
M_GQA ∝ T · H_kv · (d_k + d_v)
```

如果继续减少 KV 头数，最终就变成 MQA；但这会进一步增加不同 Query 头之间的共享程度。

我们真正想得到的是：**既保留多个 Query 头各自的表达方式，又不为每个历史 Token 长期存下完整、逐头展开的 K/V。**

换一个方向思考：一层线性投影得到的多组 K/V，能不能先写成一个较小的中间表示，需要使用时再还原或变换？

这就是 MLA（Multi-head Latent Attention）的起点。它不是简单地“再减少几个 KV 头”，而是将 K/V 的生成过程写成一个**低秩分解**。

### 6.1 先忽略 RoPE，看最简单的低秩 KV

记当前位置进入注意力的隐藏向量为 `h_t`。普通 Attention 直接投影：

```text
K_t = W_K · h_t
V_t = W_V · h_t
```

现在引入一个较窄的中间向量 `c_t`：

```text
c_t = W_DKV · h_t
```

然后从它恢复用于各个头的 Key 和 Value：

```text
K_t = W_UK · c_t
V_t = W_UV · c_t
```

其中 `W_DKV` 是降维投影，`W_UK`、`W_UV` 是向上投影。

```text
                  ┌─→ W_UK ─→ 各头的 K
h_t ─→ W_DKV ─→ c_t
                  └─→ W_UV ─→ 各头的 V
                ↑
        我们希望长期缓存这里
```

这种设计本身还没有节省缓存。**如果每次都立即展开 K/V，并把展开结果长期存起来，KV Cache 仍然很大。**

真正的关键是：只缓存 `c_t`，而在注意力计算中通过数学变换直接使用它。

### 6.2 为什么不需要先还原所有 Key？

考虑第 `i` 个 Query 头 `q_i`。若它对历史位置 `t` 的 Key 是：

```text
k_(t,i) = W_UK,i · c_t
```

那么两者点积满足（`q_i^T` 表示 `q_i` 的转置）：

```text
q_i^T · k_(t,i)
  = q_i^T · W_UK,i · c_t
  = (W_UK,i^T · q_i)^T · c_t
```

我们可以先把当前的 Query 变换到压缩空间：

```text
q̃_i = W_UK,i^T · q_i
```

然后直接用 `q̃_i` 与缓存的 `c_t` 做点积。

**过去所有位置的 K，不需要为了这次查询逐个展开出来。**

Value 端也有类似关系。设注意力权重为 `α_(i,t)`：

```text
y_i = Σ_t α_(i,t) · v_(t,i)
    = Σ_t α_(i,t) · W_UV,i · c_t
    = W_UV,i · ( Σ_t α_(i,t) · c_t )
```

于是可以先对压缩空间里的缓存做加权求和，再用 `W_UV,i` 变换回输出空间。

这个变换叫做**矩阵吸收（weight absorption）**。它的成立依赖线性运算可以重排；并不是“任意神经网络都能随便交换顺序”。

## 7. 最小实验：展开 K/V 与直接使用压缩缓存，输出相同吗？

这次先不考虑 RoPE，也不引入模型训练，只验证上面的两个代数等式。我们固定一段已经计算好的压缩缓存，以及模拟的各头变换矩阵。

**教学实现：**

```python
import math
import torch

# 使用 float64 减小数值误差，便于验证代数等价
torch.manual_seed(1)
H, T = 4, 6
D_NOPE, D_ROPE, D_VALUE, RANK = 6, 4, 5, 3

# 历史缓存：只有 T 个长度为 RANK 的向量
c_cache = torch.randn(T, RANK, dtype=torch.float64)

# 每个头自己的投影矩阵与当前 Query
w_k = torch.randn(H, D_NOPE, RANK, dtype=torch.float64)
w_v = torch.randn(H, D_VALUE, RANK, dtype=torch.float64)
q_nope = torch.randn(H, D_NOPE, dtype=torch.float64)

# 一条独立的位置路径：历史已经做过位置旋转的 K、当前已旋转的 Q
k_rope_cache = torch.randn(T, D_ROPE, dtype=torch.float64)
q_rope = torch.randn(H, D_ROPE, dtype=torch.float64)
scale = 1.0 / math.sqrt(D_NOPE + D_ROPE)

# 路线 A：直观，但先为每个历史位置展开完整的逐头 K/V
k_full = torch.einsum('hdr,tr->htd', w_k, c_cache)
v_full = torch.einsum('hvr,tr->htv', w_v, c_cache)
scores_a = (
    torch.einsum('hd,htd->ht', q_nope, k_full)
    + torch.einsum('hd,td->ht', q_rope, k_rope_cache)
) * scale
weights_a = scores_a.softmax(dim=-1)
out_a = torch.einsum('ht,htv->hv', weights_a, v_full)

# 路线 B：把 Key 投影吸收到 Query，直接访问较小的 c_cache
q_latent = torch.einsum('hd,hdr->hr', q_nope, w_k)
scores_b = (
    torch.einsum('hr,tr->ht', q_latent, c_cache)
    + torch.einsum('hd,td->ht', q_rope, k_rope_cache)
) * scale
weights_b = scores_b.softmax(dim=-1)
context_latent = torch.einsum('ht,tr->hr', weights_b, c_cache)
out_b = torch.einsum('hr,hvr->hv', context_latent, w_v)

assert torch.allclose(scores_a, scores_b, atol=1e-10)
assert torch.allclose(out_a, out_b, atol=1e-10)
print('注意力分数一致:', torch.allclose(scores_a, scores_b, atol=1e-10))
print('输出一致:', torch.allclose(out_a, out_b, atol=1e-10))
print('压缩缓存形状:', tuple(c_cache.shape))
```

预期输出：

```text
注意力分数一致: True
输出一致: True
压缩缓存形状: (6, 3)
```

这里有一个容易混淆的地方：**这个实验并不是证明低秩分解和任意原始 MHA 参数完全等价。** 如果原先的 `W_K`、`W_V` 并不能由指定秩的矩阵乘积表示，那么强行换成低秩结构可能损失表达能力。我们验证的是：**对于已经采用低秩参数化的同一套权重，展开计算与吸收计算在数学上等价**。

而且这段代码只覆盖一次注意力查询，不包含从隐藏向量生成 `c_t` 的全部投影、归一化、并行执行与真实推理内核。这些内容会通过真实源码补足。

## 8. 但有一个新问题：RoPE 不能直接照搬进低秩压缩

第一章介绍过 RoPE：它对位置 `t` 的 Q/K 使用旋转矩阵 `R_t`。普通 Attention 的带位置信息的 Key 可以表示为：

```text
RoPE(k_t) = R_t · k_t
```

如果我们把低秩 Key 直接用于 RoPE，则会出现：

```text
R_t · W_UK · c_t
```

问题在于 `R_t` 随历史位置 `t` 变化。我们之前把 `W_UK` 吸收到当前 Query 里的做法，依赖 `W_UK` 是一个对所有历史位置都固定的矩阵。

但是现在：

```text
q^T · R_t · W_UK · c_t
```

不能一般性地写成一个**与历史位置无关**的 `q̃`，再直接计算 `q̃^T · c_t`。你必须处理位置相关的旋转，简单的矩阵吸收路径就被打断了。

所以，MLA 还需要一个新设计：**解耦 RoPE（Decoupled RoPE）**。

它把 Key / Query 的内容部分与位置部分分开：

```text
当前位置的 Query
    │
    ├─ 内容部分 q_nope ───────────────┐
    └─ 位置部分 q_rope → RoPE ──────┤
                                      ├─→ 两个分数相加 → Softmax
历史 Token 的缓存                   │
    ├─ c_KV（低秩内容表示）───────────┤
    └─ k_rope（单份位置向量）─────────┘
```

若将第 `i` 个头的 Query 写成 `[q^C_i; q^R_i]`（内容部分 + 位置部分），Key 写成 `[k^C_(t,i); k^R_t]`，那么注意力分数就是：

```text
score_(i,t) = (q_i^C)^T · W_UK,i · c_t + (q_i^R)^T · k_t^R
```

其中 `q_i^R` 与 `k_t^R` 已经带有对应位置的 RoPE 旋转。这样，**内容项可以进行矩阵吸收，位置项单独保留相对位置能力**。

因此，实际需要缓存的不仅是 `c_t`，还有一份用于位置的 `k_t^R`：

```text
每个 Token 的 MLA 缓存元素数 ≈ d_c + d_r
```

这里 `d_c` 为压缩 KV 的维度，`d_r` 为单份位置 Key 的维度。这是接下来读 DeepSeek 源码最重要的线索。

## 9. 对照 DeepSeek-V3：真正的 MLA 缓存在哪里？

先解释选择这个实现的原因：论文 [DeepSeek-V2](https://arxiv.org/abs/2405.04434) 系统介绍了 MLA；[DeepSeek-V3 的公开推理参考代码](https://huggingface.co/deepseek-ai/DeepSeek-V3/blob/c8f49836bd07fd3ec0e8abf2607b564a0b3417ad/inference/model.py) 则能让我们直接看到**直观展开路径**与**吸收权重路径**之间的区别。

这次我们固定模型文件版本，不对变化中的 `main` 分支做隐含假设：

- 仓库：[`deepseek-ai/DeepSeek-V3`](https://huggingface.co/deepseek-ai/DeepSeek-V3)
- 固定 Commit：`c8f49836bd07fd3ec0e8abf2607b564a0b3417ad`
- 文件：[`inference/model.py`](https://huggingface.co/deepseek-ai/DeepSeek-V3/blob/c8f49836bd07fd3ec0e8abf2607b564a0b3417ad/inference/model.py)
- 关键类：`MLA`
- 关键位置：`__init__` 中的缓存定义与 `forward` 中的 `attn_impl` 分支

### 9.1 为什么有两种缓存？

在 `MLA.__init__` 中，代码按照 `attn_impl` 选择缓存形态。阅读时可以抽象成下面的形式：

**教学化简，依据固定版本 `MLA.__init__`：**

```python
if attn_impl == 'naive':
    # 为各个头保存展开后的 Key 和 Value
    k_cache = [batch, seq_len, num_heads, qk_head_dim]
    v_cache = [batch, seq_len, num_heads, v_head_dim]
else:
    # 吸收路径：保存低秩 KV 和一份位置 Key
    kv_cache = [batch, seq_len, kv_lora_rank]
    pe_cache = [batch, seq_len, qk_rope_head_dim]
```

这里的方括号是**形状记号**，不是可直接运行的 Python 缓存初始化代码。

`naive` 分支会先通过 `wkv_b` 恢复多头的 K/V，再存入缓存；它更容易理解，但不体现压缩缓存的主要收益。

另一条通常称作 `absorb` 的路径，真正长期存储的则是：

```text
kv_cache: [B, T, kv_lora_rank]
pe_cache: [B, T, qk_rope_head_dim]
```

### 9.2 为什么它要在 Query 上做一次变换？

沿 `MLA.forward` 的非 `naive` 分支阅读：

```text
输入 x
  │
  ├─ wq 或 wq_a → q_norm → wq_b
  │           ├─ q_nope ─→ 与 W_K 吸收 ─→ 查询压缩 KV Cache
  │           └─ q_pe ───→ RoPE ───────→ 查询位置 Key Cache
  │
  └─ wkv_a ───┬─ c_KV ─→ kv_norm ─────→ 追加到压缩 KV Cache
              └─ k_pe ─→ RoPE ────────→ 追加到位置 Key Cache
```

**对应的核心机制（教学化简，贴近实际运算）：**

```python
# wkv_b 中一部分权重用于生成内容 Key，一部分用于生成 Value
q_latent = transform_query_with_key_weights(q_nope, wkv_b)

scores = (
    q_latent @ cached_latent_kv.transpose(-2, -1)
    + q_rope @ cached_rope_keys.transpose(-2, -1)
)

context_latent = attention_weights @ cached_latent_kv
output = transform_context_with_value_weights(context_latent, wkv_b)
```

从“谁调用、谁消费”的角度看：`Block.forward` 调用 `MLA.forward`；`MLA.forward` 接收当前隐藏向量、起始位置及位置旋转参数，更新本层内部缓存并返回当前层的 Attention 输出；随后上层 Block 将这个输出用于残差连接和后续 FFN / MoE。

这里的 `transform_query_with_key_weights` 和 `transform_context_with_value_weights` **是教学伪函数，不存在于仓库**。真实代码通过 `einsum`、重新解释 `wkv_b` 的权重形状来完成。

### 9.3 还需要看一眼 Hugging Face 的 DeepSeek-V3 实现

同样固定在 Transformers [`v4.57.1`](https://github.com/huggingface/transformers/blob/v4.57.1/src/transformers/models/deepseek_v3/modeling_deepseek_v3.py) 时，`DeepseekV3Attention.forward` 会生成压缩中间量，并将它**展开成逐头的 Key/Value，再通过通用 `past_key_values.update(...)` 更新缓存**。

这是一条便于复用标准 Attention 和缓存接口的实现路线。不能因为类名是 DeepSeek-V3 / MLA，就断言该路径自动具备参考实现 `absorb` 分支所展示的低维 KV 缓存占用。

**同一个模型的数学设计，与某个推理后端实际保存了什么张量，是两个需要分别验证的问题。**

## 10. 把三种路线放在同一张图上

现在才能把整体架构图画出来：

```text
                         当前 Token 的隐藏向量 h_t
                                     │
                         Attention 如何组织 Q/K/V？
                                     │
          ┌──────────────────────────┼─────────────────────────┐
          │                          │                         │
         MHA                        GQA                       MLA
          │                          │                         │
   每个 Q 头有自己的 KV       多个 Q 头共享 KV          K/V 经过共享低秩表示
          │                          │                         │
   缓存所有头的 K/V           缓存较少的 KV 头         缓存 c_KV + 一份位置 Key
          │                          │                         │
          └──────────────────────────┼─────────────────────────┘
                                     │
                    每个新 Token 都查询可见历史信息
                                     │
                           输出当前层注意力结果
```

三者不是“旧的必然淘汰、新的必然更强”的线性排名，而是在能力、内存、带宽、训练复杂度和推理内核支持之间作取舍。

| 机制 | 缓存的核心内容 | 主要收益 | 没解决的事 / 代价 |
| --- | --- | --- | --- |
| MHA + KV Cache | 每个 KV 头的历史 K、V | 避免重复计算历史投影 | KV 缓存较大 |
| MQA + KV Cache | 一组共享的历史 K、V | 缓存和带宽成本低 | 共享很强，可能影响质量 |
| GQA + KV Cache | 少数 KV 头的历史 K、V | 在缓存成本与头部表达间折中 | 缓存仍随长度增长 |
| MLA（吸收路径） | 共享低秩表示和位置 Key | 进一步降低长期 KV 缓存与读取量 | 训练参数化、RoPE 解耦、内核实现更复杂 |

回到之前的教学配置，假设 MLA 使用 `d_c = 512`、`d_r = 64`，理想缓存主体的大小是：

```python
batch, layers, seq_len, element_bytes = 1, 32, 8192, 2
latent_dim, rope_dim = 512, 64
mla_gib = batch * layers * seq_len * (latent_dim + rope_dim) * element_bytes / 1024**3
print('MLA 压缩缓存:', mla_gib, 'GiB')
```

得到：

```text
MLA 压缩缓存: 0.28125 GiB
```

即 **288 MiB**。它与上一节的 MHA/GQA 是**统一批量、层数、历史长度与元素字节数下的教学量纲比较**；MLA 的注意力 Key/Value 维度结构与普通 MHA/GQA 不完全一样，不能将这些数字当成受控实验的质量、速度或真实服务显存对比。

还要记住：压缩 KV Cache **不等于消除了注意力随历史长度增长的访问与打分成本**。标准因果注意力即使在增量解码时，每个新 Token 仍要对可见历史位置计算注意力；如果希望更进一步减少长序列 Attention 的访问或计算，就需要研究不同的注意力模式。

## 11. 回看整条演化路径

我们从一个非常具体的动作开始：模型已经读完项目，现在准备输出下一个 Token。

最先想到的方案是把全部前缀重新送进 Transformer。它可行，但重复计算历史 K/V。于是引入 **KV Cache**，保存历史结果，只处理新增位置。

缓存解决了重复计算，却暴露出新的问题：**历史越长、并发越高，缓存越大，而且每一步都要从显存读取历史。** 于是引入 **MQA/GQA**，让多个 Query 头共享更少的 K/V 头。

但减少 KV 头数仍有共享程度与表达能力之间的折中。于是 **MLA** 改换思路，把每个位置的 K/V 写成共享的低秩表示；通过矩阵吸收避免长期存储展开后的所有 K/V，并通过 **解耦 RoPE** 保留位置相关的注意力分数。

```text
重新计算整个前缀
        ↓  重复计算太多
只算新 Token + KV Cache
        ↓  历史 K/V 越来越大
MQA / GQA：多个 Q 头共享少数 KV 头
        ↓  进一步压缩时，头共享受到权衡限制
MLA：共享低秩 KV + 独立位置 Key
        ↓  即使缓存变小，历史位置仍需被访问
下一章：长上下文与新的 Attention 计算方式
```

**本章最重要的区分是：**

- **KV Cache**：不再重复生成历史位置的 K/V。
- **GQA**：历史位置需要存多少个 K/V 头。
- **MLA**：历史位置到底需要存哪种表示，以及如何不展开完整 K/V 就完成 Attention。

它们解决的是一条因果链上的不同瓶颈，而不是三个彼此无关的缩写。

下一章我们自然要问：**如果上下文从几千 Token 增加到几十万甚至更长，光压缩 KV Cache 还够吗？** 我们将从标准因果注意力对历史序列的访问开始，逐步理解滑动窗口、稀疏注意力、线性注意力与混合架构。

---

## 源码与论文索引

1. [Hugging Face Transformers `v4.57.1` — `LlamaAttention` / `repeat_kv`](https://github.com/huggingface/transformers/blob/v4.57.1/src/transformers/models/llama/modeling_llama.py)：核对 K/V 头数、RoPE、缓存更新、分组查询注意力。
2. [DeepSeek-V3 公开推理代码，固定 Commit — `MLA`](https://huggingface.co/deepseek-ai/DeepSeek-V3/blob/c8f49836bd07fd3ec0e8abf2607b564a0b3417ad/inference/model.py)：核对 `naive` 与吸收路径缓存结构。
3. [Hugging Face Transformers `v4.57.1` — `DeepseekV3Attention`](https://github.com/huggingface/transformers/blob/v4.57.1/src/transformers/models/deepseek_v3/modeling_deepseek_v3.py)：对比标准 Attention/Cache 接口如何处理 MLA 中间表示。
4. [Fast Transformer Decoding: One Write-Head is All You Need](https://arxiv.org/abs/1911.02150)：MQA 的原始研究。
5. [GQA: Training Generalized Multi-Query Transformer Models from Multi-Head Checkpoints](https://arxiv.org/abs/2305.13245)：GQA 在 MHA 和 MQA 之间的折中。
6. [DeepSeek-V2: A Strong, Economical, and Efficient Mixture-of-Experts Language Model](https://arxiv.org/abs/2405.04434)：MLA 和解耦 RoPE 的论文来源。

> **实验边界**：文中的 TinyCausalMHA、GQA 张量示例与 MLA 矩阵吸收示例均为教学实现，不是从真实仓库复制的完整实现。它们验证的是局部数学关系与缓存形状，不声称完成某个预训练模型的真实端到端推理，也不把缓存主体估算当成吞吐量实测。
