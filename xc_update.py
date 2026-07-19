#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
XC-Dashboard Tageslauf (laeuft auf GitHub Actions)
Holt Paraglidable (2 Keys), Open-Meteo, Foehn, Sounding; rechnet Formel v2;
Sounding-Interpretation + Lernabgleich via Claude-API; schreibt data.js.
Schluessel kommen aus Umgebungsvariablen (GitHub Secrets), nie aus Dateien.
"""

import os, json, math, re, datetime, urllib.request, urllib.parse

# ---------- Hilfen ----------

def http_get(url, headers=None, timeout=45):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")

def http_post_json(url, payload, headers, timeout=120):
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

def claude(prompt, use_websearch=False, max_tokens=1500):
    """Ein Aufruf der Claude-API. Gibt den Text der Antwort zurueck (oder None)."""
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        return None
    body = {
        "model": "claude-sonnet-4-6",
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if use_websearch:
        body["tools"] = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 6}]
    try:
        resp = http_post_json(
            "https://api.anthropic.com/v1/messages", body,
            {"Content-Type": "application/json", "x-api-key": key,
             "anthropic-version": "2023-06-01"})
        return "\n".join(b.get("text","") for b in resp.get("content",[]) if b.get("type")=="text")
    except Exception as e:
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
for r in ROUTEN:
    r["key"] = r["name"].lower().replace(" ", "_").replace("\u00df", "ss")

ALT = {}
try:
    alt_raw = open("data.js", encoding="utf-8").read()
    ALT = json.loads(alt_raw.replace("window.XCDATA = ", "").rstrip().rstrip(";"))
except Exception as e:
    print("Keine alte data.js lesbar:", e)
LERNEN = ALT.get("lernen") or {"kalibrierung": {r["key"]: 1.0 for r in ROUTEN}, "fluege": []}
FEHLER = []

# ---------- 1. Paraglidable (beide Keys) ----------

def paraglidable():
    tage = {}
    for envname in ("PARAGLIDABLE_KEY1", "PARAGLIDABLE_KEY2"):
        k = os.environ.get(envname, "").strip()
        if not k:
            FEHLER.append(f"{envname} fehlt"); continue
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

# ---------- 3. Foehn (Druck Innsbruck - Bozen) ----------

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

# ---------- 4. Sounding Innsbruck -> Claude-Interpretation ----------

SOUNDING_KORREKTUR = 1.0
SOUNDING_TEXT = "Sounding heute nicht verfuegbar."
try:
    heute = datetime.date.today()
    u = ("https://weather.uwyo.edu/cgi-bin/sounding?region=europe&TYPE=TEXT%3ALIST"
         f"&YEAR={heute.year}&MONTH={heute.month:02d}"
         f"&FROM={heute.day:02d}03&TO={heute.day:02d}03&STNM=11120")
    roh = http_get(u)
    roh = re.sub(r"<[^>]+>", "", roh)
    if "11120" in roh and len(roh) > 500:
        antwort = claude(
            "Du bist Streckenflug-Meteorologe fuer die Tiroler Alpen. Unten das heutige "
            "03-UTC-Sounding Innsbruck (Station 11120), Rohtext von der Uni Wyoming.\n"
            "Werte aus Sicht eines XC-Gleitschirmpiloten aus: KKN2/elevated parcel (NICHT KKN1), "
            "Labilitaet (Showalter/Faust sinngemaess), Nullgradgrenze, Windprofil 700/600/500 hPa, "
            "Ueberentwicklungsrisiko.\n"
            "Antworte NUR mit einem JSON-Objekt, kein anderer Text:\n"
            '{"korrektur": <Zahl 0.85|1.0|1.15>, "text": "<2-3 praezise Saetze auf Deutsch>"}\n'
            "Regeln fuer korrektur: 0.85 wenn Basis niedrig (KKN2 unter ca. 3200 m) ODER starke "
            "OD-Gefahr ODER kritisches Windprofil; 1.15 wenn Basis hoch (KKN2 ueber ca. 3800 m) "
            "UND Windprofil unauffaellig; sonst 1.0.\n\n" + roh[:6000],
            max_tokens=600)
        j = json_aus_text(antwort)
        if j and 0.7 <= float(j.get("korrektur", 1.0)) <= 1.3:
            SOUNDING_KORREKTUR = float(j["korrektur"])
            SOUNDING_TEXT = str(j.get("text", ""))[:600]
        else:
            FEHLER.append("Sounding-Interpretation unbrauchbar, Korrektur=1.0")
    else:
        FEHLER.append("Sounding 11120 heute nicht im Wyoming-Archiv")
except Exception as e:
    FEHLER.append(f"Sounding: {e}")

# ---------- 5. Punkt-zu-Route-Zuordnung (dynamisch, 18-km-Korridor) ----------

punkte0 = next(iter(PG.values()), [])
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

# ---------- 6. Scores rechnen ----------

def score(rkey, punkte, datum, ist_heute_morgen):
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
    if ist_heute_morgen:
        p *= SOUNDING_KORREKTUR
    return {"v": max(0, min(100, round(p))), "weak": weak, "fly": round(minfly, 2)}

heute_s = datetime.date.today().isoformat()
morgen_s = (datetime.date.today()+datetime.timedelta(days=1)).isoformat()
DAYS = []
for datum in sorted(PG.keys()):
    eintrag = {"date": datum, "label": label(datum), "source": "paraglidable", "routes": {}}
    for r in ROUTEN:
        s = score(r["key"], PG[datum], datum, datum in (heute_s, morgen_s))
        if s: eintrag["routes"][r["key"]] = s
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

# ---------- 7. Lernabgleich via Claude (mit Websuche auf XContest) ----------

LERNFAZIT = "Lernabgleich heute nicht durchgefuehrt."
gestern = (datetime.date.today()-datetime.timedelta(days=1)).isoformat()
alt_tag = next((t for t in ALT.get("days", []) if t.get("date") == gestern), None)
if alt_tag and alt_tag.get("source") == "paraglidable":
    prognosen = {k: v["v"] for k, v in alt_tag.get("routes", {}).items()}
    regionen = {r["key"]: f"{r['launch']} (Start ca. {r['start'][0]:.2f}N {r['start'][1]:.2f}E, "
                          f"Ziel {NOMINAL[r['key']]} km)" for r in ROUTEN}
    antwort = claude(
        "Du pflegst die Kalibrierung eines XC-Prognosesystems fuer Gleitschirm-Dreiecke "
        "in den Ostalpen. Gestern war der " + gestern + ".\n"
        "Prognosen von gestern (Route: Prozent): " + json.dumps(prognosen) + "\n"
        "Routen/Regionen: " + json.dumps(regionen, ensure_ascii=False) + "\n"
        "Aktuelle Kalibrierfaktoren: " + json.dumps(LERNEN["kalibrierung"]) + "\n\n"
        "Recherchiere per Websuche auf xcontest.org (oeffentliche Tageswertungen/Metadaten), "
        "welche groessten Fluege gestern in diesen Regionen real geflogen wurden "
        "(Distanz, Schnitt, Startplatz).\n"
        "Regeln: Prognose >=40 und niemand flog >=80% der Zieldistanz in der Region -> "
        "Faktor der Route -0.05. Prognose <15 und es wurde >=80% geflogen -> +0.05. "
        "Sonst unveraendert. Grenzen 0.6 bis 1.4.\n"
        "Antworte NUR mit einem JSON-Objekt:\n"
        '{"kalibrierung": {<alle Routen mit neuem Faktor>}, '
        '"neuer_flug": null oder {"datum":"' + gestern + '","region":"...","prognose":<int>,'
        '"real_km":<Zahl>,"real_schnitt":<Zahl>,"bericht":"<3-5 Saetze auf Deutsch, aus denen '
        'ein Pilot lernt: was erwartet, was real passiert, welcher Wetterfaktor erklaert die '
        'Abweichung, was ist die Lehre>"}, '
        '"fazit": "<1-2 Saetze Ergebnis des Abgleichs>"}\n'
        "Wenn die Websuche nichts Belastbares ergibt: Faktoren unveraendert lassen, "
        "neuer_flug=null, das im fazit sagen. Keine Werte erfinden.",
        use_websearch=True, max_tokens=1800)
    j = json_aus_text(antwort)
    if j:
        neu = j.get("kalibrierung") or {}
        for k in LERNEN["kalibrierung"]:
            try:
                alt_f = LERNEN["kalibrierung"][k]
                f = float(neu.get(k, alt_f))
                if abs(f - alt_f) <= 0.051 and 0.6 <= f <= 1.4:  # max 1 Schritt pro Tag
                    LERNEN["kalibrierung"][k] = round(f, 2)
            except Exception:
                pass
        if j.get("neuer_flug"):
            LERNEN["fluege"].append(j["neuer_flug"])
            LERNEN["fluege"] = LERNEN["fluege"][-30:]
        LERNFAZIT = str(j.get("fazit", ""))[:400]
    else:
        FEHLER.append("Lernabgleich: keine auswertbare Antwort")

# ---------- 8. data.js schreiben ----------

jetzt = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=2)))
DATA = {
    "generated": jetzt.strftime("%Y-%m-%d %H:%M"),
    "points": [{"name": p["name"], "lat": p["lat"], "lon": p["lon"]} for p in punkte0],
    "assign": ASSIGN,
    "days": DAYS,
    "lernen": LERNEN,
}
with open("data.js", "w", encoding="utf-8") as f:
    f.write("window.XCDATA = " + json.dumps(DATA, ensure_ascii=False) + ";")

# ---------- 9. Kurzfazit ins Log ----------

beste = max(((t["label"], k, v["v"]) for t in DAYS for k, v in t["routes"].items()),
            key=lambda x: x[2], default=None)
print("=== KURZFAZIT ===")
if beste:
    print(f"Bester Tag: {beste[0]} mit {beste[1]} ({beste[2]} %)")
print("Sounding:", SOUNDING_TEXT, f"(Korrektur x{SOUNDING_KORREKTUR})")
warn = [f"{t['label']} dP={t['foehn']['dp']}" for t in DAYS
        if t.get("foehn") and abs(t["foehn"]["dp"]) >= 4]
print("Foehnwarnungen:", ", ".join(warn) if warn else "keine (max dP "
      + (str(max((abs(v) for v in FOEHN.values()), default=0)) + " hPa)" if FOEHN else "unbekannt)"))
print("Lernabgleich:", LERNFAZIT)
if FEHLER:
    print("Fehlende/gestoerte Quellen:", "; ".join(FEHLER))
print("data.js geschrieben:", DATA["generated"])
