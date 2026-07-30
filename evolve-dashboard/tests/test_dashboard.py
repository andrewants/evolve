"""Tests for the dashboard store and HTTP layer.

Stdlib only, so they run inside the add-on image as well as on a dev box:

    python3 -m unittest discover -s evolve-dashboard/tests -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from ha import HomeAssistant  # noqa: E402
from server import Api, Handler, Settings  # noqa: E402
from storage import Store  # noqa: E402


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "dashboard.json")
        self.store = Store(self.path)

    def test_seeds_starter_habits_on_first_run(self) -> None:
        self.assertTrue(self.store.snapshot()["habits"])

    def test_snapshot_is_a_copy(self) -> None:
        snapshot = self.store.snapshot()
        snapshot["habits"].clear()
        self.assertTrue(self.store.snapshot()["habits"])

    def test_toggle_is_idempotent_per_day(self) -> None:
        habit = self.store.add_habit({"name": "Stretch"})
        self.assertTrue(self.store.toggle_checkin(habit["id"], "2026-07-30")["done"])
        self.assertFalse(self.store.toggle_checkin(habit["id"], "2026-07-30")["done"])
        self.assertTrue(self.store.toggle_checkin(habit["id"], "2026-07-30")["done"])
        self.assertEqual(self.store.snapshot()["checkins"][habit["id"]], ["2026-07-30"])

    def test_toggle_rejects_bad_dates(self) -> None:
        habit = self.store.add_habit({"name": "Stretch"})
        with self.assertRaises(ValueError):
            self.store.toggle_checkin(habit["id"], "30-07-2026")

    def test_toggle_unknown_habit_returns_none(self) -> None:
        self.assertIsNone(self.store.toggle_checkin("missing", "2026-07-30"))

    def test_deleting_a_habit_drops_its_history(self) -> None:
        habit = self.store.add_habit({"name": "Stretch"})
        self.store.toggle_checkin(habit["id"], "2026-07-30")
        self.assertTrue(self.store.delete_habit(habit["id"]))
        self.assertNotIn(habit["id"], self.store.snapshot()["checkins"])
        self.assertFalse(self.store.delete_habit(habit["id"]))

    def test_values_are_clamped_and_truncated(self) -> None:
        habit = self.store.add_habit({"name": "x" * 500, "target_per_week": 99})
        self.assertEqual(len(habit["name"]), 80)
        self.assertEqual(habit["target_per_week"], 7)

        goal = self.store.add_goal({"title": "Save", "target": "not a number", "current": -5})
        self.assertEqual(goal["target"], 100.0)
        self.assertEqual(goal["current"], 0.0)

    def test_state_survives_a_reopen(self) -> None:
        habit = self.store.add_habit({"name": "Journal"})
        self.store.toggle_checkin(habit["id"], "2026-07-29")
        reopened = Store(self.path)
        self.assertEqual(reopened.snapshot()["checkins"][habit["id"]], ["2026-07-29"])

    def test_corrupt_file_is_quarantined_not_fatal(self) -> None:
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        recovered = Store(self.path)
        self.assertTrue(recovered.snapshot()["habits"])
        self.assertTrue(os.path.exists(f"{self.path}.corrupt"))

    def test_journal_is_capped(self) -> None:
        for i in range(510):
            self.store.add_entry({"text": str(i), "date": "2026-07-30"})
        self.assertEqual(len(self.store.snapshot()["journal"]), 500)

    def test_concurrent_toggles_do_not_lose_writes(self) -> None:
        habits = [self.store.add_habit({"name": f"H{i}"}) for i in range(8)]

        def work(habit_id: str) -> None:
            for day in range(1, 20):
                self.store.toggle_checkin(habit_id, f"2026-07-{day:02d}")

        threads = [threading.Thread(target=work, args=(h["id"],)) for h in habits]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        checkins = self.store.snapshot()["checkins"]
        for habit in habits:
            self.assertEqual(len(checkins[habit["id"]]), 19)


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dir = tempfile.TemporaryDirectory()
        settings = Settings()
        settings.data_dir = cls.dir.name
        settings.www_dir = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "app", "www")
        )
        settings.sensors = []

        Handler.settings = settings
        Handler.api = Api(settings, Store(os.path.join(cls.dir.name, "dashboard.json")), HomeAssistant(None))

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

    def call(self, path: str, method: str = "GET", body=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            self.base + path, data=data, method=method, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            raw = error.read()
            try:
                return error.code, json.loads(raw)
            except ValueError:
                return error.code, {"raw": raw[:80].decode(errors="replace")}

    def test_bootstrap_shape(self) -> None:
        status, payload = self.call("/api/bootstrap")
        self.assertEqual(status, 200)
        self.assertIn("habits", payload["state"])
        self.assertIn("today", payload["config"])

    def test_unknown_endpoint_and_method(self) -> None:
        self.assertEqual(self.call("/api/nope")[0], 404)
        self.assertEqual(self.call("/api/bootstrap", "DELETE")[0], 405)

    def test_malformed_bodies_are_rejected(self) -> None:
        request = urllib.request.Request(
            self.base + "/api/habits", data=b"<<<", method="POST",
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request)
        self.assertEqual(ctx.exception.code, 400)

        self.assertEqual(self.call("/api/habits", "POST", [1, 2])[0], 400)

    def test_habit_crud_round_trip(self) -> None:
        status, created = self.call("/api/habits", "POST", {"name": "Walk", "target_per_week": 4})
        self.assertEqual(status, 201)
        habit_id = created["habit"]["id"]

        status, toggled = self.call(f"/api/habits/{habit_id}/toggle", "POST", {"date": "2026-07-30"})
        self.assertEqual((status, toggled["done"]), (200, True))

        status, updated = self.call(f"/api/habits/{habit_id}", "PUT", {"name": "Walk further"})
        self.assertEqual((status, updated["habit"]["name"]), (200, "Walk further"))

        self.assertEqual(self.call(f"/api/habits/{habit_id}", "DELETE")[0], 200)
        self.assertEqual(self.call(f"/api/habits/{habit_id}", "DELETE")[0], 404)

    def test_goal_and_journal_round_trip(self) -> None:
        _, created = self.call("/api/goals", "POST", {"title": "Sleep 8h", "target": 30})
        goal_id = created["goal"]["id"]
        _, updated = self.call(f"/api/goals/{goal_id}", "PUT", {"current": 12, "done": True})
        self.assertEqual(updated["goal"]["current"], 12)
        self.assertTrue(updated["goal"]["done"])
        self.assertEqual(self.call(f"/api/goals/{goal_id}", "DELETE")[0], 200)

        _, entry = self.call("/api/journal", "POST", {"text": "ok", "mood": 9, "date": "2026-07-30"})
        self.assertEqual(entry["entry"]["mood"], 5)  # clamped
        self.assertEqual(self.call(f"/api/journal/{entry['entry']['id']}", "DELETE")[0], 200)

    def test_journal_rejects_bad_date(self) -> None:
        self.assertEqual(self.call("/api/journal", "POST", {"date": "yesterday"})[0], 400)

    def test_static_assets_are_served(self) -> None:
        for path in ("/", "/index.html", "/styles.css", "/app.js"):
            with urllib.request.urlopen(self.base + path) as response:
                self.assertEqual(response.status, 200, path)
                self.assertTrue(response.read())

    def test_unknown_extensionless_path_falls_back_to_the_shell(self) -> None:
        with urllib.request.urlopen(self.base + "/goals") as response:
            self.assertIn(b"<!doctype html>", response.read()[:40].lower())

    def raw_get(self, path: str) -> bytes:
        """Send a request line verbatim; urllib would normalise the path first."""
        import socket

        conn = socket.create_connection(("127.0.0.1", self.httpd.server_address[1]), timeout=5)
        try:
            conn.sendall(
                f"GET {path} HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n".encode()
            )
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
            "/../server.py",
            "/../../etc/passwd",
            "/../../../../etc/passwd",
            "/..%2f..%2fstorage.py",
            "/%2e%2e%2fserver.py",
            "/%2e%2e/%2e%2e/etc/shadow",
            "/....//server.py",
            "/./../../app/storage.py",
        ]
        # Markers that only appear in files outside the web root.
        leaks = [b"SCHEMA_VERSION", b"BaseHTTPRequestHandler", b"root:x:", b"/bin/sh"]
        for path in escapes:
            response = self.raw_get(path)
            for marker in leaks:
                self.assertNotIn(marker, response, f"{path} leaked {marker!r}")

    def test_oversized_body_is_refused(self) -> None:
        request = urllib.request.Request(
            self.base + "/api/habits",
            data=json.dumps({"name": "x" * (300 * 1024)}).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request)
        self.assertEqual(ctx.exception.code, 400)


class HomeAssistantTests(unittest.TestCase):
    def test_without_a_token_every_entity_reads_unavailable(self) -> None:
        client = HomeAssistant(None)
        self.assertFalse(client.available)
        reading = client.state("sensor.steps")
        self.assertFalse(reading["ok"])
        self.assertIsNone(reading["state"])

    def test_reads_state_from_the_core_api(self) -> None:
        import ha
        from http.server import BaseHTTPRequestHandler

        seen: list[str] = []

        class Core(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                seen.append(self.headers.get("Authorization", ""))
                if self.path.endswith("missing"):
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = json.dumps(
                    {
                        "state": "8421",
                        "attributes": {"unit_of_measurement": "steps", "friendly_name": "Steps"},
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Core)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        original = ha.CORE_API
        ha.CORE_API = f"http://127.0.0.1:{httpd.server_address[1]}/api"
        try:
            client = HomeAssistant("test-token")
            reading = client.state("sensor.steps_today")
            self.assertTrue(reading["ok"])
            self.assertEqual(reading["state"], "8421")
            self.assertEqual(reading["unit"], "steps")
            self.assertEqual(reading["friendly_name"], "Steps")
            self.assertEqual(seen, ["Bearer test-token"])

            # Repeat reads are served from the cache, not the core API.
            client.state("sensor.steps_today")
            self.assertEqual(len(seen), 1)

            missing = client.state("sensor.missing")
            self.assertFalse(missing["ok"])
            self.assertEqual(missing["reason"], "not found")
        finally:
            ha.CORE_API = original
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
