# FCast – Projektkontext für Claude Code

## Was ist FCast?
FCast ist ein privates Analyse- und Alarm-Tool für den Transfermarkt in EA FC 27 Ultimate Team.
Es sammelt Spielerpreise, erkennt günstige Einstiegspunkte, bewertet anstehende Promos
und meldet Kaufgelegenheiten per Push (ntfy). **Gekauft und verkauft wird immer manuell durch den Nutzer.**

Den Ablauf in Phasen beschreibt `PLAN.md`. Es wird immer nur eine Phase umgesetzt, danach wird gestoppt und zusammengefasst.

## Harte Grenzen (nicht verhandelbar)
- **Keine Automatisierung der EA Web App / Companion App.** Kein Login bei EA, keine Session-Cookies,
  keine automatisierten Such-, Kauf- oder Verkaufsaktionen, kein Browser-Automation-Code gegen EA-Dienste.
  FCast liest nur öffentliche Preisdaten und gibt Empfehlungen aus.
- Externe Datenquellen (z. B. FUT.GG, FUTBIN) werden rücksichtsvoll abgefragt:
  `robots.txt` respektieren, maximal 1 Request pro 3 Sekunden pro Host, eigener User-Agent,
  Caching, exponentielles Backoff bei Fehlern. Nutzungsbedingungen der Quellen vor dem Einbau prüfen
  und das Ergebnis in `docs/sources.md` festhalten.
- **Entscheidung des Nutzers (29.09.2026):** Web-Quellen werden trotz Scraping-Verbot in den
  Nutzungsbedingungen abgefragt; das Risiko (IP-Sperre, rechtliche Schritte) trägt der Nutzer.
  Aktiv: FUTBIN (erste Wahl), FUTNext (Ersatz). FUT.GG und FUTWIZ sind technisch nicht nutzbar
  (siehe `docs/sources.md`). Weiterhin verbindlich: `robots.txt`, Rate-Limit, ehrlicher User-Agent,
  keine Logins, kein Umgehen von Bot-Schutz (Cloudflare-Challenges, Headless-Browser, Proxy-/IP-Rotation,
  gefälschte User-Agents). Cookies nur für Seiteneinstellungen (z. B. Plattform), nie für Sessions.
  Antwortet eine Quelle mit 403/429/Challenge, wird sie automatisch pausiert (Default 24 h).
- In Tests keine Live-Requests. Stattdessen gespeicherte HTML/JSON-Fixtures unter `tests/fixtures/` verwenden.
- Secrets (ntfy-Token, Web-Passwort usw.) nur über `.env`, niemals committen.

## Tech-Stack
- Python 3.12, Paketverwaltung mit `uv`
- HTTP: `httpx` (async), Parsing: `selectolax`
- DB: SQLite + SQLAlchemy 2.0 (typed ORM), Migrationen mit Alembic
- Scheduler: APScheduler
- Konfiguration: `pydantic-settings`
- Web-Dashboard: FastAPI + Jinja2 + HTMX, Charts mit Chart.js
- Alerts: selbst gehostetes ntfy (JSON-API über `httpx`), austauschbar über `alerts/notifier.py`
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
- **ÜV** = „überteuert verkaufen“: Karte deutlich über dem niedrigsten Sofortkauf einstellen, lohnt sich
  bei dünnem Angebot, steigender Nachfrage und genug Luft bis zum EA-Höchstpreis.
- **Holo** = besondere, wertvollere Variante einer Karte (z. B. zu einer normalen TOTW-Karte). Idee: die
  normale Karte zum Preis der Holo-Variante verkaufen, wenn die normale knapp ist.
- **extinct** = keine Angebote auf dem Markt; ist ein eigenes Signal, kein fehlender Preis.
- Schwerpunkt des Projekts ist Spekulation (Leaks, Promos, TOTW, ÜV/Holo), siehe `PLAN.md`.
- Typische Marktmuster: Einbruch zum Promo-Start (meist Freitagabend), Hochs zur Weekend League,
  steigende Preise für SBC-Futter (Ratings 82–88) vor großen SBCs.

## Arbeitsweise
- Vor jeder Phase einen kurzen Plan zeigen, dann umsetzen.
- Kleine, nachvollziehbare Commits (Conventional Commits).
- Jede Phase endet mit grünen Tests, `ruff check`, `mypy` und einem kurzen Update in `CHANGELOG.md`.
- Bei Unklarheiten (z. B. Struktur einer externen Seite) nachfragen statt raten.
- Sprache: Code und Kommentare auf Englisch, Doku und Commit-Beschreibungen gern auf Deutsch.
