"""Canonicalization utilities for Markdown and Confluence Storage Format.

These functions let callers compare two documents modulo the
non-semantic differences that Confluence's editor and lxml's serializer
routinely introduce:

* ``ac:macro-id`` / ``ac:local-id`` / ``ri:version-at-save`` are
  re-generated on every save and must be stripped before comparing.
* The web editor inserts ``<span style="color:var(--ds-text,...)">``
  around arbitrary text runs. These are decorative only.
* HTML entities in text content become literal Unicode after a round
  trip, so pre-normalizing them lets the comparison focus on structure.
* Whitespace inside inline elements is collapsed the same way
  Confluence normalizes it on save.
"""

from __future__ import annotations

import re

import lxml.etree as ET

from cfxmark.parsers.cfx import _parse_fragment_to_element
from cfxmark.xml_ns import (
    AC_URI,
    DEFAULT_COLOR_STYLE_RE,
    ac_attr,
    collect_text_with_breaks,
    ns_of,
    ri_attr,
    strip_ns,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


VOLATILE_ATTRS = frozenset(
    {
        ac_attr("macro-id"),
        ac_attr("local-id"),
        ac_attr("schema-version"),
        ac_attr("thumbnail"),
        ac_attr("border"),
        ac_attr("align"),
        ac_attr("class"),
        ri_attr("version-at-save"),
    }
)


# CSS properties that are *always* cosmetic (no semantic role anywhere
# we care about in Confluence storage format).
_COSMETIC_STYLE_PROPS_GLOBAL = frozenset(
    {
        "text-align",
        "text-align-last",
        "list-style-type",
        "list-style-position",
        "list-style-image",
        "vertical-align",
        "padding",
        "padding-top",
        "padding-right",
        "padding-bottom",
        "padding-left",
        "margin",
        "margin-top",
        "margin-right",
        "margin-bottom",
        "margin-left",
        "font-weight",
        "font-style",
        "font-size",
        "font-family",
        "line-height",
        "letter-spacing",
        "white-space",
        "border",
        "border-top",
        "border-right",
        "border-bottom",
        "border-left",
        "border-color",
        "border-style",
        "border-width",
    }
)

# CSS properties that the Confluence editor sprays onto *table*
# elements for column-width hints. Outside the table context these
# may be semantically meaningful (e.g. an author-set image height),
# so we only strip them when the host tag is part of the table family.
_COSMETIC_STYLE_PROPS_TABLE_ONLY = frozenset(
    {
        "width",
        "min-width",
        "max-width",
        "height",
        "background-color",
    }
)

_TABLE_FAMILY_TAGS = frozenset(
    {"table", "thead", "tbody", "tfoot", "tr", "th", "td", "col", "colgroup"}
)


def _is_cosmetic_style(style: str, tag: str) -> bool:
    """True if every CSS property in ``style`` is purely cosmetic for
    an element with the given local tag name.

    See :data:`_COSMETIC_STYLE_PROPS_GLOBAL` and
    :data:`_COSMETIC_STYLE_PROPS_TABLE_ONLY` for the exact policy.
    """

    parts = [p.strip() for p in style.split(";") if p.strip()]
    if not parts:
        return True
    in_table = tag in _TABLE_FAMILY_TAGS
    for part in parts:
        if ":" not in part:
            return False
        prop, _, _value = part.partition(":")
        prop = prop.strip().lower()
        if prop == "color":
            # Only the Confluence default text colour counts as cosmetic.
            if not DEFAULT_COLOR_STYLE_RE.match(part):
                return False
            continue
        if prop in _COSMETIC_STYLE_PROPS_GLOBAL:
            continue
        if prop in _COSMETIC_STYLE_PROPS_TABLE_ONLY and in_table:
            continue
        return False
    return True


# ---------------------------------------------------------------------------
# CFX canonicalization
# ---------------------------------------------------------------------------


def canonicalize_cfx(source: str) -> str:
    """Return a deterministic, comparison-friendly form of a CFX fragment.

    The returned string:

    * Has all volatile Confluence attributes stripped.
    * Has decorative ``<span style="color:...">`` wrappers unwrapped.
    * Has HTML entities decoded to their Unicode characters.
    * Has each element serialized with sorted attributes.
    * Has no namespace declarations (they are implicit).
    * Has whitespace between block-level elements removed.
    * Has cosmetic code-macro parameters (``linenumbers``,
      ``firstline``, ``collapse``, ``theme``, ``title``) removed —
      these are rendering hints that do not affect content.
    """

    root = _parse_fragment_to_element(source)
    _strip_volatile_and_cosmetic_attrs(root)
    _strip_layout_only_elements(root)
    _unwrap_decorative_spans(root)
    _normalize_inline_whitespace(root)
    _normalize_pre_code(root)
    _strip_cosmetic_code_params(root)
    _trim_code_body_trailing_whitespace(root)
    _normalize_code_span_pipes(root)
    _split_headings_at_break(root)
    _promote_header_row_to_thead(root)
    _unwrap_divs_in_cells(root)
    _flatten_paragraphs_in_table_cells(root)
    _flatten_singleton_paragraph_in_li(root)
    _strip_trailing_breaks(root)
    _trim_inline_endpoints(root)
    _drop_empty_paragraphs(root)
    _drop_empty_pre(root)
    _trim_empty_table_cells(root)
    _strip_empty_breaks(root)
    _strip_block_whitespace(root)
    return _serialize_children(root)


def _strip_volatile_and_cosmetic_attrs(root: ET._Element) -> None:
    """Single-pass attribute scrubber.

    Walks the tree once and removes:

    * Volatile Confluence attributes (``ac:macro-id``, ``ac:local-id``,
      ``ac:schema-version``, ``ac:thumbnail``, ``ri:version-at-save``).
    * Confluence editor data attributes (``data-uuid``,
      ``data-highlight-colour``, …).
    * Cosmetic CSS rules (``style="color:var(--ds-text)"``,
      ``style="text-align: left"``, ``style="width: 963px"``, …).
    * Cosmetic ``class`` values (``wrapped``, ``fixed-width``,
      ``relative-table``, ``highlight-…``, ``confluence…``).
    """

    cosmetic_class_prefixes = (
        "highlight-",
        "confluence",
        "has-list-",
        "internal-link",
        "external-link",
        "is-unresolved",
    )
    cosmetic_class_values = {
        "wrapped",
        "fixed-width",
        "relative-table",
        "auto-cursor-target",
        "ui-provider",
        "code-line",
    }
    # ``<a>`` elements have no Markdown-preservable classes — strip
    # them all so the canonical form ignores presentational link
    # decorations.
    tags_with_all_class_cosmetic = frozenset({"a"})
    cosmetic_data_attrs = {"data-highlight-colour", "data-highlight-color"}
    # ``title`` is a Confluence editor tooltip hint and ``scope`` is
    # a presentational hint that markdown cannot preserve.
    cosmetic_table_attrs = {"title", "scope"}

    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        local_tag = strip_ns(el.tag)
        for attr in list(el.attrib):
            if attr in VOLATILE_ATTRS or attr in _VOLATILE_HTML_ATTRS:
                del el.attrib[attr]
                continue
            if attr in cosmetic_data_attrs:
                del el.attrib[attr]
                continue
            if attr in cosmetic_table_attrs and local_tag in _TABLE_FAMILY_TAGS:
                del el.attrib[attr]
        # Empty editor-noise attributes.
        for empty_attr in ("class", "title"):
            if el.get(empty_attr) == "":
                del el.attrib[empty_attr]
        style = el.get("style")
        if style and _is_cosmetic_style(style, strip_ns(el.tag)):
            del el.attrib["style"]
        cls = el.get("class")
        if cls:
            if local_tag in tags_with_all_class_cosmetic:
                del el.attrib["class"]
            else:
                kept = [
                    c
                    for c in cls.split()
                    if c not in cosmetic_class_values
                    and not any(c.startswith(p) for p in cosmetic_class_prefixes)
                ]
                if kept:
                    el.set("class", " ".join(kept))
                else:
                    del el.attrib["class"]


def _strip_layout_only_elements(root: ET._Element) -> None:
    """Drop ``<colgroup>`` / ``<col>`` and other layout-only elements.

    These appear in tables saved from the Confluence editor to encode
    column widths but carry no semantic content.
    """

    layout_tags = {"colgroup", "col"}
    for el in list(root.iter()):
        if not isinstance(el.tag, str):
            continue
        if strip_ns(el.tag) not in layout_tags:
            continue
        parent = el.getparent()
        if parent is None:
            continue
        parent.remove(el)


def _normalize_pre_code(root: ET._Element) -> None:
    """Convert ``<pre><code>...text...</code></pre>`` to a code macro.

    Confluence stores code blocks two ways: the HTML ``<pre><code>``
    form and the structured macro ``<ac:structured-macro name="code">``.
    Our renderer always emits the macro form, so canonicalization
    converts the HTML form to match.
    """

    for pre in list(root.iter()):
        if not isinstance(pre.tag, str) or strip_ns(pre.tag) != "pre":
            continue
        children = list(pre)
        # Find the inner element that holds the text. Either <pre><code>...
        # or just <pre>....
        inner = pre
        if len(children) == 1 and isinstance(children[0].tag, str) and strip_ns(children[0].tag) == "code":
            inner = children[0]
        # Refuse to canonicalize if there are nested elements other
        # than <br/>.
        skip = False
        for desc in inner.iterdescendants():
            if not isinstance(desc.tag, str):
                skip = True
                break
            if strip_ns(desc.tag) == "br":
                continue
            skip = True
            break
        if skip:
            continue
        text = collect_text_with_breaks(inner).rstrip()
        if not text:
            continue
        language = None
        cls = inner.get("class") or ""
        m = re.match(r"^language-(.*)$", cls)
        if m:
            language = m.group(1)
        # Build replacement <ac:structured-macro>.
        macro = ET.Element(
            "{" + AC_URI + "}structured-macro",
            {ac_attr("name"): "code"},
        )
        if language:
            param = ET.SubElement(
                macro,
                "{" + AC_URI + "}parameter",
                {ac_attr("name"): "language"},
            )
            param.text = language
        body = ET.SubElement(macro, "{" + AC_URI + "}plain-text-body")
        body.text = ET.CDATA(text)
        macro.tail = pre.tail
        parent = pre.getparent()
        if parent is None:
            continue
        parent.replace(pre, macro)


_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})


