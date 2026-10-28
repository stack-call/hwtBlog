import clsx from 'clsx';
import Heading from '@theme/Heading';
import MountainSvg from '@site/static/img/undraw_docusaurus_mountain.svg';
import TreeSvg from '@site/static/img/undraw_docusaurus_tree.svg';
import ReactSvg from '@site/static/img/undraw_docusaurus_react.svg';

import styles from './styles.module.css';

const FeatureList = [
  {
    title: '成体系的文档',
    Svg: MountainSvg,
    description: (
      <>
        汇编语言、操作系统、编译原理、x86-64 体系结构等基础专题，按知识脉络整理成文档，
        而不是零散的笔记。
      </>
    ),
  },
  {
    title: '源码阅读系列',
    Svg: TreeSvg,
    description: (
      <>
        MySQL、Redis、RocketMQ、Transformer、LangChain / LangGraph、DeepAgents：
        对着固定版本的源码逐段读，并区分真实源码节选、教学化简与概念伪代码。
      </>
    ),
  },
  {
    title: '博客与随笔',
    Svg: ReactSvg,
    description: (
      <>
        把 Agent 的成本、权限、沙箱、可观测性这类工程问题写成成篇的总结；
        也放读书笔记和一些还没想明白的问题。
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
