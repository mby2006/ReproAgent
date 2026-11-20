import hashlib
import ipaddress
import socket
from datetime import datetime
from typing import Annotated
from urllib.parse import urlparse
from uuid import UUID, uuid4

import httpx
import psycopg
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from psycopg import sql
from psycopg.rows import dict_row
from reproagent_schemas import (
    CardConfirmBatchRequest,
    CardEditRequest,
    CardExplainRequest,
    CardVersion,
    ConfirmationSource,
    DelegateReviewRequest,
    ExplainResponse,
    FieldEdit,
    PaperSource,
    PaperStatus,
    ReproductionCard,
    ReviewDecision,
    ReviewMode,
    ReviewModeRequest,
    ReviewRequest,
    SchemaModel,
    SpecOrigin,
)
from storage3.exceptions import StorageApiError

from ..auth import get_current_user_id
from ..clients.supabase import make_service_client
from ..services import EditError, apply_edits, insert_versioned
from ..services.card_edits import delegate_card
from ..services.review import generate_review

router = APIRouter(prefix="/v1", tags=["papers"])

MAX_PDF_BYTES = 50 * 1024 * 1024
VERSION_COLUMNS = "id,paper_id,version,origin,parent_version_id,spec_json,status,created_at"


class PaperUploadResponse(SchemaModel):
    paper_id: UUID
    workflow_id: UUID
    thread_id: str
    status: str = "queued"


class PaperSummary(SchemaModel):
    id: UUID
    title: str
    source: PaperSource
    status: PaperStatus
    created_at: datetime


class SignedPdfUrl(SchemaModel):
    url: str
    expires_in: int = 3600


class CardVersionMeta(SchemaModel):
    id: UUID
    version: int
    origin: SpecOrigin
    parent_version_id: UUID | None = None
    created_at: datetime


def _admin(request: Request):
    return make_service_client(request.app.state.settings)


def _default_project(admin, user_id: UUID) -> str:
    """Get-or-create the user's default project (ownership root for papers)."""
    rows = (
        admin.table("projects")
        .select("id")
        .eq("user_id", str(user_id))
        .eq("is_default", True)
        .execute()
        .data
    )
    if rows:
        return rows[0]["id"]
    try:
        inserted = (
            admin.table("projects")
            .insert({"user_id": str(user_id), "name": "默认项目", "is_default": True})
            .execute()
            .data
        )
        return inserted[0]["id"]
    except Exception:
        # Concurrent uploads may race the partial unique index; re-read the winner.
        rows = (
            admin.table("projects")
            .select("id")
            .eq("user_id", str(user_id))
            .eq("is_default", True)
            .execute()
            .data
        )
        if rows:
            return rows[0]["id"]
        raise


def _get_paper(admin, paper_id: UUID, user_id: UUID) -> dict:
    rows = (
        admin.table("papers")
        .select("*, projects!inner(user_id)")
        .eq("id", str(paper_id))
        .eq("projects.user_id", str(user_id))
        .execute()
        .data
    )
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Paper not found")
    return rows[0]


def _to_card_version(row: dict) -> CardVersion:
    return CardVersion(
        id=row["id"],
        paper_id=row["paper_id"],
        version=row["version"],
        origin=row["origin"],
        parent_version_id=row["parent_version_id"],
        card=row["spec_json"],
        created_at=row["created_at"],
    )


async def _enqueue_workflow(request: Request, workflow_id: UUID) -> None:
    arq = getattr(request.app.state, "arq", None)
    if arq is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Job queue unavailable")
    try:
        await arq.enqueue_job("run_workflow_task", str(workflow_id))
    except Exception as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, f"Failed to enqueue job: {exc}"
        ) from exc


def _create_workflow(admin, project_id: str, paper_id: UUID) -> UUID:
    workflow_id = uuid4()
    admin.table("workflows").insert(
        {
            "id": str(workflow_id),
            "thread_id": str(workflow_id),
            "project_id": project_id,
            "paper_id": str(paper_id),
            "status": "uploaded",
            "budget_json": {},
        }
    ).execute()
    return workflow_id


