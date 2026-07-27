"""R PowerTOST integration — regulatory-grade sample-size computation.

For a regulatory submission the sample size should be computed by (or formally
qualified against) a validated, citable tool. This module shells out to the **R
PowerTOST** package (``sampleN.TOST`` / ``power.TOST``) — the de-facto reference
implementation for bioequivalence power/sample size — and captures full
provenance so the result is defensible and reproducible:

  * the PowerTOST package version and the R version,
  * the EXACT R call that was executed, and
  * the raw PowerTOST console output (the artifact a reviewer can re-run).

When R / PowerTOST is not installed, callers fall back to the validated pure
Python implementation in :mod:`services.biostatistics`, which is qualified to
reproduce PowerTOST's canonical example to < 1e-6. Detection is cached so the
absence of R costs at most one lookup per process.

Nothing here is on a request hot-path that requires R: the integration is
best-effort and fully degradable.
"""
from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
from functools import lru_cache
from typing import Any, Optional

# Map our internal design keys to PowerTOST `design` argument values for the
# plain sampleN.TOST path. Replicate designs (NTI / highly-variable) use the
# dedicated sample_n_ntid_r() / sample_n_rsabe_r() functions below instead,
# since PowerTOST models those with different (simulation-based) functions.
_DESIGN_MAP = {
    "crossover": "2x2",
    "parallel": "parallel",
}

_RESULT_RE = re.compile(r"PTRESULT\|N=(\d+)\|POWER=([0-9.]+)\|PTVERSION=([^\|]+)\|RVERSION=([^\|]+)")

# Fixed, project-local R package library. PowerTOST is installed here and R is
# always pointed at it via R_LIBS_USER, so the package is found deterministically
# regardless of the Windows packaged-app %LOCALAPPDATA% redirection (which makes
# the default per-user library differ between a shell-launched and a
# Python-subprocess-launched R). ``RSCRIPT_PATH``/``R_POWERTOST_LIB`` env vars
# override the Rscript binary / library path for other deployment hosts.
_RLIB = os.getenv("R_POWERTOST_LIB") or os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir, "_rlib")
)


def _find_rscript() -> Optional[str]:
    """Locate Rscript.exe across PATH, an env override, and standard install dirs."""
    env = os.getenv("RSCRIPT_PATH")
    if env and os.path.isfile(env):
        return env
    found = shutil.which("Rscript") or shutil.which("Rscript.exe")
    if found:
        return found
    patterns = [
        r"C:\Program Files\R\R-*\bin\x64\Rscript.exe",
        r"C:\Program Files\R\R-*\bin\Rscript.exe",
        r"C:\Program Files\R\*\bin\x64\Rscript.exe",
        r"C:\Program Files\R\*\bin\Rscript.exe",
        os.path.expanduser(r"~\AppData\Local\Programs\R\R-*\bin\x64\Rscript.exe"),
        os.path.expanduser(r"~\AppData\Local\Programs\R\R-*\bin\Rscript.exe"),
        "/usr/bin/Rscript",
        "/usr/local/bin/Rscript",
    ]
    hits: list[str] = []
    for pat in patterns:
        hits.extend(glob.glob(pat))
    if hits:
        # Prefer the lexicographically greatest path (latest R version dir).
        return sorted(hits)[-1]
    return None


def _run_r(rscript: str, body: str, timeout: float = 60.0) -> tuple[str, str, int]:
    """Run an R snippet, returning (stdout, stderr, returncode).

    Reproducible flags (no saved workspace, no site/init profiles) but NOT
    ``--no-environ`` — R must read its environment so the per-user package
    library (R_LIBS_USER), where PowerTOST is installed, stays on .libPaths().
    """
    env = dict(os.environ)
    if os.path.isdir(_RLIB):
        # Point R at the fixed project-local library so PowerTOST is always found.
        env["R_LIBS_USER"] = _RLIB
    proc = subprocess.run(
        [rscript, "--no-save", "--no-restore", "--no-site-file", "--no-init-file", "-e", body],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, env=env,
    )
    return proc.stdout, proc.stderr, proc.returncode


