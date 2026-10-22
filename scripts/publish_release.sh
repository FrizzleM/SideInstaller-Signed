#!/bin/bash
# Publish one signing run as a release of this repository, under the same tag
# as the main repository's release it was signed from. Attached: one signed IPA
# and one OTA manifest per certificate, plus the run's metadata
# (certificate-validity.tsv, app-info.tsv, sign-state.tsv), which the install
# page is built from and the next run's no-change check compares against.
#
# When the release already exists (a re-sign of the same tag), its assets are
# replaced (--clobber) and its notes and flags refreshed. Assets of certificates
# that have left the pool since stay on it, as their IPAs used to stay in
# output/ between runs.
#
# Config, all overridable by env (resolve_release.sh sets the first five):
#   TAG, CHANNEL, MAKE_LATEST, UNSIGNED_IPA_URL, OUTPUT_BASE_URL
#   OUTPUT_DIR     signed IPAs and metadata (default: ./output)
#   OUTPUT_PREFIX  filename prefix of the signed IPAs (default: sideinstaller)
#   MAIN_REPO      default FrizzleM/SideInstaller
#   SIGNED_REPO    default this repository under Actions, else FrizzleM/SideInstaller-Signed
#   SITE_URL       the install page, for the notes (default https://sideinstaller.net)
#   DRY_RUN        1 = print the gh commands instead of running them
set -euo pipefail

TAG="${TAG:?TAG is required}"
CHANNEL="${CHANNEL:-stable}"
MAKE_LATEST="${MAKE_LATEST:-false}"
UNSIGNED_IPA_URL="${UNSIGNED_IPA_URL:-}"
OUTPUT_DIR="${OUTPUT_DIR:-output}"
OUTPUT_PREFIX="${OUTPUT_PREFIX:-sideinstaller}"
MAIN_REPO="${MAIN_REPO:-FrizzleM/SideInstaller}"
SIGNED_REPO="${SIGNED_REPO:-${GITHUB_REPOSITORY:-FrizzleM/SideInstaller-Signed}}"
SITE_URL="${SITE_URL:-https://sideinstaller.net}"
DRY_RUN="${DRY_RUN:-0}"

die() { echo "::error::$*" >&2; exit 1; }
run() {
  if [[ "$DRY_RUN" == "1" ]]; then printf '[dry run]'; printf ' %q' "$@"; printf '\n'; else "$@"; fi
}

shopt -s nullglob
ipas=("$OUTPUT_DIR/$OUTPUT_PREFIX"-*.ipa)
manifests=("$OUTPUT_DIR/$OUTPUT_PREFIX"-*.plist)
shopt -u nullglob
[[ ${#ipas[@]} -gt 0 ]] || die "No signed IPAs in $OUTPUT_DIR/ to publish"

files=("${ipas[@]}")
[[ ${#manifests[@]} -gt 0 ]] && files+=("${manifests[@]}")
for f in certificate-validity.tsv app-info.tsv sign-state.tsv; do
  [[ -f "$OUTPUT_DIR/$f" ]] && files+=("$OUTPUT_DIR/$f")
done

# ----- notes ------------------------------------------------------------------
version=""
if [[ -f "$OUTPUT_DIR/app-info.tsv" ]]; then
  version="$(awk -F'\t' '$1 == "version" { print $2; exit }' "$OUTPUT_DIR/app-info.tsv")"
fi
page="$SITE_URL/"
[[ "$CHANNEL" == "beta" ]] && page="$SITE_URL/beta.html"

notes="$(mktemp)"
trap 'rm -f "$notes"' EXIT
{
  echo "SideInstaller ${version:-$TAG}, signed once per certificate from"
  if [[ -n "$UNSIGNED_IPA_URL" ]]; then
    echo "[the unsigned IPA]($UNSIGNED_IPA_URL) of [$MAIN_REPO $TAG](https://github.com/$MAIN_REPO/releases/tag/$TAG)."
  else
    echo "[$MAIN_REPO $TAG](https://github.com/$MAIN_REPO/releases/tag/$TAG)."
  fi
  echo
  shown="${page#https://}"
  echo "**Install from [${shown%/}]($page)**, with a certificate marked Valid."
  echo
  if [[ -f "$OUTPUT_DIR/certificate-validity.tsv" ]]; then
    echo "| Certificate | Expires | Apple says |"
    echo "| :-- | :-- | :-- |"
    awk -F'\t' 'NR > 1 && $1 != "" {
      status = ($4 == "valid") ? "Valid" : ($4 == "revoked") ? "Revoked" : "Not checked"
      printf "| %s | %s | %s |\n", $1, $2, status
    }' "$OUTPUT_DIR/certificate-validity.tsv"
    echo
  fi
  if [[ "$CHANNEL" == "stable" ]]; then
    echo "Fixed link to the newest stable build for one certificate:"
    echo "\`https://github.com/$SIGNED_REPO/releases/latest/download/$OUTPUT_PREFIX-<certificate>.ipa\`"
  else
    echo "Beta build (prerelease): not on the public install page."
  fi
} > "$notes"

# ----- flags --------------------------------------------------------------------
if [[ "$CHANNEL" == "beta" ]]; then
  flags=(--prerelease --latest=false)
elif [[ "$MAKE_LATEST" == "true" ]]; then
  flags=(--prerelease=false --latest)
else
  # Re-signing an older stable tag must not take Latest from the newest one.
  flags=(--prerelease=false --latest=false)
fi

# ----- publish ------------------------------------------------------------------
echo "Publishing ${#ipas[@]} signed IPA(s) as $SIGNED_REPO $TAG ($CHANNEL)"
if gh release view "$TAG" --repo "$SIGNED_REPO" >/dev/null 2>&1; then
  echo "$TAG already exists: replacing its assets and refreshing its notes"
  run gh release upload "$TAG" "${files[@]}" --repo "$SIGNED_REPO" --clobber
  run gh release edit "$TAG" --repo "$SIGNED_REPO" --title "$TAG" --notes-file "$notes" "${flags[@]}"
else
  # The tag goes on the commit that signed it, so the certificates and scripts
  # behind a release can be looked up later.
  target=()
  [[ -n "${GITHUB_SHA:-}" ]] && target=(--target "$GITHUB_SHA")
  run gh release create "$TAG" "${files[@]}" --repo "$SIGNED_REPO" --title "$TAG" \
    --notes-file "$notes" ${target[@]+"${target[@]}"} "${flags[@]}"
fi

if [[ -n "${GITHUB_STEP_SUMMARY:-}" && "$DRY_RUN" != "1" ]]; then
  {
    echo "### Published $SIGNED_REPO $TAG ($CHANNEL)"
    echo
    echo "${#ipas[@]} signed IPA(s): https://github.com/$SIGNED_REPO/releases/tag/$TAG"
  } >> "$GITHUB_STEP_SUMMARY"
fi
