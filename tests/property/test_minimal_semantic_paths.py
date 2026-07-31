"""The quadratic scan and its replacement must agree on every input.

`_minimal_semantic_paths` replaced a nested scan over relocated tombstones. The
rewrite is an argument about what the condition meant, not a heuristic, so the way
to check it is to keep the original algorithm here and require the two to return
the identical tuple -- order, duplicates and all.

That is a stronger check than a handful of examples: the rewrite's whole claim is
that only a path's own proper prefixes can exclude it, and a randomised search over
overlapping path sets is what would find a case where that is false.

Measured reason for the rewrite, on the 79-table document under cProfile:

    437,773,545  generator calls in the scan          66.7 s of a 286 s profile
    670,429,362  dataclass __eq__ calls               44.9 s

Both are quadratic in the number of relocated tombstones.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from cfxmark.artifacts import _minimal_semantic_paths


@dataclass(frozen=True)
class _Leaf:
    """Only the field the function reads. A real `StorageLeafRef` carries more, and
    none of it participates in the decision."""

    semantic_path: tuple[str, ...]


def _original(relocated: tuple[_Leaf, ...]) -> tuple[tuple[str, ...], ...]:
    """The algorithm as it was, kept verbatim as the oracle."""

    return tuple(
        leaf.semantic_path
        for leaf in relocated
        if not any(
            other.semantic_path != leaf.semantic_path
            and leaf.semantic_path[: len(other.semantic_path)] == other.semantic_path
            for other in relocated
        )
    )


def _paths(rng: random.Random, *, count: int, alphabet: str, depth: int) -> tuple[_Leaf, ...]:
    """Deliberately collision-prone: a small alphabet and shallow depth is what makes
    prefixes, duplicates and siblings all common."""

    return tuple(_Leaf(tuple(rng.choice(alphabet) for _ in range(rng.randrange(0, depth + 1)))) for _ in range(count))


def test_the_two_algorithms_agree_on_random_overlapping_path_sets() -> None:
    rng = random.Random(20260730)
    for _ in range(2000):
        relocated = _paths(
            rng,
            count=rng.randrange(0, 12),
            alphabet="ab",
            depth=rng.randrange(0, 4),
        )
        assert _minimal_semantic_paths(relocated) == _original(relocated), relocated


def test_the_two_algorithms_agree_on_deeper_paths() -> None:
    """A wider alphabet makes exact duplicates rare and long shared prefixes common,
    which is the shape a real document produces."""

    rng = random.Random(1)
    for _ in range(500):
        relocated = _paths(rng, count=rng.randrange(0, 20), alphabet="abcdef", depth=6)
        assert _minimal_semantic_paths(relocated) == _original(relocated), relocated


def test_the_cases_worth_naming() -> None:
    """The boundaries the rewrite's reasoning turns on, spelled out.

    A randomised search reaches these, but a reader should not have to trust that it
    did -- each line here is one step of the argument in the docstring.
    """

    # An empty path is a prefix of everything, including itself. It survives; the
    # others do not. This is why the prefix range starts at 0.
    assert _minimal_semantic_paths((_Leaf(()), _Leaf(("a",)))) == ((),)

    # Equal paths do not exclude each other, and both are reported.
    assert _minimal_semantic_paths((_Leaf(("a",)), _Leaf(("a",)))) == (("a",), ("a",))

    # A longer path cannot exclude a shorter one.
    assert _minimal_semantic_paths((_Leaf(("a", "b")), _Leaf(("a",)))) == (("a",),)

    # Siblings exclude nothing.
    assert _minimal_semantic_paths((_Leaf(("a",)), _Leaf(("b",)))) == (("a",), ("b",))

    # Order follows the input, not the paths.
    assert _minimal_semantic_paths((_Leaf(("b",)), _Leaf(("a",)))) == (("b",), ("a",))

    # Nothing in, nothing out.
    assert _minimal_semantic_paths(()) == ()


def test_the_oracle_can_disagree_when_the_rewrite_is_wrong() -> None:
    """A comparison against an oracle proves nothing if it cannot fail.

    So this asserts the oracle's own answer on a case where an obvious wrong
    rewrite -- checking all prefixes including the path itself -- would differ. If
    someone changes `range(len(path))` to `range(len(path) + 1)`, the equal-paths
    case above starts returning `()` and this states what the correct answer is.
    """

    relocated = (_Leaf(("a",)), _Leaf(("a",)))
    assert _original(relocated) == (("a",), ("a",))
    wrong = tuple(
        leaf.semantic_path
        for leaf in relocated
        if not any(
            leaf.semantic_path[:index] in {other.semantic_path for other in relocated}
            for index in range(len(leaf.semantic_path) + 1)
        )
    )
    assert wrong == ()
    assert wrong != _original(relocated)
