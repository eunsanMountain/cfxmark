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
* Hard breaks use trailing ``  `` (two spaces).
* Opaque blocks are emitted as documented in :mod:`cfxmark.opaque`.

Because the output is canonical, ``to_md(to_cfx(m))`` converges after
one normalization pass regardless of the shape of ``m``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from cfxmark.ast import (
    BlockNode,
    BlockQuote,
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
    TableRow,
    Text,
)
from cfxmark.opaque import (
    HEADER_NOTICE,
    serialize_asset_marker,
    serialize_inline_opaque,
    serialize_opaque,
    serialize_payloads,
)

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
        elif ch in "`*_[]|":
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


def _render_inline_text(
    nodes: tuple[InlineNode, ...],
    *,
    prev_char: str = "",
    next_char: str = "",
) -> str:
    parts: list[str] = []
    for i, node in enumerate(nodes):
        prev = _last_char_of(parts) or prev_char
        nxt = _first_char_of_node(nodes[i + 1]) if i + 1 < len(nodes) else next_char
        parts.append(_render_inline_node(node, prev_char=prev, next_char=nxt))
    return "".join(parts)


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
    """True if ``ch`` is a Unicode word character (letters or digits).

    CommonMark's emphasis rules block closing (or opening) a delimiter
    run when it is flanked on both sides by "word" characters — this
    matters especially for CJK, where Korean / Chinese / Japanese
    characters are all word characters that defeat the usual ``**X**``
    intra-word emphasis.
    """

    if not ch:
        return False
    return ch.isalnum()


def _needs_html_fallback(
    inner: str,
    prev_char: str,
    next_char: str,
) -> bool:
    """Detect when emphasis delimiters would not be re-parsed correctly.

    The CommonMark rules for flanking delimiter runs are subtle, but
    the failure mode we actually care about is intraword emphasis
    where the inside of the delimiter touches punctuation. A simple
    conservative test: if the outside is a word char on either side
    and the adjacent inside character is punctuation, fall back.
    """

    if not inner:
        return False
    inside_first = inner[0]
    inside_last = inner[-1]
    # Empty / whitespace boundaries — plain markdown works.
    if not inside_first.strip() or not inside_last.strip():
        return False
    # Word char outside with punctuation adjacent inside — problem.
    if _is_word_char(prev_char) or _is_word_char(next_char):
        return True
    return False


def _render_inline_node(
    node: InlineNode,
    *,
    prev_char: str = "",
    next_char: str = "",
) -> str:
    if isinstance(node, Text):
        return _escape_text(node.content)
    if isinstance(node, SoftBreak):
        return "\n"
    if isinstance(node, HardBreak):
        return "  \n"
    if isinstance(node, Emphasis):
        inner = _render_inline_text(node.children)
        if _needs_html_fallback(inner, prev_char, next_char):
            return f"<em>{inner}</em>"
        return "*" + inner + "*"
    if isinstance(node, Strong):
        inner = _render_inline_text(node.children)
        if _needs_html_fallback(inner, prev_char, next_char):
            return f"<strong>{inner}</strong>"
        return "**" + inner + "**"
    if isinstance(node, Strikethrough):
        inner = _render_inline_text(node.children)
        if _needs_html_fallback(inner, prev_char, next_char):
            return f"<del>{inner}</del>"
        return "~~" + inner + "~~"
    if isinstance(node, InlineCode):
        return _render_inline_code(node.content)
    if isinstance(node, Link):
        label = _render_inline_text(node.children) or node.url
        if node.title:
            return f'[{label}]({node.url} "{_escape_md_title(node.title)}")'
        return f"[{label}]({node.url})"
    if isinstance(node, Image):
        return _render_inline_image(node)
    if isinstance(node, InlineOpaque):
        return serialize_inline_opaque(node.label, node.opaque_id)
    raise TypeError(f"unexpected inline node: {type(node).__name__}")


