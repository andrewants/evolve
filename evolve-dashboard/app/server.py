"""Momentum — HTTP server.

Serves the single-page app plus its JSON API. Two front doors are expected:
Home Assistant ingress (which rewrites the path prefix) and a Cloudflare
tunnel (which does not authenticate anyone). Because of the second, the app
authenticates on its own — every API route below /api/session requires a
signed session cookie earned with a member's PIN.
"""

from __future__ import annotations

import base64
import hmac
import json
import logging
import mimetypes
import os
import posixpath
import re
import secrets
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timedelta
from hashlib import sha256
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from backup import create_data_backup
from gym import gym_summary
from integrations import Hevy, HomeAssistant, Telegram, parse_iso_day
from scheduler import Scheduler
from counter_icons import COUNTER_ICON_CATALOG
from storage import METRICS, Store, days_since, today_iso
from zepp import parse_zepp_life_export

LOGGER = logging.getLogger("momentum")

MAX_BODY_BYTES = 512 * 1024
MAX_IMPORT_BYTES = 128 * 1024 * 1024
# An oversized body is drained (up to this much) before replying, so the
# client sees the error instead of a broken pipe. Past it, the connection
# is dropped rather than reading an unbounded upload.
MAX_DRAIN_BYTES = 8 * 1024 * 1024
SESSION_COOKIE = "momentum_session"
APP_VERSION = "2.10.0"

BASHIO_TO_PYTHON_LEVEL = {
    "trace": logging.DEBUG,
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "notice": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "fatal": logging.CRITICAL,
}


class Settings:
    def __init__(self) -> None:
        self.port = int(os.environ.get("MOMENTUM_PORT", "8099"))
        self.data_dir = os.environ.get("MOMENTUM_DATA_DIR", "/data")
        self.www_dir = os.path.abspath(
            os.environ.get("MOMENTUM_WWW_DIR", os.path.join(os.path.dirname(__file__), "www"))
        )
        self.log_level = os.environ.get("MOMENTUM_LOG_LEVEL", "info").lower()
        self.timezone = os.environ.get("MOMENTUM_TIMEZONE", "UTC")
        self.session_days = _env_int("MOMENTUM_SESSION_DAYS", 14, 1, 365)
        self.day_rollover_hour = _env_int("MOMENTUM_DAY_ROLLOVER_HOUR", 4, 0, 12)

    def today(self) -> str:
        now = datetime.now()
        if now.hour < self.day_rollover_hour:
            return (now.date() - timedelta(days=1)).isoformat()
        return now.date().isoformat()


def _env_int(name: str, fallback: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(os.environ.get(name, fallback))))
    except (TypeError, ValueError):
        return fallback


# ---------------------------------------------------------------------------
# sessions and brute-force protection
# ---------------------------------------------------------------------------


class Sessions:
    """Stateless signed cookies: base64(payload).hmac."""

    def __init__(self, secret: bytes, ttl_days: int) -> None:
        self._secret = secret
        self._ttl = ttl_days * 86400

    def issue(self, user_id: str) -> str:
        payload = json.dumps(
            {"uid": user_id, "exp": int(time.time()) + self._ttl, "n": secrets.token_hex(4)},
            separators=(",", ":"),
        ).encode()
        body = base64.urlsafe_b64encode(payload).rstrip(b"=")
        return f"{body.decode()}.{self._sign(body)}"

    def verify(self, token: str | None) -> str | None:
        if not token or "." not in token:
            return None
        body, _, signature = token.rpartition(".")
        if not hmac.compare_digest(self._sign(body.encode()), signature):
            return None
        try:
            padding = "=" * (-len(body) % 4)
            payload = json.loads(base64.urlsafe_b64decode(body + padding))
        except (ValueError, TypeError):
            return None
        if int(payload.get("exp", 0)) < time.time():
            return None
        return payload.get("uid")

    def _sign(self, body: bytes) -> str:
        return hmac.new(self._secret, body, sha256).hexdigest()[:32]


