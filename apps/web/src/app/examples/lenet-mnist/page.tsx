import Link from "next/link";
import { PredictionReader } from "@/components/PredictionReader";
import PublicHeader from "@/components/PublicHeader";
import ParameterTable from "@/components/ParameterTable";
import metrics from "../../../../public/examples/lenet/metrics.json";
import predictions from "../../../../public/examples/lenet/prediction_examples.json";
import conditions from "../../../../public/examples/lenet/conditions.json";
import manifest from "../../../../public/examples/lenet/manifest.json";

const pdf = "https://princetonuniversity.github.io/NEU-PSY-502/_static/pdf/Class%204/LeCunn1998.pdf";
const assets = "/examples/lenet";
const number = (n: number) => n.toLocaleString("zh-CN");

export const metadata = {title:"用一张手写数字读懂论文 · ReproAgent"};

export default function LeNetExample() {
  return <><PublicHeader example />
  <main id="content" tabIndex={-1} className="example-page public-container space-y-10">
    <header className="example-mast space-y-4">
      <p className="paper-citation"><span aria-hidden="true" />LeNet-5 × MNIST</p>
      <h1 className="example-title">电脑怎样认出手写数字？</h1>
      <p className="text-lg leading-relaxed text-fg-2">同一个数字，每个人的写法都不同。1998 年，LeCun 等人研究了怎样从样例中学会识别文档。这里选取论文中的手写数字实验：你和一个实际训练过的模型，来认同一张图。</p>
      <a className="text-sm text-info underline" href={pdf+"#page=1"}>阅读论文摘要 · 第 1 页</a>
    </header>
    <nav className="example-nav" aria-label="实验目录">
      <a href="#try">认一张图</a><a href="#method">了解方法</a><a href="#results">查看结果</a><a href="#conditions">核查实验依据</a>
    </nav>
    <div id="try"><PredictionReader samples={predictions.samples}/></div>
    <section id="method" className="space-y-4">
      <h2 className="display text-2xl font-semibold">模型怎样学会这件事？</h2>
      <ol className="method-list">
        {[
          ["看带答案的图", "训练时，每张图片都有数字标签。模型先猜，再根据预测与答案的差异调整权重。"],
          ["从局部拼出形状", "卷积层会在图上不同位置寻找局部特征，再逐层组合。这有助于识别位置和笔迹略有变化的数字。"],
          ["用另一批图考试", "测试时固定权重，用训练时没看过的图片作答。把每个预测和标签对照，就能统计它认对了几张。"],
        ].map(([title,description]) => <li key={title} className="space-y-2">
          <h3 className="font-semibold text-brand">{title}</h3><p className="leading-relaxed text-fg-2">{description}</p>
        </li>)}
      </ol>
      <a className="text-sm text-info underline" href={pdf+"#page=6"}>查看论文中的模型结构 · 第 6 页图 2</a>
    </section>
    <section id="results" className="result-summary space-y-3">
      <h2 className="display text-2xl font-semibold">这次小实验学到了什么？</h2>
      <p className="leading-loose">模型用 <a className="text-info underline" href="#condition-training_samples">{number(metrics.train_samples)} 张图片</a>
        学习了 <a className="text-info underline" href="#condition-epochs">{metrics.epochs_completed} 轮</a>。
        在 {number(metrics.evaluated_count)} 张测试图片中，认对了 <strong className="text-brand">{metrics.correct_count} 张</strong>，准确率为 {(metrics.accuracy*100).toFixed(2)}%。
        上方可体验的是这次测试按顺序保存的前 {predictions.samples.length} 张。</p>
      <p className="leading-relaxed text-fg-2">原论文这一组实验用了 60,000 张训练图片。我们还简化了模型，并缩小了测试集。模型能识别许多新图片；要判断哪一项改变导致了错误率差距，还需要逐项控制条件、重新实验。</p>
      <a className="text-sm text-info underline" href={pdf+"#page=10"}>核对论文的数据规模 · 第 10 页</a>
    </section>
    <ParameterTable rows={conditions} pdfUrl={pdf} fileHref={path => assets+"/"+path+(path.endsWith(".py")?".txt":"")}/>
    <footer className="space-y-4 border-t border-line pt-5 text-sm">
      <p className="text-fg-2">已完成的隔离容器运行 · {manifest.finished_at.slice(0,10)} · 模型指纹 <code>{manifest.run}</code> · 随机种子 {metrics.seed}</p>
      <div className="flex flex-wrap gap-4">
        <a className="btn btn-primary" download href={assets+"/report.md"}>下载实验报告</a>
        <a className="btn btn-ghost" href={assets+"/manifest.json"}>核查产物指纹</a>
        <Link className="btn btn-ghost" href="/papers">登录后运行自己的实验</Link>
      </div>
    </footer>
  </main></>;
}
