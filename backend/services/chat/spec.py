"""The server-side draft specs a chat session builds up (docs/redesign-chat-plan.md B4).

A chat does NOT collect study inputs by parsing prose. Each session holds a
structured draft -- ``ProtocolDraftSpec`` ("protocol_draft@1"),
``BudgetDraftSpec`` ("budget_draft@1") or ``EditSessionSpec`` ("edit_session@1")
-- stored in ``chat_sessions.spec`` (JSONB). Every field records how it got its
value in ``field_meta[path] = {status, source, quote?, at}``, and the payloads
that eventually reach the engines (``to_protocol_create`` -> ``ProtocolCreate``,
``to_budget_create`` -> ``BudgetCreate``) are validated by the SAME pydantic
schemas the wizard used, so the existing sanitisers and gates run unchanged.

PURE module: no database, no FastAPI, no Anthropic SDK. ``schemas`` (and through
it ``models``) and the router's ``study_type_gate_detail`` are imported lazily,
at call time, so importing this module never pulls the application in.

THE QUOTE RULE. When the assistant sets a field from something the user typed,
it must pass ``user_quote``. The field becomes ``confirmed`` (source
``user_text``) only if the quote is a case-insensitive substring of the user's
LATEST message AND actually supports the value (numbers must appear in the quote,
free text must appear in the quote, a drug's name or brand must appear in the
quote). Otherwise the field is stored as ``proposed``: the value is kept, shown
highlighted on the summary card, and is confirmed by the user's click (a card
button, or the Generate / Create click, which confirms everything at once). A
click on a card always yields ``confirmed`` / ``user_click``.

DEFAULTS equal the wizard's (``frontend/src/types.ts::defaultPopulationConfig``)
and the VALIDATION RANGES equal the wizard's inputs: age 18-80, weight 40-150 kg,
within-subject CV 1-120 %.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from services.chat import safety
from services.chat.contract import (
    DEFAULT_GUIDELINE_VERSION,
    MAX_EXCLUSION_TEXT,
    MAX_INDICATION_LENGTH,
    FieldSource,
    FieldStatus,
    PopulationRanges,
    SessionKind,
    SpecField,
    SpecSummary,
)
from services.design_decisions import allowed_study_types, classify_drug_modality
from services.guidelines import GUIDELINES, guideline_display_name, is_current, is_supported
from services.patient_population import sanitize_user_indication

SPEC_SCHEMA_PROTOCOL = "protocol_draft@1"
SPEC_SCHEMA_BUDGET = "budget_draft@1"
SPEC_SCHEMA_EDIT = "edit_session@1"

STUDY_TYPES: tuple[str, ...] = ("BE", "biosimilar_pk", "food_effect")
STUDY_TYPE_LABELS: dict[str, str] = {
    "BE": "Bioequivalence",
    "biosimilar_pk": "Biosimilar PK Similarity",
    "food_effect": "Food Effect",
}

#: The validation ranges chat enforces (the wizard's input ranges).
RANGES = PopulationRanges()

#: Drug-agnostic exclusion criteria (frontend PopulationBuilder GENERIC_EXCLUSIONS).
GENERIC_EXCLUSION_OPTIONS: tuple[str, ...] = (
    "Tobacco or nicotine use within 6 months",
    "Alcohol use > 14 units per week",
    "Prescription medications within 14 days",
    "Blood donation within 3 months",
    "Heavy exercise within 48 hours of dosing",
    "History of drug or alcohol abuse",
)
#: The wizard pre-selects only this one (frontend defaultPopulationConfig).
DEFAULT_EXCLUSIONS: tuple[str, ...] = ("Tobacco or nicotine use within 6 months",)

MAX_EXCLUSIONS = 40
MAX_FREE_TEXT = 200
MAX_TIMEPOINT_TEXT = 200

_INN_RE = re.compile(r"^[A-Za-z][A-Za-z \-]{1,63}$")
_BUDGET_DRUG_RE = re.compile(r"^[A-Za-z][A-Za-z0-9 \-]{1,63}$")
_NUMBER_IN_TEXT = re.compile(r"\d+(?:\.\d+)?")

PROTOCOL_DRAFT_FIELDS: tuple[str, ...] = (
    "drug_id", "study_type", "guideline_version", "subject_type",
    "age_min", "age_max", "sex", "weight_min", "weight_max", "cv_intra", "is_nti",
    "dose_strength", "reference_product",
    "additional_pk_timepoints_hours", "additional_immunogenicity_timepoints_months",
    "exclusion_add", "exclusion_remove", "user_indication",
)
BUDGET_DRAFT_FIELDS: tuple[str, ...] = (
    "drug_inn", "study_type", "subject_profile", "indication", "therapeutic_area",
    "n_sites_selected", "n_countries", "n_enrolled", "geography_profile",
    "currency", "name", "sponsor_name", "cro_name",
)
#: Inputs an edit session may change on a finished protocol (engine edits).
EDIT_INPUT_FIELDS: tuple[str, ...] = (
    "cv_intra", "is_nti", "dose_strength", "reference_product",
    "additional_pk_timepoints_hours", "additional_immunogenicity_timepoints_months",
    "exclusion_add", "exclusion_remove", "user_indication", "guideline_version",
    "subject_type", "age_min", "age_max", "sex", "weight_min", "weight_max",
    "design_decision_population", "design_decision_dose", "design_decision_route",
    "design_decision_design", "design_decision_pd_endpoint", "washout_days",
)
DECISION_KEYS: tuple[str, ...] = ("population", "dose", "route", "design", "pd_endpoint")

BUDGET_CURRENCIES: tuple[str, ...] = ("USD", "EUR", "GBP", "CAD", "AUD", "CHF", "JPY")
BUDGET_SUBJECT_PROFILES: tuple[str, ...] = ("healthy", "patient")
BUDGET_THERAPEUTIC_AREAS: tuple[str, ...] = ("oncology", "other")
MAX_BUDGET_SITES = 1000
MAX_BUDGET_COUNTRIES = 100
MAX_BUDGET_ENROLLED = 100_000


# ---------------------------------------------------------------------------
# Small shared types
# ---------------------------------------------------------------------------


class Actor(StrEnum):
    """Who is setting a field: the assistant (a tool call) or the user (a click)."""

    model = "model"
    click = "click"
    engine = "engine"


class FieldError(ValueError):
    """A value the validators reject; ``str(exc)`` is the user-facing message."""


@dataclass
class SetResult:
    ok: bool
    field: str
    message: str | None = None
    #: Status of the field after the call (None when rejected).
    status: FieldStatus | None = None
    changed: bool = False
    #: Server note on an accepted value (e.g. "superseded guideline").
    note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "accepted": self.ok,
            "status": self.status.value if self.status else None,
            "message": self.message,
            "note": self.note,
        }


class FieldMeta(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: FieldStatus
    source: FieldSource
    quote: str | None = None
    at: str


class DerivedEntry(BaseModel):
    """A cached engine output with the hash of the inputs that produced it."""

    model_config = ConfigDict(extra="ignore")

    input_hash: str
    output: Any = None
    at: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def input_hash(inputs: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(inputs, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _norm(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def quote_verified(quote: str | None, user_text: str | None) -> bool:
    """The quote rule's core test: a non-trivial quote that is a case-insensitive
    substring of the user's latest message."""
    q = _norm(quote)
    if len(q) < 2:
        return False
    return q in _norm(user_text)


def _numbers(text: str) -> set[str]:
    out: set[str] = set()
    for tok in _NUMBER_IN_TEXT.findall(text or ""):
        out.add(tok.rstrip("0").rstrip(".") if "." in tok else tok)
    return out


def _provenance(
    actor: Actor,
    quote: str | None,
    user_text: str | None,
    *,
    supports: Callable[[str], bool] | None = None,
) -> tuple[FieldStatus, FieldSource, str | None]:
    """(status, source, quote) for a value set by ``actor``.

    ``supports(normalised_quote)`` is the relatedness guard: when given, a quote
    that is a substring of the user's message but does not contain the value
    still leaves the field only ``proposed``."""
    if actor == Actor.click:
        return FieldStatus.confirmed, FieldSource.user_click, None
    if actor == Actor.engine:
        return FieldStatus.engine, FieldSource.engine, None
    clean_quote = safety.clean_free_text(quote, 300) if quote else None
    if clean_quote and quote_verified(clean_quote, user_text):
        if supports is None or supports(_norm(clean_quote)):
            return FieldStatus.confirmed, FieldSource.user_text, clean_quote
    return FieldStatus.proposed, FieldSource.user_text, clean_quote or None


def study_type_gate_message(drug_inn: str, study_type: str) -> str | None:
    """The server's own 422 wording for a study type the drug cannot run.

    Delegates to ``routers.protocols.study_type_gate_detail`` -- the single
    source of the wording for the protocol gate AND the budget gate -- imported
    lazily so this module stays free of the application at import time."""
    from routers.protocols import study_type_gate_detail

    return study_type_gate_detail(drug_inn, study_type)


