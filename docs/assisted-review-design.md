# 分级辅助审核方案（Assisted Review）

> 状态：**已评审并实现**（Phase 1–3 已落地，见 commits a4b6d78 / ee73b9a；2026-11-20 完成设计符合性审查与 UX 补齐）。
> 2026-11-20 评审决定：**专家模式不再保留旧的"逐项全量确认"行为**——需要确认的假设与数据太多，即便专家也会厌烦；专家/辅助模式统一只拦关键项（§6.2）。专家模式的差异在于：字段编辑默认记为人工确认（`confirmed_by=human`），报告标注"人工逐项核验"，而非更重的闸门。本文档中与此冲突的旧表述（§3.5、§4、§5E、§10.3）已按此决定修订。
> 目标：解决用户提出的两个核心痛点，同时**不破坏**项目既有的"不得伪造证据 / 人工问责 / 信任域分离"底线。

---

## 1. 要解决的两个问题

**问题一：非专业人士无法进行人工审核。**
当前 `human_review`（理解卡片审核）与 `human_approve_plan`（计划批准）两道闸门，要求用户读懂论文原文、理解每个超参数的含义，才能判断"推断值对不对"。看不懂论文的用户被卡死在闸门前，无法推进实验。

**问题二：需要确认的假设太多，且确认是硬闸门。**
以 LeNet-5 + MNIST 实测为例，一次计划生成产生 **6 项 `requires_confirmation=True` 的假设**，用户必须逐项确认，`approve_plan` 才会放行；任何一项未确认就返回 `409`。用户从上传到执行一直在"确认假设"，体验割裂。

这两个问题本质是同一个：**把"科研级逐项人工核验"当成了所有用户的唯一路径**。方案的核心是——把审核从"一刀切的硬闸门"改造成"分级、可解释、可辅助、可托管"的渐进流程，让不同专业程度的用户都能走通，且每一步都留下可追溯的问责记录。

---

## 2. 现状代码定位（问题根因）

| 根因 | 位置 | 说明 |
|---|---|---|
| 证据只有三档， prose 描述被误判为"推断" | `packages/schemas/.../evidence.py:10` `EvidenceStatus` | 只有 `PAPER_EXPLICIT / INFERRED / MISSING`。论文用文字描述的预处理（居中、尺寸归一化、背景扩展）没有"原文描述"档，抽取器只能标 `INFERRED`。 |
| 每个 INFERRED 预处理步骤都强制确认 | `apps/agent/.../planning/engine.py:175` `_preprocessing` | 对每个 `INFERRED` 步骤生成 `Assumption(kind=INFERRED, requires_confirmation=True)`。**6 项里有 3 项是这类"伪推断"**——它们其实有原文依据，只是非逐字数值。 |
| 缺失值一律变成待确认提案 | `apps/agent/.../planning/assumptions.py:81` `param_from_evidence` | `MISSING`/不可解析 → `Assumption(kind=MISSING_PROPOSAL, requires_confirmation=True)`。这是**正确的**（不得静默默认），但不应与"低风险常规项"混在一起逐项拦人。 |
| 假设默认全部需要确认 | `packages/schemas/.../plan.py:81` `Assumption` | `requires_confirmation: bool = True`，无风险分级字段。 |
| 批准闸门一刀切 | `apps/api/.../routers/experiments.py:339` `approve_plan` + `plan.py:190` `unconfirmed_required()` | `unconfirmed_required()` 返回所有 `requires_confirmation and not human_confirmed`；非空即 `409`。不区分关键/常规，不区分审核模式。 |
| 无"白话解释 / AI 推荐 / 批量确认" | 前端 `PlanClient.tsx` / spec-review 页 | 用户面对的是裸字段路径 + rationale，没有面向非专家的解释与建议。 |

**结论**：问题二的一半是"过度标记"（3/6 是伪推断），另一半是"无分级 + 硬闸门"。问题一是"无辅助解释 + 无替代路径"。下面分别对症。

---

## 3. 设计原则（不可妥协的底线）

