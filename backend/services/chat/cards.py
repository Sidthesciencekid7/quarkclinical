"""Card builders and the deterministic card-action engine (plan B3 "Card shape",
"Action types", B8, B9).

Two halves, both PURE (no database, no FastAPI, no SDK):

1. BUILDERS -- one function per ``CardType`` that turns the draft spec and engine
   output into a :class:`~services.chat.contract.Card` whose ``data`` is the typed
   model in ``contract.py`` (``CARD_DATA_MODELS``). Cards carry server JSON only;
   the browser renders it, never HTML. Engine values reach the user in these
   cards, not in the assistant's prose.

2. ``apply_action`` -- what a card CLICK does, with no AI cost. It validates the
   action against the session kind, applies it to a COPY of the spec with the
   same validators the assistant's tools use, refreshes the cards whose content
   depends on the spec, creates the next card in the flow, and returns the
   outcome with a one-line guide and suggestion chips. Actions that need the
   router / database writes (``generate_protocol``, ``create_budget``,
   ``add_drug``, ``confirm_edit``, ``apply_edit``, ``discard_edit``) return an
   ``ActionIntent`` for the router to execute; the ``after_*`` helpers then build
   the follow-up cards deterministically from the router's result.

Superseding: when a card of a "re-issuable" type is created, older ACTIVE cards of
the same type become ``superseded`` (UI: 60 % opacity, buttons disabled). A card
the user clicked becomes ``done`` unless the click is a toggle / selection inside
a card that stays open.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from services.chat import contract as c
from services.chat.contract import (
    ACTION_SESSION_KINDS,
    Card,
    CardAction,
    CardStatus,
    CardType,
    ChatErrorCode,
    FieldStatus,
    Guide,
    SummaryRow,
)
from services.chat.facades import ChatFacades, FacadeError
from services.chat.spec import (
    GENERIC_EXCLUSION_OPTIONS,
    RANGES,
    STUDY_TYPE_LABELS,
    STUDY_TYPES,
    Actor,
    BudgetDraftSpec,
    DraftSpec,
    EditSessionSpec,
    PendingEdit,
    PopulationDraft,
    ProtocolDraftSpec,
    study_type_gate_message,
)
from services.design_decisions import (
    DOSE_MULTIPLE_SS,
    DOSE_SINGLE,
    POP_PATIENTS_MULTI,
    allowed_study_types,
    classify_drug_modality,
)
from services.guidelines import DEFAULT_GUIDELINE, GUIDELINES, guideline_display_name

GateFn = Callable[[str, str], "str | None"]

#: Card types for which a newly created card supersedes older ACTIVE ones.
SUPERSEDING_TYPES: frozenset[CardType] = frozenset({
    CardType.drug_matches, CardType.drug_add_offer, CardType.study_type, CardType.population,
    CardType.cv_override_warning, CardType.interaction_screening, CardType.design_decisions,
    CardType.indication, CardType.guideline, CardType.protocol_summary, CardType.budget_preview,
    CardType.budget_summary, CardType.protocol_overview, CardType.edit_impact, CardType.edit_diff,
})

_MAX_TITLE = 120


# ---------------------------------------------------------------------------
# Errors and intents
# ---------------------------------------------------------------------------


class ActionError(Exception):
    """An action that cannot be applied. ``code`` maps to an HTTP status through
    ``contract.ERROR_HTTP_STATUS`` (422 validation / invalid_action, 404, ...)."""

    def __init__(self, code: ChatErrorCode, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ActionIntent:
    """Base of the objects ``apply_action`` returns for actions the router executes."""

    name: str = field(default="", init=False)


@dataclass(frozen=True)
class AddDrugIntent(ActionIntent):
    """Add ``inn`` to the global drug catalogue (after the confirm-card click),
    then call ``apply_action(select_drug)`` with the new drug's id."""

    inn: str = ""
    name: str = field(default="add_drug", init=False)


@dataclass(frozen=True)
class GenerateProtocolIntent(ActionIntent):
    """Call ``start_protocol_generation(db, request, actor, ProtocolCreate.model_validate(payload),
    provenance=provenance)``, then ``after_generation_started``."""

    payload: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)
    name: str = field(default="generate_protocol", init=False)


@dataclass(frozen=True)
class CreateBudgetIntent(ActionIntent):
    """Call ``create_budget_for_actor(db, request, actor, BudgetCreate.model_validate(payload))``,
    then ``after_budget_created``."""

    payload: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)
    name: str = field(default="create_budget", init=False)


@dataclass(frozen=True)
class ConfirmEditIntent(ActionIntent):
    """Create the ``protocol_edit_proposals`` row with THIS ``proposal_id`` (it was
    pre-minted when the impact card was built), status ``proposed`` ->
    ``generating``, and start the background job; then ``after_edit_confirmed``."""

    proposal_id: str = ""
    kind: str = "engine"
    request: dict[str, Any] = field(default_factory=dict)
    base_version: int = 0
    name: str = field(default="confirm_edit", init=False)


@dataclass(frozen=True)
class ApplyEditIntent(ActionIntent):
    proposal_id: str = ""
    name: str = field(default="apply_edit", init=False)


@dataclass(frozen=True)
class DiscardEditIntent(ActionIntent):
    """Mark the proposal discarded. ``pending_only`` is True when the proposal row
    does not exist yet (the user cancelled on the impact card): nothing to write."""

    proposal_id: str = ""
    pending_only: bool = False
    name: str = field(default="discard_edit", init=False)


@dataclass
class ActionOutcome:
    """Result of ``apply_action``. Iterating yields ``(spec, cards, guide)``.

    ``cards`` are the cards created OR changed (the ActionResponse contract).
    ``intent`` is set only for actions the router must execute."""

    spec: DraftSpec
    cards: list[Card]
    guide: Guide
    intent: ActionIntent | None = None

    def __iter__(self):
        yield self.spec
        yield self.cards
        yield self.guide


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------


def make_card(
    card_type: CardType,
    spec_version: int,
    title: str,
    data: Any,
    actions: Sequence[CardAction] = (),
    *,
    card_id: str | None = None,
    status: CardStatus = CardStatus.active,
) -> Card:
    """Build a Card, validating ``data`` against the typed model for ``card_type``."""
    model = c.CARD_DATA_MODELS[card_type]
    if isinstance(data, dict):
        data = model.model_validate(data)
    elif not isinstance(data, model):
        raise TypeError(f"{card_type.value} card data must be {model.__name__}, got {type(data).__name__}")
    return Card(
        id=card_id or str(uuid.uuid4()),
        type=card_type,
        status=status,
        spec_version=spec_version,
        title=title[:_MAX_TITLE],
        data=data.model_dump(mode="json"),
        actions=list(actions),
    )


def _fmt(value: Any) -> str:
    if value is None or value == "":
        return "not set"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, (list, tuple)):
        return "; ".join(_fmt(v) for v in value) if value else "none"
    return str(value)


def _modality(inn: str | None) -> str:
    return classify_drug_modality(inn) if inn else "unknown"


def _guideline_label(version: str, study_type: str | None) -> str:
    return guideline_display_name(version, study_type or "BE") if version in GUIDELINES else version


def _summary_rows(spec: DraftSpec, *, exclude: Sequence[str] = ()) -> list[SummaryRow]:
    rows: list[SummaryRow] = []
    for f in spec.summary_fields():
        if f.path in exclude:
            continue
        rows.append(SummaryRow(
            path=f.path, label=f.label, value=f.display_value, status=f.status, source=f.source,
            needs_check=f.status == FieldStatus.proposed,
        ))
    return rows


