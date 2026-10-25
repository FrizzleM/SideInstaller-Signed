"""Signing on macOS with Apple's codesign, many certificates at once.

All identities go into one throwaway keychain and every codesign call names
its identity by SHA-1 and that keychain (--keychain), so certificates that
share a name never get mixed up and the user's keychain search list is not
touched. The unsigned IPA is unpacked once; each certificate signs an APFS
clone of it, and the copies are signed in parallel."""
from __future__ import annotations

import copy
import plistlib
import secrets
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from . import ipa
from .certs import Profile, openssl
from .util import IN_ACTIONS, CommandError, cpu_count, log, pmap, run, sha256_file, warn

# Apple's WWDR intermediates. A pool's leaf certificates name their issuer, and
# that one is always fetched; these cover the rest so chains always build.
APPLE_CA = "https://www.apple.com/certificateauthority"
INTERMEDIATES = [f"{APPLE_CA}/AppleWWDRCAG{n}.cer" for n in (2, 3, 4, 5, 6)] + \
                ["https://www.apple.com/appleca/AppleIncRootCertificate.cer"]


class Keychain:
    """A temporary keychain with every signing identity of the run."""

    def __init__(self, workdir: Path):
        self.path = workdir / "signing.keychain-db"
        self.password = secrets.token_hex(16)
        self._search_list: list[str] | None = None

    def __enter__(self) -> "Keychain":
        if self.path.exists():
            self.path.unlink()
        run(["security", "create-keychain", "-p", self.password, self.path])
        run(["security", "set-keychain-settings", self.path])           # never auto-locks
        run(["security", "unlock-keychain", "-p", self.password, self.path])
        if IN_ACTIONS:
            # Belt and braces on the throwaway runner: also put it on the search
            # list, which is how chains were always built there. Locally,
            # --keychain alone is enough and nothing global is changed.
            self._search_list = self._current_search_list()
            run(["security", "list-keychains", "-d", "user", "-s", self.path, *self._search_list])
        return self

    def __exit__(self, *exc) -> None:
        if self._search_list is not None:
            run(["security", "list-keychains", "-d", "user", "-s", *self._search_list], check=False)
        run(["security", "delete-keychain", self.path], check=False)

    @staticmethod
    def _current_search_list() -> list[str]:
        out = run(["security", "list-keychains", "-d", "user"]).stdout.decode()
        return [line.strip().strip('"') for line in out.splitlines() if line.strip()]

    def add_certificate(self, der_path: Path) -> None:
        run(["security", "import", der_path, "-k", self.path], check=False)

    def _import_p12(self, p12: Path, password: str, check: bool) -> subprocess.CompletedProcess:
        return run(["security", "import", p12, "-k", self.path, "-f", "pkcs12", "-P", password,
                    "-A", "-T", "/usr/bin/codesign", "-T", "/usr/bin/security"], check=check)

    def add_identity(self, p12: Path, password: str, scratch: Path) -> None:
        proc = self._import_p12(p12, password, check=False)
        if proc.returncode == 0 or b"already exists" in proc.stderr:
            return
        # Some p12s use algorithms the keychain cannot read; an OpenSSL
        # round-trip into the legacy format fixes that.
        ossl = openssl()
        legacy = ["-legacy"] if ossl.legacy else []
        pem, repacked = scratch / f"{p12.stem}.pem", scratch / f"{p12.stem}-repacked.p12"
        env = {"SIGNER_P12_PASSWORD": password}
        run([ossl.path, "pkcs12", *legacy, "-in", p12, "-passin", "env:SIGNER_P12_PASSWORD",
             "-nodes", "-out", pem], env=env)
        run([ossl.path, "pkcs12", "-export", *legacy, "-in", pem, "-inkey", pem, "-out", repacked,
             "-passout", "env:SIGNER_P12_PASSWORD"], env=env)
        pem.unlink()
        self._import_p12(repacked, password, check=True)

    def allow_codesign(self) -> None:
        """Let codesign use the keys without a prompt."""
        run(["security", "set-key-partition-list", "-S", "apple-tool:,apple:,codesign:", "-s",
             "-k", self.password, self.path], check=False)

    def identities(self) -> set[str]:
        out = run(["security", "find-identity", "-p", "codesigning", self.path]).stdout.decode()
        return {line.split()[1] for line in out.splitlines()
                if ")" in line and len(line.split()) > 1 and len(line.split()[1]) == 40}


