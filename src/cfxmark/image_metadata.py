"""Occurrence-scoped Markdown image metadata comments.

The public editable grammar keeps the visible image destination clean and
stores Confluence-only dimensions in one adjacent cfxmark-owned comment.  The
Markdown parser rewrites that comment to the legacy URL fragment internally so
the existing image AST conversion can remain the single metadata decoder.
"""

from __future__ import annotations

import re
import string
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import unquote

from cfxmark.exceptions import ParseError

_ABSOLUTE_URL_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")
_IMAGE_METADATA_MARKER_RE = re.compile(r"<!--\s*cfxmark:img\b(?P<body>[^\r\n]*?)-->", re.IGNORECASE)
_RESERVED_IMAGE_METADATA_RE = re.compile(r"<!--\s*cfxmark:img\b", re.IGNORECASE)
_LEGACY_IMAGE_FRAGMENT_RE = re.compile(
    r"(?P<prefix>cfxmark(?:-external)?):(?P<attrs>(?:(?:[wh]=\d+|thumbnail=1|align=(?:center|right))"
    r"(?:,(?:[wh]=\d+|thumbnail=1|align=(?:center|right)))*))"
)
_PAIR_RE = re.compile(r"(?P<key>[a-z]+)=(?P<value>[^\s]+)", re.IGNORECASE)


@dataclass(frozen=True)
class ImageMetadata:
    width: int | None = None
    height: int | None = None
    thumbnail: bool = False
    align: str | None = None

    @property
    def is_default(self) -> bool:
        return self.width is None and self.height is None and not self.thumbnail and self.align is None


@dataclass(frozen=True)
class ImageLinkMatch:
    start: int
    end: int
    target_start: int
    target_end: int
    alt: str
    target: str
    title: str | None


def render_link_destination(url: str) -> str:
    """Render one Markdown destination using the parser's accepted grammar."""

    if (
        not re.search(r"[\s<>]", url)
        and url.count("(") == url.count(")")
        and not url.startswith("<")
        and not url.endswith(">")
    ):
        return url.replace("\\", "\\\\")
    escaped = url.replace("\\", "\\\\").replace("<", "\\<").replace(">", "\\>")
    return f"<{escaped}>"


def _scan_unescaped(text: str, start: int, delimiter: str, *, stop: int | None = None) -> int | None:
    escaped = False
    for index in range(start, len(text) if stop is None else stop):
        character = text[index]
        if character == delimiter and not escaped:
            return index
        if character == "\\" and not escaped:
            escaped = True
        else:
            escaped = False
    return None


def _parse_destination_and_title(
    text: str,
    start: int,
    stop: int,
) -> tuple[int, int, str, str | None] | None:
    if start >= stop:
        return None
    if text[start] == "<":
        closing = _scan_unescaped(text, start + 1, ">", stop=stop)
        if closing is None or closing == start + 1:
            return None
        target_start = start + 1
        target_end = closing
        rest_start = closing + 1
    else:
        target_start = start
        depth = 0
        escaped = False
        cursor = start
        while cursor < stop:
            character = text[cursor]
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == "(":
                depth += 1
            elif character == ")":
                if depth == 0:
                    return None
                depth -= 1
            elif character.isspace() and depth == 0:
                break
            cursor += 1
        if cursor == start or depth != 0:
            return None
        target_end = cursor
        rest_start = cursor

    while rest_start < stop and text[rest_start].isspace():
        rest_start += 1
    if rest_start == stop:
        title = None
    else:
        if text[rest_start] != '"':
            return None
        title_end = _scan_unescaped(text, rest_start + 1, '"', stop=stop)
        if title_end is None or any(not character.isspace() for character in text[title_end + 1 : stop]):
            return None
        title = text[rest_start + 1 : title_end]
    return target_start, target_end, text[target_start:target_end], title


def _unescape_link_destination(target: str) -> str:
    """Decode CommonMark backslash escapes in one link destination."""

    return re.sub(rf"\\([{re.escape(string.punctuation)}])", r"\1", target)


