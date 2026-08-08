"""The whole-document generation path must render the meta-analysis directive
too, not just the per-section path.

services/ai_service.py (per-section generation) and services/three_stage_pipeline.py
(whole-document, one request, the path POST /api/protocols actually uses) carry
INDEPENDENT copies of which computed engines get rendered as locked-fact
directives (see test_icf_not_in_protocol.py's docstring for the same structural
point about the icf engine). ai_service.py's _ENGINE_DIRECTIVE_PLAN wires
("meta_analysis", meta_analysis_directive) into "statistical_analysis_plan";
three_stage_pipeline.py's _ENGINE_DIRECTIVES tuple did not carry the same entry
at all - a real, confirmed gap found in code review, not a hypothetical one.

The consequence is not "no meta-analysis appears" - dropping an engine from
_ENGINE_DIRECTIVES does the OPPOSITE of suppressing it. _stage2_generate_all_sections
serialises every context key whose directive was NOT rendered into the raw JSON
context block instead (see _engine_directives' own docstring). So before this
fix, a whole-document generation still received the meta-analysis data, but as
an unlocked JSON blob the model could reinterpret freely, rather than the
locked-facts directive that states plainly which CV was adopted, why, and -
after services/meta_analysis.py's cv_basis fix - whether it was derived from a
mean/SD pair and whether a wrong-basis or wrong-population CV was found but
deliberately not used. Losing the directive means losing that disclosure text
for every protocol generated through the whole-document route.
"""
from __future__ import annotations

import json

from services.meta_analysis import build_meta_analysis, derive_cv_from_mean_sd
from services.three_stage_pipeline import _ENGINE_DIRECTIVES, _engine_directives


def _real_meta_analysis_result() -> dict:
    """A REAL build_meta_analysis() result, not a hand-written stub - the
    directive renderer returns "" for a payload missing the fields it
    narrates, so a stub would risk asserting its own fixture instead of the
    actual wiring (same rationale as test_icf_not_in_protocol.py's
    test_other_engines_still_reach_the_whole_document_prompt)."""
    extracted = [
        {"parameter": "Cmax", "value": 8.09, "sd": 3.68, "unit": "mcg/mL",
         "population": "healthy adult subjects", "source_pmid": "DAILYMED",
         "confidence": 1.0},
    ]
    return build_meta_analysis(
        derive_cv_from_mean_sd(extracted), subject_type="healthy", cv_basis="between",
    )


def test_meta_analysis_is_wired_into_the_whole_document_engine_directives():
    assert any(key == "meta_analysis" for key, _ in _ENGINE_DIRECTIVES)


def test_meta_analysis_directive_actually_renders_in_the_whole_document_path():
    ctx = {"meta_analysis": _real_meta_analysis_result()}
    directives, rendered = _engine_directives(ctx, "")

    assert "meta_analysis" in rendered
    assert "META-ANALYSIS OF PUBLISHED PK STUDIES" in directives
    assert "45.5%" in directives
    assert "derived from that study's reported mean and SD" in directives


def test_meta_analysis_raw_payload_does_not_leak_into_the_json_context_block():
    """Before this fix: meta_analysis was never in `rendered`, so it survived
    into the raw JSON context block below - the unlocked form this directive
    exists to replace. After: it is rendered, so it is excluded from the JSON
    block exactly like every other rendered engine."""
    ctx = {"meta_analysis": _real_meta_analysis_result(), "other_field": "keep me"}
    directives, rendered = _engine_directives(ctx, "")

    context_block = json.dumps(
        {k: v for k, v in ctx.items() if k not in rendered},
        default=str,
    )
    assert "meta_analysis" in rendered
    assert "\"meta_analysis\"" not in context_block  # the key itself is gone
    assert "powering_cv_pct" not in context_block  # a raw meta_analysis dict key
    assert "other_field" in context_block  # non-engine keys are unaffected


def test_meta_analysis_directive_is_skipped_gracefully_when_absent():
    """Non-regression: a context with no meta_analysis key (e.g. no PK
    literature was retrievable at all) must not error - _engine_directives
    already guards every engine on `if not payload: continue`."""
    directives, rendered = _engine_directives({}, "")
    assert "meta_analysis" not in rendered
    assert directives == ""


def test_meta_analysis_sits_alongside_regulatory_sources_like_the_per_section_plan():
    """Mirrors services/ai_service.py's _ENGINE_DIRECTIVE_PLAN["statistical_analysis_plan"],
    where meta_analysis and regulatory_sources are wired together because they
    are both sourcing-provenance facts for the same SAP narrative."""
    keys = [key for key, _ in _ENGINE_DIRECTIVES]
    assert "meta_analysis" in keys
    assert "regulatory_sources" in keys
