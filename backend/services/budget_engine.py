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
    Protocol,
)
from services import budget_rate_card
from services.budget_rate_card import (
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
    RATE_CARD,
    RATE_CARD_VERSION,
    StudyDrivers,
    quantity_for,
    should_include,
    unit_cost_for,
)
from services.design_decisions import classify_drug_modality

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


def seed_default_categories(budget_id: uuid.UUID) -> list[BudgetCategory]:
    """Build the 17 default BudgetCategory rows for a new budget.

    Returns detached ORM objects; the caller db.add_all()s them.
    """
    return [
        BudgetCategory(id=uuid.uuid4(), budget_id=budget_id, name=name,
                       bucket=bucket, sort_order=i)
        for i, (name, bucket) in enumerate(DEFAULT_CATEGORIES, start=1)
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


def _resolve_drivers(
    budget: Budget, protocol: Protocol | None, previous_assumptions: dict | None = None,
) -> tuple[StudyDrivers, dict, list[str]]:
    """STEPS 1-2 of the auto-population algorithm. Returns (drivers,
    assumptions, warnings). Mutates budget's identity/assumption fields IN
    PLACE per the "write every resolved driver back" rule (only when the field
    is not already user-authoritative — see _is_user_authoritative — is it
    re-derived from the protocol/default; a real user value is never
    clobbered)."""
    warnings: list[str] = []
    assumptions: dict[str, dict] = {}
    previous_assumptions = previous_assumptions or {}

    # --- Step 1: identity -------------------------------------------------
    drug_inn = (budget.drug_inn or "").strip() or (
        protocol.drug.inn if protocol is not None and protocol.drug else None
    )
    budget.drug_inn = drug_inn

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
        else:
            n_enrolled = int(profile["n_enrolled"])
            assumptions["n_enrolled"] = {
                "value": n_enrolled, "source": "default",
                "basis": f"Default {study_type} study profile (no protocol or user value).",
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

    # n_alternates
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
    else:
        n_sites = int(profile["n_sites"])
        assumptions["n_sites"] = {"value": n_sites, "source": "default", "basis": f"Default {study_type} study profile."}
    budget.n_sites = n_sites

    # n_dosing_groups
    n_dosing_groups = budget.n_dosing_groups
    if _is_user_authoritative(n_dosing_groups, "n_dosing_groups", previous_assumptions):
        assumptions["n_dosing_groups"] = {"value": n_dosing_groups, "source": "user", "basis": "Supplied directly."}
    else:
        n_dosing_groups = int(profile["n_dosing_groups"])
        assumptions["n_dosing_groups"] = {
            "value": n_dosing_groups, "source": "default", "basis": f"Default {study_type} study profile.",
        }
    budget.n_dosing_groups = n_dosing_groups

    # project_duration_months
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
        doses_per_subject = int(profile["doses_per_subject"])
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

    # complexity
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

    drivers = StudyDrivers(
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
    )
    assumptions["complexity"] = {
        "value": complexity, "source": "computed",
        "basis": "biosimilar_pk study type, or biologic modality, or project duration "
                 ">= 9 months.",
    }
    assumptions["modality"] = {
        "value": modality, "source": "computed",
        "basis": "services.design_decisions.classify_drug_modality(drug_inn).",
    }
    assumptions["doses_per_subject"]  # already set above

    return drivers, assumptions, warnings


def auto_populate_budget(
    budget: Budget,
    protocol: Protocol | None = None,
    *,
    mode: str = "create",
) -> dict:
    """Fully price every category of `budget` from the rate card, using the
    linked protocol's real schedule for quantities when one exists and the
    per-study-type defaults otherwise.

    SYNCHRONOUS, mutates the loaded ORM tree in place; the caller commits.

    PRECONDITION: budget.categories (and each .line_items) eagerly loaded;
    `protocol`, if given, fetched with load_drug=True.

    Returns the derivation dict (also assigned to budget.derivation as a NEW
    dict — never mutated in place, so SQLAlchemy's JSONB change tracking
    actually sees it).
    """
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

    drivers, assumptions, warnings = _resolve_drivers(budget, protocol, previous_assumptions)
    protocol_linked = protocol is not None

    line_keys: list[str] = []
    auto_priced: dict[str, float] = {}
    derivation_lines: list[dict] = []

    # -- Clinical Conduct (the one special category) ------------------------
    # Per-row protocol_derived vs rate_card is judged from each specific
    # driver's OWN resolved source, not from whether a protocol is merely
    # linked — a linked protocol with no computed schedule still falls back
    # to defaults for these, and such a row is a rate-card estimate.
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

    # -- All 16 other categories ---------------------------------------------
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
        "rate_card_version": RATE_CARD_VERSION,
        "mode": mode,
        "drug_inn": drivers.drug_inn,
        "modality": drivers.modality,
        "study_type": drivers.study_type,
        "complexity": drivers.complexity,
        "currency": budget.currency,
        "protocol_linked": protocol_linked,
        "assumptions": assumptions,
        "conduct": conduct_extra["conduct"],
        "line_keys": line_keys,
        "auto_priced": auto_priced,
        "estimated_grand_total": estimated_grand_total,
        "disclaimer": budget_rate_card.DISCLAIMER,
        "discount_note": (
            "No discount has been applied. A discount is a negotiated outcome specific "
            "to your contract and is never estimated on your behalf."
        ),
    }
    if mode == "reconcile" and user_removed_keys:
        derivation["user_removed_keys"] = user_removed_keys

    budget.derivation = derivation
    return derivation


def _line_summary(row: dict, category_name: str) -> dict:
    return {
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

        li.label = r["label"]
        li.quantity = r["quantity"]
        li.unit_description = r["unit_description"]
        li.note = r["note"]

        if _untouched(li, r["derivation_key"]):
            old_total_matched_arithmetic = (
                li.total is None
                or (li.quantity is not None and li.unit_cost is not None
                    and _floats_close(li.total, (li.quantity or 0.0) * (li.unit_cost or 0.0)))
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
    grand_total = round(
        total_direct_costs + subject_stipend_total + variable_total + pass_through_total,
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
