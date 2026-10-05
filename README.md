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
cp .env.example .env   # FCAST_WEB_PASSWORD setzen, wenn ein Login gewünscht ist
docker compose up -d --build
```

Dashboard: `http://<host>:8000`. Mit gesetztem `FCAST_WEB_PASSWORD` fragt es Basic Auth ab,
ohne läuft es ohne Login – nur im Heimnetz betreiben, für Zugriff von unterwegs ein VPN nutzen.

Nützliche Befehle im Container: `docker compose exec fcast fcast sources status`,
`docker compose exec fcast fcast collect --once`.

Backup: täglich um `FCAST_BACKUP_TIME` (Standard 03:30) nach `./backups` auf dem Host, die
letzten `FCAST_BACKUP_KEEP` (14) bleiben liegen; sofort: `docker compose exec fcast fcast db backup`.
Das Verzeichnis muss dem Container-Benutzer gehören (einmalig, ohne sudo):
`docker run --rm -v ./backups:/b alpine chown $(docker compose exec fcast id -u):$(docker compose exec fcast id -g) /b`.
Wiederherstellen: Container stoppen, Backup als `fcast.db` ins Volume kopieren, starten.

Potenzial-Radar: Dashboard „Radar“ – Frühsignale auch für Karten außerhalb der Watchlist
(Scanner über FUTBIN-Listen, Last über `FCAST_RADAR_PER_RUN`).

Marktlage auf einen Blick: `docker compose exec fcast fcast lage` (oder `--json`).

Backtest (Regel auf den gespeicherten Preisen nachspielen):
`docker compose exec fcast fcast backtest --rule BUY_DIP --from 2026-10-01 --sweep dip_pct=5,10,15,20 --curves`
– oder im Dashboard unter „Backtest“.
