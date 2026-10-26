"""
Three-stage AI pipeline for FDA-compliant clinical trial protocol generation.

All three stages call Claude Sonnet (SONNET_MODEL below) — there is no
Haiku/Opus tiering; every real model call in this pipeline uses the same
model. The "three-stage" name refers to the pipeline's three functional
roles, not three different model tiers:

  Stage 1 (Sonnet) — classification & routing. JSON-only output. Gates the
                      rest of the pipeline on confidence >= CONFIDENCE_THRESHOLD.
  Stage 2 (Sonnet) — full protocol generation. Receives PRE-COMPUTED
                      deterministic facts (washout, sampling schedule,
                      sample size, TOST limits) from biostatistics.py and
                      must reuse them verbatim — the model reasons about
                      structure and regulatory language only, never
                      arithmetic.
  Stage 3 (Sonnet) — structured JSON validation/critique of Stage 2's
                      output. No narrative feedback.

Each stage is independently logged: model, input/output tokens, latency, and
status (success / halted / failed). Stage 2 only runs if Stage 1 succeeds with
confidence >= CONFIDENCE_THRESHOLD; Stage 3 only runs if Stage 2 returns
non-empty text.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import time
from typing import Any, Optional

import anthropic

from services.ai_service import AIService, _SECTION_CONFIGS, _expertise_for, _locked_design_directive
from services.guidelines import regulatory_directive
from services.design_decisions import design_directive as design_directive_fn
from services.biostatistics import recommend_sample_size
from services.protocol_qc import QCStatus, run_qc
from services.bioanalytical import bioanalytical_directive
from services.crf_dmp import crf_dmp_directive
from services.ddi_screening import ddi_directive
# NOTE: services.icf.icf_directive is deliberately NOT imported — the consent
# form's document control must not reach the protocol prompt. See
# _ENGINE_DIRECTIVES and _PROMPT_EXCLUDED_ENGINES below.
from services.randomization import randomization_directive
from services.safety_gating import safety_gating_directive
from services.safety_reporting import safety_reporting_directive
from services.sampling_schedule import sampling_schedule_directive
from services.stratification import stratification_directive
from services.literature_service import regulatory_sources_directive
from services.comparative_safety import comparative_safety_directive
from services.meta_analysis import meta_analysis_directive
from services.multiplicity import multiplicity_directive
from services.patient_population import patient_population_directive
from services.trial_precedent import trial_precedent_directive

SONNET_MODEL = "claude-sonnet-4-6"

CONFIDENCE_THRESHOLD = 0.85

# Output ceiling for the single call that writes the whole protocol. The model
# supports 128k output tokens; 64k leaves generous headroom over the ~34k a
# 17-section document needs while staying well inside that. Raising this is only
# safe because the call streams -- a non-streaming request this large would hit
# the SDK's HTTP timeout instead.
MAX_PROTOCOL_OUTPUT_TOKENS = 64000

TEMPLATE_IDS = {
    "small_molecule": "TEMPLATE_BE_SMALL_MOLECULE",
    "biologic": "TEMPLATE_PK_BIOLOGIC",
    "biosimilar": "TEMPLATE_BIOSIMILARITY_PK",
}

# Washout = 5 x terminal half-life (per pipeline spec). Used only when no
# half-life can be extracted in Stage 1 — clearly flagged as an assumption,
# mirroring biostatistics.py's ASSUMED_CV_PCT convention for missing CV.
_ASSUMED_T_HALF_HOURS = 24.0
_WASHOUT_MULTIPLE = 5


# ---------------------------------------------------------------------------
# Deterministic utilities — washout, sampling schedule, sample size.
# Never computed by a model; always injected into Stage 2 as facts.
# ---------------------------------------------------------------------------


def _default_sampling_schedule(t_half_hours: Optional[float]) -> list[float]:
    """PK blood-draw timepoints (hours post-dose), dense early then log-spaced
    out to 5 half-lives. Deterministic — never left to the model to invent."""
    core = [0.0, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0]
    horizon = _WASHOUT_MULTIPLE * (t_half_hours or _ASSUMED_T_HALF_HOURS)
    extra: list[float] = []
    t = 6.0
    while t < horizon:
        extra.append(round(t, 1))
        t *= 1.5
    extra.append(round(horizon, 1))
    return sorted(set(core + [x for x in extra if x > 4.0]))


# cv_source recorded when the caller hand-entered the within-subject CV. Kept
# byte-identical to routers/protocols.py::_CV_SOURCE_OVERRIDE so the two
# generation paths declare the same provenance for the same situation, and so
# services/risk_scoring.py's `startswith("override")` test fires on both.
_CV_SOURCE_OVERRIDE = "override (investigator-specified; not literature-derived)"


def _compute_deterministic_facts(classification: dict, user_params: dict) -> dict:
    """Single source of truth for every number Stage 2 is allowed to use.

    Merges PK values Stage 1 extracted from the raw query with any explicit
    user-supplied overrides, then computes washout, sampling schedule, sample
    size, and TOST limits via biostatistics.py (no SciPy, exact non-central t).

    ``cv_intra_pct`` IS overridable, but never silently: a hand-entered CV wins
    over whatever Stage 1 retrieved, and when it does, the recommendation's
    ``cv_source`` records that it was investigator-specified rather than
    literature-derived. The provenance is set in the SAME expression that
    applies the override, so the number and its declared source cannot diverge —
    the defect that caused this override to be removed once already. (Same rule
    as the protocol route — see routers/protocols.py::_resolve_powering_cv.)
    """
    pk = dict(classification.get("pk_params") or {})
    for key in ("cv_intra_pct", "t_half_hours", "tmax_hours", "is_nti"):
        if user_params.get(key) is not None:
            pk[key] = user_params[key]

    drug_type = classification.get("drug_type") or "small_molecule"
    study_type = "biosimilar_pk" if drug_type in ("biologic", "biosimilar") else "BE"

    # Provenance is decided here, from the same condition that decides the
    # value: user-supplied => override; anything else => let biostatistics label
    # it ("literature" when a value came from Stage 1, "assumed" when none did).
    cv_is_user_override = user_params.get("cv_intra_pct") is not None
    rec = recommend_sample_size(
        study_type=study_type,
        drug_inn=user_params.get("drug_inn", ""),
        cv_intra_pct=pk.get("cv_intra_pct"),
        cv_source=_CV_SOURCE_OVERRIDE if cv_is_user_override else None,
        t_half_hours=pk.get("t_half_hours"),
        is_nti=pk.get("is_nti"),
    )

    t_half = pk.get("t_half_hours")
    t_half_basis = "literature" if t_half else "assumed"
    t_half_used = float(t_half) if t_half else _ASSUMED_T_HALF_HOURS
    washout_hours = _WASHOUT_MULTIPLE * t_half_used
    washout_days = math.ceil(washout_hours / 24.0)

    sample_size = rec.to_dict()
    # Machine-readable provenance flag alongside the human-readable cv_source,
    # so a downstream consumer never has to string-match to tell a hand-entered
    # CV from a retrieved one.
    sample_size["cv_is_user_override"] = cv_is_user_override

    return {
        "t_half_hours": t_half_used,
        "t_half_basis": t_half_basis,
        "washout_hours": washout_hours,
        "washout_days": washout_days,
        "washout_rule": f"{_WASHOUT_MULTIPLE} x terminal half-life",
        "sampling_schedule_hours": _default_sampling_schedule(t_half),
        "sample_size": sample_size,
        "tost_limits": {
            "lower": rec.lower_limit,
            "upper": rec.upper_limit,
            "gmr": rec.gmr,
            "alpha": rec.alpha,
            "power": rec.power,
        },
    }


# ---------------------------------------------------------------------------
# Defensive JSON parsing — every model output is text, never trusted as-is.
# ---------------------------------------------------------------------------


def _parse_json_defensively(text: str) -> tuple[Optional[Any], Optional[str]]:
    """Strip markdown fences, json.loads in a try/except, fall back to pulling
    the largest {...}/[...] span. Returns (parsed_or_None, error_or_None)."""
    if not text or not text.strip():
        return None, "empty response"
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    cleaned = cleaned.strip()
    try:
        return json.loads(cleaned), None
    except json.JSONDecodeError as exc:
        match = re.search(r"(\{.*\}|\[.*\])", cleaned, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1)), None
            except json.JSONDecodeError:
                pass
        return None, f"JSON parse failed: {exc}"


def _extract_text(message: Any) -> str:
    return "".join(
        block.text for block in message.content if getattr(block, "type", None) == "text"
    )


def _log_entry(
    stage: str, model: str, status: str, *,
    input_tokens: int = 0, output_tokens: int = 0, latency_ms: float = 0.0,
    error: Optional[str] = None,
) -> dict:
    entry = {
        "stage": stage,
        "model": model,
        "status": status,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "latency_ms": round(latency_ms, 1),
    }
    if error:
        entry["error"] = error
    return entry


async def _call_model(
    client: anthropic.AsyncAnthropic, *, model: str, system: str,
    messages: list[dict], max_tokens: int,
) -> tuple[Any, float]:
    start = time.monotonic()
    message = await client.messages.create(
        model=model, max_tokens=max_tokens, system=system, messages=messages,
    )
    return message, (time.monotonic() - start) * 1000.0


async def _call_model_streamed(
    client: anthropic.AsyncAnthropic, *, model: str, system: str,
    messages: list[dict], max_tokens: int, thinking: Optional[dict] = None,
) -> tuple[Any, float]:
    start = time.monotonic()
    kwargs: dict[str, Any] = dict(model=model, max_tokens=max_tokens, system=system, messages=messages)
    if thinking:
        kwargs["thinking"] = thinking
    async with client.messages.stream(**kwargs) as stream:
        message = await stream.get_final_message()
    return message, (time.monotonic() - start) * 1000.0


# ---------------------------------------------------------------------------
# Stage 1 — Sonnet: classification & routing
# ---------------------------------------------------------------------------

_STAGE1_SYSTEM = (
    "You are a clinical-pharmacology classification engine for an FDA/ICH protocol "
    "generation pipeline. Output ONLY a single JSON object - no prose, no markdown "
    "fences, no explanation outside the JSON. "
    'Schema: {"drug_type": "small_molecule"|"biologic"|"biosimilar", '
    '"template_id": "TEMPLATE_BE_SMALL_MOLECULE"|"TEMPLATE_PK_BIOLOGIC"|"TEMPLATE_BIOSIMILARITY_PK", '
    '"pk_params": {"t_half_hours": number|null, "cv_intra_pct": number|null, '
    '"tmax_hours": number|null, "is_nti": boolean}, '
    '"confidence": number between 0 and 1, "halt_reason": string|null}. '
    "template_id must match drug_type: small_molecule -> TEMPLATE_BE_SMALL_MOLECULE, "
    "biologic -> TEMPLATE_PK_BIOLOGIC, biosimilar -> TEMPLATE_BIOSIMILARITY_PK. "
    "Extract any PK parameters explicitly stated in the query into pk_params; use "
    "null for anything not stated - do not invent values. Set confidence below "
    "0.85 and explain in halt_reason if the drug or study intent is ambiguous, "
    "unsupported, or you lack enough information to choose a template."
)


async def _stage1_classify(client: anthropic.AsyncAnthropic, query: str) -> tuple[dict, dict]:
    stage = "stage1_classify"
    try:
        message, latency_ms = await _call_model(
            client, model=SONNET_MODEL, system=_STAGE1_SYSTEM,
            messages=[{"role": "user", "content": query}], max_tokens=1024,
        )
    except Exception as exc:  # noqa: BLE001
        return (
            {"drug_type": None, "template_id": None, "pk_params": {}, "confidence": 0.0,
             "halt_reason": f"Stage 1 API call failed: {exc}"},
            _log_entry(stage, SONNET_MODEL, "failed", error=str(exc)),
        )

    text = _extract_text(message)
    parsed, err = _parse_json_defensively(text)
    usage = message.usage

    if not isinstance(parsed, dict):
        return (
            {"drug_type": None, "template_id": None, "pk_params": {}, "confidence": 0.0,
             "halt_reason": f"Stage 1 output could not be parsed as JSON: {err}"},
            _log_entry(stage, SONNET_MODEL, "failed",
                       input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
                       latency_ms=latency_ms, error=err),
        )

    parsed.setdefault("pk_params", {})
    parsed.setdefault("halt_reason", None)
    confidence = parsed.get("confidence") or 0.0
    if confidence < CONFIDENCE_THRESHOLD and not parsed.get("halt_reason"):
        parsed["halt_reason"] = f"confidence {confidence} below threshold {CONFIDENCE_THRESHOLD}"

    log = _log_entry(
        stage, SONNET_MODEL, "success",
        input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
        latency_ms=latency_ms,
    )
    return parsed, log


# ---------------------------------------------------------------------------
# Stage 2 — Sonnet: protocol generation
# ---------------------------------------------------------------------------

_STAGE2_SYSTEM = (
    "You are a regulatory medical writer generating an FDA/ICH E6(R3)-compliant "
    "clinical trial protocol. You are given a drug classification, a template ID, "
    "and a block of PRE-COMPUTED DETERMINISTIC FACTS (sample size, washout, "
    "sampling schedule, statistical limits) produced by a validated biostatistics "
    "engine. Use those facts EXACTLY as given - do not recalculate them, round "
    "them differently, or contradict them anywhere in the document. Your job is "
    "regulatory structure, language, and completeness, not arithmetic. "
    "Write the full protocol as plain text with clear numbered section headers "
    "(e.g. '1. INTRODUCTION', '2. OBJECTIVES', ...), covering at minimum: Title, "
    "Synopsis, Introduction, Objectives, Study Design, Population, Treatment, "
    "Study Assessments, Safety Monitoring, Statistical Analysis Plan, Ethics, "
    "References. Do not leave bracketed placeholders unfilled - use the given "
    "facts, or clearly-labelled standard regulatory boilerplate, instead."
)


async def _stage2_generate(
    client: anthropic.AsyncAnthropic, query: str, classification: dict,
    user_params: dict, facts: dict,
) -> tuple[str, dict]:
    stage = "stage2_generate"
    prompt = (
        f"ORIGINAL REQUEST:\n{query}\n\n"
        f"CLASSIFICATION (Stage 1):\n{json.dumps(classification, indent=2)}\n\n"
        f"USER-SUPPLIED PARAMETERS:\n{json.dumps(user_params, indent=2)}\n\n"
        "PRE-COMPUTED DETERMINISTIC FACTS (use verbatim, do not recompute):\n"
        f"{json.dumps(facts, indent=2)}\n\n"
        "Generate the full protocol now."
    )
    try:
        message, latency_ms = await _call_model_streamed(
            client, model=SONNET_MODEL, system=_STAGE2_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=16000, thinking={"type": "adaptive"},
        )
    except Exception as exc:  # noqa: BLE001
        return "", _log_entry(stage, SONNET_MODEL, "failed", error=str(exc))

    text = _extract_text(message)
    usage = message.usage
    log = _log_entry(
        stage, SONNET_MODEL, "success" if text.strip() else "failed",
        input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
        latency_ms=latency_ms,
    )
    return text, log


# ---------------------------------------------------------------------------
# Stage 3 — Sonnet: structured validation
# ---------------------------------------------------------------------------

_STAGE3_SYSTEM = (
    "You are a regulatory QC validator. Run a structured validation pass over a "
    "generated clinical trial protocol. Output ONLY a JSON array - no prose, no "
    "markdown fences. Each element: "
    '{"check_id": string, "section": string, "status": "pass"|"fail"|"warning", '
    '"detail": string}. '
    "Required checks (each its own array element): template-drug type match; all "
    "required ICH E6(R3) sections present; no unfilled placeholders; sampling "
    "schedule plausibility; statistical framework consistency; washout period "
    "correctness; jurisdiction flags if non-FDA parameters are detected. Add "
    "further checks if warranted by what you see in the text."
)


async def _stage3_validate(
    client: anthropic.AsyncAnthropic, protocol_text: str, classification: dict,
    facts: dict,
) -> tuple[list[dict], dict]:
    stage = "stage3_validate"
    prompt = (
        f"CLASSIFICATION:\n{json.dumps(classification, indent=2)}\n\n"
        f"DETERMINISTIC FACTS USED IN GENERATION:\n{json.dumps(facts, indent=2)}\n\n"
        f"PROTOCOL TEXT:\n{protocol_text}\n\n"
        "Run the validation checks now."
    )
    try:
        # Thinking is intentionally OFF for this stage. With adaptive thinking on,
        # Sonnet spent the ENTIRE token budget reasoning about the large (~42 KB)
        # protocol and never emitted a text block (output_tokens hit max_tokens
        # exactly, text empty). Structured JSON validation is an extraction-shaped
        # task that emits the array directly without extended thinking.
        message, latency_ms = await _call_model_streamed(
            client, model=SONNET_MODEL, system=_STAGE3_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=8000,
        )
    except Exception as exc:  # noqa: BLE001
        return [], _log_entry(stage, SONNET_MODEL, "failed", error=str(exc))

    text = _extract_text(message)
    parsed, err = _parse_json_defensively(text)
    usage = message.usage
    status = "success" if isinstance(parsed, list) else "failed"
    log = _log_entry(
        stage, SONNET_MODEL, status,
        input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
        latency_ms=latency_ms, error=err if status == "failed" else None,
    )
    return (parsed if isinstance(parsed, list) else []), log


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


async def run_pipeline(query: str, user_params: Optional[dict] = None) -> dict:
    """Run all three stages with gating. Returns
    {classification, protocol, validation, pipeline_log}."""
    user_params = user_params or {}
    client = anthropic.AsyncAnthropic(
        api_key=os.getenv("ANTHROPIC_API_KEY"), max_retries=4,
    )
    pipeline_log: list[dict] = []

    classification, log1 = await _stage1_classify(client, query)
    pipeline_log.append(log1)

    protocol_text = ""
    validation: list[dict] = []

    stage1_ok = log1["status"] == "success" and not classification.get("halt_reason")
    if stage1_ok and classification.get("confidence", 0.0) >= CONFIDENCE_THRESHOLD:
        facts = _compute_deterministic_facts(classification, user_params)
        protocol_text, log2 = await _stage2_generate(
            client, query, classification, user_params, facts,
        )
        pipeline_log.append(log2)

        if log2["status"] == "success" and protocol_text.strip():
            validation, log3 = await _stage3_validate(
                client, protocol_text, classification, facts,
            )
            pipeline_log.append(log3)

    return {
        "classification": classification,
        "protocol": protocol_text,
        "validation": validation,
        "pipeline_log": pipeline_log,
    }


# ---------------------------------------------------------------------------
# Integrated flow — same three models, wired into the existing per-section
# protocol data model (routers/protocols.py::create_protocol). Distinct from
# run_pipeline() above: study_type and the drug are already known (selected
# in the UI), context/deterministic facts are already built by
# protocols.py::_build_context() + _augment_config_with_design(), and the
# output must be a {section_name: html} dict matching SECTION_ORDER exactly —
# not a single free-text document — so the rest of the app (editor, QC,
# coverage, export) needs zero changes.
# ---------------------------------------------------------------------------

_STAGE1_SANITY_SYSTEM = (
    "You are a clinical-pharmacology sanity-check classifier for a protocol-generation "
    "pipeline. The study_type was ALREADY chosen by the user. Your ONLY job is to "
    "confirm the drug's MOLECULE CLASS is compatible with that study_type, and flag "
    "ONLY a hard molecule-class mismatch. You are not the source of truth, and you do "
    "NOT review study design, regulatory strategy, originator-vs-copy status, "
    "literature, or data. "
    "\n\nThe rules are simple and absolute:\n"
    "- study_type 'BE' or 'food_effect' => expects a SMALL MOLECULE. Halt ONLY if the "
    "drug is clearly a biologic (a monoclonal antibody, fusion protein, peptide >~40 "
    "amino acids, or other large protein).\n"
    "- study_type 'biosimilar_pk' => expects a BIOLOGIC. The named drug is the "
    "REFERENCE PRODUCT for the similarity comparison. It is ALWAYS the originator / "
    "innovator biologic (e.g. dulaglutide/Trulicity, adalimumab/Humira, "
    "trastuzumab/Herceptin). Naming an originator biologic here is CORRECT and EXPECTED "
    "- that is exactly what a biosimilar PK study compares against. NEVER halt on the "
    "grounds that the drug 'is the originator, not a biosimilar' or 'is the reference, "
    "not a copy' - that is the intended, valid input, not a mismatch. Halt ONLY if the "
    "drug is clearly a SMALL MOLECULE (e.g. ibuprofen, metformin) paired with "
    "'biosimilar_pk'.\n\n"
    "Output ONLY a JSON object, no prose, no markdown fences: "
    '{"drug_type": "small_molecule"|"biologic"|"biosimilar", "confidence": number 0-1, '
    '"halt_reason": string|null}. Set confidence below 0.85 with a halt_reason ONLY for '
    "a hard molecule-class mismatch as defined above. In EVERY other case - including "
    "any originator biologic with 'biosimilar_pk' - return confidence >= 0.9 and "
    "halt_reason null. This check passes for the overwhelming majority of real requests."
)


async def _stage1_sanity_check(
    client: anthropic.AsyncAnthropic, drug_inn: str, study_type: str,
) -> tuple[dict, dict]:
    stage = "stage1_classify"
    user_content = json.dumps({"drug_inn": drug_inn, "study_type": study_type})
    try:
        message, latency_ms = await _call_model(
            client, model=SONNET_MODEL, system=_STAGE1_SANITY_SYSTEM,
            messages=[{"role": "user", "content": user_content}], max_tokens=512,
        )
    except Exception as exc:  # noqa: BLE001
        return (
            {"drug_type": None, "confidence": 0.0, "halt_reason": f"Stage 1 API call failed: {exc}"},
            _log_entry(stage, SONNET_MODEL, "failed", error=str(exc)),
        )

    text = _extract_text(message)
    parsed, err = _parse_json_defensively(text)
    usage = message.usage

    if not isinstance(parsed, dict):
        return (
            {"drug_type": None, "confidence": 0.0,
             "halt_reason": f"Stage 1 output could not be parsed as JSON: {err}"},
            _log_entry(stage, SONNET_MODEL, "failed",
                       input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
                       latency_ms=latency_ms, error=err),
        )

    parsed.setdefault("halt_reason", None)
    confidence = parsed.get("confidence") or 0.0
    if confidence < CONFIDENCE_THRESHOLD and not parsed.get("halt_reason"):
        parsed["halt_reason"] = f"confidence {confidence} below threshold {CONFIDENCE_THRESHOLD}"

    return parsed, _log_entry(
        stage, SONNET_MODEL, "success",
        input_tokens=usage.input_tokens, output_tokens=usage.output_tokens, latency_ms=latency_ms,
    )


def _field_description(name: str) -> str:
    base = f"HTML body for the '{name.replace('_', ' ').title()}' section. "
    if name == "ethics":
        # _SECTION_CONFIGS["ethics"]["instructions"] (reused verbatim in this
        # path's instructions block - see _stage2_generate_all_sections) tells
        # the model to write its four required subsections using <h2>
        # headings. The generic "<h3> as needed" note below is right for
        # every OTHER section, but for "ethics" specifically it directly
        # contradicted that instruction - a genuine, prompt-driven source of
        # mixed <h2>/<h3> subsection markup within one generated Ethics
        # section, which could make _split_informed_consent
        # (routers/export.py) misfile a trailing subsection (e.g. Participant
        # and Data Confidentiality) into the wrong appendix. State the
        # correct level explicitly here so the two instructions agree.
        return (
            base + "Use <p>, <h2> for its four required subsection headings, "
            "<ul>/<li>, <table> as needed. Body content only - no outer "
            "section title, number, or <html>/<body> tags."
        )
    return (
        base + "Use <p>, <h3>, <ul>/<li>, <table> as needed. Body content only - "
        "no outer section title, number, or <html>/<body> tags."
    )


def _build_full_protocol_tool(section_order: list[str], *, required: Optional[list[str]] = None) -> dict:
    """``required`` defaults to every section. The parallel writer passes ``[]``:
    all of its requests must send a byte-identical tool so they share one prompt
    cache, and each request is told in its own text which fields to fill (the
    returned sections are then checked group by group)."""
    return {
        "name": "write_protocol",
        "description": (
            "Write clinical trial protocol sections. Provide one field per requested "
            "section, each containing that section's HTML body content."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                name: {"type": "string", "description": _field_description(name)}
                for name in section_order
            },
            "required": list(section_order) if required is None else list(required),
        },
    }


# The deterministic engines computed by routers/protocols.py and copied into the
# generation context, paired with the directive each one ships.
#
# Their RAW payloads must not be pasted into this prompt. They are reference
# data structures (form catalogues, per-subject enrolment logs, 20-section ICF
# bodies), and together they serialise to roughly 480 KB - about 655 KB once
# indented, which is more context than the model has, for a call that then has
# to emit a ~40 KB document. Each engine's own directive() renders the SAME
# locked facts as compact instruction text: ~126 KB for all eight, a 74%
# reduction, and in the form the model is meant to consume.
#
# Order is the order they appear in the prompt; it follows the protocol's own
# structure (design -> conduct -> safety -> data -> consent).
_ENGINE_DIRECTIVES: tuple[tuple[str, Any], ...] = (
    ("sampling_schedule", sampling_schedule_directive),
    ("randomization", randomization_directive),
    ("ddi", ddi_directive),
    # Same locked facts services/ai_service.py wires into "population" and
    # "treatment" for the per-section path — a whole-document generation must
    # get the identical modality prohibition (no non-smoker/genotype criterion
    # invented for a biologic), or this single-request path silently loses the
    # protection the per-section path has.
    ("stratification", stratification_directive),
    ("bioanalytical", bioanalytical_directive),
    ("safety_gating", safety_gating_directive),
    ("safety_reporting", safety_reporting_directive),
    ("crf_dmp", crf_dmp_directive),
    # The per-study meta-analysis table AND the CV the study is powered on
    # come from one engine call, so the appendix and the sample-size
    # justification cannot state different numbers — mirrors
    # services/ai_service.py's _ENGINE_DIRECTIVE_PLAN["statistical_analysis_plan"]
    # wiring exactly. MUST be here: this tuple is this path's only source of
    # the meta-analysis directive (the whole-document path does not also
    # render ai_service.py's plan), so omitting it does not fall back to "no
    # meta-analysis" — it falls back to the raw, un-narrated dict reaching the
    # model as unlocked JSON context instead (see _stage2_generate_all_sections
    # below: any key NOT rendered here is serialised into the context block
    # verbatim), losing the CV-basis / derived-from-mean-SD disclosure this
    # directive exists to state.
    ("meta_analysis", meta_analysis_directive),
    # Same three additions the per-section path carries in its own
    # _ENGINE_DIRECTIVE_PLAN. This tuple is an INDEPENDENT copy of that plan, so
    # an engine added there and not here does NOT get suppressed here — it gets
    # serialised into the raw JSON context block instead, losing its locked
    # narration. That exact drift already happened once with meta_analysis.
    #   patient_population - the selected indication + the naive/con-med
    #     eligibility criteria that deliver it (patient biosimilar only).
    #   trial_precedent - real ClinicalTrials.gov prior-trial precedent for
    #     the Inclusion/Exclusion Criteria lists, for the SAME selected
    #     indication (patient biosimilar only).
    #   comparative_safety - doses to steady state, and the head-to-head
    #     safety/immunogenicity period that outlives the primary PK readout.
    #   multiplicity - co-primary alpha (unadjusted, with the reason) and the
    #     per-endpoint power needed to hold OVERALL power at target.
    ("patient_population", patient_population_directive),
    ("trial_precedent", trial_precedent_directive),
    ("comparative_safety", comparative_safety_directive),
    ("multiplicity", multiplicity_directive),
    # NO ("icf", icf_directive) HERE, DELIBERATELY — must stay in lockstep with
    # services/ai_service.py's _ENGINE_DIRECTIVE_PLAN["ethics"], which drops it
    # for the same reason: the consent form's document control (version, date,
    # regulatory basis, element coverage, pending site-specific fields) belongs
    # to the form, not to the protocol. This tuple is an INDEPENDENT copy of
    # that plan for the whole-document path, so leaving the directive here
    # would let a single-request generation reintroduce exactly the ICF
    # metadata the per-section path now excludes. icf_directive is still used
    # to build the standalone consent document.
    # Regulatory-assessment-report evidence (services/literature_service.py) —
    # mirrors the ai_service.py wiring into "statistical_analysis_plan" /
    # "study_rationale".
    ("regulatory_sources", regulatory_sources_directive),
)


# Engine results that must reach the protocol prompt in NO form — neither as a
# rendered directive nor as raw JSON in the serialised context block.
#
# The context block carries every key whose directive was not rendered (see
# _stage2_generate_all_sections), so simply dropping an engine from
# _ENGINE_DIRECTIVES does the OPPOSITE of suppressing it: the full raw result
# gets serialised instead. `icf` is dropped above deliberately, so it has to be
# named here too or the consent form's version, element coverage and pending
# site-specific fields would reach the model as JSON — the exact content the
# removal exists to keep out of the protocol. The result is still computed and
# stored (population_config["_icf"]) and still builds the standalone consent
# document; it just never reaches the protocol's own prompt.
_PROMPT_EXCLUDED_ENGINES: frozenset[str] = frozenset({"icf"})


def _first_line(text: str) -> str:
    """First non-blank line of a directive - used as its identity marker."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _engine_directives(context: dict, already_present: str) -> tuple[str, set[str]]:
    """Render the computed engines' locked-fact directives for the prompt.

    Returns ``(directive_text, keys_rendered)``. A directive whose header line
    is ALREADY present in ``already_present`` is skipped: services/ai_service.py
    may inject the same engine directives from its own locked-facts block, and
    emitting a 40 KB directive twice wastes budget for no gain. Skipping on the
    header rather than the whole string keeps the check cheap and stable if the
    other block reformats the body. Either way the key is reported as rendered,
    so its raw payload is dropped from the JSON block exactly once.
    """
    parts: list[str] = []
    rendered: set[str] = set()
    for key, fn in _ENGINE_DIRECTIVES:
        payload = context.get(key)
        if not payload:
            continue
        try:
            text = fn(payload)
        except Exception:  # noqa: BLE001 - a directive must never abort generation
            continue
        if not text or not text.strip():
            continue
        rendered.add(key)
        marker = _first_line(text)
        if marker and marker in already_present:
            continue
        parts.append(text)
    return ("\n".join(parts), rendered)