def _normalize_paper_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "URL must be http(s)")
    if parsed.hostname in ("arxiv.org", "www.arxiv.org") and parsed.path.startswith("/abs/"):
        return f"https://arxiv.org/pdf/{parsed.path[len('/abs/') :]}"
    return url


def _assert_public_host(hostname: str) -> None:
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "Cannot resolve URL host"
        ) from exc
    for info in infos:
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "URL host is not a public address"
            )


async def _download_pdf(url: str) -> bytes:
    try:
        return await _fetch_pdf(url)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"Failed to download URL: {exc}"
        ) from exc


async def _fetch_pdf(url: str) -> bytes:
    current = _normalize_paper_url(url)
    chunks: list[bytes] = []
    async with httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False) as client:
        for _ in range(6):
            parsed = urlparse(current)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY, "Redirect target must be http(s)"
                )
            # re-check every hop: a redirect must not land on a private address
            _assert_public_host(parsed.hostname)
            async with client.stream("GET", current) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location")
                    if not location:
                        raise HTTPException(
                            status.HTTP_422_UNPROCESSABLE_ENTITY, "Redirect without location"
                        )
                    current = str(httpx.URL(current).join(location))
                    continue
                if response.status_code != 200:
                    raise HTTPException(
                        status.HTTP_422_UNPROCESSABLE_ENTITY,
                        f"URL returned HTTP {response.status_code}",
                    )
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_PDF_BYTES:
                        raise HTTPException(
                            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "PDF exceeds 50 MB"
                        )
                    chunks.append(chunk)
            break
        else:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Too many redirects")
    data = b"".join(chunks)
    if not data.startswith(b"%PDF"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "URL did not return a PDF")
    return data


@router.post("/papers", response_model=PaperUploadResponse, status_code=status.HTTP_202_ACCEPTED)
async def upload_paper(
    request: Request,
    user_id: Annotated[UUID, Depends(get_current_user_id)],
    file: Annotated[UploadFile | None, File()] = None,
    title: Annotated[str, Form(max_length=500)] = "",
    url: Annotated[str, Form(max_length=2000)] = "",
) -> PaperUploadResponse:
    source_url: str | None = None
    if file is not None:
        pdf_bytes = await file.read()
        if len(pdf_bytes) == 0 or not pdf_bytes.startswith(b"%PDF"):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "File is not a PDF")
        if len(pdf_bytes) > MAX_PDF_BYTES:
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "PDF exceeds 50 MB")
        if not title.strip():
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Title is required")
        source = PaperSource.UPLOAD
    elif url.strip():
        url = url.strip()
        pdf_bytes = await _download_pdf(url)
        source = PaperSource.URL
        source_url = url
        if not title.strip():
            title = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1] or urlparse(url).hostname
    else:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "Provide a PDF file or a URL"
        )

    admin = _admin(request)
    project_id = _default_project(admin, user_id)
    paper_id = uuid4()
    pdf_path = f"{user_id}/{paper_id}.pdf"
    admin.storage.from_("papers").upload(pdf_path, pdf_bytes, {"content-type": "application/pdf"})
    admin.table("papers").insert(
        {
            "id": str(paper_id),
            "project_id": project_id,
            "title": title.strip()[:500],
            "source": source.value,
            "source_url": source_url,
            "pdf_path": pdf_path,
            "file_sha256": hashlib.sha256(pdf_bytes).hexdigest(),
            "status": PaperStatus.UPLOADED.value,
        }
    ).execute()
    workflow_id = _create_workflow(admin, project_id, paper_id)
    await _enqueue_workflow(request, workflow_id)
    return PaperUploadResponse(
        paper_id=paper_id, workflow_id=workflow_id, thread_id=str(workflow_id)
    )


