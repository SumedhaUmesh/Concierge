import asyncio
import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from simulator import Simulator
from signals import Suggestion
from agent.loop import AgentLoop
from obd_source import obd_source
from tool_router import enrich
from agent import music as music_mod
from agent.voice import tts, asr, classifier
from agent import llm as llm_mod, cloud as cloud_mod
from agent.prompts import VEHICLE_QA_SYSTEM, VEHICLE_QA_USER
from agent.intent_engine import decompose as decompose_intents
from agent.orchestrator import orchestrate, fallback_clarify
from agent.conversation import ConversationBuffer
import trip_memory
import calendar_source

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = FastAPI(title="Concierge")
sim = Simulator()

DASHBOARD = Path(__file__).parent.parent / "dashboard"

app.mount("/static", StaticFiles(directory=str(DASHBOARD / "static")), name="static")


@app.get("/")
async def root():
    return FileResponse(str(DASHBOARD / "index.html"))


@app.post("/sim/state")
async def sim_set_state(body: dict):
    """Set any simulator state field(s) and broadcast to all clients."""
    for key, value in body.items():
        if hasattr(sim.state, key):
            setattr(sim.state, key, value)
    await sim.broadcast()
    return {"ok": True}


@app.post("/agent/reset")
async def agent_reset():
    """Reset cooldown on all active agent loops so the next gate tick can fire immediately."""
    for agent in sim._agents:
        agent.reset_cooldown()
    return {"ok": True, "agents_reset": len(sim._agents)}


@app.post("/sim/reset")
async def sim_reset():
    """Hard reset: restore all simulator state to defaults and clear agent streaks."""
    await sim.reset()
    for agent in sim._agents:
        agent.reset_cooldown()
        agent._accept_streak  = 0
        agent._dismiss_streak = 0
        agent._adaptive_cooldown = 180.0
        agent.driver_trust    = 0.5
    # Clear POI cache so next scenario fetches fresh results
    from agent.tools import poi as _poi_mod  # noqa: PLC0415
    _poi_mod._cache.clear()
    # Tell all JS clients to reset their UI state (clear music, meal, markers)
    await _broadcast_ws_all({"type": "reset_ui"})
    return {"ok": True}


@app.post("/obd/connect")
async def obd_connect(body: dict = {}):
    port = body.get("port")  # None = auto-detect
    ok = await asyncio.to_thread(obd_source.connect, port)
    return {"ok": ok, **obd_source.status_dict()}


@app.post("/obd/disconnect")
async def obd_disconnect():
    await asyncio.to_thread(obd_source.disconnect)
    return {"ok": True}


@app.get("/obd/status")
async def obd_status():
    return obd_source.status_dict()


@app.get("/route")
async def get_route_polyline(
    from_lat: float, from_lng: float, to_lat: float, to_lng: float
):
    """Fetch OSRM driving route and return as [[lat,lng], ...] for Leaflet."""
    from agent.tools.route import get_route  # noqa: PLC0415
    points = await get_route(from_lat, from_lng, to_lat, to_lng)
    if not points:
        return {"ok": False, "points": []}
    return {"ok": True, "points": points}


@app.post("/calendar/sync")
async def calendar_sync():
    event = await _do_calendar_sync()
    await sim.broadcast()
    return {"ok": True, "event": event}


@app.get("/geocode/reverse")
async def reverse_geocode(lat: float, lng: float):
    """Nominatim reverse geocode — returns a short human-readable label."""
    import aiohttp  # noqa: PLC0415
    url = "https://nominatim.openstreetmap.org/reverse"
    params = {"lat": lat, "lon": lng, "format": "json", "zoom": 14}
    headers = {"User-Agent": "Concierge/1.0 (portfolio demo)"}
    try:
        async with aiohttp.ClientSession(headers=headers,
                                         timeout=aiohttp.ClientTimeout(total=5)) as s:
            async with s.get(url, params=params) as resp:
                data = await resp.json(content_type=None)
        addr = data.get("address", {})
        parts = [addr.get("road") or addr.get("suburb"),
                 addr.get("city") or addr.get("town") or addr.get("village"),
                 addr.get("state")]
        label = ", ".join(p for p in parts if p)
        return {"label": label or data.get("display_name", "")[:60]}
    except Exception:
        return {"label": ""}


@app.get("/memory/stats")
async def memory_stats():
    prefs = trip_memory.get_preferences()
    thresholds = trip_memory.get_adaptive_thresholds()
    return {**prefs, "adaptive_thresholds": thresholds}


@app.delete("/memory/reset")
async def memory_reset():
    with trip_memory._lock:
        conn = trip_memory._get_conn()
        conn.execute("DELETE FROM suggestions")
        conn.commit()
    return {"ok": True}


