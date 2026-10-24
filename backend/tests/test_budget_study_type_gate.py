"""Biologic / small-molecule study-type gate on POST /api/budgets (B2,
user-approved decision).

Protocol creation has always refused a study type the drug's modality cannot run
(a biologic may only run ``biosimilar_pk``; a small molecule only ``BE`` /
``food_effect``). Budgets accepted any combination, so a chat-built budget could
price a "BE study of adalimumab" that no protocol could ever be generated for.

The budget gate uses the same wording and the same classifier
(``allowed_study_types``) and is deliberately narrower in WHEN it fires:
budgets take free-text drug names, so it only rejects when a study type is
explicitly given AND the drug is classifiable. An omitted study type or an
unclassifiable drug name is never rejected.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from models import Budget, Drug, Protocol, ProtocolStatus, StudyType
from routers.budgets import validate_budget_create
from schemas import BudgetCreate

pytestmark = pytest.mark.usefixtures("no_real_ctgov_calls_for_budgets")

BIOLOGIC_BE = (
    "'adalimumab' is a biologic and cannot run a 'BE' study; "
    "allowed study type(s): biosimilar_pk."
)
SMALL_MOLECULE_BIOSIMILAR = (
    "'metformin' is a small-molecule product and cannot run a "
    "'biosimilar_pk' study; allowed study type(s): BE, food_effect."
)


def _as(user) -> dict[str, str]:
    return {"X-User-Id": str(user.id)}


async def _budget_count(db) -> int:
    return (await db.execute(select(func.count()).select_from(Budget))).scalar_one()


# ---------------------------------------------------------------------------
# Rejections
# ---------------------------------------------------------------------------


async def test_biologic_with_a_small_molecule_study_type_is_422(api_client, seeded):
    resp = await api_client.post(
        "/api/budgets", json={"drug_inn": "adalimumab", "study_type": "BE"},
        headers=_as(seeded.user),
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == BIOLOGIC_BE
    assert await _budget_count(seeded.db) == 0


@pytest.mark.parametrize("study_type", ["BE", "food_effect"])
@pytest.mark.parametrize("drug_inn", ["adalimumab", "nivolumab", "insulin glargine", "filgrastim"])
async def test_every_biologic_shape_rejects_small_molecule_study_types(
    api_client, seeded, drug_inn, study_type
):
    """Curated-KB biologics (nivolumab), '-mab', '-grastim' stems and the
    'insulin ' prefix all classify as biologic."""
    resp = await api_client.post(
        "/api/budgets", json={"drug_inn": drug_inn, "study_type": study_type},
        headers=_as(seeded.user),
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == (
        f"'{drug_inn}' is a biologic and cannot run a '{study_type}' study; "
        "allowed study type(s): biosimilar_pk."
    )


async def test_small_molecule_with_biosimilar_study_type_is_422(api_client, seeded):
    resp = await api_client.post(
        "/api/budgets", json={"drug_inn": "metformin", "study_type": "biosimilar_pk"},
        headers=_as(seeded.user),
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == SMALL_MOLECULE_BIOSIMILAR
    assert await _budget_count(seeded.db) == 0


async def test_gate_wording_is_identical_to_the_protocol_creation_gate(api_client, seeded):
    adalimumab = Drug(id=uuid.uuid4(), inn="adalimumab", brand_names=[], rxnorm_id=None)
    seeded.db.add(adalimumab)
    await seeded.db.commit()

    protocol_resp = await api_client.post(
        "/api/protocols/",
        json={"drug_id": str(adalimumab.id), "study_type": "BE"},
        headers=_as(seeded.user),
    )
    budget_resp = await api_client.post(
        "/api/budgets", json={"drug_inn": "adalimumab", "study_type": "BE"},
        headers=_as(seeded.user),
    )
    assert protocol_resp.status_code == budget_resp.status_code == 422
    assert protocol_resp.json()["detail"] == budget_resp.json()["detail"] == BIOLOGIC_BE


async def test_gate_applies_to_the_drug_taken_from_a_linked_protocol(api_client, seeded):
    """No drug_inn in the body: the drug comes from the linked protocol, and an
    explicit study type that drug cannot run is still rejected."""
    protocol = Protocol(
        id=uuid.uuid4(), org_id=seeded.user.org_id, drug_id=seeded.drug.id,  # metformin
        study_type=StudyType.be, population_config={}, sections={"synopsis": "x"},
        version=1, status=ProtocolStatus.draft, guideline_version="FDA-BE-2014",
        created_by=seeded.user.id,
    )
    seeded.db.add(protocol)
    await seeded.db.commit()

    resp = await api_client.post(
        "/api/budgets",
        json={"protocol_id": str(protocol.id), "study_type": "biosimilar_pk"},
        headers=_as(seeded.user),
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == SMALL_MOLECULE_BIOSIMILAR

    # ... but with no explicit study type the protocol's own (BE) is used and fine.
    ok = await api_client.post(
        "/api/budgets", json={"protocol_id": str(protocol.id)}, headers=_as(seeded.user),
    )
    assert ok.status_code == 201, ok.text
    assert ok.json()["study_type"] == "BE"


# ---------------------------------------------------------------------------
# Not rejected
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "drug_inn, study_type",
    [
        ("adalimumab", "biosimilar_pk"),
        ("nivolumab", "biosimilar_pk"),
        ("metformin", "BE"),
        ("metformin", "food_effect"),
    ],
)
async def test_allowed_combinations_are_created(api_client, seeded, drug_inn, study_type):
    resp = await api_client.post(
        "/api/budgets", json={"drug_inn": drug_inn, "study_type": study_type},
        headers=_as(seeded.user),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["study_type"] == study_type
    assert resp.json()["drug_inn"] == drug_inn


async def test_omitted_study_type_is_never_rejected(api_client, seeded):
    for drug_inn in ("adalimumab", "metformin", "nivolumab"):
        resp = await api_client.post(
            "/api/budgets", json={"drug_inn": drug_inn}, headers=_as(seeded.user),
        )
        assert resp.status_code == 201, f"{drug_inn}: {resp.text}"


@pytest.mark.parametrize("study_type", ["BE", "biosimilar_pk", "food_effect"])
async def test_unclassifiable_drug_name_accepts_any_study_type(api_client, seeded, study_type):
    """Budgets accept free-text drug names; with no confident classification
    nothing is restricted (same rule as allowed_study_types -> None)."""
    resp = await api_client.post(
        "/api/budgets", json={"drug_inn": "Zorbulatrex", "study_type": study_type},
        headers=_as(seeded.user),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["study_type"] == study_type


async def test_blank_study_type_is_treated_as_omitted(api_client, seeded):
    resp = await api_client.post(
        "/api/budgets", json={"drug_inn": "adalimumab", "study_type": "   "},
        headers=_as(seeded.user),
    )
    assert resp.status_code == 201, resp.text


async def test_unknown_study_type_message_is_unchanged_and_comes_first(api_client, seeded):
    resp = await api_client.post(
        "/api/budgets", json={"drug_inn": "adalimumab", "study_type": "nonsense"},
        headers=_as(seeded.user),
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == (
        "Unknown study_type 'nonsense'. Expected one of: BE, biosimilar_pk, food_effect"
    )


# ---------------------------------------------------------------------------
# The facade validates without writing
# ---------------------------------------------------------------------------


async def test_validate_budget_create_resolves_inputs_and_writes_nothing(seeded):
    from fastapi import HTTPException

    ctx = await validate_budget_create(
        seeded.db, seeded.user, BudgetCreate(drug_inn="  adalimumab ", study_type="biosimilar_pk"),
    )
    assert ctx.drug_inn == "adalimumab"
    assert ctx.study_type == "biosimilar_pk"
    assert ctx.name == "Adalimumab study budget"
    assert ctx.protocol is None

    with pytest.raises(HTTPException) as exc:
        await validate_budget_create(
            seeded.db, seeded.user, BudgetCreate(drug_inn="adalimumab", study_type="food_effect"),
        )
    assert exc.value.status_code == 422
    assert "is a biologic and cannot run a 'food_effect' study" in exc.value.detail

    with pytest.raises(HTTPException) as exc:
        await validate_budget_create(seeded.db, seeded.user, BudgetCreate())
    assert exc.value.status_code == 422
    assert "drug name" in exc.value.detail

    assert await _budget_count(seeded.db) == 0
