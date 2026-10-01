#!/usr/bin/env python3
"""Wettbot: sucht Value-Wetten, protokolliert sie als Papierwetten und misst, ob die Edge echt ist.

Prinzip: Die Quoten von Pinnacle (bzw. Betfair-Boerse) gelten als die genauesten am Markt.
Daraus wird die Buchmacher-Marge herausgerechnet -> faire Gewinnwahrscheinlichkeit.
Bietet einer deiner Buchmacher (nach Wettsteuer) eine hoehere Quote als fair, ist die Wette +EV.

Befehle:
  python3 wettbot.py scan      Quoten holen, Value-Wetten zeigen und als Papierwetten speichern
  python3 wettbot.py settle    Ergebnisse holen und offene Papierwetten abrechnen
  python3 wettbot.py stats     Bilanz: Trefferquote, Gewinn, ROI, CLV
  python3 wettbot.py morgen    settle + scan + export (Cloud-Lauf morgens)
  python3 wettbot.py abend     scan + export (Cloud-Lauf abends, misst Schlussquoten)
  python3 wettbot.py manuell   wie abend, gestartet per Knopf in der App
  python3 wettbot.py export    schreibt docs/daten.json fuer die App
  python3 wettbot.py demo      Testlauf mit Beispieldaten, ohne API-Key
"""
import json
import os
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

BASIS = os.path.dirname(os.path.abspath(__file__))
GRUPPEN = {
    "Soccer": "Fußball", "Tennis": "Tennis", "Basketball": "Basketball", "Ice Hockey": "Eishockey",
    "American Football": "American Football", "Baseball": "Baseball", "Mixed Martial Arts": "MMA",
    "Boxing": "Boxen", "Cricket": "Cricket", "Rugby League": "Rugby", "Rugby Union": "Rugby",
    "Aussie Rules": "Australian Football", "Handball": "Handball", "Lacrosse": "Lacrosse", "Golf": "Golf",
    "Volleyball": "Volleyball", "Darts": "Darts", "Table Tennis": "Tischtennis", "Snooker": "Snooker",
}
API = "https://api.the-odds-api.com/v4"
DEMO = False


# ---------- Einstellungen ----------

def lade_config():
    with open(os.path.join(BASIS, "config.json"), encoding="utf-8") as f:
        return json.load(f)


def api_key():
    key = os.environ.get("ODDS_API_KEY", "").strip()
    pfad = os.path.join(BASIS, ".env")
    if not key and os.path.exists(pfad):
        with open(pfad, encoding="utf-8") as f:
            for zeile in f:
                if zeile.startswith("ODDS_API_KEY="):
                    key = zeile.split("=", 1)[1].strip()
    if not key:
        sys.exit("Kein API-Key. Kostenlos holen auf https://the-odds-api.com und in .env eintragen: ODDS_API_KEY=...")
    return key


def db():
    name = "demo.db" if DEMO else "wetten.db"
    con = sqlite3.connect(os.path.join(BASIS, "data", name))
    con.row_factory = sqlite3.Row
    con.execute("""CREATE TABLE IF NOT EXISTS wetten (
        id INTEGER PRIMARY KEY,
        erstellt TEXT, event_id TEXT, liga TEXT, liga_key TEXT, anstoss TEXT,
        heim TEXT, gast TEXT, markt TEXT, tipp TEXT, linie REAL,
        buchmacher TEXT, quote REAL, quote_netto REAL,
        fair_prob REAL, edge REAL, einsatz REAL,
        schluss_prob REAL, status TEXT DEFAULT 'offen', gewinn REAL,
        UNIQUE(event_id, markt, tipp, linie))""")
    spalten = {r[1] for r in con.execute("PRAGMA table_info(wetten)")}
    for name, typ in (("akt_quote", "REAL"), ("akt_edge", "REAL"), ("akt_zeit", "TEXT"), ("analyse", "TEXT"),
                      ("gruppe", "TEXT")):
        if name not in spalten:
            con.execute("ALTER TABLE wetten ADD COLUMN %s %s" % (name, typ))
    con.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
    return con


def meta_setzen(con, k, v):
    con.execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)", (k, str(v)))


# ---------- API ----------

