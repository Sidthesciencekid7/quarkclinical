"""Export router — regulation-ready .docx and .pdf generation from stored protocols.

Document structure follows ICH CTD Module 5 / FDA BE Guidance conventions:
  - A4, 30 mm margins (ICH standard)
  - Cover page (metadata table)
  - Numbered sections  1.0 Title … 13.0 References  with N.M sub-headings
  - Header: Protocol ID | Drug INN | Guideline version  (skipped on cover page)
  - Footer: CONFIDENTIAL | Page N of M | Generated [date] · [Guideline]  (skipped on cover)
  - References: numbered bibliography with PubMed hyperlinks sourced from the database
  - All Word fields are standard unlocked field codes — fully editable in Microsoft Word
"""

from __future__ import annotations

import io
import math
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from docx import Document
from docx.enum.table import WD_ALIGN_VERTICAL
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import require_user
from database import get_db
from dependencies import audit_logger, authenticate_actor, get_protocol_scoped
from models import AuditLog, LiteratureCache, PKParameterRecord, Protocol, ProtocolVersion
from schemas import ExportRequest
from services.amendments import build_amendment_history
from services.protocol_qc import run_qc

# ---------------------------------------------------------------------------
# Optional PDF dependency (reportlab)
# ---------------------------------------------------------------------------
try:
    from reportlab.lib import colors as rl_colors
    from reportlab.lib.pagesizes import A4 as RL_A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
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
except ImportError:
    # Dummy values so module-level code parses cleanly even without reportlab
    rl_colors = None  # type: ignore[assignment]
    rl_canvas = None  # type: ignore[assignment]
    RL_A4 = (595.28, 841.89)  # A4 in points, fallback
    ParagraphStyle = getSampleStyleSheet = None  # type: ignore[assignment]
    rl_mm = 2.8346  # 1 mm in points, fallback
    PageBreak = Paragraph = SimpleDocTemplate = Spacer = Table = TableStyle = None  # type: ignore[assignment]
    TableOfContents = None  # type: ignore[assignment]
    _RL_OK = False


# ---------------------------------------------------------------------------
# Unicode font embedding (fixes "black square" .notdef glyphs)
# ---------------------------------------------------------------------------
# reportlab's base-14 fonts (Times-Roman / Helvetica) only carry WinAnsi/cp1252
# glyphs, so scientific notation routinely used in PK/BE protocols — the true
# minus sign (−), ≥, ≤, ∞, λ, etc. — renders as a black box. We embed a real
# Unicode TrueType serif (and sans for tables) when one can be found on the
# host, and fall back to lossless ASCII substitution (see _pdf_safe) otherwise.
#
# These module-level names are what every PDF style/table references; they
# resolve to the embedded family when registration succeeds, else to the
# base-14 names so the export still works with no Unicode font installed.
_SERIF = "Times-Roman"
_SERIF_BOLD = "Times-Bold"
_SERIF_ITALIC = "Times-Italic"
_SERIF_BOLDITALIC = "Times-BoldItalic"
_SANS = "Helvetica"
_SANS_BOLD = "Helvetica-Bold"
_UNICODE_PDF_FONT = False

