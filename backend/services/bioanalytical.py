"""Bioanalytical method validation (BMV) - the assay package a PK/BE protocol
must specify before the first sample is drawn.

WHY THIS MODULE EXISTS
----------------------
Every number a bioequivalence study reports - Cmax, AUC, the 90% CI of the
geometric mean ratio - is a *measurement*. If the assay cannot measure the
terminal phase, AUC0-t is truncated and the AUC ratio is biased. If the
calibration range tops out below the real Cmax, the peak samples come back
"above ULOQ" and have to be diluted and reanalysed, which is a deviation unless
dilution integrity was validated in advance. Neither problem is fixable after
the samples are drawn.

So the calibration range is not a free-text field a writer fills in: it is a
consequence of the drug's own pharmacokinetics. This module derives it from the
anticipated Cmax that the literature/label extraction already produced, and
emits the rest of the validation package (accuracy/precision, selectivity,
matrix effect, carryover, the full stability battery, incurred sample
reanalysis, sample handling, chain of custody, lab qualification) as structured
FACTS that the generator narrates rather than invents. Same contract as
:mod:`services.biostatistics` (sample size) and :mod:`services.stratification`
(covariate balance).

THE TWO NUMBERS THAT MATTER, AND WHY THEY ARE WHAT THEY ARE
-----------------------------------------------------------
* **ULOQ** must sit ABOVE the highest anticipated Cmax with headroom, because
  Cmax is a distribution, not a point: the upper tail of a cohort (light
  subjects, poor metabolizers, a fast absorber) routinely runs well above the
  reported mean. ``ULOQ_MARGIN_FACTOR`` states that headroom explicitly.
* **LLOQ** must sit LOW ENOUGH that the elimination phase is still quantifiable.
  The conventional anchor is 5% of Cmax (``LLOQ_FRACTION_OF_CMAX``): from Cmax
  down to 5% of Cmax is log2(20) = 4.3 half-lives of monoexponential decline,
  which is what it takes for AUC0-t to reach >= 80% of AUC0-inf - the extrapolation
  limit regulators apply to a valid BE dataset.

Both are NAMED CONSTANTS, both are surfaced in ``assumptions`` so the protocol
can disclose them, and when no Cmax can be sourced the engine refuses to invent
a range: it returns ``derived = False`` with a high-severity flag instead.

ASSAY CLASS DRIVES THE ACCEPTANCE CRITERIA
------------------------------------------
A chromatographic assay (LC-MS/MS, small molecules) and a ligand-binding assay
(ELISA/ECL, biologics) are validated to genuinely different numbers - +/-15% RE
and <=15% CV versus +/-20% and <=20%, 20% versus 25% at the LLOQ, and a 20%
versus 30% ISR window. Getting this backwards understates or overstates what the
lab must deliver, so the class is resolved once, from the study type / curated
modality knowledge base / label text, and every criterion is rendered from it.

REGULATORY FRAMING
------------------
FDA Guidance for Industry, Bioanalytical Method Validation (May 2018); ICH M10
Bioanalytical Method Validation and Study Sample Analysis (2022); FDA
Bioequivalence Studies with Pharmacokinetic Endpoints for Drugs Submitted Under
an ANDA (analyte selection: parent vs metabolite, racemate vs enantiomer); FDA
Immunogenicity Testing of Therapeutic Protein Products (2019) for the ADA/NAb
tier, which ICH M10 explicitly places outside its own scope.
"""
from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass
from typing import Any, Optional

from services.design_decisions import classify_drug_modality

# ---------------------------------------------------------------------------
# Named constants - every assumption the engine makes, in one place, so the
# protocol can disclose them (they are echoed into result["assumptions"]).
# ---------------------------------------------------------------------------

# Headroom between the highest anticipated Cmax and the ULOQ. 2-fold is the
# common industry anchor: a Cmax distribution with a 30-40% CV puts the upper
# tail roughly 1.7-1.9x above the central estimate, so 2x keeps essentially the
# whole cohort on-curve without forcing an unnecessarily wide dynamic range
# (a wider range costs sensitivity at the bottom, which is where AUC is won).
ULOQ_MARGIN_FACTOR = 2.0

# The LLOQ must be <= this fraction of Cmax. 5% = 1/20th of the peak, i.e.
# log2(20) = 4.32 half-lives of quantifiable monoexponential decline, which is
# what AUC0-t >= 80% of AUC0-inf requires. Going lower is welcome; going higher
# truncates AUC and biases the exposure ratio.
LLOQ_FRACTION_OF_CMAX = 0.05

# FDA BMV 2018 / ICH M10: a calibration curve is a blank, a zero, and at least
# six non-zero concentration levels spanning the range.
MIN_CALIBRATION_STANDARDS = 6

# QC placement (ICH M10): low QC within 3x the LLOQ; medium QC at 30-50% of the
# calibration range; high QC at >= 75% of the ULOQ. 40% / 80% are chosen inside
# those windows so neither QC sits on a boundary.
LOW_QC_MULTIPLE_OF_LLOQ = 3.0
MID_QC_FRACTION_OF_ULOQ = 0.40
HIGH_QC_FRACTION_OF_ULOQ = 0.80

# Dilution factor to validate. At 10-fold, a sample may read up to 10 x ULOQ
# (= 20 x the anticipated Cmax) and still be reported without a protocol
# deviation. Validated in advance because a dilution performed on an
# unvalidated factor invalidates the result.
DILUTION_INTEGRITY_FACTOR = 10

# Stability battery. Each figure is a MINIMUM claim; the validated duration must
# equal or exceed what study samples actually experience.
BENCHTOP_STABILITY_HOURS = 4.0      # covers collection -> freezer (target < 1 h) with margin
FREEZE_THAW_CYCLES = 3              # ICH M10 minimum; must be >= cycles samples will see
LONG_TERM_STABILITY_MONTHS = 6      # covers first collection -> last analysis for a single-dose study
PROCESSED_SAMPLE_STABILITY_HOURS = 48.0   # must exceed the longest anticipated analytical run
# Nominal long-term storage temperature. -70 C (accepted range -60 to -80 C) is
# the more conservative of the two conventional options (-20 C vs -70 C) and is
# where long-term stability is most often established, so it is the default
# unless the sponsor's validated claim says otherwise.
DEFAULT_STORAGE_TEMP_C = -70.0

# Sample processing. 1500 x g for 10 min at 4 C is the conventional plasma
# separation; the 30-minute limit keeps ex vivo degradation and cell lysis out
# of the measured concentration.
CENTRIFUGE_G = 1500
CENTRIFUGE_MINUTES = 10
CENTRIFUGE_TEMP_C = 4
MAX_MINUTES_TO_CENTRIFUGE = 30
MAX_MINUTES_TO_FREEZER = 60
ALIQUOTS_PER_SAMPLE = 2             # primary + backup, stored in separate freezers
SERUM_CLOT_MINUTES = 30             # serum must clot at room temperature, NOT on ice

# Incurred sample reanalysis. FDA BMV 2018 / ICH M10 (from the Crystal City III
# ISR workshop): 10% of the first 1000 study samples plus 5% of any beyond 1000.
# NOTE: the sizing rule is tiered at 1000 samples, not 100 - for a conventional
# BE study (a few hundred samples) the whole set therefore falls in the 10% tier.
ISR_FIRST_TIER_SAMPLES = 1000
ISR_FIRST_TIER_PCT = 10.0
ISR_SECOND_TIER_PCT = 5.0
ISR_MIN_PASS_PCT = 67.0             # at least 2/3 of repeats must agree

# Cross-validation between laboratories / methods (ICH M10): a minimum of 20
# study samples (or spiked QCs where study samples are unavailable), judged on
# the same agreement criterion as ISR.
CROSS_VALIDATION_MIN_SAMPLES = 20

# ADA screening assay sensitivity floor recommended by FDA (2019).
ADA_ASSAY_SENSITIVITY_NG_PER_ML = 100.0


# ---------------------------------------------------------------------------
# Assay class - the criteria set everything else is rendered from
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AssayCriteria:
    """Acceptance numbers that differ between assay technologies."""
    key: str
    label: str
    re_pct: float          # accuracy (relative error) at low/mid/high QC
    cv_pct: float          # precision at low/mid/high QC
    re_lloq_pct: float     # relaxed accuracy at the LLOQ
    cv_lloq_pct: float     # relaxed precision at the LLOQ
    isr_window_pct: float  # ISR agreement window
    selectivity_lots: int  # individual blank matrix lots to screen
    total_error_pct: Optional[float] = None       # LBA only
    total_error_lloq_pct: Optional[float] = None  # LBA only

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Chromatographic (LC-MS/MS) - FDA BMV 2018 Table 1 / ICH M10 section 3.
CHROMATOGRAPHIC = AssayCriteria(
    key="chromatographic",
    label="Validated LC-MS/MS chromatographic assay",
    re_pct=15.0, cv_pct=15.0, re_lloq_pct=20.0, cv_lloq_pct=20.0,
    isr_window_pct=20.0, selectivity_lots=6,
)

# Ligand-binding assay (ELISA / ECL) - ICH M10 section 4. Wider criteria plus a
# total-error requirement, because an LBA measures a binding response rather
# than a mass-resolved signal.
LIGAND_BINDING = AssayCriteria(
    key="ligand_binding",
    label="Validated ligand-binding assay (ELISA / ECL)",
    re_pct=20.0, cv_pct=20.0, re_lloq_pct=25.0, cv_lloq_pct=25.0,
    isr_window_pct=30.0, selectivity_lots=10,
    total_error_pct=30.0, total_error_lloq_pct=40.0,
)


# ---------------------------------------------------------------------------
# Validation requirement catalogue
# ---------------------------------------------------------------------------
# Text carries {placeholders} that _render() fills from the resolved assay
# criteria, so one catalogue serves both assay technologies without drifting.


