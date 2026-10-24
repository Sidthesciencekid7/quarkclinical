"""MOCK_AI: a scripted stand-in for the Anthropic client (plan B6 "Mock mode").

With ``MOCK_AI`` set, the orchestrator uses :class:`MockAnthropicClient` instead of
the real SDK, so the whole chat flow can be walked in a browser (and in tests)
with zero credits. It implements exactly the subset of the Messages streaming API
the orchestrator uses:

    client.messages.stream(**params)         -> async context manager
    client.beta.messages.stream(**params)    -> the same
        async for event in stream: ...       (message_start, content_block_*, message_delta, message_stop)
        await stream.get_final_message()     -> message with .content/.stop_reason/.usage/.model/.id

What it does is a small deterministic script, not intelligence: it reads the
latest user message and the ``<draft_spec>`` the orchestrator appended, and
emits tool calls from simple patterns (a known drug name, "biosimilar" / "BE" /
"bioequivalence" / "food", "patients" / "healthy", "age 18-45", "EMA" / "FDA",
"generate", "price" / "budget", "40 sites", ...). Every value it sets is quoted
verbatim from the user's text, so the server's quote rule behaves as with the
real model. After the tools run it writes a short plain-text reply built from the
tool results, and it never states a number of its own.

It deliberately writes no ``thinking`` blocks: a mock "signature" replayed to the
real API would be rejected, and mock sessions must not poison real ones.
"""
from __future__ import annotations

import asyncio
import json
import re
from types import SimpleNamespace
from typing import Any

# ---------------------------------------------------------------------------
# Message / block objects (attribute access like the SDK's, plus model_dump)
# ---------------------------------------------------------------------------


class MockBlock(SimpleNamespace):
    """A content block. ``model_dump`` mirrors pydantic's so the orchestrator can
    treat it like an SDK block."""

    def model_dump(self, *, mode: str = "python", exclude_none: bool = False, **_: Any) -> dict[str, Any]:
        out = dict(self.__dict__)
        if exclude_none:
            out = {k: v for k, v in out.items() if v is not None}
        return json.loads(json.dumps(out))


def text_block(text: str) -> MockBlock:
    return MockBlock(type="text", text=text)


def tool_use_block(block_id: str, name: str, tool_input: dict[str, Any]) -> MockBlock:
    return MockBlock(type="tool_use", id=block_id, name=name, input=tool_input)


class MockMessage(SimpleNamespace):
    pass


def _usage() -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=0, output_tokens=0, cache_creation_input_tokens=0, cache_read_input_tokens=0,
        cache_creation=None,
    )


# ---------------------------------------------------------------------------
# Stream object
# ---------------------------------------------------------------------------


class MockStream:
    def __init__(self, message: MockMessage):
        self.message = message

    async def __aenter__(self) -> "MockStream":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    def __aiter__(self):
        return self._events()

    async def _events(self):
        m = self.message
        yield SimpleNamespace(type="message_start", message=SimpleNamespace(id=m.id, model=m.model, role="assistant"))
        for i, block in enumerate(m.content):
            if block.type == "text":
                yield SimpleNamespace(type="content_block_start", index=i, content_block=MockBlock(type="text", text=""))
                for chunk in _chunks(block.text):
                    await asyncio.sleep(0)
                    yield SimpleNamespace(
                        type="content_block_delta", index=i,
                        delta=SimpleNamespace(type="text_delta", text=chunk),
                    )
            else:
                yield SimpleNamespace(
                    type="content_block_start", index=i,
                    content_block=MockBlock(type="tool_use", id=block.id, name=block.name, input={}),
                )
                yield SimpleNamespace(
                    type="content_block_delta", index=i,
                    delta=SimpleNamespace(type="input_json_delta", partial_json=json.dumps(block.input)),
                )
            yield SimpleNamespace(type="content_block_stop", index=i)
        yield SimpleNamespace(
            type="message_delta", delta=SimpleNamespace(stop_reason=m.stop_reason, stop_sequence=None),
            usage=m.usage,
        )
        yield SimpleNamespace(type="message_stop")

    async def get_final_message(self) -> MockMessage:
        return self.message


def _chunks(text: str, size: int = 14) -> list[str]:
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


class _MessagesAPI:
    def __init__(self, client: "MockAnthropicClient"):
        self._client = client

    def stream(self, **params: Any) -> MockStream:
        return MockStream(self._client.respond(params))

    async def create(self, **params: Any) -> MockMessage:  # non-streaming parity
        return self._client.respond(params)


