"""
Server-side tool execution and suggestion enrichment.

The agent decides WHAT to look up (via suggested_action); the server
actually runs the HTTP call and pins real places/data onto the card.
This keeps the model prompt simple and avoids giving the LLM internet access.
"""

import logging
from dataclasses import asdict

from agent.tools.poi import find_poi
from agent.tools.route import get_route
from agent.tools.weather import get_forecast
from signals import Signal, Suggestion

log = logging.getLogger(__name__)


_TYPE_ACTION_OVERRIDE = {
    "cabin":    "check_weather",
    "range":    "find_poi:fuel",
    "meal":     "find_poi:food",
    "rest":     "find_poi:rest",
    "schedule": "none",
    "music":    "none",
}


async def enrich(suggestion: Suggestion, state: Signal) -> Suggestion:
    """
    Fill in suggestion.enriched_action based on suggestion.suggested_action.
    Returns the same suggestion object (mutated in place) for convenience.
    """
    # Small LLM sometimes picks the wrong suggested_action — override by type.
    action = _TYPE_ACTION_OVERRIDE.get(suggestion.type, suggestion.suggested_action)

    if action.startswith("find_poi:"):
        category = action.split(":", 1)[1]

        # Rest stop: use the pre-computed state fields first — avoids unreliable
        # Nominatim search for "rest_area" tags which rarely exist in urban areas.
        if category == "rest" and state.next_rest_stop_lat and state.next_rest_stop_lng:
            km = round(state.next_rest_stop_km or 0)
            suggestion.enriched_action = {
                "type": "navigate",
                "label": f"Navigate to rest stop ({km} km)",
                "lat": state.next_rest_stop_lat,
                "lng": state.next_rest_stop_lng,
            }
            suggestion.headline = f"Rest stop {km} km ahead — take a break"
            suggestion.detail = "You've been driving for over 2 hours. Pull over and recharge."
            log.info("enrich[rest]: using state rest stop (%.1f km)", state.next_rest_stop_km or 0)
            return suggestion

        # Fetch route if destination is known, so POIs are on-the-way not just nearby
        route = None
        if state.destination_lat and state.destination_lng:
            route = await get_route(state.lat, state.lng,
                                    state.destination_lat, state.destination_lng)

        pois = await find_poi(category, state.lat, state.lng,
                              radius_km=25.0, limit=3, route=route)

        if not pois:
            log.info("enrich: no POIs found for %s", category)
            if category == "rest":
                suggestion.enriched_action = {
                    "type": "info",
                    "label": "Pull over safely and take a break",
                }
            return suggestion

        # For fuel: prefer a station reachable within range
        if category == "fuel":
            reachable = [p for p in pois if p.distance_km < (state.range_km or 999) * 0.8]
            best = reachable[0] if reachable else pois[0]
        else:
            best = pois[0]

        suggestion.enriched_action = {
            "type": "navigate",
            "label": f"Open in Google Maps — {best.name}",
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
        # Prefer the state's rain signal (set by scenario or live weather loop)
        state_rain = getattr(state, "rain_in_minutes", None)
        if state_rain is not None:
            windows_open = getattr(state, "windows_open", False)
            sunroof_open = getattr(state, "sunroof_open", False)
            opening = "windows and sunroof" if windows_open and sunroof_open else ("sunroof" if sunroof_open else "windows")
            suggestion.enriched_action = {
                "type": "cabin_action",
                "label": f"Close {opening} & start AC",
                "action": "close_windows_ac",
            }
            suggestion.headline = f"Rain in ~{state_rain} min — close {opening}"
            suggestion.detail = f"Rain is approaching and your {opening} are open."
        else:
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
                    f"Windows {'and sunroof' if getattr(state, 'sunroof_open', False) else ''} are open."
                )
            else:
                suggestion.enriched_action = {
                    "type": "info",
                    "label": f"{forecast.condition}, {forecast.temp_c:.0f}°C",
                }

    return suggestion
