"""Randomization allocation-list generator - the actual per-subject assignments.

WHY THIS MODULE EXISTS
----------------------
:mod:`services.stratification` decides WHICH covariates deserve balancing and
why. Nothing until now produced the thing a study actually runs on: the
randomization list itself - subject number, stratum, block, and the treatment
sequence each subject will receive. That list is a regulatory record. A
regulator may ask, years after database lock, to see it regenerated from its
seed and shown to match the list the pharmacy dispensed against.

So it belongs in code, not in a model. Like :mod:`services.biostatistics`
(sample size) and :mod:`services.stratification` (covariates), this module
produces FACTS the AI narrates: the design's sequence set, the block size, the
strata, the balance guarantee and the blinding posture are all computed here and
injected into the generation prompt as locked context.

THE THREE THINGS THAT MAKE THIS CORRECT
---------------------------------------
1. **Reproducibility.** Every draw comes from a dedicated ``random.Random``
   instance seeded with a caller-supplied integer that is recorded in the
   output. The module-level ``random`` functions are never used: they share
   global state that any other import can perturb, so a list built from them
   could not be regenerated. Per-stratum streams derive their seeds from the
   master seed with SHA-256 rather than ``hash()``, because ``hash()`` of a
   string is salted by PYTHONHASHSEED and differs between processes - which
   would silently destroy reproducibility across a redeploy.

2. **Permuted blocks, sized as a multiple of the number of sequences.** A block
   whose size is not a whole multiple of the sequence count can never contain
   each sequence an equal number of times, so "balanced blocks" quietly stops
   being true - the classic implementation error. It is rejected here. Balance
   is therefore exact at the end of every completed block, not merely at the end
   of enrolment, which is what protects a study that stops early or loses a
   period.

3. **Allocation concealment is separate from treatment masking.** A single-dose
   BE study with PK endpoints is conventionally dosed open-label, because the
   endpoint is an objective bioanalytical measurement. That does NOT license
   publishing the list: anyone who can see upcoming assignments can bias who
   gets enrolled next (ICH E9 sect. 2.3.1-2.3.2). The output therefore carries
   two explicitly distinct views - a RESTRICTED unblinded list for the
   randomization statistician and pharmacy, and a blinded, subject-ID-only
   enrolment log that is safe to place in the protocol body.

   Masking itself is not binary either. Four postures are supported (see
   ``_MASKING_POSTURES``): open-label dosing with blinded bioanalysis, an
   assessor-blind posture where the pharmacology stops the subject being masked,
   a subject- and assessor-blind posture operated through an UNBLINDED
   pharmacist for an injectable whose presentations cannot be matched, and a
   full double-blind. Concealment is mandatory in all four.

DESIGN VOCABULARY
-----------------
``design_key`` is the same three-valued vocabulary the sample-size engine emits
(``crossover`` / ``parallel`` / ``replicate``); the two replicate variants are
disambiguated exactly as :func:`services.biostatistics.recommend_sample_size`
labels them - fully replicated 4-period for NTI drugs, partial replicate
3-period for reference scaling otherwise.

REGULATORY FRAMING
------------------
ICH E9 sect. 2.3 (randomization, blinding, allocation concealment), ICH E6(R2)
sect. 5.5.3 / 5.13 (randomization records and code-break procedures), ICH M13A
and FDA "Bioequivalence Studies with Pharmacokinetic Endpoints" (2013) for the
sequence sets, 21 CFR 320.
"""
from __future__ import annotations

import hashlib
import math
import random
import re
from dataclasses import asdict, dataclass, replace
from typing import Any, Iterable, Optional, Sequence

# ---------------------------------------------------------------------------
# Engine identity and named assumptions
# ---------------------------------------------------------------------------
# Bump ENGINE_VERSION whenever the generation ALGORITHM changes (block
# construction, seed derivation, ordering). A given seed produces a given list
# only under a given algorithm, so the version is recorded next to the seed and
# an auditor needs both to regenerate a historical list. The version is
# deliberately NOT mixed into the seed derivation: a cosmetic bump must not
# invalidate lists that are still reproducible.
ENGINE_VERSION = "1.0.0"

# Each sequence appears this many times per block. Two is the conventional
# choice: block size = 2 x number of sequences (4 for a 2-sequence crossover,
# 6 for a 3-sequence partial replicate). Smaller (one copy each) makes every
# block fully predictable once its first entries are known; larger lets the
# running imbalance grow between blocks. ASSUMPTION - surfaced in the output.
SEQUENCE_REPEATS_PER_BLOCK = 2

# Replacement/spare allocations generated beyond the planned enrolment, as a
# fraction of it. BE protocols conventionally allow replacement of subjects who
# withdraw after dosing, and the sample-size engine already inflates enrolment
# by a 20% dropout allowance; the spare tail exists for the residual case where
# a withdrawal must be substituted 1:1 rather than absorbed. 10% is an
# ASSUMPTION - surfaced in the output and overridable via ``n_spare``.
SPARE_FRACTION = 0.10

# Stratified lists are built per stratum before anyone is enrolled, and the
# split of subjects across strata is not known in advance. Each stratum list is
# therefore generated with this much more capacity than an equal split would
# predict, so a stratum that over-recruits cannot exhaust its list mid-study.
# Unused numbers are simply voided at database lock. ASSUMPTION.
STRATUM_HEADROOM_FACTOR = 1.5

# Over-stratification guard. A stratum that cannot fill one complete block
# ends the study with a partial block, and the imbalance that leaves can exceed
# the imbalance stratification was meant to prevent (ICH E9 sect. 2.3.2 warns
# against balancing on factors that cannot actually be balanced). Factors are
# dropped, lowest priority first, until every stratum expects at least this
# many complete blocks.
MIN_BLOCKS_PER_STRATUM = 1


# ---------------------------------------------------------------------------
# Design catalogue - which sequence set belongs to which design
# ---------------------------------------------------------------------------
# `variant` disambiguates the two replicate designs behind the single
# `design_key` value "replicate" that services/biostatistics.py emits.


@dataclass(frozen=True)
class DesignSequences:
    key: str                    # canonical design_key: crossover|parallel|replicate
    variant: str                # 2x2x2 | 2x3x3 | 2x2x4 | 2x2x3 | parallel
    label: str                  # human-readable, matches the sample-size engine
    sequences: tuple[str, ...]  # e.g. ("TR", "RT")
    n_periods: int
    treatments: tuple[str, ...]
    assignment_term: str        # "treatment sequence" vs "treatment arm"
    rationale: str              # why THIS sequence set, in PK terms
    regulatory_basis: str

    @property
    def n_sequences(self) -> int:
        return len(self.sequences)

    @property
    def allocation_ratio(self) -> str:
        return ":".join(["1"] * self.n_sequences)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # Tuples do not survive a json.dumps -> json.loads round trip as tuples;
        # the result is persisted to a JSONB column, so emit lists.
        d["sequences"] = list(self.sequences)
        d["treatments"] = list(self.treatments)
        d["n_sequences"] = self.n_sequences
        d["allocation_ratio"] = self.allocation_ratio
        return d


_DESIGNS: tuple[DesignSequences, ...] = (
    DesignSequences(
        key="crossover",
        variant="2x2x2",
        label="Single-dose, 2-period, 2-sequence, 2-treatment crossover",
        sequences=("TR", "RT"),
        n_periods=2,
        treatments=("T", "R"),
        assignment_term="treatment sequence",
        rationale=(
            "Two sequences are the minimum that lets every subject receive both "
            "products while keeping period and sequence effects estimable and "
            "orthogonal to the treatment contrast. Balancing TR against RT is what "
            "makes a period effect (a systematic drift between Period 1 and Period "
            "2, e.g. from assay batch or seasonal diet) cancel out of the "
            "Test/Reference comparison instead of biasing it."
        ),
        regulatory_basis=(
            "FDA Bioequivalence Studies with Pharmacokinetic Endpoints (2013); "
            "ICH M13A; 21 CFR 320.26"
        ),
    ),
    DesignSequences(
        key="replicate",
        variant="2x3x3",
        label="Single-dose, partial-replicate (3-period, TRR/RTR/RRT)",
        sequences=("TRR", "RTR", "RRT"),
        n_periods=3,
        treatments=("T", "R"),
        assignment_term="treatment sequence",
        rationale=(
            "The reference is administered twice to every subject, which is the only "
            "way to estimate the within-subject standard deviation of the REFERENCE "
            "(s_WR). Reference-scaled average bioequivalence widens the acceptance "
            "limits in proportion to s_WR, so without a replicated reference the "
            "scaled method simply cannot be applied. The test product is given once, "
            "which is why this design supports RSABE but not the test-versus-"
            "reference variability comparison the NTI method requires."
        ),
        regulatory_basis=(
            "FDA Bioequivalence Studies with Pharmacokinetic Endpoints (2013), "
            "reference-scaled ABE for highly variable drugs"
        ),
    ),
    DesignSequences(
        key="replicate",
        variant="2x2x4",
        label="Single-dose, fully replicated (4-period, RTRT/TRTR)",
        sequences=("TRTR", "RTRT"),
        n_periods=4,
        treatments=("T", "R"),
        assignment_term="treatment sequence",
        rationale=(
            "Both products are administered twice to every subject, so the "
            "within-subject standard deviations of BOTH the test (s_WT) and the "
            "reference (s_WR) are estimable. The FDA method for narrow-therapeutic-"
            "index drugs requires exactly that: alongside the scaled bioequivalence "
            "test it compares s_WT with s_WR, because an NTI generic that is on "
            "average equivalent but more variable is not interchangeable in practice."
        ),
        regulatory_basis=(
            "FDA Bioequivalence Studies with Pharmacokinetic Endpoints (2013), "
            "narrow-therapeutic-index recommendations (fully replicated 2x2x4)"
        ),
    ),
    DesignSequences(
        key="replicate",
        variant="2x2x3",
        label="Single-dose, fully replicated (3-period, TRT/RTR)",
        sequences=("TRT", "RTR"),
        n_periods=3,
        treatments=("T", "R"),
        assignment_term="treatment sequence",
        rationale=(
            "A three-period alternative that still replicates both products across "
            "the design (the test twice in sequence TRT, the reference twice in "
            "sequence RTR), so s_WT and s_WR remain estimable. It trades some "
            "precision on the variance components for one fewer period, which "
            "matters when the washout is long or the confinement burden is high."
        ),
        regulatory_basis=(
            "FDA Bioequivalence Studies with Pharmacokinetic Endpoints (2013), "
            "replicate designs"
        ),
    ),
    DesignSequences(
        key="parallel",
        variant="parallel",
        label="Single-dose, 2-arm parallel-group",
        sequences=("T", "R"),
        n_periods=1,
        treatments=("T", "R"),
        assignment_term="treatment arm",
        rationale=(
            "Each subject receives one product only, so there is no sequence and no "
            "washout. The treatment contrast is between subjects, which is precisely "
            "why balanced allocation and covariate balance matter more here than in "
            "a crossover: nothing in the design cancels a between-arm difference in "
            "the subjects themselves."
        ),
        regulatory_basis=(
            "FDA Bioequivalence Studies with Pharmacokinetic Endpoints (2013); "
            "used when the half-life makes a crossover washout impractical"
        ),
    ),
)

