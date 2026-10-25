"""The certificate pool: every source listed in cert-url.txt, fetched in
parallel and merged into one list of entries, one per install card.

A source is a folder in this repository, a .zip URL, a GitHub repository or
the NexCerts API. Each certificate lives in its own folder:

    <Name>/<Name>.p12  +  <Name>/<Name>.mobileprovision  [+ <Name>/password.txt]

The folder name is the name on the install page; its file-name form (see
util.safe_name) names the release assets. When two entries share a file name
the first one in source order wins, so a certificate dropped into certs/
overrides the same one from a public pool."""
from __future__ import annotations

import json
import os
import re
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .util import (HTTPFailure, download_many, http_get, log, pmap, run, safe_name,
                   sha256_bytes, warn, CommandError)

DEFAULT_SOURCES = ["certs", "https://sideloading.net"]
NEXCERTS_HOST = os.environ.get("NEXCERTS_HOST", "sideloading.net")
PASSWORD_FILES = ("{stem}.password", "{stem}.pass", "{stem}.txt", "password.txt", "password",
                  "readme.txt", "README.txt", "readme")


@dataclass
class Entry:
    name: str                 # file-name form, the key everywhere
    display_name: str         # folder name, shown on the page
    source: str               # the cert-url.txt line it came from
    p12: Path
    profile: Path | None
    password: str
    input_sha256: str         # p12 + profile + password: changes when anything that is signed with changes
    problem: str | None = None


@dataclass
class SourceResult:
    source: str
    kind: str
    ok: bool                  # fetched completely; only then may entries missing from it be dropped
    count: int = 0
    detail: str = ""


@dataclass
class Pool:
    entries: dict[str, Entry] = field(default_factory=dict)
    sources: list[SourceResult] = field(default_factory=list)
    overridden: bool = False  # sources came from CERT_ZIP_URL for this run only

    def source_ok(self, source: str) -> bool:
        return any(s.source == source and s.ok for s in self.sources)

    def listed(self, source: str) -> bool:
        return any(s.source == source for s in self.sources)


# ----- sources ---------------------------------------------------------------

def read_sources(root: Path) -> tuple[list[str], bool]:
    """(sources, overridden). CERT_ZIP_URL replaces cert-url.txt for one run."""
    override = os.environ.get("CERT_ZIP_URL", "").split()
    if override:
        return override, True
    path = root / "cert-url.txt"
    lines: list[str] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                lines.append(line)
    return lines or list(DEFAULT_SOURCES), False


def source_kind(source: str) -> str:
    s = source.rstrip("/")
    if not re.match(r"^[a-z]+://|^git@", s):
        return "local"
    if _nexcerts_base(s):
        return "nexcerts"
    bare = s[:-4] if s.endswith(".git") else s
    m = re.match(r"^(?:https?://github\.com/|git@github\.com:)([^/]+)/([^/]+)$", bare)
    if m and not bare.endswith(".zip"):
        return "github"
    return "zip"


def _nexcerts_base(source: str) -> str | None:
    s = source.rstrip("/")
    if re.search(r"github\.com/NovaDev404/NexCerts(/|$|\.git$)", s):
        return os.environ.get("NEXCERTS_API_BASE", f"https://{NEXCERTS_HOST}")
    m = re.match(r"^(https?)://([^/]+)", s)
    if m and (m.group(2) == NEXCERTS_HOST or m.group(2).endswith("." + NEXCERTS_HOST)):
        return f"{m.group(1)}://{m.group(2)}"
    return None


# ----- reading one certificate folder ---------------------------------------

def password_from_file(path: Path) -> str:
    """A 'password: x' line (any case, either colon), else the first line."""
    first = ""
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.search(r"(?:password|密码)\s*[:：]\s*(.*)$", line, re.I)
        if m:
            return m.group(1).strip()
        if not first and line.strip():
            first = line.strip()
    return first


