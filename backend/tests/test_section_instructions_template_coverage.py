"""Coverage of the sponsor's real NHV protocol template against the
per-section generation instructions in services/ai_service.py.

A practising reviewer supplied a real, in-use clinical trial protocol template
(Template_Clinical Trial Protocol in NHV_06-10-2025_CC.docx) and asked that
every element of it be present in what this app generates. Line-by-line
comparison against the existing _SECTION_CONFIGS found the app's instructions
already matched or exceeded the template for most sections (many are more
rigorous - deterministic DDI/stratification/safety-gating tables the template
does not have at all). It found a handful of genuine, concrete omissions,
closed here:

  - Population/Inclusion: no standard contraception-method enumeration or
    post-menopausal definition (present in the template, absent from the
    instruction).
  - Population/Exclusion: several standard NHV exclusion categories the
    template names were not asked for (hospitalization/immobilization, food
    allergy/dietary restriction, confinement unavailability, planned surgery,
    positive drug/alcohol screen).
  - Population/Withdrawal: the template's one-time-rescreening allowance and
    its deferral of analysis-set inclusion/exclusion decisions to the SAP were
    both absent.
  - Treatment/Packaging-Labeling-Storage: the template enumerates 9 specific
    label content items; the instruction only said "label contents".
  - Study Assessments/Safety and Tolerability: the template has EIGHT H3
    sub-subsections; the instruction only asked for SIX - missing 'Pregnancy
    testing' (a real safety gap: no instruction anywhere asked for a pregnancy
    test) and 'Clinical Laboratory Parameters'.
  - SAP/Study Populations: the template's minor/major protocol-deviation
    classification was not requested.
  - SAP/Safety Analyses: the template breaks this into labelled
    sub-subsections (Adverse Events, Laboratory Tests, Vital Signs, ECG,
    Physical Examination, Injection Site Reaction, Prior/Concomitant
    Medications); the instruction asked for the same content as one
    undifferentiated paragraph.

These are static-instruction-text tests (no AI call): they assert the
INSTRUCTION STRING itself contains the added requirement, which is what
actually reaches the model. They do not, and cannot, prove a given generation
complies - see test_biologic_review_feedback.py / test_ai_service_prompts.py
for the pattern of testing what a LOCKED FACTS block does once real engine
data is present.
"""
from __future__ import annotations

from services.ai_service import _SECTION_CONFIGS


def _instr(section: str) -> str:
    return _SECTION_CONFIGS[section]["instructions"]


# ---------------------------------------------------------------------------
# Population
# ---------------------------------------------------------------------------


def test_inclusion_enumerates_contraception_methods_and_menopause_definition():
    i = _instr("population")
    for method in ("Abstinence".lower(), "hormonal contraception", "double-barrier",
                   "intrauterine device", "surgical sterility"):
        assert method.lower() in i.lower(), method
    assert "post-menopausal" in i.lower()
    assert "amenorrhea" in i.lower()
    assert "partner of childbearing potential" in i  # male-subject requirement


def test_exclusion_covers_the_added_standard_nhv_categories():
    i = _instr("population")
    for phrase in (
        "hospitalization or prolonged immobilization",
        "food allergy",
        "unavailability for the required confinement",
        "planned surgical or outpatient procedure",
        "positive drug-of-abuse or alcohol screen",
    ):
        assert phrase in i, phrase


def test_withdrawal_states_rescreening_allowance_and_defers_analysis_set_to_sap():
    i = _instr("population")
    assert "rescreened once" in i
    assert "new informed consent form" in i
    assert "new screening identifier" in i
    assert "Statistical Analysis Plan" in i
    assert "blinded data review meeting" in i