_VALID_DESIGN_KEYS = ("crossover", "parallel", "replicate")


def sequences_for_design(
    design_key: str,
    *,
    n_periods: Optional[int] = None,
    is_nti: Optional[bool] = None,
    dosing: str = "single",
) -> DesignSequences:
    """Resolve the sequence set for a design, matching the sample-size engine.

    ``design_key`` is the three-valued vocabulary
    :func:`services.biostatistics.recommend_sample_size` emits. The two
    replicate variants are chosen the same way that engine labels them: fully
    replicated (4-period) for a narrow-therapeutic-index drug, partial replicate
    (3-period) otherwise, unless ``n_periods`` pins the choice explicitly.

    ``dosing`` mirrors that same engine's parameter: when ``"multiple"``, the
    returned label reads "Multiple-dose" instead of the "Single-dose" every
    variant is defined with — matching biostatistics.py's own design_label,
    so the randomisation narrative and the sample-size determination never
    describe the same study with contradictory dosing regimens.
    """
    key = (design_key or "").strip().lower()
    if key not in _VALID_DESIGN_KEYS:
        raise ValueError(
            "design_key must be one of %s (the vocabulary services/biostatistics.py "
            "emits); got %r" % (", ".join(_VALID_DESIGN_KEYS), design_key)
        )

    if key == "parallel":
        if n_periods not in (None, 1):
            raise ValueError(
                "a parallel-group design has exactly 1 period; got n_periods=%r"
                % (n_periods,)
            )
        result = _variant("parallel", "parallel")
    elif key == "crossover":
        if n_periods not in (None, 2):
            raise ValueError(
                "design_key='crossover' is the 2x2x2 design (2 periods). For 3 or 4 "
                "periods use design_key='replicate'; got n_periods=%r" % (n_periods,)
            )
        result = _variant("crossover", "2x2x2")
    else:
        # replicate
        if n_periods is None:
            result = _variant("replicate", "2x2x4" if is_nti else "2x3x3")
        elif n_periods == 4:
            result = _variant("replicate", "2x2x4")
        elif n_periods == 3:
            # A 3-period replicate is a partial replicate (TRR/RTR/RRT) unless
            # the study needs both variance components, the NTI case.
            result = _variant("replicate", "2x2x3" if is_nti else "2x3x3")
        else:
            raise ValueError(
                "a replicate design has 3 or 4 periods; got n_periods=%r" % (n_periods,)
            )

    if str(dosing or "single").strip().lower() in ("multiple", "multi", "repeat"):
        result = replace(result, label=result.label.replace("Single-dose", "Multiple-dose", 1))
    return result


def _variant(key: str, variant: str) -> DesignSequences:
    for d in _DESIGNS:
        if d.key == key and d.variant == variant:
            return d
    raise ValueError("no design catalogue entry for %s/%s" % (key, variant))


# ---------------------------------------------------------------------------
# Stratum catalogue - the LEVELS the stratification engine's factors resolve to
# ---------------------------------------------------------------------------
# services/stratification.py ranks factor KEYS; a randomization list needs
# discrete LEVELS. `ascertainment` records when the level is actually knowable,
# which is the hard operational constraint: you can only stratify on something
# measured BEFORE the randomization number is issued.


@dataclass(frozen=True)
class StratumFactor:
    key: str                  # matches services.stratification factor keys
    label: str
    levels: tuple[str, ...]
    ascertainment: str        # screening_baseline | screening_laboratory | prospective_genotyping
    basis: str                # where the cut-point comes from
    # Carried through from services.stratification: True when the study's own
    # inclusion/exclusion criteria have already narrowed this characteristic, so
    # only residual within-range variation is left to balance.
    narrowed_by_eligibility: bool = False

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["levels"] = list(self.levels)
        return d


_STRATUM_CATALOGUE: tuple[StratumFactor, ...] = (
    StratumFactor(
        key="sex",
        label="Sex",
        levels=("Male", "Female"),
        ascertainment="screening_baseline",
        basis="Recorded at screening; the covariate regulators most expect balanced",
    ),
    StratumFactor(
        key="body_size",
        label="Body mass index",
        # Split at 25.0 kg/m2 - the WHO overweight threshold - which sits inside
        # the conventional BE eligibility window of 18.5-30.0 kg/m2 and so
        # divides an eligible cohort into two usable halves.
        levels=("BMI 18.5-24.9 kg/m2", "BMI 25.0-30.0 kg/m2"),
        ascertainment="screening_baseline",
        basis="WHO BMI classification cut-point of 25.0 kg/m2",
    ),
    StratumFactor(
        key="age",
        label="Age group",
        # Split at 40 years, where the age-related GFR decline of roughly
        # 1%/year begins; below it, age is not a meaningful PK covariate in an
        # 18-55 healthy-volunteer cohort.
        levels=("18-39 years", "40 years and older"),
        ascertainment="screening_baseline",
        basis="Onset of age-related GFR decline at approximately 40 years",
    ),
    StratumFactor(
        key="ancestry",
        label="Genetic ancestry group",
        levels=("East Asian ancestry", "Non-East-Asian ancestry"),
        ascertainment="screening_baseline",
        basis=(
            "ICH E17 intrinsic ethnic factors; used as a proxy for the frequency of "
            "large-effect pharmacogenetic variants, not as a mechanism in itself"
        ),
    ),
    StratumFactor(
        key="smoking_status",
        label="Smoking status",
        levels=("Non-smoker", "Current smoker"),
        ascertainment="screening_baseline",
        basis="CYP1A2 induction by tobacco smoke; only usable if smokers are eligible",
    ),
    StratumFactor(
        key="renal_function",
        label="Renal function (eGFR)",
        # KDIGO G1/G2 boundary at 90 mL/min/1.73m2, applied inside the normal
        # range an eligible healthy volunteer already occupies.
        levels=("eGFR >= 90 mL/min/1.73m2", "eGFR 60-89 mL/min/1.73m2"),
        ascertainment="screening_laboratory",
        basis="KDIGO GFR category boundary at 90 mL/min/1.73m2",
    ),
    StratumFactor(
        key="hepatic_function",
        label="Hepatic function (transaminases)",
        levels=("ALT and AST <= ULN", "ALT or AST above ULN but within eligibility"),
        ascertainment="screening_laboratory",
        basis="Laboratory upper limit of normal recorded at screening",
    ),
    StratumFactor(
        key="cyp_polymorphism",
        label="Polymorphic CYP metabolizer phenotype",
        levels=(
            "Normal or extensive metabolizer",
            "Reduced-function (intermediate or poor) metabolizer",
        ),
        ascertainment="prospective_genotyping",
        basis="CPIC phenotype grouping from the predicted diplotype",
    ),
    StratumFactor(
        key="transporter_polymorphism",
        label="Drug-transporter genotype",
        levels=("Normal-function genotype", "Reduced-function genotype"),
        ascertainment="prospective_genotyping",
        basis="Reduced-function allele status (e.g. SLCO1B1 c.521T>C)",
    ),
    StratumFactor(
        key="nat2_acetylator",
        label="NAT2 acetylator phenotype",
        levels=("Rapid or intermediate acetylator", "Slow acetylator"),
        ascertainment="prospective_genotyping",
        basis="Bimodal NAT2 acetylation phenotype from genotype",
    ),
    StratumFactor(
        key="gastric_ph",
        label="Baseline gastric acidity",
        levels=("Normal gastric acidity", "Reduced gastric acidity"),
        ascertainment="screening_laboratory",
        basis="Baseline achlorhydria / acid-suppressant exposure recorded at screening",
    ),
)

_STRATUM_BY_KEY = {f.key: f for f in _STRATUM_CATALOGUE}

_ASCERTAINMENT_NOTE = {
    "screening_baseline": (
        "Known at screening from the demographic record; adds no operational burden."
    ),
    "screening_laboratory": (
        "Requires the screening laboratory result to be back before the "
        "randomization number is issued; build the turnaround into the screening "
        "window."
    ),
    "prospective_genotyping": (
        "Requires a prospective genotype result BEFORE randomization. Confirm the "
        "assay turnaround fits inside the screening window and that the informed "
        "consent covers pharmacogenetic testing; otherwise this factor cannot be a "
        "stratum and should be handled as a pre-specified covariate instead."
    ),
}


# ---------------------------------------------------------------------------
# Blinding signals - detected from the REAL label text
# ---------------------------------------------------------------------------
# Grounded the same way services/design_decisions.py and
# services/stratification.py ground their judgements: regex cues run against the
# DailyMed SPL sections already fetched for the protocol, rather than recall.
# `sections` restricts each cue to the sections where a hit actually means what
# we claim - "intravenous" in a Clinical Pharmacology paragraph usually refers
# to an absolute-bioavailability comparison, not to the product's presentation,
# so the parenteral cue reads only the administration/how-supplied sections.


@dataclass(frozen=True)
class BlindingSignal:
    key: str
    label: str
    sections: tuple[str, ...]
    why: str            # why it threatens masking
    mitigation: str     # what a protocol actually does about it

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["sections"] = list(self.sections)
        return d


_ALL_LABEL_SECTIONS = (
    "title",
    "indications_usage",
    "clinical_pharmacology",
    "pharmacokinetics",
    "dosage_and_administration",
    "how_supplied",
)