def _missing_labels(spec: DraftSpec) -> list[str]:
    by_path = {f.path: f.label for f in spec.summary_fields()}
    needs_confirm = {
        "drug": "Drug (needs your confirmation)", "study_type": "Study type (needs your confirmation)",
        "design_decisions": "Study design (review and confirm)",
    }
    out = []
    for p in spec.missing():
        if isinstance(spec, ProtocolDraftSpec) and p in needs_confirm:
            out.append(needs_confirm[p])
        else:
            out.append(by_path.get(p, p))
    return out


# ---------------------------------------------------------------------------
# Protocol-session cards
# ---------------------------------------------------------------------------


def build_drug_matches_card(
    spec: DraftSpec, query: str, rows: Sequence[dict[str, Any]], *, truncated: bool = False,
) -> Card:
    items = [
        c.DrugMatchItem(
            id=str(r["id"]), inn=str(r["inn"]), brand_names=list(r.get("brand_names") or []),
            modality=r.get("modality") or _modality(r.get("inn")),
            allowed_study_types=list(r.get("allowed_study_types") or []),
        )
        for r in rows
    ]
    selected = spec.drug.id if isinstance(spec, ProtocolDraftSpec) and spec.drug else None
    data = c.DrugMatchesCardData(query=query, matches=items, truncated=truncated, selected_drug_id=selected)
    return make_card(CardType.drug_matches, spec.version, "Matching drugs", data)


def build_drug_add_offer_card(spec: DraftSpec, query: str) -> Card:
    inn = " ".join((query or "").strip().lower().split())
    valid = bool(re.match(r"^[a-z][a-z \-]{1,63}$", inn))
    allowed = allowed_study_types(inn) if valid else None
    data = c.DrugAddOfferCardData(
        query=query.strip()[:80],
        inn=inn if valid else None,
        valid=valid,
        message=(
            f'"{inn}" is not in the drug catalogue yet. Adding it makes it selectable for everyone '
            "using QuarkClinical. Only add real drug names (INN)."
            if valid else
            "That does not look like a drug name (2 to 64 letters, spaces or hyphens only). "
            "Try the generic (INN) name."
        ),
        modality=_modality(inn) if valid else "unknown",
        allowed_study_types=list(allowed) if allowed else [],
    )
    actions = [CardAction(id="add_drug", label=f'Add "{inn}" to the catalogue', variant="primary")] if valid else []
    return make_card(CardType.drug_add_offer, spec.version, "Drug not found", data, actions)


def build_study_type_card(
    spec: ProtocolDraftSpec, *, gate_message: str | None = None, gate: GateFn | None = None,
) -> Card:
    gate = gate or study_type_gate_message
    inn = spec.drug.inn if spec.drug else None
    options = []
    for st in STUDY_TYPES:
        reason = gate(inn, st) if inn else None
        options.append(c.StudyTypeOption(
            value=st, label=STUDY_TYPE_LABELS[st], allowed=reason is None, reason=reason,
        ))
    data = c.StudyTypeCardData(
        drug_inn=inn, modality=_modality(inn), options=options,
        selected=spec.study_type if spec.study_type in STUDY_TYPES else None,  # type: ignore[arg-type]
        gate_message=gate_message,
    )
    return make_card(CardType.study_type, spec.version, "Study type", data)


def _population_card_data(spec: ProtocolDraftSpec) -> c.PopulationCardData:
    status = {p: m.status.value for p, m in spec.field_meta.items() if p.startswith("population.")}
    return c.PopulationCardData(
        drug_inn=spec.drug.inn if spec.drug else None,
        study_type=spec.study_type,  # type: ignore[arg-type]
        values=c.PopulationValues.model_validate(spec.population_values()),
        ranges=c.PopulationRanges(**RANGES.model_dump()),
        generic_exclusion_options=list(GENERIC_EXCLUSION_OPTIONS),
        field_status=status,
        confirm_patch={"subject_type": spec.population.subject_type},
    )


def build_population_card(spec: ProtocolDraftSpec) -> Card:
    return make_card(CardType.population, spec.version, "Participants", _population_card_data(spec))


CV_OVERRIDE_CONSEQUENCES = (
    "Your value replaces the literature-derived within-subject CV used to power the study.",
    "The protocol will declare it as investigator-specified, not literature-derived.",
    "The statistical analysis plan will not cite literature trials as the source of the number "
    "(the supporting-trials table is left empty); you must state the justification.",
    "The literature values are still kept on record, labelled as not used for powering.",
)


def build_cv_override_warning_card(spec: ProtocolDraftSpec) -> Card:
    cv = spec.population.cv_intra
    if cv is None:
        raise ValueError("No CV override is set.")
    data = c.CvOverrideWarningCardData(
        cv_intra=cv,
        message=(
            f"You set the within-subject CV to {cv:g}%. Leaving it blank is the safer path: "
            "the engine then takes the highest CV reported in the literature for healthy-volunteer studies."
        ),
        consequences=list(CV_OVERRIDE_CONSEQUENCES),
        clear_patch={"cv_intra": None},
    )
    actions = [CardAction(id="update_population", label="Use the literature CV instead", variant="secondary")]
    return make_card(CardType.cv_override_warning, spec.version, "Within-subject CV override", data, actions)


def _format_days(days: float) -> str:
    rounded = int(days) if float(days).is_integer() else round(days, 1)
    return f"{rounded} day{'' if rounded == 1 else 's'}"


def criterion_text(row: dict[str, Any]) -> str:
    """The exclusion-criterion sentence for a DDI row: the engine's own wording
    when it supplies one, else the same construction the wizard used."""
    explicit = str(row.get("criterion") or "").strip()
    if explicit:
        return explicit
    examples = [str(e) for e in (row.get("examples") or []) if e][:3]
    label = str(row.get("label") or row.get("key") or "")
    head = f"{label} (e.g. {', '.join(examples)})" if examples else label
    if row.get("history_exclusion"):
        return f"{head}: excluded on history (cannot be washed out)"
    wd = row.get("washout_days")
    if isinstance(wd, (int, float)) and not isinstance(wd, bool) and wd > 0:
        return f"{head} within {_format_days(float(wd))} before first dose"
    if row.get("conditions"):
        return f"{head}: {row['conditions']}"
    return head


_DDI_KEEP = (
    "drug_inn", "design_key", "is_nti", "pathway_keys", "prohibited", "restricted", "permitted",
    "max_washout_days", "screening_history_window_days", "summary",
)


def trim_ddi(ddi: dict[str, Any]) -> dict[str, Any]:
    """The part of a DDI screening result worth caching in the spec (bounded size)."""
    out = {k: ddi.get(k) for k in _DDI_KEEP if k in ddi}
    out["unresolved_count"] = len(ddi.get("unresolved") or [])
    for status in ("prohibited", "restricted", "permitted"):
        rows = []
        for r in (out.get(status) or [])[:40]:
            if not isinstance(r, dict):
                continue
            r = dict(r)
            ev = r.get("evidence")
            if isinstance(ev, dict) and isinstance(ev.get("quote"), str):
                r["evidence"] = {"source": ev.get("source"), "quote": ev["quote"][:300]}
            rows.append(r)
        out[status] = rows
    return out


def _ddi_row_data(row: dict[str, Any], selected: set[str], status: str) -> c.DdiRowData:
    ev = row.get("evidence") if isinstance(row.get("evidence"), dict) else {}
    text = criterion_text(row)
    return c.DdiRowData(
        key=str(row.get("key") or row.get("label") or ""),
        label=str(row.get("label") or row.get("key") or ""),
        status=str(row.get("status") or status),
        pathway=row.get("pathway"),
        examples=[str(e) for e in (row.get("examples") or [])],
        washout_days=row.get("washout_days") if isinstance(row.get("washout_days"), (int, float)) else None,
        washout_basis=row.get("washout_basis"),
        history_exclusion=bool(row.get("history_exclusion")),
        rationale=row.get("rationale"),
        conditions=row.get("conditions"),
        escalated_by_nti=bool(row.get("escalated_by_nti")),
        assumption=row.get("assumption"),
        evidence_quote=(ev.get("quote") or None) if ev else None,
        criterion_text=text,
        selected=text in selected,
    )


