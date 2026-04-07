"""Registry of Confluence macro handlers."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

import lxml.etree as ET

from cfxmark.ast import BlockNode, DirectiveMacro

# ---------------------------------------------------------------------------
# Handler protocol
# ---------------------------------------------------------------------------


BodyParser = Callable[[ET._Element], tuple[BlockNode, ...]]
"""Callback: convert an lxml ``rich-text-body`` element to AST blocks.

The parser instance plugs in at runtime so macros can recursively
parse body contents without a hard import-time dependency on the
main parser module.
"""


BodyRenderer = Callable[[tuple[BlockNode, ...]], list[ET._Element]]
"""Callback: convert AST blocks back to lxml elements for the CF body."""


class MacroHandler(Protocol):
    """Protocol that every macro handler implements.

    A handler is effectively a pair of functions — forward (cf → AST)
    and reverse (AST → cf) — packaged in an object so related state
    (e.g. the supported Confluence version) can live on the instance.
    """

    name: str
    """Confluence macro name, as in ``ac:name="..."``."""

    directive_name: str
    """Name used in the Markdown directive block (often ``name``)."""

    def from_cfx(
        self,
        element: ET._Element,
        parse_body: BodyParser,
    ) -> DirectiveMacro | None:
        """Convert a Confluence macro element to an AST :class:`DirectiveMacro`.

        Returning ``None`` means "I thought I could handle this but
        actually I can't" — the caller will fall back to the opaque
        passthrough mechanism.
        """
        ...

    def to_cfx(
        self,
        directive: DirectiveMacro,
        render_body: BodyRenderer,
    ) -> ET._Element:
        """Convert a :class:`DirectiveMacro` back to a Confluence macro element."""
        ...


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class MacroRegistry:
    """Collection of macro handlers keyed by Confluence macro name.

    Registries are mutable — callers may :meth:`register` custom
    handlers to extend support without monkey-patching.
    """

    def __init__(self) -> None:
        self._by_cfx_name: dict[str, MacroHandler] = {}
        self._by_directive_name: dict[str, MacroHandler] = {}

    def register(self, handler: MacroHandler) -> None:
        """Add a handler, replacing any previous entry with the same name.

        If a handler with the same Confluence ``name`` is already
        registered under a *different* ``directive_name``, the stale
        directive alias is removed so look-ups by directive name stay
        consistent with look-ups by Confluence name.
        """

        previous = self._by_cfx_name.get(handler.name)
        if previous is not None and previous.directive_name != handler.directive_name:
            self._by_directive_name.pop(previous.directive_name, None)
        self._by_cfx_name[handler.name] = handler
        self._by_directive_name[handler.directive_name] = handler

    def get_by_cfx_name(self, name: str) -> MacroHandler | None:
        """Look up a handler by its Confluence macro name."""

        return self._by_cfx_name.get(name)

    def get_by_directive_name(self, name: str) -> MacroHandler | None:
        """Look up a handler by its Markdown directive name."""

        return self._by_directive_name.get(name)

    def cfx_names(self) -> frozenset[str]:
        """Return the set of registered Confluence macro names."""

        return frozenset(self._by_cfx_name)

    def directive_names(self) -> frozenset[str]:
        """Return the set of registered directive names."""

        return frozenset(self._by_directive_name)

    def copy(self) -> MacroRegistry:
        """Return a shallow copy so callers can customize without side effects."""

        other = MacroRegistry()
        other._by_cfx_name = dict(self._by_cfx_name)
        other._by_directive_name = dict(self._by_directive_name)
        return other


# ---------------------------------------------------------------------------
# Default registry
# ---------------------------------------------------------------------------


def _build_default_registry() -> MacroRegistry:
    # Imports are local to avoid a cycle: registry → builtins → registry.
    from cfxmark.macros.builtins import (
        AdmonitionHandler,
        ExpandHandler,
        JiraHandler,
        TocHandler,
    )

    reg = MacroRegistry()
    # Admonition panels (info / note / warning / tip).
    for name in ("info", "note", "warning", "tip"):
        reg.register(AdmonitionHandler(name))
    reg.register(JiraHandler())
    reg.register(ExpandHandler())
    reg.register(TocHandler())
    return reg


default_registry = _build_default_registry()


__all__ = [
    "BodyParser",
    "BodyRenderer",
    "MacroHandler",
    "MacroRegistry",
    "default_registry",
]
