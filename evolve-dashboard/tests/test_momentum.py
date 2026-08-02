"""Tests for the Momentum store, API and integrations.

Stdlib only, so they run inside the add-on image as well as on a dev box:

    python3 -m unittest discover -s evolve-dashboard/tests -v
"""

from __future__ import annotations

import json
import io
import logging
import os
import re
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from backup import create_data_backup  # noqa: E402
from counter_icons import COUNTER_ICON_CATALOG, COUNTER_ICONS  # noqa: E402
from gym import gym_summary  # noqa: E402
from integrations import Hevy, HomeAssistant, Telegram, normalise_workout  # noqa: E402
from scheduler import Scheduler  # noqa: E402
from server import Api, Handler, Sessions, Settings, Throttle  # noqa: E402
from storage import Store, days_since, hash_pin, verify_pin  # noqa: E402
from zepp import parse_zepp_life_export  # noqa: E402

DAY = lambda off: (date.today() - timedelta(days=off)).isoformat()  # noqa: E731


def zepp_zip(body_csv: str, *, include_user_copy: bool = False) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("export/BODY/BODY_123.csv", body_csv)
        archive.writestr("export/ACTIVITY/ACTIVITY_123.csv", "date,steps\n2026-01-01,1000\n")
        if include_user_copy:
            archive.writestr("export/user/BODY/BODY_private.csv", body_csv)
    return buffer.getvalue()


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "momentum.json")
        self.store = Store(self.path)
        self.user = self.store.add_user("Maya", "4821")
        self.uid = self.user["id"]

    # -- identity -------------------------------------------------------

    def test_pin_is_hashed_not_stored(self) -> None:
        with open(self.path, encoding="utf-8") as handle:
            raw = handle.read()
        self.assertNotIn("4821", raw)
        self.assertTrue(self.store.check_pin(self.uid, "4821"))
        self.assertFalse(self.store.check_pin(self.uid, "4822"))

    def test_pin_hash_is_salted(self) -> None:
        first, salt_a = hash_pin("1234")
        second, salt_b = hash_pin("1234")
        self.assertNotEqual(salt_a, salt_b)
        self.assertNotEqual(first, second)
        self.assertTrue(verify_pin("1234", first, salt_a))
        self.assertFalse(verify_pin("1234", first, salt_b))

    def test_store_file_is_owner_only(self) -> None:
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    # -- counters -------------------------------------------------------

    def test_counter_icon_catalog_is_large_unique_and_searchable(self) -> None:
        self.assertGreaterEqual(len(COUNTER_ICON_CATALOG), 120)
        self.assertEqual(len(COUNTER_ICONS), len(set(COUNTER_ICONS)))
        smoke = next(item for item in COUNTER_ICON_CATALOG if item["name"] == "cigarette-slash")
        self.assertIn("smoking", smoke["keywords"])
        counter = self.store.save_counter(self.uid, {"name": "Quit smoking", "icon": "cigarette-slash"}, None)
        self.assertEqual(counter["icon"], "cigarette-slash")

    def test_counter_days_and_reset_keeps_best(self) -> None:
        counter = self.store.save_counter(
            self.uid, {"name": "No sugar", "icon": "coffee", "date": DAY(24), "on_dash": True}, None
        )
        self.assertEqual(days_since(counter["date"]), 24)
        reset = self.store.reset_counter(self.uid, counter["id"])
        self.assertEqual(reset["date"], date.today().isoformat())
        self.assertEqual(reset["best"], 24)

    def test_dashboard_holds_at_most_three_counters(self) -> None:
        for i in range(3):
            self.store.save_counter(self.uid, {"name": f"C{i}", "on_dash": True}, None)
        fourth = self.store.save_counter(self.uid, {"name": "C4", "on_dash": True}, None)
        self.assertFalse(fourth["on_dash"], "the fourth counter must not auto-pin")

        with self.assertRaises(ValueError):
            self.store.toggle_counter_dash(self.uid, fourth["id"])

        shown = [c for c in self.store.user(self.uid)["counters"] if c["on_dash"]]
        self.assertEqual(len(shown), 3)

    def test_counter_requires_a_name(self) -> None:
        with self.assertRaises(ValueError):
            self.store.save_counter(self.uid, {"name": "   "}, None)

    def test_reset_reopens_milestones(self) -> None:
        counter = self.store.save_counter(self.uid, {"name": "No sugar", "date": DAY(30)}, None)
        key = f"{counter['id']}:30"
        self.assertTrue(self.store.mark_milestone(self.uid, key))
        self.assertFalse(self.store.mark_milestone(self.uid, key), "already announced")
        self.store.reset_counter(self.uid, counter["id"])
        self.assertTrue(self.store.mark_milestone(self.uid, key), "a fresh run earns it again")

    # -- affirmations / habits / journal ---------------------------------

    def test_only_one_affirmation_is_pinned(self) -> None:
        first = self.store.save_affirmation(self.uid, "One", None)
        second = self.store.save_affirmation(self.uid, "Two", None)
        self.assertTrue(first["pinned"], "the first one pins itself")
        self.store.pin_affirmation(self.uid, second["id"])
        pinned = [a for a in self.store.user(self.uid)["affirmations"] if a["pinned"]]
        self.assertEqual([a["id"] for a in pinned], [second["id"]])

    def test_mottos_keep_the_order_they_were_written_in(self) -> None:
        # The dashboard rotates the bank by index, so the order is the feature.
        for text in ("First", "Second", "Third"):
            self.store.save_motto(self.uid, text, None)
        self.assertEqual([m["text"] for m in self.store.user(self.uid)["mottos"]], ["First", "Second", "Third"])

    def test_motto_edit_keeps_its_place(self) -> None:
        first = self.store.save_motto(self.uid, "First", None)
        self.store.save_motto(self.uid, "Second", None)
        edited = self.store.save_motto(self.uid, "  First, rewritten  ", first["id"])
        self.assertEqual(edited["text"], "First, rewritten", "surrounding space is trimmed")
        self.assertEqual([m["text"] for m in self.store.user(self.uid)["mottos"]], ["First, rewritten", "Second"])

    def test_blank_motto_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.store.save_motto(self.uid, "   ", None)
        self.assertEqual(self.store.user(self.uid)["mottos"], [])

    def test_new_member_starts_with_an_empty_motto_bank(self) -> None:
        self.assertEqual(self.store.user(self.uid)["mottos"], [])

    def test_habit_toggle_is_idempotent_per_day(self) -> None:
        habit = self.store.save_habit(self.uid, "Morning yoga", None)
        today = date.today().isoformat()
        self.assertTrue(self.store.toggle_habit(self.uid, habit["id"], today)["done"])
        self.assertFalse(self.store.toggle_habit(self.uid, habit["id"], today)["done"])
        self.assertTrue(self.store.toggle_habit(self.uid, habit["id"], today)["done"])
        self.assertEqual(self.store.user(self.uid)["checkins"][habit["id"]], [today])

    def test_deleting_a_habit_drops_its_history(self) -> None:
        habit = self.store.save_habit(self.uid, "Read", None)
        self.store.toggle_habit(self.uid, habit["id"], date.today().isoformat())
        self.assertTrue(self.store.delete_habit(self.uid, habit["id"]))
        self.assertNotIn(habit["id"], self.store.user(self.uid)["checkins"])

    def test_habit_toggle_rejects_bad_dates(self) -> None:
        habit = self.store.save_habit(self.uid, "Read", None)
        with self.assertRaises(ValueError):
            self.store.toggle_habit(self.uid, habit["id"], "30-07-2026")

    # -- synced data ----------------------------------------------------

    def test_weight_samples_dedupe_by_day(self) -> None:
        self.store.record_weight_sample(self.uid, {"date": DAY(1), "weight": 60.0, "fat": 27.0})
        self.store.record_weight_sample(self.uid, {"date": DAY(1), "weight": 59.6, "fat": 26.2})
        samples = self.store.user(self.uid)["weight_samples"]
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0]["weight"], 59.6)

    def test_weight_sample_without_weight_is_refused(self) -> None:
        self.assertFalse(self.store.record_weight_sample(self.uid, {"date": DAY(0), "fat": 26.2}))

    def test_weight_samples_stay_sorted(self) -> None:
        for off in (5, 1, 9, 3):
            self.store.record_weight_sample(self.uid, {"date": DAY(off), "weight": 60 + off})
        dates = [s["date"] for s in self.store.user(self.uid)["weight_samples"]]
        self.assertEqual(dates, sorted(dates))

    def test_weight_sample_keeps_the_reading_time(self) -> None:
        # The dashboard puts a clock time on today's weight, so the moment the
        # reading was taken has to survive alongside the day it belongs to.
        self.store.record_weight_sample(
            self.uid, {"date": DAY(0), "at": "2026-07-31T08:22:11+01:00", "weight": 71.8}
        )
        sample = self.store.user(self.uid)["weight_samples"][0]
        self.assertEqual(sample["date"], DAY(0))
        self.assertEqual(sample["at"], "2026-07-31T08:22:11+01:00")

    def test_weight_sample_drops_a_timestamp_carrying_no_clock(self) -> None:
        # A bare date parses to midnight, which would show as a 00:00 weigh-in.
        for stamp in ("2026-07-31", "  2026-07-31  ", "not a date", "", None):
            self.store.record_weight_sample(
                self.uid, {"date": DAY(0), "at": stamp, "weight": 71.8}
            )
            sample = self.store.user(self.uid)["weight_samples"][0]
            self.assertNotIn("at", sample, f"{stamp!r} should not become a time")

    def test_imported_history_fills_a_missing_reading_time(self) -> None:
        self.store.record_weight_sample(self.uid, {"date": "2025-01-17", "weight": 74.9})
        result = self.store.merge_weight_samples(
            self.uid, [{"date": "2025-01-17", "at": "2025-01-17T07:05:00+00:00", "weight": 75.1}]
        )
        self.assertEqual(result["updated"], 1, "gaining a time counts as enrichment")
        sample = self.store.user(self.uid)["weight_samples"][0]
        self.assertEqual(sample["at"], "2025-01-17T07:05:00+00:00")
        self.assertEqual(sample["weight"], 74.9, "existing weight still wins")

    def test_imported_history_does_not_overwrite_a_known_reading_time(self) -> None:
        self.store.record_weight_sample(
            self.uid, {"date": "2025-01-17", "at": "2025-01-17T07:05:00+00:00", "weight": 74.9}
        )
        self.store.merge_weight_samples(
            self.uid, [{"date": "2025-01-17", "at": "2025-01-17T19:40:00+00:00", "weight": 75.1}]
        )
        sample = self.store.user(self.uid)["weight_samples"][0]
        self.assertEqual(sample["at"], "2025-01-17T07:05:00+00:00")

    def test_workout_storage_does_not_truncate_history(self) -> None:
        workouts = [{"id": str(index), "date": DAY(index)} for index in range(250)]
        self.assertTrue(self.store.replace_workouts(self.uid, workouts))
        self.assertEqual(len(self.store.user(self.uid)["workouts"]), 250)

    def test_imported_weight_history_only_fills_missing_existing_fields(self) -> None:
        self.store.record_weight_sample(
            self.uid,
            {"date": "2025-01-17", "weight": 74.9, "fat": None, "bmi": 25.6},
        )
        result = self.store.merge_weight_samples(self.uid, [
            {
                "date": "2025-01-17",
                "weight": 75.1,
                "fat": 23.7,
                "bmi": 25.68,
                "water": 52.3,
            },
            {"date": "2025-01-18", "weight": 73.6, "bmi": 25.17},
        ])
        self.assertEqual(result, {"added": 1, "updated": 1, "unchanged": 0})
        samples = {sample["date"]: sample for sample in self.store.user(self.uid)["weight_samples"]}
        self.assertEqual(samples["2025-01-17"]["weight"], 74.9, "existing weight wins")
        self.assertEqual(samples["2025-01-17"]["bmi"], 25.6, "existing BMI wins")
        self.assertEqual(samples["2025-01-17"]["fat"], 23.7, "missing fat is enriched")
        self.assertEqual(samples["2025-01-17"]["water"], 52.3)
        self.assertEqual(samples["2025-01-18"]["weight"], 73.6)

    def test_blank_hevy_password_does_not_erase_saved_key(self) -> None:
        self.store.update_user_settings(self.uid, {"hevy_key": "saved-secret"})
        self.store.update_user_settings(self.uid, {"hevy_key": ""})
        self.assertEqual(self.store.user(self.uid)["settings"]["hevy_key"], "saved-secret")
        self.store.update_user_settings(self.uid, {"clear_hevy_key": True})
        self.assertEqual(self.store.user(self.uid)["settings"]["hevy_key"], "")

    # -- isolation and durability ---------------------------------------

    def test_members_cannot_see_each_others_data(self) -> None:
        other = self.store.add_user("Dan", "1111")
        self.store.save_counter(self.uid, {"name": "Maya only"}, None)
        self.assertEqual(self.store.user(other["id"])["counters"], [])
        self.assertIsNone(self.store.save_counter(other["id"], {"name": "x"}, "nonexistent-id"))

    def test_snapshot_is_a_copy(self) -> None:
        snapshot = self.store.snapshot()
        snapshot["users"].clear()
        self.assertTrue(self.store.users())

    def test_state_survives_a_reopen(self) -> None:
        self.store.save_counter(self.uid, {"name": "No sugar", "date": DAY(10)}, None)
        reopened = Store(self.path)
        self.assertEqual(len(reopened.user(self.uid)["counters"]), 1)
        self.assertTrue(reopened.check_pin(self.uid, "4821"))

    def test_corrupt_file_is_quarantined_not_fatal(self) -> None:
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        recovered = Store(self.path)
        self.assertEqual(recovered.users(), [])
        self.assertTrue(os.path.exists(f"{self.path}.corrupt"))

    def test_concurrent_writes_do_not_lose_data(self) -> None:
        habits = [self.store.save_habit(self.uid, f"H{i}", None) for i in range(6)]

        def work(habit_id: str) -> None:
            for day in range(1, 16):
                self.store.toggle_habit(self.uid, habit_id, f"2026-06-{day:02d}")

        threads = [threading.Thread(target=work, args=(h["id"],)) for h in habits]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        checkins = self.store.user(self.uid)["checkins"]
        for habit in habits:
            self.assertEqual(len(checkins[habit["id"]]), 15)


