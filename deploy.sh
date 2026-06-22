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

git checkout -q origin/main -- . ':(exclude)data/geonames_ca_postal_codes.tsv'
git clean -fd

deploy_commit="$(git rev-parse --short origin/main)"
export DEPLOY_COMMIT="$deploy_commit"

docker compose config
docker compose pull
docker compose build --pull
docker compose up -d
docker compose ps
