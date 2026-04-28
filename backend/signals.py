from dataclasses import dataclass, asdict, field
from typing import Optional


@dataclass
class Suggestion:
    type: str              # range | meal | cabin | schedule | music
    urgency: int           # 1–5
    headline: str
    detail: str
    suggested_action: str  # find_poi:fuel | find_poi:food | find_poi:rest | check_weather | none
    enriched_action: Optional[dict] = None


@dataclass
class Signal:
    # Vehicle
    fuel_percent: float = 65.0
    range_km: float = 280.0
    speed_kmh: float = 110.0
    is_on_highway: bool = True

    # Position (default: downtown Culver City)
    lat: float = 34.0211
    lng: float = -118.3965
    location_label: str = "Downtown Culver City, CA"

    # Environment
    outside_temp_c: float = 18.0
    rain_in_minutes: Optional[int] = None
    traffic_delay_minutes: int = 0

    # Cabin
    cabin_temp_c: float = 22.0
    target_temp_c: float = 22.0
    windows_open: bool = False
    sunroof_open: bool = False
    ac_on: bool = False

    # Driver
    hours_since_meal: float = 1.5
    current_time: str = "10:00"

    # Route — gas station always set (nearest on route)
    next_gas_station_name: str = "Shell, Exit 14"
    next_gas_station_km: float = 45.0
    next_gas_station_lat: float = 33.9773
    next_gas_station_lng: float = -118.4027

    # Rest stop (None when not relevant)
    next_rest_stop_km: Optional[float] = None
    next_rest_stop_lat: Optional[float] = None
    next_rest_stop_lng: Optional[float] = None

    # Schedule (None when no meeting)
    next_meeting_title: Optional[str] = None
    next_meeting_time: Optional[str] = None
    next_meeting_location: Optional[str] = None
    normal_travel_minutes: Optional[int] = None
    destination: Optional[str] = None
    destination_km: Optional[float] = None
    destination_lat: Optional[float] = None
    destination_lng: Optional[float] = None

    def to_dict(self) -> dict:
        return asdict(self)
