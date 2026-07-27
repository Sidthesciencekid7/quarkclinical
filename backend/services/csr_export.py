"""ICH E3 Clinical Study Report (CSR) exporter — renders a completed CSR to
.docx and .pdf.

This module deliberately REUSES the mature DOCX/PDF helper toolkit already
living in :mod:`routers.export` (HTML-narrative rendering, styled tables, PK
notation normalization, cover/TOC/font machinery) rather than reinventing it.
Only the pieces that are intrinsically *protocol*-specific — the cover subtitle
wording ("Clinical Study Report" vs "Clinical Trial Protocol") and the
running header/footer that reach into a ``Protocol`` ORM object — are written
fresh here, driven by a plain ``meta`` dict.

The one inviolable rule of this exporter: **the deterministic ``results``
numbers are rendered verbatim.** Point estimates, confidence-interval bounds,
CV%, disposition counts and the bioequivalence verdict are formatted (to the
ICH-conventional 2 decimals for percentages) but never transformed into a
different value. The document is the deterministic source of truth.
"""
from __future__ import annotations

import io
import math
import re
from datetime import datetime
from typing import Any

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Inches, Pt, RGBColor
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from services.csr_sections import CSR_SECTION_ORDER, CSR_SECTION_TITLES

# --- Reused toolkit from the protocol exporter -----------------------------
# Underscore-prefixed but importable module functions (see task brief). Import
# side-effect: routers.export runs _register_unicode_fonts() at import time, so
# the _SERIF/_SANS names below already resolve to the embedded Unicode family.
from routers.export import (  # noqa: E402
    _add_page_number,
    _add_section_content_docx,
    _add_table_docx,
    _add_table_pdf,
    _add_section_content_pdf,
    _apply_doc_styles,
    _add_toc_docx,
    _enable_update_fields_on_open,
    _hf_tab_line,
    _normalize_pk_notation,
    _pdf_safe,
    _pdf_styles,
    _strip_html,
    _SANS,
    _SANS_BOLD,
    _SERIF,
)

# --- reportlab (PDF) -------------------------------------------------------
try:
    from reportlab.lib import colors as rl_colors
    from reportlab.lib.pagesizes import A4 as RL_A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm as rl_mm
    from reportlab.pdfgen import canvas as rl_canvas
    from reportlab.platypus import (
        PageBreak,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )
    from reportlab.platypus.tableofcontents import TableOfContents

    _RL_OK = True
except ImportError:  # pragma: no cover - reportlab is a stated dependency
    _RL_OK = False


# ---------------------------------------------------------------------------
# Small formatting helpers — format for display, never alter the value
# ---------------------------------------------------------------------------

# 90% CI is rendered with an en dash + spaces per the brief ("92.01 – 98.91").
_CI_DASH = "–"
_DEFAULT_BE_LIMITS = (80.0, 125.0)

# Human labels for the primary exposure metrics. Fed through the PK-notation
# normalizer so "Cmax"/"AUC0-t"/"AUC0-inf" become canonical subscripted forms.
_METRIC_LABEL = {
    "cmax": "Cmax",
    "auc0t": "AUC0-t",
    "auc0inf": "AUC0-inf",
}
_METRIC_ORDER = ("cmax", "auc0t", "auc0inf")


def _fmt_pct(v: Any) -> str:
    """Percentage to 2 decimals (ICH convention). Value is NOT rounded to fewer
    significant digits than provided — it is displayed at the mandated precision."""
    try:
        return f"{float(v):.2f}"
    except (TypeError, ValueError):
        return "—"


def _fmt_num(v: Any) -> str:
    """Scale-aware numeric formatting for derived PK summary values."""
    if v is None:
        return "—"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "—"
    a = abs(f)
    if a == 0:
        return "0"
    if a >= 1000:
        return f"{f:,.1f}"
    if a >= 100:
        return f"{f:.1f}"
    if a >= 10:
        return f"{f:.2f}"
    if a >= 1:
        return f"{f:.3f}"
    return f"{f:.4f}"


def _fmt_date(v: Any) -> str:
    if isinstance(v, datetime):
        return v.strftime("%d-%b-%Y")
    return str(v) if v else "—"


