"""Markdown → cfxmark AST.

Parsing strategy:

1. **Pre-process** the source to extract two things the stock CommonMark
   parser does not understand:

   * **Opaque blocks** — sentinel-wrapped ``cfx-storage`` fences that
     :mod:`cfxmark.opaque` produces when round-tripping from Confluence.
   * **Directive fences** — pandoc-style ``::: name ...\\n...\\n:::``
     blocks that represent known Confluence macros.

   Each stripped region is replaced with a placeholder paragraph of the
   form ``__CFXMARK_<KIND>_<N>__`` that survives CommonMark parsing
   intact (it is just an ordinary text node).
2. **Parse** the pre-processed source with :mod:`mistletoe` in strict
   CommonMark mode (plus GFM tables and strikethrough).
3. **Walk** the mistletoe tree, converting each token into a cfxmark
   AST node and swapping placeholders back for their original content.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from mistletoe import Document as MdDocument
from mistletoe import block_token, span_token

from cfxmark.ast import (
    BlockNode,
    BlockQuote,
    CellAlign,
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
    TableRow,
    Text,
)
from cfxmark.exceptions import ParseError
from cfxmark.macros import MacroRegistry, default_registry
from cfxmark.opaque import (
    find_opaque_blocks,
    parse_payloads,
    strip_asset_markers,
    strip_header_notice,
    strip_payloads_section,
)

# Enable inline HTML span parsing so our renderer can fall back to
# ``<strong>``/``<em>``/``<code>`` tags when CJK adjacency breaks the
# CommonMark emphasis boundary rules. HtmlSpan tokens are emitted as
# individual open/close runs; we reassemble the pairs in _convert_inline.
_HtmlSpanToken = getattr(span_token, "HtmlSpan", None) or getattr(
    span_token, "HTMLSpan", None
)
if _HtmlSpanToken is not None and _HtmlSpanToken not in span_token._token_types:
    span_token._token_types.insert(0, _HtmlSpanToken)


# ---------------------------------------------------------------------------
# Placeholder scheme
# ---------------------------------------------------------------------------


_OPAQUE_MARKER = "CFXMARK_OPAQUE"
_DIRECTIVE_MARKER = "CFXMARK_DIRECTIVE"
_PASSTHROUGH_MARKER = "CFXMARK_PASSTHROUGH"


def _placeholder(kind: str, index: int) -> str:
    # Wrapped in backticks so mistletoe sees it as inline code and
    # does not apply emphasis / link parsing to the underscores.
    return f"`{kind}-{index}-CFXMARK`"


_PLACEHOLDER_RE = re.compile(
    r"^(CFXMARK_OPAQUE|CFXMARK_DIRECTIVE|CFXMARK_PASSTHROUGH)-(\d+)-CFXMARK$"
)


# ---------------------------------------------------------------------------
# Pre-processing: opaque and directive extraction
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _OpaqueCapture:
    opaque_id: str
    raw_xml: str


@dataclass(frozen=True)
class _DirectiveCapture:
    name: str
    parameters: tuple[tuple[str, str], ...]
    body_source: str  # Raw markdown body, still needs a second parse pass.


@dataclass
class _PreprocessResult:
    source: str
    opaques: list[_OpaqueCapture] = field(default_factory=list)
    directives: list[_DirectiveCapture] = field(default_factory=list)
    passthroughs: list[str] = field(default_factory=list)
    inline_payloads: dict[str, str] = field(default_factory=dict)
    asset_src_map: dict[str, str] = field(default_factory=dict)


_DIRECTIVE_OPEN_RE = re.compile(
    r"^:::\s+(?P<name>[a-zA-Z][a-zA-Z0-9_-]*)(?P<rest>.*)$"
)
_DIRECTIVE_CLOSE_RE = re.compile(r"^:::\s*$")
_DIRECTIVE_PARAM_RE = re.compile(
    r'(?P<key>[a-zA-Z][a-zA-Z0-9_-]*)\s*=\s*"(?P<value>(?:\\.|[^"\\])*)"'
)

# A CommonMark fenced code block opens with at least three backticks
# or tildes at the start of a line (optionally indented up to three
# spaces) and closes with the same character run of equal-or-greater
# length. The directive scanner has to skip over these regions so that
# Markdown showing a literal ``::: info`` example does not get
# rewritten into a real directive.
_FENCE_RE = re.compile(r"^( {0,3})(`{3,}|~{3,})")


def _parse_directive_params(rest: str) -> tuple[tuple[str, str], ...]:
    params: list[tuple[str, str]] = []
    for m in _DIRECTIVE_PARAM_RE.finditer(rest):
        value = m.group("value")
        value = value.replace('\\"', '"').replace("\\\\", "\\")
        params.append((m.group("key"), value))
    return tuple(params)


def _preprocess(
    source: str,
    passthrough_html_comment_prefixes: tuple[str, ...] = (),
) -> _PreprocessResult:
    """Strip opaque blocks and directive fences, substituting placeholders."""

    # 1. Strip the cfxmark header notice if present.
    source = strip_header_notice(source)
    # 2. Extract the inline-opaque payload sidecar (and remove it from
    #    the body so mistletoe never sees it as ordinary content).
    inline_payloads = parse_payloads(source)
    source = strip_payloads_section(source)
    # 3. Extract image asset markers and build a mapping from each
    #    image's *visible* link target to its *original* Confluence
    #    filename, then strip the markers so mistletoe sees clean
    #    image syntax.
    source, asset_src_map = strip_asset_markers(source)

    result = _PreprocessResult(
        source="",
        inline_payloads=inline_payloads,
        asset_src_map=asset_src_map,
    )

    # --- Opaque blocks first: extracted and authenticated by
    # ``find_opaque_blocks``, which only honours sentinels whose id
    # matches the SHA-256 of the body. Any unauthenticated sentinel
    # is left in place to round-trip as ordinary Markdown text.
    opaque_parts: list[str] = []
    cursor = 0
    for match in find_opaque_blocks(source):
        opaque_parts.append(source[cursor : match.start])
        idx = len(result.opaques)
        result.opaques.append(
            _OpaqueCapture(opaque_id=match.opaque_id, raw_xml=match.raw_xml)
        )
        opaque_parts.append(
            "\n\n" + _placeholder(_OPAQUE_MARKER, idx) + "\n\n"
        )
        cursor = match.end
    opaque_parts.append(source[cursor:])
    after_opaque = "".join(opaque_parts)

    # --- Passthrough comment open detector. Built once per call so
    # the line scanner can fast-path when no prefix was registered.
    passthrough_open_re = _build_passthrough_open_re(
        passthrough_html_comment_prefixes
    )

    # --- Directive fences: scan line by line, balanced with a simple
    # depth counter. Nested directives are not supported in v0.1.
    # Lines inside a CommonMark fenced code block are passed through
    # untouched so a literal ``::: info`` example in a code sample is
    # not rewritten into a real directive.
    lines = after_opaque.splitlines(keepends=False)
    out_lines: list[str] = []
    i = 0
    n = len(lines)
    fence_marker: str | None = None
    while i < n:
        line = lines[i]

        if fence_marker is not None:
            # Inside a fenced code block — look for the matching close
            # and pass through verbatim.
            close = _FENCE_RE.match(line)
            if close is not None and close.group(2).startswith(fence_marker[0]) and len(close.group(2)) >= len(fence_marker):
                fence_marker = None
            out_lines.append(line)
            i += 1
            continue

        fence_match = _FENCE_RE.match(line)
        if fence_match is not None:
            fence_marker = fence_match.group(2)
            out_lines.append(line)
            i += 1
            continue

        # Caller-owned HTML comment passthrough (R1). The line must
        # *start* with ``<!--<prefix>`` (after optional whitespace);
        # then we collect lines until one contains ``-->``. This
        # handles both single-line and multi-line comments because
        # mistletoe does not recognize HTML comment blocks at all in
        # the dialects we use, so we must intercept here before
        # mistletoe sees the source.
        if passthrough_open_re is not None:
            open_pt = passthrough_open_re.match(line)
            if open_pt is not None:
                start_line = i
                # Collect lines until we find ``-->``. Greedy across
                # lines but stops at the first ``-->`` per CommonMark
                # HTML comment semantics.
                while i < n and "-->" not in lines[i]:
                    i += 1
                if i < n:
                    end_line = i
                    i += 1  # Consume the closing line.
                    captured = "\n".join(lines[start_line : end_line + 1])
                    # Strip a trailing run of whitespace after ``-->``
                    # so the captured text ends right at the sentinel.
                    arrow_pos = captured.rfind("-->")
                    captured = captured[: arrow_pos + 3]
                    idx = len(result.passthroughs)
                    result.passthroughs.append(captured)
                    out_lines.append("")
                    out_lines.append(
                        _placeholder(_PASSTHROUGH_MARKER, idx)
                    )
                    out_lines.append("")
                    continue
                # Unterminated comment — fall through and let
                # mistletoe deal with it as raw text. The wrapper
                # should not feed unterminated comments anyway.

        open_match = _DIRECTIVE_OPEN_RE.match(line.strip())
        if open_match is None:
            out_lines.append(line)
            i += 1
            continue
        # Found a directive open. Consume until a matching `:::`.
        name = open_match.group("name")
        rest = open_match.group("rest") or ""
        params = _parse_directive_params(rest)
        body_lines: list[str] = []
        i += 1
        while i < n:
            if _DIRECTIVE_CLOSE_RE.match(lines[i].strip()):
                break
            body_lines.append(lines[i])
            i += 1
        i += 1  # Consume the closing `:::`.
        idx = len(result.directives)
        result.directives.append(
            _DirectiveCapture(
                name=name,
                parameters=params,
                body_source="\n".join(body_lines),
            )
        )
        out_lines.append("")
        out_lines.append(_placeholder(_DIRECTIVE_MARKER, idx))
        out_lines.append("")

    result.source = "\n".join(out_lines)
    return result


def _build_passthrough_open_re(
    prefixes: tuple[str, ...],
) -> re.Pattern[str] | None:
    """Build a regex matching the *opening line* of an HTML comment
    whose first non-whitespace token starts with one of ``prefixes``.

    Returns ``None`` when no eligible prefix is configured. ``cfxmark:``
    prefixes are filtered out so cfxmark's own sentinel comments
    cannot be hijacked into a passthrough capture.
    """

    safe = tuple(p for p in prefixes if not p.startswith("cfxmark:"))
    if not safe:
        return None
    alternation = "|".join(re.escape(p) for p in safe)
    return re.compile(r"^[ \t]*<!--\s*(?:" + alternation + r")")


# ---------------------------------------------------------------------------
# mistletoe walker
# ---------------------------------------------------------------------------


class _MdConverter:
    def __init__(
        self,
        pre: _PreprocessResult,
        registry: MacroRegistry,
        passthrough_html_comment_prefixes: tuple[str, ...] = (),
    ) -> None:
        self.pre = pre
        self.registry = registry
        self.warnings: list[str] = []
        self.passthrough_html_comment_prefixes = passthrough_html_comment_prefixes

    # ---- block-level ------------------------------------------------------

    def convert_document(self, tok: MdDocument) -> Document:
        blocks: list[BlockNode] = []
        for child in tok.children or []:
            for b in self._convert_block(child):
                blocks.append(b)
        return Document(children=tuple(blocks))

    def _convert_block(self, tok: block_token.BlockToken) -> list[BlockNode]:
        if isinstance(tok, block_token.Heading):
            inline = self._convert_inline_children(tok)
            return [Heading(level=tok.level, children=inline)]
        if isinstance(tok, block_token.SetextHeading):
            level = tok.level
            inline = self._convert_inline_children(tok)
            return [Heading(level=level, children=inline)]
        if isinstance(tok, block_token.Paragraph):
            return self._convert_paragraph(tok)
        if isinstance(tok, block_token.CodeFence):
            lang = tok.language or None
            return [CodeBlock(content=tok.content, language=lang or None)]
        if isinstance(tok, block_token.BlockCode):
            return [CodeBlock(content=tok.content, language=None)]
        if isinstance(tok, block_token.Quote):
            inner = self._convert_blocks(tok.children or [])
            return [BlockQuote(children=tuple(inner))]
        if isinstance(tok, block_token.List):
            return [self._convert_list(tok)]
        if isinstance(tok, block_token.ThematicBreak):
            return [HorizontalRule()]
        if isinstance(tok, block_token.Table):
            return [self._convert_table(tok)]
        html_block_cls = getattr(block_token, "HTMLBlock", None) or block_token.HtmlBlock
        if isinstance(tok, html_block_cls):
            return self._convert_html_block(tok)
        # Unknown block: drop with warning.
        self.warnings.append(
            f"unsupported block token {type(tok).__name__!s} dropped"
        )
        return []

    def _convert_blocks(
        self, children: list[block_token.BlockToken]
    ) -> list[BlockNode]:
        result: list[BlockNode] = []
        for c in children:
            result.extend(self._convert_block(c))
        return result

    def _parse_inner_body(self, source: str) -> tuple[BlockNode, ...]:
        """Parse a directive body, sharing this converter's preprocess
        state so that opaque-block placeholders captured by the outer
        document still resolve correctly.

        We deliberately do **not** call ``parse_md`` recursively here:
        a recursive call would build a fresh ``_PreprocessResult``
        whose ``opaques`` list is empty, and any
        ``CFXMARK_OPAQUE_<n>`` placeholder embedded in the body would
        index into that empty list and crash.
        """

        md_doc = MdDocument(source)
        # Hand the inner walker the *outer* preprocess state so
        # placeholder lookups continue to work.
        inner = _MdConverter(
            pre=self.pre,
            registry=self.registry,
            passthrough_html_comment_prefixes=(
                self.passthrough_html_comment_prefixes
            ),
        )
        document = inner.convert_document(md_doc)
        self.warnings.extend(inner.warnings)
        return document.children

    def _convert_paragraph(self, tok: block_token.Paragraph) -> list[BlockNode]:
        inline = self._convert_inline_children(tok)
        # Placeholder substitution: a paragraph whose inline content is
        # exactly one inline-code span matching a placeholder marker
        # is replaced with the stored opaque / directive / passthrough
        # content.
        if len(inline) == 1 and isinstance(inline[0], InlineCode):
            m = _PLACEHOLDER_RE.match(inline[0].content.strip())
            if m:
                kind = m.group(1)
                idx = int(m.group(2))
                if kind == _OPAQUE_MARKER and 0 <= idx < len(self.pre.opaques):
                    op_cap = self.pre.opaques[idx]
                    return [
                        OpaqueBlock(
                            raw_xml=op_cap.raw_xml,
                            opaque_id=op_cap.opaque_id,
                        )
                    ]
                if kind == _DIRECTIVE_MARKER and 0 <= idx < len(
                    self.pre.directives
                ):
                    dir_cap = self.pre.directives[idx]
                    body_blocks = self._parse_inner_body(dir_cap.body_source)
                    return [
                        DirectiveMacro(
                            name=dir_cap.name,
                            parameters=dir_cap.parameters,
                            body=body_blocks if body_blocks else None,
                        )
                    ]
                if kind == _PASSTHROUGH_MARKER and 0 <= idx < len(
                    self.pre.passthroughs
                ):
                    return [
                        PassthroughComment(
                            content=self.pre.passthroughs[idx]
                        )
                    ]
        return [Paragraph(children=inline)]

    def _convert_list(self, tok: block_token.List) -> List:
        items: list[ListItem] = []
        is_ordered = bool(getattr(tok, "start", None) is not None or getattr(tok, "start_at", None))
        start = 1
        for attr in ("start", "start_at"):
            val = getattr(tok, attr, None)
            if isinstance(val, int):
                start = val
                is_ordered = True
                break
        loose = getattr(tok, "loose", True)
        for item in tok.children or []:
            if not isinstance(item, block_token.ListItem):
                continue
            blocks = self._convert_blocks(item.children or [])
            items.append(ListItem(children=tuple(blocks)))
        list_type = ListType.ORDERED if is_ordered else ListType.BULLET
        return List(list_type=list_type, items=tuple(items), start=start)

    def _convert_table(self, tok: block_token.Table) -> Table:
        alignments = [_convert_align(a) for a in getattr(tok, "column_align", []) or []]
        header_row: TableRow | None = None
        header = getattr(tok, "header", None)
        if header is not None:
            header_row = self._convert_table_row(header, alignments, is_header=True)
        body: list[TableRow] = []
        for row in tok.children or []:
            if row is header:
                continue
            body.append(self._convert_table_row(row, alignments, is_header=False))

        all_rows = ([header_row] if header_row is not None else []) + body
        merged_rows = _collapse_span_markers(all_rows)
        if header_row is not None:
            new_header: TableRow | None = merged_rows[0]
            new_body = tuple(merged_rows[1:])
        else:
            new_header = None
            new_body = tuple(merged_rows)
        return Table(
            header=new_header,
            body=new_body,
            alignments=tuple(alignments),
        )

    def _convert_table_row(
        self,
        tok: block_token.TableRow,
        alignments: list[CellAlign],
        is_header: bool,
    ) -> TableRow:
        cells: list[TableCell] = []
        for i, cell in enumerate(tok.children or []):
            kind = CellType.HEADER if is_header else CellType.DATA
            align = alignments[i] if i < len(alignments) else CellAlign.NONE
            inline = self._convert_inline_children(cell)
            cells.append(TableCell(kind=kind, children=inline, align=align))
        return TableRow(cells=tuple(cells))

    def _convert_html_block(
        self, tok: block_token.BlockToken
    ) -> list[BlockNode]:
        content = getattr(tok, "content", "") or ""
        # Lingering opaque sentinels that slipped past pre-processing
        # could land here; in that case we silently drop with a warning.
        if "cfxmark:opaque" in content:
            self.warnings.append(
                "HTML block contained a cfxmark opaque sentinel that was "
                "not captured in pre-processing"
            )
            return []
        self.warnings.append(
            "HTML block dropped — Confluence does not preserve inline HTML "
            "outside of supported macros"
        )
        return []

    # ---- inline -----------------------------------------------------------

    def _convert_inline_children(
        self, tok: block_token.BlockToken
    ) -> tuple[InlineNode, ...]:
        # Walk the flat span-token sequence, grouping HTML open/close
        # tag pairs we know about into their corresponding AST nodes.
        raw_tokens = list(tok.children or [])
        converted, _ = self._convert_span_sequence(raw_tokens, 0, None)
        return tuple(converted)

    # Tag → AST constructor for HTML fallback.
    _HTML_TAG_NODES: dict[str, type] = {
        "strong": Strong,
        "em": Emphasis,
        "b": Strong,
        "i": Emphasis,
        "del": Strikethrough,
        "s": Strikethrough,
    }

    def _convert_span_sequence(
        self,
        tokens: list[span_token.SpanToken],
        start: int,
        close_tag: str | None,
    ) -> tuple[list[InlineNode], int]:
        """Consume span tokens until ``close_tag`` is seen or the list ends.

        Returns ``(nodes, index_of_next_token_after_the_close)``.
        """

        out: list[InlineNode] = []
        i = start
        n = len(tokens)
        while i < n:
            raw = tokens[i]
            html_span = _html_span_match(raw)
            if html_span is not None:
                kind, tag = html_span
                if kind == "close" and tag == close_tag:
                    return out, i + 1
                if kind == "open" and tag in self._HTML_TAG_NODES:
                    inner, next_i = self._convert_span_sequence(
                        tokens, i + 1, tag
                    )
                    node_cls = self._HTML_TAG_NODES[tag]
                    out.append(node_cls(children=tuple(inner)))
                    i = next_i
                    continue
                if kind == "self" and tag == "br":
                    out.append(HardBreak())
                    i += 1
                    continue
                # Unknown HTML span — drop with warning (same policy as
                # the non-span HTMLSpan handler).
                self.warnings.append(
                    f"inline HTML tag <{tag}> dropped — no equivalent in Confluence"
                )
                i += 1
                continue
            out.extend(self._convert_inline(raw))
            i += 1
        return out, i

    def _convert_inline(
        self, tok: span_token.SpanToken
    ) -> list[InlineNode]:
        if isinstance(tok, span_token.RawText):
            return [Text(content=tok.content)]
        if isinstance(tok, span_token.EscapeSequence):
            return [Text(content=tok.children[0].content if tok.children else "")]
        if isinstance(tok, span_token.LineBreak):
            soft = getattr(tok, "soft", False)
            return [SoftBreak() if soft else HardBreak()]
        if isinstance(tok, span_token.Strong):
            return [Strong(children=self._convert_inline_children(tok))]
        if isinstance(tok, span_token.Emphasis):
            return [Emphasis(children=self._convert_inline_children(tok))]
        if isinstance(tok, span_token.Strikethrough):
            return [Strikethrough(children=self._convert_inline_children(tok))]
        if isinstance(tok, span_token.InlineCode):
            content = ""
            for c in tok.children or []:
                if isinstance(c, span_token.RawText):
                    content += c.content
            return [InlineCode(content=content)]
        if isinstance(tok, span_token.Link):
            target = tok.target or ""
            if target.startswith("cfx:op-"):
                opaque_id = target[len("cfx:") :]
                payload = self.pre.inline_payloads.get(opaque_id)
                if payload is not None:
                    label = ""
                    for c in tok.children or []:
                        if isinstance(c, span_token.RawText):
                            label += c.content
                    return [
                        InlineOpaque(
                            raw_xml=payload,
                            opaque_id=opaque_id,
                            label=label,
                        )
                    ]
                # Unauthenticated / missing payload — fall through to a
                # plain Link so the user's literal text is preserved.
            return [
                Link(
                    url=target,
                    title=tok.title or None,
                    children=self._convert_inline_children(tok),
                )
            ]
        if isinstance(tok, span_token.AutoLink):
            target = tok.target
            return [
                Link(
                    url=target,
                    title=None,
                    children=(Text(content=target),),
                )
            ]
        if isinstance(tok, span_token.Image):
            alt = ""
            for c in tok.children or []:
                if isinstance(c, span_token.RawText):
                    alt += c.content
            src, width, height = _decode_image_url(tok.src)
            # If the image has an asset marker that was captured at
            # preprocess time, the marker carries the *original*
            # Confluence filename. Restore that as the AST src so a
            # subsequent ``to_cfx`` call points back at the original
            # attachment, even if the user (or ``resolve_assets``) has
            # rewritten the visible link target.
            visible_src = tok.src or ""
            mapped = self.pre.asset_src_map.get(visible_src)
            if mapped is None and src != visible_src:
                mapped = self.pre.asset_src_map.get(src)
            if mapped is not None:
                src, _w, _h = _decode_image_url(mapped)
            return [
                Image(
                    src=src,
                    alt=alt,
                    title=tok.title or None,
                    width=width,
                    height=height,
                )
            ]
        # HtmlSpan is handled at the sequence level (_convert_span_sequence),
        # so reaching here is unexpected — fall through to the warning.
        html_span_cls = getattr(span_token, "HtmlSpan", None) or getattr(
            span_token, "HTMLSpan", None
        )
        if html_span_cls is not None and isinstance(tok, html_span_cls):
            self.warnings.append(
                "unmatched inline HTML span dropped"
            )
            return []
        # Unknown span: drop with warning.
        self.warnings.append(
            f"unsupported span token {type(tok).__name__!s} dropped"
        )
        return []


_IMAGE_FRAGMENT_RE = re.compile(
    r"cfxmark:(?P<dims>(?:[wh]=\d+(?:,[wh]=\d+)*))"
)


def _decode_image_url(src: str) -> tuple[str, int | None, int | None]:
    """Decode the width/height attributes that the renderer encoded in
    the URL fragment (see :func:`cfxmark.renderers.md._encode_image_url`).

    Returns the cleaned URL (without the ``cfxmark:`` fragment tail)
    plus optional width and height integers.
    """

    width: int | None = None
    height: int | None = None
    m = _IMAGE_FRAGMENT_RE.search(src)
    if m:
        for pair in m.group("dims").split(","):
            key, _, value = pair.partition("=")
            if not value.isdigit():
                continue
            if key == "w":
                width = int(value)
            elif key == "h":
                height = int(value)
        start = m.start()
        prev = src[start - 1] if start > 0 else ""
        if prev in ("#", "&"):
            start -= 1
        cleaned = src[:start] + src[m.end():]
        cleaned = cleaned.rstrip("#&")
        return cleaned, width, height
    return src, None, None


_HTML_OPEN_RE = re.compile(r"^<([a-zA-Z][a-zA-Z0-9]*)(?:\s[^>]*)?>$")
_HTML_CLOSE_RE = re.compile(r"^</([a-zA-Z][a-zA-Z0-9]*)\s*>$")
_HTML_SELF_RE = re.compile(r"^<([a-zA-Z][a-zA-Z0-9]*)(?:\s[^>]*)?/?\s*>$")


def _html_span_match(
    token: span_token.SpanToken,
) -> tuple[str, str] | None:
    """Classify an ``HtmlSpan`` token as open/close/self-closing.

    Returns ``(kind, tag_name)`` where ``kind`` is ``"open"``,
    ``"close"``, or ``"self"``. Non-span tokens return ``None``.
    """

    html_span_cls = getattr(span_token, "HtmlSpan", None) or getattr(
        span_token, "HTMLSpan", None
    )
    if html_span_cls is None or not isinstance(token, html_span_cls):
        return None
    content = getattr(token, "content", "") or ""
    content = content.strip()
    if m := _HTML_CLOSE_RE.match(content):
        return "close", m.group(1).lower()
    if content.endswith("/>") or content.lower() in ("<br>", "<br/>", "<hr>", "<hr/>"):
        m = _HTML_SELF_RE.match(content)
        if m:
            return "self", m.group(1).lower()
    if m := _HTML_OPEN_RE.match(content):
        return "open", m.group(1).lower()
    return None


_COLSPAN_MARK = "<"
_ROWSPAN_MARK = "^"


def _is_span_marker(cell: TableCell, mark: str) -> bool:
    """True if a cell's only inline content is the literal ``<`` / ``^``
    used to flag a colspan / rowspan continuation."""

    if len(cell.children) != 1:
        return False
    child = cell.children[0]
    return isinstance(child, Text) and child.content.strip() == mark


def _collapse_span_markers(rows: list[TableRow]) -> list[TableRow]:
    """Walk a row sequence and merge MultiMarkdown ``<`` / ``^``
    continuation cells into the cell to the left / above.

    Returns a new list of :class:`TableRow` objects with adjusted
    ``colspan`` / ``rowspan`` on the surviving cells.
    """

    if not rows:
        return rows

    # Step 1: collapse colspan markers within each row.
    collapsed: list[list[TableCell]] = []
    for src_row in rows:
        new_cells: list[TableCell] = []
        for cell in src_row.cells:
            if new_cells and _is_span_marker(cell, _COLSPAN_MARK):
                last = new_cells[-1]
                new_cells[-1] = TableCell(
                    kind=last.kind,
                    children=last.children,
                    align=last.align,
                    colspan=last.colspan + 1,
                    rowspan=last.rowspan,
                )
                continue
            new_cells.append(cell)
        collapsed.append(new_cells)

    # Step 2: collapse rowspan markers. For each ``^`` cell, find the
    # most recent real cell in the same visual column and bump its
    # rowspan, then drop the marker from the current row.
    def column_of(cell_list: list[TableCell], target_index: int) -> int:
        col = 0
        for i, c in enumerate(cell_list):
            if i == target_index:
                return col
            col += c.colspan
        return col

    for ri in range(len(collapsed)):
        cell_list = collapsed[ri]
        for ci in range(len(cell_list) - 1, -1, -1):
            cell = cell_list[ci]
            if not _is_span_marker(cell, _ROWSPAN_MARK):
                continue
            target_col = column_of(cell_list, ci)
            for ri_above in range(ri - 1, -1, -1):
                above_row = collapsed[ri_above]
                col = 0
                bumped = False
                for above_index, above_cell in enumerate(above_row):
                    if col == target_col and not _is_span_marker(
                        above_cell, _ROWSPAN_MARK
                    ):
                        above_row[above_index] = TableCell(
                            kind=above_cell.kind,
                            children=above_cell.children,
                            align=above_cell.align,
                            colspan=above_cell.colspan,
                            rowspan=above_cell.rowspan + 1,
                        )
                        bumped = True
                        break
                    col += above_cell.colspan
                if bumped:
                    break
            del cell_list[ci]

    return [TableRow(cells=tuple(r)) for r in collapsed]


def _convert_align(align: int | str | None) -> CellAlign:
    if align is None:
        return CellAlign.NONE
    if isinstance(align, int):
        mapping = {0: CellAlign.NONE, 1: CellAlign.LEFT, 2: CellAlign.CENTER, 3: CellAlign.RIGHT}
        return mapping.get(align, CellAlign.NONE)
    lookup = {
        "left": CellAlign.LEFT,
        "center": CellAlign.CENTER,
        "right": CellAlign.RIGHT,
        "none": CellAlign.NONE,
    }
    return lookup.get(str(align).lower(), CellAlign.NONE)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def parse_md(
    source: str,
    *,
    registry: MacroRegistry | None = None,
    passthrough_html_comment_prefixes: tuple[str, ...] = (),
) -> tuple[Document, list[str]]:
    """Parse a Markdown string into a cfxmark AST.

    :param source: CommonMark + GFM tables + strikethrough + cfxmark
        extensions (opaque sentinels, directive fences).
    :param registry: macro registry, defaults to :data:`cfxmark.macros.default_registry`.
    :param passthrough_html_comment_prefixes: tuple of leading-word
        prefixes (``"workflow:"``, …) that identify HTML comment
        blocks caller-owned on the Markdown side. Matching comments
        are preserved across round-trip as
        :class:`cfxmark.ast.PassthroughComment` nodes instead of being
        dropped with a warning.
    :returns: ``(document, warnings)`` tuple.
    """

    pre = _preprocess(
        source,
        passthrough_html_comment_prefixes=passthrough_html_comment_prefixes,
    )
    try:
        md_doc = MdDocument(pre.source)
        converter = _MdConverter(
            pre=pre,
            registry=registry or default_registry,
            passthrough_html_comment_prefixes=passthrough_html_comment_prefixes,
        )
        document = converter.convert_document(md_doc)
    except Exception as ex:  # pragma: no cover — defensive
        raise ParseError(f"failed to parse markdown: {ex}") from ex
    return document, converter.warnings


__all__ = ["parse_md"]
