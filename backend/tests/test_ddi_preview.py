"""POST /api/protocols/ddi-preview (B2): credit-free, drug-specific
drug-drug-interaction screening for the Population step.

Before this route existed the wizard's ``previewDdi`` call 404'd and
PopulationBuilder fell back to "Drug-specific screening isn't available" for
every drug. The route runs the same ``screen_interactions`` engine generation
runs, on stored PK + the drug's DailyMed label, and returns the engine's dict -
the shape of the frontend's ``DdiScreeningResult``.

The label fetch is stubbed (no network): either by replacing the module-level
``_fetch_label_safe`` seam (as the other preview tests do) or, for the
own-session test, by replacing ``LiteratureService.fetch_dailymed`` so the REAL
``_fetch_label_safe`` trimming runs.
"""

from __future__ import annotations

import uuid

import pytest

from models import Drug, PKParameter, PKParameterRecord
from routers import protocols as router
from routers.protocols import compute_ddi_preview
from schemas import DdiPreviewRequest

# Every key of the frontend's DdiScreeningResult that the engine always emits
# (client types.ts lines ~337-360); `drug_inn` is filled in by the route when the
# biologic path omits it.
EXPECTED_KEYS = {
    "drug_inn", "design_key", "pathway_keys", "pathways", "prohibited", "restricted",
    "permitted", "max_washout_days", "screening_history_window_days",
    "history_exclusion_agents", "study_drug_t_half_hours", "post_dose_restriction_hours",
    "unresolved", "assumptions", "summary",
}
ROW_KEYS = {"key", "label", "status"}

IBUPROFEN_LABEL = {
    "title": "Ibuprofen tablets",
    "clinical_pharmacology": (
        "Ibuprofen is extensively metabolized by CYP2C9 (the S-enantiomer) to inactive "
        "hydroxylated and carboxylated metabolites."
    ),
    "pharmacokinetics": "The elimination half-life of ibuprofen is about 2 hours.",
}
ADALIMUMAB_LABEL = {
    "title": "Adalimumab injection",
    "indications_usage": "Adalimumab is indicated for rheumatoid arthritis and Crohn's disease.",
    "clinical_pharmacology": "Adalimumab is a recombinant human IgG1 monoclonal antibody that binds TNF-alpha.",
}


def _label_stub(monkeypatch, label):
    """Replace the shared safe label fetch (module-level seam, no network)."""
    async def _fake(_lit_svc, _inn):
        return dict(label) if label is not None else None

    monkeypatch.setattr(router, "_fetch_label_safe", _fake)


async def _add_drug(db, inn: str) -> Drug:
    drug = Drug(id=uuid.uuid4(), inn=inn, brand_names=[], rxnorm_id=None)
    db.add(drug)
    await db.commit()
    await db.refresh(drug)
    return drug


def _body(drug, study_type, population_config=None):
    return {
        "drug_id": str(drug.id),
        "study_type": study_type,
        "population_config": population_config or {},
    }


def _assert_ddi_shape(data: dict) -> None:
    assert EXPECTED_KEYS <= set(data), f"missing keys: {EXPECTED_KEYS - set(data)}"
    assert isinstance(data["pathway_keys"], list)
    for bucket in ("prohibited", "restricted", "permitted"):
        assert isinstance(data[bucket], list)
        for row in data[bucket]:
            assert ROW_KEYS <= set(row), f"{bucket} row missing {ROW_KEYS - set(row)}: {row}"
    assert isinstance(data["summary"], str) and data["summary"]
    assert isinstance(data["max_washout_days"], (int, float))


# ---------------------------------------------------------------------------
# Small molecule (ibuprofen-like)
# ---------------------------------------------------------------------------


async def test_small_molecule_returns_drug_specific_screening(api_client, seeded, monkeypatch):
    _label_stub(monkeypatch, IBUPROFEN_LABEL)
    ibuprofen = await _add_drug(seeded.db, "ibuprofen")

    resp = await api_client.post("/api/protocols/ddi-preview", json=_body(ibuprofen, "BE"))

    assert resp.status_code == 200, resp.text
    data = resp.json()
    _assert_ddi_shape(data)
    assert data["drug_inn"] == "ibuprofen"
    assert data["design_key"] == "crossover"
    # Drug-specific: the CYP2C9 pathway named in the label is detected, and a
    # CYP2C9-specific prohibition (with its washout and evidence) is present.
    assert data["pathway_keys"] == ["cyp2c9"]
    prohibited = {r["key"]: r for r in data["prohibited"]}
    assert "cyp2c9_strong_inducers" in prohibited
    assert prohibited["cyp2c9_strong_inducers"]["washout_days"]
    assert "generic_perpetrators" not in prohibited
    assert data["unresolved"] == [] or all(isinstance(u, str) for u in data["unresolved"])


