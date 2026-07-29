"""
Three-stage AI pipeline for FDA-compliant clinical trial protocol generation.

Architecture (fixed — model assignments are architectural decisions, not
suggestions; do not collapse stages or swap models):

  Stage 1 (Haiku 4.5)  — classification & routing. JSON-only output. Gates the
                          rest of the pipeline on confidence >= CONFIDENCE_THRESHOLD.
  Stage 2 (Opus 4.6)   — full protocol generation. Receives PRE-COMPUTED
                          deterministic facts (washout, sampling schedule,
                          sample size, TOST limits) from biostatistics.py and
                          must reuse them verbatim — Opus reasons about
                          structure and regulatory language only, never
                          arithmetic.
  Stage 3 (Sonnet 4.6) — structured JSON validation/critique of Stage 2's
                          output. No narrative feedback.

Each stage is independently logged: model, input/output tokens, latency, and
status (success / halted / failed). Stage 2 only runs if Stage 1 succeeds with
confidence >= CONFIDENCE_THRESHOLD; Stage 3 only runs if Stage 2 returns
non-empty text.
"""
from __future__ import annotations

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
from services.icf import icf_directive
from services.randomization import randomization_directive
from services.safety_gating import safety_gating_directive
from services.safety_reporting import safety_reporting_directive
from services.sampling_schedule import sampling_schedule_directive

HAIKU_MODEL = "claude-haiku-4-5-20251001"
OPUS_MODEL = "claude-opus-4-6"
SONNET_MODEL = "claude-sonnet-4-6"

CONFIDENCE_THRESHOLD = 0.85

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
# Stage 1 — Haiku: classification & routing
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
            client, model=HAIKU_MODEL, system=_STAGE1_SYSTEM,
            messages=[{"role": "user", "content": query}], max_tokens=1024,
        )
    except Exception as exc:  # noqa: BLE001
        return (
            {"drug_type": None, "template_id": None, "pk_params": {}, "confidence": 0.0,
             "halt_reason": f"Stage 1 API call failed: {exc}"},
            _log_entry(stage, HAIKU_MODEL, "failed", error=str(exc)),
        )

    text = _extract_text(message)
    parsed, err = _parse_json_defensively(text)
    usage = message.usage

    if not isinstance(parsed, dict):
        return (
            {"drug_type": None, "template_id": None, "pk_params": {}, "confidence": 0.0,
             "halt_reason": f"Stage 1 output could not be parsed as JSON: {err}"},
            _log_entry(stage, HAIKU_MODEL, "failed",
                       input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
                       latency_ms=latency_ms, error=err),
        )

    parsed.setdefault("pk_params", {})
    parsed.setdefault("halt_reason", None)
    confidence = parsed.get("confidence") or 0.0
    if confidence < CONFIDENCE_THRESHOLD and not parsed.get("halt_reason"):
        parsed["halt_reason"] = f"confidence {confidence} below threshold {CONFIDENCE_THRESHOLD}"

    log = _log_entry(
        stage, HAIKU_MODEL, "success",
        input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
        latency_ms=latency_ms,
    )
    return parsed, log


