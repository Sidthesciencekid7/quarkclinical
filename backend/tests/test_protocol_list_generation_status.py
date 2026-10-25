"""GET /api/protocols/ — each row carries the async-generation lifecycle.

The app shell's protocol list shows a "generating" indicator for a protocol whose
background pipeline is still running (and a failure marker with the reason when
it could not finish). That needs ``generation_status`` / ``generation_error`` on
the lightweight ``ProtocolSummary`` row, so the list does not have to fetch every
protocol individually.

Rows are inserted straight into the test database (no PubMed/Anthropic), so this
exercises exactly the list query and the summary assembly. Identity is the
X-User-Id header (Clerk auth OFF), like the rest of the suite.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from models import Protocol, StudyType, User, UserRole
from security import hash_password

pytestmark = pytest.mark.asyncio

_URL = "/api/protocols/"
_BASE = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


async def _add_protocol(db, drug, user, *, minutes=0, sections=None, org_id=None, **extra):
    """Insert a protocol. ``generation_status`` / ``generation_error`` are only
    set when passed, so leaving them out exercises the column's server default
    (what every row from before migration 007 looks like)."""
    protocol = Protocol(
        id=uuid.uuid4(),
        org_id=org_id or user.org_id,
        drug_id=drug.id,
        study_type=StudyType.be,
        population_config={},
        sections=sections if sections is not None else {"synopsis": "<p>x</p>"},
        version=1,
        guideline_version="FDA-BE-2014",
        created_by=user.id,
        created_at=_BASE + timedelta(minutes=minutes),
        **extra,
    )
    db.add(protocol)
    await db.commit()
    return protocol


async def _other_org_user(db) -> User:
    user = User(
        id=uuid.uuid4(),
        org_id="other-org",
        email="generator@other-org.example",
        role=UserRole.generator,
        hashed_password=hash_password(uuid.uuid4().hex),
    )
    db.add(user)
    await db.commit()
    return user


def _hdr(user) -> dict:
    return {"X-User-Id": str(user.id)}


async def _rows_by_id(api_client, user) -> dict[str, dict]:
    resp = await api_client.get(_URL, headers=_hdr(user))
    assert resp.status_code == 200, resp.text
    return {r["id"]: r for r in resp.json()}


# ---------------------------------------------------------------------------


async def test_generating_protocol_is_listed_as_generating(api_client, seeded):
    protocol = await _add_protocol(
        seeded.db, seeded.drug, seeded.user, sections={}, generation_status="generating",
    )
    row = (await _rows_by_id(api_client, seeded.user))[str(protocol.id)]
    assert row["generation_status"] == "generating"
    assert row["generation_error"] is None
    assert row["section_count"] == 0


async def test_failed_protocol_carries_its_error_message(api_client, seeded):
    protocol = await _add_protocol(
        seeded.db, seeded.drug, seeded.user, sections={},
        generation_status="failed",
        generation_error="The AI service ran out of credits before the protocol finished.",
    )
    row = (await _rows_by_id(api_client, seeded.user))[str(protocol.id)]
    assert row["generation_status"] == "failed"
    assert row["generation_error"] == (
        "The AI service ran out of credits before the protocol finished."
    )


async def test_complete_protocol_is_listed_as_complete_with_no_error(api_client, seeded):
    protocol = await _add_protocol(
        seeded.db, seeded.drug, seeded.user, generation_status="complete",
    )
    row = (await _rows_by_id(api_client, seeded.user))[str(protocol.id)]
    assert row["generation_status"] == "complete"
    assert row["generation_error"] is None
    assert row["section_count"] == 1


async def test_row_without_an_explicit_status_reads_as_complete(api_client, seeded):
    """A pre-migration-007 row (column filled by its server default) is finished."""
    protocol = await _add_protocol(seeded.db, seeded.drug, seeded.user)
    row = (await _rows_by_id(api_client, seeded.user))[str(protocol.id)]
    assert row["generation_status"] == "complete"
    assert row["generation_error"] is None


async def test_each_row_reports_its_own_status(api_client, seeded):
    complete = await _add_protocol(seeded.db, seeded.drug, seeded.user, minutes=0)
    generating = await _add_protocol(
        seeded.db, seeded.drug, seeded.user, minutes=10, sections={},
        generation_status="generating",
    )
    failed = await _add_protocol(
        seeded.db, seeded.drug, seeded.user, minutes=20, sections={},
        generation_status="failed", generation_error="boom",
    )

    resp = await api_client.get(_URL, headers=_hdr(seeded.user))
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    # Newest first, as before.
    assert [r["id"] for r in rows] == [str(failed.id), str(generating.id), str(complete.id)]
    assert [r["generation_status"] for r in rows] == ["failed", "generating", "complete"]
    assert [r["generation_error"] for r in rows] == ["boom", None, None]


async def test_summary_stays_lightweight_and_keeps_its_existing_fields(api_client, seeded):
    protocol = await _add_protocol(seeded.db, seeded.drug, seeded.user)
    row = (await _rows_by_id(api_client, seeded.user))[str(protocol.id)]
    assert "sections" not in row
    for key in (
        "id", "drug_id", "drug_inn", "drug_brand_names", "study_type",
        "guideline_version", "guideline_outdated", "version", "status",
        "section_count", "created_at", "generation_status", "generation_error",
    ):
        assert key in row, key
    assert row["drug_inn"] == "metformin"


async def test_generating_rows_stay_org_scoped(api_client, seeded):
    mine = await _add_protocol(
        seeded.db, seeded.drug, seeded.user, sections={}, generation_status="generating",
    )
    other_user = await _other_org_user(seeded.db)
    theirs = await _add_protocol(
        seeded.db, seeded.drug, other_user, sections={}, generation_status="generating",
    )

    mine_rows = await _rows_by_id(api_client, seeded.user)
    assert set(mine_rows) == {str(mine.id)}

    their_rows = await _rows_by_id(api_client, other_user)
    assert set(their_rows) == {str(theirs.id)}
    assert their_rows[str(theirs.id)]["generation_status"] == "generating"
