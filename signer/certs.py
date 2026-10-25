"""Reading certificates and provisioning profiles, and asking Apple whether a
certificate has been revoked.

Only opening a .p12 needs OpenSSL (it is encrypted). Everything else is read
here directly: the certificate fields by signer.der, the profile's property
list straight out of its CMS envelope, so the daily check runs the same on
Linux and macOS."""
from __future__ import annotations

import os
import plistlib
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import der
from .util import CommandError, der_to_pem, http_get, pem_to_ders, pmap, run, HTTPFailure


class CertError(Exception):
    """A pool entry that cannot be used, with a reason fit for the run log."""


# ----- OpenSSL ---------------------------------------------------------------

@dataclass
class OpenSSL:
    path: str
    version: str
    legacy: bool              # supports -legacy (OpenSSL 3+, needed for RC2-encrypted p12s)


_openssl: OpenSSL | None = None
_openssl_lock = threading.Lock()


def openssl() -> OpenSSL:
    """The best OpenSSL available: $OPENSSL, else a real OpenSSL 3+ (Homebrew's
    on macOS, whose /usr/bin/openssl is LibreSSL), else whatever 'openssl' is."""
    global _openssl
    with _openssl_lock:
        if _openssl is not None:
            return _openssl
        candidates = [os.environ.get("OPENSSL"), shutil.which("openssl"),
                      "/opt/homebrew/opt/openssl@3/bin/openssl", "/opt/homebrew/bin/openssl",
                      "/usr/local/opt/openssl@3/bin/openssl", "/usr/local/bin/openssl",
                      "/usr/bin/openssl"]
        found: list[OpenSSL] = []
        for path in dict.fromkeys(c for c in candidates if c):
            if not os.path.exists(path):
                continue
            try:
                version = run([path, "version"], timeout=10).stdout.decode().strip()
                helptext = run([path, "pkcs12", "-help"], check=False, timeout=10)
            except (OSError, CommandError):
                continue
            legacy = b"-legacy" in (helptext.stdout + helptext.stderr)
            found.append(OpenSSL(path, version, legacy))
            if os.environ.get("OPENSSL") == path or (version.startswith("OpenSSL") and legacy):
                break
        if not found:
            raise CertError("OpenSSL is not installed")
        preferred = [o for o in found if o.version.startswith("OpenSSL") and o.legacy]
        _openssl = (preferred or found)[0]
        return _openssl


# ----- .p12 ------------------------------------------------------------------

def read_p12(path: Path, password: str) -> der.Certificate:
    """The signing certificate inside a .p12, read with its password."""
    ossl = openssl()
    base = [ossl.path, "pkcs12"] + (["-legacy"] if ossl.legacy else [])
    # The password goes through the environment, never the command line.
    env = {"SIGNER_P12_PASSWORD": password}
    args = ["-in", str(path), "-nokeys", "-passin", "env:SIGNER_P12_PASSWORD"]
    proc = run(base + args + ["-clcerts"], check=False, env=env, timeout=30)
    if proc.returncode != 0 and ossl.legacy and b"provider" in (proc.stderr or b"").lower():
        # A build that advertises -legacy but ships without the legacy module.
        base = [ossl.path, "pkcs12"]
        proc = run(base + args + ["-clcerts"], check=False, env=env, timeout=30)
    if proc.returncode != 0:
        detail = (proc.stderr or b"").decode("utf-8", "replace")
        if "mac verify" in detail.lower() or "invalid password" in detail.lower():
            raise CertError("wrong .p12 password")
        raise CertError("unreadable .p12: " + " ".join(detail.split())[:160])
    ders = pem_to_ders(proc.stdout.decode("utf-8", "replace"))
    if not ders:
        # No localKeyID attributes: take every certificate and pick the leaf.
        proc = run(base + args, check=False, env=env, timeout=30)
        ders = pem_to_ders(proc.stdout.decode("utf-8", "replace"))
    certs = []
    for blob in ders:
        try:
            certs.append(der.parse_certificate(blob))
        except der.DERError:
            continue
    if not certs:
        raise CertError(".p12 holds no certificate")
    signing = [c for c in certs if "Developer Relations" not in c.common_name
               and "Root CA" not in c.common_name]
    return (signing or certs)[0]


# ----- provisioning profiles -------------------------------------------------

@dataclass
class Profile:
    name: str
    uuid: str
    team_id: str
    app_id: str                       # application-identifier: TEAMID.bundle.id or TEAMID.*
    expires: datetime
    entitlements: dict
    certificate_fingerprints: set[str] = field(default_factory=set)

    @property
    def is_wildcard(self) -> bool:
        return self.app_id == f"{self.team_id}.*"


