from __future__ import annotations

import pytest

import cfxmark

EDITABLE = cfxmark.ConversionOptions(profile="editable")


def test_storage_image_metadata_emits_one_canonical_adjacent_comment() -> None:
    storage = (
        '<p><ac:image ac:alt="diagram" ac:width="320" ac:height="180" '
        'ac:thumbnail="true" ac:align="right">'
        '<ri:attachment ri:filename="diagram.png"/></ac:image></p>'
    )

    markdown = cfxmark.to_md(storage, options=EDITABLE).markdown

    assert markdown == (
        "![diagram](diagram.png)"
        "<!-- cfxmark:img w=320 h=180 thumbnail=1 align=right -->"
        '<!-- cfxmark:asset src="diagram.png" -->\n'
    )


def test_default_image_metadata_omits_img_comment() -> None:
    storage = '<p><ac:image ac:alt="plain"><ri:attachment ri:filename="plain.png"/></ac:image></p>'

    markdown = cfxmark.to_md(storage, options=EDITABLE).markdown

    assert markdown == '![plain](plain.png)<!-- cfxmark:asset src="plain.png" -->\n'
    assert "cfxmark:img" not in markdown


def test_legacy_fragment_input_is_accepted_but_reemits_only_the_comment() -> None:
    legacy = "![diagram](diagram.png#cfxmark:w=320,h=180,thumbnail=1,align=center)\n"

    storage = cfxmark.to_cfx(legacy, options=EDITABLE).xhtml
    markdown = cfxmark.to_md(storage or "", options=EDITABLE).markdown

    assert storage is not None
    assert 'ac:width="320"' in storage
    assert 'ac:height="180"' in storage
    assert 'ac:thumbnail="true"' in storage
    assert 'ac:align="center"' in storage
    assert markdown == (
        "![diagram](diagram.png)"
        "<!-- cfxmark:img w=320 h=180 thumbnail=1 align=center -->"
        '<!-- cfxmark:asset src="diagram.png" -->\n'
    )
    assert "#cfxmark:" not in markdown


def test_legacy_fragment_with_asset_marker_keeps_visible_link_metadata() -> None:
    markdown = '![diagram](remote.png#cfxmark:w=640,h=320)<!-- cfxmark:asset src="remote.png" -->\n'

    candidate = cfxmark.to_cfx_artifact(markdown, options=EDITABLE)

    assert candidate.push_safe is True
    assert 'ac:width="640"' in candidate.xhtml
    assert 'ac:height="320"' in candidate.xhtml
    assert 'ri:filename="remote.png"' in candidate.xhtml


def test_legacy_fragment_with_asset_marker_conflict_fails_closed() -> None:
    markdown = '![diagram](remote.png#cfxmark:w=123)<!-- cfxmark:img w=640 --><!-- cfxmark:asset src="remote.png" -->\n'

    with pytest.raises(cfxmark.ParseError, match="conflicts with the legacy URL fragment"):
        cfxmark.to_cfx_artifact(markdown, options=EDITABLE)


def test_legacy_fragment_with_renamed_asset_target_fails_closed() -> None:
    markdown = '![diagram](unrelated.png#cfxmark:w=640)<!-- cfxmark:asset src="remote.png" -->\n'

    with pytest.raises(cfxmark.ParseError, match="target is ambiguous"):
        cfxmark.to_cfx_artifact(markdown, options=EDITABLE)


@pytest.mark.parametrize(
    "markdown",
    [
        "![x](x.png)<!-- cfxmark:img w=1 --><!-- cfxmark:img w=1 -->\n",
        "![x](x.png#cfxmark:w=1)<!-- cfxmark:img w=2 -->\n",
        "![x](x.png)<!-- cfxmark:img w=1 w=1 -->\n",
        "![x](x.png)<!-- cfxmark:img w=wide -->\n",
        "![x](x.png)<!-- cfxmark:img thumbnail=2 -->\n",
        "![x](x.png)<!-- cfxmark:img align=left -->\n",
        "![x](x.png)<!-- cfxmark:img unknown=1 -->\n",
        "![x](x.png)<!-- cfxmark:img w=1,h=2 -->\n",
        "text<!-- cfxmark:img w=1 -->\n",
    ],
)
def test_invalid_duplicate_conflicting_or_unbound_img_marker_fails_closed(markdown: str) -> None:
    with pytest.raises(cfxmark.ParseError, match="image metadata"):
        cfxmark.to_cfx(markdown, options=EDITABLE)


def test_duplicate_same_url_occurrences_keep_distinct_metadata() -> None:
    markdown = (
        "![first](https://example.test/image.png)<!-- cfxmark:img w=100 align=center --> "
        "![second](https://example.test/image.png)<!-- cfxmark:img w=200 align=right -->\n"
    )

    storage = cfxmark.to_cfx(markdown, options=EDITABLE).xhtml

    assert storage is not None
    assert storage.count('ri:value="https://example.test/image.png"') == 2
    assert storage.index('ac:width="100"') < storage.index('ac:width="200"')
    assert storage.index('ac:align="center"') < storage.index('ac:align="right"')
    assert cfxmark.to_md(storage, options=EDITABLE).markdown == markdown


def test_image_metadata_comment_round_trips_inside_table_cell() -> None:
    markdown = (
        "| Image |\n"
        "| --- |\n"
        "| ![diagram](diagram.png)<!-- cfxmark:img w=320 thumbnail=1 align=center -->"
        '<!-- cfxmark:asset src="diagram.png" --> |\n'
    )

    storage = cfxmark.to_cfx(markdown, options=EDITABLE).xhtml

    assert storage is not None
    assert 'ac:width="320"' in storage
    assert 'ac:thumbnail="true"' in storage
    assert 'ac:align="center"' in storage
    assert cfxmark.to_md(storage, options=EDITABLE).markdown == markdown
