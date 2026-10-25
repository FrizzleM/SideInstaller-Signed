"""The one command behind every run: bring the signed releases in line with
the main repository's releases and the certificate pool.

For each live release (the main repository's latest, plus its newest
prerelease) it works out, per certificate in the pool:

  up to date   signed from the same p12, profile, password, unsigned IPA and
               signing logic: only Apple's verdict is refreshed
  to sign      new, changed, or built from an older IPA (or everything, with
               --force); a release that does not exist yet gets all of them
  removed      gone from a source that answered in full: its IPA is deleted

Status changes and removals are published straight away, from any machine.
Signing needs codesign, so with --check-only (the Linux job) the run just
reports whether there is anything to sign, and the macOS job, running the
same command without it, signs and publishes the rest."""
from __future__ import annotations

import base64
import hashlib
import os
import re
import secrets
import shutil
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import certs, der, ipa
from . import pool as poolmod
from . import state as st
from .github import GitHub, discover_token
from .util import (Fatal, HTTPFailure, add_summary, group, http_get, is_macos, iso, log, pmap,
                   set_output, utcnow, warn)

TAG_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]*$")
UPLOAD_WORKERS = 6


@dataclass
class Config:
    root: Path
    workdir: Path
    main_repo: str = "FrizzleM/SideInstaller"
    signed_repo: str = "FrizzleM/SideInstaller-Signed"
    prefix: str = "sideinstaller"
    site_url: str = "https://sideinstaller.net"
    ipa_asset: str = "SideInstaller.ipa"
    forced_bundle_id: str = ""
    asset_wait: int = 600
    target_commit: str = ""

    @classmethod
    def from_env(cls, root: Path, workdir: Path) -> "Config":
        env = os.environ
        return cls(
            root=root, workdir=workdir,
            main_repo=env.get("MAIN_REPO") or cls.main_repo,
            signed_repo=env.get("SIGNED_REPO") or env.get("GITHUB_REPOSITORY") or cls.signed_repo,
            prefix=env.get("OUTPUT_PREFIX") or cls.prefix,
            site_url=env.get("SITE_URL") or cls.site_url,
            ipa_asset=env.get("IPA_ASSET_NAME") or cls.ipa_asset,
            forced_bundle_id=env.get("FORCED_BUNDLE_ID", ""),
            asset_wait=int(env.get("ASSET_WAIT") or 600),
            target_commit=env.get("GITHUB_SHA", ""),
        )

    def ipa_name(self, name: str) -> str:
        return f"{self.prefix}-{name}.ipa"

    def plist_name(self, name: str) -> str:
        return f"{self.prefix}-{name}.plist"

    def managed(self, asset: str) -> bool:
        """Assets this tool owns on a release; anything else is left alone."""
        return (asset == st.STATE_ASSET or asset in st.LEGACY_ASSETS
                or (asset.startswith(self.prefix + "-") and asset.endswith((".ipa", ".plist")))
                or (asset.startswith("upload-") and f"-{self.prefix}-" in asset))


@dataclass
class Target:
    tag: str
    channel: str                  # stable | beta
    make_latest: bool             # the main repository's latest: becomes Latest here too
    unsigned_asset: dict          # the unsigned IPA on the main repository's release
    unsigned_id: str              # its identity: sha256 digest, or id/size/date
    release: dict | None          # this repository's release of the tag, if any
    state: dict | None
    is_latest: bool = False       # already this repository's Latest

    @property
    def unsigned(self) -> dict:
        a = self.unsigned_asset
        return {"name": a["name"], "url": a["browser_download_url"], "id": a["id"],
                "size": a.get("size"), "digest": a.get("digest")}


@dataclass
class Inspection:
    entry: poolmod.Entry
    cert: der.Certificate | None = None
    profile: certs.Profile | None = None
    error: str | None = None


