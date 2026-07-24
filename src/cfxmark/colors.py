"""Shared color classification for editable Confluence Markdown."""

from __future__ import annotations

import re

_COLOR_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "red",
        (
            "red",
            "danger",
            "#ae2e24",
            "#de350b",
            "#ff5630",
            "#ffeceb",
        ),
    ),
    (
        "amber",
        (
            "amber",
            "orange",
            "yellow",
            "warning",
            "#974f0c",
            "#ffab00",
            "#fff3eb",
        ),
    ),
    (
        "green",
        (
            "green",
            "success",
            "#216e4e",
            "#00875a",
            "#36b37e",
            "#dcfff1",
        ),
    ),
    (
        "blue",
        (
            "blue",
            "information",
            "#0055cc",
            "#0052cc",
            "#0065ff",
            "#e9f2ff",
        ),
    ),
    (
        "purple",
        (
            "purple",
            "#5e4db2",
            "#6554c0",
            "#f3f0ff",
        ),
    ),
    (
        "gray",
        (
            "grey",
            "gray",
            "#626f86",
            "#6b778c",
        ),
    ),
    (
        "inverse",
        ("inverse",),
    ),
)


def coarse_color(value: str) -> str | None:
    """Map a known editor color to a stable semantic category.

    ``None`` means the value is not in the known palette and must be
    preserved exactly instead of being guessed as a neutral color.
    """

    lowered = value.lower().replace(" ", "")
    words = set(re.findall(r"[a-z]+", lowered))
    for name, hints in _COLOR_HINTS:
        if any((hint in lowered if hint.startswith("#") else hint in words) for hint in hints):
            return name
    if "--ds-background-neutral" in lowered or "#f1f2f4" in lowered:
        return "gray"
    if lowered in {"neutral", "var(--ds-text,#172b4d)", "#172b4d"}:
        return "neutral"
    return None


__all__ = ["coarse_color"]
