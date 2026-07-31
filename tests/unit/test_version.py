"""Does this package agree with itself about what version it is?

Two questions, and only one of them was being asked.

The metadata check compares `__version__` against what is INSTALLED. In an editable
checkout that is whatever the last install wrote, so bumping `pyproject.toml` alone
keeps it passing -- and it fails on a stale environment that has no defect at all. It
is a useful check and it is not the one that matters at a release.

The pyproject check is. It reads the file a build reads, so it cannot go stale, and
it catches exactly the defect that reached a release candidate here: a wheel named
`cfxmark-0.5.2-py3-none-any.whl`, whose `METADATA` said `Version: 0.5.2`, carrying a
`_version.py` that said `0.5.1`. The wheel was built 104 seconds before the commit
that fixed the version module, and neither the filename nor the metadata gave any
sign of it. Every measurement taken against that wheel was attributed to the wrong
converter.

That matters beyond a printed string, because the version is a functional value.
atls stamps every managed manifest with `converter=f"cfxmark/{cfxmark.__version__}"`
and later validates against the same expression. Within one process the stamp and the
check agree, so no test could see a disagreement; across a release they do not, and a
manifest written by the release candidate would name a converter the published
package does not admit to being.

The sibling project (atlassian-skills) had the same defect, was given a test like the
one below, and its docstring says it exists because "the sibling project had this
defect and it was caught by a test like this one" -- meaning this one. The test was
never added here and the defect recurred. That is what this file closes.
"""

from __future__ import annotations

import re
from importlib.metadata import version as installed_version
from pathlib import Path

import pytest

from cfxmark import __version__

ROOT = Path(__file__).resolve().parents[2]

#: `version = "..."` inside the `[project]` table. Read with a regex rather than a
#: TOML parser on purpose: `tomllib` is 3.11+, this package supports 3.10, and
#: `tomli` is not a dependency. The first draft of this file branched on the
#: interpreter and SKIPPED on 3.10 -- and a mutation test caught it doing exactly
#: what its own docstring warned against: a version check that silently vanishes on
#: one interpreter is worse than one that says why. Setting `_version.py` back to
#: 0.5.1 passed the suite on the 3.10 venv.
#:
#: One line in one table is not a reason to take a dependency, and a regex that fails
#: to match raises here rather than skipping.
_PROJECT_VERSION = re.compile(
    r"^\[project\]$.*?^version\s*=\s*[\"']([^\"']+)[\"']",
    re.MULTILINE | re.DOTALL,
)


def _declared() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    found = _PROJECT_VERSION.search(text)
    assert found is not None, "could not find [project] version in pyproject.toml"
    return found.group(1)


def test_module_version_matches_distribution_metadata() -> None:
    """What is installed against what the module says.

    Skipped rather than failed when the installed metadata is behind the tree AND the
    tree agrees with `pyproject.toml`: that combination is a stale editable install,
    not a defect, and a test that goes red every time someone bumps the version
    before reinstalling is a test people learn to ignore. Any other disagreement
    still fails.
    """

    installed = installed_version("cfxmark")
    if installed != __version__ and _declared() == __version__:
        pytest.skip(
            f"installed metadata says {installed} and the tree says {__version__}; "
            "the tree agrees with pyproject.toml, so this is a stale editable install"
        )
    assert __version__ == installed


def test_module_version_matches_the_version_pyproject_declares() -> None:
    """Read from the file a build reads, so it cannot go stale.

    This is the check the 0.5.2 candidate needed and did not have. The module
    docstring says what its absence cost.
    """

    declared = _declared()
    assert __version__ == declared, (
        f"pyproject.toml says {declared} and cfxmark.__version__ says {__version__}; a release edits both or neither"
    )


def test_the_version_module_is_the_only_place_the_number_is_written() -> None:
    """`pyproject.toml` and `_version.py` are the two sites, checked against each
    other above. A third copy would be a third thing to forget.

    Derived from the source rather than from a list somebody maintains: any other
    file assigning a version literal shows up here as a failure naming it.
    """

    pattern = re.compile(r'^__version__\s*=\s*["\']', re.MULTILINE)
    writers = sorted(
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "src").rglob("*.py")
        if pattern.search(path.read_text(encoding="utf-8"))
    )
    assert writers == ["src/cfxmark/_version.py"], writers