def _interaction_card_data(spec: ProtocolDraftSpec, ddi: dict[str, Any]) -> c.InteractionScreeningCardData:
    selected = set(spec.exclusions())
    return c.InteractionScreeningCardData(
        drug_inn=ddi.get("drug_inn") or (spec.drug.inn if spec.drug else None),
        design_key=ddi.get("design_key"),
        is_nti=ddi.get("is_nti"),
        pathway_keys=[str(k) for k in (ddi.get("pathway_keys") or [])],
        prohibited=[_ddi_row_data(r, selected, "prohibited") for r in ddi.get("prohibited") or []],
        restricted=[_ddi_row_data(r, selected, "restricted") for r in ddi.get("restricted") or []],
        permitted=[_ddi_row_data(r, selected, "permitted") for r in ddi.get("permitted") or []],
        max_washout_days=ddi.get("max_washout_days"),
        screening_history_window_days=ddi.get("screening_history_window_days"),
        unresolved_count=int(ddi.get("unresolved_count") or len(ddi.get("unresolved") or [])),
        summary=ddi.get("summary"),
    )


def build_interaction_screening_card(spec: ProtocolDraftSpec, ddi: dict[str, Any]) -> Card:
    return make_card(
        CardType.interaction_screening, spec.version, "Drug interaction screening",
        _interaction_card_data(spec, ddi),
    )


def _design_card_parts(spec: ProtocolDraftSpec) -> tuple[c.DesignDecisionsCardData, list[CardAction]]:
    data = c.DesignDecisionsCardData(
        drug_inn=spec.drug.inn if spec.drug else None,
        study_type=spec.study_type,  # type: ignore[arg-type]
        decisions=[
            c.DesignDecisionData(
                key=str(d.get("key")), label=str(d.get("label") or d.get("key")), value=str(d.get("value") or ""),
                rationale=str(d.get("rationale") or ""), evidence=str(d.get("evidence") or ""),
                options=[str(o) for o in (d.get("options") or [])],
            )
            for d in spec.design_decisions
        ],
        confirmed=spec.design_confirmed,
    )
    actions = [] if spec.design_confirmed else [CardAction(id="confirm_design", label="Confirm design", variant="primary")]
    return data, actions


def build_design_decisions_card(spec: ProtocolDraftSpec) -> Card:
    data, actions = _design_card_parts(spec)
    return make_card(CardType.design_decisions, spec.version, "Study design", data, actions)


def _candidate(d: dict[str, Any] | None) -> c.IndicationCandidateData | None:
    if not isinstance(d, dict) or not d.get("indication"):
        return None
    cv = d.get("observed_cv_pct")
    return c.IndicationCandidateData(
        indication=str(d["indication"]), on_label=bool(d.get("on_label")),
        label_evidence=(str(d["label_evidence"])[:300] if d.get("label_evidence") else None),
        therapeutic_area=d.get("therapeutic_area"), setting_note=d.get("setting_note"),
        monotherapy=d.get("monotherapy"), pk_variability=d.get("pk_variability"),
        observed_cv_pct=float(cv) if isinstance(cv, (int, float)) and not isinstance(cv, bool) else None,
        recruitment=d.get("recruitment"), rank_reason=d.get("rank_reason"),
    )


def _indication_card_data(spec: ProtocolDraftSpec, result: dict[str, Any]) -> c.IndicationCardData:
    ranked = [x for x in (_candidate(r) for r in (result.get("ranked") or [])[:6]) if x]
    return c.IndicationCardData(
        applicable=bool(result.get("applicable")),
        reason=result.get("reason"),
        drug_inn=result.get("drug_inn") or (spec.drug.inn if spec.drug else None),
        drug_class=result.get("drug_class"),
        label_verified=result.get("label_verified"),
        recommended=_candidate(result.get("recommended")),
        ranked=ranked,
        user_indication=result.get("user_indication"),
        user_indication_warnings=[str(w) for w in (result.get("user_indication_warnings") or [])],
        selected_indication=spec.user_indication,
        summary=result.get("summary"),
    )


def build_indication_card(spec: ProtocolDraftSpec, result: dict[str, Any]) -> Card:
    actions = []
    if spec.user_indication:
        actions.append(CardAction(id="set_indication", label="Let the engine choose", variant="secondary"))
    return make_card(CardType.indication, spec.version, "Study indication", _indication_card_data(spec, result), actions)


def _guideline_card_data(spec: ProtocolDraftSpec) -> c.GuidelineCardData:
    ordered = sorted(
        (g for g in GUIDELINES.values() if g.is_current),
        key=lambda g: (g.id != DEFAULT_GUIDELINE, g.display_name),
    )
    st = spec.study_type or "BE"
    options = [
        c.GuidelineOptionData(
            id=g.id, display_name=guideline_display_name(g.id, st), regulator=g.regulator, region=g.region,
            citation=g.citation, is_current=g.is_current,
            submission_pathway=(g.biosimilar_pathway if st == "biosimilar_pk" else g.submission_pathway) or "",
        )
        for g in ordered
    ]
    return c.GuidelineCardData(selected=spec.guideline_version, default=DEFAULT_GUIDELINE, options=options)


def build_guideline_card(spec: ProtocolDraftSpec) -> Card:
    return make_card(CardType.guideline, spec.version, "Regulatory guideline", _guideline_card_data(spec))


def build_protocol_summary_card(spec: ProtocolDraftSpec, *, blocking_issue: str | None = None) -> Card:
    ready = spec.ready()
    notes = ["Generating a protocol uses AI credits and takes several minutes."]
    proposed = [r.label for r in _summary_rows(spec) if r.needs_check]
    if proposed:
        notes.append("Please check: " + ", ".join(proposed) + ". Clicking Generate confirms everything shown.")
    data = c.ProtocolSummaryCardData(
        rows=_summary_rows(spec), ready=ready, missing=_missing_labels(spec),
        blocking_issue=blocking_issue, notes=notes,
    )
    actions = [CardAction(id="generate_protocol", label="Generate protocol", variant="primary")] if ready and not blocking_issue else []
    return make_card(CardType.protocol_summary, spec.version, "Review and generate", data, actions)


def build_generation_progress_card(
    spec_version: int, protocol_id: str, drug_inn: str | None, status: str = "generating",
    error: str | None = None, *, card_id: str | None = None,
) -> Card:
    data = c.GenerationProgressCardData(protocol_id=protocol_id, drug_inn=drug_inn, status=status, error=error)
    card_status = CardStatus.error if status == "failed" else (CardStatus.done if status == "complete" else CardStatus.active)
    return make_card(CardType.generation_progress, spec_version, "Generating your protocol", data, card_id=card_id, status=card_status)


def build_protocol_ready_card(spec_version: int, protocol: dict[str, Any]) -> Card:
    sections = protocol.get("sections") or {}
    data = c.ProtocolReadyCardData(
        protocol_id=str(protocol["id"]), drug_inn=protocol.get("drug_inn"), study_type=protocol.get("study_type"),
        guideline_version=protocol.get("guideline_version"),
        section_count=int(protocol.get("section_count") or sum(1 for v in sections.values() if v)),
        version=int(protocol.get("version") or 1),
    )
    return make_card(CardType.protocol_ready, spec_version, "Protocol ready", data, status=CardStatus.done)


# ---------------------------------------------------------------------------
# Budget-session cards
# ---------------------------------------------------------------------------