def resolve_password(cert_dir: Path, stem: str) -> str:
    for pattern in PASSWORD_FILES:
        candidate = cert_dir / pattern.format(stem=stem)
        if candidate.is_file():
            value = password_from_file(candidate)
            if value:
                return value
    return os.environ.get("P12_PASSWORD") or "WSF"


def make_entry(display_name: str, source: str, p12: Path, profile: Path | None,
               password: str) -> Entry:
    display_name = " ".join(display_name.split())
    p12_bytes = p12.read_bytes()
    profile_bytes = profile.read_bytes() if profile else b""
    return Entry(
        name=safe_name(display_name),
        display_name=display_name,
        source=source,
        p12=p12,
        profile=profile,
        password=password,
        input_sha256=sha256_bytes(b"signing-input/1", p12_bytes, profile_bytes, password.encode()),
        problem=None if profile else "no provisioning profile next to the .p12",
    )


def walk(root_dir: Path, source: str) -> list[Entry]:
    """Every .p12 under a folder, each named after the folder it sits in."""
    entries = []
    p12s = [p for p in root_dir.rglob("*.p12")
            if not any(part.startswith(".") for part in p.relative_to(root_dir).parts)]
    for p12 in sorted(p12s, key=lambda p: str(p).encode()):
        cert_dir, stem = p12.parent, p12.stem
        group = stem if cert_dir == root_dir else cert_dir.name
        profile = None
        for candidate in (cert_dir / f"{stem}.mobileprovision", cert_dir / f"{group}.mobileprovision"):
            if candidate.is_file():
                profile = candidate
                break
        if profile is None:
            others = sorted(cert_dir.glob("*.mobileprovision"))
            profile = others[0] if others else None
        entries.append(make_entry(group, source, p12, profile, resolve_password(cert_dir, stem)))
    return entries


# ----- fetching each kind of source -----------------------------------------

def _fetch_local(source: str, root: Path) -> tuple[list[Entry], SourceResult]:
    path = Path(source)
    if not path.is_absolute():
        path = root / source
    if not path.is_dir():
        # A folder that does not exist is as good as an empty one.
        return [], SourceResult(source, "local", True, detail="folder does not exist")
    entries = walk(path, source)
    return entries, SourceResult(source, "local", True, len(entries))


def _fetch_nexcerts(source: str, dest: Path) -> tuple[list[Entry], SourceResult]:
    base = _nexcerts_base(source) or source
    status = os.environ.get("NEXCERTS_STATUS", "all")
    try:
        roster = json.loads(http_get(f"{base}/api/certificates/list/{status}", timeout=30))
    except (HTTPFailure, ValueError) as exc:
        return [], SourceResult(source, "nexcerts", False, detail=f"list request failed: {exc}")
    if not isinstance(roster, list) or not roster:
        # An empty list is treated as a failure, so an API hiccup can never
        # empty the page.
        return [], SourceResult(source, "nexcerts", False, detail="the API listed no certificates")

    rows, jobs = [], []
    for item in roster:
        if not isinstance(item, dict) or item.get("id") is None:
            continue
        cid = str(item["id"])
        if not re.fullmatch(r"[A-Za-z0-9_-]+", cid):
            continue
        name = str(item.get("folder_name") or item.get("name") or "").strip()
        name = "".join(" " if ord(c) < 32 else c for c in name).replace("/", "-").replace("\\", "-")
        if not name:
            continue
        folder = dest / cid
        api = f"{base}/api/certificates/download/{cid}"
        jobs += [(f"{api}/cert.p12", folder / "cert.p12"),
                 (f"{api}/cert.mobileprovision", folder / "cert.mobileprovision"),
                 (f"{api}/password.txt", folder / "password.txt")]
        rows.append((cid, name, folder))
    codes = download_many(jobs)

    entries, missing = [], []
    for cid, name, folder in rows:
        p12, profile = folder / "cert.p12", folder / "cert.mobileprovision"
        if not p12.exists() or not profile.exists():
            got = codes.get(p12) if not p12.exists() else codes.get(profile)
            if got in (404, 410):
                # Listed without a file (NexCerts' "missingP12"): nothing to sign.
                log(f"   NexCerts lists {name} (id {cid}) without a downloadable p12 or profile")
                continue
            missing.append(f"{name} (id {cid}, HTTP {got})")
            continue
        pw_file = folder / "password.txt"
        password = (password_from_file(pw_file) if pw_file.exists() else "") \
            or os.environ.get("P12_PASSWORD") or "WSF"
        entries.append(make_entry(name, source, p12, profile, password))
    if missing:
        # Not "ok": a certificate that failed to download must not be mistaken
        # for one that left the pool.
        return entries, SourceResult(source, "nexcerts", False, len(entries),
                                     detail="could not download " + ", ".join(missing))
    return entries, SourceResult(source, "nexcerts", True, len(entries))