@app.get("/privacy")
async def privacy_report():
    """Privacy-Aware AI: show exactly what is stored and where it goes."""
    prefs = trip_memory.get_preferences()
    return {
        "storage": "local SQLite (backend/trip_memory.db) — never uploaded",
        "what_is_stored": [
            "suggestion type and headline",
            "accepted/dismissed outcome",
            "place name and GPS coordinates (when navigating)",
            "hour of day and day of week",
            "cuisine type (for meal suggestions)",
        ],
        "what_is_NOT_stored": [
            "your voice audio (discarded after transcription)",
            "full trip routes",
            "personal identity",
        ],
        "cloud_calls": [
            "Nominatim reverse geocoding (your GPS coords, no account required)",
            "Open-Meteo weather (your GPS coords, no account required)",
            "OSRM routing (start+end coords, no account required)",
            "Overpass API POI search (bounding box, no account required)",
            "Claude Haiku (only if local LLM answer is too short, requires ANTHROPIC_API_KEY)",
        ],
        "on_device": [
            "LFM2.5-1.2B GGUF — all suggestion generation runs locally",
            "Whisper ASR — voice transcription runs locally",
            "macOS TTS — speech synthesis runs locally",
            "macOS Calendar — read via AppleScript, never leaves device",
        ],
        "total_logged_outcomes": prefs["total_logged"],
        "clear_with": "DELETE /memory/reset",
    }


@app.get("/driver/state")
async def driver_state():
    """Cognitive Driver Model: current computed driver state."""
    from agent.driver_model import compute  # noqa: PLC0415
    ds = compute(sim.state)
    return {
        "fatigue_index": ds.fatigue_index,
        "cognitive_load": ds.cognitive_load,
        "stress_index": ds.stress_index,
        "risk_level": ds.risk_level,
        "minutes_driving": round(sim.state.minutes_driving_continuously),
        "adaptive_cooldown_sec": None,  # filled from agent loop if needed
    }


@app.on_event("startup")
async def _warmup():
    from agent.llm import get_llm  # noqa: PLC0415
    await asyncio.to_thread(get_llm)
    tts.init()
    asyncio.create_task(_broadcast_loop())
    asyncio.create_task(_calendar_sync_loop())
    asyncio.create_task(_weather_loop())
    asyncio.create_task(_music_evolution_loop())
    asyncio.create_task(_meeting_watch_loop())


async def _broadcast_loop():
    """Emit state to all clients every 1.5 s — keeps gauges, bars, and cabin live."""
    while True:
        await sim.broadcast()
        await asyncio.sleep(1.5)


async def _calendar_sync_loop():
    """Sync macOS Calendar every 5 minutes in the background."""
    while True:
        await _do_calendar_sync()
        await asyncio.sleep(300)


async def _do_calendar_sync() -> Optional[dict]:
    event = await calendar_source.get_next_event()
    if event:
        sim.state.next_meeting_title    = event["title"]
        sim.state.next_meeting_time     = event["time"]
        sim.state.next_meeting_location = event["location"] or None
    else:
        sim.state.next_meeting_title    = None
        sim.state.next_meeting_time     = None
        sim.state.next_meeting_location = None
    return event


async def _weather_loop():
    """Poll Open-Meteo every 10 min and update rain_in_minutes from real forecast."""
    while True:
        await _do_weather_update()
        await asyncio.sleep(600)


async def _do_weather_update():
    # Skip if the simulator has scenario-controlled rain
    if sim._scenario_time_frozen:
        return
    try:
        from agent.tools.weather import get_forecast  # noqa: PLC0415
        fc = await get_forecast(sim.state.lat, sim.state.lng)
        if fc is None:
            return
        if fc.rain_in_hours is not None:
            sim.state.rain_in_minutes = max(1, int(fc.rain_in_hours * 60))
            log.info("Weather: rain expected in %d min", sim.state.rain_in_minutes)
        else:
            sim.state.rain_in_minutes = None
    except Exception:
        log.exception("Weather update failed")


async def _broadcast_ws_all(msg: dict) -> None:
    """Send an arbitrary JSON message to every connected WebSocket client in parallel."""
    if not sim._clients:
        return
    payload = json.dumps(msg)
    clients = list(sim._clients)

    async def _send(ws):
        try:
            await ws.send_text(payload)
        except Exception:
            sim._clients.discard(ws)

    await asyncio.gather(*[_send(ws) for ws in clients], return_exceptions=True)


# ── Continuous Adaptation: Dynamic Music Evolution ────────────────────────────

_last_music_energy: int = 0


