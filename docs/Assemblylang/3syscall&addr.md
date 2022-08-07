---
title: 系统调用与操作数寻址
description: 了解汇编程序调用系统资源的方式和操作数的寻址方式
tags: [Assembly]
---

# 系统调用与操作数寻址

## 系统环境
要学习汇编语言基本指令的操作，我们首先需要准备好学习汇编的环境。在这里，我们使用了GNU as和NASM，环境是Ubuntu-22.04-LST-Desktop
## 系统调用
另外，汇编并没有提供相应的输入输出函数，它直接与操作系统接触，因此，在Linux环境下，我们需要使用Linux下的系统调用，即Linux为在其系统上运行的软件提供的访问系统资源的方式，任何Linux程序都只能使用这些系统调用来实现其功能。实际上，很多已经被我们习以为常的C语言标准函数，在Linux平台上的实现都是靠系统调用完成的，所以如果想对系统底层的原理作深入的了解，掌握各种系统调用是初步的要求。  
在32位x86 Linux系统中，可用的系统调用定义在/usr/include/asm/unistd_32.h头文件中。  
在64位x86_64 Linux系统中，可用的系统调用定义在/usr/include/asm/unistd_64.h头文件中。  
我们会使用Linux的系统调用<code>ssize_t write(int fd, const void *buf, size_t count);</code>以及退出程序的系统调用exit，原型可能是<code>void exit(int status);</code>，通过<code>man exit</code>找到的。

### HelloWorld
接下来让我们通过HelloWorld来看一下最基本的程序，并了解一下系统调用

我们经常会用到如下使用系统调用的代码:
### 32位
32位环境下，使用NASM
```js title=helloworld.asm
; Hello World Program - asmtutor.com
; Compile with: nasm -f elf helloworld.asm
; Link with (64 bit systems require elf_i386 option): ld -m elf_i386 helloworld.o -o helloworld
; Run with: ./helloworld
 
SECTION .data
msg     db      'Hello World!', 0Ah
 
SECTION .text
global  _start
 
_start:
 
//highlight-start
    mov     edx, 13
    mov     ecx, msg
    mov     ebx, 1
    mov     eax, 4
    int     80h
//highlight-end

    mov     ebx, 0      ; return 0 status on exit - 'No Errors'
    mov     eax, 1      ; invoke SYS_EXIT (kernel opcode 1)
    int     80h
```
32位环境下，使用GNU as
```js
#compiled by
#~$as -32 hello.S -o hello.o
#~$ld -m elf_i386 hello.o -o hello
#~$./hello
.section .data
output:
	.string "Hello\n\0"

.text
.global _start
_start:

//highlight-start
movl $4, %eax # write对应编号为4
movl $1, %ebx # 第1个参数文件描述符为1，表示stdout
leal output, %ecx # 第2个参数，字符串指针
movl $7, %edx # 第3个参数，写入的字节数
int $0x80
//highlight-end
movl $1, %eax # exit系统调用
xor %edi, %edi # 第1个参数，0，即exit(0)
int $0x80
```
从代码及系统调用原型中我们可以看出，eax，ebx，ecx，edx分别对应四个参数。
:::tip
但是这时候就会有个疑问，32位环境下的函数应该是栈传参，为什么系统调用通过寄存器呢？
一下是我的猜测。我们知道，所谓系统调用是通过系统中断实现的，系统中断的中断号是0x80，也就是说我们的汇编代码执行int 80h后，便触发了0x80中断，进入相应的中断处理函数的入口代码，这个函数是一个汇编函数(C语言函数无法处理)，因此，我们可以约定eax，ebx，ecx，edx分别放什么，因为我们没有使用C语言，自然不用遵循C语言的函数规范。
```js title="一个可能的中断处理函数部分代码,使用NASM编写"
[bits32]
;syscall_table是系统调用中子功能的对应处理函数
extern syscall_table
section .text
global syscall_handler
syscall_handler:
;1 保存上下文环境
push 0

push ds
push es
push fs
push gs
pushad

push 0x80
;2 为系统调用子功能传入参数,这时压入栈了，因为这只是一个入口代码，选择使用哪个系统调用
push edx
push ecx
push ebx

call [syscall_table + eax*4]
add esp, 12
mov [esp + 8*4], eax
jmp intr_exit
```
:::

