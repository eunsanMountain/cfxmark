"""Confluence Storage Format → cfxmark AST.

The parser is a recursive tree walker over an :mod:`lxml.etree` parse
tree. The key invariants it enforces are:

1. **Supported subset → AST native nodes.** Anything in the grade-I or
   grade-II table in ``docs/SPEC.md`` becomes a proper
   :mod:`cfxmark.ast` node.
2. **Unknown block → OpaqueBlock.** Unknown ``ac:*`` / ``ri:*``
   elements, or HTML tags we don't model (``div`` etc.), are captured
   byte-for-byte as :class:`~cfxmark.ast.OpaqueBlock` so round-trip
   push can reuse the same ``macro-id``.
3. **Unknown inline → InlineOpaque.** Inline constructs with no semantic
   Markdown representation are preserved as
   :class:`~cfxmark.ast.InlineOpaque` nodes.
4. **GUI pollution is stripped.** ``<span>`` elements whose only
   attribute is a ``style`` setting the default Confluence text color
   (``var(--ds-text,#172b4d)``) are transparently unwrapped — these
   are inserted by the web editor and carry no semantic weight.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field, replace

import lxml.etree as ET

from cfxmark.ast import (
    AlignedSpan,
    BlockNode,
    BlockQuote,
    CellAlign,
    CellType,
    Citation,
    CodeBlock,
    ColorSpan,
    DirectiveMacro,
    Document,
    Emphasis,
    HardBreak,
    Heading,
    HorizontalRule,
    Image,
    InlineCode,
    InlineDirectiveMacro,
    InlineNode,
    InlineOpaque,
    Link,
    List,
    ListItem,
    ListType,
    OpaqueBlock,
    Paragraph,
    SoftBreak,
    Strikethrough,
    Strong,
    Subscript,
    Superscript,
    Table,
    TableCell,
    TableRow,
    Text,
    Underline,
)
from cfxmark.colors import coarse_color
from cfxmark.exceptions import ParseError
from cfxmark.macros import MacroRegistry, default_registry
from cfxmark.opaque import opaque_id_for
from cfxmark.presentation import DEFAULT_COLOR_CAPABILITIES, ColorCapabilityMatrix, parse_cell_background
from cfxmark.storage_normalize import continues_plain_style_run
from cfxmark.table_grid import MAX_TABLE_SPAN, TableGridError, TableGridOverlap, bounded_table_grid_layout
from cfxmark.xml_ns import (
    AC_URI,
    DEFAULT_COLOR_STYLE_RE,
    RI_URI,
    ac_attr,
    collect_text_with_breaks,
    ns_of,
    ri_attr,
    ri_tag,
    storage_semantic_path,
    strip_ns,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})

_BLOCK_TAGS_HTML = (
    frozenset(
        {
            "blockquote",
            "div",
            "hr",
            "ol",
            "p",
            "pre",
            "table",
            "tbody",
            "tfoot",
            "thead",
            "tr",
            "ul",
        }
    )
    | _HEADING_TAGS
)

# Spans whose only decoration is the Confluence default-text colour
# pollute the source with no semantic payload. We unwrap them so the
# round-trip canonicalization is simpler. The regex itself lives in
# ``cfxmark.xml_ns`` so the canonicalization layer can share it.


# ---------------------------------------------------------------------------
# HTML entity pre-processing
# ---------------------------------------------------------------------------

_HTML_ENTITY_RE = re.compile(r"&([a-zA-Z][a-zA-Z0-9]{1,31});")
_XML_SAFE_ENTITIES = frozenset({"amp", "lt", "gt", "quot", "apos"})
_CDATA_SPLIT_RE = re.compile(r"(<!\[CDATA\[.*?\]\]>)", re.DOTALL)
_UNSUPPORTED_CONTENT_MESSAGE = "unsupported Confluence content has no editable Markdown representation"


def _decode_html_entities(source: str) -> str:
    """Replace non-XML HTML entities with their Unicode characters.

    lxml's XML parser rejects entities like ``&nbsp;`` or ``&ndash;``
    unless a DTD is supplied. We side-step the DTD dance by resolving
    those entities ourselves before parsing — but only outside CDATA
    sections, where ``&nbsp;`` is a literal six-character sequence
    that the user actually typed (often in a code sample).
    """

    def repl(m: re.Match[str]) -> str:
        name = m.group(1)
        if name in _XML_SAFE_ENTITIES:
            return m.group(0)
        decoded = html.unescape(f"&{name};")
        return decoded if decoded != f"&{name};" else m.group(0)

    parts = _CDATA_SPLIT_RE.split(source)
    return "".join(part if part.startswith("<![CDATA[") else _HTML_ENTITY_RE.sub(repl, part) for part in parts)


# ---------------------------------------------------------------------------
# Root wrapping
# ---------------------------------------------------------------------------


def _wrap_for_parsing(source: str) -> str:
    """Wrap a CF fragment in a namespaced root element for XML parsing."""

    return (
        f'<?xml version="1.0" encoding="utf-8"?><root xmlns="" xmlns:ac="{AC_URI}" xmlns:ri="{RI_URI}">{source}</root>'
    )


_DOCTYPE_RE = re.compile(r"<!DOCTYPE\b", re.IGNORECASE)
_ENTITY_DECL_RE = re.compile(r"<!ENTITY\b", re.IGNORECASE)


def _parse_fragment_to_element(source: str) -> ET._Element:
    """Parse a Confluence storage fragment and return its root element.

    Hardened against the usual XML attack surface:

    * ``<!DOCTYPE>`` and ``<!ENTITY>`` declarations are rejected before
      lxml ever sees the input — this kills inline billion-laughs and
      similar custom-entity expansions.
    * ``no_network=True`` blocks any attempt to fetch external entities
      or DTDs over the network.
    * ``load_dtd=False`` prevents lxml from loading any external DTD
      file referenced from the document.
    * ``huge_tree=False`` rejects pathologically large parse trees.

    The tree is returned verbatim. Adjacent-inline-run canonicalization
    deliberately does *not* happen here: remote splice reuses subtrees of this
    parse as exact storage bytes for untouched content, so merging wrappers at
    this point would rewrite blocks the user never edited. The canonical form
    belongs to the proof layer only — see :mod:`cfxmark.storage_normalize` and
    ``extract_storage_source()``.
    """

    if _DOCTYPE_RE.search(source) or _ENTITY_DECL_RE.search(source):
        raise ParseError(
            "Confluence storage input may not contain DOCTYPE or ENTITY declarations (XXE / billion-laughs hardening)"
        )

    prepared = _decode_html_entities(source)
    wrapped = _wrap_for_parsing(prepared)
    parser = ET.XMLParser(
        remove_blank_text=False,
        remove_comments=False,
        strip_cdata=False,
        recover=False,
        no_network=True,
        load_dtd=False,
        huge_tree=False,
    )
    try:
        return ET.fromstring(wrapped.encode("utf-8"), parser=parser)
    except ET.XMLSyntaxError as ex:
        raise ParseError(f"Confluence storage XML is not well-formed: {ex}") from ex


def _enumerate_attachment_names(source: str) -> tuple[str, ...]:
    """Return exact live ``ri:attachment`` identities in document order."""

    root = _parse_fragment_to_element(source)
    return tuple(
        dict.fromkeys(
            filename
            for element in root.iter(ri_tag("attachment"))
            if (filename := element.get(ri_attr("filename"))) is not None
        )
    )


# ---------------------------------------------------------------------------
# Parser context
# ---------------------------------------------------------------------------


@dataclass
class ParserContext:
    """Shared mutable state threaded through parse recursion."""

    registry: MacroRegistry
    root: ET._Element
    color_capabilities: ColorCapabilityMatrix = DEFAULT_COLOR_CAPABILITIES
    warnings: list[str] = field(default_factory=list)
    losses: list[LocatedLoss] = field(default_factory=list)
    layout_depth: int = 0

    def add_loss(self, message: str, element: ET._Element) -> None:
        # Losses are occurrence records, not a set of message templates.
        # Typed migration approval is occurrence-scoped, so preserving every
        # append is required even when adjacent constructs emit identical text.
        self.losses.append(LocatedLoss(message, storage_semantic_path(self.root, element)))


@dataclass(frozen=True)
class LocatedLoss:
    """One parser loss bound directly to its storage construct."""

    message: str
    semantic_path: tuple[str, ...]


# ---------------------------------------------------------------------------
# Element serialization (for opaque preservation)
# ---------------------------------------------------------------------------


def _summarize_inline_label(element: ET._Element) -> str:
    """Pick a short, human-friendly hint for an inline opaque element.

    The label is shown as the visible text of the ``[label](cfx:op-…)``
    link in the rendered Markdown. It is **not** load-bearing — the
    SHA-256 hash of the raw XML is the authoritative identity — so we
    pick the most useful hint we can derive from the element type.
    """

    if not isinstance(element.tag, str):
        return "cfx"
    tag = strip_ns(element.tag)
    ns = ns_of(element.tag)
    if ns == AC_URI:
        if tag == "structured-macro":
            name = element.get(ac_attr("name")) or "macro"
            for child in element:
                if isinstance(child.tag, str) and strip_ns(child.tag) == "parameter":
                    pname = child.get(ac_attr("name")) or ""
                    if pname == "key" and child.text:
                        return f"{name}:{child.text.strip()}"
            return f"cfx:{name}"
        if tag == "link":
            visible = "".join(
                "".join(child.itertext())
                for child in element
                if isinstance(child.tag, str) and strip_ns(child.tag) in {"plain-text-link-body", "link-body"}
            ).strip()
            if visible:
                return visible
            for child in element:
                if not isinstance(child.tag, str):
                    continue
                child_local = strip_ns(child.tag)
                if child_local == "user":
                    key = child.get(ri_attr("userkey")) or ""
                    return "@user"
                if child_local == "page":
                    title = child.get(ri_attr("content-title")) or ""
                    return title or "page"
                if child_local == "attachment":
                    fname = child.get(ri_attr("filename")) or ""
                    return fname or "attachment"
            return "link"
        if tag == "image":
            return "image"
        if tag == "time":
            return (element.text or element.get("datetime") or "time").strip()
        return f"cfx:{tag}"
    if tag == "time":
        return (element.text or element.get("datetime") or "time").strip()
    return f"cfx:{tag}" if tag else "cfx"


def _record_opaque_loss(element: ET._Element, ctx: ParserContext) -> None:
    path = storage_semantic_path(ctx.root, element)
    if not any(loss.semantic_path == path for loss in ctx.losses):
        ctx.losses.append(LocatedLoss(_UNSUPPORTED_CONTENT_MESSAGE, path))


def _make_inline_opaque(element: ET._Element, ctx: ParserContext) -> InlineOpaque:
    _record_opaque_loss(element, ctx)
    raw = _serialize_element(element)
    return InlineOpaque(
        raw_xml=raw,
        opaque_id=opaque_id_for(raw),
        label=_summarize_inline_label(element),
        source_path=storage_semantic_path(ctx.root, element),
    )


def _serialize_element(element: ET._Element) -> str:
    """Serialize a single element to a Confluence-shaped XML string.

    The output strips the root-level namespace declarations that lxml
    re-emits for every subtree — Confluence expects them only at the
    document top.
    """

    raw = ET.tostring(
        element,
        encoding="unicode",
        method="xml",
        with_tail=False,
    )
    raw = re.sub(r'\s+xmlns:ac="[^"]+"', "", raw)
    raw = re.sub(r'\s+xmlns:ri="[^"]+"', "", raw)
    raw = re.sub(r'\s+xmlns=""', "", raw)
    return raw


# ---------------------------------------------------------------------------
# Span pre-processing
# ---------------------------------------------------------------------------


def _should_unwrap_span(element: ET._Element) -> bool:
    """True for a ``<span>`` that has no semantic role (GUI pollution)."""

    if strip_ns(element.tag) != "span":
        return False
    attrs = dict(element.attrib)
    style = attrs.pop("style", None)
    if attrs:
        return False
    if style is None:
        return True
    return bool(DEFAULT_COLOR_STYLE_RE.match(style))


# ---------------------------------------------------------------------------
# Inline walker
# ---------------------------------------------------------------------------


class _InlineEscalation(Exception):
    """Signals that the current inline context must escalate to opaque."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _parse_inline(
    element: ET._Element,
    ctx: ParserContext,
) -> tuple[InlineNode, ...]:
    """Walk an element that contains inline content and produce inline nodes."""

    nodes: list[InlineNode] = []
    # Style nodes may only merge with the previous one when the *elements*
    # form a plain run. The AST has no attributes, so this is the last point
    # where an attributed or comment-separated wrapper can still be told apart.
    unmergeable: set[int] = set()
    previous_child: ET._Element | None = None

    if element.text:
        nodes.append(Text(content=element.text))

    for child in element:
        produced_at = len(nodes)
        if not isinstance(child.tag, str):
            ctx.add_loss("XML comments and processing instructions are not represented in Markdown", child)
            if child.tail:
                nodes.append(Text(content=child.tail))
            previous_child = child
            continue
        tag = strip_ns(child.tag)
        ns = ns_of(child.tag)

        if _should_unwrap_span(child):
            # Inline the span's content in place.
            nodes.extend(_parse_inline_span(child, ctx))
            if child.tail:
                nodes.append(Text(content=child.tail))
            previous_child = child
            continue

        if ns == AC_URI and tag == "structured-macro":
            nodes.append(_parse_inline_structured_macro(child, ctx))
        elif ns == AC_URI and tag == "image":
            nodes.append(_parse_image(child, ctx))
        elif ns == AC_URI and tag == "emoticon":
            nodes.append(_parse_emoticon(child, ctx))
        elif ns is None or ns == "":
            nodes.extend(_parse_html_inline(child, ctx))
        else:
            # Unknown ac:/ri: inline element — preserve verbatim as
            # an inline opaque reference instead of escalating the
            # whole paragraph to a block-level opaque.
            nodes.append(_make_inline_opaque(child, ctx))

        if len(nodes) > produced_at and not continues_plain_style_run(previous_child, child):
            unmergeable.add(produced_at)

        if child.tail:
            nodes.append(Text(content=child.tail))
        previous_child = child

    return _coalesce_adjacent_text(nodes, unmergeable=frozenset(unmergeable))