def build_budget_preview_card(spec: DraftSpec, preview: dict[str, Any]) -> Card:
    d = preview.get("defaults") or {}
    data = c.BudgetPreviewCardData(
        drug_inn=str(preview.get("drug_inn") or ""),
        modality=preview.get("modality") or "unknown",
        allowed_study_types=[str(x) for x in (preview.get("allowed_study_types") or [])],
        study_type=str(preview.get("study_type") or ""),
        subject_profile=str(preview.get("subject_profile") or ""),
        subject_profile_source=str(preview.get("subject_profile_source") or ""),
        subject_profile_basis=str(preview.get("subject_profile_basis") or ""),
        therapeutic_area=str(preview.get("therapeutic_area") or "unknown"),
        profile_key=preview.get("profile_key"),
        defaults=c.BudgetDefaultsData(**{k: d.get(k) for k in c.BudgetDefaultsData.model_fields}),
        estimated_grand_total=float(preview.get("estimated_grand_total") or 0.0),
        currency=getattr(spec, "currency", "USD") if isinstance(spec, BudgetDraftSpec) else "USD",
        category_count=int(preview.get("category_count") or 0),
        disclaimer=(
            "An approximate planning estimate from a rate card calibrated against real CRO budgets; "
            "not a quotation. Every number is editable once the budget is created."
        ),
    )
    return make_card(CardType.budget_preview, spec.version, "Budget estimate", data)


def _protocol_option(p: dict[str, Any]) -> c.ProtocolOption:
    return c.ProtocolOption(
        id=str(p["id"]), drug_inn=str(p.get("drug_inn") or ""), study_type=str(p.get("study_type") or ""),
        status=str(p.get("status") or ""), version=int(p.get("version") or 1), created_at=p.get("created_at"),
    )


def build_budget_summary_card(
    spec: BudgetDraftSpec, *, options: Sequence[dict[str, Any]] = (), blocking_issue: str | None = None,
) -> Card:
    preview = spec.derived_output("budget_preview")
    ready = spec.ready()
    notes = ["The total is an approximate planning estimate, editable after creation."]
    if any(r.needs_check for r in _summary_rows(spec)):
        notes.append("Some values were inferred from your message. Clicking Create confirms everything shown.")
    data = c.BudgetSummaryCardData(
        rows=_summary_rows(spec), ready=ready, missing=_missing_labels(spec), blocking_issue=blocking_issue,
        linked_protocol=_protocol_option(spec.protocol) if spec.protocol else None,
        protocol_options=[_protocol_option(p) for p in options],
        estimated_grand_total=float(preview["estimated_grand_total"]) if isinstance(preview, dict) and preview.get("estimated_grand_total") is not None else None,
        currency=spec.currency, notes=notes,
    )
    actions = [CardAction(id="create_budget", label="Create budget", variant="primary")] if ready and not blocking_issue else []
    return make_card(CardType.budget_summary, spec.version, "Review and create", data, actions)


def build_budget_ready_card(spec_version: int, budget: dict[str, Any]) -> Card:
    rollup = budget.get("rollup") or {}
    cats = budget.get("categories") or []
    data = c.BudgetReadyCardData(
        budget_id=str(budget["id"]), name=str(budget.get("name") or "Budget"), drug_inn=budget.get("drug_inn"),
        study_type=budget.get("study_type"), currency=str(budget.get("currency") or "USD"),
        grand_total=float(rollup["grand_total"]) if rollup.get("grand_total") is not None else None,
        category_count=len(cats) if isinstance(cats, list) else 0,
        n_enrolled=budget.get("n_enrolled"),
        protocol_id=str(budget["protocol_id"]) if budget.get("protocol_id") else None,
    )
    return make_card(CardType.budget_ready, spec_version, "Budget created", data, status=CardStatus.done)


# ---------------------------------------------------------------------------
# Edit-session cards
# ---------------------------------------------------------------------------

EDIT_FIELD_LABELS: dict[str, str] = {
    "cv_intra": "Within-subject CV (%)", "is_nti": "Narrow therapeutic index",
    "dose_strength": "Dose strength", "reference_product": "Reference product",
    "additional_pk_timepoints_hours": "Extra PK time points (h)",
    "additional_immunogenicity_timepoints_months": "Extra ADA time points (months)",
    "exclusion_add": "Add exclusion criterion", "exclusion_remove": "Remove exclusion criterion",
    "user_indication": "Study indication", "guideline_version": "Guideline",
    "subject_type": "Participants", "age_min": "Minimum age", "age_max": "Maximum age", "sex": "Sex",
    "weight_min": "Minimum weight (kg)", "weight_max": "Maximum weight (kg)",
    "design_decision_population": "Design: population", "design_decision_dose": "Design: dose",
    "design_decision_route": "Design: route, device, site", "design_decision_design": "Design: parallel / crossover",
    "design_decision_pd_endpoint": "Design: PD endpoint", "washout_days": "Washout (days)",
}

RERUN_NOTE = (
    "Changing an engine input re-runs the full generation for this protocol (a few minutes, and it uses "
    "generation credits). Nothing changes in the protocol until you review the result and apply it."
)
PROSE_NOTE = (
    "Only the sections you named are rewritten (about one to two sections costs a small amount of credit). "
    "Engine-computed numbers stay locked. Nothing changes until you review the diff and apply it."
)


def locked_fact_rows(snapshot: dict[str, Any]) -> list[c.FactRow]:
    """The engine-locked facts of a stored protocol, as display rows. Reads only a
    fixed whitelist (``snapshot["facts"]``, ``decisions``, ``guideline_version``)."""
    facts = snapshot.get("facts") or {}
    rows: list[c.FactRow] = []

    def add(label: str, value: Any) -> None:
        if value is not None and value != "":
            rows.append(c.FactRow(label=label, value=_fmt(value)))

    if facts.get("n_enrolled") is not None:
        extra = f" ({facts['n_evaluable']} evaluable)" if facts.get("n_evaluable") is not None else ""
        add("Planned enrolment", f"{facts['n_enrolled']}{extra}")
    add("Design", facts.get("design"))
    add("Statistical approach", facts.get("approach"))
    if facts.get("cv_pct") is not None:
        add("Within-subject CV", f"{facts['cv_pct']:g}% — {facts.get('cv_source') or 'source not recorded'}")
    if facts.get("t_half_hours") is not None:
        add("Terminal half-life", f"{facts['t_half_hours']:g} h")
    if facts.get("washout_days") is not None:
        add("Washout between periods", f"{facts['washout_days']:g} days")
    elif facts.get("washout_applicable") is False:
        add("Washout between periods", "None (parallel design)")
    for d in snapshot.get("decisions") or []:
        if isinstance(d, dict):
            add(str(d.get("label") or d.get("key")), d.get("value"))
    ddi_counts = [(s, facts.get(f"ddi_{s}_rows")) for s in ("prohibited", "restricted", "permitted")]
    if any(n is not None for _, n in ddi_counts):
        add("Interaction rows", ", ".join(f"{n} {s}" for s, n in ddi_counts if n is not None))
    if snapshot.get("guideline_version"):
        add("Guideline", _guideline_label(str(snapshot["guideline_version"]), snapshot.get("study_type")))
    return rows


def build_protocol_overview_card(spec: EditSessionSpec, snapshot: dict[str, Any]) -> Card:
    from services.chat import safety

    labels = snapshot.get("section_labels") or {}
    stats = [
        c.SectionStat(name=name, label=str(labels.get(name, name)), words=len(safety.strip_html(text).split()))
        for name, text in (snapshot.get("sections") or {}).items() if isinstance(text, str)
    ]
    data = c.ProtocolOverviewCardData(
        protocol_id=str(snapshot["id"]), drug_inn=snapshot.get("drug_inn"), status=str(snapshot.get("status")),
        version=int(snapshot.get("version") or 1), study_type=str(snapshot.get("study_type")),
        guideline_version=str(snapshot.get("guideline_version")),
        generation_status=str(snapshot.get("generation_status") or "complete"),
        editable=spec.editable, blocked_reason=spec.blocked_reason, sections=stats,
        locked_facts=locked_fact_rows(snapshot),
    )
    return make_card(CardType.protocol_overview, spec.version, "Protocol overview", data)