def _promote_header_row_to_thead(root: ET._Element) -> None:
    """Move the leading row of a header-less tbody into a new thead.

    Confluence often emits tables as ``<table><tbody>...</tbody>
    </table>`` without an explicit ``<thead>``. cfxmark's renderer
    always uses an explicit ``<thead>`` for the first row (GFM tables
    require a header row), so canonicalize the source to match by
    promoting the first body row — converting any ``<td>`` cells in
    that row to ``<th>`` so the comparison agrees with the rendered
    output.
    """

    for table in list(root.iter()):
        if not isinstance(table.tag, str) or strip_ns(table.tag) != "table":
            continue
        # Already has a thead? Then don't second-guess.
        existing_thead = next(
            (
                child
                for child in table
                if isinstance(child.tag, str) and strip_ns(child.tag) == "thead"
            ),
            None,
        )
        if existing_thead is not None:
            continue
        tbody = next(
            (
                child
                for child in table
                if isinstance(child.tag, str) and strip_ns(child.tag) == "tbody"
            ),
            None,
        )
        if tbody is None:
            continue
        rows = [
            c
            for c in tbody
            if isinstance(c.tag, str) and strip_ns(c.tag) == "tr"
        ]
        if not rows:
            continue
        first = rows[0]
        # Convert any <td> cells in the promoted row to <th> so the
        # canonical form matches what the renderer will emit.
        for cell in first:
            if isinstance(cell.tag, str) and strip_ns(cell.tag) == "td":
                cell.tag = "th"
        thead = ET.Element("thead")
        tbody.remove(first)
        thead.append(first)
        table_index = list(table).index(tbody)
        table.insert(table_index, thead)


