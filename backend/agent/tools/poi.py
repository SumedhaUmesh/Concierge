"""
Nominatim (OpenStreetMap) client for finding Points of Interest near a location.

Uses the /search endpoint with a bounding-box viewbox — no API key required.
Results are cached in-memory per (category, rounded lat/lon) for the session.
"""

import logging
import math
from dataclasses import dataclass, field
from typing import Optional

import aiohttp

log = logging.getLogger(__name__)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_HEADERS = {"User-Agent": "Concierge/1.0 (portfolio demo)"}

# amenity= values for structured Nominatim search
_CATEGORY_AMENITIES = {
    "fuel":  ["fuel"],
    "food":  ["restaurant", "cafe", "fast_food", "food_court", "ice_cream", "bar", "pub"],
    "rest":  ["rest_area", "services"],
}

# Free-text q= terms added on top of amenity search (for shop= OSM tags)
_CATEGORY_FREETEXT = {
    "food": ["supermarket", "grocery store", "bakery", "convenience store"],
}

_cache: dict = {}


@dataclass
class POI:
    name: str
    distance_km: float
    lat: float
    lng: float
    address: str
    cuisine: str = ""


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    dLat = math.radians(lat2 - lat1)
    dLon = math.radians(lon2 - lon1)
    a = (math.sin(dLat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dLon / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _bbox(lat: float, lon: float, radius_km: float):
    """Return (west, north, east, south) bounding box for Nominatim viewbox."""
    delta_lat = radius_km / 111.0
    delta_lon = radius_km / (111.0 * math.cos(math.radians(lat)))
    return lon - delta_lon, lat + delta_lat, lon + delta_lon, lat - delta_lat


async def find_poi(
    category: str,
    lat: float,
    lon: float,
    radius_km: float = 20.0,
    limit: int = 5,
    route: Optional[list] = None,
    route_threshold_km: float = 0.8,
) -> list[POI]:
    """
    Return up to `limit` POIs sorted by distance.
    If `route` is provided (list of (lat,lon) tuples), only POIs within
    `route_threshold_km` of the route are returned — ignoring those that
    are nearby but off the driver's actual path.
    """
    key = (category, round(lat, 2), round(lon, 2))
    if key in _cache and _cache[key]:
        return _cache[key]

    amenities = _CATEGORY_AMENITIES.get(category, ["fuel"])
    west, north, east, south = _bbox(lat, lon, radius_km)
    viewbox = f"{west:.4f},{north:.4f},{east:.4f},{south:.4f}"

    all_pois: list[POI] = []

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=10),
        headers=NOMINATIM_HEADERS,
    ) as session:
        # Structured amenity searches
        queries = [{"amenity": a, "format": "json", "limit": 30,
                    "viewbox": viewbox, "bounded": 1, "extratags": 1, "namedetails": 1}
                   for a in amenities]
        # Free-text searches for shop= types (supermarket, grocery, bakery…)
        for term in _CATEGORY_FREETEXT.get(category, []):
            queries.append({"q": term, "format": "json", "limit": 15,
                            "viewbox": viewbox, "bounded": 1, "extratags": 1, "namedetails": 1})

        for params in queries:
            try:
                async with session.get(NOMINATIM_URL, params=params) as resp:
                    resp.raise_for_status()
                    results = await resp.json(content_type=None)

                label = params.get("amenity") or params.get("q")
                log.info("Nominatim[%s/%s]: %d results", category, label, len(results))

                for r in results:
                    if not r or not isinstance(r, dict):
                        continue
                    name = r.get("namedetails", {}).get("name") or r.get("display_name", "").split(",")[0]
                    if not name or len(name) < 2:
                        continue
                    try:
                        poi_lat, poi_lon = float(r["lat"]), float(r["lon"])
                    except (KeyError, ValueError):
                        continue
                    dist = _haversine(lat, lon, poi_lat, poi_lon)
                    if dist > radius_km:
                        continue
                    extra = r.get("extratags") or {}
                    cuisine = extra.get("cuisine", "").replace(";", ", ").replace("_", " ")
                    # Use shop type as cuisine label when no cuisine tag
                    if not cuisine:
                        shop_type = extra.get("shop", "")
                        if shop_type:
                            cuisine = shop_type.replace("_", " ").title()
                    address_parts = r.get("display_name", "").split(",")
                    address = ", ".join(p.strip() for p in address_parts[1:3]) if len(address_parts) > 1 else ""
                    all_pois.append(POI(
                        name=name,
                        distance_km=round(dist, 1),
                        lat=poi_lat,
                        lng=poi_lon,
                        address=address,
                        cuisine=cuisine,
                    ))
            except Exception as exc:
                log.warning("Nominatim request failed for %s: %s", params.get("amenity") or params.get("q"), exc)

    all_pois.sort(key=lambda p: p.distance_km)

    # Deduplicate by name
    seen: set[str] = set()
    unique: list[POI] = []
    for p in all_pois:
        if p.name not in seen:
            seen.add(p.name)
            unique.append(p)

    # Route-aware filtering: keep only POIs on or near the route
    if route and len(route) > 1:
        from agent.tools.route import distance_to_route
        on_route = [p for p in unique
                    if distance_to_route(p.lat, p.lng, route) <= route_threshold_km]
        if on_route:
            log.info("POI[%s] route-filtered: %d/%d on route (±%.1f km)",
                     category, len(on_route), len(unique), route_threshold_km)
            unique = on_route
        else:
            log.info("POI[%s] no results on route — using radius fallback", category)

    result = unique[:limit * 2]  # keep more candidates for preference matching
    log.info("POI[%s] found %d results near (%.3f,%.3f)", category, len(result), lat, lon)
    _cache[key] = result
    return result