@dataclass(frozen=True)
class BMVRequirement:
    key: str
    label: str
    category: str          # "performance" | "conduct"
    requirement: str       # what the laboratory must do
    acceptance: str        # the pass/fail criterion
    rationale: str         # WHY the regulator asks for it
    reference: str         # the guidance clause it comes from
    assay_classes: str = "both"   # "both" | "chromatographic" | "ligand_binding"
    # Populated per-study by plan_bioanalytical():
    applicable: bool = True
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_PARAMETERS: tuple[BMVRequirement, ...] = (
    BMVRequirement(
        key="selectivity",
        label="Selectivity",
        category="performance",
        requirement=(
            "Screen blank matrix from at least {lots} individual donors, including one "
            "haemolysed and one hyperlipidaemic lot, unspiked and spiked at the LLOQ."
        ),
        acceptance=(
            "Interference at the analyte and internal-standard positions must not exceed "
            "20% of the LLOQ analyte response and 5% of the internal-standard response; the "
            "LLOQ must quantify within +/-{re_lloq}% in at least 80% of the individual lots."
        ),
        rationale=(
            "Endogenous matrix components differ between people. A method validated in one "
            "pooled lot can fail silently in an individual subject whose plasma carries an "
            "interfering component at the analyte's retention time or epitope, producing a "
            "concentration that is wrong for that subject only - exactly the kind of error a "
            "crossover cannot detect because it looks like within-subject variability."
        ),
        reference="FDA BMV 2018 (Selectivity); ICH M10 sections 3.1 / 4.1",
    ),
    BMVRequirement(
        key="specificity",
        label="Specificity",
        category="performance",
        requirement=(
            "Demonstrate that the method distinguishes the analyte from structurally related "
            "compounds: known metabolites (including any glucuronide or lactone form capable of "
            "back-conversion), degradation products, the internal standard, permitted "
            "concomitant medications (paracetamol, ibuprofen, caffeine, nicotine) and, for a "
            "ligand-binding assay, the soluble target and anti-drug antibodies."
        ),
        acceptance=(
            "No component may bias the measured concentration beyond +/-{re}% at the QC levels "
            "tested."
        ),
        rationale=(
            "Back-conversion is the classic failure: an acyl glucuronide or a lactone metabolite "
            "reverts to the parent during extraction and inflates the parent concentration. The "
            "inflation is not constant across subjects, so it does not cancel from the "
            "test/reference ratio."
        ),
        reference="FDA BMV 2018 (Specificity); ICH M10 sections 3.1 / 4.1",
    ),
    BMVRequirement(
        key="matrix_effect",
        label="Matrix effect",
        category="performance",
        requirement=(
            "Determine the matrix factor in at least {lots} individual lots at the low and high "
            "QC concentrations, and normalise it to the internal standard."
        ),
        acceptance="Coefficient of variation of the IS-normalised matrix factor <= 15% across lots.",
        rationale=(
            "Ion suppression or enhancement from co-eluting phospholipids changes the response "
            "per unit of analyte. A stable-isotope-labelled internal standard co-elutes and "
            "cancels most of it, which is why the normalised factor - not the raw one - is the "
            "criterion."
        ),
        reference="FDA BMV 2018 (Matrix Effect); ICH M10 section 3.1",
        assay_classes="chromatographic",
    ),
    BMVRequirement(
        key="recovery",
        label="Extraction recovery",
        category="performance",
        requirement=(
            "Compare the response of extracted QC samples with that of post-extraction spiked "
            "matrix at the low, medium and high QC levels."
        ),
        acceptance=(
            "Recovery need not be 100% or concentration-independent, but it must be consistent, "
            "precise and reproducible across the three levels."
        ),
        rationale=(
            "Recovery is a robustness measure, not an accuracy measure - the calibration curve "
            "corrects for a constant loss. What matters is that the loss does not drift between "
            "runs or vary with concentration, because that would tilt the curve."
        ),
        reference="FDA BMV 2018 (Recovery); ICH M10 section 3.1",
        assay_classes="chromatographic",
    ),
    BMVRequirement(
        key="calibration_curve",
        label="Calibration curve",
        category="performance",
        requirement=(
            "Prepare a blank, a zero (blank plus internal standard) and at least {stds} non-zero "
            "calibration standards in the study matrix, spanning the LLOQ to the ULOQ, and fit "
            "the simplest regression model that describes the response."
        ),
        acceptance=(
            "Back-calculated standards within +/-{re}% of nominal (+/-{re_lloq}% at the LLOQ); at "
            "least 75% of standards, and a minimum of {stds}, must meet this in every run."
        ),
        rationale=(
            "The curve is the only link between instrument response and reported concentration. "
            "Dropping standards until a curve passes is the most common way an unacceptable run "
            "is made to look acceptable, which is why the 75% floor and the absolute minimum are "
            "both specified."
        ),
        reference="FDA BMV 2018 (Calibration Curve); ICH M10 sections 3.2 / 4.2",
    ),
    BMVRequirement(
        key="accuracy_precision_within_run",
        label="Within-run accuracy and precision",
        category="performance",
        requirement=(
            "Analyse a minimum of five replicates at each of four QC levels (LLOQ, low, medium, "
            "high) within a single analytical run."
        ),
        acceptance=(
            "Mean accuracy within +/-{re}% of nominal and CV <= {cv}% at low, medium and high; "
            "+/-{re_lloq}% and <= {cv_lloq}% at the LLOQ."
        ),
        rationale=(
            "Within-run performance sets the noise floor of a single measurement. The LLOQ "
            "criterion is deliberately relaxed because signal-to-noise is worst there, but it is "
            "still the level that decides whether a terminal-phase sample is reportable."
        ),
        reference="FDA BMV 2018 (Accuracy/Precision); ICH M10 sections 3.2 / 4.2",
    ),
    BMVRequirement(
        key="accuracy_precision_between_run",
        label="Between-run accuracy and precision",
        category="performance",
        requirement=(
            "Repeat the four-level QC assessment in at least three analytical runs conducted on "
            "at least two separate days, by more than one analyst where practicable."
        ),
        acceptance=(
            "Mean accuracy within +/-{re}% and CV <= {cv}% (+/-{re_lloq}% and <= {cv_lloq}% at the "
            "LLOQ) across runs."
        ),
        rationale=(
            "Study samples are analysed over weeks, across analysts, instruments and reagent "
            "lots. Between-run performance is what actually applies to the dataset; within-run "
            "performance alone would flatter the method."
        ),
        reference="FDA BMV 2018 (Accuracy/Precision); ICH M10 sections 3.2 / 4.2",
    ),
    BMVRequirement(
        key="carryover",
        label="Carryover",
        category="performance",
        requirement=(
            "Inject or analyse a blank matrix sample immediately after the highest calibration "
            "standard (ULOQ) in the validation runs and monitor it throughout sample analysis."
        ),
        acceptance=(
            "Carryover response in the blank <= 20% of the LLOQ analyte response and <= 5% of the "
            "internal-standard response."
        ),
        rationale=(
            "In a PK profile the peak sample is often injected immediately before a terminal-phase "
            "sample from the same subject. Uncontrolled carryover therefore inflates precisely the "
            "low concentrations that determine the terminal slope and the AUC extrapolation."
        ),
        reference="FDA BMV 2018 (Carryover); ICH M10 section 3.1",
    ),
    BMVRequirement(
        key="dilution_integrity",
        label="Dilution integrity",
        category="performance",
        requirement=(
            "Spike matrix above the ULOQ and dilute it with blank matrix by a factor of at least "
            "{dilution}, in a minimum of five replicates, using the dilution scheme the laboratory "
            "will actually apply to study samples."
        ),
        acceptance="Accuracy within +/-{re}% of nominal and CV <= {cv}% after dilution.",
        rationale=(
            "A sample reading above the ULOQ cannot be reported. Diluting it is only permissible "
            "if that dilution factor was validated beforehand - otherwise the result is an "
            "unvalidated measurement and the timepoint is lost, which for a Cmax sample means a "
            "lost primary endpoint."
        ),
        reference="FDA BMV 2018 (Dilution Integrity); ICH M10 section 3.2",
        assay_classes="chromatographic",
    ),
    BMVRequirement(
        key="dilutional_linearity_parallelism",
        label="Dilutional linearity, parallelism and minimum required dilution",
        category="performance",
        requirement=(
            "Establish the minimum required dilution (MRD); demonstrate dilutional linearity from "
            "a spiked sample above the ULOQ across at least {dilution}-fold; and demonstrate "
            "parallelism using incurred (real) high-concentration study samples serially diluted."
        ),
        acceptance=(
            "Back-calculated concentrations within +/-{re}% of nominal after correction for "
            "dilution; CV of the parallelism dilution series <= 30%, with no concentration-"
            "dependent trend and no hook effect."
        ),
        rationale=(
            "A ligand-binding assay measures binding, not mass. Spiked standards can behave "
            "differently from the endogenous analyte in real samples, so parallelism against "
            "incurred samples - not dilutional linearity alone - is what shows the assay reads "
            "the study's actual analyte correctly. A prozone (hook) effect can also make a very "
            "high sample read low, in the direction that hides a true Cmax difference."
        ),
        reference="ICH M10 section 4.2 (dilutional linearity, parallelism); FDA BMV 2018 (LBA)",
        assay_classes="ligand_binding",
    ),
    BMVRequirement(
        key="reference_standards",
        label="Reference standards and internal standard",
        category="conduct",
        requirement=(
            "Use a characterised reference standard for the analyte (and for each additional "
            "analyte) with a certificate of analysis stating lot, purity, storage condition and "
            "retest/expiry date; a stable-isotope-labelled internal standard is preferred for a "
            "chromatographic assay. Standards and QCs must be prepared from separate weighings."
        ),
        acceptance=(
            "Certificates on file before validation; standards and QCs from independent stock; "
            "no expired material used."
        ),
        rationale=(
            "Every reported concentration is traceable to the reference standard's assigned "
            "purity. Preparing standards and QCs from a single weighing hides a weighing error "
            "completely, because the QC would be wrong by the same factor as the curve."
        ),
        reference="FDA BMV 2018 (Reference Standards); ICH M10 section 2",
    ),
    BMVRequirement(
        key="run_acceptance",
        label="Analytical run acceptance",
        category="conduct",
        requirement=(
            "Include QC samples at the low, medium and high levels in duplicate (or at least 5% "
            "of the number of study samples, whichever is greater) in every analytical run of "
            "study samples."
        ),
        acceptance=(
            "At least 67% of all QC samples, and at least 50% at each concentration level, within "
            "+/-{re}% of nominal; runs failing this are rejected and reanalysed."
        ),
        rationale=(
            "This is the run-level control that catches a drifting instrument or a degraded "
            "reagent lot mid-study. Requiring 50% at EACH level stops a run passing on the back "
            "of the two easy levels while the third, most relevant level fails."
        ),
        reference="FDA BMV 2018 (Study Sample Analysis); ICH M10 sections 5 / 7",
    ),
    BMVRequirement(
        key="reanalysis_policy",
        label="Sample reanalysis and reported-value selection",
        category="conduct",
        requirement=(
            "Pre-specify in the analytical plan, BEFORE any study sample is analysed, the only "
            "permitted reasons for reanalysis (rejected run, sample processing error, "
            "concentration above the ULOQ, poor chromatography, internal-standard response "
            "failure) and the rule that decides which value is reported."
        ),
        acceptance=(
            "No reanalysis for a reason not pre-specified; every repeat, its reason and the "
            "reported-value decision documented and auditable."
        ),
        rationale=(
            "Repeating samples until a value looks right ('testing into compliance') is the "
            "single most damaging bioanalytical finding at inspection, and it biases the exposure "
            "ratio directly. Pre-specification is the only defence."
        ),
        reference="FDA BMV 2018 (Reanalysis); ICH M10 section 7.2",
    ),
)


# ---------------------------------------------------------------------------
# Stability battery
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StabilityTest:
    key: str
    label: str
    condition: str
    acceptance: str
    rationale: str
    reference: str
    applies_when: str = "always"
    # Populated per-study:
    applicable: bool = True
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_STABILITY_ACCEPTANCE = (
    "Mean measured concentration of at least three replicates at the low and high QC levels "
    "within +/-{re}% of the nominal concentration, against freshly prepared calibration standards."
)

