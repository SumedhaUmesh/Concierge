import asyncio
import json
from dataclasses import asdict
from pathlib import Path
from typing import Set, Any, Optional

from signals import Signal

SCENARIOS_DIR = Path(__file__).parent / "scenarios"


class Simulator:
    def __init__(self):
        self.state = Signal()
        self._clients: Set[Any] = set()
        self._task: Optional[asyncio.Task] = None

    # ── Client management ────────────────────────────────────────────────────

    def add_client(self, ws):
        self._clients.add(ws)

    def remove_client(self, ws):
        self._clients.discard(ws)

    # ── Broadcast ────────────────────────────────────────────────────────────

    async def broadcast(self):
        if not self._clients:
            return
        payload = json.dumps({"type": "signal", "data": asdict(self.state)})
        dead = set()
        for ws in self._clients:
            try:
                await ws.send_text(payload)
            except Exception:
                dead.add(ws)
        self._clients -= dead

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
