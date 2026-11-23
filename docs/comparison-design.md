# 优化器对照（成对实验）设计

> 状态：已实现（MNIST + LeNet-5 + 两种已支持优化器）。
> 范围：对照是**缩放实验**，任何状态都不会被表述为论文完整复现。

## 1. 要回答的问题

论文常报告多组超参数结果（如 SGD vs Adam）。用户需要的不只是"复现一次"，
而是"在受控条件下，这两组的差异能否被真实复现出来"。本功能只服务一个窄问题：
**两组除优化器（及其学习率）外完全一致，差值是多少、方向如何。**

## 2. 硬性约束（由代码而非文案保证）

| 约束 | 落点 |
|---|---|
| 两组必须绑定原文出处（页码/表号/引文） | 每组缺出处 → 生成 `PendingParam`，未处理不能批准 |
| 缺参不得静默默认 | `PendingParam` 带建议值、来源、影响；`ComparisonPlan.approval_ready()` 检查 |
| 差值只在配对通过后计算 | `verify.py`：任何一项 UNKNOWN/DEVIATED 都不产生 `delta` |
| 准确率独立重算 | `correct_count / evaluated_count`，训练脚本自报的 `metric_value` 只作对照 |
| 共同条件必须实测一致 | 数据划分 / 初始权重 / 样本顺序 / 训练预算 / 评估条件 / 代码模板，六项逐一比对 |
| 失败只补跑失败组 | `_run_group` 用同一 plan 重试该组，最多 2 次；共同配置变更必须新建计划版本 |
| 同一任务只有一个驱动 | `run_comparison_task` 持有 pg advisory lock；入队用固定 `_job_id`；尝试行在开跑前写入，恢复时优先重连在飞运行而非重训 |
| 超授权缩放必须逐字段确认 | `apply_scaling_change` 为每个超限字段生成待确认项，采用建议值即把上限写回该字段；启动前再断言一次授权范围 |
| 要均值就不能只跑一对 | `_seeds` 在问题含"均值/多次"时至少补齐到 3 个种子，并把补齐动作写进 assumptions；报告 `mean_basis` 与实际种子数一致 |
| 恢复路径必须落终态 | `_resume_run` 在没有 Runner 运行标识时直接判失败并收尾，禁止留下永久 `running` 的实验 |
| 结果只来自真实运行 | 报告数值全部取自 `execution_runs`；缺失项标"未核实" |

## 3. 数据模型

```
comparison_plans                 (project_id, paper_id, spec_id, status)
  └─ comparison_plan_versions    (version, plan_json, origin, approved_at)  ← 追加式，仅 approved_at 可改
comparisons                      (plan_version_id, status, idempotency_key, summary_json)
  ├─ comparison_runs             (seed, group_key, run_id, attempt)         ← 每组每次尝试一行
  └─ comparison_reports          (report_json, markdown, status)
```

`comparisons` 通过 `plan_version_id` 固定版本；修改计划 = 新版本 + 新 comparison，
历史 comparison 继续引用原版本。`(plan_id, idempotency_key)` 唯一索引保证重复启动
只入队一次。

## 4. 执行链

```
POST /v1/papers/{id}/comparison-candidates   → 从 Card + 解析文档定位两组（行内归属）
POST /v1/papers/{id}/comparison-plans        → 生成计划 v1（含待确认项）
POST /v1/comparison-plans/{id}/versions      → 采用建议/补充依据/调缩放 → v(n+1)
POST .../versions/{v}/approve                → 待确认项清零后批准
POST .../versions/{v}/start                  → 建 comparison（幂等）+ 入队
worker.run_comparison_task                   → 逐种子成对执行 → 核验 → 报告
GET  /v1/comparisons/{id}[/report|/export]   → 状态 / Markdown / 产物包
POST /v1/comparisons/{id}/rerun              → 镜像缺失时拒绝；可另建当前环境对照
```

每组运行复用单组实验的完整链路：确定性 codegen → 静态校验 → Runner 沙箱 →
`execution_runs` + 固定代码文件 + 审计。唯一区别是 `config.yaml` 的 optimizer/lr。