_BLINDING_SIGNAL_CATALOGUE: tuple[BlindingSignal, ...] = (
    BlindingSignal(
        key="distinguishable_appearance",
        label="Products are visually identifiable dosage forms",
        sections=("how_supplied", "dosage_and_administration"),
        why=(
            "The label describes a specific physical presentation (colour, shape, "
            "coating, scoring or debossing). Test and reference products come from "
            "different manufacturers and essentially never match on all of these, so "
            "a subject or an unblinded observer can identify which product was given "
            "on sight alone."
        ),
        mitigation=(
            "If masking of dosing is required, over-encapsulate both products in "
            "identical opaque capsules with a matched backfill - and support it with "
            "comparative dissolution data, because over-encapsulation can itself "
            "alter dissolution and therefore the bioequivalence result. If masking is "
            "not required, dose open-label and rely on blinded bioanalysis."
        ),
    ),
    BlindingSignal(
        key="visible_pharmacological_cue",
        label="Drug produces a visible effect that unblinds the subject",
        sections=_ALL_LABEL_SECTIONS,
        why=(
            "The label reports discolouration of urine or other body fluids. No "
            "amount of dosage-form matching conceals that: the subject and the ward "
            "staff see the effect directly, so any claim of subject-level blinding in "
            "the protocol would be untrue."
        ),
        mitigation=(
            "Do not claim subject masking. State plainly that dosing is open-label, "
            "and place the integrity of the comparison on the objective PK endpoint "
            "with a bioanalytical laboratory blinded to treatment."
        ),
    ),
    BlindingSignal(
        key="perceptible_systemic_effect",
        label="Drug produces a perceptible sensory or systemic effect",
        sections=_ALL_LABEL_SECTIONS,
        why=(
            "The label reports an effect the subject can feel or taste (sedation, "
            "flushing, hypoglycaemia, a distinctive taste). A double-dummy defeats "
            "appearance differences but not pharmacology, so masking of the subject "
            "is unreliable even with identical presentations."
        ),
        mitigation=(
            "Keep the ASSESSOR and the bioanalytical laboratory blinded even where "
            "the subject cannot be. State the residual risk explicitly rather than "
            "asserting a double-blind that the pharmacology does not support."
        ),
    ),
    BlindingSignal(
        key="parenteral_presentation",
        label="Parenteral product - presentation and administration are visible",
        sections=("dosage_and_administration", "how_supplied"),
        why=(
            "The product is injected or infused. Presentation differences (prefilled "
            "syringe versus vial versus autoinjector), fill volume and solution "
            "appearance are visible to whoever administers the dose."
        ),
        mitigation=(
            "Use an unblinded pharmacist or administrator who takes no part in "
            "assessment or bioanalysis; mask the syringe or infusion line where "
            "presentation allows; keep the subject and the assessor blinded."
        ),
    ),
)

_BLINDING_CUES: dict[str, tuple[str, ...]] = {
    "distinguishable_appearance": (
        r"\bdeboss", r"\bimprint", r"\bengrav", r"film[- ]?coated", r"\bscored\b",
        r"\bbiconvex\b", r"\bcapsules?\b", r"\btablets?\b",
        r"\bcolou?r(?:ed|less|s)?\b", r"white to off-white",
    ),
    "visible_pharmacological_cue": (
        r"chromaturia",
        r"discolou?r\w*(?:\s+\w+){0,4}\s+urine",
        r"urine(?:\s+\w+){0,4}\s+discolou?r",
        r"orange[- ]?red",
        r"discolou?r\w*(?:\s+\w+){0,3}\s+(?:body fluids|sweat|tears|saliva|feces|faeces)",
    ),
    "perceptible_systemic_effect": (
        r"bitter taste", r"metallic taste", r"unpleasant taste",
        r"\bflushing\b", r"somnolen", r"drowsiness", r"\bsedation\b",
        r"hypoglyc(?:a?emi)", r"\bdizziness\b",
    ),
    "parenteral_presentation": (
        r"subcutaneous", r"intravenous", r"intramuscular",
        r"pre-?filled syringe", r"\bautoinjector\b", r"\bvials?\b",
        r"\binfusion\b", r"\binject",
    ),
}

# ---------------------------------------------------------------------------
# Masking postures - four, not two
# ---------------------------------------------------------------------------
# The default for a single-dose BE study with PK endpoints is open-label DOSING
# with a blinded bioanalytical laboratory: the primary endpoint is an instrument
# reading, not a judgement, so subject masking buys little, while analyst
# masking is essential. That convention is correct and is preserved.
#
# It is not, however, the only case. Two things make a binary open/double-blind
# choice wrong:
#
# 1. PK primaries are not always the only endpoints that decide the study. In a
#    biosimilar or other biologic comparison, anti-drug antibody incidence is a
#    co-endpoint and the hypersensitivity, infusion and INJECTION-SITE reactions
#    read alongside it are elicited and GRADED by a clinician. Observer grading
#    of a local reaction by someone who knows which product was injected is a
#    recognised source of bias. That warrants masking the subject and the
#    assessor even where the PK primaries would not.
#
# 2. A full identical-presentation double-blind is frequently undeliverable for
#    an injectable: test and reference containers, fill volumes and solution
#    appearance differ and cannot be matched. Claiming "double-blind" there
#    would be a claim the conduct cannot honour. The practical posture - and the
#    one the `parenteral_presentation` mitigation text has always described - is
#    an UNBLINDED pharmacist or administrator who prepares and gives every dose
#    and takes no part in assessment or bioanalysis, with the subject and every
#    assessor blinded. That is a distinct posture and it is now representable.
#
# A fourth posture exists for honesty rather than ambition: where the label
# evidence shows the subject can perceive which product was given (chromaturia,
# a distinctive taste, sedation, hypoglycaemia), subject masking is NOT
# claimable, and the engine drops to assessor-blind rather than assert masking
# the pharmacology defeats.
_MASKING_OPEN = "open_label_dosing_with_blinded_bioanalysis"
_MASKING_ASSESSOR = "assessor_blind_dosing_subject_unblinded"
_MASKING_SUBJECT_ASSESSOR = "subject_and_assessor_blind_unblinded_administrator"
_MASKING_DOUBLE = "double_blind"


@dataclass(frozen=True)
class MaskingPosture:
    """One masking posture, with who it actually blinds and how it is operated.

    ``blinded_roles`` / ``unblinded_roles`` exist so that no downstream reader
    has to infer from a posture NAME who is masked. The randomization
    statistician is unblinded in every posture by construction - somebody has to
    hold the list - and that is stated rather than left implicit.
    """

    key: str
    label: str
    dosing_masked: bool
    blinded_roles: tuple[str, ...]
    unblinded_roles: tuple[str, ...]
    operational_statement: str


_MASKING_POSTURES: dict[str, MaskingPosture] = {
    _MASKING_OPEN: MaskingPosture(
        key=_MASKING_OPEN,
        label="Open-label dosing with a blinded bioanalytical laboratory",
        dosing_masked=False,
        blinded_roles=("bioanalytical_laboratory",),
        unblinded_roles=(
            "subject",
            "investigator_and_assessor",
            "dose_administrator",
            "randomization_statistician",
        ),
        operational_statement=(
            "Dosing is open-label. Bioanalytical samples carry the subject number, "
            "period and nominal time only, and the analytical laboratory is blinded "
            "to treatment assignment. The randomization list itself remains concealed "
            "from everyone who influences whether a candidate is enrolled."
        ),
    ),
    _MASKING_ASSESSOR: MaskingPosture(
        key=_MASKING_ASSESSOR,
        label=(
            "Assessor-blind dosing with an unblinded administrator; subject masking "
            "not claimed"
        ),
        dosing_masked=True,
        blinded_roles=("investigator_and_assessor", "bioanalytical_laboratory"),
        unblinded_roles=(
            "subject",
            "dose_administrator",
            "randomization_statistician",
        ),
        operational_statement=(
            "The investigational product is prepared and administered by an unblinded "
            "pharmacist or dosing administrator who takes no part in safety "
            "assessment, tolerability or injection-site grading, endpoint "
            "adjudication or bioanalysis. The investigator and every assessor who "
            "grades an adverse event, an injection-site reaction or any other "
            "subjective observation remain blinded, as do the bioanalytical and "
            "immunogenicity laboratories. Subject masking is NOT claimed and must "
            "not be described as achieved anywhere in the protocol; the residual "
            "risk of subject expectation is recorded as a limitation."
        ),
    ),
    _MASKING_SUBJECT_ASSESSOR: MaskingPosture(
        key=_MASKING_SUBJECT_ASSESSOR,
        label=(
            "Subject- and assessor-blind dosing with an unblinded pharmacist or "
            "administrator"
        ),
        dosing_masked=True,
        blinded_roles=(
            "subject",
            "investigator_and_assessor",
            "bioanalytical_laboratory",
        ),
        unblinded_roles=("dose_administrator", "randomization_statistician"),
        operational_statement=(
            "The investigational product is prepared and administered by an unblinded "
            "pharmacist or dosing administrator who takes no part in assessment, "
            "adverse-event or injection-site grading, or bioanalysis. The syringe, "
            "infusion line and injection site are screened from the subject's view at "
            "administration where the presentation allows. The subject, the "
            "investigator and every assessor who grades an adverse event or an "
            "injection-site reaction remain blinded, as do the bioanalytical and "
            "immunogenicity laboratories. An identical-presentation double-blind is "
            "NOT claimed: where test and reference containers, fill volumes or "
            "solution appearance differ they cannot be matched, and this is the level "
            "of masking such a presentation can actually deliver."
        ),
    ),
    _MASKING_DOUBLE: MaskingPosture(
        key=_MASKING_DOUBLE,
        label="Double-blind, identical presentations",
        dosing_masked=True,
        blinded_roles=(
            "subject",
            "investigator_and_assessor",
            "dose_administrator",
            "bioanalytical_laboratory",
        ),
        unblinded_roles=("randomization_statistician",),
        operational_statement=(
            "Test and reference are presented identically - matched over-encapsulation "
            "or a double-dummy - so that neither the subject, the person administering "
            "the dose, nor any assessor can identify the treatment. Where "
            "over-encapsulation is used to achieve this it must be supported by "
            "comparative dissolution data, because it can itself alter dissolution and "
            "therefore the bioequivalence result. The bioanalytical laboratory is "
            "blinded regardless."
        ),
    ),
}

# Route strings arrive from the Review Design step as short free text. The
# values services/design_decisions.py actually emits are "Oral",
# "Intravitreal", "IV infusion" and "SC <device> <site>", so all four must be
# classified correctly here. Long forms are matched as substrings; the
# two-letter abbreviations are matched as whole tokens only, because "sc" and
# "im" occur inside ordinary words.
#
# The intra-* routes are listed explicitly rather than left to the "inject" cue:
# an intravitreal or intrathecal product is administered by injection but its
# route string never contains the word, so without them a local-injection
# biologic would lose its injection-site-grading driver.
_PARENTERAL_ROUTE_WORDS = (
    "subcutaneous", "intravenous", "intramuscular", "intradermal",
    "intravitreal", "intrathecal", "intra-articular", "intraperitoneal",
    "infusion", "inject", "parenteral",
)
_PARENTERAL_ROUTE_TOKENS = frozenset({"sc", "sq", "subq", "iv", "im", "id"})


