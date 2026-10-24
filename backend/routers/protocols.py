"""Protocol router — generation pipeline, section edits, version history, diff, and e-signing."""

from __future__ import annotations

import asyncio
import difflib
import functools
import hashlib
import json
import logging
import re
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

import database as _database
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
    DdiPreviewRequest,
    DesignPreviewRequest,
    IndicationPreviewRequest,
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
    apply_engine_population_defaults,
    classify_drug_modality,
    determine_design_decisions,
    decisions_to_dicts,
    reconcile_decision_set,
)
from auth import require_user
from services.bioanalytical import plan_bioanalytical
from services.biostatistics import (
    WASHOUT_DESIGN_KEYS,
    WASHOUT_HALF_LIVES,
    engine_default_washout,
    format_washout_duration,
    recommend_sample_size,
    validate_washout_override,
)
from services.crf_dmp import build_crf_spec
from services.ddi_screening import screen_interactions
from services.guideline_coverage import compute_coverage
from services.guidelines import GUIDELINES, is_supported
from services.icf import build_icf
from services.literature_service import LiteratureService, plan_regulatory_sources
from services.comparative_safety import build_duration_plan
from services.meta_analysis import build_meta_analysis, derive_cv_from_mean_sd
from services.patient_population import select_patient_population
from services.protocol_qc import run_qc
from services.randomization import blinded_view, generate_allocation
from services.safety_gating import recommend_safety_gating
from services.safety_reporting import build_safety_reporting_plan
from services.sampling_schedule import compute_sampling_schedule
from services.stratification import recommend_stratification
from services.trial_precedent import select_trial_precedents
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

