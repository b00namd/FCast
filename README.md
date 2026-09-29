# FCast

Privates Analyse- und Alarm-Tool für den EA FC Ultimate Team Transfermarkt.
Gekauft und verkauft wird immer manuell. Details: `CLAUDE.md`, Umsetzung: `PLAN.md`.

## Entwicklung

```bash
uv sync
uv run fcast --version
uv run pytest
uv run ruff check .
uv run mypy
```

## Docker

```bash
cp .env.example .env
docker compose up --build
```