def _esc(s: Any) -> str:
    """Escape a *data* string for embedding in the small HTML table fragments."""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _join(seq: Any) -> str:
    if not seq:
        return "—"
    if isinstance(seq, (list, tuple)):
        return ", ".join(_esc(x) for x in seq)
    return _esc(seq)


_VERDICT_PHRASE = {
    "bioequivalent": (
        "Bioequivalent — the test product met the pre-specified "
        "bioequivalence acceptance criteria."
    ),
    "not_bioequivalent": (
        "Not bioequivalent — the pre-specified acceptance criteria "
        "were not met."
    ),
    "indeterminate": (
        "Indeterminate — bioequivalence could not be concluded from "
        "the available data."
    ),
}


def _verdict_phrase(verdict: Any) -> str:
    return _VERDICT_PHRASE.get(str(verdict), str(verdict or "").replace("_", " "))


# Every results table in the CSR is rendered deterministically by this exporter
# (see ``_splice_table_docx`` / ``_splice_table_pdf``). The AI narrative is prompted
# to write prose only (services.csr_sections.NUMBERS_LOCKED_DIRECTIVE rule 5), but
# a prompt is only a strong suggestion — so we ALSO strip any <table> the model
# emits before rendering its HTML. This makes duplicate results tables impossible
# regardless of model behaviour: the deterministic tables stay the single source.
_TABLE_RE = re.compile(r"<table\b[^>]*>.*?</table>", re.IGNORECASE | re.DOTALL)


def _strip_narrative_tables(html: Any) -> str:
    """Remove any ``<table>…</table>`` blocks from an AI-generated narrative
    fragment so they cannot duplicate the exporter's deterministic tables."""
    if not isinstance(html, str) or "<table" not in html.lower():
        return html if isinstance(html, str) else ""
    return _TABLE_RE.sub("", html)


# ---------------------------------------------------------------------------
# Deterministic results tables — built as small HTML fragments so they render
# through the SAME styled-table helpers as everything else in the toolkit.
# Each fragment is the *inner* HTML (thead/tbody) that _add_table_docx /
# _add_table_pdf expect. The whole fragment is run through _normalize_pk_notation
# so metric labels get canonical subscripts.
# ---------------------------------------------------------------------------


def _html_table(headers: list[str], rows: list[list[str]]) -> str:
    thead = "<thead><tr>" + "".join(f"<th>{h}</th>" for h in headers) + "</tr></thead>"
    body = "<tbody>" + "".join(
        "<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in rows
    ) + "</tbody>"
    return _normalize_pk_notation(thead + body)


def _disposition_table_html(results: dict) -> str:
    d = results.get("disposition") or {}
    rows: list[list[str]] = [
        ["Subjects (N)", _esc(d.get("n_subjects"))],
        ["Observations / records", _esc(d.get("n_rows"))],
        ["Treatments", _join(d.get("treatments"))],
        ["Study periods", _join(d.get("periods"))],
        ["Sequences", _join(d.get("sequences"))],
    ]
    if d.get("n_blq") is not None:
        rows.append(["BLQ observations", _esc(d.get("n_blq"))])
    if d.get("lloq") is not None:
        rows.append(["LLOQ", _fmt_num(d.get("lloq"))])
    return _html_table(["Disposition Parameter", "Value"], rows)