def hole(pfad, params):
    params = dict(params, apiKey=api_key())
    url = "%s%s?%s" % (API, pfad, urllib.parse.urlencode(params))
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            rest = r.headers.get("x-requests-remaining")
            return json.loads(r.read().decode()), rest
    except urllib.error.HTTPError as e:
        text = e.read().decode(errors="replace")
        if e.code == 401:
            sys.exit("API-Key ungueltig oder Kontingent aufgebraucht: " + text)
        if e.code == 429:
            sys.exit("Zu viele Anfragen (429). Spaeter erneut versuchen.")
        print("  Fehler %s bei %s: %s" % (e.code, pfad, text[:200]))
        return [], None


def sportarten(cfg):
    """Alle aktiven Wettbewerbe (der /sports-Endpunkt kostet nichts), Prioritaetsliste zuerst.
    Gibt [(key, name, gruppe)] und die restlichen Credits zurueck."""
    prio = cfg["prioritaet"]
    if DEMO or not cfg.get("alle_sportarten"):
        return [(k, v, "Fußball") for k, v in prio.items()], None
    liste, rest = hole("/sports", {})
    rang = {k: i for i, k in enumerate(prio)}
    auswahl = []
    for sp in liste:
        if sp.get("has_outrights") or not sp.get("active", True):
            continue
        if sp["group"] in cfg.get("ausschliessen_gruppen", []) or sp["key"] in cfg.get("ausschliessen", []):
            continue
        gruppe = GRUPPEN.get(sp["group"], sp["group"])
        name = prio.get(sp["key"], sp["title"])
        auswahl.append((sp["key"], name, gruppe))
    auswahl.sort(key=lambda x: (rang.get(x[0], len(rang)), x[2] != "Fußball", x[2], x[1]))
    return auswahl, rest


def lauf_budget(cfg, rest):
    """Verteilt die restlichen Credits gleichmaessig auf die restlichen Laeufe des Monats.
    20 % bleiben fuer die Ergebnisabfragen uebrig."""
    if rest is None:
        return None
    heute = datetime.now()
    naechster = (heute.replace(day=28) + timedelta(days=4)).replace(day=1)
    tage = (naechster.date() - heute.date()).days
    laeufe = max(1, tage * (cfg["laeufe_pro_tag"] + cfg["puffer_laeufe_pro_tag"]))
    frei = max(0, int(rest) - cfg["reserve_credits"])
    if frei < len(cfg["maerkte"]):
        return 0
    return max(len(cfg["maerkte"]), int(frei * 0.8 / laeufe))


