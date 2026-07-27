"""Unit tests for services.dataset_ingest.

These are pure in-memory tests: no database, no network, no Anthropic. They
exercise the two template generators and the strict parser/validator, with a
particular focus on the data-integrity rules (BLQ != 0, per-row error
accumulation, duplicate-sample detection, treatment normalisation).
"""

from __future__ import annotations

import io
import re

import openpyxl
import pytest

from services.dataset_ingest import (
    OPTIONAL_COLUMNS,
    REQUIRED_COLUMNS,
    ParsedDataset,
    ValidationError,
    build_template_csv,
    build_template_xlsx,
    parse_dataset,
    template_columns,
)

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _csv_bytes(header: list[str], rows: list[list]) -> bytes:
    lines = [",".join(str(c) for c in header)]
    lines += [",".join("" if c is None else str(c) for c in row) for row in rows]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _nca_csv(rows: list[list]) -> bytes:
    return _csv_bytes(REQUIRED_COLUMNS["nca"], rows)


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


def test_template_columns_include_optional_for_params():
    assert template_columns("nca") == REQUIRED_COLUMNS["nca"]
    assert template_columns("params") == REQUIRED_COLUMNS["params"] + OPTIONAL_COLUMNS["params"]
    assert "tmax" in template_columns("params") and "thalf" in template_columns("params")


def test_template_columns_rejects_unknown_kind():
    with pytest.raises(ValueError):
        template_columns("bogus")


def test_nca_csv_template_has_comment_and_parses_to_one_example_row():
    raw = build_template_csv("nca")
    text = raw.decode("utf-8")
    # A leading '#' comment documenting the BLQ/LLOQ convention must be present.
    assert text.startswith("#")
    assert "BLQ" in text
    # The parser must skip the comment lines and read exactly the example row.
    parsed = parse_dataset(raw, "nca_template.csv", "nca")
    assert parsed.summary["n_rows"] == 1
    assert parsed.rows[0]["concentration"] == 12.34
    assert parsed.rows[0]["treatment"] == "T"


# ---------------------------------------------------------------------------
# Round-trip: CSV (params)
# ---------------------------------------------------------------------------


def test_roundtrip_csv_params():
    template = build_template_csv("params")  # header + one example row (SUBJ-01)
    extra = (
        "SUBJ-02,RT,1,R,10.1,40.2,44.0,1.2,5.9\n"
        # lower-case 't' must normalise to 'T'; blank optional tmax/thalf → None
        "SUBJ-03,RT,1,t,11.5,42.0,46.3,,\n"
    ).encode("utf-8")
    data = template + extra

    parsed = parse_dataset(data, "params.csv", "params")
    assert isinstance(parsed, ParsedDataset)
    assert parsed.kind == "params"

    # Summary counts.
    assert parsed.summary["n_rows"] == 3
    assert parsed.summary["n_subjects"] == 3
    assert parsed.summary["subjects"] == ["SUBJ-01", "SUBJ-02", "SUBJ-03"]

    # Treatments normalised to the {T, R} domain (upper-case).
    assert parsed.summary["treatments"] == ["R", "T"]
    subj03 = next(r for r in parsed.rows if r["subject"] == "SUBJ-03")
    assert subj03["treatment"] == "T"  # 't' → 'T'
    assert subj03["tmax"] is None and subj03["thalf"] is None  # blank optionals

    # Typed numerics.
    subj02 = next(r for r in parsed.rows if r["subject"] == "SUBJ-02")
    assert subj02["cmax"] == 10.1 and isinstance(subj02["cmax"], float)
    assert subj02["auc0inf"] == 44.0

    # sha256 is a 64-char hex string and stable across identical bytes.
    assert _HEX64.match(parsed.sha256)
    parsed_again = parse_dataset(data, "params.csv", "params")
    assert parsed.sha256 == parsed_again.sha256


# ---------------------------------------------------------------------------
# Round-trip: XLSX (nca), incl. BLQ handling
# ---------------------------------------------------------------------------


def test_xlsx_template_is_zip_and_nonempty():
    raw = build_template_xlsx("nca")
    assert raw and raw[:4] == b"PK\x03\x04"  # .xlsx is a ZIP container


def test_roundtrip_xlsx_nca_blq_and_numeric():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(REQUIRED_COLUMNS["nca"])
    ws.append(["S1", "TR", 1, "T", 0.0, "BLQ"])   # BLQ token → None (not 0)
    ws.append(["S1", "TR", 1, "T", 0.5, 12.3])    # numeric → float
    bio = io.BytesIO()
    wb.save(bio)

    parsed = parse_dataset(bio.getvalue(), "study.xlsx", "nca")
    assert parsed.summary["n_rows"] == 2
    assert parsed.summary["n_blq"] == 1

    blq_row = parsed.rows[0]
    num_row = parsed.rows[1]
    assert blq_row["concentration"] is None            # BLQ must NOT become 0
    assert num_row["concentration"] == 12.3
    assert isinstance(num_row["concentration"], float)
    assert num_row["period"] == 1 and isinstance(num_row["period"], int)