def _be_table_html(results: dict) -> str:
    """The pivotal bioequivalence table — one row per metric in results['be'].

    Numbers are rendered EXACTLY as provided (percentages to 2 decimals). A
    metric carrying an ``error`` key renders that error text instead of stats.
    """
    be = results.get("be") or {}
    order = [m for m in _METRIC_ORDER if m in be] + [
        m for m in be if m not in _METRIC_ORDER
    ]

    # Header column reflects the acceptance limits actually in force (default
    # 80.00-125.00%, or a metric's own scaled/NTI limits).
    def_lo, def_hi = _DEFAULT_BE_LIMITS
    for m in order:
        e = be[m]
        if "error" not in e and e.get("lower_limit_pct") is not None:
            def_lo = float(e["lower_limit_pct"])
            def_hi = float(e["upper_limit_pct"])
            break
    within_hdr = f"Within {_fmt_pct(def_lo)}{_CI_DASH}{_fmt_pct(def_hi)}% (Y/N)"

    rows: list[list[str]] = []
    for m in order:
        e = be[m]
        label = _METRIC_LABEL.get(m, e.get("metric", m))
        if "error" in e:
            rows.append([label, "—", _esc(e["error"]), "—", "—", "—"])
            continue
        ci = (
            f"{_fmt_pct(e.get('ci_lower_pct'))} {_CI_DASH} "
            f"{_fmt_pct(e.get('ci_upper_pct'))}"
        )
        yn = "Yes" if e.get("within_be_limits") else "No"
        lo, hi = e.get("lower_limit_pct"), e.get("upper_limit_pct")
        within_cell = yn
        if lo is not None and hi is not None and (
            abs(float(lo) - def_lo) > 1e-9 or abs(float(hi) - def_hi) > 1e-9
        ):
            within_cell = f"{yn} ({_fmt_pct(lo)}{_CI_DASH}{_fmt_pct(hi)})"
        rows.append([
            label,
            _esc(e.get("n", "—")),
            _fmt_pct(e.get("point_estimate_pct")),
            ci,
            _fmt_pct(e.get("cv_intra_pct")),
            within_cell,
        ])

    headers = [
        "Parameter",
        "N",
        "Point Estimate (%)",
        "90% CI (%)",
        "Intra-subject CV (%)",
        within_hdr,
    ]
    return _html_table(headers, rows)


def _geomean(values: list[Any]) -> float | None:
    xs = [float(v) for v in values if isinstance(v, (int, float)) and v > 0]
    if not xs:
        return None
    return math.exp(sum(math.log(v) for v in xs) / len(xs))


def _pk_summary_table_html(results: dict) -> str | None:
    """Geometric-mean PK parameter summary per treatment, computed from the NCA
    entries. Returns None when no NCA table is present."""
    nca = results.get("nca")
    if not nca:
        return None
    by_treatment: dict[str, dict[str, list]] = {}
    for entry in nca.values():
        trt = str(entry.get("treatment") or "Unknown")
        bucket = by_treatment.setdefault(
            trt, {"cmax": [], "auc0t": [], "auc0inf": [], "n": 0}
        )
        bucket["n"] += 1
        for key in ("cmax", "auc0t", "auc0inf"):
            if entry.get(key) is not None:
                bucket[key].append(entry[key])

    rows: list[list[str]] = []
    for trt in sorted(by_treatment):
        b = by_treatment[trt]
        rows.append([
            _esc(trt),
            str(b["n"]),
            _fmt_num(_geomean(b["cmax"])),
            _fmt_num(_geomean(b["auc0t"])),
            _fmt_num(_geomean(b["auc0inf"])),
        ])
    if not rows:
        return None
    headers = [
        "Treatment",
        "N",
        "Geometric Mean Cmax",
        "Geometric Mean AUC0-t",
        "Geometric Mean AUC0-inf",
    ]
    return _html_table(headers, rows)


def _ae_table_html(results: dict) -> str | None:
    """Adverse-event summary from results['safety'] (defensive — the 'safety'
    key may be added later)."""
    safety = results.get("safety")
    if not safety:
        return None
    rows: list[list[str]] = [["Treatment-emergent AEs (total)", _esc(safety.get("n_teae", "—"))]]
    by_trt = safety.get("by_treatment") or {}
    if isinstance(by_trt, dict):
        for trt, val in by_trt.items():
            if isinstance(val, dict):
                val = val.get("n_teae", val.get("count", val))
            rows.append([f"TEAEs ({_esc(trt)})", _esc(val)])
    serious = safety.get("serious") or []
    rows.append(["Serious adverse events", _esc(len(serious) if isinstance(serious, list) else serious)])
    return _html_table(["Safety Parameter", "Value"], rows)


# ---------------------------------------------------------------------------
# DOCX builder
# ---------------------------------------------------------------------------


def _caption_docx(doc: Document, text: str) -> None:
    """A bold, non-Heading-style caption for a spliced deterministic table —
    deliberately NOT a Word Heading style so it stays out of the TOC field and
    does not disturb the section's N.M sub-heading numbering."""
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(8)
    p.paragraph_format.space_after = Pt(2)
    r = p.add_run(text)
    r.bold = True
    r.font.name = "Times New Roman"
    r.font.size = Pt(10.5)


