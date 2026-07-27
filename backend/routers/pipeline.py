"""Three-stage pipeline router — exposes the Haiku/Opus/Sonnet protocol
generation pipeline (services/three_stage_pipeline.py) as a single HTTP route.

This endpoint spends real Anthropic credits (Opus generation + Sonnet
validation), so it is gated behind ``require_user`` exactly like the main
``POST /api/protocols`` generation route. The per-stage ``pipeline_log`` is the
primary error surface; on top of that, a fatal API failure at Stage 1 (credit
exhaustion, bad key) is mapped to a clean 402/502 instead of a 200 carrying a
buried error, matching the protocols router's behaviour.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from auth import require_user
from database import get_db
from dependencies import authenticate_actor, rate_limit, require_active_license
from schemas import PipelineGenerateRequest, PipelineGenerateResponse
from services.three_stage_pipeline import run_pipeline

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])


def _fatal_http_error(pipeline_log: list[dict]) -> tuple[int, str] | None:
    """If the pipeline halted because the API itself is unusable, map it to a
    clean (status, detail). Returns None for ordinary low-confidence halts."""
    if not pipeline_log:
        return None
    first = pipeline_log[0]
    if first.get("status") != "failed":
        return None
    err = (first.get("error") or "").lower()
    if "credit balance" in err:
        return 402, (
            "Generation failed: the Anthropic API credit balance is too low. "
            "Add credits at console.anthropic.com (Plans & Billing), then try "
            "again. Note: a Claude Pro subscription does NOT include API credits."
        )
    if "authentication" in err or "api_key" in err or "api key" in err:
        return 502, "Generation failed: the Anthropic API key is invalid or missing."
    return None


@router.post(
    "/generate",
    response_model=PipelineGenerateResponse,
    dependencies=[
        Depends(require_user),
        Depends(rate_limit(10, 60, scope="pipeline_generate")),
    ],
)
async def generate_via_pipeline(
    payload: PipelineGenerateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Run the three-stage pipeline and return its full structured result.

    Stage 1 (Haiku) classifies and gates on confidence >= 0.85; Stage 2 (Opus)
    generates the protocol from pre-computed deterministic facts; Stage 3
    (Sonnet) returns structured JSON validation. A low-confidence Stage 1 halt
    returns 200 with ``halt_reason`` populated and empty protocol/validation —
    that is a valid, non-error outcome.

    Note: this is a long synchronous call (Opus generation alone runs into the
    minutes). Callers must use a generous client timeout.

    SECURITY: this route spends real Anthropic credits (Opus + Sonnet), so —
    like POST /api/protocols — it must be gated by more than just "a valid
    session exists". ``require_user`` alone let ANY Clerk signup (public sign-up
    flow) drain the API balance by looping this endpoint. It now additionally
    requires the caller to be a PROVISIONED actor (authenticate_actor → 403 for
    unprovisioned emails) whose org holds an ACTIVE license
    (require_active_license → 402 otherwise), matching create_protocol's gate.
    """
    actor = await authenticate_actor(request, db)
    await require_active_license(db, actor)

    result = await run_pipeline(payload.query, payload.user_params)

    fatal = _fatal_http_error(result["pipeline_log"])
    if fatal:
        raise HTTPException(status_code=fatal[0], detail=fatal[1])

    return result
