# Changelog

Alle nennenswerten Änderungen an FCast. Format angelehnt an [Keep a Changelog](https://keepachangelog.com/de/1.1.0/).

## [Unreleased]

### Phase 1 – Datenmodell & Datenbank
- SQLAlchemy-2.0-Modelle für `players`, `price_snapshots`, `watchlist`, `portfolio`, `promos`,
  `promo_links`, `alerts_log`. Zeitstempel werden als UTC gespeichert (naive Datumswerte werden abgelehnt),
  Preise als ganze Coins, Check-Constraints für Preise und Konfidenz, Fremdschlüssel sind aktiv.
- Spielerdetails (Name, Rating, …) sind optional, da vor Phase 2 nur die EA-ID bekannt ist.
- Alembic-Migrationen liegen im Paket unter `src/fcast/db/migrations/` (statt `alembic/`),
  damit sie im Docker-Image enthalten sind. Erste Migration `initial schema`.
- Repository-Funktionen (CRUD) für alle Tabellen, getestet gegen In-Memory-SQLite.
- CLI: `fcast db upgrade`, `fcast watch add <ea_id> --buy X --sell Y [--note] [--name]`,
  `fcast watch list [--all]`. Watch-Befehle migrieren die DB automatisch; Warnung, wenn der
  Zielverkaufspreis nach 5 % Steuer nicht über dem Zielkaufpreis liegt.

### Phase 0 – Projekt-Setup
- Projektstruktur mit `uv` (src-Layout, Python 3.12) und Paketgerüst laut `CLAUDE.md`
- Abhängigkeiten: httpx, selectolax, SQLAlchemy 2.0, Alembic, APScheduler 3.x, FastAPI, Jinja2, Typer, pydantic-settings
- `ruff`, `mypy --strict` und `pytest` (+ `pytest-asyncio`) in `pyproject.toml` konfiguriert
- `fcast.config.Settings`: `FCAST_PLATFORM` (`console`/`pc`), `FCAST_DB_PATH`, `FCAST_TELEGRAM_TOKEN`,
  `FCAST_TELEGRAM_CHAT_ID`, `FCAST_COLLECT_INTERVAL_MIN` (Default 30)
- CLI: `fcast --version`, `fcast config` (Secrets maskiert)
- `Dockerfile` (Multi-Stage, Non-Root), `docker-compose.yml` mit Volume für die SQLite-DB
- `.env.example`, `.gitignore`, `.dockerignore`