def _fetch_github(source: str, dest: Path) -> tuple[list[Entry], SourceResult]:
    url = source.rstrip("/")
    url = url[:-4] if url.endswith(".git") else url
    try:
        run(["git", "clone", "--quiet", "--no-checkout", "--depth", "1", "--filter=blob:none", url, dest],
            timeout=300)
        run(["git", "-C", dest, "sparse-checkout", "set", "--no-cone", "*.p12", "*.mobileprovision",
             "password.txt", "*.password", "*.pass"], timeout=60)
        run(["git", "-C", dest, "checkout", "--quiet"], timeout=300)
    except (CommandError, OSError) as exc:
        return [], SourceResult(source, "github", False, detail=str(exc))
    shutil.rmtree(dest / ".git", ignore_errors=True)
    entries = walk(dest, source)
    return entries, SourceResult(source, "github", bool(entries), len(entries),
                                 detail="" if entries else "no certificates found")


def _fetch_zip(source: str, dest: Path) -> tuple[list[Entry], SourceResult]:
    archive = dest.with_suffix(".zip")
    try:
        archive.write_bytes(http_get(source, timeout=180))
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(dest)            # strips absolute paths and '..'
    except (HTTPFailure, zipfile.BadZipFile, OSError) as exc:
        return [], SourceResult(source, "zip", False, detail=str(exc))
    finally:
        archive.unlink(missing_ok=True)
    entries = walk(dest, source)
    return entries, SourceResult(source, "zip", bool(entries), len(entries),
                                 detail="" if entries else "no certificates found")


def fetch(root: Path, workdir: Path) -> Pool:
    sources, overridden = read_sources(root)
    workdir.mkdir(parents=True, exist_ok=True)

    def one(indexed: tuple[int, str]) -> tuple[list[Entry], SourceResult]:
        index, source = indexed
        dest = workdir / f"source-{index:02d}"
        kind = source_kind(source)
        if kind == "local":
            return _fetch_local(source, root)
        dest.mkdir(parents=True, exist_ok=True)
        return {"nexcerts": _fetch_nexcerts, "github": _fetch_github, "zip": _fetch_zip}[kind](source, dest)

    results = pmap(one, list(enumerate(sources)), workers=8)
    pool = Pool(overridden=overridden)
    for entries, result in results:
        pool.sources.append(result)
        state = "ok" if result.ok else "INCOMPLETE"
        log(f"   {result.kind:8} {result.source}: {result.count} certificate(s), {state}"
            + (f" ({result.detail})" if result.detail else ""))
        if not result.ok:
            warn(f"Certificate source {result.source}: {result.detail or 'failed'}")
        for entry in entries:
            kept = pool.entries.get(entry.name)
            if kept:
                log(f"   duplicate '{entry.name}' from {entry.source} skipped; "
                    f"the one from {kept.source} comes first")
                continue
            pool.entries[entry.name] = entry
    return pool
