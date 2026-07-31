"""cfxmark AST → canonical Markdown.

The renderer deliberately outputs a single canonical form per
construct:

* Bullet lists use ``-``.
* Ordered lists use ``1.`` / ``2.`` / …
* Bold uses ``**``, italic uses ``*``.
* Inline code uses a single backtick; runs containing a backtick use
  double backticks.
* Code fences use triple backticks with a blank line before and after.
* Tables use GFM pipe syntax with a ``---`` alignment row.
* Headings use ATX (``#``, ``##``, …).
* Hard breaks use the explicit ``<br>`` spelling.
* Opaque blocks are emitted as documented in :mod:`cfxmark.opaque`.

Because the output is canonical, ``to_md(to_cfx(m))`` converges after
one normalization pass regardless of the shape of ``m``.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

from cfxmark.ast import (
    BLANK_PARAGRAPH_MARKER,
    LOOSE_LIST_MARKER,
    TIGHT_LIST_MARKER,
    AlignedSpan,
    BlockNode,
    BlockQuote,
    CellAlign,
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
    TableRow,
    Text,
    Underline,
    UnsupportedMarker,
    mixed_list_marker,
)
from cfxmark.colors import coarse_color
from cfxmark.exceptions import ConversionError
from cfxmark.image_metadata import render_link_destination, serialize_image_metadata
from cfxmark.opaque import (
    HEADER_NOTICE,
    has_round_trippable_inline_payload,
    is_round_trippable_inline_atomic,
    is_round_trippable_table_wrapper,
    serialize_asset_marker,
    serialize_inline_opaque,
    serialize_managed_inline_opaque,
    serialize_opaque,
    serialize_payloads,
)
from cfxmark.protected import protected_projection
from cfxmark.table_grid import TableGridError, bounded_table_grid_layout

# ---------------------------------------------------------------------------
# Rendering options
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MarkdownRenderOptions:
    """Tunable knobs for the Markdown renderer.

    These exist to let future versions experiment with alternate bullet
    markers or header styles without breaking tests written against the
    default form.
    """

    bullet_marker: str = "-"
    code_fence: str = "```"
    profile: Literal["legacy", "editable", "readable"] = "legacy"
    protected_projections: bool = False


DEFAULT_OPTIONS = MarkdownRenderOptions()


# ---------------------------------------------------------------------------
# Inline rendering
# ---------------------------------------------------------------------------


def _escape_text(text: str) -> str:
    """Escape Markdown-significant characters in a text run.

    We escape conservatively. The exact set of characters that *needs*
    escaping depends on position, but escaping the broader set
    everywhere is always safe, and the canonical form is still stable.

    The pipe character ``|`` is escaped because GFM treats lines that
    contain ``|`` followed by a separator row as table syntax. Without
    escaping, a paragraph that looks like ``| a | b |`` would round
    trip into a real table on the next ``parse_md`` pass — turning
    paragraph-shaped user content into a structural table.
    """

    out: list[str] = []
    for ch in text:
        if ch == "\\":
            out.append("\\\\")
        elif ch in "`*_[]|<>&~":
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


def _render_inline_text(
    nodes: tuple[InlineNode, ...],
    *,
    prev_char: str = "",
    next_char: str = "",
    opts: MarkdownRenderOptions = DEFAULT_OPTIONS,
    table_cell: bool = False,
) -> str:
    nodes = _coalesce_adjacent_styles(nodes)
    parts: list[str] = []
    for i, node in enumerate(nodes):
        prev = _last_char_of(parts) or prev_char
        nxt = _first_char_of_node(nodes[i + 1]) if i + 1 < len(nodes) else next_char
        parts.append(
            _render_inline_node(
                node,
                prev_char=prev,
                next_char=nxt,
                opts=opts,
                table_cell=table_cell,
            )
        )
    return "".join(parts)


def _coalesce_adjacent_styles(
    nodes: tuple[InlineNode, ...],
) -> tuple[InlineNode, ...]:
    """Merge adjacent identical inline styles into one canonical run.

    Markdown has no way to spell two adjacent style runs: ``*A**B*``
    re-parses as one ``<em>`` whose text carries a literal ``**``. An
    uninterrupted run is therefore rendered as a single span. A separator
    (even one space) is a real boundary and keeps the runs apart, because
    it lands in its own ``Text`` node and breaks the adjacency test below.
    """

    merged: list[InlineNode] = []
    for node in nodes:
        if isinstance(node, (Strong, Emphasis)) and not node.children:
            continue
        if isinstance(node, Strong) and merged and isinstance(merged[-1], Strong):
            previous = merged[-1]
            assert isinstance(previous, Strong)
            merged[-1] = Strong(children=previous.children + node.children)
        elif isinstance(node, Emphasis) and merged and isinstance(merged[-1], Emphasis):
            previous_emphasis = merged[-1]
            assert isinstance(previous_emphasis, Emphasis)
            merged[-1] = Emphasis(children=previous_emphasis.children + node.children)
        elif isinstance(node, ColorSpan) and merged and isinstance(merged[-1], ColorSpan):
            previous_color = merged[-1]
            assert isinstance(previous_color, ColorSpan)
            if previous_color.color == node.color and previous_color.background == node.background:
                merged[-1] = ColorSpan(
                    color=previous_color.color,
                    children=previous_color.children + node.children,
                    background=previous_color.background,
                )
            else:
                merged.append(node)
        else:
            merged.append(node)
    return tuple(merged)


def _last_char_of(parts: list[str]) -> str:
    for part in reversed(parts):
        if part:
            return part[-1]
    return ""


def _first_char_of_node(node: InlineNode) -> str:
    if isinstance(node, Text):
        return node.content[:1] if node.content else ""
    if isinstance(node, (SoftBreak, HardBreak)):
        return " "
    if isinstance(node, Link):
        return "["
    if isinstance(node, Image):
        return "!"
    if isinstance(node, InlineCode):
        return "`"
    if isinstance(node, Strong):
        return "*"
    if isinstance(node, Emphasis):
        return "*"
    if isinstance(node, Strikethrough):
        return "~"
    return ""


def _is_word_char(ch: str) -> bool:
    """True if ``ch`` is a Unicode word character (letters or digits)."""

    if not ch:
        return False
    return ch.isalnum()


# CommonMark 0.30 *ASCII punctuation*. Kept explicit rather than derived from
# Unicode categories: several of these (``$ + < = > ^ ` | ~``) are categorized
# ``S*``, not ``P*``, yet the spec counts them as punctuation.
_ASCII_PUNCT = frozenset("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")


def _is_md_punct(ch: str) -> bool:
    """CommonMark's *Unicode punctuation character* (spec 0.30).

    This targets the locked mistletoe 1.5.1 parser, whose punctuation table
    cites CommonMark 0.30: ASCII punctuation plus Unicode general category
    ``P*``. CommonMark 0.31.2 widens this to ``P*`` **or** ``S*``, which is
    *not* what the pinned parser does — so a symbol such as ``★``, ``°``,
    ``€``, or an emoji is deliberately not punctuation here. Upgrading
    mistletoe requires re-running the flanking property oracle.
    """

    if not ch:
        return False
    return ch in _ASCII_PUNCT or unicodedata.category(ch).startswith("P")


def _is_md_space(ch: str) -> bool:
    """CommonMark's *Unicode whitespace character* (includes category ``Zs``)."""

    if not ch:
        return False
    return ch.isspace() or unicodedata.category(ch) == "Zs"


