"""Replace a day's records atomically, including safe retries after a timeout."""

from datetime import datetime, time, timedelta, timezone

from fastapi import HTTPException

from app import models
from core.period_validation import overlap_detail


TAIPEI = timezone(timedelta(hours=8))


def replace_day_records(db, request, kind):
    if kind == "sleep":
        model = models.UsersRealSleepData
        other = models.UsersTargetWakingPeriod
        start_name, end_name = "sleep_start_time", "sleep_end_time"
        other_start, other_end = "target_start_time", "target_end_time"
        day_field = model.sleep_end_time
    else:
        model = models.UsersTargetWakingPeriod
        other = models.UsersRealSleepData
        start_name, end_name = "target_start_time", "target_end_time"
        other_start, other_end = "sleep_start_time", "sleep_end_time"
        day_field = model.target_start_time

    try:
        # Serialize replacements for one user, even from two devices.
        user = db.query(models.User).filter(
            models.User.user_id == request.user_id
        ).with_for_update().first()
        if user is None:
            raise HTTPException(404, detail="user_not_found")

        periods = []
        for period in request.periods:
            start, end = period.start, period.end
            if start.utcoffset() is None or end.utcoffset() is None:
                raise HTTPException(400, detail="timezone_required")
            if not start < end or end - start > timedelta(hours=48):
                raise HTTPException(400, detail="invalid_time_range")
            anchor = end if kind == "sleep" else start
            if anchor.astimezone(TAIPEI).date() != request.day:
                raise HTTPException(400, detail="record_date_mismatch")
            periods.append((start.astimezone(timezone.utc), end.astimezone(timezone.utc)))
        periods.sort()
        if any(current[0] < previous[1] for previous, current in zip(periods, periods[1:])):
            raise HTTPException(400, detail="periods_overlap")

        begin = datetime.combine(request.day, time.min, TAIPEI).astimezone(timezone.utc)
        finish = begin + timedelta(days=1)
        active = db.query(model).filter(
            model.user_id == request.user_id,
            model.is_active.is_(True),
            model.deleted_at.is_(None),
            model.invalidated_at.is_(None),
        )
        old = active.filter(day_field >= begin, day_field < finish).order_by(model.id).all()

        for start, end in periods:
            conflict = db.query(other).filter(
                other.user_id == request.user_id,
                other.is_active.is_(True),
                other.deleted_at.is_(None),
                other.invalidated_at.is_(None),
                getattr(other, other_start) < end,
                getattr(other, other_end) > start,
            ).first()
            if conflict:
                detail = overlap_detail(start, end, conflict.target_start_time, conflict.target_end_time, wake_id=conflict.id) if kind == "sleep" else overlap_detail(conflict.sleep_start_time, conflict.sleep_end_time, start, end, sleep_id=conflict.id)
                raise HTTPException(400, detail=detail)
            outside_conflict = active.filter(
                ~model.id.in_([entry.id for entry in old]),
                getattr(model, start_name) < end,
                getattr(model, end_name) > start,
            ).first()
            if outside_conflict:
                raise HTTPException(400, detail="periods_overlap")

        # Same payload after a lost response must not create more records.
        def utc(value):
            return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)

        old_periods = sorted((utc(getattr(row, start_name)), utc(getattr(row, end_name))) for row in old)
        if old_periods == periods:
            ids = [row.id for row in old]
            db.commit()
            return {"status": "success", "ids": ids, "changed": False}

        now = datetime.now(timezone.utc)
        for entry in old:
            entry.is_active = False
            entry.invalidated_at = now
            entry.updated_at = now
        new = []
        for index, (start, end) in enumerate(periods):
            entry = model(
                user_id=request.user_id,
                **{start_name: start, end_name: end},
                created_at=now,
                updated_at=now,
                is_active=True,
                edited_from_id=old[index].id if index < len(old) else None,
            )
            db.add(entry)
            new.append(entry)
        db.query(models.RecommendationsCaffeine).filter(
            models.RecommendationsCaffeine.user_id == request.user_id,
            models.RecommendationsCaffeine.is_active.is_(True),
            models.RecommendationsCaffeine.recommended_caffeine_intake_timing >= now,
        ).update({"is_active": False}, synchronize_session=False)
        db.flush()
        ids = [entry.id for entry in new]
        db.commit()
        return {"status": "success", "ids": ids, "changed": True}
    except Exception:
        db.rollback()
        raise
