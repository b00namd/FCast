# Preisquellen – Prüfung und Status

Regel aus `CLAUDE.md`: Eine Web-Quelle wird nur eingebaut, wenn robots.txt **und** Nutzungsbedingungen
automatisierten Zugriff erlauben oder eine schriftliche Erlaubnis vorliegt.

Stand der Prüfung: 29.09.2026. Es wurden nur robots.txt- und AGB-Seiten abgerufen, keine Preisdaten.

## Übersicht

| Quelle | Preise FC 27 | Zugang | robots.txt | AGB | Status |
|---|---|---|---|---|---|
| FUT.GG (Stormstrike Inc.) | ja | HTML, interne API | `/api/*` gesperrt | Bots/Scraper verboten, „except where expressly permitted“ (ToS 13.05.2026) | ❌ verboten – Erlaubnis wird angefragt |
| FUTBIN (Better Collective A/S) | ja | HTML | URLs mit `?` gesperrt | „No Scraping or Data Mining“ ohne schriftliche Erlaubnis (ToS 24.02.2026) | ❌ verboten – Erlaubnis wird angefragt |
| FUTNext (FUTNext LTD) | ja (PS/XB) | HTML, Echtzeit nur im Abo | weitgehend erlaubt | kein explizites Scraping-Verbot, aber Kopieren/Spiegeln untersagt (Terms 09.03.2024) | ⚠️ unklar – nur mit Erlaubnis |
| FUTWIZ | ja | HTML, Cloudflare blockt Bots | `Allow: /`, `ai-train=no` | nicht lesbar (403) | ⚠️ unklar |
| FUTDB (futdb.app) | früher API mit Key | Dienst offline (HTTP 520) | – | nicht lesbar | ⚠️ beobachten |
| EasySBC | ja | SPA | fast alles erlaubt | „No Web Scraping, Crawling or Data Mining“ (Terms 14.09.2026) | ❌ verboten |
| FUT Alert | ja | SPA | alles erlaubt | keine AGB gefunden, vermutlich gleicher Betreiber wie EasySBC | ⚠️ unklar, eher nein |
| RenderZ | FC Mobile | SPA/API | `/api/*` gesperrt | – | ❌ ungeeignet |
| EA Ratings / drop-api | nein (nur Stammdaten) | inoffizielles JSON | Rechtevorbehalt gegen Text- und Data-Mining (Art. 4 DSM-RL) | EA User Agreement 14.05.2026 | ❌ verboten ohne schriftliche Freigabe |
| Wrapper (Apify, futbin-sdk, GitHub-Scraper) | – | – | – | übernehmen FUTBIN-/FUTWIZ-Verbote | ❌ verboten |

## Entscheidung und technische Prüfung (29.09.2026)

Der Nutzer hat entschieden, Web-Quellen trotz der AGB abzufragen und das Risiko selbst zu tragen
(siehe `CLAUDE.md`). Der Nutzer spielt auf **PC**. robots.txt, Rate-Limit, ehrlicher User-Agent und „kein
Umgehen von Bot-Schutz“ gelten weiter. Die technische Prüfung ergab:

| Quelle | Ergebnis | Grund |
|---|---|---|
| FUTBIN | ✅ eingebaut | Preis für Konsole und PC, „Price Updated“ und Spielerdaten stehen im HTML der Spielerseite. Karten haben eine eigene FUTBIN-ID plus Namens-Slug (`/27/player/21487/maradona`); der Link wird je Karte gespeichert (`fcast watch add … --futbin <url>`). |
| FUT.GG | ❌ nicht nutzbar | Die Spielerseite enthält nur einen Lade-Platzhalter; der Preis kommt aus `/api/*`, das robots.txt sperrt. |
| FUTWIZ | ❌ nicht nutzbar | Cloudflare-Challenge („Just a moment…“, HTTP 403) für jeden nicht-Browser-Client. |
| FUTNext | ✅ eingebaut (Ersatz) | Preis im HTML, gerundet („4.99M“ ≈ 10.000er-Genauigkeit bei Millionen), keine Aktualisierungszeit. URL nutzt die EA-ID (`/players/<slug>/<ea_id>`, Slug beliebig). PC-Preise nur mit dem Einstellungs-Cookie `settings={"state":{"platform":"pc"},"version":0}` (das setzt auch das Plattform-Menü der Seite); ohne Cookie zeigt die Seite Konsolenpreise – der Parser prüft das Plattform-Label. |

