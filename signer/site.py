"""The install site (sideinstaller.net), rebuilt from the releases.

  index.html   the latest stable release, one card per signed certificate
  beta.html    the newest prerelease, or site/beta-closed.html without one
  stable/      index.html's OTA manifests, each pointing at an IPA on the release
  beta/        beta.html's
  terms.html, open.html, the icons and the DNS profile, from the main repository

Cards are ordered so the certificates that work come first: valid, then never
confirmed, then revoked or expired; longest-lived first within each group."""
from __future__ import annotations

import html
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from . import ipa
from . import state as st
from .github import GitHub, discover_token
from .util import Fatal, iso, log, parse_iso, pmap, utcnow, warn

STATIC_FILES = ("favicon-32.png", "favicon-192.png", "app-icon.png",
                "SideInstallerDNS.mobileconfig", "terms.html", "open.html")
INSTALL_ICON = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" '
                'stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12"/>'
                '<path d="M7 11l5 5 5-5"/><path d="M5 21h14"/></svg>')
TAGLINE = "On-device sideloader. Follow the three steps below to get set up."
BETA_TAGLINE = "Beta test build, not the public release. Follow the three steps below to get set up."


def _esc(value: str) -> str:
    return html.escape(value, quote=True)


def countdown(days: int | None) -> tuple[str, str]:
    """(class, label) of the days-left text; the template's script redraws it."""
    if days is None:
        return "unknown", "Unknown"
    if days < 0:
        return "bad", "Expired"
    if days == 0:
        return "crit", "Expires today"
    if days == 1:
        return "crit", "1 day left"
    if days <= 7:
        return "crit", f"{days} days left"
    if days <= 30:
        return "warn", f"{days} days left"
    return "good", f"{days} days left"


def card(entry: dict, install_url: str, now: datetime) -> str:
    expiry = st.effective_expiry(entry)
    days = st.days_left(expiry, now)
    status = entry.get("status", "unknown")
    name = _esc(entry.get("display_name") or entry["name"])
    pill = '<span class="pill good">Valid</span>' if status == "valid" else ""
    cls, label = countdown(days)
    meta = []
    if expiry:
        meta.append(f"Expires {expiry.date().isoformat()}")
    if cls != "unknown":
        meta.append(f'<span class="days {cls}">{label}</span>')
    meta_line = f'<p class="cert-meta">{" &middot; ".join(meta)}</p>' if meta else ""
    expires_attr = f' data-expires="{iso(expiry)}"' if expiry else ""
    return (f'    <article class="cert-card" data-name="{name}" data-days="{days if days is not None else -999999}"'
            f'{expires_attr} data-rank="{st.rank(entry)}" data-status="{status}">\n'
            f'      <div class="cert-head">\n'
            f'        <h3 class="cert-name">{name}</h3>\n'
            f'        {pill}\n'
            f'      </div>\n'
            f'      {meta_line}\n'
            f'      <a class="install-btn" href="{_esc(install_url)}">{INSTALL_ICON} Install</a>\n'
            f'    </article>\n')


def render(template: str, state: dict, *, manifest_base: str, page_title: str, tagline: str,
           download_url: str, logo_url: str, prefix: str, now: datetime) -> str:
    app = state.get("app") or {}
    app_name = app.get("title") or "SideInstaller"
    entries = st.sort_for_display(st.signed(state), now)
    cards = "".join(card(e, f"itms-services://?action=download-manifest&url={manifest_base}/"
                            f"{prefix}-{e['name']}.plist", now) for e in entries)
    if not cards:
        cards = '    <p class="empty show">No signed builds available yet. Check back soon.</p>\n'
    valid = sum(1 for e in entries if e.get("status") == "valid")
    paris = now.astimezone(_paris())
    tokens = {
        "{{PAGE_TITLE}}": _esc(page_title or app_name),
        "{{APP_NAME}}": _esc(app_name),
        "{{APP_TAGLINE}}": _esc(tagline),
        "{{APP_VERSION}}": _esc(app.get("version") or "—"),
        "{{CERT_COUNT}}": str(len(entries)),
        "{{LOGO}}": f'<img src="{_esc(logo_url)}" alt="{_esc(app_name)}">',
        "{{LAST_UPDATED}}": paris.strftime("%d %b %Y, %H:%M CET"),
        "{{LATEST_RELEASE_URL}}": _esc(download_url),
        "{{REPO_NOTE}}": (f"Built automatically &middot; signed with {len(entries)} certificate(s), "
                          f"{valid} currently valid"),
    }
    out = []
    for line in template.splitlines(keepends=True):
        if "{{CARDS}}" in line:
            out.append(cards)
            continue
        for token, value in tokens.items():
            line = line.replace(token, value)
        out.append(line)
    return "".join(out)


def _paris():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("Europe/Paris")
    except Exception:                          # no tz database: CET without DST
        from datetime import timedelta
        return timezone(timedelta(hours=1))


# ----- reading a release ------------------------------------------------------

