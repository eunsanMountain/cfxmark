"""A table must keep its identity across the storage/Markdown boundary.

A table identity is only useful if both sides compute it to the same value: the
storage side derives it from a pulled page, the candidate side from the edited
Markdown, and the presentation channel (cell background, cell style) is re-attached
by matching the two. ``source_style`` is the raw storage ``style`` attribute, so a
Markdown-parsed candidate can never carry it -- including it in the identity made
the two sides disagree about a table that is in fact the same table.

The visible symptom was that a page with two same-shape tables, at least one of
them aligned, could not be edited at all: the identity match failed, the topology
fallback found two equally good candidates, and the proof went fatal with
``table-presentation-ambiguous``. Alignment is ordinary Markdown, so this hit
ordinary documents.
"""

from __future__ import annotations

import dataclasses

import pytest

import cfxmark
from cfxmark.exceptions import OwnershipProofError
from cfxmark.parsers.md import parse_md
from cfxmark.presentation import _iter_tables, _table_identities

EDITABLE = cfxmark.ConversionOptions(profile="editable")


def _table(columns: int, rows: int, *, align_column: int | None = None, tag: str = "a") -> str:
    """A table of the given shape. ``tag`` varies the cell text.

    Tables of the same shape need distinct content, or they are duplicates in the
    ordinary sense and refusing them is correct — a different case from the one
    under test here.
    """

    header = "| " + " | ".join(f"h{tag}{i}" for i in range(columns)) + " |"
    delimiter = "| " + " | ".join("---:" if i == align_column else "---" for i in range(columns)) + " |"
    body = "\n".join("| " + " | ".join(f"c{tag}{r}{i}" for i in range(columns)) + " |" for r in range(rows))
    return f"{header}\n{delimiter}\n{body}\n"


def _publish_then_edit_in_place(markdown: str) -> None:
    """Round-trip a document the way `page create` then `page update` does.

    Raises ``OwnershipProofError`` when the in-place proof is fatal.
    """

    storage = cfxmark.to_cfx_artifact(markdown).xhtml
    base = dataclasses.replace(
        cfxmark.to_md_artifact(storage, options=EDITABLE),
        protected_regions=(),
        remote_subtrees=(),
    )
    candidate = cfxmark.to_cfx_artifact(
        markdown,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )
    cfxmark.validate_managed_cfx_artifact(candidate, source_storage=storage, options=EDITABLE)


@pytest.mark.parametrize(
    ("name", "markdown"),
    [
        (
            "two aligned tables of the same shape",
            _table(2, 1, align_column=1, tag="a") + "\n" + _table(2, 1, align_column=1, tag="b"),
        ),
        (
            "two aligned tables, different width",
            _table(2, 1, align_column=1, tag="a") + "\n" + _table(3, 1, align_column=2, tag="b"),
        ),
        (
            "two aligned tables, different height",
            _table(2, 1, align_column=1, tag="a") + "\n" + _table(2, 3, align_column=1, tag="b"),
        ),
        (
            "five aligned tables, two sharing a shape",
            "\n".join(_table(n, 1, align_column=1, tag=chr(ord("a") + n)) for n in (2, 3, 4, 5))
            + "\n"
            + _table(2, 1, align_column=1, tag="z"),
        ),
        (
            "one aligned and one plain table of the same shape",
            _table(2, 1, align_column=1, tag="a") + "\n" + _table(2, 1, tag="b"),
        ),
        (
            "mixed alignment styles",
            _table(2, 1, align_column=1, tag="a") + "\n" + _table(2, 1, align_column=0, tag="b"),
        ),
    ],
)
def test_aligned_tables_survive_a_publish_then_edit_round_trip(name: str, markdown: str) -> None:
    """Every one of these was fatal while ``source_style`` was part of the identity."""

    _publish_then_edit_in_place("# T\n\n" + markdown)


def test_identity_still_separates_tables_that_differ_only_by_alignment() -> None:
    """Dropping ``source_style`` must not cost real discriminating power.

    ``align`` carries the same meaning and Markdown can express it, so both sides
    have it and it stays in the identity.
    """

    markdown = "# T\n\n" + _table(2, 1, align_column=1) + "\n" + _table(2, 1)
    document, _ = parse_md(markdown)
    identities = _table_identities(_iter_tables(document))

    assert identities[0][0] != identities[1][0]
    assert not any(ambiguous for _id, _topology, ambiguous in identities)


def test_identical_tables_remain_ambiguous() -> None:
    """Two byte-identical tables are genuinely indistinguishable — keep refusing.

    There is no way to know which one an edit belongs to, so writing to either is
    a coin flip against the user's page.
    """

    markdown = "# T\n\n" + _table(2, 1) + "\n" + _table(2, 1)

    with pytest.raises(OwnershipProofError) as excinfo:
        _publish_then_edit_in_place(markdown)
    assert excinfo.value.summary.fatal_class == "table-presentation-ambiguous"


def test_tables_differing_only_by_a_non_alignment_style_fail_closed() -> None:
    """The one case that loses discriminating power must land on refusal, not a write.

    ``source_style`` also carries styles Markdown cannot express, so two tables
    identical apart from such a style now share an identity. That has to surface as
    an ambiguous identity — which refuses the edit — and never as a silent pick.
    Only a web-editor page can reach this shape; Markdown cannot author it.
    """

    styled = '<table><tbody><tr><td style="font-weight: bold">x</td><td>b</td></tr></tbody></table>'
    plain = "<table><tbody><tr><td>x</td><td>b</td></tr></tbody></table>"
    artifact = cfxmark.to_md_artifact("<h1>T</h1>" + styled + plain, options=EDITABLE)

    identities = _table_identities(_iter_tables(artifact.document))

    assert identities[0][0].startswith(identities[1][0].rsplit("-duplicate-", 1)[0])
    assert all(ambiguous for _id, _topology, ambiguous in identities)
    assert len(artifact.presentation.ambiguous_table_ids) == 2


def test_rendered_storage_is_unchanged_by_the_identity_rule() -> None:
    """The identity is a matching key, not an output. Pin the render so it stays that way."""

    markdown = "# T\n\n| a | b |\n| --- | ---: |\n| x | 1 |\n"

    assert cfxmark.to_cfx_artifact(markdown).xhtml == (
        "<h1>T</h1>"
        '<table><thead><tr><th>a</th><th style="text-align: right;">b</th></tr></thead>'
        '<tbody><tr><td>x</td><td style="text-align: right;">1</td></tr></tbody></table>'
    )