#: How many write requests run at once (env ``GENERATION_PARALLELISM``). One
#: request writing all ~50k output tokens took ~17 minutes in a real run
#: (2026-10-25); the model writes at a steady rate, so the slowest group sets the
#: wall-clock time. Lower it if the organisation's output-tokens-per-minute rate
#: limit is small (429s are retried by the SDK, but they cost time).
DEFAULT_GENERATION_PARALLELISM = 6

#: Relative output size of each section (HTML characters in a real 17-section
#: protocol, 2026-10-25). Only used to balance the groups; unknown sections get
#: the median.
_SECTION_WEIGHTS: dict[str, int] = {
    "title": 350, "synopsis": 8000, "introduction": 9400, "study_rationale": 7300,
    "objectives": 1900, "endpoints": 4300, "study_design": 6900, "population": 20300,
    "treatment": 20000, "study_assessments": 22500, "safety_monitoring": 33000,
    "data_handling": 8500, "study_monitoring": 6200, "statistical_analysis_plan": 26300,
    "ethics": 6600, "administrative": 7200, "references": 7900,
}

#: How long the coordinator waits for the first request to start answering (its
#: prompt is then cached) before launching the rest regardless.
_FIRST_TOKEN_WAIT_SECONDS = 120.0


def generation_parallelism() -> int:
    try:
        return max(1, int(os.environ.get("GENERATION_PARALLELISM", DEFAULT_GENERATION_PARALLELISM)))
    except ValueError:
        return DEFAULT_GENERATION_PARALLELISM


