"""ICH E3 Clinical Study Report section taxonomy + per-section AI guidance.

The CSR narrative is generated section-by-section (parallel to
``ai_service._SECTION_CONFIGS`` for protocols), but with one hard rule baked into
every prompt: **the model writes prose only and must never invent or alter a
number.** Every results figure (disposition counts, PK/NCA values, GMR, 90% CI,
CV, verdict, AE counts) is computed deterministically upstream
(:mod:`services.csr_results`) and passed to the model as read-only facts. The
model's job is to narrate those locked numbers in ICH E3 style, citing them
verbatim.

Both the generator (:mod:`services.csr_pipeline`) and the exporter
(:mod:`services.csr_export`) import ``CSR_SECTION_ORDER`` / ``CSR_SECTION_TITLES``
so the narrative and the rendered document stay in lock-step.
"""
from __future__ import annotations

# Front matter ("title" -> cover subtitle, "synopsis" -> unnumbered) then the
# numbered ICH E3 body. "references" closes it.
CSR_SECTION_ORDER: list[str] = [
    "title",
    "synopsis",
    "introduction",
    "study_objectives",
    "investigational_plan",
    "study_population",
    "pharmacokinetic_results",
    "bioequivalence_assessment",
    "safety_results",
    "discussion_conclusions",
    "references",
]

CSR_SECTION_TITLES: dict[str, str] = {
    "title": "Title",
    "synopsis": "Synopsis",
    "introduction": "Introduction",
    "study_objectives": "Study Objectives",
    "investigational_plan": "Investigational Plan",
    "study_population": "Study Population and Subject Disposition",
    "pharmacokinetic_results": "Pharmacokinetic Results",
    "bioequivalence_assessment": "Bioequivalence Assessment",
    "safety_results": "Safety Evaluation",
    "discussion_conclusions": "Discussion and Overall Conclusions",
    "references": "References",
}

# Prepended to EVERY section's system prompt — the non-negotiable boundary.
NUMBERS_LOCKED_DIRECTIVE = (
    "You are writing one section of a Clinical Study Report (CSR) that follows the "
    "ICH E3 structure. A COMPUTED RESULTS object is provided to you as verified, "
    "final facts.\n"
    "ABSOLUTE RULES:\n"
    "1. Every numeric result — subject counts, PK parameters, geometric mean ratios, "
    "90% confidence intervals, CV%, the bioequivalence verdict, adverse-event counts — "
    "is ALREADY DETERMINED and given to you. You MUST cite these numbers exactly as "
    "provided. You must NEVER invent, estimate, round differently, recompute, or alter "
    "any number.\n"
    "2. If a needed figure is not in the provided results, write '[not available]' "
    "rather than inventing one.\n"
    "3. Do not draw a bioequivalence conclusion that contradicts the provided verdict.\n"
    "4. Output clean semantic HTML only (<p>, <h3>, <ul>/<li>) — no markdown, "
    "no <html>/<body> wrapper, no commentary about these instructions.\n"
    "5. Do NOT emit HTML tables. Every results table (subject disposition, PK "
    "summary, bioequivalence, adverse events) is rendered automatically by the "
    "report itself immediately after the relevant section. Narrate the figures in "
    "prose and never reproduce them as a <table> — doing so duplicates the report's "
    "own tables.\n"
    "Write in the measured, factual past tense of a regulatory clinical study report."
)