async def _music_evolution_loop():
    """Re-evaluate music energy every 5 min as fatigue changes.

    Low fatigue → calm music; high fatigue → energetic music to fight sleepiness.
    Only fires if the target energy shifts by ≥2 from the last sent energy.
    """
    global _last_music_energy
    await asyncio.sleep(60)  # let everything settle before first eval
    while True:
        await asyncio.sleep(300)  # 5-minute cycle
        if not sim._clients:
            continue

        fatigue = sim.state.fatigue_index
        if fatigue < 0.3:
            target_energy, mood, msg = 3, "calm relaxed", "Keeping things calm and easy."
        elif fatigue < 0.5:
            target_energy, mood, msg = 4, "smooth upbeat", "Shifting to something a little more upbeat to keep you fresh."
        elif fatigue < 0.7:
            target_energy, mood, msg = 6, "upbeat energetic", "Boosting the energy a bit — you've been driving a while."
        elif fatigue < 0.85:
            target_energy, mood, msg = 7, "energetic driving", "Bringing up the energy to help you stay alert."
        else:
            target_energy, mood, msg = 8, "driving pumped",  "High-energy music — let's keep you awake and focused."

        if abs(target_energy - _last_music_energy) < 2:
            continue  # not a meaningful shift — skip

        _last_music_energy = target_energy
        tracks = music_mod.quick_match(mood, target_energy)
        if not tracks:
            continue

        log.info("Music evolution: fatigue=%.2f → energy=%d mood=%r", fatigue, target_energy, mood)
        await _broadcast_ws_all({
            "type": "music_results",
            "data": {"query": mood, "tracks": tracks},
        })
        asyncio.create_task(tts.speak(msg))


# ── Continuous Adaptation: Conference Call Mode ───────────────────────────────

_call_mode_active: bool = False


async def _meeting_watch_loop():
    """Enter quiet mode 5 min before next calendar meeting, restore after."""
    global _call_mode_active
    await asyncio.sleep(30)  # initial delay
    while True:
        await asyncio.sleep(30)
        try:
            meeting_time = sim.state.next_meeting_time
            if not meeting_time:
                if _call_mode_active:
                    await _exit_call_mode()
                continue

            def _hhmm_to_min(t: str) -> int:
                h, m = t.split(":", 1)
                return int(h) * 60 + int(m)

            now_min = _hhmm_to_min(sim.state.current_time)
            mtg_min = _hhmm_to_min(meeting_time)
            minutes_until = mtg_min - now_min

            if 0 < minutes_until <= 5 and not _call_mode_active:
                await _enter_call_mode(round(minutes_until))
            elif _call_mode_active and (minutes_until <= 0 or minutes_until > 5):
                await _exit_call_mode()
        except Exception:
            log.exception("Meeting watch loop error")


async def _enter_call_mode(minutes_until: int) -> None:
    global _call_mode_active
    _call_mode_active = True
    log.info("Conference call mode: entering (meeting in %d min)", minutes_until)

    # Enqueue speech *before* muting so it still plays
    await tts.speak(
        f"Entering quiet mode — you have a meeting in {minutes_until} minute{'s' if minutes_until != 1 else ''}."
    )
    tts.set_muted(True)

    # Suppress proactive alerts during the call window
    for agent in sim._agents:
        agent.suppress(60)

    # Close windows and turn on AC for a quiet cabin
    sim.state.windows_open = False
    sim.state.sunroof_open = False
    sim.state.ac_on        = True
    await sim.broadcast()

    await _broadcast_ws_all({
        "type": "mode_change",
        "data": {"mode": "quiet", "reason": f"Meeting in {minutes_until} min"},
    })


async def _exit_call_mode() -> None:
    global _call_mode_active
    _call_mode_active = False
    log.info("Conference call mode: exiting")
    tts.set_muted(False)
    asyncio.create_task(tts.speak("Quiet mode ended. Welcome back."))
    await _broadcast_ws_all({
        "type": "mode_change",
        "data": {"mode": "normal"},
    })


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    sim.add_client(websocket)

    await websocket.send_text(
        json.dumps({"type": "signal", "data": asdict(sim.state)})
    )

    # Track latest suggestion for voice response routing
    latest_suggestion: list[Suggestion] = []
    # Two-turn meal flow: store POIs after "hungry" until preference is spoken
    pending_meal_pois: list = []
    # Per-session conversation buffer (conversational continuity)
    convo = ConversationBuffer()

    async def _on_suggestion(suggestion: Suggestion):
        # Rest stop: fill from state coords and skip enrich entirely — Nominatim
        # rarely has "rest_area" tags in urban areas, so bypass it completely.
        if (suggestion.type == "rest"
                and sim.state.next_rest_stop_lat
                and sim.state.next_rest_stop_lng):
            km = round(sim.state.next_rest_stop_km or 0)
            suggestion.enriched_action = {
                "type": "navigate",
                "label": f"Open in Google Maps ({km} km)",
                "lat": sim.state.next_rest_stop_lat,
                "lng": sim.state.next_rest_stop_lng,
            }
            suggestion.headline = f"Rest stop {km} km ahead — take a break"
            suggestion.detail   = "You've been driving over 2 hours. Pull over and recharge."
            log.info("_on_suggestion: rest stop pinned (%.3f, %.3f)",
                     sim.state.next_rest_stop_lat, sim.state.next_rest_stop_lng)
        elif suggestion.enriched_action:
            # Already enriched (e.g. from _handle_meal_preference) — don't overwrite
            log.info("_on_suggestion: enriched_action already set, skipping enrich()")
        else:
            try:
                await enrich(suggestion, sim.state)
            except Exception:
                log.exception("Tool enrichment failed")

        latest_suggestion.clear()
        latest_suggestion.append(suggestion)

        payload = json.dumps({"type": "suggestion", "data": asdict(suggestion)})
        try:
            await websocket.send_text(payload)
        except Exception:
            pass

        # Read headline aloud
        asyncio.create_task(tts.speak(suggestion.headline))

    agent = AgentLoop(on_suggestion=_on_suggestion)
    sim.add_agent(agent)

    try:
        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            kind = msg.get("type")

            if kind == "reset":
                await sim.reset()
            elif kind == "gps_update":
                sim.state.lat = msg["lat"]
                sim.state.lng = msg["lng"]
                if msg.get("label"):
                    sim.state.location_label = msg["label"]
            elif kind == "user_dismiss":
                agent.dismiss()
                pending_meal_pois.clear()  # abort any in-progress meal flow
                if latest_suggestion:
                    trip_memory.log_outcome(latest_suggestion[0], "dismissed", sim.state)
            elif kind == "user_accept":
                agent.accept()
                pending_meal_pois.clear()  # meal flow completed
                # Apply cabin state changes immediately
                cabin_action = msg.get("action")
                if cabin_action in ("close_windows", "close_windows_ac"):
                    sim.state.windows_open = False
                    sim.state.sunroof_open = False
                    sim.state.ac_on        = True
                    sim.state.rain_in_minutes = None
                elif cabin_action == "turn_on_ac":
                    sim.state.ac_on = True
                elif cabin_action == "open_windows":
                    sim.state.windows_open = True
                if latest_suggestion:
                    trip_memory.log_outcome(latest_suggestion[0], "accepted", sim.state)
                await sim.broadcast()
            elif kind == "mute":
                tts.set_muted(msg.get("muted", False))
            elif kind == "music_query":
                asyncio.create_task(_handle_music(websocket, msg.get("query", "")))
            elif kind == "voice_input":
                asyncio.create_task(
                    _handle_voice(websocket, agent, latest_suggestion, pending_meal_pois, _on_suggestion, convo, msg.get("audio", ""))
                )
    except WebSocketDisconnect:
        sim.remove_client(websocket)
        sim.remove_agent(agent)


