"""Integration tests for clinical study budgets (routers/budgets.py, schemas.py,
dependencies.py::get_budget_scoped) plus pure-unit tests for
services/budget_engine.py.

Auth is OFF in this suite (conftest clears CLERK_ISSUER), so the acting user is
supplied via the X-User-Id header — exactly the identity path
authenticate_actor falls back to in that mode.

NOTE: this file is written against services/budget_engine.py and the Budget /
BudgetCategory / BudgetLineItem models being authored concurrently by another
agent from the same architecture spec. It is expected to fail to import/collect
until that work lands — see the "Backend API layer" task notes.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from models import (
    Budget,
    BudgetCategory,
    BudgetLineItem,
    Protocol,
    ProtocolStatus,
    StudyType,
    User,
    UserRole,
)
from security import hash_password
from services import budget_engine

# Opt in to conftest.py's no_real_ctgov_calls_for_budgets fixture for every
# test in this file — routers/budgets.py's create/reprice endpoints now call
# LiteratureService.fetch_clinicaltrials for real-trial-history grounding
# (services/budget_trial_precedent.py), and the seeded "metformin" Drug row
# would otherwise make this suite attempt a real network call. See that
# fixture's docstring for why it is not simply autouse=True suite-wide.
pytestmark = pytest.mark.usefixtures("no_real_ctgov_calls_for_budgets")

# NOTE: no module-level `pytestmark = pytest.mark.asyncio` here (unlike
# test_comments.py) — this file's asyncio_mode=auto (pytest.ini) already
# auto-detects `async def` tests, and section L below adds plain synchronous
# unit tests for budget_engine; a blanket asyncio mark on those would only
# produce a spurious PytestWarning.


# ---------------------------------------------------------------------------
# Local helpers (this repo has no shared protocol/auth-header fixture)
# ---------------------------------------------------------------------------


def _as(user: User) -> dict[str, str]:
    return {"X-User-Id": str(user.id)}


async def _make_protocol(
    db, drug, creator, org_id: str, *, study_type=StudyType.be, population_config=None,
) -> Protocol:
    protocol = Protocol(
        id=uuid.uuid4(),
        org_id=org_id,
        drug_id=drug.id,
        study_type=study_type,
        population_config=population_config or {"population": "healthy_volunteers"},
        sections={"study_design": "Placeholder."},
        version=1,
        status=ProtocolStatus.draft,
        guideline_version="FDA-BE-2014",
        created_by=creator.id,
    )
    db.add(protocol)
    await db.commit()
    await db.refresh(protocol)
    return protocol


def _derivable_config(*, n_enrolled=24, n_pk=12, n_ada=0, days=(1, 2, 3)) -> dict:
    """population_config carrying the same UNDERSCORE-prefixed engine keys the
    real pipeline writes.

    ``n_pk`` PK draws are spread evenly across ``days`` distinct visit days
    (cycling through ``days``) so "PK sampling draws" quantity == n_pk *
    n_enrolled while "Enrolled subject visits" quantity == len(days) *
    n_enrolled — the two must be independently controllable per the
    architecture spec's worked example (n_pk=12 across 3 days -> 288 PK draws,
    72 subject visits).
    """
    n_days = len(days)
    timepoints = [
        {"hours": float((days[i % n_days] - 1) * 24 + (i // n_days)), "day": days[i % n_days]}
        for i in range(n_pk)
    ]
    return {
        "population": "healthy_volunteers",
        "_sample_size": {
            "n_enrolled": n_enrolled,
            "n_evaluable": max(n_enrolled - 4, 0),
            "n_per_group": None,
            "design_key": "crossover",
        },
        "_sampling_schedule": {
            "timepoints": timepoints,
            "immunogenicity_timepoints": [
                {"hours": 24.0 * i, "day": i + 1} for i in range(n_ada)
            ],
            "steady_state_troughs": [],
            "periods": 2,
            "summary": "...",
        },
    }


async def _make_outsider(db) -> User:
    outsider = User(
        id=uuid.uuid4(),
        org_id="other-org",
        email="outsider@other.example",
        role=UserRole.generator,
        hashed_password=hash_password("irrelevant"),
    )
    db.add(outsider)
    await db.commit()
    await db.refresh(outsider)
    return outsider


async def _create_budget(api_client, user, **overrides) -> dict:
    payload = {"name": "Test Budget"} | overrides
    resp = await api_client.post("/api/budgets", json=payload, headers=_as(user))
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _strip_categories(api_client, user, budget: dict) -> dict:
    """Delete every auto-seeded rate-card category, leaving an empty budget
    shell. Categories are now ALWAYS seeded and fully priced on create (no
    more seed_default_categories=False), so tests that exercise rollup/
    line-item arithmetic in isolation strip them first instead."""
    for cat in budget["categories"]:
        resp = await api_client.delete(
            f"/api/budgets/{budget['id']}/categories/{cat['id']}", headers=_as(user),
        )
        assert resp.status_code == 204, resp.text
    refetched = await api_client.get(f"/api/budgets/{budget['id']}", headers=_as(user))
    assert refetched.status_code == 200, refetched.text
    return refetched.json()


async def _create_empty_budget(api_client, user, **overrides) -> dict:
    """The modern equivalent of the old `seed_default_categories=False`: a
    budget with the rate-card categories/line-items stripped immediately."""
    created = await _create_budget(api_client, user, **overrides)
    return await _strip_categories(api_client, user, created)


async def _create_category(api_client, user, budget_id, **overrides) -> dict:
    payload = {"name": "Custom Category", "bucket": "direct"} | overrides
    resp = await api_client.post(
        f"/api/budgets/{budget_id}/categories", json=payload, headers=_as(user),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_line_item(api_client, user, budget_id, category_id, **overrides) -> dict:
    payload = {"label": "Line item"} | overrides
    resp = await api_client.post(
        f"/api/budgets/{budget_id}/categories/{category_id}/line-items",
        json=payload,
        headers=_as(user),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _find_line_item(budget: dict, label: str) -> dict | None:
    for cat in budget["categories"]:
        for li in cat["line_items"]:
            if li["label"] == label:
                return li
    return None


def _find_category_for_line_item(budget: dict, label: str) -> dict | None:
    for cat in budget["categories"]:
        for li in cat["line_items"]:
            if li["label"] == label:
                return cat
    return None


# ===========================================================================
# A. Standalone lifecycle (no protocol)
# ===========================================================================


async def test_create_standalone_budget_seeds_default_categories(api_client, seeded):
    body = await _create_budget(api_client, seeded.user, name="Metformin BE Budget")

    assert body["protocol_id"] is None
    assert len(body["categories"]) == 17

    buckets = [c["bucket"] for c in body["categories"]]
    assert buckets.count("direct") == 14
    assert buckets.count("stipend") == 1
    assert buckets.count("variable") == 1
    assert buckets.count("pass_through") == 1

    # Categories come back in DEFAULT_CATEGORIES order (sort_order ascending).
    sort_orders = [c["sort_order"] for c in body["categories"]]
    assert sort_orders == sorted(sort_orders)

    # Categories are ALWAYS fully priced now — no empty-shell budget.
    assert body["rollup"]["grand_total"] > 0.0
    line_item_count = sum(len(c["line_items"]) for c in body["categories"])
    assert line_item_count > 60


async def test_create_budget_ignores_legacy_seed_default_categories_flag(api_client, seeded):
    """The old seed_default_categories toggle is gone — there is no
    replacement flag, and categories are ALWAYS seeded and priced. A client
    that still sends the old field gets it silently ignored (Pydantic v2's
    default extra='ignore'), not a 422 and not an empty budget."""
    body = await _create_budget(api_client, seeded.user, seed_default_categories=False)
    assert len(body["categories"]) == 17
    assert body["rollup"]["grand_total"] > 0.0


async def test_drug_name_alone_produces_a_complete_fully_priced_budget(api_client, seeded):
    """The user's own words: 'as simple as adding a drug's name and getting
    the final budget file' — no protocol, no manual pricing step."""
    body = await _create_budget(api_client, seeded.user, name=None, drug_inn="metformin")

    assert body["protocol_id"] is None
    assert body["drug_inn"] == "metformin"
    assert body["study_type"] == "BE"  # small-molecule default
    assert len(body["categories"]) == 17
    line_item_count = sum(len(c["line_items"]) for c in body["categories"])
    assert line_item_count > 60
    assert body["rollup"]["grand_total"] > 0.0
    assert body["discount_type"] == "none"

    # Every costed line item is actually priced — no manual pricing step.
    for cat in body["categories"]:
        for li in cat["line_items"]:
            if li["status"] == "costed":
                assert li["unit_cost"] is not None
                assert li["total"] is not None


