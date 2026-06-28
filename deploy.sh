#!/usr/bin/env bash
set -euo pipefail

repo_root="$(pwd -P)"
repo_url="${AUCTION_HUNTER_REPO_URL:-https://github.com/alisaleemh/auction-hunter.git}"

mkdir -p /srv/auction-hunter/data

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

git clone --depth 1 --branch main "$repo_url" "$tmpdir/repo"

deploy_commit="$(git -C "$tmpdir/repo" rev-parse --short HEAD)"
export DEPLOY_COMMIT="$deploy_commit"

rsync -a --delete \
  --exclude='.git' \
  --exclude='.env' \
  --exclude='data/auction_index.sqlite3' \
  --exclude='data/geonames_ca_postal_codes.tsv' \
  "$tmpdir/repo"/ "$repo_root"/
mv -f "$tmpdir/repo/data/geonames_ca_postal_codes.tsv" "$repo_root/data/geonames_ca_postal_codes.tsv"

docker compose config
docker compose pull
docker compose build --pull
docker compose up -d
docker compose ps
