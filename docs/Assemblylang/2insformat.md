---
title: 指令格式
description: 了解基本指令的格式是了解汇编语言的关键
tags: [Assembly]
---

# 概述 
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