def _csr_header_footer_docx(section, meta: dict) -> None:
    """CSR running header + footer (the protocol helpers need a Protocol ORM
    object; here we drive it from the plain meta dict instead)."""
    header = section.header
    header.is_linked_to_previous = False
    p1 = header.paragraphs[0] if header.paragraphs else header.add_paragraph()
    p1.clear()
    _hf_tab_line(p1, "Clinical Study Report", f"Version No.: {meta.get('version', '1.0')}")
    p2 = header.add_paragraph()
    _hf_tab_line(
        p2,
        str(meta.get("protocol_code") or "CSR"),
        f"Dated: {_fmt_date(meta.get('created_at'))}",
    )

    footer = section.footer
    footer.is_linked_to_previous = False
    pf = footer.paragraphs[0] if footer.paragraphs else footer.add_paragraph()
    pf.clear()
    pf.alignment = WD_ALIGN_PARAGRAPH.CENTER
    rc = pf.add_run("CONFIDENTIAL — Draft Clinical Study Report  ·  ")
    rc.font.size = Pt(8)
    rc.font.color.rgb = RGBColor(0x64, 0x74, 0x8B)
    _add_page_number(pf, size_pt=8)


def _add_csr_cover_docx(doc: Document, results: dict, meta: dict, study_title: str) -> None:
    """CSR cover page — 'Clinical Study Report', study title, metadata + verdict."""
    p_title = doc.add_paragraph()
    p_title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p_title.paragraph_format.space_before = Pt(48)
    p_title.paragraph_format.space_after = Pt(12)
    r = p_title.add_run("Clinical Study Report")
    r.bold = True
    r.font.name = "Times New Roman"
    r.font.size = Pt(16)

    p_sub = doc.add_paragraph()
    p_sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p_sub.paragraph_format.space_after = Pt(4)
    r2 = p_sub.add_run("(ICH E3 Structure)")
    r2.italic = True
    r2.font.name = "Times New Roman"
    r2.font.size = Pt(10.5)

    p_study = doc.add_paragraph()
    p_study.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p_study.paragraph_format.space_after = Pt(36)
    r3 = p_study.add_run(study_title)
    r3.bold = True
    r3.font.name = "Times New Roman"
    r3.font.size = Pt(12)

    doc.add_paragraph()

    drug = str(meta.get("drug_inn") or "").strip()
    rows = [
        ("Protocol Number", str(meta.get("protocol_code") or "[Protocol Number]")),
        ("Version Number and Date",
         f"{meta.get('version', '1.0')}; Dated {_fmt_date(meta.get('created_at'))}"),
        ("Investigational Product", drug.title() or "[Investigational Product]"),
        ("Study Type", str(meta.get("study_type") or "Bioequivalence")),
        ("Design", str(results.get("design_key") or "—").title()),
        ("Sponsor", str(meta.get("sponsor") or "[Sponsor Name and Address]")),
        ("Overall Bioequivalence Conclusion", _verdict_phrase(results.get("verdict"))),
    ]
    table = doc.add_table(rows=len(rows), cols=2)
    table.style = "Table Grid"
    for row_idx, (label, value) in enumerate(rows):
        row = table.rows[row_idx]
        lc = row.cells[0]
        lc.text = ""
        lr = lc.paragraphs[0].add_run(label)
        lr.bold = True
        lr.font.name = "Times New Roman"
        lr.font.size = Pt(10.5)
        shd = OxmlElement("w:shd")
        shd.set(qn("w:val"), "clear")
        shd.set(qn("w:color"), "auto")
        shd.set(qn("w:fill"), "DDEEFF")
        lc._tc.get_or_add_tcPr().append(shd)

        vc = row.cells[1]
        vc.text = ""
        vr = vc.paragraphs[0].add_run(value)
        vr.font.name = "Times New Roman"
        vr.font.size = Pt(10.5)
    for row in table.rows:
        row.cells[0].width = Inches(2.4)
        row.cells[1].width = Inches(4.35)

    p_conf = doc.add_paragraph()
    p_conf.paragraph_format.space_before = Pt(24)
    rcf = p_conf.add_run(
        "This Clinical Study Report is a DRAFT prepared for review by qualified "
        "clinical and regulatory professionals. All results reported herein are "
        "the deterministic output of the validated analysis and are presented "
        "verbatim; the narrative is subject to expert verification and sign-off "
        "before use."
    )
    rcf.italic = True
    rcf.font.name = "Times New Roman"
    rcf.font.size = Pt(10.5)

    p_conf2 = doc.add_paragraph()
    p_conf2.paragraph_format.space_before = Pt(12)
    rcf2 = p_conf2.add_run(
        "This report contains confidential information belonging to the Sponsor. "
        "It must not be used, divulged, published, or otherwise disclosed without "
        "the prior written consent of the Sponsor."
    )
    rcf2.italic = True
    rcf2.font.name = "Times New Roman"
    rcf2.font.size = Pt(10.5)