def _blocks_asterisk_flanking(outside: str, inside: str) -> bool:
    """Whether an asterisk delimiter run fails to flank across one boundary.

    A run flanks unless the character just inside it is punctuation while the
    character just outside is neither whitespace nor punctuation. An absent
    outside character (start / end of the containing text) counts as
    whitespace, so it never blocks.
    """

    return _is_md_punct(inside) and bool(outside) and not (_is_md_space(outside) or _is_md_punct(outside))


def _needs_asterisk_fallback(
    inner: str,
    prev_char: str,
    next_char: str,
) -> bool:
    """Detect when ``*`` / ``**`` would not be re-parsed as emphasis.

    An asterisk opener only has to be left-flanking and a closer only has to
    be right-flanking; the extra "must not also flank the other way" clause
    applies to ``_`` / ``__``. Asterisk emphasis therefore survives intraword
    adjacency — ``**볼드**이다`` and ``**bold**text`` both re-parse — and
    breaks only at a boundary where :func:`_blocks_asterisk_flanking` holds.

    The two boundaries are evaluated independently: the opener pairs
    ``prev_char`` with the first inside character, the closer pairs the last
    inside character with ``next_char``.
    """

    if not inner:
        return False
    inside_first = inner[0]
    inside_last = inner[-1]
    # Boundary whitespace is hoisted out of the delimiters before this call.
    if not inside_first.strip() or not inside_last.strip():
        return False
    return _blocks_asterisk_flanking(prev_char, inside_first) or _blocks_asterisk_flanking(next_char, inside_last)


