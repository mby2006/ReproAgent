/**
 * Turn internal field paths and enums into words a non-specialist can read.
 *
 * The backend uses stable identifiers (`training_config.learning_rate`,
 * `nonstandard_choice`) because they are stored, diffed and audited. Those
 * identifiers must never reach the screen; this module is the single place
 * that decides how each one is said in plain Chinese.
 */

const FIELD_LABELS: Record<string, string> = {
  "training_config.optimizer": "优化器",
  "training_config.learning_rate": "学习率",
  "training_config.loss_function": "损失函数",
  "training_config.batch_size": "批大小",
  "training_config.epochs": "训练轮数",
  "training_config.weight_decay": "权重衰减",
  "training_config.seed": "随机种子",
  "dataset_config.train_subset_size": "训练样本数",
  "dataset_config.test_subset_size": "测试样本数",
  "dataset_config.name": "数据集",
  "preprocessing_config.centering": "预处理·图像居中",
  "preprocessing_config.deslanting": "预处理·去除倾斜",
  "preprocessing_config.size_normalization": "预处理·尺寸归一化",
  "preprocessing_config.background_extension": "预处理·背景扩展",
  "evaluation_config.tolerance": "允许的误差范围",
  "evaluation_config.target_metric": "目标指标",
  "runtime_budget.max_seconds": "时间预算",
  "runtime_budget.device": "运行设备",
  "model_config.architecture": "模型结构",
  "review_mode": "审核方式",
};

const SUFFIX_LABELS: Record<string, string> = {
  optimizer: "优化器",
  learning_rate: "学习率",
  loss_function: "损失函数",
  batch_size: "批大小",
  epochs: "训练轮数",
  weight_decay: "权重衰减",
  seed: "随机种子",
  train_subset_size: "训练样本数",
  test_subset_size: "测试样本数",
  tolerance: "允许的误差范围",
  max_seconds: "时间预算",
  centering: "预处理·图像居中",
  deslanting: "预处理·去除倾斜",
  size_normalization: "预处理·尺寸归一化",
  background_extension: "预处理·背景扩展",
};

/** `training_config.learning_rate` -> `学习率`. Unknown paths stay readable. */
export function fieldLabel(path: string): string {
  if (FIELD_LABELS[path]) return FIELD_LABELS[path];
  const suffix = path.split(".").pop() ?? path;
  return SUFFIX_LABELS[suffix] ?? suffix.replaceAll("_", " ");
}

/** What kind of gap this setting fills, and what it means for the result. */
export const KIND_HINT: Record<string, { label: string; explain: string }> = {
  inferred: {
    label: "论文没写，系统推测",
    explain: "论文没有给出这个值，系统按同类实验的常见做法取了一个合理默认值。",
  },
  nonstandard_choice: {
    label: "论文做法无法直接实现",
    explain: "论文用的是不常见、没有现成代码的做法，系统换成了一个能跑、效果接近的替代方案。",
  },
  missing_proposal: {
    label: "论文没提，系统补上",
    explain: "论文完全没有提到这一项，系统补了一个值让实验能跑起来。",
  },
  mvp_scaling: {
    label: "为了跑得快而缩小",
    explain: "论文的原始规模在本机跑不完，系统按比例缩小了规模。这会拉低数值，但不改变方法本身。",
  },
};

export function kindHint(kind: string): { label: string; explain: string } {
  return (
    KIND_HINT[kind] ?? {
      label: "系统补充的设置",
      explain: "这一项不是论文原文给出的，由系统补充。",
    }
  );
}

/** Shorten a long evidence quote or paper value for a one-line summary. */
export function shortValue(value: unknown, max = 46): string {
  const text = value == null ? "—" : String(value).replace(/\s+/g, " ").trim();
  if (text.length <= max) return text;
  return `${text.slice(0, max)}…`;
}

const MISSING_GLOSS: Array<[string, string]> = [
  ["weight init", "权重初始化：论文没说参数一开始取什么值。不同取法会影响训练收敛快慢。"],
  ["regulariz", "正则化：论文没说用什么手段防止模型死记训练数据（过拟合）。"],
  ["training sample", "训练样本量：论文没说每个实验具体用了多少样本。"],
  ["language model", "语言模型结构：论文提到的 GTN 里用了什么语言模型，没有给出细节。"],
  ["oversegmentation", "过分割方法：把一整行字切成单个字符的具体规则没写清楚。"],
  ["computational cost", "计算开销：论文没有给出训练这套模型需要多少算力。"],
  ["data augmentation", "数据增强：论文没说训练时对图片做了哪些变形、扩充。"],
  ["preprocessing", "预处理细节：图片怎么归一化、怎么对齐，论文写得不够具体。"],
  ["initialization", "初始化方式：论文没有说明参数怎么初始化。"],
];

/** A one-line Chinese reason for why a missing detail matters. */
export function missingGloss(text: string): string | null {
  const haystack = text.toLowerCase();
  const hit = MISSING_GLOSS.find(([keyword]) => haystack.includes(keyword));
  return hit ? hit[1] : "论文没有给出这一项的具体做法，复现时只能按常规做法补齐。";
}
