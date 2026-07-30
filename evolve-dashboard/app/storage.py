"""Durable JSON store for Momentum.

One document under /data, so it survives add-on restarts and is captured by
Home Assistant add-on backups. Household members each own their counters,
habits, affirmations, journal and synced body/gym data; bot users and the
Telegram token are shared.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import tempfile
import threading
import uuid
from datetime import date, datetime, timezone
from typing import Any

SCHEMA_VERSION = 3

PIN_ITERATIONS = 200_000
MAX_JOURNAL_ENTRIES = 1000

COUNTER_ICONS = [
    "prohibit",
    "wine",
    "hamburger",
    "coffee",
    "device-mobile",
    "currency-dollar",
]

# Body-composition metrics the Mi scale exposes, in the order the Weight
# screen shows them. `key` doubles as the sample field and the chip id.
METRICS = [
    {"key": "weight", "label": "Weight", "unit": "kg"},
    {"key": "fat", "label": "Body fat", "unit": "%"},
    {"key": "muscle", "label": "Muscle", "unit": "kg"},
    {"key": "bmi", "label": "BMI", "unit": ""},
    {"key": "water", "label": "Water", "unit": "%"},
    {"key": "visceral", "label": "Visceral fat", "unit": ""},
]
METRIC_KEYS = [metric["key"] for metric in METRICS]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def today_iso() -> str:
    return date.today().isoformat()


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def hash_pin(pin: str, salt: str | None = None) -> tuple[str, str]:
    """PBKDF2 the PIN. A 4-digit space is small, so the server also rate
    limits attempts — see server.py."""
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), PIN_ITERATIONS)
    return digest.hex(), salt


def verify_pin(pin: str, expected_hash: str, salt: str) -> bool:
    if not expected_hash or not salt:
        return False
    candidate, _ = hash_pin(pin, salt)
    return hmac.compare_digest(candidate, expected_hash)


def default_user_settings() -> dict[str, Any]:
    return {
        "steps_goal": 10000,
        "weekly_gym_goal": 3,
        "reminder": {"on": False, "time": "08:00"},
        "telegram_chat_id": "",
        "hevy_key": "",
        # Prefer one legacy BodyMiScale composite entity per person; separate
        # metric sensors remain supported for newer integrations and fallbacks.
        "entities": {"bodymiscale": ""} | {key: "" for key in METRIC_KEYS} | {"steps": ""},
    }


def empty_state() -> dict[str, Any]:
    return {
        "version": SCHEMA_VERSION,
        "users": [],
        "bot_users": [],
        "settings": {
            "telegram": {"token": "", "default_chat": "", "last_update_id": 0},
            "health_webhook_key": secrets.token_urlsafe(24),
        },
        "session_secret": secrets.token_hex(32),
        "created": now_iso(),
    }


def new_user(name: str, pin: str | None = None) -> dict[str, Any]:
    pin_hash, salt = hash_pin(pin) if pin else ("", "")
    return {
        "id": new_id(),
        "name": name.strip()[:40] or "Member",
        "pin_hash": pin_hash,
        "pin_salt": salt,
        "counters": [],
        "affirmations": [],
        "habits": [],
        "checkins": {},
        "journal": [],
        "weight_samples": [],
        "workouts": [],
        "gym_synced_at": None,
        "milestones_sent": [],
        "settings": default_user_settings(),
        "created": now_iso(),
    }


class Store:
    """Thread-safe, crash-safe JSON document store."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._state = self._load()

    # -- persistence ----------------------------------------------------

    def _load(self) -> dict[str, Any]:
        if not os.path.exists(self._path):
            state = empty_state()
            self._write(state)
            return state
        try:
            with open(self._path, encoding="utf-8") as handle:
                state = json.load(handle)
        except (OSError, ValueError):
            # A truncated or hand-edited file must not take the add-on down;
            # move it aside so the user can still recover it.
            try:
                os.replace(self._path, f"{self._path}.corrupt")
            except OSError:
                pass
            state = empty_state()
            self._write(state)
            return state
        migrated = self._migrate(state)
        self._write(migrated)
        return migrated

    def _migrate(self, state: dict[str, Any]) -> dict[str, Any]:
        state.setdefault("version", SCHEMA_VERSION)
        state.setdefault("users", [])
        state.setdefault("bot_users", [])
        state.setdefault("session_secret", secrets.token_hex(32))
        settings = state.setdefault("settings", {})
        telegram = settings.setdefault("telegram", {})
        telegram.setdefault("token", "")
        telegram.setdefault("default_chat", "")
        telegram.setdefault("last_update_id", 0)
        settings.setdefault("health_webhook_key", secrets.token_urlsafe(24))

        for user in state["users"]:
            user.setdefault("counters", [])
            user.setdefault("affirmations", [])
            user.setdefault("habits", [])
            user.setdefault("checkins", {})
            user.setdefault("journal", [])
            user.setdefault("weight_samples", [])
            user.setdefault("workouts", [])
            user.setdefault("gym_synced_at", None)
            user.setdefault("milestones_sent", [])
            defaults = default_user_settings()
            user_settings = user.setdefault("settings", defaults)
            for key, value in defaults.items():
                user_settings.setdefault(key, value)
            for key in defaults["entities"]:
                user_settings["entities"].setdefault(key, "")
        return state

    def _write(self, state: dict[str, Any]) -> None:
        directory = os.path.dirname(self._path) or "."
        os.makedirs(directory, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w", dir=directory, prefix=".momentum-", suffix=".tmp", delete=False, encoding="utf-8"
        )
        try:
            with handle:
                json.dump(state, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(handle.name, self._path)
            os.chmod(self._path, 0o600)
        except BaseException:
            try:
                os.unlink(handle.name)
            except OSError:
                pass
            raise

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._state))

    def _commit(self) -> None:
        self._write(self._state)

    # -- accessors ------------------------------------------------------

    @property
    def session_secret(self) -> bytes:
        with self._lock:
            return bytes.fromhex(self._state["session_secret"])

    @property
    def health_webhook_key(self) -> str:
        with self._lock:
            return self._state["settings"]["health_webhook_key"]

    def telegram_config(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state["settings"]["telegram"])

    def users(self) -> list[dict[str, Any]]:
        with self._lock:
            return json.loads(json.dumps(self._state["users"]))

    def user(self, user_id: str) -> dict[str, Any] | None:
        with self._lock:
            found = _find(self._state["users"], user_id)
            return json.loads(json.dumps(found)) if found else None

    def _user_ref(self, user_id: str) -> dict[str, Any] | None:
        return _find(self._state["users"], user_id)

    def update(self, mutate) -> Any:
        """Run `mutate(state)` under the lock and persist the result."""
        with self._lock:
            result = mutate(self._state)
            self._commit()
            return result

    # -- household ------------------------------------------------------

    def add_user(self, name: str, pin: str | None) -> dict[str, Any]:
        with self._lock:
            user = new_user(name, pin)
            self._state["users"].append(user)
            self._commit()
            return json.loads(json.dumps(user))

    def set_pin(self, user_id: str, pin: str) -> bool:
        with self._lock:
            user = self._user_ref(user_id)
            if user is None:
                return False
            user["pin_hash"], user["pin_salt"] = hash_pin(pin)
            self._commit()
            return True

    def check_pin(self, user_id: str, pin: str) -> bool:
        with self._lock:
            user = self._user_ref(user_id)
            if user is None:
                return False
            return verify_pin(pin, user.get("pin_hash", ""), user.get("pin_salt", ""))

    def rename_user(self, user_id: str, name: str) -> bool:
        with self._lock:
            user = self._user_ref(user_id)
            if user is None:
                return False
            user["name"] = name.strip()[:40] or user["name"]
            self._commit()
            return True

    def delete_user(self, user_id: str) -> bool:
        with self._lock:
            users = self._state["users"]
            remaining = [u for u in users if u["id"] != user_id]
            if len(remaining) == len(users):
                return False
            self._state["users"] = remaining
            self._commit()
            return True

    # -- counters -------------------------------------------------------

    def save_counter(self, user_id: str, payload: dict[str, Any], counter_id: str | None) -> dict[str, Any] | None:
        with self._lock:
            user = self._user_ref(user_id)
            if user is None:
                return None
            name = (payload.get("name") or "").strip()[:60]
            if not name:
                raise ValueError("counter needs a name")
            since = payload.get("date") or today_iso()
            _require_day(since)
            icon = payload.get("icon") if payload.get("icon") in COUNTER_ICONS else "prohibit"
            wants_dash = bool(payload.get("on_dash"))

            if counter_id:
                counter = _find(user["counters"], counter_id)
                if counter is None:
                    return None
            else:
                counter = {"id": new_id(), "created": now_iso()}
                user["counters"].append(counter)

            counter.update({"name": name, "date": since, "icon": icon})
            # The dashboard row holds exactly three; refuse the fourth
            # rather than silently dropping one.
            on_dash_count = sum(
                1 for c in user["counters"] if c.get("on_dash") and c["id"] != counter["id"]
            )
            counter["on_dash"] = wants_dash and on_dash_count < 3
            self._commit()
            return dict(counter)

    def toggle_counter_dash(self, user_id: str, counter_id: str) -> dict[str, Any] | None:
        with self._lock:
            user = self._user_ref(user_id)
            if user is None:
                return None
            counter = _find(user["counters"], counter_id)
            if counter is None:
                return None
            if not counter.get("on_dash"):
                shown = sum(1 for c in user["counters"] if c.get("on_dash"))
                if shown >= 3:
                    raise ValueError("Dashboard shows max 3 counters")
            counter["on_dash"] = not counter.get("on_dash")
            self._commit()
            return dict(counter)

    def reset_counter(self, user_id: str, counter_id: str) -> dict[str, Any] | None:
        with self._lock:
            user = self._user_ref(user_id)
            if user is None:
                return None
            counter = _find(user["counters"], counter_id)
            if counter is None:
                return None
            previous = days_since(counter["date"])
            best = max(int(counter.get("best", 0)), previous)
            counter.update({"date": today_iso(), "best": best})
            # A fresh run should be able to earn its milestones again.
            user["milestones_sent"] = [
                m for m in user["milestones_sent"] if not m.startswith(f"{counter_id}:")
            ]
            self._commit()
            return dict(counter)

    def delete_counter(self, user_id: str, counter_id: str) -> bool:
        return self._delete_from(user_id, "counters", counter_id)

    # -- affirmations ---------------------------------------------------

    def save_affirmation(self, user_id: str, text: str, affirmation_id: str | None) -> dict[str, Any] | None:
        with self._lock:
            user = self._user_ref(user_id)
            if user is None:
                return None
            text = (text or "").strip()[:500]
            if not text:
                raise ValueError("affirmation needs text")
            if affirmation_id:
                item = _find(user["affirmations"], affirmation_id)
                if item is None:
                    return None
                item["text"] = text
            else:
                item = {
                    "id": new_id(),
                    "text": text,
                    "pinned": not user["affirmations"],
                    "created": now_iso(),
                }
                user["affirmations"].append(item)
            self._commit()
            return dict(item)

    def pin_affirmation(self, user_id: str, affirmation_id: str) -> bool:
        with self._lock:
            user = self._user_ref(user_id)
            if user is None or _find(user["affirmations"], affirmation_id) is None:
                return False
            for item in user["affirmations"]:
                item["pinned"] = item["id"] == affirmation_id
            self._commit()
            return True

    def delete_affirmation(self, user_id: str, affirmation_id: str) -> bool:
        return self._delete_from(user_id, "affirmations", affirmation_id)

    # -- habits ---------------------------------------------------------

    def save_habit(self, user_id: str, name: str, habit_id: str | None) -> dict[str, Any] | None:
        with self._lock:
            user = self._user_ref(user_id)
            if user is None:
                return None
            name = (name or "").strip()[:80]
            if not name:
                raise ValueError("habit needs a name")
            if habit_id:
                habit = _find(user["habits"], habit_id)
                if habit is None:
                    return None
                habit["name"] = name
            else:
                habit = {"id": new_id(), "name": name, "created": now_iso()}
                user["habits"].append(habit)
            self._commit()
            return dict(habit)

    def toggle_habit(self, user_id: str, habit_id: str, day: str) -> dict[str, Any] | None:
        _require_day(day)
        with self._lock:
            user = self._user_ref(user_id)
            if user is None or _find(user["habits"], habit_id) is None:
                return None
            days = set(user["checkins"].get(habit_id, []))
            done = day not in days
            days.add(day) if done else days.discard(day)
            user["checkins"][habit_id] = sorted(days)
            self._commit()
            return {"habit_id": habit_id, "date": day, "done": done}

    def delete_habit(self, user_id: str, habit_id: str) -> bool:
        with self._lock:
            user = self._user_ref(user_id)
            if user is None:
                return False
            habits = user["habits"]
            remaining = [h for h in habits if h["id"] != habit_id]
            if len(remaining) == len(habits):
                return False
            user["habits"] = remaining
            user["checkins"].pop(habit_id, None)
            self._commit()
            return True

    # -- journal --------------------------------------------------------

    def add_journal(self, user_id: str, text: str, day: str | None = None) -> dict[str, Any] | None:
        day = day or today_iso()
        _require_day(day)
        with self._lock:
            user = self._user_ref(user_id)
            if user is None:
                return None
            text = (text or "").strip()[:8000]
            if not text:
                raise ValueError("entry is empty")
            entry = {"id": new_id(), "date": day, "text": text, "created": now_iso()}
            user["journal"].insert(0, entry)
            del user["journal"][MAX_JOURNAL_ENTRIES:]
            self._commit()
            return dict(entry)

    def delete_journal(self, user_id: str, entry_id: str) -> bool:
        return self._delete_from(user_id, "journal", entry_id)

    # -- synced data ----------------------------------------------------

    def record_weight_sample(self, user_id: str, sample: dict[str, Any]) -> bool:
        """Append a body-composition sample, replacing same-day duplicates."""
        with self._lock:
            user = self._user_ref(user_id)
            if user is None:
                return False
            day = sample.get("date") or today_iso()
            _require_day(day)
            clean = {"date": day}
            for key in METRIC_KEYS:
                value = sample.get(key)
                clean[key] = None if value is None else _as_float(value)
            if clean.get("weight") is None:
                return False

            samples = [s for s in user["weight_samples"] if s.get("date") != day]
            samples.append(clean)
            samples.sort(key=lambda s: s["date"])
            user["weight_samples"] = samples
            self._commit()
            return True

    def merge_weight_samples(
        self, user_id: str, imported: list[dict[str, Any]]
    ) -> dict[str, int]:
        """Merge imported history, preserving every existing non-null field."""
        with self._lock:
            user = self._user_ref(user_id)
            if user is None:
                return {"added": 0, "updated": 0, "unchanged": 0}

            by_day = {sample.get("date"): dict(sample) for sample in user["weight_samples"]}
            added = updated = unchanged = 0
            for sample in imported:
                day = str(sample.get("date") or "")
                _require_day(day)
                incoming = {"date": day}
                for key in METRIC_KEYS:
                    value = sample.get(key)
                    incoming[key] = None if value is None else _as_float(value)
                if incoming["weight"] is None:
                    unchanged += 1
                    continue

                existing = by_day.get(day)
                if existing is None:
                    by_day[day] = incoming
                    added += 1
                    continue

                merged = dict(existing)
                changed = False
                for key in METRIC_KEYS:
                    if merged.get(key) is None and incoming.get(key) is not None:
                        merged[key] = incoming[key]
                        changed = True
                by_day[day] = merged
                if changed:
                    updated += 1
                else:
                    unchanged += 1

            user["weight_samples"] = [by_day[day] for day in sorted(by_day)]
            if added or updated:
                self._commit()
            return {"added": added, "updated": updated, "unchanged": unchanged}

    def replace_workouts(self, user_id: str, workouts: list[dict[str, Any]]) -> bool:
        with self._lock:
            user = self._user_ref(user_id)
            if user is None:
                return False
            user["workouts"] = workouts
            user["gym_synced_at"] = now_iso()
            self._commit()
            return True

    def record_steps(self, user_id: str, day: str, steps: int) -> bool:
        _require_day(day)
        with self._lock:
            user = self._user_ref(user_id)
            if user is None:
                return False
            user.setdefault("steps", {})[day] = max(0, int(steps))
            # A rolling quarter is all the dashboard ever reads back.
            for old in sorted(user["steps"])[:-120]:
                user["steps"].pop(old, None)
            self._commit()
            return True

    # -- settings -------------------------------------------------------

    def update_user_settings(self, user_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            user = self._user_ref(user_id)
            if user is None:
                return None
            settings = user["settings"]
            if "steps_goal" in patch:
                settings["steps_goal"] = _clamp_int(patch["steps_goal"], 1000, 60000, settings["steps_goal"])
            if "weekly_gym_goal" in patch:
                settings["weekly_gym_goal"] = _clamp_int(patch["weekly_gym_goal"], 1, 7, settings["weekly_gym_goal"])
            if "hevy_key" in patch:
                settings["hevy_key"] = str(patch["hevy_key"] or "").strip()[:200]
            if "telegram_chat_id" in patch:
                settings["telegram_chat_id"] = str(patch["telegram_chat_id"] or "").strip()[:40]
            if isinstance(patch.get("reminder"), dict):
                reminder = settings["reminder"]
                if "on" in patch["reminder"]:
                    reminder["on"] = bool(patch["reminder"]["on"])
                if "time" in patch["reminder"]:
                    reminder["time"] = _clean_time(patch["reminder"]["time"], reminder["time"])
            if isinstance(patch.get("entities"), dict):
                for key, value in patch["entities"].items():
                    if key in settings["entities"]:
                        settings["entities"][key] = str(value or "").strip()[:120]
            self._commit()
            return dict(settings)

    def update_telegram(self, patch: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            telegram = self._state["settings"]["telegram"]
            if "token" in patch:
                telegram["token"] = str(patch["token"] or "").strip()[:120]
            if "default_chat" in patch:
                telegram["default_chat"] = str(patch["default_chat"] or "").strip()[:40]
            self._commit()
            return dict(telegram)

    # -- bot users ------------------------------------------------------

    def upsert_bot_user(self, chat_id: str, name: str, handle: str) -> dict[str, Any]:
        with self._lock:
            existing = next(
                (u for u in self._state["bot_users"] if u["chat_id"] == str(chat_id)), None
            )
            if existing:
                existing["name"] = name or existing["name"]
                existing["handle"] = handle or existing["handle"]
            else:
                existing = {
                    "id": new_id(),
                    "chat_id": str(chat_id),
                    "name": name or "Unknown user",
                    "handle": handle or "",
                    "status": "pending",
                    "seen": now_iso(),
                }
                self._state["bot_users"].append(existing)
            self._commit()
            return dict(existing)

    def set_bot_user_status(self, bot_user_id: str, status: str) -> bool:
        if status not in ("approved", "pending"):
            return False
        with self._lock:
            user = _find(self._state["bot_users"], bot_user_id)
            if user is None:
                return False
            user["status"] = status
            self._commit()
            return True

    def delete_bot_user(self, bot_user_id: str) -> bool:
        with self._lock:
            users = self._state["bot_users"]
            remaining = [u for u in users if u["id"] != bot_user_id]
            if len(remaining) == len(users):
                return False
            self._state["bot_users"] = remaining
            self._commit()
            return True

    def approved_chat_ids(self) -> list[str]:
        with self._lock:
            return [u["chat_id"] for u in self._state["bot_users"] if u["status"] == "approved"]

    def set_telegram_offset(self, update_id: int) -> None:
        with self._lock:
            self._state["settings"]["telegram"]["last_update_id"] = int(update_id)
            self._commit()

    def mark_milestone(self, user_id: str, key: str) -> bool:
        """Record a milestone as announced. False if it already was."""
        with self._lock:
            user = self._user_ref(user_id)
            if user is None or key in user["milestones_sent"]:
                return False
            user["milestones_sent"].append(key)
            del user["milestones_sent"][:-200]
            self._commit()
            return True

    # -- helpers --------------------------------------------------------

    def _delete_from(self, user_id: str, collection: str, item_id: str) -> bool:
        with self._lock:
            user = self._user_ref(user_id)
            if user is None:
                return False
            items = user[collection]
            remaining = [i for i in items if i["id"] != item_id]
            if len(remaining) == len(items):
                return False
            user[collection] = remaining
            self._commit()
            return True


def days_since(iso_day: str) -> int:
    try:
        return max(0, (date.today() - date.fromisoformat(iso_day)).days)
    except (TypeError, ValueError):
        return 0


def _find(items: list[dict[str, Any]], item_id: str) -> dict[str, Any] | None:
    return next((item for item in items if item.get("id") == item_id), None)


def _require_day(day: str) -> None:
    try:
        date.fromisoformat(day)
    except (TypeError, ValueError):
        raise ValueError(f"expected an ISO date (YYYY-MM-DD), got {day!r}") from None


def _clean_time(value: Any, fallback: str) -> str:
    try:
        hours, minutes = str(value).split(":")[:2]
        return f"{int(hours) % 24:02d}:{int(minutes) % 60:02d}"
    except (AttributeError, TypeError, ValueError):
        return fallback


def _clamp_int(value: Any, low: int, high: int, fallback: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return fallback


def _as_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed or parsed in (float("inf"), float("-inf")):
        return None
    return round(parsed, 2)
