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
3. **Unknown inline escalates.** If an unknown element appears inline
   in the middle of a paragraph, the whole paragraph is re-emitted as
   an :class:`OpaqueBlock` with a warning.
4. **GUI pollution is stripped.** ``<span>`` elements whose only
   attribute is a ``style`` setting the default Confluence text color
   (``var(--ds-text,#172b4d)``) are transparently unwrapped — these
   are inserted by the web editor and carry no semantic weight.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

import lxml.etree as ET

from cfxmark.ast import (
    BlockNode,
    BlockQuote,
    CellAlign,
    CellType,
    Citation,
    CodeBlock,
    ColorSpan,
    Document,
    Emphasis,
    HardBreak,
    Heading,
    HorizontalRule,
    Image,
    InlineCode,
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
from cfxmark.exceptions import ParseError
from cfxmark.macros import MacroRegistry, default_registry
from cfxmark.opaque import opaque_id_for
from cfxmark.xml_ns import (
    AC_URI,
    DEFAULT_COLOR_STYLE_RE,
    RI_URI,
    ac_attr,
    collect_text_with_breaks,
    ns_of,
    ri_attr,
    strip_ns,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})

_BLOCK_TAGS_HTML = frozenset(
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
) | _HEADING_TAGS

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
    return "".join(
        part if part.startswith("<![CDATA[") else _HTML_ENTITY_RE.sub(repl, part)
        for part in parts
    )


# ---------------------------------------------------------------------------
# Root wrapping
# ---------------------------------------------------------------------------


def _wrap_for_parsing(source: str) -> str:
    """Wrap a CF fragment in a namespaced root element for XML parsing."""

    return (
        f'<?xml version="1.0" encoding="utf-8"?>'
        f'<root xmlns="" xmlns:ac="{AC_URI}" xmlns:ri="{RI_URI}">'
        f"{source}"
        f"</root>"
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

    Confluence's storage format is a fragment without a prologue, so
    we can safely refuse anything that even mentions DOCTYPE/ENTITY.
    """

    if _DOCTYPE_RE.search(source) or _ENTITY_DECL_RE.search(source):
        raise ParseError(
            "Confluence storage input may not contain DOCTYPE or ENTITY "
            "declarations (XXE / billion-laughs hardening)"
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


# ---------------------------------------------------------------------------
# Parser context
# ---------------------------------------------------------------------------


@dataclass
class ParserContext:
    """Shared mutable state threaded through parse recursion."""

    registry: MacroRegistry
    warnings: list[str] = field(default_factory=list)


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
            for child in element:
                if not isinstance(child.tag, str):
                    continue
                child_local = strip_ns(child.tag)
                if child_local == "user":
                    key = child.get(ri_attr("userkey")) or ""
                    return f"@user-{key[:8]}" if key else "@user"
                if child_local == "page":
                    title = child.get(ri_attr("content-title")) or ""
                    return f"→{title}" if title else "→page"
                if child_local == "attachment":
                    fname = child.get(ri_attr("filename")) or ""
                    return f"📎{fname}" if fname else "📎attachment"
            return "cfx:link"
        if tag == "image":
            return "image"
        return f"cfx:{tag}"
    return f"cfx:{tag}" if tag else "cfx"


def _make_inline_opaque(element: ET._Element) -> InlineOpaque:
    raw = _serialize_element(element)
    return InlineOpaque(
        raw_xml=raw,
        opaque_id=opaque_id_for(raw),
        label=_summarize_inline_label(element),
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

    if element.text:
        nodes.append(Text(content=element.text))

    for child in element:
        tag = strip_ns(child.tag)
        ns = ns_of(child.tag)

        if _should_unwrap_span(child):
            # Inline the span's content in place.
            nodes.extend(_parse_inline_span(child, ctx))
            if child.tail:
                nodes.append(Text(content=child.tail))
            continue

        if ns == AC_URI and tag == "image":
            nodes.append(_parse_image(child, ctx))
        elif ns is None or ns == "":
            nodes.extend(_parse_html_inline(child, ctx))
        else:
            # Unknown ac:/ri: inline element — preserve verbatim as
            # an inline opaque reference instead of escalating the
            # whole paragraph to a block-level opaque.
            nodes.append(_make_inline_opaque(child))

        if child.tail:
            nodes.append(Text(content=child.tail))

    return tuple(nodes)


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

    if ns == AC_URI and tag == "image":
        return [_parse_image(child, ctx)]
    if ns is None or ns == "":
        return _parse_html_inline(child, ctx)
    return [_make_inline_opaque(child)]


def ns_prefix(uri: str | None) -> str:
    if uri == AC_URI:
        return "ac"
    if uri == RI_URI:
        return "ri"
    return uri or ""


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
        color_match = re.search(r"color:\s*([^;\"']+)", style)
        if color_match:
            return [ColorSpan(color=color_match.group(1).strip(), children=_parse_inline(element, ctx))]
        # A span we didn't unwrap — treat children as inline, lossy.
        ctx.warnings.append("inline <span> with unknown attributes stripped")
        return list(_parse_inline(element, ctx))

    # Unknown HTML element — preserve verbatim as inline opaque so the
    # surrounding paragraph keeps its native Markdown shape.
    return [_make_inline_opaque(element)]


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

    src = ""
    for child in element:
        ctag = strip_ns(child.tag)
        if ns_of(child.tag) == RI_URI:
            if ctag == "attachment":
                src = child.get(ri_attr("filename")) or ""
            elif ctag == "url":
                src = child.get(ri_attr("value")) or ""
        # Ignore other children (captions etc. — v0.1 scope).

    return Image(src=src, alt=alt, title=title, width=width, height=height)


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
        blocks.append(
            Paragraph(children=(Text(content=parent.text),))
        )

    for child in parent:
        if _is_layout_element(child):
            # Confluence layout wrappers carry no semantic content of
            # their own — recurse and inline their children at this
            # level so we never have to wrap them in a placeholder
            # node that the renderer might mistreat.
            blocks.extend(_parse_blocks(child, ctx))
        else:
            block = _parse_block(child, ctx)
            if block is not None:
                blocks.append(block)
        if child.tail and child.tail.strip():
            blocks.append(
                Paragraph(children=(Text(content=child.tail),))
            )

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
            return BlockQuote(children=_parse_blocks(element, ctx))
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
                return _as_opaque(element)
            return None
        # Unknown HTML element → opaque
        return _as_opaque(element)

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
            return _as_opaque(element)
        return _as_opaque(element)

    if ns == RI_URI:
        # ri:* should not appear at block level directly.
        return _as_opaque(element)

    return _as_opaque(element)


def _parse_heading(element: ET._Element, ctx: ParserContext) -> BlockNode:
    level = int(strip_ns(element.tag)[1])
    try:
        inline = _parse_inline(element, ctx)
    except _InlineEscalation as ex:
        ctx.warnings.append(f"heading escalated to opaque: {ex.reason}")
        return _as_opaque(element)
    return Heading(level=level, children=inline)


def _parse_paragraph(element: ET._Element, ctx: ParserContext) -> BlockNode | None:
    try:
        inline = _parse_inline(element, ctx)
    except _InlineEscalation as ex:
        ctx.warnings.append(f"paragraph escalated to opaque: {ex.reason}")
        return _as_opaque(element)
    # Skip empty paragraphs (`<p></p>`, `<p><br/></p>`) — Confluence's
    # GUI editor leaves these behind as accidental vertical spacing and
    # they round-trip as lossy blank lines if we keep them.
    if _is_paragraph_empty(inline):
        return None
    return Paragraph(children=inline)


def _is_paragraph_empty(nodes: tuple[InlineNode, ...]) -> bool:
    """True if an inline sequence carries no visible content."""

    for node in nodes:
        if isinstance(node, Text):
            if node.content.strip():
                return False
        elif isinstance(node, (SoftBreak, HardBreak)):
            continue
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
            blocks.append(Paragraph(children=tuple(inline_buffer)))
            inline_buffer.clear()

    if element.text and element.text.strip():
        inline_buffer.append(Text(content=element.text))

    for child in element:
        tag = strip_ns(child.tag)
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
            except _InlineEscalation:
                flush()
                blocks.append(_as_opaque(child))
        if child.tail and child.tail.strip():
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
        return _as_opaque(element)

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


def _parse_span_attr(value: str | None) -> int:
    """Parse a ``colspan`` / ``rowspan`` attribute, defaulting to 1.

    Confluence sometimes emits the value as a stringified integer, an
    empty string, or omits it altogether. The HTML spec treats any
    non-positive value as 1.
    """

    if not value:
        return 1
    try:
        n = int(value)
    except ValueError:
        return 1
    return max(1, n)


def _parse_cell_content(
    cell_el: ET._Element,
    ctx: ParserContext,
) -> tuple[InlineNode, ...]:
    """Walk a ``<td>`` / ``<th>`` cell and produce inline content.

    The walker is forgiving with structural HTML wrappers Confluence
    routinely injects:

    * ``<div class="content-wrapper">`` (and other generic ``<div>``
      wrappers) are unwrapped — their children become direct cell
      children for the purposes of this walk.
    * Multiple ``<p>`` children are joined with hard breaks (GFM tables
      have no notion of multi-paragraph cells, so the same
      representation that ``<br/>`` would produce is used).
    * Bare inline siblings outside ``<p>`` wrappers are appended in
      place.
    """

    flat_children = list(_flatten_cell_children(cell_el))
    has_p = any(
        isinstance(c.tag, str) and strip_ns(c.tag) == "p" for c in flat_children
    )
    if not has_p:
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
    """Yield ``parent``'s children, transparently unwrapping any
    ``<div>`` (or similarly purely-structural HTML wrapper) the
    Confluence editor inserted around the actual cell content.

    Recurses so deeply nested wrappers (``<div><div><p>X</p></div></div>``)
    flatten to ``[<p>X</p>]``.
    """

    out: list[ET._Element] = []
    for child in parent:
        if not isinstance(child.tag, str):
            continue
        local = strip_ns(child.tag)
        ns = ns_of(child.tag)
        if (ns is None or ns == "") and local == "div":
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


def _parse_table(element: ET._Element, ctx: ParserContext) -> BlockNode:
    # Walk thead/tbody/tfoot to collect rows.
    header: TableRow | None = None
    body_rows: list[TableRow] = []
    column_count = 0

    def make_row(tr: ET._Element) -> TableRow | None:
        nonlocal column_count
        cells: list[TableCell] = []
        for cell_el in tr:
            ctag = strip_ns(cell_el.tag)
            if ctag not in ("td", "th"):
                continue
            colspan = _parse_span_attr(cell_el.get("colspan"))
            rowspan = _parse_span_attr(cell_el.get("rowspan"))
            kind = CellType.HEADER if ctag == "th" else CellType.DATA
            try:
                inline = _parse_cell_content(cell_el, ctx)
            except _InlineEscalation:
                return None
            cells.append(
                TableCell(
                    kind=kind,
                    children=inline,
                    colspan=colspan,
                    rowspan=rowspan,
                )
            )
        column_count = max(column_count, sum(c.colspan for c in cells))
        return TableRow(cells=tuple(cells))

    for section in element:
        stag = strip_ns(section.tag)
        if stag == "thead":
            for tr in section:
                if strip_ns(tr.tag) == "tr":
                    row = make_row(tr)
                    if row is None:
                        return _as_opaque(element)
                    if header is None:
                        header = row
                    else:
                        body_rows.append(row)
        elif stag == "tbody":
            for tr in section:
                if strip_ns(tr.tag) == "tr":
                    row = make_row(tr)
                    if row is None:
                        return _as_opaque(element)
                    # If all cells in the first row are TH and there is no
                    # dedicated thead, promote it to the header.
                    if header is None and row.cells and all(c.kind == CellType.HEADER for c in row.cells):
                        header = row
                    else:
                        body_rows.append(row)
        elif stag == "tr":
            row = make_row(section)
            if row is None:
                return _as_opaque(element)
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
                        return _as_opaque(element)
                    body_rows.append(row)

    # GFM tables require a header row. If the source had no <thead>
    # and no all-th body row to promote, use the first body row as
    # the header so the round trip stays consistent. This is a small
    # semantic widening — visually the first row was always the
    # caption-style row anyway.
    if header is None and body_rows:
        header = body_rows[0]
        body_rows = body_rows[1:]

    alignments: tuple[CellAlign, ...] = tuple([CellAlign.NONE] * column_count)
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
            return result
    # Unknown or unmatched: preserve.
    return _as_opaque(element)


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
    if language in ("none", "text"):
        language = None
    return CodeBlock(content=body_text, language=language)


# ---------------------------------------------------------------------------
# Opaque helpers
# ---------------------------------------------------------------------------


def _as_opaque(element: ET._Element) -> OpaqueBlock:
    raw = _serialize_element(element)
    return OpaqueBlock(raw_xml=raw, opaque_id=opaque_id_for(raw))


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def parse_cfx(
    source: str,
    *,
    registry: MacroRegistry | None = None,
) -> tuple[Document, list[str]]:
    """Parse a Confluence storage fragment into a cfxmark AST.

    :param source: Confluence storage format XML fragment as a string.
    :param registry: macro registry, defaults to :data:`cfxmark.macros.default_registry`.
    :returns: ``(document, warnings)`` tuple.
    """

    ctx = ParserContext(registry=registry or default_registry)
    root = _parse_fragment_to_element(source)
    blocks = _parse_blocks(root, ctx)
    return Document(children=blocks), ctx.warnings


__all__ = ["parse_cfx"]