1. **不得伪造**：任何非 `PAPER_EXPLICIT` 的值，永远不得被升级成"论文原文"。辅助/托管只是改变"谁来接受这个提案"，不改变"它是提案"这一事实。`source` 仍为 `INFERRED`，`evidence_quote/source_page` 缺失就是缺失。
2. **人工问责可追溯**：每一次接受都记录"谁接受的"——`human`（人工逐项）/ `ai_assisted`（人工采纳 AI 推荐）/ `auto`（托管自动）。报告必须如实呈现审核模式与"未经人工逐项核验"声明。
3. **关键项永不静默**：Tier 1 关键假设（见 §5）在任何模式下都不会"无声默认"。托管模式是用户**显式选择**的授权，不是系统偷偷跳过。
4. **信任域不变**：解释/推荐由 agent（持 LLM key）生成；所有 DB 写入、闸门判定、Runner 调用仍在 api/worker。runner 无 key、不联网。敏感凭据不入 Trace。
5. **专家路径保留人工问责**：专家模式保留"人工逐项核验"的定位与留痕（编辑默认记 `human` 确认），但闸门与辅助模式一致只看关键项（2026-11-20 决定，见文档头部）。

---

## 4. 方案总览

一句话：**修掉过度标记 → 给假设分风险级 → 给每项配白话解释和 AI 推荐 → 提供三种审核模式 → 全程留痕并在报告中如实声明。**

```
原始 6 项强制确认
   │
   ├─ A. 修复过度标记：3 项"原文描述"预处理 → 自动接受（有原文引文，无需确认）
   │
   ├─ B. 风险分级：剩余项分为 Tier1 关键 / Tier2 常规 / Tier3 说明
   │
   ├─ C+D. 白话注解 + AI 推荐：每项可展开"这是什么/为什么重要/接受会怎样/风险"
   │
   └─ E. 三种模式：
        专家模式  = 人工逐项核验定位（编辑默认记 human 确认；闸门同样只看 Tier1）
        辅助模式  = 默认（AI 推荐 + 解释 + 批量采纳，用户只需对 Tier1 做少量决策）
        托管模式  = 0 点击（显式授权自动接受全部推荐与安全默认，报告标注"未逐项核验"）
```

实测预期：LeNet-5 场景下，**辅助模式用户决策数从 6 降到 ≤2**，**托管模式 0 点击**，专家模式同样只拦关键项。

---

## 5. 核心机制

### A. 修复过度标记：新增 `PAPER_DESCRIBED` 证据档

在 `EvidenceStatus` 增加第四档：

```
PAPER_EXPLICIT   逐字数值/明确陈述（需 quote + page）
PAPER_DESCRIBED  原文以文字描述了该做法，但非逐字参数（需 quote + page）  ← 新增
INFERRED         论文未直接说明，由理解阶段推断（需 value + confidence）
MISSING          论文缺失（禁止 value/quote/page）
```

- `EvidenceField` 校验器扩展：`PAPER_DESCRIBED` 与 `PAPER_EXPLICIT` 一样**强制要求 `evidence_quote` + `source_page`**（保证有原文依据，杜绝伪造）。
- 抽取器（agent 理解阶段）把"居中/尺寸归一化/背景扩展"这类 prose 描述标为 `PAPER_DESCRIBED` 而非 `INFERRED`。
- `_preprocessing`（engine.py:175）对 `PAPER_DESCRIBED` 步骤：`source=PAPER_EXPLICIT` 同等待遇（展示引文），**不生成 requires_confirmation 假设**。
- 效果：**直接消除 3 项伪推断确认**，且每项仍带原文引文与页码，完全合规。

> 注意：`PAPER_DESCRIBED` 只用于"论文确实描述了"的字段。抽取器不得把推断塞进这一档——校验器强制 quote+page 是硬约束。

### B. 假设风险分级

`Assumption` 增加两个字段：

```python
risk_tier: Literal["critical", "routine", "info"]   # 关键 / 常规 / 说明
auto_acceptable: bool = False                        # 是否允许托管/批量自动接受
```

分级规则（planner 生成时确定，可被人工覆盖）：

| Tier | 含义 | 典型字段 | 默认策略 |
|---|---|---|---|
| **critical（关键）** | 改变实验本质、错了就复现失败 | optimizer、learning_rate、loss_function、model architecture、target_metric、dataset 身份 | 必须显式决策（人工或采纳推荐）；`auto_acceptable=False` |
| **routine（常规）** | 影响小、有安全默认 | weight_decay=0、seed、prose 预处理、batch/epoch 缩放 | 可一键批量采纳；`auto_acceptable=True` |
| **info（说明）** | 仅告知，不需决策 | MVP_SCALING 缩放说明、数据集预置说明 | 不拦截；`requires_confirmation=False` |

- `MVP_SCALING` 类假设统一归 `info`（现状已 `requires_confirmation=False`，仅补 tier 标签）。
- 分级是**启发式默认**，专家可在 UI 调整某项 tier（留痕）。

### C. 白话注解（面向非专家）

每个待确认字段配一段 LLM 生成的通俗解释，结构固定：

