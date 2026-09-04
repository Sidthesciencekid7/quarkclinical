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
}


def _money(v: float | None) -> str:
    return "" if v is None else f"{v:,.2f}"


def _cellstr(v: Any) -> str:
    return "—" if v is None else str(v)


def _source_label(source: str | None) -> str:
    if not source:
        return "—"
    return _SOURCE_LABEL.get(source, source.replace("_", " ").title())


def _assumption_value(budget: Budget, assumptions: dict, field: str) -> Any:
    """budget.* first (the columns _resolve_drivers writes every resolved
    driver back to), derivation["assumptions"][field]["value"] second — the
    same resolution order the engine itself uses, so the document can never
    show a number that disagrees with what was actually priced."""
    v = getattr(budget, field, None)
    if v is not None:
        return v
    entry = assumptions.get(field)
    if isinstance(entry, dict) and entry.get("value") is not None:
        return entry["value"]
    return None


# ---------------------------------------------------------------------------
# Grid table — a near-copy of routers.export._ss_grid_table_docx, extended
# with bold emphasis rows (category/grand-total subtotals) and right-aligned
# money columns. Everything else (Table Grid style, 1E3A5F header fill with
# white bold text, F0F4F8/FFFFFF zebra striping, Times New Roman, a repeating
# header row) is unchanged so the budget tables read as the same visual
# language as every other exported table in this app.
# ---------------------------------------------------------------------------


def _budget_grid_docx(
    doc,
    rows: list[list[str]],
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
            rn = para.add_run(cell_text)
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
        for c_idx, cell in enumerate(row.cells):
            cell.width = (
                col_widths[c_idx] if col_widths and c_idx < len(col_widths) else col_w
            )
    _repeat_header_row_docx(tbl)
    doc.add_paragraph()


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
    study_type_label = _STUDY_TYPE_LABELS.get(budget.study_type, "Clinical Study")

    # -- Page setup / running furniture --------------------------------------
    doc = _companion_docx()
    sec = doc.sections[0]
    _companion_header_docx(
        sec,
        left1=budget.name, right1="Clinical Study Budget",
        left2=f"{(budget.drug_inn or 'Product').title()} — {study_type_label}",
        right2=f"{budget.currency} · {budget.status.value.title()}",
    )
    _companion_footer_docx(
        sec,
        note="Approximate planning estimate — not a quotation. Every figure is editable.",
    )

    # -- Cover page -----------------------------------------------------------
    _companion_cover_docx(
        doc,
        doc_type="CLINICAL STUDY BUDGET",
        subtitle=f"{(budget.drug_inn or budget.name).title()} — {study_type_label}",
        rows=[
            ("Sponsor", budget.sponsor_name or "—"),
            ("CRO", budget.cro_name or "—"),
            ("Drug (INN)", (budget.drug_inn or "—").title()),
            ("Study type", study_type_label),
            ("Currency", budget.currency),
            ("Grand total (estimated)", f"{budget.currency} {_money(rollup['grand_total'])}"),
            ("Budget status", budget.status.value.title()),
            ("Prepared", f"{datetime.now(timezone.utc):%d %b %Y}"),
        ],
        notices=(derivation.get("disclaimer") or budget_rate_card.DISCLAIMER,),
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

    kv_rows = [
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
        ("Rate-card version", derivation.get("rate_card_version") or "—"),
    ]
    _ss_kv_table_docx(doc, kv_rows)

    # Provenance grid — "nothing is silently invented": one row per resolved
    # assumption naming exactly where its number came from.
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

    rollup_rows: list[list[str]] = [
        ["", "Total Direct Costs (before discount)", _money(rollup["direct_subtotal"])],
    ]
    if budget.discount_type != BudgetDiscountType.none:
        if budget.discount_type == BudgetDiscountType.percent:
            discount_label = f"Less discount ({budget.discount_value:g}%)"
        else:
            discount_label = "Less discount (flat)"
        rollup_rows.append(["", discount_label, f"({_money(rollup['discount_amount'])})"])
    rollup_rows.extend([
        ["", "Total Direct Costs", _money(rollup["total_direct_costs"])],
        ["", "Subject Stipend", _money(rollup["subject_stipend_total"])],
        ["", "CRO/Vendor Variable Costs (actuals)", _money(rollup["variable_total"])],
        ["", "Pass-Through Costs (actuals)", _money(rollup["pass_through_total"])],
        ["", "GRAND TOTAL", _money(rollup["grand_total"])],
    ])

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
    _ss_para(
        doc,
        "Variable and pass-through categories are rate-card only; they total to nil "
        "until actually incurred and are itemized with their unit rates in Section 3.0.",
        italic=True, size=9,
    )

    # -- 3.0 Itemized Study Budget -----------------------------------------------
    doc.add_page_break()
    doc.add_heading("3.0 Itemized Study Budget", level=1)

    for c in categories:
        doc.add_heading(f"{c.sort_order}.00 {c.name}", level=2)
        header = ["Sr. No.", "Description", "No. of Units", "Unit Cost", "Unit Description", "Total"]
        items = sorted(c.line_items, key=lambda li: li.sort_order)
        col_widths = [Inches(0.5), Inches(2.5), Inches(0.75), Inches(0.9), Inches(1.15), Inches(0.95)]

        if not items:
            _budget_grid_docx(
                doc, [header, ["", "No line items.", "", "", "", ""]],
                col_widths=col_widths, font_pt=8.5, right_align_cols=frozenset({2, 3, 5}),
            )
            continue

        data_rows: list[list[str]] = []
        for i, li in enumerate(items, start=1):
            qty_str = "" if li.quantity is None else f"{li.quantity:,.0f}"
            if li.status == BudgetLineStatus.costed:
                total_str = _money(li.total)
            else:
                total_str = _STATUS_LABEL.get(li.status.value, li.status.value)
            data_rows.append([
                f"{c.sort_order}.{i:02d}",
                li.label,
                qty_str,
                _money(li.unit_cost),
                li.unit_description or "",
                total_str,
            ])
        data_rows.append([
            "", f"Total — {c.name}", "", "", "",
            _money(category_subtotals.get(str(c.id), 0.0)),
        ])

        _budget_grid_docx(
            doc, [header] + data_rows,
            col_widths=col_widths, font_pt=8.5,
            right_align_cols=frozenset({2, 3, 5}),
            emphasis_rows=frozenset({len(data_rows) - 1}),
        )

    # -- 4.0 Basis of Estimate and Disclosure -------------------------------------
    doc.add_heading("4.0 Basis of Estimate and Disclosure", level=1)

    _ss_para(doc, derivation.get("disclaimer") or budget_rate_card.DISCLAIMER, italic=True, size=9.5)
    _ss_para(
        doc,
        f"Rate card version {derivation.get('rate_card_version', '—')}, "
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

    for w in derivation.get("warnings") or []:
        _ss_para(doc, f"— {w}", size=9.5)

    discount_note = derivation.get("discount_note")
    if discount_note:
        _ss_para(doc, discount_note, size=9.5)

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
