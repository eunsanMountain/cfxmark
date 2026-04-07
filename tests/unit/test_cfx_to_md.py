"""Confluence Storage XHTML → Markdown unit tests."""

from __future__ import annotations

import pytest

import cfxmark


def md(cfx: str) -> str:
    return cfxmark.to_md(cfx).markdown.rstrip("\n")


# ---------------------------------------------------------------------------
# Headings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("level", [1, 2, 3, 4, 5, 6])
def test_heading(level: int) -> None:
    assert md(f"<h{level}>Title</h{level}>") == "#" * level + " Title"


# ---------------------------------------------------------------------------
# Inline
# ---------------------------------------------------------------------------


def test_strong() -> None:
    assert md("<p><strong>bold</strong></p>") == "**bold**"


def test_emphasis() -> None:
    assert md("<p><em>italic</em></p>") == "*italic*"


def test_inline_code() -> None:
    assert md("<p><code>x</code></p>") == "`x`"


def test_link() -> None:
    assert (
        md('<p><a href="https://example.com">label</a></p>')
        == "[label](https://example.com)"
    )


# ---------------------------------------------------------------------------
# Lists
# ---------------------------------------------------------------------------


def test_bullet_list() -> None:
    assert md("<ul><li>a</li><li>b</li></ul>") == "- a\n- b"


def test_ordered_list() -> None:
    assert md("<ol><li>a</li><li>b</li></ol>") == "1. a\n2. b"


# ---------------------------------------------------------------------------
# Code macro
# ---------------------------------------------------------------------------


def test_code_macro_with_language() -> None:
    cfx = (
        '<ac:structured-macro ac:name="code">'
        '<ac:parameter ac:name="language">python</ac:parameter>'
        "<ac:plain-text-body>x = 1</ac:plain-text-body>"
        "</ac:structured-macro>"
    )
    assert md(cfx) == "```python\nx = 1\n```"


# ---------------------------------------------------------------------------
# Image
# ---------------------------------------------------------------------------


def test_image_attachment_with_dimensions_encoded() -> None:
    cfx = (
        '<ac:image ac:width="300" ac:height="200">'
        '<ri:attachment ri:filename="foo.png"/>'
        "</ac:image>"
    )
    out = md(f"<p>{cfx}</p>")
    assert "foo.png" in out
    assert "cfxmark:w=300,h=200" in out


# ---------------------------------------------------------------------------
# Opaque preservation
# ---------------------------------------------------------------------------


def test_unknown_macro_becomes_opaque() -> None:
    cfx = (
        '<ac:structured-macro ac:name="totally-unknown-macro">'
        '<ac:parameter ac:name="foo">bar</ac:parameter>'
        "</ac:structured-macro>"
    )
    out = md(cfx)
    assert "<!-- cfxmark:opaque" in out
    assert "```cfx-storage" in out
    assert "totally-unknown-macro" in out
