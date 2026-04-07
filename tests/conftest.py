"""Shared pytest fixtures for cfxmark tests."""

from __future__ import annotations

from pathlib import Path

import pytest

CORPUS_DIR = Path(__file__).parent / "corpus"


@pytest.fixture
def corpus_dir() -> Path:
    return CORPUS_DIR