def _unwrap_divs_in_cells(root: ET._Element) -> None:
    """Unwrap ``<div>`` elements that live inside table cells.

    Confluence's editor wraps cell content in
    ``<div class="content-wrapper">`` (or generic ``<div>``) for
    layout purposes. The wrapper has no semantic role and Markdown
    table cells cannot represent it, so canonicalization unwraps the
    div and promotes its children to direct cell children.
    """

    for cell in list(root.iter()):
        if not isinstance(cell.tag, str):
            continue
        if strip_ns(cell.tag) not in ("td", "th"):
            continue
        # Iteratively unwrap divs until none remain at the top of the
        # cell. (Nested divs flatten as we keep finding new ones.)
        while True:
            divs = [
                c
                for c in cell
                if isinstance(c.tag, str) and strip_ns(c.tag) == "div"
            ]
            if not divs:
                break
            for div in divs:
                _splice_children(cell, div)


def _flatten_paragraphs_in_table_cells(root: ET._Element) -> None:
    """Collapse ``<td><p>X</p><p>Y</p></td>`` to ``<td>X<br/>Y</td>``.

    GFM tables don't model multi-paragraph cells; cfxmark serialises
    them with explicit ``<br/>`` separators. The original CFX from
    Confluence almost always uses ``<p>`` siblings instead, so we
    rewrite the original to the same shape during canonicalization.
    """

    for cell in list(root.iter()):
        if not isinstance(cell.tag, str):
            continue
        if strip_ns(cell.tag) not in ("td", "th"):
            continue
        children = list(cell)
        if not any(
            isinstance(c.tag, str) and strip_ns(c.tag) == "p" for c in children
        ):
            continue

        new_text = cell.text if cell.text and cell.text.strip() else None
        new_children, new_text = _flatten_cell_paragraph_children(
            children, new_text
        )

        for c in list(cell):
            cell.remove(c)
        cell.text = new_text
        for c in new_children:
            cell.append(c)


