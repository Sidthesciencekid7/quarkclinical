"""Disease-setting ranking of candidate indications (services/patient_population.py).

The reviewing clinical pharmacologist's rule for patient PK studies: prefer an
indication with LOW PK variability - an oncology indication in the adjuvant
(post-resection) or low-tumour-burden setting, or a non-oncology (e.g.
immunology) indication - over metastatic disease, and monotherapy over
chemotherapy combinations, for the same reason.

Pure tests: no database, no fixtures. The label excerpts in
tests/fixtures/label_indications/ are verbatim sentences from the real DailyMed
INDICATIONS AND USAGE sections (the setid is in each file's first line).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from services import patient_population as pp
from services.patient_population import (
    _INDICATION_KB,
    infer_monotherapy,
    infer_therapeutic_area,
    infer_tumor_setting,
    patient_population_directive,
    select_patient_population,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "label_indications"


def _fixture_label(name: str) -> dict:
    lines = (_FIXTURES / f"{name}.txt").read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("# DailyMed") and "setid" in lines[0]
    return {"indications_usage_full": " ".join(lines[1:])}


def _select(drug: str, label: dict | str | None, **kw) -> dict:
    if isinstance(label, str):
        label = {"indications_usage": label}
    return select_patient_population(
        drug_inn=drug, study_type="biosimilar_pk", subject_type="patient", label=label, **kw
    )


def _names(result: dict) -> list[str]:
    return [x["indication"] for x in result["ranked"]]


# ---------------------------------------------------------------------------
# Curated molecules: adjuvant beats metastatic
# ---------------------------------------------------------------------------

_NIVO_MINIMAL = (
    "OPDIVO is indicated for the adjuvant treatment of adult patients with completely resected "
    "Stage IIB, Stage IIC, Stage III, or Stage IV melanoma. OPDIVO is indicated for the treatment "
    "of adult patients with metastatic non-small cell lung cancer."
)


def test_nivolumab_prefers_adjuvant_resected_melanoma_over_metastatic_nsclc():
    """The Kashiv nivolumab biosimilar study KSHB012-101 setting."""
    r = _select("nivolumab", _NIVO_MINIMAL)
    rec = r["recommended"]
    assert rec["indication"].startswith("Melanoma — adjuvant")
    assert rec["tumor_setting"] == "adjuvant"
    assert rec["monotherapy"] == "yes"
    assert rec["on_label"] is True
    names = _names(r)
    assert names.index(rec["indication"]) < names.index("Non-small cell lung cancer — metastatic")


def test_nivolumab_real_label_recommends_an_adjuvant_indication():
    r = _select("nivolumab", _fixture_label("nivolumab"))
    assert r["recommended"]["tumor_setting"] == "adjuvant"
    assert r["recommended"]["indication"].startswith("Melanoma — adjuvant")
    assert r["recommended"]["on_label"] is True
    # neoadjuvant NSCLC text is present in the real label and must not count as adjuvant NSCLC
    assert not any(x["on_label"] and x["indication"].startswith("Non-small cell lung cancer — adjuvant") for x in r["ranked"])
    metastatic = [x for x in r["ranked"] if x["tumor_setting"] == "high_burden"]
    assert metastatic and all(_names(r).index(m["indication"]) > 0 for m in metastatic)


def test_pembrolizumab_real_label_recommends_an_adjuvant_indication():
    r = _select("pembrolizumab", _fixture_label("pembrolizumab"))
    assert r["recommended"]["tumor_setting"] == "adjuvant"
    assert r["recommended"]["on_label"] is True
    adjuvant = [x for x in r["ranked"] if x["tumor_setting"] == "adjuvant"]
    high = [x for x in r["ranked"] if x["tumor_setting"] == "high_burden"]
    assert len(adjuvant) == 3 and high
    assert max(_names(r).index(a["indication"]) for a in adjuvant) < min(_names(r).index(h["indication"]) for h in high)


def test_trastuzumab_adjuvant_breast_outranks_metastatic_breast():
    label = (
        "Herceptin is indicated in adults for adjuvant treatment of HER2 overexpressing breast cancer. "
        "Herceptin is indicated in adults for HER2-overexpressing metastatic breast cancer."
    )
    r = _select("trastuzumab", label)
    names = _names(r)
    assert names.index("HER2-overexpressing breast cancer — adjuvant treatment") < names.index(
        "HER2-positive metastatic breast cancer"
    )
    assert r["recommended"]["tumor_setting"] == "adjuvant"


def test_trastuzumab_real_label_confirms_all_curated_indications():
    r = _select("trastuzumab", _fixture_label("trastuzumab"))
    assert all(x["on_label"] for x in r["ranked"])
    assert r["recommended"]["tumor_setting"] == "adjuvant"


def test_bevacizumab_ovarian_after_resection_is_the_only_post_surgical_candidate():
    r = _select("bevacizumab", _fixture_label("bevacizumab"))
    rec = r["recommended"]
    assert rec["indication"].startswith("Epithelial ovarian")
    # Stage III/IV disease may retain residual tumour after debulking, so it is
    # deliberately NOT classed as adjuvant (review finding); it still ranks
    # ahead of the clearly metastatic indications.
    assert rec["tumor_setting"] == "unknown"
    assert "residual tumour" in rec["setting_note"]
    assert "no macroscopic tumour burden" not in rec["rank_reason"]
    # the label's "not indicated for adjuvant treatment of colon cancer" must not confirm anything adjuvant for CRC
    crc = next(x for x in r["ranked"] if x["indication"] == "Metastatic colorectal cancer")
    assert crc["tumor_setting"] == "high_burden"


def test_rituximab_immunology_beats_oncology_and_lymphoma_is_last():
    label = (
        "RITUXAN is indicated for Non-Hodgkin's Lymphoma (NHL). RITUXAN, in combination with methotrexate, "
        "is indicated for rheumatoid arthritis. RITUXAN, in combination with glucocorticoids, is indicated "
        "for Granulomatosis with Polyangiitis (GPA) and Microscopic Polyangiitis (MPA)."
    )
    r = _select("rituximab", label)
    assert r["recommended"]["indication"] == "Rheumatoid arthritis"
    assert _names(r)[-1] == "Follicular / non-Hodgkin lymphoma"
    gpa = next(x for x in r["ranked"] if x["indication"].startswith("Granulomatosis"))
    assert gpa["therapeutic_area"] == "immunology" and gpa["monotherapy"] == "no"


def test_rituximab_real_label_confirms_ra_gpa_and_nhl():
    r = _select("rituximab", _fixture_label("rituximab"))
    assert all(x["on_label"] for x in r["ranked"])
    assert r["recommended"]["indication"] == "Rheumatoid arthritis"


# ---------------------------------------------------------------------------
# Uncurated molecules: setting is inferred from the label wording
# ---------------------------------------------------------------------------

_FAKEMAB_LABEL = (
    "Indicated for the adjuvant treatment of patients with completely resected melanoma. "
    "Indicated for metastatic non-small cell lung cancer in combination with platinum-based chemotherapy."
)


def test_uncurated_molecule_splits_an_oncology_disease_by_setting_and_prefers_adjuvant():
    r = _select("fakemab", _FAKEMAB_LABEL)
    names = _names(r)
    assert "Melanoma — adjuvant (post-resection) setting" in names
    assert "Non-small cell lung cancer — metastatic / advanced setting" in names
    assert r["recommended"]["indication"] == "Melanoma — adjuvant (post-resection) setting"
    nsclc = next(x for x in r["ranked"] if x["indication"].startswith("Non-small cell lung cancer"))
    assert nsclc["monotherapy"] == "no"
    assert "not curated" in nsclc["monotherapy_note"].lower()


def test_neoadjuvant_is_not_classified_adjuvant():
    label = "Indicated as neoadjuvant treatment of patients with resectable HER2-positive breast cancer."
    assert infer_tumor_setting(label) != "adjuvant"
    r = _select("fakemab", label)
    assert all(x["tumor_setting"] != "adjuvant" for x in r["ranked"])


@pytest.mark.parametrize("text,expected", [
    ("completely resected Stage IV melanoma", "adjuvant"),
    ("adjuvant treatment of urothelial carcinoma", "adjuvant"),
    ("following complete resection", "adjuvant"),
    ("after surgery", "adjuvant"),
    ("unresectable melanoma", "high_burden"),
    ("metastatic non-small cell lung cancer", "high_burden"),
    ("locally advanced, recurrent disease", "high_burden"),
    ("early-stage breast cancer", "low_burden"),
    ("localized prostate cancer", "low_burden"),
    ("neoadjuvant treatment of breast cancer", "unknown"),
    ("neo-adjuvant chemotherapy", "unknown"),
    ("plaque psoriasis", "unknown"),
    ("", "unknown"),
])
def test_infer_tumor_setting(text, expected):
    assert infer_tumor_setting(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("as a single agent", "yes"),
    ("as monotherapy", "yes"),
    ("in combination with paclitaxel", "no"),
    ("with platinum-based chemotherapy", "no"),
    ("as a single agent or in combination with ipilimumab", "possible"),
    ("for the treatment of patients with disease", None),
    ("", None),
])
def test_infer_monotherapy(text, expected):
    assert infer_monotherapy(text) == expected


def test_infer_therapeutic_area():
    assert infer_therapeutic_area("metastatic melanoma") == "oncology"
    assert infer_therapeutic_area("plaque psoriasis") == "other"


# ---------------------------------------------------------------------------
# Non-regression and structural guarantees
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("drug", ["adalimumab", "etanercept"])
def test_immunology_rankings_are_unchanged(drug):
    label = {"indications_usage": (
        "indicated for moderate-to-severe chronic plaque psoriasis, rheumatoid arthritis, "
        "psoriatic arthritis, ankylosing spondylitis, Crohn's disease and ulcerative colitis"
    )}
    r = select_patient_population(
        drug_inn=drug, study_type="biosimilar_pk", subject_type="patient", label=label)
    assert r["recommended"]["indication"] == "Moderate-to-severe chronic plaque psoriasis"
    assert r["recommended"]["monotherapy"] == "yes"
    assert r["recommended"]["tumor_setting"] == "not_applicable"
    assert r["recommended"]["therapeutic_area"] == "immunology"


def test_every_candidate_dict_carries_the_new_keys():
    r = _select("nivolumab", _NIVO_MINIMAL)
    for cand in [r["recommended"], *r["ranked"], *r["rejected"]]:
        for key in ("therapeutic_area", "tumor_setting", "setting_note", "curation_source", "user_specified"):
            assert key in cand
    assert r["engine_recommended"] == r["recommended"]
    assert r["user_specified"] is False and r["user_indication"] is None
    assert r["user_indication_warnings"] == []


def test_selection_rules_are_six_and_name_the_setting_rule():
    r = _select("nivolumab", _NIVO_MINIMAL)
    assert len(r["selection_rules"]) == 6
    assert "adjuvant" in r["selection_rules"][2] and "antigen sink" in r["selection_rules"][2]


def test_directive_states_the_disease_setting_for_the_nivolumab_case():
    directive = patient_population_directive(_select("nivolumab", _NIVO_MINIMAL))
    assert "Disease setting" in directive
    assert "adjuvant" in directive
    assert "ALREADY SELECTED" in directive


def test_directive_omits_the_setting_line_for_a_pure_immunology_result():
    r = _select("adalimumab", {"indications_usage": "indicated for plaque psoriasis"})
    assert "Disease setting" not in patient_population_directive(r)


def test_every_curated_oncology_entry_is_sourced_or_marked_unverified():
    seen = 0
    for inn, profiles in _INDICATION_KB.items():
        for prof in profiles:
            if prof.therapeutic_area != "oncology":
                continue
            seen += 1
            notes = " ".join([prof.monotherapy_note, prof.pk_variability_note, prof.setting_note, prof.naive_note])
            assert prof.curation_source or "unverified" in notes.lower(), f"{inn}: {prof.name}"
            # "unknown" is allowed only when the entry explains why (bevacizumab
            # post-debulking ovarian: residual disease may remain).
            assert prof.tumor_setting in ("adjuvant", "low_burden", "high_burden") or (
                prof.tumor_setting == "unknown" and prof.setting_note
            )
    assert seen >= 14


def test_pd1_entries_do_not_present_the_setting_preference_as_a_label_fact():
    for inn in ("nivolumab", "pembrolizumab"):
        for prof in _INDICATION_KB[inn]:
            assert "not a label finding" in prof.pk_variability_note


def test_uncurated_oncology_label_with_only_unqualified_disease_gets_an_unknown_setting():
    r = _select("fakemab", "Indicated for the treatment of patients with melanoma.")
    assert r["recommended"]["indication"] == "Melanoma"
    assert r["recommended"]["tumor_setting"] == "unknown"


def test_set_of_labels_used_by_the_module_is_consistent():
    assert set(pp.SETTING_LABELS) == set(pp._SETTING_RANK)
