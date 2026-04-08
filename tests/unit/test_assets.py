"""Image asset marker + resolve_assets tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import cfxmark
from cfxmark import AssetSecurityError

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
    cfx = (
        '<p><ac:image><ri:url ri:value="https://example.com/x.png"/>'
        "</ac:image></p>"
    )
    md = cfxmark.to_md(cfx).markdown
    assert "cfxmark:asset" not in md


def test_resolve_assets_inline_embeds_data_uri() -> None:
    cfx = '<p><ac:image><ri:attachment ri:filename="img.png"/></ac:image></p>'
    md = cfxmark.to_md(cfx).markdown
    out = cfxmark.resolve_assets(md, fetcher_returning(PNG_BYTES), mode="inline")
    assert "data:image/png;base64," in out
    # Marker is preserved so the operation is idempotent.
    assert '<!-- cfxmark:asset src="img.png" -->' in out


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
    back = cfxmark.to_cfx(resolved).xhtml
    assert 'ri:filename="img.png"' in back
    assert 'ri:filename="assets/img.png"' not in back


def test_resolve_assets_skips_when_fetcher_returns_none() -> None:
    cfx = '<p><ac:image><ri:attachment ri:filename="img.png"/></ac:image></p>'
    md = cfxmark.to_md(cfx).markdown
    out = cfxmark.resolve_assets(md, lambda name: None, mode="inline")
    # Marker preserved, link unchanged.
    assert "(img.png)" in out or "(img.png#" in out
    assert '<!-- cfxmark:asset src="img.png" -->' in out
    assert "data:image" not in out


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
    xhtml = (
        "<pre><code><ac:image>"
        '<ri:attachment ri:filename="opaque.png"/></ac:image></code></pre>'
    )
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
