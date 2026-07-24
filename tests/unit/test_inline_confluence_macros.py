from __future__ import annotations

import cfxmark

EDITABLE = cfxmark.ConversionOptions(profile="editable")


def _to_editable(xhtml: str) -> cfxmark.ConversionResult:
    return cfxmark.to_md(xhtml, options=EDITABLE)


def _round_trip(markdown: str) -> str:
    storage = cfxmark.to_cfx(markdown).xhtml or ""
    return _to_editable(storage).markdown or ""


def test_paragraph_only_toc_is_promoted_to_block_directive() -> None:
    xhtml = (
        '<p><ac:structured-macro ac:name="toc" ac:schema-version="1">'
        '<ac:parameter ac:name="maxLevel">3</ac:parameter>'
        "</ac:structured-macro></p>"
    )

    result = _to_editable(xhtml)

    assert result.markdown == '::: toc maxLevel="3"\n:::\n'
    assert result.push_safe is True
    assert _round_trip(result.markdown) == result.markdown


def test_inline_jira_key_is_readable_and_push_safe() -> None:
    xhtml = (
        '<p>See <ac:structured-macro ac:name="jira">'
        '<ac:parameter ac:name="key">DOC-123</ac:parameter>'
        "</ac:structured-macro> now.</p>"
    )

    result = _to_editable(xhtml)

    assert result.markdown == 'See {{jira key="DOC-123"}} now.\n'
    assert result.push_safe is True
    assert "cfxmark:unsupported" not in result.markdown
    assert _round_trip(result.markdown) == result.markdown


def test_inline_jira_preserves_jql_parameters_and_escaping() -> None:
    xhtml = (
        '<p>Count <ac:structured-macro ac:name="jira">'
        '<ac:parameter ac:name="jqlQuery">'
        "project = DOC AND summary ~ &quot;alpha*&quot;"
        "</ac:parameter>"
        '<ac:parameter ac:name="count">true</ac:parameter>'
        "</ac:structured-macro>.</p>"
    )

    result = _to_editable(xhtml)

    assert result.markdown == ('Count {{jira jqlQuery="project = DOC AND summary ~ \\"alpha*\\"" count="true"}}.\n')
    assert result.push_safe is True
    assert _round_trip(result.markdown) == result.markdown


def test_content_wrapper_around_cell_jira_is_transparent() -> None:
    xhtml = (
        "<table><tbody><tr>"
        '<td><div class="content-wrapper">'
        '<ac:structured-macro ac:name="jira">'
        '<ac:parameter ac:name="key">DOC-456</ac:parameter>'
        "</ac:structured-macro>"
        "</div></td>"
        "<td>Text</td>"
        "</tr></tbody></table>"
    )

    result = _to_editable(xhtml)

    assert '{{jira key="DOC-456"}}' in result.markdown
    assert result.push_safe is False
    assert result.losses == ("body-only tables require a synthetic Markdown header row",)
    assert "cfxmark:unsupported" not in result.markdown
    assert _round_trip(result.markdown) == result.markdown


def test_styled_cell_div_remains_unsupported() -> None:
    xhtml = (
        "<table><tbody><tr>"
        '<td><div class="content-wrapper" style="text-align:center">'
        '<ac:structured-macro ac:name="jira">'
        '<ac:parameter ac:name="key">DOC-789</ac:parameter>'
        "</ac:structured-macro>"
        "</div></td>"
        "<td>Text</td>"
        "</tr></tbody></table>"
    )

    result = _to_editable(xhtml)

    assert result.push_safe is False
    assert "cfxmark:unsupported" in result.markdown


def test_nested_table_remains_unsupported() -> None:
    xhtml = (
        "<table><tbody><tr><td>Outer</td><td>"
        "<table><tbody><tr><td>Inner</td></tr></tbody></table>"
        "</td></tr></tbody></table>"
    )

    result = _to_editable(xhtml)

    assert result.push_safe is False
    assert "cfxmark:unsupported" in result.markdown


def test_inline_jira_syntax_renders_through_registered_handler() -> None:
    markdown = 'Related: {{jira key="DOC-321"}}.\n'

    result = cfxmark.to_cfx(markdown)

    assert '<ac:structured-macro ac:name="jira"' in (result.xhtml or "")
    assert '<ac:parameter ac:name="key">DOC-321</ac:parameter>' in (result.xhtml or "")
    assert _round_trip(markdown) == markdown


def test_inline_code_that_looks_like_jira_syntax_stays_code() -> None:
    markdown = 'Literal `{{jira key="DOC-1"}}` only.\n'

    result = cfxmark.to_cfx(markdown)

    assert result.xhtml == '<p>Literal <code>{{jira key="DOC-1"}}</code> only.</p>'


def test_inline_jira_has_readable_jira_wiki_fallback() -> None:
    markdown = 'Related: {{jira key="DOC-321"}}.\n'

    result = cfxmark.to_jira_wiki(markdown, input_format="markdown")

    assert result.jira_wiki == markdown
    assert result.warnings == ()


def test_readable_profile_keeps_inline_jira_semantics() -> None:
    xhtml = (
        '<p><ac:structured-macro ac:name="jira">'
        '<ac:parameter ac:name="key">DOC-654</ac:parameter>'
        "</ac:structured-macro></p>"
    )

    result = cfxmark.to_md(xhtml, options=cfxmark.ConversionOptions(profile="readable"))

    assert result.markdown == '{{jira key="DOC-654"}}\n'
    assert result.push_safe is False
