"""
Overpass API client for finding Points of Interest near a location.

Results are cached in-memory per (category, rounded lat/lon, radius)
for the duration of the session to avoid redundant HTTP calls.
"""

import asyncio
import logging
import math
from dataclasses import dataclass
from typing import Optional

import aiohttp

log = logging.getLogger(__name__)

OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# Maps category name → Overpass tag filters
_CATEGORY_QUERIES = {
    "fuel":      '[amenity=fuel]',
    "food":      '[amenity~"restaurant|cafe|fast_food"]',
    "rest":      '[highway=services]',
    "ev_charge": '[amenity=charging_station]',
    "service":   '[shop=car_repair]',
}

_cache: dict = {}


@dataclass
class POI:
    name: str
    distance_km: float
    lat: float
    lng: float
    address: str


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return distance in km between two WGS-84 points."""
    R = 6371.0
    dLat = math.radians(lat2 - lat1)
    dLon = math.radians(lon2 - lon1)
    a = (math.sin(dLat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dLon / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _cache_key(category: str, lat: float, lon: float, radius_km: float) -> tuple:
    # Round to 2 decimal places (~1.1km grid) for cache hits
    return (category, round(lat, 2), round(lon, 2), radius_km)


def _build_query(category: str, lat: float, lon: float, radius_m: int) -> str:
    tag = _CATEGORY_QUERIES.get(category, '[amenity=fuel]')
    return f"""
[out:json][timeout:10];
(
  node{tag}(around:{radius_m},{lat},{lon});
  way{tag}(around:{radius_m},{lat},{lon});
);
out center 10;
"""


def _parse_element(elem: dict, car_lat: float, car_lon: float) -> Optional[POI]:
    tags = elem.get("tags", {})
    name = tags.get("name") or tags.get("brand") or tags.get("operator")
    if not name:
        return None

    if elem["type"] == "node":
        lat, lng = elem["lat"], elem["lon"]
    else:
        center = elem.get("center", {})
        lat, lng = center.get("lat"), center.get("lon")
        if lat is None:
            return None

    dist = _haversine(car_lat, car_lon, lat, lng)
    street = tags.get("addr:street") or tags.get("addr:full") or ""
    city = tags.get("addr:city") or ""
    address = ", ".join(filter(None, [street, city])) or "on route"

    return POI(name=name, distance_km=round(dist, 1), lat=lat, lng=lng, address=address)


async def find_poi(
    category: str,
    lat: float,
    lon: float,
    radius_km: float = 20.0,
    limit: int = 5,
) -> list[POI]:
    """Return up to `limit` POIs of the given category sorted by distance."""
    key = _cache_key(category, lat, lon, radius_km)
    if key in _cache:
        return _cache[key]

    query = _build_query(category, lat, lon, int(radius_km * 1000))

    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=12)
        ) as session:
            async with session.post(OVERPASS_URL, data={"data": query}) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
    except Exception:
        log.exception("Overpass request failed for category=%s", category)
        return []

    pois: list[POI] = []
    for elem in data.get("elements", []):
        poi = _parse_element(elem, lat, lon)
        if poi:
            pois.append(poi)

    pois.sort(key=lambda p: p.distance_km)
    result = pois[:limit]
    _cache[key] = result
    log.info("POI[%s] found %d results near (%.3f,%.3f)", category, len(result), lat, lon)
    return result
