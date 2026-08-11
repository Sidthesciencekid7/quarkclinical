"""
Literature service - PubMed and DailyMed fetching with 7-day cache, plus
regulatory-assessment-report awareness (FDA / EMA / TGA / PMDA / NMPA).

Cache strategy:
  - Cache key: (drug_id, source)
  - Hit:  any unexpired row exists -> return all rows for that key
  - Miss: call external API -> persist rows -> return results

REGULATORY ASSESSMENT REPORTS - WHAT THIS MODULE DOES AND DOES NOT DO
---------------------------------------------------------------------
The CV% and PK parameters that drive sample size come from PubMed abstracts and
the DailyMed label. A practising clinical pharmacologist pointed out that the
BEST source for healthy-volunteer PK and within-subject CV is the regulatory
assessment report, because it tabulates the actual registration-study data:

  * FDA  - Clinical Pharmacology / Biopharmaceutics review (Drugs@FDA)
  * EMA  - EPAR (European Public Assessment Report)
  * TGA  - AusPAR (Australian Public Assessment Report)
  * PMDA - Japanese review report
  * NMPA - Chinese registration review disclosure (CDE)

NONE of those five has a clean programmatic API that returns PK numbers. They
are PDF portals (Drugs@FDA serves scanned/native PDFs; EPAR/AusPAR are PDFs;
PMDA and NMPA are largely Japanese/Chinese-language PDFs). This module
therefore does NOT pretend to retrieve them. What it actually does is:

  1. Run a SECOND, regulatory-targeted PubMed query alongside the existing PK
     query, so peer-reviewed papers that report or analyse registration-study
     data - including Chinese and Japanese healthy-volunteer / ethnic-bridging
     PK studies - enter the evidence set at all. Those papers ARE indexed in
     PubMed; the reports themselves are not.
  2. Detect, deterministically, which agencies' reports the RETRIEVED abstracts
     actually reference, with the PMID and the matched phrase as evidence.
  3. Emit a locked directive (``regulatory_sources_directive``) telling the
     generator to cite only what was retrieved, to attribute it to the PMID of
     the paper that reported it, and never to present an assessment report as a
     document this system read.

So the protocol can say "PMID 12345678 reports a within-subject CV of 21%,
which that paper attributes to the EMA EPAR" - which is true - and can never
say "per the EMA EPAR", which would be a source claim this system cannot back.

WHY THE SECOND QUERY IS GATED (do not remove this without reading)
------------------------------------------------------------------
pytest-httpx 0.36 defaults to ``can_send_already_matched_responses=False`` AND
``assert_all_requests_were_expected=True``. 34 existing tests register EXACTLY
ONE esearch response, so a second esearch would (a) find no unmatched response
and raise, and (b) fail the test at teardown as an unexpected request - even if
the exception were swallowed. The regulatory query is therefore opt-in via the
``include_regulatory`` argument, whose default policy (``regulatory_search_enabled``)
is OFF under pytest and ON in real deployments. Tests that want the two-call
path pass ``include_regulatory=True`` explicitly and register two responses.
"""

import json
import logging
import os
import re
import sys
import uuid
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, TypedDict

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import Drug, LiteratureCache, LiteratureSource

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PUBMED_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
DAILYMED_BASE = "https://dailymed.nlm.nih.gov/dailymed/services/v2"
CTGOV_BASE = "https://clinicaltrials.gov/api/v2"
CACHE_TTL_DAYS = 7
PUBMED_MAX_RESULTS = 20
# Public, no API key. Searched by intervention name ONLY (no condition
# filter) so the 7-day cache is valid regardless of which indication a later
# caller asks about for the SAME drug — see fetch_clinicaltrials's docstring.
CTGOV_MAX_RESULTS = 20

# The regulatory-targeted query is a supplement, not a replacement: it adds a
# small, high-signal tail to the primary PK result set. The combined cap bounds
# the single efetch call and the size of the extraction prompt.
REGULATORY_MAX_RESULTS = 10
PUBMED_TOTAL_MAX_RESULTS = 30

# Environment switch for the regulatory-targeted PubMed query.
#   unset  -> enabled outside pytest, disabled under pytest (see module docstring)
#   "0"/"false"/"no"  -> force off
#   "1"/"true"/"yes"  -> force on (including under pytest)
REGULATORY_SEARCH_ENV = "REGULATORY_PUBMED_SEARCH"

# Status recorded by fetch_pubmed for the most recent call, so the plan can
# state honestly whether the regulatory query actually ran.
REG_STATUS_EXECUTED = "executed"
REG_STATUS_DISABLED = "disabled"
REG_STATUS_CACHE = "cache"
REG_STATUS_FAILED = "failed"
REG_STATUS_NONE = "not_run"

# SPL section codes we care about
_SPL_SECTIONS = {
    "34090-1": "clinical_pharmacology",
    "43682-4": "pharmacokinetics",
    "34067-9": "indications_usage",
    # Route/device/injection-site signals for the study-design decision engine
    # (services/design_decisions.py) — the label's own stated administration
    # instructions, not a guess from a curated drug-class table.
    "34068-7": "dosage_and_administration",
    "34069-5": "how_supplied",
    # Interaction/contraindication text for the DDI screening engine
    # (services/ddi_screening.py) - a named interacting agent HERE is the
    # strongest evidence that a clearance pathway is real and clinically
    # consequential for this product, so the drug-specific prohibited
    # comedication list is grounded in the label rather than model recall.
    "34073-7": "drug_interactions",
    "34070-3": "contraindications",
}

# ---------------------------------------------------------------------------
# Return-type schemas
# ---------------------------------------------------------------------------


class PubMedRecord(TypedDict):
    pmid: str
    title: str
    abstract: str


class DailyMedRecord(TypedDict):
    setid: str
    title: str
    sections: dict[str, str]  # section_name → plain text


