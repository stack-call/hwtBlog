---
title: "Rust 生命周期：'a 到底约束了什么"
sidebar_label: "附：生命周期一次讲清"
sidebar_position: 41
description: "用一个贯穿的问题把生命周期讲完：'a 不是时间长度，而是引用之间需要满足的有效性关系。附八个由浅入深的例子与判断清单。"
slug: /rust-design/lifetime-summary
---

# Rust 生命周期：'a 到底约束了什么

这一页把 [第 11 章](./part-3/11-reference-outlives-data.md) 到 [第 13 章](./part-3/13-borrow-checker-limits.md) 关于生命周期的问题收在一处：先讲清 `'a` 到底约束了什么，再用八个由浅入深的例子逐个验证，最后给出一套判断方式。

## 一、生命周期为什么存在？

Rust 允许通过引用访问自己不拥有的数据。

```rust
let name = String::from("Alice");
let r = &name;
```

这里：

- `name` 拥有字符串数据。
- `r` 借用字符串数据。

核心要求：使用引用时，它所指向的数据必须仍然有效，同时不能存在冲突的借用。

这就是 Rust 需要生命周期分析的根本原因。

## 二、`'a` 究竟表示什么？

```rust
struct Person<'a> {
    name: &'a str,
}
```

- `<'a>`：声明一个生命周期参数。
- `&'a str`：表示一个需要在 `'a` 范围内有效的字符串引用。
- `'a`：不是时间长度，也不直接指定代码的起止位置，而是引用有效性约束的参数。

生命周期参数不会延长数据的实际存活时间。

## 三、相同的 `'a` 表示什么？

```rust
struct Person<'a> {
    name: &'a str,
    address: &'a str,
}
```

相同的 `'a` 表示：

两个引用被共同的生命周期约束关联起来，都必须能够满足 `'a` 所要求的有效范围。

但不代表：

- 两个引用必须同时创建。
- 两个引用必须同时销毁。
- 使用 `name` 时必须同时使用 `address`。
- 两份数据的实际存活时间完全相同。

## 四、为什么需要 `'a` 和 `'b`？

当希望两个引用具有独立的生命周期约束时：

```rust
struct Person<'a, 'b> {
    name: &'a str,
    address: &'b str,
}
```

这样就可以分别表达：

- `name` 依赖 `'a`。
- `address` 依赖 `'b`。

例如，一个方法返回 `name` 的引用，就可以只关联 `'a`，不必让 `address` 的有效范围限制返回结果。

## 五、生命周期适用于哪里？

| 场景 | 生命周期的作用 |
| --- | --- |
| 普通局部引用 | 检查引用使用时数据是否有效 |
| 函数 | 表达输入与输出引用之间的关系 |
| 结构体 | 表达字段对外部数据的借用关系 |
| 枚举 | 表达变体中保存的引用的有效性约束 |
| Trait 和方法 | 表达参数、接收者与返回引用之间的契约 |

即使没有显式写 `'a`，Rust 仍然会进行生命周期和借用检查。

## 六、为什么需要程序员声明生命周期？

因为生命周期标注不仅用于防止错误，还用于定义接口契约。

例如：

```rust
fn first<'a>(
    a: &'a str,
    _b: &str,
) -> &'a str {
    a
}
```

这声明返回引用只依赖 `a`。

而：

```rust
fn choose<'a>(
    a: &'a str,
    b: &'a str,
    flag: bool,
) -> &'a str {
    if flag { a } else { b }
}
```

这声明两个输入都必须满足返回引用所需的共同有效性约束。

程序员负责表达关系，编译器负责检查实现和调用是否满足这些关系。

## 最终记住四句话

1. 所有权决定谁负责管理数据。
2. 借用允许不拥有数据也能访问它。
3. 生命周期用于保证引用可以被安全使用。
4. 相同的 `'a` 表示共同的有效性约束，不同的 `'a`、`'b` 可以表达独立的约束。

最重要的一句话：

> Rust 用生命周期参数描述引用之间的有效性关系，编译器根据实际代码验证这些关系是否安全。

它不是让多个引用活得一样久，而是让多个引用在需要的范围内满足声明的约束。

我们这次用 8 个由浅入深的例子，重点理解：

- `'a` 到底约束了什么？
- 相同的 `'a` 为什么会让不同引用产生关联？
- 什么时候需要 `'a`、`'b`？
- 为什么有的代码能编译，有的不能？
- 生命周期如何影响结构体和方法设计？

每个例子都从实际代码出发，不先背规则。

## 例子一：没有写 `'a`，生命周期依然存在

```rust
fn main() {
    let name = String::from("Alice");

    let r = &name;

    println!("{}", r);
}
```

这里：

- `name` 拥有字符串。
- `r` 借用字符串。
- 使用 `r` 时，`name` 仍然有效。

所以能编译。

现在修改：

