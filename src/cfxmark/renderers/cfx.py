"""cfxmark AST → Confluence Storage Format XHTML."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

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
    PassthroughComment,
    SoftBreak,
    Strikethrough,
    Strong,
    Subscript,
    Superscript,
    Table,
    TableCell,
    Text,
    Underline,
    UnsupportedMarker,
)
from cfxmark.exceptions import ConversionError
from cfxmark.macros import MacroRegistry, default_registry
from cfxmark.storage_map import (
    OwnerToken,
    StorageExtraction,
    StorageOwnership,
    extract_storage,
    style_property_attribute_name,
)
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
    attachment_filename_map: dict[str, str] = field(default_factory=dict)
    source_owners: dict[int, str | tuple[str, str]] = field(default_factory=dict)
    presentation_owners: dict[int, tuple[str, str]] = field(default_factory=dict)
    storage_ownership: StorageOwnership = field(default_factory=StorageOwnership)

    def owner(self, node: object) -> OwnerToken | None:
        metadata = self.source_owners.get(id(node))
        if metadata is None:
            return None
        if isinstance(metadata, tuple):
            owner, stable_node_id = metadata
            return self.storage_ownership.register_owner(owner, "ast-node", stable_node_id)
        return self.storage_ownership.register_owner(metadata, "ast-node", metadata)

    def claim_element(self, element: ET._Element, node: object) -> ET._Element:
        self.storage_ownership.claim_element(element, self.owner(node))
        return element

    def claim_leaf(
        self,
        element: ET._Element,
        field_name: str,
        node: object,
        *,
        attribute_name: str | None = None,
    ) -> None:
        self.storage_ownership.claim_leaf(
            element,
            field_name,
            self.owner(node),
            attribute_name=attribute_name,
        )

    def claim_presentation_leaf(
        self,
        element: ET._Element,
        field_name: str,
        node: object,
        *,
        attribute_name: str | None = None,
    ) -> None:
        owner_metadata = self.presentation_owners.get(id(node))
        if owner_metadata is None:
            self.claim_leaf(element, field_name, node, attribute_name=attribute_name)
            return
        owner, occurrence_id = owner_metadata
        source_owner = self.owner(node)
        claim_owner = (
            source_owner
            if source_owner is not None and source_owner[0] == owner
            else self.storage_ownership.register_owner(owner, "presentation", occurrence_id)
        )
        self.storage_ownership.claim_leaf(
            element,
            field_name,
            claim_owner,
            attribute_name=attribute_name,
        )


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

    def add_text(text: str, node: InlineNode) -> None:
        nonlocal last_child
        if not text:
            return
        if last_child is None:
            parent.text = (parent.text or "") + text
            ctx.claim_leaf(parent, "text", node)
        else:
            last_child.tail = (last_child.tail or "") + text
            ctx.claim_leaf(last_child, "tail", node)

    for node in nodes:
        if isinstance(node, Text):
            add_text(node.content, node)
        elif isinstance(node, SoftBreak):
            add_text("\n", node)
        elif isinstance(node, HardBreak):
            br = ctx.claim_element(ET.SubElement(parent, "br"), node)
            last_child = br
        elif isinstance(node, Strong):
            el = ctx.claim_element(ET.SubElement(parent, "strong"), node)
            _render_inline(node.children, el, ctx)
            last_child = el
        elif isinstance(node, Emphasis):
            el = ctx.claim_element(ET.SubElement(parent, "em"), node)
            _render_inline(node.children, el, ctx)
            last_child = el
        elif isinstance(node, Strikethrough):
            el = ctx.claim_element(ET.SubElement(parent, "del"), node)
            _render_inline(node.children, el, ctx)
            last_child = el
        elif isinstance(node, Subscript):
            el = ctx.claim_element(ET.SubElement(parent, "sub"), node)
            _render_inline(node.children, el, ctx)
            last_child = el
        elif isinstance(node, Superscript):
            el = ctx.claim_element(ET.SubElement(parent, "sup"), node)
            _render_inline(node.children, el, ctx)
            last_child = el
        elif isinstance(node, Underline):
            el = ctx.claim_element(ET.SubElement(parent, "ins"), node)
            _render_inline(node.children, el, ctx)
            last_child = el
        elif isinstance(node, ColorSpan):
            el = ctx.claim_element(ET.SubElement(parent, "span"), node)
            css_property = "background-color" if node.background else "color"
            el.set(
                "style",
                f"{css_property}:{_cfx_color_value(node.color, node.background)}",
            )
            ctx.claim_leaf(el, "attribute", node, attribute_name="style")
            _render_inline(node.children, el, ctx)
            last_child = el
        elif isinstance(node, AlignedSpan):
            _render_inline(node.children, parent, ctx)
            last_child = parent[-1] if len(parent) else last_child
        elif isinstance(node, Citation):
            el = ctx.claim_element(ET.SubElement(parent, "cite"), node)
            _render_inline(node.children, el, ctx)
            last_child = el
        elif isinstance(node, InlineCode):
            el = ctx.claim_element(ET.SubElement(parent, "code"), node)
            el.text = node.content
            ctx.claim_leaf(el, "text", node)
            last_child = el
        elif isinstance(node, Link):
            el = ctx.claim_element(ET.SubElement(parent, "a", {"href": node.url}), node)
            ctx.claim_leaf(el, "attribute", node, attribute_name="href")
            if node.title:
                el.set("title", node.title)
                ctx.claim_leaf(el, "attribute", node, attribute_name="title")
            _render_inline(node.children, el, ctx)
            last_child = el
        elif isinstance(node, Image):
            el = _render_image(node, ctx)
            parent.append(el)
            last_child = el
        elif isinstance(node, InlineDirectiveMacro):
            el = _render_inline_directive(node, ctx)
            parent.append(el)
            last_child = el
        elif isinstance(node, UnsupportedMarker):
            raise ConversionError("Markdown contains unsupported Confluence content and cannot be rendered safely")
        elif isinstance(node, InlineOpaque):
            # Re-parse the stored XML and append it as a real element.
            el = _render_inline_opaque(node)
            parent.append(el)
            last_child = el
        else:
            raise ConversionError(f"unexpected inline node: {type(node).__name__}")


def _render_inline_directive(
    node: InlineDirectiveMacro,
    ctx: RenderContext,
) -> ET._Element:
    handler = ctx.registry.get_by_directive_name(node.name)
    if handler is None:
        raise ConversionError(f"no macro handler registered for inline directive {node.name!r}")
    directive = DirectiveMacro(name=node.name, parameters=node.parameters, body=None)
    return _restore_macro_identity(handler.to_cfx(directive, lambda _blocks: []), node, ctx)


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
    if node.thumbnail:
        attrs[ac_attr("thumbnail")] = "true"
    if node.align in {"center", "right"}:
        attrs[ac_attr("align")] = node.align

    img = ctx.claim_element(ET.Element(ac_tag("image"), attrs, nsmap=NSMAP), node)
    for attribute_name in attrs:
        ctx.claim_leaf(img, "attribute", node, attribute_name=attribute_name)
    if node.attachment_filename is None and _is_absolute_url(node.src):
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
        if node.attachment_filename is not None:
            local_path = node.src
            basename = node.attachment_filename
        else:
            url = urlsplit(node.src)
            local_path = unquote(url.path)
            basename = ctx.attachment_filename_map.get(local_path, PurePosixPath(local_path).name or local_path)
        ri_el = ET.SubElement(
            img,
            ri_tag("attachment"),
            {ri_attr("filename"): basename},
        )
        # A typed marker-bound data URI is presentation transport for an
        # already identified remote attachment, not a local file to upload.
        if not local_path.startswith("data:") and local_path not in ctx.attachments:
            ctx.attachments.append(local_path)
    ctx.storage_ownership.claim_element(ri_el, ctx.owner(node))
    for raw_attribute_name in ri_el.attrib:
        canonical_attribute_name = (
            raw_attribute_name.decode("utf-8") if isinstance(raw_attribute_name, bytes) else raw_attribute_name
        )
        ctx.storage_ownership.claim_leaf(
            ri_el,
            "attribute",
            ctx.owner(node),
            attribute_name=canonical_attribute_name,
        )
    return img


_FOREGROUND_COLORS = {
    "red": "var(--ds-text-accent-red, #ae2e24)",
    "amber": "var(--ds-text-accent-orange, #974f0c)",
    "green": "var(--ds-text-accent-green, #216e4e)",
    "blue": "var(--ds-text-accent-blue, #0055cc)",
    "purple": "var(--ds-text-accent-purple, #5e4db2)",
    "gray": "var(--ds-text-subtle, #626f86)",
    "neutral": "var(--ds-text, #172b4d)",
    "inverse": "var(--ds-text-inverse, #ffffff)",
}
_BACKGROUND_COLORS = {
    "red": "var(--ds-background-accent-red-subtlest, #ffeceb)",
    "amber": "var(--ds-background-accent-orange-subtlest, #fff3eb)",
    "green": "var(--ds-background-accent-green-subtlest, #dcfff1)",
    "blue": "var(--ds-background-accent-blue-subtlest, #e9f2ff)",
    "purple": "var(--ds-background-accent-purple-subtlest, #f3f0ff)",
    "gray": "var(--ds-background-neutral, #f1f2f4)",
    "neutral": "var(--ds-background-neutral, #f1f2f4)",
}


def _cfx_color_value(color: str, background: bool) -> str:
    palette = _BACKGROUND_COLORS if background else _FOREGROUND_COLORS
    return palette.get(color, color)


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
        el = ctx.claim_element(ET.Element(tag), block)
        _render_inline(block.children, el, ctx)
        return el
    if isinstance(block, Paragraph):
        el = ctx.claim_element(ET.Element("p"), block)
        if block.align in {"left", "center", "right"}:
            el.set("style", f"text-align: {block.align};")
            ctx.claim_leaf(el, "attribute", block, attribute_name="style")
        _render_inline(block.children, el, ctx)
        return el
    if isinstance(block, CodeBlock):
        return _render_code_block(block, ctx)
    if isinstance(block, BlockQuote):
        el = ctx.claim_element(ET.Element("blockquote"), block)
        for child in block.children:
            sub = _render_block(child, ctx)
            if sub is not None:
                el.append(sub)
        return el
    if isinstance(block, List):
        return _render_list(block, ctx)
    if isinstance(block, ListItem):
        el = ctx.claim_element(ET.Element("li"), block)
        _render_list_item_body(block, el, ctx)
        return el
    if isinstance(block, HorizontalRule):
        return ctx.claim_element(ET.Element("hr"), block)
    if isinstance(block, Table):
        return _render_table(block, ctx)
    if isinstance(block, DirectiveMacro):
        return _render_directive(block, ctx)
    if isinstance(block, UnsupportedMarker):
        raise ConversionError("Markdown contains unsupported Confluence content and cannot be rendered safely")
    if isinstance(block, OpaqueBlock):
        return ctx.claim_element(_render_opaque(block), block)
    if isinstance(block, PassthroughComment):
        # Caller-owned HTML comment on the Markdown side — never
        # serialized to Confluence storage format. The caller owns
        # this block and synchronizes it out-of-band; emitting it
        # here would send local metadata to Confluence, which is
        # exactly what the R1 contract forbids.
        return None
    raise ConversionError(f"unexpected block node: {type(block).__name__}")


def _render_code_block(block: CodeBlock, ctx: RenderContext) -> ET._Element:
    parameters: list[tuple[str, str]] = []
    if block.language:
        parameters.append(("language", block.language))
    macro = build_structured_macro("code", parameters)
    schema_attribute = ac_attr("schema-version")
    if block.macro_schema_version is None:
        if schema_attribute in macro.attrib:
            del macro.attrib[schema_attribute]
    else:
        macro.set(schema_attribute, block.macro_schema_version)
    if block.macro_id is not None:
        macro.set(ac_attr("macro-id"), block.macro_id)
    ctx.claim_element(macro, block)
    if block.macro_schema_version is not None:
        ctx.claim_leaf(macro, "attribute", block, attribute_name=schema_attribute)
    if block.macro_id is not None:
        ctx.claim_leaf(macro, "attribute", block, attribute_name=ac_attr("macro-id"))
    body = ET.SubElement(macro, ac_tag("plain-text-body"))
    ctx.storage_ownership.claim_element(body, ctx.owner(block))
    body.text = ET.CDATA(block.content)
    ctx.claim_leaf(body, "text", block)
    return macro


def _render_list(block: List, ctx: RenderContext) -> ET._Element:
    tag = "ul" if block.list_type == ListType.BULLET else "ol"
    el = ctx.claim_element(ET.Element(tag), block)
    if block.list_type == ListType.ORDERED and block.start != 1:
        el.set("start", str(block.start))
        ctx.claim_leaf(el, "attribute", block, attribute_name="start")
    for item in block.items:
        li = ctx.claim_element(ET.SubElement(el, "li"), item)
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
            p = ctx.claim_element(ET.SubElement(li, "p"), child)
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
    def render_cell_content(td: ET._Element, cell: TableCell) -> None:
        if not any(isinstance(node, AlignedSpan) for node in cell.children):
            _render_inline(cell.children, td, ctx)
            return

        pending: list[InlineNode] = []

        def flush(align: str | None = None) -> None:
            if not pending:
                return
            paragraph = ET.SubElement(td, "p")
            if align in {"left", "center", "right"}:
                paragraph.set("style", f"text-align: {align};")
            _render_inline(tuple(pending), paragraph, ctx)
            pending.clear()

        for node in cell.children:
            if isinstance(node, HardBreak):
                flush()
            elif isinstance(node, AlignedSpan):
                flush()
                pending.extend(node.children)
                flush(node.align)
            else:
                pending.append(node)
        flush()

    def emit_cell(parent: ET._Element, tag: str, cell: TableCell) -> None:
        attribs: dict[str, str] = {}
        if cell.colspan > 1:
            attribs["colspan"] = str(cell.colspan)
        if cell.rowspan > 1:
            attribs["rowspan"] = str(cell.rowspan)
        style_parts: list[str] = []
        if cell.source_style is None and cell.align in {CellAlign.LEFT, CellAlign.CENTER, CellAlign.RIGHT}:
            style_parts.append(f"text-align: {cell.align.value}")
        if cell.background is not None:
            if cell.background.render_strategy == "highlight-attribute" or (
                cell.background.render_strategy is None and cell.background.source_encoding == "data-highlight-colour"
            ):
                attribs["data-highlight-colour"] = cell.background.exact
            elif cell.source_style is None and (
                cell.background.render_strategy == "style-background-color"
                or (
                    cell.background.render_strategy is None
                    and cell.background.source_encoding == "css-background-color"
                )
            ):
                style_parts.append(f"background-color:{cell.background.exact}")
            elif cell.background.render_strategy is None and cell.background.source_encoding == "bgcolor":
                attribs["bgcolor"] = cell.background.exact
        if cell.source_style is not None:
            attribs["style"] = cell.source_style
        elif style_parts:
            attribs["style"] = ";".join(style_parts) + ";"
        td = ctx.claim_element(ET.SubElement(parent, tag, attribs), cell)
        for attribute_name in attribs:
            if attribute_name in {"data-highlight-colour", "bgcolor"}:
                ctx.claim_presentation_leaf(td, "attribute", cell, attribute_name=attribute_name)
            elif attribute_name == "style":
                if cell.align in {CellAlign.LEFT, CellAlign.CENTER, CellAlign.RIGHT}:
                    ctx.claim_leaf(
                        td,
                        "attribute",
                        cell,
                        attribute_name=style_property_attribute_name("text-align"),
                    )
                if cell.background is not None and (
                    cell.background.render_strategy == "style-background-color"
                    or (
                        cell.background.render_strategy is None
                        and cell.background.source_encoding == "css-background-color"
                    )
                ):
                    ctx.claim_presentation_leaf(
                        td,
                        "attribute",
                        cell,
                        attribute_name=style_property_attribute_name("background-color"),
                    )
            else:
                ctx.claim_leaf(td, "attribute", cell, attribute_name=attribute_name)
        render_cell_content(td, cell)

    table = ctx.claim_element(ET.Element("table"), block)
    if block.header is not None:
        thead = ET.SubElement(table, "thead")
        ctx.storage_ownership.claim_element(thead, ctx.owner(block))
        tr = ctx.claim_element(ET.SubElement(thead, "tr"), block.header)
        for cell in block.header.cells:
            emit_cell(tr, "th", cell)
    tbody = ET.SubElement(table, "tbody")
    ctx.storage_ownership.claim_element(tbody, ctx.owner(block))
    for row in block.body:
        tr = ctx.claim_element(ET.SubElement(tbody, "tr"), row)
        for cell in row.cells:
            tag = "th" if cell.kind == CellType.HEADER else "td"
            emit_cell(tr, tag, cell)
    return table


def _restore_macro_identity(
    macro: ET._Element,
    owner: DirectiveMacro | InlineDirectiveMacro,
    ctx: RenderContext,
) -> ET._Element:
    """Match the built macro's identity attributes to the remote source.

    ``build_structured_macro`` always emits ``ac:schema-version="1"`` (see
    :func:`cfxmark.xml_ns.build_structured_macro`); this restores the exact
    source value -- deleting the attribute when the source omitted it and
    reattaching ``ac:macro-id`` -- so regeneration matches byte for byte and the
    macro body and parameters remain editable.  Both attributes are then claimed
    as source-bound leaves, mirroring :func:`_render_code_block`.
    """

    schema_attribute = ac_attr("schema-version")
    if owner.macro_schema_version is None:
        if schema_attribute in macro.attrib:
            del macro.attrib[schema_attribute]
    else:
        macro.set(schema_attribute, owner.macro_schema_version)
    if owner.macro_id is not None:
        macro.set(ac_attr("macro-id"), owner.macro_id)
    claimed = ctx.claim_element(macro, owner)
    if owner.macro_schema_version is not None:
        ctx.claim_leaf(macro, "attribute", owner, attribute_name=schema_attribute)
    if owner.macro_id is not None:
        ctx.claim_leaf(macro, "attribute", owner, attribute_name=ac_attr("macro-id"))
    return claimed


def _render_directive(
    block: DirectiveMacro,
    ctx: RenderContext,
) -> ET._Element:
    handler = ctx.registry.get_by_directive_name(block.name)
    if handler is None:
        raise ConversionError(f"no macro handler registered for directive {block.name!r}")

    def body_renderer(blocks: tuple[BlockNode, ...]) -> list[ET._Element]:
        rendered: list[ET._Element] = []
        for b in blocks:
            sub = _render_block(b, ctx)
            if sub is not None:
                rendered.append(sub)
        return rendered

    rendered = _restore_macro_identity(handler.to_cfx(block, body_renderer), block, ctx)
    # If the caller supplied body content but the handler emitted no
    # ``rich-text-body`` child, the handler silently dropped it. This
    # typically happens when a user writes ``::: jira`` / ``::: toc``
    # with a body — these macros only carry parameters. Surface a
    # warning so the caller knows something was lost instead of
    # failing silently.
    if block.body:
        has_body = any(child.tag.rsplit("}", 1)[-1] == "rich-text-body" for child in rendered)
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
        raise ConversionError(f"inline opaque {node.opaque_id!r} contains a DOCTYPE/ENTITY declaration")
    wrapped = f'<root xmlns:ac="{AC_URI}" xmlns:ri="{RI_URI}">{node.raw_xml}</root>'
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
        raise ConversionError(f"inline opaque {node.opaque_id!r} contains malformed XML: {ex}") from ex
    children = list(root)
    if not children:
        raise ConversionError(f"inline opaque {node.opaque_id!r} is empty")
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
            f"opaque block {block.opaque_id!r} contains a DOCTYPE/ENTITY declaration (rejected for XXE hardening)"
        )

    wrapped = f'<root xmlns:ac="{AC_URI}" xmlns:ri="{RI_URI}">{block.raw_xml}</root>'
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
        raise ConversionError(f"opaque block {block.opaque_id!r} contains malformed XML: {ex}") from ex
    children = list(root)
    if not children:
        raise ConversionError(f"opaque block {block.opaque_id!r} is empty")
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
    attachment_filename_map: tuple[tuple[str, str], ...] = (),
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

    xhtml, attachments, warnings, _source_map = render_cfx_with_source_map(
        document,
        registry=registry,
        attachment_filename_map=attachment_filename_map,
        source_owners=None,
    )
    return xhtml, attachments, warnings


def render_cfx_with_source_map(
    document: Document,
    *,
    registry: MacroRegistry | None = None,
    attachment_filename_map: tuple[tuple[str, str], ...] = (),
    source_owners: Mapping[int, str | tuple[str, str]] | None,
    presentation_owners: dict[int, tuple[str, str]] | None = None,
) -> tuple[str, list[str], list[str], StorageExtraction]:
    """Render XHTML and attribute every emitted XML leaf to one AST owner."""

    mapping = dict(attachment_filename_map)
    if len(mapping) != len(attachment_filename_map):
        raise ConversionError("attachment filename map contains duplicate local references")
    ctx = RenderContext(
        registry=registry or default_registry,
        attachment_filename_map=mapping,
        source_owners=dict(source_owners or {}),
        presentation_owners=presentation_owners or {},
    )

    # Build a temporary root so we can serialize children individually.
    root = ET.Element(
        "root",
        nsmap=NSMAP,
    )
    for block in document.children:
        el = _render_block(block, ctx)
        if el is not None:
            root.append(el)
    ctx.storage_ownership.claim_leaf(root, "topology", ctx.owner(document))

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
    extraction = extract_storage(root, ownership=ctx.storage_ownership if source_owners is not None else None)
    return xhtml, ctx.attachments, ctx.warnings, extraction


_NS_ATTR_RE = re.compile(r'\s+xmlns(?::\w+)?="[^"]*"')


def _strip_namespace_attrs(xml: str) -> str:
    """Remove redundant ``xmlns``/``xmlns:ac``/``xmlns:ri`` declarations.

    Confluence's storage format does not require these inside the body
    because they are declared once at the page level. Removing them
    keeps output diff-clean.
    """

    return _NS_ATTR_RE.sub("", xml)


__all__ = ["render_cfx", "render_cfx_with_source_map", "RenderContext"]