def unsupported_guideline_message(version: str) -> str:
    """The server's wording (``validate_protocol_request``) for an unsupported guideline."""
    return f"Unsupported guideline '{version}'. Supported: {', '.join(GUIDELINES)}."


# ---------------------------------------------------------------------------
# Value parsers (shared by the protocol spec, the budget spec and edit sessions)
# ---------------------------------------------------------------------------


def _as_text(raw: Any) -> str:
    if raw is None:
        return ""
    if isinstance(raw, bool):
        return "true" if raw else "false"
    return str(raw).strip()


def parse_int(raw: Any, name: str, lo: int, hi: int) -> int:
    if isinstance(raw, bool):
        raise FieldError(f"{name} must be a whole number between {lo} and {hi}.")
    if isinstance(raw, (int, float)):
        value = float(raw)
    else:
        text = _as_text(raw).replace(",", "")
        try:
            value = float(text)
        except ValueError:
            raise FieldError(f"{name} must be a whole number between {lo} and {hi}.") from None
    if value != int(value):
        raise FieldError(f"{name} must be a whole number between {lo} and {hi}.")
    n = int(value)
    if not lo <= n <= hi:
        raise FieldError(f"{name} must be between {lo} and {hi}.")
    return n


def parse_float(raw: Any, name: str, lo: float, hi: float) -> float:
    if isinstance(raw, bool):
        raise FieldError(f"{name} must be a number between {lo:g} and {hi:g}.")
    if isinstance(raw, (int, float)):
        value = float(raw)
    else:
        text = _as_text(raw).replace(",", "").rstrip("%").strip()
        try:
            value = float(text)
        except ValueError:
            raise FieldError(f"{name} must be a number between {lo:g} and {hi:g}.") from None
    if value != value or value in (float("inf"), float("-inf")):
        raise FieldError(f"{name} must be a number between {lo:g} and {hi:g}.")
    if not lo <= value <= hi:
        raise FieldError(f"{name} must be between {lo:g} and {hi:g}.")
    return round(value, 2)


def parse_bool(raw: Any, name: str) -> bool:
    if isinstance(raw, bool):
        return raw
    text = _as_text(raw).lower()
    if text in ("true", "yes", "y", "1"):
        return True
    if text in ("false", "no", "n", "0"):
        return False
    raise FieldError(f"{name} must be true or false.")


def parse_choice(raw: Any, name: str, choices: tuple[str, ...], *, case_insensitive: bool = True) -> str:
    text = _as_text(raw)
    for choice in choices:
        if text == choice or (case_insensitive and text.lower() == choice.lower()):
            return choice
    raise FieldError(f"{name} must be one of: {', '.join(choices)}.")


def parse_free_text(raw: Any, name: str, max_len: int = MAX_FREE_TEXT) -> str | None:
    """Single-line free text, cleaned; empty / None clears (returns None)."""
    if raw is None:
        return None
    text = safety.clean_free_text(_as_text(raw), max_len)
    return text or None


def parse_timepoints(raw: Any, name: str) -> str | None:
    """Free text the engines parse permissively; chat only requires that it
    contains at least one number (otherwise nothing would be extracted)."""
    text = parse_free_text(raw, name, MAX_TIMEPOINT_TEXT)
    if text is None:
        return None
    if not _NUMBER_IN_TEXT.search(text):
        raise FieldError(f"{name} must contain at least one number (e.g. \"36, 48, 72\").")
    return text


_FREE_TEXT_FIELDS = ("dose_strength", "reference_product")
_NUMBER_TEXT_FIELDS = ("additional_pk_timepoints_hours", "additional_immunogenicity_timepoints_months")


def parse_population_value(name: str, raw: Any) -> Any:
    """Validate one population key (shared by drafts, card patches and edit
    sessions). Raises :class:`FieldError` with a user-facing message."""
    if name == "subject_type":
        return parse_choice(raw, name, ("healthy", "patient"))
    if name == "sex":
        return parse_choice(raw, name, ("male", "female", "both"))
    if name in ("age_min", "age_max"):
        return parse_int(raw, name, RANGES.age_min, RANGES.age_max)
    if name in ("weight_min", "weight_max"):
        return parse_int(raw, name, RANGES.weight_min, RANGES.weight_max)
    if name == "cv_intra":
        if raw is None or _as_text(raw) == "":
            return None
        return parse_float(raw, name, RANGES.cv_min, RANGES.cv_max)
    if name == "is_nti":
        if raw is None or _as_text(raw) == "":
            return None
        return parse_bool(raw, name)
    if name in _FREE_TEXT_FIELDS:
        return parse_free_text(raw, name)
    if name in _NUMBER_TEXT_FIELDS:
        return parse_timepoints(raw, name)
    if name == "exclusion_presets":
        if not isinstance(raw, (list, tuple)):
            raise FieldError("exclusion_presets must be a list of criteria.")
        cleaned: list[str] = []
        for item in raw:
            text = safety.clean_free_text(_as_text(item), MAX_EXCLUSION_TEXT)
            if text and text not in cleaned:
                cleaned.append(text)
        if len(cleaned) > MAX_EXCLUSIONS:
            raise FieldError(f"At most {MAX_EXCLUSIONS} exclusion criteria are allowed.")
        return cleaned
    raise FieldError(f"'{name}' is not a population setting.")


# ---------------------------------------------------------------------------
# Spec base
# ---------------------------------------------------------------------------


