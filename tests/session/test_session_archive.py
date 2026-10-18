"""Read-only session archives created by ``/new``."""

from nanobot.session.manager import SessionManager


def test_create_session_archive_copies_messages_and_marks_metadata(tmp_path) -> None:
    manager = SessionManager(tmp_path)
    archive = manager.create_session_archive(
        "telegram:1269920255",
        [
            {"role": "user", "content": "старый вопрос", "timestamp": "2026-10-18T12:00:00"},
            {"role": "assistant", "content": "старый ответ", "timestamp": "2026-10-18T12:00:01"},
        ],
        metadata={
            "title": "live title",
            "title_user_edited": True,
            "goal_state": {"phase": "active"},
            "_compaction_route": "telegram:1269920255",
        },
    )

    assert archive is not None
    assert archive.key.startswith("telegram:1269920255:archive:")
    assert [message["content"] for message in archive.messages] == [
        "старый вопрос",
        "старый ответ",
    ]
    assert archive.metadata["archived"] is True
    assert archive.metadata["archived_from"] == "telegram:1269920255"
    assert archive.metadata["title"].startswith("Архив от ")
    assert "goal_state" not in archive.metadata
    assert "title_user_edited" not in archive.metadata
    assert "_compaction_route" not in archive.metadata

    stored = manager.read_session_file(archive.key)
    assert stored is not None
    assert len(stored["messages"]) == 2
    assert stored["metadata"]["archived_from"] == "telegram:1269920255"


def test_create_session_archive_preserves_created_at(tmp_path) -> None:
    manager = SessionManager(tmp_path)
    source = manager.get_or_create("telegram:1")
    source.add_message("user", "вопрос")
    manager.save(source)

    archive = manager.create_session_archive(
        "telegram:1",
        source.messages,
        created_at=source.created_at,
    )

    assert archive is not None
    assert archive.created_at == source.created_at


def test_create_session_archive_drops_command_messages(tmp_path) -> None:
    manager = SessionManager(tmp_path)

    archive = manager.create_session_archive(
        "max:451041899",
        [
            {"role": "user", "content": "/new", "_command": True},
            {"role": "user", "content": "останусь"},
        ],
    )

    assert archive is not None
    assert [message["content"] for message in archive.messages] == ["останусь"]


def test_create_session_archive_keeps_keys_unique_within_one_second(tmp_path) -> None:
    manager = SessionManager(tmp_path)
    messages = [{"role": "user", "content": "вопрос"}]

    first = manager.create_session_archive("telegram:1", messages)
    second = manager.create_session_archive("telegram:1", messages)

    assert first is not None
    assert second is not None
    assert first.key != second.key
    assert second.key.startswith(first.key)


def test_create_session_archive_without_messages_returns_none(tmp_path) -> None:
    manager = SessionManager(tmp_path)

    assert manager.create_session_archive("telegram:1", []) is None
    assert manager.create_session_archive(
        "telegram:1",
        [{"role": "user", "content": "/new", "_command": True}],
    ) is None
