from __future__ import annotations

import html
import subprocess
import sys

import pytest

import cfxmark
from cfxmark.exceptions import ParseError


def test_local_attachment_url_path_is_percent_decoded_before_storage_render() -> None:
    artifact = cfxmark.to_cfx_artifact("![diagram](assets/road%20map%23v1%3F100%25-%E8%A1%A8.png#cfxmark:w=640)")

    assert artifact.push_safe is True
    assert artifact.attachments == ("assets/road map#v1?100%-表.png",)
    assert 'ri:filename="road map#v1?100%-表.png"' in artifact.xhtml
    assert 'ac:width="640"' in artifact.xhtml


def test_unrepresentable_local_attachment_query_or_fragment_is_blocking() -> None:
    artifact = cfxmark.to_cfx_artifact("![diagram](assets/road.png?download=1#section)")

    assert artifact.push_safe is False
    assert any(
        diagnostic.code == "attachment-url-components-unrenderable" and diagnostic.blocking
        for diagnostic in artifact.diagnostics
    )


def test_explicit_attachment_filename_map_separates_local_path_from_remote_identity() -> None:
    options = cfxmark.ConversionOptions(
        attachment_filename_map=(("assets/road map#v1_100%-表.png", "road map#v1?100%-表.png"),)
    )

    artifact = cfxmark.to_cfx_artifact(
        "![diagram](assets/road%20map%23v1_100%25-%E8%A1%A8.png)",
        options=options,
    )

    assert artifact.push_safe is True
    assert artifact.attachments == ("assets/road map#v1_100%-表.png",)
    assert 'ri:filename="road map#v1?100%-表.png"' in artifact.xhtml


def test_unchanged_protected_table_with_attachment_marker_is_not_ambiguous() -> None:
    source = (
        '<table class="confluenceTable"><colgroup><col/></colgroup><tbody>'
        "<tr><th><p>Header</p></th></tr><tr><td><p>"
        '<ac:image xmlns:ac="http://atlassian.com/content" '
        'xmlns:ri="http://atlassian.com/resource/identifier">'
        '<ri:attachment ri:filename="diagram.png"/></ac:image>'
        "</p></td></tr></tbody></table>"
    )

    base = cfxmark.to_md_artifact(source, options=cfxmark.ConversionOptions(profile="editable"))
    candidate = cfxmark.to_cfx_artifact(
        base.markdown,
        presentation=base.presentation,
        base_artifact=base,
        splice_source=source,
        options=cfxmark.ConversionOptions(profile="editable"),
    )

    assert base.push_safe is True
    assert candidate.push_safe is True
    assert candidate.preserved_regions == candidate.required_regions
    assert not candidate.protected_region_changes
    assert not {
        "protected-region-edited",
        "protected-region-splice-incomplete",
    }.intersection(diagnostic.code for diagnostic in candidate.diagnostics)


def test_asset_markers_are_bound_to_each_image_occurrence_not_visible_url() -> None:
    artifact = cfxmark.to_cfx_artifact(
        '![a](assets/shared.png)<!-- cfxmark:asset src="remote-a.png" --> '
        '![b](assets/shared.png)<!-- cfxmark:asset src="remote-b.png" -->'
    )

    assert artifact.push_safe is True
    assert artifact.xhtml.count('ri:filename="remote-a.png"') == 1
    assert artifact.xhtml.count('ri:filename="remote-b.png"') == 1