_STABILITY: tuple[StabilityTest, ...] = (
    StabilityTest(
        key="stock_solution",
        label="Stock and working solution stability",
        condition=(
            "Analyte and internal-standard stock solutions at their stated storage condition "
            "(refrigerated or frozen) for the full period of use, plus at least 6 hours at room "
            "temperature to cover bench handling."
        ),
        acceptance=(
            "Response of the stored solution within +/-10% of a freshly prepared solution."
        ),
        rationale=(
            "A degraded stock biases the calibration curve and the QCs in the SAME direction, so "
            "the run passes its own acceptance criteria while every reported concentration is "
            "wrong. Nothing downstream can detect it."
        ),
        reference="FDA BMV 2018 (Stock Solution Stability); ICH M10 section 3.1",
    ),
    StabilityTest(
        key="bench_top",
        label="Bench-top (short-term) matrix stability",
        condition=(
            "Spiked matrix held at room temperature (and on wet ice, as applicable) for at least "
            "{bench_h} hours, which must equal or exceed the maximum interval study samples spend "
            "unfrozen during collection and processing."
        ),
        acceptance=_STABILITY_ACCEPTANCE,
        rationale=(
            "Between the needle and the freezer the analyte sits in warm, enzymatically active "
            "matrix. The validated bench-top window is what makes the collection SOP defensible; "
            "a sample handled outside it is a deviation."
        ),
        reference="FDA BMV 2018 (Bench-top Stability); ICH M10 section 3.1",
    ),
    StabilityTest(
        key="freeze_thaw",
        label="Freeze-thaw stability",
        condition=(
            "A minimum of {ft_cycles} freeze-thaw cycles from {storage} C to room temperature "
            "(or wet ice), which must equal or exceed the number of cycles any study sample will "
            "experience, including reanalysis and ISR."
        ),
        acceptance=_STABILITY_ACCEPTANCE,
        rationale=(
            "Every ISR repeat and every reanalysis thaws the aliquot again. If freeze-thaw was "
            "validated for fewer cycles than the samples actually see, the repeat result is "
            "unvalidated - and the ISR comparison it feeds becomes uninterpretable."
        ),
        reference="FDA BMV 2018 (Freeze-Thaw Stability); ICH M10 section 3.1",
    ),
    StabilityTest(
        key="long_term",
        label="Long-term frozen matrix stability",
        condition=(
            "Storage at {storage} C for at least {lt_months} months, and in any case for a period "
            "equal to or longer than the interval from the FIRST sample collection to the LAST "
            "sample analysis."
        ),
        acceptance=_STABILITY_ACCEPTANCE,
        rationale=(
            "The stability claim must bracket the real storage duration. If analysis is delayed, "
            "additional stability timepoints must be added prospectively - a claim cannot be "
            "extended retrospectively over samples that have already been stored."
        ),
        reference="FDA BMV 2018 (Long-term Stability); ICH M10 section 3.1",
    ),
    StabilityTest(
        key="processed_sample",
        label="Processed-sample / autosampler stability",
        condition=(
            "Extracted samples held in the autosampler at its operating temperature for at least "
            "{auto_h} hours, exceeding the longest anticipated analytical run including any "
            "re-injection."
        ),
        acceptance=_STABILITY_ACCEPTANCE,
        rationale=(
            "A long batch means the last injection of a run happens many hours after extraction. "
            "Degradation in the autosampler creates a systematic drift down the injection "
            "sequence, which maps onto whichever subjects happen to sit at the end of the batch."
        ),
        reference="FDA BMV 2018 (Processed Sample Stability); ICH M10 section 3.1",
    ),
    StabilityTest(
        key="whole_blood",
        label="Whole-blood stability",
        condition=(
            "Freshly drawn whole blood spiked with the analyte, held on wet ice and at room "
            "temperature for the maximum interval permitted between collection and "
            "centrifugation, then processed and assayed."
        ),
        acceptance=_STABILITY_ACCEPTANCE,
        rationale=(
            "Plasma-based stability says nothing about the period when the analyte is still in "
            "contact with intact cells and blood esterases. Esters, prodrugs, lactone/hydroxy-acid "
            "pairs and acyl glucuronides can convert substantially within that window."
        ),
        reference="FDA BMV 2018 (Whole Blood Stability); ICH M10 section 3.1",
        applies_when=(
            "The analyte is measured in whole blood, is a prodrug/ester, or the label indicates "
            "ex vivo instability or erythrocyte partitioning"
        ),
    ),
)


# ---------------------------------------------------------------------------
# Label cues - analyte selection and handling grounded in the real label text
# ---------------------------------------------------------------------------
# Matched case-insensitively against the concatenated DailyMed label sections
# already fetched for the protocol, exactly as services/stratification.py does.
# Word boundaries matter here: "\bactive metabolite" must NOT fire on
# "inactive metabolite", which is a plain substring of it.

_CUES: dict[str, tuple[str, ...]] = {
    "prodrug": (
        r"\bpro-?drug\b",
        r"\bactive moiety\b",
        # "converted/hydrolysed to the active X" - but NOT "converted to active
        # METABOLITES", which is ordinary metabolism, not a prodrug. Atorvastatin
        # ("converted to active ortho- and parahydroxylated metabolites") is the
        # case that makes this matter: reading it as a prodrug would move the
        # bioequivalence conclusion off the parent drug, which is where the
        # product-specific guidance actually puts it. A genuine prodrug label
        # says "prodrug" or "active moiety" and is caught by the cues above.
        r"(?:rapidly |extensively )?(?:hydroly[sz]ed|converted|de-?esterified)"
        r"[^.]{0,40}\bto\b[^.]{0,40}\bactive\b(?![^.]{0,40}metabolit)",
        r"converted in vivo to",
    ),
    "active_metabolite": (
        r"\bactive metabolite",
        r"\bpharmacologically active\b[^.]{0,40}\bmetabolite",
        r"\bmajor circulating metabolite",
        r"\bequipotent\b",
        r"\bmetabolite[s]?\b[^.]{0,60}\bcontribut\w+\b[^.]{0,40}"
        r"(?:activity|efficacy|effect|exposure)",
    ),
    "chiral": (
        r"\bracemi[cz]\w*",
        r"\benantiomer",
        r"\bstereoisomer",
        r"\bchiral\b",
        r"\(\s*[rs]\s*\)-",
        r"\b[rs]-\(\s*[+-]\s*\)",
    ),
    "whole_blood": (
        r"\bwhole blood\b",
        r"\bblood[- :/]to[- :/]plasma\b",
        r"\bblood\s*[:/]\s*plasma\b",
        r"partition\w*[^.]{0,40}(?:red blood cells|erythrocytes)",
        r"(?:concentrat|distribut)\w*[^.]{0,30}(?:in|into)\s+(?:red blood cells|erythrocytes)",
    ),
    "ex_vivo_instability": (
        # "unstable" alone is a trap: cardiology labels are full of "unstable
        # angina". Require it NOT to be followed by angina.
        r"\bunstable\b(?!\s+angina)",
        r"\bester(?:s|ase|ases)?\b",
        r"\bhydroly[sz]",
        r"\bacyl glucuronide\b",
        r"\blactone\b",
        r"\bback[- ]conversion\b",
        r"\binterconver",
        r"\bphoto(?:degrad|labile|sensiti)\w*",
        r"\blight[- ]sensitive\b",
    ),
    "biologic_assay": (
        r"\bmonoclonal antibod\w*",
        r"\brecombinant\b",
        r"\bfusion protein\b",
        r"\bimmunogenicity\b",
        r"\banti-?drug antibod\w*",
        r"\bpegylated\b",
    ),
}

# Negation guard. A cue only counts when it is not being denied in its own
# sentence ("there are no active metabolites", "not distributed into
# erythrocytes"). Missing this turns a label's REASSURANCE into a requirement.
_NEGATION_CUES = (
    "no ", "not ", "non-", "without ", "lack", "devoid", "inactive",
    "negligible", "does not", "did not", "unlikely",
)
_NEGATION_WINDOW = 70

# Drugs whose regulatory matrix is WHOLE BLOOD, not plasma: they partition
# heavily into erythrocytes and their FDA product-specific guidances and
# therapeutic-monitoring practice are both whole-blood based. Assaying plasma
# for these would produce numbers that cannot be compared with any reference.
_WHOLE_BLOOD_ANALYTES = frozenset({
    "tacrolimus", "cyclosporine", "ciclosporin", "sirolimus", "everolimus",
})

# FDA's four-part test for when a chiral (enantiomer-specific) assay is
# required; the default remains an achiral assay of the racemate.
_CHIRAL_CONDITIONS = (
    "The enantiomers exhibit different pharmacodynamic characteristics.",
    "The enantiomers exhibit different pharmacokinetic characteristics.",
    "Primary efficacy and safety activity resides with the minor enantiomer.",
    "At least one enantiomer exhibits nonlinear absorption.",
)


def _negated(text: str, idx: int) -> bool:
    """True if the match at ``idx`` is denied earlier in its own sentence."""
    window = text[max(0, idx - _NEGATION_WINDOW):idx]
    sentence = re.split(r"[.;]", window)[-1].lower()
    return any(cue in sentence for cue in _NEGATION_CUES)


def _first_affirmative(text: str, pattern: str) -> Optional[re.Match]:
    """First match of ``pattern`` that is not negated in its own sentence."""
    for m in re.finditer(pattern, text, re.IGNORECASE):
        if not _negated(text, m.start()):
            return m
    return None


def _detect(text: str) -> set[str]:
    """Which label cues the text gives positive, non-negated evidence for."""
    if not text:
        return set()
    return {
        key for key, patterns in _CUES.items()
        if any(_first_affirmative(text, p) for p in patterns)
    }


def _quote(text: str, key: str, width: int = 200) -> str:
    """The label fragment that triggered a cue, so a reviewer can check it."""
    for pattern in _CUES.get(key, ()):
        m = _first_affirmative(text, pattern)
        if not m:
            continue
        start, end = max(0, m.start() - 60), min(len(text), m.end() + 120)
        # ASCII-folded: this is raw label text on its way into the prompt and
        # the exported document.
        frag = _ascii(re.sub(r"\s+", " ", text[start:end]).strip())[:width]
        return ("..." if start > 0 else "") + frag + ("..." if end < len(text) else "")
    return ""


# ---------------------------------------------------------------------------
# Concentration units - normalised to ng/mL so the range arithmetic is safe
# ---------------------------------------------------------------------------
# A silent unit error here is a 1000-fold error in the calibration range, so
# unknown units are REFUSED (returned as None and flagged), never assumed.

_UNIT_FACTORS_TO_NG_PER_ML: dict[str, float] = {
    "ng/ml": 1.0, "ug/l": 1.0, "ng/cc": 1.0,
    "ug/ml": 1000.0, "mg/l": 1000.0,
    "mg/ml": 1.0e6, "g/l": 1.0e6,
    "mg/dl": 1.0e4, "ug/dl": 10.0,
    "pg/ml": 1.0e-3, "ng/l": 1.0e-3,
    "ng/dl": 0.01, "pg/l": 1.0e-6,
}


def _normalise_unit(unit: Optional[str]) -> Optional[str]:
    """Canonical lower-case unit token, or None when it is not a mass/volume
    concentration this engine can convert (molar and activity units need a
    molecular weight / potency assignment the engine does not have)."""
    if not unit:
        return None
    u = unit.strip().lower()
    # Micro sign (U+00B5) and Greek small mu (U+03BC), written as escapes to
    # keep this file pure ASCII. They are mapped to "u" rather than stripped:
    # dropping the character would turn "ug/mL" into "g/mL", a 1,000,000-fold
    # error in the calibration range.
    u = u.replace(chr(0xB5), "u").replace(chr(0x3BC), "u")
    u = u.replace("mcg", "ug").replace(" ", "").replace("*", "")
    if any(ord(ch) > 127 for ch in u):
        return None
    return u if u in _UNIT_FACTORS_TO_NG_PER_ML else None


