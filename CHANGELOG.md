# Changelog

Alle nennenswerten Änderungen an FCast. Format angelehnt an [Keep a Changelog](https://keepachangelog.com/de/1.1.0/).

## [Unreleased]

### Phase 2 – Preisquellen & Collector
- **Nachtrag:** FUTBIN-Adapter (Preis Konsole/PC, Aktualisierungszeit, Spielerdaten aus der
  Spielerseite). Web-Quellen werden über `FCAST_SOURCES` aktiviert (Default `futbin`).
  Neue Tabelle `source_refs` für den FUTBIN-Link je Karte, CLI `fcast watch add … --futbin <url>`.
  Lastverteilung: lokale Quellen (CSV) werden immer gelesen, Web-Quellen pro Spieler und Lauf
  rotiert, bei Ausfall springt die nächste ein. FUT.GG (Preis nur via gesperrter API) und FUTWIZ
  (Cloudflare-Challenge) sind technisch nicht nutzbar, siehe `docs/sources.md`.
- Prüfung der Web-Quellen (Stand 29.09.2026): FUT.GG (Stormstrike Inc., ToS vom 13.05.2026) und
  FUTBIN (Better Collective, ToS vom 24.02.2026) verbieten automatisierten Zugriff bzw. Scraping
  ohne ausdrückliche Erlaubnis. FUT.GG sperrt zusätzlich `/api/*` per robots.txt. **Deshalb ist
  kein Web-Adapter eingebaut.**
- Interface `PriceSource` (`fetch_price`, `fetch_player`) mit `PriceQuote`/`PlayerInfo`.
- `ManualSource`: CSV-Datei (`FCAST_MANUAL_CSV`), Tausendertrenner erlaubt, Zeitstempel ohne
  Offset in `FCAST_TIMEZONE` (Default Europe/Berlin), mehrere Zeilen je Karte als Historie.
- `PoliteHttpClient` für künftige, erlaubte Web-Quellen: robots.txt (RFC 9309), ≥ 3 s Abstand
  pro Host, 5-Min.-Cache, Retry mit exponentiellem Backoff und `Retry-After`, eigener User-Agent
  (Kontakt optional über `FCAST_HTTP_CONTACT`).
- Collector: holt Preise aller aktiven Watchlist-Spieler, erste Quelle mit Preis gewinnt,
  identische Quotes werden nicht doppelt gespeichert, fehlende Spielerdaten werden ergänzt.
  Fehler einer Quelle werden geloggt und stoppen den Lauf nicht.
- APScheduler (Intervall aus `FCAST_COLLECT_INTERVAL_MIN`, erster Lauf sofort, keine
  Überlappung), sauberes Beenden bei SIGTERM.
- CLI: `fcast collect [--once] [-v]`, `fcast prices import <csv>` für Historien-Import.
- Docker: Container läuft dauerhaft als Collector (`restart: unless-stopped`), CSV-Eingang
  über `./import/prices.csv`.

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
