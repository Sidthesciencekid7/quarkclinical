import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import require_user
from database import get_db
from dependencies import _admin_emails, audit_logger, authenticate_actor
from models import AuditLog, Organization, Protocol
from schemas import AuditLogRead

router = APIRouter(
    prefix="/api/audit",
    tags=["audit"],
    dependencies=[Depends(audit_logger)],
)


@router.get("/{resource_id}", response_model=list[AuditLogRead], dependencies=[Depends(require_user)])
async def get_audit_logs(
    resource_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Return the audit trail for any resource (protocol, drug, etc.) by its ID.

    Results are ordered newest-first and capped at 200 entries.
    This endpoint is read-only; audit_logs cannot be mutated (enforced at DB level).

    Audit diffs can contain protocol section-content previews (see export.py's
    old_preview/new_preview) — reading another org's audit trail is just as
    sensitive as reading their protocol directly, so when resource_id resolves
    to a real protocol, the caller must belong to that protocol's org. The same
    applies to an organization's own lifecycle trail (create/activate/suspend/
    rotate-token) — a caller must not read another org's audit metadata (its
    timestamps and IPs) by guessing its UUID. Operators (ADMIN_EMAILS) bypass
    both checks since they manage every org.
    """
    actor = await authenticate_actor(request, db)
    is_admin = actor.email.lower() in _admin_emails()

    try:
        resource_uuid = uuid.UUID(resource_id)
    except ValueError:
        resource_uuid = None

    if resource_uuid is not None and not is_admin:
        protocol = await db.get(Protocol, resource_uuid)
        if protocol is not None and protocol.org_id != actor.org_id:
            raise HTTPException(
                403, detail="Protocol belongs to a different organization."
            )
        org = await db.get(Organization, resource_uuid)
        if org is not None and org.slug != actor.org_id:
            raise HTTPException(
                403, detail="Audit trail belongs to a different organization."
            )

    result = await db.execute(
        select(AuditLog)
        .where(AuditLog.resource_id == resource_id)
        .order_by(AuditLog.timestamp.desc())
        .limit(200)
    )
    return result.scalars().all()
