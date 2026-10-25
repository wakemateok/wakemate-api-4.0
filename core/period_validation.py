"""Shared half-open interval checks; adjacent periods are not conflicts."""

from datetime import datetime, timezone


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def first_sleep_wake_overlap(sleeps, wakes):
    for sleep_index, (sleep_start, sleep_end) in enumerate(sleeps):
        for wake_index, (wake_start, wake_end) in enumerate(wakes):
            if utc(wake_start) < utc(sleep_end) and utc(wake_end) > utc(sleep_start):
                return sleep_index, wake_index
    return None


def first_recommendation_overlap(sleeps, wakes, *, now=None):
    """Only unfinished wake windows can block current/future recommendations."""
    cutoff = utc(now or datetime.now(timezone.utc))
    relevant = [(index, period) for index, period in enumerate(wakes) if utc(period[1]) > cutoff]
    conflict = first_sleep_wake_overlap(sleeps, [period for _, period in relevant])
    if conflict is None:
        return None
    return conflict[0], relevant[conflict[1]][0]


def overlap_detail(sleep_start, sleep_end, wake_start, wake_end, *, sleep_id=None, wake_id=None):
    return {
        "type": "sleep_wake_overlap",
        "message": "Sleep time and target wake time cannot overlap.",
        "conflicts": [{
            "sleep": {"id": sleep_id, "start": utc(sleep_start).isoformat(), "end": utc(sleep_end).isoformat()},
            "wake": {"id": wake_id, "start": utc(wake_start).isoformat(), "end": utc(wake_end).isoformat()},
        }],
    }
