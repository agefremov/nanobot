"""Slash-command support in the MAX channel.

The channel mirrors the Telegram UX: a client-side command menu
(`PATCH /me/commands`, analog of `set_my_commands`), a `/start` greeting, and
underscore-alias normalization for commands typed with Telegram muscle memory
(`/dream_log` → `/dream-log`).
"""

import httpx
import pytest

from nanobot.bus.queue import MessageBus
from nanobot.channels import base
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
            request = httpx.Request("PATCH", "https://example.test/")
            raise httpx.HTTPStatusError(
                "boom", request=request, response=httpx.Response(self.status_code)
            )


class FakeClient:
    """Records REST calls; PATCH can be forced to fail."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.fail_patch = False

    async def post(self, path, params=None, json=None):
        self.calls.append(("post", path, params, json))
        return FakeResponse({"message": {"body": {"mid": "mid.1"}}})

    async def patch(self, path, params=None, json=None):
        self.calls.append(("patch", path, params, json))
        return FakeResponse({}, status_code=400 if self.fail_patch else 200)


def make_channel(**config):
    channel = MaxChannel(
        {
            "token": "t",
            "mark_seen": False,
            "typing_indicator": False,
            "status_message": False,
            **config,
        },
        MessageBus(),
    )
    channel._running = True
    channel._client = FakeClient()
    return channel, channel._client


def message_update(text: str) -> dict:
    return {
        "update_type": "message_created",
        "message": {
            "body": {"text": text, "mid": "mid.1", "seq": 1, "attachments": []},
            "sender": {"user_id": 42, "first_name": "Андрей"},
            "recipient": {"chat_type": "dialog", "chat_id": "42"},
        },
    }


async def capture_agent_messages(channel, monkeypatch) -> list[dict]:
    """Replace `_handle_message` with a recorder; returns the recorded calls."""
    handled: list[dict] = []

    async def record(*args, **kwargs):
        handled.append(kwargs)

    monkeypatch.setattr(channel, "_handle_message", record)
    return handled


def posts(client):
    return [c for c in client.calls if c[0] == "post" and c[1] == "/messages"]


# ----------------------------------------------------------------------
# Command menu (PATCH /me/commands)
# ----------------------------------------------------------------------


async def test_register_commands_publishes_menu():
    channel, client = make_channel()
    await channel._register_commands()

    assert client.calls == [
        ("patch", "/me/commands", None, {"commands": max_runtime.MAX_BOT_COMMANDS})
    ]
    commands = max_runtime.MAX_BOT_COMMANDS
    names = [item["name"] for item in commands]
    assert {"start", "help", "new", "dream-log", "evaluator-prompt"} <= set(names)
    # BotCommand schema limits: 32 items max, name <= 64, description <= 128.
    assert len(commands) <= 32
    assert all(1 <= len(item["name"]) <= 64 for item in commands)
    assert all(1 <= len(item["description"]) <= 128 for item in commands)


async def test_register_commands_failure_is_non_fatal():
    channel, client = make_channel()
    client.fail_patch = True
    await channel._register_commands()  # must not raise


# ----------------------------------------------------------------------
# /start greeting (mirror of Telegram's _on_start)
# ----------------------------------------------------------------------


@pytest.mark.parametrize("text", ["/start", "/start@AGEassistant_bot"])
async def test_start_replies_with_greeting_and_skips_agent(monkeypatch, text):
    channel, client = make_channel(allow_from=["42"])
    handled = await capture_agent_messages(channel, monkeypatch)

    await channel._process_update(message_update(text))

    assert handled == []
    sent = posts(client)
    assert len(sent) == 1
    body = sent[0][3]
    assert "Привет, Андрей!" in body["text"]
    assert "/help" in body["text"]


async def test_start_from_unpaired_sender_gets_pairing_code(monkeypatch):
    # No `_handle_message` mock here: the real base handler sends the code.
    monkeypatch.setattr(base, "is_approved", lambda *args, **kwargs: False)
    monkeypatch.setattr(base, "generate_code", lambda *args, **kwargs: "123456")
    channel, client = make_channel()

    await channel._process_update(message_update("/start"))

    sent = posts(client)
    assert len(sent) == 1
    assert "123456" in sent[0][3]["text"]


# ----------------------------------------------------------------------
# Underscore aliases
# ----------------------------------------------------------------------


async def test_underscore_alias_reaches_router_as_canonical_command(monkeypatch):
    channel, client = make_channel(allow_from=["42"])
    handled = await capture_agent_messages(channel, monkeypatch)

    await channel._process_update(message_update("/dream_log 3"))

    assert len(handled) == 1
    assert handled[0]["content"] == "/dream-log 3"
    assert client.calls == []


def test_normalize_max_command_maps_aliases():
    assert max_runtime._normalize_max_command("/dream_log 3") == "/dream-log 3"
    assert max_runtime._normalize_max_command("/dream_log") == "/dream-log"
    assert max_runtime._normalize_max_command("/dream-log 3") == "/dream-log 3"
    assert max_runtime._normalize_max_command("/new") == "/new"
    assert max_runtime._normalize_max_command("просто текст") == "просто текст"
    assert max_runtime._normalize_max_command("см. /dream_log") == "см. /dream_log"
