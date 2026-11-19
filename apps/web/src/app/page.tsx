import Link from "next/link";

const PIPELINE = [
  { step: "1", title: "理解论文", desc: "解析 PDF / URL，理解方法与实验设置" },
  { step: "2", title: "提取规格", desc: "结构化复现规格 + 原文证据" },
  { step: "3", title: "实验计划", desc: "制定缩小规模的可执行计划" },
  { step: "4", title: "生成代码", desc: "Agent 生成 Python 实验代码" },
  { step: "5", title: "隔离执行", desc: "Runner 在隔离环境运行，Agent 不碰执行" },
  { step: "6", title: "对比报告", desc: "与论文结果比较，生成可追溯报告" },
];

export default function Home() {
  return (
    <main className="mx-auto w-full max-w-5xl flex-1 px-6 py-12">
      <header className="mb-10">
        <p className="mb-2 text-sm font-medium tracking-widest text-emerald-400">
          PHASE 1 · LeNet-5 + MNIST
        </p>
        <h1 className="text-4xl font-bold tracking-tight">ReproAgent</h1>
        <p className="mt-3 max-w-2xl text-zinc-400">
          上传学术论文 PDF 或提供 URL，自动完成论文理解、规格提取、实验规划、代码生成、
          隔离执行与结果对比，产出可追溯的复现报告。
        </p>
        <div className="mt-6 flex gap-3">
          <Link
            href="/papers"
            className="rounded-lg bg-emerald-600 px-5 py-2.5 text-sm font-medium hover:bg-emerald-500"
          >
            进入工作台：上传论文
          </Link>
          <Link
            href="/health"
            className="rounded-lg border border-zinc-700 px-5 py-2.5 text-sm text-zinc-300 hover:border-zinc-500"
          >
            服务健康状态
          </Link>
        </div>
      </header>

      <section className="mb-10 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {PIPELINE.map((item) => (
          <div
            key={item.step}
            className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-4"
          >
            <div className="mb-2 flex h-7 w-7 items-center justify-center rounded-full bg-emerald-500/15 text-sm font-semibold text-emerald-400">
              {item.step}
            </div>
            <h2 className="font-semibold">{item.title}</h2>
            <p className="mt-1 text-sm text-zinc-400">{item.desc}</p>
          </div>
        ))}
      </section>

      <footer className="mt-12 border-t border-zinc-800 pt-6 text-sm text-zinc-500">
        已实现：统一 LangGraph 工作流（15 节点 / 12 态 / Postgres checkpoint / 预算 / Trace）。
        上传 → 确认 Card → 批准计划 → 隔离执行 → 复现报告，全流程可从前端闭环操作。
      </footer>
    </main>
  );
}