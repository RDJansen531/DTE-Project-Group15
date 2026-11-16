# DTE-Project-Group15

Smart-home digital twin using the CASAS Aruba recording.

## Prepared dataset

The local dataset is in `data/aruba/`. It contains only Aruba. The source archive
is the CASAS release at <https://zenodo.org/records/17180309>.

| Split | Inclusive start | Exclusive end | Sensor events | Purpose |
| --- | --- | --- | ---: | --- |
| `train` | 2010-11-04 00:00 | 2011-04-01 00:00 | 1,163,177 | Fit models and preprocessing |
| `validation` | 2011-04-01 00:00 | 2011-05-01 00:00 | 233,153 | Choose features, thresholds and model settings |
| `replay` | 2011-05-01 00:00 | 2011-06-12 00:00 | 316,798 | Held-out replay and final evaluation |

The recording ends on June 11. These boundaries reserve April for validation
within the pitch's November–April development period. After selecting settings,
you may refit on the combined training and validation periods before replay.
Keep May–June held out from fitting, feature selection and threshold tuning.

Across the splits, 1,713,128 observations remain. Of these, 1,712,960 have valid
original values, one has a normalized value, and 167 retain a flagged raw value
with a null cleaned value. The annotations contain 6,440 episodes.

All timestamps retain the source's local date/time, including fractional seconds.
The source rows carry no timezone information. Their exact timezone remains
unverified. Do not append `Z` or convert them using your computer's timezone.

### Files

```text
data/aruba/
  raw/aruba.txt                 Original Aruba member, including its known issues
  sensors.json                 39 physical sensor IDs and types
  manifest.json                Counts, dates, cleaning policy and SHA-256 hashes
  cleaning_audit.jsonl.gz       Removed block, excluded rows and flagged values
  annotations.jsonl            Complete cleaned activity intervals for evaluation
  train/
    events.jsonl.gz             Timestamp-ordered sensor events
    annotations.jsonl          Activity intervals clipped to this split
    initial_state.json         Latest observations before the split starts
    context.jsonl.gz           Previous 30 minutes of sensor events
  validation/                  Same four files
  replay/                      Same four files
```

Training context is empty because the dataset starts in that split. Validation
and replay context intentionally contain earlier sensor events to warm up causal
features. Treat them as initialization data and score only events inside the
selected split. Models with longer windows need additional preceding history.

Generated recordings are ignored by Git to keep the repository small. The
preparation script and this documentation are tracked. Anyone cloning the project
must obtain the source archive and run preparation locally.

## Reproduce

Use Python 3.11 or newer. The preparation script uses the standard library.
From the repository root, with the original ZIP in the parent directory, run

```sh
python scripts/prepare_aruba.py
```

To use a different archive location or produce another copy, run

```sh
python scripts/prepare_aruba.py --source /path/to/new_labeled_data.zip --output /path/to/aruba-prepared
```

The output directory must be new. Preparation builds in a temporary directory
and publishes the result after its consistency checks pass. The source ZIP's
SHA-256 and the preparation script's SHA-256 are recorded in the manifest.

## Cleaning decisions

- Remove the second occurrence of the identical 6,425-row block on May 23, 2011.
  Preparation verifies the repeated block before removing it.
- Exclude four `LEAVEHOME` and one `ENTERHOME` rows from the sensor streams.
  Their original contents and source line numbers appear in the cleaning audit.
- Normalize the door spelling `CLOSED` to `CLOSE`.
- Preserve malformed observations with `value: null`, `quality: "invalid"`,
  their raw values and a reason. Examples include motion `ONc`, door `OPENc`
  and temperature `28.55c`.
- Flag temperatures outside the chosen review range of −20 to 60, and values
  outside the observed half-degree grid. This catches entries such as `285`
  and `19.55`. These are explicit data-review assumptions. The raw values remain
  available for revisiting them. These thresholds are independent of alert rules.
- Preserve all activity labels, their boundaries and overlaps after duplicate
  removal. Missing annotations remain unknown. Rare classes retain their labels.

