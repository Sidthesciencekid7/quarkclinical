"""Engine facades (B2): the callables the chat layer shares with the routes.

The redesign extracts the bodies of the existing routes into plain callables so
the new chat tools/actions run EXACTLY the engines and gates the wizard routes
do. These tests pin the contract that makes that safe:

  * the facade's output is identical to what the route returns, for
    design-preview, indication-preview and the budget profile-preview;
  * POST /api/protocols/ still creates a protocol through the thin wrapper
    (synchronous path under pytest) and adds NO provenance of its own;
  * ``start_protocol_generation(..., provenance=...)`` lands the provenance in
    the INITIAL version snapshot's change_detail, on the synchronous path AND on
    the background (production) path, without overwriting the pipeline's keys;
  * ``create_budget_for_actor`` is what the budget route runs.

No network and no AI credits: PubMed/DailyMed via pytest-httpx, Anthropic via
the ``mock_anthropic`` fixture, the DailyMed label for the preview facades via a
stub on ``_fetch_label_safe`` (the same seam test_patient_trial_integration.py
uses).
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import select

import database
from models import Drug, Protocol, ProtocolVersion
from routers import protocols as router
from routers.budgets import compute_budget_profile_preview, create_budget_for_actor
from routers.protocols import (
    compute_design_preview,
    compute_indication_preview,
    diff_sections,
    start_protocol_generation,
)
from schemas import BudgetCreate, DesignPreviewRequest, IndicationPreviewRequest, ProtocolCreate
from tests.conftest import register_pubmed_mocks

pytestmark = pytest.mark.usefixtures("no_real_ctgov_calls_for_budgets")

NIVO_LABEL = (
    "OPDIVO is indicated for the adjuvant treatment of patients with completely "
    "resected melanoma with lymph node involvement or metastatic disease. "
    "OPDIVO is indicated for the treatment of patients with metastatic "
    "non-small cell lung cancer expressing PD-L1."
)


def _jsonable(value):
    """What the HTTP layer would send: a JSON round trip of the facade's return."""
    return json.loads(json.dumps(value, default=str))


def _as(user) -> dict[str, str]:
    return {"X-User-Id": str(user.id)}


@pytest.fixture()
def stub_label(monkeypatch):
    async def _fake(_lit_svc, _inn):
        return {
            "title": "stub",
            "indications_usage": NIVO_LABEL,
            "indications_usage_full": NIVO_LABEL,
            "dosage_and_administration": "Administer by intravenous infusion.",
        }

    monkeypatch.setattr(router, "_fetch_label_safe", _fake)


async def _add_drug(db, inn: str, brands=None) -> Drug:
    drug = Drug(id=uuid.uuid4(), inn=inn, brand_names=brands or [], rxnorm_id=None)
    db.add(drug)
    await db.commit()
    await db.refresh(drug)
    return drug


# ---------------------------------------------------------------------------
# design-preview / indication-preview: facade == route
# ---------------------------------------------------------------------------


async def test_design_preview_facade_matches_route(api_client, seeded, stub_label):
    body = {
        "drug_id": str(seeded.drug.id),
        "study_type": "BE",
        "population_config": {"dosing": "single", "subject_type": "healthy"},
    }
    resp = await api_client.post("/api/protocols/design-preview", json=body)
    assert resp.status_code == 200, resp.text

    facade = await compute_design_preview(seeded.db, DesignPreviewRequest(**body))

    assert _jsonable(facade) == resp.json()
    assert set(facade) == {"drug_inn", "study_type", "decisions"}
    assert facade["drug_inn"] == "metformin"


async def test_design_preview_facade_with_population_choice_matches_route(
    api_client, seeded, stub_label
):
    nivo = await _add_drug(seeded.db, "nivolumab", ["Opdivo"])
    body = {
        "drug_id": str(nivo.id),
        "study_type": "biosimilar_pk",
        "population_config": {},
        "population_choice": "Patients · multi-dose",
    }
    resp = await api_client.post("/api/protocols/design-preview", json=body)
    assert resp.status_code == 200, resp.text
    facade = await compute_design_preview(seeded.db, DesignPreviewRequest(**body))
    assert _jsonable(facade) == resp.json()


async def test_design_preview_facade_raises_404_for_unknown_drug(seeded, stub_label):
    from fastapi import HTTPException

    req = DesignPreviewRequest(drug_id=uuid.uuid4(), study_type="BE")
    with pytest.raises(HTTPException) as exc:
        await compute_design_preview(seeded.db, req)
    assert exc.value.status_code == 404
    assert exc.value.detail == "Drug not found."


