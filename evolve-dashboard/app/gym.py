"""Gym progress derived from locally stored workout history."""

from __future__ import annotations

from collections import Counter
from datetime import date, timedelta
from typing import Any


def gym_summary(
    workouts: list[dict[str, Any]],
    weekly_goal: int,
    today: str,
    *,
    display_weeks: int = 8,
) -> dict[str, Any]:
    """Return Monday-aligned chart counts and the complete active streak.

    The current week remains open until it ends: when it has not reached the
    goal yet, it neither adds to nor breaks a streak earned in completed weeks.
    """
    goal = max(1, int(weekly_goal))
    visible_weeks = max(1, int(display_weeks))
    current_day = date.fromisoformat(today)
    current_monday = current_day - timedelta(days=current_day.weekday())
    by_offset: Counter[int] = Counter()

    for workout in workouts:
        try:
            workout_day = date.fromisoformat(str(workout.get("date") or ""))
        except (AttributeError, TypeError, ValueError):
            continue
        workout_monday = workout_day - timedelta(days=workout_day.weekday())
        offset = (current_monday - workout_monday).days // 7
        if offset >= 0:
            by_offset[offset] += 1

    # Oldest to newest, matching the chart's left-to-right ordering.
    counts = [
        by_offset[offset]
        for offset in range(visible_weeks - 1, -1, -1)
    ]

    streak = 0
    offset = 0 if by_offset[0] >= goal else 1
    while by_offset[offset] >= goal:
        streak += 1
        offset += 1

    return {
        "counts": counts,
        "streak": streak,
        "goal": goal,
        "current_count": by_offset[0],
    }
