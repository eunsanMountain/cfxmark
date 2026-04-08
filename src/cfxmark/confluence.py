"""cfxmark.confluence — optional Confluence REST client.

Production pitfalls handled in one place so every call site inherits
them:

* urllib.error.URLError is the PARENT of HTTPError. Catch HTTPError
  first, otherwise a timeout gets classified as an HTTP 4xx.
* urlopen has no default timeout. Always pass an explicit timeout or
  production will deadlock on a dropped socket.
* Jira / Confluence 2xx responses can have empty bodies.
  ``json.loads("")`` raises — gate the decode on ``text and expect_json``.
* Confluence Server/DC rejects attachment uploads with a blanket 403
  unless the request carries the ``X-Atlassian-Token: no-check``
  header. Confluence Cloud does not need (and may not tolerate) this
  header — client.dialect controls it.
* Multipart form-data boundary collision: if the sentinel appears in
  the uploaded file bytes, the parse breaks. Randomise the boundary
  via ``secrets.token_hex(16)`` — collision probability is
  negligible.
"""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import os
import secrets
import stat
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import urlparse

from cfxmark.api import to_cfx, to_md
from cfxmark.assets import AssetFetcher, _validate_filename, resolve_assets
from cfxmark.exceptions import CfxmarkError
from cfxmark.normalize import canonicalize_cfx

_log = logging.getLogger("cfxmark.confluence")


# ── Exceptions ─────────────────────────────────────────────────────────


class HTTPError(CfxmarkError):
    """Raised on any non-2xx HTTP response or network failure.

    ``status_code`` is -1 for network failures (DNS, timeout,
    connection refused), otherwise the HTTP status.

    The response body is truncated to ``CFXMARK_HTTP_ERROR_BODY_LIMIT``
    (default 4096 bytes) so the exception message stays usable.
    """

    def __init__(
        self,
        *,
        status_code: int,
        method: str,
        url: str,
        body: str,
    ) -> None:
        self.status_code = status_code
        self.method = method
        self.url = url
        self.body = body[: get_error_body_limit()]
        super().__init__(f"HTTP {status_code} {method} {url}\n  {self.body[:400]}")


