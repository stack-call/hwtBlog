# Autra Runtime 演化 · Docusaurus 文档包

将本目录下 `docs/` 的内容合并到现有 Docusaurus 站点的 `docs/` 目录：

```text
website/
└── docs/
    ├── runtime-execution-evolution.md
    └── assets/runtime-evolution/
        └── 01-roadmap.svg … 10-resource-model.svg
```

文章使用标准 Markdown、Docusaurus front matter 和 admonition；图片是相对路径引用的独立 SVG，无需安装 Mermaid 或其他主题插件。保留相对目录结构。自动生成 sidebar 的站点会发现文章；手动维护 sidebar 的站点需将文档 ID `runtime-execution-evolution` 加入相应栏目。若移动到子目录，ID 也可能带上子目录前缀。

`preview.html` 是同一篇文档的独立 HTML 预览（图片已内嵌），可离线阅读，不是 Docusaurus 的真实构建产物。未安装或启动目标站点，未执行 `npm run build`。

参考：[Docusaurus Front matter](https://docusaurus.io/docs/markdown-features#front-matter) · [Docusaurus Assets](https://docusaurus.io/docs/markdown-features/assets)