# ---------------------------------------------------------------------------
# Stage 2 — Opus: protocol generation
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
            client, model=OPUS_MODEL, system=_STAGE2_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=16000, thinking={"type": "adaptive"},
        )
    except Exception as exc:  # noqa: BLE001
        return "", _log_entry(stage, OPUS_MODEL, "failed", error=str(exc))

    text = _extract_text(message)
    usage = message.usage
    log = _log_entry(
        stage, OPUS_MODEL, "success" if text.strip() else "failed",
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
            client, model=HAIKU_MODEL, system=_STAGE1_SANITY_SYSTEM,
            messages=[{"role": "user", "content": user_content}], max_tokens=512,
        )
    except Exception as exc:  # noqa: BLE001
        return (
            {"drug_type": None, "confidence": 0.0, "halt_reason": f"Stage 1 API call failed: {exc}"},
            _log_entry(stage, HAIKU_MODEL, "failed", error=str(exc)),
        )

    text = _extract_text(message)
    parsed, err = _parse_json_defensively(text)
    usage = message.usage

    if not isinstance(parsed, dict):
        return (
            {"drug_type": None, "confidence": 0.0,
             "halt_reason": f"Stage 1 output could not be parsed as JSON: {err}"},
            _log_entry(stage, HAIKU_MODEL, "failed",
                       input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
                       latency_ms=latency_ms, error=err),
        )

    parsed.setdefault("halt_reason", None)
    confidence = parsed.get("confidence") or 0.0
    if confidence < CONFIDENCE_THRESHOLD and not parsed.get("halt_reason"):
        parsed["halt_reason"] = f"confidence {confidence} below threshold {CONFIDENCE_THRESHOLD}"

    return parsed, _log_entry(
        stage, HAIKU_MODEL, "success",
        input_tokens=usage.input_tokens, output_tokens=usage.output_tokens, latency_ms=latency_ms,
    )


def _build_full_protocol_tool(section_order: list[str]) -> dict:
    return {
        "name": "write_protocol",
        "description": (
            "Write the complete clinical trial protocol. Provide one field per "
            "section, each containing that section's HTML body content."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                name: {
                    "type": "string",
                    "description": (
                        f"HTML body for the '{name.replace('_', ' ').title()}' section. "
                        "Use <p>, <h3>, <ul>/<li>, <table> as needed. Body content only - "
                        "no outer section title, number, or <html>/<body> tags."
                    ),
                }
                for name in section_order
            },
            "required": list(section_order),
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
    ("bioanalytical", bioanalytical_directive),
    ("safety_gating", safety_gating_directive),
    ("safety_reporting", safety_reporting_directive),
    ("crf_dmp", crf_dmp_directive),
    ("icf", icf_directive),
)


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


