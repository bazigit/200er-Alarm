#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
XContest-Datenquellen-Probe v2.

Klaert, ob der RSS-Feed gefiltert brauchbare Daten liefert. Testet nur den
Startplatz Antholz (Route grente_250) gegen den 30.07.2026 und schaltet die
Filter einzeln zu. Gibt die ROH-Titel der Feed-Eintraege aus, damit wir das
exakte Format sehen.

Referenz aus Screenshot: Antholz Top ~316.82 km, FAI triangle.

Nur lesen, aendert nichts. Einmal laufen lassen und Ausgabe zurueckschicken.
"""
import urllib.request, urllib.parse, re

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36")

DATE = "2026-07-30"
LON, LAT = "12.0176", "46.8484"       # Antholz, config-Startkoordinate

ITEM_RE  = re.compile(r"<item>(.*?)</item>", re.S)
TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S)


def hole(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=45) as r:
        return r.status, r.headers.get("Content-Type", ""), r.read().decode("utf-8", "replace")


def u(qs):
    return "https://www.xcontest.org/rss/flights/?" + qs


def enc(d):
    return urllib.parse.urlencode(d)


URLS = [
    ("1 baseline ?world",           u("world")),
    ("2 baseline ?world/en",        u("world/en")),
    ("3 nur Datum",                 u("world/en&" + enc({"filter[date]": DATE}))),
    ("4 nur Punkt r=15000",         u("world/en&" + enc({"filter[point]": f"{LON} {LAT}",
                                                         "filter[radius]": "15000"}))),
    ("5 Punkt + Datum r=15000",     u("world/en&" + enc({"filter[point]": f"{LON} {LAT}",
                                                         "filter[radius]": "15000",
                                                         "filter[date]": DATE}))),
    ("6 Punkt vertauscht (lat lon)", u("world/en&" + enc({"filter[point]": f"{LAT} {LON}",
                                                          "filter[radius]": "15000",
                                                          "filter[date]": DATE}))),
]

for bez, url in URLS:
    print("\n=== " + bez)
    print(url)
    try:
        st, ct, tx = hole(url)
    except Exception as e:
        print("  FEHLER:", e)
        continue
    items = ITEM_RE.findall(tx)
    print(f"  HTTP {st} | {ct} | {len(tx)} Zeichen | {len(items)} <item>")
    for it in items[:8]:
        t = TITLE_RE.search(it)
        print("   TITLE:", t.group(1).strip() if t else "?")
    if not items:
        print("   Rohtext:", re.sub(r"\s+", " ", tx)[:220])
