# Concierge — Architecture Reference

## Overview

Concierge is a proactive in-car AI assistant. It runs entirely on-device (Metal-accelerated)
and surfaces suggestions only when the situation genuinely warrants them. The hard problem
is **silence** — knowing when NOT to speak.

---

## System Diagram

```
 ┌─────────────────────────────────────────────────────────────────┐
 │                      FastAPI Server (backend/server.py)          │
 │                                                                   │
 │  Simulator ──1.5s tick──► WebSocket broadcast ──► Dashboard      │
 │      │                                                            │
 │      └──► AgentLoop (per WebSocket session)                       │
 │                │                                                   │
 │    ┌───────────▼───────────┐                                      │
 │    │  Cognitive Driver Model│  fatigue / load / stress → risk     │
 │    └───────────┬───────────┘                                      │
 │    ┌───────────▼───────────┐                                      │
 │    │  Gate (LFM, ~4 tokens) │  every 3s, cooldown 60–480s         │
 │    └───────────┬───────────┘  only ~10% of ticks pass             │
 │    ┌───────────▼───────────┐                                      │
 │    │  Generator (LFM GBNF) │  GBNF-constrained JSON               │
 │    └───────────┬───────────┘                                      │
 │    ┌───────────▼───────────┐                                      │
 │    │  Tool Router           │  Overpass · Open-Meteo · OSRM       │
 │    └───────────┬───────────┘                                      │
 │           enriched Suggestion ──► Dashboard + TTS                 │
 └─────────────────────────────────────────────────────────────────┘
```

---

## AI Components

### 1. Local LLM — LFM2.5-1.2B-Instruct (default)

Loaded from `models/*.gguf` via `llama-cpp-python` with Metal GPU layers.

**Used for:**
- `gate.py` — YES/NO decision on whether to fire a suggestion (~4 tokens)
- `generator.py` — generates suggestion card (type, urgency, headline, detail, action)
- `orchestrator.py` — multi-step action planning for emotional/vague inputs
- `music.py` — mood + energy extraction from natural language queries

**Constrained output:** GBNF grammar (`backend/agent/grammars/suggestion.gbnf`) pins the generator
output to valid enum values, eliminating JSON parse failures.

**Swap model:** Drop any `.gguf` file into `models/`. The server auto-loads the first one found.
Recommended upgrade: **Phi-4-mini (3.8B)** for better reasoning on compound queries.

### 2. ASR — Deepgram Nova-2

Cloud API (`api.deepgram.com/v1/listen`). Replaces the previous Whisper tiny.en.

**Why Deepgram over Whisper tiny:** Nova-2 handles Indian English, diverse accents, and
real-world audio quality significantly better than the 39M-parameter Whisper tiny model.

**Fallback:** Returns `None` if API key is missing; ASR silently disabled.

**Key:** `DEEPGRAM_API_KEY` in `.env`.

### 3. TTS — macOS `say`

Fully on-device. Wraps the macOS `say` command via asyncio subprocess.
Broadcasts `tts_start` / `tts_end` WebSocket events so the frontend pauses music during speech.

### 4. Intent Classifier — LFM (LLM-first)

Single LLM call returns structured JSON:
```json
{"intent": "meal", "cuisine": "burger", "mood": null, "cabin_action": null, ...}
```
10 intent categories: `accept · dismiss · defer · cabin · meal · music · navigate · query · compound · other`

**Fallback:** Ordered keyword rules when LLM is unavailable.

### 5. Claude via OpenRouter — Compound Query Orchestration (optional)

Activated when `OPENROUTER_API_KEY` is set in `.env`.

**Used for:** Complex multi-intent queries that require reasoning
(e.g. "find coffee on my route and tell me if I have time before my meeting").

**Why Claude over local LLM:** The 1.2B local model can't reliably do
`margin = meeting_time - travel_time - detour` and give a correct verdict.
Claude Haiku handles this correctly in one call.

**OpenRouter** (`openrouter.ai`) provides a unified API for Claude, GPT-4, Gemini,
and others. Model can be changed by editing `_MODEL` in `agent/cloud.py`.

**Fallback:** Local LFM orchestrator when key is not set.

---

## Voice Pipeline

