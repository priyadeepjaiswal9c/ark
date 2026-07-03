"""Enrichment — turn raw extracted fields into resolved, rule-ready metadata.

Right now this means offline reverse geocoding (lat/lon → place). It is the seam
where the optional AI/semantic layer (content tags, people) plugs in later
without touching extraction or organization.
"""

from __future__ import annotations

from . import geocode
from .models import Asset, GeoPlace

# Beyond this distance from the nearest known city we don't trust the name.
# (The bundled dataset is sparse; a far "nearest" would be misleading.) The
# coordinates are always kept — only the resolved name is withheld.
MAX_TRUST_KM = 120.0


def enrich(asset: Asset) -> Asset:
    if asset.lat is not None and asset.lon is not None:
        place = geocode.reverse(asset.lat, asset.lon)
        if place is not None:
            if place.distance_km is not None and place.distance_km > MAX_TRUST_KM:
                # keep coords, withhold an untrustworthy name
                asset.place = GeoPlace(lat=asset.lat, lon=asset.lon, distance_km=place.distance_km)
            else:
                asset.place = place
                if place.city and place.city.lower() not in [t.lower() for t in asset.tags]:
                    asset.tags.append(place.city)
    return asset