def _coalesce_adjacent_text(
    nodes: list[InlineNode],
    *,
    unmergeable: frozenset[int] = frozenset(),
) -> tuple[InlineNode, ...]:
    """Keep storage and Markdown parses on one semantic owner per visible inline run.

    The merged children are coalesced recursively, so a run collapses all the
    way down to one ``Text``. Stopping at ``children=a + b`` would leave
    ``Strong(Text("A"), Text("B"))`` where a Markdown parse of the very same
    document yields ``Strong(Text("AB"))``: the two trees render identically
    but no longer correspond node-for-node, and remote splice then fails to
    recognize the block as untouched and rewrites storage the user never
    edited.
    """

    merged: list[InlineNode] = []
    for index, node in enumerate(nodes):
        if merged and (
            (isinstance(node, Text) and isinstance(merged[-1], Text))
            or (isinstance(node, InlineCode) and isinstance(merged[-1], InlineCode))
        ):
            previous = merged[-1]
            assert isinstance(previous, (Text, InlineCode))
            assert isinstance(node, (Text, InlineCode))
            merged[-1] = replace(previous, content=previous.content + node.content)
        elif merged and isinstance(node, Strong) and isinstance(merged[-1], Strong) and index not in unmergeable:
            previous_strong = merged[-1]
            assert isinstance(previous_strong, Strong)
            merged[-1] = Strong(children=_coalesce_adjacent_text(list(previous_strong.children + node.children)))
        elif merged and isinstance(node, Emphasis) and isinstance(merged[-1], Emphasis) and index not in unmergeable:
            previous_emphasis = merged[-1]
            assert isinstance(previous_emphasis, Emphasis)
            merged[-1] = Emphasis(children=_coalesce_adjacent_text(list(previous_emphasis.children + node.children)))
        else:
            merged.append(node)

    result: list[InlineNode] = []
    for node in merged:
        if not isinstance(node, Strong) or not node.children:
            result.append(node)
            continue
        children = list(node.children)
        leading = ""
        trailing = ""
        if isinstance(children[0], Text):
            match = re.match(r"\s+", children[0].content)
            if match is not None:
                leading = match.group(0)
                remaining = children[0].content[len(leading) :]
                children[:1] = [Text(content=remaining)] if remaining else []
        if children and isinstance(children[-1], Text):
            match = re.search(r"\s+$", children[-1].content)
            if match is not None:
                trailing = match.group(0)
                remaining = children[-1].content[: -len(trailing)]
                children[-1:] = [Text(content=remaining)] if remaining else []
        if leading:
            result.append(Text(content=leading))
        result.append(Strong(children=tuple(children)))
        if trailing:
            result.append(Text(content=trailing))
    return tuple(result)


