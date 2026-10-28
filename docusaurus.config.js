// @ts-check
// `@type` JSDoc annotations allow editor autocompletion and type checking
// (when paired with `@ts-check`).

import {themes as prismThemes} from 'prism-react-renderer';

/**
 * 博客排序钩子，用官方 processBlogPosts 在列表渲染前重排。
 *
 * 两个自定义 frontmatter 字段：
 * - `pinned: true` / `pinned: 1` / `pinned: 2`：置顶，数字越小越靠前。
 * - `deprecated: true`：已废弃，沉到列表末尾，但仍然可以访问。
 *
 * 未使用这两个字段的文章保持默认的日期倒序。
 */
function getPinnedRank(post) {
  const pinned = post.metadata.frontMatter?.pinned;
  if (pinned === true) {
    return 0;
  }
  if (typeof pinned === 'number' && Number.isFinite(pinned)) {
    return pinned;
  }
  return Number.POSITIVE_INFINITY;
}

function getDeprecatedRank(post) {
  return post.metadata.frontMatter?.deprecated ? 1 : 0;
}

function processBlogPosts({blogPosts}) {
  return blogPosts
    .map((post, index) => ({
      post,
      index,
      deprecated: getDeprecatedRank(post),
      rank: getPinnedRank(post),
    }))
    // 先按废弃与否分组，再按置顶序号，最后用原始下标兜底保证顺序稳定
    .sort(
      (a, b) =>
        a.deprecated - b.deprecated || a.rank - b.rank || a.index - b.index,
    )
    .map(({post}) => post);
}

/** @type {import('@docusaurus/types').Config} */
const config = {
  title: '何文涛的个人博客',
  tagline: '学习总结 && 生活随笔',
  favicon: 'img/favicon.ico',

  // GitHub Pages 项目页地址：https://stack-call.github.io/hwtBlog/
  // url 只写协议 + 域名，baseUrl 必须是 "/<仓库名>/"（大小写要和仓库名一致）
  url: 'https://stack-call.github.io',
  baseUrl: '/hwtBlog/',

  // GitHub Pages 部署配置
  organizationName: 'stack-call',
  projectName: 'hwtBlog',
  deploymentBranch: 'gh-pages',
  trailingSlash: false,

  onBrokenLinks: 'throw',

  i18n: {
    defaultLocale: 'zh-Hans',
    locales: ['zh-Hans'],
  },

  presets: [
    [
      'classic',
      /** @type {import('@docusaurus/preset-classic').Options} */
      ({
        docs: {
          sidebarPath: './sidebars.js',
          editUrl: 'https://github.com/stack-call/hwtBlog/tree/main/',
          // 让 GitHub 风格的 > [!TIP] 在站点上渲染成提示框
          beforeDefaultRemarkPlugins: [require('./plugins/remark-gfm-alerts')],
        },
        blog: {
          showReadingTime: true,
          blogTitle: '博客',
          blogDescription: '学习总结与生活随笔',
          postsPerPage: 10,
          processBlogPosts,
          beforeDefaultRemarkPlugins: [require('./plugins/remark-gfm-alerts')],
          feedOptions: {
            type: ['rss', 'atom'],
            xslt: true,
          },
          editUrl: 'https://github.com/stack-call/hwtBlog/tree/main/',
          onInlineTags: 'warn',
          onInlineAuthors: 'warn',
          onUntruncatedBlogPosts: 'ignore',
        },
        theme: {
          customCss: './src/css/custom.css',
        },
      }),
    ],
  ],

  themeConfig:
    /** @type {import('@docusaurus/preset-classic').ThemeConfig} */
    ({
      image: 'img/docusaurus-social-card.jpg',
      colorMode: {
        respectPrefersColorScheme: true,
      },
      navbar: {
        title: '主页',
        logo: {
          alt: '主页',
          src: 'img/logo.svg',
        },
        items: [
          {
            type: 'docSidebar',
            sidebarId: 'docsSidebar',
            position: 'left',
            label: '文档',
          },
          {to: '/blog', label: '博客', position: 'left'},
          {
            href: 'https://github.com/stack-call',
            label: 'GitHub',
            position: 'right',
          },
        ],
      },
      footer: {
        style: 'dark',
        links: [
          {
            title: 'Docs',
            items: [
              {label: '文档', to: '/docs/intro'},
              {label: '博客', to: '/blog'},
            ],
          },
          {
            title: 'Community',
            items: [
              {
                label: 'BiliBili',
                href: 'https://space.bilibili.com/500252024',
              },
              {
                label: 'CSDN',
                href: 'https://blog.csdn.net/m0_60737243',
              },
              {
                label: 'Stack Overflow',
                href: 'https://stackoverflow.com',
              },
            ],
          },
          {
            title: 'More',
            items: [
              {
                label: 'GitHub',
                href: 'https://github.com/stack-call',
              },
            ],
          },
        ],
        copyright: `Copyright © ${new Date().getFullYear()} He Wentao. Built with Docusaurus.`,
      },
      prism: {
        theme: prismThemes.github,
        darkTheme: prismThemes.dracula,
      },
    }),
};

export default config;
