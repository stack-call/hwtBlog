---
title: 中级指令
description: 了解汇编程序的布尔运算即流程控制
tags: [Assembly]
---

## 布尔指令  
* <code>not M/R</code>   
* <code>and M/R, L/M/R</code>  
* <code>or M/R, L/M/R</code>  
* <code>xor M/R, L/M/R</code>  

具体应用随后补充
## 跳转指令
### 无条件跳转
* <code>jmp label</code>
### 条件跳转

![](https://img-blog.csdnimg.cn/c96711c49570477f99c2ae9629a52be8.png)
条件跳转要根据EFLAG Register中的某个标志位来判断跳转，因此，我们需要一些根据某些条件设置这些标志位的指令。有两种指令
#### test
<code>test M/R, L/M/R</code>
test指令会对两个操作数执行AND操作，但是并不修改二者的值，也不保存计算的结果。不过，它会根据结果修改PF, SF及ZF等标志位。

#### cmp
<code>cmp M/R, L/M/R</code>

### 复合条件
待补充

## 重复执行(循环)
### 用CX/ECX/RCX实现循环
<code>loop label</code>  

执行到loop指令时，CPU会这样判断：   
**RCX值减一，然后判断它是否为0，若不等于0，则跳转到label，否则执行loop下面的指令**

### 使用条件跳转