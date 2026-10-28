import React from 'react';
import clsx from 'clsx';
import Title from '@theme-original/BlogPostItem/Header/Title';
import {useBlogPost} from '@docusaurus/plugin-content-blog/client';

import styles from './styles.module.css';

/**
 * 在标题前显示「置顶」/「已废弃」标记，并给废弃文章加删除线。
 *
 * - 置顶的排序逻辑在 docusaurus.config.js 的 processBlogPosts 里，这里只负责展示。
 * - 废弃只影响展示，文章仍然可以正常访问。
 */
export default function BlogPostItemHeaderTitleWrapper({className, ...props}) {
  const {frontMatter} = useBlogPost();

  const isPinned = Boolean(frontMatter?.pinned);
  const isDeprecated = Boolean(frontMatter?.deprecated);

  const title = (
    <Title
      {...props}
      className={clsx(className, isDeprecated && styles.deprecatedTitle)}
    />
  );

  if (!isPinned && !isDeprecated) {
    return title;
  }

  return (
    <>
      <span className={styles.badges}>
        {isPinned && (
          <span className={styles.pinnedBadge} title="置顶文章">
            置顶
          </span>
        )}
        {isDeprecated && (
          <span className={styles.deprecatedBadge} title="本文已废弃，仅作存档">
            已废弃
          </span>
        )}
      </span>
      {title}
    </>
  );
}
