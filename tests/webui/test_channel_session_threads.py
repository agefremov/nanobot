from __future__ import annotations

from nanobot.webui.transcript import (
    build_session_messages_thread_response,
    session_message_transcript_rows,
)


def _messages() -> list[dict]:
    return [
        {"role": "user", "content": "сделай файл", "timestamp": "2026-10-18T10:00:00"},
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": "thinking",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "write_file",
                        "arguments": '{"path": "projects/x/a.txt", "content": "hi"}',
                    },
                },
                {
                    "id": "call-2",
                    "type": "function",
                    "function": {
                        "name": "apply_patch",
                        "arguments": '{"edits": [{"path": "b.py"}, {"path": "b.py"}]}',
                    },
                },
                {
                    "id": "call-3",
                    "type": "function",
                    "function": {"name": "exec", "arguments": '{"command": "ls"}'},
                },
            ],
            "timestamp": "2026-10-18T10:00:05",
        },
        {"role": "tool", "tool_call_id": "call-1", "name": "write_file", "content": "ok"},
        {"role": "tool", "tool_call_id": "call-2", "name": "apply_patch", "content": "ok"},
        {"role": "tool", "tool_call_id": "call-3", "name": "exec", "content": "ok"},
        {
            "role": "assistant",
            "content": "готово",
            "timestamp": "2026-10-18T10:00:10",
        },
    ]


def test_session_message_rows_project_user_turns_and_file_edits() -> None:
    rows = session_message_transcript_rows("max:451041899", _messages())

    assert [row["event"] for row in rows] == ["user", "file_edit", "message"]
    user_row, edit_row, answer_row = rows
    assert user_row["chat_id"] == "451041899"
    assert user_row["text"] == "сделай файл"
    assert user_row["created_at_ms"] > 0
    assert answer_row["text"] == "готово"

    edits = edit_row["edits"]
    assert [(edit["tool"], edit["path"], edit["status"]) for edit in edits] == [
        ("write_file", "projects/x/a.txt", "done"),
        ("apply_patch", "b.py", "done"),
    ]
    # Pending calls (without a tool result) surface as still-editing rows.
    pending_rows = session_message_transcript_rows(
        "max:451041899",
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-9",
                        "type": "function",
                        "function": {
                            "name": "edit_file",
                            "arguments": '{"path": "c.md"}',
                        },
                    }
                ],
            }
        ],
    )
    assert pending_rows[0]["event"] == "file_edit"
    assert pending_rows[0]["edits"][0]["status"] == "editing"


def test_session_message_rows_skip_hidden_history_and_empty_assistants() -> None:
    messages = [
        {"role": "user", "content": "hi", "_hidden_history": True},
        {"role": "assistant", "content": "", "tool_calls": []},
        {"role": "user", "content": "real question"},
    ]
    rows = session_message_transcript_rows("telegram:1", messages)
    assert [row["event"] for row in rows] == ["user"]
    assert rows[0]["text"] == "real question"


def test_build_session_messages_thread_response_shape() -> None:
    data = build_session_messages_thread_response("max:451041899", _messages())

    assert data is not None
    assert data["sessionKey"] == "max:451041899"
    assert data["schemaVersion"] >= 1
    assert data["has_pending_tool_calls"] is False
    assert data["active_turn_id"] is None
    assert data["page"] == {
        "before_cursor": None,
        "has_more_before": False,
        "loaded_message_count": len(data["messages"]),
        "user_message_offset": 0,
    }
    contents = [
        message.get("content")
        for message in data["messages"]
        if message.get("role") in {"user", "assistant"}
    ]
    assert "сделай файл" in contents
    assert "готово" in contents
    assert any(message.get("fileEdits") for message in data["messages"])


def test_build_session_messages_thread_response_empty_session() -> None:
    assert build_session_messages_thread_response("telegram:1", []) is None