# Candidate font files per family, tried in order. First entry that exists wins.
# Windows (user's machine) first, then common Linux locations for deployment.
_SERIF_CANDIDATES = [
    # (family, regular, bold, italic, bold-italic)
    ("STSerif", r"C:\Windows\Fonts\times.ttf", r"C:\Windows\Fonts\timesbd.ttf",
     r"C:\Windows\Fonts\timesi.ttf", r"C:\Windows\Fonts\timesbi.ttf"),
    ("STSerif", "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Italic.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSerif-BoldItalic.ttf"),
    ("STSerif", "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
     "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
     "/usr/share/fonts/truetype/liberation/LiberationSerif-Italic.ttf",
     "/usr/share/fonts/truetype/liberation/LiberationSerif-BoldItalic.ttf"),
]
_SANS_CANDIDATES = [
    ("STSans", r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\arialbd.ttf"),
    ("STSans", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("STSans", "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
     "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
]


def _register_unicode_fonts() -> None:
    """Best-effort embed of a Unicode serif (+sans) so scientific glyphs render.

    Idempotent and failure-tolerant: any problem leaves the base-14 fallbacks in
    place and _UNICODE_PDF_FONT False, so _pdf_safe still degrades gracefully.
    """
    global _SERIF, _SERIF_BOLD, _SERIF_ITALIC, _SERIF_BOLDITALIC
    global _SANS, _SANS_BOLD, _UNICODE_PDF_FONT
    if not _RL_OK:
        return
    import os as _os

    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.pdfmetrics import registerFontFamily
    from reportlab.pdfbase.ttfonts import TTFont

    # ---- serif (body + headings) ----
    for fam, reg, bold, ital, bital in _SERIF_CANDIDATES:
        if not _os.path.isfile(reg):
            continue
        try:
            pdfmetrics.registerFont(TTFont(fam, reg))
            b = f"{fam}-Bold" if _os.path.isfile(bold) else fam
            i = f"{fam}-Italic" if _os.path.isfile(ital) else fam
            bi = f"{fam}-BoldItalic" if _os.path.isfile(bital) else fam
            if b != fam:
                pdfmetrics.registerFont(TTFont(b, bold))
            if i != fam:
                pdfmetrics.registerFont(TTFont(i, ital))
            if bi != fam:
                pdfmetrics.registerFont(TTFont(bi, bital))
            # Maps <b>/<i>/<b><i> inline markup to the right TTF variant.
            registerFontFamily(fam, normal=fam, bold=b, italic=i, boldItalic=bi)
            _SERIF, _SERIF_BOLD, _SERIF_ITALIC, _SERIF_BOLDITALIC = fam, b, i, bi
            _UNICODE_PDF_FONT = True
            break
        except Exception:  # noqa: BLE001 — never let font issues break export
            continue

    # ---- sans (table headers / cover labels) ----
    for fam, reg, bold in _SANS_CANDIDATES:
        if not _os.path.isfile(reg):
            continue
        try:
            pdfmetrics.registerFont(TTFont(fam, reg))
            b = f"{fam}-Bold" if _os.path.isfile(bold) else fam
            if b != fam:
                pdfmetrics.registerFont(TTFont(b, bold))
            registerFontFamily(fam, normal=fam, bold=b, italic=fam, boldItalic=b)
            _SANS, _SANS_BOLD = fam, b
            break
        except Exception:  # noqa: BLE001
            continue


_register_unicode_fonts()


router = APIRouter(
    prefix="/api/export",
    tags=["export"],
    dependencies=[Depends(audit_logger)],
)

# "title"/"synopsis" are unnumbered front matter (cover + pre-Section-1
# synopsis, see _add_cover_page_docx / the synopsis special-case in the body
# loop below). The rest are the reference template's 16 numbered sections;
# "Overview of Study Procedures and Assessments" (the template's Section 8,
# the Schedule of Activities) is generated programmatically, not via AI, so it
# has no entry here — it is spliced into the numbering after "treatment".
SECTION_ORDER: list[str] = [
    "title",
    "synopsis",
    "introduction",
    "study_rationale",
    "objectives",
    "endpoints",
    "study_design",
    "population",
    "treatment",
    "study_assessments",
    "safety_monitoring",
    "data_handling",
    "study_monitoring",
    "statistical_analysis_plan",
    "ethics",
    "administrative",
    "references",
]

SECTION_TITLES: dict[str, str] = {
    "title": "Title",
    "synopsis": "Synopsis",
    "introduction": "Introduction",
    "study_rationale": "Study Rationale and Risk/Benefit Assessment",
    "objectives": "Study Objectives",
    "endpoints": "Study Endpoints",
    "study_design": "Study Design",
    "population": "Population",
    "treatment": "Treatment",
    "study_assessments": "Study Assessments",
    "safety_monitoring": "Safety Monitoring",
    "data_handling": "Data Handling and Record Keeping",
    "study_monitoring": "Study Monitoring, Auditing, and Inspecting",
    "statistical_analysis_plan": "Statistical Methods and Data Analysis",
    "ethics": "Ethical and Regulatory Standards",
    "administrative": "Administrative Procedures",
    "references": "References",
}

# Heading text for the programmatic Schedule-of-Activities section, spliced in
# as numbered Section 8 (matching the reference template) after "treatment".
_SOA_HEADING = "Overview of Study Procedures and Assessments"
_SOA_AFTER_SECTION = "treatment"

# Word relationship type for external hyperlinks
_HYPERLINK_REL = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"
)


def _content_disposition(filename: str) -> str:
    """Build an RFC 6266/5987-compliant Content-Disposition value.

    HTTP headers are Latin-1 encoded, so a bare f'filename="{name}"' throws
    UnicodeEncodeError('latin-1', ...) for any drug name with a non-Latin-1
    character — Unicode scripts, or common pharma symbols like (R)/(TM)/em-dash
    — crashing the whole export with a 500. Provides an ASCII-sanitized
    fallback (filename=, for older clients) alongside the full-fidelity name
    (filename*=UTF-8''..., which modern browsers use to save the real name).
    """
    from urllib.parse import quote
    # Filesystem-hostile characters, replaced regardless of encoding.
    safe = re.sub(r'[\\/:*?"<>|]', "_", filename)
    ascii_fallback = safe.encode("ascii", errors="ignore").decode("ascii").strip() or "protocol"
    encoded = quote(safe, safe="")
    return f'attachment; filename="{ascii_fallback}"; filename*=UTF-8\'\'{encoded}'


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


async def _amendment_history(db: AsyncSession, protocol_id) -> list[dict[str, Any]]:
    """Load all version snapshots and diff them into an amendment change list."""
    res = await db.execute(
        select(ProtocolVersion)
        .where(ProtocolVersion.protocol_id == protocol_id)
        .order_by(ProtocolVersion.version)
    )
    versions = res.scalars().all()
    return build_amendment_history([
        {"version": v.version, "sections": v.sections, "changed_by": v.changed_by,
         "change_type": v.change_type, "created_at": v.created_at}
        for v in versions
    ])


@router.post("/docx", dependencies=[Depends(require_user)])
async def export_docx(
    payload: ExportRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Assemble a protocol into a regulation-ready .docx file and log the export."""
    actor = await authenticate_actor(request, db)
    protocol = await get_protocol_scoped(db, payload.protocol_id, actor, load_drug=True)

    citations = await _fetch_citations(db, protocol.drug_id)
    amendments = await _amendment_history(db, protocol.id)
    drug_inn = protocol.drug.inn
    docx_bytes = _build_docx(protocol, drug_inn, citations, amendments)

    inn_slug = drug_inn.replace(" ", "_")
    filename = f"protocol_{inn_slug}_{protocol.guideline_version}_v{protocol.version}.docx"

    db.add(AuditLog(
        id=uuid.uuid4(),
        user_id=actor.id,
        action="EXPORT:docx",
        resource_type="protocol",
        resource_id=str(payload.protocol_id),
        diff={
            "format": "docx",
            "filename": filename,
            "version": protocol.version,
            "sections_exported": [s for s in SECTION_ORDER if protocol.sections.get(s)],
            "citations_included": len(citations),
            "exported_at": datetime.now(timezone.utc).isoformat(),
        },
        timestamp=datetime.now(timezone.utc),
    ))
    await db.commit()

    return StreamingResponse(
        io.BytesIO(docx_bytes),
        media_type=(
            "application/vnd.openxmlformats-officedocument"
            ".wordprocessingml.document"
        ),
        headers={"Content-Disposition": _content_disposition(filename)},
    )


@router.post("/pdf", dependencies=[Depends(require_user)])
async def export_pdf(
    payload: ExportRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Assemble a protocol into a PDF file and log the export."""
    if not _RL_OK:
        raise HTTPException(
            501,
            detail="PDF export requires the 'reportlab' package. Run: pip install reportlab",
        )

    actor = await authenticate_actor(request, db)
    protocol = await get_protocol_scoped(db, payload.protocol_id, actor, load_drug=True)

    citations = await _fetch_citations(db, protocol.drug_id)
    amendments = await _amendment_history(db, protocol.id)
    drug_inn = protocol.drug.inn
    pdf_bytes = _build_pdf(protocol, drug_inn, citations, amendments)

    inn_slug = drug_inn.replace(" ", "_")
    filename = f"protocol_{inn_slug}_{protocol.guideline_version}_v{protocol.version}.pdf"

    db.add(AuditLog(
        id=uuid.uuid4(),
        user_id=actor.id,
        action="EXPORT:pdf",
        resource_type="protocol",
        resource_id=str(payload.protocol_id),
        diff={
            "format": "pdf",
            "filename": filename,
            "version": protocol.version,
            "sections_exported": [s for s in SECTION_ORDER if protocol.sections.get(s)],
            "citations_included": len(citations),
            "exported_at": datetime.now(timezone.utc).isoformat(),
        },
        timestamp=datetime.now(timezone.utc),
    ))
    await db.commit()

    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": _content_disposition(filename)},
    )


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------


async def _fetch_citations(
    db: AsyncSession,
    drug_id: uuid.UUID,
) -> list[dict[str, str]]:
    """Return a deduplicated list of {pmid, title} dicts for the drug's literature."""
    # Collect unique PMIDs from PK parameter records for this drug
    pk_result = await db.execute(
        select(PKParameterRecord.source_pmid)
        .where(
            PKParameterRecord.drug_id == drug_id,
            PKParameterRecord.source_pmid.isnot(None),
        )
        .distinct()
    )
    pmids = [r[0] for r in pk_result.all() if r[0]]
    if not pmids:
        return []

    # Look up cached titles from LiteratureCache
    lit_result = await db.execute(
        select(LiteratureCache.pmid, LiteratureCache.title)
        .where(
            LiteratureCache.drug_id == drug_id,
            LiteratureCache.pmid.in_(pmids),
        )
    )
    title_map: dict[str, str] = {row.pmid: row.title for row in lit_result.all()}

    seen: set[str] = set()
    citations: list[dict[str, str]] = []
    for pmid in pmids:
        if pmid not in seen:
            seen.add(pmid)
            citations.append({"pmid": pmid, "title": title_map.get(pmid) or ""})
    return citations


# ---------------------------------------------------------------------------
# HTML utilities
# ---------------------------------------------------------------------------


def _strip_html(text: str) -> str:
    """Flatten TipTap HTML to plain text (used for empty-content checks and fallback)."""
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<p[^>]*>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<li[^>]*>", "• ", text, flags=re.IGNORECASE)
    text = re.sub(r"</li>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return (
        text.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&nbsp;", " ")
        .replace("&#39;", "'")
        .replace("&quot;", '"')
        .strip()
    )


# ---------------------------------------------------------------------------
# Informed consent: body -> appendix relocation
# ---------------------------------------------------------------------------
# The consent process is written by the generator as one <h2> block inside the
# Ethics section (see services/ai_service.py's "ethics" instructions). It is
# moved OUT of the numbered body and rendered as a protocol appendix instead:
# the body keeps the ethical-standards, IRB and confidentiality material a
# reviewer reads inline, and the consent process travels with the document as
# an annex - the same treatment the Informed Consent Form itself already gets
# as a standalone deliverable (/export/icf/docx).
#
# This is a RENDER-TIME relocation on purpose. The generator still writes the
# subsection where it has the `icf` locked facts in context, and the stored
# sections payload is untouched, so nothing is regenerated, no new AI section
# exists, and the in-app editor keeps one coherent Ethics section to edit.
# Matching is on the heading TEXT rather than position, so a generator that
# reorders the four subsections still relocates the right one.

_ICF_HEADING_RE = re.compile(
    r"<h2[^>]*>(?P<title>.*?)</h2>", re.IGNORECASE | re.DOTALL
)


def _split_informed_consent(html: str) -> tuple[str, str]:
    """Split an Ethics section into (body_without_consent, consent_block).

    ``consent_block`` is "" when no consent subsection is present, in which
    case the body is returned unchanged - so a protocol whose generator did
    not emit the subsection, or a legacy protocol predating it, renders
    exactly as before and no empty appendix appears.
    """
    if not html or not html.strip():
        return html, ""

    matches = list(_ICF_HEADING_RE.finditer(html))
    if not matches:
        return html, ""

    for i, m in enumerate(matches):
        title = _strip_html(m.group("title")).lower()
        if "informed consent" not in title and "consent process" not in title:
            continue
        start = m.start()
        # Runs to the next <h2> (a sibling subsection) or the end of the
        # section, so the heading and every paragraph under it travel together.
        end = matches[i + 1].start() if i + 1 < len(matches) else len(html)
        block = html[start:end].strip()
        remaining = (html[:start] + html[end:]).strip()
        return remaining, block

    return html, ""


# ---------------------------------------------------------------------------
# PK-parameter notation normalization
# ---------------------------------------------------------------------------
# The AI generates PK parameters with inconsistent notation across sections
# (e.g. one section writes "AUC0-inf / kel / t1/2 / Vz/F", another writes
# "AUC0-∞ / λz / t½ / C max"). This deterministically rewrites every variant —
# flat, <sub>-wrapped, en-dash, Greek, ∞ — to ONE canonical, properly-subscripted
# form at export time, so the whole document reads consistently regardless of
# run-to-run AI variation. Idempotent.
_NS = "(?:<sub>)?"
_NES = "(?:</sub>)?"
_ND = r"[-–—]"  # hyphen / en-dash / em-dash
_PK_NORM_RULES: list[tuple[str, str]] = [
    (rf"%\s*AUC\s*{_NS}ext(?:rap(?:olated)?)?{_NES}", "%AUC<sub>extrap</sub>"),
    (rf"AUC\s*{_NS}\(?0\s*{_ND}\s*(?:inf(?:inity)?|∞)\)?{_NES}", "AUC<sub>0–∞</sub>"),
    (rf"AUC\s*{_NS}(?:inf(?:inity)?|∞){_NES}(?![A-Za-z])", "AUC<sub>0–∞</sub>"),
    (rf"AUC\s*{_NS}\(?0\s*{_ND}\s*t\)?{_NES}", "AUC<sub>0–t</sub>"),
    (rf"AUC\s*{_NS}last{_NES}", "AUC<sub>last</sub>"),
    (rf"AUC\s*{_NS}\(?0\s*{_ND}\s*(?:tau|τ)\)?{_NES}", "AUC<sub>0–τ</sub>"),
    (rf"(?<![A-Za-z])C\s?{_NS}max{_NES}", "C<sub>max</sub>"),
    (rf"(?<![A-Za-z])C\s?{_NS}min{_NES}", "C<sub>min</sub>"),
    (rf"(?<![A-Za-z])C\s?{_NS}last{_NES}", "C<sub>last</sub>"),
    (rf"(?<![A-Za-z])C\s?{_NS}trough{_NES}", "C<sub>trough</sub>"),
    (rf"(?<![A-Za-z])T\s?{_NS}max{_NES}", "T<sub>max</sub>"),
    (rf"(?<![A-Za-z])t\s?{_NS}(?:1/2|½){_NES}", "t<sub>½</sub>"),
    (rf"(?<![A-Za-z])k\s?{_NS}el{_NES}", "λ<sub>z</sub>"),
    (rf"(?<![A-Za-z])(?:λ|lambda)\s?[_ ]?{_NS}z{_NES}", "λ<sub>z</sub>"),
    (rf"(?<![A-Za-z])V\s?{_NS}([zd]){_NES}\s*/\s*F", r"V<sub>\1</sub>/F"),
    (rf"(?<![A-Za-z])V\s?{_NS}ss{_NES}", "V<sub>ss</sub>"),
]
_PK_NORM_COMPILED = [(re.compile(p, re.IGNORECASE), r) for p, r in _PK_NORM_RULES]


def _normalize_pk_notation(html: str) -> str:
    """Rewrite all PK-parameter notation variants to one canonical subscripted form."""
    if not html:
        return html
    for rx, rep in _PK_NORM_COMPILED:
        html = rx.sub(rep, html)
    return html


def _normalize_pk_flat(text: str) -> str:
    """Canonical PK notation for PLAIN-TEXT renderers (no <sub> support): same
    symbols (AUC0-∞, t½, λz, Vz/F), but with the subscript tags stripped."""
    return _normalize_pk_notation(text).replace("<sub>", "").replace("</sub>", "")


# Strip a reproduced sample-size CALCULATION from AI content — the protocol
# should just state the sample size was calculated in R using the TOST method,
# not reproduce the PowerTOST console banner or tool internals. These targets are
# never legitimate protocol prose, so removal is safe (handles legacy content
# generated before the SAP prompt was tightened).
_TOST_PRE_RE = re.compile(
    r"<pre[^>]*>(?:(?!</pre>).)*?(?:Equivalence test|Sample size \(total\)|BE margins)"
    r"(?:(?!</pre>).)*?</pre>", re.IGNORECASE | re.DOTALL)
_TOST_PLAIN_BANNER_RE = re.compile(
    r"\+{5,}[^+]*Equivalence test.*?Sample size \(total\).*?\n?\s*\d+\s+[0-9]*\.[0-9]+",
    re.IGNORECASE | re.DOTALL)
_TOST_OWEN_P_RE = re.compile(
    r"<p>(?:(?!</p>).)*?Owen's Q-function(?:(?!</p>).)*?</p>", re.IGNORECASE | re.DOTALL)
_TOST_LEADIN_P_RE = re.compile(
    r"<p>(?:(?!</p>).)*?(?:calculation is reproduced|reproduced below|"
    r"shown below|is shown below)[:.]?(?:(?!</p>).)*?</p>", re.IGNORECASE | re.DOTALL)


def _strip_tost_dump(html: str) -> str:
    """Remove a reproduced sample-size calculation (PowerTOST console banner /
    tool-internals prose), keeping the plain 'calculated in R using the TOST
    method' statement around it."""
    if not html:
        return html
    html = _TOST_PRE_RE.sub("", html)
    html = _TOST_PLAIN_BANNER_RE.sub("", html)
    html = _TOST_OWEN_P_RE.sub("", html)
    html = _TOST_LEADIN_P_RE.sub("", html)
    return re.sub(r"<p>\s*</p>", "", html)


# ---------------------------------------------------------------------------
# DOCX builder
# ---------------------------------------------------------------------------


def _build_docx(
    protocol: Protocol,
    drug_inn: str,
    citations: list[dict[str, str]],
    amendments: list[dict[str, Any]] | None = None,
) -> bytes:
    doc = Document()

    # A4 page, template margins: 0.75" sides, 1" top/bottom
    sec = doc.sections[0]
    sec.page_width = int(8.27 * 914400)
    sec.page_height = int(11.69 * 914400)
    sec.left_margin = Inches(0.75)
    sec.right_margin = Inches(0.75)
    sec.top_margin = Inches(1.0)
    sec.bottom_margin = Inches(1.0)
    # Header/footer appear on EVERY page including the cover, matching the
    # reference clinical-trial-protocol house style.
    sec.different_first_page_header_footer = False

    _apply_doc_styles(doc)
    _enable_update_fields_on_open(doc)
    _set_header(sec, protocol, drug_inn)
    _set_footer(sec, protocol)

    # Cover page (no header/footer on first page due to different_first_page)
    _add_cover_page_docx(doc, protocol, drug_inn)
    doc.add_page_break()

    # Table of Contents (right after the cover; auto-refreshes on open)
    _add_toc_docx(doc)
    doc.add_page_break()

    # Abbreviations list (study-type aware)
    _add_abbreviations_docx(doc, protocol.study_type.value)
    doc.add_page_break()

    # Front matter: "title" only powers the cover subtitle (no body
    # restatement); "synopsis" renders as an unnumbered heading, matching the
    # reference template where the synopsis precedes the numbered Section 1.
    synopsis_text = _normalize_pk_notation(_strip_tost_dump((protocol.sections.get("synopsis") or "").strip()))
    if _strip_html(synopsis_text):
        # Manually-styled heading (not doc.add_heading/"Heading 1") so the Word
        # TOC field — which scans built-in Heading styles — does not pick up
        # this front-matter section, matching the template where the Synopsis
        # is not a TOC entry.
        p_syn = doc.add_paragraph()
        p_syn.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p_syn.paragraph_format.space_after = Pt(12)
        r_syn = p_syn.add_run("Study Protocol Synopsis")
        r_syn.bold = True
        r_syn.font.name = "Times New Roman"
        r_syn.font.size = Pt(14)
        _add_section_content_docx(doc, synopsis_text, 0, numbered=False)
        doc.add_page_break()

    # Numbered body (1 Introduction … 16 References per the reference
    # template). Number only sections with actual content so a gap never
    # appears; the Schedule of Activities is spliced in as its own numbered
    # section right after "treatment" (template Section 8).
    idx = 0
    consent_block = ""
    for section_name in SECTION_ORDER:
        if section_name in ("title", "synopsis"):
            continue
        raw_text = _normalize_pk_notation(_strip_tost_dump((protocol.sections.get(section_name) or "").strip()))
        # The consent process leaves the numbered body and is rendered as an
        # appendix below — see _split_informed_consent.
        if section_name == "ethics":
            raw_text, consent_block = _split_informed_consent(raw_text)
        plain = _strip_html(raw_text)
        if not plain:
            continue
        idx += 1

        section_title = SECTION_TITLES.get(
            section_name, section_name.replace("_", " ").title()
        )
        heading_para = doc.add_heading(f"{idx} {section_title}", level=1)
        # Bookmarked so the amendment appendix's PAGEREF fields (see
        # _add_pageref_suffix) can show this section's live, auto-updating
        # page number instead of only its section number.
        _add_heading_bookmark(heading_para, section_name, idx)

        if section_name == "references":
            _add_references_docx(doc, citations, raw_text, idx, protocol.study_type.value)
        elif section_name == "statistical_analysis_plan":
            _add_section_content_docx(doc, raw_text, idx)
            _add_sample_size_section_docx(doc, protocol, idx)
        elif section_name == "study_design":
            _add_section_content_docx(doc, raw_text, idx)
            _add_study_design_schematic_docx(doc, protocol)
        else:
            _add_section_content_docx(doc, raw_text, idx)

        if section_name == _SOA_AFTER_SECTION:
            idx += 1
            _add_schedule_of_activities_docx(doc, protocol, idx, _SOA_HEADING)

    # Appendix: informed consent process, relocated out of the Ethics section.
    _add_informed_consent_appendix_docx(doc, consent_block)

    # Appendix: protocol amendment history (only when amended).
    _add_amendment_appendix_docx(doc, protocol, amendments or [])

    # Appendix: deterministic QC checklist — the self-review travels with the doc.
    _add_qc_appendix_docx(doc, protocol)

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf.getvalue()


_QC_STATUS_WORD = {
    "fail": "FAIL", "warning": "WARNING", "assumption": "ASSUMPTION",
    "pass": "PASS", "not_applicable": "N/A",
}


def _qc_report_for(protocol: Protocol) -> dict:
    study_type = protocol.study_type.value
    return run_qc(
        study_type=study_type,
        population_config=protocol.population_config or {},
        sections=protocol.sections or {},
        expected_sections=SECTION_ORDER,
        section_labels=SECTION_TITLES,
    ).to_dict()


def _add_informed_consent_appendix_docx(doc: Document, consent_html: str) -> None:
    """Render the relocated informed-consent process as a protocol appendix.

    ``consent_html`` still carries its own <h2> heading from the generated
    Ethics section, so it is stripped here and replaced by the appendix
    heading — otherwise the appendix would open with two stacked titles.
    Rendered unnumbered: an appendix sits outside the ICH E6(R2) N.M
    subsection numbering the body uses.
    """
    if not consent_html or not _strip_html(consent_html):
        return
    body = _ICF_HEADING_RE.sub("", consent_html, count=1).strip()
    if not _strip_html(body):
        return

    doc.add_page_break()
    doc.add_heading("Appendix: Informed Consent Process", level=1)
    intro = doc.add_paragraph()
    ir = intro.add_run(
        "The Informed Consent Form itself is a separate, standalone document "
        "carrying its own version number and date, submitted to the IRB/IEC "
        "together with this protocol."
    )
    ir.italic = True
    ir.font.name = "Times New Roman"
    ir.font.size = Pt(9)
    _add_section_content_docx(doc, body, 0, numbered=False)


def _add_amendment_appendix_docx(doc: Document, protocol: Protocol,
                                 amendments: list[dict[str, Any]]) -> None:
    """Protocol amendment history (Summary of Changes) appendix for DOCX."""
    if not amendments:
        return
    num_map = _section_number_map(protocol)
    doc.add_page_break()
    doc.add_heading("Appendix: Summary of Changes (Protocol Amendment History)", level=1)
    p = doc.add_paragraph(
        "This appendix provides the traceability required by a 21 CFR Part 11 / "
        "ISO-style change-control process: every amendment is recorded below with the "
        "version, date, section changed (with its live page number), the type of "
        "change, and a summary of what changed. The complete before/after content of "
        "every version is retained in the electronic version history and audit trail.")
    for r in p.runs:
        r.font.name = "Times New Roman"; r.font.size = Pt(10.5)

    headers = ["Ver.", "Date", "Section", "Change", "What changed"]
    widths = [Inches(0.5), Inches(1.2), Inches(1.9), Inches(0.9), Inches(2.5)]
    SECTION_COL = 2
    tbl = doc.add_table(rows=1, cols=len(headers))
    tbl.style = "Table Grid"
    for c_idx, h in enumerate(headers):
        cell = tbl.rows[0].cells[c_idx]
        cell.text = ""
        run = cell.paragraphs[0].add_run(h)
        run.bold = True; run.font.name = "Times New Roman"; run.font.size = Pt(8)
        run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
        shd = OxmlElement("w:shd"); shd.set(qn("w:val"), "clear")
        shd.set(qn("w:color"), "auto"); shd.set(qn("w:fill"), "0B1E3D")
        cell._tc.get_or_add_tcPr().append(shd)
    for a in amendments:
        kind = {"added": "Added", "removed": "Removed", "modified": "Revised"}.get(a["kind"], a["kind"])
        cells = tbl.add_row().cells
        vals = [f"{a['version']}.0", a["date"], _section_ref(a["section_key"], num_map),
                f"{kind} ({a['change_type']})", _normalize_pk_flat(a["summary"])]
        for c_idx, (cell, val) in enumerate(zip(cells, vals)):
            cell.text = ""
            run = cell.paragraphs[0].add_run(val)
            run.font.name = "Times New Roman"; run.font.size = Pt(8)
        # A section removed by this amendment (no longer in the current
        # version) has no heading/bookmark left to point to.
        if a["section_key"] in num_map:
            _add_pageref_suffix(cells[SECTION_COL].paragraphs[0], a["section_key"])
        for c_idx, w in enumerate(widths):
            cells[c_idx].width = w
    note = doc.add_paragraph()
    nr = note.add_run(
        "Page numbers are Word fields bound to each section's heading — they "
        "resolve automatically when this document is opened or printed and stay "
        "correct even after further edits (right-click → Update Field, or "
        "Ctrl+A then F9, if a placeholder is shown instead of a number). Full 21 "
        "CFR Part 11 / ISO change-control attributes — reason for change, "
        "approver e-signatures, and effective date — are maintained in the "
        "electronic record.")
    nr.italic = True; nr.font.name = "Times New Roman"; nr.font.size = Pt(9)


def _add_qc_appendix_docx(doc: Document, protocol: Protocol) -> None:
    """Render the deterministic QC checklist as a closing appendix."""
    report = _qc_report_for(protocol)
    c = report["counts"]

    doc.add_page_break()
    doc.add_heading("Appendix: Automated QC Checklist", level=1)

    intro = doc.add_paragraph()
    intro.add_run("Readiness: ").bold = True
    intro.add_run(
        "No blocking issues — requires expert sign-off."
        if report["submission_ready"]
        else f"{c['fail']} blocking issue(s) to resolve before finalization."
    )
    doc.add_paragraph(
        f"{c['fail']} fail · {c['warning']} warning · {c['assumption']} assumption "
        f"· {c['pass']} pass · {c['not_applicable']} n/a"
    )

    table = doc.add_table(rows=1, cols=3)
    table.style = "Table Grid"
    hdr = table.rows[0].cells
    hdr[0].text, hdr[1].text, hdr[2].text = "Status", "Category", "Finding"
    for cell in hdr:
        for para in cell.paragraphs:
            for r in para.runs:
                r.bold = True

    for f in report["findings"]:
        cells = table.add_row().cells
        cells[0].text = _QC_STATUS_WORD.get(f["status"], f["status"].upper())
        cells[1].text = f["category"]
        cells[2].text = f"{f['title']} — {f['detail']}"

    note = doc.add_paragraph()
    nr = note.add_run(
        "Deterministic internal-consistency and scientific-soundness checks (no AI). "
        "These assist — they do not replace — review and sign-off by a qualified "
        "clinical pharmacologist."
    )
    nr.italic = True
    nr.font.size = Pt(8)


def _add_cover_page_docx(doc: Document, protocol: Protocol, drug_inn: str) -> None:
    """Professional cover page: centred title, study title, metadata table."""
    p_title = doc.add_paragraph()
    p_title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p_title.paragraph_format.space_before = Pt(48)
    p_title.paragraph_format.space_after = Pt(12)
    r = p_title.add_run("Clinical Trial Protocol")
    r.bold = True
    r.font.name = "Times New Roman"
    r.font.size = Pt(14)

    study_title = _strip_html(protocol.sections.get("title") or drug_inn.title()).strip()
    p_study = doc.add_paragraph()
    p_study.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p_study.paragraph_format.space_after = Pt(36)
    r2 = p_study.add_run(study_title)
    r2.font.name = "Times New Roman"
    r2.font.size = Pt(12)
    r2.bold = True

    doc.add_paragraph()

    rows = [
        ("Protocol Number", _protocol_code(protocol, drug_inn)),
        ("Version Number and Date",
         f"{protocol.version}.0; Dated {protocol.created_at.strftime('%d-%b-%Y')}"),
        ("Superseded Version and Date",
         "Not applicable (for version 1.0 of the protocol)"),
        ("Investigational Product", drug_inn.title()),
        ("Phase of Development", _phase_label(protocol.study_type.value)),
        ("IND Number", "[IND Number]"),
        ("Sponsor", protocol.org_id or "[Sponsor Name and Address]"),
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
        shd_l = OxmlElement("w:shd")
        shd_l.set(qn("w:val"), "clear")
        shd_l.set(qn("w:color"), "auto")
        shd_l.set(qn("w:fill"), "DDEEFF")
        lc._tc.get_or_add_tcPr().append(shd_l)

        vc = row.cells[1]
        vc.text = ""
        vr = vc.paragraphs[0].add_run(value)
        vr.font.name = "Times New Roman"
        vr.font.size = Pt(10.5)

    for row in table.rows:
        row.cells[0].width = Inches(2.0)
        row.cells[1].width = Inches(4.75)

    doc.add_paragraph()
    p_comp = doc.add_paragraph()
    p_comp.paragraph_format.space_before = Pt(24)
    rc = p_comp.add_run(
        "This study will be conducted in compliance with the protocol, "
        "Declaration of Helsinki, ICH E6(R2) Good Clinical Practice guidelines, "
        "and all applicable local regulatory requirements."
    )
    rc.font.name = "Times New Roman"
    rc.font.size = Pt(10.5)
    rc.italic = True

    p_conf = doc.add_paragraph()
    p_conf.paragraph_format.space_before = Pt(12)
    rcf = p_conf.add_run(
        "This protocol contains confidential information belonging to the Sponsor. "
        "It must not be used, divulged, published, or otherwise disclosed without "
        "the prior written consent of the Sponsor."
    )
    rcf.font.name = "Times New Roman"
    rcf.font.size = Pt(10.5)
    rcf.italic = True


def _add_cover_table(doc: Document, protocol: Protocol, drug_inn: str) -> None:
    """Legacy stub - calls new cover page function."""
    _add_cover_page_docx(doc, protocol, drug_inn)


def _protocol_code(protocol: Protocol, drug_inn: str) -> str:
    """Short protocol identifier for the header + cover (e.g. 'IBUP-001')."""
    slug = re.sub(r"[^A-Za-z]", "", drug_inn or "")[:4].upper() or "PROT"
    return f"{slug}-001"


def _phase_label(study_type: str) -> str:
    """Phase-of-development label matching the reference template's style."""
    return {
        "biosimilar_pk": "PK Similarity (Phase 1)",
        "food_effect": "Food Effect (Phase 1)",
    }.get(study_type, "Bioequivalence (Phase 1)")


def _hf_tab_line(para, left: str, right: str) -> None:
    """Header/footer line: left text + right-tab text, 8 pt slate-grey."""
    pPr = para._p.get_or_add_pPr()
    tabs = OxmlElement("w:tabs")
    rt = OxmlElement("w:tab")
    rt.set(qn("w:val"), "right")
    rt.set(qn("w:pos"), "9740")  # right margin: 8.27in page - 0.75in margins
    tabs.append(rt)
    pPr.append(tabs)
    for text in (left, "	" + right):
        r = para.add_run(text)
        r.font.size = Pt(8)
        r.font.color.rgb = RGBColor(0x64, 0x74, 0x8B)


def _set_header(section, protocol: Protocol, drug_inn: str) -> None:
    """Two-column running header on every page (matches the reference template).

    Line 1: 'Clinical Trial Protocol'  |  'Version No.: x.0'
    Line 2: <protocol code>            |  'Dated: dd-Mmm-yyyy'
    """
    header = section.header
    header.is_linked_to_previous = False
    p1 = header.paragraphs[0] if header.paragraphs else header.add_paragraph()
    p1.clear()
    _hf_tab_line(p1, "Clinical Trial Protocol", f"Version No.: {protocol.version}.0")
    p2 = header.add_paragraph()
    _hf_tab_line(
        p2,
        _protocol_code(protocol, drug_inn),
        f"Dated: {protocol.created_at.strftime('%d-%b-%Y')}",
    )


def _set_footer(section, protocol: Protocol) -> None:
    """Centered 'Page N of M' footer on every page (matches the reference template)."""
    footer = section.footer
    footer.is_linked_to_previous = False
    para = footer.paragraphs[0] if footer.paragraphs else footer.add_paragraph()
    para.clear()
    para.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _add_page_number(para, size_pt=9)


def _add_page_number(para, size_pt: int = 8) -> None:
    """Insert 'Page {PAGE} of {NUMPAGES}' standard Word field codes."""
    r0 = para.add_run("Page ")
    r0.font.size = Pt(size_pt)
    _append_field(para, " PAGE ", size_pt)
    r1 = para.add_run(" of ")
    r1.font.size = Pt(size_pt)
    _append_field(para, " NUMPAGES ", size_pt)


def _append_field(para, instr: str, size_pt: int | None = None) -> None:
    """Append a Word fldChar field (PAGE or NUMPAGES) to a paragraph element."""
    p_elem = para._p

    def _run(char_type: str | None = None, text: str | None = None) -> OxmlElement:
        r = OxmlElement("w:r")
        if size_pt:
            rPr = OxmlElement("w:rPr")
            sz = OxmlElement("w:sz")
            sz.set(qn("w:val"), str(size_pt * 2))
            rPr.append(sz)
            r.append(rPr)
        if char_type:
            fc = OxmlElement("w:fldChar")
            fc.set(qn("w:fldCharType"), char_type)
            r.append(fc)
        if text is not None:
            it = OxmlElement("w:instrText")
            it.set(qn("xml:space"), "preserve")
            it.text = text
            r.append(it)
        return r

    p_elem.append(_run(char_type="begin"))
    p_elem.append(_run(text=instr))
    p_elem.append(_run(char_type="end"))


def _unstyled_subheading_docx(
    doc: Document, text: str, *, size: int, italic: bool = False,
) -> None:
    """Bold paragraph sub-heading that does NOT use a Word 'Heading' style —
    keeps front-matter content (the Synopsis) out of the TOC field's scope."""
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(8)
    p.paragraph_format.space_after = Pt(2)
    r = p.add_run(text)
    r.bold = True
    r.italic = italic
    r.font.name = "Times New Roman"
    r.font.size = Pt(size)


def _add_section_content_docx(
    doc: Document,
    html: str,
    section_idx: int,
    numbered: bool = True,
) -> None:
    """Render a TipTap HTML section into Word paragraphs with inline formatting.

    Handles <p>, <h2>, <h3>, <ul>, <ol> block elements.
    Sub-headings are numbered N.M and N.M.L for ICH E6(R2) compliance, unless
    ``numbered=False`` (used for unnumbered front matter like the Synopsis).
    """
    if not html.strip():
        return

    # Plain text path — handles Markdown tables and paragraphs
    if not re.search(r"<\w", html):
        _add_markdown_content_docx(doc, html)
        return

    h2_counter = 0
    h3_counter = 0
    found_blocks = False

    for m in re.finditer(
        r"<(table|p|h2|h3|h4|ul|ol)([^>]*)>(.*?)</\1>",
        html,
        re.IGNORECASE | re.DOTALL,
    ):
        found_blocks = True
        tag = m.group(1).lower()
        inner = m.group(3)

        if tag == "table":
            _add_table_docx(doc, inner)

        elif tag == "h2":
            h2_counter += 1
            h3_counter = 0
            text = re.sub(r"<[^>]+>", "", inner).strip()
            if numbered:
                doc.add_heading(f"{section_idx}.{h2_counter} {text}", level=2)
            else:
                # Front matter (Synopsis): a manually-bold paragraph, NOT a Word
                # "Heading" style, so the TOC field (which scans Heading 1-3)
                # does not pick it up — the template's TOC starts at Section 1.
                _unstyled_subheading_docx(doc, text, size=12)

        elif tag in ("h3", "h4"):
            text = re.sub(r"<[^>]+>", "", inner).strip()
            if not numbered:
                _unstyled_subheading_docx(doc, text, size=11, italic=True)
            elif h2_counter == 0:
                # promote an orphan sub-heading to N.M (template uses 1.1, not 1.0.1)
                h2_counter += 1
                h3_counter = 0
                doc.add_heading(f"{section_idx}.{h2_counter} {text}", level=2)
            else:
                h3_counter += 1
                doc.add_heading(
                    f"{section_idx}.{h2_counter}.{h3_counter} {text}", level=3
                )

        elif tag == "ul":
            for li_m in re.finditer(
                r"<li[^>]*>(.*?)</li>", inner, re.IGNORECASE | re.DOTALL
            ):
                para = doc.add_paragraph(style="List Bullet")
                _add_inline_runs(para, li_m.group(1))

        elif tag == "ol":
            for li_m in re.finditer(
                r"<li[^>]*>(.*?)</li>", inner, re.IGNORECASE | re.DOTALL
            ):
                para = doc.add_paragraph(style="List Number")
                _add_inline_runs(para, li_m.group(1))

        elif tag == "p":
            para = doc.add_paragraph()
            _add_inline_runs(para, inner)

    # Fallback: unrecognised HTML structure — strip tags
    if not found_blocks:
        clean = _strip_html(html)
        for chunk in clean.split("\n\n"):
            if chunk.strip():
                doc.add_paragraph(chunk.strip())


def _add_inline_runs(para, html_frag: str) -> None:
    """Decompose inline TipTap HTML into bold/italic/highlight/sub/sup Word runs."""
    parts = re.split(r"(</?[a-zA-Z][^>]*>)", html_frag)
    attrs: dict[str, bool] = {
        "bold": False, "italic": False, "highlight": False,
        "sub": False, "sup": False,
    }

    for part in parts:
        if not part:
            continue
        if part.startswith("<"):
            closing = part.startswith("</")
            m = re.match(r"</?([a-zA-Z]+)", part)
            if not m:
                continue
            tag = m.group(1).lower()
            if tag in ("strong", "b"):
                attrs = {**attrs, "bold": not closing}
            elif tag in ("em", "i"):
                attrs = {**attrs, "italic": not closing}
            elif tag == "mark":
                attrs = {**attrs, "highlight": not closing}
            elif tag == "sub":
                attrs = {**attrs, "sub": not closing}
            elif tag == "sup":
                attrs = {**attrs, "sup": not closing}
            elif tag == "br" and not closing:
                para.add_run().add_break()
        else:
            text = (
                part.replace("&amp;", "&")
                .replace("&lt;", "<")
                .replace("&gt;", ">")
                .replace("&nbsp;", " ")
                .replace("&#39;", "'")
                .replace("&quot;", '"')
            )
            if text:
                run = para.add_run(text)
                run.bold = attrs["bold"]
                run.italic = attrs["italic"]
                if attrs["highlight"]:
                    rpr = run._r.get_or_add_rPr()
                    hl = OxmlElement("w:highlight")
                    hl.set(qn("w:val"), "yellow")
                    rpr.append(hl)
                if attrs["sub"]:
                    rpr = run._r.get_or_add_rPr()
                    va = OxmlElement("w:vertAlign")
                    va.set(qn("w:val"), "subscript")
                    rpr.append(va)
                elif attrs["sup"]:
                    rpr = run._r.get_or_add_rPr()
                    va = OxmlElement("w:vertAlign")
                    va.set(qn("w:val"), "superscript")
                    rpr.append(va)


def _add_table_docx(doc: Document, table_inner_html: str) -> None:
    """Render HTML table content as a styled Word table.

    Expects table_inner_html to be the content between <table> and </table>,
    i.e. the <thead>/<tbody>/<tr>/<th>/<td> structure.
    """
    rows_html = re.findall(r"<tr[^>]*>(.*?)</tr>", table_inner_html, re.IGNORECASE | re.DOTALL)
    if not rows_html:
        return

    # Determine which rows are header rows (contain <th> or are inside <thead>)
    thead_content = ""
    thead_m = re.search(r"<thead[^>]*>(.*?)</thead>", table_inner_html, re.IGNORECASE | re.DOTALL)
    if thead_m:
        thead_content = thead_m.group(1)

    all_rows: list[list[str]] = []
    header_flags: list[bool] = []
    for row_html in rows_html:
        th_cells = re.findall(r"<th[^>]*>(.*?)</th>", row_html, re.IGNORECASE | re.DOTALL)
        td_cells = re.findall(r"<td[^>]*>(.*?)</td>", row_html, re.IGNORECASE | re.DOTALL)
        is_hdr = bool(th_cells) or (thead_content and row_html in thead_content)
        cells = th_cells if th_cells else td_cells
        if cells:
            all_rows.append(cells)
            header_flags.append(is_hdr)

    if not all_rows:
        return

    max_cols = max(len(r) for r in all_rows)
    # Pad short rows
    all_rows = [r + [""] * (max_cols - len(r)) for r in all_rows]

    tbl = doc.add_table(rows=len(all_rows), cols=max_cols)
    tbl.style = "Table Grid"

    # Available body width: 8.27" page − 2 × 1.18" margins = 5.91"
    avail_w = Inches(6.75)
    col_w = avail_w / max_cols

    for r_idx, (row_cells, is_hdr) in enumerate(zip(all_rows, header_flags)):
        row = tbl.rows[r_idx]
        for c_idx, cell_html in enumerate(row_cells):
            cell = row.cells[c_idx]
            cell.text = ""
            para = cell.paragraphs[0]
            para.paragraph_format.space_after = Pt(0)
            _add_inline_runs(para, cell_html)

            tc_pr = cell._tc.get_or_add_tcPr()
            # Cell padding via tcMar
            mar = OxmlElement("w:tcMar")
            for side in ("top", "bottom", "left", "right"):
                m_el = OxmlElement(f"w:{side}")
                m_el.set(qn("w:w"), "80")
                m_el.set(qn("w:type"), "dxa")
                mar.append(m_el)
            tc_pr.append(mar)

            shd = OxmlElement("w:shd")
            shd.set(qn("w:val"), "clear")
            shd.set(qn("w:color"), "auto")
            if is_hdr:
                shd.set(qn("w:fill"), "1E3A5F")  # dark navy header
                tc_pr.append(shd)
                for run in para.runs:
                    run.bold = True
                    run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
                    run.font.size = Pt(9)
            else:
                fill = "F8FAFC" if r_idx % 2 == 0 else "FFFFFF"
                shd.set(qn("w:fill"), fill)
                tc_pr.append(shd)
                for run in para.runs:
                    run.font.size = Pt(9)

        # Set column widths for the row
        for cell in row.cells:
            cell.width = col_w

    doc.add_paragraph()  # spacing after table


def _add_hyperlink(para, text: str, url: str) -> None:
    """Append a clickable hyperlink run to a paragraph (uses Word relationship API)."""
    r_id = para.part.relate_to(url, _HYPERLINK_REL, is_external=True)
    hlink = OxmlElement("w:hyperlink")
    hlink.set(qn("r:id"), r_id)
    hlink.set(qn("w:history"), "1")

    run = OxmlElement("w:r")
    rPr = OxmlElement("w:rPr")
    rStyle = OxmlElement("w:rStyle")
    rStyle.set(qn("w:val"), "Hyperlink")
    rPr.append(rStyle)
    run.append(rPr)

    t = OxmlElement("w:t")
    t.text = text
    run.append(t)

    hlink.append(run)
    para._p.append(hlink)


# Regulatory / guidance references cited in-text across the protocol body, so the
# bibliography resolves those citations rather than only the PubMed PMIDs.
_REG_REFS_BE = [
    "U.S. Food and Drug Administration. Guidance for Industry: Bioavailability and Bioequivalence Studies Submitted in NDAs or INDs — General Considerations. March 2014.",
    "U.S. Food and Drug Administration. Guidance for Industry: Statistical Approaches to Establishing Bioequivalence. January 2001.",
    "U.S. Food and Drug Administration. Guidance for Industry: Bioequivalence Studies with Pharmacokinetic Endpoints for Drugs Submitted Under an ANDA. December 2013.",
    "U.S. Food and Drug Administration. Guidance for Industry: Bioanalytical Method Validation. May 2018.",
    "ICH Harmonised Guideline E6(R2): Good Clinical Practice. 2016.",
    "ICH Harmonised Guideline E9(R1): Statistical Principles for Clinical Trials — Estimands and Sensitivity Analysis. 2019.",
    "U.S. Code of Federal Regulations, Title 21, Part 320 — Bioavailability and Bioequivalence Requirements.",
]
_REG_REFS_BIOSIMILAR = [
    "U.S. Food and Drug Administration. Scientific Considerations in Demonstrating Biosimilarity to a Reference Product. April 2015.",
    "U.S. Food and Drug Administration. Immunogenicity Assessment for Therapeutic Protein Products. August 2014.",
    "U.S. Food and Drug Administration. Assay Development and Validation for Immunogenicity Testing of Therapeutic Protein Products. April 2019.",
    "U.S. Food and Drug Administration. Clinical Pharmacology Data to Support a Demonstration of Biosimilarity to a Reference Product. December 2016.",
    "U.S. Food and Drug Administration. Guidance for Industry: Bioanalytical Method Validation. May 2018.",
    "ICH Harmonised Guideline E6(R2): Good Clinical Practice. 2016.",
    "EMA. Guideline on Immunogenicity Assessment of Therapeutic Proteins (EMEA/CHMP/BMWP/14327/2006 Rev.1).",
]


def _add_references_docx(
    doc: Document,
    citations: list[dict[str, str]],
    ai_text: str,
    section_idx: int,
    study_type: str = "BE",
) -> None:
    """Render references: AI preamble + regulatory guidances + numbered PubMed bibliography."""
    preamble = _strip_html(ai_text)
    if preamble.strip():
        for chunk in preamble.strip().split("\n\n"):
            if chunk.strip():
                doc.add_paragraph(chunk.strip())
        doc.add_paragraph()

    # Regulatory & guidance references (resolve in-text guidance citations)
    reg_refs = (
        _REG_REFS_BIOSIMILAR if study_type == "biosimilar_pk" else _REG_REFS_BE
    )
    doc.add_heading(f"{section_idx}.1 Regulatory and Guidance References", level=2)
    for i, ref in enumerate(reg_refs, start=1):
        para = doc.add_paragraph()
        para.paragraph_format.left_indent = Inches(0.4)
        para.paragraph_format.first_line_indent = Inches(-0.4)
        num_run = para.add_run(f"[R{i}]  ")
        num_run.bold = True
        para.add_run(ref)

    if not citations:
        return

    doc.add_heading(f"{section_idx}.2 Literature Citations", level=2)

    for i, cite in enumerate(citations, start=1):
        para = doc.add_paragraph()
        para.paragraph_format.left_indent = Inches(0.4)
        para.paragraph_format.first_line_indent = Inches(-0.4)

        num_run = para.add_run(f"[{i}]  ")
        num_run.bold = True

        title = (cite.get("title") or "Title unavailable") + ".  "
        para.add_run(title)

        pmid = cite["pmid"]
        pubmed_url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
        _add_hyperlink(para, f"PMID: {pmid}", pubmed_url)


# ---------------------------------------------------------------------------
# PDF builder (reportlab Platypus)
# ---------------------------------------------------------------------------


def _build_pdf(
    protocol: Protocol,
    drug_inn: str,
    citations: list[dict[str, str]],
    amendments: list[dict[str, Any]] | None = None,
) -> bytes:
    """Build the protocol PDF, resolving real page numbers into the amendment
    appendix (not just section numbers) when the protocol has been amended.

    reportlab lays out the whole story before any page number is final, so the
    amendment table's cell text — fixed in Python before layout runs — can't
    know a section's page on the same pass that discovers it (unlike the TOC
    flowable, which is a special self-resolving widget). So: run the full
    build once to populate section_key -> page via the SAME afterFlowable hook
    that feeds the TOC (see _build_pdf_once), then, only if there are
    amendments to show, rebuild once more now that every section's page is
    known. Nothing after the appendix depends on those numbers, so redoing the
    build with real numbers in the (last) appendix can't disturb them.
    """
    page_map: dict[str, int] = {}
    pdf_bytes = _build_pdf_once(protocol, drug_inn, citations, amendments, page_map)
    if amendments:
        pdf_bytes = _build_pdf_once(protocol, drug_inn, citations, amendments, page_map)
    return pdf_bytes


def _build_pdf_once(
    protocol: Protocol,
    drug_inn: str,
    citations: list[dict[str, str]],
    amendments: list[dict[str, Any]] | None,
    page_map: dict[str, int],
) -> bytes:
    """Build a PDF using reportlab Platypus with a two-pass NumberedCanvas.

    page_map is mutated in place with each numbered section's resolved page
    (section_key -> page) as a side effect of this call — see _build_pdf.
    """
    assert _RL_OK, "reportlab must be installed (pip install reportlab)"

    buf = io.BytesIO()
    margin = 30 * rl_mm
    NumberedCanvas = _make_numbered_canvas(protocol, drug_inn)

    # TOC-aware template: afterFlowable reports each H1/H2 heading (with its
    # resolved page number) to the TableOfContents flowable. multiBuild() then
    # re-runs the build until those page numbers converge. The same hook
    # records each H1's page into page_map, keyed by the section_key tagged
    # onto it below — by convergence page_map is fully resolved too.
    class _TocDocTemplate(SimpleDocTemplate):
        def afterFlowable(self, flowable: Any) -> None:
            if flowable.__class__.__name__ != "Paragraph":
                return
            name = getattr(flowable.style, "name", "")
            if name == "H1":
                self.notify("TOCEntry", (0, flowable.getPlainText(), self.page))
                section_key = getattr(flowable, "_section_key", None)
                if section_key:
                    page_map[section_key] = self.page
            elif name == "H2":
                self.notify("TOCEntry", (1, flowable.getPlainText(), self.page))

    doc = _TocDocTemplate(
        buf,
        pagesize=RL_A4,
        leftMargin=margin,
        rightMargin=margin,
        topMargin=38 * rl_mm,
        bottomMargin=28 * rl_mm,
        title=f"Clinical Study Protocol — {drug_inn.title()}",
        subject=f"v{protocol.version} · {protocol.guideline_version}",
        author="PK/BE Protocol Platform",
    )

    styles = _pdf_styles()
    story: list[Any] = []

    # Cover page (matches reference template)
    story.append(Paragraph("Clinical Trial Protocol", styles["CoverTitle"]))
    story.append(Spacer(1, 6 * rl_mm))
    _study_title = _strip_html(protocol.sections.get("title") or drug_inn.title()).strip()
    story.append(Paragraph(_study_title, styles["CoverSubtitle"]))
    story.append(Spacer(1, 10 * rl_mm))

    cover_data = [
        ["Protocol Number", _protocol_code(protocol, drug_inn)],
        ["Version Number and Date",
         f"{protocol.version}.0; Dated {protocol.created_at.strftime('%d-%b-%Y')}"],
        ["Superseded Version and Date", "Not applicable (for version 1.0 of the protocol)"],
        ["Investigational Product", drug_inn.title()],
        ["Phase of Development", _phase_label(protocol.study_type.value)],
        ["IND Number", "[IND Number]"],
        ["Sponsor", protocol.org_id or "[Sponsor Name and Address]"],
    ]
    cover_tbl = Table(cover_data, colWidths=[50 * rl_mm, 110 * rl_mm])
    cover_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), rl_colors.HexColor("#F1F5F9")),
        ("FONTNAME", (0, 0), (0, -1), _SANS_BOLD),
        ("FONTNAME", (1, 0), (1, -1), _SANS),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
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
        "<i>This study will be conducted in compliance with the protocol, the "
        "Declaration of Helsinki, ICH E6(R2) Good Clinical Practice guidelines, and "
        "all applicable regulatory requirements.</i>", styles["Body"]))
    story.append(Paragraph(
        "<i>This protocol contains confidential information belonging to the Sponsor. "
        "It must not be used, divulged, published, or otherwise disclosed without the "
        "prior written consent of the Sponsor.</i>", styles["Body"]))
    story.append(PageBreak())

    # Table of Contents (page numbers fill in via multiBuild)
    story.append(Paragraph("TABLE OF CONTENTS", styles["FrontHeading"]))
    toc = TableOfContents()
    toc.levelStyles = [
        ParagraphStyle(
            "TOCH1", fontName=_SERIF_BOLD, fontSize=11, leading=20,
            textColor=rl_colors.HexColor("#1E293B"),
        ),
        ParagraphStyle(
            "TOCH2", fontName=_SERIF, fontSize=10, leading=15, leftIndent=16,
            textColor=rl_colors.HexColor("#334155"),
        ),
    ]
    story.append(toc)
    story.append(PageBreak())

    # List of abbreviations (parity with DOCX)
    _add_abbreviations_pdf(story, protocol.study_type.value, styles)
    story.append(PageBreak())

    # Front matter: "title" only powers the cover subtitle; "synopsis" renders
    # as an unnumbered section (FrontHeading/FrontSub styles keep it out of
    # the TOC), matching the template where the synopsis precedes Section 1.
    synopsis_raw = _normalize_pk_notation(_strip_tost_dump((protocol.sections.get("synopsis") or "").strip()))
    if _strip_html(synopsis_raw):
        story.append(Paragraph("Study Protocol Synopsis", styles["FrontHeading"]))
        _add_section_content_pdf(story, synopsis_raw, styles, 0, numbered=False)
        story.append(PageBreak())

    # Numbered body (1 Introduction … 16 References per the reference
    # template). The Schedule of Activities is spliced in as its own numbered
    # section right after "treatment" (template Section 8).
    idx = 0
    consent_block = ""
    for section_name in SECTION_ORDER:
        if section_name in ("title", "synopsis"):
            continue
        raw = _normalize_pk_notation(_strip_tost_dump((protocol.sections.get(section_name) or "").strip()))
        # The consent process leaves the numbered body and is rendered as an
        # appendix below — see _split_informed_consent.
        if section_name == "ethics":
            raw, consent_block = _split_informed_consent(raw)
        if not _strip_html(raw):
            continue
        idx += 1

        section_title = SECTION_TITLES.get(
            section_name, section_name.replace("_", " ").title()
        )
        h1 = Paragraph(f"{idx}  {section_title}", styles["Heading1"])
        # Read by _TocDocTemplate.afterFlowable to populate page_map — see
        # _build_pdf's docstring for why the amendment appendix needs this.
        h1._section_key = section_name
        story.append(h1)

        if section_name == "references":
            _add_references_pdf(story, citations, raw, styles, idx, protocol.study_type.value)
        elif section_name == "statistical_analysis_plan":
            _add_section_content_pdf(story, raw, styles, idx)
            _add_sample_size_section_pdf(story, protocol, styles, idx)
        elif section_name == "study_design":
            _add_section_content_pdf(story, raw, styles, idx)
            _add_study_design_schematic_pdf(story, protocol, styles, idx)
        else:
            _add_section_content_pdf(story, raw, styles, idx)

        story.append(Spacer(1, 4 * rl_mm))

        if section_name == _SOA_AFTER_SECTION:
            idx += 1
            _add_schedule_of_activities_pdf(story, protocol, styles, idx, _SOA_HEADING)

    # Appendix: informed consent process, relocated out of the Ethics section.
    _add_informed_consent_appendix_pdf(story, consent_block, styles)

    # Appendix: protocol amendment history (only when the protocol has been amended).
    _add_amendment_appendix_pdf(story, protocol, amendments or [], styles, page_map)

    # Appendix: deterministic QC checklist (parity with the DOCX export).
    _add_qc_appendix_pdf(story, protocol, styles)

    # multiBuild re-runs until the TOC page numbers stabilise; NumberedCanvas
    # still supplies the "Page N of M" footer on each pass.
    doc.multiBuild(story, canvasmaker=NumberedCanvas)
    buf.seek(0)
    return buf.getvalue()