```
Browser mic → 16kHz mono WAV → base64 → WebSocket
  ↓
Deepgram Nova-2 transcription
  ↓
no_speech guard → discard if empty
  ↓
LLM Intent Classifier (single call)
  ↓
┌─────────────────────────────────────────────────────────┐
│ accept / dismiss / defer  → direct agent action          │
│ cabin                     → _cabin_intent_to_plan()      │  ← no LLM
│ meal                      → _handle_hungry()             │
│ music                     → _handle_music()              │
│ query                     → _handle_query()              │
│ navigate (scenic)         → scenic plan + card           │  ← no LLM
│ navigate (fast)           → fast plan                    │  ← no LLM
│ compound (coffee+meeting) → _handle_coffee_schedule()    │  ← no LLM
│ compound (other)          → Claude → local LLM fallback  │
│ other / emotional         → emotional fast-path          │  ← no LLM
│                           → LLM orchestrator             │
└─────────────────────────────────────────────────────────┘
  ↓
TTS reply via macOS `say`
```

**Performance note:** Most common intents (cabin, meal, music, navigate, emotional) never
hit the LLM orchestrator — they go through deterministic fast-paths for instant, reliable response.

---

## Proactive Agent Loop

The gate runs every 3 seconds against the current vehicle state. It uses **adaptive cooldown**:

| Behaviour | Effect |
|-----------|--------|
| Driver accepts suggestion | Cooldown shortens (min 60s) |
| Driver dismisses 3× in a row | Cooldown doubles (max 480s) |
| Reset / new scenario | Full reset to 90s default |

**Gate prompt** has exactly 4 YES conditions and 5 NO conditions, defaulting to NO.
This ensures the model errs on the side of silence.

---

## Suggestion Enrichment

After the generator produces a `Suggestion`, `tool_router.enrich()` looks up real data:

| Suggestion type | Enrichment |
|-----------------|-----------|
| `range` | Overpass → nearest fuel station reachable within range |
| `meal` | Overpass → nearest food POI on route |
| `rest` | State fields first (`next_rest_stop_*`), then Overpass |
| `cabin` | Open-Meteo → rain ETA + condition string |
| `schedule` | None (meeting data is already in state) |
| `music` | None |

**Type override rule:** `cabin / range / rest / meal` types always use their canonical
enrichment action, regardless of what the LLM put in `suggested_action`. This prevents
the model from hallucinating e.g. `find_poi:fuel` for a cabin alert.

---

## Background Loops

| Loop | Interval | Purpose |
|------|----------|---------|
| `_broadcast_loop` | 1.5s | State stream to all WebSocket clients |
| `_calendar_sync_loop` | 5 min | macOS Calendar → `next_meeting_*` state fields |
| `_weather_loop` | 10 min | Open-Meteo → `rain_in_minutes` |
| `_music_evolution_loop` | 5 min | Shifts music energy up as driver fatigue increases |

---

## Data & Privacy

| Data | Where it goes |
|------|--------------|
| Voice audio | Deepgram API (transcription only, not stored) |
| Trip outcomes | Local SQLite (`backend/trip_memory.db`) |
| Vehicle state | Never leaves the machine |
| Compound queries | Claude Haiku API (only if `ANTHROPIC_API_KEY` set) |

`GET /privacy` returns a live report of all active data flows.

---

## Key Files

```
backend/
  server.py              — FastAPI app, WebSocket handler, all voice dispatch
  simulator.py           — Vehicle state + physics simulation
  signals.py             — Signal / Suggestion dataclasses
  tool_router.py         — POI + weather enrichment for suggestions
  trip_memory.py         — SQLite persistence for driver preferences

  agent/
    llm.py               — llama-cpp-python singleton (Metal)
    gate.py              — YES/NO suggestion gate
    generator.py         — GBNF-constrained suggestion generator
    orchestrator.py      — Multi-step action planner (local LFM)
    cloud.py             — Claude Haiku: Q&A fallback + compound orchestration
    music.py             — Mood/energy → track catalogue matching
    driver_model.py      — Cognitive Driver Model (fatigue/load/stress)
    loop.py              — AgentLoop (gate → generate → enrich → suggest)

    voice/
      asr.py             — Deepgram Nova-2 transcription
      intent_classifier.py — LLM-first intent + parameter extraction
      tts.py             — macOS `say` wrapper

    tools/
      poi.py             — Overpass API (fuel/food/rest POIs)
      weather.py         — Open-Meteo precipitation forecast
      route.py           — OSRM routing + travel time

dashboard/
  index.html             — Single-page dashboard
  static/app.js          — WebSocket client, map, voice recording, UI logic
  static/style.css       — Dark instrument-cluster theme
```

---

## Environment Variables (`.env`)

```
DEEPGRAM_API_KEY=...      # Required for voice input (Deepgram Nova-2)
OPENROUTER_API_KEY=...    # Optional: enables Claude for compound queries + Q&A fallback
```

Model used: `anthropic/claude-haiku-4-5-20251001` via OpenRouter. Change `_MODEL` in
`agent/cloud.py` to use any other OpenRouter model (e.g. `openai/gpt-4o-mini`).