@pytest.mark.parametrize(
    "markdown",
    (
        '![x](assets/local.png) intervening text <!-- cfxmark:asset src="remote.png" -->',
        '![x](assets/local.png)<!-- cfxmark:asset src="remote.png" --><!-- cfxmark:asset src="remote.png" -->',
        '<!-- cfxmark:asset src="remote.png" -->',
        '![x](assets/local.png)<!-- cfxmark:asset src="https://example.invalid/track.png" -->',
        '![x](assets/local.png)<!-- cfxmark:asset src="subdir/remote.png" -->',
        '![x](assets/local.png)<!-- cfxmark:asset src="%2Fetc%2Fpasswd" -->',
        '![x](assets/local.png)<!-- cfxmark:asset src="%2E%2E%2Fevil.png" -->',
        '![x](assets/local.png)<!-- cfxmark:asset src="C%3A%5CWindows%5Csecret.txt" -->',
        '![x](assets/local.png)<!-- cfxmark:asset src="https%3A%2F%2Fexample.invalid%2Fx.png" -->',
        '![x](assets/local.png)<!-- cfxmark:asset v=2 src="L2V0Yy9wYXNzd2Q" -->',
        '![x](assets/local.png)<!-- cfxmark:asset v=2 src="%%%" -->',
        '![x](assets/local.png)<!-- cfxmark:asset v=4 src="cmVtb3RlLnBuZw" -->',
        '![x](assets/local.png)<!-- cfxmark:asset v=2 src="8Y--vO-wgPOksIQlLnBuZw" -->',
        '![x](assets/local.png)<!-- cfxmark:asset v=3 src="2f6574632f706173737764" -->',
        '![x](assets/local.png)<!-- cfxmark:asset v=3 src="666F6F2E706E67" -->',
        '![x](assets/local.png)<!-- cfxmark:asset\n src="remote.png" -->',
        '![x](assets/local.png)<!-- cfxmark:asset\n v=2 src="cmVtb3RlLnBuZw" -->',
    ),
)
def test_invalid_asset_marker_binding_fails_closed(markdown: str) -> None:
    with pytest.raises(ParseError):
        cfxmark.to_cfx_artifact(markdown)


@pytest.mark.parametrize(
    "markdown",
    (
        '```md\n<!-- cfxmark:asset src="literal.png" -->\n```\n',
        '    <!-- cfxmark:asset src="literal.png" -->\n',
        '`<!-- cfxmark:asset src="literal.png" -->`\n',
        '``start\n<!-- cfxmark:asset src="literal.png" -->\nend``\n',
        '> ```md\n> <!-- cfxmark:asset src="literal.png" -->\n> ```\n',
        '>     <!-- cfxmark:asset src="literal.png" -->\n',
        '- ```md\n  <!-- cfxmark:asset src="literal.png" -->\n  ```\n',
        '-     <!-- cfxmark:asset src="literal.png" -->\n',
    ),
)
def test_asset_marker_shaped_code_is_preserved_as_user_content(markdown: str) -> None:
    artifact = cfxmark.to_cfx_artifact(markdown)

    assert artifact.push_safe is True
    assert 'cfxmark:asset src="literal.png"' in artifact.xhtml


def test_source_bound_asset_transport_hashes_share_one_markdown_domain() -> None:
    source = '<p><ac:image><ri:attachment ri:filename="remote.png"/></ac:image></p>'
    options = cfxmark.ConversionOptions(profile="editable")
    base = cfxmark.to_md_artifact(source, options=options)

    unchanged = cfxmark.to_cfx_artifact(
        base.markdown,
        presentation=base.presentation,
        base_artifact=base,
        splice_source=source,
        options=options,
    )
    assert unchanged.ownership_proof is not None
    assert (
        unchanged.ownership_proof.artifacts.base_markdown_sha256
        == unchanged.ownership_proof.artifacts.edited_markdown_sha256
    )

    edited_markdown = base.markdown.replace('src="remote.png"', 'src="other.png"')
    edited = cfxmark.to_cfx_artifact(
        edited_markdown,
        presentation=base.presentation,
        base_artifact=base,
        splice_source=source,
        options=options,
    )
    assert edited.ownership_proof is not None
    assert (
        edited.ownership_proof.artifacts.base_markdown_sha256 != edited.ownership_proof.artifacts.edited_markdown_sha256
    )
    assert edited.push_safe is False
    assert any(diagnostic.code == "asset-marker-edited" and diagnostic.blocking for diagnostic in edited.diagnostics)