def build_edit_impact_card(
    spec: EditSessionSpec, pending: PendingEdit, *, estimate: c.EditEstimate | None = None,
    warnings: Sequence[str] = (),
) -> Card:
    changes: list[c.EditChange] = []
    if pending.kind == "engine":
        for fld, new in pending.changes.items():
            before = None if fld == "exclusion_add" else _fmt(spec.before_value(fld))
            changes.append(c.EditChange(
                field=fld, label=EDIT_FIELD_LABELS.get(fld, fld), before=before, after=_fmt(new),
            ))
    data = c.EditImpactCardData(
        proposal_id=pending.proposal_id, kind=pending.kind, base_version=pending.base_version,
        editable=spec.editable, blocked_reason=spec.blocked_reason, changes=changes,
        sections=list(pending.sections), instruction=pending.instruction, estimate=estimate,
        warnings=list(warnings), rerun_note=RERUN_NOTE if pending.kind == "engine" else PROSE_NOTE,
    )
    actions = []
    if spec.editable:
        actions = [
            CardAction(id="confirm_edit", label="Run this change" if pending.kind == "engine" else "Rewrite these sections", variant="primary"),
            CardAction(id="discard_edit", label="Cancel", variant="ghost"),
        ]
    return make_card(CardType.edit_impact, spec.version, "Review the change", data, actions)


def build_edit_progress_card(
    spec_version: int, proposal_id: str, kind: str, status: str = "generating", error: str | None = None,
) -> Card:
    data = c.EditProgressCardData(proposal_id=proposal_id, kind=kind, status=status, error=error)  # type: ignore[arg-type]
    card_status = CardStatus.error if status in ("failed", "stale") else (CardStatus.done if status in ("ready", "applied", "discarded") else CardStatus.active)
    return make_card(CardType.edit_progress, spec_version, "Preparing the edit", data, status=card_status)


def build_edit_diff_card(spec_version: int, proposal: dict[str, Any]) -> Card:
    """``proposal`` is the router's view of a ready proposal: ``{id, kind, status,
    base_version, stale?, diff: [SectionDiff dicts], qc?, facts_before?, facts_after?,
    applied_version?}``; fact lists are ``FactRow`` dicts (use ``locked_fact_rows``)."""
    data = c.EditDiffCardData(
        proposal_id=str(proposal["id"]), kind=proposal["kind"], status=proposal.get("status", "ready"),
        base_version=int(proposal.get("base_version") or 0), stale=bool(proposal.get("stale")),
        sections=[c.SectionDiffItem.model_validate(s) for s in proposal.get("diff") or []],
        qc=proposal.get("qc"),
        facts_before=[c.FactRow.model_validate(x) for x in proposal.get("facts_before") or []],
        facts_after=[c.FactRow.model_validate(x) for x in proposal.get("facts_after") or []],
        applied_version=proposal.get("applied_version"),
    )
    actions: list[CardAction] = []
    if data.status == c.ProposalStatus.ready and not data.stale:
        actions = [
            CardAction(id="apply_edit", label="Apply to protocol", variant="primary"),
            CardAction(id="discard_edit", label="Discard", variant="ghost"),
        ]
    elif data.status == c.ProposalStatus.ready:
        actions = [CardAction(id="discard_edit", label="Discard", variant="ghost")]
    return make_card(CardType.edit_diff, spec_version, "Proposed changes", data, actions)


def build_notice_card(spec_version: int, level: str, text: str, code: str | None = None) -> Card:
    return make_card(CardType.notice, spec_version, "Notice", c.NoticeCardData(level=level, text=text, code=code))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Card lifecycle: superseding, done, in-place refresh
# ---------------------------------------------------------------------------


def with_status(card: Card, status: CardStatus, *, data_patch: dict[str, Any] | None = None) -> Card:
    data = dict(card.data)
    if data_patch:
        data.update(data_patch)
    return card.model_copy(update={"status": status, "data": data})


def superseded_by(new_card: Card, existing: Sequence[Card]) -> list[Card]:
    """Older ACTIVE cards of ``new_card``'s type, marked superseded (empty for
    types that are not re-issuable)."""
    if new_card.type not in SUPERSEDING_TYPES:
        return []
    return [
        with_status(old, CardStatus.superseded)
        for old in existing if old.type == new_card.type and old.status == CardStatus.active and old.id != new_card.id
    ]


def _rebuild_population(card: Card, spec: DraftSpec) -> Card | None:
    if not isinstance(spec, ProtocolDraftSpec):
        return None
    return card.model_copy(update={"data": _population_card_data(spec).model_dump(mode="json"), "spec_version": spec.version})


def _rebuild_guideline(card: Card, spec: DraftSpec) -> Card | None:
    if not isinstance(spec, ProtocolDraftSpec):
        return None
    return card.model_copy(update={"data": _guideline_card_data(spec).model_dump(mode="json"), "spec_version": spec.version})


def _rebuild_design(card: Card, spec: DraftSpec) -> Card | None:
    if not isinstance(spec, ProtocolDraftSpec) or not spec.design_decisions:
        return None
    data, actions = _design_card_parts(spec)
    return card.model_copy(update={
        "data": data.model_dump(mode="json"), "actions": actions, "spec_version": spec.version,
    })


def _rebuild_screening(card: Card, spec: DraftSpec) -> Card | None:
    if not isinstance(spec, ProtocolDraftSpec):
        return None
    ddi = spec.derived_output("ddi")
    if not isinstance(ddi, dict):
        return None
    return card.model_copy(update={"data": _interaction_card_data(spec, ddi).model_dump(mode="json"), "spec_version": spec.version})


def _rebuild_matches(card: Card, spec: DraftSpec) -> Card | None:
    if not isinstance(spec, ProtocolDraftSpec):
        return None
    data = dict(card.data)
    data["selected_drug_id"] = spec.drug.id if spec.drug else None
    return card.model_copy(update={"data": data, "spec_version": spec.version})


def _rebuild_study_type(card: Card, spec: DraftSpec) -> Card | None:
    if not isinstance(spec, ProtocolDraftSpec):
        return None
    return build_study_type_card(spec).model_copy(update={"id": card.id, "status": card.status})


def _rebuild_budget_summary(card: Card, spec: DraftSpec) -> Card | None:
    if not isinstance(spec, BudgetDraftSpec):
        return None
    options = [c.ProtocolOption.model_validate(o).model_dump(mode="json") for o in (card.data.get("protocol_options") or [])]
    fresh = build_budget_summary_card(spec, options=options)
    return fresh.model_copy(update={"id": card.id, "status": card.status})


def _rebuild_indication(card: Card, spec: DraftSpec) -> Card | None:
    if not isinstance(spec, ProtocolDraftSpec):
        return None
    data = dict(card.data)
    data["selected_indication"] = spec.user_indication
    actions = [CardAction(id="set_indication", label="Let the engine choose", variant="secondary")] if spec.user_indication else []
    return card.model_copy(update={"data": data, "actions": actions, "spec_version": spec.version})


_REBUILDERS: dict[CardType, Callable[[Card, DraftSpec], "Card | None"]] = {
    CardType.population: _rebuild_population,
    CardType.guideline: _rebuild_guideline,
    CardType.design_decisions: _rebuild_design,
    CardType.interaction_screening: _rebuild_screening,
    CardType.drug_matches: _rebuild_matches,
    CardType.study_type: _rebuild_study_type,
    CardType.budget_summary: _rebuild_budget_summary,
    CardType.indication: _rebuild_indication,
}


