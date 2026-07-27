import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from auth import require_user
from database import get_db
from dependencies import audit_logger
from models import Drug, LiteratureSource
from schemas import LiteratureFetchRequest, LiteratureItemRead
from services.literature_service import LiteratureService

router = APIRouter(
    prefix="/api/literature",
    tags=["literature"],
    dependencies=[Depends(audit_logger)],
)


@router.post("/fetch", response_model=list[LiteratureItemRead], dependencies=[Depends(require_user)])
async def fetch_literature(
    payload: LiteratureFetchRequest,
    db: AsyncSession = Depends(get_db),
):
    """Fetch and cache PubMed abstracts and/or the DailyMed label for a drug.

    Returns from cache if a fresh entry (< 7 days) already exists; otherwise
    fetches from the external API, persists the results, and returns them.
    """
    drug = await db.get(Drug, payload.drug_id)
    if not drug:
        raise HTTPException(404, detail="Drug not found.")

    svc = LiteratureService(db)
    now = datetime.now(timezone.utc)
    items: list[LiteratureItemRead] = []

    if LiteratureSource.pubmed in payload.sources:
        for rec in await svc.fetch_pubmed(drug.inn):
            items.append(
                LiteratureItemRead(
                    pmid=rec["pmid"],
                    title=rec.get("title"),
                    abstract=rec.get("abstract"),
                    source="pubmed",
                    fetched_at=now,
                )
            )

    if LiteratureSource.dailymed in payload.sources:
        dm = await svc.fetch_dailymed(drug.inn)
        if dm:
            items.append(
                LiteratureItemRead(
                    pmid=dm["setid"],
                    title=dm.get("title"),
                    # Sections stored as JSON for downstream AI consumption
                    abstract=json.dumps(dm.get("sections", {})),
                    source="dailymed",
                    fetched_at=now,
                )
            )

    return items
