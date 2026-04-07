"""Public conversion API.

This module exposes the two public entry points — :func:`to_cfx` and
:func:`to_md` — plus the :class:`ConversionResult` dataclass they
return and the :class:`ConversionOptions` used to tweak their
behaviour.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from cfxmark.ast import Document
from cfxmark.macros import MacroRegistry, default_registry
from cfxmark.parsers.cfx import parse_cfx
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
    """

    bullet_marker: str = "-"
    code_fence: str = "```"

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


# ---------------------------------------------------------------------------
# Markdown → Confluence
# ---------------------------------------------------------------------------


def to_cfx(
    markdown: str,
    *,
    macros: MacroRegistry | None = None,
) -> ConversionResult:
    """Convert Markdown to Confluence Storage Format XHTML.

    :param markdown: Markdown source text.
    :param macros: Macro registry to use. Defaults to
        :data:`cfxmark.macros.default_registry`.
    :returns: A :class:`ConversionResult` with ``xhtml``,
        ``attachments``, ``warnings`` and ``document`` populated.

    There is no ``options`` parameter — Confluence storage XHTML has
    no per-call style choices to make. The Markdown side has style
    choices (bullet marker, code fence) that live on
    :class:`ConversionOptions`, which is only meaningful for
    :func:`to_md`.
    """

    registry = macros or default_registry
    document, parse_warnings = parse_md(markdown, registry=registry)
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


__all__ = [
    "ConversionOptions",
    "ConversionResult",
    "DEFAULT_OPTIONS",
    "to_cfx",
    "to_md",
]