def _to_ng_per_ml(value: float, unit: Optional[str]) -> Optional[float]:
    key = _normalise_unit(unit)
    if key is None:
        return None
    return value * _UNIT_FACTORS_TO_NG_PER_ML[key]


# ---------------------------------------------------------------------------
# ASCII folding at the boundary
# ---------------------------------------------------------------------------
# Everything this engine ECHOES back from an external source - the unit on a PK
# record, the population it was measured in, the drug name, a quoted fragment of
# the DailyMed label - can carry non-ASCII characters. Real labels are full of
# them (registered-trademark signs, micro signs, en dashes, plus-minus). They
# flow from here into the AI prompt, the DOCX/PDF export and any log line, and
# this repo's Windows console is cp1252: a stray character raises an encode
# error instead of printing. Typography is therefore mapped to its ASCII
# equivalent at the boundary, and anything left over becomes "?" so a reviewer
# can SEE that a character was lost rather than silently reading a corrupted
# unit or a truncated quotation.
_ASCII_MAP = {
    0x00A0: " ", 0x2007: " ", 0x202F: " ",          # non-breaking spaces
    0x2010: "-", 0x2011: "-", 0x2012: "-",          # hyphens / figure dash
    0x2013: "-", 0x2014: "-", 0x2015: "-",          # en / em dash
    0x2018: "'", 0x2019: "'", 0x201A: "'",          # curly single quotes
    0x201C: '"', 0x201D: '"', 0x201E: '"',          # curly double quotes
    0x2026: "...", 0x00AD: "",                      # ellipsis, soft hyphen
    0x00B1: "+/-", 0x2264: "<=", 0x2265: ">=",      # plus-minus, inequalities
    0x00D7: "x", 0x00F7: "/",                       # multiplication, division
    0x00B0: " deg", 0x00BA: " deg",                 # degree signs
    0x00AE: "(R)", 0x2122: "(TM)", 0x00A9: "(C)",   # trademark / copyright
    0x00B5: "u", 0x03BC: "u",                       # micro sign, Greek mu
    0x03B1: "alpha", 0x03B2: "beta", 0x03B3: "gamma",
    0x221E: "infinity", 0x2192: "->",
}


def _ascii(text: Any) -> str:
    """Fold an externally sourced string to ASCII for safe echoing."""
    if text is None or text == "":
        return ""
    out = []
    for ch in str(text):
        code = ord(ch)
        if code < 128:
            out.append(ch)
        else:
            out.append(_ASCII_MAP.get(code, "?"))
    return "".join(out)


def _ascii_unit(unit: Optional[str]) -> str:
    """The unit as it will be ECHOED back into the plan, forced to ASCII.

    Literature-extracted units routinely arrive carrying a real micro sign
    (U+00B5) or Greek mu (U+03BC). Those map to "u" and are NEVER dropped:
    losing the prefix would turn ug/mL into g/mL, a 1,000,000-fold error in the
    calibration range.
    """
    return _ascii(unit).strip()


# ---------------------------------------------------------------------------
# Rounding - always in the conservative direction
# ---------------------------------------------------------------------------


def _sig_round(x: float, up: bool) -> float:
    """Round to one significant figure, up for the ULOQ and down for the LLOQ,
    so the calibration anchors are round numbers a laboratory can actually
    prepare and the rounding never narrows the required range."""
    if x <= 0:
        return 0.0
    exp = math.floor(math.log10(x) + 1e-12)
    step = 10.0 ** exp
    scaled = x / step
    n = math.ceil(scaled - 1e-9) if up else math.floor(scaled + 1e-9)
    return round(n * step, max(0, -exp) + 2)


def _round_conc(x: float) -> float:
    """Present a QC concentration at three significant figures."""
    if x <= 0:
        return 0.0
    exp = math.floor(math.log10(x) + 1e-12)
    return round(x, max(0, 2 - exp))


# ---------------------------------------------------------------------------
# Anticipated Cmax
# ---------------------------------------------------------------------------


def _anticipated_cmax(
    pk_parameters: Optional[list[dict[str, Any]]],
) -> tuple[Optional[dict[str, Any]], list[dict[str, Any]]]:
    """Highest reported Cmax, converted to ng/mL, plus the records rejected.

    The HIGHEST literature Cmax is used, not the mean of the reports: the
    calibration range has to cover the most exposed subject in the study, and a
    sample above the ULOQ costs a timepoint. Choosing conservatively here only
    costs dynamic range; choosing centrally costs data.
    """
    usable: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for rec in pk_parameters or []:
        if not isinstance(rec, dict):
            continue
        if str(rec.get("parameter", "")).strip().lower() != "cmax":
            continue
        # Units are echoed back into the plan, so they are ASCII-folded here at
        # the boundary - see _ascii_unit().
        unit = _ascii_unit(rec.get("unit"))
        try:
            value = float(rec.get("value"))
        except (TypeError, ValueError):
            rejected.append({"unit": unit, "reason": "non-numeric value"})
            continue
        if value <= 0:
            rejected.append({"unit": unit, "reason": "non-positive value"})
            continue
        converted = _to_ng_per_ml(value, rec.get("unit"))
        if converted is None:
            rejected.append({
                "unit": unit,
                "value": value,
                "reason": "unit not convertible to a mass/volume concentration",
            })
            continue
        usable.append({
            "value_ng_per_ml": converted,
            "reported_value": value,
            "reported_unit": unit,
            "population": _ascii(rec.get("population")) or None,
            "source_pmid": _ascii(rec.get("source_pmid")) or None,
        })
    if not usable:
        return None, rejected
    chosen = max(usable, key=lambda r: r["value_ng_per_ml"])
    chosen["n_records_considered"] = len(usable)
    chosen["basis"] = (
        "Highest Cmax reported across the extracted literature/label PK records, "
        "converted to ng/mL"
    )
    return chosen, rejected


# ---------------------------------------------------------------------------
# Assay class resolution
# ---------------------------------------------------------------------------


def _resolve_assay_class(
    is_biologic: Optional[bool],
    study_type: str,
    drug_inn: str,
    detected: set[str],
) -> tuple[AssayCriteria, str]:
    """Pick the assay technology and say what the choice rests on."""
    if is_biologic is True:
        return LIGAND_BINDING, "Caller-supplied modality (biologic)"
    if is_biologic is False:
        return CHROMATOGRAPHIC, "Caller-supplied modality (small molecule)"
    if (study_type or "").strip().lower() == "biosimilar_pk":
        return LIGAND_BINDING, "Study type is a biosimilar PK-similarity study"
    modality = classify_drug_modality(drug_inn or "")
    if modality == "biologic":
        return LIGAND_BINDING, "Drug modality classified as a biologic (services.design_decisions)"
    if modality == "small_molecule":
        return CHROMATOGRAPHIC, "Drug modality classified as a small molecule (services.design_decisions)"
    if "biologic_assay" in detected:
        return LIGAND_BINDING, "Label text describes a recombinant/monoclonal protein product"
    return CHROMATOGRAPHIC, "Default for an unclassified product (small-molecule LC-MS/MS assay)"


# ---------------------------------------------------------------------------
# Calibration range
# ---------------------------------------------------------------------------