async def _handle_voice(
    websocket: WebSocket,
    agent: AgentLoop,
    latest_suggestion: list,
    pending_meal_pois: list,
    on_suggestion,
    convo: ConversationBuffer,
    audio_b64: str,
):
    transcript = await asr.transcribe(audio_b64)
    if not transcript:
        return

    convo.add_user(transcript)

    try:
        await websocket.send_text(json.dumps({
            "type": "transcript",
            "data": {"text": transcript},
        }))
    except Exception:
        pass

    # Mid meal-preference flow takes priority
    if pending_meal_pois:
        await _handle_meal_preference(websocket, pending_meal_pois, transcript, on_suggestion)
        return

    intent = await classifier.classify(transcript)

    if intent == "accept":
        agent.accept()
        try:
            await websocket.send_text(json.dumps({"type": "user_accept"}))
        except Exception:
            pass

    elif intent == "dismiss":
        agent.dismiss()
        try:
            await websocket.send_text(json.dumps({"type": "user_dismiss"}))
        except Exception:
            pass

    elif intent == "defer":
        agent.dismiss()
        asyncio.create_task(tts.speak("Got it. I'll check back in a few minutes."))

    else:
        # All other inputs — natural language, vague, emotional, compound — go to orchestrator
        asyncio.create_task(
            _handle_orchestrated(websocket, agent, pending_meal_pois, on_suggestion, convo, transcript)
        )


async def _handle_hungry(
    websocket: WebSocket,
    agent: AgentLoop,
    pending_meal_pois: list,
    state,
):
    from agent.tools.poi import find_poi
    from agent.tools.route import get_route

    radius_km = min(state.range_km * 0.8, 25.0)

    # Fetch route if destination is set so we only suggest on-the-way food
    route = None
    if state.destination_lat and state.destination_lng:
        route = await get_route(state.lat, state.lng,
                                state.destination_lat, state.destination_lng)

    pois = await find_poi("food", state.lat, state.lng,
                          radius_km=radius_km, route=route)

    if not pois:
        asyncio.create_task(tts.speak("I couldn't find any restaurants within range right now."))
        return

    pending_meal_pois.clear()
    pending_meal_pois.extend(pois[:6])

    # Collect unique cuisines for the preference question
    cuisines = []
    for p in pending_meal_pois:
        for c in p.cuisine.split(", "):
            c = c.strip().title()
            if c and c not in cuisines:
                cuisines.append(c)
    cuisines = cuisines[:4]

    if cuisines:
        options = ", ".join(cuisines[:-1]) + f" or {cuisines[-1]}" if len(cuisines) > 1 else cuisines[0]
        question = f"I found {len(pois)} restaurants within {round(radius_km)} km. What are you in the mood for? I see {options}."
    else:
        names = [p.name for p in pending_meal_pois[:3]]
        options = ", ".join(names[:-1]) + f" or {names[-1]}" if len(names) > 1 else names[0]
        question = f"I found some restaurants nearby — {options}. Which sounds good?"

    asyncio.create_task(tts.speak(question))

    # Send options to dashboard
    try:
        await websocket.send_text(json.dumps({
            "type": "meal_options",
            "data": {
                "question": question,
                "pois": [
                    {"name": p.name, "distance_km": p.distance_km,
                     "cuisine": p.cuisine, "lat": p.lat, "lng": p.lng}
                    for p in pending_meal_pois
                ],
            },
        }))
    except Exception:
        pass


