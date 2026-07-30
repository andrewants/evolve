"""Durable JSON store for dashboard state.

Everything lives in a single document under /data so it survives add-on
restarts and updates, and shows up in Home Assistant's add-on backups.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
from datetime import date, datetime, timezone
from typing import Any

SCHEMA_VERSION = 1

DEFAULT_HABITS = [
    {"name": "Move for 30 minutes", "icon": "activity", "color": "iris", "target_per_week": 5},
    {"name": "Read", "icon": "book", "color": "aqua", "target_per_week": 7},
    {"name": "Lights out before 23:30", "icon": "moon", "color": "violet", "target_per_week": 6},
]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def empty_state() -> dict[str, Any]:
    return {
        "version": SCHEMA_VERSION,
        "habits": [],
        "checkins": {},
        "goals": [],
        "journal": [],
        "created": now_iso(),
    }


def seeded_state() -> dict[str, Any]:
    state = empty_state()
    for spec in DEFAULT_HABITS:
        state["habits"].append(
            {
                "id": new_id(),
                "name": spec["name"],
                "icon": spec["icon"],
                "color": spec["color"],
                "target_per_week": spec["target_per_week"],
                "archived": False,
                "created": now_iso(),
            }
        )
    return state


class Store:
    """Thread-safe, crash-safe JSON document store."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._state = self._load()

    # -- persistence ----------------------------------------------------

    def _load(self) -> dict[str, Any]:
        if not os.path.exists(self._path):
            state = seeded_state()
            self._write(state)
            return state
        try:
            with open(self._path, encoding="utf-8") as handle:
                state = json.load(handle)
        except (OSError, ValueError):
            # A truncated or hand-edited file should not take the add-on
            # down; move it aside so the user can still recover it.
            broken = f"{self._path}.corrupt"
            try:
                os.replace(self._path, broken)
            except OSError:
                pass
            state = seeded_state()
            self._write(state)
            return state
        return self._migrate(state)

    def _migrate(self, state: dict[str, Any]) -> dict[str, Any]:
        state.setdefault("version", SCHEMA_VERSION)
        state.setdefault("habits", [])
        state.setdefault("checkins", {})
        state.setdefault("goals", [])
        state.setdefault("journal", [])
        state.setdefault("created", now_iso())
        return state

    def _write(self, state: dict[str, Any]) -> None:
        directory = os.path.dirname(self._path) or "."
        os.makedirs(directory, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w", dir=directory, prefix=".evolve-", suffix=".tmp", delete=False, encoding="utf-8"
        )
        try:
            with handle:
                json.dump(state, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(handle.name, self._path)
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

    # -- habits ---------------------------------------------------------

    def add_habit(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            habit = {
                "id": new_id(),
                "name": (payload.get("name") or "Untitled habit").strip()[:80],
                "icon": payload.get("icon") or "spark",
                "color": payload.get("color") or "iris",
                "target_per_week": _clamp_int(payload.get("target_per_week"), 1, 7, 7),
                "archived": False,
                "created": now_iso(),
            }
            self._state["habits"].append(habit)
            self._commit()
            return habit

    def update_habit(self, habit_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            habit = _find(self._state["habits"], habit_id)
            if habit is None:
                return None
            if "name" in payload:
                habit["name"] = (payload["name"] or habit["name"]).strip()[:80]
            for field in ("icon", "color"):
                if payload.get(field):
                    habit[field] = payload[field]
            if "target_per_week" in payload:
                habit["target_per_week"] = _clamp_int(
                    payload["target_per_week"], 1, 7, habit["target_per_week"]
                )
            if "archived" in payload:
                habit["archived"] = bool(payload["archived"])
            self._commit()
            return habit

    def delete_habit(self, habit_id: str) -> bool:
        with self._lock:
            habits = self._state["habits"]
            remaining = [h for h in habits if h["id"] != habit_id]
            if len(remaining) == len(habits):
                return False
            self._state["habits"] = remaining
            self._state["checkins"].pop(habit_id, None)
            self._commit()
            return True

    def toggle_checkin(self, habit_id: str, day: str) -> dict[str, Any] | None:
        _require_day(day)
        with self._lock:
            if _find(self._state["habits"], habit_id) is None:
                return None
            days = set(self._state["checkins"].get(habit_id, []))
            done = day not in days
            days.add(day) if done else days.discard(day)
            self._state["checkins"][habit_id] = sorted(days)
            self._commit()
            return {"habit_id": habit_id, "date": day, "done": done}

    # -- goals ----------------------------------------------------------

    def add_goal(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            goal = {
                "id": new_id(),
                "title": (payload.get("title") or "Untitled goal").strip()[:120],
                "notes": (payload.get("notes") or "").strip()[:2000],
                "unit": (payload.get("unit") or "").strip()[:24],
                "current": _clamp_float(payload.get("current"), 0.0),
                "target": _clamp_float(payload.get("target"), 100.0) or 100.0,
                "due": payload.get("due") or None,
                "done": False,
                "created": now_iso(),
            }
            self._state["goals"].append(goal)
            self._commit()
            return goal

    def update_goal(self, goal_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            goal = _find(self._state["goals"], goal_id)
            if goal is None:
                return None
            if "title" in payload:
                goal["title"] = (payload["title"] or goal["title"]).strip()[:120]
            if "notes" in payload:
                goal["notes"] = (payload["notes"] or "").strip()[:2000]
            if "unit" in payload:
                goal["unit"] = (payload["unit"] or "").strip()[:24]
            if "current" in payload:
                goal["current"] = _clamp_float(payload["current"], goal["current"])
            if "target" in payload:
                goal["target"] = _clamp_float(payload["target"], goal["target"]) or goal["target"]
            if "due" in payload:
                goal["due"] = payload["due"] or None
            if "done" in payload:
                goal["done"] = bool(payload["done"])
            self._commit()
            return goal

    def delete_goal(self, goal_id: str) -> bool:
        with self._lock:
            goals = self._state["goals"]
            remaining = [g for g in goals if g["id"] != goal_id]
            if len(remaining) == len(goals):
                return False
            self._state["goals"] = remaining
            self._commit()
            return True

    # -- journal --------------------------------------------------------

    def add_entry(self, payload: dict[str, Any]) -> dict[str, Any]:
        day = payload.get("date") or date.today().isoformat()
        _require_day(day)
        with self._lock:
            entry = {
                "id": new_id(),
                "date": day,
                "mood": _clamp_int(payload.get("mood"), 1, 5, 3),
                "energy": _clamp_int(payload.get("energy"), 1, 5, 3),
                "text": (payload.get("text") or "").strip()[:8000],
                "created": now_iso(),
            }
            self._state["journal"].insert(0, entry)
            del self._state["journal"][500:]
            self._commit()
            return entry

    def delete_entry(self, entry_id: str) -> bool:
        with self._lock:
            entries = self._state["journal"]
            remaining = [e for e in entries if e["id"] != entry_id]
            if len(remaining) == len(entries):
                return False
            self._state["journal"] = remaining
            self._commit()
            return True


def _find(items: list[dict[str, Any]], item_id: str) -> dict[str, Any] | None:
    return next((item for item in items if item.get("id") == item_id), None)


def _require_day(day: str) -> None:
    try:
        date.fromisoformat(day)
    except (TypeError, ValueError):
        raise ValueError(f"expected an ISO date (YYYY-MM-DD), got {day!r}") from None


def _clamp_int(value: Any, low: int, high: int, fallback: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return fallback


def _clamp_float(value: Any, fallback: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return fallback
    if parsed != parsed or parsed in (float("inf"), float("-inf")):
        return fallback
    return max(0.0, min(1_000_000.0, parsed))
