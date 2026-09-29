# FCast – Projektkontext für Claude Code

## Was ist FCast?
FCast ist ein privates Analyse- und Alarm-Tool für den Transfermarkt in EA FC 27 Ultimate Team.
Es sammelt Spielerpreise, erkennt günstige Einstiegspunkte, bewertet anstehende Promos
und meldet Kaufgelegenheiten per Telegram. **Gekauft und verkauft wird immer manuell durch den Nutzer.**

Den Ablauf in Phasen beschreibt `PLAN.md`. Es wird immer nur eine Phase umgesetzt, danach wird gestoppt und zusammengefasst.

## Harte Grenzen (nicht verhandelbar)
- **Keine Automatisierung der EA Web App / Companion App.** Kein Login bei EA, keine Session-Cookies,
  keine automatisierten Such-, Kauf- oder Verkaufsaktionen, kein Browser-Automation-Code gegen EA-Dienste.
  FCast liest nur öffentliche Preisdaten und gibt Empfehlungen aus.
- Externe Datenquellen (z. B. FUT.GG, FUTBIN) werden rücksichtsvoll abgefragt:
  `robots.txt` respektieren, maximal 1 Request pro 3 Sekunden pro Host, eigener User-Agent,
  Caching, exponentielles Backoff bei Fehlern. Nutzungsbedingungen der Quellen vor dem Einbau prüfen.
- In Tests keine Live-Requests. Stattdessen gespeicherte HTML/JSON-Fixtures unter `tests/fixtures/` verwenden.
- Secrets (Telegram-Token usw.) nur über `.env`, niemals committen.

## Tech-Stack
- Python 3.12, Paketverwaltung mit `uv`
- HTTP: `httpx` (async), Parsing: `selectolax`
- DB: SQLite + SQLAlchemy 2.0 (typed ORM), Migrationen mit Alembic
- Scheduler: APScheduler
- Konfiguration: `pydantic-settings`
- Web-Dashboard: FastAPI + Jinja2 + HTMX, Charts mit Chart.js
- Alerts: Telegram Bot API direkt über `httpx`
- Qualität: `ruff` (lint + format), `mypy --strict` für `src/`, `pytest` + `pytest-asyncio`
- Deployment: Docker + docker-compose (läuft auf dem Homeserver)

## Projektstruktur (Ziel)
```
fcast/
  src/fcast/
    config.py          # Settings (Plattform, Intervalle, Telegram, DB-Pfad)
    db/                # Modelle, Session, Repositories
    sources/           # Preisquellen-Adapter (Interface + Implementierungen)
    collector/         # Sammeljobs, Scheduler
    analysis/          # Statistik, Signale, Promo-Scoring, Backtesting
    alerts/            # Regeln, Deduplizierung, Telegram
    portfolio/         # Käufe, Verkäufe, Profit
    web/               # FastAPI-App, Templates
    cli.py             # Typer-CLI
  tests/
  src/fcast/db/migrations/  # Alembic migrations (inside the package, ships with the Docker image)
  docker/
  PLAN.md
  CLAUDE.md
```

## Domänenwissen
- **EA-Steuer:** 5 % auf jeden Verkauf. Netto = Verkaufspreis × 0,95.
  Profit = Verkaufspreis × 0,95 − Kaufpreis.
- **Preisstufen im Markt** (Gebotsschritte, vor Verwendung verifizieren):
  bis 1.000 → 50er-Schritte; 1.000–10.000 → 100er; 10.000–50.000 → 250er;
  50.000–100.000 → 500er; ab 100.000 → 1.000er. Alle empfohlenen Preise auf gültige Stufen runden.
- **Plattform** ist konfigurierbar (`FCAST_PLATFORM`). Preise verschiedener Plattformen nie mischen.
- Typische Marktmuster: Einbruch zum Promo-Start (meist Freitagabend), Hochs zur Weekend League,
  steigende Preise für SBC-Futter (Ratings 82–88) vor großen SBCs.

## Arbeitsweise
- Vor jeder Phase einen kurzen Plan zeigen, dann umsetzen.
- Kleine, nachvollziehbare Commits (Conventional Commits).
- Jede Phase endet mit grünen Tests, `ruff check`, `mypy` und einem kurzen Update in `CHANGELOG.md`.
- Bei Unklarheiten (z. B. Struktur einer externen Seite) nachfragen statt raten.
- Sprache: Code und Kommentare auf Englisch, Doku und Commit-Beschreibungen gern auf Deutsch.