@dataclass
class Plan:
    target: Target
    entries: dict[str, dict] = field(default_factory=dict)    # what the release holds after this run
    to_sign: list[str] = field(default_factory=list)
    removed: list[dict] = field(default_factory=list)
    changes: list[str] = field(default_factory=list)


# ----- releases --------------------------------------------------------------

def _pick_ipa(release: dict, preferred: str) -> dict | None:
    ipas = [a for a in release.get("assets", [])
            if a.get("state") == "uploaded" and a["name"].lower().endswith(".ipa")]
    for a in ipas:
        if a["name"] == preferred:
            return a
    return ipas[0] if ipas else None


def resolve_targets(gh: GitHub, cfg: Config, tag: str | None) -> list[Target]:
    main_latest = gh.latest_release(cfg.main_repo)
    latest_tag = main_latest["tag_name"] if main_latest else None
    if tag:
        if not TAG_RE.match(tag):
            raise Fatal(f"Not a usable release tag: {tag!r}")
        rel = gh.release_by_tag(cfg.main_repo, tag)
        if rel is None:
            raise Fatal(f"{cfg.main_repo} has no published release {tag}")
        wanted = [rel]
    else:
        wanted = [main_latest] if main_latest else []
        pre = [r for r in gh.releases(cfg.main_repo, 30) if r.get("prerelease") and not r.get("draft")]
        if pre:
            newest = max(pre, key=lambda r: r.get("created_at") or "")
            if newest["tag_name"] != latest_tag:
                wanted.append(newest)
        if not wanted:
            warn(f"{cfg.main_repo} has no published release yet")

    signed = {r["tag_name"]: r for r in gh.releases(cfg.signed_repo)}       # drafts included
    signed_latest = gh.latest_release(cfg.signed_repo)
    targets = []
    for rel in wanted:
        asset = _pick_ipa(rel, cfg.ipa_asset)
        waited = 0
        # A release published by hand can go live before its IPA has uploaded.
        while asset is None and tag and waited < cfg.asset_wait:
            log(f"   {cfg.main_repo} {rel['tag_name']} has no .ipa yet; checking again in 20s")
            time.sleep(20)
            waited += 20
            rel = gh.release_by_tag(cfg.main_repo, rel["tag_name"]) or rel
            asset = _pick_ipa(rel, cfg.ipa_asset)
        if asset is None:
            if tag:
                raise Fatal(f"{cfg.main_repo} {rel['tag_name']} has no .ipa asset (waited {waited}s)")
            warn(f"{cfg.main_repo} {rel['tag_name']} has no .ipa asset yet; skipping it this run")
            continue
        own = signed.get(rel["tag_name"])
        state = None
        if own:
            found = next((a for a in own.get("assets", []) if a["name"] == st.STATE_ASSET), None)
            if found:
                path = cfg.workdir / "states" / rel["tag_name"] / st.STATE_ASSET
                state = st.loads(gh.download_asset(cfg.signed_repo, found, path).read_bytes())
                if state is None:
                    warn(f"{rel['tag_name']}: {st.STATE_ASSET} is unreadable; treating the release as unsigned")
        digest = asset.get("digest")
        targets.append(Target(
            tag=rel["tag_name"],
            channel="beta" if rel.get("prerelease") else "stable",
            make_latest=not rel.get("prerelease") and rel["tag_name"] == latest_tag,
            unsigned_asset=asset,
            unsigned_id=digest or f"asset:{asset['id']}:{asset.get('size')}:{asset.get('updated_at')}",
            release=own,
            state=state,
            is_latest=bool(own and signed_latest and own["id"] == signed_latest["id"]),
        ))
        kind = "new" if own is None else ("made by the old scripts" if state is None else
                                           f"{len(state['certificates'])} certificate(s) recorded")
        log(f"   {rel['tag_name']} ({targets[-1].channel}): {kind}")
    return targets


# ----- planning --------------------------------------------------------------

