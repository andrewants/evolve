"""Self Improvement Dashboard — HTTP server.

Serves the single-page dashboard plus a small JSON API. Home Assistant
reaches it through ingress, which terminates auth upstream and rewrites the
request path, so every URL the page uses is relative.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import posixpath
import re
import sys
import urllib.parse
from datetime import datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from ha import HomeAssistant
from storage import Store

LOGGER = logging.getLogger("evolve")

MAX_BODY_BYTES = 256 * 1024
CACHE_BUSTER = os.environ.get("EVOLVE_BUILD", "1.0.0")

BASHIO_TO_PYTHON_LEVEL = {
    "trace": logging.DEBUG,
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "notice": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "fatal": logging.CRITICAL,
}


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------


def load_sensors() -> list[dict[str, str]]:
    """Parse the `sensors` add-on option, which bashio hands us as JSON."""
    raw = (os.environ.get("EVOLVE_SENSORS") or "").strip()
    if not raw or raw in ("null", "[]"):
        return []
    try:
        parsed = json.loads(raw)
    except ValueError:
        LOGGER.warning("Ignoring malformed `sensors` option: %r", raw)
        return []
    if not isinstance(parsed, list):
        return []

    sensors = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        entity = str(item.get("entity") or "").strip()
        if not entity:
            continue
        sensors.append(
            {
                "entity": entity,
                "label": str(item.get("label") or "").strip(),
                "icon": str(item.get("icon") or "").strip(),
            }
        )
    return sensors


class Settings:
    def __init__(self) -> None:
        self.port = int(os.environ.get("EVOLVE_PORT", "8099"))
        self.data_dir = os.environ.get("EVOLVE_DATA_DIR", "/data")
        self.www_dir = os.path.abspath(
            os.environ.get("EVOLVE_WWW_DIR", os.path.join(os.path.dirname(__file__), "www"))
        )
        self.week_starts_on = os.environ.get("EVOLVE_WEEK_STARTS_ON", "monday")
        self.timezone = os.environ.get("EVOLVE_TIMEZONE", "")
        self.log_level = os.environ.get("EVOLVE_LOG_LEVEL", "info").lower()
        self.sensors = load_sensors()

        try:
            self.day_rollover_hour = max(0, min(12, int(os.environ.get("EVOLVE_DAY_ROLLOVER_HOUR", "4"))))
        except ValueError:
            self.day_rollover_hour = 4

    def today(self) -> str:
        """The dashboard's 'today', honouring the late-night rollover hour."""
        now = datetime.now()
        if now.hour < self.day_rollover_hour:
            return (now.date() - timedelta(days=1)).isoformat()
        return now.date().isoformat()


# ---------------------------------------------------------------------------
# routing
# ---------------------------------------------------------------------------


class Route:
    def __init__(self, method: str, pattern: str, handler: Callable) -> None:
        self.method = method
        self.regex = re.compile(f"^{pattern}$")
        self.handler = handler


