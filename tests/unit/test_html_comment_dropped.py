"""A user-authored HTML comment (``<!-- ... -->``) in Markdown is a describable,
consent-required loss on the managed Markdown->storage push, not an opaque fatal.

Before this contract, the parser dropped a bare comment with the generic
``"unmatched inline HTML span dropped"`` warning, which fell through to the
``conversion-note`` fallback and resolved to a *fatal* effect -- blocking the
whole page. A comment carries author-only text that never renders in Confluence,
so the loss is nameable and consentable (the ``html-comment-dropped`` code with
the ``removed`` effect), the push-side mirror of the pull-side
``xml-comment-dropped``.

The discriminator is the ``<!--``/``-->`` syntax at the parser, NOT a
reclassification of the shared warning string: processing instructions
(``<? ?>``), CDATA (``<![CDATA[ ]]>``) and declarations (``<!DOCTYPE>``) reach
the same fall-through but are not comments, so they keep the generic message and
its fail-closed path.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

import cfxmark
from cfxmark.diagnostics import diagnostics_from_messages
from cfxmark.migration_report import MigrationOccurrence
from cfxmark.parsers.md import parse_md

EDITABLE = cfxmark.ConversionOptions(profile="editable")

_STORAGE = "<p>Alpha</p><p>Beta</p>"


def _managed_candidate(markdown: str, *, storage: str = _STORAGE) -> cfxmark.CfxArtifact:
    """Push ``markdown`` as a managed update against a fresh pull of ``storage``."""

    base = replace(cfxmark.to_md_artifact(storage, options=EDITABLE), protected_regions=(), remote_subtrees=())
    return cfxmark.to_cfx_artifact(
        markdown,
        presentation=base.presentation,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )


def _occurrences(candidate: cfxmark.CfxArtifact) -> tuple[MigrationOccurrence, ...]:
    report = candidate.source_migration_report
    assert report is not None
    return report.occurrences


# --- positive: a user comment is a consent-required removal, not a fatal --------


@pytest.mark.parametrize(
    ("label", "markdown", "comment_text"),
    [
        ("single_line", "Alpha\n\nBeta\n\n<!-- a user note -->\n", "a user note"),
        ("multi_line", "Alpha\n\nBeta\n\n<!--\nline one\nline two\n-->\n", "line one"),
        ("inline_in_paragraph", "Alpha with <!-- inline note --> tail\n\nBeta\n", "inline note"),
    ],
)
def test_managed_user_html_comment_is_consentable_removed_not_fatal(
    label: str, markdown: str, comment_text: str
) -> None:
    """Acceptance: the drop is a consent-required ``removed`` occurrence (not fatal,
    not a warn-only tier), and the comment is gone from the candidate storage -- for
    single-line, multi-line, and inline comments alike. Being a blocking content_loss
    (like every source loss), push_safe is False and the loss is resolved through
    consent, not silently."""

    candidate = _managed_candidate(markdown)

    report = candidate.source_migration_report
    assert report is not None
    assert report.fatal is False
    assert candidate.push_safe is False  # blocking content_loss -> not push_safe; consent (below) is the gate

    comment_occurrences = [item for item in report.occurrences if item.code == "html-comment-dropped"]
    assert len(comment_occurrences) == 1
    occurrence = comment_occurrences[0]
    assert occurrence.effect == "removed"
    assert occurrence.consent_required is True

    # The dropped comment does not reach storage (neither its delimiters nor text).
    assert "<!--" not in candidate.xhtml
    assert comment_text not in candidate.xhtml

    # A fresh-source publish preflight accepts the consentable candidate.
    cfxmark.validate_managed_cfx_artifact(candidate, source_storage=_STORAGE, options=EDITABLE)


def test_do_not_publish_comment_is_removed_consent_not_silent_or_fatal() -> None:
    """A ``<!-- do-not-publish -->`` marker must become a consent-required removal:
    publishable only after a decision, never silently dropped and never a fatal wall."""

    candidate = _managed_candidate("Alpha\n\nBeta\n\n<!-- do-not-publish -->\n")

    report = candidate.source_migration_report
    assert report is not None
    assert report.fatal is False
    assert candidate.push_safe is False  # blocking content_loss -> consent, not silent, not fatal
    assert [item.code for item in report.occurrences] == ["html-comment-dropped"]
    occurrence = report.occurrences[0]
    assert occurrence.effect == "removed"
    assert occurrence.consent_required is True
    assert "do-not-publish" not in candidate.xhtml


def test_comment_drop_adds_no_unclassified_storage_change() -> None:
    """Scope confirmation: a comment renders nothing, so its removal needs only an
    effect entry -- it never produces an ``unclassified-storage-change`` fatal, on
    any of the single-line, multi-line, or inline forms."""

    for markdown in (
        "Alpha\n\nBeta\n\n<!-- a user note -->\n",
        "Alpha\n\nBeta\n\n<!--\nline one\nline two\n-->\n",
        "Alpha with <!-- inline note --> tail\n\nBeta\n",
    ):
        codes = {item.code for item in _occurrences(_managed_candidate(markdown))}
        assert codes == {"html-comment-dropped"}
        assert "unclassified-storage-change" not in codes


# --- misclassification guard: PI / CDATA / declaration keep their old path ------


@pytest.mark.parametrize(
    ("label", "fragment"),
    [
        ("processing_instruction", "<?php echo 1; ?>"),
        ("cdata", "<![CDATA[x]]>"),
        ("declaration", "<!DOCTYPE html>"),
    ],
)
def test_non_comment_html_keeps_the_generic_warning_at_the_parser(label: str, fragment: str) -> None:
    """The ``<!--`` branch is syntax-scoped: a construct that is not a genuine
    comment keeps the original ``"unmatched inline HTML span dropped"`` message and
    is never reclassified as ``html-comment-dropped``."""

    _document, warnings = parse_md(f"Alpha {fragment} tail\n")

    assert "HTML comment dropped" not in warnings
    assert "unmatched inline HTML span dropped" in warnings

    codes = [
        diagnostic.code for diagnostic in diagnostics_from_messages(warnings=tuple(warnings), losses=(), source="x")
    ]
    assert "html-comment-dropped" not in codes
    assert "conversion-note" in codes


@pytest.mark.parametrize(
    "fragment",
    ["<?php echo 1; ?>", "<![CDATA[x]]>", "<!DOCTYPE html>"],
)
def test_non_comment_html_stays_fatal_on_the_managed_path(fragment: str) -> None:
    """The relaxation must not leak into processing instructions, CDATA, or
    declarations: they still fail closed as ``conversion-note`` fatals, not
    consentable removals."""

    candidate = _managed_candidate(f"Alpha {fragment} tail\n\nBeta\n")

    report = candidate.source_migration_report
    assert report is not None
    assert report.fatal is True
    assert candidate.push_safe is False
    codes = {item.code for item in report.occurrences}
    assert "html-comment-dropped" not in codes
    assert "conversion-note" in codes


def test_html_comment_is_reclassified_at_the_parser_by_syntax() -> None:
    """The positive half of the syntax scope: a genuine comment gets the distinct
    ``"HTML comment dropped"`` message that maps to the classified consentable code."""

    _document, warnings = parse_md("Alpha <!-- a note --> tail\n")

    assert "HTML comment dropped" in warnings
    assert "unmatched inline HTML span dropped" not in warnings

    diagnostics = diagnostics_from_messages(warnings=tuple(warnings), losses=(), source="x")
    comment_diagnostics = [item for item in diagnostics if item.code == "html-comment-dropped"]
    assert len(comment_diagnostics) == 1
    assert comment_diagnostics[0].category == "content_loss"


# --- content HTML still blocks; functional cfxmark:img comment untouched --------


@pytest.mark.parametrize(
    "markdown",
    [
        "Alpha\n\n<div>text</div>\n\nBeta\n",
        'Alpha <span style="x">text</span> tail\n\nBeta\n',
    ],
)
def test_content_html_still_classifies_as_html_tag_stripped(markdown: str) -> None:
    """Content HTML carries visible text, so it is not swallowed into the comment
    relaxation: it keeps its existing ``html-tag-stripped`` classification."""

    candidate = _managed_candidate(markdown)

    codes = {item.code for item in _occurrences(candidate)}
    assert "html-tag-stripped" in codes
    assert "html-comment-dropped" not in codes


def test_cfxmark_img_functional_comment_preserves_dimensions() -> None:
    """The ``cfxmark:img`` functional comment is consumed in pre-processing before
    the generic drop path, so image dimensions round-trip and no comment diagnostic
    is raised."""

    markdown = "![alt](img.png)<!-- cfxmark:img w=640 h=480 -->\n"

    document, warnings = parse_md(markdown)
    assert "HTML comment dropped" not in warnings

    candidate = cfxmark.to_cfx_artifact(markdown, options=EDITABLE)
    assert candidate.push_safe is True
    assert 'ac:width="640"' in candidate.xhtml
    assert 'ac:height="480"' in candidate.xhtml
    assert "html-comment-dropped" not in {diagnostic.code for diagnostic in candidate.diagnostics}