class _SpecBase(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    schema_id: str = Field(default="", alias="schema")
    #: Monotonic counter; mirrored into ``chat_sessions.spec_version``.
    version: int = 0
    field_meta: dict[str, FieldMeta] = Field(default_factory=dict)
    derived: dict[str, DerivedEntry] = Field(default_factory=dict)
    #: Steps the user has been shown / has passed (e.g. {"population": True}).
    reviewed: dict[str, bool] = Field(default_factory=dict)

    kind: SessionKind = SessionKind.protocol  # overridden by subclasses

    # -- serialisation ------------------------------------------------------
    def to_json(self) -> dict[str, Any]:
        """JSON-safe dict for ``chat_sessions.spec`` (keys by alias, so the
        ``schema`` key is present)."""
        return self.model_dump(mode="json", by_alias=True)

    @classmethod
    def from_json(cls, data: dict[str, Any]):
        return cls.model_validate(data)

    def copy_deep(self):
        return type(self).from_json(self.to_json())

    # -- bookkeeping --------------------------------------------------------
    def touch(self) -> int:
        self.version += 1
        return self.version

    def stamp(
        self,
        path: str,
        status: FieldStatus,
        source: FieldSource,
        quote: str | None = None,
    ) -> None:
        self.field_meta[path] = FieldMeta(status=status, source=source, quote=quote, at=_now())

    def meta(self, path: str) -> FieldMeta | None:
        return self.field_meta.get(path)

    def status_of(self, path: str) -> FieldStatus:
        m = self.field_meta.get(path)
        return m.status if m else FieldStatus.empty

    def clear_meta(self, *paths: str) -> None:
        for p in paths:
            self.field_meta.pop(p, None)

    def group_meta(self, *paths: str) -> tuple[FieldStatus, FieldSource | None]:
        """The weakest status of several member fields (proposed < default <
        engine < confirmed) and the source of the first member that has one."""
        metas = [self.field_meta[p] for p in paths if p in self.field_meta]
        if not metas:
            return FieldStatus.empty, None
        order = {
            FieldStatus.empty: 0, FieldStatus.proposed: 1, FieldStatus.default: 2,
            FieldStatus.engine: 3, FieldStatus.confirmed: 4,
        }
        weakest = min(metas, key=lambda m: order[m.status])
        return weakest.status, weakest.source

    def confirm_proposed(self, *, only: set[str] | None = None) -> list[str]:
        """Upgrade ``proposed`` fields to ``confirmed`` / ``user_click`` (the
        user's Generate / Create / Confirm click covers them). Returns the paths
        upgraded. The original quote is kept for the audit trail."""
        upgraded: list[str] = []
        for path, m in self.field_meta.items():
            if m.status == FieldStatus.proposed and (only is None or path in only):
                self.field_meta[path] = FieldMeta(
                    status=FieldStatus.confirmed, source=FieldSource.user_click,
                    quote=m.quote, at=_now(),
                )
                upgraded.append(path)
        return upgraded

    def field_provenance(self) -> dict[str, dict[str, Any]]:
        """``{path: {status, source, quote, at}}`` for every recorded field -- the
        audit record written into the INITIAL snapshot's change_detail."""
        return {p: m.model_dump(mode="json", exclude_none=True) for p, m in sorted(self.field_meta.items())}

    # -- subclass hooks -----------------------------------------------------
    def missing(self) -> list[str]:  # pragma: no cover - abstract
        raise NotImplementedError

    def ready(self) -> bool:
        return not self.missing()

    def summary_fields(self) -> list[SpecField]:  # pragma: no cover - abstract
        raise NotImplementedError

    def summary(self) -> SpecSummary:
        return SpecSummary(
            kind=self.kind,
            spec_version=self.version,
            ready=self.ready(),
            missing=self.missing(),
            fields=self.summary_fields(),
        )


# ---------------------------------------------------------------------------
# Protocol draft
# ---------------------------------------------------------------------------


class DrugRef(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    inn: str
    modality: str = "unknown"
    brand_names: list[str] = Field(default_factory=list)


class PopulationDraft(BaseModel):
    """The population / design hints, defaults equal to ``defaultPopulationConfig``."""

    model_config = ConfigDict(extra="ignore")

    subject_type: Literal["healthy", "patient"] = "healthy"
    age_min: int = 18
    age_max: int = 55
    sex: Literal["male", "female", "both"] = "both"
    weight_min: int = 50
    weight_max: int = 100
    exclusion_presets: list[str] = Field(default_factory=lambda: list(DEFAULT_EXCLUSIONS))
    cv_intra: float | None = None
    dose_strength: str | None = None
    reference_product: str | None = None
    is_nti: bool | None = None
    additional_pk_timepoints_hours: str | None = None
    additional_immunogenicity_timepoints_months: str | None = None


_POPULATION_PATHS: dict[str, str] = {
    name: f"population.{name}" for name in PopulationDraft.model_fields
}
_DESIGN_INPUTS = ("drug", "study_type", "subject_type", "is_nti")


def _default_protocol_meta() -> dict[str, FieldMeta]:
    at = _now()
    meta: dict[str, FieldMeta] = {
        "guideline_version": FieldMeta(status=FieldStatus.default, source=FieldSource.default, at=at),
    }
    for name in ("subject_type", "age_min", "age_max", "sex", "weight_min", "weight_max", "exclusion_presets"):
        meta[f"population.{name}"] = FieldMeta(status=FieldStatus.default, source=FieldSource.default, at=at)
    return meta


class ProtocolDraftSpec(_SpecBase):
    """Everything the protocol wizard collected, as one server-owned draft."""

    schema_id: str = Field(default=SPEC_SCHEMA_PROTOCOL, alias="schema")
    kind: SessionKind = SessionKind.protocol

    drug: DrugRef | None = None
    study_type: str | None = None
    guideline_version: str = DEFAULT_GUIDELINE_VERSION
    population: PopulationDraft = Field(default_factory=PopulationDraft)
    #: The five design decisions ({key,label,value,rationale,evidence,options}).
    design_decisions: list[dict[str, Any]] = Field(default_factory=list)
    design_confirmed: bool = False
    #: The reviewer's explicit Population decision (re-derives dose / PD), if any.
    population_choice: str | None = None
    user_indication: str | None = None
    field_meta: dict[str, FieldMeta] = Field(default_factory=_default_protocol_meta)

    # ------------------------------------------------------------------
    # derived-output cache (input hashes)
    # ------------------------------------------------------------------
    def current_hash(self, name: str) -> str | None:
        """Hash of the inputs ``name``'s engine output depends on, or None when
        a required input is still empty."""
        if self.drug is None or not self.study_type:
            return None
        base = {
            "drug_id": self.drug.id,
            "study_type": self.study_type,
            "subject_type": self.population.subject_type,
            "is_nti": self.population.is_nti,
        }
        if name == "design_preview":
            base["population_choice"] = self.population_choice
            return input_hash(base)
        if name == "ddi":
            return input_hash(base)
        if name == "indication":
            return input_hash({
                "drug_id": self.drug.id, "study_type": self.study_type,
                "user_indication": self.user_indication,
            })
        raise KeyError(name)

    def is_fresh(self, name: str) -> bool:
        entry = self.derived.get(name)
        return bool(entry and entry.input_hash == self.current_hash(name))

    def set_derived(self, name: str, output: Any) -> None:
        h = self.current_hash(name)
        if h is None:
            return
        self.derived[name] = DerivedEntry(input_hash=h, output=output, at=_now())

    def derived_output(self, name: str) -> Any:
        """The cached output if (and only if) it is fresh for the current inputs."""
        return self.derived[name].output if self.is_fresh(name) else None

    def sync_derived(self) -> None:
        """Drop every cached engine output whose inputs changed. A stale design
        preview also drops the decisions and the confirmation: decisions
        computed for a different drug / study type / subject type must never be
        confirmed or generated from."""
        for name in list(self.derived):
            if not self.is_fresh(name):
                del self.derived[name]
        if "design_preview" not in self.derived and self.design_decisions:
            self.design_decisions = []
            self.design_confirmed = False
            self.population_choice = None
            self.clear_meta(*[f"design.{k}" for k in DECISION_KEYS], "design_decisions")
        if not self.design_decisions:
            self.design_confirmed = False

    # ------------------------------------------------------------------
    # derived facts
    # ------------------------------------------------------------------
    @property
    def is_patient_biosimilar(self) -> bool:
        """The engine's own condition for the indication step."""
        pop = next((d for d in self.design_decisions if d.get("key") == "population"), None)
        patient_decision = bool(pop and str(pop.get("value", "")).startswith("Patients"))
        return self.study_type == "biosimilar_pk" and (
            self.population.subject_type == "patient" or patient_decision
        )

    def decision(self, key: str) -> dict[str, Any] | None:
        return next((d for d in self.design_decisions if d.get("key") == key), None)

    def allowed_study_types(self) -> tuple[str, ...] | None:
        return allowed_study_types(self.drug.inn) if self.drug else None

    def exclusions(self) -> list[str]:
        return list(self.population.exclusion_presets)

    def population_values(self) -> dict[str, Any]:
        return self.population.model_dump(mode="json")

    # ------------------------------------------------------------------
    # setters
    # ------------------------------------------------------------------
    def set_drug(
        self,
        drug: dict[str, Any],
        *,
        actor: Actor,
        quote: str | None = None,
        user_text: str | None = None,
    ) -> SetResult:
        """Choose the drug from a catalogue row ``{id, inn, brand_names?, modality?}``."""
        inn = str(drug.get("inn") or "").strip()
        drug_id = str(drug.get("id") or "").strip()
        if not drug_id or not _INN_RE.match(inn):
            return SetResult(False, "drug_id", "That is not a valid catalogue drug (inn must be 2-64 letters, spaces, or hyphens only).")
        brands = [str(b) for b in (drug.get("brand_names") or []) if str(b).strip()]
        inn_l = _norm(inn)

        def _supports(q: str) -> bool:
            return inn_l in q or any(_norm(b) and _norm(b) in q for b in brands)

        status, source, qt = _provenance(actor, quote, user_text, supports=_supports)
        previous = self.drug
        self.drug = DrugRef(
            id=drug_id, inn=inn.lower(), brand_names=brands,
            modality=str(drug.get("modality") or classify_drug_modality(inn)),
        )
        self.stamp("drug", status, source, qt)
        changed = previous is None or previous.id != drug_id
        note = None
        if changed and self.study_type:
            gate = study_type_gate_message(self.drug.inn, self.study_type)
            if gate is not None:
                note = f"The study type was cleared: {gate}"
                self.study_type = None
                self.clear_meta("study_type")
        if changed:
            self.reviewed.pop("indication", None)
        self.sync_derived()
        self.touch()
        return SetResult(True, "drug_id", status=status, changed=changed, note=note)

    def set_study_type(
        self,
        value: Any,
        *,
        actor: Actor,
        quote: str | None = None,
        user_text: str | None = None,
    ) -> SetResult:
        try:
            st = parse_choice(value, "study_type", STUDY_TYPES, case_insensitive=False)
        except FieldError as exc:
            return SetResult(False, "study_type", str(exc))
        if self.drug is not None:
            gate = study_type_gate_message(self.drug.inn, st)
            if gate is not None:
                return SetResult(False, "study_type", gate)
        status, source, qt = _provenance(actor, quote, user_text)
        changed = self.study_type != st
        self.study_type = st
        self.stamp("study_type", status, source, qt)
        if changed:
            self.reviewed.pop("indication", None)
        self.sync_derived()
        self.touch()
        return SetResult(True, "study_type", status=status, changed=changed)

    def set_guideline(
        self,
        value: Any,
        *,
        actor: Actor,
        quote: str | None = None,
        user_text: str | None = None,
    ) -> SetResult:
        version = _as_text(value)
        if not is_supported(version):
            return SetResult(False, "guideline_version", unsupported_guideline_message(version))
        if not is_current(version):
            return SetResult(
                False, "guideline_version",
                f"'{version}' is superseded and cannot be chosen for new work. "
                f"Choose a current guideline: {', '.join(g.id for g in GUIDELINES.values() if g.is_current)}.",
            )
        status, source, qt = _provenance(actor, quote, user_text)
        changed = self.guideline_version != version
        self.guideline_version = version
        self.stamp("guideline_version", status, source, qt)
        self.reviewed["guideline"] = True
        self.touch()
        return SetResult(True, "guideline_version", status=status, changed=changed)

    def set_user_indication(
        self,
        value: Any,
        *,
        actor: Actor,
        quote: str | None = None,
        user_text: str | None = None,
    ) -> SetResult:
        """The sponsor's own indication for a patient biosimilar study (sanitised
        exactly as the server does); empty / None clears it (engine chooses)."""
        if value is None or _as_text(value) == "":
            had = self.user_indication is not None
            self.user_indication = None
            self.clear_meta("user_indication")
            self.reviewed["indication"] = True
            self.sync_derived()
            self.touch()
            return SetResult(True, "user_indication", status=FieldStatus.empty, changed=had)
        clean = sanitize_user_indication(_as_text(value))
        if clean is None:
            return SetResult(False, "user_indication", "That indication contained nothing usable after sanitising (letters, numbers and basic punctuation only).")
        clean = clean[:MAX_INDICATION_LENGTH]
        low = _norm(clean)
        status, source, qt = _provenance(actor, quote, user_text, supports=lambda q: low in q)
        changed = self.user_indication != clean
        self.user_indication = clean
        self.stamp("user_indication", status, source, qt)
        self.reviewed["indication"] = True
        self.sync_derived()
        self.touch()
        note = None
        if self.study_type and not self.is_patient_biosimilar:
            note = "Stored, but the server only honours an indication for a patient biosimilar PK study."
        return SetResult(True, "user_indication", status=status, changed=changed, note=note)

    def set_population_field(
        self,
        name: str,
        value: Any,
        *,
        actor: Actor,
        quote: str | None = None,
        user_text: str | None = None,
        _batch: bool = False,
    ) -> SetResult:
        """Set one population key. ``_batch=True`` skips the cross-field
        (min <= max) check; :meth:`apply_population_batch` runs it once at the end."""
        if name not in PopulationDraft.model_fields:
            return SetResult(False, name, f"'{name}' is not a population setting.")
        path = _POPULATION_PATHS[name]
        try:
            parsed = self._parse_population(name, value)
        except FieldError as exc:
            return SetResult(False, name, str(exc))

        text_ok: Callable[[str], bool] | None = None
        if parsed is not None:
            if name in ("age_min", "age_max", "weight_min", "weight_max", "cv_intra") or name in _NUMBER_TEXT_FIELDS:
                # Numeric values must literally appear in the quote.
                wanted = _numbers(_as_text(parsed))
                text_ok = lambda q, wanted=wanted: bool(wanted) and wanted <= _numbers(q)  # noqa: E731
            elif name in _FREE_TEXT_FIELDS:
                low = _norm(parsed)
                text_ok = lambda q, low=low: low in q  # noqa: E731
        status, source, qt = _provenance(actor, quote, user_text, supports=text_ok)

        before = getattr(self.population, name)
        setattr(self.population, name, parsed)
        if not _batch:
            problem = self._range_problem()
            if problem:
                setattr(self.population, name, before)
                return SetResult(False, name, problem)
        if parsed is None:
            # Cleared: back to the engine's default behaviour.
            if name == "cv_intra":
                self.stamp(path, FieldStatus.engine, FieldSource.engine)
            else:
                self.clear_meta(path)
        else:
            self.stamp(path, status, source, qt)
        changed = before != parsed
        if name in ("subject_type", "is_nti"):
            self.reviewed.pop("indication", None)
        if actor != Actor.engine:
            self.reviewed["population"] = True
        self.sync_derived()
        self.touch()
        return SetResult(True, name, status=self.status_of(path), changed=changed)

    def _parse_population(self, name: str, raw: Any) -> Any:
        return parse_population_value(name, raw)

    def _range_problem(self) -> str | None:
        p = self.population
        if p.age_min > p.age_max:
            return f"age_min ({p.age_min}) cannot exceed age_max ({p.age_max})."
        if p.weight_min > p.weight_max:
            return f"weight_min ({p.weight_min}) cannot exceed weight_max ({p.weight_max})."
        return None

    def _apply_population_items(
        self,
        items: list[tuple[str, Any, str | None]],
        *,
        actor: Actor,
        user_text: str | None,
    ) -> list[SetResult]:
        """Apply ``(name, value, quote)`` population items together, then run the
        cross-field rules once: if the batch leaves age_min > age_max (or the
        weight pair), that pair is rolled back and reported as rejected, so
        ``age_min=60, age_max=75`` can move together past the old bounds."""
        snapshot = self.population.model_copy(deep=True)
        meta_snapshot = {p: m.model_copy() for p, m in self.field_meta.items()}
        results = [
            self.set_population_field(name, value, actor=actor, quote=quote, user_text=user_text, _batch=True)
            for name, value, quote in items
        ]
        problem = self._range_problem()
        if problem:
            pair = ("age_min", "age_max") if problem.startswith("age") else ("weight_min", "weight_max")
            for name in pair:
                setattr(self.population, name, getattr(snapshot, name))
                path = _POPULATION_PATHS[name]
                if path in meta_snapshot:
                    self.field_meta[path] = meta_snapshot[path]
                else:
                    self.clear_meta(path)
            for r in results:
                if r.field in pair and r.ok:
                    r.ok, r.message, r.status, r.changed = False, problem, None, False
        return results

    def apply_population_batch(
        self,
        patch: dict[str, Any],
        *,
        actor: Actor,
        quote: str | None = None,
        user_text: str | None = None,
    ) -> list[SetResult]:
        """Apply a card ``update_population`` patch (same cross-field handling)."""
        return self._apply_population_items(
            [(k, v, quote) for k, v in patch.items()], actor=actor, user_text=user_text
        )

    def toggle_exclusion(
        self,
        text: str,
        on: bool,
        *,
        actor: Actor,
        quote: str | None = None,
        user_text: str | None = None,
    ) -> SetResult:
        crit = safety.clean_free_text(_as_text(text), MAX_EXCLUSION_TEXT)
        if not crit:
            return SetResult(False, "exclusion", "The exclusion criterion was empty.")
        items = list(self.population.exclusion_presets)
        changed = False
        if on and crit not in items:
            if len(items) >= MAX_EXCLUSIONS:
                return SetResult(False, "exclusion", f"At most {MAX_EXCLUSIONS} exclusion criteria are allowed.")
            items.append(crit)
            changed = True
        elif not on and crit in items:
            items.remove(crit)
            changed = True
        self.population.exclusion_presets = items
        status, source, qt = _provenance(actor, quote, user_text, supports=lambda q, c=_norm(crit): c in q)
        self.stamp("population.exclusion_presets", status, source, qt)
        if actor != Actor.engine:
            self.reviewed["population"] = True
        self.touch()
        return SetResult(True, "exclusion", status=status, changed=changed)

    # -- design decisions -------------------------------------------------
    def set_design_decisions(self, decisions: list[dict[str, Any]], *, population_choice: str | None = None) -> None:
        """Install a freshly computed decision set (engine output) and cache it
        against the current input hash. Resets the confirmation."""
        self.population_choice = population_choice
        self.design_decisions = [dict(d) for d in decisions]
        self.design_confirmed = False
        self.set_derived("design_preview", [d.get("key") for d in decisions])
        for d in self.design_decisions:
            self.stamp(f"design.{d.get('key')}", FieldStatus.engine, FieldSource.engine)
        self.touch()

    def set_design_decision(
        self,
        key: str,
        value: Any,
        *,
        actor: Actor,
        quote: str | None = None,
        user_text: str | None = None,
    ) -> SetResult:
        """Override one decision; the value must be one of that decision's options."""
        field_name = f"design.{key}"
        d = self.decision(key)
        if d is None:
            return SetResult(False, field_name, f"No '{key}' design decision is loaded yet; run the design preview first.")
        text = _as_text(value)
        options = list(d.get("options") or [])
        if text not in options:
            return SetResult(False, field_name, f"'{text}' is not an option for {d.get('label', key)}. Options: {'; '.join(options)}.")
        status, source, qt = _provenance(actor, quote, user_text)
        changed = d.get("value") != text
        d["value"] = text
        if changed:
            d["rationale"] = (d.get("rationale") or "") + f" (Reviewer-selected: {text}.)"
        self.stamp(field_name, status, source, qt)
        if key == "population" and changed:
            self.population_choice = text
            # The choice is part of the design-preview input hash: re-stamp the
            # cache so the decision set stays "fresh" for the new choice.
            self.set_derived("design_preview", [x.get("key") for x in self.design_decisions])
        if changed:
            self.design_confirmed = False
            self.clear_meta("design_decisions")
        self.touch()
        return SetResult(True, field_name, status=status, changed=changed)

    def confirm_design(self) -> SetResult:
        """The user's click on "Confirm design": also upgrades a ``proposed``
        drug / study type (the card shows both in its header)."""
        if not self.design_decisions:
            return SetResult(False, "design_decisions", "Load the design decisions first.")
        self.design_confirmed = True
        self.confirm_proposed(only={"drug", "study_type"} | {f"design.{k}" for k in DECISION_KEYS})
        for k in DECISION_KEYS:
            if self.decision(k) is not None:
                self.stamp(f"design.{k}", FieldStatus.confirmed, FieldSource.user_click)
        self.stamp("design_decisions", FieldStatus.confirmed, FieldSource.user_click)
        self.reviewed["design"] = True
        self.touch()
        return SetResult(True, "design_decisions", status=FieldStatus.confirmed, changed=True)

    # -- generic entry used by the update_draft tool ----------------------
    def apply_field(
        self,
        field: str,
        raw: Any,
        *,
        actor: Actor,
        quote: str | None = None,
        user_text: str | None = None,
        drug: dict[str, Any] | None = None,
    ) -> SetResult:
        """Apply one ``update_draft`` change. ``drug`` is the resolved catalogue
        row when ``field == "drug_id"`` (the caller looks it up)."""
        kw = {"actor": actor, "quote": quote, "user_text": user_text}
        if field == "drug_id":
            if drug is None:
                return SetResult(False, field, "No drug in the catalogue has that id. Use search_drugs first.")
            return self.set_drug(drug, **kw)
        if field == "study_type":
            return self.set_study_type(raw, **kw)
        if field == "guideline_version":
            return self.set_guideline(raw, **kw)
        if field == "user_indication":
            return self.set_user_indication(raw, **kw)
        if field == "exclusion_add":
            return self.toggle_exclusion(_as_text(raw), True, **kw)
        if field == "exclusion_remove":
            return self.toggle_exclusion(_as_text(raw), False, **kw)
        if field in PopulationDraft.model_fields:
            return self.set_population_field(field, raw, **kw)
        return SetResult(False, field, f"'{field}' is not a field you can set. Allowed: {', '.join(PROTOCOL_DRAFT_FIELDS)}.")

    def apply_changes(
        self,
        changes: list[dict[str, Any]],
        *,
        user_text: str | None,
        drugs: dict[str, dict[str, Any]] | None = None,
    ) -> list[SetResult]:
        """Apply a batch of ``{field, value, user_quote}`` changes made by the
        assistant, returning one result per change in order. Population keys are
        applied together so min/max pairs may move at once."""
        results: list[SetResult | None] = [None] * len(changes)
        pop_idx = [i for i, c in enumerate(changes) if c.get("field") in PopulationDraft.model_fields]
        pop_results = self._apply_population_items(
            [(changes[i]["field"], changes[i].get("value"), changes[i].get("user_quote")) for i in pop_idx],
            actor=Actor.model, user_text=user_text,
        )
        for i, r in zip(pop_idx, pop_results):
            results[i] = r
        for i, c in enumerate(changes):
            if results[i] is not None:
                continue
            field = str(c.get("field"))
            drug = (drugs or {}).get(str(c.get("value"))) if field == "drug_id" else None
            results[i] = self.apply_field(
                field, c.get("value"), actor=Actor.model, quote=c.get("user_quote"),
                user_text=user_text, drug=drug,
            )
        return [r for r in results if r is not None]

    # ------------------------------------------------------------------
    # readiness
    # ------------------------------------------------------------------
    def missing(self) -> list[str]:
        out: list[str] = []
        if self.drug is None or self.status_of("drug") != FieldStatus.confirmed:
            out.append("drug")
        if not self.study_type or self.status_of("study_type") != FieldStatus.confirmed:
            out.append("study_type")
        if not (self.design_decisions and self.design_confirmed and self.is_fresh("design_preview")):
            out.append("design_decisions")
        if not is_supported(self.guideline_version):
            out.append("guideline_version")
        return out

    def next_step(self) -> str:
        """The first step still to do, in the conversation's fixed order:
        drug -> study_type -> population -> design -> indication -> guideline ->
        summary -> generate."""
        if self.drug is None or self.status_of("drug") != FieldStatus.confirmed:
            return "drug"
        if not self.study_type or self.status_of("study_type") != FieldStatus.confirmed:
            return "study_type"
        if not self.reviewed.get("population"):
            return "population"
        if not (self.design_decisions and self.is_fresh("design_preview")) or not self.design_confirmed:
            return "design"
        if self.is_patient_biosimilar and not self.reviewed.get("indication"):
            return "indication"
        if not self.reviewed.get("guideline"):
            return "guideline"
        return "summary"

    # ------------------------------------------------------------------
    # presentation
    # ------------------------------------------------------------------
    def summary_fields(self) -> list[SpecField]:
        p = self.population
        rows: list[SpecField] = []

        def add(path: str, label: str, display: str | None, status: FieldStatus, source: FieldSource | None) -> None:
            rows.append(SpecField(path=path, label=label, display_value=display, status=status, source=source))

        def one(path: str, label: str, display: str | None) -> None:
            m = self.field_meta.get(path)
            status = m.status if m else (FieldStatus.empty if display is None else FieldStatus.default)
            add(path, label, display, status, m.source if m else None)

        one("drug", "Drug", self.drug.inn if self.drug else None)
        one("study_type", "Study type", STUDY_TYPE_LABELS.get(self.study_type or ""))
        one(
            "guideline_version", "Guideline",
            guideline_display_name(self.guideline_version, self.study_type or 'BE') if is_supported(self.guideline_version) else self.guideline_version,
        )
        one("population.subject_type", "Participants", "Patients" if p.subject_type == "patient" else "Healthy volunteers")
        st, src = self.group_meta("population.age_min", "population.age_max")
        add("population.age", "Age", f"{p.age_min}–{p.age_max} years", st if st != FieldStatus.empty else FieldStatus.default, src)
        one("population.sex", "Sex", {"both": "Male and female"}.get(p.sex, p.sex.capitalize()))
        st, src = self.group_meta("population.weight_min", "population.weight_max")
        add("population.weight", "Weight", f"{p.weight_min}–{p.weight_max} kg", st if st != FieldStatus.empty else FieldStatus.default, src)
        one("population.exclusion_presets", "Exclusion criteria", f"{len(p.exclusion_presets)} selected")
        if p.cv_intra is not None:
            one("population.cv_intra", "Within-subject CV", f"{p.cv_intra:g}% (investigator override)")
        else:
            add("population.cv_intra", "Within-subject CV", "From the literature (engine)", FieldStatus.engine, FieldSource.engine)
        if p.is_nti is not None:
            one("population.is_nti", "Narrow therapeutic index", "Yes" if p.is_nti else "No")
        if p.dose_strength:
            one("population.dose_strength", "Dose strength", p.dose_strength)
        if p.reference_product:
            one("population.reference_product", "Reference product", p.reference_product)
        if p.additional_pk_timepoints_hours:
            one("population.additional_pk_timepoints_hours", "Extra PK time points (h)", p.additional_pk_timepoints_hours)
        if p.additional_immunogenicity_timepoints_months:
            one("population.additional_immunogenicity_timepoints_months", "Extra ADA time points (months)", p.additional_immunogenicity_timepoints_months)
        if self.design_decisions:
            label = f"{len(self.design_decisions)} decisions, " + ("confirmed" if self.design_confirmed else "not yet confirmed")
            status = FieldStatus.confirmed if self.design_confirmed else FieldStatus.engine
            add("design_decisions", "Study design", label, status, FieldSource.user_click if self.design_confirmed else FieldSource.engine)
        else:
            add("design_decisions", "Study design", None, FieldStatus.empty, None)
        if self.is_patient_biosimilar:
            if self.user_indication:
                one("user_indication", "Indication", self.user_indication)
            else:
                add("user_indication", "Indication", "Engine chooses", FieldStatus.engine, FieldSource.engine)
        return rows

    def display_label(self, field: str) -> str:
        return {
            "drug_id": "Drug", "study_type": "Study type", "guideline_version": "Guideline",
        }.get(field, field.replace("_", " ").capitalize())

    # ------------------------------------------------------------------
    # engine payload
    # ------------------------------------------------------------------
    def population_config(self) -> dict[str, Any]:
        """The ``population_config`` the wizard would send: every always-present
        key, plus the optional ones that are set. ``user_indication`` is included
        only for a patient biosimilar study (the server drops it otherwise)."""
        p = self.population
        cfg: dict[str, Any] = {
            "subject_type": p.subject_type,
            "age_min": p.age_min,
            "age_max": p.age_max,
            "sex": p.sex,
            "weight_min": p.weight_min,
            "weight_max": p.weight_max,
            "exclusion_presets": list(p.exclusion_presets),
        }
        for key in (
            "cv_intra", "dose_strength", "reference_product", "is_nti",
            "additional_pk_timepoints_hours", "additional_immunogenicity_timepoints_months",
        ):
            value = getattr(p, key)
            if value is not None:
                cfg[key] = value
        if self.user_indication and self.is_patient_biosimilar:
            cfg["user_indication"] = self.user_indication
        return cfg

    def to_protocol_create(self) -> dict[str, Any]:
        """The ``POST /api/protocols/`` body, validated by ``schemas.ProtocolCreate``
        (so the existing indication sanitiser still runs). Raises ``ValueError``
        listing every problem when the spec is not ready or the schema rejects it."""
        problems = self.missing()
        if problems:
            raise ValueError("The draft is not ready: " + ", ".join(problems) + " still needed.")
        from schemas import ProtocolCreate  # lazy: schemas -> models -> database

        payload = {
            "drug_id": self.drug.id,  # type: ignore[union-attr]
            "study_type": self.study_type,
            "guideline_version": self.guideline_version,
            "population_config": self.population_config(),
            "design_decisions": [dict(d) for d in self.design_decisions],
        }
        try:
            validated = ProtocolCreate.model_validate(payload)
        except ValidationError as exc:
            raise ValueError(_format_validation(exc)) from None
        return validated.model_dump(mode="json")

    def provenance(self, spec_version: int | None = None, session_id: str | None = None) -> dict[str, Any]:
        """The ``provenance`` handed to ``start_protocol_generation``: written
        into the INITIAL snapshot's change_detail so a Part 11 reviewer can see
        how each input was set."""
        return {
            "source": "chat",
            "chat_session_id": session_id,
            "spec_version": self.version if spec_version is None else spec_version,
            "field_provenance": self.field_provenance(),
        }


def _format_validation(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in err['loc']) or 'payload'}: {err['msg']}"
        for err in exc.errors(include_url=False, include_input=False)
    )


