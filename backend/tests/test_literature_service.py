"""Tests for the regulatory-assessment-report evidence layer in
services/literature_service.py.

A practising clinical pharmacologist asked for PK/CV values to be sourced from
regulatory assessment reports (FDA review, EMA EPAR, TGA AusPAR, PMDA/NMPA
review reports) where possible. Those documents are PDF-only with no PK API,
so this module's actual contribution is: (1) run a second, regulatory-
targeted PubMed query so papers that discuss registration-study data enter
the evidence set, (2) deterministically detect which agencies' reports the
RETRIEVED abstracts reference, and (3) render that as a locked directive the
generator may narrate but never invent from — it may never claim to have
read a report itself.

Covers: build_regulatory_query, detect_regulatory_sources,
plan_regulatory_sources, regulatory_sources_directive,
regulatory_extraction_hint, and regulatory_search_enabled's pytest-default-off
behavior, plus an end-to-end fetch_pubmed(include_regulatory=True) exercise
of the real two-esearch-call wiring with TWO httpx_mock responses registered
(per the module's own documented pytest-httpx 0.36 constraint).
"""
from __future__ import annotations

import re

from services.literature_service import (
    LiteratureService,
    REG_STATUS_CACHE,
    REG_STATUS_DISABLED,
    REG_STATUS_EXECUTED,
    REG_STATUS_FAILED,
    REG_STATUS_NONE,
    REGULATORY_SEARCH_ENV,
    build_regulatory_query,
    detect_regulatory_sources,
    plan_regulatory_sources,
    regulatory_extraction_hint,
    regulatory_search_enabled,
    regulatory_sources_directive,
)
from tests.conftest import PUBMED_ESEARCH_PMIDS, PUBMED_EFETCH_XML


_ESEARCH_URL = re.compile(
    r"^https://eutils\.ncbi\.nlm\.nih\.gov/entrez/eutils/esearch\.fcgi(\?.*)?$"
)
_EFETCH_URL = re.compile(
    r"^https://eutils\.ncbi\.nlm\.nih\.gov/entrez/eutils/efetch\.fcgi(\?.*)?$"
)


def _register_primary_pubmed_only(httpx_mock) -> None:
    """Register esearch + efetch only — NOT the dailymed mock that
    tests.conftest.register_pubmed_mocks also adds for the full create_protocol
    flow. A test that calls only fetch_pubmed() never requests dailymed, and
    pytest-httpx's teardown fails on any mocked-but-unrequested response."""
    httpx_mock.add_response(
        method="GET", url=_ESEARCH_URL,
        json={"esearchresult": {"idlist": PUBMED_ESEARCH_PMIDS,
                                 "count": str(len(PUBMED_ESEARCH_PMIDS)), "retmax": "20"}},
    )
    httpx_mock.add_response(
        method="GET", url=_EFETCH_URL,
        text=PUBMED_EFETCH_XML,
        headers={"Content-Type": "application/xml; charset=utf-8"},
    )


# ---------------------------------------------------------------------------
# build_regulatory_query
# ---------------------------------------------------------------------------


def test_build_regulatory_query_names_the_drug_and_is_tiab_scoped():
    q = build_regulatory_query("adalimumab")
    assert q.startswith("adalimumab[tiab]")
    assert "AND (" in q


def test_build_regulatory_query_includes_regulatory_report_and_regional_terms():
    q = build_regulatory_query("metformin")
    assert "EPAR" in q
    assert "AusPAR" in q
    assert '"healthy Chinese"' in q
    assert '"healthy Japanese"' in q
    assert '"bridging study"' in q


def test_build_regulatory_query_intersects_with_pk_context():
    """The query must not drift into pure regulatory-policy commentary with no
    PK numbers in it — both arms are ANDed with a pharmacokinetics context."""
    q = build_regulatory_query("metformin")
    assert "pharmacokinetics[MeSH Terms]" in q
    assert q.count(" AND (") == 2


def test_build_regulatory_query_is_deterministic():
    assert build_regulatory_query("warfarin") == build_regulatory_query("warfarin")


