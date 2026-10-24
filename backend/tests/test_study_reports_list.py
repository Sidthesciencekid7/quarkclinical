"""GET /api/study-reports — the org-wide study-report list.

Rows are inserted straight into the test database (no PubMed/Anthropic, no
dataset upload), so this exercises exactly the list query: org scoping, the
protocol-title / drug-INN join, newest-first ordering, and the JSONB-derived
`verdict` / `has_results`. Identity is the X-User-Id header (Clerk auth OFF),
like the rest of the suite.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from models import Drug, Protocol, StudyReport, StudyType, User, UserRole
from security import hash_password

pytestmark = pytest.mark.asyncio

_URL = "/api/study-reports"
_BASE = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
_ROW_KEYS = {
    "id", "protocol_id", "protocol_title", "drug_inn", "study_type", "input_kind",
    "version", "generation_status", "has_results", "verdict", "created_at", "updated_at",
}


async def _add_protocol(db, drug, user, *, title="<p>A study</p>", study_type=StudyType.be, org_id=None):
    sections = {"synopsis": "<p>x</p>"}
    if title is not None:
        sections["title"] = title
    protocol = Protocol(
        id=uuid.uuid4(),
        org_id=org_id or user.org_id,
        drug_id=drug.id,
        study_type=study_type,
        population_config={},
        sections=sections,
        version=1,
        guideline_version="FDA-BE-2014",
        created_by=user.id,
    )
    db.add(protocol)
    await db.commit()
    return protocol


async def _add_report(
    db, protocol, user, *, minutes=0, results=None, input_kind="params",
    generation_status="complete", version=1, org_id=None,
):
    report = StudyReport(
        id=uuid.uuid4(),
        protocol_id=protocol.id,
        org_id=org_id or protocol.org_id,
        input_kind=input_kind,
        dataset_summary={},
        results=results if results is not None else {},
        sections={"conclusion": "<p>BIG</p>"},
        version=version,
        generation_status=generation_status,
        created_by=user.id,
        created_at=_BASE + timedelta(minutes=minutes),
    )
    db.add(report)
    await db.commit()
    return report


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


# ---------------------------------------------------------------------------


async def test_empty_list(api_client, seeded):
    resp = await api_client.get(_URL, headers=_hdr(seeded.user))
    assert resp.status_code == 200, resp.text
    assert resp.json() == []


async def test_requires_an_identity(api_client, seeded):
    assert (await api_client.get(_URL)).status_code == 401


async def test_row_shape_and_join_fields(api_client, seeded):
    protocol = await _add_protocol(
        seeded.db, seeded.drug, seeded.user,
        title="<h1>Bioequivalence of  <b>Metformin</b></h1>\n<p>850 mg tablets</p>",
    )
    report = await _add_report(
        seeded.db, protocol, seeded.user,
        results={"verdict": "bioequivalent", "metrics": {"cmax": {"gmr": 1.01}}},
        input_kind="nca", version=3,
    )

    resp = await api_client.get(_URL, headers=_hdr(seeded.user))
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert len(rows) == 1
    row = rows[0]
    assert set(row) == _ROW_KEYS  # no results / sections blobs leak into the list
    assert row["id"] == str(report.id)
    assert row["protocol_id"] == str(protocol.id)
    assert row["drug_inn"] == "metformin"
    assert row["study_type"] == "BE"
    assert row["input_kind"] == "nca"
    assert row["version"] == 3
    assert row["generation_status"] == "complete"
    assert row["has_results"] is True
    assert row["verdict"] == "bioequivalent"
    # TipTap HTML flattened to a single trimmed plain-text line
    assert "<" not in row["protocol_title"] and "\n" not in row["protocol_title"]
    assert row["protocol_title"] == "Bioequivalence of Metformin 850 mg tablets"


async def test_missing_title_falls_back_to_the_drug_name(api_client, seeded):
    protocol = await _add_protocol(seeded.db, seeded.drug, seeded.user, title=None)
    await _add_report(seeded.db, protocol, seeded.user)
    row = (await api_client.get(_URL, headers=_hdr(seeded.user))).json()[0]
    assert row["protocol_title"] == "Metformin"


async def test_empty_results_means_no_results_and_no_verdict(api_client, seeded):
    protocol = await _add_protocol(seeded.db, seeded.drug, seeded.user)
    await _add_report(seeded.db, protocol, seeded.user, results={}, generation_status="complete")
    row = (await api_client.get(_URL, headers=_hdr(seeded.user))).json()[0]
    assert row["has_results"] is False
    assert row["verdict"] is None


async def test_results_without_a_verdict_still_count_as_results(api_client, seeded):
    protocol = await _add_protocol(seeded.db, seeded.drug, seeded.user)
    await _add_report(seeded.db, protocol, seeded.user, results={"metrics": {}})
    row = (await api_client.get(_URL, headers=_hdr(seeded.user))).json()[0]
    assert row["has_results"] is True
    assert row["verdict"] is None


async def test_newest_first_across_protocols(api_client, seeded):
    p1 = await _add_protocol(seeded.db, seeded.drug, seeded.user, title="<p>First</p>")
    p2 = await _add_protocol(seeded.db, seeded.drug, seeded.user, title="<p>Second</p>")
    oldest = await _add_report(seeded.db, p1, seeded.user, minutes=0)
    middle = await _add_report(seeded.db, p2, seeded.user, minutes=10)
    newest = await _add_report(seeded.db, p1, seeded.user, minutes=20)

    rows = (await api_client.get(_URL, headers=_hdr(seeded.user))).json()
    assert [r["id"] for r in rows] == [str(newest.id), str(middle.id), str(oldest.id)]
    assert [r["protocol_title"] for r in rows] == ["First", "Second", "First"]


async def test_generating_report_is_listed_with_its_status(api_client, seeded):
    protocol = await _add_protocol(seeded.db, seeded.drug, seeded.user)
    await _add_report(seeded.db, protocol, seeded.user, generation_status="generating")
    row = (await api_client.get(_URL, headers=_hdr(seeded.user))).json()[0]
    assert row["generation_status"] == "generating"


async def test_org_isolation_both_directions(api_client, seeded):
    mine_protocol = await _add_protocol(seeded.db, seeded.drug, seeded.user, title="<p>Mine</p>")
    mine = await _add_report(seeded.db, mine_protocol, seeded.user)

    other_user = await _other_org_user(seeded.db)
    theirs_protocol = await _add_protocol(seeded.db, seeded.drug, other_user, title="<p>Theirs</p>")
    theirs = await _add_report(seeded.db, theirs_protocol, other_user)

    mine_rows = (await api_client.get(_URL, headers=_hdr(seeded.user))).json()
    assert [r["id"] for r in mine_rows] == [str(mine.id)]

    their_rows = (await api_client.get(_URL, headers=_hdr(other_user))).json()
    assert [r["id"] for r in their_rows] == [str(theirs.id)]
    assert their_rows[0]["protocol_title"] == "Theirs"


async def test_every_org_member_sees_the_orgs_reports(api_client, seeded):
    """Study reports are org-shared (unlike chat transcripts): a different user
    in the same org sees a report another member created."""
    protocol = await _add_protocol(seeded.db, seeded.drug, seeded.user)
    report = await _add_report(seeded.db, protocol, seeded.user)
    rows = (await api_client.get(_URL, headers=_hdr(seeded.approver))).json()
    assert [r["id"] for r in rows] == [str(report.id)]


async def test_trailing_slash_is_served_without_a_redirect(api_client, seeded):
    protocol = await _add_protocol(seeded.db, seeded.drug, seeded.user)
    await _add_report(seeded.db, protocol, seeded.user)
    resp = await api_client.get(_URL + "/", headers=_hdr(seeded.user))
    assert resp.status_code == 200, resp.text
    assert len(resp.json()) == 1


async def test_existing_study_report_routes_are_not_shadowed(api_client, seeded):
    protocol = await _add_protocol(seeded.db, seeded.drug, seeded.user)
    report = await _add_report(seeded.db, protocol, seeded.user, results={"verdict": "bioequivalent"})
    hdr = _hdr(seeded.user)

    one = await api_client.get(f"{_URL}/{report.id}", headers=hdr)
    assert one.status_code == 200 and one.json()["id"] == str(report.id)

    template = await api_client.get(f"{_URL}/template", params={"kind": "params"}, headers=hdr)
    assert template.status_code == 200
    assert "text/csv" in template.headers["content-type"]

    per_protocol = await api_client.get(f"/api/protocols/{protocol.id}/study-reports", headers=hdr)
    assert per_protocol.status_code == 200 and len(per_protocol.json()) == 1
