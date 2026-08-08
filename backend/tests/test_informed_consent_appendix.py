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
from types import SimpleNamespace

import docx
import pytest

from routers.export import (
    _add_informed_consent_appendix_docx,
    _section_number_map,
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


# ---------------------------------------------------------------------------
# _split_informed_consent — <h3>-level and MIXED <h2>/<h3> Ethics subsections
#
# The per-section generation path (services/ai_service.py) writes Ethics
# subsections as <h2>; the whole-document path (services/three_stage_pipeline.py)
# writes them as <h3>. Before the first fix here, _split_informed_consent only
# ever matched <h2>, so a document generated via the whole-document path
# silently never relocated its consent subsection at all - not an error, just
# a no-op, so the gap was invisible until someone diffed the two paths' output.
#
# An h2-first/h3-fallback design (the first attempt at fixing that) turned out
# to have its own gap, found by adversarial review: the whole-document path's
# OWN prompt was internally contradictory for the "ethics" field specifically
# (its generic per-field schema note said "<h3> as needed" while the reused
# ai_service.py instructions said "<h2> headings"), so a real generation could
# plausibly emit a genuine MIX of both levels in one Ethics section. An
# h2-first/h3-fallback matcher never looks at <h3> once any <h2> match exists,
# so a trailing <h3> sibling after the matched <h2> consent heading was
# invisible as a boundary and got silently absorbed into the consent block.
# The fix (now tested below) treats <h2> and <h3> as equally candidate
# boundaries in ONE pass, so a heading of either level always terminates the
# preceding one. The schema contradiction that made the mix plausible in the
# first place was also fixed directly (three_stage_pipeline.py's ethics field
# now explicitly says <h2>), but the parser fix stands on its own regardless.
# ---------------------------------------------------------------------------


def test_split_supports_h3_level_ethics_subsections():
    html = (
        "<h3>Ethical Principles, Laws and Regulations</h3>"
        "<p>Declaration of Helsinki and ICH E6(R2).</p>"
        "<h3>Informed Consent Process</h3>"
        "<p>Written consent per 21 CFR 50.25 before any study procedure.</p>"
        "<h3>Participant and Data Confidentiality</h3>"
        "<p>Coded subject identifiers only.</p>"
    )
    body, consent = _split_informed_consent(html)
    consent_text = _strip_html(consent)
    body_text = _strip_html(body)
    assert "Informed Consent Process" in consent_text
    assert "21 CFR 50.25" in consent_text
    assert "Coded subject identifiers" not in consent_text
    assert "Informed Consent Process" not in body_text
    assert "Coded subject identifiers" in body_text


def test_split_handles_a_genuine_mix_of_h2_and_h3_in_one_document():
    """THE CONFIRMED FIX: a document with a genuine mix of heading levels
    (which the whole-document path's own prompt could plausibly produce for
    "ethics" before its schema contradiction was also fixed) must still stop
    the consent block at the very next heading, whatever level it is - not
    swallow a trailing subsection at the OTHER level because the matcher was
    only looking at one level. Adversarially confirmed against the previous
    h2-first/h3-fallback implementation: that version DID swallow the trailing
    <h3> "Participant and Data Confidentiality" subsection into the consent
    appendix here, because once the <h2> pass found matches it never
    consulted <h3> at all, so the trailing <h3> was invisible as a boundary."""
    html = (
        "<h2>Ethical Principles, Laws and Regulations</h2>"
        "<p>Declaration of Helsinki.</p>"
        "<h3>Ethics Committee / Institutional Review Board</h3>"
        "<p>IRB approval required before enrolment.</p>"
        "<h2>Informed Consent Process</h2>"
        "<p>Written consent obtained before any procedure.</p>"
        "<h3>Participant and Data Confidentiality</h3>"
        "<p>Coded subject identifiers, no PHI.</p>"
    )
    body, consent = _split_informed_consent(html)
    body_text = _strip_html(body)
    consent_text = _strip_html(consent)

    # The consent block carries ONLY its own heading and paragraph...
    assert "Informed Consent Process" in consent_text
    assert "Written consent obtained" in consent_text
    # ...and must NOT carry the trailing, unrelated Confidentiality subsection.
    assert "Participant and Data Confidentiality" not in consent_text
    assert "Coded subject identifiers" not in consent_text

    # The body keeps everything else, including the trailing subsection that
    # comes AFTER consent at a different heading level.
    assert "Ethical Principles" in body_text
    assert "Ethics Committee" in body_text
    assert "Participant and Data Confidentiality" in body_text
    assert "Coded subject identifiers" in body_text
    assert "Informed Consent Process" not in body_text


def test_appendix_strips_an_h3_level_consent_heading_too():
    doc = docx.Document()
    _, consent = _split_informed_consent(
        "<h3>Informed Consent Process</h3><p>21 CFR 50.25 details.</p>"
    )
    _add_informed_consent_appendix_docx(doc, consent)
    all_text = "\n".join(p.text for p in doc.paragraphs)
    assert all_text.count("Informed Consent Process") == 1
    assert "21 CFR 50.25 details" in all_text


# ---------------------------------------------------------------------------
# _split_informed_consent — defensive bound against malformed HTML
#
# The boundary regex used to be `<h2[^>]*>(?P<title>.*?)</h2>` with DOTALL and
# an UNBOUNDED lazy title match. If some earlier <h2> in the section was
# missing its closing tag (malformed AI output), that lazy match would run
# forward across everything - including the real consent heading and its own
# closing tag - and be captured as a single, garbled "heading" starting at
# the MALFORMED tag's position. Because the swallowed text still contained
# "informed consent" somewhere inside it, the code identified that garbled
# match as THE consent section and relocated everything from the malformed
# heading onward into the appendix, leaving the real subsections in between
# stripped from the body. Bounding the title match to 300 characters (far
# more than any real heading needs) means a heading with no closing tag
# nearby simply fails to match at all, rather than swallowing the rest of
# the document.
# ---------------------------------------------------------------------------


def test_split_ignores_a_malformed_unclosed_heading_defensively():
    filler = "Filler regulatory background text repeated many times over. " * 20
    html = (
        "<h2>Ethical Principles"  # missing closing </h2> - malformed
        f"<p>{filler}</p>"        # pushes well past the 300-char title bound
        "<h2>Informed Consent Process</h2>"
        "<p>Written consent per 21 CFR 50.25 before any study procedure.</p>"
        "<h2>Participant and Data Confidentiality</h2>"
        "<p>Coded subject identifiers only.</p>"
    )
    body, consent = _split_informed_consent(html)
    consent_text = _strip_html(consent)
    body_text = _strip_html(body)

    # The consent block must start at the REAL heading, not get fused with
    # the malformed one several hundred characters earlier.
    assert consent_text.startswith("Informed Consent Process")
    assert "21 CFR 50.25" in consent_text
    assert "Coded subject identifiers" not in consent_text
    # The malformed heading's own content and the following subsection both
    # stay in the body rather than being swept into the appendix.
    assert "Ethical Principles" in body_text
    assert "Coded subject identifiers" in body_text


# ---------------------------------------------------------------------------
# _section_number_map — must mirror the body loop's ethics/consent-split
#
# Confirmed, previously-shipped bug: this function checked the section's
# UNSPLIT raw text for emptiness, while the body loop it claims to "mirror"
# checks the text AFTER _split_informed_consent removes the consent block.
# When Ethics has no other content, the body loop correctly skips numbering
# and bookmarking it, but this function still assigned it a number -
# _add_amendment_appendix_docx/_pdf then gate a PAGEREF field on
# `section_key in num_map` (now true for a section with no bookmark to point
# at), producing a dangling "Error! Bookmark not defined." in the amendment
# history of what is meant to be the regulated change-control record.
# ---------------------------------------------------------------------------


def test_section_number_map_excludes_ethics_when_only_consent_remains():
    protocol = SimpleNamespace(sections={
        "introduction": "<p>Intro text.</p>",
        "ethics": "<h2>Informed Consent Process</h2><p>Consent text.</p>",
    })
    num_map = _section_number_map(protocol)
    assert "ethics" not in num_map
    assert num_map["introduction"] == (1, "Introduction")


def test_section_number_map_numbers_ethics_when_other_content_remains():
    """Non-regression: Ethics WITH other subsections besides consent is still
    numbered normally - only a consent-only Ethics section is excluded."""
    protocol = SimpleNamespace(sections={
        "introduction": "<p>Intro text.</p>",
        "ethics": (
            "<h2>Ethical Principles</h2><p>Declaration of Helsinki.</p>"
            "<h2>Informed Consent Process</h2><p>Consent text.</p>"
        ),
    })
    num_map = _section_number_map(protocol)
    assert "ethics" in num_map
    assert num_map["ethics"] == (2, "Ethical and Regulatory Standards")


def test_section_number_map_skips_a_section_that_is_purely_a_tost_dump():
    """The same class of bug, generalised: _strip_tost_dump can also empty a
    section entirely (e.g. a stray reproduced PowerTOST console banner with
    no other content). _section_number_map must apply that transform too,
    exactly as the body loop does, before deciding whether to number it."""
    protocol = SimpleNamespace(sections={
        "introduction": "<p>Intro text.</p>",
        "statistical_analysis_plan": (
            "<pre>Equivalence test\nSample size (total) = 24\n</pre>"
        ),
    })
    num_map = _section_number_map(protocol)
    assert "statistical_analysis_plan" not in num_map
    assert num_map["introduction"] == (1, "Introduction")