def _label_text(label: Optional[dict[str, str]], sections: Iterable[str]) -> str:
    if not label:
        return ""
    return " ".join(str(label.get(k, "") or "") for k in sections).lower()


def _ascii(text: str) -> str:
    """Strip non-ASCII so evidence excerpts are safe on a cp1252 console."""
    return text.encode("ascii", "ignore").decode("ascii")


def _detect_blinding_signals(label: Optional[dict[str, str]]) -> list[dict[str, Any]]:
    """Which masking threats the real label text gives positive evidence for."""
    found: list[dict[str, Any]] = []
    for sig in _BLINDING_SIGNAL_CATALOGUE:
        text = _label_text(label, sig.sections)
        if not text:
            continue
        for pattern in _BLINDING_CUES[sig.key]:
            m = re.search(pattern, text)
            if not m:
                continue
            start = max(0, m.start() - 40)
            excerpt = _ascii(text[start:m.end() + 40]).strip()
            entry = sig.to_dict()
            entry["detected"] = True
            entry["matched_pattern"] = pattern
            entry["evidence"] = "..." + excerpt + "..."
            found.append(entry)
            break
    return found


def _is_parenteral_route(route: Optional[str]) -> bool:
    """Whether a free-text route string describes an injection or infusion."""
    if not route:
        return False
    text = str(route).strip().lower()
    if any(word in text for word in _PARENTERAL_ROUTE_WORDS):
        return True
    tokens = set(re.split(r"[^a-z]+", text))
    return bool(tokens & _PARENTERAL_ROUTE_TOKENS)


# Drivers that argue for masking DOSING. Each carries the reason in the same
# voice as the signal catalogue: what the bias mechanism actually is, not an
# assertion that masking is "best practice".
_MASKING_DRIVERS: dict[str, str] = {
    "pd_coprimary": (
        "A validated pharmacodynamic endpoint is co-primary. Unlike a plasma "
        "concentration, a PD measurement can be influenced by the expectations of "
        "the subject and the assessor."
    ),
    "immunogenicity_coendpoint": (
        "Anti-drug antibody incidence is assessed as a co-endpoint. The ADA assay "
        "itself is objective and is blinded like any other laboratory measurement, "
        "but the hypersensitivity and infusion reactions read alongside it are "
        "elicited by questioning and attributed by a clinician, and an unblinded "
        "clinician's threshold for recording and attributing them is not "
        "independent of which product was given."
    ),
    "injection_site_reaction_grading": (
        "The product is administered by injection or infusion, and injection-site "
        "reactions are graded by an observer against a severity scale (erythema, "
        "induration, swelling, pain). Observer grading of a local reaction is a "
        "recognised source of bias when the observer knows which product was "
        "injected, and it is not rescued by blinding the laboratory."
    ),
    "subjective_safety_reporting": (
        "Adverse events and tolerability in a biologic comparison are elicited by "
        "questioning and graded by judgement rather than measured by an "
        "instrument. That judgement can be shaped by knowing which product the "
        "subject received, in a way a chromatographic peak area cannot be."
    ),
}


def assess_blinding(
    label: Optional[dict[str, str]] = None,
    *,
    pd_coprimary: Optional[bool] = None,
    is_biologic: Optional[bool] = None,
    study_type: Optional[str] = None,
    route: Optional[str] = None,
    immunogenicity_assessed: Optional[bool] = None,
) -> dict[str, Any]:
    """Decide the masking posture and separate it from allocation concealment.

    Allocation concealment and treatment masking are routinely conflated and
    they are not the same thing. Allocation concealment - keeping the NEXT
    assignment unknowable to anyone who influences who gets enrolled - is
    mandatory in every randomised study, including an open-label one, and this
    function asserts it in every posture without exception. Treatment masking is
    a design choice that depends on how subjective the endpoints are.

    How the posture is chosen
    -------------------------
    * ``pd_coprimary`` -> ``double_blind``. A PD measurement can be shaped by
      expectation, so both the subject and the assessor must be masked and the
      products must be presented identically.
    * a BIOLOGIC comparison (``is_biologic``, ``study_type='biosimilar_pk'``, or
      an explicit ``immunogenicity_assessed=True``) -> blinded DOSING. The PK
      primaries are objective, but immunogenicity is a co-endpoint, and the
      reactions read alongside it - including injection-site grading where the
      product is injected - are clinician judgements. The posture chosen is
      subject- and assessor-blind with an UNBLINDED pharmacist or administrator,
      because that is what an injectable presentation can actually deliver; a
      full identical-presentation double-blind is not claimed on its behalf.
    * that same case, where the label shows the subject can PERCEIVE which
      product was given -> ``assessor_blind_dosing_subject_unblinded``. Subject
      masking is dropped rather than claimed falsely; the assessor and the
      laboratories stay blinded, which is where the bias would otherwise enter.
    * everything else - notably a single-dose small-molecule BE study with PK
      endpoints only, including a small-molecule INJECTABLE - stays open-label
      with blinded bioanalysis. That convention is deliberate, not an oversight:
      the endpoint is an instrument reading.

    Parameters
    ----------
    label:
        DailyMed SPL sections; grounds the signal detection.
    pd_coprimary:
        A validated PD endpoint is co-primary.
    is_biologic:
        The comparison is of a biologic / biosimilar product.
    study_type:
        ``'BE' | 'biosimilar_pk' | 'food_effect'``; ``'biosimilar_pk'`` implies
        a biologic even if ``is_biologic`` was not passed.
    route:
        Free-text route from the design decisions ("subcutaneous", "SC",
        "oral"). Used to establish that dosing is by injection.
    immunogenicity_assessed:
        Explicit override. ``None`` derives it (true for a biologic). Setting it
        ``True`` for a non-biologic also warrants blinded dosing; setting it
        ``False`` for a biologic removes that driver but leaves the others.
    """
    signals = _detect_blinding_signals(label)
    keys = {s["key"] for s in signals}

    biologic = bool(is_biologic) or str(study_type or "").strip().lower() == (
        "biosimilar_pk")
    immunogenicity = (
        bool(immunogenicity_assessed) if immunogenicity_assessed is not None
        else biologic
    )
    parenteral = _is_parenteral_route(route) or "parenteral_presentation" in keys
    # The subject cannot be masked against pharmacology they can feel or see.
    # This is a DELIVERABILITY fact, not a preference, and it can only ever
    # weaken a claim - never strengthen one.
    subject_masking_defeated = bool(
        keys & {"visible_pharmacological_cue", "perceptible_systemic_effect"}
    )

    drivers: list[dict[str, str]] = []

    def _add(key: str) -> None:
        drivers.append({"key": key, "statement": _MASKING_DRIVERS[key]})

    if pd_coprimary:
        _add("pd_coprimary")
    if immunogenicity:
        _add("immunogenicity_coendpoint")
    if parenteral and (biologic or immunogenicity):
        _add("injection_site_reaction_grading")
    if biologic:
        _add("subjective_safety_reporting")

    # A reason to mask DOSING that the PK primaries alone would not supply.
    # A biologic qualifies on its own: even with immunogenicity sampling
    # explicitly switched off, adverse-event elicitation and grading remain
    # clinician judgements. A small-molecule injectable does NOT qualify - the
    # route alone is not a bias mechanism when the only endpoints are PK.
    dosing_masking_warranted = bool(biologic or immunogenicity)

    if pd_coprimary:
        masking = _MASKING_DOUBLE
        rationale = (
            "A validated pharmacodynamic endpoint is co-primary. Unlike a plasma "
            "concentration, a PD measurement can be influenced by the expectations of "
            "the subject and the assessor, so subject and assessor masking is "
            "required and the products must be presented identically."
        )
        if biologic:
            rationale += (
                " Immunogenicity and the clinician-graded reactions read alongside it "
                "reinforce that requirement rather than replace it."
            )
        if subject_masking_defeated:
            rationale += (
                " NOTE: the label evidence below shows the subject may nonetheless "
                "perceive which product was given. Masking is still required, but the "
                "protocol must record that residual limitation rather than assert a "
                "masking the pharmacology defeats."
            )
        if parenteral:
            rationale += (
                " Where the presentations cannot be matched, the double-blind is "
                "operated through an unblinded pharmacist or administrator who takes "
                "no part in assessment."
            )
    elif dosing_masking_warranted:
        reasons = " ".join(d["statement"] for d in drivers)
        if subject_masking_defeated:
            masking = _MASKING_ASSESSOR
            rationale = (
                "The pharmacokinetic primaries are instrument readings and would not "
                "on their own require masked dosing, but they are not the only "
                "endpoints that decide this study. " + reasons + " Masked dosing is "
                "therefore warranted. Subject masking, however, is not deliverable "
                "here: the label evidence below shows the subject can perceive which "
                "product was given, so claiming it would be untrue. The investigator "
                "and every assessor, and the bioanalytical and immunogenicity "
                "laboratories, are blinded - which is where the bias that matters "
                "would otherwise enter - and the residual risk of subject expectation "
                "is recorded rather than concealed."
            )
        else:
            masking = _MASKING_SUBJECT_ASSESSOR
            rationale = (
                "The pharmacokinetic primaries are instrument readings and would not "
                "on their own require masked dosing, but they are not the only "
                "endpoints that decide this study. " + reasons + " Those assessments "
                "are judgements rather than instrument readings, so the subject and "
                "every assessor are blinded even though the PK primaries do not "
                "demand it. A full identical-presentation double-blind is NOT claimed: "
                "test and reference containers, fill volumes and solution appearance "
                "generally differ between originator and proposed biosimilar and "
                "cannot be matched. The masking is delivered instead by an unblinded "
                "pharmacist or administrator who prepares and gives every dose and "
                "takes no part in assessment or bioanalysis, which is the level this "
                "presentation can honestly support."
            )
    else:
        masking = _MASKING_OPEN
        rationale = (
            "The primary endpoints are pharmacokinetic exposure measures derived from "
            "an instrumental assay, which cannot be influenced by knowledge of "
            "treatment. Single-dose bioequivalence studies with PK endpoints are "
            "conventionally dosed open-label on that basis; the blinding that "
            "genuinely protects the result is blinding of the bioanalytical "
            "laboratory, which is mandatory here regardless."
        )
        if keys & {"visible_pharmacological_cue", "perceptible_systemic_effect"}:
            rationale += (
                " The label evidence below also shows that subject masking could not "
                "be achieved even if it were attempted, so claiming a double-blind "
                "would be unsupportable."
            )

    posture = _MASKING_POSTURES[masking]

    return {
        "allocation_concealment": {
            "required": True,
            "applies_even_when_open_label": True,
            "statement": (
                "The randomization list is held by the unblinded randomization "
                "statistician and the dispensing pharmacy only. No investigator, "
                "recruiter, or site staff member who influences whether a candidate is "
                "enrolled has sight of any allocation before that subject has been "
                "irrevocably entered into the study."
            ),
            "basis": (
                "ICH E9 sect. 2.3.1-2.3.2; concealment prevents selection bias in "
                "enrolment and is independent of whether dosing is masked"
            ),
        },
        "treatment_masking": {
            "recommended": masking,
            "label": posture.label,
            "rationale": rationale,
            # Explicit, so no downstream reader has to infer masking from a
            # posture NAME. "Dosing is blinded" and "the subject is blinded" are
            # different claims and only one of them holds in some postures.
            "dosing_masked": posture.dosing_masked,
            "subject_masked": "subject" in posture.blinded_roles,
            "assessor_masked": "investigator_and_assessor" in posture.blinded_roles,
            "who_is_blinded": list(posture.blinded_roles),
            "who_is_unblinded": list(posture.unblinded_roles),
            "operational_statement": posture.operational_statement,
            "drivers": drivers,
            "inputs_considered": {
                "pd_coprimary": bool(pd_coprimary),
                "biologic": biologic,
                "immunogenicity_assessed": immunogenicity,
                "parenteral_administration": parenteral,
                "study_type": (str(study_type).strip() if study_type else None),
                "route": (str(route).strip() if route else None),
                "subject_masking_deliverable": not subject_masking_defeated,
            },
            "signals": signals,
            "signals_detected": sorted(keys),
        },
        "bioanalytical_blinding": {
            "required": True,
            "statement": (
                "Bioanalytical samples are labelled with the subject number, period "
                "and nominal time only. The analytical laboratory is blinded to "
                "treatment assignment, and the analytical run order does not follow "
                "the treatment sequence."
                + (
                    " The immunogenicity (anti-drug antibody) laboratory is blinded on "
                    "the same basis, and confirmatory and titre testing is performed "
                    "without knowledge of treatment."
                    if immunogenicity else ""
                )
            ),
        },
        "unblinding_procedure": {
            "statement": (
                "The code is broken only after database lock, or for an individual "
                "subject where knowledge of the treatment is necessary for clinical "
                "management of an emergency. Any emergency code break is documented "
                "with the date, time, reason and the person who authorised it."
            ),
            "basis": "ICH E6(R2) sect. 5.13.4",
        },
    }


