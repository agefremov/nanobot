from nanobot.webui.session_identity import (
    WEBUI_SESSION_STORAGE_PREFIX,
    channel_session_name,
    is_valid_webui_chat_id,
    is_webui_session_key,
    webui_chat_id,
    webui_session_key,
)


def test_webui_session_identity_preserves_persisted_wire_compatibility() -> None:
    assert WEBUI_SESSION_STORAGE_PREFIX == "websocket:"
    assert webui_session_key("chat-1") == "websocket:chat-1"
    assert is_webui_session_key("websocket:chat-1")
    assert webui_chat_id("websocket:chat-1") == "chat-1"
    assert webui_chat_id("websocket: chat-1") == " chat-1"
    assert webui_chat_id("websocket:") is None
    assert webui_chat_id("telegram:chat-1") is None


def test_webui_chat_id_validation_is_protocol_scoped() -> None:
    assert is_valid_webui_chat_id("unified:default")
    assert is_valid_webui_chat_id("x" * 64)
    assert not is_valid_webui_chat_id("x" * 65)
    assert not is_valid_webui_chat_id("../escape")


def test_channel_session_name_detects_channel_sessions() -> None:
    assert channel_session_name("telegram:1269920255") == "telegram"
    assert channel_session_name("max:451041899") == "max"
    assert channel_session_name("telegram:1:topic:5") == "telegram"
    assert channel_session_name("slack:C123:42") == "slack"


def test_channel_session_name_excludes_webui_and_internal_namespaces() -> None:
    assert channel_session_name("websocket:chat-1") is None
    assert channel_session_name("api:default") is None
    assert channel_session_name("dream:20261015-125757") is None
    assert channel_session_name("unified:default") is None
    assert channel_session_name("cli:direct") is None
    assert channel_session_name("telegram:") is None
    assert channel_session_name("telegram") is None
    assert channel_session_name("bad channel:1") is None
    assert channel_session_name("") is None