def _parse_inline_span(
    span: ET._Element,
    ctx: ParserContext,
) -> tuple[InlineNode, ...]:
    """Expand a decorative span's children as inline nodes.

    We walk the span's children directly rather than constructing a
    synthetic parent — copying elements is risky because lxml's
    ``tostring`` includes tails by default.
    """

    result: list[InlineNode] = []
    if span.text:
        result.append(Text(content=span.text))
    for child in span:
        result.extend(_parse_single_inline_child(child, ctx))
        if child.tail:
            result.append(Text(content=child.tail))
    return tuple(result)


def _parse_single_inline_child(
    child: ET._Element,
    ctx: ParserContext,
) -> list[InlineNode]:
    """Parse one inline child element (no text / tail handling)."""

    tag = strip_ns(child.tag)
    ns = ns_of(child.tag)

    if ns == AC_URI and tag == "structured-macro":
        return [_parse_inline_structured_macro(child, ctx)]
    if ns == AC_URI and tag == "image":
        return [_parse_image(child, ctx)]
    if ns == AC_URI and tag == "emoticon":
        return [_parse_emoticon(child, ctx)]
    if ns is None or ns == "":
        return _parse_html_inline(child, ctx)
    return [_make_inline_opaque(child, ctx)]


def ns_prefix(uri: str | None) -> str:
    if uri == AC_URI:
        return "ac"
    if uri == RI_URI:
        return "ri"
    return uri or ""


