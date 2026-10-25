"""Integration tests for the PK/BE Protocol Platform generation pipeline.

These tests exercise the full request/response cycle through the FastAPI app
against a real PostgreSQL test database, with external HTTP calls mocked:
  - PubMed E-utilities  →  pytest-httpx (httpx_mock fixture)
  - Anthropic API       →  pytest-mock  (mock_anthropic fixture)
  - Anthropic /health ping  →  pytest-httpx

Prerequisites
-------------
1. A running PostgreSQL instance accessible at DATABASE_URL_TEST.
2. ``pip install -r requirements.txt -r requirements-dev.txt``
3. ``pytest``  (picks up pytest.ini settings automatically)
"""

from __future__ import annotations

import io
import re
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from models import AuditLog, Drug, PKParameterRecord, Protocol, ProtocolStatus, ProtocolVersion, User, UserRole
from routers.protocols import SECTION_ORDER
from security import hash_password
from tests.conftest import PUBMED_ESEARCH_PMIDS, SEEDED_PASSWORD, register_pubmed_mocks

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_STUDY_PAYLOAD_BASE = {
    "study_type": "BE",
    "population_config": {
        "dose_strength": "850 mg",
        "formulation": "tablet",
        "org_name": "TestCRO Inc.",
    },
    "org_id": "test-org",
    "guideline_version": "FDA-BE-2014",
}


def _protocol_payload(drug_id: str, user_id: str) -> dict:
    return {**_STUDY_PAYLOAD_BASE, "drug_id": drug_id, "created_by": user_id}


# ---------------------------------------------------------------------------
# Test 1: Health endpoint — both checks pass
# ---------------------------------------------------------------------------


async def test_health_reports_a_rejected_key(api_client, httpx_mock):
    """A key Anthropic rejects is reported as invalid_api_key (status degraded),
    not "ok" - every generation and chat call would fail with it."""
    import os

    os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-health-check")

    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"^https://api\.anthropic\.com/v1/models(\?.*)?$"),
        status_code=401,  # key rejected: reachable, but every AI call would fail
        json={"error": {"type": "authentication_error"}},
    )

    resp = await api_client.get("/health")
    assert resp.status_code == 200  # liveness probes keep passing
    body = resp.json()
    assert body["checks"]["database"] == "ok"
    assert body["checks"]["anthropic_api"] == "invalid_api_key"
    assert body["status"] == "degraded"


async def test_health_ok_with_a_valid_key(api_client, httpx_mock):
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"^https://api\.anthropic\.com/v1/models(\?.*)?$"),
        status_code=200,
        json={"data": []},
    )
    body = (await api_client.get("/health")).json()
    assert body["checks"]["anthropic_api"] == "ok"
    assert body["status"] == "ok"


async def test_health_no_api_key(api_client):
    """Health endpoint gracefully handles missing ANTHROPIC_API_KEY."""
    import os

    # Temporarily remove the key so the health check hits the "no_api_key" branch
    original = os.environ.pop("ANTHROPIC_API_KEY", None)
    try:
        resp = await api_client.get("/health")
    finally:
        if original is not None:
            os.environ["ANTHROPIC_API_KEY"] = original

    assert resp.status_code == 200
    body = resp.json()
    assert body["checks"]["database"] == "ok"
    assert body["checks"]["anthropic_api"] == "no_api_key_configured"
    assert body["status"] == "degraded"


# ---------------------------------------------------------------------------
# Test 2: Full generation pipeline
# ---------------------------------------------------------------------------


async def test_create_protocol_generates_all_sections(
    api_client, seeded, mock_anthropic, httpx_mock
):
    """POST /api/protocols creates a protocol with all 13 sections populated."""
    register_pubmed_mocks(httpx_mock)

    payload = _protocol_payload(str(seeded.drug.id), str(seeded.user.id))
    resp = await api_client.post(
        "/api/protocols/", json=payload, headers={"X-User-Id": str(seeded.user.id)},
    )
    assert resp.status_code == 201, resp.text

    data = resp.json()
    assert "id" in data
    assert data["drug_id"] == str(seeded.drug.id)
    assert data["status"] == "draft"
    assert data["version"] == 1
    assert data["guideline_version"] == "FDA-BE-2014"
    assert data["guideline_outdated"] is False

    # All 13 sections must be present and non-empty
    sections = data["sections"]
    assert set(sections.keys()) >= set(SECTION_ORDER), (
        f"Missing sections: {set(SECTION_ORDER) - set(sections.keys())}"
    )
    assert len(sections) == len(SECTION_ORDER)
    for name in SECTION_ORDER:
        assert sections[name], f"Section '{name}' is empty"

    return data["id"]  # returned so callers can reference it


async def test_create_protocol_persists_pk_parameters(
    api_client, seeded, mock_anthropic, httpx_mock
):
    """PK parameters extracted by the AI mock are written to pk_parameters table."""
    register_pubmed_mocks(httpx_mock)

    payload = _protocol_payload(str(seeded.drug.id), str(seeded.user.id))
    resp = await api_client.post(
        "/api/protocols/", json=payload, headers={"X-User-Id": str(seeded.user.id)},
    )
    assert resp.status_code == 201

    # Only the two parameters with PMIDs in PUBMED_ESEARCH_PMIDS should survive
    # (the bioavailability row has PMID 99999999 which is not in the mock list
    #  and should have been filtered out by _validate_pmids)
    result = await seeded.db.execute(
        select(PKParameterRecord).where(PKParameterRecord.drug_id == seeded.drug.id)
    )
    params = result.scalars().all()
    pmids_stored = {p.source_pmid for p in params}
    assert pmids_stored.issubset(set(PUBMED_ESEARCH_PMIDS)), (
        f"Unexpected PMIDs stored: {pmids_stored - set(PUBMED_ESEARCH_PMIDS)}"
    )


async def test_create_protocol_writes_version_snapshot(
    api_client, seeded, mock_anthropic, httpx_mock
):
    """Protocol creation writes an INITIAL version snapshot."""
    register_pubmed_mocks(httpx_mock)

    payload = _protocol_payload(str(seeded.drug.id), str(seeded.user.id))
    resp = await api_client.post(
        "/api/protocols/", json=payload, headers={"X-User-Id": str(seeded.user.id)},
    )
    assert resp.status_code == 201
    protocol_id = resp.json()["id"]

    result = await seeded.db.execute(
        select(ProtocolVersion).where(
            ProtocolVersion.protocol_id == uuid.UUID(protocol_id)
        )
    )
    versions = result.scalars().all()
    assert len(versions) == 1
    snap = versions[0]
    assert snap.version == 1
    assert snap.change_type == "INITIAL"
    assert set(snap.sections.keys()) >= set(SECTION_ORDER)


