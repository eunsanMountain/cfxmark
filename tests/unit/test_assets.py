"""Image asset marker + resolve_assets tests."""

from __future__ import annotations

import html
import os
from pathlib import Path
from urllib.parse import unquote

import pytest

import cfxmark
from cfxmark import AssetSecurityError
from cfxmark.opaque import (
    find_asset_marker_occurrences,
    opaque_id_for,
    serialize_inline_opaque,
    serialize_opaque,
    serialize_payloads,
)

PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a"  # PNG signature
    "0000000d49484452"  # IHDR chunk header
    "00000001000000010802000000"  # 1x1 RGB
    "9077533de0"  # CRC
    "0000000049454e44ae426082"  # IEND
)


def fetcher_returning(data: bytes):
    return lambda name: data


def test_to_md_emits_asset_marker_for_local_image() -> None:
    cfx = '<p><ac:image><ri:attachment ri:filename="img.png"/></ac:image></p>'
    md = cfxmark.to_md(cfx).markdown
    assert '<!-- cfxmark:asset src="img.png" -->' in md


def test_to_md_no_marker_for_external_url() -> None:
    cfx = '<p><ac:image><ri:url ri:value="https://example.com/x.png"/></ac:image></p>'
    md = cfxmark.to_md(cfx).markdown
    assert "cfxmark:asset" not in md


def test_resolve_assets_inline_embeds_data_uri() -> None:
    cfx = '<p><ac:image><ri:attachment ri:filename="img.png"/></ac:image></p>'
    md = cfxmark.to_md(cfx).markdown
    out = cfxmark.resolve_assets(md, fetcher_returning(PNG_BYTES), mode="inline")
    assert "data:image/png;base64," in out
    # Marker is preserved so the operation is idempotent.
    assert '<!-- cfxmark:asset src="img.png" -->' in out
    candidate = cfxmark.to_cfx_artifact(out)
    assert candidate.attachments == ()
    assert 'ri:filename="img.png"' in candidate.xhtml


def test_artifact_attachment_merge_uses_actual_native_remote_identity() -> None:
    def opaque(remote_name: str) -> str:
        return serialize_opaque(f'<custom-block><ri:attachment ri:filename="{remote_name}"/></custom-block>')

    default = cfxmark.ConversionOptions()
    mapped = cfxmark.ConversionOptions(attachment_filename_map=(("assets/foo.png", "bar.png"),))
    typed_local = '![native](assets/foo.png)<!-- cfxmark:asset src="bar.png" -->'
    typed_data = '![native](data:image/png;base64,AA==)<!-- cfxmark:asset src="bar.png" -->'
    cases = (
        ("untyped-same", "![native](assets/foo.png)\n\n" + opaque("foo.png"), default, ("assets/foo.png",)),
        ("mapped-same", "![native](assets/foo.png)\n\n" + opaque("bar.png"), mapped, ("assets/foo.png",)),
        (
            "mapped-distinct",
            "![native](assets/foo.png)\n\n" + opaque("foo.png"),
            mapped,
            ("assets/foo.png", "foo.png"),
        ),
        ("typed-distinct", typed_local + "\n\n" + opaque("foo.png"), default, ("assets/foo.png", "foo.png")),
        ("data-same", typed_data + "\n\n" + opaque("bar.png"), default, ()),
        ("data-distinct", typed_data + "\n\n" + opaque("foo.png"), default, ("foo.png",)),
    )

    for label, markdown, options, expected in cases:
        candidate = cfxmark.to_cfx_artifact(markdown, options=options)
        assert candidate.push_safe is True, label
        assert candidate.attachments == expected, label


def test_resolve_assets_sidecar_writes_files(tmp_path: Path) -> None:
    cfx = '<p><ac:image><ri:attachment ri:filename="img.png"/></ac:image></p>'
    md = cfxmark.to_md(cfx).markdown

    asset_dir = tmp_path / "assets"
    out = cfxmark.resolve_assets(
        md,
        fetcher_returning(PNG_BYTES),
        mode="sidecar",
        asset_dir=asset_dir,
        md_path=tmp_path / "doc.md",
    )
    assert (asset_dir / "img.png").read_bytes() == PNG_BYTES
    # Visible link uses relative path.
    assert "(assets/img.png)" in out
    # Marker still references original filename.
    assert '<!-- cfxmark:asset src="img.png" -->' in out


def test_round_trip_after_resolve_preserves_original_filename(tmp_path: Path) -> None:
    """A resolved sidecar md should still ``to_cfx`` back to the
    ORIGINAL Confluence filename, not the relative sidecar path."""

    cfx = '<p><ac:image><ri:attachment ri:filename="img.png"/></ac:image></p>'
    md = cfxmark.to_md(cfx).markdown
    resolved = cfxmark.resolve_assets(
        md,
        fetcher_returning(PNG_BYTES),
        mode="sidecar",
        asset_dir=tmp_path / "assets",
        md_path=tmp_path / "doc.md",
    )
    candidate = cfxmark.to_cfx_artifact(resolved)
    back = candidate.xhtml
    assert 'ri:filename="img.png"' in back
    assert 'ri:filename="assets/img.png"' not in back
    assert candidate.attachments == ("assets/img.png",)


def test_resolve_assets_preserves_canonical_image_metadata_comment(tmp_path: Path) -> None:
    md = (
        "![diagram](diagram.png)<!-- cfxmark:img w=320 h=180 thumbnail=1 align=center -->"
        '<!-- cfxmark:asset src="diagram.png" -->\n'
    )

    resolved = cfxmark.resolve_assets(
        md,
        fetcher_returning(PNG_BYTES),
        mode="sidecar",
        asset_dir=tmp_path / "assets",
        md_path=tmp_path / "page.md",
    )

    assert "(assets/diagram.png)<!-- cfxmark:img w=320 h=180 thumbnail=1 align=center -->" in resolved
    assert resolved.index("cfxmark:img") < resolved.index("cfxmark:asset")


