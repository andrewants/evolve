"""Outbound clients: Home Assistant core, Hevy, Telegram.

All three are small, read-mostly HTTP clients built on urllib so the add-on
image needs no third-party packages. Every call is defensive: a failing
integration degrades its card, it never takes the dashboard down.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

LOGGER = logging.getLogger("momentum.integrations")

HA_CORE_API = "http://supervisor/core/api"
HEVY_API = "https://api.hevyapp.com/v1"
TELEGRAM_API = "https://api.telegram.org"

TIMEOUT = 12
HA_CACHE_TTL = 20
HA_HISTORY_START = "1970-01-01T00:00:00+00:00"

BODYMISCALE_ATTRIBUTES = {
    "weight": ("weight",),
    "fat": ("body_fat", "fat", "bodyfat"),
    "muscle": ("muscle_mass", "muscle"),
    "bmi": ("bmi",),
    "water": ("water", "body_water"),
    "visceral": ("visceral_fat", "visceral"),
}


def _request(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
    method: str | None = None,
    timeout: int = TIMEOUT,
) -> tuple[int, Any]:
    """Return (status, parsed_json). Status 0 means the call never landed."""
    data = None
    headers = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            try:
                return response.status, json.loads(body or b"null")
            except ValueError:
                return response.status, None
    except urllib.error.HTTPError as error:
        try:
            return error.code, json.loads(error.read() or b"null")
        except ValueError:
            return error.code, None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
        LOGGER.debug("request to %s failed: %s", url.split("?")[0], error)
        return 0, None


# ---------------------------------------------------------------------------
# Home Assistant
# ---------------------------------------------------------------------------


class HomeAssistant:
    """Read-only access to entity states through the Supervisor core proxy."""

    def __init__(self, token: str | None) -> None:
        self._token = token or ""
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}

    @property
    def available(self) -> bool:
        return bool(self._token)

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}

    def state(self, entity_id: str) -> dict[str, Any]:
        if not self.available:
            return _entity_unavailable(entity_id, "no supervisor token")
        if not entity_id:
            return _entity_unavailable(entity_id, "not configured")

        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(entity_id)
            if cached and now - cached[0] < HA_CACHE_TTL:
                return cached[1]

        status, payload = _request(
            f"{HA_CORE_API}/states/{urllib.parse.quote(entity_id)}", headers=self._headers
        )
        if status == 404:
            result = _entity_unavailable(entity_id, "not found")
        elif status != 200 or not isinstance(payload, dict):
            result = _entity_unavailable(entity_id, "unreachable" if status == 0 else f"HTTP {status}")
        else:
            attributes = payload.get("attributes") or {}
            raw = payload.get("state")
            result = {
                "entity_id": entity_id,
                "state": raw,
                "value": _to_float(raw),
                "attributes": attributes,
                "unit": attributes.get("unit_of_measurement") or "",
                "friendly_name": attributes.get("friendly_name") or entity_id,
                "last_changed": payload.get("last_changed"),
                "last_updated": payload.get("last_updated"),
                "ok": raw not in (None, "unknown", "unavailable"),
            }

        with self._lock:
            self._cache[entity_id] = (now, result)
        return result

    def history(
        self,
        entity_id: str,
        days: int | None = None,
        *,
        include_attributes: bool = False,
    ) -> list[dict[str, Any]]:
        """Daily last-value series for one entity, oldest first.

        Used to backfill body-composition history from the recorder. The
        recorder purges on its own schedule, which is exactly why samples
        get copied into our own store once seen.
        """
        if not self.available or not entity_id:
            return []
        start = (
            (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
            if days is not None
            else HA_HISTORY_START
        )
        flags = "" if include_attributes else "&minimal_response&no_attributes"
        url = (
            f"{HA_CORE_API}/history/period/{urllib.parse.quote(start)}"
            f"?filter_entity_id={urllib.parse.quote(entity_id)}{flags}"
        )
        status, payload = _request(url, headers=self._headers, timeout=120)
        if status != 200 or not isinstance(payload, list) or not payload:
            return []

        by_day: dict[str, dict[str, Any]] = {}
        for point in payload[0]:
            value = _to_float(point.get("state"))
            stamp = point.get("last_changed") or point.get("last_updated")
            attributes = point.get("attributes") if include_attributes else None
            if (value is None and not isinstance(attributes, dict)) or not stamp:
                continue
            try:
                day = datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone().date().isoformat()
            except ValueError:
                continue
            by_day[day] = {"date": day, "value": value}
            if isinstance(attributes, dict):
                by_day[day]["attributes"] = attributes
        return [by_day[day] for day in sorted(by_day)]

    def bodymiscale_state(self, entity_id: str) -> dict[str, Any] | None:
        """Return one complete sample from a legacy composite BodyMiScale entity."""
        reading = self.state(entity_id)
        if not reading["ok"]:
            return None
        values = _bodymiscale_values(reading.get("attributes"))
        if values.get("weight") is None:
            # Some versions expose weight as the entity state.
            values["weight"] = reading.get("value")
        if values.get("weight") is None:
            return None
        return {
            "date": _history_day(reading.get("last_updated") or reading.get("last_changed")),
            **values,
        }

    def bodymiscale_history(self, entity_id: str) -> list[dict[str, Any]]:
        """Return all daily composite samples retained by Home Assistant Recorder."""
        samples: list[dict[str, Any]] = []
        for point in self.history(entity_id, include_attributes=True):
            values = _bodymiscale_values(point.get("attributes"))
            if values.get("weight") is None:
                values["weight"] = point.get("value")
            if values.get("weight") is not None:
                samples.append({"date": point["date"], **values})
        return samples


def _entity_unavailable(entity_id: str, reason: str) -> dict[str, Any]:
    return {
        "entity_id": entity_id,
        "state": None,
        "value": None,
        "attributes": {},
        "unit": "",
        "friendly_name": entity_id or "unset",
        "last_changed": None,
        "ok": False,
        "reason": reason,
    }


def _to_float(value: Any) -> float | None:
    if isinstance(value, str):
        match = re.match(r"^\s*(-?(?:\d+(?:\.\d*)?|\.\d+))", value)
        if match:
            value = match.group(1)
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed or parsed in (float("inf"), float("-inf")):
        return None
    return parsed


def _bodymiscale_values(attributes: Any) -> dict[str, float | None]:
    attributes = attributes if isinstance(attributes, dict) else {}
    # Older BodyMiScale versions sometimes nest the calculated values.
    nested = attributes.get("sensors")
    sources = [attributes, nested] if isinstance(nested, dict) else [attributes]
    values: dict[str, float | None] = {}
    for metric, aliases in BODYMISCALE_ATTRIBUTES.items():
        values[metric] = None
        for source in sources:
            for alias in aliases:
                parsed = _to_float(source.get(alias))
                if parsed is not None:
                    values[metric] = parsed
                    break
            if values[metric] is not None:
                break
    return values


def _history_day(stamp: Any) -> str:
    if stamp:
        try:
            return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).astimezone().date().isoformat()
        except ValueError:
            pass
    return datetime.now().astimezone().date().isoformat()


# ---------------------------------------------------------------------------
# Hevy
# ---------------------------------------------------------------------------


class Hevy:
    """Hevy public API client (api-key header, read-only)."""

    def __init__(self, api_key: str) -> None:
        self._key = (api_key or "").strip()

    @property
    def configured(self) -> bool:
        return bool(self._key)

    def workouts(self, page_size: int = 10) -> tuple[list[dict[str, Any]], str | None]:
        """All workouts, newest first. Returns (workouts, error)."""
        if not self.configured:
            return [], "no api key"

        collected: list[dict[str, Any]] = []
        page = 1
        while True:
            status, payload = _request(
                f"{HEVY_API}/workouts?page={page}&pageSize={page_size}",
                headers={"api-key": self._key, "Accept": "application/json"},
            )
            if status == 401:
                return [], "invalid api key"
            if status != 200 or not isinstance(payload, dict):
                return collected, "unreachable" if status == 0 else f"HTTP {status}"

            batch = payload.get("workouts") or []
            collected.extend(normalise_workout(w) for w in batch)
            page_count = payload.get("page_count")
            if not batch or len(batch) < page_size or (
                isinstance(page_count, int) and page >= page_count
            ):
                break
            page += 1
        return [w for w in collected if w], None


def normalise_workout(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Flatten a Hevy workout into what the Gym screen needs."""
    if not isinstance(raw, dict):
        return None
    start = raw.get("start_time")
    if not start:
        return None
    try:
        started = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
    except ValueError:
        return None
    ended = None
    if raw.get("end_time"):
        try:
            ended = datetime.fromisoformat(str(raw["end_time"]).replace("Z", "+00:00"))
        except ValueError:
            ended = None

    volume = 0.0
    sets = 0
    exercises = raw.get("exercises") or []
    for exercise in exercises:
        for entry in exercise.get("sets") or []:
            reps = entry.get("reps") or 0
            weight = entry.get("weight_kg") or 0
            try:
                volume += float(reps) * float(weight)
            except (TypeError, ValueError):
                pass
            sets += 1

    duration_min = int((ended - started).total_seconds() // 60) if ended else 0
    return {
        "id": str(raw.get("id") or start),
        "name": (raw.get("title") or "Workout").strip()[:80],
        "date": started.astimezone().date().isoformat(),
        "duration_min": max(0, duration_min),
        "volume_kg": round(volume),
        "exercises": len(exercises),
        "sets": sets,
    }


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------


class Telegram:
    def __init__(self, token: str) -> None:
        self._token = (token or "").strip()

    @property
    def configured(self) -> bool:
        return bool(self._token)

    def _url(self, method: str) -> str:
        return f"{TELEGRAM_API}/bot{self._token}/{method}"

    def send(self, chat_id: str, text: str) -> tuple[bool, str | None]:
        if not self.configured:
            return False, "no bot token"
        if not chat_id:
            return False, "no chat id"
        status, payload = _request(
            self._url("sendMessage"),
            payload={"chat_id": str(chat_id), "text": text, "parse_mode": "HTML"},
        )
        if status == 200 and isinstance(payload, dict) and payload.get("ok"):
            return True, None
        reason = "unreachable" if status == 0 else f"HTTP {status}"
        if isinstance(payload, dict) and payload.get("description"):
            reason = str(payload["description"])
        return False, reason

    def send_document(
        self,
        chat_id: str,
        filename: str,
        content: bytes,
        caption: str = "",
    ) -> tuple[bool, str | None]:
        """Send a binary document using Telegram's multipart endpoint."""
        if not self.configured:
            return False, "no bot token"
        if not chat_id:
            return False, "no chat id"

        boundary = f"momentum-{uuid.uuid4().hex}"
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", filename) or "momentum-backup.zip"
        chunks: list[bytes] = []

        def field(name: str, value: str) -> None:
            chunks.extend([
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                value.encode(),
                b"\r\n",
            ])

        field("chat_id", str(chat_id))
        if caption:
            field("caption", caption)
        chunks.extend([
            f"--{boundary}\r\n".encode(),
            (
                'Content-Disposition: form-data; name="document"; '
                f'filename="{safe_name}"\r\n'
            ).encode(),
            b"Content-Type: application/zip\r\n\r\n",
            content,
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ])
        request = urllib.request.Request(
            self._url("sendDocument"),
            data=b"".join(chunks),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                status = response.status
                try:
                    payload = json.loads(response.read() or b"null")
                except ValueError:
                    payload = None
        except urllib.error.HTTPError as error:
            status = error.code
            try:
                payload = json.loads(error.read() or b"null")
            except ValueError:
                payload = None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
            LOGGER.debug("Telegram document upload failed: %s", error)
            return False, "unreachable"

        if status == 200 and isinstance(payload, dict) and payload.get("ok"):
            return True, None
        reason = "unreachable" if status == 0 else f"HTTP {status}"
        if isinstance(payload, dict) and payload.get("description"):
            reason = str(payload["description"])
        return False, reason

    def me(self) -> tuple[bool, str | None]:
        """Validate the token. Returns (ok, bot username or error)."""
        if not self.configured:
            return False, "no bot token"
        status, payload = _request(self._url("getMe"))
        if status == 200 and isinstance(payload, dict) and payload.get("ok"):
            return True, (payload.get("result") or {}).get("username")
        if isinstance(payload, dict) and payload.get("description"):
            return False, str(payload["description"])
        return False, "unreachable" if status == 0 else f"HTTP {status}"

    def updates(self, offset: int) -> tuple[list[dict[str, Any]], int]:
        """Poll for people who messaged the bot. Returns (chats, new offset).

        Only the sender identity is extracted — message text is ignored
        beyond noticing that someone made contact.
        """
        if not self.configured:
            return [], offset
        status, payload = _request(
            self._url("getUpdates") + f"?offset={offset + 1}&timeout=0&limit=100&allowed_updates=%5B%22message%22%5D",
            timeout=20,
        )
        if status != 200 or not isinstance(payload, dict) or not payload.get("ok"):
            return [], offset

        chats: dict[str, dict[str, Any]] = {}
        highest = offset
        for update in payload.get("result") or []:
            highest = max(highest, int(update.get("update_id") or 0))
            message = update.get("message") or {}
            sender = message.get("from") or {}
            chat = message.get("chat") or {}
            chat_id = chat.get("id")
            if chat_id is None:
                continue
            name = " ".join(
                part for part in (sender.get("first_name"), sender.get("last_name")) if part
            ).strip()
            chats[str(chat_id)] = {
                "chat_id": str(chat_id),
                "name": name or chat.get("title") or "Unknown user",
                "handle": f"@{sender['username']}" if sender.get("username") else "",
            }
        return list(chats.values()), highest


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------


def parse_iso_day(value: Any, fallback: str | None = None) -> str | None:
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except (TypeError, ValueError):
        return fallback
