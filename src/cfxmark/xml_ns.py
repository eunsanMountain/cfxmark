"""Confluence XML namespace helpers.

Confluence storage format uses two custom namespaces:

* ``ac`` — ``http://atlassian.com/content`` — structured macros and
  various layout / panel elements.
* ``ri`` — ``http://atlassian.com/resource/identifier`` — attachment
  and URL references.

This module centralizes namespace constants, ``ElementMaker``
factories, attribute helpers, and a few small XML utilities (text
collection, decorative-span detection) shared across the parser,
renderer, and canonicalization modules.
"""

from __future__ import annotations

import re

import lxml.etree as ET
from lxml.builder import ElementMaker

AC_URI = "http://atlassian.com/content"
RI_URI = "http://atlassian.com/resource/identifier"

NSMAP = {"ac": AC_URI, "ri": RI_URI}

# Register once with lxml so serialization uses the canonical prefixes.
ET.register_namespace("ac", AC_URI)
ET.register_namespace("ri", RI_URI)


HTML = ElementMaker()
AC = ElementMaker(namespace=AC_URI, nsmap=NSMAP)
RI = ElementMaker(namespace=RI_URI, nsmap=NSMAP)


def ac_attr(name: str) -> str:
    """Return a ``{namespace}localname`` attribute key in the ``ac`` namespace."""

    return f"{{{AC_URI}}}{name}"


def ri_attr(name: str) -> str:
    """Return a ``{namespace}localname`` attribute key in the ``ri`` namespace."""

    return f"{{{RI_URI}}}{name}"


def ac_tag(name: str) -> str:
    """Return the fully-qualified element tag in the ``ac`` namespace."""

    return f"{{{AC_URI}}}{name}"


def ri_tag(name: str) -> str:
    """Return the fully-qualified element tag in the ``ri`` namespace."""

    return f"{{{RI_URI}}}{name}"


def strip_ns(tag: object) -> str:
    """Return the local part of an element tag, discarding the namespace."""

    if not isinstance(tag, str):
        return ""
    if tag.startswith("{"):
        return tag.split("}", 1)[1]
    return tag


def ns_of(tag: object) -> str | None:
    """Return the namespace URI of an element tag, or ``None`` if bare."""

    if not isinstance(tag, str):
        return None
    if tag.startswith("{"):
        return tag[1:].split("}", 1)[0]
    return None


def storage_qualified_name(name: str | bytes) -> str:
    """Return the canonical name used in storage source-map identities."""

    if isinstance(name, bytes):
        name = name.decode("utf-8")
    if name.startswith(f"{{{AC_URI}}}"):
        return f"ac:{name.split('}', 1)[1]}"
    if name.startswith(f"{{{RI_URI}}}"):
        return f"ri:{name.split('}', 1)[1]}"
    if name.startswith("{"):
        namespace, local = name[1:].split("}", 1)
        return f"{{{namespace}}}{local}"
    return name


def storage_node_name(element: ET._Element) -> str:
    """Return the canonical source-map component name for an XML node."""

    if isinstance(element.tag, str):
        return storage_qualified_name(element.tag)
    if element.tag is ET.Comment:
        return "#comment"
    if element.tag is ET.ProcessingInstruction:
        return "#processing-instruction"
    return "#node"


def storage_semantic_path(root: ET._Element, element: ET._Element) -> tuple[str, ...]:
    """Return the exact document-order path used by storage leaf extraction."""

    components: list[str] = []
    current = element
    while current is not root:
        parent = current.getparent()
        if parent is None:
            raise ValueError("storage element is not contained by the supplied fragment root")
        name = storage_node_name(current)
        ordinal = sum(1 for sibling in current.itersiblings(preceding=True) if storage_node_name(sibling) == name)
        components.append(f"{name}[{ordinal}]")
        current = parent
    if not components:
        raise ValueError("the synthetic storage fragment root has no semantic path")
    return tuple(reversed(components))


def build_structured_macro(
    name: str,
    parameters: list[tuple[str, str]],
    body: ET._Element | None = None,
) -> ET._Element:
    """Construct ``<ac:structured-macro>`` with parameters and an optional body.

    Parameters are emitted in the order given. ``body`` is inserted as
    an ``<ac:rich-text-body>`` child if not ``None``.

    Invariant: this always emits ``ac:schema-version="1"``. The exact remote
    value (including its absence) is restored *after the fact* by the renderers
    -- ``_render_code_block`` for code and ``_restore_macro_identity`` for
    directive macros -- from the captured node identity. Do not make this
    function conditionally omit the attribute: the code-block and directive edit
    round-trips depend on the deterministic default being present here and
    adjusted downstream.
    """

    macro = ET.Element(
        ac_tag("structured-macro"),
        {
            ac_attr("name"): name,
            ac_attr("schema-version"): "1",
        },
        nsmap=NSMAP,
    )
    for pname, pvalue in parameters:
        param = ET.SubElement(
            macro,
            ac_tag("parameter"),
            {ac_attr("name"): pname},
        )
        param.text = pvalue
    if body is not None:
        macro.append(body)
    return macro


# ---------------------------------------------------------------------------
# Shared text / decoration helpers
# ---------------------------------------------------------------------------


DEFAULT_COLOR_STYLE_RE = re.compile(r"^\s*color\s*:\s*var\(\s*--ds-text\s*(?:,\s*#[0-9a-fA-F]{3,8})?\s*\)\s*;?\s*$")


def collect_text_with_breaks(element: ET._Element) -> str:
    """Collect descendant text content, materializing ``<br/>`` as a newline.

    Used both by the CFX parser (when reading a ``<pre>`` block) and
    by the canonicalization layer (when normalizing ``<pre><code>``
    into the structured-macro form). Whitespace is preserved as it
    appears in the source.
    """

    parts: list[str] = []
    if element.text:
        parts.append(element.text)
    for child in element:
        if isinstance(child.tag, str) and strip_ns(child.tag) == "br":
            parts.append("\n")
        else:
            parts.append(collect_text_with_breaks(child))
        if child.tail:
            parts.append(child.tail)
    return "".join(parts)


__all__ = [
    "AC_URI",
    "RI_URI",
    "NSMAP",
    "HTML",
    "AC",
    "RI",
    "ac_attr",
    "ri_attr",
    "ac_tag",
    "ri_tag",
    "strip_ns",
    "ns_of",
    "storage_qualified_name",
    "storage_node_name",
    "storage_semantic_path",
    "build_structured_macro",
    "DEFAULT_COLOR_STYLE_RE",
    "collect_text_with_breaks",
]