```rust
fn main() {
    let r;

    {
        let name = String::from("Alice");
        r = &name;
    }

    println!("{}", r); // 编译错误
}
```

问题出在：内部作用域结束时 `name` 被销毁，但是后面还要通过 `r` 访问数据。

理解重点：生命周期检查始终存在，即使你一个 `'a` 都没有写。

## 例子二：一个输入引用和一个返回引用

```rust
fn get_name<'a>(name: &'a str) -> &'a str {
    name
}
```

这里的两个 `'a` 是什么关系？

```text
输入引用 name
      │
      │ 'a
      ▼
   返回引用
```

意思是：

返回的引用可以在输入引用所能保证的有效范围内使用。

调用：

```rust
fn main() {
    let name = String::from("Alice");

    let result = get_name(&name);

    println!("{}", result);
}
```

能编译。

但下面的代码不能：

```rust
fn main() {
    let result;

    {
        let name = String::from("Alice");
        result = get_name(&name);
    }

    println!("{}", result); // 错误
}
```

因为返回引用依赖的 `name` 已经销毁。

这里的 `<'a>` 不指定具体的结束位置，而是在函数定义中声明一个参数。

编译器会在调用时检查数据能否支持所需的引用有效范围。

实际上，这个函数可以省略生命周期：

```rust
fn get_name(name: &str) -> &str {
    name
}
```

因为 Rust 能根据生命周期省略规则确定输入与输出的关系。

## 例子三：两个输入引用，但只返回第一个

```rust
fn first<'a>(
    a: &'a str,
    _b: &str,
) -> &'a str {
    a
}
```

注意：

```rust
a: &'a str
```

和：

```rust
_b: &str
```

只有 `a` 与返回引用共享 `'a`。

这意味着：

返回引用的有效性不需要依赖 `b`。

看下面的代码：

```rust
fn main() {
    let a = String::from("Hello");

    let result;

    {
        let b = String::from("World");

        result = first(&a, &b);
    }

    println!("{}", result); // 正常
}
```

执行过程：即使 `b` 已销毁，`result` 仍指向 `a` 的数据。

这个例子体现了生命周期参数的一个重要价值：

你可以只让真正相关的数据约束返回引用，而不让无关的数据限制它。

## 例子四：两个输入引用，可能返回任意一个

```rust
fn choose<'a>(
    a: &'a str,
    b: &'a str,
    flag: bool,
) -> &'a str {
    if flag {
        a
    } else {
        b
    }
}
```

现在两个输入和返回值都使用 `'a`。

因为函数可能返回 `a`，也可能返回 `b`。

我们来比较两种调用。

### 情况 A：可以编译

```rust
fn main() {
    let a = String::from("Hello");

    {
        let b = String::from("World");

        let result = choose(&a, &b, true);

        println!("{}", result);
    }
}
```

使用 `result` 时，`a` 和 `b` 都有效，因此可以满足共同的 `'a` 约束。

### 情况 B：不能编译

```rust
fn main() {
    let a = String::from("Hello");
    let result;

    {
        let b = String::from("World");
        result = choose(&a, &b, true);
    }

    println!("{}", result);
}
```

`result` 的使用超出了 `b` 能支持的范围。

虽然 `flag` 是 `true`，但 `choose` 的接口允许返回 `b`，调用者必须遵守接口约束。

这就是相同 `'a` 的重要含义：这些引用需要满足同一个有效性约束，但它们的实际存活时间不需要完全相同。

## 例子五：结构体只有一个引用字段

我们先不考虑函数返回值。

```rust
struct Person<'a> {
    name: &'a str,
}
```

这表示：

`Person` 保存一个借用的字符串引用，而不是拥有字符串。

使用：

```rust
fn main() {
    let name = String::from("Alice");

    let person = Person {
        name: &name,
    };

    println!("{}", person.name);
}
```

没有问题。

但是：

```rust
fn main() {
    let person;

    {
        let name = String::from("Alice");

        person = Person {
            name: &name,
        };
    }

    println!("{}", person.name); // 编译错误
}
```

原因是：

`person.name` 试图引用已经销毁的 `name` 数据。

所以在结构体中：

```rust
struct Person<'a>
```

是在声明一个生命周期参数，而：

```rust
name: &'a str
```

是在告诉 Rust：这个字段的引用必须满足这个参数表示的有效性约束。

## 例子六：结构体两个字段使用相同的 `'a`

现在增加一个地址字段：

```rust
struct Person<'a> {
    name: &'a str,
    address: &'a str,
}
```

这里有两个引用，却只使用一个 `'a`。

表示两个字段都要满足同一个生命周期约束。

例如：

```rust
fn main() {
    let name = String::from("Alice");
    let address = String::from("New York");

    let person = Person {
        name: &name,
        address: &address,
    };

    println!("{}", person.name);
    println!("{}", person.address);
}
```

完全没问题。

但考虑下面的设计：