async def test_create_protocol_writes_audit_log(
    api_client, seeded, mock_anthropic, httpx_mock
):
    """Protocol creation causes at least one audit_log entry for the protocol resource."""
    register_pubmed_mocks(httpx_mock)

    payload = _protocol_payload(str(seeded.drug.id), str(seeded.user.id))
    resp = await api_client.post(
        "/api/protocols/",
        json=payload,
        headers={"X-User-Id": str(seeded.user.id)},
    )
    assert resp.status_code == 201
    protocol_id = resp.json()["id"]

    # audit_logger dependency writes an entry via a separate session after the
    # route completes.  Give the event loop a tick to commit it.
    import asyncio
    await asyncio.sleep(0)

    result = await seeded.db.execute(
        select(AuditLog).where(
            AuditLog.resource_type == "protocol",
            AuditLog.resource_id == protocol_id,
        )
    )
    logs = result.scalars().all()
    assert len(logs) >= 1
    actions = {log.action for log in logs}
    assert any("protocol" in a.lower() or "POST" in a for a in actions)


async def test_create_protocol_requires_generator_role(api_client, seeded):
    """POST /api/protocols with a non-generator (Reviewer/Approver) actor is
    rejected — only Authors draft protocols (separation of duties, B2 RBAC).
    The role/actor check happens before any literature fetch or AI call, so
    this test needs neither httpx_mock nor mock_anthropic."""
    payload = _protocol_payload(str(seeded.drug.id), str(seeded.user.id))
    resp = await api_client.post(
        "/api/protocols/",
        json=payload,
        headers={"X-User-Id": str(seeded.approver.id)},
    )
    assert resp.status_code == 403

    resp_no_actor = await api_client.post("/api/protocols/", json=payload)
    assert resp_no_actor.status_code == 401


# ---------------------------------------------------------------------------
# Test 3: Section edit increments version
# ---------------------------------------------------------------------------


async def test_section_edit_increments_version(
    api_client, seeded, mock_anthropic, httpx_mock
):
    """PUT a section edit → protocol.version increments + new snapshot written."""
    register_pubmed_mocks(httpx_mock)

    # Create a protocol first
    payload = _protocol_payload(str(seeded.drug.id), str(seeded.user.id))
    create_resp = await api_client.post(
        "/api/protocols/", json=payload, headers={"X-User-Id": str(seeded.user.id)},
    )
    assert create_resp.status_code == 201
    protocol_id = create_resp.json()["id"]
    initial_version = create_resp.json()["version"]
    assert initial_version == 1

    # Edit the introduction section manually
    edit_resp = await api_client.put(
        f"/api/protocols/{protocol_id}/sections/introduction",
        json={"content": "<p>Updated introduction for test purposes.</p>"},
        headers={"X-User-Id": str(seeded.user.id)},
    )
    assert edit_resp.status_code == 200, edit_resp.text
    edit_data = edit_resp.json()
    assert edit_data["version"] == 2
    assert edit_data["section_name"] == "introduction"

    # Confirm version was incremented in the DB
    protocol = await seeded.db.get(Protocol, uuid.UUID(protocol_id))
    await seeded.db.refresh(protocol)
    assert protocol.version == 2

    # Confirm a new snapshot exists for version 2
    result = await seeded.db.execute(
        select(ProtocolVersion).where(
            ProtocolVersion.protocol_id == uuid.UUID(protocol_id),
            ProtocolVersion.version == 2,
        )
    )
    snap2 = result.scalar_one_or_none()
    assert snap2 is not None
    assert snap2.change_type == "MANUAL_EDIT"
    assert snap2.sections["introduction"] == "<p>Updated introduction for test purposes.</p>"


async def test_section_edit_writes_audit_log(
    api_client, seeded, mock_anthropic, httpx_mock
):
    """Editing a section creates a MANUAL_EDIT entry in audit_logs."""
    register_pubmed_mocks(httpx_mock)

    payload = _protocol_payload(str(seeded.drug.id), str(seeded.user.id))
    create_resp = await api_client.post(
        "/api/protocols/", json=payload, headers={"X-User-Id": str(seeded.user.id)},
    )
    protocol_id = create_resp.json()["id"]

    await api_client.put(
        f"/api/protocols/{protocol_id}/sections/synopsis",
        json={"content": "<p>Test synopsis content.</p>"},
        headers={"X-User-Id": str(seeded.user.id)},
    )

    import asyncio
    await asyncio.sleep(0)

    result = await seeded.db.execute(
        select(AuditLog).where(
            AuditLog.resource_type == "protocol",
            AuditLog.action.like("%MANUAL_EDIT%"),
        )
    )
    logs = result.scalars().all()
    assert len(logs) >= 1
    assert any("synopsis" in (log.action or "") for log in logs)


async def test_section_edit_requires_generator_role(
    api_client, seeded, mock_anthropic, httpx_mock
):
    """PUT a section edit as Reviewer/Approver is rejected — same separation of
    duties as protocol creation (B2 RBAC). Also covers save_section_edit
    previously having no auth dependency at all (any anonymous caller could
    edit content before this fix)."""
    register_pubmed_mocks(httpx_mock)

    payload = _protocol_payload(str(seeded.drug.id), str(seeded.user.id))
    create_resp = await api_client.post(
        "/api/protocols/", json=payload, headers={"X-User-Id": str(seeded.user.id)},
    )
    protocol_id = create_resp.json()["id"]

    as_approver = await api_client.put(
        f"/api/protocols/{protocol_id}/sections/synopsis",
        json={"content": "<p>Approver should not be able to write this.</p>"},
        headers={"X-User-Id": str(seeded.approver.id)},
    )
    assert as_approver.status_code == 403

    anonymous = await api_client.put(
        f"/api/protocols/{protocol_id}/sections/synopsis",
        json={"content": "<p>Anonymous should not be able to write this.</p>"},
    )
    assert anonymous.status_code == 401

    # Section must remain untouched by both rejected attempts
    protocol = await seeded.db.get(Protocol, uuid.UUID(protocol_id))
    await seeded.db.refresh(protocol)
    assert "should not be able to write" not in protocol.sections["synopsis"]


# ---------------------------------------------------------------------------
# Test 4: Version history endpoints
# ---------------------------------------------------------------------------


async def test_get_version_history(api_client, seeded, mock_anthropic, httpx_mock):
    """GET /versions returns a list with one entry after creation."""
    register_pubmed_mocks(httpx_mock)

    payload = _protocol_payload(str(seeded.drug.id), str(seeded.user.id))
    resp = await api_client.post(
        "/api/protocols/", json=payload, headers={"X-User-Id": str(seeded.user.id)},
    )
    protocol_id = resp.json()["id"]

    hist_resp = await api_client.get(
        f"/api/protocols/{protocol_id}/versions",
        headers={"X-User-Id": str(seeded.user.id)},
    )
    assert hist_resp.status_code == 200
    versions = hist_resp.json()
    assert isinstance(versions, list)
    assert len(versions) >= 1
    assert versions[0]["version"] == 1
    assert versions[0]["change_type"] == "INITIAL"


