"""Protocol edit proposals (plan B9): the background jobs that PREPARE a change,
and the apply / discard / read side. Nothing here touches a protocol until
:func:`apply_proposal`, which is reached only through a user's click.

Two kinds of proposal (``protocol_edit_proposals.kind``):

* ``prose``  - for each target section, ``AIService.generate_protocol_section`` is
  called with ``revision_request`` (the user's editing guidance, labelled as
  non-authoritative by the prompt layer) and ``current_section_text`` (the draft
  being edited, labelled as data). Engine-computed facts stay locked because the
  section is rebuilt from the protocol's own stored context.
* ``engine`` - the protocol's INPUT config (its non-underscore keys) with the
  requested changes merged in, plus its stored ``_design_decisions`` with any
  decision edits, are fed back through ``routers.protocols._run_generation_pipeline``
  for the same protocol id (so the randomization seed is unchanged). The result is
  the candidate sections AND the candidate augmented config.

A job writes ``proposed_sections`` / ``proposed_config`` / ``change_detail`` and
flips ``generating -> ready`` (or ``failed``). Under pytest the job runs inline
(``routers.protocols._generate_synchronously``), exactly like generation; in
production it is an asyncio task with a strong reference and its OWN
``database.AsyncSessionLocal()`` sessions. A proposal stuck in ``generating`` for
more than ``PROPOSAL_STUCK_MINUTES`` is marked failed when read (in-process jobs
die on a deploy).

APPLY succeeds only if the protocol is still at ``base_version`` and still a
draft; otherwise the proposal becomes ``stale`` and the caller answers 409
``proposal_stale``. A successful apply writes sections (+ config, + guideline),
bumps the version, adds a ``CHAT_EDIT`` version snapshot and a ``CHAT_EDIT:protocol``
audit row, atomically.

Unblinded randomization data never leaves this module: only a whitelist of
engine facts is ever copied into a view, and ``proposed_config`` (which holds the
new allocation) is stored but never serialised into a response.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import inspect as sa_inspect, select, update
from sqlalchemy.orm import selectinload

import database
from models import (
    AuditLog,
    EditProposalKind,
    EditProposalStatus,
    PKParameterRecord,
    Protocol,
    ProtocolEditProposal,
    ProtocolStatus,
    ProtocolVersion,
)
from services.chat import contract as c
from services.chat.cards import locked_fact_rows
from services.chat.facades import _SAMPLE_SIZE_FACT_KEYS

logger = logging.getLogger(__name__)


def _log_failure(what: str, exc: BaseException) -> None:
    """Class name at error level, traceback only at DEBUG: stack-frame locals hold
    the user's request and section text, which must not reach Sentry (B7)."""
    logger.error("%s: %s", what, type(exc).__name__)
    logger.debug("%s (traceback)", what, exc_info=True)

#: Strong references to in-flight proposal jobs (asyncio keeps only weak ones).
_JOB_TASKS: set[asyncio.Task] = set()

STUCK_MESSAGE = "The edit took too long and was stopped (it may have been interrupted by a restart). Please try again."
GENERIC_FAILURE = "The edit could not be prepared. Please try again."


class ProposalError(Exception):
    """A proposal operation the router must refuse. ``code`` is a
    :class:`~services.chat.contract.ChatErrorCode` value; ``status_code`` the HTTP
    status to send (taken from ``contract.ERROR_HTTP_STATUS``)."""

    def __init__(self, code: c.ChatErrorCode, message: str):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = c.ERROR_HTTP_STATUS[code]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _plain(value: Any) -> Any:
    """Round-trip through JSON so the value is JSONB-safe (UUID / Enum -> str)."""
    return json.loads(json.dumps(value, default=str))


def _detail_text(detail: Any) -> str:
    if isinstance(detail, dict):
        return str(detail.get("message") or detail)
    return str(detail)


# ---------------------------------------------------------------------------
# Merging an engine edit into the protocol's input config (PURE)
# ---------------------------------------------------------------------------

