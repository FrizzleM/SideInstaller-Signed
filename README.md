# SideInstaller-Signed

**This is the official source of signed SideInstaller builds.** It signs every
release of [FrizzleM/SideInstaller](https://github.com/FrizzleM/SideInstaller)
with each certificate in its pool, publishes the signed IPAs as
[releases of this repository](https://github.com/FrizzleM/SideInstaller-Signed/releases)
and deploys the install page, **[sideinstaller.net](https://sideinstaller.net/)**.

The app's source code, issues and unsigned releases are in
[FrizzleM/SideInstaller](https://github.com/FrizzleM/SideInstaller). Signed
copies from anywhere other than this repository or sideinstaller.net are not
official and may have been changed to steal the Apple ID typed into them.

## Installing

Open [sideinstaller.net](https://sideinstaller.net/) on the iPhone and tap
**Install** on a certificate marked **Valid**. Each card installs the IPA signed
with that certificate, straight from the release here.

Fixed links to the newest stable build, one per certificate:

```
https://github.com/FrizzleM/SideInstaller-Signed/releases/latest/download/sideinstaller-<certificate>.ipa
```

The certificate names are listed in each release's notes.

## How it works

Everything is one command, `python3 -m signer sync`, run by the
[`sign`](.github/workflows/sign.yml) workflow. Each run works out what is out of
date and does only that:

- **A new release** of FrizzleM/SideInstaller (its `notify-signer` workflow
  sends this repository a `repository_dispatch` of type `unsigned-release` with
  `{"tag": "<tag>"}`) is signed with every certificate and published as release
  `<tag>` here, one IPA and one OTA manifest per certificate. A prerelease there
  is a prerelease here. If a dispatch never arrives, the daily run notices the
  unsigned release and signs it anyway.
- **Every day** each certificate on the live releases (the latest, and the
  newest prerelease) is checked with Apple's OCSP responder. A certificate
  that Apple revoked, or that expired, or that came back, has its status
  changed on the page and in the release notes, without re-signing anything.
- **A certificate new to the pool**, or one whose p12, profile or password
  changed, is signed and added to the live releases. Nothing else is
  re-signed. A certificate that left the pool is removed from them.

The daily check runs on Linux and takes seconds. Only when there is something
to sign does a macOS job start, and it signs just that. All the certificates
it needs go into one temporary keychain, and the copies are signed in parallel
from one unpacked IPA. A full release takes a few seconds of signing. The page
is redeployed only when something on it changed.

[`deploy-pages`](.github/workflows/deploy-pages.yml) builds `index.html` from
the latest stable release and `beta.html` from the newest prerelease, and
deploys them to GitHub Pages at sideinstaller.net.

No IPA is ever committed, here or in the main repository.

### What each release holds

Next to the IPAs and manifests, each release has a `state.json`: what every IPA
was signed from (a hash of its p12, profile and password, and the unsigned
IPA's digest), what Apple last said about its certificate, and what the page
shows. That is how a run tells a new certificate from one that is already
done, and how a status change is noticed. The page is built from it.

A card's expiry date is the earlier of the certificate's and the provisioning
profile's, since the app stops launching at whichever comes first. The page
recounts the days left in the browser, so the countdown stays right between
rebuilds.

A certificate that cannot be signed (wrong password, a profile issued for a
different certificate, a codesign error) is listed under "Not signed" in the
release notes. One that fails at codesign is not retried until something about
it changes, so it never starts a Mac every day. If every certificate fails at
once, the run publishes nothing and the release stays as it was.

### The certificate pool

Every source listed in [`cert-url.txt`](cert-url.txt) is fetched in parallel and
merged into one pool: this repository's [`certs/`](certs/README.md) folder
first, then the public pools. When two certificates would get the same name,
the first source wins, so your own copy in `certs/` overrides a public one.
Adding a certificate to `certs/` (or editing `cert-url.txt`) starts a run that
signs it and puts it on the page. Deleting one removes it.

A source that fails to answer, or answers with nothing, never removes
anything: its certificates stay on the page, still checked with Apple, until it
answers again.

### Running it by hand

Actions › **sign** › Run workflow:

- blank tag: the live releases, same as the daily run;
- a tag (e.g. `v1.5.0`): that release only, created here if it does not exist;
- **force**: re-sign every certificate rather than only new and changed ones;
- certificate sources: use these for one run instead of `cert-url.txt`
  (certificates from other sources are left alone).

Actions › **deploy-pages** › Run workflow rebuilds the page from the releases
without signing anything.

### Running it locally

From the repository root, with Python 3.9+ and a `gh` login (or `GH_TOKEN`):

```bash
python3 -m signer sync --check-only --dry-run
```

`--dry-run` changes nothing on GitHub; it writes the `state.json` and release
notes it would publish under the work folder (`--workdir`, default
`$TMPDIR/signer`). Without `--check-only` it also signs, which needs macOS. The
keychain it creates is temporary and the keychain search list is not touched.
To preview the page from a dry run:

```bash
python3 -m signer site --from-dir "${TMPDIR%/}/signer/dry-run" --static-dir path/to/SideInstaller
```

Settings, all by environment variable: `MAIN_REPO`, `SIGNED_REPO`,
`OUTPUT_PREFIX` (`sideinstaller`), `SITE_URL`, `CERT_ZIP_URL` (one-off sources),
`NEXCERTS_STATUS` (`all`, `signed`, `revoked` or `missingP12`), `P12_PASSWORD`
(fallback password), `FORCED_BUNDLE_ID` (for wildcard profiles), `ASSET_WAIT`
(seconds to wait for a release's IPA to finish uploading, default 600).

To re-sign everything after changing how apps are signed, bump
`SIGNING_REVISION` in [`signer/state.py`](signer/state.py).

## Setup

- Settings › Pages › Source: **GitHub Actions**; custom domain: `sideinstaller.net`.
- No secrets are needed here: the workflows use the repository's own
  `GITHUB_TOKEN`, and the main repository is public.
- The GitHub App that FrizzleM/SideInstaller dispatches with is installed on
  this repository with **Contents: Read and write**.
- GitHub pauses scheduled workflows in a public repository after 60 days
  without activity. If the daily check stops, re-enable it under Actions › sign.
