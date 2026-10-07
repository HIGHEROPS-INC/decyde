#!/bin/sh
# decyde installer: curl -fsSL https://decyde.dev/install | sh
# Re-run it any time to upgrade. Options after `sh -s --` go to `decyde setup`,
# e.g. `curl -fsSL https://decyde.dev/install | sh -s -- --name Sam --no-hooks`.
set -eu

REPO="${DECYDE_REPO:-HIGHEROPS-INC/decyde}"
REF="${DECYDE_REF:-main}"
DEST="${DECYDE_DIR:-$HOME/.local/share/decyde}"

say() { printf '\033[38;5;141m%s\033[0m %s\n' "decyde" "$1"; }
die() { printf '\033[31merror:\033[0m %s\n' "$1" >&2; exit 1; }

command -v curl >/dev/null || die "curl is required"
command -v tar >/dev/null || die "tar is required"
PY="$(command -v python3 || true)"
[ -n "$PY" ] || die "python3 (3.10 or newer) is required"
"$PY" -c 'import sys; sys.exit(sys.version_info < (3, 10))' || die "python3 is $("$PY" -V 2>&1 | cut -d' ' -f2); decyde needs 3.10 or newer"

say "downloading $REPO@$REF"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
curl -fsSL "https://codeload.github.com/$REPO/tar.gz/$REF" | tar -xz -C "$TMP" --strip-components=1
[ -f "$TMP/bin/decyde" ] || die "download did not contain bin/decyde"

mkdir -p "$(dirname "$DEST")"
rm -rf "$DEST.old"
[ -d "$DEST" ] && mv "$DEST" "$DEST.old"
mv "$TMP" "$DEST"
trap - EXIT
rm -rf "$DEST.old"
chmod +x "$DEST/bin/decyde"
say "installed to $DEST"

# Setup asks for your name, so give it the terminal even when this script is piped into sh.
if [ -r /dev/tty ] && [ -t 1 ]; then
  "$PY" "$DEST/bin/decyde" setup "$@" </dev/tty
else
  "$PY" "$DEST/bin/decyde" setup "$@"
fi