# Maps food words the driver might say → Nominatim cuisine/name tags to search
_FOOD_KEYWORD_SYNONYMS: dict[str, list[str]] = {
    "sandwich": ["sandwich", "deli", "sub", "subway", "hoagie", "jersey", "quiznos"],
    "burger":   ["burger", "hamburger", "fast_food", "mcdonald", "wendy", "in-n-out", "shake shack"],
    "pizza":    ["pizza", "italian"],
    "sushi":    ["sushi", "japanese", "ramen", "izakaya"],
    "salad":    ["salad", "vegan", "vegetarian", "healthy", "bowl"],
    "coffee":   ["coffee", "cafe", "espresso", "starbucks", "peet"],
    "taco":     ["mexican", "taco", "burrito", "chipotle"],
    "chinese":  ["chinese", "dim_sum", "noodle", "dumpling"],
    "indian":   ["indian", "curry"],
    "thai":     ["thai"],
    "korean":   ["korean", "bbq"],
    "greek":    ["greek", "mediterranean"],
    "breakfast": ["breakfast", "brunch", "pancake", "waffle", "diner"],
}


async def _handle_meal_preference(
    websocket: WebSocket,
    pending_meal_pois: list,
    transcript: str,
    on_suggestion,
):
    lower = transcript.lower()
    matched = None
    keyword_tried: Optional[str] = None

    # Match restaurant name words first (handles "let's go to Erewhon")
    for poi in pending_meal_pois:
        words = [w.strip("'s.,") for w in poi.name.lower().split()]
        if any(w and w in lower for w in words):
            matched = poi
            break

    # Fall back to cuisine match ("something Italian", "the Mexican place")
    if not matched:
        for poi in pending_meal_pois:
            if poi.cuisine and any(c.strip().lower() in lower for c in poi.cuisine.split(",")):
                matched = poi
                break

    # Food-type keyword matching: "sandwich", "burger", "pizza", etc.
    if not matched:
        for food_word, synonyms in _FOOD_KEYWORD_SYNONYMS.items():
            if food_word in lower:
                keyword_tried = food_word
                for poi in pending_meal_pois:
                    n = poi.name.lower()
                    c = (poi.cuisine or "").lower()
                    if any(s in n or s in c for s in synonyms):
                        matched = poi
                        break
                if matched:
                    break

    # Determine fallback message before clearing
    tts_msg: str
    if not matched and keyword_tried:
        # Keyword specified but no matching place — honest fallback to nearest
        matched = pending_meal_pois[0]
        tts_msg = (
            f"I don't see any {keyword_tried} places nearby. "
            f"The closest option is {matched.name} — navigating there instead."
        )
    elif not matched:
        matched = pending_meal_pois[0]
        tts_msg = f"Great, heading to {matched.name}."
    elif keyword_tried:
        tts_msg = f"Found it — heading to {matched.name}."
    else:
        tts_msg = f"Great, heading to {matched.name}."

    pending_meal_pois.clear()
    asyncio.create_task(tts.speak(tts_msg))

    # Build the Suggestion directly — no LLM or Overpass re-query needed
    suggestion = Suggestion(
        type="meal",
        urgency=3,
        headline=f"Head to {matched.name}",
        detail=f"{matched.cuisine.title() or 'Restaurant'} · {matched.distance_km} km away",
        suggested_action="find_poi:food",
        enriched_action={
            "type": "navigate",
            "label": f"Navigate to {matched.name}",
            "lat": matched.lat,
            "lng": matched.lng,
        },
    )
    await on_suggestion(suggestion)


