export type ReviewMode = "assisted" | "expert" | "delegated";
export type OrdinaryReviewMode = Exclude<ReviewMode, "delegated">;
export type ConfirmationSource = "human" | "ai_assisted" | "auto";

export interface FieldExplanation {
  field_path: string;
  what: string;
  why: string;
  consequence: string;
  risk: string;
}

export interface FieldRecommendation {
  id: string;
  field_path: string;
  action: "accept" | "modify";
  suggested_value: string | number | boolean | null;
  confidence: number;
  reasoning: string;
  risk: string;
}

export interface ExplainResponse {
  context_hash: string;
  explanations: FieldExplanation[];
  preselected_paths: string[];
  selection_threshold: number;
  usages: Record<string, unknown>[];
}

export interface RecommendResponse {
  context_hash: string;
  recommendations: FieldRecommendation[];
  preselected_paths: string[];
  selection_threshold: number;
  usages: Record<string, unknown>[];
}

export type EvidenceStatus = "paper_explicit" | "paper_described" | "inferred" | "missing";

export interface EvidenceField {
  value: string | number | boolean | null;
  status: EvidenceStatus;
  source_page: number | null;
  source_section: string | null;
  evidence_quote: string | null;
  confidence: number;
  human_confirmed: boolean;
  confirmed_by?: ConfirmationSource | null;
}

export interface ReportedResult extends EvidenceField {
  metric: string;
  condition?: string | null;
}

export interface Baseline extends EvidenceField {
  name: string;
}

export interface ReproductionCard {
  review_mode?: ReviewMode;
  delegated_at: string | null;
  delegated_by: string | null;
  review_decisions: ReviewDecision[];
  method: EvidenceField;
  model_architecture: EvidenceField;
  dataset: EvidenceField;
  data_preprocessing: Record<string, EvidenceField>;
  hyperparameters: Record<string, EvidenceField>;
  optimizer: EvidenceField;
  loss_function: EvidenceField;
  evaluation_metrics: EvidenceField[];
  reported_results: ReportedResult[];
  baselines: Baseline[];
  missing_information: string[];
}

export interface CardVersion {
  id: string;
  paper_id: string;
  version: number;
  origin: "extraction" | "user_edit";
  parent_version_id: string | null;
  card: ReproductionCard;
  created_at: string;
}

export interface CardVersionMeta {
  id: string;
  version: number;
  origin: "extraction" | "user_edit";
  parent_version_id: string | null;
  created_at: string;
}

export interface PaperSummary {
  id: string;
  title: string;
  source: "upload" | "url";
  status: "uploaded" | "parsed" | "failed";
  created_at: string;
}

export type WorkflowStatus =
  | "uploaded"
  | "parsing"
  | "awaiting_spec_review"
  | "planning"
  | "awaiting_plan_approval"
  | "generating_code"
  | "validating_code"
  | "executing"
  | "repairing"
  | "evaluating"
  | "completed"
  | "failed"
  | "cancelled";

/** 与后端 WorkflowStatus 一致：terminal 三态互不推断，cancelled 既非失败也非成功。 */
export const WORKFLOW_TERMINAL_STATUSES: readonly WorkflowStatus[] = [
  "completed", "failed", "cancelled",
];
/** POST /v1/workflows/{id}/cancel 幂等返回当前（可能已是终态的）状态。 */
export interface WorkflowCancelResponse {
  workflow_id: string;
  status: WorkflowStatus;
}

/** 报告接口的稳定错误码；UI 只按 code 分支，不解析中文 message。 */
export type ReportErrorCode =
  | "REPORT_UNAVAILABLE"
  | "REPORT_NOT_READY"
  | "REPORT_GENERATION_FAILED";

export interface WorkflowDetail {
  id: string;
  thread_id: string;
  project_id: string;
  paper_id: string;
  status: WorkflowStatus;
  current_node: string | null;
  experiment_id: string | null;
  run_id: string | null;
  spec_version_id: string | null;
  plan_id: string | null;
  budget_json: Record<string, unknown>;
  error: string | null;
  created_at: string;
  updated_at: string;
}

export interface NodeTrace {
  id: string;
  node_name: string;
  input_json: Record<string, unknown>;
  output_json: Record<string, unknown>;
  duration_ms: number | null;
  model_name: string | null;
  token_usage: Record<string, unknown> | null;
  error: string | null;
  retry_count: number;
  created_at: string;
}

export type ParamSource = "paper_explicit" | "inferred" | "human_confirmed" | "mvp_scaling";
export type PlanOrigin = "generated" | "user_edit" | "oom_retry";
export type AssumptionKind = "inferred" | "missing_proposal" | "mvp_scaling" | "nonstandard_choice";
export type RiskTier = "critical" | "routine" | "info";