class Throttle:
    """Escalating lockout for PIN attempts.

    A 4-digit PIN is only 10,000 combinations, which a tunnel-exposed app
    would give away in minutes without this.
    """

    BASE_DELAY = 2.0
    FREE_ATTEMPTS = 4
    MAX_DELAY = 900.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._failures: dict[str, tuple[int, float]] = {}

    def retry_after(self, key: str) -> float:
        with self._lock:
            count, last = self._failures.get(key, (0, 0.0))
            if count <= self.FREE_ATTEMPTS:
                return 0.0
            delay = min(self.MAX_DELAY, self.BASE_DELAY * (2 ** (count - self.FREE_ATTEMPTS - 1)))
            remaining = (last + delay) - time.monotonic()
            return max(0.0, remaining)

    def record_failure(self, key: str) -> None:
        with self._lock:
            count, _ = self._failures.get(key, (0, 0.0))
            self._failures[key] = (count + 1, time.monotonic())
            if len(self._failures) > 2048:
                self._failures.clear()

    def clear(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)


# ---------------------------------------------------------------------------
# routing
# ---------------------------------------------------------------------------


class Route:
    def __init__(self, method: str, pattern: str, handler: Callable, public: bool = False) -> None:
        self.method = method
        self.regex = re.compile(f"^{pattern}$")
        self.handler = handler
        self.public = public


