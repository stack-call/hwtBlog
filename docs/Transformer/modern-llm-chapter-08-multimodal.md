---
id: modern-llm-chapter-08-multimodal
title: "第八章：从文本到图像、语音与视频——多模态如何进入 Transformer？"
sidebar_label: "第八章：多模态模型"
sidebar_position: 9
description: "从只有文本 Embedding 的语言模型出发，逐步理解图像 Patch、视觉编码器、多模态投影、图文对齐、音频编码器和视频时空位置，并阅读 LLaVA、Qwen2-Audio、Qwen2-VL 的真实源码。"
---

# 第八章：从文本到图像、语音与视频——多模态如何进入 Transformer？

上一章，我们研究了一个大模型请求怎样在推理引擎中运行：Prefill、Decode、动态批处理、KV Cache，以及怎样减少 GPU 计算和显存访问。现在假设用户发来一个新的代码修复任务：

> 这是退款功能失败时的截图。请结合报错日志，定位代码问题。

模型已经能够读日志、读源码，也可以在推理时多次尝试。但它能直接理解截图里的按钮、错误弹窗和数值吗？如果再给它一段用户录屏，或者客服口述的故障描述呢？

普通语言模型的输入是文本 Token ID。截图是像素数组，声音是一串采样点，视频则是随时间变化的画面。这几种输入根本不是同一种数据。

**本章要解决的唯一主问题是：怎样让原本处理文本向量的 Transformer，真正利用图像、语音和视频里的信息？**

我们先让模型读一张非常小的图片，再一步步扩展。不急着把“多模态模型”画成一个巨大的模块图。

> [!NOTE]
> 本章所有数学表达均采用纯文本与 `text` 围栏，不依赖数学插件。教学代码仅需要 PyTorch，不下载模型、不需要 API Key。文中“实际运行结果”是对教学实验的验证，不代表训练出了能看懂图片的模型。

## 一、为什么不能直接把图片交给语言模型？

先回到我们已经熟悉的文本模型。

```text
用户文本
  ↓
Tokenizer
  ↓
Token IDs                   例如：[13, 25, 81]
  ↓
Embedding                   [batch, text_len, hidden_size]
  ↓
Transformer Blocks
  ↓
预测下一个文本 Token
```

假设隐藏维度 `hidden_size = 8`，输入 3 个文本 Token，那么进入 Transformer 的数据形状就是：

```text
[batch, seq_len, hidden_size] = [1, 3, 8]
```

Transformer 的注意力只知道怎样处理这种向量序列，并不知道“图片”或“音频”是什么。如果把原始截图的像素数据直接接上去，马上就有两个问题。

第一，数据形状不同。一张 RGB 图片通常是：

```text
[batch, channel, height, width] = [1, 3, 224, 224]
```

第二，数据含义不同。文本 Token ID 是词表索引；像素数值是颜色强度。把每个像素强行当成一个词表 ID，不仅会破坏空间结构，也不会让现有文本 Embedding 自动理解颜色、边缘或对象。

于是我们真正需要的不是“把图片变成文字”，而是：

**先把图片编码成一组连续向量，再让这些向量能够进入语言模型。**

这件事可以分成两个还没有实现的要求：

1. 图片必须先变成序列，才能接入序列模型。
2. 图片序列的特征维度必须能与语言模型的隐藏维度衔接。

我们先只解决第一个问题。

## 二、先把图片切成 Patch：二维图像怎样变成序列？

假设这张截图只有 `4 × 4` 个像素，而且是单通道灰度图：

```text
 0   1   2   3
 4   5   6   7
 8   9  10  11
12  13  14  15
```

如果每个 Patch 的尺寸是 `2 × 2`，我们可以把图像切成四块：

```text
Patch 0       Patch 1
0  1          2  3
4  5          6  7

Patch 2       Patch 3
8   9         10  11
12 13         14  15
```

从原来的二维排列中，我们拿到了一个长度为 4 的 Patch 序列。

一般情况下，对于高为 `H`、宽为 `W` 的图片，如果 Patch 边长为 `P`，并且 `H` 和 `W` 均能被 `P` 整除，那么：

```text
Patch 数量 N = (H / P) × (W / P)
每个 Patch 的原始数值数目 = C × P × P
```

