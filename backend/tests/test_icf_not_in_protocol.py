"""The protocol must not describe the Informed Consent FORM as a document.

The consent form is a separate deliverable on its own approval cycle
(services/icf.py, exported via /export/icf/docx). Its document control - title,
version, date, the regulatory basis it is written against, which 21 CFR 50.25
elements it covers, and which site-specific fields are still pending - belongs
to that document. The protocol previously restated all of it, which put a
second, uncontrolled copy of the form's identity into a document that goes
stale the moment the form is revised.

So the contract is:
  * the protocol describes the consent PROCESS (how consent is obtained), and
  * says nothing about the consent FORM as an artefact, and
  * the standalone consent document is unaffected and still buildable.

Both generation paths are pinned, because they carry INDEPENDENT copies of the
engine-directive plan: services/ai_service.py (per-section) and
services/three_stage_pipeline.py (whole document in one request). A fix applied
to only one leaves the other reintroducing the content.
"""
from __future__ import annotations

import json

import pytest

from services.ai_service import (
    _ENGINE_DIRECTIVE_PLAN,
    _SECTION_CONFIGS,
    _build_section_prompt,
    _filter_context,
)
from services.three_stage_pipeline import (
    _ENGINE_DIRECTIVES,
    _PROMPT_EXCLUDED_ENGINES,
    _engine_directives,
)


def _icf_result() -> dict:
    """Shaped like services/icf.py's build_icf() output, carrying exactly the
    document-control fields that must not reach the protocol."""
    return {
        "document_type": "Informed Consent Form",
        "title": "Informed Consent Form for Study CANA-001",
        "version": "1.0",
        "regulatory_basis": "21 CFR 50.25; 21 CFR 50.27",
        "elements_coverage": {"50.25(a)(1)": True, "50.25(a)(2)": True},
        "missing_required_elements": ["compensation for injury"],
        "pending_fields": ["investigator name", "site name", "compensation text"],
        "readability": {"grade_level": 8.1},
        "assumptions": ["Site contacts to be supplied before IRB submission"],
        "sections": [{"heading": "What is this study?", "text": "Plain language."}],
    }


# Fingerprints of the form's document control. Deliberately the JSON keys AND
# human-readable phrasings, so the check catches both the raw-JSON leak and a
# rendered-directive leak.
_BANNED = [
    "pending_fields",
    "elements_coverage",
    "missing_required_elements",
    "regulatory_basis",
    "document_type",
    "compensation text",
    "investigator name",
]


def _ctx() -> dict:
    return {
        "org_name": "Acme CRO",
        "irb_info": "Central IRB",
        "study_type": "biosimilar_pk",
        "icf": _icf_result(),
        "safety_reporting": {"summary": "Expedited reporting per 21 CFR 312.32."},
    }


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def test_no_section_requests_icf_in_its_json_context():
    offenders = [n for n, c in _SECTION_CONFIGS.items() if "icf" in c["context_keys"]]
    assert offenders == [], f"sections still pulling icf into context: {offenders}"


def test_no_section_renders_the_icf_directive():
    offenders = [
        name for name, plan in _ENGINE_DIRECTIVE_PLAN.items()
        if any(key == "icf" for key, _ in plan)
    ]
    assert offenders == [], f"sections still rendering icf directive: {offenders}"


def test_whole_document_path_does_not_render_the_icf_directive():
    assert not any(key == "icf" for key, _ in _ENGINE_DIRECTIVES)


def test_whole_document_path_excludes_icf_from_the_json_context():
    """Dropping an engine from _ENGINE_DIRECTIVES alone does the OPPOSITE of
    suppressing it - the context block carries every key whose directive was
    not rendered, so the raw result would be serialised instead. This pins the
    explicit exclusion that prevents that."""
    assert "icf" in _PROMPT_EXCLUDED_ENGINES


# ---------------------------------------------------------------------------
# Actual prompt text - per-section path
# ---------------------------------------------------------------------------


def test_ethics_prompt_carries_no_consent_form_document_control():
    cfg = _SECTION_CONFIGS["ethics"]
    filtered = _filter_context(_ctx(), cfg["context_keys"])
    assert "icf" not in filtered

    _, user_prompt = _build_section_prompt("ethics", cfg, filtered)
    for banned in _BANNED:
        assert banned not in user_prompt, f"{banned!r} reached the ethics prompt"


def test_ethics_instructions_forbid_describing_the_form():
    """The instruction text itself must carry the prohibition - otherwise a
    model with general knowledge of consent forms could still volunteer a
    version number or a pending-fields list."""
    instructions = _SECTION_CONFIGS["ethics"]["instructions"]
    assert "Say NOTHING about the Informed Consent Form as a document" in instructions
    # ...while still asking for the process itself.
    assert "Informed Consent Process" in instructions
    assert "21 CFR 50.25" in instructions


# ---------------------------------------------------------------------------
# Actual prompt text - whole-document path
# ---------------------------------------------------------------------------


def test_whole_document_prompt_carries_no_consent_form_document_control():
    ctx = _ctx()
    directives, rendered = _engine_directives(ctx, "")
    assert "icf" not in rendered

    context_block = json.dumps(
        {k: v for k, v in ctx.items()
         if k not in rendered and k not in _PROMPT_EXCLUDED_ENGINES},
        default=str,
    )
    combined = directives + context_block
    for banned in _BANNED:
        assert banned not in combined, f"{banned!r} reached the whole-document prompt"


def test_other_engines_still_reach_the_whole_document_prompt():
    """Guard against over-suppression: excluding icf must not disturb the
    engines that DO belong in the protocol.

    Uses a REAL engine result rather than a hand-written stub - the directive
    renderers return "" for a payload missing the fields they narrate, so a
    stub would make this assert its own fixture instead of the wiring.
    """
    from services.safety_reporting import build_safety_reporting_plan

    ctx = _ctx()
    ctx["safety_reporting"] = build_safety_reporting_plan(
        study_type="biosimilar_pk", healthy_volunteers=True,
    )
    directives, rendered = _engine_directives(ctx, "")

    assert "safety_reporting" in rendered
    assert directives.strip()
    # ...and suppressing icf alongside it still holds.
    assert "icf" not in rendered
    for banned in _BANNED:
        assert banned not in directives


# ---------------------------------------------------------------------------
# The standalone consent document is unaffected
# ---------------------------------------------------------------------------


def test_standalone_consent_document_still_builds():
    """Removing the form from the PROTOCOL must not remove the form itself."""
    from services.icf import build_icf, icf_directive

    result = build_icf({
        "drug_inn": "canakinumab",
        "study_type": "biosimilar_pk",
        "org_name": "Acme CRO",
        "protocol_version": 1,
    })
    assert result.get("sections")
    # The directive still renders - it is what builds the standalone document.
    assert icf_directive(result).strip()