def serialize_image_metadata(
    *,
    width: int | None,
    height: int | None,
    thumbnail: bool,
    align: str | None,
) -> str:
    """Return the one canonical image metadata comment, or an empty string."""

    parts: list[str] = []
    if width is not None:
        parts.append(f"w={width}")
    if height is not None:
        parts.append(f"h={height}")
    if thumbnail:
        parts.append("thumbnail=1")
    if align in {"center", "right"}:
        parts.append(f"align={align}")
    if not parts:
        return ""
    return f"<!-- cfxmark:img {' '.join(parts)} -->"


def find_preceding_image_link(
    text: str,
    marker_start: int,
    *,
    allow_metadata_marker: bool = False,
) -> ImageLinkMatch | None:
    """Find the image immediately preceding a metadata or asset marker."""

    end = marker_start
    while end > 0 and text[end - 1] in " \t":
        end -= 1
    if allow_metadata_marker:
        metadata_matches = list(_IMAGE_METADATA_MARKER_RE.finditer(text, 0, end))
        if metadata_matches and metadata_matches[-1].end() == end:
            end = metadata_matches[-1].start()
            while end > 0 and text[end - 1] in " \t":
                end -= 1
    if end < 2 or text[end - 1] != ")":
        return None
    bang = text.rfind("![", 0, end)
    if bang < 0:
        return None
    alt_end = _scan_unescaped(text, bang + 2, "]", stop=end)
    if alt_end is None or alt_end + 1 >= end or text[alt_end + 1] != "(":
        return None
    parsed = _parse_destination_and_title(text, alt_end + 2, end - 1)
    if parsed is None:
        return None
    target_start, target_end, target, title = parsed
    return ImageLinkMatch(
        start=bang,
        end=end,
        target_start=target_start,
        target_end=target_end,
        alt=text[bang + 2 : alt_end],
        target=_unescape_link_destination(target),
        title=title,
    )


def rewrite_image_metadata_comments(source: str) -> str:
    """Validate adjacent image comments and lower them to legacy fragments.

    The lowering is internal to the Markdown parser.  Rendering always emits
    the comment grammar, so legacy fragments remain input-only compatibility.
    """

    markers = list(_IMAGE_METADATA_MARKER_RE.finditer(source))
    if not markers:
        if _RESERVED_IMAGE_METADATA_RE.search(source):
            raise ParseError("image metadata marker is not well formed")
        return source

    parts: list[str] = []
    cursor = 0
    for marker in markers:
        link = find_preceding_image_link(source, marker.start())
        if link is None or link.start < cursor:
            raise ParseError("image metadata marker is duplicate or not adjacent to one image")
        metadata = _parse_marker_body(marker.group("body"))
        legacy = _decode_legacy_fragment(link.target)
        if legacy is not None:
            _clean_target, legacy_metadata = legacy
            if legacy_metadata != metadata:
                raise ParseError("image metadata comment conflicts with the legacy URL fragment")
            rewritten_target = link.target
        else:
            rewritten_target = _append_legacy_fragment(link.target, metadata)

        parts.append(source[cursor : link.target_start])
        parts.append(rewritten_target)
        parts.append(source[link.target_end : marker.start()])
        cursor = marker.end()
    parts.append(source[cursor:])
    rewritten = "".join(parts)
    if _RESERVED_IMAGE_METADATA_RE.search(rewritten):
        raise ParseError("image metadata marker is duplicate or not well formed")
    return rewritten


def visible_target_matches_attachment(visible_src: str, remote_src: str) -> bool:
    """Return whether a marker-bound transport target identifies one remote file.

    Managed sidecars may prefix a directory and percent-encode the path once;
    inline resolution produces a data URI whose adjacent asset marker retains
    the remote identity. Other renamed or unrelated targets are ambiguous.
    """

    if visible_src.startswith("data:"):
        return True
    for candidate in (visible_src, unquote(visible_src)):
        if candidate == remote_src or candidate.rsplit("/", 1)[-1] == remote_src:
            return True
    return False


