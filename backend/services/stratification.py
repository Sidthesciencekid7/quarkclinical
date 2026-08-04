"""Patient characteristics driving PK variability — stratification at randomization.

WHY THIS MODULE EXISTS
----------------------
A bioequivalence/PK study's precision is set by the variance of the exposure
comparison. Some of that variance is irreducible assay/biological noise, but a
large, *predictable* share is carried by a handful of subject characteristics
(metabolizer genotype, renal function, body size, sex, ...). If those
characteristics land unevenly across treatment arms — or across sequences in a
crossover — they inflate the confidence interval and can bias the point estimate
outright. Stratified randomization removes that risk by construction.

This module is the deterministic knowledge layer for that decision. Like
:mod:`services.biostatistics` (sample size) and :mod:`services.design_decisions`
(the five clinical rules), it produces FACTS the AI narrates rather than
opinions the AI invents: the factor list, its rationale, and its evidence basis
are all computed here and passed to the generation prompt as locked context.

THE DESIGN NUANCE THAT MAKES THIS CORRECT
-----------------------------------------
The same covariate matters very differently depending on the design, and getting
this backwards is the classic error:

* **Crossover / replicate** — each subject receives BOTH treatments, so they are
  their own control and *between-subject* covariates (weight, sex, genotype)
  largely CANCEL from the treatment contrast. They do NOT inflate the
  within-subject CV that powers the study. Stratification here is about
  **balancing the covariate across SEQUENCES**, so a covariate cannot become
  confounded with a sequence or period effect (and so subgroup summaries are
  interpretable). Over-claiming a precision benefit here would be wrong.

* **Parallel** — the treatment contrast IS a between-subject comparison, so these
  covariates land directly in the error term. Stratification materially tightens
  the CI and protects against chance imbalance. This is where it really counts.

A factor is therefore reported with a design-aware ``effect`` describing what
stratifying actually buys, never a generic "reduces variability".

REGULATORY FRAMING
------------------
ICH E9 §2.3.2 (baseline covariate balance / stratified randomization), ICH E17
(intrinsic ethnic factors in multi-regional studies), FDA Clinical Pharmacogenomics
guidance (2013) and FDA BE guidance. Genotype-stratified or genotype-*excluded*
enrolment is explicitly contemplated for drugs with a polymorphic clearance
pathway.

Nothing here overrides the eligibility criteria: in a healthy-volunteer BE study
most of these are already narrowed by inclusion/exclusion (age 18-55, BMI
18.5-30, non-smoker, normal renal/hepatic function). The engine reports that
explicitly via ``narrowed_by_eligibility`` so the protocol does not promise a
stratification that the entry criteria have already made moot.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Factor catalogue
# ---------------------------------------------------------------------------
# `tier` ranks the magnitude of PK impact when the factor IS in play:
#   1 = can span an order of magnitude (bimodal genotype, organ failure)
#   2 = substantial, continuous (typically 1.2-2x exposure)
#   3 = drug-class specific / usually handled by exclusion instead
#
# `magnitude` is deliberately a short, sourced-style phrase — it is rendered in
# the protocol table verbatim, so it must be defensible, not hand-wavy.


@dataclass(frozen=True)
class StratificationFactor:
    key: str
    label: str
    tier: int
    magnitude: str            # size of the PK effect when in play
    rationale: str            # WHY it drives variability (mechanism)
    evidence: str             # what the applicability judgement rests on
    applies_when: str         # the condition that makes it relevant
    # Populated per-study by recommend_stratification():
    recommended: bool = False
    effect: str = ""                    # design-aware: what stratifying buys
    narrowed_by_eligibility: bool = False
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_CATALOGUE: tuple[StratificationFactor, ...] = (
    StratificationFactor(
        key="cyp_polymorphism",
        label="Polymorphic CYP metabolizer status (CYP2D6 / CYP2C19 / CYP2C9)",
        tier=1,
        magnitude="Up to 10-100x AUC between poor and ultrarapid metabolizers",
        rationale=(
            "When a single polymorphic CYP carries the major clearance pathway, the "
            "population's exposure is multi-modal rather than unimodal: poor "
            "metabolizers have little or no functional enzyme, while ultrarapid "
            "metabolizers carry gene duplications. This is the single largest "
            "identifiable source of PK variability for affected drugs, and it is "
            "genetic, so it cannot be normalised away by dosing or fasting rules."
        ),
        evidence="Clearance pathway from the approved label / PK literature",
        applies_when="The drug's principal clearance is via a polymorphic CYP enzyme",
    ),
    StratificationFactor(
        key="transporter_polymorphism",
        label="Drug-transporter genotype (SLCO1B1 / ABCG2 / ABCB1)",
        tier=1,
        magnitude="Typically 2-5x AUC for reduced-function homozygotes",
        rationale=(
            "Uptake and efflux transporters gate hepatic extraction and intestinal "
            "absorption. Reduced-function variants raise systemic exposure sharply — "
            "the SLCO1B1 c.521T>C (OATP1B1) effect on statin acid AUC is the textbook "
            "case. Because the variant frequency is appreciable, a modest sample can "
            "easily land these subjects unevenly across arms."
        ),
        evidence="Transporter substrate status from the label / PK literature",
        applies_when="The drug is a substrate of a polymorphic uptake/efflux transporter",
    ),
    StratificationFactor(
        key="renal_function",
        label="Renal function (eGFR / creatinine clearance)",
        tier=1,
        magnitude="CL/F falls roughly in proportion to GFR for renally cleared drugs",
        rationale=(
            "For a drug with a substantial unchanged-in-urine fraction, systemic "
            "clearance tracks glomerular filtration almost linearly. Even subjects who "
            "all qualify as 'normal' span a wide eGFR range (roughly 70-130 "
            "mL/min/1.73m2), so renal function remains a graded, continuous driver of "
            "exposure inside a healthy cohort."
        ),
        evidence="Fraction excreted unchanged / renal-impairment labelling",
        applies_when="A meaningful fraction of the dose is renally eliminated",
    ),
    StratificationFactor(
        key="hepatic_function",
        label="Hepatic function / hepatic blood flow",
        tier=1,
        magnitude="Several-fold exposure change for high-extraction drugs",
        rationale=(
            "For hepatically eliminated drugs — especially high-extraction compounds "
            "whose first-pass loss depends on hepatic blood flow — modest differences "
            "in liver function or perfusion translate into large differences in "
            "bioavailability and clearance."
        ),
        evidence="Hepatic clearance / extraction ratio from the label",
        applies_when="The drug is predominantly hepatically cleared or high-extraction",
    ),
    StratificationFactor(
        key="sex",
        label="Sex",
        tier=2,
        magnitude="Commonly 20-40% differences in dose-normalised exposure",
        rationale=(
            "Sex aggregates several mechanisms at once: body composition (fat mass "
            "drives volume of distribution for lipophilic drugs), higher CYP3A4 and "
            "lower CYP1A2 activity in females, differences in gastric emptying and "
            "biliary transport, and lower average body weight at a fixed dose. It is "
            "also the covariate regulators most expect to see represented and "
            "balanced."
        ),
        evidence="ICH E9 covariate balance; sex-difference PK literature",
        applies_when="Both sexes are enrolled (essentially always)",
    ),
    StratificationFactor(
        key="body_size",
        label="Body weight / BMI",
        tier=2,
        magnitude="Exposure varies inversely with body size at a fixed dose",
        rationale=(
            "Volume of distribution and clearance both scale with body size, so a "
            "fixed (non-weight-based) dose delivers systematically higher exposure to "
            "lighter subjects. The effect is strongest for lipophilic drugs and for "
            "biologics, where distribution is closely tied to body mass."
        ),
        evidence="Allometric scaling of Vd/CL; fixed-dose administration",
        applies_when="A fixed dose is given across a permitted BMI range",
    ),
    StratificationFactor(
        key="ancestry",
        label="Race / ethnicity (genetic ancestry)",
        tier=2,
        magnitude="Shifts the frequency of large-effect variants, not exposure directly",
        rationale=(
            "Ancestry matters as a proxy for allele frequency rather than as a "
            "mechanism in itself: CYP2C19 poor metabolizers are roughly 15-20% of East "
            "Asian populations versus 2-5% of European, and CYP3A5*3, NAT2, UGT1A1*28 "
            "and SLCO1B1 frequencies differ likewise. Ancestry imbalance therefore "
            "imports genotype imbalance even when genotype itself is not measured."
        ),
        evidence="ICH E17 intrinsic ethnic factors; population allele frequencies",
        applies_when="The clearance pathway carries an ancestry-linked polymorphism",
    ),
    StratificationFactor(
        key="age",
        label="Age",
        tier=2,
        magnitude="GFR declines roughly 1%/year beyond ~40 years",
        rationale=(
            "Age is a composite proxy for declining renal and hepatic function, "
            "reduced hepatic blood flow, and a rising fat-to-lean mass ratio. Within a "
            "conventional 18-55 healthy-volunteer window its effect is modest; it "
            "becomes a first-order driver as soon as older subjects are admitted."
        ),
        evidence="Age-related organ-function decline",
        applies_when="The permitted age range is wide or includes elderly subjects",
    ),
    StratificationFactor(
        key="smoking_status",
        label="Smoking status",
        tier=3,
        magnitude="Up to ~2x clearance increase for CYP1A2 substrates",
        rationale=(
            "Polycyclic aromatic hydrocarbons in tobacco smoke are potent CYP1A2 "
            "inducers, so smokers clear CYP1A2 substrates substantially faster. The "
            "effect is an induction phenomenon and reverses over days to weeks after "
            "cessation, which makes recent-quitters a hidden intermediate group."
        ),
        evidence="CYP1A2 substrate status; smoking-induction literature",
        applies_when="The drug is a CYP1A2 substrate and smokers are not excluded",
    ),
    StratificationFactor(
        key="nat2_acetylator",
        label="NAT2 acetylator phenotype",
        tier=3,
        magnitude="Clear bimodal exposure split between slow and rapid acetylators",
        rationale=(
            "N-acetyltransferase 2 activity is bimodally distributed. For drugs "
            "cleared by acetylation (isoniazid, hydralazine, procainamide, "
            "sulfamethoxazole) slow acetylators show markedly higher exposure, "
            "producing a two-peaked distribution rather than a spread around a mean."
        ),
        evidence="Acetylation clearance pathway",
        applies_when="The drug is cleared by N-acetylation",
    ),
    StratificationFactor(
        key="gastric_ph",
        label="Gastric pH / acid-suppressant use",
        tier=3,
        magnitude="Can reduce absorption several-fold for pH-dependent weak bases",
        rationale=(
            "Weak bases whose dissolution depends on an acidic stomach lose absorption "
            "when gastric pH rises. Baseline achlorhydria and any concomitant "
            "PPI/H2-blocker use therefore act as a large, absorption-side source of "
            "variability that is invisible to metabolic covariates."
        ),
        evidence="pH-dependent solubility / acid-suppressant interaction labelling",
        applies_when="The drug's dissolution is gastric-pH dependent",
    ),
)

_BY_KEY = {f.key: f for f in _CATALOGUE}

# Factors a conventional healthy-volunteer BE protocol has already narrowed via
# inclusion/exclusion criteria — reported so the protocol doesn't promise a
# stratification the entry criteria already enforce.
_ELIGIBILITY_NARROWED = {
    "renal_function": "Inclusion requires normal renal laboratory values",
    "hepatic_function": "Inclusion requires normal hepatic laboratory values",
    "age": "Inclusion restricts age to approximately 18-55 years",
    "smoking_status": "Inclusion requires non-smoker status",
    "gastric_ph": "Exclusion prohibits recent acid-suppressant use",
}


# ---------------------------------------------------------------------------
# Modality gate - factors that only exist because the product is a small molecule
# ---------------------------------------------------------------------------
# A therapeutic protein (monoclonal antibody, fusion protein, insulin or other
# peptide analogue) is not cleared the way a small molecule is. It is catabolised
# to amino acids by ubiquitous proteolysis, recycled by FcRn where it carries an
# Fc domain, and - for a target-binding molecule - removed by target-mediated
# drug disposition. It is not a substrate of a cytochrome P450 enzyme, of a
# xenobiotic membrane transporter, or of an N-acetyltransferase, and it is never
# swallowed, because gastric acid and gut proteases would destroy it.
#
# Every factor below therefore has NO MECHANISM for such a product. Carrying one
# into a biosimilar protocol is worse than merely wasting a table row: the
# smoking factor asserts "Inclusion requires non-smoker status" (see
# _ELIGIBILITY_NARROWED above), which imports a real eligibility criterion into
# the protocol and shrinks the recruitable population for a reason that does not
# exist for this product. That is precisely the finding a reviewing clinical
# pharmacologist raised against a generated canakinumab protocol.
#
# The value is a NEGATIVE statement, written so a reviewer - and the narration
# layer, which is handed this whole dict - reads that the factor was CONSIDERED
# and deliberately excluded, rather than finding it silently absent.
_SMALL_MOLECULE_ONLY: dict[str, str] = {
    "cyp_polymorphism": (
        "Not applicable to a therapeutic protein: the product is catabolised to "
        "amino acids by proteolysis and is not a cytochrome P450 substrate, so "
        "metabolizer genotype cannot move its exposure."
    ),
    # A 150 kDa immunoglobulin cannot cross a membrane through OATP1B1, BCRP or
    # P-glycoprotein - those carriers move small organic molecules of a few
    # hundred daltons. The cue list for this factor is also the loosest in the
    # module (bare "transporter" and "p-gp"), so a biologic label that merely
    # states "no transporter-mediated interactions are expected" would otherwise
    # trigger a genotype stratification on a mechanism the label was denying.
    "transporter_polymorphism": (
        "Not applicable to a therapeutic protein: uptake and efflux transporters "
        "carry small organic molecules, not a protein of this size, which is "
        "removed by proteolytic catabolism and target-mediated disposition."
    ),
    "nat2_acetylator": (
        "Not applicable to a therapeutic protein: N-acetylation is a xenobiotic "
        "conjugation pathway for small molecules carrying an acetylatable amine, "
        "which this product does not present."
    ),
    "smoking_status": (
        "Not applicable to a therapeutic protein: smoking matters only because "
        "polycyclic aromatic hydrocarbons induce CYP1A2, and this product is not "
        "a CYP1A2 substrate. No smoking-related eligibility requirement is "
        "imposed and smokers are eligible."
    ),
    # Gastric pH is an ORAL ABSORPTION factor: it governs whether a weak base
    # dissolves before it reaches the absorptive surface. A therapeutic protein
    # has no gastric dissolution step at all - it is injected, because swallowing
    # it would destroy it - so the factor has no pathway to act through. Note
    # this is a modality argument, not a route argument: it holds for a protein
    # product regardless of which parenteral route is chosen.
    "gastric_ph": (
        "Not applicable to a therapeutic protein: the product is administered "
        "parenterally and has no gastric dissolution step, so gastric pH and "
        "acid-suppressant use — including proton pump inhibitors, H2-receptor "
        "antagonists and antacids — cannot affect its absorption."
    ),
    # Ancestry is retained in the catalogue as a PROXY FOR ALLELE FREQUENCY at the
    # three pharmacogenetic loci above (its rationale names CYP2C19, CYP3A5*3,
    # NAT2, UGT1A1*28 and SLCO1B1 explicitly). With all three suppressed the proxy
    # has nothing left to proxy for, and leaving it in would put a paragraph of
    # small-molecule pharmacogenetics into a protein protocol. An ancestry-linked
    # IMMUNOGENICITY factor (HLA haplotype and anti-drug antibody incidence) is a
    # real and different thing; if it is ever added it belongs in the catalogue as
    # its own entry with its own mechanism, not smuggled in through this one.
    "ancestry": (
        "Not applicable as written to a therapeutic protein: this factor is a "
        "proxy for the frequency of polymorphic metabolism and transporter "
        "variants, none of which govern the clearance of a protein product."
    ),
}


# Factors that are RETAINED for a therapeutic protein but whose catalogue text
# argues from a small-molecule mechanism. Suppressing the inapplicable factors
# is only half the job: the rationale/magnitude strings are rendered verbatim
# into the protocol table, so a retained factor that explains itself with
# "higher CYP3A4 and lower CYP1A2 activity in females" puts cytochrome P450
# reasoning into a biosimilar protocol just as surely as a suppressed row would.
# That is the same finding the reviewing pharmacologist raised (no CYP anywhere
# in a biologic protocol), so the fix has to reach the surviving rows too.
#
# Only the fields that actually assert a mechanism are overridden; `key`,
# `label`, `tier` and `applies_when` are untouched, so ordering, the randomizer's
# factor keys and the eligibility-narrowing notes behave identically.
#
# Body size is deliberately NOT overridden: its catalogue rationale already names
# biologics explicitly and is correct as written - at a fixed (non-weight-based)
# dose a protein's exposure scales inversely with body mass, which is precisely
# why body size and sex must SURVIVE the modality gate rather than be swept out
# with the metabolism-driven factors.
_BIOLOGIC_FIELD_OVERRIDES: dict[str, dict[str, Any]] = {
    "sex": {
        "magnitude": (
            "Commonly 20-40% differences in dose-normalised exposure, "
            "largely tracking body size"
        ),
        "rationale": (
            "For a therapeutic protein the sex difference is a body-composition "
            "effect rather than an enzyme effect: at a fixed, non-weight-based "
            "dose the lower average body mass and the different lean-to-fat ratio "
            "of female subjects give a smaller distribution volume and therefore "
            "higher exposure. Sex is also the covariate regulators most expect to "
            "see represented and balanced across arms."
        ),
        "evidence": (
            "ICH E9 covariate balance; body-size scaling of protein disposition"
        ),
    },
    "renal_function": {
        "magnitude": (
            "Material for peptides and small proteins that are filtered at the "
            "glomerulus; negligible for antibody-sized molecules"
        ),
        "rationale": (
            "A therapeutic protein is eliminated by proteolytic catabolism, and an "
            "antibody-sized molecule (around 150 kDa) is far above the glomerular "
            "filtration cutoff, so renal function does not set its clearance. "
            "Peptides and smaller proteins ARE filtered and reabsorbed by the "
            "proximal tubule, and for those renal function stays a graded driver of "
            "exposure. This factor is therefore carried only where the label "
            "evidences renal elimination of THIS product."
        ),
        "evidence": (
            "Molecular size relative to the glomerular filtration cutoff; "
            "renal-elimination statements in the label"
        ),
    },
    "hepatic_function": {
        "magnitude": (
            "Not a clearance driver for a protein product; carried only where the "
            "label evidences hepatic handling"
        ),
        "rationale": (
            "A therapeutic protein is not a substrate of hepatic biotransformation "
            "enzymes and, being given parenterally, has no first-pass step, so "
            "hepatic extraction does not set its exposure. Advanced hepatic disease "
            "can still alter protein catabolism and plasma protein status, but "
            "inside a screened cohort this is not a first-order source of "
            "variability for such a product."
        ),
        "evidence": (
            "Proteolytic elimination via catabolic pathways unrelated to hepatic "
            "biotransformation enzymes; hepatic-impairment statements in the label"
        ),
    },
}


# ---------------------------------------------------------------------------
# Applicability detection — deterministic cues from the label / PK text
# ---------------------------------------------------------------------------
# Matched case-insensitively against the concatenated DailyMed label sections
# (clinical pharmacology / pharmacokinetics) already fetched for the protocol.
# This mirrors design_decisions.py's approach of grounding a decision in the
# real label text rather than model recall.

_CUES: dict[str, tuple[str, ...]] = {
    "cyp_polymorphism": (
        r"cyp\s?2d6", r"cyp\s?2c19", r"cyp\s?2c9",
        r"poor metaboli[sz]er", r"extensive metaboli[sz]er",
        r"ultra[- ]?rapid metaboli[sz]er", r"intermediate metaboli[sz]er",
        r"polymorphic", r"pharmacogenomic",
    ),
    "transporter_polymorphism": (
        r"slco1b1", r"oatp1b1", r"abcg2", r"\bbcrp\b", r"abcb1",
        r"p-?glycoprotein", r"\bp-?gp\b", r"transporter",
    ),
    "renal_function": (
        r"renal (?:impairment|elimination|excretion|clearance)",
        r"excreted (?:un)?changed in (?:the )?urine",
        r"creatinine clearance", r"\begfr\b", r"kidney",
        r"recovered in (?:the )?urine",
    ),
    "hepatic_function": (
        r"hepatic (?:impairment|metabolism|clearance|extraction)",
        r"first[- ]pass", r"metaboli[sz]ed (?:in|by) the liver", r"\bhepatic\b",
    ),
    "smoking_status": (r"cyp\s?1a2", r"smok"),
    "nat2_acetylator": (r"\bnat2\b", r"acetylat"),
    "gastric_ph": (
        r"gastric ph", r"ph[- ]dependent", r"proton pump inhibitor",
        r"\bppi\b", r"h2[- ]?(?:receptor )?antagonist", r"achlorhydria",
    ),
}

# Ancestry is recommended when an ancestry-linked polymorphism is in play, so it
# piggybacks on whichever of these were detected.
_ANCESTRY_TRIGGERS = ("cyp_polymorphism", "transporter_polymorphism", "nat2_acetylator")


def _detect(text: str) -> set[str]:
    """Which catalogue factors the label/PK text gives positive evidence for."""
    if not text:
        return set()
    lowered = text.lower()
    hits: set[str] = set()
    for key, patterns in _CUES.items():
        if any(re.search(p, lowered) for p in patterns):
            hits.add(key)
    return hits


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def recommend_stratification(
    *,
    design_key: Optional[str] = None,
    label: Optional[dict[str, str]] = None,
    is_nti: Optional[bool] = None,
    cv_intra_pct: Optional[float] = None,
    healthy_volunteers: bool = True,
    is_biologic: bool = False,
) -> dict[str, Any]:
    """Select the stratification factors that apply to THIS study.

    Parameters
    ----------
    design_key:
        'crossover' | 'parallel' | 'replicate'. Drives the design-aware ``effect``
        wording and whether stratification is materially precision-improving.
    label:
        The trimmed DailyMed label dict already assembled for the protocol
        (``_fetch_label_safe``). Its pharmacology/PK text supplies the evidence
        for which clearance pathways are in play.
    is_nti / cv_intra_pct:
        Narrow-therapeutic-index status and the sourced within-subject CV. A
        highly-variable drug (CV >= 30%) or an NTI drug raises the stakes on
        covariate balance, which is surfaced in the summary.
    healthy_volunteers:
        When True, factors already constrained by conventional BE eligibility
        criteria are flagged ``narrowed_by_eligibility`` rather than dropped.
    is_biologic:
        True when the product is a therapeutic protein (monoclonal antibody,
        fusion protein, insulin or peptide analogue). Factors in
        ``_SMALL_MOLECULE_ONLY`` are then withheld, because the mechanism that
        makes them matter does not exist for such a product. They are reported
        under ``suppressed_factors`` rather than silently dropped, so a reviewer
        can see the factor was considered and deliberately excluded, and the
        factors that DO survive argue from a protein-appropriate mechanism (see
        ``_BIOLOGIC_FIELD_OVERRIDES``). Defaults to False, so small-molecule
        behaviour is unchanged.

    Returns
    -------
    dict with ``design_key``, ``between_subject_relevant``, ``is_biologic``,
    ``summary``, ``factors`` (recommended, tier-ordered), ``considered`` (the
    catalogue with applicability) and ``suppressed_factors``, JSON-able for
    storage in population_config.

    ``suppressed_factors`` is empty for a small molecule. For a biologic it
    carries EVERY modality-excluded factor with its mechanistic reason and a
    ``would_have_been_recommended`` flag saying whether the label had actually
    cued it - because the list doubles as the prohibition handed to the
    generator, and a prohibition that only appears when the label mentioned
    CYP2D6 is absent precisely when it is needed.
    """
    design = (design_key or "crossover").strip().lower()
    is_parallel = design == "parallel"

    text = " ".join((label or {}).get(k, "") for k in (
        "pharmacokinetics", "clinical_pharmacology", "indications_usage", "title",
    ))
    detected = _detect(text)
    if detected & set(_ANCESTRY_TRIGGERS):
        detected.add("ancestry")

    # Sex and body size are recommended for every study: both are universal at a
    # fixed dose, cost nothing to balance, and are the covariates a reviewer
    # expects to see addressed.
    always = {"sex", "body_size"}

    recommended_keys = (detected | always)

    # Modality gate. Mirrors services/safety_gating.py::recommend_safety_gating:
    # what is withheld is COLLECTED and reported, never silently dropped, so a
    # reviewer reading the protocol package can see that (for example) smoking
    # status was considered and deliberately excluded rather than forgotten.
    #
    # EVERY small-molecule-only factor is disclosed, not merely the ones this
    # label happened to cue. This list is not just an audit trail: it is the text
    # stratification_directive() hands the generator as an explicit prohibition,
    # and a prohibition conditioned on the label mentioning CYP2D6 is missing
    # exactly when it is most needed. A clean monoclonal-antibody label names no
    # polymorphic CYP, no transporter and no acid suppressant, so a
    # cue-conditioned list would be EMPTY for the very product the gate exists
    # for, leaving the model free to reach for the oral small-molecule template
    # it has seen a thousand times and to reintroduce a non-smoker requirement
    # nothing had forbidden. The audit distinction is kept per entry instead, in
    # `would_have_been_recommended`.
    suppressed: list[dict[str, Any]] = []
    if is_biologic:
        suppressed = [
            {
                "key": f.key,
                "label": f.label,
                "reason": _SMALL_MOLECULE_ONLY[f.key],
                # True when the label text positively cued this factor, i.e. the
                # gate prevented a row that would really have been printed.
                "would_have_been_recommended": f.key in recommended_keys,
            }
            for f in _CATALOGUE
            if f.key in _SMALL_MOLECULE_ONLY
        ]
        recommended_keys = recommended_keys - set(_SMALL_MOLECULE_ONLY)

    factors: list[dict[str, Any]] = []
    considered: list[dict[str, Any]] = []

    for f in _CATALOGUE:
        # A suppressed factor leaves `considered` too, not just `factors`. The
        # WHOLE result dict is handed to the generation prompt (ai_service passes
        # the `stratification` context object through verbatim), so a catalogue
        # entry left behind with recommended=False still gives the model the
        # CYP1A2 rationale and, worse, the "Inclusion requires non-smoker status"
        # eligibility note - which is exactly the boilerplate this gate removes.
        # Suppressing the factor while leaving its text somewhere else downstream
        # is a mistake this codebase has already made twice.
        if is_biologic and f.key in _SMALL_MOLECULE_ONLY:
            continue
        applies = f.key in recommended_keys
        narrowed = healthy_volunteers and f.key in _ELIGIBILITY_NARROWED
        effect = _effect_wording(f.key, is_parallel)
        note = ""
        if narrowed:
            note = _ELIGIBILITY_NARROWED[f.key] + (
                " — residual variation within the permitted range still applies."
            )
        # A factor that SURVIVES the modality gate still has to argue from a
        # mechanism the product actually has - see _BIOLOGIC_FIELD_OVERRIDES.
        overrides = _BIOLOGIC_FIELD_OVERRIDES.get(f.key, {}) if is_biologic else {}
        enriched = StratificationFactor(
            **{**asdict(f),
               **overrides,
               "recommended": applies,
               "effect": effect,
               "narrowed_by_eligibility": narrowed,
               "note": note},
        ).to_dict()
        considered.append(enriched)
        if applies:
            factors.append(enriched)

    # Tier first (largest PK impact), then catalogue order for stability.
    order = {f.key: i for i, f in enumerate(_CATALOGUE)}
    factors.sort(key=lambda d: (d["tier"], order[d["key"]]))

    return {
        "design_key": design,
        "between_subject_relevant": is_parallel,
        "is_biologic": bool(is_biologic),
        "summary": _summary(
            design, is_parallel, factors, is_nti, cv_intra_pct, bool(is_biologic)),
        "factors": factors,
        "considered": considered,
        # Withheld on modality grounds, with the mechanistic reason. Exposed
        # rather than silently dropped, exactly as safety_gating exposes
        # `suppressed_rules`, so the exclusion is auditable.
        "suppressed_factors": suppressed,
    }


def _effect_wording(key: str, is_parallel: bool) -> str:
    """What stratifying on this factor actually buys, given the design.

    Kept honest: in a crossover the subject is their own control, so the payoff
    is balance/interpretability, NOT a narrower confidence interval.
    """
    if is_parallel:
        return (
            "Balances the covariate across treatment arms, removing it from the "
            "between-subject error term that sets the confidence interval."
        )
    return (
        "Balances the covariate across treatment SEQUENCES so it cannot be "
        "confounded with a sequence or period effect; the within-subject "
        "comparison itself is already controlled by the crossover."
    )


def _summary(
    design: str,
    is_parallel: bool,
    factors: list[dict[str, Any]],
    is_nti: Optional[bool],
    cv_intra_pct: Optional[float],
    modality_excluded: bool = False,
) -> str:
    """One-paragraph plain statement for the protocol narrative."""
    top = [f["label"] for f in factors[:3]]
    lead = (
        f"In this {design} design the treatment comparison is "
        + ("between subjects, so subject characteristics enter the error term "
           "directly and covariate balance materially affects precision."
           if is_parallel else
           "within subject, so each participant serves as their own control and "
           "between-subject characteristics largely cancel from the treatment "
           "contrast; stratification therefore protects against confounding with "
           "sequence and period effects rather than narrowing the confidence "
           "interval.")
    )
    stakes = ""
    if is_nti:
        stakes = (
            " Because this is a narrow-therapeutic-index drug, the acceptance limits "
            "are tightened and imbalance is correspondingly less tolerable."
        )
    elif cv_intra_pct and cv_intra_pct >= 30.0:
        stakes = (
            f" With a within-subject CV of {cv_intra_pct:.1f}% this is a highly "
            "variable drug, so any avoidable added variability is worth removing."
        )
    tail = f" The factors carried forward are: {'; '.join(top)}." if top else ""
    # Deliberately generic. This paragraph IS protocol prose, so it must not name
    # the excluded factors: writing "smoking status was excluded" in the one
    # paragraph every reader reads puts the non-smoker question straight back
    # into the document the exclusion exists to keep it out of. The named list,
    # with its mechanism, lives in `suppressed_factors` where a reviewer audits
    # it and a subject never sees it.
    modality = (
        " Factors whose underlying mechanism does not exist for a therapeutic "
        "protein - and which would otherwise narrow eligibility for no reason - "
        "were considered and excluded from the table above."
        if modality_excluded else ""
    )
    return lead + stakes + tail + modality


def stratification_directive(strat: dict[str, Any]) -> str:
    """Render the locked stratification facts as prompt text for the generator.

    Mirrors ``design_decisions.design_directive``: the model is told these are
    settled facts to narrate, never a menu to re-decide.
    """
    if not strat:
        return ""
    factors = strat.get("factors") or []
    suppressed = strat.get("suppressed_factors") or []
    # A modality exclusion is worth stating even in the (unreachable in practice,
    # since sex and body size always survive) case of an empty factor list: the
    # prohibition is the point, not the table.
    if not factors and not suppressed:
        return ""
    lines = [
        "STRATIFICATION AT RANDOMIZATION — these factors are ALREADY DETERMINED. "
        "Narrate them; do not add, drop, or re-rank them, and do not invent a "
        "magnitude that is not given here.",
        f"Design context: {strat.get('summary', '')}",
    ]
    # Named here ONLY to forbid them. Mirrors ddi_directive, which names
    # grapefruit for the same reason: an instruction to omit something is more
    # reliable than hoping the model does not reach for the oral small-molecule
    # template it has seen a thousand times.
    #
    # Leaving a suppressed factor out of the prompt is NOT equivalent to
    # forbidding it. Silence is what produced the reviewed canakinumab protocol:
    # nothing in the context mentioned smoking, and the model supplied a
    # non-smoker eligibility requirement anyway, because that is what a BE
    # protocol looks like in its training data. The exclusion therefore has to
    # arrive as an instruction, with the mechanism attached so it reads as a
    # clinical judgement rather than an arbitrary ban.
    if suppressed:
        lines.append(
            "MODALITY - THERAPEUTIC PROTEIN: this product is a biologic, "
            "catabolised to amino acids by proteolysis rather than metabolised by "
            "cytochrome P450 enzymes. The factors below were CONSIDERED and "
            "DELIBERATELY EXCLUDED because the mechanism that would make them "
            "matter does not exist for such a product. Do NOT list them as "
            "stratification factors, do NOT discuss them anywhere in the "
            "protocol, and do NOT derive any eligibility, restriction, "
            "prohibition or washout criterion from them:"
        )
        for s in suppressed:
            lines.append(f"  - EXCLUDED: {s.get('label', '')} - {s.get('reason', '')}")
        # The concrete forms the excluded factors take when they leak. Each of
        # these is a real subject restriction that costs recruitable volunteers
        # for a mechanism this product does not have, which is exactly the
        # "subject restrictions are too much for a biologic" finding.
        lines.append(
            "  In particular: do NOT require non-smoker status or exclude smokers, "
            "do NOT impose any metabolizer- or transporter-genotype screening, "
            "stratification or exclusion, do NOT restrict proton-pump inhibitors, "
            "H2-receptor antagonists or antacids on absorption grounds, and do NOT "
            "stratify or restrict by race or ethnicity on pharmacogenetic grounds. "
            "Keep the subject restrictions and eligibility criteria limited to what "
            "is applicable to THIS product."
        )
    if factors:
        lines.append("Factors:")
        for i, f in enumerate(factors, 1):
            line = (
                f"  {i}. {f['label']} (tier {f['tier']}; {f['magnitude']}). "
                f"Rationale: {f['rationale']} Effect of stratifying: {f['effect']}"
            )
            if f.get("note"):
                line += f" Note: {f['note']}"
            lines.append(line)
        # Scoped deliberately to stratification-derived criteria: the medication,
        # washout and safety criteria are owned by other engines (ddi_screening,
        # safety_gating), and a blanket "add no eligibility criterion" here would
        # tell the model to drop theirs.
        lines.append(
            "This factor list is COMPLETE for this product: do not add a "
            "stratification factor, and do not derive a subject restriction or "
            "eligibility criterion from any subject characteristic that is not "
            "listed above."
        )
    return "\n".join(lines)
