"""Managed Markdown must disclose what publishing it will do (plan Story 2).

The control comments existed as renderers with no caller, so a managed document
looked exactly like an unmanaged one. A reader had no way to tell that a push
would change anything, and no way to see what.

Three things are wired here, in the order a reader meets them: a notice that the
document is managed, a summary naming the report it corresponds to, and one
marker per migration group describing a specific change and its scope.

The round-trip requirement is the constraint that makes this non-trivial. These
comments are cfxmark's own annotations, not the user's content, so the candidate
path has to strip them back out -- otherwise every managed pull would publish its
own bookkeeping into the remote page.
"""

from __future__ import annotations

from cfxmark import api
from cfxmark.api import ConversionOptions
from cfxmark.migration_comments import (
    NOTICE_TEXT,
    parse_migration_marker,
    parse_migrations_summary,
    strip_control_comments,
)

LEGACY = ConversionOptions(profile="legacy")
EDITABLE = ConversionOptions(profile="editable")

LOSSY = "<p>a<!-- dropped comment --></p>"
CLEAN = "<p>just text</p>"


def _lines(markdown: str) -> list[str]:
    return markdown.splitlines()


# ---------------------------------------------------------------------------
# Disclosure
# ---------------------------------------------------------------------------


def test_managed_markdown_always_carries_the_notice() -> None:
    """Always-on: a document with nothing to report still needs to say it is managed.

    Emitting the notice only when something was lost would make its absence
    ambiguous -- unmanaged document, or managed one with no findings?
    """
    for storage in (CLEAN, LOSSY):
        markdown = api.to_md_artifact(storage, options=LEGACY).managed_markdown
        assert NOTICE_TEXT in markdown, f"no notice for {storage!r}"


def test_the_notice_comes_first() -> None:
    markdown = api.to_md_artifact(LOSSY, options=LEGACY).managed_markdown
    assert NOTICE_TEXT in _lines(markdown)[0]


def test_a_lossy_conversion_summarises_its_report() -> None:
    artifact = api.to_md_artifact(LOSSY, options=LEGACY)
    summary = next(
        (parse_migrations_summary(line) for line in _lines(artifact.managed_markdown) if "cfxmark:migrations" in line),
        None,
    )
    assert summary is not None, "no migrations summary in a document that lost content"
    assert summary["report"] == artifact.migration_report.sha256()


def test_the_summary_is_absent_when_nothing_was_migrated() -> None:
    """A summary of nothing is noise that trains readers to skip the header."""
    markdown = api.to_md_artifact(CLEAN, options=LEGACY).markdown
    assert "cfxmark:migrations" not in markdown


def test_each_migration_group_gets_a_scoped_marker() -> None:
    artifact = api.to_md_artifact(LOSSY, options=LEGACY)
    markers = [
        parse_migration_marker(line) for line in _lines(artifact.managed_markdown) if "cfxmark:migration " in line
    ]
    groups = artifact.migration_report.grouped()
    assert len(markers) == len(groups)
    assert {marker["code"] for marker in markers} == {group.code for group in groups}
    for marker in markers:
        assert marker["scope"], "a marker without a scope cannot be acted on"


def test_the_marker_reports_the_effect_a_reader_must_decide_about() -> None:
    artifact = api.to_md_artifact(LOSSY, options=LEGACY)
    markers = [
        parse_migration_marker(line) for line in _lines(artifact.managed_markdown) if "cfxmark:migration " in line
    ]
    assert {marker["effect"] for marker in markers} == {group.effect for group in artifact.migration_report.grouped()}


# ---------------------------------------------------------------------------
# The annotations are not content
# ---------------------------------------------------------------------------


def test_the_body_markdown_is_unchanged_by_disclosure() -> None:
    """Stripping the header must return exactly the document the user would edit."""
    artifact = api.to_md_artifact(LOSSY, options=LEGACY)
    assert strip_control_comments(artifact.managed_markdown).strip() == "a"


def test_control_comments_are_not_published() -> None:
    """The failure this would cause: cfxmark's bookkeeping written to the page."""
    for storage in (CLEAN, LOSSY):
        artifact = api.to_md_artifact(storage, options=LEGACY)
        candidate = api.to_cfx_artifact(
            artifact.managed_markdown, base_artifact=artifact, splice_source=storage, options=LEGACY
        )
        assert "cfxmark:notice" not in candidate.xhtml
        assert "cfxmark:migrations" not in candidate.xhtml
        assert "cfxmark:migration " not in candidate.xhtml


def test_disclosure_changes_the_candidate_in_no_way_at_all() -> None:
    """Checking for absent comment text is not enough.

    A Markdown parser discards an HTML comment on its own, so the header leaves
    no trace a substring check would find -- while still parsing as an empty
    block and prepending a stray empty paragraph to the published storage. The
    only assertion that catches that is byte equality against the body.
    """
    for storage in (CLEAN, LOSSY):
        artifact = api.to_md_artifact(storage, options=LEGACY)
        with_header = api.to_cfx_artifact(
            artifact.managed_markdown, base_artifact=artifact, splice_source=storage, options=LEGACY
        )
        body_only = api.to_cfx_artifact(
            artifact.markdown, base_artifact=artifact, splice_source=storage, options=LEGACY
        )
        assert with_header.xhtml == body_only.xhtml, f"disclosure altered the candidate for {storage!r}"
        assert with_header.candidate_storage_sha256 == body_only.candidate_storage_sha256


