"""Thin read-only client for the Home Assistant core API.

The Supervisor injects SUPERVISOR_TOKEN into the add-on and proxies core
requests at http://supervisor/core/api, so no user-supplied credentials are
involved. Only entity states listed in the add-on options are read.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

LOGGER = logging.getLogger("evolve.ha")

CORE_API = "http://supervisor/core/api"
CACHE_TTL_SECONDS = 20
REQUEST_TIMEOUT_SECONDS = 8


class HomeAssistant:
    def __init__(self, token: str | None) -> None:
        self._token = token or ""
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}

    @property
    def available(self) -> bool:
        return bool(self._token)

    def state(self, entity_id: str) -> dict[str, Any]:
        """Return {state, unit, friendly_name, ok} for one entity."""
        if not self.available:
            return _unavailable(entity_id, "no supervisor token")

        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(entity_id)
            if cached and now - cached[0] < CACHE_TTL_SECONDS:
                return cached[1]

        result = self._fetch(entity_id)
        with self._lock:
            self._cache[entity_id] = (now, result)
        return result

    def _fetch(self, entity_id: str) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{CORE_API}/states/{urllib.parse.quote(entity_id)}",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            reason = "not found" if error.code == 404 else f"HTTP {error.code}"
            return _unavailable(entity_id, reason)
        except (urllib.error.URLError, TimeoutError, ValueError) as error:
            LOGGER.debug("Could not read %s: %s", entity_id, error)
            return _unavailable(entity_id, "unreachable")

        attributes = payload.get("attributes") or {}
        return {
            "entity_id": entity_id,
            "state": payload.get("state"),
            "unit": attributes.get("unit_of_measurement") or "",
            "friendly_name": attributes.get("friendly_name") or entity_id,
            "ok": payload.get("state") not in (None, "unknown", "unavailable"),
        }


def _unavailable(entity_id: str, reason: str) -> dict[str, Any]:
    return {
        "entity_id": entity_id,
        "state": None,
        "unit": "",
        "friendly_name": entity_id,
        "ok": False,
        "reason": reason,
    }