_EMOTICON_TEXT = {
    "information": "ℹ️",
    "tick": "✅",
    "cross": "❌",
    "warning": "⚠️",
    "smile": "🙂",
    "sad": "🙁",
}


def _parse_emoticon(element: ET._Element, ctx: ParserContext) -> Text:
    ctx.add_loss("Confluence emoticon macros are rendered as Unicode text", element)
    name = (element.get(ac_attr("name")) or "emoticon").strip().lower()
    return Text(content=_EMOTICON_TEXT.get(name, f":{name}:"))


def _parse_inline_structured_macro(
    element: ET._Element,
    ctx: ParserContext,
) -> InlineNode:
    """Parse macros with an explicit inline Markdown representation."""

    name = element.get(ac_attr("name")) or ""
    if name == "jira":
        handler = ctx.registry.get_by_cfx_name(name)
        if handler is not None:
            directive = handler.from_cfx(element, lambda body: _parse_blocks(body, ctx))
            if isinstance(directive, DirectiveMacro) and directive.body is None:
                return InlineDirectiveMacro(
                    name=directive.name,
                    parameters=directive.parameters,
                    macro_id=element.get(ac_attr("macro-id")),
                    macro_schema_version=element.get(ac_attr("schema-version")),
                )
    return _make_inline_opaque(element, ctx)


# ---------------------------------------------------------------------------
# HTML inline handlers
# ---------------------------------------------------------------------------


def _parse_html_inline(
    element: ET._Element,
    ctx: ParserContext,
) -> list[InlineNode]:
    tag = strip_ns(element.tag)

    if tag == "strong" or tag == "b":
        return [Strong(children=_parse_inline(element, ctx))]
    if tag == "em" or tag == "i":
        return [Emphasis(children=_parse_inline(element, ctx))]
    if tag == "del" or tag == "s":
        return [Strikethrough(children=_parse_inline(element, ctx))]
    if tag == "code":
        return [InlineCode(content=_collect_text(element))]
    if tag == "a":
        href = element.get("href") or ""
        title = element.get("title")
        return [
            Link(
                url=href,
                title=title,
                children=_parse_inline(element, ctx),
            )
        ]
    if tag == "br":
        return [HardBreak()]
    if tag == "img":
        src = element.get("src") or ""
        alt = element.get("alt") or ""
        title = element.get("title")
        return [Image(src=src, alt=alt, title=title)]
    if tag == "u" or tag == "ins":
        return [Underline(children=_parse_inline(element, ctx))]
    if tag == "sub":
        return [Subscript(children=_parse_inline(element, ctx))]
    if tag == "sup":
        return [Superscript(children=_parse_inline(element, ctx))]
    if tag == "cite":
        return [Citation(children=_parse_inline(element, ctx))]
    if tag == "span":
        style = element.get("style") or ""
        background_match = re.search(r"background-color:\s*([^;\"']+)", style)
        color_match = re.search(r"(?<!-)color:\s*([^;\"']+)", style)
        if background_match or color_match:
            children = _parse_inline(element, ctx)
            if color_match:
                color = color_match.group(1).strip()
                if coarse_color(color) is None:
                    ctx.warnings.append(f"unknown foreground color preserved exactly: {color}")
                children = (ColorSpan(color=color, children=children),)
            if background_match:
                background = background_match.group(1).strip()
                if coarse_color(background) is None:
                    ctx.warnings.append(f"unknown background color preserved exactly: {background}")
                children = (
                    ColorSpan(
                        color=background,
                        children=children,
                        background=True,
                    ),
                )
            return list(children)
        # A span we didn't unwrap — treat children as inline, lossy.
        ctx.add_loss("inline <span> with unknown attributes stripped", element)
        return list(_parse_inline(element, ctx))

    # Unknown HTML element — preserve verbatim as inline opaque so the
    # surrounding paragraph keeps its native Markdown shape.
    return [_make_inline_opaque(element, ctx)]


def _collect_text(element: ET._Element) -> str:
    """Collect all descendant text as a single string (for ``<code>`` content)."""

    return "".join(element.itertext())


# ---------------------------------------------------------------------------
# Image handling
# ---------------------------------------------------------------------------


def _parse_image(element: ET._Element, ctx: ParserContext) -> Image:
    alt = element.get(ac_attr("alt")) or ""
    title = element.get(ac_attr("title"))
    width = _int_or_none(element.get(ac_attr("width")))
    height = _int_or_none(element.get(ac_attr("height")))
    thumbnail = (element.get(ac_attr("thumbnail")) or "").lower() == "true"
    align = _parse_alignment(element)

    src = ""
    attachment_filename: str | None = None
    for child in element:
        ctag = strip_ns(child.tag)
        if ns_of(child.tag) == RI_URI:
            if ctag == "attachment":
                src = child.get(ri_attr("filename")) or ""
                attachment_filename = src
            elif ctag == "url":
                src = child.get(ri_attr("value")) or ""
        # Ignore other children (captions etc. — v0.1 scope).

    return Image(
        src=src,
        attachment_filename=attachment_filename,
        alt=alt,
        title=title,
        width=width,
        height=height,
        thumbnail=thumbnail,
        align=align,
    )