## Schutz vor Sperren

- Antwortet eine Quelle mit HTTP 403/429 oder einer Bot-Challenge, wird sie sofort für
  `FCAST_SOURCE_PAUSE_H` Stunden (Default 24) pausiert – ohne weitere Versuche. Der Status steht in
  der Tabelle `source_status` und übersteht Neustarts. Anzeigen: `fcast sources status`,
  vorzeitig aufheben: `fcast sources resume <name>`.
- Sammelläufe starten mit zufälliger Verzögerung bis `FCAST_COLLECT_JITTER_S` (Default 180 s).
- Standard ist `FCAST_SOURCE_STRATEGY=priority` (FUTBIN zuerst, FUTNext nur als Ersatz), weil
  FUTNext gerundete Preise liefert. `rotate` verteilt stattdessen pro Spieler und Lauf.

## Kontakte für Erlaubnisanfragen

- FUT.GG: business@stormstrike.gg
- FUTBIN: business@futbin.com
- FUTNext: support@futnext.com
- FUTWIZ: (keine Adresse gefunden, AGB hinter Cloudflare)

## Anfragen

| Quelle | Gesendet am | Antwort | Ergebnis |
|---|---|---|---|
| FUT.GG | | | |
| FUTBIN | | | |
| FUTNext | | | |

Eine schriftliche Erlaubnis bitte hier vermerken (Datum, Umfang, Auflagen) und die Mail aufbewahren.
Erst danach wird der jeweilige Adapter gebaut – mit den Auflagen als Konfiguration (z. B. Intervall).

## Aktuell aktive Quelle

`ManualSource` (CSV, `FCAST_MANUAL_CSV`) – siehe Modul-Docstring in `src/fcast/sources/manual.py`.

## Spieldaten für die TOTW-Prognose (Stand 30.09.2026)

| Quelle | Status | Grund |
|---|---|---|
| OpenLigaDB | ✅ eingebaut | Offene Community-API ohne Schlüssel, aktuelle Saison, Tore mit Torschütze. Keine Vorlagen/Noten. |
| API-Football | ⏸ vorbereitet | Konto vorhanden (Schlüssel in `.env` auf dem Server). Free-Tarif nur Saisons 2022–2024; Pro ca. 19 $/Monat. |
| Reddit (TOTW-Prognosen der Community) | ⏸ Freigabe nötig | Seit 2026 „Responsible Builder Policy“: jeder API-Zugang braucht eine Genehmigung (2–4 Wochen). Ohne Login 403. |
| football-data.org | ❌ | Free-Tarif ohne Torschützen. |
| FotMob | ❌ | robots.txt sperrt `/api/*`. |
| SofaScore, ESPN, Kicker | ❌ | Blocken automatisierte Abrufe (403). |
| Understat | ❌ | robots.txt sperrt alles. |

## Leak-Quellen (Stand 30.09.2026)

| Quelle | Status | Grund |
|---|---|---|
| FIFA UTeam RSS (`fifauteam.com/feed/`) | ✅ eingebaut | robots.txt erlaubt alles; RSS ist für Feed-Reader gedacht. |
| RealSport101 RSS (`realsport101.com/feed.xml`) | ✅ eingebaut | robots.txt erlaubt alles; FC-Artikel werden herausgefiltert. |
| FUTBIN News | ⚠️ ungeeignet | News-Sitemap leer, Kategorie „Promo News“ veraltet. |
| FUT.GG News | ⏸ möglich | HTML-Liste, robots.txt erlaubt `/news/`; später ergänzbar. |
| X/Twitter | ❌ | Ohne Login nicht lesbar (402); nur manuell über das Formular. |
| Reddit | ❌ | API nur mit Freigabe (Responsible Builder Policy). |