def read_profile(path: Path) -> Profile:
    data = path.read_bytes()
    plist = None
    # A profile is a CMS envelope around an XML property list that is stored
    # as-is, so the plist can be cut out without parsing the envelope.
    start, end = data.find(b"<?xml"), data.rfind(b"</plist>")
    if start != -1 and end > start:
        try:
            plist = plistlib.loads(data[start:end + len(b"</plist>")])
        except Exception:
            plist = None
    if plist is None:
        ossl = openssl()
        proc = run([ossl.path, "cms", "-verify", "-noverify", "-inform", "DER", "-in", str(path)],
                   check=False, timeout=30)
        try:
            plist = plistlib.loads(proc.stdout)
        except Exception:
            raise CertError("unreadable provisioning profile") from None
    try:
        team_id = str(plist["TeamIdentifier"][0])
        entitlements = dict(plist.get("Entitlements") or {})
        app_id = str(entitlements["application-identifier"])
        expires = plist["ExpirationDate"]
    except (KeyError, IndexError, TypeError):
        raise CertError("provisioning profile lacks a team or application identifier") from None
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    fingerprints = set()
    for blob in plist.get("DeveloperCertificates") or []:
        try:
            fingerprints.add(der.parse_certificate(bytes(blob)).fingerprint)
        except der.DERError:
            continue
    return Profile(name=str(plist.get("Name", "")), uuid=str(plist.get("UUID", "")),
                   team_id=team_id, app_id=app_id, expires=expires,
                   entitlements=entitlements, certificate_fingerprints=fingerprints)


# ----- OCSP ------------------------------------------------------------------
#
# Apple revokes leaked enterprise certificates constantly, and a revoked one
# still signs and still installs; it just refuses to launch. Expiry says
# nothing about that, so ask Apple's OCSP responder, whose URL is in the
# certificate along with the URL of the certificate that issued it (OCSP needs
# the issuer to identify the certificate it is asking about).

@dataclass
class OCSPResult:
    status: str                    # valid | revoked | unknown | error
    revoked_at: datetime | None = None
    detail: str = ""


class OCSPChecker:
    def __init__(self, workdir: Path):
        self.dir = workdir / "ocsp"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._issuers: dict[str, Path] = {}
        self._lock = threading.Lock()

    def _issuer(self, url: str) -> Path:
        """Each issuer is fetched once; a whole pool shares one or two."""
        with self._lock:
            if url in self._issuers:
                return self._issuers[url]
            blob = http_get(url, timeout=20)
            if b"-----BEGIN CERTIFICATE-----" in blob:
                ders = pem_to_ders(blob.decode("ascii", "replace"))
                blob = ders[0] if ders else b""
            der.parse_certificate(blob)            # raises if it is not a certificate
            path = self.dir / f"issuer-{len(self._issuers)}.pem"
            path.write_text(der_to_pem(blob))
            self._issuers[url] = path
            return path

    def check(self, leaf_der: bytes, ocsp_url: str | None, issuer_url: str | None) -> OCSPResult:
        if not ocsp_url or not issuer_url:
            return OCSPResult("error", detail="certificate names no OCSP responder")
        cert = der.parse_certificate(leaf_der)
        leaf = self.dir / f"{cert.fingerprint}.pem"
        leaf.write_text(der_to_pem(leaf_der))
        last = OCSPResult("error")
        for attempt in range(3):
            try:
                issuer = self._issuer(issuer_url)
            except (HTTPFailure, der.DERError) as exc:
                last = OCSPResult("error", detail=f"issuer certificate: {exc}")
                time.sleep(1 + attempt)
                continue
            # -noverify skips checking the responder's signature: OCSP runs
            # over plain HTTP, so a man in the middle could at worst mislabel a
            # card. It never affects what is signed.
            proc = run([openssl().path, "ocsp", "-issuer", issuer, "-cert", leaf,
                        "-url", ocsp_url, "-no_nonce", "-noverify", "-timeout", "15"],
                       check=False, timeout=40)
            out = (proc.stdout + proc.stderr).decode("utf-8", "replace")
            match = re.search(re.escape(str(leaf)) + r":\s*(good|revoked|unknown)", out)
            if match:
                verdict = match.group(1)
                if verdict == "good":
                    return OCSPResult("valid")
                if verdict == "unknown":
                    return OCSPResult("unknown", detail="responder does not know it")
                when = re.search(r"Revocation Time:\s*(.+)", out)
                return OCSPResult("revoked", revoked_at=_openssl_time(when.group(1)) if when else None)
            last = OCSPResult("error", detail=" ".join(out.split())[:160])
            time.sleep(1 + attempt)
        return last

    def check_many(self, certs: dict[str, tuple[bytes, str | None, str | None]],
                   workers: int = 24) -> dict[str, OCSPResult]:
        """fingerprint -> result, for {fingerprint: (der, ocsp_url, issuer_url)}."""
        items = list(certs.items())
        results = pmap(lambda kv: self.check(*kv[1]), items, workers)
        return {fp: result for (fp, _), result in zip(items, results)}


def _openssl_time(text: str) -> datetime | None:
    text = " ".join(text.split())
    for fmt in ("%b %d %H:%M:%S %Y GMT", "%b %d %H:%M:%S %Y"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None
