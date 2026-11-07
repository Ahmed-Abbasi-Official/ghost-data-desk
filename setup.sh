#!/bin/sh
# One-time setup: Python environment, the public dataset, and a smoke test.
set -e
cd "$(dirname "$0")"

python3 -m venv .venv
.venv/bin/pip install -q --disable-pip-version-check pypdf

if [ ! -f dataset/deletion_requests.csv ]; then
  echo "Fetching the public HackerRank Orchestrate (October 2026) dataset..."
  tmp="$(mktemp -d)"
  git clone -q --depth 1 --filter=blob:none --sparse \
    https://github.com/interviewstreet/hackerrank-orchestrate-october26.git "$tmp/src"
  git -C "$tmp/src" sparse-checkout set dataset
  cp -R "$tmp/src/dataset" ./dataset
  rm -rf "$tmp"
fi

bin/ghost stats
echo
echo "Ready. In the Switch template, use this repository folder: $(pwd)"
