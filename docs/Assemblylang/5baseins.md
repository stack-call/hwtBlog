---
title: 基本指令
description: 了解汇编程序的基本指令
tags: [Assembly]
---
# 基本指令

## 规范
L: Literal，立即数   
M: Memory，内存操作数  
R: Register，寄存器  
这里我们的指令格式采取Intel格式，即先写目标操作数，再写源操作数，因为Intel开发者手册上的指令集就算按照这种顺序，ATT语法只需要反过来就好了
## 数据移动指令
<code>mov M/R, L/M/R</code>

要求：   
* 两个操作数大小必须相同(可以通过一些命令改变)
* 两个操作数不能全是内存操作数
* 指令寄存器不能为目标操作数

那么这些要求是从哪里来的呢，我们在《Intel开发者手册》卷二中便可以发现其根本原因
![](https://img-blog.csdnimg.cn/9339a53f1c514584ad9e67f20e36effc.png)
![](https://img-blog.csdnimg.cn/f73ac23e30534da59d0e360d97bf123f.png)
![](https://img-blog.csdnimg.cn/8d4802d5feaa42b79062a0e223b4abac.png)

我们可以发现，仅仅mov指令就是有三种，但是使用汇编语言时我们感受不到其中的不同，因为汇编器帮助我们做了转换。这仅仅是作个示例，之后的很多例子不会再一一贴出其相应的指令。  
<code>xchg M/R, M/R</code>  

## 算术运算指令
1. 操作数加1或减1  
* <code>inc M/R</code>
* <code>dec M/R</code>

2. 把源操作数加到目标操作数，或从目标操作数减去源操作数
* <code>add M/R, L/M/R</code>  
* <code>sub M/R, L/M/R</code>

3. 取负值，即求补
* <code>neg M/R</code>

4. mul为无符号整数乘法，即乘数为操作数，被乘数为相应大小ax寄存器，积放在相应大小dx和ax中，即*dx:*ax，高位在*dx中，低位在*ax中。
![](https://img-blog.csdnimg.cn/2115493edfb845c58b635a8eee778744.png)
* <code>mul M/R</code>
:::tip
执行完乘法看CPU标志位，对于无符号乘法，若乘积高半段为0，则Carry Flag(CF)为0， 否则为1。对于带符号乘法，如果乘积高半段的每一个二进制都与低半段的最高有效位相同，则CF为0，否则为1。
:::

带符号整数相乘要通过imul。与mul一样，该指令也有单操作数的版本，在这种情况下，但是其还有双操作数和三操作数的版本。双操作数版本和ADD和SUB相同，双操作数和三操作数都是把结果保存到寄存器中。
* <code>imul M/R</code>
* <code>imul R, L/M/R</code>
* <code>imul R, M/R, L</code>

与mul相似，除法也分为无符号和有符号两种。DIV指令用来执行无符号整数的除法运算，它会把结果以商和余数的形式分别保存。
![](https://img-blog.csdnimg.cn/aa5e1f6fdba74f888bfa8e48353d9682.png)

* <code>div M/R</code>
* <code>idiv M/R</code>

无符号逻辑位移，有符号算术位移
* <code>shl M/R, L</code>
* <code>shr M/R, L</code>
* <code>sal M/R, L</code>
* <code>sar M/R, L</code>

符号拓展指令
* <code>cbw</code>
* <code>cbd</code>
* <code>cbq</code>
* <code>cbo</code>
![](https://img-blog.csdnimg.cn/a021328333c54a08bce131a821f0bf00.png)

## 数据对齐
汇编代码的一项优势在于可以写出高效的程序，其中一个重要方面体现在内存访问上。CPU访问偶数地址上的数据比奇数地址上快。例如对应32位系统，如果可以把数据以32位为单元存放，并保证结构体都处在16字节边界上，那么内存访问就比较高效。
```asm title=不同汇编器的数据对其伪指令
#gas
.balign alignment   #.balign 2

;NASM
SECTION .data               SECTION .bss
ALIGN alignment  ;ALIGN 2 | ALIGNB alignment
```
这是修改位置计数器的不同方法，这些命令会让汇编器计算当前地址，并向前推进直到遇到第一个目标位置。
:::tip
在gas中，<code>.align</code>命令和其他架构的表现和x86_64架构可能不同，因此，尽量使用<code>.balign</code>命令。
:::

## 数组

## 算术运算实例
虽然现在我们可以输出，但是需要注意的是，输入默认都是字符，按照ASCII字符编码输出，在这个例子中输出The num is K，其中K是我们数字运算的结果，ASCII码为75，与我们的计算结果相同。这里不计算补码的原因是ASCII码无法显示。只能先这样简单测试结果正确与否。
:::info
等我们后面有了输出数字的方法可以再到这里测试一下输出。
:::
#### 32位NASM
```
SECTION .data
msg: db 'The num is '
sum: dd 0
val: dd 25

SECTION .text
global  _start
 
_start:

    mov eax, 0
    inc eax
    add eax, 100
    sub eax, [val]
    mov [sum], eax
    dec dword [sum]
    ;neg dword [sum]

    mov edx, 12
    mov ecx, msg
    mov bx, 1
    mov eax, 4
    int 80h

    mov ebx, 0
    mov eax, 1
    int 80h
```
#### 32位gas
```
.section .data
output: .string "Hello"
sum: .long 0
val: .long 25

.text
.global _start
_start:

    movl $0, %eax
    incl %eax
    addl $100, %eax
    subl val, %eax
    movl %eax, sum
    decl sum
    #negl sum

    movl $4, %eax # write对应编号为4
    movl $1, %ebx # 第1个参数文件描述符为1，表示stdout
    leal sum, %ecx # 第2个参数，字符串指针
    movl $1, %edx # 第3个参数，写入的字节数
    int $0x80

    movl $1, %eax # exit系统调用
    xor %edi, %edi # 第1个参数，0，即exit(0)
    int $0x80

```

#### 64位gas
```
#compiled by
#~$as hello64.S -o hello.o
#~$ld hello.o -o hello
.section .data
msg:	.string "Hello\n"
sum:    .quad 0
val:    .quad 25

.text
.globl _start
_start:
	pushq %rax # for stack alignment

    movq $0, %rax
    incq %rax
    addq $100, %rax
    subq val, %rax
    movq %rax, sum
    decq sum
    #negq sum

	movq $1, %rax 
	movq $1, %rdi
	leaq msg(%rip), %rsi
	movq $8, %rdx
	syscall

	# exit(0)
	movq $60, %rax
	xor %rdi, %rdi
	syscall
```