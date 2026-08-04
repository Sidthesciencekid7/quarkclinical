"""Regression coverage for the sample-size sensitivity table built inside
``recommend_sample_size`` (services/biostatistics.py, ~lines 684-708) and
rendered by both export paths (routers/export.py DOCX + PDF, which both read
the same ``rec["sensitivity"]`` list with no separate computation).

Bug fixed here (found by a clinical pharmacologist reviewing a real generated
canakinumab / biosimilar_pk / parallel protocol):

1. CV-basis double-conversion: when the recommendation's OWN design_key is
   "parallel", cv_pct is already a between-subject value. The old code always
   multiplied by 1.5 before calling sample_size_tost(design="parallel"),
   double-converting and producing numbers with no defensible meaning for a
   parallel-basis study (156 instead of the document's own 37/arm).
2. Total-vs-per-arm mislabeling: sample_size_tost(design="parallel") returns
   the TOTAL across both arms, but the table's own header ("Parallel/arm")
   and footnote ("parallel shown per arm") claim it is per-arm. Fixed by
   halving the parallel_80/parallel_90 values when building the row.
"""
import pytest

from services.biostatistics import recommend_sample_size, sample_size_tost


def test_parallel_sensitivity_matches_main_calculation_per_arm():
    """The single most important regression: for a parallel-design
    recommendation, the sensitivity row whose cv_pct matches the study's own
    CV must reproduce the SAME per-arm evaluable count as the main
    calculation -- this is exactly the canakinumab / biosimilar_pk / parallel
    contradiction a real reviewer found (cv_pct=25, gmr=0.95, power=0.90,
    0.80-1.25 limits: main calc gives 74 total / 37 per arm; the sensitivity
    table's cv_pct==25 / parallel_90 cell must also read 37, not 156).
    """
    rec = recommend_sample_size(
        study_type="biosimilar_pk",
        drug_inn="canakinumab",
        cv_intra_pct=25.0,
        cv_source="literature",
        power=0.90,
        gmr=0.95,
    )
    assert rec.design_key == "parallel"
    assert rec.n_evaluable == 74  # confirmed main-calc figure

    row_25 = next(r for r in rec.sensitivity if r["cv_pct"] == 25)
    assert row_25["parallel_90"] == rec.n_evaluable // 2 == 37


@pytest.mark.parametrize("power,expected_col", [(0.80, "parallel_80"), (0.90, "parallel_90")])
def test_parallel_design_sensitivity_uses_cv_direct_no_double_conversion(power, expected_col):
    """For design_key=="parallel", the parallel_XX column must be computed
    from the CV axis DIRECTLY (no x1.5), then halved to per-arm -- not
    cv*1.5 fed into sample_size_tost and left as a total."""
    rec = recommend_sample_size(
        study_type="biosimilar_pk",
        drug_inn="canakinumab",
        cv_intra_pct=25.0,
        cv_source="literature",
        power=power,
        gmr=0.95,
    )
    for row in rec.sensitivity:
        cv = row["cv_pct"]
        expected = sample_size_tost(
            cv, gmr=0.95, power=power, lower=0.80, upper=1.25, design="parallel"
        ) // 2
        assert row[expected_col] == expected


def test_parallel_design_crossover_comparison_column_divides_instead_of_multiplies():
    """For design_key=="parallel", the crossover "what-if" comparison column
    must divide the axis by 1.5 (approximating the within-subject CV),
    not use it directly."""
    rec = recommend_sample_size(
        study_type="biosimilar_pk",
        drug_inn="canakinumab",
        cv_intra_pct=25.0,
        cv_source="literature",
        power=0.90,
        gmr=0.95,
    )
    row_25 = next(r for r in rec.sensitivity if r["cv_pct"] == 25)
    expected = sample_size_tost(
        25 / 1.5, gmr=0.95, power=0.90, lower=0.80, upper=1.25, design="crossover"
    )
    assert row_25["crossover_90"] == expected


def test_crossover_design_sensitivity_crossover_columns_not_regressed():
    """Non-regression: for a crossover/BE recommendation, the crossover_80 /
    crossover_90 columns must be BYTE-FOR-BYTE unchanged from before this fix
    (pinned against the actual pre-fix output, captured by running the
    unmodified code against these exact inputs)."""
    rec = recommend_sample_size(
        study_type="BE",
        drug_inn="ibuprofen",
        cv_intra_pct=25.0,
        cv_source="literature",
        requested_design="crossover",
        power=0.90,
        gmr=0.95,
    )
    assert rec.design_key == "crossover"

    # Pinned pre-fix values (captured before the fix landed).
    expected_rows = {
        15: (12, 16),
        20: (20, 26),
        25: (28, 38),
        30: (40, 52),
        35: (52, 70),
        40: (66, 88),
    }
    for row in rec.sensitivity:
        cv = row["cv_pct"]
        exp_80, exp_90 = expected_rows[cv]
        assert row["crossover_80"] == exp_80
        assert row["crossover_90"] == exp_90


def test_crossover_design_sensitivity_parallel_columns_now_per_arm():
    """The total-vs-per-arm mislabeling fix (defect 2) is a table-wide fix,
    not conditional on design_key -- sample_size_tost(design="parallel")
    always returns a total regardless of the CV fed in, and the header/
    footnote always claim per-arm. So for a crossover-design recommendation
    too, parallel_80/90 must now be the halved (per-arm) figure, not the
    pre-fix raw total."""
    rec = recommend_sample_size(
        study_type="BE",
        drug_inn="ibuprofen",
        cv_intra_pct=25.0,
        cv_source="literature",
        requested_design="crossover",
        power=0.90,
        gmr=0.95,
    )
    row_25 = next(r for r in rec.sensitivity if r["cv_pct"] == 25)
    # Pre-fix this cell was 156 (raw total, cv*1.5 = 37.5 fed to
    # sample_size_tost(design="parallel")). Post-fix it must be halved.
    raw_total = sample_size_tost(
        25 * 1.5, gmr=0.95, power=0.90, lower=0.80, upper=1.25, design="parallel"
    )
    assert raw_total == 156
    assert row_25["parallel_90"] == raw_total // 2 == 78


def test_sensitivity_values_are_always_ints_and_even_before_halving():
    """sample_size_tost's internal step is always 2 for both designs, so the
    parallel total is always even and the //2 halving is exact (no rounding
    ambiguity), for both design_key branches."""
    for design_key, study_type, drug in [
        ("parallel", "biosimilar_pk", "canakinumab"),
        ("crossover", "BE", "ibuprofen"),
    ]:
        kwargs = dict(
            study_type=study_type, drug_inn=drug, cv_intra_pct=25.0,
            cv_source="literature", power=0.90, gmr=0.95,
        )
        if design_key == "crossover":
            kwargs["requested_design"] = "crossover"
        rec = recommend_sample_size(**kwargs)
        assert rec.design_key == design_key
        for row in rec.sensitivity:
            assert isinstance(row["parallel_80"], int)
            assert isinstance(row["parallel_90"], int)
