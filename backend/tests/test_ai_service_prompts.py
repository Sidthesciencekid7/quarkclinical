"""Tests for services/ai_service.py's prompt assembly.

Two real contradictions, both confirmed in a live generated canakinumab
document, are pinned here so they cannot silently regress:

1. Replacement-policy contradiction. services/randomization.py genuinely
   computes a subject-replacement policy (spare/reserve slots) and renders it
   via randomization_directive(). "treatment" already receives that directive.
   "population" (subsection 6.3, Subject Withdrawal/Discontinuation and
   Replacement) and "statistical_analysis_plan" (subsection 3(e)) used to carry
   a hardcoded, unconditional "NOT replaced" instruction that could contradict
   the SAME document's "treatment" section. Both sections must now receive the
   SAME locked replacement fact (via _ENGINE_DIRECTIVE_PLAN + context_keys) so
   the per-section regeneration path cannot drift from the whole-document path.

2. Within-subject vs between-subject CV mislabeling. A PARALLEL design has NO
   within-subject comparison at all -- the CV that powers it is a
   between-subject (inter-subject) quantity. _locked_design_directive() and
   _cv_provenance_lines() used to hardcode "within-subject CV" regardless of
   design. _cv_basis_label() now picks the correct term from design_key.
"""
from __future__ import annotations

from services.ai_service import (
    _SECTION_CONFIGS,
    _ENGINE_DIRECTIVE_PLAN,
    _build_section_prompt,
    _cv_basis_label,
    _cv_provenance_lines,
    _filter_context,
    _locked_design_directive,
)
from services.randomization import generate_allocation, randomization_directive


def _sample_size(design_key: str, **overrides) -> dict:
    """Minimal SampleSizeRecommendation-shaped dict for prompt-assembly tests."""
    ss = {
        "cv_pct": 25.0,
        "cv_source": "literature",
        "gmr": 0.95,
        "power": 0.8,
        "alpha": 0.05,
        "lower_limit": 0.80,
        "upper_limit": 1.25,
        "design": "Parallel-group, single-dose" if design_key == "parallel"
        else "2-period crossover, single-dose",
        "design_key": design_key,
        "approach": "ABE",
        "n_per_group": 20 if design_key == "parallel" else None,
        "n_enrolled": 40,
        "n_evaluable": 36,
        "dropout_rate": 0.10,
        "endpoint": "Cmax, AUC0-inf",
        "needs_simulation": False,
    }
    ss.update(overrides)
    return ss


def _minimal_context(*, sample_size: dict, randomization: dict | None) -> dict:
    """Only the keys population/SAP prompt-building actually reads."""
    return {
        "drug_inn": "testdrug",
        "study_type": "BE",
        "guideline_version": None,
        "sample_size": sample_size,
        "randomization": randomization,
        "population_config": {},
        "ddi": None,
        "safety_gating": None,
        "stratification": None,
        "acceptance_criteria": None,
        "cv_intra": None,
        "meta_analysis": None,
        "regulatory_sources": None,
    }


def _prompt_for(section: str, context: dict) -> str:
    cfg = _SECTION_CONFIGS[section]
    filtered = _filter_context(context, cfg["context_keys"])
    _system, user = _build_section_prompt(section, cfg, filtered)
    return user


# ---------------------------------------------------------------------------
# Bug 1 - replacement-policy contradiction
# ---------------------------------------------------------------------------


def test_population_and_sap_are_wired_to_the_randomization_engine():
    """Both sections must be ABLE to see the replacement fact, in both the
    per-section (_ENGINE_DIRECTIVE_PLAN) and JSON-context (context_keys) paths.
    """
    for section in ("population", "statistical_analysis_plan"):
        plan_keys = [key for key, _fn in _ENGINE_DIRECTIVE_PLAN.get(section, ())]
        assert "randomization" in plan_keys, (
            f"{section} must render randomization_directive so its replacement "
            "policy cannot contradict the treatment section's"
        )
        assert "randomization" in _SECTION_CONFIGS[section]["context_keys"], (
            f"{section} context_keys must include 'randomization', otherwise "
            "_filter_context strips it before _engine_directives ever sees it"
        )


def test_population_and_sap_instructions_are_conditional_on_randomization():
    """The hardcoded 'NOT replaced' instruction must now be gated behind a WHEN
    a `randomization` object is present ... OTHERWISE ... fallback, matching
    the pattern already used for ddi/stratification/regulatory_sources.
    """
    for section in ("population", "statistical_analysis_plan"):
        instr = _SECTION_CONFIGS[section]["instructions"]
        assert "`randomization` object is present" in instr
        assert "NOT replaced" in instr  # the OTHERWISE default survives