def refresh_cards(existing: Sequence[Card], spec: DraftSpec, skip_ids: set[str] = frozenset()) -> list[Card]:  # type: ignore[assignment]
    """Re-render every ACTIVE card whose content depends on the spec, returning
    only the ones whose data or buttons actually changed."""
    changed: list[Card] = []
    for card in existing:
        if card.status != CardStatus.active or card.id in skip_ids:
            continue
        rebuild = _REBUILDERS.get(card.type)
        if rebuild is None:
            continue
        fresh = rebuild(card, spec)
        if fresh is not None and (fresh.data != card.data or fresh.actions != card.actions):
            changed.append(fresh)
    return changed


# ---------------------------------------------------------------------------
# Guide line + suggestion chips (deterministic, no AI)
# ---------------------------------------------------------------------------


def guide_for(spec: DraftSpec, *, note: str | None = None) -> Guide:
    """A short next-step line plus chips for the spec's current next step. ``note``
    (e.g. "Study type cleared: ...") is prepended."""
    text, chips = _guide_parts(spec)
    if note:
        text = f"{note} {text}"
    return Guide(text=text, chips=chips)


def _guide_parts(spec: DraftSpec) -> tuple[str, list[str]]:
    step = spec.next_step()
    if isinstance(spec, ProtocolDraftSpec):
        if step == "drug":
            return ("Which drug is this study for? Type its name and I'll search the catalogue.",
                    ["Biosimilar PK study of adalimumab", "Bioequivalence study of metformin", "Food-effect study of ibuprofen"])
        if step == "study_type":
            allowed = spec.allowed_study_types() or STUDY_TYPES
            names = {"BE": "Bioequivalence study", "biosimilar_pk": "Biosimilar PK study", "food_effect": "Food-effect study"}
            return ("Which type of study is it?", [names[s] for s in allowed])
        if step == "population":
            return ("Check who takes part: age, weight, sex and exclusions. The defaults are fine to keep.",
                    ["Healthy volunteers, age 18 to 45", "Patients", "Keep the defaults"])
        if step == "design":
            return ("Review the five design decisions and confirm them.", ["Why these design choices?"])
        if step == "indication":
            return ("Choose the study indication, or let the engine pick.", ["Let the engine choose"])
        if step == "guideline":
            return ("Pick the regulatory guideline (FDA is the default).", ["Use EMA", "Use ICH M13A", "Keep FDA"])
        if spec.ready():
            return ("Everything is set. Review the summary and generate when you are ready.", ["Show the summary"])
        return ("Some required items are still open.", ["Show the summary"])
    if isinstance(spec, BudgetDraftSpec):
        if step == "drug":
            return ("Which drug should I price? A drug name alone is enough.", ["Price a patient study for nivolumab", "Price a bioequivalence study for metformin"])
        if step == "preview":
            return ("I can show the engine's estimate for this drug.", ["Show the estimate"])
        return ("Adjust anything (sites, countries, currency, a linked protocol), then create the budget.",
                ["Use 40 sites", "Link a protocol", "Show the summary"])
    if isinstance(spec, EditSessionSpec):
        if step == "overview":
            return ("Let me read the protocol first.", ["Show an overview"])
        if step == "blocked":
            return (spec.blocked_reason or "This protocol cannot be edited here.", [])
        if step == "request":
            return ("Tell me what to change: rewrite a section, or change an input such as the CV.",
                    ["Shorten the adverse events section", "Change the CV", "Show the QC summary"])
        return ("Review the change card, then run it or cancel.", [])
    return ("", [])


# ---------------------------------------------------------------------------
# Deterministic next-step cards
# ---------------------------------------------------------------------------


def _facade_error(exc: FacadeError) -> ActionError:
    code = {
        404: ChatErrorCode.not_found, 403: ChatErrorCode.forbidden, 402: ChatErrorCode.license_required,
        422: ChatErrorCode.validation, 409: ChatErrorCode.invalid_action,
    }.get(exc.status_code, ChatErrorCode.internal)
    return ActionError(code, exc.message)


def _has_active(existing: Sequence[Card], created: Sequence[Card], card_type: CardType) -> bool:
    return any(x.type == card_type and x.status == CardStatus.active for x in [*existing, *created])


async def ensure_design(spec: ProtocolDraftSpec, facades: ChatFacades) -> bool:
    """Load the design decisions if they are missing / stale. True if fetched."""
    if spec.design_decisions and spec.is_fresh("design_preview"):
        return False
    if spec.drug is None or not spec.study_type:
        return False
    result = await facades.design_preview(
        drug_id=spec.drug.id, study_type=spec.study_type, population_config=spec.population_config(),
        population_choice=spec.population_choice,
    )
    spec.set_design_decisions(result.get("decisions") or [], population_choice=spec.population_choice)
    return True


async def ensure_ddi(spec: ProtocolDraftSpec, facades: ChatFacades) -> dict[str, Any] | None:
    if spec.drug is None or not spec.study_type:
        return None
    if spec.is_fresh("ddi"):
        return spec.derived_output("ddi")
    result = await facades.ddi_preview(
        drug_id=spec.drug.id, study_type=spec.study_type, population_config=spec.population_config(),
    )
    trimmed = trim_ddi(result)
    spec.set_derived("ddi", trimmed)
    return trimmed


async def ensure_indication(spec: ProtocolDraftSpec, facades: ChatFacades) -> dict[str, Any] | None:
    if spec.drug is None or not spec.study_type:
        return None
    if spec.is_fresh("indication"):
        return spec.derived_output("indication")
    result = await facades.indication_preview(
        drug_id=spec.drug.id, study_type=spec.study_type, user_indication=spec.user_indication,
    )
    spec.set_derived("indication", result)
    return result


async def next_step_cards(
    spec: DraftSpec, facades: ChatFacades, existing: Sequence[Card], created: Sequence[Card],
) -> list[Card]:
    """The card for the spec's next step, unless an active one already exists."""
    if not isinstance(spec, ProtocolDraftSpec):
        return []
    step = spec.next_step()
    if step == "study_type" and not _has_active(existing, created, CardType.study_type):
        return [build_study_type_card(spec)]
    if step == "population" and not _has_active(existing, created, CardType.population):
        return [build_population_card(spec)]
    if step == "design":
        await ensure_design(spec, facades)
        if spec.design_decisions and not _has_active(existing, created, CardType.design_decisions):
            return [build_design_decisions_card(spec)]
        return []
    if step == "indication" and not _has_active(existing, created, CardType.indication):
        result = await ensure_indication(spec, facades)
        return [build_indication_card(spec, result)] if result else []
    if step == "guideline" and not _has_active(existing, created, CardType.guideline):
        return [build_guideline_card(spec)]
    if step == "summary" and not _has_active(existing, created, CardType.protocol_summary):
        issue = await protocol_blocking_issue(spec, facades)
        return [build_protocol_summary_card(spec, blocking_issue=issue)]
    return []


async def protocol_blocking_issue(spec: ProtocolDraftSpec, facades: ChatFacades) -> str | None:
    """The server's refusal (role, license, guideline, drug, study-type gate) for the
    CURRENT draft, so the summary can say so before the user clicks Generate."""
    if not spec.ready():
        return None
    try:
        payload = spec.to_protocol_create()
    except ValueError as exc:
        return str(exc)
    return await facades.preflight_protocol(payload)


async def budget_blocking_issue(spec: BudgetDraftSpec, facades: ChatFacades) -> str | None:
    if not spec.ready():
        return None
    try:
        payload = spec.to_budget_create()
    except ValueError as exc:
        return str(exc)
    try:
        await facades.preflight_budget(payload)
    except FacadeError as exc:
        return exc.message
    return None


# ---------------------------------------------------------------------------
# apply_action
# ---------------------------------------------------------------------------

#: Actions after which the clicked card is finished (becomes ``done``).
_DONE_ON_CLICK = frozenset({
    c.ActionType.select_drug, c.ActionType.set_study_type, c.ActionType.set_guideline,
    c.ActionType.update_population, c.ActionType.confirm_design, c.ActionType.set_indication,
    c.ActionType.add_drug,
})


