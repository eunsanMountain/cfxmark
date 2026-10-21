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
# Adjacent inline-style coalescing
#
# Markdown cannot express two adjacent style runs with no separator: ``*A**B*``
# re-parses as a single ``<em>A**B</em>`` with a literal ``**`` injected into
# the content. Confluence's editor routinely emits split wrappers, so the
# renderer merges an uninterrupted run into one visible span — the same
# equivalence the adjacent inline-code path already relies on.
#
# A separator (even a single space) is a real boundary and must survive.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("xhtml", "expected"),
    [
        ("<p><em>A</em><em>B</em></p>", "*AB*"),
        ("<p><i>A</i><i>B</i></p>", "*AB*"),
        ("<p><em>A</em><em>B</em><em>C</em></p>", "*ABC*"),
        ("<p><em>A</em><em>B</em><em>C</em><em>D</em></p>", "*ABCD*"),
        ("<p><em>A </em><em>B</em></p>", "*A B*"),
        ("<p><strong>A</strong><strong>B</strong></p>", "**AB**"),
        ("<p><b>A</b><b>B</b></p>", "**AB**"),
        ("<p><b>A</b><strong>B</strong></p>", "**AB**"),
        ("<p><strong>A</strong><b>B</b></p>", "**AB**"),
        ("<p><strong>A</strong><strong>B</strong><strong>C</strong></p>", "**ABC**"),
        ("<p><strong>A </strong><strong>B</strong></p>", "**A B**"),
    ],
)
def test_adjacent_inline_styles_render_as_one_run(xhtml: str, expected: str) -> None:
    assert md(xhtml) == expected


@pytest.mark.parametrize(
    ("xhtml", "expected"),
    [
        ("<p><em>A</em> <em>B</em></p>", "*A* *B*"),
        ("<p><strong>A</strong> <strong>B</strong></p>", "**A** **B**"),
    ],
)
def test_separated_inline_styles_stay_separate(xhtml: str, expected: str) -> None:
    assert md(xhtml) == expected


@pytest.mark.parametrize(
    "xhtml",
    [
        "<p><em>A</em><em>B</em></p>",
        "<p><i>A</i><i>B</i></p>",
        "<p><em>A</em><em>B</em><em>C</em></p>",
        "<p><strong>A</strong><strong>B</strong></p>",
    ],
)
def test_adjacent_inline_styles_do_not_inject_literal_delimiters(xhtml: str) -> None:
    # The defect this guards: ``*A**B*`` re-parses with a literal ``**`` in the
    # visible text. Re-parsing must round-trip the styled text unchanged.
    rendered = md(xhtml)
    assert "**B" not in rendered
    assert "*" not in cfxmark.to_cfx(rendered).xhtml


# ---------------------------------------------------------------------------
# Asterisk flanking (``*`` / ``**``)
#
# CommonMark only requires an asterisk opener to be left-flanking and a closer
# to be right-flanking; the extra "must not also flank the other way" clause
# applies to ``_`` / ``__``. Intraword asterisk emphasis therefore re-parses
# fine -- ``**볼드**이다`` and ``**bold**text`` both survive -- and the run
# breaks only when the *inside* boundary character is punctuation while the
# *outside* one is neither whitespace nor punctuation.
#
# "Neither whitespace nor punctuation" is wider than "word character": Unicode
# symbols (``★``, ``°``, ``€``, emoji) also block flanking, and a word-class
# predicate silently misses them.
#
# Target: the locked mistletoe 1.5.1 parser, whose punctuation table follows
# CommonMark 0.30 (ASCII punctuation plus Unicode category ``P*``). Unicode
# ``S*`` membership alone does not make a character punctuation here.
# ---------------------------------------------------------------------------


def _as_emphasis(text: str) -> str:
    return text.replace("strong>", "em>").replace("**", "*")


@pytest.mark.parametrize(
    ("xhtml", "expected"),
    [
        # Inside boundary is a word character -> delimiters always survive.
        ("<p><strong>볼드</strong>이다</p>", "**볼드**이다"),
        ("<p>이다<strong>볼드</strong></p>", "이다**볼드**"),
        ("<p><strong>bold</strong>text</p>", "**bold**text"),
        ("<p>text<strong>bold</strong></p>", "text**bold**"),
        ("<p>prefix한국어<strong>볼드</strong>suffix한국어</p>", "prefix한국어**볼드**suffix한국어"),
        # Inside punctuation, but outside is whitespace -> still fine.
        ("<p><strong>(주의)</strong> 입니다</p>", "**(주의)** 입니다"),
        ("<p>입니다 <strong>(주의)</strong></p>", "입니다 **(주의)**"),
        # Inside punctuation next to a word character -> HTML is required.
        ("<p><strong>볼드.</strong>이다</p>", "<strong>볼드.</strong>이다"),
        ("<p>이다<strong>.볼드</strong></p>", "이다<strong>.볼드</strong>"),
        # Inside punctuation next to a Unicode symbol -> HTML is required.
        ("<p>★<strong>(주의)</strong>★</p>", "★<strong>(주의)</strong>★"),
        ("<p>🙂<strong>(주의)</strong>🙂</p>", "🙂<strong>(주의)</strong>🙂"),
        ("<p>°<strong>3.5.</strong>°</p>", "°<strong>3.5.</strong>°"),
        ("<p>€<strong>(값)</strong>€</p>", "€<strong>(값)</strong>€"),
    ],
)
def test_asterisk_flanking_chooses_delimiters_or_html(xhtml: str, expected: str) -> None:
    assert md(xhtml) == expected
    assert md(_as_emphasis(xhtml)) == _as_emphasis(expected)


