"""
OSRM route client — free, no API key required.

Fetches a driving route between two GPS points and returns the polyline
as a list of (lat, lon) tuples. Used by the POI tool to filter results
to places that are actually on the driver's route.
"""

import asyncio
import logging
import math
from typing import Optional

import aiohttp

log = logging.getLogger(__name__)

OSRM_URL = "http://router.project-osrm.org/route/v1/driving"

_route_cache: dict[tuple, list] = {}
_duration_cache: dict[tuple, float] = {}


async def get_route(
    origin_lat: float, origin_lon: float,
    dest_lat: float, dest_lon: float,
) -> Optional[list[tuple[float, float]]]:
    """
    Return driving route as [(lat, lon), ...] or None on failure.
    Results are cached by rounded endpoints so re-queries are free.
    """
    key = (round(origin_lat, 3), round(origin_lon, 3),
           round(dest_lat, 3), round(dest_lon, 3))
    if key in _route_cache:
        return _route_cache[key]

    url = f"{OSRM_URL}/{origin_lon},{origin_lat};{dest_lon},{dest_lat}"
    for attempt in range(2):
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=8)
            ) as session:
                async with session.get(url, params={"overview": "full", "geometries": "geojson"}) as resp:
                    resp.raise_for_status()
                    data = await resp.json()

            route_data = data["routes"][0]
            coords = route_data["geometry"]["coordinates"]
            route = [(lat, lon) for lon, lat in coords]
            duration_min = route_data["duration"] / 60.0
            log.info("OSRM route: %d points, %.1f km, %.0f min",
                     len(route), route_data["distance"] / 1000, duration_min)
            _route_cache[key] = route
            _duration_cache[key] = duration_min
            return route

        except Exception as exc:
            if attempt == 0:
                log.warning("OSRM route attempt 1 failed (%s) — retrying", exc)
                await asyncio.sleep(1.0)
            else:
                log.warning("OSRM route request failed (%s) — falling back to radius search", exc)
    return None


async def get_travel_time(
    origin_lat: float, origin_lon: float,
    dest_lat: float, dest_lon: float,
) -> Optional[float]:
    """
    Return estimated driving time in minutes between two points.
    Uses a lightweight OSRM call (no geometry) — faster than get_route.
    Returns cached value if the same pair was already routed.
    """
    key = (round(origin_lat, 3), round(origin_lon, 3),
           round(dest_lat, 3), round(dest_lon, 3))
    if key in _duration_cache:
        return _duration_cache[key]

    url = f"{OSRM_URL}/{origin_lon},{origin_lat};{dest_lon},{dest_lat}"
    for attempt in range(2):
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=6)
            ) as session:
                async with session.get(url, params={"overview": "false"}) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
            minutes = data["routes"][0]["duration"] / 60.0
            _duration_cache[key] = minutes
            log.info("OSRM travel time: %.0f min", minutes)
            return minutes
        except Exception as exc:
            if attempt == 0:
                log.warning("OSRM travel time attempt 1 failed (%s) — retrying", exc)
                await asyncio.sleep(1.0)
            else:
                log.warning("OSRM travel time failed (%s)", exc)
    return None


def point_to_segment_dist(
    p_lat: float, p_lon: float,
    a_lat: float, a_lon: float,
    b_lat: float, b_lon: float,
) -> float:
    """
    Minimum distance in km from point P to line segment AB.
    Uses flat-earth approximation (accurate enough for <50 km segments).
    """
    # Convert to approximate metres using local projection
    cos_lat = math.cos(math.radians((a_lat + b_lat) / 2))
    ax = a_lon * cos_lat;  ay = a_lat
    bx = b_lon * cos_lat;  by = b_lat
    px = p_lon * cos_lat;  py = p_lat

    dx = bx - ax;  dy = by - ay
    seg_len2 = dx * dx + dy * dy

    if seg_len2 == 0:
        t = 0.0
    else:
        t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg_len2))

    nearest_x = ax + t * dx
    nearest_y = ay + t * dy

    dlat = math.radians(py - nearest_y)
    dlon = math.radians((px - nearest_x) / cos_lat)
    a = math.sin(dlat / 2) ** 2 + math.sin(dlon / 2) ** 2
    return 6371.0 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def distance_to_route(
    poi_lat: float, poi_lon: float,
    route: list[tuple[float, float]],
) -> float:
    """Return minimum distance in km from a POI to any segment of the route."""
    min_dist = float("inf")
    for i in range(len(route) - 1):
        d = point_to_segment_dist(
            poi_lat, poi_lon,
            route[i][0], route[i][1],
            route[i + 1][0], route[i + 1][1],
        )
        if d < min_dist:
            min_dist = d
    return min_dist