# ---------------------------------------------------------------------------
# Budget draft
# ---------------------------------------------------------------------------


class BudgetDraftSpec(_SpecBase):
    """The inputs of ``POST /api/budgets`` (``BudgetCreate``), collected by chat."""

    schema_id: str = Field(default=SPEC_SCHEMA_BUDGET, alias="schema")
    kind: SessionKind = SessionKind.budget

    drug_inn: str | None = None
    study_type: str | None = None
    #: "healthy" | "patient"; None = auto-detect (the UI's default).
    subject_profile: str | None = None
    indication: str | None = None
    therapeutic_area: str | None = None
    n_sites_selected: int | None = None
    n_countries: int | None = None
    n_enrolled: int | None = None
    geography_profile: str | None = None
    currency: str = "USD"
    name: str | None = None
    sponsor_name: str | None = None
    cro_name: str | None = None
    #: Linked protocol (optional accuracy boost): {id, drug_inn, study_type, status, version}.
    protocol: dict[str, Any] | None = None
    #: The resolved profile from the last preview (cached with its input hash).
    field_meta: dict[str, FieldMeta] = Field(
        default_factory=lambda: {
            "currency": FieldMeta(status=FieldStatus.default, source=FieldSource.default, at=_now())
        }
    )

    # -- derived cache -----------------------------------------------------
    def current_hash(self, name: str) -> str | None:
        if name != "budget_preview" or not self.drug_inn:
            return None
        return input_hash({"drug_inn": self.drug_inn})

    def is_fresh(self, name: str) -> bool:
        entry = self.derived.get(name)
        return bool(entry and entry.input_hash == self.current_hash(name))

    def set_derived(self, name: str, output: Any) -> None:
        h = self.current_hash(name)
        if h is not None:
            self.derived[name] = DerivedEntry(input_hash=h, output=output, at=_now())

    def derived_output(self, name: str) -> Any:
        return self.derived[name].output if self.is_fresh(name) else None

    def sync_derived(self) -> None:
        for name in list(self.derived):
            if not self.is_fresh(name):
                del self.derived[name]

    # -- resolved subject profile ----------------------------------------
    def resolved_subject_profile(self) -> str | None:
        """The profile the budget will actually use: the explicit choice, else
        the engine's auto-detected one from the cached preview."""
        if self.subject_profile:
            return self.subject_profile
        preview = self.derived_output("budget_preview")
        if isinstance(preview, dict):
            return str(preview.get("subject_profile") or "") or None
        return None

    @property
    def is_patient(self) -> bool:
        return self.resolved_subject_profile() == "patient"

    # -- setters -----------------------------------------------------------
    def set_field(
        self,
        field: str,
        raw: Any,
        *,
        actor: Actor,
        quote: str | None = None,
        user_text: str | None = None,
    ) -> SetResult:
        if field not in BUDGET_DRAFT_FIELDS:
            return SetResult(False, field, f"'{field}' is not a budget field you can set. Allowed: {', '.join(BUDGET_DRAFT_FIELDS)}.")
        try:
            parsed, supports = self._parse(field, raw)
        except FieldError as exc:
            return SetResult(False, field, str(exc))
        if field == "study_type" and parsed is not None and self.drug_inn:
            gate = study_type_gate_message(self.drug_inn, parsed)
            if gate is not None:
                return SetResult(False, field, gate)
        if field == "drug_inn" and parsed is not None and self.study_type:
            gate = study_type_gate_message(parsed, self.study_type)
            if gate is not None:
                return SetResult(
                    False, field,
                    f"{gate} Clear or change the study type first, or pick a different drug.",
                )
        status, source, qt = _provenance(actor, quote, user_text, supports=supports)
        before = getattr(self, field)
        setattr(self, field, parsed)
        if parsed is None:
            self.clear_meta(field)
        else:
            self.stamp(field, status, source, qt)
        note = None
        if field in ("indication", "therapeutic_area", "n_sites_selected", "n_countries", "n_enrolled", "geography_profile"):
            note = "Patient-study setting: only used when the budget is for patients."
        self.sync_derived()
        self.touch()
        return SetResult(True, field, status=self.status_of(field) if parsed is not None else FieldStatus.empty,
                         changed=before != parsed, note=note)

    def _parse(self, field: str, raw: Any) -> tuple[Any, Callable[[str], bool] | None]:
        blank = raw is None or _as_text(raw) == ""
        if field == "drug_inn":
            if blank:
                return None, None
            text = safety.clean_free_text(_as_text(raw), 64).lower()
            if not _BUDGET_DRUG_RE.match(text):
                raise FieldError("drug_inn must be 2-64 characters: letters, numbers, spaces or hyphens, starting with a letter.")
            return text, lambda q, t=text: t in q
        if field == "study_type":
            if blank:
                return None, None
            return parse_choice(raw, field, STUDY_TYPES, case_insensitive=False), None
        if field == "subject_profile":
            if blank or _as_text(raw).lower() == "auto":
                return None, None
            return parse_choice(raw, field, BUDGET_SUBJECT_PROFILES), None
        if field == "therapeutic_area":
            if blank:
                return None, None
            return parse_choice(raw, field, BUDGET_THERAPEUTIC_AREAS), None
        if field == "indication":
            if blank:
                return None, None
            clean = sanitize_user_indication(_as_text(raw))
            if clean is None:
                raise FieldError("That indication contained nothing usable after sanitising.")
            return clean, lambda q, t=_norm(clean): t in q
        if field in ("n_sites_selected", "n_countries", "n_enrolled"):
            if blank:
                return None, None
            hi = {"n_sites_selected": MAX_BUDGET_SITES, "n_countries": MAX_BUDGET_COUNTRIES, "n_enrolled": MAX_BUDGET_ENROLLED}[field]
            n = parse_int(raw, field, 0, hi)
            wanted = _numbers(str(n))
            return n, lambda q, w=wanted: w <= _numbers(q)
        if field == "geography_profile":
            if blank:
                return None, None
            from services.budget_rate_card import GEOGRAPHY_FACTORS  # lazy

            return parse_choice(raw, field, tuple(GEOGRAPHY_FACTORS), case_insensitive=False), None
        if field == "currency":
            if blank:
                raise FieldError(f"currency must be one of: {', '.join(BUDGET_CURRENCIES)}.")
            return parse_choice(raw, field, BUDGET_CURRENCIES), None
        if field in ("name", "sponsor_name", "cro_name"):
            if blank:
                return None, None
            text = parse_free_text(raw, field, 120)
            return text, (lambda q, t=_norm(text): t in q) if text else None
        raise FieldError(f"'{field}' is not a budget field.")

    def set_protocol(self, protocol: dict[str, Any] | None, *, actor: Actor) -> SetResult:
        """Link (or unlink with None) a protocol. ``protocol`` is the org-scoped
        row ``{id, drug_inn, study_type, status, version}`` the caller resolved."""
        if protocol is None:
            had = self.protocol is not None
            self.protocol = None
            self.clear_meta("protocol_id")
            self.touch()
            return SetResult(True, "protocol_id", status=FieldStatus.empty, changed=had)
        status, source, qt = _provenance(actor, None, None)
        self.protocol = {k: protocol.get(k) for k in ("id", "drug_inn", "study_type", "status", "version", "created_at")}
        self.stamp("protocol_id", status, source, qt)
        # A linked protocol supplies the drug when none was chosen (the server does the same).
        if not self.drug_inn and protocol.get("drug_inn"):
            self.drug_inn = str(protocol["drug_inn"]).lower()
            self.stamp("drug_inn", FieldStatus.engine, FieldSource.engine)
        self.sync_derived()
        self.touch()
        return SetResult(True, "protocol_id", status=status, changed=True)

    def apply_changes(self, changes: list[dict[str, Any]], *, user_text: str | None) -> list[SetResult]:
        return [
            self.set_field(
                str(c.get("field")), c.get("value"), actor=Actor.model,
                quote=c.get("user_quote"), user_text=user_text,
            )
            for c in changes
        ]

    def apply_patch(self, patch: dict[str, Any], *, actor: Actor) -> list[SetResult]:
        return [self.set_field(k, v, actor=actor) for k, v in patch.items()]

    # -- readiness ----------------------------------------------------------
    def missing(self) -> list[str]:
        out: list[str] = []
        if not self.drug_inn and not self.protocol:
            out.append("drug_inn")
        return out

    def next_step(self) -> str:
        if not self.drug_inn and not self.protocol:
            return "drug"
        if not self.is_fresh("budget_preview"):
            return "preview"
        return "summary"

    def effective_drug(self) -> str | None:
        return self.drug_inn or (str(self.protocol["drug_inn"]) if self.protocol and self.protocol.get("drug_inn") else None)

    # -- presentation ---------------------------------------------------------
    def summary_fields(self) -> list[SpecField]:
        rows: list[SpecField] = []

        def row(path: str, label: str, display: str | None, *, default_status: FieldStatus = FieldStatus.default) -> None:
            m = self.field_meta.get(path)
            if m:
                status, source = m.status, m.source
            else:
                status, source = (FieldStatus.empty, None) if display is None else (default_status, FieldSource.default)
            rows.append(SpecField(path=path, label=label, display_value=display, status=status, source=source))

        row("drug_inn", "Drug", self.effective_drug())
        row("study_type", "Study type", STUDY_TYPE_LABELS.get(self.study_type or "") if self.study_type else "Engine default")
        profile = self.resolved_subject_profile()
        row(
            "subject_profile", "Participants",
            {"healthy": "Healthy volunteers", "patient": "Patients"}.get(self.subject_profile or "", None)
            or (f"Auto-detected: {profile}" if profile else "Auto-detect"),
        )
        if self.is_patient:
            if self.indication:
                row("indication", "Indication", self.indication)
            if self.therapeutic_area:
                row("therapeutic_area", "Therapeutic area", self.therapeutic_area)
            if self.n_sites_selected is not None:
                row("n_sites_selected", "Sites", str(self.n_sites_selected))
            if self.n_countries is not None:
                row("n_countries", "Countries", str(self.n_countries))
            if self.n_enrolled is not None:
                row("n_enrolled", "Patients enrolled", str(self.n_enrolled))
            if self.geography_profile:
                row("geography_profile", "Geography", self.geography_profile)
        row("currency", "Currency", self.currency)
        if self.name:
            row("name", "Name", self.name)
        if self.sponsor_name:
            row("sponsor_name", "Sponsor", self.sponsor_name)
        if self.cro_name:
            row("cro_name", "CRO", self.cro_name)
        row("protocol_id", "Linked protocol", (f"{self.protocol['drug_inn']} ({self.protocol['study_type']})" if self.protocol else None))
        return rows

    # -- engine payload ---------------------------------------------------------
    def to_budget_create(self) -> dict[str, Any]:
        """The ``POST /api/budgets`` body, validated by ``schemas.BudgetCreate``.
        Patient-only settings are included when the budget is for patients
        (explicit choice, or the engine's auto-detected profile) -- the same
        rule the wizard form applied."""
        problems = self.missing()
        if problems:
            raise ValueError("The draft is not ready: a drug name (or a linked protocol) is still needed.")
        from schemas import BudgetCreate  # lazy

        payload: dict[str, Any] = {"currency": self.currency}
        if self.name:
            payload["name"] = self.name
        if self.drug_inn:
            payload["drug_inn"] = self.drug_inn
        if self.study_type:
            payload["study_type"] = self.study_type
        if self.protocol:
            payload["protocol_id"] = self.protocol["id"]
        if self.sponsor_name:
            payload["sponsor_name"] = self.sponsor_name
        if self.cro_name:
            payload["cro_name"] = self.cro_name
        if self.subject_profile:
            payload["subject_profile"] = self.subject_profile
        if self.is_patient:
            for key in ("indication", "therapeutic_area", "n_sites_selected", "n_countries", "n_enrolled", "geography_profile"):
                value = getattr(self, key)
                if value is not None:
                    payload[key] = value
        try:
            validated = BudgetCreate.model_validate(payload)
        except ValidationError as exc:
            raise ValueError(_format_validation(exc)) from None
        return validated.model_dump(mode="json", exclude_none=True)

    def provenance(self, session_id: str | None = None) -> dict[str, Any]:
        return {
            "source": "chat",
            "chat_session_id": session_id,
            "spec_version": self.version,
            "field_provenance": self.field_provenance(),
        }


