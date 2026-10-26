#!/usr/bin/env python3
"""
EVAC SAVIOUR - Emergency Panic Alert & Background Location Telemetry Service
Complete Python implementation of the background location tracking and panic dispatch system.

Features:
- Background asynchronous location provider (simulates live device GPS stream).
- Instant Panic Alert activation (Priority 1, zero countdown delay).
- Silent Duress mode (stealth dispatch without audible alarms).
- Geofence responder dispatch (calculates nearest police/ambulance units using Haversine distance).
- Persistent SQLite incident logging.
- Real-time telemetry broadcasting with live terminal dashboard.
"""

import asyncio
import datetime
import math
import os
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional


# ---------------------------------------------------------
# Data Models
# ---------------------------------------------------------
@dataclass
class LocationCoordinates:
    latitude: float
    longitude: float
    accuracy_meters: float
    timestamp: float


@dataclass
class EmergencyResponder:
    name: str
    unit_type: str
    phone: str
    latitude: float
    longitude: float
    status: str = "AVAILABLE"


@dataclass
class PanicAlertIncident:
    incident_id: str
    panic_type: str
    user_name: str
    phone: str
    is_silent_duress: bool
    status: str
    start_time: float
    last_known_location: LocationCoordinates
    assigned_responder: Optional[EmergencyResponder] = None
    voice_transcript: str = ""


# ---------------------------------------------------------
# Local Persistence (SQLite)
# ---------------------------------------------------------
class EmergencyDatabase:
    def __init__(self, db_path: str = "emergency_incidents.db"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS incidents (
                    incident_id TEXT PRIMARY KEY,
                    panic_type TEXT,
                    user_name TEXT,
                    phone TEXT,
                    is_silent INTEGER,
                    status TEXT,
                    latitude REAL,
                    longitude REAL,
                    timestamp REAL
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS location_breadcrumbs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    incident_id TEXT,
                    latitude REAL,
                    longitude REAL,
                    accuracy REAL,
                    timestamp REAL,
                    FOREIGN KEY(incident_id) REFERENCES incidents(incident_id)
                )
            """)
            conn.commit()

    def record_incident(self, incident: PanicAlertIncident):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO incidents 
                (incident_id, panic_type, user_name, phone, is_silent, status, latitude, longitude, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                incident.incident_id,
                incident.panic_type,
                incident.user_name,
                incident.phone,
                1 if incident.is_silent_duress else 0,
                incident.status,
                incident.last_known_location.latitude,
                incident.last_known_location.longitude,
                incident.start_time
            ))
            conn.commit()

    def log_coordinate(self, incident_id: str, loc: LocationCoordinates):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO location_breadcrumbs (incident_id, latitude, longitude, accuracy, timestamp)
                VALUES (?, ?, ?, ?, ?)
            """, (incident_id, loc.latitude, loc.longitude, loc.accuracy_meters, loc.timestamp))
            cursor.execute("""
                UPDATE incidents SET latitude = ?, longitude = ? WHERE incident_id = ?
            """, (loc.latitude, loc.longitude, incident_id))
            conn.commit()

    def close_incident(self, incident_id: str, final_status: str):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE incidents SET status = ? WHERE incident_id = ?", (final_status, incident_id))
            conn.commit()