def _built_from(record: dict | None, entry: poolmod.Entry, target: Target) -> bool:
    """record (an entry, or the inputs that last failed) matches what the pool
    holds now. A certificate that could not even be read is always read again:
    that costs nothing and never starts a Mac, and it means a passing problem
    (a flaky OpenSSL, say) can never leave a certificate stuck."""
    return bool(record) and record.get("input_sha256") == entry.input_sha256 \
        and record.get("unsigned_ipa") == target.unsigned_id \
        and record.get("signing_revision") == st.SIGNING_REVISION \
        and record.get("stage") != "read"


def plan_target(target: Target, pool: poolmod.Pool, force: bool) -> Plan:
    plan = Plan(target)
    old = {e["name"]: e for e in (target.state or {}).get("certificates", [])}
    for name, entry in pool.entries.items():
        record = old.get(name)
        if record is not None:
            # Kept until a new build replaces it, so its card never disappears.
            plan.entries[name] = dict(record, display_name=entry.display_name, source=entry.source)
        current = record is not None and not force and (
            _built_from(record, entry, target) or _built_from(record.get("failed_input"), entry, target))
        if not current:
            plan.to_sign.append(name)
    for name, record in old.items():
        if name in pool.entries:
            continue
        source = record.get("source", "")
        if (pool.listed(source) and pool.source_ok(source)) or (not pool.listed(source) and not pool.overridden):
            plan.removed.append(record)
            plan.changes.append(f"removed {record.get('display_name', name)} (no longer in its source)")
        else:
            plan.entries[name] = record           # its source did not answer in full: keep it
    return plan


def inspect(entry: poolmod.Entry) -> Inspection:
    if entry.problem:
        return Inspection(entry, error=entry.problem)
    try:
        cert = certs.read_p12(entry.p12, entry.password)
        profile = certs.read_profile(entry.profile)
    except (certs.CertError, der.DERError, OSError) as exc:
        return Inspection(entry, error=str(exc))
    if profile.certificate_fingerprints and cert.fingerprint not in profile.certificate_fingerprints:
        return Inspection(entry, cert, profile, error="the provisioning profile is for another certificate")
    return Inspection(entry, cert, profile)


def base_entry(entry: poolmod.Entry, target: Target) -> dict:
    return {"name": entry.name, "display_name": entry.display_name, "source": entry.source,
            "input_sha256": entry.input_sha256, "unsigned_ipa": target.unsigned_id,
            "signing_revision": st.SIGNING_REVISION}


def record_failure(plan: Plan, entry: poolmod.Entry, error: str, stage: str) -> None:
    """A certificate that cannot be used is remembered with the inputs that
    failed. One that failed to sign is not retried (that would start a Mac
    every day) until any of those inputs changes. A working build of it from
    before stays."""
    failed = {"input_sha256": entry.input_sha256, "unsigned_ipa": plan.target.unsigned_id,
              "signing_revision": st.SIGNING_REVISION, "stage": stage, "error": error}
    previous = plan.entries.get(entry.name)
    if previous and previous.get("ipa"):
        plan.entries[entry.name] = dict(previous, failed_input=failed)
    else:
        plan.entries[entry.name] = {**base_entry(entry, plan.target), "stage": stage, "error": error}
    if entry.name in plan.to_sign:
        plan.to_sign.remove(entry.name)
    plan.changes.append(f"could not sign {entry.display_name}: {error}")


def apply_status(entry: dict, result: certs.OCSPResult | None, now: datetime) -> tuple[dict, str | None]:
    """entry with Apple's latest answer; and what changed, if anything. When
    Apple could not be reached, the previous answer stands."""
    if not entry.get("ipa"):
        return entry, None
    ocsp, revoked_at = entry.get("ocsp") or "unknown", entry.get("revoked_at")
    if result is not None and result.status in ("valid", "revoked", "unknown"):
        ocsp = result.status
        if ocsp == "revoked":
            revoked_at = iso(result.revoked_at) if result.revoked_at else revoked_at
        else:
            revoked_at = None
    status = st.derive_status(ocsp, st.effective_expiry(entry), now)
    new = st.stamp(entry, ocsp=ocsp, revoked_at=revoked_at, status=status)
    if status == entry.get("status"):
        return new, None
    new["status_since"] = iso(now)
    before = entry.get("status")
    what = f"{entry.get('display_name', entry['name'])}: {before} -> {status}" if before else None
    return new, what


