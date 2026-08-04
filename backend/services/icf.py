"""Standalone Informed Consent Form (ICF) - the second document in the package.

WHY THIS MODULE EXISTS
----------------------
The application produces one artifact, the protocol, and folds a paragraph about
the consent PROCESS into Section 14 (Ethical and Regulatory Standards). That
paragraph is a description of a process; it is not an informed consent form.

ICH E6(R2) 4.8.10 and 21 CFR 50.25 require the ICF to be its OWN document, with
a defined element set, its own version and date, and its own signature block.
The IRB reviews and approves it separately from the protocol, and it is
version-controlled separately (a protocol amendment does not automatically carry
the ICF with it). Submitting a protocol with no separate consent form is the
single most common reason an IRB administratively bounces a package before it
ever reaches scientific review.

WHAT IS DETERMINISTIC HERE
--------------------------
Like :mod:`services.stratification` and :mod:`services.biostatistics`, this
module computes FACTS that the narration layer may only render, never invent:

  * the regulatory ELEMENT SET (21 CFR 50.25(a), (b), (c)) and which section of
    the form discharges each element - so a missing element is a computed
    finding, not something a reviewer has to notice by eye;
  * the study-specific numbers a subject is actually consenting to - number of
    blood draws, total blood volume, hours of confinement, washout length,
    number of periods, approximate number of subjects - taken from the SAME
    locked design the protocol uses, so the two documents cannot disagree. An
    ICF that contradicts its protocol is a finding in its own right;
  * the risk list and the restriction list, detected from the REAL drug label
    text by regex cue (the approach used in services/design_decisions.py and
    services/stratification.py) rather than from model recall;
  * the signature block, including when an impartial witness is required.

THE DRAFTING ERROR THIS ENGINE IS BUILT TO PREVENT
--------------------------------------------------
A healthy volunteer receives NO therapeutic benefit from a bioequivalence or
PK-similarity study. Consent forms routinely and wrongly imply otherwise
("you may benefit from the study medication"). That is a coercion finding, not a
style problem. ``benefits`` is therefore computed from the population, is
explicitly ``direct_medical_benefit: False`` for healthy volunteers, and the
narration layer is told it may not soften it.

READABILITY
-----------
Consent for healthy volunteers must be plain language. The conventional target
is roughly an 8th-grade reading level. A Flesch-Kincaid grade-level guard is
implemented locally (no new dependency) and run over the deterministic prose, so
a form that drifts into protocol register is flagged rather than shipped.
Statutory text that MUST be reproduced verbatim (the 21 CFR 50.25(c)
ClinicalTrials.gov clause) is excluded from the guard - it cannot lawfully be
simplified.

SCOPE
-----
This is the CONTENT MODEL plus the deterministic parts. It returns an ordered,
JSON-serialisable document structure (it is persisted inside population_config,
a JSONB column). Rendering to DOCX/PDF and wiring into the export routes belong
to the integration layer, not here.
"""
from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass
from typing import Any, Optional

from services.biostatistics import WASHOUT_HALF_LIVES
from services.design_decisions import extract_label_routes
from services.guidelines import get_profile

# ---------------------------------------------------------------------------
# Named assumptions
# ---------------------------------------------------------------------------
# Every number below is either a stated regulatory/clinical convention (cited in
# the comment) or an explicit assumption. Assumptions are surfaced in the result
# under "assumptions" so the protocol package can disclose them rather than
# quietly relying on them. A consent form must state the MAXIMUM burden a
# subject is agreeing to, so where a convention gives a range this module takes
# the conservative (larger) end - understating burden in a consent form is the
# unsafe direction.

# FDA BE guidance fasting-study conditions: an overnight fast of at least
# 10 hours before dosing, dosing with 240 mL water, no other fluid from 1 hour
# before until 1 hour after dosing, and a standardised meal 4 hours post-dose.
_FAST_BEFORE_DOSE_HOURS = 10.0
_DOSING_WATER_ML = 240.0
_WATER_RESTRICTION_HOURS = 1.0
_FAST_AFTER_DOSE_HOURS = 4.0
# Subjects remain upright for the early absorption phase in oral BE studies.
_UPRIGHT_AFTER_DOSE_HOURS = 4.0

# Post-dose confinement in a single-dose BE/PK study conventionally runs to the
# 24-hour sample; later points are collected as ambulatory (outpatient) visits.
_CONFINEMENT_AFTER_DOSE_HOURS = 24.0

# Standard screening window between consent/screening and first dosing.
_SCREENING_WINDOW_DAYS = 28

# Safety follow-up / end-of-study contact after the last PK sample.
_FOLLOW_UP_DAYS = 7
# Biosimilar PK-similarity studies carry an immunogenicity tail. The protocol's
# own Study Assessments section specifies ADA sampling to Day 85 with a Week-24
# end-of-study sample; the ICF must state that longer commitment, not the
# small-molecule 7-day one.
_BIOSIMILAR_EOS_DAYS = 168  # Week 24

# Blood volume. The protocol's Study Assessments section specifies 4-6 mL per PK
# draw; the ICF states the upper end because the subject consents to a maximum.
_BLOOD_ML_PER_PK_DRAW = 6.0
# A combined haematology + chemistry (+ serology/virology at screening) panel.
_BLOOD_ML_PER_SAFETY_PANEL = 10.0
# Safety panels: one at screening, one at each period check-in, one at the
# end-of-study visit.
_SAFETY_PANELS_FIXED = 2
# Ceiling on collected volume. The widely used healthy-adult research convention
# is 550 mL in any 8-week period; this app's protocol text states ~500 mL, so
# the ICF uses the same, more conservative 500 mL figure to stay consistent with
# the document it accompanies.
_BLOOD_VOLUME_CEILING_ML = 500.0
_BLOOD_VOLUME_WINDOW_DAYS = 56  # 8 weeks
# Plain-language comparators. A whole-blood donation collects about 470 mL; one
# US tablespoon is about 14.8 mL.
_BLOOD_DONATION_ML = 470.0
_ML_PER_TABLESPOON = 14.8

# Number of PK draws per period when the caller supplies no sampling schedule.
# The protocol's Study Assessments section requires 12-16 post-dose timepoints;
# 1 pre-dose + 16 post-dose is the upper end, i.e. the maximum burden.
_ASSUMED_PK_DRAWS_PER_PERIOD = 17

# Restriction windows. Each is the conventional phase-1 protocol figure.
_ALCOHOL_RESTRICTION_HOURS = 48    # 24-48 h is standard; take the longer window
_CAFFEINE_RESTRICTION_HOURS = 24
_EXERCISE_RESTRICTION_HOURS = 48
_GRAPEFRUIT_RESTRICTION_DAYS = 7   # CYP3A4 inhibition persists days after intake
_CONMED_WASHOUT_DAYS = 14          # prescription / OTC medicines, 2 weeks
_ENZYME_INDUCER_WASHOUT_DAYS = 30  # induction takes 2-4 weeks to reverse
_BLOOD_DONATION_RESTRICTION_DAYS = 90
# Contraception: female participants through 30 days after the last dose; male
# participants avoid sperm donation for 90 days (one spermatogenesis cycle is
# roughly 74-90 days).
_CONTRACEPTION_DAYS_FEMALE = 30
_CONTRACEPTION_DAYS_MALE = 90

# Readability target for a healthy-volunteer consent form.
_TARGET_READING_GRADE = 8.0
# A sentence longer than this is flagged even if the overall grade passes - long
# sentences are what actually defeats a reader, and the grade formula can hide
# one long sentence inside many short ones.
_LONG_SENTENCE_WORDS = 25

# Unfilled fields are rendered with this token so an incomplete form is obvious
# on the page (an ICF submitted with live placeholders is the other common
# administrative rejection).
_PLACEHOLDER = "[__________]"


# ---------------------------------------------------------------------------
# Regulatory element catalogue
# ---------------------------------------------------------------------------
# The statutory element set. `tier` distinguishes the elements that must ALWAYS
# appear (21 CFR 50.25(a)) from those required only when applicable (50.25(b))
# and the ClinicalTrials.gov clause (50.25(c)). Coverage is computed, not
# asserted: build_icf() maps every element code to the section(s) that discharge
# it and reports any basic element left uncovered.


@dataclass(frozen=True)
class ConsentElement:
    code: str                 # e.g. "50.25(a)(1)"
    key: str
    tier: str                 # "basic" | "additional" | "clinicaltrials"
    requirement: str          # what the regulation actually demands
    plain_language_hint: str  # how it must read to a lay subject

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_ELEMENTS: tuple[ConsentElement, ...] = (
    ConsentElement(
        code="50.25(a)(1)",
        key="research_purpose_duration_procedures",
        tier="basic",
        requirement=(
            "A statement that the study involves research, an explanation of the "
            "purposes of the research, the expected duration of the subject's "
            "participation, a description of the procedures to be followed, and "
            "identification of any procedures which are experimental."
        ),
        plain_language_hint=(
            "Say plainly that this is research, why it is being done, how long it "
            "will take, what will be done to the subject, and which parts are "
            "experimental."
        ),
    ),
    ConsentElement(
        code="50.25(a)(2)",
        key="risks",
        tier="basic",
        requirement=(
            "A description of any reasonably foreseeable risks or discomforts to "
            "the subject."
        ),
        plain_language_hint=(
            "List what can go wrong and how it will feel, including the risks of "
            "the procedures themselves, not only of the medicine."
        ),
    ),
    ConsentElement(
        code="50.25(a)(3)",
        key="benefits",
        tier="basic",
        requirement=(
            "A description of any benefits to the subject or to others which may "
            "reasonably be expected from the research."
        ),
        plain_language_hint=(
            "State honestly whether the subject personally benefits. For a healthy "
            "volunteer the answer is no."
        ),
    ),
    ConsentElement(
        code="50.25(a)(4)",
        key="alternatives",
        tier="basic",
        requirement=(
            "A disclosure of appropriate alternative procedures or courses of "
            "treatment, if any, that might be advantageous to the subject."
        ),
        plain_language_hint=(
            "For a healthy volunteer the only alternative is not to take part; say "
            "exactly that rather than listing treatments they do not need."
        ),
    ),
    ConsentElement(
        code="50.25(a)(5)",
        key="confidentiality",
        tier="basic",
        requirement=(
            "A statement describing the extent, if any, to which confidentiality of "
            "records identifying the subject will be maintained, and that notes the "
            "possibility that the Food and Drug Administration may inspect the records."
        ),
        plain_language_hint=(
            "Say who can see the records, including that the FDA may look at them, "
            "and that the subject is identified by a code."
        ),
    ),
    ConsentElement(
        code="50.25(a)(6)",
        key="injury_compensation",
        tier="basic",
        requirement=(
            "For research involving more than minimal risk, an explanation as to "
            "whether any compensation and whether any medical treatments are "
            "available if injury occurs and, if so, what they consist of, or where "
            "further information may be obtained."
        ),
        plain_language_hint=(
            "Say what happens, and who pays, if the subject is hurt by the study."
        ),
    ),
    ConsentElement(
        code="50.25(a)(7)",
        key="contacts",
        tier="basic",
        requirement=(
            "An explanation of whom to contact for answers to pertinent questions "
            "about the research and research subjects' rights, and whom to contact "
            "in the event of a research-related injury."
        ),
        plain_language_hint=(
            "Give three separate contacts: study questions, subject rights, and "
            "injury. The rights contact must not be the investigator."
        ),
    ),
    ConsentElement(
        code="50.25(a)(8)",
        key="voluntary_participation",
        tier="basic",
        requirement=(
            "A statement that participation is voluntary, that refusal to "
            "participate will involve no penalty or loss of benefits to which the "
            "subject is otherwise entitled, and that the subject may discontinue "
            "participation at any time without penalty or loss of benefits."
        ),
        plain_language_hint=(
            "Say the subject can say no, or stop at any time, and nothing bad will "
            "happen to them because of it."
        ),
    ),
    ConsentElement(
        code="50.25(b)(1)",
        key="unforeseeable_risks",
        tier="additional",
        requirement=(
            "A statement that the treatment or procedure may involve risks to the "
            "subject (or to the embryo or fetus, if the subject is or may become "
            "pregnant) which are currently unforeseeable."
        ),
        plain_language_hint=(
            "Say that there may be risks nobody knows about yet, including risks to "
            "an unborn baby."
        ),
    ),
    ConsentElement(
        code="50.25(b)(2)",
        key="investigator_termination",
        tier="additional",
        requirement=(
            "Anticipated circumstances under which the subject's participation may "
            "be terminated by the investigator without regard to the subject's consent."
        ),
        plain_language_hint=(
            "List the reasons the study doctor may take the subject out of the "
            "study even if they want to stay."
        ),
    ),
    ConsentElement(
        code="50.25(b)(3)",
        key="additional_costs",
        tier="additional",
        requirement=(
            "Any additional costs to the subject that may result from participation "
            "in the research."
        ),
        plain_language_hint="Say whether the subject has to pay for anything.",
    ),
    ConsentElement(
        code="50.25(b)(4)",
        key="withdrawal_consequences",
        tier="additional",
        requirement=(
            "The consequences of a subject's decision to withdraw from the research "
            "and procedures for orderly termination of participation by the subject."
        ),
        plain_language_hint=(
            "Say what happens if the subject leaves early, including any safety "
            "checks they are asked to complete."
        ),
    ),
    ConsentElement(
        code="50.25(b)(5)",
        key="new_findings",
        tier="additional",
        requirement=(
            "A statement that significant new findings developed during the course "
            "of the research which may relate to the subject's willingness to "
            "continue participation will be provided to the subject."
        ),
        plain_language_hint=(
            "Say the subject will be told if anything new is learned that might "
            "change their mind."
        ),
    ),
    ConsentElement(
        code="50.25(b)(6)",
        key="number_of_subjects",
        tier="additional",
        requirement="The approximate number of subjects involved in the study.",
        plain_language_hint="Give the number of people who will take part.",
    ),
    ConsentElement(
        code="50.25(c)",
        key="clinicaltrials_gov",
        tier="clinicaltrials",
        requirement=(
            "Informed consent documents for applicable clinical trials must include "
            "the statement prescribed by 21 CFR 50.25(c), reproduced verbatim."
        ),
        plain_language_hint=(
            "This wording is set by regulation and must be copied exactly; it may "
            "not be simplified."
        ),
    ),
)