@pytest.mark.parametrize(
    "xhtml",
    [
        "<p><strong>볼드</strong>이다</p>",
        "<p>text<strong>bold</strong></p>",
        "<p><strong>볼드.</strong>이다</p>",
        "<p>★<strong>(주의)</strong>★</p>",
        "<p>°<strong>3.5.</strong>°</p>",
        "<p>€<strong>(값)</strong>€</p>",
    ],
)
def test_asterisk_flanking_choice_preserves_the_style_on_reparse(xhtml: str) -> None:
    # The oracle behind the matrix above: whichever spelling the renderer
    # picks, re-parsing it must reproduce the original storage.
    assert cfxmark.to_cfx(md(xhtml)).xhtml == xhtml
    emphasis = _as_emphasis(xhtml)
    assert cfxmark.to_cfx(md(emphasis)).xhtml == emphasis


# ---------------------------------------------------------------------------
# CJK boundary regression
#
# These tests used to pin the opposite expectation: that a CJK glyph on either
# side of ``**X**`` forces raw ``<strong>`` HTML. That was based on a misread
# of CommonMark -- the "flanked by word characters on both sides" restriction
# belongs to ``_``/``__``, not to ``*``/``**``. Korean particles attach to
# almost every bold run in Korean prose, so the misread turned raw HTML into
# the default and left 93 ``<strong>`` remnants in the pinned corpus.
#
# What actually matters at a CJK boundary is unchanged: the round trip must
# survive. The asserts below therefore pin the delimiter spelling *and* the
# re-parse, so a future refactor cannot trade one for the other.
# ---------------------------------------------------------------------------


def test_strong_korean_word_flank_uses_plain_markdown() -> None:
    # Korean particles are word characters, not punctuation → ``**`` survives.
    assert md("<p>한국어<strong>볼드</strong>입니다</p>") == "한국어**볼드**입니다"


def test_strong_cjk_ideograph_flank_uses_plain_markdown() -> None:
    assert md("<p>汉字<strong>粗体</strong>文本</p>") == "汉字**粗体**文本"


def test_strong_with_spaces_around_korean_uses_plain_markdown() -> None:
    assert md("<p>한국어 <strong>bold</strong> 한국어</p>") == "한국어 **bold** 한국어"


def test_emphasis_korean_word_flank_uses_plain_markdown() -> None:
    assert md("<p>한국어<em>강조</em>입니다</p>") == "한국어*강조*입니다"


def test_emphasis_pure_korean_no_flank_uses_plain_markdown() -> None:
    # Paragraph-only emphasis has empty outer chars → plain ``*`` works.
    assert md("<p><em>강조</em></p>") == "*강조*"


def test_strikethrough_korean_word_flank_falls_back_to_html() -> None:
    # Strikethrough is deliberately NOT routed through the asterisk predicate:
    # mistletoe parses ``~~`` with a regex, not with flanking rules, so it has
    # its own delimiter contract. If this test starts failing, the shared
    # helper was changed without keeping the delimiter paths separate.
    assert md("<p>한국어<del>취소</del>입니다</p>") == "한국어<del>취소</del>입니다"


def test_strong_cjk_boundary_inside_list_item() -> None:
    # Same policy must apply inside block containers (lists, headings …).
    assert md("<ul><li>항목<strong>볼드</strong>끝</li></ul>") == "- 항목**볼드**끝"


def test_strong_cjk_boundary_inside_heading() -> None:
    assert md("<h2>제목<strong>강조</strong></h2>") == "## 제목**강조**"


def test_strong_cjk_boundary_mixed_ascii_and_korean_flank() -> None:
    assert md("<p>prefix한국어<strong>볼드</strong>suffix한국어</p>") == "prefix한국어**볼드**suffix한국어"


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
    assert md('<p><a href="https://example.com">label</a></p>') == "[label](https://example.com)"


def test_anchor_without_href_uses_exact_editable_html() -> None:
    storage = "<p><a>file:line</a></p>"

    assert md(storage) == "file:line"
    assert (
        cfxmark.to_md(
            storage,
            options=cfxmark.ConversionOptions(profile="editable"),
        ).markdown.rstrip("\n")
        == "<a>file:line</a>"
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
    cfx = '<ac:image ac:width="300" ac:height="200"><ri:attachment ri:filename="foo.png"/></ac:image>'
    out = md(f"<p>{cfx}</p>")
    assert "foo.png" in out
    assert "<!-- cfxmark:img w=300 h=200 -->" in out


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


# ---------------------------------------------------------------------------
# Table cells with editor <div> wrappers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cell", "expected"),
    [
        ("before<div>inside</div>after", "before<br>inside<br>after"),
        ("<div>inside</div>after", "inside<br>after"),
        ('<div class="content-wrapper"><p>inside</p></div>after', "inside<br>after"),
        ("<div>a<strong>b</strong>c</div>", "a**b**c"),
        ("<div><div>x</div>y</div>z", "x<br>y<br>z"),
    ],
)
def test_cell_div_keeps_surrounding_text(cell: str, expected: str) -> None:
    out = md(f"<table><tbody><tr><th>h</th></tr><tr><td>{cell}</td></tr></tbody></table>")
    assert out.splitlines()[-1] == f"| {expected} |"
