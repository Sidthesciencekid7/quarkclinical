"""CSR narrative generation — AI writes prose around LOCKED computed numbers.

``build_csr_context`` distils the deterministic results (from
:mod:`services.csr_results`) plus the parent protocol's design into a compact
"facts" object. ``generate_csr_narrative`` then writes each ICH E3 section with
those facts supplied as read-only truth and the numbers-locked directive
(:data:`services.csr_sections.NUMBERS_LOCKED_DIRECTIVE`) in force — the model
never produces or alters a results figure.

Honours ``MOCK_AI=1`` (credit-free stub sections) exactly like
``ai_service.generate_protocol_section`` so the whole flow can be exercised
end-to-end without spending API credits.
"""
from __future__ import annotations

import json
import os
from typing import Any

import anthropic

from services.csr_sections import (
    CSR_SECTION_CONFIGS,
    CSR_SECTION_ORDER,
    CSR_SECTION_TITLES,
    NUMBERS_LOCKED_DIRECTIVE,
)

# Opus for the narrative — a CSR is a high-stakes regulatory document. Same model
# family used elsewhere in the app; overridable via env for cost tuning.
_CSR_MODEL = os.getenv("CSR_NARRATIVE_MODEL", "claude-opus-4-6")
_MAX_TOKENS = 4096

_DESIGN_LABEL = {
    "crossover": "single-dose, randomized, two-treatment, two-period crossover",
    "parallel": "single-dose, randomized, two-arm parallel-group",
    "replicate": "single-dose, randomized, replicate (reference-scaled ABE) crossover",
}


def build_csr_context(protocol, drug_inn: str, results: dict) -> dict:
    """Curate the LOCKED facts the narrative may cite. Nothing here is negotiable
    by the model — it is the deterministic output of the analysis engines."""
    be = results.get("be") or {}
    ss = (getattr(protocol, "population_config", None) or {}).get("_sample_size") or {}

    bioequivalence: dict[str, Any] = {}
    unanalysable: dict[str, str] = {}
    limits = None
    for metric, r in be.items():
        if not isinstance(r, dict):
            continue
        if "error" in r:
            unanalysable[metric] = r["error"]
            continue
        bioequivalence[metric] = {
            "point_estimate_pct": round(r["point_estimate_pct"], 2),
            "ci_90_pct": [round(r["ci_lower_pct"], 2), round(r["ci_upper_pct"], 2)],
            "cv_intra_pct": round(r["cv_intra_pct"], 2),
            "within_limits": r["within_be_limits"],
            "df": r.get("df"),
        }
        if limits is None:
            limits = [r.get("lower_limit_pct"), r.get("upper_limit_pct")]

    design_key = results.get("design_key", "crossover")
    return {
        "drug": drug_inn,
        "study_type": getattr(getattr(protocol, "study_type", None), "value", "BE"),
        "design": _DESIGN_LABEL.get(design_key, design_key),
        "design_key": design_key,
        "acceptance_limits_pct": limits or [80.00, 125.00],
        "planned_n_enrolled": ss.get("n_enrolled"),
        "disposition": results.get("disposition") or {},
        "co_primary": results.get("co_primary") or ["cmax", "auc0inf"],
        "bioequivalence": bioequivalence,
        "unanalysable_metrics": unanalysable,
        "verdict": results.get("verdict"),
        "nca_method": (
            "linear-up/log-down trapezoidal AUC; best-fit terminal lambda_z "
            "(adjusted-R^2 selection)"
            if results.get("input_kind") == "nca"
            else None
        ),
        "safety": results.get("safety"),
    }


def _mock_section(section_name: str, context: dict) -> str:
    """Credit-free placeholder that still surfaces the locked verdict/design, so a
    MOCK export exercises the real layout (MOCK_AI=1)."""
    drug = context.get("drug", "the drug")
    verdict = context.get("verdict", "reported")
    if section_name == "title":
        return (
            f"A Single-Dose, Randomized Study to Assess the Bioequivalence of Test and "
            f"Reference {str(drug).title()} in Healthy Adults (MOCK SAMPLE)."
        )
    title = CSR_SECTION_TITLES.get(section_name, section_name)
    return (
        f"<p><strong>[MOCK PLACEHOLDER — {title}]</strong> Credit-free sample prose "
        f"(MOCK_AI=1). Drug: {drug}; design: {context.get('design')}; overall "
        f"bioequivalence outcome: {verdict}. Real Claude-authored narrative replaces "
        f"this when MOCK_AI is unset; all numeric results come from the locked results "
        f"object, never from the model.</p>"
    )


def _prompts(section_name: str, context: dict) -> tuple[str, str]:
    cfg = CSR_SECTION_CONFIGS[section_name]
    system = f"You are {cfg['expertise']}. {NUMBERS_LOCKED_DIRECTIVE}"
    user = (
        f"Write the '{CSR_SECTION_TITLES[section_name]}' section of the CSR.\n\n"
        f"SECTION GUIDANCE:\n{cfg['guidance']}\n\n"
        f"COMPUTED RESULTS (verified, final — cite these numbers EXACTLY, never alter "
        f"or invent any):\n{json.dumps(context, indent=2, default=str)}\n\n"
        f"Return only the section body as clean HTML (or, for the title section, one "
        f"plain-text line)."
    )
    return system, user


async def _generate_one(client, section_name: str, context: dict) -> str:
    system, user = _prompts(section_name, context)
    response = await client.messages.create(
        model=_CSR_MODEL,
        max_tokens=_MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in response.content if b.type == "text").strip()


async def generate_csr_narrative(context: dict) -> dict:
    """Generate every ICH E3 narrative section. Returns {section_key: html}.

    Raises on a fatal API error (the caller records it as a failed generation).
    Numbers are never produced here — only prose citing ``context``.
    """
    if os.environ.get("MOCK_AI"):
        return {name: _mock_section(name, context) for name in CSR_SECTION_ORDER}

    client = anthropic.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    sections: dict[str, str] = {}
    for name in CSR_SECTION_ORDER:
        sections[name] = await _generate_one(client, name, context)
    return sections
