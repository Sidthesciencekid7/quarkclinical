"""Drug-specific prohibited / restricted concomitant medication screening.

WHY THIS MODULE EXISTS
----------------------
A bioequivalence or PK-similarity study measures ONE thing: the exposure ratio
between two products of the same molecule. Anything that perturbs the victim
drug's clearance during the study perturbs that ratio directly, and a
concomitant medication is by far the most common such perturbation. So the
protocol's prohibited-medication list is not boilerplate - it is part of the
measurement.

The list has to be DRUG-SPECIFIC. A generic "no CYP3A4 inhibitors or inducers"
clause is worse than useless for a CYP2D6 substrate: it prohibits agents that
cannot affect the analyte while permitting paroxetine, quinidine and terbinafine,
which can raise its exposure severalfold. This module therefore reads the
product's REAL DailyMed label - including the DRUG INTERACTIONS (LOINC 34073-7)
and CONTRAINDICATIONS (34070-3) sections that :mod:`services.literature_service`
now fetches for exactly this purpose - decides which clearance pathways are
actually in play, and emits only the classes that matter for THIS molecule.

Like :mod:`services.stratification` (covariate balance) and
:mod:`services.biostatistics` (sample size), everything here is COMPUTED, never
modelled: the pathway call, the agent list, the washout arithmetic and the
provenance of each entry are all produced in Python and injected into the
generation prompt as locked facts the model may narrate but not re-derive.

THE PHARMACOLOGY THAT MAKES THE NUMBERS DEFENSIBLE
--------------------------------------------------
The single most-often-botched number in a protocol is the washout period for a
prohibited comedication, because inhibition and induction recover by completely
different physics and a flat "14 days for everything" is wrong in both
directions:

* **Reversible (competitive) inhibition** - the effect disappears as the
  PERPETRATOR itself is eliminated. Washout is therefore governed by the
  inhibitor's own half-life: 5 x t1/2 removes ~97% of it. For a typical
  inhibitor this is a couple of days, and the binding 7-day floor below is a
  scheduling convention, not pharmacology.

* **Mechanism-based (time-dependent, "suicide") inhibition** - clarithromycin,
  ritonavir, paroxetine, grapefruit furanocoumarins. The enzyme is covalently
  inactivated, so activity returns only as new protein is SYNTHESISED. Recovery
  is set by the enzyme's degradation half-life, not the drug's: clearing
  clarithromycin (t1/2 ~5 h) in a day leaves the enzyme pool still crippled.

* **Induction** - rifampicin, carbamazepine, St John's Wort, enzalutamide. The
  enzyme pool has been physically expanded, so de-induction needs the *excess*
  enzyme to be degraded AFTER the inducer has itself gone. CYP3A activity takes
  on the order of two weeks to return to baseline after rifampicin is stopped,
  which is why induction carries by far the longest window here.

A handful of agents break their own class rule because their own elimination is
extraordinarily slow (norfluoxetine, terbinafine, amiodarone, teriflunomide,
mitotane). Those are surfaced individually as an extended or history-based
exclusion rather than being allowed to inflate the whole class window, which is
exactly how a real protocol words it.

DESIGN AWARENESS
----------------
In a CROSSOVER the comedication perturbs one period only, so it biases the
within-subject Test/Reference contrast directly and the prohibition must run
unbroken across the between-period washout as well. In a PARALLEL study it
biases one arm and cannot cancel. The engine says which, rather than emitting a
generic "concomitant medications are prohibited".

SCOPE
-----
This is a protocol-authoring artifact: the prohibited/restricted/permitted lists,
their windows, and the provenance to cite. It is not a live interaction checker,
a prescribing tool, or a safety database.
"""
from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Named constants
# ---------------------------------------------------------------------------
# Every washout figure this engine emits traces back to one of these. Each is
# either a stated convention (with its source named) or an explicit assumption;
# the assumptions are echoed into the result's `assumptions` list so the
# protocol can DISCLOSE them rather than present them as fact.

# 5 x t1/2 removes ~96.9% of a perpetrator - the standard washout convention,
# and the same one FDA/ICH use for the "30 days or 5 half-lives, whichever is
# longer" prior-investigational-product criterion.
PERPETRATOR_HALF_LIVES = 5.0

# Floor for a purely reversible inhibitor. Almost all reversible inhibitors are
# cleared in well under a week; 7 days is a scheduling convention that gives the
# site a clean, auditable window rather than a per-agent arithmetic exercise.
REVERSIBLE_INHIBITOR_WASHOUT_DAYS = 7.0

# Mechanism-based inhibition recovers by enzyme re-synthesis. Hepatic CYP3A4
# degradation half-life is commonly taken as ~36 h in PBPK practice (Simcyp
# default kdeg 0.0193 /h); 5 x 36 h = 7.5 days for essentially full recovery.
# Rounded up to 14 days to also cover the slowest-clearing agents in the class
# (e.g. itraconazole plus its hydroxy metabolite, ~13 days at 5 x t1/2).
HEPATIC_CYP3A4_DEGRADATION_T_HALF_HOURS = 36.0
MECHANISM_BASED_INHIBITOR_WASHOUT_DAYS = 14.0

# Intestinal CYP3A4 turns over faster (kdeg ~0.03 /h, t1/2 ~23 h), so grapefruit's
# enterocyte effect resolves in roughly 3 days; 7 days is the conventional
# protocol margin and matches the standard BE exclusion wording.
INTESTINAL_CYP3A4_DEGRADATION_T_HALF_HOURS = 23.0
GRAPEFRUIT_WASHOUT_DAYS = 7.0

# De-induction: CYP3A activity returns to baseline roughly 1-2 weeks after
# rifampicin is stopped. 28 days doubles that observed recovery time as the
# conventional protocol margin (and is the window most sponsor templates use for
# strong inducers including St John's Wort).
STRONG_INDUCER_WASHOUT_DAYS = 28.0
MODERATE_INDUCER_WASHOUT_DAYS = 14.0

# Beyond this, a washout is not schedulable inside a normal BE study; the agent
# becomes a history-based exclusion instead of a washout.
IMPRACTICAL_WASHOUT_DAYS = 90.0

# Universal healthy-volunteer conventions.
PRESCRIPTION_WASHOUT_DAYS = 14.0
OTC_WASHOUT_DAYS = 7.0
HERBAL_WASHOUT_DAYS = 28.0          # inherits the induction window (St John's Wort)
INVESTIGATIONAL_WASHOUT_DAYS = 30.0  # or 5 x t1/2, whichever is longer
ALCOHOL_ABSTINENCE_HOURS = 48.0
XANTHINE_ABSTINENCE_HOURS = 24.0     # caffeine competes with CYP1A2 substrates
CHARBROILED_FOOD_WASHOUT_DAYS = 7.0  # polycyclic aromatic hydrocarbons induce CYP1A2
# CYP1A2 activity falls back toward baseline within about a week of stopping
# smoking, but the eligibility convention is a confirmed non-smoker for 6 months,
# which also avoids the recent-quitter intermediate phenotype.
CYP1A2_DEINDUCTION_DAYS = 7.0
NON_SMOKER_MONTHS = 6

# Rescue analgesia limit conventionally permitted in a healthy-volunteer BE unit.
PARACETAMOL_MAX_SINGLE_DOSE_G = 1.0
PARACETAMOL_MAX_DAILY_G = 2.0

# The victim drug's own terminal phase: comedications stay prohibited until the
# last PK sample, i.e. ~5 x the study drug's half-life after the final dose.
VICTIM_TERMINAL_PHASE_HALF_LIVES = 5.0

PROHIBITED = "prohibited"
RESTRICTED = "restricted"
PERMITTED = "permitted"


# ---------------------------------------------------------------------------
# Perpetrator and pathway catalogue
# ---------------------------------------------------------------------------
# `strength` follows the FDA Clinical Drug Interaction Studies guidance (2020)
# definitions, which is what a reviewer will hold the protocol to:
#   strong inhibitor   -> >= 5-fold increase in substrate AUC
#   moderate inhibitor -> >= 2- and < 5-fold increase
#   strong inducer     -> >= 80% decrease in substrate AUC
#   moderate inducer   -> 50-80% decrease
# `t_half_hours` is the elimination half-life of the LONGEST-LIVED active moiety
# (e.g. norfluoxetine, not fluoxetine), because that is what actually governs how
# long the interaction persists. None means "conventional, nothing unusual" and
# the class floor applies.


@dataclass(frozen=True)
class Perpetrator:
    name: str
    role: str                 # "inhibitor" | "inducer"
    strength: str             # "strong" | "moderate"
    mechanism: str            # "reversible" | "mechanism_based" | "induction"
    t_half_hours: Optional[float] = None
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Pathway:
    key: str
    label: str
    kind: str                 # "enzyme" | "transporter"
    mechanism: str            # what the pathway does for a substrate
    victim_risk: str          # what perturbing it does to the measured exposure
    perpetrators: tuple[Perpetrator, ...]
    dietary: tuple[str, ...] = ()   # universal restrictions this pathway activates
    caveat: str = ""                # honest limits (e.g. "not inducible")