## 5. 可核验摘要（`metrics.json.reproducibility`）

由训练代码在沙箱内自行计算，不是编排器断言：

- `dataset_split_sha256`：训练/测试索引列表
- `init_weights_sha256`：`torch.manual_seed(seed)` 后、训练前的参数张量
- `sample_order_sha256`：首个 epoch 的 `randperm(generator=seed)`
- `template_sha256`：model.py/train.py/evaluate.py 的内容哈希（不含 config.yaml）

`template_sha256` 而非聚合 `code_hash` 才是"同一份代码"的证据——两组 config 本就不同。

## 6. 报告与导出

Markdown 11 节：问题 / 论文依据 / 共同条件 / 两组差异 / 待确认处理 / 缩放授权 /
实测结果（重算）/ 配对核验 / 关键假设 / 局限 / 产物引用。
导出 zip 含 `manifest.json`、`plan.json`、`report.md|json`，以及每个
`runs/seed-{seed}-{group}-attempt-{n}/` 下的 `run.json`、日志、`config.yaml` 等代码、
`artifacts/`（含 `model.pt`、`metrics.json`、`reproducibility.json`）。

## 7. 测试覆盖

- `packages/schemas/tests/test_comparison.py`：两组键唯一、优化器必须不同、待确认项证据、核验一致性
- `apps/agent/tests/test_comparison.py`：多表格同名指标不串组、缺参建议、补充来源、序列化恢复、重算准确率、共同条件破坏、单组失败、多种子均值（含种子补齐与去重）、作废报告措辞、用户补充出处即证据
- `apps/api/tests/test_comparisons.py`：建计划→采用建议→批准→幂等启动→成对执行→报告→导出、单组失败补跑、全失败、改题保留旧任务为未完成（含在飞竞态）、镜像变更拒绝重跑并返回缺项、跨账户 404、共同配置变更后整对重跑、重复启动只入队一次、锁被占用时跳过、超限缩放逐字段确认并写回上限、启动前拒绝越权版本、用户出处跨版本不重复、非法输入返回 422 而非 500、改题保留批大小且拒绝混合编辑、尝试次数上限、崩溃恢复重连在飞运行、无 Runner 标识的恢复必须落 failed 并补跑、待确认值长度上限、Card 版本缺失时改题返回 409

## 8. 验收 runbook（部署环境，真实容器）

前置：`supabase start` → 迁移到 `20261124000000_comparison.sql` → `scripts/build_exec_image.sh`
→ `docker compose up`。

1. 上传一篇含两优化器对照结果的论文，走完 Card 确认（论文页出现「建立优化器对照」）。
2. 进入 `/papers/{id}/comparison`：
   - 若论文不在支持范围，页面列出缺项（数据集/模型/优化器/主指标/损失/预算）后不可继续。
   - 选择候选 → 填问题 → 生成计划；故意留空一组学习率，确认出现待确认项。
   - 「采用建议值」保存为 v2；刷新页面，确认 v2 仍是"已采用建议"（状态已持久化）。
   - 批准 v2 → 启动；同一版本再点一次启动，观察 `comparisons` 只有一行。
3. `/comparisons/{id}`：
   - 轮询出现 2 条运行（A/B 同种子）；若某组失败，确认只有该组出现 attempt 2。
   - 核验表六项全 matched 时才有差值；把批大小改版本后重跑，确认两组都重新执行。
   - 下载 Markdown 与 zip；zip 内 `runs/seed-42-a-attempt-1/` 应含 `code/`、日志、`artifacts/model.pt`、
     `artifacts/metrics.json`（含 `reproducibility`）。
4. 重跑：先 `docker rmi reproagent-exec:latest`，点「按原条件重跑」应 409 并提示镜像缺失；
   再点「改用当前环境另建对照」，确认新建 comparison 且 `origin=rerun_current_env`。
5. 交叉核对：从 zip 里的 `correct_count` / `evaluated_count` 手算准确率，应与报告数值一致；
   报告中的论文页码/表号应与原文可逐一对照。
