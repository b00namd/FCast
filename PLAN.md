# FCast – Umsetzungsplan

Jede Phase ist in sich abgeschlossen und endet mit Abnahmekriterien.
Reihenfolge einhalten, Phase für Phase.
Ab Phase 3 gilt: Jede neue Funktion bekommt direkt auch eine Ansicht bzw. Bedienung in der Weboberfläche.

**Schwerpunkt (seit 30.09.2026): Spekulation.** FCast soll zeigen, welche Karten durch Leaks, Promos
und TOTW steigen könnten und welche sich überteuert verkaufen lassen (ÜV, Holo). Portfolio und
SBC-Futter sind nachrangig.

---

## ✅ Phase 0 – Projekt-Setup (erledigt)
uv-Projekt, Tooling (ruff, mypy strict, pytest), Settings, Typer-CLI, Docker.

## ✅ Phase 1 – Datenmodell & Datenbank (erledigt)
Tabellen `players`, `price_snapshots`, `watchlist`, `portfolio`, `promos`, `promo_links`, `alerts_log`,
Alembic-Migrationen, Repositories, CLI `fcast watch`.

## ✅ Phase 2 – Preisquellen & Collector (erledigt)
`ManualSource` (CSV), FUTBIN (erste Wahl), FUTNext (Ersatz), höflicher HTTP-Client, Collector mit
Scheduler, automatischer Rückzug bei Sperren (`source_status`). Details: `docs/sources.md`.

## ✅ Phase 3 – Basis-Weboberfläche (erledigt)
`fcast serve` (Dashboard + Collector in einem Prozess), Basic Auth, Übersicht, Watchlist,
Spielerdetail mit Chart und „Preis erfassen“, Status mit „Jetzt sammeln“.

---

## Phase 4 – Marktanalyse & Angebotslage
**Aufgaben** (`analysis/stats.py`, `analysis/market.py`, `analysis/signals.py`)
- Utilities: `round_to_price_step()`, `net_after_tax()`, `profit()` (Preisstufen laut `CLAUDE.md`)
- Kennzahlen pro Karte: 24-h- und 7-Tage-Mittel, Median, Min/Max, Standardabweichung,
  Abweichung aktueller Preis vs. 7-Tage-Mittel in %, Volatilität, Liquiditäts-Proxy
- Tageszeit- und Wochentagsprofil
- **Angebotslage** (neu, aus FUTBIN): alle fünf günstigsten Angebote speichern, dazu EA-Preisspanne
  (Min/Max). Daraus: Lücke zwischen 1. und 2. Angebot („dünnes Angebot“), Luft bis EA-Maximum,
  Status **extinct** (kein Angebot) als eigener Zustand statt Ersatzpreis
- Ausreißer abfedern: Einzelangebote weit unter dem 2. Angebot nicht als Marktpreis werten
- **ÜV-Score** (überteuert verkaufen): hoch bei dünnem Angebot, steigendem Trend, viel Luft bis
  EA-Maximum; Empfehlung „einstellen zu X“ (auf Preisstufe gerundet, Profit nach Steuer)
- Signale `BUY_DIP`, `SELL_TARGET`, `OVERPRICE_CHANCE`
- Dashboard: Kennzahlen und Angebotslage im Spielerdetail, Signalliste mit Filter
- CLI: `fcast analyze <ea_id>`, `fcast signals`

**Abnahme**
- Unit-Tests mit synthetischen Reihen (Dip, Spike, flach, Lücken, Ausreißer, extinct)
- Preisstufen-Rundung ist mit Grenzwerten getestet
- ÜV-Score ist deterministisch, Gewichte liegen in der Config

---

## ✅ Phase 5 – Push-Alerts (erledigt)
Push über selbst gehostetes ntfy (statt Telegram), austauschbarer Notifier. Alert-Engine nach jedem
Sammellauf: Cooldown pro Karte und Regel, Ruhezeiten, Mindestprofit, Regeln einzeln abschaltbar;
Systemmeldungen (Quelle pausiert, Sammellauf ohne Preise). Dashboard-Seite „Alerts“ mit
Einstellungen, Verlauf und Test-Push; CLI `fcast alert test|check`.

---

## Phase 6 – Leak- & Promo-Radar
**Aufgaben**
- **Leak-Eingang:** Einträge aus erlaubten Quellen sammeln – offizielle Reddit-API (Posts mit
  Leak-Bezug), RSS-Feeds von FUT-News-Seiten (je Quelle vorher prüfen, Ergebnis in
  `docs/sources.md`). X/Twitter nur manuell bzw. über Recherche auf Zuruf (kein Login, kein Scraping).