class ClinicalTrialRecord(TypedDict):
    nct_id: str
    title: str
    status: str                    # e.g. "COMPLETED", "RECRUITING", "TERMINATED"
    phase: str                     # e.g. "PHASE3"; "" if not reported (observational)
    conditions: list[str]
    interventions: list[str]
    sex: str                       # "ALL" | "MALE" | "FEMALE" | ""
    minimum_age: str                # e.g. "18 Years"
    maximum_age: str
    healthy_volunteers: bool | None
    eligibility_criteria: str      # raw free text, "Inclusion Criteria:\n...Exclusion Criteria:\n..."


# ---------------------------------------------------------------------------
# Regulatory assessment reports - catalogue, query, detection, directive
# ---------------------------------------------------------------------------
#
# This is the deterministic knowledge layer for "where does better healthy-
# volunteer PK / within-subject CV come from". It follows the same contract as
# services/stratification.py: plan_regulatory_sources() returns a JSON-
# serialisable dict of FACTS, and regulatory_sources_directive() renders it as
# prompt text the model may narrate but never extend.


@dataclass(frozen=True)
class RegulatorySource:
    """One agency's public assessment-report programme."""

    key: str
    agency: str
    region: str
    document: str        # short name used in prose
    full_name: str       # the document's formal title
    provides: str        # what a reviewer would actually get out of it
    portal: str          # where a human obtains the document
    retrieval_note: str  # honest statement of why this app cannot fetch it

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_REGULATORY_SOURCES: tuple[RegulatorySource, ...] = (
    RegulatorySource(
        key="fda",
        agency="FDA (United States)",
        region="US",
        document="FDA Clinical Pharmacology / Biopharmaceutics Review",
        full_name=(
            "Clinical Pharmacology and Biopharmaceutics Review(s), part of the "
            "Drugs@FDA approval package (NDA/BLA)"
        ),
        provides=(
            "Individual and summary healthy-volunteer PK from the registration "
            "programme, the sponsor's bioequivalence/biocomparability analyses, "
            "and the reviewer's own within-subject CV and variance estimates - "
            "typically the most complete public source of intra-subject CV."
        ),
        portal="https://www.accessdata.fda.gov/scripts/cder/daf/",
        retrieval_note=(
            "Not retrieved by this system: Drugs@FDA publishes reviews as "
            "per-application PDF bundles (often scanned) with no PK-parameter API."
        ),
    ),
    RegulatorySource(
        key="ema",
        agency="EMA (European Union)",
        region="EU",
        document="EMA EPAR",
        full_name="European Public Assessment Report (EPAR), scientific discussion",
        provides=(
            "The CHMP scientific discussion tabulates pivotal PK study designs, "
            "healthy-volunteer exposure parameters, and the geometric mean ratios "
            "with 90% confidence intervals from the bioequivalence or "
            "biosimilar comparability studies."
        ),
        portal="https://www.ema.europa.eu/en/medicines",
        retrieval_note=(
            "Not retrieved by this system: EPARs are per-product PDF documents "
            "on ema.europa.eu with no structured PK endpoint."
        ),
    ),
    RegulatorySource(
        key="tga",
        agency="TGA (Australia)",
        region="AU",
        document="TGA AusPAR",
        full_name="Australian Public Assessment Report (AusPAR)",
        provides=(
            "A delegate-level summary of the clinical pharmacology data package, "
            "frequently restating the pivotal healthy-volunteer PK tables and the "
            "bioequivalence outcome for products where the FDA/EMA documents are "
            "terse or redacted."
        ),
        portal="https://www.tga.gov.au/resources/auspar",
        retrieval_note=(
            "Not retrieved by this system: AusPARs are PDF documents on tga.gov.au "
            "with no machine-readable PK export."
        ),
    ),
    RegulatorySource(
        key="pmda",
        agency="PMDA (Japan)",
        region="JP",
        document="PMDA Review Report",
        full_name=(
            "Pharmaceuticals and Medical Devices Agency review report / "
            "Common Technical Document summary (shinsa hokokusho)"
        ),
        provides=(
            "Japanese healthy-volunteer PK, and the ethnic-bridging comparison "
            "against the non-Japanese registration data (ICH E5) - the primary "
            "public source for whether exposure differs in a Japanese population."
        ),
        portal="https://www.pmda.go.jp/english/review-services/reviews/0001.html",
        retrieval_note=(
            "Not retrieved by this system: review reports are PDFs, most of them "
            "Japanese-language only, with no PK-parameter API."
        ),
    ),
    RegulatorySource(
        key="nmpa",
        agency="NMPA (China)",
        region="CN",
        document="NMPA / CDE registration review disclosure",
        full_name=(
            "National Medical Products Administration (formerly CFDA) Center for "
            "Drug Evaluation technical review disclosure"
        ),
        provides=(
            "Chinese healthy-volunteer PK and bioequivalence data from the local "
            "registration or consistency-evaluation programme, including "
            "within-subject CV for generics assessed under the Chinese BE guidance."
        ),
        portal="https://www.nmpa.gov.cn/",
        retrieval_note=(
            "Not retrieved by this system: CDE disclosures are Chinese-language "
            "PDF/HTML postings with no stable English API."
        ),
    ),
)

_REGULATORY_BY_KEY: dict[str, RegulatorySource] = {s.key: s for s in _REGULATORY_SOURCES}

# Phrases that, in a PubMed title/abstract, are strong evidence the paper is
# reporting or analysing a named agency's assessment report. Deliberately
# multi-word or unambiguous acronyms: a bare "FDA" is useless (every third
# abstract says "FDA-approved") and a bare "TGA" collides with
# thermogravimetric analysis and transposition of the great arteries.
_REGULATORY_PATTERNS: dict[str, tuple[str, ...]] = {
    "fda": (
        r"drugs\s*@\s*fda",
        r"clinical pharmacology and biopharmaceutics review",
        r"biopharmaceutics? review",
        r"fda (?:clinical pharmacology |medical |statistical |multi-?disciplinary )?review",
        r"summary basis of approval",
        r"fda approval package",
        r"(?:nda|bla) (?:no\.?\s*)?\d{5,6}",
        r"new drug application",
        r"biologics license application",
        r"center for drug evaluation and research",
    ),
    "ema": (
        r"\bepar\b",
        r"european public assessment report",
        r"\bchmp\b",
        r"committee for medicinal products for human use",
        r"european medicines agency",
        r"ema assessment report",
    ),
    "tga": (
        r"\bauspar\b",
        r"australian public assessment report",
        r"therapeutic goods administration",
    ),
    "pmda": (
        r"\bpmda\b",
        r"pharmaceuticals and medical devices agency",
        r"japanese review report",
        r"\bmhlw\b",
        r"ministry of health, labour and welfare",
    ),
    "nmpa": (
        r"\bnmpa\b",
        r"national medical products administration",
        r"china food and drug administration",
        r"\bcfda\b",
        r"\bsfda\b",
    ),
}

