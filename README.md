# ReproAgent

科研论文复现 Agent。上传论文 PDF 或提供 URL，系统自动完成：论文理解 → 结构化复现规格提取（含原文证据）→ 缩小规模实验计划 → Python 实验代码生成 → 隔离环境执行 → 与论文报告结果对比 → 可追溯复现报告。

**当前进度**：各模块已整合为**统一 LangGraph 工作流**（15 个业务节点 + 13 态状态机 + Postgres checkpoint + 预算/重试/Trace harness）。上传 → 确认 Card → 批准计划 → 真实训练 → 报告已具备前端闭环能力，但完整 v2 交付仍须由绑定最终 commit 的 G-01–G-05、NFR-05 与 P1 证据判定；静态归档通过不等于真实 Qwen/browser/容器验收通过。Paper Understanding、Experiment Planner、代码生成/执行与 **Reproduction Evaluator / 报告系统**已实现。理解链路：上传 PDF → PyMuPDF 解析（页码/章节/表格）→ Qwen 提取 Reproduction Card → 证据落地校验（页码/引文由解析器回填，LLM 无法伪造）→ 前端双栏证据审阅 + 追加式版本管理。规划链路：Reproduction Card → 确定性规则引擎生成缩小规模实验计划（无 LLM，每个参数带来源溯源）→ 关键假设人工确认门 → 追加式计划版本管理（用户修改 / OOM 降批大小）→ 前端审批 UI。执行链路：已审批计划 → Agent 确定性模板生成代码（train/evaluate/model/config/requirements，无 LLM）→ AST 静态校验 → API/worker 信任域控制 Runner → Runner 在**独立锁定 Docker 兄弟容器**中真实训练 → 采集 stdout/stderr/指标/审计 → 错误分类 + 最多 3 次自动修复（OOM 触发新计划版本而非私改训练目标）→ 前端运行面板（指标/审计/日志/代码版本/产物）。评估链路：运行终态 → **全确定性 Evaluator（无 LLM）**从本次运行固定的代码版本 AST 提取实现事实 → 方法验证（8 项）/实验验证（9 项）/结果对比（含百分点差）→ 6 态最终判定 → 14 节 Markdown 报告自动追加落库 → 前端结构化报告面板 + Markdown 查看/下载。**v2 范围仅为 LeNet-5 × MNIST 缩放实验**；配对比较、OCR、其他模型/数据集不属于 v2 交付或验收证据。缩放实验永远不会被标记为完整复现（Full Reproduction）。

## 访客案例与实验工作台

在首页选择「用一张手写数字读懂论文」，或访问 `/examples/lenet-mnist`。无需登录即可猜数字、核对这份**缩放 LeNet-5 × MNIST 快照自己的持久化模型预测**、跳到论文页和下载条件对照报告；它不是原始 1998 年模型的预测。案例快照来自一次独立的 CPU 容器运行，训练 4,096 张、测试 1,024 张，认对 909 张。页面上的 12 张图片按测试顺序选取。公共归档/run ID 为 `01993dd36ff17102`，模型 SHA-256 为 `01993dd36ff1710272c3c50e16a0ffb10dc2015c08e8080b22b7e480b2c83b47`，报告 SHA-256 为 `5a7fcba954b496bd5e87483f7cad81b7155f048a111bcd81d3e769eca9d7349d`。公开文件及九文件 SHA-256 清单位于 `apps/web/public/examples/lenet/`，可由公共归档 verifier 逐字节复核。

