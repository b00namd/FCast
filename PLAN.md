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

## ✅ Phase 4 – Marktanalyse & Angebotslage (erledigt)
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

## ✅ Phase 6 – Leak- & Promo-Radar (erledigt, ohne Reddit)
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

## ✅ Phase 7 – TOTW- & Holo-Spekulation (erledigt; Spielnoten folgen mit API-Football Pro)
> **Stand 30.09.2026:** TOTW-Prognose v1 vorgezogen und umgesetzt (OpenLigaDB, Auswertung über FUTBIN,
> Seite „TOTW“, Alert). Offen: Spielnoten/Vorlagen (API-Football Pro), Reddit (nach Freigabe),
> Holo-Paare.

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

## ✅ Phase 8 – Backtesting & Lernen aus Promos (erledigt)
> **Stand 30.09.2026:** Engine für `BUY_DIP` und `PROMO_PREBUY`, Promo- und TOTW-Verläufe, Sweep,
> CLI `fcast backtest` und Seite „Backtest“. ÜV-Chance und Holo-ÜV sind nicht backtestbar (ob eine
> überteuerte Karte verkauft wurde, ist in den Preisdaten nicht zu sehen). Aussagekräftig wird es
> erst mit einigen Wochen Daten; die Top-5-TOTW-Kandidaten werden dafür jetzt mitbepreist.

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

## ✅ Phase 9 – Betrieb & bessere Signale (erledigt)
> **Stand 01.10.2026:** Abfrage-Takt, tägliches Backup auf den Host, Verlauf der Angebotslage mit
> „dünn seit“, ÜV im Backtest, Mindestprofit für ÜV. Der ÜV-Backtest wird aussagekräftig, sobald
> ein paar Tage Angebots-Verlauf vorliegen (Aufzeichnung seit 01.10.2026).
**Aufgaben**
- **Abfrage-Stufen:** Abfrage-Intervall pro Watchlist-Karte (jede Runde, alle 2 h, alle 6 h);
  Collector überspringt Karten, die noch nicht fällig sind. Einstellbar in Watchlist und CLI
- **Backup:** täglich (Uhrzeit konfigurierbar) per SQLite-Backup-API, Aufbewahrung 14 Tage,
  `fcast db backup`; auf dem Server in ein Host-Verzeichnis außerhalb des Docker-Volumes
- **Verlauf der Angebotslage:** jede Beobachtung (Angebote, EA-Spanne) speichern; daraus
  „dünnes Angebot seit X h“ für ÜV-Score, Spielerdetail und `fcast lage`
- **ÜV backtestbar:** `OVERPRICE_CHANCE` im Backtest auf dem Angebots-Verlauf nachspielen
  (Kauf, Einstellen zum Ziel, verkauft sobald der Markt das Ziel erreicht)
- **Mindestprofit für ÜV-Chancen** wie beim Kauf-Dip

**Abnahme**
- Tests: Intervall-Logik, Backup inkl. Aufräumen, Verlauf/Dünn-seit, ÜV-Backtest mit bekannter Reihe
- Migration läuft auf der bestehenden Datenbank ohne Datenverlust

---

## ✅ Phase 10 – Potenzial-Radar (erledigt)
> **Stand 01.10.2026:** Scanner, Futter-Index, vier Frühsignale, Seite „Radar“, Push, `fcast lage`
> und Trend-Start im Backtest. „Nutzung steigt“ braucht zwei Tage Spielzahlen, der Futter-Index
> einen Tag – bis dahin zeigt das Radar vor allem Trend-Start und „Angebot schrumpft“.
Ziel: Chancen erkennen, bevor sie offensichtlich sind – auch bei Karten, die nicht auf der Watchlist
stehen. (Portfolio/Verkaufs-Tracking hat der Nutzer bewusst zurückgestellt, siehe Backlog.)

**Aufgaben**
- **Markt-Scanner:** FUTBIN-Listen „Popular“, „New Players“ und das aktuelle TOTW alle 12 h
  einlesen (Radar-Bestand); pro Sammellauf einige Radar-Karten bepreisen, jede etwa alle 6 h
- **Verlauf der Spielzahl** (FUTBIN „Games“) pro Karte
- **Futter-Index:** FUTBIN „Cheapest Players“ je Rating 82–90 alle 6 h (Ø der drei günstigsten)
- **Frühsignale:** Trend-Start (stetiger Anstieg, noch kein Sprung), Angebot schrumpft, Nutzung
  steigt (Wachstum der Spielzahl gegenüber den anderen Karten), Futter zieht an
- **Seite „Radar“** mit Potenzial-Score, Begründung und „Beobachten“; Push für starke Treffer
  (abschaltbar); Abschnitt in `fcast lage`
- **Backtest:** Trend-Start nachspielen

**Abnahme**
- Parser mit FUTBIN-Fixtures getestet; Signale mit konstruierten Reihen
- Anfragen bleiben im Rahmen (Scanner-Last konfigurierbar, Zähler im Test)

---

## ✅ Phase 11 – Kartenbewertung (erledigt)
> **Stand 01.10.2026:** Werte, PlayStyles (+), Skills, schwacher Fuß, Größe, Körpertyp und
> AcceleRATE von der FUTBIN-Spielerseite (ohne Extra-Anfrage); Spielwert 0–100 je Positionsgruppe;
> Meta-Score (Spielwert + tatsächliche Nutzung); Radar-Signal „Unterbewertet“; Spielwert im
> ÜV-Score, Promo-Vorkauf, Radar-Potenzial und als Warnung beim Kauf-Dip. Die Gewichte sind
> Startwerte – `fcast cards` prüft sie gegen die FUTBIN-Spielzahlen.