def build_state(target: Target, entries: dict[str, dict], now: datetime, app: dict | None = None) -> dict:
    previous = target.state or {}
    state = {
        "schema": st.SCHEMA,
        "tag": target.tag,
        "channel": target.channel,
        "app": app or previous.get("app") or {},
        "unsigned_ipa": target.unsigned,
        "certificates": [entries[name] for name in sorted(entries)],
        "updated_at": previous.get("updated_at"),
    }
    if st.content(state) != st.content(target.state):
        state["updated_at"] = iso(now)
    return state


# ----- publishing ------------------------------------------------------------

def _upload(gh: GitHub, cfg: Config, release: dict, name: str, data: bytes, ctype: str) -> dict:
    try:
        return gh.upload_asset(release, name, data, ctype)
    except HTTPFailure as exc:
        if exc.status != 422:
            raise
        # The name is taken: by this very upload, if it went through and only
        # the response was lost, or by a half-made asset a failed one left.
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        for asset in gh.release(cfg.signed_repo, release["id"]).get("assets", []):
            if asset["name"] != name:
                continue
            if asset.get("state") == "uploaded" and asset.get("size") == len(data) \
                    and asset.get("digest") in (None, digest):
                return asset
            gh.delete_asset(cfg.signed_repo, asset)
        return gh.upload_asset(release, name, data, ctype)


def _content_type(name: str) -> str:
    return {"json": "application/json", "plist": "application/xml"}.get(
        name.rsplit(".", 1)[-1], "application/octet-stream")


def publish(gh: GitHub, cfg: Config, target: Target, new_state: dict, now: datetime, *,
            uploads: dict[str, Path | bytes] | None = None, delete: set[str] | None = None,
            cleanup: bool = False, keep: set[str] | None = None) -> None:
    """Make the release hold exactly new_state: its files first, then
    state.json, then deletions, then notes and flags. A new release is built
    as a draft and published once complete, so nobody sees it half-uploaded.
    Afterwards target.release and target.state describe the result."""
    uploads = dict(uploads or {})
    notes = st.release_notes(new_state, main_repo=cfg.main_repo, signed_repo=cfg.signed_repo,
                             site_url=cfg.site_url, prefix=cfg.prefix, now=now)
    release = target.release
    if release is None:
        fields = {"tag_name": target.tag, "name": target.tag, "body": notes, "draft": True,
                  "prerelease": target.channel == "beta"}
        if cfg.target_commit:
            fields["target_commitish"] = cfg.target_commit
        release = gh.create_release(cfg.signed_repo, **fields)
    assets = {a["name"]: a for a in release.get("assets", [])}

    if gh.dry_run:
        out = cfg.workdir / "dry-run" / target.tag
        out.mkdir(parents=True, exist_ok=True)
        (out / st.STATE_ASSET).write_bytes(st.dumps(new_state))
        (out / "notes.md").write_text(notes)

    def put(item: tuple[str, Path | bytes]) -> bool:
        name, src = item
        data = src.read_bytes() if isinstance(src, Path) else src
        current = assets.get(name)
        if current and current.get("digest") == "sha256:" + hashlib.sha256(data).hexdigest():
            return False                              # already there, byte for byte
        if current is None:
            _upload(gh, cfg, release, name, data, _content_type(name))
            return True
        # Replace without a gap long enough to matter: upload beside it, then swap.
        temp = _upload(gh, cfg, release, f"upload-{secrets.token_hex(4)}-{name}", data, _content_type(name))
        gh.delete_asset(cfg.signed_repo, current)
        for attempt in range(4):
            try:
                gh.rename_asset(cfg.signed_repo, temp, name)
                break
            except HTTPFailure:
                if attempt == 3:
                    raise
                time.sleep(1 + attempt)
        return True

    uploaded = sum(pmap(put, sorted(uploads.items()), UPLOAD_WORKERS))
    put((st.STATE_ASSET, st.dumps(new_state)))

    referenced = {st.STATE_ASSET}
    for entry in st.signed(new_state):
        referenced |= {cfg.ipa_name(entry["name"]), cfg.plist_name(entry["name"])}
    doomed = set(delete or ())
    if cleanup:
        doomed |= {n for n in assets if cfg.managed(n)}
    # keep: builds of certificates that just failed to re-sign. Even when no
    # longer on the page, an IPA that worked is never deleted for that.
    doomed = {n for n in doomed if n in assets and n not in referenced and n not in (keep or ())}
    pmap(lambda n: gh.delete_asset(cfg.signed_repo, assets[n]), sorted(doomed), UPLOAD_WORKERS)

    fields = {}
    if release.get("body") != notes:
        fields["body"] = notes
    if release.get("name") != target.tag:
        fields["name"] = target.tag
    if bool(release.get("prerelease")) != (target.channel == "beta"):
        fields["prerelease"] = target.channel == "beta"
    if release.get("draft"):
        fields["draft"] = False
        fields["make_latest"] = "true" if target.make_latest else "false"
    elif target.make_latest and not target.is_latest:
        fields["make_latest"] = "true"
    if fields:
        gh.update_release(cfg.signed_repo, release["id"], **fields)
    log(f"   published {target.tag}: {uploaded} file(s) uploaded, {len(doomed)} deleted"
        + (", notes updated" if "body" in fields else ""))
    target.state = new_state
    target.release = release if gh.dry_run else gh.release(cfg.signed_repo, release["id"])


