"""Word (.docx) export for clinical study budgets.

Structure mirrors the three real signed/RFP CRO budget documents that
services/budget_rate_card.py was calibrated against: an Assumptions section
(the header-block numbers plus a full provenance table — "nothing is silently
invented"), a Summary Study Budget table (category subtotals rolling up
through the discount to the grand total, exactly like compute_rollups), and an
Itemized Study Budget appendix (every line item, per category, with the
No. of Units / Unit Cost / Unit Description / Total columns real CRO budgets
use), followed by a Basis of Estimate section that surfaces the engine's
disclaimer, warnings and discount note verbatim.

Deliberately reuses the protocol/companion exporter's DOCX scaffolding
(routers/export.py) rather than inventing a new visual style, so a budget
export looks like every other document this app produces — same page setup,
header/footer, cover page and table styling.

PURE / SYNCHRONOUS: build_budget_docx only reads attributes and relationships
that the caller has already eagerly loaded (get_budget_scoped(load_tree=True)
loads budget.categories -> line_items). It deliberately never touches
budget.protocol (not eagerly loaded by that call) to avoid a MissingGreenlet
from a lazy load under the async session — anything about the linked protocol
that this document needs is read from budget.derivation instead, which already
carries protocol_id / protocol_linked / per-assumption provenance.

---------------------------------------------------------------------------
PATIENT-PROFILE SUPPORT — a note on how this module reads the model
---------------------------------------------------------------------------
This module also renders PATIENT budgets (multi-site/multi-country, phase-
based, with investigator-grant and software-licensing buckets and
cited-research figures) alongside the original HEALTHY VOLUNTEER budget.

`models.py` already defines everything this module needs directly: the real
`Budget.subject_profile` column (`BudgetSubjectProfile` enum, values
"healthy"/"patient"), the two new `BudgetBucket` members
(`investigator_grants`, `software`), and every patient-specific driver column
(`indication`, `therapeutic_area`, `n_countries`, `n_sites_selected`,
`dropout_rate_pct`, `n_completed`, the five `*_months` phase columns,
`n_expected_saes`/`n_expected_susars`, `n_monitoring_visits_onsite/_remote`,
`n_pharmacy_visits`, `n_visits_per_patient`, `n_protocol_amendments`,
`geography_profile`/`geography_factor`, `archival_retention_years`,
`bicr_required`, `comparator_sourced_by_sponsor`). services/budget_engine.py
resolves every one of these onto the SAME column names and mirrors them into
`Budget.derivation["assumptions"][<column name>]` — confirmed by reading its
`resolve_patient_drivers`-style code directly rather than trusting the plan
text, per this task's instructions. So every lookup in this module goes
through `_assumption_value(budget, assumptions, "<real column/assumption
name>")` — column first, `derivation["assumptions"][name]["value"]` second —
using the actual names above, not guessed aliases.

UPDATE (first integration pass): the state described above was accurate when
this module was written against its sibling files' in-progress state, but as
of the integration pass that reconciled all four agents' work,
`services/budget_rate_card.py` and `services/budget_engine.py` now DO define
everything this module was written defensively around: `PATIENT_RATE_CARD`,
`PROVENANCE_TIERS`, `SOURCE_REGISTRY`, `GEOGRAPHY_FACTORS`,
`PATIENT_STUDY_PROFILES` all exist, `compute_rollups()` adds
`investigator_grants_total`/`software_total` to the rollup dict and grand
total, and `auto_populate_budget()` writes a profile-aware
`derivation["disclaimer"]`/`["rate_card_version"]` and per-line
`provenance_tier`/`source_id`/`unit_cost_published`. This module's defensive
reads below are harmless now that the data they defend against missing is
actually present — they simply never trigger their fallback branches — and
are left in place rather than stripped out, since a defensive `.get(...) or
{}` costs nothing once the key is always there:

  - `_is_patient_budget()` reads the real `budget.subject_profile` and
    compares its `.value` to "patient" — this is unaffected by any of the
    above and is exactly right today for both profiles.
  - `rollup.get("investigator_grants_total"/"software_total", 0.0)` — Section
    2.0's bucket rows are fully data-driven off the rollup dict (the actual
    fix for the "silently dropped from the summary" bug) and light up the
    moment `compute_rollups()` adds those two keys; until then a patient
    budget's summary bucket rows for these two buckets under-count (they show
    0.00 even though the per-category rows above them, which sum
    independently off `category_subtotals`, are correct) and `GRAND TOTAL`
    likewise under-counts — this is a `budget_engine.py` defect, not
    something this file can safely paper over (it would make the exported
    number disagree with the number every other part of the app shows for
    the same budget).
  - The disclaimer and "Rate-card version" display are the one place this
    module does NOT simply trust `budget.derivation` for a patient budget:
    both are picked directly from `subject_profile` (`PATIENT_DISCLAIMER` /
    `PATIENT_RATE_CARD_VERSION`) rather than the current always-healthy
    `derivation["disclaimer"]`/`derivation["rate_card_version"]`, so a
    patient document is not wrong about which card and disclaimer it is.
  - `derivation["sources"]`, `["provenance_summary"]`, `["bid_reconciliation"]`
    and per-line `provenance_tier`/`source_id` are all read with `.get(...)
    or {}`/`None` defaults and render an honest "not yet recorded" state
    (empty Sources table, no provenance sentence, "Estimated" badge) rather
    than crashing or inventing a citation, so this document is already
    correct — just less rich — before those keys exist.

Net effect: the HEALTHY VOLUNTEER document is unaffected by any of this (the
healthy render path never reads a patient-only field and was verified
paragraph-for-paragraph and table-cell-for-table-cell identical to the
pre-patient-support version of this module for a representative healthy
budget), and the patient branches read every field this codebase actually
has under its actual name today, degrading honestly wherever a sibling
module's own patient-profile work has not landed yet.
"""
from __future__ import annotations

import io
from datetime import datetime, timezone
from typing import Any

from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

