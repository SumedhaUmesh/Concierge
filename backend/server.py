import asyncio
import json
import logging
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from simulator import Simulator
from signals import Suggestion
from agent.loop import AgentLoop
from tool_router import enrich
from agent import music as music_mod

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = FastAPI(title="Concierge")
sim = Simulator()

DASHBOARD = Path(__file__).parent.parent / "dashboard"

app.mount("/static", StaticFiles(directory=str(DASHBOARD / "static")), name="static")


@app.get("/")
async def root():
    return FileResponse(str(DASHBOARD / "index.html"))


@app.on_event("startup")
async def _warmup():
    # Trigger model load in background so the first WebSocket tick isn't slow
    from agent.llm import get_llm  # noqa: PLC0415
    asyncio.create_task(asyncio.to_thread(get_llm))


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    sim.add_client(websocket)

    # Push current state immediately
    await websocket.send_text(
        json.dumps({"type": "signal", "data": asdict(sim.state)})
    )

    # Per-connection agent loop
    async def _on_suggestion(suggestion: Suggestion):
        # Enrich with real POI / weather data from tools
        try:
            await enrich(suggestion, sim.state)
        except Exception:
            log.exception("Tool enrichment failed — sending bare suggestion")

        payload = json.dumps({"type": "suggestion", "data": asdict(suggestion)})
        try:
            await websocket.send_text(payload)
        except Exception:
            pass

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
            elif kind == "user_accept":
                agent.accept()
            elif kind == "music_query":
                asyncio.create_task(_handle_music(websocket, msg.get("query", "")))
    except WebSocketDisconnect:
        sim.remove_client(websocket)
        sim.remove_agent(agent)


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
