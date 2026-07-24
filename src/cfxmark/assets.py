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
import logging
import mimetypes
import re
from collections.abc import Callable
from dataclasses import fields
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from cfxmark.ast import InlineOpaque, Node, OpaqueBlock
from cfxmark.exceptions import AssetSecurityError, ParseError
from cfxmark.image_metadata import marker_transport_path, render_link_destination, visible_target_matches_attachment
from cfxmark.opaque import (
    find_asset_marker_occurrences,
    serialize_asset_marker,
)
from cfxmark.parsers.cfx import _enumerate_attachment_names

_log = logging.getLogger("cfxmark.assets")

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
    strict_filenames: bool = True,
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
    :param strict_filenames: When ``True`` (default), attachment
        filenames are validated before the ``fetcher`` is called.
        Unsafe names (parent traversal, absolute paths, Windows path
        separators, null bytes, symlink escapes) raise
        :class:`~cfxmark.AssetSecurityError`. Pass ``False`` to
        preserve the exact 0.1.x behavior with no validation.
    :returns: A new Markdown string with the image links updated.
        Asset markers are preserved so the function is idempotent and
        the result can still round-trip through :func:`cfxmark.to_cfx`.
    """

    resolved_base: Path | None = None
    if mode == "sidecar":
        if asset_dir is None:
            raise ValueError("resolve_assets(mode='sidecar') requires asset_dir=")
        asset_dir_path = Path(asset_dir)
        if strict_filenames:
            # Resolve once outside the loop so per-attachment writes only
            # need to resolve the candidate path.
            resolved_base = asset_dir_path.resolve()
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
    marker_occurrences = find_asset_marker_occurrences(md, validate_remote_source=False)
    marker_plans = []
    for marker in marker_occurrences:
        original_src = marker.remote_src
        link = marker.link
        visible_src = link.target
        fragment = _cfxmark_fragment(visible_src)
        if fragment and visible_src == original_src:
            # Reserved-looking text in the exact typed remote filename is
            # literal, not visible-link metadata.
            fragment = ""
        elif fragment and not visible_target_matches_attachment(_strip_cfxmark_fragment(visible_src), original_src):
            raise ParseError("legacy image metadata target is ambiguous for the asset marker")

        transport_source = _strip_cfxmark_fragment(visible_src) if fragment else visible_src
        marker_transport_path(transport_source, original_src)

        _guard_filename(original_src, strict=strict_filenames)
        if mode == "sidecar" and strict_filenames:
            assert asset_dir_path is not None
            assert resolved_base is not None
            _safe_resolve(resolved_base, asset_dir_path / original_src)
        marker_plans.append((marker, fragment))

    seen = {item.remote_src for item in marker_occurrences}
    opaque_filenames: list[str] = []
    if mode == "sidecar":
        assert asset_dir_path is not None
        for raw_xml in _authenticated_opaque_xml(md):
            for filename in _enumerate_attachment_names(raw_xml):
                if filename in seen:
                    continue
                seen.add(filename)
                _guard_filename(filename, strict=strict_filenames)
                if strict_filenames:
                    assert resolved_base is not None
                    _safe_resolve(resolved_base, asset_dir_path / filename)
                opaque_filenames.append(filename)

        # Directory creation is delayed until the complete document plan has
        # passed validation, so a later unsafe occurrence has no local effect.
        if marker_plans or opaque_filenames:
            asset_dir_path.mkdir(parents=True, exist_ok=True)

    for marker, fragment in marker_plans:
        original_src = marker.remote_src
        link = marker.link
        link_start, link_end, alt, visible_src, title = (
            link.start,
            link.end,
            link.alt,
            link.target,
            link.title,
        )

        bytes_data = fetcher(original_src)
        if bytes_data is None:
            # Skip — leave the existing region untouched.
            continue

        if mode == "inline":
            new_target = _data_uri(original_src, bytes_data)
        else:
            assert asset_dir_path is not None
            _write_asset(
                asset_dir_path,
                resolved_base,
                original_src,
                bytes_data,
                strict=strict_filenames,
            )
            relative_path = f"{link_base}{original_src}" if link_base else original_src
            new_target = quote(relative_path, safe="/")
        if fragment:
            separator = "&" if "#" in new_target else "#"
            new_target = f"{new_target}{separator}{fragment}"

        out_parts.append(md[cursor:link_start])
        destination = render_link_destination(new_target)
        if title:
            new_link = f'![{alt}]({destination} "{title}")'
        else:
            new_link = f"![{alt}]({destination})"
        out_parts.append(new_link)
        out_parts.append(md[link_end : marker.start])
        out_parts.append(serialize_asset_marker(original_src))
        cursor = marker.end

    out_parts.append(md[cursor:])
    result = "".join(out_parts)

    # Fallback: opaque blocks (and the inline payload sidecar) preserve
    # raw ``<ri:attachment ri:filename="...">`` references that have no
    # paired Markdown image link. In ``sidecar`` mode we still need to
    # download those bytes so the caller's asset directory is complete.
    # ``inline`` mode has no place to embed them (the XML is opaque
    # text), so we leave it alone there.
    if mode == "sidecar":
        assert asset_dir_path is not None
        for filename in opaque_filenames:
            bytes_data = fetcher(filename)
            if bytes_data is None:
                continue
            _write_asset(
                asset_dir_path,
                resolved_base,
                filename,
                bytes_data,
                strict=strict_filenames,
            )

    return result


_CFXMARK_FRAGMENT_RE = re.compile(r"cfxmark:[^\s)]+")


def _cfxmark_fragment(target: str) -> str:
    """Return cfxmark image metadata from a visible link target."""

    match = _CFXMARK_FRAGMENT_RE.search(target)
    return match.group(0) if match else ""


def _strip_cfxmark_fragment(target: str) -> str:
    """Remove one resolver-recognized legacy metadata fragment."""

    match = _CFXMARK_FRAGMENT_RE.search(target)
    if match is None:
        return target
    start = match.start()
    if start > 0 and target[start - 1] in "#&":
        start -= 1
    return (target[:start] + target[match.end() :]).rstrip("#&")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _validate_filename(name: str) -> None:
    """Raise :class:`AssetSecurityError` if *name* is an unsafe attachment filename."""
    if not name or "\x00" in name:
        raise AssetSecurityError(name, "empty or contains null byte")
    if "\\" in name or ":" in name:
        raise AssetSecurityError(name, "contains Windows path separator")
    p = Path(name)
    if p.is_absolute() or p.drive:
        raise AssetSecurityError(name, "absolute path")
    if any(part == ".." for part in p.parts):
        raise AssetSecurityError(name, "parent traversal segment")


def _safe_resolve(resolved_base: Path, candidate: Path) -> Path:
    """Resolve *candidate* and verify it stays inside *resolved_base*.

    *resolved_base* must already be the result of ``base.resolve()`` —
    callers hoist that out of any per-attachment loop.

    Raises :class:`AssetSecurityError` on symlink escapes or resolution
    failures, preserving the original exception chain.
    """
    try:
        resolved_cand = candidate.resolve()
    except (OSError, RuntimeError) as ex:
        raise AssetSecurityError(str(candidate), f"resolve failed: {ex}") from ex
    if not resolved_cand.is_relative_to(resolved_base):
        raise AssetSecurityError(str(candidate), f"resolves outside base directory {resolved_base}")
    return resolved_cand


def _guard_filename(name: str, *, strict: bool) -> None:
    """Validate *name* when ``strict``; no-op otherwise.

    Logs a warning before re-raising so the rejection is observable
    even when the caller catches :class:`AssetSecurityError`.
    """
    if not strict:
        return
    try:
        _validate_filename(name)
    except AssetSecurityError as exc:
        _log.warning("rejecting unsafe attachment: %s (%s)", exc.name, exc.reason)
        raise


def _write_asset(
    asset_dir_path: Path,
    resolved_base: Path | None,
    filename: str,
    bytes_data: bytes,
    *,
    strict: bool,
) -> None:
    """Resolve the destination, ensure parents exist, and write *bytes_data*.

    When ``strict``, the resolved destination is verified to stay
    inside *resolved_base* (which the caller resolved once before the
    loop).
    """
    if strict:
        assert resolved_base is not None
        dest = _safe_resolve(resolved_base, asset_dir_path / filename)
    else:
        dest = asset_dir_path / filename
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(bytes_data)


def _authenticated_opaque_xml(markdown: str) -> tuple[str, ...]:
    """Return raw XML only from semantically reachable authenticated nodes."""

    from cfxmark.parsers.md import parse_md

    document, _warnings = parse_md(markdown, validate_asset_marker_sources=False)
    raw_sources: list[str] = []

    def visit(node: Node) -> None:
        if isinstance(node, (OpaqueBlock, InlineOpaque)):
            raw_sources.append(node.raw_xml)
            return
        for item in fields(node):
            value = getattr(node, item.name)
            if isinstance(value, Node):
                visit(value)
            elif isinstance(value, tuple):
                for member in value:
                    if isinstance(member, Node):
                        visit(member)

    visit(document)
    return tuple(raw_sources)


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
