"""Run the home model over a prepared split without a broker and measure its fidelity.

Location is compared with the room each annotated activity normally happens in.
Presence is compared with the time between each Leave_Home and the next Enter_Home.
Use the train or validation split while tuning. Keep the replay split for the final result.
"""

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta
import gzip
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from model import HomeModel, load_home  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
# Where each annotated activity takes place in this home.
ACTIVITY_ROOM = {"Sleeping": "bedroom", "Bed_to_Toilet": "ensuite", "Meal_Preparation": "kitchen",
                 "Wash_Dishes": "kitchen", "Eating": "dining", "Relax": "living",
                 "Work": "office", "Respirate": "office"}
TICK = timedelta(seconds=30)


def expected_room_intervals(annotations):
    return sorted((datetime.fromisoformat(a["start"]), datetime.fromisoformat(a["end"]),
                   ACTIVITY_ROOM[a["activity"]], a["activity"])
                  for a in annotations if a["activity"] in ACTIVITY_ROOM)


def away_intervals(annotations):
    leaves = sorted(datetime.fromisoformat(a["end"]) for a in annotations if a["activity"] == "Leave_Home")
    enters = sorted(datetime.fromisoformat(a["start"]) for a in annotations if a["activity"] == "Enter_Home")
    intervals, index = [], 0
    for left in leaves:
        while index < len(enters) and enters[index] <= left:
            index += 1
        if index < len(enters) and (not intervals or left >= intervals[-1][1]):
            intervals.append((left, enters[index]))
    return intervals


def overlap(start, end, intervals):
    return sum(max(timedelta(0), min(end, right) - max(start, left)).total_seconds()
               for left, right, *_ in intervals if left < end and right > start)


def evaluate(data, split, home_path):
    directory = data / split
    annotations = [json.loads(line) for line in (directory / "annotations.jsonl").open(encoding="utf-8")]
    rooms, away = expected_room_intervals(annotations), away_intervals(annotations)
    model = HomeModel(load_home(home_path))
    model.load_initial_state(json.loads((directory / "initial_state.json").read_text(encoding="utf-8")))
    naive_room = model.resident["room"]

    seconds = defaultdict(float)      # (activity, outcome) -> seconds
    presence = Counter()
    transitions = Counter()
    room_index = 0
    previous = model.now

    def account(start, end):
        nonlocal room_index
        if end <= start:
            return
        while room_index < len(rooms) and rooms[room_index][1] <= start:
            room_index += 1
        for left, right, room, activity in rooms[room_index:room_index + 4]:
            shared = (min(end, right) - max(start, left)).total_seconds()
            if shared > 0:
                seconds[activity, "total"] += shared
                seconds[activity, "model"] += shared * (model.resident["room"] == room)
                seconds[activity, "naive"] += shared * (naive_room == room)
        truly_away = overlap(start, end, away)
        span = (end - start).total_seconds()
        said_away = model.resident["presence"] == "away"
        presence["away_truth"] += truly_away
        presence["away_detected"] += truly_away * said_away
        presence["home_truth"] += span - truly_away
        presence["home_called_away"] += (span - truly_away) * said_away

    with gzip.open(directory / "events.jsonl.gz", "rt", encoding="utf-8") as stream:
        for line in stream:
            event = json.loads(line)
            stamp = datetime.fromisoformat(event["timestamp"])
            while previous + TICK < stamp:      # let time pass between readings
                account(previous, previous + TICK)
                previous += TICK
                transitions.update(t["type"] for t in model.tick(previous))
            account(previous, stamp)
            previous = stamp
            transitions.update(t["type"] for t in model.apply(event))
            if event["sensor_type"] == "motion" and event["value"] == "ON":
                naive_room = model.layout[event["sensor_id"]]["room"]

    activities = sorted({activity for activity, _ in seconds})
    total = sum(seconds[a, "total"] for a in activities)
    return {
        "split": split,
        "location_agreement": {
            "overall_model": sum(seconds[a, "model"] for a in activities) / total,
            "overall_last_motion_baseline": sum(seconds[a, "naive"] for a in activities) / total,
            "annotated_hours": total / 3600,
            "by_activity": {a: {"expected_room": ACTIVITY_ROOM[a],
                                "hours": seconds[a, "total"] / 3600,
                                "model": seconds[a, "model"] / seconds[a, "total"],
                                "last_motion_baseline": seconds[a, "naive"] / seconds[a, "total"]}
                            for a in activities},
        },
        "presence": {
            "annotated_away_hours": presence["away_truth"] / 3600,
            "away_time_detected": presence["away_detected"] / max(presence["away_truth"], 1),
            "home_time_wrongly_away": presence["home_called_away"] / max(presence["home_truth"], 1),
            "annotated_away_episodes": len(away),
        },
        "transitions": dict(sorted(transitions.items())),
        "counters": model.counters,
        "final_state": {"dataset_time": model.snapshot()["dataset_time"],
                        "resident": model.snapshot()["resident"]},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=ROOT / "data" / "aruba")
    parser.add_argument("--home", type=Path, default=ROOT / "config" / "home.json")
    parser.add_argument("--split", default="validation", choices=("train", "validation", "replay"))
    args = parser.parse_args()
    result = evaluate(args.data, args.split, args.home)
    location, presence = result["location_agreement"], result["presence"]
    print(f"Split: {args.split}")
    print(f"\nResident location vs. annotated activities ({location['annotated_hours']:.0f} annotated hours)")
    print(f"  {'activity':18}{'expected room':15}{'hours':>8}{'model':>9}{'baseline':>10}")
    for activity, row in location["by_activity"].items():
        print(f"  {activity:18}{row['expected_room']:15}{row['hours']:8.1f}"
              f"{row['model']:9.1%}{row['last_motion_baseline']:10.1%}")
    print(f"  {'overall':33}{location['annotated_hours']:8.1f}"
          f"{location['overall_model']:9.1%}{location['overall_last_motion_baseline']:10.1%}")
    print("  baseline = room of the most recent motion sensor, without the adjacency rule")
    print(f"\nPresence ({presence['annotated_away_episodes']} annotated trips out, "
          f"{presence['annotated_away_hours']:.0f} hours away)")
    print(f"  away time detected:        {presence['away_time_detected']:.1%}")
    print(f"  home time wrongly 'away':  {presence['home_time_wrongly_away']:.2%}")
    print(f"\nTransitions: {result['transitions']}")
    print(f"Counters: {result['counters']}")


if __name__ == "__main__":
    main()
