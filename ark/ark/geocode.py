"""Offline reverse geocoding — lat/lon → nearest place, zero network calls.

Loads a bundled `geodata/cities.csv` (name, admin, country, code, lat, lon) and
returns the nearest city by great-circle distance. Private, free, deterministic.
The bundled set is a curated list of major world cities; for full coverage the
CSV can be regenerated from a GeoNames `cities*.txt` dump (see
`tools/make_geodata.py`) without changing any code here.
"""

from __future__ import annotations

import csv
import math
from functools import lru_cache
from pathlib import Path
from typing import Optional

from .models import GeoPlace

_GEODATA = Path(__file__).parent / "geodata" / "cities.csv"


class _City:
    __slots__ = ("name", "admin", "country", "cc", "lat", "lon", "_rlat", "_clat", "_slat")

    def __init__(self, name, admin, country, cc, lat, lon):
        self.name = name
        self.admin = admin
        self.country = country
        self.cc = cc
        self.lat = lat
        self.lon = lon
        self._rlat = math.radians(lat)
        self._clat = math.cos(self._rlat)
        self._slat = math.sin(self._rlat)


@lru_cache(maxsize=1)
def _load(path: str = str(_GEODATA)) -> list[_City]:
    cities: list[_City] = []
    p = Path(path)
    if not p.exists():
        return cities
    with open(p, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                cities.append(_City(
                    row["name"], row.get("admin", ""), row.get("country", ""),
                    row.get("country_code", ""), float(row["lat"]), float(row["lon"]),
                ))
            except (KeyError, ValueError):
                continue
    return cities


def reverse(lat: float, lon: float) -> Optional[GeoPlace]:
    """Return the nearest known place to (lat, lon), or None if no dataset."""
    cities = _load()
    if not cities or lat is None or lon is None:
        return None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None

    rlat = math.radians(lat)
    clat, slat = math.cos(rlat), math.sin(rlat)
    rlon = math.radians(lon)

    best: Optional[_City] = None
    best_cos = -2.0  # maximize cos(central angle) == minimize distance
    for c in cities:
        # cosine of great-circle angle via spherical law of cosines
        cos_d = slat * c._slat + clat * c._clat * math.cos(rlon - math.radians(c.lon))
        if cos_d > best_cos:
            best_cos = cos_d
            best = c

    if best is None:
        return None
    cos_d = max(-1.0, min(1.0, best_cos))
    dist_km = 6371.0088 * math.acos(cos_d)
    return GeoPlace(
        lat=lat, lon=lon,
        city=best.name, admin=best.admin or None,
        country=best.country or None, country_code=best.cc or None,
        distance_km=round(dist_km, 1),
    )


def dataset_size() -> int:
    return len(_load())
