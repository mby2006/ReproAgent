"""Align grounded paper claims with the exact approved plan and its run evidence."""

from reproagent_schemas import EvaluateRequest, EvidenceRef, EvidenceStatus
from reproagent_schemas.plan import execution_metric
from reproagent_schemas.report import ParameterComparison


def parameter_comparisons(request: EvaluateRequest, methods, experiments):
    card, plan = request.card, request.plan
    rows = []
    labels = {
        "training_samples": "训练样本数", "test_samples": "测试样本数", "epochs": "训练轮数",
        "batch_size": "批大小", "optimizer": "优化器", "learning_rate": "学习率",
        "random_seed": "随机种子",
    }
    for check in experiments:
        key = check.item.value
        if key not in labels:
            continue
        field = card.optimizer if key == "optimizer" else card.hyperparameters.get(key)
        paper = evidence(field)
        param = getattr(plan.training_config, key, None)
        path = check.plan_evidence.field_path if check.plan_evidence else None
        deviation = next((d for d in plan.deviations_from_paper if d.field_path == path), None)
        reason = decision(param)
        if deviation:
            reason = " · ".join(filter(None, [reason, deviation.reason]))
        rows.append(ParameterComparison(
            key=key, label=labels[key], paper=paper.detail if paper else None,
            paper_evidence=paper, plan=text_value(check.plan_value),
            actual=text_value(check.actual_value), actual_evidence=check.actual_evidence,
            decision=reason, note=check.note,
        ))
    settings = {
        "architecture": ("模型结构", plan.model_cfg.architecture),
        "loss_function": ("损失函数", plan.training_config.loss_function.value),
        "dataset": ("数据集", plan.dataset_config.name),
    }
    for check in methods:
        key = check.item.value
        if key in settings:
            label, planned = settings[key]
            rows.append(ParameterComparison(
                key=key, label=label, paper=check.paper_value if check.paper_evidence else None,
                paper_evidence=check.paper_evidence, plan=str(planned),
                actual=check.implementation_value, actual_evidence=check.implementation_evidence,
                decision=(
                    decision(plan.training_config.loss_function) if key == "loss_function" else None
                ),
                note=check.note,
            ))
    target = request.metrics.get("target_metric")
    wanted = execution_metric(plan.evaluation_config.target_metric)
    field = next((f for f in card.evaluation_metrics if evidence(f)
                  and execution_metric(str(f.value)) == wanted), None)
    paper = evidence(field)
    rows.append(ParameterComparison(
        key="target_metric", label="评价指标", paper=paper.detail if paper else None,
        paper_evidence=paper, plan=plan.evaluation_config.target_metric, actual=target,
        actual_evidence=EvidenceRef(kind="metrics", field_path="metrics_json.target_metric",
                                    detail=target) if target else None,
        note="比较数值前需核对测试集、单位和指标方向。",
    ))
    return rows


def text_value(value):
    return str(value) if value is not None else None


def evidence(field):
    if field is None or field.status not in (
        EvidenceStatus.PAPER_EXPLICIT,
        EvidenceStatus.PAPER_DESCRIBED,
    ):
        return None
    return EvidenceRef(kind="paper", quote=field.evidence_quote, source_page=field.source_page,
                       source_section=field.source_section, detail=text_value(field.value))


def decision(param):
    if param is None:
        return None
    sources = {"paper_explicit": "论文明确", "inferred": "推定值",
               "human_confirmed": "用户设定", "mvp_scaling": "缩放设置"}
    actors = {"human": "人工确认", "ai_assisted": "人工采纳 AI 建议", "auto": "系统自动采纳"}
    parts = [sources.get(param.source.value, param.source.value), param.rationale]
    if param.confirmed_by:
        parts.append(actors.get(param.confirmed_by.value, param.confirmed_by.value))
    return " · ".join(p for p in parts if p)
