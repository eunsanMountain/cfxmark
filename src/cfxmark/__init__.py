"""cfxmark — bidirectional Markdown ↔ Confluence Storage XHTML converter."""

from cfxmark._version import __version__
from cfxmark.api import (
    DEFAULT_OPTIONS,
    ConversionOptions,
    ConversionResult,
    to_cfx,
    to_md,
)
from cfxmark.assets import AssetFetcher, ResolveMode, resolve_assets
from cfxmark.exceptions import CfxmarkError, ConversionError, MacroError, ParseError
from cfxmark.macros import MacroRegistry, default_registry
from cfxmark.normalize import canonicalize_cfx, normalize_md

__all__ = [
    "__version__",
    "ConversionOptions",
    "ConversionResult",
    "DEFAULT_OPTIONS",
    "to_cfx",
    "to_md",
    "AssetFetcher",
    "ResolveMode",
    "resolve_assets",
    "CfxmarkError",
    "ConversionError",
    "MacroError",
    "ParseError",
    "MacroRegistry",
    "default_registry",
    "canonicalize_cfx",
    "normalize_md",
]