- **这是什么**：用大白话解释字段含义（如"学习率＝每次参数更新的步长"）。
- **为什么重要**：它如何影响结果。
- **接受会怎样**：采纳当前提案的后果。
- **风险**：可能的偏差与不确定性。

约束：
- 由 **agent** 生成（持 LLM key），**只解释、不产生新证据**；解释文本明确标注"AI 通俗解读，非论文原文"。
- 按 `(field_path, value, card_hash)` **缓存**，避免重复调用与成本。
- 可折叠/开关，专家模式默认收起。
- 解释不得改写 `evidence_quote`，不得编造页码。

### D. AI 推荐（每项可一键采纳）

对每个 `critical`/`routine` 假设，agent 给出结构化推荐：

```json
{
  "field_path": "training_config.learning_rate",
  "action": "accept" | "modify",
  "suggested_value": 0.01,
  "confidence": 0.82,
  "reasoning": "LeNet-5/MNIST 常用 SGD 初始学习率；论文未给出，0.01 为安全起点",
  "risk": "若原文用衰减调度，固定 0.01 可能偏高"
}
```

- 用户可**逐项采纳**或**批量采纳全部推荐**。
- 采纳后 `human_confirmed=True`、`confirmed_by="ai_assisted"`（区别于纯人工 `"human"`）。
- `modify` 推荐走现有 `PlanEditRequest` 编辑路径，生成新 plan version（保留版本链）。
- 推荐是**建议**，不自动改 plan；只有用户采纳（或托管授权）才落地。

### E. 三种审核模式

| 模式 | 谁做决策 | Tier1 | Tier2 | 点击数（LeNet-5） | 报告标注 |
|---|---|---|---|---|---|
| **专家模式** | 用户逐项 | 逐项人工确认 | 不强制（可逐项确认） | ≤2（只拦关键项） | 人工逐项核验 |
| **辅助模式（默认）** | 用户 + AI 推荐 | 看解释/推荐后决策（可批量） | 一键批量采纳 | ≤2 | AI 辅助 + 人工决策 |
| **托管模式** | 用户显式授权系统 | 自动接受推荐/安全默认 | 自动接受 | 0 | **未经人工逐项核验** |

- 模式在 spec-review 与 plan-approval 页顶部**显式选择**，默认辅助。
- 托管模式进入前弹**一次性授权确认**："我将自动接受全部 AI 推荐与安全默认，关键假设不再逐项人工核验，报告将标注此声明。" 用户确认后记录 `delegated_at + user_id`。
- 托管模式**不伪造 human_confirmed**：见 §6 闸门改造。

### F. 批量确认 + 分组 + 进度

- 假设按 tier 分组展示：关键（红）/ 常规（ amber）/ 说明（灰）。
- "一键采纳全部常规项"按钮（仅 `auto_acceptable=True`）。
-  sticky 底栏实时显示：**"还需确认 N 项关键假设"**，N=0 时批准按钮才高亮。
- 进度条 + 已确认/总数计数，缓解" endless 确认"焦虑。

### G. 高置信自动接受（辅助模式内）

- `INFERRED` 且 `confidence ≥ 0.9` 的 `routine` 项，辅助模式下默认勾选"建议接受"，用户可一键确认全部高置信项。
- `critical` 项**无论置信度多高都不自动接受**（守住关键项底线）。
- 阈值可配置（默认 0.9），留痕。

### H. 渐进信任 / 记忆（Phase 2+，可选）

- 记录用户对某类字段的既往决策（如"该用户总是接受 SGD"），下次同类项预填推荐。
- 仅作为**推荐预填**，不跳过关键项决策；跨项目记忆需用户显式开启。
- 本期不做，列入后续。

---

## 6. 诚信与溯源保障（闸门改造）

这是方案能否守住底线的关键。

### 6.1 新增溯源字段

`Assumption` / `PlanParam` 增加：

```python
confirmed_by: Literal["human", "ai_assisted", "auto"] | None = None
```

`ExperimentPlan`（或 plan version 元数据）增加：

```python
review_mode: Literal["expert", "assisted", "delegated"] = "expert"
delegated_at: datetime | None = None      # 托管授权时间
delegated_by: UUID | None = None          # 授权用户
```

### 6.2 闸门改为模式感知

`unconfirmed_required()` 拆成两个概念：

- `unconfirmed_critical()`：Tier1 中 `requires_confirmation and not human_confirmed` 的项。
- 批准判定（`approve_plan`）：
  - **专家/辅助模式**：`unconfirmed_critical()` 非空 → `409`（与现状一致，但只看关键项；常规项未确认不拦）。
  - **托管模式**：要求 `delegated_at` 非空且属于当前用户。满足则放行，但**不修改任何 `human_confirmed`**；被自动接受的项 `confirmed_by="auto"`、`human_confirmed` 保持 `False`。