def _append_inline_text(
    new_children: list[ET._Element],
    new_text: str | None,
    s: str,
) -> str | None:
    if not s:
        return new_text
    if not new_children:
        return (new_text or "") + s
    tail = new_children[-1]
    tail.tail = (tail.tail or "") + s
    return new_text


def _flatten_cell_paragraph_children(
    children: list[ET._Element],
    new_text: str | None,
) -> tuple[list[ET._Element], str | None]:
    """Flatten ``<p>`` siblings inside a table cell into inline content
    joined by ``<br/>`` separators."""

    new_children: list[ET._Element] = []
    first_p = True
    for child in children:
        if not isinstance(child.tag, str):
            continue
        if strip_ns(child.tag) == "p":
            if not first_p:
                new_children.append(ET.Element("br"))
            first_p = False
            if child.text:
                new_text = _append_inline_text(new_children, new_text, child.text)
            # Move the paragraph's children, preserving their tails.
            for grand in list(child):
                new_children.append(grand)
            if child.tail and child.tail.strip():
                new_children.append(ET.Element("br"))
                new_text = _append_inline_text(new_children, new_text, child.tail)
        else:
            new_children.append(child)
    return new_children, new_text


def _split_headings_at_break(root: ET._Element) -> None:
    """Split a ``<hN>`` element at the first ``<br/>`` into a heading
    plus a paragraph for the trailing content.

    Markdown ATX headings cannot contain hard breaks: a ``<h3>X<br/>Y
    </h3>`` round-trips as ``### X`` plus a separate paragraph for
    ``Y``. Canonicalize the original to the same shape so the
    comparison passes.
    """

    for heading in list(root.iter()):
        if not isinstance(heading.tag, str):
            continue
        if strip_ns(heading.tag) not in _HEADING_TAGS:
            continue
        children = list(heading)
        break_index = next(
            (
                i
                for i, c in enumerate(children)
                if isinstance(c.tag, str) and strip_ns(c.tag) == "br"
            ),
            -1,
        )
        if break_index < 0:
            continue
        parent = heading.getparent()
        if parent is None:
            continue

        br = children[break_index]
        trailing = children[break_index + 1 :]

        # Build the trailing <p>: br.tail + remaining children + their tails.
        para = ET.Element("p")
        if br.tail:
            para.text = br.tail
        for child in trailing:
            heading.remove(child)
            para.append(child)

        # Detach the <br/> and the moved children from the heading.
        heading.remove(br)

        # Insert the paragraph immediately after the heading, taking the
        # heading's original tail with it.
        para.tail = heading.tail
        heading.tail = None
        heading_index = list(parent).index(heading)
        parent.insert(heading_index + 1, para)


def _drop_empty_paragraphs(root: ET._Element) -> None:
    """Remove ``<p></p>`` and ``<p><br/></p>`` placeholders.

    Confluence's editor leaves empty paragraphs behind as vertical
    spacing. They have no semantic content and break round trips
    because the Markdown side has no equivalent.
    """

    for el in list(root.iter()):
        if not isinstance(el.tag, str):
            continue
        if strip_ns(el.tag) != "p":
            continue
        if el.text and el.text.strip():
            continue
        non_empty = False
        for child in el:
            if not isinstance(child.tag, str):
                non_empty = True
                break
            if strip_ns(child.tag) == "br":
                if child.tail and child.tail.strip():
                    non_empty = True
                    break
                continue
            non_empty = True
            break
        if non_empty:
            continue
        parent = el.getparent()
        if parent is None:
            continue
        parent.remove(el)


def _drop_empty_pre(root: ET._Element) -> None:
    """Remove ``<pre></pre>`` and ``<pre><code></code></pre>`` placeholders."""

    for el in list(root.iter()):
        if not isinstance(el.tag, str):
            continue
        if strip_ns(el.tag) != "pre":
            continue
        text_content = "".join(el.itertext()).strip()
        if text_content:
            continue
        parent = el.getparent()
        if parent is None:
            continue
        parent.remove(el)