def _partition_sections(section_order: list[str], groups: int) -> list[list[str]]:
    """Split the sections into at most ``groups`` CONTIGUOUS runs with the smallest
    possible largest run (by _SECTION_WEIGHTS). Contiguous keeps related sections
    (objectives / endpoints / design) in one request, so they are written together."""
    names = list(section_order)
    if not names:
        return []
    k = max(1, min(groups, len(names)))
    known = sorted(_SECTION_WEIGHTS.values())
    median = known[len(known) // 2]
    w = [_SECTION_WEIGHTS.get(n, median) for n in names]

    def runs_needed(cap: int) -> int:
        count, current = 1, 0
        for x in w:
            if current + x > cap:
                count, current = count + 1, x
            else:
                current += x
        return count

    lo, hi = max(w), sum(w)
    while lo < hi:  # smallest cap that fits in k runs
        mid = (lo + hi) // 2
        if runs_needed(mid) <= k:
            hi = mid
        else:
            lo = mid + 1
    out: list[list[str]] = [[]]
    current = 0
    for name, x in zip(names, w):
        if out[-1] and current + x > lo:
            out.append([])
            current = 0
        out[-1].append(name)
        current += x
    return out


def _stage2_prompt(study_type: str, context: dict, all_sections: list[str], *, required: Optional[list[str]] = None) -> tuple[str, str, dict]:
    """(system, shared prompt text, tool) for writing any subset of ``all_sections``.

    Everything here is byte-identical for every request about one protocol - the
    first generation pass, its parallel groups and the QC-remediation requests -
    so they share one prompt cache. What differs per request (which sections to
    write, remediation feedback) goes in a second text block after the cache
    breakpoint (see _stage2_call)."""
    guideline_version = context.get("guideline_version")
    expertise, protocol_type = _expertise_for(study_type, guideline_version)
    design_directive = _locked_design_directive(context.get("sample_size"))
    reg_directive = regulatory_directive(guideline_version or "", study_type)
    decisions_directive = design_directive_fn(context.get("design_decisions") or [])

    # Computed engines travel as their own directives, not as raw JSON (see
    # _ENGINE_DIRECTIVES), and are then dropped from the serialised context so
    # the same facts are not carried twice in two different shapes - which is
    # also how a model ends up reconciling two renderings of one number.
    engine_directives, rendered = _engine_directives(context, design_directive + decisions_directive)
    context_block = json.dumps(
        {
            k: v for k, v in context.items()
            if k not in rendered and k not in _PROMPT_EXCLUDED_ENGINES
        },
        indent=2, default=str,
    )

    instructions_block = "\n\n".join(
        f"### Section: {name} ###\n"
        f"Regulatory reference: {cfg['fda_ref']}\n"
        f"Instructions: {cfg['instructions']}"
        for name, cfg in _SECTION_CONFIGS.items()
        if name in all_sections
    )

    shared = (
        f"You are writing the {protocol_type} study protocol by calling write_protocol, "
        f"one field per section. Each request names the sections to write now; the "
        f"protocol's sections are: {', '.join(all_sections)}.\n\n"
        f"{design_directive}\n"
        f"{reg_directive}\n"
        f"{decisions_directive}\n"
        f"{engine_directives}\n"
        f"Context:\n{context_block}\n\n"
        f"Per-section instructions:\n\n{instructions_block}\n\n"
        "For every section, write body text only - no section title, number, or "
        "meta-commentary inside the HTML; that is added separately when the document "
        "is exported."
    )
    return expertise, shared, _build_full_protocol_tool(all_sections, required=required)


def _group_request_text(group: list[str], all_sections: list[str], extra: str = "") -> str:
    if list(group) == list(all_sections):
        text = (
            "Write the COMPLETE protocol now: call write_protocol with every section "
            f"listed above ({', '.join(group)})."
        )
    else:
        text = (
            f"Write ONLY these sections now: {', '.join(group)}. Call write_protocol with "
            "exactly these fields and no others. The remaining sections are written in "
            "separate requests from the same facts: keep terminology and every number "
            "identical to the facts above, refer to other sections by name where useful, "
            "and do not repeat their content."
        )
    return text + (f"\n\n{extra}" if extra else "")


def _usage_dict(usage: Any) -> dict[str, int]:
    return {
        key: int(getattr(usage, key, 0) or 0)
        for key in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    }


async def _stage2_call(
    client: anthropic.AsyncAnthropic, *, system: str, shared: str, tool: dict,
    group: list[str], request_text: str, on_first_event: Any = None,
) -> tuple[dict[str, str], Optional[str], dict[str, int], Optional[str]]:
    """One streamed write request for ``group``.

    Returns (sections, stop_reason, usage, error). ``sections`` holds only the
    group's fields, stripped; anything else the model wrote is ignored.
    ``on_first_event`` is called once the response starts streaming - from that
    moment the prompt prefix is cached and other requests can read it."""
    params = dict(
        model=SONNET_MODEL,
        # One request may emit the whole protocol as fields of a single tool
        # call, so the ceiling has to cover the whole document at once. At 20000
        # this truncated mid-tool-call and returned ~10 of 17 sections: each of
        # the nine deterministic engines injects locked facts the model must
        # render as tables (sampling schedule, DDI list, safety gating, the BMV
        # validation/stability/handling set, CRF spec, DMP, meta-analysis
        # appendix). The model supports 128k output; streaming (used here) is
        # what makes a ceiling this large safe from HTTP timeouts.
        max_tokens=MAX_PROTOCOL_OUTPUT_TOKENS,
        system=system,
        tools=[tool],
        tool_choice={"type": "tool", "name": "write_protocol"},
        messages=[{
            "role": "user",
            "content": [
                # Breakpoint on the shared block: tools + system + this block are
                # cached and read at ~0.1x by every other request for this protocol.
                {"type": "text", "text": shared, "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": request_text},
            ],
        }],
    )
    try:
        async with client.messages.stream(**params) as stream:
            if on_first_event is not None:
                signalled = False
                async for _event in stream:
                    if not signalled:
                        signalled = True
                        on_first_event()
            message = await stream.get_final_message()
    except Exception as exc:  # noqa: BLE001
        return {}, None, {}, str(exc)
    finally:
        if on_first_event is not None:
            on_first_event()  # never leave the coordinator waiting on a failed request

    sections: dict[str, str] = {}
    for block in message.content:
        if (
            getattr(block, "type", None) == "tool_use"
            and block.name == "write_protocol"
            and isinstance(block.input, dict)
        ):
            sections = {name: (block.input.get(name) or "").strip() for name in group}
            break
    return sections, getattr(message, "stop_reason", None), _usage_dict(message.usage), None


async def _stage2_generate_groups(
    client: anthropic.AsyncAnthropic, study_type: str, context: dict, all_sections: list[str],
    groups: list[list[str]], *, extras: Optional[list[str]] = None, stage: str = "stage2_generate",
    required: Optional[list[str]] = None,
) -> tuple[dict[str, str], dict]:
    """Write ``groups`` (lists of section names) in parallel over one shared, cached
    prompt, and merge them. A truncated or incomplete group fails the WHOLE stage
    (a silently-incomplete regulated document is the worst failure mode); a group
    that errors or comes back incomplete is retried once on its own first."""
    system, shared, tool = _stage2_prompt(study_type, context, all_sections, required=required)
    extras = extras or [""] * len(groups)
    texts = [_group_request_text(g, all_sections, x) for g, x in zip(groups, extras)]
    sem = asyncio.Semaphore(generation_parallelism())
    first_started = asyncio.Event()

    async def run(i: int, *, signal: bool = False):
        async with sem:
            return await _stage2_call(
                client, system=system, shared=shared, tool=tool, group=groups[i],
                request_text=texts[i], on_first_event=first_started.set if signal else None,
            )

    start = time.monotonic()
    if len(groups) == 1:
        results = [await run(0)]
    else:
        # Fan-out: the cache is readable only once the first response streams, so
        # start one request, wait for its first event, then launch the rest.
        first = asyncio.create_task(run(0, signal=True))
        try:
            await asyncio.wait_for(first_started.wait(), timeout=_FIRST_TOKEN_WAIT_SECONDS)
        except asyncio.TimeoutError:
            pass
        rest = [asyncio.create_task(run(i)) for i in range(1, len(groups))]
        results = [await first, *(await asyncio.gather(*rest))]

    def incomplete(i: int, result) -> bool:
        sections, stop_reason, _usage, error = result
        return bool(error) or stop_reason == "max_tokens" or any(not sections.get(n) for n in groups[i])

    totals: dict[str, int] = {}
    retried: list[int] = []
    for i, result in enumerate(results):
        if len(groups) > 1 and incomplete(i, result):
            retried.append(i)
            for key, value in result[2].items():
                totals[key] = totals.get(key, 0) + value
            results[i] = await run(i)
    latency_ms = (time.monotonic() - start) * 1000.0
    for result in results:
        for key, value in result[2].items():
            totals[key] = totals.get(key, 0) + value

    def entry(status: str, error: Optional[str] = None) -> dict:
        log = _log_entry(
            stage, SONNET_MODEL, status, error=error,
            input_tokens=totals.get("input_tokens", 0), output_tokens=totals.get("output_tokens", 0),
            latency_ms=latency_ms,
        )
        log["cache_read_input_tokens"] = totals.get("cache_read_input_tokens", 0)
        log["cache_creation_input_tokens"] = totals.get("cache_creation_input_tokens", 0)
        log["parallel_groups"] = len(groups)
        if retried:
            log["groups_retried"] = len(retried)
        return log

    errors = [r[3] for r in results if r[3]]
    merged: dict[str, str] = {}
    for result in results:
        merged.update(result[0])
    wanted = [n for g in groups for n in g]
    if errors and not any(merged.get(n) for n in wanted):
        return {}, entry("failed", errors[0])

    # A truncated generation MUST NOT be reported as success. Missing fields of
    # the tool call arrive as empty strings, so the dict is still fully-keyed and
    # truthy -- which is exactly how a run that produced 10 of 17 sections was
    # logged "success", persisted, and shown to the user as a finished protocol
    # with no error anywhere. For a regulated document a silently-incomplete
    # output is the worst failure mode available, so both signals are checked:
    # the API's own stop_reason, and the sections that actually came back.
    empty = [n for n in wanted if not merged.get(n)]
    truncated = any(r[1] == "max_tokens" for r in results)
    if errors or truncated or empty:
        reason = (
            f"Generation was cut off at the {MAX_PROTOCOL_OUTPUT_TOKENS}-token output ceiling"
            if truncated else
            "Generation returned an incomplete protocol"
        )
        detail = (
            f"{reason}: {len(wanted) - len(empty)} of {len(wanted)} sections "
            f"were produced. Missing: {', '.join(empty) or 'none'}. "
            f"Output tokens used: {totals.get('output_tokens', 0)}."
            + (f" Request error: {errors[0]}" if errors else "")
        )
        return {}, entry("failed", detail)

    return {n: merged[n] for n in wanted}, entry("success")


async def _stage2_generate_all_sections(
    client: anthropic.AsyncAnthropic, study_type: str, context: dict, section_order: list[str],
    *, extra_directives: str = "", stage: str = "stage2_generate",
) -> tuple[dict[str, str], dict]:
    """One Sonnet request, forced tool-use, producing every section in
    ``section_order`` at once (the single-request form of _stage2_generate_groups;
    generation itself now writes the sections in parallel groups).

    Thinking is intentionally OFF and tool_choice is forced: this reuses the
    exact pattern services/ai_service.py's extract_pk_parameters() already
    relies on for reliable large structured output.
    """
    return await _stage2_generate_groups(
        client, study_type, context, section_order, [list(section_order)],
        extras=[extra_directives], stage=stage,
    )


async def _stage2_generate_parallel(
    client: anthropic.AsyncAnthropic, study_type: str, context: dict, section_order: list[str],
) -> tuple[dict[str, str], dict]:
    """The protocol's sections written as ~GENERATION_PARALLELISM contiguous
    groups at once over one cached prompt (about 3-4 minutes instead of ~17)."""
    groups = _partition_sections(section_order, generation_parallelism())
    return await _stage2_generate_groups(
        client, study_type, context, section_order, groups, required=[],
    )


_REMEDIABLE_STATUSES = {QCStatus.FAIL.value, QCStatus.WARNING.value}


# Context key -> the underscore-prefixed population_config key the same engine
# result is stored under. run_qc is handed a population_config, so the computed
# engines have to be mapped back to their stored names or its engine-aware
# checks silently see nothing and pass a protocol they should have flagged.
_QC_ENGINE_KEYS: tuple[tuple[str, str], ...] = (
    ("sampling_schedule", "_sampling_schedule"),
    ("stratification", "_stratification"),
    ("ddi", "_ddi"),
    ("safety_gating", "_safety_gating"),
    ("bioanalytical", "_bioanalytical"),
    ("safety_reporting", "_safety_reporting"),
    ("crf_dmp", "_crf_dmp"),
    ("icf", "_icf"),
    ("design_decisions", "_design_decisions"),
)


def _qc_population_config(context: dict) -> dict:
    """Adapt the pipeline ``context`` to the population_config shape run_qc wants."""
    cfg: dict[str, Any] = {
        "_sample_size": context.get("sample_size") or {},
        "_label": context.get("label_data") or {},
    }
    for ctx_key, cfg_key in _QC_ENGINE_KEYS:
        value = context.get(ctx_key)
        if value:
            cfg[cfg_key] = value
    # NOTE: the randomization result is deliberately NOT passed through. The
    # context only ever holds its blinded view, and re-deriving anything from it
    # here would put allocation detail into a report a blinded reader can open.
    if context.get("dose_strength"):
        cfg["dose_strength"] = context["dose_strength"]
    return cfg


async def _qc_remediate(
    client: anthropic.AsyncAnthropic, study_type: str, context: dict,
    sections: dict[str, str], section_order: list[str],
) -> tuple[dict[str, str], dict, Optional[dict]]:
    """Deterministic-QC-driven auto-fix: run run_qc, then have Sonnet regenerate
    just the sections with FAIL/WARNING findings, applying the specific feedback.

    This is the "agent actually makes the improvements" step — instead of only
    reporting what is missing, the flagged issues are fed back into one targeted
    Sonnet pass so the stored protocol comes out as complete as the checker can
    drive it. Returns (possibly-updated sections, qc summary dict, remediation
    log entry or None).
    """
    pop_cfg = _qc_population_config(context)
    report_before = run_qc(
        study_type=study_type, population_config=pop_cfg,
        sections=sections, expected_sections=section_order,
    ).to_dict()

    targeted = [
        f for f in report_before["findings"]
        if f["status"] in _REMEDIABLE_STATUSES and f.get("section") in section_order
    ]
    qc: dict[str, Any] = {
        "applied": False,
        "qc_before": report_before,
        "qc_after": None,
        "sections_remediated": [],
        "issues_targeted": [
            {"section": f.get("section"), "status": f["status"],
             "title": f["title"], "detail": f["detail"]}
            for f in targeted
        ],
    }
    if not targeted:
        return sections, qc, None

    by_section: dict[str, list[dict]] = {}
    for f in targeted:
        by_section.setdefault(f["section"], []).append(f)
    target_sections = [s for s in section_order if s in by_section]

    header = (
        "QC REMEDIATION PASS — a deterministic quality check flagged the issues "
        "below in your draft. Regenerate this section, resolving every listed issue "
        "while preserving all content that is already correct. Reuse the locked "
        "design facts above verbatim (sample size N, acceptance limits, washout "
        "duration, target power) — never restate them with different numbers. "
        "Write protocol text only: never quote, paraphrase or mention this feedback, "
        "the quality check, or what would be 'correct' or 'boilerplate' — a reader "
        "of the protocol must never see review commentary."
    )
    extras = []
    for s in target_sections:
        lines = [header, f"\nSection '{s}' — fix:"]
        lines += [f"  - [{f['status'].upper()}] {f['title']}: {f['detail']}" for f in by_section[s]]
        extras.append("\n".join(lines))

    # One request per flagged section, in parallel, over the SAME cached prompt
    # as the generation pass (same all-sections tool and shared text), so the
    # ~50k-token context is read from cache instead of being paid for again.
    fixed, log = await _stage2_generate_groups(
        client, study_type, context, section_order, [[s] for s in target_sections],
        extras=extras, stage="stage2_remediate", required=[],
    )
    if log.get("status") == "success" and fixed:
        merged = dict(sections)
        for name, text in fixed.items():
            if text and text.strip():
                merged[name] = text
        sections = merged
        qc["applied"] = True
        qc["sections_remediated"] = target_sections
        qc["qc_after"] = run_qc(
            study_type=study_type, population_config=pop_cfg,
            sections=sections, expected_sections=section_order,
        ).to_dict()

    # Residual FAILs after remediation. These are the findings the QC engine
    # still asserts about the FINAL text, so a document carrying them is one the
    # app's own checker says is wrong - most consequentially when two sections
    # state different sample sizes, or an assumed CV is presented as
    # literature-derived. The protocol is still persisted (a paid generation is
    # not thrown away and the author can fix it in the editor), but this list is
    # what routers/export.py refuses to render a deliverable document from.
    final_report = qc.get("qc_after") or qc.get("qc_before") or {}
    qc["blocking_failures"] = [
        {"section": f.get("section"), "category": f.get("category"),
         "title": f.get("title"), "detail": f.get("detail")}
        for f in final_report.get("findings", [])
        if f.get("status") == QCStatus.FAIL.value
    ]

    return sections, qc, log


async def _mock_all_sections(context: dict, section_order: list[str]) -> dict[str, str]:
    """MOCK_AI path: delegate to AIService's existing per-section stub so mock
    output is byte-identical to the pre-pipeline behaviour."""
    ai_svc = AIService()
    sections: dict[str, str] = {}
    for name in section_order:
        sections[name] = await ai_svc.generate_protocol_section(name, context)
    return sections


async def generate_protocol_via_pipeline(
    *, context: dict, study_type: str, section_order: list[str],
) -> tuple[dict[str, str], dict, list[dict], list[dict], Optional[dict]]:
    """Three-stage Sonnet-only replacement for the old per-section asyncio.gather loop.

    Returns (sections, classification, validation, pipeline_log, qc).
    ``sections`` matches the shape routers/protocols.py::create_protocol already
    persists (one HTML string per name in section_order). Stage 2 writes the
    sections as ~GENERATION_PARALLELISM groups in parallel over one cached
    prompt (see _stage2_generate_parallel); QC remediation rewrites each flagged
    section in parallel over the same cache.
    ``qc`` is the before/after QC-remediation summary (None if generation never
    reached the remediation stage).

    MOCK_AI=1 skips all real calls (see _mock_all_sections).
    """
    if os.environ.get("MOCK_AI"):
        sections = await _mock_all_sections(context, section_order)
        classification = {"drug_type": None, "confidence": 1.0, "halt_reason": None}
        pipeline_log = [
            _log_entry("stage1_classify", SONNET_MODEL, "mocked"),
            _log_entry("stage2_generate", SONNET_MODEL, "mocked"),
            _log_entry("stage2_remediate", SONNET_MODEL, "mocked"),
            _log_entry("stage3_validate", SONNET_MODEL, "mocked"),
        ]
        return sections, classification, [], pipeline_log, None

    client = anthropic.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"), max_retries=4)
    pipeline_log: list[dict] = []

    classification, log1 = await _stage1_sanity_check(
        client, context.get("drug_inn", ""), study_type,
    )
    pipeline_log.append(log1)

    sections: dict[str, str] = {}
    validation: list[dict] = []

    stage1_ok = log1["status"] == "success" and not classification.get("halt_reason")
    if not (stage1_ok and classification.get("confidence", 0.0) >= CONFIDENCE_THRESHOLD):
        return sections, classification, validation, pipeline_log, None

    sections, log2 = await _stage2_generate_parallel(client, study_type, context, section_order)
    pipeline_log.append(log2)

    qc: Optional[dict] = None
    if log2["status"] == "success" and sections:
        # QC-driven remediation: deterministically check the draft, then have
        # Sonnet fix the flagged sections before validation runs on the result.
        sections, qc, remediation_log = await _qc_remediate(
            client, study_type, context, sections, section_order,
        )
        if remediation_log is not None:
            pipeline_log.append(remediation_log)

        combined_text = "\n\n".join(
            f"=== {name.upper()} ===\n{sections[name]}"
            for name in section_order if sections.get(name)
        )
        facts = {
            "sample_size": context.get("sample_size"),
            "guideline_version": context.get("guideline_version"),
        }
        validation, log3 = await _stage3_validate(client, combined_text, classification, facts)
        pipeline_log.append(log3)

    return sections, classification, validation, pipeline_log, qc