def test_resolve_assets_skips_when_fetcher_returns_none() -> None:
    cfx = '<p><ac:image><ri:attachment ri:filename="img.png"/></ac:image></p>'
    md = cfxmark.to_md(cfx).markdown
    out = cfxmark.resolve_assets(md, lambda name: None, mode="inline")
    # Marker preserved, link unchanged.
    assert "(img.png)" in out or "(img.png#" in out
    assert '<!-- cfxmark:asset src="img.png" -->' in out
    assert "data:image" not in out


@pytest.mark.parametrize(
    "remote_name",
    [
        'foo"bar.png',
        "foo%22bar.png",
        "foo%2Fbar.png",
        "foo%23bar.png",
        "foo#bar.png",
        "foo?bar.png",
        "foo bar.png",
        "foo(bar).png",
        "foo)bar.png",
        "foo<bar>.png",
        "표 image.png",
    ],
)
def test_resolve_assets_preserves_remote_identity_and_markdown_grammar(tmp_path: Path, remote_name: str) -> None:
    source = (
        '<p><ac:image ac:alt="Alt label" ac:title="Title label">'
        f'<ri:attachment ri:filename="{html.escape(remote_name, quote=True)}"/></ac:image></p>'
    )
    options = cfxmark.ConversionOptions(profile="editable")
    base = cfxmark.to_md_artifact(source, options=options)
    fetched: list[str] = []

    def fetcher(name: str) -> bytes:
        fetched.append(name)
        return PNG_BYTES

    asset_dir = tmp_path / "assets"
    resolved = cfxmark.resolve_assets(
        base.markdown,
        fetcher,
        mode="sidecar",
        asset_dir=asset_dir,
        md_path=tmp_path / "page.md",
    )
    candidate = cfxmark.to_cfx_artifact(
        resolved,
        presentation=base.presentation,
        base_artifact=base,
        splice_source=source,
        options=options,
    )
    occurrence = find_asset_marker_occurrences(resolved)[0]

    assert fetched == [remote_name]
    assert (asset_dir / remote_name).read_bytes() == PNG_BYTES
    assert unquote(occurrence.link.target) == f"assets/{remote_name}"
    assert candidate.push_safe is True
    assert f'ri:filename="{html.escape(remote_name, quote=True)}"' in candidate.xhtml
    assert candidate.attachments == (f"assets/{remote_name}",)
    assert 'ac:alt="Alt label"' in candidate.xhtml
    assert 'ac:title="Title label"' in candidate.xhtml
    assert "ac:width" not in candidate.xhtml
    assert "ac:height" not in candidate.xhtml


@pytest.mark.parametrize("remote_name", ["foo#cfxmark:w=640", "foo&cfxmark:h=12.png"])
def test_resolve_assets_keeps_typed_filename_separate_from_image_metadata(remote_name: str) -> None:
    source = (
        '<p><ac:image ac:alt="Alt label">'
        f'<ri:attachment ri:filename="{html.escape(remote_name, quote=True)}"/></ac:image></p>'
    )
    options = cfxmark.ConversionOptions(profile="editable")
    base = cfxmark.to_md_artifact(source, options=options)
    fetched: list[str] = []

    def fetcher(name: str) -> bytes:
        fetched.append(name)
        return PNG_BYTES

    # The permissive mode exercises the parser/marker authority boundary;
    # strict mode rejects the colon before the fetcher (covered below).
    resolved = cfxmark.resolve_assets(base.markdown, fetcher, mode="inline", strict_filenames=False)
    candidate = cfxmark.to_cfx_artifact(
        resolved,
        presentation=base.presentation,
        base_artifact=base,
        splice_source=source,
        options=options,
    )

    assert fetched == [remote_name]
    assert candidate.push_safe is True
    assert f'ri:filename="{html.escape(remote_name, quote=True)}"' in candidate.xhtml
    assert "ac:width" not in candidate.xhtml
    assert "ac:height" not in candidate.xhtml
    assert not candidate.edit_operations


def test_sidecar_resolution_preserves_typed_filename_and_explicit_metadata(tmp_path: Path) -> None:
    remote_name = "foo#cfxmark:w=123"
    source = f'<p><ac:image ac:width="640"><ri:attachment ri:filename="{remote_name}"/></ac:image></p>'
    options = cfxmark.ConversionOptions(profile="editable")
    base = cfxmark.to_md_artifact(source, options=options)
    resolved = cfxmark.resolve_assets(
        base.markdown,
        lambda name: PNG_BYTES if name == remote_name else None,
        mode="sidecar",
        asset_dir=tmp_path / "assets",
        md_path=tmp_path / "page.md",
        strict_filenames=False,
    )
    candidate = cfxmark.to_cfx_artifact(
        resolved,
        presentation=base.presentation,
        base_artifact=base,
        splice_source=source,
        options=options,
    )

    assert candidate.push_safe is True
    assert 'ac:width="640"' in candidate.xhtml
    assert 'ri:filename="foo#cfxmark:w=123"' in candidate.xhtml
    assert candidate.attachments == (f"assets/{remote_name}",)
    assert not candidate.edit_operations


@pytest.mark.parametrize(
    "invalid_marker",
    [
        '![second](unrelated.png#cfxmark:w=640)<!-- cfxmark:asset src="b.png" -->',
        '![second](b.png)<!-- cfxmark:asset src="../b.png" -->',
    ],
)
def test_resolve_assets_validates_all_occurrences_before_fetch(tmp_path: Path, invalid_marker: str) -> None:
    markdown = f'![first](a.png)<!-- cfxmark:asset src="a.png" -->\n\n{invalid_marker}\n'
    fetched: list[str] = []
    asset_dir = tmp_path / "assets"

    with pytest.raises((cfxmark.ParseError, AssetSecurityError)):
        cfxmark.resolve_assets(
            markdown,
            lambda name: fetched.append(name) or PNG_BYTES,
            mode="sidecar",
            asset_dir=asset_dir,
        )

    assert fetched == []
    assert not asset_dir.exists()