POP_HEALTHY = "Healthy volunteers"
POP_PATIENTS = "Patients"


@dataclass
class MergedEngineEdit:
    """The inputs of the re-run: ``population_config`` (input keys only),
    ``design_decisions`` (stored, with edits), the ``guideline_version`` to run
    under, and plain-language ``notes`` about anything dropped or aligned."""

    population_config: dict[str, Any]
    design_decisions: list[dict[str, Any]]
    guideline_version: str
    notes: list[str] = field(default_factory=list)
    flips_to_parallel: bool = False
    flips_to_crossover: bool = False


def merge_engine_changes(
    stored_config: dict[str, Any] | None,
    changes: dict[str, Any],
    *,
    guideline_version: str,
) -> MergedEngineEdit:
    """Merge ``changes`` (``{field: parsed value}`` from ``PendingEdit.changes``)
    into a protocol's stored config.

    * Only non-underscore keys are carried (server-computed engine outputs are
      recomputed by the re-run; ``_randomization`` in particular never travels).
      The one exception is the integer ``_randomization_seed``: the filed
      allocation list must stay reproducible, so the stored seed is reused.
    * ``exclusion_add`` / ``exclusion_remove`` edit ``exclusion_presets``.
    * ``design_decision_<key>`` edits the stored decision of that key.
    * A ``subject_type`` edit also aligns the stored Population decision, because
      the re-run folds the decisions back over the config (an unaligned decision
      would silently undo the edit).
    * When the design flips to parallel, ``washout_days`` is dropped (a parallel
      design has no washout; keeping it would fail the re-run).
    * ``guideline_version`` is not a config key; it is returned separately.
    """
    stored = stored_config or {}
    cfg: dict[str, Any] = {k: copy.deepcopy(v) for k, v in stored.items() if not str(k).startswith("_")}
    seed = stored.get("_randomization_seed")
    if isinstance(seed, int) and not isinstance(seed, bool) and seed > 0:
        cfg["_randomization_seed"] = seed
    raw_decisions = stored.get("_design_decisions")
    decisions: list[dict[str, Any]] = [
        copy.deepcopy(d) for d in (raw_decisions if isinstance(raw_decisions, list) else []) if isinstance(d, dict)
    ]
    out = MergedEngineEdit(population_config=cfg, design_decisions=decisions, guideline_version=guideline_version)

    def decision(key: str) -> dict[str, Any] | None:
        return next((d for d in decisions if d.get("key") == key), None)

    for fld, value in changes.items():
        if fld == "guideline_version":
            out.guideline_version = str(value)
        elif fld == "exclusion_add":
            items = [str(x) for x in (cfg.get("exclusion_presets") or [])]
            if str(value).strip().lower() not in {x.strip().lower() for x in items}:
                items.append(str(value))
            cfg["exclusion_presets"] = items
        elif fld == "exclusion_remove":
            items = [str(x) for x in (cfg.get("exclusion_presets") or [])]
            cfg["exclusion_presets"] = [x for x in items if x.strip().lower() != str(value).strip().lower()]
        elif fld == "washout_days":
            cfg["washout_days"] = float(value)
        elif fld.startswith("design_decision_"):
            key = fld[len("design_decision_"):]
            target = decision(key)
            if target is None:
                raise ValueError(f"This protocol has no stored '{key}' design decision to change.")
            target["value"] = str(value)
            if key == "design":
                low = str(value).lower()
                out.flips_to_parallel = "parallel" in low
                out.flips_to_crossover = "crossover" in low or "replicate" in low
        elif value is None:
            cfg.pop(fld, None)
        else:
            cfg[fld] = value

    if "subject_type" in changes:
        pop = decision("population")
        if pop is not None:
            current = str(pop.get("value") or "")
            if changes["subject_type"] == "patient" and not current.startswith("Patients"):
                pop["value"] = POP_PATIENTS
                out.notes.append("The Population design decision was aligned to Patients.")
            elif changes["subject_type"] == "healthy" and current.startswith("Patients"):
                pop["value"] = POP_HEALTHY
                out.notes.append("The Population design decision was aligned to Healthy volunteers.")

    if out.flips_to_parallel and cfg.pop("washout_days", None) is not None:
        out.notes.append("The washout was dropped because a parallel design has no washout period.")
    return out


