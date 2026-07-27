"""Protocol router — generation pipeline, section edits, version history, diff, and e-signing."""

from __future__ import annotations

import asyncio
import difflib
import functools
import hashlib
import json
import logging
import statistics
import sys
import uuid
from datetime import datetime, timezone
from typing import Any

import anthropic
from anyio import CapacityLimiter
from anyio.to_thread import run_sync as _run_sync_in_thread
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from database import AsyncSessionLocal, get_db
from dependencies import (
    audit_logger,
    authenticate_actor as _authenticate_actor,
    get_protocol_scoped,
    rate_limit,
    require_active_license,
)
from models import (
    AuditLog,
    Drug,
    PKParameter,
    PKParameterRecord,
    Protocol,
    ProtocolStatus,
    ProtocolVersion,
    User,
    UserRole,
)
from schemas import (
    AuditLogRead,
    ESignRequest,
    ESignResponse,
    ProtocolCreate,
    DesignPreviewRequest,
    ProtocolRead,
    ProtocolSummary,
    ProtocolVersionRead,
    ProtocolVersionSummary,
    SectionDiff,
    SectionPatch,
    SectionSave,
    VersionDiffResult,
)
from services.ai_service import AIService
from services.design_decisions import (
    allowed_study_types,
    apply_decision_overrides,
    determine_design_decisions,
    decisions_to_dicts,
)
from auth import require_user
from services.biostatistics import recommend_sample_size
from services.guideline_coverage import compute_coverage
from services.guidelines import GUIDELINES, is_supported
from services.literature_service import LiteratureService
from services.protocol_qc import run_qc
from services.three_stage_pipeline import generate_protocol_via_pipeline
from security import verify_password

# The fixed, Part-11 "meaning" bound to a signature for each role — never
# client-supplied (schemas.ESignRequest carries no meaning field), so a signer
# cannot claim a meaning their role doesn't hold. Only 'approver' can reach
# sign_protocol today; the other entries are pre-registered for when that
# gate is opened to additional roles (see role-based workflow work).
_SIGNATURE_MEANING_BY_ROLE: dict[UserRole, str] = {
    UserRole.approver: "Approved",
    UserRole.reviewer: "Reviewed",
    UserRole.generator: "Authored",
}

router = APIRouter(
    prefix="/api/protocols",
    tags=["protocols"],
    dependencies=[Depends(audit_logger)],
)

# Matches the reference clinical-trial-protocol template's outline: "title" and
# "synopsis" are unnumbered front matter (cover page + pre-Section-1 synopsis);
# the rest are the template's 16 numbered sections (Schedule of Activities,
# the template's Section 8, is generated programmatically — see export.py —
# so it has no AI config here). Immunogenicity content lives INSIDE
# study_assessments/statistical_analysis_plan (each section's own prompt
# conditions on study_type), so there is no separate immunogenicity key.
SECTION_ORDER: list[str] = [
    "title",
    "synopsis",
    "introduction",
    "study_rationale",
    "objectives",
    "endpoints",
    "study_design",
    "population",
    "treatment",
    "study_assessments",
    "safety_monitoring",
    "data_handling",
    "study_monitoring",
    "statistical_analysis_plan",
    "ethics",
    "administrative",
    "references",
]

# Every section in SECTION_ORDER is generated for every study type — the
# per-section AI prompts internally condition on study_type (e.g. immunogenicity
# content only appears in study_assessments/statistical_analysis_plan when
# study_type == "biosimilar_pk").
def _sections_for_type(study_type: str) -> list[str]:
    return SECTION_ORDER


SECTION_DISPLAY_NAMES: dict[str, str] = {
    "title": "Title Page",
    "synopsis": "Synopsis",
    "introduction": "Introduction",
    "study_rationale": "Study Rationale & Risk/Benefit",
    "objectives": "Study Objectives",
    "endpoints": "Study Endpoints",
    "study_design": "Study Design",
    "population": "Population",
    "treatment": "Treatment",
    "study_assessments": "Study Assessments",
    "safety_monitoring": "Safety Monitoring",
    "data_handling": "Data Handling & Record Keeping",
    "study_monitoring": "Study Monitoring, Auditing & Inspecting",
    "statistical_analysis_plan": "Statistical Methods & Data Analysis",
    "ethics": "Ethical & Regulatory Standards",
    "administrative": "Administrative Procedures",
    "references": "References",
}

_OPTIONAL_CTX_KEYS = (
    "dose_strength",
    "formulation",
    "analytical_method",
    "lloq",
    "acceptance_criteria",
    "cv_intra",
    "irb_info",
    "org_name",
)

