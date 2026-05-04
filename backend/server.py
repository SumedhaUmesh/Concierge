import asyncio
import json
import logging
import os
from dataclasses import asdict
from pathlib import Path
from typing import Optional

# Load .env from project root before any agent modules read env vars
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from simulator import Simulator
from signals import Suggestion
from agent.loop import AgentLoop
from obd_source import obd_source
from tool_router import enrich
from agent import music as music_mod
from agent.voice import tts, asr, intent_classifier
from agent import llm as llm_mod, cloud as cloud_mod
from agent.prompts import VEHICLE_QA_SYSTEM, VEHICLE_QA_USER
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
    # Freeze the clock if the caller is explicitly setting current_time,
    # otherwise broadcast() would immediately overwrite it with real time.
    if "current_time" in body:
        sim._scenario_time_frozen = True
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
            "Nominatim POI search (bounding box, no account required)",
            "Claude via OpenRouter (only if local LLM answer is too short, requires OPENROUTER_API_KEY)",
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

    async def _tts_start_cb(_text: str):
        await _broadcast_ws_all(json.dumps({"type": "tts_start"}))

    async def _tts_end_cb():
        await _broadcast_ws_all(json.dumps({"type": "tts_end"}))

    tts.register_speak_events(_tts_start_cb, _tts_end_cb)

    asyncio.create_task(_broadcast_loop())
    asyncio.create_task(_calendar_sync_loop())
    asyncio.create_task(_weather_loop())
    asyncio.create_task(_music_evolution_loop())


async def _broadcast_loop():
    """Emit state to all clients every 1.5 s — keeps gauges, bars, and cabin live."""
    while True:
        await sim.broadcast()
        await asyncio.sleep(1.5)


async def _calendar_sync_loop():
    """Sync macOS Calendar every 5 minutes in the background."""
    while True:
        try:
            await _do_calendar_sync()
        except Exception:
            log.exception("Calendar sync loop error — will retry in 5 min")
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

        import time as _time_mod  # noqa: PLC0415
        if _time_mod.monotonic() < _music_suppressed_until:
            continue  # schedule alert recently fired — don't push music

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