# ---------------------------------------------------------------------------
# Stratum resolution
# ---------------------------------------------------------------------------


def _normalise_stratify_by(
    stratify_by: Any,
) -> list[StratumFactor]:
    """Accept the several shapes a caller reasonably has to hand.

    * ``None`` / empty                        -> no stratification
    * the whole ``recommend_stratification`` result (``{"factors": [...]}``)
    * a list of catalogue keys (``["sex", "body_size"]``)
    * a list of dicts carrying ``key`` and optionally ``label`` / ``levels``
    """
    if not stratify_by:
        return []

    if isinstance(stratify_by, dict):
        if "factors" in stratify_by:
            items: Sequence[Any] = stratify_by.get("factors") or []
        else:
            raise ValueError(
                "stratify_by given as a dict must be the recommend_stratification() "
                "result (it needs a 'factors' key)"
            )
    else:
        items = list(stratify_by)

    out: list[StratumFactor] = []
    seen: set[str] = set()
    for item in items:
        if isinstance(item, StratumFactor):
            factor = item
        elif isinstance(item, str):
            key = item.strip()
            if key not in _STRATUM_BY_KEY:
                raise ValueError(
                    "unknown stratification factor %r; known keys are %s (or pass a "
                    "dict with explicit 'levels')"
                    % (key, ", ".join(sorted(_STRATUM_BY_KEY)))
                )
            factor = _STRATUM_BY_KEY[key]
        elif isinstance(item, dict):
            key = str(item.get("key", "")).strip()
            base = _STRATUM_BY_KEY.get(key)
            levels = item.get("levels")
            if levels:
                factor = StratumFactor(
                    key=key or "custom",
                    label=str(item.get("label") or (base.label if base else key)),
                    levels=tuple(str(x) for x in levels),
                    ascertainment=str(
                        item.get("ascertainment")
                        or (base.ascertainment if base else "screening_baseline")
                    ),
                    basis=str(
                        item.get("basis")
                        or (base.basis if base else "Supplied by the caller")
                    ),
                    narrowed_by_eligibility=bool(
                        item.get("narrowed_by_eligibility", False)
                    ),
                )
            elif base is not None:
                # recommend_stratification() already worked out whether the entry
                # criteria have flattened this characteristic; carry that through
                # instead of recomputing or discarding it.
                factor = replace(
                    base,
                    narrowed_by_eligibility=bool(
                        item.get("narrowed_by_eligibility", False)
                    ),
                )
            else:
                raise ValueError(
                    "stratification factor %r has no catalogue entry and no explicit "
                    "'levels'" % (key or item,)
                )
        else:
            raise TypeError(
                "stratify_by entries must be a key, a dict, or a StratumFactor; got %r"
                % (type(item).__name__,)
            )

        if factor.key in seen:
            continue
        seen.add(factor.key)
        out.append(factor)
    return out


def _select_strata(
    factors: list[StratumFactor],
    n_enrolled: int,
    block_size: int,
) -> tuple[list[StratumFactor], list[dict[str, Any]]]:
    """Keep only as many factors as the sample size can actually balance.

    The guard is arithmetic, not stylistic: with K strata and a block of B, the
    study needs K x B subjects before every stratum has completed even one
    block. Beyond that point each extra factor multiplies the number of partial
    blocks at the end of enrolment, and partial blocks are exactly the imbalance
    stratification exists to prevent.

    Because only a few factors survive that arithmetic, the ORDER they are
    offered in decides the study. The caller's ranking (services.stratification
    orders by PK impact) is respected, with one correction: a characteristic the
    protocol's own inclusion/exclusion criteria have already narrowed has little
    residual variation left to balance, so spending a scarce stratification slot
    on it in preference to an unconstrained covariate would be a poor trade.
    Those factors are therefore considered last.
    """
    ordered = sorted(factors, key=lambda f: 1 if f.narrowed_by_eligibility else 0)

    kept: list[StratumFactor] = []
    dropped: list[dict[str, Any]] = []
    n_strata = 1
    for f in ordered:
        if len(f.levels) < 2:
            dropped.append({
                "key": f.key,
                "label": f.label,
                "reason": "only one level, so it does not partition the cohort",
                "deprioritised_as_narrowed_by_eligibility": f.narrowed_by_eligibility,
            })
            continue
        trial = n_strata * len(f.levels)
        if trial * block_size * MIN_BLOCKS_PER_STRATUM > n_enrolled:
            # The reason must not quote the block size: it is repeated verbatim
            # into the AI directive, and the block length is withheld from the
            # protocol body (ICH E9 sect. 2.3.2).
            reason = (
                "adding it would create %d strata, and %d subjects cannot supply "
                "even one complete randomization block to each of them, so "
                "stratifying on it would create more end-of-study imbalance than it "
                "prevents (ICH E9 sect. 2.3.2). Handle it as a pre-specified "
                "covariate in the analysis instead." % (trial, n_enrolled)
            )
            if f.narrowed_by_eligibility:
                reason += (
                    " It was also considered last because the eligibility criteria "
                    "already narrow this characteristic, leaving little residual "
                    "variation to balance."
                )
            dropped.append({
                "key": f.key,
                "label": f.label,
                "reason": reason,
                "deprioritised_as_narrowed_by_eligibility": f.narrowed_by_eligibility,
            })
            continue
        kept.append(f)
        n_strata = trial
    return kept, dropped


def _stratum_combinations(
    factors: list[StratumFactor],
) -> list[dict[str, str]]:
    """Cartesian product of the kept factors' levels, in catalogue order."""
    combos: list[dict[str, str]] = [{}]
    for f in factors:
        combos = [dict(c, **{f.key: lvl}) for c in combos for lvl in f.levels]
    return combos


# ---------------------------------------------------------------------------
# Block construction
# ---------------------------------------------------------------------------


def _derive_seed(master_seed: int, stream_id: str) -> int:
    """Deterministically derive a per-stream seed from the master seed.

    SHA-256 rather than ``hash()``: ``hash()`` of a str is salted by
    PYTHONHASHSEED, so it differs between interpreter processes. A list built on
    it would be irreproducible after a restart - the exact failure this module
    exists to prevent.
    """
    digest = hashlib.sha256(
        ("%d|%s" % (master_seed, stream_id)).encode("utf-8")
    ).hexdigest()
    return int(digest[:16], 16)


def _round_up_to_multiple(value: int, multiple: int) -> int:
    if value <= 0:
        return 0
    return int(math.ceil(value / float(multiple))) * multiple