# Flat, user-supplied hint values copied verbatim from population_config to the
# top level of the generation context. Computed engine results are NOT listed
# here — they are underscore-prefixed keys copied explicitly in _build_context.
_OPTIONAL_CTX_KEYS = (
    "dose_strength",
    "formulation",
    "analytical_method",
    "lloq",
    "acceptance_criteria",
    # Investigator-specified within-subject CV%. Supplying it OVERRIDES the
    # literature-sourced value and changes the declared provenance — see
    # _resolve_powering_cv, which is the single place that decides both.
    "cv_intra",
    "irb_info",
    "org_name",
    # --- sampling-schedule hints -------------------------------------------
    "dosing",                  # 'single' | 'multiple'
    "dosing_interval_hours",   # tau, hours (multiple-dose only)
    # --- comparative safety / immunogenicity duration -----------------------
    # Months of head-to-head follow-up. Omitted => the engine's own default
    # (12 months, the EU expectation, since the comparator is the EU reference
    # medicinal product); see services/comparative_safety.py.
    "comparative_safety_months",
    # --- bioanalytical hint -------------------------------------------------
    "dose_mg",
    # --- safety-reporting hints (sponsor regulatory-affairs overrides) ------
    "is_ind",
    "is_applicable_clinical_trial",
    "is_new_chemical_entity",
    "radiolabeled",
    # --- ICF hints (site-specific; absent ones become visible placeholders) --
    "icf_version",
    "icf_date",
    "protocol_number",
    "protocol_version",
    "protocol_title",
    "investigator_name",
    "site_name",
    "study_contact",
    "rights_contact",
    "injury_contact",
    "compensation_text",
    "confinement_hours",
    "n_pk_draws_per_period",
    "clinicaltrials_gov_id",
    "safety_text",
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


# Unit spellings the PK extractor has been observed to emit. A Tmax reported in
# minutes read as hours would put the dense peak cluster of the sampling
# schedule in completely the wrong place, so the conversion is explicit and
# anything unrecognised is DROPPED rather than guessed at.
_MINUTE_UNITS = {"min", "mins", "minute", "minutes"}
_DAY_UNITS = {"d", "day", "days"}
_HOUR_UNITS = {"h", "hr", "hrs", "hour", "hours", ""}


def _median_tmax_hours(pk_params: list[PKParameterRecord]) -> float | None:
    """Median Tmax in HOURS from extracted PK records, if any.

    Feeds services/sampling_schedule.py, which places its dense peak cluster as
    fractions of Tmax. Units are normalised explicitly (see _MINUTE_UNITS).
    """
    vals: list[float] = []
    for p in pk_params:
        if p.parameter != PKParameter.tmax or not p.value or p.value <= 0:
            continue
        unit = (p.unit or "").strip().lower()
        if unit in _MINUTE_UNITS:
            vals.append(float(p.value) / 60.0)
        elif unit in _DAY_UNITS:
            vals.append(float(p.value) * 24.0)
        elif unit in _HOUR_UNITS:
            vals.append(float(p.value))
        # else: unrecognised unit — safer to drop than to guess.
    return statistics.median(vals) if vals else None


def _pk_records_to_dicts(pk_params: list[PKParameterRecord]) -> list[dict[str, Any]]:
    """The JSON-ish PK record shape the generation context and several of the
    deterministic engines (ddi_screening, bioanalytical) both consume.

    Defined once so the records the engines reason about are byte-identical to
    the ones the prompt sees — a divergence here would let an engine lock a fact
    that the narrative context contradicts.
    """
    return [
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


def _truncate(value: str | None, max_len: int) -> str | None:
    """Defensively cap an AI-extracted string to a DB column's max length."""
    if value is None:
        return None
    return value if len(value) <= max_len else value[: max_len - 1] + "…"


_NUMBER_RE = re.compile(
    # (?<!\d) blocks a '-' directly abutting a PRECEDING digit from being read
    # as that number's sign — without it, a no-space range like "24-48" (a
    # realistic way to type "add draws at 24 and 48") parsed as [24.0, -48.0]:
    # the hyphen was read as a minus sign onto 48 instead of a separator. A
    # genuine negative ("-5, 10", or "-5" at the start) is still captured,
    # since there the character before '-' is whitespace/a separator/nothing,
    # not a digit. [eE][-+]?\d+ supports scientific notation ("1e5") as ONE
    # number (100000.0) instead of silently splitting it into the two
    # unrelated numbers "1" and "5" a plain digit regex would extract.
    r"(?<!\d)-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?"
)


def _parse_number_list(raw: Any) -> list[float]:
    """Extract every number out of a free-text field (e.g. "36, 48h, 72 hrs")
    for the additional-PK/-immunogenicity-timepoint population_config inputs.

    Deliberately permissive rather than a strict comma-split-and-cast: a user
    typing units ("36h"), extra whitespace, or a stray separator should still
    get their numbers through rather than have the whole field silently
    dropped or the request rejected with a 422 over a formatting nit. Genuine
    validation (is this number a SENSIBLE timepoint for this study) happens
    downstream in the engine, which already discloses out-of-range values in
    its own `assumptions` rather than raising.
    """
    if not raw or not isinstance(raw, str):
        return []
    return [float(m) for m in _NUMBER_RE.findall(raw)]


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


# The cv_source string recorded when the CV was hand-entered. It starts with
# "override" because services/risk_scoring.py keys the "CV was manually
# overridden" risk factor off exactly that prefix, and it names the provenance
# in plain words because services/ai_service.py::_locked_design_directive
# prints it verbatim into the prompt ("source: ...").
_CV_SOURCE_OVERRIDE = "override (investigator-specified; not literature-derived)"
_CV_SOURCE_LITERATURE = "literature (highest observed)"


def _resolve_powering_cv(
    population_config: dict[str, Any],
    extracted: list[dict[str, Any]],
    literature: list[dict[str, Any]] | None = None,
    study_type: str | None = None,
) -> tuple[float | None, dict[str, Any]]:
    """Resolve the CV% used to power the study AND its provenance, together.

    This function is the ONLY place either is decided, and it always returns
    both at once, so the powering number and the evidence presented as its
    justification cannot be set independently. That coupling is the point.

    The defect this prevents (it shipped once, and was the reason the manual
    override was removed entirely): a hand-entered CV won over the sourced
    value, but ``cv_table`` — the supporting-trials table the SAP renders as the
    provenance of the powering figure — was still populated from the literature.
    The document then cited studies that did not produce the number the study
    was actually powered on. Here, an override yields ``cv_table == []`` BY
    CONSTRUCTION; the literature rows are still retained for the record, but
    under a key whose name cannot be mistaken for a justification.

    ``study_type`` (and, when given, ``population_config["design"]``) decide
    the CV BASIS this protocol needs (see services/meta_analysis.py's
    ``cv_basis``). ``recommend_sample_size`` (services/biostatistics.py) feeds
    the resolved CV to the powering calculation with NO within-to-between
    conversion applied ONLY when the design is parallel AND the study is
    biosimilar — that is the one combination whose CV needs to already BE the
    between-subject/total quantity a mean+SD pair can supply directly, so
    ``cv_basis="between"`` is used exactly then. Every other combination
    (including a biosimilar_pk study whose design has been explicitly
    overridden away from parallel) is powered assuming the resolved CV is
    within-subject — ``recommend_sample_size`` does its OWN x1.5
    within-to-between conversion internally when the eventual design turns
    out parallel — so ``cv_basis="within"`` is used there and a
    between-subject-derived value (which cannot be converted to a
    within-subject one by simple arithmetic) is tabulated but never adopted,
    falling back to the "assumed" CV instead.

    An earlier version of this function assumed "biosimilar_pk always means
    parallel", which is false: ``recommend_sample_size``'s own design-selection
    logic checks an explicit ``requested_design`` override FIRST, before its
    biosimilar-implies-parallel default. Since that override lives in
    ``population_config["design"]`` — the SAME dict this function already
    receives, set by the caller up front rather than computed later — it can
    and must be consulted here too; ignoring it let a biosimilar protocol with
    ``design="replicate"`` or ``"crossover"`` silently feed an unconverted
    between-subject CV into a within-subject-only power calculation, roughly
    halving the recommended N. For every OTHER design ambiguity (NTI/HVD
    replicate selection depends on the CV itself; long-half-life parallel
    selection depends on PK data this function does not receive), the design
    genuinely is not knowable yet — recommend_sample_size decides it AFTER
    this function returns — so the safe default there remains "within",
    exactly as before.

    Returns ``(cv_pct_or_None, provenance)``. ``provenance`` is merged wholesale
    into the sample-size dict; ``provenance["cv_source"]`` (absent when nothing
    is available, so biostatistics' own "assumed" label stands) is what is
    passed to recommend_sample_size.
    """
    is_biosimilar = str(study_type or "").strip() == "biosimilar_pk"
    requested_design = population_config.get("design")
    if requested_design in ("crossover", "parallel", "replicate"):
        # An explicit design request is deterministic and known up front — it
        # wins over every other rule in recommend_sample_size's own
        # design-selection chain, including the biosimilar default, so it must
        # win here too.
        is_parallel = requested_design == "parallel"
    else:
        # No override: biosimilar_pk is the one study_type whose DEFAULT
        # (unrequested) design is deterministically parallel. Every other
        # study_type's default design depends on the CV itself (is_hvd) or on
        # drug/PK facts this function does not receive, so it is
        # conservatively treated as not-yet-known-parallel.
        is_parallel = is_biosimilar
    cv_basis = "between" if (is_parallel and is_biosimilar) else "within"
    basis_label = "within-subject" if cv_basis == "within" else "between-subject (total)"

    # Population-matched: the CV that powers the study and the meta-analysis
    # table the appendix prints come from ONE call, so they cannot disagree.
    # A healthy-volunteer protocol is powered on healthy-volunteer studies; a
    # patient protocol on patient studies. See services/meta_analysis.py for
    # why mixing them silently mis-sizes the trial.
    #
    # derive_cv_from_mean_sd() runs first so a source that reports mean +/- SD
    # for Cmax/AUC without a separate percentage CV - the norm for a biologic
    # label or a first-in-human paper - still contributes a CV here instead of
    # silently falling through to "assumed" for a drug whose own literature
    # already answers the question. It always derives a BETWEEN-subject value
    # (that is all a single cohort's mean/SD can ever supply); build_meta_analysis
    # only lets it power the study when cv_basis == "between" above.
    meta = build_meta_analysis(
        derive_cv_from_mean_sd(extracted),
        subject_type=population_config.get("subject_type", "healthy"),
        literature=literature,
        cv_basis=cv_basis,
    )
    cv_high = meta.get("powering_cv_pct")
    cv_table = meta.get("studies_matching") or []

    raw = population_config.get("cv_intra")
    override: float | None = None
    if raw is not None:
        try:
            candidate = float(raw)
        except (TypeError, ValueError):
            candidate = 0.0
        if candidate > 0:
            override = candidate

    if override is not None:
        return override, {
            "cv_source": _CV_SOURCE_OVERRIDE,
            "cv_is_user_override": True,
            # Empty BY CONSTRUCTION: an investigator-specified CV has no
            # supporting-trials provenance, so the SAP has no table to render
            # and cannot claim sourcing the number does not have.
            "cv_table": [],
            # Retained for the audit record only. Deliberately NOT named
            # cv_table, and never rendered as the powering justification.
            "cv_literature_not_used_for_powering": cv_table,
            "cv_literature_highest_pct": cv_high,
            # The appendix still prints the full meta-analysis: an override
            # replaces the powering NUMBER, not the evidence review.
            "meta_analysis": meta,
            "cv_provenance_note": (
                f"The {basis_label} CV of {override:.1f}% used to power this study was "
                "specified by the investigator/sponsor, NOT derived from the published "
                "literature. State its justification explicitly and do NOT present any "
                "table of literature trials as its source."
                + (
                    f" For reference only, the highest {basis_label} CV found in the "
                    f"literature search was {cv_high:.1f}%."
                    if cv_high else
                    f" No {basis_label} CV was retrievable from the literature search."
                )
            ),
        }

    if cv_high:
        # Power on the HIGHEST observed CV (Cmax or AUC), of the basis this
        # design needs, across the population-matched literature — the
        # conservative regulatory convention.
        return cv_high, {
            "cv_source": _CV_SOURCE_LITERATURE,
            "cv_is_user_override": False,
            # The CV trials table backs the Sample Size Determination narrative
            # in the SAP — present here precisely because the number came from
            # these trials.
            "cv_table": cv_table,
            "meta_analysis": meta,
            "cv_provenance_note": (
                f"The {basis_label} CV of {cv_high:.1f}% is the highest value observed for "
                "either Cmax or AUC across the published "
                f"{'patient' if meta.get('target_population') == 'patient' else 'healthy-volunteer'} "
                "studies listed in cv_table. Studies in other populations, and CVs of the wrong "
                "basis, were retrieved and tabulated in the meta-analysis appendix but "
                "deliberately NOT used for powering."
            ),
        }

    # Nothing retrievable and nothing supplied: recommend_sample_size falls back
    # to its own clearly-flagged ASSUMED_CV_PCT and labels the source "assumed".
    # cv_source is deliberately omitted so that label is not overwritten here.
    return None, {
        "cv_is_user_override": False,
        "cv_table": [],
        "meta_analysis": meta,
        "cv_provenance_note": (
            f"No {basis_label} CV was retrievable for the protocol's own population "
            "and none was supplied; the powering CV is an explicitly-flagged "
            "assumption. The meta-analysis appendix states this plainly."
        ),
    }


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
        # The 1500-char trim above can cut off adjuvant / immunology indications
        # that long oncology labels list late. This untrimmed copy (capped) is read
        # ONLY by select_patient_population; _prompt_safe_label strips it from
        # everything else, so it never reaches a prompt or another engine.
        "indications_usage_full": (secs.get("indications_usage") or "").strip()[:12000],
    }
    # Drop empty fields so the prompt context stays clean.
    return {k: v for k, v in label.items() if v}


def _prompt_safe_label(label: dict[str, str] | None) -> dict[str, str] | None:
    """The label without the full-length indications text (see _fetch_label_safe)."""
    if not label:
        return label
    return {k: v for k, v in label.items() if k != "indications_usage_full"}


def _engine_decision_prestep(
    cfg: dict[str, Any], drug_inn: str, study_type: str
) -> None:
    """Adopt the ENGINE's own population/dose call into ``cfg`` (in place) when
    no reviewer decisions were supplied, so an engine-chosen patient / multi-dose
    (target-mediated-disposition or safety-gated) study reaches every PK engine.
    Upgrade-only: never downgrades patient to healthy or overwrites 'dosing'.
    Biosimilar PK studies only: the pharmacologist's TMDD / multi-dose feedback
    is about patient biosimilar trials, and adopting the engine's population
    call for e.g. a bioequivalence study of a cytotoxic product would silently
    change that study's sizing and sampling inputs."""
    if str(study_type or "").strip().lower() != "biosimilar_pk":
        return
    pre = decisions_to_dicts(
        determine_design_decisions(
            drug_inn=drug_inn,
            study_type=study_type,
            dosing=str(cfg.get("dosing") or "single"),
            subject_type=str(cfg.get("subject_type") or "healthy"),
        )
    )
    apply_engine_population_defaults(cfg, pre)


async def _fetch_trial_precedent_safe(
    lit_svc: LiteratureService, drug_inn: str, *, study_type: str, subject_type: str
) -> list[dict[str, Any]]:
    """Fetch ClinicalTrials.gov records for the inclusion/exclusion-criteria
    precedent engine (services/trial_precedent.py); never raises into the
    pipeline. Gated on study_type/subject_type BEFORE the HTTP call, not
    inside the (unconditionally-invoked) deterministic engine, so a BE study
    or a healthy-volunteer biosimilar_pk study — the large majority of
    generations — never issues this request at all: trial precedent is only
    ever relevant when a specific indication is being selected
    (services/patient_population.py's own applicability gate), and firing an
    HTTP call this pipeline will not use just adds latency and an external
    dependency with nothing to show for it.
    """
    is_patient = str(subject_type or "healthy").strip().lower() == "patient"
    is_biosimilar = str(study_type or "").strip().lower() == "biosimilar_pk"
    if not (is_patient and is_biosimilar):
        return []
    try:
        return await lit_svc.fetch_clinicaltrials(drug_inn)
    except Exception:
        logger.exception("ClinicalTrials.gov fetch failed; continuing without trial precedent")
        return []


# Dedicated thread-pool capacity for _augment_config_with_design's blocking
# call (below), separate from Starlette's shared default limiter that
# auth.py's per-request JWT verification also runs on via run_in_threadpool.
# This call can occupy a thread for seconds (the underlying R subprocess in
# services/powertost_r.py carries a 60s timeout), so without a dedicated cap a
# burst of concurrent generations could exhaust the shared pool and start
# queuing unrelated, otherwise-instant auth checks behind them.
_DESIGN_THREAD_LIMITER = CapacityLimiter(4)


def _safe_engine(name: str, fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Run one deterministic engine, returning None if it raises.

    Every engine here is pure Python over already-fetched inputs, so a raise
    means a genuine input the engine rejects (e.g. randomization refuses an
    n_enrolled below 2, or a stratification factor with no matching stratum).
    That must not take down an otherwise-complete generation: the protocol is
    still produced, the engine's key is simply absent, and the QC layer sees the
    absence. The failure is logged loudly with the engine name so it is
    diagnosable rather than silent.
    """
    try:
        return fn(*args, **kwargs)
    except Exception:  # noqa: BLE001 — one engine must not abort generation
        logger.exception("deterministic engine '%s' failed; continuing without it", name)
        return None


def _randomization_seed(
    cfg: dict[str, Any],
    protocol_id: uuid.UUID | None,
    drug_inn: str,
    study_type: str,
) -> int:
    """The integer seed the randomization list is generated from.

    Chosen so that regenerating reproduces the SAME list, and so that two
    different studies never share one:

    1. An already-stored ``randomization_seed`` always wins. Once a list has
       been generated and filed, the seed that produced it is the record; a
       later re-run must reuse it or the filed list stops being reproducible.
    2. Otherwise it is derived from the protocol's own UUID — stable for the
       life of the protocol (the id never changes), unique per protocol, and
       already persisted, so the derivation is auditable from the row itself.
    3. Only if no protocol id is available does it fall back to a SHA-256 of the
       drug/study identity. That is deterministic but NOT protocol-unique, so it
       is a last resort rather than the primary rule.

    A random seed would be none of these things: the filed list could never be
    regenerated for an inspection.
    """
    for key in ("_randomization_seed", "randomization_seed"):
        stored = cfg.get(key)
        if isinstance(stored, int) and not isinstance(stored, bool) and stored > 0:
            return stored
    if protocol_id is not None:
        return int.from_bytes(protocol_id.bytes[:6], "big")
    digest = hashlib.sha256(f"{drug_inn}|{study_type}".encode("utf-8")).digest()
    return int.from_bytes(digest[:6], "big")


def _lock_washout(ss: dict[str, Any], cfg: dict[str, Any]) -> None:
    """Decide the washout between periods AND the basis it is presented under,
    together, and lock both into the sample-size recommendation ``ss``.

    Same contract as ``_resolve_powering_cv``: this is the ONLY place the
    washout figure and its provenance are set, so a number can never travel
    with a source that does not belong to it. ``ss["washout_days"]`` (with
    ``washout_hours``) is what ai_service._locked_design_directive tells the
    model to use verbatim, what protocol_qc checks the prose against, and what
    the export reads - before this the figure was left for the model to
    re-derive from t1/2, so it could (and did) differ between sections.

    * ``cfg["washout_days"]`` (an investigator/sponsor request, e.g. from chat)
      is validated by ``biostatistics.validate_washout_override`` against the
      design the engines actually chose and the t1/2 they actually used
      (``ss["t_half_hours"]``). A parallel design, a value below 7 x t1/2, an
      implausible value, or an unknown t1/2 raises 422 - the override is refused
      outright, never silently replaced by the default. (In the production
      background path that 422 is recorded as the generation's failure reason.)
    * Otherwise the engine default (7 x t1/2) is computed and locked as a
      number, for crossover / replicate designs only.
    * A parallel design is locked as "not applicable". A crossover/replicate
      design whose t1/2 is unknown gets no number (nothing honest to state).

    Keys written to ``ss``: ``washout_applicable``, ``washout_days``,
    ``washout_hours``, ``washout_is_user_override``, ``washout_minimum_days``
    (the engine floor), ``washout_source``.
    """
    design_key = ss.get("design_key")
    t_half = ss.get("t_half_hours")

    raw = cfg.get("washout_days")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        cfg.pop("washout_days", None)
        raw = None

    if raw is not None:
        value: Any = raw
        if isinstance(raw, str):
            try:
                value = float(raw.strip())
            except ValueError:
                value = raw  # the validator rejects a non-number with its message
        ok, minimum, message = validate_washout_override(design_key, t_half, value)
        if not ok:
            raise HTTPException(status_code=422, detail=message)
        days = float(value)
        cfg["washout_days"] = days
        ss.update({
            "washout_applicable": True,
            "washout_days": days,
            "washout_hours": round(days * 24.0, 6),
            "washout_is_user_override": True,
            "washout_minimum_days": minimum,
            "washout_source": (
                "user override (investigator-specified; engine minimum "
                f"{WASHOUT_HALF_LIVES:.0f} x t1/2 = {format_washout_duration(minimum)})"
            ),
        })
        return

    if design_key == "parallel" or design_key not in WASHOUT_DESIGN_KEYS:
        ss.update({
            "washout_applicable": False,
            "washout_days": None,
            "washout_hours": None,
            "washout_is_user_override": False,
            "washout_minimum_days": None,
            "washout_source": "not applicable (parallel-group design has no washout period)"
            if design_key == "parallel" else "not applicable (no multi-period design)",
        })
        return

    default = engine_default_washout(t_half)
    if default is None:
        ss.update({
            "washout_applicable": True,
            "washout_days": None,
            "washout_hours": None,
            "washout_is_user_override": False,
            "washout_minimum_days": None,
            "washout_source": "unavailable (no terminal half-life to apply "
                              f"{WASHOUT_HALF_LIVES:.0f} x t1/2 to)",
        })
        return
    ss.update({
        "washout_applicable": True,
        "washout_days": default["days"],
        "washout_hours": default["hours"],
        "washout_is_user_override": False,
        "washout_minimum_days": default["days"],
        "washout_source": (
            f"engine: {WASHOUT_HALF_LIVES:.0f} x t1/2 = "
            f"{format_washout_duration(default['days'])} "
            f"(t1/2 = {default['t_half_hours']:.1f} h)"
        ),
    })


def _augment_config_with_design(
    population_config: dict[str, Any],
    study_type: str,
    drug_inn: str,
    extracted: list[dict[str, Any]],
    pk_params: list[PKParameterRecord],
    label: dict[str, str] | None,
    guideline_version: str = "",
    design_decisions: list[dict[str, Any]] | None = None,
    protocol_id: uuid.UUID | None = None,
    regulatory_sources: dict[str, Any] | None = None,
    trial_records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run every deterministic engine ONCE and bake the results into the config.

    This makes the computed design the single source of truth: the same numbers
    feed the synopsis, study-design and SAP prompts and the export tables, and
    they stay stable across section regenerations. Each engine's result is
    stored under an underscore-prefixed key (server-computed, never accepted
    from a client) and copied into the prompt context by _build_context.

    Ordering matters and is deliberate: sample size first (it decides the
    design), then the engines that consume the design, then the two document
    builders (CRF/DMP and ICF) that consume the engines before them.
    """
    cfg = dict(population_config)
    pk_list = _pk_records_to_dicts(pk_params)
    # The untrimmed indications text is for indication selection ONLY (step 4c);
    # every other consumer, including cfg["_label"] and the engine contexts, gets
    # the prompt-safe label.
    label_full = label
    label = _prompt_safe_label(label)

    # --- 0. Population / dosing must be settled BEFORE sizing ---------------
    # The sample-size, sampling-schedule and comparative-safety engines below
    # read cfg["dosing"] and cfg["subject_type"], but the decisions that set them
    # (the engine's TMDD / safety-gate call, or the reviewer's Review-design
    # choice) used to be computed only at step 3 - so a "Patients · multi-dose"
    # decision produced a multi-dose label on a study still sized and sampled as
    # single-dose. With reviewer decisions they are reconciled here and
    # folded into cfg here too (idempotent, so a caller that already folded
    # them for the trial-precedent fetch is unaffected, and a caller that did
    # not still gets a study sized and sampled as the decisions say); without,
    # the engine's own call is adopted. Must run before _resolve_powering_cv,
    # which reads subject_type.
    if design_decisions:
        design_decisions = reconcile_decision_set(
            design_decisions, drug_inn=drug_inn, study_type=study_type
        )
        apply_decision_overrides(cfg, design_decisions)
    else:
        _engine_decision_prestep(cfg, drug_inn, study_type)

    # --- 1. Sample size + CV provenance -----------------------------------
    # The CV and the evidence presented as its justification are decided
    # together, in one place, so they cannot diverge — see _resolve_powering_cv.
    cv_pct, cv_provenance = _resolve_powering_cv(cfg, extracted, study_type=study_type)

    t_half = _median_t_half(pk_params)
    tmax = _median_tmax_hours(pk_params)
    rec = recommend_sample_size(
        study_type=study_type,
        drug_inn=drug_inn,
        cv_intra_pct=cv_pct,
        cv_source=cv_provenance.get("cv_source"),
        t_half_hours=t_half,
        is_nti=cfg.get("is_nti"),
        requested_design=cfg.get("design"),
        # Use the real R PowerTOST package as the authoritative, citable
        # sample-size source for the regulatory record when R is installed
        # (falls back to the validated Python engine otherwise).
        use_r=True,
        # Decide the co-primary endpoint SET and its multiplicity, which the
        # sample size then follows: a multiple-dose biosimilar study compares
        # first-dose AND steady-state AUC(0-tau) and must be powered so that
        # BOTH pass (services/multiplicity.py), whereas a single-dose study
        # keeps the conventional Cmax + AUC(0-inf) pair and the conventional
        # per-endpoint powering basis.
        dosing=str(cfg.get("dosing") or "single"),
        subject_type=str(cfg.get("subject_type") or "healthy"),
    )
    ss = rec.to_dict()
    # Carry the median half-life alongside the recommendation so the QC
    # validator can check washout adequacy without re-querying PK records.
    ss["t_half_hours"] = t_half
    ss["tmax_hours"] = tmax
    # cv_table / cv_source / cv_is_user_override all arrive from the single
    # resolver above. Nothing else in this module writes them.
    ss.update(cv_provenance)
    # The washout between periods is locked HERE, with its source, from the
    # design just chosen and the t1/2 just used - and a requested override
    # (population_config["washout_days"]) is validated against exactly those.
    _lock_washout(ss, cfg)
    cfg["_sample_size"] = ss
    # Surfaced separately so the appendix renderer and the SAP prompt can reach
    # the per-study table without digging through the sample-size dict. Same
    # object, so the adopted CV in the table is by definition the powering CV.
    cfg["_meta_analysis"] = cv_provenance.get("meta_analysis") or {}

    design_key = ss.get("design_key")
    is_nti = ss.get("is_nti") if ss.get("is_nti") is not None else cfg.get("is_nti")
    healthy_volunteers = str(cfg.get("subject_type", "healthy")).lower() != "patient"
    is_biologic = classify_drug_modality(drug_inn) == "biologic" or study_type == "biosimilar_pk"
    dosing = str(cfg.get("dosing") or "single")

    # --- 2. Stratification -------------------------------------------------
    # Stratification factors are resolved ONCE here, next to the design they
    # depend on, for the same single-source-of-truth reason as _sample_size: the
    # randomisation narrative, the population section and the export table must
    # all cite the identical factor list. Derived from the real label text, so
    # the applicability judgement is grounded rather than model-recalled.
    cfg["_stratification"] = recommend_stratification(
        design_key=design_key,
        label=label,
        is_nti=cfg.get("is_nti"),
        cv_intra_pct=ss.get("cv_pct"),
        healthy_volunteers=healthy_volunteers,
        # BUG FIX: this was previously omitted, so recommend_stratification()
        # always ran with its is_biologic=False default — the entire
        # modality-suppression gate in services/stratification.py (dropping
        # CYP/transporter/smoking/NAT2/gastric-pH factors and the
        # non-smoker-status leak they cause) never actually fired for a real
        # biosimilar_pk protocol, even though is_biologic is computed above and
        # every OTHER engine call in this function (sampling schedule, DDI
        # screen, safety gating, bioanalytical, CRF spec) already receives it.
        is_biologic=is_biologic,
    )

    # --- 3. The five locked clinical design decisions ----------------------
    # Finalised HERE rather than after this function returns, because four of
    # the engines below (randomisation blinding, CRF PD forms, safety gating
    # route, ICF population) have to agree with them. The reviewer's choices
    # from the "Review design" step win; otherwise they are derived fresh from
    # the design just computed, so generation ALWAYS reflects the 5 rules.
    label_text = " ".join(filter(None, [
        (label or {}).get("dosage_and_administration", ""),
        (label or {}).get("how_supplied", ""),
    ]))
    decisions = design_decisions or decisions_to_dicts(
        determine_design_decisions(
            drug_inn=drug_inn,
            study_type=study_type,
            design_key=design_key,
            t_half_hours=t_half,
            label_text=label_text,
            # An efficacy/PD marker is co-primary in a single-dose study but
            # SECONDARY (and never powered on) in a multiple-dose patient
            # biosimilar study — see design_decisions._pd_decision.
            dosing=dosing,
            subject_type=str(cfg.get("subject_type") or "healthy"),
        )
    )
    cfg["_design_decisions"] = decisions
    decision_values = {
        d.get("key"): (d.get("value") or "")
        for d in decisions if isinstance(d, dict)
    }
    pd_value = decision_values.get("pd_endpoint", "")
    pd_coprimary = bool(pd_value) and not pd_value.strip().lower().startswith("none")

    # --- 4a. Study duration: doses to steady state + comparative safety -----
    # Computed BEFORE the sampling schedule (step 4b) on purpose: its
    # resolved `safety_months` is what the sampling schedule's own ADA
    # (immunogenicity) sampling must span for a multiple-dose biosimilar
    # study, so the two engines cannot independently propose two different
    # ADA schedules for the same comparative period. t_half_basis is not yet
    # known at this point (the sampling schedule is what resolves it, honestly
    # or via a flagged assumption), so it is passed through blank here and the
    # duration plan's own text simply omits the parenthetical basis note.
    cfg["_comparative_safety"] = _safe_engine(
        "comparative_safety",
        build_duration_plan,
        study_type=study_type,
        dosing=dosing,
        subject_type=str(cfg.get("subject_type") or "healthy"),
        t_half_hours=t_half,
        t_half_basis="literature" if t_half else "",
        tau_hours=cfg.get("dosing_interval_hours"),
        # The comparator is the EU reference medicinal product for every
        # biosimilar study in this application (services/guidelines.py), which
        # puts the programme on the EU 12-month comparative-safety expectation.
        eu_reference_product=True,
        safety_months=cfg.get("comparative_safety_months"),
        additional_immunogenicity_months=_parse_number_list(
            cfg.get("additional_immunogenicity_timepoints_months")
        ),
    )

    # --- 4b. PK blood-sampling schedule ------------------------------------
    cfg["_sampling_schedule"] = _safe_engine(
        "sampling_schedule",
        compute_sampling_schedule,
        t_half,
        tmax,
        design_key=design_key,
        study_type=study_type,
        dosing=dosing,
        is_biologic=is_biologic,
        label=label,
        tau_hours=cfg.get("dosing_interval_hours"),
        # For a multiple-dose biosimilar study, the ADA sampling must span the
        # SAME comparative-safety period computed just above — see
        # compute_sampling_schedule's own docstring for why this parameter
        # exists at all.
        safety_months=(cfg.get("_comparative_safety") or {}).get("safety_months"),
        subject_type=str(cfg.get("subject_type") or "healthy"),
        additional_timepoints_hours=_parse_number_list(cfg.get("additional_pk_timepoints_hours")),
        additional_immunogenicity_months=_parse_number_list(
            cfg.get("additional_immunogenicity_timepoints_months")
        ),
    )
    sched = cfg.get("_sampling_schedule")

    # --- 4c. Patient population / indication selection ---------------------
    # Only produces anything for a PATIENT biosimilar study; a healthy-volunteer
    # study returns applicable=False and contributes nothing to the prompt.
    # Runs AFTER the meta-analysis (step 1) because rule 4 — "where monotherapy
    # is unavailable, pick the indication with the lowest PK variability from
    # prior trials" — is ranked on the real per-study CVs that engine extracted,
    # not on a qualitative guess, whenever the literature actually supplies them.
    cfg["_patient_population"] = _safe_engine(
        "patient_population",
        select_patient_population,
        drug_inn=drug_inn,
        study_type=study_type,
        subject_type=str(cfg.get("subject_type") or "healthy"),
        label=label_full,
        meta_analysis=cfg.get("_meta_analysis") or {},
        user_indication=cfg.get("user_indication"),
    )
    if not (cfg.get("_patient_population") or {}).get("applicable"):
        cfg.pop("user_indication", None)

    # --- 4d. Inclusion/exclusion criteria precedent (real prior trials) -----
    # Runs AFTER _patient_population (immediately above): it needs the
    # indication THAT engine selected to filter the drug-level (not
    # indication-specific) ClinicalTrials.gov cache down to trials relevant
    # to this specific study.
    cfg["_trial_precedent"] = _safe_engine(
        "trial_precedent",
        select_trial_precedents,
        drug_inn=drug_inn,
        patient_population=cfg.get("_patient_population") or {},
        trials=trial_records or [],
    )

    # --- 5. Drug-drug interaction screening (concomitant-medication rules) --
    cfg["_ddi"] = _safe_engine(
        "ddi_screening",
        screen_interactions,
        label,
        pk_list,
        drug_inn,
        design_key=design_key,
        is_nti=cfg.get("is_nti"),
        healthy_volunteers=healthy_volunteers,
        is_biologic=is_biologic,
    )

    # --- 6. Safety gating (stopping / halting rules, discharge criteria) ----
    # n_enrolled/dropout_rate come from the SAME recommendation the study is
    # powered on, so the halting counts and the powering assumption cannot
    # contradict each other in one document.
    cfg["_safety_gating"] = _safe_engine(
        "safety_gating",
        recommend_safety_gating,
        study_type=study_type,
        design_key=design_key,
        n_enrolled=ss.get("n_enrolled"),
        n_evaluable=ss.get("n_evaluable"),
        label=label,
        is_nti=cfg.get("is_nti"),
        route=decision_values.get("route") or None,
        dropout_rate=ss.get("dropout_rate"),
        healthy_volunteers=healthy_volunteers,
        is_biologic=is_biologic,
    )

    # --- 7. Bioanalytical method validation package ------------------------
    cfg["_bioanalytical"] = _safe_engine(
        "bioanalytical",
        plan_bioanalytical,
        pk_parameters=pk_list,
        label=label,
        drug_inn=drug_inn,
        study_type=study_type,
        is_biologic=is_biologic,
        dose_mg=cfg.get("dose_mg"),
    )

    # --- 8. Expedited safety reporting plan --------------------------------
    cfg["_safety_reporting"] = _safe_engine(
        "safety_reporting",
        build_safety_reporting_plan,
        study_type,
        cfg.get("is_ind"),
        cfg.get("org_name"),
        drug_inn=drug_inn,
        label=label,
        healthy_volunteers=healthy_volunteers,
        guideline_version=guideline_version or None,
        is_applicable_clinical_trial=cfg.get("is_applicable_clinical_trial"),
        is_new_chemical_entity=bool(cfg.get("is_new_chemical_entity")),
        radiolabeled=bool(cfg.get("radiolabeled")),
    )

    # --- 9. Randomization list ---------------------------------------------
    # The seed is derived deterministically and PERSISTED (see
    # _randomization_seed): the filed allocation list must be regenerable years
    # later during an inspection, which a fresh random seed would make
    # impossible. Nothing about the list reaches the prompt or the protocol body
    # unredacted — _build_context passes it through blinded_view().
    seed = _randomization_seed(cfg, protocol_id, drug_inn, study_type)
    # Stored UNDERSCORE-PREFIXED, and the plain key a client may have supplied
    # is removed. _build_context copies every non-underscore population_config
    # key straight into the prompt, and the seed plus the recorded arguments
    # regenerate the allocation list entry for entry — publishing it would undo
    # blinded_view() completely. This is why the key is not "randomization_seed".
    cfg.pop("randomization_seed", None)
    cfg["_randomization_seed"] = seed
    n_enrolled = ss.get("n_enrolled")
    if isinstance(n_enrolled, int) and n_enrolled >= 2:
        cfg["_randomization"] = _safe_engine(
            "randomization",
            generate_allocation,
            design_key=design_key or "crossover",
            n_enrolled=n_enrolled,
            seed=seed,
            stratify_by=cfg.get("_stratification"),
            is_nti=is_nti,
            pd_coprimary=pd_coprimary,
            label=label,
            dosing=dosing,
            # CONFIRMED BUG: without these three, assess_blinding() (called
            # inside generate_allocation) never learns this is a biologic/
            # biosimilar_pk comparison, so it always falls through to its
            # open-label-dosing default — even for a biosimilar patient study,
            # which its OWN documented decision logic already says should get
            # blinded dosing. is_biologic/study_type are already computed
            # above for every other engine in this function; route mirrors
            # the same decision_values.get("route") lookup used elsewhere
            # here (e.g. the safety_gating call). immunogenicity_assessed is
            # deliberately left at its default (None): assess_blinding()
            # already derives it correctly from is_biologic/study_type.
            is_biologic=is_biologic,
            study_type=study_type,
            route=decision_values.get("route") or None,
        )

    # --- 10. CRF specification + Data Management Plan ----------------------
    # Consumes the sampling schedule (for the pre-printed PK sample log) and the
    # locked decisions (for the PD forms), so it runs after both. The context it
    # is handed deliberately EXCLUDES _randomization — nothing downstream of the
    # allocation list should be able to echo it.
    engine_ctx: dict[str, Any] = {
        "drug_inn": drug_inn,
        "study_type": study_type,
        "guideline_version": guideline_version,
        "sample_size": ss,
        "label_data": label or {},
        "pk_parameters": pk_list,
        "design_decisions": decisions,
        "population_config": {
            k: v for k, v in cfg.items() if k != "_randomization"
        },
    }
    cfg["_crf_dmp"] = _safe_engine(
        "crf_dmp",
        build_crf_spec,
        engine_ctx,
        design_key=design_key,
        study_type=study_type,
        sampling=sched,
        label=label,
        dosing=dosing,
        is_biologic=is_biologic,
        n_enrolled=ss.get("n_enrolled"),
        t_half_hours=t_half,
        lloq=cfg.get("lloq"),
        pd_endpoint=pd_value or None,
    )

    # --- 11. Informed Consent Form (separate IRB document) -----------------
    # Runs last: its blood-volume and duration arithmetic must be computed from
    # the SAME sampling schedule the protocol locks, and its prohibited-
    # medication list from the SAME DDI screen, or the two documents disagree.
    icf_ctx = dict(engine_ctx)
    if sched and sched.get("timepoints"):
        icf_ctx["sampling_schedule_hours"] = [
            tp["hours"] for tp in sched["timepoints"] if tp.get("hours") is not None
        ]
    ddi = cfg.get("_ddi")
    if ddi and ddi.get("prohibited") and not cfg.get("prohibited_medications"):
        icf_ctx["prohibited_medications"] = [
            str(row.get("label")) for row in ddi["prohibited"] if row.get("label")
        ]
    cfg["_icf"] = _safe_engine("icf", build_icf, icf_ctx)

    # Drop engines that failed so downstream `if cfg.get(...)` guards behave.
    for key in (
        "_sampling_schedule", "_ddi", "_safety_gating", "_bioanalytical",
        "_safety_reporting", "_randomization", "_crf_dmp", "_icf",
    ):
        if cfg.get(key) is None:
            cfg.pop(key, None)

    if label:
        cfg["_label"] = label
    if regulatory_sources:
        cfg["_regulatory_sources"] = regulatory_sources
    return cfg


def _build_context(
    drug: Drug,
    study_type: str,
    population_config: dict[str, Any],
    org_id: str,
    guideline_version: str,
    pk_params: list[PKParameterRecord],
) -> dict[str, Any]:
    pk_list = _pk_records_to_dicts(pk_params)
    ctx: dict[str, Any] = {
        "drug_inn": drug.inn,
        "brand_names": drug.brand_names,
        "study_type": study_type,
        # user_indication is free text: it reaches the prompt ONLY through the
        # framed patient_population_directive (quoted, labelled as data), never
        # as a raw population_config key.
        "population_config": {
            k: v for k, v in population_config.items()
            if not k.startswith("_") and k != "user_indication"
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
        # Co-primary alpha/power handling. Surfaced as its own context key so
        # multiplicity_directive() can reach it without the SAP prompt having to
        # dig into the sample-size dict — same pattern as _meta_analysis. Only
        # set when there is genuinely more than one co-primary endpoint, so a
        # single-endpoint design never carries a multiplicity discussion it
        # does not need.
        mult = rec.get("multiplicity") or {}
        if mult.get("n_co_primary", 0) > 1:
            ctx["multiplicity"] = mult
    # Locked stratification factors — the randomisation and population sections
    # narrate these; they never re-derive or re-rank them.
    strat = population_config.get("_stratification")
    if strat:
        ctx["stratification"] = strat
    # Regulatory-assessment-report evidence (services/literature_service.py) —
    # the SAP/study-rationale sections narrate what was actually referenced by
    # retrieved literature; they never claim a report this system did not read.
    reg_sources = population_config.get("_regulatory_sources")
    if reg_sources:
        ctx["regulatory_sources"] = reg_sources
    # Population-matched meta-analysis: the per-study table the appendix prints,
    # and the source of the powering CV. Locked, so the SAP narrates it rather
    # than re-deriving a different number.
    meta = population_config.get("_meta_analysis")
    if meta:
        ctx["meta_analysis"] = meta
    # The five locked clinical decisions. create_protocol also sets this
    # explicitly (same value); copying it here means a single-section
    # regeneration gets the same locked directive instead of losing it.
    decisions = population_config.get("_design_decisions")
    if decisions:
        ctx["design_decisions"] = decisions

    # ---- Deterministic engines: computed once at creation, narrated only ----
    # Each is stored under an underscore key (stripped from the prompt's
    # population_config above) and surfaced under the context key its own
    # directive expects. Guarded so an engine that could not run simply leaves
    # its key absent rather than injecting an empty object the model would try
    # to narrate.
    sched = population_config.get("_sampling_schedule")
    if sched:
        ctx["sampling_schedule"] = sched
    # Selected indication + the eligibility criteria that deliver it. Only set
    # for a patient biosimilar study — the engine returns applicable=False
    # otherwise, and an inapplicable result is dropped here so it can never
    # reach a healthy-volunteer prompt as an empty population instruction.
    patient_pop = population_config.get("_patient_population")
    if patient_pop and patient_pop.get("applicable"):
        ctx["patient_population"] = patient_pop
    # Inclusion/exclusion criteria precedent from real ClinicalTrials.gov
    # trials in the selected indication. Same applicability gate as
    # patient_population above — always True/False together, since this
    # engine needs the indication THAT one selects.
    trial_precedent = population_config.get("_trial_precedent")
    if trial_precedent and trial_precedent.get("applicable"):
        ctx["trial_precedent"] = trial_precedent
    # Doses to steady state + head-to-head comparative safety duration. Same
    # applicability gate: single-dose and non-biosimilar studies get nothing.
    comp_safety = population_config.get("_comparative_safety")
    if comp_safety and comp_safety.get("applicable"):
        ctx["comparative_safety"] = comp_safety
    ddi = population_config.get("_ddi")
    if ddi:
        ctx["ddi"] = ddi
    gating = population_config.get("_safety_gating")
    if gating:
        ctx["safety_gating"] = gating
    bioanalytical = population_config.get("_bioanalytical")
    if bioanalytical:
        ctx["bioanalytical"] = bioanalytical
    reporting = population_config.get("_safety_reporting")
    if reporting:
        ctx["safety_reporting"] = reporting
    crf = population_config.get("_crf_dmp")
    if crf:
        ctx["crf_dmp"] = crf
    icf = population_config.get("_icf")
    if icf:
        ctx["icf"] = icf

    # RANDOMIZATION — the single highest-risk line in this function. ctx is what
    # reaches the AI prompt and, through it, the protocol body. The raw stored
    # dict contains the full unblinded subject-to-treatment allocation AND the
    # seed that regenerates it; putting either in front of a blinded reader is
    # an allocation-concealment breach (ICH E9 sect. 2.3.2). blinded_view()
    # strips both, plus the block size that makes the last allocation of each
    # block deducible. Never pass the raw dict here.
    rand = population_config.get("_randomization")
    if rand:
        ctx["randomization"] = blinded_view(rand)

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


def diff_sections(
    old_sections: dict[str, Any],
    new_sections: dict[str, Any],
    from_version: int,
    to_version: int,
) -> list[SectionDiff]:
    """Section-by-section diff (plain text, HTML stripped) in SECTION_ORDER.

    Public so the chat edit-proposal service can reuse exactly the diff the
    version-compare route renders. ``_diff_sections`` is kept as an alias."""
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


# Backwards-compatible private alias (the name this function had before it was
# made public); existing importers keep working.
_diff_sections = diff_sections


# Protocol statuses that LOCK a protocol against content edits. Real
# ProtocolStatus values are draft / review / approved. Only `approved` is ever
# set by the code (POST /{id}/sign freezes the signed state and its SHA-256
# hash), so it is the one locked status. `review` is never assigned anywhere in
# the codebase and no route treats it as read-only, so it stays editable, like
# `draft`.
_LOCKED_PROTOCOL_STATUSES: frozenset[ProtocolStatus] = frozenset({ProtocolStatus.approved})


def require_protocol_editable(protocol: Protocol) -> None:
    """Raise 409 when the protocol is signed/approved: its content is frozen
    (21 CFR Part 11 — the e-signature binds to an exact document state), so
    neither a manual save nor an AI section regeneration may change it. To
    change a signed protocol, clone it into a new draft."""
    if protocol.status in _LOCKED_PROTOCOL_STATUSES:
        raise HTTPException(
            409,
            detail=(
                f"This protocol is signed (status '{protocol.status.value}') and locked: "
                "its sections can no longer be edited or regenerated. "
                "Clone it to start a new draft and make your changes there."
            ),
        )


def study_type_gate_detail(drug_inn: str, study_type: str) -> str | None:
    """The 422 message for a study type the drug's modality cannot run, or None
    when the study type is allowed (or the drug cannot be classified, in which
    case nothing is restricted). Single source of the wording for both the
    protocol-creation gate and the budget-creation gate."""
    permitted = allowed_study_types(drug_inn)
    if permitted is None or study_type in permitted:
        return None
    if permitted == ("biosimilar_pk",):
        return (
            f"'{drug_inn}' is a biologic and cannot run a '{study_type}' study; "
            f"allowed study type(s): {', '.join(permitted)}."
        )
    return (
        f"'{drug_inn}' is a small-molecule product and cannot run a "
        f"'{study_type}' study; allowed study type(s): {', '.join(permitted)}."
    )


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


async def _fetch_label_isolated(drug_inn: str) -> dict[str, str] | None:
    """DailyMed label for ``drug_inn`` through LiteratureService on its OWN
    short-lived session (``database.AsyncSessionLocal`` looked up at call time).

    LiteratureService's cache-miss path commits the session it was given. The
    preview facades are also called from places that hold their own session with
    pending state (the chat actions endpoint / a streaming turn), and a commit on
    THAT session mid-call would flush half-built rows and, for objects with
    relationships, can end in sqlalchemy.exc.MissingGreenlet (the budget
    precedent incident). A private session makes the facades side-effect free for
    the caller. Looked up on the ``database`` module (not name-imported) so the
    test fixture's patch of ``database.AsyncSessionLocal`` applies."""
    async with _database.AsyncSessionLocal() as label_db:
        return await _fetch_label_safe(LiteratureService(label_db), drug_inn)


async def compute_design_preview(
    db: AsyncSession, payload: DesignPreviewRequest
) -> dict[str, Any]:
    """Credit-free preview of the five study-design decisions (the body of
    POST /design-preview). Raises HTTPException(404) for an unknown drug.

    Uses the deterministic sample-size engine (for the parallel/crossover call),
    stored PK half-life, and the drug's real DailyMed label (route/device/
    injection-site: free NIH API, no AI cost) with the curated clinical
    knowledge base as fallback when the label can't be fetched or doesn't state
    something explicitly. Open data only: no org data is read."""
    drug = await db.get(Drug, payload.drug_id)
    if not drug:
        raise HTTPException(404, detail="Drug not found.")

    pk_result = await db.execute(
        select(PKParameterRecord).where(PKParameterRecord.drug_id == drug.id)
    )
    t_half = _median_t_half(pk_result.scalars().all())

    # Real label text grounds the route/device/site call — free NIH API, no AI
    # cost, and cached 7 days by LiteratureService so repeat previews are instant.
    label = await _fetch_label_isolated(drug.inn)
    label_text = " ".join(filter(None, [
        (label or {}).get("dosage_and_administration", ""),
        (label or {}).get("how_supplied", ""),
    ]))

    # design_key (parallel/crossover/replicate) is decided deterministically from
    # half-life + study type; use_r=False keeps it a pure-Python, R-independent call.
    cfg = payload.population_config or {}
    rec = recommend_sample_size(
        study_type=payload.study_type.value,
        drug_inn=drug.inn,
        t_half_hours=t_half,
        is_nti=cfg.get("is_nti"),
        requested_design=cfg.get("design"),
        use_r=False,
    )
    decisions = determine_design_decisions(
        drug_inn=drug.inn,
        study_type=payload.study_type.value,
        design_key=rec.design_key,
        t_half_hours=t_half,
        label_text=label_text,
        dosing=str(cfg.get("dosing") or "single"),
        subject_type=str(cfg.get("subject_type") or "healthy"),
        population_choice=payload.population_choice,
    )
    return {
        "drug_inn": drug.inn,
        "study_type": payload.study_type.value,
        "decisions": decisions_to_dicts(decisions),
    }


async def compute_indication_preview(
    db: AsyncSession, payload: IndicationPreviewRequest
) -> dict[str, Any]:
    """Credit-free ranking of the reference product's licensed indications for a
    patient biosimilar study (the body of POST /indication-preview), optionally
    checked against the user's own indication (already sanitised by
    IndicationPreviewRequest). Raises HTTPException(404) for an unknown drug."""
    drug = await db.get(Drug, payload.drug_id)
    if not drug:
        raise HTTPException(404, detail="Drug not found.")
    label = await _fetch_label_isolated(drug.inn)
    return select_patient_population(
        drug_inn=drug.inn,
        study_type=payload.study_type.value,
        subject_type="patient",
        label=label,
        meta_analysis=None,
        user_indication=payload.user_indication,
    )


async def compute_ddi_preview(
    db: AsyncSession, payload: DdiPreviewRequest
) -> dict[str, Any]:
    """Credit-free, drug-specific drug-drug-interaction screening (the body of
    POST /ddi-preview). Raises HTTPException(404) for an unknown drug.

    Runs the same ``screen_interactions`` engine, on the same inputs, that
    protocol generation runs at step 5 of ``_augment_config_with_design``, so the
    rows shown while the user builds the population are the rows the generated
    protocol will carry:
      * stored PK records (median half-life sets the post-dose restriction);
      * the drug's DailyMed label via the shared safe fetch (own session, see
        ``_fetch_label_isolated``), in the prompt-safe form generation uses;
      * the study design key from ``recommend_sample_size(use_r=False)``, a
        pure-Python call identical to design-preview's;
      * healthy-volunteer vs patient from the supplied ``subject_type``, with the
        engine's own population call adopted (upgrade-only) for biosimilar PK
        studies as generation does;
      * biologic modality from the INN classifier or a biosimilar PK study.
    Returns the engine's dict (``drug_inn`` filled in when the biologic path
    omits it), which is the shape the frontend's ``DdiScreeningResult`` type
    describes."""
    drug = await db.get(Drug, payload.drug_id)
    if not drug:
        raise HTTPException(404, detail="Drug not found.")

    study_type = payload.study_type.value
    pk_result = await db.execute(
        select(PKParameterRecord).where(PKParameterRecord.drug_id == drug.id)
    )
    pk_params = pk_result.scalars().all()
    t_half = _median_t_half(pk_params)

    label = await _fetch_label_isolated(drug.inn)

    cfg = dict(payload.population_config or {})
    # Same upgrade-only population call generation makes when no reviewer
    # decisions exist yet (biosimilar PK only; a no-op for BE / food effect).
    _engine_decision_prestep(cfg, drug.inn, study_type)

    rec = recommend_sample_size(
        study_type=study_type,
        drug_inn=drug.inn,
        t_half_hours=t_half,
        is_nti=cfg.get("is_nti"),
        requested_design=cfg.get("design"),
        use_r=False,
    )
    result = screen_interactions(
        _prompt_safe_label(label),
        _pk_records_to_dicts(pk_params),
        drug.inn,
        design_key=rec.design_key,
        is_nti=cfg.get("is_nti"),
        healthy_volunteers=str(cfg.get("subject_type") or "healthy").lower() != "patient",
        is_biologic=classify_drug_modality(drug.inn) == "biologic" or study_type == "biosimilar_pk",
    )
    # The small-molecule path echoes drug_inn; the biologic path does not. Make
    # the preview uniform so a client can always title the result.
    result.setdefault("drug_inn", drug.inn)
    return result


@router.post(
    "/design-preview",
    dependencies=[Depends(rate_limit(30, 60, scope="design_preview"))],
)
async def design_preview(
    payload: DesignPreviewRequest,
    db: AsyncSession = Depends(get_db),
):
    """Credit-free preview of the five study-design decisions for the 'Review
    design' wizard step (see ``compute_design_preview``). Open (like drug
    search): it returns clinical suggestions for a public drug, no org data."""
    return await compute_design_preview(db, payload)


@router.post(
    "/indication-preview",
    dependencies=[Depends(rate_limit(30, 60, scope="indication_preview"))],
)
async def indication_preview(
    payload: IndicationPreviewRequest,
    db: AsyncSession = Depends(get_db),
):
    """Credit-free preview of the patient-study indication for the 'Review
    design' step: ranks the reference product's licensed indications (real
    DailyMed label text, free NIH API) by the engine's disease-setting /
    monotherapy / PK-variability rules and, when the user typed their own
    indication, reports how it compares and what it costs in variability. Open
    (like design-preview): public drug data, no org data. The user's text passes
    through a server-side sanitiser and is returned as data only. See
    ``compute_indication_preview``."""
    return await compute_indication_preview(db, payload)


@router.post(
    "/ddi-preview",
    dependencies=[Depends(rate_limit(30, 60, scope="ddi_preview"))],
)
async def ddi_preview(
    payload: DdiPreviewRequest,
    db: AsyncSession = Depends(get_db),
):
    """Credit-free, drug-specific concomitant-medication screening for the
    Population step: which comedication classes are prohibited / restricted /
    permitted for THIS drug (from its real DailyMed label and stored PK), so the
    exclusion criteria are no longer the drug-agnostic defaults. Open (like
    design-preview): public drug data, no org data. See ``compute_ddi_preview``.
    Declared before every ``/{protocol_id}`` route so it is never shadowed."""
    return await compute_ddi_preview(db, payload)


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
    protocol_id: uuid.UUID | None = None,
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

    # Fold the reviewer's design/population overrides (from the "Review design"
    # step) into population_config BEFORE anything reads subject_type / dosing:
    # the trial-precedent fetch below (a reviewer who chose Patients after a
    # step-2 "healthy" would otherwise never get precedent) and the sizing in
    # _augment_config_with_design. Without reviewer decisions the engine's own
    # population/dose call is adopted instead (see _engine_decision_prestep).
    pop_cfg = dict(population_config)
    if design_decisions:
        design_decisions = reconcile_decision_set(
            design_decisions, drug_inn=drug.inn, study_type=study_type
        )
        apply_decision_overrides(pop_cfg, design_decisions)
    else:
        _engine_decision_prestep(pop_cfg, drug.inn, study_type)
    if (
        str(pop_cfg.get("subject_type") or "healthy").strip().lower() != "patient"
        or study_type != "biosimilar_pk"
    ):
        pop_cfg.pop("user_indication", None)

    trial_records = await _fetch_trial_precedent_safe(
        lit_svc, drug.inn,
        study_type=study_type,
        subject_type=str(pop_cfg.get("subject_type") or "healthy"),
    )

    # Deterministic plan of which regulatory assessment-report programmes
    # (FDA/EMA/TGA/PMDA/NMPA) are actually referenced by the literature just
    # retrieved. Built even when the regulatory-targeted second query did not
    # run (lit_svc.last_regulatory_status records why) so the SAP/study-
    # rationale sections can state honestly that no such evidence was
    # retrieved, rather than saying nothing at all.
    regulatory_plan = plan_regulatory_sources(
        drug_inn=drug.inn,
        records=pubmed_records,
        search_status=lit_svc.last_regulatory_status,
        query=lit_svc.last_regulatory_query,
        regulatory_query_pmids=lit_svc.last_regulatory_pmids,
    )

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
            regulatory_plan=regulatory_plan,
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
            guideline_version=guideline_version,
            # The five design decisions are finalized INSIDE the augment call
            # now (the reviewer's if supplied, else derived fresh from the
            # computed design), because several of the deterministic engines
            # run there have to agree with them.
            design_decisions=design_decisions,
            protocol_id=protocol_id,
            regulatory_sources=regulatory_plan,
            trial_records=trial_records,
        ),
        limiter=_DESIGN_THREAD_LIMITER,
    )
    decisions = augmented_config.get("_design_decisions") or []

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


def _merge_provenance(
    change_detail: dict[str, Any], provenance: dict[str, Any] | None
) -> dict[str, Any]:
    """Merge caller-supplied input provenance (e.g. ``{"source": "chat",
    "chat_session_id": ..., "spec_version": ..., "field_provenance": {...}}``)
    into the INITIAL snapshot's change_detail, so a Part 11 reviewer can see how
    each input was set. The pipeline's own keys (generated_sections, ai_*) always
    win on a name clash — provenance can add to the audit record but never
    overwrite what the pipeline recorded. Round-tripped through JSON (non-JSON
    values such as UUIDs become strings) so a careless caller cannot break the
    JSONB write."""
    if not provenance:
        return change_detail
    safe = json.loads(json.dumps(provenance, default=str))
    return {**safe, **change_detail}


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
    provenance: dict[str, Any] | None = None,
) -> None:
    """Background worker: run the pipeline against a NEW DB session (the
    request's session is long closed) and write the result onto the existing
    'generating' row. Never raises — a fatal/empty outcome or any unexpected
    error is recorded as generation_status='failed' so the client's poll ends.

    ``provenance`` (how the inputs were collected, e.g. a chat session) is merged
    into the INITIAL snapshot's change_detail — see _merge_provenance."""
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
                protocol_id=protocol_id,
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
                change_detail=_merge_provenance(change_detail, provenance),
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


async def validate_protocol_request(
    db: AsyncSession, actor: User, payload: ProtocolCreate
) -> Drug:
    """The gates a protocol-generation request must pass, in the order (and with
    the exact status codes and messages) the creation route has always applied:

      1. role: only the 'generator' (Author) role may draft a new protocol
         (separation of duties: the eventual approver should not also be the
         drafter) -> 403;
      2. the org's license must be active -> 402;
      3. the guideline version must be supported -> 422;
      4. the drug must exist -> 404;
      5. the biologic / small-molecule study-type gate: a biologic may only run
         a biosimilar PK study, a small molecule only BE / food effect -> 422.

    Returns the resolved Drug. Shared by POST /api/protocols/ and the chat
    generate action so both enforce identical gates."""
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

    gate_detail = study_type_gate_detail(drug.inn, payload.study_type.value)
    if gate_detail is not None:
        raise HTTPException(422, detail=gate_detail)
    return drug


async def start_protocol_generation(
    db: AsyncSession,
    request: Request | None,
    actor: User,
    payload: ProtocolCreate,
    *,
    provenance: dict[str, Any] | None = None,
) -> Protocol:
    """Validate the request (``validate_protocol_request``) and start a protocol
    generation: literature -> PK -> design -> sections.

    Real generation runs for minutes, so in production it runs in a BACKGROUND
    task: this returns a ``generation_status="generating"`` protocol immediately
    and the client polls GET /protocols/{id} until it flips to "complete" (open
    the editor) or "failed" (show generation_error). Under pytest it runs
    synchronously and returns the finished protocol, matching the mocked suite.

    ``provenance``, when given, is merged into the INITIAL version snapshot's
    change_detail on both paths (see ``_merge_provenance``). ``request`` is only
    used to hand the audit logger the new protocol's id; pass None when there is
    no HTTP request to annotate.
    """
    drug = await validate_protocol_request(db, actor, payload)

    # ---- Synchronous path (pytest): generate inline, return the finished row.
    if _generate_synchronously():
        # The id is minted BEFORE generation because the randomization engine
        # seeds its allocation list from it (see _randomization_seed): the seed
        # has to be the protocol's own, stable identifier, not a value invented
        # after the list already exists.
        new_protocol_id = uuid.uuid4()
        sections, augmented_config, change_detail = await _run_generation_pipeline(
            db,
            drug=drug,
            org_id=actor.org_id,
            study_type=payload.study_type.value,
            guideline_version=payload.guideline_version,
            population_config=payload.population_config,
            design_decisions=payload.design_decisions,
            protocol_id=new_protocol_id,
        )
        protocol = Protocol(
            id=new_protocol_id,
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
        if request is not None:
            request.state.audit_resource_id = str(protocol.id)
        _snapshot(
            db,
            protocol_id=protocol.id,
            version=1,
            sections=sections,
            changed_by=actor.id,
            change_type="INITIAL",
            change_detail=_merge_provenance(change_detail, provenance),
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
    if request is not None:
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
        provenance=provenance,
    )
    return protocol


@router.post("/", response_model=ProtocolRead, status_code=201, dependencies=[Depends(require_user)])
async def create_protocol(
    payload: ProtocolCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Start a protocol generation: literature -> PK -> design -> sections.

    Thin wrapper: authenticates the actor, then ``start_protocol_generation``
    enforces the gates (Author role, active license, supported guideline, drug
    exists, biologic / small-molecule study-type gate) and runs the generation
    (inline under pytest; a background task otherwise, with the client polling
    GET /protocols/{id} for generation_status).
    """
    actor = await _authenticate_actor(request, db)
    return await start_protocol_generation(db, request, actor, payload)


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
    protocol = await get_protocol_scoped(db, protocol_id, actor, load_drug=True)
    out = ProtocolRead.model_validate(protocol)
    if protocol.drug is not None:
        out.drug_inn = protocol.drug.inn
        out.drug_brand_names = list(protocol.drug.brand_names or [])
    return out


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
    # Signed/approved protocols are frozen: refuse BEFORE any AI credit is spent.
    require_protocol_editable(protocol)

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
    # Signed/approved protocols are frozen (the e-signature binds to exact content).
    require_protocol_editable(protocol)

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

    sections = diff_sections(old_sections, new_pv.sections, from_version, to_version)
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