_REGULATORY_COMPILED: dict[str, tuple[re.Pattern, ...]] = {
    key: tuple(re.compile(p, re.IGNORECASE) for p in pats)
    for key, pats in _REGULATORY_PATTERNS.items()
}

# Healthy-volunteer signal. The pharmacologist's ask was specifically about
# "PK in drugs for FDA in healthy volunteers" - a registration PK study in
# patients is a different animal and must not be conflated with one.
_HEALTHY_VOLUNTEER_PATTERN = re.compile(
    r"healthy\s+(?:adult\s+|male\s+|female\s+|young\s+|chinese\s+|japanese\s+|korean\s+)*"
    r"(?:volunteer|subject|participant|male|female|adult|men|women)",
    re.IGNORECASE,
)

# Regional registration/bridging populations the ask named explicitly.
_REGIONAL_POPULATION_PATTERNS: dict[str, re.Pattern] = {
    "chinese": re.compile(r"\bchinese\b|\bhan\s+chinese\b", re.IGNORECASE),
    "japanese": re.compile(r"\bjapanese\b", re.IGNORECASE),
}

# --- the regulatory-targeted PubMed query -----------------------------------
# Arm 1: papers that explicitly report/analyse an assessment report.
_REGULATORY_REPORT_TERMS: tuple[str, ...] = (
    '"public assessment report"[tiab]',
    "EPAR[tiab]",
    "AusPAR[tiab]",
    '"assessment report"[tiab]',
    '"review report"[tiab]',
    '"Drugs@FDA"[tiab]',
    '"approval package"[tiab]',
    '"summary basis of approval"[tiab]',
    '"new drug application"[tiab]',
    '"biologics license application"[tiab]',
    '"marketing authorisation"[tiab]',
    '"marketing authorization"[tiab]',
    '"regulatory submission"[tiab]',
    '"registration study"[tiab]',
    '"registration trial"[tiab]',
)
# Arm 2: the regional healthy-volunteer registration/bridging PK literature
# (Chinese and Japanese studies were called out by name).
_REGIONAL_HV_TERMS: tuple[str, ...] = (
    '"healthy Chinese"[tiab]',
    '"healthy Japanese"[tiab]',
    '"Chinese subjects"[tiab]',
    '"Japanese subjects"[tiab]',
    '"Chinese volunteers"[tiab]',
    '"Japanese volunteers"[tiab]',
    '"East Asian"[tiab]',
    '"ethnic sensitivity"[tiab]',
    '"ethnic difference"[tiab]',
    '"bridging study"[tiab]',
)
# Both arms are then intersected with a PK context so the query cannot drift
# into pure regulatory-policy commentary with no numbers in it.
_PK_CONTEXT_TERMS: tuple[str, ...] = (
    "pharmacokinetics[MeSH Terms]",
    "pharmacokinetic*[tiab]",
    "bioequivalence[tiab]",
    '"healthy volunteers"[tiab]',
    '"healthy subjects"[tiab]',
)

_SNIPPET_RADIUS = 110


def build_regulatory_query(drug_inn: str) -> str:
    """Build the regulatory-assessment-report PubMed term for *drug_inn*.

    Pure and deterministic so the exact string can be asserted in tests and
    recorded in the plan for audit - a reviewer must be able to see precisely
    what was searched.
    """
    drug = (drug_inn or "").strip()
    source_arm = " OR ".join(_REGULATORY_REPORT_TERMS + _REGIONAL_HV_TERMS)
    pk_arm = " OR ".join(_PK_CONTEXT_TERMS)
    return f"{drug}[tiab] AND ({source_arm}) AND ({pk_arm})"


def regulatory_search_enabled() -> bool:
    """Default policy for whether the second (regulatory) esearch call runs.

    ON in real deployments, OFF under pytest. See the module docstring: 34
    existing tests register exactly one esearch response, and pytest-httpx 0.36
    both refuses to replay a matched response and fails the test at teardown for
    an unexpected request, so an unconditional second call would break them all.
    Tests that want this path pass ``include_regulatory=True`` explicitly.
    """
    raw = os.getenv(REGULATORY_SEARCH_ENV)
    if raw is not None and raw.strip() != "":
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return "pytest" not in sys.modules


def _snippet(text: str, start: int, end: int) -> str:
    """Short, single-line context window around a match, for the audit trail."""
    lo = max(0, start - _SNIPPET_RADIUS)
    hi = min(len(text), end + _SNIPPET_RADIUS)
    frag = " ".join(text[lo:hi].split())
    if lo > 0:
        frag = "..." + frag
    if hi < len(text):
        frag = frag + "..."
    return frag


def detect_regulatory_sources(text: str) -> dict[str, list[str]]:
    """Return {source_key: [matched phrases]} for one piece of text.

    Only the phrase that actually matched is returned, so nothing downstream can
    claim an agency was referenced without being able to show the words.
    """
    found: dict[str, list[str]] = {}
    if not text:
        return found
    for key, patterns in _REGULATORY_COMPILED.items():
        hits: list[str] = []
        for pat in patterns:
            m = pat.search(text)
            if m:
                phrase = m.group(0).strip()
                if phrase.lower() not in {h.lower() for h in hits}:
                    hits.append(phrase)
        if hits:
            found[key] = hits
    return found


