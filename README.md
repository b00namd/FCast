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
cp .env.example .env   # FCAST_WEB_PASSWORD setzen!
docker compose up -d --build
```

Dashboard: `http://<host>:8000` (Basic Auth). Nur im Heimnetz betreiben, für Zugriff von
unterwegs ein VPN nutzen.

Nützliche Befehle im Container: `docker compose exec fcast fcast sources status`,
`docker compose exec fcast fcast collect --once`.

Backtest (Regel auf den gespeicherten Preisen nachspielen):
`docker compose exec fcast fcast backtest --rule BUY_DIP --from 2026-10-01 --sweep dip_pct=5,10,15,20 --curves`
– oder im Dashboard unter „Backtest“.