export interface PlanParam {
  confidence?: number;
  value: string | number | boolean | null;
  source: ParamSource;
  paper_value?: string | number | boolean | null;
  evidence_quote?: string | null;
  source_page?: number | null;
  rationale?: string | null;
  requires_confirmation: boolean;
  human_confirmed: boolean;
  confirmed_by?: ConfirmationSource | null;
}

export interface Assumption {
  confidence?: number;
  field_path: string;
  kind: AssumptionKind;
  value: unknown;
  rationale: string;
  requires_confirmation: boolean;
  human_confirmed: boolean;
  confirmed_by?: ConfirmationSource | null;
  risk_tier?: RiskTier;
  auto_acceptable?: boolean;
  card_status?: EvidenceStatus | null;
  evidence_quote?: string | null;
  source_page?: number | null;
}

export interface Deviation {
  field_path: string;
  paper_value?: string | null;
  plan_value: string;
  reason: string;
}

export interface DatasetConfig {
  name: string;
  num_classes: number;
  input_shape: number[];
  train_subset_size: number;
  test_subset_size: number;
  full_train_size?: number | null;
  full_test_size?: number | null;
  source: ParamSource;
}

export interface ModelConfig {
  architecture: string;
  input_shape: number[];
  num_classes: number;
  description?: string | null;
  source: ParamSource;
}

export interface PreprocessingStep {
  name: string;
  detail: string;
  source: ParamSource;
  rationale?: string | null;
}

export interface PreprocessingConfig {
  steps: PreprocessingStep[];
}

export interface TrainingConfig {
  optimizer: PlanParam;
  learning_rate: PlanParam;
  loss_function: PlanParam;
  batch_size: PlanParam;
  epochs: PlanParam;
  weight_decay?: PlanParam | null;
  seed: number;
}

export interface EvaluationConfig {
  metrics: string[];
  target_metric: string;
  reported_value?: number | null;
  reported_condition?: string | null;
  reported_source_page?: number | null;
  tolerance: number;
  comparison_note: string;
}

export interface RuntimeBudget {
  max_seconds: number;
  device: string;
  seed: number;
}

export interface ExpectedRuntime {
  estimated_seconds: number;
  basis: string;
}

export interface ResourceEstimates {
  device: string;
  estimated_memory_mb?: number | null;
  estimated_disk_mb?: number | null;
  note?: string | null;
}

export interface ExperimentPlan {
  review_mode?: ReviewMode;
  delegated_at: string | null;
  delegated_by: string | null;
  scaling_profile: string;
  rationale: string;
  dataset_config: DatasetConfig;
  model_config: ModelConfig;
  preprocessing_config: PreprocessingConfig;
  training_config: TrainingConfig;
  evaluation_config: EvaluationConfig;
  runtime_budget: RuntimeBudget;
  expected_runtime: ExpectedRuntime;
  resource_estimates: ResourceEstimates;
  assumptions: Assumption[];
  deviations_from_paper: Deviation[];
}

export interface ReviewDecision {
  id: string;
  field_path: string;
  before: unknown;
  after: unknown;
  confirmed_by?: ConfirmationSource | null;
  review_mode?: ReviewMode | null;
  actor?: string | null;
  timestamp?: string;
  recommendation_id?: string | null;
  recommendation_confidence?: number | null;
  recommendation_reasoning?: string | null;
  recommendation_risk?: string | null;
  preselection_threshold?: number | null;
}

export type PlanChangeRecord = ReviewDecision;

export interface PlanVersion {
  id: string;
  experiment_id: string;
  version: number;
  origin: PlanOrigin;
  parent_version_id: string | null;
  plan: ExperimentPlan;
  change_reason: string | null;
  changes: PlanChangeRecord[];
  related_error: string | null;
  related_run_id: string | null;
  approved_at: string | null;
  created_at: string;
}

export interface PlanVersionMeta {
  id: string;
  version: number;
  origin: PlanOrigin;
  parent_version_id: string | null;
  change_reason: string | null;
  approved_at: string | null;
  created_at: string;
}

export interface ExperimentSummary {
  id: string;
  paper_id: string;
  name: string;
  status: string;
  created_at: string;
}

export interface ExperimentDetail {
  id: string;
  paper_id: string;
  spec_id: string | null;
  name: string;
  status: string;
  created_at: string;
  plan: PlanVersion | null;
}

export type RunStatus =
  | "queued"
  | "running"
  | "succeeded"
  | "failed"
  | "timeout"
  | "cancelled";

