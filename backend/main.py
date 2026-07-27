import os
import subprocess
import sys

import httpx
from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from routers import audit, comments, drugs, export, guidelines, leads, literature, orgs, pipeline, protocols, risk, study_reports, users


def _init_sentry() -> None:
    """Wire up Sentry error monitoring — a NO-OP unless SENTRY_DSN is set.

    So this is safe to ship before the Sentry project exists: nothing happens
    until you create the project and add SENTRY_DSN (+ optional SENTRY_ENV) to
    the Railway env. Once set, unhandled exceptions in any route are reported
    automatically via the FastAPI integration. traces_sample_rate is low to
    keep the free tier well within quota; send_default_pii stays False so we
    never ship request bodies / emails to Sentry by default.
    """
    dsn = os.getenv("SENTRY_DSN", "").strip()
    if not dsn:
        return
    try:
        import sentry_sdk  # imported lazily so a missing package never crashes boot

        sentry_sdk.init(
            dsn=dsn,
            environment=os.getenv("SENTRY_ENV", "production"),
            traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.1")),
            send_default_pii=False,
        )
        print("Sentry error monitoring enabled.")
    except Exception as exc:  # never let monitoring setup take down the app
        print(f"Sentry init skipped ({exc!r}).", file=sys.stderr)


def _run_migrations_on_boot() -> None:
    """Belt-and-suspenders: apply any pending Alembic migrations before the
    app starts serving, regardless of how the process was launched.

    The Procfile also chains `alembic upgrade head &&` before `uvicorn` —
    that's the intended mechanism — but on Railway a service can have its
    own dashboard-configured Start Command that silently overrides the
    Procfile entirely, which is exactly what happened here (confirmed via
    deploy logs: "Starting Container" was immediately followed by uvicorn's
    own startup lines, with zero Alembic output in between, and a
    subsequent request failed with `relation "leads" does not exist` even
    though migration 005 had been written and applied locally).

    Runs as a SUBPROCESS, not `alembic.command.upgrade()` in-process —
    alembic/env.py's `run_migrations_online()` calls `asyncio.run(...)`,
    which cannot be nested inside an event loop FastAPI may already be
    running by the time module import happens under some ASGI servers, so a
    fresh subprocess sidesteps that entirely. `upgrade head` is idempotent:
    on an already-current database (e.g. every local dev restart) this is a
    fast no-op.

    Skipped under pytest — tests point their own engine at DATABASE_URL_TEST
    via conftest.py's patched_db fixture, not at DATABASE_URL, so running
    this here would migrate the wrong (dev) database on every test session
    for no benefit.

    INCIDENT HISTORY (resolved — kept for context): production's
    `alembic_version` table didn't exist at all — the database had been
    provisioned some other way (never truly tracked by Alembic), even though
    its schema genuinely matched migrations 001-003. The first deploy of
    this function raised on migration failure, which is correct in
    principle but took the whole app down when `upgrade head` tried to
    replay migration 001 from scratch and hit `DuplicateObjectError` on
    objects that already existed. Fixed by manually creating
    `alembic_version` and stamping it to '003' (matching the database's
    true state, confirmed via direct inspection — not guessed), then letting
    this function apply 004 and 005 for real. Confirmed resolved: `leads`
    table now exists and serves real inserts, and migration 004's backfill
    correctly marked the pre-existing `default-org` license `active`. Now
    that the version bookkeeping is trustworthy, raising on failure again is
    the right posture — a future migration failure should be loud, not
    silently skipped. See [[project_launch_readiness_g4]] for the full story.
    """
    if "pytest" in sys.modules:
        return

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=os.path.dirname(os.path.abspath(__file__)),
        capture_output=True, text=True,
    )
    print(result.stdout, end="")
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr, end="")
        raise RuntimeError("Database migration failed on startup — refusing to start.")


_init_sentry()
_run_migrations_on_boot()

app = FastAPI(title="PK/BE Protocol Platform API", version="1.0.0")