公共静态归档与登录工作台分开：静态页验证证明归档身份和九文件完整，不证明真实 Qwen、数据库、Docker、browser 闭环或最终 commit 验收。工作台仍按用户权限读取私人实验，完整 v2 交付以[下方验收账本](#v2-验收证据绑定)为准。

## 架构

```
┌──────────┐   HTTP    ┌──────────┐   HTTP    ┌──────────┐
│   web    │ ────────> │   api    │ ────────> │  agent   │
│ Next.js  │           │ FastAPI  │           │理解/规划/ │
│  :3000   │           │  :8000   │           │确定性计算 │
└──────────┘           └────┬─────┘           └──────────┘
                            │ API/worker 信任域
                     ┌──────┴──────┐          Qwen LLM API
                     │             │         (统一 LLM Adapter)
                Supabase      Redis + worker ──HTTP──> runner ──Docker──> sibling container
             (PostgreSQL +    (arq：完整工作流、          :8002      一次性锁定执行容器
              Storage)         执行/修复/报告编排)
```

| 服务 | 职责 | LLM Key | Supabase Service Role Key |
|---|---|---|---|
| `apps/web` | 登录、上传、Card 证据审阅/编辑、计划审批、运行面板、**复现报告面板**、健康面板 | ✗ | ✗（仅 anon key + 用户 JWT） |
| `apps/api` | 认证、user ownership、业务数据、任务编排、报告读取/重生成 | ✗ | ✓ |
| `worker` | arq 异步任务（api 同镜像）：持有并驱动**完整 LangGraph 工作流**、执行/修复循环、终态报告；独占 DB 写入并控制 Runner | ✗ | ✓ |
| `apps/agent` | 活跃的论文理解/规划 LangGraph，以及确定性代码生成/静态校验/错误分类/修复/复现评估与报告渲染；不是完整工作流 owner，也不是 Runner proxy | ✓ | ✗ |
| `apps/runner` | 由 API/worker 控制的无密钥沙箱编排器：经挂载 Docker Socket 生成锁定的一次性兄弟执行容器（自身不跑 torch） | ✗ | ✗ |

**硬性边界**

- 完整 LangGraph 位于 **API/worker 信任域**。Agent 只保留活跃的理解/规划图与确定性计算，不拥有完整工作流，不代理 Runner。
- Runner 由 API/worker 控制；生成的训练代码只在 Runner 创建的 sibling container 内执行，绝不在 api/agent/worker 主进程运行。
- 所有权统一经 `projects.user_id` 传递：papers/specs/experiments/runs 等表通过外键链
  归属到 project，全表启用 RLS（`owns_project/owns_paper/owns_experiment/owns_run` helper），
  不依赖前端隐藏数据；API 层另做归属校验（双保险）。
- 所有 Agent/服务间数据交换使用 `packages/schemas` 中的 Pydantic 模型校验（`extra="forbid"`）。
- 长任务一律异步：上传/重新解析返回 `202 + paper_id/workflow_id/thread_id`，由 Redis + arq worker 执行。
  Run 的活跃创建/读取路径是 `POST /v1/experiments/{experiment_id}/runs` 与 `GET /v1/runs/{run_id}`；
  `/v1/jobs` 和 `/v1/reproductions` 已从活跃 surface 退役。
- **证据不可伪造**：LLM 只提出 value + 原文引文；`source_page`/`source_section` 由解析器
  对引文做归一化模糊匹配后回填，LLM 声称的页码一律丢弃。匹配失败的 `paper_explicit`
  降级为 `inferred`（剥掉引文）或 `missing`。
- **版本只增不改**：`reproduction_specs` 为追加式版本表，RLS 只有 select/insert 策略
  （无 update/delete）；重新解析与用户编辑都产生新版本并记录 `parent_version_id`。
  `experiment_plans` 由 BEFORE UPDATE 触发器锁定除 `approved_at` 外的所有列；
  `generated_files` 追加式，每次修复生成新版本并记录 `sha256`/`change_reason`/`related_error`。
  `reproduction_reports` 按 `(run_id, version)` 追加，重生成不覆盖旧版本；`execution_run_files`
  只是 final-attempt 投影，完整的 `execution_attempts` / `execution_attempt_files` 历史单独保存。

## 执行沙箱安全模型

生成代码**绝不**在 api/agent/worker 主进程中执行。Runner 通过挂载的 `/var/run/docker.sock`
调用 Docker SDK，为每次运行生成一个**兄弟容器**（非 privileged），运行结束即强制销毁：

- **镜像**：`reproagent-exec`（`scripts/build_exec_image.sh` 构建）。CPU 版 torch/torchvision
  与 **MNIST 数据集烘焙进镜像**，运行时零下载；镜像 label 记录 digest，数据集指纹为烘焙文件 sha256。
- **锁定**：非 root 用户（1000:1000）、`network_mode=none`（无外网）、`cap_drop=ALL`、
  `no-new-privileges`、只读根文件系统 + tmpfs `/tmp`、`mem_limit`/`memswap_limit`、
  `nano_cpus`、`pids_limit`、`storage_opt size`（磁盘配额，宿主不支持时优雅降级）。
- **硬超时**：`container.wait(timeout)` 到点强杀（默认上限 600s），超时归类 `TIMEOUT`。
- **无密钥**：执行容器不注入任何宿主凭证；环境变量仅含运行所需配置。集成测试断言容器内
  读不到 `QWEN_API_KEY`/`SUPABASE_SERVICE_ROLE_KEY`，也无法访问未挂载的宿主文件。
- **依赖白名单**：`requirements.txt` 由确定性模板生成、固定版本，静态校验拒绝白名单外依赖；
  禁止 Agent 任意 `pip install`。
- **静态校验**（执行前）：AST 检查语法、入口、文件路径、依赖白名单、必需配置、指标输出接口，
  并拒绝访问宿主/环境变量/密钥/任意网络的代码。
- **审计**：每次运行落盘镜像 digest、Python 版本、依赖锁 hash、数据集指纹、随机种子、
  代码 hash、计划版本、stdout/stderr、指标、exit code（`execution_runs.audit_json` 等）。
- **自动修复**：错误按 8 类确定性分类（SYNTAX/IMPORT/SHAPE_MISMATCH/OUT_OF_MEMORY/TIMEOUT/
  DATA_DOWNLOAD/METRIC/UNKNOWN）。最多 3 次修复，每次重渲染规范模板→新版本→重跑静态校验→重执行；
  **OOM 由 Planner 生成新计划版本（降批大小，`origin=oom_retry`）**，而非 Coding Agent 私改训练目标。

## 复现评估与报告（Reproduction Evaluator）

运行到达终态（成功/失败/超时/取消）后，Worker **自动**调用 Agent 的确定性 Evaluator
追加报告版本到 `reproduction_reports`（按 `run_id + version` 唯一）；也可经
`POST /v1/runs/{id}/report` 手动重生成并追加新版本，不覆盖历史。**整条判定链路无 LLM 参与**：

- **实现事实提取**：从本次运行最终 attempt 精确绑定的代码版本（历史在
  `execution_attempt_files`，`execution_run_files` 维护 final-attempt 投影）AST 解析 model.py/train.py + config.yaml，提取卷积/全连接/池化/激活/损失/预处理/数据集事实。
  修复后的代码若偏离规范实现，会被如实检出，而非信任模板描述。
- **方法验证（8 项）**：Architecture / Layer configuration / Activation / Subsampling /
  Output layer / Loss Function / Preprocessing / Dataset。每项输出
  `matched / deviated / unknown / not_applicable`，并附论文证据（引文+页码）与代码证据（文件+事实）。
  论文侧缺证据一律 `unknown`，绝不猜测。
- **实验验证（9 项）**：Training samples / Test samples / Epochs / Batch size / Optimizer /
  Learning rate / Random seed / Dataset fingerprint / Runtime environment。
  计划值 vs 真实运行数据（metrics.json / audit_json / 固定的 config.yaml）。
- **结果对比**：Accuracy / Loss / Runtime / 其他指标。记录论文值、实验值、绝对差、相对差、
  **百分点差（percentage points）**、单位与对比状态。百分制/错误率自动换算为 accuracy 小数
  （错误率换算依据原文引文中的 percent/% 证据）。tolerance 仅作**趋势参考**：
  数值接近不等于复现成功，超出容差也不构成缩放实验的失败定论。
- **最终状态（6 态决策树）**：`EXECUTION_FAILED` → `DEVIATION_FOUND` →
  `INSUFFICIENT_EVIDENCE` → `PARTIAL_MATCH` → `METHOD_VALIDATED` → `SCALED_RUN_COMPLETED`。
  **枚举中不存在 Full Reproduction**；任何状态的 summary 都声明缩放实验不构成完整复现判定。
- **Markdown 报告（14 节）**：Paper Overview / Reproduction Objective / Method Summary /
  Experimental Setup / Scaling Strategy / Implementation Details / Execution Environment /
  Results Comparison / Method Deviations / Missing Information / Agent Assumptions /
  Known Limitations / Final Assessment / Code and Execution Trace References。
- **证据硬约束**：所有实验数值来自真实 `execution_runs`（metrics/audit/runtime），
  所有论文数值来自证据落地的 Card `reported_results`，不允许 LLM 编造任何指标。
  每份报告关联 Paper ID / Spec Version / Plan Version / Code Version / Run ID /
  固定代码文件 ID / stdout/stderr 存储路径，可完整追溯。

## 统一工作流（LangGraph Harness）

整条复现链路由**单一 LangGraph 图**编排（`apps/api/.../workflow/`），图运行在 api/worker 信任域
（持有 Supabase 密钥并驱动 Runner）；agent 仅负责 LLM/确定性计算，runner 仅负责隔离执行，二者均不持有彼此密钥。

**15 个节点**：`ingest_paper → extract_paper → build_reproduction_spec → validate_evidence →
human_review → plan_experiment → human_approve_plan → generate_code → validate_code →
execute_code → classify_error → repair_code → evaluate_result → generate_report → persist_results`。
其中 `human_review` / `human_approve_plan` 用 `interrupt()` 暂停，等待前端确认后经 `Command(resume=)` 继续；
`validate_code` / `execute_code` / `classify_error` 后按条件路由（校验失败即停、成功→评估、失败→分类→修复或评估）。

**13 态状态机**（前端 `WorkflowStepper` 实时展示）：`uploaded / parsing / awaiting_spec_review /
planning / awaiting_plan_approval / generating_code / validating_code / executing / repairing /
evaluating / completed / failed / cancelled`。`completed / failed / cancelled` 是三个独立终态。

**Harness 能力**：
- **Checkpoint**：每个节点经 `AsyncPostgresSaver` 落 checkpoint（`thread_id == workflow_id`，Supabase Postgres），
  人工门与崩溃均可从最后完成节点恢复；worker 重启时 `recover_workflow_task` 自动重驱非终态、非待确认的工作流。
- **Retry Policy**：最多 3 次自动修复；超时不可无限重试；OOM 触发新计划版本（降批大小，`max_replans=2`）而非私改训练目标；失败时保留完整 checkpoint 状态并置 `failed`。
- **Budget Control**：`BudgetConfig` 限制 LLM 调用次数 / 总 Token / 实验时长 / 容器资源（CPU/内存/超时），`BudgetState` 累计实际消耗，超限即终止并落 `failed`。
- **Trace**：每节点写 `agent_traces`（`node_name / input / output / duration_ms / model_name / token_usage / error / retry_count`），**输入输出经凭据脱敏**（`pdf_b64`、`sk-`、service_role 等永不落 Trace）。

**前端闭环**：上传论文 → 查看 Reproduction Card → 确认缺失参数（`/card/edits` + `/card/confirm`）→
`resume(spec)` → 查看 Experiment Plan → 确认关键假设并批准（`/plans` + `/plans/{v}/approve`）→
`resume(plan)` → 工作流自动执行 → 实时日志 / 训练指标 / 自动修复记录 / Reproduction Report。
工作流驱动模式下执行由图自动发起，前端运行面板轮询展示，无需手动点击执行。

## 非 v2 扩展：优化器对照（成对实验）

仓库保留了配对比较的扩展代码与测试，但它**不属于当前 v2 交付范围，也不能作为 v2 验收证据**。当前 v2 仍只接受 LeNet-5 × MNIST 缩放实验；OCR、其他模型/数据集同样不在范围。以下仅说明 dormant/extension 设计，不表示已纳入本次验收：

**流程**：论文 Card 确认后 → `POST /v1/papers/{id}/comparison-candidates` 定位原文中的两组
（结果按"同一行内的优化器"归属，同名指标不会串组）→ `POST /v1/papers/{id}/comparison-plans`
生成计划 v1 → 待确认项可"一键采用建议"或补充依据/页码，保存为 v(n+1)（原版本不可改）→
`.../versions/{v}/approve` → `.../versions/{v}/start`（`Idempotency-Key` 保证重复提交只入队一次）
→ Worker 按种子逐对执行 → 自动核验 + 报告。

**硬性保证**（由代码而非文案）：

- 缺参不静默默认：`PendingParam` 带建议值、来源与"可能改变排名"的影响说明，未处理不能批准；
  状态随计划版本持久化，刷新/离开后可恢复。
- 差值只在配对通过后计算：两组都成功且**数据划分 / 初始权重 / 样本顺序 / 训练预算 /
  评估条件 / 代码模板**六项实测一致，才产生 `delta`；任一项缺失记为"未核实"，不视为通过。
- 准确率独立重算：`correct_count / evaluated_count`，训练脚本自报的 `metric_value` 只作交叉对照。
- 失败只补跑失败组（同一配置最多 2 次）；需要改共同批大小等条件时，必须新建计划版本并整对重跑，
  原成绩保留。
- 训练代码自报摘要：`metrics.json.reproducibility` 含 split / init weights / sample order /
  template 四个 sha256，由沙箱内的训练脚本计算，编排器无法代填。

**报告与产物**：Markdown 报告 11 节（问题、论文依据、共同条件、两组差异、待确认处理、缩放授权、
实测结果、配对核验、关键假设、局限、产物引用）；`GET /v1/comparisons/{id}/export` 返回 zip
（manifest / plan / report / 每对运行的代码、日志、metrics、model.pt、审计）。重跑沿用获批配置与
种子；原镜像缺失时明确提示缺项，可另建使用当前环境的对照。

设计与测试矩阵见 `docs/comparison-design.md`。

## 数据模型

迁移位于 `infra/supabase/migrations/`，当前有 **16 个 canonical migrations**；
`deploy/server/supabase/migrations/` 有 **16 个逐文件 SHA-256 相同的 tracked deploy copies**。
所有权根为 `projects.user_id`，其余表经外键链传递归属：

| 表 | 归属链 | 说明 |
|---|---|---|
| `projects` | `user_id` 直接 | 所有权根；每用户一个 `is_default` 默认项目（部分唯一索引） |
| `papers` | `project_id → projects` | PDF 存 Storage，`pdf_path`/`file_sha256`/`metadata_json`/`status` |
| `reproduction_specs` | `paper_id → papers` | Card 版本，`spec_json` + `origin` + `parent_version_id`；**追加式**（仅 select/insert） |
| `understanding_jobs` | `paper_id → papers` | 旧异步解析任务表（已由统一 `workflows` 取代，保留兼容） |
| `workflows` | `project_id → projects` | 统一工作流：`thread_id`/`status`(13 态，含 `cancelled`)/`current_node`/`experiment_id`/`run_id`/`spec_version_id`/`plan_id`/`budget_json`/`error` |
| `experiments` | `project_id → projects` | 复现实验，关联 paper/spec |
| `experiment_plans` | `experiment_id → experiments` | 版本化计划，`config_json`/`budget_json`/`approved_at` |
| `generated_files` | `experiment_id → experiments` | 代码文件版本，`sha256`/`change_reason`/`related_error`；**追加式** |
| `execution_runs` | `experiment_id → experiments` | 运行记录，`plan_id`/`code_version`/`plan_version`/`metrics_json`/`runtime_json`/`error_category`/`error`/`repair_attempt`/`code_hash`/`runner_run_id`/`audit_json` |
| `execution_run_files` | `run_id → execution_runs` | **final-attempt 投影**（`run_id`,`generated_file_id`）；历史由 `execution_attempts` / `execution_attempt_files` 单独追加保存 |
| `execution_artifacts` | `run_id → execution_runs` | 运行产物（Storage 路径 + attempt + 元数据） |
| `reproduction_reports` | `run_id → execution_runs` | 按 `(run_id, version)` 追加的不可变报告，含 `report_json` / `markdown` / `attempt` |
| `agent_traces` | `workflow_id → workflows` | 节点级可观测性（`input/output/duration_ms/model_name/token_usage/error/retry_count`，凭据脱敏），**追加式** |

Storage 私有 bucket `papers`（对象路径 `{user_id}/{paper_id}.pdf`，按首段 `{user_id}` 文件夹做 RLS）。
运行日志/产物存私有 bucket `runs`（路径 `{run_id}/attempt-{n}/...`），不对客户端直接开放，
统一由 api 经 service role 读取并通过 ownership 校验的端点（`/runs/{id}/logs`、`/artifacts`、`/files`）返回。

## 目录结构

```
apps/
  web/       Next.js + TypeScript 前端
  api/       FastAPI 公共网关（reproagent_api）
  agent/     FastAPI + LangGraph Agent 服务（reproagent_agent）
  runner/    FastAPI 隔离执行 Worker（reproagent_runner）
packages/
  schemas/   共享 Pydantic 契约（reproagent_schemas）
  paperparse/ PDF 解析（PyMuPDF）+ OCR 扩展接口 + 证据落地匹配（reproagent_paperparse）
infra/
  docker/    各服务 Dockerfile
  supabase/  Supabase 配置与 SQL 迁移（RLS + Storage bucket）
scripts/
  smoke_test.py        全链路冒烟测试（健康 + 规划）
  smoke_execution.py   执行链路验收（真实容器训练 + 审计 + 隔离）
  build_exec_image.sh  构建 reproagent-exec 沙箱镜像（烘焙 MNIST + 非 root）
tests/
  integration/         需 supabase start / 真实 Docker 的集成测试
```

Python 侧为 uv workspace（根 `pyproject.toml`），前端为 pnpm workspace。

## 前置条件

- Node.js ≥ 22、pnpm ≥ 10
- Python 3.12、[uv](https://docs.astral.sh/uv/)
- Docker Desktop（含 Compose v2）
- [Supabase CLI](https://supabase.com/docs/guides/local-development/cli/getting-started)（`npm i -g supabase` 或 scoop）
- 一个 Qwen API Key（[DashScope](https://dashscope.console.aliyun.com/)）。留空不影响服务启动，
  但论文解析任务会在 LLM 提取一步失败；填入 `.env` 的 `QWEN_API_KEY` 后重启 agent/worker 即可。

## 从零启动（Docker 模式，推荐）

```bash
# 1. 安装依赖
uv sync                 # Python workspace（含 dev 依赖）
pnpm install            # 前端 workspace

# 2. 启动本地 Supabase（首次会拉取镜像，较慢）
cd infra
supabase start          # 结束后打印 API URL 与密钥
cd ..

# 3. 配置环境变量：复制模板并填入上一步打印的密钥
cp .env.example .env
#    Supabase CLI v3 输出 Publishable / Secret 两个密钥：
#    Publishable → SUPABASE_ANON_KEY，Secret → SUPABASE_SERVICE_ROLE_KEY

# 4. 应用数据库迁移（supabase start 已自动应用 migrations/，
#    如修改过迁移可执行：cd infra && supabase migration up）

# 5. 构建沙箱执行镜像（烘焙 CPU torch + MNIST，首次较慢；需可访问 PyPI/pytorch 镜像）
bash scripts/build_exec_image.sh

# 6. 构建并启动全部服务
docker compose build
docker compose up -d

# 7. 验证
uv run python scripts/smoke_test.py        # 健康 + 规划链路
uv run python scripts/smoke_execution.py   # 真实容器执行验收（需 exec 镜像 + 栈已启动）
```

打开 http://localhost:3000 可看到首页与各服务健康面板。

> Runner 不发布端口。验证 API/worker → Runner 链路：`curl http://localhost:8000/health/dependencies`。

## 生产发布、备份与回滚

以下命令都在服务器仓库 `/opt/reproagent` 中执行，且已经完成 `deploy/server/gen_secrets.sh`。
生产数据位于 `/var/lib/reproagent/`；不要对正在运行的数据库直接复制数据目录。

### 数据库迁移

```bash
cd /opt/reproagent
./deploy/server/bootstrap.sh migrate
```

迁移器只执行 `infra/supabase/migrations/` 中尚未记账的 SQL：每个文件在一个事务内执行，
事务内持有 Postgres advisory lock，并写入 `public.schema_migrations` 的版本与 SHA-256。
重复执行同一版本是 no-op；已应用文件内容发生变化会直接失败，不能靠改历史迁移“打补丁”。
有待执行迁移时，迁移器先停止 api/worker/agent/runner，再创建
`/var/lib/reproagent/backups/pre-migration/<时间>-<commit>/`，校验数据库 dump 和 Storage tar
后才开始迁移；快照失败则迁移不执行。成功后恢复原容器；迁移失败时应用保持停止，需人工检查
`public.schema_migrations` 和失败原因后再决定启动或恢复快照。

旧版 bootstrap 没有版本表。升级到本流程时，先备份并人工确认当前 schema，再显式接纳历史版本：

```bash
uv run python deploy/server/ops.py backup
uv run python deploy/server/ops.py verify /var/lib/reproagent/backups/<本次备份目录>
./deploy/server/bootstrap.sh migrate --adopt-existing
```

接纳操作只把当前迁移文件登记为“已应用”，**不验证历史 SQL 是否真的执行过**；只有确认现有
`public.papers` 等表与迁移末尾状态一致时才可使用。全新空数据库不要加这个参数。

### 定期备份与异地副本

```bash
cd /opt/reproagent
uv run python deploy/server/ops.py backup
uv run python deploy/server/ops.py verify /var/lib/reproagent/backups/<本次备份目录>
rsync -a /var/lib/reproagent/backups/ /mnt/offsite/reproagent/backups/
```

`backup` 会先停止 api/worker/agent/runner，避免应用继续写 Storage；无论成功失败都会按原容器
配置重新启动它们。备份包含 Postgres custom-format dump、Storage tar、迁移清单与文件 SHA-256。
`verify` 会重算校验和并用 `pg_restore --list`、tar 结构检查验证文件可读，但它不等同于完整恢复演练。
备份含用户数据与密码哈希，必须限制目录权限并只进入加密的异地存储；`.env.prod` 另用服务器现有
机密管理流程保存，不要明文塞进备份目录。

建议在 root 的 crontab 中加入每天备份和异地同步；`/mnt/offsite` 必须是已经挂载的异地加密存储，
未挂载时同步命令会失败并写日志，不会假装备份成功：

```cron
17 3 * * * cd /opt/reproagent && uv run python deploy/server/ops.py backup >> /var/log/reproagent-backup.log 2>&1
20 3 * * * test -d /mnt/offsite/reproagent && rsync -a /var/lib/reproagent/backups/ /mnt/offsite/reproagent/backups/ >> /var/log/reproagent-backup.log 2>&1
```

脚本不自动删除旧备份或旧镜像；确认异地副本和恢复演练通过前不要配置清理策略。

### 健康检查、指标与告警

`/health` 是低成本存活检查；`/ready` 只读后台探测的缓存，不阻塞在慢依赖上。API 每 15 秒并行检查
agent、Supabase Auth、Postgres、Storage、Redis、worker 心跳和 Runner；缓存超过 45 秒或任一依赖异常，
`/ready` 返回 503。worker 每 15 秒写入 60 秒过期的 Redis 心跳。人工诊断会实时执行完整依赖检查：

```bash
cd /opt/reproagent
./deploy/server/bootstrap.sh status
uv run python deploy/server/ops.py health
curl -fsS http://127.0.0.1:8000/health/dependencies
```

`ops.py health` 在未就绪或依赖降级时退出码为 1，可直接接入服务器巡检或外部监控。外部 uptime 监控轮询
公网 `/ready` 即可；连续失败告警比单次失败更可靠。仓库不内置邮件/Webhook 发送器，告警渠道由现有监控
平台配置。不要把 `/metrics` 当公开端点：`.env.prod` 没设置 `OPS_TOKEN` 时它返回 404，设置后必须带内部令牌：

```bash
cd /opt/reproagent
OPS_TOKEN=$(sed -n 's/^OPS_TOKEN=//p' deploy/server/.env.prod | head -1)
curl -fsS -H "X-Ops-Token: $OPS_TOKEN" http://127.0.0.1:8000/metrics
unset OPS_TOKEN
```

指标目前只有进程内 HTTP 请求数和耗时总和，按 method/route template/status 低基数聚合，最多 100 条序列；
进程重启会清零，没有直方图、队列积压跨实例聚合或历史分位数，不能替代日志与数据库运行记录。访问日志只记录
请求 ID、路由模板、状态和耗时，不记录查询串（包括 PDF 签名链接）或请求体。API/worker 的结构化 JSON 日志由
`LOG_FORMAT=json` 开启；所有生产容器使用 10MB × 3 的 json-file 轮转。数据库记录 warning、超过 1 秒的语句和锁等待：

```bash
docker logs --since 30m reproagent-prod-api-1
docker logs --since 30m reproagent-prod-worker-1
docker logs --since 30m supabase-db-1
```

日志可能包含业务错误文本，分享前仍需脱敏；不要开启 HTTP 请求体日志。容器 unhealthy 先看
`/health/dependencies` 区分的依赖，再决定重启；readiness 失败不应触发无限自动重启。以上配置需要重建容器才生效，
本次离线验证没有实际执行容器健康检查、Prometheus 抓取或告警投递。

### 应用发布与应用回滚

提交并推送本次发布代码后，在服务器拉取到该 commit，保持工作区干净，然后执行：

```bash
cd /opt/reproagent
git pull --ff-only
./deploy/server/bootstrap.sh migrate
uv run python deploy/server/ops.py release "$(git rev-parse HEAD)"
./deploy/server/bootstrap.sh gate
```

`release` 拒绝脏工作区和非当前 HEAD，构建并使用完整 commit SHA 标记 api/worker/agent/runner
和执行镜像；已存在的 SHA 镜像直接复用，不覆盖为 `latest`。发布记录在
`/var/lib/reproagent/releases/`，旧 tag 保留。执行镜像首次构建仍需要 PyTorch 镜像源与 MNIST 下载。

应用异常、但数据库 schema 仍兼容上一版本时：

```bash
cd /opt/reproagent
uv run python deploy/server/ops.py rollback <上一个完整commit-SHA>
./deploy/server/bootstrap.sh gate
```

回滚前会检查发布记录和全部五个镜像是否存在。它**只回滚容器镜像，不回滚数据库**；如果新版本
已经写入不兼容数据，必须先写向前兼容的修复迁移，不能只退回旧镜像。不要执行会清理旧 tag 的
Docker prune，否则会破坏回滚能力。

### 数据库恢复与恢复演练

`restore` 会覆盖当前数据库，属于破坏性操作。优先使用向后兼容的修复迁移；确需回滚时，先确认
Redis 队列可以清空（其中任务可能引用回滚后不存在的数据），再恢复迁移前快照：

```bash
cd /opt/reproagent
uv run python deploy/server/ops.py verify /var/lib/reproagent/backups/pre-migration/<快照目录>
uv run python deploy/server/ops.py restore /var/lib/reproagent/backups/pre-migration/<快照目录> \
  --confirm DESTROY_CURRENT_DATABASE
```

只有数据库与 Storage 是同一份快照且确实需要一起回退时才加 `--storage`。原 Storage 目录会先
重命名为 `storage.before-restore-<时间>`，不会自动删除。恢复后检查
`public.schema_migrations`、登录、上传和一条完整运行链路，再重新开放服务；恢复旧数据库后不要
直接启动需要新 schema 的新镜像。

完整恢复演练必须在备用服务器/隔离环境使用异地副本执行，并实际登录、读取 Storage、启动 worker、
完成一次训练。不要为了演练在生产实例上运行 `restore`。本仓库的离线测试只验证命令编排、失败
中止、校验和与重启保障，不能代替真实恢复演练。

## 本地进程模式（不用 Docker 跑应用）

Supabase 仍用 `supabase start`（Docker），Redis 可用 `docker compose up -d redis`，应用直接跑在本机：

```bash
cp .env.example .env    # 填入 supabase start 打印的 key

# 五个终端分别执行：
uv run uvicorn reproagent_api.main:app    --port 8000 --reload
uv run uvicorn reproagent_agent.main:app  --port 8001 --reload
uv run uvicorn reproagent_runner.main:app --port 8002 --reload
uv run arq reproagent_api.worker.WorkerSettings
pnpm dev:web
```

前端另需 `apps/web/.env.local`（已被 gitignore）：
`NEXT_PUBLIC_API_URL`、`NEXT_PUBLIC_SUPABASE_URL`、`NEXT_PUBLIC_SUPABASE_ANON_KEY`。

## 前端构建与发布

前端只有 `NEXT_PUBLIC_SITE_MODE` 一个构建模式开关，缺省为 `live`；显式构建命令会覆盖它，
Windows、Linux 与 macOS 使用同一套命令，不需要额外设置环境变量。

```bash
pnpm build:web              # live：Next.js 服务端构建，产物 apps/web/.next/
pnpm build:web:static       # static：纯静态导出，产物 apps/web/out/
pnpm verify:web:live        # 独立检查 live 产物标记与 BUILD_ID
pnpm verify:web:static      # 独立检查静态页面、资源目录与产物标记
```

两种构建都会写入 `site-mode.json` 并自动验证产物；单独发布前必须运行对应 `verify` 命令。
`live` 启用 `cacheComponents` 与 `partialPrefetching`；`static` 使用 `output: "export"` 且不启用
这两个服务端渲染选项。静态构建会在编译时关闭登录和工作台后端功能，仅保留公开案例；
它不是连接后端的一种降级运行模式。

当前 Qoder Sites 静态托管没有 HTML 回退，深层链接必须带 `.html`，例如 `/login.html`、
`/papers.html?id=<paper_id>`、`/experiments.html?id=<experiment_id>`、
`/comparisons.html?id=<comparison_id>`。静态构建的站内导航会自动补上 `.html` 并使用整页跳转，
避免客户端路由请求不存在的无扩展名地址。发布目录是 `apps/web/out/`，不要发布 `.next/` 或
另一个模式留下的旧产物。`bash scripts/check.sh` 与离线 CI 会依次构建并验证两种模式。

## 测试

```bash
uv run pytest                        # 离线单元/契约测试（schemas/paperparse/api/agent/runner）
uv run pytest tests/integration -v   # RLS + 理解 + 规划 + 真实容器执行 + 统一工作流 E2E（需 supabase start / Docker）
pnpm lint:web                        # 前端 lint
pnpm --filter web test               # 前端离线回归测试
pnpm --filter web exec tsc --noEmit  # 前端类型检查
pnpm build:web                       # live 前端生产构建与产物校验
pnpm build:web:static                # static 前端生产构建与产物校验
bash scripts/check.sh                # 完整离线门禁（Python + 前端 + 两种构建）
uv run pytest scripts/test_verify_v2_acceptance.py apps/api/tests/test_active_surface.py -v
uv run python scripts/smoke_test.py        # 健康 + 规划冒烟（需 compose 栈已启动）
uv run python scripts/smoke_execution.py   # 真实容器执行验收（需 exec 镜像 + compose 栈）
```

`tests/integration/test_workflow_e2e.py` 驱动**完整前端闭环 E2E**：上传 LeNet-5 PDF → 真实解析 →
（stub LLM）Card → 真实证据落地 → 用户确认缺失信息 → `resume(spec)` → 真实确定性规划 → 用户确认假设并缩放子集 →
`resume(plan)` → 真实代码生成 → AST 静态校验 → **真实 Docker 容器 MNIST 训练** → 真实指标 → 确定性评估报告 →
落库。断言报告与真实运行记录关联（`references.run_id`/`code_hash`）、预算累计真实 LLM/实验消耗、
各关键节点 Trace 齐全且**无凭据泄漏**、跨用户 ownership 隔离。LLM 提取在 HTTP 边界 stub，其余 agent 步骤跑真实确定性引擎，
执行经 Runner app（ASGI transport）生成真实兄弟容器，状态用共享 memory checkpointer 跨两个人工门恢复同一图线程。

`tests/integration/test_execution.py` 用**真实 Docker** 验证沙箱验收标准：真实训练产出指标与完整审计、
硬超时强杀容器、内存限额杀进程（exit 137）、网络隔离、容器内无宿主密钥、读不到未挂载宿主文件、
一次真实错误检测（IMPORT_ERROR）→ 自动修复 → 重执行成功，以及**对真实容器运行结果做端到端评估**
（实验验证项与实际执行一致、指标对比来自真实 metrics、14 节报告、绝不判完整复现）。

测试覆盖：schema 校验与 ownership 约束、JWT 认证依赖、ownership 拦截、
PDF 解析（页码/章节/表格/OCR 标记）、证据落地匹配（伪造页码覆写、不可验证引文降级）、
LangGraph 图编译与节点契约、LLM Adapter 工厂、Runner 密钥隔离断言、
服务间 HTTP 契约（httpx MockTransport）、健康检查聚合、Card 编辑与版本追加、
确定性规划引擎（参数溯源、缺失参数标记为待确认提案、非标准优化器替代、MVP 缩放与原文偏差区分、
审批门）、计划编辑/版本追加/确认同步、实验编排路由（生成/编辑/审批/ownership）、
真实数据库上的上传→解析→版本→编辑全流程、Card→计划→编辑→审批全流程、
RLS 跨用户隔离、追加式版本不可篡改与计划配置不可变触发器（仅 approved_at 可更新）、
确定性代码生成与 AST 静态校验、错误分类与修复（含 OOM 触发新计划版本）、
Worker 执行/修复循环（成功/IMPORT_ERROR 自动修复/OOM 降批大小/静态校验失败即停/不可修复错误停止）、
运行编排路由（发起/详情/列表/产物/代码文件/日志/取消，ownership 校验）、
**确定性 Evaluator**（AST 实现事实提取、8 项方法验证的 matched/deviated/unknown 分支、
9 项实验验证、百分制/错误率换算与百分点差、6 态判定树全分支、14 节 Markdown 与引文转义、
`/v1/evaluate` 路由）、Worker 终态自动生成报告（成功/失败路径、报告失败不影响运行状态）、
报告读取/重生成端点（追加不可变 version、ownership、非终态 409）。

## v2 验收（证据绑定）

`bash scripts/check.sh` 只是离线质量/构建门禁，不会因为缺少 credentialed acceptance evidence 而失败；它包含 verifier 单测。完整交付验收是独立的显式命令，读取 `_bmad-output/acceptance/v2/` 下证据，写入 JSON 账本和终端表，只要 G-01–G-05、NFR-05、P1 任一为 pending/failed 就返回非零。

```bash
# 离线代码、契约、前端与静态构建门禁
bash scripts/check.sh

# 真实闭环：需要专用账号密码（只从环境读取，不写入证据）
LIVE_ACCEPTANCE_PASSWORD=... node apps/web/scripts/live-acceptance.mjs \
  --base-url http://127.0.0.1:3000 \
  --pdf docs/1998-lecun.pdf --email <dedicated-account> \
  --output _bmad-output/acceptance/v2/g02

# 负向/恢复矩阵与安全验收：真实 Supabase + Docker，包含终态、OOM、repair、ownership/隔离
bash scripts/check_integration.sh
uv run pytest tests/integration -m integration -v

# NFR-05：同一获批环境顺序执行 3 个新 Run，记录四段耗时与 canonical environment hash
REPROAGENT_ACCESS_TOKEN=... uv run python scripts/measure_run_segments.py \
  --experiment-id <approved-experiment-id> \
  --output _bmad-output/acceptance/v2/nfr05/result.json

# 安全/legacy 审计（只读扫描；输出属于 ignored evidence）
uv run python scripts/audit_legacy_consumers.py \
  --format json --output _bmad-output/acceptance/v2/legacy-consumers.json
uv run python scripts/audit_legacy_consumers.py --check-migrations

# 最终账本：默认绑定当前 HEAD；受控复评可显式指定完整 SHA
uv run python scripts/verify_v2_acceptance.py \
  --evidence-root _bmad-output/acceptance/v2 \
  --output _bmad-output/acceptance/v2/acceptance-ledger.json
# 或：--commit-sha <full-final-sha>
```

上述采集命令保存的是原始运行记录；受控验收流程还需将其整理为账本 schema，并显式写入最终
`commitSha`、断言和本地证据路径。verifier 不会从相邻文件、历史 wrapper 或当前 HEAD 代填绑定。

最终证据入口是 `g01/result.json`、`g02/result.json`、`g03/result.json`、`g04/result.json`、
`g05/result.json`、`nfr05/result.json`、`p1/result.json`。每项都必须有完整的 `commitSha`、通过断言和
本地证据路径；G-02 还会逐字节核对 report Markdown SHA-256，以及 run/workflow/experiment/Plan/
code/report/version/final-attempt 身份。NFR-05 必须是三个不同 succeeded Run、同一 canonical
environment hash 和四个有限非负耗时阶段。P1 需要桌面/窄屏三列摘要与五类 evidence-index 的
browser/API 证据。历史 `full-run-*`、旧 G-05 wrapper 或缺少 SHA 的 browser 记录不会自动成为最终证据。
verifier 只读证据、计算 hash 并运行公共九文件 verifier，不调用 credentialed workflow、不改数据库、
不打印 secret，也不制造证据。`_bmad-output/acceptance/v2/` 是 ignored evidence，不提交。

## 环境变量

见 [.env.example](.env.example)。要点：

- `SUPABASE_*` 只注入 api/worker 容器；`QWEN_*` 只注入 agent 容器；runner 容器不注入任何密钥。
- `SUPABASE_PUBLIC_URL`：返回给前端的签名 PDF 链接所用的浏览器可达地址。compose 内 api 通过
  `host.docker.internal` 访问 Supabase，但浏览器无法解析该主机名，故单独配置为 `http://localhost:54321`。
- `REDIS_URL`：api/worker 的任务队列地址（compose 内为 `redis://redis:6379`）。
- `SUPABASE_DB_URL`：LangGraph Postgres checkpointer 连接串（宿主进程模式用 `127.0.0.1:54322`）。
  compose 内 worker 经 `SUPABASE_DB_URL_CONTAINER`（默认 `host.docker.internal:54322`）访问宿主 Supabase DB，
  因为容器内 `127.0.0.1` 不是宿主机。
- `NEXT_PUBLIC_SUPABASE_URL` / `NEXT_PUBLIC_SUPABASE_ANON_KEY`：web 登录用（浏览器可达的地址）。
- `QWEN_MODEL` 需足够长的上下文以容纳整篇论文：47 页 LeNet-5 论文约 63K tokens，
  硅基流动请用 `Qwen/Qwen2.5-72B-Instruct-128K`（32K 版本会报 max_seq_len 超限）。
- Compose 内服务互访使用服务名（`http://agent:8001`、`http://runner:8002`），
  容器访问宿主机 Supabase 使用 `host.docker.internal`。
- **执行镜像**：CPU 版 torch/torchvision 与 MNIST 只烘焙进 `reproagent-exec` 镜像
  （`scripts/build_exec_image.sh`，可用 `PYPI_INDEX`/`TORCH_INDEX`/`PYTHON_IMAGE`/`EXEC_IMAGE` 覆盖镜像源）。
  Runner 自身是轻量沙箱编排器，只依赖 `docker` SDK，**不安装 torch**；本地 `uv sync` 也不装 ML 依赖。
- `RUNNER_*`（仅 runner 容器，无任何密钥）：`RUNNER_WORK_DIR`（宿主 bind 工作目录，compose 内 `./.runner-work`）、
  `RUNNER_TIMEOUT_SECONDS`（硬超时上限）、`RUNNER_EXEC_IMAGE`（执行镜像）、`RUNNER_ENFORCE_DISK_QUOTA`
  （宿主不支持 storage_opt 时设 false 优雅降级）、`RUNNER_SANDBOX_USER`（默认 `1000:1000` 非 root）。

## 接口契约

| 端点 | 说明 |
|---|---|
| `GET /health`（api/agent/runner） | 自身健康；agent 报告 `graph_loaded`，runner 报告 `torch_available` |
| `GET /health/dependencies`（api） | 聚合 agent、supabase、runner 状态 |
| `GET /health/dependencies`（agent） | Agent 自身依赖状态；不代理 Runner |
| `POST /v1/papers`（api，需 JWT） | multipart 上传 PDF → Storage + papers + 创建 workflow + 入队，`202 + paper_id/workflow_id/thread_id` |
| `GET /v1/papers` / `GET /v1/papers/{id}` | 论文列表 / 详情（ownership 校验） |
| `GET /v1/papers/{id}/pdf` | Storage 签名 URL（1 小时有效） |
| `GET /v1/papers/{id}/card?version=` | Reproduction Card（默认最新版） |
| `GET /v1/papers/{id}/card.md?version=` | Card 的 Markdown 导出（`text/markdown` + attachment，含证据/页码/缺失清单/审核留痕；确定性渲染，无 LLM） |
| `GET /v1/papers/{id}/versions` | 版本列表（extraction / user_edit） |
| `POST /v1/papers/{id}/card/edits` | 字段确认/修改 → 追加新版本（`base_version` 乐观校验） |
| `POST /v1/papers/{id}/card/confirm` | 将最新 Card 原样确认为 `confirmed` 版本（解锁 spec 人工门），`201` |
| `POST /v1/papers/{id}/reparse` | 重新解析 → 新 workflow + 新版本 |
| `GET /v1/workflows` / `GET /v1/workflows/{id}` | 工作流列表（可按 paper 过滤）/ 详情（13 态 + budget + 引用，ownership 校验） |
| `GET /v1/papers/{id}/workflow` | 论文最新工作流 |
| `GET /v1/workflows/{id}/traces` | 节点级 Trace（凭据脱敏，ownership 校验） |
| `POST /v1/workflows/{id}/resume` | 人工门后继续：`stage=spec`（需 confirmed Card）/ `stage=plan`（需已批准计划），否则 `409`；入队 worker |
| `POST /v1/understand`（agent，内部） | `UnderstandRequest(pdf_b64)` → 解析 + 提取 + 证据落地，同步返回 Card |
| `POST /v1/plan`（agent，内部） | `PlanRequest(card, scaling overrides)` → 确定性生成实验计划，同步返回 |
| `POST /v1/papers/{id}/experiments`（api，需 JWT） | 读取最新 Card → 调 agent 规划 → 建实验 + 计划 v1，`201` |
| `GET /v1/papers/{id}/experiments` | 论文下的实验列表（ownership 校验） |
| `GET /v1/experiments/{id}` | 实验详情 + 最新计划版本 |
| `GET /v1/experiments/{id}/plans` | 计划版本列表（generated / user_edit / oom_retry） |
| `GET /v1/experiments/{id}/plans/{version}` | 指定计划版本完整内容 |
| `POST /v1/experiments/{id}/plans` | 编辑/确认参数 → 追加新版本（记录修改原因/前后值/关联错误） |
| `POST /v1/experiments/{id}/plans/{version}/approve` | 审批门：存在未确认关键假设返回 `409`，否则置 `approved_at` + 实验 `approved` |
| `POST /v1/experiments/{id}/runs`（api，需 JWT） | 需已审批计划（否则 `409`）→ 建运行记录 + 入队 worker，`202 + run_id` |
| `GET /v1/experiments/{id}/runs` | 实验下运行列表（ownership 校验） |
| `GET /v1/runs/{id}` | 运行详情：status/exit_code/error_category/repair_attempt/metrics/audit/版本 |
| `GET /v1/runs/{id}/logs` | stdout/stderr（service role 读 `runs` bucket） |
| `GET /v1/runs/{id}/artifacts` / `/files` | 产物列表 / 本次运行绑定的代码文件版本（含 sha256/change_reason） |
| `POST /v1/runs/{id}/cancel` | 取消运行（透传 Runner 强杀容器） |
| `GET /v1/runs/{id}/report` | 复现报告（`report_json` + `markdown`，ownership 校验；未生成 404） |
| `POST /v1/runs/{id}/report` | 手动重生成报告（仅终态运行，否则 409；按 `(run_id, version)` 追加，不覆盖历史） |
| `POST /v1/codegen`（agent，内部） | 已审批计划 → 确定性模板生成 5 个代码文件 |
| `POST /v1/codegen/validate`（agent，内部） | AST 静态校验 → `ValidationReport` |
| `POST /v1/codegen/classify`（agent，内部） | stderr/exit_code → 8 类错误分类 + 是否可修复 |
| `POST /v1/codegen/repair`（agent，内部） | 重渲染规范模板修复（OOM 返回 `replan_required`） |
| `POST /v1/evaluate`（agent，内部） | `EvaluateRequest`（真实运行数据 + 固定代码 + Card/Plan）→ 确定性评估，同步返回 `EvaluationReport` + 14 节 Markdown |
| `POST /runs`（runner，内部） | `RunRequest` → 生成锁定容器异步执行，`202 + run_id` |
| `GET /runs/{id}` / `POST /runs/{id}/cancel`（runner） | 运行结果 / 取消强杀 |
| `GET /runs/{id}/artifacts` / `/artifacts/{name}`（runner） | 产物名列表 / 产物字节 |

`/v1/jobs` 与 `/v1/reproductions` 不在活跃 API/Agent surface；旧占位实现不注册路由，零生产消费者由 legacy audit 记录。

## 常见问题

- **Docker Hub 拉镜像超时**：为 Docker Desktop 配置代理（Settings → Resources → Proxies，
  指向本机代理，如 `http://127.0.0.1:7897`），或配置 registry mirror。
- **端口冲突**：Supabase 占用 54321-54327 等端口，应用占用 3000/8000/8001/8002。
- **Windows 下 httpx 报 InvalidURL**：系统代理环境变量异常时，脚本内使用 `trust_env=False`。

## 后续阶段

1. 扩展更多模型/数据集模板（当前确定性代码生成与评估仅覆盖 LeNet-5 + MNIST）。
2. URL 来源论文摄取（当前仅支持 PDF 上传）。
3. 多运行横向对比与复现历史聚合视图。