# ----- signing (macOS) -------------------------------------------------------

def sign_and_publish(gh: GitHub, cfg: Config, plans: list[Plan], inspections: dict[str, Inspection],
                     ocsp: dict[str, certs.OCSPResult], now: datetime) -> list[Plan]:
    from . import codesign
    pending = [p for p in plans if p.to_sign]
    names = sorted({n for p in pending for n in p.to_sign})
    jobs = {}
    for name in names:
        insp = inspections[name]
        jobs[name] = codesign.Job(name, insp.entry.p12, insp.entry.password, insp.entry.profile,
                                  insp.profile, insp.cert.fingerprint)
    workdir = cfg.workdir / "sign"
    shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True)
    issuers = []
    for url in sorted({inspections[n].cert.issuer_url for n in names if inspections[n].cert.issuer_url}):
        try:
            issuers.append(http_get(url, timeout=20))
        except HTTPFailure as exc:
            warn(f"Could not fetch issuer certificate {url}: {exc}")

    with codesign.Keychain(workdir) as keychain:
        with group(f"Loading {len(jobs)} identities"):
            usable = codesign.prepare_keychain(keychain, list(jobs.values()), issuers, workdir / "keychain")
        for plan in pending:
            target = plan.target
            with group(f"Signing {target.tag} with {len(plan.to_sign)} certificate(s)"):
                tdir = workdir / target.tag
                unsigned = gh.download_asset(cfg.main_repo, target.unsigned_asset, tdir / "unsigned.ipa")
                digest = target.unsigned_asset.get("digest")
                actual = "sha256:" + hashlib.sha256(unsigned.read_bytes()).hexdigest()
                if digest and digest != actual:
                    raise Fatal(f"{target.tag}: the unsigned IPA does not match its digest")
                try:
                    signer = codesign.Signer(unsigned, tdir, keychain, cfg.forced_bundle_id)
                except (ValueError, OSError, zipfile.BadZipFile) as exc:
                    raise Fatal(f"{target.tag}: cannot use the unsigned IPA: {exc}") from None
                attempted = list(plan.to_sign)
                todo = [jobs[n] for n in attempted if jobs[n].fingerprint in usable]
                results = signer.sign_all(todo, tdir / "out", cfg.prefix)
                ok = {r.name for r in results if r.ok}
                if len(attempted) >= 3 and not ok:
                    # Every certificate failing points at this machine, not at
                    # them. Leave the release exactly as it is and let the next
                    # run try again.
                    first = next((r.error for r in results if not r.ok), "no identity could be imported")
                    raise Fatal(f"{target.tag}: all {len(attempted)} certificates failed to sign, so nothing "
                                f"was published. First error: {first}")

            uploads: dict[str, Path | bytes] = {}
            failed: set[str] = set()
            for name in attempted:
                if jobs[name].fingerprint not in usable:
                    record_failure(plan, inspections[name].entry, "the keychain would not take its identity", "sign")
                    failed.add(name)
            for result in results:
                insp = inspections[result.name]
                if not result.ok:
                    record_failure(plan, insp.entry, result.error, "sign")
                    failed.add(result.name)
                    continue
                cert, profile = insp.cert, insp.profile
                entry = {**base_entry(insp.entry, target),
                         "fingerprint": cert.fingerprint, "common_name": cert.common_name,
                         "team_id": profile.team_id, "bundle_id": result.bundle_id,
                         "certificate_expires": iso(cert.not_after), "profile_expires": iso(profile.expires),
                         "leaf_certificate": base64.b64encode(cert.der).decode(),
                         "ocsp_url": cert.ocsp_url, "issuer_url": cert.issuer_url,
                         "ipa": {"name": cfg.ipa_name(result.name), "size": result.size, "sha256": result.sha256},
                         "signed_at": iso(now)}
                previous = plan.entries.get(result.name)
                if previous and previous.get("status"):
                    entry.update({k: previous[k] for k in ("ocsp", "revoked_at", "status", "status_since")
                                  if k in previous})
                entry, _ = apply_status(entry, ocsp.get(cert.fingerprint), now)
                plan.entries[result.name] = entry
                verb = "re-signed" if previous and previous.get("ipa") else "added"
                plan.changes.append(f"{verb} {insp.entry.display_name} ({entry['status']})")
                uploads[cfg.ipa_name(result.name)] = result.ipa
                url = f"https://github.com/{cfg.signed_repo}/releases/download/{target.tag}/{cfg.ipa_name(result.name)}"
                uploads[cfg.plist_name(result.name)] = ipa.manifest(
                    url, result.bundle_id, signer.info["version"], signer.info["title"]).encode()
            plan.to_sign = []
            app = {k: signer.info[k] for k in ("title", "version", "bundle_id")}
            with group(f"Publishing {target.tag}"):
                publish(gh, cfg, target, build_state(target, plan.entries, now, app), now,
                        uploads=uploads, cleanup=True,
                        delete={n for r in plan.removed for n in (cfg.ipa_name(r["name"]), cfg.plist_name(r["name"]))},
                        keep={n for f in failed for n in (cfg.ipa_name(f), cfg.plist_name(f))})
    return pending