- Einfügen-Hilfe: Leak-Text ins Dashboard kopieren → FCast erkennt Spieler/Ligen/Nationen/Vereine
  und schlägt einen Promo-Eintrag vor, der bestätigt oder verworfen wird
- Promos mit Zeitraum, Quelle, Konfidenz (0–1) und Links (Spieler/Liga/Nation/Verein) pflegen
- **Kandidaten-Pool:** Karten, die von einem Leak betroffen sind, automatisch mitverfolgen –
  mit geringerer Frequenz als die Watchlist (Default 1–2 Abrufe pro Tag), Obergrenze pro Tag
- **Link-Scoring:** Score pro Karte aus Treffer mit geleakten Karten (Spieler > Verein > Liga > Nation),
  Zeit bis Promo-Start, Konfidenz, Preis vs. 7-Tage-Mittel, Liquidität, Angebotslage
- Signal `PROMO_PREBUY` (Score über Schwelle, Start in 2–7 Tagen)
- Dashboard: Leak-Eingang, Promo-Kalender, Top-Kandidaten mit Begründung („gleiche Liga wie X“)

**Abnahme**
- Scoring ist deterministisch und getestet, Gewichte liegen in der Config
- Kandidaten-Abrufe halten Frequenz und Tageslimit ein (Test)
- Parser für Leak-Texte ist mit Beispieltexten getestet (Fixtures)

---

## Phase 7 – TOTW- & Holo-Spekulation
**Aufgaben**
- **Recherche zuerst:** Wie funktionieren Holo-Karten in FC 27 (Varianten, Erscheinen, Preisbezug)?
  Ergebnis in `docs/` festhalten, Beispiele als Fixtures
- **Kartenpaare:** normale Karte ↔ Holo-Variante (bzw. Gold ↔ TOTW) verknüpfen; Preisabstand,
  Angebotslage der normalen Karte, Signal `HOLO_SPREAD` (normale Karte knapp, Abstand groß)
- **TOTW-Prognose:** echte Spieldaten des Wochenendes (Tore, Vorlagen, Noten) über eine offizielle API
  mit Free-Tier (z. B. API-Football; Nutzungsbedingungen vorher prüfen) → Kandidatenliste bis
  Montag/Dienstag, damit vor der TOTW-Veröffentlichung (mittwochs) gekauft werden kann
- Auswertung nach Veröffentlichung: Trefferquote der Prognose, Preisreaktion der normalen Karten
- Dashboard: TOTW-Kandidaten, Holo-Paare mit Spread, Trefferquote

**Abnahme**
- Prognose und Spread-Berechnung sind mit Fixtures getestet
- API-Aufrufe bleiben im Free-Tier-Limit (Test mit Zähler)

---

## Phase 8 – Backtesting & Lernen aus Promos
**Aufgaben**
- Engine, die Signale auf historischen Snapshots simuliert (Kauf zum Snapshot-Preis, Verkauf nach
  Regel oder Haltedauer, Steuer einrechnen)
- **Promo-Verläufe:** Preisreaktion betroffener Karten vor/nach Promo-Start und TOTW-Release
  automatisch auswerten (z. B. Ø-Änderung T-3 bis T+2)
- Kennzahlen: Trefferquote, Ø Profit pro Trade, Max-Drawdown, Kapitalbindung
- Parameter-Sweep für Schwellen und Scoring-Gewichte
- CLI: `fcast backtest --rule PROMO_PREBUY --from ... --to ...`, Report im Dashboard

**Abnahme**
- Tests mit konstruierten Reihen, bei denen das Ergebnis bekannt ist
- Report ist reproduzierbar (gleiche Daten → gleiches Ergebnis)

---

## Später / Ideen-Backlog
- **Portfolio & Profit** (bisher Phase 6): Käufe/Verkäufe erfassen, realisierter Profit nach Steuer,
  offene Positionen, Verkaufsempfehlung, Risiko-Limit
- **SBC-Futter-Tracker** (bisher Phase 9): günstigste Preise je Rating-Stufe 82–90, Signal `FODDER_STOCK`
- Sentiment-Signal (Reddit-Titel nach Spielernamen, Hype-Score)
- Kartenbewertung (Stats, AcceleRATE, PlayStyles) als Faktor im Scoring
- SBC-Lösungsrechner mit eigenem Club (Import per CSV, kein EA-Login)
- Selbstlernende Gewichtung: Signale anhand ihrer Trefferquote aus dem Backtesting nachjustieren
- Backup der SQLite-DB (Cronjob oder in bestehendes Backup-Konzept einhängen)
- Zugriff von unterwegs über VPN (WireGuard in der Fritzbox)