@dataclass
class Job:
    name: str
    p12: Path
    password: str
    profile_path: Path
    profile: Profile
    fingerprint: str


@dataclass
class Result:
    name: str
    ok: bool
    error: str = ""
    ipa: Path | None = None
    size: int = 0
    sha256: str = ""
    bundle_id: str = ""
    seconds: float = 0.0


class Signer:
    """Signs one unsigned IPA with many certificates."""

    def __init__(self, unsigned_ipa: Path, workdir: Path, keychain: Keychain,
                 forced_bundle_id: str = ""):
        self.workdir = workdir
        self.keychain = keychain
        self.forced_bundle_id = forced_bundle_id
        self.template = workdir / "template"
        shutil.rmtree(self.template, ignore_errors=True)
        self.dates = ipa.extract(unsigned_ipa, self.template)
        apps = sorted((self.template / "Payload").glob("*.app"))
        if not apps:
            raise ValueError("the IPA has no Payload/*.app")
        self.app_rel = apps[0].relative_to(self.template)
        self.info = ipa.app_info(apps[0])
        if not self.info["bundle_id"]:
            raise ValueError("the app's Info.plist has no CFBundleIdentifier")
        # Remove any signature the build left, once, before anything is cloned.
        for leftover in apps[0].rglob("_CodeSignature"):
            shutil.rmtree(leftover, ignore_errors=True)
        self.cache = ipa.DeflateCache()
        self.cache.prime(self.template)

    # -- one certificate ------------------------------------------------------

    def target_bundle_id(self, profile: Profile) -> str:
        if profile.is_wildcard:
            return self.forced_bundle_id or self.info["bundle_id"]
        return profile.app_id[len(profile.team_id) + 1:] if profile.app_id.startswith(
            profile.team_id + ".") else self.info["bundle_id"]

    @staticmethod
    def entitlements(profile: Profile, bundle_id: str) -> dict:
        """The profile's entitlements, pointed at the bundle id being signed."""
        ent = copy.deepcopy(profile.entitlements)
        team = profile.team_id
        ent["application-identifier"] = f"{team}.{bundle_id}"
        ent["com.apple.developer.team-identifier"] = team
        groups = ent.get("keychain-access-groups")
        if isinstance(groups, list) and groups:
            ent["keychain-access-groups"] = [f"{team}.{bundle_id}" if g == f"{team}.*" else g
                                             for g in groups]
        else:
            ent["keychain-access-groups"] = [f"{team}.{bundle_id}"]
        return ent

    def _codesign(self, path: Path, identity: str, entitlements: Path | None = None) -> None:
        cmd = ["codesign", "--force", "--sign", identity, "--keychain", str(self.keychain.path),
               "--timestamp=none", "--generate-entitlement-der"]
        if entitlements is not None:
            cmd += ["--entitlements", str(entitlements)]
        run(cmd + [str(path)], timeout=300)

    def _sign_nested(self, bundle: Path, identity: str) -> None:
        """Frameworks and dylibs, deepest first so each outer seal covers the inner."""
        frameworks = bundle / "Frameworks"
        if not frameworks.is_dir():
            return
        found = [p for p in frameworks.rglob("*") if p.suffix in (".framework", ".dylib")]
        for item in sorted(found, key=lambda p: (-len(p.parts), str(p))):
            self._codesign(item, identity)

    @staticmethod
    def _set_bundle_id(info_plist: Path, bundle_id: str) -> None:
        raw = info_plist.read_bytes()
        fmt = plistlib.FMT_BINARY if raw.startswith(b"bplist") else plistlib.FMT_XML
        data = plistlib.loads(raw)
        data["CFBundleIdentifier"] = bundle_id
        info_plist.write_bytes(plistlib.dumps(data, fmt=fmt))

    def sign(self, job: Job, out_dir: Path, prefix: str) -> Result:
        start = time.monotonic()
        work = self.workdir / "work" / job.name
        root = work / "ipa"                    # what gets zipped; entitlements stay outside it
        try:
            shutil.rmtree(work, ignore_errors=True)
            root.mkdir(parents=True)
            # An APFS clone: instant, and copy-on-write for the few files changed.
            run(["cp", "-cR", self.template / "Payload", root / "Payload"])
            app = root / self.app_rel
            profile = job.profile
            bundle_id = self.target_bundle_id(profile)

            self._set_bundle_id(app / "Info.plist", bundle_id)
            shutil.copyfile(job.profile_path, app / "embedded.mobileprovision")
            entitlements = work / "entitlements.plist"
            entitlements.write_bytes(plistlib.dumps(self.entitlements(profile, bundle_id)))
            self._sign_nested(app, job.fingerprint)

            # Extensions (PlugIns/*.appex) need ids of their own under the app's.
            # A wildcard profile covers those; an explicit one covers the app's
            # id only, so the extensions go, and the app does without them.
            plugins = app / "PlugIns"
            if plugins.is_dir() and not profile.is_wildcard:
                shutil.rmtree(plugins)
            elif plugins.is_dir():
                for appex in sorted(plugins.glob("*.appex")):
                    info = appex / "Info.plist"
                    with open(info, "rb") as fh:
                        old_id = str(plistlib.load(fh).get("CFBundleIdentifier", ""))
                    original = self.info["bundle_id"]
                    new_id = (bundle_id + old_id[len(original):] if old_id.startswith(original + ".")
                              else f"{bundle_id}.{appex.stem}")
                    self._set_bundle_id(info, new_id)
                    shutil.copyfile(job.profile_path, appex / "embedded.mobileprovision")
                    appex_ent = work / f"{appex.stem}.entitlements.plist"
                    appex_ent.write_bytes(plistlib.dumps(self.entitlements(profile, new_id)))
                    self._sign_nested(appex, job.fingerprint)
                    self._codesign(appex, job.fingerprint, appex_ent)

            self._codesign(app, job.fingerprint, entitlements)
            run(["codesign", "--verify", "--deep", "--strict", app], timeout=300)

            out = out_dir / f"{prefix}-{job.name}.ipa"
            ipa.pack(root, out, cache=self.cache, dates=self.dates)
            ipa.verify(out)
            return Result(job.name, True, ipa=out, size=out.stat().st_size, sha256=sha256_file(out),
                          bundle_id=bundle_id, seconds=time.monotonic() - start)
        except (CommandError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
            return Result(job.name, False, error=str(exc)[:300], seconds=time.monotonic() - start)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def sign_all(self, jobs: list[Job], out_dir: Path, prefix: str, workers: int = 0) -> list[Result]:
        out_dir.mkdir(parents=True, exist_ok=True)
        workers = workers or min(8, cpu_count() + 1)
        results = pmap(lambda job: self.sign(job, out_dir, prefix), jobs, workers)
        for r in results:
            if r.ok:
                log(f"   signed  {r.name} ({r.size / 1e6:.1f} MB, {r.seconds:.1f}s)")
            else:
                warn(f"Could not sign {r.name}: {r.error}")
        return results


def prepare_keychain(keychain: Keychain, jobs: list[Job], issuer_ders: list[bytes],
                     scratch: Path) -> set[str]:
    """Load intermediates and one p12 per distinct certificate; returns the
    fingerprints codesign can now use."""
    from .util import http_get, HTTPFailure
    scratch.mkdir(parents=True, exist_ok=True)

    def fetch(url: str) -> bytes | None:
        try:
            return http_get(url, timeout=20, retries=1)
        except HTTPFailure:
            return None

    blobs = list(issuer_ders) + [b for b in pmap(fetch, INTERMEDIATES, len(INTERMEDIATES)) if b]
    for i, blob in enumerate(blobs):
        path = scratch / f"ca-{i}.cer"
        path.write_bytes(blob)
        keychain.add_certificate(path)
    seen: set[str] = set()
    for job in jobs:
        if job.fingerprint in seen:
            continue
        seen.add(job.fingerprint)
        try:
            keychain.add_identity(job.p12, job.password, scratch)
        except CommandError as exc:
            warn(f"Could not import the identity of {job.name}: {exc}")
    keychain.allow_codesign()
    return keychain.identities()
