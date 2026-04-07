"""Markdown → Confluence storage XHTML unit tests.

Each test checks that a small Markdown sample produces the expected
Confluence storage fragment, after canonicalization.
"""

from __future__ import annotations

import pytest

import cfxmark


def assert_md_to_cfx(markdown: str, expected_cfx: str) -> None:
    result = cfxmark.to_cfx(markdown)
    actual = cfxmark.canonicalize_cfx(result.xhtml)
    expected = cfxmark.canonicalize_cfx(expected_cfx)
    assert actual == expected, (
        f"Markdown → CFX mismatch.\n"
        f"  markdown: {markdown!r}\n"
        f"  expected: {expected!r}\n"
        f"  actual:   {actual!r}"
    )


# ---------------------------------------------------------------------------
# Headings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "level,marker",
    [(1, "#"), (2, "##"), (3, "###"), (4, "####"), (5, "#####"), (6, "######")],
)
def test_heading_level(level: int, marker: str) -> None:
    assert_md_to_cfx(f"{marker} Title", f"<h{level}>Title</h{level}>")


# ---------------------------------------------------------------------------
# Inline emphasis
# ---------------------------------------------------------------------------


def test_strong() -> None:
    assert_md_to_cfx("**bold**", "<p><strong>bold</strong></p>")


def test_emphasis() -> None:
    assert_md_to_cfx("*italic*", "<p><em>italic</em></p>")


def test_inline_code() -> None:
    assert_md_to_cfx("`code`", "<p><code>code</code></p>")


def test_strikethrough() -> None:
    assert_md_to_cfx("~~struck~~", "<p><del>struck</del></p>")


def test_combined_inline() -> None:
    assert_md_to_cfx(
        "**bold** and *italic* and `code`",
        "<p><strong>bold</strong> and <em>italic</em> and <code>code</code></p>",
    )


# ---------------------------------------------------------------------------
# Links and images
# ---------------------------------------------------------------------------


def test_link() -> None:
    assert_md_to_cfx(
        "[example](https://example.com)",
        '<p><a href="https://example.com">example</a></p>',
    )


def test_image_url() -> None:
    assert_md_to_cfx(
        "![alt](https://example.com/img.png)",
        '<p><ac:image ac:alt="alt"><ri:url ri:value="https://example.com/img.png"/></ac:image></p>',
    )


def test_image_attachment_records_in_attachments() -> None:
    result = cfxmark.to_cfx("![alt](local.png)")
    assert "local.png" in result.attachments


def test_image_with_dimensions_round_trip() -> None:
    md = "![alt](local.png#cfxmark:w=300,h=200)"
    result = cfxmark.to_cfx(md)
    assert 'ac:width="300"' in result.xhtml
    assert 'ac:height="200"' in result.xhtml


# ---------------------------------------------------------------------------
# Lists
# ---------------------------------------------------------------------------


def test_bullet_list() -> None:
    assert_md_to_cfx(
        "- one\n- two\n- three",
        "<ul><li>one</li><li>two</li><li>three</li></ul>",
    )


def test_ordered_list() -> None:
    assert_md_to_cfx(
        "1. one\n2. two",
        "<ol><li>one</li><li>two</li></ol>",
    )


def test_nested_list() -> None:
    assert_md_to_cfx(
        "- a\n  - a.1\n  - a.2\n- b",
        "<ul><li>a<ul><li>a.1</li><li>a.2</li></ul></li><li>b</li></ul>",
    )


# ---------------------------------------------------------------------------
# Code blocks
# ---------------------------------------------------------------------------


def test_code_fence_with_language() -> None:
    md = "```python\nx = 1\n```"
    expected = (
        '<ac:structured-macro ac:name="code">'
        '<ac:parameter ac:name="language">python</ac:parameter>'
        "<ac:plain-text-body>x = 1</ac:plain-text-body>"
        "</ac:structured-macro>"
    )
    assert_md_to_cfx(md, expected)


def test_code_fence_without_language() -> None:
    md = "```\nplain text\n```"
    expected = (
        '<ac:structured-macro ac:name="code">'
        "<ac:plain-text-body>plain text</ac:plain-text-body>"
        "</ac:structured-macro>"
    )
    assert_md_to_cfx(md, expected)


# ---------------------------------------------------------------------------
# Block quote, hr, table
# ---------------------------------------------------------------------------


def test_blockquote() -> None:
    assert_md_to_cfx("> hello", "<blockquote><p>hello</p></blockquote>")


def test_horizontal_rule() -> None:
    assert_md_to_cfx("---", "<hr/>")


def test_simple_table() -> None:
    md = "| a | b |\n| --- | --- |\n| 1 | 2 |"
    expected = (
        "<table>"
        "<thead><tr><th>a</th><th>b</th></tr></thead>"
        "<tbody><tr><td>1</td><td>2</td></tr></tbody>"
        "</table>"
    )
    assert_md_to_cfx(md, expected)


# ---------------------------------------------------------------------------
# CJK emphasis fallback
# ---------------------------------------------------------------------------


def test_intraword_strong_html_fallback_round_trip() -> None:
    """Intraword <strong> fallback must survive cfx → md → cfx.

    Plain CommonMark ``**X**`` inside a word-like run can fail the
    boundary rules and would not be re-parsed as emphasis. The md
    renderer detects this and emits ``<strong>...</strong>`` as
    inline HTML; the md parser reconstitutes it as a Strong AST node
    via its HtmlSpan handling.
    """

    cfx = "<p>Alpha<strong>(module boundary contract)</strong>Beta</p>"
    md = cfxmark.to_md(cfx).markdown
    back = cfxmark.to_cfx(md).xhtml
    assert "<strong>(module boundary contract)</strong>" in back
