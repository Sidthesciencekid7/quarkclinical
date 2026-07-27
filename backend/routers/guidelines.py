"""Guidelines router — exposes the supported regulatory guideline registry so the
frontend can render the guideline selector. Public read (like drug search): the
list of supported guidelines is not sensitive and the entry flow needs it.
"""
from fastapi import APIRouter

from services.guidelines import DEFAULT_GUIDELINE, GUIDELINES

router = APIRouter(prefix="/api/guidelines", tags=["guidelines"])


@router.get("/")
async def list_guidelines() -> dict:
    """Return the SELECTABLE guidelines for new protocols (current only — a
    superseded guideline must not be chosen for new work, though it stays a
    valid stored value for legacy records), default first."""
    current = [p for p in GUIDELINES.values() if p.is_current]
    ordered = sorted(current, key=lambda p: (p.id != DEFAULT_GUIDELINE, p.display_name))
    return {
        "default": DEFAULT_GUIDELINE,
        "guidelines": [
            {
                "id": p.id,
                "display_name": p.display_name,
                "regulator": p.regulator,
                "region": p.region,
                "citation": p.citation,
                "is_current": p.is_current,
            }
            for p in ordered
        ],
    }