async def test_biologic_drug_name_defaults_to_biosimilar_pk_profile(api_client, seeded):
    """A biologic INN with no protocol linked should fall back to the
    biosimilar_pk default profile, reproducing the real Abatacept reference
    numbers: 60 enrolled, 10% alternates -> 6, 40% screen-failure rate ->
    110 screened, 44 screen failures."""
    body = await _create_budget(api_client, seeded.user, name=None, drug_inn="abatacept")

    assert body["study_type"] == "biosimilar_pk"
    assert body["n_enrolled"] == 60
    assert body["n_screened"] == 110
    assert body["n_screen_failures"] == 44
    assert body["n_alternates"] == 6
    assert body["derivation"]["modality"] == "biologic"
    assert body["derivation"]["complexity"] == "complex"


def _fake_ctgov_trial(nct_id: str, *, enrollment_count: int, phase: str = "PHASE1", **overrides) -> dict:
    """Builds a services.literature_service.ClinicalTrialRecord-shaped dict
    (a TypedDict, so a plain dict satisfies it at runtime) for monkeypatching
    LiteratureService.fetch_clinicaltrials in the tests below."""
    record = {
        "nct_id": nct_id, "title": f"A study of {nct_id}", "status": "COMPLETED",
        "phase": phase, "conditions": [], "interventions": [], "sex": "ALL",
        "minimum_age": "18 Years", "maximum_age": "", "healthy_volunteers": True,
        "eligibility_criteria": "", "enrollment_count": enrollment_count,
        "enrollment_type": "ACTUAL", "countries": ["United States"], "n_locations": 1,
        "start_date": "2018-01", "primary_completion_date": "2018-07",
    }
    record.update(overrides)
    return record


async def test_two_drugs_with_different_real_trial_history_get_different_budgets(
    api_client, seeded, monkeypatch,
):
    """The user's exact bug report: 'I am getting the same dollar amount for
    any drug I submit'. Two biosimilar_pk-classified drugs sharing the SAME
    generic default profile (n_enrolled=60) must diverge once each has its
    own real ClinicalTrials.gov trial history — proving the budget is no
    longer a generic per-study-type template."""
    from services.literature_service import LiteratureService

    async def _fake_fetch(self, drug_inn):
        if drug_inn == "abatacept":
            return [_fake_ctgov_trial("NCT00001111", enrollment_count=200)]
        return []
    monkeypatch.setattr(LiteratureService, "fetch_clinicaltrials", _fake_fetch)

    # adalimumab (like abatacept) is nhv_single_dose_safe and not on the
    # curated oncology list, so it resolves to the SAME healthy/biosimilar_pk
    # classification and the SAME generic default (n_enrolled=60) abatacept
    # would ALSO get without a real trial history — the actual bug being
    # fixed here.
    grounded = await _create_budget(api_client, seeded.user, name=None, drug_inn="abatacept")
    ungrounded = await _create_budget(api_client, seeded.user, name=None, drug_inn="adalimumab")

    assert ungrounded["study_type"] == grounded["study_type"] == "biosimilar_pk"

    assert grounded["n_enrolled"] == 200
    assert grounded["derivation"]["assumptions"]["n_enrolled"]["source"] == "trial_precedent"
    assert grounded["derivation"]["trial_precedent"]["found"] is True
    assert grounded["derivation"]["trial_precedent"]["trials"][0]["nct_id"] == "NCT00001111"

    # adalimumab has no fake trial history registered above -> honest fallback
    # to the exact same generic default as before this feature existed.
    assert ungrounded["n_enrolled"] == 60
    assert ungrounded["derivation"]["assumptions"]["n_enrolled"]["source"] == "default"
    assert ungrounded["derivation"]["trial_precedent"]["found"] is False

    assert grounded["rollup"]["grand_total"] != ungrounded["rollup"]["grand_total"]


async def test_ctgov_fetch_failure_falls_back_to_generic_default(api_client, seeded, monkeypatch):
    """A CT.gov outage (or an unregistered drug raising inside
    LiteratureService) must never break budget creation — same resilience
    contract as routers/protocols.py's _fetch_trial_precedent_safe."""
    from services.literature_service import LiteratureService

    async def _boom(self, drug_inn):
        raise RuntimeError("ClinicalTrials.gov is down")
    monkeypatch.setattr(LiteratureService, "fetch_clinicaltrials", _boom)

    body = await _create_budget(api_client, seeded.user, name=None, drug_inn="abatacept")
    assert body["n_enrolled"] == 60
    assert body["derivation"]["trial_precedent"]["found"] is False


async def test_create_budget_requires_name_or_drug_inn(api_client, seeded):
    resp = await api_client.post(
        "/api/budgets", json={"sponsor_name": "Acme"}, headers=_as(seeded.user),
    )
    assert resp.status_code == 422, resp.text


async def test_list_budgets_for_same_org(api_client, seeded):
    created = await _create_budget(api_client, seeded.user, name="Listed Budget")

    listing = await api_client.get("/api/budgets", headers=_as(seeded.user))
    assert listing.status_code == 200, listing.text
    rows = listing.json()
    row = next(r for r in rows if r["id"] == created["id"])
    assert row["name"] == "Listed Budget"
    assert row["grand_total"] > 0.0
    assert row["category_count"] == 17
    assert row["line_item_count"] > 60


async def test_patch_updates_only_supplied_fields(api_client, seeded):
    created = await _create_budget(api_client, seeded.user, sponsor_name="Acme Sponsor")

    patched = await api_client.patch(
        f"/api/budgets/{created['id']}",
        json={"name": "Renamed Budget", "status": "final", "n_sites": 12},
        headers=_as(seeded.user),
    )
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["name"] == "Renamed Budget"
    assert body["status"] == "final"
    assert body["n_sites"] == 12
    # Untouched fields survive (proves exclude_unset semantics).
    assert body["sponsor_name"] == "Acme Sponsor"
    assert body["currency"] == "USD"


async def test_delete_line_item_removes_it_but_keeps_the_category(api_client, seeded):
    created = await _create_budget(api_client, seeded.user)
    cat = await _create_category(api_client, seeded.user, created["id"])
    li = await _create_line_item(api_client, seeded.user, created["id"], cat["id"], label="Doomed")

    deleted = await api_client.delete(
        f"/api/budgets/{created['id']}/line-items/{li['id']}", headers=_as(seeded.user),
    )
    assert deleted.status_code == 204, deleted.text

    refetched = await api_client.get(f"/api/budgets/{created['id']}", headers=_as(seeded.user))
    body = refetched.json()
    remaining_cat = next(c for c in body["categories"] if c["id"] == cat["id"])
    assert remaining_cat["line_items"] == []
    assert await seeded.db.get(BudgetLineItem, uuid.UUID(li["id"])) is None


async def test_delete_budget_cascades_categories_and_line_items(api_client, seeded):
    created = await _create_budget(api_client, seeded.user)
    budget_id = uuid.UUID(created["id"])
    category_id = uuid.UUID(created["categories"][0]["id"])
    li = await _create_line_item(api_client, seeded.user, created["id"], str(category_id))
    line_item_id = uuid.UUID(li["id"])

    deleted = await api_client.delete(f"/api/budgets/{created['id']}", headers=_as(seeded.user))
    assert deleted.status_code == 204, deleted.text

    getresp = await api_client.get(f"/api/budgets/{created['id']}", headers=_as(seeded.user))
    assert getresp.status_code == 404

    assert await seeded.db.get(Budget, budget_id) is None
    assert await seeded.db.get(BudgetCategory, category_id) is None
    assert await seeded.db.get(BudgetLineItem, line_item_id) is None


