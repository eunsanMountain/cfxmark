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
    TableRow,
    Text,
    Underline,
    UnsupportedMarker,
)
from cfxmark.exceptions import ParseError
from cfxmark.image_metadata import (
    marker_transport_path,
    rewrite_image_metadata_comments,
    visible_target_matches_attachment,
)
from cfxmark.macros import MacroRegistry, default_registry
from cfxmark.opaque import (
    AssetMarkerBinding,
    bind_inline_opaque_refs,
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
_HtmlSpanToken = getattr(span_token, "HtmlSpan", None) or getattr(span_token, "HTMLSpan", None)
if _HtmlSpanToken is not None and _HtmlSpanToken not in span_token._token_types:
    span_token._token_types.insert(0, _HtmlSpanToken)


# ---------------------------------------------------------------------------
# Placeholder scheme
# ---------------------------------------------------------------------------


_OPAQUE_MARKER = "CFXMARK_OPAQUE"
_DIRECTIVE_MARKER = "CFXMARK_DIRECTIVE"
_PASSTHROUGH_MARKER = "CFXMARK_PASSTHROUGH"
_ALIGN_SENTINEL_PREFIX = "\ue000CFXALIGN:"
_ALIGN_SENTINEL_SUFFIX = "\ue001"
_NBSP_SENTINEL = "\ue002CFXNBSP\ue003"
_ALIGN_COMMENT_RE = re.compile(r"<!--\s*cfxmark:align=(left|center|right)\s*-->", re.IGNORECASE)
_ALIGN_MANAGED_RE = re.compile(r"\{cfxalign=(left|center|right)\}", re.IGNORECASE)
_UNSUPPORTED_COMMENT_RE = re.compile(r"^<!--\s*cfxmark:unsupported\b(?P<attrs>.*?)-->$", re.IGNORECASE | re.DOTALL)
_UNSUPPORTED_ATTR_RE = re.compile(r'([a-z-]+)=(?:"([^"]*)"|([^\s]+))', re.IGNORECASE)


def _placeholder(kind: str, index: int) -> str:
    # Wrapped in backticks so mistletoe sees it as inline code and
    # does not apply emphasis / link parsing to the underscores.
    return f"`{kind}-{index}-CFXMARK`"


_PLACEHOLDER_RE = re.compile(r"^(CFXMARK_OPAQUE|CFXMARK_DIRECTIVE|CFXMARK_PASSTHROUGH)-(\d+)-CFXMARK$")


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
    inline_opaque_bindings: dict[str, tuple[str, str]] = field(default_factory=dict)
    asset_src_map: dict[str, AssetMarkerBinding] = field(default_factory=dict)


_DIRECTIVE_OPEN_RE = re.compile(r"^:::\s+(?P<name>[a-zA-Z][a-zA-Z0-9_-]*)(?P<rest>.*)$")
_DIRECTIVE_CLOSE_RE = re.compile(r"^:::\s*$")
_DIRECTIVE_PARAM_RE = re.compile(r'(?P<key>[a-zA-Z][a-zA-Z0-9_-]*)\s*=\s*"(?P<value>(?:\\.|[^"\\])*)"')
_INLINE_DIRECTIVE_RE = re.compile(
    r"\{\{(?P<name>jira)(?P<rest>(?:\s+[a-zA-Z][a-zA-Z0-9_-]*\s*=\s*"
    r'"(?:\\.|[^"\\])*")*)\s*\}\}',
    re.IGNORECASE,
)

# A CommonMark fenced code block opens with at least three backticks
# or tildes at the start of a line (optionally indented up to three
# spaces) and closes with the same character run of equal-or-greater
# length. The directive scanner has to skip over these regions so that
# Markdown showing a literal ``::: info`` example does not get
# rewritten into a real directive.
_FENCE_RE = re.compile(r"^( {0,3})(`{3,}|~{3,})")


def _lines_outside_fences(lines: list[str]) -> list[bool]:
    outside: list[bool] = []
    fence_char = ""
    fence_length = 0
    for line in lines:
        stripped = line.lstrip(" ")
        if fence_char:
            outside.append(False)
            candidate = stripped.strip()
            if len(line) - len(stripped) <= 3 and len(candidate) >= fence_length and set(candidate) == {fence_char}:
                fence_char = ""
                fence_length = 0
            continue
        fence_match = _FENCE_RE.match(line)
        if fence_match is not None:
            outside.append(False)
            fence = fence_match.group(2)
            fence_char = fence[0]
            fence_length = len(fence)
            continue
        outside.append(True)
    return outside


def _is_gfm_table_delimiter(line: str) -> bool:
    stripped = line.strip()
    if "|" not in stripped:
        return False
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    cells = stripped.split("|")
    return bool(cells) and all(re.fullmatch(r":?-+:?", cell.strip()) is not None for cell in cells)


def _gfm_table_alignments(line: str) -> list[CellAlign]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    alignments: list[CellAlign] = []
    for raw_cell in stripped.split("|"):
        cell = raw_cell.strip()
        if cell.startswith(":") and cell.endswith(":"):
            alignments.append(CellAlign.CENTER)
        elif cell.endswith(":"):
            alignments.append(CellAlign.RIGHT)
        elif cell.startswith(":"):
            alignments.append(CellAlign.LEFT)
        else:
            alignments.append(CellAlign.NONE)
    return alignments


def _has_unescaped_pipe(text: str) -> bool:
    for index, ch in enumerate(text):
        if ch != "|":
            continue
        backslashes = 0
        cursor = index - 1
        while cursor >= 0 and text[cursor] == "\\":
            backslashes += 1
            cursor -= 1
        # mistletoe's GFM table tokenizer treats any immediately preceding
        # backslash run as an escaped cell delimiter and preserves the run in
        # code spans.  Match that parser contract instead of applying normal
        # prose backslash parity here.
        if backslashes == 0:
            return True
    return False


def _backtick_is_escaped(line: str, pos: int) -> bool:
    """Whether the backtick at ``pos`` is a backslash-escaped literal.

    A ``\\``` is a literal backtick to the CommonMark/mistletoe inline parser,
    not a code-span delimiter, so it never opens or closes a code span. The
    table code-span ambiguity check must skip it, or literal backticks that
    happen to straddle a ``|`` cell boundary (a Confluence table cell holding
    the text ``\\`x | y\\```) are wrongly reported as an unescaped pipe inside a
    code span.
    """

    backslashes = 0
    cursor = pos - 1
    while cursor >= 0 and line[cursor] == "\\":
        backslashes += 1
        cursor -= 1
    return backslashes % 2 == 1


def _line_has_ambiguous_code_pipe(line: str) -> bool:
    cursor = 0
    while cursor < len(line):
        if line[cursor] != "`" or _backtick_is_escaped(line, cursor):
            cursor += 1
            continue
        opener_end = cursor
        while opener_end < len(line) and line[opener_end] == "`":
            opener_end += 1
        delimiter_length = opener_end - cursor
        close_start = opener_end
        while close_start < len(line):
            if line[close_start] != "`" or _backtick_is_escaped(line, close_start):
                close_start += 1
                continue
            close_end = close_start
            while close_end < len(line) and line[close_end] == "`":
                close_end += 1
            if close_end - close_start == delimiter_length:
                if _has_unescaped_pipe(line[opener_end:close_start]):
                    return True
                cursor = close_end
                break
            close_start = close_end
        else:
            return False
    return False


def _has_ambiguous_table_code_pipe(source: str) -> bool:
    """Detect raw code-span pipes only inside syntactic GFM tables."""

    lines = source.splitlines()
    outside_fences = _lines_outside_fences(lines)
    table_rows: set[int] = set()
    for index in range(len(lines) - 1):
        if not outside_fences[index] or not outside_fences[index + 1]:
            continue
        if "|" not in lines[index] or not _is_gfm_table_delimiter(lines[index + 1]):
            continue
        table_rows.add(index)
        body_index = index + 2
        while body_index < len(lines) and lines[body_index].strip():
            table_rows.add(body_index)
            body_index += 1

    return any(_line_has_ambiguous_code_pipe(lines[index]) for index in table_rows)


def _parse_directive_params(rest: str) -> tuple[tuple[str, str], ...]:
    params: list[tuple[str, str]] = []
    for m in _DIRECTIVE_PARAM_RE.finditer(rest):
        value = m.group("value")
        value = value.replace('\\"', '"').replace("\\\\", "\\")
        params.append((m.group("key"), value))
    return tuple(params)


class _InlineDirectiveToken(span_token.SpanToken):
    """Typed inline macro syntax such as ``{{jira key="DOC-1"}}``."""

    pattern = _INLINE_DIRECTIVE_RE
    parse_inner = False

    def __init__(self, match: re.Match[str]) -> None:
        self.name = match.group("name").lower()
        self.parameters = _parse_directive_params(match.group("rest") or "")


if _InlineDirectiveToken not in span_token._token_types:
    span_token._token_types.insert(0, _InlineDirectiveToken)


def _preprocess(
    source: str,
    passthrough_html_comment_prefixes: tuple[str, ...] = (),
    *,
    validate_asset_marker_sources: bool = True,
) -> _PreprocessResult:
    """Strip opaque blocks and directive fences, substituting placeholders."""

    # 1. Strip the cfxmark header notice if present.
    source = strip_header_notice(source)
    source = _ALIGN_COMMENT_RE.sub(
        lambda match: f'<cfxalign data-value="{match.group(1).lower()}"/>',
        source,
    )
    source = _ALIGN_MANAGED_RE.sub(
        lambda match: _ALIGN_SENTINEL_PREFIX + match.group(1).lower() + _ALIGN_SENTINEL_SUFFIX,
        source,
    )
    # 2. Extract the inline-opaque payload sidecar (and remove it from
    #    the body so mistletoe never sees it as ordinary content).
    inline_payloads = parse_payloads(source)
    source = strip_payloads_section(source)
    source, inline_opaque_bindings = bind_inline_opaque_refs(source, inline_payloads)
    # 3. Bind each asset marker to a unique parser-only image placeholder.
    #    Doing this before metadata lowering prevents literal attachment
    #    filename text such as ``#cfxmark:w=...`` from gaining dimension
    #    authority.
    source, asset_src_map = strip_asset_markers(
        source,
        validate_remote_source=validate_asset_marker_sources,
    )
    # 4. Validate occurrence-scoped image metadata, then lower it onto the
    #    placeholder target. Rendering only emits the adjacent comment grammar.
    source = rewrite_image_metadata_comments(source)

    result = _PreprocessResult(
        source="",
        inline_opaque_bindings=inline_opaque_bindings,
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
        result.opaques.append(_OpaqueCapture(opaque_id=match.opaque_id, raw_xml=match.raw_xml))
        opaque_parts.append("\n\n" + _placeholder(_OPAQUE_MARKER, idx) + "\n\n")
        cursor = match.end
    opaque_parts.append(source[cursor:])
    after_opaque = "".join(opaque_parts)

    # --- Passthrough comment open detector. Built once per call so
    # the line scanner can fast-path when no prefix was registered.
    passthrough_open_re = _build_passthrough_open_re(passthrough_html_comment_prefixes)

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
            if (
                close is not None
                and close.group(2).startswith(fence_marker[0])
                and len(close.group(2)) >= len(fence_marker)
            ):
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
                    out_lines.append(_placeholder(_PASSTHROUGH_MARKER, idx))
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

    result.source = "\n".join(out_lines).replace("\u00a0", _NBSP_SENTINEL)
    return result


def _build_passthrough_open_re(
    prefixes: tuple[str, ...],
) -> re.Pattern[str] | None:
    """Build a regex matching the *opening line* of an HTML comment
    whose first non-whitespace token starts with one of ``prefixes``.

    Returns ``None`` when no eligible prefix remains after the
    ``cfxmark:`` filter in
    :func:`cfxmark.normalize._safe_passthrough_alternation`.
    """

    from cfxmark.normalize import _safe_passthrough_alternation

    alternation = _safe_passthrough_alternation(prefixes)
    if alternation is None:
        return None
    return re.compile(r"^[ \t]*<!--\s*(?:" + alternation + r")")


# ---------------------------------------------------------------------------
# mistletoe walker
# ---------------------------------------------------------------------------


class _MdConverter:
    def __init__(
        self,
        pre: _PreprocessResult,
        registry: MacroRegistry,
    ) -> None:
        self.pre = pre
        self.registry = registry
        self.warnings: list[str] = []
        self._table_cell_depth = 0

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
            content = _restore_nbsp(tok.content)
            if content.endswith("\n"):
                content = content[:-1]
            return [CodeBlock(content=content, language=lang or None)]
        if isinstance(tok, block_token.BlockCode):
            return [CodeBlock(content=_restore_nbsp(tok.content), language=None)]
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
        self.warnings.append(f"unsupported block token {type(tok).__name__!s} dropped")
        return []

    def _convert_blocks(self, children: list[block_token.BlockToken]) -> list[BlockNode]:
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
        inner = _MdConverter(pre=self.pre, registry=self.registry)
        document = inner.convert_document(md_doc)
        self.warnings.extend(inner.warnings)
        return document.children

    def _convert_paragraph(self, tok: block_token.Paragraph) -> list[BlockNode]:
        converted = self._convert_inline_nodes(tok)
        paragraph_align = _pop_trailing_alignment_marker(converted)
        inline = tuple(_coalesce_adjacent_text(_apply_alignment_markers(converted)))
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
                if kind == _DIRECTIVE_MARKER and 0 <= idx < len(self.pre.directives):
                    dir_cap = self.pre.directives[idx]
                    body_blocks = self._parse_inner_body(dir_cap.body_source)
                    return [
                        DirectiveMacro(
                            name=dir_cap.name,
                            parameters=dir_cap.parameters,
                            body=body_blocks if body_blocks else None,
                        )
                    ]
                if kind == _PASSTHROUGH_MARKER and 0 <= idx < len(self.pre.passthroughs):
                    return [PassthroughComment(content=self.pre.passthroughs[idx])]
        if paragraph_align is not None:
            if not inline:
                return []
            return [Paragraph(children=inline, align=paragraph_align)]
        if len(inline) == 1 and isinstance(inline[0], AlignedSpan):
            aligned = inline[0]
            return [Paragraph(children=aligned.children, align=aligned.align)]
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
        for item in tok.children or []:
            if not isinstance(item, block_token.ListItem):
                continue
            blocks = self._convert_blocks(item.children or [])
            items.append(ListItem(children=tuple(blocks)))
        list_type = ListType.ORDERED if is_ordered else ListType.BULLET
        return List(list_type=list_type, items=tuple(items), start=start)

    def _convert_table(self, tok: block_token.Table) -> Table:
        alignments = [_convert_align(a) for a in getattr(tok, "column_align", []) or []]
        source_lines = self.pre.source.splitlines()
        delimiter_line_index = getattr(tok, "line_number", 0)
        if 0 <= delimiter_line_index < len(source_lines):
            delimiter_line = source_lines[delimiter_line_index]
            if _is_gfm_table_delimiter(delimiter_line):
                alignments = _gfm_table_alignments(delimiter_line)
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
            self._table_cell_depth += 1
            try:
                inline = self._convert_inline_children(cell)
            finally:
                self._table_cell_depth -= 1
            cells.append(TableCell(kind=kind, children=inline, align=align))
        return TableRow(cells=tuple(cells))

    def _convert_html_block(self, tok: block_token.BlockToken) -> list[BlockNode]:
        content = getattr(tok, "content", "") or ""
        unsupported = _parse_unsupported_marker(content)
        if unsupported is not None:
            return [unsupported]
        # Lingering opaque sentinels that slipped past pre-processing
        # could land here; in that case we silently drop with a warning.
        if "cfxmark:opaque" in content:
            self.warnings.append(
                "HTML block contained a cfxmark opaque sentinel that was not captured in pre-processing"
            )
            return []
        self.warnings.append(
            "HTML block dropped — Confluence does not preserve inline HTML outside of supported macros"
        )
        return []

    # ---- inline -----------------------------------------------------------

    def _convert_inline_children(self, tok: block_token.BlockToken) -> tuple[InlineNode, ...]:
        # Walk the flat span-token sequence, grouping HTML open/close
        # tag pairs we know about into their corresponding AST nodes.
        return tuple(_coalesce_adjacent_text(_apply_alignment_markers(self._convert_inline_nodes(tok))))

    def _convert_inline_nodes(self, tok: block_token.BlockToken) -> list[InlineNode]:
        raw_tokens = list(tok.children or [])
        converted, _ = self._convert_span_sequence(raw_tokens, 0, None)
        return converted

    # Tag → AST constructor for HTML fallback.
    _HTML_TAG_NODES: dict[str, type] = {
        "strong": Strong,
        "em": Emphasis,
        "b": Strong,
        "i": Emphasis,
        "del": Strikethrough,
        "s": Strikethrough,
        "sub": Subscript,
        "sup": Superscript,
        "ins": Underline,
        "u": Underline,
        "cite": Citation,
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
                    inner, next_i = self._convert_span_sequence(tokens, i + 1, tag)
                    node_cls = self._HTML_TAG_NODES[tag]
                    out.append(node_cls(children=tuple(_coalesce_adjacent_text(inner))))
                    i = next_i
                    continue
                if kind == "self" and tag == "br":
                    out.append(HardBreak())
                    i += 1
                    continue
                if kind == "self" and tag == "cfxalign":
                    align = _extract_align_value(raw)
                    if align is not None:
                        out.append(Text(content=(_ALIGN_SENTINEL_PREFIX + align + _ALIGN_SENTINEL_SUFFIX)))
                    i += 1
                    continue
                if kind == "open" and tag == "span":
                    foreground = _extract_color_span(raw, background=False)
                    background = _extract_color_span(raw, background=True, accept_data_attribute=False)
                    if foreground is not None or background is not None:
                        inner, next_i = self._convert_span_sequence(tokens, i + 1, "span")
                        children: tuple[InlineNode, ...] = tuple(inner)
                        if foreground is not None:
                            children = (ColorSpan(color=foreground, children=children, background=False),)
                        if background is not None:
                            canonical_background = "gray" if background == "neutral" else background
                            children = (ColorSpan(color=canonical_background, children=children, background=True),)
                        out.extend(children)
                        i = next_i
                        continue
                if kind == "open" and tag == "mark":
                    color = _extract_color_span(raw, background=True)
                    if color is not None:
                        color = "gray" if color == "neutral" else color
                        inner, next_i = self._convert_span_sequence(tokens, i + 1, "mark")
                        out.append(
                            ColorSpan(
                                color=color,
                                children=tuple(inner),
                                background=True,
                            )
                        )
                        i = next_i
                        continue
                # Unknown HTML span — drop with warning (same policy as
                # the non-span HTMLSpan handler).
                self.warnings.append(f"inline HTML tag <{tag}> dropped — no equivalent in Confluence")
                i += 1
                continue
            out.extend(self._convert_inline(raw))
            i += 1
        return out, i

    def _convert_inline(self, tok: span_token.SpanToken) -> list[InlineNode]:
        if isinstance(tok, _InlineDirectiveToken):
            return [
                InlineDirectiveMacro(
                    name=tok.name,
                    parameters=tok.parameters,
                )
            ]
        if isinstance(tok, span_token.RawText):
            return [Text(content=_restore_nbsp(tok.content))]
        if isinstance(tok, span_token.EscapeSequence):
            content = tok.children[0].content if tok.children else ""
            return [Text(content=_restore_nbsp(content))]
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
            content = _restore_nbsp(content)
            if self._table_cell_depth:
                content = re.sub(
                    r"(\\{2})+\|",
                    lambda match: "\\" * (len(match.group(0)[:-1]) // 2) + "|",
                    content,
                )
            return [InlineCode(content=content)]
        if isinstance(tok, span_token.Link):
            target = _restore_nbsp(tok.target or "")
            inline_binding = self.pre.inline_opaque_bindings.get(target)
            if inline_binding is not None:
                opaque_id, payload = inline_binding
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
                    title=_restore_nbsp(tok.title) if tok.title else None,
                    children=self._convert_inline_children(tok),
                )
            ]
        if isinstance(tok, span_token.AutoLink):
            target = _restore_nbsp(tok.target)
            return [
                Link(
                    url=target,
                    title=None,
                    children=(Text(content=target),),
                )
            ]
        if isinstance(tok, span_token.Image):
            alt = _restore_nbsp(_plain_span_text(tok.children or []))
            visible_src = _restore_nbsp(tok.src or "")
            placeholder_src, width, height, thumbnail, align = _decode_image_url(visible_src)
            asset_binding = self.pre.asset_src_map.get(placeholder_src)
            if asset_binding is not None:
                # Metadata belongs to the visible Markdown link domain, never
                # to the typed remote attachment identity. A legacy fragment
                # is therefore authoritative only when the visible target is
                # distinct from the exact remote filename. This preserves old
                # sidecar/link input while keeping filenames such as
                # ``foo#cfxmark:w=640`` literal.
                clean_visible_src, legacy_width, legacy_height, legacy_thumbnail, legacy_align = _decode_image_url(
                    asset_binding.visible_src
                )
                transport_src = asset_binding.visible_src
                comment_metadata = (width, height, thumbnail, align) if _legacy_image_fragment(visible_src) else None
                has_visible_legacy_metadata = bool(_legacy_image_fragment(asset_binding.visible_src))
                legacy_metadata: tuple[int | None, int | None, bool, str | None] | None = None
                if has_visible_legacy_metadata and asset_binding.visible_src != asset_binding.remote_src:
                    if not visible_target_matches_attachment(clean_visible_src, asset_binding.remote_src):
                        raise ParseError("legacy image metadata target is ambiguous for the asset marker")
                    legacy_metadata = (legacy_width, legacy_height, legacy_thumbnail, legacy_align)
                    transport_src = clean_visible_src
                if comment_metadata is not None and legacy_metadata is not None and comment_metadata != legacy_metadata:
                    raise ParseError("image metadata comment conflicts with the legacy URL fragment")
                if comment_metadata is None and legacy_metadata is not None:
                    width, height, thumbnail, align = legacy_metadata
                src = marker_transport_path(transport_src, asset_binding.remote_src)
            elif _is_absolute_url(visible_src) and _legacy_image_fragment(visible_src):
                self.warnings.append(
                    "external image URL uses the reserved cfxmark fragment; URL preserved without interpreting dimensions"
                )
                src, width, height, thumbnail, align = visible_src, None, None, False, None
            else:
                src = placeholder_src
            attachment_filename: str | None = None
            # If the image has an asset marker that was captured at preprocess
            # time, restore the original flat Confluence filename in a typed
            # AST field. Keeping the raw filename in the semantic domain avoids
            # false edits while preventing URL punctuation from gaining
            # authority during storage rendering.
            if asset_binding is not None:
                attachment_filename = asset_binding.remote_src
            return [
                Image(
                    src=src,
                    attachment_filename=attachment_filename,
                    alt=alt,
                    title=tok.title or None,
                    width=width,
                    height=height,
                    thumbnail=thumbnail,
                    align=align,
                )
            ]
        # HtmlSpan is handled at the sequence level (_convert_span_sequence),
        # so reaching here is unexpected — fall through to the warning.
        html_span_cls = getattr(span_token, "HtmlSpan", None) or getattr(span_token, "HTMLSpan", None)
        if html_span_cls is not None and isinstance(tok, html_span_cls):
            content = getattr(tok, "content", "") or ""
            unsupported = _parse_unsupported_marker(content)
            if unsupported is not None:
                return [unsupported]
            # A genuine HTML comment (<!-- ... -->) is a recognisable, describable
            # loss: it holds author-only text that never renders in Confluence, so
            # it is dropped but classified for consent instead of folded into the
            # unknown-construct bucket (which fails closed to fatal). The <!--/-->
            # syntax is the discriminator — processing instructions (<? ?>), CDATA
            # (<![CDATA[ ]]>) and declarations (<!DOCTYPE>) share this fall-through
            # yet are NOT comments, so they keep the generic message and its path.
            stripped = content.strip()
            if stripped.startswith("<!--") and stripped.endswith("-->"):
                self.warnings.append("HTML comment dropped")
                return []
            self.warnings.append("unmatched inline HTML span dropped")
            return []
        # Unknown span: drop with warning.
        self.warnings.append(f"unsupported span token {type(tok).__name__!s} dropped")
        return []


def _plain_span_text(tokens: list[span_token.SpanToken]) -> str:
    """Collect visible text from an image label, including escaped/nested tokens."""

    parts: list[str] = []
    for token in tokens:
        content = getattr(token, "content", None)
        children = list(getattr(token, "children", None) or [])
        if isinstance(token, span_token.RawText):
            parts.append(content or "")
        elif children:
            parts.append(_plain_span_text(children))
        elif content:
            parts.append(str(content))
    return "".join(parts)


def _restore_nbsp(value: str) -> str:
    return value.replace(_NBSP_SENTINEL, "\u00a0")


_ABSOLUTE_URL_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")
_IMAGE_FRAGMENT_RE = re.compile(
    r"(?P<prefix>cfxmark(?:-external)?):(?P<attrs>(?:(?:[wh]=\d+|thumbnail=1|align=(?:center|right))"
    r"(?:,(?:[wh]=\d+|thumbnail=1|align=(?:center|right)))*))"
)


def _is_absolute_url(src: str) -> bool:
    return bool(_ABSOLUTE_URL_RE.match(src))


def _legacy_image_fragment(src: str) -> bool:
    match = _IMAGE_FRAGMENT_RE.search(src)
    return bool(match is not None and match.group("prefix") == "cfxmark")


def _decode_image_url(
    src: str,
) -> tuple[str, int | None, int | None, bool, str | None]:
    """Decode legacy image attributes from a URL fragment.

    New renderer output uses an adjacent ``cfxmark:img`` comment, which the
    preprocess pass validates and temporarily lowers to this compatibility
    grammar. Returns the cleaned URL plus the typed image attributes.
    """

    width: int | None = None
    height: int | None = None
    thumbnail = False
    align: str | None = None
    m = _IMAGE_FRAGMENT_RE.search(src)
    if m:
        for pair in m.group("attrs").split(","):
            key, _, value = pair.partition("=")
            if key == "w" and value.isdigit():
                width = int(value)
            elif key == "h" and value.isdigit():
                height = int(value)
            elif key == "thumbnail" and value == "1":
                thumbnail = True
            elif key == "align" and value in {"center", "right"}:
                align = value
        start = m.start()
        prev = src[start - 1] if start > 0 else ""
        if prev in ("#", "&"):
            start -= 1
        cleaned = src[:start] + src[m.end() :]
        cleaned = cleaned.rstrip("#&")
        return cleaned, width, height, thumbnail, align
    return src, None, None, False, None


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

    html_span_cls = getattr(span_token, "HtmlSpan", None) or getattr(span_token, "HTMLSpan", None)
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


_SPAN_COLOR_RE = re.compile(r'style="[^"]*(?<!-)color:\s*([^;"\']+)')
_SPAN_BACKGROUND_RE = re.compile(r'style="[^"]*background-color:\s*([^;"\']+)')
_DATA_COLOR_RE = re.compile(r'data-cfx-color="([^"]+)"')
_ALIGN_VALUE_RE = re.compile(r'data-value="(center|right)"')


def _extract_span_color(token: span_token.SpanToken) -> str | None:
    """Return the ``color`` value from ``<span style="color:...">`` or None."""
    content = getattr(token, "content", "") or ""
    m = _SPAN_COLOR_RE.search(content)
    return m.group(1).strip() if m else None


def _extract_color_span(
    token: span_token.SpanToken,
    *,
    background: bool,
    accept_data_attribute: bool = True,
) -> str | None:
    content = getattr(token, "content", "") or ""
    if accept_data_attribute:
        data_match = _DATA_COLOR_RE.search(content)
        if data_match:
            return data_match.group(1)
    pattern = _SPAN_BACKGROUND_RE if background else _SPAN_COLOR_RE
    match = pattern.search(content)
    return match.group(1).strip() if match else None


def _extract_align_value(token: span_token.SpanToken) -> str | None:
    content = getattr(token, "content", "") or ""
    match = _ALIGN_VALUE_RE.search(content)
    return match.group(1) if match else None


def _apply_alignment_markers(nodes: list[InlineNode]) -> list[InlineNode]:
    """Attach an alignment marker to its current hard-break segment."""

    out: list[InlineNode] = []
    for node in nodes:
        if (
            isinstance(node, Text)
            and node.content.startswith(_ALIGN_SENTINEL_PREFIX)
            and node.content.endswith(_ALIGN_SENTINEL_SUFFIX)
        ):
            align = node.content[len(_ALIGN_SENTINEL_PREFIX) : -len(_ALIGN_SENTINEL_SUFFIX)]
            start = len(out)
            while start > 0 and not isinstance(out[start - 1], HardBreak):
                start -= 1
            segment = tuple(out[start:])
            if segment and align in {"left", "center", "right"}:
                del out[start:]
                out.append(AlignedSpan(align=align, children=segment))
            continue
        out.append(node)
    return out


def _coalesce_adjacent_text(nodes: list[InlineNode]) -> list[InlineNode]:
    """Keep one semantic owner when Markdown escapes split one text run."""

    result: list[InlineNode] = []
    for node in nodes:
        if isinstance(node, SoftBreak):
            node = Text(content="\n")
        if isinstance(node, Text) and result and isinstance(result[-1], Text):
            previous = result.pop()
            assert isinstance(previous, Text)
            result.append(Text(content=previous.content + node.content))
        else:
            result.append(node)
    return result


def _pop_trailing_alignment_marker(nodes: list[InlineNode]) -> str | None:
    """Remove and return a paragraph-level trailing alignment marker."""

    if not nodes or not isinstance(nodes[-1], Text):
        return None
    content = nodes[-1].content
    marker_start = content.rfind(_ALIGN_SENTINEL_PREFIX)
    if marker_start < 0:
        return None
    marker_end = content.find(_ALIGN_SENTINEL_SUFFIX, marker_start)
    if marker_end < 0:
        return None
    align = content[marker_start + len(_ALIGN_SENTINEL_PREFIX) : marker_end]
    if align not in {"left", "center", "right"}:
        return None
    remainder = content[:marker_start] + content[marker_end + len(_ALIGN_SENTINEL_SUFFIX) :]
    if remainder:
        nodes[-1] = Text(content=remainder)
    else:
        nodes.pop()
    return align


def _parse_unsupported_marker(content: str) -> UnsupportedMarker | None:
    """Parse a canonical or historical local unsupported marker."""

    match = _UNSUPPORTED_COMMENT_RE.match(content.strip())
    if match is None:
        return None
    attrs = {
        attr.group(1).lower(): attr.group(2) if attr.group(2) is not None else attr.group(3)
        for attr in _UNSUPPORTED_ATTR_RE.finditer(match.group("attrs"))
    }
    if attrs.get("push-safe") != "false":
        return None
    kind = attrs.get("kind", "inline")
    if kind not in {"inline", "block"}:
        kind = "inline"
    return UnsupportedMarker(kind=kind, macro=attrs.get("macro"), occurrence_id=attrs.get("occurrence"))


_COLSPAN_MARK = "<"
_ROWSPAN_MARK = "^"


def _is_span_marker(cell: TableCell, mark: str) -> bool:
    """True if a cell's only inline content is the literal ``<`` / ``^``
    used to flag a colspan / rowspan continuation."""

    if len(cell.children) != 1:
        return False
    child = cell.children[0]
    return isinstance(child, Text) and child.content.strip() == mark


@dataclass
class _SpanCell:
    kind: CellType
    children: tuple[InlineNode, ...]
    align: CellAlign
    colspan: int = 1
    rowspan: int = 1

    @classmethod
    def from_cell(cls, cell: TableCell) -> _SpanCell:
        return cls(
            kind=cell.kind,
            children=cell.children,
            align=cell.align,
            colspan=cell.colspan,
            rowspan=cell.rowspan,
        )

    def freeze(self) -> TableCell:
        return TableCell(
            kind=self.kind,
            children=self.children,
            align=self.align,
            colspan=self.colspan,
            rowspan=self.rowspan,
        )


def _collapse_span_markers(rows: list[TableRow]) -> list[TableRow]:
    """Walk a row sequence and merge MultiMarkdown ``<`` / ``^``
    continuation cells into the cell to the left / above.

    Returns a new list of :class:`TableRow` objects with adjusted
    ``colspan`` / ``rowspan`` on the surviving cells.
    """

    owner_rows: list[list[_SpanCell | None]] = []
    emitted_rows: list[list[_SpanCell]] = []

    for row_index, src_row in enumerate(rows):
        owners: list[_SpanCell | None] = []
        emitted: list[_SpanCell] = []
        previous: _SpanCell | None = None
        extended: set[int] = set()

        for column, cell in enumerate(src_row.cells):
            owner: _SpanCell | None = None
            if _is_span_marker(cell, _COLSPAN_MARK) and previous is not None:
                previous.colspan += 1
                owner = previous
            elif _is_span_marker(cell, _ROWSPAN_MARK) and row_index > 0:
                above = owner_rows[row_index - 1]
                owner = above[column] if column < len(above) else None
                if owner is not None and id(owner) not in extended:
                    owner.rowspan += 1
                    extended.add(id(owner))

            if owner is None:
                owner = _SpanCell.from_cell(cell)
                emitted.append(owner)
            owners.append(owner)
            previous = owner

        owner_rows.append(owners)
        emitted_rows.append(emitted)

    return [TableRow(cells=tuple(record.freeze() for record in row)) for row in emitted_rows]


def _convert_align(align: int | str | None) -> CellAlign:
    if align is None:
        return CellAlign.NONE
    if isinstance(align, int):
        # mistletoe 1.x: None=left/default, 0=center, 1=right.
        mapping = {0: CellAlign.CENTER, 1: CellAlign.RIGHT}
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
    validate_asset_marker_sources: bool = True,
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

    if _has_ambiguous_table_code_pipe(source):
        raise ParseError("unescaped pipe inside a GFM table code span is ambiguous; write \\| instead")

    pre = _preprocess(
        source,
        passthrough_html_comment_prefixes=passthrough_html_comment_prefixes,
        validate_asset_marker_sources=validate_asset_marker_sources,
    )
    try:
        md_doc = MdDocument(pre.source)
        converter = _MdConverter(
            pre=pre,
            registry=registry or default_registry,
        )
        document = converter.convert_document(md_doc)
    except Exception as ex:  # pragma: no cover — defensive
        raise ParseError(f"failed to parse markdown: {ex}") from ex
    return document, converter.warnings


__all__ = ["parse_md"]
