#!/bin/sh
# eggpool-connect bootstrap for macOS/Linux (reviewed static file).
#
# This script only selects, downloads, verifies, and executes the native
# `eggpool-connect` helper for one pinned EggPool release. It performs no
# client config mutation itself; the verified helper owns plan, backup,
# install, verification, and rollback.
#
# Safety properties (covered by tests/tooling/test_connect_release.py):
# - HTTPS only (`--proto '=https'`), no TLS bypass, redirects stay HTTPS.
# - The helper SHA-256 is verified against the release SHA256SUMS before
#   anything is executed; a missing entry or mismatch fails closed.
# - The profile token is passed to the helper as a data argument only and is
#   never evaluated, never printed, and never written to a file by this
# - A private temporary directory holds downloads and is removed on exit.
# - No global install, no repo clone, no cargo install fallback.
#
# Usage:
#   sh eggpool-connect.sh --version <X.Y.Z> --profile '<epc1...>' [--client codex|opencode] [--repo <owner/name>]
#
# The `--profile` value is a shareable secret-free connection token. The
# desktop still needs its own authorized EggPool key (EGGPOOL_API_KEY) when
# the helper runs; this script never accepts or forwards that key.
set -eu

VERSION=""
PROFILE=""
REPO="eggstack/eggpool"
CLIENT=""

usage() {
    echo "usage: eggpool-connect.sh --version <X.Y.Z> --profile '<epc1...>' [--client codex|opencode] [--repo <owner/name>]" >&2
}

fail() {
    echo "eggpool-connect bootstrap: $1" >&2
    exit 1
}

while [ $# -gt 0 ]; do
    case "$1" in
        --version)
            [ $# -ge 2 ] || { usage; exit 2; }
            VERSION="$2"
            shift 2
            ;;
        --profile)
            [ $# -ge 2 ] || { usage; exit 2; }
            PROFILE="$2"
            shift 2
            ;;
        --repo)
            [ $# -ge 2 ] || { usage; exit 2; }
            REPO="$2"
            shift 2
            ;;
        --client)
            [ $# -ge 2 ] || { usage; exit 2; }
            CLIENT="$2"
            shift 2
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            usage
            exit 2
            ;;
    esac
done

[ -n "$VERSION" ] || fail "missing --version (pinned EggPool release, e.g. 0.8.0)"
[ -n "$PROFILE" ] || fail "missing --profile (secret-free epc1 connection token)"
case "$PROFILE" in
    epc1.*) ;;
    *) fail "profile does not look like an epc1 connection token" ;;
esac
case "$VERSION" in
    *[!A-Za-z0-9._-]* | "" | .* | *..*)
        fail "version contains unsupported characters"
        ;;
esac
case "$REPO" in
    *[!A-Za-z0-9._/-]* | "" | *..*)
        fail "repo contains unsupported characters"
        ;;
esac
case "$CLIENT" in
    ""|codex|opencode) ;;
    *) fail "client must be codex or opencode" ;;
esac

OS_NAME=$(uname -s)
ARCH_NAME=$(uname -m)
case "$OS_NAME" in
    Linux) OS_CLASS="linux" ;;
    Darwin) OS_CLASS="macos" ;;
    *) fail "unsupported OS '$OS_NAME' (Windows desktops use eggpool-connect.ps1)" ;;
esac
case "$ARCH_NAME" in
    x86_64|amd64) ARCH_CLASS="x86_64" ;;
    arm64|aarch64) ARCH_CLASS="aarch64" ;;
    *) fail "unsupported architecture '$ARCH_NAME'" ;;
esac

ASSET="eggpool-connect-${VERSION}-${OS_CLASS}-${ARCH_CLASS}"
BASE="https://github.com/${REPO}/releases/download/v${VERSION}"

command -v curl >/dev/null 2>&1 || fail "curl is required"
if command -v sha256sum >/dev/null 2>&1; then
    HASH_CMD="sha256sum"
elif command -v shasum >/dev/null 2>&1; then
    HASH_CMD="shasum -a 256"
else
    fail "sha256sum or shasum is required"
fi

TMP_DIR=$(mktemp -d 2>/dev/null) || fail "cannot create a private temporary directory"
chmod 700 "$TMP_DIR"
cleanup() {
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT INT TERM

curl --fail --silent --show-error --location --proto '=https' \
    --output "$TMP_DIR/helper" "$BASE/$ASSET" \
    || fail "helper download failed ($ASSET)"
curl --fail --silent --show-error --location --proto '=https' \
    --output "$TMP_DIR/SHA256SUMS" "$BASE/SHA256SUMS" \
    || fail "checksum download failed (SHA256SUMS)"

EXPECTED=$(grep "${ASSET}\$" "$TMP_DIR/SHA256SUMS" | awk '{print $1}' | head -n 1)
[ -n "$EXPECTED" ] || fail "checksum entry is missing for $ASSET"
if [ ${#EXPECTED} -ne 64 ]; then
    fail "checksum entry is malformed for $ASSET"
fi
case "$EXPECTED" in
    *[!0-9a-f]*) fail "checksum entry is malformed for $ASSET" ;;
esac
ACTUAL=$($HASH_CMD "$TMP_DIR/helper" | awk '{print $1}')
[ "$EXPECTED" = "$ACTUAL" ] || fail "SHA-256 mismatch for $ASSET (refusing to execute)"

chmod +x "$TMP_DIR/helper"
if [ -n "$CLIENT" ]; then
    "$TMP_DIR/helper" install --profile "$PROFILE" --client "$CLIENT"
else
    "$TMP_DIR/helper" install --profile "$PROFILE"
fi
STATUS=$?
trap - EXIT INT TERM
cleanup
exit $STATUS
