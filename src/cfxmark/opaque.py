r"""Opaque passthrough block serialization.

An **opaque block** is a Confluence XML fragment that cfxmark cannot
represent natively in Markdown. To preserve it losslessly across
``cfx → md → cfx`` round trips we serialize it as:

```
<!-- cfxmark:opaque id="op-<hash>" -->
​```cfx-storage
<ac:structured-macro ac:name="drawio" ...>
  ...
</ac:structured-macro>
​```
<!-- /cfxmark:opaque -->
```

Design notes:

* The HTML comment sentinels are the authoritative marker — the Markdown
  parser detects the start sentinel and swallows everything until the
  end sentinel. The fenced code block inside is purely cosmetic (it
  makes the block render as a code block in GitHub / Obsidian / VS Code
  previews so a human reader sees "don't touch this").
* The ``id`` is a short hash of the raw XML content (not random), so
  an unchanged opaque block has a stable ID across conversions. This
  also means two identical opaque blocks in the same document share an
  ID, which is fine — the ID is a label, not a primary key.
* ``ac:macro-id`` attributes inside the raw XML are **preserved as is**
  — this is the whole reason the mechanism exists. Confluence uses
  ``ac:macro-id`` as an identity key, so reusing the same UUID makes
  Confluence treat the fragment as the same macro instance on push,
  preserving comments, permissions, and attachments.
* The inner fence uses ``cfx-storage`` as its info string. Any other
  info string is ignored by cfxmark's parser, so a user cannot
  accidentally create a fake opaque block just by typing
  ``\`\`\`cfx-storage``.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from dataclasses import dataclass
from urllib.parse import unquote

from cfxmark.exceptions import ParseError
from cfxmark.image_metadata import ImageLinkMatch, find_preceding_image_link

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

OPAQUE_FENCE_INFO = "cfx-storage"
OPAQUE_END = "<!-- /cfxmark:opaque -->"

# Inline opaque uses an ordinary Markdown link with a custom URL
# scheme so it survives any third-party renderer.
INLINE_OPAQUE_SCHEME = "cfx:"


_OPAQUE_BLOCK_RE = re.compile(
    r'<!--\s*cfxmark:opaque\s+id="(?P<id>[a-zA-Z0-9_-]+)"\s*-->'
    r"\s*\n"
    r"```" + re.escape(OPAQUE_FENCE_INFO) + r"\s*\n"
    r"(?P<body>.*?)"
    r"\n```\s*\n"
    r"<!--\s*/cfxmark:opaque\s*-->",
    re.DOTALL,
)


# These are Confluence atoms whose visible Markdown projection loses remote
# identity even though cfxmark already has an authenticated opaque transport
# for their exact XML.  Keep this set deliberately narrow: a generic unknown
# element remains a protected projection and must not silently become
# full-migration publishable.
_ROUND_TRIPPABLE_TIME_RE = re.compile(r"^<time(?:\s[^<>]*)?(?:/>|>[^<>]*</time>)$", re.DOTALL)
_ROUND_TRIPPABLE_LINK_RE = re.compile(
    r"^<ac:link>\s*<ri:(?:user|attachment)(?:\s[^<>]*)?/>\s*</ac:link>$",
    re.DOTALL,
)
# A body-less structured macro (self-closing, empty, or plain-text parameters
# only — e.g. an inline <ac:structured-macro ac:name="toc"> inside a heading).
# Its exact XML round-trips through the authenticated payload sidecar, so the
# macro identity (ac:macro-id / ac:schema-version) is preserved byte for byte
# instead of being dropped into a lossy plain-text projection. Macros with a
# rich-text or plain-text body stay protected/fail-closed.
_ROUND_TRIPPABLE_BODYLESS_MACRO_RE = re.compile(
    r"^<ac:structured-macro\b[^<>]*"
    r"(?:/>|>(?:\s*<ac:parameter\b[^<>]*(?:/>|>[^<>]*</ac:parameter>))*\s*</ac:structured-macro>)$",
    re.DOTALL,
)
_ROUND_TRIPPABLE_TABLE_DIV_RE = re.compile(r"^<div(?:\s|>)", re.DOTALL)
_ROUND_TRIPPABLE_TABLE_LINK_RE = re.compile(
    r"<ac:link(?:\s[^<>]*)?>\s*<ri:(?:user|attachment)(?:\s[^<>]*)?/>\s*</ac:link>",
    re.DOTALL,
)


def is_round_trippable_inline_atomic(raw_xml: str) -> bool:
    """Whether an inline Confluence atom has an exact managed transport.

    The matcher intentionally accepts only ``time``, direct user/attachment
    links, and body-less structured macros (plain-text parameters at most).
    Other opaque XML remains protected and fail-closed in editable Markdown.
    """

    return bool(
        _ROUND_TRIPPABLE_TIME_RE.fullmatch(raw_xml)
        or _ROUND_TRIPPABLE_LINK_RE.fullmatch(raw_xml)
        or _ROUND_TRIPPABLE_BODYLESS_MACRO_RE.fullmatch(raw_xml)
    )


def is_round_trippable_table_wrapper(raw_xml: str, source_path: tuple[str, ...]) -> bool:
    """Whether a table atom has an exact managed opaque transport."""

    in_table = "table[" in " ".join(source_path)
    return (raw_xml.startswith("<table") or (in_table and bool(_ROUND_TRIPPABLE_TABLE_DIV_RE.match(raw_xml)))) and bool(
        _ROUND_TRIPPABLE_TABLE_LINK_RE.search(raw_xml)
    )


def has_round_trippable_inline_payload(raw_xml: str) -> bool:
    """Whether an editable inline opaque reference needs a payload sidecar."""

    return is_round_trippable_inline_atomic(raw_xml) or (
        bool(_ROUND_TRIPPABLE_TABLE_DIV_RE.match(raw_xml)) and bool(_ROUND_TRIPPABLE_TABLE_LINK_RE.search(raw_xml))
    )


# ---------------------------------------------------------------------------
# ID scheme
# ---------------------------------------------------------------------------


def opaque_id_for(raw_xml: str) -> str:
    """Deterministic short ID derived from the raw XML content.

    The ID is ``op-`` followed by the first 8 hex chars of SHA-256.
    It is deterministic so:

    * Two cfxmark runs over the same input produce identical output.
    * An unchanged opaque block has a stable ID across round trips.
    """

    digest = hashlib.sha256(raw_xml.encode("utf-8")).hexdigest()
    return f"op-{digest[:8]}"


# ---------------------------------------------------------------------------
# Serialization (AST OpaqueBlock → markdown string)
# ---------------------------------------------------------------------------


def serialize_opaque(raw_xml: str, opaque_id: str | None = None) -> str:
    """Render an opaque block as its markdown representation.

    The resulting string is surrounded by blank lines so it sits cleanly
    in a paragraph sequence. The caller is responsible for joining.
    """

    ident = opaque_id or opaque_id_for(raw_xml)
    body = raw_xml.rstrip("\n")
    return f'<!-- cfxmark:opaque id="{ident}" -->\n```{OPAQUE_FENCE_INFO}\n{body}\n```\n{OPAQUE_END}'


# ---------------------------------------------------------------------------
# Deserialization (markdown string → OpaqueBlock data)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OpaqueMatch:
    """A parsed opaque block candidate."""

    opaque_id: str
    raw_xml: str
    start: int
    end: int


def find_opaque_blocks(source: str) -> list[OpaqueMatch]:
    """Locate every opaque block in a Markdown source string.

    A sentinel is only honoured if its ``id`` matches the SHA-256 prefix
    derived from the body — see :func:`opaque_id_for`. This makes it
    practically impossible for a user to type a real-looking opaque
    block by accident: the body would have to hash to the exact id
    they typed, which collides with probability 2⁻³².

    Returned matches are in document order. Callers may use the
    ``start`` / ``end`` offsets to split the source and interleave
    opaque blocks with ordinary Markdown regions.
    """

    code_ranges, comment_ranges, raw_html_ranges = _markdown_context_ranges(source)
    matches: list[OpaqueMatch] = []
    for m in _OPAQUE_BLOCK_RE.finditer(source):
        if (
            _inside_ranges(m.start(), code_ranges)
            or any(start < m.start() < end for start, end in comment_ranges)
            or _inside_ranges(m.start(), raw_html_ranges)
        ):
            continue
        body = m.group("body")
        claimed_id = m.group("id")
        if opaque_id_for(body) != claimed_id:
            # Sentinel didn't authenticate — leave the region as plain
            # Markdown so the user's literal text round-trips intact.
            continue
        matches.append(
            OpaqueMatch(
                opaque_id=claimed_id,
                raw_xml=body,
                start=m.start(),
                end=m.end(),
            )
        )
    return matches


def strip_opaque_blocks(source: str) -> tuple[str, list[OpaqueMatch]]:
    """Remove opaque blocks from a source string, returning them separately.

    Each removed block is replaced with a unique placeholder line of the
    form ``__CFXMARK_OPAQUE_<n>__`` so downstream Markdown parsing does
    not mis-interpret the embedded XML. Callers then re-inject the
    opaque blocks at those placeholder positions.
    """

    matches = find_opaque_blocks(source)
    if not matches:
        return source, []

    chunks: list[str] = []
    cursor = 0
    for i, m in enumerate(matches):
        chunks.append(source[cursor : m.start])
        chunks.append(f"\n\n__CFXMARK_OPAQUE_{i}__\n\n")
        cursor = m.end
    chunks.append(source[cursor:])
    return "".join(chunks), matches


_PLACEHOLDER_RE = re.compile(r"__CFXMARK_OPAQUE_(\d+)__")


def opaque_placeholder_index(line: str) -> int | None:
    """If a line is an opaque placeholder, return its index. Else ``None``."""

    stripped = line.strip()
    m = _PLACEHOLDER_RE.fullmatch(stripped)
    if m is None:
        return None
    return int(m.group(1))


# ---------------------------------------------------------------------------
# Asset marker format (image attachments)
# ---------------------------------------------------------------------------

# Image references attached to the source page carry an HTML-comment
# metadata marker so a separate ``resolve_assets`` step can fetch the
# bytes and either embed them as a data URI or save them to a sidecar
# directory. The marker survives round-trip conversion (it is stripped
# in the md preprocess pass and re-emitted by the renderer) and is
# idempotent so the resolver can re-run safely.

_ASSET_MARKER_RE = re.compile(
    r'<!--[ \t]*cfxmark:asset(?:[ \t]+v=(?P<version>[23]))?[ \t]+src="(?P<src>[^\r\n"]*)"[ \t]*-->',
    re.IGNORECASE,
)
_ASSET_MARKER_PREFIX_RE = re.compile(r"<!--\s*cfxmark:asset\b", re.IGNORECASE)
_FENCE_LINE_RE = re.compile(r"^ {0,3}(?P<marker>`{3,}|~{3,})")
_ATX_HEADING_LINE_RE = re.compile(r"^ {0,3}#{1,6}(?:[ \t]+|$)")
_SETEXT_H1_LINE_RE = re.compile(r"^ {0,3}=+[ \t]*$")
_THEMATIC_BREAK_LINE_RE = re.compile(r"^ {0,3}(?P<marker>[-_*])(?:[ \t]*(?P=marker)){2,}[ \t]*$")
_LIST_ITEM_LINE_RE = re.compile(r"^ {0,3}(?P<marker>\d{1,9}[.)]|[+*-])(?P<spacing>[ \t]+)(?P<body>.*)$")
_ASSET_PLACEHOLDER_PREFIX = ".cfxmark-asset-ref-"


@dataclass(frozen=True)
class AssetMarkerBinding:
    """One adjacent image occurrence bound to its remote attachment name."""

    visible_src: str
    remote_src: str


@dataclass(frozen=True)
class AssetMarkerOccurrence:
    """One validated non-code asset marker and its adjacent image link."""

    start: int
    end: int
    remote_src: str
    link: ImageLinkMatch


def serialize_asset_marker(original_src: str) -> str:
    """Render the asset marker for an image whose original Confluence
    attachment filename was ``original_src``."""

    if any(character in original_src for character in '%"\r\n') or "--" in original_src:
        encoded = original_src.encode("utf-8").hex()
        return f'<!-- cfxmark:asset v=3 src="{encoded}" -->'
    return f'<!-- cfxmark:asset src="{original_src}" -->'


def _asset_marker_source(marker: re.Match[str]) -> tuple[str, bool]:
    source = marker.group("src")
    version = marker.group("version")
    if version is None:
        return source, False
    if "--" in source:
        raise ParseError("asset marker encoding is not comment-safe")
    if version == "2":
        padding = "=" * (-len(source) % 4)
        try:
            decoded = base64.b64decode(source + padding, altchars=b"-_", validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError) as error:
            raise ParseError("asset marker encoding is invalid") from error
        canonical = base64.urlsafe_b64encode(decoded.encode("utf-8")).rstrip(b"=").decode("ascii")
    else:
        try:
            decoded = bytes.fromhex(source).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as error:
            raise ParseError("asset marker encoding is invalid") from error
        canonical = decoded.encode("utf-8").hex()
    if source != canonical:
        raise ParseError("asset marker encoding is not canonical")
    return decoded, True


def find_asset_markers(text: str) -> list[tuple[int, int, str]]:
    """Locate every ``<!-- cfxmark:asset src="..." -->`` marker.

    Returns a list of ``(start, end, original_src)`` tuples in
    document order so callers can splice or strip them.
    """

    return [(item.start, item.end, item.remote_src) for item in find_asset_marker_occurrences(text)]


def find_asset_marker_occurrences(
    text: str,
    *,
    validate_remote_source: bool = True,
) -> tuple[AssetMarkerOccurrence, ...]:
    """Return every strict, occurrence-bound marker outside Markdown code."""

    literal_ranges = _markdown_code_ranges(text)
    markers = [match for match in _ASSET_MARKER_RE.finditer(text) if not _inside_ranges(match.start(), literal_ranges)]
    reserved = [
        match for match in _ASSET_MARKER_PREFIX_RE.finditer(text) if not _inside_ranges(match.start(), literal_ranges)
    ]
    marker_starts = {match.start() for match in markers}
    if any(match.start() not in marker_starts for match in reserved):
        raise ParseError("asset marker is not well formed")

    occurrences: list[AssetMarkerOccurrence] = []
    cursor = 0
    for marker in markers:
        link = find_preceding_image_link(text, marker.start(), allow_metadata_marker=True)
        if link is None or link.start < cursor:
            raise ParseError("asset marker is duplicate or not adjacent to one image")
        remote_src, typed_encoding = _asset_marker_source(marker)
        if validate_remote_source:
            _validate_asset_marker_source(remote_src, interpret_percent_escapes=not typed_encoding)
        occurrences.append(AssetMarkerOccurrence(marker.start(), marker.end(), remote_src, link))
        cursor = marker.end()
    return tuple(occurrences)


def strip_asset_markers(
    text: str,
    *,
    validate_remote_source: bool = True,
) -> tuple[str, dict[str, AssetMarkerBinding]]:
    """Bind valid adjacent markers by occurrence and preserve code literals.

    Each bound image target is replaced with a unique parser-only placeholder.
    A visible URL is therefore never used as an occurrence ID, so two images
    may share a sidecar path without one marker overwriting the other. Reserved
    markers outside code must be well formed and adjacent to exactly one image;
    malformed, orphaned, or duplicate markers fail closed.
    """

    occurrences = find_asset_marker_occurrences(text, validate_remote_source=validate_remote_source)
    if not occurrences:
        return text, {}

    bindings: dict[str, AssetMarkerBinding] = {}
    parts: list[str] = []
    cursor = 0
    for ordinal, occurrence in enumerate(occurrences):
        link = occurrence.link
        remote_src = occurrence.remote_src
        placeholder = _asset_placeholder(text, ordinal, occurrence.start, remote_src, bindings)
        parts.append(text[cursor : link.target_start])
        parts.append(placeholder)
        parts.append(text[link.target_end : occurrence.start])
        cursor = occurrence.end
        bindings[placeholder] = AssetMarkerBinding(
            # ``find_preceding_image_link`` already returns the parsed,
            # title-free destination. Re-parsing it would truncate valid
            # angle destinations containing spaces.
            visible_src=link.target,
            remote_src=remote_src,
        )
    parts.append(text[cursor:])
    return "".join(parts), bindings


def _validate_asset_marker_source(source: str, *, interpret_percent_escapes: bool) -> None:
    decoded = unquote(source) if interpret_percent_escapes else source
    if (
        not source
        or "/" in decoded
        or "\\" in decoded
        or decoded in {".", ".."}
        or decoded.startswith("~")
        or re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", decoded)
    ):
        raise ParseError("asset marker source must be one remote attachment filename")


def _asset_placeholder(
    text: str,
    ordinal: int,
    marker_start: int,
    remote_src: str,
    bindings: dict[str, AssetMarkerBinding],
) -> str:
    salt = 0
    while True:
        digest = hashlib.sha256(
            f"cfxmark-asset-binding-v1\0{ordinal}\0{marker_start}\0{remote_src}\0{salt}".encode()
        ).hexdigest()[:20]
        placeholder = f"{_ASSET_PLACEHOLDER_PREFIX}{digest}"
        if placeholder not in text and placeholder not in bindings:
            return placeholder
        salt += 1


def _inside_ranges(offset: int, ranges: tuple[tuple[int, int], ...]) -> bool:
    return any(start <= offset < end for start, end in ranges)


def _container_content_start(line: str) -> int:
    cursor = 0
    while True:
        probe = cursor
        spaces = 0
        while probe < len(line) and line[probe] == " " and spaces < 3:
            probe += 1
            spaces += 1
        if probe < len(line) and line[probe] == ">":
            cursor = probe + 1
            if cursor < len(line) and line[cursor] in " \t":
                cursor += 1
            continue
        list_marker = re.match(r"(?:[-+*]|\d{1,9}[.)])(?=[ \t])", line[probe:])
        if list_marker is not None:
            cursor = probe + list_marker.end() + 1
            continue
        return cursor


_RAW_HTML_BLOCK_TAGS = frozenset(
    {
        "address",
        "article",
        "aside",
        "base",
        "basefont",
        "blockquote",
        "body",
        "caption",
        "center",
        "col",
        "colgroup",
        "dd",
        "details",
        "dialog",
        "dir",
        "div",
        "dl",
        "dt",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "frame",
        "frameset",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "head",
        "header",
        "hr",
        "html",
        "iframe",
        "legend",
        "li",
        "link",
        "main",
        "menu",
        "menuitem",
        "nav",
        "noframes",
        "ol",
        "optgroup",
        "option",
        "p",
        "param",
        "search",
        "section",
        "summary",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "title",
        "tr",
        "track",
        "ul",
    }
)
_RAW_HTML_LITERAL_TAG_RE = re.compile(r"^ {0,3}<(?P<tag>pre|script|style|textarea)(?:\s|>|$)", re.IGNORECASE)
_RAW_HTML_BLOCK_TAG_RE = re.compile(r"^ {0,3}</?(?P<tag>[A-Za-z][A-Za-z0-9-]*)(?:\s|/?>|$)", re.IGNORECASE)
_RAW_HTML_COMPLETE_TAG_RE = re.compile(
    r"^ {0,3}(?:</[A-Za-z][A-Za-z0-9-]*\s*>|"
    r"<[A-Za-z][A-Za-z0-9-]*(?:\s+[^<>]*)?\s*/?>)\s*$",
    re.IGNORECASE,
)


def _line_end(text: str, start: int) -> int:
    newline = text.find("\n", start)
    return len(text) if newline < 0 else newline + 1


def _raw_html_end(text: str, start: int, content: str) -> int | None:
    """Return one CommonMark raw-HTML block end, or ``None`` if inactive."""

    stripped = content.lstrip(" ")
    if len(content) - len(stripped) > 3:
        return None
    if stripped.startswith("<!--"):
        comment_start = start + len(content) - len(stripped)
        if _OPAQUE_BLOCK_RE.match(text, comment_start) is not None or text.startswith(PAYLOADS_BEGIN, comment_start):
            return None
        close = text.find("-->", comment_start + 4)
        return len(text) if close < 0 else _line_end(text, close + 3)
    literal = _RAW_HTML_LITERAL_TAG_RE.match(content)
    if literal is not None:
        closing = re.search(rf"</{re.escape(literal.group('tag'))}\s*>", text[start:], re.IGNORECASE)
        return len(text) if closing is None else start + closing.end()
    end_marker: str | None = None
    if stripped.startswith("<?"):
        end_marker = "?>"
    elif stripped.startswith("<![CDATA["):
        end_marker = "]]>"
    elif re.match(r"<![A-Z]", stripped):
        end_marker = ">"
    if end_marker is not None:
        close = text.find(end_marker, start + len(content) - len(stripped) + 2)
        return len(text) if close < 0 else close + len(end_marker)
    block = _RAW_HTML_BLOCK_TAG_RE.match(content)
    if block is None or (
        block.group("tag").lower() not in _RAW_HTML_BLOCK_TAGS and _RAW_HTML_COMPLETE_TAG_RE.match(content) is None
    ):
        return None
    cursor = _line_end(text, start)
    while cursor < len(text):
        end = _line_end(text, cursor)
        if not text[cursor:end].strip():
            return cursor
        cursor = end
    return len(text)


def _raw_html_interrupts_paragraph(content: str) -> bool:
    stripped = content.lstrip(" ")
    if len(content) - len(stripped) > 3:
        return False
    if _RAW_HTML_LITERAL_TAG_RE.match(content) is not None:
        return True
    if stripped.startswith(("<!--", "<?", "<![CDATA[")) or re.match(r"<![A-Z]", stripped) is not None:
        return True
    block = _RAW_HTML_BLOCK_TAG_RE.match(content)
    return block is not None and block.group("tag").lower() in _RAW_HTML_BLOCK_TAGS


def _line_interrupts_paragraph(line: str) -> bool:
    if not line.strip() or _raw_html_interrupts_paragraph(line):
        return True
    if (
        _ATX_HEADING_LINE_RE.match(line) is not None
        or _SETEXT_H1_LINE_RE.match(line) is not None
        or _THEMATIC_BREAK_LINE_RE.match(line) is not None
    ):
        return True
    stripped = line.lstrip(" ")
    if len(line) - len(stripped) <= 3 and stripped.startswith(">"):
        return True
    fence = _FENCE_LINE_RE.match(line)
    if fence is not None and not (fence.group("marker").startswith("`") and "`" in line[fence.end() :]):
        return True
    list_item = _LIST_ITEM_LINE_RE.match(line)
    if list_item is None or not list_item.group("body").strip():
        return False
    marker = list_item.group("marker")
    return not marker[0].isdigit() or marker in {"1.", "1)"}


def _paragraph_end(text: str, next_line_start: int) -> int:
    cursor = next_line_start
    while cursor < len(text):
        end = _line_end(text, cursor)
        line = text[cursor:end].rstrip("\r\n")
        if _line_interrupts_paragraph(line):
            return cursor
        cursor = end
    return len(text)


def _find_code_span_end(text: str, start: int, run_end: int, *, search_end: int | None = None) -> int | None:
    marker = text[start:run_end]
    limit = len(text) if search_end is None else search_end
    close = run_end
    while True:
        close = text.find(marker, close, limit)
        if close < 0:
            return None
        before_same = close > 0 and text[close - 1] == "`"
        after = close + len(marker)
        after_same = after < len(text) and text[after] == "`"
        if not before_same and not after_same:
            return after
        close = after


def _markdown_context_ranges(
    text: str,
) -> tuple[tuple[tuple[int, int], ...], tuple[tuple[int, int], ...], tuple[tuple[int, int], ...]]:
    """Lex Markdown code, comment, and raw-HTML contexts in one ordered pass."""

    code_ranges: list[tuple[int, int]] = []
    comment_ranges: list[tuple[int, int]] = []
    raw_html_ranges: list[tuple[int, int]] = []
    cursor = 0
    while cursor < len(text):
        invalid_fence_run_end: int | None = None
        line_start = text.rfind("\n", 0, cursor) + 1
        end = _line_end(text, cursor)
        if cursor == line_start:
            line = text[line_start:end].rstrip("\r\n")
            content_start = _container_content_start(line)
            content = line[content_start:]
            fence = _FENCE_LINE_RE.match(content)
            if fence is not None and fence.group("marker").startswith("`") and "`" in content[fence.end() :]:
                invalid_fence_run_start = line_start + content_start + fence.start("marker")
                invalid_fence_run_end = line_start + content_start + fence.end()
                paragraph_end = _paragraph_end(text, end)
                inline_code_end = _find_code_span_end(
                    text,
                    invalid_fence_run_start,
                    invalid_fence_run_end,
                    search_end=paragraph_end,
                )
                if inline_code_end is not None:
                    code_ranges.append((invalid_fence_run_start, inline_code_end))
                    cursor = inline_code_end
                    continue
                fence = None
            if fence is not None:
                marker = fence.group("marker")
                close_cursor = end
                while close_cursor < len(text):
                    close_end = _line_end(text, close_cursor)
                    close_line = text[close_cursor:close_end].rstrip("\r\n")
                    close_content = close_line[_container_content_start(close_line) :]
                    if re.match(rf"^ {{0,3}}{re.escape(marker[0])}{{{len(marker)},}}\s*$", close_content):
                        close_cursor = close_end
                        break
                    close_cursor = close_end
                code_ranges.append((line_start, close_cursor))
                cursor = close_cursor
                continue
            if content.startswith(("    ", "\t")):
                code_ranges.append((line_start, end))
                cursor = end
                continue
            raw_end = _raw_html_end(text, line_start, content)
            if raw_end is not None:
                raw_html_ranges.append((line_start, raw_end))
                cursor = raw_end
                continue
            if invalid_fence_run_end is not None:
                cursor = invalid_fence_run_end
                continue

        backtick = text.find("`", cursor, end)
        comment = text.find("<!--", cursor, end)
        starts = [item for item in (backtick, comment) if item >= 0]
        if not starts:
            cursor = end
            continue
        start = min(starts)
        if start == comment:
            close = text.find("-->", start + 4)
            comment_end = len(text) if close < 0 else close + 3
            comment_ranges.append((start, comment_end))
            cursor = comment_end
            continue
        run_end = start
        while run_end < len(text) and text[run_end] == "`":
            run_end += 1
        code_end = _find_code_span_end(text, start, run_end)
        if code_end is None:
            cursor = run_end
            continue
        code_ranges.append((start, code_end))
        cursor = code_end
    return tuple(code_ranges), tuple(comment_ranges), tuple(raw_html_ranges)


def _markdown_code_ranges(text: str) -> tuple[tuple[int, int], ...]:
    """Return fenced, indented, and multiline backtick literal ranges."""

    return _markdown_context_ranges(text)[0]


# ---------------------------------------------------------------------------
# Inline opaque format
# ---------------------------------------------------------------------------


_INLINE_LINK_RE = re.compile(r"\[(?P<label>[^\]]*)\]\((?P<target>cfx:(?P<id>op-[a-f0-9]+))\)")
_INLINE_MANAGED_RE = re.compile(r"\[(?P<label>[^\]]*)\](?P<target>\{cfx:(?P<id>op-[a-f0-9]+)\})")
_INLINE_OPAQUE_PLACEHOLDER_PREFIX = "cfxmark-inline-opaque-"


def serialize_inline_opaque(label: str, opaque_id: str) -> str:
    """Render an inline opaque reference as a Markdown link.

    The visible text is ``label``; the link target is the
    ``cfx:op-XXXX`` URL whose hash refers back to the payload stored
    in the document's ``cfxmark:payloads`` sidecar section.
    """

    return f"[{label}](cfx:{opaque_id})"


def serialize_managed_inline_opaque(label: str, opaque_id: str) -> str:
    """Render an editable opaque reference without turning its whole line into a link.

    The parser lowers the authenticated ``{cfx:...}`` suffix back to the same
    internal link placeholder as the legacy form.  Keeping the visible label
    as prose lets ordinary text on that line remain editable.
    """

    return f"[{label}]{{cfx:{opaque_id}}}"


@dataclass(frozen=True)
class InlineOpaqueRef:
    """A parsed reference to an inline opaque payload."""

    label: str
    opaque_id: str
    start: int = 0
    end: int = 0
    target_start: int = 0
    target_end: int = 0
    replacement_prefix: str = ""
    replacement_suffix: str = ""


def find_inline_opaque_refs(text: str) -> list[InlineOpaqueRef]:
    """Locate reachable legacy and managed inline opaque references."""

    code_ranges, comment_ranges, raw_html_ranges = _markdown_context_ranges(text)
    payload_ranges = tuple((match.start(), match.end()) for match in _active_payload_sections(text))
    refs: list[InlineOpaqueRef] = []
    for pattern, prefix, suffix in (
        (_INLINE_LINK_RE, "", ""),
        (_INLINE_MANAGED_RE, "(", ")"),
    ):
        for m in pattern.finditer(text):
            if (
                _inside_ranges(m.start(), code_ranges)
                or _inside_ranges(m.start(), comment_ranges)
                or _inside_ranges(m.start(), raw_html_ranges)
                or _inside_ranges(m.start(), payload_ranges)
            ):
                continue
            refs.append(
                InlineOpaqueRef(
                    label=m.group("label"),
                    opaque_id=m.group("id"),
                    start=m.start(),
                    end=m.end(),
                    target_start=m.start("target"),
                    target_end=m.end("target"),
                    replacement_prefix=prefix,
                    replacement_suffix=suffix,
                )
            )
    return sorted(refs, key=lambda ref: ref.start)


def bind_inline_opaque_refs(
    text: str,
    payloads: dict[str, str],
) -> tuple[str, dict[str, tuple[str, str]]]:
    """Bind reachable inline references to unique parser-only targets."""

    bindings: dict[str, tuple[str, str]] = {}
    parts: list[str] = []
    cursor = 0
    for ordinal, ref in enumerate(find_inline_opaque_refs(text)):
        payload = payloads.get(ref.opaque_id)
        if payload is None:
            continue
        salt = 0
        while True:
            digest = hashlib.sha256(
                f"cfxmark-inline-binding-v1\0{ordinal}\0{ref.start}\0{ref.opaque_id}\0{salt}".encode()
            ).hexdigest()[:20]
            placeholder = f"{_INLINE_OPAQUE_PLACEHOLDER_PREFIX}{digest}"
            if placeholder not in text and placeholder not in bindings:
                break
            salt += 1
        parts.append(text[cursor : ref.target_start])
        parts.append(f"{ref.replacement_prefix}{placeholder}{ref.replacement_suffix}")
        cursor = ref.target_end
        bindings[placeholder] = (ref.opaque_id, payload)
    if not bindings:
        return text, {}
    parts.append(text[cursor:])
    return "".join(parts), bindings


# ---------------------------------------------------------------------------
# Payload sidecar (collects all inline opaque XML at the document end)
# ---------------------------------------------------------------------------


PAYLOADS_BEGIN = "<!-- cfxmark:payloads -->"
PAYLOADS_END = "<!-- /cfxmark:payloads -->"

_PAYLOAD_ENTRY_RE = re.compile(
    r"<!--\s*(?P<id>op-[a-f0-9]+)\s*\n(?P<body>.*?)\n-->",
    re.DOTALL,
)
_PAYLOADS_SECTION_RE = re.compile(
    re.escape(PAYLOADS_BEGIN) + r"(?P<body>.*?)" + re.escape(PAYLOADS_END),
    re.DOTALL,
)


def _active_payload_sections(source: str) -> tuple[re.Match[str], ...]:
    """Return payload sidecars outside Markdown literal and outer-comment contexts."""

    code_ranges, comment_ranges, raw_html_ranges = _markdown_context_ranges(source)
    return tuple(
        match
        for match in _PAYLOADS_SECTION_RE.finditer(source)
        if not _inside_ranges(match.start(), code_ranges)
        and not any(start < match.start() < end for start, end in comment_ranges)
        and not _inside_ranges(match.start(), raw_html_ranges)
    )


def serialize_payloads(payloads: dict[str, str]) -> str:
    """Render the trailing ``cfxmark:payloads`` sidecar section.

    ``payloads`` is a mapping from opaque-id to raw XML body. The
    section is empty (returns "") if there are no payloads.
    """

    if not payloads:
        return ""
    parts = [PAYLOADS_BEGIN]
    for opaque_id, body in payloads.items():
        parts.append(f"<!-- {opaque_id}\n{body}\n-->")
    parts.append(PAYLOADS_END)
    return "\n".join(parts)


def parse_payloads(source: str) -> dict[str, str]:
    """Extract the ``op-id → raw_xml`` map from a payload sidecar.

    Payloads whose recomputed SHA-256 prefix does not match the
    declared id are silently dropped, the same way ``find_opaque_blocks``
    rejects unauthenticated block sentinels.
    """

    out: dict[str, str] = {}
    sections = _active_payload_sections(source)
    if not sections:
        return out
    section = sections[0]
    body = section.group("body")
    for m in _PAYLOAD_ENTRY_RE.finditer(body):
        opaque_id = m.group("id")
        raw = m.group("body")
        if opaque_id_for(raw) != opaque_id:
            continue
        out[opaque_id] = raw
    return out


def strip_payloads_section(source: str) -> str:
    """Remove the ``cfxmark:payloads`` block from a source string."""

    sections = _active_payload_sections(source)
    if not sections:
        return source
    parts: list[str] = []
    cursor = 0
    for section in sections:
        parts.append(source[cursor : section.start()])
        cursor = section.end()
    parts.append(source[cursor:])
    return "".join(parts)


# ---------------------------------------------------------------------------
# Header notice
# ---------------------------------------------------------------------------


# The header notice is a single-line HTML comment so the body
# contains no nested ``<!--`` / ``-->`` sequences (which would
# terminate the comment early in some Markdown renderers). It
# explains just enough for an agent reader to understand what the
# inline ``[label](cfx:op-...)`` references mean and where the
# payloads live, then points at the bottom of the file for details.
HEADER_NOTICE = (
    "<!-- cfxmark:notice Converted from Confluence storage format. "
    "Inline [label](cfx:op-XXXXXXXX) references preserve Confluence "
    "content that has no native Markdown form; the raw XML for each "
    "lives in the cfxmark:payloads sidecar at the bottom of this file. "
    "Do not edit those references or the sidecar — tampering "
    "invalidates a SHA-256 fingerprint and the round trip falls back "
    "to plain text. -->"
)


_HEADER_RE = re.compile(
    r"^<!--\s*cfxmark:notice\b.*?-->\n?",
    re.DOTALL,
)


def strip_header_notice(source: str) -> str:
    """Remove the cfxmark header notice from a source string."""

    return _HEADER_RE.sub("", source, count=1).lstrip("\n")


__all__ = [
    "OPAQUE_FENCE_INFO",
    "OPAQUE_END",
    "INLINE_OPAQUE_SCHEME",
    "PAYLOADS_BEGIN",
    "PAYLOADS_END",
    "HEADER_NOTICE",
    "opaque_id_for",
    "serialize_opaque",
    "serialize_inline_opaque",
    "serialize_managed_inline_opaque",
    "has_round_trippable_inline_payload",
    "serialize_payloads",
    "serialize_asset_marker",
    "parse_payloads",
    "strip_payloads_section",
    "strip_header_notice",
    "find_inline_opaque_refs",
    "bind_inline_opaque_refs",
    "find_asset_markers",
    "find_asset_marker_occurrences",
    "AssetMarkerOccurrence",
    "strip_asset_markers",
    "InlineOpaqueRef",
    "OpaqueMatch",
    "find_opaque_blocks",
    "strip_opaque_blocks",
    "opaque_placeholder_index",
]
