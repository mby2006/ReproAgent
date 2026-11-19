from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import Field
from reproagent_schemas import SchemaModel

from ..auth import get_current_user_id

router = APIRouter(prefix="/v1", tags=["reproductions"])


class ReproductionCreate(SchemaModel):
    """Web-facing body; user_id is taken from the JWT, never from the client."""

    paper_id: UUID
    title: str = Field(min_length=1)
    paper_text: str | None = None
    source_url: str | None = None


@router.post("/reproductions", status_code=status.HTTP_202_ACCEPTED)
async def start_reproduction(
    body: ReproductionCreate,
    user_id: Annotated[UUID, Depends(get_current_user_id)],
) -> None:
    raise HTTPException(
        status.HTTP_501_NOT_IMPLEMENTED,
        "Reproduction workflow is not implemented in phase 1",
    )