@router.get("/papers", response_model=list[PaperSummary])
async def list_papers(
    request: Request, user_id: Annotated[UUID, Depends(get_current_user_id)]
) -> list[PaperSummary]:
    admin = _admin(request)
    projects = admin.table("projects").select("id").eq("user_id", str(user_id)).execute().data
    project_ids = [row["id"] for row in projects]
    if not project_ids:
        return []
    rows = (
        admin.table("papers")
        .select("id,title,source,status,created_at")
        .in_("project_id", project_ids)
        .order("created_at", desc=True)
        .execute()
        .data
    )
    return [PaperSummary.model_validate(row) for row in rows]


@router.get("/papers/{paper_id}", response_model=PaperSummary)
async def get_paper(
    request: Request, paper_id: UUID, user_id: Annotated[UUID, Depends(get_current_user_id)]
) -> PaperSummary:
    admin = _admin(request)
    _get_paper(admin, paper_id, user_id)
    rows = (
        admin.table("papers")
        .select("id,title,source,status,created_at")
        .eq("id", str(paper_id))
        .execute()
        .data
    )
    return PaperSummary.model_validate(rows[0])


@router.delete("/papers/{paper_id}")
def delete_paper(
    request: Request, paper_id: UUID, user_id: Annotated[UUID, Depends(get_current_user_id)]
) -> dict[str, str]:
    admin = _admin(request)
    _get_paper(admin, paper_id, user_id)
    settings = request.app.state.settings
    try:
        with psycopg.connect(settings.supabase_db_url, row_factory=dict_row) as conn:
            # Keep new work and status transitions blocked until deletion commits.
            paper = conn.execute(
                "select pa.* from public.papers pa "
                "join public.projects pr on pr.id = pa.project_id "
                "where pa.id = %s and pr.user_id = %s for update of pa",
                (paper_id, user_id),
            ).fetchone()
            if paper is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Paper not found")
            workflows = conn.execute(
                "select status, thread_id from public.workflows where paper_id = %s for update",
                (paper_id,),
            ).fetchall()
            inactive = {"awaiting_spec_review", "awaiting_plan_approval", "completed", "failed"}
            if any(w["status"] not in inactive for w in workflows):
                raise HTTPException(status.HTTP_409_CONFLICT, "论文工作流正在运行，请完成后再删除")
            jobs = conn.execute(
                "select status from public.understanding_jobs where paper_id = %s for update",
                (paper_id,),
            ).fetchall()
            if any(j["status"] in {"queued", "running"} for j in jobs):
                raise HTTPException(status.HTTP_409_CONFLICT, "PDF 正在解析，请完成后再删除")
            experiments = conn.execute(
                "select id, status from public.experiments where paper_id = %s for update",
                (paper_id,),
            ).fetchall()
            if any(e["status"] in {"planning", "running"} for e in experiments):
                raise HTTPException(status.HTTP_409_CONFLICT, "关联实验正在运行，请完成后再删除")
            runs = conn.execute(
                "select id, status from public.execution_runs "
                "where experiment_id = any(%s) for update",
                ([e["id"] for e in experiments],),
            ).fetchall()
            if any(r["status"] in {"queued", "running"} for r in runs):
                raise HTTPException(status.HTTP_409_CONFLICT, "关联实验正在运行，请完成后再删除")
            objects = conn.execute(
                "select name from storage.objects "
                "where bucket_id = 'runs' and split_part(name, '/', 1) = any(%s)",
                ([str(r["id"]) for r in runs],),
            ).fetchall()
            thread_ids = [w["thread_id"] for w in workflows]
            if thread_ids:
                for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
                    exists = conn.execute(
                        "select to_regclass(%s) as name", (f"public.{table}",)
                    ).fetchone()["name"]
                    if exists:
                        conn.execute(
                            sql.SQL("delete from {} where thread_id = any(%s)").format(
                                sql.Identifier("public", table)
                            ),
                            (thread_ids,),
                        )
            # Check FK cascades before touching files; storage errors roll back database changes.
            conn.execute("delete from public.papers where id = %s", (paper_id,))
            paths = [obj["name"] for obj in objects]
            for offset in range(0, len(paths), 100):
                admin.storage.from_("runs").remove(paths[offset : offset + 100])
            if paper["pdf_path"]:
                admin.storage.from_("papers").remove([paper["pdf_path"]])
    except psycopg.errors.ForeignKeyViolation as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "论文仍被其他记录引用，无法删除"
        ) from exc
    except (StorageApiError, httpx.HTTPError) as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "删除存储文件失败，请重试"
        ) from exc
    return {"paper_id": str(paper_id), "status": "deleted"}


