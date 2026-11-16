"""Representation model of the Aruba home: rooms, sensors, doors and one resident.

The model holds no MQTT code. Feed it sensor events with ``apply`` and let time
pass with ``tick``. Read the current state with ``snapshot``. Every change that
matters to a user is also returned as a transition (room entered, door opened,
resident left home, and so on).
"""

from collections import deque
from datetime import datetime, timedelta
import json
from pathlib import Path


# Tunable assumptions of the model. They are not learned from the replay period.
CONFIRM_JUMP_WINDOW = timedelta(seconds=60)   # a jump to a non-adjacent room needs a second
                                              # motion there within this time
AWAY_AFTER = timedelta(minutes=5)             # silence after using an exit door before "away"
EXIT_WINDOW = timedelta(minutes=3)            # how close door use and last motion must be
MULTI_PERSON_WINDOW = timedelta(minutes=10)
MULTI_PERSON_JUMPS = 3                        # rejected jumps in the window that suggest a visitor


def load_home(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


class HomeModel:
    def __init__(self, home):
        self.home = home
        self.rooms = home["rooms"]
        self.layout = home["sensors"]
        self.exit_sensor_door = {sensor: door for door, info in home["exit_doors"].items()
                                 for sensor in info["motion_sensors"]}
        self.now = None
        self.sensors = {sensor: {"value": None, "timestamp": None, "invalid_readings": 0,
                                 "last_seen": None} for sensor in self.layout}
        self.doors = {sensor: {"state": "unknown", "since": None}
                      for sensor, info in self.layout.items() if info["sensor_type"] == "door"}
        self.temperatures = {}        # room -> {"value", "timestamp", "sensor"}
        self.room_last_motion = {room: None for room in self.rooms}
        self.resident = {"presence": "unknown", "room": None, "since": None,
                         "last_motion": None, "last_motion_sensor": None}
        self.pending_jump = None      # (room, timestamp, sensor) waiting for confirmation
        self.rejected_jumps = deque()
        self.last_exit_door_use = None  # (door, timestamp)
        self.counters = {"events": 0, "invalid": 0, "room_changes": 0,
                         "confirmed_jumps": 0, "rejected_jumps": 0}
        # Filled in by the machine-learning step.
        self.activity = None
        self.alerts = []

    # ----- initial situation ------------------------------------------------

    def load_initial_state(self, initial):
        """Start from the last observations before the replay window (no transitions)."""
        self.now = datetime.fromisoformat(initial["as_of"])
        latest_motion = None
        for sensor, record in initial["sensors"].items():
            if sensor not in self.layout:
                continue
            observation = record.get("last_valid_observation")
            if observation:
                stamp = datetime.fromisoformat(observation["timestamp"])
                self._store(sensor, observation["value"], stamp)
            motion_on = record.get("last_motion_on")
            if motion_on:
                stamp = datetime.fromisoformat(motion_on)
                room = self.layout[sensor]["room"]
                if not self.room_last_motion[room] or stamp > self.room_last_motion[room]:
                    self.room_last_motion[room] = stamp
                if not latest_motion or stamp > latest_motion[0]:
                    latest_motion = (stamp, sensor)
        if latest_motion:
            stamp, sensor = latest_motion
            self.resident.update(presence="home", room=self.layout[sensor]["room"], since=stamp,
                                 last_motion=stamp, last_motion_sensor=sensor)

    def _store(self, sensor, value, stamp):
        kind = self.layout[sensor]["sensor_type"]
        self.sensors[sensor].update(value=value, timestamp=stamp, last_seen=stamp)
        if kind == "door":
            self.doors[sensor] = {"state": value, "since": stamp}
        elif kind == "temperature":
            self.temperatures[self.layout[sensor]["room"]] = {
                "value": value, "timestamp": stamp, "sensor": sensor}

    # ----- sensor events ----------------------------------------------------

    def apply(self, event):
        """Update the model with one sensor reading. Returns a list of transitions."""
        sensor = event["sensor_id"]
        if sensor not in self.layout:
            return []
        stamp = datetime.fromisoformat(event["timestamp"])
        transitions = self.tick(stamp)
        self.counters["events"] += 1
        record = self.sensors[sensor]
        record["last_seen"] = stamp
        if event.get("value") is None:
            # A garbled reading says the sensor is alive but tells us nothing about the home.
            record["invalid_readings"] += 1
            self.counters["invalid"] += 1
            transitions.append(self._transition("sensor_invalid_reading", stamp, sensor=sensor,
                                                raw_value=event.get("raw_value")))
            return transitions

        kind = self.layout[sensor]["sensor_type"]
        previous = record["value"]
        self._store(sensor, event["value"], stamp)
        if kind == "door" and event["value"] != previous:
            name = self.home["exit_doors"].get(sensor, {}).get("name", sensor)
            transitions.append(self._transition(
                "door_opened" if event["value"] == "OPEN" else "door_closed", stamp,
                sensor=sensor, door=name))
            if sensor in self.home["exit_doors"]:
                self.last_exit_door_use = (sensor, stamp)
        elif kind == "motion" and event["value"] == "ON":
            transitions += self._motion(sensor, stamp)
        return transitions

    def _motion(self, sensor, stamp):
        transitions = []
        room = self.layout[sensor]["room"]
        self.room_last_motion[room] = stamp
        resident = self.resident
        if resident["presence"] == "away":
            transitions.append(self._transition("resident_returned", stamp, room=room, sensor=sensor))
        resident["presence"] = "home"
        resident["last_motion"], resident["last_motion_sensor"] = stamp, sensor
        current = resident["room"]
        if room == current:
            self.pending_jump = None
        elif current is None or room in self.rooms[current]["adjacent"]:
            transitions.append(self._move(room, stamp, sensor))
        elif (self.pending_jump and self.pending_jump[0] == room
              and stamp - self.pending_jump[1] <= CONFIRM_JUMP_WINDOW):
            # Two motions in a row in the far room: the resident really is there and a
            # sensor on the way was missed.
            self.counters["confirmed_jumps"] += 1
            transitions.append(self._move(room, stamp, sensor))
        else:
            # A single motion in a room the resident cannot have reached directly.
            # Keep the current room until a second motion confirms the jump.
            self.pending_jump = (room, stamp, sensor)
            self.counters["rejected_jumps"] += 1
            self.rejected_jumps.append(stamp)
        return transitions

    def _move(self, room, stamp, sensor):
        previous = self.resident["room"]
        self.resident.update(room=room, since=stamp)
        self.pending_jump = None
        self.counters["room_changes"] += 1
        return self._transition("room_entered", stamp, room=room, previous_room=previous, sensor=sensor)

    # ----- passage of time --------------------------------------------------

    def tick(self, now):
        """Advance dataset time. Returns transitions caused by time alone."""
        if self.now and now < self.now:
            return []
        self.now = now
        transitions = []
        while self.rejected_jumps and now - self.rejected_jumps[0] > MULTI_PERSON_WINDOW:
            self.rejected_jumps.popleft()
        resident = self.resident
        if resident["presence"] == "home" and resident["last_motion"] and self.last_exit_door_use:
            door, used = self.last_exit_door_use
            near_exit = self.exit_sensor_door.get(resident["last_motion_sensor"]) == door
            if (near_exit and abs(used - resident["last_motion"]) <= EXIT_WINDOW
                    and now - max(used, resident["last_motion"]) >= AWAY_AFTER):
                resident.update(presence="away", room=None, since=max(used, resident["last_motion"]))
                self.pending_jump = None
                transitions.append(self._transition(
                    "resident_left", resident["since"], door=self.home["exit_doors"][door]["name"]))
        return transitions

    def _transition(self, kind, stamp, **details):
        return {"type": kind, "timestamp": stamp.isoformat(), **details}

    # ----- current state ----------------------------------------------------

    def snapshot(self):
        now = self.now
        age = lambda stamp: None if not (stamp and now) else max(0.0, (now - stamp).total_seconds())
        iso = lambda stamp: stamp.isoformat() if stamp else None
        resident = self.resident
        rooms = {}
        for room, info in self.rooms.items():
            temperature = self.temperatures.get(room)
            rooms[room] = {
                "name": info["name"],
                "occupied": resident["room"] == room,
                "last_motion": iso(self.room_last_motion[room]),
                "active_motion_sensors": sorted(
                    sensor for sensor, layout in self.layout.items()
                    if layout["room"] == room and layout["sensor_type"] == "motion"
                    and self.sensors[sensor]["value"] == "ON"),
                "temperature": temperature and {
                    "celsius": temperature["value"], "sensor": temperature["sensor"],
                    "timestamp": iso(temperature["timestamp"]),
                    "age_seconds": age(temperature["timestamp"])},
            }
        return {
            "home_id": self.home["home_id"],
            "dataset_time": iso(now),
            "resident": {
                "presence": resident["presence"],
                "room": resident["room"],
                "room_name": self.rooms[resident["room"]]["name"] if resident["room"] else None,
                "since": iso(resident["since"]),
                "last_motion": iso(resident["last_motion"]),
                "last_motion_sensor": resident["last_motion_sensor"],
                "inactive_seconds": age(resident["last_motion"]),
                "location_unconfirmed_room": self.pending_jump[0] if self.pending_jump else None,
            },
            "possible_second_person": len(self.rejected_jumps) >= MULTI_PERSON_JUMPS,
            "rooms": rooms,
            "doors": {sensor: {
                "name": self.home["exit_doors"].get(sensor, {}).get("name", sensor),
                "state": door["state"], "since": iso(door["since"]),
                "open_seconds": age(door["since"]) if door["state"] == "OPEN" else None,
            } for sensor, door in self.doors.items()},
            "sensors": {sensor: {
                "value": record["value"], "timestamp": iso(record["timestamp"]),
                "invalid_readings": record["invalid_readings"],
            } for sensor, record in self.sensors.items()},
            "activity": self.activity,
            "alerts": self.alerts,
            "counters": dict(self.counters),
        }