# ---------------------------------------------------------
# Geodesic Math (Haversine Distance & ETA)
# ---------------------------------------------------------
def calculate_haversine_distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculates great-circle distance between two GPS coordinates in kilometers."""
    radius_earth_km = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon / 2) ** 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return radius_earth_km * c


def estimate_eta_minutes(distance_km: float, average_speed_kmh: float = 45.0) -> int:
    """Estimates responder arrival time based on distance and standard urban response speed."""
    hours = distance_km / average_speed_kmh
    return max(1, math.ceil(hours * 60))


# ---------------------------------------------------------
# Background Location Tracking Service
# ---------------------------------------------------------
class BackgroundLocationService:
    """
    Simulates high-precision background location provider (equivalent to Android
    FusedLocationProviderClient running inside a Foreground Service).
    Streams updates every interval seconds.
    """
    def __init__(self, base_lat: float = 28.6139, base_lng: float = 77.2090, update_interval: float = 3.0):
        self.base_lat = base_lat
        self.base_lng = base_lng
        self.update_interval = update_interval
        self._is_running = False
        self._thread: Optional[threading.Thread] = None
        self._callbacks = []
        self._step = 0

    def add_location_listener(self, callback):
        self._callbacks.append(callback)

    def start(self):
        if self._is_running:
            return
        self._is_running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._is_running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)

    def _run_loop(self):
        while self._is_running:
            self._step += 1
            # Simulate realistic pedestrian/vehicle movement jitter
            lat_jitter = (math.sin(self._step * 0.3) * 0.00015) + (self._step * 0.00008)
            lng_jitter = (math.cos(self._step * 0.3) * 0.00018) + (self._step * 0.00006)

            current_location = LocationCoordinates(
                latitude=self.base_lat + lat_jitter,
                longitude=self.base_lng + lng_jitter,
                accuracy_meters=4.2 + (math.sin(self._step) * 1.5),
                timestamp=time.time()
            )

            for cb in self._callbacks:
                try:
                    cb(current_location)
                except Exception as e:
                    print(f"[Error in Location Callback]: {e}")

            time.sleep(self.update_interval)


# ---------------------------------------------------------
# Panic Alert Controller & Dispatch Engine
# ---------------------------------------------------------
class PanicAlertController:
    def __init__(self, db: EmergencyDatabase):
        self.db = db
        self.location_service = BackgroundLocationService(update_interval=2.5)
        self.active_incident: Optional[PanicAlertIncident] = None
        self.siren_active = False
        self.flashlight_strobe = False
        self._location_history: List[LocationCoordinates] = []

        # Available responders pool
        self.responders = [
            EmergencyResponder("PCR Vehicle Echo-7", "Rapid Action Police", "+91 112", 28.6250, 77.2200),
            EmergencyResponder("Ambulance Unit DL-3C", "Advanced Life Support", "+91 108", 28.6290, 77.2010),
            EmergencyResponder("Fire Squad Station 14", "Rescue & Fire", "+91 101", 28.6350, 77.2150),
            EmergencyResponder("Verified Volunteer Rahul", "First Responder", "+91 98765 12345", 28.6190, 77.2120),
        ]

        # Register location listener
        self.location_service.add_location_listener(self._on_location_update)

    def trigger_panic_alert(self, panic_type: str = "CRITICAL PANIC ALERT", is_silent: bool = False, user_name: str = "Arjun Sharma") -> PanicAlertIncident:
        """
        Triggers an immediate emergency signal state with ZERO countdown delay.
        Activates background location tracking and alerts nearest tactical unit.
        """
        incident_id = f"PANIC-{int(time.time() * 1000)}"
        initial_loc = LocationCoordinates(latitude=28.6139, longitude=77.2090, accuracy_meters=5.0, timestamp=time.time())

        # Audio/visual beacons (only if not silent duress)
        if not is_silent:
            self.siren_active = True
            self.flashlight_strobe = True
        else:
            self.siren_active = False
            self.flashlight_strobe = False

        status_text = "SILENT DURESS BROADCAST ACTIVE (POLICE ALERTED)" if is_silent else "PRIORITY 1: IMMEDIATE PANIC SIGNAL ACTIVE"

        # Dispatch closest responder
        best_responder = self._find_nearest_responder(initial_loc.latitude, initial_loc.longitude)

        incident = PanicAlertIncident(
            incident_id=incident_id,
            panic_type=panic_type,
            user_name=user_name,
            phone="+91 98765 43210",
            is_silent_duress=is_silent,
            status=status_text,
            start_time=time.time(),
            last_known_location=initial_loc,
            assigned_responder=best_responder
        )

        self.active_incident = incident
        self.db.record_incident(incident)

        # Start background location streaming service
        self.location_service.start()

        return incident

    def cancel_panic(self):
        """Cancels active panic state and shuts down background location tracking."""
        if self.active_incident:
            self.db.close_incident(self.active_incident.incident_id, "CANCELLED_BY_USER")
            self.active_incident = None
        self.siren_active = False
        self.flashlight_strobe = False
        self.location_service.stop()

    def resolve_safe(self):
        """Marks user safe and shuts down background location tracking."""
        if self.active_incident:
            self.db.close_incident(self.active_incident.incident_id, "RESOLVED_SAFE")
            self.active_incident = None
        self.siren_active = False
        self.flashlight_strobe = False
        self.location_service.stop()

    def _find_nearest_responder(self, user_lat: float, user_lng: float) -> EmergencyResponder:
        return min(
            self.responders,
            key=lambda r: calculate_haversine_distance_km(user_lat, user_lng, r.latitude, r.longitude)
        )

    def _on_location_update(self, location: LocationCoordinates):
        if not self.active_incident:
            return

        self.active_incident.last_known_location = location
        self._location_history.append(location)
        self.db.log_coordinate(self.active_incident.incident_id, location)

        # Move assigned responder closer to the victim in real time
        if self.active_incident.assigned_responder:
            resp = self.active_incident.assigned_responder
            resp.latitude += (location.latitude - resp.latitude) * 0.15
            resp.longitude += (location.longitude - resp.longitude) * 0.15


# ---------------------------------------------------------
# Terminal Dashboard & Live Monitor Demo
# ---------------------------------------------------------
def render_live_dashboard(controller: PanicAlertController):
    """Renders real-time telemetry dashboard in console."""
    inc = controller.active_incident
    if not inc:
        print("\n[System Idle] No active emergency broadcast.")
        return

    dist = 0.0
    eta = 0
    if inc.assigned_responder:
        dist = calculate_haversine_distance_km(
            inc.last_known_location.latitude,
            inc.last_known_location.longitude,
            inc.assigned_responder.latitude,
            inc.assigned_responder.longitude
        )
        eta = estimate_eta_minutes(dist)

    os.system("clear" if os.name == "posix" else "cls")
    banner = "🚨 CRITICAL PANIC SIGNAL BROADCASTING 🚨" if not inc.is_silent_duress else "🤫 SILENT DURESS SIGNAL ACTIVE (STEALTH POLICE RELAY)"
    print("=" * 65)
    print(banner.center(65))
    print("=" * 65)
    print(f" Incident ID       : {inc.incident_id}")
    print(f" Alert Type        : {inc.panic_type}")
    print(f" Citizen Protected : {inc.user_name} ({inc.phone})")
    print(f" Status            : {inc.status}")
    print(f" GPS Coordinates   : {inc.last_known_location.latitude:.6f}, {inc.last_known_location.longitude:.6f}")
    print(f" GPS Accuracy      : ±{inc.last_known_location.accuracy_meters:.1f} meters (High-Precision)")
    print(f" Hardware Siren    : {'ACTIVE (110dB)' if controller.siren_active else 'OFF'}")
    print(f" Strobe Beacon     : {'FLASHING RED' if controller.flashlight_strobe else 'OFF'}")
    print("-" * 65)
    if inc.assigned_responder:
        print(f" Dispatched Unit   : {inc.assigned_responder.name} ({inc.assigned_responder.unit_type})")
        print(f" Responder Direct  : {inc.assigned_responder.phone}")
        print(f" Distance to Victim: {dist:.2f} km")
        print(f" Estimated ETA     : ~{eta} min{'s' if eta > 1 else ''}")
    print("=" * 65)
    print(" Background location service is streaming telemetry to dispatch...")
    print(" Type 'safe' to resolve incident, or 'cancel' to abort.")


def interactive_cli():
    db = EmergencyDatabase()
    controller = PanicAlertController(db)

    print("\n=======================================================")
    print("       EVAC SAVIOUR - PANIC ALERT TERMINAL NODE        ")
    print("=======================================================")
    print(" 1. Trigger Loud Panic Alert (Immediate Beacon + Siren)")
    print(" 2. Trigger Silent Duress Alert (Stealth Police Relay)")
    print(" 3. Exit")
    print("-------------------------------------------------------")

    try:
        choice = input("Select mode [1/2/3]: ").strip()
        if choice == "1":
            controller.trigger_panic_alert(panic_type="CRITICAL PANIC ALERT", is_silent=False)
        elif choice == "2":
            controller.trigger_panic_alert(panic_type="Silent Duress Stalking/Hostage", is_silent=True)
        else:
            print("Exiting.")
            sys.exit(0)

        # Monitor loop
        stop_event = threading.Event()

        def user_input_thread():
            while not stop_event.is_set():
                cmd = input().strip().lower()
                if cmd in ("safe", "i am safe"):
                    controller.resolve_safe()
                    print("\n[RESOLVED] User confirmed safe. Location service terminated.")
                    stop_event.set()
                    break
                elif cmd in ("cancel", "stop"):
                    controller.cancel_panic()
                    print("\n[CANCELLED] Emergency broadcast stopped. Location service terminated.")
                    stop_event.set()
                    break

        input_thread = threading.Thread(target=user_input_thread, daemon=True)
        input_thread.start()

        # Update loop
        while not stop_event.is_set() and controller.active_incident is not None:
            render_live_dashboard(controller)
            time.sleep(2.0)

    except KeyboardInterrupt:
        controller.cancel_panic()
        print("\nEmergency stopped by operator interrupt.")


if __name__ == "__main__":
    interactive_cli()