@pytest.mark.parametrize(
    "visible_src",
    [
        "https%3A//example.test/b.png",
        "file%3A///tmp/b.png",
        "C%3A/Windows/b.png",
        "%2Ftmp/b.png",
        "%2E%2E/b.png",
        "assets/b.png%3Fdownload=1",
        "assets/b.png%23section",
    ],
)
def test_resolve_assets_validates_encoded_transport_before_any_effect(tmp_path: Path, visible_src: str) -> None:
    markdown = (
        '![first](a.png)<!-- cfxmark:asset src="a.png" -->\n\n'
        f'![second]({visible_src})<!-- cfxmark:asset src="b.png" -->\n'
    )
    fetched: list[str] = []
    asset_dir = tmp_path / "assets"

    with pytest.raises(cfxmark.ParseError, match="asset marker transport"):
        cfxmark.resolve_assets(
            markdown,
            lambda name: fetched.append(name) or PNG_BYTES,
            mode="sidecar",
            asset_dir=asset_dir,
        )

    assert fetched == []
    assert not asset_dir.exists()


@pytest.mark.parametrize("remote_name", ["foo)bar.png", "foo[bar].png"])
def test_escaped_commonmark_transport_preserves_exact_attachment_identity(tmp_path: Path, remote_name: str) -> None:
    escaped_name = remote_name.replace(")", r"\)").replace("[", r"\[").replace("]", r"\]")
    markdown = f'![x](assets/{escaped_name})<!-- cfxmark:asset src="{remote_name}" -->\n'

    resolved = cfxmark.resolve_assets(
        markdown,
        fetcher_returning(PNG_BYTES),
        mode="sidecar",
        asset_dir=tmp_path / "assets",
        md_path=tmp_path / "page.md",
    )
    candidate = cfxmark.to_cfx_artifact(resolved)

    assert (tmp_path / "assets" / remote_name).read_bytes() == PNG_BYTES
    assert candidate.attachments == (f"assets/{remote_name}",)
    assert f'ri:filename="{remote_name}"' in candidate.xhtml


@pytest.mark.parametrize("remote_name", ["foo#cfxmark:w=640", "foo&cfxmark:h=12.png"])
def test_strict_sidecar_rejects_metadata_token_filename_before_fetch(tmp_path: Path, remote_name: str) -> None:
    source = f'<p><ac:image><ri:attachment ri:filename="{html.escape(remote_name, quote=True)}"/></ac:image></p>'
    base = cfxmark.to_md_artifact(source, options=cfxmark.ConversionOptions(profile="editable"))
    fetched: list[str] = []

    with pytest.raises(AssetSecurityError):
        cfxmark.resolve_assets(
            base.markdown,
            lambda name: fetched.append(name) or PNG_BYTES,
            mode="sidecar",
            asset_dir=tmp_path / "assets",
        )

    assert fetched == []


@pytest.mark.parametrize(
    ("attribute_value", "remote_name"),
    [
        ("foo&amp;bar.png", "foo&bar.png"),
        ("foo&quot;bar.png", 'foo"bar.png'),
        ("foo&apos;bar.png", "foo'bar.png"),
        ("foo&#35;bar.png", "foo#bar.png"),
        ("foo&#x23;bar.png", "foo#bar.png"),
        ("foo&amp;quot;bar.png", "foo&quot;bar.png"),
    ],
)
def test_resolve_assets_decodes_opaque_attachment_identity_once(
    tmp_path: Path, attribute_value: str, remote_name: str
) -> None:
    source = f'<pre><code><ac:image><ri:attachment ri:filename="{attribute_value}"/></ac:image></code></pre>'
    base = cfxmark.to_md_artifact(source)
    fetched: list[str] = []

    cfxmark.resolve_assets(
        base.markdown,
        lambda name: fetched.append(name) or PNG_BYTES,
        mode="sidecar",
        asset_dir=tmp_path / "assets",
    )

    assert base.attachments == (remote_name,)
    assert fetched == [remote_name]
    assert (tmp_path / "assets" / remote_name).read_bytes() == PNG_BYTES


@pytest.mark.parametrize(
    "markdown",
    [
        '```md\n![x](local.png)<!-- cfxmark:asset src="remote.png" -->\n```\n',
        '`![x](local.png)<!-- cfxmark:asset src="remote.png" -->`\n',
        '    ![x](local.png)<!-- cfxmark:asset src="remote.png" -->\n',
    ],
)
def test_resolve_assets_does_not_touch_asset_marker_literals_in_code(tmp_path: Path, markdown: str) -> None:
    fetched: list[str] = []

    def fetcher(name: str) -> bytes:
        fetched.append(name)
        return PNG_BYTES

    resolved = cfxmark.resolve_assets(markdown, fetcher, mode="sidecar", asset_dir=tmp_path)

    assert resolved == markdown
    assert fetched == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "markdown",
    [
        '```xml\n<ri:attachment ri:filename="literal.png"/>\n```\n',
        '`<ri:attachment ri:filename="literal.png"/>`\n',
        '    <ri:attachment ri:filename="literal.png"/>\n',
        '<!-- <ri:attachment ri:filename="literal.png"/> -->\n',
    ],
)
def test_resolve_assets_ignores_opaque_attachment_literals(tmp_path: Path, markdown: str) -> None:
    fetched: list[str] = []
    asset_dir = tmp_path / "assets"

    resolved = cfxmark.resolve_assets(
        markdown,
        lambda name: fetched.append(name) or PNG_BYTES,
        mode="sidecar",
        asset_dir=asset_dir,
    )

    assert resolved == markdown
    assert fetched == []
    assert not asset_dir.exists()