def _needs_strikethrough_fallback(
    inner: str,
    prev_char: str,
    next_char: str,
) -> bool:
    """Detect when ``~~`` would not be re-parsed as a strikethrough.

    ``~~`` is not a CommonMark construct and the pinned parser does not run it
    through the flanking rules at all — mistletoe 1.5.1 matches it with the
    regex ``(?<!\\\\)(?:\\\\\\\\)*~~(.+?)~~``. It therefore keeps its own
    conservative word-boundary contract instead of sharing the asterisk
    predicate; routing ``~~`` through the asterisk rules would emit ``<del>``
    where ``~~`` works and drop it where ``~~`` does not.
    """

    if not inner:
        return False
    if not inner[0].strip() or not inner[-1].strip():
        return False
    return _is_word_char(prev_char) or _is_word_char(next_char)


def _render_inline_node(
    node: InlineNode,
    *,
    prev_char: str = "",
    next_char: str = "",
    opts: MarkdownRenderOptions = DEFAULT_OPTIONS,
    table_cell: bool = False,
) -> str:
    if isinstance(node, Text):
        return _escape_text(node.content)
    if isinstance(node, SoftBreak):
        return "\n"
    if isinstance(node, HardBreak):
        return "<br>"
    if isinstance(node, Emphasis):
        inner = _render_inline_text(node.children, opts=opts, table_cell=table_cell)
        leading, core, trailing = _split_boundary_whitespace(inner)
        if not core:
            return inner
        if _needs_asterisk_fallback(
            core,
            leading[-1:] or prev_char,
            trailing[:1] or next_char,
        ):
            return f"{leading}<em>{core}</em>{trailing}"
        return f"{leading}*{core}*{trailing}"
    if isinstance(node, Strong):
        children = _flatten_redundant_strong(node.children)
        inner = _render_inline_text(children, opts=opts, table_cell=table_cell)
        leading, core, trailing = _split_boundary_whitespace(inner)
        if not core:
            return inner
        if _needs_asterisk_fallback(
            core,
            leading[-1:] or prev_char,
            trailing[:1] or next_char,
        ):
            return f"{leading}<strong>{core}</strong>{trailing}"
        return f"{leading}**{core}**{trailing}"
    if isinstance(node, Strikethrough):
        inner = _render_inline_text(node.children, opts=opts, table_cell=table_cell)
        leading, core, trailing = _split_boundary_whitespace(inner)
        if not core:
            return inner
        if _needs_strikethrough_fallback(
            core,
            leading[-1:] or prev_char,
            trailing[:1] or next_char,
        ):
            return f"{leading}<del>{core}</del>{trailing}"
        return f"{leading}~~{core}~~{trailing}"
    if isinstance(node, Subscript):
        return f"<sub>{_render_inline_text(node.children, opts=opts, table_cell=table_cell)}</sub>"
    if isinstance(node, Superscript):
        return f"<sup>{_render_inline_text(node.children, opts=opts, table_cell=table_cell)}</sup>"
    if isinstance(node, Underline):
        inner = _render_inline_text(node.children, opts=opts, table_cell=table_cell)
        return inner if opts.profile == "readable" else f"<ins>{inner}</ins>"
    if isinstance(node, ColorSpan):
        inner = _render_inline_text(node.children, opts=opts, table_cell=table_cell)
        if opts.profile == "readable":
            return inner
        if opts.profile == "editable":
            color = coarse_color(node.color)
            if color is None:
                color = node.color
            if color == "neutral" and not node.background:
                return inner
            tag = "mark" if node.background else "span"
            return f'<{tag} data-cfx-color="{_escape_attr(color)}">{inner}</{tag}>'
        css_property = "background-color" if node.background else "color"
        return f'<span style="{css_property}:{node.color}">{inner}</span>'
    if isinstance(node, AlignedSpan):
        inner = _render_inline_text(node.children, opts=opts, table_cell=table_cell)
        if opts.profile == "readable":
            return inner
        return f"{inner}<!-- cfxmark:align={node.align} -->"
    if isinstance(node, Citation):
        return f"<cite>{_render_inline_text(node.children, opts=opts, table_cell=table_cell)}</cite>"
    if isinstance(node, InlineCode):
        return _render_inline_code(node.content, escape_pipes=table_cell)
    if isinstance(node, Link):
        label = _render_inline_text(node.children, opts=opts, table_cell=table_cell)
        if not node.href_present:
            if opts.profile == "editable":
                return f"<a>{label}</a>"
            return label
        if not label and opts.profile != "editable":
            label = _escape_text(node.url)
        destination = _render_link_destination(node.url)
        if node.title:
            return f'[{label}]({destination} "{_escape_md_title(node.title)}")'
        return f"[{label}]({destination})"
    if isinstance(node, Image):
        return _render_inline_image(node, opts)
    if isinstance(node, InlineDirectiveMacro):
        params = " ".join(f'{key}="{_escape_attr(value)}"' for key, value in node.parameters)
        suffix = f" {params}" if params else ""
        return f"{{{{{node.name}{suffix}}}}}"
    if isinstance(node, UnsupportedMarker):
        return _render_unsupported_marker(node)
    if isinstance(node, InlineOpaque):
        if opts.profile == "readable":
            return f"[{_escape_text(node.label)}]"
        if opts.profile == "editable" and (
            is_round_trippable_inline_atomic(node.raw_xml)
            or is_round_trippable_table_wrapper(node.raw_xml, node.source_path)
        ):
            return serialize_managed_inline_opaque(node.label, node.opaque_id)
        if opts.profile == "editable" and opts.protected_projections:
            return protected_projection(node)
        if opts.profile == "editable":
            marker = UnsupportedMarker(kind="inline", macro=_opaque_macro_name(node.raw_xml))
            return f"\\[{_escape_text(node.label)}\\]{_render_unsupported_marker(marker)}"
        return serialize_inline_opaque(node.label, node.opaque_id)
    raise TypeError(f"unexpected inline node: {type(node).__name__}")