_ELEMENTS_BY_CODE = {e.code: e for e in _ELEMENTS}
BASIC_ELEMENT_CODES: tuple[str, ...] = tuple(e.code for e in _ELEMENTS if e.tier == "basic")

# 21 CFR 50.25(c), reproduced exactly. This text is prescribed by regulation and
# must not be reworded, shortened, or "plain-languaged" - it is deliberately
# excluded from the readability guard for that reason.
#
# THIS CLAUSE BELONGS TO THE CONSENT FORM, NOT TO THE PROTOCOL. The PROTOCOL no
# longer says anything about ClinicalTrials.gov, FDAAA 801 or 42 CFR part 11
# when the study is not an applicable clinical trial: a protocol does not carry
# a paragraph explaining why a regulation it is not subject to does not apply
# (see services/safety_reporting.py::_ctgov_obligation, where that paragraph was
# removed). The ICF is a DIFFERENT DOCUMENT with a DIFFERENT legal basis - 21
# CFR 50.25(c) prescribes this exact sentence for the consent document itself,
# in wording that may not lawfully be simplified - so it stays here, verbatim,
# and the two changes must not be confused with one another.
CLINICALTRIALS_GOV_STATEMENT = (
    "A description of this clinical trial will be available on "
    "http://www.ClinicalTrials.gov, as required by U.S. Law. This Web site will "
    "not include information that can identify you. At most, the Web site will "
    "include a summary of the results. You can search this Web site at any time."
)


# ---------------------------------------------------------------------------
# Risk catalogue
# ---------------------------------------------------------------------------
# Procedure risks (`always=True`) are inherent to any single-dose phase-1 PK
# study and are stated regardless of the drug. Drug risks are only stated when
# the REAL label text gives positive evidence for them - a consent form that
# lists risks the product does not have is as much a finding as one that omits
# risks it does.


@dataclass(frozen=True)
class RiskItem:
    key: str
    label: str
    category: str          # "procedure" | "drug" | "reproductive" | "unknown"
    likelihood: str        # "common" | "uncommon" | "rare" | "unknown"
    plain_language: str    # the sentence that goes on the form
    mitigation: str        # what the site does about it
    evidence: str          # what the applicability judgement rests on
    applies_when: str
    always: bool = False
    oral_only: bool = False   # meaningless for an injected product
    detected: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_RISK_CATALOGUE: tuple[RiskItem, ...] = (
    RiskItem(
        key="venipuncture",
        label="Blood draws",
        category="procedure",
        likelihood="common",
        plain_language=(
            "Blood draws can hurt. You may get a bruise or a sore arm. Some people "
            "feel faint or dizzy. Less often, the skin where the needle goes in can "
            "get infected."
        ),
        mitigation=(
            "Trained staff take all samples. A cannula is used so you are not stuck "
            "with a needle for every sample."
        ),
        evidence="Inherent to the PK sampling schedule",
        applies_when="Always - every PK study takes repeated blood samples",
        always=True,
    ),
    RiskItem(
        key="blood_volume",
        label="Total blood taken",
        category="procedure",
        likelihood="uncommon",
        plain_language=(
            "Because we take blood many times, you may feel tired for a short time. "
            "The amount we take is checked so it stays within safe limits."
        ),
        mitigation=(
            "Total volume is capped and haemoglobin is checked at screening and at "
            "the end of the study."
        ),
        evidence="Cumulative sampling volume computed for this study",
        applies_when="Always - repeated sampling adds up across periods",
        always=True,
    ),
    RiskItem(
        key="fasting",
        label="Going without food",
        category="procedure",
        likelihood="common",
        plain_language=(
            "You must not eat for several hours before your dose. This can make you "
            "hungry. It can also give you a headache or make you feel weak or dizzy."
        ),
        mitigation="Meals are given on a set schedule once the fast ends.",
        evidence="Fasting conditions required by the bioequivalence guidance",
        applies_when="An orally administered product dosed under fasting conditions",
        always=True,
        # An injected product is not dosed under a fast. Leaving this risk in an
        # injected-product form would warn the subject about a fast that the same
        # document's restriction list (correctly) never asks them to keep.
        oral_only=True,
    ),
    RiskItem(
        key="confinement",
        label="Staying at the unit",
        category="procedure",
        likelihood="common",
        plain_language=(
            "You must stay at the unit for each study stay. You cannot leave when "
            "you want to. Your sleep and your daily routine may be upset."
        ),
        mitigation="You may leave the study at any time if you choose to stop.",
        evidence="Confinement required to control sampling and meal timing",
        applies_when="Always - the unit controls dosing and sampling conditions",
        always=True,
    ),
    RiskItem(
        key="allergic_reaction",
        label="Allergic reaction",
        category="drug",
        likelihood="rare",
        plain_language=(
            "Any medicine can cause an allergic reaction. Signs are a rash, itching, "
            "swelling, or trouble breathing. A severe reaction is rare but can be "
            "life threatening."
        ),
        mitigation=(
            "Staff trained in emergency care are on site, and emergency medicines "
            "and equipment are kept ready."
        ),
        evidence="Inherent to administering any medicinal product",
        applies_when="Always - allergy risk cannot be excluded for any drug",
        always=True,
    ),
    RiskItem(
        key="ecg_skin",
        label="Heart tracing (ECG)",
        category="procedure",
        likelihood="uncommon",
        plain_language=(
            "Sticky pads are put on your chest for the heart tracing. Taking them "
            "off can pull at the skin or leave a red mark."
        ),
        mitigation="Pads are placed and removed by trained staff.",
        evidence="Inherent to the safety assessment schedule",
        applies_when="Always - ECGs are part of the safety schedule",
        always=True,
    ),
    RiskItem(
        key="pregnancy",
        label="Risk to an unborn baby",
        category="reproductive",
        likelihood="unknown",
        plain_language=(
            "The study drug could harm an unborn baby. You must not join if you are "
            "pregnant or breastfeeding. You must use birth control as described "
            "below."
        ),
        mitigation=(
            "Pregnancy tests are done at screening and before each dose. "
            "Contraception is required for the whole study."
        ),
        evidence="Standard reproductive-risk element (21 CFR 50.25(b)(1))",
        applies_when="Always - reproductive risk is not fully known in phase 1",
        always=True,
    ),
    RiskItem(
        key="unknown_risks",
        label="Risks we do not know about",
        category="unknown",
        likelihood="unknown",
        plain_language=(
            "There may be risks that are not known yet. You will be told right away "
            "if we learn anything new that might change your mind about taking part."
        ),
        mitigation="New safety findings are reported to you and to the review board.",
        evidence="Required element 21 CFR 50.25(b)(1)",
        applies_when="Always",
        always=True,
    ),
    # ---- label-detected drug risks ----------------------------------------
    RiskItem(
        key="hypoglycemia",
        label="Low blood sugar",
        category="drug",
        likelihood="common",
        plain_language=(
            "This medicine can make your blood sugar drop too low. You may feel "
            "shaky, sweaty, hungry, or confused. Very low blood sugar can make you "
            "pass out."
        ),
        mitigation=(
            "Blood sugar is watched closely and sugar is given straight away if it "
            "falls too low."
        ),
        evidence="Label text describes hypoglycaemia",
        applies_when="The label reports hypoglycaemia or the product lowers glucose",
    ),
    RiskItem(
        key="bleeding",
        label="Bleeding or bruising",
        category="drug",
        likelihood="common",
        plain_language=(
            "This medicine can make you bleed or bruise more easily. Tell the study "
            "staff about any bleeding that will not stop."
        ),
        mitigation="Clotting tests are done and bleeding signs are checked daily.",
        evidence="Label text describes bleeding or anticoagulant action",
        applies_when="The label reports bleeding risk or the drug affects clotting",
    ),
    RiskItem(
        key="qt_prolongation",
        label="Change in heart rhythm",
        category="drug",
        likelihood="uncommon",
        plain_language=(
            "This medicine can change the rhythm of your heart. In rare cases this "
            "can cause a fast or uneven heartbeat that is dangerous."
        ),
        mitigation="Heart tracings are taken before and after dosing.",
        evidence="Label text describes QT prolongation or arrhythmia",
        applies_when="The label reports QT effects or torsades de pointes",
    ),
    RiskItem(
        key="hepatotoxicity",
        label="Effect on the liver",
        category="drug",
        likelihood="uncommon",
        plain_language=(
            "This medicine can affect the liver. Signs are yellow skin or eyes, dark "
            "urine, or pain in the upper belly."
        ),
        mitigation="Liver blood tests are done at screening and at the end of study.",
        evidence="Label text describes liver injury or raised liver enzymes",
        applies_when="The label reports hepatotoxicity or transaminase elevation",
    ),
    RiskItem(
        key="nephrotoxicity",
        label="Effect on the kidneys",
        category="drug",
        likelihood="uncommon",
        plain_language=(
            "This medicine can affect the kidneys. You may pass less urine than "
            "usual or your ankles may swell."
        ),
        mitigation="Kidney blood tests are done at screening and at the end of study.",
        evidence="Label text describes renal injury",
        applies_when="The label reports nephrotoxicity or acute kidney injury",
    ),
    RiskItem(
        key="gi_upset",
        label="Stomach upset",
        category="drug",
        likelihood="common",
        plain_language=(
            "This medicine can upset your stomach. You may feel sick, be sick, or "
            "have loose stools or belly pain."
        ),
        mitigation="Symptoms are recorded and treated as needed.",
        evidence="Label text describes gastrointestinal adverse reactions",
        applies_when="The label reports nausea, vomiting or diarrhoea",
    ),
    RiskItem(
        key="somnolence",
        label="Feeling sleepy or dizzy",
        category="drug",
        likelihood="common",
        plain_language=(
            "This medicine can make you sleepy or dizzy. Do not drive and do not use "
            "machines until the study doctor says it is safe."
        ),
        mitigation="You stay at the unit while the drug is in your body.",
        evidence="Label text describes somnolence, sedation or dizziness",
        applies_when="The label reports drowsiness, sedation or dizziness",
    ),
    RiskItem(
        key="hypotension",
        label="Low blood pressure or slow pulse",
        category="drug",
        likelihood="uncommon",
        plain_language=(
            "This medicine can lower your blood pressure or slow your pulse. You may "
            "feel dizzy or faint when you stand up."
        ),
        mitigation="Blood pressure and pulse are checked at set times after dosing.",
        evidence="Label text describes hypotension or bradycardia",
        applies_when="The label reports hypotension, orthostasis or bradycardia",
    ),
    RiskItem(
        key="lactic_acidosis",
        label="Build-up of acid in the blood",
        category="drug",
        likelihood="rare",
        plain_language=(
            "This medicine can rarely cause acid to build up in the blood. Signs are "
            "deep or fast breathing, muscle pain, and feeling very weak. This is rare "
            "but serious."
        ),
        mitigation="Kidney function is checked and blood tests are taken if you feel unwell.",
        evidence="Label text describes lactic acidosis",
        applies_when="The label carries a lactic acidosis warning",
    ),
    RiskItem(
        key="skin_reaction",
        label="Skin rash",
        category="drug",
        likelihood="uncommon",
        plain_language=(
            "This medicine can cause a skin rash. In rare cases a rash can be severe "
            "and involve the mouth or eyes."
        ),
        mitigation="Skin is checked daily and any rash is reviewed by the study doctor.",
        evidence="Label text describes rash or serious skin reactions",
        applies_when="The label reports rash or severe cutaneous reactions",
    ),
    RiskItem(
        key="photosensitivity",
        label="Skin burning in sunlight",
        category="drug",
        likelihood="uncommon",
        plain_language=(
            "This medicine can make your skin burn more easily in the sun. Stay out "
            "of strong sun and use sun cream."
        ),
        mitigation="You are told to avoid sunlamps and strong sunlight.",
        evidence="Label text describes photosensitivity",
        applies_when="The label reports photosensitivity",
    ),
    RiskItem(
        key="injection_site",
        label="Reaction where the injection is given",
        category="drug",
        likelihood="common",
        plain_language=(
            "The place where the injection is given can turn red, swell, or feel "
            "sore or itchy. This usually settles within a few days."
        ),
        mitigation="The injection site is checked and graded before and after dosing.",
        evidence="Label text describes subcutaneous or intramuscular administration",
        applies_when="The product is given by injection under the skin or into muscle",
    ),
    RiskItem(
        key="infusion_reaction",
        label="Reaction during a drip",
        category="drug",
        likelihood="uncommon",
        plain_language=(
            "During the drip you may get chills, fever, a rash, or feel short of "
            "breath. The drip can be slowed or stopped if this happens."
        ),
        mitigation="You are watched throughout the infusion and for a set time after it.",
        evidence="Label text describes infusion-related reactions",
        applies_when="The product is given as an intravenous infusion",
    ),
    RiskItem(
        key="immunogenicity",
        label="Your body making antibodies to the drug",
        category="drug",
        likelihood="uncommon",
        plain_language=(
            "Your body may make antibodies against this medicine. This can change "
            "how the medicine works and, rarely, can cause an allergic reaction."
        ),
        mitigation="Blood samples are taken over several months to test for antibodies.",
        evidence="Label text describes immunogenicity or anti-drug antibodies",
        applies_when="The product is a biologic with a known immunogenicity profile",
    ),
    RiskItem(
        key="myelosuppression",
        label="Effect on blood cells",
        category="drug",
        likelihood="uncommon",
        plain_language=(
            "This medicine can lower the number of cells in your blood. That can "
            "make it easier to get an infection or to bruise."
        ),
        mitigation="Full blood counts are done at screening and at the end of study.",
        evidence="Label text describes neutropenia, anaemia or thrombocytopenia",
        applies_when="The label reports bone-marrow or blood-count effects",
    ),
    RiskItem(
        key="infection_risk",
        label="Higher chance of infection",
        category="drug",
        likelihood="uncommon",
        plain_language=(
            "This medicine can weaken the way your body fights infection. Tell the "
            "study staff at once if you get a fever or feel unwell."
        ),
        mitigation="You are screened for infection before dosing and watched afterwards.",
        evidence="Label text describes immunosuppression or serious infection",
        applies_when="The product suppresses the immune system",
    ),
)

