"""Assemble the DETERMINISTIC results object for a Clinical Study Report.

This is the glue between the validated engines and the stored CSR: it takes a
parsed upload (:mod:`services.dataset_ingest`) plus the parent protocol's design,
runs NCA (for raw concentration-time input) and the bioequivalence analysis, and
returns a single JSON-able ``results`` dict of LOCKED numbers. Nothing here is
ever shown to the LLM as something to change — the AI narrative is written
*around* this object.

Every number is computed, not guessed. A metric that cannot be analysed (e.g.
lambda_z not estimable for AUC0-inf, or an unsupported replicate design) records
an explicit ``error`` string rather than a fabricated value, so a reviewer always
sees why a figure is missing.
"""
from __future__ import annotations

from typing import Any

from services.be_analysis import analyze_crossover_2x2, analyze_parallel
from services.be_replicate import analyze_replicate
from services.dataset_ingest import ParsedDataset
from services.nca import compute_profiles

# PK metrics analysed for bioequivalence. Cmax + AUC0-inf are the co-primary
# endpoints that decide the overall verdict; AUC0-t is secondary/supportive.
_BE_METRICS = ("cmax", "auc0t", "auc0inf")
_COPRIMARY = ("cmax", "auc0inf")


def _be_one_metric(
    records: list[dict], metric: str, *, design_key: str, is_nti: bool
) -> dict:
    """Run the BE analysis for a single metric, robust to missing values.

    Drops subjects/periods whose value for this metric is missing or non-positive
    (e.g. AUC0-inf where lambda_z was not estimable) and records an ``error``
    dict instead of raising if the metric cannot be analysed.
    """
    clean = [
        {
            "subject": r["subject"],
            "sequence": r.get("sequence", ""),
            "period": r.get("period"),
            "treatment": r["treatment"],
            "value": r["values"][metric],
        }
        for r in records
        if r["values"].get(metric) is not None and r["values"][metric] > 0
    ]
    try:
        if design_key == "replicate":
            # FDA reference-scaled ABE (RSABE) for replicate designs / highly
            # variable drugs. Returns its own dict (already compatible with the
            # verdict + export consumers). is_nti raises NotImplementedError,
            # which we surface as an error row rather than a fabricated result.
            return analyze_replicate(clean, metric_name=metric, is_nti=is_nti)
        if design_key == "parallel":
            res = analyze_parallel(clean, metric_name=metric, is_nti=is_nti)
        else:  # crossover (default) — also covers any higher-order crossover
            res = analyze_crossover_2x2(clean, metric_name=metric, is_nti=is_nti)
        return res.to_dict()
    except Exception as exc:  # noqa: BLE001 — surface, don't crash the whole CSR
        return {"metric": metric, "error": str(exc)}


def _records_from_params(parsed: ParsedDataset) -> list[dict]:
    """Pre-computed-parameter rows → per-(subject,period) metric records."""
    return [
        {
            "subject": r["subject"],
            "sequence": r["sequence"],
            "period": r["period"],
            "treatment": r["treatment"],
            "values": {"cmax": r["cmax"], "auc0t": r["auc0t"], "auc0inf": r["auc0inf"]},
        }
        for r in parsed.rows
    ]


def _records_from_nca(parsed: ParsedDataset) -> tuple[list[dict], dict]:
    """Raw concentration-time rows → NCA per (subject,period) → metric records.

    Returns (be_records, nca_table) where nca_table is {"subject|period": {...}}.
    """
    profiles: dict[tuple, list] = {}
    meta: dict[tuple, tuple] = {}
    for r in parsed.rows:
        key = (r["subject"], r["period"])
        profiles.setdefault(key, []).append((r["time"], r["concentration"]))
        meta[key] = (r["sequence"], r["treatment"])

    lloq = parsed.summary.get("lloq") or 0.0
    nca_results = compute_profiles(profiles, lloq=lloq)

    be_records: list[dict] = []
    nca_table: dict[str, Any] = {}
    for key, res in nca_results.items():
        subject, period = key
        sequence, treatment = meta[key]
        nca_table[f"{subject}|{period}"] = {
            "subject": subject,
            "period": period,
            "treatment": treatment,
            **res.to_dict(),
        }
        be_records.append(
            {
                "subject": subject,
                "sequence": sequence,
                "period": period,
                "treatment": treatment,
                "values": {"cmax": res.cmax, "auc0t": res.auc0t, "auc0inf": res.auc0inf},
            }
        )
    return be_records, nca_table


def assemble_results(
    parsed: ParsedDataset, *, design_key: str, is_nti: bool = False
) -> dict:
    """Build the locked CSR results object from a parsed dataset + the design.

    ``design_key`` comes from the parent protocol's computed design
    (``population_config['_sample_size']['design_key']``): 'crossover' |
    'parallel' | 'replicate'.
    """
    design_key = (design_key or "crossover").strip().lower()

    if parsed.kind == "nca":
        be_records, nca_table = _records_from_nca(parsed)
    else:  # params
        be_records = _records_from_params(parsed)
        nca_table = None

    be = {m: _be_one_metric(be_records, m, design_key=design_key, is_nti=is_nti) for m in _BE_METRICS}

    # Overall verdict from the co-primary endpoints (Cmax + AUC0-inf). Only a
    # clean pass on BOTH is "bioequivalent"; an un-analysable co-primary makes
    # the verdict indeterminate rather than a pass.
    coprimary_ok = []
    for m in _COPRIMARY:
        r = be.get(m) or {}
        coprimary_ok.append(bool(r.get("within_be_limits")) if "within_be_limits" in r else None)
    if any(v is None for v in coprimary_ok):
        verdict = "indeterminate"
    elif all(coprimary_ok):
        verdict = "bioequivalent"
    else:
        verdict = "not_bioequivalent"

    disposition = {
        "n_subjects": parsed.summary.get("n_subjects"),
        "n_rows": parsed.summary.get("n_rows"),
        "treatments": parsed.summary.get("treatments"),
        "periods": parsed.summary.get("periods"),
        "sequences": parsed.summary.get("sequences"),
    }
    if parsed.kind == "nca":
        disposition["n_blq"] = parsed.summary.get("n_blq")
        disposition["lloq"] = parsed.summary.get("lloq")

    results: dict[str, Any] = {
        "input_kind": parsed.kind,
        "design_key": design_key,
        "is_nti": is_nti,
        "co_primary": list(_COPRIMARY),
        "verdict": verdict,
        "disposition": disposition,
        "be": be,
    }
    if nca_table is not None:
        results["nca"] = nca_table
    return results
