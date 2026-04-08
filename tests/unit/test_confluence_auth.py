"""Auth helper tests for cfxmark.confluence."""

from __future__ import annotations

import base64
import os
from pathlib import Path

import pytest

from cfxmark.confluence import BasicAuth, BearerToken, BearerTokenFile, EnvBearerToken


def test_bearer_token_headers() -> None:
    auth = BearerToken("mytoken123")
    h = auth.headers()
    assert h["Authorization"] == "Bearer mytoken123"


@pytest.mark.skipif(os.name != "posix", reason="chmod 600 check requires POSIX")
def test_bearer_token_file_rejects_world_readable(tmp_path: Path) -> None:
    token_file = tmp_path / "token.txt"
    token_file.write_text("secret")
    token_file.chmod(0o644)  # world-readable
    with pytest.raises(PermissionError, match="group/world readable"):
        BearerTokenFile(token_file)


@pytest.mark.skipif(os.name != "posix", reason="chmod 600 check requires POSIX")
def test_bearer_token_file_caches_on_init(tmp_path: Path) -> None:
    token_file = tmp_path / "token.txt"
    token_file.write_text("initial_token")
    token_file.chmod(0o600)
    auth = BearerTokenFile(token_file)
    # Modify file after construction — cached value must not change.
    token_file.write_text("updated_token")
    assert auth.headers()["Authorization"] == "Bearer initial_token"


def test_basic_auth_encodes_email_api_token() -> None:
    auth = BasicAuth("user@example.com", "my_api_token")
    h = auth.headers()
    expected_encoded = base64.b64encode(b"user@example.com:my_api_token").decode("ascii")
    assert h["Authorization"] == f"Basic {expected_encoded}"


def test_env_bearer_token_reads_on_each_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_TOKEN_VAR", "first_value")
    auth = EnvBearerToken("MY_TOKEN_VAR")
    assert auth.headers()["Authorization"] == "Bearer first_value"
    # Update env var — next call must see the new value.
    monkeypatch.setenv("MY_TOKEN_VAR", "second_value")
    assert auth.headers()["Authorization"] == "Bearer second_value"


def test_env_bearer_token_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MISSING_TOKEN_VAR", raising=False)
    auth = EnvBearerToken("MISSING_TOKEN_VAR")
    with pytest.raises(RuntimeError, match="MISSING_TOKEN_VAR"):
        auth.headers()
