"""GET /api/chat/stream-check and the SSE plumbing (B4).

The stream-check route exists to prove, after a deploy, that the platform in front
of the API does not buffer a stream: five ``tick`` events, a few seconds apart, no
AI. These tests pin its wire format and, by calling the ASGI app directly (the
in-process HTTP client collects a whole body before returning), that bytes really
leave the app progressively and that an idle turn stream carries ``: ping``
comments.
"""
from __future__ import annotations

import asyncio
import time
import uuid

import pytest

import routers.chat as chat_router
from main import app
from services.chat import contract as c
from services.chat.store import SqlChatRepository
from tests.test_chat_api import as_user, create_chat, names, offline, parse_sse  # noqa: F401

pytestmark = pytest.mark.asyncio


class AsgiCall:
    """One request made straight against the ASGI app, with every sent message
    timestamped as it is produced."""

    def __init__(self, method: str, path: str, headers: dict[str, str], body: bytes = b""):
        self.sent: asyncio.Queue = asyncio.Queue()
        self.disconnect = asyncio.Event()
        self._body = body
        self._served = False
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "path": path,
            "raw_path": path.encode(), "query_string": b"", "root_path": "", "scheme": "http",
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
            "client": ("127.0.0.1", 50000), "server": ("testserver", 80),
        }
        self.task = asyncio.create_task(app(scope, self._receive, self._send))

    async def _receive(self):
        if not self._served:
            self._served = True
            return {"type": "http.request", "body": self._body, "more_body": False}
        await self.disconnect.wait()
        return {"type": "http.disconnect"}

    async def _send(self, message):
        await self.sent.put((time.monotonic(), message))

    async def next(self, timeout: float = 10.0):
        return await asyncio.wait_for(self.sent.get(), timeout)

    async def read_all(self, timeout: float = 20.0):
        """(status, headers, [(t, chunk)...]) once the response completes."""
        status, headers, chunks = None, {}, []
        while True:
            t, message = await self.next(timeout)
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = {k.decode().lower(): v.decode() for k, v in message["headers"]}
            elif message["type"] == "http.response.body":
                if message.get("body"):
                    chunks.append((t, message["body"]))
                if not message.get("more_body"):
                    return status, headers, chunks


@pytest.fixture()
def fast_ticks(monkeypatch):
    monkeypatch.setattr(chat_router, "STREAM_CHECK_INTERVAL_SECONDS", 0.05)


async def test_stream_check_sends_five_ticks_with_the_streaming_headers(api_client, seeded, fast_ticks):
    resp = await api_client.get("/api/chat/stream-check", headers=as_user(seeded.user))
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers["cache-control"] == "no-cache, no-transform"
    assert resp.headers["x-accel-buffering"] == "no"

    events = parse_sse(resp.text)
    assert names(events) == ["tick"] * c.STREAM_CHECK_EVENTS == ["tick"] * 5
    assert [e["seq"] for _, e in events] == [1, 2, 3, 4, 5]
    assert {e["total"] for _, e in events} == {5}
    elapsed = [e["elapsed_ms"] for _, e in events]
    assert elapsed[0] < 40 and elapsed == sorted(elapsed) and elapsed[-1] >= 4 * 40  # spaced by the interval
    for frame in resp.text.strip().split("\n\n"):
        assert frame.startswith("event: tick\ndata: {") and "\n" not in frame.split("data: ", 1)[1]


async def test_stream_check_default_spacing_is_about_four_seconds():
    assert c.STREAM_CHECK_EVENTS == 5 and c.STREAM_CHECK_INTERVAL_SECONDS == 4.0
    assert chat_router.STREAM_CHECK_INTERVAL_SECONDS == 4.0
    assert c.SSE_HEARTBEAT_SECONDS == 15 and chat_router.heartbeat_seconds() == 15.0


async def test_stream_check_is_routed_before_the_session_id_route(api_client, seeded, fast_ticks):
    resp = await api_client.get("/api/chat/stream-check", headers=as_user(seeded.user))
    assert resp.status_code == 200  # not a 422 "stream-check is not a UUID"
    other = await api_client.get(f"/api/chat/{uuid.uuid4()}", headers=as_user(seeded.user))
    assert other.status_code == 404


async def test_stream_check_needs_a_session(api_client, seeded, fast_ticks, monkeypatch):
    import auth

    monkeypatch.setattr(auth, "AUTH_ENABLED", True)
    monkeypatch.setattr(auth, "_verify", lambda token: {"sub": f"clerk_{token}", "email": token})
    assert (await api_client.get("/api/chat/stream-check")).status_code == 401
    ok = await api_client.get("/api/chat/stream-check", headers={"Authorization": f"Bearer {seeded.user.email}"})
    assert ok.status_code == 200 and len(parse_sse(ok.text)) == 5


async def test_ticks_leave_the_app_one_at_a_time_not_in_one_buffered_burst(seeded, monkeypatch):
    monkeypatch.setattr(chat_router, "STREAM_CHECK_INTERVAL_SECONDS", 0.15)
    call = AsgiCall("GET", "/api/chat/stream-check", as_user(seeded.user))
    status, headers, chunks = await call.read_all()
    await call.task
    assert status == 200 and headers["x-accel-buffering"] == "no"
    assert len(chunks) == 5  # one body message per event
    gaps = [chunks[i + 1][0] - chunks[i][0] for i in range(4)]
    assert all(g >= 0.1 for g in gaps), gaps  # each tick left ~interval after the previous one
    assert chunks[-1][0] - chunks[0][0] >= 0.5


async def test_an_idle_turn_stream_carries_ping_comments_and_survives_a_disconnect(api_client, seeded, offline, monkeypatch):
    """While the model is thinking the stream emits ': ping' heartbeats; hanging
    up mid-turn does not stop the turn."""
    from services.chat import mock_model

    monkeypatch.setenv("CHAT_SSE_HEARTBEAT_SECONDS", "0.05")
    gate = asyncio.Event()
    original = mock_model.MockStream.__aenter__

    async def slow_enter(self):
        await gate.wait()
        return await original(self)

    monkeypatch.setattr(mock_model.MockStream, "__aenter__", slow_enter)
    made = await create_chat(api_client, seeded.user)
    sid = made["id"]
    body = ('{"text": "A bioequivalence study of metformin", "client_turn_id": "hb-1"}').encode()
    call = AsgiCall(
        "POST", f"/api/chat/{sid}/turns", {**as_user(seeded.user), "content-type": "application/json"}, body,
    )
    seen: list[bytes] = []
    start = await call.next()
    assert start[1]["type"] == "http.response.start" and start[1]["status"] == 200
    while len([b for b in seen if b == c.SSE_PING.encode()]) < 2:
        _t, message = await call.next(5.0)
        if message["type"] == "http.response.body" and message.get("body"):
            seen.append(message["body"])
    assert c.SSE_PING.encode() in seen  # idle: heartbeats, no events yet
    assert not any(b.startswith(b"event: turn_end") for b in seen)

    call.disconnect.set()  # the browser goes away
    gate.set()
    repo = SqlChatRepository()
    for _ in range(300):
        await asyncio.sleep(0.05)
        if not await repo.is_turn_locked(sid):
            break
    assert not await repo.is_turn_locked(sid)
    messages = await repo.list_messages(sid)
    assert [m.role.value for m in messages if m.role.value in ("user", "assistant")][:2] == ["user", "assistant"]
    call.task.cancel()
    try:
        await call.task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