def spielplan(cfg, liga_key):
    """Der /events-Endpunkt kostet keine Credits. Gibt (Spiele in 12 h, Spiele in 48 h) zurueck,
    damit Credits nur fuer Wettbewerbe ausgegeben werden, in denen bald gespielt wird."""
    if DEMO:
        return 1, 1
    jetzt = datetime.now(timezone.utc)
    bis = jetzt + timedelta(hours=cfg.get("stunden_bis_anstoss", 48))
    events, _ = hole("/sports/%s/events" % liga_key, {
        "commenceTimeFrom": jetzt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "commenceTimeTo": bis.strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    bald = (jetzt + timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return sum(1 for e in events if e["commence_time"] <= bald), len(events)


def hole_quoten(cfg, liga_key):
    if DEMO:
        with open(os.path.join(BASIS, "fixtures", "beispiel_quoten.json"), encoding="utf-8") as f:
            return [e for e in json.load(f) if e["sport_key"] == liga_key], "demo"
    jetzt = datetime.now(timezone.utc)
    bis = jetzt + timedelta(days=cfg["tage_voraus"])
    return hole("/sports/%s/odds" % liga_key, {
        "regions": cfg["region"],
        "markets": ",".join(cfg["maerkte"]),
        "oddsFormat": "decimal",
        "commenceTimeFrom": jetzt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "commenceTimeTo": bis.strftime("%Y-%m-%dT%H:%M:%SZ"),
    })


# ---------- Mathematik ----------

def entmargen(quoten):
    """Power-Methode: findet k mit sum((1/q)^k) = 1. Korrigiert den Favoriten-Aussenseiter-Effekt
    besser als einfaches Normieren. Gibt faire Wahrscheinlichkeiten zurueck."""
    roh = [1.0 / q for q in quoten]
    lo, hi = 0.5, 3.0
    for _ in range(100):
        k = (lo + hi) / 2
        s = sum(p ** k for p in roh)
        if s > 1:
            lo = k
        else:
            hi = k
    return [p ** k for p in roh]


def netto_quote(cfg, buch, quote):
    """Wettsteuer (5,3 % auf den Einsatz) senkt die effektive Quote, wenn der Buchmacher sie weitergibt."""
    if cfg["meine_buchmacher"].get(buch, {}).get("steuer_weitergegeben", True):
        return quote * (1 - cfg["wettsteuer"])
    return quote


def kelly_einsatz(cfg, p, q_netto):
    f = (p * q_netto - 1) / (q_netto - 1)
    f = max(0.0, f) * cfg["kelly_anteil"]
    f = min(f, cfg["max_einsatz_anteil"])
    return round(cfg["bankroll"] * f, 2)


def marktgruppen(markt):
    """Teilt einen Markt in vollstaendige Ausgaenge auf (bei Totals je Linie, z. B. Ueber/Unter 2,5)."""
    gruppen = {}
    for o in markt["outcomes"]:
        gruppen.setdefault(o.get("point"), {})[o["name"]] = o["price"]
    return gruppen


def plausibel(preise, untergrenze=0.995, obergrenze=1.12):
    """Summe der Kehrwerte = Buchmacher-Marge. Unter 1 heisst veraltete oder duenne Quoten
    (typisch fuer illiquide Boersenmaerkte), deutlich ueber 1 eine zu grobe Quelle."""
    return untergrenze <= sum(1.0 / q for q in preise.values()) <= obergrenze


def faire_preise(cfg, event):
    """{(markt, linie, ausgaenge): {tipp: faire_prob}} aus dem ersten verfuegbaren scharfen Buchmacher.
    Die Ausgaenge gehoeren zum Schluessel, damit z. B. Eishockey mit Verlaengerung (2 Ausgaenge)
    nie mit 60-Minuten-Wetten (3 Ausgaenge mit Unentschieden) verglichen wird."""
    buecher = {b["key"]: b for b in event.get("bookmakers", [])}
    ergebnis = {}
    for scharf in cfg["scharfe_buchmacher"]:
        if scharf not in buecher:
            continue
        for m in buecher[scharf]["markets"]:
            for linie, preise in marktgruppen(m).items():
                schluessel = (m["key"], linie, frozenset(preise))
                if schluessel in ergebnis or len(preise) < 2 or not plausibel(preise):
                    continue
                namen = list(preise)
                probs = entmargen([preise[n] for n in namen])
                ergebnis[schluessel] = {"quelle": scharf, "probs": dict(zip(namen, probs))}
    return ergebnis


def quellen_vergleich(cfg, event):
    """Faire Wahrscheinlichkeiten je Quelle: jeder scharfe Buchmacher einzeln plus der Schnitt
    aller uebrigen Buchmacher (ohne die eigenen). Dient als Gegenprobe zur Hauptquelle."""
    ergebnis = {}
    for b in event.get("bookmakers", []):
        for m in b["markets"]:
            for linie, preise in marktgruppen(m).items():
                if len(preise) < 2 or not plausibel(preise, obergrenze=1.15):
                    continue
                namen = list(preise)
                probs = dict(zip(namen, entmargen([preise[n] for n in namen])))
                eintrag = ergebnis.setdefault((m["key"], linie, frozenset(preise)), {"markt_summe": {}, "n": 0})
                if b["key"] in cfg["scharfe_buchmacher"]:
                    eintrag[b["key"]] = probs
                if b["key"] not in cfg["meine_buchmacher"]:
                    eintrag["n"] += 1
                    for n, pr in probs.items():
                        eintrag["markt_summe"][n] = eintrag["markt_summe"].get(n, 0) + pr
    for eintrag in ergebnis.values():
        n = eintrag.pop("n")
        summe = eintrag.pop("markt_summe")
        eintrag["markt"] = {k: v / n for k, v in summe.items()} if n else {}
        eintrag["anzahl"] = n
    return ergebnis


def analyse(cfg, quellen, tipp, qn, hauptquelle):
    """Gegenprobe: Haelt die Edge auch gegen die anderen Quellen? -> Sicherheit hoch/mittel/niedrig."""
    a = {"anzahl_buecher": quellen.get("anzahl", 0)}
    for key, name in (("pinnacle", "pinnacle"), ("betfair_ex_eu", "betfair"), ("markt", "markt")):
        p = quellen.get(key, {}).get(tipp)
        if p is not None:
            a[name] = round(p, 4)
    hauptname = {"pinnacle": "pinnacle", "betfair_ex_eu": "betfair"}.get(hauptquelle, hauptquelle)
    andere = [a[n] for n in ("pinnacle", "betfair", "markt") if n in a and n != hauptname]
    positiv = sum(1 for p in andere if p * qn > 1)
    if len(andere) >= 2 and positiv == len(andere) and a["anzahl_buecher"] >= 8:
        a["sicherheit"] = "hoch"
    elif positiv >= 1:
        a["sicherheit"] = "mittel"
    else:
        a["sicherheit"] = "niedrig"
    a["hauptquelle"] = hauptname
    return a


def finde_value(cfg, event, liga_name, gruppe="Fußball"):
    fair = faire_preise(cfg, event)
    vergleich = quellen_vergleich(cfg, event)
    kandidaten = []
    for b in event.get("bookmakers", []):
        if b["key"] not in cfg["meine_buchmacher"]:
            continue
        for m in b["markets"]:
            for linie, preise in marktgruppen(m).items():
                schluessel = (m["key"], linie, frozenset(preise))
                ref = fair.get(schluessel)
                if not ref:
                    continue
                # 3-Wege-Siegwetten ausserhalb von Fussball/Handball gelten nur fuer die regulaere Spielzeit;
                # die API-Ergebnisse enthalten aber die Verlaengerung -> nicht sauber abrechenbar
                if m["key"] == "h2h" and len(preise) == 3 and gruppe not in ("Fußball", "Handball"):
                    continue
                gegen = vergleich.get(schluessel, {})
                for tipp, quote in preise.items():
                    p = ref["probs"].get(tipp)
                    if p is None or not (cfg["min_quote"] <= quote <= cfg["max_quote"]):
                        continue
                    markt_p = gegen.get("markt", {}).get(tipp)
                    if markt_p is not None and abs(p - markt_p) > cfg.get("max_abweichung_markt", 0.08):
                        continue  # Hauptquelle und Marktschnitt passen nicht zusammen -> Datenfehler
                    qn = netto_quote(cfg, b["key"], quote)
                    edge = p * qn - 1
                    kandidaten.append({
                        "event_id": event["id"], "liga": liga_name, "liga_key": event["sport_key"], "gruppe": gruppe,
                        "anstoss": event["commence_time"], "heim": event["home_team"],
                        "gast": event["away_team"], "markt": m["key"], "tipp": tipp, "linie": linie,
                        "buchmacher": b["key"], "quote": quote, "quote_netto": round(qn, 3),
                        "fair_prob": p, "edge": edge, "quelle": ref["quelle"],
                        "analyse": analyse(cfg, gegen, tipp, qn, ref["quelle"]),
                    })
    # pro Ausgang nur das beste Angebot
    bestes = {}
    for k in kandidaten:
        s = (k["markt"], k["tipp"], k["linie"])
        if s not in bestes or k["edge"] > bestes[s]["edge"]:
            bestes[s] = k
    return list(bestes.values()), fair


# ---------- Darstellung ----------

def tipp_text(w):
    if w["markt"] == "h2h":
        return "Unentschieden" if w["tipp"] == "Draw" else "Sieg " + w["tipp"]
    if w["markt"] == "totals":
        art = "Über" if w["tipp"] == "Over" else "Unter"
        return "%s %s" % (art, ("%g" % w["linie"]).replace(".", ","))
    return "%s %s" % (w["tipp"], w["linie"] or "")


def zeit_text(iso):
    t = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone()
    tage = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
    return "%s %s" % (tage[t.weekday()], t.strftime("%d.%m. %H:%M"))


def buch_name(cfg, key):
    return cfg["meine_buchmacher"].get(key, {}).get("name", key)


def melden(titel, text):
    if DEMO:
        return
    try:
        subprocess.run(["osascript", "-e", 'display notification "%s" with title "%s"' % (text, titel)],
                       check=False, timeout=10)
    except Exception:
        pass


# ---------- Befehle ----------

def scan(cfg, manuell=False):
    con = db()
    heute = datetime.now().strftime("%Y-%m-%d")
    alle_value = []
    anzahl_spiele = 0
    liste, rest = sportarten(cfg)
    budget = lauf_budget(cfg, rest)
    if manuell and budget is not None:
        budget *= 2
    kosten = len(cfg["maerkte"])
    gescannt, ausgelassen = [], 0
    # Rotation: zuerst Ligen mit offenen Tipps (Quote/CLV aktuell halten), dann die am laengsten nicht gescannten
    jetzt_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    mit_tipps = {r[0] for r in con.execute(
        "SELECT DISTINCT liga_key FROM wetten WHERE status='offen' AND anstoss > ?", (jetzt_iso,))}
    zuletzt = {r[0][8:]: r[1] for r in con.execute("SELECT k, v FROM meta WHERE k LIKE 'zuletzt_%'")}
    rang = {k: i for i, (k, _, _) in enumerate(liste)}
    faellig_vor = (datetime.now(timezone.utc) - timedelta(hours=cfg.get("prioritaet_intervall_stunden", 24))
                   ).strftime("%Y-%m-%dT%H:%M:%SZ")
    prio_faellig = {k for k in cfg["prioritaet"] if zuletzt.get(k, "") < faellig_vor}
    vor_6h = (datetime.now(timezone.utc) - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
    plan = {k: spielplan(cfg, k) for k, _, _ in liste}
    liste = [x for x in liste if plan[x[0]][1] > 0]
    # Reihenfolge: offene Tipps > Spiele in den naechsten 12 h (viele zuerst, nicht in den letzten 6 h gescannt)
    #              > Prioritaets-Ligen (1x taeglich) > Rotation nach letztem Scan
    def reihenfolge(x):
        k = x[0]
        heute = plan[k][0] > 0 and zuletzt.get(k, "") < vor_6h
        return (k not in mit_tipps, not heute, -plan[k][0] if heute else 0, k not in prio_faellig,
                zuletzt.get(k, ""), rang[k])
    liste.sort(key=reihenfolge)
    print("%d Wettbewerbe aktiv, Budget fuer diesen Lauf: %s Credits" % (len(liste), budget if budget is not None else "frei"))
    for liga_key, liga_name, gruppe in liste:
        if budget is not None and budget < kosten:
            ausgelassen += 1
            continue
        events, r = hole_quoten(cfg, liga_key)
        rest = r or rest
        meta_setzen(con, "zuletzt_" + liga_key, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        if budget is not None:
            budget -= kosten
        if events:
            gescannt.append({"name": liga_name, "gruppe": gruppe, "spiele": len(events)})
        for ev in events:
            anzahl_spiele += 1
            value, fair = finde_value(cfg, ev, liga_name, gruppe)
            # Schlussquote (CLV) fuer offene Wetten mitschreiben, solange das Spiel nicht begonnen hat
            jetzt_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            aktuell = {(v["markt"], v["tipp"], v["linie"]): v for v in value}
            for w in con.execute("SELECT id, markt, tipp, linie FROM wetten WHERE event_id=? AND status='offen'",
                                 (ev["id"],)).fetchall():
                a = aktuell.get((w["markt"], w["tipp"], w["linie"]))
                if a:
                    con.execute("UPDATE wetten SET schluss_prob=? WHERE id=?", (a["fair_prob"], w["id"]))
                    con.execute("UPDATE wetten SET akt_quote=?, akt_edge=?, akt_zeit=?, analyse=? WHERE id=?",
                                (a["quote"], a["edge"], jetzt_iso, json.dumps(a["analyse"]), w["id"]))
            alle_value += [v for v in value if v["edge"] >= cfg["min_edge"]]

    alle_value.sort(key=lambda v: v["edge"], reverse=True)
    neu = 0
    zeilen = []
    for v in alle_value:
        verdaechtig = v["edge"] > cfg["max_edge_plausibel"]
        einsatz = 0.0 if verdaechtig else kelly_einsatz(cfg, v["fair_prob"], v["quote_netto"])
        if not verdaechtig:
            cur = con.execute("""INSERT OR IGNORE INTO wetten (erstellt, event_id, liga, liga_key, anstoss, heim,
                gast, markt, tipp, linie, buchmacher, quote, quote_netto, fair_prob, edge, einsatz, schluss_prob,
                akt_quote, akt_edge, akt_zeit, analyse, gruppe)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (heute, v["event_id"], v["liga"], v["liga_key"], v["anstoss"], v["heim"], v["gast"], v["markt"],
                 v["tipp"], v["linie"], v["buchmacher"], v["quote"], v["quote_netto"], v["fair_prob"],
                 v["edge"], einsatz, v["fair_prob"], v["quote"], v["edge"],
                 datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), json.dumps(v["analyse"]), v["gruppe"]))
            neu += cur.rowcount
        zeilen.append((v, einsatz, verdaechtig))
    meta_setzen(con, "letzter_scan", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    meta_setzen(con, "spiele_geprueft", anzahl_spiele)
    meta_setzen(con, "gescannt", json.dumps(gescannt, ensure_ascii=False))
    meta_setzen(con, "ausgelassen", ausgelassen)
    if ausgelassen:
        print("%d Wettbewerbe wegen des Credit-Budgets ausgelassen." % ausgelassen)
    if rest and rest != "demo":
        meta_setzen(con, "credits", rest)
    con.commit()

    md = ["# Value-Wetten %s" % datetime.now().strftime("%d.%m.%Y %H:%M"), "",
          "%d Spiele geprueft, %d Wetten mit mindestens %.0f %% Edge nach Steuer." %
          (anzahl_spiele, len(alle_value), cfg["min_edge"] * 100), ""]
    if zeilen:
        md += ["| Anstoss | Spiel | Tipp | Quote | Gewinn-Wahrsch. | Edge | Sicherheit | Einsatz |",
               "|---|---|---|---|---|---|---|---|"]
        for v, einsatz, verd in zeilen:
            md.append("| %s | %s – %s (%s) | %s | %.2f %s | %.1f %% | %+.1f %%%s | %s | %s |" % (
                zeit_text(v["anstoss"]), v["heim"], v["gast"], v["liga"], tipp_text(v), v["quote"],
                buch_name(cfg, v["buchmacher"]), v["fair_prob"] * 100, v["edge"] * 100,
                " ⚠️ pruefen" if verd else "", v["analyse"]["sicherheit"], "–" if verd else "%.2f €" % einsatz))
        md += ["", "Gewinn-Wahrsch. = faire Wahrscheinlichkeit aus Pinnacle/Betfair ohne Marge.",
               "Edge = erwarteter Gewinn pro 1 € Einsatz nach Wettsteuer. Einsatz = %d %% Kelly, max. %d %% der Bankroll." %
               (cfg["kelly_anteil"] * 100, cfg["max_einsatz_anteil"] * 100),
               "Edge ueber %d %% ist meist ein Datenfehler oder eine veraltete Quote und wird nicht gezaehlt." %
               (cfg["max_edge_plausibel"] * 100)]
    else:
        md.append("Heute keine Value-Wetten. Das ist normal – nicht wetten ist dann die richtige Entscheidung.")
    if rest:
        md += ["", "API-Kontingent uebrig: %s" % rest]
    text = "\n".join(md)

    if not DEMO and os.path.isdir(os.path.join(BASIS, "reports")):
        with open(os.path.join(BASIS, "reports", "%s.md" % heute), "w", encoding="utf-8") as f:
            f.write(text + "\n")
    print(text)
    print("\n%d neue Papierwetten gespeichert." % neu)
    gute = [z for z in zeilen if not z[2]]
    if gute:
        v = gute[0][0]
        melden("Wettbot: %d Value-Wetten" % len(gute),
               "Top: %s %.2f (%.0f %%, Edge %+.1f %%)" % (tipp_text(v), v["quote"], v["fair_prob"] * 100, v["edge"] * 100))


def ausgang(w, heim_tore, gast_tore):
    """Gibt 'gewonnen', 'verloren' oder 'void' zurueck."""
    if w["markt"] == "h2h":
        if heim_tore > gast_tore:
            sieger = w["heim"]
        elif gast_tore > heim_tore:
            sieger = w["gast"]
        else:
            sieger = "Draw"
        return "gewonnen" if w["tipp"] == sieger else "verloren"
    if w["markt"] == "totals":
        summe = heim_tore + gast_tore
        if summe == w["linie"]:
            return "void"
        ueber = summe > w["linie"]
        return "gewonnen" if (w["tipp"] == "Over") == ueber else "verloren"
    return None


def settle(cfg):
    con = db()
    jetzt = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    offen = con.execute("SELECT * FROM wetten WHERE status='offen' AND anstoss < ?", (jetzt,)).fetchall()
    if not offen:
        print("Keine offenen Wetten zum Abrechnen.")
        return
    ligen = sorted({w["liga_key"] for w in offen})
    ergebnisse = {}
    for liga in ligen:
        if DEMO:
            with open(os.path.join(BASIS, "fixtures", "beispiel_ergebnisse.json"), encoding="utf-8") as f:
                daten = json.load(f)
        else:
            daten, _ = hole("/sports/%s/scores" % liga, {"daysFrom": 3})
        for e in daten:
            if e.get("completed") and e.get("scores"):
                ergebnisse[e["id"]] = {s["name"]: int(s["score"]) for s in e["scores"]}
    abgerechnet = 0
    for w in offen:
        s = ergebnisse.get(w["event_id"])
        if not s:
            alt = datetime.now(timezone.utc) - datetime.strptime(w["anstoss"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            if alt > timedelta(days=5):
                con.execute("UPDATE wetten SET status='unklar', gewinn=0 WHERE id=?", (w["id"],))
                print("Kein Ergebnis von der API: %s – %s, als unklar abgehakt." % (w["heim"], w["gast"]))
            continue
        res = ausgang(w, s.get(w["heim"], 0), s.get(w["gast"], 0))
        if res is None:
            continue
        gewinn = {"gewonnen": w["einsatz"] * (w["quote_netto"] - 1), "verloren": -w["einsatz"], "void": 0.0}[res]
        con.execute("UPDATE wetten SET status=?, gewinn=? WHERE id=?", (res, round(gewinn, 2), w["id"]))
        abgerechnet += 1
        print("%s: %s – %s %d:%d, %s -> %s (%+.2f €)" % (
            w["liga"], w["heim"], w["gast"], s.get(w["heim"], 0), s.get(w["gast"], 0),
            tipp_text(w), res, gewinn))
    con.commit()
    print("%d Wetten abgerechnet, %d noch offen." % (abgerechnet, len(offen) - abgerechnet))


def stats(cfg):
    con = db()
    alle = con.execute("SELECT * FROM wetten").fetchall()
    fertig = [w for w in alle if w["status"] in ("gewonnen", "verloren")]
    print("\n== Bilanz der Papierwetten ==")
    print("Wetten gesamt: %d | abgerechnet: %d | offen: %d" %
          (len(alle), len(fertig), sum(1 for w in alle if w["status"] == "offen")))
    if not fertig:
        print("Noch keine abgerechneten Wetten.")
        return
    treffer = sum(1 for w in fertig if w["status"] == "gewonnen")
    erwartet = sum(w["fair_prob"] for w in fertig)
    einsatz = sum(w["einsatz"] for w in fertig)
    gewinn = sum(w["gewinn"] for w in fertig)
    flach = sum((w["quote_netto"] - 1) if w["status"] == "gewonnen" else -1 for w in fertig)
    mit_clv = [w for w in alle if w["schluss_prob"]]
    clv = sum(w["quote"] * w["schluss_prob"] - 1 for w in mit_clv) / len(mit_clv) if mit_clv else 0
    print("Treffer: %d von %d (%.1f %%), erwartet waren %.1f" % (treffer, len(fertig), treffer / len(fertig) * 100, erwartet))
    print("Gewinn mit Kelly-Einsaetzen: %+.2f € bei %.2f € Einsatz (ROI %+.1f %%)" %
          (gewinn, einsatz, gewinn / einsatz * 100 if einsatz else 0))
    print("Gewinn mit 1 € pro Wette: %+.2f € (ROI %+.1f %%)" % (flach, flach / len(fertig) * 100))
    print("Durchschnittlicher CLV: %+.2f %% (ueber %d Wetten)" % (clv * 100, len(mit_clv)))
    if len(fertig) < 200:
        print("Achtung: Unter ~200 Wetten ist der Gewinn fast nur Glueck oder Pech. "
              "Der CLV ist frueher aussagekraeftig: dauerhaft positiv = echte Edge.")


def bilanz_daten(alle):
    fertig = [w for w in alle if w["status"] in ("gewonnen", "verloren")]
    mit_clv = [w for w in alle if w["schluss_prob"]]
    einsatz = sum(w["einsatz"] for w in fertig)
    gewinn = sum(w["gewinn"] for w in fertig)
    flach = sum((w["quote_netto"] - 1) if w["status"] == "gewonnen" else -1 for w in fertig)
    return {
        "gesamt": len(alle),
        "abgerechnet": len(fertig),
        "offen": sum(1 for w in alle if w["status"] == "offen"),
        "treffer": sum(1 for w in fertig if w["status"] == "gewonnen"),
        "erwartet": round(sum(w["fair_prob"] for w in fertig), 2),
        "einsatz": round(einsatz, 2),
        "gewinn": round(gewinn, 2),
        "roi": round(gewinn / einsatz, 4) if einsatz else None,
        "gewinn_flach": round(flach, 2),
        "clv": round(sum(w["quote"] * w["schluss_prob"] - 1 for w in mit_clv) / len(mit_clv), 4) if mit_clv else None,
        "clv_anzahl": len(mit_clv),
    }


def export(cfg, ziel=None):
    """Schreibt alles, was die App braucht, in eine JSON-Datei."""
    con = db()
    alle = [dict(w) for w in con.execute("SELECT * FROM wetten ORDER BY anstoss")]
    meta = {r["k"]: r["v"] for r in con.execute("SELECT k, v FROM meta")}
    jetzt = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def karte(w):
        return {
            "id": w["id"], "liga": w["liga"], "gruppe": w["gruppe"] or "Fußball", "anstoss": w["anstoss"], "heim": w["heim"], "gast": w["gast"],
            "tipp": tipp_text(w), "buchmacher": buch_name(cfg, w["buchmacher"]),
            "quote": w["quote"], "prob": round(w["fair_prob"], 4), "edge": round(w["edge"], 4),
            "einsatz": w["einsatz"], "status": w["status"], "gewinn": w["gewinn"],
            "akt_quote": w["akt_quote"], "akt_edge": round(w["akt_edge"], 4) if w["akt_edge"] is not None else None,
            "akt_zeit": w["akt_zeit"],
            "clv": round(w["quote"] * w["schluss_prob"] - 1, 4) if w["schluss_prob"] else None,
            "prob_jetzt": round(w["schluss_prob"], 4) if w["schluss_prob"] else None,
            "analyse": json.loads(w["analyse"]) if w["analyse"] else None,
        }

    tipps = [karte(w) for w in alle if w["status"] == "offen" and w["anstoss"] > jetzt]
    verlauf = [karte(w) for w in reversed(alle) if not (w["status"] == "offen" and w["anstoss"] > jetzt)][:300]

    kurve, summe = [], 0.0
    for w in alle:
        if w["status"] in ("gewonnen", "verloren", "void"):
            summe += w["gewinn"] or 0
            kurve.append({"t": w["anstoss"], "summe": round(summe, 2)})

    daten = {
        "erzeugt": jetzt,
        "letzter_scan": meta.get("letzter_scan"),
        "spiele_geprueft": int(meta.get("spiele_geprueft", 0)),
        "credits": int(meta["credits"]) if meta.get("credits") else None,
        "einstellungen": {k: cfg[k] for k in ("bankroll", "kelly_anteil", "max_einsatz_anteil", "min_edge")},
        "gescannt": json.loads(meta["gescannt"]) if meta.get("gescannt") else [],
        "ausgelassen": int(meta.get("ausgelassen", 0)),
        "tipps": tipps,
        "verlauf": verlauf,
        "bilanz": bilanz_daten(alle),
        "kurve": kurve,
    }
    ziel = ziel or os.path.join(BASIS, "docs", "daten.json")
    with open(ziel, "w", encoding="utf-8") as f:
        json.dump(daten, f, ensure_ascii=False, indent=1)
    print("App-Daten geschrieben: %s (%d Tipps, %d im Verlauf)" % (ziel, len(tipps), len(verlauf)))


def genug_credits(cfg):
    con = db()
    r = con.execute("SELECT v FROM meta WHERE k='credits'").fetchone()
    if r and int(r["v"]) < cfg.get("min_credits_rest", 0):
        print("Nur noch %s Credits uebrig, Abendlauf wird uebersprungen." % r["v"])
        return False
    return True


def main():
    global DEMO
    befehl = sys.argv[1] if len(sys.argv) > 1 else "scan"
    cfg = lade_config()
    if befehl == "demo":
        DEMO = True
        pfad = os.path.join(BASIS, "data", "demo.db")
        if os.path.exists(pfad):
            os.remove(pfad)
        scan(cfg)
        con = db()  # Demo: Spiele als begonnen markieren, damit settle sie abrechnet
        con.execute("UPDATE wetten SET anstoss='2026-09-27T13:30:00Z' WHERE event_id IN ('ev1', 'ev2')")
        con.commit()
        settle(cfg)
        stats(cfg)
        export(cfg, os.path.join(BASIS, "data", "demo-daten.json"))
    elif befehl == "scan":
        scan(cfg)
    elif befehl == "settle":
        settle(cfg)
    elif befehl == "stats":
        stats(cfg)
    elif befehl == "export":
        export(cfg)
    elif befehl == "morgen":
        settle(cfg)
        scan(cfg)
        stats(cfg)
        export(cfg)
    elif befehl in ("abend", "manuell"):
        if genug_credits(cfg):
            scan(cfg, manuell=befehl == "manuell")
        export(cfg)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
