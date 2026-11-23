export interface PaperReference {
  label: string;
  quote?: string | null;
  source_page?: number | null;
  source_section?: string | null;
  table_id?: string | null;
  metric?: string | null;
  value?: number | null;
  condition?: string | null;
}

export interface PlanParam<T> {
  value: T;
  source: string;
  paper_value?: T | null;
  evidence_quote?: string | null;
  source_page?: number | null;
  rationale?: string | null;
  requires_confirmation?: boolean;
  human_confirmed?: boolean;
  confirmed_by?: string | null;
}

export interface ComparisonGroup {
  key: "a" | "b";
  label: string;
  optimizer: PlanParam<string>;
  learning_rate: PlanParam<number>;
  weight_decay?: PlanParam<number> | null;
  references: PaperReference[];
  reported_result?: PaperReference | null;
}

export type PendingStatus = "pending" | "adopted" | "provided";

export interface PendingParam {
  id: string;
  group_key: "a" | "b" | null;
  field_path: string;
  label: string;
  reason: string;
  impact: string;
  risk_tier: "critical" | "routine" | "info";
  suggested_value: unknown;
  suggestion_source: string | null;
  status: PendingStatus;
  resolved_value: unknown;
  resolved_source: string | null;
}

export interface SupportGap {
  field: string;
  detail: string;
  expectation: string;
}

export interface SupportCheck {
  ok: boolean;
  supported: string[];
  missing: SupportGap[];
}

export interface ComparisonQuestion {
  question: string;
  restatement: string;
  factor: string;
  target_metric: string;
  expected_direction?: string | null;
  requires_mean: boolean;
  mean_basis?: string | null;
}

export interface SharedPlan {
  rationale: string;
  dataset_config: {
    name: string;
    train_subset_size: number;
    test_subset_size: number;
    full_train_size?: number | null;
    full_test_size?: number | null;
  };
  model_cfg: { architecture: string };
  training_config: {
    optimizer: PlanParam<string>;
    learning_rate: PlanParam<number>;
    loss_function: PlanParam<string>;
    batch_size: PlanParam<number>;
    epochs: PlanParam<number>;
    seed: number;
  };
  evaluation_config: { target_metric: string; tolerance: number; metrics: string[] };
  runtime_budget: { max_seconds: number; device: string; seed: number };
}

export interface ComparisonPlan {
  question: ComparisonQuestion;
  base_plan: SharedPlan;
  groups: ComparisonGroup[];
  seeds: number[];
  authorization: {
    max_train_samples: number;
    max_test_samples: number;
    max_epochs: number;
    max_seconds: number;
    max_seeds: number;
    note: string;
  };
  support: SupportCheck;
  pending: PendingParam[];
  paper_references: PaperReference[];
  assumptions: string[];
  limitations: string[];
  notes?: string | null;
}

export interface ComparisonCandidate {
  id: string;
  label: string;
  group_optimizers: string[];
  metric?: string | null;
  references: PaperReference[];
  comparable: boolean;
  note?: string | null;
  blocking: string[];
}

export interface CandidateResponse {
  candidates: ComparisonCandidate[];
  support: SupportCheck;
  paper_references: PaperReference[];
}

export interface VersionSummary {
  id: string;
  version: number;
  origin: string;
  approved_at?: string | null;
  change_reason?: string | null;
  created_at?: string | null;
}

export type ComparisonStatus =
  | "draft"
  | "awaiting_confirmation"
  | "awaiting_approval"
  | "queued"
  | "running"
  | "completed"
  | "incomplete"
  | "failed";

export interface ComparisonSummary {
  id: string;
  status: ComparisonStatus;
  origin: string;
  completed_pairs: number;
  requested_pairs: number;
  error?: string | null;
  created_at?: string | null;
}

export interface ComparisonPlanDetail {
  id: string;
  paper_id: string;
  name: string;
  status: string;
  latest_version: number;
  latest_version_id: string;
  approved_version?: number | null;
  plan: ComparisonPlan;
  versions: VersionSummary[];
  comparisons: ComparisonSummary[];
}

export type RunStatus = "queued" | "running" | "succeeded" | "failed" | "timeout" | "cancelled";

export interface ComparisonRunView {
  id: string;
  seed: number;
  group_key: "a" | "b";
  attempt: number;
  status: RunStatus;
  accuracy?: number | null;
  correct_count?: number | null;
  evaluated_count?: number | null;
  metric_value?: number | null;
  error?: string | null;
  code_hash?: string | null;
  image_digest?: string | null;
}

export interface PairCheckItem {
  item: string;
  status: "matched" | "deviated" | "unknown" | "not_applicable";
  group_a?: string | null;
  group_b?: string | null;
  note: string;
}

export interface PairVerification {
  seed: number;
  verified: boolean;
  checks: PairCheckItem[];
  blocking: string[];
}

export interface SeedComparison {
  seed: number;
  group_a?: ComparisonRunView | null;
  group_b?: ComparisonRunView | null;
  recomputed_accuracy_a?: number | null;
  recomputed_accuracy_b?: number | null;
  delta?: number | null;
  delta_percentage_points?: number | null;
  direction?: string | null;
  verification?: PairVerification | null;
  complete: boolean;
  blockers: string[];
}

export interface ComparisonOutcome {
  status: ComparisonStatus;
  target_metric: string;
  seeds: SeedComparison[];
  mean_a?: number | null;
  mean_b?: number | null;
  mean_delta?: number | null;
  mean_delta_percentage_points?: number | null;
  direction?: string | null;
  completed_pairs: number;
  requested_pairs: number;
  blockers: string[];
  verifications: PairVerification[];
}

export interface ComparisonDetail {
  id: string;
  plan_id: string;
  plan_version: number;
  plan_version_id: string;
  status: ComparisonStatus;
  origin: string;
  source_comparison_id?: string | null;
  question?: string | null;
  requested_pairs: number;
  completed_pairs: number;
  groups: ComparisonGroup[];
  seeds: number[];
  runs: ComparisonRunView[];
  outcome?: ComparisonOutcome | null;
  report_status?: string | null;
  error?: string | null;
  created_at?: string | null;
}

export const GROUP_LABEL: Record<"a" | "b", string> = { a: "A 组", b: "B 组" };

export const SOURCE_LABEL: Record<string, string> = {
  paper_explicit: "原文明确",
  inferred: "推断/待确认",
  human_confirmed: "人工确认",
  mvp_scaling: "缩放配置",
};

export const COMPARISON_META: Record<
  ComparisonStatus,
  { label: string; tone: "ok" | "warn" | "bad" | "info" | "mute" }
> = {
  draft: { label: "草稿", tone: "mute" },
  awaiting_confirmation: { label: "待确认参数", tone: "warn" },
  awaiting_approval: { label: "待批准", tone: "warn" },
  queued: { label: "排队中", tone: "mute" },
  running: { label: "成对执行中", tone: "info" },
  completed: { label: "对照完成", tone: "ok" },
  incomplete: { label: "对照未完成", tone: "warn" },
  failed: { label: "执行失败", tone: "bad" },
};
