# hwtBlog · 何文涛的个人博客

基于 [Docusaurus 3](https://docusaurus.io/) 构建的个人博客与学习文档站，自动部署到 GitHub Pages。

- 站点地址：<https://stack-call.github.io/hwtBlog/>
- 文档：汇编语言 / 操作系统 / 编译原理 / Rust / x86-64 体系结构 / 计算机历史
- 博客：学习总结与随笔

## 本地开发

需要 Node.js 20 或更高版本。

```bash
npm install      # 首次安装依赖
npm run start    # 启动开发服务器（默认 http://localhost:3000/hwtBlog/）
```

## 构建

```bash
npm run build    # 产物输出到 build/
npm run serve    # 本地预览构建产物
```

## 写文章

| 类型 | 位置 | 说明 |
| --- | --- | --- |
| 文档 | `docs/` | 按目录分专题，侧边栏由目录结构自动生成 |
| 博客 | `blog/YYYY-MM-DD-标题.md` | 文件名必须以 `YYYY-MM-DD` 开头 |
| 图片 | `static/img/` | 引用方式 `![alt](/img/xxx.png)` |

分类的显示名、排序、URL 在各级 `_category_.json` 里配置：

```json
{
  "label": "操作系统",
  "position": 4,
  "link": {"type": "generated-index", "slug": "/category/os"}
}
```

## 部署

推送到 `main` 分支后，GitHub Actions（`.github/workflows/deploy.yml`）会自动构建并发布。

首次使用需要在仓库里做一次性设置：

1. `Settings` → `Pages` → `Build and deployment` → `Source` 选择 **GitHub Actions**。
2. 之后每次 `git push origin main`，Actions 跑完即可访问站点。

> 如果仓库名或用户名变了，记得同步修改 `docusaurus.config.js` 里的
> `url`（`https://<用户名>.github.io`）和 `baseUrl`（`/<仓库名>/`），否则静态资源会 404。

## 目录结构

```
docs/                 文档内容
blog/                 博客文章
src/pages/index.js    首页
src/components/       首页组件
src/css/custom.css    全局样式
static/               静态资源（img、.nojekyll 等）
docusaurus.config.js  站点配置
sidebars.js           侧边栏配置
```
