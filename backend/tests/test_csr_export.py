"""Tests for the ICH E3 Clinical Study Report (CSR) exporter.

These exercise the deterministic-numbers-rendered-verbatim contract: the pivotal
bioequivalence table must show the EXACT point estimate and 90% CI bounds from
the ``results`` object, and the document must build for the error-metric and
NCA cases without crashing.
"""
from __future__ import annotations

import copy
import io
from datetime import datetime

import docx

from services.csr_export import build_csr_docx, build_csr_pdf, _strip_narrative_tables
from services.csr_sections import CSR_SECTION_TITLES


# ---------------------------------------------------------------------------
# Sample fixtures — a bioequivalent 2x2 crossover
# ---------------------------------------------------------------------------


def _sample_results() -> dict:
    """A realistic, LOCKED results object for a bioequivalent crossover.

    Percentages are chosen to already sit at 2-decimal precision so the rendered
    string equals the source value verbatim (proving no transformation).
    """
    return {
        "input_kind": "params",
        "design_key": "crossover",
        "is_nti": False,
        "co_primary": ["cmax", "auc0inf"],
        "verdict": "bioequivalent",
        "disposition": {
            "n_subjects": 24,
            "n_rows": 48,
            "treatments": ["Test", "Reference"],
            "periods": [1, 2],
            "sequences": ["TR", "RT"],
        },
        "be": {
            "cmax": {
                "metric": "cmax", "n": 24,
                "point_estimate_pct": 101.23,
                "ci_lower_pct": 95.10, "ci_upper_pct": 108.44,
                "cv_intra_pct": 18.42, "df": 22, "se_ln": 0.031,
                "within_be_limits": True,
                "lower_limit_pct": 80.00, "upper_limit_pct": 125.00,
            },
            "auc0t": {
                "metric": "auc0t", "n": 24,
                "point_estimate_pct": 100.87,
                "ci_lower_pct": 96.55, "ci_upper_pct": 105.36,
                "cv_intra_pct": 14.10, "df": 22, "se_ln": 0.026,
                "within_be_limits": True,
                "lower_limit_pct": 80.00, "upper_limit_pct": 125.00,
            },
            "auc0inf": {
                "metric": "auc0inf", "n": 24,
                "point_estimate_pct": 101.05,
                "ci_lower_pct": 96.88, "ci_upper_pct": 105.42,
                "cv_intra_pct": 13.75, "df": 22, "se_ln": 0.025,
                "within_be_limits": True,
                "lower_limit_pct": 80.00, "upper_limit_pct": 125.00,
            },
        },
    }


def _sample_sections() -> dict:
    return {
        "title": "A Single-Dose, Randomized, Two-Way Crossover Bioequivalence Study of "
                 "Metformin Test versus Reference in Healthy Adult Subjects",
        "synopsis": "<p>Objective: demonstrate bioequivalence of test versus reference "
                    "metformin. 24 subjects were enrolled in a crossover design.</p>",
        "introduction": "<p>Metformin is a widely used biguanide. This study evaluated "
                        "bioequivalence per FDA and ICH M13A guidance.</p>",
        "study_objectives": "<p>The primary objective was to demonstrate bioequivalence "
                            "by comparing Cmax and AUC.</p>",
        "investigational_plan": "<p>Open-label, randomized, single-dose, two-treatment, "
                                "two-period crossover with log-transformed ANOVA.</p>",
        "study_population": "<p>Healthy adult volunteers aged 18-45 were enrolled.</p>",
        "pharmacokinetic_results": "<p>Exposure metrics Cmax and AUC0-inf were "
                                   "summarized for test and reference.</p>",
        "bioequivalence_assessment": "<p>The geometric mean ratios and 90% confidence "
                                     "intervals fell within the acceptance range.</p>",
        "safety_results": "<p>No serious adverse events were reported.</p>",
        "discussion_conclusions": "<p>The test product was bioequivalent to the "
                                  "reference product.</p>",
        "references": "<p>Applicable regulatory references are listed below.</p>",
    }


def _sample_meta() -> dict:
    return {
        "drug_inn": "metformin",
        "protocol_code": "MET-001",
        "sponsor": "Acme Generics, Inc.",
        "study_type": "Bioequivalence",
        "version": "1.0",
        "created_at": datetime(2026, 7, 24, 12, 0, 0),
    }