_RISK_BY_KEY = {r.key: r for r in _RISK_CATALOGUE}

# Regex cues matched case-insensitively against the concatenated label text
# already fetched for the protocol (DailyMed sections, plus whatever adverse
# reaction text a caller supplies). Grounding the risk list in the real label is
# the same discipline design_decisions.py uses for route/device/site.
_RISK_CUES: dict[str, tuple[str, ...]] = {
    "hypoglycemia": (r"hypoglyc", r"\binsulin\b", r"blood glucose lowering", r"sulfonylurea"),
    "bleeding": (
        r"bleed", r"h(a)?emorrhage", r"anticoagul", r"\binr\b",
        r"prothrombin time", r"antiplatelet",
    ),
    "qt_prolongation": (r"qt(c)?\s*(interval\s*)?prolong", r"torsade", r"ventricular arrhythmi"),
    "hepatotoxicity": (
        r"hepatotox", r"liver injury", r"transaminase", r"\balt\b elevation",
        r"jaundice", r"hepatic failure",
    ),
    "nephrotoxicity": (r"nephrotox", r"acute kidney injury", r"renal failure", r"renal toxicity"),
    "gi_upset": (r"nausea", r"vomit", r"diarrh", r"dyspepsia", r"abdominal pain"),
    "somnolence": (r"somnolen", r"drowsi", r"sedation", r"dizziness", r"impair.{0,20}driv"),
    "hypotension": (r"hypotension", r"orthostatic", r"bradycardi", r"syncope"),
    "lactic_acidosis": (r"lactic acidosis",),
    "skin_reaction": (r"\brash\b", r"stevens-johnson", r"toxic epidermal", r"urticaria"),
    "photosensitivity": (r"photosensitiv", r"phototox"),
    "injection_site": (
        r"injection site", r"subcutaneous(ly)?\b", r"intramuscular(ly)?\b",
        r"pre-?filled syringe", r"auto-?injector",
    ),
    "infusion_reaction": (r"infusion[- ]related reaction", r"infusion reaction", r"\biv infusion\b"),
    "immunogenicity": (
        r"immunogenic", r"anti-?drug antibod", r"neutrali[sz]ing antibod", r"\bada\b",
    ),
    "myelosuppression": (
        r"neutropeni", r"myelosuppress", r"thrombocytopeni", r"an(a)?emia",
        r"agranulocytosis",
    ),
    "infection_risk": (
        r"immunosuppress", r"serious infection", r"tuberculosis", r"opportunistic infection",
    ),
}


# ---------------------------------------------------------------------------
# Restriction catalogue
# ---------------------------------------------------------------------------
# What the subject must actually DO and avoid. A generic restriction list that
# does not match the protocol's exclusion criteria is a finding, so the
# drug-specific entries are again detected from the real label text.


@dataclass(frozen=True)
class Restriction:
    key: str
    label: str
    plain_language: str
    window: str            # when the restriction runs
    rationale: str         # why it exists (scientific, not moral)
    evidence: str
    applies_when: str
    always: bool = False
    oral_only: bool = False   # meaningless for an injected product
    detected: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _restriction_catalogue() -> tuple[Restriction, ...]:
    """Built by a function so the window strings can quote the named constants
    rather than repeat their values as literals."""
    return (
        Restriction(
            key="fasting",
            label="Not eating before your dose",
            plain_language=(
                f"You must not eat for at least {_FAST_BEFORE_DOSE_HOURS:.0f} hours "
                f"before your dose. You take the dose with about "
                f"{_DOSING_WATER_ML:.0f} mL of water. You then wait about "
                f"{_FAST_AFTER_DOSE_HOURS:.0f} hours before your next meal."
            ),
            window=(
                f"{_FAST_BEFORE_DOSE_HOURS:.0f} h before dosing until "
                f"{_FAST_AFTER_DOSE_HOURS:.0f} h after dosing"
            ),
            rationale=(
                "Food changes how fast and how much drug is absorbed, so every "
                "subject must be dosed under the same fasting conditions."
            ),
            evidence="Fasting conditions specified by the bioequivalence guidance",
            applies_when="An orally administered product dosed under fasting conditions",
            always=True,
            oral_only=True,
        ),
        Restriction(
            key="water",
            label="Drinking water around your dose",
            plain_language=(
                f"You must not drink water for {_WATER_RESTRICTION_HOURS:.0f} hour "
                f"before your dose or for {_WATER_RESTRICTION_HOURS:.0f} hour after "
                "it. The water you take the dose with is allowed."
            ),
            window=(
                f"{_WATER_RESTRICTION_HOURS:.0f} h before until "
                f"{_WATER_RESTRICTION_HOURS:.0f} h after dosing"
            ),
            rationale="Fluid volume changes stomach emptying and so changes absorption.",
            evidence="Standard bioequivalence dosing condition",
            applies_when="An orally administered product dosed under fasting conditions",
            always=True,
            oral_only=True,
        ),
        Restriction(
            key="posture",
            label="Staying upright after your dose",
            plain_language=(
                f"You must stay sitting or standing for about "
                f"{_UPRIGHT_AFTER_DOSE_HOURS:.0f} hours after your dose. Do not lie "
                "down unless staff ask you to."
            ),
            window=f"First {_UPRIGHT_AFTER_DOSE_HOURS:.0f} h after dosing",
            rationale="Lying down slows stomach emptying and changes the peak level.",
            evidence="Standard bioequivalence dosing condition",
            applies_when="An orally administered product",
            always=True,
            oral_only=True,
        ),
        Restriction(
            key="alcohol",
            label="Alcohol",
            plain_language=(
                f"You must not drink any alcohol from "
                f"{_ALCOHOL_RESTRICTION_HOURS:.0f} hours before each dose until your "
                "last blood sample of that stay."
            ),
            window=f"{_ALCOHOL_RESTRICTION_HOURS:.0f} h before dosing to last sample",
            rationale=(
                "Alcohol changes liver enzyme activity and stomach emptying, and can "
                "add side effects of its own."
            ),
            evidence="Standard phase-1 restriction",
            applies_when="Always",
            always=True,
        ),
        Restriction(
            key="caffeine",
            label="Coffee, tea, cola and energy drinks",
            plain_language=(
                f"You must not have drinks with caffeine for "
                f"{_CAFFEINE_RESTRICTION_HOURS:.0f} hours before each dose. You must "
                "also avoid them while you are at the unit. This means coffee, tea, "
                "cola and energy drinks."
            ),
            window=f"{_CAFFEINE_RESTRICTION_HOURS:.0f} h before dosing and during confinement",
            rationale=(
                "Caffeine and related compounds act on the same liver enzyme "
                "(CYP1A2) as many drugs and can change blood pressure and pulse."
            ),
            evidence="Standard phase-1 restriction",
            applies_when="Always",
            always=True,
        ),
        Restriction(
            key="tobacco",
            label="Smoking and nicotine",
            plain_language=(
                "You must not smoke or use any nicotine product during the study. "
                "You must also be a non-smoker to join."
            ),
            window="From screening to the end of the study",
            rationale=(
                "Tobacco smoke speeds up a liver enzyme (CYP1A2) and would change "
                "how much drug stays in your blood."
            ),
            evidence="Non-smoker inclusion criterion",
            applies_when="Always in a healthy-volunteer study",
            always=True,
        ),
        Restriction(
            key="other_medicines",
            label="Other medicines, including ones you buy yourself",
            plain_language=(
                f"You must not take any other medicine for {_CONMED_WASHOUT_DAYS} "
                "days before your first dose. You must also avoid them until the "
                "study ends. This includes medicines you buy without a prescription, "
                "and vitamins. Ask the study doctor first if you think you need one."
            ),
            window=f"{_CONMED_WASHOUT_DAYS} days before first dose to end of study",
            rationale=(
                "Other medicines can change how the study drug is absorbed or "
                "cleared, which would make the results impossible to interpret."
            ),
            evidence="Standard prohibited-medication exclusion",
            applies_when="Always",
            always=True,
        ),
        Restriction(
            key="herbal_inducers",
            label="Herbal products such as St John's wort",
            plain_language=(
                f"You must not take herbal products for "
                f"{_ENZYME_INDUCER_WASHOUT_DAYS} days before your first dose. You "
                "must also avoid them until the study ends. St John's wort is one "
                "example."
            ),
            window=f"{_ENZYME_INDUCER_WASHOUT_DAYS} days before first dose to end of study",
            rationale=(
                "These products switch on liver enzymes, and it takes weeks for that "
                "effect to wear off."
            ),
            evidence="Enzyme-induction reversal time (2-4 weeks)",
            applies_when="Always",
            always=True,
        ),
        Restriction(
            key="exercise",
            label="Hard exercise",
            plain_language=(
                f"You must not do hard exercise for "
                f"{_EXERCISE_RESTRICTION_HOURS:.0f} hours before each dose or while "
                "you are at the unit."
            ),
            window=f"{_EXERCISE_RESTRICTION_HOURS:.0f} h before dosing and during confinement",
            rationale=(
                "Hard exercise changes blood flow and can raise muscle blood tests, "
                "which makes safety results hard to read."
            ),
            evidence="Standard phase-1 restriction",
            applies_when="Always",
            always=True,
        ),
        Restriction(
            key="blood_donation",
            label="Giving blood",
            plain_language=(
                f"You must not give blood or plasma for "
                f"{_BLOOD_DONATION_RESTRICTION_DAYS} days before screening or for "
                f"{_BLOOD_DONATION_RESTRICTION_DAYS} days after your last blood "
                "sample."
            ),
            window=(
                f"{_BLOOD_DONATION_RESTRICTION_DAYS} days before screening and after "
                "the last sample"
            ),
            rationale=(
                "The study already takes a set amount of blood. Giving blood as well "
                "would take you over safe limits."
            ),
            evidence="Cumulative blood-volume limit",
            applies_when="Always",
            always=True,
        ),
        Restriction(
            key="contraception",
            label="Birth control",
            plain_language=(
                f"If you can become pregnant, you must use effective birth control "
                f"from screening until {_CONTRACEPTION_DAYS_FEMALE} days after your "
                f"last dose. If you can father a child, you must not donate sperm "
                f"for {_CONTRACEPTION_DAYS_MALE} days after your last dose."
            ),
            window=(
                f"Screening to {_CONTRACEPTION_DAYS_FEMALE} days (female) / "
                f"{_CONTRACEPTION_DAYS_MALE} days (male) after the last dose"
            ),
            rationale=(
                "The effect of the study drug on an unborn baby is not fully known, "
                "and sperm take about three months to be replaced."
            ),
            evidence="Reproductive-risk element (21 CFR 50.25(b)(1))",
            applies_when="Always",
            always=True,
        ),
        # ---- label-detected restrictions ----------------------------------
        Restriction(
            key="grapefruit",
            label="Grapefruit and Seville oranges",
            plain_language=(
                f"You must not eat grapefruit or Seville oranges for "
                f"{_GRAPEFRUIT_RESTRICTION_DAYS} days before your first dose. You "
                "must also avoid them until the study ends. This includes their "
                "juice."
            ),
            window=f"{_GRAPEFRUIT_RESTRICTION_DAYS} days before first dose to end of study",
            rationale=(
                "Grapefruit blocks a gut enzyme (CYP3A4) that breaks this drug down, "
                "which can push your blood level much higher than planned."
            ),
            evidence="Label text describes CYP3A4 metabolism or a grapefruit interaction",
            applies_when="The drug is cleared by CYP3A4 or the label names grapefruit",
        ),
        Restriction(
            key="acid_suppressants",
            label="Antacids and heartburn medicines",
            plain_language=(
                "You must not take antacids or heartburn medicines, such as omeprazole "
                "or ranitidine, during the study."
            ),
            window="From screening to the end of the study",
            rationale=(
                "These medicines raise the acid level in your stomach, and this drug "
                "needs stomach acid to dissolve properly."
            ),
            evidence="Label text describes pH-dependent absorption or an acid-suppressant interaction",
            applies_when="The drug's dissolution depends on gastric pH",
        ),
        Restriction(
            key="enzyme_interactions",
            label="Medicines that change liver enzymes",
            plain_language=(
                "You must not take medicines that speed up or slow down the liver "
                "enzymes that break this drug down. The study doctor will check every "
                "medicine you take."
            ),
            window="From screening to the end of the study",
            rationale=(
                "These medicines would change your blood level of the study drug and "
                "make the comparison invalid."
            ),
            evidence="Label text names CYP inhibitors or inducers",
            applies_when="The label describes CYP-mediated drug interactions",
        ),
        Restriction(
            key="dairy_and_minerals",
            label="Dairy foods, antacids and iron or calcium tablets",
            plain_language=(
                "You must not have dairy foods, or tablets with iron, calcium, "
                "magnesium or zinc, in the hours around your dose."
            ),
            window="From 2 h before to 2 h after each dose",
            rationale=(
                "These bind to the drug in the gut and stop it being absorbed "
                "properly."
            ),
            evidence="Label text describes chelation or a cation interaction",
            applies_when="The drug forms complexes with di- or trivalent cations",
        ),
        Restriction(
            key="sun_exposure",
            label="Strong sunlight and sunbeds",
            plain_language=(
                "You must stay out of strong sunlight and must not use a sunbed "
                "during the study. Use sun cream and cover up outdoors."
            ),
            window="From first dose to the end of the study",
            rationale="This drug can make your skin burn much more easily in the sun.",
            evidence="Label text describes photosensitivity",
            applies_when="The label reports photosensitivity",
        ),
        Restriction(
            key="driving",
            label="Driving and using machines",
            plain_language=(
                "You must not drive or use machines after your dose until the study "
                "doctor tells you it is safe."
            ),
            window="From dosing until the study doctor clears you",
            rationale="This drug can make you sleepy or dizzy.",
            evidence="Label text describes somnolence, sedation or dizziness",
            applies_when="The label reports drowsiness, sedation or dizziness",
        ),
    )


