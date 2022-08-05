---
title: 指令格式
description: 了解基本指令的格式是了解汇编语言的关键
tags: [Assembly]
---

# 引言
我们知道，汇编的格式是对机器码的抽象，是基于 Intel 64 和 IA-32处理器 上运行的指令集(instruction set) ，汇编语言编译器是汇编语言和指令集及其机器码的中间层。因此，了解指令的格式是深入了解汇编语言的关键。

# 指令格式
下图为基本指令格式的图表
![](https://img-blog.csdnimg.cn/fcec3db5bf73416ebcd1363f48036b75.png)

## 指令前缀(Instruction Prefixes)
指令前缀可以分为4组，每组由一些前缀代码组成。对于每个指令，必须包含四组前缀中的至少一个四组中的前缀代码才是有效的指令前缀。

* Group 1
  —— Lock和repeat前缀  
  * LOCK前缀以F0H编码
  * REPNE/REPNZ 前缀以F2H编码。Repeat-Not-Zero前缀仅仅用在字符串(string)和输入输出(input/output)指令。
  * REP 或 REPE/REPZ以F3H编码，repeat前缀仅仅用在字符串(string)和输入输出(input/output)指令。  
  
  —— BND前缀
* 待补充。。。
## 操作码(Opcodes)
操作码的主要部分(即图中的Opcode位置)可以为1，2或3字节。有时可能在ModR/M位置有附加的3位操作码字段(field)。这些字段定义了操作的direction(待定)，偏移的大小，寄存器编码，条件代码，或者符号拓展。
待补充。。。
## ModR/M和SIB字节
许多引用内存中操作数的指令都有一个指定寻址方式的字节，即ModR/M字节。ModR/M包含3个字段：
* <code>mod</code>字段与<code>r/m</code>字段结合使用组成32种可能的值：8个寄存器和24个寻址模式。
* <code>reg/opcode</code>字段指定一个寄存器数或前面提到的操作码的3位拓展信息。<code>reg/opcode</code>字段的含义由opcode部分指定。
* <code>r/m</code>字段可以指定一个寄存器作为操作数或与<code>mod</code>字段结合来编码一种寻址方式。有时，某些<code>mod</code>和<code>r/m</code>字段的结合用来为某些指令展现操作码信息。？
  
某些时候，<code>ModR/M</code>字节可能需要另外的寻址字节(SIB字节)。base-plus-index和scale-plus-index的寻址方式需要SIB字节。<code>SIB</code>字节包含以下字段：  
* <code>scale</code>字段指定比例因子
* <code>index</code>指定作为index寄存器的寄存器数
* <code>base</code>指定作为base寄存器的寄存器数
## 偏移和立即数字节
一些寻址方式需要一个立即数偏移跟在ModR/M后面(或者如果有SIB的话再SIB字节后面)

## ModR/M和SIB字节组成的寻址模式
Table 2-1展示了16-bit下由ModR/M字节指定的寻址方式；32-bit的寻址方式由Table 2-2指定；Table 2-3展示了由SIB字节指定的寻址方式。
在Table 2-1和Table 2-2中，Effective Address列列举了32个可以通过使用<code>Mod</code>和<code>R/M</code>字段作为指令第一个操作数的有效地址。前24个提供了指定一个内存位置的方式，后8种(Mod=11B)指定了使用通用寄存器和MMX技术或XMM寄存器的方式。  
其中的<code>Mod</code>和<code>R/M</code>列展示了为了获取第一列的Effective Address而对应的Mod和R/M二进制编码。  
现在让我们看一下第七行，即<code>REG=</code>，这一行指定了当3-bit的<code>Reg/Opcode</code>字段被用来指定为第二个操作数时相应的二进制编码，第二个操作数必定为通用寄存器，MXX技术或XMM寄存器。第一行到第五行列出了可能同表中值结合的寄存器，具体是哪个寄存器被使用取决于opcode字节的操作字长属性。  
如果指令不需要第二个操作数， 那么Reg/Opcode字段会被用为opcode的拓展，这个拓展显示在第六行<code>/digit(Opcode)</code>，以十进制显示。  
Table 2-1和Table 2-2的表格主题内容<code>“Value of ModR/M Byte (in Hexadecimal)</code>包含了8列32行共256个<code>ModR/M</code>对应的十六进制值。  
具体的计算规则如下：
![](https://img-blog.csdnimg.cn/60659f42628940bdaac84e36463ea80b.png)
以ModR/M值为C8H为例，其中Mod为11，R/M为000，REG或/digit(Opcode)为001，则组合后的值为0xC8
![](https://img-blog.csdnimg.cn/f898a9b0efef4212be751882a196799b.png)
![](https://img-blog.csdnimg.cn/3b9557eead114d60a70aae3943f85623.png)
Table 2-3给出了SIB字节256种可能的组合值  
作为基址的通用寄存器在表的最上面三行，Index列指定了变址寄存器，SS指定了比例因子，从最左面的Scaled Index列我们可以看出一个SS对应一个倍数，如SS=01对应*2倍，这也是为什么比例系数只能为1，2，4或8的原因。
![](https://img-blog.csdnimg.cn/82d3fc735df340bfb23ffab8e2ca66db.png)
在64-bit操作系统下，汇编语言的运用与32-bit操作系统下有着相当一部分的不同，其主要原因就是处理器所支持的指令不同，如RIP-relative的寻址方式等等。
:::note
好像因为CPU自身结构问题，不能直接两个操作数全部是内存中的数据，有时间求证一下
:::
## 示例分析
参考链接
<https://www.cnblogs.com/dongc/p/10727457.html>
### 示例1
add dword ptr ds:[esi+ecx*4],eax的机器码  
接下来，我们就选择汇编中使用频率非常高的add指令来分析一下汇编指令与二进制机器码的转换  
下图为ADD指令参考，可以看到，图中给出了对应的Opcode及相应的指令含义等等。
![](https://img-blog.csdnimg.cn/0177f0ffa09641a3ae4633c8d3bb192a.png)
我们先看汇编指令，dword表示内存指针中的数据大小，因为我们只有汇编数据在内存中的首地址，而不知道其大小，那么我们需要指定其大小，在Intel格式的汇编中就是使用这种方式指定，ptr表示为指针，ds表示数据段。可以看出，这个指令的含义就是把寄存器的值加到内存地址中(顺序是反过来看的)，对应  
![](https://img-blog.csdnimg.cn/b1ffa82fdcac45cba9a828b883015fae.png)，可以看到其中<code>ADD r/m32, r32</code>和我们要分析的指令格式相同。通过01/r可以看出，没有前缀，opcode为01h，/r表示有ModR/M结构，且其中的Reg/Opcode表示第二个寄存器操作数，即eax，查Table 2-2可知ModR/M为04H([-][-]表示使用SIB结构，即用SIB表示内存操作数)，对应SIB，因为是4倍，所以SS为10，index为00，变址寄存器为esi，则r32为110，故SIB=0x8E  
所以机器码为01 04 8E
![](https://img-blog.csdnimg.cn/162ce682d7694277b179f9edccde182c.png)
