"""In-process fake HTTP server tests for cfxmark.confluence.ConfluenceClient.

# NOTE: test_canonicalize_cross_version_invariant is intentionally skipped
# here — see plan §3.0.4; requires an isolated cfxmark==0.1.4 install to
# generate the golden JSON, which is not available in this session.
"""

from __future__ import annotations

import contextlib
import http.server
import io
import json
import logging
import socket
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

import cfxmark
from cfxmark.confluence import (
    BearerToken,
    ConfluenceClient,
    ConfluenceVersionConflict,
    HTTPError,
)

# ---------------------------------------------------------------------------
# Fake HTTP server infrastructure
# ---------------------------------------------------------------------------


class FakeHandler(http.server.BaseHTTPRequestHandler):
    """Route-table-driven fake HTTP server handler."""

    routes: dict[tuple[str, str], Any] = {}

    def log_message(self, *args: Any, **kwargs: Any) -> None:  # silence logs
        pass

    def _respond(self, method: str) -> None:
        path_no_qs = self.path.split("?")[0]
        key = (method, path_no_qs)
        route = self.routes.get(key) or self.routes.get(("*", path_no_qs))
        if route is None:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'{"message":"not found"}')
            return
        if callable(route):
            status, body, headers = route(self)
        else:
            status, body, headers = route
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        if isinstance(body, bytes):
            self.wfile.write(body)
        else:
            self.wfile.write(body.encode())

    def do_GET(self) -> None:
        self._respond("GET")

    def do_POST(self) -> None:
        self._respond("POST")

    def do_PUT(self) -> None:
        self._respond("PUT")

    def do_DELETE(self) -> None:
        self._respond("DELETE")