_RESTRICTION_CUES: dict[str, tuple[str, ...]] = {
    "grapefruit": (r"grapefruit", r"cyp\s?3a4", r"cyp\s?3a\b", r"seville"),
    "acid_suppressants": (
        r"gastric ph", r"ph[- ]dependent", r"proton pump inhibitor", r"\bppi\b",
        r"h2[- ]?(?:receptor )?antagonist", r"antacid", r"achlorhydria",
    ),
    "enzyme_interactions": (
        r"cyp\s?\d[a-z]?\d*", r"enzyme induc", r"enzyme inhibit",
        r"strong inhibitor", r"strong inducer",
    ),
    "dairy_and_minerals": (
        r"chelat", r"polyvalent cation", r"divalent cation", r"\bantacid",
        r"calcium, magnesium", r"iron salts",
    ),
    "sun_exposure": (r"photosensitiv", r"phototox"),
    "driving": (r"somnolen", r"drowsi", r"sedation", r"impair.{0,20}driv"),
}


# ---------------------------------------------------------------------------
# Plain-language guard
# ---------------------------------------------------------------------------
# Jargon the narration layer must not reintroduce. The left-hand side is what a
# protocol writer types by reflex; the right-hand side is what the subject can
# actually read. Used twice: to flag drift in generated text, and to instruct the
# model in icf_directive().

_JARGON_SUBSTITUTIONS: dict[str, str] = {
    "adverse event": "side effect",
    "administer": "give",
    "administration": "giving the medicine",
    "prior to": "before",
    "subsequent to": "after",
    "discontinue": "stop",
    "terminate": "stop",
    "randomization": "chosen by chance",
    "randomisation": "chosen by chance",
    "randomized": "chosen by chance",
    "venipuncture": "blood draw",
    "cannula": "small tube in a vein",
    "pharmacokinetic": "how the medicine moves through your body",
    "bioequivalence": "showing the two medicines behave the same way",
    "bioavailability": "how much medicine reaches your blood",
    "washout": "gap between study stays",
    "confinement": "staying at the unit",
    "utilize": "use",
    "commence": "start",
    "physician": "doctor",
    "hypoglycemia": "low blood sugar",
    "hypoglycaemia": "low blood sugar",
    "hypersensitivity": "allergic reaction",
    "anaphylaxis": "severe allergic reaction",
    "contraindicated": "not allowed",
    "in the event that": "if",
}

# Phrases in which a jargon term is unavoidable and correct - the FDA's own
# name contains "Administration", and 21 CFR 50.25(a)(5) requires the agency to
# be named. These are removed before the jargon scan so the guard does not raise
# a finding nobody can act on.
_JARGON_SAFE_CONTEXTS: tuple[str, ...] = (
    "food and drug administration",
)


def _count_syllables(word: str) -> int:
    """Vowel-group syllable estimate. Deliberately simple and dependency-free -
    it is a screening heuristic for a grade-level score, not a lexicon."""
    w = re.sub(r"[^a-z]", "", word.lower())
    if not w:
        return 0
    groups = re.findall(r"[aeiouy]+", w)
    n = len(groups)
    # A trailing silent 'e' is not a syllable ("make"), but "-le" is ("simple").
    if w.endswith("e") and not w.endswith(("le", "ee", "ye")) and n > 1:
        n -= 1
    return max(1, n)


def readability_grade(text: str) -> dict[str, Any]:
    """Flesch-Kincaid grade level plus the two things the score alone hides.

    Returns the grade, whether it meets ``_TARGET_READING_GRADE``, the sentences
    longer than ``_LONG_SENTENCE_WORDS`` words, and any protocol jargon found
    with the plain word that should replace it. Empty text scores 0.0 and passes
    rather than dividing by zero.
    """
    clean = re.sub(r"\s+", " ", (text or "").strip())
    if not clean:
        return {
            "words": 0, "sentences": 0, "syllables": 0,
            "grade_level": 0.0, "target_grade": _TARGET_READING_GRADE,
            "meets_target": True, "long_sentences": [], "jargon": [],
        }
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", clean) if s.strip()]
    words = re.findall(r"[A-Za-z][A-Za-z'-]*", clean)
    n_sent = max(1, len(sentences))
    n_words = max(1, len(words))
    n_syll = sum(_count_syllables(w) for w in words)
    grade = 0.39 * (n_words / n_sent) + 11.8 * (n_syll / n_words) - 15.59

    long_sentences = [
        s for s in sentences
        if len(re.findall(r"[A-Za-z][A-Za-z'-]*", s)) > _LONG_SENTENCE_WORDS
    ]
    scannable = clean.lower()
    for safe in _JARGON_SAFE_CONTEXTS:
        scannable = scannable.replace(safe, " ")
    jargon = [
        {"term": term, "use_instead": plain}
        for term, plain in _JARGON_SUBSTITUTIONS.items()
        if re.search(r"\b" + re.escape(term) + r"\b", scannable)
    ]
    return {
        "words": len(words),
        "sentences": len(sentences),
        "syllables": n_syll,
        "grade_level": round(grade, 1),
        "target_grade": _TARGET_READING_GRADE,
        "meets_target": grade <= _TARGET_READING_GRADE,
        "long_sentences": long_sentences,
        "jargon": jargon,
    }


# ---------------------------------------------------------------------------
# Context access
# ---------------------------------------------------------------------------
# The caller passes the same context dict routers/protocols.py::_build_context
# assembles. ICF-specific hints may arrive either at the top level or inside
# population_config, so every lookup checks both.


def _ctx(context: dict[str, Any], key: str, default: Any = None) -> Any:
    if key in context and context[key] is not None:
        return context[key]
    pop = context.get("population_config")
    if isinstance(pop, dict) and pop.get(key) is not None:
        return pop[key]
    return default


def _label_text(context: dict[str, Any]) -> str:
    """Every label section we have, concatenated. Deliberately reads ALL values
    rather than a fixed key list so that if the SPL section map is widened later
    (e.g. to add Adverse Reactions, LOINC 34084-4) the new text is picked up
    without a change here."""
    label = context.get("label_data") or _ctx(context, "_label") or {}
    if not isinstance(label, dict):
        return ""
    parts = [str(v) for v in label.values() if isinstance(v, (str, int, float))]
    # Free-text safety notes a caller may pass alongside the label.
    extra = _ctx(context, "safety_text")
    if isinstance(extra, str):
        parts.append(extra)
    return " ".join(parts)


def _detect(text: str, cues: dict[str, tuple[str, ...]]) -> set[str]:
    """Catalogue keys the label text gives positive evidence for."""
    if not text:
        return set()
    lowered = text.lower()
    return {
        key for key, patterns in cues.items()
        if any(re.search(p, lowered) for p in patterns)
    }


def _route_info(text: str, study_type: str) -> dict[str, Any]:
    """Is the product swallowed or injected, and on what evidence?

    This matters more than it looks. A consent form for an injected biosimilar
    that tells the subject to swallow the dose with 240 mL of water, to stay
    upright for four hours, and to fast for ten hours has copied an oral BE
    template - it contradicts its own protocol on the single procedure the
    subject cares about most.

    Route comes from the REAL label text via
    :func:`services.design_decisions.extract_label_routes`, which is negation
    aware (a route the label rules out with "do not administer intravenously"
    is not counted as offered). With no label, study type decides: a
    biosimilar PK study is an injected biologic, everything else is oral.
    """
    routes = list(extract_label_routes(text)) if text else []
    if "oral" in routes:
        return {"routes": routes, "is_oral": True, "basis": "label text states an oral route"}
    if routes:
        return {
            "routes": routes,
            "is_oral": False,
            "basis": f"label text states {'/'.join(routes)} only",
        }
    is_oral = study_type != "biosimilar_pk"
    return {
        "routes": [],
        "is_oral": is_oral,
        "basis": (
            "no route found in the label - inferred from study type "
            f"({study_type}); confirm against the real label"
        ),
    }


def _median_t_half(context: dict[str, Any]) -> Optional[float]:
    """Median terminal half-life from the locked design first, then from the
    extracted PK records. Never re-derived from anything else, so the ICF's
    washout can only ever equal the protocol's."""
    ss = context.get("sample_size") or {}
    if isinstance(ss, dict) and ss.get("t_half_hours"):
        try:
            return float(ss["t_half_hours"])
        except (TypeError, ValueError):
            pass
    vals: list[float] = []
    for p in context.get("pk_parameters") or []:
        if not isinstance(p, dict):
            continue
        if str(p.get("parameter")) in ("t_half", "t_1/2", "half_life") and p.get("value"):
            try:
                v = float(p["value"])
            except (TypeError, ValueError):
                continue
            if v > 0:
                vals.append(v)
    if not vals:
        return None
    vals.sort()
    mid = len(vals) // 2
    return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2.0


# ---------------------------------------------------------------------------
# Deterministic study facts
# ---------------------------------------------------------------------------


def _n_periods(ss: dict[str, Any]) -> tuple[int, str]:
    """Number of dosing periods, read from the locked design label where possible
    so the ICF cannot state a different number from the protocol."""
    label = str(ss.get("design") or "")
    m = re.search(r"(\d+)\s*-?\s*period", label, re.IGNORECASE)
    if m:
        return int(m.group(1)), "protocol design label"
    key = ss.get("design_key")
    if key == "parallel":
        return 1, "parallel-group design - each subject is dosed once"
    if key == "replicate":
        # A fully replicated NTI design is 4-period; a partial replicate is 3.
        return (4 if ss.get("is_nti") else 3), "replicate design"
    return 2, "two-period crossover"


def _washout(ss: dict[str, Any], t_half: Optional[float]) -> dict[str, Any]:
    """Washout between periods, computed with the SAME rule and formatting the
    protocol's locked-design block uses (7 x t1/2, hours below one day). Any
    divergence here would put a different number in the two documents."""
    if ss.get("design_key") == "parallel":
        return {
            "applicable": False,
            "hours": None,
            "days": None,
            "protocol_text": "Not applicable (parallel-group design)",
            "plain": "You take the study medicine once, so there is no gap to wait through.",
            "basis": "parallel-group design",
        }
    if not t_half:
        return {
            "applicable": True,
            "hours": None,
            "days": None,
            "protocol_text": _PLACEHOLDER,
            "plain": (
                "There is a gap between each stay so the medicine leaves your body. "
                "The study doctor will tell you how long it is."
            ),
            "basis": "no half-life available - must be completed before IRB submission",
        }
    hours = WASHOUT_HALF_LIVES * float(t_half)
    if hours < 24.0:
        text = f">= {math.ceil(hours)} hours"
        plain = (
            f"There is a gap of at least {math.ceil(hours)} hours between stays so "
            "the medicine leaves your body."
        )
    else:
        text = f">= {hours / 24.0:.1f} days"
        plain = (
            f"There is a gap of about {hours / 24.0:.0f} days between stays so the "
            "medicine leaves your body."
        )
    return {
        "applicable": True,
        "hours": round(hours, 1),
        "days": round(hours / 24.0, 1),
        "protocol_text": text,
        "plain": plain,
        "basis": f"{WASHOUT_HALF_LIVES:.0f} x terminal half-life (t1/2 = {float(t_half):.1f} h)",
    }


