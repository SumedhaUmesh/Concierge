"""
Server-side tool execution and suggestion enrichment.

The agent decides WHAT to look up (via suggested_action); the server
actually runs the HTTP call and pins real places/data onto the card.
This keeps the model prompt simple and avoids giving the LLM internet access.
"""

import logging
from dataclasses import asdict

from agent.tools.poi import find_poi
from agent.tools.weather import get_forecast
from signals import Signal, Suggestion

log = logging.getLogger(__name__)


async def enrich(suggestion: Suggestion, state: Signal) -> Suggestion:
    """
    Fill in suggestion.enriched_action based on suggestion.suggested_action.
    Returns the same suggestion object (mutated in place) for convenience.
    """
    action = suggestion.suggested_action

    if action.startswith("find_poi:"):
        category = action.split(":", 1)[1]
        pois = await find_poi(category, state.lat, state.lng, radius_km=25.0, limit=3)

        if not pois:
            log.info("enrich: no POIs found for %s", category)
            return suggestion

        # For fuel: prefer a station reachable within range
        if category == "fuel":
            reachable = [p for p in pois if p.distance_km < state.range_km * 0.8]
            best = reachable[0] if reachable else pois[0]
        else:
            best = pois[0]

        suggestion.enriched_action = {
            "type": "navigate",
            "label": f"Navigate to {best.name}",
            "place_name": best.name,
            "distance_km": best.distance_km,
            "lat": best.lat,
            "lng": best.lng,
            "address": best.address,
        }

        # Rewrite headline/detail with real place name
        if category == "fuel":
            suggestion.headline = f"Fuel stop: {best.name}, {best.distance_km:.0f} km"
            suggestion.detail = (
                f"Range is {state.range_km:.0f} km — "
                f"{best.name} at {best.address or 'on route'} is closest."
            )
        elif category == "food":
            suggestion.headline = f"Food: {best.name}, {best.distance_km:.0f} km ahead"
            suggestion.detail = f"{best.name} is the closest option. You've been driving {state.hours_since_meal:.0f}+ hours since eating."
        elif category == "rest":
            suggestion.headline = f"Rest stop ahead: {best.distance_km:.0f} km"
            suggestion.detail = f"{best.name} — good time to stretch."

        log.info("enrich[%s]: pinned to %s (%.1fkm)", category, best.name, best.distance_km)

    elif action == "check_weather":
        forecast = await get_forecast(state.lat, state.lng)
        if forecast is None:
            return suggestion

        if forecast.rain_in_hours is not None:
            mins = int(forecast.rain_in_hours * 60)
            suggestion.enriched_action = {
                "type": "cabin_action",
                "label": "Close windows & start AC",
                "action": "close_windows_ac",
            }
            suggestion.headline = f"Rain in ~{mins} min — close windows"
            suggestion.detail = (
                f"{forecast.condition} approaching. "
                f"Windows {'and sunroof' if state.sunroof_open else ''} are open."
            )
        else:
            suggestion.enriched_action = {
                "type": "info",
                "label": f"{forecast.condition}, {forecast.temp_c:.0f}°C",
            }

    return suggestion