def _split_boundary_whitespace(text: str) -> tuple[str, str, str]:
    """Move delimiter-adjacent whitespace outside inline markup."""

    match = re.match(r"^(\s*)(.*?)(\s*)$", text, re.DOTALL)
    if match is None:
        return "", text, ""
    return match.group(1), match.group(2), match.group(3)


def _flatten_redundant_strong(
    nodes: tuple[InlineNode, ...],
) -> tuple[InlineNode, ...]:
    """Remove nested strong nodes while an outer strong is active."""

    flattened: list[InlineNode] = []
    for node in nodes:
        if isinstance(node, Strong):
            flattened.extend(_flatten_redundant_strong(node.children))
        else:
            flattened.append(node)
    return tuple(flattened)


def _protect_boundary_nbsp(text: str) -> str:
    """Encode boundary NBSP so CommonMark parsers cannot trim it."""

    match = re.match(r"^(\u00a0*)(.*?)(\u00a0*)$", text, re.DOTALL)
    if match is None:
        return text
    return "&nbsp;" * len(match.group(1)) + match.group(2) + "&nbsp;" * len(match.group(3))


def _render_inline_code(content: str, *, escape_pipes: bool = False) -> str:
    if escape_pipes:
        content = re.sub(
            r"(\\*)\|",
            lambda match: ("\\" if not match.group(1) else match.group(1) * 2) + "|",
            content,
        )
    if "`" not in content:
        padded = f" {content} " if content.strip() and (content[:1].isspace() or content[-1:].isspace()) else content
        return f"`{padded}`"
    # Pick a backtick run longer than any run inside the content.
    longest = 0
    current = 0
    for ch in content:
        if ch == "`":
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    fence = "`" * (longest + 1)
    padded = content
    if padded.strip() and (padded.startswith(("`", " ")) or padded.endswith(("`", " "))):
        padded = f" {padded} "
    return f"{fence}{padded}{fence}"


def _render_inline_image(node: Image, opts: MarkdownRenderOptions) -> str:
    alt = _escape_text(node.alt)
    destination = _render_link_destination(node.src)
    if node.title:
        link = f'![{alt}]({destination} "{_escape_md_title(node.title)}")'
    else:
        link = f"![{alt}]({destination})"
    if opts.profile != "readable":
        link += serialize_image_metadata(
            width=node.width,
            height=node.height,
            thumbnail=node.thumbnail,
            align=node.align,
        )
    # Local attachments (non-absolute URLs) carry an asset marker after
    # the image metadata marker so
    # ``cfxmark.resolve_assets`` can fetch and either embed or
    # sidecar them later. External URLs need no marker — they are
    # already resolvable.
    if opts.profile != "readable" and (node.attachment_filename is not None or _is_local_attachment(node.src)):
        link += serialize_asset_marker(node.attachment_filename or node.src)
    return link


def _is_local_attachment(src: str) -> bool:
    """True if ``src`` is a Confluence attachment (not an absolute URL).

    The check intentionally rejects ``data:`` URIs as well — those are
    already self-contained and need no further resolution.
    """

    if src.startswith("data:"):
        return False
    return not bool(_ABSOLUTE_URL_RE.match(src))


def _is_absolute_url(src: str) -> bool:
    return bool(_ABSOLUTE_URL_RE.match(src))


_ABSOLUTE_URL_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


def _escape_md_title(title: str) -> str:
    """Escape characters that would terminate a Markdown link/image title.

    A title literal is wrapped in ``"…"`` so any embedded ``"`` has to
    be backslash-escaped, otherwise mistletoe (and any CommonMark
    parser) will treat the inner quote as the closing delimiter and
    the rest of the construct degrades to plain text.
    """

    return title.replace("\\", "\\\\").replace('"', '\\"')


