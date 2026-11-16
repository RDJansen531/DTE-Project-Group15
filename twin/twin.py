"""The digital twin service: keeps the home model in step with the sensor streams.

It subscribes to every sensor topic, updates the model and publishes
  aruba/twin/state    the complete current state (retained)
  aruba/twin/events   one message per transition (room entered, door opened, ...)

Start it before replay.py. It sets up the initial situation by itself when a
replay announces its start time.
"""

import argparse
from datetime import datetime, timedelta
import gzip
import json
from pathlib import Path
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from model import HomeModel, load_home  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
STATUS_TOPIC = "aruba/replay/status"
STATE_TOPIC = "aruba/twin/state"
EVENTS_TOPIC = "aruba/twin/events"


def build_initial_model(home, data, split, start):
    """Initial situation at dataset time ``start``.

    Load the snapshot taken at the split start, then apply the recorded readings
    between the split start and ``start`` so every sensor, door and room is current.
    """
    model = HomeModel(home)
    directory = data / split
    model.load_initial_state(json.loads((directory / "initial_state.json").read_text(encoding="utf-8")))
    applied = 0
    if start > model.now:
        with gzip.open(directory / "events.jsonl.gz", "rt", encoding="utf-8") as stream:
            for line in stream:
                event = json.loads(line)
                if datetime.fromisoformat(event["timestamp"]) >= start:
                    break
                model.apply(event)
                applied += 1
    model.tick(start)
    for key in model.counters:      # count only what happens during the replay itself
        model.counters[key] = 0
    return model, applied


class Twin:
    def __init__(self, home, data, publish, clock=time.monotonic, log=print):
        self.home, self.data, self.publish, self.clock, self.log = home, data, publish, clock, log
        self.lock = threading.Lock()
        self.model = HomeModel(home)
        self.speed, self.end = None, None
        self.anchor = None            # (dataset time, wall time) of the latest known moment
        self.dirty = False
        self.last_state_publish = 0.0

    def on_status(self, status):
        with self.lock:
            if status.get("state") == "running":
                start = datetime.fromisoformat(status["dataset_start"])
                self.model, applied = build_initial_model(self.home, self.data, status["split"], start)
                self.speed = float(status["speed"])
                self.end = datetime.fromisoformat(status["dataset_end"])
                self.anchor = (start, self.clock())
                resident = self.model.snapshot()["resident"]
                self.log(f"Replay started at dataset time {start.isoformat()} ({self.speed:g}x). "
                         f"Initial situation built from the snapshot plus {applied:,} earlier readings: "
                         f"resident {resident['presence']}, room {resident['room_name']}.")
                self.dirty = True
            elif self.speed is not None:
                self.log(f"Replay {status.get('state')}: {status.get('published', '?')} readings published, "
                         f"{self.model.counters['events']} applied by the twin.")
                self.speed = None     # stop the clock, keep the last state
                self.dirty = True

    def on_sensor(self, event):
        with self.lock:
            stamp = datetime.fromisoformat(event["timestamp"])
            self.anchor = (stamp, self.clock())
            self._emit(self.model.apply(event))
            self.dirty = True

    def step(self, min_interval=0.2, max_interval=1.0):
        """Called regularly: lets dataset time pass and publishes the state when needed."""
        with self.lock:
            wall = self.clock()
            if self.speed and self.anchor:
                estimate = self.anchor[0] + timedelta(seconds=(wall - self.anchor[1]) * self.speed)
                transitions = self.model.tick(min(estimate, self.end))
                if transitions:
                    self._emit(transitions)
                    self.dirty = True
            since = wall - self.last_state_publish
            if (self.dirty and since >= min_interval) or (self.speed and since >= max_interval):
                self.publish(STATE_TOPIC, self.model.snapshot(), retain=True)
                self.last_state_publish, self.dirty = wall, False

    def _emit(self, transitions):
        for transition in transitions:
            self.publish(EVENTS_TOPIC, transition, retain=False)
            if transition["type"] != "sensor_invalid_reading":
                details = ", ".join(f"{k}={v}" for k, v in transition.items() if k not in ("type", "timestamp"))
                self.log(f"{transition['timestamp'][:19]}  {transition['type']:18} {details}")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", type=Path, default=ROOT / "data" / "aruba")
    parser.add_argument("--home", type=Path, default=ROOT / "config" / "home.json")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--topic-prefix", default="aruba/sensors")
    args = parser.parse_args()

    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        sys.exit("paho-mqtt is missing. Run: pip install -r requirements.txt")

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="aruba-twin")

    def publish(topic, payload, retain):
        client.publish(topic, json.dumps(payload, separators=(",", ":")), qos=1, retain=retain)

    twin = Twin(load_home(args.home), args.data, publish)

    def on_connect(client, userdata, flags, reason_code, properties):
        client.subscribe([(STATUS_TOPIC, 1), (f"{args.topic_prefix}/#", 1)])

    def on_message(client, userdata, message):
        payload = json.loads(message.payload)
        if message.topic == STATUS_TOPIC:
            twin.on_status(payload)
        else:
            twin.on_sensor(payload)

    client.on_connect, client.on_message = on_connect, on_message
    try:
        client.connect(args.host, args.port, keepalive=60)
    except OSError as error:
        sys.exit(f"Cannot reach the MQTT broker at {args.host}:{args.port} ({error}). Is Mosquitto running?")
    client.loop_start()
    print("Twin is listening. Start replay.py in another terminal. Press Ctrl+C to stop.", flush=True)
    try:
        while True:
            twin.step()
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    client.loop_stop()
    client.disconnect()


if __name__ == "__main__":
    main()