def _build_blocks(
    rng: random.Random,
    sequences: tuple[str, ...],
    n_slots: int,
    block_size: int,
) -> list[tuple[int, int, str]]:
    """Permuted blocks covering ``n_slots`` slots.

    Returns (block_index, position_in_block, sequence) triples. Each block holds
    every sequence exactly ``block_size / n_sequences`` times and is shuffled
    independently, so balance is exact at the end of every completed block.
    """
    n_seq = len(sequences)
    repeats = block_size // n_seq
    n_blocks = _round_up_to_multiple(n_slots, block_size) // block_size
    out: list[tuple[int, int, str]] = []
    for b in range(1, n_blocks + 1):
        content = list(sequences) * repeats
        rng.shuffle(content)
        for pos, seq in enumerate(content, start=1):
            out.append((b, pos, seq))
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_allocation(
    design_key: str,
    n_enrolled: int,
    seed: int,
    stratify_by: Any = None,
    n_periods: Optional[int] = None,
    *,
    is_nti: Optional[bool] = None,
    block_size: Optional[int] = None,
    n_spare: Optional[int] = None,
    subject_id_prefix: str = "S",
    pd_coprimary: Optional[bool] = None,
    label: Optional[dict[str, str]] = None,
    is_biologic: Optional[bool] = None,
    study_type: Optional[str] = None,
    route: Optional[str] = None,
    immunogenicity_assessed: Optional[bool] = None,
    dosing: str = "single",
) -> dict[str, Any]:
    """Generate the study's randomization list.

    Parameters
    ----------
    design_key:
        ``'crossover' | 'parallel' | 'replicate'`` - the vocabulary
        :func:`services.biostatistics.recommend_sample_size` emits.
    n_enrolled:
        Planned enrolment (the dropout-inflated figure, i.e. ``n_enrolled`` from
        the sample-size recommendation, not ``n_evaluable``).
    seed:
        Integer seed. Recorded in the output; the same seed with the same
        arguments and the same ``engine_version`` regenerates a byte-identical
        list years later.
    stratify_by:
        Factors to balance within. Accepts the whole
        :func:`services.stratification.recommend_stratification` result, a list
        of its factor keys, or dicts carrying explicit ``levels``. Factors the
        sample size cannot actually balance are dropped, with the reason
        recorded.
    n_periods:
        Pins the replicate variant (3 = partial replicate / 3-period full
        replicate, 4 = fully replicated 2x2x4). Defaults to the same choice the
        sample-size engine makes.
    is_nti / pd_coprimary / label:
        NTI status selects the fully replicated variant; a PD co-primary forces
        a double-blind; the DailyMed label text grounds the masking assessment.
    is_biologic / study_type / route / immunogenicity_assessed:
        Passed straight to :func:`assess_blinding`, which uses them to decide
        whether DOSING is masked and at what level. All default to ``None``, and
        with all four omitted the masking decision is exactly what it was before
        they existed - a small-molecule PK-only BE study stays open-label. A
        biologic / biosimilar comparison instead gets subject- and assessor-blind
        dosing with an unblinded administrator, because immunogenicity is a
        co-endpoint and injection-site and adverse-event grading are clinician
        judgements. See :func:`assess_blinding` for the full decision table.

    Returns
    -------
    dict, JSON-serialisable for storage in ``population_config`` (JSONB), with
    ``unblinded_allocation`` (RESTRICTED) and ``blinded_enrolment_log``
    (protocol-body safe) held explicitly apart.
    """
    # --- validate ---------------------------------------------------------
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError(
            "seed must be an integer so the list can be regenerated and audited; "
            "got %r" % (type(seed).__name__,)
        )
    if not isinstance(n_enrolled, int) or isinstance(n_enrolled, bool) or n_enrolled < 2:
        raise ValueError("n_enrolled must be an integer >= 2; got %r" % (n_enrolled,))
    # Only None falls back to the default: an explicitly blank prefix would
    # produce subject numbers with no study identifier on them, which is a
    # caller error worth surfacing rather than silently papering over.
    prefix = "S" if subject_id_prefix is None else str(subject_id_prefix).strip()
    if not prefix or not prefix.isascii():
        raise ValueError("subject_id_prefix must be a non-empty ASCII string")

    design = sequences_for_design(design_key, n_periods=n_periods, is_nti=is_nti, dosing=dosing)
    n_seq = design.n_sequences

    # Cautions carry a `disclosable` flag rather than being plain strings,
    # because some of them are only sayable inside the restricted record: a
    # caution phrased in terms of "allocations per sequence per block" states
    # the block size just as plainly as printing the number would, and the
    # number of sequences is public (the protocol names TR/RT). blinded_view()
    # keeps only the disclosable ones.
    cautions: list[dict[str, Any]] = []

    # --- block size: THE classic pitfall ----------------------------------
    # A block that is not a whole multiple of the number of sequences cannot
    # contain each sequence equally often, so it silently stops being balanced.
    if block_size is None:
        block = n_seq * SEQUENCE_REPEATS_PER_BLOCK
        block_source = (
            "default: %d sequences x %d repeats per block"
            % (n_seq, SEQUENCE_REPEATS_PER_BLOCK)
        )
    else:
        if not isinstance(block_size, int) or isinstance(block_size, bool):
            raise ValueError("block_size must be an integer; got %r" % (block_size,))
        if block_size < n_seq or block_size % n_seq != 0:
            raise ValueError(
                "block_size must be a positive multiple of the number of sequences "
                "(%d for the %s design, sequences %s); got %d. A block that is not a "
                "whole multiple cannot contain each sequence equally often, so the "
                "list would not be balanced at the end of a block."
                % (n_seq, design.variant, "/".join(design.sequences), block_size)
            )
        block = block_size
        block_source = "caller-specified"
        if block == n_seq:
            cautions.append({
                "key": "block_equals_sequence_count",
                "disclosable": False,
                "text": (
                    "Block size equals the number of sequences, so once the earlier "
                    "allocations in a block are known the last one is fully determined. "
                    "Acceptable only because the list is concealed; a larger block is "
                    "preferred where any risk of deduction exists."
                ),
            })
        if block > n_seq * 4:
            cautions.append({
                "key": "large_block",
                "disclosable": False,
                "text": (
                    "Block size is more than four allocations per sequence, which lets "
                    "the running imbalance grow further between block boundaries; "
                    "balance is still exact at the end of each completed block."
                ),
            })

    repeats = block // n_seq

    # --- strata -----------------------------------------------------------
    requested = _normalise_stratify_by(stratify_by)
    kept, dropped = _select_strata(requested, n_enrolled, block)
    combos = _stratum_combinations(kept)
    stratified = bool(kept)
    n_strata = len(combos)

    for f in kept:
        if f.ascertainment == "prospective_genotyping":
            # Operational, not concealment-sensitive: the protocol has to say
            # this out loud or the site cannot plan the screening window.
            cautions.append({
                "key": "genotype_turnaround",
                "disclosable": True,
                "text": (
                    "Stratifying on %s requires the genotype result before the "
                    "randomization number is issued; confirm the assay turnaround fits "
                    "the screening window." % (f.label,)
                ),
            })

    # --- spares -----------------------------------------------------------
    if n_spare is None:
        spare_target = int(math.ceil(n_enrolled * SPARE_FRACTION))
        spare_source = "default: %.0f%% of planned enrolment" % (SPARE_FRACTION * 100,)
    else:
        if not isinstance(n_spare, int) or isinstance(n_spare, bool) or n_spare < 0:
            raise ValueError("n_spare must be a non-negative integer; got %r" % (n_spare,))
        spare_target = n_spare
        spare_source = "caller-specified"

    # --- capacity per stream ---------------------------------------------
    if stratified:
        # The split of subjects across strata is unknown before enrolment, so
        # each stratum list carries headroom above the equal-split expectation.
        expected = n_enrolled / float(n_strata)
        primary_slots = max(
            block,
            _round_up_to_multiple(int(math.ceil(expected * STRATUM_HEADROOM_FACTOR)), block),
        )
        spare_slots = _round_up_to_multiple(
            int(math.ceil(spare_target / float(n_strata))), block
        )
    else:
        primary_slots = _round_up_to_multiple(n_enrolled, block)
        spare_slots = _round_up_to_multiple(spare_target, block)

    # --- build ------------------------------------------------------------
    unblinded: list[dict[str, Any]] = []
    blinded: list[dict[str, Any]] = []
    strata_meta: list[dict[str, Any]] = []

    streams: list[tuple[str, dict[str, str]]] = []
    if stratified:
        for i, combo in enumerate(combos, start=1):
            streams.append(("ST%02d" % i, combo))
    else:
        streams.append(("ALL", {}))

    id_width = max(3, len(str(primary_slots + spare_slots)))

    for stratum_id, combo in streams:
        rng = random.Random(_derive_seed(seed, stratum_id))
        triples = _build_blocks(rng, design.sequences, primary_slots + spare_slots, block)
        for idx, (b_idx, pos, seq) in enumerate(triples, start=1):
            is_replacement = idx > primary_slots
            if stratified:
                subject_id = "%s-%s-%0*d" % (prefix, stratum_id[2:], id_width, idx)
                block_id = "%s-B%02d" % (stratum_id, b_idx)
            else:
                subject_id = "%s-%0*d" % (prefix, id_width, idx)
                block_id = "B%02d" % b_idx
            role = "replacement" if is_replacement else "primary"
            unblinded.append({
                "subject_id": subject_id,
                "randomization_number": idx,
                "stratum_id": stratum_id,
                "stratum": dict(combo),
                "block": block_id,
                "position_in_block": pos,
                "sequence": seq,
                "treatment_by_period": {
                    str(p): t for p, t in enumerate(seq, start=1)
                },
                "role": role,
                "is_replacement": is_replacement,
            })
            # The blinded log deliberately omits sequence AND block membership:
            # block identity plus a single leaked assignment would let someone
            # narrow the remaining allocations in that block.
            blinded.append({
                "subject_id": subject_id,
                "randomization_number": idx,
                "stratum_id": stratum_id,
                "stratum": dict(combo),
                "role": role,
                "is_replacement": is_replacement,
            })
        strata_meta.append({
            "stratum_id": stratum_id,
            "levels": dict(combo),
            "description": (
                "; ".join(
                    "%s = %s" % (_STRATUM_BY_KEY[k].label if k in _STRATUM_BY_KEY else k, v)
                    for k, v in combo.items()
                ) or "All subjects (no stratification)"
            ),
            "primary_slots": primary_slots,
            "replacement_slots": spare_slots,
            "n_blocks": (primary_slots + spare_slots) // block,
        })

    primary_entries = [e for e in unblinded if not e["is_replacement"]]
    balance = _balance_stats(
        primary_entries, design, block, n_enrolled, stratified, n_strata
    )

    result: dict[str, Any] = {
        "engine": "services/randomization.py",
        "engine_version": ENGINE_VERSION,
        "design": design.to_dict(),
        "reproducibility": {
            "seed": seed,
            "generator": "Python random.Random (Mersenne Twister MT19937)",
            "shuffle": "random.Random.shuffle (Fisher-Yates), one draw per block",
            "stream_seed_derivation": (
                "int(sha256('<seed>|<stratum_id>').hexdigest()[:16], 16) - SHA-256 "
                "rather than hash(), which is salted by PYTHONHASHSEED and would "
                "differ between processes"
            ),
            "global_random_used": False,
            "statement": (
                "The list is fully determined by the recorded seed, the recorded "
                "arguments and engine version %s. Re-running generate_allocation with "
                "them reproduces it entry for entry." % (ENGINE_VERSION,)
            ),
            "call": {
                "design_key": design.key,
                "n_enrolled": n_enrolled,
                "seed": seed,
                "n_periods": design.n_periods,
                "is_nti": bool(is_nti) if is_nti is not None else None,
                "block_size": block,
                "n_spare": spare_target,
                "subject_id_prefix": prefix,
                "stratify_by": [f.key for f in kept],
            },
        },
        "block": {
            "size": block,
            "source": block_source,
            "n_sequences": n_seq,
            "repeats_per_sequence_per_block": repeats,
            "is_multiple_of_n_sequences": block % n_seq == 0,
            "n_blocks_per_stream": (primary_slots + spare_slots) // block,
            # `method` is the wording that may go in the protocol body: it says
            # nothing from which the block length can be recovered. `method_detail`
            # carries the number and stays with the restricted record - stating
            # "each sequence appears twice per block" would disclose the block size
            # just as surely as printing it.
            "method": (
                "Randomly permuted blocks of undisclosed length. Every block contains "
                "each %s an equal number of times and is permuted independently, so "
                "the allocation is exactly balanced at the end of every completed "
                "block rather than only at the end of enrolment."
                % (design.assignment_term,)
            ),
            "method_detail": (
                "RESTRICTED: block size %d = %d %ss x %d repeat(s) each."
                % (block, n_seq, design.assignment_term, repeats)
            ),
            "disclosure": {
                "disclosable_in_protocol_body": False,
                "basis": (
                    "ICH E9 sect. 2.3.2 - the block length is withheld from "
                    "investigators and from the protocol body, because knowing it "
                    "makes the final allocation of each block deducible."
                ),
                "protocol_body_statement": (
                    "Subjects will be allocated to %s in a %s ratio using a "
                    "computer-generated randomization schedule prepared with randomly "
                    "permuted blocks of undisclosed length."
                    % (design.assignment_term, design.allocation_ratio)
                ),
            },
        },
        "stratification": {
            "applied": stratified,
            "n_strata": n_strata,
            "factors": [
                dict(f.to_dict(), ascertainment_note=_ASCERTAINMENT_NOTE.get(
                    f.ascertainment, ""))
                for f in kept
            ],
            "strata": strata_meta,
            "dropped_factors": dropped,
            "method": (
                "An independent permuted-block sequence is generated for each "
                "stratum, so balance holds within every stratum and not merely "
                "overall." if stratified else
                "Simple (unstratified) permuted-block randomization; no factor met "
                "the minimum of one complete block per stratum at this sample size."
            ),
            "capacity_note": (
                "Each stratum list is generated with %.0f%% more capacity than an "
                "equal split of %d subjects would predict, because the distribution "
                "across strata is not known before enrolment. Unused numbers are "
                "voided at database lock."
                % ((STRATUM_HEADROOM_FACTOR - 1.0) * 100, n_enrolled)
                if stratified else ""
            ),
        },
        "replacement": {
            "policy": "same_sequence",
            "n_replacement_slots_per_stream": spare_slots,
            "n_replacement_slots_total": spare_slots * len(streams),
            "target": spare_target,
            "source": spare_source,
            "rule": (
                "A subject who withdraws after dosing may be replaced. The "
                "replacement receives the SAME %s as the subject replaced, which is "
                "what preserves the balance of the completed blocks; the replacement "
                "subject number is drawn from the reserve tail of the list. The %s "
                "pre-printed against a reserve number is used only where no specific "
                "subject is being substituted."
                % (design.assignment_term, design.assignment_term)
            ),
            "note": (
                "Reserve allocations are generated as complete blocks, so the reserve "
                "tail is itself balanced and using it does not distort the design."
            ),
        },
        "balance": balance,
        "blinding": assess_blinding(
            label,
            pd_coprimary=pd_coprimary,
            is_biologic=is_biologic,
            study_type=study_type,
            route=route,
            immunogenicity_assessed=immunogenicity_assessed,
        ),
        "unblinded_allocation": {
            "confidentiality": "RESTRICTED",
            "contains_treatment_assignments": True,
            "holders": [
                "Unblinded randomization statistician",
                "Dispensing / investigational pharmacy",
            ],
            "warning": (
                "This is the unblinded randomization list. It must NOT appear in the "
                "protocol body, the synopsis, or any document circulated to "
                "investigators, recruiters or the bioanalytical laboratory. It is "
                "filed as a restricted appendix in the trial master file."
            ),
            "n_entries": len(unblinded),
            "entries": unblinded,
        },
        "blinded_enrolment_log": {
            "confidentiality": "BLINDED",
            "contains_treatment_assignments": False,
            "purpose": (
                "Subject numbering and stratum assignment only. Safe to reproduce in "
                "the protocol, the site file and the enrolment log; carries no "
                "treatment information and no block membership."
            ),
            "n_entries": len(blinded),
            "entries": blinded,
        },
        "assumptions": [
            {
                "name": "SEQUENCE_REPEATS_PER_BLOCK",
                "value": SEQUENCE_REPEATS_PER_BLOCK,
                "why": (
                    "Each sequence appears twice per block, giving a block size of "
                    "twice the number of sequences. One copy each would make every "
                    "block's final allocation deducible; more copies would let the "
                    "running imbalance grow further between block boundaries."
                ),
            },
            {
                "name": "SPARE_FRACTION",
                "value": SPARE_FRACTION,
                "why": (
                    "Reserve allocations are generated at 10% of planned enrolment, "
                    "rounded up to complete blocks, to cover 1:1 replacement of "
                    "post-dose withdrawals. The sample-size engine's dropout "
                    "allowance already covers attrition in the powering."
                ),
            },
            {
                "name": "STRATUM_HEADROOM_FACTOR",
                "value": STRATUM_HEADROOM_FACTOR,
                "why": (
                    "Per-stratum lists are generated with 50% more slots than an "
                    "equal split predicts, because the stratum distribution is "
                    "unknown before enrolment."
                ),
            },
            {
                "name": "MIN_BLOCKS_PER_STRATUM",
                "value": MIN_BLOCKS_PER_STRATUM,
                "why": (
                    "Stratification factors are dropped until every stratum expects "
                    "at least one complete block, because a stratum that cannot fill "
                    "a block adds imbalance rather than removing it."
                ),
            },
        ],
        "cautions": cautions,
    }
    result["summary"] = _summary(result, design, n_enrolled)
    return result