from models import Budget, BudgetDiscountType, BudgetLineStatus
from routers.export import (
    _companion_cover_docx,
    _companion_docx,
    _companion_footer_docx,
    _companion_header_docx,
    _repeat_header_row_docx,
    _ss_kv_table_docx,
    _ss_para,
)
from services import budget_rate_card

_STUDY_TYPE_LABELS = {
    "BE": "Bioequivalence Study",
    "biosimilar_pk": "Biosimilar PK Similarity Study",
    "food_effect": "Food-Effect Study",
}

# The real, observed CRO vocabulary for "this row exists but has no dollar
# total" — mirrors models.BudgetLineStatus and is used wherever a non-costed
# line/category needs to print its status instead of a number.
_STATUS_LABEL = {
    "out_of_scope": "Out of Scope",
    "not_applicable": "Not Applicable",
    "not_planned": "Not Planned",
    "outsourced": "Outsourced to Vendor",
    "actuals_only": "Actuals incurred",
}

# Budget.derivation["assumptions"][k]["source"] -> a human-readable provenance
# label. Any source value not listed here (the engine is free to add new ones)
# falls back to a title-cased rendering of the raw value rather than dropping
# the row — the provenance table's whole point is that nothing is silently
# invented, including in the source column itself.
_SOURCE_LABEL = {
    "protocol": "Derived from linked protocol",
    "user": "Entered by you",
    "default": "Rate-card default for this study type",
    "computed": "Computed from the assumptions above",
    "inferred_from_modality": "Inferred from the drug's modality",
    "inferred_from_safety_profile": "Inferred from the product's safety profile",
    "phase_scaled": "Scaled from the total duration you set",
    "kb_default": "Curated knowledge-base default",
}

# Fallback text used only if services/budget_rate_card.py has not yet defined
# the corresponding constant (see module docstring). Verbatim from spec so
# behaviour is identical once that module catches up — this is a safety net,
# not a second source of truth.
_FALLBACK_PATIENT_DISCLAIMER = (
    "Figures in this budget come from three sources, each labelled on every line. "
    "RESEARCHED figures are real published rates — institutional fee schedules, "
    "manufacturer list prices, government schedules and peer-reviewed benchmarks — "
    "cited in full in the Sources section. BID-CALIBRATED figures are unit rates taken "
    "from real CRO bids for a comparable patient oncology study; they are one vendor's "
    "price, not a market benchmark. ESTIMATED figures are approximations for items no "
    "public price exists for, and are flagged as such. Nothing here is a quotation, a "
    "bid, or a negotiated price. Every number is editable."
)

_FALLBACK_PROVENANCE_TIERS = {
    "published_rate": "Published rate — read directly from an official fee schedule, "
                       "manufacturer price list, government schedule or peer-reviewed "
                       "benchmark.",
    "derived": "Derived — triangulated by us from two or more published figures. "
               "Not itself a published benchmark.",
    "bid_calibrated": "Bid-calibrated — a unit rate taken from a real CRO bid for a "
                       "comparable study. A single vendor's price, not a market benchmark.",
    "estimate": "Estimate — a rate-card approximation. No public figure exists for "
                "this item; treat it as an assumption to be replaced by a quote.",
}

# provenance_tier (on a line's derivation record) -> the short badge word that
# goes in Section 3.0's "Provenance" column for a patient budget.
_TIER_BADGE = {
    "published_rate": "Researched",
    "derived": "Derived",
    "bid_calibrated": "CRO bid",
    "estimate": "Estimated",
}

# Bucket value -> a readable label, used for patient category headings
# ("N.00 Name — Bucket Label") and, with slightly different wording, for the
# Section 2.0 rollup row labels (see _BUCKET_ROLLUP_ROWS below).
_BUCKET_DISPLAY_LABEL = {
    "direct": "Professional Fees",
    "stipend": "Subject Costs",
    "variable": "CRO/Vendor Variable Costs",
    "pass_through": "Pass-Through Costs",
    "investigator_grants": "Investigator Grants and Site Fees",
    "software": "Software Licensing and Hosting",
}

# Section 2.0 rollup rows, data-driven off the rollup dict rather than
# hardcoded — the load-bearing fix for patient budgets, which add two buckets
# (investigator_grants, software) that would otherwise be silently dropped
# from the summary while still counting toward the grand total.
#
# Each entry is (rollup dict key, category bucket value, healthy-budget label
# or None, patient-budget label). The healthy label is None for the two new
# buckets: a healthy budget has no categories in those buckets and its rollup
# dict carries no such keys, so those rows are skipped regardless — but the
# explicit None also documents that these rows must never appear on a
# healthy-volunteer document. The healthy labels for the other four buckets
# are the EXACT strings this module has always printed, so that a
# healthy-volunteer document keeps rendering byte-for-byte identically; the
# patient labels are the new spec's vocabulary ("Total Professional Fees",
# "Subject Costs", "Pass-Through Costs" without "(actuals)") — a deliberate,
# spec-directed divergence from the healthy wording that only ever fires when
# the budget is actually a patient budget.
_BUCKET_ROLLUP_ROWS: tuple[tuple[str, str, str | None, str], ...] = (
    ("total_direct_costs", "direct", "Total Direct Costs", "Total Professional Fees"),
    ("software_total", "software", None, "Software Licensing and Hosting"),
    ("subject_stipend_total", "stipend", "Subject Stipend", "Subject Costs"),
    ("variable_total", "variable", "CRO/Vendor Variable Costs (actuals)", "CRO/Vendor Variable Costs (actuals)"),
    ("pass_through_total", "pass_through", "Pass-Through Costs (actuals)", "Pass-Through Costs"),
    ("investigator_grants_total", "investigator_grants", None, "Investigator Grants and Site Fees"),
)


def _money(v: float | None) -> str:
    return "" if v is None else f"{v:,.2f}"


def _cellstr(v: Any) -> str:
    return "—" if v is None else str(v)


def _yesno(v: Any) -> str:
    if isinstance(v, bool):
        return "Yes" if v else "No"
    if isinstance(v, str):
        low = v.strip().lower()
        if low in ("true", "yes", "y", "1"):
            return "Yes"
        if low in ("false", "no", "n", "0"):
            return "No"
        return v
    return _cellstr(v)


