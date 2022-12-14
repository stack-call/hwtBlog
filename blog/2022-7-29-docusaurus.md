---
title: Docusaurus的使用总结
description: 使用Docusaurus时的一些总结
slug: docusaurus-summary
authors:
  - name: He Wentao
    title: Docusaurus的使用总结
    url: https://github.com/stack-call
    image_url: https://github.com/stack-call.png
tags: [Docusaurus]
hide_table_of_contents: false
---

# Docusaurus的使用总结


## 安装
```javascript
npm init docusaurus
```
安装过程中使用 <code>classic</code> 模板，不使用<code>TS(typescript)</code>

### 项目结构
```
website-rootdir
├── blog
│   ├── 2021-08-26-welcome
|   |   ├──docusaurus-plushie-banner.jpeg
|   |   └──index.md
|   |
│   ├── 2019-05-28-first-blog-post.md
│   ├── 2019-05-29-long-blog-post.md
│   ├── 2021-08-01-mdx-blog-post.mdx
│   └── authors.yml
├── docs
│   ├── tutorial-basics
|   |   ├── _category_.json
|   |   ├── congratulations.md
|   |   ├── create-a-blog-post.md
|   |   ├── create-a-document.md
|   |   ├── create-a-page.md
|   |   ├── deploy-your-site.md
|   |   └── markdown-features.mdx
│   ├── tutorial-extras
|   |   ├── img
|   |   |   ├──localeDropdown.png
|   |   |   └──docsVersionDropdown.png
|   |   ├── _category_.json
|   |   ├── manage-docs-versions.md
|   |   └── translate-your-site.md
│   └── intro.md
├──node_modules
|   └──...
├── src
│   ├── components
|   |   └──HomepageFeatures
|   |      ├── index.js
│   |      └── styles.module.css
│   ├── css
│   │   └── custom.css
│   └── pages
│       ├── index.js
|       ├── index.module.css
│       └── markdown-page.md
├── static
│   └── img
├── .gitignore
├── babel.config.js
├── docusaurus.config.js
├── package.json
├── package-lock.json
├── README.md
└── sidebars.js
```

* <code>/blog/</code>是博客的存放目录,官方文档说明可以通过修改<code>path</code>修改，但是不知道<code>path</code>到底在哪，可能是修改<code>docusaurus.config.js</code>中的module.exports，在其中添加path选项,也可能是修改其中的<code>to:'blog'</code>。例如：
```js
  module.exports = {
  // ...
  plugins: [
    [
      'content-blog',
      {
        path: 'blog',
        routeBasePath: 'blog',
        include: ['*.md', '*.mdx'],
        // ...
      },
    ],
    'content-pages',
  ],
};
```
* <code>/docs/</code>是文档的存放目录，文档中可以定义侧边栏，而博客中没有这个功能。我们可以在<code>sidebars.js</code>中定义侧边栏。同样可以通过设置<code>path</code>修改目录名称。
* <code>/static/</code>是静态内容，被编译(build)后直接复制到build目录
* <code>/src/pages</code>包含独立页面。
* <code>/docusaurus.config.js</code>是全局配置文件，即站点配置文件。
* <code>/sidebars.js</code>是文档目录的侧边栏的设置文件，在/docusaurus.config.js中被引用

即网站共包含三种网页文件：<code>独立页面，博客文件，文档文件</code>。

### 本地编辑开发
```
cd website-rootdir
npm run start
```
之后，便可以打开网站，但是这只是本地编辑运行。网站最终还是要生成HTML和CSS代码，因此需要构建(build)，也就是编译。

### 构建
```
npm run build
```
之后便会在<code>/build</code>目录下生存HTML和CSS代码，便可以托管在静态网页托管网站部署。

## 配置
><code>在配置文件/docusaurus.config.js</code>中可以配置网站全局的相关值。总的来说，配置有四类：

* 网站的元数据
* 部署配置
* 主题、插件和预设配置
* 自定义配置
  
### 网站的元数据
>可以配置网站的基本数据，也即对网站的基本描述，例如<code>title</code>、<code>url</code>、<code>baseUrl</code> 以及 <code>favicon</code>。

### 部署配置

### 主题、插件和预设配置

>即分别在 <code>themes</code>、<code>plugins</code> 以及 <code>presets</code> 字段中列出网站所用的主题、插件以及预设 。

### 自定义配置
>即添加为定义的自段

## 网站页面的建立
><code>即独立页面、文档页面和博客页面</code>

### 独立页面
><code>/src/pages/</code>可以近似看作网站的根目录。例如在<code>/src/pages/</code>目录下建立文件<code>/src/pages/helloReact.js</code>，那么访问http://localhost:3000/helloreact就可以看到刚刚创建的页面。

:::info

在Windows操作系统中是不区分大小写的，因此<code>helloReact</code>大小写无所谓，但是Linux操作系统作为服务器应该就不可以了。

:::
>同样，我们可以使用Markdown文件或MDX文件建立独立页面。

### 路由
>在<code>/src/pages/</code>中的任何文件会被转换为相应网站页面


* <code>/src/pages/index.js</code> → <code>[baseUrl]</code>
* <code>/src/pages/foo.js</code> → <code>[baseUrl]/foo</code>
* <code>/src/pages/foo/test.js</code> → <code>[baseUrl]/foo/test</code>
* <code>/src/pages/foo/index.js</code> → <code>[baseUrl]/foo/</code>
  
