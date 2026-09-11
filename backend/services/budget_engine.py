"""Deterministic budget engine — seeds the category template, auto-populates
every category with a fully priced rate-card estimate, derives real
cost-driver QUANTITIES from a linked protocol when one exists, and computes
the rollup totals.

ESTIMATION RULE (explicitly directed by the product owner, superseding the
earlier "never fill a price" rule for THIS tool): every line item is created
fully priced from a rate card (services/budget_rate_card.py) calibrated
against real signed CRO budgets. Nothing is left blank. In exchange,
Budget.derivation records, for every single assumption and every single line,
whether the number came from a linked protocol, from a value the user
supplied, or from the rate card's per-study-type default — so an
approximation is always traceable, never silently invented. Discount is the
one exception: it is a negotiation outcome and is never estimated.
"""
from __future__ import annotations

import dataclasses
import math
import uuid
from datetime import datetime, timezone
from typing import Any, Sequence

from models import (
    Budget,
    BudgetBucket,
    BudgetCategory,
    BudgetDiscountType,
    BudgetLineItem,
    BudgetLineSource,
    BudgetLineStatus,
    BudgetSubjectProfile,
    Protocol,
)
from services import budget_rate_card
from services.budget_rate_card import (
    COMPARATOR_AGENT_FEE_PCT,
    CONDUCT_ADA_DRAW_RATE,
    CONDUCT_ALTERNATE_RATIO,
    CONDUCT_PK_DRAW_RATE,
    CONDUCT_SCREEN_FAILURE_RATIO,
    CONDUCT_TARGET_PER_ENROLLED,
    CONDUCT_VISIT_DAY_RATE,
    DEFAULT_ADA_TIMEPOINTS,
    DEFAULT_PK_TIMEPOINTS,
    DEFAULT_STUDY_PROFILES,
    DEFAULT_VISIT_DAYS,
    GEOGRAPHY_FACTORS,
    PATIENT_DISCLAIMER,
    PATIENT_RATE_CARD,
    PATIENT_RATE_CARD_VERSION,
    PATIENT_STUDY_PROFILES,
    PROVENANCE_TIERS,
    RATE_CARD,
    RATE_CARD_VERSION,
    SITE_MONTH_RAMP,
    SOURCE_REGISTRY,
    StudyDrivers,
    quantity_for,
    should_include,
    unit_cost_for,
)
from services.budget_trial_precedent import BudgetPrecedentResult, NO_PRECEDENT
from services.design_decisions import (
    classify_drug_modality,
    classify_therapeutic_area,
    requires_patient_population,
)

# Keys in PATIENT_RATE_CARD's "Comparator Drug Sourcing" category whose STATUS
# is gated at runtime by Budget.comparator_sourced_by_sponsor (spec §1.6),
# rather than being a static RateCardLine.status. Both default to
# actuals_only in the rate card itself (the off-by-default state); the engine
# flips them to costed only when the sponsor has explicitly opted in.
COMPARATOR_GATED_KEYS = frozenset({"comp.drug", "comp.agent_fee"})

# ---------------------------------------------------------------------------
# Patient-profile constants that live here (not in budget_rate_card.py) because
# they are engine-level DRIVER RESOLUTION policy, not pricing data.
# ---------------------------------------------------------------------------

MONITORING_REMOTE_FRACTION = 0.20   # src.alcoa_crio_2025 on-site/remote mix, §2.2 C4
RECRUITMENT_RATE_BAND = (0.2, 1.5)  # patients/site/month sanity band, §3.6

# The derived rows' home category. The ONLY category the engine looks up by
# name — if a user renames or deletes it, auto_populate_budget recreates it
# (and says so in derivation["warnings"]) rather than silently dropping the
# derived rows into the wrong section.
CLINICAL_CONDUCT_NAME = "Clinical Conduct"

# The 17-row seed template. A TEMPLATE, not a hardcoded enum: every row is
# renameable, re-bucketable, reorderable and deletable afterwards, and users
# may add their own. Seeded with sort_order 1..17 (1-based) so a later
# "insert at top" can use 0 without a renumber. Sourced from
# budget_rate_card.RATE_CARD so the category list has a single source of
# truth shared with the pricing engine.
DEFAULT_CATEGORIES: list[tuple[str, BudgetBucket]] = budget_rate_card.default_categories()

# The patient card's 22-category seed template. Built here (not in
# budget_rate_card.default_categories(), which is healthy-card-only and
# untouched by this work) so a patient budget seeds its OWN category set
# rather than the 17 healthy-card names — the two card's category names are
# almost entirely disjoint, so seeding the wrong set would leave 17 stray
# empty categories sitting next to the 22 real patient ones.
PATIENT_DEFAULT_CATEGORIES: list[tuple[str, BudgetBucket]] = [
    (c.name, c.bucket) for c in PATIENT_RATE_CARD
]


def seed_default_categories(
    budget_id: uuid.UUID, subject_profile: str = "healthy",
) -> list[BudgetCategory]:
    """Build the default BudgetCategory rows for a new budget: the 17-row
    healthy-volunteer template, or (subject_profile="patient") the 22-row
    patient template. Returns detached ORM objects; the caller db.add_all()s
    them (or appends to budget.categories, as routers/budgets.py does).
    """
    template = PATIENT_DEFAULT_CATEGORIES if subject_profile == "patient" else DEFAULT_CATEGORIES
    return [
        BudgetCategory(id=uuid.uuid4(), budget_id=budget_id, name=name,
                       bucket=bucket, sort_order=i)
        for i, (name, bucket) in enumerate(template, start=1)
    ]