def _source_label(source: str | None) -> str:
    if not source:
        return "—"
    return _SOURCE_LABEL.get(source, source.replace("_", " ").title())


def _assumption_value(budget: Budget, assumptions: dict, field: str) -> Any:
    """budget.* first (the columns _resolve_drivers writes every resolved
    driver back to), derivation["assumptions"][field]["value"] second — the
    same resolution order the engine itself uses, so the document can never
    show a number that disagrees with what was actually priced.

    Deliberately tolerant of `field` naming nothing on the model at all
    (getattr's default handles that): this is how today's healthy-volunteer
    card already renders drivers like doses_per_subject / n_pk_timepoints /
    modality, which have never had a dedicated Budget column, and it is the
    same mechanism patient-only drivers (sites, phases, indication, ...) will
    use once services/budget_engine.py starts writing them."""
    v = getattr(budget, field, None)
    if v is not None:
        return v
    entry = assumptions.get(field)
    if isinstance(entry, dict) and entry.get("value") is not None:
        return entry["value"]
    return None


def _assumption_first(budget: Budget, assumptions: dict, *fields: str) -> Any:
    """Like _assumption_value, but tries several plausible key names in
    order and returns the first that resolves. Used for patient drivers whose
    exact key name in derivation["assumptions"] is owned by a parallel
    engine change this file cannot see yet — trying a short list of the
    spec's own vocabulary keeps this file correct regardless of which of a
    few reasonable names the engine ultimately picks."""
    for f in fields:
        v = _assumption_value(budget, assumptions, f)
        if v is not None:
            return v
    return None


def _is_patient_budget(budget: Budget, assumptions: dict) -> bool:
    """True only for a real patient-profile budget. Reads budget.subject_profile
    defensively (see module docstring): works whether that ends up a real enum
    column, a plain string column, or (today) does not exist at all — in
    which case this is always False and every budget takes the
    healthy-volunteer path exactly as before."""
    raw = _assumption_value(budget, assumptions, "subject_profile")
    val = getattr(raw, "value", raw)
    return isinstance(val, str) and val.strip().lower() == "patient"


def _study_descriptor(budget: Budget, assumptions: dict) -> str:
    """The study-type phrase used in the header/cover. For a patient budget
    this becomes "<type> · Patient Study — <indication>"; for a healthy
    budget it stays the exact bare study-type label this module has always
    printed, so a healthy-volunteer document is unaffected."""
    base = _STUDY_TYPE_LABELS.get(budget.study_type, "Clinical Study")
    if _is_patient_budget(budget, assumptions):
        indication = _assumption_first(budget, assumptions, "indication")
        pop = "Patient Study" + (f" — {indication}" if indication else "")
        return f"{base} · {pop}"
    return base


def _geography_basis_text(budget: Budget, assumptions: dict) -> str:
    profile = _assumption_first(budget, assumptions, "geography_profile")
    factor = _assumption_first(budget, assumptions, "geography_factor")
    if profile is None and factor is None:
        return "—"
    profile_text = str(profile).replace("_", " ").title() if profile else "—"
    factor_text = f"{factor:.2f}" if isinstance(factor, (int, float)) else "—"
    return f"{profile_text} (× {factor_text})"


def _sites_summary_text(budget: Budget, assumptions: dict) -> str:
    # Real column/assumption-key names (models.Budget, services/budget_engine.py):
    # `n_sites` is sites ACTIVATED (shared with the healthy card's "Number of
    # study sites" row); `n_sites_selected` is sites CONTRACTED. There is no
    # "n_sites_activated" / "sites_activated" column or assumption key.
    activated = _assumption_value(budget, assumptions, "n_sites")
    selected = _assumption_value(budget, assumptions, "n_sites_selected")
    if activated is None and selected is None:
        return "—"
    return f"{_cellstr(activated)} / {_cellstr(selected)}"


def _provenance_badge(source_value: str | None, line_deriv: dict) -> str:
    """Section 3.0's "Provenance" column for a patient budget: "Yours" /
    "From protocol" come straight off BudgetLineSource; the citation-bearing
    values ("Researched" / "Derived" / "CRO bid" / "Estimated") come off the
    line's `provenance_tier` (see services/budget_rate_card.py's
    RateCardSource / PROVENANCE_TIERS design). Falls back to "Estimated" when
    no tier is recorded, which is the correct, honest default for a rate-card
    row with no cited source."""
    if source_value == "manual":
        return "Yours"
    if source_value == "protocol_derived":
        return "From protocol"
    tier = (line_deriv or {}).get("provenance_tier")
    return _TIER_BADGE.get(tier, "Estimated")


def _provenance_sentence(currency: str, grand_total: Any, prov: dict) -> str | None:
    """Builds the "Of the USD X estimated here, USD Y (Z%) rests on..."
    sentence from Budget.derivation["provenance_summary"], entirely from
    whatever numeric fields are actually present — never hardcoded, so it
    stays correct as the underlying figures change. Returns None (render
    nothing) if there isn't enough data to say anything honest."""
    if not isinstance(grand_total, (int, float)) or not grand_total:
        return None
    segments = (
        ("researched_value", "rests on published rates cited below"),
        ("bid_calibrated_value", "on unit rates from real CRO bids for a comparable study"),
        ("derived_value", "on figures we derived by triangulating published sources"),
        ("estimate_value", "on estimates for items no public price exists for"),
    )
    clauses: list[str] = []
    for key, phrase in segments:
        val = prov.get(key)
        if isinstance(val, (int, float)):
            pct = val / grand_total * 100
            clauses.append(f"{currency} {val:,.0f} ({pct:.1f}%) {phrase}")
    if not clauses:
        return None
    if len(clauses) == 1:
        body = clauses[0]
    else:
        body = ", ".join(clauses[:-1]) + ", and " + clauses[-1]
    return f"Of the {currency} {grand_total:,.0f} estimated here, {body}."


# ---------------------------------------------------------------------------
# Assumptions KV builders — profile-specific, sharing _assumption_value.
# ---------------------------------------------------------------------------