# ----- the command -----------------------------------------------------------

def sync(cfg: Config, *, tag: str | None = None, force: bool = False, check_only: bool = False,
         dry_run: bool = False) -> bool:
    """Returns True when anything was (or, in a dry run, would be) published."""
    started = time.monotonic()
    now = utcnow()
    gh = GitHub(discover_token(), dry_run=dry_run)
    cfg.workdir.mkdir(parents=True, exist_ok=True)

    with group("Releases"):
        targets = resolve_targets(gh, cfg, tag)
    if not targets:
        set_output("needs_signing", "false")
        set_output("changed", "false")
        return False

    with group("Certificate pool"):
        pool = poolmod.fetch(cfg.root, cfg.workdir / "pool")
        log(f"   {len(pool.entries)} certificate(s) in the pool")

    plans = [plan_target(t, pool, force) for t in targets]

    with group("Reading new and changed certificates"):
        names = sorted({n for p in plans for n in p.to_sign})
        inspections = dict(zip(names, pmap(lambda n: inspect(pool.entries[n]), names, 16)))
        for plan in plans:
            for name in list(plan.to_sign):
                if inspections[name].error:
                    record_failure(plan, pool.entries[name], inspections[name].error, "read")
        log(f"   {len(names)} to read, {sum(1 for i in inspections.values() if not i.error)} usable")

    with group("Asking Apple about every certificate"):
        wanted: dict[str, tuple[bytes, str | None, str | None]] = {}
        for plan in plans:
            for entry in plan.entries.values():
                if entry.get("ipa") and entry.get("leaf_certificate"):
                    wanted[entry["fingerprint"]] = (base64.b64decode(entry["leaf_certificate"]),
                                                    entry.get("ocsp_url"), entry.get("issuer_url"))
        for insp in inspections.values():
            if insp.cert and not insp.error:
                wanted[insp.cert.fingerprint] = (insp.cert.der, insp.cert.ocsp_url, insp.cert.issuer_url)
        ocsp = certs.OCSPChecker(cfg.workdir).check_many(wanted)
        for fp, result in ocsp.items():
            if result.status == "error":
                warn(f"OCSP for {fp[:8]}: {result.detail}; keeping its previous status")
        tally: dict[str, int] = {}
        for result in ocsp.values():
            tally[result.status] = tally.get(result.status, 0) + 1
        log(f"   {len(ocsp)} distinct certificate(s): " + ", ".join(f"{v} {k}" for k, v in sorted(tally.items())))
        for plan in plans:
            for name, entry in list(plan.entries.items()):
                fresh, what = apply_status(entry, ocsp.get(entry.get("fingerprint", "")), now)
                plan.entries[name] = fresh
                if what:
                    plan.changes.append(what)

    changed = False
    with group("Publishing status changes"):
        for plan in plans:
            t = plan.target
            if t.release is None or t.state is None:
                continue          # new, or made by the old scripts: published whole once signed
            if plan.to_sign and not check_only:
                continue          # published together with the new builds, below
            new_state = build_state(t, plan.entries, now)
            if st.content(new_state) == st.content(t.state) and not plan.removed:
                log(f"   {t.tag}: no change")
                continue
            removed = {n for r in plan.removed for n in (cfg.ipa_name(r["name"]), cfg.plist_name(r["name"]))}
            publish(gh, cfg, t, new_state, now, delete=removed, cleanup=not plan.to_sign)
            changed = True

    needs_signing = any(p.to_sign for p in plans)
    if needs_signing and not check_only:
        if not is_macos():
            raise Fatal("Signing needs macOS; run with --check-only to only check")
        sign_and_publish(gh, cfg, plans, inspections, ocsp, now)
        changed = True
        needs_signing = False

    _report(plans, check_only, time.monotonic() - started)
    set_output("needs_signing", "true" if needs_signing else "false")
    set_output("changed", "true" if changed else "false")
    return changed


def _report(plans: list[Plan], check_only: bool, seconds: float) -> None:
    lines = []
    for plan in plans:
        t = plan.target
        entries = [e for e in plan.entries.values() if e.get("ipa")]
        valid = sum(1 for e in entries if e.get("status") == "valid")
        head = f"{t.tag} ({t.channel}): {len(entries)} signed certificate(s), {valid} valid"
        if plan.to_sign:
            head += f"; {len(plan.to_sign)} to sign" + (" on macOS" if check_only else "")
        log(head)
        lines.append(f"### {head}")
        for change in plan.changes:
            log(f"   - {change}")
            lines.append(f"- {change}")
        if not plan.changes and not plan.to_sign:
            lines.append("Nothing changed.")
    log(f"Done in {seconds:.1f}s")
    add_summary("\n".join(lines) + f"\n\n_{seconds:.1f}s_")