@lru_cache(maxsize=1)
def r_powertost_status() -> dict[str, Any]:
    """Detect R and the PowerTOST package. Cached for the process lifetime.

    Returns a dict with ``available`` plus, when present, ``rscript``,
    ``r_version`` and ``powertost_version`` (or ``error`` when unavailable).
    """
    rscript = _find_rscript()
    if not rscript:
        return {"available": False, "error": "Rscript not found on this host."}
    body = (
        'ok <- requireNamespace("PowerTOST", quietly=TRUE); '
        'ptv <- if (ok) as.character(packageVersion("PowerTOST")) else "NA"; '
        'cat(sprintf("PTSTATUS|R=%s|PT=%s", as.character(getRversion()), ptv))'
    )
    try:
        out, err, rc = _run_r(rscript, body, timeout=60.0)
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "rscript": rscript, "error": f"R invocation failed: {exc}"}
    m = re.search(r"PTSTATUS\|R=([^\|]+)\|PT=(\S+)", out)
    if not m:
        return {"available": False, "rscript": rscript,
                "error": f"Unexpected R status output: {out.strip()[:200] or err.strip()[:200]}"}
    r_version, pt_version = m.group(1), m.group(2)
    if pt_version == "NA":
        return {"available": False, "rscript": rscript, "r_version": r_version,
                "error": "PowerTOST package is not installed in R."}
    return {
        "available": True, "rscript": rscript,
        "r_version": r_version, "powertost_version": pt_version,
    }


def _format_call(*, alpha: float, cv: float, theta0: float, theta1: float,
                 theta2: float, target_power: float, design: str) -> str:
    return (
        f'sampleN.TOST(alpha={alpha:g}, CV={cv:.6g}, theta0={theta0:g}, '
        f'theta1={theta1:g}, theta2={theta2:g}, targetpower={target_power:g}, '
        f'design="{design}", print=FALSE)'
    )


def sample_n_tost_r(
    *,
    cv_pct: float,
    design_key: str,
    theta0: float = 0.95,
    target_power: float = 0.90,
    theta1: float = 0.80,
    theta2: float = 1.25,
    alpha: float = 0.05,
    timeout: float = 60.0,
) -> Optional[dict[str, Any]]:
    """Compute the sample size with the real R PowerTOST ``sampleN.TOST``.

    Returns a provenance-rich dict (n, achieved_power, powertost_version,
    r_version, r_call, r_output) or ``None`` if R/PowerTOST is unavailable or the
    design is not supported by this path (replicate/RSABE).
    """
    status = r_powertost_status()
    if not status.get("available"):
        return None
    design = _DESIGN_MAP.get(design_key)
    if design is None:
        return None

    cv = cv_pct / 100.0
    call = _format_call(alpha=alpha, cv=cv, theta0=theta0, theta1=theta1,
                        theta2=theta2, target_power=target_power, design=design)
    # One script: run for values (print=FALSE) AND capture the formatted banner
    # (print=TRUE) as the human-readable, re-runnable audit artifact.
    body = (
        'suppressWarnings(suppressMessages(library(PowerTOST)))\n'
        f'res <- {call}\n'
        'banner <- paste(capture.output('
        f'{_format_call(alpha=alpha, cv=cv, theta0=theta0, theta1=theta1, theta2=theta2, target_power=target_power, design=design).replace("print=FALSE", "print=TRUE")}'
        '), collapse="\\n")\n'
        'cat(sprintf("PTRESULT|N=%d|POWER=%.8f|PTVERSION=%s|RVERSION=%s",\n'
        '  as.integer(res[["Sample size"]]), as.numeric(res[["Achieved power"]]),\n'
        '  as.character(packageVersion("PowerTOST")), as.character(getRversion())))\n'
        'cat("\\nPTBANNER_START\\n"); cat(banner); cat("\\nPTBANNER_END\\n")\n'
    )
    try:
        out, err, rc = _run_r(status["rscript"], body, timeout=timeout)
    except Exception:  # noqa: BLE001
        return None
    m = _RESULT_RE.search(out)
    if not m:
        return None
    banner = ""
    bm = re.search(r"PTBANNER_START\n(.*?)\nPTBANNER_END", out, re.DOTALL)
    if bm:
        banner = bm.group(1).strip()
    return {
        "engine": "R PowerTOST",
        "powertost_version": m.group(3),
        "r_version": m.group(4),
        "n": int(m.group(1)),
        "achieved_power": float(m.group(2)),
        "design": design,
        "r_call": f"library(PowerTOST); {call}",
        "r_output": banner,
    }


