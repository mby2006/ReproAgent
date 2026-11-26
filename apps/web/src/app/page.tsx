import SiteLink from "@/components/SiteLink";
import PublicHeader from "@/components/PublicHeader";
import SamplePlate from "@/components/SamplePlate";
import { IconArrowRight, IconFlask, IconPapers, IconPulse } from "@/components/icons";
import predictions from "../../public/examples/lenet/prediction_examples.json";
import metrics from "../../public/examples/lenet/metrics.json";
import { isStaticSite } from "@/lib/site";

const example = "/examples/lenet-mnist";
const sampleOrder = [8, 1, 0, 4, 7];
const samples = sampleOrder.map(index => {
  const { label, prediction } = predictions.samples[index];
  return { index, label, prediction };
});

const chapters = [
  { title: "读论文的方法", text: "看模型怎样从带有答案的图片中学习，再用没见过的笔迹参加测试。", href: "#method", icon: IconPapers },
  { title: "看实验的结果", text: `在 ${metrics.evaluated_count.toLocaleString("zh-CN")} 张测试图中，模型认对了 ${metrics.correct_count} 张。查看这次缩小规模的实验能说明什么。`, href: "#results", icon: IconPulse },
  { title: "核对复现条件", text: "并排查看论文设定、实验计划和实际取值，追溯每项差异的依据。", href: "#conditions", icon: IconFlask },
];

export default function Home() {
  return <>
    <PublicHeader />
    <main id="content" tabIndex={-1} className="public-container">
      <section className="home-hero">
        <div className="hero-copy">
          <p className="paper-citation"><span aria-hidden="true" />LeCun 等，1998</p>
          <h1>读懂论文，<br />从一次实验开始。</h1>
          <p className="hero-intro">同一张手写数字，你和模型会给出什么答案？从一次 LeNet-5 实验中，了解模型如何学习，再对照论文核查结果。</p>
          <div className="hero-actions">
            <SiteLink className="btn btn-primary" href={example}>运行 LeNet-5 × MNIST 复现实验 <IconArrowRight /></SiteLink>
            {!isStaticSite && <SiteLink className="text-action" href="/papers">复现自己的论文</SiteLink>}
          </div>
        </div>
        <SamplePlate samples={samples} count={predictions.samples.length} />
      </section>
      <section aria-labelledby="reading-title" className="reading-index">
        <div><h2 id="reading-title">实验阅读指南</h2><p>选择你想了解的部分。</p></div>
        <div className="chapter-list">
          {chapters.map(({ title, text, href, icon: Icon }) => <SiteLink className="chapter" href={example + href} key={href}>
            <Icon className="chapter-icon" /><div><h3>{title}</h3><p>{text}</p></div><IconArrowRight className="chapter-arrow" />
          </SiteLink>)}
        </div>
      </section>
    </main>
    <footer className="public-footer public-container"><span>ReproAgent</span>{!isStaticSite && <SiteLink href="/health">服务状态</SiteLink>}</footer>
  </>;
}