# ── Emotional fast-path — no LLM needed ──────────────────────────────────────
# Maps keyword groups → pre-built action plans executed without any LLM call.
# This ensures reliable, instant response for the most common emotional states.
_EMOTIONAL_FAST_PATHS = [
    (
        {"tired", "sleepy", "exhausted", "drowsy", "fatigue", "fatigued"},
        {
            "reply":   "You sound tired. I'll cool things down a little and put on something calm.",
            "actions": [
                {"type": "cabin_temp", "celsius": 19},
                {"type": "music",      "mood": "calm",   "energy": 3},
                {"type": "reduce_alerts", "minutes": 20},
            ],
        },
    ),
    (
        {"stressed", "anxious", "nervous", "worried", "edge", "tense"},
        {
            "reply":   "Let me help you unwind — calm music and a comfortable temperature.",
            "actions": [
                {"type": "cabin_temp", "celsius": 21},
                {"type": "music",      "mood": "calm relaxed", "energy": 2},
                {"type": "reduce_alerts", "minutes": 15},
            ],
        },
    ),
    (
        {"energetic", "pumped", "awake", "alert", "excited", "motivated"},
        {
            "reply":   "Let's go! Bringing up the energy.",
            "actions": [
                {"type": "cabin_temp", "celsius": 20},
                {"type": "music",      "mood": "energetic upbeat", "energy": 8},
            ],
        },
    ),
    (
        {"relaxed", "relaxing", "chill", "chilled", "peaceful", "mellow"},
        {
            "reply":   "Perfect. I'll keep things smooth and comfortable.",
            "actions": [
                {"type": "cabin_temp", "celsius": 21},
                {"type": "music",      "mood": "relaxed smooth", "energy": 3},
            ],
        },
    ),
    (
        {"bored", "boring"},
        {
            "reply":   "Let me liven things up a bit.",
            "actions": [
                {"type": "music", "mood": "upbeat groovy", "energy": 7},
            ],
        },
    ),
    (
        {"hot", "warm", "sweating", "stuffy"},
        {
            "reply":   "I'll cool the cabin down for you.",
            "actions": [
                {"type": "cabin_temp", "celsius": 18},
                {"type": "ac", "on": True},
            ],
        },
    ),
    (
        {"cold", "freezing", "chilly", "shivering"},
        {
            "reply":   "I'll warm things up.",
            "actions": [
                {"type": "cabin_temp", "celsius": 24},
                {"type": "ac", "on": True},
            ],
        },
    ),
]


def _emotional_fast_path(transcript: str) -> Optional[dict]:
    """Return a pre-built plan if the transcript matches an emotional keyword, else None."""
    # Strip punctuation so "tired." and "tired!" both match "tired"
    lower = set(w.strip(".,!?;:'\"") for w in transcript.lower().split())
    for keywords, plan in _EMOTIONAL_FAST_PATHS:
        if lower & keywords:
            matched = lower & keywords
            log.info("Emotional fast-path: matched %s in %r", matched, transcript[:40])
            return plan
    return None


async def _handle_orchestrated(
    websocket: WebSocket,
    agent: AgentLoop,
    pending_meal_pois: list,
    on_suggestion,
    convo: ConversationBuffer,
    transcript: str,
) -> None:
    """
    Natural language orchestration: routes any non-control utterance through
    the LLM orchestrator which understands vague/emotional/indirect language
    and returns a coordinated multi-action plan.
    """
    import json as _json
    from dataclasses import asdict as _asdict

    # Fast path: emotional states are handled instantly without any LLM call
    fast_plan = _emotional_fast_path(transcript)
    if fast_plan:
        log.info("Orchestrator: emotional fast-path for %r", transcript[:40])
        await _execute_plan(websocket, agent, pending_meal_pois, on_suggestion, convo,
                            fast_plan, transcript)
        return

    # Intent Decomposition Engine — handle compound/scenic requests without LLM
    intents = decompose_intents(transcript)
    from agent.intent_engine import describe as _describe_intents  # noqa: PLC0415
    if intents:
        log.info("Intents decomposed: %s ← %r", _describe_intents(intents), transcript[:40])

    # Meal intent: bypass LLM — go straight to two-turn cuisine flow
    meal_intent = next((i for i in intents if i.type == "meal"), None)
    if meal_intent:
        log.info("Orchestrator: meal fast-path for %r", transcript[:40])
        asyncio.create_task(_handle_hungry(websocket, agent, pending_meal_pois, sim.state))
        return

    nav_intent = next((i for i in intents if i.type == "navigate"), None)
    if nav_intent and nav_intent.modifiers.get("scenic"):
        scenic_plan = {
            "reply": "I'll find you a scenic route. It may take a bit longer, but the views will be worth it.",
            "actions": [
                {"type": "music", "mood": "warm calm", "energy": 3},
                {"type": "reduce_alerts", "minutes": 20},
            ],
        }
        log.info("Orchestrator: scenic route fast-path")
        await _execute_plan(websocket, agent, pending_meal_pois, on_suggestion, convo,
                            scenic_plan, transcript)
        asyncio.create_task(tts.speak(scenic_plan["reply"]))
        return
    elif nav_intent and nav_intent.modifiers.get("fast"):
        fast_nav_plan = {
            "reply": "Got it — I'll keep you on the fastest route.",
            "actions": [{"type": "reduce_alerts", "minutes": 10}],
        }
        await _execute_plan(websocket, agent, pending_meal_pois, on_suggestion, convo,
                            fast_nav_plan, transcript)
        return

    # Resolve references ("make it closer") using conversation history
    resolved = convo.resolve(transcript)

    # Build context
    state_json  = _json.dumps(_asdict(sim.state), default=str)[:600]
    conv_ctx    = convo.context_str()
    pref_ctx    = trip_memory.get_context_string() or ""

    plan = await orchestrate(resolved, state_json, conv_ctx, pref_ctx)

    if plan is None:
        # Graceful failure — smart clarifying question, not "I don't understand"
        question = fallback_clarify(transcript)
        convo.add_assistant(question)
        asyncio.create_task(tts.speak(question))
        return

    confidence = plan.get("confidence", "high")
    clarify    = plan.get("clarify")

    # If low confidence, ask smart question instead of guessing wrong
    if confidence == "low" and clarify:
        convo.add_assistant(clarify)
        asyncio.create_task(tts.speak(clarify))
        return

    await _execute_plan(websocket, agent, pending_meal_pois, on_suggestion, convo,
                        plan, transcript)


