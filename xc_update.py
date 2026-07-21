#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
XC-Dashboard Tageslauf (laeuft auf GitHub Actions)

Holt Paraglidable (2 Keys), Open-Meteo und Foehndruck; rechnet Formel v2;
gleicht die gestrige Prognose gegen die realen XContest-Fluege ab und
schreibt data.js.

Aufgabenteilung beim Lernabgleich:
  - Claude liefert AUSSCHLIESSLICH Fakten (km, Distanzart, Startplatz).
  - Die Kalibrierung rechnet Python deterministisch aus diesen Fakten.
Damit ist das Ergebnis reproduzierbar und nicht von Formulierungen abhaengig.

Schluessel kommen aus Umgebungsvariablen (GitHub Secrets), nie aus Dateien.
"""

import os, json, math, re, datetime, urllib.request, urllib.error

# ---------- Hilfen ----------

def http_get(url, headers=None, timeout=45):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")

def http_post_json(url, payload, headers, timeout=180):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

def dist_km(la1, lo1, la2, lo2):
    R = 6371.0
    la1, lo1, la2, lo2 = map(math.radians, [la1, lo1, la2, lo2])
    h = math.sin((la2-la1)/2)**2 + math.cos(la1)*math.cos(la2)*math.sin((lo2-lo1)/2)**2
    return 2*R*math.asin(math.sqrt(h))

WD = ["MO","DI","MI","DO","FR","SA","SO"]
def label(datum):
    d = datetime.date.fromisoformat(datum)
    return f"{WD[d.weekday()]} {d.day:02d}.{d.month:02d}."

FEHLER = []

def claude(prompt, use_websearch=False, max_tokens=1500):
    """Ein Aufruf der Claude-API. Gibt den Text der Antwort zurueck (oder None)."""
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        FEHLER.append("ANTHROPIC_API_KEY fehlt - Lernabgleich uebersprungen")
        return None
    body = {
        "model": os.environ.get("CLAUDE_MODEL", "claude-opus-4-8"),
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if use_websearch:
        body["tools"] = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 8}]
    try:
        resp = http_post_json(
            "https://api.anthropic.com/v1/messages", body,
            {"Content-Type": "application/json", "x-api-key": key,
             "anthropic-version": "2023-06-01"})
        return "\n".join(b.get("text","") for b in resp.get("content",[])
                         if b.get("type") == "text")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            pass
        FEHLER.append(f"Claude-API HTTP {e.code}: {detail}")
        print("Claude-API-Fehler:", e.code, detail)
        return None
    except Exception as e:
        FEHLER.append(f"Claude-API: {e}")
        print("Claude-API-Fehler:", e)
        return None

def json_aus_text(text):
    """Zieht das erste JSON-Objekt aus einer Claude-Antwort."""
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None

# ---------- Konfiguration & alte Daten laden ----------

CFG = json.load(open("config.json", encoding="utf-8"))
ROUTEN = CFG["routen"]
NOMINAL = CFG["nominal_km"]
KORRIDOR = float(CFG.get("korridor_km", 18.0))
STARTPLAETZE = CFG.get("xcontest_startplaetze", {})
GEWICHT = CFG.get("distanzart_gewicht", {"fai": 1.0, "flach": 0.85, "frei": 0.7})
LERNREGEL = CFG.get("lernregel", {})
SCHWELLE_HOCH = float(LERNREGEL.get("prognose_hoch", 40))
SCHWELLE_TIEF = float(LERNREGEL.get("prognose_tief", 15))
ERFUELLT_ANTEIL = float(LERNREGEL.get("erfuellt_anteil", 0.8))
SCHRITT = float(LERNREGEL.get("schritt", 0.05))
GRENZE_MIN = float(LERNREGEL.get("grenze_min", 0.6))
GRENZE_MAX = float(LERNREGEL.get("grenze_max", 1.4))

for r in ROUTEN:
    r["key"] = r["name"].lower().replace(" ", "_").replace("ß", "ss")

ALT = {}
try:
    alt_raw = open("data.js", encoding="utf-8").read()
    ALT = json.loads(alt_raw.replace("window.XCDATA = ", "").rstrip().rstrip(";"))
except Exception as e:
    print("Keine alte data.js lesbar:", e)

LERNEN = ALT.get("lernen") or {"kalibrierung": {}, "fluege": []}
LERNEN.setdefault("kalibrierung", {})
LERNEN.setdefault("fluege", [])
for r in ROUTEN:
    LERNEN["kalibrierung"].setdefault(r["key"], 1.0)

# Archiv der bisher abgegebenen Tagesprognosen: {datum: {routenkey: prozent}}
ARCHIV = ALT.get("prognose_archiv") or {}

# ---------- 1. Paraglidable (beide Keys) ----------

def paraglidable():
    tage = {}
    for envname in ("PARAGLIDABLE_KEY1", "PARAGLIDABLE_KEY2"):
        k = os.environ.get(envname, "").strip()
        if not k:
            FEHLER.append(f"{envname} fehlt")
            continue
        try:
            raw = json.loads(http_get(f"https://api.paraglidable.com/?key={k}&format=JSON"))
            for datum, punkte in raw.items():
                tage.setdefault(datum, [])
                vorhanden = {p["name"] for p in tage[datum]}
                for p in punkte:
                    if p["name"] not in vorhanden:
                        tage[datum].append(p)
        except Exception as e:
            FEHLER.append(f"Paraglidable {envname}: {e}")
    return tage

PG = paraglidable()

# Referenztag fuer Kartenpunkte und Routenzuordnung: immer der FRUEHESTE Tag,
# nicht ein zufaelliger aus der API-Reihenfolge. Sonst wackelt die Zuordnung
# zwischen zwei Laeufen und die Prozentwerte werden unvergleichbar.
punkte0 = PG[min(PG.keys())] if PG else []

# ---------- 2. Open-Meteo: Modellfaktoren pro Startplatz ----------

def openmeteo_start(lat, lon):
    url = ("https://api.open-meteo.com/v1/forecast?"
           f"latitude={lat}&longitude={lon}"
           "&hourly=wind_speed_700hPa"
           "&daily=precipitation_probability_max,sunshine_duration"
           "&forecast_days=16&timezone=Europe%2FBerlin")
    return json.loads(http_get(url))

MODELL = {}
for r in ROUTEN:
    try:
        om = openmeteo_start(r["start"][0], r["start"][1])
        htime = om["hourly"]["time"]; hw = om["hourly"]["wind_speed_700hPa"]
        wind_pro_tag = {}
        for t, w in zip(htime, hw):
            datum, stunde = t[:10], int(t[11:13])
            if w is not None and 10 <= stunde <= 17:
                wind_pro_tag.setdefault(datum, []).append(w)
        tage = {}
        for datum, regen, sonne in zip(om["daily"]["time"],
                                       om["daily"]["precipitation_probability_max"],
                                       om["daily"]["sunshine_duration"]):
            wlist = wind_pro_tag.get(datum, [])
            wind = sum(wlist)/len(wlist) if wlist else None
            tage[datum] = {
                "wind": wind,
                "regen": regen if regen is not None else 0,
                "sonne_h": (sonne or 0)/3600.0,
            }
        MODELL[r["key"]] = tage
    except Exception as e:
        FEHLER.append(f"Open-Meteo {r['name']}: {e}")
        MODELL[r["key"]] = {}

def faktoren(rkey, datum):
    m = MODELL.get(rkey, {}).get(datum)
    if not m:
        return 1.0, 1.0, 1.0  # Quelle fehlt -> neutral, nicht raten
    w = m["wind"]
    fw = 1.0 if (w is None or w <= 10) else max(0.2, 1 - (w-10)/25*0.8)
    p = m["regen"]
    fr = 1.0 if p <= 20 else max(0.0, 1 - (p-20)/80)
    fs = min(1.0, m["sonne_h"]/9.0)
    return fw, fr, fs

# ---------- 3. Foehn (Druck Bozen - Innsbruck) ----------

FOEHN = {}
try:
    om = json.loads(http_get(
        "https://api.open-meteo.com/v1/forecast?latitude=47.26,46.50&longitude=11.39,11.35"
        "&hourly=pressure_msl&forecast_days=16&timezone=Europe%2FBerlin"))
    ibk, boz = om[0], om[1]
    tages_dp = {}
    for t, pi, pb in zip(ibk["hourly"]["time"], ibk["hourly"]["pressure_msl"],
                         boz["hourly"]["pressure_msl"]):
        datum, stunde = t[:10], int(t[11:13])
        if pi is None or pb is None or not (8 <= stunde <= 20):
            continue
        dp = pb - pi
        if datum not in tages_dp or abs(dp) > abs(tages_dp[datum]):
            tages_dp[datum] = dp
    FOEHN = {d: round(v, 1) for d, v in tages_dp.items()}
except Exception as e:
    FEHLER.append(f"Foehn/Druck: {e}")

# ---------- 4. Punkt-zu-Route-Zuordnung (dynamisch, 18-km-Korridor) ----------

ASSIGN = {}
for r in ROUTEN:
    near = []
    for p in punkte0:
        try:
            dmin = min(dist_km(p["lat"], p["lon"], t[0], t[1]) for t in r["track"])
            if dmin <= KORRIDOR:
                near.append(p["name"])
        except Exception:
            pass
    ASSIGN[r["key"]] = near or CFG.get("punkt_zuordnung", {}).get(r["key"], [])

# ---------- 5. Scores rechnen ----------

def score(rkey, punkte, datum):
    namen = ASSIGN.get(rkey, [])
    vals = [p for p in punkte if p["name"] in namen]
    if not vals:
        return None
    minxc = min(v["forecast"]["XC"] for v in vals)
    minfly = min(v["forecast"]["fly"] for v in vals)
    weak = min(vals, key=lambda v: v["forecast"]["XC"])["name"]
    fw, fr, fs = faktoren(rkey, datum)
    p = (100 * minxc * (200.0/NOMINAL[rkey])**1.2 * min(1.0, minfly/0.7)
         * fw * fr * fs * LERNEN["kalibrierung"].get(rkey, 1.0))
    return {"v": max(0, min(100, round(p))), "weak": weak, "fly": round(minfly, 2)}

heute_s = datetime.date.today().isoformat()
DAYS = []
for datum in sorted(PG.keys()):
    eintrag = {"date": datum, "label": label(datum), "source": "paraglidable", "routes": {}}
    for r in ROUTEN:
        s = score(r["key"], PG[datum], datum)
        if s:
            eintrag["routes"][r["key"]] = s
    dp = FOEHN.get(datum)
    eintrag["foehn"] = {"dp": dp} if dp is not None else None
    DAYS.append(eintrag)

# Trend-Tage bis Tag 14 (nur Modellfaktoren, ohne Paraglidable)
letztes = max(PG.keys()) if PG else heute_s
d = datetime.date.fromisoformat(letztes)
while len(DAYS) < 14:
    d += datetime.timedelta(days=1)
    datum = d.isoformat()
    eintrag = {"date": datum, "label": label(datum), "source": "trend", "routes": {}}
    for r in ROUTEN:
        if not MODELL.get(r["key"], {}).get(datum):
            eintrag["routes"][r["key"]] = {"v": 0, "weak": "keine Modelldaten", "fly": 0}
            continue
        fw, fr, fs = faktoren(r["key"], datum)
        basis = 55 * fw * fr * fs * LERNEN["kalibrierung"].get(r["key"], 1.0)
        basis *= (200.0/NOMINAL[r["key"]])**1.2
        eintrag["routes"][r["key"]] = {"v": max(0, min(100, round(basis))),
                                       "weak": "ENS-Trend", "fly": 0}
    dp = FOEHN.get(datum)
    eintrag["foehn"] = {"dp": dp} if dp is not None else None
    DAYS.append(eintrag)

# ---------- 6. Lernabgleich ----------
#
# Schritt 1: Claude sucht auf XContest die groessten Fluege von gestern je
#            Startplatz und gibt NUR Zahlen zurueck.
# Schritt 2: Python gewichtet nach Distanzart und passt die Kalibrierung an.

def gewichte_flug(km, art):
    """Rechnet reale Kilometer in FAI-aequivalente Kilometer um."""
    return float(km) * float(GEWICHT.get(str(art).lower(), GEWICHT.get("frei", 0.7)))

def kalibriere(prognose, gewichtete_km, ziel_km, faktor):
    """Deterministische Lernregel. Gibt (neuer_faktor, begruendung) zurueck."""
    schwelle = ERFUELLT_ANTEIL * ziel_km
    erfuellt = gewichtete_km >= schwelle
    if prognose >= SCHWELLE_HOCH and not erfuellt:
        neu = max(GRENZE_MIN, round(faktor - SCHRITT, 2))
        return neu, (f"Prognose {prognose} % war zu optimistisch: nur "
                     f"{gewichtete_km:.0f} von {schwelle:.0f} gewichteten km erreicht.")
    if prognose < SCHWELLE_TIEF and erfuellt:
        neu = min(GRENZE_MAX, round(faktor + SCHRITT, 2))
        return neu, (f"Prognose {prognose} % war zu pessimistisch: "
                     f"{gewichtete_km:.0f} gewichtete km geflogen.")
    return faktor, "Prognose im Rahmen, keine Anpassung."

LERNFAZIT = "Lernabgleich heute nicht durchgefuehrt."
gestern = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
prognosen_gestern = ARCHIV.get(gestern)

if not prognosen_gestern:
    LERNFAZIT = (f"Keine archivierte Prognose fuer {gestern} vorhanden - "
                 "der Lernabgleich startet ab dem naechsten Lauf.")
else:
    startplatz_liste = {}
    for r in ROUTEN:
        startplatz_liste[r["key"]] = (STARTPLAETZE.get(r["key"])
                                      or [r.get("launch", r["name"])])
    antwort = claude(
        "Du recherchierst Fakten aus der oeffentlichen XContest-Tageswertung. "
        f"Stichtag ist der {gestern}.\n\n"
        "Fuer jede der folgenden Routen ist eine Liste von Startplaetzen angegeben. "
        "Suche in der XContest-Tageswertung (xcontest.org, Datum "
        f"{gestern}) den WEITESTEN Gleitschirmflug, der an einem dieser "
        "Startplaetze gestartet ist.\n\n"
        + json.dumps(startplatz_liste, ensure_ascii=False, indent=1) + "\n\n"
        "Antworte NUR mit einem JSON-Objekt, ohne Vor- oder Nachtext:\n"
        '{"<routenkey>": {"km": <Zahl>, "art": "fai"|"flach"|"frei", '
        '"startplatz": "<Name>"} oder null, ...}\n\n'
        "Bedeutung von art: 'fai' = FAI-Dreieck, 'flach' = flaches Dreieck, "
        "'frei' = freie Strecke bzw. Streckenflug ohne Dreiecksbewertung.\n"
        "km ist die von XContest ausgewiesene Streckenlaenge in Kilometern, "
        "NICHT die Punktzahl.\n"
        "Wenn du fuer eine Route keinen Flug findest oder unsicher bist: null. "
        "Erfinde unter keinen Umstaenden Werte.",
        use_websearch=True, max_tokens=2500)

    fluege_roh = json_aus_text(antwort)
    if not fluege_roh:
        FEHLER.append("Lernabgleich: keine auswertbare Antwort von Claude")
        LERNFAZIT = ("Lernabgleich fehlgeschlagen - die XContest-Recherche lieferte "
                     "nichts Auswertbares.")
    else:
        angepasst, gefunden = [], 0
        for r in ROUTEN:
            k = r["key"]
            flug = fluege_roh.get(k)
            prognose = prognosen_gestern.get(k)
            if not isinstance(flug, dict) or prognose is None:
                continue
            try:
                km = float(flug.get("km"))
            except (TypeError, ValueError):
                continue
            art = str(flug.get("art", "frei")).lower()
            if art not in GEWICHT:
                FEHLER.append(f"Lernabgleich {k}: unbekannte Distanzart '{art}', als 'frei' gewertet")
                art = "frei"
            if not (0 < km <= 600):          # Plausibilitaetsgrenze
                FEHLER.append(f"Lernabgleich {k}: unplausible Distanz {flug.get('km')}")
                continue
            gefunden += 1
            gew = gewichte_flug(km, art)
            alt_f = LERNEN["kalibrierung"].get(k, 1.0)
            neu_f, grund = kalibriere(prognose, gew, NOMINAL[k], alt_f)
            LERNEN["kalibrierung"][k] = neu_f
            if neu_f != alt_f:
                angepasst.append(f"{r['name']} {alt_f:.2f}→{neu_f:.2f}")
            LERNEN["fluege"].append({
                "datum": gestern,
                "region": r["name"],
                "startplatz": flug.get("startplatz") or "",
                "prognose": prognose,
                "real_km": round(km, 1),
                "art": art,
                "gewichtet_km": round(gew, 1),
                "ziel_km": NOMINAL[k],
                "faktor_alt": alt_f,
                "faktor_neu": neu_f,
                "bewertung": grund,
            })
        LERNEN["fluege"] = LERNEN["fluege"][-60:]
        LERNFAZIT = (f"{gefunden} von {len(ROUTEN)} Routen mit realen Fluegen abgeglichen. "
                     + ("Angepasst: " + ", ".join(angepasst) + "." if angepasst
                        else "Keine Kalibrierung musste angepasst werden."))

# ---------- 7. Heutige Prognose archivieren ----------

heute_eintrag = next((t for t in DAYS if t["date"] == heute_s), None)
if heute_eintrag:
    ARCHIV[heute_s] = {k: v["v"] for k, v in heute_eintrag["routes"].items()}
else:
    FEHLER.append(f"Kein Tageseintrag fuer heute ({heute_s}) - nichts archiviert")
ARCHIV = {k: ARCHIV[k] for k in sorted(ARCHIV.keys())[-30:]}

# ---------- 8. data.js schreiben ----------

jetzt = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=2)))
DATA = {
    "generated": jetzt.strftime("%Y-%m-%d %H:%M"),
    "points": [{"name": p["name"], "lat": p["lat"], "lon": p["lon"]} for p in punkte0],
    "assign": ASSIGN,
    "days": DAYS,
    "lernen": LERNEN,
    "prognose_archiv": ARCHIV,
    "status": {
        "lernfazit": LERNFAZIT,
        "fehler": FEHLER,
    },
}
with open("data.js", "w", encoding="utf-8") as f:
    f.write("window.XCDATA = " + json.dumps(DATA, ensure_ascii=False) + ";")

# ---------- 9. Kurzfazit ins Log ----------

beste = max(((t["label"], k, v["v"]) for t in DAYS for k, v in t["routes"].items()),
            key=lambda x: x[2], default=None)
print("=== KURZFAZIT ===")
if beste:
    print(f"Bester Tag: {beste[0]} mit {beste[1]} ({beste[2]} %)")
warn = [f"{t['label']} dP={t['foehn']['dp']}" for t in DAYS
        if t.get("foehn") and abs(t["foehn"]["dp"]) >= 4]
print("Foehnwarnungen:", ", ".join(warn) if warn else "keine")
print("Lernabgleich:", LERNFAZIT)
print("Kalibrierung:", json.dumps(LERNEN["kalibrierung"]))
if FEHLER:
    print("Fehlende/gestoerte Quellen:", "; ".join(FEHLER))
print("data.js geschrieben:", DATA["generated"])