@pytest.mark.parametrize(
    "remote_name",
    [
        "foo bar.png",
        "표.png",
        "foo;bar.png",
        "foo+bar.png",
        "foo@bar.png",
        "foo:bar.png",
        "C:drive.png",
        "foo(bar).png",
        "foo[bar].png",
        "foo<bar>.png",
        "foo&bar.png",
        "foo%2Fbar.png",
        "foo%5Cbar.png",
        "road map#v1?100%-表.png",
    ],
)
def test_renderer_and_parser_accept_remote_attachment_filename_without_false_migration(remote_name: str) -> None:
    source = f'<p><ac:image><ri:attachment ri:filename="{html.escape(remote_name, quote=True)}"/></ac:image></p>'
    options = cfxmark.ConversionOptions(profile="editable")

    base = cfxmark.to_md_artifact(source, options=options)
    candidate = cfxmark.to_cfx_artifact(
        base.markdown,
        presentation=base.presentation,
        base_artifact=base,
        splice_source=source,
        options=options,
    )

    assert base.push_safe is True
    assert candidate.push_safe is True
    assert f'ri:filename="{html.escape(remote_name, quote=True)}"' in candidate.xhtml
    assert base.attachments == (remote_name,)
    assert candidate.attachments == (remote_name,)

    direct_markdown = (
        base.markdown
        if "%" in remote_name
        else f'![diagram](assets/local.png)<!-- cfxmark:asset src="{remote_name}" -->'
    )
    direct = cfxmark.to_cfx_artifact(direct_markdown)
    assert direct.push_safe is True
    expected_local_path = remote_name if "%" in remote_name else "assets/local.png"
    assert direct.attachments == (expected_local_path,)
    assert f'ri:filename="{html.escape(remote_name, quote=True)}"' in direct.xhtml


def test_source_bound_asset_marker_identity_swap_is_fatal() -> None:
    source = (
        '<p><ac:image ac:alt="A"><ri:attachment ri:filename="a.png"/></ac:image></p>'
        '<p><ac:image ac:alt="B"><ri:attachment ri:filename="b.png"/></ac:image></p>'
    )
    options = cfxmark.ConversionOptions(profile="editable")
    base = cfxmark.to_md_artifact(source, options=options)
    edited_markdown = (
        base.markdown.replace('src="a.png"', 'src="swap.tmp"')
        .replace('src="b.png"', 'src="a.png"')
        .replace('src="swap.tmp"', 'src="b.png"')
    )

    candidate = cfxmark.to_cfx_artifact(
        edited_markdown,
        presentation=base.presentation,
        base_artifact=base,
        splice_source=source,
        options=options,
    )

    assert candidate.push_safe is False
    assert any(diagnostic.code == "asset-marker-edited" and diagnostic.blocking for diagnostic in candidate.diagnostics)


@pytest.mark.parametrize(
    "visible_src",
    [
        "assets/remote.png?download=1",
        "assets/remote.png#section",
        "https://example.test/remote.png",
        "file:///tmp/remote.png",
        "/tmp/remote.png",
        "../remote.png",
        r"<assets/foo\\bar.png>",
        "https%3A//example.test/remote.png",
        "file%3A///tmp/remote.png",
        "C%3A/Windows/remote.png",
        "%2Ftmp/remote.png",
        "%2E%2E/remote.png",
        "assets/remote.png%3Fdownload=1",
        "assets/remote.png%23section",
    ],
)
def test_marker_bound_transport_must_be_a_plain_relative_local_path(visible_src: str) -> None:
    markdown = f'![x]({visible_src})<!-- cfxmark:asset src="remote.png" -->\n'

    with pytest.raises(cfxmark.ParseError, match="asset marker transport"):
        cfxmark.to_cfx_artifact(markdown)


def test_asset_marker_encoding_distinguishes_quote_from_literal_percent_escape() -> None:
    options = cfxmark.ConversionOptions(profile="editable")
    markdown_by_name: dict[str, str] = {}

    for remote_name in ('foo"bar.png', "foo%22bar.png"):
        source = f'<p><ac:image><ri:attachment ri:filename="{html.escape(remote_name, quote=True)}"/></ac:image></p>'
        base = cfxmark.to_md_artifact(source, options=options)
        candidate = cfxmark.to_cfx_artifact(
            base.markdown,
            presentation=base.presentation,
            base_artifact=base,
            splice_source=source,
            options=options,
        )

        assert base.push_safe is True
        assert candidate.push_safe is True
        assert base.attachments == (remote_name,)
        assert f'ri:filename="{html.escape(remote_name, quote=True)}"' in candidate.xhtml
        assert not candidate.edit_operations
        markdown_by_name[remote_name] = base.markdown

    assert markdown_by_name['foo"bar.png'] != markdown_by_name["foo%22bar.png"]