_CATALOGUE: tuple[Pathway, ...] = (
    Pathway(
        key="cyp3a4",
        label="CYP3A4/5",
        kind="enzyme",
        mechanism=(
            "CYP3A4/5 is the highest-abundance human drug-metabolising enzyme and sits "
            "in both the enterocyte and the hepatocyte, so it governs first-pass loss as "
            "well as systemic clearance."
        ),
        victim_risk=(
            "Because both gut and liver CYP3A are affected, perturbation hits Cmax and "
            "AUC together and is frequently severalfold; strong inhibitors raise "
            "substrate AUC >= 5-fold and strong inducers cut it by >= 80%."
        ),
        perpetrators=(
            Perpetrator("ritonavir", "inhibitor", "strong", "mechanism_based", 5.0),
            Perpetrator("cobicistat", "inhibitor", "strong", "mechanism_based", 3.5),
            Perpetrator("clarithromycin", "inhibitor", "strong", "mechanism_based", 5.0),
            Perpetrator("itraconazole", "inhibitor", "strong", "reversible", 64.0,
                        "Accumulates on repeat dosing; the hydroxy metabolite is also an inhibitor."),
            Perpetrator("ketoconazole (oral)", "inhibitor", "strong", "reversible", 8.0),
            Perpetrator("voriconazole", "inhibitor", "strong", "reversible", 6.0),
            Perpetrator("erythromycin", "inhibitor", "moderate", "mechanism_based", 3.0),
            Perpetrator("diltiazem", "inhibitor", "moderate", "mechanism_based", 5.0),
            Perpetrator("verapamil", "inhibitor", "moderate", "mechanism_based", 7.0),
            Perpetrator("fluconazole", "inhibitor", "moderate", "reversible", 30.0),
            Perpetrator("rifampicin (rifampin)", "inducer", "strong", "induction", 3.5,
                        "Chronic dosing induces; a SINGLE dose instead acutely INHIBITS OATP1B1."),
            Perpetrator("carbamazepine", "inducer", "strong", "induction", 36.0),
            Perpetrator("phenytoin", "inducer", "strong", "induction", 22.0),
            Perpetrator("phenobarbital", "inducer", "strong", "induction", 90.0),
            Perpetrator("St John's Wort (Hypericum perforatum)", "inducer", "strong", "induction", 9.0,
                        "Unregulated hyperforin content; induction persists well beyond the herb's own clearance."),
            Perpetrator("enzalutamide", "inducer", "strong", "induction", 139.0),
            Perpetrator("apalutamide", "inducer", "strong", "induction", 72.0),
            Perpetrator("mitotane", "inducer", "strong", "induction", 2280.0,
                        "Sequestered in adipose tissue; elimination half-life is measured in months."),
            Perpetrator("efavirenz", "inducer", "moderate", "induction", 52.0),
        ),
        dietary=("grapefruit",),
    ),
    Pathway(
        key="cyp2d6",
        label="CYP2D6",
        kind="enzyme",
        mechanism=(
            "CYP2D6 is a low-abundance but high-affinity hepatic enzyme carrying the "
            "clearance of many cardiovascular and CNS drugs; its activity is genetically "
            "polymorphic and it is NOT transcriptionally inducible."
        ),
        victim_risk=(
            "A strong inhibitor phenocopies the poor-metaboliser genotype, converting an "
            "extensive metaboliser into a functional PM for the duration of the study and "
            "raising exposure severalfold in that subject only - precisely the kind of "
            "subject-specific shift that a bioequivalence contrast cannot absorb."
        ),
        perpetrators=(
            Perpetrator("paroxetine", "inhibitor", "strong", "mechanism_based", 21.0),
            Perpetrator("fluoxetine", "inhibitor", "strong", "mechanism_based", 240.0,
                        "The active metabolite norfluoxetine has a 4-16 day half-life, so the "
                        "inhibition long outlasts the parent drug."),
            Perpetrator("bupropion", "inhibitor", "strong", "mechanism_based", 21.0),
            Perpetrator("quinidine", "inhibitor", "strong", "reversible", 7.0),
            Perpetrator("terbinafine", "inhibitor", "strong", "reversible", 300.0,
                        "Slow redistribution from skin and adipose gives a terminal half-life "
                        "of 200-400 h."),
            Perpetrator("duloxetine", "inhibitor", "moderate", "reversible", 12.0),
            Perpetrator("mirabegron", "inhibitor", "moderate", "reversible", 50.0),
            Perpetrator("cinacalcet", "inhibitor", "moderate", "reversible", 40.0),
        ),
        caveat=(
            "CYP2D6 is not appreciably inducible in humans - no clinically relevant "
            "inducer is recognised - so no inducer prohibition is stated for this "
            "pathway. Reduced activity comes from genotype or from inhibitors, not from "
            "induction."
        ),
    ),
    Pathway(
        key="cyp2c19",
        label="CYP2C19",
        kind="enzyme",
        mechanism=(
            "CYP2C19 is a polymorphic hepatic enzyme clearing proton-pump inhibitors, "
            "several antidepressants and clopidogrel's activation step."
        ),
        victim_risk=(
            "Inhibition converts the subject to a functional poor metaboliser; induction "
            "by rifampicin can more than halve substrate exposure."
        ),
        perpetrators=(
            Perpetrator("fluvoxamine", "inhibitor", "strong", "reversible", 16.0),
            Perpetrator("fluconazole", "inhibitor", "strong", "reversible", 30.0),
            Perpetrator("ticlopidine", "inhibitor", "strong", "mechanism_based", 96.0,
                        "Half-life rises to 4-5 days on repeat dosing."),
            Perpetrator("omeprazole", "inhibitor", "moderate", "reversible", 1.0),
            Perpetrator("esomeprazole", "inhibitor", "moderate", "reversible", 1.5),
            Perpetrator("voriconazole", "inhibitor", "moderate", "reversible", 6.0),
            Perpetrator("rifampicin (rifampin)", "inducer", "strong", "induction", 3.5),
            Perpetrator("carbamazepine", "inducer", "moderate", "induction", 36.0),
            Perpetrator("phenytoin", "inducer", "moderate", "induction", 22.0),
        ),
    ),
    Pathway(
        key="cyp2c9",
        label="CYP2C9",
        kind="enzyme",
        mechanism=(
            "CYP2C9 clears the NSAIDs, sulfonylureas, phenytoin and S-warfarin, and is "
            "polymorphic (*2/*3 reduced-function alleles)."
        ),
        victim_risk=(
            "Inhibition of a CYP2C9 substrate raises exposure roughly 2-3 fold; because "
            "several CYP2C9 substrates are narrow-index drugs the tolerance for any "
            "perturbation is low."
        ),
        perpetrators=(
            Perpetrator("fluconazole", "inhibitor", "moderate", "reversible", 30.0),
            Perpetrator("miconazole (oral gel)", "inhibitor", "moderate", "reversible", 24.0),
            Perpetrator("fluvoxamine", "inhibitor", "moderate", "reversible", 16.0),
            Perpetrator("amiodarone", "inhibitor", "moderate", "reversible", 1392.0,
                        "Mean terminal half-life ~58 days (range 15-142); a conventional "
                        "washout is not achievable."),
            Perpetrator("rifampicin (rifampin)", "inducer", "strong", "induction", 3.5),
            Perpetrator("carbamazepine", "inducer", "moderate", "induction", 36.0),
            Perpetrator("phenytoin", "inducer", "moderate", "induction", 22.0),
        ),
        caveat=(
            "FDA's index list recognises no STRONG CYP2C9 inhibitor; the agents above are "
            "classified moderate, so they are stated as restricted with a documented "
            "washout unless the victim drug is narrow-therapeutic-index."
        ),
    ),
    Pathway(
        key="cyp2c8",
        label="CYP2C8",
        kind="enzyme",
        mechanism=(
            "CYP2C8 clears the glitazones, repaglinide, montelukast and paclitaxel; its "
            "index inhibitors act through acyl-glucuronide metabolites."
        ),
        victim_risk=(
            "Gemfibrozil raises CYP2C8 substrate AUC severalfold (repaglinide ~8-fold), "
            "one of the largest single-agent interactions in clinical pharmacology."
        ),
        perpetrators=(
            Perpetrator("gemfibrozil", "inhibitor", "strong", "mechanism_based", 1.5,
                        "Inhibition is carried by the acyl-glucuronide, so it outlasts the parent."),
            Perpetrator("clopidogrel", "inhibitor", "strong", "mechanism_based", 6.0,
                        "Also acyl-glucuronide mediated."),
            Perpetrator("trimethoprim", "inhibitor", "moderate", "reversible", 10.0),
            Perpetrator("teriflunomide", "inhibitor", "moderate", "reversible", 456.0,
                        "Enterohepatic recirculation gives a 15-19 day half-life; elimination "
                        "normally requires an accelerated cholestyramine procedure."),
            Perpetrator("rifampicin (rifampin)", "inducer", "strong", "induction", 3.5),
        ),
    ),
    Pathway(
        key="cyp1a2",
        label="CYP1A2",
        kind="enzyme",
        mechanism=(
            "CYP1A2 clears caffeine, theophylline, tizanidine, olanzapine and duloxetine, "
            "and is strongly induced by the polycyclic aromatic hydrocarbons in tobacco "
            "smoke and charbroiled food."
        ),
        victim_risk=(
            "Fluvoxamine raises CYP1A2 substrate AUC severalfold, while smoking roughly "
            "doubles clearance - so both the medication list and the diet/lifestyle "
            "restrictions matter for this pathway."
        ),
        perpetrators=(
            Perpetrator("fluvoxamine", "inhibitor", "strong", "reversible", 16.0),
            Perpetrator("ciprofloxacin", "inhibitor", "strong", "reversible", 4.0),
            Perpetrator("enoxacin", "inhibitor", "strong", "reversible", 5.0),
            Perpetrator("oral contraceptives (ethinylestradiol)", "inhibitor", "moderate", "reversible", 20.0),
            Perpetrator("mexiletine", "inhibitor", "moderate", "reversible", 11.0),
            Perpetrator("zafirlukast", "inhibitor", "moderate", "reversible", 10.0),
            Perpetrator("rifampicin (rifampin)", "inducer", "moderate", "induction", 3.5),
            Perpetrator("phenytoin", "inducer", "moderate", "induction", 22.0),
            Perpetrator("teriflunomide", "inducer", "moderate", "induction", 456.0),
        ),
        dietary=("tobacco", "xanthines", "charbroiled"),
        caveat=(
            "No STRONG CYP1A2 inducer is recognised in FDA's index list; tobacco smoking "
            "is the dominant real-world inducer and is handled as an eligibility "
            "criterion rather than a washout."
        ),
    ),
    Pathway(
        key="cyp2b6",
        label="CYP2B6",
        kind="enzyme",
        mechanism=(
            "CYP2B6 clears efavirenz, bupropion, ketamine and cyclophosphamide "
            "activation, and is both polymorphic and readily inducible."
        ),
        victim_risk=(
            "Induction by rifampicin roughly halves CYP2B6 substrate exposure; inhibition "
            "by ticlopidine or clopidogrel raises it around 2-fold."
        ),
        perpetrators=(
            Perpetrator("ticlopidine", "inhibitor", "moderate", "mechanism_based", 96.0),
            Perpetrator("clopidogrel", "inhibitor", "moderate", "mechanism_based", 6.0),
            Perpetrator("voriconazole", "inhibitor", "moderate", "reversible", 6.0),
            Perpetrator("rifampicin (rifampin)", "inducer", "strong", "induction", 3.5),
            Perpetrator("carbamazepine", "inducer", "moderate", "induction", 36.0),
            Perpetrator("efavirenz", "inducer", "moderate", "induction", 52.0),
            Perpetrator("ritonavir", "inducer", "moderate", "induction", 5.0),
        ),
        caveat=(
            "FDA's index list recognises no strong CYP2B6 inhibitor; the inhibitors above "
            "are moderate."
        ),
    ),
    Pathway(
        key="ugt",
        label="UDP-glucuronosyltransferases (UGT1A1/1A4/1A9/2B7)",
        kind="enzyme",
        mechanism=(
            "Glucuronidation is a high-capacity phase II conjugation route; it is harder "
            "to saturate than CYP oxidation but is still inhibited competitively and is "
            "induced by the same PXR/CAR ligands that induce CYP3A4."
        ),
        victim_risk=(
            "Interactions are usually smaller than CYP interactions (commonly < 2-fold) "
            "but rifampicin-type induction can still cut exposure by half, and the "
            "restriction has a second consequence: paracetamol, the default rescue "
            "analgesic, is itself glucuronidated."
        ),
        perpetrators=(
            Perpetrator("atazanavir", "inhibitor", "strong", "reversible", 7.0,
                        "Potent UGT1A1 inhibitor."),
            Perpetrator("probenecid", "inhibitor", "moderate", "reversible", 8.0),
            Perpetrator("valproic acid", "inhibitor", "moderate", "reversible", 14.0),
            Perpetrator("gemfibrozil", "inhibitor", "moderate", "reversible", 1.5),
            Perpetrator("mefenamic acid", "inhibitor", "moderate", "reversible", 2.0),
            Perpetrator("rifampicin (rifampin)", "inducer", "strong", "induction", 3.5),
            Perpetrator("carbamazepine", "inducer", "moderate", "induction", 36.0),
            Perpetrator("phenytoin", "inducer", "moderate", "induction", 22.0),
            Perpetrator("St John's Wort (Hypericum perforatum)", "inducer", "moderate", "induction", 9.0),
        ),
    ),
    Pathway(
        key="pgp",
        label="P-glycoprotein (P-gp / ABCB1 / MDR1)",
        kind="transporter",
        mechanism=(
            "P-gp is an efflux pump on the apical enterocyte membrane, the biliary "
            "canaliculus, the renal tubule and the blood-brain barrier; it limits oral "
            "absorption and promotes elimination."
        ),
        victim_risk=(
            "Inhibition raises the absorbed fraction of a P-gp substrate and typically "
            "increases both Cmax and AUC by 1.5-3 fold; induction by rifampicin or "
            "St John's Wort reduces them comparably. Because the effect is largely "
            "absorption-side, Cmax often moves more than AUC."
        ),
        perpetrators=(
            Perpetrator("quinidine", "inhibitor", "strong", "reversible", 7.0),
            Perpetrator("verapamil", "inhibitor", "strong", "reversible", 7.0),
            Perpetrator("ritonavir", "inhibitor", "strong", "mechanism_based", 5.0),
            Perpetrator("clarithromycin", "inhibitor", "strong", "mechanism_based", 5.0),
            Perpetrator("itraconazole", "inhibitor", "strong", "reversible", 64.0),
            Perpetrator("ciclosporin (cyclosporine)", "inhibitor", "strong", "reversible", 19.0),
            Perpetrator("amiodarone", "inhibitor", "moderate", "reversible", 1392.0,
                        "Mean terminal half-life ~58 days; a conventional washout is not achievable."),
            Perpetrator("dronedarone", "inhibitor", "moderate", "reversible", 27.0),
            Perpetrator("rifampicin (rifampin)", "inducer", "strong", "induction", 3.5),
            Perpetrator("St John's Wort (Hypericum perforatum)", "inducer", "strong", "induction", 9.0),
            Perpetrator("carbamazepine", "inducer", "moderate", "induction", 36.0),
            Perpetrator("phenytoin", "inducer", "moderate", "induction", 22.0),
        ),
    ),
    Pathway(
        key="bcrp",
        label="BCRP (ABCG2)",
        kind="transporter",
        mechanism=(
            "BCRP is an apical efflux transporter in gut, liver and kidney with heavy "
            "substrate overlap with P-gp; it is polymorphic (ABCG2 c.421C>A)."
        ),
        victim_risk=(
            "Inhibition raises oral exposure of BCRP substrates roughly 2-fold, again "
            "weighted toward the absorption phase and therefore toward Cmax."
        ),
        perpetrators=(
            Perpetrator("ciclosporin (cyclosporine)", "inhibitor", "strong", "reversible", 19.0),
            Perpetrator("eltrombopag", "inhibitor", "strong", "reversible", 30.0),
            Perpetrator("febuxostat", "inhibitor", "moderate", "reversible", 8.0),
            Perpetrator("curcumin-containing supplements", "inhibitor", "moderate", "reversible", None,
                        "Unregulated supplement; content and potency vary between products."),
        ),
        dietary=("fruit_juice_transporter",),
        caveat=(
            "No clinically established BCRP inducer is recognised, so no inducer "
            "prohibition is stated for this transporter."
        ),
    ),
    Pathway(
        key="oatp",
        label="OATP1B1 / OATP1B3 (SLCO1B1 / SLCO1B3)",
        kind="transporter",
        mechanism=(
            "OATP1B1/1B3 are sinusoidal uptake transporters that carry anions - statin "
            "acids, sartans, bosentan - INTO the hepatocyte, so they gate hepatic uptake "
            "and therefore hepatic clearance itself."
        ),
        victim_risk=(
            "Because uptake is rate-limiting for these substrates, inhibition produces "
            "some of the largest exposure changes in clinical pharmacology (ciclosporin "
            "raises statin AUC severalfold) and does so within a single dose."
        ),
        perpetrators=(
            Perpetrator("ciclosporin (cyclosporine)", "inhibitor", "strong", "reversible", 19.0),
            Perpetrator("rifampicin (rifampin), single dose", "inhibitor", "strong", "reversible", 3.5,
                        "Acute OATP1B1 inhibition - the OPPOSITE direction to its chronic "
                        "induction effect; the protocol must not conflate the two."),
            Perpetrator("atazanavir/ritonavir", "inhibitor", "strong", "reversible", 7.0),
            Perpetrator("gemfibrozil", "inhibitor", "moderate", "reversible", 1.5),
            Perpetrator("clarithromycin", "inhibitor", "moderate", "reversible", 5.0),
            Perpetrator("simeprevir", "inhibitor", "moderate", "reversible", 41.0),
        ),
        dietary=("fruit_juice_transporter",),
        caveat=(
            "Hepatic uptake transporters are not clinically inducible in the way CYP "
            "enzymes are, so no inducer prohibition is stated for this pathway."
        ),
    ),
    Pathway(
        key="oct_mate",
        label="OCT2 / MATE1 / MATE2-K",
        kind="transporter",
        mechanism=(
            "OCT2 takes organic cations up into the renal proximal tubule cell and "
            "MATE1/MATE2-K pump them out into urine; together they run active renal "
            "secretion for cationic drugs such as metformin."
        ),
        victim_risk=(
            "Inhibition blocks active tubular secretion and raises exposure by roughly "
            "1.5-2 fold while lowering renal clearance - the effect appears in the "
            "elimination phase rather than in absorption."
        ),
        perpetrators=(
            Perpetrator("cimetidine", "inhibitor", "moderate", "reversible", 2.0),
            Perpetrator("dolutegravir", "inhibitor", "moderate", "reversible", 14.0),
            Perpetrator("trimethoprim", "inhibitor", "moderate", "reversible", 10.0),
            Perpetrator("ranolazine", "inhibitor", "moderate", "reversible", 7.0),
            Perpetrator("pyrimethamine", "inhibitor", "strong", "reversible", 96.0,
                        "Potent MATE inhibitor with a ~4 day half-life."),
            Perpetrator("vandetanib", "inhibitor", "moderate", "reversible", 456.0,
                        "Terminal half-life ~19 days."),
        ),
        caveat=(
            "No clinically established inducer of renal cation transporters is "
            "recognised, so no inducer prohibition is stated for this pathway."
        ),
    ),
)

