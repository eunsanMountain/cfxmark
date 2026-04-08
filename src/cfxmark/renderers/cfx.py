"""cfxmark AST → Confluence Storage Format XHTML."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

import lxml.etree as ET

from cfxmark.ast import (
    BlockNode,
    BlockQuote,
    CellType,
    CodeBlock,
    DirectiveMacro,
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
    PassthroughComment,
    SoftBreak,
    Strikethrough,
    Strong,
    Table,
    TableCell,
    Text,
)
from cfxmark.exceptions import ConversionError
from cfxmark.macros import MacroRegistry, default_registry
from cfxmark.xml_ns import (
    AC_URI,
    NSMAP,
    RI_URI,
    ac_attr,
    ac_tag,
    build_structured_macro,
    ri_attr,
    ri_tag,
)

# ---------------------------------------------------------------------------
# Rendering context
# ---------------------------------------------------------------------------


@dataclass
class RenderContext:
    registry: MacroRegistry
    attachments: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Inline rendering
# ---------------------------------------------------------------------------


def _render_inline(
    nodes: tuple[InlineNode, ...],
    parent: ET._Element,
    ctx: RenderContext,
) -> None:
    """Attach inline nodes as children of ``parent``.

    Text content goes into ``parent.text`` or the preceding sibling's
    ``.tail``; element content is appended as children.
    """

    last_child: ET._Element | None = None

    def add_text(text: str) -> None:
        nonlocal last_child
        if not text:
            return
        if last_child is None:
            parent.text = (parent.text or "") + text
        else:
            last_child.tail = (last_child.tail or "") + text

    for node in nodes:
        if isinstance(node, Text):
            add_text(node.content)
        elif isinstance(node, SoftBreak):
            add_text("\n")
        elif isinstance(node, HardBreak):
            br = ET.SubElement(parent, "br")
            last_child = br
        elif isinstance(node, Strong):
            el = ET.SubElement(parent, "strong")
            _render_inline(node.children, el, ctx)
            last_child = el
        elif isinstance(node, Emphasis):
            el = ET.SubElement(parent, "em")
            _render_inline(node.children, el, ctx)
            last_child = el
        elif isinstance(node, Strikethrough):
            el = ET.SubElement(parent, "del")
            _render_inline(node.children, el, ctx)
            last_child = el
        elif isinstance(node, InlineCode):
            el = ET.SubElement(parent, "code")
            el.text = node.content
            last_child = el
        elif isinstance(node, Link):
            el = ET.SubElement(parent, "a", {"href": node.url})
            if node.title:
                el.set("title", node.title)
            _render_inline(node.children, el, ctx)
            last_child = el
        elif isinstance(node, Image):
            el = _render_image(node, ctx)
            parent.append(el)
            last_child = el
        elif isinstance(node, InlineOpaque):
            # Re-parse the stored XML and append it as a real element.
            el = _render_inline_opaque(node)
            parent.append(el)
            last_child = el
        else:
            raise ConversionError(f"unexpected inline node: {type(node).__name__}")


def _render_image(node: Image, ctx: RenderContext) -> ET._Element:
    attrs: dict[str, str] = {}
    if node.alt:
        attrs[ac_attr("alt")] = node.alt
    if node.title:
        attrs[ac_attr("title")] = node.title
    if node.width is not None:
        attrs[ac_attr("width")] = str(node.width)
    if node.height is not None:
        attrs[ac_attr("height")] = str(node.height)

    img = ET.Element(ac_tag("image"), attrs, nsmap=NSMAP)
    if _is_absolute_url(node.src):
        ri_el = ET.SubElement(
            img,
            ri_tag("url"),
            {ri_attr("value"): node.src},
        )
    else:
        # Confluence stores attachments in a flat per-page namespace,
        # so the ``ri:filename`` must be the basename only. We keep the
        # *original* path in ``ctx.attachments`` so the caller knows
        # where to read the bytes from on the local filesystem.
        basename = PurePosixPath(node.src).name or node.src
        ri_el = ET.SubElement(
            img,
            ri_tag("attachment"),
            {ri_attr("filename"): basename},
        )
        if node.src not in ctx.attachments:
            ctx.attachments.append(node.src)
    return img


_ABSOLUTE_URL_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


def _is_absolute_url(url: str) -> bool:
    return bool(_ABSOLUTE_URL_RE.match(url))


# ---------------------------------------------------------------------------
# Block rendering
# ---------------------------------------------------------------------------


def _render_block(
    block: BlockNode,
    ctx: RenderContext,
) -> ET._Element | None:
    if isinstance(block, Heading):
        tag = f"h{max(1, min(6, block.level))}"
        el = ET.Element(tag)
        _render_inline(block.children, el, ctx)
        return el
    if isinstance(block, Paragraph):
        el = ET.Element("p")
        _render_inline(block.children, el, ctx)
        return el
    if isinstance(block, CodeBlock):
        return _render_code_block(block)
    if isinstance(block, BlockQuote):
        el = ET.Element("blockquote")
        for child in block.children:
            sub = _render_block(child, ctx)
            if sub is not None:
                el.append(sub)
        return el
    if isinstance(block, List):
        return _render_list(block, ctx)
    if isinstance(block, ListItem):
        el = ET.Element("li")
        _render_list_item_body(block, el, ctx)
        return el
    if isinstance(block, HorizontalRule):
        return ET.Element("hr")
    if isinstance(block, Table):
        return _render_table(block, ctx)
    if isinstance(block, DirectiveMacro):
        return _render_directive(block, ctx)
    if isinstance(block, OpaqueBlock):
        return _render_opaque(block)
    if isinstance(block, PassthroughComment):
        # Caller-owned HTML comment on the Markdown side — never
        # serialized to Confluence storage format. The caller owns
        # this block and synchronizes it out-of-band; emitting it
        # here would send local metadata to Confluence, which is
        # exactly what the R1 contract forbids.
        return None
    raise ConversionError(f"unexpected block node: {type(block).__name__}")


def _render_code_block(block: CodeBlock) -> ET._Element:
    parameters: list[tuple[str, str]] = []
    if block.language:
        parameters.append(("language", block.language))
    macro = build_structured_macro("code", parameters)
    body = ET.SubElement(macro, ac_tag("plain-text-body"))
    # Strip the trailing newline mistletoe leaves on fenced content —
    # Confluence's native code macros never emit one, and keeping it
    # would make the canonical round-trip differ by a single byte per
    # code block.
    body.text = ET.CDATA(block.content.rstrip("\n"))
    return macro


def _render_list(block: List, ctx: RenderContext) -> ET._Element:
    tag = "ul" if block.list_type == ListType.BULLET else "ol"
    el = ET.Element(tag)
    if block.list_type == ListType.ORDERED and block.start != 1:
        el.set("start", str(block.start))
    for item in block.items:
        li = ET.SubElement(el, "li")
        _render_list_item_body(item, li, ctx)
    return el


def _render_list_item_body(
    item: ListItem,
    li: ET._Element,
    ctx: RenderContext,
) -> None:
    """Render a list item body using the tight or loose form.

    Tight form (``<li>text<ul>...</ul></li>``) is used when the item's
    children are a leading Paragraph followed by Lists only, or a single
    Paragraph. Anything more structural (multiple paragraphs, a code
    block, …) forces the loose form with each block wrapped in ``<p>``.
    """

    children = list(item.children)
    if not children:
        return

    if _is_tight_item(children):
        # First child Paragraph → inlined; the rest are Lists.
        first = children[0]
        if isinstance(first, Paragraph):
            _render_inline(first.children, li, ctx)
            rest = children[1:]
        else:
            rest = children
        for child in rest:
            sub = _render_block(child, ctx)
            if sub is not None:
                li.append(sub)
        return

    for child in children:
        if isinstance(child, Paragraph):
            p = ET.SubElement(li, "p")
            _render_inline(child.children, p, ctx)
        else:
            sub = _render_block(child, ctx)
            if sub is not None:
                li.append(sub)


def _is_tight_item(children: list[BlockNode]) -> bool:
    """Return True if this item should render in the compact (tight) form."""

    if not children:
        return True
    if len(children) == 1:
        # Any single block can be rendered inline into <li>.
        return True
    first = children[0]
    if not isinstance(first, Paragraph):
        return False
    return all(isinstance(child, List) for child in children[1:])


def _render_table(block: Table, ctx: RenderContext) -> ET._Element:
    def emit_cell(parent: ET._Element, tag: str, cell: TableCell) -> None:
        attribs: dict[str, str] = {}
        if cell.colspan > 1:
            attribs["colspan"] = str(cell.colspan)
        if cell.rowspan > 1:
            attribs["rowspan"] = str(cell.rowspan)
        td = ET.SubElement(parent, tag, attribs)
        _render_inline(cell.children, td, ctx)

    table = ET.Element("table")
    if block.header is not None:
        thead = ET.SubElement(table, "thead")
        tr = ET.SubElement(thead, "tr")
        for cell in block.header.cells:
            emit_cell(tr, "th", cell)
    tbody = ET.SubElement(table, "tbody")
    for row in block.body:
        tr = ET.SubElement(tbody, "tr")
        for cell in row.cells:
            tag = "th" if cell.kind == CellType.HEADER else "td"
            emit_cell(tr, tag, cell)
    return table


def _render_directive(
    block: DirectiveMacro,
    ctx: RenderContext,
) -> ET._Element:
    handler = ctx.registry.get_by_directive_name(block.name)
    if handler is None:
        raise ConversionError(
            f"no macro handler registered for directive {block.name!r}"
        )

    def body_renderer(blocks: tuple[BlockNode, ...]) -> list[ET._Element]:
        rendered: list[ET._Element] = []
        for b in blocks:
            sub = _render_block(b, ctx)
            if sub is not None:
                rendered.append(sub)
        return rendered

    rendered = handler.to_cfx(block, body_renderer)
    # If the caller supplied body content but the handler emitted no
    # ``rich-text-body`` child, the handler silently dropped it. This
    # typically happens when a user writes ``::: jira`` / ``::: toc``
    # with a body — these macros only carry parameters. Surface a
    # warning so the caller knows something was lost instead of
    # failing silently.
    if block.body:
        has_body = any(
            child.tag.rsplit("}", 1)[-1] == "rich-text-body" for child in rendered
        )
        if not has_body:
            ctx.warnings.append(
                f"directive '::: {block.name}' ignores body content; "
                "move values to header parameters (e.g. "
                f'``::: {block.name} key="..."``).'
            )
    return rendered


def _render_inline_opaque(node: InlineOpaque) -> ET._Element:
    """Re-parse the stored inline XML and return the resulting element.

    Inline opaque payloads use the same hardening as
    :func:`_render_opaque` — DOCTYPE/ENTITY declarations are refused
    and external resolution is disabled.
    """

    if "<!DOCTYPE" in node.raw_xml or "<!ENTITY" in node.raw_xml:
        raise ConversionError(
            f"inline opaque {node.opaque_id!r} contains a DOCTYPE/ENTITY declaration"
        )
    wrapped = (
        f'<root xmlns:ac="{AC_URI}" xmlns:ri="{RI_URI}">'
        f"{node.raw_xml}"
        f"</root>"
    )
    parser = ET.XMLParser(
        strip_cdata=False,
        remove_comments=False,
        no_network=True,
        load_dtd=False,
        huge_tree=False,
    )
    try:
        root = ET.fromstring(wrapped.encode("utf-8"), parser=parser)
    except ET.XMLSyntaxError as ex:
        raise ConversionError(
            f"inline opaque {node.opaque_id!r} contains malformed XML: {ex}"
        ) from ex
    children = list(root)
    if not children:
        raise ConversionError(
            f"inline opaque {node.opaque_id!r} is empty"
        )
    return children[0]  # type: ignore[no-any-return]


def _render_opaque(block: OpaqueBlock) -> ET._Element:
    """Re-parse the stored raw XML into an element.

    We wrap the fragment in a namespaced root element so that bare
    ``ac:`` / ``ri:`` prefixes inside the raw XML resolve correctly,
    then extract the first child. The parser is hardened the same way
    as :func:`cfxmark.parsers.cfx._parse_fragment_to_element` —
    DOCTYPE/ENTITY declarations are rejected and external resolution
    is disabled.
    """

    if "<!DOCTYPE" in block.raw_xml or "<!ENTITY" in block.raw_xml:
        raise ConversionError(
            f"opaque block {block.opaque_id!r} contains a DOCTYPE/ENTITY "
            f"declaration (rejected for XXE hardening)"
        )

    wrapped = (
        f'<root xmlns:ac="{AC_URI}" xmlns:ri="{RI_URI}">'
        f"{block.raw_xml}"
        f"</root>"
    )
    parser = ET.XMLParser(
        strip_cdata=False,
        remove_comments=False,
        no_network=True,
        load_dtd=False,
        huge_tree=False,
    )
    try:
        root = ET.fromstring(wrapped.encode("utf-8"), parser=parser)
    except ET.XMLSyntaxError as ex:
        raise ConversionError(
            f"opaque block {block.opaque_id!r} contains malformed XML: {ex}"
        ) from ex
    children = list(root)
    if not children:
        raise ConversionError(
            f"opaque block {block.opaque_id!r} is empty"
        )
    if len(children) > 1:
        # Multiple top-level elements — wrap them in a div so the
        # caller gets exactly one node back.
        div = ET.Element("div")
        for c in children:
            div.append(c)
        return div
    return children[0]  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Document rendering
# ---------------------------------------------------------------------------


def render_cfx(
    document: Document,
    *,
    registry: MacroRegistry | None = None,
) -> tuple[str, list[str], list[str]]:
    """Render a cfxmark AST to a Confluence storage format fragment.

    :param document: The document to render.
    :param registry: Macro registry to use. Defaults to
        :data:`cfxmark.macros.default_registry`.
    :returns: ``(xhtml, attachments, warnings)``. ``attachments`` is a
        deduplicated list of local file references that the caller
        should upload via the Confluence REST API before pushing the
        rendered XHTML. ``warnings`` carries human-readable messages
        about anything the renderer could not represent exactly.
    """

    ctx = RenderContext(registry=registry or default_registry)

    # Build a temporary root so we can serialize children individually.
    root = ET.Element(
        "root",
        nsmap=NSMAP,
    )
    for block in document.children:
        el = _render_block(block, ctx)
        if el is not None:
            root.append(el)

    fragments: list[str] = []
    for child in root:
        raw = ET.tostring(
            child,
            encoding="unicode",
            method="xml",
            with_tail=False,
        )
        raw = _strip_namespace_attrs(raw)
        fragments.append(raw)

    xhtml = "".join(fragments)
    return xhtml, ctx.attachments, ctx.warnings


_NS_ATTR_RE = re.compile(r'\s+xmlns(?::\w+)?="[^"]*"')


def _strip_namespace_attrs(xml: str) -> str:
    """Remove redundant ``xmlns``/``xmlns:ac``/``xmlns:ri`` declarations.

    Confluence's storage format does not require these inside the body
    because they are declared once at the page level. Removing them
    keeps output diff-clean.
    """

    return _NS_ATTR_RE.sub("", xml)


__all__ = ["render_cfx", "RenderContext"]
