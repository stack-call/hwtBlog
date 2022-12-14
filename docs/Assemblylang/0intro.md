---
title: 汇编语言简介
description: 介绍汇编语言的用处
tags: [Assembly]
---

## 写在前面
本人只了解intel的x86-64，虽然amd64和intel64本质相同，但是还是有微小区别。本人也只使用过intel处理器的电脑运行相关代码，相关知识从《Intel® 64 and IA-32 Architectures
Software Developer’s Manual》及其他书籍中获取，对于AMD64等或其他架构不太了解，许多内容可能不够全面，本文档主要为本人在学习时整理，如有出错还请见谅。

**注：文档中没有特别说明的寄存器或者内存访问等可能指的是32位情况(因为Intel开发者手册默认就是将32位)**

## 为什么需要汇编语言
* 汇编语言可以让我们更好的了解计算机的体系结构，底层原理，如体系结构中的寄存器，CPU等的细节，更加贴近计算机的底层。
* 对于汇编的学习可以让我们更好的把握对上层知识的把握，例如C语言，C++的编程，并充分利益硬件的特征来优化程序的效率。(汇编语言虽然开发效率低，兼容性不强，但是其效率非常高)
* 许多计算机领域需要拥有汇编的知识储备，例如操作系统的引导就需要使用汇编语言(gas)，在其中的某些C语言代码中如果需要非常精细的操作还需要用到C语言的内联汇编，由此可见C语言在底层操作中的重要性。
>总之，使用汇编语言就像直接同计算机的底层对话，这是使用其他的编程语言所感受不到的。
## 汇编语言分类

### 语法分类
汇编语言虽然底层原理都是将操作数和操作码按照开发手册中的目录翻译成操作码，但是汇编语言本身的语法刚开始并没有一个定法，因此就有了<code>AT&T</code>和<code>Intel</code>两种语法，针对这两种语法也有很多编译器，但是现在很多编译器都可以识别两种语法，例如<code>gas</code>作为<code>AT&T</code>的代表编译器就可以编译<code>Intel</code>语法(但是我没有用过)。

### 编译器
在我看到的使用汇编语言的书籍中，提及最多的就是<code>Nasm</code>，<code>Masm</code>和<code>gas</code>，其他的还有<code>yasm</code>，<code>fasm</code>等等。我使用过的就是<code>Nasm</code>和<code>gas</code>。同时，学习<code>Nasm</code>和<code>gas</code>最好使用Linux环境。  
:::tip
学习汇编语言不要去看教程，很多都是过时的，在学校图书馆甚至基本都没找到讲64位架构上的汇编的书籍，甚至感觉讲x86的都不多。推荐一本书《Assembly Programming and Computer Architecture for Software Engineers (APCASE)》，中文名《汇编程序设计与计算机体系结构——软件工程师教程》。讲的<code>Nasm</code>，<code>Masm</code>和<code>gas</code>的x86和64架构上的编程，非常全面。
:::

### 文档组织方式
文档讲述汇编内容会着重从：  
1. 汇编语言从底层来说是翻译成机器码的，也就是说需要操作码和操作数，即指令和指令操作数。  
因此，汇编语言需要有指令(instruction)和指令的操作数，操作数可以是立即数，寄存器或者内存空间。在这里，内存空间通过标识符(变量)来标志。  
2. 同时，汇编语言与高级语言大的区别就是其需要分段。
     * 因为汇编是比较贴近底层的，而在计算机运行时，CPU运算单元执行指令，这个指令由ip(eip,rip)指向，每次运行完一条指令就加一，当需要数据时，再从数据段(内存)或寄存器中取?是不是还有其他原因
    * 对于操作系统而言，指令段和数据段的权限是不同的，分开储存

3. 汇编语言也是一种编程语言，只是其语法比较接近机器指令，它也会有编译器提供的独特的，方便编程的内容(就像C语言的预处理器？)，这就是伪指令(directive或叫伪操作)，就是编译器提供的操作，并不是这个语言本身的内容，因为这些本来就不能由语言本身完成(这里指的语言更接近于可以编译生成的机器码)，就像<code>Nasm</code>中的<code>.org</code>，用来指定指令加载地点，在操作系统的引导中非常有用，因为这是汇编语言生产的<code>elf</code>格式的文件中的内容，不可能用编译器修改，而是编译器生成时加上的。  

这些汇编语言的组成讲述。
:::info
文档参考书籍及网站链接汇总
* [ Intel® 64 and IA-32 Architectures Software Developer’s Manual Combined Volumes: 1, 2A, 2B, 2C, 2D, 3A, 3B, 3C, 3D, and 4 ](https://www.intel.cn/content/www/cn/zh/developer/articles/technical/intel-sdm.html?wapkw=intel\ 64)
* [NASM Assembly Language Tutorials - asmtutor.com](https://asmtutor.com)
* [《Assembly Programming and Computer Architecture for Software Engineers (APCASE)》，中文名《汇编程序设计与计算机体系结构——软件工程师教程》的代码仓库](https://github.com/brianrhall/Assembly)
* [主函数main和程序入口_start](https://blog.csdn.net/m0_55708805/article/details/117827482)
* [深入理解计算机系统(CSAPP)]
* [程序员的自我修养—链接、装载与库]
* [Professional Assembly Language(汇编语言程序设计)————Richard Blum著，马朝晖 等译]
:::