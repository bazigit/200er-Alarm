#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
XContest-Datenquellen-Probe.

Ziel: herausfinden, mit welcher URL wir pro Startplatz + Tag die realen
Fluege maschinenlesbar bekommen. Testet mehrere Varianten gegen den
30.07.2026 (Tag aus den Screenshots) und gibt aus, was jede URL liefert.

Erwartete Referenzwerte (aus den Screenshots):
  Antholz     : Top ~316.82 km, FAI triangle
  Zillertal   : Top ~329.82 km, FAI triangle
  Speikboden  : Top ~231.68 km

Das Skript RUFT NUR AB und PARST - es aendert nichts. Einmal laufen lassen
(lokal oder als manueller GitHub-Actions-Run) und die Ausgabe zurueckgeben.
"""
import urllib.request, urllib.parse, re

DATUM_ISO = "2026-07-30"
DATUM_DMY = "30.07.2026"

# Testrouten: (Name, lat, lon) - Koordinaten aus deiner config.json ("start")
ROUTEN = [
    ("grente_250 (Antholz)",  46.8484, 12.0176),
    ("zillertal_250",         47.2190, 11.8249),
    ("speikboden_200",        46.9154, 11.8962),
]

RADIUS_M = 4000
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36")

TITEL_RE = re.compile(r"\[\s*([\d.]+)\s*km\s*::\s*([a-z_]+)\s*\]", re.I)


def baue_urls(lat, lon):
    """Liefert (bezeichnung, url)-Paare fuer alle zu testenden Varianten."""
    pt = f"{lon} {lat}"          # Reihenfolge laut XContest-Beispiel: lon lat
    gemein = {
        "filter[point]": pt,
        "filter[radius]": str(RADIUS_M),
        "filter[mode]": "START",
        "list[sort]": "pts",
        "list[dir]": "down",
    }
    def q(extra):
        d = dict(gemein); d.update(extra)
        return urllib.parse.urlencode(d)
    return [
        ("RSS + date ISO",
         "https://www.xcontest.org/rss/flights/?world/en&"
         + q({"filter[date]": DATUM_ISO})),
        ("RSS + date dmy",
         "https://www.xcontest.org/rss/flights/?world/en&"
         + q({"filter[date]": DATUM_DMY, "filter[date_mode]": "dmy"})),
        ("Suche(HTML) + date dmy",
         "https://www.xcontest.org/world/en/flights-search/?"
         + q({"filter[date]": DATUM_DMY, "filter[date_mode]": "dmy"})),
    ]


def hole(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=45) as r:
        roh = r.read()
        return r.status, r.headers.get("Content-Type", ""), roh.decode("utf-8", "replace")


def main():
    for name, lat, lon in ROUTEN:
        print("\n" + "=" * 70)
        print(name)
        print("=" * 70)
        for bez, url in baue_urls(lat, lon):
            print(f"\n--- {bez}")
            print(url)
            try:
                status, ctype, text = hole(url)
            except Exception as e:
                print(f"  FEHLER: {e}")
                continue
            treffer = TITEL_RE.findall(text)
            print(f"  HTTP {status} | {ctype} | {len(text)} Zeichen | "
                  f"{len(treffer)} Flug-Treffer")
            for km, art in treffer[:5]:
                print(f"     {km} km :: {art}")
            if not treffer:
                schnipsel = re.sub(r"\s+", " ", text)[:180]
                print(f"     (kein Treffer) Anfang: {schnipsel}")


if __name__ == "__main__":
    main()
