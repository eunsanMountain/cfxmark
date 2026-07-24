"""Recover every diagnostic code the package can emit, including dynamic ones.

The first inventory gate only understood ``make_diagnostic(code="literal")``. That
was enough to catch the original defect but not enough to be a real inventory:
five codes reach ``make_diagnostic`` through a variable and were therefore
invisible to it, so they sat outside the effect registry and silently resolved to
fatal — the exact failure the gate was built to prevent, one layer down.

So this module resolves the ``code`` argument instead of matching its syntax. It
walks a small abstract domain (strings, tuples of strings, unknown) through the
assignments, conditionals, tuple unpacking, loop targets and dict lookups that
actually feed those calls. Anything it cannot resolve is reported as unresolved
rather than skipped, because a silently skipped producer is how the registry
drifted in the first place.
"""

from __future__ import annotations

import ast
import pathlib
from dataclasses import dataclass, field
from typing import Any

# An abstract value is a set of possibilities. Each possibility is either a
# concrete string, a tuple of abstract values, NONE, or UNKNOWN.
#
# NONE is kept apart from UNKNOWN on purpose. A dict lookup that misses yields
# None, and every such lookup here is guarded by `if spec is None: continue`
# before the call. `make_diagnostic(code: str)` is typed and mypy passes over
# src/, so a None possibility provably cannot reach a code argument — it is a
# resolved dead branch, not an unresolved producer. UNKNOWN is the real failure
# signal: an expression this resolver could not follow at all.
UNKNOWN = object()
NONE = object()
AbstractValue = frozenset  # frozenset[str | tuple[AbstractValue, ...] | object]


@dataclass(frozen=True)
class CodeCallsite:
    """One ``make_diagnostic`` call and the codes it can pass."""

    origin: str
    line: int
    literal: bool
    codes: frozenset[str]
    unresolved: bool


@dataclass
class _Module:
    path: pathlib.Path
    origin: str
    tree: ast.AST
    parents: dict[ast.AST, ast.AST] = field(default_factory=dict)


def _index_parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _enclosing_scope(module: _Module, node: ast.AST) -> ast.AST:
    """The function a node lives in, or the module when it is top level."""
    current: ast.AST | None = node
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current
        current = module.parents.get(current)
    return module.tree


def _target_path(target: ast.AST, name: str) -> tuple[int, ...] | None:
    """Where ``name`` sits inside an assignment target, e.g. ``a, (b, c) = ...``."""
    if isinstance(target, ast.Name):
        return () if target.id == name else None
    if isinstance(target, (ast.Tuple, ast.List)):
        for index, element in enumerate(target.elts):
            sub = _target_path(element, name)
            if sub is not None:
                return (index, *sub)
    return None


def _index(value: AbstractValue, position: int) -> AbstractValue:
    """Project a set of tuples onto one of their positions."""
    out: set[Any] = set()
    for item in value:
        if isinstance(item, tuple) and position < len(item):
            out.update(item[position])
        elif item is NONE:
            out.add(NONE)  # unpacking None is a guarded dead branch, not a producer
        else:
            out.add(UNKNOWN)
    return frozenset(out)


def _follow(value: AbstractValue, path: tuple[int, ...]) -> AbstractValue:
    for position in path:
        value = _index(value, position)
    return value


def _lift(runtime: Any) -> Any:
    """Turn a real Python value from a known constant table into an abstract one."""
    if isinstance(runtime, str):
        return runtime
    if isinstance(runtime, tuple):
        return tuple(frozenset({_lift(item)}) for item in runtime)
    return UNKNOWN