# Lossless-ish ASCII fallbacks for glyphs absent from the base-14 WinAnsi font.
# Only applied when NO Unicode font could be embedded (_UNICODE_PDF_FONT False),
# so we never show a black .notdef box even on a host with no TrueType fonts.
_GLYPH_FALLBACKS = {
    "−": "-",   "≈": "~",  "≥": ">=", "≤": "<=", "≠": "!=",
    "∞": "infinity", "×": "x", "÷": "/", "·": "*", "±": "+/-",
    "λ": "lambda", "α": "alpha", "β": "beta", "γ": "gamma", "μ": "u", "µ": "u",
    "→": "->", "←": "<-", "≅": "~", "≡": "=", "√": "sqrt",
    "½": "1/2", "¼": "1/4", "¾": "3/4",
    "‰": " per mil", "′": "'", "″": '"',
}


def _pdf_safe(s: str) -> str:
    """Escape XML for reportlab paragraphs, handling glyphs the font lacks.

    With a Unicode font embedded (the normal case) the text passes through
    unchanged except XML escaping — every scientific glyph renders. Without one,
    we substitute ASCII equivalents so nothing becomes a black square.
    """
    from xml.sax.saxutils import escape
    if not _UNICODE_PDF_FONT:
        for k, v in _GLYPH_FALLBACKS.items():
            s = s.replace(k, v)
    return escape(s)


def _safe_paragraph(text: str, style: Any) -> Any:
    """Build a reportlab Paragraph, surviving malformed inline markup.

    AI-generated HTML occasionally contains unbalanced inline tags (e.g. a stray
    </sub> with no opener). reportlab's paragraph parser raises on those, which
    would abort the whole export. On failure we retry with all tags stripped and
    re-escaped so the content still renders as plain text rather than crashing.
    """
    try:
        return Paragraph(text, style)
    except Exception:
        from xml.sax.saxutils import escape
        plain = re.sub(r"<[^>]+>", "", text)
        plain = escape(plain.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">"))
        try:
            return Paragraph(plain, style)
        except Exception:
            return Paragraph("", style)


def _section_number_map(protocol: Protocol) -> dict[str, tuple[int, str]]:
    """Map each section key to its (number, title) exactly as numbered in the body,
    so the amendment appendix can cite section numbers. Mirrors the body loop
    (skips title/synopsis, splices the SoA number after 'treatment')."""
    m: dict[str, tuple[int, str]] = {}
    idx = 0
    for name in SECTION_ORDER:
        if name in ("title", "synopsis"):
            continue
        raw = (protocol.sections.get(name) or "").strip()
        if not _strip_html(raw):
            continue
        idx += 1
        m[name] = (idx, SECTION_TITLES.get(name, name.replace("_", " ").title()))
        if name == _SOA_AFTER_SECTION:
            idx += 1  # the programmatic Schedule of Activities takes the next number
    return m


def _section_ref(
    section_key: str,
    num_map: dict[str, tuple[int, str]],
    page_map: dict[str, int] | None = None,
) -> str:
    """Human 'Section N — Title, p. X' reference for an amendment row.

    page_map (section_key -> resolved page number) is only known on the
    second of _build_pdf's two build passes (see there) — a fresh/empty
    page_map degrades gracefully to the section-number-only form used before
    this existed, so a caller mid-first-pass never sees a wrong page number.
    """
    if section_key in ("title", "synopsis"):
        return section_key.title()
    if section_key in num_map:
        n, title = num_map[section_key]
        page = (page_map or {}).get(section_key)
        suffix = f", p. {page}" if page else ""
        return f"Section {n} — {title}{suffix}"
    # Section not in the current document (e.g. removed in this amendment).
    pretty = SECTION_TITLES.get(section_key, section_key.replace("_", " ").title())
    return f"{pretty} (not in current version)"


def _add_informed_consent_appendix_pdf(story: list, consent_html: str,
                                       styles: dict) -> None:
    """Informed-consent appendix for the PDF (parity with the DOCX export).

    Same contract as ``_add_informed_consent_appendix_docx``: the block's own
    <h2> is dropped in favour of the appendix heading, and nothing is emitted
    when there is no consent subsection to relocate.
    """
    if not consent_html or not _strip_html(consent_html):
        return
    body = _ICF_HEADING_RE.sub("", consent_html, count=1).strip()
    if not _strip_html(body):
        return

    story.append(PageBreak())
    story.append(Paragraph("Appendix: Informed Consent Process", styles["Heading1"]))
    story.append(Paragraph(
        "<i>The Informed Consent Form itself is a separate, standalone document "
        "carrying its own version number and date, submitted to the IRB/IEC "
        "together with this protocol.</i>",
        styles["Citation"],
    ))
    _add_section_content_pdf(story, body, styles, 0, numbered=False)


def _add_amendment_appendix_pdf(
    story: list, protocol: Protocol, amendments: list[dict[str, Any]], styles: dict,
    page_map: dict[str, int] | None = None,
) -> None:
    """Render the protocol amendment history (Summary of Changes) as an appendix.
    Only emitted when the protocol has been amended (>= 1 change beyond v1)."""
    if not _RL_OK or not amendments:
        return
    num_map = _section_number_map(protocol)

    story.append(PageBreak())
    story.append(Paragraph("Appendix: Summary of Changes (Protocol Amendment History)",
                           styles["Heading1"]))
    story.append(Paragraph(
        "This appendix provides the traceability required by a 21 CFR Part 11 / "
        "ISO-style change-control process: every amendment to this protocol is "
        "recorded below with the version, date, the section changed (with its "
        "page number), the type of change, and a summary of what changed. The "
        "complete before/after content of every version is retained in the "
        "electronic version history and audit trail.",
        styles["Body"]))
    story.append(Spacer(1, 3 * rl_mm))

    def _hdr(t: str):
        return Paragraph(f'<font color="white"><b>{t}</b></font>', styles["Citation"])

    data = [[_hdr("Ver."), _hdr("Date"), _hdr("Section"), _hdr("Change"), _hdr("What changed")]]
    for a in amendments:
        kind = {"added": "Added", "removed": "Removed", "modified": "Revised"}.get(a["kind"], a["kind"])
        data.append([
            Paragraph(f"{a['version']}.0", styles["Citation"]),
            Paragraph(_pdf_safe(a["date"]), styles["Citation"]),
            Paragraph(_pdf_safe(_section_ref(a["section_key"], num_map, page_map)), styles["Citation"]),
            Paragraph(f"{_pdf_safe(kind)}<br/><font size=\"7\">{_pdf_safe(a['change_type'])}</font>",
                      styles["Citation"]),
            Paragraph(_pdf_safe(_normalize_pk_flat(a["summary"])), styles["Citation"]),
        ])
    tbl = Table(data, colWidths=[12 * rl_mm, 30 * rl_mm, 42 * rl_mm, 22 * rl_mm, 62 * rl_mm],
                repeatRows=1)
    tbl.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, rl_colors.HexColor("#cbd5e1")),
        ("BACKGROUND", (0, 0), (-1, 0), rl_colors.HexColor("#0B1E3D")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.append(tbl)
    story.append(Spacer(1, 3 * rl_mm))
    story.append(Paragraph(
        "<i>Full 21 CFR Part 11 / ISO change-control attributes — reason for "
        "change, approver e-signatures, and effective date — are maintained in "
        "the electronic record.</i>", styles["Citation"]))


def _add_qc_appendix_pdf(story: list, protocol: Protocol, styles: dict) -> None:
    """Render the deterministic QC checklist as a closing appendix (PDF parity)."""
    report = _qc_report_for(protocol)
    c = report["counts"]

    story.append(PageBreak())
    story.append(Paragraph("Appendix: Automated QC Checklist", styles["Heading1"]))
    ready = (
        "No blocking issues — requires expert sign-off."
        if report["submission_ready"]
        else f"{c['fail']} blocking issue(s) to resolve before finalization."
    )
    story.append(Paragraph(f"<b>Readiness:</b> {_pdf_safe(ready)}", styles["Body"]))
    story.append(Paragraph(
        f"{c['fail']} fail · {c['warning']} warning · {c['assumption']} assumption "
        f"· {c['pass']} pass · {c['not_applicable']} n/a", styles["Body"]))
    story.append(Spacer(1, 3 * rl_mm))

    def _hdr(label: str):
        return Paragraph(f'<font color="white"><b>{label}</b></font>', styles["Citation"])

    data = [[_hdr("Status"), _hdr("Category"), _hdr("Finding")]]
    for f in report["findings"]:
        data.append([
            Paragraph(_QC_STATUS_WORD.get(f["status"], f["status"].upper()), styles["Citation"]),
            Paragraph(_pdf_safe(f["category"]), styles["Citation"]),
            Paragraph(f"<b>{_pdf_safe(f['title'])}</b> — {_pdf_safe(f['detail'])}", styles["Citation"]),
        ])
    tbl = Table(data, colWidths=[20 * rl_mm, 28 * rl_mm, 120 * rl_mm], repeatRows=1)
    tbl.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, rl_colors.HexColor("#cbd5e1")),
        ("BACKGROUND", (0, 0), (-1, 0), rl_colors.HexColor("#0B1E3D")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.append(tbl)
    story.append(Spacer(1, 3 * rl_mm))
    story.append(Paragraph(
        "<i>Deterministic checks (no AI). These assist — they do not replace — "
        "review and sign-off by a qualified clinical pharmacologist.</i>",
        styles["Citation"]))


def _pdf_styles() -> dict[str, Any]:
    base = getSampleStyleSheet()
    return {
        "CoverTitle": ParagraphStyle(
            "CoverTitle",
            parent=base["Title"],
            fontName=_SERIF_BOLD,
            fontSize=18,
            spaceAfter=6,
            textColor=rl_colors.HexColor("#1E293B"),
            alignment=1,
        ),
        # Centred heading for front-matter pages (TOC, Abbreviations). Distinct
        # style name so these are NOT picked up as TOC entries (only H1/H2 are).
        "FrontHeading": ParagraphStyle(
            "FrontHeading",
            fontName=_SERIF_BOLD,
            fontSize=12,
            spaceAfter=12,
            textColor=rl_colors.HexColor("#1E293B"),
            alignment=1,
        ),
        "CoverSubtitle": ParagraphStyle(
            "CoverSubtitle",
            fontName=_SERIF_BOLD,
            fontSize=10.5,
            leading=15,
            spaceAfter=6,
            textColor=rl_colors.HexColor("#334155"),
            alignment=1,
        ),
        # Sub-heading style for unnumbered front matter (Synopsis). Distinct
        # style NAME from "Heading2"/"H2" so the TOC's afterFlowable hook
        # (which matches style.name == "H1"/"H2") does not capture it.
        "FrontSub": ParagraphStyle(
            "FrontSub",
            fontName=_SERIF_BOLD,
            fontSize=10.5,
            spaceBefore=8,
            spaceAfter=3,
            textColor=rl_colors.HexColor("#1E293B"),
        ),
        "Heading1": ParagraphStyle(
            "H1",
            fontName=_SERIF_BOLD,
            fontSize=12,
            spaceBefore=18,
            spaceAfter=8,
            textColor=rl_colors.HexColor("#1E293B"),
            keepWithNext=True,
        ),
        "Heading2": ParagraphStyle(
            "H2",
            fontName=_SERIF_BOLD,
            fontSize=10.5,
            spaceBefore=12,
            spaceAfter=4,
            textColor=rl_colors.HexColor("#334155"),
            keepWithNext=True,
        ),
        "Body": ParagraphStyle(
            "Body",
            fontName=_SERIF,
            fontSize=10.5,
            leading=15,
            spaceAfter=6,
            textColor=rl_colors.HexColor("#1E293B"),
        ),
        "Bullet": ParagraphStyle(
            "Bullet",
            fontName=_SERIF,
            fontSize=10.5,
            leading=15,
            leftIndent=14,
            spaceAfter=3,
            textColor=rl_colors.HexColor("#1E293B"),
        ),
        "Citation": ParagraphStyle(
            "Citation",
            fontName=_SERIF,
            fontSize=10,
            leading=14,
            leftIndent=18,
            firstLineIndent=-18,
            spaceAfter=5,
            textColor=rl_colors.HexColor("#1E293B"),
        ),
    }


def _add_section_content_pdf(
    story: list[Any],
    html: str,
    styles: dict[str, Any],
    section_idx: int,
    numbered: bool = True,
) -> None:
    """Convert TipTap HTML to reportlab Paragraph objects with sub-heading numbering.

    ``numbered=False`` (used for the front-matter Synopsis) renders sub-headings
    in the "FrontSub" style instead of "Heading2"/"H2" — the TOC's afterFlowable
    hook only captures "H1"/"H2"-named styles, so this keeps the Synopsis out of
    the TOC, matching the template (whose TOC starts at Section 1).
    """
    if not html.strip():
        return

    h2_counter = 0
    h3_counter = 0
    found_blocks = False

    for m in re.finditer(
        r"<(table|p|h2|h3|h4|ul|ol)([^>]*)>(.*?)</\1>",
        html,
        re.IGNORECASE | re.DOTALL,
    ):
        found_blocks = True
        tag = m.group(1).lower()
        inner = m.group(3)

        if tag == "table":
            _add_table_pdf(story, inner, styles)

        elif tag == "h2":
            h2_counter += 1
            h3_counter = 0
            text = re.sub(r"<[^>]+>", "", inner).strip()
            if numbered:
                story.append(
                    Paragraph(f"{section_idx}.{h2_counter}  {text}", styles["Heading2"])
                )
            else:
                story.append(Paragraph(text, styles["FrontSub"]))

        elif tag in ("h3", "h4"):
            text = re.sub(r"<[^>]+>", "", inner).strip()
            if not numbered:
                story.append(Paragraph(text, styles["FrontSub"]))
            elif h2_counter == 0:
                # promote an orphan sub-heading to N.M (template uses 1.1, not 1.0.1)
                h2_counter += 1
                h3_counter = 0
                story.append(
                    Paragraph(f"{section_idx}.{h2_counter}  {text}", styles["Heading2"])
                )
            else:
                h3_counter += 1
                story.append(
                    Paragraph(
                        f"{section_idx}.{h2_counter}.{h3_counter}  {text}",
                        styles["Heading2"],
                    )
                )

        elif tag == "ul":
            for li_m in re.finditer(
                r"<li[^>]*>(.*?)</li>", inner, re.IGNORECASE | re.DOTALL
            ):
                story.append(
                    _safe_paragraph(f"• {_rl_inline(li_m.group(1))}", styles["Bullet"])
                )

        elif tag == "ol":
            for i, li_m in enumerate(
                re.finditer(r"<li[^>]*>(.*?)</li>", inner, re.IGNORECASE | re.DOTALL),
                start=1,
            ):
                story.append(
                    _safe_paragraph(f"{i}. {_rl_inline(li_m.group(1))}", styles["Bullet"])
                )

        elif tag == "p":
            text = _rl_inline(inner)
            if text.strip():
                story.append(_safe_paragraph(text, styles["Body"]))

    if not found_blocks:
        _add_markdown_content_pdf(story, html, styles)


def _rl_inline(html_frag: str) -> str:
    """Convert inline TipTap HTML tags to reportlab XML markup."""
    out = html_frag
    out = re.sub(r"<strong>(.*?)</strong>", r"<b>\g<1></b>", out, flags=re.DOTALL | re.IGNORECASE)
    out = re.sub(r"<em>(.*?)</em>", r"<i>\g<1></i>", out, flags=re.DOTALL | re.IGNORECASE)
    out = re.sub(
        r"<mark[^>]*>(.*?)</mark>",
        r'<font backColor="#FEF08A">\g<1></font>',
        out,
        flags=re.DOTALL | re.IGNORECASE,
    )
    out = re.sub(r"<sub>(.*?)</sub>", r"<sub>\g<1></sub>", out, flags=re.DOTALL | re.IGNORECASE)
    out = re.sub(r"<sup>(.*?)</sup>", r"<super>\g<1></super>", out, flags=re.DOTALL | re.IGNORECASE)
    out = re.sub(r"<br\s*/?>", "<br/>", out, flags=re.IGNORECASE)
    # Strip remaining unknown tags but keep reportlab-safe ones
    out = re.sub(r"<(?!/?(?:b|i|br|sub|super|font)[^>]*>)[^>]+>", "", out)
    return (
        out.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&nbsp;", " ")
        .replace("&#39;", "'")
        .replace("&quot;", '"')
        .strip()
    )



def _add_table_pdf(story: list, table_inner_html: str, styles: dict) -> None:
    """Render HTML table content as a styled reportlab Table."""
    if not _RL_OK:
        return

    rows_html = re.findall(r"<tr[^>]*>(.*?)</tr>", table_inner_html, re.IGNORECASE | re.DOTALL)
    if not rows_html:
        return

    thead_content = ""
    thead_m = re.search(r"<thead[^>]*>(.*?)</thead>", table_inner_html, re.IGNORECASE | re.DOTALL)
    if thead_m:
        thead_content = thead_m.group(1)

    all_rows: list[list] = []
    header_flags: list[bool] = []
    for row_html in rows_html:
        th_cells = re.findall(r"<th[^>]*>(.*?)</th>", row_html, re.IGNORECASE | re.DOTALL)
        td_cells = re.findall(r"<td[^>]*>(.*?)</td>", row_html, re.IGNORECASE | re.DOTALL)
        is_hdr = bool(th_cells) or (thead_content and row_html in thead_content)
        cells = th_cells if th_cells else td_cells
        if cells:
            all_rows.append(cells)
            header_flags.append(is_hdr)

    if not all_rows:
        return

    max_cols = max(len(r) for r in all_rows)
    all_rows = [r + [""] * (max_cols - len(r)) for r in all_rows]

    from reportlab.lib.styles import ParagraphStyle as _PS
    cell_style = _PS(
        "TableCell",
        parent=styles["Body"],
        fontSize=9,
        leading=13,
        spaceAfter=0,
        spaceBefore=0,
    )
    hdr_style = _PS(
        "TableHdr",
        parent=cell_style,
        fontName=_SANS_BOLD,
        textColor=rl_colors.white,
    )

    table_data = []
    for row_idx, (row_cells, is_hdr) in enumerate(zip(all_rows, header_flags)):
        rendered = []
        for cell_html in row_cells:
            text = _rl_inline(cell_html)
            rendered.append(Paragraph(text, hdr_style if is_hdr else cell_style))
        table_data.append(rendered)

    col_w = (150 * rl_mm) / max_cols  # 150 mm = usable body width
    tbl = Table(table_data, colWidths=[col_w] * max_cols)

    tbl_style = [
        ("GRID", (0, 0), (-1, -1), 0.5, rl_colors.HexColor("#CBD5E1")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]
    # Header rows: navy background
    for r_idx, is_hdr in enumerate(header_flags):
        if is_hdr:
            tbl_style.append(("BACKGROUND", (0, r_idx), (-1, r_idx), rl_colors.HexColor("#1E3A5F")))
        elif r_idx % 2 == 0:
            tbl_style.append(("BACKGROUND", (0, r_idx), (-1, r_idx), rl_colors.HexColor("#F8FAFC")))

    tbl.setStyle(TableStyle(tbl_style))
    story.append(tbl)
    story.append(Spacer(1, 4 * rl_mm))


def _add_references_pdf(
    story: list[Any],
    citations: list[dict[str, str]],
    ai_text: str,
    styles: dict[str, Any],
    section_idx: int,
    study_type: str = "BE",
) -> None:
    """Add references section to the PDF story (guidances + PubMed bibliography)."""
    preamble = _strip_html(ai_text)
    if preamble.strip():
        for chunk in preamble.strip().split(chr(10) + chr(10)):
            if chunk.strip():
                story.append(Paragraph(chunk.strip(), styles["Body"]))
        story.append(Spacer(1, 4 * rl_mm))

    reg_refs = _REG_REFS_BIOSIMILAR if study_type == "biosimilar_pk" else _REG_REFS_BE
    story.append(
        Paragraph(f"{section_idx}.1  Regulatory and Guidance References", styles["Heading2"])
    )
    for _i, _ref in enumerate(reg_refs, start=1):
        story.append(Paragraph(f"[R{_i}] {_ref}", styles["Citation"]))

    if not citations:
        return

    story.append(
        Paragraph(f"{section_idx}.2  Literature Citations", styles["Heading2"])
    )
    for i, cite in enumerate(citations, start=1):
        title = (cite.get("title") or "Title unavailable") + "."
        pmid = cite["pmid"]
        pubmed_url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
        text_ = (
            f'[{i}] {title} '
            f'<link href="{pubmed_url}" color="#2563EB">PMID: {pmid}</link>'
        )
        story.append(Paragraph(text_, styles["Citation"]))


def _make_numbered_canvas(protocol: Protocol, drug_inn: str) -> type:
    """Return a NumberedCanvas class with protocol metadata captured in its closure.

    NumberedCanvas performs a two-pass build: showPage() buffers page states,
    and save() replays them with the final total-page count, enabling
    "Page N of M" in the footer without a second document pass.
    """

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
            code = _protocol_code(protocol, drug_inn)
            ver = f"Version No.: {protocol.version}.0"
            dated = f"Dated: {protocol.created_at.strftime('%d-%b-%Y')}"

            self.saveState()
            self.setFont("Helvetica", 8)
            self.setFillColor(rl_colors.HexColor("#64748B"))

            # Two-column running header on every page (including the cover)
            self.drawString(lm, H - 15 * rl_mm, "Clinical Trial Protocol")
            self.drawString(lm, H - 19 * rl_mm, code)
            self.drawRightString(W - rm, H - 15 * rl_mm, ver)
            self.drawRightString(W - rm, H - 19 * rl_mm, dated)
            self.setStrokeColor(rl_colors.HexColor("#E2E8F0"))
            self.line(lm, H - 21 * rl_mm, W - rm, H - 21 * rl_mm)

            # Footer: centered "Page N of M" above a rule
            self.line(lm, 16 * rl_mm, W - rm, 16 * rl_mm)
            self.setFont("Helvetica", 9)
            self.drawCentredString(
                W / 2, 11 * rl_mm, f"Page {self._pageNumber} of {total_pages}"
            )

            self.restoreState()

    return NumberedCanvas

# ---------------------------------------------------------------------------
# Markdown content renderers (plain-text path)
# ---------------------------------------------------------------------------


def _md_inline_runs(para, text: str) -> None:
    """Add runs converting **bold** and *italic* Markdown markers to Word runs."""
    parts = re.split(r"(\*\*[^*]+\*\*|\*[^*]+\*)", text)
    for part in parts:
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            run = para.add_run(part[2:-2])
            run.bold = True
        elif part.startswith("*") and part.endswith("*") and len(part) > 2:
            run = para.add_run(part[1:-1])
            run.italic = True
        elif part:
            para.add_run(part)


def _parse_md_table(lines: list) -> tuple:
    """Return (all_rows, header_row_indices_set) from Markdown table lines."""
    SEP_RE = re.compile(r"^\s*\|[\s\-:|]+\|\s*$")
    sep_positions: list = []
    data_lines: list = []
    for raw in lines:
        if SEP_RE.match(raw):
            sep_positions.append(len(data_lines))
        else:
            data_lines.append(raw)

    def parse_row(line: str) -> list:
        line = line.strip()
        if line.startswith("|"):
            line = line[1:]
        if line.endswith("|"):
            line = line[:-1]
        return [c.strip() for c in line.split("|")]

    rows = [parse_row(l) for l in data_lines]
    header_idx: set = set(range(sep_positions[0])) if sep_positions else {0}
    return rows, header_idx


def _add_markdown_table_docx(doc: Document, tbl_lines: list) -> None:
    """Render Markdown table lines as a styled Word table."""
    rows, header_idx = _parse_md_table(tbl_lines)
    if not rows:
        return
    max_cols = max(len(r) for r in rows)
    if max_cols == 0:
        return
    rows = [r + [""] * (max_cols - len(r)) for r in rows]

    tbl = doc.add_table(rows=len(rows), cols=max_cols)
    tbl.style = "Table Grid"
    avail_w = Inches(6.75)
    col_w = avail_w / max_cols

    for r_idx, row_cells in enumerate(rows):
        is_hdr = r_idx in header_idx
        row = tbl.rows[r_idx]
        for c_idx, cell_text in enumerate(row_cells):
            cell = row.cells[c_idx]
            cell.text = ""
            para = cell.paragraphs[0]
            para.paragraph_format.space_after = Pt(0)
            _md_inline_runs(para, cell_text)
            tc_pr = cell._tc.get_or_add_tcPr()
            mar = OxmlElement("w:tcMar")
            for side in ("top", "bottom", "left", "right"):
                m_el = OxmlElement(f"w:{side}")
                m_el.set(qn("w:w"), "80")
                m_el.set(qn("w:type"), "dxa")
                mar.append(m_el)
            tc_pr.append(mar)
            shd = OxmlElement("w:shd")
            shd.set(qn("w:val"), "clear")
            shd.set(qn("w:color"), "auto")
            if is_hdr:
                shd.set(qn("w:fill"), "1E3A5F")
                tc_pr.append(shd)
                for run in para.runs:
                    run.bold = True
                    run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
                    run.font.size = Pt(9)
            else:
                shd.set(qn("w:fill"), "F8FAFC" if r_idx % 2 == 0 else "FFFFFF")
                tc_pr.append(shd)
                for run in para.runs:
                    run.font.size = Pt(9)
        for cell in row.cells:
            cell.width = col_w
    doc.add_paragraph()


def _add_markdown_content_docx(doc: Document, text: str) -> None:
    """Render plain-text Markdown (paragraphs + pipe tables) into Word."""
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        if re.match(r"\s*\|", line):
            tbl_lines: list = []
            while i < len(lines) and re.match(r"\s*\|", lines[i].rstrip()):
                tbl_lines.append(lines[i].rstrip())
                i += 1
            _add_markdown_table_docx(doc, tbl_lines)
        else:
            if line.strip():
                para = doc.add_paragraph()
                _md_inline_runs(para, line.strip())
            i += 1


def _md_to_rl(text: str) -> str:
    """Convert Markdown **bold**/*italic* to reportlab XML tags."""
    text = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"\*([^*]+)\*",       r"<i>\1</i>", text)
    return text


def _add_markdown_table_pdf(story: list, tbl_lines: list, styles: dict) -> None:
    """Render Markdown table lines as a styled reportlab Table."""
    if not _RL_OK:
        return
    rows, header_idx = _parse_md_table(tbl_lines)
    if not rows:
        return
    max_cols = max(len(r) for r in rows)
    if max_cols == 0:
        return
    rows = [r + [""] * (max_cols - len(r)) for r in rows]

    from reportlab.lib.styles import ParagraphStyle as _PS
    cell_style = _PS("MdCell", parent=styles["Body"], fontSize=9, leading=13,
                     spaceAfter=0, spaceBefore=0)
    hdr_style  = _PS("MdHdr", parent=cell_style, fontName=_SANS_BOLD,
                     textColor=rl_colors.white)

    table_data = []
    for r_idx, row_cells in enumerate(rows):
        sty = hdr_style if r_idx in header_idx else cell_style
        table_data.append([Paragraph(_md_to_rl(c), sty) for c in row_cells])

    col_w = (150 * rl_mm) / max_cols
    tbl = Table(table_data, colWidths=[col_w] * max_cols)
    tbl_cmds = [
        ("GRID",          (0, 0), (-1, -1), 0.5, rl_colors.HexColor("#CBD5E1")),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",    (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING",   (0, 0), (-1, -1), 6),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 6),
    ]
    for r_idx in range(len(rows)):
        if r_idx in header_idx:
            tbl_cmds.append(("BACKGROUND", (0, r_idx), (-1, r_idx),
                             rl_colors.HexColor("#1E3A5F")))
        elif r_idx % 2 == 0:
            tbl_cmds.append(("BACKGROUND", (0, r_idx), (-1, r_idx),
                             rl_colors.HexColor("#F8FAFC")))
    tbl.setStyle(TableStyle(tbl_cmds))
    story.append(tbl)
    story.append(Spacer(1, 4 * rl_mm))