async def test_indication_preview_facade_matches_route(api_client, seeded, stub_label):
    nivo = await _add_drug(seeded.db, "nivolumab", ["Opdivo"])
    body = {
        "drug_id": str(nivo.id),
        "study_type": "biosimilar_pk",
        "user_indication": "metastatic melanoma",
    }
    resp = await api_client.post("/api/protocols/indication-preview", json=body)
    assert resp.status_code == 200, resp.text

    facade = await compute_indication_preview(seeded.db, IndicationPreviewRequest(**body))

    assert _jsonable(facade) == resp.json()
    assert facade["applicable"] is True
    assert facade["recommended"]["user_specified"] is True

    # Without a user indication too.
    plain_body = {"drug_id": str(nivo.id), "study_type": "biosimilar_pk"}
    plain_resp = await api_client.post("/api/protocols/indication-preview", json=plain_body)
    plain_facade = await compute_indication_preview(
        seeded.db, IndicationPreviewRequest(**plain_body)
    )
    assert _jsonable(plain_facade) == plain_resp.json()


async def test_preview_label_fetch_uses_its_own_session(seeded, monkeypatch):
    """The facades must not hand the CALLER'S session to LiteratureService (whose
    cache-miss path commits it). Record which session the real
    ``_fetch_label_safe`` -> LiteratureService got and prove it was a private one."""
    from services.literature_service import LiteratureService

    seen: list[object] = []

    async def _fake_dailymed(self, drug_inn):
        seen.append(self._session)
        return {"title": "t", "sections": {"indications_usage": NIVO_LABEL}}

    monkeypatch.setattr(LiteratureService, "fetch_dailymed", _fake_dailymed)

    nivo = await _add_drug(seeded.db, "nivolumab", ["Opdivo"])
    await compute_design_preview(
        seeded.db, DesignPreviewRequest(drug_id=nivo.id, study_type="biosimilar_pk")
    )
    await compute_indication_preview(
        seeded.db, IndicationPreviewRequest(drug_id=nivo.id, study_type="biosimilar_pk")
    )

    assert len(seen) == 2
    assert all(s is not seeded.db for s in seen)


# ---------------------------------------------------------------------------
# budget profile-preview: facade == route
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("drug_inn", ["nivolumab", "metformin", "SomeUnknownDrug"])
async def test_budget_profile_preview_facade_matches_route(api_client, seeded, drug_inn):
    resp = await api_client.get(
        "/api/budgets/profile-preview", params={"drug_inn": drug_inn}, headers=_as(seeded.user),
    )
    assert resp.status_code == 200, resp.text

    facade = compute_budget_profile_preview(seeded.user, drug_inn)

    assert facade.model_dump(mode="json") == resp.json()
    assert facade.drug_inn == drug_inn


async def test_budget_profile_preview_facade_strips_the_drug_name(seeded):
    assert compute_budget_profile_preview(seeded.user, "  nivolumab  ").drug_inn == "nivolumab"


# ---------------------------------------------------------------------------
# budget create: the route runs create_budget_for_actor
# ---------------------------------------------------------------------------


async def test_create_budget_for_actor_matches_route(api_client, seeded):
    payload = {"drug_inn": "metformin", "study_type": "BE"}
    resp = await api_client.post("/api/budgets", json=payload, headers=_as(seeded.user))
    assert resp.status_code == 201, resp.text
    via_route = resp.json()

    via_facade = await create_budget_for_actor(
        seeded.db, None, seeded.user, BudgetCreate(**payload)
    )

    assert via_facade.id != uuid.UUID(via_route["id"])
    assert via_facade.org_id == via_route["org_id"] == seeded.user.org_id
    assert via_facade.name == via_route["name"] == "Metformin study budget"
    assert via_facade.study_type == via_route["study_type"] == "BE"
    assert via_facade.rollup.grand_total == via_route["rollup"]["grand_total"]
    assert len(via_facade.categories) == len(via_route["categories"])


# ---------------------------------------------------------------------------
# create_protocol wrapper + provenance
# ---------------------------------------------------------------------------

_BASE_POPULATION = {"dose_strength": "850 mg", "formulation": "tablet", "org_name": "TestCRO Inc."}


async def _initial_snapshot(db, protocol_id) -> ProtocolVersion:
    result = await db.execute(
        select(ProtocolVersion).where(
            ProtocolVersion.protocol_id == protocol_id, ProtocolVersion.version == 1
        )
    )
    snap = result.scalar_one()
    assert snap.change_type == "INITIAL"
    return snap