def _run_scaled_sample_n(
    fn_name: str, call_args: str, *, timeout: float,
) -> Optional[dict[str, Any]]:
    """Shared runner for the simulation-based scaled methods (NTID/RSABE) —
    same result shape as sample_n_tost_r's return, so callers can treat all
    three uniformly. ``call_args`` is the fully-formatted argument string
    (without ``print=``); ``fn_name`` is the PowerTOST function name.
    """
    status = r_powertost_status()
    if not status.get("available"):
        return None
    call = f"{fn_name}({call_args}, print=FALSE, details=FALSE)"
    banner_call = f"{fn_name}({call_args}, print=TRUE, details=FALSE)"
    body = (
        'suppressWarnings(suppressMessages(library(PowerTOST)))\n'
        f'res <- {call}\n'
        f'banner <- paste(capture.output({banner_call}), collapse="\\n")\n'
        'cat(sprintf("PTRESULT|N=%d|POWER=%.8f|PTVERSION=%s|RVERSION=%s",\n'
        '  as.integer(res[["Sample size"]]), as.numeric(res[["Achieved power"]]),\n'
        '  as.character(packageVersion("PowerTOST")), as.character(getRversion())))\n'
        'cat("\\nPTBANNER_START\\n"); cat(banner); cat("\\nPTBANNER_END\\n")\n'
    )
    try:
        out, err, rc = _run_r(status["rscript"], body, timeout=timeout)
    except Exception:  # noqa: BLE001
        return None
    m = _RESULT_RE.search(out)
    if not m:
        return None
    banner = ""
    bm = re.search(r"PTBANNER_START\n(.*?)\nPTBANNER_END", out, re.DOTALL)
    if bm:
        banner = bm.group(1).strip()
    return {
        "engine": "R PowerTOST",
        "powertost_version": m.group(3),
        "r_version": m.group(4),
        "n": int(m.group(1)),
        "achieved_power": float(m.group(2)),
        "r_call": f"library(PowerTOST); {call}",
        "r_output": banner,
    }


def sample_n_ntid_r(
    *, cv_pct: float, theta0: float = 0.95, target_power: float = 0.90,
    theta1: float = 0.80, theta2: float = 1.25, alpha: float = 0.05,
    timeout: float = 60.0,
) -> Optional[dict[str, Any]]:
    """FDA method for Narrow Therapeutic Index Drugs, via R PowerTOST's
    ``sampleN.NTID`` — a fully-replicated 4-period (2x2x4) design that jointly
    tests scaled ABE and a within-subject variability ratio (simulation-based;
    no closed form exists). ``theta1``/``theta2`` are the NOMINAL 0.80-1.25
    limits; the function scales them internally per the FDA NTID method.
    Returns the same shape as sample_n_tost_r, plus ``design`` fixed to "2x2x4".
    """
    args = (f"alpha={alpha:g}, targetpower={target_power:g}, theta0={theta0:g}, "
            f"theta1={theta1:g}, theta2={theta2:g}, CV={cv_pct/100.0:.6g}, "
            f'design="2x2x4"')
    res = _run_scaled_sample_n("sampleN.NTID", args, timeout=timeout)
    if res:
        res["design"] = "2x2x4"
    return res


def sample_n_rsabe_r(
    *, cv_pct: float, theta0: float = 0.95, target_power: float = 0.90,
    theta1: float = 0.80, theta2: float = 1.25, alpha: float = 0.05,
    regulator: str = "FDA", timeout: float = 60.0,
) -> Optional[dict[str, Any]]:
    """FDA Reference-Scaled Average Bioequivalence method for highly-variable
    drugs, via R PowerTOST's ``sampleN.RSABE`` — a partial-replicate 3-period
    (2x3x3) design (simulation-based). ``theta1``/``theta2`` are the NOMINAL
    0.80-1.25 limits; the function widens them internally once CV exceeds the
    ~30% switching point. Returns the same shape as sample_n_tost_r, plus
    ``design`` fixed to "2x3x3".
    """
    args = (f"alpha={alpha:g}, targetpower={target_power:g}, theta0={theta0:g}, "
            f"theta1={theta1:g}, theta2={theta2:g}, CV={cv_pct/100.0:.6g}, "
            f'design="2x3x3", regulator="{regulator}"')
    res = _run_scaled_sample_n("sampleN.RSABE", args, timeout=timeout)
    if res:
        res["design"] = "2x3x3"
    return res