def test_empty_body_directive_warns_on_dropped_body() -> None:
    """``::: jira`` (and any other parameter-only directive) silently
    dropped body content before. Now we surface a warning so the
    caller can fix their Markdown."""

    md = "::: jira\nkey: PROJ-1\n:::\n"
    result = cfxmark.to_cfx(md)
    assert any("ignores body content" in w for w in result.warnings)


def test_cdata_ri_attachment_not_enumerated() -> None:
    """A ``<ri:attachment>`` that appears *inside* a CDATA section (for
    example a Confluence ``code`` macro documenting storage XML) must
    not be reported as a real attachment."""

    xhtml = (
        '<ac:structured-macro ac:name="code">'
        "<ac:plain-text-body>"
        '<![CDATA[<ri:attachment ri:filename="example.png"/>]]>'
        "</ac:plain-text-body>"
        "</ac:structured-macro>"
    )
    assert cfxmark.to_md(xhtml).attachments == ()


def test_attachment_enumeration_ignores_non_element_xml_literals() -> None:
    xhtml = (
        '<ac:structured-macro ac:name="mystery">'
        '<!-- <ri:attachment ri:filename="comment.png"/> -->'
        '<?sample <ri:attachment ri:filename="pi.png"/> ?>'
        '<ac:plain-text-body><![CDATA[<ri:attachment ri:filename="cdata.png"/>]]></ac:plain-text-body>'
        '<ri:attachmentX ri:filename="wrong-element.png"/>'
        '<ri:attachment ri:filename="real&amp;name.png"/>'
        "</ac:structured-macro>"
    )

    assert cfxmark.to_md(xhtml).attachments == ("real&name.png",)


@pytest.mark.parametrize("inline", [False, True])
def test_resolve_assets_only_fetches_live_elements_from_authenticated_opaque(tmp_path: Path, inline: bool) -> None:
    raw_xml = (
        '<ac:structured-macro ac:name="mystery">'
        '<!-- <ri:attachment ri:filename="comment.png"/> -->'
        '<?sample <ri:attachment ri:filename="pi.png"/> ?>'
        '<ac:plain-text-body><![CDATA[<ri:attachment ri:filename="cdata.png"/>]]></ac:plain-text-body>'
        '<ri:attachmentX ri:filename="wrong-element.png"/>'
        '<ri:attachment ri:filename="real&amp;name.png"/>'
        "</ac:structured-macro>"
    )
    if inline:
        opaque_id = opaque_id_for(raw_xml)
        markdown = (
            serialize_inline_opaque("inline", opaque_id) + "\n\n" + serialize_payloads({opaque_id: raw_xml}) + "\n"
        )
    else:
        markdown = cfxmark.to_md(raw_xml).markdown
    fetched: list[str] = []

    cfxmark.resolve_assets(
        markdown,
        lambda name: fetched.append(name) or PNG_BYTES,
        mode="sidecar",
        asset_dir=tmp_path / "assets",
    )

    assert fetched == ["real&name.png"]
    assert (tmp_path / "assets" / "real&name.png").read_bytes() == PNG_BYTES
    for phantom in ("comment.png", "pi.png", "cdata.png", "wrong-element.png"):
        assert not (tmp_path / "assets" / phantom).exists()


@pytest.mark.parametrize("context", ["fenced", "inline", "indented", "comment"])
def test_authenticated_opaque_block_literal_has_no_asset_authority(tmp_path: Path, context: str) -> None:
    raw_xml = '<ac:link><ri:attachment ri:filename="literal.png"/></ac:link>'
    control = serialize_opaque(raw_xml)
    if context == "fenced":
        markdown = f"````md\n{control}\n````\n"
    elif context == "inline":
        markdown = f"Text ````{control}\n```` tail\n"
    elif context == "indented":
        markdown = "\n".join(f"    {line}" for line in control.splitlines()) + "\n"
    else:
        markdown = f"<!--\n{control}\n-->\n"
    fetched: list[str] = []
    asset_dir = tmp_path / "assets"

    resolved = cfxmark.resolve_assets(
        markdown,
        lambda name: fetched.append(name) or PNG_BYTES,
        mode="sidecar",
        asset_dir=asset_dir,
    )

    assert resolved == markdown
    assert fetched == []
    assert not asset_dir.exists()


@pytest.mark.parametrize("tag", ["div", "script", "pre"])
@pytest.mark.parametrize("control_kind", ["block", "inline"])
def test_authenticated_opaque_control_in_raw_html_has_no_asset_authority(
    tmp_path: Path, tag: str, control_kind: str
) -> None:
    raw_xml = '<ac:link><ri:attachment ri:filename="literal.png"/></ac:link>'
    if control_kind == "block":
        control = serialize_opaque(raw_xml)
    else:
        opaque_id = opaque_id_for(raw_xml)
        control = serialize_inline_opaque("inline", opaque_id) + "\n" + serialize_payloads({opaque_id: raw_xml})
    markdown = f"<{tag}>\n{control}\n</{tag}>\n\n"
    fetched: list[str] = []
    asset_dir = tmp_path / "assets"

    cfxmark.resolve_assets(
        markdown,
        lambda name: fetched.append(name) or PNG_BYTES,
        mode="sidecar",
        asset_dir=asset_dir,
    )

    assert fetched == []
    assert not asset_dir.exists()