# ===========================================================================
# B. Rollup arithmetic — the reference case
# ===========================================================================


async def test_rollup_reference_arithmetic(api_client, seeded):
    """Reproduces the shape of a signed CRO budget's rollup: a direct-cost base
    with a 20% discount applied only to that base, plus an undiscounted
    subject-stipend bucket.

    NOTE: uses 950,000 / 150,000 (round figures) rather than the architecture
    spec's literal 956,942 / 167,900 example — 20% of 956,942 is 191,388.40,
    not the clean 191,388 the spec's worked example asserts, so those exact
    numbers cannot simultaneously satisfy "direct_subtotal == 956942" and
    "discount_amount == 191388.0" against a straight
    round(direct_subtotal * pct / 100, 2) implementation (confirmed against
    services/budget_engine.py::compute_rollups). Round figures keep the
    percent/flat/none assertions internally consistent and floating-point-exact.
    """
    created = await _create_empty_budget(api_client, seeded.user)
    budget_id = created["id"]

    direct_cat = await _create_category(api_client, seeded.user, budget_id, name="Recruiting", bucket="direct")
    stipend_cat = await _create_category(api_client, seeded.user, budget_id, name="Subject Stipend", bucket="stipend")

    await _create_line_item(
        api_client, seeded.user, budget_id, direct_cat["id"],
        label="Direct costs lump sum", total=950000,
    )
    await _create_line_item(
        api_client, seeded.user, budget_id, stipend_cat["id"],
        label="Stipend lump sum", total=150000,
    )

    async def _rollup(discount_type, discount_value):
        patched = await api_client.patch(
            f"/api/budgets/{budget_id}",
            json={"discount_type": discount_type, "discount_value": discount_value},
            headers=_as(seeded.user),
        )
        assert patched.status_code == 200, patched.text
        rollup_resp = await api_client.get(f"/api/budgets/{budget_id}/rollup", headers=_as(seeded.user))
        assert rollup_resp.status_code == 200, rollup_resp.text
        return rollup_resp.json()

    r = await _rollup("percent", 20)
    assert r["direct_subtotal"] == 950000.0
    assert r["discount_amount"] == 190000.0
    assert r["total_direct_costs"] == 760000.0
    assert r["subject_stipend_total"] == 150000.0
    assert r["variable_total"] == 0.0
    assert r["pass_through_total"] == 0.0
    assert r["grand_total"] == 910000.0

    r = await _rollup("flat", 190000)
    assert r["grand_total"] == 910000.0

    r = await _rollup("none", 0)
    assert r["grand_total"] == 1100000.0


# ===========================================================================
# C. Rollup exclusion rules
# ===========================================================================


@pytest.mark.parametrize(
    "status", ["out_of_scope", "not_applicable", "not_planned", "outsourced", "actuals_only"],
)
async def test_non_costed_status_excluded_even_with_a_total(api_client, seeded, status):
    created = await _create_empty_budget(api_client, seeded.user)
    cat = await _create_category(api_client, seeded.user, created["id"], name="Excluded", bucket="direct")
    await _create_line_item(
        api_client, seeded.user, created["id"], cat["id"],
        label="Not summed", total=5000, status=status,
    )

    got = await api_client.get(f"/api/budgets/{created['id']}", headers=_as(seeded.user))
    body = got.json()
    assert body["categories"][0]["subtotal"] == 0.0
    assert body["rollup"]["grand_total"] == 0.0


async def test_costed_line_with_null_total_contributes_zero(api_client, seeded):
    created = await _create_empty_budget(api_client, seeded.user)
    cat = await _create_category(api_client, seeded.user, created["id"], bucket="direct")
    await _create_line_item(
        api_client, seeded.user, created["id"], cat["id"],
        label="Unpriced", total=None, status="costed",
    )

    got = await api_client.get(f"/api/budgets/{created['id']}", headers=_as(seeded.user))
    body = got.json()
    assert body["categories"][0]["subtotal"] == 0.0
    assert body["rollup"]["grand_total"] == 0.0


async def test_all_actuals_only_variable_and_pass_through_categories_total_zero(api_client, seeded):
    created = await _create_empty_budget(api_client, seeded.user)
    var_cat = await _create_category(api_client, seeded.user, created["id"], name="CRO Variable", bucket="variable")
    pt_cat = await _create_category(api_client, seeded.user, created["id"], name="Pass-Through", bucket="pass_through")
    await _create_line_item(
        api_client, seeded.user, created["id"], var_cat["id"],
        label="Rate card row", total=9999, status="actuals_only",
    )
    await _create_line_item(
        api_client, seeded.user, created["id"], pt_cat["id"],
        label="Vendor pass-through", total=4321, status="actuals_only",
    )

    rollup = (await api_client.get(f"/api/budgets/{created['id']}/rollup", headers=_as(seeded.user))).json()
    assert rollup["variable_total"] == 0.0
    assert rollup["pass_through_total"] == 0.0


async def test_rebucketing_a_category_moves_its_subtotal(api_client, seeded):
    created = await _create_empty_budget(api_client, seeded.user)
    cat = await _create_category(api_client, seeded.user, created["id"], name="Movable", bucket="direct")
    await _create_line_item(api_client, seeded.user, created["id"], cat["id"], label="Row", total=1000)

    before = (await api_client.get(f"/api/budgets/{created['id']}/rollup", headers=_as(seeded.user))).json()
    assert before["total_direct_costs"] == 1000.0
    assert before["subject_stipend_total"] == 0.0

    await api_client.patch(
        f"/api/budgets/{created['id']}/categories/{cat['id']}",
        json={"bucket": "stipend"},
        headers=_as(seeded.user),
    )

    after = (await api_client.get(f"/api/budgets/{created['id']}/rollup", headers=_as(seeded.user))).json()
    assert after["total_direct_costs"] == 0.0
    assert after["subject_stipend_total"] == 1000.0


# ===========================================================================
# D. Line-item total semantics
# ===========================================================================


async def test_total_computed_from_quantity_and_unit_cost_when_omitted(api_client, seeded):
    created = await _create_budget(api_client, seeded.user)
    cat = await _create_category(api_client, seeded.user, created["id"])
    li = await _create_line_item(
        api_client, seeded.user, created["id"], cat["id"],
        label="Computed", quantity=4, unit_cost=250,
    )
    assert li["total"] == 1000.0


async def test_explicit_total_is_never_overwritten(api_client, seeded):
    created = await _create_budget(api_client, seeded.user)
    cat = await _create_category(api_client, seeded.user, created["id"])
    li = await _create_line_item(
        api_client, seeded.user, created["id"], cat["id"],
        label="Lump sum", quantity=4, unit_cost=250, total=900,
    )
    assert li["total"] == 900.0


async def test_total_only_keeps_quantity_null(api_client, seeded):
    created = await _create_budget(api_client, seeded.user)
    cat = await _create_category(api_client, seeded.user, created["id"])
    li = await _create_line_item(
        api_client, seeded.user, created["id"], cat["id"],
        label="Total only", total=500,
    )
    assert li["total"] == 500.0
    assert li["quantity"] is None


async def test_explicit_null_total_stays_null_even_with_units(api_client, seeded):
    created = await _create_budget(api_client, seeded.user)
    cat = await _create_category(api_client, seeded.user, created["id"])
    li = await _create_line_item(
        api_client, seeded.user, created["id"], cat["id"],
        label="Explicit null total", quantity=4, unit_cost=250, total=None,
    )
    assert li["total"] is None


