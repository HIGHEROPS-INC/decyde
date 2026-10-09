#!/bin/sh
# Publish a release: scripts/release.sh 0.3.1 [commit]
#
# Before running: bump __version__ in decyde/__init__.py, add a "## [X.Y.Z] - date"
# section to CHANGELOG.md, commit, and push main. This script checks the two agree,
# tags the commit, and creates the GitHub release with that changelog section as notes.
# Installs and `decyde update` pick up the newest release, so nothing reaches users
# until this runs.
set -eu

VERSION="${1:?usage: scripts/release.sh X.Y.Z [commit]}"
TARGET="${2:-HEAD}"
TAG="v$VERSION"
cd "$(dirname "$0")/.."

die() { printf 'error: %s\n' "$1" >&2; exit 1; }

if [ "$TARGET" = HEAD ]; then
  [ -z "$(git status --porcelain)" ] || die "working tree is not clean"
  git fetch -q origin main
  [ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ] || die "HEAD is not origin/main; push first"
  CODE_VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' decyde/__init__.py)
  [ "$CODE_VERSION" = "$VERSION" ] || die "decyde/__init__.py says $CODE_VERSION, not $VERSION"
fi
git rev-parse -q --verify "refs/tags/$TAG" >/dev/null && die "$TAG already exists"

NOTES=$(awk -v v="$VERSION" '
  $0 ~ "^## \\[" v "\\]" { on = 1; next }
  on && /^## \[/ { exit }
  on && /^\[[0-9]/ { exit }
  on { print }' CHANGELOG.md)
[ -n "$(printf '%s' "$NOTES" | tr -d '[:space:]')" ] || die "no CHANGELOG.md section for $VERSION"

git tag -a "$TAG" -m "decyde $VERSION" "$TARGET"
git push -q origin "$TAG"
printf '%s\n' "$NOTES" | gh release create "$TAG" --title "decyde $VERSION" --notes-file - --verify-tag
echo "released $TAG"