@router.get("/papers/{paper_id}/pdf", response_model=SignedPdfUrl)
async def get_pdf_url(
    request: Request, paper_id: UUID, user_id: Annotated[UUID, Depends(get_current_user_id)]
) -> SignedPdfUrl:
    admin = _admin(request)
    paper = _get_paper(admin, paper_id, user_id)
    if not paper["pdf_path"]:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Paper has no stored PDF")
    result = admin.storage.from_("papers").create_signed_url(paper["pdf_path"], 3600)
    url = result.get("signedURL") or result.get("signedUrl")
    if not url:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Storage did not return a signed URL")
    settings = request.app.state.settings
    public = settings.browser_supabase_url
    internal = settings.supabase_url.rstrip("/")
    if url.startswith("/"):
        url = public + url
    elif url.startswith(internal):
        url = public + url[len(internal) :]
    return SignedPdfUrl(url=url)


@router.get("/papers/{paper_id}/card", response_model=CardVersion)
async def get_card(
    request: Request,
    paper_id: UUID,
    user_id: Annotated[UUID, Depends(get_current_user_id)],
    version: int | None = None,
) -> CardVersion:
    admin = _admin(request)
    _get_paper(admin, paper_id, user_id)
    query = admin.table("reproduction_specs").select(VERSION_COLUMNS).eq("paper_id", str(paper_id))
    if version is not None:
        rows = query.eq("version", version).execute().data
    else:
        rows = query.order("version", desc=True).limit(1).execute().data
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No reproduction card for this paper yet")
    return _to_card_version(rows[0])


@router.get("/papers/{paper_id}/versions", response_model=list[CardVersionMeta])
async def list_versions(
    request: Request, paper_id: UUID, user_id: Annotated[UUID, Depends(get_current_user_id)]
) -> list[CardVersionMeta]:
    admin = _admin(request)
    _get_paper(admin, paper_id, user_id)
    rows = (
        admin.table("reproduction_specs")
        .select("id,version,origin,parent_version_id,created_at")
        .eq("paper_id", str(paper_id))
        .order("version")
        .execute()
        .data
    )
    return [CardVersionMeta.model_validate(row) for row in rows]


@router.post(
    "/papers/{paper_id}/card/edits",
    response_model=CardVersion,
    status_code=status.HTTP_201_CREATED,
)
async def edit_card(
    request: Request,
    paper_id: UUID,
    body: CardEditRequest,
    user_id: Annotated[UUID, Depends(get_current_user_id)],
) -> CardVersion:
    admin = _admin(request)
    _get_paper(admin, paper_id, user_id)
    base_rows = (
        admin.table("reproduction_specs")
        .select(VERSION_COLUMNS)
        .eq("paper_id", str(paper_id))
        .eq("version", body.base_version)
        .execute()
        .data
    )
    if not base_rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Base version not found")
    base = base_rows[0]
    try:
        edited = apply_edits(
            ReproductionCard.model_validate(base["spec_json"]), body, actor=user_id,
        )
    except EditError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    def next_version() -> int:
        latest = (
            admin.table("reproduction_specs")
            .select("version")
            .eq("paper_id", str(paper_id))
            .order("version", desc=True)
            .limit(1)
            .execute()
            .data
        )
        return latest[0]["version"] + 1 if latest else 1

    def insert(v: int) -> list:
        return (
            admin.table("reproduction_specs")
            .insert(
                {
                    "paper_id": str(paper_id),
                    "version": v,
                    "origin": SpecOrigin.USER_EDIT.value,
                    "parent_version_id": base["id"],
                    "spec_json": edited.model_dump(mode="json"),
                    "status": "confirmed",
                }
            )
            .execute()
            .data
        )

    inserted = insert_versioned(next_version, insert)
    return _to_card_version(inserted[0])


