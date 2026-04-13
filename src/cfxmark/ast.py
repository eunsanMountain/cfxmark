"""Internal document AST shared by parsers and renderers.

The AST is deliberately **semantic**, not concrete — `**bold**` and
`__bold__` both map to ``Strong``. Round-trip stability is achieved by
defining a canonical form (see :mod:`cfxmark.normalize`), not by
preserving surface syntax.

The nodes are immutable frozen dataclasses for easier hashing and
equality comparison during tests. Only the list-valued ``children``
field is wrapped in a tuple conversion via ``__post_init__`` to allow
callers to pass lists for ergonomics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

# ---------------------------------------------------------------------------
# Base node
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Node:
    """Abstract base for all AST nodes."""


# ---------------------------------------------------------------------------
# Inline nodes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Text(Node):
    """A literal run of text. HTML entities are already decoded."""

    content: str


@dataclass(frozen=True)
class SoftBreak(Node):
    """A newline within a paragraph that renders as a space (CommonMark)."""


@dataclass(frozen=True)
class HardBreak(Node):
    """A forced line break (`<br/>` / `  \\n` / `\\\\\\n`)."""


@dataclass(frozen=True)
class Emphasis(Node):
    """Italic text (``*x*`` / ``_x_`` / ``<em>x</em>``)."""

    children: tuple[InlineNode, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Strong(Node):
    """Bold text (``**x**`` / ``__x__`` / ``<strong>x</strong>``)."""

    children: tuple[InlineNode, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Strikethrough(Node):
    """Strikethrough text (``~~x~~`` / ``<s>x</s>`` / ``<del>x</del>``)."""

    children: tuple[InlineNode, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class InlineCode(Node):
    """Inline code span (`` `x` `` / ``<code>x</code>``)."""

    content: str


@dataclass(frozen=True)
class Link(Node):
    """Hyperlink (``[text](url)`` / ``<a href>``)."""

    url: str
    title: str | None = None
    children: tuple[InlineNode, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Image(Node):
    """Image reference.

    ``src`` is either an absolute URL (rendered as ``<ri:url>``) or a
    local file reference (rendered as ``<ri:attachment>``). The library
    does not upload attachments — the caller owns that.
    """

    src: str
    alt: str = ""
    title: str | None = None
    width: int | None = None
    height: int | None = None


@dataclass(frozen=True)
class Subscript(Node):
    """Subscript text (Jira wiki ``~x~`` / HTML ``<sub>x</sub>``)."""

    children: tuple[InlineNode, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Superscript(Node):
    """Superscript text (Jira wiki ``^x^`` / HTML ``<sup>x</sup>``)."""

    children: tuple[InlineNode, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Underline(Node):
    """Underline/insert text (Jira wiki ``+x+`` / HTML ``<ins>x</ins>``)."""

    children: tuple[InlineNode, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ColorSpan(Node):
    """Colored text (Jira wiki ``{color:red}text{color}``)."""

    color: str
    children: tuple[InlineNode, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Citation(Node):
    """Citation text (Jira wiki ``??text??`` / HTML ``<cite>text</cite>``)."""

    children: tuple[InlineNode, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class InlineOpaque(Node):
    """A Confluence fragment preserved verbatim in inline position.

    Used for things like inline ``<ac:link>`` user mentions or inline
    ``<ac:structured-macro>`` references that have no Markdown
    equivalent. The Markdown renderer emits a short
    ``[label](cfx:op-<hash>)`` link and stores the XML payload in the
    ``cfxmark:payloads`` sidecar section at the bottom of the
    document; the parser reverses both halves and reconstructs this
    node.

    ``label`` is a short human-friendly hint (e.g. ``@user`` or
    ``jira:PROJ-1``). It has no semantic role at round-trip time —
    the SHA-256 of ``raw_xml`` is the authoritative identity.
    """

    raw_xml: str
    opaque_id: str
    label: str


InlineNode = (
    Text
    | SoftBreak
    | HardBreak
    | Emphasis
    | Strong
    | Strikethrough
    | Subscript
    | Superscript
    | Underline
    | ColorSpan
    | Citation
    | InlineCode
    | Link
    | Image
    | InlineOpaque
)


# ---------------------------------------------------------------------------
# Block nodes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Heading(Node):
    """An ATX heading at level 1-6."""

    level: int
    children: tuple[InlineNode, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Paragraph(Node):
    """A block of inline content."""

    children: tuple[InlineNode, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class CodeBlock(Node):
    """A fenced code block with an optional language tag.

    Both ``<pre><code>`` and ``<ac:structured-macro ac:name="code">``
    from Confluence map to this node.
    """

    content: str
    language: str | None = None


@dataclass(frozen=True)
class BlockQuote(Node):
    """A block quote containing block children."""

    children: tuple[BlockNode, ...] = field(default_factory=tuple)


class ListType(Enum):
    """Whether a list is bulleted or numbered."""

    BULLET = "bullet"
    ORDERED = "ordered"


@dataclass(frozen=True)
class List(Node):
    """A bullet or ordered list."""

    list_type: ListType
    items: tuple[ListItem, ...] = field(default_factory=tuple)
    start: int = 1


@dataclass(frozen=True)
class ListItem(Node):
    """A single list entry. Children are block-level."""

    children: tuple[BlockNode, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class HorizontalRule(Node):
    """A thematic break (``---`` / ``<hr/>``)."""


class CellType(Enum):
    """Table cell role."""

    HEADER = "header"
    DATA = "data"


class CellAlign(Enum):
    """Per-column alignment."""

    LEFT = "left"
    CENTER = "center"
    RIGHT = "right"
    NONE = "none"


@dataclass(frozen=True)
class TableCell(Node):
    """A single table cell.

    The children are inline (GFM / Confluence simple table conventions).
    ``colspan`` and ``rowspan`` default to 1 (no merge). When greater
    than 1 the cell occupies multiple columns or rows; the renderer
    expands the row to the wide form using MultiMarkdown's ``<`` /
    ``^`` continuation markers, and the parser collapses them back.
    """

    kind: CellType
    children: tuple[InlineNode, ...] = field(default_factory=tuple)
    align: CellAlign = CellAlign.NONE
    colspan: int = 1
    rowspan: int = 1


@dataclass(frozen=True)
class TableRow(Node):
    """A single table row."""

    cells: tuple[TableCell, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Table(Node):
    """A simple GFM-compatible table with a header row + body rows."""

    header: TableRow | None
    body: tuple[TableRow, ...] = field(default_factory=tuple)
    alignments: tuple[CellAlign, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class DirectiveMacro(Node):
    """A Confluence macro we know how to translate to a Markdown directive.

    The ``name`` matches a :class:`cfxmark.macros.MacroRegistry` entry.
    ``parameters`` carries ``ac:parameter`` values. ``body`` is either
    ``None`` (for empty-body macros like ``toc``) or a block sequence
    (for ``rich-text-body`` macros like ``info`` or ``expand``).
    """

    name: str
    parameters: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    body: tuple[BlockNode, ...] | None = None


@dataclass(frozen=True)
class OpaqueBlock(Node):
    """A block-level Confluence fragment we preserve verbatim.

    The ``raw_xml`` field contains the serialized XML of the element as
    it appeared in the source, including any ``ac:macro-id`` or other
    volatile attributes. Round-trip push to Confluence will re-use the
    same ``macro-id``, so Confluence treats it as the same macro
    instance.

    ``opaque_id`` is a short, stable identifier assigned at parse time
    so the markdown serialization can label the fence unambiguously.
    """

    raw_xml: str
    opaque_id: str


@dataclass(frozen=True)
class PassthroughComment(Node):
    """A HTML comment that lives on the Markdown side only.

    Used for caller-owned metadata blocks like
    ``<!-- workflow:meta ... -->`` which must survive a Markdown
    round-trip but must NOT be serialized to Confluence or Jira (they
    are not document content). The Markdown parser captures them when
    the caller opts in via
    :attr:`ConversionOptions.passthrough_html_comment_prefixes`; the
    Markdown renderer emits them verbatim; CFX and Jira wiki renderers
    drop them silently.

    ``content`` is the full comment text including the ``<!--`` and
    ``-->`` sentinels, so the renderer can echo it byte-for-byte.
    """

    content: str


BlockNode = (
    Heading
    | Paragraph
    | CodeBlock
    | BlockQuote
    | List
    | ListItem
    | HorizontalRule
    | Table
    | DirectiveMacro
    | OpaqueBlock
    | PassthroughComment
)


# ---------------------------------------------------------------------------
# Document root
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Document(Node):
    """The top-level document node."""

    children: tuple[BlockNode, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Union type for anything
# ---------------------------------------------------------------------------

AnyNode = Document | BlockNode | InlineNode


__all__ = [
    "Node",
    "Text",
    "SoftBreak",
    "HardBreak",
    "Emphasis",
    "Strong",
    "Strikethrough",
    "Subscript",
    "Superscript",
    "Underline",
    "ColorSpan",
    "Citation",
    "InlineCode",
    "Link",
    "Image",
    "InlineOpaque",
    "InlineNode",
    "Heading",
    "Paragraph",
    "CodeBlock",
    "BlockQuote",
    "ListType",
    "List",
    "ListItem",
    "HorizontalRule",
    "CellType",
    "CellAlign",
    "TableCell",
    "TableRow",
    "Table",
    "DirectiveMacro",
    "OpaqueBlock",
    "PassthroughComment",
    "BlockNode",
    "Document",
    "AnyNode",
]
