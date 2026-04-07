"""Opaque passthrough tests."""

from __future__ import annotations

import cfxmark
from cfxmark.opaque import find_opaque_blocks, opaque_id_for, serialize_opaque


def test_opaque_id_is_deterministic() -> None:
    a = opaque_id_for("<a/>")
    b = opaque_id_for("<a/>")
    assert a == b
    assert a.startswith("op-")


def test_serialize_round_trip() -> None:
    raw = '<ac:structured-macro ac:name="x"/>'
    serialized = serialize_opaque(raw)
    matches = find_opaque_blocks(serialized)
    assert len(matches) == 1
    assert matches[0].raw_xml == raw


def test_unknown_macro_preserves_macro_id() -> None:
    cfx = (
        '<ac:structured-macro ac:name="custom" ac:macro-id="abc-123-uuid">'
        "<ac:parameter ac:name=\"k\">v</ac:parameter>"
        "</ac:structured-macro>"
    )
    md = cfxmark.to_md(cfx).markdown
    assert "abc-123-uuid" in md  # Preserved verbatim through opaque block
    back = cfxmark.to_cfx(md).xhtml
    assert "abc-123-uuid" in back  # Round-trips back to CFX intact


def test_round_trip_through_opaque_is_byte_stable() -> None:
    cfx = (
        '<ac:structured-macro ac:name="custom-thing">'
        "<ac:parameter ac:name=\"foo\">bar</ac:parameter>"
        "<ac:rich-text-body><p>body</p></ac:rich-text-body>"
        "</ac:structured-macro>"
    )
    canonical_orig = cfxmark.canonicalize_cfx(cfx)

    md_result = cfxmark.to_md(cfx)
    cfx_result = cfxmark.to_cfx(md_result.markdown)
    canonical_after = cfxmark.canonicalize_cfx(cfx_result.xhtml)

    assert canonical_orig == canonical_after