def _failures(results: Sequence[Any]) -> str | None:
    msgs = [f"{r.field}: {r.message}" for r in results if not r.ok]
    return "; ".join(msgs) if msgs else None


async def apply_action(
    spec: DraftSpec,
    request: c.ActionRequest,
    facades: ChatFacades,
    *,
    session_id: str | None = None,
    existing_cards: Sequence[Card] = (),
) -> ActionOutcome:
    """Apply a card click. Raises :class:`ActionError` (nothing is changed) when it
    cannot be applied. Never mutates ``spec``: the outcome carries the updated copy.

    The router must (1) check the session kind / role / license / ``spec_version``,
    (2) call this, (3) execute ``outcome.intent`` if set and then the matching
    ``after_*`` helper, (4) persist ``outcome.spec`` and every card in
    ``outcome.cards`` (upsert by id), (5) return the ActionResponse."""
    action = request.action
    if spec.kind not in ACTION_SESSION_KINDS[action]:
        raise ActionError(ChatErrorCode.invalid_action, f"'{action.value}' is not available in a {spec.kind.value} chat.")
    payload = request.parsed_payload()
    work = spec.copy_deep()
    existing = list(existing_cards)
    clicked = next((x for x in existing if x.id == request.card_id), None)
    created: list[Card] = []
    note: str | None = None
    intent: ActionIntent | None = None

    try:
        if isinstance(work, ProtocolDraftSpec):
            note, intent, created = await _protocol_action(work, action, payload, facades, existing, session_id)
        elif isinstance(work, BudgetDraftSpec):
            note, intent, created = await _budget_action(work, action, payload, facades, existing, session_id)
        elif isinstance(work, EditSessionSpec):
            note, intent, created = await _edit_action(work, action, payload, existing)
        else:  # pragma: no cover
            raise ActionError(ChatErrorCode.invalid_action, "Unsupported session.")
    except FacadeError as exc:
        raise _facade_error(exc) from None

    changed: dict[str, Card] = {}
    for new in created:
        for old in superseded_by(new, existing):
            changed[old.id] = old
    # Re-render every still-active card whose content depends on the spec ...
    skip = {x.id for x in created}
    for fresh in refresh_cards([x for x in existing if x.id not in changed], work, skip):
        changed[fresh.id] = fresh
    # ... then finish the clicked card (on its refreshed content). The summary
    # cards of generate_protocol / create_budget are finished by the after_*
    # helpers, once the router has succeeded.
    if clicked is not None and action in _DONE_ON_CLICK:
        changed[clicked.id] = with_status(changed.get(clicked.id, clicked), CardStatus.done)
    cards = [*changed.values(), *created]
    guide = Guide(text="Working on it…", chips=[]) if intent is not None else guide_for(work, note=note)
    return ActionOutcome(spec=work, cards=cards, guide=guide, intent=intent)


async def _protocol_action(
    work: ProtocolDraftSpec, action: c.ActionType, payload: Any, facades: ChatFacades,
    existing: list[Card], session_id: str | None,
) -> tuple[str | None, ActionIntent | None, list[Card]]:
    A = c.ActionType
    created: list[Card] = []
    note: str | None = None

    if action == A.select_drug:
        drug = await facades.get_drug(payload.drug_id)
        if drug is None:
            raise ActionError(ChatErrorCode.not_found, "No drug in the catalogue has that id.")
        res = work.set_drug(drug, actor=Actor.click)
        if not res.ok:
            raise ActionError(ChatErrorCode.validation, res.message or "Could not select that drug.")
        note = res.note
        created += await next_step_cards(work, facades, existing, created)

    elif action == A.add_drug:
        return None, AddDrugIntent(inn=payload.inn), []

    elif action == A.set_study_type:
        res = work.set_study_type(payload.study_type, actor=Actor.click)
        if not res.ok:
            raise ActionError(ChatErrorCode.validation, res.message or "That study type is not allowed.")
        created += await next_step_cards(work, facades, existing, created)

    elif action == A.set_guideline:
        res = work.set_guideline(payload.guideline_version, actor=Actor.click)
        if not res.ok:
            raise ActionError(ChatErrorCode.validation, res.message or "That guideline is not available.")
        created += await next_step_cards(work, facades, existing, created)

    elif action == A.update_population:
        unknown = [k for k in payload.patch if k not in PopulationDraft.model_fields]
        if unknown:
            raise ActionError(ChatErrorCode.validation, f"Unknown population setting(s): {', '.join(unknown)}.")
        had_cv = work.population.cv_intra
        results = work.apply_population_batch(payload.patch, actor=Actor.click)
        problem = _failures(results)
        if problem:
            raise ActionError(ChatErrorCode.validation, problem)
        work.reviewed["population"] = True
        if work.drug is not None and work.study_type:
            ddi = await ensure_ddi(work, facades)
            if ddi is not None:
                created.append(build_interaction_screening_card(work, ddi))
        if work.population.cv_intra is not None and work.population.cv_intra != had_cv:
            created.append(build_cv_override_warning_card(work))
        created += await next_step_cards(work, facades, existing, created)

    elif action == A.toggle_exclusion:
        res = work.toggle_exclusion(payload.text, payload.on, actor=Actor.click)
        if not res.ok:
            raise ActionError(ChatErrorCode.validation, res.message or "Could not change the exclusion list.")

    elif action == A.set_design_decision:
        res = work.set_design_decision(payload.key, payload.value, actor=Actor.click)
        if not res.ok:
            raise ActionError(ChatErrorCode.validation, res.message or "That value is not an option.")
        if payload.key == "population" and res.changed:
            await _align_after_population_choice(work, payload.value, facades)
        if work.is_patient_biosimilar and not work.reviewed.get("indication"):
            result = await ensure_indication(work, facades)
            if result and not _has_active(existing, created, CardType.indication):
                created.append(build_indication_card(work, result))

    elif action == A.set_indication:
        res = work.set_user_indication(payload.text, actor=Actor.click)
        if not res.ok:
            raise ActionError(ChatErrorCode.validation, res.message or "That indication is not usable.")
        if work.study_type and work.drug:
            result = await facades.indication_preview(
                drug_id=work.drug.id, study_type=work.study_type, user_indication=work.user_indication,
            )
            work.set_derived("indication", result)
        created += await next_step_cards(work, facades, existing, created)

    elif action == A.confirm_design:
        res = work.confirm_design()
        if not res.ok:
            raise ActionError(ChatErrorCode.invalid_action, res.message or "Nothing to confirm yet.")
        if work.is_patient_biosimilar:
            await ensure_indication(work, facades)
        created += await next_step_cards(work, facades, existing, created)

    elif action == A.generate_protocol:
        if not work.ready():
            raise ActionError(ChatErrorCode.invalid_action, "Not ready to generate: " + ", ".join(_missing_labels(work)) + ".")
        try:
            body = work.to_protocol_create()
        except ValueError as exc:
            raise ActionError(ChatErrorCode.validation, str(exc)) from None
        provenance = work.provenance(session_id=session_id)
        upgraded = work.confirm_proposed()
        provenance["confirmed_by_generate_click"] = upgraded
        return None, GenerateProtocolIntent(payload=body, provenance=provenance), []

    else:  # pragma: no cover - guarded by ACTION_SESSION_KINDS
        raise ActionError(ChatErrorCode.invalid_action, f"'{action.value}' is not available here.")
    return note, None, created


