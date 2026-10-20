"""The fork localizes /help output to Russian."""

from __future__ import annotations

from nanobot.command.builtin import (
    BUILTIN_COMMAND_SPECS,
    HELP_DESCRIPTIONS_RU,
    build_help_text,
)


def test_help_text_header_is_russian() -> None:
    text = build_help_text()
    assert text.startswith("🐈 Команды nanobot:")


def test_every_builtin_command_has_russian_description() -> None:
    missing = [
        spec.command
        for spec in BUILTIN_COMMAND_SPECS
        if spec.command not in HELP_DESCRIPTIONS_RU
    ]
    assert missing == [], f"commands without Russian help text: {missing}"


def test_no_stale_russian_descriptions() -> None:
    known = {spec.command for spec in BUILTIN_COMMAND_SPECS}
    stale = [command for command in HELP_DESCRIPTIONS_RU if command not in known]
    assert stale == [], f"Russian help entries for removed commands: {stale}"


def test_help_text_lists_every_command_with_args_hint() -> None:
    text = build_help_text()
    for spec in BUILTIN_COMMAND_SPECS:
        line = spec.command if not spec.arg_hint else f"{spec.command} {spec.arg_hint}"
        assert line in text
        assert HELP_DESCRIPTIONS_RU[spec.command] in text
