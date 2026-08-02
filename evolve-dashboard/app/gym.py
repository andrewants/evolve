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
    week_start: int = 6,
) -> dict[str, Any]:
    """Return week-aligned counts, training streak and goal streak.

    The current week remains open until it ends: when it has not reached the
    relevant threshold yet, it neither adds to nor breaks a streak earned in
    completed weeks.

    `week_start` is 0 for Monday through 6 for Sunday. Where the boundary falls
    changes which week a session belongs to, so a Sunday trainer can score a
    very different streak under Monday-aligned weeks than under Sunday-aligned
    ones — Hevy makes the same day configurable, and the two have to agree for
    the streaks to agree.
    """
    goal = max(1, int(weekly_goal))
    visible_weeks = max(1, int(display_weeks))
    first_day = int(week_start) % 7
    current_day = date.fromisoformat(today)

    def week_of(day: date) -> date:
        return day - timedelta(days=(day.weekday() - first_day) % 7)

    current_week = week_of(current_day)
    by_offset: Counter[int] = Counter()

    for workout in workouts:
        try:
            workout_day = date.fromisoformat(str(workout.get("date") or ""))
        except (AttributeError, TypeError, ValueError):
            continue
        offset = (current_week - week_of(workout_day)).days // 7
        if offset >= 0:
            by_offset[offset] += 1

    # Oldest to newest, matching the chart's left-to-right ordering.
    counts = [
        by_offset[offset]
        for offset in range(visible_weeks - 1, -1, -1)
    ]

    def current_run(minimum_sessions: int) -> tuple[int, int | None]:
        """(weeks in the run, offset of its oldest week)."""
        offset = 0 if by_offset[0] >= minimum_sessions else 1
        length = 0
        while by_offset[offset] >= minimum_sessions:
            length += 1
            offset += 1
        return length, (offset - 1 if length else None)

    # "Training streak" means consecutive weeks with activity. Hitting a
    # configurable goal is useful progress, but changing it must not erase a
    # person's record of consistently showing up.
    streak, streak_oldest = current_run(1)
    goal_streak, _ = current_run(goal)

    # Sessions a week, averaged over the completed weeks the chart shows. The
    # current week is left out: it fills up as the week goes on, and counting
    # it would drop the average every Monday and raise it every Sunday for no
    # change in habit. Weeks before the first logged session are left out too,
    # so someone a fortnight into their history is averaged over that
    # fortnight rather than against six weeks of zeroes they never lived.
    oldest_logged = max(by_offset) if by_offset else -1
    averaged = [
        offset for offset in range(1, visible_weeks) if offset <= oldest_logged
    ]
    weekly_average = (
        round(sum(by_offset[offset] for offset in averaged) / len(averaged), 1)
        if averaged
        else None
    )

    return {
        "counts": counts,
        "streak": streak,
        # The week the run began, so a streak that looks short can be checked
        # against the log instead of taken on faith.
        "streak_since": (
            (current_week - timedelta(weeks=streak_oldest)).isoformat()
            if streak_oldest is not None
            else None
        ),
        # The week that ended the previous run — i.e. the gap that capped the
        # streak. None when the history simply does not reach any further back.
        "streak_broken_week": (
            (current_week - timedelta(weeks=streak_oldest + 1)).isoformat()
            if streak_oldest is not None and any(o > streak_oldest for o in by_offset)
            else None
        ),
        "goal_streak": goal_streak,
        "goal": goal,
        "current_count": by_offset[0],
        # None until a completed week exists to average over.
        "weekly_average": weekly_average,
        "week_start": first_day,
    }
