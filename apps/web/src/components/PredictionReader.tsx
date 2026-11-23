"use client";

import { useEffect, useRef, useState } from "react";


export interface PredictionExample {
  index: number;
  pixels: string;
  label: number;
  prediction: number;
}

export function PredictionReader({ samples }: { samples: PredictionExample[] }) {
  const [position, setPosition] = useState(0);
  const [answer, setAnswer] = useState<number | "skip" | null>(null);
  const image = useRef<HTMLCanvasElement>(null);
  const sample = samples[position];
  useEffect(() => {
    const ctx = image.current?.getContext("2d");
    if (!ctx || !sample) return;
    const frame = ctx.createImageData(28, 28);
    for (let i = 0; i < 784; i++) {
      const gray = parseInt(sample.pixels.slice(i * 2, i * 2 + 2), 16);
      frame.data.set([gray, gray, gray, 255], i * 4);
    }
    ctx.putImageData(frame, 0, 0);
  }, [sample]);
  if (!sample) return null;
  return <section className="reader">
    <div className="reader-image">
      <canvas ref={image} width={28} height={28} role="img" aria-label="这次测试使用的手写数字"
        className="aspect-square w-40 rounded-xl bg-black" style={{imageRendering:"pixelated"}} />
      <span className="text-sm">测试样本 {position + 1} / {samples.length}</span>
    </div>
    <div className="reader-controls space-y-4">
      <h2 className="display text-2xl font-semibold">你觉得这是几？</h2>
      <p className="text-sm text-fg-2">选一个数字，再看模型的预测和数据集的答案。</p>
      {answer === null && <div className="grid max-w-sm grid-cols-5 gap-2">
        {Array.from({length:10}, (_, n) => <button key={n} className="btn btn-ghost min-h-11"
          onClick={() => setAnswer(n)}>{n}</button>)}
      </div>}
      <div aria-live="polite">
        {answer !== null && <div className="flex flex-wrap gap-4 text-lg">
          {typeof answer === "number" && <span>你猜 <b>{answer}</b></span>}
          <span>模型猜 <b className="text-brand">{sample.prediction}</b></span>
          <span>数据集标注 <b>{sample.label}</b></span>
          <strong>{sample.prediction === sample.label ? "模型认对了" : "模型认错了"}</strong>
        </div>}
      </div>
      {answer === null ? <button className="link-quiet min-h-11" onClick={() => setAnswer("skip")}>直接看答案</button>
        : <button className="btn btn-primary" onClick={() => {setPosition((position + 1) % samples.length); setAnswer(null);}}>
          {position + 1 === samples.length ? "再看第一张" : "试试下一张"}</button>}
    </div>
  </section>;
}

export function PrivatePredictions({runId}: {runId: string}) {
  const [samples, setSamples] = useState<PredictionExample[]>([]);
  const [message, setMessage] = useState("正在读取本次运行的预测…");
  useEffect(() => {
    const controller = new AbortController();
    import("@/lib/api").then(({apiFetch}) => apiFetch<{samples: PredictionExample[]}>(`/v1/runs/${runId}/prediction-examples`, {signal:controller.signal}))
      .then(data => { if (!controller.signal.aborted) setSamples(data.samples); })
      .catch(err => { if (!controller.signal.aborted) setMessage(err.message); });
    return () => controller.abort();
  }, [runId]);
  return samples.length ? <PredictionReader samples={samples}/> : <p className="text-sm text-fg-3">{message}</p>;
}
