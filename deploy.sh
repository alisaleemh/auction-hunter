#!/usr/bin/env bash
set -euo pipefail

repo_root="$(pwd -P)"
repo_url="${AUCTION_HUNTER_REPO_URL:-https://github.com/alisaleemh/auction-hunter.git}"

git config --global --add safe.directory "$repo_root" || true

mkdir -p "$repo_root/data"
if [[ ! -w "$repo_root" || ! -w "$repo_root/data" ]]; then
  echo "Deploy path must be writable by $(id -un): $repo_root and $repo_root/data" >&2
  echo "Fix once on the LXC: chown -R $(id -un):$(id -gn) '$repo_root'" >&2
  exit 1
fi

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

git clone --depth 1 --branch main "$repo_url" "$tmpdir/repo"

deploy_commit="$(git -C "$tmpdir/repo" rev-parse --short HEAD)"
export DEPLOY_COMMIT="$deploy_commit"

rsync -rltD --delete --no-owner --no-group --no-perms --omit-dir-times \
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
