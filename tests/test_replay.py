"""Timing and ordering tests for the replay. Run: python -m unittest discover tests"""

from datetime import datetime, timedelta
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "replay"))
import replay  # noqa: E402
import verify_replay  # noqa: E402


class FakeClock:
    """A clock that only moves when the replay sleeps, plus a fixed publish cost."""

    def __init__(self, publish_cost=0.0):
        self.now, self.publish_cost = 1000.0, publish_cost

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def make_events(offsets):
    t_zero = datetime(2011, 5, 1)
    events = []
    for index, offset in enumerate(offsets):
        stamp = t_zero + timedelta(seconds=offset)
        sensor = f"M{index % 3 + 1:03}"
        events.append((stamp, {"event_id": f"aruba:{index}", "timestamp": stamp.isoformat(),
                               "sensor_id": sensor, "sensor_type": "motion", "value": "ON"}))
    return t_zero, events


class ReplayTiming(unittest.TestCase):
    def play(self, offsets, speed, publish_cost=0.0):
        t_zero, events = make_events(offsets)
        clock, sent = FakeClock(publish_cost), []

        def publish(event):
            sent.append((clock.now, event))
            clock.now += clock.publish_cost
            return f"aruba/sensors/{event['sensor_id']}"

        result = replay.run(events, publish, t_zero, speed, clock=clock, sleep=clock.sleep,
                            report_every=1e9)
        return sent, result

    def test_deadlines_follow_the_formula(self):
        offsets = [0, 6, 6, 60, 3600]
        for speed in (1, 60, 3600):
            sent, result = self.play(offsets, speed)
            for (wall, _), offset in zip(sent, offsets):
                self.assertAlmostEqual(wall, 1000.0 + offset / speed, places=6)
            self.assertEqual(result["published"], len(offsets))
            self.assertLess(result["lateness_ms"]["max"], 1e-3)

    def test_source_order_is_kept_for_equal_timestamps(self):
        sent, _ = self.play([5, 5, 5, 5], 10)
        self.assertEqual([event["event_id"] for _, event in sent],
                         [f"aruba:{i}" for i in range(4)])

    def test_slow_publishing_does_not_accumulate_delay(self):
        # Each publish costs 20 ms. After a burst, a later event must still be on time.
        sent, _ = self.play([0, 0, 0, 0, 0, 10], 1, publish_cost=0.02)
        self.assertAlmostEqual(sent[-1][0], 1010.0, places=6)

    def test_one_topic_per_sensor(self):
        _, result = self.play(range(30), 100)
        self.assertEqual(result["distinct_topics"], 3)


class VerifierChecks(unittest.TestCase):
    def build(self, speed=60, skew=0.0):
        t_zero, events = make_events([0, 30, 60, 600])
        status = {"speed": speed, "dataset_start": t_zero.isoformat(), "published": len(events)}
        messages = [(50.0 + (stamp - t_zero).total_seconds() / speed + (skew if i == 3 else 0),
                     f"aruba/sensors/{event['sensor_id']}", event)
                    for i, (stamp, event) in enumerate(events)]
        return messages, status

    def test_good_replay_passes(self):
        messages, status = self.build()
        self.assertTrue(all(ok for _, ok, _ in verify_replay.check(messages, status, "aruba/sensors", 50)))

    def test_late_message_fails_timing(self):
        messages, status = self.build(skew=0.5)
        failed = [name for name, ok, _ in verify_replay.check(messages, status, "aruba/sensors", 50) if not ok]
        self.assertEqual(len(failed), 1)
        self.assertIn("timing", failed[0])

    def test_wrong_topic_and_lost_message_fail(self):
        messages, status = self.build()
        messages[1] = (messages[1][0], "aruba/sensors/M999", messages[1][2])
        failed = [name for name, ok, _ in verify_replay.check(messages[:-1], status, "aruba/sensors", 50) if not ok]
        self.assertTrue(any("own sensor" in name for name in failed))
        self.assertTrue(any("arrived" in name for name in failed))


if __name__ == "__main__":
    unittest.main()