@contextmanager
def fake_server(routes: dict[tuple[str, str], Any]):  # type: ignore[type-arg]
    """Start a local HTTP server with the given route table."""
    FakeHandler.routes = routes
    srv = http.server.HTTPServer(("127.0.0.1", 0), FakeHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CT_JSON = {"Content-Type": "application/json"}


def _page(page_id: str, title: str, xhtml: str, version: int = 1) -> dict[str, Any]:
    return {
        "id": page_id,
        "title": title,
        "version": {"number": version},
        "body": {"storage": {"value": xhtml}},
    }


def _attachments(*titles: str) -> dict[str, Any]:
    return {"results": [{"title": t, "_links": {"download": f"/dl/{t}"}} for t in titles]}


def _empty_attachments() -> dict[str, Any]:
    return {"results": []}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_get_page_success() -> None:
    page = _page("1", "Test", "<p>hello</p>")
    routes: dict[tuple[str, str], Any] = {
        ("GET", "/rest/api/content/1"): (200, json.dumps(page), _CT_JSON),
    }
    with fake_server(routes) as base_url:
        client = ConfluenceClient(host=base_url, auth=BearerToken("t"), dialect="server")
        result = client.get_page("1")
    assert result["id"] == "1"
    assert result["title"] == "Test"


def test_get_page_404() -> None:
    routes: dict[tuple[str, str], Any] = {
        ("GET", "/rest/api/content/99"): (404, '{"message":"not found"}', _CT_JSON),
    }
    with fake_server(routes) as base_url:
        client = ConfluenceClient(host=base_url, auth=BearerToken("t"))
        with pytest.raises(HTTPError) as exc_info:
            client.get_page("99")
    assert exc_info.value.status_code == 404


def test_update_page_success() -> None:
    page = _page("2", "P", "<p>old</p>", version=3)
    updated = _page("2", "P", "<p>new</p>", version=4)
    routes: dict[tuple[str, str], Any] = {
        ("GET", "/rest/api/content/2"): (200, json.dumps(page), _CT_JSON),
        ("PUT", "/rest/api/content/2"): (200, json.dumps(updated), _CT_JSON),
    }
    with fake_server(routes) as base_url:
        client = ConfluenceClient(host=base_url, auth=BearerToken("t"))
        result = client.update_page("2", "<p>new</p>", page=page)
    assert result["version"]["number"] == 4


def test_update_page_409_abort() -> None:
    page = _page("3", "P", "<p>remote</p>", version=1)
    routes: dict[tuple[str, str], Any] = {
        ("GET", "/rest/api/content/3"): (200, json.dumps(page), _CT_JSON),
        ("PUT", "/rest/api/content/3"): (409, '{"message":"conflict"}', _CT_JSON),
    }
    with fake_server(routes) as base_url:
        client = ConfluenceClient(host=base_url, auth=BearerToken("t"))
        with pytest.raises(ConfluenceVersionConflict):
            client.push_markdown("3", "# New\n", on_conflict="abort")


def test_update_page_409_retry_is_canonical_aware() -> None:
    """After 409, fresh remote equals local → no second PUT (no-op)."""
    md_text = "# Hello\n\nWorld.\n"
    local_xhtml = cfxmark.to_cfx(md_text).xhtml or ""

    page_v1 = _page("4", "P", "<p>OLD REMOTE CONTENT</p>", version=7)
    page_v2 = _page("4", "P", local_xhtml, version=8)  # now matches local

    get_call: list[int] = [0]

    def handle_get(handler: FakeHandler) -> tuple[int, str, dict[str, str]]:
        get_call[0] += 1
        if get_call[0] == 1:
            return (200, json.dumps(page_v1), _CT_JSON)
        return (200, json.dumps(page_v2), _CT_JSON)

    put_call: list[int] = [0]

    def handle_put(handler: FakeHandler) -> tuple[int, str, dict[str, str]]:
        put_call[0] += 1
        return (409, '{"message":"conflict"}', _CT_JSON)

    routes: dict[tuple[str, str], Any] = {
        ("GET", "/rest/api/content/4"): handle_get,
        ("PUT", "/rest/api/content/4"): handle_put,
        ("GET", "/rest/api/content/4/child/attachment"): (
            200, json.dumps(_empty_attachments()), _CT_JSON,
        ),
    }
    with fake_server(routes) as base_url:
        client = ConfluenceClient(host=base_url, auth=BearerToken("t"))
        result = client.push_markdown("4", md_text, on_conflict="retry")

    # Only one PUT attempted; after retry converged, no second PUT.
    assert put_call[0] == 1
    assert not result.changed


def test_update_page_409_retry_last_writer_wins() -> None:
    """After 409, fresh remote differs → second PUT wins."""
    md_text = "# Hello\n\nWorld.\n"
    local_xhtml = cfxmark.to_cfx(md_text).xhtml or ""
    page_v2_updated = _page("5", "P", local_xhtml, version=9)

    page_v1 = _page("5", "P", "<p>OLD</p>", version=7)
    page_v2 = _page("5", "P", "<p>DIFFERENT FRESH</p>", version=8)

    get_call: list[int] = [0]

    def handle_get(handler: FakeHandler) -> tuple[int, str, dict[str, str]]:
        get_call[0] += 1
        if get_call[0] == 1:
            return (200, json.dumps(page_v1), _CT_JSON)
        return (200, json.dumps(page_v2), _CT_JSON)

    put_call: list[int] = [0]

    def handle_put(handler: FakeHandler) -> tuple[int, str, dict[str, str]]:
        put_call[0] += 1
        if put_call[0] == 1:
            return (409, '{"message":"conflict"}', _CT_JSON)
        return (200, json.dumps(page_v2_updated), _CT_JSON)

    routes: dict[tuple[str, str], Any] = {
        ("GET", "/rest/api/content/5"): handle_get,
        ("PUT", "/rest/api/content/5"): handle_put,
        ("GET", "/rest/api/content/5/child/attachment"): (
            200, json.dumps(_empty_attachments()), _CT_JSON,
        ),
    }
    with fake_server(routes) as base_url:
        client = ConfluenceClient(host=base_url, auth=BearerToken("t"))
        result = client.push_markdown("5", md_text, on_conflict="retry")

    assert put_call[0] == 2
    assert result.changed
    assert result.new_version == 9


def test_update_page_409_force() -> None:
    """force always PUTs with the fresh parent regardless of canonical comparison."""
    md_text = "# Force\n"
    local_xhtml = cfxmark.to_cfx(md_text).xhtml or ""
    page_v2_updated = _page("6", "P", local_xhtml, version=10)

    page_v1 = _page("6", "P", "<p>OLD</p>", version=8)
    page_v2 = _page("6", "P", "<p>FRESH SAME AS OLD</p>", version=9)

    get_call: list[int] = [0]

    def handle_get(handler: FakeHandler) -> tuple[int, str, dict[str, str]]:
        get_call[0] += 1
        if get_call[0] == 1:
            return (200, json.dumps(page_v1), _CT_JSON)
        return (200, json.dumps(page_v2), _CT_JSON)

    put_call: list[int] = [0]

    def handle_put(handler: FakeHandler) -> tuple[int, str, dict[str, str]]:
        put_call[0] += 1
        if put_call[0] == 1:
            return (409, '{"message":"conflict"}', _CT_JSON)
        return (200, json.dumps(page_v2_updated), _CT_JSON)

    routes: dict[tuple[str, str], Any] = {
        ("GET", "/rest/api/content/6"): handle_get,
        ("PUT", "/rest/api/content/6"): handle_put,
        ("GET", "/rest/api/content/6/child/attachment"): (
            200, json.dumps(_empty_attachments()), _CT_JSON,
        ),
    }
    with fake_server(routes) as base_url:
        client = ConfluenceClient(host=base_url, auth=BearerToken("t"))
        result = client.push_markdown("6", md_text, on_conflict="force")

    # force always does the second PUT regardless of canonical comparison
    assert put_call[0] == 2
    assert result.changed


def test_push_markdown_no_op() -> None:
    """Canonical-equal body and no missing attachments → changed=False."""
    md_text = "# Hello\n\nWorld.\n"
    xhtml = cfxmark.to_cfx(md_text).xhtml or ""
    page = _page("10", "P", xhtml, version=5)

    routes: dict[tuple[str, str], Any] = {
        ("GET", "/rest/api/content/10"): (200, json.dumps(page), _CT_JSON),
        ("GET", "/rest/api/content/10/child/attachment"): (
            200, json.dumps(_empty_attachments()), _CT_JSON,
        ),
    }
    with fake_server(routes) as base_url:
        client = ConfluenceClient(host=base_url, auth=BearerToken("t"))
        result = client.push_markdown("10", md_text)

    assert not result.changed
    assert result.new_version == 5
    assert result.uploaded_attachments == ()


def test_push_markdown_uploads_missing_attachments(tmp_path: Path) -> None:
    """Missing attachment is uploaded even when body changes."""
    img = tmp_path / "img.png"
    img.write_bytes(b"FAKE_PNG")

    md_text = "# Hi\n\n![](img.png)\n"
    local_xhtml = cfxmark.to_cfx(md_text).xhtml or ""
    page_original = _page("11", "P", "<p>OLD</p>", version=2)
    page_updated = _page("11", "P", local_xhtml, version=3)

    upload_called: list[bool] = []

    def handle_upload(handler: FakeHandler) -> tuple[int, str, dict[str, str]]:
        cl = int(handler.headers.get("Content-Length", 0))
        handler.rfile.read(cl)  # consume body
        upload_called.append(True)
        return (200, json.dumps({"results": [{"title": "img.png"}]}), _CT_JSON)

    routes: dict[tuple[str, str], Any] = {
        ("GET", "/rest/api/content/11"): (200, json.dumps(page_original), _CT_JSON),
        ("PUT", "/rest/api/content/11"): (200, json.dumps(page_updated), _CT_JSON),
        ("GET", "/rest/api/content/11/child/attachment"): (
            200, json.dumps(_empty_attachments()), _CT_JSON,
        ),
        ("POST", "/rest/api/content/11/child/attachment"): handle_upload,
    }
    with fake_server(routes) as base_url:
        client = ConfluenceClient(host=base_url, auth=BearerToken("t"))
        result = client.push_markdown(
            "11", md_text, md_path=str(tmp_path / "page.md")
        )

    assert result.changed
    assert "img.png" in result.uploaded_attachments
    assert len(upload_called) == 1


def test_push_markdown_canonical_equal_still_uploads_missing_attachments(
    tmp_path: Path,
) -> None:
    """Attachment loop runs even when body is canonical-equal (R3-3 fix)."""
    img = tmp_path / "img.png"
    img.write_bytes(b"FAKE_PNG")

    md_text = "# Title\n\n![](img.png)\n"
    # Use the actual to_cfx output as the remote body → canonical-equal.
    local_xhtml = cfxmark.to_cfx(md_text).xhtml or ""
    page = _page("12", "P", local_xhtml, version=4)

    upload_called: list[bool] = []

    def handle_upload(handler: FakeHandler) -> tuple[int, str, dict[str, str]]:
        cl = int(handler.headers.get("Content-Length", 0))
        handler.rfile.read(cl)
        upload_called.append(True)
        return (200, json.dumps({"results": [{"title": "img.png"}]}), _CT_JSON)

    routes: dict[tuple[str, str], Any] = {
        ("GET", "/rest/api/content/12"): (200, json.dumps(page), _CT_JSON),
        # No PUT route — if push_markdown calls PUT, the server returns 404
        ("GET", "/rest/api/content/12/child/attachment"): (
            200, json.dumps(_empty_attachments()), _CT_JSON,
        ),
        ("POST", "/rest/api/content/12/child/attachment"): handle_upload,
    }
    with fake_server(routes) as base_url:
        client = ConfluenceClient(host=base_url, auth=BearerToken("t"))
        result = client.push_markdown(
            "12", md_text, md_path=str(tmp_path / "page.md")
        )

    # Body NOT changed (canonical-equal), but attachment was uploaded.
    assert result.changed  # attachment upload counts as changed
    assert result.uploaded_attachments == ("img.png",)
    assert len(upload_called) == 1


def test_push_markdown_partial_failure(tmp_path: Path) -> None:
    """One upload fails → has_partial_failure=True, other succeeds."""
    (tmp_path / "img1.png").write_bytes(b"PNG1")
    # img2.png intentionally NOT created → FileNotFoundError

    md_text = "![](img1.png)\n![](img2.png)\n"
    page_original = _page("13", "P", "<p>OLD</p>", version=1)
    local_xhtml = cfxmark.to_cfx(md_text).xhtml or ""
    page_updated = _page("13", "P", local_xhtml, version=2)

    def handle_upload(handler: FakeHandler) -> tuple[int, str, dict[str, str]]:
        cl = int(handler.headers.get("Content-Length", 0))
        handler.rfile.read(cl)
        return (200, json.dumps({"results": []}), _CT_JSON)

    routes: dict[tuple[str, str], Any] = {
        ("GET", "/rest/api/content/13"): (200, json.dumps(page_original), _CT_JSON),
        ("PUT", "/rest/api/content/13"): (200, json.dumps(page_updated), _CT_JSON),
        ("GET", "/rest/api/content/13/child/attachment"): (
            200, json.dumps(_empty_attachments()), _CT_JSON,
        ),
        ("POST", "/rest/api/content/13/child/attachment"): handle_upload,
    }
    with fake_server(routes) as base_url:
        client = ConfluenceClient(host=base_url, auth=BearerToken("t"))
        result = client.push_markdown(
            "13", md_text, md_path=str(tmp_path / "page.md")
        )

    assert result.has_partial_failure
    assert "img1.png" in result.uploaded_attachments
    failed_names = [name for name, _ in result.failed_attachments]
    assert "img2.png" in failed_names


def test_pull_markdown_with_sidecar_assets(tmp_path: Path) -> None:
    page_xhtml = '<p><ac:image><ri:attachment ri:filename="photo.png"/></ac:image></p>'
    png_bytes = b"FAKE_PNG_BYTES"
    page = _page("20", "P", page_xhtml, version=1)

    atts = {
        "results": [{
            "title": "photo.png",
            "_links": {"download": "/download/attachments/20/photo.png"},
        }]
    }

    routes: dict[tuple[str, str], Any] = {
        ("GET", "/rest/api/content/20"): (200, json.dumps(page), _CT_JSON),
        ("GET", "/rest/api/content/20/child/attachment"): (
            200, json.dumps(atts), _CT_JSON,
        ),
        ("GET", "/download/attachments/20/photo.png"): (
            200, png_bytes, {"Content-Type": "image/png"},
        ),
    }
    asset_dir = tmp_path / "assets"
    with fake_server(routes) as base_url:
        client = ConfluenceClient(host=base_url, auth=BearerToken("t"))
        result = client.pull_markdown(
            "20",
            resolve_assets_mode="sidecar",
            asset_dir=str(asset_dir),
        )

    assert (asset_dir / "photo.png").exists()
    assert (asset_dir / "photo.png").read_bytes() == png_bytes
    assert result.resolved_asset_count == 1


def test_upload_attachment_multipart_shape(tmp_path: Path) -> None:
    """Verify multipart body structure and XSRF header presence/absence."""
    img = tmp_path / "test.png"
    img.write_bytes(b"PNG_DATA_HERE")

    captured: dict[str, Any] = {}

    def handle_upload(handler: FakeHandler) -> tuple[int, str, dict[str, str]]:
        cl = int(handler.headers.get("Content-Length", 0))
        captured["body"] = handler.rfile.read(cl) if cl else b""
        captured["content_type"] = handler.headers.get("Content-Type", "")
        captured["x_atlassian_token"] = handler.headers.get("X-Atlassian-Token", "")
        return (200, json.dumps({"results": []}), _CT_JSON)

    routes: dict[tuple[str, str], Any] = {
        ("POST", "/rest/api/content/30/child/attachment"): handle_upload,
    }

    # Server dialect — X-Atlassian-Token must be present.
    with fake_server(routes) as base_url:
        client_server = ConfluenceClient(
            host=base_url, auth=BearerToken("t"), dialect="server"
        )
        client_server.upload_attachment("30", img)

    body = captured["body"]
    assert b"test.png" in body
    assert b"Content-Disposition" in body
    assert b"PNG_DATA_HERE" in body
    assert "multipart/form-data" in captured["content_type"]
    assert "boundary=" in captured["content_type"]
    assert captured["x_atlassian_token"].lower() == "no-check"

    # Cloud dialect — X-Atlassian-Token must be absent.
    captured.clear()
    with fake_server(routes) as base_url:
        client_cloud = ConfluenceClient(
            host=base_url, auth=BearerToken("t"), dialect="cloud"
        )
        client_cloud.upload_attachment("30", img)

    assert captured.get("x_atlassian_token", "") == ""


def test_timeout_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """CFXMARK_HTTP_TIMEOUT=0.001 causes a network error (status_code == -1)."""
    monkeypatch.setenv("CFXMARK_HTTP_TIMEOUT", "0.001")

    def slow_handler(handler: FakeHandler) -> tuple[int, str, dict[str, str]]:
        time.sleep(0.5)  # 500 ms >> 1 ms timeout
        return (200, "{}", _CT_JSON)

    routes: dict[tuple[str, str], Any] = {
        ("GET", "/rest/api/content/50"): slow_handler,
    }
    with fake_server(routes) as base_url:
        client = ConfluenceClient(host=base_url, auth=BearerToken("t"))
        with pytest.raises(HTTPError) as exc_info:
            client.get_page("50")
    assert exc_info.value.status_code == -1


def test_error_body_limit_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """CFXMARK_HTTP_ERROR_BODY_LIMIT=100 truncates the error body."""
    monkeypatch.setenv("CFXMARK_HTTP_ERROR_BODY_LIMIT", "100")
    large_body = json.dumps({"error": "x" * 500})  # > 100 chars

    routes: dict[tuple[str, str], Any] = {
        ("GET", "/rest/api/content/51"): (404, large_body, _CT_JSON),
    }
    with fake_server(routes) as base_url:
        client = ConfluenceClient(host=base_url, auth=BearerToken("t"))
        with pytest.raises(HTTPError) as exc_info:
            client.get_page("51")
    assert len(exc_info.value.body) <= 100


def test_network_error_returns_sentinel_status_code() -> None:
    """A connection that is immediately closed returns status_code == -1."""

    # Server that accepts and immediately closes without sending data.
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_sock.bind(("127.0.0.1", 0))
    port = server_sock.getsockname()[1]
    server_sock.listen(5)
    server_sock.settimeout(2.0)

    def close_immediately() -> None:
        try:
            while True:
                try:
                    conn, _ = server_sock.accept()
                    conn.close()
                except OSError:
                    break
        except Exception:
            pass

    t = threading.Thread(target=close_immediately, daemon=True)
    t.start()

    from cfxmark.confluence import _request

    try:
        with pytest.raises(HTTPError) as exc_info:
            _request("GET", f"http://127.0.0.1:{port}/test", headers={})
        assert exc_info.value.status_code == -1
    finally:
        server_sock.close()


def test_logging_no_stderr_writes() -> None:
    """Library must not write to sys.stderr during a normal push."""
    md_text = "# Silent\n"
    xhtml = cfxmark.to_cfx(md_text).xhtml or ""
    page = _page("60", "Silent", xhtml, version=1)

    routes: dict[tuple[str, str], Any] = {
        ("GET", "/rest/api/content/60"): (200, json.dumps(page), _CT_JSON),
        ("GET", "/rest/api/content/60/child/attachment"): (
            200, json.dumps(_empty_attachments()), _CT_JSON,
        ),
    }
    err_buf = io.StringIO()
    with fake_server(routes) as base_url:
        client = ConfluenceClient(host=base_url, auth=BearerToken("t"))
        with contextlib.redirect_stderr(err_buf):
            client.push_markdown("60", md_text)

    assert err_buf.getvalue() == "", f"Unexpected stderr: {err_buf.getvalue()!r}"


def test_client_rejects_non_http_scheme() -> None:
    with pytest.raises(ValueError, match="http"):
        ConfluenceClient(host="ftp://example.com", auth=BearerToken("t"))


def test_client_warns_on_plain_http(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="cfxmark.confluence"):
        ConfluenceClient(host="http://example.com", auth=BearerToken("t"))
    assert any("http://" in r.message for r in caplog.records)


def test_client_rejects_empty_netloc() -> None:
    with pytest.raises(ValueError, match="netloc"):
        ConfluenceClient(host="https://", auth=BearerToken("t"))


# ---------------------------------------------------------------------------
# push_markdown attachment-path validation (defense in depth)
# ---------------------------------------------------------------------------


def test_push_markdown_rejects_attachment_traversal_before_http() -> None:
    """A malicious ``![](../../../etc/passwd)`` markdown must be rejected
    BEFORE any HTTP request is issued — the validation runs against
    ``cfx_result.attachments`` (which preserves the raw traversal path
    even though the rendered XHTML strips to basename) right after
    ``to_cfx`` and before ``get_page``.
    """
    md_text = "![p](../../../etc/passwd)"
    # Empty route table — any HTTP call would 404, but we want to assert
    # that NO call is made at all. AssetSecurityError must fire first.
    routes: dict[tuple[str, str], Any] = {}
    with fake_server(routes) as base_url:
        client = ConfluenceClient(host=base_url, auth=BearerToken("t"))
        with pytest.raises(cfxmark.AssetSecurityError, match="parent traversal"):
            client.push_markdown("999", md_text)


def test_push_markdown_strict_filenames_false_bypasses_validation() -> None:
    """The escape hatch lets callers opt out of attachment validation.

    With ``strict_filenames=False`` the validation is skipped — the call
    proceeds to ``get_page`` (and would normally proceed to read the
    referenced file, which we don't actually want to do here). The
    fake server 404s the get_page so the call still raises, but with
    ``HTTPError(404)`` rather than ``AssetSecurityError``, proving the
    validation gate was bypassed.
    """
    md_text = "![p](../../../etc/passwd)"
    routes: dict[tuple[str, str], Any] = {}  # 404s everything
    with fake_server(routes) as base_url:
        client = ConfluenceClient(host=base_url, auth=BearerToken("t"))
        with pytest.raises(HTTPError) as excinfo:
            client.push_markdown("999", md_text, strict_filenames=False)
        assert excinfo.value.status_code == 404
        # AssetSecurityError would have fired before get_page if strict
        # were True; HTTP 404 proves we got past the validation gate.


# ---------------------------------------------------------------------------
# Codex review fixes — content-aware attachment sync, off-host download
# guard, raw OSError handling on attachment downloads
# ---------------------------------------------------------------------------


def _attachment_record(title: str, file_size: int, *, att_id: str = "att1") -> dict[str, Any]:
    """Build a Confluence attachment list entry with the given size."""
    return {
        "id": att_id,
        "type": "attachment",
        "title": title,
        "extensions": {"fileSize": file_size, "mediaType": "image/png"},
        "_links": {"download": f"/download/attachments/100/{title}?version=1"},
    }


def test_push_markdown_size_drift_re_uploads(tmp_path: Path) -> None:
    """When the local file size differs from the remote attachment, the
    upload runs even though the basename already exists on the page.
    Codex review §2 fix.
    """
    # Local file: 10 bytes
    img = tmp_path / "logo.png"
    img.write_bytes(b"0123456789")
    md_path = tmp_path / "doc.md"
    md_path.write_text("![logo](logo.png)\n")
    md_text = md_path.read_text()

    page = _page("100", "Doc", "<p>existing</p>", version=1)
    upload_log: list[bytes] = []

    def upload_handler(handler: Any) -> tuple[int, str, dict[str, str]]:
        clen = int(handler.headers.get("Content-Length", "0"))
        upload_log.append(handler.rfile.read(clen))
        return 200, json.dumps({"id": "att2"}), _CT_JSON

    routes: dict[tuple[str, str], Any] = {
        ("GET", "/rest/api/content/100"): (200, json.dumps(page), _CT_JSON),
        ("GET", "/rest/api/content/100/child/attachment"): (
            # Remote says fileSize=999 (mismatch with local 10) → must re-upload
            200,
            json.dumps({"results": [_attachment_record("logo.png", 999)]}),
            _CT_JSON,
        ),
        ("PUT", "/rest/api/content/100"): (
            200, json.dumps(_page("100", "Doc", "<p>new</p>", version=2)), _CT_JSON,
        ),
        ("POST", "/rest/api/content/100/child/attachment"): upload_handler,
    }
    with fake_server(routes) as base_url:
        client = ConfluenceClient(host=base_url, auth=BearerToken("t"))
        result = client.push_markdown("100", md_text, md_path=md_path)

    assert result.uploaded_attachments == ("logo.png",)
    assert len(upload_log) == 1


def test_push_markdown_size_match_skips_upload(tmp_path: Path) -> None:
    """When the local file size matches the remote attachment, the
    upload is skipped — content is assumed identical.
    """
    img = tmp_path / "logo.png"
    img.write_bytes(b"0123456789")  # 10 bytes
    md_path = tmp_path / "doc.md"
    md_path.write_text("![logo](logo.png)\n")
    md_text = md_path.read_text()

    page = _page("100", "Doc", "<p>existing</p>", version=1)
    upload_log: list[bytes] = []

    def upload_handler(handler: Any) -> tuple[int, str, dict[str, str]]:
        clen = int(handler.headers.get("Content-Length", "0"))
        upload_log.append(handler.rfile.read(clen))
        return 200, json.dumps({"id": "att2"}), _CT_JSON

    routes: dict[tuple[str, str], Any] = {
        ("GET", "/rest/api/content/100"): (200, json.dumps(page), _CT_JSON),
        ("GET", "/rest/api/content/100/child/attachment"): (
            # Remote fileSize=10 (matches local) → skip upload
            200,
            json.dumps({"results": [_attachment_record("logo.png", 10)]}),
            _CT_JSON,
        ),
        ("PUT", "/rest/api/content/100"): (
            200, json.dumps(_page("100", "Doc", "<p>new</p>", version=2)), _CT_JSON,
        ),
    }
    with fake_server(routes) as base_url:
        client = ConfluenceClient(host=base_url, auth=BearerToken("t"))
        result = client.push_markdown("100", md_text, md_path=md_path)

    assert result.uploaded_attachments == ()
    assert upload_log == []  # POST never reached


def test_push_markdown_missing_remote_filesize_re_uploads(tmp_path: Path) -> None:
    """If the remote attachment record omits ``extensions.fileSize`` (some
    Confluence configurations or older versions), the convergent
    default is to re-upload — never assume "same name = same bytes"
    without size proof.
    """
    img = tmp_path / "logo.png"
    img.write_bytes(b"data")
    md_path = tmp_path / "doc.md"
    md_path.write_text("![logo](logo.png)\n")
    md_text = md_path.read_text()

    page = _page("100", "Doc", "<p>existing</p>", version=1)
    upload_log: list[bytes] = []

    def upload_handler(handler: Any) -> tuple[int, str, dict[str, str]]:
        clen = int(handler.headers.get("Content-Length", "0"))
        upload_log.append(handler.rfile.read(clen))
        return 200, json.dumps({"id": "att2"}), _CT_JSON

    # Build a record with no extensions.fileSize at all
    record_no_size = {
        "id": "att1",
        "title": "logo.png",
        "extensions": {},
        "_links": {"download": "/download/attachments/100/logo.png"},
    }
    routes: dict[tuple[str, str], Any] = {
        ("GET", "/rest/api/content/100"): (200, json.dumps(page), _CT_JSON),
        ("GET", "/rest/api/content/100/child/attachment"): (
            200, json.dumps({"results": [record_no_size]}), _CT_JSON,
        ),
        ("PUT", "/rest/api/content/100"): (
            200, json.dumps(_page("100", "Doc", "<p>new</p>", version=2)), _CT_JSON,
        ),
        ("POST", "/rest/api/content/100/child/attachment"): upload_handler,
    }
    with fake_server(routes) as base_url:
        client = ConfluenceClient(host=base_url, auth=BearerToken("t"))
        result = client.push_markdown("100", md_text, md_path=md_path)

    assert result.uploaded_attachments == ("logo.png",)
    assert len(upload_log) == 1


def test_attachment_fetcher_rejects_off_host_download_url(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An absolute ``_links.download`` pointing at an off-host URL must
    NOT receive the caller's bearer credential. Codex review §1 fix.
    """
    routes: dict[tuple[str, str], Any] = {
        ("GET", "/rest/api/content/100/child/attachment"): (
            200,
            json.dumps({
                "results": [{
                    "id": "att1",
                    "title": "logo.png",
                    "extensions": {"fileSize": 4},
                    "_links": {"download": "https://evil.example.com/steal/logo.png"},
                }]
            }),
            _CT_JSON,
        ),
    }
    with fake_server(routes) as base_url:
        client = ConfluenceClient(host=base_url, auth=BearerToken("secret-token"))
        fetcher = client.attachment_fetcher("100")
        with caplog.at_level(logging.WARNING, logger="cfxmark.confluence"):
            result = fetcher("logo.png")
    assert result is None
    assert any(
        "off-host" in r.message or "refusing" in r.message
        for r in caplog.records
    ), f"expected off-host warning, got: {[r.message for r in caplog.records]}"


def test_attachment_fetcher_accepts_same_origin_absolute_url(tmp_path: Path) -> None:
    """An absolute download URL is accepted when its netloc matches
    ``self._host`` — Confluence Server installations sometimes return
    fully-qualified URLs.
    """
    # We need to know the fake server URL up front to seed the absolute link.
    # Spin up the server first with an empty route table, then patch routes.
    srv = http.server.HTTPServer(("127.0.0.1", 0), FakeHandler)
    base_url = f"http://127.0.0.1:{srv.server_address[1]}"
    abs_download = f"{base_url}/download/attachments/100/logo.png"
    FakeHandler.routes = {
        ("GET", "/rest/api/content/100/child/attachment"): (
            200,
            json.dumps({
                "results": [{
                    "id": "att1",
                    "title": "logo.png",
                    "extensions": {"fileSize": 5},
                    "_links": {"download": abs_download},
                }]
            }),
            _CT_JSON,
        ),
        ("GET", "/download/attachments/100/logo.png"): (200, b"hello", {}),
    }
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        client = ConfluenceClient(host=base_url, auth=BearerToken("t"))
        fetcher = client.attachment_fetcher("100")
        assert fetcher("logo.png") == b"hello"
    finally:
        srv.shutdown()
        srv.server_close()


def test_attachment_fetcher_handles_raw_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    """Codex review §3 fix: a raw ``OSError`` from urlopen (Python
    3.11+ ``TimeoutError`` / ``RemoteDisconnected``) must be caught and
    surfaced as ``None``, not propagated up to abort the whole pull.
    """
    routes: dict[tuple[str, str], Any] = {
        ("GET", "/rest/api/content/100/child/attachment"): (
            200,
            json.dumps({
                "results": [{
                    "id": "att1",
                    "title": "logo.png",
                    "extensions": {"fileSize": 4},
                    "_links": {"download": "/download/attachments/100/logo.png"},
                }]
            }),
            _CT_JSON,
        ),
    }
    with fake_server(routes) as base_url:
        client = ConfluenceClient(host=base_url, auth=BearerToken("t"))
        fetcher = client.attachment_fetcher("100")

        # Force urlopen to raise raw TimeoutError (subclass of OSError),
        # mimicking the Python 3.11+ http.client behavior on a flaky
        # post-handshake disconnect.
        import urllib.request as ur

        def raise_timeout(*_a: Any, **_kw: Any) -> Any:
            raise TimeoutError("simulated read timeout")

        monkeypatch.setattr(ur, "urlopen", raise_timeout)
        result = fetcher("logo.png")

    # Must return None, not raise — same partial-failure contract as
    # the rest of the network surface.
    assert result is None