class ConfluenceVersionConflict(HTTPError):
    """Raised on HTTP 409 from update_page.

    ``current_version`` is reserved for future server-version parsing
    out of the error body. The current implementation always sets it
    to ``None`` — callers can rely on the field existing but should
    treat ``None`` as the only realistic value for now.
    """

    def __init__(self, *, current_version: int | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.current_version = current_version


# ── Timeout and limits ─────────────────────────────────────────────────


def get_timeout() -> float:
    """Read HTTP timeout from CFXMARK_HTTP_TIMEOUT env (default 30s)."""
    raw = os.environ.get("CFXMARK_HTTP_TIMEOUT", "30")
    try:
        return float(raw)
    except ValueError:
        return 30.0


def get_error_body_limit() -> int:
    """Read error body truncation limit from CFXMARK_HTTP_ERROR_BODY_LIMIT (default 4096)."""
    raw = os.environ.get("CFXMARK_HTTP_ERROR_BODY_LIMIT", "4096")
    try:
        return max(0, int(raw))
    except ValueError:
        return 4096


# ── HTTP helper ────────────────────────────────────────────────────────


def _request(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    data: Any = None,
    expect_json: bool = True,
    raw_body: bytes | None = None,
) -> Any:
    """Issue a single HTTP request and decode the response.

    See module docstring for the production pitfalls this helper handles.
    """
    body: bytes | None
    if raw_body is not None:
        body = raw_body
    elif data is not None:
        body = json.dumps(data).encode("utf-8")
        headers = {**headers, "Content-Type": "application/json"}
    else:
        body = None

    # noqa: S310 — host scheme/netloc is validated in
    # ConfluenceClient.__init__, so the URL is not arbitrary user input.
    req = urllib.request.Request(  # noqa: S310
        url, data=body, headers=headers, method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=get_timeout()) as resp:  # noqa: S310
            text = resp.read().decode("utf-8")
            return json.loads(text) if (text and expect_json) else text
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise HTTPError(status_code=e.code, method=method, url=url, body=err_body) from None
    except urllib.error.URLError as e:
        raise HTTPError(
            status_code=-1,
            method=method,
            url=url,
            body=f"Network error: {e.reason!r} (timeout={get_timeout()}s)",
        ) from None
    except OSError as e:
        # Python 3.11+: http.client.getresponse() raises TimeoutError /
        # RemoteDisconnected directly without URLError wrapping when the
        # timeout fires after the TCP handshake (e.g. while reading the
        # response status line). Catch raw OSError so callers always see
        # HTTPError(status_code=-1).
        raise HTTPError(
            status_code=-1,
            method=method,
            url=url,
            body=f"Network error: {e!r} (timeout={get_timeout()}s)",
        ) from None


# ── Multipart builder ──────────────────────────────────────────────────


def _build_multipart(
    filename: str,
    bytes_data: bytes,
    mime_type: str,
    comment: str = "",
) -> tuple[bytes, str]:
    """Build a multipart/form-data body for an attachment upload.

    Returns ``(body_bytes, boundary)`` where ``boundary`` is a random
    ``secrets.token_hex(16)`` value.
    """
    boundary = secrets.token_hex(16)
    crlf = b"\r\n"
    parts: list[bytes] = []

    # File field
    parts.append(f"--{boundary}".encode())
    parts.append(crlf)
    parts.append(
        f'Content-Disposition: form-data; name="file"; filename="{filename}"'.encode()
    )
    parts.append(crlf)
    parts.append(f"Content-Type: {mime_type}".encode())
    parts.append(crlf)
    parts.append(crlf)
    parts.append(bytes_data)
    parts.append(crlf)

    if comment:
        parts.append(f"--{boundary}".encode())
        parts.append(crlf)
        parts.append(b'Content-Disposition: form-data; name="comment"')
        parts.append(crlf)
        parts.append(crlf)
        parts.append(comment.encode("utf-8"))
        parts.append(crlf)

    parts.append(f"--{boundary}--".encode())
    parts.append(crlf)

    return b"".join(parts), boundary


# ── Auth ───────────────────────────────────────────────────────────────


class Auth(Protocol):
    """Strategy protocol — return the headers for one request."""

    def headers(self) -> dict[str, str]: ...


class BearerToken:
    """Static bearer token.

    Use for tests and one-shot scripts. The token is held in memory
    as a string; rotate by constructing a new instance.
    """

    def __init__(self, token: str) -> None:
        self._header = f"Bearer {token}"

    def headers(self) -> dict[str, str]:
        return {"Authorization": self._header}


class BearerTokenFile:
    """Bearer token read once from a file at construction time.

    Semantics: "boot-time" token. The file is read once in
    ``__init__``, cached in memory, and reused for the lifetime of
    the instance.

    **TOCTOU caveat.** The ``stat`` permission check and the
    ``read_text`` call are two separate syscalls, so a theoretical
    TOCTOU window exists between them. For the normal case — a
    token file under ``~/.secrets/`` owned by the running user with
    ``chmod 600`` — the window is not exploitable because an
    attacker would need write access to a directory the user
    already controls. If the token file lives in a directory the
    caller does not fully control, prefer ``EnvBearerToken`` or an
    external credential helper.

    On POSIX, the file must be readable only by the owner
    (``chmod 600``). Group-readable or world-readable files raise
    ``PermissionError`` at construction time.

    **On Windows the permission check is skipped** because POSIX
    mode bits are meaningless on NTFS. Windows users must enforce
    access via ACLs manually — for example::

        icacls <path> /inheritance:r /grant:r %USERNAME%:R

    The constructor does *not* verify Windows ACLs; a file that is
    world-readable on Windows will be accepted without warning.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path).expanduser()
        if os.name == "posix":
            mode = self._path.stat().st_mode
            if mode & (stat.S_IRGRP | stat.S_IROTH):
                raise PermissionError(
                    f"{self._path} is group/world readable — chmod 600 and retry"
                )
        self._header = f"Bearer {self._path.read_text().strip()}"

    def headers(self) -> dict[str, str]:
        return {"Authorization": self._header}


class BasicAuth:
    """HTTP Basic auth using an email + API token pair.

    Designed for Confluence Cloud, which authenticates REST calls
    as ``<account-email>:<api-token>`` encoded in an ``Authorization:
    Basic ...`` header. Confluence Server / DC users should prefer
    ``BearerToken`` or ``BearerTokenFile`` with a Personal Access
    Token.
    """

    def __init__(self, email: str, api_token: str) -> None:
        encoded = base64.b64encode(f"{email}:{api_token}".encode()).decode("ascii")
        self._header = f"Basic {encoded}"

    def headers(self) -> dict[str, str]:
        return {"Authorization": self._header}


class EnvBearerToken:
    """Bearer token read from an environment variable each call.

    Useful for runtimes where the token is rotated out-of-band
    (short-lived service tokens, credential helpers writing to env).
    Raises ``RuntimeError`` if the variable is unset at call time.
    """

    def __init__(self, var_name: str) -> None:
        self._var = var_name

    def headers(self) -> dict[str, str]:
        token = os.environ.get(self._var)
        if not token:
            raise RuntimeError(f"environment variable {self._var} is unset")
        return {"Authorization": f"Bearer {token}"}


# ── Constants ──────────────────────────────────────────────────────────

_DEFAULT_EXPAND = "body.storage,version,space,ancestors"


# ── Result dataclasses ─────────────────────────────────────────────────


@dataclass(frozen=True)
class PushResult:
    """Result of a :meth:`ConfluenceClient.push_markdown` call."""

    changed: bool
    new_version: int | None
    uploaded_attachments: tuple[str, ...]
    failed_attachments: tuple[tuple[str, Exception], ...]
    remote_page: dict | None = None

    @property
    def has_partial_failure(self) -> bool:
        """True when at least one attachment upload failed."""
        return bool(self.failed_attachments)


@dataclass(frozen=True)
class PullResult:
    """Result of a :meth:`ConfluenceClient.pull_markdown` call.

    ``warnings`` carries the parse-time messages produced by
    :func:`cfxmark.to_md` (e.g. unknown macro fallbacks). It does
    **not** include errors from the asset-resolution phase: a strict
    filename violation raises :class:`cfxmark.AssetSecurityError`,
    and per-attachment download failures are logged via the
    ``cfxmark.confluence`` logger rather than aggregated here.
    ``resolved_asset_count`` reports how many attachments were
    successfully fetched.
    """

    markdown: str
    remote_page: dict
    warnings: tuple[str, ...] = field(default_factory=tuple)
    resolved_asset_count: int = 0


# ── ConfluenceClient ───────────────────────────────────────────────────


class ConfluenceClient:
    """Opinionated but overridable Confluence REST client.

    See module docstring for the production pitfalls handled here.
    The client is stateless apart from its auth and dialect settings;
    call it from multiple threads only if your ``Auth`` implementation
    is thread-safe (all four built-in helpers are).

    :param host: Base URL, e.g. ``https://confluence.example.com``.
    :param auth: One of ``BearerToken``, ``BearerTokenFile``,
        ``BasicAuth``, ``EnvBearerToken``, or a custom ``Auth``
        implementation.
    :param dialect:
        ``"server"`` (default) — Confluence Server / Data Center.
            Attachment uploads set ``X-Atlassian-Token: no-check`` to
            bypass the XSRF check (mandatory on Server/DC, uploads
            receive a blanket 403 without it).
        ``"cloud"`` — Confluence Cloud. The XSRF header is omitted.
        **Cloud support is best-effort** — the reference test suite
        uses Server/DC; Cloud-only paths that differ should be
        reported as bugs.
    :param api_base: REST API path prefix. Defaults to
        ``/rest/api``. Override only if your Confluence instance
        mounts the API under a different path.
    """

    def __init__(
        self,
        *,
        host: str,
        auth: Auth,
        dialect: Literal["server", "cloud"] = "server",
        api_base: str = "/rest/api",
    ) -> None:
        if dialect not in ("server", "cloud"):
            raise ValueError(f"dialect must be 'server' or 'cloud', got {dialect!r}")

        # Host validation — reject malformed URLs up front. Three checks:
        # (1) scheme must be http or https;
        # (2) netloc must be non-empty (catches ``host="https://"`` typos);
        # (3) plain http triggers a warning (credentials in plaintext).
        parsed = urlparse(host)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(
                f"host must be http:// or https://, got {parsed.scheme!r}"
            )
        if not parsed.netloc:
            raise ValueError(f"host missing netloc: {host!r}")
        if parsed.scheme == "http":
            _log.warning(
                "ConfluenceClient host %s uses http:// — credentials will "
                "be transmitted in plaintext. Use https:// for production.",
                host,
            )

        self._host = host.rstrip("/")
        self._auth = auth
        self._dialect = dialect
        self._api = api_base

    def _content_url(
        self,
        *suffix: str,
        query: str = "",
    ) -> str:
        """Build a content REST URL.

        ``self._content_url()`` → ``{host}{api}/content``
        ``self._content_url("123")`` → ``{host}{api}/content/123``
        ``self._content_url("123", "child/attachment")`` →
            ``{host}{api}/content/123/child/attachment``
        ``self._content_url("123", query="expand=body")`` →
            ``{host}{api}/content/123?expand=body``
        """
        url = f"{self._host}{self._api}/content"
        if suffix:
            url = url + "/" + "/".join(suffix)
        if query:
            url = f"{url}?{query}"
        return url

    # ── High-level canonical-aware ops ──

    def push_markdown(
        self,
        page_id: str,
        md_text: str,
        *,
        md_path: str | Path | None = None,
        on_conflict: Literal["abort", "retry", "force"] = "abort",
        version_message: str = "Updated via cfxmark",
        strict_filenames: bool = True,
    ) -> PushResult:
        """Push a Markdown document to a Confluence page.

        Algorithm:

        1. Render Markdown to storage XHTML via ``cfxmark.to_cfx``.
        2. Validate every referenced attachment filename
           (``strict_filenames=True``) so a malicious markdown like
           ``![](../../../etc/passwd)`` cannot exfiltrate the host
           filesystem through the upload loop. The check raises
           :class:`cfxmark.AssetSecurityError` *before* any HTTP
           traffic is issued.
        3. Fetch the current page body (``get_page``).
        4. Canonicalize both local and remote bodies and compare.
        5. If different, PUT the new body (``update_page``).
        6. Upload any missing attachments — runs **unconditionally**,
           even on the canonical-equal fast path (R3-3 fix: a
           concurrent pusher may have landed the same body but
           skipped attachment uploads).

        ``PushResult.changed`` is ``True`` whenever this call modified
        the page at all — body PUT or at least one attachment uploaded.

        :param strict_filenames: When ``True`` (default), every filename
            in ``cfx_result.attachments`` is validated against the same
            path-traversal ruleset that ``resolve_assets`` uses on the
            pull side. Pass ``False`` only if you trust the markdown
            input completely (e.g. machine-generated content).
        :param on_conflict:
            ``"abort"`` (default) — raise ``ConfluenceVersionConflict``
                on HTTP 409.
            ``"retry"`` — canonical-aware last-writer-wins. After a
                409, re-fetch and re-canonicalize-compare. If
                convergent, no-op; otherwise PUT with fresh parent.
            ``"force"`` — after any 409, re-fetch and PUT
                unconditionally.
        """
        # 1. Render local (cheap, no I/O — fail fast on bad input)
        cfx_result = to_cfx(md_text)

        # 2. Validate attachment paths BEFORE any HTTP traffic. The
        #    markdown source may have been authored by an attacker
        #    with repo write access; without this check ``![](../../etc/passwd)``
        #    flows through to ``upload_attachment`` and exfiltrates
        #    arbitrary host files into Confluence.
        if strict_filenames:
            for filename in cfx_result.attachments:
                _validate_filename(filename)

        # 3. Fetch remote
        remote = self.get_page(page_id)

        # 3. Canonical compare
        canonical_remote = canonicalize_cfx(remote["body"]["storage"]["value"])
        canonical_local = canonicalize_cfx(cfx_result.xhtml or "")

        # 4. PUT body first, only if the canonical forms differ.
        #    NOTE on the "body convergent" case: even when
        #    canonical_remote == canonical_local, we do NOT
        #    early-return. The attachment upload loop below runs
        #    unconditionally for exactly this reason.
        body_changed = False
        new_page: dict[str, Any] = remote

        if canonical_remote != canonical_local:
            try:
                new_page = self.update_page(
                    page_id,
                    cfx_result.xhtml or "",
                    page=remote,
                    version_message=version_message,
                )
                body_changed = True
            except ConfluenceVersionConflict:
                if on_conflict == "abort":
                    raise
                # Bounded retry — single attempt
                fresh = self.get_page(page_id)
                fresh_canonical = canonicalize_cfx(fresh["body"]["storage"]["value"])
                if on_conflict == "retry" and fresh_canonical == canonical_local:
                    # Body has converged between our fetch and PUT.
                    # Skip second PUT; fall through to attachment loop.
                    new_page = fresh
                    body_changed = False
                else:
                    # Either "retry" with divergent fresh remote, or "force".
                    new_page = self.update_page(
                        page_id,
                        cfx_result.xhtml or "",
                        page=fresh,
                        version_message=version_message,
                    )
                    body_changed = True
        else:
            _log.debug(
                "push_markdown: canonical equal, skipping PUT for %s "
                "(attachment loop still runs)",
                page_id,
            )

        # 5. Upload missing or drifted attachments — runs on EVERY code
        #    path, including the canonical-equal fast path.
        #
        #    Convergence rule (Codex review fix): a same-named remote
        #    attachment alone is NOT proof the bytes are current. We
        #    compare the local file size against ``extensions.fileSize``
        #    from ``list_attachments``: if the sizes differ (or remote
        #    fileSize is missing), we re-upload so Confluence creates a
        #    new version of the attachment. Same-size files are assumed
        #    identical — content-hash comparison is a future enhancement
        #    (plan §7) but size catches every edit that changes byte
        #    length, which is the overwhelmingly common case.
        existing_by_title: dict[str, dict[str, Any]] = {
            a["title"]: a for a in self.list_attachments(page_id)
        }
        md_dir = Path(md_path).parent if md_path else Path.cwd()
        uploaded: list[str] = []
        failed: list[tuple[str, Exception]] = []
        for filename in cfx_result.attachments:
            basename = Path(filename).name
            local = md_dir / filename
            remote_att = existing_by_title.get(basename)
            if remote_att is not None and _attachment_size_matches(local, remote_att):
                # Sizes match → content assumed identical, skip upload.
                continue
            try:
                self.upload_attachment(page_id, local)
                uploaded.append(basename)
            except Exception as ex:  # noqa: BLE001 — partial-failure collection: every per-attachment exception is captured into PushResult.failed_attachments so the caller can decide retry/abort policy.
                _log.warning("upload_attachment failed for %s: %r", basename, ex)
                failed.append((basename, ex))

        return PushResult(
            changed=body_changed or bool(uploaded),
            new_version=new_page["version"]["number"],
            uploaded_attachments=tuple(uploaded),
            failed_attachments=tuple(failed),
            remote_page=new_page,
        )

    def pull_markdown(
        self,
        page_id: str,
        *,
        md_path: str | Path | None = None,
        resolve_assets_mode: Literal["sidecar", "inline", "none"] = "none",
        asset_dir: str | Path | None = None,
    ) -> PullResult:
        """Pull a Confluence page and convert to Markdown.

        Algorithm:

        1. ``get_page(page_id)``
        2. ``cfxmark.to_md(remote_body)``
        3. If ``resolve_assets_mode != "none"``: call
           ``cfxmark.resolve_assets`` with ``self.attachment_fetcher``.
        4. Return a ``PullResult``.
        """
        remote = self.get_page(page_id)
        xhtml = remote["body"]["storage"]["value"]
        md_result = to_md(xhtml)
        markdown = md_result.markdown or ""
        resolved_count = 0

        if resolve_assets_mode != "none":
            base_fetcher = self.attachment_fetcher(page_id)

            def counting_fetcher(name: str) -> bytes | None:
                nonlocal resolved_count
                result = base_fetcher(name)
                if result is not None:
                    resolved_count += 1
                return result

            markdown = resolve_assets(
                markdown,
                counting_fetcher,
                mode=resolve_assets_mode,
                asset_dir=asset_dir,
                md_path=md_path,
            )

        return PullResult(
            markdown=markdown,
            remote_page=remote,
            warnings=md_result.warnings,
            resolved_asset_count=resolved_count,
        )

    # ── Low-level REST ops ──

    def get_page(
        self,
        page_id: str,
        *,
        expand: str = _DEFAULT_EXPAND,
    ) -> dict[str, Any]:
        """GET /content/{page_id}?expand=<expand>.

        Default ``expand`` is conservative (body.storage, version,
        space, ancestors). Override for performance when you only
        need a subset.
        """
        url = self._content_url(page_id, query=f"expand={expand}")
        headers = {**self._auth.headers(), "Accept": "application/json"}
        result: dict[str, Any] = _request("GET", url, headers=headers)
        return result

    def create_page(
        self,
        parent_id: str,
        title: str,
        xhtml: str,
        *,
        space_key: str,
    ) -> dict[str, Any]:
        """POST a new page as a child of ``parent_id``."""
        url = self._content_url()
        headers = self._auth.headers()
        data: dict[str, Any] = {
            "type": "page",
            "title": title,
            "space": {"key": space_key},
            "ancestors": [{"id": parent_id}],
            "body": {
                "storage": {
                    "value": xhtml,
                    "representation": "storage",
                }
            },
        }
        result: dict[str, Any] = _request("POST", url, headers=headers, data=data)
        return result

    def update_page(
        self,
        page_id: str,
        xhtml: str,
        *,
        page: dict[str, Any] | None = None,
        version_message: str = "",
    ) -> dict[str, Any]:
        """PUT new storage XHTML.

        If ``page`` (a previously-fetched body) is provided, the
        internal GET is skipped. Raises ``ConfluenceVersionConflict``
        on HTTP 409.
        """
        if page is None:
            page = self.get_page(page_id)
        current_version: int = page["version"]["number"]
        title: str = page["title"]
        url = self._content_url(page_id)
        headers = self._auth.headers()
        payload: dict[str, Any] = {
            "version": {"number": current_version + 1},
            "title": title,
            "type": "page",
            "body": {
                "storage": {
                    "value": xhtml,
                    "representation": "storage",
                }
            },
        }
        if version_message:
            payload["version"]["message"] = version_message
        try:
            result: dict[str, Any] = _request("PUT", url, headers=headers, data=payload)
            return result
        except HTTPError as e:
            if e.status_code == 409:
                raise ConfluenceVersionConflict(
                    current_version=None,
                    status_code=e.status_code,
                    method=e.method,
                    url=e.url,
                    body=e.body,
                ) from None
            raise

    def delete_page(self, page_id: str) -> None:
        """DELETE /content/{page_id}."""
        url = self._content_url(page_id)
        headers = self._auth.headers()
        _request("DELETE", url, headers=headers, expect_json=False)

    def upload_attachment(
        self,
        page_id: str,
        local_path: str | Path,
        *,
        comment: str = "",
    ) -> dict[str, Any]:
        """POST a multipart upload.

        Uses a random ``secrets.token_hex(16)`` boundary so file bytes
        cannot collide with the sentinel. Sets ``X-Atlassian-Token:
        no-check`` when ``dialect="server"``.
        """
        path = Path(local_path)
        bytes_data = path.read_bytes()
        mime_type, _ = mimetypes.guess_type(str(path))
        if mime_type is None:
            mime_type = "application/octet-stream"

        body, boundary = _build_multipart(path.name, bytes_data, mime_type, comment)

        url = self._content_url(page_id, "child/attachment")
        headers: dict[str, str] = {
            **self._auth.headers(),
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "application/json",
        }
        if self._dialect == "server":
            headers["X-Atlassian-Token"] = "no-check"

        result: dict[str, Any] = _request("POST", url, headers=headers, raw_body=body)
        return result

    def list_attachments(self, page_id: str, *, limit: int = 200) -> list[dict[str, Any]]:  # noqa: E501
        """GET /content/{page_id}/child/attachment.

        ``limit=200`` matches Confluence Server's default page size.
        Pagination via the ``_links.next`` cursor is not yet
        implemented; pages with more than 200 attachments will only
        see the first 200 here. (Future enhancement.)
        """
        url = self._content_url(page_id, "child/attachment", query=f"limit={limit}")
        headers = {**self._auth.headers(), "Accept": "application/json"}
        data: dict[str, Any] = _request("GET", url, headers=headers)
        results: list[dict[str, Any]] = data.get("results", [])
        return results

    def attachment_fetcher(self, page_id: str) -> AssetFetcher:
        """Return a closure that fetches bytes for one attachment.

        Paired with ``cfxmark.resolve_assets``. Pre-fetches the full
        attachment list once and caches it in the closure so the
        list endpoint is called only once per ``pull_markdown`` call.
        """
        attachments = self.list_attachments(page_id)
        attachment_map: dict[str, dict[str, Any]] = {
            a["title"]: a for a in attachments
        }
        host = self._host
        auth = self._auth

        host_netloc = urlparse(host).netloc

        def fetcher(filename: str) -> bytes | None:
            att = attachment_map.get(filename)
            if att is None:
                return None
            dl: str = att.get("_links", {}).get("download", "")
            if not dl:
                return None
            # Defense in depth (Codex review fix): the download URL is
            # supplied by the remote Confluence server. We must NOT
            # forward the caller's bearer/basic credential to an
            # arbitrary host — a misconfigured proxy or compromised
            # server could return ``_links.download="https://evil.com/x"``
            # and turn attachment resolution into credential
            # exfiltration.  Accept either:
            #   * a host-relative link (starts with "/"), OR
            #   * an absolute URL whose netloc matches ``self._host``.
            # Anything else is logged and dropped.
            if dl.startswith("/"):
                dl_url = f"{host}{dl}"
            else:
                dl_netloc = urlparse(dl).netloc
                if dl_netloc != host_netloc:
                    _log.warning(
                        "refusing off-host attachment download for %s: "
                        "_links.download=%r is not on %s",
                        filename, dl, host_netloc,
                    )
                    return None
                dl_url = dl
            # noqa: S310 — host validated above; URL is host-relative
            # to self._host or has the matching netloc.
            req = urllib.request.Request(  # noqa: S310
                dl_url, headers={**auth.headers()}
            )
            try:
                with urllib.request.urlopen(req, timeout=get_timeout()) as resp:  # noqa: S310
                    downloaded: bytes = resp.read()
                    return downloaded
            except urllib.error.HTTPError as ex:
                # Log instead of swallow — 401/403/5xx during a pull
                # would otherwise present as "attachment missing" and
                # silently truncate the document.
                _log.warning(
                    "attachment download failed: %s (HTTP %s)", filename, ex.code,
                )
                return None
            except urllib.error.URLError as ex:
                _log.warning(
                    "attachment download failed: %s (%r)", filename, ex.reason,
                )
                return None
            except OSError as ex:
                # Python 3.11+: http.client raises raw TimeoutError /
                # RemoteDisconnected without URLError wrapping when the
                # disconnect happens after the TCP handshake. Match the
                # ``_request`` helper so a flaky download cannot kill
                # the whole pull.
                _log.warning(
                    "attachment download failed: %s (%r)", filename, ex,
                )
                return None

        return fetcher


# ── Module-level helpers ───────────────────────────────────────────────


def _attachment_size_matches(local: Path, remote_att: dict[str, Any]) -> bool:
    """Return True iff the local file and the remote attachment have the
    same byte length.

    Used by ``ConfluenceClient.push_markdown`` to skip re-uploads when
    a same-named remote attachment already has identical byte length.
    Same-size files are assumed identical — content-hash comparison is
    a future enhancement (plan §7).

    Returns ``False`` (i.e. force a re-upload) when:
    - the local path does not exist or cannot be stat'd,
    - the remote payload omits ``extensions.fileSize``,
    - the sizes differ.

    The False-on-uncertainty default keeps the contract honest: when in
    doubt, re-upload so Confluence creates a new version.
    """
    try:
        local_size = local.stat().st_size
    except OSError:
        return False
    remote_size_raw = remote_att.get("extensions", {}).get("fileSize")
    if remote_size_raw is None:
        return False
    try:
        remote_size = int(remote_size_raw)
    except (TypeError, ValueError):
        return False
    return remote_size == local_size


__all__ = [
    "HTTPError",
    "ConfluenceVersionConflict",
    "get_timeout",
    "get_error_body_limit",
    "Auth",
    "BearerToken",
    "BearerTokenFile",
    "BasicAuth",
    "EnvBearerToken",
    "PushResult",
    "PullResult",
    "ConfluenceClient",
]