def plan_regulatory_sources(
    *,
    drug_inn: str,
    records: list[PubMedRecord] | list[dict[str, Any]] | None = None,
    search_status: str = REG_STATUS_NONE,
    query: str | None = None,
    regulatory_query_pmids: list[str] | None = None,
) -> dict[str, Any]:
    """Deterministic plan describing which regulatory assessment reports are
    actually evidenced by the retrieved literature.

    Parameters
    ----------
    drug_inn:
        The INN the search was run for.
    records:
        The PubMed records actually retrieved (pmid/title/abstract). Detection
        runs over title + abstract only - never over anything this system did
        not fetch.
    search_status:
        One of the ``REG_STATUS_*`` constants, recording what the
        regulatory-targeted query did on the call that produced ``records``.
    query:
        The exact PubMed term used, when it was run.
    regulatory_query_pmids:
        PMIDs that came specifically from the regulatory-targeted query.

    Returns a JSON-serialisable dict. The contract every caller relies on:
    ``sources[i]["referenced"]`` is True only when a retrieved abstract contains
    a matched phrase, and ``sources[i]["retrieved_document"]`` is ALWAYS False,
    because this system never downloads the assessment report itself.
    """
    recs = list(records or [])
    reg_pmids = set(regulatory_query_pmids or [])

    evidence: dict[str, list[dict[str, Any]]] = {k: [] for k in _REGULATORY_BY_KEY}
    hv_pmids: list[str] = []
    regional: dict[str, list[str]] = {k: [] for k in _REGIONAL_POPULATION_PATTERNS}

    for rec in recs:
        pmid = str(rec.get("pmid") or "").strip()
        title = str(rec.get("title") or "")
        abstract = str(rec.get("abstract") or "")
        blob = f"{title} {abstract}".strip()
        if not blob:
            continue

        for key, phrases in detect_regulatory_sources(blob).items():
            # Re-locate the first phrase to build an auditable snippet.
            m = re.search(re.escape(phrases[0]), blob, re.IGNORECASE)
            evidence[key].append(
                {
                    "pmid": pmid,
                    "title": title,
                    "matched_phrases": phrases,
                    "snippet": _snippet(blob, m.start(), m.end()) if m else "",
                    "from_regulatory_query": pmid in reg_pmids,
                }
            )

        is_hv = bool(_HEALTHY_VOLUNTEER_PATTERN.search(blob))
        if is_hv and pmid:
            hv_pmids.append(pmid)
            for region_key, pat in _REGIONAL_POPULATION_PATTERNS.items():
                if pat.search(blob):
                    regional[region_key].append(pmid)

    sources: list[dict[str, Any]] = []
    for src in _REGULATORY_SOURCES:
        hits = evidence[src.key]
        entry = src.to_dict()
        entry.update(
            {
                "referenced": bool(hits),
                # Always False, by construction: the catalogue describes where a
                # human finds the document; this system reads only PubMed and
                # DailyMed. Nothing downstream may upgrade this.
                "retrieved_document": False,
                "evidence": hits,
                "evidence_pmids": [h["pmid"] for h in hits if h["pmid"]],
            }
        )
        sources.append(entry)

    referenced = [s["key"] for s in sources if s["referenced"]]
    not_referenced = [s["key"] for s in sources if not s["referenced"]]

    if referenced:
        names = ", ".join(_REGULATORY_BY_KEY[k].document for k in referenced)
        summary = (
            f"{len(referenced)} of {len(_REGULATORY_SOURCES)} regulatory assessment-report "
            f"programmes are referenced by the retrieved literature for {drug_inn}: {names}. "
            "The reports themselves were not retrieved; the citable evidence is the "
            "PubMed record that references them."
        )
    else:
        summary = (
            f"No retrieved abstract for {drug_inn} references an FDA, EMA, TGA, PMDA or "
            "NMPA assessment report. The protocol must not cite any of them."
        )

    return {
        "drug_inn": drug_inn,
        "search_status": search_status,
        "search_executed": search_status == REG_STATUS_EXECUTED,
        "query": query,
        "records_scanned": len(recs),
        "regulatory_query_pmids": sorted(reg_pmids),
        "sources": sources,
        "referenced_sources": referenced,
        "unreferenced_sources": not_referenced,
        "healthy_volunteer_pmids": sorted(set(hv_pmids)),
        "regional_healthy_volunteer_pmids": {
            k: sorted(set(v)) for k, v in regional.items() if v
        },
        "summary": summary,
        "citation_rule": (
            "Cite only what was retrieved. A regulatory assessment report may be "
            "mentioned ONLY as the source a retrieved PubMed article itself names, "
            "attributed to that article's PMID. Never write 'per the FDA review', "
            "'per the EPAR', 'per the AusPAR', 'per the PMDA review report' or 'per "
            "the NMPA review' as though this protocol consulted the document."
        ),
    }