# Timestamp after which the music evolution loop may fire again.
# Set to future when a schedule/lateness alert fires so music doesn't
# auto-play immediately after a "you'll be late" warning.
_music_suppressed_until: float = 0.0


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

    async def _on_suggestion(suggestion: Suggestion, speak: bool = True):
        # Proactive meal: ask for preference first rather than pinning a specific restaurant.
        # This matches the voice-triggered hungry flow and feels more conversational.
        if suggestion.type == "meal" and not suggestion.enriched_action:
            asyncio.create_task(_handle_hungry(websocket, agent, pending_meal_pois, sim.state))
            return

        # Rest stop: fill from state coords and skip enrich entirely — Nominatim
        # rarely has "rest_area" tags in urban areas, so bypass it completely.
        # Match by type OR suggested_action because the small LLM may output
        # type="cabin" with suggested_action="find_poi:rest" when grammar lacked "rest".
        _is_rest = (suggestion.type == "rest"
                    or suggestion.suggested_action == "find_poi:rest")
        if (_is_rest
                and sim.state.next_rest_stop_lat
                and sim.state.next_rest_stop_lng):
            km = round(sim.state.next_rest_stop_km or 0)
            suggestion.enriched_action = {
                "type": "navigate",
                "label": f"Navigate to rest stop ({km} km)",
                "place_name": f"Rest Stop • {km} km ahead",
                "lat": sim.state.next_rest_stop_lat,
                "lng": sim.state.next_rest_stop_lng,
            }
            suggestion.headline = f"Rest stop {km} km ahead"
            suggestion.detail   = "You've been driving over 2 hours. Pull over, stretch, and recharge."
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

        # Speak the suggestion — schedule gets context-aware phrasing from state
        if speak:
            if suggestion.type == "schedule" and sim.state.next_meeting_title and sim.state.next_meeting_time:
                global _music_suppressed_until
                import time as _t  # noqa: PLC0415
                _music_suppressed_until = _t.monotonic() + 600  # suppress music for 10 min
                mtg = sim.state.next_meeting_title
                try:
                    mins_until = _hhmm_to_min(sim.state.next_meeting_time) - _hhmm_to_min(sim.state.current_time)
                    travel = (sim.state.normal_travel_minutes or 0) + (sim.state.traffic_delay_minutes or 0)
                    if mins_until < travel:
                        tts_msg = (f"You might be late for {mtg} — you need {travel} minutes "
                                   f"but only have {mins_until}. I'd leave now.")
                    else:
                        tts_msg = (f"Time to head out for {mtg}. "
                                   f"It's {travel} minutes away and you have about {mins_until} minutes.")
                except Exception:
                    tts_msg = suggestion.headline
                asyncio.create_task(tts.speak(tts_msg))
            else:
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
    log.info("Voice input received: %d b64-chars", len(audio_b64))
    transcript = await asr.transcribe(audio_b64)
    if not transcript:
        log.info("Voice: no transcript returned (empty speech or ASR error)")
        # Reset frontend — otherwise "Transcribing…" spinner hangs indefinitely
        try:
            await websocket.send_text(json.dumps({"type": "transcript", "data": {"text": ""}}))
        except Exception:
            pass
        return

    convo.add_user(transcript)

    try:
        await websocket.send_text(json.dumps({
            "type": "transcript",
            "data": {"text": transcript},
        }))
    except Exception:
        pass

    # Mid meal-preference flow: driver is answering the cuisine question — skip classification
    if pending_meal_pois:
        await _handle_meal_preference(websocket, pending_meal_pois, transcript, on_suggestion)
        return

    # LLM-first: single call classifies intent and extracts parameters
    intent_data = await intent_classifier.classify(transcript)
    intent = intent_data.get("intent", "other")

    # Normalize: small LLM sometimes returns the field name ("cabin_action") as intent
    if intent == "cabin_action":
        intent = "cabin"

    # Noise guard: genuine accept / dismiss / defer are short phrases (≤5 words).
    # A longer transcript classified as one of these is almost certainly background
    # audio that Whisper mis-transcribed as conversational speech.
    if intent in ("accept", "dismiss", "defer") and len(transcript.split()) > 5:
        log.info("Noise guard: dropped %d-word '%s' transcript classified as %s",
                 len(transcript.split()), intent, intent)
        return

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

    elif intent == "meal":
        asyncio.create_task(_handle_hungry(
            websocket, agent, pending_meal_pois, sim.state,
            on_suggestion=on_suggestion,
            initial_transcript=transcript,
        ))

    elif intent == "cabin":
        cabin_action = intent_data.get("cabin_action")
        celsius      = intent_data.get("celsius")
        # If no action extracted (keyword fallback or LLM confusion), infer from transcript
        if not cabin_action and not celsius:
            _t = transcript.lower()
            if "window" in _t:
                cabin_action = "windows_open" if any(w in _t for w in ("open", "down", "roll", "lower")) else "windows_close"
            elif "sunroof" in _t:
                cabin_action = "sunroof_open" if "open" in _t else "sunroof_close"
            elif any(w in _t for w in ("ac on", "turn on ac", "turn on the ac", "start ac", "start the ac")):
                cabin_action = "ac_on"
            elif any(w in _t for w in ("hot", "stuffy", "heat", "sweating", "burning")):
                cabin_action = "cool"   # driver is too hot → cool down
            elif any(w in _t for w in ("cold", "chill", "freez", "shiver")):
                cabin_action = "warm"   # driver is too cold → warm up
            elif any(w in _t for w in ("warm", "warmer", "heat up")):
                cabin_action = "warm"   # driver explicitly wants warmth
            elif any(w in _t for w in (" ac ", "cool", "cooler", "air")):
                cabin_action = "cool"   # generic AC / cooling request
            log.info("Cabin: inferred action=%r from transcript", cabin_action)
        plan = _cabin_intent_to_plan(cabin_action, celsius)
        if plan:
            asyncio.create_task(_execute_plan(
                websocket, agent, pending_meal_pois, on_suggestion, convo, plan, transcript
            ))
        else:
            asyncio.create_task(
                _handle_orchestrated(websocket, agent, pending_meal_pois, on_suggestion, convo, transcript)
            )

    elif intent == "compound":
        t_lower = transcript.lower()
        # Coffee + meeting time check → dedicated handler (no LLM, deterministic)
        _coffee_words  = ("coffee", "cafe", "café", "espresso", "latte", "cappuccino")
        _meeting_words = ("meeting", "time", "late", "make it", "before", "appointment")
        if any(w in t_lower for w in _coffee_words) and any(w in t_lower for w in _meeting_words):
            asyncio.create_task(_handle_coffee_schedule(websocket, on_suggestion, sim.state))
        else:
            # General compound → Claude if available, else local LLM orchestrator
            asyncio.create_task(
                _handle_orchestrated(websocket, agent, pending_meal_pois, on_suggestion, convo,
                                     transcript, use_cloud=True)
            )

    elif intent == "music":
        asyncio.create_task(_handle_music(websocket, transcript))

    elif intent == "query":
        # "Find me a coffee shop on my route and tell me..." — ASR often truncates the
        # tail ("if I have time before my meeting"), causing the classifier to miss
        # the compound nature. Catch it here by keyword before falling to Q&A.
        t_lower = transcript.lower()
        _coffee_words  = ("coffee", "cafe", "café", "espresso", "latte", "cappuccino")
        _route_words   = ("route", "on my way", "on the way", "nearby", "find me")
        if any(w in t_lower for w in _coffee_words) and any(w in t_lower for w in _route_words):
            asyncio.create_task(_handle_coffee_schedule(websocket, on_suggestion, sim.state))
        else:
            asyncio.create_task(_handle_query(websocket, transcript, sim.state))

    elif intent == "navigate":
        fast = intent_data.get("fast", False)
        if fast:
            fast_plan = {
                "reply":   "Got it — keeping you on the fastest route.",
                "actions": [{"type": "reduce_alerts", "minutes": 10}],
            }
            asyncio.create_task(_execute_plan(
                websocket, agent, pending_meal_pois, on_suggestion, convo, fast_plan, transcript
            ))
        else:
            asyncio.create_task(
                _handle_orchestrated(websocket, agent, pending_meal_pois, on_suggestion, convo, transcript)
            )

    else:
        # other, emotional, unrecognized — orchestrator handles these
        asyncio.create_task(
            _handle_orchestrated(websocket, agent, pending_meal_pois, on_suggestion, convo, transcript)
        )


