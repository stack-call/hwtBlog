---
title: ATT与Intel汇编格式
description: 了解两种汇编语言格式的不同
tags: [Assembly]
---
|Intel语法    | AT&T语法   |
|    ---      | ---       |
|   |    |
## 操作数方向
|Intel语法    | AT&T语法   |
|    ---      | ---       |
|  目的操作数在前，源操作数在后 |  源操作数在前，目的操作数在后  |

## 寄存器
|Intel语法    | AT&T语法   |
|    ---      | ---       |
|  无前缀    |  加上前缀"%"  |

## 立即数
|Intel语法    | AT&T语法   |
|    ---      | ---       |
| 无前缀    | 加上前缀"$"   |
|十六进制和二进制加上后缀"h"和"b"|十六进制加上前缀"0x"|?



## 内存操作数
Base(Reg) + Index(Reg)*Scale(Imm) + Displacement(Imm)   

|Intel语法    | AT&T语法   |
|    ---      | ---       |
| Reg<sub>segment</sub>:[Reg<sub>base</sub> + Reg<sub>index</sub>*Imm<sub>scale</sub> + Imm<sub>displacement</sub>] | Reg<sub>segment</sub>:Imm<sub>displacement</sub>(%Reg<sub>base</sub> , %Reg<sub>index</sub> , Imm<sub>scale</sub>)  |
|简写为 segreg:[base+index*scale+displacement] |简写为 %segreg:displacement(base,index,scale)|
:::tip
注意此时ATT语法立即数Imm不需要带$，带$是为了区分地址和内存值，因此在可以区分的时候是不需要的。这一切由编译器区分。
:::

## 指令操作数字长
|Intel语法    | AT&T语法   |
|    ---      | ---       |
| 使用"byte ptr"或"word ptr"表示  | 根据操作数大小加上下表相应后缀 |

![](https://img-blog.csdnimg.cn/b894fe909c574b318a9db4637bc55a21.png)
## 特殊指令
### 绝对转移和绝对调用(jump/call)
|Intel语法    | AT&T语法   |
|    ---      | ---       |
| -  |  操作数加上前缀"*"  |

### 远转移和远调用(jmp/call)
|Intel语法    | AT&T语法   |
|    ---      | ---       |
|  jmp far section:offset |  ljmp $section:$offset  |
|  call far section:offset |   lcall $section:$offset |
### 远程返回(ret)
|Intel语法    | AT&T语法   |
|    ---      | ---       |
|ret far stack_adjust|lret stack_adjust|

:::tip
Linux使用32位线性地址，段基址为0，段长为全部线性空间。因此偏移计算为<code>disp + base + index*scale</code>
:::

源操作数在前，目的操作数在后
Intel
label即为地址本身
访问地址对应的值需要[label]，还需要dword等单独的大小指示
num就表示数字
reg就表示相应寄存器


ATT
label为地址对应的值，后缀即表明了对应值的大小
$label为地址本身(jmp?)
$num表示数字
%reg表示相应寄存器



操作系统中的非常见代码
```js
# The xv6 kernel starts executing in this file. This file is linked with
# the kernel C code, so it can refer to kernel symbols such as main().
# The boot block (bootasm.S and bootmain.c) jumps to entry below.

#include "asm.h"
#include "memlayout.h"
#include "mmu.h"
#include "param.h"

.p2align 2
.text

# By convention, the _start symbol specifies the ELF entry point.
# Since we haven't set up virtual memory yet, our entry point is
# the physical address of 'entry'.

.globl _start
_start = V2P_WO(entry)

# Entering xv6 on boot processor, with paging off.
.globl entry
entry:
  # Turn on page size extension for 4Mbyte pages
  movl    %cr4, %eax
  orl     $(CR4_PSE), %eax
  movl    %eax, %cr4
  # Set page directory
  movl    $(V2P_WO(entrypgdir)), %eax # entrypgdir在main.c里
  movl    %eax, %cr3
  # Turn on paging.
  movl    %cr0, %eax
  orl     $(CR0_PG|CR0_WP), %eax
  movl    %eax, %cr0

  # Set up the stack pointer.
  movl $(stack + KSTACKSIZE), %esp

  # Jump to main(), and switch to executing at
  # high addresses. The indirect call is needed because
  # the assembler produces a PC-relative instruction
  # for a direct jump.
  mov $main, %eax # 因为paging已经设置好，所以main方法是虚拟地址是可行的
  jmp *%eax

.comm stack, KSTACKSIZE # stack标签占据KSTACKSIZE大小的空间
```

```js
#include "asm.h"
#include "memlayout.h"
#include "mmu.h"

# %cs=0 %ip=7c00.

.code16                       # Assemble for 16-bit mode
.globl start                  # 声明start对外开放，这样Makefile中才可以使用
start:
  cli                         # BIOS enabled interrupts;

  xorw    %ax,%ax             # Set %ax to zero
  movw    %ax,%ds             # -> Data Segment
  movw    %ax,%es             # -> Extra Segment
  movw    %ax,%ss             # -> Stack Segment


  inb $0x92, %al
  orb $0x2, %al
  outb %al, $0x92

  lgdt    gdtdesc
  movl    %cr0, %eax
  orl     $CR0_PE, %eax
  movl    %eax, %cr0

  ljmp    $(SEG_KCODE<<3), $start32

.code32  # Tell assembler to generate 32-bit code now.
start32:
  # Set up the protected-mode data segment registers
  movw    $(SEG_KDATA<<3), %ax    # Our data segment selector，SEG_KDATA值为2，表示指向gdt下标为2的segment descriptor
  movw    %ax, %ds                # -> DS: Data Segment
  movw    %ax, %es                # -> ES: Extra Segment
  movw    %ax, %ss                # -> SS: Stack Segment
  movw    $0, %ax                 # Zero segments not ready for use，gdt的0下标指向的数据为null
  movw    %ax, %fs                # -> FS
  movw    %ax, %gs                # -> GS

  movl    $start, %esp

  call    bootmain

  # 下面的逻辑不应该被执行，除非代码bug了
  # If bootmain returns (it shouldn't), trigger a Bochs
  # breakpoint if running under Bochs, then loop.
  movw    $0x8a00, %ax            # 0x8a00 -> port 0x8a00
  movw    %ax, %dx
  outw    %ax, %dx
  movw    $0x8ae0, %ax            # 0x8ae0 -> port 0x8a00
  outw    %ax, %dx
spin:
  jmp     spin

.p2align 2                                # force 4 byte alignment
gdt:
  SEG_NULLASM                             # null seg，gdt的地一个descriptor不用
  SEG_ASM(STA_X|STA_R, 0x0, 0xffffffff)   # code seg
  SEG_ASM(STA_W, 0x0, 0xffffffff)         # data seg

gdtdesc:
  .word   (gdtdesc - gdt - 1)             # sizeof(gdt) - 1，gdt的地址范围是[base, base+limit]，包含最后一个字节，所以减1
  .long   gdt                             # address gdt

```
其中Linux从2.4.0版本开始全面使用as汇编而不是部分使用as86和ld86，因此，2.4.x版本往后的引导代码汇编都有很大的参考价值。