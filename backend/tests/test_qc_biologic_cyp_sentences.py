"""The biologic CYP/transporter QC flag is judged per sentence.

A real adalimumab generation (2026-10-25) was failed for this tobacco exclusion:
"...This restriction is applied as a standardised-conditions measure ...; it is
not predicated on a cytochrome P450 interaction, which would have no mechanistic
relevance to a therapeutic protein." - a sentence that DENIES CYP relevance next
to a drug-agnostic rule. That is exactly what a correct biologic protocol says;
only a sentence that actually restricts on a P450/transporter basis must fail.
"""
from services.protocol_qc import QCStatus, _check_ddi

_BIOLOGIC_DDI = {"modality": "biologic", "pathway_keys": [], "prohibited": [], "restricted": []}


def _cyp_flag(population_text: str):
    out = []
    _check_ddi(out.append, {"population": f"<p>{population_text}</p>"}, {"_ddi": _BIOLOGIC_DDI})
    return next((f for f in out if f.id == "ddi_biologic_cyp"), None)


def test_denying_cyp_relevance_next_to_a_drug_agnostic_rule_passes():
    text = (
        "History of tobacco or nicotine use within six months before screening. This restriction "
        "is applied as a standardised-conditions measure to minimise between-subject variability; "
        "it is not predicated on a cytochrome P450 interaction, which would have no mechanistic "
        "relevance to a therapeutic protein catabolised by proteolysis."
    )
    assert _cyp_flag(text) is None


def test_stating_no_cyp_restriction_is_imposed_passes():
    text = ("No restriction of cytochrome P450 inhibitors, inducers, or drug transporter substrates "
            "is imposed, as adalimumab is not a CYP or transporter substrate.")
    assert _cyp_flag(text) is None


def test_the_real_generation_denial_wording_passes():
    # Verbatim from the 2026-10-25 real adalimumab generation (Population section).
    text = ("Because adalimumab is a therapeutic protein catabolised by proteolytic enzymes rather "
            "than by cytochrome P450 enzymes or drug transporters, no CYP-based, transporter-based, "
            "or food-effect-based drug interaction restriction is applicable; concomitant "
            "medications are governed by the immunological criteria below.")
    assert _cyp_flag(text) is None


def test_a_real_cyp_restriction_still_fails():
    text = "Strong CYP3A4 inhibitors are not permitted within 14 days before the first dose."
    flag = _cyp_flag(text)
    assert flag is not None and flag.status == QCStatus.FAIL


def test_a_transporter_prohibition_still_fails():
    text = "Subjects must not take P-gp inhibitors such as verapamil during the study."
    assert _cyp_flag(text) is not None
