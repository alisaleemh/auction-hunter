#!/usr/bin/env bash
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

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
rsync -a --exclude='data/geonames_ca_postal_codes.tsv' "$tmpdir"/ "$repo_root"/

docker compose config
docker compose pull
docker compose build --pull
docker compose up -d
docker compose ps