# ---------------------------------------------------------------------------
# regulatory_search_enabled — the pytest-httpx safety net
# ---------------------------------------------------------------------------


def test_regulatory_search_disabled_by_default_under_pytest(monkeypatch):
    """Module docstring: 34+ existing tests register exactly one esearch
    response, so this MUST default to False while running under pytest, or an
    unconditional second query breaks every one of them."""
    monkeypatch.delenv(REGULATORY_SEARCH_ENV, raising=False)
    assert regulatory_search_enabled() is False


def test_regulatory_search_env_override_forces_it_on(monkeypatch):
    monkeypatch.setenv(REGULATORY_SEARCH_ENV, "1")
    assert regulatory_search_enabled() is True


def test_regulatory_search_env_override_forces_it_off(monkeypatch):
    monkeypatch.setenv(REGULATORY_SEARCH_ENV, "0")
    assert regulatory_search_enabled() is False


def test_regulatory_search_env_accepts_common_truthy_strings(monkeypatch):
    for val in ("true", "Yes", "ON"):
        monkeypatch.setenv(REGULATORY_SEARCH_ENV, val)
        assert regulatory_search_enabled() is True, val


# ---------------------------------------------------------------------------
# detect_regulatory_sources
# ---------------------------------------------------------------------------


def test_detect_regulatory_sources_finds_ema_epar_and_fda_review():
    text = (
        "Data were derived from the EPAR scientific discussion and the FDA "
        "clinical pharmacology review of the approval package."
    )
    found = detect_regulatory_sources(text)
    assert set(found) == {"ema", "fda"}
    assert "epar" in found["ema"][0].lower()


def test_detect_regulatory_sources_finds_tga_pmda_nmpa():
    text = (
        "The AusPAR summarised the bioequivalence data; the PMDA review report "
        "and an NMPA disclosure were also consulted for the Asian bridging study."
    )
    found = detect_regulatory_sources(text)
    assert set(found) == {"tga", "pmda", "nmpa"}


def test_detect_regulatory_sources_ignores_bare_fda_mention():
    """A bare 'FDA-approved' is deliberately NOT a match — every third abstract
    says this, and it is not evidence the paper analyses an assessment report."""
    text = "This FDA-approved generic product was studied in healthy volunteers."
    assert detect_regulatory_sources(text) == {}


def test_detect_regulatory_sources_ignores_bare_tga_acronym_collision():
    """A bare 'TGA' collides with thermogravimetric analysis / transposition of
    the great arteries — the module deliberately requires a fuller phrase."""
    text = "Thermal stability was assessed by TGA under stress conditions."
    assert detect_regulatory_sources(text) == {}


def test_detect_regulatory_sources_empty_for_plain_pk_text():
    text = "Cmax was 1.03 ug/mL and terminal half-life was 6.2 hours."
    assert detect_regulatory_sources(text) == {}


def test_detect_regulatory_sources_returns_only_the_matched_phrase():
    text = "See the Australian Public Assessment Report for full detail."
    found = detect_regulatory_sources(text)
    assert found["tga"] == ["Australian Public Assessment Report"]


# ---------------------------------------------------------------------------
# plan_regulatory_sources
# ---------------------------------------------------------------------------


def test_plan_marks_referenced_source_and_never_claims_retrieval():
    records = [
        {"pmid": "111", "title": "PK bridging study", "abstract":
            "Exposure data were taken from the EMA EPAR for this product."},
    ]
    plan = plan_regulatory_sources(drug_inn="metformin", records=records,
                                    search_status=REG_STATUS_EXECUTED)
    ema = next(s for s in plan["sources"] if s["key"] == "ema")
    assert ema["referenced"] is True
    # ALWAYS False by construction — this system never downloads the report.
    assert ema["retrieved_document"] is False
    assert ema["evidence"][0]["pmid"] == "111"
    assert "111" in plan["referenced_sources"] or "ema" in plan["referenced_sources"]
    assert "ema" in plan["referenced_sources"]
    assert "fda" in plan["unreferenced_sources"]
    assert plan["search_executed"] is True