async def test_get_specific_version(api_client, seeded, mock_anthropic, httpx_mock):
    """GET /versions/1 returns the full section snapshot for version 1."""
    register_pubmed_mocks(httpx_mock)

    payload = _protocol_payload(str(seeded.drug.id), str(seeded.user.id))
    resp = await api_client.post(
        "/api/protocols/", json=payload, headers={"X-User-Id": str(seeded.user.id)},
    )
    protocol_id = resp.json()["id"]

    ver_resp = await api_client.get(
        f"/api/protocols/{protocol_id}/versions/1",
        headers={"X-User-Id": str(seeded.user.id)},
    )
    assert ver_resp.status_code == 200
    ver_data = ver_resp.json()
    assert ver_data["version"] == 1
    assert set(ver_data["sections"].keys()) >= set(SECTION_ORDER)


# ---------------------------------------------------------------------------
# Test 5: Diff endpoint
# ---------------------------------------------------------------------------


async def test_diff_after_edit(api_client, seeded, mock_anthropic, httpx_mock):
    """Diff between v1 and v2 correctly identifies the edited section."""
    register_pubmed_mocks(httpx_mock)

    payload = _protocol_payload(str(seeded.drug.id), str(seeded.user.id))
    resp = await api_client.post(
        "/api/protocols/", json=payload, headers={"X-User-Id": str(seeded.user.id)},
    )
    protocol_id = resp.json()["id"]

    # Edit objectives
    await api_client.put(
        f"/api/protocols/{protocol_id}/sections/objectives",
        json={"content": "<p>New objectives text for diff test.</p>"},
        headers={"X-User-Id": str(seeded.user.id)},
    )

    diff_resp = await api_client.get(
        f"/api/protocols/{protocol_id}/diff",
        params={"from_version": 1, "to_version": 2},
        headers={"X-User-Id": str(seeded.user.id)},
    )
    assert diff_resp.status_code == 200
    diff = diff_resp.json()
    assert diff["from_version"] == 1
    assert diff["to_version"] == 2
    assert diff["sections_changed"] >= 1

    modified = [s for s in diff["sections"] if s["status"] == "modified"]
    modified_names = [s["section_name"] for s in modified]
    assert "objectives" in modified_names


# ---------------------------------------------------------------------------
# Test 6: DOCX export
# ---------------------------------------------------------------------------


async def test_export_docx_returns_valid_docx(
    api_client, seeded, mock_anthropic, httpx_mock
):
    """POST /api/export/docx returns a well-formed .docx (valid ZIP/OOXML container)."""
    register_pubmed_mocks(httpx_mock)

    payload = _protocol_payload(str(seeded.drug.id), str(seeded.user.id))
    create_resp = await api_client.post(
        "/api/protocols/", json=payload, headers={"X-User-Id": str(seeded.user.id)},
    )
    assert create_resp.status_code == 201
    protocol_id = create_resp.json()["id"]

    export_resp = await api_client.post(
        "/api/export/docx",
        json={"protocol_id": protocol_id},
        headers={"X-User-Id": str(seeded.user.id)},
    )
    assert export_resp.status_code == 200, export_resp.text
    assert export_resp.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )

    # A .docx is a ZIP archive — verify the magic bytes
    content = export_resp.content
    assert len(content) > 1024, "DOCX should be larger than 1 KB"
    assert content[:2] == b"PK", "DOCX must begin with the ZIP magic bytes 'PK'"

    # Parse with python-docx to confirm it's a valid document
    from docx import Document as DocxDocument

    doc = DocxDocument(io.BytesIO(content))
    # Should have at least as many paragraphs as there are sections
    assert len(doc.paragraphs) >= len(SECTION_ORDER)


async def test_export_docx_writes_audit_log(
    api_client, seeded, mock_anthropic, httpx_mock
):
    """Exporting a .docx appends an export entry to audit_logs."""
    register_pubmed_mocks(httpx_mock)

    payload = _protocol_payload(str(seeded.drug.id), str(seeded.user.id))
    create_resp = await api_client.post(
        "/api/protocols/", json=payload, headers={"X-User-Id": str(seeded.user.id)},
    )
    protocol_id = create_resp.json()["id"]

    await api_client.post(
        "/api/export/docx",
        json={"protocol_id": protocol_id},
        headers={"X-User-Id": str(seeded.user.id)},
    )

    import asyncio
    await asyncio.sleep(0)

    result = await seeded.db.execute(
        select(AuditLog).where(AuditLog.resource_type == "export")
    )
    logs = result.scalars().all()
    assert len(logs) >= 1


# ---------------------------------------------------------------------------
# Test 6b: Page-numbered amendment appendix (B4)
# ---------------------------------------------------------------------------


async def test_docx_amendment_appendix_has_pageref_field(
    api_client, seeded, mock_anthropic, httpx_mock
):
    """After an edit creates an amendment, the DOCX appendix's Section cell
    carries a live Word PAGEREF field bound to that section's bookmarked
    heading — not just a section number."""
    import zipfile

    register_pubmed_mocks(httpx_mock)

    payload = _protocol_payload(str(seeded.drug.id), str(seeded.user.id))
    create_resp = await api_client.post(
        "/api/protocols/", json=payload, headers={"X-User-Id": str(seeded.user.id)},
    )
    protocol_id = create_resp.json()["id"]

    await api_client.put(
        f"/api/protocols/{protocol_id}/sections/objectives",
        json={"content": "<p>Amended objectives text for the page-number test.</p>"},
        headers={"X-User-Id": str(seeded.user.id)},
    )

    export_resp = await api_client.post(
        "/api/export/docx",
        json={"protocol_id": protocol_id},
        headers={"X-User-Id": str(seeded.user.id)},
    )
    assert export_resp.status_code == 200, export_resp.text

    xml = zipfile.ZipFile(io.BytesIO(export_resp.content)).read("word/document.xml").decode("utf-8")
    assert "w:bookmarkStart" in xml
    assert "PAGEREF sec_objectives \\h" in xml


async def test_pdf_amendment_appendix_shows_resolved_page_number(
    api_client, seeded, mock_anthropic, httpx_mock
):
    """The PDF appendix's Section cell shows the section's actual resolved
    page number ('Section N — Title, p. X'), not just the section number."""
    import re

    register_pubmed_mocks(httpx_mock)

    payload = _protocol_payload(str(seeded.drug.id), str(seeded.user.id))
    create_resp = await api_client.post(
        "/api/protocols/", json=payload, headers={"X-User-Id": str(seeded.user.id)},
    )
    protocol_id = create_resp.json()["id"]

    await api_client.put(
        f"/api/protocols/{protocol_id}/sections/objectives",
        json={"content": "<p>Amended objectives text for the page-number test.</p>"},
        headers={"X-User-Id": str(seeded.user.id)},
    )

    export_resp = await api_client.post(
        "/api/export/pdf",
        json={"protocol_id": protocol_id},
        headers={"X-User-Id": str(seeded.user.id)},
    )
    assert export_resp.status_code == 200, export_resp.text

    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(export_resp.content))
    text = re.sub(r"\s+", " ", "\n".join(p.extract_text() or "" for p in reader.pages))
    assert re.search(r"Section \d+ .{1,3}\s+Study\s+Objectives,\s*p\.\s*\d+", text)


# ---------------------------------------------------------------------------
# Test 7: E-signature (21 CFR Part 11)
# ---------------------------------------------------------------------------


