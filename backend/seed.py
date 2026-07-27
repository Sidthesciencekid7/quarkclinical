"""Seed script — inserts test drugs and representative PK parameters."""
import asyncio
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from database import AsyncSessionLocal, engine, Base
from models import Drug, PKParameterRecord, PKParameter

DRUGS = [
    {
        "inn": "ibuprofen",
        "brand_names": ["Advil", "Motrin", "Nurofen"],
        "rxnorm_id": "5640",
    },
    {
        "inn": "metformin",
        "brand_names": ["Glucophage", "Fortamet", "Glumetza"],
        "rxnorm_id": "6809",
    },
    {
        "inn": "atorvastatin",
        "brand_names": ["Lipitor", "Torvast"],
        "rxnorm_id": "83367",
    },
]

# (inn, parameter, value, unit, population, source_pmid, confidence)
PK_DATA: list[tuple] = [
    # Ibuprofen
    ("ibuprofen", PKParameter.cmax,           25.4,  "µg/mL",   "healthy adults", "12345678", 0.92),
    ("ibuprofen", PKParameter.tmax,            1.5,  "h",        "healthy adults", "12345678", 0.90),
    ("ibuprofen", PKParameter.t_half,          2.0,  "h",        "healthy adults", "12345678", 0.95),
    ("ibuprofen", PKParameter.auc0t,          80.0,  "µg·h/mL", "healthy adults", "12345678", 0.91),
    ("ibuprofen", PKParameter.protein_binding, 99.0, "%",        "healthy adults", "12345679", 0.98),
    # Metformin
    ("metformin",  PKParameter.cmax,            1.03, "µg/mL",   "healthy adults", "23456789", 0.89),
    ("metformin",  PKParameter.tmax,            2.5,  "h",        "healthy adults", "23456789", 0.88),
    ("metformin",  PKParameter.t_half,          6.2,  "h",        "healthy adults", "23456789", 0.93),
    ("metformin",  PKParameter.auc0t,          10.8,  "µg·h/mL", "healthy adults", "23456789", 0.87),
    ("metformin",  PKParameter.bioavailability, 55.0, "%",        "healthy adults", "23456790", 0.85),
    # Atorvastatin
    ("atorvastatin", PKParameter.cmax,           0.014, "µg/mL",  "healthy adults", "34567890", 0.90),
    ("atorvastatin", PKParameter.tmax,           1.0,   "h",       "healthy adults", "34567890", 0.88),
    ("atorvastatin", PKParameter.t_half,        14.0,   "h",       "healthy adults", "34567890", 0.94),
    ("atorvastatin", PKParameter.auc0inf,        0.08,  "µg·h/mL","healthy adults", "34567890", 0.86),
    ("atorvastatin", PKParameter.protein_binding,98.0,  "%",       "healthy adults", "34567891", 0.97),
]


async def seed() -> None:
    async with AsyncSessionLocal() as session:
        # Index existing drugs by INN to avoid duplicates on re-run
        result = await session.execute(select(Drug.inn))
        existing = {row[0] for row in result.all()}

        drug_map: dict[str, uuid.UUID] = {}

        for d in DRUGS:
            if d["inn"] in existing:
                res = await session.execute(select(Drug.id).where(Drug.inn == d["inn"]))
                drug_map[d["inn"]] = res.scalar_one()
                print(f"  skip (exists): {d['inn']}")
                continue

            drug = Drug(
                id=uuid.uuid4(),
                inn=d["inn"],
                brand_names=d["brand_names"],
                rxnorm_id=d["rxnorm_id"],
                updated_at=datetime.now(timezone.utc),
            )
            session.add(drug)
            await session.flush()
            drug_map[drug.inn] = drug.id
            print(f"  inserted drug: {drug.inn}")

        for inn, param, value, unit, population, pmid, confidence in PK_DATA:
            if inn not in drug_map:
                continue
            pk = PKParameterRecord(
                id=uuid.uuid4(),
                drug_id=drug_map[inn],
                parameter=param,
                value=value,
                unit=unit,
                population=population,
                source_pmid=pmid,
                confidence=confidence,
                extracted_at=datetime.now(timezone.utc),
            )
            session.add(pk)

        await session.commit()
        print("Seed complete.")


if __name__ == "__main__":
    asyncio.run(seed())
