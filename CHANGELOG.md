# Changelog

Alle nennenswerten Änderungen an FCast. Format angelehnt an [Keep a Changelog](https://keepachangelog.com/de/1.1.0/).

## [Unreleased]

### Phase 0 – Projekt-Setup
- Projektstruktur mit `uv` (src-Layout, Python 3.12) und Paketgerüst laut `CLAUDE.md`
- Abhängigkeiten: httpx, selectolax, SQLAlchemy 2.0, Alembic, APScheduler 3.x, FastAPI, Jinja2, Typer, pydantic-settings
- `ruff`, `mypy --strict` und `pytest` (+ `pytest-asyncio`) in `pyproject.toml` konfiguriert
- `fcast.config.Settings`: `FCAST_PLATFORM` (`console`/`pc`), `FCAST_DB_PATH`, `FCAST_TELEGRAM_TOKEN`,
  `FCAST_TELEGRAM_CHAT_ID`, `FCAST_COLLECT_INTERVAL_MIN` (Default 30)
- CLI: `fcast --version`, `fcast config` (Secrets maskiert)
- `Dockerfile` (Multi-Stage, Non-Root), `docker-compose.yml` mit Volume für die SQLite-DB
- `.env.example`, `.gitignore`, `.dockerignore`