def load_release(gh: GitHub, repo: str, release: dict, workdir: Path,
                 prefix: str) -> tuple[dict, dict[str, bytes]]:
    """(state, manifests by file name). A release the old scripts made has no
    state.json; one is pieced together from its TSV files and manifests."""
    tag = release["tag_name"]
    folder = workdir / tag
    assets = {a["name"]: a for a in release.get("assets", [])}
    for _ in range(3):
        if st.STATE_ASSET in assets or "certificate-validity.tsv" in assets:
            break
        # Mid-way through a sign run swapping state.json? Look again shortly.
        time.sleep(3)
        assets = {a["name"]: a for a in gh.release(repo, release["id"]).get("assets", [])}
    else:
        raise Fatal(f"{repo} {tag} has no {st.STATE_ASSET}; not deploying a page without certificates")
    if st.STATE_ASSET in assets:
        state = st.loads(gh.download_asset(repo, assets[st.STATE_ASSET], folder / st.STATE_ASSET).read_bytes())
        if state is None:
            raise Fatal(f"{repo} {tag}: {st.STATE_ASSET} is unreadable")
        return state, {}

    warn(f"{tag} has no {st.STATE_ASSET} yet (made by the old scripts); reading its TSV files")
    def text(name: str) -> str:
        return gh.download_asset(repo, assets[name], folder / name).read_text() if name in assets else ""
    app = dict(line.split("\t", 1) for line in text("app-info.tsv").splitlines() if "\t" in line)
    plists = sorted(n for n in assets if n.endswith(".plist"))
    blobs = pmap(lambda n: gh.download_asset(repo, assets[n], folder / n).read_bytes(), plists, 8)
    manifests = dict(zip(plists, blobs))
    entries, now = {}, utcnow()
    for line in text("certificate-validity.tsv").splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        name, expires, _, revocation = parts[:4]
        expiry = f"{expires}T00:00:00Z" if len(expires) == 10 else None
        entry = {"name": name, "display_name": name, "certificate_expires": expiry,
                 "ocsp": revocation if revocation in ("valid", "revoked") else "unknown",
                 "ipa": {"name": f"{prefix}-{name}.ipa"}}
        entry["status"] = st.derive_status(entry["ocsp"], parse_iso(expiry), now)
        entries[name] = entry
    return ({"tag": tag, "channel": "beta" if release.get("prerelease") else "stable",
             "app": {"title": app.get("title", ""), "version": app.get("version", "")},
             "certificates": list(entries.values())}, manifests)


# ----- the command ------------------------------------------------------------

def build(*, root: Path, out: Path, static_dir: Path, site_url: str, signed_repo: str,
          main_repo: str, prefix: str, ipa_asset: str, workdir: Path,
          from_dir: Path | None = None) -> None:
    site_url = "https://" + site_url.split("://", 1)[-1].rstrip("/")   # itms-services needs https
    template = (root / "site" / "template.html").read_text(encoding="utf-8")
    logo = f"https://raw.githubusercontent.com/{main_repo}/main/app-icon.png"
    now = utcnow()
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)

    pages: list[tuple[str, dict, dict[str, bytes]]] = []
    if from_dir:
        # A dry run's output, for previewing the site locally.
        for path in sorted(from_dir.glob(f"*/{st.STATE_ASSET}")):
            state = st.loads(path.read_bytes())
            if state:
                pages = [p for p in pages if p[0] != state["channel"]] + [(state["channel"], state, {})]
    else:
        gh = GitHub(discover_token())
        latest = gh.latest_release(signed_repo)
        if latest is None:
            raise Fatal(f"{signed_repo} has no release marked Latest, so there is nothing to put on index.html")
        pages.append(("stable",) + load_release(gh, signed_repo, latest, workdir, prefix))
        betas = [r for r in gh.releases(signed_repo) if r.get("prerelease") and not r.get("draft")]
        if betas:
            beta = max(betas, key=lambda r: r.get("created_at") or "")
            pages.append(("beta",) + load_release(gh, signed_repo, beta, workdir, prefix))

    for channel, state, legacy_manifests in pages:
        folder = out / channel
        folder.mkdir()
        app = state.get("app") or {}
        for entry in st.signed(state):
            plist = f"{prefix}-{entry['name']}.plist"
            if plist in legacy_manifests:
                (folder / plist).write_bytes(legacy_manifests[plist])
                continue
            url = f"https://github.com/{signed_repo}/releases/download/{state['tag']}/{prefix}-{entry['name']}.ipa"
            (folder / plist).write_text(ipa.manifest(url, entry.get("bundle_id", ""),
                                                     app.get("version", ""), app.get("title") or "SideInstaller"))
        if channel == "stable":
            title, tagline = app.get("title") or "SideInstaller", TAGLINE
            download = f"https://github.com/{main_repo}/releases/latest/download/{ipa_asset}"
        else:
            title, tagline = "SideInstaller — Beta", BETA_TAGLINE
            download = (state.get("unsigned_ipa") or {}).get("url") or \
                f"https://github.com/{main_repo}/releases/tag/{state['tag']}"
        page = "index.html" if channel == "stable" else "beta.html"
        (out / page).write_text(render(template, state, manifest_base=f"{site_url}/{channel}",
                                       page_title=title, tagline=tagline, download_url=download,
                                       logo_url=logo, prefix=prefix, now=now), encoding="utf-8")
        log(f"   {page}: {state['tag']}, {len(st.signed(state))} card(s)")

    if not any(channel == "beta" for channel, _, _ in pages):
        closed = root / "site" / "beta-closed.html"
        if closed.exists():
            shutil.copyfile(closed, out / "beta.html")
            log("   beta.html: no prerelease, so the closed-beta page")
    for name in STATIC_FILES:
        if (static_dir / name).exists():
            shutil.copyfile(static_dir / name, out / name)
        else:
            warn(f"{name} is missing from {static_dir}; the site goes out without it")
    log(f"   site assembled in {out}")