async def test_screening_follows_the_label_not_a_static_list(api_client, seeded, monkeypatch):
    """The point of the route: a different label gives different rows for the
    same request (the old static preset did not)."""
    ibuprofen = await _add_drug(seeded.db, "ibuprofen")

    _label_stub(monkeypatch, IBUPROFEN_LABEL)
    cyp2c9 = (await api_client.post("/api/protocols/ddi-preview", json=_body(ibuprofen, "BE"))).json()

    _label_stub(monkeypatch, {
        "title": "Other",
        "clinical_pharmacology": "The drug is metabolized by CYP3A4 and is a P-glycoprotein substrate.",
    })
    cyp3a4 = (await api_client.post("/api/protocols/ddi-preview", json=_body(ibuprofen, "BE"))).json()

    assert cyp2c9["pathway_keys"] == ["cyp2c9"]
    assert set(cyp3a4["pathway_keys"]) == {"cyp3a4", "pgp"}
    assert {r["key"] for r in cyp2c9["prohibited"]} != {r["key"] for r in cyp3a4["prohibited"]}

    # No label at all: the engine's clearly-flagged conservative fallback.
    _label_stub(monkeypatch, None)
    none = (await api_client.post("/api/protocols/ddi-preview", json=_body(ibuprofen, "BE"))).json()
    assert none["pathway_keys"] == []
    assert any(r["key"] == "generic_perpetrators" for r in none["prohibited"])
    assert none["unresolved"]


async def test_stored_pk_half_life_reaches_the_screen(api_client, seeded, monkeypatch):
    _label_stub(monkeypatch, IBUPROFEN_LABEL)
    ibuprofen = await _add_drug(seeded.db, "ibuprofen")
    seeded.db.add(PKParameterRecord(
        id=uuid.uuid4(), drug_id=ibuprofen.id, parameter=PKParameter.t_half,
        value=2.0, unit="h", population="healthy adults", source_pmid="23456789", confidence=0.9,
    ))
    await seeded.db.commit()

    data = (await api_client.post("/api/protocols/ddi-preview", json=_body(ibuprofen, "BE"))).json()

    assert data["study_drug_t_half_hours"] == 2.0
    assert data["post_dose_restriction_hours"]


async def test_nti_flag_in_population_config_reaches_the_engine(api_client, seeded, monkeypatch):
    _label_stub(monkeypatch, IBUPROFEN_LABEL)
    ibuprofen = await _add_drug(seeded.db, "ibuprofen")

    plain = (await api_client.post("/api/protocols/ddi-preview", json=_body(ibuprofen, "BE"))).json()
    nti = (await api_client.post(
        "/api/protocols/ddi-preview",
        json=_body(ibuprofen, "BE", {"is_nti": True, "design": "replicate"}),
    )).json()

    assert plain["is_nti"] is False
    assert nti["is_nti"] is True
    assert nti["design_key"] == "replicate"


# ---------------------------------------------------------------------------
# Biologic (adalimumab-like)
# ---------------------------------------------------------------------------


async def test_biologic_gets_immunologic_not_cyp_screening(api_client, seeded, monkeypatch):
    _label_stub(monkeypatch, ADALIMUMAB_LABEL)
    adalimumab = await _add_drug(seeded.db, "adalimumab")

    resp = await api_client.post(
        "/api/protocols/ddi-preview", json=_body(adalimumab, "biosimilar_pk"),
    )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    _assert_ddi_shape(data)
    assert data["drug_inn"] == "adalimumab"  # filled in on the biologic path
    assert data["design_key"] == "parallel"
    # No CYP / transporter rows for a therapeutic protein ...
    assert data["pathway_keys"] == []
    prohibited = {r["key"] for r in data["prohibited"]}
    assert not any(k.startswith("cyp") or k == "generic_perpetrators" for k in prohibited)
    # ... the interactions that matter for it instead.
    assert {"prior_exposure_naive", "immunosuppressants", "live_vaccines"} <= prohibited


