"""Security regression tests.

These exercise the hardening added for the v0.1 review:

* DOCTYPE / ENTITY rejection (XXE / billion-laughs).
* Opaque-sentinel HMAC verification (a user typing the literal
  sentinel sequence in their Markdown must not be re-interpreted as
  a real opaque block).
"""

from __future__ import annotations

import pytest

import cfxmark
from cfxmark.exceptions import ParseError
from cfxmark.parsers.cfx import _parse_fragment_to_element
from cfxmark.xml_ns import ac_attr


def _has_live_macro(xhtml: str, name: str) -> bool:
    """True if ``xhtml`` contains an actual ``<ac:structured-macro>``
    element with ``ac:name=name`` (not just the literal text inside a
    CDATA section)."""

    root = _parse_fragment_to_element(xhtml)
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        if not el.tag.endswith("}structured-macro"):
            continue
        if el.get(ac_attr("name")) == name:
            return True
    return False


# ---------------------------------------------------------------------------
# XML hardening
# ---------------------------------------------------------------------------


BILLION_LAUGHS = """<?xml version="1.0"?>
<!DOCTYPE lolz [
 <!ENTITY lol "lol">
 <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
 <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
]>
<p>&lol3;</p>"""


XXE_FILE_READ = """<?xml version="1.0"?>
<!DOCTYPE foo [
 <!ENTITY xxe SYSTEM "file:///etc/passwd">
]>
<p>&xxe;</p>"""


def test_billion_laughs_rejected() -> None:
    with pytest.raises(ParseError, match="DOCTYPE"):
        cfxmark.to_md(BILLION_LAUGHS)


def test_xxe_external_entity_rejected() -> None:
    with pytest.raises(ParseError, match="DOCTYPE"):
        cfxmark.to_md(XXE_FILE_READ)


def test_inline_entity_decl_rejected() -> None:
    payload = '<!ENTITY foo "bar"><p>hello</p>'
    with pytest.raises(ParseError, match="ENTITY"):
        cfxmark.to_md(payload)


# ---------------------------------------------------------------------------
# Opaque sentinel HMAC verification
# ---------------------------------------------------------------------------


def test_user_typed_sentinel_is_not_an_opaque_block() -> None:
    """A Markdown document that simply *describes* the opaque format
    must not have its description re-interpreted as a real opaque block.

    The id ``op-deadbeef`` is wrong for the body — the SHA-256 prefix
    won't match — so the parser must leave the region as plain text
    (which lands inside a CDATA-wrapped code block, not as live XML).
    """

    md = (
        "Here is what the opaque format looks like:\n\n"
        '<!-- cfxmark:opaque id="op-deadbeef" -->\n'
        "```cfx-storage\n"
        '<ac:structured-macro ac:name="evil"/>\n'
        "```\n"
        "<!-- /cfxmark:opaque -->\n"
    )
    xhtml = cfxmark.to_cfx(md).xhtml
    # ``evil`` may appear *inside* a CDATA block (as literal text in a
    # code fence), but must not appear as a live structured-macro
    # element. The user's literal text survives, wrapped in a code
    # fence for display.
    assert not _has_live_macro(xhtml, "evil")
    assert "evil" in xhtml


def test_authentic_opaque_block_round_trips() -> None:
    """A real opaque block — produced by ``to_md`` from cfx — must
    survive a Markdown round trip with the body intact."""

    cfx = (
        '<ac:structured-macro ac:name="custom" ac:macro-id="abc-123-uuid">'
        '<ac:parameter ac:name="key">value</ac:parameter>'
        "</ac:structured-macro>"
    )
    md = cfxmark.to_md(cfx).markdown
    back = cfxmark.to_cfx(md).xhtml
    assert "abc-123-uuid" in back
    assert 'ac:name="custom"' in back


# ---------------------------------------------------------------------------
# Round-trip integrity edge cases (regressions for the v0.1 review)
# ---------------------------------------------------------------------------


