import clsx from 'clsx';
import Heading from '@theme/Heading';
import MountainSvg from '@site/static/img/undraw_docusaurus_mountain.svg';
import TreeSvg from '@site/static/img/undraw_docusaurus_tree.svg';
import ReactSvg from '@site/static/img/undraw_docusaurus_react.svg';

import styles from './styles.module.css';

const FeatureList = [
  {
    title: '体系化的文档',
    Svg: MountainSvg,
    description: (
      <>
        汇编语言、操作系统、编译原理、x86-64 体系结构等专题，按知识体系整理成文档，
        而不是零散的笔记。
      </>
    ),
  },
  {
    title: '学习与思考的记录',
    Svg: TreeSvg,
    description: (
      <>
        博客里放的是成篇的总结与随笔：读书笔记、源码阅读、以及一些还没想明白的问题。
      </>
    ),
  },
  {
    title: '持续更新',
    Svg: ReactSvg,
    description: (
      <>
        内容基于 Docusaurus 3 构建，推送到 <code>main</code> 分支即自动发布到 GitHub Pages。
      </>
    ),
  },
];

function Feature({Svg, title, description}) {
  return (
    <div className={clsx('col col--4')}>
      <div className="text--center">
        <Svg className={styles.featureSvg} role="img" />
      </div>
      <div className="text--center padding-horiz--md">
        <Heading as="h3">{title}</Heading>
        <p>{description}</p>
      </div>
    </div>
  );
}

export default function HomepageFeatures() {
  return (
    <section className={styles.features}>
      <div className="container">
        <div className="row">
          {FeatureList.map((props, idx) => (
            <Feature key={idx} {...props} />
          ))}
        </div>
      </div>
    </section>
  );
}
