"""Image asset resolution.

After ``cfxmark.to_md(cfx)`` produces a Markdown document, every
local-attachment image carries a ``<!-- cfxmark:asset src="..." -->``
metadata marker. The functions in this module read those markers,
fetch the image bytes via a caller-provided callback, and either:

* embed the bytes inline as a ``data:`` URI (``mode="inline"``), or
* save the bytes to a sidecar directory and rewrite the link to a
  relative path (``mode="sidecar"``).

The marker itself is preserved in both cases so the original
Confluence attachment filename is always recoverable, which keeps
``resolve_assets`` idempotent and lets ``to_cfx`` round-trip the
result back to the correct ``<ri:attachment>`` reference.

cfxmark deliberately stays out of the network business — the
``fetcher`` callback is the only thing that touches Confluence. The
caller is responsible for deciding *how* to fetch each attachment
(e.g. via the Confluence REST API, a vendored HTTP client, or a
pre-loaded cache).
"""

from __future__ import annotations

import base64
import mimetypes
import re
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from cfxmark.opaque import _ASSET_MARKER_RE, serialize_asset_marker

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


ResolveMode = Literal["inline", "sidecar"]
"""Resolution strategy for :func:`resolve_assets`.

* ``"inline"`` — embed each image as a base64 ``data:`` URI inside the
  Markdown link. The result is a single self-contained file.
* ``"sidecar"`` — write each image to ``asset_dir`` and rewrite the
  link to a relative path. The result is a Markdown file plus an
  adjacent assets folder.
"""


AssetFetcher = Callable[[str], bytes | None]
"""Callback that returns the bytes for one Confluence attachment.

Receives the *original* attachment filename (the one stored in the
asset marker, not whatever the visible link target currently is).
Returning ``None`` skips that asset and leaves the marker untouched.
"""


def resolve_assets(
    md: str,
    fetcher: AssetFetcher,
    *,
    mode: ResolveMode = "sidecar",
    asset_dir: str | Path | None = None,
    md_path: str | Path | None = None,
) -> str:
    """Replace cfxmark image asset markers with resolved content.

    :param md: Markdown source produced by :func:`cfxmark.to_md`.
    :param fetcher: Callback that returns the bytes for one
        attachment, given its original Confluence filename. Returning
        ``None`` skips that asset.
    :param mode: ``"sidecar"`` to save bytes to ``asset_dir`` and use
        relative paths, ``"inline"`` to embed as ``data:`` URIs.
    :param asset_dir: Required for ``sidecar`` mode — the directory
        bytes are written to. Created if it does not exist.
    :param md_path: Optional. When ``sidecar`` mode is used, the
        relative path emitted in the Markdown link is computed
        relative to this file's location. Defaults to the current
        working directory.
    :returns: A new Markdown string with the image links updated.
        Asset markers are preserved so the function is idempotent and
        the result can still round-trip through :func:`cfxmark.to_cfx`.
    """

    if mode == "sidecar":
        if asset_dir is None:
            raise ValueError("resolve_assets(mode='sidecar') requires asset_dir=")
        asset_dir_path = Path(asset_dir)
        asset_dir_path.mkdir(parents=True, exist_ok=True)
        link_base = _link_base_for(asset_dir_path, md_path)
    elif mode == "inline":
        asset_dir_path = None
        link_base = ""
    else:  # pragma: no cover — Literal type narrows this out
        raise ValueError(f"unknown mode: {mode!r}")

    # Walk the document, splicing each image link followed by its
    # asset marker. We process in document order, building the new
    # string piece by piece.
    out_parts: list[str] = []
    cursor = 0
    for marker in _ASSET_MARKER_RE.finditer(md):
        original_src = marker.group("src")

        # Find the image link immediately preceding the marker.
        link_span = _find_preceding_image_link(md, marker.start())
        if link_span is None:
            # Marker without a paired link — leave the surrounding
            # text alone.
            continue
        link_start, link_end, alt, _visible_src, title = link_span

        bytes_data = fetcher(original_src)
        if bytes_data is None:
            # Skip — leave the existing region untouched.
            continue

        if mode == "inline":
            new_target = _data_uri(original_src, bytes_data)
        else:
            assert asset_dir_path is not None
            (asset_dir_path / original_src).write_bytes(bytes_data)
            new_target = f"{link_base}{original_src}" if link_base else original_src

        out_parts.append(md[cursor:link_start])
        if title:
            new_link = f'![{alt}]({new_target} "{title}")'
        else:
            new_link = f"![{alt}]({new_target})"
        out_parts.append(new_link)
        out_parts.append(serialize_asset_marker(original_src))
        cursor = marker.end()

    out_parts.append(md[cursor:])
    return "".join(out_parts)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


_IMAGE_LINK_RE = re.compile(
    r'!\[(?P<alt>(?:\\.|[^\]\\])*)\]\((?P<target>[^\s)]+)(?:\s+"(?P<title>(?:\\.|[^"\\])*)")?\)',
)


def _find_preceding_image_link(
    text: str,
    marker_start: int,
) -> tuple[int, int, str, str, str | None] | None:
    """Find the image link immediately to the left of ``marker_start``.

    Returns ``(start, end, alt, target, title)`` or ``None`` if no
    image link sits adjacent (allowing only whitespace) to the marker.
    """

    # Scan back to the nearest non-whitespace position before the marker.
    i = marker_start
    while i > 0 and text[i - 1] in " \t":
        i -= 1
    # The link must end at exactly position ``i`` (no other text in
    # between). Walk back to find a matching ``![``.
    if i < 2 or text[i - 1] != ")":
        return None
    # Find the start of the image link by scanning back for "![".
    bang = text.rfind("![", 0, i)
    if bang < 0:
        return None
    candidate = text[bang:i]
    m = _IMAGE_LINK_RE.fullmatch(candidate)
    if m is None:
        return None
    return (
        bang,
        i,
        m.group("alt"),
        m.group("target"),
        m.group("title"),
    )


def _data_uri(filename: str, data: bytes) -> str:
    """Return a ``data:image/...;base64,...`` URI for ``data``."""

    mime, _ = mimetypes.guess_type(filename)
    if not mime:
        mime = "application/octet-stream"
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _link_base_for(
    asset_dir: Path,
    md_path: str | Path | None,
) -> str:
    """Compute the relative-link prefix from a Markdown file to the
    asset directory.

    Returns a string ending in ``/`` (or empty if assets land next to
    the document). Falls back to ``asset_dir.name`` when no
    ``md_path`` is given so the result is at least a sensible
    one-component prefix.
    """

    if md_path is None:
        # No anchor — use the directory's basename so the link is at
        # least relative-looking.
        return f"{asset_dir.name}/"
    md_dir = Path(md_path).parent
    try:
        rel = asset_dir.resolve().relative_to(md_dir.resolve())
    except ValueError:
        # Asset dir is outside the markdown file's directory — fall
        # back to a posix-style relative path.
        import os

        rel = Path(os.path.relpath(asset_dir.resolve(), md_dir.resolve()))
    rel_str = rel.as_posix()
    if not rel_str or rel_str == ".":
        return ""
    return f"{rel_str}/"


__all__ = [
    "AssetFetcher",
    "ResolveMode",
    "resolve_assets",
]
