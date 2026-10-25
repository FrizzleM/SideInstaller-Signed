"""Helpers shared by every part of the signer: logging, subprocesses, HTTP,
hashing and parallel work. Standard library only, Python 3.9+."""
from __future__ import annotations

import concurrent.futures
import contextlib
import hashlib
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator, Sequence, TypeVar

T = TypeVar("T")
R = TypeVar("R")

IN_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"
# Cloudflare in front of the NexCerts API rejects Python's default agent.
USER_AGENT = "SideInstaller-Signed/2 (+https://github.com/FrizzleM/SideInstaller-Signed)"


class Fatal(Exception):
    """Ends the run with a one-line message instead of a traceback."""


# ----- logging ---------------------------------------------------------------

def log(msg: str = "") -> None:
    print(msg, flush=True)


def warn(msg: str) -> None:
    print(f"::warning::{msg}" if IN_ACTIONS else f"warning: {msg}", flush=True)


def error(msg: str) -> None:
    print(f"::error::{msg}" if IN_ACTIONS else f"error: {msg}", flush=True)


@contextlib.contextmanager
def group(title: str) -> Iterator[None]:
    """A collapsible log section under Actions, with its duration after it."""
    start = time.monotonic()
    print(f"::group::{title}" if IN_ACTIONS else f"== {title}", flush=True)
    try:
        yield
    finally:
        if IN_ACTIONS:
            print("::endgroup::", flush=True)
        print(f"   {title}: {time.monotonic() - start:.1f}s", flush=True)


def set_output(name: str, value: str) -> None:
    """A step output under Actions; printed either way."""
    print(f"output {name}={value}", flush=True)
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{name}={value}\n")


def add_summary(markdown: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(markdown.rstrip() + "\n\n")


# ----- subprocesses ----------------------------------------------------------

class CommandError(Exception):
    def __init__(self, cmd: Sequence[str], proc: subprocess.CompletedProcess):
        self.cmd = list(cmd)
        self.returncode = proc.returncode
        self.stdout = proc.stdout or b""
        self.stderr = proc.stderr or b""
        detail = (self.stderr or self.stdout).decode("utf-8", "replace").strip()
        detail = " | ".join(detail.splitlines()[-4:])
        super().__init__(f"{Path(self.cmd[0]).name} exited {self.returncode}: {detail}")


def run(cmd: Sequence[object], *, check: bool = True, input: bytes | None = None,
        env: dict[str, str] | None = None, cwd: str | Path | None = None,
        timeout: float | None = None) -> subprocess.CompletedProcess:
    args = [str(c) for c in cmd]
    full_env = None
    if env:
        full_env = dict(os.environ)
        full_env.update(env)
    proc = subprocess.run(args, input=input, capture_output=True, env=full_env,
                          cwd=cwd, timeout=timeout)
    if check and proc.returncode != 0:
        raise CommandError(args, proc)
    return proc


# ----- parallel work ---------------------------------------------------------

def pmap(fn: Callable[[T], R], items: Iterable[T], workers: int) -> list[R]:
    """map() over a thread pool, keeping order. Subprocesses, zlib and network
    reads all release the GIL, which is where this code spends its time."""
    items = list(items)
    if workers <= 1 or len(items) <= 1:
        return [fn(item) for item in items]
    with concurrent.futures.ThreadPoolExecutor(min(workers, len(items))) as pool:
        return list(pool.map(fn, items))


def cpu_count() -> int:
    return max(1, os.cpu_count() or 1)


# ----- hashing and names -----------------------------------------------------

def sha256_bytes(*parts: bytes) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(len(part).to_bytes(8, "big"))
        h.update(part)
    return h.hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_name(name: str) -> str:
    """The file-name form of a certificate's name, as the asset names have
    always been built: spaces and anything outside [A-Za-z0-9._-] become '-',
    runs of '-' collapse, and no '-' at either end."""
    s = re.sub(r"[^A-Za-z0-9._-]", "-", name.replace(" ", "-"))
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s or "certificate"


# ----- time ------------------------------------------------------------------

def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ----- HTTP ------------------------------------------------------------------

class HTTPFailure(Exception):
    def __init__(self, url: str, status: int, body: bytes = b"", headers=None):
        self.url, self.status, self.body, self.headers = url, status, body, headers or {}
        snippet = body[:200].decode("utf-8", "replace").strip()
        super().__init__(f"HTTP {status} from {url}" + (f": {snippet}" if snippet else ""))


def http_get(url: str, *, headers: dict[str, str] | None = None, timeout: float = 30,
             retries: int = 2) -> bytes:
    """GET with retries on network errors and 5xx/429."""
    hdrs = {"User-Agent": USER_AGENT}
    hdrs.update(headers or {})
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            body = exc.read() if hasattr(exc, "read") else b""
            if exc.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise HTTPFailure(url, exc.code, body) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise HTTPFailure(url, 0, str(exc).encode()) from None
    raise AssertionError("unreachable")


def _curl_quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def download_many(jobs: Sequence[tuple[str, Path]], *, parallel: int = 32,
                  timeout: int = 60) -> dict[Path, int]:
    """Fetch many small files in one curl process. Its parallel mode
    multiplexes them over a single HTTP/2 connection, which turns the ~90
    requests of the NexCerts pool into about a second of work. Returns the HTTP
    status per file (0 = no response); a file that did not come back with 200
    is deleted, so its presence means success."""
    if not jobs:
        return {}
    with tempfile.NamedTemporaryFile("w", suffix=".curlrc", delete=False, encoding="utf-8") as cfg:
        for url, out in jobs:
            out.parent.mkdir(parents=True, exist_ok=True)
            cfg.write(f'url = "{_curl_quote(url)}"\noutput = "{_curl_quote(str(out))}"\n')
    try:
        proc = subprocess.run(
            ["curl", "--silent", "--show-error", "--location", "--parallel",
             "--parallel-max", str(parallel), "--retry", "2", "--connect-timeout", "15",
             "--max-time", str(timeout), "--user-agent", USER_AGENT, "--config", cfg.name,
             "--write-out", "%{http_code}\t%{filename_effective}\n"],
            capture_output=True, text=True)
    finally:
        os.unlink(cfg.name)
    codes: dict[str, int] = {}
    for line in proc.stdout.splitlines():
        code, _, name = line.partition("\t")
        codes[name] = int(code) if code.isdigit() else 0
    result: dict[Path, int] = {}
    for _, out in jobs:
        code = codes.get(str(out), 0)
        if code != 200 and out.exists():
            out.unlink()
        result[out] = code
    return result


def pem_to_ders(text: str) -> list[bytes]:
    import base64
    blocks = re.findall(r"-----BEGIN CERTIFICATE-----(.*?)-----END CERTIFICATE-----", text, re.S)
    return [base64.b64decode("".join(b.split())) for b in blocks]


def der_to_pem(der: bytes) -> str:
    import base64
    b64 = base64.b64encode(der).decode()
    lines = [b64[i:i + 64] for i in range(0, len(b64), 64)]
    return "-----BEGIN CERTIFICATE-----\n" + "\n".join(lines) + "\n-----END CERTIFICATE-----\n"


def is_macos() -> bool:
    return sys.platform == "darwin"
