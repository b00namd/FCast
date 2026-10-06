# Changelog

Alle nennenswerten Änderungen an FCast. Format angelehnt an [Keep a Changelog](https://keepachangelog.com/de/1.1.0/).

## [Unreleased]

### Behoben
- **SBC- und Objective-Karten (02.10.)** erzeugten bei jedem Sammellauf je Karte zwei
  Warnungen („no price element found“) und setzten FUTBIN/FUTNext auf Fehler, weil die Seiten
  statt eines Marktpreises nur die SBC-Kosten bzw. das Objective zeigen. Beide Parser erkennen
  diese Karten jetzt (`UntradeableError`; FUTBIN über `data-price-box-types`, FUTNext über die
  eingebetteten Kartendaten). Der Collector wertet das wie „extinct“ als Antwort: kein Fehler,
  keine Ersatzanfrage bei FUTNext, eigener Zähler „nicht handelbar“ im Status.
- **ÜV-Einkaufsliste (05.10.):** „Max. Kauf“ war Marktpreis + Aufschlag ohne Rundung und damit
  oft kein gültiger Preis (9.800 + 500 = 10.300). Jetzt auf die Preisstufe abgerundet (10.250),
  der Break-even wird aus dem gerundeten Wert berechnet.

### Geändert
- **Karten für 0 Coins im Portfolio (06.10.):** Karten aus Packs oder Belohnungen lassen sich
  mit Kaufpreis 0 eintragen – im Dashboard, per `fcast portfolio buy <Karte> 0` und im
  Screenshot-JSON. Anders als „unbekannt“ (Kauf nicht erfasst) zählt der Verkauf dann voll als
  Gewinn (Verkaufspreis nach Steuer). Verkaufs- und Einstellpreise müssen weiter > 0 sein.
  Migration `5a3c9e7f1d24` lockert den Check-Constraint auf `buy_price >= 0`.
- **Migrationen auf SQLite** laufen jetzt mit `PRAGMA foreign_keys=OFF`: Beim Neuaufbau einer
  Tabelle (Batch-Migration) hätte das Löschen der alten Tabelle sonst per `ON DELETE CASCADE`
  abhängige Zeilen (z. B. die Einstell-Historie in `portfolio_listings`) mitgelöscht.
- **Suche im Portfolio (05.10.):** Suchfeld über der Positionstabelle filtert nach Name, Rating,
  Kartentyp, EA-ID oder Positionsnummer („#12“); alle Wörter müssen passen. Wirkt zusammen mit
  „Verkaufte mit anzeigen“ und zeigt „x von y Positionen“.
- **Marktpreis je offener Position** im Portfolio: neue Spalten „Markt“ und „Jetzt“ – was ein
  sofortiger Verkauf zum aktuellen Marktpreis nach Steuer brächte – sowie die Summe darüber.
  Damit ist auf einen Blick zu sehen, ob eine offene Position im Plus steht und ob der eigene
  Einstellpreis noch zum Markt passt.
- `tests/test_cli.py`: zu lange Zeile umgebrochen, `ruff check` läuft wieder ohne Befund durch.

- **Dashboard-Login optional:** Ohne gesetztes `FCAST_WEB_PASSWORD` startet das Dashboard jetzt
  ohne Basic Auth, statt den Start zu verweigern – beim Start steht eine Warnung im Log. Mit
  gesetztem Passwort bleibt alles wie bisher. Gedacht für den Betrieb rein im Heimnetz; der
  Schutz gegen Cross-Site-Posts (`SameOriginMiddleware`) bleibt in beiden Fällen aktiv.

### Dashboard: ÜV und Portfolio (zu Phase 12 und 13)
- **Seite „ÜV"** (`/uev`): die Einkaufsliste aus Phase 13 im Browser, nach Bewertung gruppiert,
  mit Filtern für Bewertung, Höchstpreis, Aufschlag und Anzahl je Bewertung. Neu dabei: der
  **meistgenutzte Chemie-Stil** je Karte (aus `players.chem_style`) als farbiger Chip – damit
  steht in der Liste, wonach im Transfermarkt zu suchen ist.
- **Seite „Portfolio"** (`/portfolio`): offene und verkaufte Positionen mit Break-even und
  Gewinn nach Steuer, dazu realisierter Gewinn und gebundenes Kapital. `?show_all=true` zeigt
  auch die verkauften.
- **Eintragen direkt im Dashboard:** Kauf anlegen (EA-ID oder FUT.GG-Link plus Preis),
  je Position „Verkauft“, „Eingestellt“ und „Löschen“. Preise dürfen getippt werden wie man sie
  liest – `4500`, `4.500` oder `45k` (`forms.parse_coins`). Nach dem Absenden wird auf die Seite
  zurückgeleitet (POST/Redirect/GET), Fehler stehen oben auf der Seite.
- Beide Seiten in der Hauptnavigation; die CLI-Befehle (`fcast portfolio …`) bleiben daneben
  bestehen.

### Phase 16 – Gewinn nach Zeitraum & gebundenes Kapital
- **Gewinn heute, diese Woche, gesamt** – nach 5 % Steuer, nach deutscher Zeit, Woche Montag bis
  Sonntag. Wird automatisch aus den erfassten Verkäufen berechnet, auch aus denen per Screenshot.
  Verkäufe ohne erfassten Kaufpreis zählen nicht mit und werden als Anzahl genannt.
- **Gebundenes Kapital über alle offenen Positionen** (unabhängig vom Filter der Ansicht), dazu
  der Marktwert nach Steuer, wenn alles zum Marktpreis verkauft würde; Positionen ohne Kaufpreis
  bzw. ohne Marktpreis werden gesondert gezählt.
- Anzeige: Kasten „Gewinn nach Steuer“ im Portfolio mit aufklappbarer Liste der letzten 14 Tage
  und 8 Wochen; `fcast portfolio profit` (neu); Zusammenfassung unter `fcast portfolio list` und
  in der Vorschau von `fcast portfolio apply`.

### Phase 15 – Eintragen per Screenshot
- **Karten per Name:** `fcast portfolio buy "Musiala 87" 45000` – Wörter vor der Bewertung sind
  der Name, danach der Kartentyp (`"Olise 91 TOTW"`, `"… Holo"`; Kürzel wie TOTW/IF/POTM
  werden übersetzt). Gesucht wird zuerst lokal; bei mehreren Treffern gewinnt die einzige
  Basiskarte, sonst Abbruch mit Liste. Unbekannte Karten sucht FCast über die FUTBIN-Sitemap
  (höchstens 6 Seiten), speichert sie und setzt sie auf die Watchlist (Notiz „Portfolio“),
  damit ihr Preis aktuell bleibt. Das Kauf-Formular im Dashboard nimmt ebenfalls Namen an.
- **`listed` und `sell` nach Karte:** `fcast portfolio sell "Wirtz 86" 30000`; Zahlen bleiben
  Positionsnummern (`17` oder `#17`). Gewählt wird die passende offene Position (gleicher
  Einstellpreis, sonst die älteste).
- **Einstell-Verlauf:** jede (Neu-)Einstellung wird gespeichert (neue Tabelle
  `portfolio_listings`, Migration übernimmt bestehende Einstellungen). Portfolio zeigt
  „N× eingestellt“ mit Preisverlauf und den Hinweis „Markt bei X – senken?“, wenn eine Karte
  mehrfach ohne Verkauf eingestellt war und der Markt darunter liegt.
- **`fcast portfolio apply <datei|->`:** mehrere Einträge als JSON (`buy`/`listed`/`sold`,
  Karte oder Position, Preis auch als „45k“) plus Coinstand. Ohne `--yes` nur Vorschau; ein
  Fehler in einem Eintrag → nichts wird geschrieben.
- **Duplikat-Schutz:** Screenshots zeigen dieselben Einträge, bis die Transferliste geleert
  wird. Übersprungen werden: gleicher Kauf binnen 24 h, unveränderte Einstellung, Verkauf zum
  selben Preis binnen 3 Tagen. `--again` / `"again": true` bucht trotzdem (z. B. Neueinstellung
  nach Ablauf zum gleichen Preis).
- **Coinstand aus dem Screenshot:** wird nach den Einträgen gesetzt; die Einträge zählen dabei
  nicht doppelt. Die Vorschau zeigt den berechneten Stand vorher und nachher.
- **Kaufpreis unbekannt:** Ein Verkauf oder eine Einstellung ohne offene Position legt eine
  Position ohne Kaufpreis an (Pack, Belohnung, vor FCast gekauft). Sie zählt für den Coinstand,
  nicht für Gewinn und gebundenes Kapital.

### Phase 14 – Coinstand & bezahlbare Signale
- **Coinstand im Portfolio:** auf der Seite „Portfolio“ oder mit `fcast portfolio coins <betrag>`
  eintragen. Danach rechnen erfasste Käufe (−Kaufpreis) und Verkäufe (+Erlös nach 5 % Steuer)
  ihn fort; gelöschte Positionen fallen wieder heraus. Belohnungen, Packs und SBCs sieht FCast
  nicht – weicht der Stand vom Spiel ab, einfach neu eintragen. Gespeichert in den
  App-Einstellungen, keine Migration.
- **Nur bezahlbare Pushes:** Kauf-Dip, Promo-Vorkauf, ÜV-Chance und Radar-Treffer, deren Preis
  über dem Coinstand liegt, werden nicht gepusht (`too_expensive` im Alert-Testlauf). Verkaufsziel,
  Holo-ÜV und ÜV-Chancen bei extinct (betreffen eigene Karten) kommen weiter. Ohne Coinstand
  wird nichts gefiltert.
- Seiten „Signale“ und „Radar“ markieren solche Karten als **„zu teuer“**; `fcast portfolio list`
  zeigt den Coinstand mit an.

### Phase 13 – ÜV-Einkaufsliste
- **`fcast uev`** listet die meistgespielten Karten (FUTBIN-Spielzähler), die noch günstig zu
  haben sind – **nach Kartenbewertung gruppiert**, innerhalb einer Bewertung die beliebtesten
  zuerst. Je Karte: Marktpreis, höchster sinnvoller Kaufpreis (`--premium`, Standard 500) und
  der Break-even-Verkaufspreis nach EA-Steuer.
- Optionen: `--max-price` (Standard 60.000), `--premium`, `--limit` (je Bewertung, Standard 5),
  `--rating 86` für eine einzelne Bewertung, `--flat` für eine durchgehende Liste nach
  Beliebtheit statt der Gruppierung.
- Hintergrund: Der gesammelte Marktpreis ist der günstigste Sofortkauf, und diese Angebote
  tragen meist **keinen Chemie-Stil**. Eine Karte mit dem beliebten Stil ist einem Käufer mehr
  wert – gesucht wird also eine solche nahe am Grundpreis. Welche Angebote einen Stil tragen,
  steht nicht in den Preisdaten; dieser Schritt bleibt Handarbeit.

### Phase 12 – Portfolio & Verkaufs-Tracking
- **Käufe und Verkäufe erfassen:** `fcast portfolio buy <ea-id> <preis>`, `listed <id> <preis>`,
  `sell <id> <preis>`, `rm <id>` und `list` (mit `--all` auch die verkauften Positionen).
- **Gewinn nach EA-Steuer:** `buy` nennt sofort den Break-even-Verkaufspreis, `sell` den Gewinn
  bzw. Verlust; `list` zeigt je Position den Break-even und summiert realisierten Gewinn und
  gebundenes Kapital. Gerechnet wird mit `analysis.pricing` (5 % Steuer, gültige Preisstufen).
- Im Repository fehlte nur `remove_position`; die übrigen Portfolio-Funktionen gab es bereits.
- Dashboard und Backtest bleiben unberührt – der Backtest spielt weiterhin Regeln auf den
  gespeicherten Marktpreisen nach, unabhängig von den eigenen Positionen.

### Phase 11 – Kartenbewertung
- **Behoben (01.10.):** „Unterbewertet“ feuerte direkt nach dem Start 8 Pushes auf Basis eines
  noch unbestätigten Spielwerts und einer Preiskurve aus 18 Karten (höheres Rating = billiger).
  Das Signal ist jetzt erst aktiv ab 40 bepreisten Goldkarten, mit plausibler Kurve und wenn der
  Spielwert nachweislich zur Nutzung passt (Übereinstimmung ≥ +0,2, Preis herausgerechnet).
- **Kartenwerte** von der FUTBIN-Spielerseite bei jeder Abfrage (keine Extra-Anfrage):
  Haupt- und Einzelwerte, PlayStyles inkl. PlayStyle+, Skills, schwacher Fuß, Größe,
  Körpertyp, starker Fuß, AcceleRATE (Spalte `players.attributes`, JSON).
- **Spielwert 0–100** je Positionsgruppe (Sturm, Flügel, Spielmacher, Mittelfeld, Sechser,
  Außen-/Innenverteidiger, Torwart): bis 80 Punkte aus gewichteten Werten, bis 20 aus passenden
  PlayStyle+, Skills/schwachem Fuß, AcceleRATE Explosive (Angreifer) und Größe (Innenverteidiger).
- **Meta-Score:** 80 % Spielwert + 20 % wie viel die Karte im Vergleich gespielt wird.
- **Neues Radar-Signal „Unterbewertet“:** Goldkarte mit Spielwert ab 65 und mindestens 40 % unter
  dem Preis, den Goldkarten mit ähnlichem Spielwert und Rating üblicherweise kosten (log-lineare
  Preiskurve über Meta-Score und Rating, Karten ab 2.000 Coins). Spezialkarten und Holo-Versionen
  sind eigene Märkte und werden nicht verglichen.
- **Im Scoring:** ÜV-Score (Gewicht `FCAST_UEV_WEIGHT_META` = 0,10), Promo-Vorkauf
  (Faktor 0,8–1,2), Radar-Potenzial (bis +5) und Warnung beim Kauf-Dip unter Spielwert 40.
- **Anzeige:** Abschnitt „Kartenbewertung“ im Spielerdetail, Spalte in Übersicht und Radar,
  `fcast lage`; neuer Befehl `fcast cards` mit Abgleich Spielwert ↔ Spielzahl
  (Rangkorrelation; Spiele pro Tag, solange die fehlen nur Goldkarten mit Spielen gesamt –
  neue Spezialkarten hatten weniger Zeit, gespielt zu werden).

### Phase 10 – Potenzial-Radar
- **Markt-Scanner:** liest alle 12 h FUTBIN Popular, New Players und das aktuelle TOTW
  (je bis `FCAST_RADAR_LIST_LIMIT` = 150 Karten), löst pro Lauf bis zu 5 neue Karten auf und
  bepreist bis zu `FCAST_RADAR_PER_RUN` = 15 Radar-Karten (je Karte etwa alle 6 h) – ohne sie auf
  die Watchlist zu setzen. `FCAST_RADAR_PER_RUN=0` schaltet den Scanner ab.
- **Verlauf der Spielzahl** (FUTBIN „Games“) für alle bepreisten Karten.
- **Futter-Index:** alle 6 h der Ø der drei günstigsten Karten je Rating 82–90 (FUTBIN
  „Cheapest Players“).
- **Frühsignale:** Trend-Start (stetiger Anstieg ≥ 5 % in 12 h, noch kein Sprung über 30 %),
  Angebot schrumpft (z. B. 5 → 2 Angebote in 24 h bei haltendem Preis), Nutzung steigt
  (Spiele/Tag mindestens 1,5-mal so viele wie am Vortag und über dem Schnitt), Futter zieht an
  (+8 % in 24 h). Potenzial = stärkstes Signal + 5 je weiterem.
- **Seite „Radar“** mit Treffern, Begründung, „Beobachten“-Knopf und Futter-Index; Push ab
  Potenzial 70 (höchstens einmal am Tag pro Karte/Rating, unter Alerts abschaltbar);
  Abschnitte „Radar“ und „Futter-Index“ in `fcast lage`.
- **Backtest:** neue Regel Trend-Start (`--rule TREND_START`, Sweep z. B. `trend_target_pct`).
- Portfolio/Verkaufs-Tracking zurückgestellt (Backlog).

### Phase 9 – Betrieb & bessere Signale
- **Abfrage-Takt pro Karte:** jede Runde, alle 2/6/12 h (Watchlist „Bearbeiten“ oder
  `fcast watch interval <ea_id> <minuten>`); der Collector überspringt Karten, die noch nicht dran
  sind, und zeigt das auf der Status-Seite. Neu: `fcast watch remove`.
- **Tägliches Backup** um `FCAST_BACKUP_TIME` (03:30) per SQLite-Online-Backup, 14 Stück
  aufbewahrt (`FCAST_BACKUP_KEEP`); im Docker-Setup auf dem Host unter `./backups`;
  `fcast db backup` für sofort; Status-Seite zeigt das letzte Backup.
- **Verlauf der Angebotslage** (`market_observations`): „weniger als 5 Angebote seit …“ im
  Spielerdetail und in `fcast lage`; hält das dünne Angebot mindestens 6 h an, steigt der
  ÜV-Score (bis +0,2 auf den Angebots-Anteil) und es steht als Grund dabei. Spielerdetail zeigt
  jetzt auch die Gründe des ÜV-Scores.
- **ÜV im Backtest** (`--rule OVERPRICE_CHANCE`): Kauf zum Marktpreis, Einstellen zum ÜV-Preis,
  verkauft sobald das günstigste Angebot den ÜV-Preis erreicht; Sweep über `uev_threshold` u. a.
- **ÜV-Chance mit Mindestprofit** wie beim Kauf-Dip (keine 12-Coins-Signale mehr).

### Behoben
- **Holo-ÜV nur noch realistisch:** höchstens +150 % Abstand (`FCAST_HOLO_MAX_SPREAD_PCT`),
  normale Karte knapp (< 5 Angebote), mindestens 48 h Preisverlauf (`FCAST_HOLO_MIN_HISTORY_H`).
  Vorher meldete FCast bei frischen TOTW-Karten z. B. „normale Son-Karte (52.500) zu 500.000
  einstellen“.
- **Kauf-Dip bei neuen Karten:** Ein frisch erschienenes TOTW fällt in den ersten Stunden stark;
  der „Ø 7 Tage“ bestand dann nur aus den Release-Preisen und löste falsche Kauf-Alerts aus
  (z. B. Gyökeres bei 122.000, danach 86.000). `BUY_DIP` braucht jetzt mindestens 3 Tage
  Preisverlauf (`WindowStats.span`).

### Marktlage auf Zuruf
- CLI `fcast lage [--json]`: alles für eine Analyse in einem Rutsch – je Karte Preis, Alter,
  24-h-Änderung, Abstand zum Ø 7 Tage, Spanne, Angebote/Lücke/Luft, ÜV-Score, Holo-Abstand,
  günstigste Tageszeit (ab 3 Tagen Daten) bzw. Wochentag (ab 14 Tagen), Signale, Notiz; dazu
  Marktstimmung, Promos mit Kandidaten, TOTW-Prognose, Alerts der letzten 24 h, Quellenstatus und
  Kauf-Dip-Backtest der letzten 30 Tage. Grundlage für Analysen und Tipps auf Nachfrage.

### Phase 8 – Backtesting & Lernen aus Promos
- **Engine** (`fcast.backtest`): spielt `BUY_DIP` und `PROMO_PREBUY` auf den gespeicherten Preisen
  nach – ohne Blick in die Zukunft, Kauf zum Snapshot-Preis, Verkauf am Ziel (Ø 7 Tage), nach
  Haltedauer/Stop-Loss bzw. zum Promo-Ausstieg eine Stufe unter Markt, 5 % Steuer.
- **Kennzahlen:** Trefferquote, Profit gesamt und pro Trade, Max-Drawdown, Kapitalbindung (Spitze),
  Ø Haltedauer, offene Positionen zum Marktwert.
- **Parameter-Sweep** (`dip_pct`, `min_margin_pct`, `max_hold_h`, `stop_loss_pct`, `threshold`,
  `entry_days`, `exit_h`), bester Wert markiert.
- **Promo- und TOTW-Verläufe:** Ø Preisänderung der verknüpften Karten bzw. der TOTW-Kandidaten
  (getrennt nach „im TOTW“/„nicht im TOTW“) von T-3 bis T+2 Tage.
- Die besten 5 verknüpften TOTW-Kandidaten werden bis 3 Tage nach dem Release im Pool bepreist
  (`FCAST_TOTW_POOL_TOP`), damit Verläufe entstehen.
- **Reproduzierbar:** Fingerprint über alle Eingangsdaten; gleiche Daten → gleiches Ergebnis.
- CLI `fcast backtest --rule … --from … --to … [--sweep dip_pct=5,10,15] [--hold 48] [--curves]`,
  Dashboard-Seite **Backtest** mit Diagramm der Verläufe.
- Standardabweichung wird mit Gleitkomma statt `statistics.pstdev` berechnet (Backtest ~2× schneller).

### Phase 7 – Holo-Paare (TOTW-Prognose siehe unten)
- Recherche zu Holo-/Pristine-Karten in `docs/holo.md`; Fixture der Olise-TOTW-Holo-Seite.
- FUTBIN: Versionsliste und Holo-Erkennung (`parse_versions`, `is_holo_page`); Holo-EA-ID = normale
  EA-ID + 2²⁴.
- Paarbildung für Watchlist-Karten (Tabelle `card_pairs`), Holo-Partner werden alle 2 h bepreist.
- Signal `HOLO_SPREAD` („Holo-ÜV“): normale Karte knapp unter dem Holo-Preis einstellen, Profit nach
  Steuer; in Signalen, Alerts (abschaltbar) und im Spielerdetail (Abschnitt „Holo-Version“).

### Phase 6 – Leak- & Promo-Radar
- **Leak-Eingang:** RSS-Feeds von FIFA UTeam und RealSport101 (robots.txt erlaubt, Feeds sind für
  Reader gedacht), alle 6 h; nur FC-/Ultimate-Team-Artikel mit Promo-/Leak-Bezug, Leaks markiert.
  XML wird mit `defusedxml` gelesen. FUTBIN-News geprüft: Sitemap leer, Promo-News veraltet.
  Reddit bewusst weggelassen (API nur mit Freigabe).
- **Erkennung** in Leak-Texten: Spieler über die FUTBIN-Sitemap (volle Namen und eindeutige
  Nachnamen wie „Ødegaard“), Ligen, Nationen und Vereine über Alias-Listen; Umschrift für ø/æ/ł …
- **Promos** im Dashboard erfassen (Text analysieren → Vorschläge bestätigen) oder per CLI
  (`fcast promo add|list`); Löschen im Dashboard.
- **Kandidaten-Pool:** Basiskarten geleakter Spieler werden über FUTBIN gefunden und bis 3 Tage
  nach Promo-Ende alle 12 h bepreist (höchstens 4 Karten pro Sammellauf).
- **Link-Scoring:** Spieler > Verein > Liga > Nation, Zeit bis Start (optimal 2–7 Tage),
  Konfidenz, Preis vs. Ø 7 Tage, Aktivität; Gewichte in der Config (`FCAST_PROMO_*`).
- **Signal `PROMO_PREBUY`** („Promo-Vorkauf“) in Signalen, Alerts (abschaltbar) und auf der neuen
  Seite **Promos** (Kandidaten, Promos, Leak-Eingang).

### TOTW-Prognose (Teil von Phase 7, vorgezogen)
- **Daten:** OpenLigaDB (offen, ohne Schlüssel) für 1., 2. und 3. Liga – Spiele, Ergebnis, Tore mit
  Torschütze, Elfmeter, Eigentor. Gespeichert in `real_matches`. API-Football geprüft: kostenloser
  Tarif nur Saisons 2022–2024, aktuelle Saison erst mit Pro (ca. 19 $/Monat); Reddit-API braucht
  seit 2026 eine Freigabe. Beides später anschließbar.
- **TOTW-Wochen:** Release mittwochs 19:00 (konfigurierbar), Fenster = Spiele seit dem letzten
  Release; Nummerierung wie bei FUTBIN.
- **Score:** Tore (Elfer schwächer), Doppelpack/Hattrick-Bonus, Sieg, Liga-Faktor; Gewichte in der
  Config (`FCAST_TOTW_*`). Begründung pro Kandidat („3 Tore · Hattrick · Sieg“).
- **FC-Karte:** Für die Top-Kandidaten sucht FCast über die FUTBIN-Sitemap die Basiskarte (Name +
  Verein müssen passen), zeigt Preis und Chemstyles; Zuordnung wird zwischengespeichert.
- **Auswertung:** Nach dem Release liest FCast das echte TOTW von FUTBIN (`/27/totw/TOTWn`,
  Tabelle `totw_actuals`) und zeigt die Trefferquote der Top 10.
- **Dashboard:** neue Seite **TOTW** (Kandidaten, Preise, „Beobachten“, Rückblick), Aktualisierung
  alle 6 h und per Button. **Alert** mit den Top 5 einmal pro Woche bis 20 h vor dem Release
  (abschaltbar unter Alerts).
- Test mit echten Daten: Für TOTW 2 lag Olise (Hattrick) auf Platz 1 und war im TOTW.

### Nach Phase 5 – Watchlist-Komfort & Chemstyle
- **FUTBIN-Link wird automatisch gesucht**, wenn nur EA-ID oder FUT.GG-Link bekannt ist: FCast liest
  FUTBINs Spieler-Sitemaps (einmal täglich, 3 Dateien), wählt Kandidaten über den Namen (Slug) und
  prüft höchstens 6 Seiten, bis das Kartenbild die EA-ID zeigt. Läuft im Hintergrund nach dem
  Hinzufügen und vor jedem Sammellauf für bis zu 3 Karten; erfolglose Suchen erst nach einem Tag
  erneut, keine Suche während FUTBIN pausiert ist.
- **FUTBIN-Link reicht:** Die EA-ID wird aus dem Kartenbild der FUTBIN-Seite gelesen
  (`p50579475.png` = Karten-ID bei Sonderkarten, `190042.png` = Spieler-ID = Karten-ID bei
  Basiskarten). Alternativ akzeptiert das Feld eine Zahl oder einen FUT.GG-/FUTNext-Link.
  Passen EA-ID und FUTBIN-Link nicht zur selben Karte, gibt es einen Fehler. Ist FUTBIN
  pausiert, wird nicht nachgefragt.
- **Chemstyles:** die drei beliebtesten Chemstyles der FUTBIN-Community mit Anteil (z. B.
  „Hunter 77 % · Artist 8 % · Engine 8 %“), plattformunabhängig; Anzeige in Übersicht, Watchlist
  und Spielerdetail. Der Satz „best chemistry style“ aus dem Profiltext dient nur noch als Fallback.
- **Nutzung:** gespielte Spiele und Tore/Spiel laut FUTBIN
  für die eingestellte Plattform (neue Spalten in `players`). Aktualisierung bei jedem Lauf
  aus der bereits geladenen Seite (keine zusätzlichen Anfragen). Anzeige in Watchlist und
  Spielerdetail.
- Kartentyp aus dem FUTBIN-Profiltext („Base Icon“, „Team of the Week“ …).
- Tests: globaler Schutz gegen echte HTTP-Anfragen (`tests/conftest.py`); Web-Tests nutzen eine
  FUTBIN-Quelle mit gespeicherten Seiten.
- Fix: Spieler auf der Watchlist ließen sich nicht löschen (Cascade fehlte).

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