def _balance_stats(
    primary_entries: list[dict[str, Any]],
    design: DesignSequences,
    block: int,
    n_enrolled: int,
    stratified: bool,
    n_strata: int,
) -> dict[str, Any]:
    """Balance the protocol can ASSERT rather than assume.

    Two different guarantees are reported and they answer different questions.
    ``counts_full_list`` is the end-state, which permuted blocks make exact by
    construction. ``max_transient_imbalance`` is the one that actually matters
    mid-study: with ``repeats`` copies of each sequence per block, the counts of
    any two sequences can never diverge by more than ``repeats`` at ANY point
    during enrolment, because completed blocks contribute zero difference and
    only the block in progress can be lopsided.
    """
    seqs = list(design.sequences)
    repeats = block // len(seqs)

    counts_full = {s: 0 for s in seqs}
    for e in primary_entries:
        counts_full[e["sequence"]] += 1

    # Realised worst-case divergence, walked per stream in list order.
    by_stream: dict[str, dict[str, int]] = {}
    running: dict[str, dict[str, int]] = {}
    realised = 0
    for e in primary_entries:
        sid = e["stratum_id"]
        run = running.setdefault(sid, {s: 0 for s in seqs})
        run[e["sequence"]] += 1
        realised = max(realised, max(run.values()) - min(run.values()))
        by_stream[sid] = dict(run)

    if stratified:
        counts_at_enrolment: Optional[dict[str, int]] = None
        max_at_enrolment: Optional[int] = None
    else:
        head = primary_entries[:n_enrolled]
        counts_at_enrolment = {s: 0 for s in seqs}
        for e in head:
            counts_at_enrolment[e["sequence"]] += 1
        max_at_enrolment = max(counts_at_enrolment.values()) - min(
            counts_at_enrolment.values()
        )

    # Two statements, deliberately. The technical one quantifies the transient
    # bound, which is what a statistician needs - but quoting "no more than R"
    # discloses the repeats per block and therefore the block size, so it stays
    # with the restricted record. The disclosable one asserts the same guarantee
    # without the number and is what the protocol body may say.
    statement = (
        "Allocation is exactly balanced at the end of every completed block: each "
        "block contains all %d %ss %d time(s). At no point during enrolment can the "
        "number of subjects on any two %ss differ by more than %d, since only the "
        "block in progress can be unequal."
        % (len(seqs), design.assignment_term, repeats, design.assignment_term, repeats)
    )
    disclosable = (
        "Allocation is exactly balanced across %ss at the end of every completed "
        "block, so balance is maintained throughout enrolment and not only at its "
        "end. The block length is not stated here, in order to preserve allocation "
        "concealment." % (design.assignment_term,)
    )
    if stratified:
        tail = (
            " That guarantee holds WITHIN each of the %d strata, not merely overall."
            % (n_strata,)
        )
    elif max_at_enrolment == 0:
        tail = (
            " At the planned enrolment of %d subjects the allocation is exactly "
            "equal across %ss." % (n_enrolled, design.assignment_term)
        )
    else:
        tail = (
            " At the planned enrolment of %d subjects the projected difference "
            "between the largest and smallest %s is %d subject(s), because enrolment "
            "stops part-way through a block."
            % (n_enrolled, design.assignment_term, max_at_enrolment)
        )
    statement += tail
    disclosable += tail

    return {
        "sequences": seqs,
        "allocation_ratio": design.allocation_ratio,
        "counts_full_list": counts_full,
        "counts_at_planned_enrolment": counts_at_enrolment,
        "max_imbalance_at_planned_enrolment": max_at_enrolment,
        "max_transient_imbalance_theoretical": repeats,
        "max_transient_imbalance_realised": realised,
        "exact_at_every_completed_block": True,
        "by_stratum_counts": by_stream,
        "statement": statement,
        "disclosable_statement": disclosable,
    }


def _summary(result: dict[str, Any], design: DesignSequences, n_enrolled: int) -> str:
    """One-paragraph plain statement for the protocol narrative."""
    strat = result["stratification"]
    bits = [
        "Subjects are allocated to %s (%s) in a %s ratio by a computer-generated "
        "schedule using randomly permuted blocks."
        % (
            design.assignment_term,
            "/".join(design.sequences),
            design.allocation_ratio,
        )
    ]
    if strat["applied"]:
        names = ", ".join(f["label"] for f in strat["factors"])
        bits.append(
            "Randomization is stratified by %s, giving %d strata, each with its own "
            "independent block sequence." % (names, strat["n_strata"])
        )
    else:
        bits.append(
            "Randomization is not stratified: no candidate factor could be balanced "
            "at a planned enrolment of %d subjects." % (n_enrolled,)
        )
    bits.append(result["balance"]["disclosable_statement"])
    bits.append(
        "The schedule is generated from a recorded integer seed so that it can be "
        "regenerated and verified during inspection, and it is held by the unblinded "
        "randomization statistician and the dispensing pharmacy only."
    )
    return " ".join(bits)


# ---------------------------------------------------------------------------
# Views and prompt rendering
# ---------------------------------------------------------------------------