async def _handle_hungry(
    websocket: WebSocket,
    agent: AgentLoop,
    pending_meal_pois: list,
    state,
    *,
    on_suggestion=None,
    initial_transcript: str = "",
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

    # If the driver's original utterance already names a food type (e.g. "I want coffee"),
    # skip the preference question and go straight to matching.
    if initial_transcript and on_suggestion and _has_food_preference(initial_transcript):
        log.info("_handle_hungry: preference already in utterance, skipping question")
        await _handle_meal_preference(websocket, pending_meal_pois, initial_transcript, on_suggestion)
        return

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
    "sandwich":   ["sandwich", "deli", "sub", "subway", "hoagie", "jersey", "quiznos"],
    "burger":     ["burger", "hamburger", "fast_food", "mcdonald", "wendy", "in-n-out", "shake shack"],
    "pizza":      ["pizza", "italian"],
    "pasta":      ["pasta", "spaghetti", "carbonara", "lasagna", "fettuccine"],
    "sushi":      ["sushi", "izakaya"],
    "ramen":      ["ramen", "noodle", "pho", "udon", "tonkotsu"],
    "japanese":   ["japanese", "teriyaki", "tempura"],
    "salad":      ["salad", "vegan", "vegetarian", "healthy"],
    "bowl":       ["bowl", "grain bowl", "rice bowl", "acai"],
    "coffee":     ["coffee", "cafe", "espresso", "starbucks", "peet", "latte", "cappuccino", "americano"],
    "smoothie":   ["smoothie", "juice bar", "acai bowl"],
    "taco":       ["mexican", "taco", "burrito", "chipotle", "quesadilla", "enchilada"],
    "chinese":    ["chinese", "dim sum", "dim_sum", "dumpling", "wonton", "hot pot"],
    "indian":     ["indian", "curry", "biryani", "naan", "tikka", "masala"],
    "thai":       ["thai", "pad thai", "satay"],
    "korean":     ["korean", "kbbq", "bibimbap", "bulgogi", "korean bbq"],
    "greek":      ["greek", "mediterranean"],
    "shawarma":   ["shawarma", "kebab", "gyro", "falafel", "hummus", "middle eastern", "halal"],
    "bbq":        ["bbq", "barbecue", "ribs", "smoked", "brisket", "pulled pork"],
    "wings":      ["wings", "buffalo", "chicken wings"],
    "steak":      ["steak", "steakhouse", "chophouse"],
    "seafood":    ["seafood", "fish", "lobster", "crab", "shrimp", "oyster", "clam"],
    "poke":       ["poke", "hawaiian", "ahi"],
    "vietnamese": ["vietnamese", "banh mi", "spring roll", "bun bo"],
    "breakfast":  ["breakfast", "brunch", "pancake", "waffle", "diner", "omelette", "eggs"],
}


