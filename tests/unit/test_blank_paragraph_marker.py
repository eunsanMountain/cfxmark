from __future__ import annotations

import cfxmark

EDITABLE = cfxmark.ConversionOptions(profile="editable")
MARKER = "<!-- cfxmark:blank -->"


def _markdown(storage: str) -> str:
    result = cfxmark.to_md(storage, options=EDITABLE)
    assert result.markdown is not None
    return result.markdown


def _storage(markdown: str) -> str:
    result = cfxmark.to_cfx(markdown, options=EDITABLE)
    assert result.xhtml is not None
    return result.xhtml


def test_bare_and_break_empty_paragraphs_have_distinct_fixed_point_spellings() -> None:
    bare_markdown = _markdown("<p/>")
    break_markdown = _markdown("<p><br/></p>")

    assert bare_markdown.strip() == MARKER
    assert MARKER not in break_markdown
    assert _storage(bare_markdown) == "<p/>"
    assert _storage(break_markdown) == "<p><br/></p>"


def test_author_blank_lines_do_not_mint_storage_empty_paragraphs() -> None:
    markdown = "alpha\n\n\nbravo\n"

    assert MARKER not in markdown
    assert _storage(markdown) == "<p>alpha</p><p>bravo</p>"


def test_blank_marker_round_trips_in_structured_containers() -> None:
    samples = (
        "<ul><li><p/></li></ul>",
        "<blockquote><p>before</p><p/><p>after</p></blockquote>",
        (
            '<ac:structured-macro ac:name="expand" ac:schema-version="1">'
            "<ac:rich-text-body><p/></ac:rich-text-body>"
            "</ac:structured-macro>"
        ),
    )

    for storage in samples:
        markdown = _markdown(storage)
        assert MARKER in markdown
        assert _storage(markdown) == storage


def test_table_cell_empty_paragraph_stays_on_the_existing_loss_path() -> None:
    storage = "<table><tbody><tr><td><p/></td></tr></tbody></table>"
    result = cfxmark.to_md(storage, options=EDITABLE)

    assert result.markdown is not None
    assert MARKER not in result.markdown
    assert result.losses


def test_marker_is_control_syntax_only_as_its_own_block_and_never_eats_prose() -> None:
    """A marker can be nested in a structured container, but cannot turn an
    inline HTML comment into an empty paragraph or silently erase its neighbours.
    """

    fenced = f"```text\n{MARKER}\n```\n"
    prose = f"before {MARKER} after\n"

    assert "<ac:plain-text-body><![CDATA[" in _storage(fenced)
    result = cfxmark.to_cfx(prose, options=EDITABLE)
    assert result.xhtml is not None
    assert "before" in result.xhtml
    assert "after" in result.xhtml
    assert "<p/>" not in result.xhtml
    assert "HTML comment dropped" in result.losses
    artifact = cfxmark.to_cfx_artifact(prose, options=EDITABLE)
    assert any(diagnostic.code == "html-comment-dropped" for diagnostic in artifact.diagnostics)


def test_attributed_empty_paragraph_is_not_silently_downgraded_to_the_bare_marker() -> None:
    for storage in ('<p style="text-align: center;"></p>', '<p data-owner="author"></p>'):
        result = cfxmark.to_md(storage, options=EDITABLE)
        assert result.markdown is not None
        assert MARKER not in result.markdown