class SessionTests(unittest.TestCase):
    def test_round_trip_and_tamper_resistance(self) -> None:
        sessions = Sessions(b"secret-key", ttl_days=1)
        token = sessions.issue("user-1")
        self.assertEqual(sessions.verify(token), "user-1")

        body, _, signature = token.rpartition(".")
        self.assertIsNone(sessions.verify(f"{body}.{'0' * len(signature)}"), "bad signature")
        self.assertIsNone(sessions.verify("garbage"))
        self.assertIsNone(sessions.verify(None))
        self.assertIsNone(Sessions(b"other-key", 1).verify(token), "signed by a different secret")

    def test_expired_token_is_rejected(self) -> None:
        expired = Sessions(b"secret-key", ttl_days=0)
        self.assertIsNone(expired.verify(expired.issue("user-1")))


class ThrottleTests(unittest.TestCase):
    def test_backoff_escalates_after_free_attempts(self) -> None:
        throttle = Throttle()
        for _ in range(Throttle.FREE_ATTEMPTS):
            throttle.record_failure("k")
        self.assertEqual(throttle.retry_after("k"), 0.0, "early attempts are free")

        throttle.record_failure("k")
        first = throttle.retry_after("k")
        self.assertGreater(first, 0)

        throttle.record_failure("k")
        self.assertGreater(throttle.retry_after("k"), first, "the delay doubles")

    def test_success_clears_the_lockout(self) -> None:
        throttle = Throttle()
        for _ in range(8):
            throttle.record_failure("k")
        self.assertGreater(throttle.retry_after("k"), 0)
        throttle.clear("k")
        self.assertEqual(throttle.retry_after("k"), 0.0)


