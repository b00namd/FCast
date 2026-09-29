# FCast – Umsetzungsplan

Jede Phase ist in sich abgeschlossen und endet mit Abnahmekriterien.
Reihenfolge einhalten, Phase für Phase.
Ab Phase 3 gilt: Jede neue Funktion bekommt direkt auch eine Ansicht bzw. Bedienung in der Weboberfläche.

---

## Phase 0 – Projekt-Setup
**Aufgaben**
- Repo-Struktur laut `CLAUDE.md` anlegen, `uv init`, Abhängigkeiten installieren
- `ruff`, `mypy`, `pytest` konfigurieren (`pyproject.toml`)
- `config.py` mit `pydantic-settings`: `FCAST_PLATFORM`, `FCAST_DB_PATH`, `FCAST_TELEGRAM_TOKEN`,
  `FCAST_TELEGRAM_CHAT_ID`, `FCAST_COLLECT_INTERVAL_MIN` (Default 30)
- `.env.example`, `.gitignore`, `Dockerfile`, `docker-compose.yml` (Volume für die SQLite-DB)
- Typer-CLI mit `fcast --version`

**Abnahme**
- `uv run pytest`, `ruff check .` und `mypy src` laufen grün
- `docker compose up` startet einen Container, der die Version ausgibt

---

## Phase 1 – Datenmodell & Datenbank
**Tabellen**
- `players`: id, ea_id (unique), name, rating, position, card_type, league, nation, club, updated_at
- `price_snapshots`: id, player_id, platform, price, source, captured_at (Index auf player_id + captured_at)
- `watchlist`: player_id, target_buy, target_sell, note, active
- `portfolio`: id, player_id, buy_price, bought_at, sell_price, sold_at, status (holding/listed/sold)
- `promos`: id, name, starts_at, ends_at, source, confidence (0–1), note
- `promo_links`: promo_id, link_type (player/league/nation/club), link_value
- `alerts_log`: id, rule, player_id, message, sent_at

**Aufgaben**
- SQLAlchemy-Modelle, Alembic-Init und erste Migration
- Repository-Funktionen (CRUD) mit Tests gegen eine In-Memory-SQLite
- CLI: `fcast watch add <ea_id> --buy X --sell Y`, `fcast watch list`

**Abnahme**
- Migration läuft auf einer frischen DB
- CRUD-Tests grün

---

## Phase 2 – Preisquellen & Collector
**Aufgaben**
- Interface `PriceSource` mit `async fetch_price(ea_id, platform) -> PriceQuote`
  und `async fetch_player(ea_id) -> PlayerInfo`
- `ManualSource` (CSV-Import) als erste, risikofreie Quelle
- Erster Web-Adapter (z. B. FUT.GG). Vorher `robots.txt` und Nutzungsbedingungen prüfen und das Ergebnis im PR notieren.
  Rate-Limiter (≤ 1 Req/3 s pro Host), Retry mit Backoff, Response-Cache (5 Min.)
- Collector-Job: holt Preise für alle aktiven Watchlist-Spieler und speichert Snapshots
- APScheduler-Integration, Intervall aus der Config
- CLI: `fcast collect --once`

**Abnahme**
- Adapter-Tests laufen ausschließlich gegen Fixtures
- `fcast collect --once` schreibt Snapshots in die DB
- Fehler einer Quelle stoppen den Collector nicht (Logging + Weiterlaufen)

---

## Phase 3 – Basis-Weboberfläche
**Aufgaben**
- FastAPI-App im selben Container wie Collector und Scheduler (ein Prozess, Scheduler im Lifespan starten)
- Port 8000 in `docker-compose.yml` freigeben, Basic Auth (Credentials aus `.env`:
  `FCAST_WEB_USER`, `FCAST_WEB_PASSWORD`)
- Jinja2 + HTMX, schlichtes responsives Layout (Pico.css oder eigenes CSS, keine Build-Pipeline)
- Seiten:
  - **Watchlist:** Spieler hinzufügen (EA-ID, Ziel-Kaufpreis, Ziel-Verkaufspreis), bearbeiten, deaktivieren
  - **Preisübersicht:** Tabelle aller Watchlist-Spieler mit letztem Preis, Zeitpunkt, Änderung zu 24 h
  - **Spielerdetail:** Preis-Chart der letzten 7 Tage (Chart.js)
  - **Status:** letzter Collector-Lauf, Fehler pro Quelle, Button „Jetzt sammeln“
- Healthcheck-Endpunkt `/health` für Docker

**Abnahme**
- `docker compose up` → Dashboard unter `http://<host>:8000` erreichbar, Login erforderlich
- Watchlist komplett im Browser pflegbar, Preise und Chart sichtbar
- Route-Tests mit FastAPI-TestClient

---

## Phase 4 – Marktanalyse
**Aufgaben** (`analysis/stats.py`, `analysis/signals.py`)
- Utilities: `round_to_price_step()`, `net_after_tax()`, `profit()`
- Kennzahlen pro Spieler: 24-h- und 7-Tage-Mittel, Median, Min/Max, Standardabweichung,
  Abweichung aktueller Preis vs. 7-Tage-Mittel in %, Volatilität
- Liquiditäts-Proxy: Anzahl Preisänderungen pro Tag
- Tageszeit- und Wochentagsprofil (Durchschnittspreis je Stunde/Wochentag)
- Signal `BUY_DIP`: Preis ≥ X % unter 7-Tage-Mittel **und** Profit nach Steuer ≥ Mindestmarge
- Signal `SELL_TARGET`: Preis ≥ Zielpreis der Watchlist bzw. des Portfolios
- CLI: `fcast analyze <ea_id>`, `fcast signals`

