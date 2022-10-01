---
title: Rust基本语句的汇编实现
description: Rust汇编原理
tags: [Rust]
---

# 查看Rust生成的汇编
## 使用编译器生成汇编
直接使用<code>rustc -O --emit asm=test_rust.s .\test_rust.rs</code>来编译生成汇编代码
## 使用GNU二进制工具objdump反汇编生成的代码
使用cargo创建rust项目并编译时，默认编译为debug版本，这个时候我们使用<code>objdump -j .text -S testrust.rs > testrust.s</code>来反汇编生成汇编，因为此时是调试版本，其中包含调试信息并且减少了对代码的优化，因此我们甚至可以更加完整的查看语句与其对应的汇编代码。
```asm
0000000000008260 <_ZN8testrust4main17he13f1e6ebf6c8278E>:
fn main() {
    8260:	48 83 ec 38          	sub    $0x38,%rsp
    let s1 = String::from("hello");
    8264:	48 8d 7c 24 08       	lea    0x8(%rsp),%rdi
    8269:	48 8d 35 2a 5e 03 00 	lea    0x35e2a(%rip),%rsi        # 3e09a <_fini+0xfb6>
    8270:	ba 05 00 00 00       	mov    $0x5,%edx
    8275:	e8 16 0f 00 00       	call   9190 <_ZN76_$LT$alloc..string..String$u20$as$u20$core..convert..From$LT$$RF$str$GT$$GT$4from17h8d67c460939c9434E>
    827a:	48 8d 7c 24 08       	lea    0x8(%rsp),%rdi

    let len = calculate_length(&s1);
    827f:	e8 5c 00 00 00       	call   82e0 <_ZN8testrust16calculate_length17h9b522f408d683362E>
    8284:	48 89 04 24          	mov    %rax,(%rsp)
    8288:	eb 1c                	jmp    82a6 <_ZN8testrust4main17he13f1e6ebf6c8278E+0x46>
    828a:	48 8d 7c 24 08       	lea    0x8(%rsp),%rdi

}
    828f:	e8 2c 0d 00 00       	call   8fc0 <_ZN4core3ptr42drop_in_place$LT$alloc..string..String$GT$17h7e25b57dbd59f357E>
    8294:	eb 30                	jmp    82c6 <_ZN8testrust4main17he13f1e6ebf6c8278E+0x66>
    8296:	48 89 c1             	mov    %rax,%rcx
    8299:	89 d0                	mov    %edx,%eax
```

