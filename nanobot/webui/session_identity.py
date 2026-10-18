"""Stable mapping between public WebUI chat IDs and persisted session keys."""

from __future__ import annotations

import re
from typing import Any, TypeGuard

WEBUI_SESSION_STORAGE_PREFIX = "websocket:"
_WEBUI_CHAT_ID_RE = re.compile(r"^[A-Za-z0-9_:-]{1,64}$")


def is_valid_webui_chat_id(value: Any) -> TypeGuard[str]:
    """Validate the compact chat IDs accepted by the WebUI protocol."""
    return isinstance(value, str) and _WEBUI_CHAT_ID_RE.fullmatch(value) is not None


def webui_session_key(chat_id: str) -> str:
    """Return the backward-compatible persisted key for a WebUI chat."""
    return f"{WEBUI_SESSION_STORAGE_PREFIX}{chat_id}"


def is_webui_session_key(session_key: str) -> bool:
    """Return whether *session_key* belongs to the WebUI session namespace."""
    return session_key.startswith(WEBUI_SESSION_STORAGE_PREFIX)


def webui_chat_id(session_key: str) -> str | None:
    """Extract a non-empty WebUI chat ID from a persisted session key."""
    if not is_webui_session_key(session_key):
        return None
    chat_id = session_key.removeprefix(WEBUI_SESSION_STORAGE_PREFIX)
    return chat_id or None


# Session namespaces owned by internal agents, not by a user-facing channel.
_INTERNAL_SESSION_CHANNELS: frozenset[str] = frozenset({
    "websocket",
    "api",
    "dream",
    "unified",
    "cli",
})
_CHANNEL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def channel_session_name(session_key: str) -> str | None:
    """Return the channel name for a foreign channel session such as ``telegram:1``.

    Returns ``None`` for WebUI sessions and internal namespaces (``api:``,
    ``dream:``, ``unified:``, ``cli:``), which must stay invisible to the
    WebUI channel-topic listing.
    """
    if not isinstance(session_key, str) or is_webui_session_key(session_key):
        return None
    channel, sep, rest = session_key.partition(":")
    if not sep or not rest or not _CHANNEL_NAME_RE.fullmatch(channel):
        return None
    if channel in _INTERNAL_SESSION_CHANNELS:
        return None
    return channel
