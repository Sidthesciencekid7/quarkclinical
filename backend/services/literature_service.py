"""
Literature service — PubMed and DailyMed fetching with 7-day cache.

Cache strategy:
  - Cache key: (drug_id, source)
  - Hit:  any unexpired row exists → return all rows for that key
  - Miss: call external API → persist rows → return results
"""

import json
import os
import re
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import TypedDict

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import Drug, LiteratureCache, LiteratureSource

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PUBMED_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
DAILYMED_BASE = "https://dailymed.nlm.nih.gov/dailymed/services/v2"
CACHE_TTL_DAYS = 7
PUBMED_MAX_RESULTS = 20

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


# ---------------------------------------------------------------------------
# Service class
# ---------------------------------------------------------------------------


class LiteratureService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._pubmed_api_key = os.getenv("PUBMED_API_KEY", "")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def fetch_pubmed(self, drug_inn: str) -> list[PubMedRecord]:
        """Return PK-related PubMed articles for *drug_inn*, using cache."""
        drug = await self._resolve_drug(drug_inn)

        cached = await self._get_cache(drug.id, LiteratureSource.pubmed)
        if cached:
            return [
                PubMedRecord(pmid=r.pmid, title=r.title or "", abstract=r.abstract or "")
                for r in cached
            ]

        pmids = await self._pubmed_search(drug_inn)
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

    # ------------------------------------------------------------------
    # PubMed E-utilities
    # ------------------------------------------------------------------

    async def _pubmed_search(self, drug_inn: str) -> list[str]:
        """Search PubMed and return a list of PMIDs."""
        query = (
            f"{drug_inn}[tiab] AND ("
            "pharmacokinetics[MeSH Terms] OR bioequivalence[tiab] OR "
            '"pharmacokinetic"[tiab] OR "PK/PD"[tiab])'
        )
        params: dict = {
            "db": "pubmed",
            "term": query,
            "retmax": PUBMED_MAX_RESULTS,
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


# ---------------------------------------------------------------------------
# Pure parsing helpers (no I/O — easy to unit-test)
# ---------------------------------------------------------------------------


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