def _render_inline_code(content: str) -> str:
    if "`" not in content:
        return f"`{content}`"
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
    if padded.startswith("`"):
        padded = " " + padded
    if padded.endswith("`"):
        padded = padded + " "
    return f"{fence}{padded}{fence}"


def _render_inline_image(node: Image) -> str:
    alt = node.alt.replace("]", "\\]")
    url = _encode_image_url(node)
    if node.title:
        link = f'![{alt}]({url} "{_escape_md_title(node.title)}")'
    else:
        link = f"![{alt}]({url})"
    # Local attachments (non-absolute URLs) carry an asset marker so
    # ``cfxmark.resolve_assets`` can fetch and either embed or
    # sidecar them later. External URLs need no marker — they are
    # already resolvable.
    if _is_local_attachment(node.src):
        link += serialize_asset_marker(node.src)
    return link


def _is_local_attachment(src: str) -> bool:
    """True if ``src`` is a Confluence attachment (not an absolute URL).

    The check intentionally rejects ``data:`` URIs as well — those are
    already self-contained and need no further resolution.
    """

    if src.startswith("data:"):
        return False
    return not bool(_ABSOLUTE_URL_RE.match(src))


_ABSOLUTE_URL_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


def _escape_md_title(title: str) -> str:
    """Escape characters that would terminate a Markdown link/image title.

    A title literal is wrapped in ``"…"`` so any embedded ``"`` has to
    be backslash-escaped, otherwise mistletoe (and any CommonMark
    parser) will treat the inner quote as the closing delimiter and
    the rest of the construct degrades to plain text.
    """

    return title.replace("\\", "\\\\").replace('"', '\\"')


def _encode_image_url(node: Image) -> str:
    """Encode image width/height into the URL fragment when needed.

    Standard Markdown has no syntax for image dimensions, so we piggy
    back on the URL fragment using a ``#cfxmark:w=<N>,h=<N>`` tail.
    The fragment is ignored by markdown renderers but parsed back by
    :func:`~cfxmark.parsers.md.parse_md`, which preserves the round
    trip through Confluence.
    """

    if node.width is None and node.height is None:
        return node.src
    parts: list[str] = []
    if node.width is not None:
        parts.append(f"w={node.width}")
    if node.height is not None:
        parts.append(f"h={node.height}")
    fragment = "cfxmark:" + ",".join(parts)
    if "#" in node.src:
        return f"{node.src}&{fragment}"
    return f"{node.src}#{fragment}"


# ---------------------------------------------------------------------------
# Block rendering
# ---------------------------------------------------------------------------


def _render_block(block: BlockNode, opts: MarkdownRenderOptions) -> str:
    if isinstance(block, Heading):
        level = max(1, min(6, block.level))
        return "#" * level + " " + _render_inline_text(block.children).strip()
    if isinstance(block, Paragraph):
        return _render_inline_text(block.children).strip("\n")
    if isinstance(block, CodeBlock):
        language = block.language or ""
        fence = opts.code_fence
        body = block.content
        # Ensure the content does not contain the fence sequence.
        if fence in body:
            fence = "`" * (body.count("`") + 3)
        body = body.rstrip("\n")
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
        return _render_table(block)
    if isinstance(block, DirectiveMacro):
        return _render_directive(block, opts)
    if isinstance(block, OpaqueBlock):
        return serialize_opaque(block.raw_xml, block.opaque_id)
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


def _render_list(
    node: List,
    opts: MarkdownRenderOptions,
    indent: str,
) -> str:
    """Render a list. ``indent`` is the column the bullet starts at."""

    lines: list[str] = []
    for i, item in enumerate(node.items):
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
        lines.append(f"{indent}{marker}{body_lines[0]}")
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
            parts.append(_render_inline_text(child.children).strip("\n"))
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
        tight = (
            isinstance(prev, (Paragraph, List))
            and isinstance(curr, List)
        )
        sep = "\n" if tight else "\n\n"
        result.append(sep + parts[i])
    return "".join(result)


