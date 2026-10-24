"""Study Report (CSR) endpoints — the post-study bookend to protocols.

Flow:
  1. POST /api/protocols/{id}/study-report        create a CSR shell (input_kind)
  2. GET  /api/study-reports/template             download the fill-in template
  3. POST /api/study-reports/{id}/dataset         upload results → analyse → lock
  4. GET  /api/study-reports/{id}                 fetch (results + status)
  5. GET  /api/protocols/{id}/study-reports       list a protocol's CSRs

Everything is org-scoped (get_protocol_scoped / get_study_report_scoped — a CSR
holds real aggregate clinical results, so cross-org access must be impossible),
RBAC-gated to the author role for writes, and audit-logged. The dataset is parsed
in memory, analysed deterministically, and the RAW rows are discarded — only a
SHA-256 checksum + a non-identifying summary are retained (results-only).
"""
from __future__ import annotations

import asyncio
import io
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import Text, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import require_user
from database import AsyncSessionLocal, get_db
from dependencies import (
    audit_logger,
    authenticate_actor as _authenticate_actor,
    get_protocol_scoped,
    get_study_report_scoped,
)
from models import Drug, Protocol, StudyReport, StudyReportVersion, StudyType, UserRole
from routers.protocols import _generate_synchronously, _require_role
from schemas import StudyReportCreate, StudyReportRead, StudyReportSummary
from routers.export import _content_disposition, _strip_html
from services.csr_export import build_csr_docx, build_csr_pdf
from services.csr_pipeline import build_csr_context, generate_csr_narrative
from services.csr_qc import run_csr_qc
from services.csr_results import assemble_results
from services.dataset_ingest import (
    VALID_KINDS,
    ValidationError,
    build_template_csv,
    build_template_xlsx,
    parse_dataset,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["study-reports"], dependencies=[Depends(audit_logger)])


class StudyReportListItem(BaseModel):
    """One row of the org-wide study-report list (GET /api/study-reports): the
    report's own status plus the parent protocol's title and drug INN, so the
    UI can render and open a CSR without a second fetch. No result/section
    payloads."""
    id: uuid.UUID
    protocol_id: uuid.UUID
    protocol_title: str
    drug_inn: str
    study_type: StudyType
    input_kind: str
    version: int
    generation_status: str
    has_results: bool = False
    verdict: Optional[str] = None
    created_at: datetime
    updated_at: datetime


def _plain_title(raw: Optional[str], *, limit: int = 300) -> str:
    """A protocol's title section (TipTap HTML) as one trimmed plain-text line."""
    if not raw:
        return ""
    return re.sub(r"\s+", " ", _strip_html(raw)).strip()[:limit]


def _design_of(protocol) -> tuple[str, bool]:
    """(design_key, is_nti) from the protocol's computed sample-size design."""
    ss = (protocol.population_config or {}).get("_sample_size") or {}
    design_key = ss.get("design_key") or "crossover"
    is_nti = bool(ss.get("is_nti"))
    return design_key, is_nti


async def _sr_snapshot(db: AsyncSession, sr: StudyReport, *, change_type: str, change_detail: dict | None = None) -> None:
    """Append an immutable StudyReportVersion snapshot (Part 11 trail).

    The version is (current max for this report) + 1, so re-analysis and
    re-generation each land a distinct, monotonically increasing version without
    colliding on the (study_report_id, version) uniqueness — robust to any call
    order. ``sr.version`` is advanced to match.
    """
    current_max = (
        await db.execute(
            select(func.max(StudyReportVersion.version)).where(
                StudyReportVersion.study_report_id == sr.id
            )
        )
    ).scalar() or 0
    sr.version = current_max + 1
    db.add(
        StudyReportVersion(
            id=uuid.uuid4(),
            study_report_id=sr.id,
            version=sr.version,
            sections=dict(sr.sections or {}),
            results=dict(sr.results or {}),
            changed_by=sr.created_by,
            change_type=change_type,
            change_detail=change_detail,
        )
    )


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


