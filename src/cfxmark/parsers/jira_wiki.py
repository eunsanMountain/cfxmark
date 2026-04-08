"""Jira wiki markup → cfxmark AST (experimental, lossy).

The Jira wiki dialect is looser and less formally specified than the
Confluence Storage Format. This parser targets the real-world patterns
seen in the cfxmark fixture corpus plus the spec-standard constructs
from Atlassian's Jira documentation. It makes **no lossless round-trip
promise** — that contract belongs to ``parse_cfx`` / ``render_cfx``.

Strategy
--------

1. **Block tokenizer** — line-oriented. Walks the source one line at a
   time and dispatches into block builders (heading, paired macro,
   list, table, blockquote, paragraph).
2. **Inline tokenizer** — character-oriented. Handles escape, soft
   break, monospace, inline color macro, link variants, image, and
   boundary-aware emphasis (``*``/``_``/``-``/``~``/``+``/``^``).

Heading policy (P3 / D1(c))
---------------------------

The parser is **1:1**: ``h1.`` → ``Heading(level=1)``, ``h2.`` → level
2, ... . The renderer side's Confluence-style promotion
(``_HEADING_PROMOTION_CONFLUENCE``) only runs on the output path and
does not affect parsing.

Unsupported constructs
----------------------

* ``~text~`` / ``+text+`` / ``^text^`` (subscript / underline /
  superscript) — boundary-aware parsed, but cfxmark's AST has no
  node for these. A matched pair is dropped (markers removed,
  content preserved) with a lossy warning. An unmatched marker is
  plain text. The end result is that real Jira text like
  ``8월~9월`` (a Korean date range) survives intact because neither
  end has a word boundary, while hypothetical ``word ~hint~ word``
  loses its subscript styling but keeps ``hint`` visible.
* ``[~username]`` user mentions — dropped with a warning.
* ``{color:#hex}...{color}`` — content kept, color emphasis dropped
  with a warning (D4 matches renderer policy).
* ``{panel:title=...}`` — mapped to a ``note`` admonition with a
  lossy warning (D4).
* Non-core Jira macros (``{toc}``, ``{anchor}``, ``{children}``,
  ``{expand}``, …) — emitted as empty unknown DirectiveMacro nodes
  with a warning.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from cfxmark.ast import (
    BlockNode,
    BlockQuote,
    CellType,
    CodeBlock,
    DirectiveMacro,
    Document,
    Emphasis,
    Heading,
    HorizontalRule,
    Image,
    InlineCode,
    InlineNode,
    Link,
    List,
    ListItem,
    ListType,
    Paragraph,
    SoftBreak,
    Strikethrough,
    Strong,
    Table,
    TableCell,
    TableRow,
    Text,
)
from cfxmark.exceptions import ParseError

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Filename extensions that should round-trip as an :class:`Image`
# rather than a :class:`Link` when encountered in ``[^attachment.ext]``
# or ``!file.ext!`` syntax. Everything else (``.msg``, ``.docx``, …)
# becomes a ``Link`` with an ``attachment:`` URL scheme so the
# wrapper can fetch it out-of-band.
_IMAGE_EXTENSIONS = frozenset(
    {"png", "jpg", "jpeg", "gif", "svg", "webp", "bmp", "tiff", "ico"}
)

# Known admonition macros that map to cfxmark's built-in flavours.
_ADMONITION_MACROS = frozenset({"info", "note", "warning", "tip"})

# Block-level paired macros. Each one opens with ``{name}`` or
# ``{name:params}`` on its own line (possibly with trailing text) and
# closes with ``{name}`` on its own line.
_BLOCK_PAIRED_MACROS = frozenset(
    {"code", "noformat", "quote", "info", "note", "warning", "tip", "panel"}
)

# Emphasis markers and their AST node class (``None`` means "drop
# markers, keep content, emit a lossy warning" — used for sub/sup/ins
# since cfxmark has no matching node).
_EMPHASIS_NODE: dict[str, type | None] = {
    "*": Strong,
    "_": Emphasis,
    "-": Strikethrough,
    "~": None,
    "+": None,
    "^": None,
}

_HEADING_RE = re.compile(r"^h([1-6])\.\s+(.*)$")
# The list marker group captures ``*`` / ``#`` / ``-`` chains. The
# trailing content is *optional* — a bare marker line (``-\n`` with
# nothing after it) is recognised as an empty list item so the
# round-trip agrees with mistletoe's CommonMark interpretation of
# ``- `` with no body, which otherwise would round-trip as ``-`` →
# empty List → ``*`` → plain text → ``\*`` and oscillate forever.
_LIST_MARKER_RE = re.compile(r"^\s*([*#\-]+)(?:\s+(.*))?$")
_MACRO_OPEN_RE = re.compile(r"^\s*\{([a-zA-Z][a-zA-Z0-9_-]*)(?::([^}]*))?\}")
_BLOCKQUOTE_RE = re.compile(r"^bq\.\s+(.*)$")
_HORIZONTAL_RULE_RE = re.compile(r"^-{4,}\s*$")


# ---------------------------------------------------------------------------
# Parse context
# ---------------------------------------------------------------------------


@dataclass
class _ParseContext:
    warnings: list[str] = field(default_factory=list)
    attachments: list[str] = field(default_factory=list)

    def add_attachment(self, filename: str) -> None:
        if filename and filename not in self.attachments:
            self.attachments.append(filename)


# ---------------------------------------------------------------------------
# Inline parsing
# ---------------------------------------------------------------------------


def _is_word_char(ch: str) -> bool:
    """True if ``ch`` is alphanumeric in any Unicode script.

    Used for boundary checks in emphasis parsing. Korean / Japanese /
    Chinese characters are ``isalnum()``-true so ``2~3`` and ``8월~9월``
    correctly fail the opener boundary check.
    """

    return bool(ch) and ch.isalnum()


def _is_emphasis_left_boundary(text: str, i: int) -> bool:
    """True if position ``i`` is a valid opening boundary for an
    emphasis marker.

    The opener is valid when the preceding character (if any) is not
    a word character — mirroring Jira's rendering rule that says
    ``2*3*4`` is literal text, not ``2<bold>3</bold>4``.
    """

    if i == 0:
        return True
    return not _is_word_char(text[i - 1])


def _is_emphasis_right_boundary(text: str, i: int) -> bool:
    """True if position ``i`` is a valid closing boundary for an
    emphasis marker (i.e. the character after the closer is not a
    word character)."""

    if i >= len(text):
        return True
    return not _is_word_char(text[i])


def _find_emphasis_close(text: str, start: int, marker: str) -> int | None:
    """Locate the matching closing emphasis marker for a run opened at
    ``text[start]``. Returns the index of the closing marker, or
    ``None`` if there is no valid pair on the same line.

    Rules:

    * The run must not cross a ``\\n`` (Jira emphasis is single-line).
    * Escape sequences are honoured (``\\*`` inside the run is text).
    * The content between the markers must be non-empty and must not
      begin with whitespace (``* text *`` is literal, not bold).
    * The character after the closer must satisfy the right-boundary
      rule.
    """

    n = len(text)
    if start + 1 >= n or text[start + 1].isspace():
        return None
    i = start + 1
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if ch == "\n":
            return None
        if ch == marker:
            # Disallow ``**`` being parsed as ``*<empty>*``.
            if i == start + 1:
                return None
            # The character *before* the closer cannot be whitespace —
            # ``*bold *`` is literal, not bold.
            if text[i - 1].isspace():
                i += 1
                continue
            if _is_emphasis_right_boundary(text, i + 1):
                return i
            # Right boundary failed — this closer is part of plain
            # text. Keep scanning for a later close.
            i += 1
            continue
        i += 1
    return None


def _try_parse_inline_macro(
    text: str, start: int, ctx: _ParseContext
) -> tuple[list[InlineNode], int] | None:
    """Try to parse a paired inline macro (e.g. ``{color:#hex}...{color}``).

    Returns ``(nodes, next_index)`` on success or ``None`` on no
    match. The only inline macro cfxmark recognises is
    ``{color}`` — its colour emphasis is dropped with a warning and
    the wrapped content is kept (D4 alignment with the renderer).
    """

    n = len(text)
    if start >= n or text[start] != "{":
        return None
    m = re.match(r"\{([a-zA-Z][a-zA-Z0-9_-]*)(?::([^}]*))?\}", text[start:])
    if m is None:
        return None
    name = m.group(1)
    if name != "color":
        return None
    open_end = start + m.end()
    close_marker = "{color}"
    close_idx = text.find(close_marker, open_end)
    if close_idx == -1:
        return None
    inner = text[open_end:close_idx]
    inner_nodes = _parse_inline(inner, ctx)
    ctx.warnings.append("color emphasis dropped (content kept)")
    return inner_nodes, close_idx + len(close_marker)


def _try_parse_link(
    text: str, start: int, ctx: _ParseContext
) -> tuple[InlineNode, int] | None:
    """Try to parse a Jira link starting at ``text[start]`` (which is
    ``[``).

    Supported forms:

    * ``[url]``                    — bare URL
    * ``[label|url]``              — labelled URL; label may itself
                                     contain nested brackets
    * ``[^filename.ext]``          — attachment reference; becomes
                                     an :class:`Image` for image
                                     extensions or a :class:`Link`
                                     with an ``attachment:`` URL
                                     for everything else
    * ``[~username]``              — user mention; dropped with a
                                     warning

    Returns ``(node, next_index)`` or ``None`` on no match.
    """

    n = len(text)
    if start >= n or text[start] != "[":
        return None
    depth = 1
    i = start + 1
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if ch == "\n":
            return None
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                break
        i += 1
    if i >= n or depth != 0:
        return None
    inner = text[start + 1 : i]
    next_i = i + 1

    # Attachment reference
    if inner.startswith("^"):
        filename = inner[1:].strip()
        if not filename:
            return None
        ctx.add_attachment(filename)
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext in _IMAGE_EXTENSIONS:
            return Image(src=filename, alt=""), next_i
        return (
            Link(
                url=f"attachment:{filename}",
                children=(Text(content=filename),),
            ),
            next_i,
        )

    # User mention — drop with warning
    if inner.startswith("~"):
        ctx.warnings.append(f"user mention [{inner}] dropped")
        return Text(content=""), next_i

    # Labelled or bare URL. Find the first *unescaped* top-level ``|``.
    pipe_idx = -1
    depth2 = 0
    j = 0
    while j < len(inner):
        c = inner[j]
        if c == "\\" and j + 1 < len(inner):
            j += 2
            continue
        if c == "[":
            depth2 += 1
        elif c == "]":
            depth2 -= 1
        elif c == "|" and depth2 == 0:
            pipe_idx = j
            break
        j += 1

    if pipe_idx == -1:
        url = _unescape(inner)
        return Link(url=url, children=()), next_i

    label = inner[:pipe_idx]
    url = _unescape(inner[pipe_idx + 1 :])
    label_nodes = _parse_inline(label, ctx, allow_link=False)
    return Link(url=url, children=tuple(label_nodes)), next_i


def _try_parse_image(
    text: str, start: int, ctx: _ParseContext
) -> tuple[Image, int] | None:
    """Try to parse ``!filename.ext!`` or
    ``!filename.ext|alt=foo,width=123!``.

    The opening ``!`` must sit at a word boundary, the filename must
    contain a dot (so plain ``hello!world`` does not get parsed as an
    image), and the closing ``!`` must sit at a word boundary too.
    """

    n = len(text)
    if start >= n or text[start] != "!":
        return None
    if not _is_emphasis_left_boundary(text, start):
        return None
    # Scan for the closing ``!`` on the same line.
    i = start + 1
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if ch == "\n":
            return None
        if ch == "!":
            break
        i += 1
    else:
        return None
    inner = text[start + 1 : i]
    next_i = i + 1
    if not _is_emphasis_right_boundary(text, next_i):
        return None
    if not inner or " " in inner.split("|", 1)[0] or "." not in inner.split("|", 1)[0]:
        return None
    parts = inner.split("|", 1)
    filename = parts[0]
    alt = ""
    width: int | None = None
    height: int | None = None
    if len(parts) == 2:
        for kv in parts[1].split(","):
            key, _, val = kv.partition("=")
            key = key.strip()
            val = val.strip()
            if key == "alt":
                alt = val
            elif key == "width" and val.isdigit():
                width = int(val)
            elif key == "height" and val.isdigit():
                height = int(val)
    ctx.add_attachment(filename)
    return Image(src=filename, alt=alt, width=width, height=height), next_i


def _try_parse_monospace(
    text: str, start: int
) -> tuple[InlineCode, int] | None:
    """Try to parse ``{{content}}`` monospace."""
    if text[start : start + 2] != "{{":
        return None
    end = text.find("}}", start + 2)
    if end == -1:
        return None
    content = text[start + 2 : end]
    return InlineCode(content=content), end + 2


def _unescape(text: str) -> str:
    """Strip backslash escapes from a raw text run.

    The Jira wiki renderer escapes the characters enumerated in its
    ``_ESCAPE_RE``. We honour any backslash-escaped character the
    same way so that ``\\|``, ``\\*``, ``\\~`` etc. round-trip to
    their literal form.
    """

    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n:
            out.append(text[i + 1])
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _parse_inline(
    text: str,
    ctx: _ParseContext,
    *,
    allow_link: bool = True,
) -> list[InlineNode]:
    """Convert a Jira wiki inline run into a list of AST inline nodes.

    ``allow_link=False`` disables the ``[...]`` link recognizer. Used
    when recursively parsing a link's own label so that nested
    ``[...]`` brackets in the label (e.g.
    ``[EXAMPLE-100 [draft] extended mode spec|url]``) are preserved
    as literal text instead of re-interpreted as another link.
    """

    nodes: list[InlineNode] = []
    buf: list[str] = []
    i = 0
    n = len(text)

    def flush_buf() -> None:
        if buf:
            nodes.append(Text(content="".join(buf)))
            buf.clear()

    while i < n:
        ch = text[i]

        # Escape — the next character is always literal.
        if ch == "\\" and i + 1 < n:
            buf.append(text[i + 1])
            i += 2
            continue

        # Newline inside an inline run is a soft break.
        if ch == "\n":
            flush_buf()
            nodes.append(SoftBreak())
            i += 1
            continue

        # Monospace {{...}}
        mono = _try_parse_monospace(text, i)
        if mono is not None:
            node, next_i = mono
            flush_buf()
            nodes.append(node)
            i = next_i
            continue

        # Inline macro ({color:...}...{color})
        if ch == "{":
            macro = _try_parse_inline_macro(text, i, ctx)
            if macro is not None:
                macro_nodes, next_i = macro
                flush_buf()
                nodes.extend(macro_nodes)
                i = next_i
                continue

        # Link [...]
        if ch == "[" and allow_link:
            link = _try_parse_link(text, i, ctx)
            if link is not None:
                node, next_i = link
                flush_buf()
                if isinstance(node, Text) and not node.content:
                    # Dropped user mention — do not emit an empty
                    # Text node.
                    pass
                else:
                    nodes.append(node)
                i = next_i
                continue

        # Image !file.ext!
        if ch == "!":
            image = _try_parse_image(text, i, ctx)
            if image is not None:
                node, next_i = image
                flush_buf()
                nodes.append(node)
                i = next_i
                continue

        # Emphasis: * _ - ~ + ^
        if ch in _EMPHASIS_NODE:
            # Boundary check on the opener.
            if _is_emphasis_left_boundary(text, i):
                close = _find_emphasis_close(text, i, ch)
                if close is not None:
                    inner = text[i + 1 : close]
                    inner_nodes = _parse_inline(inner, ctx)
                    node_cls = _EMPHASIS_NODE[ch]
                    flush_buf()
                    if node_cls is None:
                        # sub / sup / ins — drop markers, keep
                        # content, emit lossy warning.
                        ctx.warnings.append(
                            f"Jira '{ch}' inline formatting dropped"
                            f" (content preserved)"
                        )
                        nodes.extend(inner_nodes)
                    else:
                        nodes.append(node_cls(children=tuple(inner_nodes)))
                    i = close + 1
                    continue

        buf.append(ch)
        i += 1

    flush_buf()
    return nodes


# ---------------------------------------------------------------------------
# Block parsing
# ---------------------------------------------------------------------------


def _line_is_heading(line: str) -> bool:
    return _HEADING_RE.match(line.lstrip()) is not None


def _line_is_blockquote(line: str) -> bool:
    return _BLOCKQUOTE_RE.match(line.lstrip()) is not None


def _line_is_hr(line: str) -> bool:
    return _HORIZONTAL_RULE_RE.match(line.strip()) is not None


def _line_is_list(line: str) -> bool:
    return _LIST_MARKER_RE.match(line) is not None


def _line_is_table_row(line: str) -> bool:
    stripped = line.lstrip()
    return stripped.startswith("|") and stripped != "|"


def _line_is_paired_macro_open(line: str) -> str | None:
    """If the line opens a block-level paired macro, return the macro
    name. Else ``None``."""
    m = _MACRO_OPEN_RE.match(line)
    if m is None:
        return None
    name = m.group(1)
    if name in _BLOCK_PAIRED_MACROS:
        return name
    return None


def _line_terminates_paragraph(
    line: str,
    *,
    inside_table: bool = False,
) -> bool:
    """Return ``True`` if *line* starts a new block and therefore
    breaks an in-progress paragraph.

    ``inside_table=True`` disables the list / table-row checks —
    a nested list inside a table cell is a legitimate Jira pattern,
    and the table-row check is already handled upstream by the row
    start detector.
    """
    if not line.strip():
        return True
    if _line_is_heading(line):
        return True
    if _line_is_hr(line):
        return True
    if _line_is_blockquote(line):
        return True
    if not inside_table:
        if _line_is_list(line):
            return True
        if _line_is_table_row(line):
            return True
    if _line_is_paired_macro_open(line) is not None:
        return True
    return False


def _parse_macro_params(params: str) -> tuple[tuple[str, str], ...]:
    """Parse a macro parameter string like ``title=Foo|language=java``
    or a bare ``python`` (for ``{code:python}``).

    A bare single token is stored as ``("value", token)`` so the
    caller can inspect it. Key=value pairs are preserved in order.
    """

    params = params or ""
    if not params:
        return ()
    if "|" not in params and "=" not in params:
        return (("value", params.strip()),)
    out: list[tuple[str, str]] = []
    for pair in params.split("|"):
        pair = pair.strip()
        if not pair:
            continue
        if "=" in pair:
            key, _, val = pair.partition("=")
            out.append((key.strip(), val.strip()))
        else:
            out.append(("value", pair))
    return tuple(out)


def _code_language_from_params(
    params: tuple[tuple[str, str], ...],
) -> str | None:
    """Extract the ``language=`` value (or a bare positional language
    token) from a parsed ``{code}`` parameter list."""
    for key, val in params:
        if key == "language" and val:
            return val
        if key == "value" and val:
            return val
    return None


def _consume_paragraph(
    lines: list[str], start: int, ctx: _ParseContext
) -> tuple[Paragraph, int]:
    i = start
    n = len(lines)
    buf: list[str] = []
    while i < n:
        line = lines[i]
        if not buf:
            # First line of a paragraph is always consumed.
            buf.append(line)
            i += 1
            continue
        if _line_terminates_paragraph(line):
            break
        buf.append(line)
        i += 1
    text = "\n".join(buf)
    inline_nodes = _parse_inline(text, ctx)
    return Paragraph(children=tuple(inline_nodes)), i


def _consume_paired_macro(
    lines: list[str],
    start: int,
    name: str,
    ctx: _ParseContext,
) -> tuple[BlockNode | None, int]:
    """Consume a block-level paired macro ``{name[:params]}...{name}``.

    Returns ``(block, next_index)``. If the closing token is missing,
    returns ``(None, start)`` so the caller falls through to paragraph
    handling and does not lose the source line.
    """

    n = len(lines)
    first = lines[start]
    m = _MACRO_OPEN_RE.match(first)
    if m is None:
        return None, start
    params_text = m.group(2) or ""

    # The opening token may be followed by inline body content on the
    # same line (``{code:python}print(1){code}``). We do not special-
    # case that; Jira authors typically put bodies on their own lines.
    close_token = f"{{{name}}}"
    # Body starts after the opening token.
    open_end = m.end()
    remainder = first[open_end:]
    body_parts: list[str] = []
    close_line = -1

    # Same-line close?
    if close_token in remainder:
        idx = remainder.find(close_token)
        body_parts.append(remainder[:idx])
        close_line = start
    else:
        if remainder:
            body_parts.append(remainder)
        j = start + 1
        while j < n:
            if close_token in lines[j]:
                close_line = j
                before, _, _ = lines[j].partition(close_token)
                if before:
                    body_parts.append(before)
                break
            body_parts.append(lines[j])
            j += 1

    if close_line == -1:
        return None, start

    body_text = "\n".join(body_parts)
    params = _parse_macro_params(params_text)

    if name in ("code", "noformat"):
        language = _code_language_from_params(params) if name == "code" else None
        return CodeBlock(content=body_text, language=language), close_line + 1

    if name == "quote":
        inner_blocks = _parse_blocks(body_text, ctx)
        return BlockQuote(children=tuple(inner_blocks)), close_line + 1

    if name in _ADMONITION_MACROS:
        inner_blocks = _parse_blocks(body_text, ctx)
        return (
            DirectiveMacro(
                name=name,
                parameters=params,
                body=tuple(inner_blocks) if inner_blocks else None,
            ),
            close_line + 1,
        )

    if name == "panel":
        # D4: map panel → note admonition with a lossy warning so the
        # caller knows the mapping happened. The ``title=`` parameter
        # is preserved so the renderer can echo it.
        ctx.warnings.append(
            "{panel} macro mapped to {note} admonition (lossy)"
        )
        inner_blocks = _parse_blocks(body_text, ctx)
        return (
            DirectiveMacro(
                name="note",
                parameters=params,
                body=tuple(inner_blocks) if inner_blocks else None,
            ),
            close_line + 1,
        )

    # Unreachable because _line_is_paired_macro_open filters by name.
    return None, start


def _consume_blockquote_line(
    lines: list[str], start: int, ctx: _ParseContext
) -> tuple[BlockQuote, int]:
    line = lines[start]
    m = _BLOCKQUOTE_RE.match(line.lstrip())
    assert m is not None
    inline = _parse_inline(m.group(1), ctx)
    return (
        BlockQuote(children=(Paragraph(children=tuple(inline)),)),
        start + 1,
    )


@dataclass
class _RawListItem:
    marker: str  # e.g. "*", "**", "#*", "-"
    content: str
    continuation: list[str] = field(default_factory=list)


def _normalize_list_marker(raw: str) -> str:
    """Normalize a raw list marker to canonical form.

    Jira accepts ``-`` and ``*`` as equivalent bullet markers at the
    first level; we canonicalise ``-`` to ``*`` so the nested marker
    chain machinery does not have to special-case it.

    Raises ``ValueError`` if the marker contains an unknown character.
    """
    out = []
    for ch in raw:
        if ch == "-":
            out.append("*")
        elif ch in ("*", "#"):
            out.append(ch)
        else:
            raise ValueError(f"unknown list marker character: {ch!r}")
    return "".join(out)


def _consume_list(
    lines: list[str], start: int, ctx: _ParseContext
) -> tuple[List, int]:
    """Consume a Jira wiki list (possibly nested) and return a single
    top-level :class:`List` plus the next line index.

    Jira wiki list nesting uses marker repetition: ``*``, ``**``,
    ``*#``, ``#*#``, … . The *first* character of the marker chain
    determines the type of the top-level list; deeper levels are
    attached as nested ``ListItem`` children.
    """

    raw_items: list[_RawListItem] = []
    i = start
    n = len(lines)
    while i < n:
        line = lines[i]
        m = _LIST_MARKER_RE.match(line)
        if m is None:
            if not line.strip():
                # Blank line — look ahead. If the next non-blank is
                # still a list item, continue; otherwise terminate.
                j = i + 1
                while j < n and not lines[j].strip():
                    j += 1
                if j < n and _LIST_MARKER_RE.match(lines[j]):
                    i = j
                    continue
                break
            # Not a list marker and not blank — a continuation line
            # of the previous item (lazy continuation). Jira wiki
            # allows this in practice; attach to the previous item.
            if raw_items:
                raw_items[-1].continuation.append(line)
                i += 1
                continue
            break
        try:
            marker = _normalize_list_marker(m.group(1))
        except ValueError:
            break
        # ``m.group(2)`` is None when the line is a bare marker with
        # no trailing content (``-\n`` with nothing after it); treat
        # it as an empty string so the list item has empty inline
        # content instead of crashing the nested-list builder.
        content = m.group(2) or ""
        raw_items.append(_RawListItem(marker=marker, content=content))
        i += 1

    if not raw_items:
        return List(list_type=ListType.BULLET, items=()), start

    top = _build_nested_list(raw_items, ctx, prefix="")
    return top, i


def _build_nested_list(
    items: list[_RawListItem],
    ctx: _ParseContext,
    prefix: str,
) -> List:
    """Recursively turn a flat sequence of raw list items into a
    nested :class:`List` structure.

    ``prefix`` is the marker chain of the *parent* list. Items whose
    marker equals ``prefix + 'x'`` belong to the current list; items
    whose marker extends further nest into the previous item.
    """

    if not items:
        return List(list_type=ListType.BULLET, items=())

    first_marker = items[0].marker
    assert first_marker.startswith(prefix)
    level = len(prefix) + 1
    first_char = first_marker[level - 1]
    list_type = ListType.BULLET if first_char == "*" else ListType.ORDERED
    list_items: list[ListItem] = []
    i = 0
    n = len(items)
    while i < n:
        current = items[i]
        if len(current.marker) < level:
            # Back to a shallower level — caller handles these.
            break
        current_at_level = current.marker[:level]
        if current_at_level != prefix + first_char:
            # A different marker type at the current level (e.g. we
            # were building a ``*`` list and hit a ``#``). Stop here
            # and let the caller start a new list.
            break
        # Collect all items that belong under this item (same level
        # marker means sibling; longer marker means nested child).
        j = i + 1
        while j < n and len(items[j].marker) > level:
            j += 1
        children_blocks: list[BlockNode] = []
        # Build the inline content for this item.
        text_parts = [current.content]
        text_parts.extend(current.continuation)
        inline_text = "\n".join(text_parts)
        inline_nodes = _parse_inline(inline_text, ctx)
        if inline_nodes:
            children_blocks.append(Paragraph(children=tuple(inline_nodes)))
        # Recurse for nested items.
        nested_raw = items[i + 1 : j]
        if nested_raw:
            nested_list = _build_nested_list(
                nested_raw, ctx, prefix=prefix + first_char
            )
            children_blocks.append(nested_list)
        list_items.append(ListItem(children=tuple(children_blocks)))
        i = j

    return List(list_type=list_type, items=tuple(list_items))


def _split_table_row(line: str) -> tuple[list[str], bool]:
    """Split a single table-row line into cell contents.

    Honours ``\\|`` as an escape for literal pipes inside cells so
    the ``\\|\\|`` sequences in the RLM corpus round-trip as literal
    text instead of cell separators. Returns the list of raw cell
    strings and a flag telling whether the row is a header (``||``
    prefix)."""

    s = line.lstrip()
    is_header = s.startswith("||")
    # Normalize delimiter: for a header row, ``||x||y||`` becomes
    # ``|x|y|`` so we can use a single split path.
    if is_header:
        s = s.replace("||", "|")
    cells: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(s)
    if n == 0 or s[0] != "|":
        return [], is_header
    i = 1  # Skip the leading ``|``.
    while i < n:
        ch = s[i]
        if ch == "\\" and i + 1 < n:
            buf.append(s[i])
            buf.append(s[i + 1])
            i += 2
            continue
        if ch == "|":
            cells.append("".join(buf))
            buf.clear()
            i += 1
            continue
        buf.append(ch)
        i += 1
    # Discard the trailing empty cell (every Jira row ends with ``|``).
    if cells and not buf:
        pass
    elif buf:
        cells.append("".join(buf))
    return cells, is_header


def _consume_table(
    lines: list[str], start: int, ctx: _ParseContext
) -> tuple[Table, int]:
    """Consume a Jira wiki table.

    Multi-line cells are supported: a line that does not itself begin
    a new row and does not start a different block construct is glued
    onto the previous row with a newline separator. After all source
    lines of a row are joined into one string, the row is re-split by
    unescaped ``|`` characters — this is what Jira actually does, and
    it is the only way to correctly split rows like ::

        |Mode Alpha
        (OR)| * {color:#de350b}...{color}|

    which logically has two cells (``Mode Alpha\\n(OR)`` and the
    coloured body) separated by the ``|`` between them on line 2.

    A row is terminated by: a blank line, the end of the source, or a
    line that starts a different block construct (heading, horizontal
    rule, list, paired macro, another ``bq.``). Without the block-start
    guard the parser would greedily swallow a trailing ``h3. ...``
    heading into the last cell — a bug caught during fixture
    development where the final heading was authored without a
    preceding blank line.
    """

    row_sources: list[str] = []
    i = start
    n = len(lines)
    while i < n:
        line = lines[i]
        if not line.strip():
            break
        stripped = line.lstrip()
        if stripped.startswith("|"):
            row_sources.append(line)
            i += 1
            continue
        # Continuation — only honoured if the line does not start a
        # different block. Without this guard the table greedily eats
        # trailing content like ``h3. *TO-BE*`` that happens to sit
        # right after the last row with no blank separator.
        if row_sources and not _line_terminates_paragraph(
            line, inside_table=True
        ):
            row_sources[-1] = row_sources[-1] + "\n" + line
            i += 1
            continue
        break

    if not row_sources:
        return Table(header=None, body=()), start

    rows: list[tuple[list[str], bool]] = []
    for source in row_sources:
        cells, is_header = _split_table_row(source)
        if not cells:
            continue
        rows.append((cells, is_header))

    if not rows:
        return Table(header=None, body=()), start

    header_row: TableRow | None = None
    body_rows: list[TableRow] = []
    first_cells, first_is_header = rows[0]
    if first_is_header:
        header_row = _build_table_row(first_cells, CellType.HEADER, ctx)
        data_rows = rows[1:]
    else:
        data_rows = rows
    for cells, _ in data_rows:
        body_rows.append(_build_table_row(cells, CellType.DATA, ctx))

    return (
        Table(header=header_row, body=tuple(body_rows), alignments=()),
        i,
    )


def _build_table_row(
    raw_cells: list[str],
    kind: CellType,
    ctx: _ParseContext,
) -> TableRow:
    cells = [
        TableCell(
            kind=kind,
            children=tuple(_parse_inline(cell, ctx)),
        )
        for cell in raw_cells
    ]
    return TableRow(cells=tuple(cells))


def _parse_blocks(source: str, ctx: _ParseContext) -> list[BlockNode]:
    """Parse a Jira wiki source fragment into a list of block nodes.

    Shared between the top-level document parse and the recursive
    paired-macro body parse.
    """

    if not source:
        return []
    # Normalise line endings so the tokenizer sees ``\n`` only.
    source = source.replace("\r\n", "\n").replace("\r", "\n")
    lines = source.split("\n")
    # Trim a single trailing empty entry from ``split('\n')`` so a
    # source that ends with ``\n`` does not produce a phantom blank.
    if lines and lines[-1] == "":
        lines.pop()
    blocks: list[BlockNode] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        # Heading
        heading_match = _HEADING_RE.match(line.lstrip())
        if heading_match is not None:
            level = int(heading_match.group(1))
            content = heading_match.group(2)
            inline = _parse_inline(content, ctx)
            blocks.append(Heading(level=level, children=tuple(inline)))
            i += 1
            continue
        # Horizontal rule
        if _line_is_hr(line):
            blocks.append(HorizontalRule())
            i += 1
            continue
        # Paired macro
        macro_name = _line_is_paired_macro_open(line)
        if macro_name is not None:
            block, next_i = _consume_paired_macro(lines, i, macro_name, ctx)
            if block is not None:
                blocks.append(block)
                i = next_i
                continue
            # Unclosed macro — fall through to paragraph handling.
        # Blockquote line
        if _line_is_blockquote(line):
            bq, next_i = _consume_blockquote_line(lines, i, ctx)
            blocks.append(bq)
            i = next_i
            continue
        # List
        if _line_is_list(line):
            list_block, next_i = _consume_list(lines, i, ctx)
            blocks.append(list_block)
            i = next_i
            continue
        # Table
        if _line_is_table_row(line):
            table, next_i = _consume_table(lines, i, ctx)
            blocks.append(table)
            i = next_i
            continue
        # Paragraph (default)
        para, next_i = _consume_paragraph(lines, i, ctx)
        blocks.append(para)
        i = next_i
    return blocks


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def parse_jira_wiki(
    source: str,
) -> tuple[Document, list[str], tuple[str, ...]]:
    """Parse a Jira wiki markup string into a cfxmark AST.

    :param source: Jira wiki markup text. Empty / ``None`` inputs
        produce an empty :class:`Document` without raising.
    :returns: ``(document, warnings, attachments)``. ``attachments``
        is a tuple of filenames referenced via ``[^file]`` or
        ``!file!`` syntax, in document order and deduplicated, so
        the caller can fetch them out-of-band.
    """

    if not source:
        return Document(children=()), [], ()
    ctx = _ParseContext()
    try:
        blocks = _parse_blocks(source, ctx)
    except Exception as exc:  # pragma: no cover — defensive
        raise ParseError(f"failed to parse jira wiki markup: {exc}") from exc
    document = Document(children=tuple(blocks))
    return document, ctx.warnings, tuple(ctx.attachments)


__all__ = ["parse_jira_wiki"]