async def test_esign_requires_approver_role(
    api_client, seeded, mock_anthropic, httpx_mock
):
    """POST /sign with a generator-role user should return 403."""
    register_pubmed_mocks(httpx_mock)

    payload = _protocol_payload(str(seeded.drug.id), str(seeded.user.id))
    resp = await api_client.post(
        "/api/protocols/", json=payload, headers={"X-User-Id": str(seeded.user.id)},
    )
    protocol_id = resp.json()["id"]

    sign_resp = await api_client.post(
        f"/api/protocols/{protocol_id}/sign",
        json={
            "signatory_name": "Dr. Generator",
            "reason": "Should fail",
            "reauth_password": seeded.password,
        },
        headers={"X-User-Id": str(seeded.user.id)},  # generator — not allowed
    )
    assert sign_resp.status_code == 403


async def test_esign_approver_succeeds(
    api_client, seeded, mock_anthropic, httpx_mock
):
    """POST /sign with approver role + correct password approves the protocol,
    stores the SHA-256 hash, and binds the role-derived signature meaning."""
    register_pubmed_mocks(httpx_mock)

    payload = _protocol_payload(str(seeded.drug.id), str(seeded.user.id))
    create_resp = await api_client.post(
        "/api/protocols/", json=payload, headers={"X-User-Id": str(seeded.user.id)},
    )
    protocol_id = create_resp.json()["id"]

    sign_resp = await api_client.post(
        f"/api/protocols/{protocol_id}/sign",
        json={
            "signatory_name": "Dr. Approver",
            "reason": "Approved for submission",
            "reauth_password": seeded.password,
        },
        headers={"X-User-Id": str(seeded.approver.id)},
    )
    assert sign_resp.status_code == 200, sign_resp.text
    sign_data = sign_resp.json()
    assert sign_data["status"] == ProtocolStatus.approved.value
    assert len(sign_data["sha256_hash"]) == 64  # hex-encoded SHA-256
    assert sign_data["signatory_email"] == seeded.approver.email
    assert sign_data["meaning"] == "Approved"

    # Protocol in DB should now be approved
    protocol = await seeded.db.get(Protocol, uuid.UUID(protocol_id))
    await seeded.db.refresh(protocol)
    assert protocol.status == ProtocolStatus.approved

    # A SIGNED snapshot should exist
    result = await seeded.db.execute(
        select(ProtocolVersion).where(
            ProtocolVersion.protocol_id == uuid.UUID(protocol_id),
            ProtocolVersion.change_type == "SIGNED",
        )
    )
    signed_snap = result.scalar_one_or_none()
    assert signed_snap is not None
    assert signed_snap.change_detail["meaning"] == "Approved"


async def test_esign_wrong_password_returns_401(
    api_client, seeded, mock_anthropic, httpx_mock
):
    """POST /sign with the correct role but an incorrect password should return
    401 — a live session/correct role alone must not be sufficient to sign
    (21 CFR Part 11 §11.200(a)(1) re-authentication requirement)."""
    register_pubmed_mocks(httpx_mock)

    payload = _protocol_payload(str(seeded.drug.id), str(seeded.user.id))
    create_resp = await api_client.post(
        "/api/protocols/", json=payload, headers={"X-User-Id": str(seeded.user.id)},
    )
    protocol_id = create_resp.json()["id"]

    sign_resp = await api_client.post(
        f"/api/protocols/{protocol_id}/sign",
        json={
            "signatory_name": "Dr. Approver",
            "reason": "Approved for submission",
            "reauth_password": "definitely-not-the-password",
        },
        headers={"X-User-Id": str(seeded.approver.id)},
    )
    assert sign_resp.status_code == 401

    # Protocol must remain untouched — a failed re-auth is not a partial sign
    protocol = await seeded.db.get(Protocol, uuid.UUID(protocol_id))
    await seeded.db.refresh(protocol)
    assert protocol.status != ProtocolStatus.approved


async def test_esign_already_approved_returns_409(
    api_client, seeded, mock_anthropic, httpx_mock
):
    """Attempting to sign an already-approved protocol returns 409 Conflict."""
    register_pubmed_mocks(httpx_mock)

    payload = _protocol_payload(str(seeded.drug.id), str(seeded.user.id))
    create_resp = await api_client.post(
        "/api/protocols/", json=payload, headers={"X-User-Id": str(seeded.user.id)},
    )
    protocol_id = create_resp.json()["id"]

    headers = {"X-User-Id": str(seeded.approver.id)}
    sign_body = {
        "signatory_name": "Dr. Approver",
        "reason": "First sign",
        "reauth_password": seeded.password,
    }

    first = await api_client.post(f"/api/protocols/{protocol_id}/sign", json=sign_body, headers=headers)
    assert first.status_code == 200

    second = await api_client.post(f"/api/protocols/{protocol_id}/sign", json=sign_body, headers=headers)
    assert second.status_code == 409


async def test_esign_cross_org_signatory_returns_403(
    api_client, seeded, mock_anthropic, httpx_mock
):
    """An approver from a different org than the protocol's must not be able to
    sign it, even with the correct role and password — org_id is an access
    boundary, not just a stored label (see B3 org-isolation work)."""
    register_pubmed_mocks(httpx_mock)

    other_org_password = "another-org-secret"
    other_org_approver = User(
        id=uuid.uuid4(),
        org_id="other-org",
        email="approver@othercro.com",
        role=UserRole.approver,
        hashed_password=hash_password(other_org_password),
    )
    seeded.db.add(other_org_approver)
    await seeded.db.commit()
    await seeded.db.refresh(other_org_approver)

    payload = _protocol_payload(str(seeded.drug.id), str(seeded.user.id))  # org_id="test-org"
    create_resp = await api_client.post(
        "/api/protocols/", json=payload, headers={"X-User-Id": str(seeded.user.id)},
    )
    protocol_id = create_resp.json()["id"]

    sign_resp = await api_client.post(
        f"/api/protocols/{protocol_id}/sign",
        json={
            "signatory_name": "Dr. OtherOrg",
            "reason": "Should fail — wrong org",
            "reauth_password": other_org_password,
        },
        headers={"X-User-Id": str(other_org_approver.id)},
    )
    assert sign_resp.status_code == 403

    protocol = await seeded.db.get(Protocol, uuid.UUID(protocol_id))
    await seeded.db.refresh(protocol)
    assert protocol.status != ProtocolStatus.approved


# ---------------------------------------------------------------------------
# Test 8: Guideline version lock
# ---------------------------------------------------------------------------


async def test_guideline_outdated_flag(
    api_client, seeded, mock_anthropic, httpx_mock
):
    """A protocol created with a stale guideline version has guideline_outdated=True."""
    register_pubmed_mocks(httpx_mock)

    payload = {
        **_STUDY_PAYLOAD_BASE,
        "drug_id": str(seeded.drug.id),
        "created_by": str(seeded.user.id),
        "guideline_version": "FDA-BE-2003",  # intentionally old
    }
    resp = await api_client.post(
        "/api/protocols/", json=payload, headers={"X-User-Id": str(seeded.user.id)},
    )
    assert resp.status_code == 201
    assert resp.json()["guideline_outdated"] is True


