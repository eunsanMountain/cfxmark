"""Public conversion APIs expose only cfxmark errors for invalid text input."""

from __future__ import annotations

from collections.abc import Callable

import pytest

import cfxmark
import cfxmark.api as api


def _to_jira_wiki_from_markdown(source: str) -> object:
    return cfxmark.to_jira_wiki(source, input_format="markdown")


def _to_jira_wiki_from_storage(source: str) -> object:
    return cfxmark.to_jira_wiki(source, input_format="xhtml")


def _resolve_storage_migrations(source: str) -> object:
    return cfxmark.resolve_storage_migrations(source, {})


PUBLIC_TEXT_ENTRYPOINTS: tuple[tuple[str, Callable[[str], object]], ...] = (
    ("to_cfx", cfxmark.to_cfx),
    ("to_cfx_artifact", cfxmark.to_cfx_artifact),
    ("to_md", cfxmark.to_md),
    ("to_md_artifact", cfxmark.to_md_artifact),
    ("to_jira_wiki_markdown", _to_jira_wiki_from_markdown),
    ("to_jira_wiki_storage", _to_jira_wiki_from_storage),
    ("from_jira_wiki", cfxmark.from_jira_wiki),
    ("resolve_storage_migrations", _resolve_storage_migrations),
)


@pytest.mark.parametrize(
    ("name", "entrypoint"), PUBLIC_TEXT_ENTRYPOINTS, ids=lambda value: value if isinstance(value, str) else None
)
@pytest.mark.parametrize(("source", "codepoint"), (("\ud800", "D800"), ("\x01", "0001")))
def test_public_conversion_rejects_xml_incompatible_text_as_parse_error(
    name: str,
    entrypoint: Callable[[str], object],
    source: str,
    codepoint: str,
) -> None:
    del name

    with pytest.raises(cfxmark.ParseError, match=rf"XML-incompatible.*U\+{codepoint}.*index 0"):
        entrypoint(source)


def test_public_conversion_does_not_hide_programmer_value_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_parser(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise ValueError("programmer bug")

    monkeypatch.setattr(api, "parse_md", broken_parser)

    with pytest.raises(ValueError, match="programmer bug"):
        cfxmark.to_cfx("valid input")