def marker_transport_path(visible_src: str, remote_src: str) -> str:
    """Validate and decode one marker-bound local transport path."""

    if visible_src == remote_src or visible_src.startswith("data:"):
        return visible_src
    if _ABSOLUTE_URL_RE.match(visible_src) or visible_src.startswith(("/", "\\")):
        raise ParseError("asset marker transport must be a relative local path")
    if "?" in visible_src or "#" in visible_src:
        raise ParseError("asset marker transport query or fragment is not representable")
    decoded = unquote(visible_src)
    if (
        _ABSOLUTE_URL_RE.match(decoded)
        or re.match(r"^[A-Za-z]:[/\\]", decoded)
        or decoded.startswith(("/", "\\"))
        or "\\" in decoded
    ):
        raise ParseError("asset marker transport must be a cross-platform relative local path")
    path = PurePosixPath(decoded)
    if path.is_absolute() or any(part == ".." for part in path.parts):
        raise ParseError("asset marker transport escapes the local path boundary")
    if ("?" in decoded or "#" in decoded) and not visible_target_matches_attachment(decoded, remote_src):
        raise ParseError("asset marker transport query or fragment is not representable")
    return decoded


def _parse_marker_body(body: str) -> ImageMetadata:
    raw = body.strip()
    if not raw:
        raise ParseError("image metadata marker has no attributes")
    values: dict[str, str] = {}
    for token in raw.split():
        match = _PAIR_RE.fullmatch(token)
        if match is None:
            raise ParseError("image metadata marker must use space-separated k=v attributes")
        key = match.group("key").lower()
        value = match.group("value")
        if key in values:
            raise ParseError(f"image metadata marker repeats {key!r}")
        if key not in {"w", "h", "thumbnail", "align"}:
            raise ParseError(f"image metadata marker has unknown attribute {key!r}")
        values[key] = value

    for key in ("w", "h"):
        if key in values and not values[key].isdigit():
            raise ParseError(f"image metadata {key!r} must be an integer")
    if values.get("thumbnail", "0") not in {"0", "1"}:
        raise ParseError("image metadata 'thumbnail' must be 0 or 1")
    if values.get("align") not in {None, "center", "right"}:
        raise ParseError("image metadata 'align' must be center or right")
    return ImageMetadata(
        width=int(values["w"]) if "w" in values else None,
        height=int(values["h"]) if "h" in values else None,
        thumbnail=values.get("thumbnail") == "1",
        align=values.get("align"),
    )


def _decode_legacy_fragment(target: str) -> tuple[str, ImageMetadata] | None:
    match = _LEGACY_IMAGE_FRAGMENT_RE.search(target)
    if match is None:
        return None
    values: dict[str, str] = {}
    for pair in match.group("attrs").split(","):
        key, value = pair.split("=", 1)
        if key in values:
            raise ParseError(f"image metadata legacy fragment repeats {key!r}")
        values[key] = value
    start = match.start()
    if start > 0 and target[start - 1] in {"#", "&"}:
        start -= 1
    clean = (target[:start] + target[match.end() :]).rstrip("#&")
    return clean, ImageMetadata(
        width=int(values["w"]) if "w" in values else None,
        height=int(values["h"]) if "h" in values else None,
        thumbnail=values.get("thumbnail") == "1",
        align=values.get("align"),
    )


def _append_legacy_fragment(target: str, metadata: ImageMetadata) -> str:
    parts: list[str] = []
    if metadata.width is not None:
        parts.append(f"w={metadata.width}")
    if metadata.height is not None:
        parts.append(f"h={metadata.height}")
    if metadata.thumbnail:
        parts.append("thumbnail=1")
    if metadata.align is not None:
        parts.append(f"align={metadata.align}")
    if not parts:
        return target
    prefix = "cfxmark-external:" if _ABSOLUTE_URL_RE.match(target) else "cfxmark:"
    separator = "&" if "#" in target else "#"
    return f"{target}{separator}{prefix}{','.join(parts)}"


__all__ = [
    "ImageLinkMatch",
    "ImageMetadata",
    "find_preceding_image_link",
    "marker_transport_path",
    "rewrite_image_metadata_comments",
    "serialize_image_metadata",
    "visible_target_matches_attachment",
]
