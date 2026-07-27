"""Idempotent user provisioning.

With Clerk email-binding (see dependencies.authenticate_actor), a person can
only use the app if their Clerk-login email exists as a row in the users table.
This script ensures the demo users exist and lets you provision an operator's
real Clerk email so they aren't locked out.

Usage (from the backend directory):

    # ensure the 3 demo users exist (default-org)
    python seed_users.py

    # also provision your own Clerk-login email with a role
    python seed_users.py --email you@example.com --role generator --org default-org

Roles: generator (Author — create/edit), reviewer (read-only), approver (sign).
Re-running with an existing email UPDATES that user's role/org (idempotent).
"""
from __future__ import annotations

import argparse
import asyncio
import uuid

from dotenv import load_dotenv
from sqlalchemy import select

load_dotenv()

from database import AsyncSessionLocal  # noqa: E402
from models import User, UserRole  # noqa: E402
from security import hash_password  # noqa: E402

# Known demo password (also documented in project memory). Only used for the
# e-signature re-auth step; Clerk handles actual login.
DEMO_PASSWORD = "ProtocolAI-Demo-2026"

DEMO_USERS = [
    ("system@pkbe.local", UserRole.generator),
    ("reviewer@pkbe.local", UserRole.reviewer),
    ("approver@pkbe.local", UserRole.approver),
]


async def _upsert(session, email: str, role: UserRole, org_id: str) -> str:
    email = email.strip().lower()
    result = await session.execute(select(User).where(User.email == email))
    existing = result.scalar_one_or_none()
    if existing:
        changed = existing.role != role or existing.org_id != org_id
        existing.role = role
        existing.org_id = org_id
        if not existing.hashed_password:
            existing.hashed_password = hash_password(DEMO_PASSWORD)
        return f"updated {email} -> {role.value} @ {org_id}" if changed else f"ok      {email} (unchanged)"
    session.add(
        User(
            id=uuid.uuid4(),
            org_id=org_id,
            email=email,
            role=role,
            hashed_password=hash_password(DEMO_PASSWORD),
        )
    )
    return f"created {email} -> {role.value} @ {org_id}"


async def main(email: str | None, role: str, org_id: str) -> None:
    async with AsyncSessionLocal() as session:
        for demo_email, demo_role in DEMO_USERS:
            print("  " + await _upsert(session, demo_email, demo_role, "default-org"))
        if email:
            print("  " + await _upsert(session, email, UserRole(role), org_id))
        await session.commit()
    print("Done.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", help="operator's Clerk-login email to provision")
    ap.add_argument("--role", default="generator", choices=[r.value for r in UserRole])
    ap.add_argument("--org", default="default-org", help="org_id for --email")
    args = ap.parse_args()
    asyncio.run(main(args.email, args.role, args.org))
