import SiteLink from "@/components/SiteLink";
import { PredictionReader } from "@/components/PredictionReader";
import PublicHeader from "@/components/PublicHeader";
import ParameterTable from "@/components/ParameterTable";
import { isStaticSite } from "@/lib/site";
import metrics from "../../../../public/examples/lenet/metrics.json";
import predictions from "../../../../public/examples/lenet/prediction_examples.json";
import conditions from "../../../../public/examples/lenet/conditions.json";
import manifest from "../../../../public/examples/lenet/manifest.json";

const pdf = "https://princetonuniversity.github.io/NEU-PSY-502/_static/pdf/Class%204/LeCunn1998.pdf";
const assets = "/examples/lenet";
const number = (n: number) => n.toLocaleString("zh-CN");

export const metadata = {title:"电脑怎样认出手写数字？一次 LeNet-5 小实验 · ReproAgent"};

export default function LeNetExample() {
  return <><PublicHeader example />
  <main id="content" tabIndex={-1} className="example-page public-container space-y-10">
    <header className="example-mast space-y-4">
      <p className="paper-citation"><span aria-hidden="true" />LeNet-5 × MNIST</p>
      <h1 className="example-title">电脑怎样认出手写数字？</h1>
      <p className="text-lg leading-relaxed text-fg-2">想象一张手写支票。两个人写的“5”可能很不一样，金额里的两个数字还可能连在一起。1998 年，LeCun 等人研究了怎样让电脑从样例中学会识别笔迹，并把它用在读取支票这样的任务上。</p>
      <p className="leading-relaxed">我们用简化的 LeNet-5 重做其中一个小实验。给模型一张已经单独截好的数字图片，让它从 0 到 9 中选一个答案。遇到训练时没见过的图片，它能认对多少？</p>
      <a className="text-sm text-info underline" href={pdf+"#page=1"}>阅读论文摘要 · 第 1 页</a>
      <dl className="grid gap-x-8 gap-y-1.5 rounded-xl border border-line bg-bg-2/40 px-4 py-3 text-sm sm:grid-cols-2" aria-label="归档身份信息">
        <div className="flex gap-2"><dt className="text-fg-3">归档 ID</dt><dd><code className="font-mono">{manifest.run}</code></dd></div>
        <div className="flex gap-2"><dt className="text-fg-3">对比组</dt><dd>LeNet-5 × MNIST 缩放复现（缩放版实验，仅复刻论文一小部分）</dd></div>
        <div className="flex flex-wrap gap-2 sm:col-span-2"><dt className="text-fg-3">模型 SHA-256</dt><dd><code className="break-all font-mono">{manifest.model_sha256}</code></dd></div>
        <div className="flex flex-wrap gap-2 sm:col-span-2"><dt className="text-fg-3">镜像 digest</dt><dd><code className="break-all font-mono">{manifest.image}</code></dd></div>
        <div className="flex gap-2"><dt className="text-fg-3">完成时间</dt><dd>{manifest.finished_at}</dd></div>
      </dl>
    </header>
    <nav className="example-nav" aria-label="实验目录">
      <a href="#method">模型怎样学</a><a href="#results">两次考试的成绩</a><a href="#try">自己认一张</a><a href="#evidence">核查实验依据</a>
    </nav>
    <section id="method" className="space-y-4">
      <h2 className="display text-2xl font-semibold">模型怎样学会这件事？</h2>
      <ol className="method-list">
        {[
          ["看带答案的图", "每张训练图都标着正确数字。模型先猜，再根据答案调整内部参数。把所有训练图片看完一遍，叫作一轮。"],
          ["从笔画认数字", "模型在图片的不同位置寻找短线、弯折等局部线索，然后把线索组合起来判断整个数字。"],
          ["换一批图考试", "考试时模型停止调整参数。让它逐张作答，再和每张图的标签核对，就知道它认对了多少张。"],
        ].map(([title,description]) => <li key={title} className="space-y-2">
          <h3 className="font-semibold text-brand">{title}</h3><p className="leading-relaxed text-fg-2">{description}</p>
        </li>)}
      </ol>
      <a className="text-sm text-info underline" href={pdf+"#page=6"}>查看论文中的模型结构 · 第 6 页图 2</a>
    </section>
    <section id="results" className="result-summary space-y-3">
      <h2 className="display text-2xl font-semibold">两次考试，各认错了多少？</h2>
      <p className="leading-loose">这次给模型 {number(metrics.evaluated_count)} 张测试图片。它认对了 <strong className="text-brand">{metrics.correct_count} 张</strong>，认错了 {metrics.evaluated_count - metrics.correct_count} 张。</p>
      <table className="table-lab w-full" aria-label="论文无形变组与本次实验的条件和成绩">
        <thead><tr><th scope="col">比较项</th><th scope="col">论文无形变组</th><th scope="col">本次实验</th></tr></thead>
        <tbody>{[
          ["训练图片", "60,000 张", number(metrics.train_samples) + " 张"],
          ["每张练几遍", "20 遍", metrics.epochs_completed + " 遍"],
          ["测试图片", "10,000 张", number(metrics.evaluated_count) + " 张"],
          ["每 100 张约认错", "1 张（0.95%）", Math.round(metrics.error_rate * 100) + " 张（" + (metrics.error_rate * 100).toFixed(2) + "%）"],
        ].map(([label, paper, actual]) => <tr key={label}><th scope="row">{label}</th><td>{paper}</td><td>{actual}</td></tr>)}</tbody>
      </table>
      <p className="leading-relaxed text-fg-2">为什么用 0.95% 比？原作者还试过把图片拉伸、倾斜，给模型增加新的练习材料，那一组的错误率是 0.8%。本次只用了原始训练图片，所以表中选取无形变组。 <a className="text-info underline" href={pdf+"#page=11"}>核对论文第 11 页</a></p>
      <h3 className="font-semibold">多练几轮，能追上论文吗？</h3>
      <p className="leading-relaxed">简化模型认出了多数测试数字。两次实验用的图片数量、训练轮数、模型结构和更新参数的方法都有差别。要单独看轮数的影响，可以用同一批图片、同一个模型，分别练 4 轮和 20 轮，再做同一套测试。</p>
    </section>
    <div id="try" className="space-y-3">
      <p className="text-fg-2">这里按测试顺序保留了前 {predictions.samples.length} 张图片，包括模型认错的样本。你也可以先猜，再核对它的答案。</p>
      <PredictionReader samples={predictions.samples}/>
    </div>
    <details id="evidence" className="space-y-4">
      <summary className="cursor-pointer py-2 text-lg font-semibold">查看原文依据与运行记录</summary>
      <ParameterTable rows={conditions} pdfUrl={pdf} fileHref={path => assets+"/"+path+(path.endsWith(".py")?".txt":"")}/>
    </details>
    <footer className="space-y-4 border-t border-line pt-5 text-sm">
      <p className="text-fg-2">已完成的隔离容器运行 · 归档 ID <code>{manifest.run}</code> · 完成时间 {manifest.finished_at.slice(0,10)} · 随机种子 {metrics.seed}</p>
      <p className="text-xs leading-relaxed text-fg-3">运行标识是归档 ID；模型 SHA-256 是模型文件指纹，两者不是同一概念。本页为缩放版复现归档，九个归档文件的 SHA-256 可用 manifest.json 离线逐项核验。</p>
      <details className="rounded-xl border border-line bg-bg-2/40 p-3 text-sm">
        <summary className="cursor-pointer font-medium text-fg-2">查看九个归档文件</summary>
        <ul className="mt-3 grid gap-2 sm:grid-cols-2" aria-label="九个归档文件">
          {Object.entries(manifest.files).map(([file, hash]) => <li key={file} className="flex min-w-0 items-center justify-between gap-3">
            <a className="min-w-0 truncate text-info underline" href={`${assets}/${file}`} download>{file}</a>
            <code className="shrink-0 font-mono text-[10px] text-fg-3" title={hash}>{hash.slice(0, 12)}…</code>
          </li>)}
        </ul>
      </details>
      <div className="flex flex-wrap gap-4">
        <a className="btn btn-primary" download href={assets+"/report.md"}>下载实验报告</a>
        <a className="btn btn-ghost" href={assets+"/manifest.json"}>核查产物指纹</a>
        {!isStaticSite && <SiteLink className="btn btn-ghost" href="/papers">登录后运行自己的实验</SiteLink>}
      </div>
    </footer>
  </main></>;
}