### 64位
64位环境下，使用GNU as
```js title="hello.S"
#compiled by
#~$gcc hello.S -o hello
#~$./hello
.section .data #rodata?
msg:
	.string "Hello\n"

.text
.globl main
main:
	pushq %rax # for stack alignment

// highlight-start
	movq $1, %rax 
	movq $1, %rdi
	leaq msg(%rip), %rsi
	movq $6, %rdx
	syscall
 // highlight-end

	# exit(0)
	movq $60, %rax
	xor %rdi, %rdi
	syscall

```
64位环境下NASM
```js title=hello.asm

;nasm -f elf64 hello.asm -o hello.o
;ld hello.o -o hello
;./hello
SECTION .data                     ; Section for variable definitions

; Variable containing a string that has a line break and is null-terminated
string:   DB "Hello\n",0ah

; Variable that calculates the value of an expression to determine the
; length, in bytes, of the variable "stringLiteral" by subtracting the
; starting memory address of the variable from the current memory address
lenString:        EQU ($ - string)

SECTION .text                     ; Section for instructions
global _start                      ; Make the label "_main"
                                  ; available to the linker as an
                                   ; entry point for the program
_start:                            ; Label for program entry

//highlight-start
mov rax, 1
mov rdi, 1
lea rsi, [rel string] ;或者直接[string],还可以直接mov rsi, string
mov rdx, lenString
syscall
//highlight-end

mov rax, 60                       ; Set the system call for exit
xor rdi, rdi                      ; Set the return value in rdi (0)
syscall                           ; Issue the kernel interrupt
```
同时我们也可以看出32位和64位汇编其中许多不同的细节，以及nasm和as语法的不同。


:::tip
* 对于GNU as来说，我们知道它是gcc编译的一个阶段，我们可以通过<code>gcc -S xxx.c -o xxx.S</code>来生成C语言代码对应的汇编代码，同样，我们可以使用<code>gcc xxx.S</code>来编译汇编代码，利用这种方法，我们可以在汇编中使用C函数(使用C语言库)，甚至使用C的预处理器(后缀名一定为.S)。对应汇编代码，我们同样可以直接使用as进行编译，如果在汇编代码中使用C语言库，我建议直接使用gcc进行编译，因为如果直接使用as的话是很难同C语言库进行链接的，但是使用gcc它会直接帮你完成所有操作。  
* 同时，gcc和as识别的入口函数是不同的，在同问as的代码中，我们可以发现，as直接编译下的入口函数为_start，gcc编译的入口函数为main。(或者是链接器？再研究一下)
* 对于 int 80h 和syscall  
SYSCALL指令是INT 0X80的64位版本，syscall是在x86-64上进入内核模式的默认方式。
但是仍然可以在64位代码中使用INT 0X80，但是强烈不建议使用了。
int 0x80是调用系统调用的传统方法，应避免使用。
而且syscall具有比int 0x80更少的开销。
int 0x80是选择eax作为系统调用编号，ebx，ecx，edx，esi，edi和ebp传递参数。
Syscall是使用rdi,rsi,rdx,rcx，r8,r9传递传输。
:::
## 数据类型(仅了解)
在了解如何获取操作数之前，我们先了解一下在机器级层面都有什么类型的数据

### 基本数据类型
 bytes, words, doublewords, quadwords, and double quadwords ，即字节，字，双字，四字，双四字。
:::tip
数据边界对齐：为了提升效率，在数据结构中，尤其时栈中需要自然边界对其以减少处理器访问数据所花费的指令周期。
:::
### 数字数据类型
即有符号或无符号整数，浮点数和相关指令。

