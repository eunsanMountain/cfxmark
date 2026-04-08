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


# ---------------------------------------------------------------------------
# CJK boundary fallback regression
#
# CommonMark's flanking-delimiter rule blocks ``**X**`` from being re-parsed
# as bold when both outer characters are "word" characters. CJK glyphs are
# all alphanumeric under Python's ``str.isalnum``, so every Korean / Chinese
# / Japanese character triggers the rule. ``to_md`` must emit raw
# ``<strong>`` HTML in those positions so the round trip survives — otherwise
# the asterisks would stay literal on re-parse and the user would see
# ``**볼드**`` on Confluence instead of **볼드**.
#
# These tests pin the behavior so refactors cannot silently flip the policy.
# ---------------------------------------------------------------------------


def test_strong_korean_word_flank_falls_back_to_html() -> None:
    # Both sides are Korean word characters → must use raw <strong>.
    assert (
        md("<p>한국어<strong>볼드</strong>입니다</p>")
        == "한국어<strong>볼드</strong>입니다"
    )


def test_strong_cjk_ideograph_flank_falls_back_to_html() -> None:
    # Chinese ideographs are also alphanumeric → same fallback applies.
    assert (
        md("<p>汉字<strong>粗体</strong>文本</p>")
        == "汉字<strong>粗体</strong>文本"
    )


def test_strong_with_spaces_around_korean_uses_plain_markdown() -> None:
    # Spaces on both sides break the word-char flank → plain ``**`` is safe.
    assert (
        md("<p>한국어 <strong>bold</strong> 한국어</p>")
        == "한국어 **bold** 한국어"
    )


def test_emphasis_korean_word_flank_falls_back_to_html() -> None:
    assert (
        md("<p>한국어<em>강조</em>입니다</p>")
        == "한국어<em>강조</em>입니다"
    )


def test_emphasis_pure_korean_no_flank_uses_plain_markdown() -> None:
    # Paragraph-only emphasis has empty outer chars → plain ``*`` works.
    assert md("<p><em>강조</em></p>") == "*강조*"


def test_strikethrough_korean_word_flank_falls_back_to_html() -> None:
    assert (
        md("<p>한국어<del>취소</del>입니다</p>")
        == "한국어<del>취소</del>입니다"
    )


def test_strong_cjk_boundary_inside_list_item() -> None:
    # Same policy must apply inside block containers (lists, headings …).
    assert (
        md("<ul><li>항목<strong>볼드</strong>끝</li></ul>")
        == "- 항목<strong>볼드</strong>끝"
    )


def test_strong_cjk_boundary_inside_heading() -> None:
    assert (
        md("<h2>제목<strong>강조</strong></h2>")
        == "## 제목<strong>강조</strong>"
    )


def test_strong_cjk_boundary_mixed_ascii_and_korean_flank() -> None:
    # Flank detection only looks at the adjacent character; mixed ASCII and
    # Korean on either side still counts as word-flank and triggers fallback.
    assert (
        md("<p>prefix한국어<strong>볼드</strong>suffix한국어</p>")
        == "prefix한국어<strong>볼드</strong>suffix한국어"
    )


# ---------------------------------------------------------------------------
# Renderer determinism guardrails
#
# The renderer is a pure function over the AST, so calling ``to_md`` multiple
# times on the same input MUST produce identical output. If these tests ever
# fail, the renderer has acquired an internal cache, random seed, or other
# context-dependent branch — investigate immediately.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "xhtml",
    [
        "<p>한국어<strong>볼드</strong>입니다</p>",
        "<p>汉字<strong>粗体</strong>文本</p>",
        "<p>한국어 <strong>bold</strong> 한국어</p>",
        "<h2>제목<strong>강조</strong></h2>",
        "<ul><li>항목<strong>볼드</strong>끝</li></ul>",
    ],
)
def test_to_md_is_deterministic_across_calls(xhtml: str) -> None:
    outputs = {cfxmark.to_md(xhtml).markdown for _ in range(5)}
    assert len(outputs) == 1, f"non-deterministic output: {outputs}"


@pytest.mark.parametrize(
    "xhtml",
    [
        "<p>한국어<strong>볼드</strong>입니다</p>",
        "<p>汉字<strong>粗体</strong>文本</p>",
        "<p>한국어 <strong>bold</strong> 한국어</p>",
        "<h2>제목<strong>강조</strong></h2>",
        "<ul><li>항목<strong>볼드</strong>끝</li></ul>",
    ],
)
def test_cjk_boundary_round_trip_converges_in_one_pass(xhtml: str) -> None:
    # The core invariant: once you've passed through to_md→to_cfx once, a
    # second pass must be a fixed point. The CJK boundary branch of this
    # invariant was not covered by the v0.1.0 ASCII-only property test.
    once = cfxmark.to_md(cfxmark.to_cfx(cfxmark.to_md(xhtml).markdown).xhtml).markdown
    twice = cfxmark.to_md(cfxmark.to_cfx(once).xhtml).markdown
    assert once == twice


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
