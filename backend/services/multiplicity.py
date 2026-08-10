"""Multiplicity handling for co-primary endpoints — alpha and power, together.

WHY THIS EXISTS
---------------
A biosimilar PK-similarity study in patients does not have one primary
endpoint. It has several that must ALL succeed: first-dose exposure and
steady-state exposure, each of which may itself be assessed on AUC (and, where
specified, Cmax). "Both endpoints need to pass" is the design. That structure
changes the statistics in a way that is easy to get backwards, and getting it
backwards mis-sizes the study in one direction or the other, so this module
computes it once, explicitly, and hands the result to the sample-size engine as
a locked fact.

THE TWO ERROR RATES MOVE IN OPPOSITE DIRECTIONS
-----------------------------------------------
This is the whole point of the module, and the part that is routinely
mishandled:

  TYPE I ERROR (alpha) — NOT inflated, so NOT adjusted.
      When every co-primary endpoint must pass for the study to succeed, the
      decision rule is an INTERSECTION-UNION TEST (IUT; Berger 1982). The null
      hypothesis of the overall test is the UNION of the individual nulls, so
      the study can only make a false-positive claim if EVERY endpoint makes
      one simultaneously. The overall type I error is therefore bounded ABOVE
      by the largest individual alpha - it does not accumulate the way it does
      when any one of several endpoints could win. Each endpoint is tested at
      the full alpha = 0.05 (the conventional 90% confidence interval), with no
      Bonferroni or other split.

      This is not a shortcut. Splitting alpha here would be an ERROR in the
      conservative direction: it would tighten every confidence interval for a
      multiplicity that the IUT has already controlled, and inflate the sample
      size for no gain in type I error protection. Both FDA and EMA accept the
      unadjusted 90% CI on each co-primary PK endpoint for exactly this reason,
      and this is the same convention already applied to the standard
      Cmax + AUC co-primary pair in a conventional bioequivalence study.

  TYPE II ERROR (power) — IS inflated, so IT is what gets adjusted.
      The mirror image. Because EVERY endpoint has to pass, the probability
      that all of them do is at most the probability that any single one does,
      and is strictly lower whenever the endpoints are less than perfectly
      correlated. Powering each endpoint individually at 90% therefore does NOT
      give a 90% chance of the study succeeding - with two independent
      endpoints it gives about 81%, with four about 66%. To hold OVERALL power
      at the required 90%, the PER-ENDPOINT power must be raised above 90%, and
      the sample size computed from that raised value.

So: alpha is deliberately left unadjusted and the reason is stated; power is
adjusted upward and the study is sized on the adjusted value. Both decisions
are recorded on the result so the statistical-analysis section narrates the
actual reasoning rather than asserting a convention.

CORRELATION
-----------
Exposure endpoints are positively correlated (a subject with high first-dose
AUC tends to have high steady-state AUC), and correlation REDUCES the power
penalty - at perfect correlation there is effectively one endpoint and no
penalty at all. The default assumption here is INDEPENDENCE (rho = 0), which is
the conservative choice: it over-states the penalty, so the study is never
under-powered by this module. A caller who can justify a correlation from prior
data may supply one, and the assumption is then reported explicitly as an
assumption.

Follows the deterministic-engine pattern: computed in Python once, injected as
locked facts, never re-derived by the model.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional


# Decision structures for the co-primary rule.
#   "iut"   - every endpoint must pass (intersection-union). Alpha unadjusted.
#   "union" - the claim succeeds if ANY endpoint passes. Alpha IS split, because
#             here the type I error genuinely does accumulate.
TEST_STRUCTURE_IUT = "iut"
TEST_STRUCTURE_UNION = "union"

DEFAULT_OVERALL_POWER = 0.90
DEFAULT_ALPHA = 0.05

# Per-endpoint power is never raised above this. A required per-endpoint power
# arbitrarily close to 1 makes N diverge, which is a sign the endpoint set is
# too large to power honestly rather than a sample size anyone should enrol.
_MAX_PER_ENDPOINT_POWER = 0.995


@dataclass
class MultiplicityPlan:
    n_co_primary: int
    endpoints: list[str]
    test_structure: str
    overall_power: float
    per_endpoint_power: float
    alpha: float
    alpha_per_endpoint: float
    alpha_adjusted: bool
    correlation_assumed: float
    effective_independent_endpoints: float
    ci_pct: float
    alpha_rationale: str = ""
    power_rationale: str = ""
    capped: bool = False
    # True when the sample size was actually sized UP so that OVERALL power hits
    # `overall_power`. False when `per_endpoint_power` is the conventional
    # per-endpoint figure and `overall_power` is instead the independence-based
    # LOWER BOUND that figure implies — the two cases must render differently,
    # or a conventional bioequivalence study reads as though it were powered at
    # 81% when it is powered exactly as the guidance intends.
    power_adjusted: bool = True
    assumptions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _effective_endpoints(k: int, correlation: float) -> float:
    """How many INDEPENDENT endpoints k correlated endpoints behave like.

    Interpolates between the two anchors that are known exactly:
      rho = 0 -> k   (independent; the full product-rule penalty applies)
      rho = 1 -> 1   (perfectly correlated; one endpoint, no penalty at all)
    Linear in rho between them, which is monotone, never exceeds k, and never
    drops below 1 - so the adjustment can neither over-shoot independence nor
    invent a penalty where none exists.
    """
    rho = min(max(float(correlation), 0.0), 1.0)
    return 1.0 + (k - 1) * (1.0 - rho)


def required_per_endpoint_power(
    n_co_primary: int,
    overall_power: float = DEFAULT_OVERALL_POWER,
    correlation: float = 0.0,
    test_structure: str = TEST_STRUCTURE_IUT,
) -> float:
    """Per-endpoint power needed for OVERALL power to reach ``overall_power``.

    The two decision rules move the required per-endpoint power in OPPOSITE
    directions, so the structure must be known before computing it:

      IUT (every endpoint must pass) - overall = per_endpoint ** k under
      independence, so per_endpoint = overall ** (1/k): the per-endpoint
      figure must be RAISED above the overall target, because passing gets
      harder as more independent hurdles are added.

      UNION (any endpoint passing is enough) - overall = 1 - (1-per_endpoint)
      ** k, so per_endpoint = 1 - (1-overall) ** (1/k): the per-endpoint
      figure must be LOWERED below the overall target, because succeeding
      gets easier as more independent chances are added. Applying the IUT
      formula here would raise per-endpoint power when it should fall,
      oversizing the study for no statistical reason.

    Correlation shrinks the exponent toward 1 via _effective_endpoints in
    both cases (less independent chances/hurdles as correlation rises).
    """
    k = max(int(n_co_primary), 1)
    if k == 1:
        return float(overall_power)
    k_eff = _effective_endpoints(k, correlation)
    if test_structure == TEST_STRUCTURE_UNION:
        return 1.0 - (1.0 - float(overall_power)) ** (1.0 / k_eff)
    return float(overall_power) ** (1.0 / k_eff)


def plan_co_primary(
    *,
    endpoints: list[str],
    overall_power: float = DEFAULT_OVERALL_POWER,
    alpha: float = DEFAULT_ALPHA,
    correlation: float = 0.0,
    test_structure: str = TEST_STRUCTURE_IUT,
) -> MultiplicityPlan:
    """Resolve alpha AND power for a co-primary endpoint set.

    ``endpoints`` is the ordered list of co-primary endpoint names (e.g.
    first-dose AUC and steady-state AUC0-tau). A single-endpoint list is
    handled correctly and yields no adjustment of either kind.
    """
    names = [e for e in (endpoints or []) if str(e).strip()]
    k = max(len(names), 1)
    assumptions: list[str] = []

    # --- alpha -----------------------------------------------------------
    if test_structure == TEST_STRUCTURE_UNION:
        alpha_each = alpha / k
        alpha_adjusted = True
        alpha_rationale = (
            f"The claim succeeds if ANY of the {k} endpoints passes, so the type I error "
            f"accumulates across them and MUST be controlled. A Bonferroni split is applied: "
            f"each endpoint is tested at alpha = {alpha_each:.4f} "
            f"(= {alpha:.2f} / {k}), giving a {100 * (1 - 2 * alpha_each):.2f}% confidence "
            f"interval per endpoint and preserving the family-wise error rate at {alpha:.2f}."
        )
    else:
        alpha_each = alpha
        alpha_adjusted = False
        if k == 1:
            alpha_rationale = (
                f"A single primary endpoint — no multiplicity arises, and each two one-sided "
                f"test is performed at alpha = {alpha:.2f} (90% confidence interval)."
            )
        else:
            alpha_rationale = (
                f"All {k} co-primary endpoints must pass for similarity to be concluded, so the "
                f"decision rule is an intersection-union test. The overall null is the UNION of "
                f"the individual nulls, so a false-positive conclusion requires every endpoint to "
                f"err simultaneously; the family-wise type I error is therefore bounded above by "
                f"alpha = {alpha:.2f} WITHOUT any adjustment. Each endpoint is accordingly tested "
                f"at the full alpha = {alpha:.2f} (90.00% confidence interval), and no Bonferroni "
                f"or other alpha split is applied — splitting alpha here would tighten every "
                f"interval for a multiplicity the intersection-union test has already controlled, "
                f"inflating the sample size with no gain in type I error protection."
            )

    # --- power -----------------------------------------------------------
    # Kept alongside `overall_power` (which the capped branch below
    # reassigns to the achieved ceiling) so the k>1 IUT rationale can still
    # contrast what was ORIGINALLY requested against what was ACTUALLY
    # achieved, rather than reusing the same variable for both and producing
    # self-referential prose ("powering each at 86% would deliver only 1%,
    # not 86%") in the capped case.
    requested_overall_power = float(overall_power)
    per_endpoint = required_per_endpoint_power(k, overall_power, correlation, test_structure)
    capped = False
    k_eff = _effective_endpoints(k, correlation)
    if test_structure != TEST_STRUCTURE_UNION and per_endpoint > _MAX_PER_ENDPOINT_POWER:
        per_endpoint = _MAX_PER_ENDPOINT_POWER
        capped = True
        # The requested overall-power TARGET is not actually reachable at the
        # cap. Reporting `overall_power` as the original (unreachable) request
        # would contradict `power_adjusted`'s own contract — that the sample
        # size was "sized UP so that OVERALL power hits `overall_power`" — so
        # the field is corrected here to the true ceiling achievable at the
        # capped per-endpoint power, and that ceiling is what gets sized to
        # and reported everywhere downstream.
        achievable_overall = per_endpoint ** k_eff
        assumptions.append(
            f"The per-endpoint power required to reach {overall_power * 100:.0f}% overall power "
            f"across {k} co-primary endpoints exceeded {_MAX_PER_ENDPOINT_POWER * 100:.1f}% and "
            f"was capped there. The achieved OVERALL power is therefore {achievable_overall * 100:.2f}%, "
            f"short of the {overall_power * 100:.0f}% originally requested — reduce the number of "
            f"co-primary endpoints or accept the lower overall power explicitly."
        )
        overall_power = achievable_overall

    if k == 1:
        power_rationale = (
            f"A single primary endpoint, so the study is powered directly at "
            f"{overall_power * 100:.0f}%."
        )
    elif test_structure == TEST_STRUCTURE_UNION:
        naive = 1.0 - (1.0 - float(overall_power)) ** k
        power_rationale = (
            f"Because the claim succeeds if ANY of the {k} endpoints passes, powering each "
            f"endpoint at {overall_power * 100:.0f}% would deliver about {naive * 100:.0f}% "
            f"OVERALL power — MORE than the {overall_power * 100:.0f}% target, since each "
            f"additional endpoint is another independent chance to succeed. To hold overall "
            f"power at exactly {overall_power * 100:.0f}% without oversizing the study, each "
            f"endpoint is instead powered at the LOWER figure of {per_endpoint * 100:.2f}%, and "
            f"the sample size is computed from that value. "
        )
        if correlation > 0:
            power_rationale += (
                f"A correlation of {correlation:.2f} between the endpoints is assumed, so the "
                f"{k} endpoints behave like {k_eff:.2f} independent ones."
            )
        else:
            power_rationale += (
                "The endpoints are conservatively treated as INDEPENDENT for this calculation."
            )
    elif capped:
        naive = requested_overall_power ** k
        power_rationale = (
            f"Because ALL {k} co-primary endpoints must pass, the probability that the study "
            f"succeeds is the probability that every one of them does. Powering each endpoint "
            f"at {requested_overall_power * 100:.0f}% would therefore deliver only about "
            f"{naive * 100:.0f}% OVERALL power, not {requested_overall_power * 100:.0f}%. The "
            f"per-endpoint power needed to reach {requested_overall_power * 100:.0f}% overall "
            f"power was capped at {per_endpoint * 100:.2f}% (see the capping note below), so the "
            f"study is instead sized to the ACHIEVED ceiling of {overall_power * 100:.2f}% "
            f"overall power. "
        )
    else:
        naive = float(overall_power) ** k
        power_rationale = (
            f"Because ALL {k} co-primary endpoints must pass, the probability that the study "
            f"succeeds is the probability that every one of them does. Powering each endpoint "
            f"at {overall_power * 100:.0f}% would therefore deliver only about "
            f"{naive * 100:.0f}% OVERALL power, not {overall_power * 100:.0f}%. To hold overall "
            f"power at {overall_power * 100:.0f}%, each endpoint is powered at "
            f"{per_endpoint * 100:.2f}%, and the sample size is computed from that value. "
        )
        if correlation > 0:
            power_rationale += (
                f"A correlation of {correlation:.2f} between the endpoints is assumed, so the "
                f"{k} endpoints behave like {k_eff:.2f} independent ones and the penalty is "
                f"smaller than under full independence."
            )
        else:
            power_rationale += (
                "The endpoints are conservatively treated as INDEPENDENT; exposure endpoints "
                "are in reality positively correlated, so the true overall power will be at "
                "least the stated value and this assumption cannot under-power the study."
            )

    if correlation > 0:
        assumptions.append(
            f"An inter-endpoint correlation of {correlation:.2f} is assumed when converting "
            f"overall power to per-endpoint power. It must be justified from prior data; if it "
            f"is over-stated the study is under-powered."
        )

    return MultiplicityPlan(
        n_co_primary=k,
        endpoints=names or ["Primary endpoint"],
        test_structure=test_structure,
        overall_power=float(overall_power),
        per_endpoint_power=round(per_endpoint, 6),
        alpha=float(alpha),
        alpha_per_endpoint=round(alpha_each, 6),
        alpha_adjusted=alpha_adjusted,
        correlation_assumed=float(correlation),
        effective_independent_endpoints=round(k_eff, 4),
        ci_pct=round(100 * (1 - 2 * alpha_each), 2),
        alpha_rationale=alpha_rationale,
        power_rationale=power_rationale,
        capped=capped,
        assumptions=assumptions,
    )


def multiplicity_directive(plan: Optional[dict[str, Any]]) -> str:
    """Render the multiplicity plan as LOCKED facts for the generation prompt."""
    if not plan or not plan.get("n_co_primary"):
        return ""
    k = plan["n_co_primary"]
    is_union = plan.get("test_structure") == TEST_STRUCTURE_UNION
    lines = [
        "\nCO-PRIMARY ENDPOINT MULTIPLICITY - ALREADY DETERMINED. Do NOT re-derive,",
        "do NOT apply a different alpha, and do NOT restate the power differently.",
        (
            f"- Co-primary endpoints ({k}), ANY ONE of which is sufficient for similarity to be "
            f"concluded:"
            if is_union else
            f"- Co-primary endpoints ({k}), ALL of which must pass for similarity to be concluded:"
        ),
    ]
    for name in plan.get("endpoints", []):
        lines.append(f"    {name}")
    lines.append(
        f"- Decision rule: {'union test (any may pass)' if is_union else 'intersection-union test (all must pass)'}."
    )
    lines.append(
        f"- ALPHA: {plan['alpha_per_endpoint']:.4f} per endpoint, "
        f"{'ADJUSTED (Bonferroni)' if plan.get('alpha_adjusted') else 'NOT adjusted'}; "
        f"{plan['ci_pct']:.2f}% confidence interval per endpoint."
    )
    lines.append(f"    {plan.get('alpha_rationale', '')}")
    if plan.get("power_adjusted", True) and plan["n_co_primary"] > 1:
        probability_clause = (
            "the probability that at least one co-primary endpoint passes"
            if is_union else
            "the probability that every co-primary endpoint passes"
        )
        lines.append(
            f"- POWER: {plan['overall_power'] * 100:.0f}% OVERALL ({probability_clause}), "
            f"achieved by powering each endpoint at {plan['per_endpoint_power'] * 100:.2f}%."
        )
    else:
        # The conventional basis. `overall_power` here is an independence-based
        # LOWER BOUND, not the study's design power, so it must not be presented
        # as the headline figure — doing so would make a correctly-sized
        # bioequivalence study read as under-powered.
        lines.append(
            f"- POWER: {plan['per_endpoint_power'] * 100:.0f}% per endpoint — the conventional "
            f"bioequivalence basis, with the study sized on the more variable co-primary "
            f"endpoint. Report the power as {plan['per_endpoint_power'] * 100:.0f}%; do NOT "
            f"describe the study as powered at any lower figure."
        )
    lines.append(f"    {plan.get('power_rationale', '')}")
    lines.append(
        "- State the alpha and the power exactly as above. In particular, when the alpha is "
        "NOT adjusted, say so and give the intersection-union reason - do not leave it "
        "looking like multiplicity was overlooked."
    )
    for note in plan.get("assumptions", []):
        lines.append(f"- ASSUMPTION (state it explicitly): {note}")
    return "\n".join(lines) + "\n"
