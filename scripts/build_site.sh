#!/bin/bash
# Assemble the install site (sideinstaller.net) from this repository's
# releases. Nothing is signed here: each page is rebuilt from what the last
# signing run attached to its release.
#
#   index.html   the latest stable release (the one marked Latest)
#   beta.html    the newest prerelease, when there is one; otherwise the
#                closed-beta notice in site/beta-closed.html
#   stable/      index.html's OTA manifests, each pointing at a signed IPA on
#                the release
#   beta/        beta.html's
#   terms.html, open.html, the icons and the DNS profile, from the main
#   repository (STATIC_DIR)
#
# Config, all overridable by env:
#   SITE_URL       the site's base URL as it is served, e.g. https://sideinstaller.net
#                  (required; the install links are absolute)
#   SITE_DIR       where the site is assembled (default: ./_site)
#   STATIC_DIR     checkout of MAIN_REPO with the static files (default: ./main-repo)
#   SIGNED_REPO    default this repository under Actions, else FrizzleM/SideInstaller-Signed
#   MAIN_REPO      default FrizzleM/SideInstaller
#   OUTPUT_PREFIX  filename prefix of the signed IPAs (default: sideinstaller)
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT_DIR="$ROOT_DIR/scripts"
SITE_URL="${SITE_URL:?SITE_URL is required, e.g. https://sideinstaller.net}"
SITE_URL="${SITE_URL%/}"
# itms-services only takes https:// manifest URLs. Pages reports a custom
# domain as http:// until HTTPS is enforced, though it serves https anyway.
SITE_URL="https://${SITE_URL#*://}"
SITE_DIR="${SITE_DIR:-$ROOT_DIR/_site}"
STATIC_DIR="${STATIC_DIR:-$ROOT_DIR/main-repo}"
SIGNED_REPO="${SIGNED_REPO:-${GITHUB_REPOSITORY:-FrizzleM/SideInstaller-Signed}}"
MAIN_REPO="${MAIN_REPO:-FrizzleM/SideInstaller}"
OUTPUT_PREFIX="${OUTPUT_PREFIX:-sideinstaller}"

die() { echo "::error::$*" >&2; exit 1; }

case "$SITE_DIR" in /*) ;; *) SITE_DIR="$PWD/$SITE_DIR" ;; esac
case "$STATIC_DIR" in /*) ;; *) STATIC_DIR="$PWD/$STATIC_DIR" ;; esac

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
rm -rf "$SITE_DIR"
mkdir -p "$SITE_DIR"

stable_tag="$(gh api "repos/$SIGNED_REPO/releases/latest" --jq .tag_name 2>/dev/null || true)"
[[ -n "$stable_tag" ]] || die "$SIGNED_REPO has no release marked Latest, so there is nothing to put on index.html"
beta_tag="$(gh release list --repo "$SIGNED_REPO" --limit 100 --json tagName,isPrerelease,isDraft,createdAt \
  --jq '[.[] | select(.isPrerelease and (.isDraft | not))] | sort_by(.createdAt) | last | .tagName // empty')"

# The page for one channel: its manifests copied into the site, and the page
# generated from the release's metadata with links to those manifests.
build_page() {   # $1 channel  $2 tag  $3 page file name
  local channel="$1" tag="$2" html="$3" dir="$WORK/$1" unsigned_url=""
  mkdir -p "$dir" "$SITE_DIR/$channel"
  gh release download "$tag" --repo "$SIGNED_REPO" --dir "$dir" \
    --pattern "$OUTPUT_PREFIX-*.plist" --pattern '*.tsv'

  shopt -s nullglob
  local manifests=("$dir/$OUTPUT_PREFIX"-*.plist)
  shopt -u nullglob
  [[ ${#manifests[@]} -gt 0 ]] || die "$SIGNED_REPO $tag has no $OUTPUT_PREFIX-*.plist manifests"
  cp "${manifests[@]}" "$SITE_DIR/$channel/"

  local extra=()
  if [[ "$channel" == "beta" ]]; then
    # The beta page's Download IPA button hands out the build its cards were
    # signed from, as it did before.
    if [[ -f "$dir/sign-state.tsv" ]]; then
      unsigned_url="$(awk -F'\t' '$1 == "ipa_url" { print $2; exit }' "$dir/sign-state.tsv")"
    fi
    [[ -n "$unsigned_url" ]] || unsigned_url="https://github.com/$MAIN_REPO/releases/tag/$tag"
    extra=(
      "PAGE_TITLE=SideInstaller — Beta"
      "APP_TAGLINE=Beta test build, not the public release. Follow the three steps below to get set up."
      "LATEST_RELEASE_URL=$unsigned_url"
    )
  fi

  echo "[*] $html: $SIGNED_REPO $tag (${#manifests[@]} manifest(s), served from $SITE_URL/$channel)"
  # generate_index.sh falls back on UNSIGNED_IPA_URL for its Download IPA
  # button; drop any stray one so the stable page links the latest release.
  env -u UNSIGNED_IPA_URL \
    OUTPUT_DIR="$dir" \
    OUTPUT_PREFIX="$OUTPUT_PREFIX" \
    OUTPUT_HTML="$SITE_DIR/$html" \
    OUTPUT_BASE_URL="$SITE_URL/$channel" \
    GITHUB_USER="${MAIN_REPO%%/*}" \
    GITHUB_REPO="${MAIN_REPO#*/}" \
    LOGO_URL="https://raw.githubusercontent.com/$MAIN_REPO/main/app-icon.png" \
    ${extra[@]+"${extra[@]}"} \
    "$SCRIPT_DIR/generate_index.sh"
}

build_page stable "$stable_tag" index.html
if [[ -n "$beta_tag" ]]; then
  build_page beta "$beta_tag" beta.html
elif [[ -f "$ROOT_DIR/site/beta-closed.html" ]]; then
  # No beta running: beta.html stays the "The beta is over" page it has been
  # since the v1 release.
  cp "$ROOT_DIR/site/beta-closed.html" "$SITE_DIR/beta.html"
  echo "[*] beta.html: no prerelease, so the closed-beta page (site/beta-closed.html)"
else
  echo "[*] No prerelease, so no beta.html"
fi

# Static pages and icons the pages link to, kept in the main repository.
for f in favicon-32.png favicon-192.png app-icon.png SideInstallerDNS.mobileconfig terms.html open.html; do
  if [[ -f "$STATIC_DIR/$f" ]]; then
    cp "$STATIC_DIR/$f" "$SITE_DIR/$f"
  else
    echo "[WARN] $f is missing from $STATIC_DIR; the site goes out without it" >&2
  fi
done

echo "[✓] Site assembled in $SITE_DIR"
find "$SITE_DIR" -maxdepth 1 -type f -exec basename {} \; | LC_ALL=C sort | sed 's/^/      /'
