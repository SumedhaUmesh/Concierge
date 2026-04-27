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

Open `http://localhost:8000`. Pick a scenario. The agent fires when conditions warrant it —
fuel critical, rain approaching, skipped meal, late for a meeting.

Without the model the dashboard still runs fully; the agent stays silent.

## Stack

| Layer | Choice | Why |
|---|---|---|
| Model | LFM2.5-1.2B-Instruct GGUF via llama-cpp-python | on-device, Metal-accelerated, ~800 MB |
| Server | FastAPI + uvicorn | async WebSocket, non-blocking inference |
| Agent loop | Two-stage gate + generator | gate fires in ~4 tokens; generator runs only ~10% of the time |
| Structured output | GBNF grammar | pins type/urgency/action to legal enum values, eliminates parse failures |
| POI | Overpass API | keyless, real fuel/food/rest stops |
| Weather | Open-Meteo | keyless, 2-hour precipitation forecast |
| Dashboard | Plain HTML/CSS/JS + Leaflet | no build step, dark instrument-cluster aesthetic |

## Features

Five qualitatively different reasoning shapes from a single 1.2B-parameter model:

| Scenario | What the agent detects |
|---|---|
| **Range** | Fuel % vs distance to next station — escalates urgency as gap narrows |
| **Meal** | Hours since last meal + time of day + trip context |
| **Cabin** | Rain ETA vs windows/sunroof state — coordinates close + cool |
| **Schedule** | Calendar event + traffic delay → calm escalation toward lateness |
| **Music** | Natural-language taste → structured genre/energy/mood query → catalogue match |

## Architecture

```
simulator/ → WebSocket → server.py
                              │
                    per-session AgentLoop
                              │
                    ┌─────────┴─────────┐
                    │  gate (4 tokens)  │  every 8s, 3-min cooldown
                    └────────┬──────────┘
                             │ YES (~10% of ticks)
                    ┌────────▼──────────┐
                    │   generator       │  GBNF-constrained JSON
                    └────────┬──────────┘
                             │
                    ┌────────▼──────────┐
                    │  tool_router      │  Overpass + Open-Meteo
                    └────────┬──────────┘
                             │ enriched Suggestion
                         dashboard
```

The gate is the engineering challenge. A model that speaks every tick is useless.
A model that never speaks is also useless. The gate prompt enumerates exactly four
YES conditions and five NO conditions — and defaults to NO.

## Agent loop design

```
every 8s wall-clock (never stacked):
  gate(last_3_states) → YES | NO      # max_tokens=4, temperature=0
  if YES:
    generate(last_3_states) → Suggestion   # GBNF grammar, temperature=0
    tool_router.enrich(suggestion, state)  # Overpass / Open-Meteo
    → send to dashboard

cooldown: 3 minutes after any suggestion fires
dismissal: driver dismiss → gate gets "was_dismissed=true" flag → more conservative
```

## No-cloud constraint

Everything runs locally. Overpass and Open-Meteo are keyless public APIs.
No OpenAI, no Anthropic, no cloud inference. The model runs fully on-device via Metal.