def regulatory_sources_directive(plan: dict[str, Any] | None) -> str:
    """Render the regulatory-source plan as locked prompt text.

    Mirrors ``stratification.stratification_directive``: settled facts to
    narrate, never a menu to re-decide. Returns "" when there is nothing to say
    so the caller can concatenate unconditionally.
    """
    if not plan:
        return ""

    sources = plan.get("sources") or []
    referenced = [s for s in sources if s.get("referenced")]
    lines: list[str] = [
        "LOCKED FACTS - REGULATORY ASSESSMENT-REPORT EVIDENCE "
        "(deterministic; narrate, do not extend):",
        f"- Drug: {plan.get('drug_inn', 'the study drug')}",
        f"- PubMed records scanned for agency references: {plan.get('records_scanned', 0)}",
    ]

    status = plan.get("search_status")
    if status == REG_STATUS_EXECUTED:
        lines.append(
            "- A dedicated regulatory-assessment-report PubMed query WAS run in "
            "addition to the standard PK query."
        )
    elif status == REG_STATUS_CACHE:
        lines.append(
            "- Literature came from the 7-day cache; the regulatory-targeted query "
            "was not re-run on this generation."
        )
    elif status in (REG_STATUS_DISABLED, REG_STATUS_NONE):
        lines.append("- The regulatory-targeted PubMed query was not run for this generation.")
    elif status == REG_STATUS_FAILED:
        lines.append(
            "- The regulatory-targeted PubMed query failed; only the standard PK "
            "result set is available."
        )

    if referenced:
        lines.append("- Assessment-report programmes REFERENCED by retrieved abstracts:")
        for s in referenced:
            pmids = ", ".join(f"PMID {p}" for p in s.get("evidence_pmids", []) if p)
            phrases = ", ".join(
                sorted({p for h in s.get("evidence", []) for p in h.get("matched_phrases", [])})
            )
            lines.append(
                f"    * {s['document']} ({s['agency']}) - referenced by {pmids or 'a retrieved record'}"
                f"; matched wording: {phrases}."
            )
            lines.append(f"      What that document provides: {s['provides']}")
            lines.append(f"      {s['retrieval_note']}")
        lines.append(
            "  You MAY write, for example: 'PMID <id> reports these data as derived from "
            "the <document>.' Attribute the statement to the PMID, not to the agency document."
        )
    else:
        lines.append(
            "- NO retrieved abstract references an FDA, EMA, TGA, PMDA or NMPA "
            "assessment report. Do not mention any of these documents as a source "
            "for this protocol's PK or variability assumptions."
        )

    unref = [_REGULATORY_BY_KEY[k].document for k in plan.get("unreferenced_sources", [])]
    if unref:
        lines.append(
            "- NOT referenced and therefore NOT citable here: " + "; ".join(unref) + "."
        )

    hv = plan.get("healthy_volunteer_pmids") or []
    if hv:
        lines.append(
            "- Retrieved records identified as healthy-volunteer studies: "
            + ", ".join(f"PMID {p}" for p in hv)
            + ". Healthy-volunteer data are the appropriate basis for this protocol's "
            "PK and within-subject CV assumptions; patient-population data are not."
        )
    regional = plan.get("regional_healthy_volunteer_pmids") or {}
    for region_key, pmids in regional.items():
        if pmids:
            lines.append(
                f"- Healthy-volunteer records in a {region_key.capitalize()} population: "
                + ", ".join(f"PMID {p}" for p in pmids)
                + ". Describe the population explicitly if these are used; do not "
                "present them as interchangeable with the primary population."
            )

    lines.append("- SOURCE RULE: " + str(plan.get("citation_rule", "")))
    lines.append(
        "- Every assessment report listed above was NOT downloaded by this system "
        "(all are PDF portals without a PK API). The protocol must never imply "
        "otherwise."
    )
    return "\n".join(lines)


def regulatory_extraction_hint(plan: dict[str, Any] | None) -> str:
    """Extra rule text for the PK-extraction prompt (services/ai_service.py).

    Registration-report values reported inside an abstract are the highest-quality
    within-subject CV available to this system, so the extractor should be told to
    look for them - while keeping source_pmid anchored to a real retrieved PMID.
    Returns "" when nothing was referenced, so the prompt does not grow for no
    reason and cannot suggest an agency the search did not surface.
    """
    if not plan:
        return ""
    referenced = plan.get("referenced_sources") or []
    if not referenced:
        return ""
    docs = "; ".join(_REGULATORY_BY_KEY[k].document for k in referenced if k in _REGULATORY_BY_KEY)
    return (
        "REGULATORY-SOURCE PRIORITY: at least one abstract below reports data it "
        f"attributes to a regulatory assessment report ({docs}). Values an abstract "
        "explicitly attributes to such a report - especially within-subject CV, "
        "geometric mean ratios and healthy-volunteer exposure - come from the actual "
        "registration dataset and are the best available evidence: extract every one "
        "of them. source_pmid MUST remain the PMID of the retrieved abstract that "
        "reports the value. NEVER invent a PMID or an accession number for the "
        "assessment report itself."
    )


# ---------------------------------------------------------------------------
# Service class
# ---------------------------------------------------------------------------


