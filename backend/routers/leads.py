"""Enterprise "Contact sales" lead capture.

No CRM, no Stripe — a lead lands in the `leads` table and the operator checks
GET / (admin-gated) and follows up by hand. Once a deal closes, the operator
either creates the org directly or activates the prospect's own self-serve org
via POST /api/orgs/{id}/activate-license (see routers/orgs.py).

A lead is ALSO emailed to the operator (services/email_service.py) so a new
enquiry doesn't sit unnoticed until someone opens the admin dashboard. That
notification is best-effort and no-ops entirely unless SMTP_* is configured —
lead capture itself never depends on it.
"""
from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import auth
from database import get_db
from dependencies import audit_logger, rate_limit, require_admin
from models import Lead
from schemas import LeadCreate, LeadRead
from services.email_service import send_lead_notification

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/leads", tags=["leads"], dependencies=[Depends(audit_logger)])


@router.post(
    "/",
    response_model=LeadRead,
    status_code=201,
    dependencies=[Depends(rate_limit(20, 60, scope="leads_create"))],
)
async def create_lead(payload: LeadCreate, db: AsyncSession = Depends(get_db)):
    """Public, unauthenticated — the landing page's Enterprise "Contact
    sales" form. Rate-limited (20/min per client IP) as best-effort spam
    mitigation; a human filling the form once won't come close."""
    lead = Lead(
        id=uuid.uuid4(),
        company_name=payload.company_name.strip(),
        contact_name=payload.contact_name.strip(),
        contact_email=payload.contact_email.strip().lower(),
        team_size_estimate=(payload.team_size_estimate or "").strip() or None,
        message=(payload.message or "").strip() or None,
    )
    db.add(lead)
    await db.commit()
    await db.refresh(lead)

    # Notify the operator by email. Best-effort and deliberately non-fatal: the
    # lead is already committed, so a mail failure must never turn a successful
    # capture into an error for the prospect. Snapshot the fields into a plain
    # dict FIRST — the notification runs in a worker thread (smtplib is
    # blocking), and passing the ORM object across that boundary risks a lazy
    # load against an AsyncSession from the wrong thread.
    lead_fields = {
        "company_name": lead.company_name,
        "contact_name": lead.contact_name,
        "contact_email": lead.contact_email,
        "team_size_estimate": lead.team_size_estimate,
        "message": lead.message,
    }
    try:
        await run_in_threadpool(send_lead_notification, lead_fields)
    except Exception as exc:  # noqa: BLE001 — notification must never fail capture
        logger.warning("lead notification failed (lead still saved): %r", exc)

    return lead


@router.get("/", response_model=list[LeadRead], dependencies=[Depends(auth.require_user)])
async def list_leads(request: Request, db: AsyncSession = Depends(get_db)):
    """Admin-only (ADMIN_EMAILS) — the operator's own inbox for new leads.

    require_user must run first (via the route dependency) so it populates
    request.state.clerk_claims — require_admin only reads that, it doesn't
    verify a session itself. Missing this dependency was a real bug found
    live: every caller, admin or not, got "Authentication required" because
    clerk_claims was never set. See [[project_launch_readiness_g4]].
    """
    await require_admin(request)
    result = await db.execute(select(Lead).order_by(Lead.created_at.desc()))
    return result.scalars().all()


@router.delete("/{lead_id}", status_code=204, dependencies=[Depends(auth.require_user)])
async def delete_lead(lead_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)):
    """Admin-only — removes a lead (test data, spam, or a resolved inquiry
    the operator no longer needs to see). Permanent; no undo."""
    await require_admin(request)
    lead = await db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404, detail="Lead not found.")
    await db.delete(lead)
    await db.commit()
    request.state.audit_resource_id = str(lead_id)