def _healthy_kv_rows(
    budget: Budget, assumptions: dict, *, sf_rate: Any, linked_text: str, modality: Any,
    rate_card_version: str,
) -> list[tuple[str, str]]:
    """The original 15 rows, byte-identical to the pre-patient-support
    version of this module."""
    return [
        ("Number of study sites", _cellstr(_assumption_value(budget, assumptions, "n_sites"))),
        ("Number of dosing groups/arms", _cellstr(_assumption_value(budget, assumptions, "n_dosing_groups"))),
        ("Subjects enrolled (dosed)", _cellstr(_assumption_value(budget, assumptions, "n_enrolled"))),
        ("Alternate subjects", _cellstr(_assumption_value(budget, assumptions, "n_alternates"))),
        ("Screen failures", _cellstr(_assumption_value(budget, assumptions, "n_screen_failures"))),
        ("Subjects screened (total)", _cellstr(_assumption_value(budget, assumptions, "n_screened"))),
        ("Screen-failure rate", f"{sf_rate:.1f}%" if isinstance(sf_rate, (int, float)) else "—"),
        ("Project duration (months)", _cellstr(_assumption_value(budget, assumptions, "project_duration_months"))),
        ("Doses per subject", _cellstr(_assumption_value(budget, assumptions, "doses_per_subject"))),
        ("PK sampling timepoints per subject", _cellstr(_assumption_value(budget, assumptions, "n_pk_timepoints"))),
        ("Immunogenicity (ADA) timepoints per subject", _cellstr(_assumption_value(budget, assumptions, "n_ada_timepoints"))),
        ("Distinct on-site visit days per subject", _cellstr(_assumption_value(budget, assumptions, "n_visit_days"))),
        ("Linked protocol", linked_text),
        ("Product modality", modality.replace("_", " ").title() if modality else "—"),
        ("Rate-card version", rate_card_version),
    ]


def _patient_kv_rows_pre_phase(budget: Budget, assumptions: dict, *, sf_rate: Any) -> list[tuple[str, str]]:
    dropout = _assumption_first(budget, assumptions, "dropout_rate_pct", "drop_out_rate_pct")
    dropout_text = f"{dropout:.1f}%" if isinstance(dropout, (int, float)) else "—"
    return [
        ("Indication", _cellstr(_assumption_first(budget, assumptions, "indication"))),
        ("Therapeutic area", _cellstr(_assumption_first(budget, assumptions, "therapeutic_area"))),
        ("Sites activated", _cellstr(_assumption_value(budget, assumptions, "n_sites"))),
        ("Sites contracted", _cellstr(_assumption_value(budget, assumptions, "n_sites_selected"))),
        ("Countries", _cellstr(_assumption_first(budget, assumptions, "n_countries", "countries"))),
        ("Patients enrolled (dosed)", _cellstr(_assumption_value(budget, assumptions, "n_enrolled"))),
        ("Patients screened", _cellstr(_assumption_value(budget, assumptions, "n_screened"))),
        ("Screen failures", _cellstr(_assumption_value(budget, assumptions, "n_screen_failures"))),
        ("Screen-failure rate", f"{sf_rate:.1f}%" if isinstance(sf_rate, (int, float)) else "—"),
        ("Drop-out rate", dropout_text),
        ("Patients completing", _cellstr(_assumption_value(budget, assumptions, "n_completed"))),
        ("Visits per patient", _cellstr(_assumption_value(budget, assumptions, "n_visits_per_patient"))),
    ]


def _patient_phase_rows(budget: Budget, assumptions: dict) -> list[tuple[str, Any]]:
    return [
        ("Start-up", _assumption_first(budget, assumptions, "startup_months", "n_startup_months")),
        ("Recruitment", _assumption_first(budget, assumptions, "recruitment_months", "n_recruitment_months")),
        ("Treatment", _assumption_first(budget, assumptions, "treatment_months", "n_treatment_months")),
        ("Follow-up", _assumption_first(budget, assumptions, "followup_months", "n_followup_months")),
        ("Close-out", _assumption_first(budget, assumptions, "closeout_months", "n_closeout_months")),
    ]


def _patient_kv_rows_post_phase(
    budget: Budget, assumptions: dict, *, linked_text: str, modality: Any,
    rate_card_version: str,
) -> list[tuple[str, str]]:
    return [
        ("Expected SAEs", _cellstr(_assumption_first(budget, assumptions, "n_expected_saes", "expected_saes"))),
        ("Expected SUSARs", _cellstr(_assumption_first(budget, assumptions, "n_expected_susars", "expected_susars"))),
        ("On-site monitoring visits", _cellstr(_assumption_first(
            budget, assumptions, "n_monitoring_visits_onsite", "n_imv_onsite",
        ))),
        ("Remote monitoring visits", _cellstr(_assumption_first(
            budget, assumptions, "n_monitoring_visits_remote", "n_imv_remote",
        ))),
        ("Pharmacy/drug-accountability visits", _cellstr(_assumption_first(
            budget, assumptions, "n_pharmacy_visits",
        ))),
        ("Protocol amendments", _cellstr(_assumption_value(budget, assumptions, "n_protocol_amendments"))),
        ("Geography basis", _geography_basis_text(budget, assumptions)),
        ("Archival retention (years)", _cellstr(_assumption_first(budget, assumptions, "archival_retention_years"))),
        ("BICR required", _yesno(_assumption_first(budget, assumptions, "bicr_required"))),
        ("Comparator sourced by sponsor", _yesno(_assumption_first(budget, assumptions, "comparator_sourced_by_sponsor"))),
        ("Linked protocol", linked_text),
        ("Product modality", modality.replace("_", " ").title() if modality else "—"),
        ("Rate-card version", rate_card_version),
    ]


# ---------------------------------------------------------------------------
# Grid table — a near-copy of routers.export._ss_grid_table_docx, extended
# with bold emphasis rows (category/grand-total subtotals) and right-aligned
# money columns. Everything else (Table Grid style, 1E3A5F header fill with
# white bold text, F0F4F8/FFFFFF zebra striping, Times New Roman, a repeating
# header row) is unchanged so the budget tables read as the same visual
# language as every other exported table in this app.
#
# A cell may be a plain str, OR a (main_text, suffix_text) tuple — used only
# for a patient budget's geo-scaled Unit Cost cells, which print the applied
# rate plus a small "(published × factor)" suffix in a smaller face. No
# existing caller passes a tuple, so this is purely additive.
# ---------------------------------------------------------------------------


