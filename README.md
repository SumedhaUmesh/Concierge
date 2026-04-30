# Concierge

A proactive, on-device in-car AI assistant. Watches vehicle, driver, and environment
signals continuously and surfaces suggestions only when something is actually worth
saying — the hard problem is silence, not speech.

Inspired by the Mercedes-Benz × Liquid AI partnership (April 2026). Independent build.

## Run in 5 minutes

```bash
# 1. Install dependencies
cd backend
python3.13 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Download the model (~800 MB, requires HF account)
cd .. && bash scripts/fetch_model.sh

# 3. Start
bash scripts/run.sh
```

Open `http://localhost:8000`. The agent fires when conditions warrant it —
fuel critical, rain approaching, skipped meal, late for a meeting.

Without the model the dashboard still runs fully; the agent stays silent.

## Stack

| Layer | Choice | Why |
|---|---|---|
| Model | LFM2.5-1.2B-Instruct GGUF via llama-cpp-python | on-device, Metal-accelerated, ~800 MB |
| Server | FastAPI + uvicorn | async WebSocket, non-blocking inference |
| Agent loop | Two-stage gate + generator | gate fires in ~4 tokens; generator runs only ~10% of the time |
| Structured output | GBNF grammar | pins type/urgency/action to legal enum values, eliminates parse failures |
| Voice | Whisper (local ASR) + macOS `say` (TTS) | fully on-device, no mic data leaves the machine |
| POI | Overpass API | keyless, real fuel/food/rest stops |
| Weather | Open-Meteo | keyless, 2-hour precipitation forecast |
| Routing | OSRM | keyless, real driving route + polyline |
| Dashboard | Plain HTML/CSS/JS + Leaflet | no build step, dark instrument-cluster aesthetic |

## Features

### Proactive suggestions

Five qualitatively different reasoning shapes from a single 1.2B-parameter model:

| Scenario | What the agent detects |
|---|---|
| **Range** | Fuel % vs distance to next station — escalates urgency as gap narrows |
| **Meal** | Hours since last meal + time of day + trip context |
| **Cabin** | Rain ETA vs windows/sunroof state — coordinates close + cool |
| **Schedule** | Calendar event + traffic delay → calm escalation toward lateness |
| **Rest** | Drive time + time of day → Cognitive Driver Model → rest stop suggestion |

### Cognitive Driver Model

Computes three indexes every tick from raw vehicle/time signals:

- **Fatigue index** — drive time + circadian penalty (late night / early morning)
- **Cognitive load** — speed + traffic delay + rain
- **Stress index** — low fuel + heavy traffic + high speed

Combined into a `risk_level` (low / moderate / high) shown live in the sidebar. High fatigue fast-tracks the rest-stop gate threshold.

### Voice interface

- Push-to-talk or wake word ("Hey Concierge") via browser Speech Recognition
- Whisper transcription runs fully on-device
- Natural language understood by the LLM orchestrator — emotional, compound, indirect
- Emotional fast-path: keyword match bypasses LLM entirely for instant reliable response
  - "I'm tired" → cool cabin + calm music + reduced alerts, no LLM call
  - "I'm stressed" → calm music + comfortable temperature
  - "I'm energetic" → upbeat music
- Two-turn meal flow: "I'm hungry" → Concierge asks cuisine preference → navigates

### Intent Decomposition Engine

Parses compound utterances into ordered, typed sub-intents without an LLM call:

```
"I'm hungry and need gas"   → [fuel(4), meal(3)]
"Find a scenic route"       → [navigate(3, scenic=True)]
"I'm tired and want music"  → [rest(4), music(2)]
```

Scenic / fast route preferences attach as modifiers to the navigate intent.

### Music concierge

- Natural-language query → catalogue match by mood + energy + genre
- Synthesized preview using Web Audio API (tempo-locked, energy-matched oscillators)
- Falls back to iTunes 30-second preview when available
- Pause / resume / stop controls

### Continuous Adaptation

Two background loops that run for the lifetime of the server:

**Dynamic Music Evolution** — re-evaluates every 5 minutes as fatigue changes. Gradually shifts music energy upward as the driver gets sleepier (calm at fatigue < 0.3, driving-pumped at fatigue > 0.85). Only fires when energy shifts by ≥2 levels to avoid pointless re-queuing.

