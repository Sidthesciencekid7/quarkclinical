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


def _flags(population_text: str, treatment_text: str = "") -> dict:
    out = []
    _check_ddi(out.append, {"population": f"<p>{population_text}</p>", "treatment": f"<p>{treatment_text}</p>"},
               {"_ddi": _BIOLOGIC_DDI})
    return {f.id: f for f in out}


def test_second_real_run_denials_raise_no_interaction_failures():
    # Verbatim from the 2026-10-25 parallel-generation run: every pathway and
    # grapefruit mention DENIES a restriction, yet three checks failed on them.
    population = (
        "The restriction on tobacco and nicotine use in criterion 15 above is imposed as a "
        "standardised-conditions measure for confinement, not on the basis of CYP1A2 induction "
        "or any other metabolic mechanism. No restriction is imposed on these grounds, and no "
        "cytochrome-P450 or drug-transporter comedication prohibition is applicable to adalimumab."
    )
    treatment = (
        "Because adalimumab is catabolised by proteolytic pathways and recycled by the neonatal Fc "
        "receptor (FcRn) rather than metabolised by cytochrome P450 enzymes or drug transporters, no "
        "restriction on CYP-interacting agents, P-glycoprotein substrates or inhibitors, gastric acid "
        "suppressants, grapefruit, or other enzyme-modulating agents is imposed. "
        "Grapefruit, Seville orange, and pomelo: No restriction is imposed."
    )
    flags = _flags(population, treatment)
    for fid in ("ddi_biologic_cyp", "ddi_grapefruit"):
        assert fid not in flags, flags.get(fid)
    assert not (flags.get("ddi_specificity") and flags["ddi_specificity"].status == QCStatus.FAIL), \
        flags["ddi_specificity"].detail


def test_a_real_grapefruit_restriction_still_fails_for_a_biologic():
    flags = _flags("Subjects must avoid grapefruit and grapefruit juice from 7 days before dosing.")
    assert flags["ddi_grapefruit"].status == QCStatus.FAIL


def test_a_real_cyp_restriction_still_fails():
    text = "Strong CYP3A4 inhibitors are not permitted within 14 days before the first dose."
    flag = _cyp_flag(text)
    assert flag is not None and flag.status == QCStatus.FAIL


def test_a_transporter_prohibition_still_fails():
    text = "Subjects must not take P-gp inhibitors such as verapamil during the study."
    assert _cyp_flag(text) is not None