async def test_guideline_current_flag_false(
    api_client, seeded, mock_anthropic, httpx_mock
):
    """A protocol created with the current guideline has guideline_outdated=False."""
    register_pubmed_mocks(httpx_mock)

    payload = _protocol_payload(str(seeded.drug.id), str(seeded.user.id))
    resp = await api_client.post(
        "/api/protocols/", json=payload, headers={"X-User-Id": str(seeded.user.id)},
    )
    assert resp.status_code == 201
    assert resp.json()["guideline_outdated"] is False


# ---------------------------------------------------------------------------
# Test 9: Fetch a non-existent protocol returns 404
# ---------------------------------------------------------------------------


async def test_get_nonexistent_protocol_404(api_client, seeded):
    """GET /api/protocols/<random-uuid> returns 404 for an authenticated actor."""
    resp = await api_client.get(
        f"/api/protocols/{uuid.uuid4()}",
        headers={"X-User-Id": str(seeded.user.id)},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Test 10: Per-organization data isolation (B3)
# ---------------------------------------------------------------------------


async def _other_org_generator(db) -> User:
    """A generator in a different org than the `seeded` fixture's (test-org)."""
    other = User(
        id=uuid.uuid4(),
        org_id="other-org",
        email="generator@othercro.com",
        role=UserRole.generator,
        hashed_password=hash_password("irrelevant-for-these-tests"),
    )
    db.add(other)
    await db.commit()
    await db.refresh(other)
    return other


async def test_create_protocol_derives_org_from_actor(
    api_client, seeded, mock_anthropic, httpx_mock
):
    """org_id is not client-suppliable — a created protocol always belongs to
    the authenticated actor's own org, never one the caller merely names."""
    register_pubmed_mocks(httpx_mock)

    payload = _protocol_payload(str(seeded.drug.id), str(seeded.user.id))
    resp = await api_client.post(
        "/api/protocols/", json=payload, headers={"X-User-Id": str(seeded.user.id)},
    )
    assert resp.status_code == 201
    assert resp.json()["org_id"] == seeded.user.org_id == "test-org"


async def test_get_protocol_cross_org_returns_403(
    api_client, seeded, mock_anthropic, httpx_mock
):
    """A generator from a different org cannot read another org's protocol by
    guessing/holding its id — org isolation, not just role, gates read access."""
    register_pubmed_mocks(httpx_mock)
    other = await _other_org_generator(seeded.db)

    payload = _protocol_payload(str(seeded.drug.id), str(seeded.user.id))
    create_resp = await api_client.post(
        "/api/protocols/", json=payload, headers={"X-User-Id": str(seeded.user.id)},
    )
    protocol_id = create_resp.json()["id"]

    cross_org_resp = await api_client.get(
        f"/api/protocols/{protocol_id}",
        headers={"X-User-Id": str(other.id)},
    )
    assert cross_org_resp.status_code == 403

    same_org_resp = await api_client.get(
        f"/api/protocols/{protocol_id}",
        headers={"X-User-Id": str(seeded.user.id)},
    )
    assert same_org_resp.status_code == 200


async def test_export_docx_cross_org_returns_403(
    api_client, seeded, mock_anthropic, httpx_mock
):
    """A different org cannot export another org's protocol to .docx."""
    register_pubmed_mocks(httpx_mock)
    other = await _other_org_generator(seeded.db)

    payload = _protocol_payload(str(seeded.drug.id), str(seeded.user.id))
    create_resp = await api_client.post(
        "/api/protocols/", json=payload, headers={"X-User-Id": str(seeded.user.id)},
    )
    protocol_id = create_resp.json()["id"]

    export_resp = await api_client.post(
        "/api/export/docx",
        json={"protocol_id": protocol_id},
        headers={"X-User-Id": str(other.id)},
    )
    assert export_resp.status_code == 403


async def test_audit_logs_cross_org_returns_403(
    api_client, seeded, mock_anthropic, httpx_mock
):
    """Audit-log diffs can contain section-content previews (see export.py) —
    a different org must not be able to read another org's audit trail either."""
    register_pubmed_mocks(httpx_mock)
    other = await _other_org_generator(seeded.db)

    payload = _protocol_payload(str(seeded.drug.id), str(seeded.user.id))
    create_resp = await api_client.post(
        "/api/protocols/", json=payload, headers={"X-User-Id": str(seeded.user.id)},
    )
    protocol_id = create_resp.json()["id"]

    cross_org_resp = await api_client.get(
        f"/api/audit/{protocol_id}",
        headers={"X-User-Id": str(other.id)},
    )
    assert cross_org_resp.status_code == 403

    same_org_resp = await api_client.get(
        f"/api/audit/{protocol_id}",
        headers={"X-User-Id": str(seeded.user.id)},
    )
    assert same_org_resp.status_code == 200


async def test_section_edit_cross_org_returns_403(
    api_client, seeded, mock_anthropic, httpx_mock
):
    """A generator from a different org cannot edit another org's protocol
    section, even though both are role='generator' (org gates on top of role)."""
    register_pubmed_mocks(httpx_mock)
    other = await _other_org_generator(seeded.db)

    payload = _protocol_payload(str(seeded.drug.id), str(seeded.user.id))
    create_resp = await api_client.post(
        "/api/protocols/", json=payload, headers={"X-User-Id": str(seeded.user.id)},
    )
    protocol_id = create_resp.json()["id"]

    cross_org_resp = await api_client.put(
        f"/api/protocols/{protocol_id}/sections/synopsis",
        json={"content": "<p>Should not be writable from another org.</p>"},
        headers={"X-User-Id": str(other.id)},
    )
    assert cross_org_resp.status_code == 403

    protocol = await seeded.db.get(Protocol, uuid.UUID(protocol_id))
    await seeded.db.refresh(protocol)
    assert "Should not be writable" not in protocol.sections["synopsis"]


# ---------------------------------------------------------------------------
# Test 11: Clerk email-binding — identity comes from the verified session,
# not the client-supplied X-User-Id header (the critical auth fix)
# ---------------------------------------------------------------------------


def _fake_request(*, clerk_claims=None, x_user_id: str | None = None):
    """A minimal stand-in for starlette.Request with just what
    authenticate_actor reads: .state.clerk_claims and .headers.get(...)."""
    state = SimpleNamespace()
    if clerk_claims is not None:
        state.clerk_claims = clerk_claims
    headers = {"X-User-Id": x_user_id} if x_user_id else {}
    return SimpleNamespace(state=state, headers=headers)


async def test_actor_bound_to_verified_email_ignores_x_user_id(seeded, monkeypatch):
    """With Clerk auth ON, the acting user is resolved from the verified session's
    email — even a spoofed X-User-Id naming a DIFFERENT user is ignored."""
    import auth
    from dependencies import authenticate_actor

    monkeypatch.setattr(auth, "AUTH_ENABLED", True)

    async def _fake_email_for_sub(sub):
        return seeded.approver.email  # the verified session belongs to the approver

    monkeypatch.setattr(auth, "clerk_email_for_sub", _fake_email_for_sub)

    # Session says approver; header tries to impersonate the generator.
    req = _fake_request(
        clerk_claims={"sub": "clerk_user_abc"},
        x_user_id=str(seeded.user.id),
    )
    actor = await authenticate_actor(req, seeded.db)
    assert actor.id == seeded.approver.id  # header ignored — identity from session
    assert actor.id != seeded.user.id


async def test_actor_email_claim_used_directly_when_present(seeded, monkeypatch):
    """If the Clerk JWT already carries an email claim, it's used without a
    Backend-API lookup (case-insensitively)."""
    import auth
    from dependencies import authenticate_actor

    monkeypatch.setattr(auth, "AUTH_ENABLED", True)

    async def _should_not_be_called(sub):  # pragma: no cover
        raise AssertionError("clerk_email_for_sub should not run when email claim present")

    monkeypatch.setattr(auth, "clerk_email_for_sub", _should_not_be_called)

    req = _fake_request(clerk_claims={"sub": "x", "email": seeded.user.email.upper()})
    actor = await authenticate_actor(req, seeded.db)
    assert actor.id == seeded.user.id


async def test_actor_unprovisioned_email_returns_403(seeded, monkeypatch):
    """A valid Clerk session whose email isn't in the users table → 403
    (session is real, the person just isn't authorized in this workspace)."""
    import auth
    from dependencies import authenticate_actor

    monkeypatch.setattr(auth, "AUTH_ENABLED", True)

    async def _fake_email_for_sub(sub):
        return "stranger@nowhere.example"

    monkeypatch.setattr(auth, "clerk_email_for_sub", _fake_email_for_sub)

    req = _fake_request(clerk_claims={"sub": "clerk_user_stranger"})
    with pytest.raises(HTTPException) as exc:
        await authenticate_actor(req, seeded.db)
    assert exc.value.status_code == 403


async def test_actor_auth_disabled_falls_back_to_x_user_id(seeded, monkeypatch):
    """With Clerk auth OFF (local dev / this test suite's default), identity still
    comes from X-User-Id so local runs and tests keep working."""
    import auth
    from dependencies import authenticate_actor

    monkeypatch.setattr(auth, "AUTH_ENABLED", False)

    req = _fake_request(x_user_id=str(seeded.approver.id))
    actor = await authenticate_actor(req, seeded.db)
    assert actor.id == seeded.approver.id


# ---------------------------------------------------------------------------
# Test 12: Multi-guideline support (C1)
# ---------------------------------------------------------------------------


async def test_guidelines_endpoint_lists_supported(api_client):
    """GET /api/guidelines returns FDA + EMA + ICH M13 with the FDA default."""
    resp = await api_client.get("/api/guidelines/")
    assert resp.status_code == 200
    body = resp.json()
    assert body["default"] == "FDA-BE-2014"
    ids = {g["id"] for g in body["guidelines"]}
    assert {"FDA-BE-2014", "EMA-BE-2010", "ICH-M13A-2024"} <= ids
    # Display names must be clean ASCII (regression: em-dashes once mojibaked).
    for g in body["guidelines"]:
        assert g["display_name"].isascii(), g["display_name"]


async def test_create_protocol_rejects_unknown_guideline(
    api_client, seeded, mock_anthropic
):
    """An unsupported guideline_version is rejected with 422 before generation.

    No literature mocks are registered on purpose: validation happens before any
    fetch, so registering mocks would leave them unused (pytest-httpx would then
    fail teardown on 'all responses were requested')."""
    payload = {
        **_protocol_payload(str(seeded.drug.id), str(seeded.user.id)),
        "guideline_version": "MADE-UP-2099",
    }
    resp = await api_client.post(
        "/api/protocols/", json=payload, headers={"X-User-Id": str(seeded.user.id)},
    )
    assert resp.status_code == 422
    assert "guideline" in resp.text.lower()


async def test_list_protocols_is_org_scoped(
    api_client, seeded, mock_anthropic, httpx_mock
):
    """GET /api/protocols returns the caller's org's protocols (My Reports), with
    drug info + section counts, and never another org's."""
    register_pubmed_mocks(httpx_mock)
    other = await _other_org_generator(seeded.db)

    # test-org creates one protocol.
    payload = _protocol_payload(str(seeded.drug.id), str(seeded.user.id))
    create_resp = await api_client.post(
        "/api/protocols/", json=payload, headers={"X-User-Id": str(seeded.user.id)},
    )
    assert create_resp.status_code == 201
    created_id = create_resp.json()["id"]

    # The author sees it in their history, with drug + counts populated.
    mine = await api_client.get("/api/protocols/", headers={"X-User-Id": str(seeded.user.id)})
    assert mine.status_code == 200
    rows = mine.json()
    assert any(r["id"] == created_id for r in rows)
    row = next(r for r in rows if r["id"] == created_id)
    assert row["drug_inn"] == "metformin"
    assert row["section_count"] > 0
    assert "sections" not in row  # summary is lightweight

    # A different org sees an empty history (never test-org's protocol).
    theirs = await api_client.get("/api/protocols/", headers={"X-User-Id": str(other.id)})
    assert theirs.status_code == 200
    assert all(r["id"] != created_id for r in theirs.json())


async def test_list_protocols_requires_auth(api_client):
    """No X-User-Id / session -> 401 (history is per-user, never anonymous)."""
    resp = await api_client.get("/api/protocols/")
    assert resp.status_code == 401


async def test_create_protocol_accepts_ema_guideline(
    api_client, seeded, mock_anthropic, httpx_mock
):
    """A protocol created under EMA is stored with that guideline and is NOT
    flagged outdated (EMA is current in its own right, not just != FDA)."""
    register_pubmed_mocks(httpx_mock)
    payload = {
        **_protocol_payload(str(seeded.drug.id), str(seeded.user.id)),
        "guideline_version": "EMA-BE-2010",
    }
    resp = await api_client.post(
        "/api/protocols/", json=payload, headers={"X-User-Id": str(seeded.user.id)},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["guideline_version"] == "EMA-BE-2010"
    assert body["guideline_outdated"] is False


# ---------------------------------------------------------------------------
# Biologic-only study-type gating (user rule: "if the drug is a biologic it
# can only select the respective study type")
# ---------------------------------------------------------------------------


async def test_create_protocol_rejects_small_molecule_drug_with_biosimilar_study_type(
    api_client, seeded, mock_anthropic,
):
    """metformin (small molecule) cannot run a biosimilar PK-similarity study.

    No literature mocks registered: this is rejected before any fetch, same as
    the unsupported-guideline check above."""
    payload = {
        **_protocol_payload(str(seeded.drug.id), str(seeded.user.id)),
        "study_type": "biosimilar_pk",
    }
    resp = await api_client.post(
        "/api/protocols/", json=payload, headers={"X-User-Id": str(seeded.user.id)},
    )
    assert resp.status_code == 422
    assert "biologic" in resp.text.lower() or "small-molecule" in resp.text.lower()


async def test_create_protocol_rejects_biologic_drug_with_be_study_type(
    api_client, seeded, mock_anthropic,
):
    """A biologic (mAb, curated KB) cannot run a small-molecule BE study."""
    drug = Drug(id=uuid.uuid4(), inn="adalimumab", brand_names=["Humira"], rxnorm_id=None)
    seeded.db.add(drug)
    await seeded.db.commit()
    await seeded.db.refresh(drug)

    payload = {
        **_protocol_payload(str(drug.id), str(seeded.user.id)),
        "study_type": "BE",
    }
    resp = await api_client.post(
        "/api/protocols/", json=payload, headers={"X-User-Id": str(seeded.user.id)},
    )
    assert resp.status_code == 422
    assert "biologic" in resp.text.lower()


async def test_drug_search_reports_modality(api_client, seeded):
    """GET /api/drugs surfaces each drug's classified modality so the frontend
    can gate the study-type selector."""
    resp = await api_client.get("/api/drugs/", params={"q": "metformin"})
    assert resp.status_code == 200
    body = resp.json()
    assert body and body[0]["modality"] == "small_molecule"


# ---------------------------------------------------------------------------
# Requirement 10 - inclusion/exclusion criteria grounded in real prior trials
# ---------------------------------------------------------------------------
#
# services/trial_precedent.py needs the indication services/patient_
# population.py selects, so it is called from routers/protocols.py's
# _augment_config_with_design() one step after that engine — these tests
# exercise that real wiring directly (no HTTP/DB needed: the function is pure
# Python over already-fetched inputs), proving the cross-engine consistency
# a full end-to-end HTTP test could only assert indirectly.


async def test_trial_precedent_fetch_is_gated_before_any_http_call(seeded):
    """CONFIRMED requirement: fetching ClinicalTrials.gov data must NOT fire
    for the vast majority of generations (BE studies, healthy-volunteer
    biosimilar_pk studies) — only for a patient biosimilar_pk study, where
    services/patient_population.py's own applicability gate also fires. A
    stub LiteratureService whose fetch_clinicaltrials() raises if called
    proves the gate short-circuits BEFORE any HTTP attempt, for both
    dimensions of the gate independently."""
    from routers.protocols import _fetch_trial_precedent_safe

    class _ExplodingLitSvc:
        async def fetch_clinicaltrials(self, drug_inn):
            raise AssertionError("fetch_clinicaltrials must not be called here")

    lit_svc = _ExplodingLitSvc()

    # BE study, even with subject_type="patient" (nonsensical combination,
    # but the study_type half of the gate must still hold on its own).
    assert await _fetch_trial_precedent_safe(
        lit_svc, "adalimumab", study_type="BE", subject_type="patient"
    ) == []
    # Healthy-volunteer biosimilar_pk study — the subject_type half of the
    # gate must hold on its own too.
    assert await _fetch_trial_precedent_safe(
        lit_svc, "adalimumab", study_type="biosimilar_pk", subject_type="healthy"
    ) == []


async def test_trial_precedent_fetch_fires_only_for_patient_biosimilar(seeded, httpx_mock):
    """The one combination that DOES need real ClinicalTrials.gov data."""
    from routers.protocols import _fetch_trial_precedent_safe

    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"^https://clinicaltrials\.gov/api/v2/studies(\?.*)?$"),
        json={"studies": []},
    )
    from services.literature_service import LiteratureService
    lit_svc = LiteratureService(seeded.db)

    result = await _fetch_trial_precedent_safe(
        lit_svc, seeded.drug.inn, study_type="biosimilar_pk", subject_type="patient"
    )
    assert result == []  # empty mock response, but the call genuinely happened
    assert len(httpx_mock.get_requests()) == 1