def _card_review_context(admin, paper_id: UUID, version: int, user_id: UUID):
    _get_paper(admin, paper_id, user_id)
    rows = (
        admin.table("reproduction_specs").select(VERSION_COLUMNS)
        .eq("paper_id", str(paper_id)).eq("version", version).execute().data
    )
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Card version not found")
    return rows[0], ReviewRequest(
        card=ReproductionCard.model_validate(rows[0]["spec_json"]), scope=str(user_id),
    )


@router.post("/papers/{paper_id}/card/explain", response_model=ExplainResponse)
async def explain_card(
    request: Request, paper_id: UUID, body: CardExplainRequest,
    user_id: Annotated[UUID, Depends(get_current_user_id)],
) -> ExplainResponse:
    _, context = _card_review_context(_admin(request), paper_id, body.base_version, user_id)
    context.field_paths = body.field_paths
    return await generate_review(request, context)


@router.post(
    "/papers/{paper_id}/card/confirm-batch", response_model=CardVersion,
    status_code=status.HTTP_201_CREATED,
)
async def confirm_card_batch(
    request: Request, paper_id: UUID, body: CardConfirmBatchRequest,
    user_id: Annotated[UUID, Depends(get_current_user_id)],
) -> CardVersion:
    admin = _admin(request)
    base, context = _card_review_context(admin, paper_id, body.base_version, user_id)

    def next_version() -> int:
        rows = (
            admin.table("reproduction_specs").select("id,version")
            .eq("paper_id", str(paper_id)).order("version", desc=True).limit(1).execute().data
        )
        if not rows or rows[0]["id"] != base["id"]:
            raise HTTPException(status.HTTP_409_CONFLICT, "理解卡版本已变化，请刷新后重新审核")
        return rows[0]["version"] + 1

    next_version()
    if context.context_hash() != body.context_hash:
        raise HTTPException(status.HTTP_409_CONFLICT, "解释上下文已变化，请重新获取")
    response = await generate_review(request, context)
    if (
        len(set(body.field_paths)) != len(body.field_paths)
        or set(body.field_paths) - set(response.preselected_paths)
    ):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "只能批量确认高置信常规推断项")
    fields = {field.field_path: field for field in context.review_fields()}
    edited = apply_edits(
        context.card, CardEditRequest(
            base_version=body.base_version, review_mode=body.review_mode,
            edits=[
                FieldEdit(field_path=path, value=fields[path].value) for path in body.field_paths
            ],
        ), confirmed_by=ConfirmationSource.AI_ASSISTED, actor=user_id,
    )
    edited.review_selection_threshold = response.selection_threshold

    def insert(v: int) -> dict:
        return admin.table("reproduction_specs").insert({
            "paper_id": str(paper_id), "version": v, "origin": SpecOrigin.USER_EDIT.value,
            "parent_version_id": base["id"], "spec_json": edited.model_dump(mode="json"),
            "status": base["status"],
        }).execute().data[0]

    return _to_card_version(insert_versioned(next_version, insert))