def _calibration(
    cmax: Optional[dict[str, Any]],
    rejected: list[dict[str, Any]],
    crit: AssayCriteria,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Derive LLOQ/ULOQ from the anticipated Cmax, or refuse to and say why."""
    flags: list[dict[str, Any]] = []
    if cmax is None:
        flags.append({
            "key": "cmax_unavailable",
            "severity": "high",
            "message": (
                "No usable Cmax was extracted from the literature or label, so the "
                "calibration range CANNOT be derived. The protocol must state the range as "
                "to be established during method development / from the reference product's "
                "PK, and must not quote a numeric LLOQ or ULOQ until it is."
            ),
        })
        if rejected:
            flags.append({
                "key": "cmax_units_unconvertible",
                "severity": "medium",
                "message": (
                    "Cmax record(s) were found but could not be converted to a mass/volume "
                    "concentration: "
                    + "; ".join(
                        f"{r.get('value', '?')} {r.get('unit') or 'no unit'} ({r['reason']})"
                        for r in rejected
                    )
                    + ". Molar or activity units require a molecular weight / potency "
                    "assignment, which is a bioanalytical decision, not an arithmetic one."
                ),
            })
        return {
            "derived": False,
            "unit": "ng/mL",
            "anticipated_cmax": None,
            "lloq": None,
            "uloq": None,
            "rejected_records": rejected,
            "statement": (
                "The calibration range is NOT established in this plan because no anticipated "
                "Cmax could be sourced. It must be set during method development so that the "
                "ULOQ exceeds the anticipated Cmax by at least "
                f"{ULOQ_MARGIN_FACTOR:.0f}-fold and the LLOQ is no higher than "
                f"{LLOQ_FRACTION_OF_CMAX * 100:.0f}% of it."
            ),
        }, flags

    cmax_ng = cmax["value_ng_per_ml"]
    uloq = _sig_round(cmax_ng * ULOQ_MARGIN_FACTOR, up=True)
    lloq = _sig_round(cmax_ng * LLOQ_FRACTION_OF_CMAX, up=False)
    if lloq <= 0:
        # Defensive: an extremely small Cmax could round the LLOQ to zero.
        lloq = cmax_ng * LLOQ_FRACTION_OF_CMAX
    half_lives = math.log(cmax_ng / lloq, 2) if lloq > 0 else 0.0

    qc_levels = [
        {"key": "lloq_qc", "label": "LLOQ QC",
         "concentration": _round_conc(lloq),
         "basis": "At the LLOQ"},
        {"key": "low_qc", "label": "Low QC",
         "concentration": _round_conc(lloq * LOW_QC_MULTIPLE_OF_LLOQ),
         "basis": f"Within {LOW_QC_MULTIPLE_OF_LLOQ:.0f}x the LLOQ"},
        {"key": "mid_qc", "label": "Medium QC",
         "concentration": _round_conc(uloq * MID_QC_FRACTION_OF_ULOQ),
         "basis": f"{MID_QC_FRACTION_OF_ULOQ * 100:.0f}% of the calibration range (30-50% window)"},
        {"key": "high_qc", "label": "High QC",
         "concentration": _round_conc(uloq * HIGH_QC_FRACTION_OF_ULOQ),
         "basis": f"{HIGH_QC_FRACTION_OF_ULOQ * 100:.0f}% of the ULOQ (>= 75% required)"},
    ]

    statement = (
        f"The validated calibration range is {lloq:g} to {uloq:g} ng/mL. The upper limit is "
        f"{uloq / cmax_ng:.1f}-fold the anticipated Cmax of {_round_conc(cmax_ng):g} ng/mL "
        f"(highest value reported in the sourced pharmacokinetic literature), providing headroom "
        f"for the upper tail of the exposure distribution. The lower limit is "
        f"{lloq / cmax_ng * 100:.1f}% of the anticipated Cmax, permitting approximately "
        f"{half_lives:.1f} terminal half-lives of quantifiable decline so that AUC0-t reaches at "
        f"least 80% of AUC0-inf and the terminal rate constant can be estimated from at least "
        f"three quantifiable timepoints."
    )
    if rejected:
        flags.append({
            "key": "cmax_units_unconvertible",
            "severity": "low",
            "message": (
                "Some Cmax records were excluded from the derivation because their units could "
                "not be converted: "
                + "; ".join(
                    f"{r.get('value', '?')} {r.get('unit') or 'no unit'} ({r['reason']})"
                    for r in rejected
                )
                + "."
            ),
        })

    return {
        "derived": True,
        "unit": "ng/mL",
        "anticipated_cmax": {
            "value": _round_conc(cmax_ng),
            "unit": "ng/mL",
            "reported_as": f"{cmax['reported_value']:g} {cmax['reported_unit'] or ''}".strip(),
            "population": cmax.get("population"),
            "source_pmid": cmax.get("source_pmid"),
            "records_considered": cmax.get("n_records_considered"),
            "basis": cmax.get("basis"),
        },
        "lloq": lloq,
        "uloq": uloq,
        "uloq_margin_x": round(uloq / cmax_ng, 2),
        "lloq_pct_of_cmax": round(lloq / cmax_ng * 100.0, 2),
        "dynamic_range_x": round(uloq / lloq, 1),
        "half_lives_quantifiable": round(half_lives, 1),
        "min_calibration_standards": MIN_CALIBRATION_STANDARDS,
        "qc_levels": qc_levels,
        "rejected_records": rejected,
        "statement": statement,
    }, flags


# ---------------------------------------------------------------------------
# Analyte selection
# ---------------------------------------------------------------------------


def _analytes(text: str, detected: set[str], drug_inn: str, crit: AssayCriteria) -> dict[str, Any]:
    """Decide what is measured, grounded in the label rather than recall.

    The FDA position, which this encodes: measure the PARENT drug released from
    the dosage form; bioequivalence is concluded on the parent. A metabolite is
    added only when it is formed presystemically and contributes meaningfully to
    activity, or when the parent is not reliably measurable - and even then it is
    supportive, except for a prodrug whose parent is not quantifiable, where the
    active moiety necessarily becomes the primary analyte.
    """
    inn = (drug_inn or "the investigational product").strip()
    primary: list[dict[str, Any]] = []
    additional: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []

    is_prodrug = "prodrug" in detected
    has_active_metabolite = "active_metabolite" in detected

    if is_prodrug:
        be_basis = "active moiety"
        primary.append({
            "analyte": f"Active moiety derived from {inn}",
            "role": "primary",
            "rationale": (
                "The label identifies the administered substance as a prodrug, so the parent is "
                "converted presystemically and is frequently not quantifiable for long enough to "
                "characterise exposure. The active moiety therefore carries the "
                "bioequivalence/similarity conclusion."
            ),
            "evidence": "Prodrug language in the approved label",
            "quote": _quote(text, "prodrug"),
        })
        additional.append({
            "analyte": f"Intact prodrug ({inn})",
            "role": "supportive",
            "rationale": (
                "The intact prodrug is measured additionally WHERE IT IS QUANTIFIABLE, because it "
                "reflects the release and absorption step that the formulations are actually being "
                "compared on. It is supportive, not the basis of the conclusion."
            ),
            "evidence": "Prodrug language in the approved label",
            "quote": "",
        })
        decisions.append({
            "key": "prodrug",
            "decision": "Measure the active moiety as the primary analyte; measure the intact prodrug where quantifiable.",
            "confirm": (
                "Confirm from the full label and the reference product's PK whether the intact "
                "prodrug is quantifiable in plasma; if it is not, state that explicitly."
            ),
        })
    else:
        be_basis = "parent drug"
        primary.append({
            "analyte": f"Parent drug ({inn})",
            "role": "primary",
            "rationale": (
                "Measurement of the parent drug released from the dosage form is the default: it "
                "is the moiety most sensitive to differences in formulation performance, whereas a "
                "metabolite is buffered by the metabolic step and can mask an absorption "
                "difference."
            ),
            "evidence": "FDA bioequivalence guidance (analyte selection)",
            "quote": "",
        })

    if has_active_metabolite:
        # Exactly ONE analyte may carry the bioequivalence conclusion. For a
        # prodrug the labelled "active metabolite" is ordinarily the same
        # substance as the active moiety already designated primary above, so it
        # is recorded as supportive with an identity check rather than as a
        # second primary - two primary analytes in a protocol is a review
        # question ("which one decides bioequivalence?"), not a stronger plan.
        metabolite_rationale = (
            "The label describes a pharmacologically active or major circulating metabolite. "
            "It is measured when it is formed presystemically and contributes meaningfully to "
            "safety or efficacy. The bioequivalence conclusion still rests on the "
            f"{be_basis}; the metabolite data are supportive."
        )
        if is_prodrug:
            metabolite_rationale += (
                " For this prodrug the labelled active metabolite is ordinarily the SAME "
                "substance as the active moiety already designated the primary analyte - "
                "confirm the identity from the label rather than listing it twice; only a "
                "distinct downstream active metabolite is an extra analyte."
            )
        additional.append({
            "analyte": "Active / major circulating metabolite named in the label",
            "role": "supportive",
            "rationale": metabolite_rationale,
            "evidence": "Active-metabolite language in the approved label",
            "quote": _quote(text, "active_metabolite"),
        })
        decisions.append({
            "key": "active_metabolite",
            "decision": (
                "Add the labelled active/major circulating metabolite as an additional analyte; "
                f"the bioequivalence conclusion remains based on the {be_basis}."
            ),
            "confirm": (
                "Name the metabolite from the label and confirm that a characterised reference "
                "standard is available before the method is validated."
                + (" For a prodrug, first confirm whether this metabolite IS the active moiety."
                   if is_prodrug else "")
            ),
        })

    chiral = {
        "racemate_detected": "chiral" in detected,
        # The default is ALWAYS the achiral assay. FDA requires all four
        # conditions below to be met before an enantiomer-specific assay is
        # recommended, so detecting a racemate is a prompt to test those
        # conditions, never an automatic switch.
        "assay": "Achiral assay measuring the racemate (total drug)",
        "conditions_for_chiral_assay": list(_CHIRAL_CONDITIONS),
        "rationale": (
            "An achiral assay of the racemate is the regulatory default. An enantiomer-specific "
            "assay is recommended only when ALL of the listed conditions are met; a chiral method "
            "is otherwise slower to develop, harder to validate and adds no interpretive value."
        ),
        "quote": _quote(text, "chiral") if "chiral" in detected else "",
    }
    if chiral["racemate_detected"]:
        decisions.append({
            "key": "chiral",
            "decision": (
                "Racemate detected in the label. Retain the achiral assay unless ALL FOUR FDA "
                "conditions are met, in which case validate an enantiomer-specific method and "
                "report each enantiomer separately."
            ),
            "confirm": "Assess the four conditions against the label and the PK literature.",
        })

    return {
        "primary": primary,
        "additional": additional,
        "be_basis": be_basis,
        "chiral_assessment": chiral,
        "decisions": decisions,
        "detected_cues": sorted(detected),
        "statement": (
            "Bioequivalence/pharmacokinetic similarity is concluded on the "
            f"{be_basis}. "
            + ("An additional analyte is measured in support (see the analyte table). "
               if additional else "")
            + f"The assay is {crit.label.lower()}."
        ),
    }


# ---------------------------------------------------------------------------
# Matrix and sample handling
# ---------------------------------------------------------------------------


def _matrix_and_handling(
    crit: AssayCriteria, detected: set[str], drug_inn: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Collection matrix, the physical handling chain, and chain of custody."""
    flags: list[dict[str, Any]] = []
    inn = (drug_inn or "").strip().lower()
    is_lba = crit.key == "ligand_binding"

    if inn in _WHOLE_BLOOD_ANALYTES:
        matrix, tube = "Whole blood", "K2EDTA (whole blood, NOT separated)"
        matrix_basis = (
            "This analyte partitions extensively into erythrocytes and is measured in whole "
            "blood by regulatory convention and in routine therapeutic monitoring; a plasma "
            "concentration would not be comparable with any reference value."
        )
    elif is_lba:
        matrix, tube = "Serum", "Serum separator / clot-activator tube (no anticoagulant)"
        matrix_basis = (
            "Serum is the conventional matrix for a ligand-binding assay of a therapeutic "
            "protein and is the matrix in which the reference product's exposure data were "
            "generated."
        )
    else:
        matrix, tube = "Plasma", "K2EDTA (potassium ethylenediaminetetraacetic acid)"
        matrix_basis = (
            "K2EDTA plasma is the default matrix for a small-molecule LC-MS/MS assay; EDTA "
            "chelation also suppresses some metal-dependent ex vivo degradation."
        )

    if "whole_blood" in detected and inn not in _WHOLE_BLOOD_ANALYTES:
        flags.append({
            "key": "erythrocyte_partitioning",
            "severity": "medium",
            "message": (
                "The label describes whole-blood distribution or erythrocyte partitioning. "
                "Confirm during method development whether whole blood, rather than "
                f"{matrix.lower()}, is the correct regulatory matrix for this analyte."
            ),
        })

    steps: list[dict[str, str]] = [
        {
            "key": "collection",
            "label": "Collection",
            "detail": (
                f"Draw venous blood into a pre-labelled {tube} tube via an indwelling cannula "
                "(or direct venepuncture), discarding the cannula dead volume before each draw."
            ),
            "rationale": (
                "Discarding the dead volume prevents saline dilution and residual carryover from "
                "the previous flush, both of which distort a single timepoint rather than the "
                "whole profile."
            ),
        },
    ]

    if matrix == "Serum":
        steps.append({
            "key": "clotting",
            "label": "Clotting",
            "detail": (
                f"Allow the tube to clot upright at room temperature for {SERUM_CLOT_MINUTES} "
                "minutes. Do NOT place a serum tube on ice before clotting is complete."
            ),
            "rationale": (
                "Chilling before the clot has formed leaves fibrin in the separated serum and "
                "produces latex-like strands that block pipette tips and bias volume-dependent "
                "assay steps."
            ),
        })
    else:
        steps.append({
            "key": "cooling",
            "label": "Immediate cooling",
            "detail": (
                "Invert gently 8-10 times to mix with the anticoagulant and place immediately in "
                "an ice-water bath, protected from light."
            ),
            "rationale": (
                "Cooling arrests enzymatic (esterase, amidase) conversion in the interval between "
                "the draw and centrifugation - the part of the chain no plasma stability "
                "experiment covers."
            ),
        })

    if matrix == "Whole blood":
        steps.append({
            "key": "no_separation",
            "label": "No separation",
            "detail": (
                "Do NOT centrifuge or separate. Aliquot the mixed whole blood directly into "
                f"{ALIQUOTS_PER_SAMPLE} pre-labelled polypropylene cryovials."
            ),
            "rationale": (
                "The measurand is the whole-blood concentration; separating plasma would discard "
                "the erythrocyte-bound fraction that carries most of the dose."
            ),
        })
    else:
        steps.append({
            "key": "centrifugation",
            "label": "Centrifugation",
            "detail": (
                f"Centrifuge at approximately {CENTRIFUGE_G} x g for {CENTRIFUGE_MINUTES} minutes "
                f"at {CENTRIFUGE_TEMP_C} C, within {MAX_MINUTES_TO_CENTRIFUGE} minutes of "
                "collection."
            ),
            "rationale": (
                "A fixed, documented g-force, time and temperature keeps cell lysis and platelet "
                "carryover constant across sites and days, so processing does not become a "
                "hidden source of between-sample variability."
            ),
        })
        steps.append({
            "key": "aliquoting",
            "label": "Aliquoting",
            "detail": (
                f"Transfer the separated {matrix.lower()} into {ALIQUOTS_PER_SAMPLE} pre-labelled "
                "polypropylene cryovials (a primary and a back-up aliquot of equal volume), "
                "avoiding the cell layer."
            ),
            "rationale": (
                "The back-up aliquot is what makes incurred sample reanalysis and any repeat "
                "possible without subjecting the primary aliquot to an extra freeze-thaw cycle."
            ),
        })

    steps.extend([
        {
            "key": "labelling",
            "label": "Labelling",
            "detail": (
                "Label every aliquot with a unique sample identifier encoding protocol number, "
                "subject number, period, nominal timepoint and aliquot (A/B), using "
                "freezer-grade labels applied before collection."
            ),
            "rationale": (
                "A mislabelled aliquot is unrecoverable: the concentration is real but assigned "
                "to the wrong subject or timepoint, which corrupts both profiles it touches."
            ),
        },
        {
            "key": "freezing",
            "label": "Freezing",
            "detail": (
                f"Place aliquots upright at {DEFAULT_STORAGE_TEMP_C:.0f} C (accepted range -60 to "
                f"-80 C) within {MAX_MINUTES_TO_FREEZER} minutes of collection; record the "
                "actual freezing time."
            ),
            "rationale": (
                "The recorded collection-to-freezer interval is what is compared against the "
                "validated bench-top stability window; without it the stability claim cannot be "
                "shown to cover the samples."
            ),
        },
        {
            "key": "storage",
            "label": "Storage",
            "detail": (
                f"Store at {DEFAULT_STORAGE_TEMP_C:.0f} C in a restricted-access freezer with "
                "continuous temperature monitoring and audible/remote alarm; store the primary "
                "and back-up aliquots in separate freezers or separate compartments."
            ),
            "rationale": (
                "Splitting the aliquots means a single freezer failure cannot destroy the study's "
                "only copy of a subject's profile."
            ),
        },
        {
            "key": "shipment",
            "label": "Shipment",
            "detail": (
                "Ship to the bioanalytical facility on dry ice in validated insulated packaging "
                "with a calibrated temperature logger in each shipment, by a courier under a "
                "shipping agreement, with the manifest sent to the laboratory in advance and the "
                "back-up aliquots shipped separately."
            ),
            "rationale": (
                "The logger converts the cold chain from an assumption into evidence, and the "
                "separate shipment means a lost or thawed consignment does not end the study."
            ),
        },
    ])

    custody = [
        {
            "key": "manifest",
            "requirement": (
                "A sample manifest listing every aliquot by unique identifier, subject, period "
                "and nominal timepoint accompanies each shipment and is reconciled on receipt."
            ),
        },
        {
            "key": "receipt",
            "requirement": (
                "The receiving laboratory documents date/time of receipt, condition (dry ice "
                "present, temperature-logger trace, integrity of each vial) and any discrepancy, "
                "and reports discrepancies to the sponsor within 24 hours."
            ),
        },
        {
            "key": "accountability",
            "requirement": (
                "A continuous chain-of-custody record links every transfer of every aliquot - "
                "clinic to storage, storage to courier, courier to laboratory, freezer to bench - "
                "with the responsible individual, date and time."
            ),
        },
        {
            "key": "excursions",
            "requirement": (
                "Any temperature excursion is documented with its duration and magnitude and "
                "assessed against the validated stability data before the affected samples are "
                "analysed or excluded."
            ),
        },
        {
            "key": "records",
            "requirement": (
                "Sample receipt, storage, analysis and disposition are recorded in a validated, "
                "21 CFR Part 11 compliant LIMS with a secure, computer-generated, time-stamped "
                "audit trail."
            ),
        },
        {
            "key": "retention",
            "requirement": (
                "Study samples are retained until the analysis and the study report are complete "
                "and the sponsor authorises disposal; reserve samples of the test and reference "
                "products are retained separately in accordance with 21 CFR 320.38 and 320.63."
            ),
        },
    ]

    return {
        "matrix": matrix,
        "collection_tube": tube,
        "matrix_basis": matrix_basis,
        "storage_temp_c": DEFAULT_STORAGE_TEMP_C,
        "aliquots_per_sample": ALIQUOTS_PER_SAMPLE,
        "steps": steps,
        "chain_of_custody": custody,
    }, flags


# ---------------------------------------------------------------------------
# ISR, laboratory qualification, immunogenicity
# ---------------------------------------------------------------------------


def isr_sample_count(total_samples: int) -> int:
    """Number of incurred sample reanalysis (ISR) samples required.

    ``ISR_FIRST_TIER_PCT`` of the first ``ISR_FIRST_TIER_SAMPLES`` study samples
    plus ``ISR_SECOND_TIER_PCT`` of any beyond that, rounded UP - the count is a
    minimum, so a fractional sample is always taken as a whole one.
    """
    n = max(0, int(total_samples or 0))
    if n == 0:
        return 0
    first = min(n, ISR_FIRST_TIER_SAMPLES)
    beyond = max(0, n - ISR_FIRST_TIER_SAMPLES)
    return math.ceil(first * ISR_FIRST_TIER_PCT / 100.0 + beyond * ISR_SECOND_TIER_PCT / 100.0)


def _isr_plan(crit: AssayCriteria) -> dict[str, Any]:
    return {
        "required": True,
        "rule": (
            f"{ISR_FIRST_TIER_PCT:.0f}% of the first {ISR_FIRST_TIER_SAMPLES} study samples plus "
            f"{ISR_SECOND_TIER_PCT:.0f}% of any samples beyond {ISR_FIRST_TIER_SAMPLES}, rounded up."
        ),
        "first_tier_samples": ISR_FIRST_TIER_SAMPLES,
        "first_tier_pct": ISR_FIRST_TIER_PCT,
        "second_tier_pct": ISR_SECOND_TIER_PCT,
        "acceptance": (
            f"At least {ISR_MIN_PASS_PCT:.0f}% of the repeated samples must agree with the "
            f"original result to within {crit.isr_window_pct:.0f}% of the mean of the two values."
        ),
        "agreement_window_pct": crit.isr_window_pct,
        "min_pass_pct": ISR_MIN_PASS_PCT,
        "selection": (
            "Select samples spanning the concentration range - specifically around Cmax and in "
            "the elimination phase - and across subjects, periods and treatments, so that a "
            "treatment- or period-specific problem cannot hide."
        ),
        "conduct": (
            "Repeats are analysed from the back-up aliquot in a separate analytical run on a "
            "different day, with the original result not visible to the analyst."
        ),
        "example_counts": [
            {"total_samples": n, "isr_samples": isr_sample_count(n)}
            for n in (400, 800, 1600)
        ],
        "sizing_note": (
            "The absolute number is fixed once enrolment and the sampling schedule are final; "
            "apply the rule to the total number of study samples actually analysed."
        ),
        "rationale": (
            "Validation is performed on spiked matrix. ISR is the only check that the method "
            "reproduces a result in REAL samples, which contain metabolites, concomitant "
            "medications and the products of ex vivo conversion that spiked standards do not. A "
            "method can pass every validation criterion and still fail ISR."
        ),
        "reference": "FDA BMV 2018 (Incurred Sample Reanalysis); ICH M10 section 7.3",
    }


def _lab_qualification(crit: AssayCriteria) -> dict[str, Any]:
    return {
        "facility": "[Bioanalytical facility name, address and site identifier to be inserted]",
        "facility_named": False,
        "requirements": [
            {
                "key": "quality_system",
                "requirement": (
                    "The facility operates a documented quality system compliant with GLP "
                    "(21 CFR Part 58) or an equivalent quality standard for bioanalysis of "
                    "clinical samples, with an independent quality assurance unit."
                ),
            },
            {
                "key": "inspection_history",
                "requirement": (
                    "Regulatory inspection history is disclosed to the sponsor, and the facility "
                    "is not subject to an unresolved finding affecting bioanalytical data "
                    "integrity."
                ),
            },
            {
                "key": "validation_before_analysis",
                "requirement": (
                    "Full method validation is complete and the validation report approved BEFORE "
                    "the first study sample is analysed; a partial validation is performed and "
                    "documented for any change of matrix, anticoagulant, species, instrument "
                    "platform or calibration range."
                ),
            },
            {
                "key": "analytical_plan",
                "requirement": (
                    "A study-specific bioanalytical plan (analytical method reference, run "
                    "acceptance criteria, reanalysis rules, ISR design) is approved before sample "
                    "analysis begins."
                ),
            },
            {
                "key": "part11",
                "requirement": (
                    "Chromatographic data systems and the LIMS are validated and 21 CFR Part 11 "
                    "compliant, with unique user accounts, no shared logins, and an audit trail "
                    "that cannot be disabled."
                ),
            },
            {
                "key": "personnel",
                "requirement": (
                    "Training records, CVs and job descriptions are current for every analyst "
                    "performing or reviewing the method."
                ),
            },
            {
                "key": "documentation",
                "requirement": (
                    "The final bioanalytical report includes the validation summary, all run "
                    "acceptance data, the ISR outcome, a sample storage/analysis timeline "
                    "demonstrating coverage by the stability claims, and a listing of every "
                    "reanalysed sample with its reason."
                ),
            },
        ],
        "cross_validation": {
            "required_when": (
                "More than one laboratory or analytical site analyses study samples, or the "
                "method or platform changes during the study."
            ),
            "design": (
                f"Analyse the same set of at least {CROSS_VALIDATION_MIN_SAMPLES} study samples "
                "(spanning the calibration range, or spiked QC samples at three levels where "
                "study samples are not available) at both sites, before or concurrently with "
                "study sample analysis."
            ),
            "acceptance": (
                f"At least {ISR_MIN_PASS_PCT:.0f}% of the paired results within "
                f"{crit.isr_window_pct:.0f}% of the mean of the two values; QC-based comparisons "
                f"within +/-{crit.re_pct:.0f}% of nominal at each site."
            ),
            "rationale": (
                "Without cross-validation, a systematic offset between two laboratories is "
                "indistinguishable from a treatment effect if the sites did not analyse balanced "
                "sets of test and reference samples."
            ),
            "reference": "ICH M10 section 3.3 (Cross-validation)",
        },
    }


def _immunogenicity(study_type: str, crit: AssayCriteria) -> Optional[dict[str, Any]]:
    """ADA/NAb assay package - biologics only.

    ICH M10 explicitly excludes anti-drug antibody assays from its scope, so
    these criteria come from the FDA immunogenicity testing guidance instead.
    """
    if crit.key != "ligand_binding":
        return None
    return {
        "required": True,
        "tiers": [
            {
                "tier": "Screening",
                "assay": "Bridging ELISA / ECL immunoassay",
                "criterion": (
                    "Screening cut point set to a 5% false-positive rate, established from at "
                    "least 50 drug-naive individual donor samples."
                ),
            },
            {
                "tier": "Confirmatory",
                "assay": "Competitive inhibition with excess drug",
                "criterion": "Confirmatory cut point set to a 1% false-positive rate.",
            },
            {
                "tier": "Titre",
                "assay": "Serial dilution of confirmed-positive samples",
                "criterion": "Reported as a titre to allow magnitude comparison between arms.",
            },
            {
                "tier": "Neutralising",
                "assay": "Cell-based or competitive ligand-binding NAb assay",
                "criterion": "Performed on all confirmed ADA-positive samples.",
            },
        ],
        "validation_elements": [
            f"Sensitivity of at least {ADA_ASSAY_SENSITIVITY_NG_PER_ML:.0f} ng/mL of a positive "
            "control antibody.",
            "Drug tolerance demonstrated at the drug concentrations actually present in the "
            "immunogenicity samples, which for a single-dose PK study means the trough "
            "concentrations at the ADA sampling times.",
            "Assessment of prozone (hook) effect, matrix interference and specificity.",
        ],
        "rationale": (
            "For a biosimilar PK study, anti-drug antibodies both are an endpoint in their own "
            "right and confound the PK endpoint: an ADA-positive subject can clear the drug "
            "faster, so exposure differences must be interpretable against ADA status. Drug "
            "tolerance is the parameter that most often invalidates an ADA dataset, because "
            "circulating drug masks the antibody."
        ),
        "reference": (
            "FDA Immunogenicity Testing of Therapeutic Protein Products (2019); ICH M10 "
            "section 1.2 places ADA assays outside its scope"
        ),
        "applies_to_study_type": (study_type or "").strip().lower() or "biosimilar_pk",
    }


# ---------------------------------------------------------------------------
# Rendering the catalogues against the resolved criteria
# ---------------------------------------------------------------------------


def _render_tokens(crit: AssayCriteria) -> dict[str, Any]:
    return {
        "re": f"{crit.re_pct:.0f}",
        "cv": f"{crit.cv_pct:.0f}",
        "re_lloq": f"{crit.re_lloq_pct:.0f}",
        "cv_lloq": f"{crit.cv_lloq_pct:.0f}",
        "lots": crit.selectivity_lots,
        "stds": MIN_CALIBRATION_STANDARDS,
        "dilution": DILUTION_INTEGRITY_FACTOR,
        "storage": f"{DEFAULT_STORAGE_TEMP_C:.0f}",
        "bench_h": f"{BENCHTOP_STABILITY_HOURS:.0f}",
        "ft_cycles": FREEZE_THAW_CYCLES,
        "lt_months": LONG_TERM_STABILITY_MONTHS,
        "auto_h": f"{PROCESSED_SAMPLE_STABILITY_HOURS:.0f}",
    }


def _render_parameters(crit: AssayCriteria) -> list[dict[str, Any]]:
    tokens = _render_tokens(crit)
    out: list[dict[str, Any]] = []
    for p in _PARAMETERS:
        if p.assay_classes not in ("both", crit.key):
            continue
        note = ""
        if p.key == "recovery" and crit.key == "ligand_binding":
            note = "Not applicable to a ligand-binding assay (no extraction step)."
        enriched = BMVRequirement(**{
            **asdict(p),
            "requirement": p.requirement.format(**tokens),
            "acceptance": p.acceptance.format(**tokens),
            "applicable": True,
            "note": note,
        }).to_dict()
        out.append(enriched)

    if crit.key == "ligand_binding":
        # State the criteria an LBA adds and the ones it drops, rather than
        # silently omitting them - a reviewer needs to see the reasoning.
        out.append(BMVRequirement(
            key="total_error",
            label="Total error",
            category="performance",
            requirement=(
                "Compute total error (absolute relative error plus coefficient of variation) at "
                "each QC level."
            ),
            acceptance=(
                f"Total error <= {crit.total_error_pct:.0f}% at low, medium and high QC and "
                f"<= {crit.total_error_lloq_pct:.0f}% at the LLOQ."
            ),
            rationale=(
                "A ligand-binding assay can be accurate on average while imprecise, or precise "
                "while biased. Total error caps the combination, which is what actually "
                "determines whether a single reported concentration is trustworthy."
            ),
            reference="ICH M10 section 4.2 (Total error)",
            assay_classes="ligand_binding",
        ).to_dict())
        out.append(BMVRequirement(
            key="extraction_recovery_na",
            label="Extraction recovery (not applicable)",
            category="performance",
            requirement=(
                "Not performed. A ligand-binding assay has no extraction step; the equivalent "
                "control is establishment of the minimum required dilution (MRD) and "
                "demonstration of parallelism."
            ),
            acceptance="Not applicable.",
            rationale=(
                "Recorded explicitly so the absence of a recovery experiment is a stated design "
                "choice rather than an apparent omission at review."
            ),
            reference="ICH M10 section 4",
            assay_classes="ligand_binding",
            applicable=False,
        ).to_dict())
    return out


def _render_stability(
    crit: AssayCriteria, detected: set[str], matrix: str, is_prodrug: bool
) -> list[dict[str, Any]]:
    tokens = _render_tokens(crit)
    whole_blood_needed = (
        matrix == "Whole blood"
        or is_prodrug
        or "ex_vivo_instability" in detected
        or "whole_blood" in detected
    )
    out: list[dict[str, Any]] = []
    for s in _STABILITY:
        applicable = True
        note = ""
        if s.key == "whole_blood":
            applicable = whole_blood_needed
            if applicable:
                if matrix == "Whole blood":
                    note = "Required: whole blood is the analytical matrix."
                elif is_prodrug:
                    note = (
                        "Required: the label describes a prodrug/ester, which blood esterases can "
                        "convert before centrifugation. Consider an esterase inhibitor "
                        "(e.g. sodium fluoride) or immediate chilling and acidification, "
                        "confirmed during method development."
                    )
                else:
                    note = (
                        "Required: the label carries an ex vivo instability or erythrocyte "
                        "partitioning signal - confirm during method development."
                    )
            else:
                note = (
                    "Not triggered: no whole-blood matrix, prodrug/ester or ex vivo instability "
                    "signal was found in the label. Add it if method development shows "
                    "instability in blood."
                )
        if s.key == "processed_sample" and crit.key == "ligand_binding":
            note = (
                "For a plate-based assay this covers the interval between plate preparation and "
                "reading, including any repeat read."
            )
        out.append(StabilityTest(**{
            **asdict(s),
            "condition": s.condition.format(**tokens),
            "acceptance": s.acceptance.format(**tokens),
            "applicable": applicable,
            "note": note,
        }).to_dict())
    return out


def _assumptions(calibration: dict[str, Any]) -> list[dict[str, Any]]:
    """The engine's named assumptions, surfaced so the protocol discloses them."""
    items = [
        {
            "constant": "ULOQ_MARGIN_FACTOR",
            "value": ULOQ_MARGIN_FACTOR,
            "applies_to": "Upper limit of quantification",
            "why": (
                "The ULOQ is set to at least this multiple of the anticipated Cmax so that the "
                "upper tail of the exposure distribution stays on-curve; a sample above the ULOQ "
                "must be diluted and reanalysed."
            ),
        },
        {
            "constant": "LLOQ_FRACTION_OF_CMAX",
            "value": LLOQ_FRACTION_OF_CMAX,
            "applies_to": "Lower limit of quantification",
            "why": (
                "The LLOQ is set no higher than this fraction of the anticipated Cmax, which "
                "corresponds to about 4.3 terminal half-lives of quantifiable decline and keeps "
                "AUC0-t at or above 80% of AUC0-inf."
            ),
        },
        {
            "constant": "DILUTION_INTEGRITY_FACTOR",
            "value": DILUTION_INTEGRITY_FACTOR,
            "applies_to": "Dilution integrity / dilutional linearity",
            "why": (
                "Validating this dilution factor allows a sample up to this multiple of the ULOQ "
                "to be reported without a protocol deviation."
            ),
        },
        {
            "constant": "DEFAULT_STORAGE_TEMP_C",
            "value": DEFAULT_STORAGE_TEMP_C,
            "applies_to": "Long-term sample storage and freeze-thaw stability",
            "why": (
                "Nominal storage temperature (accepted range -60 to -80 C), the more conservative "
                "of the two conventional options and the one at which long-term stability is most "
                "commonly established."
            ),
        },
        {
            "constant": "LONG_TERM_STABILITY_MONTHS",
            "value": LONG_TERM_STABILITY_MONTHS,
            "applies_to": "Long-term frozen stability claim",
            "why": (
                "Minimum claim assumed for planning; the validated duration must equal or exceed "
                "the real interval from first collection to last analysis."
            ),
        },
        {
            "constant": "ISR_FIRST_TIER_SAMPLES",
            "value": ISR_FIRST_TIER_SAMPLES,
            "applies_to": "Incurred sample reanalysis sizing",
            "why": (
                "The ISR tier boundary: 10% of the first 1000 study samples and 5% beyond, per "
                "the FDA bioanalytical method validation guidance and ICH M10."
            ),
        },
    ]
    if not calibration.get("derived"):
        items.append({
            "constant": "calibration_range",
            "value": None,
            "applies_to": "Calibration range",
            "why": (
                "NOT derived - no anticipated Cmax was available. The range must be established "
                "during method development and inserted before the protocol is finalised."
            ),
        })
    return items


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def plan_bioanalytical(
    pk_parameters: Optional[list[dict[str, Any]]] = None,
    label: Optional[dict[str, str]] = None,
    drug_inn: str = "",
    study_type: str = "BE",
    is_biologic: Optional[bool] = None,
    dose_mg: Optional[float] = None,
) -> dict[str, Any]:
    """Build the bioanalytical method validation package for THIS study.

    Parameters
    ----------
    pk_parameters:
        The extracted PK records already assembled for the prompt context (see
        routers/protocols.py::_build_context) - dicts of
        ``parameter``/``value``/``unit``/``population``/``source_pmid``. The
        Cmax records drive the calibration range; everything else is ignored.
    label:
        The trimmed DailyMed label dict (``_fetch_label_safe``). Its text
        grounds analyte selection (prodrug, active metabolite, racemate),
        matrix choice and the ex vivo stability triggers.
    drug_inn:
        Used for the analyte names, the whole-blood matrix look-up, and the
        modality fallback when ``is_biologic`` is not supplied.
    study_type:
        'BE' | 'biosimilar_pk' | 'food_effect'. A biosimilar PK study forces the
        ligand-binding criteria and adds the ADA/NAb package.
    is_biologic:
        Explicit modality when the caller already knows it; overrides every
        other assay-class signal.
    dose_mg:
        Planned single dose, recorded for disclosure. It is NOT used to rescale
        the literature Cmax, because the dose at which that Cmax was observed is
        not carried on the PK record - a flag asks for that confirmation instead
        of silently applying dose proportionality.

    Returns
    -------
    dict, JSON-serialisable for storage in population_config (JSONB), with
    ``assay_class``, ``calibration``, ``analytes``, ``parameters``,
    ``stability``, ``isr``, ``sample_handling``, ``lab_qualification``,
    ``immunogenicity``, ``assumptions``, ``flags`` and ``summary``.
    """
    # Externally supplied identifiers are echoed into analyte names and the
    # summary, so they are ASCII-folded once here (see _ascii()).
    drug_inn = _ascii(drug_inn)
    study_type = _ascii(study_type)

    text = " ".join(
        v for v in (label or {}).values() if isinstance(v, str) and v
    )
    detected = _detect(text)
    crit, class_basis = _resolve_assay_class(is_biologic, study_type, drug_inn, detected)

    cmax, rejected = _anticipated_cmax(pk_parameters)
    calibration, flags = _calibration(cmax, rejected, crit)

    analytes = _analytes(text, detected, drug_inn, crit)
    handling, handling_flags = _matrix_and_handling(crit, detected, drug_inn)
    flags.extend(handling_flags)

    is_prodrug = "prodrug" in detected
    parameters = _render_parameters(crit)
    stability = _render_stability(crit, detected, handling["matrix"], is_prodrug)

    dilution_key = (
        "dilution_integrity" if crit.key == "chromatographic"
        else "dilutional_linearity_parallelism"
    )
    dilution = next((p for p in parameters if p["key"] == dilution_key), None)
    dilution_block = {
        "required": True,
        "factor": DILUTION_INTEGRITY_FACTOR,
        "trigger": (
            "Required because a measured concentration can exceed the ULOQ: the ULOQ is set at "
            f"{ULOQ_MARGIN_FACTOR:.0f}x the anticipated Cmax, and individual subjects, "
            "formulation differences or a shortened sampling interval can push a peak sample "
            "above it."
        ),
        "reportable_ceiling": (
            f"Validating a {DILUTION_INTEGRITY_FACTOR}-fold dilution allows concentrations up to "
            f"{DILUTION_INTEGRITY_FACTOR} x ULOQ"
            + (
                f" ({calibration['uloq'] * DILUTION_INTEGRITY_FACTOR:g} ng/mL, i.e. "
                f"{DILUTION_INTEGRITY_FACTOR * ULOQ_MARGIN_FACTOR:.0f}x the anticipated Cmax)"
                if calibration.get("derived") else ""
            )
            + " to be reported without a protocol deviation."
        ),
        "detail": dilution,
    }

    if dose_mg:
        flags.append({
            "key": "dose_scaling_unverified",
            "severity": "medium",
            "message": (
                f"The planned dose is {dose_mg:g} mg. The anticipated Cmax used for the "
                "calibration range comes from published PK whose dose is not carried on the "
                "record. Confirm the literature dose and, if it differs, rescale the anticipated "
                "Cmax (dose-proportionally where PK are linear) and reconfirm the range before "
                "method validation."
            ),
        })
    if is_prodrug:
        flags.append({
            "key": "prodrug_detected",
            "severity": "medium",
            "message": (
                "The label describes a prodrug. The active moiety carries the bioequivalence "
                "conclusion, and ex vivo esterase conversion must be controlled at collection "
                "(chilling, and an esterase inhibitor where method development shows one is "
                "needed)."
            ),
        })
    if "active_metabolite" in detected:
        flags.append({
            "key": "active_metabolite_detected",
            "severity": "medium",
            "message": (
                "The label describes an active or major circulating metabolite. Name it, confirm "
                "a reference standard exists, and note that the bioequivalence conclusion still "
                f"rests on the {analytes['be_basis']}."
            ),
        })
    if "chiral" in detected:
        flags.append({
            "key": "racemate_detected",
            "severity": "low",
            "message": (
                "The label describes a racemate or enantiomers. The plan retains an ACHIRAL assay "
                "of the racemate; an enantiomer-specific method is required only if all four FDA "
                "conditions are met."
            ),
        })
    if "ex_vivo_instability" in detected and not is_prodrug:
        flags.append({
            "key": "ex_vivo_instability_signal",
            "severity": "medium",
            "message": (
                "The label carries an ex vivo instability signal (ester, hydrolysis, lactone, "
                "acyl glucuronide, photodegradation or similar). Whole-blood stability is "
                "included; confirm the handling precautions during method development."
            ),
        })
    flags.append({
        "key": "facility_not_named",
        "severity": "medium",
        "message": (
            "The bioanalytical facility is a placeholder. Its name, address and quality-system "
            "status must be inserted before the protocol is finalised."
        ),
    })

    summary = _summary(crit, calibration, analytes, handling, class_basis)

    return {
        "assay_class": crit.key,
        "assay_class_label": crit.label,
        "assay_class_basis": class_basis,
        "criteria": crit.to_dict(),
        "study_type": (study_type or "").strip() or "BE",
        "drug_inn": (drug_inn or "").strip(),
        "planned_dose_mg": dose_mg,
        "calibration": calibration,
        "dilution": dilution_block,
        "analytes": analytes,
        "parameters": parameters,
        "stability": stability,
        "isr": _isr_plan(crit),
        "sample_handling": handling,
        "lab_qualification": _lab_qualification(crit),
        "immunogenicity": _immunogenicity(study_type, crit),
        "assumptions": _assumptions(calibration),
        "flags": flags,
        "summary": summary,
    }


def _summary(
    crit: AssayCriteria,
    calibration: dict[str, Any],
    analytes: dict[str, Any],
    handling: dict[str, Any],
    class_basis: str,
) -> str:
    """One-paragraph plain statement for the protocol narrative."""
    lead = (
        f"Study samples are analysed by a {crit.label.lower()} at a qualified bioanalytical "
        f"facility, in {handling['matrix'].lower()} collected into "
        f"{handling['collection_tube']}. "
    )
    if calibration.get("derived"):
        rng = (
            f"The method is validated over {calibration['lloq']:g} to {calibration['uloq']:g} "
            f"ng/mL, an upper limit {calibration['uloq_margin_x']:.1f}-fold above the anticipated "
            f"Cmax and a lower limit at {calibration['lloq_pct_of_cmax']:.1f}% of it "
            f"({calibration['half_lives_quantifiable']:.1f} half-lives of quantifiable decline). "
        )
    else:
        rng = (
            "The calibration range is not yet established: no anticipated Cmax could be sourced, "
            "so it must be set during method development against the stated margin rules rather "
            "than assumed here. "
        )
    crit_txt = (
        f"Accuracy and precision are accepted at +/-{crit.re_pct:.0f}% relative error and "
        f"<= {crit.cv_pct:.0f}% CV ({crit.re_lloq_pct:.0f}% and {crit.cv_lloq_pct:.0f}% at the "
        f"LLOQ), and incurred sample reanalysis must agree to within "
        f"{crit.isr_window_pct:.0f}% for at least {ISR_MIN_PASS_PCT:.0f}% of repeats. "
    )
    return lead + rng + crit_txt + analytes["statement"] + f" Assay class basis: {class_basis}."


def bioanalytical_directive(plan: dict[str, Any]) -> str:
    """Render the locked bioanalytical facts as prompt text for the generator.

    Mirrors ``stratification.stratification_directive`` and
    ``design_decisions.design_directive``: these are settled facts to narrate,
    never a menu to re-decide. Concentrations, temperatures, durations and
    acceptance criteria are the ones a regulator would check, so the model is
    told explicitly not to round or restate them.
    """
    if not plan:
        return ""
    cal = plan.get("calibration") or {}
    crit = plan.get("criteria") or {}
    lines = [
        "BIOANALYTICAL METHOD - ALREADY DETERMINED. The assay, its calibration "
        "range, acceptance criteria, stability battery, sample handling and "
        "incurred-sample-reanalysis plan below are settled facts computed from "
        "this drug's own pharmacokinetics and label. Narrate them EXACTLY; do "
        "NOT invent, round, rescale or recalculate any concentration, "
        "temperature, duration, percentage or acceptance criterion, and do not "
        "add a number that does not appear here.",
        f"Assay: {plan.get('assay_class_label', '')} (basis: {plan.get('assay_class_basis', '')}).",
    ]

    handling = plan.get("sample_handling") or {}
    if handling:
        lines.append(
            f"Matrix: {handling.get('matrix', '')} collected into "
            f"{handling.get('collection_tube', '')}; stored at "
            f"{handling.get('storage_temp_c', '')} C in "
            f"{handling.get('aliquots_per_sample', '')} aliquots. "
            f"{handling.get('matrix_basis', '')}"
        )

    if cal.get("derived"):
        lines.append(
            f"Calibration range: LLOQ {cal['lloq']:g} ng/mL to ULOQ {cal['uloq']:g} ng/mL, with a "
            f"minimum of {cal.get('min_calibration_standards')} non-zero standards plus a blank "
            f"and a zero. {cal.get('statement', '')}"
        )
        qc = "; ".join(
            f"{q['label']} {q['concentration']:g} ng/mL ({q['basis']})"
            for q in cal.get("qc_levels", [])
        )
        if qc:
            lines.append(f"Quality controls: {qc}.")
    else:
        lines.append(
            "Calibration range: NOT DETERMINED. " + cal.get("statement", "")
            + " State this as to be established; do NOT state a numeric LLOQ or ULOQ."
        )

    analytes = plan.get("analytes") or {}
    if analytes:
        names = "; ".join(a["analyte"] for a in analytes.get("primary", []))
        extra = "; ".join(a["analyte"] for a in analytes.get("additional", []))
        lines.append(
            f"Analytes: primary - {names}."
            + (f" Additional - {extra}." if extra else "")
            + f" Bioequivalence/similarity is concluded on the {analytes.get('be_basis', '')}."
        )
        chiral = analytes.get("chiral_assessment") or {}
        if chiral.get("racemate_detected"):
            lines.append(
                "Chirality: the label describes a racemate. The assay remains ACHIRAL "
                "(racemate/total drug) unless all four FDA conditions are met - do NOT state "
                "that an enantiomer-specific assay is required."
            )

    if crit:
        lines.append(
            f"Acceptance criteria: accuracy +/-{crit.get('re_pct', 0):.0f}% and precision "
            f"<= {crit.get('cv_pct', 0):.0f}% CV at low/medium/high QC; "
            f"+/-{crit.get('re_lloq_pct', 0):.0f}% and <= {crit.get('cv_lloq_pct', 0):.0f}% at the "
            "LLOQ. Carryover in a blank after the ULOQ standard <= 20% of the LLOQ response and "
            "<= 5% of the internal-standard response."
        )

    dil = plan.get("dilution") or {}
    if dil:
        lines.append(
            f"Dilution: a {dil.get('factor')}-fold dilution is validated. "
            f"{dil.get('reportable_ceiling', '')}"
        )

    stability = [s for s in plan.get("stability", []) if s.get("applicable")]
    if stability:
        lines.append("Stability battery (all required):")
        for s in stability:
            line = f"  - {s['label']}: {s['condition']} Acceptance: {s['acceptance']}"
            if s.get("note"):
                line += f" Note: {s['note']}"
            lines.append(line)

    isr = plan.get("isr") or {}
    if isr:
        lines.append(
            f"Incurred sample reanalysis: {isr.get('rule', '')} {isr.get('acceptance', '')} "
            f"{isr.get('selection', '')} {isr.get('conduct', '')}"
        )

    imm = plan.get("immunogenicity")
    if imm:
        tiers = " -> ".join(t["tier"] for t in imm.get("tiers", []))
        lines.append(
            f"Immunogenicity assays (tiered): {tiers}. "
            + " ".join(imm.get("validation_elements", []))
        )

    lab = plan.get("lab_qualification") or {}
    if lab:
        lines.append(
            f"Bioanalytical facility: {lab.get('facility', '')} - state it as a placeholder to be "
            "completed; do not invent a laboratory name. Cross-validation applies when "
            + (lab.get("cross_validation", {}) or {}).get("required_when", "")
        )

    assumptions = plan.get("assumptions") or []
    if assumptions:
        lines.append(
            "Assumptions to disclose in the text: "
            + "; ".join(
                f"{a['applies_to']} - {a['why']}" for a in assumptions
            )
        )
    return "\n".join(lines)
