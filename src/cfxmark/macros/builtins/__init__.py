"""Built-in Confluence macro handlers shipped with cfxmark."""

from __future__ import annotations

from cfxmark.macros.builtins.admonition import AdmonitionHandler
from cfxmark.macros.builtins.expand import ExpandHandler
from cfxmark.macros.builtins.jira import JiraHandler
from cfxmark.macros.builtins.toc import TocHandler

__all__ = [
    "AdmonitionHandler",
    "ExpandHandler",
    "JiraHandler",
    "TocHandler",
]