class Api:
    def __init__(self, settings: Settings, store: Store, hass: HomeAssistant, scheduler: Scheduler) -> None:
        self.settings = settings
        self.store = store
        self.hass = hass
        self.scheduler = scheduler
        self.sessions = Sessions(store.session_secret, settings.session_days)
        self.throttle = Throttle()

        self.routes = [
            Route("GET", r"/api/session", self.session_info, public=True),
            Route("POST", r"/api/session", self.login, public=True),
            Route("DELETE", r"/api/session", self.logout, public=True),
            Route("POST", r"/api/setup", self.first_run_setup, public=True),
            Route("POST", r"/api/health-webhook", self.health_webhook, public=True),

            Route("GET", r"/api/bootstrap", self.bootstrap),

            Route("POST", r"/api/counters", self.create_counter),
            Route("PUT", r"/api/counters/([\w-]+)", self.update_counter),
            Route("DELETE", r"/api/counters/([\w-]+)", self.delete_counter),
            Route("POST", r"/api/counters/([\w-]+)/reset", self.reset_counter),
            Route("POST", r"/api/counters/([\w-]+)/dash", self.toggle_counter_dash),

            Route("POST", r"/api/affirmations", self.create_affirmation),
            Route("PUT", r"/api/affirmations/([\w-]+)", self.update_affirmation),
            Route("DELETE", r"/api/affirmations/([\w-]+)", self.delete_affirmation),
            Route("POST", r"/api/affirmations/([\w-]+)/pin", self.pin_affirmation),
            Route("POST", r"/api/mottos", self.create_motto),
            Route("PUT", r"/api/mottos/([\w-]+)", self.update_motto),
            Route("DELETE", r"/api/mottos/([\w-]+)", self.delete_motto),

            Route("POST", r"/api/habits", self.create_habit),
            Route("PUT", r"/api/habits/([\w-]+)", self.update_habit),
            Route("DELETE", r"/api/habits/([\w-]+)", self.delete_habit),
            Route("POST", r"/api/habits/([\w-]+)/toggle", self.toggle_habit),

            Route("POST", r"/api/journal", self.create_journal),
            Route("DELETE", r"/api/journal/([\w-]+)", self.delete_journal),

            Route("PUT", r"/api/settings", self.update_settings),
            Route("PUT", r"/api/telegram", self.update_telegram),
            Route("POST", r"/api/telegram/test", self.telegram_test),
            Route("POST", r"/api/telegram/backup", self.telegram_backup),
            Route("POST", r"/api/bot-users/([\w-]+)/approve", self.approve_bot_user),
            Route("DELETE", r"/api/bot-users/([\w-]+)", self.remove_bot_user),

            Route("POST", r"/api/household", self.add_member),
            Route("PUT", r"/api/household/([\w-]+)", self.update_member),
            Route("DELETE", r"/api/household/([\w-]+)", self.delete_member),

            Route("POST", r"/api/sync/weight", self.sync_weight),
            Route("POST", r"/api/sync/hevy", self.sync_hevy),
            Route("POST", r"/api/sync/backfill", self.backfill),
            Route("POST", r"/api/import/zepp-life", self.import_zepp_life),
        ]

    # -- dispatch -------------------------------------------------------

    def dispatch(self, method: str, path: str, body: dict, ctx: "RequestContext") -> tuple[int, Any]:
        matched = False
        for route in self.routes:
            match = route.regex.match(path)
            if not match:
                continue
            matched = True
            if route.method != method:
                continue
            if not route.public and ctx.user_id is None:
                return HTTPStatus.UNAUTHORIZED, {"error": "not signed in"}
            try:
                return route.handler(body, ctx, *match.groups())
            except ValueError as error:
                return HTTPStatus.BAD_REQUEST, {"error": str(error)}
        if matched:
            return HTTPStatus.METHOD_NOT_ALLOWED, {"error": "method not allowed"}
        return HTTPStatus.NOT_FOUND, {"error": "unknown endpoint"}

    # -- auth -----------------------------------------------------------

    def session_info(self, _body: dict, ctx: "RequestContext") -> tuple[int, Any]:
        users = self.store.users()
        if not users:
            return HTTPStatus.OK, {"setup_required": True, "users": [], "signed_in": None}
        return HTTPStatus.OK, {
            "setup_required": False,
            "signed_in": ctx.user_id,
            "users": [
                {
                    "id": u["id"],
                    "name": u["name"],
                    "initials": u["name"][:1].upper(),
                    "meta": f"{len(u['counters'])} counters · {len(u['habits'])} habits",
                    "has_pin": bool(u["pin_hash"]),
                }
                for u in users
            ],
        }

    def login(self, body: dict, ctx: "RequestContext") -> tuple[int, Any]:
        user_id = str(body.get("user_id") or "")
        pin = str(body.get("pin") or "")
        # Throttle per account and per source, so neither one account nor
        # one client can be hammered.
        keys = [f"u:{user_id}", f"ip:{ctx.client_ip}"]
        wait = max(self.throttle.retry_after(key) for key in keys)
        if wait > 0:
            return HTTPStatus.TOO_MANY_REQUESTS, {
                "error": f"Too many attempts. Try again in {int(wait) + 1}s.",
                "retry_after": int(wait) + 1,
            }

        if not self.store.check_pin(user_id, pin):
            for key in keys:
                self.throttle.record_failure(key)
            return HTTPStatus.UNAUTHORIZED, {"error": "Wrong PIN"}

        for key in keys:
            self.throttle.clear(key)
        user = self.store.user(user_id)
        ctx.set_cookie = self.sessions.issue(user_id)
        return HTTPStatus.OK, {"ok": True, "user": {"id": user["id"], "name": user["name"]}}

    def logout(self, _body: dict, ctx: "RequestContext") -> tuple[int, Any]:
        ctx.clear_cookie = True
        return HTTPStatus.OK, {"ok": True}

    def first_run_setup(self, body: dict, ctx: "RequestContext") -> tuple[int, Any]:
        """Create the first member. Only possible while none exist."""
        if self.store.users():
            return HTTPStatus.CONFLICT, {"error": "already set up"}
        name = str(body.get("name") or "").strip()
        pin = str(body.get("pin") or "")
        if not name:
            raise ValueError("name is required")
        _validate_pin(pin)
        user = self.store.add_user(name, pin)
        _seed_starter_content(self.store, user["id"])
        ctx.set_cookie = self.sessions.issue(user["id"])
        return HTTPStatus.CREATED, {"ok": True, "user": {"id": user["id"], "name": user["name"]}}

    # -- bootstrap ------------------------------------------------------

    def bootstrap(self, _body: dict, ctx: "RequestContext") -> tuple[int, Any]:
        user = self.store.user(ctx.user_id)
        if user is None:
            ctx.clear_cookie = True
            return HTTPStatus.UNAUTHORIZED, {"error": "not signed in"}

        today = self.settings.today()
        telegram = self.store.telegram_config()
        settings = user["settings"]
        samples = user["weight_samples"]

        return HTTPStatus.OK, {
            "me": {
                "id": user["id"],
                "name": user["name"],
                "initials": user["name"][:1].upper(),
            },
            "today": today,
            "counters": [
                {
                    **counter,
                    "days": days_since(counter["date"]),
                    "best": counter.get("best", 0),
                }
                for counter in user["counters"]
            ],
            "affirmations": user["affirmations"],
            "mottos": user["mottos"],
            "habits": [
                {**habit, "done": today in user["checkins"].get(habit["id"], [])}
                for habit in user["habits"]
            ],
            "journal": user["journal"][:60],
            "weight_samples": samples,
            "workouts": user["workouts"],
            "gym": gym_summary(
                user["workouts"],
                settings["weekly_gym_goal"],
                today,
                week_start=settings.get("gym_week_start", 6),
            ),
            "gym_synced_at": user["gym_synced_at"],
            "steps": (user.get("steps") or {}).get(today, 0),
            "metrics": METRICS,
            "counter_icons": COUNTER_ICON_CATALOG,
            "settings": {
                "steps_goal": settings["steps_goal"],
                "weekly_gym_goal": settings["weekly_gym_goal"],
                "gym_week_start": settings.get("gym_week_start", 6),
                "reminder": settings["reminder"],
                "telegram_chat_id": settings["telegram_chat_id"],
                "entities": settings["entities"],
                "hevy_configured": bool(settings["hevy_key"]),
            },
            "household": [
                {
                    "id": u["id"],
                    "name": u["name"],
                    "initials": u["name"][:1].upper(),
                    "meta": f"{len(u['counters'])} counters · {len(u['affirmations'])} affirmations",
                    "active": u["id"] == user["id"],
                }
                for u in self.store.users()
            ],
            "bot_users": self.store.snapshot()["bot_users"],
            "telegram": {
                # The token never leaves the container; the UI only needs to
                # know whether one is set.
                "configured": bool(telegram.get("token")),
                "default_chat": telegram.get("default_chat", ""),
            },
            "integrations": {
                "ha_available": self.hass.available,
                "health_webhook_path": f"/api/health-webhook?key={self.store.health_webhook_key}",
            },
            "version": APP_VERSION,
        }

    # -- counters -------------------------------------------------------

    def create_counter(self, body: dict, ctx) -> tuple[int, Any]:
        counter = self.store.save_counter(ctx.user_id, body, None)
        return _created(counter, "counter")

    def update_counter(self, body: dict, ctx, counter_id: str) -> tuple[int, Any]:
        return _found(self.store.save_counter(ctx.user_id, body, counter_id), "counter")

    def delete_counter(self, _body: dict, ctx, counter_id: str) -> tuple[int, Any]:
        return _deleted(self.store.delete_counter(ctx.user_id, counter_id))

    def reset_counter(self, _body: dict, ctx, counter_id: str) -> tuple[int, Any]:
        return _found(self.store.reset_counter(ctx.user_id, counter_id), "counter")

    def toggle_counter_dash(self, _body: dict, ctx, counter_id: str) -> tuple[int, Any]:
        return _found(self.store.toggle_counter_dash(ctx.user_id, counter_id), "counter")

    # -- affirmations ---------------------------------------------------

    def create_affirmation(self, body: dict, ctx) -> tuple[int, Any]:
        item = self.store.save_affirmation(ctx.user_id, body.get("text", ""), None)
        return _created(item, "affirmation")

    def update_affirmation(self, body: dict, ctx, item_id: str) -> tuple[int, Any]:
        return _found(self.store.save_affirmation(ctx.user_id, body.get("text", ""), item_id), "affirmation")

    def delete_affirmation(self, _body: dict, ctx, item_id: str) -> tuple[int, Any]:
        return _deleted(self.store.delete_affirmation(ctx.user_id, item_id))

    def pin_affirmation(self, _body: dict, ctx, item_id: str) -> tuple[int, Any]:
        return _deleted(self.store.pin_affirmation(ctx.user_id, item_id))

    # -- mottos ---------------------------------------------------------

    def create_motto(self, body: dict, ctx) -> tuple[int, Any]:
        return _created(self.store.save_motto(ctx.user_id, body.get("text", ""), None), "motto")

    def update_motto(self, body: dict, ctx, item_id: str) -> tuple[int, Any]:
        return _found(self.store.save_motto(ctx.user_id, body.get("text", ""), item_id), "motto")

    def delete_motto(self, _body: dict, ctx, item_id: str) -> tuple[int, Any]:
        return _deleted(self.store.delete_motto(ctx.user_id, item_id))

    # -- habits ---------------------------------------------------------

    def create_habit(self, body: dict, ctx) -> tuple[int, Any]:
        return _created(self.store.save_habit(ctx.user_id, body.get("name", ""), None), "habit")

    def update_habit(self, body: dict, ctx, habit_id: str) -> tuple[int, Any]:
        return _found(self.store.save_habit(ctx.user_id, body.get("name", ""), habit_id), "habit")

    def delete_habit(self, _body: dict, ctx, habit_id: str) -> tuple[int, Any]:
        return _deleted(self.store.delete_habit(ctx.user_id, habit_id))

    def toggle_habit(self, body: dict, ctx, habit_id: str) -> tuple[int, Any]:
        day = body.get("date") or self.settings.today()
        result = self.store.toggle_habit(ctx.user_id, habit_id, day)
        return _found(result, "checkin")

    # -- journal --------------------------------------------------------

    def create_journal(self, body: dict, ctx) -> tuple[int, Any]:
        entry = self.store.add_journal(ctx.user_id, body.get("text", ""), body.get("date"))
        return _created(entry, "entry")

    def delete_journal(self, _body: dict, ctx, entry_id: str) -> tuple[int, Any]:
        return _deleted(self.store.delete_journal(ctx.user_id, entry_id))

    # -- settings -------------------------------------------------------

    def update_settings(self, body: dict, ctx) -> tuple[int, Any]:
        return _found(self.store.update_user_settings(ctx.user_id, body), "settings")

    def update_telegram(self, body: dict, ctx) -> tuple[int, Any]:
        updated = self.store.update_telegram(body)
        ok, detail = Telegram(updated.get("token", "")).me() if updated.get("token") else (False, None)
        return HTTPStatus.OK, {
            "telegram": {"configured": bool(updated.get("token")), "default_chat": updated.get("default_chat", "")},
            "bot": {"ok": ok, "detail": detail},
        }

    def telegram_test(self, body: dict, ctx) -> tuple[int, Any]:
        config = self.store.telegram_config()
        bot = Telegram(config.get("token", ""))
        if not bot.configured:
            return HTTPStatus.BAD_REQUEST, {"error": "Set a bot token first"}

        user = self.store.user(ctx.user_id)
        target = (
            str(body.get("chat_id") or "")
            or user["settings"].get("telegram_chat_id")
            or config.get("default_chat")
        )
        if not target:
            return HTTPStatus.BAD_REQUEST, {"error": "No chat ID to send to"}
        ok, error = bot.send(target, "✅ Momentum is connected.")
        if not ok:
            return HTTPStatus.BAD_GATEWAY, {"error": error or "send failed"}
        return HTTPStatus.OK, {"ok": True}

    def telegram_backup(self, body: dict, ctx) -> tuple[int, Any]:
        config = self.store.telegram_config()
        bot = Telegram(config.get("token", ""))
        if not bot.configured:
            return HTTPStatus.BAD_REQUEST, {"error": "Set a bot token first"}

        user = self.store.user(ctx.user_id)
        target = (
            str(body.get("chat_id") or "")
            or user["settings"].get("telegram_chat_id")
            or config.get("default_chat")
        )
        if not target:
            return HTTPStatus.BAD_REQUEST, {"error": "No chat ID to send to"}

        archive, file_count = create_data_backup(self.settings.data_dir)
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        filename = f"momentum-backup-{stamp}.zip"
        ok, error = bot.send_document(
            target,
            filename,
            archive,
            caption=f"Momentum data backup · {file_count} file{'s' if file_count != 1 else ''}",
        )
        if not ok:
            return HTTPStatus.BAD_GATEWAY, {"error": error or "backup send failed"}
        return HTTPStatus.OK, {
            "ok": True,
            "files": file_count,
            "bytes": len(archive),
            "filename": filename,
        }

    def approve_bot_user(self, _body: dict, ctx, bot_user_id: str) -> tuple[int, Any]:
        return _deleted(self.store.set_bot_user_status(bot_user_id, "approved"))

    def remove_bot_user(self, _body: dict, ctx, bot_user_id: str) -> tuple[int, Any]:
        return _deleted(self.store.delete_bot_user(bot_user_id))

    # -- household ------------------------------------------------------

    def add_member(self, body: dict, ctx) -> tuple[int, Any]:
        name = str(body.get("name") or "").strip()
        pin = str(body.get("pin") or "")
        if not name:
            raise ValueError("name is required")
        _validate_pin(pin)
        user = self.store.add_user(name, pin)
        _seed_starter_content(self.store, user["id"])
        return HTTPStatus.CREATED, {"member": {"id": user["id"], "name": user["name"]}}

    def update_member(self, body: dict, ctx, member_id: str) -> tuple[int, Any]:
        changed = False
        if body.get("name"):
            changed = self.store.rename_user(member_id, str(body["name"]))
        if body.get("pin"):
            # Only the signed-in member may change their own PIN.
            if member_id != ctx.user_id:
                return HTTPStatus.FORBIDDEN, {"error": "You can only change your own PIN"}
            _validate_pin(str(body["pin"]))
            changed = self.store.set_pin(member_id, str(body["pin"])) or changed
        return _deleted(changed)

    def delete_member(self, _body: dict, ctx, member_id: str) -> tuple[int, Any]:
        if len(self.store.users()) <= 1:
            return HTTPStatus.CONFLICT, {"error": "The last member cannot be removed"}
        if member_id == ctx.user_id:
            return HTTPStatus.CONFLICT, {"error": "Sign in as someone else to remove this member"}
        return _deleted(self.store.delete_user(member_id))

    # -- sync -----------------------------------------------------------

    def sync_weight(self, _body: dict, ctx) -> tuple[int, Any]:
        return HTTPStatus.OK, {"synced": self.scheduler.sync_weight()}

    def sync_hevy(self, _body: dict, ctx) -> tuple[int, Any]:
        user = self.store.user(ctx.user_id)
        key = user["settings"].get("hevy_key")
        if not key:
            return HTTPStatus.BAD_REQUEST, {"error": "Add a Hevy API key first"}
        workouts, error = Hevy(key).workouts()
        if error:
            return HTTPStatus.BAD_GATEWAY, {"error": error}
        existing = len(user.get("workouts") or [])
        if workouts:
            self.store.replace_workouts(ctx.user_id, workouts)
        return HTTPStatus.OK, {
            "synced": len(workouts),
            "preserved": existing if not workouts else 0,
        }

    def backfill(self, _body: dict, ctx) -> tuple[int, Any]:
        result = self.scheduler.backfill_weight(ctx.user_id)
        if not result["found"] and result["errors"]:
            return HTTPStatus.BAD_GATEWAY, {"error": "; ".join(result["errors"]), **result}
        return HTTPStatus.OK, result

    def import_zepp_life(self, body: dict, ctx) -> tuple[int, Any]:
        archive = body.get("archive")
        if not isinstance(archive, bytes):
            raise ValueError("Upload a Zepp Life export ZIP")
        samples, rows = parse_zepp_life_export(archive)
        merged = self.store.merge_weight_samples(ctx.user_id, samples)
        LOGGER.info(
            "Imported Zepp Life history for %s: rows=%d days=%d added=%d updated=%d",
            ctx.user_id,
            rows,
            len(samples),
            merged["added"],
            merged["updated"],
        )
        return HTTPStatus.OK, {"rows": rows, "days": len(samples), **merged}

    # -- webhook --------------------------------------------------------

    def health_webhook(self, body: dict, ctx: "RequestContext") -> tuple[int, Any]:
        """Ingest Apple Health data pushed by Health Auto Export.

        Apple Health has no server-side API, so the phone pushes instead.
        Authenticated by a per-install key rather than a session.
        """
        if not hmac.compare_digest(ctx.query.get("key", ""), self.store.health_webhook_key):
            return HTTPStatus.UNAUTHORIZED, {"error": "bad key"}

        users = self.store.users()
        target = str(ctx.query.get("user") or "")
        user = next((u for u in users if u["id"] == target), None) or (users[0] if users else None)
        if user is None:
            return HTTPStatus.CONFLICT, {"error": "no members yet"}

        written = {"steps": 0, "weight": 0}
        for metric in _health_metrics(body):
            name = str(metric.get("name") or "").lower()
            for point in metric.get("data") or []:
                day = parse_iso_day(point.get("date"), None)
                value = point.get("qty", point.get("value"))
                if day is None or value is None:
                    continue
                if "step" in name:
                    self.store.record_steps(user["id"], day, int(float(value)))
                    written["steps"] += 1
                elif "weight" in name or "mass" in name:
                    self.store.record_weight_sample(
                        user["id"],
                        # Health Auto Export sends a full timestamp; `day` is
                        # only its date half.
                        {"date": day, "at": point.get("date"), "weight": value},
                    )
                    written["weight"] += 1
        return HTTPStatus.OK, {"ok": True, "written": written}


