from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_public_docs_describe_source_bound_informed_migration() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    spec = (ROOT / "docs" / "SPEC.md").read_text(encoding="utf-8")
    combined = readme + spec

    for stale in (
        "lossless opaque preservation for everything",
        "**Never dropped, never rewritten.**",
        "# lossless, Confluence side",
    ):
        assert stale not in combined

    for required in (
        "managed_markdown",
        "MigrationOccurrence.occurrence_id",
        "S0/M0/C0/M1/C1",
        "actual edit operation ID",
        "Stage membership alone is",
        "not ownership evidence",
        "validate_managed_cfx_artifact",
    ):
        assert required in combined


def test_package_description_names_informed_source_bound_contract() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert "informed migration reports and source-bound change proof" in pyproject


def test_package_has_no_proofless_remote_writer_surface() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert not (ROOT / "src" / "cfxmark" / "confluence.py").exists()
    assert "cfxmark.confluence" not in readme
    assert "confluence = []" not in pyproject