def test_population_instructions_still_cover_pre_existing_ddi_and_stratification_rules():
    """Non-regression: the new text must not have clobbered the existing
    deterministic-engine wiring this section depends on."""
    i = _instr("population")
    assert "`ddi` object" in i
    assert "`stratification` object" in i
    assert "`randomization` object" in i
    assert "suppressed_factors" in i
    assert "INDIVIDUAL SUBJECT STOPPING RULES" in i


# ---------------------------------------------------------------------------
# Treatment
# ---------------------------------------------------------------------------


def test_packaging_labeling_storage_enumerates_required_label_content():
    i = _instr("treatment")
    for item in (
        "protocol number", "subject/study centre number", "contents and quantity",
        "randomisation code or kit number", "investigator name", "storage instructions",
        "for clinical trial use only", "sponsor contact information", "expiry date",
    ):
        assert item.lower() in i.lower(), item


def test_treatment_instructions_still_cover_pre_existing_blinding_and_stratification_rules():
    i = _instr("treatment")
    assert "Stratification Factors" in i
    assert "CRITICAL ALLOCATION-CONCEALMENT RULES" in i
    assert "Breaking the Blind" in i


# ---------------------------------------------------------------------------
# Study Assessments
# ---------------------------------------------------------------------------


def test_safety_and_tolerability_has_eight_subsections_including_pregnancy_and_labs():
    i = _instr("study_assessments")
    assert "EIGHT <h3> sub-subsections" in i
    assert "'Pregnancy testing'" in i
    assert "'Clinical Laboratory Parameters'" in i
    # The actual safety-relevant content, not just the heading.
    assert "serum pregnancy test at screening and EOS" in i
    assert "urine pregnancy test before dosing" in i
    assert "confirmed negative pre-dose result" in i


def test_study_assessments_instructions_still_cover_pre_existing_subsections():
    i = _instr("study_assessments")
    for label in ("'Demographic Data'", "'Vital Signs'", "'Injection Site Assessment'",
                  "'Electrocardiogram (ECG)'", "'Physical Examination'",
                  "'Adverse Event Monitoring'"):
        assert label in i, label
    assert "Discharge Criteria" in i


# ---------------------------------------------------------------------------
# Statistical Analysis Plan
# ---------------------------------------------------------------------------


def test_study_populations_requests_minor_major_deviation_classification():
    i = _instr("statistical_analysis_plan")
    assert "minor or major" in i
    assert "before unblinding" in i


def test_safety_analyses_breaks_into_labelled_subsections():
    i = _instr("statistical_analysis_plan")
    for label in (
        "'Adverse Events'", "'Laboratory Tests'", "'Vital Signs'",
        "'Electrocardiogram (ECG)'", "'Physical Examination'",
        "'Prior and Concomitant Medications'", "'Injection Site Reaction'",
    ):
        assert label in i, label
    # The biologic-only gating on the added Injection Site Reaction subsection
    # must be explicit, not silently always-on.
    assert "ONLY for a biologic" in i


def test_sap_instructions_still_cover_pre_existing_cv_provenance_and_replacement_rules():
    """Non-regression: the SAP section carries a LOT of prior, carefully-tuned
    logic (CV provenance branching, meta-analysis appendix, replacement
    policy). Confirm none of it was disturbed."""
    i = _instr("statistical_analysis_plan")
    assert "CV PROVENANCE line" in i
    assert "INVESTIGATOR-SPECIFIED CV" in i
    assert "Appendix: Meta-Analysis of Published Pharmacokinetic Studies" in i
    assert "`randomization` object is present in context" in i


# ---------------------------------------------------------------------------
# Cross-section: every edited section still imports/parses and every other
# section's instructions are untouched.
# ---------------------------------------------------------------------------


def test_all_seventeen_sections_still_present_and_non_empty():
    for name, cfg in _SECTION_CONFIGS.items():
        assert cfg.get("instructions", "").strip(), name
        assert cfg.get("fda_ref", "").strip(), name
        assert isinstance(cfg.get("context_keys"), list), name
    assert len(_SECTION_CONFIGS) == 17
