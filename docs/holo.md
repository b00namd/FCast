# Holo-Karten in FC 27 – Recherche und Umsetzung

Stand: 30.09.2026

## Was Holo-Karten sind

- **Seltene Varianten von Spezialkarten**: Kampagnen, TOTW, Heroes, Icons und weitere Spezialkarten
  können seit TOTW 1 als holografische Version aus Packs kommen.
- **Rein kosmetisch**: gleiche Werte wie die normale Karte, der Name steht in Zierschrift statt der Stats.
- **Zwei Stufen**: bis 87 OVR „Holographic“, ab 88 OVR „Pristine“ (auch „Flawless Holographic“).
- **Spielerischer Nutzen**: Holo erhöht den Item Score für Sets in der FUT Gallery und für Streamlined
  SBCs (zusätzlich zum Seltenheits-Bonus) – daher echte Nachfrage.

Quellen: [Dexerto – Holographic & Pristine cards](https://www.dexerto.com/wikis/ea-fc-27/holographic-and-pristine-cards/),
[Killer Fut Trading auf X](https://x.com/KillerFutCoins/status/2099606402360856950).

## Wie FUTBIN Holo-Karten zeigt

- Eigene Spielerseite je Holo-Version, z. B. Olise TOTW `/27/player/22947` (normal) und
  `/27/player/22948` (Holo).
- **Versionsliste** auf jeder Kartenseite (`a.player-card-preview`): Holo-Versionen tragen
  `player-rating-card-holo`.
- **Holo-Seite**: Die Hauptkarte hat die Klasse `playercard-27-holo`.
- **EA-ID**: Holo = EA-ID der normalen Karte + 2²⁴ (Olise: 50579475 → 67356691).
- Beispiel Olise TOTW 91 (30.09.2026): normal 1.080.000 (Konsole) / 1.380.000 (PC),
  Holo 2.499.000 (Konsole, nächstes Angebot 4.999.000), auf PC extinct.
- Nebenbefund: Maradonas zweite FUTBIN-Seite (21489) ist die Holo-Version seiner Icon-Karte.

## Umsetzung in FCast

- **Paarbildung** (`src/fcast/holo.py`): Für Watchlist-Karten mit FUTBIN-Link sucht FCast in der
  Versionsliste eine Holo-Version mit gleichem Rating und prüft sie über die EA-ID (+2²⁴).
  Höchstens 3 Karten pro Sammellauf; ohne Holo wird nach 2 Tagen erneut geprüft
  (Holo-Versionen erscheinen mit dem Release der Karte). Tabelle `card_pairs`.
- **Preise der Holo-Partner** alle `FCAST_HOLO_INTERVAL_H` Stunden (Default 2).
- **Signal `HOLO_SPREAD` („Holo-ÜV“)**: Holo mindestens `FCAST_HOLO_MIN_SPREAD_PCT` (30 %) über der
  normalen Karte → normale Karte eine Preisstufe unter dem Holo-Preis einstellen (gedeckelt durch
  EA-Maximum), Profit nach Steuer. Bei extinct Holo gilt der letzte bekannte Preis (gekennzeichnet).
- **Nur realistische Fälle** (seit 01.10.2026): Abstand höchstens `FCAST_HOLO_MAX_SPREAD_PCT`
  (150 %), normale Karte knapp (weniger als 5 Angebote) und mindestens `FCAST_HOLO_MIN_HISTORY_H`
  (48 h) Preisverlauf. Anlass: Am Tag nach TOTW 3 standen die Holos bei Release-Preisen
  (Son normal 52.500, Holo 750.000) – die Regel hätte „normale Karte zu 500.000 einstellen“
  empfohlen.
- **Hinweis:** Ob sich die normale Karte zum Holo-Preis verkauft, hängt davon ab, dass Käufer die
  Versionen verwechseln oder gezielt suchen. FCast zeigt die Chance, nicht die Wahrscheinlichkeit –
  das Backtesting (Phase 8) soll zeigen, wie oft es klappt.