# Per-section guidance. ``expertise`` frames the author; ``guidance`` says what the
# section must cover, always in terms of the provided locked results.
CSR_SECTION_CONFIGS: dict[str, dict[str, str]] = {
    "title": {
        "expertise": "a regulatory medical writer",
        "guidance": (
            "Return ONE plain-text sentence (no HTML) giving the full study title in "
            "the style of a completed BE study report: single-dose, randomized, "
            "crossover or parallel as indicated by the design, naming the drug and "
            "that it evaluated bioequivalence of test vs reference in healthy adults."
        ),
    },
    "synopsis": {
        "expertise": "a regulatory medical writer",
        "guidance": (
            "Write the CSR synopsis: a compact structured summary covering objective, "
            "methodology (design, N enrolled/analysed, treatments), the primary PK "
            "results (Cmax and AUC point estimates with 90% CIs), the bioequivalence "
            "conclusion (state the provided verdict), and a one-line safety statement. "
            "Use the exact numbers from the results object."
        ),
    },
    "introduction": {
        "expertise": "a clinical pharmacologist",
        "guidance": (
            "Briefly introduce the drug and the regulatory rationale for a "
            "bioequivalence study (generic/reference comparison, the relevant FDA/ICH "
            "guidance). Two or three short paragraphs; no results here."
        ),
    },
    "study_objectives": {
        "expertise": "a clinical pharmacologist",
        "guidance": (
            "State the primary objective (demonstrate bioequivalence of test vs "
            "reference by comparing Cmax and AUC) and any secondary objective "
            "(characterise the PK profile, assess safety/tolerability)."
        ),
    },
    "investigational_plan": {
        "expertise": "a clinical trial methodologist",
        "guidance": (
            "Describe the overall study design and plan from the provided design "
            "(crossover/parallel/replicate), the treatments (test vs reference), "
            "randomisation and the analysis approach: log-transformed ANOVA for the "
            "crossover with the 80.00-125.00% acceptance range (or the scaled/NTI "
            "limits given). State the pre-specified sample size if provided."
        ),
    },
    "study_population": {
        "expertise": "a clinical study report author",
        "guidance": (
            "Report subject disposition and analysis populations using the disposition "
            "counts in the results (number of subjects, per sequence, any BLQ handling "
            "note). Reference the demographic/eligibility framing from the protocol. "
            "State how many subjects contributed to the PK analysis. The subject-"
            "disposition table is rendered automatically by the report; narrate in "
            "prose only and do not emit a table."
        ),
    },
    "pharmacokinetic_results": {
        "expertise": "a pharmacokineticist",
        "guidance": (
            "Present the pharmacokinetic results. Summarise the primary exposure metrics "
            "(Cmax, AUC0-t, AUC0-inf) for test and reference, and, when an NCA table is "
            "provided, note the derivation (linear-up/log-down AUC, best-fit lambda_z). "
            "The detailed per-metric statistical table is rendered separately by the "
            "report; here, narrate what the numbers show. Cite figures exactly."
        ),
    },
    "bioequivalence_assessment": {
        "expertise": "a bioequivalence biostatistician",
        "guidance": (
            "State, for each co-primary metric (Cmax and AUC0-inf; AUC0-t supportive), "
            "the geometric mean ratio and its 90% confidence interval exactly as given, "
            "and whether it falls within the acceptance limits. Then state the overall "
            "bioequivalence conclusion — matching the provided verdict exactly. If the "
            "verdict is 'indeterminate', say bioequivalence could not be concluded for "
            "the stated reason. Do not contradict the provided numbers. The "
            "bioequivalence results table (GMR and 90% CI per metric) is rendered "
            "automatically by the report; narrate in prose only and do not emit a table."
        ),
    },
    "safety_results": {
        "expertise": "a drug-safety physician",
        "guidance": (
            "Summarise safety from the provided adverse-event summary: total treatment-"
            "emergent AEs, by treatment and severity, any serious AEs, and that no "
            "deaths/withdrawals occurred unless the data indicate otherwise. If no AE "
            "data were provided, state that safety data were not included in this "
            "analysis and that safety will be reported separately. When AE data are "
            "present, the adverse-event table is rendered automatically by the report; "
            "narrate in prose only and do not emit a table."
        ),
    },
    "discussion_conclusions": {
        "expertise": "a clinical pharmacologist",
        "guidance": (
            "Provide a brief discussion integrating the PK and BE findings and the "
            "safety summary, then a clear overall conclusion consistent with the "
            "provided bioequivalence verdict. Do not overstate; do not introduce new "
            "numbers."
        ),
    },
    "references": {
        "expertise": "a regulatory medical writer",
        "guidance": (
            "List the applicable regulatory references (e.g. ICH E3; the relevant FDA "
            "bioequivalence guidance; ICH M13A) as a short numbered list. No study "
            "results here."
        ),
    },
}
