import asyncio
import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional, Set

from signals import Signal

SCENARIOS_DIR = Path(__file__).parent / "scenarios"


class Simulator:
    def __init__(self):
        self.state = Signal()
        self._clients: Set[Any] = set()
        self._agents: list = []
        self._task: Optional[asyncio.Task] = None
        self._last_broadcast: float = time.monotonic()

    # ── Client management ────────────────────────────────────────────────────

    def add_client(self, ws):
        self._clients.add(ws)

    def remove_client(self, ws):
        self._clients.discard(ws)

    def add_agent(self, agent):
        self._agents.append(agent)

    def remove_agent(self, agent):
        try:
            self._agents.remove(agent)
        except ValueError:
            pass

    # ── Broadcast ────────────────────────────────────────────────────────────

    async def broadcast(self):
        if not self._clients:
            return
        # Track continuous driving time
        now = time.monotonic()
        elapsed_min = (now - self._last_broadcast) / 60.0
        self._last_broadcast = now
        if self.state.speed_kmh > 30:
            self.state.minutes_driving_continuously += elapsed_min
        else:
            self.state.minutes_driving_continuously = 0.0

        # Overlay real OBD-II readings on top of simulated state
        try:
            from obd_source import obd_source
            for key, val in obd_source.read().items():
                if not key.startswith("_") and hasattr(self.state, key):
                    setattr(self.state, key, val)
        except Exception:
            pass
        payload = json.dumps({"type": "signal", "data": asdict(self.state)})
        dead: Set[Any] = set()
        for ws in self._clients:
            try:
                await ws.send_text(payload)
            except Exception:
                dead.add(ws)
        self._clients -= dead

        # Tick all registered agent loops (non-blocking)
        for agent in list(self._agents):
            asyncio.create_task(agent.tick(self.state))

    # ── Scenario control ─────────────────────────────────────────────────────

    async def play(self, scenario_name: str):
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = asyncio.create_task(self._run(scenario_name))

    async def reset(self):
        if self._task and not self._task.done():
            self._task.cancel()
        self.state = Signal()
        await self.broadcast()

    async def _run(self, scenario_name: str):
        path = SCENARIOS_DIR / f"{scenario_name}.json"
        if not path.exists():
            return
        frames = json.loads(path.read_text())
        for frame in frames:
            delay = frame.pop("_delay", 1.5)
            for key, value in frame.items():
                if hasattr(self.state, key):
                    setattr(self.state, key, value)
            await self.broadcast()
            await asyncio.sleep(delay)