# ---------------------------------------------------------------------------
# Edit session
# ---------------------------------------------------------------------------


class PendingEdit(BaseModel):
    """A change request the user has seen on an ``edit_impact`` card and not yet
    confirmed. ``proposal_id`` is pre-minted; the ``confirm_edit`` action creates
    the proposal row with exactly this id."""

    model_config = ConfigDict(extra="ignore")

    proposal_id: str
    kind: Literal["engine", "prose"]
    #: engine: {field: value} (values already parsed). prose: {}.
    changes: dict[str, Any] = Field(default_factory=dict)
    #: prose: section names to rewrite.
    sections: list[str] = Field(default_factory=list)
    #: prose: the editing guidance (cleaned; labelled non-authoritative downstream).
    instruction: str | None = None
    quote: str | None = None
    base_version: int
    at: str = Field(default_factory=_now)

    def request_payload(self) -> dict[str, Any]:
        """The ``protocol_edit_proposals.request`` JSON."""
        if self.kind == "engine":
            return {"changes": self.changes, "user_quote": self.quote}
        return {"sections": self.sections, "instruction": self.instruction, "user_quote": self.quote}


class EditSessionSpec(_SpecBase):
    """State of a protocol-edit chat: which protocol, what it looked like when
    the overview was read, and the one pending change request (if any)."""

    schema_id: str = Field(default=SPEC_SCHEMA_EDIT, alias="schema")
    kind: SessionKind = SessionKind.protocol_edit

    protocol_id: str | None = None
    drug_inn: str | None = None
    study_type: str | None = None
    status: str | None = None
    version_seen: int | None = None
    editable: bool = False
    blocked_reason: str | None = None
    #: The protocol's current engine inputs (non-underscore config keys) and
    #: guideline, captured at overview time: the "before" side of an impact card.
    inputs: dict[str, Any] = Field(default_factory=dict)
    #: Decision key -> {value, options} from the stored ``_design_decisions``.
    decisions: dict[str, dict[str, Any]] = Field(default_factory=dict)
    design_key: str | None = None
    t_half_hours: float | None = None
    #: Display strings for engine-derived current values (CV and its source, the
    #: locked washout, NTI), shown as "before" when the stored input is blank.
    current: dict[str, str] = Field(default_factory=dict)
    pending: PendingEdit | None = None
    last_proposal_id: str | None = None

    # -- helpers -----------------------------------------------------------------
    def load_overview(self, snapshot: dict[str, Any]) -> None:
        """Record what the protocol looked like when the assistant read it."""
        self.protocol_id = str(snapshot.get("id") or self.protocol_id or "")
        self.drug_inn = snapshot.get("drug_inn")
        self.study_type = snapshot.get("study_type")
        self.status = snapshot.get("status")
        self.version_seen = int(snapshot.get("version") or 0)
        gen = snapshot.get("generation_status") or "complete"
        self.editable = self.status == "draft" and gen == "complete"
        self.blocked_reason = None
        if self.status != "draft":
            self.blocked_reason = (
                f"This protocol is '{self.status}'. Chat edits only work on draft protocols; "
                "clone it to start a new draft."
            )
        elif gen != "complete":
            self.blocked_reason = "This protocol is still generating (or failed); edits are not available."
        # Never keep a server-internal (underscore) key: the spec is persisted and
        # `_randomization` in particular is unblinded data.
        self.inputs = {k: v for k, v in (snapshot.get("inputs") or {}).items() if not str(k).startswith("_")}
        self.inputs["guideline_version"] = snapshot.get("guideline_version")
        self.decisions = {
            str(d.get("key")): {"value": d.get("value"), "options": list(d.get("options") or []), "label": d.get("label")}
            for d in (snapshot.get("decisions") or []) if isinstance(d, dict)
        }
        self.design_key = snapshot.get("design_key")
        self.t_half_hours = snapshot.get("t_half_hours")
        facts = snapshot.get("facts") or {}
        self.current = {}
        if isinstance(facts.get("cv_pct"), (int, float)):
            self.current["cv_intra"] = f"{facts['cv_pct']:g}% ({facts.get('cv_source') or 'source not recorded'})"
        if isinstance(facts.get("washout_days"), (int, float)):
            self.current["washout_days"] = f"{facts['washout_days']:g}"
        elif facts.get("washout_applicable") is False:
            self.current["washout_days"] = "none (parallel design)"
        if isinstance(facts.get("is_nti"), bool):
            self.current["is_nti"] = "Yes" if facts["is_nti"] else "No"
        self.stamp("protocol", FieldStatus.engine, FieldSource.engine)
        self.touch()

    def before_value(self, field: str) -> Any:
        if field.startswith("design_decision_"):
            return (self.decisions.get(field[len("design_decision_"):]) or {}).get("value")
        if field in ("exclusion_add", "exclusion_remove"):
            return self.inputs.get("exclusion_presets")
        value = self.inputs.get(field)
        return self.current.get(field) if value is None else value

    def validate_change(self, field: str, raw: Any) -> Any:
        """Parse one engine-input change with the same validators the draft spec
        uses; raises :class:`FieldError`."""
        if field not in EDIT_INPUT_FIELDS:
            raise FieldError(f"'{field}' cannot be edited here. Allowed: {', '.join(EDIT_INPUT_FIELDS)}.")
        if field == "guideline_version":
            version = _as_text(raw)
            if not is_supported(version):
                raise FieldError(unsupported_guideline_message(version))
            if not is_current(version):
                raise FieldError(f"'{version}' is superseded and cannot be chosen for new work.")
            return version
        if field in ("exclusion_add", "exclusion_remove"):
            text = safety.clean_free_text(_as_text(raw), MAX_EXCLUSION_TEXT)
            if not text:
                raise FieldError("The exclusion criterion was empty.")
            return text
        if field == "user_indication":
            if raw is None or _as_text(raw) == "":
                return None
            clean = sanitize_user_indication(_as_text(raw))
            if clean is None:
                raise FieldError("That indication contained nothing usable after sanitising.")
            return clean
        if field == "washout_days":
            return parse_float(raw, "washout_days", 0.5, 365)
        if field.startswith("design_decision_"):
            key = field[len("design_decision_"):]
            decision = self.decisions.get(key)
            if decision is None:
                raise FieldError(f"This protocol has no stored '{key}' design decision to change.")
            text = _as_text(raw)
            if text not in decision["options"]:
                raise FieldError(f"'{text}' is not an option for {decision.get('label') or key}. Options: {'; '.join(decision['options'])}.")
            return text
        return parse_population_value(field, raw)

    def propose_engine_change(
        self, changes: list[dict[str, Any]], *, quote: str | None, user_text: str | None,
    ) -> tuple[PendingEdit | None, list[dict[str, Any]]]:
        """Validate ``[{field, value}]`` and, if at least one change is valid and
        editing is allowed, record a :class:`PendingEdit`. Returns
        ``(pending_or_None, per_field_results)``."""
        results: list[dict[str, Any]] = []
        accepted: dict[str, Any] = {}
        if not self.editable:
            reason = self.blocked_reason or "Load the protocol overview first."
            return None, [{"field": c.get("field"), "accepted": False, "message": reason} for c in changes]
        for c in changes:
            field = str(c.get("field"))
            try:
                accepted[field] = self.validate_change(field, c.get("value"))
                results.append({"field": field, "accepted": True, "message": None})
            except FieldError as exc:
                results.append({"field": field, "accepted": False, "message": str(exc)})
        problem = self._cross_check(accepted)
        if problem:
            return None, [dict(r, accepted=False, message=problem) if r["accepted"] else r for r in results]
        if not accepted:
            return None, results
        wanted: set[str] = set()
        for value in accepted.values():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                wanted |= _numbers(_as_text(value))
        status, _source, qt = _provenance(
            Actor.model, quote, user_text, supports=(lambda q, w=wanted: w <= _numbers(q)) if wanted else None,
        )
        self.pending = PendingEdit(
            proposal_id=str(uuid.uuid4()), kind="engine", changes=accepted,
            quote=qt, base_version=int(self.version_seen or 0),
        )
        self.stamp("pending", status, FieldSource.user_text, qt)
        self.touch()
        return self.pending, results

    def _cross_check(self, accepted: dict[str, Any]) -> str | None:
        amin = accepted.get("age_min", self.inputs.get("age_min"))
        amax = accepted.get("age_max", self.inputs.get("age_max"))
        if isinstance(amin, int) and isinstance(amax, int) and amin > amax:
            return f"age_min ({amin}) cannot exceed age_max ({amax})."
        wmin = accepted.get("weight_min", self.inputs.get("weight_min"))
        wmax = accepted.get("weight_max", self.inputs.get("weight_max"))
        if isinstance(wmin, int) and isinstance(wmax, int) and wmin > wmax:
            return f"weight_min ({wmin}) cannot exceed weight_max ({wmax})."
        return None

    def propose_prose_change(
        self, sections: list[str], instruction: str, *, quote: str | None, user_text: str | None,
        valid_sections: tuple[str, ...],
    ) -> tuple[PendingEdit | None, str | None]:
        if not self.editable:
            return None, self.blocked_reason or "Load the protocol overview first."
        names = []
        for s in sections:
            if s not in valid_sections:
                return None, f"'{s}' is not a section of this protocol. Sections: {', '.join(valid_sections)}."
            if s not in names:
                names.append(s)
        if not names:
            return None, "Choose at least one section."
        text = safety.clean_free_text(instruction, 1000)
        if not text:
            return None, "The instruction was empty."
        status, _source, qt = _provenance(Actor.model, quote, user_text)
        self.pending = PendingEdit(
            proposal_id=str(uuid.uuid4()), kind="prose", sections=names, instruction=text,
            quote=qt, base_version=int(self.version_seen or 0),
        )
        self.stamp("pending", status, FieldSource.user_text, qt)
        self.touch()
        return self.pending, None

    def clear_pending(self) -> None:
        if self.pending is not None:
            self.last_proposal_id = self.pending.proposal_id
        self.pending = None
        self.clear_meta("pending")
        self.touch()

    def missing(self) -> list[str]:
        out: list[str] = []
        if not self.protocol_id:
            out.append("protocol")
        elif not self.editable:
            out.append("editable")
        if self.pending is None:
            out.append("pending")
        return out

    def ready(self) -> bool:
        return bool(self.protocol_id and self.editable and self.pending is not None)

    def next_step(self) -> str:
        if not self.protocol_id or self.version_seen is None:
            return "overview"
        if not self.editable:
            return "blocked"
        if self.pending is None:
            return "request"
        return "confirm"

    def summary_fields(self) -> list[SpecField]:
        def row(path: str, label: str, display: str | None) -> SpecField:
            m = self.field_meta.get(path)
            return SpecField(
                path=path, label=label, display_value=display,
                status=m.status if m else (FieldStatus.empty if display is None else FieldStatus.default),
                source=m.source if m else None,
            )

        rows = [
            row("protocol", "Protocol", f"{self.drug_inn} ({STUDY_TYPE_LABELS.get(self.study_type or '', self.study_type)}) v{self.version_seen}" if self.protocol_id and self.drug_inn else None),
            row("editable", "Editable", ("Yes" if self.editable else (self.blocked_reason or "No")) if self.protocol_id else None),
        ]
        if self.pending is None:
            rows.append(row("pending", "Requested change", None))
        elif self.pending.kind == "engine":
            rows.append(row("pending", "Requested change", ", ".join(self.pending.changes)))
        else:
            rows.append(row("pending", "Requested change", f"Rewrite: {', '.join(self.pending.sections)}"))
        return rows


