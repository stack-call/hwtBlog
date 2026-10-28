import Link from '@docusaurus/Link';
import Heading from '@theme/Heading';

import styles from './styles.module.css';

const CategoryGroups = [
  {
    title: '计算机基础',
    description: '从机器指令到语言实现，按知识脉络整理。',
    items: [
      {label: '汇编语言', to: '/docs/category/assembly'},
      {label: '编译原理', to: '/docs/category/compiler'},
      {label: '操作系统', to: '/docs/category/os'},
      {label: 'x86-64 体系结构', to: '/docs/category/architecture'},
      {label: '计算机相关历史发展', to: '/docs/category/history'},
      {label: 'Rust', to: '/docs/category/rust'},
      {label: 'Leetcode', to: '/docs/category/leetcode'},
    ],
  },
  {
    title: '中间件与存储',
    description: '不只记结论，而是跟着源码走一遍关键路径。',
    items: [
      {label: 'MySQL 源码分析', to: '/docs/category/mysql'},
      {label: 'Redis 源码分析', to: '/docs/category/redis'},
      {label: 'RocketMQ 源码分析', to: '/docs/category/rocketmq'},
    ],
  },
  {
    title: '模型与 Agent',
    description: '从 Transformer 到框架运行时，再到 Agent 工程实践。',
    items: [
      {label: 'Transformer', to: '/docs/category/transformer'},
      {label: 'LangChain 与 LangGraph 源码分析', to: '/docs/category/langchain'},
      {label: 'DeepAgents 源码分析', to: '/docs/category/deepagents'},
      {label: 'DeepAgents 源码分析（新）', to: '/docs/category/deepagents-new'},
      {label: 'AI Agent', to: '/docs/category/agent'},
    ],
  },
];

export default function HomepageCategories() {
  return (
    <section className={styles.categories}>
      <div className="container">
        <div className="text--center">
          <Heading as="h2">内容地图</Heading>
          <p className={styles.lead}>
            共 15 个专题分类。基础、中间件、模型与 Agent 三条线各自成体系，
            也可以按目录顺序从头读。
          </p>
        </div>
        <div className="row">
          {CategoryGroups.map((group) => (
            <div key={group.title} className="col col--4 margin-bottom--lg">
              <div className={styles.card}>
                <Heading as="h3" className={styles.cardTitle}>
                  {group.title}
                </Heading>
                <p className={styles.cardDescription}>{group.description}</p>
                <ul className={styles.list}>
                  {group.items.map((item) => (
                    <li key={item.to}>
                      <Link to={item.to}>{item.label}</Link>
                    </li>
                  ))}
                </ul>
              </div>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}
