"""Taipei civil time for model hours and daily recommendation accounting."""

from datetime import timedelta, timezone

TAIPEI = timezone(timedelta(hours=8))


def taipei_time(value):
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(TAIPEI)
