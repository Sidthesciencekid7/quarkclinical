"""
Evaluation harness — the "evidence layer".

Two independent evals, runnable with `python evals/run_evals.py` from the
backend directory:

1. Biostatistics reference validation
   Compares the exact-TOST sample-size engine against the canonical published
   sample-size table (Diletti et al.; reproduced by R's PowerTOST
   `sampleN.TOST`) for a balanced 2×2 crossover, GMR 0.95, 80%/90% power,
   80.00–125.00% limits. The 80%-power column is asserted within ±2 subjects;
   the 90%-power column is reported for visibility.

2. Protocol-quality aggregate
   Runs the deterministic QC validator and the guideline coverage matrix over
   every protocol in the database and reports aggregate readiness and coverage.

Exit code is non-zero if any asserted biostatistics reference is off by more
than the tolerance — so this doubles as a CI regression guard.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Load .env so DATABASE_URL (and any other config) is available when this
# harness is run standalone from the backend directory.
try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # noqa: BLE001 — dotenv optional; env may already be set
    pass

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

from services.biostatistics import sample_size_tost  # noqa: E402

TOL = 2  # subjects; rounding-to-even + method variance

# Canonical balanced 2×2 crossover N (total), GMR 0.95, limits 0.80–1.25,
# alpha 0.05. Source: Diletti/Hauschke exact method == PowerTOST sampleN.TOST.
# Asserted set (high confidence; two are independently validated in-project).
REFERENCE_80 = [
    # (cv_pct, expected_total_N)
    (15.0, 12),
    (20.0, 20),
    (25.0, 28),
    (30.0, 40),
]
# Reported-only (engine output shown; not asserted).
GRID_CVS = [10.0, 15.0, 20.0, 25.0, 30.0, 40.0, 50.0]


def _hr(title: str) -> None:
    print("\n" + "═" * 64)
    print(title)
    print("═" * 64)


def eval_biostatistics() -> int:
    """Return the number of asserted references that failed tolerance."""
    _hr("1. BIOSTATISTICS — engine vs PowerTOST/Diletti reference")

    print("\nAsserted (80% power, GMR 0.95, 2×2 crossover, 80.00–125.00%):")
    print(f"  {'CV%':>5} {'engine':>7} {'ref':>5} {'Δ':>4}  result")
    failures = 0
    for cv, ref in REFERENCE_80:
        n = sample_size_tost(cv_pct=cv, gmr=0.95, power=0.80,
                             lower=0.80, upper=1.25, alpha=0.05, design="crossover")
        d = n - ref
        ok = abs(d) <= TOL
        if not ok:
            failures += 1
        print(f"  {cv:>5.0f} {n:>7} {ref:>5} {d:>+4}  {'PASS' if ok else 'FAIL'}")

    # Informational grid across CV × power
    print("\nReported grid (engine N, GMR 0.95, crossover):")
    print(f"  {'CV%':>5} {'80% pow':>8} {'90% pow':>8}")
    for cv in GRID_CVS:
        n80 = sample_size_tost(cv_pct=cv, gmr=0.95, power=0.80, design="crossover")
        n90 = sample_size_tost(cv_pct=cv, gmr=0.95, power=0.90, design="crossover")
        print(f"  {cv:>5.0f} {n80:>8} {n90:>8}")

    print(f"\n  → {len(REFERENCE_80) - failures}/{len(REFERENCE_80)} asserted references within ±{TOL}.")
    return failures


async def eval_protocols() -> None:
    _hr("2. PROTOCOL QUALITY — QC + coverage over all DB protocols")
    try:
        import json
        import asyncpg
        from services.protocol_qc import run_qc
        from services.guideline_coverage import compute_coverage
    except Exception as exc:  # noqa: BLE001
        print(f"  (skipped — {exc})")
        return

    # Every section applies to every study type — immunogenicity-specific
    # content is conditioned internally (study_assessments / statistical_analysis_plan)
    # rather than being its own top-level section. See protocols.py SECTION_ORDER.
    ALL_SECTIONS = [
        "title", "synopsis", "introduction", "study_rationale", "objectives",
        "endpoints", "study_design", "population", "treatment", "study_assessments",
        "safety_monitoring", "data_handling", "study_monitoring",
        "statistical_analysis_plan", "ethics", "administrative", "references",
    ]

    def expected(st: str) -> list[str]:
        return ALL_SECTIONS

    # Read the connection string from the environment (same DATABASE_URL the app
    # uses) — never hardcode credentials in source. asyncpg wants a plain
    # postgresql:// DSN, so strip SQLAlchemy's "+asyncpg" driver suffix.
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("  (skipped — DATABASE_URL not set; export it or add it to .env)")
        return
    dsn = db_url.replace("+asyncpg", "", 1)
    try:
        conn = await asyncpg.connect(dsn)
        rows = await conn.fetch(
            "SELECT id, study_type, population_config, sections FROM protocols ORDER BY created_at DESC")
        await conn.close()
    except Exception as exc:  # noqa: BLE001
        print(f"  (skipped — database unreachable: {str(exc)[:80]})")
        return

    if not rows:
        print("  (no protocols in database)")
        return

    n = len(rows)
    ready = 0
    cov_total = 0
    fail_categories: dict[str, int] = {}
    print(f"\n  {'protocol':>10} {'type':>14} {'QC':>10} {'coverage':>9}")
    for row in rows:
        st = row["study_type"]
        pc, sec = row["population_config"], row["sections"]
        if isinstance(pc, str):
            pc = json.loads(pc)
        if isinstance(sec, str):
            sec = json.loads(sec)
        qc = run_qc(study_type=st, population_config=pc or {}, sections=sec or {},
                    expected_sections=expected(st)).to_dict()
        cov = compute_coverage(study_type=st, population_config=pc or {}, sections=sec or {}).to_dict()
        if qc["submission_ready"]:
            ready += 1
        cov_total += cov["coverage_pct"]
        for f in qc["findings"]:
            if f["status"] == "fail":
                fail_categories[f["category"]] = fail_categories.get(f["category"], 0) + 1
        qc_str = "READY" if qc["submission_ready"] else f"{qc['counts']['fail']} fail"
        print(f"  {str(row['id'])[:8]:>10} {st:>14} {qc_str:>10} {cov['coverage_pct']:>8}%")

    print(f"\n  → {n} protocol(s): {ready}/{n} QC-ready, "
          f"avg coverage {round(cov_total / n)}%.")
    if fail_categories:
        top = sorted(fail_categories.items(), key=lambda kv: -kv[1])
        print("  → most common QC failures: " +
              ", ".join(f"{cat} ({cnt})" for cat, cnt in top))


def main() -> int:
    import asyncio
    biostat_failures = eval_biostatistics()
    asyncio.run(eval_protocols())

    _hr("SUMMARY")
    if biostat_failures == 0:
        print("  ✓ Biostatistics engine matches the reference table within tolerance.")
    else:
        print(f"  ✗ {biostat_failures} biostatistics reference(s) FAILED — investigate the engine.")
    return 1 if biostat_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
