# Concierge

A proactive, on-device in-car AI assistant. Watches vehicle, driver, and environment
signals continuously and surfaces suggestions only when something is actually worth
saying — the hard problem is silence, not speech.

Inspired by the Mercedes-Benz × Liquid AI partnership (April 2026). Independent build.

## Running (Week 1 — no AI)

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn server:app --reload
```

Open `http://localhost:8000` — the dashboard loads and connects over WebSocket.
Use the scenario buttons to drive the simulator.

## Stack

| Layer | Choice | Why |
|---|---|---|
| Server | FastAPI + uvicorn | async WebSocket, zero boilerplate |
| Model (Week 2+) | LFM2.5-1.2B GGUF via llama-cpp-python | on-device, Metal-accelerated |
| Dashboard | Plain HTML/CSS/JS | no build step, inspectable, fast to iterate |
| Map | Leaflet + CartoDB dark tiles | keyless, looks right |
| POI | Overpass API | keyless, real data |
| Weather | Open-Meteo | keyless, hourly forecasts |

## Thesis

The two-stage agent loop (Week 2):

```
every 10s:
  gate(signals) → should_speak: bool      # cheap, grammar-pinned JSON
  if should_speak:
    generate(signals) → alert: str        # full response, fires rarely
```

The gate is the engineering challenge. The generator is easy once the gate works.
