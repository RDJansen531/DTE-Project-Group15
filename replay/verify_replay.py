"""Subscribe to the replay and check it against the Step 2 requirements.

Start this before replay.py. It stops by itself when the replay reports that it
finished, then prints PASS or FAIL for each check.
"""

import argparse
from collections import Counter
from datetime import datetime
import json
import sys
import threading
import time


STATUS_TOPIC = "aruba/replay/status"


def check(messages, status, prefix, tolerance_ms):
    """messages: list of (arrival_seconds, topic, event). Returns [(name, ok, detail)]."""
    results = []
    add = lambda name, ok, detail: results.append((name, bool(ok), detail))
    add("messages received", messages, f"{len(messages):,}")
    if not messages:
        return results

    if status and "published" in status:
        add("every published reading arrived", len(messages) == status["published"],
            f"received {len(messages):,}, replay published {status['published']:,}")

    wrong_topic = [topic for _, topic, event in messages if topic != f"{prefix}/{event['sensor_id']}"]
    add("each reading is on its own sensor's topic", not wrong_topic,
        f"{len({topic for _, topic, _ in messages})} topics, {len(wrong_topic)} misplaced")

    ids = Counter(event["event_id"] for _, _, event in messages)
    duplicates = sum(count - 1 for count in ids.values())
    add("one message per reading (no duplicates)", duplicates == 0, f"{duplicates} duplicates")

    stamps = [datetime.fromisoformat(event["timestamp"]) for _, _, event in messages]
    backwards = sum(later < earlier for earlier, later in zip(stamps, stamps[1:]))
    add("dataset timestamps never go backwards", backwards == 0, f"{backwards} reversals")

    speed = (status or {}).get("speed")
    if speed and status.get("dataset_start"):
        # Anchor on the first arrival, then compare every later arrival with the formula.
        t_zero = datetime.fromisoformat(status["dataset_start"])
        expected = [(stamp - t_zero).total_seconds() / speed for stamp in stamps]
        offset = messages[0][0] - expected[0]
        errors = [abs(arrival - offset - due) * 1000 for (arrival, _, _), due in zip(messages, expected)]
        add(f"timing follows t0 + (T - T0) / speed within {tolerance_ms:g} ms",
            max(errors) <= tolerance_ms,
            f"speed {speed:g}x, worst error {max(errors):.1f} ms, mean {sum(errors) / len(errors):.2f} ms")
    else:
        add("timing check", False, "no replay status seen, so speed is unknown")
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--topic-prefix", default="aruba/sensors")
    parser.add_argument("--tolerance-ms", type=float, default=50.0)
    parser.add_argument("--timeout", type=float, default=3600.0, help="give up after this many seconds")
    args = parser.parse_args()

    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        sys.exit("paho-mqtt is missing. Run: pip install -r requirements.txt")

    messages, status, done, started = [], {}, threading.Event(), threading.Event()

    def on_connect(client, userdata, flags, reason_code, properties):
        client.subscribe([(f"{args.topic_prefix}/#", 1), (STATUS_TOPIC, 1)])

    def on_message(client, userdata, message):
        arrival = time.monotonic()
        payload = json.loads(message.payload)
        if message.topic == STATUS_TOPIC:
            if payload.get("state") == "running":
                # A new run begins: forget anything left from an earlier one.
                messages.clear()
                status.clear()
                status.update(payload)
                started.set()
                print(f"Replay started: {payload}", flush=True)
            elif started.is_set():
                status.update(payload)
                done.set()
            return
        messages.append((arrival, message.topic, payload))

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="aruba-verify")
    client.on_connect, client.on_message = on_connect, on_message
    try:
        client.connect(args.host, args.port, keepalive=60)
    except OSError as error:
        sys.exit(f"Cannot reach the MQTT broker at {args.host}:{args.port} ({error}). Is Mosquitto running?")
    client.loop_start()
    print("Listening. Now start replay.py in another terminal.", flush=True)
    try:
        done.wait(args.timeout)
        time.sleep(0.5)  # let the last in-flight readings arrive
    except KeyboardInterrupt:
        pass
    client.loop_stop()
    client.disconnect()

    results = check(messages, status, args.topic_prefix, args.tolerance_ms)
    print()
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")
    by_type = Counter(event["sensor_type"] for _, _, event in messages)
    print(f"\nBy sensor type: {dict(sorted(by_type.items()))}")
    sys.exit(0 if all(ok for _, ok, _ in results) else 1)


if __name__ == "__main__":
    main()
