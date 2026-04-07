"""Macro registry and built-in handlers.

A **macro handler** knows how to translate a specific Confluence macro
(``ac:structured-macro ac:name="..."``) to and from a cfxmark
:class:`~cfxmark.ast.DirectiveMacro` AST node, which in turn renders as
an explicit Markdown directive.

Handlers are registered in a :class:`MacroRegistry`. Unknown macros
fall through to the opaque-passthrough mechanism (see
:mod:`cfxmark.opaque`).
"""

from __future__ import annotations

from cfxmark.macros.registry import MacroHandler, MacroRegistry, default_registry

__all__ = ["MacroHandler", "MacroRegistry", "default_registry"]
