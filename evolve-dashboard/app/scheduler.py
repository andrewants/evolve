"""Background worker: data sync, Telegram polling, reminders, milestones.

One daemon thread ticks every 30 seconds and decides what is due. Each job
is wrapped so a failing integration can never kill the loop.
"""

from __future__ import annotations

import html
import logging
import threading
from datetime import datetime

from integrations import Hevy, Telegram
from storage import METRIC_KEYS, Store, days_since, today_iso

LOGGER = logging.getLogger("momentum.scheduler")

TICK_SECONDS = 30
WEIGHT_SYNC_SECONDS = 600
HEVY_SYNC_SECONDS = 3600
TELEGRAM_POLL_SECONDS = 60

# Counter anniversaries worth a nudge.
MILESTONE_DAYS = (7, 14, 30, 60, 100, 180, 270, 365, 500, 730)


class Scheduler:
    def __init__(self, store: Store, hass, settings) -> None:
        self.store = store
        self.hass = hass
        self.settings = settings
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last: dict[str, float] = {}
        self._sent_reminders: set[str] = set()

    # -- lifecycle ------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="momentum-scheduler", daemon=True)
        self._thread.start()
        LOGGER.info("Scheduler started")

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        # Give the HTTP server a moment before the first sync burst.
        self._stop.wait(5)
        while not self._stop.is_set():
            self._tick()
            self._stop.wait(TICK_SECONDS)

    def _due(self, key: str, period: float) -> bool:
        now = datetime.now().timestamp()
        if now - self._last.get(key, 0) < period:
            return False
        self._last[key] = now
        return True

    def _tick(self) -> None:
        for name, period, job in (
            ("weight", WEIGHT_SYNC_SECONDS, self.sync_weight),
            ("hevy", HEVY_SYNC_SECONDS, self.sync_hevy),
            ("telegram", TELEGRAM_POLL_SECONDS, self.poll_telegram),
        ):
            if self._due(name, period):
                self._guard(name, job)
        # Reminders and milestones are cheap; check them every tick so a
        # configured time is never missed by more than one interval.
        self._guard("reminders", self.run_reminders)
        self._guard("milestones", self.check_milestones)

    def _guard(self, name: str, job) -> None:
        try:
            job()
        except Exception:  # pragma: no cover - defensive
            LOGGER.exception("Scheduled job %s failed", name)

    # -- jobs -----------------------------------------------------------

    def sync_weight(self) -> int:
        """Copy current Mi-scale readings into our own history."""
        if not self.hass.available:
            return 0
        written = 0
        for user in self.store.users():
            entities = user["settings"]["entities"]
            weight_entity = entities.get("weight")
            if not weight_entity:
                continue

            reading = self.hass.state(weight_entity)
            if not reading["ok"] or reading["value"] is None:
                continue

            sample = {"date": _day_of(reading["last_changed"]), "weight": reading["value"]}
            for key in METRIC_KEYS:
                if key == "weight":
                    continue
                entity = entities.get(key)
                if not entity:
                    continue
                metric = self.hass.state(entity)
                if metric["ok"]:
                    sample[key] = metric["value"]

            if self.store.record_weight_sample(user["id"], sample):
                written += 1

            steps_entity = entities.get("steps")
            if steps_entity:
                steps = self.hass.state(steps_entity)
                if steps["ok"] and steps["value"] is not None:
                    self.store.record_steps(user["id"], today_iso(), int(steps["value"]))
        return written

    def backfill_weight(self, user_id: str, days: int = 180) -> int:
        """Pull body-composition history out of the HA recorder once."""
        user = self.store.user(user_id)
        if user is None or not self.hass.available:
            return 0
        entities = user["settings"]["entities"]
        if not entities.get("weight"):
            return 0

        series: dict[str, dict[str, float]] = {}
        for key in METRIC_KEYS:
            entity = entities.get(key)
            if not entity:
                continue
            for point in self.hass.history(entity, days=days):
                series.setdefault(point["date"], {})[key] = point["value"]

        written = 0
        for day, values in sorted(series.items()):
            if "weight" not in values:
                continue
            if self.store.record_weight_sample(user_id, {"date": day, **values}):
                written += 1
        LOGGER.info("Backfilled %d weight samples for %s", written, user_id)
        return written

    def sync_hevy(self) -> int:
        synced = 0
        for user in self.store.users():
            key = user["settings"].get("hevy_key")
            if not key:
                continue
            workouts, error = Hevy(key).workouts()
            if error:
                LOGGER.warning("Hevy sync for %s: %s", user["name"], error)
                continue
            if workouts:
                self.store.replace_workouts(user["id"], workouts)
                synced += 1
        return synced

    def poll_telegram(self) -> int:
        config = self.store.telegram_config()
        bot = Telegram(config.get("token", ""))
        if not bot.configured:
            return 0
        chats, offset = bot.updates(int(config.get("last_update_id") or 0))
        for chat in chats:
            self.store.upsert_bot_user(chat["chat_id"], chat["name"], chat["handle"])
        if offset != int(config.get("last_update_id") or 0):
            self.store.set_telegram_offset(offset)
        return len(chats)

    def run_reminders(self) -> int:
        """Send each member's affirmation at their configured local time."""
        now = datetime.now()
        stamp = now.strftime("%Y-%m-%d %H:%M")
        # Drop yesterday's keys so the set cannot grow without bound.
        today_prefix = now.strftime("%Y-%m-%d")
        self._sent_reminders = {k for k in self._sent_reminders if k.startswith(today_prefix)}

        sent = 0
        for user in self.store.users():
            reminder = user["settings"]["reminder"]
            if not reminder.get("on"):
                continue
            if reminder.get("time") != now.strftime("%H:%M"):
                continue
            key = f"{stamp}:{user['id']}"
            if key in self._sent_reminders:
                continue

            pinned = next((a for a in user["affirmations"] if a.get("pinned")), None)
            if pinned is None and user["affirmations"]:
                pinned = user["affirmations"][0]
            if pinned is None:
                continue

            body = f"<b>{html.escape(user['name'])}</b>\n\n“{html.escape(pinned['text'])}”"
            if self._notify(user, body):
                sent += 1
            self._sent_reminders.add(key)
        return sent

    def check_milestones(self) -> int:
        """Congratulate a counter as it passes a round number of days."""
        sent = 0
        for user in self.store.users():
            for counter in user["counters"]:
                elapsed = days_since(counter["date"])
                for milestone in MILESTONE_DAYS:
                    if elapsed != milestone:
                        continue
                    key = f"{counter['id']}:{milestone}"
                    if not self.store.mark_milestone(user["id"], key):
                        continue
                    body = (
                        f"🎉 <b>{html.escape(user['name'])}</b> — "
                        f"{milestone} days of <b>{html.escape(counter['name'])}</b>."
                    )
                    if self._notify(user, body):
                        sent += 1
        return sent

    # -- notification ---------------------------------------------------

    def _notify(self, user: dict, text: str) -> bool:
        """Send to the member's own chat, else every approved chat."""
        config = self.store.telegram_config()
        bot = Telegram(config.get("token", ""))
        if not bot.configured:
            return False

        target = user["settings"].get("telegram_chat_id") or config.get("default_chat")
        recipients = [target] if target else self.store.approved_chat_ids()
        delivered = False
        for chat_id in recipients:
            ok, error = bot.send(chat_id, text)
            delivered = delivered or ok
            if not ok:
                LOGGER.warning("Telegram send to %s failed: %s", chat_id, error)
        return delivered


def _day_of(stamp: str | None) -> str:
    if not stamp:
        return today_iso()
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone().date().isoformat()
    except (AttributeError, ValueError):
        return today_iso()
