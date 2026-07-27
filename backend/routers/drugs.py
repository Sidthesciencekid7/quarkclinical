import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import require_user
from database import get_db
from dependencies import audit_logger
from models import Drug
from schemas import DrugCreate, DrugRead

router = APIRouter(
    prefix="/api/drugs",
    tags=["drugs"],
    dependencies=[Depends(audit_logger)],
)


@router.get("/", response_model=list[DrugRead])
async def search_drugs(
    q: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """Search registered drugs by INN (case-insensitive substring). Returns up to 50."""
    stmt = select(Drug)
    if q:
        stmt = stmt.where(Drug.inn.ilike(f"%{q.strip().lower()}%"))
    result = await db.execute(stmt.order_by(Drug.inn).limit(50))
    return result.scalars().all()


@router.post("/", response_model=DrugRead, status_code=201, dependencies=[Depends(require_user)])
async def create_drug(
    payload: DrugCreate,
    db: AsyncSession = Depends(get_db),
):
    """Register a new drug by INN. Duplicate INNs (case-insensitive) are rejected.

    Write endpoint — requires a valid session (``require_user``). The drug
    catalog is global reference data, but unauthenticated writes are still
    disallowed. GET search stays open. The frontend only creates drugs from
    inside the Clerk-<Protected> route, so a token is always present.
    """
    inn = payload.inn.strip().lower()

    existing = await db.execute(select(Drug).where(Drug.inn == inn))
    if existing.scalar_one_or_none():
        raise HTTPException(409, detail=f"Drug '{inn}' already exists.")

    drug = Drug(
        id=uuid.uuid4(),
        inn=inn,
        brand_names=payload.brand_names,
        rxnorm_id=payload.rxnorm_id,
    )
    db.add(drug)
    await db.commit()
    await db.refresh(drug)
    return drug