def blinded_view(result: dict[str, Any]) -> dict[str, Any]:
    """The same result with everything treatment-revealing removed.

    Use this for anything a blinded reader can reach: the stored protocol body,
    an export, a UI panel. Dropping the list alone is NOT enough. Two secrets
    have to survive this function - the assignments themselves, and the block
    length (ICH E9 sect. 2.3.2, which makes the last allocation of each block
    deducible) - and both are recoverable from fields that look innocuous:

    * the SEED, plus the recorded call, re-runs the engine and rebuilds the list
      entry for entry. That is the entire point of recording it, and it is
      exactly why it cannot travel with the blinded copy;
    * ``reproducibility.call.block_size`` states the block length outright;
    * the repeats-per-block assumption gives it too, since the sequence count is
      public (the protocol names TR/RT);
    * the per-stratum and replacement SLOT COUNTS are whole multiples of the
      block, so ``primary_slots // n_blocks`` recovers it arithmetically;
    * the transient-imbalance bound equals the repeats per block;
    * a caution phrased as "block size equals the number of sequences" says it
      in words.

    Every one of those is stripped here, and what was removed is listed in
    ``withheld`` so a reader can see that the omission was deliberate rather
    than wonder whether the record is incomplete.
    """
    if not result:
        return {}
    out = {k: v for k, v in result.items() if k != "unblinded_allocation"}

    block = result.get("block") or {}
    out["block"] = {
        "disclosed": False,
        "method": block.get("method", ""),
        "reason": (block.get("disclosure") or {}).get("basis", ""),
        "protocol_body_statement": (block.get("disclosure") or {}).get(
            "protocol_body_statement", ""
        ),
    }

    # The seed and the recorded call regenerate the list outright, and the call
    # carries the block size verbatim. Keep the AUDIT CLAIM (a blinded reader is
    # entitled to know the schedule is reproducible on demand) without the
    # material that makes it reproducible by them.
    repro = result.get("reproducibility") or {}
    out["reproducibility"] = {
        "seed_recorded": repro.get("seed") is not None,
        "seed_disclosed": False,
        "generator": repro.get("generator", ""),
        "statement": (
            "The schedule was generated from a recorded integer seed and can be "
            "regenerated entry for entry and verified during inspection. The seed "
            "and the generating arguments are held with the restricted "
            "randomization record and with the unblinded statistician, because "
            "anyone holding them can rebuild the allocation list."
        ),
    }

    bal = dict(result.get("balance") or {})
    for k in (
        "max_transient_imbalance_theoretical",
        "max_transient_imbalance_realised",
        "statement",
    ):
        bal.pop(k, None)
    if "disclosable_statement" in bal:
        bal["statement"] = bal["disclosable_statement"]
    out["balance"] = bal

    # block size = number of sequences x repeats, and the sequence set is public.
    out["assumptions"] = [
        a for a in (result.get("assumptions") or [])
        if a.get("name") != "SEQUENCE_REPEATS_PER_BLOCK"
    ]

    # Slot counts are rounded up to whole blocks, so they divide back to it.
    strat = dict(result.get("stratification") or {})
    if strat.get("strata"):
        strat["strata"] = [
            {k: v for k, v in s.items()
             if k not in ("primary_slots", "replacement_slots", "n_blocks")}
            for s in strat["strata"]
        ]
    out["stratification"] = strat

    out["replacement"] = {
        k: v for k, v in (result.get("replacement") or {}).items()
        if k not in ("n_replacement_slots_per_stream", "n_replacement_slots_total")
    }

    out["cautions"] = [
        c for c in (result.get("cautions") or []) if c.get("disclosable")
    ]

    out["withheld"] = {
        "reason": (
            "Withheld so that neither the treatment assignments nor the block "
            "length can be recovered from this copy (ICH E9 sect. 2.3.2). Held "
            "with the restricted randomization record."
        ),
        "fields": [
            "unblinded_allocation",
            "reproducibility.seed",
            "reproducibility.call",
            "block.size",
            "assumptions.SEQUENCE_REPEATS_PER_BLOCK",
            "stratification.strata[].primary_slots",
            "stratification.strata[].replacement_slots",
            "stratification.strata[].n_blocks",
            "replacement.n_replacement_slots_per_stream",
            "replacement.n_replacement_slots_total",
            "balance.max_transient_imbalance_theoretical",
            "balance.max_transient_imbalance_realised",
            "cautions (block-length-revealing entries)",
        ],
    }
    return out


def format_unblinded_list(result: dict[str, Any]) -> str:
    """RESTRICTED appendix: the pharmacy/statistician allocation list as CSV.

    ASCII only - this is printed and filed on Windows consoles.
    """
    if not result or not result.get("unblinded_allocation"):
        return ""
    header = (
        "subject_id,randomization_number,stratum_id,stratum,block,"
        "position_in_block,sequence,role"
    )
    lines = [
        "# RESTRICTED - UNBLINDED RANDOMIZATION LIST",
        "# engine=%s version=%s seed=%s"
        % (
            result.get("engine", ""),
            result.get("engine_version", ""),
            (result.get("reproducibility") or {}).get("seed", ""),
        ),
        "# Do not circulate to investigators, recruiters or the bioanalytical lab.",
        header,
    ]
    for e in result["unblinded_allocation"]["entries"]:
        stratum = ";".join("%s=%s" % (k, v) for k, v in sorted(e["stratum"].items()))
        lines.append(
            "%s,%d,%s,%s,%s,%d,%s,%s"
            % (
                e["subject_id"],
                e["randomization_number"],
                e["stratum_id"],
                stratum,
                e["block"],
                e["position_in_block"],
                e["sequence"],
                e["role"],
            )
        )
    return _ascii("\n".join(lines))


def randomization_directive(alloc: dict[str, Any]) -> str:
    """Render the locked randomization facts as prompt text for the generator.

    Mirrors ``stratification.stratification_directive``: settled facts to
    narrate, never a menu to re-decide. Critically, it hands the model the
    METHOD and forbids it from reproducing any per-subject assignment or the
    block size - putting either in the protocol body would compromise the
    study's allocation concealment.
    """
    if not alloc or not alloc.get("design"):
        return ""
    design = alloc["design"]
    block = alloc.get("block") or {}
    strat = alloc.get("stratification") or {}
    bal = alloc.get("balance") or {}
    blind = alloc.get("blinding") or {}
    masking = (blind.get("treatment_masking") or {})

    lines = [
        "RANDOMIZATION AND BLINDING - these facts are ALREADY DETERMINED. Narrate "
        "them; do not change the design, the sequence set, the allocation ratio, or "
        "the stratification factors, and do not invent any numbers.",
        "- Design: %s" % (design.get("label", ""),),
        "- %s set: %s (%d periods, allocation ratio %s)"
        % (
            design.get("assignment_term", "sequence").capitalize(),
            " / ".join(design.get("sequences", [])),
            design.get("n_periods", 0),
            design.get("allocation_ratio", ""),
        ),
        "- Why this sequence set: %s" % (design.get("rationale", ""),),
        "- Method: %s" % (block.get("method", ""),),
    ]

    if strat.get("applied"):
        names = "; ".join(f.get("label", "") for f in strat.get("factors", []))
        lines.append(
            "- Stratified by: %s (%d strata). %s"
            % (names, strat.get("n_strata", 0), strat.get("method", ""))
        )
    else:
        lines.append("- Stratification: %s" % (strat.get("method", ""),))
    for d in strat.get("dropped_factors", []):
        lines.append(
            "- NOT used as a stratum: %s - %s" % (d.get("label", ""), d.get("reason", ""))
        )

    # The disclosable wording, not the technical one: the technical statement
    # quantifies the transient imbalance, from which the block size follows.
    lines.append("- Balance guarantee: %s" % (bal.get("disclosable_statement", ""),))

    repl = alloc.get("replacement") or {}
    if repl.get("rule"):
        lines.append("- Replacement of withdrawals: %s" % (repl["rule"],))

    conceal = blind.get("allocation_concealment") or {}
    if conceal.get("statement"):
        lines.append("- Allocation concealment: %s" % (conceal["statement"],))
    if masking.get("recommended"):
        lines.append(
            "- Treatment masking: %s. %s"
            % (masking["recommended"].replace("_", " "), masking.get("rationale", ""))
        )
    # WHO is masked is a separate fact from the posture's name, and the two are
    # easy to conflate in prose. Stating both, and forbidding any wider claim,
    # is what stops the model writing "double-blind" over an assessor-blind
    # study or "the subject was blinded" where the pharmacology defeats it.
    if masking.get("who_is_blinded"):
        lines.append(
            "- Blinded to treatment: %s. NOT blinded: %s. Describe exactly these "
            "parties as blinded - do not widen the claim, and do not use the phrase "
            "\"double-blind\" unless the posture above is double blind."
            % (
                ", ".join(
                    str(r).replace("_", " ") for r in masking.get("who_is_blinded", [])
                ),
                ", ".join(
                    str(r).replace("_", " ") for r in masking.get("who_is_unblinded", [])
                ) or "no one",
            )
        )
    if masking.get("operational_statement"):
        lines.append(
            "- How the masking is operated: %s" % (masking["operational_statement"],)
        )
    for drv in masking.get("drivers", []):
        if drv.get("statement"):
            lines.append("  - Why masking is at this level: %s" % (drv["statement"],))
    for sig in masking.get("signals", []):
        lines.append(
            "  - Masking signal from the product label: %s. %s Mitigation: %s"
            % (sig.get("label", ""), sig.get("why", ""), sig.get("mitigation", ""))
        )
    bio = blind.get("bioanalytical_blinding") or {}
    if bio.get("statement"):
        lines.append("- Bioanalytical blinding: %s" % (bio["statement"],))
    unb = blind.get("unblinding_procedure") or {}
    if unb.get("statement"):
        lines.append("- Code break: %s" % (unb["statement"],))

    # Accept EITHER the full result or blinded_view()'s output. The integration
    # path deliberately puts the blinded view in the prompt context (the raw
    # result carries the list and the seed), and blinded_view flattens
    # block.disclosure up into block, so read both shapes. The directive draws
    # only on disclosable material, so both shapes render identically - which is
    # what makes it safe to build the prompt from either.
    disclosure = block.get("disclosure") or {}
    basis = disclosure.get("basis") or block.get("reason", "")
    body_statement = (
        disclosure.get("protocol_body_statement")
        or block.get("protocol_body_statement", "")
    )
    lines.append(
        "CONFIDENTIALITY - MANDATORY: do NOT reproduce any per-subject allocation, "
        "randomization number paired with a treatment, or the block size anywhere in "
        "the protocol text. %s Use this exact wording for the allocation statement: "
        "\"%s\"" % (basis, body_statement)
    )
    return "\n".join(lines)