_BY_KEY = {p.key: p for p in _CATALOGUE}
PATHWAY_KEYS: tuple[str, ...] = tuple(p.key for p in _CATALOGUE)


# ---------------------------------------------------------------------------
# Applicability detection - deterministic cues from the REAL label text
# ---------------------------------------------------------------------------
# Matched case-insensitively against the DailyMed sections fetched for the
# protocol. The DRUG INTERACTIONS and CONTRAINDICATIONS sections are searched
# FIRST because a named interacting agent there ("concomitant ketoconazole
# increased ... AUC 3-fold") is the strongest possible evidence that the pathway
# is real and clinically consequential for this product - stronger than a
# passing in-vitro mention in Clinical Pharmacology.
#
# Named-agent cues are deliberately restricted to unambiguous index perpetrators
# (ketoconazole/itraconazole for CYP3A4, quinidine for CYP2D6, gemfibrozil for
# CYP2C8, digoxin for P-gp). Agents that hit several pathways (fluconazole,
# ritonavir, clarithromycin) are NOT used as cues, because they would attribute
# the wrong pathway.

_CUES: dict[str, tuple[str, ...]] = {
    "cyp3a4": (
        r"cyp\s*-?\s*3a", r"\bketoconazole\b", r"\bitraconazole\b",
        r"\bgrapefruit\b", r"\bmidazolam\b",
    ),
    "cyp2d6": (
        r"cyp\s*-?\s*2d6", r"\bquinidine\b", r"\bdebrisoquine\b", r"\bsparteine\b",
        r"\bdextromethorphan\b",
    ),
    "cyp2c19": (
        r"cyp\s*-?\s*2c19", r"\bmephenytoin\b",
    ),
    "cyp2c9": (
        r"cyp\s*-?\s*2c9\b", r"\bs-?\s?warfarin\b", r"\btolbutamide\b",
    ),
    "cyp2c8": (
        r"cyp\s*-?\s*2c8", r"\bgemfibrozil\b", r"\brepaglinide\b",
    ),
    "cyp1a2": (
        r"cyp\s*-?\s*1a2", r"\bfluvoxamine\b", r"\bcaffeine\b", r"\btheophylline\b",
    ),
    "cyp2b6": (
        r"cyp\s*-?\s*2b6", r"\befavirenz\b",
    ),
    "ugt": (
        r"\bugt\s*-?\s*[12][ab]\d", r"\bugt\b", r"glucuronidat", r"glucuronide",
        r"uridine diphosphate", r"udp-?glucuronosyltransferase",
    ),
    "pgp": (
        r"p-?\s?glycoprotein", r"\bp-?gp\b", r"\babcb1\b", r"\bmdr1\b", r"\bdigoxin\b",
    ),
    "bcrp": (
        r"\bbcrp\b", r"\babcg2\b", r"breast cancer resistance protein",
    ),
    "oatp": (
        r"\boatp\s*-?\s*1b[13]\b", r"\boatp\b", r"\bslco1b1\b",
        r"organic anion[- ]transporting polypeptide",
    ),
    "oct_mate": (
        r"\boct\s*-?\s*[12]\b", r"\bmate\s*-?\s*[12]\b", r"mate2-?k",
        r"organic cation transporter", r"multidrug and toxin extrusion",
    ),
}