def _budget_grid_docx(
    doc,
    rows: list[list[Any]],
    *,
    col_widths: list[Any] | None = None,
    font_pt: float = 9,
    emphasis_rows: frozenset[int] = frozenset(),
    strong_rows: frozenset[int] = frozenset(),
    right_align_cols: frozenset[int] = frozenset(),
) -> None:
    """rows[0] is the header. `emphasis_rows` / `strong_rows` are 0-based
    indices into the DATA rows (i.e. excluding the header) — emphasis_rows get
    bold text on an EAF1F8 fill (category subtotals), strong_rows get bold on
    D9E7F5 (the grand total). `right_align_cols` right-aligns money columns."""
    tbl = doc.add_table(rows=len(rows), cols=len(rows[0]))
    tbl.style = "Table Grid"
    col_w = Inches(6.75) / len(rows[0])
    for r_idx, row_data in enumerate(rows):
        row = tbl.rows[r_idx]
        is_hdr = r_idx == 0
        data_idx = r_idx - 1
        is_strong = (not is_hdr) and data_idx in strong_rows
        is_emphasized = (not is_hdr) and data_idx in emphasis_rows
        for c_idx, cell_text in enumerate(row_data):
            cell = row.cells[c_idx]
            cell.text = ""
            para = cell.paragraphs[0]
            para.paragraph_format.space_after = Pt(0)
            if c_idx in right_align_cols:
                para.alignment = WD_ALIGN_PARAGRAPH.RIGHT
            if isinstance(cell_text, tuple):
                main_text, suffix_text = cell_text
            else:
                main_text, suffix_text = cell_text, None
            rn = para.add_run(main_text)
            rn.font.name = "Times New Roman"
            rn.font.size = Pt(font_pt)
            shd = OxmlElement("w:shd")
            shd.set(qn("w:val"), "clear")
            shd.set(qn("w:color"), "auto")
            if is_hdr:
                shd.set(qn("w:fill"), "1E3A5F")
                cell._tc.get_or_add_tcPr().append(shd)
                rn.bold = True
                rn.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
            elif is_strong:
                shd.set(qn("w:fill"), "D9E7F5")
                cell._tc.get_or_add_tcPr().append(shd)
                rn.bold = True
            elif is_emphasized:
                shd.set(qn("w:fill"), "EAF1F8")
                cell._tc.get_or_add_tcPr().append(shd)
                rn.bold = True
            else:
                shd.set(qn("w:fill"), "F0F4F8" if r_idx % 2 == 0 else "FFFFFF")
                cell._tc.get_or_add_tcPr().append(shd)
            if suffix_text:
                rn2 = para.add_run(suffix_text)
                rn2.font.name = "Times New Roman"
                rn2.font.size = Pt(7)
                if is_hdr:
                    rn2.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
        for c_idx, cell in enumerate(row.cells):
            cell.width = (
                col_widths[c_idx] if col_widths and c_idx < len(col_widths) else col_w
            )
    _repeat_header_row_docx(tbl)
    doc.add_paragraph()