def _health_metrics(body: dict) -> list[dict]:
    data = body.get("data") if isinstance(body.get("data"), dict) else body
    metrics = data.get("metrics") if isinstance(data, dict) else None
    return metrics if isinstance(metrics, list) else []


def _validate_pin(pin: str) -> None:
    if not re.fullmatch(r"\d{4}", pin or ""):
        raise ValueError("PIN must be exactly 4 digits")


def _seed_starter_content(store: Store, user_id: str) -> None:
    """Give a new member something to look at instead of six empty cards."""
    store.save_counter(user_id, {"name": "No doomscrolling", "icon": "device-mobile", "on_dash": True}, None)
    store.save_affirmation(user_id, "Small steps compound into big changes.", None)
    for habit in ("Morning stretch", "Read 20 minutes", "2L of water"):
        store.save_habit(user_id, habit, None)


def _created(item, key: str) -> tuple[int, Any]:
    if item is None:
        return HTTPStatus.NOT_FOUND, {"error": f"{key} not found"}
    return HTTPStatus.CREATED, {key: item}


def _found(item, key: str) -> tuple[int, Any]:
    if item is None:
        return HTTPStatus.NOT_FOUND, {"error": f"{key} not found"}
    return HTTPStatus.OK, {key: item}


def _deleted(ok: bool) -> tuple[int, Any]:
    if not ok:
        return HTTPStatus.NOT_FOUND, {"error": "not found"}
    return HTTPStatus.OK, {"ok": True}


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------