def test_augment_config_wires_trial_precedent_to_the_selected_indication():
    """End-to-end (pure Python, no I/O): the trial_precedent engine must
    filter against whichever indication _patient_population ACTUALLY
    selected for this drug, not an assumed one — proving the two engines
    agree without hardcoding what patient_population.py would pick."""
    from routers.protocols import _augment_config_with_design

    trials = [
        {"nct_id": "NCT_PSO", "title": "Psoriasis study", "status": "COMPLETED", "phase": "PHASE3",
         "conditions": ["Plaque Psoriasis"], "interventions": ["Adalimumab"],
         "sex": "ALL", "minimum_age": "18 Years", "maximum_age": "", "healthy_volunteers": False,
         "eligibility_criteria": (
             "Inclusion Criteria:\n\n1. Moderate to severe plaque psoriasis\n\n"
             "Exclusion Criteria:\n\n1. Guttate or pustular psoriasis"
         )},
    ]

    cfg = _augment_config_with_design(
        population_config={"subject_type": "patient", "dosing": "multiple"},
        study_type="biosimilar_pk",
        drug_inn="adalimumab",
        extracted=[],
        pk_params=[],
        label=None,
        trial_records=trials,
    )

    pp = cfg["_patient_population"]
    tp = cfg["_trial_precedent"]
    assert tp["applicable"] is True
    # Whatever indication patient_population actually recommended (curated
    # for adalimumab without a real label, this is plaque psoriasis — see
    # services/patient_population.py's _INDICATION_KB), trial_precedent must
    # have filtered against exactly that indication, not a different one.
    assert tp["indication"] == pp["recommended"]["indication"]
    assert tp["precedent_found"] is True
    assert tp["precedent_trials"][0]["nct_id"] == "NCT_PSO"


