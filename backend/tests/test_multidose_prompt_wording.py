"""The locked design directive tells the model a multiple-dose patient
biosimilar study is NOT a healthy-volunteer single-dose study (pure tests)."""
from services import ai_service, three_stage_pipeline
from services.biostatistics import recommend_sample_size

OVERRIDE = "POPULATION / REGIMEN OVERRIDE"


def _directive(**kw) -> str:
    return ai_service._locked_design_directive(recommend_sample_size(**kw).to_dict())


def test_patient_multiple_dose_biosimilar_gets_regimen_override():
    text = _directive(
        study_type="biosimilar_pk", drug_inn="rituximab", cv_intra_pct=28.0,
        cv_source="literature", dosing="multiple", subject_type="patient",
    )
    assert OVERRIDE in text
    assert "ENDPOINT OVERRIDE" in text
    assert "steady state" in text
    assert "single-dose" in text.split(OVERRIDE, 1)[1].split("\n", 1)[0]


def test_override_absent_for_single_dose_bioequivalence():
    text = _directive(
        study_type="bioequivalence", drug_inn="ibuprofen", cv_intra_pct=20.0,
        cv_source="literature",
    )
    assert OVERRIDE not in text


def test_override_absent_for_single_dose_biosimilar():
    text = _directive(
        study_type="biosimilar_pk", drug_inn="adalimumab", cv_intra_pct=25.0,
        cv_source="literature",
    )
    assert OVERRIDE not in text


def test_override_absent_for_multiple_dose_healthy_biosimilar():
    text = _directive(
        study_type="biosimilar_pk", drug_inn="rituximab", cv_intra_pct=28.0,
        cv_source="literature", dosing="multiple", subject_type="healthy",
    )
    assert OVERRIDE not in text


def test_both_prompt_paths_share_the_same_directive_function():
    assert three_stage_pipeline._locked_design_directive is ai_service._locked_design_directive


def test_override_appears_after_the_co_primary_endpoints_line():
    text = _directive(
        study_type="biosimilar_pk", drug_inn="rituximab", cv_intra_pct=28.0,
        cv_source="literature", dosing="multiple", subject_type="patient",
    )
    assert text.index("Co-primary endpoints:") < text.index(OVERRIDE)


def test_regimen_override_lines_tolerates_missing_or_odd_endpoints():
    assert ai_service._regimen_override_lines({}) == []
    assert ai_service._regimen_override_lines({"co_primary_endpoints": None}) == []
    assert ai_service._regimen_override_lines(
        {"co_primary_endpoints": ["First-dose AUC(0-tau) x"]}
    ) == []