def _trim_empty_table_cells(root: ET._Element) -> None:
    """Drop trailing empty ``<td>``/``<th>`` cells from each row.

    Confluence tables sometimes have ragged rows where the editor
    leaves a trailing empty cell behind, while a clean re-render does
    not produce it. Trimming the rags makes the canonical comparison
    independent of that quirk.
    """

    for tr in list(root.iter()):
        if not isinstance(tr.tag, str) or strip_ns(tr.tag) != "tr":
            continue
        cells = [c for c in tr if isinstance(c.tag, str) and strip_ns(c.tag) in ("td", "th")]
        while cells and _is_cell_empty(cells[-1]):
            tr.remove(cells[-1])
            cells.pop()


def _is_cell_empty(cell: ET._Element) -> bool:
    if cell.text and cell.text.strip():
        return False
    for child in cell:
        if not isinstance(child.tag, str):
            return False
        if strip_ns(child.tag) == "br":
            if child.tail and child.tail.strip():
                return False
            continue
        return False
    return True


def _strip_trailing_breaks(root: ET._Element) -> None:
    """Drop trailing ``<br/>`` elements from cells, headings, and ``<p>``.

    Confluence's editor leaves dangling line breaks at the bottom of
    multi-line cells (``<td>...text<br/><br/></td>``). They are
    invisible at render time and would round-trip as nothing on the
    Markdown side, so canonicalize them away on both sides.
    """

    for el in list(root.iter()):
        if not isinstance(el.tag, str):
            continue
        if strip_ns(el.tag) not in _TRIM_TAGS:
            continue
        # Repeatedly remove trailing <br/> children with no significant tail.
        while True:
            children = list(el)
            if not children:
                break
            last = children[-1]
            if not isinstance(last.tag, str) or strip_ns(last.tag) != "br":
                break
            if last.tail and last.tail.strip():
                break
            el.remove(last)


def _strip_empty_breaks(root: ET._Element) -> None:
    """Remove ``<br/>`` elements that don't separate any content.

    A lone ``<br/>`` inside an otherwise empty cell or paragraph is
    visual noise from the Confluence editor. Removing them stabilises
    the round trip.
    """

    for el in list(root.iter()):
        if not isinstance(el.tag, str):
            continue
        for child in list(el):
            if not isinstance(child.tag, str):
                continue
            if strip_ns(child.tag) != "br":
                continue
            # Drop a <br/> that has no surrounding text inside the
            # parent and is the only child.
            siblings = list(el)
            if len(siblings) != 1:
                continue
            if el.text and el.text.strip():
                continue
            if child.tail and child.tail.strip():
                continue
            el.remove(child)


_COSMETIC_CODE_PARAMS = frozenset(
    {"linenumbers", "firstline", "collapse", "theme", "title"}
)


def _normalize_code_span_pipes(root: ET._Element) -> None:
    """Collapse ``\\|`` to ``|`` inside inline ``<code>`` elements.

    GFM table cells consume backslash-escapes for the pipe character
    even when the pipe lives inside a code span — there is no way to
    represent ``\\|`` literally in a markdown code span inside a
    table. Both forms therefore round-trip to the same plain ``|`` so
    we collapse the original to match.
    """

    for el in list(root.iter()):
        if not isinstance(el.tag, str):
            continue
        if strip_ns(el.tag) != "code":
            continue
        if el.text and "\\|" in el.text:
            el.text = el.text.replace("\\|", "|")


def _trim_code_body_trailing_whitespace(root: ET._Element) -> None:
    """Strip trailing whitespace inside ``<ac:plain-text-body>``.

    The renderer always emits code without a trailing newline; the
    Confluence editor sometimes leaves one in. Trim both sides for
    canonical comparison.
    """

    for el in list(root.iter()):
        if not isinstance(el.tag, str):
            continue
        if not el.tag.endswith("}plain-text-body"):
            continue
        if el.text:
            el.text = el.text.rstrip("\n")


def _strip_cosmetic_code_params(root: ET._Element) -> None:
    """Drop code-macro parameters that only tweak display."""

    # Collect first because mutating the tree mid-iteration breaks
    # lxml's iter() walker.
    code_macros: list[ET._Element] = []
    for el in list(root.iter()):
        if not isinstance(el.tag, str):
            continue
        if strip_ns(el.tag) != "structured-macro":
            continue
        if ns_of(el.tag) != AC_URI:
            continue
        if el.get(ac_attr("name")) != "code":
            continue
        code_macros.append(el)

    for macro in code_macros:
        for child in list(macro):
            if not isinstance(child.tag, str):
                continue
            if strip_ns(child.tag) != "parameter":
                continue
            if child.get(ac_attr("name")) in _COSMETIC_CODE_PARAMS:
                macro.remove(child)