def test_an_unedited_managed_document_still_round_trips() -> None:
    """Disclosure must not make a document look edited."""
    artifact = api.to_md_artifact(CLEAN, options=LEGACY)
    candidate = api.to_cfx_artifact(
        artifact.managed_markdown, base_artifact=artifact, splice_source=CLEAN, options=LEGACY
    )
    assert candidate.push_safe
    assert "just text" in candidate.xhtml


def test_a_user_edit_still_lands_through_the_header() -> None:
    """The header must not shift or swallow the edit beneath it."""
    artifact = api.to_md_artifact(CLEAN, options=LEGACY)
    edited = artifact.managed_markdown.replace("just text", "replaced text")
    candidate = api.to_cfx_artifact(edited, base_artifact=artifact, splice_source=CLEAN, options=LEGACY)
    assert "replaced text" in candidate.xhtml
    assert "just text" not in candidate.xhtml


def test_a_reader_who_deletes_the_header_is_not_punished() -> None:
    """The comments are cfxmark's, so removing them is not a content edit."""
    artifact = api.to_md_artifact(CLEAN, options=LEGACY)
    stripped = strip_control_comments(artifact.managed_markdown)
    candidate = api.to_cfx_artifact(stripped, base_artifact=artifact, splice_source=CLEAN, options=LEGACY)
    assert candidate.push_safe
    assert "just text" in candidate.xhtml


def test_disclosure_reaches_the_editable_profile_too() -> None:
    """The profile managed pull actually uses."""
    markdown = api.to_md_artifact(LOSSY, options=EDITABLE).managed_markdown
    assert NOTICE_TEXT in markdown


# ---------------------------------------------------------------------------
# Why the header is not stored inside `markdown`
# ---------------------------------------------------------------------------


STYLED_TABLE = '<p>before</p><table><tbody><tr><td style="background-color: #ff0000">X</td></tr></tbody></table>'


def test_a_marker_sits_next_to_the_block_it_describes() -> None:
    """A scope list in the header is a summary the reader must resolve by hand.

    Someone scrolling to a table needs to learn there that its colours will not
    survive -- at the point they would otherwise assume the table is fine.
    """
    lines = _lines(api.to_md_artifact(STYLED_TABLE, options=LEGACY).managed_markdown)
    marker_line = next(index for index, line in enumerate(lines) if "cfxmark:migration " in line)
    table_line = next(index for index, line in enumerate(lines) if line.startswith("|"))
    assert marker_line < table_line
    assert table_line - marker_line <= 2, "the marker drifted away from its table"


def test_the_marker_is_below_the_body_text_that_precedes_the_table() -> None:
    """Proves placement is block-local rather than just a header by another name."""
    lines = _lines(api.to_md_artifact(STYLED_TABLE, options=LEGACY).managed_markdown)
    before_line = next(index for index, line in enumerate(lines) if line.strip() == "before")
    marker_line = next(index for index, line in enumerate(lines) if "cfxmark:migration " in line)
    assert before_line < marker_line


def test_an_inline_marker_still_strips_back_to_the_body() -> None:
    artifact = api.to_md_artifact(STYLED_TABLE, options=LEGACY)
    assert strip_control_comments(artifact.managed_markdown) == artifact.markdown


def test_an_inline_marker_does_not_reach_the_published_storage() -> None:
    artifact = api.to_md_artifact(STYLED_TABLE, options=LEGACY)
    with_markers = api.to_cfx_artifact(
        artifact.managed_markdown, base_artifact=artifact, splice_source=STYLED_TABLE, options=LEGACY
    )
    body_only = api.to_cfx_artifact(
        artifact.markdown, base_artifact=artifact, splice_source=STYLED_TABLE, options=LEGACY
    )
    assert with_markers.xhtml == body_only.xhtml


def test_markdown_stays_the_body_that_offsets_are_measured_against() -> None:
    """The reason disclosure is composed rather than prefixed.

    Ranges on this artifact index `markdown`. Prefixing that field would leave
    every one of them pointing at the wrong bytes while still looking correct --
    an off-by-header error that no type would catch.
    """
    artifact = api.to_md_artifact(CLEAN, options=LEGACY)
    assert not artifact.markdown.startswith("<!--")
    assert artifact.markdown == strip_control_comments(artifact.managed_markdown)


def test_protected_region_ranges_index_the_body_not_the_managed_document() -> None:
    macro = (
        "<p>before</p>"
        '<ac:structured-macro ac:name="totally-unknown">'
        "<ac:plain-text-body><![CDATA[payload]]></ac:plain-text-body>"
        "</ac:structured-macro>"
        "<p>after</p>"
    )
    artifact = api.to_md_artifact(macro, options=EDITABLE)
    assert artifact.protected_regions
    for region in artifact.protected_regions:
        span = artifact.markdown[region.base_markdown_range.start : region.base_markdown_range.end]
        assert span, "a protected region range points outside the body"
        assert "cfxmark:" not in span, "a range slid into the disclosure header"