def stored_design(population_config: dict[str, Any] | None) -> tuple[str | None, float | None]:
    """``(design_key, t_half_hours)`` from a protocol's stored ``_sample_size``."""
    cfg = population_config or {}
    ss = cfg.get("_sample_size") if isinstance(cfg.get("_sample_size"), dict) else {}
    t_half = ss.get("t_half_hours")
    return ss.get("design_key"), (float(t_half) if isinstance(t_half, (int, float)) and not isinstance(t_half, bool) else None)


def validate_engine_request(protocol: Protocol, request: dict[str, Any]) -> None:
    """Re-check an engine edit at CONFIRM time, before any credits are spent.

    The washout override is validated by ``biostatistics.validate_washout_override``
    against the protocol's stored design and half-life (``_sample_size``), after
    accounting for a design change in the same request. Raises
    :class:`ProposalError` ``validation`` with the engine's own message."""
    changes = request.get("changes") or {}
    if "washout_days" not in changes:
        return
    from services.biostatistics import validate_washout_override

    design_key, t_half = stored_design(protocol.population_config)
    try:
        merged = merge_engine_changes(
            protocol.population_config, {k: v for k, v in changes.items() if k != "washout_days"},
            guideline_version=protocol.guideline_version,
        )
    except ValueError as exc:
        raise ProposalError(c.ChatErrorCode.validation, str(exc)) from None
    if merged.flips_to_parallel:
        design_key = "parallel"
    elif merged.flips_to_crossover and design_key == "parallel":
        design_key = "crossover"
    ok, _minimum, message = validate_washout_override(design_key, t_half, changes["washout_days"])
    if not ok:
        raise ProposalError(c.ChatErrorCode.validation, message)


# ---------------------------------------------------------------------------
# Facts (a whitelist of engine outputs; never `_randomization`)
# ---------------------------------------------------------------------------


def fact_snapshot(population_config: dict[str, Any] | None, guideline_version: str, study_type: str) -> dict[str, Any]:
    """The shape ``cards.locked_fact_rows`` reads, copied from a stored config by
    an explicit whitelist (same fields ``RealFacades.get_protocol`` exposes)."""
    cfg = population_config or {}
    ss = cfg.get("_sample_size") if isinstance(cfg.get("_sample_size"), dict) else {}
    facts = {k: ss.get(k) for k in _SAMPLE_SIZE_FACT_KEYS if ss.get(k) is not None}
    ddi = cfg.get("_ddi") if isinstance(cfg.get("_ddi"), dict) else {}
    for status in ("prohibited", "restricted", "permitted"):
        rows = ddi.get(status)
        if isinstance(rows, list):
            facts[f"ddi_{status}_rows"] = len(rows)
    decisions = cfg.get("_design_decisions") if isinstance(cfg.get("_design_decisions"), list) else []
    return {
        "facts": facts,
        "decisions": [
            {k: d.get(k) for k in ("key", "label", "value")} for d in decisions if isinstance(d, dict)
        ],
        "guideline_version": guideline_version,
        "study_type": study_type,
    }


def fact_rows(population_config: dict[str, Any] | None, guideline_version: str, study_type: str) -> list[dict[str, str]]:
    return [r.model_dump() for r in locked_fact_rows(fact_snapshot(population_config, guideline_version, study_type))]


# ---------------------------------------------------------------------------
# Creating / starting a proposal
# ---------------------------------------------------------------------------


