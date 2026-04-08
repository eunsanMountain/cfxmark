"""Golden tests for the Jira wiki renderer over synthesised corpus fixtures.

Each test reads a fixture file, converts it with to_jira_wiki, and
asserts the exact expected output (or structural property).
"""

from __future__ import annotations

import re
from pathlib import Path

from cfxmark.api import to_jira_wiki

CORPUS = Path(__file__).parent.parent / "corpus" / "inline_italic_boundary"


# ---------------------------------------------------------------------------
# Fixture: simple
# ---------------------------------------------------------------------------


def test_simple_section_renders_bullet_list():
    """fixture_simple.md with section='Story Summary' → bullet list in Jira wiki."""
    source = (CORPUS / "fixture_simple.md").read_text()
    result = to_jira_wiki(source, section="Story Summary")
    wiki = result.jira_wiki
    assert wiki is not None
    assert "Some paragraph." in wiki
    assert "* one" in wiki
    assert "* two" in wiki


# ---------------------------------------------------------------------------
# Fixture: legitimate_italic — italic survives (drop pattern not given)
# ---------------------------------------------------------------------------


def test_legitimate_italic_no_patterns_preserves_italic():
    """Italic paragraph survives when drop_leading_notice=()."""
    source = (CORPUS / "fixture_legitimate_italic.md").read_text()
    result = to_jira_wiki(source, section="Story Summary", drop_leading_notice=())
    wiki = result.jira_wiki
    assert wiki is not None
    assert "_summary single line_" in wiki


def test_legitimate_italic_non_matching_pattern_preserves_italic():
    """Italic paragraph survives when NOTICE pattern does not match."""
    source = (CORPUS / "fixture_legitimate_italic.md").read_text()
    result = to_jira_wiki(
        source,
        section="Story Summary",
        drop_leading_notice=(re.compile(r"NOTICE"),),
    )
    wiki = result.jira_wiki
    assert wiki is not None
    assert "_summary single line_" in wiki


# ---------------------------------------------------------------------------
# Fixture: notice_pattern — italic dropped when pattern matches
# ---------------------------------------------------------------------------


def test_notice_pattern_matching_drops_italic_paragraph():
    """*NOTICE: do not edit this section.* is dropped when pattern matches."""
    source = (CORPUS / "fixture_notice_pattern.md").read_text()
    result = to_jira_wiki(
        source,
        section="Story Summary",
        drop_leading_notice=(re.compile(r"NOTICE.*do not edit"),),
    )
    wiki = result.jira_wiki
    assert wiki is not None
    assert "NOTICE" not in wiki
    assert "do not edit" not in wiki
    # Body text after the notice must still be present
    assert "Body text." in wiki


# ---------------------------------------------------------------------------
# Fixture: section_missing — returns None
# ---------------------------------------------------------------------------


def test_section_missing_returns_none():
    """section='Story Summary' not found → jira_wiki is None."""
    source = (CORPUS / "fixture_section_missing.md").read_text()
    result = to_jira_wiki(source, section="Story Summary")
    assert result.jira_wiki is None
