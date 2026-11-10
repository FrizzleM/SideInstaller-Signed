"""The site (sideinstaller.net), rebuilt from the releases.

  index.html    the overview: what SideInstaller is and how it works
  install.html  the latest stable release, one row per signed certificate
  beta.html     the newest prerelease, or site/beta-closed.html without one
  help.html     tutorials, questions, the community and donations
  assets/       the stylesheet, the script, the icons and the images
  stable/       install.html's OTA manifests, each pointing at an IPA on the release
  beta/         beta.html's
  terms.html, open.html, the icons and the DNS profile, from the main repository

Every page is a file in site/ with {{TOKENS}} in it. {{HEAD}}, {{NAV}} and
{{FOOTER}} are the shared pieces in site/partials/; the rest come from the
release's state.

Rows are ordered so the certificates that work come first: valid, then never
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
# Pages that say the same thing whatever the release holds.
PLAIN_PAGES = ("index.html", "help.html")
PARTIALS = ("head", "nav", "footer")
HEADLINE = "Install SideInstaller"
TAGLINE = "Three steps in Safari, on the iPhone or iPad you want to set up."
BETA_HEADLINE = "Install the beta"
BETA_TAGLINE = ("A beta test build, not the public release. Three steps in Safari, "
                "on the iPhone or iPad you want to set up.")
# The navigation's button, left off the pages it would only point back to.
NAV_CTA = '<a class="btn btn-sm btn-primary" href="install.html">Install</a>'
# Points at https://sideinstaller.net/install.html, so only that page shows it.
HANDOFF_QR = ('<div class="qr"><img src="assets/qr-install.svg" width="112" height="112" '
              'alt="QR code for the install page"></div>')


def _esc(value: str) -> str:
    return html.escape(value, quote=True)


def countdown(days: int | None) -> tuple[str, str]:
    """(class, label) of the days-left text; the page's script redraws it."""
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
    """One certificate's row. Only a certificate Apple still honours is marked;
    assets/site.js reads the data attributes to sort, search and recount."""
    expiry = st.effective_expiry(entry)
    days = st.days_left(expiry, now)
    status = entry.get("status", "unknown")
    name = _esc(entry.get("display_name") or entry["name"])
    cls, label = countdown(days)
    meta = []
    if status == "valid":
        meta.append('<span class="status"><i class="dot good"></i>Valid</span>')
    if expiry:
        meta.append(f"<span>Expires {expiry.date().isoformat()}</span>")
    if cls != "unknown":
        meta.append(f'<span class="days {cls}">{label}</span>')
    sep = '<span class="sep">·</span>'
    meta_line = f'<p class="cert-meta">{sep.join(meta)}</p>' if meta else ""
    expires_attr = f' data-expires="{iso(expiry)}"' if expiry else ""
    return (f'            <li class="cert" data-name="{name}" data-days="{days if days is not None else -999999}"'
            f'{expires_attr} data-rank="{st.rank(entry)}" data-status="{status}">\n'
            f'              <div class="cert-main">\n'
            f'                <h3 class="cert-name">{name}</h3>\n'
            f'                {meta_line}\n'
            f'              </div>\n'
            f'              <a class="btn btn-sm btn-secondary" href="{_esc(install_url)}">Install</a>\n'
            f'            </li>\n')


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def install_tokens(state: dict, *, manifest_base: str, page_title: str, headline: str, tagline: str,
                   download_url: str, page_url: str, qr: str, prefix: str, now: datetime) -> dict[str, str]:
    """What an install page (install.html, beta.html) shows of its release."""
    entries = st.sort_for_display(st.signed(state), now)
    cards = "".join(card(e, f"itms-services://?action=download-manifest&url={manifest_base}/"
                            f"{prefix}-{e['name']}.plist", now) for e in entries)
    if not cards:
        cards = '            <li class="certs-none">No signed builds are available yet. Check back soon.</li>\n'
    valid = sum(1 for e in entries if e.get("status") == "valid")
    paris = now.astimezone(_paris())
    return {
        "{{PAGE_TITLE}}": _esc(page_title),
        "{{HEADLINE}}": _esc(headline),
        "{{APP_TAGLINE}}": _esc(tagline),
        "{{LAST_UPDATED}}": paris.strftime("%d %b %Y, %H:%M CET"),
        "{{LATEST_RELEASE_URL}}": _esc(download_url),
        "{{REPO_NOTE}}": (f"Built automatically · signed with {_count(len(entries), 'certificate')}, "
                          f"{valid} currently valid"),
        "{{PAGE_URL}}": _esc(page_url),
        "{{HANDOFF_QR}}": qr,
        "{{CARDS}}": cards.strip(),
    }


