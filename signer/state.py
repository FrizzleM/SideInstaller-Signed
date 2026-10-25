"""state.json, one per signed release, attached to it as an asset.

It records what each IPA on the release was signed from, so a later run can
tell a new or changed certificate from one that is already done; what Apple
last said about each certificate, so a change is noticed; and everything the
install page and the release notes show. The release is the single source of
truth: the page is rebuilt from it, and nothing is ever committed.

Each certificate entry looks like this (an entry that could not be signed has
"error" set and no "ipa"):

  name, display_name, source        file-name form, folder name, cert-url.txt line
  input_sha256                      hash of the p12, the profile and the password
  unsigned_ipa, signing_revision    the IPA and signing logic it was built with
  fingerprint, common_name          the signing certificate
  team_id, bundle_id
  certificate_expires, profile_expires
  ocsp, revoked_at                  Apple's answer: valid | revoked | unknown
  status, status_since              what the page shows: valid | revoked | expired | unknown
  leaf_certificate, ocsp_url, issuer_url   enough to ask Apple again without the p12
  ipa: {name, size, sha256}, signed_at
"""
from __future__ import annotations

import json
from datetime import datetime

from .util import iso, parse_iso

SCHEMA = 1
# Bump to re-sign every certificate after a change to how apps are signed.
SIGNING_REVISION = 1
STATE_ASSET = "state.json"
LEGACY_ASSETS = ("sign-state.tsv", "certificate-validity.tsv", "app-info.tsv")

STATUS_LABELS = {"valid": "Valid", "revoked": "Revoked", "expired": "Expired", "unknown": "Not checked"}


def loads(data: bytes) -> dict | None:
    try:
        state = json.loads(data)
    except ValueError:
        return None
    if not isinstance(state, dict) or state.get("schema") != SCHEMA:
        return None
    if not isinstance(state.get("certificates"), list):
        return None
    return state


def dumps(state: dict) -> bytes:
    return (json.dumps(state, indent=1, ensure_ascii=False) + "\n").encode()


def content(state: dict | None) -> str:
    """Everything that matters for deciding whether the release must change."""
    if state is None:
        return ""
    view = {k: v for k, v in state.items() if k != "updated_at"}
    view["certificates"] = sorted(view.get("certificates", []), key=lambda e: e.get("name", ""))
    return json.dumps(view, sort_keys=True)


def effective_expiry(entry: dict) -> datetime | None:
    """An app stops launching when either its certificate or its provisioning
    profile expires, whichever comes first."""
    dates = [d for d in (parse_iso(entry.get("certificate_expires")),
                         parse_iso(entry.get("profile_expires"))) if d]
    return min(dates) if dates else None


def derive_status(ocsp: str | None, expiry: datetime | None, now: datetime) -> str:
    if ocsp == "revoked":
        return "revoked"
    if expiry is not None and expiry <= now:
        return "expired"
    return "valid" if ocsp == "valid" else "unknown"


def days_left(expiry: datetime | None, now: datetime) -> int | None:
    if expiry is None:
        return None
    seconds = (expiry - now).total_seconds()
    return int(-(-seconds // 86400)) if seconds >= 0 else int(seconds // 86400)


def rank(entry: dict) -> int:
    """0 works, 1 never confirmed, 2 dead. The page and the notes lead with 0."""
    return {"valid": 0, "unknown": 1}.get(entry.get("status", "unknown"), 2)


def signed(state: dict | None) -> list[dict]:
    """The entries that have an IPA on the release."""
    return [e for e in (state or {}).get("certificates", []) if e.get("ipa")]


def sort_for_display(entries: list[dict], now: datetime) -> list[dict]:
    def key(e: dict):
        d = days_left(effective_expiry(e), now)
        return (rank(e), -(d if d is not None else -10**9), e.get("display_name", e["name"]).lower())
    return sorted(entries, key=key)


def release_notes(state: dict, *, main_repo: str, signed_repo: str, site_url: str,
                  prefix: str, now: datetime) -> str:
    tag, channel = state["tag"], state["channel"]
    version = (state.get("app") or {}).get("version") or tag
    unsigned = (state.get("unsigned_ipa") or {}).get("url")
    page = site_url.rstrip("/") + ("/beta.html" if channel == "beta" else "/")
    shown = page.split("://", 1)[-1].rstrip("/")
    source = f"[{main_repo} {tag}](https://github.com/{main_repo}/releases/tag/{tag})"
    lines = [f"SideInstaller {version}, signed once per certificate from",
             (f"[the unsigned IPA]({unsigned}) of {source}." if unsigned else f"{source}."),
             "", f"**Install from [{shown}]({page})**, with a certificate marked Valid.", ""]
    entries = sort_for_display(signed(state), now)
    if entries:
        lines += ["| Certificate | Expires | Apple says |", "| :-- | :-- | :-- |"]
        for e in entries:
            expiry = effective_expiry(e)
            name = e.get("display_name", e["name"]).replace("|", "\\|")
            lines.append(f"| {name} | {expiry.date().isoformat() if expiry else 'unknown'} | "
                         f"{STATUS_LABELS.get(e.get('status'), 'Not checked')} |")
        lines.append("")
    failed = [e for e in state.get("certificates", []) if e.get("error") and not e.get("ipa")]
    if failed:
        lines.append("Not signed: " + "; ".join(
            f"{e.get('display_name', e['name'])} ({e['error']})" for e in failed))
        lines.append("")
    updated = parse_iso(state.get("updated_at"))
    lines.append("Statuses are checked against Apple every day"
                 + (f"; last change {updated.date().isoformat()}." if updated else "."))
    lines.append("")
    if channel == "stable":
        lines += ["Fixed link to the newest stable build for one certificate:",
                  f"`https://github.com/{signed_repo}/releases/latest/download/{prefix}-<certificate>.ipa`"]
    else:
        lines.append("Beta build (prerelease): not on the public install page.")
    return "\n".join(lines) + "\n"


def stamp(entry: dict, **fields) -> dict:
    """A copy of entry with fields set, None values dropped."""
    out = dict(entry)
    for key, value in fields.items():
        if value is None:
            out.pop(key, None)
        else:
            out[key] = iso(value) if isinstance(value, datetime) else value
    return out