def test_augment_config_finds_no_precedent_for_an_irrelevant_trial():
    """Non-regression: real trial DATA being present is not enough — it must
    also match the SELECTED indication, or precedent_found stays False."""
    from routers.protocols import _augment_config_with_design

    trials = [
        {"nct_id": "NCT_RA", "title": "RA study", "status": "COMPLETED", "phase": "PHASE3",
         "conditions": ["Rheumatoid Arthritis"], "interventions": ["Adalimumab"],
         "sex": "ALL", "minimum_age": "18 Years", "maximum_age": "", "healthy_volunteers": False,
         "eligibility_criteria": "Inclusion Criteria:\n\n1. RA\n\nExclusion Criteria:\n\n1. Infection"},
    ]

    cfg = _augment_config_with_design(
        population_config={"subject_type": "patient", "dosing": "multiple"},
        study_type="biosimilar_pk",
        drug_inn="adalimumab",
        extracted=[],
        pk_params=[],
        label=None,
        trial_records=trials,
    )

    pp = cfg["_patient_population"]
    tp = cfg["_trial_precedent"]
    assert pp["recommended"]["indication"] != "Rheumatoid Arthritis"
    assert tp["applicable"] is True
    assert tp["precedent_found"] is False


def test_augment_config_trial_precedent_absent_for_a_be_study():
    """Non-regression: a conventional small-molecule study gets nothing from
    either engine, and _augment_config_with_design must not crash when
    trial_records is omitted entirely (the router only passes it for
    biosimilar_pk/patient generations)."""
    from routers.protocols import _augment_config_with_design

    cfg = _augment_config_with_design(
        population_config={},
        study_type="BE",
        drug_inn="ibuprofen",
        extracted=[],
        pk_params=[],
        label=None,
    )
    assert cfg["_trial_precedent"]["applicable"] is False
    assert cfg["_patient_population"]["applicable"] is False


