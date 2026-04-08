"""Public conversion API.

This module exposes the two public entry points — :func:`to_cfx` and
:func:`to_md` — plus the :class:`ConversionResult` dataclass they
return and the :class:`ConversionOptions` used to tweak their
behaviour.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from cfxmark.ast import Document
from cfxmark.macros import MacroRegistry, default_registry
from cfxmark.parsers.cfx import parse_cfx
from cfxmark.parsers.jira_wiki import parse_jira_wiki
from cfxmark.parsers.md import parse_md
from cfxmark.renderers.cfx import render_cfx
from cfxmark.renderers.md import (
    MarkdownRenderOptions,
    render_md,
)

_RI_FILENAME_RE = re.compile(r'<ri:attachment[^>]*ri:filename="([^"]+)"')
_CDATA_RE = re.compile(r"<!\[CDATA\[.*?\]\]>", re.DOTALL)


def _enumerate_attachments(xhtml: str) -> tuple[str, ...]:
    """Return every ``ri:filename`` referenced in ``xhtml`` in document
    order, deduplicated. Catches both Grade I/II native ``<ac:image>``
    references and Grade III opaque blocks that preserve raw XML.

    CDATA sections (e.g. the body of a ``code`` macro demonstrating
    Confluence XML) are stripped first, so documentation text that
    happens to mention ``<ri:attachment>`` does not leak into the
    result as a phantom attachment.
    """

    cleaned = _CDATA_RE.sub("", xhtml)
    return tuple(dict.fromkeys(_RI_FILENAME_RE.findall(cleaned)))

# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConversionOptions:
    """Options shared by :func:`to_cfx` and :func:`to_md`.

    * ``bullet_marker`` — the Markdown bullet character to use (``-`` by
      default). Only ``-``, ``*``, and ``+`` are meaningful.
    * ``code_fence`` — the code fence characters used in Markdown
      output. Defaults to ``"```"``.
    * ``passthrough_html_comment_prefixes`` — tuple of leading-word
      prefixes (``"workflow:"``, ``"mytool:"``, …) that identify HTML
      comment blocks caller-owned on the Markdown side. Matching
      comments are preserved across ``parse_md`` / ``render_md`` as
      :class:`cfxmark.ast.PassthroughComment` nodes and silently
      dropped by :func:`to_cfx` and :func:`to_jira_wiki` (they are
      local metadata, not document content). ``cfxmark:`` prefixes
      are never eligible — cfxmark's own sentinel comments have
      dedicated handling.
    """

    bullet_marker: str = "-"
    code_fence: str = "```"
    passthrough_html_comment_prefixes: tuple[str, ...] = ()

    def md_options(self) -> MarkdownRenderOptions:
        return MarkdownRenderOptions(
            bullet_marker=self.bullet_marker,
            code_fence=self.code_fence,
        )


DEFAULT_OPTIONS = ConversionOptions()


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConversionResult:
    """Outcome of a single conversion call.

    * ``xhtml`` — present on ``to_cfx`` results. ``None`` otherwise.
    * ``markdown`` — present on ``to_md`` results. ``None`` otherwise.
    * ``attachments`` — list of local file references emitted during
      Markdown → Confluence conversion. The caller is responsible for
      uploading these via the Confluence REST API before pushing the
      page body. ``None`` for ``to_md``.
    * ``warnings`` — human-readable messages describing anything the
      converter could not represent exactly (dropped HTML comments,
      escalated inline unknowns, …).
    * ``document`` — the intermediate cfxmark AST.
    """

    xhtml: str | None = None
    markdown: str | None = None
    attachments: tuple[str, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)
    document: Document | None = None
    jira_wiki: str | None = None


# ---------------------------------------------------------------------------
# Markdown → Confluence
# ---------------------------------------------------------------------------


def to_cfx(
    markdown: str,
    *,
    options: ConversionOptions | None = None,
    macros: MacroRegistry | None = None,
) -> ConversionResult:
    """Convert Markdown to Confluence Storage Format XHTML.

    :param markdown: Markdown source text.
    :param options: Conversion options. Defaults to
        :data:`DEFAULT_OPTIONS`. Only
        :attr:`ConversionOptions.passthrough_html_comment_prefixes`
        affects the ``markdown → cfx`` direction — bullet marker and
        code fence settings are no-ops here because Confluence storage
        XHTML has no Markdown style choices to make.
    :param macros: Macro registry to use. Defaults to
        :data:`cfxmark.macros.default_registry`.
    :returns: A :class:`ConversionResult` with ``xhtml``,
        ``attachments``, ``warnings`` and ``document`` populated.
    """

    opts = options or DEFAULT_OPTIONS
    registry = macros or default_registry
    document, parse_warnings = parse_md(
        markdown,
        registry=registry,
        passthrough_html_comment_prefixes=opts.passthrough_html_comment_prefixes,
    )
    xhtml, native_attachments, render_warnings = render_cfx(
        document, registry=registry
    )
    # Native images preserve their *original* local path (which may
    # contain directory prefixes like ``assets/img.png``) so the caller
    # knows where to read bytes from. Opaque blocks contain byte-
    # preserved XML that only exposes the Confluence basename; we
    # merge those in without duplicating natives already listed.
    native_basenames = {n.rsplit("/", 1)[-1] for n in native_attachments}
    merged: list[str] = list(native_attachments)
    for name in _enumerate_attachments(xhtml):
        if name in native_basenames or name in merged:
            continue
        merged.append(name)
    return ConversionResult(
        xhtml=xhtml,
        markdown=None,
        attachments=tuple(merged),
        warnings=tuple(parse_warnings) + tuple(render_warnings),
        document=document,
    )


# ---------------------------------------------------------------------------
# Confluence → Markdown
# ---------------------------------------------------------------------------


def to_md(
    xhtml: str,
    *,
    options: ConversionOptions | None = None,
    macros: MacroRegistry | None = None,
) -> ConversionResult:
    """Convert Confluence Storage Format XHTML to Markdown.

    :param xhtml: Confluence storage XHTML fragment (the body of a
        page, without the XML declaration or surrounding document
        structure).
    :param options: Conversion options. Defaults to :data:`DEFAULT_OPTIONS`.
    :param macros: Macro registry to use. Defaults to
        :data:`cfxmark.macros.default_registry`.
    :returns: A :class:`ConversionResult` with ``markdown``,
        ``warnings`` and ``document`` populated.
    """

    opts = options or DEFAULT_OPTIONS
    registry = macros or default_registry
    document, parse_warnings = parse_cfx(xhtml, registry=registry)
    markdown = render_md(document, options=opts.md_options())
    return ConversionResult(
        xhtml=None,
        markdown=markdown,
        attachments=_enumerate_attachments(xhtml),
        warnings=tuple(parse_warnings),
        document=document,
    )


# ---------------------------------------------------------------------------
# Jira wiki markup
# ---------------------------------------------------------------------------

_XHTML_UNAMBIGUOUS_RE = re.compile(r'^<[^>]+xmlns(:[a-z]+)?="')


def _resolve_input_format(
    source: str,
    hint: Literal["auto", "markdown", "xhtml"],
    warnings: list[str],
) -> str:
    if hint != "auto":
        return hint
    s = source.lstrip()
    if s.startswith(("<?xml", "<!DOCTYPE", "<ac:", "<ri:")):
        return "xhtml"
    if _XHTML_UNAMBIGUOUS_RE.match(s):
        return "xhtml"
    if s.startswith("<"):
        warnings.append(
            "input_format=auto: source starts with '<' but no Confluence-specific "
            "tokens matched; defaulting to markdown. Pass input_format=\"markdown\" "
            "or input_format=\"xhtml\" to be explicit."
        )
        return "markdown"
    return "markdown"


def to_jira_wiki(
    source: str,
    *,
    input_format: Literal["auto", "markdown", "xhtml"] = "auto",
    section: str | None = None,
    drop_leading_notice: tuple[re.Pattern[str], ...] = (),
    heading_promotion: Literal["confluence", "jira", "none"] = "confluence",
    options: ConversionOptions | None = None,
    macros: MacroRegistry | None = None,
) -> ConversionResult:
    """Convert Markdown or Confluence XHTML to Jira wiki markup.

    :param source: Source text (Markdown or Confluence Storage XHTML).
    :param input_format: ``"auto"`` (default), ``"markdown"``, or
        ``"xhtml"``. Auto-detect uses a narrow set of tokens to
        identify Confluence XHTML; ambiguous ``<tag>`` input defaults
        to markdown with a warning.
    :param section: If set, only the content of the first H2 section
        whose title equals this string is rendered. ``jira_wiki`` will
        be ``None`` if the section is not found.
    :param drop_leading_notice: If the first block is a paragraph
        whose flattened text matches any of these patterns, it is
        silently removed before rendering.
    :param heading_promotion: Heading level mapping policy —
        ``"confluence"`` (default, H3→``h2``, H4→``h3``, …) for
        pushing to a Confluence page where the page title occupies
        the top slot, or ``"jira"``/``"none"`` for a 1:1 identity
        mapping when pushing to a Jira issue ``description``.
    :param options: Conversion options. Only
        :attr:`ConversionOptions.passthrough_html_comment_prefixes`
        affects the Jira wiki pipeline — matching HTML comments on
        the Markdown side are captured as
        :class:`PassthroughComment` nodes and then dropped from the
        rendered Jira output (they are local metadata, not content).
    :param macros: Macro registry. Defaults to
        :data:`cfxmark.macros.default_registry`.
    :returns: A :class:`ConversionResult` with ``jira_wiki``,
        ``warnings`` and ``document`` populated.
    """
    from cfxmark.renderers.jira_wiki import render_jira_wiki

    opts = options or DEFAULT_OPTIONS
    registry = macros or default_registry
    warnings: list[str] = []
    fmt = _resolve_input_format(source, input_format, warnings)
    if fmt == "xhtml":
        document, parse_warnings = parse_cfx(source, registry=registry)
    else:
        document, parse_warnings = parse_md(
            source,
            registry=registry,
            passthrough_html_comment_prefixes=opts.passthrough_html_comment_prefixes,
        )
    warnings.extend(parse_warnings)
    wiki_out, render_warnings = render_jira_wiki(
        document,
        section=section,
        drop_leading_notice=drop_leading_notice,
        heading_promotion=heading_promotion,
    )
    warnings.extend(render_warnings)
    return ConversionResult(
        jira_wiki=wiki_out,
        warnings=tuple(warnings),
        document=document,
    )


# ---------------------------------------------------------------------------
# Jira wiki → Markdown (experimental, lossy)
# ---------------------------------------------------------------------------


def from_jira_wiki(
    source: str,
    *,
    options: ConversionOptions | None = None,
    macros: MacroRegistry | None = None,
) -> ConversionResult:
    """Parse Jira wiki markup and re-emit it as canonical Markdown.

    **Experimental and lossy.** The Jira wiki dialect is looser and
    less formally specified than the Confluence Storage Format, and
    several inline constructs (``{color}`` colour emphasis, ``~sub~``
    / ``+ins+`` / ``^sup^``, user mentions, ``{panel}`` styling) have
    no equivalent on the Markdown side and are dropped with warnings
    recorded on the returned :class:`ConversionResult`. See
    :mod:`cfxmark.parsers.jira_wiki` for the full contract.

    :param source: Raw Jira wiki markup text. Empty / ``None`` inputs
        return a :class:`ConversionResult` with an empty Markdown
        body and no warnings.
    :param options: Conversion options for the Markdown render phase.
        Style fields (``bullet_marker``, ``code_fence``) apply; the
        ``passthrough_html_comment_prefixes`` field is ignored
        because Jira wiki has no HTML comment syntax.
    :param macros: Macro registry to use. Defaults to
        :data:`cfxmark.macros.default_registry`.
    :returns: A :class:`ConversionResult` with ``markdown``,
        ``warnings``, ``attachments`` and ``document`` populated.
        ``attachments`` lists every ``[^filename]`` / ``!file!``
        referenced by the source in document order (deduplicated),
        so the caller can fetch the bytes out-of-band.
    """

    opts = options or DEFAULT_OPTIONS
    registry = macros or default_registry
    if not source:
        return ConversionResult(
            markdown="",
            warnings=(),
            attachments=(),
            document=Document(children=()),
        )
    document, parse_warnings, attachments = parse_jira_wiki(
        source, registry=registry
    )
    markdown = render_md(document, options=opts.md_options())
    return ConversionResult(
        markdown=markdown,
        warnings=tuple(parse_warnings),
        attachments=attachments,
        document=document,
    )


__all__ = [
    "ConversionOptions",
    "ConversionResult",
    "DEFAULT_OPTIONS",
    "to_cfx",
    "to_md",
    "to_jira_wiki",
    "from_jira_wiki",
]