### 指针数据类型
指针就是内存中的地址值  
32位模式中，定义了两种指针：**近指针**和**远指针**，近指针是同一个段内的32-bit或16-bit偏移。(**待补充**)
远指针是一个逻辑地址，必须显示指定段选择子
![](https://img-blog.csdnimg.cn/9ff10dda6c964104acc38cef8354b3e5.png)
64位模式下  
近指针是64位的，并且等于有效地址(偏移offset)，因为此时位平坦模型，CS，DS，ES，SS为0，线性地址空间没有分段。
远指针有以下三种方式：
* 当操作数为32位，16位段选择子，16位偏移
* 当操作数位32位，16位段选择子，32位偏移
* 当操作数位63位，16位段选择子，64位偏移
![](https://img-blog.csdnimg.cn/e1b2a77520744d9cadfac36a68b3edb9.png)
### 位字段数据类型(bit field)
### 字符串数据类型
### PACKED SIMD 数据类型
......待补充
## 操作数寻址
由于是从16位体系拓展到32位，64位的，因此Intel用术语"字(word)"表示16位数据，称32位数据为"双字(double word)"，称64位数据位"四字(quad word)"。  
在汇编语言中，大多数指令都有一个或多个操作数(operand)，指示操作中要使用的源数据的位置以及要放置结果的位置。源数据可以以常数(immediate)形式给出，或是从寄存器或内存中给出。结果可以放在寄存器或内存中。  
根据《Intel开发者手册》卷一3.7节操作数寻址(OPERAND ADDRESSING)中提到的寻址方式，  
**源操作数**可能在:
* 指令自己(立即数操作数)
* 寄存器
* 内存位置
* I/O端口

当指令返回值到**目的操作数**中，可能返回到：

* 寄存器
* 内存位置
* I/O端口
### 立即数
在ATT格式汇编中，立即数为数值前加上'$'前缀，而在Intel中则没有这种要求。立即数操作数就是把常数操作数编码在指令中，例如ADD EAX, 14
### 寄存器
下图是《深入理解计算机系统》中的一张通用寄存器的图表。即我们再执行环境那一节中提到的64位环境中的16个通用寄存器。
![](https://img-blog.csdnimg.cn/545f925b9ef34bbdaf98652aff68eb96.png)
:::tip
根据《Intel开发者手册》中的卷一3.4节，基本程序执行寄存器可以分为四组：
* **通用寄存器(General-purpose)** 可以用来存储操作数或者指针
* **段寄存器(Segment)** 存储段选择子
* **程序状态和控制寄存器(EFLAGS)** 报告程序的状态和允许处理器的应用级控制
* **指令指针寄存器(EIP, instruction pointer)** 寄存器存储要执行的下一条指令的地址

在32位环境下，通用寄存器只有8个(EAX, EBX, ECX, EDX, ESI, EDI, ESP, EBP)，在64位下又扩展了许多寄存器，上图画的很清楚。
:::
接下来是《Intel开发者手册》卷一3.7.2节中列举的详细的寄存器操作数：
>Register operands in 64-bit mode can be any of the following:
* 64-bit general-purpose registers (RAX, RBX, RCX, RDX, RSI, RDI, RSP, RBP, or R8-R15)
* 32-bit general-purpose registers (EAX, EBX, ECX, EDX, ESI, EDI, ESP, EBP, or R8D-R15D)
* 16-bit general-purpose registers (AX, BX, CX, DX, SI, DI, SP, BP, or R8W-R15W)
* 8-bit general-purpose registers: AL, BL, CL, DL, SIL, DIL, SPL, BPL, and R8B-R15B are available using REX
prefixes; AL, BL, CL, DL, AH, BH, CH, DH are available without using REX prefixes.
* Segment registers (CS, DS, SS, ES, FS, and GS)
* RFLAGS register
* x87 FPU registers (ST0 through ST7, status word, control word, tag word, data operand pointer, and instruction
pointer)
* MMX registers (MM0 through MM7)
* XMM registers (XMM0 through XMM15) and the MXCSR register
* Control registers (CR0, CR2, CR3, CR4, and CR8) and system table pointer registers (GDTR, LDTR, IDTR, and
task register)
* Debug registers (DR0, DR1, DR2, DR3, DR6, and DR7)
* MSR registers
* RDX:RAX register pair representing a 128-bit operand

### 内存
在内存中的寻址方式是比较多的。  
#### 内存操作数格式
在32位情况下，内存中的源和目的操作数通过段选择子和一个偏移来访问。段选择子指定包含操作数的段，偏移指定操作数的线性地址或有效地址，偏移可能是32位(m16:32)或16位(m16:16)  
在64位情况下，基本情况和上面相同，但是偏移也可能位64位。
:::info
在x86中，程序中的地址是由段选择子和偏移两部分构成的**逻辑地址**，这种逻辑地址并不能直接用于访问物理地址，需要使用x86架构中的地址变换机制将它转换为物理内存地址。  
有人可能会疑问为什么要这么麻烦，主要是为了保护和地址变换。保护是限制程序的访问范围，即通过段选择子来控制其可以访问的段范围，通过这种方式可以将逻辑地址转换为一个**线性地址**。此时若没有开启分页，则线性地址就是**物理地址**；若开启分页，则通过地址变换，也就是虚拟分页机制转换到相应的物理地址。(分段机制是必须的，但是现代操作系统没有按照Intel设计时所设想的大量使用分段，因为效率?反而是依靠分页机制实现功能？我猜的)
:::

#### 指定段选择子和偏移
通过内存操作数格式，我们可以看出，要想指定一个地址，我们需要指定段选择子和一个偏移。

#### 指定段选择子
段选择子可以被隐式或显式指定。我们在操作系统上写汇编时不需要关注这些，但是在裸机系统上编写汇编时，就需要有很多这方面的了解。毕竟在操作系统中写汇编有很多事情都由操作系统管理了，汇编没有办法发挥其全部功力。  
在隐式指定的情况下，也即默认情况，遵循如下规则：
![](https://img-blog.csdnimg.cn/69bc9b6c56574ce28a0c250f3e027958.png)
从图中可以看出，对于所有的指令取指，都默认使用CS指向的代码段，对于所有push，pop和使用ESP或EBP的指令都默认为SS段，使用栈数据段等等。  
当向内存存储数据或从内存中加载数据，默认的DS段可以覆盖为其他段，在大多数汇编器中，段覆盖使用符号":"，例如，EBX是段内偏移指针，EAX中是数据，那么<code>MOV [EBX], EAX</code>是把EAX放到DS:[EBX]，此时，我们使用<code>MOV ES:[EBX], EAX</code> 修改其默认段。  
但是如下情况不能使用段覆盖：
* 指令取指令必须是从代码段
* 字符串操作指令中的目的字符串必须储存在由ES指向的数据段中
* PUSH和POP操作必须由SS段指向
:::info
Segment selectors can also be specified explicitly as part of a 48-bit far pointer in memory. Here, the first doubleword
in memory contains the offset and the next word contains the segment selector.
:::

在64为情况下，分段机制被禁止，整个地址空间被视为平坦的64-bit线性地址空间，CS, DS, ES, SS被视为0，例外的FS和GS可以在一些线性地址计算中使用。
**不过好像就算是32为操作系统也不会使用其分段机制，同样是使用平坦模型，主要使用虚拟分页机制**
:::tip
此处的offset和displacement都有偏移的意思，offset指的是总的偏移，而displacement是变址计算中的相对偏移。即<code>Offset = Base + (Index * Scale) + Displacement</code>
:::
##### 指定偏移(offset)
偏移可以直接由位移(displacement)或通过以下部分的组合计算获得：
* **Displacement** —— 8-，16-或 32-bit的值
* **Base** ——通用寄存器中的一个值
* **Index** ——通用寄存器中的一个值
* **Scale factor** ——2，4或8

下图罗列了可能的计算公式
![](https://img-blog.csdnimg.cn/fc3583c89cab4ec7b9facbbc643a7680.png)
:::tip
* ESP不能作为索引寄存器
* 当ESP或EBP作为基址寄存器，SS段是默认的段，在其他情况下，DS指向的段是默认的段
:::

偏移可能的组合方式
* **偏移(Displacement)** 单独的偏移不用计算，可以编码在指令中(见指令格式)，因此也被称为绝对地址或静态地址，常用于访问静态已分配的标量操作数
* **基址(Base)** 基址单独表示操作数的偏移，因为Base存储在寄存器中，因此其可以用于变量或数据结构的动态存储
* **基址+偏移(Base + Displacement)** 有以下两种独特用法
  * As an index into an array when the element size is not 2, 4, or 8 bytes—The displacement component
encodes the static offset to the beginning of the array. The base register holds the results of a calculation
to determine the offset to a specific element within the array.
  * To access a field of a record: the base register holds the address of the beginning of the record, while the
displacement is a static offset to the field.  
  * 这种组合的重要的特殊情况是函数的传参。即EBP作为基址。
* **索引 * 比例系数) + 偏移 |(Index ∗ Scale) + Displacement ** 这种寻址方式提供了访问静态数组的有效方式，当数组大小为2，4或8字节时，偏移表示数组的起始地址，索引表示数组下标。
* **基址+索引+偏移(Base + Index + Displacement)** 可以用来实现二维数组
* **基址+(索引*比例系数)+偏移|Base + (Index ∗ Scale) + Displacement** 同样可以实现二维数组的实现
:::info
其中偏移中的标量操作数用的是scalar operand，那么这个到底是什么呢？
以下是从stack overflow中查到的
<https://stackoverflow.com/questions/57808011/the-scalar-type-additionnal-in-c>
:::
##### 在64位中指定偏移
* **Displacement** —— 8-，16-或 32-bit的值
* **Base** ——64-bit通用寄存器中的一个值
* **Index** ——64-bit通用寄存器中的一个值
* **Scale factor** ——2，4或8
* **RIP + Displacement** 在64-bit中，RIP-relative寻址使用一个有符号32位偏移来计算有效地址，通过使用这种技术，一个有效地址可以通过在RIP指向的下一条指令地址的基础上加上一个位移，即RIP的64位地址加上一个有符号拓展的32位偏移值寻址

:::tip
* 64位中指定Displacement仍然最多32位而不是64位
* RIP相对寻址在64位汇编中使用比较频繁
:::

### I/O端口
处理器支持65536个8-bit I/O端口，暂且不用管