class DiagnosticExtractor:
    def __init__(self, src: pathlib.Path, *, tables: dict[str, Any] | None = None) -> None:
        self._src = src
        self._tables = tables or {}
        self._modules: list[_Module] = []
        for path in sorted(src.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            module = _Module(path=path, origin=str(path.relative_to(src.parent)), tree=tree)
            module.parents = _index_parents(tree)
            self._modules.append(module)

    # -- public ----------------------------------------------------------

    def callsites(self) -> list[CodeCallsite]:
        found: list[CodeCallsite] = []
        for module in self._modules:
            for node in ast.walk(module.tree):
                if not isinstance(node, ast.Call):
                    continue
                name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
                if name != "make_diagnostic":
                    continue
                argument = next((kw.value for kw in node.keywords if kw.arg == "code"), None)
                if argument is None:
                    found.append(
                        CodeCallsite(module.origin, node.lineno, literal=False, codes=frozenset(), unresolved=True)
                    )
                    continue
                literal = isinstance(argument, ast.Constant)
                value = self._resolve(module, argument, set())
                codes = frozenset(item for item in value if isinstance(item, str))
                # NONE is a guarded dead branch (see the module docstring); only a
                # genuinely unfollowable expression counts as unresolved.
                unresolved = any(item is UNKNOWN for item in value) or not codes
                found.append(CodeCallsite(module.origin, node.lineno, literal, codes, unresolved))
        return found

    # -- resolution ------------------------------------------------------

    def _resolve(self, module: _Module, node: ast.AST, seen: set[Any]) -> AbstractValue:
        key = (id(module), id(node))
        if key in seen:
            return frozenset()
        seen = seen | {key}

        if isinstance(node, ast.Constant):
            return frozenset({node.value}) if isinstance(node.value, str) else frozenset({UNKNOWN})
        if isinstance(node, ast.IfExp):
            return self._resolve(module, node.body, seen) | self._resolve(module, node.orelse, seen)
        if isinstance(node, ast.Tuple):
            return frozenset({tuple(self._resolve(module, element, seen) for element in node.elts)})
        if isinstance(node, ast.Name):
            return self._resolve_name(module, node, seen)
        if isinstance(node, ast.Attribute):
            return self._resolve_attribute(module, node)
        if isinstance(node, ast.Call):
            return self._resolve_call(module, node, seen)
        if isinstance(node, ast.Subscript):
            return self._resolve_subscript(node)
        return frozenset({UNKNOWN})

    def _resolve_call(self, module: _Module, node: ast.Call, seen: set[Any]) -> AbstractValue:
        # TABLE.get(key) / TABLE.get(key, default) over a known constant table.
        if isinstance(node.func, ast.Attribute) and node.func.attr == "get":
            table = self._table_of(node.func.value)
            if table is not None:
                out: set[Any] = {_lift(value) for value in table.values()}
                if len(node.args) > 1:
                    out.update(self._resolve(module, node.args[1], seen))
                else:
                    out.add(NONE)  # a miss yields None, which the caller must guard
                return frozenset(out)
        return frozenset({UNKNOWN})

    def _resolve_subscript(self, node: ast.Subscript) -> AbstractValue:
        table = self._table_of(node.value)
        if table is not None:
            return frozenset(_lift(value) for value in table.values())
        return frozenset({UNKNOWN})

    def _table_of(self, node: ast.AST) -> dict[Any, Any] | None:
        if isinstance(node, ast.Name):
            candidate = self._tables.get(node.id)
            if isinstance(candidate, dict):
                return candidate
        return None

    def _resolve_attribute(self, module: _Module, node: ast.Attribute) -> AbstractValue:
        """Resolve ``error.code`` by finding the class the name was bound to."""
        if node.attr != "code" or not isinstance(node.value, ast.Name):
            return frozenset({UNKNOWN})
        bound = node.value.id
        current: ast.AST | None = node
        while current is not None:
            if isinstance(current, ast.Try):
                for handler in current.handlers:
                    if handler.name == bound and handler.type is not None:
                        return self._class_attribute(handler.type, "code")
            current = module.parents.get(current)
        return frozenset({UNKNOWN})

    def _class_attribute(self, type_node: ast.AST, attribute: str) -> AbstractValue:
        names: list[str] = []
        if isinstance(type_node, ast.Name):
            names = [type_node.id]
        elif isinstance(type_node, ast.Tuple):
            names = [element.id for element in type_node.elts if isinstance(element, ast.Name)]
        if not names:
            return frozenset({UNKNOWN})

        out: set[Any] = set()
        for wanted in names:
            hits = self._classes(wanted)
            if not hits:
                out.add(UNKNOWN)
            for definition in hits:
                out.update(self._class_attribute_value(definition, attribute))
        return frozenset(out)

    def _classes(self, name: str) -> list[ast.ClassDef]:
        return [
            node
            for module in self._modules
            for node in ast.walk(module.tree)
            if isinstance(node, ast.ClassDef) and node.name == name
        ]

    def _class_attribute_value(self, definition: ast.ClassDef, attribute: str) -> AbstractValue:
        out: set[Any] = set()
        for statement in definition.body:
            if isinstance(statement, ast.Assign):
                for target in statement.targets:
                    if isinstance(target, ast.Name) and target.id == attribute:
                        if isinstance(statement.value, ast.Constant) and isinstance(statement.value.value, str):
                            out.add(statement.value.value)
                        else:
                            out.add(UNKNOWN)
            elif isinstance(statement, ast.AnnAssign):
                target = statement.target
                if isinstance(target, ast.Name) and target.id == attribute and statement.value is not None:
                    if isinstance(statement.value, ast.Constant) and isinstance(statement.value.value, str):
                        out.add(statement.value.value)
                    else:
                        out.add(UNKNOWN)
        if not out:
            # Inherited attributes: fall back to the base classes.
            for base in definition.bases:
                if isinstance(base, ast.Name):
                    for parent in self._classes(base.id):
                        out.update(self._class_attribute_value(parent, attribute))
        return frozenset(out) if out else frozenset({UNKNOWN})

    def _resolve_name(self, module: _Module, node: ast.Name, seen: set[Any]) -> AbstractValue:
        scope = _enclosing_scope(module, node)
        out: set[Any] = set()
        matched = False

        for statement in ast.walk(scope):
            if isinstance(statement, ast.Assign):
                for target in statement.targets:
                    path = _target_path(target, node.id)
                    if path is None:
                        continue
                    matched = True
                    out.update(_follow(self._resolve(module, statement.value, seen), path))
            elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
                path = _target_path(statement.target, node.id)
                if path is not None:
                    matched = True
                    out.update(_follow(self._resolve(module, statement.value, seen), path))
            elif isinstance(statement, (ast.For, ast.AsyncFor)):
                path = _target_path(statement.target, node.id)
                if path is not None:
                    matched = True
                    element = self._iterate(module, statement.iter, seen)
                    out.update(_follow(element, path))
            elif isinstance(statement, (ast.GeneratorExp, ast.ListComp, ast.SetComp, ast.DictComp)):
                # `make_diagnostic(code=code) for ordinal, (code, ...) in enumerate(specs)`
                # binds its loop variables here rather than in an ast.For.
                for generator in statement.generators:
                    path = _target_path(generator.target, node.id)
                    if path is not None:
                        matched = True
                        element = self._iterate(module, generator.iter, seen)
                        out.update(_follow(element, path))

        if not matched:
            # A module-level constant or an argument we cannot see from here.
            out.add(UNKNOWN)
        return frozenset(out)

    def _iterate(self, module: _Module, node: ast.AST, seen: set[Any]) -> AbstractValue:
        """The abstract value of one element produced by iterating ``node``."""
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "enumerate" and node.args:
            inner = self._iterate(module, node.args[0], seen)
            return frozenset({(frozenset({UNKNOWN}), inner)})
        if isinstance(node, (ast.Tuple, ast.List)):
            out: set[Any] = set()
            for element in node.elts:
                if isinstance(element, ast.Starred):
                    out.update(self._iterate(module, element.value, seen))
                else:
                    out.update(self._resolve(module, element, seen))
            return frozenset(out)
        if isinstance(node, ast.Name):
            return self._elements_of(module, node, seen)
        return frozenset({UNKNOWN})

    def _elements_of(self, module: _Module, node: ast.Name, seen: set[Any]) -> AbstractValue:
        """Elements a list variable can hold, gathered from its assignments and appends."""
        scope = _enclosing_scope(module, node)
        out: set[Any] = set()
        matched = False

        for statement in ast.walk(scope):
            if isinstance(statement, ast.Assign):
                for target in statement.targets:
                    if isinstance(target, ast.Name) and target.id == node.id:
                        matched = True
                        out.update(self._iterate(module, statement.value, seen))
            elif isinstance(statement, ast.AnnAssign):
                target = statement.target
                if isinstance(target, ast.Name) and target.id == node.id:
                    matched = True
                    if statement.value is not None:
                        out.update(self._iterate(module, statement.value, seen))
            elif isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
                call = statement.value
                if not isinstance(call.func, ast.Attribute) or not isinstance(call.func.value, ast.Name):
                    continue
                if call.func.value.id != node.id or not call.args:
                    continue
                if call.func.attr == "append":
                    matched = True
                    out.update(self._resolve(module, call.args[0], seen))
                elif call.func.attr == "extend":
                    matched = True
                    out.update(self._iterate(module, call.args[0], seen))

        if not matched:
            out.add(UNKNOWN)
        return frozenset(out)