def _blood(context: dict[str, Any], n_periods: int) -> dict[str, Any]:
    """Number of draws and total volume - the two numbers a subject and an IRB
    look for first, and the two most often left generic."""
    schedule = _ctx(context, "sampling_schedule_hours")
    assumed_draws = False
    if isinstance(schedule, (list, tuple)) and schedule:
        draws_per_period = len(schedule)
        draw_basis = "protocol PK sampling schedule"
    else:
        explicit = _ctx(context, "n_pk_draws_per_period")
        if isinstance(explicit, int) and explicit > 0:
            draws_per_period = explicit
            draw_basis = "caller-supplied draw count"
        else:
            draws_per_period = _ASSUMED_PK_DRAWS_PER_PERIOD
            draw_basis = (
                f"ASSUMED {_ASSUMED_PK_DRAWS_PER_PERIOD} draws per period "
                "(1 pre-dose + 16 post-dose, the upper end of the protocol's "
                "12-16 post-dose requirement)"
            )
            assumed_draws = True

    pk_draws = draws_per_period * n_periods
    safety_panels = _SAFETY_PANELS_FIXED + n_periods
    pk_ml = pk_draws * _BLOOD_ML_PER_PK_DRAW
    safety_ml = safety_panels * _BLOOD_ML_PER_SAFETY_PANEL
    total_ml = pk_ml + safety_ml

    # Plain-language comparator, COMPUTED from the total rather than asserted.
    # A dense schedule over three or four periods genuinely exceeds one whole-
    # blood donation, and a form that prints "588 mL ... less than one blood
    # donation, which is about 470 mL" contradicts itself on the same line and
    # UNDERSTATES the burden the subject is consenting to - the unsafe
    # direction. The comparison therefore tracks the real number in both
    # directions.
    if total_ml <= _BLOOD_DONATION_ML:
        donation_comparison = (
            f"that is less than one blood donation, which is about "
            f"{_BLOOD_DONATION_ML:.0f} mL"
        )
    elif total_ml <= 2.0 * _BLOOD_DONATION_ML:
        donation_comparison = (
            f"that is more than one blood donation, which is about "
            f"{_BLOOD_DONATION_ML:.0f} mL, but it is spread out over the whole "
            "study and is not taken at once"
        )
    else:
        donation_comparison = (
            f"that is about {total_ml / _BLOOD_DONATION_ML:.0f} times one blood "
            f"donation, which is about {_BLOOD_DONATION_ML:.0f} mL, spread out "
            "over the whole study"
        )
    return {
        "pk_draws_per_period": draws_per_period,
        "pk_draws_total": pk_draws,
        "ml_per_pk_draw": _BLOOD_ML_PER_PK_DRAW,
        "safety_panels": safety_panels,
        "ml_per_safety_panel": _BLOOD_ML_PER_SAFETY_PANEL,
        "pk_volume_ml": round(pk_ml, 1),
        "safety_volume_ml": round(safety_ml, 1),
        "total_volume_ml": round(total_ml, 1),
        "total_tablespoons": round(total_ml / _ML_PER_TABLESPOON, 1),
        "donation_comparison_ml": _BLOOD_DONATION_ML,
        "donation_multiple": round(total_ml / _BLOOD_DONATION_ML, 2),
        "exceeds_one_donation": total_ml > _BLOOD_DONATION_ML,
        "donation_comparison": donation_comparison,
        "basis": draw_basis,
        "assumed": assumed_draws,
    }


def _duration(
    context: dict[str, Any],
    n_periods: int,
    washout: dict[str, Any],
    study_type: str,
) -> dict[str, Any]:
    """How long the subject is committed for, split into the parts they feel:
    screening, nights in the unit, gaps between stays, follow-up."""
    schedule = _ctx(context, "sampling_schedule_hours")
    last_sample_h = None
    if isinstance(schedule, (list, tuple)) and schedule:
        try:
            last_sample_h = float(max(float(x) for x in schedule))
        except (TypeError, ValueError):
            last_sample_h = None

    confine_after = _ctx(context, "confinement_hours")
    if isinstance(confine_after, (int, float)) and confine_after > 0:
        confine_after = float(confine_after)
        confine_basis = "caller-supplied confinement duration"
    else:
        # Confinement runs to the 24 h sample, or to the last sample if the
        # schedule stops earlier - never longer than needed.
        confine_after = _CONFINEMENT_AFTER_DOSE_HOURS
        if last_sample_h is not None:
            confine_after = min(_CONFINEMENT_AFTER_DOSE_HOURS, last_sample_h)
        confine_basis = (
            f"ASSUMED confinement to the {_CONFINEMENT_AFTER_DOSE_HOURS:.0f} h "
            "sample, the standard single-dose BE convention"
        )

    # Check-in the evening before dosing for the overnight fast, then one night
    # per full 24 h of post-dose confinement.
    nights_per_period = 1 + max(1, math.ceil(confine_after / 24.0))
    days_per_period = nights_per_period + 1

    ambulatory_visits = 0
    if last_sample_h is not None and last_sample_h > confine_after:
        ambulatory_visits = sum(
            1 for x in schedule if float(x) > confine_after
        )

    follow_up_days = (
        _BIOSIMILAR_EOS_DAYS if study_type == "biosimilar_pk" else _FOLLOW_UP_DAYS
    )
    washout_days = washout.get("days") or 0
    total_days = (
        _SCREENING_WINDOW_DAYS
        + n_periods * days_per_period
        + max(0, n_periods - 1) * washout_days
        + follow_up_days
    )
    return {
        "screening_window_days": _SCREENING_WINDOW_DAYS,
        "periods": n_periods,
        "fast_before_dose_hours": _FAST_BEFORE_DOSE_HOURS,
        "confinement_after_dose_hours": confine_after,
        "confinement_basis": confine_basis,
        "nights_per_period": nights_per_period,
        "days_per_period": days_per_period,
        "ambulatory_visits_per_period": ambulatory_visits,
        "washout_days_between_periods": washout_days if washout.get("applicable") else 0,
        "follow_up_days": follow_up_days,
        "total_days": int(math.ceil(total_days)),
        "total_weeks": round(total_days / 7.0, 1),
    }