# A label that talks about CYP metabolism WITHOUT naming an isoform still tells
# us the drug is oxidatively cleared; that warrants a conservative generic
# prohibition (flagged as an assumption) rather than silence.
_GENERIC_CYP_CUES: tuple[str, ...] = (
    r"cytochrome\s+p-?\s?450", r"\bcyp\s?450\b", r"\bcyp\b",
    r"hepatic (?:metabolism|oxidation)", r"oxidative metaboli[sz]",
)

# Label sections consulted, in evidence-strength order. The first two are the
# ones literature_service.py had never fetched before this engine existed.
_SECTION_ORDER: tuple[str, ...] = (
    "drug_interactions",
    "contraindications",
    "clinical_pharmacology",
    "pharmacokinetics",
)

_SECTION_LABELS: dict[str, str] = {
    "drug_interactions": "DailyMed label, DRUG INTERACTIONS (SPL LOINC 34073-7)",
    "contraindications": "DailyMed label, CONTRAINDICATIONS (SPL LOINC 34070-3)",
    "clinical_pharmacology": "DailyMed label, CLINICAL PHARMACOLOGY (SPL LOINC 34090-1)",
    "pharmacokinetics": "DailyMed label, PHARMACOKINETICS (SPL LOINC 43682-4)",
}

_CONVENTION_EVIDENCE = {
    "source": "Standard healthy-volunteer bioequivalence protocol convention",
    "cue": "",
    "quote": "",
}

_QUOTE_WINDOW = 110  # chars of surrounding label text kept as the citable quote