def _add_markdown_content_pdf(story: list, text: str, styles: dict) -> None:
    """Render plain-text Markdown (paragraphs + pipe tables) into PDF story."""
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        if re.match(r"\s*\|", line):
            tbl_lines: list = []
            while i < len(lines) and re.match(r"\s*\|", lines[i].rstrip()):
                tbl_lines.append(lines[i].rstrip())
                i += 1
            _add_markdown_table_pdf(story, tbl_lines, styles)
        else:
            if line.strip():
                story.append(Paragraph(_md_to_rl(line.strip()), styles["Body"]))
            i += 1


# ---------------------------------------------------------------------------
# Professional formatting helpers (template-based)
# ---------------------------------------------------------------------------


def _apply_doc_styles(doc: Document) -> None:
    """Set Times New Roman 12pt as default and style headings to match template."""
    try:
        normal = doc.styles["Normal"]
        normal.font.name = "Times New Roman"
        normal.font.size = Pt(10.5)
    except Exception:
        pass
    _h_style(doc, "Heading 1", size_pt=12, bold=True,  space_before=18, space_after=6)
    _h_style(doc, "Heading 2", size_pt=11, bold=True,  space_before=12, space_after=3)
    _h_style(doc, "Heading 3", size_pt=10.5, bold=True,  italic=True, space_before=9, space_after=3)


def _h_style(doc, name, size_pt, bold=False, italic=False,
             space_before=0, space_after=0) -> None:
    try:
        s = doc.styles[name]
        s.font.name = "Times New Roman"
        s.font.size = Pt(size_pt)
        s.font.bold = bold
        s.font.italic = italic
        s.font.color.rgb = RGBColor(0, 0, 0)
        s.paragraph_format.space_before = Pt(space_before)
        s.paragraph_format.space_after = Pt(space_after)
    except Exception:
        pass


# CT_Settings (w:settings) requires its children in this fixed order per
# ECMA-376/ISO-29500 — echoed here since python-docx's own copy
# (docx.oxml.settings.CT_Settings._tag_seq) is deleted after class definition
# and isn't importable. Only used to place w:updateFields correctly among
# whatever settings elements python-docx's template already wrote; unconditionally
# appending it at the end (the previous behaviour) put it after w14:* extension
# elements, which is schema-invalid and can trigger Word's "repair this
# document?" prompt — the exact class of bug _add_heading_bookmark also had to
# guard against for w:pPr (see there).
_SETTINGS_TAG_ORDER = (
    "w:writeProtection", "w:view", "w:zoom", "w:removePersonalInformation",
    "w:removeDateAndTime", "w:doNotDisplayPageBoundaries", "w:displayBackgroundShape",
    "w:printPostScriptOverText", "w:printFractionalCharacterWidth", "w:printFormsData",
    "w:embedTrueTypeFonts", "w:embedSystemFonts", "w:saveSubsetFonts", "w:saveFormsData",
    "w:mirrorMargins", "w:alignBordersAndEdges", "w:bordersDoNotSurroundHeader",
    "w:bordersDoNotSurroundFooter", "w:gutterAtTop", "w:hideSpellingErrors",
    "w:hideGrammaticalErrors", "w:activeWritingStyle", "w:proofState", "w:formsDesign",
    "w:attachedTemplate", "w:linkStyles", "w:stylePaneFormatFilter", "w:stylePaneSortMethod",
    "w:documentType", "w:mailMerge", "w:revisionView", "w:trackRevisions",
    "w:doNotTrackMoves", "w:doNotTrackFormatting", "w:documentProtection",
    "w:autoFormatOverride", "w:styleLockTheme", "w:styleLockQFSet", "w:defaultTabStop",
    "w:autoHyphenation", "w:consecutiveHyphenLimit", "w:hyphenationZone",
    "w:doNotHyphenateCaps", "w:showEnvelope", "w:summaryLength", "w:clickAndTypeStyle",
    "w:defaultTableStyle", "w:evenAndOddHeaders", "w:bookFoldRevPrinting",
    "w:bookFoldPrinting", "w:bookFoldPrintingSheets", "w:drawingGridHorizontalSpacing",
    "w:drawingGridVerticalSpacing", "w:displayHorizontalDrawingGridEvery",
    "w:displayVerticalDrawingGridEvery", "w:doNotUseMarginsForDrawingGridOrigin",
    "w:drawingGridHorizontalOrigin", "w:drawingGridVerticalOrigin", "w:doNotShadeFormData",
    "w:noPunctuationKerning", "w:characterSpacingControl", "w:printTwoOnOne",
    "w:strictFirstAndLastChars", "w:noLineBreaksAfter", "w:noLineBreaksBefore",
    "w:savePreviewPicture", "w:doNotValidateAgainstSchema", "w:saveInvalidXml",
    "w:ignoreMixedContent", "w:alwaysShowPlaceholderText", "w:doNotDemarcateInvalidXml",
    "w:saveXmlDataOnly", "w:useXSLTWhenSaving", "w:saveThroughXslt", "w:showXMLTags",
    "w:alwaysMergeEmptyNamespace", "w:updateFields",
    # Order among everything after updateFields doesn't matter for placing it —
    # they only need to be recognized as "later" than it.
    "w:hdrShapeDefaults", "w:footnotePr", "w:endnotePr", "w:compat", "w:docVars",
    "w:rsids", "m:mathPr", "w:attachedSchema", "w:themeFontLang", "w:clrSchemeMapping",
    "w:doNotIncludeSubdocsInStats", "w:doNotAutoCompressPictures", "w:forceUpgrade",
    "w:captions", "w:readModeInkLockDown", "w:smartTagType", "sl:schemaLibrary",
    "w:shapeDefaults", "w:doNotEmbedSmartTags", "w:decimalSymbol", "w:listSeparator",
)


def _enable_update_fields_on_open(doc: Document) -> None:
    """Tell Word to refresh all fields (incl. the TOC and Page N of M) on open.

    Without this the TOC shows its placeholder until the user presses F9; with
    it, Word recalculates fields when the document is opened so the TOC and page
    numbers are populated automatically. (Word may show a one-time 'update
    fields?' prompt depending on the user's settings — that is expected.)
    """
    try:
        settings = doc.settings.element
        if settings.find(qn("w:updateFields")) is not None:
            return
        upd = OxmlElement("w:updateFields")
        upd.set(qn("w:val"), "true")

        clark_order = [qn(t) for t in _SETTINGS_TAG_ORDER]
        target_pos = clark_order.index(qn("w:updateFields"))
        insert_at = len(settings)
        for i, child in enumerate(settings):
            if child.tag in clark_order and clark_order.index(child.tag) > target_pos:
                insert_at = i
                break
        settings.insert(insert_at, upd)
    except Exception:
        pass


def _add_toc_docx(doc: Document) -> None:
    """Insert a Word TOC field (auto-updates when opened in Word / Ctrl+A, F9)."""
    p_head = doc.add_paragraph()
    p_head.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p_head.paragraph_format.space_after = Pt(18)
    rh = p_head.add_run("TABLE OF CONTENTS")
    rh.bold = True
    rh.font.name = "Times New Roman"
    rh.font.size = Pt(13)

    p_toc = doc.add_paragraph()
    p_toc._p.clear()

    def _fr(char_type=None, instr_text=None, placeholder=None):
        r = OxmlElement("w:r")
        if char_type:
            fc = OxmlElement("w:fldChar")
            fc.set(qn("w:fldCharType"), char_type)
            if char_type == "begin":
                fc.set(qn("w:dirty"), "true")
            r.append(fc)
        elif instr_text is not None:
            it = OxmlElement("w:instrText")
            it.set(qn("xml:space"), "preserve")
            it.text = instr_text
            r.append(it)
        elif placeholder is not None:
            t = OxmlElement("w:t")
            t.text = placeholder
            r.append(t)
        return r

    p_toc._p.append(_fr(char_type="begin"))
    p_toc._p.append(_fr(instr_text=' TOC \\o "1-3" \\h \\z \\u '))
    p_toc._p.append(_fr(char_type="separate"))
    p_toc._p.append(_fr(placeholder="[Right-click and select Update Field to refresh the Table of Contents]"))
    p_toc._p.append(_fr(char_type="end"))

    doc.add_paragraph()


def _section_bookmark_name(section_key: str) -> str:
    """Word bookmark names must start with a letter and contain only
    letters/digits/underscores — section_key (e.g. 'study_design') already
    satisfies that, so this just namespaces it to avoid colliding with any
    other bookmark Word itself might add."""
    return f"sec_{section_key}"


def _add_heading_bookmark(paragraph, section_key: str, bookmark_id: int) -> None:
    """Wrap a heading paragraph in a named bookmark so the amendment appendix
    can reference its resolved page number via a PAGEREF field (see
    _add_pageref_suffix) — bookmark_id only needs to be unique within the
    document; the section's position in SECTION_ORDER already guarantees that.
    """
    start = OxmlElement("w:bookmarkStart")
    start.set(qn("w:id"), str(bookmark_id))
    start.set(qn("w:name"), _section_bookmark_name(section_key))
    end = OxmlElement("w:bookmarkEnd")
    end.set(qn("w:id"), str(bookmark_id))
    # CT_P requires w:pPr (if present) to be the paragraph's FIRST child —
    # inserting the bookmark before it produces an invalid document that Word
    # offers to "repair" on open. doc.add_heading() always sets a pPr (the
    # Heading1/2/3 pStyle), so insert right after it, not at raw index 0.
    p = paragraph._p
    ppr = p.find(qn("w:pPr"))
    insert_at = 1 if ppr is not None else 0
    p.insert(insert_at, start)
    p.append(end)


def _add_pageref_suffix(paragraph, section_key: str) -> None:
    """Append a live ' (p. N)' Word field to a paragraph, where N auto-resolves
    to the bookmarked heading's current page number on open (PAGEREF \\h) —
    stays correct even after the user edits the document, unlike a page number
    computed once at export time. Mirrors _add_toc_docx's field-code pattern.
    """
    def _fld(char_type=None, instr_text=None, placeholder=None):
        r = OxmlElement("w:r")
        rpr = OxmlElement("w:rPr")
        rfonts = OxmlElement("w:rFonts")
        rfonts.set(qn("w:ascii"), "Times New Roman")
        rfonts.set(qn("w:hAnsi"), "Times New Roman")
        rpr.append(rfonts)
        sz = OxmlElement("w:sz")
        sz.set(qn("w:val"), "16")  # 8pt, matching the amendment table's cell font
        rpr.append(sz)
        r.append(rpr)
        if char_type:
            fc = OxmlElement("w:fldChar")
            fc.set(qn("w:fldCharType"), char_type)
            if char_type == "begin":
                fc.set(qn("w:dirty"), "true")
            r.append(fc)
        elif instr_text is not None:
            it = OxmlElement("w:instrText")
            it.set(qn("xml:space"), "preserve")
            it.text = instr_text
            r.append(it)
        elif placeholder is not None:
            t = OxmlElement("w:t")
            t.text = placeholder
            r.append(t)
        return r

    prefix = paragraph.add_run(" (p. ")
    prefix.font.name = "Times New Roman"
    prefix.font.size = Pt(8)

    bookmark = _section_bookmark_name(section_key)
    p = paragraph._p
    p.append(_fld(char_type="begin"))
    p.append(_fld(instr_text=f" PAGEREF {bookmark} \\h "))
    p.append(_fld(char_type="separate"))
    p.append(_fld(placeholder="…"))
    p.append(_fld(char_type="end"))

    suffix = paragraph.add_run(")")
    suffix.font.name = "Times New Roman"
    suffix.font.size = Pt(8)


def _add_schedule_of_activities_docx(
    doc: Document, protocol: Protocol, idx: int, heading: str,
) -> None:
    """Render a Schedule of Activities (SoA) matrix — required by ICH M11/E6(R2).

    Numbered as its own top-level section (idx, e.g. "8 Overview of Study
    Procedures and Assessments"), matching the reference template, with the
    table itself under an "{idx}.1 Schedule of Study Assessments and
    Procedures" sub-heading. Columns are study visits; rows are procedures; an
    'X' marks where each procedure is performed. Layout adapts to crossover vs
    parallel/biosimilar designs (biosimilars gain immunogenicity and
    injection-site rows + extended follow-up visits)."""
    study_type = protocol.study_type.value
    rec = (protocol.population_config or {}).get("_sample_size") or {}
    design_key = rec.get("design_key") or (
        "parallel" if study_type == "biosimilar_pk" else "crossover"
    )
    is_biosimilar = study_type == "biosimilar_pk"

    doc.add_heading(f"{idx} {heading}", level=1)
    doc.add_heading(f"{idx}.1 Schedule of Study Assessments and Procedures", level=2)
    _ss_para(
        doc,
        "The schedule below summarises study procedures by visit. 'X' indicates the "
        "procedure is performed; nominal times are shown — refer to the Study "
        "Assessments section for the complete PK (and, where applicable, "
        "immunogenicity) sampling schedule.",
        size=10.5,
    )

    if design_key in ("parallel",) or is_biosimilar:
        cols = ["Procedure", "Screening\n(Day -28 to -2)", "Check-in\n(Day -1)",
                "Dosing\n(Day 1)", "PK Sampling\n(Day 1-4)",
                "Follow-up\n(Wk 4/8/12)", "End of Study\n(Wk 24)"]
        rows = [
            ("Informed consent", {1}),
            ("Demographics & medical history", {1}),
            ("Physical examination", {1, 6}),
            ("Vital signs", {1, 2, 3, 4, 5, 6}),
            ("12-lead ECG", {1, 3, 6}),
            ("Clinical laboratory (haematology, chemistry, UA)", {1, 2, 5, 6}),
            ("Serology, drug & alcohol screen", {1, 2}),
            ("Pregnancy test (WOCBP)", {1, 2, 6}),
            ("Eligibility review", {1, 2}),
            ("Randomisation", {2}),
            ("Study drug administration", {3}),
            ("PK blood sampling", {4}),
        ]
        if is_biosimilar:
            rows += [
                ("Immunogenicity (ADA/NAb) sampling", {3, 5, 6}),
                ("Injection/infusion-site assessment", {3, 4}),
            ]
        rows += [
            ("Adverse event assessment", {2, 3, 4, 5, 6}),
            ("Concomitant medication review", {1, 2, 3, 4, 5, 6}),
        ]
    else:
        cols = ["Procedure", "Screening\n(Day -28 to -2)", "Check-in\n(Day -1)",
                "Dosing\n(Day 1)", "PK Sampling\n(Day 1-3)",
                "Washout", "End of Study\n(Follow-up)"]
        rows = [
            ("Informed consent", {1}),
            ("Demographics & medical history", {1}),
            ("Physical examination", {1, 6}),
            ("Vital signs", {1, 2, 3, 4, 6}),
            ("12-lead ECG", {1, 3, 6}),
            ("Clinical laboratory (haematology, chemistry, UA)", {1, 2, 6}),
            ("Serology, drug & alcohol screen", {1, 2}),
            ("Pregnancy test (WOCBP)", {1, 2, 6}),
            ("Eligibility review", {1, 2}),
            ("Randomisation", {2}),
            ("Study drug administration (each period)", {3}),
            ("PK blood sampling (each period)", {4}),
            ("Adverse event assessment", {2, 3, 4, 5, 6}),
            ("Concomitant medication review", {1, 2, 3, 4, 5, 6}),
        ]

    n_cols = len(cols)
    tbl = doc.add_table(rows=len(rows) + 1, cols=n_cols)
    tbl.style = "Table Grid"
    # Wider first column for procedure names; remaining split evenly.
    proc_w = Inches(2.3)
    other_w = (Inches(6.75) - proc_w) / (n_cols - 1)

    # Header row
    hdr = tbl.rows[0]
    for c_idx, label in enumerate(cols):
        cell = hdr.cells[c_idx]
        cell.text = ""
        para = cell.paragraphs[0]
        para.paragraph_format.space_after = Pt(0)
        if c_idx > 0:
            para.alignment = WD_ALIGN_PARAGRAPH.CENTER
        rn = para.add_run(label)
        rn.bold = True
        rn.font.name = "Times New Roman"
        rn.font.size = Pt(8)
        rn.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
        shd = OxmlElement("w:shd")
        shd.set(qn("w:val"), "clear")
        shd.set(qn("w:color"), "auto")
        shd.set(qn("w:fill"), "1E3A5F")
        cell._tc.get_or_add_tcPr().append(shd)

    # Body rows
    for r_idx, (proc, marks) in enumerate(rows, start=1):
        row = tbl.rows[r_idx]
        fill = "F0F4F8" if r_idx % 2 == 0 else "FFFFFF"
        for c_idx in range(n_cols):
            cell = row.cells[c_idx]
            cell.text = ""
            para = cell.paragraphs[0]
            para.paragraph_format.space_after = Pt(0)
            if c_idx == 0:
                rn = para.add_run(proc)
            else:
                para.alignment = WD_ALIGN_PARAGRAPH.CENTER
                rn = para.add_run("X" if c_idx in marks else "")
            rn.font.name = "Times New Roman"
            rn.font.size = Pt(8)
            shd = OxmlElement("w:shd")
            shd.set(qn("w:val"), "clear")
            shd.set(qn("w:color"), "auto")
            shd.set(qn("w:fill"), fill)
            cell._tc.get_or_add_tcPr().append(shd)
        row.cells[0].width = proc_w
        for c_idx in range(1, n_cols):
            row.cells[c_idx].width = other_w

    doc.add_paragraph()
    doc.add_page_break()


def _abbreviations_for(study_type: str = "BE") -> list[tuple[str, str]]:
    """Return the sorted (abbreviation, definition) list for the study type.

    Shared by the DOCX and PDF exporters so both stay identical. A biosimilar
    protocol carries immunogenicity terms (ADA, NAb, ELISA, ECL, LBA, …); a
    small-molecule BE protocol is not padded with biologic-only acronyms.
    """
    base = [
        ("AE",      "Adverse Event"),
        ("ANCOVA",  "Analysis of Covariance"),
        ("ANOVA",   "Analysis of Variance"),
        ("AUC",     "Area Under the Plasma Concentration-Time Curve"),
        ("AUC0-t",  "AUC from time zero to last quantifiable concentration"),
        ("AUC0-∞",  "AUC from time zero extrapolated to infinity"),
        ("%AUCextrap", "Percentage of AUC0-∞ obtained by extrapolation"),
        ("BE",      "Bioequivalence"),
        ("BMI",     "Body Mass Index"),
        ("CFR",     "Code of Federal Regulations"),
        ("CI",      "Confidence Interval"),
        ("CL",      "Total Plasma Clearance"),
        ("Cmax",    "Maximum Plasma Concentration"),
        ("CRF",     "Case Report Form"),
        ("CV",      "Coefficient of Variation"),
        ("CVw",     "Within-Subject (Intra-Subject) Coefficient of Variation"),
        ("ECG",     "Electrocardiogram"),
        ("FDA",     "Food and Drug Administration"),
        ("GCP",     "Good Clinical Practice"),
        ("GMR",     "Geometric Mean Ratio"),
        ("ICH",     "International Council for Harmonisation"),
        ("IRB/EC",  "Institutional Review Board / Ethics Committee"),
        ("ITT",     "Intent-to-Treat"),
        ("λz",      "Terminal Elimination Rate Constant"),
        ("LLOQ",    "Lower Limit of Quantification"),
        ("NCA",     "Non-Compartmental Analysis"),
        ("PK",      "Pharmacokinetics"),
        ("PP",      "Per Protocol"),
        ("SAE",     "Serious Adverse Event"),
        ("SAP",     "Statistical Analysis Plan"),
        ("SD",      "Standard Deviation"),
        ("SoA",     "Schedule of Activities"),
        ("t½",      "Terminal Elimination Half-Life"),
        ("Tmax",    "Time to Maximum Plasma Concentration"),
        ("TOST",    "Two One-Sided Tests"),
        ("Vz/F",    "Apparent Volume of Distribution (terminal phase)"),
    ]

    if study_type == "biosimilar_pk":
        specific = [
            ("ADA",     "Anti-Drug Antibody"),
            ("ECL",     "Electrochemiluminescence"),
            ("ELISA",   "Enzyme-Linked Immunosorbent Assay"),
            ("IAR",     "Infusion-Associated Reaction"),
            ("ISR",     "Injection-Site Reaction"),
            ("LBA",     "Ligand-Binding Assay"),
            ("MRD",     "Minimum Required Dilution"),
            ("NAb",     "Neutralizing Antibody"),
            ("PHSA",    "Public Health Service Act (351(k) pathway)"),
        ]
    else:
        specific = [
            ("ANDA",    "Abbreviated New Drug Application"),
            ("IND",     "Investigational New Drug Application"),
            ("NDA",     "New Drug Application"),
            ("NTI",     "Narrow Therapeutic Index"),
            ("RLD",     "Reference Listed Drug"),
            ("RSABE",   "Reference-Scaled Average Bioequivalence"),
        ]

    return sorted(base + specific, key=lambda x: x[0].lower())


def _add_abbreviations_docx(doc: Document, study_type: str = "BE") -> None:
    """Insert the standardized clinical-trial abbreviations table (DOCX)."""
    p_head = doc.add_paragraph()
    p_head.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p_head.paragraph_format.space_after = Pt(12)
    rh = p_head.add_run("LIST OF ABBREVIATIONS")
    rh.bold = True
    rh.font.name = "Times New Roman"
    rh.font.size = Pt(13)

    abbrevs = _abbreviations_for(study_type)

    tbl = doc.add_table(rows=len(abbrevs) + 1, cols=2)
    tbl.style = "Table Grid"

    hdr_row = tbl.rows[0]
    for cell_text, cell in zip(("Abbreviation", "Definition"), hdr_row.cells):
        cell.text = ""
        rn = cell.paragraphs[0].add_run(cell_text)
        rn.bold = True
        rn.font.name = "Times New Roman"
        rn.font.size = Pt(10.5)
        shd = OxmlElement("w:shd")
        shd.set(qn("w:val"), "clear")
        shd.set(qn("w:color"), "auto")
        shd.set(qn("w:fill"), "1E3A5F")
        cell._tc.get_or_add_tcPr().append(shd)
        for run in cell.paragraphs[0].runs:
            run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)

    for row_idx, (abbr, full) in enumerate(abbrevs, start=1):
        row = tbl.rows[row_idx]
        fill = "F0F4F8" if row_idx % 2 == 0 else "FFFFFF"
        for cell_text, cell in zip((abbr, full), row.cells):
            cell.text = ""
            rn = cell.paragraphs[0].add_run(cell_text)
            rn.font.name = "Times New Roman"
            rn.font.size = Pt(10.5)
            shd = OxmlElement("w:shd")
            shd.set(qn("w:val"), "clear")
            shd.set(qn("w:color"), "auto")
            shd.set(qn("w:fill"), fill)
            cell._tc.get_or_add_tcPr().append(shd)

    for row in tbl.rows:
        row.cells[0].width = Inches(1.5)
        row.cells[1].width = Inches(5.25)

    doc.add_paragraph()


# ---------------------------------------------------------------------------
# Sample size determination
# ---------------------------------------------------------------------------


def _compute_sample_size(cv_pct: float, power_pct: float = 80.0,
                         be_limit_pct: float = 125.0, design: str = "crossover") -> int:
    """Minimum sample size for a BE study (normal approximation)."""
    z_beta_map = {80: 0.8416, 85: 1.0364, 90: 1.2816, 95: 1.6449}
    z_alpha = 1.6449
    z_beta = z_beta_map.get(int(round(power_pct)), 0.8416)
    cv = cv_pct / 100.0
    sigma_w = math.sqrt(math.log(1.0 + cv ** 2))
    delta = math.log(be_limit_pct / 100.0)
    if design == "crossover":
        n = 2.0 * ((z_alpha + z_beta) * sigma_w / delta) ** 2
        n_int = math.ceil(n)
        return n_int + (n_int % 2)  # round up to nearest even
    else:
        sigma_total = sigma_w * math.sqrt(2.0)
        n_arm = 2.0 * ((z_alpha + z_beta) * sigma_total / delta) ** 2
        return math.ceil(n_arm)


# Every key the sample-size PDF/DOCX section reads via subscript (rec['x']),
# with a safe, disclosed-conservative default. A hand-edited or partially-
# populated _sample_size dict (e.g. via direct API/DB access, or a future
# schema change) previously raised a bare KeyError and crashed the whole
# export; normalizing once here means a missing field degrades to a
# reasonable placeholder instead. Matches services.biostatistics' own
# DEFAULT_* constants where applicable.
_SAMPLE_SIZE_DEFAULTS: dict[str, Any] = {
    "design": "Not specified", "approach": "Not specified",
    "lower_limit": 0.80, "upper_limit": 1.25,
    "endpoint": "Cmax and AUC0-∞ (co-primary); AUC0-t secondary",
    "regulatory_basis": "FDA Guidance: Statistical Approaches to Establishing "
                        "Bioequivalence (2001); 21 CFR 320.23",
    "cv_pct": 25.0, "cv_source": "not specified",
    "gmr": 0.95, "power": 0.90, "alpha": 0.05, "dropout_rate": 0.20,
    "n_evaluable": 0, "n_enrolled": 0, "n_per_group": None,
    "achieved_power": 0.90, "needs_simulation": False, "sensitivity": [],
    "design_key": "crossover", "t_half_hours": None, "provenance": {},
}


