# Concierge

A proactive, on-device in-car AI assistant. Watches vehicle, driver, and environment
signals continuously and surfaces suggestions only when something is actually worth
saying — the hard problem is silence, not speech.


### Demo video


[![CONCIERGE demo — click to play on YouTube](https://img.youtube.com/vi/mJqeR_JMt8g/hqdefault.jpg)](https://youtu.be/mJqeR_JMt8g)

**Direct link:** [https://youtu.be/n6l0w4NIRUo](https://youtu.be/n6l0w4NIRUo)
## Quick start

```bash
# 1. Install dependencies
cd backend && python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Download the LLM model (~800 MB)
cd .. && bash scripts/fetch_model.sh

# 3. Add API keys (Deepgram required for voice)
cp .env.example .env   # then fill in keys

# 4. Start
bash scripts/run.sh
```

Open `http://localhost:8000`. The dashboard runs without a model — the agent stays silent
but the UI, map, and simulated vehicle state all work.

## API Keys

| Key | Where | Required for |
|-----|-------|-------------|
| `DEEPGRAM_API_KEY` | `.env` | Voice input (ASR) |
| `ANTHROPIC_API_KEY` | `.env` | Compound / complex query handling |

Deepgram: free tier gives $200 in credits (~50,000 min of audio). Sign up at deepgram.com.  
Anthropic: optional. Without it, compound queries fall back to the local LLM.

## Stack

| Layer | Choice | Notes |
|-------|--------|-------|
| On-device LLM | LFM2.5-1.2B-Instruct (GGUF) | gate, generator, orchestrator — Metal-accelerated |
| ASR | Deepgram Nova-2 | cloud API, excellent Indian English / accent handling |
| TTS | macOS `say` | fully on-device, no data leaves machine |
| Intent classifier | LFM (LLM-first) | single call → structured JSON, keyword fallback |
| Compound queries | Claude Haiku (optional) | meeting-time reasoning, route trade-offs |
| Server | FastAPI + uvicorn | async WebSocket, non-blocking inference |
| Structured output | GBNF grammar | pins type/urgency/action to valid enums |
| POI | Overpass API | keyless — real fuel/food/rest stops |
| Weather | Open-Meteo | keyless — 2-hour precipitation forecast |
| Routing | OSRM | keyless — real driving routes + travel times |
| Dashboard | HTML/CSS/JS + Leaflet | no build step, dark instrument-cluster theme |

## Features

### Proactive suggestions

Five qualitatively different reasoning shapes, running entirely on-device:

| Scenario | What the agent detects |
|----------|----------------------|
| **Range** | Fuel % vs distance to next reachable station |
| **Meal** | Hours since last meal + time of day + trip context |
| **Cabin** | Rain ETA vs open windows/sunroof — close + cool |
| **Schedule** | Calendar event + traffic delay → lateness risk |
| **Rest** | Drive time + time of day → Cognitive Driver Model |

### Voice interface

- Push-to-talk button or wake word ("Hey Concierge")
- **Deepgram Nova-2** transcription — handles diverse accents, background noise
- **LLM-first intent classification** — single call returns structured JSON (intent + parameters)
- Most intents handled without LLM: cabin, meal, navigate, emotional states
- Two-turn meal flow: vague ("I'm hungry") → ask cuisine → navigate; specific ("I want coffee") → skip to result
- Compound queries: "find coffee on my route and tell me if I have time before my meeting" → OSRM + calendar math

### Cognitive Driver Model

Three indexes computed every tick from raw signals:

- **Fatigue** — drive time + circadian penalty (late night / early morning)
- **Cognitive load** — speed + traffic + rain
- **Stress** — low fuel + heavy traffic + high speed

Combined into `risk_level` (low / moderate / high) shown live. High fatigue fast-tracks rest-stop threshold.

### Music concierge

- Natural-language → mood + energy match against a curated catalogue
- Auto-plays top track on voice command
- Web Audio synth preview; replaces with iTunes 30s preview where available
- Music automatically pauses when TTS speaks, resumes after

### Continuous adaptation

- **Dynamic music evolution** — shifts energy as fatigue changes (checked every 5 min)
- **Adaptive cooldown** — driver accepts → agent speaks more often; dismisses 3× → agent backs off
- **Trip memory** — learns meal/fuel/rest thresholds from trip history (SQLite)

## Architecture

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full technical reference.

Quick diagram:

```
Vehicle state (Simulator)
    │
    ├── WebSocket broadcast ──► Dashboard (1.5s)
    │
    └── AgentLoop
          │
    Cognitive Driver Model
          │
    Gate (LFM, ~4 tokens, ~10% pass rate)
          │
    Generator (LFM + GBNF grammar)
          │
    Tool Router (Overpass / Open-Meteo / OSRM)
          │
    enriched Suggestion ──► Dashboard + TTS
```

## Simulate scenarios

```bash
# Fuel warning
curl -X POST http://localhost:8000/sim/state \
  -H 'Content-Type: application/json' \
  -d '{"fuel_percent": 12, "range_km": 45, "speed_kmh": 110}'

# Rain with open windows
curl -X POST http://localhost:8000/sim/state \
  -H 'Content-Type: application/json' \
  -d '{"rain_in_minutes": 8, "windows_open": true, "sunroof_open": true}'

# Meeting + traffic (should fire lateness alert)
curl -X POST http://localhost:8000/sim/state \
  -H 'Content-Type: application/json' \
  -d '{"current_time":"14:00","next_meeting_time":"14:25","normal_travel_minutes":20,"traffic_delay_minutes":12}'

# Reset agent for fresh scenario
curl -X POST http://localhost:8000/agent/reset

# Full privacy report
curl http://localhost:8000/privacy | python3 -m json.tool
```

Run all 10 scenarios interactively:
```bash
bash scripts/test_scenarios.sh
```

## Privacy

All LLM inference runs on-device. Voice audio is sent to Deepgram only for transcription
and is not stored. Complex queries are sent to Claude Haiku only if `ANTHROPIC_API_KEY` is
set. Trip memory is local SQLite only. `GET /privacy` returns a live data-flow report.
