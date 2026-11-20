import httpx
from fastapi import HTTPException, Request, status
from reproagent_schemas import (
    ExplainResponse,
    RecommendResponse,
    ReviewRequest,
    preselected_paths,
)

from ..clients.agent import AgentClient


def validate_review_response(
    context: ReviewRequest, result: ExplainResponse | RecommendResponse,
    *, recommend: bool = False,
) -> None:
    fields = context.review_fields()
    items = result.recommendations if recommend else result.explanations
    paths = [item.field_path for item in items]
    if (
        result.context_hash != context.context_hash()
        or len(set(paths)) != len(paths)
        or set(paths) != {field.field_path for field in fields}
    ):
        raise ValueError("review response does not match its context")
    eligible = set(preselected_paths(fields, result.selection_threshold))
    if not set(result.preselected_paths).issubset(eligible):
        raise ValueError("invalid preselection")
    if recommend:
        by_path = {field.field_path: field for field in fields}
        ids = [item.id for item in items]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate recommendation ids")
        for item in items:
            field = by_path[item.field_path]
            if item.action == "accept" and (
                item.suggested_value != field.value
                or isinstance(item.suggested_value, bool) != isinstance(field.value, bool)
            ):
                raise ValueError("accept recommendation changes the value")
            if item.field_path in result.preselected_paths and (
                item.action != "accept" or item.confidence < result.selection_threshold
            ):
                raise ValueError("invalid recommendation preselection")


async def generate_review(
    request: Request, context: ReviewRequest, *, recommend: bool = False,
) -> ExplainResponse | RecommendResponse:
    try:
        context.review_fields()
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    agent = AgentClient(
        request.app.state.settings,
        transport=getattr(request.app.state, "agent_transport", None),
    )
    try:
        result = await agent.recommend(context) if recommend else await agent.explain(context)
        validate_review_response(context, result, recommend=recommend)
        return result
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "AI 辅助暂时不可用，请重试或切换专家模式"
        ) from exc
    finally:
        await agent.aclose()