def _splice_table_docx(doc: Document, results: dict, section_name: str) -> None:
    """Render the deterministic table(s) that belong immediately after the given
    numbered section, if the relevant results are present."""
    if section_name == "study_population":
        _caption_docx(doc, "Table: Subject Disposition")
        _add_table_docx(doc, _disposition_table_html(results))
    elif section_name == "pharmacokinetic_results":
        html = _pk_summary_table_html(results)
        if html:
            _caption_docx(doc, "Table: Pharmacokinetic Parameter Summary (Geometric Means by Treatment)")
            _add_table_docx(doc, html)
    elif section_name == "bioequivalence_assessment":
        _caption_docx(doc, "Table: Pivotal Bioequivalence Assessment")
        _add_table_docx(doc, _be_table_html(results))
        p = doc.add_paragraph()
        rp = p.add_run(f"Overall bioequivalence conclusion: {_verdict_phrase(results.get('verdict'))}")
        rp.bold = True
        rp.font.name = "Times New Roman"
        rp.font.size = Pt(10.5)
    elif section_name == "safety_results":
        html = _ae_table_html(results)
        if html:
            _caption_docx(doc, "Table: Treatment-Emergent Adverse Event Summary")
            _add_table_docx(doc, html)


# Sections after which a deterministic table is always/conditionally spliced.
_ANCHOR_SECTIONS = {
    "study_population",
    "pharmacokinetic_results",
    "bioequivalence_assessment",
    "safety_results",
}


def _has_splice_content(results: dict, section_name: str) -> bool:
    """Whether an anchor section has a deterministic table worth an entry even
    if its narrative HTML is empty."""
    if section_name == "study_population":
        return bool(results.get("disposition"))
    if section_name == "pharmacokinetic_results":
        return bool(results.get("nca"))
    if section_name == "bioequivalence_assessment":
        return bool(results.get("be"))
    if section_name == "safety_results":
        return bool(results.get("safety"))
    return False


