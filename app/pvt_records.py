"""Persist a completed test once when the client retries a lost response."""

from fastapi import HTTPException
from app import models


def save_pvt_result(db, data):
    try:
        user = db.query(models.User).filter(
            models.User.user_id == data.user_id
        ).with_for_update().first()
        if user is None:
            raise HTTPException(404, detail="user_not_found")
        entry = db.query(models.UsersPVTResults).filter(
            models.UsersPVTResults.user_id == data.user_id,
            models.UsersPVTResults.test_at == data.test_at,
            models.UsersPVTResults.device == data.device,
        ).order_by(models.UsersPVTResults.id).first()
        if entry is None:
            entry = models.UsersPVTResults(**data.model_dump())
            db.add(entry)
            db.flush()
        else:
            entry.kss_level = data.kss_level
        entry_id = entry.id
        db.commit()
        return entry_id
    except Exception:
        db.rollback()
        raise
