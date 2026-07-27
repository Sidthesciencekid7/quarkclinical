"""Protocol amendment traceability.

Every protocol change creates an immutable ``ProtocolVersion`` snapshot (see
models.ProtocolVersion). This module diffs consecutive snapshots to produce a
human-readable **amendment history** — for each amended version, which sections
changed, the nature of the change, who made it and when, and a short summary of
what changed. That history is rendered as a "Summary of Changes" appendix in the
export (routers/export.py) so an amended protocol carries its own change record —
the traceability a 21 CFR Part 11 / ISO-style change-control process requires.

Section NUMBERS (and, later, page numbers) are resolved by the export layer,
which owns the document numbering; this module works purely from section keys and
snapshot text so it stays presentation-agnostic and unit-testable.
"""
from __future__ import annotations

import re
from typing import Any

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _plain(html: str) -> str:
    """Tag-stripped, whitespace-collapsed text for diffing/summarising."""
    txt = _TAG_RE.sub(" ", html or "")
    txt = (txt.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
              .replace("&nbsp;", " ").replace("&#39;", "'").replace("&quot;", '"'))
    return _WS_RE.sub(" ", txt).strip()


def _sentences(text: str) -> list[str]:
    # Split on sentence-ish boundaries; keep it simple and robust.
    parts = re.split(r"(?<=[.;:])\s+", text)
    return [p.strip() for p in parts if p.strip()]


def _summarize_change(prev_html: str, curr_html: str, kind: str) -> str:
    """One-line, human-readable description of what changed in a section."""
    a, b = _plain(prev_html), _plain(curr_html)
    if kind == "added":
        return f"Section added ({len(b)} characters). Begins: \"{b[:120]}…\""
    if kind == "removed":
        return f"Section removed (was {len(a)} characters)."
    # modified — find the first sentence that differs, report before -> after
    import difflib
    sa, sb = _sentences(a), _sentences(b)
    sm = difflib.SequenceMatcher(a=sa, b=sb, autojunk=False)
    added = removed = 0
    first_before = first_after = ""
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        removed += (i2 - i1)
        added += (j2 - j1)
        if not first_before and not first_after:
            first_before = " ".join(sa[i1:i2])[:110]
            first_after = " ".join(sb[j1:j2])[:110]
    delta = len(b) - len(a)
    detail = ""
    if first_after and first_before:
        detail = f" e.g. \"{first_before}…\" → \"{first_after}…\""
    elif first_after:
        detail = f" e.g. added \"{first_after}…\""
    elif first_before:
        detail = f" e.g. removed \"{first_before}…\""
    return (f"Text revised ({added} passage(s) changed, "
            f"{'+' if delta >= 0 else ''}{delta} characters).{detail}")


def diff_sections(prev: dict[str, str], curr: dict[str, str]) -> list[dict[str, Any]]:
    """Per-section changes between two snapshots (order-stable by curr then prev)."""
    changes: list[dict[str, Any]] = []
    ordered_keys = list(dict.fromkeys(list(curr.keys()) + list(prev.keys())))
    for key in ordered_keys:
        a = (prev.get(key) or "").strip()
        b = (curr.get(key) or "").strip()
        if _plain(a) == _plain(b):
            continue
        kind = "added" if not a else "removed" if not b else "modified"
        changes.append({
            "section_key": key,
            "kind": kind,
            "summary": _summarize_change(a, b, kind),
        })
    return changes


def build_amendment_history(versions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Diff consecutive version snapshots into a flat amendment change list.

    ``versions`` is an ascending-by-version list of dicts with keys:
    ``version``, ``sections`` (dict), ``changed_by``, ``change_type``,
    ``created_at`` (datetime or iso string). Returns one record per changed
    section per amended version (version >= 2). Version 1 (INITIAL) is the
    baseline and produces no amendment rows.
    """
    if not versions:
        return []
    ordered = sorted(versions, key=lambda v: v.get("version", 0))
    out: list[dict[str, Any]] = []
    for i in range(1, len(ordered)):
        prev, curr = ordered[i - 1], ordered[i]
        for ch in diff_sections(prev.get("sections") or {}, curr.get("sections") or {}):
            created = curr.get("created_at")
            date_str = getattr(created, "strftime", lambda *_: str(created or ""))("%d-%b-%Y %H:%M UTC")
            out.append({
                "version": curr.get("version"),
                "date": date_str,
                "changed_by": str(curr.get("changed_by") or "—"),
                "change_type": curr.get("change_type") or "AMENDMENT",
                **ch,
            })
    return out
