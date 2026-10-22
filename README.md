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

1. A release of FrizzleM/SideInstaller carries the **unsigned** `SideInstaller.ipa`.
   Its `notify-signer` workflow sends this repository a `repository_dispatch`
   (`unsigned-release`, `{"tag": "<tag>"}`). That is the only call between the two.
2. [`sign`](.github/workflows/sign.yml) downloads that IPA with
   `gh release download`, signs it once per certificate with
   [`scripts/sign_with_all_certs.sh`](scripts/sign_with_all_certs.sh)
   (`codesign` with a temporary keychain per certificate), writes one OTA
   manifest per IPA and publishes them all as release `<tag>` here. A
   prerelease there is a prerelease here.
3. [`deploy-pages`](.github/workflows/deploy-pages.yml) builds `index.html`
   from the latest stable release and `beta.html` from the newest prerelease,
   and deploys them to GitHub Pages at sideinstaller.net.

No IPA is ever committed, here or in the main repository.

### The certificate pool

Every source listed in [`cert-url.txt`](cert-url.txt) is merged into one pool:
this repository's [`certs/`](certs/README.md) folder first, then the public
pools. Adding a certificate to `certs/` (or editing `cert-url.txt`) re-signs the
main repository's latest release.

### Re-signing by hand

Actions › **sign** › Run workflow, with the tag to sign (blank = the main
repository's latest release). The release here is updated in place: its IPAs
are replaced and its notes refreshed.

Actions › **deploy-pages** › Run workflow rebuilds the page from the releases
without signing anything.

## Setup

- Settings › Pages › Source: **GitHub Actions**; custom domain: `sideinstaller.net`.
- No secrets are needed here: the workflows use the repository's own
  `GITHUB_TOKEN`, and the main repository is public.
- The GitHub App that FrizzleM/SideInstaller dispatches with is installed on
  this repository with **Contents: Read and write**.