# Allowed browser origins. Defaults to the local Vite dev server; in production
# set ALLOWED_ORIGINS to a comma-separated list including the deployed frontend
# domain (e.g. "https://protocolai.vercel.app,https://app.yourdomain.com").
# NOTE: the recommended prod topology proxies /api through Vercel to the backend
# (same-origin — see DEPLOY.md), which avoids CORS entirely and also sidesteps
# the cross-origin Authorization-header-strip issue; CORS here is the fallback
# for a direct cross-origin setup.
_default_origins = "http://localhost:5173"
ALLOWED_ORIGINS = [
    o.strip() for o in os.getenv("ALLOWED_ORIGINS", _default_origins).split(",") if o.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Auth model (verification is a no-op when CLERK_ISSUER is unset — see
# auth.AUTH_ENABLED; /health is always public for load balancers):
#   - WRITES and protocol-scoped access require a valid session (require_user):
#     protocol generate/regenerate/edit/sign, all exports, QC, coverage,
#     versions, diff, audit reads, drug create, literature fetch, users list.
#   - Protocol-scoped routes ALSO enforce org isolation via
#     dependencies.get_protocol_scoped (B3) — a session alone isn't enough;
#     the actor must belong to the protocol's org.
#   - Only genuinely public reads stay open: GET /api/drugs search (global
#     reference catalog) so the entry flow can look drugs up.
# KNOWN GAP: the acting identity still comes from the client-supplied
# X-User-Id header (dependencies.authenticate_actor); the verified Clerk `sub`
# is not yet linked to the users table, so require_user proves only that *a*
# valid session exists, not *whose*. Closing that is the outstanding critical
# auth item — see project_security_scan / project_org_isolation audit notes.
app.include_router(drugs.router)
app.include_router(literature.router)
app.include_router(protocols.router)
app.include_router(export.router)
app.include_router(audit.router)
app.include_router(pipeline.router)
app.include_router(users.router)
app.include_router(guidelines.router)
app.include_router(risk.router)
app.include_router(orgs.router)
app.include_router(leads.router)
app.include_router(comments.router)
app.include_router(study_reports.router)


# ---------------------------------------------------------------------------
# Health endpoint — checks DB connection and Anthropic API reachability
# ---------------------------------------------------------------------------

_ANTHROPIC_MODELS_URL = "https://api.anthropic.com/v1/models"
_ANTHROPIC_VERSION_HEADER = "2023-06-01"
_HEALTH_TIMEOUT = 4.0  # seconds; kept short so load-balancers don't time out


@app.get("/health", tags=["ops"])
async def health_check(db: AsyncSession = Depends(get_db)):
    """Liveness + dependency check endpoint (21 CFR Part 11 operational continuity).

    Returns HTTP 200 with ``status="ok"`` when all checks pass.
    Returns HTTP 200 with ``status="degraded"`` when any check fails — callers
    can use the ``checks`` map to determine which subsystem is down.

    Checks performed:
    - **database**: executes ``SELECT 1`` via the async DB connection.
    - **anthropic_api**: sends a lightweight GET to the Anthropic models list
      endpoint.  HTTP 200 or 401 (invalid key but network reachable) are both
      treated as ``"ok"``; the check confirms network reachability only, not
      key validity.
    """
    checks: dict[str, str] = {}

    # 1 ── Database ─────────────────────────────────────────────────────────
    try:
        await db.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["database"] = f"error: {str(exc)[:120]}"

    # 2 ── Anthropic API ─────────────────────────────────────────────────────
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        checks["anthropic_api"] = "no_api_key_configured"
    else:
        try:
            async with httpx.AsyncClient(timeout=_HEALTH_TIMEOUT) as client:
                resp = await client.get(
                    _ANTHROPIC_MODELS_URL,
                    headers={
                        "x-api-key": api_key,
                        "anthropic-version": _ANTHROPIC_VERSION_HEADER,
                    },
                )
            # 200 = success; 401 = key rejected but endpoint reachable
            if resp.status_code in (200, 401):
                checks["anthropic_api"] = "ok"
            else:
                checks["anthropic_api"] = f"http_{resp.status_code}"
        except httpx.ConnectError:
            checks["anthropic_api"] = "unreachable"
        except httpx.TimeoutException:
            checks["anthropic_api"] = "timeout"
        except Exception as exc:  # noqa: BLE001
            checks["anthropic_api"] = f"error: {str(exc)[:120]}"

    overall = "ok" if all(v == "ok" for v in checks.values()) else "degraded"
    return {"status": overall, "checks": checks}
