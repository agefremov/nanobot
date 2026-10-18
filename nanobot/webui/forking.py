"""WebUI chat fork orchestration."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Protocol, cast

from loguru import logger

from nanobot.session.history_visibility import is_hidden_history_message
from nanobot.session.manager import SessionManager
from nanobot.session.webui_turns import WEBUI_TITLE_METADATA_KEY, clean_generated_title
from nanobot.webui.session_identity import (
    channel_session_name,
    is_valid_webui_chat_id,
    webui_session_key,
)
from nanobot.webui.transcript import (
    append_fork_marker,
    delete_webui_transcript,
    fork_transcript_before_user_index,
    write_session_messages_as_transcript,
)

if TYPE_CHECKING:
    from websockets.asyncio.server import ServerConnection

    from nanobot.webui.gateway_services import GatewayServices


class WebUIForkHost(Protocol):
    gateway: GatewayServices

    async def send_webui_protocol_error(
        self,
        connection: ServerConnection,
        detail: str,
    ) -> None: ...

    async def attach_webui_fork(
        self,
        connection: ServerConnection,
        *,
        fork_id: str,
        fork_key: str,
    ) -> None: ...


def create_webui_chat_fork(
    session_manager: SessionManager,
    *,
    source_chat_id: str,
    before_user_index: int,
    title: str | None = None,
) -> tuple[str, str] | None:
    """Return ``(chat_id, session_key)`` for a new fork, or ``None`` for bad input."""
    new_id = str(uuid.uuid4())
    source_key = webui_session_key(source_chat_id)
    target_key = webui_session_key(new_id)
    try:
        forked = session_manager.fork_session_before_user_index(
            source_key,
            target_key,
            before_user_index,
        )
        if forked is None:
            return None

        transcript_ok = fork_transcript_before_user_index(
            source_key,
            target_key,
            before_user_index,
        )
        if not transcript_ok:
            write_session_messages_as_transcript(target_key, forked.messages)
        append_fork_marker(target_key)

        fork_title = clean_generated_title(title)
        if fork_title:
            forked.metadata[WEBUI_TITLE_METADATA_KEY] = fork_title
            session_manager.save(forked, fsync=True)
    except Exception:
        delete_webui_transcript(target_key)
        session_manager.delete_session(target_key)
        raise
    return new_id, target_key


def _first_visible_user_text(messages: list[dict[str, Any]]) -> str:
    for message in messages:
        if message.get("role") != "user" or is_hidden_history_message(message):
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
    return ""


def create_webui_channel_fork(
    session_manager: SessionManager,
    *,
    source_key: str,
    title: str | None = None,
) -> tuple[str, str] | None:
    """Fork a channel session into a new WebUI chat with the full history.

    Used when the user continues a read-only channel topic from the WebUI:
    the source stays untouched, the fork owns its own transcript and never
    inherits the channel routing metadata.
    """
    if channel_session_name(source_key) is None:
        return None
    source_data = session_manager.read_session_file(source_key)
    raw_messages = source_data.get("messages") if isinstance(source_data, dict) else None
    if not isinstance(raw_messages, list):
        return None
    messages = [
        cast(dict[str, Any], item)
        for item in cast(list[Any], raw_messages)
        if isinstance(item, dict)
    ]
    user_count = sum(
        1
        for message in messages
        if message.get("role") == "user" and not is_hidden_history_message(message)
    )
    new_id = str(uuid.uuid4())
    target_key = webui_session_key(new_id)
    try:
        forked = session_manager.fork_session_before_user_index(
            source_key,
            target_key,
            user_count,
        )
        if forked is None:
            return None
        # A WebUI-owned fork must not route compaction summaries back to the
        # source channel chat.
        forked.metadata.pop("_compaction_route", None)
        write_session_messages_as_transcript(target_key, forked.messages)
        append_fork_marker(target_key)
        fork_title = clean_generated_title(title)
        if not fork_title:
            fork_title = clean_generated_title(_first_visible_user_text(messages))
        if fork_title:
            forked.metadata[WEBUI_TITLE_METADATA_KEY] = fork_title
        session_manager.save(forked, fsync=True)
    except Exception:
        delete_webui_transcript(target_key)
        session_manager.delete_session(target_key)
        raise
    return new_id, target_key


async def handle_webui_fork_chat(
    channel: WebUIForkHost,
    connection: ServerConnection,
    envelope: Mapping[str, Any],
) -> None:
    """Handle the WebUI ``fork_chat`` websocket command.

    ``websocket.py`` owns the transport. This module owns WebUI fork semantics:
    validate the request, clone session/transcript state, attach the new chat,
    and hydrate the client.
    """
    source_chat_id = envelope.get("source_chat_id")
    source_session_key = envelope.get("source_session_key")
    raw_index = envelope.get("before_user_index")
    title = envelope.get("title") if isinstance(envelope.get("title"), str) else None

    if source_session_key is not None:
        if not isinstance(source_session_key, str) or channel_session_name(source_session_key) is None:
            await channel.send_webui_protocol_error(connection, "invalid source_session_key")
            return
    else:
        if not is_valid_webui_chat_id(source_chat_id):
            await channel.send_webui_protocol_error(connection, "invalid source_chat_id")
            return
        if isinstance(raw_index, bool) or not isinstance(raw_index, int) or raw_index < 0:
            await channel.send_webui_protocol_error(connection, "invalid before_user_index")
            return

    session_manager = channel.gateway.session_manager
    if session_manager is None:
        await channel.send_webui_protocol_error(connection, "session_manager_unavailable")
        return

    try:
        if source_session_key is not None:
            forked = create_webui_channel_fork(
                session_manager,
                source_key=source_session_key,
                title=title,
            )
            if forked is None:
                await channel.send_webui_protocol_error(connection, "invalid fork source or index")
                return
        else:
            forked = create_webui_chat_fork(
                session_manager,
                source_chat_id=cast(str, source_chat_id),
                before_user_index=cast(int, raw_index),
                title=title,
            )
            if forked is None:
                await channel.send_webui_protocol_error(connection, "invalid fork source or index")
                return
        fork_id, fork_key = forked
    except Exception as exc:
        logger.warning("fork_chat failed: {}", exc)
        await channel.send_webui_protocol_error(connection, "fork_chat_failed")
        return

    await channel.attach_webui_fork(
        connection,
        fork_id=fork_id,
        fork_key=fork_key,
    )
