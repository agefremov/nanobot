"""Session reset command behavior (/new)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import GenerationSettings, LLMResponse
from nanobot.webui.session_identity import channel_session_name


@pytest.fixture
async def loop(tmp_path):
    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings(max_tokens=100)
    provider.can_resume_conversation_state.return_value = True
    provider.chat_stream_with_retry = AsyncMock(return_value=LLMResponse(
        content="archived",
        finish_reason="stop",
    ))
    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=tmp_path,
        model="test-model",
        context_window_tokens=128_000,
    )
    loop.tools.get_definitions = MagicMock(return_value=[])
    try:
        yield loop
    finally:
        await loop.aclose()


def _archive_keys(loop: AgentLoop, prefix: str) -> list[str]:
    return sorted(
        item["key"]
        for item in loop.sessions._store.list_sessions()  # pyright: ignore[reportPrivateUsage]
        if item["key"].startswith(f"{prefix}:archive:")
    )


@pytest.mark.asyncio
async def test_new_archives_channel_dialog_and_clears_the_session(loop) -> None:
    session = loop.sessions.get_or_create("telegram:123")
    session.add_message("user", "старый вопрос")
    session.add_message("assistant", "старый ответ")
    loop.sessions.save(session)

    msg = InboundMessage(channel="telegram", sender_id="user", chat_id="123", content="/new")
    await loop._process_message(msg, runtime=loop.llm_runtime())  # pyright: ignore[reportPrivateUsage]

    archives = _archive_keys(loop, "telegram:123")
    assert len(archives) == 1
    archived = loop.sessions.read_session_file(archives[0])
    assert archived is not None
    assert [message["content"] for message in archived["messages"]] == [
        "старый вопрос",
        "старый ответ",
    ]
    assert archived["metadata"]["archived_from"] == "telegram:123"

    loop.sessions.invalidate("telegram:123")
    reloaded = loop.sessions.get_or_create("telegram:123")
    assert reloaded.messages == []


@pytest.mark.asyncio
async def test_new_keeps_channel_session_visible_as_archived_topic(loop) -> None:
    session = loop.sessions.get_or_create("telegram:123")
    session.add_message("user", "вопрос")
    loop.sessions.save(session)

    msg = InboundMessage(channel="telegram", sender_id="user", chat_id="123", content="/new")
    await loop._process_message(msg, runtime=loop.llm_runtime())  # pyright: ignore[reportPrivateUsage]

    archives = _archive_keys(loop, "telegram:123")
    assert len(archives) == 1
    # The archive key stays a foreign channel session: it is listed read-only
    # in the WebUI channel section and keeps the "telegram" grouping.
    assert channel_session_name(archives[0]) == "telegram"


@pytest.mark.asyncio
async def test_new_does_not_archive_non_channel_sessions(loop) -> None:
    session = loop.sessions.get_or_create("cli:test")
    session.add_message("user", "вопрос")
    loop.sessions.save(session)

    msg = InboundMessage(channel="cli", sender_id="user", chat_id="test", content="/new")
    await loop._process_message(msg, runtime=loop.llm_runtime())  # pyright: ignore[reportPrivateUsage]

    assert _archive_keys(loop, "cli:test") == []
