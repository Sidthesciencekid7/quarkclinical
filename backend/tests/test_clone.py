"""Clone / start-from-template tests — POST /api/protocols/{id}/clone.

Covers the two things that make the feature correct:

  1. It really is a *copy*: the content fields (sections, study_type,
     guideline_version, drug, population_config) come across verbatim while the
     identity/lifecycle fields (id, version, status) are reset to a fresh draft.
     No AI is involved, so unlike the create_protocol tests these need neither
     `mock_anthropic` nor `register_pubmed_mocks` — the source protocol is
     seeded straight into the DB with known section text, which also makes the
     "copied verbatim" assertion exact rather than approximate.

  2. It is org-isolated: cloning is a read of the source, so a source belonging
     to another org must 403 (never silently produce a copy of someone else's
     study design).

Note on the license gate: clone_protocol calls require_active_license, which
deliberately fails open for orgs that have no Organization row — the `seeded`
fixture's "test-org" is exactly that case (see
test_orgs.py::test_require_active_license_fails_open_for_orgs_without_organization_row),
so these tests exercise clone semantics, not licensing.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from models import Protocol, ProtocolStatus, ProtocolVersion, StudyType, User, UserRole
from security import hash_password

# Distinctive content so "copied verbatim" is unambiguous in the assertions.
SOURCE_SECTIONS = {
    "title_page": "Protocol PKBE-CLONE-001 — a single-dose crossover study.",
    "objectives": "Primary objective: demonstrate bioequivalence of Test vs Reference.",
    "statistical_methods": "ANOVA on ln-transformed Cmax and AUC; 90% CI within 80.00-125.00%.",
}

SOURCE_POPULATION = {
    "dose_strength": "850 mg",
    "formulation": "tablet",
    "org_name": "TestCRO Inc.",
}


async def _seed_source_protocol(db, *, drug, user, status=ProtocolStatus.approved, version=4):
    """A finished, already-generated protocol to clone from.

    Seeded at version 4 / status=approved on purpose: it proves the clone
    RESETS those rather than inheriting them.
    """
    protocol = Protocol(
        id=uuid.uuid4(),
        org_id=user.org_id,
        drug_id=drug.id,
        study_type=StudyType.be,
        population_config=dict(SOURCE_POPULATION),
        sections=dict(SOURCE_SECTIONS),
        version=version,
        status=status,
        guideline_version="EMA-BE-2010",
        created_by=user.id,
    )
    db.add(protocol)
    await db.commit()
    await db.refresh(protocol)
    return protocol


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


# ---------------------------------------------------------------------------
# Copy semantics
# ---------------------------------------------------------------------------


async def test_clone_returns_new_draft_copying_content(api_client, seeded):
    """The clone is a NEW protocol: fresh id, version 1, status draft — but its
    content fields are byte-identical to the source's."""
    source = await _seed_source_protocol(seeded.db, drug=seeded.drug, user=seeded.user)

    resp = await api_client.post(
        f"/api/protocols/{source.id}/clone",
        headers={"X-User-Id": str(seeded.user.id)},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()

    # --- Reset (identity / lifecycle) ---
    assert body["id"] != str(source.id)
    assert body["version"] == 1                    # not the source's 4
    assert body["status"] == "draft"               # not the source's "approved"

    # --- Copied (content) ---
    assert body["sections"] == SOURCE_SECTIONS
    assert body["study_type"] == "BE"
    assert body["guideline_version"] == "EMA-BE-2010"
    assert body["drug_id"] == str(seeded.drug.id)
    assert body["population_config"] == SOURCE_POPULATION

    # --- Ownership: the clone belongs to the actor, in the actor's org ---
    assert body["org_id"] == seeded.user.org_id
    assert body["created_by"] == str(seeded.user.id)


async def test_clone_persists_a_second_protocol_row(api_client, seeded):
    """The source survives untouched and the clone is a genuinely separate row
    (a copy, not a move or an in-place mutation)."""
    source = await _seed_source_protocol(seeded.db, drug=seeded.drug, user=seeded.user)

    resp = await api_client.post(
        f"/api/protocols/{source.id}/clone",
        headers={"X-User-Id": str(seeded.user.id)},
    )
    assert resp.status_code == 201
    clone_id = uuid.UUID(resp.json()["id"])

    rows = (await seeded.db.execute(select(Protocol))).scalars().all()
    assert len(rows) == 2
    assert {r.id for r in rows} == {source.id, clone_id}

    # Source is unchanged — still v4/approved
    await seeded.db.refresh(source)
    assert source.version == 4
    assert source.status is ProtocolStatus.approved


async def test_clone_writes_version_1_snapshot_with_provenance(api_client, seeded):
    """Like create_protocol, the clone gets an INITIAL v1 snapshot — tagged with
    the id it was cloned from, so the Part 11 trail records the provenance."""
    source = await _seed_source_protocol(seeded.db, drug=seeded.drug, user=seeded.user)

    resp = await api_client.post(
        f"/api/protocols/{source.id}/clone",
        headers={"X-User-Id": str(seeded.user.id)},
    )
    clone_id = uuid.UUID(resp.json()["id"])

    snapshots = (
        (await seeded.db.execute(
            select(ProtocolVersion).where(ProtocolVersion.protocol_id == clone_id)
        ))
        .scalars()
        .all()
    )
    assert len(snapshots) == 1
    snap = snapshots[0]
    assert snap.version == 1
    assert snap.change_type == "INITIAL"
    assert snap.sections == SOURCE_SECTIONS
    assert snap.change_detail["cloned_from_protocol_id"] == str(source.id)


async def test_clone_of_clone_is_independent(api_client, seeded):
    """Editing is per-row: the clone's JSONB is its own object, so a clone can be
    re-cloned and the chain doesn't share mutable state."""
    source = await _seed_source_protocol(seeded.db, drug=seeded.drug, user=seeded.user)

    first = await api_client.post(
        f"/api/protocols/{source.id}/clone",
        headers={"X-User-Id": str(seeded.user.id)},
    )
    first_id = first.json()["id"]

    second = await api_client.post(
        f"/api/protocols/{first_id}/clone",
        headers={"X-User-Id": str(seeded.user.id)},
    )
    assert second.status_code == 201
    assert second.json()["id"] not in {first_id, str(source.id)}
    assert second.json()["sections"] == SOURCE_SECTIONS
    assert second.json()["version"] == 1


# ---------------------------------------------------------------------------
# Org isolation / access control
# ---------------------------------------------------------------------------


async def test_clone_cross_org_returns_403(api_client, seeded):
    """A generator from another org cannot clone a protocol they can't read —
    holding the id is not enough. And nothing is written."""
    source = await _seed_source_protocol(seeded.db, drug=seeded.drug, user=seeded.user)
    other = await _other_org_generator(seeded.db)

    resp = await api_client.post(
        f"/api/protocols/{source.id}/clone",
        headers={"X-User-Id": str(other.id)},
    )
    assert resp.status_code == 403

    # The refused clone must not have created a row
    rows = (await seeded.db.execute(select(Protocol))).scalars().all()
    assert len(rows) == 1
    assert rows[0].id == source.id


async def test_clone_unknown_protocol_returns_404(api_client, seeded):
    """A nonexistent source id is a 404, not a 500 or an empty draft."""
    resp = await api_client.post(
        f"/api/protocols/{uuid.uuid4()}/clone",
        headers={"X-User-Id": str(seeded.user.id)},
    )
    assert resp.status_code == 404


async def test_clone_requires_generator_role(api_client, seeded):
    """Separation of duties: cloning drafts a new protocol, so it is Author-only
    — an approver may read the source but not duplicate it."""
    source = await _seed_source_protocol(seeded.db, drug=seeded.drug, user=seeded.user)

    resp = await api_client.post(
        f"/api/protocols/{source.id}/clone",
        headers={"X-User-Id": str(seeded.approver.id)},
    )
    assert resp.status_code == 403