def test_nca_lloq_below_limit_becomes_none():
    # A numeric value strictly below the supplied LLOQ is treated as BLQ (None);
    # a value at/above it is kept.
    data = _nca_csv([
        ["S1", "TR", 1, "T", 0.0, 0.02],   # below lloq → None
        ["S1", "TR", 1, "T", 0.5, 5.0],    # above lloq → kept
    ])
    parsed = parse_dataset(data, "x.csv", "nca", lloq=0.1)
    assert parsed.rows[0]["concentration"] is None
    assert parsed.rows[1]["concentration"] == 5.0
    assert parsed.summary["lloq"] == 0.1


# ---------------------------------------------------------------------------
# Error handling — each must raise ValidationError with an informative message
# ---------------------------------------------------------------------------


def test_error_missing_required_column():
    header = ["subject", "sequence", "period", "treatment", "cmax", "auc0t"]  # no auc0inf
    data = _csv_bytes(header, [["SUBJ-01", "TR", 1, "T", 1.0, 2.0]])
    with pytest.raises(ValidationError) as exc:
        parse_dataset(data, "params.csv", "params")
    assert "auc0inf" in str(exc.value)


def test_error_non_numeric_concentration_not_blq_token():
    data = _nca_csv([["S1", "TR", 1, "T", 0.5, "oops"]])
    with pytest.raises(ValidationError) as exc:
        parse_dataset(data, "x.csv", "nca")
    msg = str(exc.value)
    assert "concentration" in msg and "oops" in msg


def test_error_treatment_not_t_or_r():
    data = _nca_csv([["S1", "TR", 1, "X", 0.5, 10.0]])
    with pytest.raises(ValidationError) as exc:
        parse_dataset(data, "x.csv", "nca")
    assert "treatment" in str(exc.value) and "T or R" in str(exc.value)


def test_error_empty_subject():
    data = _nca_csv([["", "TR", 1, "T", 0.5, 10.0]])
    with pytest.raises(ValidationError) as exc:
        parse_dataset(data, "x.csv", "nca")
    assert "subject" in str(exc.value) and "non-empty" in str(exc.value)


def test_error_period_not_integer():
    data = _nca_csv([["S1", "TR", "abc", "T", 0.5, 10.0]])
    with pytest.raises(ValidationError) as exc:
        parse_dataset(data, "x.csv", "nca")
    assert "period" in str(exc.value) and "positive integer" in str(exc.value)


def test_error_duplicate_subject_period_time_in_nca():
    data = _nca_csv([
        ["S1", "TR", 1, "T", 0.5, 10.0],
        ["S1", "TR", 1, "T", 0.5, 11.0],  # same (subject, period, time)
    ])
    with pytest.raises(ValidationError) as exc:
        parse_dataset(data, "x.csv", "nca")
    assert "duplicate" in str(exc.value).lower()


def test_errors_accumulate_across_rows():
    data = _nca_csv([
        ["S1", "TR", 1, "X", 0.5, 10.0],    # bad treatment
        ["", "TR", 1, "T", 0.5, 10.0],      # empty subject
        ["S3", "TR", "abc", "T", 0.5, 10.0],  # bad period
    ])
    with pytest.raises(ValidationError) as exc:
        parse_dataset(data, "x.csv", "nca")
    # All three independent failures must be reported, not just the first.
    assert len(exc.value.errors) >= 3


def test_error_unsupported_extension():
    with pytest.raises(ValidationError) as exc:
        parse_dataset(b"subject\nS1\n", "data.txt", "nca")
    assert "unsupported" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# AE kind — boolean coercion
# ---------------------------------------------------------------------------


def test_ae_boolean_coercion():
    data = _csv_bytes(
        REQUIRED_COLUMNS["ae"],
        [["S1", "Headache", "Mild", "yes", "no", 2, "Recovered"]],
    )
    parsed = parse_dataset(data, "ae.csv", "ae")
    row = parsed.rows[0]
    assert row["serious"] is True      # "yes" → True
    assert row["related"] is False     # "no"  → False
    assert row["start_day"] == 2 and isinstance(row["start_day"], int)
    assert row["severity"] == "Mild" and row["outcome"] == "Recovered"


def test_ae_bad_boolean_rejected():
    data = _csv_bytes(
        REQUIRED_COLUMNS["ae"],
        [["S1", "Headache", "Mild", "maybe", "no", 2, "Recovered"]],
    )
    with pytest.raises(ValidationError) as exc:
        parse_dataset(data, "ae.csv", "ae")
    assert "serious" in str(exc.value)


# ---------------------------------------------------------------------------
# Extra columns preserved
# ---------------------------------------------------------------------------


def test_extra_columns_preserved_verbatim():
    header = REQUIRED_COLUMNS["nca"] + ["site"]
    data = _csv_bytes(header, [["S1", "TR", 1, "T", 0.5, 10.0, "Site-A"]])
    parsed = parse_dataset(data, "x.csv", "nca")
    assert parsed.rows[0]["site"] == "Site-A"
    assert "site" in parsed.columns