def _blood_volume_check(blood: dict[str, Any], duration: dict[str, Any]) -> dict[str, Any]:
    """Is the collected volume inside the accepted healthy-adult ceiling?

    Compared over the 8-week window the convention is written against, not over
    the whole study: a long-washout crossover can run past 8 weeks, in which case
    only the periods that fall inside one window count. Reporting the whole-study
    total against an 8-week limit would raise a false alarm; reporting nothing
    would miss a real one.
    """
    cycle_days = duration["days_per_period"] + duration["washout_days_between_periods"]
    cycle_days = max(1.0, float(cycle_days))
    periods_in_window = max(1, min(
        duration["periods"], int(_BLOOD_VOLUME_WINDOW_DAYS // cycle_days) or 1
    ))
    per_period_ml = (
        blood["pk_draws_per_period"] * blood["ml_per_pk_draw"]
        + blood["ml_per_safety_panel"]
    )
    # The screening and end-of-study panels sit inside whichever window they fall
    # in, so count them once against the worst case.
    window_ml = periods_in_window * per_period_ml + _SAFETY_PANELS_FIXED * blood["ml_per_safety_panel"]
    return {
        "ceiling_ml": _BLOOD_VOLUME_CEILING_ML,
        "window_days": _BLOOD_VOLUME_WINDOW_DAYS,
        "periods_in_window": periods_in_window,
        "volume_in_window_ml": round(window_ml, 1),
        "within_ceiling": window_ml <= _BLOOD_VOLUME_CEILING_ML,
        "basis": (
            f"{_BLOOD_VOLUME_CEILING_ML:.0f} mL per {_BLOOD_VOLUME_WINDOW_DAYS}-day "
            "window, matching the protocol's Study Assessments limit"
        ),
    }


# ---------------------------------------------------------------------------
# Section model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IcfSection:
    key: str
    heading: str
    cfr_refs: tuple[str, ...]
    purpose: str                 # what this section must accomplish
    body: tuple[str, ...] = ()   # deterministic paragraphs
    bullets: tuple[str, ...] = ()
    verbatim: bool = False       # statutory text - must not be reworded
    field_list: bool = False     # labelled fields, not prose - not gradable
    narration: str = ""          # instruction for the AI narration layer
    applicable: bool = True
    applicability_note: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["cfr_refs"] = list(self.cfr_refs)
        d["body"] = list(self.body)
        d["bullets"] = list(self.bullets)
        return d


def _terminate(fragment: str) -> str:
    """End a body paragraph or bullet with sentence punctuation.

    Without this the readability guard joins a lead-in ending in ':' onto the
    first bullet and reports one enormous sentence that does not exist on the
    page - a false finding that would train a reader to ignore the guard.
    """
    text = (fragment or "").strip()
    if not text:
        return ""
    return text if text[-1] in ".!?" else text + "."


# How each administration route is described to a lay reader. The technical
# route code is never shown to the subject - "subcutaneous" is exactly the kind
# of word the plain-language guard exists to keep off the page.
_ROUTE_PLAIN: dict[str, str] = {
    "SC": "as an injection under the skin",
    "IV": "as a drip into a vein",
    "IM": "as an injection into a muscle",
    "intravitreal": "as an injection into the eye",
    "oral": "by mouth",
}


def _route_plain(route: dict[str, Any]) -> str:
    """Plain-language description of how the dose is given.

    Falls back to a neutral phrase rather than guessing when the label named no
    route - an unstated route becomes a pending field, not an invented one.
    """
    for code in route.get("routes") or []:
        if code in _ROUTE_PLAIN:
            return _ROUTE_PLAIN[code]
    return "by the study staff, in the way the study doctor explains to you"


def _fmt(value: Any, unit: str = "") -> str:
    """Render a computed number, or the placeholder token when it is missing, so
    an incomplete form is visibly incomplete rather than silently vague."""
    if value is None:
        return _PLACEHOLDER
    if isinstance(value, float):
        text = f"{value:.0f}" if abs(value - round(value)) < 0.05 else f"{value:.1f}"
    else:
        text = str(value)
    return f"{text} {unit}".strip()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_icf(protocol_context: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Build the standalone Informed Consent Form content model for THIS study.

    Parameters
    ----------
    protocol_context:
        The context dict routers/protocols.py::_build_context assembles -
        ``drug_inn``, ``study_type``, ``population_config``, ``sample_size``,
        ``label_data``, ``pk_parameters``, ``guideline_version`` and the optional
        hint keys. ICF-specific hints (``icf_version``, ``protocol_number``,
        ``sampling_schedule_hours``, ``confinement_hours``,
        ``prohibited_medications``, the three contacts, ``compensation_text``)
        may sit at the top level or inside ``population_config``. Everything is
        optional: absent inputs degrade to a named assumption or a visible
        placeholder, never to a silently wrong number.

    Returns
    -------
    dict with ``version``, ``study_facts``, ``sections`` (ordered), ``risks``,
    ``restrictions``, ``benefits``, ``signature_block``, ``elements_coverage``,
    ``missing_required_elements``, ``assumptions``, ``pending_fields`` and
    ``readability``. JSON-serialisable throughout - it is persisted inside
    population_config, a JSONB column.
    """
    context = dict(protocol_context or {})
    pop = context.get("population_config") or {}
    if not isinstance(pop, dict):
        pop = {}

    drug = str(context.get("drug_inn") or _PLACEHOLDER)
    study_type = str(context.get("study_type") or "BE")
    ss = context.get("sample_size") or _ctx(context, "_sample_size") or {}
    if not isinstance(ss, dict):
        ss = {}
    healthy = str(pop.get("subject_type", "healthy")).lower() != "patient"
    guideline_version = str(context.get("guideline_version") or "")
    profile = get_profile(guideline_version)

    text = _label_text(context)
    route = _route_info(text, study_type)
    t_half = _median_t_half(context)
    n_periods, periods_basis = _n_periods(ss)
    washout = _washout(ss, t_half)
    blood = _blood(context, n_periods)
    duration = _duration(context, n_periods, washout, study_type)
    volume_check = _blood_volume_check(blood, duration)

    assumptions: list[dict[str, str]] = []
    if blood["assumed"]:
        assumptions.append({
            "field": "pk_draws_per_period",
            "value": str(blood["pk_draws_per_period"]),
            "why": blood["basis"],
        })
    if duration["confinement_basis"].startswith("ASSUMED"):
        assumptions.append({
            "field": "confinement_after_dose_hours",
            "value": _fmt(duration["confinement_after_dose_hours"], "h"),
            "why": duration["confinement_basis"],
        })
    assumptions.append({
        "field": "ml_per_pk_draw",
        "value": f"{_BLOOD_ML_PER_PK_DRAW:.0f} mL",
        "why": (
            "The protocol allows 4-6 mL per draw; the consent form states the "
            "upper end because a subject consents to a maximum."
        ),
    })
    if not volume_check["within_ceiling"]:
        assumptions.append({
            "field": "total_volume_ml",
            "value": _fmt(volume_check["volume_in_window_ml"], "mL"),
            "why": (
                "EXCEEDS the "
                f"{_BLOOD_VOLUME_CEILING_ML:.0f} mL / "
                f"{_BLOOD_VOLUME_WINDOW_DAYS}-day ceiling - reduce the draw volume "
                "or the number of draws before IRB submission."
            ),
        })

    # --- risks -------------------------------------------------------------
    detected_risks = _detect(text, _RISK_CUES)
    # Route gate. "subcutaneous" appears in plenty of oral labels (comparative
    # pharmacology, animal data), so a raw cue hit is not enough to put an
    # injection-site reaction on an oral BE consent form - and an infusion
    # reaction only exists if there is an infusion.
    if route["is_oral"]:
        detected_risks -= {"injection_site", "infusion_reaction"}
    elif route["routes"] and "IV" not in route["routes"]:
        detected_risks.discard("infusion_reaction")

    risks: list[dict[str, Any]] = []
    considered_risks: list[dict[str, Any]] = []
    for r in _RISK_CATALOGUE:
        # Oral-only procedure risks (the pre-dose fast) are dropped for an
        # injected product, exactly as the matching restriction is - otherwise
        # the risk section and the restriction section of the SAME form
        # disagree about whether the subject has to fast.
        oral_gated = r.oral_only and not route["is_oral"]
        applies = (r.always or r.key in detected_risks) and not oral_gated
        enriched = RiskItem(**{**asdict(r), "detected": r.key in detected_risks}).to_dict()
        considered_risks.append(enriched)
        if applies:
            risks.append(enriched)
    # Procedure and reproductive risks first: they are certain, whereas the drug
    # risks are possibilities. A subject reads the top of the list.
    cat_order = {"procedure": 0, "reproductive": 1, "drug": 2, "unknown": 3}
    risk_order = {r.key: i for i, r in enumerate(_RISK_CATALOGUE)}
    risks.sort(key=lambda d: (cat_order.get(d["category"], 9), risk_order[d["key"]]))

    # --- restrictions ------------------------------------------------------
    catalogue = _restriction_catalogue()
    detected_restrictions = _detect(text, _RESTRICTION_CUES)
    restrictions: list[dict[str, Any]] = []
    for item in catalogue:
        # An oral-only restriction (fasting, dosing water, staying upright) is
        # dropped for an injected product rather than carried over from the oral
        # BE template.
        if item.oral_only and not route["is_oral"]:
            continue
        applies = item.always or item.key in detected_restrictions
        enriched = Restriction(
            **{**asdict(item), "detected": item.key in detected_restrictions}
        ).to_dict()
        if applies:
            restrictions.append(enriched)

    # Real prohibited-medication text from the protocol wins over the generic
    # catalogue entry - a consent form that lists different prohibitions from the
    # protocol's exclusion criteria is a finding.
    prohibited = _ctx(context, "prohibited_medications")
    prohibited_list: list[str] = []
    if isinstance(prohibited, (list, tuple)):
        prohibited_list = [str(x) for x in prohibited if str(x).strip()]
    elif isinstance(prohibited, str) and prohibited.strip():
        prohibited_list = [prohibited.strip()]

    # --- benefits ----------------------------------------------------------
    benefits = _benefits(healthy, drug, study_type)

    # --- versioning --------------------------------------------------------
    version = {
        "icf_version": str(_ctx(context, "icf_version") or "1.0"),
        "icf_date": _ctx(context, "icf_date") or None,
        "protocol_number": _ctx(context, "protocol_number") or None,
        "protocol_version": _ctx(context, "protocol_version") or None,
        "protocol_title": _ctx(context, "protocol_title") or None,
        "guideline_version": guideline_version or profile.id,
        "note": (
            "This consent form is version-controlled separately from the protocol "
            "and is approved separately by the IRB. Its version and date must be "
            "re-issued whenever a protocol amendment changes anything a subject "
            "consented to."
        ),
    }

    facts = {
        "drug": drug,
        "study_type": study_type,
        "subject_population": "healthy volunteers" if healthy else "patients",
        "design": ss.get("design") or _PLACEHOLDER,
        "design_key": ss.get("design_key") or None,
        "route": route,
        "periods": n_periods,
        "periods_basis": periods_basis,
        "n_enrolled": ss.get("n_enrolled"),
        "n_per_group": ss.get("n_per_group"),
        "t_half_hours": t_half,
        "washout": washout,
        "blood": blood,
        "blood_volume_check": volume_check,
        "duration": duration,
        "regulator": profile.regulator,
        "region": profile.region,
    }

    ct_gov = _clinicaltrials_gov(context, healthy)
    sections = _build_sections(
        drug=drug,
        healthy=healthy,
        study_type=study_type,
        facts=facts,
        risks=risks,
        restrictions=restrictions,
        prohibited_list=prohibited_list,
        benefits=benefits,
        version=version,
        context=context,
        profile_region=profile.region,
        ct_gov=ct_gov,
    )

    coverage, missing = _coverage(sections)
    signature_block = _signature_block(healthy)
    pending = _pending_fields(version, context, washout, facts)
    readability = _readability_report(sections)

    return {
        "document_type": "informed_consent_form",
        "title": (
            f"Informed Consent Form - {drug} "
            f"{'bioequivalence' if study_type != 'biosimilar_pk' else 'PK similarity'} study"
        ),
        "regulatory_basis": (
            "21 CFR 50.25 (basic and additional elements of informed consent); "
            "21 CFR 50.20 (general requirements); ICH E6(R2) 4.8; "
            "Declaration of Helsinki (2013)"
        ),
        "version": version,
        "study_facts": facts,
        "sections": [s.to_dict() for s in sections],
        "risks": risks,
        "risks_considered": considered_risks,
        "restrictions": restrictions,
        "prohibited_medications": prohibited_list,
        "benefits": benefits,
        "clinicaltrials_gov": ct_gov,
        "signature_block": signature_block,
        "elements_coverage": coverage,
        "missing_required_elements": missing,
        "assumptions": assumptions,
        "pending_fields": pending,
        "readability": readability,
        "plain_language_substitutions": dict(_JARGON_SUBSTITUTIONS),
    }


def _benefits(healthy: bool, drug: str, study_type: str) -> dict[str, Any]:
    """The honesty gate. A healthy volunteer gets NO therapeutic benefit from a
    single-dose PK study, and a consent form that hints otherwise is coercive.
    This is returned as a computed flag, not left to the narration layer."""
    if healthy:
        return {
            "direct_medical_benefit": False,
            "statement": (
                "You will get no medical benefit from taking part in this study. "
                "You are healthy, and this study is not meant to treat you. The "
                "study medicine will not make you better in any way."
            ),
            "benefit_to_others": (
                f"What we learn may help others later. It may help show that a new "
                f"{drug} product works the same way as the one already approved, so "
                "that more people can get it."
            ),
            "forbidden_framing": (
                "Do NOT state or imply any personal therapeutic benefit, improved "
                "health, free health care, or 'benefit from the study medication'. "
                "The free screening tests are NOT a benefit and must not be "
                "presented as one."
            ),
        }
    return {
        "direct_medical_benefit": None,
        "statement": (
            "You may get no medical benefit from taking part in this study. Any "
            "benefit to you is not certain. The main aim is to learn how the "
            "medicine behaves in the body, not to treat you."
        ),
        "benefit_to_others": (
            "What we learn may help other people with your condition in the future."
        ),
        "forbidden_framing": (
            "Do NOT promise or imply a treatment benefit. State plainly that "
            "benefit is uncertain."
        ),
    }


def _clinicaltrials_gov(context: dict[str, Any], healthy: bool) -> dict[str, Any]:
    """The 21 CFR 50.25(c) clause, and an honest applicability determination.

    Phase 1 drug trials are excluded from the "applicable clinical trial"
    definition, so a single-dose BE/PK study in healthy volunteers is not
    strictly caught by 50.25(c). Many IRBs expect the clause anyway, and
    including it can never be a deficiency, so it is ALWAYS included and its
    status is reported rather than the clause being silently dropped.

    CONSENT FORM ONLY - AND WHY THAT IS NOT INCONSISTENT
    ---------------------------------------------------
    The PROTOCOL deliberately says nothing about ClinicalTrials.gov when the
    study is not an applicable clinical trial: a protocol does not need a
    paragraph explaining why a regulation does not apply to it, and that
    paragraph has been removed from the protocol path (see
    services/safety_reporting.py::_ctgov_obligation). This function is the
    CONSENT FORM path, which is governed by a different rule - 21 CFR 50.25(c)
    prescribes a specific ClinicalTrials.gov sentence for the consent document
    itself. Keeping it here while removing it from the protocol is the correct
    distinction, not a leftover.

    ``basis`` below is an INTERNAL audit record of how the determination was
    made. It is not narrated into the protocol (ai_service does not project it,
    and icf_directive does not render it) and it is not printed in the consent
    document either: the exporter renders ``applicability_note`` only for
    sections marked NOT applicable, and this section is always applicable. What
    the subject actually reads is ``statement``, verbatim - nothing else.
    """
    override = _ctx(context, "clinicaltrials_gov_required")
    if isinstance(override, bool):
        required = override
        basis = "explicitly set by the caller"
    else:
        required = not healthy
        basis = (
            "Phase 1 drug trials are excluded from the 'applicable clinical trial' "
            "definition (FDAAA 801 / 42 CFR 11.10), so this healthy-volunteer "
            "single-dose study is not strictly caught by 21 CFR 50.25(c). The "
            "clause is included anyway because including it is never a deficiency."
            if healthy else
            "This study enrols patients and may meet the 'applicable clinical "
            "trial' definition, so 21 CFR 50.25(c) applies."
        )
    return {
        "statement": CLINICALTRIALS_GOV_STATEMENT,
        "verbatim": True,
        "required": required,
        "included": True,
        "basis": basis,
        "nct_number": _ctx(context, "clinicaltrials_gov_id") or _PLACEHOLDER,
        "warning": (
            "This wording is prescribed by 21 CFR 50.25(c) and must be reproduced "
            "exactly. Do not simplify, shorten, or reformat it."
        ),
    }


def _build_sections(
    *,
    drug: str,
    healthy: bool,
    study_type: str,
    facts: dict[str, Any],
    risks: list[dict[str, Any]],
    restrictions: list[dict[str, Any]],
    prohibited_list: list[str],
    benefits: dict[str, Any],
    version: dict[str, Any],
    context: dict[str, Any],
    profile_region: str,
    ct_gov: dict[str, Any],
) -> list[IcfSection]:
    """The ordered form. Each section names the CFR element(s) it discharges, so
    coverage is computed from the document rather than asserted about it."""
    dur = facts["duration"]
    blood = facts["blood"]
    washout = facts["washout"]
    n_enrolled = facts.get("n_enrolled")
    sponsor = str(_ctx(context, "org_name") or context.get("org_name") or _PLACEHOLDER)

    sections: list[IcfSection] = []

    sections.append(IcfSection(
        key="header",
        heading="Informed Consent Form",
        cfr_refs=(),
        purpose="Identify the study, the sponsor, the site, and the version of this form.",
        body=(
            f"Study title: {version.get('protocol_title') or _PLACEHOLDER}",
            f"Protocol number: {version.get('protocol_number') or _PLACEHOLDER}   "
            f"Protocol version: {version.get('protocol_version') or _PLACEHOLDER}",
            f"Consent form version: {version.get('icf_version')}   "
            f"Consent form date: {version.get('icf_date') or _PLACEHOLDER}",
            f"Sponsor: {sponsor}",
            f"Study doctor: {_ctx(context, 'investigator_name') or _PLACEHOLDER}",
            f"Site: {_ctx(context, 'site_name') or _PLACEHOLDER}",
        ),
        field_list=True,
        narration=(
            "Render exactly as given. Do not add marketing language or the "
            "sponsor's product claims to the header."
        ),
    ))

    sections.append(IcfSection(
        key="invitation_and_research_statement",
        heading="What is this form?",
        cfr_refs=("50.25(a)(1)",),
        purpose="State plainly that this is research and that taking part is a choice.",
        body=(
            "You are being asked to take part in a research study. This is research, "
            "not medical care.",
            "Please read this form. Take as much time as you need. Ask any question "
            "you have. You can talk it over with your family or your own doctor "
            "before you decide.",
            "You will get a signed copy of this form to keep.",
        ),
        narration=(
            "Keep the research statement in the first two sentences. Do not open "
            "with the science."
        ),
    ))

    # Short declarative sentences. The research purpose has to survive a reader
    # who stops after the first line, so the comparison claim comes first and the
    # method second.
    purpose_lead = (
        f"This study looks at a new {drug} product."
        if study_type != "biosimilar_pk"
        else f"This study looks at a new copy of a {drug} product."
    )
    purpose_claim = (
        "We want to know if it puts the same amount of medicine into your blood "
        f"as the {drug} product that doctors already use."
        if study_type != "biosimilar_pk"
        else "We want to know if it acts in your body in the same way as the "
        f"{drug} product that doctors already use."
    )
    sections.append(IcfSection(
        key="purpose",
        heading="Why is this study being done?",
        cfr_refs=("50.25(a)(1)",),
        purpose="Explain the research purpose in one or two plain sentences.",
        body=(
            purpose_lead,
            purpose_claim,
            "To do this we measure how much medicine is in your blood over time. "
            "We then compare the two sets of results.",
            "This study is not testing whether the medicine treats an illness."
            if healthy else
            "This study is about how the medicine moves through the body. It is not "
            "testing how well the medicine treats your illness.",
        ),
        narration=(
            "One idea per sentence. Do not use the words bioequivalence, "
            "pharmacokinetics, AUC or Cmax in this section."
        ),
    ))

    sections.append(IcfSection(
        key="number_of_subjects",
        heading="How many people will take part?",
        cfr_refs=("50.25(b)(6)",),
        purpose="Give the approximate number of subjects.",
        body=(
            f"About {_fmt(n_enrolled)} people will take part in this study."
            if n_enrolled else
            f"About {_PLACEHOLDER} people will take part in this study.",
        ),
        narration="State the number as given. Do not round it further.",
    ))

    duration_bullets = [
        f"Screening visit: within {dur['screening_window_days']} days before your first dose.",
        f"Study stays: {dur['periods']} stay(s) at the unit, "
        f"about {dur['nights_per_period']} night(s) each.",
    ]
    if washout.get("applicable") and washout.get("days"):
        duration_bullets.append(
            f"Gap between stays: about {washout['days']:.0f} days."
        )
    if dur["ambulatory_visits_per_period"]:
        duration_bullets.append(
            f"Return visits for blood samples: about "
            f"{dur['ambulatory_visits_per_period']} per stay."
        )
    duration_bullets.append(
        f"Final check-up: about {dur['follow_up_days']} days after your last dose."
    )
    sections.append(IcfSection(
        key="duration_and_visits",
        heading="How long will I be in the study?",
        cfr_refs=("50.25(a)(1)",),
        purpose="State the expected duration of participation, in the parts a subject feels.",
        body=(
            f"From your screening visit to your last visit, the study lasts about "
            f"{dur['total_weeks']:.0f} weeks in total.",
            "Here is what that time is made up of:",
        ),
        bullets=tuple(duration_bullets),
        narration=(
            "Use the exact numbers given. They come from the same locked design as "
            "the protocol, so changing any of them makes the two documents disagree."
        ),
    ))

    procedure_bullets = [
        "Screening: we check your health with questions, a physical exam, a heart "
        "tracing, and blood and urine tests. We also test for drugs of abuse, "
        "alcohol, and infections such as hepatitis and HIV.",
        "Pregnancy test: if you can become pregnant, we test before screening and "
        "before each dose.",
    ]
    # Route-aware dosing. Telling a subject in an injected-biosimilar study to
    # swallow the dose with 240 mL of water after a 10 h fast is the oral-BE
    # template leaking into the wrong form - it contradicts the protocol on the
    # one procedure the subject cares about most. _route_info() already decided
    # this from the real label; the procedures list must obey it too.
    if facts["route"]["is_oral"]:
        procedure_bullets.append(
            f"Fasting: you must not eat for at least "
            f"{dur['fast_before_dose_hours']:.0f} hours before each dose."
        )
        procedure_bullets.append(
            f"Dosing: you take one dose of the study medicine by mouth with about "
            f"{_DOSING_WATER_ML:.0f} mL of water."
        )
    else:
        procedure_bullets.append(
            f"Dosing: you are given one dose of the study medicine "
            f"{_route_plain(facts['route'])}."
        )
    procedure_bullets += [
        f"Blood samples: about {blood['pk_draws_per_period']} samples during each "
        f"stay, and about {blood['pk_draws_total']} samples in total. A small tube "
        "is put in a vein in your arm so you are not stuck each time.",
        f"Total blood: about {_fmt(blood['total_volume_ml'], 'mL')} over the whole "
        f"study. That is about {blood['total_tablespoons']:.0f} tablespoons, and "
        f"{blood['donation_comparison']}.",
        f"Staying at the unit: about "
        f"{_fmt(dur['confinement_after_dose_hours'], 'hours')} after each dose.",
        "Safety checks: blood pressure, pulse, temperature, heart tracings, and "
        "questions about how you feel.",
    ]
    sections.append(IcfSection(
        key="procedures",
        heading="What will happen to me?",
        cfr_refs=("50.25(a)(1)",),
        purpose="Describe every procedure the subject undergoes, with real numbers.",
        body=(
            "If you agree to take part, this is what will happen:",
        ),
        bullets=tuple(procedure_bullets),
        narration=(
            "Keep every number exactly as given. Do not add procedures that are not "
            "listed, and do not drop any that are."
        ),
    ))

    sections.append(IcfSection(
        key="experimental_procedures",
        heading="Which parts are experimental?",
        cfr_refs=("50.25(a)(1)",),
        purpose="Identify which procedures are experimental (an explicit 50.25(a)(1) element).",
        body=(
            f"The experimental part of this study is taking the new {drug} product. "
            "It is not yet approved for sale.",
            f"The approved {drug} product used for comparison is already on the "
            "market and is given here at its normal dose.",
            "The blood samples taken to measure the medicine are done for research "
            "only. They are not needed for your own health care.",
            "The health checks, such as the physical exam and the blood safety "
            "tests, are standard medical tests. They are done here to keep you safe.",
        ),
        narration=(
            "This section must clearly separate experimental from standard. Do not "
            "merge it into the procedures list."
        ),
    ))

    risk_bullets = tuple(f"{r['label']}: {r['plain_language']}" for r in risks)
    sections.append(IcfSection(
        key="risks",
        heading="What are the risks?",
        cfr_refs=("50.25(a)(2)", "50.25(b)(1)"),
        purpose="State every reasonably foreseeable risk, of the drug AND of the procedures.",
        body=(
            "Taking part in this study has risks. Some are from the study medicine. "
            "Some are from the tests we do.",
            "These are the risks we know about:",
        ),
        bullets=risk_bullets,
        narration=(
            "Narrate ONLY the risks listed. Do not add risks that are not in the "
            "list and do not remove any. Do not soften a risk with words like "
            "'minimal' or 'generally well tolerated'."
        ),
    ))

    sections.append(IcfSection(
        key="reproductive_risks",
        heading="Risks if you could become pregnant or father a child",
        cfr_refs=("50.25(b)(1)",),
        purpose="State the reproductive risk and the contraception requirement.",
        body=(
            "The study medicine could harm an unborn baby. We do not know all the "
            "risks.",
            "You must not take part if you are pregnant or breastfeeding.",
            f"If you can become pregnant, you must use effective birth control from "
            f"screening until {_CONTRACEPTION_DAYS_FEMALE} days after your last dose.",
            f"If you can father a child, you must not donate sperm for "
            f"{_CONTRACEPTION_DAYS_MALE} days after your last dose.",
            "Tell the study doctor at once if you or your partner becomes pregnant "
            "during the study.",
        ),
        narration="Keep this section short and direct. Do not merge it into the risk list.",
    ))

    sections.append(IcfSection(
        key="benefits",
        heading="Will taking part help me?",
        cfr_refs=("50.25(a)(3)",),
        purpose="State honestly whether the subject benefits. For healthy volunteers: no.",
        body=(
            benefits["statement"],
            benefits["benefit_to_others"],
        ),
        narration=(
            "LOCKED. " + benefits["forbidden_framing"] + " Reproduce the meaning of "
            "the statement exactly; you may only make the wording simpler."
        ),
    ))

    sections.append(IcfSection(
        key="alternatives",
        heading="What are my other choices?",
        cfr_refs=("50.25(a)(4)",),
        purpose="Disclose alternatives. For a healthy volunteer the only one is not to take part.",
        body=(
            (
                "This study is not treatment, so there is no other treatment to "
                "offer you. Your only other choice is to not take part."
            )
            if healthy else
            (
                "You do not have to join this study to be treated. Other treatments "
                "may be right for you. The study doctor will talk with you about "
                "them before you decide."
            )
        ,),
        narration=(
            "Do not list medical treatments for a healthy volunteer. Saying there is "
            "no alternative treatment is the correct answer here, not a gap."
        ),
    ))

    restriction_bullets = [f"{r['label']}: {r['plain_language']}" for r in restrictions]
    if prohibited_list:
        restriction_bullets.append(
            "Medicines you must not take: " + "; ".join(prohibited_list) + "."
        )
    sections.append(IcfSection(
        key="restrictions",
        heading="What do I have to do, and what must I avoid?",
        cfr_refs=("50.25(a)(1)",),
        purpose="State the real restrictions the subject must follow, matching the protocol.",
        body=(
            "While you are in this study you must follow these rules. They keep you "
            "safe and keep the results correct.",
        ),
        bullets=tuple(restriction_bullets),
        narration=(
            "These restrictions come from the protocol's exclusion criteria and the "
            "drug's own label. Do not add or drop any - a consent form that does "
            "not match the protocol is a finding."
        ),
    ))

    confidentiality_body = [
        "Your name will not be on your study records. You are given a code number "
        "instead.",
        "Some people may look at your records to check that the study was done "
        "properly. These are:",
    ]
    confidentiality_bullets = [
        "The study staff at this site.",
        "The sponsor, and people working for the sponsor.",
        "The review board that approved this study.",
        "Government health authorities.",
    ]
    confidentiality_tail = [
        "The U.S. Food and Drug Administration (FDA) may inspect your records.",
        "Your name will not appear in any report or paper about this study.",
    ]
    if "European" in profile_region:
        confidentiality_tail.append(
            "Your data is handled under European data protection law. You may ask to "
            "see the data held about you. You may also ask for mistakes to be fixed."
        )
    sections.append(IcfSection(
        key="confidentiality",
        heading="Who will see my records?",
        cfr_refs=("50.25(a)(5)",),
        purpose="Describe the extent of confidentiality and note possible FDA inspection.",
        body=tuple(confidentiality_body + confidentiality_tail),
        bullets=tuple(confidentiality_bullets),
        narration=(
            "The FDA inspection sentence is a required element. Keep it as its own "
            "sentence; do not bury it in a list."
        ),
    ))

    compensation = _ctx(context, "compensation_text")
    payment_body = [
        str(compensation) if compensation else
        f"You will be paid for taking part. The amount is {_PLACEHOLDER}. Payment is "
        "for your time and travel, not for taking a risk.",
        "If you stop early, you are paid for the part of the study you finished.",
        "You do not have to pay anything to take part. The study pays for all study "
        "visits, tests and the study medicine.",
    ]
    sections.append(IcfSection(
        key="costs_and_payment",
        heading="Will I be paid, and will it cost me anything?",
        cfr_refs=("50.25(b)(3)",),
        purpose="State payment for participation and any additional cost to the subject.",
        body=tuple(payment_body),
        narration=(
            "Payment must be described as compensation for time and travel, never as "
            "a benefit of taking part and never as a reason to join."
        ),
    ))

    sections.append(IcfSection(
        key="injury",
        heading="What if I am hurt by the study?",
        cfr_refs=("50.25(a)(6)",),
        purpose="State what medical treatment and compensation are available for injury.",
        body=(
            "Tell the study doctor right away if you feel unwell or are hurt in any "
            "way during this study.",
            "You will be given the medical care you need for any injury caused by "
            "the study. The sponsor will pay for that care.",
            f"The sponsor holds insurance for this study. For more about this, "
            f"contact {_ctx(context, 'injury_contact') or _PLACEHOLDER}.",
            "By signing this form you do not give up any of your legal rights.",
        ),
        narration=(
            "Never state or imply that the subject waives any legal right. That is a "
            "prohibited exculpatory statement under 21 CFR 50.20."
        ),
    ))

    sections.append(IcfSection(
        key="contacts",
        heading="Who can I call?",
        cfr_refs=("50.25(a)(7)",),
        purpose="Give three separate contacts: research questions, subject rights, injury.",
        body=(
            "If you have questions about the study, call the study doctor: "
            f"{_ctx(context, 'study_contact') or _PLACEHOLDER}.",
            "If you have questions about your rights, call the review board: "
            f"{_ctx(context, 'rights_contact') or _PLACEHOLDER}. The review board "
            "is not part of the study team.",
            "If you are hurt or feel unwell because of the study, call: "
            f"{_ctx(context, 'injury_contact') or _PLACEHOLDER}. In an emergency, "
            "call your local emergency number first.",
        ),
        narration=(
            "The rights contact MUST be independent of the study team. Do not merge "
            "the three contacts into one."
        ),
    ))

    sections.append(IcfSection(
        key="voluntary_and_withdrawal",
        heading="Do I have to take part?",
        cfr_refs=("50.25(a)(8)", "50.25(b)(2)", "50.25(b)(4)"),
        purpose=(
            "State that participation is voluntary, that stopping carries no penalty, "
            "and the circumstances in which the investigator may withdraw the subject."
        ),
        body=(
            "No. Taking part is your choice.",
            "You can say no. You can also stop at any time, for any reason. You do "
            "not have to give a reason.",
            "If you say no, or if you stop, nothing bad will happen to you. You will "
            "not lose any benefit or care you would otherwise get.",
            "If you stop early, we will ask you to come in for a final safety check. "
            "This is for your safety. You may say no to that too.",
            "The study doctor may take you out of the study without your agreement. "
            "This can happen for these reasons:",
        ),
        bullets=(
            "It is not safe for you to carry on.",
            "You do not follow the study rules.",
            "You take a medicine that is not allowed.",
            "You become pregnant.",
            "The sponsor or the health authority stops the study.",
        ),
        narration=(
            "The voluntary statement must come first and must be unqualified. Do not "
            "add any condition to it."
        ),
    ))

    sections.append(IcfSection(
        key="new_findings",
        heading="What if something new is found out?",
        cfr_refs=("50.25(b)(5)",),
        purpose="Commit to telling the subject about significant new findings.",
        body=(
            "If we learn anything new during the study that might change your mind "
            "about taking part, we will tell you as soon as we can.",
            "You may then be asked to sign a new version of this form.",
        ),
        narration="Keep both sentences. The re-consent sentence is the operative one.",
    ))

    sections.append(IcfSection(
        key="clinicaltrials_gov",
        heading="Information about this study on the internet",
        cfr_refs=("50.25(c)",),
        purpose="Reproduce the 21 CFR 50.25(c) statement verbatim.",
        body=(ct_gov["statement"],),
        verbatim=True,
        applicable=True,
        applicability_note=ct_gov["basis"],
        narration=(
            "VERBATIM. This wording is set by 21 CFR 50.25(c). Reproduce it exactly, "
            "character for character. Do not simplify it, shorten it, or apply the "
            "plain-language rules to it."
        ),
    ))

    sections.append(IcfSection(
        key="statement_of_consent",
        heading="Your signature",
        cfr_refs=("50.25(a)(8)",),
        purpose="Record the subject's voluntary agreement before any study procedure.",
        body=(
            "I have read this form, or it has been read to me.",
            "I have had the chance to ask questions, and my questions were answered.",
            "I know that taking part is my choice, and that I can stop at any time.",
            "I agree to take part in this study.",
            "I will be given a signed and dated copy of this form.",
        ),
        narration=(
            "These lines are the subject's own attestation. Keep them in the first "
            "person and keep them short."
        ),
    ))

    return sections


def _coverage(sections: list[IcfSection]) -> tuple[dict[str, Any], list[str]]:
    """Map every element code to the section(s) discharging it, and report any
    BASIC (50.25(a)) element left uncovered. A missing basic element is the whole
    reason this engine exists, so it is computed rather than eyeballed."""
    coverage: dict[str, Any] = {}
    for element in _ELEMENTS:
        covering = [s.key for s in sections if element.code in s.cfr_refs]
        coverage[element.code] = {
            "key": element.key,
            "tier": element.tier,
            "requirement": element.requirement,
            "plain_language_hint": element.plain_language_hint,
            "sections": covering,
            "covered": bool(covering),
        }
    missing = [c for c in BASIC_ELEMENT_CODES if not coverage[c]["covered"]]
    return coverage, missing


def _signature_block(healthy: bool) -> dict[str, Any]:
    """Signature rows, with the timing rule that makes them meaningful.

    ICH E6(R2) 4.8.8 requires the subject AND the person conducting the consent
    discussion to sign and personally date the form; 4.8.9 requires an impartial
    witness when the subject (or their representative) cannot read.
    """
    return {
        "timing_rule": (
            "Written consent must be obtained, and this form signed and personally "
            "dated by the subject and by the person conducting the consent "
            "discussion, BEFORE any study-specific procedure is performed - "
            "including screening tests. A screening test performed before this "
            "signature is a protocol deviation and a GCP finding."
        ),
        "regulatory_basis": "ICH E6(R2) 4.8.8-4.8.11; 21 CFR 50.20, 50.27",
        "copy_rule": (
            "A signed and dated copy of this form must be given to the subject."
        ),
        "rows": [
            {
                "key": "subject",
                "role": "Subject",
                "required": True,
                "fields": ["Printed name", "Signature", "Date", "Time"],
                "note": (
                    "The subject dates the form personally. Staff must not enter the "
                    "date on the subject's behalf."
                ),
            },
            {
                "key": "consent_discussion",
                "role": "Person conducting the consent discussion",
                "required": True,
                "fields": ["Printed name", "Role", "Signature", "Date", "Time"],
                "note": (
                    "Signs to confirm the discussion took place and that the subject's "
                    "questions were answered before signing."
                ),
            },
            {
                "key": "investigator",
                "role": "Principal Investigator (or authorised delegate)",
                "required": True,
                "fields": ["Printed name", "Signature", "Date"],
                "note": (
                    "May be the same person as the consent discussant where the "
                    "delegation log records it."
                ),
            },
            {
                "key": "impartial_witness",
                "role": "Impartial witness",
                "required": False,
                "required_when": (
                    "Required when the subject, or their legally authorised "
                    "representative, is unable to read. The witness attends the whole "
                    "consent discussion and attests that the information was "
                    "accurately explained and that consent was freely given "
                    "(ICH E6(R2) 4.8.9)."
                ),
                "fields": ["Printed name", "Signature", "Date"],
                "note": "Leave blank and mark N/A when not applicable.",
            },
            {
                "key": "legally_authorized_representative",
                "role": "Legally authorised representative",
                "required": False,
                "required_when": (
                    "Required only where the subject cannot give consent themselves. "
                    "Not expected in a healthy-volunteer study, where the capacity to "
                    "consent is an inclusion criterion."
                    if healthy else
                    "Required where the subject is unable to give consent themselves."
                ),
                "fields": ["Printed name", "Relationship to subject", "Signature", "Date"],
                "note": "Leave blank and mark N/A when not applicable.",
            },
        ],
    }


def _pending_fields(
    version: dict[str, Any],
    context: dict[str, Any],
    washout: dict[str, Any],
    facts: dict[str, Any],
) -> list[dict[str, str]]:
    """Fields that are still placeholders. An ICF submitted to an IRB with live
    placeholders is the other routine administrative rejection, so they are
    listed rather than left for someone to spot on the page."""
    pending: list[dict[str, str]] = []

    def _need(field: str, value: Any, why: str) -> None:
        if value in (None, "", _PLACEHOLDER):
            pending.append({"field": field, "why": why})

    _need("icf_date", version.get("icf_date"), "The form must carry its own issue date.")
    _need("protocol_number", version.get("protocol_number"),
          "Ties this consent form to the protocol version it belongs to.")
    _need("protocol_version", version.get("protocol_version"),
          "Ties this consent form to the protocol version it belongs to.")
    _need("protocol_title", version.get("protocol_title"),
          "The study title must match the protocol title exactly.")
    _need("investigator_name", _ctx(context, "investigator_name"),
          "The study doctor must be named on the form.")
    _need("site_name", _ctx(context, "site_name"), "The site must be named on the form.")
    _need("study_contact", _ctx(context, "study_contact"),
          "Required element 21 CFR 50.25(a)(7) - research questions.")
    _need("rights_contact", _ctx(context, "rights_contact"),
          "Required element 21 CFR 50.25(a)(7) - subject rights; must be independent "
          "of the study team.")
    _need("injury_contact", _ctx(context, "injury_contact"),
          "Required element 21 CFR 50.25(a)(7) - research-related injury.")
    _need("compensation_text", _ctx(context, "compensation_text"),
          "The payment amount and schedule must be stated.")
    if washout.get("applicable") and washout.get("days") is None:
        pending.append({
            "field": "washout",
            "why": "No half-life was available, so the gap between stays is unstated.",
        })
    if not facts.get("n_enrolled"):
        pending.append({
            "field": "n_enrolled",
            "why": "Required element 21 CFR 50.25(b)(6) - approximate number of subjects.",
        })
    return pending


def _readability_report(sections: list[IcfSection]) -> dict[str, Any]:
    """Grade the deterministic prose, per section and overall.

    Two kinds of section are EXCLUDED. Verbatim statutory text (21 CFR 50.25(c))
    prescribes its own wording, so scoring it would only produce a finding nobody
    can lawfully act on. Labelled field lists (the header block) are not prose at
    all, and grading "Protocol number: ..." as a sentence is meaningless.

    Body paragraphs and bullets are terminated before being joined, so a bullet
    that ends in a colon is not fused with the next one into a phantom
    run-on sentence.
    """
    per_section: list[dict[str, Any]] = []
    corpus: list[str] = []
    excluded: list[str] = []
    for s in sections:
        if s.verbatim or s.field_list:
            excluded.append(s.key)
            continue
        text = " ".join(_terminate(f) for f in list(s.body) + list(s.bullets))
        if not text.strip():
            continue
        grade = readability_grade(text)
        per_section.append({
            "section": s.key,
            "grade_level": grade["grade_level"],
            "meets_target": grade["meets_target"],
            "long_sentences": grade["long_sentences"],
            "jargon": grade["jargon"],
        })
        corpus.append(text)

    overall = readability_grade(" ".join(corpus))
    return {
        "target_grade": _TARGET_READING_GRADE,
        "method": "Flesch-Kincaid grade level (local implementation, no dependencies)",
        "overall": overall,
        "per_section": per_section,
        "sections_over_target": [
            p["section"] for p in per_section if not p["meets_target"]
        ],
        "excluded_verbatim_sections": excluded,
        "note": (
            "Verbatim statutory text is excluded from the guard because it may not "
            "lawfully be simplified."
        ),
    }


def icf_directive(icf: dict[str, Any]) -> str:
    """Render the locked ICF facts as prompt text for the narration layer.

    Mirrors ``stratification.stratification_directive`` and
    ``design_decisions.design_directive``: these are settled facts to render in
    plain language, never a menu to re-decide. Returns "" for empty input rather
    than a misleading empty header.
    """
    if not icf or not icf.get("sections"):
        return ""

    facts = icf.get("study_facts", {})
    blood = facts.get("blood", {}) or {}
    dur = facts.get("duration", {}) or {}
    washout = facts.get("washout", {}) or {}
    benefits = icf.get("benefits", {}) or {}
    version = icf.get("version", {}) or {}

    lines = [
        "STANDALONE INFORMED CONSENT FORM - the content below is ALREADY "
        "DETERMINED. Render it in plain language; do not add, drop, re-order or "
        "recalculate anything, and do not invent a number that is not given here.",
        "",
        "PLAIN-LANGUAGE RULES (this is a consent form, not a protocol):",
        f"  - Target reading level: grade {_TARGET_READING_GRADE:.0f} or below.",
        "  - Short sentences. One idea per sentence. Address the subject as 'you'.",
        "  - Use the everyday word, not the technical one. For example: "
        + "; ".join(
            f"'{k}' -> '{v}'"
            for k, v in list(_JARGON_SUBSTITUTIONS.items())[:6]
        )
        + ".",
        "  - Never use bioequivalence, pharmacokinetics, AUC, Cmax, or randomisation "
        "without explaining them in the same sentence.",
        "",
        "LOCKED STUDY FACTS - these come from the same design as the protocol, so "
        "any change makes the two documents contradict each other:",
        f"  - Study drug: {facts.get('drug')}; population: {facts.get('subject_population')}",
        f"  - Design: {facts.get('design')} ({facts.get('periods')} period(s))",
        f"  - Approximate number of subjects: {facts.get('n_enrolled')}",
        f"  - Blood draws: {blood.get('pk_draws_per_period')} per period, "
        f"{blood.get('pk_draws_total')} in total",
        f"  - Total blood volume: {blood.get('total_volume_ml')} mL "
        f"(about {blood.get('total_tablespoons')} tablespoons)",
        f"  - Fasting before dosing: {dur.get('fast_before_dose_hours')} h; "
        f"confinement after dosing: {dur.get('confinement_after_dose_hours')} h "
        f"({dur.get('nights_per_period')} night(s) per stay)",
        f"  - Gap between stays: {washout.get('protocol_text')}",
        f"  - Total time in the study: about {dur.get('total_weeks')} weeks",
    ]

    lines.append("")
    lines.append(
        "BENEFIT STATEMENT - LOCKED AND NON-NEGOTIABLE: "
        + str(benefits.get("statement", ""))
    )
    lines.append("  " + str(benefits.get("forbidden_framing", "")))

    lines.append("")
    lines.append(
        "REQUIRED SECTIONS, in this exact order. Each names the regulation it "
        "satisfies - a section that stops satisfying it is a rejected submission:"
    )
    for i, s in enumerate(icf["sections"], 1):
        refs = ", ".join(s.get("cfr_refs") or []) or "structural"
        line = f"  {i}. {s.get('heading')} [{refs}] - {s.get('purpose')}"
        if s.get("verbatim"):
            line += " VERBATIM: reproduce exactly; do not simplify."
        if s.get("narration"):
            line += f" {s['narration']}"
        lines.append(line)

    risks = icf.get("risks") or []
    if risks:
        lines.append("")
        lines.append(
            "RISKS TO STATE - exactly these, no more and no fewer; do not soften them:"
        )
        for r in risks:
            lines.append(f"  - {r['label']} ({r['likelihood']}): {r['plain_language']}")

    restrictions = icf.get("restrictions") or []
    if restrictions:
        lines.append("")
        lines.append("RESTRICTIONS TO STATE - these match the protocol's criteria:")
        for r in restrictions:
            lines.append(f"  - {r['label']} [{r['window']}]: {r['plain_language']}")

    if icf.get("prohibited_medications"):
        lines.append(
            "  - Prohibited medicines from the protocol: "
            + "; ".join(icf["prohibited_medications"])
        )

    sig = icf.get("signature_block") or {}
    if sig:
        lines.append("")
        lines.append("SIGNATURE BLOCK - reproduce every row:")
        for row in sig.get("rows", []):
            flag = "required" if row.get("required") else "when applicable"
            lines.append(f"  - {row['role']} ({flag}): {', '.join(row['fields'])}")
        lines.append(f"  TIMING: {sig.get('timing_rule', '')}")

    pending = icf.get("pending_fields") or []
    if pending:
        lines.append("")
        lines.append(
            "UNFILLED FIELDS - render each as a blank line for completion. Do NOT "
            "invent a value for any of these:"
        )
        for p in pending:
            lines.append(f"  - {p['field']}: {p['why']}")

    lines.append("")
    lines.append(
        f"VERSION CONTROL: this form is ICF version {version.get('icf_version')}, "
        f"tied to protocol {version.get('protocol_number') or '[TBD]'} version "
        f"{version.get('protocol_version') or '[TBD]'}. {version.get('note', '')}"
    )
    return "\n".join(lines)