def build_csr_docx(
    *,
    results: dict,
    sections: dict,
    meta: dict,
    citations: list | None = None,
) -> bytes:
    """Render a completed CSR to a Word .docx and return the bytes."""
    sections = {k: _strip_narrative_tables(v) for k, v in (sections or {}).items()}
    meta = meta or {}

    doc = Document()
    sec = doc.sections[0]
    sec.page_width = int(8.27 * 914400)
    sec.page_height = int(11.69 * 914400)
    sec.left_margin = Inches(0.75)
    sec.right_margin = Inches(0.75)
    sec.top_margin = Inches(1.0)
    sec.bottom_margin = Inches(1.0)
    sec.different_first_page_header_footer = False

    _apply_doc_styles(doc)
    _enable_update_fields_on_open(doc)
    _csr_header_footer_docx(sec, meta)

    study_title = _strip_html(
        (sections.get("title") or "").strip()
        or str(meta.get("drug_inn") or "").title()
    ).strip() or "Clinical Study Report"

    # Cover
    _add_csr_cover_docx(doc, results, meta, study_title)
    doc.add_page_break()

    # Table of contents
    _add_toc_docx(doc)
    doc.add_page_break()

    # Unnumbered front-matter synopsis
    synopsis = _normalize_pk_notation((sections.get("synopsis") or "").strip())
    if _strip_html(synopsis):
        p_syn = doc.add_paragraph()
        p_syn.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p_syn.paragraph_format.space_after = Pt(12)
        r_syn = p_syn.add_run(CSR_SECTION_TITLES.get("synopsis", "Synopsis"))
        r_syn.bold = True
        r_syn.font.name = "Times New Roman"
        r_syn.font.size = Pt(14)
        _add_section_content_docx(doc, synopsis, 0, numbered=False)
        doc.add_page_break()

    # Numbered E3 body
    idx = 0
    for section_name in CSR_SECTION_ORDER:
        if section_name in ("title", "synopsis"):
            continue
        raw = _normalize_pk_notation((sections.get(section_name) or "").strip())
        has_narrative = bool(_strip_html(raw))
        is_anchor = section_name in _ANCHOR_SECTIONS and _has_splice_content(results, section_name)
        if not has_narrative and not is_anchor:
            continue
        idx += 1

        title = CSR_SECTION_TITLES.get(section_name, section_name.replace("_", " ").title())
        heading = doc.add_heading(f"{idx} {title}", level=1)
        # Keep numbering black (Heading 1 default may be styled otherwise).
        for r in heading.runs:
            r.font.color.rgb = RGBColor(0, 0, 0)

        if has_narrative:
            _add_section_content_docx(doc, raw, idx)

        if is_anchor:
            _splice_table_docx(doc, results, section_name)

        if section_name == "references" and citations:
            _add_citations_docx(doc, citations)

    # Closing draft/confidentiality note.
    note = doc.add_paragraph()
    note.paragraph_format.space_before = Pt(18)
    nr = note.add_run(
        "This Clinical Study Report is a draft generated for review by qualified "
        "clinical and regulatory reviewers. The deterministic results tables above "
        "are the authoritative source of the reported figures."
    )
    nr.italic = True
    nr.font.name = "Times New Roman"
    nr.font.size = Pt(9)

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf.getvalue()


def _add_citations_docx(doc: Document, citations: list) -> None:
    _caption_docx(doc, "Literature Citations")
    for i, cite in enumerate(citations, start=1):
        if isinstance(cite, dict):
            title = cite.get("title") or "Title unavailable"
            pmid = cite.get("pmid")
            text = f"[{i}] {title}" + (f" (PMID: {pmid})" if pmid else "")
        else:
            text = f"[{i}] {cite}"
        p = doc.add_paragraph()
        p.paragraph_format.left_indent = Inches(0.4)
        p.paragraph_format.first_line_indent = Inches(-0.4)
        r = p.add_run(text)
        r.font.name = "Times New Roman"
        r.font.size = Pt(10)


# ---------------------------------------------------------------------------
# PDF builder (mirrors routers.export._build_pdf_once, adapted to meta)
# ---------------------------------------------------------------------------


def _csr_numbered_canvas(meta: dict) -> type:
    """NumberedCanvas capturing CSR meta for the 'Page N of M' footer + header."""
    code = str(meta.get("protocol_code") or "CSR")
    ver = f"Version No.: {meta.get('version', '1.0')}"
    dated = f"Dated: {_fmt_date(meta.get('created_at'))}"

    class NumberedCanvas(rl_canvas.Canvas):  # type: ignore[misc]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            rl_canvas.Canvas.__init__(self, *args, **kwargs)
            self._saved_states: list[dict[str, Any]] = []

        def showPage(self) -> None:
            self._saved_states.append(dict(self.__dict__))
            self._startPage()

        def save(self) -> None:
            total = len(self._saved_states)
            for state in self._saved_states:
                self.__dict__.update(state)
                self._draw_hf(total)
                rl_canvas.Canvas.showPage(self)
            rl_canvas.Canvas.save(self)

        def _draw_hf(self, total_pages: int) -> None:
            W, H = RL_A4
            lm = rm = 30 * rl_mm
            self.saveState()
            self.setFont("Helvetica", 8)
            self.setFillColor(rl_colors.HexColor("#64748B"))
            self.drawString(lm, H - 15 * rl_mm, "Clinical Study Report")
            self.drawString(lm, H - 19 * rl_mm, code)
            self.drawRightString(W - rm, H - 15 * rl_mm, ver)
            self.drawRightString(W - rm, H - 19 * rl_mm, dated)
            self.setStrokeColor(rl_colors.HexColor("#E2E8F0"))
            self.line(lm, H - 21 * rl_mm, W - rm, H - 21 * rl_mm)
            self.line(lm, 16 * rl_mm, W - rm, 16 * rl_mm)
            self.setFont("Helvetica", 8)
            self.setFillColor(rl_colors.HexColor("#64748B"))
            self.drawCentredString(
                W / 2, 11 * rl_mm,
                f"CONFIDENTIAL — Draft Clinical Study Report   ·   "
                f"Page {self._pageNumber} of {total_pages}",
            )
            self.restoreState()

    return NumberedCanvas