def compose(template: str, parts: dict[str, str], tokens: dict[str, str], *,
            current: str = "", cta: bool = True) -> str:
    """A page from its template: the shared head, navigation and footer first,
    then the tokens. `current` is the navigation link to mark as this page."""
    nav = parts["nav"].replace("{{NAV_CTA}}", NAV_CTA if cta else "")
    if current:
        nav = nav.replace(f'<a href="{current}"', f'<a href="{current}" aria-current="page"')
    page = template
    for token, value in (("{{HEAD}}", parts["head"]), ("{{NAV}}", nav), ("{{FOOTER}}", parts["footer"])):
        page = page.replace(token, value.rstrip("\n"))
    for token, value in tokens.items():
        page = page.replace(token, value)
    left = page.find("{{")
    if left != -1:
        raise Fatal(f"a page still has an unfilled token: {page[left:left + 40]!r}")
    return page


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
    host = site_url.split("://", 1)[-1]
    site = root / "site"
    parts = {name: (site / "partials" / f"{name}.html").read_text(encoding="utf-8") for name in PARTIALS}
    install_page = (site / "install.html").read_text(encoding="utf-8")
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
            raise Fatal(f"{signed_repo} has no release marked Latest, so there is nothing to put on install.html")
        pages.append(("stable",) + load_release(gh, signed_repo, latest, workdir, prefix))
        betas = [r for r in gh.releases(signed_repo) if r.get("prerelease") and not r.get("draft")]
        if betas:
            beta = max(betas, key=lambda r: r.get("created_at") or "")
            pages.append(("beta",) + load_release(gh, signed_repo, beta, workdir, prefix))

    # The version every page's footer shows is the stable release's.
    stable = next((state for channel, state, _ in pages if channel == "stable"), None)
    shared = {"{{APP_VERSION}}": _esc(((stable or {}).get("app") or {}).get("version") or "—")}

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
            page, title, headline, tagline, qr = "install.html", HEADLINE, HEADLINE, TAGLINE, HANDOFF_QR
            download = f"https://github.com/{main_repo}/releases/latest/download/{ipa_asset}"
        else:
            page, title, headline, tagline, qr = "beta.html", "SideInstaller beta", BETA_HEADLINE, BETA_TAGLINE, ""
            download = (state.get("unsigned_ipa") or {}).get("url") or \
                f"https://github.com/{main_repo}/releases/tag/{state['tag']}"
        tokens = install_tokens(state, manifest_base=f"{site_url}/{channel}", page_title=title,
                                headline=headline, tagline=tagline, download_url=download,
                                page_url=f"{host}/{page}", qr=qr, prefix=prefix, now=now)
        # The footer names the stable version; the page's own line names this release's.
        tokens = {**shared, "{{PAGE_VERSION}}": _esc(app.get("version") or "—"), **tokens}
        # An install page's own navigation link stands in for the Install button.
        (out / page).write_text(compose(install_page, parts, tokens, current="install.html" if channel == "stable" else "",
                                        cta=False), encoding="utf-8")
        log(f"   {page}: {state['tag']}, {len(st.signed(state))} row(s)")

    for page in PLAIN_PAGES:
        template = (site / page).read_text(encoding="utf-8")
        (out / page).write_text(compose(template, parts, shared, current=page), encoding="utf-8")
    if not any(channel == "beta" for channel, _, _ in pages):
        closed = site / "beta-closed.html"
        if closed.exists():
            (out / "beta.html").write_text(compose(closed.read_text(encoding="utf-8"), parts, shared),
                                           encoding="utf-8")
            log("   beta.html: no prerelease, so the closed-beta page")
    shutil.copytree(site / "assets", out / "assets")
    for name in STATIC_FILES:
        if (static_dir / name).exists():
            shutil.copyfile(static_dir / name, out / name)
        else:
            warn(f"{name} is missing from {static_dir}; the site goes out without it")
    log(f"   site assembled in {out}")
