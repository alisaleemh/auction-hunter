#!/usr/bin/env bash
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

git config --global --add safe.directory "$repo_root" || true

mkdir -p /srv/auction-hunter/data

if [[ ! -f .env ]]; then
  echo "missing required local file: .env" >&2
  exit 1
fi

git fetch origin main

deploy_commit="$(git rev-parse --short origin/main)"
export DEPLOY_COMMIT="$deploy_commit"

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

git archive --format=tar origin/main | tar -xf - -C "$tmpdir"
rsync -a --delete \
  --exclude='.env' \
  --exclude='data/auction_index.sqlite3' \
  --exclude='data/geonames_ca_postal_codes.tsv' \
  "$tmpdir"/ "$repo_root"/
mv -f "$tmpdir/data/geonames_ca_postal_codes.tsv" "$repo_root/data/geonames_ca_postal_codes.tsv"

docker compose config
docker compose pull
docker compose build --pull
docker compose up -d
docker compose ps
