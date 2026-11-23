# ReproAgent

科研论文复现 Agent。上传论文 PDF 或提供 URL，系统自动完成：论文理解 → 结构化复现规格提取（含原文证据）→ 缩小规模实验计划 → Python 实验代码生成 → 隔离环境执行 → 与论文报告结果对比 → 可追溯复现报告。

**当前进度**：各模块已整合为**统一 LangGraph 工作流**（15 节点 + 12 态状态机 + Postgres checkpoint + 预算/重试/Trace harness），上传 → 确认 Card → 批准计划 → 真实训练 → 报告的全流程可**完全从前端闭环操作**。Paper Understanding、Experiment Planner、代码生成/执行与 **Reproduction Evaluator / 报告系统**已实现。理解链路：上传 PDF → PyMuPDF 解析（页码/章节/表格/OCR 扩展接口）→ Qwen 提取 Reproduction Card → 证据落地校验（页码/引文由解析器回填，LLM 无法伪造）→ 前端双栏证据审阅 + 追加式版本管理。规划链路：Reproduction Card → 确定性规则引擎生成缩小规模实验计划（无 LLM，每个参数带来源溯源）→ 关键假设人工确认门 → 追加式计划版本管理（用户修改 / OOM 降批大小）→ 前端审批 UI。执行链路：已审批计划 → Agent 确定性模板生成代码（train/evaluate/model/config/requirements，无 LLM）→ AST 静态校验 → Worker 驱动 Runner 在**独立锁定 Docker 容器**中真实训练 → 采集 stdout/stderr/指标/审计 → 错误分类 + 最多 3 次自动修复（OOM 触发新计划版本而非私改训练目标）→ 前端运行面板（指标/审计/日志/代码版本/产物）。评估链路：运行终态 → **全确定性 Evaluator（无 LLM）**从本次运行固定的代码版本 AST 提取实现事实 → 方法验证（8 项）/实验验证（9 项）/结果对比（含百分点差）→ 6 态最终判定 → 14 节 Markdown 报告自动落库 → 前端结构化报告面板 + Markdown 查看/下载。复现目标仅 LeNet-5 + MNIST；**缩放实验永远不会被标记为完整复现（Full Reproduction）**。

## 访客案例与实验工作台

在首页选择「用一张手写数字读懂论文」，或访问 `/examples/lenet-mnist`。无需登录即可猜数字、核对真实预测、跳到论文页和下载条件对照报告。案例快照来自一次独立的 CPU 容器运行，训练 4,096 张、测试 1,024 张，认对 909 张。页面上的 12 张图片按测试顺序选取。公开文件及 SHA-256 清单位于 `apps/web/public/examples/lenet/`。

工作台仍按用户权限读取私人实验。成功运行的 `GET /v1/runs/{run_id}/prediction-examples` 返回本次尝试的样本。未留存样本的旧运行仍可查看指标和报告。新报告在 `parameter_comparisons` 中区分原文证据、计划选择、实际观测和代码或配置来源。已存在的报告可以从报告面板重新生成。

## 架构

```
┌──────────┐   HTTP    ┌──────────┐   HTTP    ┌──────────┐   HTTP    ┌──────────┐
│   web    │ ────────> │   api    │ ────────> │  agent   │ ────────> │  runner  │
│ Next.js  │           │ FastAPI  │           │ LangGraph│           │ 隔离执行  │
│  :3000   │           │  :8000   │           │  :8001   │           │  :8002   │
└──────────┘           └────┬─────┘           └────┬─────┘           └──────────┘
                            │                      │                 仅 internal 网络
                     ┌──────┴──────┐          Qwen LLM API           无外网、无密钥
                     │             │         (统一 LLM Adapter)
                Supabase      Redis + worker
             (PostgreSQL +    (arq 异步任务：
              Storage)        理解编排 + 执行/修复循环)
```

