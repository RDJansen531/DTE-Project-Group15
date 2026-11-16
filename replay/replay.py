"""Replay prepared Aruba sensor events to an MQTT broker at a configurable speed.

Every physical sensor publishes to its own topic (``aruba/sensors/<sensor_id>``)
and every reading is its own message. A reading with dataset timestamp ``T`` is
published at wall-clock time ``t0 + (T - T0) / speed``.
"""

import argparse
from collections import Counter
from datetime import datetime
import gzip
import json
from pathlib import Path
import signal
import statistics
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
STATUS_TOPIC = "aruba/replay/status"


def read_events(path, start, end):
    """Yield (timestamp, event) for start <= timestamp < end, in file order."""
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        for line in stream:
            event = json.loads(line)
            timestamp = datetime.fromisoformat(event["timestamp"])
            if timestamp < start:
                continue
            if timestamp >= end:
                break
            yield timestamp, event


def schedule(events, t_zero, speed, wall_zero):
    """Yield (deadline, event). Deadlines are absolute, so errors never accumulate."""
    for timestamp, event in events:
        yield wall_zero + (timestamp - t_zero).total_seconds() / speed, event


def run(events, publish, t_zero, speed, clock=time.monotonic, sleep=time.sleep,
        report_every=5.0, stop=lambda: False):
    """Publish each event at its deadline. Returns counts and lateness statistics."""
    wall_zero = clock()
    lateness, by_type, topics = [], Counter(), set()
    next_report, last_timestamp = wall_zero + report_every, None
    for deadline, event in schedule(events, t_zero, speed, wall_zero):
        if stop():
            break
        wait = deadline - clock()
        while wait > 0:
            # Sleep in short slices so Ctrl+C and progress reports stay responsive.
            sleep(min(wait, 0.25))
            if stop():
                break
            now = clock()
            if now >= next_report:
                print(f"  waiting, dataset time {last_timestamp or t_zero.isoformat()}, "
                      f"{len(lateness):,} published", flush=True)
                next_report = now + report_every
            wait = deadline - now
        if stop():
            break
        topics.add(publish(event))
        now = clock()
        lateness.append(now - deadline)
        by_type[event["sensor_type"]] += 1
        last_timestamp = event["timestamp"]
        if now >= next_report:
            print(f"  dataset time {last_timestamp}, {len(lateness):,} published, "
                  f"currently {lateness[-1] * 1000:.1f} ms late", flush=True)
            next_report = now + report_every
    return {
        "published": len(lateness),
        "by_sensor_type": dict(sorted(by_type.items())),
        "distinct_topics": len(topics),
        "last_dataset_timestamp": last_timestamp,
        "wall_seconds": clock() - wall_zero,
        "lateness_ms": summarize(lateness),
    }


def summarize(lateness):
    if not lateness:
        return None
    ordered = sorted(lateness)
    pick = lambda fraction: ordered[min(len(ordered) - 1, int(fraction * len(ordered)))] * 1000
    return {"mean": statistics.fmean(ordered) * 1000, "p50": pick(0.50),
            "p99": pick(0.99), "max": ordered[-1] * 1000}


def connect(host, port, client_id):
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        sys.exit("paho-mqtt is missing. Run: pip install -r requirements.txt")
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    try:
        client.connect(host, port, keepalive=60)
    except OSError as error:
        sys.exit(f"Cannot reach the MQTT broker at {host}:{port} ({error}). Is Mosquitto running?")
    client.loop_start()
    return client


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", type=Path, default=ROOT / "data" / "aruba",
                        help="prepared dataset directory")
    parser.add_argument("--split", default="replay", choices=("train", "validation", "replay"))
    parser.add_argument("--start", type=datetime.fromisoformat,
                        help="dataset time to start at, e.g. 2011-05-01T07:00:00 (default: split start)")
    parser.add_argument("--end", type=datetime.fromisoformat,
                        help="dataset time to stop before (default: split end)")
    parser.add_argument("--speed", type=float, default=60.0,
                        help="replay multiplier: 1 is real time, 60 plays one hour per minute")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--topic-prefix", default="aruba/sensors")
    parser.add_argument("--qos", type=int, default=1, choices=(0, 1))
    parser.add_argument("--max-events", type=int, help="stop after this many events (for quick tests)")
    parser.add_argument("--dry-run", action="store_true",
                        help="keep the timing but print instead of publishing (no broker needed)")
    args = parser.parse_args()
    if args.speed <= 0:
        parser.error("--speed must be positive")

    manifest = json.loads((args.data / "manifest.json").read_text(encoding="utf-8"))
    split = manifest["splits"][args.split]
    split_start = datetime.fromisoformat(split["start_inclusive"])
    split_end = datetime.fromisoformat(split["end_exclusive"])
    start, end = args.start or split_start, args.end or split_end
    if not split_start <= start < end <= split_end:
        parser.error(f"--start/--end must lie inside the {args.split} split "
                     f"({split_start.isoformat()} to {split_end.isoformat()})")

    events = read_events(args.data / args.split / "events.jsonl.gz", start, end)
    if args.max_events:
        events = (pair for pair, _ in zip(events, range(args.max_events)))

    stopping = []
    signal.signal(signal.SIGINT, lambda *_: stopping.append(True))
    status = {"split": args.split, "dataset_start": start.isoformat(),
              "dataset_end": end.isoformat(), "speed": args.speed}

    if args.dry_run:
        client = None
        def publish(event):
            topic = f"{args.topic_prefix}/{event['sensor_id']}"
            print(f"{topic} {json.dumps(event, separators=(',', ':'))}")
            return topic
    else:
        client = connect(args.host, args.port, "aruba-replay")
        # Retained, so consumers that join late still learn the replay's time mapping.
        client.publish(STATUS_TOPIC, json.dumps({**status, "state": "running"}), qos=1, retain=True)
        def publish(event):
            topic = f"{args.topic_prefix}/{event['sensor_id']}"
            client.publish(topic, json.dumps(event, separators=(",", ":")), qos=args.qos)
            return topic

    print(f"Replaying {args.split} from {start.isoformat()} to {end.isoformat()} at {args.speed:g}x"
          f"{' (dry run)' if args.dry_run else f' to {args.host}:{args.port}'}", flush=True)
    result = run(events, publish, start, args.speed, stop=lambda: bool(stopping))
    state = "interrupted" if stopping else "finished"
    if client:
        final = client.publish(STATUS_TOPIC, json.dumps({**status, "state": state, **result}),
                               qos=1, retain=True)
        final.wait_for_publish(timeout=30)
        client.loop_stop()
        client.disconnect()
    print(f"Replay {state}.")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
