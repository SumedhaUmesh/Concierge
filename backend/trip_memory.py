"""
SQLite-backed trip memory: logs suggestion outcomes and extracts driver preferences.

Every accepted or dismissed suggestion is stored. After 5+ outcomes the system
extracts preference signals (cuisine, frequent stops, active hours) and injects
them into the generator prompt so suggestions become personalised over time.
"""

import datetime
import logging
import sqlite3
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_DB_PATH = Path(__file__).parent / "trip_memory.db"
_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None


# ── Schema ────────────────────────────────────────────────────────────────────

def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("""
            CREATE TABLE IF NOT EXISTS suggestions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          REAL    NOT NULL,
                type        TEXT    NOT NULL,
                headline    TEXT,
                place_name  TEXT,
                cuisine     TEXT,
                hour        INTEGER,
                day_of_week INTEGER,
                outcome     TEXT    NOT NULL
            )
        """)
        _conn.commit()
    return _conn


# ── Write ─────────────────────────────────────────────────────────────────────

def log_outcome(suggestion, outcome: str, state=None) -> None:
    """
    Record a suggestion outcome.
    outcome: "accepted" | "dismissed"
    state: Signal dataclass (for hour/day-of-week context)
    """
    s = asdict(suggestion) if hasattr(suggestion, "__dataclass_fields__") else dict(suggestion)

    now = time.time()
    hour: Optional[int] = None
    dow: Optional[int] = None

    if state:
        try:
            time_str = getattr(state, "current_time", None) or ""
            if time_str:
                hour = int(str(time_str).split(":")[0])
        except (ValueError, AttributeError):
            pass
        dow = datetime.datetime.fromtimestamp(now).weekday()  # 0=Monday

    enriched = s.get("enriched_action") or {}
    place_name = enriched.get("place_name") if isinstance(enriched, dict) else None

    # Extract cuisine from meal detail ("Italian · 1.2 km away" → "Italian")
    cuisine: Optional[str] = None
    if s.get("type") == "meal":
        detail = s.get("detail", "")
        cuisine = detail.split("·")[0].strip() if "·" in detail else None

    with _lock:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO suggestions "
            "(ts, type, headline, place_name, cuisine, hour, day_of_week, outcome) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (now, s.get("type", ""), s.get("headline", ""),
             place_name, cuisine, hour, dow, outcome),
        )
        conn.commit()

    log.info("trip_memory: %s → %s (place=%s)", s.get("type"), outcome, place_name)


# ── Read / Preference extraction ──────────────────────────────────────────────

def get_preferences() -> dict:
    """Compute preference signals from logged history."""
    with _lock:
        conn = _get_conn()

        # Cuisine accept rates — only count cuisines seen ≥2 times
        cuisine_rows = conn.execute("""
            SELECT cuisine,
                   SUM(CASE WHEN outcome='accepted' THEN 1 ELSE 0 END) AS accepted,
                   COUNT(*) AS total
            FROM suggestions
            WHERE type='meal' AND cuisine IS NOT NULL AND cuisine != ''
            GROUP BY cuisine
            HAVING total >= 2
        """).fetchall()

        preferred_cuisines, avoided_cuisines = [], []
        for row in cuisine_rows:
            rate = row["accepted"] / row["total"]
            if rate >= 0.6:
                preferred_cuisines.append(row["cuisine"])
            elif rate <= 0.2:
                avoided_cuisines.append(row["cuisine"])

        # Places accepted ≥2 times
        stop_rows = conn.execute("""
            SELECT place_name, COUNT(*) AS cnt
            FROM suggestions
            WHERE outcome='accepted' AND place_name IS NOT NULL AND place_name != ''
            GROUP BY place_name
            HAVING cnt >= 2
            ORDER BY cnt DESC
            LIMIT 5
        """).fetchall()
        frequent_stops = [r["place_name"] for r in stop_rows]

        # Hours where acceptance rate ≥ 50% (need ≥3 samples per hour)
        hour_rows = conn.execute("""
            SELECT hour,
                   SUM(CASE WHEN outcome='accepted' THEN 1 ELSE 0 END) AS accepted,
                   COUNT(*) AS total
            FROM suggestions
            WHERE hour IS NOT NULL
            GROUP BY hour
            HAVING total >= 3
            ORDER BY hour
        """).fetchall()
        active_hours = [r["hour"] for r in hour_rows
                        if r["accepted"] / r["total"] >= 0.5]

        totals = conn.execute("""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN outcome='accepted' THEN 1 ELSE 0 END) AS accepted
            FROM suggestions
        """).fetchone()

    total = totals["total"] if totals else 0
    accepted = totals["accepted"] if totals else 0
    return {
        "preferred_cuisines": preferred_cuisines,
        "avoided_cuisines": avoided_cuisines,
        "frequent_stops": frequent_stops,
        "active_hours": active_hours,
        "total_logged": total,
        "accept_rate": round(accepted / max(total, 1), 2),
    }


def get_context_string() -> Optional[str]:
    """
    Return a short preference summary for prompt injection.
    Returns None when fewer than 5 outcomes have been logged.
    """
    prefs = get_preferences()
    if prefs["total_logged"] < 5:
        return None

    lines = []
    if prefs["preferred_cuisines"]:
        lines.append(f"Prefers: {', '.join(prefs['preferred_cuisines'])}")
    if prefs["avoided_cuisines"]:
        lines.append(f"Avoids: {', '.join(prefs['avoided_cuisines'])}")
    if prefs["frequent_stops"]:
        lines.append(f"Frequent stops: {', '.join(prefs['frequent_stops'][:3])}")
    if prefs["active_hours"]:
        hr_str = _compress_hours(prefs["active_hours"])
        if hr_str:
            lines.append(f"Typically receptive: {hr_str}")

    if not lines:
        return None

    return "Driver preferences (from trip history):\n" + "\n".join(f"- {l}" for l in lines)


def _compress_hours(hours: list) -> str:
    """[11, 12, 13, 17, 18] → '11:00–14:00, 17:00–19:00'"""
    if not hours:
        return ""
    hours = sorted(set(hours))
    ranges = []
    start = end = hours[0]
    for h in hours[1:]:
        if h == end + 1:
            end = h
        else:
            ranges.append((start, end))
            start = end = h
    ranges.append((start, end))
    return ", ".join(
        f"{s:02d}:00–{e + 1:02d}:00" if s != e else f"{s:02d}:00"
        for s, e in ranges
    )
