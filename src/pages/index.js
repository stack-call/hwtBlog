import clsx from 'clsx';
import Link from '@docusaurus/Link';
import useDocusaurusContext from '@docusaurus/useDocusaurusContext';
import Layout from '@theme/Layout';
import Heading from '@theme/Heading';
import HomepageFeatures from '@site/src/components/HomepageFeatures';
import HomepageCategories from '@site/src/components/HomepageCategories';

import styles from './index.module.css';

function HomepageHeader() {
  const {siteConfig} = useDocusaurusContext();
  return (
    <header className={clsx('hero hero--primary', styles.heroBanner)}>
      <div className="container">
        <Heading as="h1" className="hero__title">
          {siteConfig.title}
        </Heading>
        <p className="hero__subtitle">{siteConfig.tagline}</p>
        <p className={styles.heroIntro}>
          这里放两类内容：按知识体系整理的<strong>文档</strong>，和成篇的<strong>博客</strong>。
          文档偏计算机基础与源码阅读，博客偏 AI Agent 的工程实践，以及一些还没想明白的问题。
        </p>
        <div className={styles.buttons}>
          <Link className="button button--secondary button--lg" to="/docs/intro">
            阅读文档
          </Link>
          <Link className="button button--secondary button--lg" to="/blog">
            浏览博客
          </Link>
        </div>
      </div>
    </header>
  );
}

export default function Home() {
  const {siteConfig} = useDocusaurusContext();
  return (
    <Layout title={siteConfig.title} description={siteConfig.tagline}>
      <HomepageHeader />
      <main>
        <HomepageFeatures />
        <HomepageCategories />
      </main>
    </Layout>
  );
}
