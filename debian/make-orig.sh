#!/bin/bash
# make-orig.sh OUTDIR
#
# Write the upstream tarball this package builds from. It is the committed
# tree plus the two things a network-less build cannot fetch:
#   vendor/   the Go modules, from "go mod vendor"
#   fonts/    the symbols font, fetched by pinned SHA-256
# This step needs the network and Go; the package build that follows does not.
set -euo pipefail
umask 022
HERE="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$(readlink -f "$1")"
version="$(dpkg-parsechangelog -l "$HERE/debian/changelog" -S Version)"
upstream="${version%-*}"
epoch="$(git -C "$HERE" log -1 --format=%ct)"
FONT_URL=https://github.com/ryanoasis/nerd-fonts/releases/download/v3.4.0/NerdFontsSymbolsOnly.tar.xz
FONT_ARCHIVE_SHA256=7f8c090da3b0eaa7108646bf34cbbb6ed13d5358a72460522108b06c7ecd716a
FONT_SHA256=f0f624d9b474bea1662cf7e862d44aebe1ae1f6c7f9cb7a0ca5d0e5ac9561c60

work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
tree="$work/kilix-engine-$upstream"
mkdir "$tree"
git -C "$HERE" archive HEAD -- . ':!debian' | tar -C "$tree" -xf -
(
    cd "$tree"
    export PATH="/usr/lib/go-1.27/bin:$PATH" GOTOOLCHAIN=local GOFLAGS=-mod=mod
    export GOPATH="$work/gopath" GOCACHE="$work/gocache" HOME="$work/home"
    go mod vendor
    curl -fsSL -o "$work/font.tar.xz" "$FONT_URL"
    echo "$FONT_ARCHIVE_SHA256  $work/font.tar.xz" | sha256sum -c - >/dev/null
    mkdir -p fonts
    tar -xf "$work/font.tar.xz" -C fonts SymbolsNerdFontMono-Regular.ttf
    echo "$FONT_SHA256  fonts/SymbolsNerdFontMono-Regular.ttf" | sha256sum -c - >/dev/null
)
tar -C "$work" --sort=name --mtime="@$epoch" --owner=0 --group=0 --numeric-owner \
    -cf - "kilix-engine-$upstream" | xz -6 -T1 > "$OUT/kilix-engine_$upstream.orig.tar.xz"
sha256sum "$OUT/kilix-engine_$upstream.orig.tar.xz"