_BLOCK_TAGS = frozenset(
    {
        "blockquote",
        "div",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "hr",
        "li",
        "ol",
        "p",
        "pre",
        "table",
        "tbody",
        "td",
        "th",
        "thead",
        "tfoot",
        "tr",
        "ul",
    }
)


def _is_block(element: ET._Element) -> bool:
    if not isinstance(element.tag, str):
        return False
    tag = strip_ns(element.tag)
    ns = ns_of(element.tag)
    if ns in (None, "") and tag in _BLOCK_TAGS:
        return True
    if ns == AC_URI and tag in ("structured-macro", "layout", "layout-section", "layout-cell"):
        return True
    return False


def _strip_block_whitespace(element: ET._Element) -> None:
    """Remove pure-whitespace text between block-level children.

    Also trims trailing whitespace from text content that immediately
    precedes a block-level child, since that whitespace gets eaten on
    a Markdown round-trip and would otherwise show up as a noisy diff.
    """

    for parent in list(element.iter()):
        if not isinstance(parent.tag, str):
            continue
        if not _is_block_container(parent):
            # Even non-container elements can have pure-whitespace
            # text we should drop (e.g. ``<tr></tr>``).
            if parent.text and not parent.text.strip() and not list(parent):
                parent.text = None
            continue
        children = list(parent)
        if not children:
            if parent.text and not parent.text.strip():
                parent.text = None
            continue
        # Leading text before the first block child.
        if _is_block(children[0]):
            if parent.text and not parent.text.strip():
                parent.text = None
            elif parent.text:
                parent.text = parent.text.rstrip()
                if not parent.text:
                    parent.text = None
        for i, child in enumerate(children):
            if not child.tail:
                continue
            is_last = i == len(children) - 1
            next_is_block = is_last or _is_block(children[i + 1])
            if not next_is_block:
                continue
            if not child.tail.strip():
                child.tail = None
            else:
                child.tail = child.tail.rstrip()
                if not child.tail:
                    child.tail = None


def _is_block_container(element: ET._Element) -> bool:
    """True if the element is allowed to hold block-level children."""

    if not isinstance(element.tag, str):
        return False
    tag = strip_ns(element.tag)
    ns = ns_of(element.tag)
    if ns in (None, "") and tag in (
        "blockquote",
        "div",
        "li",
        "ol",
        "table",
        "tbody",
        "tfoot",
        "thead",
        "tr",
        "ul",
        "root",
    ):
        return True
    if ns == AC_URI and tag in ("rich-text-body", "layout", "layout-section", "layout-cell"):
        return True
    return False


_VOLATILE_HTML_ATTRS = frozenset({"data-uuid", "data-macro-id"})


def _flatten_singleton_paragraph_in_li(root: ET._Element) -> None:
    """Treat ``<li><p>X</p></li>`` and ``<li>X</li>`` as equivalent.

    Confluence's editor flips between the two forms unpredictably.
    Both versions canonicalize to the wrapped form ``<li><p>X</p></li>``
    so the round-trip can ignore the difference.

    Actually we go the other way — strip the wrapper — because that
    matches the form our renderer emits for tight list items.
    """

    for li in list(root.iter()):
        if not isinstance(li.tag, str):
            continue
        if strip_ns(li.tag) != "li":
            continue
        # Only flatten when the entire li body is a single <p> with
        # no leading or trailing text on the li itself.
        if li.text and li.text.strip():
            continue
        children = list(li)
        if len(children) != 1:
            continue
        p = children[0]
        if not isinstance(p.tag, str) or strip_ns(p.tag) != "p":
            continue
        if p.tail and p.tail.strip():
            continue
        # Promote <p>'s contents up to <li>.
        li.text = p.text
        li.remove(p)
        for inner_child in p:
            li.append(inner_child)
    # Also flatten when the li starts with a <p> followed by other
    # block elements (e.g. nested lists). The leading <p> can be
    # safely flattened in that case as well.
    for li in list(root.iter()):
        if not isinstance(li.tag, str):
            continue
        if strip_ns(li.tag) != "li":
            continue
        children = list(li)
        if not children:
            continue
        first = children[0]
        if not isinstance(first.tag, str) or strip_ns(first.tag) != "p":
            continue
        if first.tail and first.tail.strip():
            continue
        if li.text and li.text.strip():
            continue
        # Only flatten when remaining children are all block-level.
        rest = children[1:]
        if not all(isinstance(c.tag, str) and _is_block(c) for c in rest):
            continue
        # Carry the <p>'s text into li.text and prepend its children.
        li.text = first.text
        first_children = list(first)
        li.remove(first)
        for j, ic in enumerate(first_children):
            li.insert(j, ic)