class WorkoutTests(unittest.TestCase):
    def test_volume_and_duration_are_derived(self) -> None:
        workout = normalise_workout({
            "id": "w1",
            "title": "Full Body A",
            "start_time": "2026-07-28T09:00:00Z",
            "end_time": "2026-07-28T09:52:00Z",
            "exercises": [
                {"sets": [{"reps": 10, "weight_kg": 40}, {"reps": 10, "weight_kg": 40}]},
                {"sets": [{"reps": 8, "weight_kg": 60}]},
            ],
        })
        self.assertEqual(workout["name"], "Full Body A")
        self.assertEqual(workout["duration_min"], 52)
        self.assertEqual(workout["volume_kg"], 10 * 40 + 10 * 40 + 8 * 60)
        self.assertEqual(workout["exercises"], 2)
        self.assertEqual(workout["sets"], 3)

    def test_malformed_workouts_are_dropped(self) -> None:
        self.assertIsNone(normalise_workout({}))
        self.assertIsNone(normalise_workout({"start_time": "not a date"}))
        self.assertIsNone(normalise_workout("nope"))

    def test_missing_set_values_do_not_crash(self) -> None:
        workout = normalise_workout({
            "title": "Odd", "start_time": "2026-07-28T09:00:00Z",
            "exercises": [{"sets": [{"reps": None, "weight_kg": None}, {}]}],
        })
        self.assertEqual(workout["volume_kg"], 0)
        self.assertEqual(workout["sets"], 2)


