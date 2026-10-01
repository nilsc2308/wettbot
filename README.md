# Wettbot

Sucht täglich Value-Wetten im Fußball, nennt die faire Gewinnwahrscheinlichkeit und protokolliert alles als Papierwetten (ohne echtes Geld).

## Wie er rechnet
1. Er holt die Quoten von The Odds API (Region `eu`).
2. Er nimmt die Quoten von Pinnacle (ersatzweise Betfair-Börse) und rechnet die Marge mit der Power-Methode heraus. Ergebnis: die faire Wahrscheinlichkeit.
3. Er vergleicht diese mit den Quoten deiner Buchmacher (`meine_buchmacher` in `config.json`). Wo die Wettsteuer weitergegeben wird, zieht er 5,3 % ab.
4. Edge = Wahrscheinlichkeit × Nettoquote − 1. Ab 2 % wird die Wette gemeldet. Über 15 % gilt sie als verdächtig (meist eine veraltete Quote).
5. Einsatz: ¼ Kelly, höchstens 2 % der Bankroll.

## Start
1. Kostenlosen Key auf https://the-odds-api.com holen (500 Credits pro Monat) und in `.env` eintragen: `ODDS_API_KEY=...`
2. `python3 wettbot.py scan`: zeigt die Value-Wetten und speichert sie unter `reports/DATUM.md`
3. `python3 wettbot.py settle`: rechnet beendete Spiele ab
4. `python3 wettbot.py stats`: Bilanz mit Trefferquote, ROI und CLV
5. Automatisch: GitHub Actions startet `morgen` (10 Uhr) und `abend` (17 Uhr)
6. `python3 wettbot.py demo`: Testlauf mit Beispieldaten

## Sportarten und Kontingent
`alle_sportarten: true` scannt alles, was The Odds API gerade anbietet: Fußball weltweit, Tennis, Basketball, Eishockey, NFL, MMA und mehr. Politik und Langzeitwetten sind ausgeschlossen. `prioritaet` wird zuerst gescannt.
Jeder Wettbewerb kostet pro Markt 1 Credit. Die Liste der Sportarten und die Spielprüfung über `/events` sind gratis. Der Bot verteilt das Restguthaben automatisch auf die restlichen Läufe im Monat (2 geplante und 1 manueller pro Tag, 20 % Reserve für Ergebnisse).
Gratis-Plan (500): etwa 4 Wettbewerbe pro Lauf. Plan mit 20.000 Credits (ca. 30 $/Monat): etwa 170 pro Lauf, also alles.
Ergebnisse, die die API nicht liefert, werden nach 5 Tagen als `unklar` abgehakt und nicht gewertet.

## Bewertung
- **CLV** (Closing Line Value): War deine Quote besser als die faire Schlussquote? Das ist nach etwa 50–100 Wetten aussagekräftig. Dauerhaft positiv heißt, die Edge ist echt.
- **Gewinn/ROI** sagt erst ab etwa 200–500 Wetten etwas aus, vorher ist es vor allem Glück oder Pech.
- In `meine_buchmacher` nur Anbieter eintragen, bei denen du ein Konto hast. Gewinner werden von Buchmachern limitiert.

## App
https://nilsc2308.github.io/wettbot/ (Ordner `docs/`, GitHub Pages). Auf dem iPhone in Safari öffnen, dann Teilen → „Zum Home-Bildschirm“.
Die App liest `docs/daten.json` und `docs/backtest.json`. „Gesetzt?“-Markierungen bleiben nur auf dem Gerät.

## Cloud-Lauf
`.github/workflows/wettbot.yml` startet 10 und 17 Uhr (Sommerzeit). Morgens wird abgerechnet und gescannt, abends nur gescannt (Schlussquoten für den CLV).
Der Key liegt als Secret `ODDS_API_KEY` im Repo. Ohne Secret wird der Lauf übersprungen. Ligen ohne Spiele kosten keine Credits (Prüfung über `/events`).

## Backtest
Historische CSVs von football-data.co.uk nach `data/historie/` laden (`D1_2425.csv` usw.), dann `python3 backtest.py`. Das schreibt `docs/backtest.json`.
Stand 1.10.2026 (9.419 Spiele): Pinnacle-Wahrscheinlichkeiten exakt kalibriert. Favoriten −3,5 %. Value mit einem Buchmacher +20,6 % aus nur 71 Wetten (CLV +0,5 %, also kaum belastbar). Value mit der besten Quote +5,3 % aus 2.406 Wetten (CLV +3,1 %).
