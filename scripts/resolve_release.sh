#!/bin/bash
# Work out what a signing run signs: the main repository's release for TAG (its
# latest release when TAG is blank), whether that release is a beta, and which
# of its assets is the unsigned IPA. A release published by hand can go live
# before its IPA finishes uploading, so a missing asset is waited for.
#
#   TAG=v1.6.0 scripts/resolve_release.sh
#
# Config, all overridable by env:
#   TAG          release tag in MAIN_REPO; blank = MAIN_REPO's latest release
#   MAIN_REPO    owner/repo the unsigned IPA comes from (default FrizzleM/SideInstaller)
#   SIGNED_REPO  owner/repo the signed IPAs are published to (default this
#                repository under Actions, else FrizzleM/SideInstaller-Signed)
#   ASSET_WAIT   seconds to wait for an .ipa asset to show up (default 600)
#
# Prints these, and under Actions also writes them to $GITHUB_OUTPUT and
# $GITHUB_ENV for the steps after it:
#   TAG               the tag, same in both repositories
#   CHANNEL           stable | beta (a prerelease is a beta)
#   IPA_ASSET         name of the unsigned IPA asset (SideInstaller.ipa, normally)
#   UNSIGNED_IPA_URL  its download URL
#   MAKE_LATEST       true when this is MAIN_REPO's latest stable release, so
#                     the signed release becomes this repository's Latest too
#   OUTPUT_BASE_URL   where the signed IPAs will be downloaded from (the signed
#                     release's download URL), which the manifests point at
set -euo pipefail

MAIN_REPO="${MAIN_REPO:-FrizzleM/SideInstaller}"
SIGNED_REPO="${SIGNED_REPO:-${GITHUB_REPOSITORY:-FrizzleM/SideInstaller-Signed}}"
ASSET_WAIT="${ASSET_WAIT:-600}"
TAG="${TAG:-}"

die() { echo "::error::$*" >&2; exit 1; }

emit() {   # $1 name  $2 value
  printf '%s=%s\n' "$1" "$2"
  if [[ -n "${GITHUB_OUTPUT:-}" ]]; then printf '%s=%s\n' "$1" "$2" >> "$GITHUB_OUTPUT"; fi
  if [[ -n "${GITHUB_ENV:-}" ]]; then printf '%s=%s\n' "$1" "$2" >> "$GITHUB_ENV"; fi
}

# `gh release view` without a tag is the latest release: published, not a
# prerelease.
latest="$(gh release view --repo "$MAIN_REPO" --json tagName --jq .tagName 2>/dev/null || true)"
[[ -n "$TAG" ]] || TAG="$latest"
[[ -n "$TAG" ]] || die "$MAIN_REPO has no published release to sign"
# The tag ends up in file names, URLs and gh arguments; nothing else is let in.
[[ "$TAG" =~ ^[0-9A-Za-z][0-9A-Za-z._-]*$ ]] || die "Not a usable release tag: '$TAG'"

# Prefer SideInstaller.ipa; otherwise the first .ipa asset, which is what the
# old signing workflow took from a release.
ASSET_QUERY='
  [.assets[] | select(.state == "uploaded" and (.name | ascii_downcase | endswith(".ipa")))] as $ipas
  | ([$ipas[] | select(.name == "SideInstaller.ipa")] + $ipas) | first // empty
  | "\(.name)\t\(.url)"'

waited=0
while :; do
  info="$(gh release view "$TAG" --repo "$MAIN_REPO" --json isDraft,isPrerelease \
            --jq '"\(.isDraft)\t\(.isPrerelease)"' 2>/dev/null)" \
    || die "$MAIN_REPO has no release $TAG"
  IFS=$'\t' read -r is_draft is_prerelease <<< "$info"
  [[ "$is_draft" == "false" ]] || die "$MAIN_REPO's $TAG is still a draft"

  asset="$(gh release view "$TAG" --repo "$MAIN_REPO" --json assets --jq "$ASSET_QUERY")"
  [[ -n "$asset" ]] && break
  if (( waited >= ASSET_WAIT )); then
    die "$MAIN_REPO's $TAG has no .ipa asset (waited ${ASSET_WAIT}s)"
  fi
  echo "No .ipa on $MAIN_REPO $TAG yet; checking again in 30s" >&2
  sleep 30
  waited=$((waited + 30))
done
IFS=$'\t' read -r ipa_asset ipa_url <<< "$asset"

channel=stable
[[ "$is_prerelease" == "true" ]] && channel=beta
make_latest=false
[[ "$channel" == "stable" && "$TAG" == "$latest" ]] && make_latest=true

emit TAG "$TAG"
emit CHANNEL "$channel"
emit IPA_ASSET "$ipa_asset"
emit UNSIGNED_IPA_URL "$ipa_url"
emit MAKE_LATEST "$make_latest"
emit OUTPUT_BASE_URL "https://github.com/$SIGNED_REPO/releases/download/$TAG"
