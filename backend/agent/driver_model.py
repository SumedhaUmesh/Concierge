"""
Cognitive Driver Model.

Estimates fatigue, cognitive load, and stress from vehicle telemetry.
Used by the gate to adjust suggestion urgency and by the UI to show driver state.
"""

from dataclasses import dataclass


@dataclass
class DriverState:
    fatigue_index: float    # 0.0–1.0  (0 = alert, 1 = severely fatigued)
    cognitive_load: float   # 0.0–1.0  (0 = relaxed, 1 = overloaded)
    stress_index: float     # 0.0–1.0
    risk_level: str         # "low" | "moderate" | "high"


def compute(signal) -> DriverState:
    drive_min = getattr(signal, "minutes_driving_continuously", 0) or 0

    # Fatigue: continuous drive time (saturates at 2 h)
    fatigue_from_drive = min(drive_min / 120.0, 1.0)

    # Time-of-day drowsiness — peak 2–5 am, secondary 14–16 pm
    try:
        h = int(str(getattr(signal, "current_time", "12:00")).split(":")[0])
        tod = 0.75 if 2 <= h <= 5 else (0.25 if 14 <= h <= 16 else 0.0)
    except (ValueError, AttributeError):
        tod = 0.0

    fatigue = min(fatigue_from_drive + tod * (1 - fatigue_from_drive), 1.0)

    # Cognitive load: speed + traffic + weather
    speed        = getattr(signal, "speed_kmh", 0) or 0
    traffic_min  = getattr(signal, "traffic_delay_minutes", 0) or 0
    rain_min     = getattr(signal, "rain_in_minutes", None)

    load_speed   = min(speed / 140.0, 1.0)
    load_traffic = min(traffic_min / 30.0, 1.0)
    load_rain    = 0.4 if rain_min is not None and rain_min < 15 else 0.0

    cognitive_load = load_speed * 0.4 + load_traffic * 0.35 + load_rain * 0.25

    # Stress: fuel urgency + time pressure + speed
    fuel = getattr(signal, "fuel_percent", 100) or 100
    stress_fuel    = min(max(0, 20 - fuel) / 20.0, 1.0)
    stress_traffic = min(traffic_min / 20.0, 1.0)
    stress_speed   = min(speed / 160.0, 1.0)
    stress = stress_fuel * 0.45 + stress_traffic * 0.35 + stress_speed * 0.20

    # Composite risk
    combined = fatigue * 0.45 + cognitive_load * 0.35 + stress * 0.20
    if combined > 0.60:
        risk = "high"
    elif combined > 0.30:
        risk = "moderate"
    else:
        risk = "low"

    return DriverState(
        fatigue_index=round(fatigue, 2),
        cognitive_load=round(cognitive_load, 2),
        stress_index=round(stress, 2),
        risk_level=risk,
    )