def _has_food_preference(text: str) -> bool:
    """True when the utterance already names a specific food type."""
    lower = text.lower()
    return any(
        s in lower
        for synonyms in _FOOD_KEYWORD_SYNONYMS.values()
        for s in synonyms
    )


async def _handle_meal_preference(
    websocket: WebSocket,
    pending_meal_pois: list,
    transcript: str,
    on_suggestion,
):
    lower = transcript.lower()
    matched = None
    keyword_tried: Optional[str] = None

    # Match restaurant name words first (handles "let's go to Erewhon").
    # Skip words shorter than 4 chars to avoid "el", "in", "the" matching as substrings.
    for poi in pending_meal_pois:
        words = [w.strip("'s.,") for w in poi.name.lower().split()]
        if any(w and len(w) >= 4 and w in lower for w in words):
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
    await on_suggestion(suggestion, speak=False)  # tts_msg already spoken above


# ── Compound handler: coffee stop + meeting time check ───────────────────────

def _hhmm_to_min(t: str) -> int:
    """Convert "HH:MM" to total minutes since midnight."""
    try:
        h, m = str(t).split(":", 1)
        return int(h) * 60 + int(m)
    except Exception:
        return 0


async def _handle_coffee_schedule(
    websocket: WebSocket,
    on_suggestion,
    state,
) -> None:
    """
    Compound handler for 'find coffee on my route + do I have time before meeting'.

    Flow:
      1. Find coffee/cafe POIs filtered to the driver's route
      2. Get travel time: car → coffee shop (OSRM)
      3. Get travel time: coffee shop → destination (OSRM, if destination known)
      4. Calculate detour cost vs time until meeting
      5. Give a single spoken verdict + suggestion card
    """
    from agent.tools.poi import find_poi          # noqa: PLC0415
    from agent.tools.route import get_route, get_travel_time  # noqa: PLC0415

    # ── Step 1: Find coffee on route ────────────────────────────────────────
    route = None
    if state.destination_lat and state.destination_lng:
        route = await get_route(state.lat, state.lng,
                                state.destination_lat, state.destination_lng)

    pois = await find_poi("food", state.lat, state.lng, radius_km=15,
                          route=route, limit=10)

    # Prefer places with coffee/cafe in name or cuisine
    _COFFEE_WORDS = {"coffee", "cafe", "café", "espresso", "starbucks",
                     "peet", "blue bottle", "lavazza", "tim horton"}
    coffee_pois = [
        p for p in pois
        if any(w in (p.name + " " + (p.cuisine or "")).lower() for w in _COFFEE_WORDS)
    ]
    candidates = coffee_pois if coffee_pois else pois  # fall back to any food nearby
    if not candidates:
        asyncio.create_task(tts.speak(
            "I couldn't find any coffee shops on your route right now."
        ))
        return

    best = candidates[0]

    # ── Step 2: Travel times via OSRM ───────────────────────────────────────
    travel_to_coffee = await get_travel_time(
        state.lat, state.lng, best.lat, best.lng
    )

    detour_min: Optional[float] = None
    if travel_to_coffee is not None and state.destination_lat and state.destination_lng:
        travel_coffee_to_dest = await get_travel_time(
            best.lat, best.lng,
            state.destination_lat, state.destination_lng,
        )
        current_travel = await get_travel_time(
            state.lat, state.lng,
            state.destination_lat, state.destination_lng,
        )
        if travel_coffee_to_dest is not None and current_travel is not None:
            detour_min = max(0.0, (travel_to_coffee + travel_coffee_to_dest) - current_travel)
    elif travel_to_coffee is not None:
        # No destination set — estimate detour as round-trip to coffee
        detour_min = travel_to_coffee * 1.5

    # ── Step 3: Meeting time check ───────────────────────────────────────────
    has_time: Optional[bool] = None
    margin_min: float = 0.0
    minutes_until_meeting: Optional[float] = None

    if state.next_meeting_time and state.current_time:
        minutes_until_meeting = (
            _hhmm_to_min(state.next_meeting_time) - _hhmm_to_min(state.current_time)
        )
        travel_needed = (state.normal_travel_minutes or 0) + (state.traffic_delay_minutes or 0)
        stop_time = 10  # assume 10 min at the coffee shop
        detour_cost = detour_min if detour_min is not None else (travel_to_coffee or 10) * 1.5
        time_needed = travel_needed + detour_cost + stop_time
        margin_min = minutes_until_meeting - time_needed
        has_time = margin_min >= 0

    # ── Step 4: Compose spoken verdict ──────────────────────────────────────
    dist_str = (f"{round(detour_min)} min detour" if detour_min is not None
                else f"{best.distance_km} km away")

    if has_time is True:
        verdict = (
            f"You have about {round(margin_min)} minutes to spare — enough for a quick stop."
        )
        reply = f"Found {best.name}, {dist_str}. {verdict}"
    elif has_time is False:
        verdict = (
            f"It'd be tight — your meeting is in {round(minutes_until_meeting)} minutes."
        )
        reply = f"There's {best.name} {dist_str}, but {verdict} I'd skip it today."
    else:
        # No meeting in state
        reply = f"{best.name} is {dist_str}. Want me to navigate there?"
        verdict = f"{best.cuisine.title() or 'Coffee'} · {best.distance_km} km"

    asyncio.create_task(tts.speak(reply))
    log.info("Coffee+schedule: %s, detour=%.1f min, has_time=%s",
             best.name, detour_min or 0, has_time)

    # ── Step 5: Suggestion card ──────────────────────────────────────────────
    headline = f"☕ {best.name}"
    if has_time is True:
        headline += f" — {round(margin_min)} min to spare"
    elif has_time is False:
        headline += " — tight on time"

    suggestion = Suggestion(
        type="meal",
        urgency=3,
        headline=headline,
        detail=verdict,
        suggested_action="find_poi:food",
        enriched_action={
            "type": "navigate",
            "label": f"Navigate to {best.name}",
            "place_name": best.name,
            "distance_km": best.distance_km,
            "lat": best.lat,
            "lng": best.lng,
            "address": best.address,
        },
    )
    await on_suggestion(suggestion, speak=False)  # reply already spoken above



