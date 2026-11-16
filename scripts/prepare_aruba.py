"""Prepare timestamped Aruba recordings using only the Python standard library."""

import argparse
from collections import Counter, deque
from contextlib import ExitStack
from datetime import datetime, timedelta
import gzip
import hashlib
import itertools
import json
import math
from pathlib import Path
import shutil
import tempfile
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[1]
SENSORS = {
    **{f"M{i:03}": "motion" for i in range(1, 32)},
    **{sensor: "door" for sensor in ("D001", "D002", "D004")},
    **{f"T{i:03}": "temperature" for i in range(1, 6)},
}
SPLITS = {
    "train": ("2010-11-04T00:00:00", "2011-04-01T00:00:00"),
    "validation": ("2011-04-01T00:00:00", "2011-05-01T00:00:00"),
    "replay": ("2011-05-01T00:00:00", "2011-06-12T00:00:00"),
}


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def write_row(handle, value):
    handle.write(json.dumps(value, separators=(",", ":")) + "\n")


def digest(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def normalize(sensor, raw):
    """Keep uncertain observations with a null value and an explicit reason."""
    kind = SENSORS[sensor]
    if kind == "motion":
        if raw in {"ON", "OFF"}:
            return raw, "valid", None
        return None, "invalid", "unrecognized_motion_value"
    if kind == "door":
        if raw == "CLOSED":
            return "CLOSE", "normalized", "CLOSED_to_CLOSE"
        if raw in {"OPEN", "CLOSE"}:
            return raw, "valid", None
        return None, "invalid", "unrecognized_door_value"
    try:
        value = float(raw)
    except ValueError:
        return None, "invalid", "non_numeric_temperature"
    # Dataset cleaning assumptions, not clinical or alert thresholds.
    if not math.isfinite(value) or not -20 <= value <= 60:
        return None, "invalid", "temperature_outside_review_range"
    if not (value * 2).is_integer():
        return None, "invalid", "temperature_off_observed_half_degree_grid"
    return value, "valid", None


def recording_rows(raw_path, audit, counts):
    """Remove the verified repeated block while retaining original line numbers."""
    with raw_path.open(encoding="utf-8") as source:
        rows = enumerate(source, start=1)
        for date, group in itertools.groupby(rows, key=lambda row: row[1].split()[0]):
            day = list(group)
            counts["raw_rows"] += len(day)
            if date == "2011-05-23":
                tokens = [line.split() for _, line in day]
                half = len(day) // 2
                if len(day) != 12850 or tokens[:half] != tokens[half:]:
                    raise ValueError("May 23 differs from the verified repeated block. Review the input.")
                write_row(audit, {
                    "action": "remove_duplicate_block",
                    "date": date,
                    "first_removed_source_line": day[half][0],
                    "last_removed_source_line": day[-1][0],
                    "rows": half,
                })
                counts["duplicate_rows_removed"] += half
                day = day[:half]
            yield from day


def write_annotations(directory, episodes):
    """Preserve concurrent activities and clip labels at each split boundary."""
    summary = {}
    for split, (start, end) in SPLITS.items():
        start_dt, end_dt = datetime.fromisoformat(start), datetime.fromisoformat(end)
        activity_counts = Counter()
        with (directory / split / "annotations.jsonl").open("w", encoding="utf-8") as handle:
            for episode in episodes:
                left = datetime.fromisoformat(episode["start"])
                right = datetime.fromisoformat(episode["end"])
                if left >= end_dt or right < start_dt:
                    continue
                clipped = {
                    "episode_id": episode["episode_id"],
                    "activity": episode["activity"],
                    "start": max(left, start_dt).isoformat(),
                    "end": min(right, end_dt).isoformat(),
                    "start_clipped": left < start_dt,
                    "end_clipped": right >= end_dt,
                    "end_inclusive": right < end_dt,
                }
                write_row(handle, clipped)
                activity_counts[episode["activity"]] += 1
        summary[split] = dict(sorted(activity_counts.items()))
    with (directory / "annotations.jsonl").open("w", encoding="utf-8") as handle:
        for episode in episodes:
            write_row(handle, episode)
    return summary


def summarize_annotations(episodes, first, last):
    merged = []
    overlap_count = 0
    for episode in sorted(episodes, key=lambda item: item["start"]):
        start = datetime.fromisoformat(episode["start"])
        end = datetime.fromisoformat(episode["end"])
        if merged and start < merged[-1][1]:
            overlap_count += 1
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    seconds = sum((end - start).total_seconds() for start, end in merged)
    return {
        "episodes": len(episodes),
        "activity_counts": dict(sorted(Counter(e["activity"] for e in episodes).items())),
        "episodes_overlapping_an_earlier_interval": overlap_count,
        "elapsed_time_coverage_fraction": seconds / (last - first).total_seconds(),
    }


def prepare(source, directory):
    counts = Counter()
    by_sensor = Counter()
    quality = Counter()
    split_counts = {name: Counter() for name in SPLITS}
    split_bounds = {}
    active = {}
    episodes = []
    history = deque()
    initialized = set()
    states = {sensor: {"last_observation": None, "last_valid_observation": None,
                       "last_motion_on": None} for sensor in SENSORS}
    first = previous = None
    raw_path = directory / "raw" / "aruba.txt"
    raw_path.parent.mkdir()
    with ZipFile(source) as archive, archive.open("aruba.txt") as data, raw_path.open("wb") as raw:
        shutil.copyfileobj(data, raw)
    for split in SPLITS:
        (directory / split).mkdir()

    with ExitStack() as stack:
        audit = stack.enter_context(gzip.open(directory / "cleaning_audit.jsonl.gz", "wt", encoding="utf-8"))
        outputs = {
            split: stack.enter_context(gzip.open(directory / split / "events.jsonl.gz", "wt", encoding="utf-8"))
            for split in SPLITS
        }
        for line_number, line in recording_rows(raw_path, audit, counts):
            fields = line.split()
            if len(fields) not in (4, 6):
                raise ValueError(f"Unexpected fields at source line {line_number}")
            date, time, sensor, raw, *annotation = fields
            timestamp = datetime.fromisoformat(date + "T" + time)
            if previous and timestamp < previous:
                raise ValueError(f"Timestamp goes backwards at source line {line_number}")
            first = first or timestamp
            previous = timestamp
            if annotation:
                activity, boundary = annotation
                if boundary == "begin":
                    if activity in active:
                        raise ValueError(f"Unmatched begin for {activity} at {line_number}")
                    active[activity] = (timestamp, line_number)
                elif boundary == "end":
                    start, start_line = active.pop(activity)
                    episodes.append({
                        "episode_id": f"aruba:activity:{start_line}", "activity": activity,
                        "start": start.isoformat(), "end": timestamp.isoformat(),
                        "begin_source_line": start_line, "end_source_line": line_number,
                    })
                else:
                    raise ValueError(f"Unknown activity boundary at {line_number}")
            if sensor not in SENSORS:
                counts["non_sensor_rows_excluded"] += 1
                write_row(audit, {"action": "exclude_non_sensor", "source_line": line_number,
                                  "raw_line": line.rstrip()})
                continue

            split = next(name for name, (start, end) in SPLITS.items()
                         if datetime.fromisoformat(start) <= timestamp < datetime.fromisoformat(end))
            if split not in initialized:
                split_start = datetime.fromisoformat(SPLITS[split][0])
                write_json(directory / split / "initial_state.json", {
                    "home_id": "aruba", "as_of": split_start.isoformat(),
                    "meaning": "Observations strictly before the split start",
                    "sensors": states,
                })
                with gzip.open(directory / split / "context.jsonl.gz", "wt", encoding="utf-8") as context:
                    for when, event in history:
                        if split_start - timedelta(minutes=30) <= when < split_start:
                            write_row(context, event)
                initialized.add(split)
            value, status, reason = normalize(sensor, raw)
            event = {
                "event_id": f"aruba:{line_number}", "home_id": "aruba",
                "timestamp": timestamp.isoformat(), "sensor_id": sensor,
                "sensor_type": SENSORS[sensor], "value": value,
                "raw_value": raw, "quality": status, "quality_reason": reason,
                "source_line": line_number,
            }
            write_row(outputs[split], event)
            counts["physical_sensor_rows"] += 1
            quality[status] += 1
            by_sensor[sensor] += 1
            split_counts[split][status] += 1
            split_bounds.setdefault(split, {"first": event["timestamp"]})["last"] = event["timestamp"]
            if status != "valid":
                write_row(audit, {"action": status, **event})
            state = states[sensor]
            state["last_observation"] = event
            if value is not None:
                state["last_valid_observation"] = event
            if SENSORS[sensor] == "motion" and value == "ON":
                state["last_motion_on"] = event["timestamp"]
            history.append((timestamp, event))
            while history and history[0][0] < timestamp - timedelta(minutes=30):
                history.popleft()

    if active or initialized != set(SPLITS):
        raise ValueError("Unclosed activity intervals or missing split data")
    if counts["raw_rows"] != sum(counts[key] for key in (
            "physical_sensor_rows", "duplicate_rows_removed", "non_sensor_rows_excluded")):
        raise ValueError("Input/output row accounting failed")
    if set(by_sensor) != set(SENSORS):
        raise ValueError("Unexpected physical sensor inventory")
    episodes.sort(key=lambda item: (item["start"], item["begin_source_line"]))
    annotation_counts = write_annotations(directory, episodes)
    write_json(directory / "sensors.json", [
        {"home_id": "aruba", "sensor_id": sensor, "sensor_type": kind,
         "room": None, "recording_mode": "event" if kind != "temperature" else "irregular_observations"}
        for sensor, kind in sorted(SENSORS.items())
    ])
    files = {str(path.relative_to(directory)).replace("\\", "/"): {
        "bytes": path.stat().st_size, "sha256": digest(path),
    } for path in sorted(directory.rglob("*")) if path.is_file()}
    manifest = {
        "schema_version": 1,
        "home_id": "aruba",
        "source": {"archive": source.name, "sha256": digest(source), "member": "aruba.txt",
                   "url": "https://zenodo.org/records/17180309"},
        "preparation_script_sha256": digest(Path(__file__)),
        "timestamp_convention": "Source local timestamps with no timezone attached or conversion applied",
        "counts": dict(counts), "quality_counts": dict(quality),
        "sensor_counts": dict(sorted(by_sensor.items())),
        "annotations": summarize_annotations(episodes, first, previous),
        "splits": {name: {
            "start_inclusive": start, "end_exclusive": end,
            "event_count": sum(split_counts[name].values()),
            "quality_counts": dict(split_counts[name]), **split_bounds[name],
            "annotation_counts": annotation_counts[name],
        } for name, (start, end) in SPLITS.items()},
        "cleaning_policy": {
            "duplicates": "Remove the second verified 6425-row block on 2011-05-23",
            "unknown_sensor_ids": "Exclude and audit",
            "door_alias": "CLOSED becomes CLOSE",
            "invalid_values": "Retain event with null value and quality=invalid",
            "temperature_review_range": [-20, 60],
            "temperature_resolution": 0.5,
            "temperature_policy_basis": "Chosen review rules based on the dominant recorded values",
        },
        "files": files,
    }
    write_json(directory / "manifest.json", manifest)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT.parent / "new_labeled_data.zip")
    parser.add_argument("--output", type=Path, default=ROOT / "data" / "aruba")
    args = parser.parse_args()
    source, output = args.source.resolve(), args.output.resolve()
    if output.exists():
        parser.error(f"Output already exists. Choose a fresh --output directory: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="aruba-preparation-", dir=output.parent) as temporary:
        staging = Path(temporary) / "aruba"
        staging.mkdir()
        manifest = prepare(source, staging)
        staging.rename(output)
    print(f"Prepared {manifest['counts']['physical_sensor_rows']:,} sensor events in {output}")
    for name, split in manifest["splits"].items():
        print(f"  {name}: {split['event_count']:,} events")
    print(f"Quality counts: {manifest['quality_counts']}")


if __name__ == "__main__":
    main()
