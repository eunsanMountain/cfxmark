"""cfxmark AST → Jira wiki markup renderer."""
from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field

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
    ListType,
    OpaqueBlock,
    Paragraph,
    SoftBreak,
    Strikethrough,
    Strong,
    Table,
    Text,
)

_log = logging.getLogger("cfxmark.renderers.jira_wiki")

_NATIVE_ADMONITIONS = {"info", "note", "warning", "tip"}

# Single-pass escape: every match is replaced independently, so the
# backslash-first ordering issue from a sequential .replace() chain
# disappears.
_ESCAPE_RE = re.compile(r"[\\*_{[|]")

# Constants for `JiraWikiContext.dropped_counts` keys — typo-as-key
# silently bypasses `_DROPPED_LABELS` lookups, so go through the
# constants instead of bare string literals.
_DROP_TABLE = "table"
_DROP_BLOCKQUOTE = "blockquote"
_DROP_HARD_BREAK = "hard_break"
_DROP_OPAQUE_BLOCK = "opaque_block"
_DROP_INLINE_OPAQUE = "inline_opaque"

_DROPPED_LABELS: dict[str, str] = {
    _DROP_TABLE: "<table>",
    _DROP_BLOCKQUOTE: "<blockquote>",
    _DROP_HARD_BREAK: "<br>",
    _DROP_OPAQUE_BLOCK: "<opaque>",
    _DROP_INLINE_OPAQUE: "<inline opaque>",
}

# Markdown headings collapse one level (H3→h2, H4→h3, …) so a section
# subheading authored as H3 surfaces as the top-level Jira section
# after the page-title H2 is stripped. H1 and H2 stay as h1/h2.
_HEADING_PROMOTION: dict[int, int] = {1: 1, 2: 2, 3: 2, 4: 3, 5: 4, 6: 5}


@dataclass
class JiraWikiContext:
    warnings: list[str] = field(default_factory=list)
    dropped_counts: Counter[str] = field(default_factory=Counter)


def _escape_wiki(content: str) -> str:
    return _ESCAPE_RE.sub(lambda m: "\\" + m.group(0), content)


def _inline_text(nodes: tuple[InlineNode, ...]) -> str:
    """Return flattened plain text content for heading/section comparisons."""
    parts: list[str] = []
    for n in nodes:
        if isinstance(n, Text):
            parts.append(n.content)
        elif isinstance(n, (Emphasis, Strong, Strikethrough, Link)):
            parts.append(_inline_text(n.children))
        elif isinstance(n, InlineCode):
            parts.append(n.content)
        elif isinstance(n, (SoftBreak, HardBreak)):
            parts.append(" ")
        # Image, InlineOpaque contribute nothing textual
    return "".join(parts)


def _render_inline(nodes: tuple[InlineNode, ...], ctx: JiraWikiContext) -> str:
    buf: list[str] = []
    for n in nodes:
        if isinstance(n, Text):
            buf.append(_escape_wiki(n.content))
        elif isinstance(n, SoftBreak):
            buf.append(" ")
        elif isinstance(n, HardBreak):
            ctx.dropped_counts[_DROP_HARD_BREAK] += 1
        elif isinstance(n, Strong):
            buf.append("*" + _render_inline(n.children, ctx) + "*")
        elif isinstance(n, Emphasis):
            buf.append("_" + _render_inline(n.children, ctx) + "_")
        elif isinstance(n, Strikethrough):
            buf.append("-" + _render_inline(n.children, ctx) + "-")
        elif isinstance(n, InlineCode):
            buf.append("{{" + n.content + "}}")
        elif isinstance(n, Link):
            inner = _render_inline(n.children, ctx)
            buf.append(f"[{inner}|{n.url}]" if inner else f"[{n.url}]")
        elif isinstance(n, Image):
            basename = n.src.rsplit("/", 1)[-1]
            if n.alt:
                buf.append(f"!{basename}|alt={_escape_wiki(n.alt)}!")
            else:
                buf.append(f"!{basename}!")
        elif isinstance(n, InlineOpaque):
            buf.append(f"(cfx:{n.label})")
            ctx.dropped_counts[_DROP_INLINE_OPAQUE] += 1
    return "".join(buf)


def _render_list(node: List, ctx: JiraWikiContext, marker_prefix: str = "") -> str:
    """Render a list with ancestor-chain marker concatenation.

    Each nesting level prepends its parent's marker character so that:
    - bullet           → ``*``
    - bullet > bullet  → ``**``
    - ordered > bullet → ``#*``
    - bullet > ordered → ``*#``
    """
    marker_char = "*" if node.list_type == ListType.BULLET else "#"
    marker = marker_prefix + marker_char
    lines: list[str] = []
    for item in node.items:
        if not item.children:
            lines.append(marker + " ")
            continue
        rendered_inline = ""
        rendered_nested: list[str] = []
        for child in item.children:
            if isinstance(child, Paragraph):
                text = _render_inline(child.children, ctx)
                if not rendered_inline:
                    rendered_inline = text
                else:
                    rendered_nested.append(marker + " " + text)
            elif isinstance(child, List):
                rendered_nested.append(_render_list(child, ctx, marker_prefix=marker))
            else:
                rendered_nested.append(_render_block(child, ctx))
        lines.append(marker + " " + rendered_inline)
        lines.extend(r for r in rendered_nested if r)
    return "\n".join(lines)


