"""Session-key decoding for ``/api/sessions/<key>/...`` routes.

Channel session keys embed chat addresses (``email:user@example.com``), so the
path-key validator must accept them or read-only channel topics 400 out.
"""

from __future__ import annotations

from nanobot.webui.ws_http import _decode_api_key


def test_decode_api_key_accepts_channel_keys_with_chat_addresses() -> None:
    assert _decode_api_key("email:a.g.efremov@yandex.ru") == "email:a.g.efremov@yandex.ru"
    assert _decode_api_key("email%3Aa.g.efremov%40yandex.ru") == "email:a.g.efremov@yandex.ru"
    assert _decode_api_key("whatsapp:+79991234567") == "whatsapp:+79991234567"
    assert _decode_api_key("matrix:@user:example.org") == "matrix:@user:example.org"
    assert _decode_api_key("telegram:1269920255") == "telegram:1269920255"
    assert _decode_api_key("max:451041899:archive:20261018-131458") == (
        "max:451041899:archive:20261018-131458"
    )


def test_decode_api_key_accepts_unicode_chat_addresses() -> None:
    assert _decode_api_key("email:андрей@почта.рф") == "email:андрей@почта.рф"


def test_decode_api_key_rejects_unsafe_keys() -> None:
    assert _decode_api_key("../etc/passwd") is None
    assert _decode_api_key("a%2Fb") is None  # decoded "/" must not pass
    assert _decode_api_key("a%00b") is None  # decoded NUL must not pass
    assert _decode_api_key("a b") is None
    assert _decode_api_key("") is None
    assert _decode_api_key("x" * 129) is None