:::tip
网站会默认找<code>index.html</code>
:::

>同时，这也意味着，
* <code>/src/pages/support.js</code>
* <code>/src/pages/support/</code>加上<code>/src/pages/support/index.js</code>

>指向同一个路由
但是后面这种方式会更好，因为可以在其中建立独有的css文件

### 文档页面

文档由
  1. 单页。
  2. 边栏。
  3. 版本。
  4. 插件实例。  

组成

对于文档来说，默认访问结构如下：
```
example.com/                                -> generated from `src/pages/index.js`

example.com/docs/intro                      -> generated from `docs/intro.md`
example.com/docs/tutorial-basics/...        -> generated from `docs/tutorial-basics/...`
...
```

>文档侧栏可以自由定制，我只是使用了默认的侧栏
在默认侧栏的设置中，如果没有指定<code>sidebarPath</code>，那么就会根据docs文件夹下的文档目录默认生成一个目录，要使用这种默认生成的方式，只需要在<code>sidebars.js</code>中
```
module.exports = {
  myAutogeneratedSidebar: [
    {
      type: 'autogenerated',
      dirName: '.', // '.' means the current docs folder
    },
  ],
};
```
但是同时，在<code>/docs/</code>的每个目录下，也需要一个页面来描述当前页面，即<code>\_category\_.json</code>或<code>\_category\_.yml</code>。类似
```
{
  "position": 2.5,
  "label": "Title",
  "collapsible": true,
  "collapsed": false,
  "className": "red",
  "link": {
    "type": "generated-index",
    "title": "Tutorial overview"
  },
  "customProps": {
    "description": "This description can be used in the swizzled DocCard"
  }
}
```
其中的<code>"label": "Title"</code>表示的是docs下的文件夹的路径名。要了解这是什么意思，首先要知道，<code>docusaurus</code>会如何生成文档页面。  
对于直接位于<code>/docs/</code>目录下的文件，例如<code>follow.md</code>，它会生成<code>www.example.com/docs/follow</code>的URL。  
对于<code>/docs/</code>目录下的文件夹及其文件，例如<code>/docs/test/</code>文件夹下，有一个<code>\_category\_.json</code>文件，其中<code>"label": "support"</code>，那么<code>/docs/test</code>文件夹的概览路径的URL就会显示为<code>www.example.com/docs/category/support</code>，并且这个目录在侧边栏显示的名称也是<code>support</code>，而这个文档目录下的文档例如<code>unknown.md</code>的URL会显示为<code>www.example.com/docs/test/unknown</code>。总结来说就是文件夹(在侧边栏显示的名称及概览)的URL由<code>\_category\_.json</code>中的<code>"label"</code>决定，而文件夹中的文件的URL和原本的路径生成规则相同。  
:::tip
同博客页面一样，我们可以通过在文档头加上元信息修改文档在目录中显示的名称和URL。  
修改方式见博客页面内容。
:::

###  博客页面
相对于文档页面，博客页面没有侧边栏的设置，相对来说比较简单。  
在默认情况下。    
博客的URL由其文档名决定。例如<code>/blog/2022-7-29-docusaurus.md</code>这篇博客，其URL为<code>www.example.com/blog/2022/7/29/docusaurus</code>。  
而博客在侧面的<code>Recent posts</code>中生成的标题和<code>Markdown</code>文档中的第一个#标记的内容默认相同。  
然而URL和侧面标题都可以在<code>Markdown</code>文档中指定。通过在文档头加上
```
---
title: Welcome Docusaurus v2
description: This is my first post on Docusaurus 2.
slug: welcome-docusaurus-v2
authors:
  - name: Joel Marcey
    title: Co-creator of Docusaurus 1
    url: https://github.com/JoelMarcey
    image_url: https://github.com/JoelMarcey.png
  - name: Sébastien Lorber
    title: Docusaurus maintainer
    url: https://sebastienlorber.com
    image_url: https://github.com/slorber.png
tags: [hello, docusaurus-v2]
image: https://i.imgur.com/mErPwqL.png
hide_table_of_contents: false
---
```
其中<code>slug</code>指定了URL中本文的的id  
<code>title</code>指定了侧面显示的文章标题，

## 修改样式
有时候，我们对原生的颜色或者样式不满意，那么我们就可以自己写样式。  
在<code>docusaurus.config.js</code>中我们可以看到
```
module.exports = {
  // ...
  presets: [
    [
      'classic',
      /** @type {import('@docusaurus/preset-classic').Options} */
        theme: {
          customCss: require.resolve('./src/css/custom.css'),
        },
      }),
    ],
  ],

};
```
我们可以看到引入了<code>./src/css/custom.css</code>，因此，在该文件中编辑的css样式可以全局通用，因此，我们可以通过Google DevTools查看某个标签的css类，并在<code>./src/css/custom.css</code>中添加修改。如果没有生效可以使用<code>!important</code>提升优先级。

### 路由总结
* 对于一个网站页面来说(博客页面，文档页面)，其路由如果不修改其URL就是其储存的默认位置去掉后缀名，其在侧边的显示就是其第一个<code>#</code>一级标题的内容。如果需要修改则可以在文档中的头部加上元信息。
* 对于一个文件夹页面来说，在目录下的<code>\_category\_.json</code>修改其URL和标题内容。