---
title: 操作系统进程管理
description: 介绍操作系统的进程管理
tags: [Operating System]
---

## 要处理的问题
1. 如何管理所有的任务结构体
2. 如何进程进程切换
3. 虽然我们要尽量给每个进程提供一个不受干扰的，与其他进程隔离的环境，但是我们还是不可避免的需要进程之间的通信方式
4. 对每一个任务的各种状态信息进行保存，包括运行状态，运行时间，使用的文件等等(其中需要着重考虑的有两点)
 * 在任务切换的过程中，我们需要保存之前任务的各种寄存器上下文
 * 对于一个进程，我们需要获得其代码段与数据段和栈这些运行时不可或缺的内容
### 任务结构体的管理
先不要管任务的内容到底是什么样子的，首先我们应该考虑用什么样的方式管理所有进程的信息。  
首先想到的就算数组，但是很明显，任务是不连续的，而且可能会随时退出或者增加，因此数组很明显是不行的，但是链表就刚好符合这种应用场景，也就是说，我们可以在内核中专门为其预留一段空间(类似数组链表)来处理，同时，我们也可以把每个任务节点放在这个任务自己的空间内并使用链表链接起来。

### 任务切换
任务如何进行切换呢，我们可以让CPU每运行一段时间就停下来，然后去运行另一个进程。但是这个过程肯定是不能将这些工作交给用户代码的，因此我们需要有一种方式，让任务可以隔一段时间停止运行，然后运行我们的任务调度程序，进而转去另外一个任务。为了实现这种功能，CPU设计者们添加了计时器，没隔一段时间就去提醒CPU该停一下，进而进行任务切换

### 进程通信
首先要明确，这里的进程通信是比较简单的，是两个进程之间通过传递某些信息从而告诉另外的进程应该执行什么动作(函数)，并不是数据信息的传递。
这个好像有点难搞，如何才能让进程进行通信呢？这个通信一定要  
* 及时的，不能依赖于某些特定事件的发生
* 在接受到信息后，进程要可以进行相应的动作，不然这种信息传递将毫无作用
* 消息可以同步也可以异步也可以

我们分析一下，要检测通信信号，我们就需要单独的进程(代码)对其进行检测和操作，也就是说每一次的CPU任务切换的时候我们就需要执行一遍通信信号检测，那么我们为何不直接把信号检测和任务切换放在一起呢？

### 任务结构体的内容
* **首先就是任务上时间片运行的时间，以及当前已经运行的时间**
* **同时，有些进程是不能上时间片运行的，例如等待硬盘等低速设备的进程，因此需要表明进程状态**
* 对于进程，我们需要对其进行管理，因此就需要为其赋予id等管理信息
* 对于进程，我们还需要保存其调用者的信息即用户信息
* **为了保存任务的上下文信息，我们需要相关的结构体**
* **为了保存任务的代码、数据和栈的信息，我们需要相关的结构体**
* **为了进程间的通信，我们需要建立通信所需的内容**