def _unwrap_decorative_spans(element: ET._Element) -> None:
    """Unwrap ``<span>`` elements whose only role is styling.

    Collected in a single bottom-up pass and removed without re-walking
    the tree, so the cost is O(n) in the number of elements rather than
    the previous O(n²) "rewalk after every removal" loop.
    """

    decorative: list[ET._Element] = []
    for el in element.iter():
        if not isinstance(el.tag, str):
            continue
        if strip_ns(el.tag) != "span":
            continue
        if ns_of(el.tag) not in (None, ""):
            continue
        if _is_decorative(el):
            decorative.append(el)

    # Process leaves first so a span nested inside another decorative
    # span is unwrapped before its parent is touched.
    for span in reversed(decorative):
        parent = span.getparent()
        if parent is None:
            continue
        _splice_children(parent, span)


def _is_decorative(span: ET._Element) -> bool:
    """A ``<span>`` is decorative for canonicalization purposes if its
    only attributes are presentational hints (``style`` and ``class``).

    Markdown has no way to represent inline span styling — colours,
    classes, custom font hints — so any presentational attribute on a
    ``<span>`` is lost on a round trip through ``to_md``. Treating
    these spans as decorative makes the canonical form ignore them.
    """

    attrs = dict(span.attrib)
    attrs.pop("style", None)
    attrs.pop("class", None)
    return not attrs


def _splice_children(parent: ET._Element, span: ET._Element) -> None:
    """Replace ``span`` within ``parent`` with its children + text/tail."""

    idx = list(parent).index(span)
    preceding_tail: str | None
    if idx == 0:
        preceding_tail = parent.text
    else:
        preceding_tail = parent[idx - 1].tail

    new_text = (preceding_tail or "") + (span.text or "")
    children = list(span)
    tail = span.tail or ""

    parent.remove(span)

    if idx == 0:
        parent.text = new_text
    else:
        parent[idx - 1].tail = new_text

    for offset, child in enumerate(children):
        parent.insert(idx + offset, child)

    if children:
        last = children[-1]
        last.tail = (last.tail or "") + tail
    else:
        if idx == 0:
            parent.text = (parent.text or "") + tail
        else:
            prev = parent[idx - 1]
            prev.tail = (prev.tail or "") + tail


_NORMALIZE_TEXT_TAGS = frozenset(
    {
        "a",
        "b",
        "blockquote",
        "code",
        "del",
        "div",
        "em",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "i",
        "li",
        "p",
        "s",
        "span",
        "strong",
        "sub",
        "sup",
        "td",
        "th",
        "u",
    }
)


_TRIM_TAGS = frozenset(
    {"td", "th", "h1", "h2", "h3", "h4", "h5", "h6", "p"}
)


def _trim_inline_endpoints(root: ET._Element) -> None:
    """Strip leading/trailing whitespace from text inside cells, headings.

    GFM table cells and Markdown headings round-trip without their
    surrounding whitespace, so the canonicalized form must do the
    same — otherwise a cell that ends ``"...logic "`` in the source
    would diff against the rerendered ``"...logic"``.
    """

    for el in list(root.iter()):
        if not isinstance(el.tag, str):
            continue
        if strip_ns(el.tag) not in _TRIM_TAGS:
            continue
        children = list(el)
        if not children:
            if el.text:
                el.text = el.text.strip()
        else:
            if el.text:
                el.text = el.text.lstrip()
                if not el.text:
                    el.text = None
            last = children[-1]
            if last.tail:
                last.tail = last.tail.rstrip()
                if not last.tail:
                    last.tail = None