def _render_link_destination(url: str) -> str:
    return render_link_destination(url)


_PARAGRAPH_PREFIX_RE = re.compile(r"^(?P<indent> {0,3})(?P<marker>#{1,6}|[+\->)]|\d{1,9}[.)])(?=\s|$)")


def _escape_paragraph_start(text: str) -> str:
    """Prevent paragraph content from being reparsed as another block type."""

    if text.startswith("\t"):
        return "&#9;" + text[1:]
    if text.startswith("    "):
        return "&#32;" + text[1:]
    if re.match(r"^ {0,3}\.(?:\s|$)", text):
        dot = text.index(".")
        return text[:dot] + "\\" + text[dot:]
    match = _PARAGRAPH_PREFIX_RE.match(text)
    if match is None:
        if re.fullmatch(r" {0,3}={3,}\s*", text):
            marker_index = text.index("=")
            return text[:marker_index] + "\\" + text[marker_index:]
        if re.fullmatch(r" {0,3}-{3,}\s*", text):
            marker_index = text.index("-")
            return text[:marker_index] + "\\" + text[marker_index:]
        return text
    marker = match.group("marker")
    marker_start = match.start("marker")
    escape_at = marker_start + len(marker) - 1 if marker[0].isdigit() else marker_start
    return text[:escape_at] + "\\" + text[escape_at:]


def _escape_paragraph_lines(text: str) -> str:
    """Protect every line after a soft break from becoming a new block."""

    return "\n".join(_escape_paragraph_start(line) for line in text.split("\n"))


# ---------------------------------------------------------------------------
# Block rendering
# ---------------------------------------------------------------------------


def _render_block(block: BlockNode, opts: MarkdownRenderOptions) -> str:
    if isinstance(block, Heading):
        level = max(1, min(6, block.level))
        body = _render_inline_text(block.children, opts=opts).strip(" \t\r\n")
        return "#" * level + " " + _protect_boundary_nbsp(body)
    if isinstance(block, Paragraph):
        if block.bare_empty and len(block.children) == 1 and isinstance(block.children[0], HardBreak):
            return BLANK_PARAGRAPH_MARKER
        body = _render_inline_text(block.children, opts=opts).strip("\n")
        body = _protect_boundary_nbsp(body)
        body = _escape_paragraph_lines(body)
        if block.align in {"left", "center", "right"} and opts.profile != "readable":
            body += f"{{cfxalign={block.align}}}" if block.align == "left" else f"<!-- cfxmark:align={block.align} -->"
        return body
    if isinstance(block, CodeBlock):
        language = block.language or ""
        fence = opts.code_fence
        body = block.content
        # Ensure the content does not contain the fence sequence.
        if fence in body:
            fence = "`" * (body.count("`") + 3)
        return f"{fence}{language}\n{body}\n{fence}"
    if isinstance(block, BlockQuote):
        inner = _render_blocks(block.children, opts)
        return "\n".join("> " + line if line else ">" for line in inner.split("\n"))
    if isinstance(block, List):
        return _render_list(block, opts, indent="")
    if isinstance(block, ListItem):
        # Emitting a lone ListItem is unusual; wrap in a bullet list.
        return _render_list(
            List(list_type=ListType.BULLET, items=(block,)),
            opts,
            indent="",
        )
    if isinstance(block, HorizontalRule):
        return "---"
    if isinstance(block, Table):
        return _render_table(block, opts)
    if isinstance(block, DirectiveMacro):
        return _render_directive(block, opts)
    if isinstance(block, OpaqueBlock):
        if opts.profile == "readable":
            return "> [Unsupported Confluence content]"
        if opts.profile == "editable" and is_round_trippable_table_wrapper(block.raw_xml, block.source_path):
            return serialize_opaque(block.raw_xml, block.opaque_id)
        if opts.profile == "editable" and opts.protected_projections:
            return protected_projection(block)
        if opts.profile == "editable":
            marker = UnsupportedMarker(kind="block", macro=_opaque_macro_name(block.raw_xml))
            return "> \\[Unsupported Confluence content\\]\n\n" + _render_unsupported_marker(marker)
        return serialize_opaque(block.raw_xml, block.opaque_id)
    if isinstance(block, UnsupportedMarker):
        return _render_unsupported_marker(block)
    if isinstance(block, PassthroughComment):
        # Caller-owned HTML comment — emit verbatim. The content
        # already includes the ``<!--`` / ``-->`` sentinels so the
        # renderer simply echoes it as its own block with the normal
        # surrounding blank lines that ``_render_blocks`` provides.
        return block.content
    raise TypeError(f"unexpected block node: {type(block).__name__}")