async def test_create_protocol_route_still_works_through_the_wrapper(
    api_client, seeded, mock_anthropic, httpx_mock
):
    register_pubmed_mocks(httpx_mock)
    resp = await api_client.post(
        "/api/protocols/",
        json={
            "drug_id": str(seeded.drug.id),
            "study_type": "BE",
            "population_config": dict(_BASE_POPULATION),
            "guideline_version": "FDA-BE-2014",
        },
        headers=_as(seeded.user),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["generation_status"] == "complete"
    assert body["version"] == 1
    assert body["status"] == "draft"
    assert all(body["sections"].get(name) for name in router.SECTION_ORDER)

    snap = await _initial_snapshot(seeded.db, uuid.UUID(body["id"]))
    # The route passes no provenance, so the snapshot carries only the
    # pipeline's own keys.
    assert "generated_sections" in snap.change_detail
    assert "source" not in snap.change_detail
    assert "chat_session_id" not in snap.change_detail


async def test_start_protocol_generation_merges_provenance_into_initial_snapshot(
    seeded, mock_anthropic, httpx_mock
):
    register_pubmed_mocks(httpx_mock)
    session_id = uuid.uuid4()
    provenance = {
        "source": "chat",
        "chat_session_id": session_id,  # a UUID: must be coerced to a string
        "spec_version": 7,
        "field_provenance": {"study_type": {"status": "confirmed", "source": "user_click"}},
        # A clashing key must NOT overwrite what the pipeline recorded.
        "generated_sections": ["forged"],
    }
    payload = ProtocolCreate(
        drug_id=seeded.drug.id,
        study_type="BE",
        population_config=dict(_BASE_POPULATION),
        guideline_version="FDA-BE-2014",
    )

    protocol = await start_protocol_generation(
        seeded.db, None, seeded.user, payload, provenance=provenance
    )

    assert protocol.generation_status == "complete"
    assert protocol.created_by == seeded.user.id
    snap = await _initial_snapshot(seeded.db, protocol.id)
    detail = snap.change_detail
    assert detail["source"] == "chat"
    assert detail["chat_session_id"] == str(session_id)
    assert detail["spec_version"] == 7
    assert detail["field_provenance"]["study_type"]["source"] == "user_click"
    # The pipeline's own record wins on a clash, and its other keys are intact.
    assert detail["generated_sections"] != ["forged"]
    assert set(detail["generated_sections"]) == set(protocol.sections)
    assert "ai_pipeline_log" in detail


async def test_start_protocol_generation_without_provenance_adds_no_keys(
    seeded, mock_anthropic, httpx_mock
):
    register_pubmed_mocks(httpx_mock)
    payload = ProtocolCreate(
        drug_id=seeded.drug.id, study_type="BE", population_config=dict(_BASE_POPULATION),
    )
    protocol = await start_protocol_generation(seeded.db, None, seeded.user, payload)
    snap = await _initial_snapshot(seeded.db, protocol.id)
    assert "source" not in snap.change_detail


async def test_background_generation_path_threads_provenance_to_initial_snapshot(
    seeded, mock_anthropic, httpx_mock, monkeypatch
):
    """Production path: a 'generating' row is returned at once and the spawned
    task fills it in. Capture the spawn, run the task body, and check the INITIAL
    snapshot carries the provenance."""
    register_pubmed_mocks(httpx_mock)
    monkeypatch.setattr(router, "_generate_synchronously", lambda: False)
    spawned: list[dict] = []
    monkeypatch.setattr(router, "_spawn_generation", lambda **kw: spawned.append(kw))
    # _generation_task opens its own session through the module-level name; point
    # it at the (already patched) test session factory.
    monkeypatch.setattr(router, "AsyncSessionLocal", database.AsyncSessionLocal)

    provenance = {"source": "chat", "chat_session_id": "abc", "spec_version": 3}
    payload = ProtocolCreate(
        drug_id=seeded.drug.id, study_type="BE", population_config=dict(_BASE_POPULATION),
    )
    protocol = await start_protocol_generation(
        seeded.db, None, seeded.user, payload, provenance=provenance
    )

    protocol_id = protocol.id
    assert protocol.generation_status == "generating"
    assert protocol.sections == {}
    assert len(spawned) == 1
    assert spawned[0]["provenance"] == provenance
    assert spawned[0]["protocol_id"] == protocol_id

    await router._generation_task(**spawned[0])

    seeded.db.expire_all()
    done = await seeded.db.get(Protocol, protocol_id)
    assert done.generation_status == "complete", done.generation_error
    snap = await _initial_snapshot(seeded.db, protocol_id)
    assert snap.change_detail["source"] == "chat"
    assert snap.change_detail["chat_session_id"] == "abc"
    assert snap.change_detail["spec_version"] == 3
    assert "generated_sections" in snap.change_detail


# ---------------------------------------------------------------------------
# validate_protocol_request keeps the route's gates (messages unchanged)
# ---------------------------------------------------------------------------


async def test_validate_protocol_request_gate_order_and_messages(seeded):
    from fastapi import HTTPException

    from routers.protocols import validate_protocol_request

    # reviewer-like actor: role gate comes first
    with pytest.raises(HTTPException) as exc:
        await validate_protocol_request(
            seeded.db,
            seeded.approver,
            ProtocolCreate(drug_id=seeded.drug.id, study_type="BE"),
        )
    assert exc.value.status_code == 403
    assert "create protocols" in exc.value.detail

    with pytest.raises(HTTPException) as exc:
        await validate_protocol_request(
            seeded.db,
            seeded.user,
            ProtocolCreate(drug_id=seeded.drug.id, study_type="BE", guideline_version="NOPE-1"),
        )
    assert exc.value.status_code == 422
    assert exc.value.detail.startswith("Unsupported guideline 'NOPE-1'. Supported: ")

    with pytest.raises(HTTPException) as exc:
        await validate_protocol_request(
            seeded.db, seeded.user, ProtocolCreate(drug_id=uuid.uuid4(), study_type="BE"),
        )
    assert (exc.value.status_code, exc.value.detail) == (404, "Drug not found.")

    adalimumab = await _add_drug(seeded.db, "adalimumab")
    with pytest.raises(HTTPException) as exc:
        await validate_protocol_request(
            seeded.db, seeded.user, ProtocolCreate(drug_id=adalimumab.id, study_type="BE"),
        )
    assert exc.value.status_code == 422
    assert exc.value.detail == (
        "'adalimumab' is a biologic and cannot run a 'BE' study; "
        "allowed study type(s): biosimilar_pk."
    )

    with pytest.raises(HTTPException) as exc:
        await validate_protocol_request(
            seeded.db, seeded.user, ProtocolCreate(drug_id=seeded.drug.id, study_type="biosimilar_pk"),
        )
    assert exc.value.status_code == 422
    assert exc.value.detail == (
        "'metformin' is a small-molecule product and cannot run a "
        "'biosimilar_pk' study; allowed study type(s): BE, food_effect."
    )

    ok = await validate_protocol_request(
        seeded.db, seeded.user, ProtocolCreate(drug_id=seeded.drug.id, study_type="food_effect"),
    )
    assert ok.id == seeded.drug.id


# ---------------------------------------------------------------------------
# diff_sections is public; the old private name is kept as an alias
# ---------------------------------------------------------------------------


def test_diff_sections_is_public_with_private_alias():
    assert router._diff_sections is router.diff_sections is diff_sections

    result = diff_sections(
        {"objectives": "<p>Old text.</p>"},
        {"objectives": "<p>New text.</p>", "synopsis": "<p>Added.</p>"},
        1,
        2,
    )
    by_name = {d.section_name: d for d in result}
    assert by_name["objectives"].status == "modified"
    assert by_name["synopsis"].status == "added"
    assert by_name["introduction"].status == "unchanged"
    assert [d.section_name for d in result] == router.SECTION_ORDER


# ---------------------------------------------------------------------------
# ProtocolRead carries the drug name on GET /{id}
# ---------------------------------------------------------------------------


async def test_get_protocol_includes_drug_inn_and_brand_names(
    api_client, seeded, mock_anthropic, httpx_mock
):
    register_pubmed_mocks(httpx_mock)
    created = await api_client.post(
        "/api/protocols/",
        json={
            "drug_id": str(seeded.drug.id),
            "study_type": "BE",
            "population_config": dict(_BASE_POPULATION),
        },
        headers=_as(seeded.user),
    )
    assert created.status_code == 201, created.text
    protocol_id = created.json()["id"]

    # The create response does not load the drug and leaves the fields empty.
    assert created.json()["drug_inn"] is None
    assert created.json()["drug_brand_names"] == []

    got = await api_client.get(f"/api/protocols/{protocol_id}", headers=_as(seeded.user))
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["drug_inn"] == "metformin"
    assert body["drug_brand_names"] == ["Glucophage", "Fortamet", "Glumetza"]
    # everything the route returned before is still there
    for key in ("id", "drug_id", "org_id", "study_type", "population_config", "sections",
                "version", "status", "guideline_version", "guideline_outdated",
                "generation_status", "generation_error", "created_by", "created_at"):
        assert key in body
