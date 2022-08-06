---
title: 基本指令
description: 了解汇编程序经常使用的基本指令
tags: [Assembly]
---

# 基本指令

## 准备阶段
### 系统环境
要学习汇编语言基本指令的操作，我们首先需要准备好学习汇编的环境。在这里，我们使用了GNU as和NASM，环境是Ubuntu-22.04-LST-Desktop
### 系统调用
另外，汇编并没有提供相应的输入输出函数，它直接与操作系统接触，因此，在Linux环境下，我们需要使用Linux下的系统调用，即Linux为在其系统上运行的软件提供的访问系统资源的方式，任何Linux程序都只能使用这些系统调用来实现其功能。实际上，很多已经被我们习以为常的C语言标准函数，在Linux平台上的实现都是靠系统调用完成的，所以如果想对系统底层的原理作深入的了解，掌握各种系统调用是初步的要求。  
在32位x86 Linux系统中，可用的系统调用定义在/usr/include/asm/unistd_32.h头文件中。  
在64位x86_64 Linux系统中，可用的系统调用定义在/usr/include/asm/unistd_64.h头文件中。  
我们会使用Linux的系统调用<code>ssize_t write(int fd, const void *buf, size_t count);</code>以及退出程序的系统调用exit，原型可能是<code>void exit(int status);</code>，通过<code>man exit</code>找到的。

### HelloWorld
接下来让我们通过HelloWorld来看一下最基本的程序，并了解一下系统调用

我们经常会用到如下使用系统调用的代码:
#### 32位
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
movq $1, %eax # exit系统调用
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

#### 64位
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