def _phase_duration_grid_docx(doc, budget: Budget, assumptions: dict) -> None:
    """The five phase-duration rows PLUS a bold total, as their own visually
    grouped block — phase-based pricing is the structural fact that
    distinguishes a patient budget, and a reader must be able to see at a
    glance which phase each monthly rate multiplies against."""
    phases = _patient_phase_rows(budget, assumptions)
    total = _assumption_value(budget, assumptions, "project_duration_months")
    rows: list[list[str]] = [["Study Phase", "Duration (months)"]]
    for label, val in phases:
        rows.append([label, _cellstr(val)])
    rows.append(["Total study duration", _cellstr(total)])
    _budget_grid_docx(
        doc, rows,
        col_widths=[Inches(3.35), Inches(3.4)],
        strong_rows=frozenset({len(rows) - 2}),
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def build_budget_docx(budget: Budget, rollup: dict[str, Any]) -> bytes:
    """Render `budget` (with categories/line_items eagerly loaded) and its
    already-computed `rollup` (budget_engine.compute_rollups(...)) as a .docx,
    returned as raw bytes. Pure and synchronous — safe to call from an async
    route without awaiting anything."""
    derivation = budget.derivation or {}
    assumptions: dict[str, dict] = derivation.get("assumptions") or {}
    categories = sorted(budget.categories, key=lambda c: c.sort_order)
    is_patient = _is_patient_budget(budget, assumptions)
    descriptor = _study_descriptor(budget, assumptions)

    if is_patient:
        # derivation["disclaimer"] is written by services/budget_engine.py's
        # single auto-population function, which (as of this change) always
        # stores the HEALTHY-volunteer budget_rate_card.DISCLAIMER regardless
        # of subject_profile — it is not yet profile-aware. Printing a patient
        # budget's disclaimer straight off the profile flag (rather than
        # trusting that stored value) means this document is correct the
        # moment subject_profile is set, independent of when the engine's own
        # disclaimer selection catches up; once it does, the two will simply
        # agree. The healthy branch below is untouched: it keeps trusting
        # derivation["disclaimer"] first exactly as before.
        disclaimer_text = getattr(budget_rate_card, "PATIENT_DISCLAIMER", _FALLBACK_PATIENT_DISCLAIMER)
    else:
        disclaimer_text = derivation.get("disclaimer") or budget_rate_card.DISCLAIMER

    # Same reasoning as the disclaimer above: derivation["rate_card_version"]
    # is currently always the HEALTHY RATE_CARD_VERSION, so a patient budget
    # prefers services/budget_rate_card.PATIENT_RATE_CARD_VERSION when it is
    # defined, and only falls back to the stored value if that constant is
    # ever removed. The healthy branch is exactly the pre-existing expression.
    if is_patient:
        rate_card_version_text = (
            getattr(budget_rate_card, "PATIENT_RATE_CARD_VERSION", None)
            or derivation.get("rate_card_version") or "—"
        )
    else:
        rate_card_version_text = derivation.get("rate_card_version") or "—"

    # -- Page setup / running furniture --------------------------------------
    doc = _companion_docx()
    sec = doc.sections[0]
    _companion_header_docx(
        sec,
        left1=budget.name, right1="Clinical Study Budget",
        left2=f"{(budget.drug_inn or 'Product').title()} — {descriptor}",
        right2=f"{budget.currency} · {budget.status.value.title()}",
    )
    _companion_footer_docx(
        sec,
        note="Approximate planning estimate — not a quotation. Every figure is editable.",
    )

    # -- Cover page -----------------------------------------------------------
    cover_rows: list[tuple[str, str]] = [
        ("Sponsor", budget.sponsor_name or "—"),
        ("CRO", budget.cro_name or "—"),
        ("Drug (INN)", (budget.drug_inn or "—").title()),
        ("Study type", descriptor),
        ("Currency", budget.currency),
        ("Grand total (estimated)", f"{budget.currency} {_money(rollup['grand_total'])}"),
        ("Budget status", budget.status.value.title()),
        ("Prepared", f"{datetime.now(timezone.utc):%d %b %Y}"),
    ]
    if is_patient:
        cover_rows[3:3] = [
            ("Indication", _cellstr(_assumption_first(budget, assumptions, "indication"))),
            ("Therapeutic area", _cellstr(_assumption_first(budget, assumptions, "therapeutic_area"))),
            ("Sites (activated / selected)", _sites_summary_text(budget, assumptions)),
            ("Countries", _cellstr(_assumption_first(budget, assumptions, "n_countries", "countries"))),
            ("Total duration (months)", _cellstr(_assumption_value(budget, assumptions, "project_duration_months"))),
            ("Geography basis", _geography_basis_text(budget, assumptions)),
        ]

    _companion_cover_docx(
        doc,
        doc_type="CLINICAL STUDY BUDGET",
        subtitle=f"{(budget.drug_inn or budget.name).title()} — {descriptor}",
        rows=cover_rows,
        notices=(disclaimer_text,),
    )
    doc.add_page_break()

    # -- 1.0 Assumptions -------------------------------------------------------
    doc.add_heading("1.0 Assumptions", level=1)

    sf_rate = _assumption_value(budget, assumptions, "screen_failure_rate_pct")
    protocol_linked = bool(derivation.get("protocol_linked"))
    protocol_id = derivation.get("protocol_id")
    if protocol_linked and protocol_id:
        linked_text = f"Yes (protocol {protocol_id})"
    elif protocol_linked:
        linked_text = "Yes"
    else:
        linked_text = "No — drug-name-only estimate"
    modality = _assumption_value(budget, assumptions, "modality")

    if is_patient:
        _ss_kv_table_docx(doc, _patient_kv_rows_pre_phase(budget, assumptions, sf_rate=sf_rate))
        _ss_para(
            doc,
            "Phase durations — each monthly rate below is priced against its own "
            "study phase, not a single blended duration:",
            italic=True, size=9.5,
        )
        _phase_duration_grid_docx(doc, budget, assumptions)
        _ss_kv_table_docx(
            doc,
            _patient_kv_rows_post_phase(
                budget, assumptions, linked_text=linked_text, modality=modality,
                rate_card_version=rate_card_version_text,
            ),
        )
    else:
        _ss_kv_table_docx(
            doc,
            _healthy_kv_rows(
                budget, assumptions, sf_rate=sf_rate, linked_text=linked_text, modality=modality,
                rate_card_version=rate_card_version_text,
            ),
        )

    # Provenance grid — "nothing is silently invented": one row per resolved
    # assumption naming exactly where its number came from. Unchanged logic;
    # it automatically picks up whatever new patient assumption keys the
    # engine starts writing.
    prov_rows: list[list[str]] = [["Assumption", "Value", "Source", "Basis"]]
    for key, entry in assumptions.items():
        if not isinstance(entry, dict):
            continue
        prov_rows.append([
            key.replace("_", " "),
            _cellstr(entry.get("value")),
            _source_label(entry.get("source")),
            entry.get("basis") or "",
        ])
    if len(prov_rows) > 1:
        _budget_grid_docx(
            doc, prov_rows,
            col_widths=[Inches(2.6), Inches(1.3), Inches(1.4), Inches(1.45)],
        )

    # -- 2.0 Summary Study Budget -----------------------------------------------
    doc.add_heading("2.0 Summary Study Budget", level=1)

    category_subtotals: dict[str, float] = rollup.get("category_subtotals") or {}
    summary_data_rows: list[list[str]] = []
    for c in categories:
        subtotal = category_subtotals.get(str(c.id), 0.0)
        cell_val = _money(subtotal)
        if subtotal == 0.0 and c.line_items:
            statuses = {li.status for li in c.line_items}
            if len(statuses) == 1:
                only_status = next(iter(statuses))
                if only_status != BudgetLineStatus.costed:
                    cell_val = _STATUS_LABEL.get(only_status.value, only_status.value)
        summary_data_rows.append([f"{c.sort_order}.00", c.name, cell_val])

    category_row_count = len(summary_data_rows)

    # Bucket totals present as categories on THIS budget — read off the
    # runtime bucket value rather than an imported enum member, so this keeps
    # working even before BudgetBucket gains investigator_grants/software.
    bucket_categories: dict[str, list] = {}
    for c in categories:
        bucket_val = getattr(c.bucket, "value", c.bucket)
        bucket_categories.setdefault(bucket_val, []).append(c)

    bucket_rows: list[list[str]] = []
    for rollup_key, bucket_val, healthy_label, patient_label in _BUCKET_ROLLUP_ROWS:
        label = patient_label if is_patient else healthy_label
        if label is None:
            continue
        total = rollup.get(rollup_key, 0.0) or 0.0
        has_category = bool(bucket_categories.get(bucket_val))
        if total == 0.0 and not has_category:
            continue
        bucket_rows.append(["", label, _money(total)])

    rollup_rows: list[list[str]] = [
        ["", "Total Direct Costs (before discount)", _money(rollup["direct_subtotal"])],
    ]
    discount_footnote = None
    if budget.discount_type != BudgetDiscountType.none:
        if budget.discount_type == BudgetDiscountType.percent:
            discount_label = (
                f"Less CRO discount ({budget.discount_value:g}%)" if is_patient
                else f"Less discount ({budget.discount_value:g}%)"
            )
        else:
            discount_label = "Less CRO discount (flat)" if is_patient else "Less discount (flat)"
        rollup_rows.append(["", discount_label, f"({_money(rollup['discount_amount'])})"])
        if is_patient:
            discount_footnote = (
                "The discount applies to professional fees only. Investigator grants, "
                "software licensing and pass-through costs are never discounted — this "
                "is how all three reference CRO budgets compute it."
            )
    rollup_rows.extend(bucket_rows)
    rollup_rows.append(["", "GRAND TOTAL", _money(rollup["grand_total"])])

    emphasis_rows = frozenset(range(category_row_count, category_row_count + len(rollup_rows) - 1))
    strong_rows = frozenset({category_row_count + len(rollup_rows) - 1})

    _budget_grid_docx(
        doc,
        [["No.", "Cost Category", f"Total ({budget.currency})"]] + summary_data_rows + rollup_rows,
        col_widths=[Inches(0.7), Inches(4.6), Inches(1.45)],
        right_align_cols=frozenset({2}),
        emphasis_rows=emphasis_rows,
        strong_rows=strong_rows,
    )
    if is_patient:
        _ss_para(
            doc,
            "Investigator grants and site fees are paid directly to sites and are not "
            "subject to the CRO discount. Variable and pass-through categories carrying "
            "no total are rate-card only until incurred.",
            italic=True, size=9,
        )
        if discount_footnote:
            _ss_para(doc, discount_footnote, italic=True, size=8.5)
    else:
        _ss_para(
            doc,
            "Variable and pass-through categories are rate-card only; they total to nil "
            "until actually incurred and are itemized with their unit rates in Section 3.0.",
            italic=True, size=9,
        )

    # -- 3.0 Itemized Study Budget -----------------------------------------------
    doc.add_page_break()
    doc.add_heading("3.0 Itemized Study Budget", level=1)

    lines_by_key: dict[str, dict] = {
        l.get("derivation_key"): l
        for l in (derivation.get("lines") or [])
        if isinstance(l, dict) and l.get("derivation_key")
    }

    for c in categories:
        bucket_val = getattr(c.bucket, "value", c.bucket)
        if is_patient:
            bucket_label = _BUCKET_DISPLAY_LABEL.get(bucket_val, bucket_val.replace("_", " ").title())
            doc.add_heading(f"{c.sort_order}.00 {c.name} — {bucket_label}", level=2)
        else:
            doc.add_heading(f"{c.sort_order}.00 {c.name}", level=2)

        header = ["Sr. No.", "Description", "No. of Units", "Unit Cost", "Unit Description"]
        if is_patient:
            header.append("Provenance")
        header.append("Total")

        if is_patient:
            col_widths = [Inches(0.45), Inches(2.15), Inches(0.6), Inches(0.8), Inches(1.0), Inches(0.85), Inches(0.9)]
            right_align_cols = frozenset({2, 3, 6})
        else:
            col_widths = [Inches(0.5), Inches(2.5), Inches(0.75), Inches(0.9), Inches(1.15), Inches(0.95)]
            right_align_cols = frozenset({2, 3, 5})

        items = sorted(c.line_items, key=lambda li: li.sort_order)

        if not items:
            empty_row = ["", "No line items.", "", "", ""]
            if is_patient:
                empty_row.append("")
            empty_row.append("")
            _budget_grid_docx(
                doc, [header, empty_row],
                col_widths=col_widths, font_pt=8.5, right_align_cols=right_align_cols,
            )
            continue

        data_rows: list[list[Any]] = []
        for i, li in enumerate(items, start=1):
            qty_str = "" if li.quantity is None else f"{li.quantity:,.0f}"
            if li.status == BudgetLineStatus.costed:
                total_str = _money(li.total)
            else:
                total_str = _STATUS_LABEL.get(li.status.value, li.status.value)

            line_deriv = lines_by_key.get(li.derivation_key) or {}
            unit_cost_cell: Any = _money(li.unit_cost)
            if is_patient:
                published = line_deriv.get("unit_cost_published")
                if (
                    isinstance(published, (int, float))
                    and li.unit_cost is not None
                    and round(float(published), 2) != round(float(li.unit_cost), 2)
                ):
                    geo_factor = line_deriv.get("geography_factor")
                    factor_txt = f"{geo_factor:.2f}" if isinstance(geo_factor, (int, float)) else "?"
                    unit_cost_cell = (_money(li.unit_cost), f" ({_money(published)} × {factor_txt})")

            row: list[Any] = [
                f"{c.sort_order}.{i:02d}",
                li.label,
                qty_str,
                unit_cost_cell,
                li.unit_description or "",
            ]
            if is_patient:
                source_val = getattr(li.source, "value", li.source)
                row.append(_provenance_badge(source_val, line_deriv))
            row.append(total_str)
            data_rows.append(row)

        total_row: list[Any] = ["", f"Total — {c.name}", "", "", ""]
        if is_patient:
            total_row.append("")
        total_row.append(_money(category_subtotals.get(str(c.id), 0.0)))
        data_rows.append(total_row)

        _budget_grid_docx(
            doc, [header] + data_rows,
            col_widths=col_widths, font_pt=8.5,
            right_align_cols=right_align_cols,
            emphasis_rows=frozenset({len(data_rows) - 1}),
        )

    # -- NEW 4.0 Sources and Basis of Estimate (patient budgets only) -----------
    if is_patient:
        doc.add_page_break()
        doc.add_heading("4.0 Sources and Basis of Estimate", level=1)

        prov_summary = derivation.get("provenance_summary") or {}
        sentence = _provenance_sentence(budget.currency, rollup.get("grand_total"), prov_summary)
        if sentence:
            _ss_para(doc, sentence, size=9.5)

        tiers = getattr(budget_rate_card, "PROVENANCE_TIERS", _FALLBACK_PROVENANCE_TIERS)
        sources = derivation.get("sources") or {}
        if sources:
            src_rows: list[list[str]] = [["Ref", "Tier", "Figure as published", "Citation", "Caveat"]]
            for ref, s in sources.items():
                if not isinstance(s, dict):
                    continue
                tier_key = s.get("tier")
                tier_text = tiers.get(tier_key, tier_key or "—")
                citation = s.get("citation") or "—"
                url = s.get("url")
                if url:
                    citation = f"{citation} {url}"
                src_rows.append([
                    ref, tier_text, s.get("figure") or "—", citation, s.get("caveat") or "—",
                ])
            _budget_grid_docx(
                doc, src_rows,
                col_widths=[Inches(0.9), Inches(1.3), Inches(1.6), Inches(1.95), Inches(1.0)],
                font_pt=8,
            )
        else:
            _ss_para(
                doc,
                "No cited sources are recorded on this budget yet.",
                italic=True, size=9.5,
            )
        basis_heading = "5.0 Basis of Estimate and Disclosure"
    else:
        basis_heading = "4.0 Basis of Estimate and Disclosure"

    # -- Basis of Estimate and Disclosure -----------------------------------------
    # Page break only when section 4.0 (Sources) was just printed above, to
    # separate it from section 5.0 -- the healthy-volunteer document never had
    # an extra section here and must go straight from the last Section 3.0
    # category table into "4.0 Basis of Estimate and Disclosure" exactly as it
    # always has (no page break), so this stays byte-for-byte unchanged.
    if is_patient:
        doc.add_page_break()
    doc.add_heading(basis_heading, level=1)

    _ss_para(doc, disclaimer_text, italic=True, size=9.5)
    _ss_para(
        doc,
        f"Rate card version {rate_card_version_text}, "
        f"generated {derivation.get('derived_at', '—')}.",
        size=9.5,
    )

    if protocol_linked:
        supplied_keys = [
            k for k, a in assumptions.items()
            if isinstance(a, dict) and a.get("source") == "protocol"
        ]
        supplied = ", ".join(k.replace("_", " ") for k in supplied_keys) or "none of the tracked assumptions"
        pid_text = f"protocol {protocol_id}" if protocol_id else "a linked protocol"
        _ss_para(
            doc,
            f"This budget is linked to {pid_text}, which supplied real computed "
            f"values for: {supplied}.",
            size=9.5,
        )
    else:
        _ss_para(
            doc,
            "No protocol is linked to this budget; every assumption above came from "
            "your own entries or the per-study-type default profile.",
            size=9.5,
        )

    conduct = derivation.get("conduct") or {}
    target = conduct.get("target_per_enrolled")
    itemized = conduct.get("itemized_per_enrolled")
    residual = conduct.get("residual_per_enrolled")
    sf_ratio = conduct.get("screen_failure_ratio")
    alt_ratio = conduct.get("alternate_ratio")
    complexity = _assumption_value(budget, assumptions, "complexity") or "simple"
    if None not in (target, itemized, residual, sf_ratio, alt_ratio):
        _ss_para(
            doc,
            f"Clinical conduct is anchored to a reference cost of "
            f"{budget.currency} {target:,.2f} per enrolled subject for a "
            f"{complexity} study; the itemized procedure lines account for "
            f"{itemized:,.2f} of that and the balancing site-conduct row for "
            f"{residual:,.2f}. Screen failures are priced at {sf_ratio:.0%} and "
            f"alternate subjects at {alt_ratio:.0%} of the enrolled rate, mirroring "
            f"the ratios in the signed reference budgets.",
            size=9.5,
        )

    if is_patient:
        _ss_para(
            doc,
            "Project management, site management, medical monitoring, regulatory "
            "oversight, data management and biostatistics management are each priced "
            "per month of a SPECIFIC study phase, at a different monthly rate per phase, "
            "mirroring all three reference CRO budgets. Site-months are ramped "
            "(start-up 75%, active 60%, close-out 85% of the theoretical site × month "
            "product) because sites activate progressively; a flat site × month product "
            "would overstate site management by 40-65%.",
            size=9.5,
        )

        recon = derivation.get("bid_reconciliation")
        if isinstance(recon, dict):
            doc.add_heading("Bid Reconciliation", level=2)
            ref_study = recon.get("reference_study")
            if ref_study:
                _ss_para(doc, f"Reference study: {ref_study}", size=9.5)

            bid_rows: list[list[str]] = [["CRO", "Grand Total", "Note"]]
            for b in recon.get("bids") or []:
                if not isinstance(b, dict):
                    continue
                gt = b.get("grand_total")
                gt_text = f"{budget.currency} {_money(gt)}" if isinstance(gt, (int, float)) else "—"
                bid_rows.append([b.get("cro") or "—", gt_text, b.get("note") or ""])
            this_estimate = recon.get("this_estimate")
            if isinstance(this_estimate, (int, float)):
                bid_rows.append(["This estimate", f"{budget.currency} {_money(this_estimate)}", ""])
            if len(bid_rows) > 1:
                _budget_grid_docx(
                    doc, bid_rows,
                    col_widths=[Inches(2.0), Inches(1.75), Inches(3.0)], font_pt=9,
                )

            delta = recon.get("delta_vs_highest_bid_pct")
            if isinstance(delta, (int, float)):
                _ss_para(doc, f"Delta vs. highest bid: {delta:+.1f}%.", size=9.5)

            for b in recon.get("explained_by") or []:
                _ss_para(doc, f"— {b}", size=9.5)

            if recon.get("disclaimer"):
                _ss_para(doc, recon["disclaimer"], italic=True, size=9)

    for w in derivation.get("warnings") or []:
        _ss_para(doc, f"— {w}", size=9.5)

    discount_note = derivation.get("discount_note")
    if discount_note:
        _ss_para(doc, discount_note, size=9.5)

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
