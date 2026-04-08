"""cfxmark — bidirectional Markdown ↔ Confluence Storage XHTML converter."""

from cfxmark._version import __version__
from cfxmark.api import (
    DEFAULT_OPTIONS,
    ConversionOptions,
    ConversionResult,
    from_jira_wiki,
    to_cfx,
    to_jira_wiki,
    to_md,
)
from cfxmark.assets import AssetFetcher, ResolveMode, resolve_assets
from cfxmark.exceptions import AssetSecurityError, CfxmarkError, ConversionError, MacroError, ParseError
from cfxmark.macros import MacroRegistry, default_registry
from cfxmark.normalize import canonicalize_cfx, normalize_md, strip_passthrough_comments

__all__ = [
    "__version__",
    "ConversionOptions",
    "ConversionResult",
    "DEFAULT_OPTIONS",
    "to_cfx",
    "to_jira_wiki",
    "from_jira_wiki",
    "to_md",
    "AssetFetcher",
    "ResolveMode",
    "resolve_assets",
    "AssetSecurityError",
    "CfxmarkError",
    "ConversionError",
    "MacroError",
    "ParseError",
    "MacroRegistry",
    "default_registry",
    "canonicalize_cfx",
    "normalize_md",
    "strip_passthrough_comments",
]
