"""Tests for relocating the informed-consent process out of the protocol body.

A practising reviewer asked for the consent material to come OUT of the main
protocol and travel as an appendix instead. The generator still writes it as
one <h2> block inside the Ethics section (that is where it has the `icf`
locked facts in context), so the move is a RENDER-TIME relocation in
routers/export.py: the Ethics section renders without it, and it reappears as
"Appendix: Informed Consent Process".

The contract pinned below is two-sided - the block must be ABSENT from the
numbered body and PRESENT as an appendix - and it must not cost the document
a section: the numbered body keeps exactly the sections it had.
"""
from __future__ import annotations

import re

import docx
import pytest

from routers.export import (
    _add_informed_consent_appendix_docx,
    _split_informed_consent,
    _strip_html,
)


_ETHICS_HTML = (
    "<h2>Ethical Principles, Laws and Regulations</h2>"
    "<p>Declaration of Helsinki and ICH E6(R2).</p>"
    "<h2>Ethics Committee / Institutional Review Board</h2>"
    "<p>Written approval before enrolment.</p>"
    "<h2>Informed Consent Process</h2>"
    "<p>Written consent per 21 CFR 50.25 before any study procedure.</p>"
    "<p>The ICF is a separate standalone document.</p>"
    "<h2>Participant and Data Confidentiality</h2>"
    "<p>Coded subject identifiers only.</p>"
)


def _h2s(html: str) -> list[str]:
    return [_strip_html(t) for t in re.findall(r"<h2[^>]*>(.*?)</h2>", html, re.I | re.S)]


# ---------------------------------------------------------------------------
# _split_informed_consent
# ---------------------------------------------------------------------------


def test_split_removes_consent_from_body_and_returns_it():
    body, consent = _split_informed_consent(_ETHICS_HTML)

    assert _h2s(body) == [
        "Ethical Principles, Laws and Regulations",
        "Ethics Committee / Institutional Review Board",
        "Participant and Data Confidentiality",
    ]
    assert _h2s(consent) == ["Informed Consent Process"]


def test_split_carries_every_paragraph_under_the_heading():
    """The block runs to the NEXT <h2>, so a multi-paragraph consent
    subsection travels whole rather than losing its tail to the body."""
    _, consent = _split_informed_consent(_ETHICS_HTML)
    text = _strip_html(consent)
    assert "21 CFR 50.25" in text
    assert "separate standalone document" in text
    # ...and the following subsection must NOT be swept in with it.
    assert "Coded subject identifiers" not in text


def test_split_does_not_leak_consent_text_into_the_body():
    body, _ = _split_informed_consent(_ETHICS_HTML)
    body_text = _strip_html(body).lower()
    assert "informed consent process" not in body_text
    assert "21 cfr 50.25" not in body_text
    # The surrounding subsections must survive intact.
    assert "declaration of helsinki" in body_text
    assert "coded subject identifiers" in body_text


def test_split_matches_on_heading_text_not_position():
    """Matching by text means a generator that reorders the subsections still
    relocates the right one."""
    reordered = (
        "<h2>Informed Consent Process</h2><p>Consent first.</p>"
        "<h2>Participant and Data Confidentiality</h2><p>Coded IDs.</p>"
    )
    body, consent = _split_informed_consent(reordered)
    assert _h2s(consent) == ["Informed Consent Process"]
    assert _h2s(body) == ["Participant and Data Confidentiality"]


@pytest.mark.parametrize("heading", [
    "Informed Consent Process",
    "INFORMED CONSENT PROCESS",
    "Informed Consent",
    "Consent Process",
])
def test_split_tolerates_heading_wording_and_case_variants(heading):
    html = f"<h2>{heading}</h2><p>Body.</p><h2>Other</h2><p>Keep.</p>"
    body, consent = _split_informed_consent(html)
    assert _h2s(consent) == [heading]
    assert _h2s(body) == ["Other"]


def test_split_is_a_no_op_when_there_is_no_consent_subsection():
    html = "<h2>Ethical Principles</h2><p>Only this.</p>"
    body, consent = _split_informed_consent(html)
    assert body == html
    assert consent == ""


@pytest.mark.parametrize("html", ["", "   ", "<p>No headings at all.</p>"])
def test_split_handles_empty_and_headingless_input(html):
    body, consent = _split_informed_consent(html)
    assert body == html
    assert consent == ""


# ---------------------------------------------------------------------------
# DOCX appendix rendering
# ---------------------------------------------------------------------------


def test_appendix_renders_with_its_own_heading_and_no_duplicate_title():
    doc = docx.Document()
    _, consent = _split_informed_consent(_ETHICS_HTML)
    _add_informed_consent_appendix_docx(doc, consent)

    h1 = [p.text.strip() for p in doc.paragraphs if p.style.name == "Heading 1"]
    assert h1 == ["Appendix: Informed Consent Process"]

    # The block's own <h2> is dropped in favour of the appendix heading, so
    # the title must not appear twice.
    all_text = "\n".join(p.text for p in doc.paragraphs)
    assert all_text.count("Informed Consent Process") == 1
    assert "21 CFR 50.25" in all_text


def test_appendix_is_omitted_when_there_is_nothing_to_relocate():
    for empty in ("", "   ", "<h2>Informed Consent Process</h2>"):
        doc = docx.Document()
        before = len(doc.paragraphs)
        _add_informed_consent_appendix_docx(doc, empty)
        # A heading with no body beneath it must not produce a bare appendix.
        assert len(doc.paragraphs) == before


def test_appendix_states_the_icf_is_a_separate_document():
    doc = docx.Document()
    _, consent = _split_informed_consent(_ETHICS_HTML)
    _add_informed_consent_appendix_docx(doc, consent)
    all_text = " ".join(p.text for p in doc.paragraphs)
    assert "separate, standalone document" in all_text
