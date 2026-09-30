# Changelog

Alle nennenswerten Änderungen an FCast. Format angelehnt an [Keep a Changelog](https://keepachangelog.com/de/1.1.0/).

## [Unreleased]

### Phase 5 – Push-Alerts
- **ntfy statt Telegram** (Entscheidung des Nutzers): selbst gehostet auf dem Homeserver
  (`~/docker/ntfy`, Login-Pflicht, keine Weboberfläche). FCast sendet über die JSON-API mit
  Zugriffstoken (`FCAST_NTFY_URL`, `FCAST_NTFY_TOPIC`, `FCAST_NTFY_TOKEN`); ohne URL landen
  Alerts nur im Log. Telegram-Einstellungen entfernt.
- `alerts/notifier.py`: austauschbare `Notifier`-Schnittstelle (ntfy, Log).
- `alerts/engine.py`: nach jedem Sammellauf werden Signale zu Nachrichten – mit Preis, Ø 7 Tage,
  Empfehlung, Profit nach Steuer, Begründung sowie Buttons „Dashboard“ und „FUTBIN“
  (`FCAST_DASHBOARD_URL`). Kauf-Dip und ÜV mit hoher Priorität.
- Cooldown pro Karte und Regel (Default 6 h, über `alerts_log`), Ruhezeiten (auch über
  Mitternacht; zurückgehaltene Signale kommen danach, wenn sie noch gelten), Mindestprofit,
  Regeln einzeln abschaltbar. Fehlgeschlagene Zustellungen werden beim nächsten Lauf erneut versucht.
- Systemmeldungen: Quelle pausiert (403/429/Challenge) und Sammellauf ganz ohne Preise.
- Dashboard: Seite **Alerts** (Einstellungen in der neuen Tabelle `app_settings`, überschreiben die
  Env-Defaults; Verlauf; Test-Push). CLI: `fcast alert test`, `fcast alert check` (Trockenlauf).

### Phase 4 – Marktanalyse & Angebotslage
- `analysis/pricing.py`: Preisstufen (runden ab/auf/nächste, n Stufen weiter), `net_after_tax()`,
  `profit()`, `break_even_sell_price()`; Grenzwerte getestet. Die Stufen aus `CLAUDE.md` sind für
  50er/100er belegt und passen zu allen bisher gesehenen FUTBIN-Preisen.
- **Angebotslage:** FUTBIN liefert jetzt alle fünf günstigsten Angebote und die EA-Preisspanne;
  gespeichert in der neuen Tabelle `market_state` (aktueller Stand je Karte/Plattform).
  **Extinct** (keine Angebote) wird erkannt und mit Startzeit gespeichert; die Ersatzquelle wird
  dann nicht mehr nach einem veralteten Preis gefragt.
- **Ausreißer:** Liegt das günstigste Angebot mehr als `FCAST_OUTLIER_GAP_PCT` (15 %) unter dem
  zweiten, wird das zweite als Marktpreis gespeichert; das Ausreißer-Angebot bleibt sichtbar und
  zählt nicht in den ÜV-Score.
- `analysis/stats.py`: 24-h- und 7-Tage-Kennzahlen, Änderung 24 h, Abweichung zum Ø, Volatilität,
  Preisänderungen pro Tag, Tageszeit- und Wochentagsprofil (Ortszeit); robust bei Datenlücken.
- `analysis/signals.py`: `BUY_DIP` (mit empfohlenem Max-Kaufpreis), `SELL_TARGET`,
  `OVERPRICE_CHANCE` (ÜV-Score 0–100 aus Angebot, Trend, Luft bis EA-Maximum, Liquidität;
  Einstellpreis knapp unter dem nächsten Angebot, Profit nach Steuer). Schwellen und Gewichte
  sind Settings (`FCAST_DIP_PCT`, `FCAST_UEV_*` …).
- Dashboard: neue Seite **Signale** mit Filter; Spielerdetail mit Signalen, Kennzahlen,
  Angebotslage (Ausreißer markiert, extinct seit …) und Profil-Charts; Übersicht mit
  extinct- und Signal-Markierungen.
- CLI: `fcast analyze <ea_id>`, `fcast signals [--rule …]`.

### Phase 3 – Basis-Weboberfläche
- `fcast serve`: FastAPI-Dashboard und Collector/Scheduler in einem Prozess (Scheduler im Lifespan).
  Docker startet jetzt `serve`, Port 8000, Docker-Healthcheck über `/health`.
- Basic Auth (`FCAST_WEB_USER`, `FCAST_WEB_PASSWORD`); ohne Passwort startet das Dashboard nicht.
  Same-Origin-Prüfung für alle Formular-Aktionen (Schutz gegen Cross-Site-Requests).
- Jinja2 + HTMX + Pico.css, Chart.js; Bibliotheken liegen versioniert unter `web/static/vendor`
  (keine Build-Pipeline, kein CDN). Deutsche Oberfläche, Dark Mode, mobil nutzbar.
- Seiten: **Übersicht** (letzter Preis, Quelle, Änderung zu 24 h, Markierung bei erreichtem
  Kauf-/Verkaufsziel), **Watchlist** (hinzufügen inkl. FUTBIN-Link, inline bearbeiten,
  (de)aktivieren), **Spielerdetail** (Kennzahlen und Chart der letzten 7 Tage je Quelle,
  Schnell-Eingabe „Preis erfassen“, letzte Preise), **Status** (letzter Lauf, Fehler,
  Quellen mit Pause und „Fortsetzen“, Button „Jetzt sammeln“).
- Betragseingaben verstehen „1.200.000“, „1,2M“, „45k“.

### Phase 2 – Preisquellen & Collector
- **Nachtrag 2:** FUTNext als Ersatzquelle (PC-Preise über Einstellungs-Cookie, Plattform-Label
  wird geprüft). Automatischer Rückzug: HTTP 403/429/Bot-Challenge → Quelle wird ohne weitere
  Versuche für `FCAST_SOURCE_PAUSE_H` (24 h) pausiert, persistent in `source_status`.
  `FCAST_SOURCE_STRATEGY` (`priority`/`rotate`), Start-Jitter `FCAST_COLLECT_JITTER_S`.
  CLI: `fcast sources status`, `fcast sources resume <name>`.
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
