import asyncio
import json
import logging
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from simulator import Simulator
from signals import Suggestion
from agent.loop import AgentLoop
from obd_source import obd_source
from tool_router import enrich
from agent import music as music_mod
from agent.voice import tts, asr, classifier
from agent import llm as llm_mod
from agent.prompts import VEHICLE_QA_SYSTEM, VEHICLE_QA_USER
import trip_memory

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = FastAPI(title="Concierge")
sim = Simulator()

DASHBOARD = Path(__file__).parent.parent / "dashboard"

app.mount("/static", StaticFiles(directory=str(DASHBOARD / "static")), name="static")


@app.get("/")
async def root():
    return FileResponse(str(DASHBOARD / "index.html"))


@app.get("/demo", response_class=HTMLResponse)
async def demo_panel():
    return HTMLResponse(_DEMO_HTML)


@app.post("/demo/state")
async def demo_set_state(body: dict):
    for key, value in body.items():
        if hasattr(sim.state, key):
            setattr(sim.state, key, value)
    await sim.broadcast()
    return {"ok": True, "state": asdict(sim.state)}


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


@app.get("/memory/stats")
async def memory_stats():
    return trip_memory.get_preferences()


@app.delete("/memory/reset")
async def memory_reset():
    import sqlite3 as _sq
    with trip_memory._lock:
        conn = trip_memory._get_conn()
        conn.execute("DELETE FROM suggestions")
        conn.commit()
    return {"ok": True}


@app.on_event("startup")
async def _warmup():
    # Load model eagerly at startup so the first gate call doesn't pay
    # the full load latency. The lock in llm.py ensures this is safe.
    from agent.llm import get_llm  # noqa: PLC0415
    await asyncio.to_thread(get_llm)


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

    async def _on_suggestion(suggestion: Suggestion):
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

            if kind == "play":
                await sim.play(msg["scenario"])
            elif kind == "reset":
                await sim.reset()
            elif kind == "user_dismiss":
                agent.dismiss()
                if latest_suggestion:
                    trip_memory.log_outcome(latest_suggestion[0], "dismissed", sim.state)
            elif kind == "user_accept":
                agent.accept()
                if latest_suggestion:
                    trip_memory.log_outcome(latest_suggestion[0], "accepted", sim.state)
            elif kind == "mute":
                tts.set_muted(msg.get("muted", False))
            elif kind == "music_query":
                asyncio.create_task(_handle_music(websocket, msg.get("query", "")))
            elif kind == "voice_input":
                asyncio.create_task(
                    _handle_voice(websocket, agent, latest_suggestion, pending_meal_pois, _on_suggestion, msg.get("audio", ""))
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
    audio_b64: str,
):
    transcript = await asr.transcribe(audio_b64)
    if not transcript:
        return

    try:
        await websocket.send_text(json.dumps({
            "type": "transcript",
            "data": {"text": transcript},
        }))
    except Exception:
        pass

    # If we're mid meal-preference flow, handle that before normal routing
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

    elif intent == "hungry":
        asyncio.create_task(_handle_hungry(websocket, agent, pending_meal_pois, sim.state))

    elif intent == "music":
        await _handle_music(websocket, transcript)

    elif intent == "defer":
        agent.dismiss()
        asyncio.create_task(tts.speak("Got it. I'll check back in a few minutes."))

    elif intent == "query":
        asyncio.create_task(_handle_query(websocket, transcript, sim.state))


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


async def _handle_meal_preference(
    websocket: WebSocket,
    pending_meal_pois: list,
    transcript: str,
    on_suggestion,
):
    lower = transcript.lower()
    matched = None

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

    if not matched:
        matched = pending_meal_pois[0]  # default to nearest

    pending_meal_pois.clear()

    asyncio.create_task(tts.speak(f"Great, heading to {matched.name}."))

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


