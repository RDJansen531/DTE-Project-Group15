# Step 2: Historical replay

The replay streams the prepared Aruba recording to an MQTT broker as if the
sensors were live. It meets the three Step 2 requirements.

| Requirement | How it is met |
| --- | --- |
| Every sensor has its own stream, no aggregation | One topic per physical sensor, `aruba/sensors/<sensor_id>`. One MQTT message per reading. |
| Cadence kept at a configurable speed | A reading with dataset time `T` is published at `t0 + (T - T0) / speed`. Deadlines are absolute on a monotonic clock, so timing errors never add up. |
| Published through a message broker | Eclipse Mosquitto (MQTT). The replay knows nothing about its consumers. |

## Run it (macOS)

One-time setup, from the repository root:

```sh
brew install mosquitto
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python scripts/prepare_aruba.py        # only if data/aruba/ does not exist yet
```

Then use three terminals (run `source .venv/bin/activate` in terminals 2 and 3).

```sh
# Terminal 1: the broker
mosquitto -c mosquitto/mosquitto.conf

# Terminal 2: the checker (start it before the replay)
python replay/verify_replay.py

# Terminal 3: replay four hours of 1 May 2011 in four minutes
python replay/replay.py --start 2011-05-01T08:00:00 --end 2011-05-01T12:00:00 --speed 60
```

When the replay ends, terminal 2 prints `PASS` or `FAIL` for each check: every
reading arrived, each one on its own sensor's topic, none duplicated, in order,
and on time according to the formula.

To watch raw messages, run `mosquitto_sub -t 'aruba/#' -v` in another terminal.

Useful options for `replay.py`:

| Option | Meaning | Default |
| --- | --- | --- |
| `--speed` | Replay multiplier. `1` is real time, `60` plays an hour per minute. | `60` |
| `--start`, `--end` | Dataset time window inside the split | whole split |
| `--split` | `train`, `validation` or `replay` | `replay` |
| `--dry-run` | Keep the timing, print instead of publishing. Needs no broker. | off |
| `--max-events` | Stop after this many readings | none |
| `--host`, `--port`, `--qos` | Broker address and delivery level | `localhost`, `1883`, `1` |

Run the unit tests with `python -m unittest discover tests`.

## Topics and messages

Each reading is published to `aruba/sensors/<sensor_id>` as one JSON message:

```json
{"event_id":"aruba:1398960","home_id":"aruba","timestamp":"2011-05-01T08:10:07.131119",
 "sensor_id":"M004","sensor_type":"motion","value":"ON","raw_value":"ON",
 "quality":"valid","quality_reason":null,"source_line":1398960}
```

`timestamp` is the original dataset time. Consumers must use it, never the
computer clock. Activity labels are never published.

The replay also publishes a retained message on `aruba/replay/status` when it
starts and ends. It carries the dataset window, the speed and, at the end, the
number of readings published and the lateness statistics. It contains no sensor
data. Consumers use it to map wall-clock time to dataset time.

Readings flagged `quality: "invalid"` during preparation (167 in the whole
recording) are still published, with `value: null` and the raw token. A real
sensor can emit garbage too, so the twin decides how to treat them.

## Timing

`T0` is the start of the replayed window and `t0` is the wall-clock moment the
replay starts. Each reading waits until its own deadline `t0 + (T - T0) / speed`.
Readings with equal timestamps keep their source order. If a reading is late, the
next one is still scheduled from `t0`, so lateness does not carry over.

Lateness measured without a broker (`--dry-run`, Linux container):

| Window | Speed | Readings | Mean | 99th percentile | Worst |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1 May, 08:00 to 09:00 | 3600x | 942 | 0.16 ms | 0.37 ms | 1.1 ms |
| 1 May, full day | 1440x | 8,876 | 0.20 ms | 0.90 ms | 9.1 ms |

Repeat these with the real broker on your own machine for the report.

## Sensor inventory

All 39 physical sensors. Motion and door sensors are event-based: they send only
when their state changes. Temperature sensors are sampled on a cycle of about
five minutes (303 s), and a reading is recorded only when the value changed, so
gaps between temperature messages are multiples of about five minutes.

