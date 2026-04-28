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
from agent.voice import tts, asr, classifier

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
            elif kind == "user_accept":
                agent.accept()
            elif kind == "mute":
                tts.set_muted(msg.get("muted", False))
            elif kind == "music_query":
                asyncio.create_task(_handle_music(websocket, msg.get("query", "")))
            elif kind == "voice_input":
                asyncio.create_task(
                    _handle_voice(websocket, agent, latest_suggestion, msg.get("audio", ""))
                )
    except WebSocketDisconnect:
        sim.remove_client(websocket)
        sim.remove_agent(agent)


async def _handle_voice(
    websocket: WebSocket,
    agent: AgentLoop,
    latest_suggestion: list,
    audio_b64: str,
):
    transcript = await asr.transcribe(audio_b64)
    if not transcript:
        return

    # Echo transcript to dashboard
    try:
        await websocket.send_text(json.dumps({
            "type": "transcript",
            "data": {"text": transcript},
        }))
    except Exception:
        pass

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

    elif intent == "music":
        await _handle_music(websocket, transcript)

    elif intent == "defer":
        agent.dismiss()
        asyncio.create_task(tts.speak("Got it. I'll check back in a few minutes."))

    # intent == "query": no action, transcript already echoed to dashboard


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