# ---------------------------------------------------------------------------
# Additional PK/immunogenicity timepoint inputs — routers/protocols.py wiring
# ---------------------------------------------------------------------------


def test_parse_number_list_extracts_numbers_permissively():
    from routers.protocols import _parse_number_list

    assert _parse_number_list("36, 48, 72") == [36.0, 48.0, 72.0]
    assert _parse_number_list("36h, 48 hrs, 72 hours") == [36.0, 48.0, 72.0]
    assert _parse_number_list("garbage") == []
    assert _parse_number_list("") == []
    assert _parse_number_list(None) == []
    assert _parse_number_list(-1.5) == []  # non-string input is not a crash
    assert _parse_number_list("9.5, -1, 3") == [9.5, -1.0, 3.0]


def test_augment_config_merges_additional_pk_and_immunogenicity_timepoints():
    """End-to-end: both free-text fields flow from population_config through
    _parse_number_list() into the two engines, land in BOTH PK profiles of a
    dual-profile study, and out-of-range values are disclosed as rejected
    rather than silently dropped or silently extending the horizon/period."""
    from routers.protocols import _augment_config_with_design

    cfg = _augment_config_with_design(
        population_config={
            "subject_type": "patient",
            "dosing": "multiple",
            "dosing_interval_hours": 336.0,
            # 108h is confirmed NOT already present in this fixture's
            # auto-computed schedule (unlike 48h, which coincides with an
            # auto absorption-fraction point and would be correctly reported
            # as "already covered" rather than "added" — see
            # test_sampling_schedule.py's provenance-honesty tests).
            "additional_pk_timepoints_hours": "108h, 500000",
            "additional_immunogenicity_timepoints_months": "9, 50",
        },
        study_type="biosimilar_pk",
        drug_inn="adalimumab",
        extracted=[],
        pk_params=[],
        label=None,
        trial_records=[],
    )
    sched = cfg["_sampling_schedule"]
    comp = cfg["_comparative_safety"]

    assert any(tp.get("user_added") and tp["hours"] == pytest.approx(108.0, abs=0.05)
               for tp in sched["timepoints_first_dose"])
    assert any(tp.get("user_added") for tp in sched["timepoints_steady_state"])
    assert any(a["key"] == "additional_pk_timepoints_rejected" for a in sched["assumptions"])

    assert 9.0 in comp["immunogenicity_timepoints_months"]
    assert 50.0 not in comp["immunogenicity_timepoints_months"]


def test_augment_config_additional_timepoint_fields_are_optional():
    """Non-regression: omitting both fields entirely must not change
    behaviour or crash — every existing biosimilar_pk test in this suite
    relies on this."""
    from routers.protocols import _augment_config_with_design

    with_fields = _augment_config_with_design(
        population_config={
            "subject_type": "patient", "dosing": "multiple", "dosing_interval_hours": 336.0,
        },
        study_type="biosimilar_pk", drug_inn="adalimumab",
        extracted=[], pk_params=[], label=None, trial_records=[],
    )
    without_fields = _augment_config_with_design(
        population_config={
            "subject_type": "patient", "dosing": "multiple", "dosing_interval_hours": 336.0,
            "additional_pk_timepoints_hours": "",
            "additional_immunogenicity_timepoints_months": "",
        },
        study_type="biosimilar_pk", drug_inn="adalimumab",
        extracted=[], pk_params=[], label=None, trial_records=[],
    )
    assert with_fields["_sampling_schedule"]["timepoints"] == without_fields["_sampling_schedule"]["timepoints"]
    assert (with_fields["_comparative_safety"]["immunogenicity_timepoints_months"]
            == without_fields["_comparative_safety"]["immunogenicity_timepoints_months"])


# ---------------------------------------------------------------------------
# Blinding wiring fix — biosimilar patient studies get blinded dosing
# ---------------------------------------------------------------------------
#
# CONFIRMED BUG: generate_allocation() was never given is_biologic/study_type/
# route, so assess_blinding() (services/randomization.py) always fell through
# to its open-label default regardless of study type — even though its own
# documented decision logic already says a biologic/biosimilar_pk comparison
# should get blinded dosing. User-scoped fix: biosimilar patient studies only
# (conventional small-molecule BE keeps the standard, regulator-accepted
# open-label-dosing-with-blinded-bioanalysis convention).


def test_biosimilar_patient_study_gets_blinded_dosing_not_open_label():
    from routers.protocols import _augment_config_with_design

    # pd_endpoint forced to "None" so pd_coprimary=False — isolates the
    # is_biologic/study_type wiring from the SEPARATE (already-correct)
    # pd_coprimary path, which alone is already sufficient to force
    # double-blind and would mask a regression in this specific fix.
    cfg = _augment_config_with_design(
        population_config={
            "subject_type": "patient", "dosing": "multiple", "dosing_interval_hours": 336.0,
        },
        study_type="biosimilar_pk",
        drug_inn="adalimumab",
        extracted=[], pk_params=[], label=None, trial_records=[],
        design_decisions=[{"key": "pd_endpoint", "value": "None", "label": "PD co-primary"}],
    )
    masking = cfg["_randomization"]["blinding"]["treatment_masking"]
    assert masking["inputs_considered"]["biologic"] is True
    assert masking["inputs_considered"]["pd_coprimary"] is False
    assert masking["recommended"] != "open_label_dosing_with_blinded_bioanalysis"
    assert masking["dosing_masked"] is True


def test_conventional_be_study_keeps_the_open_label_convention():
    """Non-regression: the user's explicit scope decision — this fix must
    NOT change blinding for conventional small-molecule BE studies, which
    have no established regulatory rationale for masking a purely
    bioanalytically-measured PK endpoint."""
    from routers.protocols import _augment_config_with_design

    cfg = _augment_config_with_design(
        population_config={"subject_type": "healthy"},
        study_type="BE",
        drug_inn="ibuprofen",
        extracted=[], pk_params=[], label=None,
    )
    masking = cfg["_randomization"]["blinding"]["treatment_masking"]
    assert masking["recommended"] == "open_label_dosing_with_blinded_bioanalysis"
    assert masking["inputs_considered"]["biologic"] is False


def test_allocation_concealment_is_asserted_regardless_of_masking_posture():
    """Non-regression: allocation concealment (a SEPARATE, always-mandatory
    guarantee from treatment masking) must not be affected by this fix
    either way."""
    from routers.protocols import _augment_config_with_design

    for study_type, drug_inn, pop_extra in (
        ("biosimilar_pk", "adalimumab", {"subject_type": "patient", "dosing": "multiple",
                                          "dosing_interval_hours": 336.0}),
        ("BE", "ibuprofen", {"subject_type": "healthy"}),
    ):
        cfg = _augment_config_with_design(
            population_config=pop_extra,
            study_type=study_type,
            drug_inn=drug_inn,
            extracted=[], pk_params=[], label=None,
            trial_records=[] if study_type == "biosimilar_pk" else None,
        )
        assert cfg["_randomization"]["blinding"]["allocation_concealment"]["required"] is True