# ── Cabin intent → plan (no LLM needed) ─────────────────────────────────────

_CABIN_PLAN_MAP: dict[str, tuple[str, list]] = {
    "cool":          ("I'll cool the cabin down for you.",  [{"type": "cabin_temp", "celsius": 18}, {"type": "ac", "on": True}]),
    "warm":          ("I'll warm things up.",               [{"type": "cabin_temp", "celsius": 24}]),
    "ac_on":         ("AC is on.",                          [{"type": "ac", "on": True}]),
    "windows_open":  ("Opening the windows.",               [{"type": "windows", "open": True}]),
    "windows_close": ("Closing the windows.",               [{"type": "windows", "open": False}]),
    "sunroof_open":  ("Opening the sunroof.",               [{"type": "sunroof", "open": True}]),
    "sunroof_close": ("Closing the sunroof.",               [{"type": "sunroof", "open": False}]),
}


def _cabin_intent_to_plan(cabin_action: Optional[str], celsius: Optional[float]) -> Optional[dict]:
    if celsius is not None:
        return {
            "reply":   f"Setting the cabin to {celsius}°C.",
            "actions": [{"type": "cabin_temp", "celsius": celsius}],
        }
    if not cabin_action:
        return None
    entry = _CABIN_PLAN_MAP.get(cabin_action)
    if not entry:
        return None
    reply, actions = entry
    return {"reply": reply, "actions": list(actions)}


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
    use_cloud: bool = False,
) -> None:
    """
    Orchestrate cabin, compound, emotional, and other open-ended utterances.
    Reaches here only after LLM intent classification in _handle_voice().
    use_cloud=True tries Claude Haiku first (compound queries); falls back to local LLM.
    """
    import json as _json
    from dataclasses import asdict as _asdict

    # Emotional fast-path: instant response for clear emotional states, no LLM needed
    fast_plan = _emotional_fast_path(transcript)
    if fast_plan:
        log.info("Orchestrator: emotional fast-path for %r", transcript[:40])
        await _execute_plan(websocket, agent, pending_meal_pois, on_suggestion, convo,
                            fast_plan, transcript)
        return

    resolved   = convo.resolve(transcript)
    state_json = _json.dumps(_asdict(sim.state), default=str)[:600]
    conv_ctx   = convo.context_str()
    pref_ctx   = trip_memory.get_context_string() or ""

    # For compound queries, try Claude Haiku first — much better at multi-step reasoning
    plan = None
    if use_cloud:
        plan = await cloud_mod.orchestrate_compound(resolved, state_json, conv_ctx)
        if plan:
            log.info("Orchestrator: using Claude for compound query")

    if plan is None:
        plan = await orchestrate(resolved, state_json, conv_ctx, pref_ctx)

    if plan is None:
        question = fallback_clarify(transcript)
        convo.add_assistant(question)
        asyncio.create_task(tts.speak(question))
        return

    confidence = plan.get("confidence", "high")
    clarify    = plan.get("clarify")

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

    if "sunroof" in raw:
        return {"type": "sunroof", "open": bool(action.get("open", False))}

    if "window" in raw:
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

    # When the primary intent is a food search, skip ambient actions the orchestrator
    # may have added (music, cabin, windows) — the driver just wants food, not a mood reset.
    _food_only = any(
        a.get("type") == "find_poi" and a.get("category") == "food" for a in actions
    )

    for action in actions:
        atype = action.get("type")
        if not atype:
            continue
        if _food_only and atype in ("music", "cabin_temp", "ac", "windows", "navigate"):
            log.info("Action: skipping %s (food-only request)", atype)
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
                # Pass original transcript so _handle_hungry skips the preference question
                # when the driver already named a specific food ("find me a burger").
                _handle_hungry(websocket, agent, pending_meal_pois, sim.state,
                               on_suggestion=on_suggestion,
                               initial_transcript=original_transcript)
                if category == "food"
                else _find_and_suggest(websocket, on_suggestion, category, sim.state)
            )

        elif atype == "navigate":
            destination = action.get("destination", "")
            log.info("Orchestrator: navigate to %r", destination)

        elif atype == "reduce_alerts":
            agent.suppress(float(action.get("minutes", 15)))

        elif atype == "windows":
            sim.state.windows_open = bool(action.get("open", False))

        elif atype == "sunroof":
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
    poi_map = {"rest": "rest", "gas": "fuel", "coffee": "food"}
    trigger_map = {
        "rest":   "driver requested a rest stop — suggest one nearby (type=rest)",
        "fuel":   "driver asked about fuel — suggest the nearest station (type=range)",
        "food":   "driver wants coffee or a quick stop — suggest a café (type=meal)",
    }
    poi_type = poi_map.get(category, "rest")
    trigger  = trigger_map.get(poi_type, f"driver requested {category} stop (type=rest)")
    from agent.generator import generate_suggestion  # noqa: PLC0415
    suggestion = await generate_suggestion([state], trigger=trigger)
    if suggestion:
        await on_suggestion(suggestion)


async def _handle_query(websocket: WebSocket, question: str, state) -> None:
    """
    Answer a driver's spoken question.
    Tries local LFM first; escalates to Claude Haiku if the answer is too
    short or hedging'.
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
            # auto_play starts the top-ranked track after a short delay
            # (300 ms lets any TTS finish before music begins)
            "data": {"query": query, "tracks": tracks, "auto_play": True, "delay_ms": 300},
        }))
    except Exception:
        pass