class Api:
    def __init__(self, settings: Settings, store: Store, hass: HomeAssistant) -> None:
        self.settings = settings
        self.store = store
        self.hass = hass
        self.routes = [
            Route("GET", r"/api/bootstrap", self.bootstrap),
            Route("GET", r"/api/sensors", self.sensors),
            Route("POST", r"/api/habits", self.create_habit),
            Route("PUT", r"/api/habits/([\w-]+)", self.update_habit),
            Route("DELETE", r"/api/habits/([\w-]+)", self.delete_habit),
            Route("POST", r"/api/habits/([\w-]+)/toggle", self.toggle_habit),
            Route("POST", r"/api/goals", self.create_goal),
            Route("PUT", r"/api/goals/([\w-]+)", self.update_goal),
            Route("DELETE", r"/api/goals/([\w-]+)", self.delete_goal),
            Route("POST", r"/api/journal", self.create_entry),
            Route("DELETE", r"/api/journal/([\w-]+)", self.delete_entry),
        ]

    def dispatch(self, method: str, path: str, body: dict[str, Any]) -> tuple[int, Any]:
        matched_path = False
        for route in self.routes:
            match = route.regex.match(path)
            if not match:
                continue
            matched_path = True
            if route.method != method:
                continue
            return route.handler(body, *match.groups())
        if matched_path:
            return HTTPStatus.METHOD_NOT_ALLOWED, {"error": "method not allowed"}
        return HTTPStatus.NOT_FOUND, {"error": "unknown endpoint"}

    # -- handlers -------------------------------------------------------

    def bootstrap(self, _body: dict[str, Any]) -> tuple[int, Any]:
        return HTTPStatus.OK, {
            "state": self.store.snapshot(),
            "config": {
                "today": self.settings.today(),
                "week_starts_on": self.settings.week_starts_on,
                "day_rollover_hour": self.settings.day_rollover_hour,
                "timezone": self.settings.timezone,
                "sensors": self.settings.sensors,
                "ha_available": self.hass.available,
                "version": CACHE_BUSTER,
            },
        }

    def sensors(self, _body: dict[str, Any]) -> tuple[int, Any]:
        readings = []
        for sensor in self.settings.sensors:
            reading = self.hass.state(sensor["entity"])
            reading["label"] = sensor["label"] or reading["friendly_name"]
            reading["icon"] = sensor["icon"]
            readings.append(reading)
        return HTTPStatus.OK, {"sensors": readings}

    def create_habit(self, body: dict[str, Any]) -> tuple[int, Any]:
        return HTTPStatus.CREATED, {"habit": self.store.add_habit(body)}

    def update_habit(self, body: dict[str, Any], habit_id: str) -> tuple[int, Any]:
        habit = self.store.update_habit(habit_id, body)
        return _found(habit, "habit")

    def delete_habit(self, _body: dict[str, Any], habit_id: str) -> tuple[int, Any]:
        return _deleted(self.store.delete_habit(habit_id))

    def toggle_habit(self, body: dict[str, Any], habit_id: str) -> tuple[int, Any]:
        day = body.get("date") or self.settings.today()
        try:
            result = self.store.toggle_checkin(habit_id, day)
        except ValueError as error:
            return HTTPStatus.BAD_REQUEST, {"error": str(error)}
        if result is None:
            return HTTPStatus.NOT_FOUND, {"error": "habit not found"}
        return HTTPStatus.OK, result

    def create_goal(self, body: dict[str, Any]) -> tuple[int, Any]:
        return HTTPStatus.CREATED, {"goal": self.store.add_goal(body)}

    def update_goal(self, body: dict[str, Any], goal_id: str) -> tuple[int, Any]:
        return _found(self.store.update_goal(goal_id, body), "goal")

    def delete_goal(self, _body: dict[str, Any], goal_id: str) -> tuple[int, Any]:
        return _deleted(self.store.delete_goal(goal_id))

    def create_entry(self, body: dict[str, Any]) -> tuple[int, Any]:
        try:
            entry = self.store.add_entry(body)
        except ValueError as error:
            return HTTPStatus.BAD_REQUEST, {"error": str(error)}
        return HTTPStatus.CREATED, {"entry": entry}

    def delete_entry(self, _body: dict[str, Any], entry_id: str) -> tuple[int, Any]:
        return _deleted(self.store.delete_entry(entry_id))


def _found(item: dict[str, Any] | None, key: str) -> tuple[int, Any]:
    if item is None:
        return HTTPStatus.NOT_FOUND, {"error": f"{key} not found"}
    return HTTPStatus.OK, {key: item}