@pytest.mark.parametrize(
    "remote_name",
    [
        "foo--bar.png",
        "foo-->bar.png",
        "x-->**INJECTED**<!--.png",
        "🚫--payload.png",
    ],
)
def test_asset_marker_encoding_is_markdown_comment_safe(remote_name: str) -> None:
    source = f'<p><ac:image><ri:attachment ri:filename="{html.escape(remote_name, quote=True)}"/></ac:image></p>'
    options = cfxmark.ConversionOptions(profile="editable")
    base = cfxmark.to_md_artifact(source, options=options)
    candidate = cfxmark.to_cfx_artifact(
        base.markdown,
        presentation=base.presentation,
        base_artifact=base,
        splice_source=source,
        options=options,
    )
    rendered = subprocess.run(
        [sys.executable, "-c", "import mistletoe,sys; print(mistletoe.markdown(sys.stdin.read()))"],
        input=base.markdown,
        text=True,
        capture_output=True,
        check=True,
    ).stdout
    payload = base.markdown.split('src="', 1)[1].split('"', 1)[0]

    assert '<!-- cfxmark:asset v=3 src="' in base.markdown
    assert "--" not in payload
    assert "&lt;!--" not in rendered
    assert "<strong>INJECTED</strong>" not in rendered
    assert base.push_safe is True
    assert candidate.push_safe is True
    assert not candidate.edit_operations


@pytest.mark.parametrize("remote_name", ["foo#cfxmark:w=640", "foo&cfxmark:h=12.png"])
def test_typed_attachment_filename_cannot_inject_image_metadata(remote_name: str) -> None:
    source = f'<p><ac:image><ri:attachment ri:filename="{html.escape(remote_name, quote=True)}"/></ac:image></p>'
    options = cfxmark.ConversionOptions(profile="editable")
    base = cfxmark.to_md_artifact(source, options=options)
    candidate = cfxmark.to_cfx_artifact(
        base.markdown,
        presentation=base.presentation,
        base_artifact=base,
        splice_source=source,
        options=options,
    )

    assert base.push_safe is True
    assert candidate.push_safe is True
    assert "ac:width" not in candidate.xhtml
    assert "ac:height" not in candidate.xhtml
    assert not candidate.edit_operations


def test_adjacent_image_metadata_remains_authoritative_for_typed_attachment() -> None:
    remote_name = "foo#cfxmark:w=123"
    source = (
        '<p><ac:image ac:width="640"><ri:attachment '
        f'ri:filename="{html.escape(remote_name, quote=True)}"/></ac:image></p>'
    )
    options = cfxmark.ConversionOptions(profile="editable")
    base = cfxmark.to_md_artifact(source, options=options)
    candidate = cfxmark.to_cfx_artifact(
        base.markdown,
        presentation=base.presentation,
        base_artifact=base,
        splice_source=source,
        options=options,
    )

    assert "<!-- cfxmark:img w=640 -->" in base.markdown
    assert candidate.push_safe is True
    assert 'ac:width="640"' in candidate.xhtml
    assert 'ri:filename="foo#cfxmark:w=123"' in candidate.xhtml
    assert not candidate.edit_operations


def test_source_bound_asset_marker_allows_alt_edit_move_and_whole_image_delete() -> None:
    source = (
        '<p><ac:image ac:alt="A"><ri:attachment ri:filename="a.png"/></ac:image></p>'
        '<p><ac:image ac:alt="B"><ri:attachment ri:filename="b.png"/></ac:image></p>'
    )
    options = cfxmark.ConversionOptions(profile="editable")
    base = cfxmark.to_md_artifact(source, options=options)
    first, second = base.markdown.strip().split("\n\n")

    for edited_markdown in (
        base.markdown.replace("![A]", "![Changed]"),
        f"{second}\n\n{first}\n",
        f"{second}\n",
    ):
        candidate = cfxmark.to_cfx_artifact(
            edited_markdown,
            presentation=base.presentation,
            base_artifact=base,
            splice_source=source,
            options=options,
        )
        assert candidate.push_safe is True
        assert not any(diagnostic.code == "asset-marker-edited" for diagnostic in candidate.diagnostics)