@pytest.mark.parametrize("context", ["orphan", "referenced-from-code", "all-fenced", "all-inline-code", "comment"])
def test_unreachable_authenticated_inline_payload_has_no_asset_authority(tmp_path: Path, context: str) -> None:
    raw_xml = '<ac:link><ri:attachment ri:filename="orphan.png"/></ac:link>'
    opaque_id = opaque_id_for(raw_xml)
    ref = serialize_inline_opaque("inline", opaque_id)
    payload = serialize_payloads({opaque_id: raw_xml})
    if context == "orphan":
        markdown = payload + "\n"
    elif context == "referenced-from-code":
        markdown = f"````md\n{ref}\n````\n\n{payload}\n"
    elif context == "all-fenced":
        markdown = f"````md\n{ref}\n\n{payload}\n````\n"
    elif context == "all-inline-code":
        markdown = f"Text ````{ref}\n\n{payload}\n```` tail\n"
    else:
        markdown = f"<!--\n{ref}\n\n{payload}\n-->\n"
    fetched: list[str] = []
    asset_dir = tmp_path / "assets"

    cfxmark.resolve_assets(
        markdown,
        lambda name: fetched.append(name) or PNG_BYTES,
        mode="sidecar",
        asset_dir=asset_dir,
    )

    assert fetched == []
    assert not asset_dir.exists()


def test_authenticated_opaque_literal_remains_code_in_source_bound_candidate() -> None:
    source = (
        '<ac:structured-macro ac:name="code">'
        "<ac:plain-text-body><![CDATA[before]]></ac:plain-text-body>"
        "</ac:structured-macro>"
    )
    options = cfxmark.ConversionOptions(profile="editable")
    base = cfxmark.to_md_artifact(source, options=options)
    raw_xml = '<ac:link><ri:attachment ri:filename="literal.png"/></ac:link>'
    edited = f"````\n{serialize_opaque(raw_xml)}\n````\n"

    candidate = cfxmark.to_cfx_artifact(
        edited,
        presentation=base.presentation,
        base_artifact=base,
        splice_source=source,
        options=options,
    )

    assert candidate.push_safe is True
    assert candidate.attachments == ()
    assert "CFXMARK_OPAQUE" not in candidate.xhtml
    assert "cfxmark:opaque" in candidate.xhtml
    assert 'ri:filename="literal.png"' in candidate.xhtml


@pytest.mark.parametrize("code_prefix", ["fenced", "inline", "indented"])
@pytest.mark.parametrize("control_kind", ["block", "inline"])
def test_literal_comment_opener_in_code_does_not_disable_later_active_control(
    tmp_path: Path, code_prefix: str, control_kind: str
) -> None:
    if code_prefix == "fenced":
        prefix = "```text\n<!-- literal opener inside code\n```\n\n"
    elif code_prefix == "inline":
        prefix = "``<!-- literal opener inside code``\n\n"
    else:
        prefix = "    <!-- literal opener inside code\n\n"
    raw_xml = '<ac:link><ri:attachment ri:filename="active.png"/></ac:link>'
    if control_kind == "block":
        control = serialize_opaque(raw_xml)
    else:
        opaque_id = opaque_id_for(raw_xml)
        control = serialize_inline_opaque("active", opaque_id) + "\n\n" + serialize_payloads({opaque_id: raw_xml})
    markdown = prefix + control + "\n"
    fetched: list[str] = []

    cfxmark.resolve_assets(
        markdown,
        lambda name: fetched.append(name) or PNG_BYTES,
        mode="sidecar",
        asset_dir=tmp_path / "assets",
    )
    candidate = cfxmark.to_cfx_artifact(markdown)

    assert fetched == ["active.png"]
    assert (tmp_path / "assets" / "active.png").read_bytes() == PNG_BYTES
    assert 'ri:filename="active.png"' in candidate.xhtml
    assert candidate.attachments == ("active.png",)
    assert "CFXMARK_OPAQUE" not in candidate.xhtml


@pytest.mark.parametrize("control_kind", ["block", "inline"])
def test_invalid_backtick_fence_info_does_not_disable_later_active_control(tmp_path: Path, control_kind: str) -> None:
    raw_xml = '<ac:link><ri:attachment ri:filename="active.png"/></ac:link>'
    if control_kind == "block":
        control = serialize_opaque(raw_xml)
    else:
        opaque_id = opaque_id_for(raw_xml)
        control = serialize_inline_opaque("active", opaque_id) + "\n\n" + serialize_payloads({opaque_id: raw_xml})
    markdown = "```invalid`info\n\n" + control + "\n"
    fetched: list[str] = []

    cfxmark.resolve_assets(
        markdown,
        lambda name: fetched.append(name) or PNG_BYTES,
        mode="sidecar",
        asset_dir=tmp_path / "assets",
    )
    candidate = cfxmark.to_cfx_artifact(markdown)

    assert fetched == ["active.png"]
    assert (tmp_path / "assets" / "active.png").read_bytes() == PNG_BYTES
    assert 'ri:filename="active.png"' in candidate.xhtml
    assert candidate.attachments == ("active.png",)


def test_invalid_fence_run_with_inline_closer_preserves_literal_opaque_ref(tmp_path: Path) -> None:
    raw_xml = '<ac:link><ri:attachment ri:filename="literal.png"/></ac:link>'
    opaque_id = opaque_id_for(raw_xml)
    ref = serialize_inline_opaque("inline", opaque_id)
    markdown = f"```invalid`info\n{ref} ```\n\n{serialize_payloads({opaque_id: raw_xml})}\n"
    fetched: list[str] = []

    cfxmark.resolve_assets(
        markdown,
        lambda name: fetched.append(name) or PNG_BYTES,
        mode="sidecar",
        asset_dir=tmp_path / "assets",
    )
    candidate = cfxmark.to_cfx_artifact(markdown)

    assert fetched == []
    assert not (tmp_path / "assets").exists()
    assert candidate.attachments == ()
    assert f"cfx:{opaque_id}" in candidate.xhtml
    assert "cfxmark-inline-opaque-" not in candidate.xhtml
    assert 'ri:filename="literal.png"' not in candidate.xhtml