def _render_blocks(
    blocks: tuple[BlockNode, ...],
    opts: MarkdownRenderOptions,
) -> str:
    parts: list[str] = []
    for block in blocks:
        parts.append(_render_block(block, opts))
    return "\n\n".join(parts)


def _commonmark_would_force_loose(node: List) -> bool:
    """Whether re-parsing this list's Markdown would come back loose.

    True when any item carries more than inline content, because the blank line
    that separates an item's blocks is exactly what makes CommonMark call a list
    loose -- and there is no way to write those blocks without it.
    """

    for item in node.items:
        blocks = [child for child in item.children if not isinstance(child, List)]
        if len(blocks) > 1 or any(not isinstance(child, Paragraph) for child in blocks):
            return True
    return False


def _render_list(
    node: List,
    opts: MarkdownRenderOptions,
    indent: str,
) -> str:
    """Render a list. ``indent`` is the column the bullet starts at."""

    lines: list[str] = []
    # Item-level shape outranks the list-level marker. A paragraph plus another
    # block makes CommonMark call the list loose, but storage may wrap only that
    # item; a plain `tight` marker would erase its `<p>`. The mixed marker names
    # the wrapped items and therefore has to be chosen first.
    #
    # A uniformly tight list can still have no tight spelling in CommonMark:
    # an item holding a paragraph and a code block makes the whole list loose,
    # and loose wraps every item in a paragraph. Measured on the corpus, two
    # pages are exactly this. Rather than quietly wrap items nobody touched, the
    # shape is recorded so the way back can reproduce it.
    #
    # Only for documents that came from storage -- a marker is written by a pull
    # and never by an author, so hand-written Markdown keeps CommonMark's own
    # answer and this changes no existing dialect.
    if not node.loose and any(item.loose for item in node.items):
        wrapped = tuple(i for i, item in enumerate(node.items) if item.loose)
        lines.append(f"{indent}{mixed_list_marker(wrapped)}")
    elif not node.loose and _commonmark_would_force_loose(node):
        lines.append(f"{indent}{TIGHT_LIST_MARKER}")
    # And the mirror: looseness is spelled with blank lines *between* items, so a
    # one-item list has nowhere to put one and comes back tight. Storage holds
    # `<ul><li><p>Keep</p></li></ul>` readily, and without this it returned as
    # `<ul><li>Keep</li></ul>` -- a change to an item nobody edited.
    elif node.loose and len(node.items) < 2:
        lines.append(f"{indent}{LOOSE_LIST_MARKER}")
    for i, item in enumerate(node.items):
        # The spelling that makes the distinction survive. A loose list is
        # written with its items separated by blank lines, which is how
        # CommonMark says "wrap each item's content in a paragraph" -- and it is
        # the only way back to `<li><p>x</p></li>` from Markdown.
        if node.loose and i:
            lines.append("")
        if node.list_type == ListType.BULLET:
            marker = f"{opts.bullet_marker} "
        else:
            marker = f"{node.start + i}. "
        body_indent = indent + " " * len(marker)
        body = _render_list_item_body(item, opts, body_indent)
        body_lines = body.split("\n")
        if not body_lines:
            lines.append(f"{indent}{marker}")
            continue
        # First line carries the bullet marker.
        first_line = body_lines[0]
        # When the item's first child is itself a list, that nested list already
        # rendered its own first line at ``body_indent`` (``indent`` + marker
        # width). Prepending this item's marker on top of that indent would push
        # the nested content 4+ columns right, and Markdown reparses the
        # over-indented line as a code block (``- x\n  -     1. a`` -> code
        # macro). Strip exactly one ``body_indent`` prefix so the parent marker
        # replaces it: ``- x\n  - 1. a``. A first child that is inline text, a
        # code block, or any non-list block keeps its own indentation contract
        # and must not take this branch.
        if item.children and isinstance(item.children[0], List) and first_line.startswith(body_indent):
            first_line = first_line[len(body_indent) :]
        lines.append(f"{indent}{marker}{first_line}")
        # Continuation lines sit under the bullet body column. Lines
        # that already start with the body indent are passed through
        # (this happens for nested lists that rendered themselves with
        # their own indent).
        for line in body_lines[1:]:
            if not line:
                lines.append("")
            elif line.startswith(body_indent):
                lines.append(line)
            else:
                lines.append(body_indent + line)
    return "\n".join(lines)


