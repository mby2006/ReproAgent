"use client";

import { useState } from "react";
import type { CSSProperties } from "react";

type Sample = { index: number; label: number; prediction: number };

function sampleStyle(index: number, count: number): CSSProperties {
  return {
    backgroundSize: `${100 * count}% 100%`,
    backgroundPosition: `${count > 1 ? index * 100 / (count - 1) : 0}% center`,
  };
}

export default function SamplePlate({ samples, count }: { samples: Sample[]; count: number }) {
  const [selected, setSelected] = useState(0);
  const sample = samples[selected];
  return <figure className="sample-plate" aria-label="LeNet-5 测试样本观察板">
    <figcaption className="plate-heading"><b>LeNet-5</b><span>MNIST 测试样本</span></figcaption>
    <div className="plate-observation" aria-live="polite" aria-atomic="true">
      <div className="plate-frame">
        <span className="digit-image" role="img" aria-label={`手写数字 ${sample.label}`} style={sampleStyle(sample.index, count)} />
      </div>
      <dl className="plate-readings">
        <div><dt>人工标注</dt><dd>{sample.label}</dd></div>
        <div><dt>模型预测</dt><dd>{sample.prediction}</dd></div>
      </dl>
    </div>
    <div className="plate-bottom">
      <span>切换样本</span>
      <div className="plate-strip" role="group" aria-label="切换测试样本">
        {samples.map((item, i) => <button key={item.index} type="button"
          aria-label={`查看第 ${item.index + 1} 个测试样本`} aria-pressed={i === selected}
          onClick={() => setSelected(i)}>
          <span aria-hidden="true" className="digit-image" style={sampleStyle(item.index, count)} />
        </button>)}
      </div>
    </div>
  </figure>;
}