def test_invalid_fence_does_not_consume_immediate_canonical_opaque_block(tmp_path: Path) -> None:
    raw_xml = '<ac:link><ri:attachment ri:filename="active.png"/></ac:link>'
    markdown = "```invalid`info\n" + serialize_opaque(raw_xml) + "\n"
    fetched: list[str] = []

    cfxmark.resolve_assets(
        markdown,
        lambda name: fetched.append(name) or PNG_BYTES,
        mode="sidecar",
        asset_dir=tmp_path / "assets",
    )
    candidate = cfxmark.to_cfx_artifact(markdown)

    assert fetched == ["active.png"]
    assert (tmp_path / "assets" / "active.png").read_bytes() == PNG_BYTES
    assert candidate.attachments == ("active.png",)
    assert 'ri:filename="active.png"' in candidate.xhtml
    assert "cfx-storage" not in candidate.xhtml


@pytest.mark.parametrize("block_line", ["# heading", "> quoted", "- listed"])
def test_invalid_fence_does_not_consume_inline_ref_in_interrupting_block(tmp_path: Path, block_line: str) -> None:
    raw_xml = '<ac:link><ri:attachment ri:filename="active.png"/></ac:link>'
    opaque_id = opaque_id_for(raw_xml)
    ref = serialize_inline_opaque("inline", opaque_id)
    markdown = f"```invalid`info\n{block_line} {ref} ```\n\n{serialize_payloads({opaque_id: raw_xml})}\n"
    fetched: list[str] = []

    cfxmark.resolve_assets(
        markdown,
        lambda name: fetched.append(name) or PNG_BYTES,
        mode="sidecar",
        asset_dir=tmp_path / "assets",
    )
    candidate = cfxmark.to_cfx_artifact(markdown)

    assert fetched == ["active.png"]
    assert (tmp_path / "assets" / "active.png").read_bytes() == PNG_BYTES
    assert candidate.attachments == ("active.png",)
    assert 'ri:filename="active.png"' in candidate.xhtml
    assert "cfxmark-inline-opaque-" not in candidate.xhtml


def test_invalid_fence_does_not_consume_inline_ref_after_setext_h1(tmp_path: Path) -> None:
    raw_xml = '<ac:link><ri:attachment ri:filename="active.png"/></ac:link>'
    opaque_id = opaque_id_for(raw_xml)
    ref = serialize_inline_opaque("inline", opaque_id)
    markdown = f"```invalid`info\n===\n{ref} ```\n\n{serialize_payloads({opaque_id: raw_xml})}\n"
    fetched: list[str] = []

    cfxmark.resolve_assets(
        markdown,
        lambda name: fetched.append(name) or PNG_BYTES,
        mode="sidecar",
        asset_dir=tmp_path / "assets",
    )
    candidate = cfxmark.to_cfx_artifact(markdown)

    assert fetched == ["active.png"]
    assert (tmp_path / "assets" / "active.png").read_bytes() == PNG_BYTES
    assert candidate.attachments == ("active.png",)
    assert 'ri:filename="active.png"' in candidate.xhtml
    assert "cfxmark-inline-opaque-" not in candidate.xhtml