def _render_list_item_body(
    item: ListItem,
    opts: MarkdownRenderOptions,
    body_indent: str,
) -> str:
    """Render the contents of a list item.

    The returned string has its first line *un-indented* — the caller
    prepends the bullet marker. Subsequent lines are indented to
    ``body_indent`` (which is ``indent + len(marker)`` at the call site).
    """

    if not item.children:
        return ""

    parts: list[str] = []
    for i, child in enumerate(item.children):
        if isinstance(child, List):
            # Nested list renders itself with its own indentation.
            parts.append(_render_list(child, opts, body_indent))
        elif isinstance(child, Paragraph):
            paragraph = (
                BLANK_PARAGRAPH_MARKER
                if child.bare_empty and len(child.children) == 1 and isinstance(child.children[0], HardBreak)
                else _render_inline_text(child.children, opts=opts).strip("\n")
            )
            parts.append(_protect_boundary_nbsp(paragraph))
        elif isinstance(child, CodeBlock):
            # Code blocks are rendered verbatim; indent all lines.
            code = _render_block(child, opts)
            parts.append("\n".join(body_indent + ln if ln else ln for ln in code.split("\n")).lstrip(" "))
        else:
            body = _render_block(child, opts)
            parts.append("\n".join(body_indent + ln if ln else ln for ln in body.split("\n")).lstrip(" "))

    # Join parts with the appropriate separator:
    #   Paragraph → List  : single newline (tight list continuation)
    #   List → List       : single newline
    #   anything else     : blank line
    result: list[str] = [parts[0]]
    for i in range(1, len(parts)):
        prev = item.children[i - 1]
        curr = item.children[i]
        tight = isinstance(prev, (Paragraph, List)) and isinstance(curr, List)
        sep = "\n" if tight else "\n\n"
        result.append(sep + parts[i])
    return "".join(result)


_COLSPAN_MARK = "<"
_ROWSPAN_MARK = "^"


def _render_table(table: Table, opts: MarkdownRenderOptions) -> str:
    def render_cell_text(cell: TableCell) -> str:
        # GFM table cells are single-line. HardBreak already renders as
        # canonical ``<br>``; turn any remaining soft-break newlines into
        # the same inline spelling so they cannot terminate the cell.
        text = _render_inline_text(cell.children, opts=opts, table_cell=True).strip(" \t\r\n")
        text = _protect_boundary_nbsp(text)
        text = text.replace("\n", "<br>")
        return text

    grid = _expand_table_grid(table)
    if not grid:
        return ""
    n_cols = len(grid[0])

    def render_grid_row(row: list[TableCell | str | None]) -> str:
        rendered: list[str] = []
        for entry in row:
            if entry is None:
                rendered.append("")
            elif isinstance(entry, str):
                rendered.append("" if opts.profile == "readable" else entry)
            else:
                rendered.append(render_cell_text(entry))
        return "| " + " | ".join(rendered) + " |"

    lines: list[str] = []
    if table.header is not None:
        # GFM permits exactly one row before the separator. Rowspan
        # continuations therefore live in the body grid and are folded
        # back into the header cell by the Markdown parser.
        lines.append(render_grid_row(grid[0]))
        body_grid = grid[1:]
    else:
        # GFM requires a header row — emit an empty one.
        lines.append("| " + " | ".join([""] * n_cols) + " |")
        body_grid = grid
    delimiters: list[str] = []
    for column in range(n_cols):
        align = table.alignments[column] if column < len(table.alignments) else CellAlign.NONE
        delimiters.append(
            {
                CellAlign.LEFT: ":---",
                CellAlign.CENTER: ":---:",
                CellAlign.RIGHT: "---:",
            }.get(align, "---")
        )
    lines.append("| " + " | ".join(delimiters) + " |")
    for r in body_grid:
        lines.append(render_grid_row(r))
    return "\n".join(lines)


def _opaque_macro_name(raw_xml: str) -> str | None:
    match = re.search(r'\bac:name="([^"]+)"', raw_xml)
    return match.group(1) if match else None


def _render_unsupported_marker(node: UnsupportedMarker) -> str:
    macro = f' macro="{_escape_attr(node.macro)}"' if node.macro else ""
    occurrence = f' occurrence="{_escape_attr(node.occurrence_id)}"' if node.occurrence_id else ""
    return f"<!-- cfxmark:unsupported kind={node.kind}{macro}{occurrence} push-safe=false -->"


