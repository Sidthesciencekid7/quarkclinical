"""QC must not demand safety rules the engine deliberately suppressed.

For a therapeutic protein the safety-gating engine withholds the QT and Hy's Law
rules (mechanistically inapplicable - see the pharmacologist's biologic review)
and records them in ``suppressed_rules``, but leaves their numbers in
``thresholds``/``counts``. The protocol must not state those rules, so a QC that
still required them failed every biologic protocol for text it was told to omit.
"""
from services.protocol_qc import QCStatus, _check_safety_gating

_THRESHOLDS = {
    "qt": {"qtcf_absolute_ms": 500.0, "qtcf_delta_from_baseline_ms": 60.0},
    "hys_law": {"alt_or_ast_uln_multiple": 3.0, "total_bilirubin_uln_multiple": 2.0},
}
_COUNTS = {
    "related_grade3_halt": 2,
    "qt_boundary_subjects_halt": 4,  # distinct from the stated counts, so its absence is detectable
    "same_event_grade2_cluster_halt": 3,
    "ae_withdrawal_halt": 3,
}
# Halting text that states the Grade 3, cluster and withdrawal counts but says
# nothing about QT or liver tests - correct for a monoclonal antibody.
_SAFETY_TEXT = (
    "<p>Dosing of further subjects will be halted if 2 or more subjects experience a "
    "related Grade 3 or higher adverse event, if 3 or more report the same related "
    "Grade 2 event, or if 3 or more withdraw because of adverse events.</p>"
)


def _findings(suppressed):
    out = []
    gating = {"counts": _COUNTS, "thresholds": _THRESHOLDS, "suppressed_rules": suppressed}
    _check_safety_gating(out.append, {"safety_monitoring": _SAFETY_TEXT}, {"_safety_gating": gating})
    return {f.id: f for f in out}


def test_biologic_with_qt_and_hys_law_suppressed_is_not_failed_for_omitting_them():
    found = _findings(["ind_hys_law", "ind_hepatic_discontinuation", "ind_qt_prolongation",
                       "coh_hys_law", "coh_qt_count"])
    assert "safety_qt" not in found
    assert "safety_hepatic" not in found
    # The QT halting count is not listed as a missing secondary threshold either.
    assert "safety_halting_counts" not in found
    assert found["safety_halting"].status == QCStatus.PASS


def test_small_molecule_still_requires_the_qt_and_hepatic_rules():
    found = _findings([])
    assert found["safety_qt"].status == QCStatus.FAIL
    assert found["safety_hepatic"].status == QCStatus.FAIL
    assert "QT boundary" in found["safety_halting_counts"].detail


def test_suppressed_rules_given_as_objects_are_honoured_too():
    found = _findings([{"key": "ind_qt_prolongation"}, {"key": "ind_hys_law"}, {"key": "coh_qt_count"}])
    assert "safety_qt" not in found and "safety_hepatic" not in found
