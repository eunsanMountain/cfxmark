"""Image asset marker + resolve_assets tests."""

from __future__ import annotations

from pathlib import Path

import pytest

import cfxmark

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


def test_resolve_assets_sidecar_requires_asset_dir() -> None:
    cfx = '<p><ac:image><ri:attachment ri:filename="img.png"/></ac:image></p>'
    md = cfxmark.to_md(cfx).markdown
    with pytest.raises(ValueError, match="asset_dir"):
        cfxmark.resolve_assets(md, fetcher_returning(PNG_BYTES), mode="sidecar")
