---
title: Rust函数的汇编实现
description: Rust汇编原理
tags: [Rust]
---
# Rust函数的汇编实现
## 函数调用
```rust
fn main() {
    let x = 66;
    another_function();
}

fn another_function() {
    let y = 99;
}
```
```asm
00000000000078c0 <_ZN8testrust4main17he13f1e6ebf6c8278E>:
fn main() {
    78c0:	50                   	push   %rax
    let x = 66;
    78c1:	c7 44 24 04 42 00 00 	movl   $0x42,0x4(%rsp)
    78c8:	00 
    another_function();
    78c9:	e8 02 00 00 00       	call   78d0 <_ZN8testrust16another_function17h59adf1e5e99fcce0E>
}
    78ce:	58                   	pop    %rax
    78cf:	c3                   	ret    
```
```asm
00000000000078d0 <_ZN8testrust16another_function17h59adf1e5e99fcce0E>:

fn another_function() {
    78d0:	48 83 ec 04          	sub    $0x4,%rsp
    let y = 99;
    78d4:	c7 04 24 63 00 00 00 	movl   $0x63,(%rsp)
    78db:	48 83 c4 04          	add    $0x4,%rsp
    78df:	c3                   	ret    
```
嗯......，没有什么好说的

## 带参数的函数调用
### 带mut
```rust
fn main() {
    another_function(5);
}

fn another_function(mut x: i32) {
    x = x + 1;
}
```
```asm
00000000000078c0 <_ZN8testrust4main17he13f1e6ebf6c8278E>:
fn main() {
    78c0:	50                   	push   %rax
    another_function(5);
    78c1:	bf 05 00 00 00       	mov    $0x5,%edi
    78c6:	e8 05 00 00 00       	call   78d0 <_ZN8testrust16another_function17ha26e002c311b0308E>
}
    78cb:	58                   	pop    %rax
    78cc:	c3                   	ret    
    78cd:	0f 1f 00             	nopl   (%rax)
```
```
00000000000078d0 <_ZN8testrust16another_function17ha26e002c311b0308E>:
fn another_function(mut x: i32) {
    78d0:	50                   	push   %rax
    78d1:	89 7c 24 04          	mov    %edi,0x4(%rsp)
    x = x + 1;
    78d5:	8b 44 24 04          	mov    0x4(%rsp),%eax
    78d9:	ff c0                	inc    %eax
    78db:	89 04 24             	mov    %eax,(%rsp)
    78de:	0f 90 c0             	seto   %al
    78e1:	a8 01                	test   $0x1,%al
    78e3:	75 09                	jne    78ee <_ZN8testrust16another_function17ha26e002c311b0308E+0x1e>
    78e5:	8b 04 24             	mov    (%rsp),%eax
    78e8:	89 44 24 04          	mov    %eax,0x4(%rsp)
    78ec:	58                   	pop    %rax
    78ed:	c3                   	ret      
```
可以发现确实只是副本传参，并且使用inc为其栈中的x增加1。

### 不带mut
```rust
fn main() {
    another_function(5);
}

fn another_function(x: i32) {
    let y = x;
}
```
```asm
00000000000078c0 <_ZN8testrust4main17he13f1e6ebf6c8278E>:
fn main() {
    78c0:	50                   	push   %rax
    another_function(5);
    78c1:	bf 05 00 00 00       	mov    $0x5,%edi
    78c6:	e8 05 00 00 00       	call   78d0 <_ZN8testrust16another_function17ha26e002c311b0308E>
}
    78cb:	58                   	pop    %rax
    78cc:	c3                   	ret    
    78cd:	0f 1f 00             	nopl   (%rax)
```
```
00000000000078d0 <_ZN8testrust16another_function17ha26e002c311b0308E>:
fn another_function(x: i32) {
    78d0:	50                   	push   %rax
    78d1:	89 3c 24             	mov    %edi,(%rsp)
    let y = x;
    78d4:	89 7c 24 04          	mov    %edi,0x4(%rsp)
    78d8:	58                   	pop    %rax
    78d9:	c3                   	ret    
    78da:	66 0f 1f 44 00 00    	nopw   0x0(%rax,%rax,1)
```
我们可以发现带不带mut和变量的处理一样，只是编译器的行为，类似C++的带const的参数。

## 带返回值的函数
```rust
fn five() -> i32 {
    5
}

fn main() {
    let x = five();

}
```
```
00000000000078c0 <_ZN8testrust4five17h7c58057d65c83c27E>:
fn five() -> i32 {
    5
}
    78c0:	b8 05 00 00 00       	mov    $0x5,%eax
    78c5:	c3                   	ret    
    78c6:	66 2e 0f 1f 84 00 00 	cs nopw 0x0(%rax,%rax,1)
    78cd:	00 00 00 
```
```
00000000000078d0 <_ZN8testrust4main17he13f1e6ebf6c8278E>:
fn main() {
    78d0:	50                   	push   %rax
    let x = five();
    78d1:	e8 ea ff ff ff       	call   78c0 <_ZN8testrust4five17h7c58057d65c83c27E>
    78d6:	89 44 24 04          	mov    %eax,0x4(%rsp)

    78da:	58                   	pop    %rax
    78db:	c3                   	ret    
    78dc:	0f 1f 40 00          	nopl   0x0(%rax)
```
没啥说的。