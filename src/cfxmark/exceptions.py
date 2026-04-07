"""Exception hierarchy for cfxmark."""

from __future__ import annotations


class CfxmarkError(Exception):
    """Base class for all cfxmark exceptions."""


class ParseError(CfxmarkError):
    """Raised when input cannot be parsed.

    * Markdown parsing errors (CommonMark violations)
    * XHTML parsing errors (malformed XML, namespace issues)
    """


class ConversionError(CfxmarkError):
    """Raised when a well-formed input cannot be converted.

    Typically means an AST was produced but it references something we
    cannot emit — for example, an inline unknown Confluence element that
    we cannot escalate to a block-level opaque fence.
    """


class MacroError(CfxmarkError):
    """Raised when a macro handler fails.

    * Missing required parameter
    * Forward or reverse function raised an unexpected error
    """