async def _stage2_generate_all_sections(
    client: anthropic.AsyncAnthropic, study_type: str, context: dict, section_order: list[str],
    *, extra_directives: str = "", stage: str = "stage2_generate",
) -> tuple[dict[str, str], dict]:
    """One Opus call, forced tool-use, producing every section at once.

    Thinking is intentionally OFF and tool_choice is forced: this reuses the
    exact pattern services/ai_service.py's extract_pk_parameters() already
    relies on for reliable large structured output, and avoids stacking an
    untested thinking + forced-tool_choice combination on top of a call that
    already has to fit a ~40KB document in its response.

    ``extra_directives`` (used by the QC remediation pass) is appended to the
    prompt so the same generation machinery can be re-run on a SUBSET of
    sections with specific fix instructions. ``stage`` lets the caller label the
    log entry distinctly (e.g. "stage2_remediate").
    """
    guideline_version = context.get("guideline_version")
    expertise, protocol_type = _expertise_for(study_type, guideline_version)
    design_directive = _locked_design_directive(context.get("sample_size"))
    reg_directive = regulatory_directive(guideline_version or "", study_type)
    decisions_directive = design_directive_fn(context.get("design_decisions") or [])

    # Computed engines travel as their own directives, not as raw JSON (see
    # _ENGINE_DIRECTIVES), and are then dropped from the serialised context so
    # the same facts are not carried twice in two different shapes - which is
    # also how a model ends up reconciling two renderings of one number.
    engine_directives, rendered = _engine_directives(
        context, design_directive + decisions_directive + extra_directives
    )
    context_block = json.dumps(
        {k: v for k, v in context.items() if k not in rendered},
        indent=2, default=str,
    )

    instructions_block = "\n\n".join(
        f"### Section: {name} ###\n"
        f"Regulatory reference: {cfg['fda_ref']}\n"
        f"Instructions: {cfg['instructions']}"
        for name, cfg in _SECTION_CONFIGS.items()
        if name in section_order
    )

    user_prompt = (
        f"Generate the COMPLETE {protocol_type} study protocol by calling write_protocol "
        f"with one field per section.\n\n"
        f"{design_directive}\n"
        f"{reg_directive}\n"
        f"{decisions_directive}\n"
        f"{engine_directives}\n"
        f"Context:\n{context_block}\n\n"
        f"Per-section instructions:\n\n{instructions_block}\n\n"
        "For every section, write body text only - no section title, number, or "
        "meta-commentary inside the HTML; that is added separately when the document "
        "is exported."
        + (f"\n\n{extra_directives}" if extra_directives else "")
    )

    tool = _build_full_protocol_tool(section_order)
    start = time.monotonic()
    try:
        async with client.messages.stream(
            model=OPUS_MODEL,
            max_tokens=20000,
            system=expertise,
            tools=[tool],
            tool_choice={"type": "tool", "name": "write_protocol"},
            messages=[{"role": "user", "content": user_prompt}],
        ) as stream:
            message = await stream.get_final_message()
    except Exception as exc:  # noqa: BLE001
        return {}, _log_entry(stage, OPUS_MODEL, "failed", error=str(exc))
    latency_ms = (time.monotonic() - start) * 1000.0

    sections: dict[str, str] = {}
    for block in message.content:
        if (
            getattr(block, "type", None) == "tool_use"
            and block.name == "write_protocol"
            and isinstance(block.input, dict)
        ):
            sections = {name: (block.input.get(name) or "").strip() for name in section_order}
            break

    usage = message.usage
    log = _log_entry(
        stage, OPUS_MODEL, "success" if sections else "failed",
        input_tokens=usage.input_tokens, output_tokens=usage.output_tokens, latency_ms=latency_ms,
    )
    return sections, log


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
    """Deterministic-QC-driven auto-fix: run run_qc, then have Opus regenerate
    just the sections with FAIL/WARNING findings, applying the specific feedback.

    This is the "agent actually makes the improvements" step — instead of only
    reporting what is missing, the flagged issues are fed back into one targeted
    Opus pass so the stored protocol comes out as complete as the checker can
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

    directive_lines = [
        "QC REMEDIATION PASS — a deterministic quality check flagged the issues "
        "below in your draft. Regenerate ONLY these sections, resolving every "
        "listed issue while preserving all content that is already correct. Reuse "
        "the locked design facts above verbatim (sample size N, acceptance limits, "
        "washout duration, target power) — never restate them with different numbers.",
    ]
    for s in target_sections:
        directive_lines.append(f"\nSection '{s}' — fix:")
        for f in by_section[s]:
            directive_lines.append(f"  - [{f['status'].upper()}] {f['title']}: {f['detail']}")
    directives = "\n".join(directive_lines)

    fixed, log = await _stage2_generate_all_sections(
        client, study_type, context, target_sections,
        extra_directives=directives, stage="stage2_remediate",
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
    """Three-model replacement for the old per-section asyncio.gather loop.

    Returns (sections, classification, validation, pipeline_log, qc).
    ``sections`` matches the shape routers/protocols.py::create_protocol already
    persists (one HTML string per name in section_order) — callers don't need to
    know generation happened via Haiku->Opus->Sonnet instead of N Sonnet calls.
    ``qc`` is the before/after QC-remediation summary (None if generation never
    reached the remediation stage).

    MOCK_AI=1 skips all real calls (see _mock_all_sections).
    """
    if os.environ.get("MOCK_AI"):
        sections = await _mock_all_sections(context, section_order)
        classification = {"drug_type": None, "confidence": 1.0, "halt_reason": None}
        pipeline_log = [
            _log_entry("stage1_classify", HAIKU_MODEL, "mocked"),
            _log_entry("stage2_generate", OPUS_MODEL, "mocked"),
            _log_entry("stage2_remediate", OPUS_MODEL, "mocked"),
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

    sections, log2 = await _stage2_generate_all_sections(client, study_type, context, section_order)
    pipeline_log.append(log2)

    qc: Optional[dict] = None
    if log2["status"] == "success" and sections:
        # QC-driven remediation: deterministically check the draft, then have
        # Opus fix the flagged sections before validation runs on the result.
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