async def create_proposal(
    db: Any, *, proposal_id: str, protocol: Protocol, session_id: str, actor_id: Any, kind: str,
    request: dict[str, Any], base_version: int,
) -> ProtocolEditProposal:
    """Insert the proposal row (the id was pre-minted on the impact card) already
    in ``generating`` and commit. A duplicate id raises ``IntegrityError``."""
    row = ProtocolEditProposal(
        id=uuid.UUID(str(proposal_id)), protocol_id=protocol.id, org_id=protocol.org_id,
        session_id=uuid.UUID(str(session_id)), created_by=actor_id if isinstance(actor_id, uuid.UUID) else uuid.UUID(str(actor_id)),
        kind=EditProposalKind(kind), status=EditProposalStatus.generating, base_version=int(base_version),
        request=_plain(request), started_at=_now(),
    )
    db.add(row)
    await db.commit()
    return row


async def start_job(proposal_id: str) -> None:
    """Run the proposal's job: inline under pytest (like generation), otherwise as
    a background task with a strong reference."""
    from routers.protocols import _generate_synchronously

    if _generate_synchronously():
        await run_proposal_job(proposal_id)
        return
    task = asyncio.create_task(run_proposal_job(proposal_id))
    _JOB_TASKS.add(task)
    task.add_done_callback(_JOB_TASKS.discard)


# ---------------------------------------------------------------------------
# The job
# ---------------------------------------------------------------------------


async def _fail(proposal_id: uuid.UUID, message: str) -> None:
    """generating -> failed (only if still generating), in its own session."""
    try:
        async with database.AsyncSessionLocal() as session:
            await session.execute(
                update(ProtocolEditProposal)
                .where(ProtocolEditProposal.id == proposal_id, ProtocolEditProposal.status == EditProposalStatus.generating)
                .values(status=EditProposalStatus.failed, error=(message or GENERIC_FAILURE)[:2000])
            )
            await session.commit()
    except Exception as exc:  # noqa: BLE001
        _log_failure(f"could not mark proposal {proposal_id} failed", exc)


async def run_proposal_job(proposal_id: str) -> None:
    """Prepare one proposal. Never raises: any failure becomes ``status=failed``
    with a human-readable ``error``."""
    pid = uuid.UUID(str(proposal_id))
    try:
        async with database.AsyncSessionLocal() as session:
            proposal = await session.get(ProtocolEditProposal, pid)
            if proposal is None or proposal.status != EditProposalStatus.generating:
                return  # discarded / expired / already done
            result = await session.execute(
                select(Protocol).options(selectinload(Protocol.drug)).where(Protocol.id == proposal.protocol_id)
            )
            protocol = result.scalar_one_or_none()
            if protocol is None:
                raise HTTPException(404, detail="Protocol not found.")
            request = dict(proposal.request or {})
            if proposal.kind == EditProposalKind.prose:
                merged_sections, proposed_config, new_guideline, notes, pipeline = await _prepare_prose(session, protocol, request)
            else:
                merged_sections, proposed_config, new_guideline, notes, pipeline = await _prepare_engine(session, protocol, request)
            detail = _build_detail(
                protocol, merged_sections, proposed_config, new_guideline, notes, proposal.kind.value, pipeline,
            )
            res = await session.execute(
                update(ProtocolEditProposal)
                .where(ProtocolEditProposal.id == pid, ProtocolEditProposal.status == EditProposalStatus.generating)
                .values(
                    status=EditProposalStatus.ready, proposed_sections=_plain(merged_sections),
                    proposed_config=_plain(proposed_config) if proposed_config is not None else None,
                    change_detail=_plain(detail), error=None,
                )
            )
            await session.commit()
            if (res.rowcount or 0) != 1:
                logger.info("proposal %s was no longer generating when its job finished; result dropped", pid)
    except HTTPException as exc:
        await _fail(pid, _detail_text(exc.detail))
    except Exception as exc:  # noqa: BLE001
        _log_failure(f"edit proposal job {pid} failed", exc)
        await _fail(pid, GENERIC_FAILURE)