logger = logging.getLogger("protocols")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _is_fatal_api_error(exc: BaseException) -> bool:
    """True for AI-service errors that retrying cannot fix — credit exhaustion,
    invalid/missing key, or revoked permissions. These mean the API itself is
    unusable, so EVERY section will fail the same way."""
    if isinstance(exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
        return True
    if isinstance(exc, anthropic.BadRequestError):
        return "credit balance" in str(exc).lower()
    return False


def _human_api_error(exc: BaseException | None) -> tuple[int, str]:
    """Map an AI-service exception to an (HTTP status, user-facing detail)."""
    msg = str(exc or "")
    if "credit balance" in msg.lower():
        return 402, (
            "Generation failed: the Anthropic API credit balance is too low. "
            "Add credits at console.anthropic.com (Plans & Billing), then try "
            "again. Note: a Claude Pro subscription does NOT include API credits."
        )
    if isinstance(exc, anthropic.AuthenticationError):
        return 502, "Generation failed: the Anthropic API key is invalid or missing."
    return 502, "Generation failed due to an AI service error. Please try again."


def _is_fatal_pipeline_error(err: str | None) -> bool:
    """String counterpart to _is_fatal_api_error — the three-stage pipeline
    catches exceptions internally and reports them as log-entry error strings,
    not exception objects, so this checks for the same failure signatures by
    substring instead of isinstance."""
    if not err:
        return False
    low = err.lower()
    return "credit balance" in low or "authentication" in low or "permission" in low


def _human_pipeline_error(err: str) -> tuple[int, str]:
    """String counterpart to _human_api_error."""
    if "credit balance" in err.lower():
        return 402, (
            "Generation failed: the Anthropic API credit balance is too low. "
            "Add credits at console.anthropic.com (Plans & Billing), then try "
            "again. Note: a Claude Pro subscription does NOT include API credits."
        )
    return 502, "Generation failed: the Anthropic API key is invalid or missing."


def _median_t_half(pk_params: list[PKParameterRecord]) -> float | None:
    """Median terminal half-life (hours) from extracted PK records, if any."""
    vals = [
        p.value
        for p in pk_params
        if p.parameter == PKParameter.t_half and p.value and p.value > 0
    ]
    return statistics.median(vals) if vals else None


def _truncate(value: str | None, max_len: int) -> str | None:
    """Defensively cap an AI-extracted string to a DB column's max length."""
    if value is None:
        return None
    return value if len(value) <= max_len else value[: max_len - 1] + "…"


_CV_METRIC_LABEL = {
    "cv_intra_cmax": "Cmax",
    "cv_intra_auc": "AUC",
    "cv_intra": "Cmax or AUC (unspecified)",
    "cv_within": "Cmax or AUC (unspecified)",
    "cv_intrasubject": "Cmax or AUC (unspecified)",
}


def _select_cv_for_powering(
    extracted: list[dict[str, Any]],
) -> tuple[float | None, list[dict[str, Any]]]:
    """Pick the CV% used to power the study and build the supporting trials table.

    Per regulatory practice for PK-similarity/BE studies, the study is powered on
    the HIGHEST within-subject CV observed for either Cmax or AUC across published
    single-dose studies in the same (healthy-volunteer) population — a conservative
    choice. Returns ``(highest_cv_pct, cv_table)`` where ``cv_table`` is one row per
    extracted CV value (source, population, metric, cv%) for rendering in the SAP.
    """
    rows: list[dict[str, Any]] = []
    for p in extracted:
        param = p.get("parameter")
        if param in _CV_METRIC_LABEL and p.get("value"):
            try:
                cv = float(p["value"])
            except (TypeError, ValueError):
                continue
            if cv <= 0:
                continue
            rows.append({
                "source": p.get("source_pmid", "—"),
                "population": p.get("population") or "healthy volunteers",
                "metric": _CV_METRIC_LABEL[param],
                "cv_pct": round(cv, 1),
            })
    if not rows:
        return None, []
    highest = max(r["cv_pct"] for r in rows)
    # Sort the table highest-CV first so the adopted value sits at the top.
    rows.sort(key=lambda r: r["cv_pct"], reverse=True)
    return highest, rows


async def _fetch_label_safe(
    lit_svc: LiteratureService, drug_inn: str
) -> dict[str, str] | None:
    """Fetch and trim the DailyMed SPL label; never raises into the pipeline."""
    try:
        rec = await lit_svc.fetch_dailymed(drug_inn)
    except Exception:
        return None
    if not rec:
        return None
    secs = rec.get("sections", {}) or {}

    def _trim(s: str | None) -> str:
        return (s or "").strip()[:1500]

    label = {
        "title": rec.get("title", ""),
        "indications_usage": _trim(secs.get("indications_usage")),
        "pharmacokinetics": _trim(secs.get("pharmacokinetics")),
        "clinical_pharmacology": _trim(secs.get("clinical_pharmacology")),
        # Route/device/injection-site source text for design_decisions.py —
        # not surfaced to the AI generation prompt (only PK/pharmacology are),
        # just used to ground the Review Design route decision in the real label.
        "dosage_and_administration": _trim(secs.get("dosage_and_administration")),
        "how_supplied": _trim(secs.get("how_supplied")),
    }
    # Drop empty fields so the prompt context stays clean.
    return {k: v for k, v in label.items() if v}


# Dedicated thread-pool capacity for _augment_config_with_design's blocking
# call (below), separate from Starlette's shared default limiter that
# auth.py's per-request JWT verification also runs on via run_in_threadpool.
# This call can occupy a thread for seconds (the underlying R subprocess in
# services/powertost_r.py carries a 60s timeout), so without a dedicated cap a
# burst of concurrent generations could exhaust the shared pool and start
# queuing unrelated, otherwise-instant auth checks behind them.
_DESIGN_THREAD_LIMITER = CapacityLimiter(4)


def _augment_config_with_design(
    population_config: dict[str, Any],
    study_type: str,
    drug_inn: str,
    extracted: list[dict[str, Any]],
    pk_params: list[PKParameterRecord],
    label: dict[str, str] | None,
) -> dict[str, Any]:
    """Compute the planned sample size ONCE and bake it into the stored config.

    This makes the design the single source of truth: the same numbers feed the
    synopsis, study-design and SAP prompts and the export tables, and they stay
    stable across section regenerations.
    """
    cfg = dict(population_config)

    cv_override = cfg.get("cv_intra")
    cv_high, cv_table = _select_cv_for_powering(extracted)
    if cv_override:
        cv_pct, cv_source = float(cv_override), "override"
    elif cv_high:
        # Power on the HIGHEST observed within-subject CV (Cmax or AUC) across the
        # healthy-volunteer literature — the conservative regulatory convention.
        cv_pct, cv_source = cv_high, "literature (highest observed)"
    else:
        cv_pct, cv_source = None, None

    t_half = _median_t_half(pk_params)
    rec = recommend_sample_size(
        study_type=study_type,
        drug_inn=drug_inn,
        cv_intra_pct=cv_pct,
        cv_source=cv_source,
        t_half_hours=t_half,
        is_nti=cfg.get("is_nti"),
        requested_design=cfg.get("design"),
        # Use the real R PowerTOST package as the authoritative, citable
        # sample-size source for the regulatory record when R is installed
        # (falls back to the validated Python engine otherwise).
        use_r=True,
    )
    ss = rec.to_dict()
    # Carry the median half-life alongside the recommendation so the QC
    # validator can check washout adequacy without re-querying PK records.
    ss["t_half_hours"] = t_half
    # The CV trials table backs the Sample Size Determination narrative in the SAP
    # (the prompt renders it when present); empty when no literature CV was found.
    ss["cv_table"] = cv_table
    cfg["_sample_size"] = ss
    if label:
        cfg["_label"] = label
    return cfg


def _build_context(
    drug: Drug,
    study_type: str,
    population_config: dict[str, Any],
    org_id: str,
    guideline_version: str,
    pk_params: list[PKParameterRecord],
) -> dict[str, Any]:
    pk_list = [
        {
            "parameter": p.parameter.value,
            "value": p.value,
            "unit": p.unit,
            "population": p.population,
            "source_pmid": p.source_pmid,
            "confidence": p.confidence,
        }
        for p in pk_params
    ]
    ctx: dict[str, Any] = {
        "drug_inn": drug.inn,
        "brand_names": drug.brand_names,
        "study_type": study_type,
        "population_config": {
            k: v for k, v in population_config.items() if not k.startswith("_")
        },
        "pk_parameters": pk_list,
        "org_name": population_config.get("org_name", org_id),
        "guideline_version": guideline_version,
    }
    for key in _OPTIONAL_CTX_KEYS:
        val = population_config.get(key)
        if val is not None:
            ctx[key] = val

    # Single-source-of-truth design recommendation (computed at creation).
    rec = population_config.get("_sample_size")
    if rec:
        ctx["sample_size"] = rec
    label = population_config.get("_label")
    if label:
        ctx["label_data"] = label
    return ctx


def _snapshot(
    db: AsyncSession,
    *,
    protocol_id: uuid.UUID,
    version: int,
    sections: dict[str, Any],
    changed_by: uuid.UUID | None,
    change_type: str,
    change_detail: dict[str, Any] | None = None,
) -> None:
    """Add a ProtocolVersion snapshot to the current session (no flush/commit)."""
    db.add(
        ProtocolVersion(
            id=uuid.uuid4(),
            protocol_id=protocol_id,
            version=version,
            sections=dict(sections),
            changed_by=changed_by,
            change_type=change_type,
            change_detail=change_detail,
        )
    )


def _compute_sections_hash(sections: dict[str, Any]) -> str:
    """SHA-256 of a canonical, deterministic JSON encoding of the sections dict.

    Sections are serialised in SECTION_ORDER with missing sections as empty string.
    Sorted keys and compact separators ensure the hash is stable across Python
    dict orderings and JSON serialiser versions.
    """
    canonical = json.dumps(
        {k: sections.get(k, "") for k in SECTION_ORDER},
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _strip_html_simple(html: str) -> str:
    """Strip HTML tags and decode basic entities → plain text for diffing."""
    import re
    text = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return (
        text.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&nbsp;", " ")
        .replace("&#39;", "'")
        .strip()
    )


def _diff_sections(
    old_sections: dict[str, Any],
    new_sections: dict[str, Any],
    from_version: int,
    to_version: int,
) -> list[SectionDiff]:
    result: list[SectionDiff] = []
    for name in SECTION_ORDER:
        old_html = old_sections.get(name, "")
        new_html = new_sections.get(name, "")
        old_text = _strip_html_simple(old_html) if old_html else ""
        new_text = _strip_html_simple(new_html) if new_html else ""

        if not old_text and not new_text:
            status = "unchanged"
        elif not old_text:
            status = "added"
        elif not new_text:
            status = "removed"
        elif old_text == new_text:
            status = "unchanged"
        else:
            status = "modified"

        diff_lines: list[str] = []
        if status == "modified":
            diff_lines = list(
                difflib.unified_diff(
                    old_text.splitlines(keepends=True),
                    new_text.splitlines(keepends=True),
                    fromfile=f"v{from_version}/{name}",
                    tofile=f"v{to_version}/{name}",
                    lineterm="",
                )
            )

        old_words = len(old_text.split()) if old_text else 0
        new_words = len(new_text.split()) if new_text else 0

        result.append(
            SectionDiff(
                section_name=name,
                display_name=SECTION_DISPLAY_NAMES.get(name, name),
                status=status,
                old_content=old_html if status != "unchanged" else None,
                new_content=new_html if status != "unchanged" else None,
                diff_lines=diff_lines,
                word_count_delta=new_words - old_words,
            )
        )
    return result


def _require_role(actor: User, allowed: set[UserRole], *, action: str) -> None:
    """Raise 403 unless actor.role is in allowed. Author(generator)/Reviewer/
    Approver map to a real editorial workflow: only Authors draft and edit
    content; Reviewers and Approvers have read access but not write access,
    and only Approvers may sign (enforced separately in sign_protocol) —
    standard separation of duties (the signer should not also be the drafter)."""
    if actor.role not in allowed:
        raise HTTPException(
            403,
            detail=(
                f"Role '{actor.role.value}' is not permitted to {action}. "
                f"Allowed roles: {', '.join(sorted(r.value for r in allowed))}."
            ),
        )


# ---------------------------------------------------------------------------
# Routes — protocol CRUD
# ---------------------------------------------------------------------------


@router.get("/", response_model=list[ProtocolSummary], dependencies=[Depends(require_user)])
async def list_protocols(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """List the caller's organization's protocols (newest first) for the
    'My Reports' history view. Org-scoped via the authenticated actor — a user
    only ever sees their own organization's past generations (see B3). Returns
    lightweight summaries (no section payloads) joined with each drug's INN so
    the UI can render the list and re-open a protocol without extra fetches.
    """
    actor = await _authenticate_actor(request, db)
    result = await db.execute(
        select(Protocol)
        .options(selectinload(Protocol.drug))
        .where(Protocol.org_id == actor.org_id)
        .order_by(Protocol.created_at.desc())
    )
    protocols = result.scalars().all()
    return [
        ProtocolSummary(
            id=p.id,
            drug_id=p.drug_id,
            drug_inn=p.drug.inn if p.drug else "(unknown)",
            drug_brand_names=(p.drug.brand_names if p.drug else []),
            study_type=p.study_type,
            guideline_version=p.guideline_version,
            guideline_outdated=p.guideline_outdated,
            version=p.version,
            status=p.status,
            section_count=sum(1 for v in (p.sections or {}).values() if v),
            created_at=p.created_at,
        )
        for p in protocols
    ]


@router.post(
    "/design-preview",
    dependencies=[Depends(rate_limit(30, 60, scope="design_preview"))],
)
async def design_preview(
    payload: DesignPreviewRequest,
    db: AsyncSession = Depends(get_db),
):
    """Credit-free preview of the five study-design decisions for the 'Review
    design' wizard step. Uses the deterministic sample-size engine (for the
    parallel/crossover call) + stored PK half-life + the drug's real DailyMed
    label (route/device/injection-site — free NIH API, no AI cost) with the
    curated clinical knowledge base as fallback when the label can't be
    fetched or doesn't state something explicitly. Open (like drug search): it
    returns clinical suggestions for a public drug, no org data.
    """
    drug = await db.get(Drug, payload.drug_id)
    if not drug:
        raise HTTPException(404, detail="Drug not found.")

    pk_result = await db.execute(
        select(PKParameterRecord).where(PKParameterRecord.drug_id == drug.id)
    )
    t_half = _median_t_half(pk_result.scalars().all())

    # Real label text grounds the route/device/site call — free NIH API, no AI
    # cost, and cached 7 days by LiteratureService so repeat previews are instant.
    label = await _fetch_label_safe(LiteratureService(db), drug.inn)
    label_text = " ".join(filter(None, [
        (label or {}).get("dosage_and_administration", ""),
        (label or {}).get("how_supplied", ""),
    ]))

    # design_key (parallel/crossover/replicate) is decided deterministically from
    # half-life + study type; use_r=False keeps it a pure-Python, R-independent call.
    rec = recommend_sample_size(
        study_type=payload.study_type.value,
        drug_inn=drug.inn,
        t_half_hours=t_half,
        is_nti=payload.population_config.get("is_nti"),
        requested_design=payload.population_config.get("design"),
        use_r=False,
    )
    decisions = determine_design_decisions(
        drug_inn=drug.inn,
        study_type=payload.study_type.value,
        design_key=rec.design_key,
        t_half_hours=t_half,
        label_text=label_text,
    )
    return {
        "drug_inn": drug.inn,
        "study_type": payload.study_type.value,
        "decisions": decisions_to_dicts(decisions),
    }


# ---------------------------------------------------------------------------
# Protocol generation — shared pipeline + sync/async orchestration
# ---------------------------------------------------------------------------
#
# Real AI generation runs the 3-model pipeline for MINUTES. Holding one HTTP
# request open that long fails behind Railway's edge proxy: it drops the idle
# connection and the browser reports "Failed to fetch". So in PRODUCTION,
# create_protocol persists a "generating" protocol row and returns it
# immediately; the pipeline then runs in a background task that fills the row
# in, and the client polls GET /protocols/{id} until generation_status flips to
# "complete" (open the editor) or "failed" (show the reason).
#
# Under PYTEST the suite mocks the AI and asserts on the fully-generated
# response, so generation stays synchronous there — identical to the original
# inline behaviour, including raising HTTPException on a fatal/empty outcome.
# Same "pytest in sys.modules" gate the rate limiter and the boot migration use.


def _generate_synchronously() -> bool:
    """True under pytest (generate inline, mocked, what the suite asserts on);
    False in real deployments (generate in a background task so the HTTP
    response returns immediately)."""
    return "pytest" in sys.modules


async def _run_generation_pipeline(
    session: AsyncSession,
    *,
    drug: Drug,
    org_id: str,
    study_type: str,
    guideline_version: str,
    population_config: dict[str, Any],
    design_decisions: list[dict[str, Any]],
) -> tuple[dict[str, str], dict[str, Any], dict[str, Any]]:
    """The full literature -> PK -> design -> 3-stage-pipeline flow, shared by
    the sync request path and the async background task.

    Returns (sections, augmented_config, snapshot_change_detail). Raises
    HTTPException on a fatal API error or a completely empty result — exactly as
    the original inline create_protocol did; each caller decides how to surface
    that (a sync request re-raises it; the background task records it as a
    'failed' generation_status).
    """
    lit_svc = LiteratureService(session)
    pubmed_records = await lit_svc.fetch_pubmed(drug.inn)
    label = await _fetch_label_safe(lit_svc, drug.inn)

    ai_svc = AIService()
    # Mine the FDA label's PK text too (reliably reports t_half/Tmax/Cmax that
    # PubMed abstracts often omit), tagged with source "DAILYMED".
    label_pk_text = None
    if label:
        _pk_parts = [label.get("pharmacokinetics", ""), label.get("clinical_pharmacology", "")]
        label_pk_text = "\n\n".join(p for p in _pk_parts if p) or None

    # PK extraction is the first AI call. A fatal error here (out of credits,
    # bad key) means every section call will fail the same way, so fail fast with
    # the clean reason rather than a raw 500. A transient/other error degrades
    # gracefully — sections can still generate without extracted PK params.
    try:
        extracted = await ai_svc.extract_pk_parameters(
            literature=[
                {"pmid": r["pmid"], "title": r.get("title", ""), "abstract": r.get("abstract", "")}
                for r in pubmed_records
            ],
            drug_inn=drug.inn,
            label_text=label_pk_text,
        )
    except Exception as exc:  # noqa: BLE001
        if _is_fatal_api_error(exc):
            logger.error("PK extraction failed (fatal, aborting generation): %r", exc)
            status_code, detail = _human_api_error(exc)
            raise HTTPException(status_code=status_code, detail=detail) from exc
        logger.warning("PK extraction failed (non-fatal), continuing without PK: %r", exc)
        extracted = []

    for param in extracted:
        try:
            param_enum = PKParameter(param["parameter"])
        except ValueError:
            continue
        session.add(
            PKParameterRecord(
                id=uuid.uuid4(),
                drug_id=drug.id,
                parameter=param_enum,
                value=float(param["value"]),
                # AI-extracted strings can exceed the DB column limits (e.g. a
                # DDI-study unit like "ratio (GMR, Warfarin+PB-119 vs Warfarin
                # alone, within 80-125% BE range)" overflowed VARCHAR(64) and
                # crashed generation) — truncate defensively regardless of how
                # well the prompt behaves, so a wordy edge case never crashes.
                unit=_truncate(param.get("unit", ""), 64),
                population=_truncate(param.get("population"), 512),
                source_pmid=_truncate(param["source_pmid"], 64),
                confidence=param.get("confidence"),
            )
        )
    await session.flush()

    pk_result = await session.execute(
        select(PKParameterRecord).where(PKParameterRecord.drug_id == drug.id)
    )
    all_pk = pk_result.scalars().all()

    # Fold the reviewer's design/population overrides (from the "Review design"
    # step) into population_config BEFORE sizing, so the sample-size design and
    # the population section stay consistent with what was chosen.
    pop_cfg = dict(population_config)
    if design_decisions:
        apply_decision_overrides(pop_cfg, design_decisions)

    # Compute the planned sample size / design ONCE and bake it into the config
    # so every section and the export read the same numbers. Offloaded to a
    # dedicated thread-pool limiter — _augment_config_with_design may shell out
    # to Rscript (a blocking call) which must never stall the event loop; see
    # _DESIGN_THREAD_LIMITER above.
    augmented_config = await _run_sync_in_thread(
        functools.partial(
            _augment_config_with_design,
            population_config=pop_cfg,
            study_type=study_type,
            drug_inn=drug.inn,
            extracted=extracted,
            pk_params=all_pk,
            label=label,
        ),
        limiter=_DESIGN_THREAD_LIMITER,
    )

    # Finalize the five design decisions: use the reviewer's if provided, else
    # derive them fresh from the computed design so generation ALWAYS reflects
    # the 5 clinical rules (population, dose, route, design, PD co-primary).
    _ss = augmented_config.get("_sample_size") or {}
    _label_text = " ".join(filter(None, [
        (label or {}).get("dosage_and_administration", ""),
        (label or {}).get("how_supplied", ""),
    ]))
    decisions = design_decisions or decisions_to_dicts(
        determine_design_decisions(
            drug_inn=drug.inn,
            study_type=study_type,
            design_key=_ss.get("design_key"),
            t_half_hours=_ss.get("t_half_hours"),
            label_text=_label_text,
        )
    )
    augmented_config["_design_decisions"] = decisions

    context = _build_context(
        drug=drug,
        study_type=study_type,
        population_config=augmented_config,
        org_id=org_id,
        guideline_version=guideline_version,
        pk_params=all_pk,
    )
    # Locked directive read by both generation prompt paths (see design_decisions.py).
    context["design_decisions"] = decisions

    sections_to_generate = _sections_for_type(study_type)
    sections, classification, validation, pipeline_log, qc = await generate_protocol_via_pipeline(
        context=context,
        study_type=study_type,
        section_order=sections_to_generate,
    )

    # The pipeline catches exceptions internally and records them as error
    # strings on log entries; failure classification mirrors _is_fatal_api_error
    # but operates on those strings.
    failed = [name for name in sections_to_generate if name not in sections]
    first_fatal_err: str | None = None
    for entry in pipeline_log:
        err = entry.get("error")
        if err and first_fatal_err is None and _is_fatal_pipeline_error(err):
            first_fatal_err = err
    halt_reason = (classification or {}).get("halt_reason")

    if failed:
        logger.warning(
            "Protocol generation for '%s' incomplete: %d of %d sections failed (%s).",
            drug.inn, len(failed), len(sections_to_generate), ", ".join(failed),
        )

    # If nothing generated at all, either the API is unusable (out of credits or
    # a bad key) or Stage 1 halted on a low-confidence classification. Don't
    # persist an empty husk — surface the real reason.
    if not sections:
        if first_fatal_err:
            status_code, detail = _human_pipeline_error(first_fatal_err)
        elif halt_reason:
            status_code, detail = 422, f"Generation halted: {halt_reason}"
        else:
            status_code, detail = 502, "Generation failed due to an AI service error. Please try again."
        raise HTTPException(status_code=status_code, detail=detail)

    change_detail = {
        "generated_sections": list(sections.keys()),
        "ai_classification": classification,
        "ai_validation": validation,
        "ai_pipeline_log": pipeline_log,
        "ai_qc_remediation": qc,
    }
    return sections, augmented_config, change_detail


# Strong refs to in-flight background generation tasks — without this the event
# loop can garbage-collect a still-running task (asyncio only holds weak refs).
_GENERATION_TASKS: set[asyncio.Task] = set()


def _spawn_generation(**kwargs: Any) -> None:
    task = asyncio.create_task(_generation_task(**kwargs))
    _GENERATION_TASKS.add(task)
    task.add_done_callback(_GENERATION_TASKS.discard)


async def _mark_generation_failed(protocol_id: uuid.UUID, detail: str) -> None:
    """Flip a 'generating' row to 'failed' with a reason so the polling client
    stops waiting and shows the error. Best-effort in its own session."""
    try:
        async with AsyncSessionLocal() as session:
            protocol = await session.get(Protocol, protocol_id)
            if protocol is not None:
                protocol.generation_status = "failed"
                protocol.generation_error = (detail or "Generation failed.")[:2000]
                await session.commit()
    except Exception:  # noqa: BLE001 — nothing left to do if even this fails
        logger.exception("could not mark protocol %s failed", protocol_id)


async def _generation_task(
    *,
    protocol_id: uuid.UUID,
    org_id: str,
    actor_id: uuid.UUID,
    drug_id: uuid.UUID,
    study_type: str,
    guideline_version: str,
    population_config: dict[str, Any],
    design_decisions: list[dict[str, Any]],
) -> None:
    """Background worker: run the pipeline against a NEW DB session (the
    request's session is long closed) and write the result onto the existing
    'generating' row. Never raises — a fatal/empty outcome or any unexpected
    error is recorded as generation_status='failed' so the client's poll ends."""
    try:
        async with AsyncSessionLocal() as session:
            drug = await session.get(Drug, drug_id)
            if drug is None:  # deleted between request and task — extremely unlikely
                raise HTTPException(404, detail="Drug not found.")
            sections, augmented_config, change_detail = await _run_generation_pipeline(
                session,
                drug=drug,
                org_id=org_id,
                study_type=study_type,
                guideline_version=guideline_version,
                population_config=population_config,
                design_decisions=design_decisions,
            )
            protocol = await session.get(Protocol, protocol_id)
            if protocol is None:
                return  # row deleted mid-flight; nothing to update
            protocol.sections = sections
            protocol.population_config = augmented_config
            protocol.generation_status = "complete"
            protocol.generation_error = None
            _snapshot(
                session,
                protocol_id=protocol_id,
                version=1,
                sections=sections,
                changed_by=actor_id,
                change_type="INITIAL",
                change_detail=change_detail,
            )
            await session.commit()
    except HTTPException as exc:
        # A fatal/empty generation — surface the same human message the sync
        # path would have raised, via the row's failed status.
        await _mark_generation_failed(protocol_id, str(exc.detail))
    except Exception:  # noqa: BLE001
        logger.exception("background generation failed for protocol %s", protocol_id)
        await _mark_generation_failed(
            protocol_id, "Generation failed due to an unexpected error. Please try again."
        )


@router.post("/", response_model=ProtocolRead, status_code=201, dependencies=[Depends(require_user)])
async def create_protocol(
    payload: ProtocolCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Start a protocol generation: literature -> PK -> design -> sections.

    Only the 'generator' (Author) role may draft a new protocol (separation of
    duties: the eventual approver should not also be the drafter), and the org's
    license must be active.

    Real generation runs for minutes, so in production it runs in a BACKGROUND
    task: this returns a ``generation_status="generating"`` protocol immediately
    and the client polls GET /protocols/{id} until it flips to "complete" (open
    the editor) or "failed" (show generation_error). Under pytest it runs
    synchronously and returns the finished protocol, matching the mocked suite.
    """
    actor = await _authenticate_actor(request, db)
    _require_role(actor, {UserRole.generator}, action="create protocols")
    await require_active_license(db, actor)

    if not is_supported(payload.guideline_version):
        raise HTTPException(
            422,
            detail=(
                f"Unsupported guideline '{payload.guideline_version}'. "
                f"Supported: {', '.join(GUIDELINES)}."
            ),
        )

    drug = await db.get(Drug, payload.drug_id)
    if not drug:
        raise HTTPException(404, detail="Drug not found.")

    permitted = allowed_study_types(drug.inn)
    if permitted is not None and payload.study_type.value not in permitted:
        raise HTTPException(
            422,
            detail=(
                f"'{drug.inn}' is a biologic and cannot run a '{payload.study_type.value}' study; "
                f"allowed study type(s): {', '.join(permitted)}."
                if permitted == ("biosimilar_pk",) else
                f"'{drug.inn}' is a small-molecule product and cannot run a "
                f"'{payload.study_type.value}' study; allowed study type(s): {', '.join(permitted)}."
            ),
        )

    # ---- Synchronous path (pytest): generate inline, return the finished row.
    if _generate_synchronously():
        sections, augmented_config, change_detail = await _run_generation_pipeline(
            db,
            drug=drug,
            org_id=actor.org_id,
            study_type=payload.study_type.value,
            guideline_version=payload.guideline_version,
            population_config=payload.population_config,
            design_decisions=payload.design_decisions,
        )
        protocol = Protocol(
            id=uuid.uuid4(),
            org_id=actor.org_id,
            drug_id=drug.id,
            study_type=payload.study_type,
            population_config=augmented_config,
            sections=sections,
            guideline_version=payload.guideline_version,
            created_by=actor.id,
            generation_status="complete",
        )
        db.add(protocol)
        await db.flush()
        # Creation has no id in the URL for audit_logger's path parser to find —
        # hand it the real id now that one exists (see dependencies.py::audit_logger).
        request.state.audit_resource_id = str(protocol.id)
        _snapshot(
            db,
            protocol_id=protocol.id,
            version=1,
            sections=sections,
            changed_by=actor.id,
            change_type="INITIAL",
            change_detail=change_detail,
        )
        await db.commit()
        await db.refresh(protocol)
        return protocol

    # ---- Async path (production): persist a 'generating' row, return it now,
    #      and let a background task fill it in. No long-held HTTP connection, so
    #      nothing for the edge proxy to time out.
    protocol = Protocol(
        id=uuid.uuid4(),
        org_id=actor.org_id,
        drug_id=drug.id,
        study_type=payload.study_type,
        population_config=dict(payload.population_config),
        sections={},
        guideline_version=payload.guideline_version,
        created_by=actor.id,
        generation_status="generating",
    )
    db.add(protocol)
    await db.flush()
    request.state.audit_resource_id = str(protocol.id)
    await db.commit()
    await db.refresh(protocol)

    _spawn_generation(
        protocol_id=protocol.id,
        org_id=actor.org_id,
        actor_id=actor.id,
        drug_id=drug.id,
        study_type=payload.study_type.value,
        guideline_version=payload.guideline_version,
        population_config=payload.population_config,
        design_decisions=payload.design_decisions,
    )
    return protocol


@router.post(
    "/{protocol_id}/clone",
    response_model=ProtocolRead,
    status_code=201,
    dependencies=[Depends(require_user)],
)
async def clone_protocol(
    protocol_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Duplicate an existing protocol as a fresh draft — "start from template"
    (F3), for fast reuse of a prior study design.

    The clone copies the source's drug, study type, guideline, population/design
    config and its ALREADY-GENERATED section content VERBATIM. The AI pipeline is
    deliberately NOT re-run — instant, credit-free reuse is the whole point — so
    this is a pure DB copy, not a regeneration. The new row is a clean draft owned
    by the acting actor: fresh uuid, version reset to 1, status back to `draft`,
    and created_by/org set to the actor (never the source's), exactly as a
    brand-new protocol would be.

    Author-only, same separation of duties as create_protocol (only the
    'generator'/Author role drafts protocols), and gated on an active license
    the same way — cloning still produces a new billable protocol, so it must
    not be a way around the license gate. Org isolation is enforced by
    get_protocol_scoped: a source id from another org yields 403, never a copy.
    """
    actor = await _authenticate_actor(request, db)
    _require_role(actor, {UserRole.generator}, action="clone protocols")
    await require_active_license(db, actor)

    # Org-scoped fetch: 404 if missing, 403 if it belongs to another org — a user
    # can only clone a protocol they are already allowed to see.
    source = await get_protocol_scoped(db, protocol_id, actor)

    # Copy content fields verbatim (top-level dict copies so the clone and source
    # never share the same mutable JSONB object), while resetting every identity/
    # lifecycle field to that of a new draft owned by the acting actor.
    clone = Protocol(
        id=uuid.uuid4(),
        org_id=actor.org_id,
        drug_id=source.drug_id,
        study_type=source.study_type,
        population_config=dict(source.population_config or {}),
        sections=dict(source.sections or {}),
        version=1,
        status=ProtocolStatus.draft,
        guideline_version=source.guideline_version,
        created_by=actor.id,
    )
    db.add(clone)
    # Flush to obtain clone.id before it's referenced in the snapshot / audit id.
    await db.flush()
    # The POST URL carries the SOURCE id, so audit_logger's path parser would
    # attribute the creation to the source — point it at the real new id instead
    # (mirrors create_protocol, whose URL has no id at all).
    request.state.audit_resource_id = str(clone.id)

    # Version 1 snapshot — the clone's initial state, tagged with its provenance
    # so the audit trail records exactly which protocol it was duplicated from.
    _snapshot(
        db,
        protocol_id=clone.id,
        version=1,
        sections=clone.sections,
        changed_by=actor.id,
        change_type="INITIAL",
        change_detail={
            "cloned_from_protocol_id": str(source.id),
            "cloned_from_version": source.version,
            "copied_sections": list(clone.sections.keys()),
        },
    )

    await db.commit()
    await db.refresh(clone)
    return clone


@router.get("/{protocol_id}", response_model=ProtocolRead, dependencies=[Depends(require_user)])
async def get_protocol(
    protocol_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    actor = await _authenticate_actor(request, db)
    return await get_protocol_scoped(db, protocol_id, actor)


@router.get("/{protocol_id}/qc", dependencies=[Depends(require_user)])
async def protocol_qc(
    protocol_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Deterministic QC report — internal-consistency and scientific-soundness
    checks computed without any AI call. Safe to call repeatedly."""
    actor = await _authenticate_actor(request, db)
    protocol = await get_protocol_scoped(db, protocol_id, actor)

    report = run_qc(
        study_type=protocol.study_type.value,
        population_config=protocol.population_config or {},
        sections=protocol.sections or {},
        expected_sections=_sections_for_type(protocol.study_type.value),
        section_labels=SECTION_DISPLAY_NAMES,
    )
    return report.to_dict()


@router.get("/{protocol_id}/coverage", dependencies=[Depends(require_user)])
async def protocol_coverage(
    protocol_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Regulatory guideline coverage matrix — which required elements are present,
    partial, or missing. Deterministic, no AI."""
    actor = await _authenticate_actor(request, db)
    protocol = await get_protocol_scoped(db, protocol_id, actor)

    report = compute_coverage(
        study_type=protocol.study_type.value,
        population_config=protocol.population_config or {},
        sections=protocol.sections or {},
        section_labels=SECTION_DISPLAY_NAMES,
    )
    return report.to_dict()


# ---------------------------------------------------------------------------
# Routes — section edits
# ---------------------------------------------------------------------------


@router.patch("/{protocol_id}/sections/{section_name}", dependencies=[Depends(require_user)])
async def regen_section(
    protocol_id: uuid.UUID,
    section_name: str,
    payload: SectionPatch,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Regenerate one section with AI, increment the protocol version, snapshot, and audit.

    Author-only, same as create_protocol (see _require_role docstring)."""
    actor = await _authenticate_actor(request, db)
    _require_role(actor, {UserRole.generator}, action="edit protocol sections")

    if section_name not in SECTION_ORDER:
        raise HTTPException(422, detail=f"Unknown section '{section_name}'.")

    protocol = await get_protocol_scoped(db, protocol_id, actor, load_drug=True)

    pk_result = await db.execute(
        select(PKParameterRecord).where(PKParameterRecord.drug_id == protocol.drug_id)
    )
    pk_params = pk_result.scalars().all()

    context = _build_context(
        drug=protocol.drug,
        study_type=protocol.study_type.value,
        population_config=protocol.population_config,
        org_id=protocol.org_id,
        guideline_version=protocol.guideline_version,
        pk_params=pk_params,
    )
    context.update(payload.context_override)

    ai_svc = AIService()
    new_text = await ai_svc.generate_protocol_section(section_name, context)

    old_text: str = protocol.sections.get(section_name, "")
    new_version = protocol.version + 1
    new_sections = {**protocol.sections, section_name: new_text}

    protocol.sections = new_sections
    protocol.version = new_version

    changed_by = actor.id

    _snapshot(
        db,
        protocol_id=protocol_id,
        version=new_version,
        sections=new_sections,
        changed_by=changed_by,
        change_type="AI_REGEN",
        change_detail={"section": section_name},
    )

    db.add(
        AuditLog(
            id=uuid.uuid4(),
            user_id=changed_by,
            action=f"UPDATE:section:{section_name}",
            resource_type="protocol",
            resource_id=str(protocol_id),
            diff={
                "section": section_name,
                "version": new_version,
                "old_preview": old_text[:400],
                "new_preview": new_text[:400],
            },
            timestamp=datetime.now(timezone.utc),
        )
    )

    await db.commit()
    return {
        "protocol_id": str(protocol_id),
        "section_name": section_name,
        "version": new_version,
        "content": new_text,
    }


@router.put("/{protocol_id}/sections/{section_name}", dependencies=[Depends(require_user)])
async def save_section_edit(
    protocol_id: uuid.UUID,
    section_name: str,
    payload: SectionSave,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Persist a manually edited section, increment the protocol version, and snapshot.

    Author-only, same as create_protocol/regen_section (see _require_role
    docstring). This endpoint previously had no auth dependency at all — unlike
    its sibling regen_section (PATCH), it didn't even require require_user.
    """
    actor = await _authenticate_actor(request, db)
    _require_role(actor, {UserRole.generator}, action="edit protocol sections")

    if section_name not in SECTION_ORDER:
        raise HTTPException(422, detail=f"Unknown section '{section_name}'.")

    protocol = await get_protocol_scoped(db, protocol_id, actor)

    old_text: str = protocol.sections.get(section_name, "")
    new_version = protocol.version + 1
    new_sections = {**protocol.sections, section_name: payload.content}

    protocol.sections = new_sections
    protocol.version = new_version

    changed_by = actor.id

    _snapshot(
        db,
        protocol_id=protocol_id,
        version=new_version,
        sections=new_sections,
        changed_by=changed_by,
        change_type="MANUAL_EDIT",
        change_detail={"section": section_name},
    )

    db.add(
        AuditLog(
            id=uuid.uuid4(),
            user_id=changed_by,
            action=f"MANUAL_EDIT:section:{section_name}",
            resource_type="protocol",
            resource_id=str(protocol_id),
            diff={
                "section": section_name,
                "version": new_version,
                "old_preview": old_text[:200],
                "new_preview": payload.content[:200],
            },
            timestamp=datetime.now(timezone.utc),
        )
    )

    await db.commit()
    return {
        "section_name": section_name,
        "content": payload.content,
        "version": new_version,
    }


# ---------------------------------------------------------------------------
# Routes — version history
# ---------------------------------------------------------------------------


@router.get("/{protocol_id}/versions", response_model=list[ProtocolVersionSummary], dependencies=[Depends(require_user)])
async def list_protocol_versions(
    protocol_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Return metadata for every stored version of this protocol, newest first."""
    actor = await _authenticate_actor(request, db)
    await get_protocol_scoped(db, protocol_id, actor)  # org check; result unused

    result = await db.execute(
        select(ProtocolVersion)
        .where(ProtocolVersion.protocol_id == protocol_id)
        .order_by(ProtocolVersion.version.desc())
    )
    return result.scalars().all()


@router.get("/{protocol_id}/versions/{version}", response_model=ProtocolVersionRead, dependencies=[Depends(require_user)])
async def get_protocol_version(
    protocol_id: uuid.UUID,
    version: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Fetch the full section snapshot for a specific version number."""
    actor = await _authenticate_actor(request, db)
    await get_protocol_scoped(db, protocol_id, actor)  # org check; result unused

    result = await db.execute(
        select(ProtocolVersion).where(
            ProtocolVersion.protocol_id == protocol_id,
            ProtocolVersion.version == version,
        )
    )
    pv = result.scalar_one_or_none()
    if not pv:
        raise HTTPException(
            404, detail=f"Version {version} not found for protocol {protocol_id}."
        )
    return pv


# ---------------------------------------------------------------------------
# Routes — diff view
# ---------------------------------------------------------------------------


@router.get("/{protocol_id}/diff", response_model=VersionDiffResult, dependencies=[Depends(require_user)])
async def diff_protocol_versions(
    protocol_id: uuid.UUID,
    request: Request,
    from_version: int = Query(..., description="Earlier version number"),
    to_version: int = Query(..., description="Later version number"),
    db: AsyncSession = Depends(get_db),
):
    """Compare two stored versions section-by-section.

    Returns a unified diff of plain text (HTML tags stripped) for each modified
    section, plus word-count deltas and status flags (unchanged/added/removed/modified).
    """
    actor = await _authenticate_actor(request, db)
    await get_protocol_scoped(db, protocol_id, actor)  # org check; result unused

    if from_version == to_version:
        raise HTTPException(400, detail="from_version and to_version must differ.")

    # Fetch both snapshots in one round-trip
    result = await db.execute(
        select(ProtocolVersion).where(
            ProtocolVersion.protocol_id == protocol_id,
            ProtocolVersion.version.in_([from_version, to_version]),
        )
    )
    rows = {pv.version: pv for pv in result.scalars().all()}

    # Allow from_version=0 to mean "empty document" (useful for initial diff)
    if from_version == 0:
        old_sections: dict[str, Any] = {}
        from_created_at = None
    else:
        old_pv = rows.get(from_version)
        if not old_pv:
            raise HTTPException(404, detail=f"Version {from_version} not found.")
        old_sections = old_pv.sections
        from_created_at = old_pv.created_at

    new_pv = rows.get(to_version)
    if not new_pv:
        raise HTTPException(404, detail=f"Version {to_version} not found.")

    sections = _diff_sections(old_sections, new_pv.sections, from_version, to_version)
    changed = sum(1 for s in sections if s.status != "unchanged")

    return VersionDiffResult(
        protocol_id=protocol_id,
        from_version=from_version,
        to_version=to_version,
        from_created_at=from_created_at,
        to_created_at=new_pv.created_at,
        sections_changed=changed,
        sections=sections,
    )


# ---------------------------------------------------------------------------
# Routes — e-signature (21 CFR Part 11)
# ---------------------------------------------------------------------------


@router.post("/{protocol_id}/sign", response_model=ESignResponse, dependencies=[Depends(require_user)])
async def sign_protocol(
    protocol_id: uuid.UUID,
    payload: ESignRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Electronic signature for regulatory approval.

    Requirements (21 CFR Part 11 §11.50 / §11.70 / §11.200(a)(1)):
      - Signer must have role 'approver' (verified against users table)
      - Re-authentication: the signer re-enters their password at the moment
        of signing (verified via bcrypt against the stored hash); a prior
        login/session alone is not sufficient to apply a signature. This also
        closes a real gap in the identity check below: the acting user id
        comes from the (client-supplied, unauthenticated) X-User-Id header,
        so without this check anyone could claim to be a different user by
        sending that user's id — they would still need that user's actual
        password to complete the signature.
      - The signature's MEANING (e.g. "Approved") is derived from the
        signer's role, never accepted from the client, so it can't be spoofed
      - A SHA-256 hash of the canonical sections JSON is computed and stored
      - The hash binds the signature to an exact document state
      - An append-only AuditLog entry records all signature metadata
      - A ProtocolVersion snapshot with change_type='SIGNED' preserves the
        signed state so the hash can be independently verified later
    """
    signer = await _authenticate_actor(request, db)
    _require_role(signer, {UserRole.approver}, action="sign protocols")
    if not verify_password(payload.reauth_password, signer.hashed_password):
        raise HTTPException(
            401,
            detail="Re-authentication failed: incorrect password. Signing requires "
                   "re-entering your password, even if you are already logged in.",
        )

    # get_protocol_scoped's 403 covers the same "signatory's org != protocol's
    # org" check this used to do manually.
    protocol = await get_protocol_scoped(db, protocol_id, signer)

    if protocol.status == ProtocolStatus.approved:
        raise HTTPException(
            409,
            detail="Protocol is already signed. Create a new version before re-signing.",
        )

    signed_at = datetime.now(timezone.utc)
    sha256_hash = _compute_sections_hash(protocol.sections)
    new_version = protocol.version + 1
    meaning = _SIGNATURE_MEANING_BY_ROLE.get(signer.role, "Signed")

    # Freeze status and bump version so the signed state is distinguishable
    protocol.status = ProtocolStatus.approved
    protocol.version = new_version

    # Immutable snapshot preserves the exact sections that were hashed
    _snapshot(
        db,
        protocol_id=protocol_id,
        version=new_version,
        sections=dict(protocol.sections),
        changed_by=signer.id,
        change_type="SIGNED",
        change_detail={
            "signatory_name": payload.signatory_name,
            "signatory_email": signer.email,
            "meaning": meaning,
            "reason": payload.reason,
            "sha256_hash": sha256_hash,
            "signed_at": signed_at.isoformat(),
        },
    )

    # 21 CFR Part 11 §11.50 — append-only audit entry
    db.add(
        AuditLog(
            id=uuid.uuid4(),
            user_id=signer.id,
            action="E_SIGN:protocol",
            resource_type="protocol",
            resource_id=str(protocol_id),
            diff={
                "signatory_id": str(signer.id),
                "signatory_email": signer.email,
                "signatory_name": payload.signatory_name,
                "meaning": meaning,
                "reason": payload.reason,
                "sha256_hash": sha256_hash,
                "signed_version": new_version,
                "signed_at": signed_at.isoformat(),
            },
            timestamp=signed_at,
        )
    )

    await db.commit()

    return ESignResponse(
        protocol_id=protocol_id,
        version=new_version,
        sha256_hash=sha256_hash,
        signed_at=signed_at,
        signatory_id=signer.id,
        signatory_email=signer.email,
        status=ProtocolStatus.approved,
        meaning=meaning,
    )