**Abnahme**
- Unit-Tests mit synthetischen Preisreihen (Dip, Spike, flach, Lücken in den Daten)
- Preisstufen-Rundung ist mit Grenzwerten getestet

---

## Phase 5 – Telegram-Alerts
**Aufgaben**
- Telegram-Client (`sendMessage`, Markdown-Formatierung)
- Alert-Engine: Signale → Nachrichten, Cooldown pro Spieler und Regel (Default 6 h),
  Deduplizierung über `alerts_log`
- Nachricht enthält: Spieler, aktueller Preis, Ø 7 Tage, empfohlener Max-Kaufpreis,
  erwarteter Profit nach Steuer
- Ruhezeiten konfigurierbar (z. B. 00–07 Uhr keine Alerts)
- CLI: `fcast alert test`

**Abnahme**
- Tests mit gemocktem Telegram-Client
- Kein doppelter Alert innerhalb des Cooldowns

---

## Phase 6 – Portfolio & Profit
**Aufgaben**
- CLI: `fcast buy <ea_id> <preis>`, `fcast list <id> <preis>`, `fcast sell <id> <preis>`
- Auswertung: offene Positionen mit aktuellem Wert, realisierter Profit (nach Steuer),
  Gewinn pro Tag/Woche, beste und schlechteste Trades
- Verkaufsempfehlung für offene Positionen (nutzt `SELL_TARGET` und Trend)
- Risiko-Limit: Warnung, wenn eine Position > X % des erfassten Coin-Bestands ausmacht

**Abnahme**
- Profitberechnung mit Steuer ist getestet
- `fcast portfolio` zeigt eine übersichtliche Tabelle (`rich`)

---

## Phase 7 – Dashboard-Ausbau
**Aufgaben**
- Übersichtsseite mit allen aktiven Signalen (BUY_DIP, SELL_TARGET) und Filter
- Spielerdetail erweitern: Kennzahlen aus Phase 4, Tageszeit- und Wochentagsprofil, 30-Tage-Chart
- Portfolio-Seite: Positionen erfassen, Verkauf buchen, Profit-Übersicht
- Alert-Einstellungen im Browser (Cooldown, Ruhezeiten, Mindestmarge)
- Platzhalter-Seite für den Promo-Kalender (wird in Phase 8 gefüllt)

**Abnahme**
- Alle Funktionen aus Phase 4–6 sind ohne CLI im Browser bedienbar
- Endpunkte sind getestet, die Seiten funktionieren auf dem Smartphone

---

## Phase 8 – Promo-Radar
**Aufgaben**
- Promos und Links manuell pflegen (Dashboard + CLI `fcast promo add ...`), Konfidenz je Eintrag
- Optionaler Import-Adapter für Leak-Quellen (gleiche Regeln wie Phase 2, zuerst prüfen)
- **Link-Scoring:** Für jeden Spieler einen Score berechnen aus
  - geteilter Liga/Nation/Verein mit geleakten Promo-Karten
  - Zeit bis zum Promo-Start
  - Konfidenz des Leaks
  - aktueller Preis vs. 7-Tage-Mittel (günstig = besser)
  - Liquidität
- Signal `PROMO_PREBUY`: Score über Schwelle und Promo-Start in 2–7 Tagen
- Kalender-Ansicht mit Wochenmustern (Weekend League, TOTW, Promo-Start)

**Abnahme**
- Scoring-Funktion ist deterministisch und getestet, die Gewichte liegen in der Config
- Dashboard zeigt die Top 20 Promo-Kandidaten mit Begründung („gleiche Liga wie X“)

---

## Phase 9 – SBC-Futter-Tracker
**Aufgaben**
- Günstigste Preise je Rating-Stufe (82–90) tracken, dazu passende Referenzspieler automatisch wählen
- Trend je Stufe, Signal `FODDER_STOCK`, wenn eine Stufe unter ihrem 14-Tage-Mittel liegt
- Dashboard-Widget „Futter-Index“

**Abnahme**
- Futter-Index wird pro Collector-Lauf aktualisiert, Tests mit Fixtures

---

## Phase 10 – Backtesting
**Aufgaben**
- Engine, die Signale auf historischen Snapshots simuliert
  (Kauf zum Snapshot-Preis, Verkauf nach Regel oder Haltedauer, Steuer einrechnen)
- Kennzahlen: Trefferquote, Ø Profit pro Trade, Max-Drawdown, Kapitalbindung
- Parameter-Sweep für Schwellen (z. B. Dip-% 10/15/20/25)
- CLI: `fcast backtest --rule BUY_DIP --from ... --to ...`, Report im Dashboard

**Abnahme**
- Tests mit konstruierten Reihen, bei denen das Ergebnis bekannt ist
- Report ist reproduzierbar (gleiche Daten → gleiches Ergebnis)

---

## Später / Ideen-Backlog
- Sentiment-Signal (Reddit/YouTube-Titel nach Spielernamen, Hype-Score)
- Kartenbewertung (Stats, AcceleRATE, Playstyles) als Faktor im Scoring
- SBC-Lösungsrechner mit eigenem Club (Import per CSV)
- Selbstlernende Gewichtung: Signale anhand ihrer Trefferquote aus dem Backtesting nachjustieren
- Backup der SQLite-DB (Cronjob oder in bestehendes Backup-Konzept einhängen)

---

## Start-Prompt für Claude Code
> Lies `CLAUDE.md` und `PLAN.md`. Setze Phase 0 um. Zeig mir zuerst kurz deinen Plan,
> dann implementiere. Stoppe nach Phase 0 und fasse zusammen, was fertig ist und was ich prüfen soll.