| Sensor | Type | Reporting | Values | Topic | Readings in recording |
| --- | --- | --- | --- | --- | ---: |
| `D001` | door | Event-based | `OPEN` / `CLOSE` when the door contact changes | `aruba/sensors/D001` | 517 |
| `D002` | door | Event-based | `OPEN` / `CLOSE` when the door contact changes | `aruba/sensors/D002` | 413 |
| `D004` | door | Event-based | `OPEN` / `CLOSE` when the door contact changes | `aruba/sensors/D004` | 5,852 |
| `M001` | motion | Event-based | `ON` when movement starts, `OFF` when it stops | `aruba/sensors/M001` | 12,083 |
| `M002` | motion | Event-based | `ON` when movement starts, `OFF` when it stops | `aruba/sensors/M002` | 9,499 |
| `M003` | motion | Event-based | `ON` when movement starts, `OFF` when it stops | `aruba/sensors/M003` | 40,458 |
| `M004` | motion | Event-based | `ON` when movement starts, `OFF` when it stops | `aruba/sensors/M004` | 19,975 |
| `M005` | motion | Event-based | `ON` when movement starts, `OFF` when it stops | `aruba/sensors/M005` | 35,409 |
| `M006` | motion | Event-based | `ON` when movement starts, `OFF` when it stops | `aruba/sensors/M006` | 27,273 |
| `M007` | motion | Event-based | `ON` when movement starts, `OFF` when it stops | `aruba/sensors/M007` | 93,725 |
| `M008` | motion | Event-based | `ON` when movement starts, `OFF` when it stops | `aruba/sensors/M008` | 35,401 |
| `M009` | motion | Event-based | `ON` when movement starts, `OFF` when it stops | `aruba/sensors/M009` | 262,587 |
| `M010` | motion | Event-based | `ON` when movement starts, `OFF` when it stops | `aruba/sensors/M010` | 47,049 |
| `M011` | motion | Event-based | `ON` when movement starts, `OFF` when it stops | `aruba/sensors/M011` | 4,049 |
| `M012` | motion | Event-based | `ON` when movement starts, `OFF` when it stops | `aruba/sensors/M012` | 30,272 |
| `M013` | motion | Event-based | `ON` when movement starts, `OFF` when it stops | `aruba/sensors/M013` | 86,413 |
| `M014` | motion | Event-based | `ON` when movement starts, `OFF` when it stops | `aruba/sensors/M014` | 88,902 |
| `M015` | motion | Event-based | `ON` when movement starts, `OFF` when it stops | `aruba/sensors/M015` | 97,860 |
| `M016` | motion | Event-based | `ON` when movement starts, `OFF` when it stops | `aruba/sensors/M016` | 29,525 |
| `M017` | motion | Event-based | `ON` when movement starts, `OFF` when it stops | `aruba/sensors/M017` | 49,726 |
| `M018` | motion | Event-based | `ON` when movement starts, `OFF` when it stops | `aruba/sensors/M018` | 81,844 |
| `M019` | motion | Event-based | `ON` when movement starts, `OFF` when it stops | `aruba/sensors/M019` | 157,079 |
| `M020` | motion | Event-based | `ON` when movement starts, `OFF` when it stops | `aruba/sensors/M020` | 161,614 |
| `M021` | motion | Event-based | `ON` when movement starts, `OFF` when it stops | `aruba/sensors/M021` | 46,357 |
| `M022` | motion | Event-based | `ON` when movement starts, `OFF` when it stops | `aruba/sensors/M022` | 30,939 |
| `M023` | motion | Event-based | `ON` when movement starts, `OFF` when it stops | `aruba/sensors/M023` | 12,535 |
| `M024` | motion | Event-based | `ON` when movement starts, `OFF` when it stops | `aruba/sensors/M024` | 57,285 |
| `M025` | motion | Event-based | `ON` when movement starts, `OFF` when it stops | `aruba/sensors/M025` | 4,959 |
| `M026` | motion | Event-based | `ON` when movement starts, `OFF` when it stops | `aruba/sensors/M026` | 25,437 |
| `M027` | motion | Event-based | `ON` when movement starts, `OFF` when it stops | `aruba/sensors/M027` | 14,805 |
| `M028` | motion | Event-based | `ON` when movement starts, `OFF` when it stops | `aruba/sensors/M028` | 7,791 |
| `M029` | motion | Event-based | `ON` when movement starts, `OFF` when it stops | `aruba/sensors/M029` | 7,744 |
| `M030` | motion | Event-based | `ON` when movement starts, `OFF` when it stops | `aruba/sensors/M030` | 8,443 |
| `M031` | motion | Event-based | `ON` when movement starts, `OFF` when it stops | `aruba/sensors/M031` | 2,849 |
| `T001` | temperature | Periodic (~5 min), reported on change | Degrees Celsius in 0.5 steps | `aruba/sensors/T001` | 16,588 |
| `T002` | temperature | Periodic (~5 min), reported on change | Degrees Celsius in 0.5 steps | `aruba/sensors/T002` | 26,193 |
| `T003` | temperature | Periodic (~5 min), reported on change | Degrees Celsius in 0.5 steps | `aruba/sensors/T003` | 26,385 |
| `T004` | temperature | Periodic (~5 min), reported on change | Degrees Celsius in 0.5 steps | `aruba/sensors/T004` | 20,174 |
| `T005` | temperature | Periodic (~5 min), reported on change | Degrees Celsius in 0.5 steps | `aruba/sensors/T005` | 27,119 |