**Conference Call Mode** — polls calendar every 30 seconds. Five minutes before any meeting:
- Announces "Entering quiet mode" via TTS
- Mutes all future TTS output
- Suppresses proactive agent alerts for 60 minutes
- Closes windows / sunroof, turns on AC for a quiet cabin
- Shows a **🔇 QUIET MODE** badge on the dashboard
- Automatically restores normal mode after the meeting window passes

### Shared Control Model

Driver behavior adjusts future agent cadence in real time:

- Accept streak → shorter cooldown (agent speaks more often, down to 60 s floor)
- Dismiss streak → longer cooldown (agent backs off, up to 480 s ceiling)
- Geofence memory: re-surfaces a previously accepted place when you're within 500 m again

### Privacy

All inference runs on-device. Voice audio is discarded immediately after transcription.
Trip outcomes are stored in a local SQLite file (`backend/trip_memory.db`) — never uploaded.
Cloud calls are limited to keyless public APIs (Nominatim, Open-Meteo, OSRM, Overpass).
Claude Haiku is called only as a Q&A fallback when the local model answer is too short,
and only if `ANTHROPIC_API_KEY` is set. See `GET /privacy` for the full report.

## Architecture

```
Simulator (vehicle signals)
        │
        └─── WebSocket broadcast (1.5 s) ──► Dashboard
        │
        └─── per-session AgentLoop
                    │
          ┌─────────┴─────────┐
          │  Cognitive Driver  │  fatigue / load / stress → risk_level
          │  Model (every tick)│
          └─────────┬──────────┘
                    │
          ┌─────────┴─────────┐
          │  Gate (4 tokens)  │  every 3 s, adaptive cooldown 60–480 s
          └─────────┬──────────┘
                    │ YES (~10% of ticks)
          ┌─────────▼──────────┐
          │   Generator        │  GBNF-constrained JSON
          └─────────┬──────────┘
                    │
          ┌─────────▼──────────┐
          │  Tool Router        │  Overpass + Open-Meteo + OSRM
          └─────────┬──────────┘
                    │ enriched Suggestion
                Dashboard + TTS

Background loops (always running):
  _broadcast_loop      1.5 s   state stream to all WS clients
  _calendar_sync_loop  5 min   macOS Calendar → next_meeting_*
  _weather_loop        10 min  Open-Meteo → rain_in_minutes
  _music_evolution_loop 5 min  fatigue → music energy shift
  _meeting_watch_loop  30 s    calendar → quiet mode trigger
```

The gate is the engineering challenge. A model that speaks every tick is useless.
A model that never speaks is also useless. The gate prompt enumerates exactly four
YES conditions and five NO conditions — and defaults to NO.

## Voice pipeline

```
browser mic → WAV (16 kHz mono) → base64 → WebSocket
  → server: Whisper transcription (on-device)
  → classifier: accept | dismiss | defer | other
      ├─ accept/dismiss/defer → direct agent action
      └─ other → Emotional fast-path (keyword match, no LLM)
                    ├─ match → pre-built plan → _execute_plan
                    └─ no match → Intent Decomposition Engine
                                    ├─ scenic/fast nav → plan
                                    └─ other → LLM orchestrator
                                                  → _normalize_action
                                                  → _execute_plan
  ← TTS reply via macOS `say` (queued, no overlap)
```

## Simulate state via API

```bash
# Set fatigue high (triggers rest-stop gate)
curl -X POST http://localhost:8000/sim/state \
  -H 'Content-Type: application/json' \
  -d '{"minutes_driving_continuously": 120, "speed_kmh": 100}'

# Simulate an upcoming meeting (triggers quiet mode in ~5 min)
curl -X POST http://localhost:8000/sim/state \
  -H 'Content-Type: application/json' \
  -d '{"next_meeting_title": "Team standup", "next_meeting_time": "HH:MM"}'

# Reset agent cooldown so next gate tick can fire immediately
curl -X POST http://localhost:8000/agent/reset

# Full privacy report
curl http://localhost:8000/privacy
```

## No-cloud constraint

Everything runs locally. Overpass, Open-Meteo, OSRM, and Nominatim are keyless public APIs.
No OpenAI, no Anthropic, no cloud inference by default. The LFM2.5-1.2B model runs fully
on-device via Metal. Claude Haiku is an opt-in fallback for the Q&A path only.