async def _prepare_prose(
    session: Any, protocol: Protocol, request: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None, str | None, list[str], dict[str, Any] | None]:
    from routers.protocols import SECTION_ORDER, _build_context
    from services.ai_service import AIService

    names = [str(s) for s in (request.get("sections") or [])]
    instruction = str(request.get("instruction") or "").strip() or None
    bad = [n for n in names if n not in SECTION_ORDER]
    if bad or not names:
        raise HTTPException(422, detail=f"Unknown section(s): {', '.join(bad) or '(none given)'}.")
    pk_params = (
        await session.execute(select(PKParameterRecord).where(PKParameterRecord.drug_id == protocol.drug_id))
    ).scalars().all()
    context = _build_context(
        drug=protocol.drug, study_type=protocol.study_type.value,
        population_config=protocol.population_config or {}, org_id=protocol.org_id,
        guideline_version=protocol.guideline_version, pk_params=pk_params,
    )
    current = dict(protocol.sections or {})
    merged = dict(current)
    ai = AIService()
    for name in names:
        merged[name] = await ai.generate_protocol_section(
            name, context, revision_request=instruction, current_section_text=(current.get(name) or None),
        )
    return merged, None, None, [], None


async def _prepare_engine(
    session: Any, protocol: Protocol, request: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None, str | None, list[str], dict[str, Any] | None]:
    from routers.protocols import _run_generation_pipeline

    changes = dict(request.get("changes") or {})
    merged = merge_engine_changes(protocol.population_config, changes, guideline_version=protocol.guideline_version)
    sections, augmented, rerun_detail = await _run_generation_pipeline(
        session, drug=protocol.drug, org_id=protocol.org_id, study_type=protocol.study_type.value,
        guideline_version=merged.guideline_version, population_config=merged.population_config,
        design_decisions=merged.design_decisions, protocol_id=protocol.id,
    )
    full = {**(protocol.sections or {}), **sections}  # a section that failed to regenerate keeps its old text
    new_guideline = merged.guideline_version if merged.guideline_version != protocol.guideline_version else None
    # What the re-run did, in the same keys the INITIAL snapshot records, for the audit trail.
    pipeline = {k: rerun_detail.get(k) for k in ("generated_sections", "ai_pipeline_log", "ai_qc_remediation")}
    return full, augmented, new_guideline, merged.notes, pipeline