- 这样：托管不是"假装人工确认了"，而是"用户显式授权系统代为接受，且系统如实记录这是 auto"。

### 6.3 报告如实声明

`ReportPanel` / 报告生成（worker）增加：

- 顶部审核模式徽章：`人工逐项核验` / `AI 辅助审核` / `托管自动（未逐项核验）`。
- 托管模式报告显著位置声明：**"本实验关键假设由系统自动接受，未经人工逐项核验；结果仅供趋势参考。"**
- 假设清单每项显示 `confirmed_by` 来源标签。
- 绝不把 `auto`/`ai_assisted` 接受的项渲染成"论文原文"。

### 6.4 Trace 留痕

- 每次确认/采纳/托管授权写入 Trace：`field_path, old→new, confirmed_by, review_mode, actor, timestamp`。
- **敏感凭据不入 Trace**（沿用现有约束）。
- 报告 trace refs 可回溯到具体决策。

---

## 7. 前端 UX 流程

### spec-review 页（理解卡片审核）
- 顶部模式选择器（专家/辅助/托管）。
- 卡片字段按"原文 / 原文描述 / 推断 / 缺失"着色；`PAPER_DESCRIBED` 展示引文+页码，无需确认。
- 推断/缺失字段可展开白话注解。
- 辅助模式：高置信项预勾选 + "接受全部高置信"按钮。

### plan-approval 页（计划批准）
- 假设按 tier 分组卡片：关键（红边）/ 常规（amber）/ 说明（灰，折叠）。
- 每张关键卡片：白话注解（可折叠）+ AI 推荐卡（accept/modify + confidence + reasoning + risk）+ "采纳"/"修改"按钮。
- 常规区："一键采纳全部常规项"。
- sticky 底栏："还需确认 N 项关键假设" + 批准按钮（N=0 高亮）。
- 托管模式：进入时一次性授权弹窗 → 确认后所有项标 `auto`，底栏直接可批准。

### 报告页
- 审核模式徽章 + 托管声明 + 每项 `confirmed_by` 标签（§6.3）。

---

## 8. 后端改动清单

| 层 | 文件 | 改动 |
|---|---|---|
| schemas | `evidence.py` | 新增 `PAPER_DESCRIBED`；校验器要求 quote+page |
| schemas | `plan.py` | `Assumption` 加 `risk_tier/auto_acceptable/confirmed_by`；`PlanParam` 加 `confirmed_by`；`ExperimentPlan` 加 `review_mode/delegated_at/delegated_by`；`unconfirmed_required()` → `unconfirmed_critical()` + 模式感知判定 |
| agent | 理解抽取器 | prose 预处理标 `PAPER_DESCRIBED`（带 quote+page） |
| agent | `planning/engine.py` | `_preprocessing` 对 `PAPER_DESCRIBED` 不生成强确认；生成假设时打 `risk_tier/auto_acceptable` |
| agent | `planning/assumptions.py` | `param_from_evidence` 输出 tier；新增解释/推荐生成端点（LLM，缓存） |
| agent | 新路由 | `POST /v1/explain`、`POST /v1/recommend`（仅生成建议，不写 DB） |
| api | `routers/experiments.py` | `approve_plan` 模式感知闸门；新增批量确认端点 `POST .../plans/{v}/confirm-batch`；托管授权端点 `POST .../plans/{v}/delegate` |
| api | `services` | `apply_plan_edits` 记录 `confirmed_by`；批量确认写新 version 或更新确认态（按版本不可变约束设计） |
| worker | 报告生成 | 注入 `review_mode` + 托管声明 + `confirmed_by` 标签 |
| web | `PlanClient.tsx` / spec-review / `ReportPanel.tsx` | 模式选择、分组、注解、推荐卡、批量、sticky 计数、报告徽章 |
| DB | migration | `experiment_plans` 加 `review_mode/delegated_at/delegated_by`；确认态字段（注意 config 不可变触发器——确认态应放独立列或新 version，不能改旧 config_json） |

> **版本不可变约束**：现有 DB 触发器禁止覆盖旧 `config_json`。确认/采纳属于"元数据变更"，应落在**独立确认态列**或**生成新 plan version**，不得回写旧 config。实现时二选一并保持一致（推荐：确认态独立表/列，plan config 仍只在编辑时新增 version）。

---

## 9. 分阶段实施