async def _handle_query(websocket: WebSocket, question: str, state) -> None:
    """Answer a driver's spoken question using streaming LLM → sentence-by-sentence TTS."""
    from dataclasses import asdict
    compact = {k: v for k, v in asdict(state).items()
               if v is not None and v != 0 and k not in ("is_on_highway",)}
    state_json = json.dumps(compact, indent=2)

    messages = [
        {"role": "system", "content": VEHICLE_QA_SYSTEM},
        {"role": "user", "content": VEHICLE_QA_USER.format(
            state_json=state_json, question=question,
        )},
    ]

    token_gen = llm_mod.stream_complete(messages, max_tokens=120, temperature=0.3)
    answer = await tts.speak_stream(token_gen)

    log.info("Q&A: %r → %r", question[:40], answer[:80])

    try:
        await websocket.send_text(json.dumps({
            "type": "transcript",
            "data": {"text": f"A: {answer.strip()}"},
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


# ── Demo control panel HTML ───────────────────────────────────────────────────

_DEMO_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Concierge — Demo Controls</title>
<style>
  body { font-family: monospace; background: #0d0d0d; color: #ccc; padding: 32px; max-width: 640px; }
  h1 { color: #4ade80; font-size: 16px; letter-spacing: 2px; margin-bottom: 24px; }
  h2 { color: #888; font-size: 11px; letter-spacing: 1px; margin: 20px 0 8px; text-transform: uppercase; }
  label { display: flex; justify-content: space-between; align-items: center; margin: 6px 0; font-size: 12px; }
  label span { color: #888; min-width: 160px; }
  input[type=range] { flex: 1; margin: 0 12px; accent-color: #4ade80; }
  input[type=text], input[type=number] { background: #1a1a1a; border: 1px solid #333; color: #ccc; padding: 4px 8px; border-radius: 4px; width: 160px; font-family: monospace; font-size: 12px; }
  .val { min-width: 40px; text-align: right; color: #4ade80; }
  .toggle { display: flex; gap: 8px; }
  .toggle button { background: #1a1a1a; border: 1px solid #333; color: #888; padding: 4px 12px; border-radius: 4px; cursor: pointer; font-family: monospace; font-size: 11px; }
  .toggle button.on { background: #0d1a0d; border-color: #4ade80; color: #4ade80; }
  .preset-row { display: flex; gap: 8px; flex-wrap: wrap; margin: 8px 0; }
  .preset { background: #1a1a1a; border: 1px solid #333; color: #888; padding: 5px 12px; border-radius: 4px; cursor: pointer; font-size: 11px; font-family: monospace; }
  .preset:hover { border-color: #4ade80; color: #4ade80; }
  button#apply { background: #4ade80; color: #000; border: none; padding: 10px 28px; border-radius: 6px; cursor: pointer; font-family: monospace; font-size: 13px; font-weight: bold; margin-top: 20px; width: 100%; }
  #status { margin-top: 12px; font-size: 11px; color: #888; min-height: 16px; }
</style>
</head>
<body>
<h1>CONCIERGE — DEMO CONTROLS</h1>

<h2>OBD-II Live Data</h2>
<div style="display:flex;gap:8px;align-items:center;margin-bottom:8px">
  <input type="text" id="obd_port" placeholder="/dev/cu.OBDII (blank=auto)" style="flex:1">
  <button class="preset" onclick="obdConnect()">Connect</button>
  <button class="preset" onclick="obdDisconnect()">Disconnect</button>
</div>
<div id="obd_status" style="font-size:11px;color:#888;margin-bottom:12px">Not connected — simulator data in use</div>

<h2>Quick Presets</h2>
<div class="preset-row">
  <button class="preset" onclick="applyPreset('low_fuel')">Low Fuel</button>
  <button class="preset" onclick="applyPreset('rain_open')">Rain + Windows Open</button>
  <button class="preset" onclick="applyPreset('hungry')">Hungry at Lunch</button>
  <button class="preset" onclick="applyPreset('late_meeting')">Late for Meeting</button>
  <button class="preset" onclick="applyPreset('normal')">Normal Drive</button>
</div>

<h2>Vehicle</h2>
<label><span>Fuel %</span><input type="range" id="fuel_percent" min="0" max="100" value="65" oninput="this.nextElementSibling.textContent=this.value"><span class="val">65</span></label>
<label><span>Range km</span><input type="range" id="range_km" min="0" max="600" value="280" oninput="this.nextElementSibling.textContent=this.value"><span class="val">280</span></label>
<label><span>Speed km/h</span><input type="range" id="speed_kmh" min="0" max="200" value="110" oninput="this.nextElementSibling.textContent=this.value"><span class="val">110</span></label>

<h2>Location</h2>
<label><span>Latitude</span><input type="text" id="lat" value="34.0211"></label>
<label><span>Longitude</span><input type="text" id="lng" value="-118.3965"></label>
<label><span>Label</span><input type="text" id="location_label" value="Downtown Culver City, CA"></label>
<div class="preset-row">
  <button class="preset" onclick="setLocation(34.0211,-118.3965,'Downtown Culver City, CA')">Culver City</button>
  <button class="preset" onclick="setLocation(34.0522,-118.2437,'Downtown Los Angeles, CA')">Downtown LA</button>
  <button class="preset" onclick="setLocation(34.1478,-118.1445,'Pasadena, CA')">Pasadena</button>
  <button class="preset" onclick="setLocation(34.0195,-118.4912,'Santa Monica, CA')">Santa Monica</button>
</div>

<h2>Cabin</h2>
<label><span>Windows Open</span><div class="toggle"><button id="windows_open_btn" onclick="toggleBool('windows_open')">Closed</button></div></label>
<label><span>Sunroof Open</span><div class="toggle"><button id="sunroof_open_btn" onclick="toggleBool('sunroof_open')">Closed</button></div></label>
<label><span>AC On</span><div class="toggle"><button id="ac_on_btn" onclick="toggleBool('ac_on')">Off</button></div></label>
<label><span>Rain in minutes</span><input type="number" id="rain_in_minutes" placeholder="null = no rain" min="0" max="60"></label>

<h2>Driver</h2>
<label><span>Hours since meal</span><input type="range" id="hours_since_meal" min="0" max="10" step="0.5" value="1.5" oninput="this.nextElementSibling.textContent=this.value"><span class="val">1.5</span></label>
<label><span>Current time</span><input type="text" id="current_time" value="10:00"></label>

<h2>Destination (enables route-aware POI)</h2>
<label><span>Dest label</span><input type="text" id="destination" placeholder="e.g. Santa Monica Pier"></label>
<label><span>Dest latitude</span><input type="text" id="destination_lat" placeholder="e.g. 34.0083"></label>
<label><span>Dest longitude</span><input type="text" id="destination_lng" placeholder="e.g. -118.4982"></label>
<div class="preset-row">
  <button class="preset" onclick="setDest('Santa Monica Pier',34.0083,-118.4982)">Santa Monica</button>
  <button class="preset" onclick="setDest('LAX Airport',33.9425,-118.4081)">LAX</button>
  <button class="preset" onclick="setDest('Pasadena City Hall',34.1478,-118.1445)">Pasadena</button>
  <button class="preset" onclick="setDest('',null,null)">Clear</button>
</div>

<h2>Schedule</h2>
<label><span>Meeting title</span><input type="text" id="next_meeting_title" placeholder="leave blank for none"></label>
<label><span>Meeting time</span><input type="text" id="next_meeting_time" placeholder="e.g. 11:00"></label>
<label><span>Meeting location</span><input type="text" id="next_meeting_location" placeholder="e.g. Santa Monica"></label>
<label><span>Normal travel min</span><input type="number" id="normal_travel_minutes" placeholder="null"></label>
<label><span>Traffic delay min</span><input type="number" id="traffic_delay_minutes" value="0" min="0"></label>

<h2>Trip Memory</h2>
<div style="display:flex;gap:8px;align-items:center;margin-bottom:8px">
  <button class="preset" onclick="loadMemory()">Refresh Stats</button>
  <button class="preset" style="color:#ef4444;border-color:#ef4444" onclick="resetMemory()">Reset Memory</button>
</div>
<pre id="mem_stats" style="font-size:11px;color:#4ade80;background:#0a0a0a;padding:12px;border-radius:4px;margin:0;min-height:48px;white-space:pre-wrap">Loading…</pre>

<button id="apply" onclick="applyState()">Apply to Simulator</button>
<div id="status"></div>

<script>
const bools = { windows_open: false, sunroof_open: false, ac_on: false };

function toggleBool(key) {
  bools[key] = !bools[key];
  const btn = document.getElementById(key + '_btn');
  const labels = { windows_open: ['Open','Closed'], sunroof_open: ['Open','Closed'], ac_on: ['On','Off'] };
  btn.textContent = bools[key] ? labels[key][0] : labels[key][1];
  btn.classList.toggle('on', bools[key]);
}

function setDest(label, lat, lng) {
  document.getElementById('destination').value = label;
  document.getElementById('destination_lat').value = lat ?? '';
  document.getElementById('destination_lng').value = lng ?? '';
}

function setLocation(lat, lng, label) {
  document.getElementById('lat').value = lat;
  document.getElementById('lng').value = lng;
  document.getElementById('location_label').value = label;
}

const PRESETS = {
  low_fuel:     { fuel_percent: 12, range_km: 48, speed_kmh: 90 },
  rain_open:    { rain_in_minutes: 8, _bools: { windows_open: true, sunroof_open: true } },
  hungry:       { hours_since_meal: 5.0, current_time: '12:30' },
  late_meeting: { next_meeting_title: 'Product Review', next_meeting_time: '11:00',
                  next_meeting_location: 'Santa Monica', normal_travel_minutes: 45,
                  traffic_delay_minutes: 20, current_time: '10:15' },
  normal:       { fuel_percent: 65, range_km: 280, speed_kmh: 110,
                  rain_in_minutes: null, hours_since_meal: 1.5, current_time: '10:00',
                  _bools: { windows_open: false, sunroof_open: false, ac_on: false } },
};

function applyPreset(name) {
  const p = PRESETS[name];
  for (const [k, v] of Object.entries(p)) {
    if (k === '_bools') {
      for (const [bk, bv] of Object.entries(v)) {
        if (bools[bk] !== bv) toggleBool(bk);
      }
    } else {
      const el = document.getElementById(k);
      if (el) { el.value = v ?? ''; if (el.type === 'range') el.nextElementSibling.textContent = v; }
    }
  }
}

async function obdConnect() {
  const port = document.getElementById('obd_port').value.trim() || null;
  const res = await fetch('/obd/connect', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(port ? {port} : {}) });
  const d = await res.json();
  document.getElementById('obd_status').textContent = d.ok
    ? `✓ Connected on ${d.port} — live data active`
    : '✗ Not connected — check adapter and ignition';
  document.getElementById('obd_status').style.color = d.ok ? '#4ade80' : '#ef4444';
}

async function obdDisconnect() {
  await fetch('/obd/disconnect', { method: 'POST' });
  document.getElementById('obd_status').textContent = 'Disconnected — simulator data in use';
  document.getElementById('obd_status').style.color = '#888';
}

async function applyState() {
  const state = {};
  ['fuel_percent','range_km','speed_kmh','hours_since_meal'].forEach(k => {
    const v = document.getElementById(k)?.value;
    if (v !== '' && v != null) state[k] = parseFloat(v);
  });
  ['lat','lng'].forEach(k => {
    const v = document.getElementById(k)?.value;
    if (v) state[k] = parseFloat(v);
  });
  ['destination_lat','destination_lng'].forEach(k => {
    const v = document.getElementById(k)?.value;
    state[k] = v ? parseFloat(v) : null;
  });
  ['destination','location_label','current_time','next_meeting_title','next_meeting_time','next_meeting_location'].forEach(k => {
    const v = document.getElementById(k)?.value;
    state[k] = v || null;
  });
  ['normal_travel_minutes','traffic_delay_minutes'].forEach(k => {
    const v = document.getElementById(k)?.value;
    state[k] = v !== '' && v != null ? parseInt(v) : null;
  });
  const rain = document.getElementById('rain_in_minutes')?.value;
  state.rain_in_minutes = rain !== '' && rain != null ? parseInt(rain) : null;
  Object.assign(state, bools);

  const res = await fetch('/demo/state', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(state) });
  const data = await res.json();
  document.getElementById('status').textContent = data.ok ? '✓ Applied — dashboard updated' : '✗ Error';
  setTimeout(() => document.getElementById('status').textContent = '', 3000);
}

async function loadMemory() {
  const res = await fetch('/memory/stats');
  const d = await res.json();
  const lines = [
    `Logged: ${d.total_logged} outcomes  |  Accept rate: ${(d.accept_rate * 100).toFixed(0)}%`,
    d.preferred_cuisines.length ? `Prefers: ${d.preferred_cuisines.join(', ')}` : null,
    d.avoided_cuisines.length   ? `Avoids:  ${d.avoided_cuisines.join(', ')}`   : null,
    d.frequent_stops.length     ? `Frequent stops: ${d.frequent_stops.join(', ')}` : null,
    d.active_hours.length       ? `Active hours: ${d.active_hours.map(h => h+':00').join(', ')}` : null,
    d.total_logged < 5          ? '(Need 5+ outcomes before preferences activate)' : null,
  ].filter(Boolean);
  document.getElementById('mem_stats').textContent = lines.join('\n') || 'No data yet';
}

async function resetMemory() {
  if (!confirm('Clear all trip memory?')) return;
  await fetch('/memory/reset', { method: 'DELETE' });
  loadMemory();
}

loadMemory();
</script>
</body>
</html>"""