def _expand_table_grid(
    table: Table,
) -> list[list[TableCell | str | None]]:
    """Expand a Table into a 2-D grid of cells / continuation markers.

    Each grid row has the same number of columns. A position is one of:

    * a :class:`TableCell` — the cell that owns this position
    * ``"<"`` — a colspan continuation of the cell to the left
    * ``"^"`` — a rowspan continuation of the cell above
    * ``None`` — defensive padding (should not appear in well-formed
      tables)
    """

    rows: list[TableRow] = []
    if table.header is not None:
        rows.append(table.header)
    rows.extend(table.body)

    if not rows:
        return []

    try:
        layout = bounded_table_grid_layout(rows)
    except TableGridError as error:
        raise ConversionError(str(error)) from error
    n_cols = layout.width

    grid: list[list[TableCell | str | None]] = [[None] * n_cols for _row in rows]
    for row_index, (row, anchors) in enumerate(zip(rows, layout.anchor_columns, strict=True)):
        for cell, column in zip(row.cells, anchors, strict=True):
            for covered_row in range(row_index, min(len(rows), row_index + cell.rowspan)):
                for covered_column in range(column, column + cell.colspan):
                    if (covered_row, covered_column) == (row_index, column):
                        grid[covered_row][covered_column] = cell
                    elif covered_row == row_index:
                        grid[covered_row][covered_column] = _COLSPAN_MARK
                    else:
                        grid[covered_row][covered_column] = _ROWSPAN_MARK
    return grid


def _render_directive(node: DirectiveMacro, opts: MarkdownRenderOptions) -> str:
    """Render a ``DirectiveMacro`` as a pandoc-style fenced div.

    ```
    ::: <name> key="value" key="value"
    <body as blocks>
    :::
    ```
    """

    head = f"::: {node.name}"
    if node.parameters:
        params = " ".join(f'{k}="{_escape_attr(v)}"' for k, v in node.parameters)
        head = f"{head} {params}"
    if node.body is None:
        return f"{head}\n:::"
    body_md = _render_blocks(node.body, opts)
    return f"{head}\n{body_md}\n:::"


def _escape_attr(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def render_md(
    document: Document,
    *,
    options: MarkdownRenderOptions = DEFAULT_OPTIONS,
) -> str:
    """Render a cfxmark AST to canonical Markdown.

    The result has three implicit sections:

    1. A ``<!-- cfxmark:notice -->`` header explaining the round-trip
       conventions to humans and AI agents — only emitted if the
       document contains any opaque or directive markers.
    2. The Markdown body itself.
    3. A trailing ``<!-- cfxmark:payloads -->`` block holding the raw
       XML for every inline opaque reference, indexed by the same
       SHA-256-prefixed opaque ID used in the inline links.
    """

    body = _render_blocks(document.children, options)
    payloads = (
        _collect_inline_opaque_payloads(document)
        if options.profile == "legacy"
        else {
            opaque_id: raw_xml
            for opaque_id, raw_xml in _collect_inline_opaque_payloads(document).items()
            if has_round_trippable_inline_payload(raw_xml)
        }
        if options.profile == "editable"
        else {}
    )
    parts: list[str] = []
    if options.profile == "legacy" and _document_has_special_markers(document, payloads):
        parts.append(HEADER_NOTICE)
        parts.append("")
    parts.append(body.rstrip("\n"))
    if payloads:
        parts.append("")
        parts.append(serialize_payloads(payloads))
    return "\n".join(parts).rstrip("\n") + "\n"


def _collect_inline_opaque_payloads(document: Document) -> dict[str, str]:
    """Walk the AST and collect every inline opaque payload by ID."""

    payloads: dict[str, str] = {}

    def visit(node: object) -> None:
        if isinstance(node, InlineOpaque):
            payloads[node.opaque_id] = node.raw_xml
            return
        for attr in ("children", "items", "cells", "body"):
            value = getattr(node, attr, None)
            if value is None:
                continue
            if isinstance(value, (list, tuple)):
                for child in value:
                    visit(child)
        # Tables nest header / body separately.
        header = getattr(node, "header", None)
        if header is not None:
            visit(header)

    visit(document)
    return payloads


def _document_has_special_markers(
    document: Document,
    payloads: dict[str, str],
) -> bool:
    """True if the document contains any cfxmark-specific construct
    that needs the header notice (block opaque, inline opaque, or a
    directive macro)."""

    if payloads:
        return True

    found = False

    def visit(node: object) -> None:
        nonlocal found
        if found:
            return
        if isinstance(node, (OpaqueBlock, DirectiveMacro, InlineDirectiveMacro)):
            found = True
            return
        for attr in ("children", "items", "cells", "body"):
            value = getattr(node, attr, None)
            if value is None:
                continue
            if isinstance(value, (list, tuple)):
                for child in value:
                    visit(child)
        header = getattr(node, "header", None)
        if header is not None:
            visit(header)

    visit(document)
    return found


__all__ = [
    "MarkdownRenderOptions",
    "DEFAULT_OPTIONS",
    "render_md",
]
