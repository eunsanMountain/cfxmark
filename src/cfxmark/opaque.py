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

import hashlib
import re
from dataclasses import dataclass

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
    return (
        f'<!-- cfxmark:opaque id="{ident}" -->\n'
        f"```{OPAQUE_FENCE_INFO}\n"
        f"{body}\n"
        f"```\n"
        f"{OPAQUE_END}"
    )


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

    matches: list[OpaqueMatch] = []
    for m in _OPAQUE_BLOCK_RE.finditer(source):
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
    r'<!--\s*cfxmark:asset\s+src="(?P<src>[^"]*)"\s*-->'
)


def serialize_asset_marker(original_src: str) -> str:
    """Render the asset marker for an image whose original Confluence
    attachment filename was ``original_src``."""

    safe = original_src.replace('"', '%22')
    return f'<!-- cfxmark:asset src="{safe}" -->'


def find_asset_markers(text: str) -> list[tuple[int, int, str]]:
    """Locate every ``<!-- cfxmark:asset src="..." -->`` marker.

    Returns a list of ``(start, end, original_src)`` tuples in
    document order so callers can splice or strip them.
    """

    return [
        (m.start(), m.end(), m.group("src"))
        for m in _ASSET_MARKER_RE.finditer(text)
    ]


def strip_asset_markers(text: str) -> tuple[str, dict[str, str]]:
    """Remove every asset marker from ``text`` and build a mapping
    from each adjacent image's visible link target to its original
    Confluence filename.

    The parser uses this so that, after mistletoe sees the marker-free
    Markdown, it can rewrite the AST ``Image.src`` field back to the
    original filename — preserving the round trip even when the user
    has run :func:`cfxmark.resolve_assets` to point the link at a
    sidecar file or data URI.
    """

    markers = list(_ASSET_MARKER_RE.finditer(text))
    if not markers:
        return text, {}

    src_map: dict[str, str] = {}
    for m in markers:
        # Find the nearest preceding ``](VISIBLE_SRC)`` link target
        # before the marker. We scan backwards from the marker start
        # to the most recent ``](`` close-bracket-open-paren so the
        # match is robust against ``)`` characters inside an image
        # title.
        idx = m.start()
        close = text.rfind(")", 0, idx)
        if close <= 0:
            continue
        open_paren = text.rfind("(", 0, close)
        if open_paren <= 0:
            continue
        if text[open_paren - 1 : open_paren + 1] != "](":
            continue
        visible_src = text[open_paren + 1 : close]
        # The visible target may include a title (``url "title"``);
        # take just the URL portion.
        space = visible_src.find(" ")
        if space >= 0:
            visible_src = visible_src[:space]
        src_map[visible_src] = m.group("src")

    cleaned = _ASSET_MARKER_RE.sub("", text)
    return cleaned, src_map


# ---------------------------------------------------------------------------
# Inline opaque format
# ---------------------------------------------------------------------------


_INLINE_LINK_RE = re.compile(
    r"\[(?P<label>[^\]]*)\]\(cfx:(?P<id>op-[a-f0-9]+)\)"
)


def serialize_inline_opaque(label: str, opaque_id: str) -> str:
    """Render an inline opaque reference as a Markdown link.

    The visible text is ``label``; the link target is the
    ``cfx:op-XXXX`` URL whose hash refers back to the payload stored
    in the document's ``cfxmark:payloads`` sidecar section.
    """

    return f"[{label}](cfx:{opaque_id})"


@dataclass(frozen=True)
class InlineOpaqueRef:
    """A parsed reference to an inline opaque payload."""

    label: str
    opaque_id: str


def find_inline_opaque_refs(text: str) -> list[InlineOpaqueRef]:
    """Locate every inline ``[label](cfx:op-...)`` reference."""

    refs: list[InlineOpaqueRef] = []
    for m in _INLINE_LINK_RE.finditer(text):
        refs.append(
            InlineOpaqueRef(
                label=m.group("label"),
                opaque_id=m.group("id"),
            )
        )
    return refs


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
    section = _PAYLOADS_SECTION_RE.search(source)
    if section is None:
        return out
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

    return _PAYLOADS_SECTION_RE.sub("", source)


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
    "serialize_payloads",
    "serialize_asset_marker",
    "parse_payloads",
    "strip_payloads_section",
    "strip_header_notice",
    "find_inline_opaque_refs",
    "find_asset_markers",
    "strip_asset_markers",
    "InlineOpaqueRef",
    "OpaqueMatch",
    "find_opaque_blocks",
    "strip_opaque_blocks",
    "opaque_placeholder_index",
]