class GymSummaryTests(unittest.TestCase):
    def test_current_tuesday_thursday_and_saturday_share_the_current_week(self) -> None:
        summary = gym_summary(
            [
                {"date": "2026-07-28"},
                {"date": "2026-07-30"},
                {"date": "2026-08-01"},
            ],
            3,
            "2026-07-31",
        )
        self.assertEqual(summary["counts"][-1], 3)
        self.assertEqual(summary["current_count"], 3)
        self.assertEqual(summary["streak"], 1)
        self.assertEqual(summary["goal_streak"], 1)

    def test_incomplete_current_week_does_not_erase_completed_streak(self) -> None:
        summary = gym_summary(
            [
                {"date": "2026-07-30"},
                {"date": "2026-07-20"},
                {"date": "2026-07-22"},
                {"date": "2026-07-24"},
                {"date": "2026-07-13"},
                {"date": "2026-07-15"},
                {"date": "2026-07-17"},
            ],
            3,
            "2026-07-31",
        )
        self.assertEqual(summary["current_count"], 1)
        self.assertEqual(summary["streak"], 3)
        self.assertEqual(summary["goal_streak"], 2)

    def test_streak_uses_full_history_not_only_eight_chart_weeks(self) -> None:
        monday = date.fromisoformat("2026-07-27")
        workouts = []
        for week in range(12):
            start = monday - timedelta(weeks=week)
            workouts.extend(
                {"date": (start + timedelta(days=day)).isoformat()}
                for day in (0, 2, 4)
            )
        summary = gym_summary(workouts, 3, "2026-07-31")
        self.assertEqual(len(summary["counts"]), 8)
        self.assertEqual(summary["streak"], 12)
        self.assertEqual(summary["goal_streak"], 12)

    def test_missed_completed_week_breaks_the_streak(self) -> None:
        summary = gym_summary(
            [
                {"date": "2026-07-27"},
                {"date": "2026-07-29"},
                {"date": "2026-07-31"},
                {"date": "2026-07-13"},
                {"date": "2026-07-15"},
                {"date": "2026-07-17"},
            ],
            3,
            "2026-07-31",
        )
        self.assertEqual(summary["streak"], 1)
        self.assertEqual(summary["goal_streak"], 1)

    def test_active_streak_is_not_erased_by_a_high_session_goal(self) -> None:
        monday = date.fromisoformat("2026-07-27")
        workouts = []
        for week in range(10):
            start = monday - timedelta(weeks=week)
            workouts.extend(
                {"date": (start + timedelta(days=day)).isoformat()}
                for day in (0, 1, 2, 3)
            )
        summary = gym_summary(workouts, 5, "2026-07-31")
        self.assertEqual(summary["current_count"], 4)
        self.assertEqual(summary["streak"], 10)
        self.assertEqual(summary["goal_streak"], 0)

    def test_week_start_day_changes_which_week_a_session_counts_for(self) -> None:
        # Sunday the 26th and Monday the 27th. Evenly spaced sessions land one
        # per week under any alignment, so only sessions straddling a boundary
        # tell the two apart: Monday-aligned these fall in consecutive weeks and
        # read as a 2-week streak, Sunday-aligned they share one week, leaving
        # the week before empty for a streak of 1. Same log, two answers, which
        # is why this has to agree with Hevy's setting.
        sessions = [{"date": "2026-07-26"}, {"date": "2026-07-27"}]
        monday_aligned = gym_summary(sessions, 1, "2026-07-31", week_start=0)
        sunday_aligned = gym_summary(sessions, 1, "2026-07-31", week_start=6)
        self.assertEqual(monday_aligned["streak"], 2)
        self.assertEqual(sunday_aligned["streak"], 1)
        self.assertEqual(sunday_aligned["week_start"], 6)

    def test_streak_reports_the_week_it_began_and_the_gap_that_capped_it(self) -> None:
        monday = date.fromisoformat("2026-07-27")
        workouts = [{"date": (monday - timedelta(weeks=week)).isoformat()} for week in range(3)]
        # Nothing in the week of 6 July; an older session proves history reaches
        # past the gap, so the cap is a real miss rather than the log running out.
        workouts.append({"date": (monday - timedelta(weeks=4)).isoformat()})
        summary = gym_summary(workouts, 1, "2026-07-31", week_start=0)
        self.assertEqual(summary["streak"], 3)
        self.assertEqual(summary["streak_since"], "2026-07-13")
        self.assertEqual(summary["streak_broken_week"], "2026-07-06")

    def test_streak_running_to_the_start_of_history_reports_no_gap(self) -> None:
        monday = date.fromisoformat("2026-07-27")
        workouts = [{"date": (monday - timedelta(weeks=week)).isoformat()} for week in range(3)]
        summary = gym_summary(workouts, 1, "2026-07-31", week_start=0)
        self.assertEqual(summary["streak"], 3)
        self.assertEqual(summary["streak_since"], "2026-07-13")
        self.assertIsNone(summary["streak_broken_week"])

    def test_week_start_setting_round_trips_and_is_clamped(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = Store(os.path.join(directory.name, "momentum.json"))
        uid = store.add_user("Andrew", "1234")["id"]
        # Sunday by default, matching Hevy.
        self.assertEqual(store.user(uid)["settings"]["gym_week_start"], 6)
        self.assertEqual(store.update_user_settings(uid, {"gym_week_start": 0})["gym_week_start"], 0)
        self.assertEqual(store.update_user_settings(uid, {"gym_week_start": 6})["gym_week_start"], 6)
        self.assertEqual(store.update_user_settings(uid, {"gym_week_start": 99})["gym_week_start"], 6)
        self.assertEqual(store.update_user_settings(uid, {"gym_week_start": "x"})["gym_week_start"], 6)


class IntegrationClientTests(unittest.TestCase):
    """Each client is pointed at a local stub rather than the real service."""

    def stub(self, handler_cls):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        # Cleanups run last-in-first-out: stop serving, join, then release
        # the socket — otherwise the listener leaks into the next test.
        self.addCleanup(httpd.server_close)
        self.addCleanup(thread.join, 5)
        self.addCleanup(httpd.shutdown)
        return f"http://127.0.0.1:{httpd.server_address[1]}"

    def test_home_assistant_reads_and_caches(self) -> None:
        import integrations

        hits = []

        class Core(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                hits.append(self.path)
                if "missing" in self.path:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = json.dumps({
                    "state": "59.6",
                    "attributes": {"unit_of_measurement": "kg", "friendly_name": "Weight"},
                    "last_changed": "2026-07-28T07:12:00+00:00",
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        base = self.stub(Core)
        original = integrations.HA_CORE_API
        integrations.HA_CORE_API = f"{base}/api"
        try:
            client = HomeAssistant("token")
            reading = client.state("sensor.mi_scale_weight")
            self.assertTrue(reading["ok"])
            self.assertEqual(reading["value"], 59.6)
            self.assertEqual(reading["unit"], "kg")

            client.state("sensor.mi_scale_weight")
            self.assertEqual(len(hits), 1, "second read is served from cache")

            missing = client.state("sensor.missing")
            self.assertFalse(missing["ok"])
            self.assertEqual(missing["reason"], "not found")
        finally:
            integrations.HA_CORE_API = original

    def test_home_assistant_without_token_is_inert(self) -> None:
        client = HomeAssistant(None)
        self.assertFalse(client.available)
        self.assertFalse(client.state("sensor.anything")["ok"])

    def test_bodymiscale_reads_current_and_complete_attribute_history(self) -> None:
        import integrations

        hits = []

        class Core(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                hits.append(self.path)
                if "/history/period/" in self.path:
                    body = json.dumps([[
                        {
                            "state": "on",
                            "attributes": {
                                "weight": "61.2 kg",
                                "body_fat": 23.4,
                                "muscle_mass": 44.1,
                                "bmi": 21.8,
                                "water": 52.3,
                                "visceral_fat": 7,
                            },
                            "last_changed": "2024-01-02T07:00:00+00:00",
                        },
                        {
                            "state": "on",
                            "attributes": {"weight": 60.8, "body_fat": 23.1},
                            "last_changed": "2026-07-28T07:00:00+00:00",
                        },
                    ]]).encode()
                else:
                    body = json.dumps({
                        "state": "on",
                        "attributes": {
                            "weight": 60.8,
                            "body_fat": 23.1,
                            "muscle_mass": 44.4,
                            "bmi": 21.6,
                            "water": 52.8,
                            "visceral_fat": 7,
                        },
                        "last_updated": "2026-07-28T07:00:00+00:00",
                    }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        base = self.stub(Core)
        original = integrations.HA_CORE_API
        integrations.HA_CORE_API = f"{base}/api"
        try:
            client = HomeAssistant("token")
            current = client.bodymiscale_state("bodymiscale.maya")
            self.assertEqual(current["weight"], 60.8)
            self.assertEqual(current["fat"], 23.1)
            self.assertEqual(current["visceral"], 7)

            history = client.bodymiscale_history("bodymiscale.maya")
            self.assertEqual([sample["date"] for sample in history], ["2024-01-02", "2026-07-28"])
            self.assertEqual(history[0]["weight"], 61.2)
            history_url = next(path for path in hits if "/history/period/" in path)
            self.assertIn("1970-01-01", history_url)
            self.assertNotIn("no_attributes", history_url)
            self.assertNotIn("minimal_response", history_url)
        finally:
            integrations.HA_CORE_API = original

    def test_bodymiscale_reads_nested_measurement_objects(self) -> None:
        import integrations

        class Core(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                body = json.dumps({
                    "state": "on",
                    "attributes": {
                        "measurements": {
                            "Weight (kg)": {"value": "75.2 kg", "unit": "kg"},
                            "Body Fat Percentage": {"state": 23.1},
                        },
                        "sensors": [
                            {"name": "Body Water Percentage", "value": 52.0},
                            {"name": "Visceral Fat Level", "value": 8},
                        ],
                    },
                    "last_updated": "2026-07-28T07:00:00+00:00",
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        base = self.stub(Core)
        original = integrations.HA_CORE_API
        integrations.HA_CORE_API = f"{base}/api"
        try:
            current = HomeAssistant("token").bodymiscale_state("bodymiscale.andrey")
            self.assertEqual(current["weight"], 75.2)
            self.assertEqual(current["fat"], 23.1)
            self.assertEqual(current["water"], 52.0)
            self.assertEqual(current["visceral"], 8)
        finally:
            integrations.HA_CORE_API = original

    def test_hevy_fetches_every_page(self) -> None:
        import integrations

        pages = []

        class HevyApi(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                if self.path.endswith("/workouts/count"):
                    body = json.dumps({"workout_count": 23}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                page = int(self.path.split("page=")[1].split("&")[0])
                pages.append(page)
                start = (page - 1) * 10
                count = 10 if page < 3 else 3
                workouts = [
                    {
                        "id": str(index),
                        "title": f"Workout {index}",
                        "start_time": f"2026-07-{(index % 28) + 1:02d}T07:00:00Z",
                        "exercises": [],
                    }
                    for index in range(start, start + count)
                ]
                body = json.dumps({"workouts": workouts, "page_count": 3}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        base = self.stub(HevyApi)
        original = integrations.HEVY_API
        integrations.HEVY_API = f"{base}/v1"
        try:
            workouts, error = Hevy("key").workouts()
            self.assertIsNone(error)
            self.assertEqual(len(workouts), 23)
            self.assertEqual(pages, [1, 2, 3])
        finally:
            integrations.HEVY_API = original

    def test_hevy_reports_later_page_failure_instead_of_accepting_partial_data(self) -> None:
        import integrations

        class HevyApi(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                if self.path.endswith("/workouts/count"):
                    body = json.dumps({"workout_count": 20}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                page = int(self.path.split("page=")[1].split("&")[0])
                if page == 2:
                    self.send_response(503)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                workouts = [
                    {
                        "id": str(index),
                        "title": "Workout",
                        "start_time": "2026-07-28T07:00:00Z",
                        "exercises": [],
                    }
                    for index in range(10)
                ]
                body = json.dumps({"workouts": workouts}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        base = self.stub(HevyApi)
        original = integrations.HEVY_API
        integrations.HEVY_API = f"{base}/v1"
        original_sleep = integrations.time.sleep
        integrations.time.sleep = lambda _seconds: None
        try:
            workouts, error = Hevy("key").workouts()
            self.assertEqual(len(workouts), 10)
            self.assertEqual(error, "Hevy API returned HTTP 503")
        finally:
            integrations.time.sleep = original_sleep
            integrations.HEVY_API = original

    def test_hevy_reports_a_bad_key(self) -> None:
        import integrations

        class Api401(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(401)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        base = self.stub(Api401)
        original = integrations.HEVY_API
        integrations.HEVY_API = f"{base}/v1"
        try:
            workouts, error = Hevy("bad-key").workouts()
            self.assertEqual(workouts, [])
            self.assertEqual(error, "Hevy API key is invalid")
        finally:
            integrations.HEVY_API = original

    def test_hevy_without_a_key_does_not_call_out(self) -> None:
        self.assertEqual(Hevy("").workouts(), ([], "no api key"))

    def test_telegram_extracts_senders_from_updates(self) -> None:
        import integrations

        class Bot(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                body = json.dumps({
                    "ok": True,
                    "result": [
                        {
                            "update_id": 12,
                            "message": {
                                "chat": {"id": 482915337},
                                "from": {"first_name": "Dan", "username": "dan_v"},
                                "text": "/start",
                            },
                        },
                        {
                            "update_id": 13,
                            "message": {
                                "chat": {"id": 904471182},
                                "from": {"first_name": "Alex"},
                                "text": "hello",
                            },
                        },
                    ],
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        base = self.stub(Bot)
        original = integrations.TELEGRAM_API
        integrations.TELEGRAM_API = base
        try:
            chats, offset = Telegram("token").updates(0)
            self.assertEqual(offset, 13)
            self.assertEqual(
                sorted((c["chat_id"], c["handle"]) for c in chats),
                [("482915337", "@dan_v"), ("904471182", "")],
            )
        finally:
            integrations.TELEGRAM_API = original

    def test_telegram_without_a_token_is_inert(self) -> None:
        bot = Telegram("")
        self.assertFalse(bot.configured)
        self.assertEqual(bot.send("1", "hi"), (False, "no bot token"))
        self.assertEqual(bot.send_document("1", "backup.zip", b"zip"), (False, "no bot token"))
        self.assertEqual(bot.updates(0), ([], 0))

    def test_telegram_sends_zip_as_multipart_document(self) -> None:
        import integrations

        received = {}

        class Bot(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers["Content-Length"])
                received["path"] = self.path
                received["type"] = self.headers["Content-Type"]
                received["body"] = self.rfile.read(length)
                body = json.dumps({"ok": True, "result": {"document": {}}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        base = self.stub(Bot)
        original = integrations.TELEGRAM_API
        integrations.TELEGRAM_API = base
        try:
            ok, error = Telegram("secret").send_document(
                "-100123", "momentum backup.zip", b"PK-test-data", "Momentum backup"
            )
            self.assertTrue(ok)
            self.assertIsNone(error)
            self.assertEqual(received["path"], "/botsecret/sendDocument")
            self.assertIn("multipart/form-data; boundary=", received["type"])
            self.assertIn(b'name="chat_id"\r\n\r\n-100123', received["body"])
            self.assertIn(b'filename="momentum_backup.zip"', received["body"])
            self.assertIn(b"PK-test-data", received["body"])
        finally:
            integrations.TELEGRAM_API = original


class BackupTests(unittest.TestCase):
    def test_data_backup_includes_nested_files_and_ignores_symlinks_and_temp_files(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            os.mkdir(os.path.join(folder, "nested"))
            with open(os.path.join(folder, "momentum.json"), "wb") as handle:
                handle.write(b'{"history":"complete"}')
            with open(os.path.join(folder, "nested", "extra.db"), "wb") as handle:
                handle.write(b"extra")
            with open(os.path.join(folder, ".momentum-write.tmp"), "wb") as handle:
                handle.write(b"partial")
            os.symlink(os.path.join(folder, "momentum.json"), os.path.join(folder, "linked.json"))

            payload, count = create_data_backup(folder)
            self.assertEqual(count, 2)
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                self.assertEqual(sorted(archive.namelist()), ["momentum.json", "nested/extra.db"])
                self.assertEqual(archive.read("momentum.json"), b'{"history":"complete"}')


class ZeppLifeTests(unittest.TestCase):
    def test_body_csv_maps_metrics_and_ignores_zero_null_and_user_folder(self) -> None:
        archive = zepp_zip(
            "\ufefftime,weight,height,bmi,fatRate,bodyWaterRate,boneMass,metabolism,muscleRate,visceralFat\n"
            "2025-01-17 08:00:00+0000,75.5,171,25.8,0,0,0,0,0,0\n"
            "2025-01-17 12:53:17+0000,75.1,171,25.68,23.7,52.3,2.91,1694,54.34,9\n"
            "2025-01-18 08:19:56+0000,73.6,171,25.17,null,null,null,null,null,null\n",
            include_user_copy=True,
        )
        samples, rows = parse_zepp_life_export(archive)
        self.assertEqual(rows, 3, "the duplicate CSV under user must be ignored")
        self.assertEqual(len(samples), 2)
        self.assertEqual(samples[0], {
            "date": "2025-01-17",
            "weight": 75.1,
            "bmi": 25.68,
            "fat": 23.7,
            "water": 52.3,
            "muscle": 54.34,
            "visceral": 9.0,
        })
        self.assertEqual(samples[1], {"date": "2025-01-18", "weight": 73.6, "bmi": 25.17})

    def test_invalid_or_bodyless_exports_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "valid Zepp Life ZIP"):
            parse_zepp_life_export(b"not a zip")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("export/user/profile.csv", "private")
        with self.assertRaisesRegex(ValueError, "No BODY"):
            parse_zepp_life_export(buffer.getvalue())


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dir = tempfile.TemporaryDirectory()
        settings = Settings()
        settings.data_dir = cls.dir.name
        settings.www_dir = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "app", "www")
        )
        cls.store = Store(os.path.join(cls.dir.name, "momentum.json"))
        hass = HomeAssistant(None)
        Handler.settings = settings
        Handler.api = Api(settings, cls.store, hass, Scheduler(cls.store, hass, settings))

        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)
        cls.dir.cleanup()

    def setUp(self) -> None:
        self.cookie = None
        self.raw_cookie = None

    def call(self, path, method="GET", body=None, cookie=True):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if cookie and self.cookie:
            headers["Cookie"] = self.cookie
        request = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request) as response:
                self._capture(response)
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            self._capture(error)
            raw = error.read()
            try:
                return error.code, json.loads(raw)
            except ValueError:
                return error.code, {"raw": raw[:80].decode(errors="replace")}

    def call_raw(self, path, body: bytes, content_type: str, cookie=True):
        headers = {"Content-Type": content_type}
        if cookie and self.cookie:
            headers["Cookie"] = self.cookie
        request = urllib.request.Request(
            self.base + path, data=body, method="POST", headers=headers
        )
        try:
            with urllib.request.urlopen(request) as response:
                self._capture(response)
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            self._capture(error)
            return error.code, json.loads(error.read() or b"{}")

    def _capture(self, response) -> None:
        for value in response.headers.get_all("Set-Cookie") or []:
            self.raw_cookie = value          # attributes intact, for flag assertions
            self.cookie = value.split(";")[0]  # name=value, for sending back

    def sign_in(self):
        status, payload = self.call("/api/session")
        if payload.get("setup_required"):
            self.call("/api/setup", "POST", {"name": "Maya", "pin": "4821"})
            return
        user_id = payload["users"][0]["id"]
        # A shared Throttle across tests would spuriously lock us out.
        Handler.api.throttle.clear(f"u:{user_id}")
        Handler.api.throttle.clear("ip:127.0.0.1")
        self.call("/api/session", "POST", {"user_id": user_id, "pin": "4821"})

    # -- auth -----------------------------------------------------------

    def test_protected_routes_need_a_session(self) -> None:
        for path, method in [
            ("/api/bootstrap", "GET"),
            ("/api/counters", "POST"),
            ("/api/settings", "PUT"),
            ("/api/telegram/backup", "POST"),
            ("/api/sync/hevy", "POST"),
        ]:
            status, _ = self.call(path, method, {} if method != "GET" else None, cookie=False)
            self.assertEqual(status, 401, f"{method} {path}")

    def test_setup_then_sign_in(self) -> None:
        self.sign_in()
        status, payload = self.call("/api/bootstrap")
        self.assertEqual(status, 200)
        self.assertEqual(payload["me"]["name"], "Maya")

    def test_setup_cannot_run_twice(self) -> None:
        self.sign_in()
        status, _ = self.call("/api/setup", "POST", {"name": "Intruder", "pin": "0000"})
        self.assertEqual(status, 409)

    def test_pin_must_be_four_digits(self) -> None:
        self.sign_in()
        for bad in ("123", "12345", "abcd", ""):
            status, _ = self.call("/api/household", "POST", {"name": "X", "pin": bad})
            self.assertEqual(status, 400, bad)

    def test_secrets_never_leave_the_container(self) -> None:
        self.sign_in()
        self.call("/api/telegram", "PUT", {"token": "12345:SECRET-TOKEN", "default_chat": "-100"})
        self.call("/api/settings", "PUT", {"hevy_key": "HEVY-SECRET"})
        _, payload = self.call("/api/bootstrap")
        blob = json.dumps(payload)
        self.assertNotIn("SECRET-TOKEN", blob)
        self.assertNotIn("HEVY-SECRET", blob)
        self.assertTrue(payload["telegram"]["configured"])
        self.assertTrue(payload["settings"]["hevy_configured"])

    def test_signing_out_invalidates_the_cookie(self) -> None:
        self.sign_in()
        self.assertEqual(self.call("/api/bootstrap")[0], 200)
        self.call("/api/session", "DELETE")
        self.cookie = None
        self.assertEqual(self.call("/api/bootstrap", cookie=False)[0], 401)

    # -- resources ------------------------------------------------------

    def test_counter_lifecycle(self) -> None:
        self.sign_in()
        status, created = self.call(
            "/api/counters", "POST", {"name": "No sugar", "icon": "coffee", "date": DAY(24), "on_dash": True}
        )
        self.assertEqual(status, 201)
        counter_id = created["counter"]["id"]

        _, boot = self.call("/api/bootstrap")
        found = next(c for c in boot["counters"] if c["id"] == counter_id)
        self.assertEqual(found["days"], 24)

        status, reset = self.call(f"/api/counters/{counter_id}/reset", "POST")
        self.assertEqual((status, reset["counter"]["best"]), (200, 24))
        self.assertEqual(self.call(f"/api/counters/{counter_id}", "DELETE")[0], 200)
        self.assertEqual(self.call(f"/api/counters/{counter_id}", "DELETE")[0], 404)

    def test_habit_toggle_round_trip(self) -> None:
        self.sign_in()
        _, created = self.call("/api/habits", "POST", {"name": "Morning yoga"})
        habit_id = created["habit"]["id"]
        _, toggled = self.call(f"/api/habits/{habit_id}/toggle", "POST", {})
        self.assertTrue(toggled["checkin"]["done"])
        _, boot = self.call("/api/bootstrap")
        self.assertTrue(next(h for h in boot["habits"] if h["id"] == habit_id)["done"])

    def test_motto_round_trip(self) -> None:
        self.sign_in()
        status, created = self.call(
            "/api/mottos", "POST",
            {"text": "You'll never know the value of a moment, until it becomes a memory"},
        )
        self.assertEqual(status, 201)
        motto_id = created["motto"]["id"]

        _, boot = self.call("/api/bootstrap")
        self.assertIn(motto_id, [m["id"] for m in boot["mottos"]])

        status, edited = self.call(f"/api/mottos/{motto_id}", "PUT", {"text": "The obstacle is the way"})
        self.assertEqual((status, edited["motto"]["text"]), (200, "The obstacle is the way"))

        self.assertEqual(self.call(f"/api/mottos/{motto_id}", "DELETE")[0], 200)
        self.assertEqual(self.call(f"/api/mottos/{motto_id}", "DELETE")[0], 404)

    def test_blank_motto_is_rejected_by_the_api(self) -> None:
        self.sign_in()
        status, _ = self.call("/api/mottos", "POST", {"text": "  "})
        self.assertEqual(status, 400)

    def test_last_member_cannot_be_removed(self) -> None:
        self.sign_in()
        _, boot = self.call("/api/bootstrap")
        status, _ = self.call(f"/api/household/{boot['me']['id']}", "DELETE")
        self.assertIn(status, (409,))

    def test_cannot_change_another_members_pin(self) -> None:
        self.sign_in()
        status, other = self.call("/api/household", "POST", {"name": "Dan", "pin": "1111"})
        self.assertEqual(status, 201)
        status, _ = self.call(f"/api/household/{other['member']['id']}", "PUT", {"pin": "9999"})
        self.assertEqual(status, 403)

    # -- webhook --------------------------------------------------------

    def test_bootstrap_exposes_the_gym_week_start(self) -> None:
        # The payload is an explicit allow-list, so a setting the UI renders has
        # to be added to it or the control silently falls back to its default.
        self.sign_in()
        _, boot = self.call("/api/bootstrap")
        self.assertEqual(boot["settings"]["gym_week_start"], 6)
        self.call("/api/settings", "PUT", {"gym_week_start": 0})
        _, boot = self.call("/api/bootstrap")
        self.assertEqual(boot["settings"]["gym_week_start"], 0)

    def test_health_webhook_requires_the_key(self) -> None:
        self.sign_in()
        status, _ = self.call("/api/health-webhook?key=wrong", "POST", {}, cookie=False)
        self.assertEqual(status, 401)

    def test_health_webhook_ingests_steps(self) -> None:
        self.sign_in()
        _, boot = self.call("/api/bootstrap")
        path = boot["integrations"]["health_webhook_path"]
        status, payload = self.call(path, "POST", {
            "data": {"metrics": [
                {"name": "step_count", "units": "count",
                 "data": [{"date": f"{DAY(0)} 00:00:00 +0000", "qty": 8930}]},
            ]},
        }, cookie=False)
        self.assertEqual(status, 200)
        self.assertEqual(payload["written"]["steps"], 1)
        _, boot = self.call("/api/bootstrap")
        self.assertEqual(boot["steps"], 8930)

    def test_health_webhook_keeps_the_weigh_in_time(self) -> None:
        # Health Auto Export sends a full timestamp; only its date half was
        # being kept, leaving nothing to show for a reading taken today.
        self.sign_in()
        _, boot = self.call("/api/bootstrap")
        status, payload = self.call(boot["integrations"]["health_webhook_path"], "POST", {
            "data": {"metrics": [
                {"name": "weight_body_mass",
                 "data": [{"date": f"{DAY(0)} 08:22:15 +0000", "qty": 71.8}]},
            ]},
        }, cookie=False)
        self.assertEqual(status, 200)
        self.assertEqual(payload["written"]["weight"], 1)
        _, boot = self.call("/api/bootstrap")
        sample = boot["weight_samples"][-1]
        self.assertEqual(sample["date"], DAY(0))
        self.assertTrue(sample["at"].startswith(f"{DAY(0)}T08:22:15"), sample["at"])

    def test_zepp_life_import_requires_auth_and_merges_history(self) -> None:
        archive = zepp_zip(
            "time,weight,height,bmi,fatRate,bodyWaterRate,boneMass,metabolism,muscleRate,visceralFat\n"
            "2031-03-04 07:00:00+0000,70.2,171,24.0,20.1,54.8,3,1600,52.4,8\n"
        )
        self.assertEqual(
            self.call_raw("/api/import/zepp-life", archive, "application/zip", cookie=False)[0],
            401,
        )
        self.sign_in()
        status, payload = self.call_raw(
            "/api/import/zepp-life", archive, "application/zip"
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["rows"], 1)
        self.assertEqual(payload["days"], 1)
        self.assertEqual(payload["added"], 1)
        _, boot = self.call("/api/bootstrap")
        sample = next(item for item in boot["weight_samples"] if item["date"] == "2031-03-04")
        self.assertEqual(sample["weight"], 70.2)
        self.assertEqual(sample["fat"], 20.1)

    def test_bootstrap_returns_complete_weight_history(self) -> None:
        self.sign_in()
        _, boot = self.call("/api/bootstrap")
        start = date(2010, 1, 1)
        samples = [
            {"date": (start + timedelta(days=index)).isoformat(), "weight": 80 - index / 100}
            for index in range(150)
        ]
        self.store.merge_weight_samples(boot["me"]["id"], samples)
        _, refreshed = self.call("/api/bootstrap")
        dates = {sample["date"] for sample in refreshed["weight_samples"]}
        self.assertTrue({sample["date"] for sample in samples}.issubset(dates))

    # -- transport ------------------------------------------------------

    def test_malformed_bodies_are_rejected(self) -> None:
        self.sign_in()
        request = urllib.request.Request(
            self.base + "/api/counters", data=b"<<<", method="POST",
            headers={"Content-Type": "application/json", "Cookie": self.cookie},
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request)
        self.assertEqual(ctx.exception.code, 400)
        self.assertEqual(self.call("/api/counters", "POST", [1, 2])[0], 400)

    def test_unknown_endpoint_and_method(self) -> None:
        self.assertEqual(self.call("/api/nope", cookie=False)[0], 404)
        # A verb the route knows but does not allow.
        self.assertEqual(self.call("/api/bootstrap", "DELETE", {}, cookie=False)[0], 405)
        # A verb the server implements nowhere at all.
        self.assertEqual(self.call("/api/session", "PATCH", {}, cookie=False)[0], 501)

    def test_static_assets_are_served(self) -> None:
        for path in (
            "/",
            "/index.html",
            "/styles.css",
            "/app.js",
            "/icons.js",
            "/fonts/inter.woff2",
            "/manifest.webmanifest",
            "/icons/icon-180.png",
            "/icons/icon-192.png",
            "/icons/icon-512.png",
            "/icons/icon-maskable-512.png",
        ):
            with urllib.request.urlopen(self.base + path) as response:
                self.assertEqual(response.status, 200, path)
                self.assertTrue(response.read())

    def test_web_app_manifest_is_installable(self) -> None:
        # iOS drops the manifest unless it arrives as manifest+json, and the
        # URLs have to stay relative to survive the ingress token prefix.
        with urllib.request.urlopen(self.base + "/manifest.webmanifest") as response:
            self.assertEqual(response.headers.get_content_type(), "application/manifest+json")
            manifest = json.loads(response.read())
        self.assertEqual(manifest["display"], "standalone")
        self.assertEqual(manifest["start_url"], "./")
        for entry in manifest["icons"]:
            self.assertFalse(entry["src"].startswith("/"), entry["src"])
            with urllib.request.urlopen(f"{self.base}/{entry['src']}") as response:
                self.assertEqual(response.headers.get_content_type(), "image/png")

    def test_shell_declares_ios_web_app_metadata(self) -> None:
        with urllib.request.urlopen(self.base + "/index.html") as response:
            shell = response.read().decode()
        # `viewport-fit=cover` plus a translucent status bar is what made iOS
        # lay the standalone app out from the top of the screen while sizing
        # the viewport one status bar short, leaving a gap under the tab bar.
        # Letting iOS inset the web view itself is what keeps the bar flush.
        viewport = re.search(r'name="viewport"\s+content="([^"]*)"', shell)
        assert viewport is not None, shell
        self.assertNotIn("viewport-fit", viewport.group(1))
        self.assertIn("user-scalable=no", viewport.group(1))
        self.assertIn(
            'name="apple-mobile-web-app-status-bar-style" content="black"', shell
        )
        self.assertIn('name="apple-mobile-web-app-capable" content="yes"', shell)
        self.assertIn('rel="apple-touch-icon"', shell)
        self.assertIn('rel="manifest"', shell)

    def test_security_headers_are_set(self) -> None:
        with urllib.request.urlopen(self.base + "/") as response:
            self.assertEqual(response.headers.get("X-Content-Type-Options"), "nosniff")
            self.assertIn("default-src 'self'", response.headers.get("Content-Security-Policy", ""))

    def test_session_cookie_is_hardened(self) -> None:
        self.sign_in()
        self.assertIn("HttpOnly", self.raw_cookie or "")
        self.assertIn("SameSite=Lax", self.raw_cookie or "")
        self.assertIn("Path=/", self.raw_cookie or "")

    def raw_get(self, path: str) -> bytes:
        import socket

        conn = socket.create_connection(("127.0.0.1", self.httpd.server_address[1]), timeout=5)
        try:
            conn.sendall(f"GET {path} HTTP/1.1\r\nHost: t\r\nConnection: close\r\n\r\n".encode())
            chunks = []
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            conn.close()

    def test_path_traversal_cannot_escape_the_web_root(self) -> None:
        escapes = [
            "/../server.py", "/../../etc/passwd", "/../../../../etc/passwd",
            "/..%2f..%2fstorage.py", "/%2e%2e%2fserver.py", "/....//server.py",
            "/./../../app/storage.py", "/../momentum.json",
        ]
        leaks = [b"SCHEMA_VERSION", b"BaseHTTPRequestHandler", b"root:x:", b"pin_hash", b"session_secret"]
        for path in escapes:
            response = self.raw_get(path)
            for marker in leaks:
                self.assertNotIn(marker, response, f"{path} leaked {marker!r}")

    def test_oversized_body_is_refused_cleanly(self) -> None:
        """The client must receive 413, not a broken pipe.

        The server rejects on Content-Length before buffering, so it has to
        drain the rejected body or the reply races the still-uploading client.
        """
        self.sign_in()
        request = urllib.request.Request(
            self.base + "/api/journal",
            data=json.dumps({"text": "x" * (600 * 1024)}).encode(),
            method="POST",
            headers={"Content-Type": "application/json", "Cookie": self.cookie},
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request)
        self.assertEqual(ctx.exception.code, 413)
        self.assertIn("too large", json.loads(ctx.exception.read())["error"])

    def test_body_at_the_limit_is_accepted(self) -> None:
        self.sign_in()
        # Comfortably under 512 KiB once JSON-encoded, but far past any
        # ordinary entry — the cap must not clip normal use.
        status, _ = self.call("/api/journal", "POST", {"text": "y" * 7000})
        self.assertEqual(status, 201)


class SchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.store = Store(os.path.join(self.dir.name, "momentum.json"))
        self.user = self.store.add_user("Maya", "4821")
        self.scheduler = Scheduler(self.store, HomeAssistant(None), Settings())
        self.sent: list[tuple[str, str]] = []
        self.scheduler._notify = lambda user, text: (self.sent.append((user["name"], text)), True)[1]

    def test_milestone_fires_once_per_threshold(self) -> None:
        self.store.save_counter(self.user["id"], {"name": "No sugar", "date": DAY(30)}, None)
        self.assertEqual(self.scheduler.check_milestones(), 1)
        self.assertEqual(self.scheduler.check_milestones(), 0, "not announced twice")
        self.assertIn("30 days", self.sent[0][1])

    def test_no_milestone_on_an_ordinary_day(self) -> None:
        self.store.save_counter(self.user["id"], {"name": "No sugar", "date": DAY(31)}, None)
        self.assertEqual(self.scheduler.check_milestones(), 0)

    def test_milestone_text_escapes_html(self) -> None:
        self.store.save_counter(self.user["id"], {"name": "No <b>junk</b>", "date": DAY(7)}, None)
        self.scheduler.check_milestones()
        self.assertIn("&lt;b&gt;junk&lt;/b&gt;", self.sent[0][1])

    def test_reminder_only_fires_at_the_configured_minute(self) -> None:
        uid = self.user["id"]
        self.store.save_affirmation(uid, "Progress, not perfection.", None)
        self.store.update_user_settings(uid, {"reminder": {"on": True, "time": "00:00"}})
        now = time.strftime("%H:%M")
        self.store.update_user_settings(uid, {"reminder": {"on": True, "time": now}})
        self.assertEqual(self.scheduler.run_reminders(), 1)
        self.assertEqual(self.scheduler.run_reminders(), 0, "deduped within the same minute")

    def test_reminder_off_sends_nothing(self) -> None:
        uid = self.user["id"]
        self.store.save_affirmation(uid, "Progress.", None)
        self.store.update_user_settings(uid, {"reminder": {"on": False, "time": time.strftime("%H:%M")}})
        self.assertEqual(self.scheduler.run_reminders(), 0)

    def test_jobs_survive_a_failing_integration(self) -> None:
        def boom():
            raise RuntimeError("integration down")

        # The guard logs the traceback; silence it so the run stays readable.
        logging.getLogger("momentum.scheduler").setLevel(logging.CRITICAL)
        self.addCleanup(logging.getLogger("momentum.scheduler").setLevel, logging.NOTSET)
        self.scheduler._guard("weight", boom)  # must not raise

    def test_hourly_sync_keeps_the_reading_time_from_home_assistant(self) -> None:
        # The hourly sync is what feeds today's weight, so its `last_changed`
        # is the timestamp the dashboard's "Today 8:22" actually comes from.
        class FakeHomeAssistant:
            available = True

            @staticmethod
            def state(_entity):
                return {
                    "ok": True,
                    "value": 71.8,
                    "last_changed": "2026-07-31T08:22:11+01:00",
                }

        uid = self.user["id"]
        self.store.update_user_settings(uid, {"entities": {"weight": "sensor.mi_scale_weight"}})
        scheduler = Scheduler(self.store, FakeHomeAssistant(), Settings())
        self.assertEqual(scheduler.sync_weight(), 1)
        sample = self.store.user(uid)["weight_samples"][-1]
        self.assertEqual(sample["weight"], 71.8)
        self.assertEqual(sample["at"], "2026-07-31T08:22:11+01:00")

    def test_bodymiscale_backfill_uses_current_state_when_recorder_is_empty(self) -> None:
        class FakeHomeAssistant:
            available = True

            @staticmethod
            def bodymiscale_history_result(_entity):
                return [], "Recorder has no history for bodymiscale.andrey"

            @staticmethod
            def bodymiscale_state(_entity):
                return {
                    "date": "2026-07-30",
                    "weight": 75.2,
                    "fat": 23.1,
                    "muscle": None,
                    "bmi": 25.1,
                    "water": None,
                    "visceral": None,
                }

            @staticmethod
            def history_result(_entity):
                return [], None

        uid = self.user["id"]
        self.store.update_user_settings(
            uid, {"entities": {"bodymiscale": "bodymiscale.andrey"}}
        )
        scheduler = Scheduler(self.store, FakeHomeAssistant(), Settings())
        result = scheduler.backfill_weight(uid)
        self.assertEqual(result["found"], 1)
        self.assertEqual(result["added"], 1)
        self.assertIn("Recorder has no history", result["errors"][0])
        sample = self.store.user(uid)["weight_samples"][0]
        self.assertEqual(sample["weight"], 75.2)
        self.assertEqual(sample["fat"], 23.1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