class MockAnthropicClient:
    """Drop-in for the orchestrator's client. ``calls`` records every request's
    params (tests assert on them); ``respond`` can be replaced for custom scripts."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.messages = _MessagesAPI(self)
        self.beta = SimpleNamespace(messages=_MessagesAPI(self))

    def respond(self, params: dict[str, Any]) -> MockMessage:
        self.calls.append(params)
        content = script_response(params)
        has_tools = any(b.type == "tool_use" for b in content)
        return MockMessage(
            id=f"msg_mock_{len(self.calls)}", type="message", role="assistant",
            model=params.get("model") or "claude-opus-5-5", content=content,
            stop_reason="tool_use" if has_tools else "end_turn", stop_details=None, usage=_usage(),
            _request_id=f"req_mock_{len(self.calls)}",
        )


# ---------------------------------------------------------------------------
# The script
# ---------------------------------------------------------------------------

_EXTRA_DRUGS = (
    "adalimumab", "metformin", "ibuprofen", "nivolumab", "pembrolizumab", "insulin glargine", "trastuzumab",
    "rituximab", "bevacizumab", "infliximab", "atorvastatin", "warfarin", "filgrastim", "pegfilgrastim",
)

_SECTION_KEYWORDS: tuple[tuple[str, str], ...] = (
    (r"adverse|safety", "safety_monitoring"), (r"synopsis", "synopsis"), (r"introduction", "introduction"),
    (r"objective", "objectives"), (r"endpoint", "endpoints"), (r"statistic|\bsap\b", "statistical_analysis_plan"),
    (r"ethic", "ethics"), (r"study design", "study_design"), (r"eligib|population", "population"),
    (r"treatment|dosing", "treatment"), (r"assessment", "study_assessments"), (r"rationale", "study_rationale"),
    (r"reference", "references"), (r"monitoring", "study_monitoring"), (r"data handling", "data_handling"),
)

_EXPLAIN_KEYWORDS: tuple[tuple[str, str], ...] = (
    (r"washout", "washout"), (r"\bcv\b|variab", "cv_source"), (r"sample size|how many subjects", "sample_size"),
    (r"pric|cost", "budget_pricing"), (r"parallel|crossover", "design_parallel_crossover"),
    (r"\broute\b|device|injection", "design_route"), (r"\bdose\b|dosing", "design_dose"),
    (r"\bpd\b|endpoint", "design_pd_endpoint"), (r"population|healthy volunteers", "design_population"),
)


def _known_drugs() -> list[str]:
    names = set(_EXTRA_DRUGS)
    try:
        from services.design_decisions import _KB

        names |= set(_KB)
    except Exception:  # noqa: BLE001
        pass
    return sorted(names, key=len, reverse=True)


def _blocks(content: Any) -> list[Any]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return list(content or [])


def _bget(block: Any, key: str, default: Any = None) -> Any:
    return block.get(key, default) if isinstance(block, dict) else getattr(block, key, default)


def _latest_user_text(messages: list[dict[str, Any]]) -> str:
    for m in reversed(messages):
        if m.get("role") != "user":
            continue
        texts = [
            str(_bget(b, "text", ""))
            for b in _blocks(m.get("content")) if _bget(b, "type") == "text"
        ]
        texts = [t for t in texts if not t.lstrip().startswith("<system-reminder>") and "<draft_spec" not in t]
        if texts:
            return "\n".join(texts)
    return ""


def _draft_spec(messages: list[dict[str, Any]]) -> dict[str, Any]:
    pattern = re.compile(r"<draft_spec[^>]*>\s*(\{.*?\})\s*</draft_spec>", re.DOTALL)
    for m in reversed(messages):
        texts = [m.get("content")] if isinstance(m.get("content"), str) else [
            str(_bget(b, "text", "")) for b in _blocks(m.get("content")) if _bget(b, "type") == "text"
        ]
        for t in reversed(texts):
            found = pattern.findall(t or "")
            if found:
                try:
                    return json.loads(found[-1])
                except ValueError:
                    return {}
    return {}


def _tool_results(messages: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any], bool]] | None:
    """``[(tool_name, parsed_result, is_error)]`` when the conversation ends with
    a tool_results message (a continuation call), else None."""
    tail = [m for m in messages if m.get("role") != "system"]
    if not tail or tail[-1].get("role") != "user":
        return None
    results = [b for b in _blocks(tail[-1].get("content")) if _bget(b, "type") == "tool_result"]
    if not results:
        return None
    names: dict[str, str] = {}
    for m in reversed(tail[:-1]):
        if m.get("role") == "assistant":
            for b in _blocks(m.get("content")):
                if _bget(b, "type") == "tool_use":
                    names[str(_bget(b, "id"))] = str(_bget(b, "name"))
            break
    out = []
    for b in results:
        raw = _bget(b, "content")
        if isinstance(raw, list):
            raw = "".join(str(_bget(x, "text", "")) for x in raw)
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else {}
        except ValueError:
            parsed = {"text": raw}
        out.append((names.get(str(_bget(b, "tool_use_id")), "?"), parsed if isinstance(parsed, dict) else {}, bool(_bget(b, "is_error"))))
    return out


def _kind(tool_names: set[str]) -> str:
    if "update_budget_draft" in tool_names:
        return "budget"
    if "get_protocol_overview" in tool_names:
        return "protocol_edit"
    return "protocol"


def script_response(params: dict[str, Any]) -> list[MockBlock]:
    """The assistant message for one request: tool calls on the first call of a
    turn, a plain reply once tool results are in."""
    messages = list(params.get("messages") or [])
    tool_names = {str(t.get("name")) for t in (params.get("tools") or [])}
    kind = _kind(tool_names)
    results = _tool_results(messages)
    if results is not None:
        return [text_block(_compose_reply(kind, results))]
    text = _latest_user_text(messages)
    spec = _draft_spec(messages)
    calls = {
        "protocol": _protocol_calls, "budget": _budget_calls, "protocol_edit": _edit_calls,
    }[kind](text, spec)
    n = len(messages)
    blocks: list[MockBlock] = []
    if calls:
        blocks.append(text_block("One moment while I check that. "))
        blocks.extend(tool_use_block(f"toolu_mock_{n}_{i}", name, args) for i, (name, args) in enumerate(calls))
        return blocks
    return [text_block(_idle_reply(kind, spec))]


def _search(pattern: str, text: str, flags: int = re.IGNORECASE) -> re.Match[str] | None:
    return re.search(pattern, text, flags)


def _explain_calls(text: str) -> list[tuple[str, dict[str, Any]]]:
    if not _search(r"\bwhy\b|\bexplain\b|\bhow (?:does|is|are|do)\b|\bwhat is\b", text):
        return []
    for pattern, topic in _EXPLAIN_KEYWORDS:
        if _search(pattern, text):
            return [("explain_rule", {"topic": topic})]
    return []


def _drug_in(text: str) -> re.Match[str] | None:
    for name in _known_drugs():
        m = _search(r"\b" + re.escape(name) + r"\b", text)
        if m:
            return m
    return None


def _protocol_calls(text: str, spec: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []
    changes: list[dict[str, str]] = []

    def change(field: str, value: str, quote: str) -> None:
        changes.append({"field": field, "value": value, "user_quote": quote})

    drug = _drug_in(text)
    if drug:
        calls.append(("search_drugs", {"query": drug.group(0).lower()}))
    if (m := _search(r"\bbiosimilar\b", text)):
        change("study_type", "biosimilar_pk", m.group(0))
    elif (m := _search(r"\bbioequivalence\b", text)) or (m := _search(r"\bBE\b", text, 0)):
        change("study_type", "BE", m.group(0))
    elif (m := _search(r"\bfood[- ]effect\b", text)):
        change("study_type", "food_effect", m.group(0))
    if (m := _search(r"\bpatients?\b", text)):
        change("subject_type", "patient", m.group(0))
    elif (m := _search(r"\bhealthy\b", text)):
        change("subject_type", "healthy", m.group(0))
    if (m := _search(r"\bages?\s*(\d{2})\s*(?:-|to|–)\s*(\d{2})\b", text)):
        change("age_min", m.group(1), m.group(0))
        change("age_max", m.group(2), m.group(0))
    if (m := _search(r"\b(males?|females?|men|women)\b", text)):
        word = m.group(1).lower()
        change("sex", "male" if word in ("male", "males", "men") else "female", m.group(0))
    if (m := _search(r"\bCV\s*(?:of|=|:|at)?\s*(\d+(?:\.\d+)?)\s*%?", text, 0)) or (m := _search(r"\bcv\s*(?:of|=|:|at)?\s*(\d+(?:\.\d+)?)\s*%?", text)):
        change("cv_intra", m.group(1), m.group(0))
    if (m := _search(r"\bICH[- ]?M13A?\b", text)):
        change("guideline_version", "ICH-M13A-2024", m.group(0))
    elif (m := _search(r"\bICH[- ]?M11\b", text)):
        change("guideline_version", "ICH-M11-2025", m.group(0))
    elif (m := _search(r"\bEMA\b", text, 0)):
        change("guideline_version", "EMA-BE-2010", m.group(0))
    elif (m := _search(r"\bFDA\b", text, 0)):
        change("guideline_version", "FDA-BE-2014", m.group(0))
    if changes:
        calls.append(("update_draft", {"changes": changes}))
    if _search(r"\binteraction|\bscreen\b|\bconcomitant", text):
        calls.append(("screen_interactions", {}))
    if _search(r"\b(review|preview|show|work out)\b.*\bdesign\b|\bdesign (?:decisions|rules)\b|\bstudy design\b", text):
        calls.append(("preview_design", {}))
    if _search(r"\bindications?\b", text):
        calls.append(("rank_indications", {}))
    if _search(r"\bguidelines?\b", text) and not any(c["field"] == "guideline_version" for c in changes):
        calls.append(("list_guidelines", {}))
    if _search(r"\bgenerate\b|\bsummary\b|\bfinish\b|\bready\b", text):
        calls.append(("show_review_summary", {}))
    calls += _explain_calls(text)
    return calls


def _budget_calls(text: str, spec: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []
    changes: list[dict[str, str]] = []

    def change(field: str, value: str, quote: str) -> None:
        changes.append({"field": field, "value": value, "user_quote": quote})

    drug = _drug_in(text)
    if drug:
        change("drug_inn", drug.group(0).lower(), drug.group(0))
    if (m := _search(r"\bpatients?\b", text)) and not _search(r"\b\d+\s*patients\b", text):
        change("subject_profile", "patient", m.group(0))
    elif (m := _search(r"\bhealthy\b", text)):
        change("subject_profile", "healthy", m.group(0))
    if (m := _search(r"(\d+)\s*sites?\b", text)):
        change("n_sites_selected", m.group(1), m.group(0))
    if (m := _search(r"(\d+)\s*countries\b", text)):
        change("n_countries", m.group(1), m.group(0))
    if (m := _search(r"(\d+)\s*patients\b", text)):
        change("n_enrolled", m.group(1), m.group(0))
    if (m := _search(r"\b(USD|EUR|GBP|CAD|AUD|CHF|JPY)\b", text, 0)):
        change("currency", m.group(1), m.group(0))
    if changes:
        calls.append(("update_budget_draft", {"changes": changes}))
    if drug and _search(r"\bprice|\bbudget|\bcost|\bestimate", text):
        calls.append(("preview_budget", {"drug_inn": drug.group(0).lower()}))
    if _search(r"\blink\b|\bmy protocols\b|\bexisting protocol", text):
        calls.append(("list_my_protocols", {}))
    if _search(r"\bsummary\b|\bcreate\b|\bfinish\b|\bready\b", text):
        calls.append(("show_budget_summary", {}))
    calls += _explain_calls(text)
    return calls


def _edit_calls(text: str, spec: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []
    if spec.get("next_step") == "overview" or _search(r"\boverview\b|\bsummar(?:y|ise)\b", text):
        calls.append(("get_protocol_overview", {}))
    if _search(r"\bqc\b|\bquality check", text):
        calls.append(("get_qc_summary", {}))
    if (m := _search(r"\b(?:shorten|rewrite|expand|simplify|tighten|reword|trim)\b[^.\n]*", text)):
        for pattern, section in _SECTION_KEYWORDS:
            if _search(pattern, m.group(0)):
                calls.append(("propose_section_revision", {
                    "sections": [section], "instruction": m.group(0).strip()[:500], "user_quote": m.group(0).strip(),
                }))
                break
    changes: list[dict[str, str]] = []
    quote = ""
    if (m := _search(r"\bcv\b\D{0,12}(\d+(?:\.\d+)?)\s*%?", text)):
        changes.append({"field": "cv_intra", "value": m.group(1)})
        quote = m.group(0)
    if (m := _search(r"\bwashout\b\D{0,16}(\d+(?:\.\d+)?)\s*days?", text)):
        changes.append({"field": "washout_days", "value": m.group(1)})
        quote = m.group(0)
    if (m := _search(r"\bnti\b|\bnarrow therapeutic", text)):
        changes.append({"field": "is_nti", "value": "true"})
        quote = m.group(0)
    if changes:
        calls.append(("propose_input_change", {"changes": changes, "user_quote": quote}))
    if (m := _search(r"\bread\b[^.\n]*\bsection\b", text)):
        for pattern, section in _SECTION_KEYWORDS:
            if _search(pattern, m.group(0)):
                calls.append(("get_section", {"name": section}))
                break
    calls += _explain_calls(text)
    return calls


# -- replies -------------------------------------------------------------------


def _idle_reply(kind: str, spec: dict[str, Any]) -> str:
    step = spec.get("next_step")
    if kind == "protocol":
        hints = {
            "drug": "Which drug is the study for? Name it and I will search the catalogue.",
            "study_type": "Which type of study is it: bioequivalence, biosimilar PK, or food effect?",
            "population": "You can tell me the age range and whether it is healthy volunteers or patients, or keep the defaults.",
            "design": "Ask me to review the design and I will show the decisions for you to confirm.",
            "indication": "Ask me to rank the indications, or let the engine choose.",
            "guideline": "Tell me which guideline to use (FDA, EMA or ICH M13A), or keep FDA.",
            "summary": "Everything is in place. Ask for the summary when you want to generate.",
        }
        return hints.get(str(step), "Tell me about the study you want to set up: the drug and the type of study.")
    if kind == "budget":
        return "Which drug should I price? A drug name alone is enough; you can add sites and countries for a patient study."
    if step == "blocked":
        return "This protocol cannot be edited here. Clone it to make a new draft first."
    return "Tell me what to change: a section to reword, or an input such as the CV."


def _compose_reply(kind: str, results: list[tuple[str, dict[str, Any], bool]]) -> str:
    parts: list[str] = []
    for name, res, is_error in results:
        if is_error or "error" in res:
            parts.append(str(res.get("error") or "That did not work."))
        elif name == "search_drugs":
            n = len(res.get("matches") or [])
            if n:
                parts.append("I found " + ("a match" if n == 1 else "some matches") + " in the drug catalogue. Pick the right one on the card.")
            else:
                parts.append("I could not find that drug in the catalogue. If it is a real drug name, the card lets you add it.")
        elif name in ("update_draft", "update_budget_draft"):
            rejected = [r for r in res.get("results") or [] if not r.get("accepted")]
            accepted = [r for r in res.get("results") or [] if r.get("accepted")]
            if accepted:
                parts.append("Noted.")
            for r in rejected:
                parts.append(str(r.get("message") or "That value was not accepted."))
        elif name == "screen_interactions":
            parts.append("The interaction screening is on the card. Switch on any rows you want as exclusion criteria.")
        elif name == "preview_design":
            parts.append("The design decisions are on the card. Review them and click Confirm design.")
        elif name == "set_design_decision":
            parts.append("I updated the design card. Please confirm it when you are happy.")
        elif name == "rank_indications":
            parts.append("The indication ranking is on the card.")
        elif name == "list_guidelines":
            parts.append("The guidelines are on the card.")
        elif name == "show_review_summary":
            if res.get("ready"):
                parts.append("The summary is on the card. Click Generate protocol when you are ready.")
            else:
                missing = ", ".join(str(x) for x in res.get("missing") or [])
                parts.append(f"Not ready yet. Still needed: {missing}.")
        elif name == "preview_budget":
            parts.append("The estimate is on the card above. You can adjust sites, countries or currency, or ask for the summary.")
        elif name == "list_my_protocols":
            parts.append("Your protocols are on the card. Link one with a click if you want the cost drivers to follow it.")
        elif name == "show_budget_summary":
            if res.get("ready"):
                parts.append("The summary is on the card. Click Create budget when you are ready.")
            else:
                parts.append("Not ready yet: I still need a drug name.")
        elif name == "explain_rule":
            parts.append(f"{res.get('title', 'Rule')}: {res.get('text', '')}")
        elif name == "get_protocol_overview":
            parts.append("The protocol overview is on the card." + ("" if res.get("editable") else " It cannot be edited here: " + str(res.get("blocked_reason") or "")))
        elif name == "get_section":
            parts.append(f"I read the {res.get('label', 'section')}. What should change?")
        elif name == "get_qc_summary":
            parts.append("I ran the QC checks; the findings that need attention are in my notes. Ask me about any of them.")
        elif name in ("propose_input_change", "propose_section_revision"):
            if res.get("request_recorded"):
                parts.append("The impact card shows what will change. Nothing happens until you click it.")
            else:
                msgs = [str(r.get("message")) for r in res.get("results") or [] if not r.get("accepted") and r.get("message")]
                parts.append(" ".join(msgs) or str(res.get("message") or "I could not record that change."))
    return " ".join(p for p in parts if p).strip() or "Done."
