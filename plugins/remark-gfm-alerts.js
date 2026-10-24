/**
 * 把 GitHub 风格的 alert 语法转换成 Docusaurus 的 admonition。
 *
 * 输入（GFM，GitHub / VS Code / Obsidian / 语雀 都能识别）：
 *
 *   > [!TIP]
 *   > 这是一条提示。
 *
 *   > [!NOTE] 源码版本
 *   > 标记同一行后面的文字会变成提示框标题。
 *
 * 输出（渲染成站点里带图标的提示框）：
 *
 *   :::tip
 *   这是一条提示。
 *   :::
 *
 * 这样源文件保持纯 Markdown，换平台不会坏；在站点上又能有正常的样式。
 *
 * 支持的类型：NOTE / TIP / IMPORTANT / WARNING / CAUTION
 */

const {visit} = require('unist-util-visit');

// GitHub 的五种类型 -> Docusaurus 主题实际存在的类型
const TYPE_MAP = {
  NOTE: 'note',
  TIP: 'tip',
  IMPORTANT: 'info',
  WARNING: 'warning',
  CAUTION: 'danger',
};

const MARKER = /^\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\][ \t]*/;

/**
 * 去掉段落开头的 [!TYPE] 标记。
 * 同一行剩下的文字作为标题返回，其余内容留在段落里。
 */
function stripMarker(paragraph) {
  const first = paragraph.children[0];
  if (!first || first.type !== 'text') {
    return null;
  }

  const match = first.value.match(MARKER);
  if (!match) {
    return null;
  }

  const [fullMatch, type] = match;
  let rest = first.value.slice(fullMatch.length);

  // 标记同一行、第一个换行之前的文字就是标题
  let title;
  const newlineAt = rest.indexOf('\n');
  if (newlineAt === -1) {
    title = rest.trim();
    rest = '';
  } else {
    title = rest.slice(0, newlineAt).trim();
    rest = rest.slice(newlineAt + 1);
  }

  if (rest === '') {
    paragraph.children.shift();
  } else {
    first.value = rest;
  }

  // 清理标记独占一行时留下的空行
  if (paragraph.children.length === 1 && paragraph.children[0].type === 'text') {
    paragraph.children[0].value = paragraph.children[0].value.replace(/^\n+/, '');
  }

  return {type, title: title || undefined};
}

module.exports = function remarkGfmAlerts() {
  return (tree) => {
    visit(tree, 'blockquote', (node, index, parent) => {
      if (!parent || index === null || index === undefined) {
        return;
      }

      const firstChild = node.children[0];
      if (!firstChild || firstChild.type !== 'paragraph') {
        return;
      }

      const parsed = stripMarker(firstChild);
      if (!parsed) {
        return;
      }

      // 标记所在段落如果被清空，就整段移除
      if (firstChild.children.length === 0) {
        node.children.shift();
      }

      // 交给 Docusaurus 的 admonitions 插件继续处理。
      // title 通过 attributes 传入，插件会优先读它（见 admonitions/index.js）。
      parent.children[index] = {
        type: 'containerDirective',
        name: TYPE_MAP[parsed.type],
        attributes: parsed.title ? {title: parsed.title} : {},
        children: node.children,
      };
    });
  };
};
