"""Tests for the standalone Informed Consent Form engine.

The ICF is a legal document, not a chapter of the protocol. These tests pin the
things that get a submission bounced or, worse, that mislead a subject:

* every basic 21 CFR 50.25(a) element is actually discharged by a section;
* a healthy volunteer is told plainly that they get NO medical benefit;
* the statutory 50.25(c) wording survives verbatim;
* the study-specific numbers are the SAME numbers the protocol computed, so the
  two documents cannot contradict each other;
* the form cannot contradict ITSELF (an injected product is not told to fast,
  and the stated blood volume is not compared away as "less than a donation"
  when it is more).

A smoke test would pass on a form that quietly promises a healthy volunteer a
therapeutic benefit. These are written to fail on that.
"""
from __future__ import annotations

import json

import pytest

from services.biostatistics import WASHOUT_HALF_LIVES
from services.icf import (
    BASIC_ELEMENT_CODES,
    CLINICALTRIALS_GOV_STATEMENT,
    _ELEMENTS,
    _RISK_CATALOGUE,
    _restriction_catalogue,
    build_icf,
    icf_directive,
    readability_grade,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _section(icf: dict, key: str) -> dict:
    """The one section with this key, or fail loudly."""
    matches = [s for s in icf["sections"] if s["key"] == key]
    assert matches, f"no section keyed {key}"
    return matches[0]


def _section_text(icf: dict, key: str) -> str:
    s = _section(icf, key)
    return " ".join(list(s["body"]) + list(s["bullets"])).lower()


def _all_text(icf: dict) -> str:
    return " ".join(
        " ".join(list(s["body"]) + list(s["bullets"])) for s in icf["sections"]
    ).lower()


def _be_context(**overrides):
    """A conventional healthy-volunteer oral BE study."""
    ctx = {
        "drug_inn": "metformin",
        "study_type": "BE",
        "sample_size": {
            "design": "2-period, 2-sequence crossover",
            "design_key": "crossover",
            "t_half_hours": 6.2,
            "n_enrolled": 48,
        },
    }
    ctx.update(overrides)
    return ctx


_SC_LABEL = {
    "description": "Adalimumab-xxxx injection, for subcutaneous use. Supplied in a prefilled syringe.",
    "adverse_reactions": "Injection site reactions, serious infection, and immunogenicity with anti-drug antibodies.",
}


# ---------------------------------------------------------------------------
# Catalogue integrity
# ---------------------------------------------------------------------------


def test_element_catalogue_covers_the_whole_statute():
    """All eight basic elements of 21 CFR 50.25(a), the six additional elements
    of 50.25(b), and the 50.25(c) clause must be in the catalogue."""
    codes = [e.code for e in _ELEMENTS]
    assert len(codes) == len(set(codes)), "duplicate element codes"

    for n in range(1, 9):
        assert f"50.25(a)({n})" in codes, f"missing basic element 50.25(a)({n})"
    for n in range(1, 7):
        assert f"50.25(b)({n})" in codes, f"missing additional element 50.25(b)({n})"
    assert "50.25(c)" in codes

    assert len(BASIC_ELEMENT_CODES) == 8
    for e in _ELEMENTS:
        assert e.tier in ("basic", "additional", "clinicaltrials")
        assert e.requirement and e.plain_language_hint
        # The requirement is quoted to a reviewer, so it must be a real
        # statement of the rule rather than a stub.
        assert len(e.requirement) > 40, f"{e.code} requirement too thin"


def test_risk_and_restriction_catalogues_are_well_formed():
    keys = [r.key for r in _RISK_CATALOGUE]
    assert len(keys) == len(set(keys)), "duplicate risk keys"
    for r in _RISK_CATALOGUE:
        assert r.category in ("procedure", "drug", "reproductive", "unknown")
        assert r.likelihood in ("common", "uncommon", "rare", "unknown")
        assert r.plain_language and r.mitigation and r.evidence and r.applies_when

    rkeys = [x.key for x in _restriction_catalogue()]
    assert len(rkeys) == len(set(rkeys)), "duplicate restriction keys"
    for x in _restriction_catalogue():
        assert x.plain_language and x.window and x.rationale
        # A restriction with no stated reason reads as arbitrary and gets
        # ignored by subjects; the rationale is what earns compliance.
        assert len(x.rationale) > 30, f"{x.key} rationale too thin"


# ---------------------------------------------------------------------------
# Statutory coverage - the reason the engine exists
# ---------------------------------------------------------------------------


def test_every_basic_element_is_discharged_by_a_section():
    icf = build_icf(_be_context())
    assert icf["missing_required_elements"] == [], (
        "a missing basic 50.25(a) element is an administrative rejection")
    for code in BASIC_ELEMENT_CODES:
        entry = icf["elements_coverage"][code]
        assert entry["covered"] is True
        assert entry["sections"], f"{code} claims coverage but names no section"


def test_coverage_holds_for_patients_and_for_injected_biosimilars():
    """Coverage must not depend on the happy path - the element set is the same
    whatever the population or the route."""
    patient = build_icf(_be_context(
        population_config={"subject_type": "patient"}))
    assert patient["missing_required_elements"] == []

    biosim = build_icf({
        "drug_inn": "adalimumab",
        "study_type": "biosimilar_pk",
        "label_data": _SC_LABEL,
        "sample_size": {"design": "parallel-group", "design_key": "parallel",
                        "n_enrolled": 200},
    })
    assert biosim["missing_required_elements"] == []


def test_empty_context_still_produces_a_complete_element_set():
    """With no context at all the form must still be structurally complete -
    unknown values become visible placeholders, never missing elements."""
    icf = build_icf({})
    assert icf["missing_required_elements"] == []
    assert icf["sections"]
    assert icf["pending_fields"], "unknown fields must be listed, not silently blank"


# ---------------------------------------------------------------------------
# The benefit honesty gate - the serious drafting error
# ---------------------------------------------------------------------------


def test_healthy_volunteer_is_told_they_get_no_medical_benefit():
    """The single most serious drafting error in a phase-1 consent form is
    implying the subject may benefit. A healthy volunteer in a BE study gets
    nothing therapeutic, and the form must say so without hedging."""
    icf = build_icf(_be_context())
    assert icf["benefits"]["direct_medical_benefit"] is False

    text = _section_text(icf, "benefits")
    assert "no medical benefit" in text
    # The classic coercive phrasings.
    for bad in (
        "you may benefit",
        "may benefit from the study medication",
        "improve your health",
        "free health care",
        "therapeutic benefit to you",
    ):
        assert bad not in text, f"coercive benefit framing present: {bad!r}"


def test_benefit_statement_is_locked_against_the_narration_layer():
    icf = build_icf(_be_context())
    narration = _section(icf, "benefits")["narration"].lower()
    assert "locked" in narration
    assert "do not" in narration
    # Free screening tests are a classic thing wrongly sold as a benefit.
    assert "screening" in icf["benefits"]["forbidden_framing"].lower()


def test_patient_study_does_not_reuse_the_healthy_volunteer_no_benefit_claim():
    """A patient MIGHT benefit. Asserting a flat 'no benefit' to a patient would
    be as wrong as promising one to a healthy volunteer."""
    icf = build_icf(_be_context(population_config={"subject_type": "patient"}))
    assert icf["benefits"]["direct_medical_benefit"] is None
    text = _section_text(icf, "benefits")
    assert "not certain" in text or "may get no medical benefit" in text


# ---------------------------------------------------------------------------
# 21 CFR 50.25(c) - verbatim means verbatim
# ---------------------------------------------------------------------------


# 21 CFR 50.25(c), as prescribed. Hardcoded here ON PURPOSE: asserting that the
# module's constant equals itself would pass even if someone "simplified" the
# statutory wording, which is the exact failure this test exists to catch.
_STATUTORY_CT_GOV_TEXT = (
    "A description of this clinical trial will be available on "
    "http://www.ClinicalTrials.gov, as required by U.S. Law. This Web site will "
    "not include information that can identify you. At most, the Web site will "
    "include a summary of the results. You can search this Web site at any time."
)


def test_clinicaltrials_gov_clause_is_reproduced_verbatim():
    assert CLINICALTRIALS_GOV_STATEMENT == _STATUTORY_CT_GOV_TEXT, (
        "21 CFR 50.25(c) prescribes this wording; it may not be reworded, "
        "shortened, or plain-languaged")

    icf = build_icf(_be_context())
    ct = icf["clinicaltrials_gov"]
    assert ct["statement"] == _STATUTORY_CT_GOV_TEXT
    assert ct["verbatim"] is True
    assert ct["included"] is True, "including the clause can never be a deficiency"

    section = _section(icf, "clinicaltrials_gov")
    assert section["verbatim"] is True
    assert section["body"] == [_STATUTORY_CT_GOV_TEXT], (
        "the statutory wording must not be reworded or wrapped")


def test_verbatim_clause_is_exempt_from_the_plain_language_guard():
    """The clause cannot lawfully be simplified, so grading it would raise a
    finding nobody can act on."""
    icf = build_icf(_be_context())
    assert "clinicaltrials_gov" in icf["readability"]["excluded_verbatim_sections"]
    assert "clinicaltrials_gov" not in icf["readability"]["sections_over_target"]


def test_clinicaltrials_gov_applicability_is_reported_honestly():
    """A phase-1 healthy-volunteer study is NOT an 'applicable clinical trial',
    so the engine must include the clause while saying so, not pretend the
    statute compels it."""
    healthy = build_icf(_be_context())["clinicaltrials_gov"]
    assert healthy["required"] is False
    assert healthy["included"] is True
    assert "not strictly caught" in healthy["basis"]

    patient = build_icf(_be_context(
        population_config={"subject_type": "patient"}))["clinicaltrials_gov"]
    assert patient["required"] is True


def test_the_consent_form_keeps_its_clause_while_the_protocol_drops_the_topic():
    """The PROTOCOL no longer carries any ClinicalTrials.gov / FDAAA 801 /
    42 CFR 11.10 paragraph (see services/safety_reporting.py). The CONSENT FORM
    is a DIFFERENT document with a DIFFERENT legal basis - 21 CFR 50.25(c)
    prescribes this exact sentence for the consent document - so it stays here.

    This test pins both halves of that distinction so a later blanket-delete
    cannot strip the statutory clause out of the consent form by mistake.
    """
    icf = build_icf(_be_context())

    # (1) The consent form KEEPS the statutory clause, verbatim, in its own
    #     section, and that section is the only place the subject reads it.
    section = _section(icf, "clinicaltrials_gov")
    assert section["body"] == [_STATUTORY_CT_GOV_TEXT]
    assert section["applicable"] is True
    assert "50.25(c)" in section["cfr_refs"]

    # (2) The FDAAA 801 / 42 CFR 11.10 applicability reasoning is an INTERNAL
    #     audit record only. It never appears in prose the subject reads.
    body_text = _all_text(icf)
    for internal in ("fdaaa", "42 cfr 11.10", "phase 1"):
        assert internal not in body_text, (
            f"'{internal}' is engine reasoning, not consent-form text")

    # (3) Nor does it reach the generation prompt, where it could be narrated
    #     back into the protocol's Ethics section.
    directive = icf_directive(icf)
    assert "FDAAA" not in directive
    assert "42 CFR 11.10" not in directive
    assert "not strictly caught" not in directive


# ---------------------------------------------------------------------------
# Cross-document consistency - the ICF cannot disagree with its protocol
# ---------------------------------------------------------------------------


def test_washout_uses_the_same_rule_as_the_protocol():
    """The protocol locks washout at 7 x t1/2. If the ICF derived its own
    number the two documents would state different gaps between stays."""
    icf = build_icf(_be_context())
    washout = icf["study_facts"]["washout"]
    assert washout["applicable"] is True
    assert washout["hours"] == pytest.approx(WASHOUT_HALF_LIVES * 6.2, rel=1e-6)
    assert f"{WASHOUT_HALF_LIVES:.0f} x terminal half-life" in washout["basis"]


def test_short_half_life_washout_is_stated_in_hours_not_fractional_days():
    """Below one day, '0.6 days' is unreadable; the protocol states hours."""
    icf = build_icf(_be_context(sample_size={
        "design": "2-period crossover", "design_key": "crossover",
        "t_half_hours": 2.0, "n_enrolled": 24}))
    washout = icf["study_facts"]["washout"]
    assert washout["hours"] == pytest.approx(14.0)
    assert "hours" in washout["protocol_text"]


def test_parallel_design_has_no_washout_and_says_so():
    """A parallel subject is dosed once. Telling them to wait out a gap between
    stays would describe a study they are not in."""
    icf = build_icf(_be_context(sample_size={
        "design": "parallel-group", "design_key": "parallel", "n_enrolled": 60}))
    washout = icf["study_facts"]["washout"]
    assert washout["applicable"] is False
    assert washout["hours"] is None
    assert icf["study_facts"]["periods"] == 1
    assert "no gap" in washout["plain"].lower()
    # And the duration section must not invent a gap bullet.
    assert "gap between stays" not in _section_text(icf, "duration_and_visits")


def test_missing_half_life_leaves_a_visible_placeholder_not_a_guess():
    icf = build_icf(_be_context(sample_size={
        "design": "2-period crossover", "design_key": "crossover",
        "n_enrolled": 24}))
    washout = icf["study_facts"]["washout"]
    assert washout["hours"] is None
    assert "____" in washout["protocol_text"]
    assert any(p["field"] == "washout" for p in icf["pending_fields"])


def test_period_count_is_read_from_the_locked_design_label():
    """The number of stays a subject consents to must come from the protocol's
    own design string, not be re-derived."""
    icf = build_icf(_be_context(sample_size={
        "design": "3-period partial replicate", "design_key": "replicate",
        "t_half_hours": 10.0, "n_enrolled": 36}))
    assert icf["study_facts"]["periods"] == 3
    assert "design label" in icf["study_facts"]["periods_basis"]


# ---------------------------------------------------------------------------
# The real study-specific numbers
# ---------------------------------------------------------------------------


def test_blood_draws_and_volume_come_from_the_real_sampling_schedule():
    """A generic 'several blood samples' is the finding this replaces."""
    schedule = [0, 0.5, 1, 1.5, 2, 3, 4, 6, 8, 12, 24]  # 11 draws
    icf = build_icf(_be_context(sampling_schedule_hours=schedule))
    blood = icf["study_facts"]["blood"]

    assert blood["pk_draws_per_period"] == 11
    assert blood["pk_draws_total"] == 22, "2 periods x 11 draws"
    assert blood["assumed"] is False
    assert "sampling schedule" in blood["basis"]
    # 22 draws x 6 mL + (2 fixed + 2 period) panels x 10 mL
    assert blood["total_volume_ml"] == pytest.approx(22 * 6.0 + 4 * 10.0)

    text = _section_text(icf, "procedures")
    assert "22 samples in total" in text
    assert str(int(blood["total_volume_ml"])) in text


def test_absent_schedule_is_an_named_assumption_not_a_silent_number():
    icf = build_icf(_be_context())
    blood = icf["study_facts"]["blood"]
    assert blood["assumed"] is True
    assert blood["basis"].startswith("ASSUMED")
    assert any(a["field"] == "pk_draws_per_period" for a in icf["assumptions"]), (
        "an assumed burden figure must be disclosed, not buried")


def test_blood_volume_is_checked_against_the_healthy_adult_ceiling():
    icf = build_icf(_be_context())
    check = icf["study_facts"]["blood_volume_check"]
    assert check["ceiling_ml"] == 500.0
    assert check["window_days"] == 56
    assert check["within_ceiling"] is True
    # The ceiling is a per-window limit, so it must never be compared against a
    # whole-study total that spans more than one window.
    assert check["periods_in_window"] <= icf["study_facts"]["periods"]


def test_confinement_and_fasting_reach_the_procedures_section():
    icf = build_icf(_be_context(confinement_hours=48.0))
    dur = icf["study_facts"]["duration"]
    assert dur["confinement_after_dose_hours"] == 48.0
    assert "caller-supplied" in dur["confinement_basis"]
    # Check-in the night before + one night per 24 h of confinement.
    assert dur["nights_per_period"] == 3

    text = _section_text(icf, "procedures")
    assert "48 hours after each dose" in text
    assert "10 hours before each dose" in text


def test_real_prohibited_medications_override_the_generic_wording():
    """A consent form listing different prohibitions from the protocol's own
    exclusion criteria is a finding in its own right."""
    meds = ["warfarin", "any strong CYP3A4 inhibitor", "St John's wort"]
    icf = build_icf(_be_context(prohibited_medications=meds))
    assert icf["prohibited_medications"] == meds
    text = _section_text(icf, "restrictions")
    for m in meds:
        assert m.lower() in text
    assert "warfarin" in icf_directive(icf).lower()


# ---------------------------------------------------------------------------
# Label-grounded applicability
# ---------------------------------------------------------------------------


def test_qt_risk_is_stated_only_when_the_label_supports_it():
    qt_label = {"warnings": (
        "QT interval prolongation and torsade de pointes have been reported. "
        "Supplied as tablets for oral use."
    )}
    icf = build_icf(_be_context(label_data=qt_label))
    assert "qt_prolongation" in {r["key"] for r in icf["risks"]}

    bland = {"pharmacokinetics": "Absorption is rapid. Tablets for oral use."}
    icf2 = build_icf(_be_context(label_data=bland))
    keys = {r["key"] for r in icf2["risks"]}
    assert "qt_prolongation" not in keys, "a risk the product does not have is also a finding"
    assert "hepatotoxicity" not in keys
    assert "lactic_acidosis" not in keys


def test_procedure_risks_are_stated_regardless_of_the_drug():
    """Venipuncture, cumulative volume, confinement and allergy are inherent to
    every phase-1 PK study and cannot be label-gated away."""
    icf = build_icf(_be_context(label_data={"pharmacokinetics": "Rapidly absorbed."}))
    keys = {r["key"] for r in icf["risks"]}
    for always_on in ("venipuncture", "blood_volume", "confinement",
                      "allergic_reaction", "pregnancy", "unknown_risks"):
        assert always_on in keys, f"{always_on} must always be disclosed"


def test_grapefruit_restriction_is_driven_by_the_label_not_by_recall():
    cyp = {"clinical_pharmacology": "Metabolised by CYP3A4; grapefruit juice increases exposure. Capsules."}
    icf = build_icf(_be_context(label_data=cyp))
    assert "grapefruit" in {r["key"] for r in icf["restrictions"]}

    icf2 = build_icf(_be_context(label_data={"pharmacokinetics": "Excreted unchanged in urine. Tablets."}))
    assert "grapefruit" not in {r["key"] for r in icf2["restrictions"]}


# ---------------------------------------------------------------------------
# The form must not contradict itself
# ---------------------------------------------------------------------------


def test_injected_product_is_not_told_to_swallow_the_dose_with_water():
    """An oral BE template copied onto an injected biosimilar contradicts the
    protocol on the one procedure the subject cares about most."""
    icf = build_icf({
        "drug_inn": "adalimumab",
        "study_type": "biosimilar_pk",
        "label_data": _SC_LABEL,
        "sample_size": {"design": "parallel-group", "design_key": "parallel",
                        "n_enrolled": 200},
    })
    assert icf["study_facts"]["route"]["is_oral"] is False

    procedures = _section_text(icf, "procedures")
    assert "240 ml of water" not in procedures
    assert "by mouth" not in procedures
    assert "injection under the skin" in procedures

    # The oral-only dosing restrictions must be gone too.
    rkeys = {r["key"] for r in icf["restrictions"]}
    for oral_only in ("fasting", "water", "posture"):
        assert oral_only not in rkeys, f"{oral_only} is meaningless for an injection"


def test_risk_list_and_restriction_list_agree_about_fasting():
    """Internal consistency: the form must not warn about the discomfort of a
    fast in one section while never asking for a fast in another."""
    for ctx, expect_fast in (
        (_be_context(label_data={"description": "Tablets for oral use."}), True),
        ({
            "drug_inn": "adalimumab", "study_type": "biosimilar_pk",
            "label_data": _SC_LABEL,
            "sample_size": {"design": "parallel-group", "design_key": "parallel",
                            "n_enrolled": 200},
        }, False),
    ):
        icf = build_icf(ctx)
        risk_has_fast = "fasting" in {r["key"] for r in icf["risks"]}
        restriction_has_fast = "fasting" in {r["key"] for r in icf["restrictions"]}
        assert risk_has_fast == restriction_has_fast == expect_fast, (
            "risks and restrictions disagree about fasting")


def test_oral_product_keeps_the_fasting_and_dosing_conditions():
    icf = build_icf(_be_context(label_data={"description": "Film-coated tablets for oral use."}))
    assert icf["study_facts"]["route"]["is_oral"] is True
    procedures = _section_text(icf, "procedures")
    assert "by mouth" in procedures
    assert "240 ml of water" in procedures
    assert "fasting" in {r["key"] for r in icf["restrictions"]}


def test_blood_volume_comparison_never_understates_the_burden():
    """A form that prints a 588 mL total and then calls it 'less than one blood
    donation, which is about 470 mL' contradicts itself on the same line and
    understates what the subject is agreeing to - the unsafe direction."""
    dense = [0, 0.25, 0.5, 0.75, 1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10, 12, 16,
             24, 36, 48, 72, 96, 120]
    icf = build_icf(_be_context(
        sample_size={"design": "4-period fully replicate", "design_key": "replicate",
                     "is_nti": True, "t_half_hours": 40.0, "n_enrolled": 60},
        sampling_schedule_hours=dense))
    blood = icf["study_facts"]["blood"]
    assert blood["total_volume_ml"] > blood["donation_comparison_ml"]
    assert blood["exceeds_one_donation"] is True
    assert "less than one blood donation" not in blood["donation_comparison"]
    assert "more than one blood donation" in blood["donation_comparison"]
    assert "less than one blood donation" not in _section_text(icf, "procedures")


def test_modest_study_may_still_use_the_reassuring_comparison():
    """The guard must not overcorrect: when the volume really is under one
    donation, saying so is accurate and helps the reader."""
    icf = build_icf(_be_context(sampling_schedule_hours=[0, 1, 2, 4, 8, 12, 24]))
    blood = icf["study_facts"]["blood"]
    assert blood["total_volume_ml"] < blood["donation_comparison_ml"]
    assert blood["exceeds_one_donation"] is False
    assert "less than one blood donation" in blood["donation_comparison"]


# ---------------------------------------------------------------------------
# Voluntariness, contacts, and prohibited exculpatory language
# ---------------------------------------------------------------------------


def test_participation_is_voluntary_with_no_penalty_for_withdrawal():
    icf = build_icf(_be_context())
    text = _section_text(icf, "voluntary_and_withdrawal")
    assert "your choice" in text
    assert "stop at any time" in text
    assert "nothing bad will happen" in text
    assert "not lose any benefit" in text
    # 50.25(b)(2): the investigator may also withdraw the subject.
    assert "take you out of the study" in text


def test_three_separate_contacts_are_required_and_rights_contact_is_independent():
    """One phone number for all three is a standard IRB finding."""
    icf = build_icf(_be_context())
    section = _section(icf, "contacts")
    assert "50.25(a)(7)" in section["cfr_refs"]
    text = " ".join(section["body"]).lower()
    assert "questions about the study" in text
    assert "questions about your rights" in text
    assert "hurt" in text
    assert "not part of the study team" in text
    assert "independent" in section["narration"].lower()

    fields = {p["field"] for p in icf["pending_fields"]}
    for contact in ("study_contact", "rights_contact", "injury_contact"):
        assert contact in fields, "an unfilled contact must be flagged before submission"


def test_form_contains_no_exculpatory_waiver_of_legal_rights():
    """21 CFR 50.20 forbids any language waiving or appearing to waive the
    subject's legal rights."""
    icf = build_icf(_be_context())
    text = _all_text(icf)
    assert "do not give up any of your legal rights" in text
    for forbidden in (
        "you waive",
        "you release the sponsor",
        "you give up your right",
        "not liable",
    ):
        assert forbidden not in text, f"exculpatory language present: {forbidden!r}"


def test_injury_section_states_treatment_and_who_pays():
    icf = build_icf(_be_context())
    text = _section_text(icf, "injury")
    assert "medical care" in text
    assert "sponsor will pay" in text


# ---------------------------------------------------------------------------
# Signature block
# ---------------------------------------------------------------------------


def test_signature_block_has_subject_consent_discussant_and_witness():
    icf = build_icf(_be_context())
    block = icf["signature_block"]
    rows = {r["key"]: r for r in block["rows"]}

    assert rows["subject"]["required"] is True
    assert rows["consent_discussion"]["required"] is True, (
        "ICH E6(R2) 4.8.8 requires the person conducting the discussion to sign")
    for field in ("Printed name", "Signature", "Date"):
        assert field in rows["subject"]["fields"]

    witness = rows["impartial_witness"]
    assert witness["required"] is False
    assert "unable to read" in witness["required_when"], (
        "the witness condition must be stated, not left to the site to guess")


def test_consent_must_precede_any_study_specific_procedure():
    """Screening before signature is a GCP finding, so the rule is on the form
    rather than assumed."""
    icf = build_icf(_be_context())
    timing = icf["signature_block"]["timing_rule"].lower()
    assert "before any study-specific procedure" in timing
    assert "screening" in timing
    assert "personally dated" in timing
    assert "copy" in icf["signature_block"]["copy_rule"].lower()


# ---------------------------------------------------------------------------
# Version control - the ICF is versioned separately from the protocol
# ---------------------------------------------------------------------------


def test_version_block_ties_the_form_to_its_protocol_version():
    icf = build_icf(_be_context(
        icf_version="2.1",
        icf_date="2026-07-28",
        protocol_number="PKBE-2026-014",
        protocol_version="3.0",
    ))
    v = icf["version"]
    assert v["icf_version"] == "2.1"
    assert v["protocol_number"] == "PKBE-2026-014"
    assert v["protocol_version"] == "3.0"
    assert "separately" in v["note"]

    header = " ".join(_section(icf, "header")["body"])
    assert "PKBE-2026-014" in header
    assert "2.1" in header

    # Supplied fields must drop off the pending list.
    fields = {p["field"] for p in icf["pending_fields"]}
    assert "protocol_number" not in fields
    assert "icf_date" not in fields


def test_unversioned_form_flags_every_tie_to_the_protocol_as_pending():
    icf = build_icf(_be_context())
    fields = {p["field"] for p in icf["pending_fields"]}
    for required in ("icf_date", "protocol_number", "protocol_version"):
        assert required in fields


# ---------------------------------------------------------------------------
# Readability - a consent form nobody can read is not consent
# ---------------------------------------------------------------------------


def test_readability_helper_scores_and_flags_correctly():
    plain = readability_grade("You can stop at any time. You do not have to give a reason.")
    assert plain["grade_level"] <= 8.0
    assert plain["meets_target"] is True

    dense = readability_grade(
        "Subsequent to randomization, the investigator shall administer the "
        "investigational medicinal product and document any adverse event "
        "utilizing the electronic data capture system prior to the "
        "pharmacokinetic sampling interval commencing at the designated hour."
    )
    assert dense["grade_level"] > 8.0
    assert dense["meets_target"] is False
    assert dense["long_sentences"], "a 40-word sentence must be flagged"
    flagged = {j["term"] for j in dense["jargon"]}
    assert {"administer", "adverse event", "prior to"} <= flagged

    empty = readability_grade("")
    assert empty["grade_level"] == 0.0 and empty["meets_target"] is True


def test_generated_consent_prose_is_plain_language():
    icf = build_icf(_be_context())
    report = icf["readability"]
    assert report["overall"]["grade_level"] <= 8.0
    assert report["overall"]["meets_target"] is True
    assert report["sections_over_target"] == []
    assert report["overall"]["jargon"] == [], (
        "protocol jargon has leaked into the consent form")
    assert report["overall"]["long_sentences"] == []


def test_fda_name_does_not_trip_the_jargon_guard():
    """21 CFR 50.25(a)(5) requires the agency to be named, and its name contains
    'Administration'. Flagging that would be a finding nobody can act on."""
    result = readability_grade(
        "The Food and Drug Administration may inspect your records."
    )
    assert result["jargon"] == []


def test_readability_holds_for_the_injected_biosimilar_form_too():
    icf = build_icf({
        "drug_inn": "adalimumab",
        "study_type": "biosimilar_pk",
        "label_data": _SC_LABEL,
        "sample_size": {"design": "parallel-group", "design_key": "parallel",
                        "n_enrolled": 200},
    })
    assert icf["readability"]["overall"]["meets_target"] is True
    assert icf["readability"]["sections_over_target"] == []


# ---------------------------------------------------------------------------
# Serialisation and the AI directive
# ---------------------------------------------------------------------------


def test_result_is_json_serialisable_for_population_config():
    """It is persisted inside population_config (JSONB), so it must round-trip."""
    icf = build_icf(_be_context(
        label_data={"warnings": "QT prolongation. Tablets for oral use."},
        sampling_schedule_hours=[0, 1, 2, 4, 8, 24],
        prohibited_medications=["warfarin"],
    ))
    restored = json.loads(json.dumps(icf))
    assert restored == icf
    assert restored["study_facts"]["blood"]["pk_draws_total"] == \
        icf["study_facts"]["blood"]["pk_draws_total"]
    assert restored["sections"][0]["key"] == icf["sections"][0]["key"]


def test_empty_and_minimal_contexts_also_round_trip():
    for ctx in ({}, {"drug_inn": "aspirin"}, _be_context()):
        assert json.loads(json.dumps(build_icf(ctx)))


def test_directive_locks_the_facts_for_the_prompt():
    icf = build_icf(_be_context(sampling_schedule_hours=[0, 1, 2, 4, 8, 24]))
    directive = icf_directive(icf)

    assert "ALREADY DETERMINED" in directive
    # The numbers the model may not recalculate.
    blood = icf["study_facts"]["blood"]
    assert str(blood["pk_draws_total"]) in directive
    assert str(blood["total_volume_ml"]) in directive
    assert icf["study_facts"]["washout"]["protocol_text"] in directive
    # Every section, with the regulation it discharges.
    for s in icf["sections"]:
        assert s["heading"] in directive
    assert "50.25(a)(1)" in directive
    # The benefit rule must survive into the prompt as non-negotiable.
    assert "LOCKED AND NON-NEGOTIABLE" in directive
    assert "plain language" in directive.lower()
    # Empty input yields no directive rather than a misleading empty header.
    assert icf_directive({}) == ""
    assert icf_directive({"sections": []}) == ""


def test_directive_tells_the_model_not_to_invent_unfilled_fields():
    icf = build_icf(_be_context())
    directive = icf_directive(icf)
    assert "UNFILLED FIELDS" in directive
    assert "Do NOT" in directive
    assert "study_contact" in directive


def test_directive_forbids_simplifying_the_verbatim_clause():
    directive = icf_directive(build_icf(_be_context()))
    assert "VERBATIM" in directive
    assert CLINICALTRIALS_GOV_STATEMENT[:40] in directive or "50.25(c)" in directive


def test_output_is_ascii_only():
    """Windows console encoding (cp1252) has bitten this repo before, and this
    text is printed, logged, and rendered into DOCX."""
    payload = json.dumps(build_icf(_be_context(
        label_data={"warnings": "QT prolongation, rash, nausea. Tablets."})))
    payload.encode("ascii")
    icf_directive(build_icf(_be_context())).encode("ascii")
