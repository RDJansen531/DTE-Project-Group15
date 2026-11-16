"""Tests for the home model. Run: python -m unittest discover tests"""

from datetime import datetime, timedelta
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "twin"))
from model import HomeModel, load_home  # noqa: E402

HOME = load_home(ROOT / "config" / "home.json")
T0 = datetime(2011, 5, 1, 8, 0, 0)


def reading(sensor, value, seconds):
    return {"sensor_id": sensor, "value": value, "raw_value": str(value),
            "timestamp": (T0 + timedelta(seconds=seconds)).isoformat()}


def kinds(transitions):
    return [t["type"] for t in transitions]


class Layout(unittest.TestCase):
    def test_all_39_sensors_have_a_known_room(self):
        self.assertEqual(len(HOME["sensors"]), 39)
        for sensor in HOME["sensors"].values():
            self.assertIn(sensor["room"], HOME["rooms"])

    def test_adjacency_is_symmetric(self):
        for room, info in HOME["rooms"].items():
            for neighbour in info["adjacent"]:
                self.assertIn(room, HOME["rooms"][neighbour]["adjacent"])


class Location(unittest.TestCase):
    def setUp(self):
        self.model = HomeModel(HOME)

    def test_motion_places_the_resident_in_the_sensor_room(self):
        transitions = self.model.apply(reading("M019", "ON", 0))
        self.assertEqual(kinds(transitions), ["room_entered"])
        self.assertEqual(self.model.snapshot()["resident"]["room"], "kitchen")
        self.assertTrue(self.model.snapshot()["rooms"]["kitchen"]["occupied"])

    def test_motion_off_does_not_move_the_resident(self):
        self.model.apply(reading("M019", "ON", 0))
        self.assertEqual(self.model.apply(reading("M003", "OFF", 5)), [])
        self.assertEqual(self.model.resident["room"], "kitchen")

    def test_moving_to_an_adjacent_room(self):
        self.model.apply(reading("M019", "ON", 0))
        self.model.apply(reading("M014", "ON", 10))
        self.assertEqual(self.model.resident["room"], "dining")

    def test_single_motion_in_a_far_room_is_not_trusted(self):
        self.model.apply(reading("M019", "ON", 0))          # kitchen
        self.assertEqual(self.model.apply(reading("M003", "ON", 5)), [])   # bedroom, not adjacent
        snapshot = self.model.snapshot()["resident"]
        self.assertEqual(snapshot["room"], "kitchen")
        self.assertEqual(snapshot["location_unconfirmed_room"], "bedroom")

    def test_second_motion_confirms_the_far_room(self):
        self.model.apply(reading("M019", "ON", 0))
        self.model.apply(reading("M003", "ON", 5))
        self.model.apply(reading("M002", "ON", 20))
        self.assertEqual(self.model.resident["room"], "bedroom")

    def test_repeated_far_motions_suggest_a_second_person(self):
        self.model.apply(reading("M019", "ON", 0))
        for index, sensor in enumerate(("M003", "M026", "M003", "M026")):
            self.model.apply(reading(sensor, "ON", 5 + index * 90))
            self.model.apply(reading("M019", "ON", 50 + index * 90))
        self.assertTrue(self.model.snapshot()["possible_second_person"])

    def test_toilet_visit_from_bed_and_back(self):
        self.model.apply(reading("M003", "ON", 0))          # in bed
        self.model.apply(reading("M004", "ON", 20))         # doorway of the en-suite toilet
        self.assertEqual(self.model.resident["room"], "ensuite")
        self.model.apply(reading("M007", "ON", 200))
        self.assertEqual(self.model.resident["room"], "bedroom")

    def test_inactivity_grows_with_time(self):
        self.model.apply(reading("M009", "ON", 0))
        self.model.tick(T0 + timedelta(minutes=30))
        self.assertEqual(self.model.snapshot()["resident"]["inactive_seconds"], 1800)


