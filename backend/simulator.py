import asyncio
import json
import math
import random
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Set

from signals import Signal

# ── Driving physics constants ────────────────────────────────────────────────
_TANK_LITRES    = 50.0    # assumed tank size for fuel-percent math
_L_PER_100KM    = 8.0     # average consumption (city+highway blend)
_FULL_RANGE_KM  = _TANK_LITRES / (_L_PER_100KM / 100)  # ≈ 625 km at full tank
_SPEED_NOISE_SD = 0.6     # km/h std-dev per tick (organic road variation)
_RPM_NOISE_SD   = 35      # RPM std-dev per tick

SCENARIOS_DIR = Path(__file__).parent / "scenarios"


class Simulator:
    def __init__(self):
        self.state = Signal()
        self._clients: Set[Any] = set()
        self._agents: list = []
        self._task: Optional[asyncio.Task] = None
        self._last_broadcast: float = time.monotonic()
        self._scenario_time_frozen: bool = False   # True while a scenario controls current_time
        self._stopped_since: Optional[float] = None  # monotonic time when speed first dropped below 30

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
            self._stopped_since = None
        else:
            # Only reset drive time after 2 min stopped — red lights and slow traffic don't count
            if self._stopped_since is None:
                self._stopped_since = now
            elif (now - self._stopped_since) > 120:
                self.state.minutes_driving_continuously = 0.0
                self._stopped_since = None

        if not self._scenario_time_frozen:
            self.state.current_time = datetime.now().strftime("%H:%M")

        # ── Live vehicle simulation ──────────────────────────────────────────
        self._simulate_tick(elapsed_min)

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

    # ── Live vehicle simulation ──────────────────────────────────────────────

    def _simulate_tick(self, elapsed_min: float) -> None:
        """Animate speed, RPM, fuel, and range organically each broadcast tick."""
        s = self.state
        spd = s.speed_kmh

        # Speed: gentle Gaussian noise around the current value (road micro-variation)
        if spd > 5:
            spd = max(0.0, spd + random.gauss(0, _SPEED_NOISE_SD))
            s.speed_kmh = round(spd, 1)

        # RPM: derived from speed + gear estimate + noise
        if spd < 5:
            target_rpm = random.randint(700, 900)        # idle
        elif spd < 30:
            target_rpm = int(spd * 40)                   # low gear, city crawl
        elif spd < 60:
            target_rpm = int(spd * 30)                   # 3rd–4th gear
        elif spd < 100:
            target_rpm = int(spd * 24)                   # 5th gear
        else:
            target_rpm = int(spd * 19)                   # 6th gear, highway cruise
        s.rpm = max(700, min(6500, target_rpm + int(random.gauss(0, _RPM_NOISE_SD))))

        # Fuel: consume based on distance travelled this tick
        if spd > 5 and elapsed_min > 0:
            dist_km = spd * elapsed_min / 60.0
            litres_used = dist_km * _L_PER_100KM / 100.0
            pct_used = litres_used / _TANK_LITRES * 100.0
            s.fuel_percent = max(0.0, round(s.fuel_percent - pct_used, 3))
            s.range_km = round(s.fuel_percent / 100.0 * _FULL_RANGE_KM, 1)

    # ── Scenario control ─────────────────────────────────────────────────────

    async def play(self, scenario_name: str):
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = asyncio.create_task(self._run(scenario_name))

    async def reset(self):
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
        self._scenario_time_frozen = False
        self._last_broadcast = time.monotonic()
        self.state = Signal()
        await self.broadcast()

    async def _run(self, scenario_name: str):
        path = SCENARIOS_DIR / f"{scenario_name}.json"
        if not path.exists():
            return
        frames = json.loads(path.read_text())
        for frame in frames:
            delay = frame.pop("_delay", 1.5)
            self._scenario_time_frozen = "current_time" in frame
            for key, value in frame.items():
                if hasattr(self.state, key):
                    setattr(self.state, key, value)
            await self.broadcast()
            await asyncio.sleep(delay)
        self._scenario_time_frozen = False
