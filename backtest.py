#!/usr/bin/env python3
"""Historischer Test der Methode mit echten Quoten und Ergebnissen (football-data.co.uk).

Fragen:
  1. Stimmen die fairen Pinnacle-Wahrscheinlichkeiten? (Kalibrierung: vorhergesagt vs. tatsaechlich)
  2. Haette die Value-Methode Geld gebracht?
     - mit EINEM Buchmacher (Bet365 als Stellvertreter fuer Winamax, beide ohne Steuerabzug)
     - mit der besten Quote aller Buchmacher (Obergrenze)
  3. Zum Vergleich: Was bringen "offensichtliche" Strategien (immer auf den Favoriten)?

Aufruf: python3 backtest.py   (Daten vorher nach data/historie/ laden, siehe README)
"""
import csv
import glob
import json
import os

from wettbot import entmargen

BASIS = os.path.dirname(os.path.abspath(__file__))
LIGEN = {"D1": "Bundesliga", "E0": "Premier League", "SP1": "La Liga", "I1": "Serie A"}
AUSGAENGE = ("H", "D", "A")


def zahl(x):
    try:
        v = float(x)
        return v if v > 1 else None
    except (TypeError, ValueError):
        return None


def lade_spiele():
    spiele = []
    for pfad in sorted(glob.glob(os.path.join(BASIS, "data", "historie", "*.csv"))):
        liga, saison = os.path.basename(pfad)[:-4].split("_")
        with open(pfad, encoding="latin-1") as f:
            for z in csv.DictReader(f):
                if z.get("FTR") not in AUSGAENGE:
                    continue
                ps = [zahl(z.get("PS" + a)) for a in AUSGAENGE]
                psc = [zahl(z.get("PSC" + a)) for a in AUSGAENGE]
                b365 = [zahl(z.get("B365" + a)) for a in AUSGAENGE]
                mx = [zahl(z.get("Max" + a)) for a in AUSGAENGE]
                if None in ps or None in b365:
                    continue
                spiele.append({
                    "liga": LIGEN[liga], "saison": "20%s/%s" % (saison[:2], saison[2:]),
                    "ergebnis": z["FTR"], "fair": entmargen(ps),
                    "schluss": entmargen(psc) if None not in psc else None,
                    "b365": b365, "max": mx if None not in mx else None,
                })
    return spiele


def kalibrierung(spiele):
    toepfe = {}
    for s in spiele:
        for i, a in enumerate(AUSGAENGE):
            p = s["fair"][i]
            t = min(int(p * 10), 9)
            e = toepfe.setdefault(t, [0, 0.0, 0])
            e[0] += 1
            e[1] += p
            e[2] += 1 if s["ergebnis"] == a else 0
    return [{"bereich": "%d–%d %%" % (t * 10, t * 10 + 10), "anzahl": n,
             "vorhergesagt": round(sp / n, 4), "eingetreten": round(tr / n, 4)}
            for t, (n, sp, tr) in sorted(toepfe.items()) if n >= 30]


def strategie(spiele, quelle, min_edge, min_q=1.3, max_q=6.0):
    """Flach 1 Einheit pro Wette, wo faire Pinnacle-Wahrscheinlichkeit x Quote - 1 >= min_edge."""
    wetten = []
    for s in spiele:
        quoten = s[quelle]
        if not quoten:
            continue
        for i, a in enumerate(AUSGAENGE):
            q = quoten[i]
            if not (min_q <= q <= max_q):
                continue
            edge = s["fair"][i] * q - 1
            if edge >= min_edge:
                wetten.append({"saison": s["saison"], "liga": s["liga"], "p": s["fair"][i], "q": q,
                               "gewonnen": s["ergebnis"] == a,
                               "clv": q * s["schluss"][i] - 1 if s["schluss"] else None})
    return auswerten(wetten)


def favorit(spiele):
    wetten = []
    for s in spiele:
        i = max(range(3), key=lambda k: s["fair"][k])
        wetten.append({"saison": s["saison"], "liga": s["liga"], "p": s["fair"][i], "q": s["b365"][i],
                       "gewonnen": s["ergebnis"] == AUSGAENGE[i], "clv": None})
    return auswerten(wetten)


def auswerten(wetten):
    if not wetten:
        return {"wetten": 0}
    gewinn = sum((w["q"] - 1) if w["gewonnen"] else -1 for w in wetten)
    clvs = [w["clv"] for w in wetten if w["clv"] is not None]
    saisons = {}
    for w in wetten:
        s = saisons.setdefault(w["saison"], [0, 0.0])
        s[0] += 1
        s[1] += (w["q"] - 1) if w["gewonnen"] else -1
    return {
        "wetten": len(wetten),
        "treffer": round(sum(w["gewonnen"] for w in wetten) / len(wetten), 4),
        "erwartet": round(sum(w["p"] for w in wetten) / len(wetten), 4),
        "quote_schnitt": round(sum(w["q"] for w in wetten) / len(wetten), 2),
        "gewinn": round(gewinn, 1),
        "roi": round(gewinn / len(wetten), 4),
        "clv": round(sum(clvs) / len(clvs), 4) if clvs else None,
        "saisons": [{"saison": k, "wetten": v[0], "roi": round(v[1] / v[0], 4)} for k, v in sorted(saisons.items())],
    }


def main():
    spiele = lade_spiele()
    if not spiele:
        raise SystemExit("Keine Daten in data/historie/.")
    saisons = sorted({s["saison"] for s in spiele})
    ergebnis = {
        "spiele": len(spiele),
        "zeitraum": "%s bis %s" % (saisons[0], saisons[-1]),
        "ligen": sorted({s["liga"] for s in spiele}),
        "kalibrierung": kalibrierung(spiele),
        "strategien": [
            dict(name="Immer auf den Favoriten (Bet365)", art="vergleich", **favorit(spiele)),
            dict(name="Value ab 2 % – ein Buchmacher (Bet365)", art="value", **strategie(spiele, "b365", 0.02)),
            dict(name="Value ab 2 % – beste Quote aller Buchmacher", art="obergrenze", **strategie(spiele, "max", 0.02)),
        ],
    }
    with open(os.path.join(BASIS, "docs", "backtest.json"), "w", encoding="utf-8") as f:
        json.dump(ergebnis, f, ensure_ascii=False, indent=1)

    print("%d Spiele, %s, %s\n" % (len(spiele), ergebnis["zeitraum"], ", ".join(ergebnis["ligen"])))
    print("Kalibrierung (stimmen die Wahrscheinlichkeiten?)")
    for k in ergebnis["kalibrierung"]:
        print("  %-9s %6d Tipps: vorhergesagt %5.1f %%, eingetreten %5.1f %%" %
              (k["bereich"], k["anzahl"], k["vorhergesagt"] * 100, k["eingetreten"] * 100))
    print("\nStrategien (1 € pro Wette)")
    for s in ergebnis["strategien"]:
        if not s["wetten"]:
            print("  %s: keine Wetten" % s["name"])
            continue
        print("  %-46s %5d Wetten | Treffer %4.1f %% (erwartet %4.1f %%) | ROI %+5.1f %% | CLV %s" % (
            s["name"], s["wetten"], s["treffer"] * 100, s["erwartet"] * 100, s["roi"] * 100,
            "%+.1f %%" % (s["clv"] * 100) if s["clv"] is not None else "–"))
        print("      je Saison: " + ", ".join("%s %+.1f %% (%d)" % (x["saison"], x["roi"] * 100, x["wetten"])
                                          for x in s["saisons"]))


if __name__ == "__main__":
    main()
