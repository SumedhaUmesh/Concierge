"""
OBD-II data source via python-obd (ELM327 adapter).

Reads real vehicle data — speed, fuel level, RPM, coolant temp.
Falls back gracefully when no adapter is present so the simulator
can still run in demo mode.

Hardware: any ELM327 adapter ($10-15 on Amazon).
  - USB:       plug in, auto-detected at /dev/cu.usbserial-*
  - Bluetooth: pair in macOS System Prefs first, then connect here
  - WiFi:      ELM327 WiFi adapters appear as a serial port too
"""

import logging
import threading
from typing import Optional

log = logging.getLogger(__name__)

# Standard OBD-II PIDs we read
_PIDS = {
    "speed":   "SPEED",          # km/h
    "fuel":    "FUEL_LEVEL",     # %
    "rpm":     "RPM",            # rev/min
    "coolant": "COOLANT_TEMP",   # °C
    "intake":  "INTAKE_TEMP",    # °C (cabin proxy)
}

# Rough range estimate when no dedicated range PID is available
_KM_PER_FUEL_PCT = 5.0   # 500 km at 100% fuel


class OBDSource:
    """
    Thread-safe wrapper around an obd.OBD connection.

    Call connect() once at startup. read() returns a dict of Signal-compatible
    field overrides — only fields the adapter successfully reported.
    """

    def __init__(self):
        self._conn = None
        self._lock = threading.Lock()
        self._connected = False
        self._port: Optional[str] = None

    # ── Connection ────────────────────────────────────────────────────────────

    def connect(self, port: Optional[str] = None) -> bool:
        """
        Connect to ELM327 adapter.
        port=None → python-obd auto-detects USB/Bluetooth serial port.
        Returns True if the car's ECU is responding.
        """
        try:
            import obd  # import here so server still starts without obd installed
            conn = obd.OBD(port, fast=False, timeout=5)
            if conn.is_connected():
                self._conn = conn
                self._connected = True
                self._port = conn.port_name()
                log.info("OBD-II connected on %s — protocols: %s",
                         self._port, conn.protocol_name())
                return True
            else:
                log.warning("OBD-II adapter found but ECU not responding. "
                            "Is the ignition on?")
                return False
        except Exception:
            log.exception("OBD-II connection failed")
            return False

    def disconnect(self):
        with self._lock:
            if self._conn:
                try:
                    self._conn.close()
                except Exception:
                    pass
            self._conn = None
            self._connected = False
        log.info("OBD-II disconnected")

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def port(self) -> Optional[str]:
        return self._port

    # ── Reading ───────────────────────────────────────────────────────────────

    def read(self) -> dict:
        """
        Query the adapter for current values.
        Returns a dict of Signal field overrides (only successfully read fields).
        Empty dict if not connected.
        """
        if not self._connected or self._conn is None:
            return {}

        overrides: dict = {}

        with self._lock:
            try:
                import obd

                speed_r = self._conn.query(obd.commands.SPEED)
                if not speed_r.is_null():
                    overrides["speed_kmh"] = round(float(speed_r.value.to("kph").magnitude), 1)

                fuel_r = self._conn.query(obd.commands.FUEL_LEVEL)
                if not fuel_r.is_null():
                    pct = round(float(fuel_r.value.magnitude), 1)
                    overrides["fuel_percent"] = pct
                    overrides["range_km"] = round(pct * _KM_PER_FUEL_PCT, 0)

                rpm_r = self._conn.query(obd.commands.RPM)
                if not rpm_r.is_null():
                    overrides["_rpm"] = round(float(rpm_r.value.magnitude))   # internal only

                coolant_r = self._conn.query(obd.commands.COOLANT_TEMP)
                if not coolant_r.is_null():
                    # Proxy: outside temp ≈ coolant when engine cold, skip when hot
                    temp_c = float(coolant_r.value.to("degC").magnitude)
                    if temp_c < 40:  # engine cold = ambient approximation
                        overrides["outside_temp_c"] = round(temp_c, 1)

            except Exception:
                log.exception("OBD read error — returning partial data")

        return overrides

    def status_dict(self) -> dict:
        return {
            "connected": self._connected,
            "port": self._port,
        }


# Module-level singleton — imported by server.py
obd_source = OBDSource()