The physical inventory is 31 motion sensors (`M001`–`M031`), three door contacts
(`D001`, `D002`, `D004`) and five temperature sensors (`T001`–`T005`). Room mappings
are currently `null`. Populate them from the CASAS floor plan before implementing
room occupancy. The floor plans are available with the companion release at
<https://zenodo.org/records/15708568>.

## Reading events

Each `.jsonl.gz` file is gzip-compressed JSON Lines. Every line is one observation.
Read a stream with

```python
import gzip
import json

with gzip.open("data/aruba/replay/events.jsonl.gz", "rt", encoding="utf-8") as stream:
    for line in stream:
        event = json.loads(line)
        # Pass this event to the replay scheduler or model pipeline.
```

Each event contains these fields.

| Field | Meaning |
| --- | --- |
| `event_id` | Stable `aruba:<original source line number>` identifier |
| `home_id` | `aruba` |
| `timestamp` | Original local timestamp in ISO format |
| `sensor_id`, `sensor_type` | Physical device and motion/door/temperature type |
| `value` | Validated reading, or `null` for an invalid observation |
| `raw_value` | Original reading token |
| `quality` | `valid`, `normalized` or `invalid` |
| `quality_reason` | Reason for normalization or rejection, otherwise `null` |
| `source_line` | One-based line in `raw/aruba.txt` |

Use `value` for state updates and ML input. An invalid observation can update
sensor-quality information, while the last valid value continues to represent
the observed sensor state. Track its timestamp so the dashboard can show its age.
Do not use source line numbers or event IDs as predictive features.

Motion and door readings are event-based. Temperature observations have variable
intervals in this recording. Replay all families at their recorded timestamps.

## Replay initialization and timing

Load `initial_state.json` to initialize each sensor from observations strictly
before the split start. Each sensor includes its last observation, last valid
observation, and, for motion sensors, its last `ON` timestamp. A null observation
means the earlier state is unknown. This snapshot contains sensor evidence only.

Use `context.jsonl.gz` to initialize rolling feature buffers before inference.
It contains no activity annotations. Do not apply older context events over the
already initialized sensor snapshot. Initialize model features separately, or
rebuild state chronologically from earlier history.

For replay, publish every event on its own sensor topic, such as
`aruba/sensors/M003`. With dataset start `T0` from the split boundary, wall-clock
start `t0`, event timestamp `T`, and positive replay multiplier `speed`, schedule

```text
publish_time = t0 + (T - T0) / speed
```

Use a monotonic wall clock and absolute deadlines to avoid accumulated timing
error. Preserve source order for equal timestamps. Initial state and context
files are inputs for initialization, outside the scored replay event stream.

## Activity labels and evaluation

The event streams contain no activity labels. Join the annotation intervals only
when constructing training targets or evaluating predictions. The complete
annotation file is an offline evaluation resource.

In the complete annotation file, `start` and `end` denote the original annotated
boundary events. Both endpoints count as belonging to the activity. Split files
clip intervals to the split boundaries and include `start_clipped`, `end_clipped`
and `end_inclusive`. A clipped end at the next split's start is exclusive. This
keeps each partition's labels inside its own time range. Clipped intervals have
incomplete episode durations and should be excluded from full-duration analyses.

Concurrent annotations retain separate intervals. A simple single-label
classifier can exclude ambiguous examples from fitting and scoring while
reporting that exclusion. Unannotated times have unknown ground truth. They do
not supply a verified background or inactivity class. Ensure every feature uses
only observations available at or before its prediction timestamp.

Evaluate with per-class support and recall, macro-F1, and a majority-class
baseline. Some activities are absent or scarce during replay. The manifest lists
annotation counts by split. Evaluate data fidelity independently by comparing
sensor state at selected times against the original recording and cleaning audit.

The data supports activity recognition and routine monitoring. It contains no
verified fall labels. Any unusual-inactivity alert therefore requires its own
evaluation and a description of what evidence triggered it.