:::tip
可以使用<code>readelf -h testrust</code>来查看elf可执行文件的elf头，同时查看其中的入口地址  
```asm
hwt@hwt-virtual-machine:~/rustlang/testrust/target/debug$ readelf -h testrust  
ELF 头：
  Magic：   7f 45 4c 46 02 01 01 00 00 00 00 00 00 00 00 00 
  类别:                              ELF64
  数据:                              2 补码，小端序 (little endian)
  Version:                           1 (current)
  OS/ABI:                            UNIX - System V
  ABI 版本:                          0
  类型:                              DYN (Position-Independent Executable file)
  系统架构:                          Advanced Micro Devices X86-64
  版本:                              0x1
  入口点地址：               0x77a0
  程序头起点：          64 (bytes into file)
  Start of section headers:          3881448 (bytes into file)
  标志：             0x0
  Size of this header:               64 (bytes)
  Size of program headers:           56 (bytes)
  Number of program headers:         14
  Size of section headers:           64 (bytes)
  Number of section headers:         44
  Section header string table index: 43

```
入口函数为0x77a0，查看反汇编文件即可发现为_start  
```asm
00000000000077a0 <_start>:
    77a0:	f3 0f 1e fa          	endbr64 
    77a4:	31 ed                	xor    %ebp,%ebp
    77a6:	49 89 d1             	mov    %rdx,%r9
    77a9:	5e                   	pop    %rsi
    77aa:	48 89 e2             	mov    %rsp,%rdx
    77ad:	48 83 e4 f0          	and    $0xfffffffffffffff0,%rsp
    77b1:	50                   	push   %rax
    77b2:	54                   	push   %rsp
    77b3:	45 31 c0             	xor    %r8d,%r8d
    77b6:	31 c9                	xor    %ecx,%ecx
    77b8:	48 8d 3d 21 01 00 00 	lea    0x121(%rip),%rdi        # 78e0 <main>
    77bf:	ff 15 d3 51 04 00    	call   *0x451d3(%rip)        # 4c998 <__libc_start_main@GLIBC_2.34>
    77c5:	f4                   	hlt    
    77c6:	66 2e 0f 1f 84 00 00 	cs nopw 0x0(%rax,%rax,1)
    77cd:	00 00 00 
```
其中借助其他函数调用了main函数
```asm
00000000000078e0 <main>:
    78e0:	50                   	push   %rax
    78e1:	48 89 f2             	mov    %rsi,%rdx
    78e4:	48 8d 05 80 9b 03 00 	lea    0x39b80(%rip),%rax        # 4146b <__rustc_debug_gdb_scripts_section__>
    78eb:	8a 00                	mov    (%rax),%al
    78ed:	48 63 f7             	movslq %edi,%rsi
    78f0:	48 8d 3d c9 ff ff ff 	lea    -0x37(%rip),%rdi        # 78c0 <_ZN8testrust4main17he13f1e6ebf6c8278E>
    78f7:	e8 e4 00 00 00       	call   79e0 <_ZN3std2rt10lang_start17he2d8036685df4af9E>
    78fc:	59                   	pop    %rcx
    78fd:	c3                   	ret    
    78fe:	66 90                	xchg   %ax,%ax
```
之后又调用了rust真正的主函数<_ZN8testrust4main17he13f1e6ebf6c8278E>，从符号中可以读出testrust main的符号，很明显是经过修饰的符号。在0x78c0的位置。  
```
00000000000078c0 <_ZN8testrust4main17he13f1e6ebf6c8278E>:
fn main() {
    78c0:	50                   	push   %rax
    let first_var:i32 = 66;
    78c1:	c7 04 24 42 00 00 00 	movl   $0x42,(%rsp)
    let mut second_var:i32 = 99;
    78c8:	c7 44 24 04 63 00 00 	movl   $0x63,0x4(%rsp)
    78cf:	00 
}
    78d0:	58                   	pop    %rax
    78d1:	c3                   	ret    
    78d2:	66 2e 0f 1f 84 00 00 	cs nopw 0x0(%rax,%rax,1)
    78d9:	00 00 00 
    78dc:	0f 1f 40 00          	nopl   0x0(%rax)
```
从这段rust主函数就可以看出因为是debug版本的关系，甚至可以有语句相对应，这就为分析汇编提供了便利。
:::
# Rust基本语句的汇编
## 变量
### 可变和不可变变量
下面的代码编译就生成了上方TIP中的汇编代码  
```rust
fn main() {
    let first_var:i32 = 66;
    let mut second_var:i32 = 99;
}
```
通过汇编代码我们可以发现在汇编层面，代码是没有区分可变与不可变的，事实上也是不可能的，因为这是编译器的工作。

### 遮蔽
```asm
00000000000078c0 <_ZN8testrust4main17he13f1e6ebf6c8278E>:
fn main() {
    78c0:	48 83 ec 0c          	sub    $0xc,%rsp
    let x = 5;
    78c4:	c7 04 24 05 00 00 00 	movl   $0x5,(%rsp)

    let x = x + 1;
    78cb:	c7 44 24 04 06 00 00 	movl   $0x6,0x4(%rsp)
    78d2:	00 

    {
        let x = x * 2;
    78d3:	c7 44 24 08 0c 00 00 	movl   $0xc,0x8(%rsp)
    78da:	00 
    }

    78db:	48 83 c4 0c          	add    $0xc,%rsp
    78df:	c3                   	ret   
```
我们可以发现，这些变量看似是相同的名字，但是遮蔽后实际上是用新的变量空间，也就是说是用新的代替了原来的变量，但是原来的变量还在，如果在代码中{let x = x*2}的作用域中输出，那么会输出12，而当脱离了作用域后，x又恢复成let x=x+1;运算后的值了，即6。也就是说遮蔽其实就算相同作用域中后面的变量取代前面的变量，内部作用域的变量取代外部作用域的变量，其实现方式是为每个变量分配一个空间。