注意：**Patch 是图像的一块区域；Visual Token 是经过处理后进入模型的视觉序列元素。** 两者不一定一对一。现代模型还可能进行裁剪、合并、池化或动态分辨率处理。

### 实验 1：用 PyTorch 真正切开一张图片

下面是**教学化简代码**，只展示 Patch 切分，没有学习任何视觉语义。

```python
import torch

image = torch.arange(16, dtype=torch.float32).reshape(1, 1, 4, 4)

# 沿高度和宽度每隔 2 个像素取一个 2x2 Patch
patches = image.unfold(2, 2, 2).unfold(3, 2, 2)

# [batch, channel, grid_h, grid_w, patch_h, patch_w]
patches = patches.permute(0, 2, 3, 1, 4, 5)
patches = patches.reshape(1, 4, 4)

print("图片形状:", tuple(image.shape))
print("Patch 形状:", tuple(patches.shape))
print("Patch 0:", patches[0, 0].tolist())
print("Patch 3:", patches[0, 3].tolist())

assert patches.shape == (1, 4, 4)
```

预期输出：

```text
图片形状: (1, 1, 4, 4)
Patch 形状: (1, 4, 4)
Patch 0: [0.0, 1.0, 4.0, 5.0]
Patch 3: [10.0, 11.0, 14.0, 15.0]
```

现在我们已经有了序列，但每个元素只是四个像素数值，还不是语言模型能直接使用的隐藏状态。

我们需要把每个 Patch 映射成一个特征向量。

## 三、从 Patch 到视觉特征：为什么需要 Vision Encoder？

最简单的做法，是让每个 Patch 经过一个可学习的线性变换：

```text
原始 Patch 数值
   ↓
Linear / Patch Embedding
   ↓
Patch 向量
```

对于真实 RGB 图像，`Patch Embedding` 可以用展平后做线性投影实现，也常常用卷积实现。卷积核尺寸和步长都等于 Patch 大小时，它可以一次完成不重叠 Patch 的投影。

但是这里又出现了一个限制：如果只是把各个 Patch 投影出去，Patch 之间并没有充分交流。一张错误弹窗可能分布在多个 Patch 里，文字、边框和背景要一起理解。

这时才有必要在 Patch Embedding 后加入处理图像的网络，也就是**视觉编码器（Vision Encoder）**。

一种典型的视觉编码器是 Vision Transformer（ViT）：

```text
图片
  ↓
Patch Embedding
  ↓
视觉位置表示
  ↓
多层视觉 Transformer
  ↓
一串上下文化视觉特征
```

这里要区分两层含义：

- Patch Embedding 主要负责把像素块变成向量。
- Vision Encoder 进一步让不同图像区域交换信息，学习适合视觉任务的表示。

这有点像文本模型里“Token Embedding 与后续 Transformer Block”之间的区别。

不过我们先不去从头训练 ViT。对于多模态大模型来说，一个常见而且更容易理解的方案是：**拿一个已经训练好的视觉编码器，再把它接到语言模型前面。**

新的计算路径变成：

```text
图片
  ↓
Vision Encoder
  ↓
Visual Features                 [batch, visual_len, vision_dim]
```

于是第二个问题暴露了：`vision_dim` 不一定等于语言模型的 `hidden_size`。

## 四、为什么还要一个 Projector？

假设视觉编码器输出：

```text
[batch, visual_len, vision_dim] = [1, 4, 6]
```

但语言模型期望：

```text
[batch, seq_len, hidden_size] = [1, 任意长度, 8]
```

即使都是“向量序列”，最后一维也不一样。把 `[1, 4, 6]` 直接拼到 `[1, 3, 8]` 上，张量操作会失败。

最小解决方案是增加一个可学习映射：

```text
Visual Features          [1, 4, 6]
      ↓
Projector: 6 → 8
      ↓
Projected Features       [1, 4, 8]
```

它被称作**多模态投影器（Multimodal Projector）**。可以是一个 Linear，也可以是带激活函数的 MLP；具体选择取决于模型设计。

这里的“对齐”主要是指把视觉特征变换到语言模型可接收的表示空间，并通过训练让语言模型学会使用它。**维度相同不代表模型自动懂图片。**

### 真实源码对照：LLaVA 的投影器

固定版本：Hugging Face Transformers `v4.57.1`。

仓库路径：

`src/transformers/models/llava/modeling_llava.py`

类：`LlavaMultiModalProjector`

