"""Patient-population (indication) selection for a biosimilar PK-similarity study.

WHY THIS EXISTS
---------------
When a biosimilar PK study cannot be run in healthy volunteers (see
services/design_decisions.py's population rule — cytotoxic or highly
immunogenic products), it must be run in PATIENTS. But "patients" is not an
answer: the reference product usually carries several licensed indications, and
they are NOT interchangeable for a PK study. The choice of indication decides
how much of the observed exposure variability is drug and how much is noise,
and therefore decides whether the study can detect a real difference at all.

The objective, stated by the clinical pharmacologist this engine encodes, is to
pick the population that is the CLEANEST and MOST SENSITIVE for evaluating PK —
controlling patient factors that inflate PK variability and controlling
concomitant medications that perturb exposure — while still being recruitable.

THE FIVE RULES, IN PRIORITY ORDER
---------------------------------
  1. ON LABEL. The indication must be a licensed indication of the reference
     product. This is a GATE, not a preference: a PK study run in an
     unlicensed indication is not comparing the products under the conditions
     either is approved for. Grounded in the real DailyMed
     ``indications_usage`` text when available, never assumed.
  2. NAIVE. Subjects must never have been exposed to the test drug OR to its
     drug class. Pre-existing anti-drug antibodies alter clearance directly
     (an ADA-positive subject can have several-fold lower exposure), so prior
     exposure is a PK confounder, not merely a safety one. An indication whose
     standard of care means most patients are already on a biologic of the
     same class is therefore a poor choice even when it is on label.
  3. MONOTHERAPY. An indication where the product is licensed as monotherapy
     is ideal: no background agent can alter absorption, distribution,
     metabolism, or clearance, and no background agent's own toxicity confounds
     the safety comparison. Rule 2 (naive) and rule 3 (monotherapy) together
     are what actually deliver the "clean" population.
  4. LOWEST PK VARIABILITY. Where monotherapy is not available for any licensed
     indication, choose the indication with the lowest observed PK variability
     in prior trials. Because required N scales with the SQUARE of CV, this is
     not a marginal preference — it can halve or double the study. Sourced from
     the study-level meta-analysis (services/meta_analysis.py) when the
     retrieved literature actually reports it, and clearly flagged as a
     qualitative prior when it does not.
  5. RECRUITABILITY. A smaller eligible population lengthens recruitment. This
     is the LAST tiebreaker deliberately: a fast study in a noisy population is
     a failed study, so recruitment never overrides rules 1-4. It breaks ties
     between otherwise equivalent candidates.

Rules 1-3 are largely categorical and are applied as a lexicographic ranking;
rules 4-5 order what remains. Every candidate carries the reason it was ranked
where it was, so the protocol can defend the choice rather than assert it.

Follows the deterministic-engine pattern of services/stratification.py and
services/meta_analysis.py: Python computes the selection, the model may only
narrate it, never re-derive or substitute its own.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Indication knowledge base
# ---------------------------------------------------------------------------
#
# Curated per molecule, because "is this indication monotherapy?" is a fact
# about a SPECIFIC product's licence, not about the disease. Adalimumab in
# plaque psoriasis is monotherapy; infliximab in rheumatoid arthritis is
# licensed only WITH methotrexate. The same disease name therefore scores
# differently for different molecules, and a disease-keyed KB would be wrong.
#
# monotherapy:
#   "yes"      - licensed and routinely used without a background agent
#   "possible" - licensed as monotherapy but combination is common practice
#   "no"       - the licence requires (or practice mandates) a background agent
# pk_variability: qualitative prior, "low" | "moderate" | "high". Superseded by
#   real per-study CV from the meta-analysis whenever that is available.
# recruitment: "easy" | "moderate" | "difficult" (eligible-pool size + site density)
# naive_feasible: can a biologic-naive cohort realistically be enrolled in this
#   indication, given its standard of care?

@dataclass(frozen=True)
class IndicationProfile:
    name: str
    monotherapy: str = "possible"
    monotherapy_note: str = ""
    pk_variability: str = "moderate"
    pk_variability_note: str = ""
    recruitment: str = "moderate"
    recruitment_note: str = ""
    naive_feasible: bool = True
    naive_note: str = ""
    # Regex alternation matched (case-insensitively) against the real label's
    # INDICATIONS AND USAGE text to confirm rule 1 rather than assume it.
    label_cues: tuple[str, ...] = ()


def _ind(
    name: str, monotherapy: str, mono_note: str, pk_var: str, pk_note: str,
    recruit: str, recruit_note: str, naive_ok: bool, naive_note: str,
    cues: tuple[str, ...],
) -> IndicationProfile:
    return IndicationProfile(
        name=name, monotherapy=monotherapy, monotherapy_note=mono_note,
        pk_variability=pk_var, pk_variability_note=pk_note,
        recruitment=recruit, recruitment_note=recruit_note,
        naive_feasible=naive_ok, naive_note=naive_note, label_cues=cues,
    )


# Shared cue sets, so the same disease is detected consistently across molecules.
_CUE_PSORIASIS = (r"plaque psoriasis", r"\bpsoriasis\b")
_CUE_RA = (r"rheumatoid arthritis", r"\bRA\b")
_CUE_PSA = (r"psoriatic arthritis",)
_CUE_AS = (r"ankylosing spondylitis", r"axial spondyloarthritis")
_CUE_CD = (r"Crohn['’]?s disease",)  # tolerate the Unicode right single quote (U+2019) real SPL text commonly uses
_CUE_UC = (r"ulcerative colitis",)
_CUE_BC = (r"breast cancer",)
_CUE_CRC = (r"colorectal cancer", r"\bmCRC\b")
_CUE_NSCLC = (r"non-?small cell lung", r"\bNSCLC\b")
_CUE_NHL = (r"non-?Hodgkin", r"follicular lymphoma", r"\bNHL\b")
_CUE_OSTEO = (r"osteoporosis",)
_CUE_AMD = (r"macular degeneration", r"\bAMD\b")
_CUE_ASTHMA = (r"\basthma\b",)
_CUE_CSU = (r"chronic idiopathic urticaria", r"chronic spontaneous urticaria", r"urticaria")


_MONO_CLEAN = "Licensed as monotherapy — no background agent can perturb exposure or confound safety."
_MONO_MTX = "Licence/practice requires background methotrexate, which is itself immunomodulatory and alters ADA formation and therefore clearance."
_MONO_CHEMO = "Given with cytotoxic chemotherapy, which perturbs both exposure and the safety comparison."

_NAIVE_FIRSTLINE = "Biologic-naive patients are routinely available — this is a first-line biologic indication."
_NAIVE_LATELINE = "Most patients have already failed one or more biologics of this class, so a truly naive cohort is hard to assemble."

_INDICATION_KB: dict[str, tuple[IndicationProfile, ...]] = {
    "adalimumab": (
        _ind("Moderate-to-severe chronic plaque psoriasis", "yes", _MONO_CLEAN,
             "low", "Dermatology cohorts are otherwise healthy adults with stable weight and no organ impairment, giving the tightest exposure distribution of this molecule's indications.",
             "easy", "Large prevalent population with dense dermatology site coverage.",
             True, _NAIVE_FIRSTLINE, _CUE_PSORIASIS),
        _ind("Rheumatoid arthritis", "no", _MONO_MTX,
             "high", "Background methotrexate suppresses immunogenicity variably between subjects, so clearance varies with a co-medication rather than with the product.",
             "easy", "Very large population, but confounded by background therapy.",
             True, _NAIVE_FIRSTLINE, _CUE_RA),
        _ind("Psoriatic arthritis", "possible", "Licensed with or without methotrexate; combination is common.",
             "moderate", "Mixed mono/combination use widens the exposure distribution.",
             "moderate", "Moderate prevalence.", True, _NAIVE_FIRSTLINE, _CUE_PSA),
        _ind("Ankylosing spondylitis", "yes", _MONO_CLEAN,
             "moderate", "Typically monotherapy, but a smaller and more variable population than plaque psoriasis.",
             "moderate", "Smaller eligible pool than psoriasis or RA.", True, _NAIVE_FIRSTLINE, _CUE_AS),
        _ind("Crohn's disease", "possible", "Often combined with an immunomodulator in practice.",
             "high", "Active inflammatory bowel disease causes protein-losing enteropathy and hypoalbuminaemia, which materially increase monoclonal-antibody clearance and inflate between-subject variability.",
             "moderate", "Specialist centres only.", True, _NAIVE_FIRSTLINE, _CUE_CD),
        _ind("Ulcerative colitis", "possible", "Often combined with an immunomodulator in practice.",
             "high", "Same protein-loss / hypoalbuminaemia clearance confound as Crohn's disease.",
             "moderate", "Specialist centres only.", True, _NAIVE_FIRSTLINE, _CUE_UC),
    ),
    "etanercept": (
        _ind("Moderate-to-severe chronic plaque psoriasis", "yes", _MONO_CLEAN,
             "low", "Otherwise-healthy dermatology cohort; the cleanest exposure distribution for this molecule.",
             "easy", "Large prevalent population, dense site coverage.", True, _NAIVE_FIRSTLINE, _CUE_PSORIASIS),
        _ind("Rheumatoid arthritis", "possible", "Licensed with or without methotrexate.",
             "moderate", "Variable background methotrexate use widens the exposure distribution.",
             "easy", "Very large population.", True, _NAIVE_FIRSTLINE, _CUE_RA),
        _ind("Ankylosing spondylitis", "yes", _MONO_CLEAN, "moderate",
             "Typically monotherapy; smaller population than psoriasis.",
             "moderate", "Smaller eligible pool.", True, _NAIVE_FIRSTLINE, _CUE_AS),
    ),
    "infliximab": (
        _ind("Rheumatoid arthritis", "no", _MONO_MTX,
             "high", "Licence mandates concurrent methotrexate, so exposure cannot be observed free of a co-medication that alters immunogenicity and clearance.",
             "easy", "Large population.", True, _NAIVE_FIRSTLINE, _CUE_RA),
        _ind("Ankylosing spondylitis", "yes", _MONO_CLEAN,
             "moderate", "Monotherapy is licensed here, which removes the methotrexate confound that dominates the RA indication.",
             "moderate", "Smaller eligible pool than RA.", True, _NAIVE_FIRSTLINE, _CUE_AS),
        _ind("Plaque psoriasis", "yes", _MONO_CLEAN,
             "low", "Monotherapy in an otherwise-healthy dermatology cohort.",
             "moderate", "Fewer patients receive infliximab for psoriasis than adalimumab.",
             True, _NAIVE_FIRSTLINE, _CUE_PSORIASIS),
        _ind("Crohn's disease", "possible", "Frequently combined with an immunomodulator.",
             "high", "Protein-losing enteropathy and hypoalbuminaemia increase clearance and inflate variability.",
             "moderate", "Specialist centres.", True, _NAIVE_FIRSTLINE, _CUE_CD),
        _ind("Ulcerative colitis", "possible", "Frequently combined with an immunomodulator.",
             "high", "Same protein-loss clearance confound as Crohn's disease.",
             "moderate", "Specialist centres.", True, _NAIVE_FIRSTLINE, _CUE_UC),
    ),
    "rituximab": (
        _ind("Rheumatoid arthritis", "no", _MONO_MTX,
             "moderate", "Licensed in combination with methotrexate.",
             "moderate", "Second-line after TNF failure, so the pool is narrower.",
             True, "Rituximab-naive RA patients are available, though many have had a different-class biologic.",
             _CUE_RA),
        _ind("Follicular / non-Hodgkin lymphoma", "possible",
             "Monotherapy is licensed in the maintenance and relapsed settings, but induction is usually with chemotherapy.",
             "high", "Tumour burden acts as an antigen sink that alters clearance, and it varies widely between patients.",
             "difficult", "Oncology recruitment competes with active treatment trials.",
             False, _NAIVE_LATELINE, _CUE_NHL),
    ),
    "trastuzumab": (
        _ind("HER2-positive metastatic breast cancer", "possible",
             "Monotherapy is licensed after prior chemotherapy, but combination use predominates.",
             "high", "Tumour burden acts as an antigen sink and varies widely; concurrent chemotherapy adds further variability.",
             "difficult", "Competing oncology trials and rapid standard-of-care change.",
             False, _NAIVE_LATELINE, _CUE_BC),
    ),
    "bevacizumab": (
        _ind("Metastatic colorectal cancer", "no", _MONO_CHEMO,
             "high", "Always given with chemotherapy; tumour burden and chemotherapy both perturb exposure.",
             "difficult", "Oncology recruitment.", False, _NAIVE_LATELINE, _CUE_CRC),
        _ind("Non-small cell lung cancer", "no", _MONO_CHEMO,
             "high", "Always given with chemotherapy.", "difficult", "Oncology recruitment.",
             False, _NAIVE_LATELINE, _CUE_NSCLC),
    ),
    "denosumab": (
        _ind("Postmenopausal osteoporosis", "yes", _MONO_CLEAN,
             "low", "A stable, ambulatory, otherwise-healthy population with no tumour sink and no background biologic — among the cleanest PK populations available for any monoclonal antibody.",
             "easy", "Very large prevalent population, recruited through primary care and endocrinology.",
             True, _NAIVE_FIRSTLINE, _CUE_OSTEO),
    ),
    "tocilizumab": (
        _ind("Rheumatoid arthritis", "possible",
             "Monotherapy is explicitly licensed for tocilizumab, unlike most TNF inhibitors — the monotherapy arm should be selected.",
             "moderate", "Interleukin-6 suppression normalises CYP activity and albumin, so exposure is more stable off methotrexate.",
             "easy", "Large population.", True, _NAIVE_FIRSTLINE, _CUE_RA),
    ),
    "omalizumab": (
        _ind("Chronic spontaneous (idiopathic) urticaria", "yes", _MONO_CLEAN,
             "low", "Dosing is fixed (not weight/IgE-adjusted) in urticaria, removing the dose-normalisation variability that dominates the asthma indication.",
             "moderate", "Moderate prevalence, dermatology and allergy centres.",
             True, _NAIVE_FIRSTLINE, _CUE_CSU),
        _ind("Moderate-to-severe persistent asthma", "no",
             "Given on a background of inhaled corticosteroids and other controllers.",
             "high", "Dose is adjusted by body weight AND baseline IgE, so exposure varies by design rather than by product.",
             "easy", "Large population.", True, _NAIVE_FIRSTLINE, _CUE_ASTHMA),
    ),
    "ranibizumab": (
        _ind("Neovascular (wet) age-related macular degeneration", "yes", _MONO_CLEAN,
             "moderate", "Intravitreal dosing gives low and variable systemic exposure; the systemic PK comparison is inherently imprecise.",
             "easy", "Large population, dense retina-clinic coverage.",
             True, _NAIVE_FIRSTLINE, _CUE_AMD),
    ),
    "ustekinumab": (
        _ind("Moderate-to-severe chronic plaque psoriasis", "yes", _MONO_CLEAN,
             "low", "Otherwise-healthy dermatology cohort with weight-band dosing.",
             "easy", "Large prevalent population.", True, _NAIVE_FIRSTLINE, _CUE_PSORIASIS),
        _ind("Crohn's disease", "possible", "Often combined with an immunomodulator.",
             "high", "Protein-losing enteropathy increases clearance and inflates variability.",
             "moderate", "Specialist centres.", True, _NAIVE_FIRSTLINE, _CUE_CD),
    ),
}


# Class-level fallback so an uncurated molecule still gets a defensible,
# clearly-flagged starting point instead of nothing. Used ONLY when the label
# text yielded no generic candidates either (see _generic_candidates_from_label
# below) — e.g. no label text was retrievable at all.
_GENERIC_MAB_FALLBACK = IndicationProfile(
    name="Principal labelled indication (to be confirmed against the reference product's label)",
    monotherapy="possible",
    monotherapy_note="Monotherapy status could not be established from the curated knowledge base — confirm against the reference product's licence and prefer a monotherapy indication if one exists.",
    pk_variability="unknown",
    pk_variability_note="No indication-level PK variability is available for this product; the meta-analysis CV is used as-is and the assumption must be stated.",
    recruitment="moderate",
    recruitment_note="Recruitability not established for this product.",
    naive_feasible=True,
    naive_note="A biologic-naive cohort is assumed feasible; confirm against the indication's standard of care.",
)

# (cues, generic disease name) pairs, reusing the SAME cue tuples the curated
# per-molecule knowledge base above is built from — many real biosimilar
# molecules target the SAME diseases as the curated ones (e.g. golimumab, like
# adalimumab/etanercept, is licensed in RA/PsA/AS/UC) even though this module
# has no per-molecule monotherapy/CV data for them specifically.
_GENERIC_DISEASE_CUES: tuple[tuple[tuple[str, ...], str], ...] = (
    (_CUE_PSORIASIS, "Moderate-to-severe chronic plaque psoriasis"),
    (_CUE_RA, "Rheumatoid arthritis"),
    (_CUE_PSA, "Psoriatic arthritis"),
    (_CUE_AS, "Ankylosing spondylitis"),
    (_CUE_CD, "Crohn's disease"),
    (_CUE_UC, "Ulcerative colitis"),
    (_CUE_BC, "HER2-positive metastatic breast cancer"),
    (_CUE_CRC, "Metastatic colorectal cancer"),
    (_CUE_NSCLC, "Non-small cell lung cancer"),
    (_CUE_NHL, "Follicular / non-Hodgkin lymphoma"),
    (_CUE_OSTEO, "Postmenopausal osteoporosis"),
    (_CUE_AMD, "Neovascular (wet) age-related macular degeneration"),
    (_CUE_ASTHMA, "Moderate-to-severe persistent asthma"),
    (_CUE_CSU, "Chronic spontaneous (idiopathic) urticaria"),
)


def _generic_candidates_from_label(label_text: str) -> tuple[IndicationProfile, ...]:
    """Indications actually mentioned in the retrieved label, for a molecule
    with no per-molecule curated entry in ``_INDICATION_KB``.

    Fixes a real gap: the KB covers 10 molecules; every other real biosimilar
    target (golimumab, certolizumab, filgrastim, pegfilgrastim, epoetin alfa,
    insulin glargine, aflibercept, natalizumab, every US biosimilar-suffixed
    INN such as "adalimumab-atto") fell through to a single fixed placeholder
    that the retrieved label text played no part in at all — even when that
    text plainly listed real, checkable indications. This is grounded in the
    ACTUAL label (so ``on_label`` is genuinely True by construction, not
    assumed), but has NO per-molecule monotherapy/CV/recruitment data behind
    it, so those fields are conservative, clearly-flagged unknowns rather than
    invented specifics — the honest middle ground between "nothing" and
    "confidently wrong".
    """
    if not label_text:
        return ()
    found: list[IndicationProfile] = []
    for cues, name in _GENERIC_DISEASE_CUES:
        if _cues_confirmed_in_text(cues, label_text):
            found.append(_ind(
                name, "possible",
                "Monotherapy status is not curated for this molecule — confirm against the "
                "reference product's licence and prefer a monotherapy arm if one exists.",
                "unknown",
                "No molecule-specific PK-variability data is curated for this indication; "
                "rely on the meta-analysis CV (if retrievable) rather than this qualitative tier.",
                "moderate", "Recruitability is not curated for this molecule.",
                True,
                "Assumed feasible; confirm against this indication's actual standard of care "
                "for this molecule.",
                cues,
            ))
    return tuple(found)


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------

# Lexicographic ranks. Lower sorts better. The ordering below IS the rule
# priority: monotherapy (rule 3) outranks PK variability (rule 4), which
# outranks recruitment (rule 5), because a clean population is worth more than
# a fast one. Naive-feasibility (rule 2) is folded in ahead of monotherapy,
# since prior exposure is a direct clearance confound (anti-drug antibodies),
# not merely a background-therapy one.
_MONO_RANK = {"yes": 0, "possible": 1, "no": 2}
_PKVAR_RANK = {"low": 0, "moderate": 1, "unknown": 2, "high": 3}
_RECRUIT_RANK = {"easy": 0, "moderate": 1, "difficult": 2}

# A candidate with NO measured CV is scored as if its variability were this
# value — comfortably above any realistic MEASURED figure this module's own
# knowledge base uses (12-45%), but below a genuinely poor one. Purely a
# ranking placeholder, never shown to the user.
#
# WHY a fixed sentinel rather than "no data always beats/loses to measured
# data": a candidate's qualitative pk_variability tier ("low"/"moderate"/...)
# is a curated prior, not a measurement, so it must not be able to
# systematically outrank a WORSE-tier candidate's actual measured number
# (that inversion — one incidentally-retrieved study, however bad, silently
# overriding every unmeasured candidate regardless of the measured value —
# was a confirmed bug this sentinel design fixes). But it also must not
# systematically LOSE to an ordinary, unremarkable measured value just
# because a number exists (a candidate whose only literature hit reports a
# perfectly typical 25% CV should not be penalised relative to an unmeasured
# candidate the KB otherwise prefers). A sentinel comfortably above ordinary
# measured values threads both: it only loses to a measurement that is
# GENUINELY high, and only wins against an unmeasured competitor at the same
# monotherapy tier when there is nothing better to prefer.
_NO_DATA_CV_SENTINEL = 60.0


def _profiles_for(drug_inn: str) -> tuple[IndicationProfile, ...]:
    key = (drug_inn or "").strip().lower()
    return _INDICATION_KB.get(key, ())


# Sentence-local negation markers. Not a general-purpose negation detector —
# just the specific, common label phrasing that flips a cue match's meaning:
# "NOT indicated for X", "not approved/recommended/licensed for X", a
# "Limitations of Use" carve-out, or an exclusion ("other than X", "except
# for X", "excludes patients with X"). Deliberately NARROWER than a bare
# "\bnot\b|\bno\b": an earlier version used that catch-all and produced a
# live false-negative on ordinary label phrasing describing patient history
# rather than negating the indication itself — "...who have NOT responded
# adequately to conventional DMARD therapy" tripped \bnot\b even though
# nothing about the indication is being negated, incorrectly flipping a
# genuinely on-label indication to unconfirmed. Anchoring "not"/"no" to the
# specific verbs that actually negate licensure (indicated/approved/
# recommended/licensed) keeps the real carve-out patterns while dropping
# that false trigger.
_NEGATION_CUE = re.compile(
    r"\bnot\s+(?:be\s+)?(?:indicated|approved|recommended|licensed)\b|"
    r"\bno\s+indication\b|exclud|contraindicat|other than|except|apart from|"
    r"limitations? of use",
    re.I,
)


def _cues_confirmed_in_text(cues: tuple[str, ...], text: str) -> bool:
    """True when at least one CUE appears in ``text`` outside a negated
    sentence. A bare substring/regex presence test has no negation handling:
    a real label sentence like "Limitations of Use: XYZ is NOT indicated for
    ulcerative colitis" contains the cue "ulcerative colitis" and would flip
    on_label=True — the opposite of what the label says. Splitting into
    sentence-like chunks and requiring the matching chunk itself be
    negation-free fixes that specific, real failure mode without attempting
    full NLP negation scope resolution. A cue that is negated in one sentence
    but genuinely confirmed in another (both appear in real labels) still
    returns True, correctly.

    A COLON-INTRODUCED LIST gets special handling: "Not indicated for the
    following: giant cell arteritis; ulcerative colitis; and Behcet
    disease." is grammatically ONE negation shared across every semicolon-
    separated item after the colon — splitting purely on ';' would otherwise
    treat "ulcerative colitis;" as its own, unnegated chunk and confirm an
    explicitly excluded indication. When the clause BEFORE a colon in a
    sentence is itself negated, every cue found anywhere after that colon is
    treated as negated too, regardless of intervening semicolons. This does
    NOT extend to a bare semicolon with no colon ("Indicated for RA; not
    indicated for pediatric use.") — those remain independently-scoped
    clauses, since two clauses joined only by ';' are not reliably one
    grammatical list the way a colon-introduced one is; a semicolon-only
    negated list is a known, accepted residual gap of this heuristic."""
    if not text or not cues:
        return False
    for sentence in re.split(r"(?<=[.\n])\s+", text):
        sentence = sentence.strip()
        if not sentence:
            continue
        colon_idx = sentence.find(":")
        if colon_idx != -1 and _NEGATION_CUE.search(sentence[:colon_idx]):
            if any(re.search(cue, sentence[colon_idx + 1:], re.I) for cue in cues):
                continue  # every cue here is negated by the head clause
        for clause in re.split(r"(?<=;)\s+", sentence):
            for cue in cues:
                if re.search(cue, clause, re.I) and not _NEGATION_CUE.search(clause):
                    return True
    return False


def _label_confirms(profile: IndicationProfile, label_text: str) -> bool:
    """True when the reference product's real INDICATIONS AND USAGE text
    mentions this indication. Rule 1 is a GATE, so this is what separates
    'licensed' from 'we assumed it was licensed'."""
    return _cues_confirmed_in_text(profile.label_cues, label_text)


def _observed_cv_for(profile: IndicationProfile, meta: Optional[dict]) -> Optional[float]:
    """Rule 4, grounded: the lowest study-level CV among population-MATCHED
    retrieved studies whose population text actually matches this indication.

    LOWEST, not highest, deliberately — and this is the one place in the whole
    codebase that takes a minimum rather than a maximum CV. Everywhere else
    (services/meta_analysis.py) the maximum is correct, because once the
    population is FIXED the conservative choice is the widest variability that
    population might exhibit. Here the population is precisely what is being
    CHOSEN, and the rule is to choose the least variable one, so the comparison
    across candidate indications is on their lowest demonstrated variability.
    This value only ORDERS candidate indications against each other. The
    powering CV meta_analysis.py actually uses is a SEPARATE number — its
    max() runs over every study matching the protocol's population CLASS
    (patient vs healthy), not the specific indication selected here, so it can
    genuinely differ from this indication's own lowest observed value. That
    is disclosed explicitly (see ``cv_reconciliation_note`` on the result of
    ``select_patient_population``) rather than silently presented as the
    same number twice.

    ONLY ``studies_matching`` is searched, never the full ``studies`` list.
    ``build_meta_analysis`` already separates studies into the target
    population (patient, for this module's only caller) and everything else
    (``studies_excluded_wrong_population`` — including healthy-volunteer
    bridging studies, whose titles routinely name the indication too). Without
    this filter a healthy-volunteer CV — which meta_analysis.py itself
    deliberately excludes from POWERING for exactly this reason — would still
    be picked up here and asserted as "the lowest PK variability observed for
    this indication" in the patient population the protocol actually enrols.
    """
    if not meta:
        return None
    cues = profile.label_cues
    if not cues:
        return None
    observed: list[float] = []
    for study in meta.get("studies_matching") or []:
        cv = study.get("cv_pct")
        if cv is None:
            continue
        haystack = " ".join(str(study.get(k) or "") for k in ("population_text", "indication", "title"))
        if not haystack.strip():
            continue
        if any(re.search(cue, haystack, re.I) for cue in cues):
            observed.append(float(cv))
    return min(observed) if observed else None


@dataclass
class RankedIndication:
    name: str
    on_label: bool
    label_evidence: str
    monotherapy: str
    monotherapy_note: str
    pk_variability: str
    pk_variability_note: str
    observed_cv_pct: Optional[float]
    recruitment: str
    recruitment_note: str
    naive_feasible: bool
    naive_note: str
    rank_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "indication": self.name,
            "on_label": self.on_label,
            "label_evidence": self.label_evidence,
            "monotherapy": self.monotherapy,
            "monotherapy_note": self.monotherapy_note,
            "pk_variability": self.pk_variability,
            "pk_variability_note": self.pk_variability_note,
            "observed_cv_pct": self.observed_cv_pct,
            "recruitment": self.recruitment,
            "recruitment_note": self.recruitment_note,
            "naive_feasible": self.naive_feasible,
            "naive_note": self.naive_note,
            "rank_reason": self.rank_reason,
        }


# ---------------------------------------------------------------------------
# Eligibility rules that follow from the selection (rule 2 + the con-med aim)
# ---------------------------------------------------------------------------

def _eligibility_rules(drug_inn: str, drug_class: str) -> list[dict[str, str]]:
    """The eligibility criteria the population choice IMPLIES.

    Rule 2 is not only a selection criterion — it has to appear as an actual
    exclusion criterion in the protocol, or the chosen population is not
    delivered. Same for the concomitant-medication control that the stated
    objective ("controlling concomitant medications that may impact PK")
    requires. Emitting them here keeps the selection and the criteria that
    enforce it in ONE place, so they cannot drift apart.
    """
    # Currently dead (the only call site always passes drug_class_for()'s
    # return value, which is never empty), but kept grammatical rather than
    # "the same class" — "any other the same class" is broken regardless of
    # what follows "any other", since "the" cannot follow it — in case a
    # future caller ever passes drug_class directly.
    cls = drug_class or "biologic agent of the same class"
    return [
        {
            "key": "naive_to_product",
            "type": "exclusion",
            "criterion": (
                f"Any prior exposure, at any time, to {drug_inn}, to the reference product, "
                f"or to any biosimilar of it."
            ),
            "rationale": (
                "Prior exposure can have induced anti-drug antibodies. ADA alter clearance "
                "directly and can change exposure several-fold, so a previously-exposed "
                "subject measures their own immune history rather than the two products."
            ),
        },
        {
            "key": "naive_to_class",
            "type": "exclusion",
            # No trailing "agent" appended here on purpose: every curated class
            # name already ends in its own noun ("... inhibitor", "... agent",
            # "... antibody"), and the uncurated fallback ("biologic agent of
            # the same class") already ends in "class" — appending "agent"
            # unconditionally produced "... agent agent." for the two classes
            # already ending in "agent", and "... of the same class agent."
            # for the fallback.
            "criterion": f"Any prior exposure, at any time, to any other {cls}.",
            "rationale": (
                "Cross-reactive anti-drug antibodies and class-level target modulation both "
                "perturb clearance, so class-naivety — not only molecule-naivety — is required "
                "for a clean PK comparison."
            ),
        },
        {
            "key": "concomitant_pk_modifiers",
            "type": "restriction",
            "criterion": (
                "Prohibit initiation, discontinuation, or dose change of any concomitant "
                "medication that alters the disposition of the study product (including "
                "immunomodulators such as methotrexate or azathioprine, systemic "
                "corticosteroids above physiological replacement, and any other biologic) "
                "from screening through the end of the primary PK sampling period."
            ),
            "rationale": (
                "The objective is a population in which the only thing that differs between "
                "arms is the product. A background agent that is started, stopped, or "
                "re-dosed mid-study moves exposure independently of the comparison."
            ),
        },
        {
            "key": "stable_disease",
            "type": "inclusion",
            "criterion": (
                "Clinically stable disease at screening, with no change in disease-modifying "
                "therapy within the preceding treatment interval."
            ),
            "rationale": (
                "Acute inflammatory activity alters albumin, target expression, and "
                "monoclonal-antibody clearance; requiring stability removes that source of "
                "within-study drift."
            ),
        },
    ]


_CLASS_BY_INN: dict[str, str] = {
    "adalimumab": "TNF-alpha inhibitor",
    "etanercept": "TNF-alpha inhibitor",
    "infliximab": "TNF-alpha inhibitor",
    "golimumab": "TNF-alpha inhibitor",
    "certolizumab pegol": "TNF-alpha inhibitor",
    "rituximab": "anti-CD20 monoclonal antibody",
    "obinutuzumab": "anti-CD20 monoclonal antibody",
    "ocrelizumab": "anti-CD20 monoclonal antibody",
    "trastuzumab": "anti-HER2 monoclonal antibody",
    "bevacizumab": "anti-VEGF monoclonal antibody",
    "ranibizumab": "anti-VEGF agent",
    "aflibercept": "anti-VEGF agent",
    "denosumab": "RANK-ligand inhibitor",
    "tocilizumab": "interleukin-6 receptor antagonist",
    "ustekinumab": "interleukin-12/23 inhibitor",
    "omalizumab": "anti-IgE monoclonal antibody",
    "pembrolizumab": "PD-1 checkpoint inhibitor",
    "nivolumab": "PD-1 checkpoint inhibitor",
    "filgrastim": "granulocyte colony-stimulating factor",
    "pegfilgrastim": "granulocyte colony-stimulating factor",
    "epoetin alfa": "erythropoiesis-stimulating agent",
    "darbepoetin alfa": "erythropoiesis-stimulating agent",
}


def drug_class_for(drug_inn: str) -> str:
    """The therapeutic class used by the class-naivety exclusion (rule 2)."""
    return _CLASS_BY_INN.get((drug_inn or "").strip().lower(), "biologic agent of the same class")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def select_patient_population(
    *,
    drug_inn: str,
    study_type: str,
    subject_type: str = "healthy",
    label: Optional[dict[str, str]] = None,
    meta_analysis: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Rank the licensed indications and recommend one, applying rules 1-5.

    Returns ``{"applicable": False, ...}`` — and nothing else that could reach a
    prompt — for any study this does not govern: a healthy-volunteer study (the
    population question is already answered) and any non-biosimilar study type.
    The caller can therefore call it unconditionally.
    """
    is_patient = str(subject_type or "healthy").strip().lower() == "patient"
    is_biosimilar = str(study_type or "").strip().lower() == "biosimilar_pk"
    if not (is_patient and is_biosimilar):
        return {
            "applicable": False,
            "reason": (
                "Healthy-volunteer study — the population is already determined and no "
                "indication needs to be selected."
                if is_biosimilar else
                "Indication selection applies to patient biosimilar PK-similarity studies only."
            ),
        }

    label_text = (label or {}).get("indications_usage") or ""
    profiles = _profiles_for(drug_inn)
    drug_class = drug_class_for(drug_inn)
    assumptions: list[str] = []

    if not profiles:
        # No per-molecule curation exists (the KB covers 10 molecules; every
        # US biosimilar-suffixed INN, and most real biosimilar targets beyond
        # the curated set, land here) - prefer real candidates detected from
        # the ACTUAL retrieved label over the single uninformative placeholder,
        # whenever there is label text to detect them from.
        profiles = _generic_candidates_from_label(label_text)
        if profiles:
            assumptions.append(
                f"No curated indication set exists for {drug_inn}; the candidate indications "
                f"below were detected from the reference product's own retrieved label text, "
                f"but have no molecule-specific monotherapy/PK-variability/recruitment data "
                f"curated for them - confirm the recommended indication's monotherapy status and "
                f"expected variability against the actual label and prescribing literature before "
                f"the protocol is finalised."
            )
        else:
            profiles = (_GENERIC_MAB_FALLBACK,)
            assumptions.append(
                f"No curated indication set exists for {drug_inn}, and no candidate indication "
                "could be detected from the retrieved label text either; a single generic "
                "placeholder is returned and the indication MUST be selected and confirmed "
                "manually against the reference product's licence before the protocol is "
                "finalised."
            )
    if not label_text:
        assumptions.append(
            "The reference product's INDICATIONS AND USAGE text could not be retrieved, so "
            "rule 1 (indication must be on the product label) could not be verified "
            "programmatically for any candidate — each must be confirmed manually."
        )

    ranked: list[RankedIndication] = []
    for prof in profiles:
        confirmed = _label_confirms(prof, label_text)
        observed_cv = _observed_cv_for(prof, meta_analysis)
        ranked.append(RankedIndication(
            name=prof.name,
            on_label=confirmed,
            label_evidence=(
                "Confirmed present in the reference product's INDICATIONS AND USAGE section."
                if confirmed else
                "NOT confirmed from the retrieved label text — verify against the reference "
                "product's licence before use."
            ),
            monotherapy=prof.monotherapy,
            monotherapy_note=prof.monotherapy_note,
            pk_variability=prof.pk_variability,
            pk_variability_note=prof.pk_variability_note,
            observed_cv_pct=observed_cv,
            recruitment=prof.recruitment,
            recruitment_note=prof.recruitment_note,
            naive_feasible=prof.naive_feasible,
            naive_note=prof.naive_note,
        ))

    def _sort_key(r: RankedIndication) -> tuple:
        # Rule 4 compares a MEASURED CV and a qualitative prior on the SAME
        # numeric scale, via the tier midpoints above — a measured value is
        # NOT an unconditional override of "no data". A single incidentally-
        # retrieved study reporting a genuinely high CV (say 70%) must not
        # automatically outrank a candidate with no literature hit at all but
        # a "low" qualitative prior; comparing on one scale is what prevents
        # that (a fix for a real ranking inversion found in adversarial
        # review — the previous version put ANY measured value ahead of
        # EVERY unmeasured one, however bad the measured value was).
        effective_cv = r.observed_cv_pct if r.observed_cv_pct is not None else _NO_DATA_CV_SENTINEL
        return (
            0 if r.on_label else 1,                                   # rule 1 (gate)
            0 if r.naive_feasible else 1,                             # rule 2
            _MONO_RANK.get(r.monotherapy, 1),                         # rule 3
            effective_cv,                                             # rule 4, primary (measured vs sentinel)
            _PKVAR_RANK.get(r.pk_variability, 2),                     # rule 4, tiebreak among unmeasured
            _RECRUIT_RANK.get(r.recruitment, 1),                      # rule 5
            r.name,
        )

    ranked.sort(key=_sort_key)

    for i, r in enumerate(ranked):
        bits = []
        bits.append("on the reference product's label" if r.on_label
                    else "NOT confirmed on the label from retrieved text")
        bits.append("a biologic-naive cohort is feasible" if r.naive_feasible
                    else "a biologic-naive cohort is NOT realistically available")
        bits.append({
            "yes": "licensed as monotherapy",
            "possible": "monotherapy is possible but not the norm",
            "no": "requires a background agent",
        }.get(r.monotherapy, "monotherapy status unclear"))
        if r.observed_cv_pct is not None:
            bits.append(f"lowest observed PK CV in the retrieved literature is {r.observed_cv_pct:.1f}%")
        else:
            bits.append(f"{r.pk_variability} expected PK variability")
        bits.append(f"{r.recruitment} to recruit")
        r.rank_reason = f"Rank {i + 1}: " + "; ".join(bits) + "."

    recommended = ranked[0] if ranked else None
    rejected = ranked[1:] if len(ranked) > 1 else []

    # Rule 1 is documented as a GATE, but the sort key above only makes it the
    # first tiebreaker — with every candidate off-label, ranked[0] is still
    # whichever one wins on rules 2-5 among a field that already failed rule
    # 1. That is a materially different situation from "we couldn't verify
    # the label" (label_text empty): here the label WAS retrieved and EVERY
    # candidate was checked against it and failed. Surfaced as its own flag
    # rather than silently returning a `recommended` indication that reads
    # exactly like a genuinely on-label one.
    no_on_label_candidate = bool(label_text) and recommended is not None and not recommended.on_label

    # The number the study is ACTUALLY powered on (meta_analysis.py's own
    # max() over every population-matched study, filtered by population CLASS
    # only) can genuinely differ from the recommended indication's OWN lowest
    # observed CV above (filtered by class AND matched to this specific
    # indication's label cues, taking a min()). They answer different
    # questions on purpose — see _observed_cv_for's docstring — so silently
    # printing both without comment would read as an unexplained
    # contradiction in the same document (a Crohn's-disease study's CV could
    # be the max used for powering even when plaque psoriasis, with a lower
    # CV of its own, is the recommended and enrolled population). Disclosed
    # here rather than reconciled by narrowing the powering CV to the
    # selected indication, which would require deciding the population BEFORE
    # the meta-analysis that only exists to help select it — the same
    # ordering circularity this module's own CV-basis logic in
    # routers/protocols.py already avoids elsewhere.
    powering_cv = (meta_analysis or {}).get("powering_cv_pct")
    cv_reconciliation_note = ""
    if (
        recommended is not None
        and recommended.observed_cv_pct is not None
        and powering_cv is not None
        and abs(recommended.observed_cv_pct - powering_cv) > 0.05
    ):
        cv_reconciliation_note = (
            f"The recommended indication's own lowest observed CV ({recommended.observed_cv_pct:.1f}%) "
            f"differs from the CV this study is actually POWERED on ({powering_cv:.1f}%). This is "
            f"expected, not an error: the powering CV is the highest observed across ALL "
            f"population-matched studies (the codebase's conservative regulatory convention, applied "
            f"regardless of indication), while the figure above is specific to the recommended "
            f"indication and is used only to RANK candidate indications against each other. State "
            f"both figures for what they are; do NOT silently reconcile them into one number or treat "
            f"the difference as an inconsistency to be smoothed over."
        )
        assumptions.append(cv_reconciliation_note)

    return {
        "applicable": True,
        "drug_inn": drug_inn,
        "drug_class": drug_class,
        "label_verified": bool(label_text),
        "recommended": recommended.to_dict() if recommended else None,
        "ranked": [r.to_dict() for r in ranked],
        "rejected": [r.to_dict() for r in rejected],
        "eligibility_rules": _eligibility_rules(drug_inn, drug_class),
        "selection_rules": [
            "1. The indication must be a licensed indication of the reference product.",
            "2. Subjects must never have been exposed to the product or to any agent of its class.",
            "3. A monotherapy indication is preferred — no background agent can perturb exposure.",
            "4. Where monotherapy is unavailable, the indication with the lowest PK variability in prior trials is chosen.",
            "5. Recruitability breaks ties only; it never overrides rules 1-4.",
        ],
        "powering_cv_pct": powering_cv,
        "cv_reconciliation_note": cv_reconciliation_note,
        "no_on_label_candidate": no_on_label_candidate,
        "assumptions": assumptions,
        "summary": _summary(recommended, rejected, drug_class, bool(label_text), no_on_label_candidate),
    }


def _summary(
    recommended: Optional[RankedIndication],
    rejected: list[RankedIndication],
    drug_class: str,
    label_verified: bool,
    no_on_label_candidate: bool = False,
) -> str:
    if recommended is None:
        return (
            "No candidate indication could be established; the population must be selected "
            "manually against the reference product's licence."
        )
    if no_on_label_candidate:
        return (
            f"NO CANDIDATE INDICATION COULD BE CONFIRMED ON-LABEL: the reference product's "
            f"retrieved label text was checked against every candidate indication, and none "
            f"matched. {recommended.name} is shown below only as the best-ranked candidate on "
            f"the remaining criteria (rules 2-5) — it is NOT a licensed indication by this "
            f"check and MUST be replaced with a genuinely on-label indication, confirmed "
            f"manually against the reference product's licence, before the protocol is used."
        )
    parts = [
        f"{recommended.name} is selected as the study population. "
        + ("It is confirmed on the reference product's label. " if recommended.on_label
           else "It could NOT be confirmed against the retrieved label text and must be verified manually. ")
        + {
            "yes": "It is licensed as monotherapy, so no background agent can perturb exposure or confound the safety comparison. ",
            "possible": "Monotherapy is possible in this indication and must be required by protocol. ",
            "no": "Monotherapy is not available in this indication, so the background agent must be held constant across both arms. ",
        }.get(recommended.monotherapy, "")
    ]
    if recommended.observed_cv_pct is not None:
        parts.append(
            f"The lowest PK variability observed for this indication in the retrieved "
            f"literature is {recommended.observed_cv_pct:.1f}%."
        )
    else:
        parts.append(
            f"Expected PK variability is {recommended.pk_variability} "
            f"({recommended.pk_variability_note})"
        )
    parts.append(
        f"Subjects must be naive to the product AND to every other {drug_class}, since "
        "pre-existing anti-drug antibodies alter clearance directly."
    )
    if rejected:
        names = "; ".join(r.name for r in rejected[:3])
        parts.append(
            f"{len(rejected)} other labelled indication(s) were considered and not selected "
            f"({names}) — each is tabulated with the reason."
        )
    if not label_verified:
        parts.append(
            "NOTE: the label's indications text was not retrievable, so the on-label "
            "requirement was not verified programmatically."
        )
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Prompt directive
# ---------------------------------------------------------------------------


def patient_population_directive(result: dict[str, Any]) -> str:
    """Render the selection as LOCKED facts for the generation prompt."""
    if not result or not result.get("applicable") or not result.get("recommended"):
        return ""
    rec = result["recommended"]
    if result.get("no_on_label_candidate"):
        # Rule 1 is a GATE, and every candidate failed it against the actually
        # retrieved label — this is NOT a locked selection the model should
        # narrate with confidence, unlike every other branch of this
        # directive. Say so plainly instead of reading exactly like a
        # confirmed on-label pick.
        lines = [
            "\nPATIENT POPULATION / INDICATION - NOT YET DETERMINED. The reference product's "
            "label was retrieved and checked against every candidate indication, and NONE "
            "matched. Do NOT present the indication below as licensed or as a locked selection.",
            f"- BEST-RANKED CANDIDATE ON REMAINING CRITERIA (NOT confirmed on-label): "
            f"{rec['indication']}.",
            "- State explicitly that no on-label candidate could be confirmed and that the "
            "population must be selected and verified manually against the reference "
            "product's actual licence before the protocol is used.",
        ]
        for note in result.get("assumptions", []):
            lines.append(f"- ASSUMPTION (state it explicitly): {note}")
        return "\n".join(lines) + "\n"
    lines = [
        "\nPATIENT POPULATION / INDICATION - ALREADY SELECTED. Do NOT choose a different",
        "indication, do NOT add indications, and do NOT re-derive this selection.",
        f"- STUDY POPULATION: patients with {rec['indication']}.",
        f"- Monotherapy status: {rec['monotherapy']} - {rec['monotherapy_note']}",
    ]
    if rec.get("observed_cv_pct") is not None:
        lines.append(
            f"- Lowest PK variability observed for this indication in the retrieved "
            f"literature: {rec['observed_cv_pct']:.1f}%. This figure is what RANKED the "
            f"candidate indications; it is NOT necessarily the CV this study is powered on "
            f"(see the meta-analysis appendix / Sample Size Determination for that figure, "
            f"which is the conservative maximum across ALL population-matched studies)."
        )
    else:
        lines.append(f"- Expected PK variability: {rec['pk_variability']} - {rec['pk_variability_note']}")
    if result.get("cv_reconciliation_note"):
        lines.append(f"- {result['cv_reconciliation_note']}")
    lines.append(f"- Recruitability: {rec['recruitment']} - {rec['recruitment_note']}")
    if not rec.get("on_label"):
        lines.append(
            "- The on-label status of this indication was NOT confirmed from the retrieved "
            "label text. State plainly that it requires confirmation against the reference "
            "product's licence; do NOT assert it is licensed."
        )

    lines.append("- The eligibility criteria below are REQUIRED and must appear in the")
    lines.append("  population/eligibility section, worded as criteria, not as discussion:")
    for rule in result.get("eligibility_rules", []):
        lines.append(f"    [{rule['type'].upper()}] {rule['criterion']}")
        lines.append(f"        Why: {rule['rationale']}")

    rejected = result.get("rejected") or []
    if rejected:
        lines.append(
            "- Other labelled indications were REVIEWED AND DELIBERATELY NOT SELECTED. "
            "Present this as a considered choice with the reason, not as an omission:"
        )
        for r in rejected:
            lines.append(f"    {r['indication']} - {r['rank_reason']}")

    lines.append("- Selection rules applied, in this priority order:")
    for rule in result.get("selection_rules", []):
        lines.append(f"    {rule}")

    for note in result.get("assumptions", []):
        lines.append(f"- ASSUMPTION (state it explicitly): {note}")

    return "\n".join(lines) + "\n"