def _visit_days(schedule: dict) -> tuple[int, list[str]]:
    """Count of DISTINCT study days on which the subject must be at the site.

    Returns (n_distinct_days, warnings).
    """
    days: set[int] = set()
    warnings: list[str] = []
    undated = 0

    for key in (
        "timepoints",
        "timepoints_steady_state",
        "steady_state_troughs",
        "immunogenicity_timepoints",
    ):
        entries = schedule.get(key) or []
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            d = entry.get("day")
            if isinstance(d, (int, float)) and not isinstance(d, bool):
                days.add(int(d))
                continue
            h = entry.get("hours")
            if isinstance(h, (int, float)) and not isinstance(h, bool):
                days.add(int(h // 24) + 1)  # matches sampling_schedule._day_number
            else:
                undated += 1

    if undated:
        warnings.append(
            f"{undated} scheduled draw(s) in this protocol carry no day or hour "
            "and were not counted as visit days."
        )

    return len(days), warnings


def build_clinical_conduct_rows(
    drivers: StudyDrivers,
    protocol_linked: bool,
    *,
    pk_from_protocol: bool = False,
    ada_from_protocol: bool = False,
    visit_days_from_protocol: bool = False,
) -> tuple[list[dict], dict]:
    """Pure function. Returns (rows, extra) where `rows` are plain dicts ready
    to become BudgetLineItem kwargs and `extra` carries the "conduct" block
    plus any warnings this construction produced.

    Every row is fully priced. Quantities come from the linked protocol's real
    computed schedule when one exists (source=protocol_derived) and from the
    per-study-type defaults otherwise (source=rate_card); either way the unit
    cost is always a rate-card approximation.

    NOTE: whether a row counts as protocol_derived is judged per-row, from
    whether ITS OWN driver (pk/ada timepoints, visit days) actually came from
    the linked protocol's computed schedule — NOT merely from whether a
    protocol is linked at all. A linked protocol with no computed sample size
    still falls back to the per-study-type defaults for every driver, and
    such a row is a rate-card estimate, not something the protocol produced.
    """
    target = CONDUCT_TARGET_PER_ENROLLED[drivers.complexity]
    warnings: list[str] = []
    rows: list[dict] = []
    order = 0

    n_pk = drivers.n_pk_timepoints
    n_ada = drivers.n_ada_timepoints
    n_days = drivers.n_visit_days
    n_enrolled = drivers.n_enrolled

    if n_pk > 0:
        order += 1
        qty = float(n_pk * n_enrolled)
        row_source = BudgetLineSource.protocol_derived if pk_from_protocol else BudgetLineSource.rate_card
        rows.append({
            "derivation_key": "pk_draws",
            "label": "PK sampling draws",
            "quantity": qty,
            "unit_cost": CONDUCT_PK_DRAW_RATE,
            "unit_description": "per draw",
            "total": round(qty * CONDUCT_PK_DRAW_RATE, 2),
            "status": BudgetLineStatus.costed,
            "source": row_source,
            "sort_order": order,
            "note": (
                f"{n_pk} PK timepoints x {n_enrolled} enrolled subjects"
                + (", from this protocol's computed sampling schedule." if pk_from_protocol
                   else ", from the default sampling assumption for this study type.")
                + f" Unit cost ${CONDUCT_PK_DRAW_RATE:.2f} is an approximate blended "
                  "procedure rate from the rate card, not a quote."
            ),
        })

    if n_ada > 0:
        order += 1
        qty = float(n_ada * n_enrolled)
        row_source = BudgetLineSource.protocol_derived if ada_from_protocol else BudgetLineSource.rate_card
        rows.append({
            "derivation_key": "ada_draws",
            "label": "Immunogenicity/ADA sampling draws",
            "quantity": qty,
            "unit_cost": CONDUCT_ADA_DRAW_RATE,
            "unit_description": "per draw",
            "total": round(qty * CONDUCT_ADA_DRAW_RATE, 2),
            "status": BudgetLineStatus.costed,
            "source": row_source,
            "sort_order": order,
            "note": (
                f"{n_ada} ADA timepoints x {n_enrolled} enrolled subjects"
                + (", from this protocol's computed immunogenicity schedule." if ada_from_protocol
                   else ", from the default immunogenicity assumption for this study type.")
                + f" Unit cost ${CONDUCT_ADA_DRAW_RATE:.2f} is an approximate blended "
                  "procedure rate from the rate card, not a quote."
            ),
        })

    if n_days > 0:
        order += 1
        qty = float(n_days * n_enrolled)
        row_source = BudgetLineSource.protocol_derived if visit_days_from_protocol else BudgetLineSource.rate_card
        rows.append({
            "derivation_key": "subject_visits",
            "label": "Enrolled subject visits",
            "quantity": qty,
            "unit_cost": CONDUCT_VISIT_DAY_RATE,
            "unit_description": "per visit",
            "total": round(qty * CONDUCT_VISIT_DAY_RATE, 2),
            "status": BudgetLineStatus.costed,
            "source": row_source,
            "sort_order": order,
            "note": (
                f"{n_days} distinct visit days"
                + (" implied by the schedule" if visit_days_from_protocol
                   else " assumed for this study type")
                + f" x {n_enrolled} enrolled subjects. Unit cost "
                  f"${CONDUCT_VISIT_DAY_RATE:.2f} is an approximate non-procedure "
                  "site-time rate from the rate card, not a quote."
            ),
        })

    itemized_per_enrolled = n_pk * CONDUCT_PK_DRAW_RATE + n_ada * CONDUCT_ADA_DRAW_RATE + n_days * CONDUCT_VISIT_DAY_RATE
    residual = round(max(0.0, target - itemized_per_enrolled), 2)

    if itemized_per_enrolled > target:
        warnings.append(
            f"This protocol's schedule itemizes to ${itemized_per_enrolled:,.2f} per "
            f"enrolled subject, above the ${target:,.2f} reference per-subject "
            "clinical-conduct cost for a study of this type, so no balancing row was added."
        )
    elif residual > 0:
        order += 1
        rows.append({
            "derivation_key": "clinical_conduct.site_conduct_enrolled",
            "label": (
                "Site conduct balance: investigator, coordinator, nursing, housing and "
                "confinement (enrolled subjects)"
            ),
            "quantity": float(n_enrolled),
            "unit_cost": residual,
            "unit_description": "per subject",
            "total": round(n_enrolled * residual, 2),
            "status": BudgetLineStatus.costed,
            "source": BudgetLineSource.rate_card,
            "sort_order": order,
            "note": (
                f"Balances the itemized procedure lines above up to the ${target:,.2f} "
                "reference per-enrolled-subject clinical-conduct cost observed in "
                "comparable signed CRO budgets."
            ),
        })

    sf_rate = round(target * CONDUCT_SCREEN_FAILURE_RATIO, 2)
    if drivers.n_screen_failures > 0:
        order += 1
        rows.append({
            "derivation_key": "clinical_conduct.screen_failure_conduct",
            "label": "Screen failure subject clinical conduct (screening procedures only)",
            "quantity": float(drivers.n_screen_failures),
            "unit_cost": sf_rate,
            "unit_description": "per subject",
            "total": round(drivers.n_screen_failures * sf_rate, 2),
            "status": BudgetLineStatus.costed,
            "source": BudgetLineSource.rate_card,
            "sort_order": order,
            "note": (
                f"{drivers.n_screen_failures} screen failures x ${sf_rate:.2f} "
                f"(~{CONDUCT_SCREEN_FAILURE_RATIO:.0%} of the ${target:,.2f} full "
                "per-subject clinical-conduct cost — screen failures only complete "
                "screening)."
            ),
        })

    alt_rate = round(target * CONDUCT_ALTERNATE_RATIO, 2)
    if drivers.n_alternates > 0:
        order += 1
        rows.append({
            "derivation_key": "clinical_conduct.alternate_conduct",
            "label": "Alternate subject clinical conduct (replaced before completion)",
            "quantity": float(drivers.n_alternates),
            "unit_cost": alt_rate,
            "unit_description": "per subject",
            "total": round(drivers.n_alternates * alt_rate, 2),
            "status": BudgetLineStatus.costed,
            "source": BudgetLineSource.rate_card,
            "sort_order": order,
            "note": (
                f"{drivers.n_alternates} alternates x ${alt_rate:.2f} "
                f"(~{CONDUCT_ALTERNATE_RATIO:.0%} of the ${target:,.2f} full "
                "per-subject clinical-conduct cost — alternates may complete a bit "
                "more than screening before being replaced)."
            ),
        })

    if isinstance(drivers.periods, int) and drivers.periods > 1:
        warnings.append(
            f"This is a {drivers.periods}-period design. The PK-draw quantity counts ONE "
            f"profile per subject; multiply by {drivers.periods} if your CRO prices each "
            "period's draws separately."
        )

    extra = {
        "conduct": {
            "target_per_enrolled": target,
            "pk_draw_rate": CONDUCT_PK_DRAW_RATE,
            "ada_draw_rate": CONDUCT_ADA_DRAW_RATE,
            "visit_day_rate": CONDUCT_VISIT_DAY_RATE,
            "itemized_per_enrolled": round(itemized_per_enrolled, 2),
            "residual_per_enrolled": residual,
            "screen_failure_ratio": CONDUCT_SCREEN_FAILURE_RATIO,
            "alternate_ratio": CONDUCT_ALTERNATE_RATIO,
        },
        "warnings": warnings,
    }
    return rows, extra


def _is_user_authoritative(current_value: Any, key: str, previous_assumptions: dict) -> bool:
    """True when `current_value` (already sitting on the Budget row) should be
    treated as a real user override rather than a value THIS ENGINE itself
    wrote back on a prior auto-population run.

    Every resolved driver is written back onto the budget row so it is
    visible/editable (the "write every resolved driver back" rule) — but that
    means, without this check, an engine-derived value looks indistinguishable
    from a genuine user edit on every SUBSEQUENT run, permanently freezing the
    field: a re-link to a different protocol would never be able to update
    e.g. n_enrolled again, because the previous run's protocol-derived 24 is
    non-None and would look "user-supplied" forever after.

    A value counts as user-authoritative when there is no prior derivation
    snapshot to compare against (first run — a non-null value at this point
    came from the create payload, not from us), OR the prior snapshot's own
    source was already "user", OR the current value has since diverged from
    what the engine last wrote (the user edited it after the fact).
    """
    if current_value is None:
        return False
    prev = previous_assumptions.get(key)
    if prev is None:
        return True
    if prev.get("source") == "user":
        return True
    prev_value = prev.get("value")
    if isinstance(current_value, (int, float)) and isinstance(prev_value, (int, float)):
        return not _floats_close(float(current_value), float(prev_value))
    return current_value != prev_value


def _enum_str(v: Any) -> Any:
    """Unwraps a Python Enum member to its `.value`; passes through anything
    else (plain strings, None) unchanged. Used because a Budget column typed
    Enum(SomeEnum, native_enum=False) may come back as either the enum member
    or a bare string depending on where the value originated (freshly
    constructed vs. loaded from the DB)."""
    return v.value if isinstance(v, BudgetSubjectProfile) else v


def resolve_subject_profile(
    budget: Budget, protocol: Protocol | None, previous_assumptions: dict,
) -> tuple[str, dict]:
    """SPEC §1.2 — resolve `budget.subject_profile`. Precedence, strictly:

      1. User    — budget.subject_profile is user-authoritative (same
                   _is_user_authoritative mechanism as n_enrolled).
      2. Linked protocol — population_config['subject_type'] == 'patient'
                   (case-insensitive, stripped) -> patient, else healthy.
      3. Drug knowledge base — services.design_decisions.requires_patient_population.
      4. Default -> healthy. Preserves every existing budget's behaviour
         bit-for-bit: a budget that never sets subject_profile prices exactly
         as it does today.

    Pure with respect to `protocol`; does not mutate `budget`. Returns
    (profile_value, assumption_record) for the caller to write back.
    """
    current = _enum_str(budget.subject_profile)
    if _is_user_authoritative(current, "subject_profile", previous_assumptions):
        return current, {"value": current, "source": "user", "basis": "Supplied directly on the budget."}

    if protocol is not None and isinstance(protocol.population_config, dict):
        subject_type = protocol.population_config.get("subject_type")
        if isinstance(subject_type, str) and subject_type.strip():
            resolved = "patient" if subject_type.strip().lower() == "patient" else "healthy"
            return resolved, {
                "value": resolved, "source": "protocol",
                "basis": f"Linked protocol population_config['subject_type'] = '{subject_type}'.",
            }

    drug_inn = (budget.drug_inn or "").strip()
    requires_patient = requires_patient_population(drug_inn) if drug_inn else None
    if requires_patient is True:
        return "patient", {
            "value": "patient", "source": "inferred_from_safety_profile",
            "basis": "Cytotoxic / toxic safety profile — not safe in healthy volunteers.",
        }
    if requires_patient is False:
        return "healthy", {
            "value": "healthy", "source": "inferred_from_safety_profile",
            "basis": "Curated safety knowledge base classifies this product as safe for "
                     "single-dose administration in healthy volunteers.",
        }

    return "healthy", {
        "value": "healthy", "source": "default",
        "basis": "No subject_profile supplied, no linked-protocol population type, and "
                 "the drug is not in the curated safety knowledge base; defaulting to a "
                 "healthy-volunteer study.",
    }


def resolve_therapeutic_area_and_indication(
    budget: Budget, protocol: Protocol | None, previous_assumptions: dict,
) -> tuple[str, str | None, dict, dict]:
    """SPEC §1.2 (therapeutic-area / oncology sub-flag) + the indication rule.

    Indication: linked protocol's recommended patient-population indication ->
    the user's own budget.indication -> None.
    Therapeutic area: user -> classify_therapeutic_area(drug_inn, indication) ->
    'unknown' (treated as 'other' for pricing, with a warning left to the caller).

    Returns (therapeutic_area, indication, ta_assumption, indication_assumption).
    """
    indication = budget.indication
    indication_source = None

    if protocol is not None and isinstance(protocol.population_config, dict):
        pp = protocol.population_config.get("_patient_population")
        if isinstance(pp, dict):
            rec = pp.get("recommended")
            if (
                isinstance(rec, dict) and rec.get("indication")
                and not _is_user_authoritative(indication, "indication", previous_assumptions)
            ):
                indication = rec["indication"]
                indication_source = "protocol"

    if indication and indication_source is None:
        indication_source = "user" if budget.indication else "default"

    indication_assumption = {
        "value": indication,
        "source": indication_source or "default",
        "basis": (
            "Linked protocol's recommended patient-population indication."
            if indication_source == "protocol"
            else "Supplied directly on the budget." if indication_source == "user"
            else "No indication available from a linked protocol or the user."
        ),
    }

    current_ta = budget.therapeutic_area
    if _is_user_authoritative(current_ta, "therapeutic_area", previous_assumptions):
        ta = current_ta
        ta_assumption = {"value": ta, "source": "user", "basis": "Supplied directly on the budget."}
    else:
        ta = classify_therapeutic_area(budget.drug_inn or "", indication)
        basis = f"services.design_decisions.classify_therapeutic_area('{budget.drug_inn}', indication)."
        if ta == "unknown":
            basis += (
                " Not classifiable from the curated oncology list or indication text; "
                "treated as 'other' for pricing purposes."
            )
        ta_assumption = {"value": ta, "source": "computed", "basis": basis}

    return ta, indication, ta_assumption, indication_assumption


def _resolve_patient_profile_key(study_type: str, therapeutic_area: str) -> str:
    """SPEC §1.5 step 4 — key = f'{study_type}_oncology' if oncology else
    f'{study_type}_patient', with a fallback chain: exact -> 'biosimilar_pk_patient'
    (any patient study of an unknown/other TA is priced on the non-oncology
    patient profile)."""
    candidate = f"{study_type}_oncology" if therapeutic_area == "oncology" else f"{study_type}_patient"
    if candidate in PATIENT_STUDY_PROFILES:
        return candidate
    return "biosimilar_pk_patient"


def _protocol_treatment_months(protocol: Protocol | None) -> float | None:
    """SPEC §3.6 step 2 — the ONLY phase with a genuine protocol source:
    the linked protocol's computed sampling schedule's maximum scheduled day
    / 30.44, or the dosing block's n_cycles x cycle_length_days / 30.44 when
    present. Rounded to 0.5 months, floored at 1.0. Returns None when neither
    is available — the other four phases NEVER get a protocol source."""
    if protocol is None or not isinstance(protocol.population_config, dict):
        return None
    pc = protocol.population_config

    sched = pc.get("_sampling_schedule")
    if isinstance(sched, dict):
        days: list[float] = []
        for key in ("timepoints", "timepoints_steady_state", "steady_state_troughs"):
            entries = sched.get(key) or []
            if isinstance(entries, list):
                for e in entries:
                    d = e.get("day") if isinstance(e, dict) else None
                    if isinstance(d, (int, float)) and not isinstance(d, bool):
                        days.append(float(d))
        if days:
            months = max(days) / 30.44
            return max(1.0, round(months * 2) / 2)

    dosing = pc.get("dosing")
    if isinstance(dosing, dict):
        n_cycles = dosing.get("n_cycles")
        cycle_len = dosing.get("cycle_length_days")
        if (
            isinstance(n_cycles, (int, float)) and not isinstance(n_cycles, bool)
            and isinstance(cycle_len, (int, float)) and not isinstance(cycle_len, bool)
        ):
            months = (n_cycles * cycle_len) / 30.44
            return max(1.0, round(months * 2) / 2)

    return None


def _resolve_patient_extra_drivers(
    budget: Budget,
    protocol: Protocol | None,
    previous_assumptions: dict,
    assumptions: dict,
    warnings: list[str],
    *,
    study_type: str,
    therapeutic_area: str,
    profile_key: str,
    n_enrolled: int,
    n_sites: int,
    precedent: BudgetPrecedentResult = NO_PRECEDENT,
) -> dict[str, Any]:
    """SPEC §1.3/§1.5/§3.2/§3.6 — resolves every patient-profile-only driver in
    the same user -> protocol -> default precedence used throughout this
    module. Mutates `assumptions` / `warnings` in place and writes every
    resolved value back onto `budget` (the "write every resolved driver back"
    rule). Returns a dict of StudyDrivers kwargs plus the three base-field
    overrides (project_duration_months, complexity, n_external_vendors) and a
    "phases_block" entry for the derivation audit trail — the caller pops
    those four before spreading the rest into StudyDrivers(**...).
    """
    profile = PATIENT_STUDY_PROFILES.get(profile_key) or PATIENT_STUDY_PROFILES["biosimilar_pk_patient"]

    def _simple_field(key: str, default_value, cast=float):
        cur = getattr(budget, key)
        if _is_user_authoritative(cur, key, previous_assumptions):
            assumptions[key] = {"value": cur, "source": "user", "basis": "Supplied directly on the budget."}
            val = cur
        else:
            val = cast(default_value)
            assumptions[key] = {
                "value": val, "source": "default",
                "basis": f"Default '{profile_key}' patient study profile.",
            }
        setattr(budget, key, val)
        return val

    def _precedent_or_simple_field(key: str, default_value, precedent_value: int | None, cast=float):
        """Same as _simple_field, but with a real-trial-precedent tier
        inserted between 'user' and 'default' when the caller has one."""
        cur = getattr(budget, key)
        if _is_user_authoritative(cur, key, previous_assumptions):
            assumptions[key] = {"value": cur, "source": "user", "basis": "Supplied directly on the budget."}
            val = cur
        elif precedent_value is not None:
            val = cast(precedent_value)
            assumptions[key] = {
                "value": val, "source": "trial_precedent",
                "basis": f"Median across {len(precedent.precedents)} real ClinicalTrials.gov "
                         f"trial(s) of '{budget.drug_inn}' "
                         f"({', '.join(p.nct_id for p in precedent.precedents)}).",
            }
        else:
            val = cast(default_value)
            assumptions[key] = {
                "value": val, "source": "default",
                "basis": f"Default '{profile_key}' patient study profile.",
            }
        setattr(budget, key, val)
        return val

    n_countries = _precedent_or_simple_field(
        "n_countries", profile["n_countries"], precedent.median_n_countries, int
    )
    n_sites_selected = _precedent_or_simple_field(
        "n_sites_selected", profile["n_sites_selected"], precedent.median_n_sites, int
    )
    dropout_rate_pct = _simple_field("dropout_rate_pct", profile["dropout_rate_pct"], float)
    n_expected_saes = _simple_field("n_expected_saes", profile["n_expected_saes"], int)
    n_expected_susars = _simple_field("n_expected_susars", profile["n_expected_susars"], int)
    n_visits_per_patient = _simple_field("n_visits_per_patient", profile["n_visits_per_patient"], int)
    n_protocol_amendments = _simple_field("n_protocol_amendments", profile["n_protocol_amendments"], int)
    archival_retention_years = _simple_field(
        "archival_retention_years", profile["archival_retention_years"], int
    )

    # --- geography -----------------------------------------------------------
    geography_profile = budget.geography_profile
    if _is_user_authoritative(geography_profile, "geography_profile", previous_assumptions):
        assumptions["geography_profile"] = {
            "value": geography_profile, "source": "user", "basis": "Supplied directly.",
        }
    else:
        geography_profile = profile.get("geography_profile", "global_emerging_mixed")
        assumptions["geography_profile"] = {
            "value": geography_profile, "source": "default",
            "basis": f"Default '{profile_key}' patient study profile.",
        }
    budget.geography_profile = geography_profile

    geography_factor = budget.geography_factor
    if _is_user_authoritative(geography_factor, "geography_factor", previous_assumptions):
        assumptions["geography_factor"] = {
            "value": geography_factor, "source": "user",
            "basis": "Supplied directly; overrides the geography_profile lookup.",
        }
    else:
        geography_factor = GEOGRAPHY_FACTORS.get(geography_profile, GEOGRAPHY_FACTORS["global_emerging_mixed"])
        assumptions["geography_factor"] = {
            "value": geography_factor, "source": "default",
            "basis": f"GEOGRAPHY_FACTORS['{geography_profile}'].",
        }
    budget.geography_factor = geography_factor

    # --- BICR ------------------------------------------------------------------
    bicr_required = budget.bicr_required
    if _is_user_authoritative(bicr_required, "bicr_required", previous_assumptions):
        assumptions["bicr_required"] = {"value": bicr_required, "source": "user", "basis": "Supplied directly."}
    else:
        bicr_required = bool(profile.get("bicr_required", therapeutic_area == "oncology"))
        assumptions["bicr_required"] = {
            "value": bicr_required, "source": "computed",
            "basis": "True when therapeutic_area == 'oncology' (imaging, BICR, SAE volume "
                     "and grant magnitude all scale with an oncology indication).",
        }
    budget.bicr_required = bicr_required

    # --- comparator (deliberately opt-in — §1.6) --------------------------------
    comparator_sourced = bool(budget.comparator_sourced_by_sponsor)
    assumptions["comparator_sourced_by_sponsor"] = {
        "value": comparator_sourced,
        "source": "user" if comparator_sourced else "default",
        "basis": (
            "Explicitly enabled." if comparator_sourced else
            "Off by default — a sponsor-sourced reference product can be a $10M+ line; "
            "the comparator-drug line stays actuals_only until the user opts in."
        ),
    }

    comparator_arm_fraction = budget.comparator_arm_fraction
    if _is_user_authoritative(comparator_arm_fraction, "comparator_arm_fraction", previous_assumptions):
        assumptions["comparator_arm_fraction"] = {
            "value": comparator_arm_fraction, "source": "user", "basis": "Supplied directly.",
        }
    else:
        comparator_arm_fraction = 0.5
        assumptions["comparator_arm_fraction"] = {
            "value": comparator_arm_fraction, "source": "default",
            "basis": "Default 1:1 randomization assumption (50% of enrolled subjects on "
                     "the comparator arm).",
        }
    budget.comparator_arm_fraction = comparator_arm_fraction

    # --- phase durations (§3.6) -------------------------------------------------
    phase_defaults = {
        "startup_months": profile["startup_months"],
        "recruitment_months": profile["recruitment_months"],
        "treatment_months": profile["treatment_months"],
        "followup_months": profile["followup_months"],
        "closeout_months": profile["closeout_months"],
    }
    resolved_phases: dict[str, float] = {}
    protocol_treatment = _protocol_treatment_months(protocol)
    for key, default_val in phase_defaults.items():
        cur = getattr(budget, key)
        if _is_user_authoritative(cur, key, previous_assumptions):
            resolved_phases[key] = float(cur)
            assumptions[key] = {"value": resolved_phases[key], "source": "user", "basis": "Supplied directly."}
        elif key == "treatment_months" and protocol_treatment is not None:
            resolved_phases[key] = protocol_treatment
            assumptions[key] = {
                "value": resolved_phases[key], "source": "protocol",
                "basis": "Derived from the linked protocol's computed sampling schedule "
                         "(max scheduled day / 30.44) or dosing cycles.",
            }
        else:
            resolved_phases[key] = float(default_val)
            assumptions[key] = {
                "value": resolved_phases[key], "source": "default",
                "basis": f"Default '{profile_key}' patient study profile.",
            }

    phase_sum = round(sum(resolved_phases.values()), 4)
    pdm = budget.project_duration_months
    if (
        _is_user_authoritative(pdm, "project_duration_months", previous_assumptions)
        and phase_sum > 0 and not _floats_close(pdm, phase_sum)
    ):
        ratio = float(pdm) / phase_sum
        for key in resolved_phases:
            resolved_phases[key] = round(resolved_phases[key] * ratio, 2)
            assumptions[key] = {
                "value": resolved_phases[key], "source": "phase_scaled",
                "basis": f"Scaled from the default profile by a factor of {ratio:.4f} to "
                         f"match the user-supplied total of {pdm} months.",
            }
        project_duration_months = float(pdm)
        warnings.append(
            f"You set a total duration of {pdm} months; the five phase durations were "
            "scaled proportionally from the default profile. Edit any phase directly to "
            "control it."
        )
        assumptions["project_duration_months"] = {
            "value": project_duration_months, "source": "user",
            "basis": "Supplied directly; the five phase durations were scaled to match.",
        }
    else:
        project_duration_months = round(phase_sum, 2)
        assumptions["project_duration_months"] = {
            "value": project_duration_months, "source": "computed",
            "basis": "startup_months + recruitment_months + treatment_months + "
                     "followup_months + closeout_months.",
        }

    for key, val in resolved_phases.items():
        setattr(budget, key, val)
    budget.project_duration_months = project_duration_months

    startup_months = resolved_phases["startup_months"]
    recruitment_months = resolved_phases["recruitment_months"]
    treatment_months = resolved_phases["treatment_months"]
    followup_months = resolved_phases["followup_months"]
    closeout_months = resolved_phases["closeout_months"]
    active_months = recruitment_months + treatment_months + followup_months

    # Recruitment-duration sanity check (§3.6).
    if n_sites > 0 and recruitment_months > 0:
        rate_per_site_month = n_enrolled / (n_sites * recruitment_months)
        lo, hi = RECRUITMENT_RATE_BAND
        if not (lo <= rate_per_site_month <= hi):
            warnings.append(
                f"Implied enrolment rate is {rate_per_site_month:.2f} patients/site/month "
                f"({n_enrolled} enrolled / {n_sites} sites / {recruitment_months:.1f} "
                f"recruitment months), outside the typical {lo}-{hi} band — an implied "
                "enrolment rate outside this range is the single most common cause of a "
                "wrong phase-based budget."
            )

    # --- derived driver formulas (§3.2) -----------------------------------------
    dropout_rate_pct = max(0.0, min(100.0, dropout_rate_pct))
    n_completed_computed = round(n_enrolled * (1 - dropout_rate_pct / 100.0))
    n_completed = budget.n_completed
    if _is_user_authoritative(n_completed, "n_completed", previous_assumptions):
        assumptions["n_completed"] = {"value": n_completed, "source": "user", "basis": "Supplied directly."}
    else:
        n_completed = n_completed_computed
        assumptions["n_completed"] = {
            "value": n_completed, "source": "computed",
            "basis": f"round({n_enrolled} enrolled x (1 - {dropout_rate_pct:.1f}% dropout)).",
        }
    budget.n_completed = n_completed
    n_dropouts = max(0, n_enrolled - n_completed)

    n_siv = n_sites
    n_cov = n_sites
    n_qualification_visits = round(n_sites * 1.02)

    total_monitoring_visits = round(n_sites * active_months * 0.29)
    onsite_computed = round(total_monitoring_visits * (1 - MONITORING_REMOTE_FRACTION))
    remote_computed = total_monitoring_visits - onsite_computed

    n_imv_onsite = budget.n_monitoring_visits_onsite
    if _is_user_authoritative(n_imv_onsite, "n_monitoring_visits_onsite", previous_assumptions):
        assumptions["n_monitoring_visits_onsite"] = {
            "value": n_imv_onsite, "source": "user", "basis": "Supplied directly.",
        }
    else:
        n_imv_onsite = onsite_computed
        assumptions["n_monitoring_visits_onsite"] = {
            "value": n_imv_onsite, "source": "computed",
            "basis": f"round({n_sites} sites x {active_months:.1f} active months x 0.29 "
                     f"visits/site-month x {1 - MONITORING_REMOTE_FRACTION:.0%} on-site).",
        }
    budget.n_monitoring_visits_onsite = n_imv_onsite

    n_imv_remote = budget.n_monitoring_visits_remote
    if _is_user_authoritative(n_imv_remote, "n_monitoring_visits_remote", previous_assumptions):
        assumptions["n_monitoring_visits_remote"] = {
            "value": n_imv_remote, "source": "user", "basis": "Supplied directly.",
        }
    else:
        n_imv_remote = remote_computed
        assumptions["n_monitoring_visits_remote"] = {
            "value": n_imv_remote, "source": "computed",
            "basis": f"round({n_sites} sites x {active_months:.1f} active months x 0.29 "
                     f"visits/site-month x {MONITORING_REMOTE_FRACTION:.0%} remote).",
        }
    budget.n_monitoring_visits_remote = n_imv_remote

    n_pharmacy_visits = budget.n_pharmacy_visits
    if _is_user_authoritative(n_pharmacy_visits, "n_pharmacy_visits", previous_assumptions):
        assumptions["n_pharmacy_visits"] = {"value": n_pharmacy_visits, "source": "user", "basis": "Supplied directly."}
    else:
        n_pharmacy_visits = round(n_sites * active_months * 0.335)
        assumptions["n_pharmacy_visits"] = {
            "value": n_pharmacy_visits, "source": "computed",
            "basis": f"round({n_sites} sites x {active_months:.1f} active months x 0.335).",
        }
    budget.n_pharmacy_visits = n_pharmacy_visits

    site_months_startup = round(n_sites * startup_months * SITE_MONTH_RAMP["startup"])
    site_months_active = round(n_sites * active_months * SITE_MONTH_RAMP["active"])
    site_months_closeout = round(n_sites * closeout_months * SITE_MONTH_RAMP["closeout"])
    site_years = round(n_sites * project_duration_months / 12)

    crf_pages_completer = int(profile.get("crf_pages_completer", 0))
    crf_pages_dropout = int(profile.get("crf_pages_dropout", 0))
    imaging_timepoints_completer = int(profile.get("imaging_timepoints_completer", 0))
    imaging_timepoints_dropout = int(profile.get("imaging_timepoints_dropout", 0))
    n_depots = int(profile.get("n_depots", 0))
    ip_shipments_per_site = int(profile.get("ip_shipments_per_site", 0))
    n_external_vendors = int(profile.get("n_external_vendors", 1))
    complexity = profile.get("complexity", "complex")

    for k, v in {
        "crf_pages_completer": crf_pages_completer, "crf_pages_dropout": crf_pages_dropout,
        "imaging_timepoints_completer": imaging_timepoints_completer,
        "imaging_timepoints_dropout": imaging_timepoints_dropout,
        "n_depots": n_depots, "ip_shipments_per_site": ip_shipments_per_site,
        "monitoring_remote_fraction": MONITORING_REMOTE_FRACTION,
    }.items():
        assumptions[k] = {"value": v, "source": "default", "basis": f"Default '{profile_key}' patient study profile."}

    return dict(
        n_countries=n_countries, n_sites_selected=n_sites_selected,
        dropout_rate_pct=dropout_rate_pct, n_completed=n_completed, n_dropouts=n_dropouts,
        startup_months=startup_months, recruitment_months=recruitment_months,
        treatment_months=treatment_months, followup_months=followup_months,
        closeout_months=closeout_months, n_expected_saes=n_expected_saes,
        n_expected_susars=n_expected_susars, n_imv_onsite=n_imv_onsite, n_imv_remote=n_imv_remote,
        n_pharmacy_visits=n_pharmacy_visits, n_siv=n_siv, n_cov=n_cov,
        n_qualification_visits=n_qualification_visits, n_visits_per_patient=n_visits_per_patient,
        crf_pages_completer=crf_pages_completer, crf_pages_dropout=crf_pages_dropout,
        n_protocol_amendments=n_protocol_amendments, n_depots=n_depots,
        ip_shipments_per_site=ip_shipments_per_site,
        imaging_timepoints_completer=imaging_timepoints_completer,
        imaging_timepoints_dropout=imaging_timepoints_dropout,
        geography_factor=geography_factor, archival_retention_years=archival_retention_years,
        bicr_required=bicr_required, comparator_sourced_by_sponsor=comparator_sourced,
        comparator_arm_fraction=comparator_arm_fraction,
        # base-field overrides + audit block — popped by the caller before
        # spreading the remainder into StudyDrivers(**...):
        project_duration_months=project_duration_months, complexity=complexity,
        n_external_vendors=n_external_vendors,
        phases_block=dict(
            startup_months=startup_months, recruitment_months=recruitment_months,
            treatment_months=treatment_months, followup_months=followup_months,
            closeout_months=closeout_months, total_months=project_duration_months,
            active_months=round(active_months, 2),
            site_months_startup=site_months_startup, site_months_active=site_months_active,
            site_months_closeout=site_months_closeout, site_years=site_years,
            ramp_factors=dict(SITE_MONTH_RAMP),
            # popped separately by the caller (_resolve_drivers) into its own
            # top-level derivation["profile_key"] — not part of the "phases"
            # audit block itself.
            profile_key=profile_key,
        ),
    )


def _resolve_drivers(
    budget: Budget, protocol: Protocol | None, previous_assumptions: dict | None = None,
    precedent: BudgetPrecedentResult | None = None,
) -> tuple[StudyDrivers, dict, list[str], dict]:
    """STEPS 1-2 of the auto-population algorithm. Returns (drivers,
    assumptions, warnings, phases_block). `phases_block` is {} for a
    healthy-volunteer budget and the §3.2/§7.5 phase/site-month audit block
    for a patient budget. Mutates budget's identity/assumption fields IN
    PLACE per the "write every resolved driver back" rule (only when the field
    is not already user-authoritative — see _is_user_authoritative — is it
    re-derived from the protocol/default; a real user value is never
    clobbered).

    `precedent` (services.budget_trial_precedent.BudgetPrecedentResult) adds a
    THIRD resolution tier — user -> protocol -> real trial precedent ->
    generic per-study-type default — for the handful of quantities a drug's
    own ClinicalTrials.gov history can actually answer (n_enrolled, n_sites,
    and, for a patient study, n_countries/n_sites_selected). This is what
    makes two different drugs of the same study_type/subject_profile produce
    DIFFERENT budgets instead of the same generic template. Defaults to
    NO_PRECEDENT (behaves exactly as before) when the caller has none — e.g.
    the CT.gov lookup failed, or the drug has no usable trial history."""
    warnings: list[str] = []
    assumptions: dict[str, dict] = {}
    previous_assumptions = previous_assumptions or {}
    precedent = precedent or NO_PRECEDENT

    # --- Step 1: identity -------------------------------------------------
    drug_inn = (budget.drug_inn or "").strip() or (
        protocol.drug.inn if protocol is not None and protocol.drug else None
    )
    budget.drug_inn = drug_inn

    # subject_profile — resolved FIRST (spec §1.2), before study_type, because
    # the patient default profile is keyed on study_type AND therapeutic_area.
    subject_profile, subject_profile_assumption = resolve_subject_profile(
        budget, protocol, previous_assumptions
    )
    budget.subject_profile = BudgetSubjectProfile(subject_profile)
    assumptions["subject_profile"] = subject_profile_assumption

    therapeutic_area, indication, ta_assumption, indication_assumption = (
        resolve_therapeutic_area_and_indication(budget, protocol, previous_assumptions)
    )
    budget.therapeutic_area = therapeutic_area
    budget.indication = indication
    assumptions["therapeutic_area"] = ta_assumption
    assumptions["indication"] = indication_assumption
    if therapeutic_area == "unknown":
        warnings.append(
            f"'{drug_inn or ''}' could not be classified as oncology or another "
            "therapeutic area; treated as 'other' for pricing purposes."
        )

    modality = classify_drug_modality(drug_inn or "")

    study_type = (budget.study_type or "").strip() or None
    if not study_type and protocol is not None and protocol.study_type:
        study_type = protocol.study_type.value
    if not study_type:
        study_type = "biosimilar_pk" if modality == "biologic" else "BE"
        assumptions["study_type"] = {
            "value": study_type,
            "source": "inferred_from_modality" if modality != "unknown" else "fallback_default",
            "basis": f"No study_type supplied or linked; inferred '{study_type}' from "
                     f"drug modality '{modality}'.",
        }
    else:
        assumptions["study_type"] = {
            "value": study_type,
            "source": "protocol" if (protocol is not None and protocol.study_type
                                      and protocol.study_type.value == study_type) else "user",
            "basis": "Study type supplied directly or taken from the linked protocol.",
        }
    budget.study_type = study_type

    if subject_profile == "patient":
        profile_key = _resolve_patient_profile_key(study_type, therapeutic_area)
        profile = PATIENT_STUDY_PROFILES.get(profile_key) or PATIENT_STUDY_PROFILES["biosimilar_pk_patient"]
    else:
        profile_key = None
        profile = DEFAULT_STUDY_PROFILES.get(study_type, DEFAULT_STUDY_PROFILES["BE"])

    # --- Step 2: drivers, in strict resolution order user -> protocol -> default
    pc = (protocol.population_config or {}) if protocol is not None else {}
    ss = pc.get("_sample_size") or {}
    sched = pc.get("_sampling_schedule") or {}
    if not isinstance(ss, dict):
        ss = {}
    if not isinstance(sched, dict):
        sched = {}
    bio = pc.get("_bioanalytical") if protocol is not None else None

    def _pos_int(v) -> int | None:
        return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else None

    # n_enrolled
    n_enrolled = budget.n_enrolled
    if _is_user_authoritative(n_enrolled, "n_enrolled", previous_assumptions):
        assumptions["n_enrolled"] = {"value": n_enrolled, "source": "user",
                                      "basis": "Supplied directly on the budget."}
    else:
        n_enrolled = _pos_int(ss.get("n_enrolled"))
        if n_enrolled is not None:
            assumptions["n_enrolled"] = {
                "value": n_enrolled, "source": "protocol",
                "basis": "population_config['_sample_size']['n_enrolled'] on the linked protocol.",
            }
        elif precedent.median_n_enrolled is not None:
            n_enrolled = precedent.median_n_enrolled
            assumptions["n_enrolled"] = {
                "value": n_enrolled, "source": "trial_precedent",
                "basis": f"Median enrollment across {len(precedent.precedents)} real "
                         f"ClinicalTrials.gov trial(s) of '{drug_inn}' "
                         f"({', '.join(p.nct_id for p in precedent.precedents)}).",
            }
        else:
            n_enrolled = int(profile["n_enrolled"])
            assumptions["n_enrolled"] = {
                "value": n_enrolled, "source": "default",
                "basis": f"Default {study_type} study profile (no protocol, real trial "
                         "precedent, or user value).",
            }
    budget.n_enrolled = n_enrolled

    # screen_failure_rate_pct
    sf_rate = budget.screen_failure_rate_pct
    if _is_user_authoritative(sf_rate, "screen_failure_rate_pct", previous_assumptions):
        assumptions["screen_failure_rate_pct"] = {"value": sf_rate, "source": "user", "basis": "Supplied directly."}
    else:
        sf_rate = float(profile["screen_failure_rate_pct"])
        assumptions["screen_failure_rate_pct"] = {
            "value": sf_rate, "source": "default",
            "basis": f"Default {study_type} study profile.",
        }
    budget.screen_failure_rate_pct = sf_rate

    # n_alternates — forced to 0 for the patient profile (§1.3): alternate
    # subjects are a healthy-volunteer clinic concept; patient studies have
    # dropouts instead.
    if subject_profile == "patient":
        if budget.n_alternates:
            warnings.append(
                f"n_alternates ({budget.n_alternates}) was supplied, but alternate "
                "subjects are a healthy-volunteer clinic concept; patient studies have "
                "dropouts instead. Forced to 0."
            )
        n_alternates = 0
        assumptions["n_alternates"] = {
            "value": 0, "source": "computed",
            "basis": "Forced to 0 for the patient subject profile (dropouts model "
                     "attrition instead).",
        }
        budget.n_alternates = 0
    else:
        n_alternates = budget.n_alternates
        if _is_user_authoritative(n_alternates, "n_alternates", previous_assumptions):
            assumptions["n_alternates"] = {"value": n_alternates, "source": "user", "basis": "Supplied directly."}
        else:
            n_alternates = math.ceil(n_enrolled * float(profile["alternate_fraction"]))
            assumptions["n_alternates"] = {
                "value": n_alternates, "source": "computed",
                "basis": f"ceil({n_enrolled} enrolled x {profile['alternate_fraction']:.0%} "
                         "default alternate fraction).",
            }
        budget.n_alternates = n_alternates

    # n_screened
    n_screened = budget.n_screened
    if _is_user_authoritative(n_screened, "n_screened", previous_assumptions):
        assumptions["n_screened"] = {"value": n_screened, "source": "user", "basis": "Supplied directly."}
    else:
        if 0 < sf_rate < 100:
            n_screened = math.ceil((n_enrolled + n_alternates) / (1 - sf_rate / 100.0))
            assumptions["n_screened"] = {
                "value": n_screened, "source": "computed",
                "basis": f"ceil(({n_enrolled} enrolled + {n_alternates} alternates) / "
                         f"(1 - {sf_rate:.1f}% screen-failure rate)).",
            }
        else:
            n_screened = n_enrolled + n_alternates
            warnings.append(
                f"screen_failure_rate_pct ({sf_rate}) is out of the usable 0-100 range; "
                "n_screened was set to n_enrolled + n_alternates instead of being "
                "back-computed from the rate."
            )
            assumptions["n_screened"] = {
                "value": n_screened, "source": "computed",
                "basis": "Fallback: n_enrolled + n_alternates (invalid screen-failure rate).",
            }
    budget.n_screened = n_screened

    # n_screen_failures
    n_screen_failures = budget.n_screen_failures
    if _is_user_authoritative(n_screen_failures, "n_screen_failures", previous_assumptions):
        assumptions["n_screen_failures"] = {"value": n_screen_failures, "source": "user", "basis": "Supplied directly."}
    else:
        n_screen_failures = max(0, n_screened - n_enrolled - n_alternates)
        assumptions["n_screen_failures"] = {
            "value": n_screen_failures, "source": "computed",
            "basis": f"max(0, {n_screened} screened - {n_enrolled} enrolled - {n_alternates} alternates).",
        }
    budget.n_screen_failures = n_screen_failures

    # n_sites
    n_sites = budget.n_sites
    if _is_user_authoritative(n_sites, "n_sites", previous_assumptions):
        assumptions["n_sites"] = {"value": n_sites, "source": "user", "basis": "Supplied directly."}
    elif precedent.median_n_sites is not None:
        n_sites = precedent.median_n_sites
        assumptions["n_sites"] = {
            "value": n_sites, "source": "trial_precedent",
            "basis": f"Median listed-location count across {len(precedent.precedents)} real "
                     f"ClinicalTrials.gov trial(s) of '{drug_inn}' — a site-count proxy, not "
                     "an exact contracted-site figure.",
        }
    else:
        n_sites = int(profile["n_sites"])
        assumptions["n_sites"] = {"value": n_sites, "source": "default", "basis": f"Default {study_type} study profile."}
    budget.n_sites = n_sites

    # n_dosing_groups
    n_dosing_groups = budget.n_dosing_groups
    if _is_user_authoritative(n_dosing_groups, "n_dosing_groups", previous_assumptions):
        assumptions["n_dosing_groups"] = {"value": n_dosing_groups, "source": "user", "basis": "Supplied directly."}
    else:
        n_dosing_groups = int(profile.get("n_dosing_groups", 2))
        assumptions["n_dosing_groups"] = {
            "value": n_dosing_groups, "source": "default", "basis": f"Default {study_type} study profile.",
        }
    budget.n_dosing_groups = n_dosing_groups

    # project_duration_months — for the PATIENT profile this is DERIVED from the
    # five phase durations (§1.3/§3.6), resolved by _resolve_patient_extra_drivers
    # below; skip the generic per-study-type default here so it is never
    # overwritten with a value that ignores the phases.
    patient_kwargs: dict[str, Any] = {}
    patient_complexity: str | None = None
    if subject_profile == "patient":
        patient_kwargs = _resolve_patient_extra_drivers(
            budget, protocol, previous_assumptions, assumptions, warnings,
            study_type=study_type, therapeutic_area=therapeutic_area,
            profile_key=profile_key, n_enrolled=n_enrolled, n_sites=n_sites,
            precedent=precedent,
        )
        phases_block = patient_kwargs.pop("phases_block")
        project_duration_months = patient_kwargs.pop("project_duration_months")
        patient_complexity = patient_kwargs.pop("complexity")
        # n_external_vendors is resolved generically below from the SAME
        # profile dict (profile["n_external_vendors"]) with the ADA-vendor
        # bump, so the extra-drivers copy is discarded here rather than
        # applied twice.
        patient_kwargs.pop("n_external_vendors", None)
    else:
        phases_block = {}
        project_duration_months = budget.project_duration_months
        if _is_user_authoritative(project_duration_months, "project_duration_months", previous_assumptions):
            assumptions["project_duration_months"] = {"value": project_duration_months, "source": "user", "basis": "Supplied directly."}
        else:
            project_duration_months = float(profile["project_duration_months"])
            assumptions["project_duration_months"] = {
                "value": project_duration_months, "source": "default", "basis": f"Default {study_type} study profile.",
            }
        budget.project_duration_months = project_duration_months

    # doses_per_subject
    periods_val = ss.get("periods") if isinstance(ss.get("periods"), int) else sched.get("periods")
    doses_per_subject = _pos_int(sched.get("periods")) or _pos_int(ss.get("periods"))
    if doses_per_subject is not None:
        assumptions["doses_per_subject"] = {
            "value": doses_per_subject, "source": "protocol",
            "basis": "Linked protocol's computed schedule 'periods'.",
        }
    else:
        doses_per_subject = int(profile.get("doses_per_subject", 1))
        assumptions["doses_per_subject"] = {
            "value": doses_per_subject, "source": "default", "basis": f"Default {study_type} study profile.",
        }

    # n_pk_timepoints
    timepoints = sched.get("timepoints") or []
    if isinstance(timepoints, list) and len(timepoints) > 0:
        n_pk_timepoints = len(timepoints)
        assumptions["n_pk_timepoints"] = {
            "value": n_pk_timepoints, "source": "protocol",
            "basis": "Linked protocol's computed sampling schedule.",
        }
    else:
        n_pk_timepoints = DEFAULT_PK_TIMEPOINTS.get(study_type, DEFAULT_PK_TIMEPOINTS["BE"])
        assumptions["n_pk_timepoints"] = {
            "value": n_pk_timepoints, "source": "default", "basis": f"Default {study_type} study profile.",
        }

    # n_ada_timepoints
    ada = sched.get("immunogenicity_timepoints") or []
    if isinstance(ada, list) and len(ada) > 0:
        n_ada_timepoints = len(ada)
        assumptions["n_ada_timepoints"] = {
            "value": n_ada_timepoints, "source": "protocol",
            "basis": "Linked protocol's computed immunogenicity schedule.",
        }
    else:
        n_ada_timepoints = DEFAULT_ADA_TIMEPOINTS.get(study_type, 0)
        assumptions["n_ada_timepoints"] = {
            "value": n_ada_timepoints, "source": "default", "basis": f"Default {study_type} study profile.",
        }

    # n_visit_days
    n_visit_days_from_sched, day_warnings = _visit_days(sched) if sched else (0, [])
    warnings.extend(day_warnings)
    if n_visit_days_from_sched > 0:
        n_visit_days = n_visit_days_from_sched
        assumptions["n_visit_days"] = {
            "value": n_visit_days, "source": "protocol",
            "basis": "Distinct visit days implied by the linked protocol's schedule.",
        }
    else:
        n_visit_days = DEFAULT_VISIT_DAYS.get(study_type, DEFAULT_VISIT_DAYS["BE"])
        assumptions["n_visit_days"] = {
            "value": n_visit_days, "source": "default", "basis": f"Default {study_type} study profile.",
        }

    design_key = ss.get("design_key")
    periods = sched.get("periods") if isinstance(sched.get("periods"), int) else ss.get("periods")

    # complexity — the patient profile always prices as "complex" (§1.5); the
    # heuristic below is a healthy-volunteer-only concept. (patient_complexity
    # was already resolved by _resolve_patient_extra_drivers from the same
    # profile dict; re-reading `profile` here keeps this branch self-contained.)
    if subject_profile == "patient":
        complexity = patient_complexity or profile.get("complexity", "complex")
    else:
        complexity = (
            "complex"
            if (study_type == "biosimilar_pk" or modality == "biologic" or project_duration_months >= 9)
            else "simple"
        )

    # n_qa_audits (computed)
    if project_duration_months <= 6:
        n_qa_audits = 2
    elif project_duration_months <= 12:
        n_qa_audits = 3
    else:
        n_qa_audits = 4
    assumptions["n_qa_audits"] = {
        "value": n_qa_audits, "source": "computed",
        "basis": f"Scaled from project_duration_months ({project_duration_months}).",
    }

    # n_monitoring_visits (computed)
    n_monitoring_visits = max(2, math.ceil(project_duration_months / 2))
    assumptions["n_monitoring_visits"] = {
        "value": n_monitoring_visits, "source": "computed",
        "basis": f"max(2, ceil({project_duration_months} / 2)).",
    }

    # n_external_vendors (computed)
    n_external_vendors = int(profile["n_external_vendors"])
    if n_ada_timepoints > 0 and n_external_vendors < 2:
        n_external_vendors = 2
    assumptions["n_external_vendors"] = {
        "value": n_external_vendors, "source": "computed",
        "basis": "Default study-profile vendor count, bumped to 2 when immunogenicity "
                 "sampling is present.",
    }

    # is_randomized_multi_arm (computed)
    is_randomized_multi_arm = (
        n_dosing_groups >= 2 or design_key == "parallel" or study_type == "biosimilar_pk"
    )
    assumptions["is_randomized_multi_arm"] = {
        "value": is_randomized_multi_arm, "source": "computed",
        "basis": "n_dosing_groups >= 2, or a parallel design, or a biosimilar-PK study type.",
    }

    # bioanalysis_in_house (computed)
    bioanalysis_in_house = isinstance(bio, dict) and bio.get("assay_location") == "in_house"
    assumptions["bioanalysis_in_house"] = {
        "value": bioanalysis_in_house, "source": "computed",
        "basis": "True only when the linked protocol's bioanalytical plan explicitly "
                 "specifies in-house assay_location; otherwise outsourced by default, "
                 "matching both reference budgets.",
    }

    drivers_kwargs: dict[str, Any] = dict(
        drug_inn=drug_inn,
        modality=modality,
        study_type=study_type,
        complexity=complexity,
        n_enrolled=n_enrolled,
        n_alternates=n_alternates,
        n_screen_failures=n_screen_failures,
        n_screened=n_screened,
        screen_failure_rate_pct=sf_rate,
        n_sites=n_sites,
        n_dosing_groups=n_dosing_groups,
        project_duration_months=project_duration_months,
        doses_per_subject=doses_per_subject,
        n_pk_timepoints=n_pk_timepoints,
        n_ada_timepoints=n_ada_timepoints,
        n_visit_days=n_visit_days,
        periods=periods,
        design_key=design_key,
        n_qa_audits=n_qa_audits,
        n_monitoring_visits=n_monitoring_visits,
        n_external_vendors=n_external_vendors,
        is_randomized_multi_arm=is_randomized_multi_arm,
        bioanalysis_in_house=bioanalysis_in_house,
        # Patient/oncology axis — no-op defaults on the StudyDrivers dataclass
        # for a healthy budget (subject_profile == "healthy" here), so this
        # changes nothing about healthy-volunteer pricing.
        subject_profile=subject_profile,
        therapeutic_area=therapeutic_area,
        indication=indication,
    )
    if subject_profile == "patient":
        drivers_kwargs.update(patient_kwargs)
        drivers_kwargs["monitoring_remote_fraction"] = MONITORING_REMOTE_FRACTION
    drivers = StudyDrivers(**drivers_kwargs)

    assumptions["complexity"] = {
        "value": complexity, "source": "computed",
        "basis": (
            "Default 'complex' patient study profile (§1.5)." if subject_profile == "patient"
            else "biosimilar_pk study type, or biologic modality, or project duration "
                 ">= 9 months."
        ),
    }
    assumptions["modality"] = {
        "value": modality, "source": "computed",
        "basis": "services.design_decisions.classify_drug_modality(drug_inn).",
    }
    assumptions["doses_per_subject"]  # already set above

    return drivers, assumptions, warnings, phases_block


# ---------------------------------------------------------------------------
# Patient-card-only helpers: provenance badging, the derivation audit blocks
# (§4.4/§7.5), and the engine cross-check warnings (§2.4). None of these are
# reachable from the healthy-volunteer path.
# ---------------------------------------------------------------------------


def _provenance_for_line(line) -> tuple[BudgetLineSource, str | None, str | None]:
    """SPEC §4.2's precedence rule, adapted to this rate card's actual shape.

    No PATIENT_RATE_CARD line's QUANTITY is itself sourced from a linked
    protocol's schedule the way the healthy card's Clinical Conduct rows are
    (only the treatment_months ASSUMPTION can be protocol-sourced, and that
    is recorded on the phases block, not per-row) — so "protocol_derived"
    never applies at the row level here, and this reduces to: researched (a
    real, cited, non-estimate-tier source) vs rate_card (no citation, or a
    citation whose own tier is "estimate" — SPEC §4.3's explicit "must render
    as estimate, never as researched" for the lines whose research failed).
    """
    source_id = line.source_ref
    if not source_id or source_id not in SOURCE_REGISTRY:
        return BudgetLineSource.rate_card, None, None
    tier = SOURCE_REGISTRY[source_id].tier
    if tier == "estimate":
        return BudgetLineSource.rate_card, tier, source_id
    return BudgetLineSource.researched, tier, source_id


def _sum_line_totals(budget: Budget, keys: set[str]) -> float:
    return round(
        sum(
            (li.total or 0.0)
            for c in budget.categories for li in c.line_items
            if li.derivation_key in keys
        ),
        2,
    )


def _category_subtotal_by_name(budget: Budget, name: str) -> float:
    cat = next((c for c in budget.categories if c.name == name), None)
    return category_subtotal(cat) if cat is not None else 0.0


_PROVENANCE_TIER_PREFIX = {
    "published_rate": "researched",
    "derived": "derived",
    "bid_calibrated": "bid_calibrated",
    "estimate": "estimate",
}


def _build_provenance_summary(derivation_lines: list[dict], grand_total: float) -> dict:
    """SPEC §4.4's `provenance_summary` block. Grouped by the SOURCE's tier
    (published_rate/derived/bid_calibrated/estimate), not by the coarse
    BudgetLineSource badge — an estimate-tier line still has a source_id
    (the internal-estimate placeholder) but must count as "estimate", never
    inflate the "researched" total."""
    counts = {prefix: 0 for prefix in _PROVENANCE_TIER_PREFIX.values()}
    values = {prefix: 0.0 for prefix in _PROVENANCE_TIER_PREFIX.values()}
    for ln in derivation_lines:
        tier = ln.get("provenance_tier")
        prefix = _PROVENANCE_TIER_PREFIX.get(tier)
        if prefix is None:
            continue
        counts[prefix] += 1
        values[prefix] = round(values[prefix] + (ln.get("total") or 0.0), 2)

    out: dict[str, Any] = {}
    for prefix in ("researched", "bid_calibrated", "derived", "estimate"):
        out[f"{prefix}_lines"] = counts[prefix]
        out[f"{prefix}_value"] = round(values[prefix], 2)
    out["researched_share_of_grand_total"] = (
        round(values["researched"] / grand_total, 4) if grand_total else 0.0
    )
    return out


def _build_patient_cross_checks(budget: Budget, rollup: dict, warnings: list[str]) -> list[dict]:
    """SPEC §2.4 — top-down validations against real published benchmarks.
    Failing checks (and #5-#7, which have no band) are appended to
    `warnings` IN PLACE; the caller's derivation["warnings"] already holds
    this same list object."""
    grand_total = rollup.get("grand_total") or 0.0
    checks: list[dict] = []

    def _add(check_id: str, value: float, band: tuple[float, float], source_id: str, fail_message: str) -> None:
        lo, hi = band
        passed = lo <= value <= hi
        entry = {"id": check_id, "value": round(value, 4), "band": [lo, hi], "pass": passed, "source_id": source_id}
        if not passed:
            entry["message"] = fail_message
            warnings.append(fail_message)
        checks.append(entry)

    if grand_total > 0:
        c4_total = _category_subtotal_by_name(budget, "Clinical Site Management and Monitoring")
        monitoring_travel = _sum_line_totals(budget, {"pt.monitoring_travel"})
        site_monitoring_share = (c4_total + monitoring_travel) / grand_total
        _add(
            "site_monitoring_share", site_monitoring_share, (0.09, 0.14), "src.erg_aspe_2014",
            f"Site monitoring is {site_monitoring_share:.1%} of the grand total, outside the "
            "9-14% band HHS/ASPE cost studies observe (Sertkaya et al., 2014).",
        )

        central_lab_share = _category_subtotal_by_name(budget, "Central Laboratory") / grand_total
        _add(
            "central_lab_share", central_lab_share, (0.04, 0.12), "src.erg_aspe_2014",
            f"Central lab is {central_lab_share:.1%} of the grand total, outside the 4-12% "
            "band HHS/ASPE cost studies observe (Sertkaya et al., ERG Table 2).",
        )

        rtsm_total = _sum_line_totals(budget, {"dm.rtsm_setup", "dm.rtsm_run", "dm.rtsm_close"})
        rtsm_share = rtsm_total / grand_total
        _add(
            "rtsm_share", rtsm_share, (0.02, 0.03), "src.suvoda_rtsm_2026",
            f"RTSM/IRT is {rtsm_share:.1%} of the grand total, below the 2-3% of total study "
            "budget Suvoda's 2026 benchmark reports for Phase II/III trials — the bid-derived "
            "RTSM rates here sit below the vendor-published benchmark.",
        )

        ig_share = (rollup.get("investigator_grants_total") or 0.0) / grand_total
        _add(
            "investigator_grant_share", ig_share, (0.25, 0.30), "src.kshb012_bids",
            f"Investigator grants are {ig_share:.1%} of the grand total, outside the 25-30% "
            "band all three reference CRO bids fall in.",
        )

    # #5 — per-patient grant floor, US-only geography only (no band; not a
    # cross_checks entry).
    ig_line = next(
        (li for c in budget.categories for li in c.line_items if li.derivation_key == "ig.per_patient"),
        None,
    )
    if (
        ig_line is not None and ig_line.unit_cost is not None
        and budget.geography_profile == "us_only" and ig_line.unit_cost < 25000
    ):
        warnings.append(
            f"The per-patient investigator grant (${ig_line.unit_cost:,.2f}) is below "
            "$25,000 for a us_only geography profile — likely to under-fund a US site."
        )

    # #6 — always emitted.
    warnings.append(
        "Peer-reviewed planned-vs-actual data show IMP supply costs run ~34% above plan at "
        "the median (worst cases 180-400%). Consider a +25% contingency on Clinical Supply, "
        "Depot and Logistics."
    )

    # #7 — always emitted for oncology.
    if budget.therapeutic_area == "oncology":
        warnings.append(
            "Protocol amendments are common in oncology trials, so the amendment-handling "
            "line item is budgeted as near-certain here (quantity 1 per site), not as a "
            "contingency — no specific, independently verifiable amendment-rate statistic "
            "backs this beyond that general judgement call."
        )

    return checks


PATIENT_BID_RECONCILIATION_STATIC: dict[str, Any] = {
    "reference_study": "Kashiv Biosciences KSHB012-101 (nivolumab biosimilar, Phase I-II, "
                        "melanoma / oesophageal cancer)",
    "bids": [
        {"cro": "Novotech", "grand_total": 11701368},
        {"cro": "PPD/Emmes", "grand_total": 13701349, "note": "after 8% discount"},
        {"cro": "IQVIA", "grand_total": None, "note": "region-stratified; comparable"},
    ],
    "explained_by": [
        "Site ISF archiving priced at 675 per site PER YEAR for 15 years, not as a single "
        "~600 fee (Emory 2025 and Utah FY2027 independently; Utah invoices 15 years at "
        "close-out when the contract is silent). None of the three bids contains this.",
        "Site-side monitoring-visit hosting fees, which the site charges the sponsor "
        "separately from the CRO's CRA fee (Utah 918/day, LSU 1,000/day, Lurie 1,350/day). "
        "Absent from all three bids.",
        "Imaging site activation and blinded independent central review. No bid contains an "
        "imaging line despite a RECIST-driven oncology design.",
        "Annual sponsor-equipment calibration and inspection at 737 per site per year "
        "(Johns Hopkins FY2024).",
    ],
    "disclaimer": (
        "This estimate is deliberately the UNION of what three CROs each priced plus items "
        "the research showed all three omit. It is a sponsor-side planning figure, not a "
        "bid, and it is expected to sit above any single CRO's quotation."
    ),
}


def _build_bid_reconciliation(grand_total: float) -> dict[str, Any]:
    bid_totals = [b["grand_total"] for b in PATIENT_BID_RECONCILIATION_STATIC["bids"] if b["grand_total"]]
    highest_bid = max(bid_totals) if bid_totals else None
    delta_pct = round((grand_total - highest_bid) / highest_bid * 100.0, 1) if highest_bid else None
    return {
        **PATIENT_BID_RECONCILIATION_STATIC,
        "this_estimate": grand_total,
        "delta_vs_highest_bid_pct": delta_pct,
    }


def auto_populate_budget(
    budget: Budget,
    protocol: Protocol | None = None,
    *,
    mode: str = "create",
    precedent: BudgetPrecedentResult | None = None,
) -> dict:
    """Fully price every category of `budget` from the rate card, using the
    linked protocol's real schedule for quantities when one exists, this
    drug's own real ClinicalTrials.gov trial history (`precedent`) next, and
    the per-study-type defaults only when neither is available.

    SYNCHRONOUS, mutates the loaded ORM tree in place; the caller commits.
    `precedent` is resolved by the caller BEFORE this call (it requires an
    async HTTP fetch — see routers/budgets.py's
    _fetch_budget_trial_precedent_safe) and defaults to NO_PRECEDENT, which
    reproduces the exact pre-existing generic-default behaviour.

    PRECONDITION: budget.categories (and each .line_items) eagerly loaded;
    `protocol`, if given, fetched with load_drug=True.

    Returns the derivation dict (also assigned to budget.derivation as a NEW
    dict — never mutated in place, so SQLAlchemy's JSONB change tracking
    actually sees it).
    """
    precedent = precedent or NO_PRECEDENT
    if protocol is not None:
        budget.protocol_id = protocol.id

    # Snapshot BEFORE _resolve_drivers/reconciliation touch anything — this is
    # the fingerprint reconcile mode uses to tell "untouched" (still equals
    # what the LAST run auto-priced it at) from "the user has since edited
    # it", per derivation_key. Reading it late (e.g. after a category has
    # already been reconciled) would compare a row against ITS OWN new price
    # instead of the price it actually shipped with.
    previous_derivation = budget.derivation or {}
    previous_auto_priced: dict[str, float] = previous_derivation.get("auto_priced", {})
    previous_assumptions: dict = previous_derivation.get("assumptions", {})

    drivers, assumptions, warnings, phases_block = _resolve_drivers(
        budget, protocol, previous_assumptions, precedent,
    )
    protocol_linked = protocol is not None
    is_patient = drivers.subject_profile == "patient"

    line_keys: list[str] = []
    auto_priced: dict[str, float] = {}
    derivation_lines: list[dict] = []

    if not is_patient:
        # ===================================================================
        # HEALTHY-VOLUNTEER PATH — byte-for-byte the original algorithm. Not
        # refactored to share code with the patient path below, deliberately:
        # this is the one code path that MUST keep producing exactly the
        # numbers it produces today (spec hard requirement).
        # ===================================================================

        # -- Clinical Conduct (the one special category) ---------------------
        # Per-row protocol_derived vs rate_card is judged from each specific
        # driver's OWN resolved source, not from whether a protocol is merely
        # linked — a linked protocol with no computed schedule still falls
        # back to defaults for these, and such a row is a rate-card estimate.
        conduct_rows, conduct_extra = build_clinical_conduct_rows(
            drivers,
            protocol_linked,
            pk_from_protocol=assumptions.get("n_pk_timepoints", {}).get("source") == "protocol",
            ada_from_protocol=assumptions.get("n_ada_timepoints", {}).get("source") == "protocol",
            visit_days_from_protocol=assumptions.get("n_visit_days", {}).get("source") == "protocol",
        )
        warnings.extend(conduct_extra["warnings"])

        conduct_cat = next((c for c in budget.categories if c.name == CLINICAL_CONDUCT_NAME), None)
        if conduct_cat is None:
            conduct_cat = BudgetCategory(
                id=uuid.uuid4(), budget_id=budget.id, name=CLINICAL_CONDUCT_NAME,
                bucket=BudgetBucket.direct,
                sort_order=(max((c.sort_order for c in budget.categories), default=0) + 1),
            )
            budget.categories.append(conduct_cat)
            warnings.append(
                "No 'Clinical Conduct' category was found, so one was recreated to hold "
                "the auto-priced conduct rows."
            )

        _reconcile_category(conduct_cat, conduct_rows, mode=mode, previous_auto_priced=previous_auto_priced)
        for r in conduct_rows:
            line_keys.append(r["derivation_key"])
            if r["unit_cost"] is not None:
                auto_priced[r["derivation_key"]] = r["unit_cost"]
            derivation_lines.append(_line_summary(r, CLINICAL_CONDUCT_NAME))

        # -- All 16 other categories -------------------------------------------
        for card_cat in RATE_CARD:
            if card_cat.special == "clinical_conduct":
                continue

            cat = next((c for c in budget.categories if c.name == card_cat.name), None)
            if cat is None:
                cat = BudgetCategory(
                    id=uuid.uuid4(), budget_id=budget.id, name=card_cat.name,
                    bucket=card_cat.bucket,
                    sort_order=card_cat.sort_order,
                )
                budget.categories.append(cat)
                warnings.append(
                    f"No '{card_cat.name}' category was found, so one was recreated to hold "
                    "the rate-card rows."
                )

            rows: list[dict] = []
            order = 0
            for line in card_cat.lines:
                if not should_include(line, drivers):
                    continue
                qty = quantity_for(line, drivers)
                uc = unit_cost_for(line, drivers)

                if line.status == BudgetLineStatus.costed:
                    if qty == 0 and line.qty_basis != "per_study":
                        continue
                    total = round(qty * uc, 2) if uc is not None else None
                    quantity_out, unit_cost_out = qty, uc
                elif line.status == BudgetLineStatus.outsourced:
                    quantity_out, unit_cost_out, total = qty, uc, None
                else:  # actuals_only (rate_card_only basis)
                    quantity_out, unit_cost_out, total = 0.0, uc, None

                order += 1
                rows.append({
                    "derivation_key": line.key,
                    "label": line.label,
                    "quantity": quantity_out,
                    "unit_cost": unit_cost_out,
                    "unit_description": line.unit_description,
                    "total": total,
                    "status": line.status,
                    "source": BudgetLineSource.rate_card,
                    "sort_order": order,
                    "note": line.basis,
                })

            if not rows and card_cat.out_of_scope_line:
                rows.append({
                    "derivation_key": f"{card_cat.name.lower().replace(' ', '_').replace('/', '_')}.out_of_scope",
                    "label": card_cat.out_of_scope_line,
                    "quantity": None,
                    "unit_cost": None,
                    "unit_description": None,
                    "total": None,
                    "status": BudgetLineStatus.out_of_scope,
                    "source": BudgetLineSource.rate_card,
                    "sort_order": 1,
                    "note": None,
                })

            _reconcile_category(cat, rows, mode=mode, previous_auto_priced=previous_auto_priced)
            for r in rows:
                line_keys.append(r["derivation_key"])
                if r["unit_cost"] is not None:
                    auto_priced[r["derivation_key"]] = r["unit_cost"]
                derivation_lines.append(_line_summary(r, card_cat.name))

        conduct_extra_conduct = conduct_extra["conduct"]

    else:
        # ===================================================================
        # PATIENT PATH — SPEC §2: 22 PATIENT_RATE_CARD categories, no Clinical
        # Conduct (that is a healthy-volunteer-clinic concept). Geo-scaling,
        # the comparator-drug opt-in gate, and provenance tiering are all
        # per-line concerns handled here; nothing here can execute for a
        # healthy budget (is_patient is False for every one of them).
        # ===================================================================
        conduct_extra_conduct = {}

        for card_cat in PATIENT_RATE_CARD:
            cat = next((c for c in budget.categories if c.name == card_cat.name), None)
            if cat is None:
                cat = BudgetCategory(
                    id=uuid.uuid4(), budget_id=budget.id, name=card_cat.name,
                    bucket=card_cat.bucket,
                    sort_order=card_cat.sort_order,
                )
                budget.categories.append(cat)
                warnings.append(
                    f"No '{card_cat.name}' category was found, so one was recreated to hold "
                    "the patient rate-card rows."
                )

            rows: list[dict] = []
            extra_by_key: dict[str, dict] = {}
            order = 0
            comparator_drug_total: float | None = None
            for line in card_cat.lines:
                if not should_include(line, drivers):
                    continue
                qty = quantity_for(line, drivers)
                uc = unit_cost_for(line, drivers)
                effective_status = line.status

                # The comparator-drug gate (spec §1.6): comp.drug/comp.agent_fee
                # default actuals_only on the rate card itself; flip to costed
                # only when the sponsor has explicitly opted in.
                if line.key in COMPARATOR_GATED_KEYS and drivers.comparator_sourced_by_sponsor:
                    effective_status = BudgetLineStatus.costed

                if line.key == "comp.agent_fee":
                    # % of comp.drug's OWN computed acquisition value, not a
                    # normal qty_basis x unit_cost line. comp.drug is declared
                    # before comp.agent_fee in the rate card, so its total is
                    # already known by the time this row is reached.
                    if effective_status == BudgetLineStatus.costed and comparator_drug_total:
                        qty = 1.0
                        uc = round(comparator_drug_total * COMPARATOR_AGENT_FEE_PCT / 100.0, 2)
                    else:
                        effective_status = BudgetLineStatus.actuals_only
                        qty, uc = 0.0, uc

                if effective_status == BudgetLineStatus.costed:
                    if qty == 0 and line.qty_basis != "per_study":
                        continue
                    total = round(qty * uc, 2) if uc is not None else None
                    quantity_out, unit_cost_out = qty, uc
                elif effective_status == BudgetLineStatus.outsourced:
                    quantity_out, unit_cost_out, total = qty, uc, None
                else:  # actuals_only / not_planned / out_of_scope / not_applicable
                    quantity_out, unit_cost_out, total = 0.0, uc, None

                if line.key == "comp.drug":
                    comparator_drug_total = total

                order += 1
                rows.append({
                    "derivation_key": line.key,
                    "label": line.label,
                    "quantity": quantity_out,
                    "unit_cost": unit_cost_out,
                    "unit_description": line.unit_description,
                    "total": total,
                    "status": effective_status,
                    "source": _provenance_for_line(line)[0],
                    "sort_order": order,
                    "note": line.basis,
                })

                _, provenance_tier, source_id = _provenance_for_line(line)
                unit_cost_published = None
                geo_factor_out = None
                if line.geo_scaled:
                    geo_factor_out = drivers.geography_factor
                    unit_cost_published = unit_cost_for(
                        line, dataclasses.replace(drivers, geography_factor=1.0)
                    )
                extra_by_key[line.key] = {
                    "unit_cost_published": unit_cost_published,
                    "geography_factor": geo_factor_out,
                    "provenance_tier": provenance_tier,
                    "source_id": source_id,
                }

            if not rows and card_cat.out_of_scope_line:
                rows.append({
                    "derivation_key": f"{card_cat.name.lower().replace(' ', '_').replace('/', '_')}.out_of_scope",
                    "label": card_cat.out_of_scope_line,
                    "quantity": None,
                    "unit_cost": None,
                    "unit_description": None,
                    "total": None,
                    "status": BudgetLineStatus.out_of_scope,
                    "source": BudgetLineSource.rate_card,
                    "sort_order": 1,
                    "note": None,
                })

            _reconcile_category(cat, rows, mode=mode, previous_auto_priced=previous_auto_priced)
            for r in rows:
                line_keys.append(r["derivation_key"])
                if r["unit_cost"] is not None:
                    auto_priced[r["derivation_key"]] = r["unit_cost"]
                derivation_lines.append(
                    _line_summary(r, card_cat.name, extra_by_key.get(r["derivation_key"]))
                )

    # -- Reconcile mode: keys the user removed --------------------------------
    user_removed_keys: list[str] = []
    if mode == "reconcile":
        previous_keys = set(previous_derivation.get("line_keys", []))
        current_row_keys = set(line_keys)
        all_current_ids = {
            li.derivation_key for c in budget.categories for li in c.line_items if li.derivation_key
        }
        for key in previous_keys - current_row_keys:
            if key not in all_current_ids:
                user_removed_keys.append(key)

    if modality_unknown_warning := (drivers.modality == "unknown"):
        warnings.append(
            f"'{drivers.drug_inn or ''}' could not be classified as a biologic or a small "
            "molecule, so the small-molecule / BE defaults were used."
        )
    if budget.currency and budget.currency != "USD":
        warnings.append(
            f"The rate card is denominated in USD. This budget's currency is set to "
            f"{budget.currency}; the figures have NOT been converted."
        )
    defaulted = sorted(k for k, v in assumptions.items() if v.get("source") == "default")
    if defaulted:
        warnings.append(
            "The following were not available from a protocol and used the default "
            f"profile for a {drivers.study_type} study: {', '.join(defaulted)}. Edit any "
            "of them and the affected lines can be re-priced."
        )

    estimated_grand_total = round(
        sum(
            (li.total or 0.0)
            for c in budget.categories for li in c.line_items
            if li.status == BudgetLineStatus.costed
        ),
        2,
    )

    rate_card_version = PATIENT_RATE_CARD_VERSION if is_patient else RATE_CARD_VERSION
    disclaimer_text = PATIENT_DISCLAIMER if is_patient else budget_rate_card.DISCLAIMER

    derivation: dict[str, Any] = {
        "protocol_id": str(protocol.id) if protocol is not None else None,
        "derived_at": datetime.now(timezone.utc).isoformat(),
        "n_enrolled": drivers.n_enrolled,
        "n_pk_timepoints": drivers.n_pk_timepoints,
        "n_ada_timepoints": drivers.n_ada_timepoints,
        "n_visit_days": drivers.n_visit_days,
        "design_key": drivers.design_key,
        "periods": drivers.periods,
        "lines": derivation_lines,
        "warnings": warnings,
        "skipped": False,
        "engine": "budget_auto_population",
        "rate_card_version": rate_card_version,
        "mode": mode,
        "drug_inn": drivers.drug_inn,
        "modality": drivers.modality,
        "study_type": drivers.study_type,
        "complexity": drivers.complexity,
        "currency": budget.currency,
        "protocol_linked": protocol_linked,
        "assumptions": assumptions,
        "conduct": conduct_extra_conduct,
        "line_keys": line_keys,
        "auto_priced": auto_priced,
        "estimated_grand_total": estimated_grand_total,
        "disclaimer": disclaimer_text,
        "discount_note": (
            "No discount has been applied. A discount is a negotiated outcome specific "
            "to your contract and is never estimated on your behalf."
        ),
        # subject_profile is ALSO a plain Budget column (BudgetRead.subject_profile);
        # it is repeated here so the derivation audit trail is self-contained —
        # this key is new but additive for a healthy budget (it was simply
        # absent before) and touches no total, so it does not affect the
        # "healthy output unchanged" guarantee.
        "subject_profile": drivers.subject_profile,
        # Real-trial-history grounding disclosure (services/budget_trial_precedent.py)
        # — always present, even when no precedent was found, so the UI/export
        # can honestly state "no real trial history was found for this drug"
        # rather than silently saying nothing.
        "trial_precedent": {
            "found": precedent.found,
            "trials": [
                {
                    "nct_id": p.nct_id, "title": p.title, "status": p.status, "phase": p.phase,
                    "enrollment_count": p.enrollment_count, "enrollment_type": p.enrollment_type,
                    "n_countries": p.n_countries, "n_locations": p.n_locations,
                    "duration_months": p.duration_months,
                }
                for p in precedent.precedents
            ],
            "median_n_enrolled": precedent.median_n_enrolled,
            "median_n_countries": precedent.median_n_countries,
            "median_n_sites": precedent.median_n_sites,
            "median_duration_months": precedent.median_duration_months,
        },
    }
    if mode == "reconcile" and user_removed_keys:
        derivation["user_removed_keys"] = user_removed_keys

    if is_patient:
        profile_key = phases_block.pop("profile_key", None)
        # budget.discount_type/discount_value can still be None here: a
        # BRAND NEW Budget's Python-side column defaults are only applied by
        # SQLAlchemy at flush/INSERT time, and create_budget calls
        # auto_populate_budget on the in-memory object BEFORE that flush.
        # Normalize rather than crash on `None.value` inside compute_rollups.
        rollup = compute_rollups(
            budget.categories,
            discount_type=budget.discount_type or BudgetDiscountType.none,
            discount_value=budget.discount_value or 0.0,
        )
        sources_out: dict[str, dict] = {}
        for ln in derivation_lines:
            sid = ln.get("source_id")
            if sid and sid not in sources_out and sid in SOURCE_REGISTRY:
                rec = SOURCE_REGISTRY[sid]
                sources_out[sid] = {
                    "tier": rec.tier, "figure": rec.figure, "citation": rec.citation,
                    "url": rec.url, "accessed": rec.accessed, "caveat": rec.caveat,
                }

        derivation.update({
            "therapeutic_area": drivers.therapeutic_area,
            "indication": drivers.indication,
            "profile_key": profile_key,
            "geography_profile": budget.geography_profile,
            "geography_factor": drivers.geography_factor,
            "phases": phases_block,
            "sources": sources_out,
            "provenance_summary": _build_provenance_summary(derivation_lines, rollup["grand_total"]),
            # cross-check warnings are appended to `warnings` in place — the
            # dict above already holds this SAME list object, so no reassignment
            # is needed for "warnings" to pick them up.
            "cross_checks": _build_patient_cross_checks(budget, rollup, warnings),
            "bid_reconciliation": _build_bid_reconciliation(rollup["grand_total"]),
        })

    budget.derivation = derivation
    return derivation


def _line_summary(row: dict, category_name: str, extra: dict | None = None) -> dict:
    """`extra` (patient-card only — spec §4.4/§7.5) carries unit_cost_published /
    geography_factor / provenance_tier / source_id. Omitted entirely (not just
    null-valued) for a healthy-card row, so a healthy budget's derivation["lines"]
    entries are byte-for-byte what they were before this work."""
    out = {
        "derivation_key": row["derivation_key"],
        "label": row["label"],
        "quantity": row["quantity"],
        "unit_description": row["unit_description"],
        "unit_cost": row["unit_cost"],
        "total": row["total"],
        "source": row["source"].value if hasattr(row["source"], "value") else row["source"],
        "category": category_name,
        "basis": row.get("note"),
    }
    if extra is not None:
        out["unit_cost_published"] = extra.get("unit_cost_published")
        out["geography_factor"] = extra.get("geography_factor")
        out["provenance_tier"] = extra.get("provenance_tier")
        out["source_id"] = extra.get("source_id")
    return out


def _reconcile_category(
    cat: BudgetCategory,
    rows: list[dict],
    *,
    mode: str,
    previous_auto_priced: dict[str, float] | None = None,
) -> None:
    """Merge `rows` (plain dicts keyed by derivation_key) into `cat.line_items`.

    mode="create": rows are appended fresh (the category was just seeded, so
    there is nothing to reconcile against).

    mode="reconcile": index existing rows by derivation_key. A key that
    already exists gets its label/quantity/unit_description/note updated
    always; its unit_cost/total are updated ONLY if the existing unit_cost
    still matches `previous_auto_priced[key]` — the price THIS SAME key was
    auto-priced at on the prior run (i.e. it is untouched) — or is None.
    Once the user has typed a different unit_cost, it (and any lump-sum total
    that isn't just quantity*unit_cost) is left alone. A key that no longer
    appears in `rows` is removed if untouched by that same fingerprint, else
    flipped to a manual row with an explanatory note (mirrors the old
    apply_protocol_derivation stale-row rule).

    IMPORTANT: the fingerprint is `previous_auto_priced`, NOT the price this
    run would newly compute for the key — those can legitimately differ (e.g.
    a rate-card row whose price depends on simple/complex study
    classification, and the classification just changed on relink), and
    comparing against the NEW price would misclassify an actually-untouched
    row as user-edited.
    """
    if mode != "reconcile":
        for r in rows:
            cat.line_items.append(
                BudgetLineItem(id=uuid.uuid4(), category_id=cat.id, **r)
            )
        return

    previous_auto_priced = previous_auto_priced or {}
    existing = {li.derivation_key: li for li in cat.line_items if li.derivation_key}
    incoming_keys = {r["derivation_key"] for r in rows}

    def _untouched(li: BudgetLineItem, key: str) -> bool:
        if li.unit_cost is None:
            return True
        prior = previous_auto_priced.get(key)
        return prior is not None and _floats_close(li.unit_cost, prior)

    for r in rows:
        li = existing.get(r["derivation_key"])
        if li is None:
            cat.line_items.append(
                BudgetLineItem(id=uuid.uuid4(), category_id=cat.id, **r)
            )
            continue

        # Snapshot the PRE-update quantity before mutating the row: the
        # arithmetic fingerprint below asks whether the line's OLD total
        # still equals OLD quantity x OLD unit_cost (i.e. nobody hand-typed
        # a lump-sum total that doesn't match the formula). Using the
        # already-overwritten NEW quantity here would make that comparison
        # spuriously fail on every quantity-only change (e.g. n_sites),
        # since NEW quantity x OLD unit_cost almost never equals the OLD
        # total — silently freezing `total` (and therefore every rollup and
        # the /reprice endpoint) even though the line was never touched by
        # hand.
        old_quantity = li.quantity

        li.label = r["label"]
        li.quantity = r["quantity"]
        li.unit_description = r["unit_description"]
        li.note = r["note"]

        if _untouched(li, r["derivation_key"]):
            old_total_matched_arithmetic = (
                li.total is None
                or (old_quantity is not None and li.unit_cost is not None
                    and _floats_close(li.total, (old_quantity or 0.0) * (li.unit_cost or 0.0)))
            )
            li.unit_cost = r["unit_cost"]
            if old_total_matched_arithmetic:
                li.total = r["total"]
            li.status = r["status"]

    for key, li in existing.items():
        if key in incoming_keys:
            continue
        if _untouched(li, key):
            cat.line_items.remove(li)
        else:
            li.source = BudgetLineSource.manual
            li.derivation_key = None
            li.note = (
                (li.note or "")
                + " [No longer produced by the current rate-card run; kept because it "
                "carries a price you entered.]"
            ).strip()


def _floats_close(a: float | None, b: float | None, tol: float = 0.005) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(float(a) - float(b)) <= tol


def apply_protocol_derivation(budget: Budget, protocol: Protocol) -> dict:
    """Thin wrapper kept for routers/budgets.py::link_protocol — re-runs the
    full auto-population in "reconcile" mode so linking/re-linking never
    clobbers a price the user already typed."""
    return auto_populate_budget(budget, protocol, mode="reconcile")


def category_subtotal(category: BudgetCategory) -> float:
    """Sum of `total` over the category's COSTED lines only. A line with a
    null total contributes 0; a non-costed line (out_of_scope/actuals_only/...)
    contributes nothing at all, which is why the CRO-Variable and Pass-Through
    rate-card categories normally subtotal to 0."""
    return round(
        sum(
            (li.total or 0.0)
            for li in category.line_items
            if li.status == BudgetLineStatus.costed
        ),
        2,
    )


def compute_rollups(
    categories: Sequence[BudgetCategory],
    *,
    discount_type: BudgetDiscountType = BudgetDiscountType.none,
    discount_value: float = 0.0,
) -> dict[str, Any]:
    """Compute the budget's rollup totals.

    RULES (do not "improve"):
    - The discount applies to the DIRECT bucket only, never to stipend/
      variable/pass-through. That is how the signed reference documents
      compute it.
    - A flat discount is NOT clamped to direct_subtotal and grand_total is NOT
      clamped at 0 — a negotiated flat credit larger than the directs is a
      real (if unusual) thing, and silently clamping would hide a data-entry
      error.
    - Nothing here is persisted. Rollups are computed on every read, so they
      can never go stale relative to the line items.
    """
    subtotals = {str(c.id): category_subtotal(c) for c in categories}

    def _bucket_sum(b: BudgetBucket) -> float:
        return sum(subtotals[str(c.id)] for c in categories if c.bucket == b)

    direct_subtotal = round(_bucket_sum(BudgetBucket.direct), 2)

    value = float(discount_value or 0.0)
    if discount_type == BudgetDiscountType.percent:
        discount_amount = round(direct_subtotal * (value / 100.0), 2)
    elif discount_type == BudgetDiscountType.flat:
        discount_amount = round(value, 2)
    else:
        discount_amount = 0.0

    total_direct_costs = round(direct_subtotal - discount_amount, 2)
    subject_stipend_total = round(_bucket_sum(BudgetBucket.stipend), 2)
    variable_total = round(_bucket_sum(BudgetBucket.variable), 2)
    pass_through_total = round(_bucket_sum(BudgetBucket.pass_through), 2)
    # NEW buckets (spec §2.0/§7.5) — always 0.00 for a healthy-volunteer
    # budget (no healthy-card category ever uses either bucket), so adding
    # them to grand_total changes no existing total.
    investigator_grants_total = round(_bucket_sum(BudgetBucket.investigator_grants), 2)
    software_total = round(_bucket_sum(BudgetBucket.software), 2)
    grand_total = round(
        total_direct_costs + subject_stipend_total + variable_total + pass_through_total
        + investigator_grants_total + software_total,
        2,
    )

    return {
        "direct_subtotal": direct_subtotal,
        "discount_type": discount_type.value,
        "discount_value": value,
        "discount_amount": discount_amount,
        "total_direct_costs": total_direct_costs,
        "subject_stipend_total": subject_stipend_total,
        "variable_total": variable_total,
        "pass_through_total": pass_through_total,
        "investigator_grants_total": investigator_grants_total,
        "software_total": software_total,
        "grand_total": grand_total,
        "category_subtotals": subtotals,
    }


def line_total_from_units(quantity: float | None, unit_cost: float | None) -> float | None:
    """Convenience used ONLY when the caller omitted `total` entirely.

    The server NEVER force-derives total when the client supplied the field:
    many real budget rows are lump sums with no unit arithmetic at all.
    """
    if quantity is None or unit_cost is None:
        return None
    return round(float(quantity) * float(unit_cost), 2)