def _qc_dict(protocol: Protocol, sections: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    from routers.protocols import SECTION_DISPLAY_NAMES, _sections_for_type
    from services.protocol_qc import run_qc

    report = run_qc(
        study_type=protocol.study_type.value, population_config=config or {}, sections=sections,
        expected_sections=_sections_for_type(protocol.study_type.value), section_labels=SECTION_DISPLAY_NAMES,
    )
    return report.to_dict()


def _build_detail(
    protocol: Protocol, merged_sections: dict[str, Any], proposed_config: dict[str, Any] | None,
    new_guideline: str | None, notes: list[str], kind: str, pipeline: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from routers.protocols import diff_sections

    base = int(protocol.version)
    diff = diff_sections(protocol.sections or {}, merged_sections, base, base + 1)
    changed = [d.section_name for d in diff if d.status != "unchanged"]
    effective_config = proposed_config if proposed_config is not None else (protocol.population_config or {})
    guideline_after = new_guideline or protocol.guideline_version
    detail: dict[str, Any] = {
        "kind": kind,
        "changed_sections": changed,
        "qc": _qc_dict(protocol, merged_sections, effective_config),
        "guideline_version": new_guideline,
        "notes": list(notes),
    }
    if pipeline:
        detail["pipeline"] = pipeline
    if kind == "engine":
        detail["facts_before"] = fact_rows(protocol.population_config, protocol.guideline_version, protocol.study_type.value)
        detail["facts_after"] = fact_rows(effective_config, guideline_after, protocol.study_type.value)
    return detail


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


async def load_proposal(db: Any, proposal_id: Any, *, org_id: str, session_id: str | None = None) -> ProtocolEditProposal | None:
    """The proposal, only if it is in ``org_id`` and (when given) belongs to the
    chat ``session_id``; ``None`` otherwise (the caller answers 404)."""
    try:
        pid = proposal_id if isinstance(proposal_id, uuid.UUID) else uuid.UUID(str(proposal_id))
    except ValueError:
        return None
    row = (await db.execute(select(ProtocolEditProposal).where(ProtocolEditProposal.id == pid))).scalar_one_or_none()
    if row is None or row.org_id != org_id:
        return None
    if session_id is not None and (row.session_id is None or str(row.session_id) != str(session_id)):
        return None
    return row


async def expire_if_stuck(db: Any, proposal: ProtocolEditProposal) -> bool:
    """A proposal ``generating`` for more than ``PROPOSAL_STUCK_MINUTES`` is
    marked failed (in-process jobs do not survive a deploy). True if changed."""
    if proposal.status != EditProposalStatus.generating:
        return False
    started = _aware(proposal.started_at or proposal.created_at)
    if started is None or _now() - started <= timedelta(minutes=c.PROPOSAL_STUCK_MINUTES):
        return False
    res = await db.execute(
        update(ProtocolEditProposal)
        .where(ProtocolEditProposal.id == proposal.id, ProtocolEditProposal.status == EditProposalStatus.generating)
        .values(status=EditProposalStatus.failed, error=STUCK_MESSAGE)
    )
    await db.commit()
    if (res.rowcount or 0) == 1:
        proposal.status = EditProposalStatus.failed
        proposal.error = STUCK_MESSAGE
        return True
    await db.refresh(proposal)
    return False


async def refresh_staleness(db: Any, proposal: ProtocolEditProposal, protocol: Protocol | None) -> bool:
    """A READY proposal whose protocol moved on (version bumped by a manual edit /
    another apply, no longer a draft, or deleted) becomes ``stale``. True if changed."""
    if proposal.status != EditProposalStatus.ready:
        return False
    moved = (
        protocol is None
        or int(protocol.version) != int(proposal.base_version)
        or protocol.status != ProtocolStatus.draft
    )
    if not moved:
        return False
    await _mark_stale(db, proposal)
    return True


async def _mark_stale(db: Any, proposal: ProtocolEditProposal) -> None:
    await db.execute(
        update(ProtocolEditProposal)
        .where(ProtocolEditProposal.id == proposal.id, ProtocolEditProposal.status == EditProposalStatus.ready)
        .values(status=EditProposalStatus.stale)
    )
    await db.commit()
    proposal.status = EditProposalStatus.stale


async def _base_sections(db: Any, proposal: ProtocolEditProposal, protocol: Protocol | None) -> dict[str, Any] | None:
    """The sections the proposal was computed against."""
    if protocol is not None and int(protocol.version) == int(proposal.base_version):
        return dict(protocol.sections or {})
    row = (await db.execute(
        select(ProtocolVersion).where(
            ProtocolVersion.protocol_id == proposal.protocol_id, ProtocolVersion.version == proposal.base_version,
        )
    )).scalar_one_or_none()
    return dict(row.sections or {}) if row is not None else None


async def build_view(db: Any, proposal: ProtocolEditProposal, protocol: Protocol | None = None) -> dict[str, Any]:
    """Everything a client may see about a proposal (no config, no randomization):
    the dict ``cards.build_edit_diff_card`` takes, extended with the
    ``ProposalRead`` fields. ``diff`` lists only the sections that changed."""
    state = sa_inspect(proposal)
    if state.expired_attributes:  # e.g. updated_at after an UPDATE with a server-side onupdate
        await db.refresh(proposal)
    if protocol is None:
        protocol = (await db.execute(select(Protocol).where(Protocol.id == proposal.protocol_id))).scalar_one_or_none()
    detail = dict(proposal.change_detail or {})
    status = proposal.status.value
    diff: list[dict[str, Any]] = []
    if status in ("ready", "applied") and proposal.proposed_sections:
        base_sections = await _base_sections(db, proposal, protocol)
        if base_sections is not None:
            from routers.protocols import diff_sections

            base = int(proposal.base_version)
            diff = [
                d.model_dump() for d in diff_sections(base_sections, dict(proposal.proposed_sections), base, base + 1)
                if d.status != "unchanged"
            ]
    return {
        "id": str(proposal.id), "protocol_id": str(proposal.protocol_id),
        "session_id": str(proposal.session_id) if proposal.session_id else None,
        "kind": proposal.kind.value, "status": status, "base_version": int(proposal.base_version),
        "applied_version": proposal.applied_version, "request": dict(proposal.request or {}),
        "stale": status == "stale", "diff": diff,
        "qc": detail.get("qc") if status in ("ready", "applied", "stale") else None,
        "facts_before": detail.get("facts_before") or [], "facts_after": detail.get("facts_after") or [],
        "notes": list(detail.get("notes") or []), "error": proposal.error,
        "started_at": proposal.started_at, "created_at": proposal.created_at, "updated_at": proposal.updated_at,
    }


def to_proposal_read(view: dict[str, Any]) -> c.ProposalRead:
    """The ``GET /proposals/{pid}`` body. Facts become ``{label: value}`` maps."""
    def as_map(rows: list[dict[str, str]]) -> dict[str, str] | None:
        return {r["label"]: r["value"] for r in rows} if rows else None

    return c.ProposalRead(
        id=view["id"], protocol_id=view["protocol_id"], session_id=view["session_id"], kind=view["kind"],
        status=view["status"], base_version=view["base_version"], applied_version=view["applied_version"],
        request=view["request"], diff=[c.SectionDiffItem.model_validate(d) for d in view["diff"]],
        qc=view["qc"], facts_before=as_map(view["facts_before"]), facts_after=as_map(view["facts_after"]),
        error=view["error"], started_at=view["started_at"], created_at=view["created_at"],
        updated_at=view["updated_at"],
    )


# ---------------------------------------------------------------------------
# Apply / discard
# ---------------------------------------------------------------------------


async def apply_proposal(
    db: Any, *, proposal_id: str, session_id: str, actor: Any,
) -> dict[str, Any]:
    """Apply a READY proposal to its protocol (the user clicked Apply).

    Raises :class:`ProposalError`: ``proposal_stale`` (409) when the protocol is no
    longer at ``base_version`` or no longer a draft (the proposal is marked
    ``stale``), ``invalid_action`` (422) when the proposal is not ready, and
    ``not_found`` (404) for a proposal of another chat / org. Returns the view of
    the applied proposal."""
    proposal = await load_proposal(db, proposal_id, org_id=actor.org_id, session_id=session_id)
    if proposal is None:
        raise ProposalError(c.ChatErrorCode.not_found, "That proposed change was not found.")
    await expire_if_stuck(db, proposal)
    status = proposal.status
    if status == EditProposalStatus.stale:
        raise ProposalError(c.ChatErrorCode.proposal_stale, "The protocol changed after this edit was prepared. Ask for the change again.")
    if status != EditProposalStatus.ready:
        raise ProposalError(
            c.ChatErrorCode.invalid_action,
            {
                EditProposalStatus.applied: "This change has already been applied.",
                EditProposalStatus.discarded: "This change was discarded.",
                EditProposalStatus.failed: "This change could not be prepared, so there is nothing to apply.",
                EditProposalStatus.generating: "This change is still being prepared.",
                EditProposalStatus.proposed: "This change has not been confirmed yet.",
            }.get(status, "This change cannot be applied."),
        )

    # Serialise with any concurrent save / sign on the same protocol.
    protocol = (await db.execute(
        select(Protocol).where(Protocol.id == proposal.protocol_id).with_for_update()
    )).scalar_one_or_none()
    if protocol is None or protocol.org_id != actor.org_id:
        raise ProposalError(c.ChatErrorCode.not_found, "The protocol was not found.")
    if int(protocol.version) != int(proposal.base_version) or protocol.status != ProtocolStatus.draft:
        await _mark_stale(db, proposal)
        reason = (
            f"This protocol is now '{protocol.status.value}' and can no longer be edited here."
            if protocol.status != ProtocolStatus.draft
            else "The protocol was edited after this change was prepared. Ask for the change again so it starts from the latest text."
        )
        raise ProposalError(c.ChatErrorCode.proposal_stale, reason)
    if not proposal.proposed_sections:
        raise ProposalError(c.ChatErrorCode.invalid_action, "This change has no prepared content to apply.")

    detail = dict(proposal.change_detail or {})
    old_sections = dict(protocol.sections or {})
    new_sections = dict(proposal.proposed_sections)
    changed = list(detail.get("changed_sections") or [k for k in new_sections if new_sections.get(k) != old_sections.get(k)])
    new_version = int(protocol.version) + 1

    protocol.sections = new_sections
    if proposal.proposed_config is not None:
        protocol.population_config = dict(proposal.proposed_config)
    if detail.get("guideline_version"):
        protocol.guideline_version = str(detail["guideline_version"])
    protocol.version = new_version

    # Part 11: a version snapshot and an audit row, in the same transaction.
    from routers.protocols import _snapshot

    snapshot_detail: dict[str, Any] = {
        "proposal_id": str(proposal.id), "session_id": str(proposal.session_id) if proposal.session_id else None,
        "kind": proposal.kind.value, "request": dict(proposal.request or {}), "sections": changed,
        "base_version": int(proposal.base_version),
    }
    if detail.get("pipeline"):  # an engine edit: what the re-run did (same keys as the INITIAL snapshot)
        snapshot_detail.update({k: v for k, v in dict(detail["pipeline"]).items() if v is not None})
    _snapshot(
        db, protocol_id=protocol.id, version=new_version, sections=new_sections, changed_by=actor.id,
        change_type="CHAT_EDIT", change_detail=_plain(snapshot_detail),
    )
    db.add(AuditLog(
        id=uuid.uuid4(), user_id=actor.id, action="CHAT_EDIT:protocol", resource_type="protocol",
        resource_id=str(protocol.id),
        diff=_plain({
            "proposal_id": str(proposal.id), "session_id": str(proposal.session_id) if proposal.session_id else None,
            "kind": proposal.kind.value, "version": new_version, "sections": changed,
            "old_preview": {k: str(old_sections.get(k) or "")[:200] for k in changed[:6]},
            "new_preview": {k: str(new_sections.get(k) or "")[:200] for k in changed[:6]},
        }),
        timestamp=_now(),
    ))
    proposal.status = EditProposalStatus.applied
    proposal.applied_version = new_version
    await db.commit()
    view = await build_view(db, proposal, protocol)
    view["status"] = "applied"
    view["applied_version"] = new_version
    return view


async def discard_proposal(db: Any, *, proposal_id: str, session_id: str, actor: Any) -> ProtocolEditProposal:
    """Mark a proposal discarded (idempotent). An applied proposal cannot be
    discarded. A job still running notices the status change and drops its result."""
    proposal = await load_proposal(db, proposal_id, org_id=actor.org_id, session_id=session_id)
    if proposal is None:
        raise ProposalError(c.ChatErrorCode.not_found, "That proposed change was not found.")
    if proposal.status == EditProposalStatus.applied:
        raise ProposalError(c.ChatErrorCode.invalid_action, "This change has already been applied and cannot be discarded.")
    if proposal.status != EditProposalStatus.discarded:
        proposal.status = EditProposalStatus.discarded
        await db.commit()
    return proposal