class LiteratureService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._pubmed_api_key = os.getenv("PUBMED_API_KEY", "")
        # Recorded by the most recent fetch_pubmed() call so a caller can build
        # a plan_regulatory_sources() plan without fetch_pubmed's return type
        # having to change (it stays list[PubMedRecord] for every existing
        # caller). See the module docstring's "WHY THE SECOND QUERY IS GATED"
        # section for why this is a second query at all.
        self.last_regulatory_status: str = REG_STATUS_NONE
        self.last_regulatory_query: str | None = None
        self.last_regulatory_pmids: list[str] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def fetch_pubmed(
        self, drug_inn: str, *, include_regulatory: bool | None = None
    ) -> list[PubMedRecord]:
        """Return PK-related PubMed articles for *drug_inn*, using cache.

        ``include_regulatory`` controls whether a SECOND, regulatory-assessment-
        -report-targeted esearch (``build_regulatory_query``) is run alongside
        the standard PK query and its PMIDs merged into the result before the
        single efetch call. Defaults to ``regulatory_search_enabled()`` — ON
        outside pytest, OFF under pytest (see the module docstring). Passing
        ``True`` explicitly (as a test wanting the two-call path must) forces
        it on even under pytest.

        The outcome (whether it ran, the exact query, and which PMIDs came
        from it) is recorded on ``self.last_regulatory_*`` rather than
        changing this method's return type, so every existing caller of
        ``fetch_pubmed`` is unaffected. Callers that want a
        ``plan_regulatory_sources()`` plan read those attributes right after
        calling this method.
        """
        run_regulatory = (
            regulatory_search_enabled() if include_regulatory is None else include_regulatory
        )
        self.last_regulatory_query = build_regulatory_query(drug_inn) if run_regulatory else None
        self.last_regulatory_pmids = []

        drug = await self._resolve_drug(drug_inn)

        cached = await self._get_cache(drug.id, LiteratureSource.pubmed)
        if cached:
            # The cache does not distinguish which PMIDs came from which query,
            # so on a cache hit the regulatory query is simply not re-run —
            # regulatory_sources_directive() renders this status honestly
            # ("literature came from the 7-day cache") rather than claiming a
            # query that did not happen on this call.
            self.last_regulatory_status = REG_STATUS_CACHE if run_regulatory else REG_STATUS_NONE
            return [
                PubMedRecord(pmid=r.pmid, title=r.title or "", abstract=r.abstract or "")
                for r in cached
            ]

        pmids = await self._pubmed_search(drug_inn)

        if run_regulatory:
            try:
                reg_pmids = await self._pubmed_search(
                    drug_inn,
                    query=self.last_regulatory_query,
                    max_results=REGULATORY_MAX_RESULTS,
                )
            except Exception:  # noqa: BLE001 - the primary query must still succeed
                reg_pmids = []
                self.last_regulatory_status = REG_STATUS_FAILED
            else:
                self.last_regulatory_status = REG_STATUS_EXECUTED
            new_pmids = [p for p in reg_pmids if p not in pmids]
            self.last_regulatory_pmids = new_pmids
            pmids = (pmids + new_pmids)[:PUBMED_TOTAL_MAX_RESULTS]
        else:
            self.last_regulatory_status = REG_STATUS_DISABLED

        if not pmids:
            return []

        records = await self._pubmed_fetch_details(pmids)
        await self._save_pubmed_cache(drug.id, records)
        return records

    async def fetch_dailymed(self, drug_inn: str) -> DailyMedRecord | None:
        """Return structured DailyMed label data for *drug_inn*, using cache."""
        drug = await self._resolve_drug(drug_inn)

        cached = await self._get_cache(drug.id, LiteratureSource.dailymed)
        if cached:
            row = cached[0]
            return DailyMedRecord(
                setid=row.pmid,
                title=row.title or "",
                sections=json.loads(row.abstract) if row.abstract else {},
            )

        candidates = await self._dailymed_search(drug_inn)
        if not candidates:
            return None

        # DailyMed has one SPL per manufacturer/repackager/package, and many
        # (especially small repackagers, or OTC "Drug Facts" formulations) only
        # republish a subset of sections — omitting Clinical Pharmacology /
        # Pharmacokinetics entirely. Picking the first title match (the old
        # behaviour) reliably grabbed a PK-free label (e.g. warfarin resolved to
        # a "REMEDYREPACK INC." repackager label, ibuprofen to the OTC "MOTRIN
        # IB" Drug Facts label), so t_half/CV were NEVER extractable regardless
        # of how well the AI extraction prompt was tuned. Try each candidate
        # (bounded, already-fetched list) and keep the first with a substantive
        # PK section; fall back to the first candidate if none qualify.
        best: DailyMedRecord | None = None
        for setid, label_title in candidates:
            xml_text = await self._dailymed_fetch_spl(setid)
            sections = _parse_spl_sections(xml_text)
            candidate_record = DailyMedRecord(setid=setid, title=label_title, sections=sections)
            if best is None:
                best = candidate_record
            pk_text = sections.get("pharmacokinetics", "") or sections.get("clinical_pharmacology", "")
            if len(pk_text) >= 200:
                best = candidate_record
                break

        await self._save_dailymed_cache(drug.id, best)
        return best

    async def fetch_clinicaltrials(self, drug_inn: str) -> list[ClinicalTrialRecord]:
        """Return prior/registered trials for *drug_inn* from ClinicalTrials.gov,
        using cache. Grounds inclusion/exclusion criteria in real precedent
        (services/trial_precedent.py) rather than generic boilerplate — a
        clinical pharmacologist's explicit requirement.

        Searched by INTERVENTION NAME ONLY, with no condition/indication
        filter, deliberately: the 7-day cache is keyed by (drug_id, source)
        with no indication column (matching fetch_pubmed's cache shape), so a
        query result must be valid for ANY caller asking about this drug,
        regardless of which indication they need precedent for. Filtering the
        cached list down to trials relevant to a SPECIFIC indication is
        services/trial_precedent.py's job, done fresh on every call against
        this cached-but-unfiltered raw list — the same "cache the retrieval,
        filter downstream" split services/meta_analysis.py already uses for
        population-specific filtering of cached PubMed results.

        A US biosimilar-suffixed INN (e.g. "adalimumab-atto") is searched
        under its BASE INN ("adalimumab") — real prior trials are registered
        under the originator's generic or brand name, never under a
        biosimilar suffix that did not exist when they were run.
        """
        drug = await self._resolve_drug(drug_inn)

        cached = await self._get_cache(drug.id, LiteratureSource.ct_gov)
        if cached:
            return [_ctgov_row_to_record(r) for r in cached]

        studies = await self._ctgov_search(_base_inn(drug_inn))
        records = [rec for s in studies if (rec := _parse_ctgov_study(s)) is not None]
        await self._save_ctgov_cache(drug.id, records)
        return records

    # ------------------------------------------------------------------
    # Cache helpers
    # ------------------------------------------------------------------

    async def _resolve_drug(self, inn: str) -> Drug:
        result = await self._session.execute(
            select(Drug).where(Drug.inn == inn.lower())
        )
        drug = result.scalar_one_or_none()
        if drug is None:
            raise ValueError(f"Drug '{inn}' not found in database.")
        return drug

    async def _get_cache(
        self, drug_id: uuid.UUID, source: LiteratureSource
    ) -> list[LiteratureCache] | None:
        now = datetime.now(timezone.utc)
        result = await self._session.execute(
            select(LiteratureCache).where(
                LiteratureCache.drug_id == drug_id,
                LiteratureCache.source == source,
                LiteratureCache.ttl_expires_at > now,
            )
        )
        rows = result.scalars().all()
        return list(rows) if rows else None

    async def _save_pubmed_cache(
        self, drug_id: uuid.UUID, records: list[PubMedRecord]
    ) -> None:
        ttl = datetime.now(timezone.utc) + timedelta(days=CACHE_TTL_DAYS)
        for rec in records:
            self._session.add(
                LiteratureCache(
                    id=uuid.uuid4(),
                    pmid=rec["pmid"],
                    drug_id=drug_id,
                    title=rec["title"],
                    abstract=rec["abstract"],
                    source=LiteratureSource.pubmed,
                    fetched_at=datetime.now(timezone.utc),
                    ttl_expires_at=ttl,
                )
            )
        await self._session.commit()

    async def _save_dailymed_cache(
        self, drug_id: uuid.UUID, record: DailyMedRecord
    ) -> None:
        ttl = datetime.now(timezone.utc) + timedelta(days=CACHE_TTL_DAYS)
        self._session.add(
            LiteratureCache(
                id=uuid.uuid4(),
                pmid=record["setid"],          # setid stored in pmid column
                drug_id=drug_id,
                title=record["title"],
                abstract=json.dumps(record["sections"]),  # JSON blob in abstract
                source=LiteratureSource.dailymed,
                fetched_at=datetime.now(timezone.utc),
                ttl_expires_at=ttl,
            )
        )
        await self._session.commit()

    async def _save_ctgov_cache(
        self, drug_id: uuid.UUID, records: list[ClinicalTrialRecord]
    ) -> None:
        ttl = datetime.now(timezone.utc) + timedelta(days=CACHE_TTL_DAYS)
        for rec in records:
            self._session.add(
                LiteratureCache(
                    id=uuid.uuid4(),
                    pmid=rec["nct_id"],        # NCT id stored in pmid column
                    drug_id=drug_id,
                    title=rec["title"],
                    # Every OTHER field JSON-serialised into abstract, same
                    # convention as _save_dailymed_cache's sections blob.
                    abstract=json.dumps({k: v for k, v in rec.items() if k not in ("nct_id", "title")}),
                    source=LiteratureSource.ct_gov,
                    fetched_at=datetime.now(timezone.utc),
                    ttl_expires_at=ttl,
                )
            )
        await self._session.commit()

    # ------------------------------------------------------------------
    # PubMed E-utilities
    # ------------------------------------------------------------------

    async def _pubmed_search(
        self, drug_inn: str, *, query: str | None = None, max_results: int | None = None
    ) -> list[str]:
        """Search PubMed and return a list of PMIDs.

        ``query``/``max_results`` let the regulatory-targeted second query
        (see ``build_regulatory_query``) reuse this same E-utilities call
        rather than duplicating the HTTP plumbing; both default to the
        standard PK-search behaviour when omitted.
        """
        term = query or (
            f"{drug_inn}[tiab] AND ("
            "pharmacokinetics[MeSH Terms] OR bioequivalence[tiab] OR "
            '"pharmacokinetic"[tiab] OR "PK/PD"[tiab])'
        )
        params: dict = {
            "db": "pubmed",
            "term": term,
            "retmax": max_results or PUBMED_MAX_RESULTS,
            "retmode": "json",
            "usehistory": "n",
        }
        if self._pubmed_api_key:
            params["api_key"] = self._pubmed_api_key

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(f"{PUBMED_BASE}/esearch.fcgi", params=params)
            resp.raise_for_status()

        data = resp.json()
        return data.get("esearchresult", {}).get("idlist", [])

    async def _pubmed_fetch_details(self, pmids: list[str]) -> list[PubMedRecord]:
        """Fetch titles and abstracts for a list of PMIDs via efetch XML."""
        params: dict = {
            "db": "pubmed",
            "id": ",".join(pmids),
            "retmode": "xml",
            "rettype": "abstract",
        }
        if self._pubmed_api_key:
            params["api_key"] = self._pubmed_api_key

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(f"{PUBMED_BASE}/efetch.fcgi", params=params)
            resp.raise_for_status()

        return _parse_pubmed_xml(resp.text)

    # ------------------------------------------------------------------
    # DailyMed
    # ------------------------------------------------------------------

    async def _dailymed_search(self, drug_inn: str) -> list[tuple[str, str]]:
        """Search DailyMed and return up to 25 (setid, label_title) candidates,
        INN-title matches first — the caller tries each in order until one has
        a substantive PK section (see fetch_dailymed). A generic drug's results
        are dominated by repackagers (e.g. a single repackager filed 13 near-
        identical warfarin SPLs, ALL with an empty Clinical Pharmacology
        section) — a small pagesize can be entirely repackager duplicates with
        no PK content at all, so this needs real headroom, not just a few extra."""
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{DAILYMED_BASE}/spls.json",
                params={"drug_name": drug_inn, "pagesize": 25},
            )
            resp.raise_for_status()

        data = resp.json()
        items = data.get("data", [])
        if not items:
            return []

        inn_lower = drug_inn.lower()
        matching = [i for i in items if inn_lower in i.get("title", "").lower()]
        ordered = matching or items

        # De-duplicate by manufacturer (the bracketed "[...]" suffix DailyMed
        # titles end with) — repackagers commonly file a dozen+ near-identical
        # SPLs for the same product (13 for warfarin alone, in one real check),
        # and near-identical filings from the same filer have near-identical
        # section completeness, so trying more than one per manufacturer rarely
        # helps and multiplies fetch_dailymed's worst-case XML fetch count.
        seen_mfr: set[str] = set()
        deduped: list[dict] = []
        for i in ordered:
            title = i.get("title", "")
            mfr_match = re.search(r"\[([^\]]+)\]\s*$", title)
            mfr = mfr_match.group(1).strip().lower() if mfr_match else title.lower()
            if mfr in seen_mfr:
                continue
            seen_mfr.add(mfr)
            deduped.append(i)
        return [(i["setid"], i.get("title", "")) for i in deduped]

    async def _dailymed_fetch_spl(self, setid: str) -> str:
        """Fetch the full SPL XML for a given setid."""
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(f"{DAILYMED_BASE}/spls/{setid}.xml")
            resp.raise_for_status()
        return resp.text

    # ------------------------------------------------------------------
    # ClinicalTrials.gov
    # ------------------------------------------------------------------

    async def _ctgov_search(self, drug_inn: str) -> list[dict]:
        """Search ClinicalTrials.gov v2 (public, no API key) by intervention
        name and return the raw "studies" list. Only the fields this module
        actually reads are requested (via ``fields=``), keeping the payload
        small regardless of how verbose a study's other modules are."""
        fields = ",".join([
            "protocolSection.identificationModule.nctId",
            "protocolSection.identificationModule.briefTitle",
            "protocolSection.statusModule.overallStatus",
            "protocolSection.conditionsModule.conditions",
            "protocolSection.armsInterventionsModule.interventions",
            "protocolSection.designModule.phases",
            "protocolSection.eligibilityModule.eligibilityCriteria",
            "protocolSection.eligibilityModule.sex",
            "protocolSection.eligibilityModule.minimumAge",
            "protocolSection.eligibilityModule.maximumAge",
            "protocolSection.eligibilityModule.healthyVolunteers",
        ])
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{CTGOV_BASE}/studies",
                params={
                    "query.intr": drug_inn,
                    "pageSize": CTGOV_MAX_RESULTS,
                    "fields": fields,
                },
            )
            resp.raise_for_status()

        data = resp.json()
        return data.get("studies", [])