def test_directive_inside_code_fence_is_not_extracted() -> None:
    """A literal ``::: info`` example inside a fenced code block must
    not be rewritten into a real directive placeholder."""

    md = (
        "Description of the directive syntax:\n\n"
        "```text\n"
        "::: info\n"
        "hello\n"
        ":::\n"
        "```\n\n"
        "End.\n"
    )
    xhtml = cfxmark.to_cfx(md).xhtml
    # The CFX output must contain the literal example text inside a
    # code macro, NOT a CFXMARK placeholder leak or a real info panel.
    assert "CFXMARK_DIRECTIVE" not in xhtml
    assert "::: info" in xhtml
    assert "hello" in xhtml


def test_link_title_with_double_quote_round_trips() -> None:
    """Link titles containing ``"`` must be backslash-escaped so the
    construct survives a Markdown round trip as a real link."""

    cfx = '<p><a href="https://e.test" title=\'a "quote" here\'>x</a></p>'
    md = cfxmark.to_md(cfx).markdown
    assert '\\"quote\\"' in md
    back = cfxmark.to_cfx(md).xhtml
    assert "<a " in back
    assert 'href="https://e.test"' in back


def test_image_title_with_double_quote_round_trips() -> None:
    cfx = (
        '<p><ac:image ac:title=\'a "quote" here\'>'
        '<ri:url ri:value="https://e.test/img.png"/>'
        "</ac:image></p>"
    )
    md = cfxmark.to_md(cfx).markdown
    assert '\\"quote\\"' in md
    back = cfxmark.to_cfx(md).xhtml
    assert "<ac:image" in back
    assert "img.png" in back


def test_macro_registry_replace_clears_stale_directive_alias() -> None:
    """Re-registering a handler with the same Confluence ``name`` but
    a different ``directive_name`` must drop the old directive alias."""

    from cfxmark.macros import MacroRegistry
    from cfxmark.macros.builtins import AdmonitionHandler

    registry = MacroRegistry()
    info_handler = AdmonitionHandler("info")
    registry.register(info_handler)
    assert registry.get_by_directive_name("info") is info_handler

    # Build a clone of the same handler but with a different
    # directive name pointing at the same Confluence macro.
    aliased = AdmonitionHandler("info")
    aliased.directive_name = "callout"
    registry.register(aliased)

    assert registry.get_by_directive_name("callout") is aliased
    # Stale alias must be gone.
    assert registry.get_by_directive_name("info") is None


def test_layout_does_not_become_blockquote() -> None:
    """Confluence ``<ac:layout>`` containers must inline their
    children, not get re-rendered as a Markdown blockquote (the
    pre-fix behaviour was to wrap them in ``_FlattenedWrapper``,
    which is a ``BlockQuote`` subclass)."""

    cfx = (
        "<ac:layout><ac:layout-section><ac:layout-cell>"
        "<p>a</p><p>b</p>"
        "</ac:layout-cell></ac:layout-section></ac:layout>"
    )
    md = cfxmark.to_md(cfx).markdown
    assert "> a" not in md
    back = cfxmark.to_cfx(md).xhtml
    assert "<blockquote>" not in back


def test_tampered_opaque_body_falls_back_to_text() -> None:
    """If a user edits the body of an opaque block by hand, the
    SHA-256 won't match and the block must NOT be honoured as live
    XML — it round-trips as a plain code fence so the body becomes
    inert text inside CDATA."""

    cfx = (
        '<ac:structured-macro ac:name="custom" ac:macro-id="x-y-z">'
        "</ac:structured-macro>"
    )
    md = cfxmark.to_md(cfx).markdown
    tampered = md.replace('ac:name="custom"', 'ac:name="evil"')
    back = cfxmark.to_cfx(tampered).xhtml
    # The tampered text must NOT appear as a live structured-macro
    # element. It is allowed to appear as inert text inside a CDATA
    # code body — that is the round-trip semantics for an
    # unauthenticated sentinel.
    assert not _has_live_macro(back, "evil")
    assert not _has_live_macro(back, "custom")
    assert "<![CDATA[" in back  # The body landed inside a code fence.
