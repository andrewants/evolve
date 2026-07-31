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
    """Return Monday-aligned counts, training streak and goal streak.

    The current week remains open until it ends: when it has not reached the
    relevant threshold yet, it neither adds to nor breaks a streak earned in
    completed weeks.
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

    def consecutive_weeks(minimum_sessions: int) -> int:
        streak = 0
        offset = 0 if by_offset[0] >= minimum_sessions else 1
        while by_offset[offset] >= minimum_sessions:
            streak += 1
            offset += 1
        return streak

    return {
        "counts": counts,
        # "Training streak" means consecutive weeks with activity. Hitting a
        # configurable goal is useful progress, but changing it must not erase
        # a person's record of consistently showing up.
        "streak": consecutive_weeks(1),
        "goal_streak": consecutive_weeks(goal),
        "goal": goal,
        "current_count": by_offset[0],
    }