@router.get("/study-reports/template", dependencies=[Depends(require_user)])
async def download_template(kind: str, fmt: str = "csv"):
    """Download the fixed data template for a dataset kind ('nca'|'params'|'ae')."""
    if kind not in VALID_KINDS:
        raise HTTPException(422, detail=f"Unknown template kind '{kind}'. Expected one of: {', '.join(VALID_KINDS)}.")
    fmt = (fmt or "csv").lower()
    if fmt == "csv":
        data, media, ext = build_template_csv(kind), "text/csv", "csv"
    elif fmt in ("xlsx", "excel"):
        data = build_template_xlsx(kind)
        media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ext = "xlsx"
    else:
        raise HTTPException(422, detail=f"Unknown format '{fmt}'. Expected 'csv' or 'xlsx'.")
    filename = f"csr_{kind}_template.{ext}"
    return StreamingResponse(
        io.BytesIO(data),
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Create / read / list
# ---------------------------------------------------------------------------


@router.get(
    "/study-reports",
    response_model=list[StudyReportListItem],
    dependencies=[Depends(require_user)],
)
@router.get(
    "/study-reports/",
    response_model=list[StudyReportListItem],
    dependencies=[Depends(require_user)],
    include_in_schema=False,
)
async def list_all_study_reports(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """List every study report in the caller's organization, newest first,
    joined with each parent protocol's title and drug INN.

    Org-scoped on the report's own (denormalised) org_id from the verified
    actor, never a client filter. Selects only scalar columns - the big
    results/sections JSONB blobs are never loaded; ``verdict``, ``has_results``
    and the protocol title are read straight out of the JSONB in SQL. Both
    spellings (with/without trailing slash) are registered so a cross-origin
    fetch is never answered with a redirect.
    """
    actor = await _authenticate_actor(request, db)
    result = await db.execute(
        select(
            StudyReport.id,
            StudyReport.protocol_id,
            StudyReport.input_kind,
            StudyReport.version,
            StudyReport.generation_status,
            StudyReport.created_at,
            StudyReport.updated_at,
            StudyReport.results["verdict"].astext.label("verdict"),
            (~cast(StudyReport.results, Text).in_(("{}", "null"))).label("has_results"),
            Protocol.study_type,
            Protocol.sections["title"].astext.label("title_raw"),
            Drug.inn.label("drug_inn"),
        )
        .join(Protocol, Protocol.id == StudyReport.protocol_id)
        .join(Drug, Drug.id == Protocol.drug_id)
        .where(StudyReport.org_id == actor.org_id)
        .order_by(StudyReport.created_at.desc())
    )
    return [
        StudyReportListItem(
            id=row.id,
            protocol_id=row.protocol_id,
            protocol_title=_plain_title(row.title_raw) or row.drug_inn.title(),
            drug_inn=row.drug_inn,
            study_type=row.study_type,
            input_kind=row.input_kind,
            version=row.version,
            generation_status=row.generation_status,
            has_results=bool(row.has_results),
            verdict=row.verdict,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
        for row in result.all()
    ]


@router.post(
    "/protocols/{protocol_id}/study-report",
    response_model=StudyReportRead,
    status_code=201,
    dependencies=[Depends(require_user)],
)
async def create_study_report(
    protocol_id: uuid.UUID,
    payload: StudyReportCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Create a CSR shell bound to a completed protocol. Author role only."""
    actor = await _authenticate_actor(request, db)
    _require_role(actor, {UserRole.generator}, action="create study reports")
    protocol = await get_protocol_scoped(db, protocol_id, actor)

    if protocol.generation_status != "complete" or not (protocol.sections or {}):
        raise HTTPException(
            409,
            detail="Generate the protocol before creating its study report.",
        )

    sr = StudyReport(
        id=uuid.uuid4(),
        protocol_id=protocol.id,
        org_id=actor.org_id,
        input_kind=payload.input_kind,
        dataset_summary={},
        results={},
        sections={},
        version=1,
        generation_status="complete",
        created_by=actor.id,
    )
    db.add(sr)
    await db.flush()
    request.state.audit_resource_id = str(sr.id)
    await db.commit()
    await db.refresh(sr)
    return sr


@router.get(
    "/study-reports/{study_report_id}",
    response_model=StudyReportRead,
    dependencies=[Depends(require_user)],
)
async def get_study_report(
    study_report_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    actor = await _authenticate_actor(request, db)
    return await get_study_report_scoped(db, study_report_id, actor)


@router.get(
    "/protocols/{protocol_id}/study-reports",
    response_model=list[StudyReportSummary],
    dependencies=[Depends(require_user)],
)
async def list_study_reports(
    protocol_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """List a protocol's CSRs, newest first (org-scoped via the parent)."""
    actor = await _authenticate_actor(request, db)
    await get_protocol_scoped(db, protocol_id, actor)
    result = await db.execute(
        select(StudyReport)
        .where(StudyReport.protocol_id == protocol_id, StudyReport.org_id == actor.org_id)
        .order_by(StudyReport.created_at.desc())
    )
    return [
        StudyReportSummary(
            id=sr.id,
            protocol_id=sr.protocol_id,
            input_kind=sr.input_kind,
            version=sr.version,
            generation_status=sr.generation_status,
            has_results=bool(sr.results),
            verdict=(sr.results or {}).get("verdict"),
            created_at=sr.created_at,
        )
        for sr in result.scalars().all()
    ]


# ---------------------------------------------------------------------------
# Dataset upload → deterministic analysis
# ---------------------------------------------------------------------------


@router.post(
    "/study-reports/{study_report_id}/dataset",
    response_model=StudyReportRead,
    dependencies=[Depends(require_user)],
)
async def upload_dataset(
    study_report_id: uuid.UUID,
    request: Request,
    file: UploadFile = File(...),
    lloq: Optional[float] = Form(None),
    db: AsyncSession = Depends(get_db),
):
    """Upload the results file, analyse it deterministically, and lock the numbers.

    The parser strictly validates the upload (422 with per-row errors on bad
    data); NCA runs for raw concentration-time input; the BE analysis produces
    the 90% CI verdict. Raw rows are discarded — only checksum + summary remain.
    """
    actor = await _authenticate_actor(request, db)
    _require_role(actor, {UserRole.generator}, action="upload study data")
    sr = await get_study_report_scoped(db, study_report_id, actor)
    protocol = await get_protocol_scoped(db, sr.protocol_id, actor)

    raw = await file.read()
    if not raw:
        raise HTTPException(422, detail="Uploaded file is empty.")

    try:
        parsed = parse_dataset(raw, file.filename or "upload.csv", sr.input_kind, lloq=lloq)
    except ValidationError as exc:
        # Structured, per-row validation feedback for the UI.
        raise HTTPException(422, detail={"message": "Dataset validation failed.", "errors": exc.errors})
    except ValueError as exc:  # unknown kind etc. — programming/route error
        raise HTTPException(400, detail=str(exc))

    design_key, is_nti = _design_of(protocol)
    results = assemble_results(parsed, design_key=design_key, is_nti=is_nti)

    sr.dataset_checksum = parsed.sha256
    sr.dataset_summary = parsed.summary
    sr.results = results
    await _sr_snapshot(
        db, sr, change_type="ANALYSIS",
        change_detail={
            "filename": file.filename,
            "sha256": parsed.sha256,
            "verdict": results.get("verdict"),
            "analysed_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    request.state.audit_resource_id = str(sr.id)
    await db.commit()
    await db.refresh(sr)
    return sr


# ---------------------------------------------------------------------------
# Narrative generation (async, numbers-locked) + QC
# ---------------------------------------------------------------------------

# Strong refs to in-flight CSR narrative tasks (asyncio holds only weak refs).
_CSR_TASKS: set[asyncio.Task] = set()


def _spawn_csr_narrative(**kwargs: Any) -> None:
    task = asyncio.create_task(_csr_narrative_task(**kwargs))
    _CSR_TASKS.add(task)
    task.add_done_callback(_CSR_TASKS.discard)


async def _mark_csr_failed(study_report_id: uuid.UUID, detail: str) -> None:
    try:
        async with AsyncSessionLocal() as session:
            sr = await session.get(StudyReport, study_report_id)
            if sr is not None:
                sr.generation_status = "failed"
                sr.generation_error = (detail or "Narrative generation failed.")[:2000]
                await session.commit()
    except Exception:  # noqa: BLE001
        logger.exception("could not mark study report %s failed", study_report_id)


async def _csr_narrative_task(
    *, study_report_id: uuid.UUID, protocol_id: uuid.UUID, drug_inn: str
) -> None:
    """Background worker: generate the ICH E3 narrative around the locked results.
    Own DB session; never raises (records 'failed' so the client's poll ends)."""
    try:
        async with AsyncSessionLocal() as session:
            sr = await session.get(StudyReport, study_report_id)
            protocol = await session.get(Protocol, protocol_id)
            if sr is None or protocol is None:
                return
            context = build_csr_context(protocol, drug_inn, sr.results or {})
            sr.sections = await generate_csr_narrative(context)
            sr.generation_status = "complete"
            sr.generation_error = None
            await _sr_snapshot(
                session, sr, change_type="AI_NARRATIVE",
                change_detail={"generated_sections": list(sr.sections.keys())},
            )
            await session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.exception("CSR narrative generation failed for %s", study_report_id)
        await _mark_csr_failed(study_report_id, f"Narrative generation failed: {exc}")


@router.post(
    "/study-reports/{study_report_id}/generate",
    response_model=StudyReportRead,
    dependencies=[Depends(require_user)],
)
async def generate_narrative(
    study_report_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Generate the ICH E3 narrative for a CSR whose data has been analysed.

    The narrative is prose ONLY — every number comes from the locked results.
    Runs in a background task in production (poll GET /study-reports/{id} until
    generation_status flips to complete/failed); synchronously under pytest.
    """
    actor = await _authenticate_actor(request, db)
    _require_role(actor, {UserRole.generator}, action="generate study report narrative")
    sr = await get_study_report_scoped(db, study_report_id, actor)
    if not sr.results:
        raise HTTPException(
            409, detail="Upload and analyse a dataset before generating the report narrative."
        )
    protocol = await get_protocol_scoped(db, sr.protocol_id, actor, load_drug=True)
    drug_inn = protocol.drug.inn

    # ---- Synchronous path (pytest): generate inline, return the finished CSR.
    if _generate_synchronously():
        context = build_csr_context(protocol, drug_inn, sr.results)
        sr.sections = await generate_csr_narrative(context)
        sr.generation_status = "complete"
        sr.generation_error = None
        await _sr_snapshot(
            db, sr, change_type="AI_NARRATIVE",
            change_detail={"generated_sections": list(sr.sections.keys())},
        )
        request.state.audit_resource_id = str(sr.id)
        await db.commit()
        await db.refresh(sr)
        return sr

    # ---- Async path (production): flip to 'generating', return now, fill later.
    sr.generation_status = "generating"
    sr.generation_error = None
    request.state.audit_resource_id = str(sr.id)
    await db.commit()
    await db.refresh(sr)
    _spawn_csr_narrative(study_report_id=sr.id, protocol_id=sr.protocol_id, drug_inn=drug_inn)
    return sr


@router.get("/study-reports/{study_report_id}/qc", dependencies=[Depends(require_user)])
async def study_report_qc(
    study_report_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Deterministic QC checklist for a CSR's locked results (no AI)."""
    actor = await _authenticate_actor(request, db)
    sr = await get_study_report_scoped(db, study_report_id, actor)
    protocol = await get_protocol_scoped(db, sr.protocol_id, actor)
    return run_csr_qc(sr.results or {}, protocol)


# ---------------------------------------------------------------------------
# Export (ICH E3 DOCX / PDF)
# ---------------------------------------------------------------------------

_DOCX_MEDIA = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _csr_meta(protocol: Protocol, sr: StudyReport, drug_inn: str) -> dict:
    return {
        "drug_inn": drug_inn,
        "protocol_code": f"CSR-{str(sr.id)[:8].upper()}",
        "sponsor": protocol.org_id,
        "study_type": getattr(protocol.study_type, "value", "BE"),
        "version": f"{sr.version}.0",
        "created_at": sr.created_at,
    }


async def _load_for_export(study_report_id: uuid.UUID, request: Request, db: AsyncSession):
    actor = await _authenticate_actor(request, db)
    sr = await get_study_report_scoped(db, study_report_id, actor)
    if not sr.results:
        raise HTTPException(409, detail="Analyse a dataset before exporting the study report.")
    protocol = await get_protocol_scoped(db, sr.protocol_id, actor, load_drug=True)
    request.state.audit_resource_id = str(sr.id)
    return sr, protocol, protocol.drug.inn


@router.get("/study-reports/{study_report_id}/export/docx", dependencies=[Depends(require_user)])
async def export_csr_docx(
    study_report_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Render the CSR to a regulation-style ICH E3 .docx (results tables verbatim)."""
    sr, protocol, drug_inn = await _load_for_export(study_report_id, request, db)
    data = build_csr_docx(results=sr.results, sections=sr.sections or {}, meta=_csr_meta(protocol, sr, drug_inn))
    filename = f"CSR_{drug_inn.replace(' ', '_')}_v{sr.version}.docx"
    return StreamingResponse(
        io.BytesIO(data), media_type=_DOCX_MEDIA,
        headers={"Content-Disposition": _content_disposition(filename)},
    )


@router.get("/study-reports/{study_report_id}/export/pdf", dependencies=[Depends(require_user)])
async def export_csr_pdf(
    study_report_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Render the CSR to a regulation-style ICH E3 PDF (results tables verbatim)."""
    sr, protocol, drug_inn = await _load_for_export(study_report_id, request, db)
    data = build_csr_pdf(results=sr.results, sections=sr.sections or {}, meta=_csr_meta(protocol, sr, drug_inn))
    filename = f"CSR_{drug_inn.replace(' ', '_')}_v{sr.version}.pdf"
    return StreamingResponse(
        io.BytesIO(data), media_type="application/pdf",
        headers={"Content-Disposition": _content_disposition(filename)},
    )