async def test_biologic_also_detected_for_a_biosimilar_study_of_an_unclassified_name(
    api_client, seeded, monkeypatch
):
    """A biosimilar PK study is a biologic study even when the INN stem
    heuristics don't recognise the name."""
    _label_stub(monkeypatch, None)
    odd = await _add_drug(seeded.db, "zorbulatrex")

    data = (await api_client.post(
        "/api/protocols/ddi-preview", json=_body(odd, "biosimilar_pk"),
    )).json()

    assert data["pathway_keys"] == []
    assert any(r["key"] == "immunosuppressants" for r in data["prohibited"])


# ---------------------------------------------------------------------------
# Errors, routing, rate-limit scope, facade
# ---------------------------------------------------------------------------


async def test_unknown_drug_is_404(api_client, seeded, monkeypatch):
    _label_stub(monkeypatch, None)
    resp = await api_client.post(
        "/api/protocols/ddi-preview",
        json={"drug_id": str(uuid.uuid4()), "study_type": "BE", "population_config": {}},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Drug not found."


async def test_invalid_study_type_is_422_and_not_shadowed_by_protocol_id_routes(api_client, seeded):
    resp = await api_client.post(
        "/api/protocols/ddi-preview",
        json={"drug_id": str(seeded.drug.id), "study_type": "nonsense"},
    )
    # A request-validation error from OUR body model (not a "protocol_id is not a
    # valid UUID" path-parameter error from a shadowing /{protocol_id} route).
    assert resp.status_code == 422
    locs = [tuple(e["loc"]) for e in resp.json()["detail"]]
    assert ("body", "study_type") in locs
    assert not any("protocol_id" in loc for loc in locs)


async def test_route_is_open_like_design_preview(api_client, seeded, monkeypatch):
    """Public drug data, no org data: no X-User-Id needed (same as design-preview)."""
    _label_stub(monkeypatch, IBUPROFEN_LABEL)
    ibuprofen = await _add_drug(seeded.db, "ibuprofen")
    resp = await api_client.post("/api/protocols/ddi-preview", json=_body(ibuprofen, "BE"))
    assert resp.status_code == 200


def test_ddi_preview_has_the_same_rate_limit_as_design_preview():
    """Same 30 requests / 60 s, in its own bucket (scope) so the Population step
    calling both previews does not halve either allowance."""
    def _limiter_closure_values(path):
        route = next(r for r in router.router.routes if getattr(r, "path", "") == path)
        # route.dependencies also carries the router-level audit_logger; pick the
        # rate limiter (a closure created inside dependencies.rate_limit).
        dep = next(
            d.dependency for d in route.dependencies
            if d.dependency.__qualname__.startswith("rate_limit.")
        )
        return {c.cell_contents for c in (dep.__closure__ or ())}

    design = _limiter_closure_values("/api/protocols/design-preview")
    ddi = _limiter_closure_values("/api/protocols/ddi-preview")
    assert {30, 60} <= design
    assert {30, 60} <= ddi
    assert "design_preview" in design
    assert "ddi_preview" in ddi


async def test_design_preview_response_is_unchanged_no_ddi_field(api_client, seeded, monkeypatch):
    _label_stub(monkeypatch, None)
    resp = await api_client.post(
        "/api/protocols/design-preview",
        json={"drug_id": str(seeded.drug.id), "study_type": "BE", "population_config": {}},
    )
    assert resp.status_code == 200
    assert set(resp.json()) == {"drug_inn", "study_type", "decisions"}


async def test_facade_matches_route_and_uses_its_own_label_session(seeded, api_client, monkeypatch):
    """compute_ddi_preview == the route, and the REAL label fetch (trimming
    included) runs on a private session, not the caller's."""
    from services.literature_service import LiteratureService

    seen_sessions: list[object] = []

    async def _fake_dailymed(self, drug_inn):
        seen_sessions.append(self._session)
        return {"title": "Ibuprofen", "sections": dict(IBUPROFEN_LABEL)}

    monkeypatch.setattr(LiteratureService, "fetch_dailymed", _fake_dailymed)
    ibuprofen = await _add_drug(seeded.db, "ibuprofen")
    body = _body(ibuprofen, "BE")

    resp = await api_client.post("/api/protocols/ddi-preview", json=body)
    assert resp.status_code == 200, resp.text
    facade = await compute_ddi_preview(seeded.db, DdiPreviewRequest(**body))

    assert facade == resp.json()
    assert facade["pathway_keys"] == ["cyp2c9"]
    assert len(seen_sessions) == 2
    assert all(s is not seeded.db for s in seen_sessions)