def test_plan_with_no_evidence_says_so_and_forbids_all_five():
    plan = plan_regulatory_sources(drug_inn="ibuprofen", records=[
        {"pmid": "1", "title": "t", "abstract": "Cmax and AUC were determined."},
    ])
    assert plan["referenced_sources"] == []
    assert set(plan["unreferenced_sources"]) == {"fda", "ema", "tga", "pmda", "nmpa"}
    assert "No retrieved abstract" in plan["summary"]
    assert "ibuprofen" in plan["summary"]


def test_plan_records_scanned_and_empty_records_is_safe():
    plan = plan_regulatory_sources(drug_inn="x", records=None)
    assert plan["records_scanned"] == 0
    assert plan["referenced_sources"] == []
    assert plan["search_executed"] is False


def test_plan_flags_healthy_volunteer_and_regional_population_pmids():
    records = [
        {"pmid": "200", "title": "t",
         "abstract": "A single-dose study in healthy Chinese volunteers was conducted."},
        {"pmid": "201", "title": "t",
         "abstract": "Pharmacokinetics in patients with renal impairment were assessed."},
    ]
    plan = plan_regulatory_sources(drug_inn="drugx", records=records)
    assert "200" in plan["healthy_volunteer_pmids"]
    assert "201" not in plan["healthy_volunteer_pmids"]
    assert plan["regional_healthy_volunteer_pmids"].get("chinese") == ["200"]


def test_plan_from_regulatory_query_flag_is_recorded_per_pmid():
    records = [
        {"pmid": "77", "title": "t", "abstract": "Per the EMA EPAR, GMR was 0.98."},
    ]
    plan = plan_regulatory_sources(
        drug_inn="d", records=records, search_status=REG_STATUS_EXECUTED,
        query="d[tiab] AND EPAR", regulatory_query_pmids=["77"],
    )
    ema = next(s for s in plan["sources"] if s["key"] == "ema")
    assert ema["evidence"][0]["from_regulatory_query"] is True
    assert plan["query"] == "d[tiab] AND EPAR"


# ---------------------------------------------------------------------------
# regulatory_sources_directive
# ---------------------------------------------------------------------------


def test_directive_empty_for_no_plan():
    assert regulatory_sources_directive(None) == ""
    assert regulatory_sources_directive({}) == ""


def test_directive_narrates_referenced_source_with_source_rule():
    plan = plan_regulatory_sources(
        drug_inn="metformin",
        records=[{"pmid": "111", "title": "t",
                  "abstract": "Derived from the EMA EPAR scientific discussion."}],
        search_status=REG_STATUS_EXECUTED,
    )
    d = regulatory_sources_directive(plan)
    assert "LOCKED FACTS" in d
    assert "PMID 111" in d
    assert "SOURCE RULE" in d
    assert "per the EPAR" in d  # named only to forbid the phrasing
    assert "was NOT downloaded by this system" in d


def test_directive_forbids_unreferenced_sources_explicitly():
    plan = plan_regulatory_sources(drug_inn="ibuprofen", records=[])
    d = regulatory_sources_directive(plan)
    assert "NO retrieved abstract" in d
    assert "NOT referenced and therefore NOT citable" in d


def test_directive_is_ascii_safe():
    """Windows cp1252 console: a non-ASCII char in a directive has crashed
    generation logging before (see test_biologic_review_feedback.py)."""
    plan = plan_regulatory_sources(
        drug_inn="metformin",
        records=[{"pmid": "111", "title": "t", "abstract": "Per the PMDA review report."}],
        search_status=REG_STATUS_EXECUTED,
    )
    assert regulatory_sources_directive(plan).isascii()


def test_directive_status_lines_reflect_cache_and_disabled_and_failed():
    for status, expected in (
        (REG_STATUS_CACHE, "cache"),
        (REG_STATUS_DISABLED, "not run for this generation"),
        (REG_STATUS_NONE, "not run for this generation"),
        (REG_STATUS_FAILED, "failed"),
    ):
        plan = plan_regulatory_sources(drug_inn="x", records=[], search_status=status)
        d = regulatory_sources_directive(plan)
        assert expected in d, (status, d)