async def test_patch_quantity_alone_does_not_touch_a_user_set_total(api_client, seeded):
    created = await _create_budget(api_client, seeded.user)
    cat = await _create_category(api_client, seeded.user, created["id"])
    li = await _create_line_item(
        api_client, seeded.user, created["id"], cat["id"],
        label="Priced lump sum", total=750,
    )
    assert li["unit_cost"] is None

    patched = await api_client.patch(
        f"/api/budgets/{created['id']}/line-items/{li['id']}",
        json={"quantity": 10},
        headers=_as(seeded.user),
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["total"] == 750.0
    assert patched.json()["quantity"] == 10.0


# ===========================================================================
# E. Protocol derivation
# ===========================================================================


async def test_protocol_derivation_produces_expected_line_items(api_client, seeded):
    protocol = await _make_protocol(
        seeded.db, seeded.drug, seeded.user, seeded.user.org_id,
        population_config=_derivable_config(n_enrolled=24, n_pk=12, n_ada=0, days=(1, 2, 3)),
    )

    created = await _create_budget(api_client, seeded.user, protocol_id=str(protocol.id))

    pk_row = _find_line_item(created, "PK sampling draws")
    assert pk_row is not None
    assert pk_row["quantity"] == 288.0  # 12 timepoints * 24 enrolled
    assert pk_row["unit_description"] == "per draw"
    assert pk_row["status"] == "costed"
    assert pk_row["source"] == "protocol_derived"
    assert pk_row["derivation_key"] == "pk_draws"
    # ESTIMATION RULE — quantity is confidently derived from the real protocol
    # schedule; unit_cost is a rate-card approximation, but it is filled in,
    # not left for the user to price by hand.
    assert pk_row["unit_cost"] == 45.0  # CONDUCT_PK_DRAW_RATE
    assert pk_row["total"] == 12960.0  # 288 * 45.00

    visits_row = _find_line_item(created, "Enrolled subject visits")
    assert visits_row is not None
    assert visits_row["quantity"] == 72.0  # 3 visit days * 24 enrolled
    assert visits_row["unit_description"] == "per visit"
    assert visits_row["unit_cost"] == 185.0  # CONDUCT_VISIT_DAY_RATE
    assert visits_row["total"] == 13320.0  # 72 * 185.00

    assert _find_line_item(created, "Immunogenicity/ADA sampling draws") is None

    conduct_cat = _find_category_for_line_item(created, "PK sampling draws")
    assert conduct_cat["name"] == "Clinical Conduct"

    assert created["n_enrolled"] == 24
    assert created["drug_inn"] == "metformin"
    assert created["study_type"] == "BE"

    # Every category is auto-priced now, not just Clinical Conduct.
    assert created["rollup"]["grand_total"] > 0.0


async def test_protocol_derivation_includes_ada_row_when_immunogenicity_timepoints_present(api_client, seeded):
    protocol = await _make_protocol(
        seeded.db, seeded.drug, seeded.user, seeded.user.org_id,
        population_config=_derivable_config(n_enrolled=24, n_pk=12, n_ada=5, days=(1, 2, 3)),
    )
    created = await _create_budget(api_client, seeded.user, protocol_id=str(protocol.id))

    ada_row = _find_line_item(created, "Immunogenicity/ADA sampling draws")
    assert ada_row is not None
    assert ada_row["quantity"] == 120.0  # 5 ADA timepoints * 24 enrolled
    assert ada_row["unit_cost"] == 65.0  # CONDUCT_ADA_DRAW_RATE
    assert ada_row["total"] == 7800.0  # 120 * 65.00


async def test_protocol_without_sample_size_uses_defaults_and_still_prices_everything(api_client, seeded):
    """A linked protocol with no computed sample size no longer means an
    unpriced or skipped budget — the rate card's per-study-type defaults take
    over for whichever drivers the protocol didn't supply, and the budget
    comes back fully priced regardless."""
    protocol = await _make_protocol(
        seeded.db, seeded.drug, seeded.user, seeded.user.org_id,
        population_config={"population": "healthy_volunteers"},  # no _sample_size at all
    )
    created = await _create_budget(api_client, seeded.user, protocol_id=str(protocol.id))

    derived_rows = [
        li for cat in created["categories"] for li in cat["line_items"]
        if li["source"] == "protocol_derived"
    ]
    assert derived_rows == []
    assert created["derivation"]["skipped"] is False
    assert created["derivation"]["assumptions"]["n_enrolled"]["source"] == "default"
    warnings = " ".join(created["derivation"].get("warnings") or [])
    assert "n_enrolled" in warnings  # names the defaulted assumptions
    assert created["rollup"]["grand_total"] > 0.0


async def test_generic_steady_state_troughs_are_not_counted_as_visit_days(api_client, seeded):
    config = _derivable_config(n_enrolled=24, n_pk=12, n_ada=0, days=(1, 2, 3))
    # The generic multiple-dose steady_state_troughs shape: neither "day" nor
    # "hours" keys — must not silently be treated as a visit day.
    config["_sampling_schedule"]["steady_state_troughs"] = [
        {"label": "pre-dose trough, day 5"}, {"label": "pre-dose trough, day 6"},
    ]
    protocol = await _make_protocol(
        seeded.db, seeded.drug, seeded.user, seeded.user.org_id, population_config=config,
    )
    created = await _create_budget(api_client, seeded.user, protocol_id=str(protocol.id))

    visits_row = _find_line_item(created, "Enrolled subject visits")
    # Still just the 3 explicit timepoint days — the unrecognized trough shape
    # is not counted.
    assert visits_row["quantity"] == 72.0
    assert created["derivation"].get("warnings")


async def test_multi_period_schedule_warns_but_does_not_double_pk_quantity(api_client, seeded):
    config = _derivable_config(n_enrolled=24, n_pk=12, n_ada=0, days=(1, 2, 3))
    config["_sampling_schedule"]["periods"] = 2
    protocol = await _make_protocol(
        seeded.db, seeded.drug, seeded.user, seeded.user.org_id, population_config=config,
    )
    created = await _create_budget(api_client, seeded.user, protocol_id=str(protocol.id))

    pk_row = _find_line_item(created, "PK sampling draws")
    assert pk_row["quantity"] == 288.0  # NOT doubled for the 2nd period
    warnings = " ".join(created["derivation"].get("warnings") or [])
    assert "period" in warnings.lower()


# ===========================================================================
# F. Re-derivation must not clobber user pricing
# ===========================================================================


async def test_relinking_updates_quantity_but_preserves_user_pricing(api_client, seeded):
    protocol_a = await _make_protocol(
        seeded.db, seeded.drug, seeded.user, seeded.user.org_id,
        population_config=_derivable_config(n_enrolled=24, n_pk=12, n_ada=0, days=(1, 2, 3)),
    )
    created = await _create_budget(api_client, seeded.user, protocol_id=str(protocol_a.id))
    pk_row = _find_line_item(created, "PK sampling draws")

    priced = await api_client.patch(
        f"/api/budgets/{created['id']}/line-items/{pk_row['id']}",
        json={"unit_cost": 38, "total": 10944},
        headers=_as(seeded.user),
    )
    assert priced.status_code == 200, priced.text
    assert priced.json()["total"] == 10944.0

    protocol_b = await _make_protocol(
        seeded.db, seeded.drug, seeded.user, seeded.user.org_id,
        population_config=_derivable_config(n_enrolled=30, n_pk=10, n_ada=0, days=(1, 2)),
    )
    relinked = await api_client.post(
        f"/api/budgets/{created['id']}/link-protocol",
        json={"protocol_id": str(protocol_b.id)},
        headers=_as(seeded.user),
    )
    assert relinked.status_code == 200, relinked.text
    body = relinked.json()
    new_pk_row = _find_line_item(body, "PK sampling draws")
    assert new_pk_row["quantity"] == 300.0  # 10 timepoints * 30 enrolled
    assert new_pk_row["unit_cost"] == 38.0
    assert new_pk_row["total"] == 10944.0  # untouched — the user's own figure


async def test_relinking_removes_untouched_derived_row_whose_key_disappears(api_client, seeded):
    protocol_with_ada = await _make_protocol(
        seeded.db, seeded.drug, seeded.user, seeded.user.org_id,
        population_config=_derivable_config(n_enrolled=24, n_pk=12, n_ada=5, days=(1, 2, 3)),
    )
    created = await _create_budget(api_client, seeded.user, protocol_id=str(protocol_with_ada.id))
    assert _find_line_item(created, "Immunogenicity/ADA sampling draws") is not None

    protocol_no_ada = await _make_protocol(
        seeded.db, seeded.drug, seeded.user, seeded.user.org_id,
        population_config=_derivable_config(n_enrolled=24, n_pk=12, n_ada=0, days=(1, 2, 3)),
    )
    relinked = await api_client.post(
        f"/api/budgets/{created['id']}/link-protocol",
        json={"protocol_id": str(protocol_no_ada.id)},
        headers=_as(seeded.user),
    )
    body = relinked.json()
    assert _find_line_item(body, "Immunogenicity/ADA sampling draws") is None


async def test_relinking_keeps_a_priced_derived_row_whose_key_disappears_but_flips_to_manual(api_client, seeded):
    protocol_with_ada = await _make_protocol(
        seeded.db, seeded.drug, seeded.user, seeded.user.org_id,
        population_config=_derivable_config(n_enrolled=24, n_pk=12, n_ada=5, days=(1, 2, 3)),
    )
    created = await _create_budget(api_client, seeded.user, protocol_id=str(protocol_with_ada.id))
    ada_row = _find_line_item(created, "Immunogenicity/ADA sampling draws")

    priced = await api_client.patch(
        f"/api/budgets/{created['id']}/line-items/{ada_row['id']}",
        json={"unit_cost": 50, "total": 6000},
        headers=_as(seeded.user),
    )
    assert priced.status_code == 200, priced.text

    protocol_no_ada = await _make_protocol(
        seeded.db, seeded.drug, seeded.user, seeded.user.org_id,
        population_config=_derivable_config(n_enrolled=24, n_pk=12, n_ada=0, days=(1, 2, 3)),
    )
    relinked = await api_client.post(
        f"/api/budgets/{created['id']}/link-protocol",
        json={"protocol_id": str(protocol_no_ada.id)},
        headers=_as(seeded.user),
    )
    body = relinked.json()
    surviving = _find_line_item(body, "Immunogenicity/ADA sampling draws")
    assert surviving is not None, "a priced derived row must be kept, not deleted"
    assert surviving["source"] == "manual"
    assert surviving["derivation_key"] is None
    assert surviving["total"] == 6000.0  # user pricing untouched
    assert surviving["note"]  # explanatory note recorded


async def test_unlink_protocol_keeps_line_items_and_records_unlinked_at(api_client, seeded):
    protocol = await _make_protocol(
        seeded.db, seeded.drug, seeded.user, seeded.user.org_id,
        population_config=_derivable_config(),
    )
    created = await _create_budget(api_client, seeded.user, protocol_id=str(protocol.id))
    assert created["protocol_id"] == str(protocol.id)
    line_item_count_before = sum(len(c["line_items"]) for c in created["categories"])
    assert line_item_count_before > 0

    unlinked = await api_client.post(
        f"/api/budgets/{created['id']}/link-protocol",
        json={"protocol_id": None},
        headers=_as(seeded.user),
    )
    assert unlinked.status_code == 200, unlinked.text
    body = unlinked.json()
    assert body["protocol_id"] is None
    assert sum(len(c["line_items"]) for c in body["categories"]) == line_item_count_before
    assert "unlinked_at" in body["derivation"]


# ===========================================================================
# G. Org isolation / auth
# ===========================================================================


async def test_cross_org_access_is_forbidden_on_every_route(api_client, seeded):
    outsider = await _make_outsider(seeded.db)
    created = await _create_budget(api_client, seeded.user)
    budget_id = created["id"]
    category_id = created["categories"][0]["id"]
    li = await _create_line_item(api_client, seeded.user, budget_id, category_id)

    assert (await api_client.get(f"/api/budgets/{budget_id}", headers=_as(outsider))).status_code == 403
    assert (
        await api_client.patch(f"/api/budgets/{budget_id}", json={"name": "x"}, headers=_as(outsider))
    ).status_code == 403
    assert (await api_client.delete(f"/api/budgets/{budget_id}", headers=_as(outsider))).status_code == 403
    assert (await api_client.get(f"/api/budgets/{budget_id}/rollup", headers=_as(outsider))).status_code == 403
    assert (
        await api_client.post(
            f"/api/budgets/{budget_id}/link-protocol", json={"protocol_id": None}, headers=_as(outsider),
        )
    ).status_code == 403
    assert (
        await api_client.post(
            f"/api/budgets/{budget_id}/categories", json={"name": "x"}, headers=_as(outsider),
        )
    ).status_code == 403
    assert (
        await api_client.patch(
            f"/api/budgets/{budget_id}/categories/{category_id}", json={"name": "x"}, headers=_as(outsider),
        )
    ).status_code == 403
    assert (
        await api_client.delete(
            f"/api/budgets/{budget_id}/categories/{category_id}", headers=_as(outsider),
        )
    ).status_code == 403
    assert (
        await api_client.post(
            f"/api/budgets/{budget_id}/categories/{category_id}/line-items",
            json={"label": "x"}, headers=_as(outsider),
        )
    ).status_code == 403
    assert (
        await api_client.patch(
            f"/api/budgets/{budget_id}/line-items/{li['id']}", json={"label": "x"}, headers=_as(outsider),
        )
    ).status_code == 403
    assert (
        await api_client.delete(
            f"/api/budgets/{budget_id}/line-items/{li['id']}", headers=_as(outsider),
        )
    ).status_code == 403
    assert (
        await api_client.post(
            f"/api/budgets/{budget_id}/categories/reorder",
            json={"ordered_ids": [c["id"] for c in created["categories"]]},
            headers=_as(outsider),
        )
    ).status_code == 403
    assert (
        await api_client.post(
            f"/api/budgets/{budget_id}/categories/{category_id}/line-items/reorder",
            json={"ordered_ids": [li["id"]]},
            headers=_as(outsider),
        )
    ).status_code == 403


async def test_list_budgets_does_not_leak_across_orgs(api_client, seeded):
    outsider = await _make_outsider(seeded.db)
    created = await _create_budget(api_client, seeded.user, name="Org A Only")

    listing = await api_client.get("/api/budgets", headers=_as(outsider))
    assert listing.status_code == 200, listing.text
    assert created["id"] not in {r["id"] for r in listing.json()}


async def test_create_budget_with_other_orgs_protocol_is_forbidden_and_creates_nothing(api_client, seeded):
    outsider = await _make_outsider(seeded.db)
    other_protocol = await _make_protocol(seeded.db, seeded.drug, outsider, outsider.org_id)

    resp = await api_client.post(
        "/api/budgets",
        json={"name": "Sneaky Budget", "protocol_id": str(other_protocol.id)},
        headers=_as(seeded.user),
    )
    assert resp.status_code == 403, resp.text

    listing = await api_client.get("/api/budgets", headers=_as(seeded.user))
    assert "Sneaky Budget" not in {r["name"] for r in listing.json()}


async def test_link_protocol_from_another_org_is_forbidden(api_client, seeded):
    outsider = await _make_outsider(seeded.db)
    other_protocol = await _make_protocol(seeded.db, seeded.drug, outsider, outsider.org_id)
    created = await _create_budget(api_client, seeded.user)

    resp = await api_client.post(
        f"/api/budgets/{created['id']}/link-protocol",
        json={"protocol_id": str(other_protocol.id)},
        headers=_as(seeded.user),
    )
    assert resp.status_code == 403, resp.text


async def test_unknown_uuid_is_404_for_budget_category_and_line_item(api_client, seeded):
    created = await _create_budget(api_client, seeded.user)
    random_id = "00000000-0000-0000-0000-000000000000"

    assert (await api_client.get(f"/api/budgets/{random_id}", headers=_as(seeded.user))).status_code == 404
    assert (
        await api_client.patch(
            f"/api/budgets/{created['id']}/categories/{random_id}", json={"name": "x"}, headers=_as(seeded.user),
        )
    ).status_code == 404
    assert (
        await api_client.patch(
            f"/api/budgets/{created['id']}/line-items/{random_id}", json={"label": "x"}, headers=_as(seeded.user),
        )
    ).status_code == 404


async def test_category_from_a_different_budget_is_404_through_this_budget(api_client, seeded):
    budget_a = await _create_budget(api_client, seeded.user)
    budget_b = await _create_budget(api_client, seeded.user)
    cat_of_a = await _create_category(api_client, seeded.user, budget_a["id"])

    resp = await api_client.patch(
        f"/api/budgets/{budget_b['id']}/categories/{cat_of_a['id']}",
        json={"name": "hijacked"},
        headers=_as(seeded.user),
    )
    assert resp.status_code == 404, resp.text


async def test_requests_without_x_user_id_are_401(api_client, seeded):
    created = await _create_budget(api_client, seeded.user)
    resp = await api_client.get(f"/api/budgets/{created['id']}")
    assert resp.status_code == 401, resp.text


async def test_client_supplied_org_id_in_body_is_ignored(api_client, seeded):
    resp = await api_client.post(
        "/api/budgets",
        json={"name": "Spoofed org", "org_id": "somebody-elses-org"},
        headers=_as(seeded.user),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["org_id"] == seeded.user.org_id


# ===========================================================================
# H. Client cannot forge provenance
# ===========================================================================


async def test_create_line_item_cannot_forge_protocol_derived_source(api_client, seeded):
    created = await _create_budget(api_client, seeded.user)
    cat_id = created["categories"][0]["id"]
    resp = await api_client.post(
        f"/api/budgets/{created['id']}/categories/{cat_id}/line-items",
        json={"label": "Sneaky", "source": "protocol_derived", "derivation_key": "pk_draws"},
        headers=_as(seeded.user),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["source"] == "manual"
    assert body["derivation_key"] is None


async def test_patch_cannot_change_source_or_derivation_key(api_client, seeded):
    protocol = await _make_protocol(
        seeded.db, seeded.drug, seeded.user, seeded.user.org_id, population_config=_derivable_config(),
    )
    created = await _create_budget(api_client, seeded.user, protocol_id=str(protocol.id))
    pk_row = _find_line_item(created, "PK sampling draws")

    resp = await api_client.patch(
        f"/api/budgets/{created['id']}/line-items/{pk_row['id']}",
        json={"source": "manual", "derivation_key": "hacked"},
        headers=_as(seeded.user),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "protocol_derived"
    assert body["derivation_key"] == "pk_draws"


# ===========================================================================
# I. Reorder
# ===========================================================================


async def test_reorder_categories(api_client, seeded):
    created = await _create_budget(api_client, seeded.user)
    ids = [c["id"] for c in created["categories"]]
    shuffled = list(reversed(ids))

    resp = await api_client.post(
        f"/api/budgets/{created['id']}/categories/reorder",
        json={"ordered_ids": shuffled},
        headers=_as(seeded.user),
    )
    assert resp.status_code == 200, resp.text

    got = await api_client.get(f"/api/budgets/{created['id']}", headers=_as(seeded.user))
    body = got.json()
    assert [c["id"] for c in body["categories"]] == shuffled
    assert [c["sort_order"] for c in body["categories"]] == list(range(1, len(shuffled) + 1))


async def test_reorder_categories_rejects_incomplete_list(api_client, seeded):
    created = await _create_budget(api_client, seeded.user)
    ids = [c["id"] for c in created["categories"]]

    resp = await api_client.post(
        f"/api/budgets/{created['id']}/categories/reorder",
        json={"ordered_ids": ids[:-1]},  # missing one
        headers=_as(seeded.user),
    )
    assert resp.status_code == 422, resp.text

    resp2 = await api_client.post(
        f"/api/budgets/{created['id']}/categories/reorder",
        json={"ordered_ids": ids + [ids[0]]},  # duplicate
        headers=_as(seeded.user),
    )
    assert resp2.status_code == 422, resp2.text

    # Nothing changed.
    got = await api_client.get(f"/api/budgets/{created['id']}", headers=_as(seeded.user))
    assert [c["id"] for c in got.json()["categories"]] == ids


async def test_reorder_line_items(api_client, seeded):
    created = await _create_budget(api_client, seeded.user)
    cat = await _create_category(api_client, seeded.user, created["id"])
    li1 = await _create_line_item(api_client, seeded.user, created["id"], cat["id"], label="A")
    li2 = await _create_line_item(api_client, seeded.user, created["id"], cat["id"], label="B")
    li3 = await _create_line_item(api_client, seeded.user, created["id"], cat["id"], label="C")
    shuffled = [li3["id"], li1["id"], li2["id"]]

    resp = await api_client.post(
        f"/api/budgets/{created['id']}/categories/{cat['id']}/line-items/reorder",
        json={"ordered_ids": shuffled},
        headers=_as(seeded.user),
    )
    assert resp.status_code == 200, resp.text

    got = await api_client.get(f"/api/budgets/{created['id']}", headers=_as(seeded.user))
    body_cat = next(c for c in got.json()["categories"] if c["id"] == cat["id"])
    assert [li["id"] for li in body_cat["line_items"]] == shuffled
    assert [li["sort_order"] for li in body_cat["line_items"]] == [1, 2, 3]


async def test_reorder_line_items_rejects_partial_list(api_client, seeded):
    created = await _create_budget(api_client, seeded.user)
    cat = await _create_category(api_client, seeded.user, created["id"])
    li1 = await _create_line_item(api_client, seeded.user, created["id"], cat["id"], label="A")
    await _create_line_item(api_client, seeded.user, created["id"], cat["id"], label="B")

    resp = await api_client.post(
        f"/api/budgets/{created['id']}/categories/{cat['id']}/line-items/reorder",
        json={"ordered_ids": [li1["id"]]},
        headers=_as(seeded.user),
    )
    assert resp.status_code == 422, resp.text


# ===========================================================================
# J. Audit trail
# ===========================================================================


async def test_audit_trail_records_create_and_delete(api_client, seeded):
    import asyncio
    from models import AuditLog

    created = await _create_budget(api_client, seeded.user)
    await asyncio.sleep(0)  # let audit_logger's post-response write commit

    create_logs = (
        await seeded.db.execute(select(AuditLog).where(AuditLog.action == "POST:budget"))
    ).scalars().all()
    assert any(
        log.resource_type == "budget" and log.resource_id == created["id"] for log in create_logs
    )

    await api_client.delete(f"/api/budgets/{created['id']}", headers=_as(seeded.user))
    await asyncio.sleep(0)

    delete_logs = (
        await seeded.db.execute(select(AuditLog).where(AuditLog.action == "DELETE:budget"))
    ).scalars().all()
    assert any(
        log.resource_type == "budget" and log.resource_id == created["id"] for log in delete_logs
    )


# ===========================================================================
# K. updated_at freshness
# ===========================================================================


async def test_child_mutation_advances_budget_updated_at(api_client, seeded):
    created = await _create_budget(api_client, seeded.user)
    before_updated_at = created["updated_at"]
    cat_id = created["categories"][0]["id"]

    await _create_line_item(api_client, seeded.user, created["id"], cat_id)

    refetched = await api_client.get(f"/api/budgets/{created['id']}", headers=_as(seeded.user))
    assert refetched.json()["updated_at"] != before_updated_at


# ===========================================================================
# L. Pure-unit tests for services/budget_engine.py (no DB, fast)
# ===========================================================================


class _FakeLineItem:
    def __init__(self, *, total=None, status="costed"):
        self.total = total
        self.status = status


class _FakeCategory:
    def __init__(self, *, bucket="direct", line_items=None):
        self.id = uuid.uuid4()
        self.bucket = bucket
        self.line_items = line_items or []


def test_default_categories_length_and_bucket_composition():
    from models import BudgetBucket

    cats = budget_engine.DEFAULT_CATEGORIES  # list[tuple[name, BudgetBucket]]
    assert len(cats) == 17
    buckets = [bucket for _name, bucket in cats]
    assert buckets.count(BudgetBucket.direct) == 14
    assert buckets.count(BudgetBucket.stipend) == 1
    assert buckets.count(BudgetBucket.variable) == 1
    assert buckets.count(BudgetBucket.pass_through) == 1


def test_category_subtotal_excludes_non_costed_and_null_totals():
    cat = _FakeCategory(
        line_items=[
            _FakeLineItem(total=100, status="costed"),
            _FakeLineItem(total=9999, status="out_of_scope"),
            _FakeLineItem(total=None, status="costed"),
            _FakeLineItem(total=200, status="costed"),
        ]
    )
    assert budget_engine.category_subtotal(cat) == 300.0


def test_compute_rollups_percent_discount():
    from models import BudgetDiscountType

    categories = [
        _FakeCategory(bucket="direct", line_items=[_FakeLineItem(total=1000)]),
        _FakeCategory(bucket="stipend", line_items=[_FakeLineItem(total=200)]),
    ]
    rollup = budget_engine.compute_rollups(
        categories, discount_type=BudgetDiscountType.percent, discount_value=10,
    )
    assert rollup["direct_subtotal"] == 1000.0
    assert rollup["discount_amount"] == 100.0
    assert rollup["total_direct_costs"] == 900.0
    assert rollup["grand_total"] == 1100.0


def test_compute_rollups_flat_discount_can_exceed_direct_subtotal_and_go_negative():
    from models import BudgetDiscountType

    categories = [_FakeCategory(bucket="direct", line_items=[_FakeLineItem(total=100)])]
    rollup = budget_engine.compute_rollups(
        categories, discount_type=BudgetDiscountType.flat, discount_value=500,
    )
    # A flat discount greater than the direct subtotal is NOT clamped to zero.
    assert rollup["total_direct_costs"] == -400.0
    assert rollup["grand_total"] == -400.0


def test_compute_rollups_none_discount_applies_zero():
    from models import BudgetDiscountType

    categories = [_FakeCategory(bucket="direct", line_items=[_FakeLineItem(total=500)])]
    rollup = budget_engine.compute_rollups(
        categories, discount_type=BudgetDiscountType.none, discount_value=0,
    )
    assert rollup["discount_amount"] == 0.0
    assert rollup["total_direct_costs"] == 500.0


# ===========================================================================
# M. Patient / oncology rate-card profile (PROFILE SELECTION AND MODEL
# CHANGES / THE PATIENT RATE CARD / QUANTITY BASES / PROVENANCE DESIGN / API
# CONTRACT). Sections A-L above are ALL still exercising the healthy-volunteer
# path; this section adds explicit "still healthy, still 17 categories, new
# buckets stay at 0.00" assertions rather than rewriting them, plus the new
# patient-profile coverage.
# ===========================================================================


async def test_healthy_volunteer_budget_still_prices_exactly_as_before(api_client, seeded):
    """Hard regression: a healthy-volunteer budget (a drug NOT in the
    cytotoxic/biologic-unsafe curated list) must resolve subject_profile to
    'healthy' and price with the untouched 17-category healthy card — the
    two new rollup buckets never move for it."""
    body = await _create_budget(api_client, seeded.user, name=None, drug_inn="metformin")
    assert body["subject_profile"] == "healthy"
    assert len(body["categories"]) == 17
    assert body["rollup"]["investigator_grants_total"] == 0.0
    assert body["rollup"]["software_total"] == 0.0
    buckets = {c["bucket"] for c in body["categories"]}
    assert "investigator_grants" not in buckets
    assert "software" not in buckets


async def test_biologic_kb_safe_drug_still_resolves_healthy(api_client, seeded):
    """abatacept is in the curated safety KB as NHV-safe — the drug-KB
    inference (spec §1.2 step 3) must resolve it to 'healthy', not
    'patient', even though it is a biologic. The pre-existing reference
    numbers (test_biologic_drug_name_defaults_to_biosimilar_pk_profile) are
    unaffected by this work — spot-checked again here."""
    body = await _create_budget(api_client, seeded.user, name=None, drug_inn="abatacept")
    assert body["subject_profile"] == "healthy"
    assert body["study_type"] == "biosimilar_pk"
    assert len(body["categories"]) == 17
    assert body["n_enrolled"] == 60


async def test_cytotoxic_drug_name_alone_infers_patient_profile(api_client, seeded):
    """The spec's headline scenario: the user types 'nivolumab' and nothing
    else -> a fully-priced 22-category patient/oncology budget, no protocol,
    no manual pricing step."""
    body = await _create_budget(api_client, seeded.user, name=None, drug_inn="nivolumab")

    assert body["subject_profile"] == "patient"
    assert body["derivation"]["assumptions"]["subject_profile"]["source"] == "inferred_from_safety_profile"
    assert body["study_type"] == "biosimilar_pk"
    assert body["therapeutic_area"] == "oncology"
    assert body["n_enrolled"] == 128
    assert body["n_sites"] == 40
    assert body["n_countries"] == 7
    assert body["geography_profile"] == "global_emerging_mixed"
    assert body["bicr_required"] is True
    assert body["comparator_sourced_by_sponsor"] is False

    assert len(body["categories"]) == 22
    line_item_count = sum(len(c["line_items"]) for c in body["categories"])
    assert line_item_count > 100

    # Every one of the 22 categories actually got priced (spec's "every
    # category populated" requirement) — no empty shell for any of them.
    for cat in body["categories"]:
        assert len(cat["line_items"]) > 0, f"category '{cat['name']}' has no line items"

    for cat in body["categories"]:
        for li in cat["line_items"]:
            if li["status"] == "costed":
                assert li["unit_cost"] is not None
                assert li["total"] is not None

    # The rollup reconciles: the two new buckets are populated, and the
    # comparator drug (opt-in, default OFF) contributes nothing by default.
    rollup = body["rollup"]
    assert rollup["investigator_grants_total"] > 0.0
    assert rollup["software_total"] > 0.0
    assert rollup["pass_through_total"] > 0.0
    assert rollup["grand_total"] == pytest.approx(
        rollup["total_direct_costs"] + rollup["subject_stipend_total"] + rollup["variable_total"]
        + rollup["pass_through_total"] + rollup["investigator_grants_total"] + rollup["software_total"],
        abs=0.01,
    )
    comparator_cat = next(c for c in body["categories"] if c["name"] == "Comparator Drug Sourcing")
    assert comparator_cat["subtotal"] == 0.0

    # Derivation audit trail (spec §4.4/§7.5).
    deriv = body["derivation"]
    assert deriv["subject_profile"] == "patient"
    assert deriv["profile_key"] == "biosimilar_pk_oncology"
    assert deriv["phases"]["total_months"] == body["project_duration_months"]
    assert len(deriv["sources"]) > 0
    assert "provenance_summary" in deriv
    assert "bid_reconciliation" in deriv
    assert "cross_checks" in deriv
    researched = [ln for ln in deriv["lines"] if ln["source"] == "researched"]
    assert researched, "expected at least one researched (cited) line on the patient card"
    for ln in researched:
        assert ln["source_id"] in deriv["sources"]


async def test_patient_budget_user_can_override_subject_profile_explicitly(api_client, seeded):
    """User-authoritative override (spec §1.2 step 1) wins over everything,
    including a drug the KB would otherwise leave healthy."""
    body = await _create_budget(
        api_client, seeded.user, name=None, drug_inn="metformin", subject_profile="patient",
    )
    assert body["subject_profile"] == "patient"
    assert len(body["categories"]) == 22


async def test_invalid_subject_profile_is_422(api_client, seeded):
    resp = await api_client.post(
        "/api/budgets", json={"drug_inn": "metformin", "subject_profile": "made_up"},
        headers=_as(seeded.user),
    )
    assert resp.status_code == 422, resp.text


async def test_invalid_geography_profile_is_422(api_client, seeded):
    resp = await api_client.post(
        "/api/budgets", json={"drug_inn": "nivolumab", "geography_profile": "mars"},
        headers=_as(seeded.user),
    )
    assert resp.status_code == 422, resp.text


async def test_dropout_rate_out_of_range_is_422(api_client, seeded):
    resp = await api_client.post(
        "/api/budgets", json={"drug_inn": "nivolumab", "dropout_rate_pct": 150},
        headers=_as(seeded.user),
    )
    assert resp.status_code == 422, resp.text


async def test_comparator_gate_defaults_off_and_can_be_opted_in(api_client, seeded):
    """spec §1.6 — a sponsor-sourced reference product can be a $10M+ line,
    so it must be a deliberate opt-in, never a silent default."""
    off_body = await _create_budget(api_client, seeded.user, name=None, drug_inn="nivolumab")
    comp_cat = next(c for c in off_body["categories"] if c["name"] == "Comparator Drug Sourcing")
    comp_drug = next(li for li in comp_cat["line_items"] if li["derivation_key"] == "comp.drug")
    assert comp_drug["status"] == "actuals_only"
    assert comp_drug["quantity"] == 0
    assert comp_drug["total"] is None
    assert comp_drug["unit_cost"] is not None  # shown, just not summed

    on_body = await _create_budget(
        api_client, seeded.user, name=None, drug_inn="nivolumab", comparator_sourced_by_sponsor=True,
    )
    on_comp_cat = next(c for c in on_body["categories"] if c["name"] == "Comparator Drug Sourcing")
    on_comp_drug = next(li for li in on_comp_cat["line_items"] if li["derivation_key"] == "comp.drug")
    assert on_comp_drug["status"] == "costed"
    assert on_comp_drug["quantity"] > 0
    assert on_comp_drug["total"] is not None
    assert on_body["rollup"]["grand_total"] > off_body["rollup"]["grand_total"]


async def test_reprice_endpoint_reprices_after_driver_change(api_client, seeded):
    """spec §7.4 — a plain PATCH does not move any total by itself; the new
    /reprice endpoint is how a caller asks for that re-price explicitly."""
    body = await _create_budget(api_client, seeded.user, name=None, drug_inn="nivolumab")
    original_total = body["rollup"]["grand_total"]

    patch_resp = await api_client.patch(
        f"/api/budgets/{body['id']}", json={"n_sites": 80}, headers=_as(seeded.user),
    )
    assert patch_resp.status_code == 200, patch_resp.text
    patched = patch_resp.json()
    assert patched["n_sites"] == 80
    assert patched["rollup"]["grand_total"] == pytest.approx(original_total, abs=0.01)

    reprice_resp = await api_client.post(
        f"/api/budgets/{body['id']}/reprice", headers=_as(seeded.user),
    )
    assert reprice_resp.status_code == 200, reprice_resp.text
    repriced = reprice_resp.json()
    assert repriced["n_sites"] == 80
    assert repriced["rollup"]["grand_total"] != pytest.approx(original_total, abs=0.01)
    assert repriced["rollup"]["grand_total"] > 0.0


async def test_profile_preview_endpoint(api_client, seeded):
    resp = await api_client.get(
        "/api/budgets/profile-preview", params={"drug_inn": "nivolumab"}, headers=_as(seeded.user),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["subject_profile"] == "patient"
    assert body["subject_profile_source"] == "inferred_from_safety_profile"
    assert body["therapeutic_area"] == "oncology"
    assert body["category_count"] == 22
    assert body["estimated_grand_total"] > 0.0
    assert body["defaults"]["n_sites"] == 40

    healthy_resp = await api_client.get(
        "/api/budgets/profile-preview", params={"drug_inn": "metformin"}, headers=_as(seeded.user),
    )
    assert healthy_resp.status_code == 200, healthy_resp.text
    healthy_body = healthy_resp.json()
    assert healthy_body["subject_profile"] == "healthy"
    assert healthy_body["category_count"] == 17


async def test_rate_card_meta_endpoint(api_client, seeded):
    patient_resp = await api_client.get(
        "/api/budgets/rate-card-meta", params={"subject_profile": "patient"}, headers=_as(seeded.user),
    )
    assert patient_resp.status_code == 200, patient_resp.text
    patient_meta = patient_resp.json()
    assert len(patient_meta["categories"]) == 22
    assert "investigator_grants" in patient_meta["buckets"]
    assert "software" in patient_meta["buckets"]
    assert patient_meta["geography_profiles"]["global_emerging_mixed"] == pytest.approx(0.45)

    healthy_resp = await api_client.get(
        "/api/budgets/rate-card-meta", params={"subject_profile": "healthy"}, headers=_as(seeded.user),
    )
    assert healthy_resp.status_code == 200, healthy_resp.text
    healthy_meta = healthy_resp.json()
    assert len(healthy_meta["categories"]) == 17
    assert "investigator_grants" not in healthy_meta["buckets"]


async def test_budget_summary_list_carries_patient_fields(api_client, seeded):
    await _create_budget(api_client, seeded.user, name=None, drug_inn="nivolumab")
    resp = await api_client.get("/api/budgets", headers=_as(seeded.user))
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    row = next(r for r in rows if r["drug_inn"] == "nivolumab")
    assert row["subject_profile"] == "patient"
    assert row["therapeutic_area"] == "oncology"
    assert row["n_sites"] == 40
    assert row["n_countries"] == 7


# ---------------------------------------------------------------------------
# M2. Pure-unit tests (no DB) — budget_engine.py's new resolvers
# ---------------------------------------------------------------------------


def _bare_budget(**overrides):
    from models import Budget, BudgetDiscountType

    defaults = dict(
        id=uuid.uuid4(), org_id="test-org", name="t", created_by=uuid.uuid4(),
        currency="USD", derivation={}, discount_type=BudgetDiscountType.none,
        discount_value=0.0, comparator_sourced_by_sponsor=False,
    )
    defaults.update(overrides)
    return Budget(**defaults)


def test_resolve_subject_profile_user_wins_over_everything():
    from models import BudgetSubjectProfile

    budget = _bare_budget(drug_inn="nivolumab", subject_profile=BudgetSubjectProfile.healthy)
    profile, assumption = budget_engine.resolve_subject_profile(budget, None, {})
    assert profile == "healthy"
    assert assumption["source"] == "user"


def test_resolve_subject_profile_drug_kb_infers_patient_for_cytotoxic():
    budget = _bare_budget(drug_inn="nivolumab")
    profile, assumption = budget_engine.resolve_subject_profile(budget, None, {})
    assert profile == "patient"
    assert assumption["source"] == "inferred_from_safety_profile"


def test_resolve_subject_profile_drug_kb_stays_healthy_for_nhv_safe_biologic():
    budget = _bare_budget(drug_inn="trastuzumab")
    profile, assumption = budget_engine.resolve_subject_profile(budget, None, {})
    assert profile == "healthy"
    assert assumption["source"] == "inferred_from_safety_profile"


def test_resolve_subject_profile_unknown_drug_defaults_healthy():
    budget = _bare_budget(drug_inn="some-made-up-molecule-xyz")
    profile, assumption = budget_engine.resolve_subject_profile(budget, None, {})
    assert profile == "healthy"
    assert assumption["source"] == "default"


def test_compute_rollups_investigator_grants_and_software_buckets():
    categories = [
        _FakeCategory(bucket="direct", line_items=[_FakeLineItem(total=1000)]),
        _FakeCategory(bucket="investigator_grants", line_items=[_FakeLineItem(total=500)]),
        _FakeCategory(bucket="software", line_items=[_FakeLineItem(total=250)]),
    ]
    rollup = budget_engine.compute_rollups(categories)
    assert rollup["investigator_grants_total"] == 500.0
    assert rollup["software_total"] == 250.0
    assert rollup["grand_total"] == 1750.0


def test_patient_rate_card_seed_categories_match_patient_rate_card():
    from services import budget_rate_card

    cats = budget_engine.seed_default_categories(uuid.uuid4(), subject_profile="patient")
    assert len(cats) == 22
    assert [c.name for c in cats] == [c.name for c in budget_rate_card.PATIENT_RATE_CARD]


def test_healthy_seed_categories_unaffected_by_default_argument():
    cats_default = budget_engine.seed_default_categories(uuid.uuid4())
    cats_explicit = budget_engine.seed_default_categories(uuid.uuid4(), subject_profile="healthy")
    assert [c.name for c in cats_default] == [c.name for c in cats_explicit]
    assert len(cats_default) == 17