# ---------------------------------------------------------------------------
# Pure parsing helpers (no I/O — easy to unit-test)
# ---------------------------------------------------------------------------


_BIOSIMILAR_SUFFIX_RE = re.compile(r"-[a-z]{4}$")


def _base_inn(drug_inn: str) -> str:
    """Strip a US biosimilar four-letter suffix (e.g. "-atto", "-adbm") from
    an INN, so a search targets the originator's registered trials rather
    than a suffix that postdates every one of them. Non-suffixed INNs (small
    molecules, or a biosimilar referred to by its base INN already) pass
    through unchanged."""
    return _BIOSIMILAR_SUFFIX_RE.sub("", (drug_inn or "").strip().lower())


def _parse_ctgov_study(study: dict) -> ClinicalTrialRecord | None:
    """Parse one ClinicalTrials.gov v2 "study" object into a ClinicalTrialRecord.
    Returns None for a study with no NCT id (malformed/unusable) — every other
    field degrades to an honest empty/None value rather than raising, since a
    real study can genuinely omit any of them (e.g. an observational study has
    no phase; healthyVolunteers is frequently absent)."""
    protocol = study.get("protocolSection") or {}
    ident = protocol.get("identificationModule") or {}
    nct_id = ident.get("nctId") or ""
    if not nct_id:
        return None

    status = (protocol.get("statusModule") or {}).get("overallStatus") or ""
    conditions = (protocol.get("conditionsModule") or {}).get("conditions") or []
    interventions = [
        i.get("name", "")
        for i in (protocol.get("armsInterventionsModule") or {}).get("interventions") or []
        if i.get("name")
    ]
    phases = (protocol.get("designModule") or {}).get("phases") or []
    elig = protocol.get("eligibilityModule") or {}

    return ClinicalTrialRecord(
        nct_id=nct_id,
        title=ident.get("briefTitle") or "",
        status=status,
        phase=phases[0] if phases else "",
        conditions=list(conditions),
        interventions=interventions,
        sex=elig.get("sex") or "",
        minimum_age=elig.get("minimumAge") or "",
        maximum_age=elig.get("maximumAge") or "",
        healthy_volunteers=elig.get("healthyVolunteers"),
        eligibility_criteria=elig.get("eligibilityCriteria") or "",
    )