# ---------------------------------------------------------------------------
# regulatory_extraction_hint
# ---------------------------------------------------------------------------


def test_extraction_hint_empty_when_nothing_referenced():
    plan = plan_regulatory_sources(drug_inn="ibuprofen", records=[])
    assert regulatory_extraction_hint(plan) == ""
    assert regulatory_extraction_hint(None) == ""


def test_extraction_hint_present_and_names_the_document_when_referenced():
    plan = plan_regulatory_sources(
        drug_inn="metformin",
        records=[{"pmid": "111", "title": "t",
                  "abstract": "Values were taken from the EMA EPAR."}],
        search_status=REG_STATUS_EXECUTED,
    )
    hint = regulatory_extraction_hint(plan)
    assert "REGULATORY-SOURCE PRIORITY" in hint
    assert "EMA EPAR" in hint
    assert "source_pmid MUST remain the PMID" in hint


# ---------------------------------------------------------------------------
# fetch_pubmed(include_regulatory=...) — the real two-call wiring
# ---------------------------------------------------------------------------


async def test_fetch_pubmed_default_skips_regulatory_query_under_pytest(seeded, httpx_mock):
    """Exactly the existing single-response registration every other test in
    this suite relies on — proves the default policy does not add a second
    call that would break them."""
    _register_primary_pubmed_only(httpx_mock)
    lit_svc = LiteratureService(seeded.db)

    records = await lit_svc.fetch_pubmed("metformin")

    assert records
    assert lit_svc.last_regulatory_status == REG_STATUS_DISABLED
    assert lit_svc.last_regulatory_query is None
    assert lit_svc.last_regulatory_pmids == []


async def test_fetch_pubmed_include_regulatory_runs_second_query_and_merges(
    seeded, httpx_mock
):
    """The two-call path: primary PK esearch, then the regulatory-targeted
    esearch, merged before the single efetch call. Registers TWO esearch
    responses as the module docstring requires for this path."""
    # Response 1: the standard PK query.
    httpx_mock.add_response(
        method="GET", url=_ESEARCH_URL,
        json={"esearchresult": {"idlist": PUBMED_ESEARCH_PMIDS,
                                 "count": str(len(PUBMED_ESEARCH_PMIDS)), "retmax": "20"}},
    )
    # Response 2: the regulatory-targeted query — a PMID NOT in the primary
    # set, so the merge is actually observable.
    httpx_mock.add_response(
        method="GET", url=_ESEARCH_URL,
        json={"esearchresult": {"idlist": ["30000001"], "count": "1", "retmax": "10"}},
    )
    httpx_mock.add_response(
        method="GET", url=_EFETCH_URL,
        text=PUBMED_EFETCH_XML,
        headers={"Content-Type": "application/xml; charset=utf-8"},
    )

    lit_svc = LiteratureService(seeded.db)
    records = await lit_svc.fetch_pubmed("metformin", include_regulatory=True)

    assert records  # the single efetch call still returned the fixture records
    assert lit_svc.last_regulatory_status == REG_STATUS_EXECUTED
    assert lit_svc.last_regulatory_query == build_regulatory_query("metformin")
    assert lit_svc.last_regulatory_pmids == ["30000001"]


async def test_fetch_pubmed_cache_hit_reports_cache_status_not_executed(
    seeded, httpx_mock
):
    """On a cache hit the regulatory query is not re-run — only one esearch/
    efetch pair is registered, proving no second HTTP call is attempted even
    with include_regulatory=True."""
    _register_primary_pubmed_only(httpx_mock)
    lit_svc = LiteratureService(seeded.db)
    await lit_svc.fetch_pubmed("metformin")  # populates the 7-day cache

    lit_svc2 = LiteratureService(seeded.db)
    records = await lit_svc2.fetch_pubmed("metformin", include_regulatory=True)

    assert records
    assert lit_svc2.last_regulatory_status == REG_STATUS_CACHE