版本链接：[查看固定版本源码](https://github.com/huggingface/transformers/blob/v4.57.1/src/transformers/models/llava/modeling_llava.py)

在该版本中，投影器包含 `linear_1`、激活函数 `act` 和 `linear_2`。为了只保留与当前推导有关的部分，这里不整段复制源码，而将它写成**教学化简**：

```python
import torch


class SimpleProjector(torch.nn.Module):
    def __init__(self, vision_dim, text_dim):
        super().__init__()
        self.fc1 = torch.nn.Linear(vision_dim, text_dim)
        self.act = torch.nn.GELU()
        self.fc2 = torch.nn.Linear(text_dim, text_dim)

    def forward(self, features):
        return self.fc2(self.act(self.fc1(features)))
```

实际源码在 `LlavaModel.get_image_features()` 中先调用 `vision_tower`，选取视觉网络隐藏状态，然后调用 `multi_modal_projector`。调用者是多模态模型的 `forward()`；它最终要把投影后的视觉特征放到语言模型输入中。

至此，我们已经解决了图片怎样变成能输入语言模型的特征。可是用户的问题是“看这张截图”，其中同时包含图片和文字。**两种输入究竟在哪里汇合？**

## 五、图文融合：图片不是普通文字 Token

先看用户真实输入的逻辑结构：

```text
请看这张截图：[图片] 为什么退款失败？
```

这里的“[图片]”只是人类阅读时的记号，它不表示图片本身已经被 Tokenizer 变成了文字。

一种直接的融合方式是：处理文本时预留视觉内容的位置，然后用真正的视觉特征替换那些位置的 Embedding。

```text
文本 Token IDs
    ↓
文本 Embedding
    ↓
在视觉占位处预留位置
    ↓
把这些位置换成 Projector 输出的视觉特征
    ↓
统一的 Embedding 序列
    ↓
原来的语言模型
```

例如，我们让占位 ID 为 `30`，文字为 `1` 和 `2`，共有 4 个视觉特征，那么处理器准备的序列可以是：

```text
Token IDs:       [1, 30, 30, 30, 30, 2]
                      ↑   ↑   ↑   ↑
                      4 个视觉特征位置

Embedding 后:    [T, I0, I1, I2, I3, T]
```

这里 `I0` 到 `I3` 都是连续视觉特征，而不是“红色”“按钮”这种由 Tokenizer 编出的自然语言单词。

### 实验 2：真的把视觉特征接入文本序列

下面是**教学化简代码**。我们故意用很小的图片和随机权重，只验证“形状能衔接，内容按位置注入”。

```python
import torch
from torch import nn


torch.manual_seed(7)

image = torch.randn(1, 3, 4, 4)

# 卷积同时承担 Patch Embedding 的角色
patch_embed = nn.Conv2d(3, 6, kernel_size=2, stride=2)
visual_features = patch_embed(image)                   # [1, 6, 2, 2]
visual_features = visual_features.flatten(2).transpose(1, 2)

projector = nn.Linear(6, 8)
visual_features = projector(visual_features)           # [1, 4, 8]

embedding = nn.Embedding(32, 8)
image_token_id = 30
input_ids = torch.tensor([[1, 30, 30, 30, 30, 2]])
text_embeds = embedding(input_ids)


def merge_visual_features(input_ids, inputs_embeds, features, marker_id):
    marker = input_ids.eq(marker_id)
    feature_rows = features.reshape(-1, features.shape[-1])
    count = int(marker.sum().item())

    if count != feature_rows.shape[0]:
        raise ValueError(
            f"占位数量 {count} 与视觉特征数量 {feature_rows.shape[0]} 不一致"
        )

    merged = inputs_embeds.clone()
    merged[marker] = feature_rows
    return merged


merged = merge_visual_features(
    input_ids, text_embeds, visual_features, image_token_id
)

print("视觉特征:", tuple(visual_features.shape))
print("融合结果:", tuple(merged.shape))
print("首尾文本位置保持不变:", bool(torch.equal(merged[:, [0, 5]], text_embeds[:, [0, 5]])))
print("图像位置被替换:", bool(torch.equal(merged[:, 1:5], visual_features)))

assert merged.shape == (1, 6, 8)
assert torch.equal(merged[:, 1:5], visual_features)
```

预期输出：

```text
视觉特征: (1, 4, 8)
融合结果: (1, 6, 8)
首尾文本位置保持不变: True
图像位置被替换: True
```

注意这个实验没有训练视觉编码器，没有训练投影器，更没有训练语言模型。它只是打通了输入的数据流。

### 真实源码对照：LLaVA 在哪里替换占位？

还是固定版本 `v4.57.1`、同一个文件：

`src/transformers/models/llava/modeling_llava.py`

关键函数：`LlavaModel.forward()` 和 `LlavaModel.get_placeholder_mask()`。

**真实源码关键行节选**（省略上下文）：

```python
special_image_mask = self.get_placeholder_mask(
    input_ids, inputs_embeds=inputs_embeds, image_features=image_features
)
inputs_embeds = inputs_embeds.masked_scatter(special_image_mask, image_features)
```

这几行为什么重要？

- `get_placeholder_mask()` 找出图像占位位置，并检查占位数量与视觉特征数量是否匹配。
- `masked_scatter()` 把视觉特征放进这些位置，不是把像素值传给 Tokenizer。
- 随后的 `language_model` 使用合成后的 `inputs_embeds` 继续运行。

源码还会根据视觉特征层选择策略筛选隐藏状态，常见配置会去掉视觉编码器的某些特殊位置。具体视觉 Token 数量由处理器、视觉编码器和模型配置共同决定，因此不能假定“每张图片只对应一个特殊 Token”。

真正看懂这一段后，多模态模型就不再神秘了：**语言模型的主干仍然可以是 Decoder-only Transformer，变化主要发生在输入侧和训练方式上。**

## 六、占位长度为什么必须匹配？单张图片究竟占多少 Token？

我们刚才用 4 个视觉特征，文本就预留 4 个位置。可是如果只写一个图像占位，会怎样？

```text
处理器预留： [T, I, T]                  只有 1 个视觉位置
视觉编码器： [I0, I1, I2, I3]           实际有 4 个特征
```

二者无法逐位置替换。模型不是可以“随便多塞几个向量”的黑箱：输入长度、Attention Mask、位置编号和缓存位置都需要同步。

### 实验 3：复现占位数不匹配的问题

在上一个实验后继续运行：

```python
bad_ids = torch.tensor([[1, 30, 2]])
bad_embeds = embedding(bad_ids)

try:
    merge_visual_features(
        bad_ids, bad_embeds, visual_features, image_token_id
    )
except ValueError as error:
    print(type(error).__name__ + ":", error)
```

预期输出：

```text
ValueError: 占位数量 1 与视觉特征数量 4 不一致
```

真实 LLaVA 的 `get_placeholder_mask()` 也有对应的数量检查。

更进一步，当图片分辨率变高，最朴素的 Patch 切法还会让视觉 Token 数量迅速增加。

例如，Patch 边长固定为 14：

```text
224 × 224 图片：16 × 16 = 256 个 Patch
448 × 448 图片：32 × 32 = 1024 个 Patch
```

图片宽高各扩大一倍，Patch 数量扩大四倍。**这意味着多模态问题又回到了第三、四、七章研究过的长上下文与推理成本问题。**

现代系统因此可能使用动态分辨率、裁剪、Patch 合并、池化或其他 Token 压缩策略。不过这些机制会改变细节，不能把上面的 256/1024 当成所有视觉模型的真实 Token 数量。

目前我们只解决了“怎么接进来”，还没有解决“怎么学会理解”。

## 七、让模型会看图：只有 Projector 为什么还不够？

把随机视觉向量塞进语言模型，语言模型不会突然获得 OCR 或识图能力。

假设代码修复任务中，我们提供这样的训练样本：

```text
输入：
  文本：请说明截图中的报错。
  图片：退款页面截图

目标输出：
  错误提示为“退款金额超过可退金额”。
```

这时模型必须学会让视觉特征与答案建立联系。一个容易理解的训练方案是：

1. 从已训练的视觉编码器与语言模型开始。
2. 首先学习连接它们的投影器，让图像特征更适合语言模型使用。
3. 使用图文指令数据进行后续训练；是否训练视觉编码器、语言模型的一部分或全部参数，取决于具体方案。

LLaVA 的原始工作使用视觉编码器与语言模型之间的连接，并利用视觉指令数据开展训练。它不是简单地“加个 Linear 就自然能看图”。

从数据流角度，训练仍然可以沿用熟悉的下一 Token 预测目标：

```text
图片 + 用户问题
       ↓
多模态输入 Embedding
       ↓
语言模型
       ↓
逐 Token 预测参考回答
       ↓
根据目标答案计算训练损失
```

一般需要注意，用户提示中的文本位置、图像占位位置和模型生成的目标答案位置，可能采用不同的 Loss Mask。常见指令微调做法是主要监督回答部分，而不是要求模型预测原始像素。

这又回答了一个容易混淆的问题：

**多模态训练不等于把每个模态都变成普通单词。模型学习的是不同模态特征与目标任务之间的可用关系。**

接下来把任务从静态截图扩展到一个更复杂的输入：用户发来的是故障录屏。

## 八、视频只是很多张图片吗？

最简单的想法，是从录屏中抽出几帧，对每一帧都使用刚才的图像通路。

```text
视频
  ↓
抽取多个画面
  ↓
逐帧 Vision Encoder
  ↓
按时间拼接视觉特征
  ↓
语言模型
```

这确实是一个有用的最小方案。每帧都能复用我们刚刚学会的 Patch、Vision Encoder 和 Projector。

但想象这样一段退款录屏：

- 第 1 秒，用户输入退款金额 50。
- 第 3 秒，用户点击确认。
- 第 4 秒，弹出“金额不能超过余额”。

如果把所有画面当成一堆没有顺序的图片，模型就可能看见按钮、弹窗和数值，却不知道**先发生了什么，后发生了什么**。

因此视频除了空间信息，还需要时间信息。

### 实验 4：把视频整理成具有时间顺序的特征序列

这是**教学化简代码**，它按顺序处理 3 帧，每帧有 4 个 Patch；这里只验证形状与索引，不训练时序理解能力。

```python
import torch
from torch import nn


torch.manual_seed(11)
frames = torch.randn(1, 3, 3, 4, 4)  # [batch, time, channel, h, w]

patch_embed = nn.Conv2d(3, 6, kernel_size=2, stride=2)
projector = nn.Linear(6, 8)

batch, time, channels, height, width = frames.shape
flat_frames = frames.reshape(batch * time, channels, height, width)

features = patch_embed(flat_frames)              # [3, 6, 2, 2]
features = features.flatten(2).transpose(1, 2)  # [3, 4, 6]
features = projector(features)                   # [3, 4, 8]

video_features = features.reshape(batch, time * 4, 8)

# 为每个视觉特征记录：来自哪一帧、该帧哪个 Patch
positions = [(t, p) for t in range(time) for p in range(4)]

print("视频特征:", tuple(video_features.shape))
print("前 5 个 (帧编号, Patch 编号):", positions[:5])

assert video_features.shape == (1, 12, 8)
```

预期输出：

```text
视频特征: (1, 12, 8)
前 5 个 (帧编号, Patch 编号): [(0, 0), (0, 1), (0, 2), (0, 3), (1, 0)]
```

仅有顺序索引不保证模型掌握时序关系，但它至少告诉我们：视觉特征不再只是平面的网格，还具有时间维度。

### 真实源码对照：Qwen2-VL 的多维位置

固定版本：Hugging Face Transformers `v4.57.1`。

仓库路径：`src/transformers/models/qwen2_vl/modeling_qwen2_vl.py`

关键类与函数：`Qwen2VLModel`、`get_rope_index()`。

[查看固定版本源码](https://github.com/huggingface/transformers/blob/v4.57.1/src/transformers/models/qwen2_vl/modeling_qwen2_vl.py)

这个实现的一个关键思想是为视觉内容构造对应**时间、高度和宽度**的位置信息（Multi-dimensional RoPE，简称 M-RoPE）。

源码注释举过一个小例子：3 个时间位置，每个时间位置有 `2 × 2` 个视觉位置。总共 12 个视觉位置，它们的三组索引可以理解成：

```text
时间 t : 0 0 0 0 | 1 1 1 1 | 2 2 2 2
高度 h : 0 0 1 1 | 0 0 1 1 | 0 0 1 1
宽度 w : 0 1 0 1 | 0 1 0 1 | 0 1 0 1
```

这三组索引分别刻画“第几段时间”“画面的哪一行”“画面的哪一列”。它们在实际 RoPE 实现中不是简单相加的三个整数，而是作用在旋转位置编码的不同维度组上。

这里正好接上第一章的 RoPE：文本主要处理一维位置，而图像和视频还需要表达空间与时间结构。

需要注意，这里展示的是 Qwen2-VL 固定版本的源码思想；不同模型可能采用帧时间戳、不同采样策略、局部/全局视觉编码和其他位置设计，不能认为所有视频模型都使用同样的 M-RoPE。

视频路径到这里已经能理解：复用图片的表示方式，但需要补上跨帧和时间结构。现在再换一种输入：用户直接口述问题。

## 九、声音为什么不能直接当成文本？

假设用户说了一句：

> 我点了退款按钮，但系统一直提示余额不足。

最容易想到的方案是先用语音识别模型把声音转成文本，再交给现有语言模型。

```text
声音
  ↓
语音识别（ASR）
  ↓
文字
  ↓
语言模型
```

这个方案简单、实用，在很多场景里足够好。因为对于“提取用户说了什么”这样的任务，文字已经保留了大部分必要信息。

但它也有明显的限制：

- 语音中的语气、背景声、说话人变化可能没有被转写出来。
- 如果用户说“听一下这个警报声”，根本不一定存在需要转写的词语。
- 多人对话中的时间重叠和停顿，也可能对任务有意义。

所以我们需要区分两条路线：

1. **ASR → 文本模型**：先得到转写文字，沿用纯文本语言模型。
2. **音频编码器 → 语言模型**：保留可学习的音频特征，让语言模型利用声音本身的信息。

第二条路线与刚才的视觉通路非常相似，但具体输入不再是像素。

## 十、从波形到音频特征：时间上的 Patch

数字音频最原始的表示是按采样率记录的振幅数值。例如，单声道 16 kHz 音频，一秒钟有 16000 个采样点。

```text
波形样本
[0.01, 0.04, -0.02, ...]
```

与图片一样，直接把每个采样点送给语言模型通常不合适。这样会产生非常长的序列，而且缺少适合识别声音模式的表示。

一个常见中间步骤是把声音切成短时间窗，计算频率相关特征，比如 Mel 频谱，再由专门的音频编码器加工：

```text
波形
  ↓
短时窗口 / 频谱特征
  ↓
Audio Encoder
  ↓
音频特征序列
  ↓
Projector
  ↓
语言模型隐藏维度
```

这里音频的时间窗，有点像图像的 Patch：它们都是先把非文本数据组织成模型可处理的局部单元。但图像的局部结构是空间邻域，音频的局部结构是时间与频率，不能简单混为一谈。

### 实验 5：一秒声音怎样变成特征序列？

下面是**教学化简代码**：使用 STFT 得到幅度谱，再做一次线性维度映射。**这里的 STFT 并不是 Mel 频谱，线性层也不等于训练好的 Audio Encoder。**

```python
import math
import torch
from torch import nn


sample_rate = 16000
seconds = 1
t = torch.arange(sample_rate * seconds) / sample_rate

# 用 440 Hz 正弦波模拟一秒钟音频
waveform = torch.sin(2 * math.pi * 440 * t)

spectrum = torch.stft(
    waveform,
    n_fft=400,
    hop_length=160,
    win_length=400,
    window=torch.hann_window(400),
    return_complex=True,
)

# [频率 bin, 时间帧] -> [时间帧, 频率 bin]
frames = spectrum.abs().transpose(0, 1)
projector = nn.Linear(201, 8)
audio_features = projector(frames)

print("采样点:", waveform.numel())
print("幅度谱:", tuple(frames.shape))
print("投影后:", tuple(audio_features.shape))

assert audio_features.shape == (101, 8)
```

预期输出：

```text
采样点: 16000
幅度谱: (101, 201)
投影后: (101, 8)
```

这里 101 帧来自 PyTorch `torch.stft()` 在默认居中填充下的长度计算；实际音频处理器会有不同的帧率、填充、裁剪与下采样逻辑。

这个实验说明：即使只有一秒音频，进入模型的也可能是一个不短的特征序列。长语音依然会带来输入长度和计算成本问题。

### 真实源码对照：Qwen2-Audio 如何连接音频和文本？

固定版本：Hugging Face Transformers `v4.57.1`。

仓库路径：`src/transformers/models/qwen2_audio/modeling_qwen2_audio.py`

相关类：`Qwen2AudioForConditionalGeneration`。

[查看固定版本源码](https://github.com/huggingface/transformers/blob/v4.57.1/src/transformers/models/qwen2_audio/modeling_qwen2_audio.py)

在该版本的前向执行路径中，存在一条清晰的数据流。下面是**真实源码关键行节选**：

```python
audio_outputs = self.audio_tower(input_features, attention_mask=audio_attention_mask)
selected_audio_feature = audio_outputs.last_hidden_state
audio_features = self.multi_modal_projector(selected_audio_feature)
```

前面两个组件分别扮演与 Vision Encoder、Projector 相似的角色：

- `audio_tower` 负责加工已经预处理的音频特征。
- `multi_modal_projector` 将其映射为后续语言模型能使用的隐藏表示。

后续代码还要处理音频特征长度与输入占位的位置关系，再把音频表示融合到语言模型输入。

因此，在这一路径中，声音并不必须先生成一份自然语言转写结果，才能与语言模型交互。

当然，这不等于“所有多模态模型都把音频 Embedding 简单拼到文字前面”。不同架构还可能使用 Cross-Attention、独立音频解码器、流式分块与实时同步机制。

## 十一、为什么有些模型拼接，有些模型使用 Cross-Attention？

我们已经看到了最直观的一种方案：把不同模态转换到语言模型隐藏空间，作为同一序列里的若干位置。

这种方式通常称作**序列拼接或占位替换式融合**。对于 Decoder-only 模型，它可以较大程度复用已有语言模型主干。

```text
[文本 Embedding | 视觉 Embedding | 文本 Embedding]
                        ↓
                  Self-Attention
                        ↓
                    文本回答
```

但当输入特别长，例如数分钟视频或高频率音频时，直接把所有视觉与音频特征展开进同一条序列可能带来很大的 Prefill 与缓存压力。

这时另一个设计选择才变得有吸引力：不要把全部模态信息都插进主序列，而是保留一个外部特征序列，让语言模型通过 **Cross-Attention（交叉注意力）** 按需读取。

```text
文本状态 ——产生 Query——┐
                        ├── Cross-Attention → 更新文本状态
图像 / 音频特征 → Key/Value┘
```

这与第一章 Encoder–Decoder 结构中的交叉注意力有类似之处：Query 来自正在生成的文本，Key/Value 来自另一组被编码的输入特征。

二者不是简单的“旧方案与新方案”关系，而是不同设计选择：

| 方案 | 好处 | 代价或限制 |
| --- | --- | --- |
| 拼接视觉/音频 Embedding | 能复用语言模型自注意力路径；图文信息在统一序列内交互 | 输入可能变长，计算和缓存成本上升 |
| Cross-Attention 读取外部特征 | 文本主序列可以不直接容纳全部模态 Token；模态信息独立编码 | 需要额外的交叉注意力计算和相应训练设计 |
| 先转写/描述为文本 | 最容易复用纯文本模型与工具链 | 会丢失文本没表达的视觉、声音或时间信息 |

不能说任意一种融合方式必然更好：效果取决于训练数据、模型容量、输入长度、任务与推理部署约束。

## 十二、多模态让推理能力自动增强了吗？

回到代码修复任务。

现在模型可能接收到：

```text
问题描述（文本）
错误弹窗截图（图像）
操作录像（视频）
用户口述（音频）
```

有了多模态输入，模型就可能获得文字以外的证据，例如截图中的金额、用户点击的先后顺序、音频中的警报声。

但**获得更多输入，不等于自动获得更好的推理或更高的事实可靠性**。

例如截图里写着“当前余额 35”，用户申请退款 50。模型需要从图片中准确识别两个数字，结合退款规则推导结果；如果视觉识别错了数字，后面的语言推理再流畅也可能错。

更重要的是，多模态模型仍然不等于可以直接操作真实世界。它即使能看懂截图，也不会因为多了一条 Vision Encoder，就自动拥有读取仓库、修改文件、运行测试的权限。

因此，至少需要区分三个层次：

```text
多模态感知：从文本、图像、声音、视频获取证据
       ↓
模型推理：根据证据提出解释、计划或下一步动作
       ↓
外部执行：通过明确授权的工具调用修改文件、运行测试
```

上一章讨论推理引擎，是让计算执行得更高效；这一章讨论多模态，是让模型获得更多类型的信息；真正连接外部工具和反馈循环，还涉及独立的 Agent Runtime 设计。

我们不在这里提前展开 Agent 的工具协议、运行循环与状态管理，因为读者还没有遇到其具体实现问题。

## 十三、回看：整个多模态架构是怎样长出来的？

现在再画全图，它已经不再是一组陌生名词。每个模块都有明确来由：

```text
                         用户输入
                            │
           ┌────────────────┼──────────────────┐
           │                │                  │
         文本             图像              音频 / 视频
           │                │                  │
       Tokenizer       Image Patches       时间窗 / 视频帧
           │                │                  │
       Embedding       Vision Encoder      Audio / Video Encoder
           │                │                  │
           │           Visual Projector     Modal Projector
           │                │                  │
           └────────────────┴──────────────────┘
                            │
                  融合后的输入表示
                    （一种设计）
                            │
                     Language Model
                            │
                     自回归文本生成
```

从头看这个演化过程：

1. 语言模型只会接收文本向量，于是我们先寻找非文本到向量序列的映射。
2. 图片是二维的，于是用 Patch 和视觉编码器形成上下文化视觉特征。
3. 视觉维度不匹配，于是加入 Projector。
4. 图文需要在同一请求中联合处理，于是引入占位对齐与特征融合。
5. 拼起来不代表会理解，于是需要图文任务数据和相应训练。
6. 视频多了时间关系，于是需要时间、空间位置与跨帧处理。
7. 音频不是文字，于是加入音频编码器，并区分转写与直接音频感知。
8. 输入序列可能过长，于是还要考虑 Token 压缩和融合架构的成本取舍。

**多模态的核心不在于让 Transformer 学会读取某种文件格式，而在于构建能保留不同模态有用信息的表示，并通过训练让模型真正利用这些表示完成任务。**

## 十四、源码阅读清单与验证边界

以下是本章使用的固定版本。它们是实际项目代码入口，而非虚构的统一多模态接口。

| 模型 | 仓库及版本 | 关键入口 | 对应解决的问题 |
| --- | --- | --- | --- |
| LLaVA | Transformers `v4.57.1` | `LlavaMultiModalProjector`、`LlavaModel.get_image_features()`、`LlavaModel.forward()` | 图片特征如何投影并放入语言模型 |
| Qwen2-VL | Transformers `v4.57.1` | `Qwen2VLModel.get_rope_index()` | 图像和视频的时空位置如何表示 |
| Qwen2-Audio | Transformers `v4.57.1` | `Qwen2AudioForConditionalGeneration` 的前向音频路径 | 音频编码器如何通过投影与文本模型衔接 |

对应的固定版本源码：

- [LLaVA：modeling_llava.py](https://github.com/huggingface/transformers/blob/v4.57.1/src/transformers/models/llava/modeling_llava.py)
- [Qwen2-VL：modeling_qwen2_vl.py](https://github.com/huggingface/transformers/blob/v4.57.1/src/transformers/models/qwen2_vl/modeling_qwen2_vl.py)
- [Qwen2-Audio：modeling_qwen2_audio.py](https://github.com/huggingface/transformers/blob/v4.57.1/src/transformers/models/qwen2_audio/modeling_qwen2_audio.py)
- [LLaVA 论文：Visual Instruction Tuning](https://arxiv.org/abs/2304.08485)

本章实验验证的是张量形状、Patch 顺序、特征占位、时序索引以及音频帧化。它们没有加载这些真实模型的权重，也没有证明教学代码具备 OCR、语音识别或视频理解能力。真实项目的 API 和权重使用方式应以固定版本及对应模型配置为准。

## 十五、下一步：模型理解了信息，怎样真正完成任务？

回到我们贯穿全书的退款功能修复任务。

现在模型已经具备一种可能的能力：既读懂退款源码，又看见错误截图，还能从录屏中观察用户操作。

但如果用户真正要求的是：

> 找到退款精度问题，修改代码，执行测试；如果仍然失败，就继续查原因。

那么仅有多模态输入仍然不够。模型需要在一轮推理之外，实际执行读文件、改文件、运行测试，并根据结果决定后续动作。

这便引出了一个与模型架构不同的新问题：

**怎样把语言模型变成能够与工具、外部环境和反馈循环协作的 Agent，而不是只生成下一段文字？**

它会成为下一章的起点。
