#!/usr/bin/env bash
# termshop installer -- picks the best available Python tool installer.
set -euo pipefail
REPO="git+https://github.com/21prnv/termshop.git"

if command -v uv >/dev/null 2>&1; then
    uv tool install --force "$REPO"
elif command -v pipx >/dev/null 2>&1; then
    pipx install --force "$REPO"
elif command -v pip3 >/dev/null 2>&1; then
    pip3 install --user "$REPO"
else
    echo "error: need one of uv, pipx, or pip3 on PATH" >&2
    echo "  macOS:  brew install uv        (or: brew install pipx)" >&2
    echo "  Linux:  see https://docs.astral.sh/uv/  or  apt install pipx" >&2
    exit 1
fi

echo
echo "termshop installed. Try:  termshop photo.jpg"
echo "macOS clipboard paste needs:  brew install pngpaste"