@router.post(
    "/papers/{paper_id}/card/delegate", response_model=CardVersion,
    status_code=status.HTTP_201_CREATED,
)
async def delegate_card_review(
    request: Request, paper_id: UUID, body: DelegateReviewRequest,
    user_id: Annotated[UUID, Depends(get_current_user_id)],
) -> CardVersion:
    admin = _admin(request)
    base, context = _card_review_context(admin, paper_id, body.base_version, user_id)

    def next_version() -> int:
        rows = (
            admin.table("reproduction_specs").select("id,version")
            .eq("paper_id", str(paper_id)).order("version", desc=True).limit(1).execute().data
        )
        if not rows or rows[0]["id"] != base["id"]:
            raise HTTPException(status.HTTP_409_CONFLICT, "理解卡版本已变化，请刷新后重新授权")
        return rows[0]["version"] + 1

    next_version()
    card = delegate_card(context.card, actor=user_id)
    row = insert_versioned(
        next_version,
        lambda v: admin.table("reproduction_specs").insert({
            "paper_id": str(paper_id), "version": v, "origin": SpecOrigin.USER_EDIT.value,
            "parent_version_id": base["id"], "spec_json": card.model_dump(mode="json"),
            "status": "confirmed",
        }).execute().data[0],
    )
    return _to_card_version(row)


@router.post(
    "/papers/{paper_id}/reparse",
    response_model=PaperUploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def reparse_paper(
    request: Request, paper_id: UUID, user_id: Annotated[UUID, Depends(get_current_user_id)]
) -> PaperUploadResponse:
    admin = _admin(request)
    paper = _get_paper(admin, paper_id, user_id)
    if not paper["pdf_path"]:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Paper has no stored PDF")
    workflow_id = _create_workflow(admin, paper["project_id"], paper_id)
    await _enqueue_workflow(request, workflow_id)
    return PaperUploadResponse(
        paper_id=paper_id, workflow_id=workflow_id, thread_id=str(workflow_id)
    )


@router.post(
    "/papers/{paper_id}/card/confirm",
    response_model=CardVersion,
    status_code=status.HTTP_201_CREATED,
)
async def confirm_card(
    request: Request,
    paper_id: UUID,
    user_id: Annotated[UUID, Depends(get_current_user_id)],
    body: ReviewModeRequest | None = None,
) -> CardVersion:
    """Confirm the latest card as-is (no edits), creating a confirmed version."""
    admin = _admin(request)
    _get_paper(admin, paper_id, user_id)
    latest = (
        admin.table("reproduction_specs")
        .select(VERSION_COLUMNS)
        .eq("paper_id", str(paper_id))
        .order("version", desc=True)
        .limit(1)
        .execute()
        .data
    )
    if not latest:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No reproduction card to confirm")
    base = latest[0]
    card = ReproductionCard.model_validate(base["spec_json"])
    previous_mode = card.review_mode
    card.review_mode = body.review_mode if body else (
        ReviewMode.ASSISTED if previous_mode is ReviewMode.DELEGATED else previous_mode
    )
    card.delegated_at = None
    card.delegated_by = None
    card.review_decisions = [ReviewDecision(
        field_path="card.status", before=base["status"], after="confirmed",
        confirmed_by=ConfirmationSource.HUMAN, review_mode=card.review_mode, actor=user_id,
    )]
    if previous_mode != card.review_mode:
        card.review_decisions.append(ReviewDecision(
            field_path="review_mode", before=previous_mode, after=card.review_mode,
            review_mode=card.review_mode, actor=user_id,
        ))

    def next_version() -> int:
        rows = (
            admin.table("reproduction_specs")
            .select("id,version")
            .eq("paper_id", str(paper_id))
            .order("version", desc=True)
            .limit(1)
            .execute()
            .data
        )
        if not rows or rows[0]["id"] != base["id"]:
            raise HTTPException(status.HTTP_409_CONFLICT, "理解卡版本已变化，请刷新后重新确认")
        return rows[0]["version"] + 1

    def insert(v: int) -> list:
        return (
            admin.table("reproduction_specs")
            .insert(
                {
                    "paper_id": str(paper_id),
                    "version": v,
                    "origin": SpecOrigin.USER_EDIT.value,
                    "parent_version_id": base["id"],
                    "spec_json": card.model_dump(mode="json"),
                    "status": "confirmed",
                }
            )
            .execute()
            .data
        )

    inserted = insert_versioned(next_version, insert)
    return _to_card_version(inserted[0])
