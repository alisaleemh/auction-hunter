# Auction Hunter

A local Flask app that searches a local SQLite index of current and open lots from `hibid.com` and `403auction.com`.

## Features

- Server-rendered search page at `GET /`
- JSON API at `GET /api/search?q=...`
- Local SQLite-backed search index
- Built-in nightly index scheduler
- Manual index rebuild command
- Case-insensitive token matching across lot title, condition, and details
- Indexed scope limited to open lots ending within the next 7 days
- Generic schema with provider raw payload retention

## Local Setup

Create and activate a virtual environment, then install the local requirements:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
source .venv/bin/activate
python app.py serve --port 5001
```

Open `http://127.0.0.1:5001`.

## Build the Index

```bash
source .venv/bin/activate
python app.py index
```

The app reads only from the local SQLite index. Run `python app.py index` once before the first search. The `serve` command also starts a built-in nightly scheduler that refreshes the index automatically.

## Test

```bash
source .venv/bin/activate
pytest -q
```

## Production Deployment

The repo includes a Docker Compose stack with separate `web` and `indexer` services:

```bash
docker compose up -d --build
```

The web app listens on port `5001` and stores its SQLite database under `data/`.

## Notes

- The tool is read-only.
- HiBid search is fixed to `zip=L9T 8N6` and `miles=25` in v1.
- Search requests do not fetch live upstream data.
- Results are limited to lots ending within the next 7 days.