def _ctgov_row_to_record(row: "LiteratureCache") -> ClinicalTrialRecord:
    """Reconstruct a ClinicalTrialRecord from a cached LiteratureCache row
    (see LiteratureService._save_ctgov_cache for the storage convention)."""
    extra = json.loads(row.abstract) if row.abstract else {}
    return ClinicalTrialRecord(
        nct_id=row.pmid,
        title=row.title or "",
        status=extra.get("status", ""),
        phase=extra.get("phase", ""),
        conditions=extra.get("conditions", []),
        interventions=extra.get("interventions", []),
        sex=extra.get("sex", ""),
        minimum_age=extra.get("minimum_age", ""),
        maximum_age=extra.get("maximum_age", ""),
        healthy_volunteers=extra.get("healthy_volunteers"),
        eligibility_criteria=extra.get("eligibility_criteria", ""),
    )


def _parse_pubmed_xml(xml_text: str) -> list[PubMedRecord]:
    """Parse PubMed efetch XML into a list of PubMedRecord dicts."""
    records: list[PubMedRecord] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return records

    for article in root.iter("PubmedArticle"):
        pmid_elem = article.find(".//PMID")
        title_elem = article.find(".//ArticleTitle")
        abstract_elems = article.findall(".//AbstractText")

        pmid = pmid_elem.text or "" if pmid_elem is not None else ""
        title = _elem_text(title_elem)

        # Structured abstracts have multiple AbstractText elements with Label attrs
        if abstract_elems:
            parts = []
            for elem in abstract_elems:
                label = elem.get("Label")
                text = _elem_text(elem)
                parts.append(f"{label}: {text}" if label else text)
            abstract = " ".join(filter(None, parts))
        else:
            abstract = ""

        if pmid:
            records.append(PubMedRecord(pmid=pmid, title=title, abstract=abstract))

    return records


def _parse_spl_sections(xml_text: str) -> dict[str, str]:
    """Extract target sections from a DailyMed SPL XML document."""
    ns = "urn:hl7-org:v3"
    sections: dict[str, str] = {}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return sections

    for section in root.iter(f"{{{ns}}}section"):
        code_elem = section.find(f"{{{ns}}}code")
        if code_elem is None:
            continue
        code = code_elem.get("code", "")
        section_key = _SPL_SECTIONS.get(code)
        if not section_key:
            continue

        # Walk the WHOLE matched section subtree, not just its direct <text>
        # child. FDA SPL documents commonly nest the actual numbered content
        # (e.g. 12.1/12.2/12.3 under "Clinical Pharmacology") as child
        # <component><section> elements — often tagged with the generic
        # "SPL UNCLASSIFIED SECTION" code rather than their own LOINC code — so
        # the parent's direct <text> child is empty while the real prose
        # (e.g. "The terminal half-life of warfarin is...") lives several
        # levels down. Verified on a real warfarin label: direct-child text
        # was 0 chars; the full subtree had 5720 chars including the exact
        # half-life sentence.
        raw = " ".join(t.strip() for t in section.itertext() if t.strip())
        if not raw:
            continue
        # Keep the longer candidate if the same section code is matched twice
        # (e.g. a nested subsection independently carrying the same code).
        if section_key not in sections or len(raw) > len(sections[section_key]):
            sections[section_key] = raw

    return sections


def _elem_text(elem: ET.Element | None) -> str:
    """Flatten all text inside an element (handles mixed content)."""
    if elem is None:
        return ""
    return "".join(elem.itertext()).strip()