| 服务 | 职责 | LLM Key | Supabase Service Role Key |
|---|---|---|---|
| `apps/web` | 登录、上传、Card 证据审阅/编辑、计划审批、运行面板、**复现报告面板**、健康面板 | ✗ | ✗（仅 anon key + 用户 JWT） |
| `apps/api` | 认证、user ownership、业务数据、任务编排、报告读取/重生成 | ✗ | ✓ |
| `worker` | arq 异步任务（api 同镜像）：PDF 理解编排；**驱动执行/修复循环**（codegen→校验→Runner→分类→修复）；**终态自动生成报告**，独占 DB 写入 | ✗ | ✓ |
| `apps/agent` | 论文理解/规格提取/规划/**确定性代码生成/静态校验/错误分类/修复/复现评估与报告渲染**（LangGraph，不执行代码） | ✓ | ✗ |
| `apps/runner` | 沙箱编排：经挂载的 Docker Socket 生成锁定的一次性执行容器（自身不跑 torch） | ✗ | ✗ |

**硬性边界**

- Agent 不直接执行 Python 代码，只通过 `ExecutionRequest`/`ExecutionResult` 契约委托 Runner。
- Runner 在 Compose 的 `runner` 网络（`internal: true`）中，无外网出口、不发布端口，仅 Agent 可达。
- 所有权统一经 `projects.user_id` 传递：papers/specs/jobs/experiments/runs 等表通过外键链
  归属到 project，全表启用 RLS（`owns_project/owns_paper/owns_experiment/owns_run` helper），
  不依赖前端隐藏数据；API 层另做归属校验（双保险）。
- 所有 Agent/服务间数据交换使用 `packages/schemas` 中的 Pydantic 模型校验（`extra="forbid"`）。
- 长任务一律异步：上传/重新解析返回 `202 + job_id`，由 Redis + arq worker 执行，前端轮询 `GET /v1/jobs/{id}`。
- **证据不可伪造**：LLM 只提出 value + 原文引文；`source_page`/`source_section` 由解析器
  对引文做归一化模糊匹配后回填，LLM 声称的页码一律丢弃。匹配失败的 `paper_explicit`
  降级为 `inferred`（剥掉引文）或 `missing`。
- **版本只增不改**：`reproduction_specs` 为追加式版本表，RLS 只有 select/insert 策略
  （无 update/delete）；重新解析与用户编辑都产生新版本并记录 `parent_version_id`。
  `experiment_plans` 由 BEFORE UPDATE 触发器锁定除 `approved_at` 外的所有列；
  `generated_files` 追加式，每次修复生成新版本并记录 `sha256`/`change_reason`/`related_error`。

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
生成报告并 upsert 到 `reproduction_reports`（每运行一份）；也可经
`POST /v1/runs/{id}/report` 手动重生成。**整条判定链路无 LLM 参与**：

- **实现事实提取**：从本次运行精确绑定的代码版本（`execution_run_files` → `generated_files`）
  AST 解析 model.py/train.py + config.yaml，提取卷积/全连接/池化/激活/损失/预处理/数据集事实。
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

**12 态状态机**（前端 `WorkflowStepper` 实时展示）：`uploaded / parsing / awaiting_spec_review /
planning / awaiting_plan_approval / generating_code / validating_code / executing / repairing /
evaluating / completed / failed`。

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

## 优化器对照（成对实验）

除单组复现外，平台支持回答论文中的一个**对照问题**：两组只在优化器（及其学习率）上不同，
其余条件必须相同。范围仍为 MNIST + 现有 LeNet-5 模板 + 两种已支持优化器；超出范围时上传页
直接列出缺项（数据集/模型/优化器/主指标/损失/预算），不会进入执行。

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

迁移位于 `infra/supabase/migrations/`，所有权根为 `projects.user_id`，其余表经外键链传递归属：

| 表 | 归属链 | 说明 |
|---|---|---|
| `projects` | `user_id` 直接 | 所有权根；每用户一个 `is_default` 默认项目（部分唯一索引） |
| `papers` | `project_id → projects` | PDF 存 Storage，`pdf_path`/`file_sha256`/`metadata_json`/`status` |
| `reproduction_specs` | `paper_id → papers` | Card 版本，`spec_json` + `origin` + `parent_version_id`；**追加式**（仅 select/insert） |
| `understanding_jobs` | `paper_id → papers` | 旧异步解析任务表（已由统一 `workflows` 取代，保留兼容） |
| `workflows` | `project_id → projects` | 统一工作流：`thread_id`/`status`(12 态)/`current_node`/`experiment_id`/`run_id`/`spec_version_id`/`plan_id`/`budget_json`/`error` |
| `experiments` | `project_id → projects` | 复现实验，关联 paper/spec |
| `experiment_plans` | `experiment_id → experiments` | 版本化计划，`config_json`/`budget_json`/`approved_at` |
| `generated_files` | `experiment_id → experiments` | 代码文件版本，`sha256`/`change_reason`/`related_error`；**追加式** |
| `execution_runs` | `experiment_id → experiments` | 运行记录，`plan_id`/`code_version`/`plan_version`/`metrics_json`/`runtime_json`/`error_category`/`error`/`repair_attempt`/`code_hash`/`runner_run_id`/`audit_json` |
| `execution_run_files` | `run_id → execution_runs` | **运行↔代码文件精确绑定**（`run_id`,`generated_file_id`），每次尝试重新绑定；双亲均 `on delete cascade` |
| `execution_artifacts` | `run_id → execution_runs` | 运行产物（Storage 路径 + 元数据） |
| `reproduction_reports` | `run_id → execution_runs` | 每运行一份报告，`report_json` + `markdown` |
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

> Runner 不发布端口。验证 Runner：`curl http://localhost:8001/health/dependencies`
> （api → agent → runner 链路探测），或 `docker compose exec agent python -c "import httpx; print(httpx.get('http://runner:8002/health').text)"`。

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

## 测试

```bash
uv run pytest                        # 356 个单元/契约测试（schemas/paperparse/api/agent/runner）
uv run pytest tests/integration -v   # RLS + 理解 + 规划 + 真实容器执行 + 统一工作流 E2E（需 supabase start / Docker）
pnpm --filter web build              # 前端类型检查 + 生产构建
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
报告读取/重生成端点（upsert、ownership、非终态 409）。

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
| `GET /health/dependencies`（api） | 聚合 agent、supabase、runner（经 agent）状态 |
| `GET /health/dependencies`（agent） | runner 状态 |
| `POST /v1/papers`（api，需 JWT） | multipart 上传 PDF → Storage + papers + 创建 workflow + 入队，`202 + paper_id/workflow_id/thread_id` |
| `GET /v1/papers` / `GET /v1/papers/{id}` | 论文列表 / 详情（ownership 校验） |
| `GET /v1/papers/{id}/pdf` | Storage 签名 URL（1 小时有效） |
| `GET /v1/papers/{id}/card?version=` | Reproduction Card（默认最新版） |
| `GET /v1/papers/{id}/card.md?version=` | Card 的 Markdown 导出（`text/markdown` + attachment，含证据/页码/缺失清单/审核留痕；确定性渲染，无 LLM） |
| `GET /v1/papers/{id}/versions` | 版本列表（extraction / user_edit） |
| `POST /v1/papers/{id}/card/edits` | 字段确认/修改 → 追加新版本（`base_version` 乐观校验） |
| `POST /v1/papers/{id}/card/confirm` | 将最新 Card 原样确认为 `confirmed` 版本（解锁 spec 人工门），`201` |
| `POST /v1/papers/{id}/reparse` | 重新解析 → 新 workflow + 新版本 |
| `GET /v1/workflows` / `GET /v1/workflows/{id}` | 工作流列表（可按 paper 过滤）/ 详情（12 态 + budget + 引用，ownership 校验） |
| `GET /v1/papers/{id}/workflow` | 论文最新工作流 |
| `GET /v1/workflows/{id}/traces` | 节点级 Trace（凭据脱敏，ownership 校验） |
| `POST /v1/workflows/{id}/resume` | 人工门后继续：`stage=spec`（需 confirmed Card）/ `stage=plan`（需已批准计划），否则 `409`；入队 worker |
| `GET /v1/jobs/{id}`（api） | 旧解析任务状态（已由 workflow 取代，保留兼容） |
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
| `POST /v1/runs/{id}/report` | 手动重生成报告（仅终态运行，否则 409；upsert 每运行一份） |
| `POST /v1/codegen`（agent，内部） | 已审批计划 → 确定性模板生成 5 个代码文件 |
| `POST /v1/codegen/validate`（agent，内部） | AST 静态校验 → `ValidationReport` |
| `POST /v1/codegen/classify`（agent，内部） | stderr/exit_code → 8 类错误分类 + 是否可修复 |
| `POST /v1/codegen/repair`（agent，内部） | 重渲染规范模板修复（OOM 返回 `replan_required`） |
| `POST /v1/evaluate`（agent，内部） | `EvaluateRequest`（真实运行数据 + 固定代码 + Card/Plan）→ 确定性评估，同步返回 `EvaluationReport` + 14 节 Markdown |
| `POST /runs`（runner，内部） | `RunRequest` → 生成锁定容器异步执行，`202 + run_id` |
| `GET /runs/{id}` / `POST /runs/{id}/cancel`（runner） | 运行结果 / 取消强杀 |
| `GET /runs/{id}/artifacts` / `/artifacts/{name}`（runner） | 产物名列表 / 产物字节 |
| `POST /v1/reproductions`（api/agent） | 旧占位 501（报告能力已迁移至 `/v1/evaluate` 与 `/v1/runs/{id}/report`） |

## 常见问题

- **Docker Hub 拉镜像超时**：为 Docker Desktop 配置代理（Settings → Resources → Proxies，
  指向本机代理，如 `http://127.0.0.1:7897`），或配置 registry mirror。
- **端口冲突**：Supabase 占用 54321-54327 等端口，应用占用 3000/8000/8001/8002。
- **Windows 下 httpx 报 InvalidURL**：系统代理环境变量异常时，脚本内使用 `trust_env=False`。

## 后续阶段

1. 扩展更多模型/数据集模板（当前确定性代码生成与评估仅覆盖 LeNet-5 + MNIST）。
2. URL 来源论文摄取（当前仅支持 PDF 上传）。
3. 多运行横向对比与复现历史聚合视图。