# ---------------------------------------------------------------------------
# Factory / (de)serialisation
# ---------------------------------------------------------------------------

DraftSpec = ProtocolDraftSpec | BudgetDraftSpec | EditSessionSpec

_SPEC_CLASSES: dict[str, type[_SpecBase]] = {
    SPEC_SCHEMA_PROTOCOL: ProtocolDraftSpec,
    SPEC_SCHEMA_BUDGET: BudgetDraftSpec,
    SPEC_SCHEMA_EDIT: EditSessionSpec,
}


def new_spec(kind: SessionKind | str, **init: Any) -> DraftSpec:
    """A fresh spec for a new session of ``kind`` (``init`` seeds e.g.
    ``protocol_id`` for an edit session)."""
    kind = SessionKind(kind)
    if kind == SessionKind.protocol:
        return ProtocolDraftSpec(**init)
    if kind == SessionKind.budget:
        return BudgetDraftSpec(**init)
    return EditSessionSpec(**init)


def load_spec(kind: SessionKind | str, data: dict[str, Any] | None) -> DraftSpec:
    """Rebuild a spec from ``chat_sessions.spec``. ``{}`` / None (a brand-new
    session's JSONB default) yields a fresh spec; a stored ``schema`` that does
    not match the session kind is an error (a corrupted row must not be
    reinterpreted as another kind's draft)."""
    kind = SessionKind(kind)
    if not data:
        return new_spec(kind)
    schema = data.get("schema")
    cls = _SPEC_CLASSES.get(str(schema))
    expected = {
        SessionKind.protocol: ProtocolDraftSpec, SessionKind.budget: BudgetDraftSpec,
        SessionKind.protocol_edit: EditSessionSpec,
    }[kind]
    if cls is None:
        cls = expected  # an unversioned legacy row: read it as the kind's own class
    if cls is not expected:
        raise ValueError(f"Stored spec schema {schema!r} does not match session kind {kind.value!r}.")
    return cls.from_json(data)


def dump_spec(spec: DraftSpec) -> dict[str, Any]:
    return spec.to_json()
