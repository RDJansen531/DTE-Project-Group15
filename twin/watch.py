"""Show the twin's current state in the terminal. A stand-in until the dashboard exists.

It only reads aruba/twin/state and aruba/twin/events, exactly as the dashboard will.
"""

import argparse
from collections import deque
import json
import sys
import threading
import time


def duration(seconds):
    if seconds is None:
        return "-"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02}s"
    return f"{seconds // 3600}h {seconds % 3600 // 60:02}m"


def render(state, recent):
    resident = state["resident"]
    lines = [f"ARUBA DIGITAL TWIN      dataset time {(state['dataset_time'] or '-')[:19].replace('T', ' ')}", ""]
    where = resident["room_name"] or ("out of the house" if resident["presence"] == "away" else "unknown")
    lines.append(f"Resident:  {resident['presence'].upper():8} {where}")
    lines.append(f"           last movement {duration(resident['inactive_seconds'])} ago"
                 f" (sensor {resident['last_motion_sensor'] or '-'})")
    if state["possible_second_person"]:
        lines.append("           movement in far-apart rooms: possibly more than one person at home")
    if state.get("activity"):
        lines.append(f"Activity:  {state['activity']}")
    lines += ["", f"{'Room':14}{'Resident':10}{'Motion now':22}{'Temperature':14}"]
    for room in state["rooms"].values():
        temperature = room["temperature"]
        shown = f"{temperature['celsius']:.1f} C" if temperature else "-"
        lines.append(f"{room['name']:14}{'HERE' if room['occupied'] else '':10}"
                     f"{' '.join(room['active_motion_sensors']) or '-':22}{shown:14}")
    lines.append("")
    for door in state["doors"].values():
        extra = f" for {duration(door['open_seconds'])}" if door["state"] == "OPEN" else ""
        lines.append(f"{door['name']:14}{door['state']}{extra}")
    for alert in state.get("alerts") or []:
        lines.append(f"ALERT: {alert}")
    lines += ["", "Latest transitions:"] + [f"  {line}" for line in recent]
    counters = state["counters"]
    lines += ["", f"{counters['events']:,} readings applied, {counters['room_changes']:,} room changes, "
                  f"{counters['invalid']} invalid readings"]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=1883)
    args = parser.parse_args()
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        sys.exit("paho-mqtt is missing. Run: pip install -r requirements.txt")

    latest, recent, lock = {}, deque(maxlen=8), threading.Lock()

    def on_connect(client, userdata, flags, reason_code, properties):
        client.subscribe([("aruba/twin/state", 1), ("aruba/twin/events", 1)])

    def on_message(client, userdata, message):
        payload = json.loads(message.payload)
        with lock:
            if message.topic == "aruba/twin/state":
                latest["state"] = payload
            elif payload["type"] != "sensor_invalid_reading":
                details = ", ".join(f"{k}={v}" for k, v in payload.items() if k not in ("type", "timestamp"))
                recent.append(f"{payload['timestamp'][11:19]}  {payload['type']:18} {details}")

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="aruba-watch")
    client.on_connect, client.on_message = on_connect, on_message
    try:
        client.connect(args.host, args.port, keepalive=60)
    except OSError as error:
        sys.exit(f"Cannot reach the MQTT broker at {args.host}:{args.port} ({error}). Is Mosquitto running?")
    client.loop_start()
    try:
        while True:
            with lock:
                text = render(latest["state"], recent) if latest else "Waiting for the twin to publish its state..."
            sys.stdout.write("\033[2J\033[H" + text + "\n")
            sys.stdout.flush()
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    client.loop_stop()
    client.disconnect()


if __name__ == "__main__":
    main()