**Phase 1 — 减负（无 LLM 成本，先解决"太多"）**
- A：`PAPER_DESCRIBED` + 抽取器 + `_preprocessing` 修复（消除 3 项伪推断）。
- B：`risk_tier/auto_acceptable` 分级。
- F：前端分组 + 批量采纳常规项 + sticky 计数。
- 闸门：常规项未确认不再拦（只看关键项）。
- 验收：LeNet-5 强制确认从 6 → ≤3，专家模式保留人工确认留痕。

**Phase 2 — 辅助（解决"看不懂"）**
- C：白话注解端点 + 缓存 + 前端折叠卡。
- D：AI 推荐端点 + 逐项/批量采纳 + `confirmed_by="ai_assisted"`。
- E：辅助模式（默认）+ G 高置信预勾选。
- 验收：非专家在辅助模式下 ≤2 次决策完成 LeNet-5 批准。

**Phase 3 — 托管 + 溯源闭环**
- E：托管模式 + 一次性授权弹窗 + `delegated_at/by`。
- §6：模式感知闸门 + 报告徽章/声明 + Trace 留痕。
- H（可选）：渐进信任预填。
- 验收：托管 0 点击完成，报告显著标注"未逐项核验"，全程可追溯。

---

## 10. 验收标准

1. **减负**：LeNet-5 场景强制确认项 ≤3（Phase 1），辅助模式用户决策 ≤2（Phase 2），托管 0 点击（Phase 3）。
2. **非专家可走通**：不懂论文的用户在辅助/托管模式下能完成上传→审核→批准→执行→报告全流程。
3. **专家模式定位**（2026-11-20 修订）：专家模式编辑默认记人工确认、报告标"人工逐项核验"；闸门与辅助模式一致只看关键项，不恢复旧的全量逐项硬闸门。
4. **不伪造**：任何 `auto/ai_assisted` 接受的项 `source` 仍为 `INFERRED`，缺失证据仍标缺失；报告不把它当原文。
5. **可追溯**：每项决策有 `confirmed_by + review_mode + actor + timestamp`，Trace 可回溯，凭据不入 Trace。
6. **托管诚实**：托管报告显著标注"未经人工逐项核验"。
7. **关键项不静默**：Tier1 在专家/辅助模式未确认仍 `409`；托管需显式授权。
8. **测试**：schemas 校验器、planner 分级、闸门模式分支、批量/托管端点、报告声明均有单元/E2E 覆盖；`uv run --no-sync pytest` + ruff 全绿。

---

## 11. 风险与权衡

| 风险 | 缓解 |
|---|---|
| 托管模式被滥用导致"假复现" | 报告强制声明 + `confirmed_by=auto` 留痕 + 关键项仍标提案；MVP 缩放本就不可判定 Full Reproduction（沿用现有报告逻辑） |
| `PAPER_DESCRIBED` 被抽取器塞入推断 | 校验器强制 quote+page；抽取 prompt 明确"只有原文确实描述才用此档" |
| LLM 解释/推荐产生幻觉 | 解释标注"非原文"；推荐只建议不自动改 plan；critical 不自动接受；缓存按 card_hash 失效 |
| 版本不可变触发器冲突 | 确认态走独立列/表，不回写旧 config_json（§8 注） |
| LLM 成本/延迟 | 解释/推荐按字段缓存；Phase 1 完全无 LLM 成本；批量一次生成 |
| 分级启发式误判 | 专家可手动调 tier（留痕）；critical 名单保守（宁可多拦关键项） |

---

## 12. 评审决策点（已决）

1. 确认态存储：**每次确认/采纳生成新 plan/card version**（config_json 不可变触发器下最一致的方案；旧版本完整保留可回溯）。
2. 托管模式对所有登录用户开放，进入前一次性显式授权（consent），授权绑定用户与时间，任何人工编辑即撤销。
3. `critical` 名单按 §5B 落地，planner 启发式打标；**人工可在 UI 调整 tier**（`POST .../plans/{v}/tier`，新版本 + `confirmed_by=human` 留痕，调整即撤销托管授权）。
4. 高置信阈值默认 0.9，可配置（`review_selection_threshold`），采纳留痕记录阈值。
5. Phase 1 → 2 → 3 已全部落地（2026-11-19/20）。

> 2026-11-20 UX 补齐：推荐卡独立"修改…"按钮（预填建议值走人工编辑路径）、sticky 底栏"已确认/总数"计数 + 进度条、info 组默认折叠、报告徽章措辞统一为"人工逐项核验 / AI 辅助审核 / 托管自动（未逐项核验）"、`PAPER_DESCRIBED` 与 `PAPER_EXPLICIT` 徽章配色区分。