export type ErrorCategory =
  | "SYNTAX_ERROR"
  | "IMPORT_ERROR"
  | "SHAPE_MISMATCH"
  | "OUT_OF_MEMORY"
  | "TIMEOUT"
  | "DATA_DOWNLOAD_ERROR"
  | "METRIC_ERROR"
  | "UNKNOWN_ERROR";

export interface RunAccepted {
  run_id: string;
  status: RunStatus;
}

export interface RunSummary {
  id: string;
  experiment_id: string;
  plan_id: string;
  code_version: number;
  plan_version: number;
  status: RunStatus;
  repair_attempt: number;
  exit_code: number | null;
  error_category: ErrorCategory | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  updated_at: string | null;
}

export interface RunAudit {
  image_digest?: string | null;
  python_version?: string | null;
  dependency_lock_hash?: string | null;
  dataset_fingerprint?: string | null;
  seed?: number | null;
  code_hash?: string | null;
  plan_version?: number | null;
  code_version?: number | null;
}

export interface RunDetail extends RunSummary {
  error: string | null;
  code_hash: string | null;
  runner_run_id: string | null;
  metrics_json: Record<string, unknown>;
  audit_json: RunAudit;
  runtime_json: { execution_time?: number };
  stdout_path: string | null;
  stderr_path: string | null;
}

export interface RunLogs {
  stdout: string;
  stderr: string;
}

export interface ArtifactRecord {
  id: string;
  artifact_type: string;
  storage_path: string;
  metadata_json: Record<string, unknown>;
  created_at: string;
}

export interface CodeFileRecord {
  path: string;
  version: number;
  sha256: string;
  content: string | null;
  change_reason: string | null;
  related_error: string | null;
}

export type CheckStatus = "matched" | "deviated" | "unknown" | "not_applicable";

export type FinalStatus =
  | "METHOD_VALIDATED"
  | "SCALED_RUN_COMPLETED"
  | "PARTIAL_MATCH"
  | "DEVIATION_FOUND"
  | "INSUFFICIENT_EVIDENCE"
  | "EXECUTION_FAILED";

export interface EvidenceRef {
  kind: string;
  quote?: string | null;
  source_page?: number | null;
  source_section?: string | null;
  file_path?: string | null;
  field_path?: string | null;
  detail?: string | null;
}

export interface MethodCheck {
  item: string;
  status: CheckStatus;
  paper_value: string | null;
  implementation_value: string | null;
  paper_evidence: EvidenceRef | null;
  implementation_evidence: EvidenceRef | null;
  note: string;
}

export interface ExperimentCheck {
  item: string;
  status: CheckStatus;
  plan_value: string | number | null;
  actual_value: string | number | null;
  plan_evidence: EvidenceRef | null;
  actual_evidence: EvidenceRef | null;
  note: string;
}

export interface MetricComparison {
  metric: string;
  paper_value: number | null;
  experiment_value: number | null;
  absolute_difference: number | null;
  relative_difference: number | null;
  percentage_point_difference: number | null;
  unit: string;
  comparison_status: CheckStatus;
  tolerance: number | null;
  paper_evidence: EvidenceRef | null;
  experiment_evidence: EvidenceRef | null;
  note: string | null;
}

export interface ReportReferences {
  paper_id: string;
  spec_version_id: string;
  spec_version: number;
  experiment_id: string;
  plan_id: string;
  plan_version: number;
  code_version: number;
  code_hash: string | null;
  run_id: string;
  generated_file_ids: string[];
  review_trace_ids?: string[];
}

export interface ParameterComparison {
  key: string;
  label: string;
  paper?: string | null;
  plan?: string | null;
  actual?: string | null;
  paper_evidence?: EvidenceRef | null;
  actual_evidence?: EvidenceRef | null;
  decision?: string | null;
  note: string;
}

export interface EvaluationReport {
  reader_summary?: string | null;
  target_metric?: string | null;
  parameter_comparisons?: ParameterComparison[];
  review_mode?: ReviewMode | null;
  delegated_at?: string | null;
  delegated_by?: string | null;
  review_decisions?: ReviewDecision[];
  review_disclaimer?: string | null;
  references: ReportReferences;
  final_status: FinalStatus;
  summary: string;
  method_checks: MethodCheck[];
  experiment_checks: ExperimentCheck[];
  metric_comparisons: MetricComparison[];
  scaling_deviations: Deviation[];
  missing_information: string[];
  assumptions: Assumption[];
  limitations: string[];
}

export interface RunReport {
  run_id: string;
  report_json: EvaluationReport;
  markdown: string;
  created_at: string;
}
