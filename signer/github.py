"""A small GitHub REST client: releases and their assets, nothing else.

Writes go through one place, so --dry-run can log them instead of making
them. Requests are retried on network errors, 5xx and rate limiting (honouring
Retry-After), which matters on a full re-sign: a hundred-odd asset uploads,
deletes and renames in a burst."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .util import HTTPFailure, USER_AGENT, log

API = "https://api.github.com"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):          # noqa: D401 - urllib hook
        return None


_plain = urllib.request.build_opener()
_no_redirect = urllib.request.build_opener(_NoRedirect)


def discover_token() -> str | None:
    for var in ("GH_TOKEN", "GITHUB_TOKEN"):
        if os.environ.get(var):
            return os.environ[var]
    if shutil.which("gh"):
        proc = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    return None


class GitHub:
    def __init__(self, token: str | None, *, dry_run: bool = False):
        self.token = token
        self.dry_run = dry_run
        self._fake_id = 0

    # ----- transport ---------------------------------------------------------

    def _headers(self, extra: dict[str, str] | None = None, auth: bool = True) -> dict[str, str]:
        headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28"}
        if auth and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        headers.update(extra or {})
        return headers

    def request(self, method: str, url: str, *, data: bytes | None = None,
                json_body: Any = None, headers: dict[str, str] | None = None,
                allow_404: bool = False, follow: bool = True, timeout: float = 60,
                retries: int = 5) -> tuple[int, bytes, Any]:
        if url.startswith("/"):
            url = API + url
        if json_body is not None:
            data = json.dumps(json_body).encode()
            headers = {"Content-Type": "application/json", **(headers or {})}
        opener = _plain if follow else _no_redirect
        for attempt in range(retries + 1):
            req = urllib.request.Request(url, data=data, method=method, headers=self._headers(headers))
            try:
                with opener.open(req, timeout=timeout) as resp:
                    return resp.status, resp.read(), resp.headers
            except urllib.error.HTTPError as exc:
                body = exc.read() or b""
                if exc.code in (301, 302, 303, 307, 308) and not follow:
                    return exc.code, body, exc.headers
                if exc.code == 404 and allow_404:
                    return 404, body, exc.headers
                wait = self._retry_after(exc.code, body, exc.headers, attempt)
                if wait is not None and attempt < retries:
                    log(f"   GitHub {exc.code} on {method} {url.split('?')[0]}; retrying in {wait:.0f}s")
                    time.sleep(wait)
                    continue
                raise HTTPFailure(url, exc.code, body, exc.headers) from None
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                if attempt < retries:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise HTTPFailure(url, 0, str(exc).encode()) from None
        raise AssertionError("unreachable")

    @staticmethod
    def _retry_after(status: int, body: bytes, headers: Any, attempt: int) -> float | None:
        limited = status == 429 or (status == 403 and (
            b"rate limit" in body.lower() or (headers and headers.get("Retry-After"))))
        if not (limited or status >= 500):
            return None
        if headers and headers.get("Retry-After", "").isdigit():
            return min(120.0, float(headers["Retry-After"]))
        if limited and headers and headers.get("X-RateLimit-Remaining") == "0":
            reset = float(headers.get("X-RateLimit-Reset", "0") or 0)
            return min(120.0, max(5.0, reset - time.time()))
        return min(60.0, 3.0 * 2 ** attempt) if limited else 2.0 * (attempt + 1)

    def get_json(self, path: str, *, allow_404: bool = False) -> Any:
        status, body, _ = self.request("GET", path, allow_404=allow_404)
        if status == 404:
            return None
        return json.loads(body)

    # ----- reads -------------------------------------------------------------

    def latest_release(self, repo: str) -> dict | None:
        return self.get_json(f"/repos/{repo}/releases/latest", allow_404=True)

    def release_by_tag(self, repo: str, tag: str) -> dict | None:
        """Published releases only; drafts are found through releases()."""
        return self.get_json(f"/repos/{repo}/releases/tags/{urllib.parse.quote(tag)}", allow_404=True)

    def releases(self, repo: str, limit: int = 100) -> list[dict]:
        return self.get_json(f"/repos/{repo}/releases?per_page={min(limit, 100)}") or []

    def release(self, repo: str, release_id: int) -> dict:
        return self.get_json(f"/repos/{repo}/releases/{release_id}")

    def download_asset(self, repo: str, asset: dict, dest: Path) -> Path:
        """Through the API, which works for drafts too. The redirect goes to
        signed storage that must not see our token, so it is followed by hand."""
        status, body, headers = self.request(
            "GET", f"/repos/{repo}/releases/assets/{asset['id']}",
            headers={"Accept": "application/octet-stream"}, follow=False, timeout=300)
        if status in (301, 302, 303, 307, 308):
            location = headers.get("Location")
            req = urllib.request.Request(location, headers={"User-Agent": USER_AGENT})
            for attempt in range(4):
                try:
                    with _plain.open(req, timeout=300) as resp:
                        body = resp.read()
                    break
                except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
                    if attempt == 3:
                        raise
                    time.sleep(2 * (attempt + 1))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(body)
        return dest

    # ----- writes ------------------------------------------------------------

    def _fake(self, **fields: Any) -> dict:
        self._fake_id -= 1
        return {"id": self._fake_id, **fields}

    def create_release(self, repo: str, **fields: Any) -> dict:
        if self.dry_run:
            log(f"   [dry run] create release {fields.get('tag_name')} (draft={fields.get('draft')})")
            return self._fake(tag_name=fields.get("tag_name"), draft=True, assets=[],
                              upload_url="https://uploads.github.com/dry-run{?name,label}",
                              prerelease=fields.get("prerelease", False))
        _, body, _ = self.request("POST", f"/repos/{repo}/releases", json_body=fields)
        return json.loads(body)

    def update_release(self, repo: str, release_id: int, **fields: Any) -> dict | None:
        if self.dry_run:
            shown = {k: (v if k != "body" else f"<{len(v)} chars>") for k, v in fields.items()}
            log(f"   [dry run] update release {release_id}: {shown}")
            return None
        _, body, _ = self.request("PATCH", f"/repos/{repo}/releases/{release_id}", json_body=fields)
        return json.loads(body)

    def upload_asset(self, release: dict, name: str, data: bytes,
                     content_type: str = "application/octet-stream") -> dict:
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        if self.dry_run:
            log(f"   [dry run] upload {name} ({len(data):,} bytes)")
            return self._fake(name=name, size=len(data), state="uploaded", digest=digest)
        base = release["upload_url"].split("{", 1)[0]
        url = f"{base}?name={urllib.parse.quote(name)}"
        _, body, _ = self.request("POST", url, data=data, timeout=600,
                                  headers={"Content-Type": content_type})
        return json.loads(body)

    def delete_asset(self, repo: str, asset: dict) -> None:
        if self.dry_run:
            log(f"   [dry run] delete {asset.get('name')}")
            return
        self.request("DELETE", f"/repos/{repo}/releases/assets/{asset['id']}", allow_404=True)

    def rename_asset(self, repo: str, asset: dict, name: str) -> dict:
        if self.dry_run:
            log(f"   [dry run] rename {asset.get('name')} -> {name}")
            return {**asset, "name": name}
        _, body, _ = self.request("PATCH", f"/repos/{repo}/releases/assets/{asset['id']}",
                                  json_body={"name": name})
        return json.loads(body)