async def _align_after_population_choice(work: ProtocolDraftSpec, value: str, facades: ChatFacades) -> None:
    """Mirror the wizard: switching the Population decision keeps Dose consistent
    locally, then the engine re-derives Dose and the PD endpoint for the choice."""
    dose = work.decision("dose")
    if dose is not None:
        opts = dose.get("options") or []
        if value == POP_PATIENTS_MULTI and dose.get("value") == DOSE_SINGLE and DOSE_MULTIPLE_SS in opts:
            dose["value"] = DOSE_MULTIPLE_SS
        elif value != POP_PATIENTS_MULTI and dose.get("value") == DOSE_MULTIPLE_SS and DOSE_SINGLE in opts:
            dose["value"] = DOSE_SINGLE
    if work.drug is None or not work.study_type:
        return
    fresh = await facades.design_preview(
        drug_id=work.drug.id, study_type=work.study_type, population_config=work.population_config(),
        population_choice=value,
    )
    by_key = {d.get("key"): d for d in fresh.get("decisions") or [] if isinstance(d, dict)}
    work.design_decisions = [
        dict(by_key[d["key"]]) if d.get("key") in ("dose", "pd_endpoint") and d.get("key") in by_key else d
        for d in work.design_decisions
    ]
    work.design_confirmed = False
    work.touch()


async def _budget_action(
    work: BudgetDraftSpec, action: c.ActionType, payload: Any, facades: ChatFacades,
    existing: list[Card], session_id: str | None,
) -> tuple[str | None, ActionIntent | None, list[Card]]:
    A = c.ActionType
    created: list[Card] = []
    if action == A.set_budget_fields:
        results = work.apply_patch(payload.patch, actor=Actor.click)
        problem = _failures(results)
        if problem:
            raise ActionError(ChatErrorCode.validation, problem)
        if work.drug_inn and not work.is_fresh("budget_preview"):
            preview = await facades.budget_profile_preview(work.drug_inn)
            work.set_derived("budget_preview", preview)
            created.append(build_budget_preview_card(work, preview))
    elif action == A.link_protocol:
        if payload.protocol_id is None:
            work.set_protocol(None, actor=Actor.click)
        else:
            option = await facades.get_protocol_option(payload.protocol_id)
            if option is None:
                raise ActionError(ChatErrorCode.not_found, "That protocol was not found in your organization.")
            work.set_protocol(option, actor=Actor.click)
            if work.drug_inn and not work.is_fresh("budget_preview"):
                preview = await facades.budget_profile_preview(work.drug_inn)
                work.set_derived("budget_preview", preview)
    elif action == A.create_budget:
        if not work.ready():
            raise ActionError(ChatErrorCode.invalid_action, "Not ready to create: a drug name (or a linked protocol) is still needed.")
        try:
            body = work.to_budget_create()
        except ValueError as exc:
            raise ActionError(ChatErrorCode.validation, str(exc)) from None
        try:
            await facades.preflight_budget(body)
        except FacadeError as exc:
            raise _facade_error(exc) from None
        provenance = work.provenance(session_id=session_id)
        provenance["confirmed_by_create_click"] = work.confirm_proposed()
        return None, CreateBudgetIntent(payload=body, provenance=provenance), []
    else:  # pragma: no cover
        raise ActionError(ChatErrorCode.invalid_action, f"'{action.value}' is not available here.")
    return None, None, created


async def _edit_action(
    work: EditSessionSpec, action: c.ActionType, payload: Any, existing: list[Card],
) -> tuple[str | None, ActionIntent | None, list[Card]]:
    A = c.ActionType
    pending = work.pending
    if action == A.confirm_edit:
        if pending is None or pending.proposal_id != payload.proposal_id:
            raise ActionError(ChatErrorCode.invalid_action, "That change request is no longer pending. Describe the change again.")
        if not work.editable:
            raise ActionError(ChatErrorCode.invalid_action, work.blocked_reason or "This protocol cannot be edited.")
        return None, ConfirmEditIntent(
            proposal_id=pending.proposal_id, kind=pending.kind, request=pending.request_payload(),
            base_version=pending.base_version,
        ), []
    if action == A.apply_edit:
        return None, ApplyEditIntent(proposal_id=payload.proposal_id), []
    if action == A.discard_edit:
        pending_only = pending is not None and pending.proposal_id == payload.proposal_id
        if pending_only:
            work.clear_pending()
        return None, DiscardEditIntent(proposal_id=payload.proposal_id, pending_only=pending_only), []
    raise ActionError(ChatErrorCode.invalid_action, f"'{action.value}' is not available here.")  # pragma: no cover


# ---------------------------------------------------------------------------
# After the router executed an intent
# ---------------------------------------------------------------------------


def _finish(existing: Sequence[Card], card_id: str, new_cards: Sequence[Card]) -> list[Card]:
    out = list(new_cards)
    clicked = next((x for x in existing if x.id == card_id), None)
    if clicked is not None:
        out.insert(0, with_status(clicked, CardStatus.done))
    return out


def after_generation_started(
    spec: ProtocolDraftSpec, protocol: dict[str, Any], *, clicked_card_id: str, existing: Sequence[Card] = (),
) -> ActionOutcome:
    """Build the outcome after ``start_protocol_generation`` returned ``protocol``
    (``{id, drug_inn?, generation_status}``): the summary card is finished and a
    generation_progress card appears. Bumps the spec version."""
    work = spec.copy_deep()
    work.reviewed["generated"] = True
    work.touch()
    status = str(protocol.get("generation_status") or "generating")
    card = build_generation_progress_card(
        work.version, str(protocol["id"]), protocol.get("drug_inn") or (work.drug.inn if work.drug else None), status,
        protocol.get("generation_error"),
    )
    guide = Guide(
        text="Generation started. This takes a few minutes; you can keep chatting or leave this page.",
        chips=[],
    )
    return ActionOutcome(spec=work, cards=_finish(existing, clicked_card_id, [card]), guide=guide)


def after_budget_created(
    spec: BudgetDraftSpec, budget: dict[str, Any], *, clicked_card_id: str, existing: Sequence[Card] = (),
) -> ActionOutcome:
    work = spec.copy_deep()
    work.touch()
    card = build_budget_ready_card(work.version, budget)
    guide = Guide(text="Budget created. Open it to review every line.", chips=["Link this budget to a protocol"])
    return ActionOutcome(spec=work, cards=_finish(existing, clicked_card_id, [card]), guide=guide)


def after_edit_confirmed(
    spec: EditSessionSpec, proposal: dict[str, Any], *, clicked_card_id: str, existing: Sequence[Card] = (),
) -> ActionOutcome:
    """After the proposal row was created and its job started. ``proposal`` is
    ``{id, kind, status}``."""
    work = spec.copy_deep()
    work.clear_pending()
    card = build_edit_progress_card(work.version, str(proposal["id"]), str(proposal["kind"]), str(proposal.get("status") or "generating"))
    guide = Guide(text="Working on the edit. I will show the differences when it is ready.", chips=[])
    return ActionOutcome(spec=work, cards=_finish(existing, clicked_card_id, [card]), guide=guide)


def after_edit_applied(
    spec: EditSessionSpec, proposal: dict[str, Any], *, clicked_card_id: str, existing: Sequence[Card] = (),
) -> ActionOutcome:
    """After apply succeeded: ``proposal`` is the diff-card view with
    ``status='applied'`` and ``applied_version``. The overview is re-read next turn."""
    work = spec.copy_deep()
    if proposal.get("applied_version"):
        work.version_seen = int(proposal["applied_version"])
    work.touch()
    card = build_edit_diff_card(work.version, proposal)
    done = card.model_copy(update={"status": CardStatus.done})
    guide = Guide(text="Applied. The protocol is now at the new version. Anything else to change?", chips=["Show an overview"])
    return ActionOutcome(spec=work, cards=_finish(existing, clicked_card_id, [done]), guide=guide)


def after_edit_discarded(
    spec: EditSessionSpec, *, clicked_card_id: str, existing: Sequence[Card] = (),
) -> ActionOutcome:
    work = spec.copy_deep()
    work.touch()
    guide = Guide(text="Discarded. Nothing was changed. What would you like to do next?", chips=["Show an overview"])
    return ActionOutcome(spec=work, cards=_finish(existing, clicked_card_id, []), guide=guide)