def _deleted(ok: bool) -> tuple[int, Any]:
    if not ok:
        return HTTPStatus.NOT_FOUND, {"error": "not found"}
    return HTTPStatus.OK, {"deleted": True}


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "EvolveDashboard/1.0"
    protocol_version = "HTTP/1.1"

    api: Api
    settings: Settings

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        path = self._path()
        if path.startswith("/api/"):
            self._handle_api("GET", path)
        else:
            self._serve_static(path, include_body=True)

    def do_HEAD(self) -> None:  # noqa: N802
        path = self._path()
        if path.startswith("/api/"):
            self.send_error(HTTPStatus.METHOD_NOT_ALLOWED)
        else:
            self._serve_static(path, include_body=False)

    def do_POST(self) -> None:  # noqa: N802
        self._handle_api("POST", self._path())

    def do_PUT(self) -> None:  # noqa: N802
        self._handle_api("PUT", self._path())

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle_api("DELETE", self._path())

    # -- helpers --------------------------------------------------------

    def _path(self) -> str:
        return urllib.parse.urlparse(self.path).path

    def _handle_api(self, method: str, path: str) -> None:
        if not path.startswith("/api/"):
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "unknown endpoint"})
            return
        try:
            body = self._read_json_body()
        except ValueError as error:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return

        try:
            status, payload = self.api.dispatch(method, path, body)
        except Exception:  # pragma: no cover - defensive
            LOGGER.exception("Unhandled error for %s %s", method, path)
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal error"})
            return

        self._send_json(status, payload)

    def _read_json_body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ValueError("invalid Content-Length") from None
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            raise ValueError("request body too large")
        raw = self.rfile.read(length)
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ValueError("body must be valid JSON") from None
        if not isinstance(parsed, dict):
            raise ValueError("body must be a JSON object")
        return parsed

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, path: str, *, include_body: bool) -> None:
        target = self._resolve(path)
        if target is None:
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return

        try:
            with open(target, "rb") as handle:
                body = handle.read()
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return

        content_type, _ = mimetypes.guess_type(target)
        if content_type and content_type.startswith("text/"):
            content_type = f"{content_type}; charset=utf-8"

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        # index.html must not be cached or an add-on update would keep
        # serving the previous shell; fingerprinted assets can be cached.
        if target.endswith("index.html"):
            self.send_header("Cache-Control", "no-store")
        else:
            self.send_header("Cache-Control", "public, max-age=300")
        self.end_headers()
        if include_body:
            self.wfile.write(body)

    def _resolve(self, path: str) -> str | None:
        """Map a request path to a file inside the www directory."""
        clean = posixpath.normpath(urllib.parse.unquote(path))
        if clean in ("/", "."):
            clean = "/index.html"
        # normpath collapses traversal, but a leading '..' can survive.
        if clean.startswith(".."):
            return None

        root = self.settings.www_dir
        candidate = os.path.abspath(os.path.join(root, clean.lstrip("/")))
        if candidate != root and not candidate.startswith(root + os.sep):
            return None
        if os.path.isfile(candidate):
            return candidate

        # Unknown non-asset paths fall back to the SPA shell so ingress deep
        # links keep working.
        if not os.path.splitext(candidate)[1]:
            return os.path.join(root, "index.html")
        return None

    def log_message(self, fmt: str, *args: Any) -> None:
        LOGGER.debug("%s - %s", self.address_string(), fmt % args)


def main() -> int:
    settings = Settings()
    logging.basicConfig(
        level=BASHIO_TO_PYTHON_LEVEL.get(settings.log_level, logging.INFO),
        format="[%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )

    if not os.path.isdir(settings.www_dir):
        LOGGER.error("Web root %s does not exist", settings.www_dir)
        return 1

    store = Store(os.path.join(settings.data_dir, "dashboard.json"))
    hass = HomeAssistant(os.environ.get("EVOLVE_SUPERVISOR_TOKEN"))

    Handler.api = Api(settings, store, hass)
    Handler.settings = settings

    httpd = ThreadingHTTPServer(("0.0.0.0", settings.port), Handler)
    httpd.daemon_threads = True

    LOGGER.info(
        "Dashboard ready on :%s (today=%s, sensors=%d, ha_api=%s)",
        settings.port,
        settings.today(),
        len(settings.sensors),
        hass.available,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        LOGGER.info("Shutting down")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
