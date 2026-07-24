"""Control comments in managed Markdown (plan sections 6.1-6.5, Story 2).

Managed Markdown carries a few machine-readable comments alongside the user's
prose. Two things have to hold for that to be safe.

They must be removable exactly. The canonical content hash is defined over the
document with control comments stripped, so a stripper that misses one, or eats
a line the user wrote, changes the hash and breaks the consent chain.

They must not drown the document. A table with 220 styled cells is one
decision, not 220, and emitting a comment per cell would make the Markdown
unreadable and push the real content out of view.
"""

from __future__ import annotations

import pytest

from cfxmark.migration_comments import (
    NOTICE_TEXT,
    canonical_content,
    parse_migration_marker,
    parse_migrations_summary,
    render_migration_marker,
    render_migrations_summary,
    render_notice,
    strip_control_comments,
)
from cfxmark.migration_report import LocationComponent, MigrationOccurrence, MigrationReport, occurrence_identity

SOURCE_SHA = "sha256:" + "a" * 64


def _occurrence(*, code: str, effect: str, table: str, cell: str, ordinal: int) -> MigrationOccurrence:
    location = (
        LocationComponent(kind="table", stable_id=table),
        LocationComponent(kind="cell", anchor=cell),
    )
    return MigrationOccurrence(
        occurrence_id=occurrence_identity(
            source_storage_sha256=SOURCE_SHA, code=code, location_identity=location, ordinal=ordinal
        ),
        code=code,
        effect=effect,
        category="presentation_loss",
        location_identity=location,
    )


def _report(count: int = 3) -> MigrationReport:
    return MigrationReport.build(
        occurrences=tuple(
            _occurrence(code="table-cell-background-removed", effect="removed", table="t1", cell=f"A{i}", ordinal=i)
            for i in range(count)
        )
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_notice_is_a_single_html_comment() -> None:
    rendered = render_notice()
    assert rendered.startswith("<!-- cfxmark:notice ")
    assert rendered.endswith("-->")
    assert "\n" not in rendered
    assert NOTICE_TEXT in rendered


def test_migrations_summary_carries_counts_and_the_report_hash() -> None:
    report = _report()
    rendered = render_migrations_summary(report)

    fields = parse_migrations_summary(rendered)
    assert fields["v"] == "1"
    assert fields["removed"] == "3"
    assert fields["report"] == report.sha256()


def test_summary_omits_effects_with_no_occurrences() -> None:
    """Zeroes are noise; the reader should see only what actually happens."""
    fields = parse_migrations_summary(render_migrations_summary(_report()))
    assert "converted" not in fields
    assert "unsupported" not in fields


def test_repeated_cells_produce_one_marker_not_one_per_cell() -> None:
    report = _report(count=220)
    markers = [render_migration_marker(group) for group in report.grouped()]

    assert len(markers) == 1
    fields = parse_migration_marker(markers[0])
    assert fields["count"] == "220"
    assert fields["effect"] == "removed"
    assert fields["code"] == "table-cell-background-removed"
    assert fields["scope"] == "table:t1"


def test_marker_grammar_is_space_separated_key_values() -> None:
    marker = render_migration_marker(_report().grouped()[0])
    body = marker.removeprefix("<!-- cfxmark:migration ").removesuffix(" -->")
    for token in body.split(" "):
        assert "=" in token, token
        assert not token.startswith("=") and not token.endswith("=")


def test_rendered_comments_never_embed_raw_page_content() -> None:
    """Only identifiers and counts; a summary must not leak body text."""
    marker = render_migration_marker(_report().grouped()[0])
    summary = render_migrations_summary(_report())
    for rendered in (marker, summary, render_notice()):
        assert "<" not in rendered.removeprefix("<!--").removesuffix("-->")
        assert "\n" not in rendered


# ---------------------------------------------------------------------------
# Stripping and the canonical content hash
# ---------------------------------------------------------------------------


def test_strip_removes_control_comments_but_keeps_image_semantics() -> None:
    """cfxmark:img carries storage meaning, so it is content, not control."""
    document = (
        "<!-- atls:managed v=2 page=1 -->\n"
        f"{render_notice()}\n"
        f"{render_migrations_summary(_report())}\n"
        "\n"
        "# Title\n"
        "\n"
        f"{render_migration_marker(_report().grouped()[0])}\n"
        "Body text.\n"
        "\n"
        '![alt](a.png)<!-- cfxmark:img w=320 --><!-- cfxmark:asset src="a.png" -->\n'
    )
    stripped = strip_control_comments(document)

    for removed in ("atls:managed", "cfxmark:notice", "cfxmark:migrations", "cfxmark:migration ", "cfxmark:asset"):
        assert removed not in stripped, removed
    assert "cfxmark:img w=320" in stripped
    assert "# Title" in stripped
    assert "Body text." in stripped


def test_strip_leaves_marker_shaped_text_inside_fences_alone() -> None:
    """Inside a code fence these are the user's characters, not our control plane."""
    document = (
        f"{render_notice()}\n"
        "\n"
        "```\n"
        "<!-- cfxmark:notice this is documentation, not a directive -->\n"
        "<!-- atls:managed v=2 page=999 -->\n"
        "```\n"
    )
    stripped = strip_control_comments(document)

    assert stripped.count("cfxmark:notice") == 1
    assert "page=999" in stripped


def test_strip_leaves_indented_code_blocks_alone() -> None:
    document = f"{render_notice()}\n\n    <!-- atls:managed v=2 page=42 -->\n"
    stripped = strip_control_comments(document)
    assert "page=42" in stripped


def test_canonical_content_matches_the_plan_hash_definition() -> None:
    """Plan 6.5: LF, control comments gone, cfxmark:img kept, one trailing newline."""
    document = (
        "<!-- atls:managed v=2 page=1 -->\r\n"
        f"{render_notice()}\r\n"
        "\r\n"
        "# Title\r\n"
        '![a](a.png)<!-- cfxmark:img w=1 --><!-- cfxmark:asset src="a.png" -->\r\n'
        "\r\n\r\n"
    )
    content = canonical_content(document)

    assert "\r" not in content
    assert content.endswith("\n") and not content.endswith("\n\n")
    assert "cfxmark:img w=1" in content
    assert "atls:managed" not in content and "cfxmark:asset" not in content


def test_canonical_content_is_stable_when_only_control_comments_change() -> None:
    """Re-pulling must not change the content hash just because counts moved."""
    body = "# Title\n\nBody text.\n"
    first = canonical_content(f"{render_notice()}\n{render_migrations_summary(_report(3))}\n\n{body}")
    second = canonical_content(f"{render_notice()}\n{render_migrations_summary(_report(9))}\n\n{body}")
    assert first == second


def test_canonical_content_changes_when_the_user_edits() -> None:
    assert canonical_content("# A\n") != canonical_content("# B\n")


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_marker_round_trips_through_parse() -> None:
    group = _report(count=7).grouped()[0]
    fields = parse_migration_marker(render_migration_marker(group))

    assert fields["code"] == group.code
    assert fields["effect"] == group.effect
    assert fields["count"] == str(group.count)


@pytest.mark.parametrize(
    "line",
    [
        "<!-- cfxmark:migration -->",
        "<!-- cfxmark:migration code -->",
        "<!-- cfxmark:migration code= effect=removed -->",
        "<!-- cfxmark:migration code=a code=b -->",
    ],
)
def test_malformed_markers_are_rejected_rather_than_partly_parsed(line: str) -> None:
    with pytest.raises(ValueError):
        parse_migration_marker(line)
