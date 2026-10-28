import React from 'react';
import Link from '@docusaurus/Link';
import Header from '@theme-original/BlogPostItem/Header';
import {useBlogPost} from '@docusaurus/plugin-content-blog/client';

import styles from './styles.module.css';

/**
 * 废弃文章的提示条。
 *
 * frontmatter：
 *   deprecated: true
 *   supersededBy: /blog/xxx        # 可选，替代文章的站内路径
 *   supersededByLabel: 新版标题     # 可选，链接文字，默认「替代版本」
 *
 * 只在文章详情页显示；列表页靠标题的删除线和「已废弃」标记提示。
 */
export default function BlogPostItemHeaderWrapper(props) {
  const {frontMatter, isBlogPostPage} = useBlogPost();

  if (!frontMatter?.deprecated || !isBlogPostPage) {
    return <Header {...props} />;
  }

  const {supersededBy, supersededByLabel} = frontMatter;

  return (
    <>
      <Header {...props} />
      <div className={styles.notice} role="note">
        <strong className={styles.noticeTitle}>本文已废弃</strong>
        <span className={styles.noticeBody}>
          内容已被后续文章替代，仅保留作存档，可能包含过时的结论或链接。
          {supersededBy ? (
            <>
              {' '}
              请阅读
              <Link className={styles.noticeLink} to={supersededBy}>
                {supersededByLabel ?? '替代版本'}
              </Link>
              。
            </>
          ) : null}
        </span>
      </div>
    </>
  );
}