def _docx_text(document: "docx.document.Document") -> str:
    """All visible text (paragraphs + table cells) from a python-docx Document."""
    parts = [p.text for p in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                parts.append(cell.text)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_build_csr_docx_renders_locked_numbers():
    data = build_csr_docx(
        results=_sample_results(),
        sections=_sample_sections(),
        meta=_sample_meta(),
    )
    assert isinstance(data, (bytes, bytearray))
    assert len(data) > 0

    document = docx.Document(io.BytesIO(data))
    text = _docx_text(document)
    lower = text.lower()

    # Study drug present
    assert "metformin" in lower

    # Verdict wording present
    assert "bioequivalent" in lower

    # Section titles from the taxonomy are rendered as headings
    for key in ("introduction", "pharmacokinetic_results", "bioequivalence_assessment"):
        assert CSR_SECTION_TITLES[key] in text, f"missing section title for {key}"

    # The pivotal BE table renders the EXACT locked numbers, verbatim
    assert "101.23" in text          # cmax point estimate
    assert "95.10" in text           # cmax CI lower bound
    assert "108.44" in text          # cmax CI upper bound
    assert "100.87" in text          # auc0t point estimate
    assert "101.05" in text          # auc0inf point estimate

    # Disposition count rendered
    assert "24" in text


def test_build_csr_pdf_returns_pdf_bytes():
    data = build_csr_pdf(
        results=_sample_results(),
        sections=_sample_sections(),
        meta=_sample_meta(),
    )
    assert isinstance(data, (bytes, bytearray))
    assert data[:4] == b"%PDF"


def test_build_csr_docx_handles_metric_error():
    results = copy.deepcopy(_sample_results())
    # A co-primary metric that could not be analysed -> error string, not a value.
    results["be"]["auc0inf"] = {"metric": "auc0inf", "error": "lambda_z not estimable"}
    results["verdict"] = "indeterminate"

    data = build_csr_docx(
        results=results,
        sections=_sample_sections(),
        meta=_sample_meta(),
    )
    assert len(data) > 0
    document = docx.Document(io.BytesIO(data))
    text = _docx_text(document)

    # Error text is rendered in place of the numbers, no crash. (The reused PK-
    # notation normalizer canonicalizes "lambda_z" -> "λz", so match the stable
    # tail of the message rather than the raw token.)
    assert "not estimable" in text
    # The remaining good metric still renders verbatim
    assert "101.23" in text
    # Indeterminate verdict wording present
    assert "indeterminate" in text.lower()


def test_build_csr_docx_with_nca_includes_pk_summary():
    results = copy.deepcopy(_sample_results())
    results["input_kind"] = "nca"
    results["disposition"]["n_blq"] = 3
    results["disposition"]["lloq"] = 0.05
    results["nca"] = {
        "1|1": {"subject": 1, "period": 1, "treatment": "Test",
                "cmax": 1.10, "tmax": 2.5, "auc0t": 10.5, "auc0inf": 11.2,
                "lambda_z": 0.11, "thalf": 6.3, "pct_extrap": 6.2},
        "1|2": {"subject": 1, "period": 2, "treatment": "Reference",
                "cmax": 1.05, "tmax": 2.5, "auc0t": 10.1, "auc0inf": 10.9,
                "lambda_z": 0.10, "thalf": 6.9, "pct_extrap": 7.3},
        "2|1": {"subject": 2, "period": 1, "treatment": "Reference",
                "cmax": 1.02, "tmax": 3.0, "auc0t": 9.8, "auc0inf": 10.4,
                "lambda_z": 0.10, "thalf": 6.5, "pct_extrap": 5.8},
        "2|2": {"subject": 2, "period": 2, "treatment": "Test",
                "cmax": 1.15, "tmax": 2.0, "auc0t": 11.0, "auc0inf": 11.7,
                "lambda_z": 0.12, "thalf": 5.9, "pct_extrap": 6.0},
    }

    data = build_csr_docx(
        results=results,
        sections=_sample_sections(),
        meta=_sample_meta(),
    )
    assert len(data) > 0
    document = docx.Document(io.BytesIO(data))
    text = _docx_text(document)

    # PK parameter summary table rendered (geometric means by treatment)
    assert "Pharmacokinetic Parameter Summary" in text
    assert "Geometric Mean" in text
    # Both treatments appear in the summary
    assert "Test" in text
    assert "Reference" in text

    # PDF path also builds for the NCA case
    pdf = build_csr_pdf(
        results=results,
        sections=_sample_sections(),
        meta=_sample_meta(),
    )
    assert pdf[:4] == b"%PDF"


def test_strip_narrative_tables_removes_tables_keeps_prose():
    """Unit contract for the render-layer guard: any <table> the AI narrative
    emits is removed (case/attribute-insensitive) while prose is untouched."""
    assert _strip_narrative_tables(
        "<p>keep</p><table><tr><td>x</td></tr></table>") == "<p>keep</p>"
    assert "<table" not in _strip_narrative_tables(
        "<TABLE class='t'>a</TABLE>mid<table>b</table>").lower()
    assert _strip_narrative_tables("<p>prose only</p>") == "<p>prose only</p>"
    assert _strip_narrative_tables(None) == ""


def test_narrative_table_does_not_duplicate_deterministic_table():
    """Every results table is rendered deterministically by the exporter. If the
    AI narrative ALSO emits a results table, the document must NOT show a
    duplicate: rendering the same locked results with vs without a stray
    narrative table must yield the SAME number of tables, and the deterministic
    numbers must still render."""
    base_sections = _sample_sections()
    stray_sections = dict(base_sections)
    stray_sections["bioequivalence_assessment"] = (
        base_sections["bioequivalence_assessment"]
        + "<table><thead><tr><th>Parameter</th><th>GMR (%)</th><th>90% CI</th>"
          "</tr></thead><tbody>"
          "<tr><td>Cmax</td><td>101.23</td><td>95.10-108.44</td></tr>"
          "<tr><td>AUC0-inf</td><td>101.05</td><td>96.88-105.42</td></tr>"
          "</tbody></table>"
    )

    n_base = len(docx.Document(io.BytesIO(build_csr_docx(
        results=_sample_results(), sections=base_sections, meta=_sample_meta()))).tables)
    doc_stray = docx.Document(io.BytesIO(build_csr_docx(
        results=_sample_results(), sections=stray_sections, meta=_sample_meta())))
    n_stray = len(doc_stray.tables)

    assert n_stray == n_base, (
        f"stray narrative table not stripped: {n_stray} tables vs {n_base} baseline")

    # The deterministic BE table still renders the exact locked numbers.
    text = _docx_text(doc_stray)
    assert "101.23" in text and "95.10" in text