---

## ✅ Phase 12 – Portfolio & Verkaufs-Tracking (erledigt)
> **Stand 03.10.2026:** `fcast portfolio buy/listed/sell/rm/list` und Seite „Portfolio“: Käufe,
> eingestellte und verkaufte Karten, Break-even, Gewinn nach 5 % Steuer, gebundenes Kapital und
> was ein Verkauf zum aktuellen Marktpreis brächte. Kein Import aus der Web-App.

---

## ✅ Phase 13 – ÜV-Einkaufsliste (erledigt)
> **Stand 05.10.2026:** `fcast uev` und Seite „ÜV“: meistgespielte Karten bis zu einem Höchstpreis,
> nach Bewertung gruppiert, mit höchstem sinnvollem Kaufpreis für eine Karte mit Chemie-Stil
> (auf gültige Preisstufe gerundet) und Break-even. Welche Angebote einen Stil tragen, bleibt Handarbeit.

---

## Phase 14 – Selbstlernende Gewichtung
Ziel: Signale, die sich im Backtest bewährt haben, stärker gewichten – und schwache leiser stellen.

**Aufgaben**
- Trefferquote und Ø Profit je Regel (Kauf-Dip, Promo-Vorkauf, ÜV-Chance, Trend-Start) regelmäßig
  aus dem Backtest auf den gespeicherten Daten berechnen und mit Datum speichern
- Daraus Vorschläge für Schwellen und Scoring-Gewichte ableiten (bestes Sweep-Ergebnis mit
  Mindestanzahl Trades, Änderungen pro Lauf begrenzt)
- Vorschläge **nicht automatisch übernehmen**: Anzeige im Dashboard mit „übernehmen“ /
  „verwerfen“, übernommene Werte mit Herkunft speichern
- Seite „Backtest“: Verlauf der Trefferquote je Regel

**Abnahme**
- Tests mit konstruierten Reihen, bei denen die bessere Gewichtung bekannt ist
- Zu wenig Daten → kein Vorschlag (klar angezeigt), keine stillen Änderungen

---

## Phase 15 – SBC-Futter vor großen SBCs
> Futter-Index je Rating 82–90 und Signal „Futter zieht an“ gibt es seit Phase 10. Offen ist der
> Vorlauf: Futter kaufen, **bevor** eine große SBC erscheint.

**Aufgaben**
- SBC-Ankündigungen aus dem Leak- & Promo-Radar als Ereignis erkennen (z. B. Icon-/Hero-SBCs,
  Promo-Start)
- Verlauf des Futter-Index um vergangene SBCs auswerten (wie Promo-Verläufe in Phase 8)
- Signal `FODDER_STOCK`: welche Ratings sich vor einem Ereignis lohnen, Kauf- und Verkaufsziel
  nach Steuer; Push und Abschnitt auf der Seite „Radar“
- Backtest: Futter-Kauf vor vergangenen SBCs nachspielen

**Offen:** Woran erkennt FCast eine „große“ SBC zuverlässig? Vor dem Bau mit dem Nutzer klären.

**Abnahme**
- Tests mit konstruierten Futter-Verläufen; keine zusätzlichen Anfragen ohne Zähler im Test

---

## Phase 16 – Reddit-Sentiment
Ziel: Hype um einzelne Karten früh sehen (Hype-Score als zusätzliches Radar-Signal).

**Aufgaben**
- **Vorher:** Quelle prüfen (offizielle Reddit-API, Nutzungsbedingungen, Rate-Limits) und das
  Ergebnis in `docs/sources.md` festhalten; ohne Freigabe kein Einbau
- Titel aus r/EASportsFC (o. ä.) abrufen, Spielernamen der Watchlist/Radar-Karten zuordnen
- Hype-Score: Erwähnungen pro Tag gegenüber dem eigenen Schnitt; Signal im Radar und als
  Faktor im Potenzial-Score
- Backtest: Hat ein Hype-Anstieg vor Preisanstiegen gelegen?

**Abnahme**
- Zuordnung von Namen mit Fixtures getestet (Spitznamen, gleiche Nachnamen)
- Keine Live-Requests in Tests; Rate-Limit mit Zähler geprüft

---

## Phase 17 – SBC-Lösungsrechner mit eigenem Club
Ziel: günstigste Lösung für eine SBC aus eigenen Karten plus Markt.

**Aufgaben**
- Club-Import per CSV (manueller Export, **kein EA-Login**)
- SBC-Anforderungen erfassen (Rating, Chemie, Ligen/Nationen – Umfang vorab festlegen)
- Rechner: eigene Karten zuerst, fehlende zum Marktpreis; Kosten der Lösung nach aktuellen Preisen
- Seite „SBC“ und `fcast sbc`

**Abnahme**
- Tests mit kleinen, von Hand gelösten SBCs
- Ergebnis nachvollziehbar (welche Karte warum, Gesamtkosten)

---

## Später / Ideen-Backlog
- Zugriff von unterwegs über VPN (WireGuard in der Fritzbox)
- Dashboard-Login: auf dem Server ist `FCAST_WEB_PASSWORD` leer (offen im LAN) – vor VPN-Zugriff
  wieder setzen