## Linux0.12中的实现
### 任务结构体
我们首先需要实现一个任务结构体，Linux0.12中如下：  
```cpp
struct task_struct {
	/* these are hardcoded - don't touch */
	// 下面这几个字段是硬编码字段
	long state;							// 任务的运行状态(-1 不可运行,0 可运行(就绪), >0 已停止)
	long counter;						// 任务运行时间计数(递减)(滴答数),运行时间片
	long priority;						// 优先数.任务开始运行时counter=priority,越大运行越长
	long signal;						// 信号位图,每个比特位代表一种信号,信号值=位偏移值+1
	struct sigaction sigaction[32];		// 信号执行属性结构,对应信号将要执行的操作和标志信息
	long blocked;						// 进程信号屏蔽码(对应信号位图)

	/* various fields */
	int exit_code;						// 任务执行停止的退出码,其父进程会取.
	unsigned long start_code;			// 代码段地址
	unsigned long end_code;				// 代码长度(字节数)
	unsigned long end_data;				// 代码长度+数据长度(字节数)
	unsigned long brk;					// 总长度(字节数)
	unsigned long start_stack;			// 堆栈段地址
	long pid;							// 进程标识号(进程号)
	long pgrp;							// 进程组号
	long session;						// 会话号
	long leader;						// 会话首领
	int	groups[NGROUPS];				// 进程所属组号.一个进程可属于多个组
	/*
	 * pointers to parent process, youngest child, younger sibling,
	 * older sibling, respectively.  (p->father can be replaced with
	 * p->p_pptr->pid)
	 */
	struct task_struct *p_pptr;			// 指向父进程的指针
	struct task_struct *p_cptr;			// 指向最新子进程的指针
	struct task_struct *p_ysptr;		// 指向比自己后创建的相邻进程的指针
	struct task_struct *p_osptr;		// 指向比自己早创建的相邻进程的指针
	unsigned short uid;					// 用户标识号(用户id)
	unsigned short euid;				// 有效用户id
	unsigned short suid;				// 保存的用户id
	unsigned short gid;					// 组标识号(级id)
	unsigned short egid;				// 有效级id
	unsigned short sgid;				// 保存的组id
	unsigned long timeout;				// 内核定时超时值
	unsigned long alarm;				// 报警定时值(滴答数)
	long utime;							// 用户态运行时间(滴答数)
	long stime;							// 系统态运行时间(滴答数)
	long cutime;						// 子进程用户态运行时间
	long cstime;						// 子进程系统态运行时间
	long start_time;					// 进程开始运行时刻.
	struct rlimit rlim[RLIM_NLIMITS];	// 进程资源使用统计数组.
	/* per process flags, defined below */
	unsigned int flags;					// 各进程的标志
	unsigned short used_math;			// 标志:是否使用了协处理器.

	/* file system info */
	/* -1 if no tty, so it must be signed */
	int tty;							// 进程使用tty终端的子设备号.-1表示没有使用
	unsigned short umask;				// 文件创建属性屏蔽位
	struct m_inode * pwd;				// 当前工作目录i节点结构指针
	struct m_inode * root;				// 根目录i节点结构指针
	struct m_inode * executable;		// 执行文件i节点结构指针
	struct m_inode * library;			// 被加载库文件i节点结构指针
	unsigned long close_on_exec;		// 执行时关闭文件句柄位图标志.(include/fcntl.h)
	struct file * filp[NR_OPEN];		// 文件结构指针表,最多32项.表项号即是文件描述符的值
	/* ldt for this task 0 - zero 1 - cs 2 - ds&ss */
	struct desc_struct ldt[3];			// 局部描述符表, 0 - 空,1 - 代码段cs,2 - 数据和堆栈段ds&ss
	/* tss for this task */
	struct tss_struct tss;				// 进程的任务状态段信息结构
};
```

### 任务结构体的管理
对于任务结构体，为了遍历的迅速，Linux0.12使用一个进程结构体指针数组储存任务结构体而非链表。<code>struct task_struct * task[NR_TASKS] = {&(init_task.task), };</code>
    
### 任务切换
任务切换是通过schedule()函数，通过扫描任务结构体指针数组来选择下一个任务，并通过switch()汇编宏进行切换。

### 信号
Linux0.12使用了任务结构体中的<code>long signal</code>和<code>struct sigaction sigaction[32]</code>及<code>long block</code>来表示任务对信号的处理。如果接收到信号，就在signal的相应位置置1，并在中断返回进行信号检测和处理时执行sigaction[32]数组中相应的信号处理函数。那么如何接收信号呢？  
在tty_intr(struct tty_struct *tty, int mask);中我们可以看到如何接收信号  
```cpp title=tty_io.c
void tty_intr (struct tty_struct *tty, int mask) {
    int i;
    ...
    for (i = 0; i < NR_TASKS; i++) {
        if (task[i] && task[i]->pgrp == tty->pgrp) {
            task[i]->signal |= mask;
        }
    }
}
```
即为简单的相应的位置置位。