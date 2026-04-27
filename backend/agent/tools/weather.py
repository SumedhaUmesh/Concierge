"""
Open-Meteo weather client — current conditions + 2-hour forecast.
Cached for 10 minutes per (rounded lat/lon) to avoid rate-limiting.
"""

import logging
import time
from dataclasses import dataclass
from typing import Optional

import aiohttp

log = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
_CACHE_TTL = 600   # seconds

_cache: dict = {}  # key → (timestamp, Forecast)


@dataclass
class Forecast:
    condition: str          # e.g. "Clear", "Rain", "Overcast"
    temp_c: float
    wind_kmh: float
    precip_prob_pct: int    # next-2-hour precipitation probability
    rain_in_hours: Optional[float]  # None if no rain expected


# WMO weather code → human label
_WMO_LABELS = {
    0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Icy fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow",
    80: "Showers", 81: "Heavy showers", 82: "Violent showers",
    95: "Thunderstorm",
}


def _cache_key(lat: float, lon: float) -> tuple:
    return (round(lat, 2), round(lon, 2))


async def get_forecast(lat: float, lon: float, hours_ahead: int = 2) -> Optional[Forecast]:
    key = _cache_key(lat, lon)
    now = time.monotonic()
    if key in _cache:
        ts, cached = _cache[key]
        if now - ts < _CACHE_TTL:
            return cached

    params = {
        "latitude": lat,
        "longitude": lon,
        "current": "temperature_2m,wind_speed_10m,weather_code",
        "hourly": "precipitation_probability,weather_code",
        "forecast_hours": max(hours_ahead + 1, 3),
        "wind_speed_unit": "kmh",
        "timezone": "auto",
    }

    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=8)
        ) as session:
            async with session.get(OPEN_METEO_URL, params=params) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
    except Exception:
        log.exception("Open-Meteo request failed for (%.3f, %.3f)", lat, lon)
        return None

    current = data.get("current", {})
    hourly = data.get("hourly", {})

    wmo = current.get("weather_code", 0)
    condition = _WMO_LABELS.get(wmo, f"Code {wmo}")
    temp = current.get("temperature_2m", 0.0)
    wind = current.get("wind_speed_10m", 0.0)

    precip_probs = hourly.get("precipitation_probability", [0] * (hours_ahead + 1))
    hourly_codes = hourly.get("weather_code", [0] * (hours_ahead + 1))

    # Average precip prob over the next N hours
    avg_precip = int(sum(precip_probs[:hours_ahead]) / max(len(precip_probs[:hours_ahead]), 1))

    # Estimate when rain starts (first hour with prob > 60%)
    rain_in_hours = None
    for i, prob in enumerate(precip_probs[:hours_ahead]):
        if prob > 60:
            rain_in_hours = float(i)
            break

    forecast = Forecast(
        condition=condition,
        temp_c=round(temp, 1),
        wind_kmh=round(wind, 1),
        precip_prob_pct=avg_precip,
        rain_in_hours=rain_in_hours,
    )
    _cache[key] = (now, forecast)
    log.info("Weather[%.3f,%.3f]: %s %.1f°C precip=%d%%", lat, lon, condition, temp, avg_precip)
    return forecast