def _normalize_action(action: dict) -> dict:
    """
    The small LLM sometimes returns {"action": "play calming music"} instead of
    {"type": "music", "mood": "calm", "energy": 3}. This maps both formats to the
    canonical type-keyed form so _execute_plan always works correctly.
    """
    if action.get("type") in ("cabin_temp", "music", "find_poi", "navigate",
                               "reduce_alerts", "windows", "ac"):
        return action  # already canonical

    raw = (action.get("action") or action.get("type") or "").lower()

    if any(w in raw for w in ("music", "song", "play", "audio", "track")):
        mood   = str(action.get("mood") or action.get("genre") or "calm")
        energy = action.get("energy") or action.get("level") or 4
        return {"type": "music", "mood": mood, "energy": int(energy)}

    if any(w in raw for w in ("cabin", "temperature", "temp", "cool", "warm", "heat", "degree")):
        celsius = action.get("celsius") or action.get("value") or action.get("temperature") or 21
        return {"type": "cabin_temp", "celsius": float(celsius)}

    if any(w in raw for w in ("window", "sunroof")):
        return {"type": "windows", "open": bool(action.get("open", False))}

    if any(w in raw for w in ("ac ", "air condition", "air-condition", "hvac")):
        return {"type": "ac", "on": bool(action.get("on", True))}

    if any(w in raw for w in ("alert", "interrupt", "notification", "quiet", "distract", "mute")):
        return {"type": "reduce_alerts", "minutes": float(action.get("minutes", 15))}

    if any(w in raw for w in ("navigate", "route", "direction", "go to", "head to")):
        return {"type": "navigate", "destination": str(action.get("destination", ""))}

    if any(w in raw for w in ("food", "restaurant", "eat", "hungry", "meal", "coffee")):
        return {"type": "find_poi", "category": "food"}

    if any(w in raw for w in ("gas", "fuel", "station")):
        return {"type": "find_poi", "category": "fuel"}

    log.debug("Unknown action format — skipping: %s", action)
    return {}  # unknown — skip


async def _execute_plan(
    websocket: WebSocket,
    agent: AgentLoop,
    pending_meal_pois: list,
    on_suggestion,
    convo: ConversationBuffer,
    plan: dict,
    original_transcript: str,
) -> None:
    """Execute a resolved action plan (from LLM orchestrator or emotional fast-path)."""
    reply   = plan.get("reply", "")
    actions = [_normalize_action(a) for a in plan.get("actions", []) if isinstance(a, dict)]

    log.info("Execute plan: reply=%r actions=%s",
             reply[:50] if reply else "", [a.get("type") for a in actions])

    # Estimate how long TTS will take so music waits for speech to finish.
    # 185 wpm → ~3 words/sec; add 400 ms buffer so music doesn't interrupt last word.
    tts_delay_ms = 0
    if reply:
        convo.add_assistant(reply, actions=actions)
        asyncio.create_task(tts.speak(reply))
        words = len(reply.split())
        tts_delay_ms = max(1500, int(words / 3.0 * 1000) + 400)

    for action in actions:
        atype = action.get("type")
        if not atype:
            continue
        log.info("Action: %s %s", atype, {k: v for k, v in action.items() if k != "type"})

        if atype == "cabin_temp":
            sim.state.cabin_temp_c  = float(action.get("celsius", sim.state.cabin_temp_c))
            sim.state.target_temp_c = sim.state.cabin_temp_c
            sim.state.ac_on = True

        elif atype == "music":
            mood   = action.get("mood", "calm")
            energy = int(action.get("energy", 5))
            tracks = music_mod.quick_match(mood, energy)
            log.info("Music: sending %d tracks for mood=%r energy=%d", len(tracks), mood, energy)
            try:
                await websocket.send_text(json.dumps({
                    "type": "music_results",
                    "data": {"query": mood, "tracks": tracks,
                             "auto_play": True, "delay_ms": tts_delay_ms},
                }))
            except Exception:
                log.exception("Music send failed")

        elif atype == "find_poi":
            category = action.get("category", "food")
            asyncio.create_task(
                _handle_hungry(websocket, agent, pending_meal_pois, sim.state)
                if category == "food"
                else _find_and_suggest(websocket, on_suggestion, category, sim.state)
            )

        elif atype == "navigate":
            destination = action.get("destination", "")
            log.info("Orchestrator: navigate to %r", destination)
            asyncio.create_task(tts.speak(f"Setting navigation to {destination}."))

        elif atype == "reduce_alerts":
            agent.suppress(float(action.get("minutes", 15)))

        elif atype == "windows":
            sim.state.windows_open = bool(action.get("open", False))
            sim.state.sunroof_open = bool(action.get("open", False))

        elif atype == "ac":
            sim.state.ac_on = bool(action.get("on", True))

    # After any voice-triggered plan, suppress the proactive agent for 5 min
    # so a concurrent tick doesn't immediately override the voice response.
    has_reduce_alerts = any(a.get("type") == "reduce_alerts" for a in actions)
    if not has_reduce_alerts:
        agent.suppress(5)

    await sim.broadcast()