_COLSPAN_MARK = "<"
_ROWSPAN_MARK = "^"


def _render_table(table: Table) -> str:
    def render_cell_text(cell: TableCell) -> str:
        # GFM table cells are single-line. A real ``\n`` (or the
        # ``  \n`` Markdown hard-break sequence) would terminate the
        # cell early, so we substitute hard breaks with the inline
        # ``<br>`` HTML tag — which GFM passes through verbatim.
        text = _render_inline_text(cell.children).strip()
        text = text.replace("  \n", "<br>")
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
                rendered.append(entry)
            else:
                rendered.append(render_cell_text(entry))
        return "| " + " | ".join(rendered) + " |"

    lines: list[str] = []
    if table.header is not None:
        # The header row may itself span multiple visual rows if any
        # of its cells uses rowspan > 1.
        header_rows = (
            max((c.rowspan for c in table.header.cells), default=1)
            if table.header.cells
            else 1
        )
        for r in grid[:header_rows]:
            lines.append(render_grid_row(r))
        body_grid = grid[header_rows:]
    else:
        # GFM requires a header row — emit an empty one.
        lines.append("| " + " | ".join([""] * n_cols) + " |")
        body_grid = grid
    lines.append("| " + " | ".join(["---"] * n_cols) + " |")
    for r in body_grid:
        lines.append(render_grid_row(r))
    return "\n".join(lines)


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

    # First pass: determine total column count from the row that
    # consumes the most columns (sum of colspans plus inherited
    # rowspan continuations from above).
    n_cols = 0
    pending_above: dict[int, int] = {}
    row_widths: list[int] = []
    for row in rows:
        col = 0
        cell_index = 0
        while cell_index < len(row.cells) or any(
            v > 0 for v in pending_above.values()
        ):
            while pending_above.get(col, 0) > 0:
                pending_above[col] -= 1
                col += 1
            if cell_index >= len(row.cells):
                break
            cell = row.cells[cell_index]
            for span_col in range(cell.colspan):
                if cell.rowspan > 1:
                    pending_above[col + span_col] = max(
                        pending_above.get(col + span_col, 0),
                        cell.rowspan - 1,
                    )
            col += cell.colspan
            cell_index += 1
        row_widths.append(col)
        n_cols = max(n_cols, col)
        # Decay any over-counted rowspans for this row.
        pending_above = {k: v for k, v in pending_above.items() if v > 0}

    # Second pass: build the grid.
    grid: list[list[TableCell | str | None]] = []
    pending: dict[int, TableCell] = {}
    pending_left: dict[int, int] = {}
    for row in rows:
        grid_row: list[TableCell | str | None] = [None] * n_cols
        col = 0
        for cell in row.cells:
            while col < n_cols and col in pending and pending_left[col] > 0:
                grid_row[col] = _ROWSPAN_MARK
                pending_left[col] -= 1
                if pending_left[col] == 0:
                    del pending[col]
                    del pending_left[col]
                col += 1
            if col >= n_cols:
                break
            grid_row[col] = cell
            for offset in range(1, cell.colspan):
                if col + offset < n_cols:
                    grid_row[col + offset] = _COLSPAN_MARK
            if cell.rowspan > 1:
                for offset in range(cell.colspan):
                    pending[col + offset] = cell
                    pending_left[col + offset] = cell.rowspan - 1
            col += cell.colspan
        # Tail: any unfinished rowspan continuations after the last cell.
        while col < n_cols:
            if col in pending and pending_left[col] > 0:
                grid_row[col] = _ROWSPAN_MARK
                pending_left[col] -= 1
                if pending_left[col] == 0:
                    del pending[col]
                    del pending_left[col]
            col += 1
        grid.append(grid_row)
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
    payloads = _collect_inline_opaque_payloads(document)
    parts: list[str] = []
    if _document_has_special_markers(document, payloads):
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
        if isinstance(node, (OpaqueBlock, DirectiveMacro)):
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
