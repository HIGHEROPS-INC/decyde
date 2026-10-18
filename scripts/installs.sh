#!/bin/sh
# Downloads of each release's tarball: fresh installs and `decyde update` runs together.
# Releases before 0.5.2 had no tarball, so they show nothing.
gh api --paginate repos/HIGHEROPS-INC/decyde/releases --jq \
  '.[] | "\(.tag_name)\t\([.assets[] | select(.name | endswith(".tar.gz")) | .download_count] | add // 0)"' |
  awk -F'\t' '{ print; total += $2 } END { print "total\t" total }'
