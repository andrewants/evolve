"""Safe, read-only parser for Zepp Life data export archives."""

from __future__ import annotations

import csv
import io
import zipfile
from datetime import datetime
from typing import Any

MAX_BODY_CSV_BYTES = 50 * 1024 * 1024
MAX_BODY_FILES = 20
MAX_ARCHIVE_ENTRIES = 10_000

ZEPP_FIELDS = {
    "weight": "weight",
    "bmi": "bmi",
    "fatRate": "fat",
    "bodyWaterRate": "water",
    "muscleRate": "muscle",
    "visceralFat": "visceral",
}


def parse_zepp_life_export(archive: bytes) -> tuple[list[dict[str, Any]], int]:
    """Return daily body samples and the number of valid source rows.

    Archives are inspected in memory and never extracted, so member names
    cannot escape the data directory. The optional ``user`` folder and all
    non-body datasets are deliberately ignored.
    """
    if not archive:
        raise ValueError("Choose a Zepp Life export ZIP")
    try:
        zipped = zipfile.ZipFile(io.BytesIO(archive))
    except (zipfile.BadZipFile, OSError):
        raise ValueError("File is not a valid Zepp Life ZIP export") from None

    with zipped:
        if len(zipped.infolist()) > MAX_ARCHIVE_ENTRIES:
            raise ValueError("Zepp Life export contains too many files")
        body_files = [
            info
            for info in zipped.infolist()
            if not info.is_dir() and _is_body_csv(info.filename)
        ]
        if not body_files:
            raise ValueError("No BODY/BODY_*.csv file found in the Zepp Life export")
        if len(body_files) > MAX_BODY_FILES:
            raise ValueError("Zepp Life export contains too many BODY files")

        by_day: dict[str, dict[str, Any]] = {}
        seen_at: dict[tuple[str, str], datetime] = {}
        valid_rows = 0
        for info in body_files:
            if info.flag_bits & 0x1:
                raise ValueError("Encrypted Zepp Life exports are not supported")
            if info.file_size > MAX_BODY_CSV_BYTES:
                raise ValueError("Zepp Life BODY history is too large")
            try:
                raw = zipped.read(info)
                text = raw.decode("utf-8-sig")
            except (OSError, RuntimeError, UnicodeDecodeError, zipfile.BadZipFile):
                raise ValueError("Could not read Zepp Life BODY history") from None

            reader = csv.DictReader(io.StringIO(text))
            if not reader.fieldnames:
                continue
            reader.fieldnames = [str(name or "").strip() for name in reader.fieldnames]
            if not {"time", "weight"}.issubset(reader.fieldnames):
                continue

            for row in reader:
                stamp = _timestamp(row.get("time"))
                weight = _positive_float(row.get("weight"))
                if stamp is None or weight is None:
                    continue
                valid_rows += 1
                day = stamp.date().isoformat()
                sample = by_day.setdefault(day, {"date": day})
                # The last valid reading of the day wins for each available
                # field. Zepp uses zero/null when composition was not measured.
                _set_latest(sample, seen_at, day, "weight", weight, stamp)
                for zepp_key, momentum_key in ZEPP_FIELDS.items():
                    if zepp_key == "weight":
                        continue
                    value = _positive_float(row.get(zepp_key))
                    if value is not None:
                        _set_latest(sample, seen_at, day, momentum_key, value, stamp)

        if not by_day:
            raise ValueError("No valid body measurements found in the Zepp Life export")
        return [by_day[day] for day in sorted(by_day)], valid_rows


def _is_body_csv(name: str) -> bool:
    parts = [part for part in name.replace("\\", "/").split("/") if part]
    if not parts or any(part.lower() in {"__macosx", "user"} for part in parts):
        return False
    filename = parts[-1].upper()
    return len(parts) >= 2 and parts[-2].upper() == "BODY" and filename.startswith("BODY_") and filename.endswith(".CSV")


def _timestamp(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    for pattern in ("%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(raw, pattern)
        except ValueError:
            continue
    return None


def _set_latest(
    sample: dict[str, Any],
    seen_at: dict[tuple[str, str], datetime],
    day: str,
    key: str,
    value: float,
    stamp: datetime,
) -> None:
    marker = (day, key)
    previous = seen_at.get(marker)
    if previous is None or stamp >= previous:
        sample[key] = value
        seen_at[marker] = stamp


def _positive_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed <= 0 or parsed != parsed or parsed in (float("inf"), float("-inf")):
        return None
    return parsed
