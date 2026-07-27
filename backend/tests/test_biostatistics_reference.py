"""Regression guard: the exact-TOST engine must reproduce the canonical
balanced 2x2 crossover sample-size table (Diletti / PowerTOST sampleN.TOST),
GMR 0.95, 80% power, 80.00-125.00% limits, alpha 0.05, within +/-2 subjects.

This is the CI counterpart to evals/run_evals.py.
"""
import pytest

from services.biostatistics import sample_size_tost

# (cv_pct, expected_total_N)
_REFERENCE_80 = [(15.0, 12), (20.0, 20), (25.0, 28), (30.0, 40)]
_TOL = 2


@pytest.mark.parametrize("cv_pct,expected", _REFERENCE_80)
def test_sample_size_matches_reference(cv_pct, expected):
    n = sample_size_tost(
        cv_pct=cv_pct, gmr=0.95, power=0.80,
        lower=0.80, upper=1.25, alpha=0.05, design="crossover",
    )
    assert abs(n - expected) <= _TOL, (
        f"CV={cv_pct}%: engine N={n}, reference={expected} (tol +/-{_TOL})"
    )
