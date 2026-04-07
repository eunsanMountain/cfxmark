"""Golden corpus tests against real Confluence pages.

These tests load Confluence storage XHTML samples placed in the
``tests/corpus/`` directory, push them through the full
``cfx → md → cfx`` round-trip, and assert that the canonical form
is byte-identical before and after. Drop your own ``.cfx`` files
into ``tests/corpus/`` to exercise this against your own Confluence
instance — the directory is gitignored so private content stays
local.

If a corpus file fails this test, the canonicalization layer in
:mod:`cfxmark.normalize` is the right place to teach the round-trip
about a new Confluence quirk.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import cfxmark

CORPUS_FILES = sorted(
    (Path(__file__).parent / "corpus").glob("*.cfx"),
)


@pytest.mark.corpus
@pytest.mark.skipif(
    not CORPUS_FILES,
    reason="No private corpus files in tests/corpus/. See README §Development.",
)
@pytest.mark.parametrize("path", CORPUS_FILES, ids=lambda p: p.name)
def test_round_trip_canonical(path: Path) -> None:
    original = path.read_text(encoding="utf-8")
    md_result = cfxmark.to_md(original)
    cfx_result = cfxmark.to_cfx(md_result.markdown)

    canonical_orig = cfxmark.canonicalize_cfx(original)
    canonical_back = cfxmark.canonicalize_cfx(cfx_result.xhtml)

    if canonical_orig != canonical_back:
        # Compute first diff position for easy debugging.
        diff_at = next(
            (
                i
                for i, (a, b) in enumerate(zip(canonical_orig, canonical_back))
                if a != b
            ),
            min(len(canonical_orig), len(canonical_back)),
        )
        ctx = 80
        before_orig = canonical_orig[max(0, diff_at - 30) : diff_at + ctx]
        before_back = canonical_back[max(0, diff_at - 30) : diff_at + ctx]
        pytest.fail(
            f"{path.name}: round-trip differs at byte {diff_at}\n"
            f"  orig: {before_orig!r}\n"
            f"  back: {before_back!r}"
        )