def _excerpt(original: str, start: int, end: int) -> str:
    """A short, whitespace-normalised quote around a cue hit, for citation."""
    lo = max(0, start - _QUOTE_WINDOW // 2)
    hi = min(len(original), end + _QUOTE_WINDOW)
    snippet = " ".join(original[lo:hi].split())
    return f"{'...' if lo > 0 else ''}{snippet}{'...' if hi < len(original) else ''}"


def _detect(sections: dict[str, str]) -> dict[str, list[dict[str, str]]]:
    """Which pathways the label gives positive evidence for, with provenance.

    Returns ``{pathway_key: [{"source", "cue", "quote"}, ...]}`` with the
    strongest section first, so every emitted prohibition can name the label
    text it rests on instead of asserting the pathway.
    """
    found: dict[str, list[dict[str, str]]] = {}
    for section_key in _SECTION_ORDER:
        text = sections.get(section_key) or ""
        if not text:
            continue
        lowered = text.lower()
        for key, patterns in _CUES.items():
            for pattern in patterns:
                match = re.search(pattern, lowered)
                if not match:
                    continue
                found.setdefault(key, []).append({
                    "source": _SECTION_LABELS.get(section_key, section_key),
                    "cue": match.group(0),
                    "quote": _excerpt(text, match.start(), match.end()),
                })
                break  # one citation per pathway per section is enough
    return found


def _detect_generic_cyp(sections: dict[str, str]) -> Optional[dict[str, str]]:
    """Evidence that the drug is CYP-cleared without the isoform being named."""
    for section_key in _SECTION_ORDER:
        text = sections.get(section_key) or ""
        if not text:
            continue
        lowered = text.lower()
        for pattern in _GENERIC_CYP_CUES:
            match = re.search(pattern, lowered)
            if match:
                return {
                    "source": _SECTION_LABELS.get(section_key, section_key),
                    "cue": match.group(0),
                    "quote": _excerpt(text, match.start(), match.end()),
                }
    return None


# ---------------------------------------------------------------------------
# Washout arithmetic
# ---------------------------------------------------------------------------


def _class_floor_days(role: str, strength: str, mechanisms: set[str]) -> tuple[float, str]:
    """The class-level washout and the sentence justifying it.

    The asymmetry is the whole point: an inducer's window is set by how long the
    EXPANDED ENZYME POOL takes to be degraded once the inducer is gone, which is
    far longer than the time needed to clear a reversible inhibitor.
    """
    if role == "inducer":
        if strength == "strong":
            return (
                STRONG_INDUCER_WASHOUT_DAYS,
                "De-induction is governed by degradation of the induced enzyme pool "
                "AFTER the inducer itself has been cleared, not by the inducer's "
                "half-life. CYP3A activity returns to baseline roughly 1-2 weeks after "
                f"rifampicin is stopped; {STRONG_INDUCER_WASHOUT_DAYS:.0f} days applies "
                "the conventional doubling of that observed recovery time as a margin.",
            )
        return (
            MODERATE_INDUCER_WASHOUT_DAYS,
            "Moderate induction (50-80% reduction in substrate AUC) resolves faster than "
            f"strong induction; {MODERATE_INDUCER_WASHOUT_DAYS:.0f} days covers "
            "degradation of the induced enzyme pool for this class.",
        )
    if "mechanism_based" in mechanisms:
        return (
            MECHANISM_BASED_INHIBITOR_WASHOUT_DAYS,
            "This class includes mechanism-based (time-dependent) inhibitors, which "
            "covalently inactivate the enzyme, so recovery requires new enzyme SYNTHESIS "
            "rather than clearance of the inhibitor. At a hepatic CYP3A4 degradation "
            f"half-life of ~{HEPATIC_CYP3A4_DEGRADATION_T_HALF_HOURS:.0f} h, "
            f"{PERPETRATOR_HALF_LIVES:.0f} turnover half-lives is ~7.5 days; "
            f"{MECHANISM_BASED_INHIBITOR_WASHOUT_DAYS:.0f} days rounds that up and also "
            "covers the slowest-clearing agents in the class.",
        )
    return (
        REVERSIBLE_INHIBITOR_WASHOUT_DAYS,
        "Reversible (competitive) inhibition disappears as the inhibitor is eliminated, "
        f"so the window is {PERPETRATOR_HALF_LIVES:.0f} x the inhibitor's own half-life; "
        f"{REVERSIBLE_INHIBITOR_WASHOUT_DAYS:.0f} days is the scheduling floor that covers "
        "every conventional agent in this class.",
    )


def _agent_washout_days(perp: Perpetrator) -> Optional[int]:
    """5 x the agent's own half-life, in whole days - or None if unremarkable."""
    if not perp.t_half_hours:
        return None
    return int(math.ceil(PERPETRATOR_HALF_LIVES * perp.t_half_hours / 24.0))


def _build_agent_lists(
    perps: tuple[Perpetrator, ...], class_days: float
) -> tuple[list[str], list[dict[str, Any]], list[dict[str, Any]]]:
    """Split a class into: agents the class window covers, agents needing a LONGER
    individual window, and agents whose washout is not schedulable at all.

    Letting one outlier (norfluoxetine, terbinafine, amiodarone) inflate the whole
    class window would be unworkable for a site; naming it separately is how a
    real protocol words the same fact.
    """
    covered: list[str] = []
    extended: list[dict[str, Any]] = []
    history: list[dict[str, Any]] = []
    for p in perps:
        days = _agent_washout_days(p)
        if days is None or days <= class_days:
            covered.append(p.name)
            continue
        basis = (
            f"{PERPETRATOR_HALF_LIVES:.0f} x its own terminal half-life "
            f"({p.t_half_hours:.0f} h) = {days} days"
        )
        if p.note:
            basis += f". {p.note}"
        if days > IMPRACTICAL_WASHOUT_DAYS:
            history.append({
                "name": p.name,
                "estimated_washout_days": days,
                "basis": basis,
                "action": (
                    f"Washout exceeds the {IMPRACTICAL_WASHOUT_DAYS:.0f}-day practical "
                    "ceiling for a healthy-volunteer study; exclude any subject with a "
                    "history of use rather than attempting a washout."
                ),
            })
        else:
            extended.append({"name": p.name, "washout_days": days, "basis": basis})
    return covered, extended, history


# ---------------------------------------------------------------------------
# Entry construction
# ---------------------------------------------------------------------------


def _entry(
    *,
    key: str,
    label: str,
    status: str,
    washout_days: Optional[float],
    basis: str,
    rationale: str,
    evidence: dict[str, str],
    examples: Optional[list[str]] = None,
    extended: Optional[list[dict[str, Any]]] = None,
    history: Optional[list[dict[str, Any]]] = None,
    pathway: str = "",
    conditions: str = "",
    escalated_by_nti: bool = False,
    assumption: str = "",
) -> dict[str, Any]:
    """One row of the protocol's concomitant-medication table (JSON-safe)."""
    return {
        "key": key,
        "label": label,
        "status": status,
        "pathway": pathway,
        "examples": list(examples or []),
        "washout_days": washout_days,
        "washout_basis": basis,
        "extended_washout": list(extended or []),
        "history_exclusion": list(history or []),
        "rationale": rationale,
        "conditions": conditions,
        "escalated_by_nti": escalated_by_nti,
        "assumption": assumption,
        "evidence": dict(evidence),
    }


def _pathway_entries(
    pw: Pathway,
    evidence: dict[str, str],
    *,
    is_nti: bool,
) -> list[dict[str, Any]]:
    """The prohibited/restricted rows a single detected pathway generates."""
    rows: list[dict[str, Any]] = []
    for role in ("inhibitor", "inducer"):
        for strength in ("strong", "moderate"):
            perps = tuple(
                p for p in pw.perpetrators if p.role == role and p.strength == strength
            )
            if not perps:
                continue
            class_days, basis = _class_floor_days(
                role, strength, {p.mechanism for p in perps}
            )
            covered, extended, history = _build_agent_lists(perps, class_days)

            # Strong perpetrators are exclusionary. Moderate ones are normally
            # allowed with a documented washout - but for a narrow-therapeutic-index
            # victim a 2-fold exposure shift is already outside the tightened
            # acceptance limits, so they are escalated to prohibited.
            escalate = strength == "moderate" and is_nti
            status = PROHIBITED if (strength == "strong" or escalate) else RESTRICTED

            direction = (
                "raise the exposure of the study drug" if role == "inhibitor"
                else "lower the exposure of the study drug"
            )
            rationale = (
                f"{pw.mechanism} {pw.victim_risk} A {strength} {role} would therefore "
                f"{direction} in any subject taking one, which biases the "
                f"Test/Reference comparison rather than adding symmetric noise."
            )
            if escalate:
                rationale += (
                    " This is a narrow-therapeutic-index product, so the acceptance "
                    "limits are tightened and a moderate perturbation is already "
                    "unacceptable; the class is therefore prohibited rather than "
                    "merely restricted."
                )
            conditions = "" if status == PROHIBITED else (
                "Permitted only if the last dose was taken at least the stated washout "
                "before first study drug administration, the use is documented in source "
                "and on the CRF, and the investigator confirms eligibility."
            )
            rows.append(_entry(
                key=f"{pw.key}_{strength}_{role}s",
                label=f"{strength.capitalize()} {role}s of {pw.label}",
                status=status,
                washout_days=class_days,
                basis=basis,
                rationale=rationale,
                evidence=evidence,
                examples=covered,
                extended=extended,
                history=history,
                pathway=pw.key,
                conditions=conditions,
                escalated_by_nti=escalate,
            ))
    return rows


# ---------------------------------------------------------------------------
# Universal healthy-volunteer restrictions
# ---------------------------------------------------------------------------
# Deliberately split into ALWAYS-ON entries and PATHWAY-GATED entries. Grapefruit
# and tobacco are the two most-copied boilerplate exclusions in the industry and
# both are pathway-specific: grapefruit only matters to a CYP3A4 (or intestinal
# transporter) substrate, tobacco only to a CYP1A2 substrate. Emitting them for a
# drug that uses neither pathway is exactly the defect this engine exists to fix.


def _dietary_entry(key: str, evidence: dict[str, str]) -> Optional[dict[str, Any]]:
    if key == "grapefruit":
        return _entry(
            key="grapefruit",
            label="Grapefruit, Seville (bitter) orange, pomelo and their juices",
            status=PROHIBITED,
            washout_days=GRAPEFRUIT_WASHOUT_DAYS,
            basis=(
                "Furanocoumarins in grapefruit are MECHANISM-BASED inhibitors of "
                "ENTEROCYTE CYP3A4, so recovery needs new enterocyte enzyme, not "
                "clearance of the juice: at an intestinal CYP3A4 degradation half-life "
                f"of ~{INTESTINAL_CYP3A4_DEGRADATION_T_HALF_HOURS:.0f} h, activity takes "
                f"roughly 3 days to return. {GRAPEFRUIT_WASHOUT_DAYS:.0f} days is the "
                "conventional protocol margin on top of that."
            ),
            rationale=(
                "The study drug is a CYP3A4 substrate, so a single glass of grapefruit "
                "juice can raise its first-pass availability substantially and does so "
                "for days after ingestion - large enough to move Cmax out of the "
                "acceptance range in the affected period."
            ),
            evidence=evidence,
            pathway="cyp3a4",
        )
    if key == "fruit_juice_transporter":
        return _entry(
            key="fruit_juice_transporter",
            label="Grapefruit, orange and apple juice (large volumes)",
            status=RESTRICTED,
            washout_days=3.0,
            basis=(
                "Fruit-juice inhibition of intestinal OATP2B1/BCRP is reversible and "
                "resolves once the juice is cleared from the gut lumen; 3 days is a "
                "conservative margin around that."
            ),
            rationale=(
                "The study drug depends on an intestinal uptake/efflux transporter that "
                "common fruit juices inhibit, which changes the absorbed fraction and "
                "therefore Cmax."
            ),
            evidence=evidence,
            conditions=(
                "Prohibited from 3 days before each dosing until the final PK sample of "
                "that period; ordinary quantities outside that window are acceptable."
            ),
        )
    if key == "tobacco":
        return _entry(
            key="tobacco",
            label="Tobacco and nicotine-containing products (including e-cigarettes)",
            status=PROHIBITED,
            washout_days=float(NON_SMOKER_MONTHS * 30),
            basis=(
                "Polycyclic aromatic hydrocarbons in smoke INDUCE CYP1A2. Activity falls "
                f"back toward baseline within about {CYP1A2_DEINDUCTION_DAYS:.0f} days of "
                f"cessation, but eligibility conventionally requires confirmed non-smoker "
                f"status for {NON_SMOKER_MONTHS} months, which also avoids enrolling "
                "recent quitters with an intermediate, still-changing phenotype."
            ),
            rationale=(
                "The study drug is a CYP1A2 substrate, and smoking can roughly double "
                "CYP1A2 clearance. Smoking that starts, stops or changes intensity "
                "between periods would shift clearance within a single subject and "
                "corrupt the within-subject comparison."
            ),
            evidence=evidence,
            pathway="cyp1a2",
        )
    if key == "xanthines":
        return _entry(
            key="xanthines",
            label="Caffeine and other xanthine-containing food and drink (coffee, tea, cola, chocolate, energy drinks)",
            status=RESTRICTED,
            washout_days=XANTHINE_ABSTINENCE_HOURS / 24.0,
            basis=(
                f"Caffeine is a competing CYP1A2 substrate; abstinence from "
                f"{XANTHINE_ABSTINENCE_HOURS:.0f} h before dosing until the final PK "
                "sample of the period removes the competition during the measurement."
            ),
            rationale=(
                "The study drug is cleared by CYP1A2, so a competing substrate taken "
                "around dosing changes its clearance for that period only."
            ),
            evidence=evidence,
            pathway="cyp1a2",
            conditions=(
                f"Prohibited from {XANTHINE_ABSTINENCE_HOURS:.0f} h pre-dose through the "
                "last PK sample of each period; normal intake permitted outside that window."
            ),
        )
    if key == "charbroiled":
        return _entry(
            key="charbroiled",
            label="Charbroiled or char-grilled foods and cruciferous vegetables in excess",
            status=RESTRICTED,
            washout_days=CHARBROILED_FOOD_WASHOUT_DAYS,
            basis=(
                "Charbroiling generates the same polycyclic aromatic hydrocarbons as "
                f"tobacco smoke and induces CYP1A2; {CHARBROILED_FOOD_WASHOUT_DAYS:.0f} "
                "days is the conventional dietary window used in BE units."
            ),
            rationale=(
                "Dietary CYP1A2 induction is a real and easily missed source of "
                "between-period clearance change for a CYP1A2 substrate."
            ),
            evidence=evidence,
            pathway="cyp1a2",
        )
    return None


def _always_on_entries(
    *, victim_t_half_hours: Optional[float], allow_paracetamol: bool, paracetamol_reason: str,
    contraceptive_restricted: bool, healthy_volunteers: bool,
) -> list[dict[str, Any]]:
    """Restrictions that apply to any BE/PK study regardless of pathway. Kept
    deliberately short - anything pathway-specific belongs above."""
    investigational_days = INVESTIGATIONAL_WASHOUT_DAYS
    investigational_basis = (
        f"{INVESTIGATIONAL_WASHOUT_DAYS:.0f} days or "
        f"{PERPETRATOR_HALF_LIVES:.0f} x the half-life of the previous investigational "
        "product, whichever is LONGER - the standard first-in-human/BE convention."
    )

    # A blanket prohibition on all prescription medicines is only honest in a
    # healthy-volunteer study. Patients enrolled in a PK-similarity study are on
    # standard-of-care therapy that cannot be stopped for the study, so the rule
    # becomes "restricted and held constant" - the pathway-specific rows above
    # still bind, and they are what actually protect the measurement.
    if healthy_volunteers:
        rx_status, rx_conditions = PROHIBITED, ""
        rx_rationale = (
            "A healthy-volunteer PK study is only interpretable if the subject's "
            "clearance is unperturbed; an unrecognised comedication is the most common "
            "cause of an unexplained outlying exposure."
        )
    else:
        rx_status = RESTRICTED
        rx_rationale = (
            "Subjects are patients on background therapy that cannot be withdrawn for "
            "the study, so medication is controlled by keeping the regimen CONSTANT "
            "rather than by prohibiting it; the pathway-specific classes above remain "
            "fully exclusionary because those genuinely move the analyte's exposure."
        )
        rx_conditions = (
            "Background therapy required for the underlying condition is permitted "
            "provided it does not appear in the pathway-specific prohibited classes "
            "above, the dose and schedule are unchanged from screening through the final "
            "PK sample, and any change is recorded as a potential PK confounder."
        )

    rows = [
        _entry(
            key="prescription_medication",
            label="Any prescription medication",
            status=rx_status,
            washout_days=PRESCRIPTION_WASHOUT_DAYS,
            basis=(
                f"{PRESCRIPTION_WASHOUT_DAYS:.0f} days is the conventional blanket window "
                "for prescription medicines in a BE study; agents named in the "
                "pathway-specific rows above carry their own, longer windows and those "
                "take precedence."
            ),
            rationale=rx_rationale,
            evidence=_CONVENTION_EVIDENCE,
            conditions=rx_conditions,
        ),
        _entry(
            key="otc_medication",
            label="Over-the-counter medicines, vitamins and mineral supplements",
            status=RESTRICTED,
            washout_days=OTC_WASHOUT_DAYS,
            basis=(
                f"{OTC_WASHOUT_DAYS:.0f} days covers the elimination of essentially all "
                "conventional OTC agents."
            ),
            rationale=(
                "OTC use is under-reported at screening and several common agents "
                "(antacids, H2 antagonists, proton-pump inhibitors, NSAIDs) alter "
                "absorption or clearance."
            ),
            evidence=_CONVENTION_EVIDENCE,
            conditions=(
                "Permitted only with prior investigator approval, documented in source "
                "and on the CRF, if taken within the window."
            ),
        ),
        _entry(
            key="herbal_supplement",
            label="Herbal preparations and dietary supplements (including St John's Wort)",
            status=PROHIBITED,
            washout_days=HERBAL_WASHOUT_DAYS,
            basis=(
                "The window inherits the strong-inducer window rather than the OTC one, "
                "because St John's Wort is a strong CYP3A4/P-gp inducer and herbal "
                "products are not composition-controlled, so potency cannot be assumed "
                f"from the label. {HERBAL_WASHOUT_DAYS:.0f} days = the de-induction margin."
            ),
            rationale=(
                "Herbal products are the one comedication class whose actual content is "
                "unknowable at screening, so the window must assume the worst-case "
                "constituent."
            ),
            evidence=_CONVENTION_EVIDENCE,
        ),
        _entry(
            key="alcohol",
            label="Alcohol",
            status=PROHIBITED,
            washout_days=ALCOHOL_ABSTINENCE_HOURS / 24.0,
            basis=(
                f"Abstinence from {ALCOHOL_ABSTINENCE_HOURS:.0f} h before each dosing "
                "until discharge from the period. Chronic intake induces CYP2E1 and can "
                "alter hepatic blood flow, and alcohol can trigger dose dumping from "
                "modified-release formulations."
            ),
            rationale=(
                "Alcohol perturbs hepatic function, gastric emptying and (for "
                "modified-release products) the release mechanism itself, and its intake "
                "is not stable between periods."
            ),
            evidence=_CONVENTION_EVIDENCE,
        ),
        _entry(
            key="investigational_product",
            label="Any investigational product or participation in another clinical study",
            status=PROHIBITED,
            washout_days=investigational_days,
            basis=investigational_basis,
            rationale=(
                "Residual investigational drug, and any enzyme perturbation it caused, "
                "would be entirely unquantifiable in this study's data."
            ),
            evidence=_CONVENTION_EVIDENCE,
        ),
    ]

    # Rescue analgesia - the one thing a unit genuinely needs to be able to give.
    if allow_paracetamol:
        rows.append(_entry(
            key="paracetamol_rescue",
            label="Paracetamol (acetaminophen) as rescue analgesia",
            status=PERMITTED,
            washout_days=None,
            basis=(
                f"Conventional BE-unit allowance: up to "
                f"{PARACETAMOL_MAX_SINGLE_DOSE_G:.0f} g per dose and not more than "
                f"{PARACETAMOL_MAX_DAILY_G:.0f} g in 24 h."
            ),
            rationale=(
                "Paracetamol is cleared by glucuronidation/sulfation with essentially no "
                "CYP-mediated interaction at therapeutic doses, and none of this study "
                "drug's identified pathways overlap with it, so limited rescue use does "
                "not confound the exposure measurement."
            ),
            evidence=_CONVENTION_EVIDENCE,
            conditions=(
                f"Up to {PARACETAMOL_MAX_SINGLE_DOSE_G:.0f} g per dose, maximum "
                f"{PARACETAMOL_MAX_DAILY_G:.0f} g in 24 h, recorded with time and dose on "
                "the concomitant medication CRF; any use must be reported to the "
                "investigator."
            ),
        ))
    else:
        rows.append(_entry(
            key="paracetamol_rescue",
            label="Paracetamol (acetaminophen) as rescue analgesia",
            status=RESTRICTED,
            washout_days=None,
            basis="Not available as a free rescue allowance for this study drug.",
            rationale=paracetamol_reason,
            evidence=_CONVENTION_EVIDENCE,
            conditions=(
                "Only on investigator decision for a clinical need, with the time and "
                "dose recorded; the affected period must be flagged for the statistician "
                "as a potential PK confounder."
            ),
        ))

    rows.append(_entry(
        key="hormonal_contraception",
        label="Hormonal contraception",
        status=RESTRICTED if contraceptive_restricted else PERMITTED,
        washout_days=None,
        basis=(
            "Ethinylestradiol is a moderate CYP1A2 inhibitor, so for a CYP1A2 substrate "
            "its use must at least be recorded and kept constant across periods."
            if contraceptive_restricted else
            "Continuation is expected and does not interact with this drug's identified "
            "clearance pathways; contraception is separately REQUIRED for subjects of "
            "childbearing potential."
        ),
        rationale=(
            "Hormonal contraceptives inhibit CYP1A2, so for a CYP1A2 substrate they are "
            "a genuine covariate rather than a neutral background medication."
            if contraceptive_restricted else
            "Stopping established contraception for a study would create a safety risk "
            "with no analytical benefit."
        ),
        evidence=_CONVENTION_EVIDENCE,
        conditions=(
            "Permitted but must be recorded and the regimen kept unchanged from screening "
            "through the final PK sample."
        ),
    ))

    rows.append(_entry(
        key="topical_local",
        label="Topical, ophthalmic and dermatological preparations with negligible systemic absorption",
        status=PERMITTED,
        washout_days=None,
        basis="Systemic exposure is negligible, so no interaction with the analyte is credible.",
        rationale=(
            "Prohibiting these adds no analytical protection and creates avoidable "
            "protocol deviations."
        ),
        evidence=_CONVENTION_EVIDENCE,
        conditions="Must still be recorded on the concomitant medication CRF.",
    ))

    if victim_t_half_hours:
        hours = int(math.ceil(VICTIM_TERMINAL_PHASE_HALF_LIVES * victim_t_half_hours))
        rows.append(_entry(
            key="post_dose_window",
            label="Any new medication started after dosing",
            status=PROHIBITED,
            washout_days=None,
            basis=(
                f"The prohibition runs until the final PK sample of each period, i.e. "
                f"approximately {hours} h after dosing "
                f"({VICTIM_TERMINAL_PHASE_HALF_LIVES:.0f} x the study drug's "
                f"{victim_t_half_hours:.1f} h half-life), because the terminal phase is "
                "still being measured until then."
            ),
            rationale=(
                "A medication started after dosing but before the last sample alters the "
                "terminal slope and therefore AUC0-inf, even though the subject was fully "
                "eligible at dosing."
            ),
            evidence=_CONVENTION_EVIDENCE,
        ))
    return rows


# ---------------------------------------------------------------------------
# Study-drug half-life (from the extracted PK records)
# ---------------------------------------------------------------------------

_HOUR_UNITS = {"h", "hr", "hrs", "hour", "hours"}
_DAY_UNITS = {"d", "day", "days"}
_MINUTE_UNITS = {"min", "mins", "minute", "minutes"}


def _t_half_hours(pk_parameters: Optional[list[Any]]) -> Optional[float]:
    """Median terminal half-life in HOURS from the extracted PK records.

    Accepts the JSON-ish dicts used in the generation context as well as ORM
    records. Units are normalised explicitly - a days/hours mix-up here would
    silently change every washout number downstream.
    """
    if not pk_parameters:
        return None
    values: list[float] = []
    for rec in pk_parameters:
        if isinstance(rec, dict):
            name, value, unit = rec.get("parameter"), rec.get("value"), rec.get("unit")
        else:
            name = getattr(rec, "parameter", None)
            value, unit = getattr(rec, "value", None), getattr(rec, "unit", None)
        name = getattr(name, "value", name)  # tolerate an Enum
        if str(name or "").lower() not in ("t_half", "t1/2", "half_life"):
            continue
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue
        if value <= 0:
            continue
        unit_key = str(unit or "h").strip().lower()
        if unit_key in _DAY_UNITS:
            value *= 24.0
        elif unit_key in _MINUTE_UNITS:
            value /= 60.0
        elif unit_key not in _HOUR_UNITS:
            continue  # unrecognised unit - safer to drop than to guess
        values.append(value)
    if not values:
        return None
    values.sort()
    mid = len(values) // 2
    if len(values) % 2:
        return values[mid]
    return (values[mid - 1] + values[mid]) / 2.0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_PARACETAMOL_NAMES = {"paracetamol", "acetaminophen"}


def screen_interactions(
    label: Optional[dict[str, str]] = None,
    pk_parameters: Optional[list[Any]] = None,
    drug_inn: Optional[str] = None,
    *,
    design_key: Optional[str] = None,
    is_nti: Optional[bool] = None,
    healthy_volunteers: bool = True,
) -> dict[str, Any]:
    """Build the drug-specific concomitant-medication rules for THIS study.

    Parameters
    ----------
    label:
        The trimmed DailyMed label dict assembled for the protocol. The
        ``drug_interactions`` and ``contraindications`` sections (SPL LOINC
        34073-7 / 34070-3, added to literature_service for this engine) are the
        primary evidence; ``clinical_pharmacology`` and ``pharmacokinetics`` are
        the fallback.
    pk_parameters:
        Extracted PK records; the median terminal half-life sets how long after
        dosing the prohibition has to run (through the last PK sample).
    drug_inn:
        Used only to avoid absurdities such as permitting paracetamol as rescue
        analgesia in a paracetamol bioequivalence study.
    design_key:
        'crossover' | 'parallel' | 'replicate' - changes what a comedication
        actually does to the comparison, and therefore the wording of the rule.
    is_nti:
        Narrow-therapeutic-index victim: moderate perpetrators escalate from
        restricted to prohibited, because the acceptance limits are tightened.

    Returns
    -------
    A JSON-serialisable dict (persisted inside population_config, a JSONB
    column): detected ``pathways`` with provenance, the ``prohibited`` /
    ``restricted`` / ``permitted`` lists, the governing washout figures, and the
    ``assumptions`` the protocol must disclose.
    """
    design = (design_key or "crossover").strip().lower()
    nti = bool(is_nti)
    inn = (drug_inn or "").strip().lower()

    sections = {k: (label or {}).get(k, "") or "" for k in _SECTION_ORDER}
    detected = _detect(sections)

    pathways: list[dict[str, Any]] = []
    prohibited: list[dict[str, Any]] = []
    restricted: list[dict[str, Any]] = []
    unresolved: list[str] = []

    for pw in _CATALOGUE:
        hits = detected.get(pw.key)
        if not hits:
            continue
        evidence = hits[0]
        pathways.append({
            "key": pw.key,
            "label": pw.label,
            "kind": pw.kind,
            "mechanism": pw.mechanism,
            "victim_risk": pw.victim_risk,
            "caveat": pw.caveat,
            "evidence": [dict(h) for h in hits],
        })
        if pw.caveat:
            unresolved.append(f"{pw.label}: {pw.caveat}")
        for row in _pathway_entries(pw, evidence, is_nti=nti):
            (prohibited if row["status"] == PROHIBITED else restricted).append(row)
        # Diet/lifestyle rules ride on the pathway that makes them relevant, and
        # carry that pathway's label evidence with them - so grapefruit appears
        # for a CYP3A4 substrate and for nothing else.
        for d in pw.dietary:
            entry = _dietary_entry(d, evidence)
            if entry is None:
                continue
            if any(r["key"] == entry["key"] for r in prohibited + restricted):
                continue
            (prohibited if entry["status"] == PROHIBITED else restricted).append(entry)

    assumptions: list[dict[str, Any]] = []

    # No isoform named anywhere: fall back to a conservative, clearly-flagged
    # generic prohibition instead of silently emitting nothing. This entry MUST
    # NOT appear once a specific pathway is known - a generic clause is exactly
    # the defect this engine replaces.
    if not pathways:
        generic_ev = _detect_generic_cyp(sections) or dict(_CONVENTION_EVIDENCE)
        assumption_text = (
            "No specific CYP isoform or transporter could be identified from the "
            "product label, so a conservative pan-pathway prohibition is applied. The "
            "clinical pharmacologist must replace this with the drug's actual pathways "
            "before finalisation."
        )
        prohibited.append(_entry(
            key="generic_perpetrators",
            label="Strong inhibitors or inducers of any major CYP enzyme or drug transporter",
            status=PROHIBITED,
            washout_days=STRONG_INDUCER_WASHOUT_DAYS,
            basis=(
                "With the pathway unknown the window must assume the slowest-recovering "
                f"mechanism, which is induction: {STRONG_INDUCER_WASHOUT_DAYS:.0f} days."
            ),
            rationale=(
                "The label text available did not identify the clearance pathway, so no "
                "narrower prohibition can be justified from evidence."
            ),
            evidence=generic_ev,
            assumption=assumption_text,
        ))
        unresolved.append(assumption_text)
        assumptions.append({
            "constant": "pathway_identification",
            "value": "unresolved",
            "basis": assumption_text,
        })

    victim_t_half = _t_half_hours(pk_parameters)

    # Paracetamol is only a safe rescue analgesic when it does not share the
    # study drug's pathway - and obviously not when it IS the study drug.
    ugt_detected = any(p["key"] == "ugt" for p in pathways)
    allow_paracetamol = True
    paracetamol_reason = ""
    if inn in _PARACETAMOL_NAMES:
        allow_paracetamol = False
        paracetamol_reason = (
            "Paracetamol is the study analyte, so any rescue dose would be "
            "indistinguishable from the administered dose in the PK profile."
        )
    elif ugt_detected:
        allow_paracetamol = False
        paracetamol_reason = (
            "The study drug is glucuronidated, and paracetamol is cleared by the same "
            "UGT conjugation route at gram doses, so free rescue use could compete for "
            "the study drug's clearance pathway."
        )

    contraceptive_restricted = any(p["key"] == "cyp1a2" for p in pathways)

    permitted: list[dict[str, Any]] = []
    for row in _always_on_entries(
        victim_t_half_hours=victim_t_half,
        allow_paracetamol=allow_paracetamol,
        paracetamol_reason=paracetamol_reason,
        contraceptive_restricted=contraceptive_restricted,
        healthy_volunteers=healthy_volunteers,
    ):
        if row["status"] == PROHIBITED:
            prohibited.append(row)
        elif row["status"] == RESTRICTED:
            restricted.append(row)
        else:
            permitted.append(row)

    practical = [
        r["washout_days"] for r in prohibited + restricted
        if r["washout_days"] and r["washout_days"] <= IMPRACTICAL_WASHOUT_DAYS
    ]
    max_washout = max(practical) if practical else 0.0
    # The medication-history interview at screening must reach back at least as
    # far as the longest window, otherwise the exclusion cannot be verified.
    screening_window = max(
        [max_washout]
        + [e["washout_days"] for r in prohibited + restricted for e in r["extended_washout"]]
    )
    history_agents = [
        e for r in prohibited + restricted for e in r["history_exclusion"]
    ]

    assumptions.extend(_assumptions_used(prohibited + restricted))

    return {
        "drug_inn": inn,
        "design_key": design,
        "is_nti": nti,
        "pathways": pathways,
        "pathway_keys": [p["key"] for p in pathways],
        "prohibited": prohibited,
        "restricted": restricted,
        "permitted": permitted,
        "max_washout_days": float(max_washout),
        "screening_history_window_days": float(screening_window),
        "history_exclusion_agents": history_agents,
        "study_drug_t_half_hours": victim_t_half,
        "post_dose_restriction_hours": (
            int(math.ceil(VICTIM_TERMINAL_PHASE_HALF_LIVES * victim_t_half))
            if victim_t_half else None
        ),
        "unresolved": unresolved,
        "assumptions": assumptions,
        "summary": _summary(
            design, pathways, prohibited, restricted, max_washout, screening_window, nti
        ),
    }


def _assumptions_used(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The engine's own conventions, surfaced so the protocol DISCLOSES them.

    Only the ones actually driving an emitted row are listed - a disclosure the
    reader cannot tie to a rule is noise.
    """
    used_days = {r["washout_days"] for r in rows if r["washout_days"]}
    catalogue = [
        (REVERSIBLE_INHIBITOR_WASHOUT_DAYS, "REVERSIBLE_INHIBITOR_WASHOUT_DAYS",
         "Scheduling floor for reversible inhibitors; 5 x the inhibitor's own half-life "
         "is shorter than this for every conventional agent in the class."),
        (MECHANISM_BASED_INHIBITOR_WASHOUT_DAYS, "MECHANISM_BASED_INHIBITOR_WASHOUT_DAYS",
         "Recovery from mechanism-based inhibition requires enzyme re-synthesis "
         "(~7.5 days at a 36 h hepatic CYP3A4 degradation half-life), rounded up."),
        (STRONG_INDUCER_WASHOUT_DAYS, "STRONG_INDUCER_WASHOUT_DAYS",
         "CYP3A activity returns to baseline roughly 1-2 weeks after rifampicin is "
         "stopped; this doubles that recovery time as a protocol margin."),
        (MODERATE_INDUCER_WASHOUT_DAYS, "MODERATE_INDUCER_WASHOUT_DAYS",
         "Moderate induction resolves faster than strong induction."),
        (GRAPEFRUIT_WASHOUT_DAYS, "GRAPEFRUIT_WASHOUT_DAYS",
         "Intestinal CYP3A4 recovers in ~3 days (23 h degradation half-life); 7 days is "
         "the conventional margin."),
        (PRESCRIPTION_WASHOUT_DAYS, "PRESCRIPTION_WASHOUT_DAYS",
         "Conventional blanket window for prescription medicines in a BE study."),
        (HERBAL_WASHOUT_DAYS, "HERBAL_WASHOUT_DAYS",
         "Herbal products are not composition-controlled, so the window assumes the "
         "worst-case constituent (St John's Wort, a strong inducer)."),
        (OTC_WASHOUT_DAYS, "OTC_WASHOUT_DAYS",
         "Covers elimination of essentially all conventional OTC agents."),
        (INVESTIGATIONAL_WASHOUT_DAYS, "INVESTIGATIONAL_WASHOUT_DAYS",
         "30 days or 5 x the previous product's half-life, whichever is longer."),
    ]
    return [
        {"constant": name, "value": days, "basis": basis}
        for days, name, basis in catalogue if days in used_days
    ]


def _summary(
    design: str,
    pathways: list[dict[str, Any]],
    prohibited: list[dict[str, Any]],
    restricted: list[dict[str, Any]],
    max_washout: float,
    screening_window: float,
    is_nti: bool,
) -> str:
    """One paragraph for the protocol narrative - design-aware and specific."""
    if pathways:
        named = ", ".join(p["label"] for p in pathways)
        lead = (
            f"The concomitant medication rules below are derived from this product's own "
            f"label, which identifies {named} as the clearance pathway(s) at risk; classes "
            f"that cannot act on those pathways are deliberately NOT prohibited."
        )
    else:
        lead = (
            "The product label did not identify a specific clearance pathway, so a "
            "conservative pan-pathway prohibition is applied and flagged for the clinical "
            "pharmacologist to narrow."
        )
    if design == "parallel":
        design_text = (
            " In this parallel-group design a comedication perturbs one arm only and "
            "cannot be cancelled by a within-subject comparison, so it biases the "
            "Test/Reference ratio directly."
        )
    else:
        design_text = (
            " In this crossover design each subject is their own control, so a "
            "comedication taken in one period only - including during the washout "
            "between periods - shifts that period's exposure and biases the "
            "within-subject Test/Reference contrast; the prohibition therefore runs "
            "continuously from screening through the final PK sample of the last period, "
            "not merely on dosing days."
        )
    nti_text = (
        " Because this is a narrow-therapeutic-index product, moderate inhibitors and "
        "inducers are prohibited rather than merely restricted."
        if is_nti else ""
    )
    # The class window and the screening-history window are NOT the same number.
    # A class window is what a subject must have been free of; the history window
    # must additionally reach back far enough to detect the individually-named
    # long-lived agents (norfluoxetine, terbinafine, phenobarbital), otherwise the
    # exclusion is unverifiable. Reporting only the class window here would
    # contradict the figure ddi_directive() states to the model.
    tail = (
        f" {len(prohibited)} class(es) are prohibited and {len(restricted)} restricted; "
        f"the longest routine class washout is {max_washout:.0f} days."
    )
    if screening_window > max_washout:
        tail += (
            f" The screening medication history must nevertheless reach back at least "
            f"{screening_window:.0f} days, because individually-named agents in these "
            f"classes are eliminated too slowly for the class window to detect them."
        )
    else:
        tail += (
            f" That is also the minimum period the screening medication history must "
            f"cover."
        )
    return lead + design_text + nti_text + tail


def ddi_directive(result: dict[str, Any]) -> str:
    """Render the locked concomitant-medication rules as prompt text.

    Mirrors ``stratification.stratification_directive`` and
    ``design_decisions.design_directive``: these are settled facts the model
    narrates into the exclusion criteria and the concomitant-medication section.
    It must not add drug classes, invent washout periods, or re-decide a status.
    """
    if not result or not (result.get("prohibited") or result.get("restricted")):
        return ""

    lines = [
        "PROHIBITED AND RESTRICTED CONCOMITANT MEDICATIONS - these are ALREADY "
        "DETERMINED for this specific drug. Narrate them EXACTLY; do NOT add drug "
        "classes, do NOT change a status, and do NOT invent or round any washout "
        "period. In particular, do NOT insert a generic 'CYP3A4 inhibitors or "
        "inducers' clause unless CYP3A4 appears in the list below.",
        f"Basis: {result.get('summary', '')}",
    ]

    pathways = result.get("pathways") or []
    if pathways:
        lines.append("Clearance pathways identified from the product label:")
        for p in pathways:
            ev = (p.get("evidence") or [{}])[0]
            src = ev.get("source", "")
            lines.append(
                f"  - {p['label']} ({p['kind']}) - evidence: {src}"
                + (f"; label text: \"{ev.get('quote', '')}\"" if ev.get("quote") else "")
            )
            if p.get("caveat"):
                lines.append(f"    Caveat to state honestly: {p['caveat']}")

    for status, heading in (
        ("prohibited", "PROHIBITED (exclusionary - a subject taking one is not eligible, "
                       "and starting one on study is a withdrawal criterion)"),
        ("restricted", "RESTRICTED (allowed only outside the stated window, with the use "
                       "documented and investigator-approved)"),
        ("permitted", "PERMITTED (explicitly allowed - state these so the site does not "
                      "over-restrict)"),
    ):
        rows = result.get(status) or []
        if not rows:
            continue
        lines.append(heading + ":")
        for i, r in enumerate(rows, 1):
            bits = [f"  {i}. {r['label']}"]
            if r.get("washout_days"):
                bits.append(f" - washout {r['washout_days']:g} days before first dose")
            if r.get("examples"):
                bits.append(f" (e.g. {', '.join(r['examples'])})")
            lines.append("".join(bits))
            lines.append(f"     Why: {r['rationale']}")
            if r.get("washout_basis"):
                lines.append(f"     Washout basis: {r['washout_basis']}")
            if r.get("conditions"):
                lines.append(f"     Conditions: {r['conditions']}")
            for ext in r.get("extended_washout") or []:
                lines.append(
                    f"     LONGER individual washout - {ext['name']}: "
                    f"{ext['washout_days']} days ({ext['basis']})"
                )
            for hist in r.get("history_exclusion") or []:
                lines.append(
                    f"     NOT washable - {hist['name']}: {hist['action']} "
                    f"({hist['basis']})"
                )
            ev = r.get("evidence") or {}
            if ev.get("source"):
                lines.append(f"     Cite as: {ev['source']}")
            if r.get("assumption"):
                lines.append(f"     DISCLOSE this assumption: {r['assumption']}")

    if result.get("post_dose_restriction_hours"):
        lines.append(
            f"Post-dose window: the prohibition continues for at least "
            f"{result['post_dose_restriction_hours']} hours after each dose (through the "
            f"final PK sample of the period)."
        )
    lines.append(
        f"Screening medication history must cover at least the preceding "
        f"{result.get('screening_history_window_days', 0):.0f} days."
    )
    return "\n".join(lines)