```rust
impl<'a> Person<'a> {
    fn get_name(&self) -> &'a str {
        self.name
    }
}
```

返回的姓名引用使用结构体的 `'a`。

现在：

```rust
fn main() {
    let name = String::from("Alice");
    let result;

    {
        let address = String::from("New York");

        let person = Person {
            name: &name,
            address: &address,
        };

        result = person.get_name();
    }

    println!("{}", result); // 编译错误
}
```

虽然 `name` 仍然存在，但 `address` 已经销毁。

由于我们把两个字段绑定到同一个 `'a`，`address` 也必须支持 `result` 所要求的有效范围。

这里出现了一个设计问题：

我们只想返回姓名，为什么地址的存活时间也会限制姓名？

这时就需要两个独立的生命周期参数。

## 例子七：结构体使用 `'a` 和 `'b`

将结构体修改为：

```rust
struct Person<'a, 'b> {
    name: &'a str,
    address: &'b str,
}
```

现在两个字段可以具有独立的生命周期约束。

再定义方法：

```rust
impl<'a, 'b> Person<'a, 'b> {
    fn get_name(&self) -> &'a str {
        self.name
    }

    fn get_address(&self) -> &'b str {
        self.address
    }
}
```

这里：

- `get_name()` 返回引用的有效性依赖 `'a`。
- `get_address()` 返回引用的有效性依赖 `'b`。

现在再试：

```rust
fn main() {
    let name = String::from("Alice");
    let result;

    {
        let address = String::from("New York");

        let person = Person {
            name: &name,
            address: &address,
        };

        result = person.get_name();
    }

    println!("{}", result); // 可以
}
```

为什么现在可以？

因为：

- `result` 指向 `name`。
- `name` 在外部作用域仍然有效。
- `address` 的生命周期 `'b` 不再约束返回的姓名引用。

这里要特别记住：

两个生命周期参数不是为了让两个引用的实际存活时间不同，而是为了让两个引用的有效性关系可以独立表达。

## 例子八：方法返回引用，为什么有时不需要写 `'a`？

看这个方法：

```rust
struct User {
    name: String,
}

impl User {
    fn get_name(&self) -> &str {
        &self.name
    }
}
```

这里完全没有显式生命周期参数。

为什么？

因为方法的返回引用借用的是 `self` 内部的数据。

Rust 的生命周期省略规则可以自动将返回引用与 `&self` 的生命周期关联。

等价于：

```rust
impl User {
    fn get_name<'a>(&'a self) -> &'a str {
        &self.name
    }
}
```

所以：

```rust
fn main() {
    let user = User {
        name: String::from("Alice"),
    };

    let name = user.get_name();

    println!("{}", name);
}
```

可以编译。

但下面的不行：

```rust
fn main() {
    let name;

    {
        let user = User {
            name: String::from("Alice"),
        };

        name = user.get_name();
    }

    println!("{}", name); // 编译错误
}
```

因为 `name` 引用的是 `user` 内部拥有的字符串。

`user` 被销毁后，字符串数据也被销毁。

这里体现的是另一种生命周期关系：方法返回引用依赖于对 `self` 的借用。

## 九、现在把 8 个例子放在一起比较

| 例子 | 核心问题 | 生命周期表达了什么 |
| --- | --- | --- |
| 1. 普通局部引用 | 数据是否仍然有效？ | 编译器自动分析 |
| 2. 一个输入一个输出 | 返回引用依赖谁？ | 输入与输出关联 |
| 3. 两个输入只返回 a | b 能否提前销毁？ | 返回只依赖 a |
| 4. 两个输入返回任意一个 | 哪些来源必须有效？ | 两个输入共同约束输出 |
| 5. 结构体一个字段 | 结构体借用谁的数据？ | 字段依赖外部数据 |
| 6. 结构体共同 `'a` | 为什么一个字段会限制另一个？ | 两个字段关联到同一约束 |
| 7. 结构体 `'a`、`'b` | 如何让引用关系独立？ | 两组独立约束 |
| 8. 方法 `&self` | 返回引用依赖对象吗？ | 返回引用与 self 的借用关联 |

## 最后总结

你现在应该建立的判断方式是：

不要看到 `'a` 就想「它活多久」，而是问「这个引用需要和谁保持有效性关系」。

- `&'a T`：这个引用需要满足 `'a` 的有效性约束。
- 多个 `&'a T`：它们共同满足这个约束。
- `&'a T` 和 `&'b U`：它们可以独立满足不同的约束。
- 函数返回 `&'a T`：返回引用受到 `'a` 对应输入或数据关系的约束。
- 结构体字段使用 `&'a T`：结构体保存了一个受到 `'a` 约束的借用。
- 方法返回 `&str`：可能通过生命周期省略规则与 `&self` 建立关系。

一句话：生命周期参数不是在规定数据活多久，而是在规定引用之间需要满足什么有效性关系；编译器再根据真实代码检查这些关系能否成立。