def test_population_and_sap_prompts_agree_when_replacement_is_allowed():
    """When a real randomization result with a replacement policy is in
    context, BOTH sections' assembled prompts must carry the SAME
    replacement-policy directive text -- no contradiction.
    """
    alloc = generate_allocation("parallel", 40, seed=1)
    assert "may be replaced" in alloc["replacement"]["rule"]

    context = _minimal_context(
        sample_size=_sample_size("parallel"), randomization=alloc,
    )

    pop_prompt = _prompt_for("population", context)
    sap_prompt = _prompt_for("statistical_analysis_plan", context)

    expected_line = "- Replacement of withdrawals: %s" % alloc["replacement"]["rule"]
    assert expected_line in pop_prompt
    assert expected_line in sap_prompt
    # Sanity: the two prompts do not merely both mention "replaced" in passing
    # -- they carry the identical locked-fact line, word for word.
    assert "may be replaced" in pop_prompt
    assert "may be replaced" in sap_prompt


def test_population_and_sap_prompts_omit_the_directive_with_no_randomization_data():
    """With NO randomization result in context, neither section may render the
    randomization_directive's locked-fact line (there is nothing to lock), so
    the model is left with only the static, identical OTHERWISE default in
    both sections' instructions -- consistent by construction.
    """
    context = _minimal_context(sample_size=_sample_size("crossover"), randomization=None)

    pop_prompt = _prompt_for("population", context)
    sap_prompt = _prompt_for("statistical_analysis_plan", context)

    for prompt in (pop_prompt, sap_prompt):
        assert "Replacement of withdrawals:" not in prompt
        assert "may be replaced" not in prompt


def test_three_stage_pipeline_already_carries_randomization_directive():
    """services/three_stage_pipeline.py has its own independent engine-directive
    plumbing for the whole-document path. It must already include
    randomization_directive (confirmed by direct read) -- pinned here so a
    future edit cannot silently drop it.
    """
    from services.three_stage_pipeline import _ENGINE_DIRECTIVES

    plan_keys = [key for key, _fn in _ENGINE_DIRECTIVES]
    assert "randomization" in plan_keys
    assert dict(_ENGINE_DIRECTIVES)["randomization"] is randomization_directive


# ---------------------------------------------------------------------------
# Bug 2 - within-subject vs between-subject CV mislabeling
# ---------------------------------------------------------------------------


def test_cv_basis_label():
    assert _cv_basis_label("crossover") == "within-subject"
    assert _cv_basis_label("replicate") == "within-subject"
    assert _cv_basis_label("parallel") == "between-subject"
    # Unrecognised/missing design_key preserves old (within-subject) behavior.
    assert _cv_basis_label(None) == "within-subject"
    assert _cv_basis_label("some_future_design") == "within-subject"


def test_locked_design_directive_uses_between_subject_for_parallel():
    directive = _locked_design_directive(_sample_size("parallel"))
    assert "between-subject CV" in directive
    assert "within-subject CV" not in directive


def test_locked_design_directive_still_uses_within_subject_for_crossover():
    """No regression for the existing/majority BE case."""
    directive = _locked_design_directive(_sample_size("crossover"))
    assert "within-subject CV" in directive
    assert "between-subject CV" not in directive


def test_locked_design_directive_still_uses_within_subject_for_replicate():
    directive = _locked_design_directive(_sample_size("replicate"))
    assert "within-subject CV" in directive
    assert "between-subject CV" not in directive


def test_cv_provenance_lines_label_parallel_as_between_subject():
    ss = _sample_size("parallel", cv_source="investigator override", cv_is_user_override=True)
    lines = _cv_provenance_lines(ss)
    assert lines, "override CV must emit a provenance line"
    assert "between-subject CV" in lines[0]
    assert "within-subject CV" not in lines[0]


def test_cv_provenance_lines_label_crossover_as_within_subject():
    ss = _sample_size("crossover", cv_source="investigator override", cv_is_user_override=True)
    lines = _cv_provenance_lines(ss)
    assert lines, "override CV must emit a provenance line"
    assert "within-subject CV" in lines[0]
    assert "between-subject CV" not in lines[0]


def test_synopsis_and_sap_instructions_use_design_conditional_cv_wording():
    """The instruction STRINGS (module-level constants, no ss available at
    authoring time) must tell the model to read the term from the locked
    design rather than hardcoding 'within-subject'.
    """
    for section in ("synopsis", "statistical_analysis_plan"):
        instr = _SECTION_CONFIGS[section]["instructions"]
        assert "between-subject CV' for a parallel design" in instr
        assert "do not default to 'within-subject' unconditionally" in instr


def test_study_design_instruction_mentions_between_subject_for_parallel_case():
    instr = _SECTION_CONFIGS["study_design"]["instructions"]
    assert "between-subject variability" in instr
    assert "no within-subject comparison at all" in instr
