"""Live status message (👀 reaction substitute) in the MAX channel.

MAX has no message reactions; the channel emulates the Telegram-style UX with
one lifecycle message: sent as 👀 shortly after the message is read, edited
with progress while the turn runs, deleted (or frozen as a bare 👀) when the
reply lands.
"""

import asyncio

import httpx

from nanobot.bus.events import OutboundMessage
from nanobot.bus.outbound_events import ProgressEvent
from nanobot.bus.queue import MessageBus
from nanobot.channels.max import runtime as max_runtime
from nanobot.channels.max.runtime import MaxChannel


class FakeResponse:
    def __init__(self, payload=None, status_code=200):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("POST", "https://example.test/")
            raise httpx.HTTPStatusError(
                "boom", request=request, response=httpx.Response(self.status_code)
            )


class FakeClient:
    """Records REST calls and answers POST /messages with a fresh mid."""

    def __init__(self, mid="mid.status"):
        self.calls: list[tuple] = []
        self.mid = mid

    def _record(self, method, path, params, json):
        self.calls.append((method, path, params, json))

    async def post(self, path, params=None, json=None):
        self._record("post", path, params, json)
        return FakeResponse({"message": {"body": {"mid": self.mid}}})

    async def put(self, path, params=None, json=None):
        self._record("put", path, params, json)
        return FakeResponse({})

    async def delete(self, path, params=None, json=None):
        self._record("delete", path, params, json)
        return FakeResponse({})


def make_channel(**config):
    channel = MaxChannel({"token": "t", **config}, MessageBus())
    channel._running = True
    channel._client = FakeClient()
    channel._routes["42"] = ("chat_id", "42")
    return channel, channel._client


def fast_status(monkeypatch, *, create_delay=0.01, edit_interval=0.0):
    monkeypatch.setattr(max_runtime, "_STATUS_CREATE_DELAY_S", create_delay)
    monkeypatch.setattr(max_runtime, "_STATUS_MIN_EDIT_INTERVAL_S", edit_interval)


async def wait_for(predicate, timeout=2.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


async def test_status_created_then_deleted_on_reply(monkeypatch):
    fast_status(monkeypatch)
    channel, client = make_channel()
    channel._start_status("42", "42", origin_mid="mid.user")
    await wait_for(lambda: any(c[0] == "post" for c in client.calls))

    post = next(c for c in client.calls if c[0] == "post")
    assert post[3]["link"] == {"type": "reply", "mid": "mid.user"}
    assert post[3]["text"].startswith("👀")

    await channel.send(OutboundMessage(channel="max", chat_id="42", content="ответ"))
    assert ("delete", "/messages", {"message_id": "mid.status"}, None) in client.calls


async def test_quick_reply_creates_no_status_message(monkeypatch):
    fast_status(monkeypatch, create_delay=0.05)
    channel, client = make_channel()
    channel._start_status("42", "42", origin_mid="mid.user")
    await channel.send(
        OutboundMessage(channel="max", chat_id="42", content="быстрый ответ")
    )
    await asyncio.sleep(0.1)

    posts = [c for c in client.calls if c[0] == "post"]
    assert posts == [("post", "/messages", {"chat_id": "42"},
                      {"text": "быстрый ответ", "format": "markdown"})]
    assert not [c for c in client.calls if c[0] in {"put", "delete"}]


async def test_progress_hint_folds_into_status(monkeypatch):
    fast_status(monkeypatch)
    channel, client = make_channel()
    channel._start_status("42", "42", origin_mid="mid.user")
    await wait_for(lambda: any(c[0] == "post" for c in client.calls))

    await channel.send(
        OutboundMessage(
            channel="max",
            chat_id="42",
            content="$ ls -la",
            event=ProgressEvent(content="$ ls -la", tool_hint=True),
        )
    )
    puts = [c for c in client.calls if c[0] == "put"]
    assert puts, "progress hint must edit the status message"
    assert "$ ls -la" in puts[-1][3]["text"]
    # Progress never posts separate chat messages.
    assert len([c for c in client.calls if c[0] == "post"]) == 1


async def test_status_freezes_as_emoji_when_kept(monkeypatch):
    fast_status(monkeypatch)
    channel, client = make_channel(status_delete_on_reply=False)
    channel._start_status("42", "42", origin_mid="mid.user")
    await wait_for(lambda: any(c[0] == "post" for c in client.calls))

    await channel.send(OutboundMessage(channel="max", chat_id="42", content="ответ"))
    assert not [c for c in client.calls if c[0] == "delete"]
    puts = [c for c in client.calls if c[0] == "put"]
    assert puts and puts[-1][3] == {"text": "👀"}


async def test_progress_defaults_follow_status_switch():
    on, _ = make_channel()
    assert on.progress_transport_defaults() == (False, True)
    off, _ = make_channel(status_message=False)
    assert off.progress_transport_defaults() == (False, False)
