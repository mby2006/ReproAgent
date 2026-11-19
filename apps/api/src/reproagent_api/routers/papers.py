import hashlib
import ipaddress
import socket
from datetime import datetime
from typing import Annotated
from urllib.parse import urlparse
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from reproagent_schemas import (
    CardEditRequest,
    CardVersion,
    PaperSource,
    PaperStatus,
    ReproductionCard,
    SchemaModel,
    SpecOrigin,
)

from ..auth import get_current_user_id
from ..clients.supabase import make_service_client
from ..services import EditError, apply_edits, insert_versioned

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
        edited = apply_edits(ReproductionCard.model_validate(base["spec_json"]), body)
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

    def next_version() -> int:
        rows = (
            admin.table("reproduction_specs")
            .select("version")
            .eq("paper_id", str(paper_id))
            .order("version", desc=True)
            .limit(1)
            .execute()
            .data
        )
        return rows[0]["version"] + 1 if rows else 1

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
