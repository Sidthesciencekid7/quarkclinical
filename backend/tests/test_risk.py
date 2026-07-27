"""Integration tests for the protocol risk / feasibility scoring endpoint.

GET /api/protocols/{id}/risk is deterministic (no AI call) but still needs the
full request cycle: org-scoped auth, a real protocol row, and the QC engine
feeding the scorer. External calls are mocked exactly as in test_pipeline.py.
"""

from __future__ import annotations

import uuid

from models import User, UserRole
from security import hash_password
from services.risk_scoring import score_protocol_risk
from tests.conftest import register_pubmed_mocks

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

_LEVELS = {"low", "moderate", "high"}


def _protocol_payload(drug_id: str, user_id: str) -> dict:
    return {**_STUDY_PAYLOAD_BASE, "drug_id": drug_id, "created_by": user_id}


async def _create_protocol(api_client, seeded, httpx_mock) -> str:
    register_pubmed_mocks(httpx_mock)
    resp = await api_client.post(
        "/api/protocols/",
        json=_protocol_payload(str(seeded.drug.id), str(seeded.user.id)),
        headers={"X-User-Id": str(seeded.user.id)},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _other_org_generator(db) -> User:
    """A generator in a different org than the `seeded` fixture's (test-org)."""
    other = User(
        id=uuid.uuid4(),
        org_id="other-org",
        email="generator@otherrisk.com",
        role=UserRole.generator,
        hashed_password=hash_password("irrelevant-for-these-tests"),
    )
    db.add(other)
    await db.commit()
    await db.refresh(other)
    return other


# ---------------------------------------------------------------------------
# Endpoint shape
# ---------------------------------------------------------------------------


async def test_risk_endpoint_returns_scored_report(
    api_client, seeded, mock_anthropic, httpx_mock
):
    """GET /risk returns a 0-100 score, a valid band, and well-formed factors."""
    protocol_id = await _create_protocol(api_client, seeded, httpx_mock)

    resp = await api_client.get(
        f"/api/protocols/{protocol_id}/risk",
        headers={"X-User-Id": str(seeded.user.id)},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert isinstance(body["overall_score"], int)
    assert 0 <= body["overall_score"] <= 100
    assert body["risk_level"] in _LEVELS

    factors = body["factors"]
    assert isinstance(factors, list) and factors, "expected at least one factor"
    for f in factors:
        assert isinstance(f, dict)
        assert set(("label", "severity", "detail")).issubset(f.keys())
        assert isinstance(f["label"], str) and f["label"]
        assert f["severity"] in _LEVELS
        assert isinstance(f["detail"], str) and f["detail"], "every factor needs plain English"


async def test_risk_score_band_matches_score(
    api_client, seeded, mock_anthropic, httpx_mock
):
    """The band is a pure function of the score — no drift between the two."""
    protocol_id = await _create_protocol(api_client, seeded, httpx_mock)

    body = (await api_client.get(
        f"/api/protocols/{protocol_id}/risk",
        headers={"X-User-Id": str(seeded.user.id)},
    )).json()

    score, level = body["overall_score"], body["risk_level"]
    expected = "low" if score < 30 else ("moderate" if score < 60 else "high")
    assert level == expected


# ---------------------------------------------------------------------------
# Org isolation
# ---------------------------------------------------------------------------


async def test_risk_cross_org_returns_403(
    api_client, seeded, mock_anthropic, httpx_mock
):
    """A generator from another org cannot read this protocol's risk report."""
    protocol_id = await _create_protocol(api_client, seeded, httpx_mock)
    other = await _other_org_generator(seeded.db)

    cross_org = await api_client.get(
        f"/api/protocols/{protocol_id}/risk",
        headers={"X-User-Id": str(other.id)},
    )
    assert cross_org.status_code == 403, cross_org.text

    same_org = await api_client.get(
        f"/api/protocols/{protocol_id}/risk",
        headers={"X-User-Id": str(seeded.user.id)},
    )
    assert same_org.status_code == 200


async def test_risk_unknown_protocol_returns_404(api_client, seeded):
    resp = await api_client.get(
        f"/api/protocols/{uuid.uuid4()}/risk",
        headers={"X-User-Id": str(seeded.user.id)},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Scorer unit behaviour — robustness and factor derivation (no DB needed)
# ---------------------------------------------------------------------------


class _FakeProtocol:
    def __init__(self, population_config=None, sections=None, guideline_version=None):
        self.population_config = population_config
        self.sections = sections
        self.guideline_version = guideline_version


def test_scorer_survives_completely_empty_protocol():
    """Missing/None fields must degrade, never raise."""
    result = score_protocol_risk(_FakeProtocol())
    assert 0 <= result["overall_score"] <= 100
    assert result["risk_level"] in _LEVELS
    assert result["factors"]


def test_scorer_flags_large_cohort_nti_and_assumed_cv():
    result = score_protocol_risk(_FakeProtocol(
        population_config={"_sample_size": {
            "n_enrolled": 140,
            "cv_pct": 48.0,
            "cv_source": "assumed",
            "is_nti": True,
            "design_key": "replicate",
            "t_half_hours": 8.0,
        }},
        sections={"synopsis": "text"},
        guideline_version="FDA-BE-2014",
    ))
    labels = " | ".join(f["label"] for f in result["factors"]).lower()
    assert "140" in labels
    assert "narrow therapeutic index" in labels
    assert "assumed" in labels
    assert "replicate" in labels
    assert result["risk_level"] == "high"
