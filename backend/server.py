import json
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from simulator import Simulator

app = FastAPI(title="Concierge")
sim = Simulator()

DASHBOARD = Path(__file__).parent.parent / "dashboard"

# Static assets (/static/style.css, /static/app.js)
app.mount("/static", StaticFiles(directory=str(DASHBOARD / "static")), name="static")


@app.get("/")
async def root():
    return FileResponse(str(DASHBOARD / "index.html"))


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    sim.add_client(websocket)

    # Push current state immediately so the dashboard isn't blank on connect
    await websocket.send_text(
        json.dumps({"type": "signal", "data": asdict(sim.state)})
    )

    try:
        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            kind = msg.get("type")

            if kind == "play":
                await sim.play(msg["scenario"])
            elif kind == "reset":
                await sim.reset()
    except WebSocketDisconnect:
        sim.remove_client(websocket)