@pytest.mark.parametrize("literal_opener", ["<!-- literal opener inside code", "` literal opener inside code"])
def test_source_bound_fixed_point_keeps_active_control_after_literal_opener_in_code(literal_opener: str) -> None:
    source = (
        '<ac:structured-macro ac:name="code">'
        f"<ac:plain-text-body><![CDATA[{literal_opener}]]></ac:plain-text-body>"
        "</ac:structured-macro>"
        '<custom-block><ri:attachment ri:filename="active.png"/></custom-block>'
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

    assert candidate.push_safe is True
    assert candidate.attachments == ("active.png",)
    assert 'ri:filename="active.png"' in candidate.xhtml
    assert "CFXMARK_OPAQUE" not in candidate.xhtml


@pytest.mark.parametrize("literal_context", ["fenced", "comment"])
@pytest.mark.parametrize("control_kind", ["block", "inline"])
def test_literal_backtick_does_not_disable_later_active_control(
    tmp_path: Path, literal_context: str, control_kind: str
) -> None:
    if literal_context == "fenced":
        prefix = "```text\n` literal opener inside code\n```\n\n"
    else:
        prefix = "<!-- ` literal opener inside comment -->\n\n"
    raw_xml = '<ac:link><ri:attachment ri:filename="active.png"/></ac:link>'
    if control_kind == "block":
        control = serialize_opaque(raw_xml)
    else:
        opaque_id = opaque_id_for(raw_xml)
        control = serialize_inline_opaque("active", opaque_id) + "\n\n" + serialize_payloads({opaque_id: raw_xml})
    markdown = prefix + control + "\n"
    fetched: list[str] = []

    cfxmark.resolve_assets(
        markdown,
        lambda name: fetched.append(name) or PNG_BYTES,
        mode="sidecar",
        asset_dir=tmp_path / "assets",
    )
    candidate = cfxmark.to_cfx_artifact(markdown)

    assert fetched == ["active.png"]
    assert 'ri:filename="active.png"' in candidate.xhtml
    assert candidate.attachments == ("active.png",)


@pytest.mark.parametrize("raw_prefix", ["<?instruction\n", "<![CDATA[\n", "</div>\n"])
@pytest.mark.parametrize("control_kind", ["block", "inline"])
def test_authenticated_control_in_unclosed_raw_html_has_no_asset_authority(
    tmp_path: Path, raw_prefix: str, control_kind: str
) -> None:
    raw_xml = '<ac:link><ri:attachment ri:filename="literal.png"/></ac:link>'
    if control_kind == "block":
        control = serialize_opaque(raw_xml)
    else:
        opaque_id = opaque_id_for(raw_xml)
        control = serialize_inline_opaque("inline", opaque_id) + "\n" + serialize_payloads({opaque_id: raw_xml})
    fetched: list[str] = []
    asset_dir = tmp_path / "assets"

    cfxmark.resolve_assets(
        raw_prefix + control + "\n",
        lambda name: fetched.append(name) or PNG_BYTES,
        mode="sidecar",
        asset_dir=asset_dir,
    )

    assert fetched == []
    assert not asset_dir.exists()


@pytest.mark.parametrize("raw_context", ["div", "closing-div", "pre", "script", "declaration"])
def test_inline_ref_in_raw_html_cannot_claim_external_payload(tmp_path: Path, raw_context: str) -> None:
    raw_xml = '<ac:link><ri:attachment ri:filename="literal.png"/></ac:link>'
    opaque_id = opaque_id_for(raw_xml)
    ref = serialize_inline_opaque("inline", opaque_id)
    payload = serialize_payloads({opaque_id: raw_xml})
    if raw_context == "div":
        markdown = f"<div>\n{ref}\n\n{payload}\n"
    elif raw_context == "closing-div":
        markdown = f"</div>\n{ref}\n\n{payload}\n"
    elif raw_context == "pre":
        markdown = f"<pre>\n{ref}\n</pre>\n{payload}\n"
    elif raw_context == "script":
        markdown = f"<script>\n{ref}\n</script>\n{payload}\n"
    else:
        markdown = f"<!DECL\n{ref}\n>\n{payload}\n"
    fetched: list[str] = []
    asset_dir = tmp_path / "assets"

    cfxmark.resolve_assets(
        markdown,
        lambda name: fetched.append(name) or PNG_BYTES,
        mode="sidecar",
        asset_dir=asset_dir,
    )
    bare = cfxmark.to_cfx_artifact(markdown)
    source = "<p>before</p>"
    options = cfxmark.ConversionOptions(profile="editable")
    base = cfxmark.to_md_artifact(source, options=options)
    source_bound = cfxmark.to_cfx_artifact(
        base.markdown + "\n" + markdown,
        presentation=base.presentation,
        base_artifact=base,
        splice_source=source,
        options=options,
    )

    assert fetched == []
    assert not asset_dir.exists()
    assert bare.attachments == ()
    assert source_bound.attachments == ()
    assert 'ri:filename="literal.png"' not in bare.xhtml
    assert 'ri:filename="literal.png"' not in source_bound.xhtml


def test_inline_ref_after_line_start_html_comment_is_inert(tmp_path: Path) -> None:
    raw_xml = '<ac:link><ri:attachment ri:filename="literal.png"/></ac:link>'
    opaque_id = opaque_id_for(raw_xml)
    ref = serialize_inline_opaque("inline", opaque_id)
    markdown = f"<!-- raw block --> {ref}\n\n{serialize_payloads({opaque_id: raw_xml})}\n"
    fetched: list[str] = []

    cfxmark.resolve_assets(
        markdown,
        lambda name: fetched.append(name) or PNG_BYTES,
        mode="sidecar",
        asset_dir=tmp_path / "assets",
    )
    candidate = cfxmark.to_cfx_artifact(markdown)

    assert fetched == []
    assert not (tmp_path / "assets").exists()
    assert candidate.attachments == ()
    assert 'ri:filename="literal.png"' not in candidate.xhtml


def test_inline_ref_after_inline_html_comment_remains_active(tmp_path: Path) -> None:
    raw_xml = '<ac:link><ri:attachment ri:filename="active.png"/></ac:link>'
    opaque_id = opaque_id_for(raw_xml)
    ref = serialize_inline_opaque("inline", opaque_id)
    markdown = f"prefix <!-- inline --> {ref}\n\n{serialize_payloads({opaque_id: raw_xml})}\n"
    fetched: list[str] = []

    cfxmark.resolve_assets(
        markdown,
        lambda name: fetched.append(name) or PNG_BYTES,
        mode="sidecar",
        asset_dir=tmp_path / "assets",
    )
    candidate = cfxmark.to_cfx_artifact(markdown)

    assert fetched == ["active.png"]
    assert (tmp_path / "assets" / "active.png").read_bytes() == PNG_BYTES
    assert candidate.attachments == ("active.png",)
    assert 'ri:filename="active.png"' in candidate.xhtml


@pytest.mark.parametrize("control_kind", ["block", "inline"])
def test_custom_inline_html_does_not_disable_later_active_control(tmp_path: Path, control_kind: str) -> None:
    raw_xml = '<ac:link><ri:attachment ri:filename="active.png"/></ac:link>'
    if control_kind == "block":
        control = serialize_opaque(raw_xml)
    else:
        opaque_id = opaque_id_for(raw_xml)
        control = serialize_inline_opaque("active", opaque_id) + "\n\n" + serialize_payloads({opaque_id: raw_xml})
    markdown = "<x-widget>ordinary custom inline html\n" + control + "\n"
    fetched: list[str] = []

    cfxmark.resolve_assets(
        markdown,
        lambda name: fetched.append(name) or PNG_BYTES,
        mode="sidecar",
        asset_dir=tmp_path / "assets",
    )

    assert fetched == ["active.png"]


def test_to_cfx_strips_directory_from_ri_filename() -> None:
    """Confluence stores attachments in a flat per-page namespace, so
    a Markdown image that points into a sidecar directory must emit
    only the basename in ``<ri:filename>``. The original path stays
    in ``result.attachments`` so the caller knows where to upload
    from."""

    md = "![](assets/demo.png)\n"
    result = cfxmark.to_cfx(md)
    assert 'ri:filename="demo.png"' in result.xhtml
    assert 'ri:filename="assets/demo.png"' not in result.xhtml
    assert result.attachments == ("assets/demo.png",)


def test_literal_placeholder_does_not_crash_to_cfx() -> None:
    """A user typing the cfxmark internal placeholder syntax must not
    blow up the parser with an IndexError."""

    md = "before\n\n`CFXMARK_OPAQUE-0-CFXMARK`\n\nafter\n"
    cfxmark.to_cfx(md)  # must not raise

    md2 = "`CFXMARK_DIRECTIVE-99-CFXMARK`\n"
    cfxmark.to_cfx(md2)  # must not raise


def test_attachments_enumerates_opaque_references() -> None:
    """Images trapped inside ``<pre><code>`` become opaque blocks, but
    their ``ri:filename`` must still appear in ``result.attachments``
    so callers know what to upload."""

    xhtml = (
        '<p><ac:image><ri:attachment ri:filename="grade1.png"/></ac:image></p>'
        "<pre><code><ac:image>"
        '<ri:attachment ri:filename="opaque.png"/></ac:image></code></pre>'
    )
    md_result = cfxmark.to_md(xhtml)
    assert md_result.attachments == ("grade1.png", "opaque.png")

    cfx_result = cfxmark.to_cfx(md_result.markdown)
    assert set(cfx_result.attachments) == {"grade1.png", "opaque.png"}


def test_resolve_assets_sidecar_downloads_opaque_attachments(
    tmp_path: Path,
) -> None:
    xhtml = '<pre><code><ac:image><ri:attachment ri:filename="opaque.png"/></ac:image></code></pre>'
    md = cfxmark.to_md(xhtml).markdown
    asset_dir = tmp_path / "assets"
    cfxmark.resolve_assets(
        md,
        fetcher_returning(PNG_BYTES),
        mode="sidecar",
        asset_dir=asset_dir,
        md_path=tmp_path / "doc.md",
    )
    assert (asset_dir / "opaque.png").read_bytes() == PNG_BYTES


def test_resolve_assets_downloads_authenticated_inline_opaque_attachment(tmp_path: Path) -> None:
    raw_xml = '<ac:link><ri:attachment ri:filename="inline.png"/></ac:link>'
    opaque_id = opaque_id_for(raw_xml)
    markdown = serialize_inline_opaque("inline", opaque_id) + "\n\n" + serialize_payloads({opaque_id: raw_xml}) + "\n"
    fetched: list[str] = []
    asset_dir = tmp_path / "assets"

    cfxmark.resolve_assets(
        markdown,
        lambda name: fetched.append(name) or PNG_BYTES,
        mode="sidecar",
        asset_dir=asset_dir,
    )

    assert fetched == ["inline.png"]
    assert (asset_dir / "inline.png").read_bytes() == PNG_BYTES


def test_resolve_assets_sidecar_requires_asset_dir() -> None:
    cfx = '<p><ac:image><ri:attachment ri:filename="img.png"/></ac:image></p>'
    md = cfxmark.to_md(cfx).markdown
    with pytest.raises(ValueError, match="asset_dir"):
        cfxmark.resolve_assets(md, fetcher_returning(PNG_BYTES), mode="sidecar")


# ---------------------------------------------------------------------------
# strict_filenames path traversal defense (§3.1.4)
# ---------------------------------------------------------------------------

BAD_CASES = [
    ("../../../etc/passwd", "parent traversal"),
    ("/absolute/path.png", "absolute"),
    ("C:\\windows\\system32.dll", "windows"),
    ("stream:colon.png", "colon"),
    ("null\x00byte.png", "null byte"),
    ("..", "parent traversal alone"),
    ("subdir/../escape.png", "subdir parent traversal"),
]


@pytest.mark.parametrize("bad_name,_label", BAD_CASES)
def test_strict_filenames_rejects(bad_name: str, _label: str, tmp_path: Path) -> None:
    # Marker must be on same line as image link (no newline between them)
    md = f'![x]({bad_name}) <!-- cfxmark:asset src="{bad_name}" -->'
    with pytest.raises(AssetSecurityError):
        cfxmark.resolve_assets(md, lambda n: b"data", mode="sidecar", asset_dir=tmp_path)


def test_strict_filenames_allows_legitimate(tmp_path: Path) -> None:
    md = '![x](legitimate.png) <!-- cfxmark:asset src="legitimate.png" -->'
    cfxmark.resolve_assets(md, fetcher_returning(PNG_BYTES), mode="sidecar", asset_dir=tmp_path)
    assert (tmp_path / "legitimate.png").exists()


def test_strict_filenames_allows_subdir(tmp_path: Path) -> None:
    (tmp_path / "subdir").mkdir(exist_ok=True)
    md = '![x](subdir/nested.png) <!-- cfxmark:asset src="subdir/nested.png" -->'
    cfxmark.resolve_assets(md, fetcher_returning(PNG_BYTES), mode="sidecar", asset_dir=tmp_path)
    assert (tmp_path / "subdir" / "nested.png").exists()


def test_strict_filenames_allows_unicode(tmp_path: Path) -> None:
    md = '![x](한국어.png) <!-- cfxmark:asset src="한국어.png" -->'
    cfxmark.resolve_assets(md, fetcher_returning(PNG_BYTES), mode="sidecar", asset_dir=tmp_path)
    assert (tmp_path / "한국어.png").exists()


@pytest.mark.skipif(os.name != "posix", reason="symlink trap requires POSIX")
def test_strict_filenames_rejects_symlink_escape(tmp_path: Path) -> None:
    (tmp_path / "evil").symlink_to("/etc")
    md = '![x](evil/passwd) <!-- cfxmark:asset src="evil/passwd" -->'
    with pytest.raises(AssetSecurityError):
        cfxmark.resolve_assets(md, lambda n: b"data", mode="sidecar", asset_dir=tmp_path)


def test_strict_filenames_false_is_permissive(tmp_path: Path) -> None:
    md = '![x](../evil.png) <!-- cfxmark:asset src="../evil.png" -->'
    try:
        cfxmark.resolve_assets(
            md,
            fetcher_returning(PNG_BYTES),
            mode="sidecar",
            asset_dir=tmp_path,
            strict_filenames=False,
        )
    except AssetSecurityError:
        pytest.fail("strict_filenames=False should bypass validation")