def _normalize_inline_whitespace(element: ET._Element) -> None:
    """Collapse runs of whitespace inside inline-context elements.

    Confluence's editor normalizes whitespace inside inline contexts
    the same way HTML rendering does (runs of spaces, tabs, and
    newlines collapse to a single space), so comparing against a
    freshly-PUT fragment always has to apply this step. ``<pre>``,
    ``<ac:plain-text-body>``, and code macros are exempted because
    whitespace is significant there.
    """

    for el in element.iter():
        if not isinstance(el.tag, str):
            continue
        tag = strip_ns(el.tag)
        ns = ns_of(el.tag)
        if ns not in (None, ""):
            continue
        if tag not in _NORMALIZE_TEXT_TAGS:
            continue
        # Skip elements that live inside a <pre> — whitespace there is
        # significant (this is the only place an inline <code> may
        # legitimately contain newlines).
        if _is_inside_pre(el):
            continue
        if el.text:
            el.text = re.sub(r"\s+", " ", el.text)
        for child in el:
            if child.tail:
                child.tail = re.sub(r"\s+", " ", child.tail)


def _is_inside_pre(element: ET._Element) -> bool:
    parent = element.getparent()
    while parent is not None:
        if isinstance(parent.tag, str) and strip_ns(parent.tag) == "pre":
            return True
        parent = parent.getparent()
    return False


def _serialize_children(root: ET._Element) -> str:
    """Serialize the root's children into a single XML string.

    Attribute order is normalized (lxml sorts with C14N output).
    """

    parts: list[str] = []
    for child in root:
        raw = ET.tostring(
            child,
            method="c14n",
            with_comments=False,
        ).decode("utf-8")
        # c14n re-emits xmlns declarations; strip them for readability.
        raw = re.sub(r'\s+xmlns:ac="[^"]*"', "", raw)
        raw = re.sub(r'\s+xmlns:ri="[^"]*"', "", raw)
        raw = re.sub(r'\s+xmlns=""', "", raw)
        parts.append(raw)
    return "".join(parts)


# ---------------------------------------------------------------------------
# Markdown canonicalization
# ---------------------------------------------------------------------------


def normalize_md(source: str) -> str:
    """Return a canonical form of a Markdown string.

    For v0.1 this is implemented as parse→render: the round trip
    through the cfxmark AST is itself a canonicalization step.
    """

    from cfxmark.parsers.md import parse_md
    from cfxmark.renderers.md import render_md

    doc, _warnings = parse_md(source)
    return render_md(doc)


def _safe_passthrough_alternation(
    prefixes: tuple[str, ...],
) -> str | None:
    """Build a regex alternation (``re.escape``-joined) of the
    caller-supplied passthrough prefixes, filtering out any
    ``cfxmark:`` prefix so cfxmark's own sentinel comments cannot be
    hijacked.

    Returns ``None`` when no eligible prefix remains — the caller
    should short-circuit in that case.

    Single source of truth used by both
    :func:`strip_passthrough_comments` (which matches the full
    comment block) and
    :func:`cfxmark.parsers.md._build_passthrough_open_re` (which
    matches only the opening line of a multi-line comment).
    """

    safe = tuple(p for p in prefixes if not p.startswith("cfxmark:"))
    if not safe:
        return None
    return "|".join(re.escape(p) for p in safe)


def strip_passthrough_comments(
    source: str,
    prefixes: tuple[str, ...],
) -> str:
    """Remove caller-owned HTML comment blocks from a Markdown string.

    The wrapper's canonical-compare workflow needs to diff two Markdown
    documents while ignoring any comment whose first token starts with
    one of ``prefixes`` — those comments are local metadata managed
    by the wrapper (``<!-- workflow:meta ... -->`` and friends), and
    they are known to exist on the local side but never survive a
    push-pull round trip through cfxmark (R1 contract).

    ``cfxmark:`` prefixes are silently filtered out so callers cannot
    accidentally strip cfxmark's own sentinel comments (``cfxmark:opaque``,
    ``cfxmark:notice``, …) and break round-trip safety.

    :param source: Markdown source text.
    :param prefixes: Tuple of leading-word prefixes to strip.
    :returns: The source with every matching comment block removed,
        including any trailing blank line the comment owned.
    """

    alternation = _safe_passthrough_alternation(prefixes)
    if alternation is None:
        return source
    # Match the comment plus any trailing whitespace run so the
    # surrounding blank line the comment owned is collapsed too —
    # otherwise stripping would leave stranded double newlines that
    # diff-compare as noise.
    pattern = re.compile(
        r"<!--\s*(?:" + alternation + r").*?-->[ \t]*\n?",
        re.DOTALL,
    )
    return pattern.sub("", source)


__all__ = [
    "VOLATILE_ATTRS",
    "canonicalize_cfx",
    "normalize_md",
    "strip_passthrough_comments",
]
