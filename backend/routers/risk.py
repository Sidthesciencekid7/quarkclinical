"""Protocol risk / feasibility scoring endpoint.

Deterministic (no AI call), org-scoped and audit-logged like the sibling QC and
coverage endpoints. Safe to call repeatedly — it only reads stored state.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from auth import require_user
from database import get_db
from dependencies import (
    audit_logger,
    authenticate_actor as _authenticate_actor,
    get_protocol_scoped,
)
from routers.protocols import SECTION_DISPLAY_NAMES, SECTION_ORDER
from services.protocol_qc import run_qc
from services.risk_scoring import score_protocol_risk

router = APIRouter(prefix="/api/protocols", tags=["risk"], dependencies=[Depends(audit_logger)])


@router.get("/{protocol_id}/risk", dependencies=[Depends(require_user)])
async def protocol_risk(
    protocol_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Operational feasibility risk score for a protocol.

    Returns ``{overall_score, risk_level, factors[]}``. The QC report is
    computed first and fed to the scorer so unresolved failures and missing
    sections are reflected in the score rather than re-derived.
    """
    actor = await _authenticate_actor(request, db)
    protocol = await get_protocol_scoped(db, protocol_id, actor)

    try:
        qc_report = run_qc(
            study_type=protocol.study_type.value,
            population_config=protocol.population_config or {},
            sections=protocol.sections or {},
            expected_sections=list(SECTION_ORDER),
            section_labels=SECTION_DISPLAY_NAMES,
        )
    except Exception:
        # Risk scoring must never fail just because a QC check tripped on odd
        # data — it degrades to the design/guideline signals alone.
        qc_report = None

    return score_protocol_risk(protocol, qc_report)