def _render_block(node: BlockNode, ctx: JiraWikiContext) -> str:
    if isinstance(node, Heading):
        # H3 collapses into h2 (and H4→h3, …) so a "Story Summary"
        # subsection (typically authored as H3 under an H2 page title)
        # surfaces as a top-level Jira section after the renderer
        # strips the page-title H2.
        promoted = _HEADING_PROMOTION[node.level]
        return f"h{promoted}. {_render_inline(node.children, ctx)}"
    if isinstance(node, Paragraph):
        return _render_inline(node.children, ctx)
    if isinstance(node, CodeBlock):
        fence_open = f"{{code:{node.language}}}" if node.language else "{code}"
        content = node.content.rstrip("\n")
        if "{code}" in content:
            ctx.warnings.append(
                "CodeBlock content contains '{code}' literal — Jira wiki cannot "
                "express this without premature termination. Content may be truncated."
            )
        return f"{fence_open}\n{content}\n{{code}}"
    if isinstance(node, BlockQuote):
        ctx.dropped_counts[_DROP_BLOCKQUOTE] += 1
        return ""
    if isinstance(node, List):
        return _render_list(node, ctx)
    if isinstance(node, HorizontalRule):
        return "----"
    if isinstance(node, Table):
        ctx.dropped_counts[_DROP_TABLE] += 1
        return ""
    if isinstance(node, DirectiveMacro):
        body_blocks = node.body or ()
        body_text = "\n\n".join(filter(None, (_render_block(b, ctx) for b in body_blocks)))
        if node.name in _NATIVE_ADMONITIONS:
            return f"{{{node.name}}}\n{body_text}\n{{{node.name}}}"
        ctx.warnings.append(f"unknown directive macro {node.name!r}: body inlined")
        return body_text
    if isinstance(node, OpaqueBlock):
        ctx.dropped_counts[_DROP_OPAQUE_BLOCK] += 1
        return ""
    return ""  # unreachable for well-formed AST


def _slice_section(document: Document, section_name: str) -> Document | None:
    """Find the first H2 whose text equals ``section_name`` and collect
    blocks until the next H2 / HorizontalRule / EOF."""
    start_idx: int | None = None
    for i, child in enumerate(document.children):
        if (
            isinstance(child, Heading)
            and child.level == 2
            and _inline_text(child.children).strip() == section_name
        ):
            start_idx = i + 1
            break
    if start_idx is None:
        return None
    collected: list[BlockNode] = []
    for child in document.children[start_idx:]:
        if isinstance(child, Heading) and child.level == 2:
            break
        if isinstance(child, HorizontalRule):
            break
        collected.append(child)
    return Document(children=tuple(collected))


def _drop_leading(doc: Document, patterns: tuple[re.Pattern[str], ...]) -> Document:
    """If the first block is a Paragraph whose flattened text matches any
    pattern, remove it."""
    if not doc.children:
        return doc
    first = doc.children[0]
    if not isinstance(first, Paragraph):
        return doc
    flat = _inline_text(first.children)
    if any(p.search(flat) for p in patterns):
        return Document(children=doc.children[1:])
    return doc


def _finalize_warnings(ctx: JiraWikiContext) -> None:
    for key, count in ctx.dropped_counts.items():
        label = _DROPPED_LABELS.get(key, key)
        ctx.warnings.append(f"{label}x{count} will be dropped")


def render_jira_wiki(
    document: Document,
    *,
    section: str | None = None,
    drop_leading_notice: tuple[re.Pattern[str], ...] = (),
) -> tuple[str | None, list[str]]:
    """Render *document* to Jira wiki markup.

    :param section: If set, only the content of the first H2 section
        whose title equals this string is rendered. Returns ``None`` if
        the section is not found.
    :param drop_leading_notice: If the first block of the target
        document is a paragraph whose flattened text matches any pattern
        in this tuple, that paragraph is silently removed.
    :returns: ``(body, warnings)`` where ``body`` is the rendered Jira
        wiki string (or ``None`` when *section* was not found).
    """
    ctx = JiraWikiContext()
    if section is not None:
        target = _slice_section(document, section)
        if target is None:
            return None, []
    else:
        target = document
    if drop_leading_notice:
        target = _drop_leading(target, drop_leading_notice)
    rendered = [_render_block(child, ctx) for child in target.children]
    rendered = [r for r in rendered if r]
    _finalize_warnings(ctx)
    body = ("\n\n".join(rendered).rstrip() + "\n") if rendered else ""
    return body, ctx.warnings


__all__ = [
    "JiraWikiContext",
    "render_jira_wiki",
]