def _int_or_none(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Block walker
# ---------------------------------------------------------------------------


_LAYOUT_TAGS = frozenset({"layout", "layout-section", "layout-cell"})


def _is_layout_element(element: ET._Element) -> bool:
    if not isinstance(element.tag, str):
        return False
    if ns_of(element.tag) != AC_URI:
        return False
    return strip_ns(element.tag) in _LAYOUT_TAGS


def _parse_blocks(
    parent: ET._Element,
    ctx: ParserContext,
) -> tuple[BlockNode, ...]:
    blocks: list[BlockNode] = []

    # Text immediately inside a block-level element. Confluence rarely
    # emits loose text here, but if it does, wrap it in a synthetic
    # paragraph.
    if parent.text and parent.text.strip():
        blocks.append(Paragraph(children=(Text(content=parent.text),)))

    for child in parent:
        if not isinstance(child.tag, str):
            ctx.add_loss("XML comments and processing instructions are not represented in Markdown", child)
            if child.tail and child.tail.strip():
                blocks.append(Paragraph(children=(Text(content=child.tail),)))
            continue
        if _is_layout_element(child):
            if ctx.layout_depth == 0:
                ctx.add_loss("Confluence layout structure is flattened in Markdown", child)
            # Confluence layout wrappers carry no semantic content of
            # their own — recurse and inline their children at this
            # level so we never have to wrap them in a placeholder
            # node that the renderer might mistreat.
            ctx.layout_depth += 1
            try:
                blocks.extend(_parse_blocks(child, ctx))
            finally:
                ctx.layout_depth -= 1
        else:
            block = _parse_block(child, ctx)
            if block is not None:
                blocks.append(replace(block, source_path=storage_semantic_path(ctx.root, child)))
        if child.tail and child.tail.strip():
            blocks.append(Paragraph(children=(Text(content=child.tail),)))

    return tuple(blocks)


def _parse_block(
    element: ET._Element,
    ctx: ParserContext,
) -> BlockNode | None:
    tag = strip_ns(element.tag)
    ns = ns_of(element.tag)

    if ns is None or ns == "":
        if tag in _HEADING_TAGS:
            return _parse_heading(element, ctx)
        if tag == "p":
            return _parse_paragraph(element, ctx)
        if tag in ("ul", "ol"):
            return _parse_list(element, ctx)
        if tag == "li":
            # A lone <li> — wrap in a bullet list so higher levels can
            # still render it (rarely happens in practice).
            return List(
                list_type=ListType.BULLET,
                items=(ListItem(children=_parse_blocks(element, ctx)),),
            )
        if tag == "blockquote":
            loss_count = len(ctx.losses)
            children = _parse_blocks(element, ctx)
            if any(isinstance(child, OpaqueBlock) for child in children):
                del ctx.losses[loss_count:]
                return _as_opaque(element, ctx)
            return BlockQuote(children=children)
        if tag == "hr":
            return HorizontalRule()
        if tag == "pre":
            return _parse_pre(element, ctx)
        if tag == "table":
            return _parse_table(element, ctx)
        if tag == "div":
            # A div with pure block children: transparently flatten.
            nested = _parse_blocks(element, ctx)
            if len(nested) == 1:
                return nested[0]
            if nested:
                # Multiple blocks — we do not have a container, so
                # escalate to opaque to preserve structure faithfully.
                return _as_opaque(element, ctx)
            return None
        # Unknown HTML element → opaque
        return _as_opaque(element, ctx)

    if ns == AC_URI:
        if tag == "structured-macro":
            return _parse_structured_macro(element, ctx)
        if tag == "image":
            # An image floating at block level. Wrap it in a paragraph.
            return Paragraph(children=(_parse_image(element, ctx),))
        if tag in _LAYOUT_TAGS:
            # Defensive: layout elements are normally inlined by
            # ``_parse_blocks``; reaching here means a layout element
            # appears as a stand-alone block. Treat it as opaque.
            return _as_opaque(element, ctx)
        return _as_opaque(element, ctx)

    if ns == RI_URI:
        # ri:* should not appear at block level directly.
        return _as_opaque(element, ctx)

    return _as_opaque(element, ctx)


def _parse_heading(element: ET._Element, ctx: ParserContext) -> BlockNode:
    level = int(strip_ns(element.tag)[1])
    try:
        inline = _parse_inline(element, ctx)
    except _InlineEscalation as ex:
        ctx.warnings.append(f"heading escalated to opaque: {ex.reason}")
        return _as_opaque(element, ctx)
    return Heading(level=level, children=inline)


def _parse_paragraph(element: ET._Element, ctx: ParserContext) -> BlockNode | None:
    standalone_macro = _parse_standalone_macro_paragraph(element, ctx)
    if standalone_macro is not None:
        return standalone_macro
    try:
        inline = _parse_inline(element, ctx)
    except _InlineEscalation as ex:
        ctx.warnings.append(f"paragraph escalated to opaque: {ex.reason}")
        return _as_opaque(element, ctx)
    # Project an empty storage paragraph as an explicit Markdown break. The
    # managed remote-subtree path retains the exact empty ``<p></p>`` bytes
    # when the user edits elsewhere, while the visible projection remains
    # stable and editable boundaries stay explicit.
    if _is_paragraph_empty(inline):
        return Paragraph(children=(HardBreak(),), align=_parse_alignment(element))
    return Paragraph(children=inline, align=_parse_alignment(element))


def _parse_standalone_macro_paragraph(
    element: ET._Element,
    ctx: ParserContext,
) -> DirectiveMacro | None:
    """Promote a paragraph containing only a block-semantic macro."""

    if element.text and element.text.strip():
        return None
    children = [child for child in element if isinstance(child.tag, str)]
    if len(children) != 1:
        return None
    child = children[0]
    if child.tail and child.tail.strip():
        return None
    if ns_of(child.tag) != AC_URI or strip_ns(child.tag) != "structured-macro":
        return None
    if (child.get(ac_attr("name")) or "") != "toc":
        return None
    parsed = _parse_structured_macro(child, ctx)
    return parsed if isinstance(parsed, DirectiveMacro) else None


def _parse_alignment(element: ET._Element) -> str | None:
    """Return a supported paragraph or image alignment."""

    explicit = (element.get(ac_attr("align")) or element.get("align") or "").lower()
    if explicit in {"left", "center", "right"}:
        return explicit
    style = element.get("style") or ""
    match = re.search(r"(?:^|;)\s*text-align\s*:\s*(left|center|right)\s*(?=;|$)", style, re.IGNORECASE)
    return match.group(1).lower() if match else None


def _is_paragraph_empty(nodes: tuple[InlineNode, ...]) -> bool:
    """True if an inline sequence carries no visible content."""

    for node in nodes:
        if isinstance(node, Text):
            if node.content.strip():
                return False
        elif isinstance(node, SoftBreak):
            continue
        elif isinstance(node, HardBreak):
            return False
        else:
            return False
    return True


def _parse_list(element: ET._Element, ctx: ParserContext) -> List:
    tag = strip_ns(element.tag)
    list_type = ListType.BULLET if tag == "ul" else ListType.ORDERED
    start = 1
    start_attr = element.get("start")
    if start_attr and start_attr.isdigit():
        start = int(start_attr)

    items: list[ListItem] = []
    for child in element:
        if strip_ns(child.tag) == "li":
            items.append(_parse_list_item(child, ctx))
    return List(list_type=list_type, items=tuple(items), start=start)


def _parse_list_item(element: ET._Element, ctx: ParserContext) -> ListItem:
    # ``<li>`` mixes inline text with block-level children (nested
    # lists, paragraphs). We pack inline runs into a paragraph on the
    # fly.
    blocks: list[BlockNode] = []
    inline_buffer: list[InlineNode] = []

    def flush() -> None:
        if inline_buffer:
            blocks.append(Paragraph(children=_coalesce_adjacent_text(inline_buffer)))
            inline_buffer.clear()

    if element.text and element.text.strip():
        inline_buffer.append(Text(content=element.text))

    for child in element:
        tag = strip_ns(child.tag)
        appended_inline = False
        if tag in {"ul", "ol"}:
            flush()
            blocks.append(_parse_list(child, ctx))
        elif tag == "p":
            flush()
            para = _parse_paragraph(child, ctx)
            if para is not None:
                blocks.append(para)
        elif tag in _BLOCK_TAGS_HTML or (ns_of(child.tag) == AC_URI and tag == "structured-macro"):
            flush()
            sub = _parse_block(child, ctx)
            if sub is not None:
                blocks.append(sub)
        else:
            # Treat as inline
            try:
                inline_buffer.extend(_parse_single_inline_child(child, ctx))
                appended_inline = True
            except _InlineEscalation:
                flush()
                blocks.append(_as_opaque(child, ctx))
        # Preserve the tail verbatim after an inline child (matching
        # _parse_inline): whitespace between inline elements -- e.g. the single
        # space between two adjacent <code> spans -- is meaningful content, not
        # XML formatting. After a block child only a non-whitespace tail starts a
        # new inline run; formatting whitespace is dropped.
        if child.tail and (appended_inline or child.tail.strip()):
            inline_buffer.append(Text(content=child.tail))

    flush()
    return ListItem(children=tuple(blocks))


def _parse_pre(element: ET._Element, ctx: ParserContext) -> BlockNode | None:
    """Parse ``<pre>`` as a code block, or opaque if it has weird content.

    Confluence's web editor occasionally emits ``<pre><code>...</code></pre>``
    where the inner ``<code>`` contains real child elements such as
    ``<br/>`` or even ``<ac:image>``. Those cannot be represented as a
    fenced code block, so we fall back to opaque preservation.
    """

    inner = element
    children = list(element)
    if len(children) == 1 and strip_ns(children[0].tag) == "code":
        inner = children[0]

    for desc in inner.iterdescendants():
        dtag = strip_ns(desc.tag)
        dns = ns_of(desc.tag)
        if dtag == "br":
            continue
        # Any element other than <br> inside a <pre> means we cannot
        # represent the contents as a flat code block — preserve the
        # whole element verbatim.
        return _as_opaque(element, ctx)

    # Pure text code block.
    text = collect_text_with_breaks(inner)
    if not text.strip():
        # Empty <pre>: editor noise, drop it.
        return None
    language = None
    code_class = inner.get("class") or ""
    m = re.match(r"^language-(.*)$", code_class)
    if m:
        language = m.group(1)
    return CodeBlock(content=text, language=language)


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------

_TABLE_SPAN_LIMIT_MESSAGE = "table cell span exceeds the safe editable limit"
_TABLE_SPAN_OVERLAP_MESSAGE = "table cell spans overlap in the logical grid"


def _parse_span_attr(value: str | None) -> int | None:
    """Parse a ``colspan`` / ``rowspan`` attribute, defaulting to 1.

    Confluence sometimes emits the value as a stringified integer, an
    empty string, or omits it altogether. The HTML spec treats any
    non-positive value as 1. Values above the editable safety bound return
    ``None`` so the whole table can be preserved opaquely without allocating
    an attacker-controlled logical grid.
    """

    if not value:
        return 1
    try:
        n = int(value)
    except ValueError:
        return 1
    normalized = max(1, n)
    return normalized if normalized <= MAX_TABLE_SPAN else None


def _parse_cell_alignment(element: ET._Element) -> CellAlign:
    value = (element.get("align") or "").strip().lower()
    style = element.get("style") or ""
    match = re.search(r"(?:^|;)\s*text-align\s*:\s*(left|center|right)\s*(?=;|$)", style, re.IGNORECASE)
    if match is not None:
        value = match.group(1).lower()
    return {
        "left": CellAlign.LEFT,
        "center": CellAlign.CENTER,
        "right": CellAlign.RIGHT,
    }.get(value, CellAlign.NONE)


def _parse_cell_content(
    cell_el: ET._Element,
    ctx: ParserContext,
) -> tuple[InlineNode, ...]:
    """Walk a ``<td>`` / ``<th>`` cell and produce inline content.

    The walker is forgiving with structural HTML wrappers Confluence
    routinely injects:

    * Attribute-free ``<div>`` elements and
      ``<div class="content-wrapper">`` are unwrapped — their children
      become direct cell children for the purposes of this walk.
    * Multiple ``<p>`` children are joined with hard breaks (GFM tables
      have no notion of multi-paragraph cells, so the same
      representation that ``<br/>`` would produce is used).
    * Bare inline siblings outside ``<p>`` wrappers are appended in
      place.
    """

    direct_children = [child for child in cell_el if isinstance(child.tag, str)]
    flat_children = list(_flatten_cell_children(cell_el))
    has_p = any(isinstance(c.tag, str) and strip_ns(c.tag) == "p" for c in flat_children)
    if not has_p and flat_children == direct_children:
        return _parse_inline(cell_el, ctx)

    out: list[InlineNode] = []
    if cell_el.text and cell_el.text.strip():
        out.append(Text(content=cell_el.text))

    for child in flat_children:
        if not isinstance(child.tag, str):
            continue
        if strip_ns(child.tag) == "p":
            inline = _parse_inline(child, ctx)
            if out and inline:
                out.append(HardBreak())
            align = _parse_alignment(child)
            if align and inline:
                out.append(AlignedSpan(align=align, children=inline))
            else:
                out.extend(inline)
            if child.tail and child.tail.strip():
                out.append(HardBreak())
                out.append(Text(content=child.tail))
        else:
            out.extend(_parse_single_inline_child(child, ctx))
            if child.tail and child.tail.strip():
                out.append(Text(content=child.tail))
    return tuple(out)


def _flatten_cell_children(
    parent: ET._Element,
) -> list[ET._Element]:
    """Yield ``parent``'s children, unwrapping known structural divs.

    Recurses so deeply nested wrappers (``<div><div><p>X</p></div></div>``)
    flatten to ``[<p>X</p>]``.
    """

    out: list[ET._Element] = []
    for child in parent:
        if not isinstance(child.tag, str):
            continue
        local = strip_ns(child.tag)
        ns = ns_of(child.tag)
        if (ns is None or ns == "") and local == "div" and _is_transparent_cell_div(child):
            inner = _flatten_cell_children(child)
            if not inner and child.text and child.text.strip():
                # Pure text inside a div — synthesise a <p> shell so
                # the caller's paragraph join-with-break logic still
                # applies.
                pseudo = ET.Element("p")
                pseudo.text = child.text
                out.append(pseudo)
                continue
            out.extend(inner)
            continue
        out.append(child)
    return out


def _is_transparent_cell_div(element: ET._Element) -> bool:
    """Return whether a cell div is a structure-only editor wrapper."""

    attrs = dict(element.attrib)
    if not attrs:
        return True
    if set(attrs) != {"class"}:
        return False
    classes = set(attrs["class"].split())
    return bool(classes) and classes <= {"content-wrapper"}


def _parse_table(element: ET._Element, ctx: ParserContext) -> BlockNode:
    # A GFM table cannot carry these atoms without dropping their remote
    # identity or reshaping the table.  Preserve the exact table as a single
    # authenticated opaque block instead of pretending a full migration is
    # ownership-equivalent.
    if any(
        isinstance(descendant.tag, str)
        and (
            ns_of(descendant.tag) == AC_URI
            and strip_ns(descendant.tag) == "link"
            and any(
                isinstance(child.tag, str) and strip_ns(child.tag) in {"attachment", "user"} for child in descendant
            )
        )
        for descendant in element.iter()
    ):
        return _as_opaque(element, ctx)

    # Walk thead/tbody/tfoot to collect rows.
    header: TableRow | None = None
    body_rows: list[TableRow] = []

    def make_row(tr: ET._Element) -> TableRow | None:
        cells: list[TableCell] = []
        for cell_el in tr:
            if not isinstance(cell_el.tag, str):
                continue
            ctag = strip_ns(cell_el.tag)
            if ctag not in ("td", "th"):
                continue
            colspan = _parse_span_attr(cell_el.get("colspan"))
            rowspan = _parse_span_attr(cell_el.get("rowspan"))
            if colspan is None or rowspan is None:
                ctx.add_loss(_TABLE_SPAN_LIMIT_MESSAGE, cell_el)
                return None
            kind = CellType.HEADER if ctag == "th" else CellType.DATA
            background, background_losses = parse_cell_background(
                {str(name): str(value) for name, value in cell_el.attrib.items()},
                capabilities=ctx.color_capabilities,
            )
            for background_loss in background_losses:
                ctx.add_loss(background_loss, cell_el)
            try:
                inline = _parse_cell_content(cell_el, ctx)
            except _InlineEscalation:
                return None
            cells.append(
                TableCell(
                    kind=kind,
                    children=inline,
                    align=_parse_cell_alignment(cell_el),
                    source_style=cell_el.get("style"),
                    colspan=colspan,
                    rowspan=rowspan,
                    background=background,
                )
            )
        return TableRow(cells=tuple(cells))

    for section in element:
        if not isinstance(section.tag, str):
            continue
        stag = strip_ns(section.tag)
        if stag == "thead":
            for tr in section:
                if strip_ns(tr.tag) == "tr":
                    row = make_row(tr)
                    if row is None:
                        return _as_opaque(element, ctx)
                    if header is None:
                        header = row
                    else:
                        body_rows.append(row)
        elif stag == "tbody":
            for tr in section:
                if strip_ns(tr.tag) == "tr":
                    row = make_row(tr)
                    if row is None:
                        return _as_opaque(element, ctx)
                    # If all cells in the first row are TH and there is no
                    # dedicated thead, promote it to the header.
                    if header is None and row.cells and all(c.kind == CellType.HEADER for c in row.cells):
                        header = row
                    else:
                        body_rows.append(row)
        elif stag == "tr":
            row = make_row(section)
            if row is None:
                return _as_opaque(element, ctx)
            if header is None and row.cells and all(c.kind == CellType.HEADER for c in row.cells):
                header = row
            else:
                body_rows.append(row)
        elif stag == "colgroup" or stag == "col" or stag == "caption":
            # Silently ignore for v0.1 — width hints don't affect content.
            continue
        elif stag == "tfoot":
            for tr in section:
                if strip_ns(tr.tag) == "tr":
                    row = make_row(tr)
                    if row is None:
                        return _as_opaque(element, ctx)
                    body_rows.append(row)

    rows = ([header] if header is not None else []) + body_rows
    try:
        layout = bounded_table_grid_layout(rows)
    except TableGridError as error:
        message = _TABLE_SPAN_OVERLAP_MESSAGE if isinstance(error, TableGridOverlap) else _TABLE_SPAN_LIMIT_MESSAGE
        ctx.add_loss(message, element)
        return _as_opaque(element, ctx)
    column_count = layout.width

    alignments_by_column: list[set[CellAlign]] = [set() for _ in range(column_count)]
    for row, anchors in zip(rows, layout.anchor_columns, strict=True):
        if row is None:
            continue
        for cell, column in zip(row.cells, anchors, strict=True):
            for spanned_column in range(column, min(column + cell.colspan, column_count)):
                alignments_by_column[spanned_column].add(cell.align)
    if any(len(cell_alignments) > 1 for cell_alignments in alignments_by_column):
        ctx.add_loss("table column contains mixed cell alignments that Markdown cannot preserve", element)
    alignments = tuple(
        next(iter(cell_alignments)) if len(cell_alignments) == 1 else CellAlign.NONE
        for cell_alignments in alignments_by_column
    )
    if header is None and body_rows:
        ctx.add_loss("body-only tables require a synthetic Markdown header row", element)
    return Table(header=header, body=tuple(body_rows), alignments=alignments)


# ---------------------------------------------------------------------------
# Structured macros
# ---------------------------------------------------------------------------


def _parse_structured_macro(
    element: ET._Element,
    ctx: ParserContext,
) -> BlockNode:
    name = element.get(ac_attr("name")) or ""
    if name == "code":
        return _parse_code_macro(element)

    handler = ctx.registry.get_by_cfx_name(name)
    if handler is not None:
        result = handler.from_cfx(element, lambda body: _parse_blocks(body, ctx))
        if result is not None:
            if isinstance(result, DirectiveMacro):
                # Capture the exact remote identity so regeneration matches the
                # source byte for byte. ``None`` schema-version means the source
                # omitted the attribute, which must survive the round-trip.
                result = replace(
                    result,
                    macro_id=element.get(ac_attr("macro-id")),
                    macro_schema_version=element.get(ac_attr("schema-version")),
                )
            return result
    # Unknown or unmatched: preserve.
    return _as_opaque(element, ctx)


def _parse_code_macro(element: ET._Element) -> CodeBlock:
    language: str | None = None
    body_text = ""
    for child in element:
        tag = strip_ns(child.tag)
        if tag == "parameter":
            pname = child.get(ac_attr("name")) or ""
            if pname == "language":
                language = (child.text or "").strip() or None
        elif tag == "plain-text-body":
            body_text = child.text or ""
    if language == "none":
        language = None
    return CodeBlock(
        content=body_text,
        language=language,
        macro_id=element.get(ac_attr("macro-id")),
        macro_schema_version=element.get(ac_attr("schema-version")),
    )


# ---------------------------------------------------------------------------
# Opaque helpers
# ---------------------------------------------------------------------------


def _as_opaque(element: ET._Element, ctx: ParserContext) -> OpaqueBlock:
    _record_opaque_loss(element, ctx)
    raw = _serialize_element(element)
    return OpaqueBlock(
        raw_xml=raw,
        opaque_id=opaque_id_for(raw),
        source_path=storage_semantic_path(ctx.root, element),
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def parse_cfx_with_diagnostics(
    source: str,
    *,
    registry: MacroRegistry | None = None,
    color_capabilities: ColorCapabilityMatrix = DEFAULT_COLOR_CAPABILITIES,
) -> tuple[Document, list[str], list[str]]:
    """Parse a Confluence storage fragment into a cfxmark AST.

    :param source: Confluence storage format XML fragment as a string.
    :param registry: macro registry, defaults to :data:`cfxmark.macros.default_registry`.
    :returns: ``(document, warnings, losses)`` tuple.
    """

    root = _parse_fragment_to_element(source)
    ctx = ParserContext(root=root, registry=registry or default_registry, color_capabilities=color_capabilities)
    blocks = _parse_blocks(root, ctx)
    return Document(children=blocks), ctx.warnings, [loss.message for loss in ctx.losses]


def parse_cfx_with_loss_locations(
    source: str,
    *,
    registry: MacroRegistry | None = None,
    color_capabilities: ColorCapabilityMatrix = DEFAULT_COLOR_CAPABILITIES,
) -> tuple[Document, list[str], list[LocatedLoss]]:
    """Parse storage and retain the exact source path of every loss."""

    root = _parse_fragment_to_element(source)
    ctx = ParserContext(root=root, registry=registry or default_registry, color_capabilities=color_capabilities)
    blocks = _parse_blocks(root, ctx)
    return Document(children=blocks), ctx.warnings, ctx.losses


def parse_cfx(
    source: str,
    *,
    registry: MacroRegistry | None = None,
) -> tuple[Document, list[str]]:
    """Parse storage and expose round-trip losses through warnings."""

    document, warnings, losses = parse_cfx_with_diagnostics(source, registry=registry)
    warnings.extend(f"round-trip unsafe: {message}" for message in losses)
    return document, warnings


__all__ = ["LocatedLoss", "parse_cfx", "parse_cfx_with_diagnostics", "parse_cfx_with_loss_locations"]