class BodyTooLarge(ValueError):
    """Request body exceeded MAX_BODY_BYTES."""


class RequestContext:
    def __init__(self, user_id: str | None, client_ip: str, query: dict[str, str], secure: bool) -> None:
        self.user_id = user_id
        self.client_ip = client_ip
        self.query = query
        self.secure = secure
        self.set_cookie: str | None = None
        self.clear_cookie = False


class Handler(BaseHTTPRequestHandler):
    server_version = "Momentum/2.0"
    protocol_version = "HTTP/1.1"

    api: Api
    settings: Settings

    def do_GET(self) -> None:  # noqa: N802
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

    def _query(self) -> dict[str, str]:
        raw = urllib.parse.urlparse(self.path).query
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}

    def _client_ip(self) -> str:
        # Behind cloudflared / the ingress proxy the socket peer is always
        # local, so prefer the forwarded chain's first hop when present.
        forwarded = self.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()[:45]
        return self.client_address[0] if self.client_address else "?"

    def _is_secure(self) -> bool:
        return (self.headers.get("X-Forwarded-Proto") or "").lower() == "https"

    def _session_user(self) -> str | None:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            cookie = SimpleCookie(raw)
        except Exception:
            return None
        morsel = cookie.get(SESSION_COOKIE)
        return self.api.sessions.verify(morsel.value) if morsel else None

    def _handle_api(self, method: str, path: str) -> None:
        if not path.startswith("/api/"):
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "unknown endpoint"})
            return
        try:
            body = self._read_api_body(path)
        except BodyTooLarge as error:
            self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": str(error)})
            return
        except ValueError as error:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return

        ctx = RequestContext(self._session_user(), self._client_ip(), self._query(), self._is_secure())
        try:
            status, payload = self.api.dispatch(method, path, body, ctx)
        except Exception:  # pragma: no cover - defensive
            LOGGER.exception("Unhandled error for %s %s", method, path)
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal error"})
            return
        self._send_json(status, payload, ctx=ctx)

    def _read_api_body(self, path: str) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ValueError("invalid Content-Length") from None
        if length <= 0:
            return {}
        limit = MAX_IMPORT_BYTES if path == "/api/import/zepp-life" else MAX_BODY_BYTES
        if length > limit:
            self._drain(length)
            raise BodyTooLarge(
                "Zepp Life export is too large" if path == "/api/import/zepp-life" else "request body too large"
            )
        raw = self.rfile.read(length)
        if path == "/api/import/zepp-life":
            return {"archive": raw}
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ValueError("body must be valid JSON") from None
        if not isinstance(parsed, dict):
            raise ValueError("body must be a JSON object")
        return parsed

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "font-src 'self'; connect-src 'self'; frame-ancestors 'self' https://*.home-assistant.io",
        )

    def _drain(self, length: int) -> None:
        """Consume a rejected body so the reply is not lost to a broken pipe.

        Read in chunks rather than one big allocation — the whole point of
        rejecting is to not buffer the payload.
        """
        if length > MAX_DRAIN_BYTES:
            self.close_connection = True
            return
        remaining = length
        try:
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
        except OSError:
            self.close_connection = True

    def _send_json(self, status: int, payload: Any, ctx: RequestContext | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        if ctx and ctx.set_cookie:
            self._set_session_cookie(ctx.set_cookie, ctx.secure)
        elif ctx and ctx.clear_cookie:
            self._set_session_cookie("", ctx.secure, expire=True)
        self.end_headers()
        self.wfile.write(body)

    def _set_session_cookie(self, value: str, secure: bool, expire: bool = False) -> None:
        parts = [
            f"{SESSION_COOKIE}={value}",
            "Path=/",
            "HttpOnly",
            "SameSite=Lax",
            f"Max-Age={0 if expire else self.settings.session_days * 86400}",
        ]
        if secure:
            parts.append("Secure")
        self.send_header("Set-Cookie", "; ".join(parts))

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
        if target.endswith(".woff2"):
            content_type = "font/woff2"
        elif target.endswith(".js"):
            content_type = "text/javascript"
        elif target.endswith(".webmanifest"):
            # Not in every platform's mimetypes table, and iOS ignores the
            # manifest outright when it arrives as octet-stream.
            content_type = "application/manifest+json"
        if content_type and content_type.startswith("text/"):
            content_type = f"{content_type}; charset=utf-8"

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        # The shell must never be cached or an update would keep serving the
        # previous build; fonts are immutable and can be held for a year.
        if target.endswith("index.html"):
            self.send_header("Cache-Control", "no-store")
        elif target.endswith(".woff2"):
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        else:
            self.send_header("Cache-Control", "public, max-age=300")
        self._security_headers()
        self.end_headers()
        if include_body:
            self.wfile.write(body)

    def _resolve(self, path: str) -> str | None:
        clean = posixpath.normpath(urllib.parse.unquote(path))
        if clean in ("/", "."):
            clean = "/index.html"
        if clean.startswith(".."):
            return None
        root = self.settings.www_dir
        candidate = os.path.abspath(os.path.join(root, clean.lstrip("/")))
        if candidate != root and not candidate.startswith(root + os.sep):
            return None
        if os.path.isfile(candidate):
            return candidate
        # Extensionless paths fall through to the shell so deep links work.
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

    store = Store(os.path.join(settings.data_dir, "momentum.json"))
    hass = HomeAssistant(os.environ.get("MOMENTUM_SUPERVISOR_TOKEN"))
    scheduler = Scheduler(store, hass, settings)

    Handler.api = Api(settings, store, hass, scheduler)
    Handler.settings = settings

    httpd = ThreadingHTTPServer(("0.0.0.0", settings.port), Handler)
    httpd.daemon_threads = True
    scheduler.start()

    LOGGER.info(
        "Momentum %s ready on :%s (members=%d, ha_api=%s, tz=%s)",
        APP_VERSION,
        settings.port,
        len(store.users()),
        hass.available,
        settings.timezone,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        LOGGER.info("Shutting down")
    finally:
        scheduler.stop()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