async def _find_and_suggest(websocket: WebSocket, on_suggestion, category: str, state) -> None:
    """Find a non-food POI and push it as a suggestion (rest stop, gas, etc.)."""
    poi_map = {"rest": "rest", "gas": "fuel", "coffee": "food", "scenic": "rest"}
    trigger_map = {
        "rest":   "driver requested a rest stop — suggest one nearby (type=rest)",
        "fuel":   "driver asked about fuel — suggest the nearest station (type=range)",
        "food":   "driver wants coffee or a quick stop — suggest a café (type=meal)",
    }
    poi_type = poi_map.get(category, "rest")
    trigger  = trigger_map.get(poi_type, f"driver requested {category} stop (type=rest)")
    from agent.loop import AgentLoop  # noqa: PLC0415
    # Force-generate a suggestion for this trigger
    from agent.generator import generate_suggestion  # noqa: PLC0415
    from agent.loop import AgentLoop as _AL  # noqa: PLC0415
    suggestion = await generate_suggestion([state], trigger=trigger)
    if suggestion:
        await on_suggestion(suggestion)


async def _handle_compound_intents(
    websocket: WebSocket,
    agent: AgentLoop,
    pending_meal_pois: list,
    sub_intents,
    raw: str,
) -> None:
    """
    Intent Decomposition Engine — execute multiple intents from one utterance in priority order.
    Example: "I'm hungry and need gas" fires fuel check first, then meal search.
    """
    acknowledged = [i.type for i in sub_intents]
    ack_text = " and ".join(acknowledged[:2])
    asyncio.create_task(tts.speak(f"Got it — I'll handle {ack_text} for you."))

    for intent in sub_intents:
        if intent.type == "fuel":
            agent.force_suggest("fuel critically low — suggest stopping for fuel (type=range)")
        elif intent.type in ("meal", "hungry"):
            asyncio.create_task(_handle_hungry(websocket, agent, pending_meal_pois, sim.state))
        elif intent.type == "rest":
            agent.force_suggest("driver requested a rest stop (type=rest)")
        elif intent.type == "music":
            await _handle_music(websocket, raw)
        elif intent.type == "query":
            asyncio.create_task(_handle_query(websocket, raw, sim.state))
        await asyncio.sleep(0.3)   # slight stagger so TTS doesn't overlap


async def _handle_query(websocket: WebSocket, question: str, state) -> None:
    """
    Answer a driver's spoken question.
    Tries local LFM first; escalates to Claude Haiku if the answer is too
    short or hedging — mirrors MB × Liquid AI 'complements cloud LLMs'.
    """
    from dataclasses import asdict
    compact = {k: v for k, v in asdict(state).items()
               if v is not None and v != 0 and k not in ("is_on_highway",)}
    state_json = json.dumps(compact, indent=2)

    # ── Local attempt ────────────────────────────────────────────────────────
    messages = [
        {"role": "system", "content": VEHICLE_QA_SYSTEM},
        {"role": "user", "content": VEHICLE_QA_USER.format(
            state_json=state_json, question=question,
        )},
    ]
    local_answer = await llm_mod.complete(messages, max_tokens=120, temperature=0.3)
    local_answer = (local_answer or "").strip()

    if not cloud_mod.needs_cloud(local_answer):
        log.info("Q&A [local]: %r → %r", question[:40], local_answer[:80])
        asyncio.create_task(tts.speak(local_answer))
        source = "⚡ local"
        answer = local_answer
    else:
        # ── Cloud escalation ─────────────────────────────────────────────────
        log.info("Q&A [escalating to cloud]: local=%r", local_answer[:40])
        token_gen = await cloud_mod.stream_answer(question, state_json)
        if token_gen:
            answer = await tts.speak_stream(token_gen)
            source = "☁ cloud"
            log.info("Q&A [cloud]: %r → %r", question[:40], answer[:80])
        else:
            # Cloud unavailable — use local answer however weak
            asyncio.create_task(tts.speak(local_answer or "I'm not sure about that."))
            answer = local_answer or "I'm not sure about that."
            source = "⚡ local"

    try:
        await websocket.send_text(json.dumps({
            "type": "transcript",
            "data": {"text": f"{source}  {answer.strip()}"},
        }))
    except Exception:
        pass


async def _handle_music(websocket: WebSocket, query: str):
    tracks = await music_mod.query(query)
    if tracks is None:
        return
    try:
        await websocket.send_text(json.dumps({
            "type": "music_results",
            "data": {"query": query, "tracks": tracks},
        }))
    except Exception:
        pass