接下来我们看一看在{let x=x*2}的作用域外部是如何使用同作用域的变量的
```rust
fn main() {
    let mut x = 5;

    let mut x = x + 1;

    {
        let x = x * 2;

    }
    x = x + 2;

}
```
```asm
00000000000078c0 <_ZN8testrust4main17he13f1e6ebf6c8278E>:
fn main() {
    78c0:	48 83 ec 18          	sub    $0x18,%rsp
    let mut x = 5;
    78c4:	c7 44 24 10 05 00 00 	movl   $0x5,0x10(%rsp)
    78cb:	00 

    let mut x = x + 1;
    78cc:	c7 44 24 0c 06 00 00 	movl   $0x6,0xc(%rsp)
    78d3:	00 

    {
        let x = x * 2;
    78d4:	c7 44 24 14 0c 00 00 	movl   $0xc,0x14(%rsp)
    78db:	00 

    }
    x = x + 2;
    78dc:	8b 44 24 0c          	mov    0xc(%rsp),%eax
    78e0:	83 c0 02             	add    $0x2,%eax
    78e3:	89 44 24 08          	mov    %eax,0x8(%rsp)
    78e7:	0f 90 c0             	seto   %al
    78ea:	a8 01                	test   $0x1,%al
    78ec:	75 0d                	jne    78fb <_ZN8testrust4main17he13f1e6ebf6c8278E+0x3b>
    78ee:	8b 44 24 08          	mov    0x8(%rsp),%eax
    78f2:	89 44 24 0c          	mov    %eax,0xc(%rsp)

    78f6:	48 83 c4 18          	add    $0x18,%rsp
    78fa:	c3                   	ret    
```


注意不要把重新使用let进行变量遮蔽和对原变量的操作搞混，例如  
```rust
fn main() {
    let mut x = 5;

    x = x + 1;

    {
        let x = x * 2;
    }
}
```
```nasm
00000000000078c0 <_ZN8testrust4main17he13f1e6ebf6c8278E>:
fn main() {
    78c0:	50                   	push   %rax
    let mut x = 5;
    78c1:	c7 04 24 05 00 00 00 	movl   $0x5,(%rsp)

    x = x + 1;
    78c8:	c7 04 24 06 00 00 00 	movl   $0x6,(%rsp)

    {
        let x = x * 2;
    78cf:	c7 44 24 04 0c 00 00 	movl   $0xc,0x4(%rsp)
    78d6:	00 
    }
    78d7:	58                   	pop    %rax
    78d8:	c3                   	ret    
    78d9:	0f 1f 80 00 00 00 00 	nopl   0x0(%rax)

```
我们可以看出进行操作的是同一个变量x。

### 常量
```rust
fn main() {
    const THREE_HOURS_IN_SECONDS: i32 = 60;
    let x = THREE_HOURS_IN_SECONDS;
}
```
```asm
00000000000078c0 <_ZN8testrust4main17he13f1e6ebf6c8278E>:
fn main() {
    78c0:	48 83 ec 04          	sub    $0x4,%rsp
    const THREE_HOURS_IN_SECONDS: i32 = 60;
    let x = THREE_HOURS_IN_SECONDS;
    78c4:	c7 04 24 3c 00 00 00 	movl   $0x3c,(%rsp)
    78cb:	48 83 c4 04          	add    $0x4,%rsp
    78cf:	c3                   	ret    
```
感觉和C语言的宏表现相似，直接替换的感觉，具体的不太清楚了。
## 数据类型
### bool
```rust
fn main() {
    let t:bool = false;
}
```
首先说个结论，bool类型在内存中是用1或0表示的，并且只有一个字节大小。那么它会有字节对其吗？  
```rust
fn main() {
    let a = 1;
    let t = false;
    let b = 2;
}
```
```asm
00000000000078c0 <_ZN8testrust4main17he13f1e6ebf6c8278E>:
fn main() {
    78c0:	48 83 ec 0c          	sub    $0xc,%rsp
    let a = 1;
    78c4:	c7 04 24 01 00 00 00 	movl   $0x1,(%rsp)
    let t = false;
    78cb:	c6 44 24 07 00       	movb   $0x0,0x7(%rsp)
    let b = 2;
    78d0:	c7 44 24 08 02 00 00 	movl   $0x2,0x8(%rsp)
    78d7:	00 
    78d8:	48 83 c4 0c          	add    $0xc,%rsp
    78dc:	c3                   	ret    
    78dd:	0f 1f 00             	nopl   (%rax)
```
可以发现确实是的，为了对其在0x4(%rsp)到0x7(%rsp)是空的。

