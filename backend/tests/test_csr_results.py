"""Unit tests for the CSR results-assembly glue (services/csr_results.py).

Exercises the integration seam that turns a parsed dataset + the protocol's
design into the locked results object — including dispatch to the RSABE engine
for replicate designs. Pure functions, no DB/HTTP, no API credits.
"""
from __future__ import annotations

import csv
import io

from services.csr_results import assemble_results
from services.dataset_ingest import parse_dataset


def _params_csv(rows: list[list]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["subject", "sequence", "period", "treatment", "cmax", "auc0t", "auc0inf"])
    for r in rows:
        w.writerow(r)
    return buf.getvalue().encode("utf-8")


def test_assemble_crossover_params():
    pairs = [
        ("S01", "TR", 101, 100), ("S02", "RT", 102, 100), ("S03", "TR", 99, 100),
        ("S04", "RT", 103, 101), ("S05", "TR", 100, 99), ("S06", "RT", 101, 100),
        ("S07", "TR", 98, 100), ("S08", "RT", 102, 101),
    ]
    rows: list[list] = []
    for subj, seq, tc, rc in pairs:
        plan = [("1", "T", tc), ("2", "R", rc)] if seq == "TR" else [("1", "R", rc), ("2", "T", tc)]
        for per, trt, cm in plan:
            rows.append([subj, seq, per, trt, cm, round(cm * 5, 3), round(cm * 5.5, 3)])

    parsed = parse_dataset(_params_csv(rows), "cross.csv", "params")
    results = assemble_results(parsed, design_key="crossover")

    assert results["verdict"] == "bioequivalent"
    assert results["design_key"] == "crossover"
    for m in ("cmax", "auc0t", "auc0inf"):
        assert "error" not in results["be"][m]
        assert results["be"][m]["within_be_limits"] is True


def test_assemble_replicate_dispatches_to_rsabe():
    """A replicate design routes each metric through the RSABE engine and comes
    back with the scaled-specific fields (no crash, real decision)."""
    rows: list[list] = []
    for i in range(1, 13):
        subj = f"S{i:02d}"
        seq = "TRTR" if i % 2 else "RTRT"
        order = ["T", "R", "T", "R"] if seq == "TRTR" else ["R", "T", "R", "T"]
        base = 100 + i
        for per, trt in enumerate(order, start=1):
            # deterministic within-subject spread so the Reference replicates
            # differ (a real CVwR to scale on) and T tracks R
            spread = 1.0 + 0.12 * (((per * 7 + i) % 5) - 2) / 2.0
            cm = base * (1.02 if trt == "T" else 1.0) * spread
            rows.append([subj, seq, per, trt, round(cm, 3), round(cm * 5, 3), round(cm * 5.5, 3)])

    parsed = parse_dataset(_params_csv(rows), "rep.csv", "params")
    results = assemble_results(parsed, design_key="replicate")

    assert results["design_key"] == "replicate"
    cmax = results["be"]["cmax"]
    assert "error" not in cmax, cmax
    assert isinstance(cmax["within_be_limits"], bool)
    assert "scaled" in cmax  # RSABE-specific field is present
    assert cmax["point_estimate_pct"] > 0
    # provenance carries the RSABE regulatory basis + the independent R cross-check
    assert "regulatory_basis" in cmax["provenance"]
