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
from typing import Literal

# ---------------------------------------------------------------------------
# Base node
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Node:
    """Abstract base for all AST nodes."""

    source_path: tuple[str, ...] = field(default=(), kw_only=True, compare=False, repr=False)


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
    href_present: bool = True
    title: str | None = None
    children: tuple[InlineNode, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Image(Node):
    """Image reference.

    ``src`` is either an absolute URL (rendered as ``<ri:url>``) or a
    local file reference (rendered as ``<ri:attachment>``). A typed
    ``attachment_filename`` preserves a source attachment's flat Confluence
    identity without interpreting filename punctuation as URL syntax. The
    library does not upload attachments — the caller owns that.
    """

    src: str
    attachment_filename: str | None = None
    alt: str = ""
    title: str | None = None
    width: int | None = None
    height: int | None = None
    thumbnail: bool = False
    align: str | None = None


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
    background: bool = False


@dataclass(frozen=True)
class AlignedSpan(Node):
    """Inline segment with paragraph-level center or right alignment."""

    align: str
    children: tuple[InlineNode, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Citation(Node):
    """Citation text (Jira wiki ``??text??`` / HTML ``<cite>text</cite>``)."""

    children: tuple[InlineNode, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class InlineDirectiveMacro(Node):
    """A known Confluence macro represented in inline Markdown."""

    name: str
    parameters: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    # Remote identity that the inline Markdown projection cannot carry. A
    # freshly authored macro keeps ``schema_version == "1"``; a macro parsed
    # from remote storage records the exact source values (``None`` when the
    # source omitted ``ac:schema-version``) so regeneration matches byte for
    # byte and its body/parameters stay editable.
    macro_id: str | None = None
    macro_schema_version: str | None = "1"


@dataclass(frozen=True)
class UnsupportedMarker(Node):
    """Local marker for Confluence content omitted from editable Markdown."""

    kind: str
    macro: str | None = None
    occurrence_id: str | None = None


@dataclass(frozen=True)
class InlineOpaque(Node):
    """A Confluence fragment preserved verbatim in inline position.

    Used for things like inline ``<ac:link>`` user mentions or unsupported
    inline ``<ac:structured-macro>`` references. The Markdown renderer emits a short
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
    | AlignedSpan
    | Citation
    | InlineCode
    | Link
    | Image
    | InlineDirectiveMacro
    | UnsupportedMarker
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
    align: str | None = None
    #: The source storage paragraph was the exact bare form ``<p/>`` (or the
    #: XML-equivalent ``<p></p>``), not ``<p><br/></p>``.
    #:
    #: Confluence renders the two forms differently. Markdown therefore needs
    #: separate spellings or an unrelated edit silently rewrites untouched page
    #: spacing. Only an attribute-free, child-free paragraph may set this flag;
    #: paragraphs carrying style, comments, or other metadata remain on the
    #: diagnostic path.
    bare_empty: bool = False


@dataclass(frozen=True)
class CodeBlock(Node):
    """A fenced code block with an optional language tag.

    Both ``<pre><code>`` and ``<ac:structured-macro ac:name="code">``
    from Confluence map to this node.
    """

    content: str
    language: str | None = None
    macro_id: str | None = None
    macro_schema_version: str | None = None


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
    #: CommonMark's tight/loose distinction, carried rather than re-derived.
    #: A loose list wraps each item's content in a paragraph and a tight one
    #: does not, and storage records the difference: ``<li><p>x</p></li>`` and
    #: ``<li>x</li>`` are stored and rendered differently by the server. Without
    #: this field both spell as ``- x`` and the wrapper is lost on the way back,
    #: which changed list items nobody had edited.
    loose: bool = False


@dataclass(frozen=True)
class ListItem(Node):
    """A single list entry. Children are block-level."""

    children: tuple[BlockNode, ...] = field(default_factory=tuple)
    #: Whether *this item* was wrapped in a paragraph in storage.
    #:
    #: CommonMark has no spelling for "these items are wrapped and that one is not", and
    #: storage holds such lists anyway. `List.loose` stays the list-level property the
    #: format defines; this records what the item actually was, so a mixed list can be
    #: reproduced instead of flattened. Only a mixed list needs it -- a uniform one is
    #: fully described by `List.loose`.
    loose: bool = False


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


ColorSourceEncoding = Literal["data-highlight-colour", "css-background-color", "bgcolor"]
ColorValueKind = Literal["named", "hex", "rgb", "rgba", "design-token"]
RenderStrategy = Literal["highlight-attribute", "style-background-color"]


@dataclass(frozen=True)
class CellBackground:
    """Validated table-cell background presentation.

    Parsing a value and proving that a Confluence server can render it are
    deliberately separate.  ``capability_id`` and ``render_strategy`` stay
    ``None`` until a versioned server read-back capability proves the pair.
    """

    exact: str
    equality_key: str
    source_encoding: ColorSourceEncoding
    value_kind: ColorValueKind
    semantic: str | None = None
    capability_id: str | None = None
    render_strategy: RenderStrategy | None = None


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
    source_style: str | None = None
    colspan: int = 1
    rowspan: int = 1
    background: CellBackground | None = None


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
    # Remote identity the Markdown projection cannot carry. A freshly authored
    # ``::: expand`` keeps ``schema_version == "1"``; a macro parsed from remote
    # storage records the exact source values (``None`` when the source omitted
    # ``ac:schema-version``) so regeneration matches byte for byte and the body
    # and parameters stay editable rather than becoming a preserve-only island.
    macro_id: str | None = None
    macro_schema_version: str | None = "1"


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
    | UnsupportedMarker
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
    "InlineDirectiveMacro",
    "UnsupportedMarker",
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
    "CellBackground",
    "ColorSourceEncoding",
    "ColorValueKind",
    "RenderStrategy",
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


#: Written before a list whose storage shape CommonMark cannot spell, and read
#: back by the Markdown parser. Defined here so the parser and the renderer
#: share one spelling rather than importing each other.
TIGHT_LIST_MARKER = "<!-- cfxmark:list tight -->"

#: The mirror of the above, for a loose list CommonMark would read back tight.
#: A one-item list has no blank line *between* items to be loose with.
LOOSE_LIST_MARKER = "<!-- cfxmark:list loose -->"

#: Exact control spelling for an attribute-free, child-free storage ``<p/>``.
#:
#: The marker travels with the paragraph instead of storing a document-wide
#: ordinal, so inserting another blank elsewhere cannot shift restoration onto
#: the wrong block.
BLANK_PARAGRAPH_MARKER = "<!-- cfxmark:blank -->"

#: The third spelling, for a list whose items disagree.
#:
#: `<!-- cfxmark:list loose=0,2 -->` names which items storage had wrapped. The two markers
#: above already invent spellings for shapes CommonMark cannot express; this is the same
#: device for the case where the shape varies per item, which the parser previously had to
#: flatten. Written by a pull, never by an author.
MIXED_LIST_MARKER_PREFIX = "<!-- cfxmark:list loose="


def mixed_list_marker(indices: tuple[int, ...]) -> str:
    return f"{MIXED_LIST_MARKER_PREFIX}{','.join(str(index) for index in indices)} -->"


def parse_mixed_list_marker(line: str) -> tuple[int, ...] | None:
    """The item indices a mixed marker names, or `None` if this is not one."""

    text = line.strip()
    if not text.startswith(MIXED_LIST_MARKER_PREFIX) or not text.endswith("-->"):
        return None
    body = text[len(MIXED_LIST_MARKER_PREFIX) : -len("-->")].strip()
    if not body:
        return None
    try:
        return tuple(int(part) for part in body.split(",") if part != "")
    except ValueError:
        return None