class DoorsTemperaturesQuality(unittest.TestCase):
    def setUp(self):
        self.model = HomeModel(HOME)

    def test_door_transitions_and_open_duration(self):
        self.assertEqual(kinds(self.model.apply(reading("D001", "OPEN", 0))), ["door_opened"])
        self.model.tick(T0 + timedelta(seconds=90))
        door = self.model.snapshot()["doors"]["D001"]
        self.assertEqual((door["name"], door["state"], door["open_seconds"]), ("Front door", "OPEN", 90))
        self.assertEqual(kinds(self.model.apply(reading("D001", "CLOSE", 120))), ["door_closed"])

    def test_temperature_updates_its_room_and_ages(self):
        self.model.apply(reading("T003", 22.5, 0))
        self.model.tick(T0 + timedelta(minutes=10))
        temperature = self.model.snapshot()["rooms"]["kitchen"]["temperature"]
        self.assertEqual((temperature["celsius"], temperature["age_seconds"]), (22.5, 600))

    def test_invalid_reading_keeps_the_last_valid_value(self):
        self.model.apply(reading("T003", 22.5, 0))
        transitions = self.model.apply(reading("T003", None, 60))
        self.assertEqual(kinds(transitions), ["sensor_invalid_reading"])
        snapshot = self.model.snapshot()
        self.assertEqual(snapshot["rooms"]["kitchen"]["temperature"]["celsius"], 22.5)
        self.assertEqual(snapshot["sensors"]["T003"]["invalid_readings"], 1)


class Presence(unittest.TestCase):
    def leave(self):
        model = HomeModel(HOME)
        model.apply(reading("M022", "ON", 0))
        model.apply(reading("M030", "ON", 10))      # at the garage door
        model.apply(reading("D004", "OPEN", 12))
        model.apply(reading("D004", "CLOSE", 20))
        return model

    def test_resident_leaves_after_silence_at_an_exit_door(self):
        model = self.leave()
        self.assertEqual(model.tick(T0 + timedelta(minutes=2)), [])
        self.assertEqual(kinds(model.tick(T0 + timedelta(minutes=6))), ["resident_left"])
        snapshot = model.snapshot()["resident"]
        self.assertEqual((snapshot["presence"], snapshot["room"]), ("away", None))

    def test_resident_returns_on_the_next_motion(self):
        model = self.leave()
        model.tick(T0 + timedelta(minutes=6))
        transitions = model.apply(reading("M030", "ON", 3600))
        self.assertEqual(kinds(transitions), ["resident_returned", "room_entered"])
        self.assertEqual(model.resident["presence"], "home")

    def test_opening_a_door_without_leaving_is_not_away(self):
        model = HomeModel(HOME)
        model.apply(reading("M030", "ON", 0))
        model.apply(reading("D004", "OPEN", 5))
        model.apply(reading("M022", "ON", 30))      # walks back in
        self.assertEqual(model.tick(T0 + timedelta(minutes=30)), [])
        self.assertEqual(model.resident["presence"], "home")


class InitialSituation(unittest.TestCase):
    def test_snapshot_sets_doors_temperatures_and_location_without_transitions(self):
        model = HomeModel(HOME)
        obs = lambda sensor, value, seconds: {"sensor_id": sensor, "value": value,
                                              "timestamp": (T0 - timedelta(seconds=seconds)).isoformat()}
        sensors = {sensor: {"last_observation": None, "last_valid_observation": None, "last_motion_on": None}
                   for sensor in HOME["sensors"]}
        sensors["D001"]["last_valid_observation"] = obs("D001", "CLOSE", 5000)
        sensors["T001"]["last_valid_observation"] = obs("T001", 21.0, 900)
        sensors["M003"]["last_valid_observation"] = obs("M003", "OFF", 290)
        sensors["M003"]["last_motion_on"] = (T0 - timedelta(seconds=300)).isoformat()
        sensors["M019"]["last_motion_on"] = (T0 - timedelta(seconds=7200)).isoformat()
        model.load_initial_state({"as_of": T0.isoformat(), "sensors": sensors})
        snapshot = model.snapshot()
        self.assertEqual(snapshot["resident"]["room"], "bedroom")
        self.assertEqual(snapshot["resident"]["inactive_seconds"], 300)
        self.assertEqual(snapshot["doors"]["D001"]["state"], "CLOSE")
        self.assertEqual(snapshot["doors"]["D002"]["state"], "unknown")
        self.assertEqual(snapshot["rooms"]["bedroom"]["temperature"]["celsius"], 21.0)
        self.assertEqual(snapshot["counters"]["events"], 0)


if __name__ == "__main__":
    unittest.main()