def _splice_table_pdf(story: list, results: dict, section_name: str, styles: dict) -> None:
    if section_name == "study_population":
        story.append(Paragraph("Table: Subject Disposition", styles["FrontSub"]))
        _add_table_pdf(story, _disposition_table_html(results), styles)
    elif section_name == "pharmacokinetic_results":
        html = _pk_summary_table_html(results)
        if html:
            story.append(Paragraph(
                "Table: Pharmacokinetic Parameter Summary (Geometric Means by Treatment)",
                styles["FrontSub"]))
            _add_table_pdf(story, html, styles)
    elif section_name == "bioequivalence_assessment":
        story.append(Paragraph("Table: Pivotal Bioequivalence Assessment", styles["FrontSub"]))
        _add_table_pdf(story, _be_table_html(results), styles)
        story.append(Paragraph(
            f"<b>Overall bioequivalence conclusion: "
            f"{_pdf_safe(_verdict_phrase(results.get('verdict')))}</b>",
            styles["Body"]))
    elif section_name == "safety_results":
        html = _ae_table_html(results)
        if html:
            story.append(Paragraph(
                "Table: Treatment-Emergent Adverse Event Summary", styles["FrontSub"]))
            _add_table_pdf(story, html, styles)


def build_csr_pdf(
    *,
    results: dict,
    sections: dict,
    meta: dict,
    citations: list | None = None,
) -> bytes:
    """Render a completed CSR to a PDF and return the bytes."""
    if not _RL_OK:  # pragma: no cover
        raise RuntimeError("PDF export requires the 'reportlab' package.")

    sections = {k: _strip_narrative_tables(v) for k, v in (sections or {}).items()}
    meta = meta or {}
    styles = _pdf_styles()
    margin = 30 * rl_mm

    class _CsrTocDocTemplate(SimpleDocTemplate):
        def afterFlowable(self, flowable: Any) -> None:
            if flowable.__class__.__name__ != "Paragraph":
                return
            name = getattr(flowable.style, "name", "")
            if name == "H1":
                self.notify("TOCEntry", (0, flowable.getPlainText(), self.page))
            elif name == "H2":
                self.notify("TOCEntry", (1, flowable.getPlainText(), self.page))

    buf = io.BytesIO()
    doc = _CsrTocDocTemplate(
        buf,
        pagesize=RL_A4,
        leftMargin=margin,
        rightMargin=margin,
        topMargin=38 * rl_mm,
        bottomMargin=28 * rl_mm,
        title=f"Clinical Study Report — {str(meta.get('drug_inn') or '').title()}",
        author="PK/BE Protocol Platform",
    )

    study_title = _strip_html(
        (sections.get("title") or "").strip()
        or str(meta.get("drug_inn") or "").title()
    ).strip() or "Clinical Study Report"

    story: list[Any] = []

    # Cover
    story.append(Paragraph("Clinical Study Report", styles["CoverTitle"]))
    story.append(Paragraph("<i>(ICH E3 Structure)</i>", styles["CoverSubtitle"]))
    story.append(Spacer(1, 4 * rl_mm))
    story.append(Paragraph(_pdf_safe(study_title), styles["CoverSubtitle"]))
    story.append(Spacer(1, 10 * rl_mm))

    drug = str(meta.get("drug_inn") or "").strip()
    cover_data = [
        ["Protocol Number", _pdf_safe(str(meta.get("protocol_code") or "[Protocol Number]"))],
        ["Version Number and Date",
         _pdf_safe(f"{meta.get('version', '1.0')}; Dated {_fmt_date(meta.get('created_at'))}")],
        ["Investigational Product", _pdf_safe(drug.title() or "[Investigational Product]")],
        ["Study Type", _pdf_safe(str(meta.get("study_type") or "Bioequivalence"))],
        ["Design", _pdf_safe(str(results.get("design_key") or "—").title())],
        ["Sponsor", _pdf_safe(str(meta.get("sponsor") or "[Sponsor Name and Address]"))],
        ["Overall Bioequivalence Conclusion", _pdf_safe(_verdict_phrase(results.get("verdict")))],
    ]
    cover_tbl = Table(
        [[Paragraph(f"<b>{k}</b>", styles["Body"]), Paragraph(v, styles["Body"])]
         for k, v in cover_data],
        colWidths=[55 * rl_mm, 105 * rl_mm],
    )
    cover_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), rl_colors.HexColor("#F1F5F9")),
        ("GRID", (0, 0), (-1, -1), 0.5, rl_colors.HexColor("#CBD5E1")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(cover_tbl)
    story.append(Spacer(1, 10 * rl_mm))
    story.append(Paragraph(
        "<i>This Clinical Study Report is a DRAFT prepared for review by qualified "
        "clinical and regulatory professionals. All results are the deterministic "
        "output of the validated analysis and are presented verbatim; the narrative "
        "is subject to expert verification and sign-off before use.</i>",
        styles["Body"]))
    story.append(Paragraph(
        "<i>This report contains confidential information belonging to the Sponsor. "
        "It must not be used, divulged, published, or otherwise disclosed without the "
        "prior written consent of the Sponsor.</i>", styles["Body"]))
    story.append(PageBreak())

    # Table of contents
    story.append(Paragraph("TABLE OF CONTENTS", styles["FrontHeading"]))
    toc = TableOfContents()
    toc.levelStyles = [
        ParagraphStyle("TOCH1", fontName=_SANS_BOLD, fontSize=11, leading=18,
                       textColor=rl_colors.HexColor("#1E293B")),
        ParagraphStyle("TOCH2", fontName=_SANS, fontSize=10, leading=15, leftIndent=16,
                       textColor=rl_colors.HexColor("#334155")),
    ]
    story.append(toc)
    story.append(PageBreak())

    # Synopsis (unnumbered front matter)
    synopsis = _normalize_pk_notation((sections.get("synopsis") or "").strip())
    if _strip_html(synopsis):
        story.append(Paragraph(CSR_SECTION_TITLES.get("synopsis", "Synopsis"),
                               styles["FrontHeading"]))
        _add_section_content_pdf(story, synopsis, styles, 0, numbered=False)
        story.append(PageBreak())

    # Numbered E3 body
    idx = 0
    for section_name in CSR_SECTION_ORDER:
        if section_name in ("title", "synopsis"):
            continue
        raw = _normalize_pk_notation((sections.get(section_name) or "").strip())
        has_narrative = bool(_strip_html(raw))
        is_anchor = section_name in _ANCHOR_SECTIONS and _has_splice_content(results, section_name)
        if not has_narrative and not is_anchor:
            continue
        idx += 1

        title = CSR_SECTION_TITLES.get(section_name, section_name.replace("_", " ").title())
        story.append(Paragraph(f"{idx}  {title}", styles["Heading1"]))

        if has_narrative:
            _add_section_content_pdf(story, raw, styles, idx)
        if is_anchor:
            _splice_table_pdf(story, results, section_name, styles)
        if section_name == "references" and citations:
            _add_citations_pdf(story, citations, styles)

        story.append(Spacer(1, 4 * rl_mm))

    story.append(Spacer(1, 6 * rl_mm))
    story.append(Paragraph(
        "<i>This Clinical Study Report is a draft generated for review by qualified "
        "clinical and regulatory reviewers. The deterministic results tables above are "
        "the authoritative source of the reported figures.</i>", styles["Body"]))

    doc.multiBuild(story, canvasmaker=_csr_numbered_canvas(meta))
    buf.seek(0)
    return buf.getvalue()


def _add_citations_pdf(story: list, citations: list, styles: dict) -> None:
    story.append(Paragraph("Literature Citations", styles["Heading2"]))
    for i, cite in enumerate(citations, start=1):
        if isinstance(cite, dict):
            title = cite.get("title") or "Title unavailable"
            pmid = cite.get("pmid")
            text = f"[{i}] {title}" + (f" (PMID: {pmid})" if pmid else "")
        else:
            text = f"[{i}] {cite}"
        story.append(Paragraph(_pdf_safe(text), styles["Citation"]))