def _normalize_sample_size(rec: dict[str, Any] | None) -> dict[str, Any] | None:
    """Backfill any missing _sample_size keys with safe defaults so the export
    can never KeyError on a sparse/partial dict. Returns None unchanged (the
    caller's legacy-fallback path handles that case)."""
    if not rec:
        return rec
    merged = dict(_SAMPLE_SIZE_DEFAULTS)
    merged.update(rec)
    # Cross-fill n_evaluable/n_enrolled from each other if only one was given.
    if not merged.get("n_evaluable") and merged.get("n_enrolled"):
        merged["n_evaluable"] = merged["n_enrolled"]
    if not merged.get("n_enrolled") and merged.get("n_evaluable"):
        merged["n_enrolled"] = merged["n_evaluable"]
    return merged


def _ss_para(doc: Document, text: str, *, italic: bool = False, size: float = 10.5,
             indent: float | None = None) -> None:
    p = doc.add_paragraph()
    if indent is not None:
        p.paragraph_format.left_indent = Inches(indent)
    r = p.add_run(text)
    r.font.name = "Times New Roman"
    r.font.size = Pt(size)
    r.italic = italic


def _design_schematic_stages(rec: dict) -> list[dict[str, Any]]:
    """Shared stage list for the study-design schematic (PDF + DOCX).

    Each stage is either shared ({"label": str, "arms": None}, drawn as one
    box spanning the full height of the figure) or an arm split
    ({"label": None, "arms": [top, bottom]}, drawn as two boxes in separate,
    visually parallel lanes). Only a design with genuinely concurrent
    treatment arms (parallel) branches: a crossover subject moves through the
    same periods sequentially, not alongside another arm, so it stays a
    single row. The arms stay in their own lane through every stage they are
    actually in (treatment AND its assessments), converging back to one lane
    only at End of Study - a momentary split-then-immediate-merge would
    understate that the two arms run concurrently for the bulk of the study.
    """
    design_key = rec.get("design_key") or "crossover"
    n_enr = rec.get("n_enrolled")
    n_arm = rec.get("n_per_group") or (n_enr // 2 if isinstance(n_enr, int) else None)
    n_txt = f"N={n_enr}" if n_enr else "N"
    arm_txt = f"n={n_arm}" if n_arm else "n"
    if design_key == "parallel":
        assessment = "PK Sampling & Safety / Immunogenicity"
        return [
            {"label": "Screening (Day -28 to -1)", "arms": None},
            {"label": f"Randomize 1:1 ({n_txt})", "arms": None},
            {"label": None, "arms": [f"Test Product ({arm_txt})", f"Reference ({arm_txt})"]},
            {"label": None, "arms": [assessment, assessment]},
            {"label": "End of Study (EOS)", "arms": None},
        ]
    return [
        {"label": "Screening", "arms": None},
        {"label": f"Randomize sequence ({n_txt})", "arms": None},
        {"label": "Period 1 (Test or Reference)", "arms": None},
        {"label": "Washout (>=5-7 x t1/2)", "arms": None},
        {"label": "Period 2 (crossover)", "arms": None},
        {"label": "End of Study (EOS)", "arms": None},
    ]


def _add_study_design_schematic_docx(doc: Document, protocol: Protocol) -> None:
    """Deterministic study-design schematic for DOCX — a shaded box-and-arrow
    flow (parity with the PDF Figure 1). For a parallel design the two
    treatment arms run as two visually separate, parallel rows from the
    randomisation split to the point they converge again at End of Study,
    rather than naming both arms inside a single box."""
    rec = (protocol.population_config or {}).get("_sample_size")
    if not rec:
        return
    stages = _design_schematic_stages(rec)
    branched = any(s["arms"] for s in stages)

    cap = doc.add_paragraph()
    cr = cap.add_run(f"Figure 1. Study design schematic — {rec.get('design', '')}.")
    cr.italic = True
    cr.font.name = "Times New Roman"
    cr.font.size = Pt(9)

    def _shade(cell, fill: str) -> None:
        shd = OxmlElement("w:shd")
        shd.set(qn("w:val"), "clear")
        shd.set(qn("w:color"), "auto")
        shd.set(qn("w:fill"), fill)
        cell._tc.get_or_add_tcPr().append(shd)

    def _fill_cell(cell, text: str, *, arrow: bool = False, fill: str | None = None) -> None:
        cell.text = ""
        cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
        para = cell.paragraphs[0]
        para.alignment = WD_ALIGN_PARAGRAPH.CENTER
        para.paragraph_format.space_after = Pt(0)
        run = para.add_run(text)
        run.font.name = "Times New Roman"
        if arrow:
            run.font.size = Pt(11)
        else:
            run.bold = True
            run.font.size = Pt(7.5)
            run.font.color.rgb = RGBColor(0x1E, 0x29, 0x3B)
        if fill:
            _shade(cell, fill)

    # Stage columns interleaved with arrow columns; two rows only when the
    # design actually branches, so a crossover keeps the original single row.
    n_cols = len(stages) * 2 - 1
    n_rows = 2 if branched else 1
    tbl = doc.add_table(rows=n_rows, cols=n_cols)
    tbl.autofit = True

    for i, stage in enumerate(stages):
        col = i * 2
        if stage["arms"]:
            _fill_cell(tbl.cell(0, col), stage["arms"][0], fill="FDF3E3")
            _fill_cell(tbl.cell(1, col), stage["arms"][1], fill="FDF3E3")
        else:
            cell = tbl.cell(0, col)
            if branched:
                cell = cell.merge(tbl.cell(1, col))
            _fill_cell(cell, stage["label"], fill="EAF2FB")

        if col + 1 >= n_cols:
            continue
        acol = col + 1
        # A branch or convergence point: arrows appear in both rows rather
        # than being merged, so the split/join is visible at a glance.
        if branched and (stage["arms"] or stages[i + 1]["arms"]):
            _fill_cell(tbl.cell(0, acol), "→", arrow=True)
            _fill_cell(tbl.cell(1, acol), "→", arrow=True)
        else:
            acell = tbl.cell(0, acol)
            if branched:
                acell = acell.merge(tbl.cell(1, acol))
            _fill_cell(acell, "→", arrow=True)
    doc.add_paragraph()


def _add_sample_size_section_docx(doc: Document, protocol: Protocol,
                                   section_idx: int) -> None:
    """Sample size determination from the locked design recommendation.

    Reads the recommendation computed once at protocol creation
    (population_config['_sample_size']) so the N here is identical to the N in
    the synopsis, study design and SAP. Falls back to a generic table for
    legacy protocols created before the engine existed.
    """
    rec = _normalize_sample_size((protocol.population_config or {}).get("_sample_size"))

    doc.add_heading(f"{section_idx}.S Sample Size Determination", level=2)

    if not rec:
        _add_sample_size_section_legacy_docx(doc, section_idx)
        return

    lower = rec["lower_limit"] * 100
    upper = rec["upper_limit"] * 100
    per_arm = (
        f", {rec['n_per_group']} per arm" if rec.get("n_per_group") else ""
    )

    _ss_para(
        doc,
        f"The sample size was determined for the {rec['design'].lower()} using the "
        f"two one-sided tests (TOST) method, calculated in R (PowerTOST package). "
        f"The statistical approach is {rec['approach']}, "
        f"with the 90% confidence interval of the Test/Reference geometric mean ratio "
        f"for the co-primary endpoints ({_normalize_pk_flat(rec['endpoint'])}) required to fall within "
        f"{lower:.2f}-{upper:.2f}%. Regulatory basis: {rec['regulatory_basis']}.",
    )

    if rec.get("cv_source") == "assumed":
        _ss_para(
            doc,
            f"NOTE: No within-subject CV was available from the literature; a moderate "
            f"value of {rec['cv_pct']:.0f}% has been ASSUMED for planning and must be "
            f"replaced with drug-specific intra-subject variability before the protocol "
            f"is finalised.",
            italic=True, size=10.5,
        )

    # --- Assumptions table ------------------------------------------------
    doc.add_heading(f"{section_idx}.S.1 Design Assumptions", level=3)
    assumptions = [
        ("Study design", rec["design"]),
        ("Statistical approach", rec["approach"]),
        ("Acceptance limits (90% CI of GMR)", f"{lower:.2f}-{upper:.2f}%"),
        ("Assumed true GMR (T/R)", f"{rec['gmr']:.2f}"),
        ("Within-subject CV", f"{rec['cv_pct']:.1f}% ({rec['cv_source']})"),
        ("Target power", f"{rec['power']*100:.0f}%"),
        ("Significance level (alpha)", f"{rec['alpha']:.2f} (one-sided)"),
        ("Evaluable subjects required", f"{rec['n_evaluable']}"),
        ("Dropout allowance", f"{rec['dropout_rate']*100:.0f}%"),
        ("Planned enrolment", f"{rec['n_enrolled']} subjects{per_arm}"),
        ("Achieved power at planned N", f"{rec['achieved_power']*100:.1f}%"),
    ]
    _ss_kv_table_docx(doc, assumptions)

    _ss_para(
        doc,
        f"Conclusion: a total of {rec['n_enrolled']} subjects{per_arm} will be enrolled "
        f"to provide {rec['n_evaluable']} evaluable subjects, yielding "
        f"{rec['achieved_power']*100:.0f}% power to demonstrate "
        f"{'PK similarity' if protocol.study_type.value == 'biosimilar_pk' else 'bioequivalence'} "
        f"under the stated assumptions.",
    )

    if rec.get("needs_simulation"):
        _ss_para(
            doc,
            "Because a reference-scaled / replicate method applies, the figure above is "
            "the conservative average-bioequivalence estimate. The final sample size must "
            "be confirmed by simulation of the scaled procedure (e.g., the FDA RSABE method "
            "or PowerTOST scABEL), which typically reduces the requirement for highly "
            "variable drugs.",
            italic=True, size=10.5,
        )

    # --- Sensitivity table ------------------------------------------------
    doc.add_heading(
        f"{section_idx}.S.2 Sensitivity of Sample Size to CV and Power", level=3
    )
    headers = [
        "CV (%)", "Crossover 80%", "Crossover 90%",
        "Parallel/arm 80%", "Parallel/arm 90%",
    ]
    rows = [headers]
    for r in rec.get("sensitivity", []):
        rows.append([
            str(r.get("cv_pct", "?")),
            str(r.get("crossover_80", "?")), str(r.get("crossover_90", "?")),
            str(r.get("parallel_80", "?")), str(r.get("parallel_90", "?")),
        ])
    _ss_grid_table_docx(doc, rows)

    _ss_para(
        doc,
        "Note: values are minimum evaluable subject counts (crossover totals across both "
        "sequences; parallel shown per arm) computed by exact TOST power at a true GMR of "
        f"{rec['gmr']:.2f}. The planned design uses the row matching the assumed CV above, "
        "inflated for the dropout allowance.",
        italic=True, size=10.5,
    )


def _ss_kv_table_docx(doc: Document, rows: list[tuple[str, str]]) -> None:
    """Two-column key/value table for the design assumptions."""
    tbl = doc.add_table(rows=len(rows), cols=2)
    tbl.style = "Table Grid"
    for r_idx, (k, v) in enumerate(rows):
        row = tbl.rows[r_idx]
        for c_idx, text in enumerate((k, v)):
            cell = row.cells[c_idx]
            cell.text = ""
            para = cell.paragraphs[0]
            para.paragraph_format.space_after = Pt(0)
            rn = para.add_run(text)
            rn.font.name = "Times New Roman"
            rn.font.size = Pt(10)
            if c_idx == 0:
                rn.bold = True
            shd = OxmlElement("w:shd")
            shd.set(qn("w:val"), "clear")
            shd.set(qn("w:color"), "auto")
            shd.set(qn("w:fill"), "EAF1F8" if c_idx == 0 else "FFFFFF")
            cell._tc.get_or_add_tcPr().append(shd)
        row.cells[0].width = Inches(2.6)
        row.cells[1].width = Inches(4.15)
    doc.add_paragraph()


def _ss_grid_table_docx(
    doc: Document,
    rows: list[list[str]],
    col_widths: list[Any] | None = None,
    font_pt: float = 9,
) -> None:
    """Header + data grid used for the CV sensitivity table.

    ``col_widths`` / ``font_pt`` are optional and default to the original
    behaviour (equal columns across the 6.75" body width, 9 pt); the standalone
    companion documents (ICF / randomization list / CRF spec) reuse this same
    grid with a wide first column and a smaller face so a long field or rule
    does not wrap into an unreadable column.
    """
    tbl = doc.add_table(rows=len(rows), cols=len(rows[0]))
    tbl.style = "Table Grid"
    col_w = Inches(6.75) / len(rows[0])
    for r_idx, row_data in enumerate(rows):
        row = tbl.rows[r_idx]
        is_hdr = r_idx == 0
        for c_idx, cell_text in enumerate(row_data):
            cell = row.cells[c_idx]
            cell.text = ""
            para = cell.paragraphs[0]
            para.paragraph_format.space_after = Pt(0)
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
            else:
                shd.set(qn("w:fill"), "F0F4F8" if r_idx % 2 == 0 else "FFFFFF")
                cell._tc.get_or_add_tcPr().append(shd)
        for c_idx, cell in enumerate(row.cells):
            cell.width = (
                col_widths[c_idx]
                if col_widths and c_idx < len(col_widths)
                else col_w
            )
    _repeat_header_row_docx(tbl)
    doc.add_paragraph()


def _repeat_header_row_docx(tbl) -> None:
    """Mark row 0 as a repeating header (w:tblHeader) so Word reprints it at the
    top of every page a long table spills onto — without it, page 2 of the
    allocation list is a wall of unlabelled columns."""
    try:
        tr_pr = tbl.rows[0]._tr.get_or_add_trPr()
        if tr_pr.find(qn("w:tblHeader")) is None:
            el = OxmlElement("w:tblHeader")
            el.set(qn("w:val"), "true")
            tr_pr.append(el)
    except Exception:  # noqa: BLE001 — cosmetic only; never break an export
        pass


def _add_sample_size_section_legacy_docx(doc: Document, section_idx: int) -> None:
    """Generic fallback table for protocols created before the design engine."""
    cv_values = [15, 20, 25, 30, 35, 40]
    headers = ["CV (%)", "Crossover (80%)", "Crossover (90%)",
               "Parallel/arm (80%)", "Parallel/arm (90%)"]
    rows = [headers]
    for cv in cv_values:
        rows.append([
            str(cv),
            str(_compute_sample_size(cv, 80, 125, "crossover")),
            str(_compute_sample_size(cv, 90, 125, "crossover")),
            str(_compute_sample_size(cv, 80, 125, "parallel")),
            str(_compute_sample_size(cv, 90, 125, "parallel")),
        ])
    _ss_grid_table_docx(doc, rows)


# ---------------------------------------------------------------------------
# PDF: sample size, schedule of activities (parity with DOCX)
# ---------------------------------------------------------------------------


def _wrap_label(text: str, width: int = 16) -> list[str]:
    """Greedy word-wrap a short box label into <=3 lines for the schematic."""
    words = text.split()
    lines: list[str] = []
    cur = ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines[:3]


def _add_study_design_schematic_pdf(story, protocol, styles, section_idx) -> None:
    """Render a deterministic study-design schematic (Figure 1) from the locked
    design — a horizontal box-and-arrow flow that adapts to parallel vs
    crossover. For a parallel design the two treatment arms run as two
    visually separate, parallel lanes from the randomisation split to the
    point they converge again at End of Study, rather than a single box
    naming both arms or a momentary split that immediately re-merges.

    Drawn with reportlab graphics (not AI) so it is always present and always
    matches the computed design, N, and arms.
    """
    if not _RL_OK:
        return
    from reportlab.graphics.shapes import Drawing, Line, Polygon, Rect, String

    rec = (protocol.population_config or {}).get("_sample_size") or {}
    design_key = rec.get("design_key") or "crossover"
    if design_key not in ("parallel", "crossover", "replicate"):
        return
    stages = _design_schematic_stages(rec)
    branched = any(s["arms"] for s in stages)

    width = 425.0
    n_col = len(stages)
    gap = 14.0
    bw = (width - (n_col - 1) * gap) / n_col
    H = 104.0 if branched else 96.0
    cy = H / 2.0
    bh = 34.0
    lane_offset = 16.0             # distance of each arm's lane from centre
    top_lane = cy + lane_offset
    bot_lane = cy - lane_offset
    arm_bh = 26.0
    border = rl_colors.HexColor("#2F5C9E")
    fill = rl_colors.HexColor("#EAF2FB")
    arm_fill = rl_colors.HexColor("#FDF3E3")
    dark = rl_colors.HexColor("#1E293B")

    d = Drawing(width, H)

    def _box(cx_left, cy_center, w, h, label, fillc):
        d.add(Rect(cx_left, cy_center - h / 2.0, w, h, rx=4, ry=4,
                   fillColor=fillc, strokeColor=border, strokeWidth=0.8))
        lines = _wrap_label(label, width=max(8, int(w / 4.4)))
        n = len(lines)
        for li, ln in enumerate(lines):
            ty = cy_center + (n - 1) * 4.0 - li * 8.0 - 2.6
            d.add(String(cx_left + w / 2.0, ty, ln, fontName=_SANS, fontSize=6.2,
                         fillColor=dark, textAnchor="middle"))

    def _arrow(x0, x1, y):
        """Straight horizontal arrow — the common case, and also the final
        approach segment of a branch/converge connector, so every arrowhead
        stays perfectly horizontal even where the line leading into it was
        diagonal."""
        d.add(Line(x0, y, x1 - 4, y, strokeColor=border, strokeWidth=0.9))
        d.add(Polygon([x1, y, x1 - 5, y + 2.6, x1 - 5, y - 2.6],
                      fillColor=border, strokeColor=border))

    def _branch_arrow(x0, y0, x1, y1):
        """Connector between a shared lane and an arm's own lane: a plain
        diagonal for the first stretch (no arrowhead - rotating one to match
        an arbitrary angle is more trouble than it is worth here), then a
        horizontal arrow for the final approach into the box, at y1."""
        x_mid = x0 + (x1 - x0) * 0.45
        d.add(Line(x0, y0, x_mid, y1, strokeColor=border, strokeWidth=0.9))
        _arrow(x_mid, x1, y1)

    x = 0.0
    col_x: list[float] = []
    for stage in stages:
        col_x.append(x)
        if stage["arms"]:
            _box(x, top_lane, bw, arm_bh, stage["arms"][0], arm_fill)
            _box(x, bot_lane, bw, arm_bh, stage["arms"][1], arm_fill)
        else:
            _box(x, cy, bw, bh, stage["label"], fill)
        x += bw + gap

    for i in range(n_col - 1):
        left, right = stages[i], stages[i + 1]
        x0 = col_x[i] + bw
        x1 = col_x[i + 1]
        if left["arms"] and right["arms"]:
            # both sides already in their own lane - two parallel arrows
            _arrow(x0, x1, top_lane)
            _arrow(x0, x1, bot_lane)
        elif right["arms"]:
            # branch point: the single upstream lane splits into two
            _branch_arrow(x0, cy, x1, top_lane)
            _branch_arrow(x0, cy, x1, bot_lane)
        elif left["arms"]:
            # convergence point: the two arm lanes rejoin a single lane
            _branch_arrow(x0, top_lane, x1, cy)
            _branch_arrow(x0, bot_lane, x1, cy)
        else:
            _arrow(x0, x1, cy)

    story.append(Spacer(1, 2 * rl_mm))
    story.append(Paragraph(
        f"<b>Figure 1.</b> Study design schematic — "
        f"{rec.get('design', design_key)}.", styles["Citation"]))
    story.append(d)
    story.append(Spacer(1, 3 * rl_mm))


def _add_sample_size_section_pdf(story, protocol, styles, section_idx):
    """Sample size determination for the PDF, from the locked design recommendation."""
    rec = _normalize_sample_size((protocol.population_config or {}).get("_sample_size"))
    story.append(Paragraph(f"{section_idx}.S  Sample Size Determination", styles["Heading2"]))
    if not rec:
        return

    lower = rec["lower_limit"] * 100
    upper = rec["upper_limit"] * 100
    per_arm = f", {rec['n_per_group']} per arm" if rec.get("n_per_group") else ""

    story.append(Paragraph(
        f"The sample size was determined for the {rec['design'].lower()} using the "
        f"two one-sided tests (TOST) method, calculated in R (PowerTOST package). Approach: "
        f"{rec['approach']}; the 90% CI of the Test/Reference geometric mean ratio for "
        f"{_normalize_pk_notation(rec['endpoint'])} must fall within {lower:.2f}-{upper:.2f}%. "
        f"Regulatory basis: {rec['regulatory_basis']}.",
        styles["Body"],
    ))
    if rec.get("cv_source") == "assumed":
        story.append(Paragraph(
            f"<i>Note: no within-subject CV was available from the literature; "
            f"{rec['cv_pct']:.0f}% is assumed and must be confirmed before finalisation.</i>",
            styles["Body"],
        ))

    assumptions = [
        ["Parameter", "Value"],
        ["Study design", rec["design"]],
        ["Statistical approach", rec["approach"]],
        ["Acceptance limits (90% CI of GMR)", f"{lower:.2f}-{upper:.2f}%"],
        ["Assumed true GMR (T/R)", f"{rec['gmr']:.2f}"],
        ["Within-subject CV", f"{rec['cv_pct']:.1f}% ({rec['cv_source']})"],
        ["Target power", f"{rec['power']*100:.0f}%"],
        ["Significance level (alpha)", f"{rec['alpha']:.2f} (one-sided)"],
        ["Evaluable subjects required", f"{rec['n_evaluable']}"],
        ["Dropout allowance", f"{rec['dropout_rate']*100:.0f}%"],
        ["Planned enrolment", f"{rec['n_enrolled']} subjects{per_arm}"],
        ["Achieved power at planned N", f"{rec['achieved_power']*100:.1f}%"],
    ]
    _simple_table_pdf(story, assumptions, styles, col_widths=[70 * rl_mm, 90 * rl_mm])

    story.append(Paragraph(
        f"Conclusion: {rec['n_enrolled']} subjects{per_arm} will be enrolled to provide "
        f"{rec['n_evaluable']} evaluable subjects ({rec['achieved_power']*100:.0f}% power).",
        styles["Body"],
    ))

    if rec.get("needs_simulation"):
        story.append(Paragraph(
            "<i>This scaled/replicate method's final N must be confirmed by simulation "
            "(FDA RSABE method / PowerTOST scABEL); the stated N is the conservative ABE "
            "estimate.</i>",
            styles["Body"],
        ))

    story.append(Spacer(1, 3 * rl_mm))
    story.append(Paragraph("Sensitivity of sample size to CV and power:", styles["Body"]))
    sens = [["CV (%)", "Crossover 80%", "Crossover 90%", "Parallel/arm 80%", "Parallel/arm 90%"]]
    for r in rec.get("sensitivity", []):
        sens.append([
            str(r.get("cv_pct", "?")), str(r.get("crossover_80", "?")), str(r.get("crossover_90", "?")),
            str(r.get("parallel_80", "?")), str(r.get("parallel_90", "?")),
        ])
    _simple_table_pdf(story, sens, styles)


def _add_schedule_of_activities_pdf(story, protocol, styles, idx, heading):
    """Schedule of Activities matrix for the PDF, numbered as its own top-level
    section (idx, e.g. "8 Overview of Study Procedures and Assessments"),
    matching the reference template."""
    study_type = protocol.study_type.value
    rec = (protocol.population_config or {}).get("_sample_size") or {}
    design_key = rec.get("design_key") or (
        "parallel" if study_type == "biosimilar_pk" else "crossover"
    )
    is_biosimilar = study_type == "biosimilar_pk"

    story.append(Paragraph(f"{idx}  {heading}", styles["Heading1"]))
    story.append(Paragraph(
        f"{idx}.1  Schedule of Study Assessments and Procedures", styles["Heading2"]
    ))
    story.append(Paragraph(
        "'X' indicates the procedure is performed at the visit; see the Study "
        "Assessments section for the full sampling schedule.", styles["Body"],
    ))

    if design_key == "parallel" or is_biosimilar:
        cols = ["Procedure", "Screen", "Day -1", "Day 1", "PK", "Wk 4/8/12", "Wk 24"]
        rows = [
            ("Informed consent", {1}),
            ("Demographics & history", {1}),
            ("Physical examination", {1, 6}),
            ("Vital signs", {1, 2, 3, 4, 5, 6}),
            ("12-lead ECG", {1, 3, 6}),
            ("Clinical laboratory", {1, 2, 5, 6}),
            ("Serology, drug/alcohol screen", {1, 2}),
            ("Pregnancy test (WOCBP)", {1, 2, 6}),
            ("Eligibility review", {1, 2}),
            ("Randomisation", {2}),
            ("Study drug administration", {3}),
            ("PK blood sampling", {4}),
        ]
        if is_biosimilar:
            rows += [
                ("Immunogenicity (ADA/NAb)", {3, 5, 6}),
                ("Injection/infusion-site assessment", {3, 4}),
            ]
        rows += [
            ("Adverse event assessment", {2, 3, 4, 5, 6}),
            ("Concomitant medication review", {1, 2, 3, 4, 5, 6}),
        ]
    else:
        cols = ["Procedure", "Screen", "Day -1", "Day 1", "PK", "Washout", "EoS"]
        rows = [
            ("Informed consent", {1}),
            ("Demographics & history", {1}),
            ("Physical examination", {1, 6}),
            ("Vital signs", {1, 2, 3, 4, 6}),
            ("12-lead ECG", {1, 3, 6}),
            ("Clinical laboratory", {1, 2, 6}),
            ("Serology, drug/alcohol screen", {1, 2}),
            ("Pregnancy test (WOCBP)", {1, 2, 6}),
            ("Eligibility review", {1, 2}),
            ("Randomisation", {2}),
            ("Study drug administration (each period)", {3}),
            ("PK blood sampling (each period)", {4}),
            ("Adverse event assessment", {2, 3, 4, 5, 6}),
            ("Concomitant medication review", {1, 2, 3, 4, 5, 6}),
        ]

    table_data = [cols]
    for proc, marks in rows:
        table_data.append([proc] + ["X" if i in marks else "" for i in range(1, len(cols))])
    _simple_table_pdf(story, table_data, styles, first_col_wide=True)
    story.append(PageBreak())


def _simple_table_pdf(story, data, styles, col_widths=None, first_col_wide=False):
    """Render a header+body table with the standard navy header styling."""
    if not _RL_OK or not data:
        return
    from reportlab.lib.styles import ParagraphStyle as _PS
    cell_style = _PS("STCell", parent=styles["Body"], fontSize=8, leading=11,
                     spaceAfter=0, spaceBefore=0)
    hdr_style = _PS("STHdr", parent=cell_style, fontName=_SANS_BOLD,
                    textColor=rl_colors.white)

    n_cols = len(data[0])
    if col_widths is None:
        if first_col_wide:
            first = 52 * rl_mm
            rest = (160 * rl_mm - first) / (n_cols - 1)
            col_widths = [first] + [rest] * (n_cols - 1)
        else:
            col_widths = [160 * rl_mm / n_cols] * n_cols

    rendered = []
    for r_idx, row in enumerate(data):
        sty = hdr_style if r_idx == 0 else cell_style
        rendered.append([Paragraph(str(c), sty) for c in row])

    tbl = Table(rendered, colWidths=col_widths)
    cmds = [
        ("GRID", (0, 0), (-1, -1), 0.5, rl_colors.HexColor("#CBD5E1")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("BACKGROUND", (0, 0), (-1, 0), rl_colors.HexColor("#1E3A5F")),
    ]
    for r_idx in range(1, len(data)):
        if r_idx % 2 == 0:
            cmds.append(("BACKGROUND", (0, r_idx), (-1, r_idx), rl_colors.HexColor("#F0F4F8")))
    tbl.setStyle(TableStyle(cmds))
    story.append(tbl)
    story.append(Spacer(1, 4 * rl_mm))


def _add_abbreviations_pdf(story: list, study_type: str, styles: dict) -> None:
    """List of abbreviations for the PDF (parity with the DOCX export)."""
    if not _RL_OK:
        return
    story.append(Paragraph("LIST OF ABBREVIATIONS", styles["FrontHeading"]))
    data = [["Abbreviation", "Definition"]]
    data.extend([abbr, full] for abbr, full in _abbreviations_for(study_type))
    _simple_table_pdf(story, data, styles, col_widths=[34 * rl_mm, 126 * rl_mm])


# ===========================================================================
# Standalone companion documents (ICF / randomization list / CRF spec + DMP)
# ===========================================================================
# These are SEPARATE documents, not appendices of the protocol body:
#
#   * the Informed Consent Form is reviewed and approved by the IRB on its own
#     cycle and is version-controlled separately from the protocol, so it gets
#     its own file, its own cover and its own version/date in the running
#     header and footer;
#   * the UNBLINDED randomization allocation list must never travel inside a
#     document that reaches investigators, recruiters or the bioanalytical
#     laboratory (ICH E9 sect. 2.3.2 - allocation concealment), so it is
#     reachable only through its own endpoint and every page of it is stamped
#     RESTRICTED - UNBLINDED. It is deliberately absent from _build_docx /
#     _build_pdf and must stay that way;
#   * the CRF specification and Data Management Plan is an operational
#     companion that would swamp the protocol body.
#
# All three are assembled with the SAME helpers the protocol export already
# uses (_apply_doc_styles, _enable_update_fields_on_open, _hf_tab_line,
# _add_page_number, _ss_para, _ss_kv_table_docx, _ss_grid_table_docx,
# _unstyled_subheading_docx, _pdf_styles, _simple_table_pdf, _pdf_safe). That
# is deliberate rather than lazy: the w:settings child ordering handled by
# _enable_update_fields_on_open / _SETTINGS_TAG_ORDER is schema-sensitive
# (ECMA-376/ISO-29500) and this codebase has already shipped a "Word wants to
# repair this document" bug from getting it wrong. Do not invent new document
# scaffolding here.

# The content engines are imported defensively: a missing or broken companion
# engine must degrade to a clear 501 on its own endpoint, never take the
# protocol .docx/.pdf export down with it.
try:  # pragma: no cover - exercised whenever the engine module imports cleanly
    from services.icf import build_icf as _build_icf_content
    _ICF_ENGINE_OK = True
except Exception:  # noqa: BLE001
    _build_icf_content = None  # type: ignore[assignment]
    _ICF_ENGINE_OK = False

try:  # pragma: no cover
    from services.crf_dmp import build_crf_spec as _build_crf_content
    _CRF_ENGINE_OK = True
except Exception:  # noqa: BLE001
    _build_crf_content = None  # type: ignore[assignment]
    _CRF_ENGINE_OK = False

try:  # pragma: no cover
    from services.randomization import format_unblinded_list as _format_unblinded_list
    _RANDOMIZATION_ENGINE_OK = True
except Exception:  # noqa: BLE001
    _format_unblinded_list = None  # type: ignore[assignment]
    _RANDOMIZATION_ENGINE_OK = False


# The one string that must be impossible to miss on the allocation list. Kept
# ASCII (cp1252-safe) because this document is printed and filed on Windows.
_RESTRICTED_BANNER = "RESTRICTED - UNBLINDED - DO NOT CIRCULATE"
_RESTRICTED_RGB = RGBColor(0xB9, 0x1C, 0x1C)
_RESTRICTED_HEX = "#B91C1C"
_BLANK_LINE = "_" * 46


def _cell(value: Any, default: str = "") -> str:
    """Flatten an engine value into one plain string for a table cell.

    The companion engines emit nested lists/dicts freely; a table cell has to be
    a string, and str({...}) in a regulatory document is not acceptable output.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, (list, tuple)):
        parts = [_cell(v) for v in value]
        return "; ".join(p for p in parts if p)
    if isinstance(value, dict):
        parts = [f"{k}: {_cell(v)}" for k, v in value.items()]
        return "; ".join(p for p in parts if p)
    return str(value)


# ---------------------------------------------------------------------------
# Resolving the engine payloads
# ---------------------------------------------------------------------------


def _companion_context(protocol: Protocol, drug_inn: str) -> dict[str, Any]:
    """Rebuild the minimal generation context the companion engines read.

    Used ONLY as a fallback for a protocol created before the engine was wired
    into _augment_config_with_design. Everything here is either stored on the
    protocol row or derived deterministically from it - nothing is invented and
    nothing is re-fetched from an external service at export time.
    """
    pop = dict(protocol.population_config or {})
    ss = pop.get("_sample_size") or {}
    title = _strip_html(protocol.sections.get("title") or "").strip()
    return {
        "drug_inn": drug_inn,
        "study_type": protocol.study_type.value,
        "population_config": pop,
        "sample_size": ss,
        "_sample_size": ss,
        "label_data": pop.get("_label") or {},
        "guideline_version": protocol.guideline_version,
        "org_name": pop.get("org_name") or protocol.org_id or "",
        "protocol_number": _protocol_code(protocol, drug_inn),
        "protocol_version": f"{protocol.version}.0",
        "protocol_title": title or None,
        "sampling_schedule": pop.get("_sampling_schedule"),
        "_sampling_schedule": pop.get("_sampling_schedule"),
        "design_decisions": pop.get("_design_decisions"),
        "_design_decisions": pop.get("_design_decisions"),
    }


def _resolve_icf(protocol: Protocol, drug_inn: str) -> dict[str, Any]:
    """The stored ICF content model, or a freshly computed one for a protocol
    created before the engine was wired in."""
    stored = (protocol.population_config or {}).get("_icf")
    if isinstance(stored, dict) and stored.get("sections"):
        return stored
    if not _ICF_ENGINE_OK:
        raise HTTPException(
            501,
            detail="Informed Consent Form generation is unavailable "
                   "(services/icf.py could not be imported).",
        )
    try:
        return _build_icf_content(_companion_context(protocol, drug_inn))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            500, detail=f"Informed Consent Form content could not be assembled: {exc}"
        ) from exc


def _resolve_crf_dmp(protocol: Protocol, drug_inn: str) -> dict[str, Any]:
    """The stored CRF/DMP specification, or a freshly computed one."""
    stored = (protocol.population_config or {}).get("_crf_dmp")
    if isinstance(stored, dict) and stored.get("forms"):
        return stored
    if not _CRF_ENGINE_OK:
        raise HTTPException(
            501,
            detail="CRF/DMP generation is unavailable "
                   "(services/crf_dmp.py could not be imported).",
        )
    pop = protocol.population_config or {}
    ss = pop.get("_sample_size") or {}
    try:
        return _build_crf_content(
            _companion_context(protocol, drug_inn),
            design_key=ss.get("design_key"),
            study_type=protocol.study_type.value,
            sampling=pop.get("_sampling_schedule"),
            label=pop.get("_label"),
            n_enrolled=ss.get("n_enrolled"),
            t_half_hours=ss.get("t_half_hours"),
            lloq=pop.get("lloq"),
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            500, detail=f"CRF specification could not be assembled: {exc}"
        ) from exc


def _resolve_randomization(protocol: Protocol) -> dict[str, Any]:
    """The stored allocation list - never regenerated at export time.

    Unlike the ICF and the CRF specification, the randomization list is NOT a
    pure function of the protocol: it is a function of the recorded seed. Making
    one up at export time would produce a list that is not the one the study was
    randomised against, which is the single worst failure this document can
    have. So a protocol without a filed schedule gets an explicit refusal.
    """
    stored = (protocol.population_config or {}).get("_randomization")
    if isinstance(stored, dict) and stored.get("unblinded_allocation"):
        return stored
    raise HTTPException(
        409,
        detail=(
            "No randomization schedule has been generated and filed for this "
            "protocol, so there is no allocation list to export. The list is "
            "never regenerated at export time: a re-drawn list would not be the "
            "one this study was randomised against. Regenerate the protocol "
            "design so the schedule and its seed are recorded, then export."
        ),
    )


async def _log_companion_export(
    db: AsyncSession,
    actor: Any,
    protocol_id: Any,
    action: str,
    diff: dict[str, Any],
) -> None:
    """Audit-log a companion-document export (same shape as the protocol export
    entries, so one query over resource_type='protocol' still sees everything)."""
    db.add(AuditLog(
        id=uuid.uuid4(),
        user_id=actor.id,
        action=action,
        resource_type="protocol",
        resource_id=str(protocol_id),
        diff={**diff, "exported_at": datetime.now(timezone.utc).isoformat()},
        timestamp=datetime.now(timezone.utc),
    ))
    await db.commit()


# ---------------------------------------------------------------------------
# Shared DOCX scaffolding for the companion documents
# ---------------------------------------------------------------------------


def _companion_docx() -> Document:
    """A blank A4 document with the protocol export's page setup and styles."""
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
    return doc


def _companion_header_docx(
    section, left1: str, right1: str, left2: str, right2: str, banner: str = "",
) -> None:
    """Two-line running header, optionally preceded by a bold red banner line."""
    header = section.header
    header.is_linked_to_previous = False
    first = header.paragraphs[0] if header.paragraphs else header.add_paragraph()
    first.clear()
    if banner:
        first.alignment = WD_ALIGN_PARAGRAPH.CENTER
        rb = first.add_run(banner)
        rb.bold = True
        rb.font.name = "Times New Roman"
        rb.font.size = Pt(9)
        rb.font.color.rgb = _RESTRICTED_RGB
        line1 = header.add_paragraph()
    else:
        line1 = first
    _hf_tab_line(line1, left1, right1)
    _hf_tab_line(header.add_paragraph(), left2, right2)


def _companion_footer_docx(section, note: str = "", banner: str = "") -> None:
    """Centered 'Page N of M', an optional document-identity note, and an
    optional bold red confidentiality banner on every page."""
    footer = section.footer
    footer.is_linked_to_previous = False
    para = footer.paragraphs[0] if footer.paragraphs else footer.add_paragraph()
    para.clear()
    para.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _add_page_number(para, size_pt=9)
    if note:
        p = footer.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        rn = p.add_run(note)
        rn.italic = True
        rn.font.name = "Times New Roman"
        rn.font.size = Pt(8)
        rn.font.color.rgb = RGBColor(0x64, 0x74, 0x8B)
    if banner:
        p = footer.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        rb = p.add_run(banner)
        rb.bold = True
        rb.font.name = "Times New Roman"
        rb.font.size = Pt(9)
        rb.font.color.rgb = _RESTRICTED_RGB


def _companion_cover_docx(
    doc: Document,
    *,
    doc_type: str,
    subtitle: str,
    rows: list[tuple[str, str]],
    notices: tuple[str, ...] = (),
    banner: str = "",
) -> None:
    """Cover page: optional banner, document type, subtitle, metadata table."""
    if banner:
        pb = doc.add_paragraph()
        pb.alignment = WD_ALIGN_PARAGRAPH.CENTER
        pb.paragraph_format.space_before = Pt(12)
        pb.paragraph_format.space_after = Pt(12)
        rb = pb.add_run(banner)
        rb.bold = True
        rb.font.name = "Times New Roman"
        rb.font.size = Pt(16)
        rb.font.color.rgb = _RESTRICTED_RGB

    p_title = doc.add_paragraph()
    p_title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p_title.paragraph_format.space_before = Pt(0 if banner else 36)
    p_title.paragraph_format.space_after = Pt(10)
    rt = p_title.add_run(doc_type)
    rt.bold = True
    rt.font.name = "Times New Roman"
    rt.font.size = Pt(15)

    if subtitle:
        p_sub = doc.add_paragraph()
        p_sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p_sub.paragraph_format.space_after = Pt(24)
        rs = p_sub.add_run(subtitle)
        rs.bold = True
        rs.font.name = "Times New Roman"
        rs.font.size = Pt(11)

    if rows:
        _ss_kv_table_docx(doc, rows)
    for notice in notices:
        _ss_para(doc, notice, italic=True, size=10)


# ---------------------------------------------------------------------------
# Shared PDF scaffolding for the companion documents
# ---------------------------------------------------------------------------


def _make_companion_canvas(
    *,
    left1: str,
    left2: str,
    right1: str,
    right2: str,
    footer_note: str = "",
    banner: str = "",
) -> type:
    """NumberedCanvas variant for a companion document.

    Same two-pass buffering as _make_numbered_canvas (page states are held and
    replayed once the total is known, giving 'Page N of M'), with the protocol
    header swapped for the companion document's own identity and an optional
    confidentiality banner top and bottom of every page.
    """

    class CompanionCanvas(rl_canvas.Canvas):  # type: ignore[misc]
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

            top = H - 15 * rl_mm
            if banner:
                self.setFont("Helvetica-Bold", 9)
                self.setFillColor(rl_colors.HexColor(_RESTRICTED_HEX))
                self.drawCentredString(W / 2, H - 11 * rl_mm, banner)
                top = H - 19 * rl_mm

            self.setFont("Helvetica", 8)
            self.setFillColor(rl_colors.HexColor("#64748B"))
            self.drawString(lm, top, left1)
            self.drawString(lm, top - 4 * rl_mm, left2)
            self.drawRightString(W - rm, top, right1)
            self.drawRightString(W - rm, top - 4 * rl_mm, right2)
            self.setStrokeColor(rl_colors.HexColor("#E2E8F0"))
            self.line(lm, top - 6 * rl_mm, W - rm, top - 6 * rl_mm)

            self.line(lm, 16 * rl_mm, W - rm, 16 * rl_mm)
            self.setFont("Helvetica", 9)
            self.setFillColor(rl_colors.HexColor("#64748B"))
            self.drawCentredString(
                W / 2, 11 * rl_mm, f"Page {self._pageNumber} of {total_pages}"
            )
            if footer_note:
                self.setFont("Helvetica-Oblique", 7)
                self.drawCentredString(W / 2, 7.5 * rl_mm, footer_note)
            if banner:
                self.setFont("Helvetica-Bold", 8)
                self.setFillColor(rl_colors.HexColor(_RESTRICTED_HEX))
                self.drawCentredString(W / 2, 4 * rl_mm, banner)

            self.restoreState()

    return CompanionCanvas


def _companion_pdf_doc(
    buf: io.BytesIO, *, title: str, subject: str,
    top_mm: float = 38.0, bottom_mm: float = 28.0,
) -> Any:
    """SimpleDocTemplate with the protocol export's A4 geometry."""
    return SimpleDocTemplate(
        buf,
        pagesize=RL_A4,
        leftMargin=30 * rl_mm,
        rightMargin=30 * rl_mm,
        topMargin=top_mm * rl_mm,
        bottomMargin=bottom_mm * rl_mm,
        title=title,
        subject=subject,
        author="PK/BE Protocol Platform",
    )


def _companion_cover_pdf(
    story: list[Any],
    styles: dict[str, Any],
    *,
    doc_type: str,
    subtitle: str,
    rows: list[tuple[str, str]],
    notices: tuple[str, ...] = (),
    banner: str = "",
) -> None:
    """PDF cover page, parity with _companion_cover_docx."""
    if banner:
        story.append(Paragraph(
            f'<font color="{_RESTRICTED_HEX}"><b>{_pdf_safe(banner)}</b></font>',
            styles["CoverTitle"]))
        story.append(Spacer(1, 4 * rl_mm))
    story.append(Paragraph(_pdf_safe(doc_type), styles["CoverTitle"]))
    if subtitle:
        story.append(Spacer(1, 4 * rl_mm))
        story.append(Paragraph(_pdf_safe(subtitle), styles["CoverSubtitle"]))
    story.append(Spacer(1, 8 * rl_mm))
    if rows:
        _long_table_pdf(
            story,
            [["Field", "Value"]] + [[_pdf_safe(k), _pdf_safe(v)] for k, v in rows],
            styles,
            col_widths=[55 * rl_mm, 105 * rl_mm],
            font_size=9,
        )
    for notice in notices:
        story.append(Paragraph(f"<i>{_pdf_safe(notice)}</i>", styles["Body"]))


def _long_table_pdf(
    story: list[Any],
    data: list[list[str]],
    styles: dict[str, Any],
    *,
    col_widths: list[float] | None = None,
    font_size: float = 8.0,
) -> None:
    """Header + body table whose header row REPEATS on every page.

    _simple_table_pdf is fine for the short protocol tables it was written for,
    but the allocation list and the CRF field tables run for pages, and a
    continuation page of unlabelled columns is unusable. Cell text must already
    be _pdf_safe'd by the caller.
    """
    if not _RL_OK or not data:
        return
    from reportlab.lib.styles import ParagraphStyle as _PS
    cell_style = _PS(
        "CmpCell", parent=styles["Body"], fontSize=font_size,
        leading=font_size + 2.6, spaceAfter=0, spaceBefore=0,
    )
    hdr_style = _PS(
        "CmpHdr", parent=cell_style, fontName=_SANS_BOLD,
        textColor=rl_colors.white,
    )
    n_cols = len(data[0])
    if col_widths is None:
        col_widths = [(160 * rl_mm) / n_cols] * n_cols

    rendered = [
        [_safe_paragraph(str(c), hdr_style if r_idx == 0 else cell_style) for c in row]
        for r_idx, row in enumerate(data)
    ]
    tbl = Table(rendered, colWidths=col_widths, repeatRows=1)
    cmds = [
        ("GRID", (0, 0), (-1, -1), 0.5, rl_colors.HexColor("#CBD5E1")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("BACKGROUND", (0, 0), (-1, 0), rl_colors.HexColor("#1E3A5F")),
    ]
    for r_idx in range(1, len(data)):
        if r_idx % 2 == 0:
            cmds.append(
                ("BACKGROUND", (0, r_idx), (-1, r_idx), rl_colors.HexColor("#F0F4F8"))
            )
    tbl.setStyle(TableStyle(cmds))
    story.append(tbl)
    story.append(Spacer(1, 4 * rl_mm))


# ---------------------------------------------------------------------------
# Informed Consent Form (its own document, its own version, its own IRB cycle)
# ---------------------------------------------------------------------------


def _icf_header_fields(icf: dict[str, Any]) -> list[tuple[str, str]]:
    """Split the ICF's own 'header' section (a labelled field list, not prose)
    into (label, value) pairs for the cover table.

    Two fields share one line where the engine pairs them (e.g. 'Protocol
    number: X   Protocol version: Y'), separated by run of spaces.
    """
    out: list[tuple[str, str]] = []
    for s in icf.get("sections") or []:
        if s.get("key") != "header":
            continue
        for line in s.get("body") or []:
            for frag in re.split(r"\s{3,}", str(line)):
                frag = frag.strip()
                if not frag:
                    continue
                if ":" in frag:
                    label, value = frag.split(":", 1)
                    out.append((label.strip(), value.strip()))
                else:
                    out.append((frag, ""))
        break
    return out


def _icf_cover_rows(
    protocol: Protocol, drug_inn: str, icf: dict[str, Any],
) -> list[tuple[str, str]]:
    ver = icf.get("version") or {}
    facts = icf.get("study_facts") or {}
    rows = _icf_header_fields(icf)
    if not rows:
        rows = [
            ("Consent form version", _cell(ver.get("icf_version"), "1.0")),
            ("Consent form date", _cell(ver.get("icf_date"), "[__________]")),
            ("Protocol number", _cell(ver.get("protocol_number"),
                                      _protocol_code(protocol, drug_inn))),
            ("Protocol version", _cell(ver.get("protocol_version"),
                                       f"{protocol.version}.0")),
        ]
    rows += [
        ("Investigational product", drug_inn.title()),
        ("Study", _phase_label(protocol.study_type.value)),
        ("Subjects", _cell(facts.get("subject_population"))),
        ("Guideline", _cell(ver.get("guideline_version"), protocol.guideline_version)),
        ("Regulatory basis", _cell(icf.get("regulatory_basis"))),
    ]
    return [(k, v) for k, v in rows if k]


def _build_icf_docx(protocol: Protocol, drug_inn: str, icf: dict[str, Any]) -> bytes:
    """Assemble the Informed Consent Form as its own .docx."""
    doc = _companion_docx()
    ver = icf.get("version") or {}
    icf_version = _cell(ver.get("icf_version"), "1.0")
    icf_date = _cell(ver.get("icf_date"), "[__________]")

    _companion_header_docx(
        doc.sections[0],
        "Informed Consent Form", f"ICF Version: {icf_version}",
        _cell(ver.get("protocol_number"), _protocol_code(protocol, drug_inn)),
        f"ICF Dated: {icf_date}",
    )
    _companion_footer_docx(
        doc.sections[0],
        note=("Informed Consent Form - version controlled and IRB-approved "
              "separately from the protocol"),
    )

    _companion_cover_docx(
        doc,
        doc_type="Informed Consent Form",
        subtitle=_cell(icf.get("title")),
        rows=_icf_cover_rows(protocol, drug_inn, icf),
        notices=(
            _cell(ver.get("note")),
            "A signed and dated copy of this form is given to the subject.",
        ),
    )
    doc.add_page_break()

    # --- the form itself ---------------------------------------------------
    idx = 0
    for s in icf.get("sections") or []:
        if s.get("key") == "header":
            continue  # already rendered as the cover's identification block
        idx += 1
        doc.add_heading(f"{idx} {_cell(s.get('heading'), _cell(s.get('key')))}", level=1)

        refs = s.get("cfr_refs") or []
        if refs:
            p = doc.add_paragraph()
            rr = p.add_run("21 CFR " + "; ".join(str(r) for r in refs))
            rr.italic = True
            rr.font.name = "Times New Roman"
            rr.font.size = Pt(8)
            rr.font.color.rgb = RGBColor(0x64, 0x74, 0x8B)

        if not s.get("applicable", True):
            _ss_para(doc, "Not applicable to this study. "
                          + _cell(s.get("applicability_note")), italic=True, size=10)
            continue

        if s.get("verbatim"):
            # Statutory wording. It is emitted character-for-character and must
            # not be reflowed, shortened or run through a plain-language pass.
            _ss_para(
                doc,
                "The wording below is prescribed by regulation and is reproduced "
                "exactly. It must not be simplified, shortened or reformatted.",
                italic=True, size=9,
            )
        for line in s.get("body") or []:
            _ss_para(doc, str(line), size=10.5)
        for bullet in s.get("bullets") or []:
            doc.add_paragraph(str(bullet), style="List Bullet")

    _icf_signatures_docx(doc, icf)
    _icf_readiness_docx(doc, icf)

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf.getvalue()


def _icf_signatures_docx(doc: Document, icf: dict[str, Any]) -> None:
    """Signature pages: the timing rule, then a ruled block per signatory."""
    sig = icf.get("signature_block") or {}
    doc.add_page_break()
    doc.add_heading("Signatures", level=1)
    if sig.get("timing_rule"):
        _ss_para(doc, _cell(sig.get("timing_rule")))
    if sig.get("copy_rule"):
        _ss_para(doc, _cell(sig.get("copy_rule")))
    if sig.get("regulatory_basis"):
        _ss_para(doc, "Regulatory basis: " + _cell(sig.get("regulatory_basis")),
                 italic=True, size=9)

    for row in sig.get("rows") or []:
        required = bool(row.get("required"))
        label = _cell(row.get("role"))
        if not required:
            label += "  (complete only if applicable - otherwise write N/A)"
        _unstyled_subheading_docx(doc, label, size=11)
        if not required and row.get("required_when"):
            _ss_para(doc, "When required: " + _cell(row.get("required_when")),
                     italic=True, size=9)
        if row.get("note"):
            _ss_para(doc, _cell(row.get("note")), italic=True, size=9)
        fields = [str(f) for f in (row.get("fields") or [])]
        if fields:
            _ss_kv_table_docx(doc, [(f, _BLANK_LINE) for f in fields])


def _icf_readiness_docx(doc: Document, icf: dict[str, Any]) -> None:
    """Appendix A/B: what still blocks IRB submission, and 50.25 coverage.

    Kept as appendices of the ICF rather than folded into the protocol's QC
    appendix, because the IRB reviews this document on its own.
    """
    doc.add_page_break()
    doc.add_heading("Appendix A: IRB Submission Readiness", level=1)

    missing = icf.get("missing_required_elements") or []
    if missing:
        _ss_para(
            doc,
            "BLOCKING - required basic elements of consent (21 CFR 50.25(a)) that "
            "this form does not cover: " + ", ".join(str(m) for m in missing)
            + ". A form missing a basic element is an administrative rejection.",
        )
    else:
        _ss_para(doc, "All basic elements of informed consent required by "
                      "21 CFR 50.25(a) are covered by this form.")

    _unstyled_subheading_docx(doc, "Fields still to be completed", size=11)
    pending = icf.get("pending_fields") or []
    if pending:
        _ss_grid_table_docx(
            doc,
            [["Field", "Why it is required"]]
            + [[_cell(p.get("field")), _cell(p.get("why"))] for p in pending],
            col_widths=[Inches(1.9), Inches(4.85)], font_pt=8.5,
        )
        _ss_para(doc, "Placeholders shown as [__________] in the body above must be "
                      "filled in before this form is submitted to the IRB.",
                 italic=True, size=9)
    else:
        _ss_para(doc, "None - no placeholder fields remain.")

    assumptions = icf.get("assumptions") or []
    if assumptions:
        _unstyled_subheading_docx(doc, "Disclosed assumptions", size=11)
        _ss_grid_table_docx(
            doc,
            [["Field", "Value", "Basis"]]
            + [[_cell(a.get("field")), _cell(a.get("value")), _cell(a.get("why"))]
               for a in assumptions],
            col_widths=[Inches(1.5), Inches(1.3), Inches(3.95)], font_pt=8.5,
        )

    readability = icf.get("readability") or {}
    overall = readability.get("overall") or {}
    if overall:
        _unstyled_subheading_docx(doc, "Plain-language check", size=11)
        over = readability.get("sections_over_target") or []
        _ss_para(
            doc,
            f"Overall reading grade {_cell(overall.get('grade_level'))} against a "
            f"target of {_cell(readability.get('target_grade'))} "
            f"({_cell(overall.get('words'))} words). "
            + ("Sections above target: " + ", ".join(str(s) for s in over) + "."
               if over else "Every graded section meets the target."),
            size=10,
        )
        _ss_para(doc, _cell(readability.get("method")), italic=True, size=9)

    volume = (icf.get("study_facts") or {}).get("blood_volume_check") or {}
    if volume and not volume.get("within_ceiling", True):
        _ss_para(
            doc,
            "WARNING: the total blood volume stated in this form exceeds the "
            "conventional ceiling for the window checked. Reduce the draw volume "
            "or the number of draws before IRB submission.",
        )

    doc.add_page_break()
    doc.add_heading("Appendix B: 21 CFR 50.25 Element Coverage", level=1)
    coverage = icf.get("elements_coverage") or {}
    rows = [["21 CFR", "Tier", "Requirement", "Covered", "Section(s)"]]
    for code, item in coverage.items():
        rows.append([
            str(code),
            _cell((item or {}).get("tier")),
            _cell((item or {}).get("requirement")),
            _cell((item or {}).get("covered")),
            _cell((item or {}).get("sections")) or "-",
        ])
    if len(rows) > 1:
        _ss_grid_table_docx(
            doc, rows,
            col_widths=[Inches(0.85), Inches(0.7), Inches(3.0), Inches(0.6),
                        Inches(1.6)],
            font_pt=8,
        )


def _build_icf_pdf(protocol: Protocol, drug_inn: str, icf: dict[str, Any]) -> bytes:
    """Assemble the Informed Consent Form as its own PDF (parity with the DOCX)."""
    assert _RL_OK, "reportlab must be installed (pip install reportlab)"
    ver = icf.get("version") or {}
    icf_version = _cell(ver.get("icf_version"), "1.0")
    icf_date = _cell(ver.get("icf_date"), "[__________]")

    buf = io.BytesIO()
    doc = _companion_pdf_doc(
        buf,
        title=f"Informed Consent Form - {drug_inn.title()}",
        subject=f"ICF v{icf_version} - separate from protocol v{protocol.version}",
    )
    canvasmaker = _make_companion_canvas(
        left1="Informed Consent Form",
        left2=_cell(ver.get("protocol_number"), _protocol_code(protocol, drug_inn)),
        right1=f"ICF Version: {icf_version}",
        right2=f"ICF Dated: {icf_date}",
        footer_note=("Informed Consent Form - version controlled and IRB-approved "
                     "separately from the protocol"),
    )

    styles = _pdf_styles()
    story: list[Any] = []
    _companion_cover_pdf(
        story, styles,
        doc_type="Informed Consent Form",
        subtitle=_cell(icf.get("title")),
        rows=_icf_cover_rows(protocol, drug_inn, icf),
        notices=(
            _cell(ver.get("note")),
            "A signed and dated copy of this form is given to the subject.",
        ),
    )
    story.append(PageBreak())

    idx = 0
    for s in icf.get("sections") or []:
        if s.get("key") == "header":
            continue
        idx += 1
        story.append(Paragraph(
            f"{idx}  {_pdf_safe(_cell(s.get('heading'), _cell(s.get('key'))))}",
            styles["Heading1"]))
        refs = s.get("cfr_refs") or []
        if refs:
            story.append(Paragraph(
                "<i>21 CFR " + _pdf_safe("; ".join(str(r) for r in refs)) + "</i>",
                styles["Citation"]))
        if not s.get("applicable", True):
            story.append(Paragraph(
                "<i>Not applicable to this study. "
                + _pdf_safe(_cell(s.get("applicability_note"))) + "</i>",
                styles["Body"]))
            continue
        if s.get("verbatim"):
            story.append(Paragraph(
                "<i>The wording below is prescribed by regulation and is reproduced "
                "exactly. It must not be simplified, shortened or reformatted.</i>",
                styles["Citation"]))
        for line in s.get("body") or []:
            story.append(_safe_paragraph(_pdf_safe(str(line)), styles["Body"]))
        for bullet in s.get("bullets") or []:
            story.append(_safe_paragraph(
                "- " + _pdf_safe(str(bullet)), styles["Bullet"]))

    # --- signatures --------------------------------------------------------
    sig = icf.get("signature_block") or {}
    story.append(PageBreak())
    story.append(Paragraph("Signatures", styles["Heading1"]))
    for key in ("timing_rule", "copy_rule"):
        if sig.get(key):
            story.append(Paragraph(_pdf_safe(_cell(sig.get(key))), styles["Body"]))
    if sig.get("regulatory_basis"):
        story.append(Paragraph(
            "<i>Regulatory basis: " + _pdf_safe(_cell(sig.get("regulatory_basis")))
            + "</i>", styles["Citation"]))
    for row in sig.get("rows") or []:
        required = bool(row.get("required"))
        label = _cell(row.get("role"))
        if not required:
            label += "  (complete only if applicable - otherwise write N/A)"
        story.append(Paragraph(_pdf_safe(label), styles["Heading2"]))
        if not required and row.get("required_when"):
            story.append(Paragraph(
                "<i>When required: " + _pdf_safe(_cell(row.get("required_when")))
                + "</i>", styles["Citation"]))
        if row.get("note"):
            story.append(Paragraph(
                "<i>" + _pdf_safe(_cell(row.get("note"))) + "</i>", styles["Citation"]))
        fields = [str(f) for f in (row.get("fields") or [])]
        if fields:
            _long_table_pdf(
                story,
                [["Field", "Complete by hand"]]
                + [[_pdf_safe(f), _pdf_safe(_BLANK_LINE)] for f in fields],
                styles, col_widths=[55 * rl_mm, 105 * rl_mm], font_size=9,
            )

    _icf_readiness_pdf(story, styles, icf)

    doc.build(story, canvasmaker=canvasmaker)
    buf.seek(0)
    return buf.getvalue()


def _icf_readiness_pdf(story: list[Any], styles: dict[str, Any],
                       icf: dict[str, Any]) -> None:
    """PDF parity for _icf_readiness_docx."""
    story.append(PageBreak())
    story.append(Paragraph("Appendix A: IRB Submission Readiness", styles["Heading1"]))

    missing = icf.get("missing_required_elements") or []
    if missing:
        story.append(Paragraph(
            "BLOCKING - required basic elements of consent (21 CFR 50.25(a)) that "
            "this form does not cover: "
            + _pdf_safe(", ".join(str(m) for m in missing))
            + ". A form missing a basic element is an administrative rejection.",
            styles["Body"]))
    else:
        story.append(Paragraph(
            "All basic elements of informed consent required by 21 CFR 50.25(a) "
            "are covered by this form.", styles["Body"]))

    story.append(Paragraph("Fields still to be completed", styles["Heading2"]))
    pending = icf.get("pending_fields") or []
    if pending:
        _long_table_pdf(
            story,
            [["Field", "Why it is required"]]
            + [[_pdf_safe(_cell(p.get("field"))), _pdf_safe(_cell(p.get("why")))]
               for p in pending],
            styles, col_widths=[45 * rl_mm, 115 * rl_mm], font_size=8.5,
        )
    else:
        story.append(Paragraph("None - no placeholder fields remain.", styles["Body"]))

    assumptions = icf.get("assumptions") or []
    if assumptions:
        story.append(Paragraph("Disclosed assumptions", styles["Heading2"]))
        _long_table_pdf(
            story,
            [["Field", "Value", "Basis"]]
            + [[_pdf_safe(_cell(a.get("field"))), _pdf_safe(_cell(a.get("value"))),
                _pdf_safe(_cell(a.get("why")))] for a in assumptions],
            styles, col_widths=[35 * rl_mm, 30 * rl_mm, 95 * rl_mm], font_size=8.5,
        )

    readability = icf.get("readability") or {}
    overall = readability.get("overall") or {}
    if overall:
        over = readability.get("sections_over_target") or []
        story.append(Paragraph("Plain-language check", styles["Heading2"]))
        story.append(Paragraph(
            f"Overall reading grade {_pdf_safe(_cell(overall.get('grade_level')))} "
            f"against a target of {_pdf_safe(_cell(readability.get('target_grade')))} "
            f"({_pdf_safe(_cell(overall.get('words')))} words). "
            + ("Sections above target: "
               + _pdf_safe(", ".join(str(s) for s in over)) + "."
               if over else "Every graded section meets the target."),
            styles["Body"]))

    story.append(PageBreak())
    story.append(Paragraph("Appendix B: 21 CFR 50.25 Element Coverage",
                           styles["Heading1"]))
    coverage = icf.get("elements_coverage") or {}
    rows = [["21 CFR", "Tier", "Requirement", "Covered", "Section(s)"]]
    for code, item in coverage.items():
        rows.append([
            _pdf_safe(str(code)),
            _pdf_safe(_cell((item or {}).get("tier"))),
            _pdf_safe(_cell((item or {}).get("requirement"))),
            _pdf_safe(_cell((item or {}).get("covered"))),
            _pdf_safe(_cell((item or {}).get("sections")) or "-"),
        ])
    if len(rows) > 1:
        _long_table_pdf(
            story, rows, styles,
            col_widths=[20 * rl_mm, 16 * rl_mm, 72 * rl_mm, 16 * rl_mm, 36 * rl_mm],
            font_size=7.5,
        )


# ---------------------------------------------------------------------------
# Randomization allocation list - RESTRICTED, UNBLINDED, signed appendix
# ---------------------------------------------------------------------------
# This document carries per-subject treatment assignments. It is deliberately
# NOT reachable from _build_docx / _build_pdf: putting it inside the protocol
# would hand every investigator the allocation and destroy the study's
# allocation concealment (ICH E9 sect. 2.3.2). Every page is stamped, the cover
# states the holders, and the release is signed for.

# Who signs the appendix, and what their signature attests to. These are
# protocol-package roles, not an implementation of a workflow: nothing here
# tracks whether a signature has actually been obtained.
_RANDOMIZATION_SIGNATORIES: tuple[tuple[str, str, str], ...] = (
    ("Prepared by", "Unblinded randomization statistician",
     "Generated this list from the recorded seed and verified it against the "
     "locked design, sequence set and allocation ratio."),
    ("Checked by", "Independent (unblinded) reviewer",
     "Independently confirmed the sequence set, the allocation ratio and the "
     "balance statement, and that no assignment is disclosed elsewhere."),
    ("Released to", "Investigational pharmacy / dispensing",
     "Received the list under restricted access, for dispensing only."),
    ("Filed by", "Trial master file custodian",
     "Filed as a restricted appendix; access to it is logged."),
)


def _randomization_cover_rows(
    protocol: Protocol, drug_inn: str, alloc: dict[str, Any],
) -> list[tuple[str, str]]:
    design = alloc.get("design") or {}
    repro = alloc.get("reproducibility") or {}
    unblinded = alloc.get("unblinded_allocation") or {}
    strat = alloc.get("stratification") or {}
    return [
        ("Protocol number", _protocol_code(protocol, drug_inn)),
        ("Protocol version", f"{protocol.version}.0"),
        ("Investigational product", drug_inn.title()),
        ("Design", _cell(design.get("label"), _cell(design.get("key")))),
        ("Sequences", _cell(design.get("sequences"))),
        ("Allocation ratio", _cell(design.get("allocation_ratio"))),
        ("Periods", _cell(design.get("n_periods"))),
        ("Stratified", _cell(strat.get("applied"))),
        ("Number of strata", _cell(strat.get("n_strata"))),
        ("Entries in this list", _cell(unblinded.get("n_entries"))),
        ("Confidentiality", _cell(unblinded.get("confidentiality"), "RESTRICTED")),
        ("Randomization seed (RESTRICTED)", _cell(repro.get("seed"))),
        ("Engine", f"{_cell(alloc.get('engine'))} "
                   f"v{_cell(alloc.get('engine_version'))}"),
        ("Generated", datetime.now(timezone.utc).strftime("%d-%b-%Y")),
    ]


def _allocation_rows(alloc: dict[str, Any]) -> list[list[str]]:
    """The allocation table: header row plus one row per subject slot."""
    entries = ((alloc.get("unblinded_allocation") or {}).get("entries")) or []
    rows: list[list[str]] = [[
        "Subject ID", "Rand. no.", "Stratum", "Block", "Pos.",
        "Sequence", "Treatment by period", "Role",
    ]]
    for e in entries:
        by_period = e.get("treatment_by_period") or {}
        try:
            ordered = sorted(by_period.items(), key=lambda kv: int(kv[0]))
        except (TypeError, ValueError):
            ordered = sorted(by_period.items())
        stratum = e.get("stratum") or {}
        stratum_txt = "; ".join(f"{k}={v}" for k, v in sorted(stratum.items()))
        rows.append([
            _cell(e.get("subject_id")),
            _cell(e.get("randomization_number")),
            _cell(e.get("stratum_id")) + (f" ({stratum_txt})" if stratum_txt else ""),
            _cell(e.get("block")),
            _cell(e.get("position_in_block")),
            _cell(e.get("sequence")),
            "; ".join(f"P{p}={t}" for p, t in ordered),
            _cell(e.get("role")),
        ])
    return rows


def _build_randomization_docx(
    protocol: Protocol, drug_inn: str, alloc: dict[str, Any],
) -> bytes:
    """Assemble the unblinded allocation list as a restricted, signed appendix."""
    doc = _companion_docx()
    unblinded = alloc.get("unblinded_allocation") or {}
    design = alloc.get("design") or {}
    block = alloc.get("block") or {}
    strat = alloc.get("stratification") or {}
    balance = alloc.get("balance") or {}
    blinding = alloc.get("blinding") or {}
    repro = alloc.get("reproducibility") or {}

    _companion_header_docx(
        doc.sections[0],
        "Randomization Schedule (Appendix)", f"Protocol Version: {protocol.version}.0",
        _protocol_code(protocol, drug_inn),
        f"Generated: {datetime.now(timezone.utc).strftime('%d-%b-%Y')}",
        banner=_RESTRICTED_BANNER,
    )
    _companion_footer_docx(
        doc.sections[0],
        note="Restricted appendix - filed in the trial master file, not in the protocol",
        banner=_RESTRICTED_BANNER,
    )

    _companion_cover_docx(
        doc,
        doc_type="Randomization Schedule - Unblinded Allocation List",
        subtitle=f"{drug_inn.title()} - {_cell(design.get('label'))}",
        rows=_randomization_cover_rows(protocol, drug_inn, alloc),
        banner=_RESTRICTED_BANNER,
    )

    warn = doc.add_paragraph()
    warn.paragraph_format.space_before = Pt(14)
    rw = warn.add_run(_cell(
        unblinded.get("warning"),
        "This is the unblinded randomization list. It must NOT appear in the "
        "protocol body, the synopsis, or any document circulated to "
        "investigators, recruiters or the bioanalytical laboratory.",
    ))
    rw.bold = True
    rw.font.name = "Times New Roman"
    rw.font.size = Pt(10.5)
    rw.font.color.rgb = _RESTRICTED_RGB

    holders = unblinded.get("holders") or []
    if holders:
        _ss_para(doc, "Authorised holders of this document:")
        for h in holders:
            doc.add_paragraph(str(h), style="List Bullet")

    # --- release / signature block ----------------------------------------
    doc.add_heading("Release and Custody Record", level=1)
    _ss_para(
        doc,
        "This appendix is released and filed under signature. Each signatory "
        "attests to the action described below; the attestations are recorded "
        "here so the list's chain of custody travels with the list itself.",
    )
    _ss_grid_table_docx(
        doc,
        [["Action", "Role", "Printed name", "Signature", "Date"]]
        + [[a, r, "", "", ""] for a, r, _why in _RANDOMIZATION_SIGNATORIES],
        col_widths=[Inches(1.0), Inches(1.85), Inches(1.5), Inches(1.5),
                    Inches(0.9)],
        font_pt=8.5,
    )
    _ss_grid_table_docx(
        doc,
        [["Action", "What the signature attests to"]]
        + [[a, why] for a, _r, why in _RANDOMIZATION_SIGNATORIES],
        col_widths=[Inches(1.2), Inches(5.55)], font_pt=8,
    )

    # --- method ------------------------------------------------------------
    doc.add_page_break()
    doc.add_heading("Method", level=1)
    if alloc.get("summary"):
        _ss_para(doc, _cell(alloc.get("summary")))
    if design.get("rationale"):
        _ss_para(doc, _cell(design.get("rationale")))
    method_rows = [
        ("Design", _cell(design.get("label"))),
        ("Variant", _cell(design.get("variant"))),
        ("Sequences", _cell(design.get("sequences"))),
        ("Treatments", _cell(design.get("treatments"))),
        ("Allocation ratio", _cell(design.get("allocation_ratio"))),
        ("Regulatory basis", _cell(design.get("regulatory_basis"))),
        ("Randomization method", _cell(block.get("method"))),
        ("Block detail (RESTRICTED)", _cell(block.get("method_detail"))),
        ("Blocks per stream", _cell(block.get("n_blocks_per_stream"))),
        ("Stratification", _cell(strat.get("method"))),
        ("Balance", _cell(balance.get("statement"))),
    ]
    _ss_kv_table_docx(doc, [(k, v) for k, v in method_rows if v])

    disclosure = block.get("disclosure") or {}
    if disclosure.get("basis"):
        _ss_para(doc, "Why the block length is withheld from the protocol body: "
                      + _cell(disclosure.get("basis")), italic=True, size=9)
    if disclosure.get("protocol_body_statement"):
        _ss_para(doc, "Wording that MAY appear in the protocol body: \""
                      + _cell(disclosure.get("protocol_body_statement")) + "\"",
                 italic=True, size=9)

    factors = strat.get("factors") or []
    if factors:
        _unstyled_subheading_docx(doc, "Stratification factors", size=11)
        _ss_grid_table_docx(
            doc,
            [["Factor", "Levels", "Rationale"]]
            + [[_cell(f.get("label"), _cell(f.get("key"))), _cell(f.get("levels")),
                _cell(f.get("rationale"))] for f in factors],
            col_widths=[Inches(1.4), Inches(1.8), Inches(3.55)], font_pt=8,
        )

    replacement = alloc.get("replacement") or {}
    if replacement.get("rule"):
        _unstyled_subheading_docx(doc, "Replacement of withdrawn subjects", size=11)
        _ss_para(doc, _cell(replacement.get("rule")), size=10)
        if replacement.get("note"):
            _ss_para(doc, _cell(replacement.get("note")), italic=True, size=9)

    # --- blinding posture --------------------------------------------------
    if blinding:
        _unstyled_subheading_docx(doc, "Blinding and allocation concealment", size=11)
        for key, title in (
            ("allocation_concealment", "Allocation concealment"),
            ("treatment_masking", "Treatment masking"),
            ("bioanalytical_blinding", "Bioanalytical blinding"),
            ("unblinding_procedure", "Code break"),
        ):
            block_ = blinding.get(key) or {}
            text = _cell(block_.get("statement")) or _cell(block_.get("rationale"))
            recommended = _cell(block_.get("recommended"))
            if recommended:
                text = f"Recommended posture: {recommended}. {text}"
            if text:
                _ss_para(doc, f"{title}: {text}", size=10)

    # --- the list ----------------------------------------------------------
    doc.add_page_break()
    doc.add_heading("Unblinded Allocation List", level=1)
    _ss_para(
        doc,
        "Every row below carries a treatment assignment. Reproduction of any row, "
        "in whole or in part, outside the authorised holders listed on the cover "
        "compromises the allocation concealment of this study.",
        italic=True, size=9,
    )
    rows = _allocation_rows(alloc)
    if len(rows) > 1:
        _ss_grid_table_docx(
            doc, rows,
            col_widths=[Inches(1.1), Inches(0.55), Inches(1.15), Inches(0.7),
                        Inches(0.4), Inches(0.8), Inches(1.35), Inches(0.7)],
            font_pt=7.5,
        )
    else:
        _ss_para(doc, "The stored schedule contains no allocation entries.")

    # --- disclosed conventions --------------------------------------------
    assumptions = alloc.get("assumptions") or []
    cautions = alloc.get("cautions") or []
    if assumptions or cautions:
        doc.add_page_break()
        doc.add_heading("Appendix: Disclosed Conventions and Cautions", level=1)
    if assumptions:
        _ss_grid_table_docx(
            doc,
            [["Convention", "Value", "Why"]]
            + [[_cell(a.get("name")), _cell(a.get("value")), _cell(a.get("why"))]
               for a in assumptions],
            col_widths=[Inches(1.6), Inches(0.7), Inches(4.45)], font_pt=8,
        )
    if cautions:
        _unstyled_subheading_docx(doc, "Cautions", size=11)
        _ss_grid_table_docx(
            doc,
            [["Caution", "May be disclosed in the protocol", "Detail"]]
            + [[_cell(c.get("key")), _cell(c.get("disclosable")), _cell(c.get("text"))]
               for c in cautions],
            col_widths=[Inches(1.4), Inches(1.1), Inches(4.25)], font_pt=8,
        )

    if repro.get("statement"):
        _ss_para(doc, "Reproducibility: " + _cell(repro.get("statement")),
                 italic=True, size=9)

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf.getvalue()


def _build_randomization_pdf(
    protocol: Protocol, drug_inn: str, alloc: dict[str, Any],
) -> bytes:
    """PDF parity for the restricted allocation appendix."""
    assert _RL_OK, "reportlab must be installed (pip install reportlab)"
    unblinded = alloc.get("unblinded_allocation") or {}
    design = alloc.get("design") or {}
    block = alloc.get("block") or {}
    strat = alloc.get("stratification") or {}
    balance = alloc.get("balance") or {}
    blinding = alloc.get("blinding") or {}
    repro = alloc.get("reproducibility") or {}

    buf = io.BytesIO()
    doc = _companion_pdf_doc(
        buf,
        title=f"RESTRICTED - Randomization Schedule - {drug_inn.title()}",
        subject="Unblinded allocation list - do not circulate",
        top_mm=44.0,
        bottom_mm=32.0,
    )
    canvasmaker = _make_companion_canvas(
        left1="Randomization Schedule (Appendix)",
        left2=_protocol_code(protocol, drug_inn),
        right1=f"Protocol Version: {protocol.version}.0",
        right2=f"Generated: {datetime.now(timezone.utc).strftime('%d-%b-%Y')}",
        footer_note=("Restricted appendix - filed in the trial master file, "
                     "not in the protocol"),
        banner=_RESTRICTED_BANNER,
    )

    styles = _pdf_styles()
    story: list[Any] = []
    _companion_cover_pdf(
        story, styles,
        doc_type="Randomization Schedule - Unblinded Allocation List",
        subtitle=f"{drug_inn.title()} - {_cell(design.get('label'))}",
        rows=_randomization_cover_rows(protocol, drug_inn, alloc),
        banner=_RESTRICTED_BANNER,
    )
    story.append(Paragraph(
        f'<font color="{_RESTRICTED_HEX}"><b>'
        + _pdf_safe(_cell(unblinded.get("warning"),
                          "This is the unblinded randomization list. It must NOT "
                          "appear in the protocol body or any document circulated "
                          "to investigators."))
        + "</b></font>", styles["Body"]))
    for h in unblinded.get("holders") or []:
        story.append(Paragraph("- " + _pdf_safe(str(h)), styles["Bullet"]))

    story.append(Paragraph("Release and Custody Record", styles["Heading1"]))
    _long_table_pdf(
        story,
        [["Action", "Role", "Printed name", "Signature", "Date"]]
        + [[_pdf_safe(a), _pdf_safe(r), "", "", ""]
           for a, r, _why in _RANDOMIZATION_SIGNATORIES],
        styles,
        col_widths=[24 * rl_mm, 44 * rl_mm, 34 * rl_mm, 34 * rl_mm, 24 * rl_mm],
        font_size=8.5,
    )
    _long_table_pdf(
        story,
        [["Action", "What the signature attests to"]]
        + [[_pdf_safe(a), _pdf_safe(why)] for a, _r, why in _RANDOMIZATION_SIGNATORIES],
        styles, col_widths=[28 * rl_mm, 132 * rl_mm], font_size=8,
    )

    story.append(PageBreak())
    story.append(Paragraph("Method", styles["Heading1"]))
    for text in (alloc.get("summary"), design.get("rationale")):
        if text:
            story.append(Paragraph(_pdf_safe(_cell(text)), styles["Body"]))
    method_rows = [
        ("Design", _cell(design.get("label"))),
        ("Variant", _cell(design.get("variant"))),
        ("Sequences", _cell(design.get("sequences"))),
        ("Treatments", _cell(design.get("treatments"))),
        ("Allocation ratio", _cell(design.get("allocation_ratio"))),
        ("Regulatory basis", _cell(design.get("regulatory_basis"))),
        ("Randomization method", _cell(block.get("method"))),
        ("Block detail (RESTRICTED)", _cell(block.get("method_detail"))),
        ("Stratification", _cell(strat.get("method"))),
        ("Balance", _cell(balance.get("statement"))),
    ]
    _long_table_pdf(
        story,
        [["Item", "Value"]]
        + [[_pdf_safe(k), _pdf_safe(v)] for k, v in method_rows if v],
        styles, col_widths=[45 * rl_mm, 115 * rl_mm], font_size=8.5,
    )

    factors = strat.get("factors") or []
    if factors:
        story.append(Paragraph("Stratification factors", styles["Heading2"]))
        _long_table_pdf(
            story,
            [["Factor", "Levels", "Rationale"]]
            + [[_pdf_safe(_cell(f.get("label"), _cell(f.get("key")))),
                _pdf_safe(_cell(f.get("levels"))),
                _pdf_safe(_cell(f.get("rationale")))] for f in factors],
            styles, col_widths=[32 * rl_mm, 40 * rl_mm, 88 * rl_mm], font_size=8,
        )

    replacement = alloc.get("replacement") or {}
    if replacement.get("rule"):
        story.append(Paragraph("Replacement of withdrawn subjects", styles["Heading2"]))
        story.append(Paragraph(_pdf_safe(_cell(replacement.get("rule"))), styles["Body"]))

    if blinding:
        story.append(Paragraph("Blinding and allocation concealment",
                               styles["Heading2"]))
        for key, title in (
            ("allocation_concealment", "Allocation concealment"),
            ("treatment_masking", "Treatment masking"),
            ("bioanalytical_blinding", "Bioanalytical blinding"),
            ("unblinding_procedure", "Code break"),
        ):
            blk = blinding.get(key) or {}
            text = _cell(blk.get("statement")) or _cell(blk.get("rationale"))
            recommended = _cell(blk.get("recommended"))
            if recommended:
                text = f"Recommended posture: {recommended}. {text}"
            if text:
                story.append(Paragraph(
                    f"<b>{_pdf_safe(title)}:</b> {_pdf_safe(text)}", styles["Body"]))

    story.append(PageBreak())
    story.append(Paragraph("Unblinded Allocation List", styles["Heading1"]))
    story.append(Paragraph(
        "<i>Every row below carries a treatment assignment. Reproduction of any "
        "row outside the authorised holders listed on the cover compromises the "
        "allocation concealment of this study.</i>", styles["Citation"]))
    rows = _allocation_rows(alloc)
    if len(rows) > 1:
        _long_table_pdf(
            story, [[_pdf_safe(c) for c in row] for row in rows], styles,
            col_widths=[27 * rl_mm, 13 * rl_mm, 27 * rl_mm, 17 * rl_mm,
                        10 * rl_mm, 19 * rl_mm, 31 * rl_mm, 16 * rl_mm],
            font_size=7,
        )
    else:
        story.append(Paragraph("The stored schedule contains no allocation entries.",
                               styles["Body"]))

    assumptions = alloc.get("assumptions") or []
    cautions = alloc.get("cautions") or []
    if assumptions or cautions:
        story.append(PageBreak())
        story.append(Paragraph("Appendix: Disclosed Conventions and Cautions",
                               styles["Heading1"]))
    if assumptions:
        _long_table_pdf(
            story,
            [["Convention", "Value", "Why"]]
            + [[_pdf_safe(_cell(a.get("name"))), _pdf_safe(_cell(a.get("value"))),
                _pdf_safe(_cell(a.get("why")))] for a in assumptions],
            styles, col_widths=[42 * rl_mm, 18 * rl_mm, 100 * rl_mm], font_size=8,
        )
    if cautions:
        story.append(Paragraph("Cautions", styles["Heading2"]))
        _long_table_pdf(
            story,
            [["Caution", "Disclosable", "Detail"]]
            + [[_pdf_safe(_cell(c.get("key"))), _pdf_safe(_cell(c.get("disclosable"))),
                _pdf_safe(_cell(c.get("text")))] for c in cautions],
            styles, col_widths=[34 * rl_mm, 22 * rl_mm, 104 * rl_mm], font_size=8,
        )

    if repro.get("statement"):
        story.append(Paragraph(
            "<i>Reproducibility: " + _pdf_safe(_cell(repro.get("statement")))
            + "</i>", styles["Citation"]))

    doc.build(story, canvasmaker=canvasmaker)
    buf.seek(0)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# CRF specification + Data Management Plan (operational companion document)
# ---------------------------------------------------------------------------


def _crf_cover_rows(
    protocol: Protocol, drug_inn: str, spec: dict[str, Any],
) -> list[tuple[str, str]]:
    standards = spec.get("standards") or {}
    dmp = spec.get("data_management_plan") or {}
    return [
        ("Protocol number", _protocol_code(protocol, drug_inn)),
        ("Protocol version", f"{protocol.version}.0"),
        ("Investigational product", drug_inn.title()),
        ("Study type", _cell(spec.get("study_type"), protocol.study_type.value)),
        ("Design", _cell(spec.get("design_key"))),
        ("Periods", _cell(spec.get("periods"))),
        ("Sequences", _cell(spec.get("sequences"))),
        ("Planned enrolment", _cell(spec.get("n_enrolled"))),
        ("Forms", _cell(spec.get("n_forms"))),
        ("Fields", _cell(spec.get("n_fields"))),
        ("Programmed edit checks", _cell(spec.get("n_edit_checks"))),
        ("SDTM domains", _cell(spec.get("sdtm_domains"))),
        ("Collection standard", _cell(standards.get("collection"))),
        ("Tabulation standard", _cell(standards.get("tabulation"))),
        ("Metadata standard", _cell(standards.get("metadata"))),
        ("Archival retention", _cell(dmp.get("retention_years")) + " years"
         if dmp.get("retention_years") else ""),
    ]


def _build_crf_dmp_docx(
    protocol: Protocol, drug_inn: str, spec: dict[str, Any],
) -> bytes:
    """Assemble the CRF specification and Data Management Plan as one .docx."""
    doc = _companion_docx()
    _companion_header_docx(
        doc.sections[0],
        "CRF Specification and Data Management Plan",
        f"Protocol Version: {protocol.version}.0",
        _protocol_code(protocol, drug_inn),
        f"Dated: {protocol.created_at.strftime('%d-%b-%Y')}",
    )
    _companion_footer_docx(
        doc.sections[0],
        note="Operational companion to the clinical study protocol",
    )

    _companion_cover_docx(
        doc,
        doc_type="Case Report Form Specification and Data Management Plan",
        subtitle=f"{drug_inn.title()} - {_cell(spec.get('design_key'))} design",
        rows=[(k, v) for k, v in _crf_cover_rows(protocol, drug_inn, spec) if v],
        notices=(
            "This document specifies what is collected and how the data are "
            "managed. It does not replace the protocol; where the two differ, the "
            "protocol governs and this specification must be corrected.",
        ),
    )
    doc.add_page_break()

    doc.add_heading("1 Summary", level=1)
    if spec.get("summary"):
        _ss_para(doc, _cell(spec.get("summary")))

    # --- 2 forms -----------------------------------------------------------
    forms = spec.get("forms") or []
    doc.add_heading("2 Case Report Forms", level=1)
    if forms:
        _ss_grid_table_docx(
            doc,
            [["Form", "SDTM", "Completed at", "Repeats", "Fields", "Critical"]]
            + [[_cell(f.get("title"), _cell(f.get("key"))),
                _cell(f.get("sdtm_domain")),
                _cell(f.get("completed_at")),
                _cell(f.get("repeats")) if f.get("repeating") else "Once",
                _cell(f.get("n_fields")),
                _cell(f.get("critical"))] for f in forms],
            col_widths=[Inches(1.85), Inches(0.55), Inches(2.0), Inches(0.85),
                        Inches(0.6), Inches(0.9)],
            font_pt=8,
        )
        conditional = [f for f in forms if f.get("conditional_on")]
        if conditional:
            _unstyled_subheading_docx(doc, "Why the conditional forms apply", size=11)
            _ss_grid_table_docx(
                doc,
                [["Form", "Condition", "Rationale"]]
                + [[_cell(f.get("title"), _cell(f.get("key"))),
                    _cell(f.get("conditional_on")),
                    _cell(f.get("condition_rationale"))] for f in conditional],
                col_widths=[Inches(1.6), Inches(1.3), Inches(3.85)], font_pt=8,
            )

    # --- 3 field definitions for the critical forms ------------------------
    doc.add_heading("3 Field Definitions (Critical Forms)", level=1)
    _ss_para(
        doc,
        "Field-level definitions are given for the forms whose data drive the "
        "primary analysis. The remaining forms follow the same conventions and "
        "are specified in full in the electronic CRF build specification.",
        italic=True, size=9,
    )
    critical = [f for f in forms if f.get("critical")]
    for f in critical:
        _unstyled_subheading_docx(
            doc, _cell(f.get("title"), _cell(f.get("key")))
            + f"  ({_cell(f.get('sdtm_domain'))})", size=11)
        if f.get("purpose"):
            _ss_para(doc, _cell(f.get("purpose")), italic=True, size=9)
        fields = f.get("fields") or []
        if not fields:
            continue
        _ss_grid_table_docx(
            doc,
            [["Field", "Label", "Type", "Units", "Permitted values", "Req.", "SDTM"]]
            + [[_cell(fl.get("name")), _cell(fl.get("label")), _cell(fl.get("dtype")),
                _cell(fl.get("units")),
                _cell(fl.get("permitted")) or _cell(fl.get("codelist")),
                _cell(fl.get("required")), _cell(fl.get("sdtm_variable"))]
               for fl in fields],
            col_widths=[Inches(1.05), Inches(1.4), Inches(0.65), Inches(0.55),
                        Inches(1.85), Inches(0.45), Inches(0.8)],
            font_pt=7.5,
        )

    # --- 4 visit schedule --------------------------------------------------
    visits = spec.get("visit_schedule") or []
    if visits:
        doc.add_page_break()
        doc.add_heading("4 Visit Structure and Forms per Visit", level=1)
        _ss_grid_table_docx(
            doc,
            [["Visit", "Timing", "Epoch", "Forms"]]
            + [[_cell(v.get("name")), _cell(v.get("timing")), _cell(v.get("epoch")),
                _cell(v.get("forms"))] for v in visits],
            col_widths=[Inches(1.5), Inches(1.7), Inches(1.2), Inches(2.35)],
            font_pt=7.5,
        )

    # --- 5 PK sample log ---------------------------------------------------
    pk_rows = spec.get("pk_sample_log_rows") or []
    doc.add_heading("5 Pharmacokinetic Sample Log (Pre-Printed Template)", level=1)
    if pk_rows:
        _ss_para(
            doc,
            "The log is pre-printed with the locked sampling schedule so a site "
            "selects a scheduled row rather than typing a nominal time. The actual "
            "clock time of every draw is recorded alongside it; the elapsed time "
            "used by the analysis is derived from the actual times, never assumed "
            "from the nominal ones.",
            size=10,
        )
        _ss_grid_table_docx(
            doc,
            [["#", "Nominal (h)", "Label", "Phase", "Window", "Volume (mL)",
              "Lambda-z", "Periods"]]
            + [[_cell(r.get("index")), _cell(r.get("nominal_time_h")),
                _cell(r.get("label")), _cell(r.get("phase")),
                _cell(r.get("window")), _cell(r.get("volume_ml")),
                _cell(r.get("lambda_z_point")), _cell(r.get("repeated_in_periods"))]
               for r in pk_rows],
            col_widths=[Inches(0.35), Inches(0.85), Inches(1.35), Inches(0.9),
                        Inches(1.1), Inches(0.85), Inches(0.65), Inches(0.7)],
            font_pt=7.5,
        )
    else:
        _ss_para(
            doc,
            "No computed sampling schedule was available, so the log could not be "
            "pre-printed. The timepoint rows must be generated from the locked "
            "sampling schedule before the database is built - a PK log with "
            "free-typed timepoints is how nominal-time-only data gets created.",
        )

    # --- 6 edit checks -----------------------------------------------------
    checks = spec.get("edit_checks") or []
    if checks:
        doc.add_page_break()
        doc.add_heading("6 Programmed Edit Checks", level=1)
        _ss_grid_table_docx(
            doc,
            [["Check", "Type", "Severity", "Rule", "Action"]]
            + [[_cell(c.get("key")), _cell(c.get("check_type")),
                _cell(c.get("severity")), _cell(c.get("rule")),
                _cell(c.get("action"))] for c in checks],
            col_widths=[Inches(1.15), Inches(0.8), Inches(0.65), Inches(2.5),
                        Inches(1.65)],
            font_pt=7.5,
        )

    # --- 7 data management plan -------------------------------------------
    dmp = spec.get("data_management_plan") or {}
    sections = dmp.get("sections") or []
    doc.add_page_break()
    doc.add_heading("7 Data Management Plan", level=1)
    for n, s in enumerate(sections, start=1):
        doc.add_heading(f"7.{n} {_cell(s.get('title'), _cell(s.get('key')))}", level=2)
        if s.get("content"):
            _ss_para(doc, _cell(s.get("content")))
        for d in s.get("deliverables") or []:
            doc.add_paragraph(str(d), style="List Bullet")
        meta = "; ".join(
            f"{k}: {_cell(s.get(k))}" for k in ("responsible", "timing", "reference")
            if s.get(k)
        )
        if meta:
            _ss_para(doc, meta, italic=True, size=9)

    coding = dmp.get("coding_dictionaries") or []
    if coding:
        doc.add_heading("7.C Coding Dictionaries", level=2)
        _ss_grid_table_docx(
            doc,
            [["Scope", "Dictionary", "Version policy", "Levels reported"]]
            + [[_cell(c.get("scope")), _cell(c.get("dictionary")),
                _cell(c.get("version_policy")), _cell(c.get("levels_reported"))]
               for c in coding],
            col_widths=[Inches(1.3), Inches(1.3), Inches(2.75), Inches(1.4)],
            font_pt=7.5,
        )

    # --- 8 analysis coverage + disclosures ---------------------------------
    coverage = spec.get("downstream_analysis_coverage") or {}
    doc.add_page_break()
    doc.add_heading("8 Downstream Analysis Coverage", level=1)
    if coverage.get("statement"):
        _ss_para(doc, _cell(coverage.get("statement")))
    uncovered = coverage.get("uncovered_required_columns") or []
    if uncovered:
        _ss_para(
            doc,
            "BLOCKING - the specification does not reach every column the analysis "
            "requires: " + ", ".join(str(c) for c in uncovered)
            + ". A study run on this specification could not be analysed without "
              "collecting those data some other way.",
        )

    assumptions = spec.get("assumptions") or []
    if assumptions:
        doc.add_heading("9 Disclosed Assumptions", level=1)
        _ss_grid_table_docx(
            doc,
            [["Assumption", "Value", "Basis"]]
            + [[_cell(a.get("name"), _cell(a.get("key"))), _cell(a.get("value")),
                _cell(a.get("why")) or _cell(a.get("basis"))] for a in assumptions],
            col_widths=[Inches(1.8), Inches(0.9), Inches(4.05)], font_pt=8,
        )

    flags = spec.get("flags") or []
    if flags:
        doc.add_heading("10 Flags Requiring Disclosure", level=1)
        _ss_grid_table_docx(
            doc,
            [["Flag", "Severity", "Must surface", "Message"]]
            + [[_cell(f.get("key")), _cell(f.get("severity")),
                _cell(f.get("must_surface_in_protocol")), _cell(f.get("message"))]
               for f in flags],
            col_widths=[Inches(1.3), Inches(0.75), Inches(0.85), Inches(3.85)],
            font_pt=8,
        )

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf.getvalue()


def _build_crf_dmp_pdf(
    protocol: Protocol, drug_inn: str, spec: dict[str, Any],
) -> bytes:
    """PDF parity for the CRF specification / Data Management Plan."""
    assert _RL_OK, "reportlab must be installed (pip install reportlab)"
    buf = io.BytesIO()
    doc = _companion_pdf_doc(
        buf,
        title=f"CRF Specification and Data Management Plan - {drug_inn.title()}",
        subject=f"Companion to protocol v{protocol.version}",
    )
    canvasmaker = _make_companion_canvas(
        left1="CRF Specification and Data Management Plan",
        left2=_protocol_code(protocol, drug_inn),
        right1=f"Protocol Version: {protocol.version}.0",
        right2=f"Dated: {protocol.created_at.strftime('%d-%b-%Y')}",
        footer_note="Operational companion to the clinical study protocol",
    )

    styles = _pdf_styles()
    story: list[Any] = []
    _companion_cover_pdf(
        story, styles,
        doc_type="Case Report Form Specification and Data Management Plan",
        subtitle=f"{drug_inn.title()} - {_cell(spec.get('design_key'))} design",
        rows=[(k, v) for k, v in _crf_cover_rows(protocol, drug_inn, spec) if v],
        notices=(
            "This document specifies what is collected and how the data are "
            "managed. It does not replace the protocol; where the two differ, the "
            "protocol governs.",
        ),
    )
    story.append(PageBreak())

    story.append(Paragraph("1  Summary", styles["Heading1"]))
    if spec.get("summary"):
        story.append(Paragraph(_pdf_safe(_cell(spec.get("summary"))), styles["Body"]))

    forms = spec.get("forms") or []
    story.append(Paragraph("2  Case Report Forms", styles["Heading1"]))
    if forms:
        _long_table_pdf(
            story,
            [["Form", "SDTM", "Completed at", "Repeats", "Fields", "Critical"]]
            + [[_pdf_safe(_cell(f.get("title"), _cell(f.get("key")))),
                _pdf_safe(_cell(f.get("sdtm_domain"))),
                _pdf_safe(_cell(f.get("completed_at"))),
                _pdf_safe(_cell(f.get("repeats")) if f.get("repeating") else "Once"),
                _pdf_safe(_cell(f.get("n_fields"))),
                _pdf_safe(_cell(f.get("critical")))] for f in forms],
            styles,
            col_widths=[44 * rl_mm, 14 * rl_mm, 48 * rl_mm, 20 * rl_mm,
                        14 * rl_mm, 20 * rl_mm],
            font_size=7.5,
        )

    story.append(Paragraph("3  Field Definitions (Critical Forms)", styles["Heading1"]))
    for f in [x for x in forms if x.get("critical")]:
        story.append(Paragraph(
            _pdf_safe(_cell(f.get("title"), _cell(f.get("key")))
                      + f"  ({_cell(f.get('sdtm_domain'))})"), styles["Heading2"]))
        fields = f.get("fields") or []
        if not fields:
            continue
        _long_table_pdf(
            story,
            [["Field", "Label", "Type", "Units", "Permitted values", "Req.", "SDTM"]]
            + [[_pdf_safe(_cell(fl.get("name"))), _pdf_safe(_cell(fl.get("label"))),
                _pdf_safe(_cell(fl.get("dtype"))), _pdf_safe(_cell(fl.get("units"))),
                _pdf_safe(_cell(fl.get("permitted")) or _cell(fl.get("codelist"))),
                _pdf_safe(_cell(fl.get("required"))),
                _pdf_safe(_cell(fl.get("sdtm_variable")))] for fl in fields],
            styles,
            col_widths=[25 * rl_mm, 33 * rl_mm, 15 * rl_mm, 13 * rl_mm,
                        45 * rl_mm, 10 * rl_mm, 19 * rl_mm],
            font_size=7,
        )

    visits = spec.get("visit_schedule") or []
    if visits:
        story.append(PageBreak())
        story.append(Paragraph("4  Visit Structure and Forms per Visit",
                               styles["Heading1"]))
        _long_table_pdf(
            story,
            [["Visit", "Timing", "Epoch", "Forms"]]
            + [[_pdf_safe(_cell(v.get("name"))), _pdf_safe(_cell(v.get("timing"))),
                _pdf_safe(_cell(v.get("epoch"))), _pdf_safe(_cell(v.get("forms")))]
               for v in visits],
            styles,
            col_widths=[34 * rl_mm, 40 * rl_mm, 28 * rl_mm, 58 * rl_mm],
            font_size=7,
        )

    pk_rows = spec.get("pk_sample_log_rows") or []
    story.append(Paragraph("5  Pharmacokinetic Sample Log (Pre-Printed Template)",
                           styles["Heading1"]))
    if pk_rows:
        _long_table_pdf(
            story,
            [["#", "Nominal (h)", "Label", "Phase", "Window", "Volume (mL)",
              "Lambda-z", "Periods"]]
            + [[_pdf_safe(_cell(r.get("index"))),
                _pdf_safe(_cell(r.get("nominal_time_h"))),
                _pdf_safe(_cell(r.get("label"))), _pdf_safe(_cell(r.get("phase"))),
                _pdf_safe(_cell(r.get("window"))),
                _pdf_safe(_cell(r.get("volume_ml"))),
                _pdf_safe(_cell(r.get("lambda_z_point"))),
                _pdf_safe(_cell(r.get("repeated_in_periods")))] for r in pk_rows],
            styles,
            col_widths=[8 * rl_mm, 20 * rl_mm, 34 * rl_mm, 22 * rl_mm,
                        26 * rl_mm, 20 * rl_mm, 16 * rl_mm, 14 * rl_mm],
            font_size=7,
        )
    else:
        story.append(Paragraph(
            "No computed sampling schedule was available, so the log could not be "
            "pre-printed. The timepoint rows must be generated from the locked "
            "sampling schedule before the database is built.", styles["Body"]))

    checks = spec.get("edit_checks") or []
    if checks:
        story.append(PageBreak())
        story.append(Paragraph("6  Programmed Edit Checks", styles["Heading1"]))
        _long_table_pdf(
            story,
            [["Check", "Type", "Severity", "Rule", "Action"]]
            + [[_pdf_safe(_cell(c.get("key"))), _pdf_safe(_cell(c.get("check_type"))),
                _pdf_safe(_cell(c.get("severity"))), _pdf_safe(_cell(c.get("rule"))),
                _pdf_safe(_cell(c.get("action")))] for c in checks],
            styles,
            col_widths=[28 * rl_mm, 20 * rl_mm, 16 * rl_mm, 58 * rl_mm, 38 * rl_mm],
            font_size=7,
        )

    dmp = spec.get("data_management_plan") or {}
    story.append(PageBreak())
    story.append(Paragraph("7  Data Management Plan", styles["Heading1"]))
    for n, s in enumerate(dmp.get("sections") or [], start=1):
        story.append(Paragraph(
            f"7.{n}  " + _pdf_safe(_cell(s.get("title"), _cell(s.get("key")))),
            styles["Heading2"]))
        if s.get("content"):
            story.append(Paragraph(_pdf_safe(_cell(s.get("content"))), styles["Body"]))
        for d in s.get("deliverables") or []:
            story.append(Paragraph("- " + _pdf_safe(str(d)), styles["Bullet"]))
        meta = "; ".join(
            f"{k}: {_cell(s.get(k))}" for k in ("responsible", "timing", "reference")
            if s.get(k)
        )
        if meta:
            story.append(Paragraph("<i>" + _pdf_safe(meta) + "</i>", styles["Citation"]))

    coding = dmp.get("coding_dictionaries") or []
    if coding:
        story.append(Paragraph("7.C  Coding Dictionaries", styles["Heading2"]))
        _long_table_pdf(
            story,
            [["Scope", "Dictionary", "Version policy", "Levels reported"]]
            + [[_pdf_safe(_cell(c.get("scope"))), _pdf_safe(_cell(c.get("dictionary"))),
                _pdf_safe(_cell(c.get("version_policy"))),
                _pdf_safe(_cell(c.get("levels_reported")))] for c in coding],
            styles,
            col_widths=[30 * rl_mm, 30 * rl_mm, 66 * rl_mm, 34 * rl_mm],
            font_size=7,
        )

    coverage = spec.get("downstream_analysis_coverage") or {}
    story.append(PageBreak())
    story.append(Paragraph("8  Downstream Analysis Coverage", styles["Heading1"]))
    if coverage.get("statement"):
        story.append(Paragraph(_pdf_safe(_cell(coverage.get("statement"))),
                               styles["Body"]))
    uncovered = coverage.get("uncovered_required_columns") or []
    if uncovered:
        story.append(Paragraph(
            "BLOCKING - the specification does not reach every column the analysis "
            "requires: " + _pdf_safe(", ".join(str(c) for c in uncovered)) + ".",
            styles["Body"]))

    assumptions = spec.get("assumptions") or []
    if assumptions:
        story.append(Paragraph("9  Disclosed Assumptions", styles["Heading1"]))
        _long_table_pdf(
            story,
            [["Assumption", "Value", "Basis"]]
            + [[_pdf_safe(_cell(a.get("name"), _cell(a.get("key")))),
                _pdf_safe(_cell(a.get("value"))),
                _pdf_safe(_cell(a.get("why")) or _cell(a.get("basis")))]
               for a in assumptions],
            styles, col_widths=[42 * rl_mm, 24 * rl_mm, 94 * rl_mm], font_size=7.5,
        )

    flags = spec.get("flags") or []
    if flags:
        story.append(Paragraph("10  Flags Requiring Disclosure", styles["Heading1"]))
        _long_table_pdf(
            story,
            [["Flag", "Severity", "Must surface", "Message"]]
            + [[_pdf_safe(_cell(f.get("key"))), _pdf_safe(_cell(f.get("severity"))),
                _pdf_safe(_cell(f.get("must_surface_in_protocol"))),
                _pdf_safe(_cell(f.get("message")))] for f in flags],
            styles,
            col_widths=[30 * rl_mm, 18 * rl_mm, 20 * rl_mm, 92 * rl_mm],
            font_size=7.5,
        )

    doc.build(story, canvasmaker=canvasmaker)
    buf.seek(0)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Companion-document routes
# ---------------------------------------------------------------------------
# Same auth posture as the protocol export: require_user gates the route,
# authenticate_actor resolves the caller, and get_protocol_scoped enforces the
# org boundary before a single byte is rendered.

_DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


def _inn_slug(drug_inn: str) -> str:
    return (drug_inn or "product").replace(" ", "_")


@router.post("/icf/docx", dependencies=[Depends(require_user)])
async def export_icf_docx(
    payload: ExportRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Export the Informed Consent Form as its own .docx.

    Separate from the protocol export on purpose: the ICF is reviewed and
    approved by the IRB on its own cycle and carries its own version and date.
    """
    actor = await authenticate_actor(request, db)
    protocol = await get_protocol_scoped(db, payload.protocol_id, actor, load_drug=True)
    drug_inn = protocol.drug.inn

    icf = _resolve_icf(protocol, drug_inn)
    docx_bytes = _build_icf_docx(protocol, drug_inn, icf)

    icf_version = _cell((icf.get("version") or {}).get("icf_version"), "1.0")
    filename = f"icf_{_inn_slug(drug_inn)}_v{icf_version}.docx"

    await _log_companion_export(
        db, actor, payload.protocol_id, "EXPORT:icf_docx",
        {
            "format": "docx",
            "document": "informed_consent_form",
            "filename": filename,
            "icf_version": icf_version,
            "protocol_version": protocol.version,
            "missing_required_elements": icf.get("missing_required_elements") or [],
            "pending_fields": [
                _cell(p.get("field")) for p in (icf.get("pending_fields") or [])
            ],
        },
    )
    return StreamingResponse(
        io.BytesIO(docx_bytes),
        media_type=_DOCX_MEDIA_TYPE,
        headers={"Content-Disposition": _content_disposition(filename)},
    )


@router.post("/icf/pdf", dependencies=[Depends(require_user)])
async def export_icf_pdf(
    payload: ExportRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Export the Informed Consent Form as its own PDF."""
    if not _RL_OK:
        raise HTTPException(
            501,
            detail="PDF export requires the 'reportlab' package. Run: pip install reportlab",
        )
    actor = await authenticate_actor(request, db)
    protocol = await get_protocol_scoped(db, payload.protocol_id, actor, load_drug=True)
    drug_inn = protocol.drug.inn

    icf = _resolve_icf(protocol, drug_inn)
    pdf_bytes = _build_icf_pdf(protocol, drug_inn, icf)

    icf_version = _cell((icf.get("version") or {}).get("icf_version"), "1.0")
    filename = f"icf_{_inn_slug(drug_inn)}_v{icf_version}.pdf"

    await _log_companion_export(
        db, actor, payload.protocol_id, "EXPORT:icf_pdf",
        {
            "format": "pdf",
            "document": "informed_consent_form",
            "filename": filename,
            "icf_version": icf_version,
            "protocol_version": protocol.version,
            "missing_required_elements": icf.get("missing_required_elements") or [],
        },
    )
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": _content_disposition(filename)},
    )


@router.post("/randomization/docx", dependencies=[Depends(require_user)])
async def export_randomization_docx(
    payload: ExportRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Export the UNBLINDED randomization allocation list as a restricted appendix.

    This is the only route that emits per-subject treatment assignments. It is
    intentionally a separate document with its own banner on every page: the
    list must never be bound into the protocol, whose readers include the
    investigators whose enrolment decisions the concealment protects.
    """
    actor = await authenticate_actor(request, db)
    protocol = await get_protocol_scoped(db, payload.protocol_id, actor, load_drug=True)
    drug_inn = protocol.drug.inn

    alloc = _resolve_randomization(protocol)
    docx_bytes = _build_randomization_docx(protocol, drug_inn, alloc)

    filename = (
        f"RESTRICTED_randomization_list_{_inn_slug(drug_inn)}_v{protocol.version}.docx"
    )
    await _log_companion_export(
        db, actor, payload.protocol_id, "EXPORT:randomization_docx",
        {
            "format": "docx",
            "document": "unblinded_randomization_list",
            "confidentiality": "RESTRICTED",
            "contains_treatment_assignments": True,
            "filename": filename,
            "n_entries": (alloc.get("unblinded_allocation") or {}).get("n_entries"),
            "engine_version": alloc.get("engine_version"),
            "protocol_version": protocol.version,
        },
    )
    return StreamingResponse(
        io.BytesIO(docx_bytes),
        media_type=_DOCX_MEDIA_TYPE,
        headers={"Content-Disposition": _content_disposition(filename)},
    )


@router.post("/randomization/pdf", dependencies=[Depends(require_user)])
async def export_randomization_pdf(
    payload: ExportRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Export the UNBLINDED randomization allocation list as a restricted PDF."""
    if not _RL_OK:
        raise HTTPException(
            501,
            detail="PDF export requires the 'reportlab' package. Run: pip install reportlab",
        )
    actor = await authenticate_actor(request, db)
    protocol = await get_protocol_scoped(db, payload.protocol_id, actor, load_drug=True)
    drug_inn = protocol.drug.inn

    alloc = _resolve_randomization(protocol)
    pdf_bytes = _build_randomization_pdf(protocol, drug_inn, alloc)

    filename = (
        f"RESTRICTED_randomization_list_{_inn_slug(drug_inn)}_v{protocol.version}.pdf"
    )
    await _log_companion_export(
        db, actor, payload.protocol_id, "EXPORT:randomization_pdf",
        {
            "format": "pdf",
            "document": "unblinded_randomization_list",
            "confidentiality": "RESTRICTED",
            "contains_treatment_assignments": True,
            "filename": filename,
            "n_entries": (alloc.get("unblinded_allocation") or {}).get("n_entries"),
            "engine_version": alloc.get("engine_version"),
            "protocol_version": protocol.version,
        },
    )
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": _content_disposition(filename)},
    )


@router.post("/randomization/csv", dependencies=[Depends(require_user)])
async def export_randomization_csv(
    payload: ExportRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Export the UNBLINDED allocation list as the engine's restricted CSV.

    The machine-readable companion to the signed appendix: this is the form the
    dispensing pharmacy loads. Its own header lines carry the RESTRICTED banner,
    so the confidentiality survives the file being opened in a spreadsheet.
    """
    if not _RANDOMIZATION_ENGINE_OK:
        raise HTTPException(
            501,
            detail="Randomization CSV export is unavailable "
                   "(services/randomization.py could not be imported).",
        )
    actor = await authenticate_actor(request, db)
    protocol = await get_protocol_scoped(db, payload.protocol_id, actor, load_drug=True)
    drug_inn = protocol.drug.inn

    alloc = _resolve_randomization(protocol)
    csv_text = _format_unblinded_list(alloc) or ""

    filename = (
        f"RESTRICTED_randomization_list_{_inn_slug(drug_inn)}_v{protocol.version}.csv"
    )
    await _log_companion_export(
        db, actor, payload.protocol_id, "EXPORT:randomization_csv",
        {
            "format": "csv",
            "document": "unblinded_randomization_list",
            "confidentiality": "RESTRICTED",
            "contains_treatment_assignments": True,
            "filename": filename,
            "n_entries": (alloc.get("unblinded_allocation") or {}).get("n_entries"),
            "protocol_version": protocol.version,
        },
    )
    return StreamingResponse(
        io.BytesIO(csv_text.encode("ascii", errors="replace")),
        media_type="text/csv; charset=us-ascii",
        headers={"Content-Disposition": _content_disposition(filename)},
    )


@router.post("/crf-dmp/docx", dependencies=[Depends(require_user)])
async def export_crf_dmp_docx(
    payload: ExportRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Export the CRF specification and Data Management Plan as its own .docx."""
    actor = await authenticate_actor(request, db)
    protocol = await get_protocol_scoped(db, payload.protocol_id, actor, load_drug=True)
    drug_inn = protocol.drug.inn

    spec = _resolve_crf_dmp(protocol, drug_inn)
    docx_bytes = _build_crf_dmp_docx(protocol, drug_inn, spec)

    filename = f"crf_dmp_{_inn_slug(drug_inn)}_v{protocol.version}.docx"
    await _log_companion_export(
        db, actor, payload.protocol_id, "EXPORT:crf_dmp_docx",
        {
            "format": "docx",
            "document": "crf_specification_and_dmp",
            "filename": filename,
            "n_forms": spec.get("n_forms"),
            "n_fields": spec.get("n_fields"),
            "n_edit_checks": spec.get("n_edit_checks"),
            "must_disclose": spec.get("must_disclose"),
            "protocol_version": protocol.version,
        },
    )
    return StreamingResponse(
        io.BytesIO(docx_bytes),
        media_type=_DOCX_MEDIA_TYPE,
        headers={"Content-Disposition": _content_disposition(filename)},
    )


@router.post("/crf-dmp/pdf", dependencies=[Depends(require_user)])
async def export_crf_dmp_pdf(
    payload: ExportRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Export the CRF specification and Data Management Plan as its own PDF."""
    if not _RL_OK:
        raise HTTPException(
            501,
            detail="PDF export requires the 'reportlab' package. Run: pip install reportlab",
        )
    actor = await authenticate_actor(request, db)
    protocol = await get_protocol_scoped(db, payload.protocol_id, actor, load_drug=True)
    drug_inn = protocol.drug.inn

    spec = _resolve_crf_dmp(protocol, drug_inn)
    pdf_bytes = _build_crf_dmp_pdf(protocol, drug_inn, spec)

    filename = f"crf_dmp_{_inn_slug(drug_inn)}_v{protocol.version}.pdf"
    await _log_companion_export(
        db, actor, payload.protocol_id, "EXPORT:crf_dmp_pdf",
        {
            "format": "pdf",
            "document": "crf_specification_and_dmp",
            "filename": filename,
            "n_forms": spec.get("n_forms"),
            "n_fields": spec.get("n_fields"),
            "n_edit_checks": spec.get("n_edit_checks"),
            "must_disclose": spec.get("must_disclose"),
            "protocol_version": protocol.version,
        },
    )
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": _content_disposition(filename)},
    )
