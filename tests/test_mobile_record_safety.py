import os
import unittest
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4
from unittest.mock import patch

os.environ["DATABASE_URL"] = "sqlite://"

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from pydantic import ValidationError
from app.database import Base
from app import models, schemas
from app.day_records import replace_day_records
from app.pvt_records import save_pvt_result


DAY = date(2026, 10, 20)
ZONE = timezone(timedelta(hours=8))


def at(hour, day=20):
    return datetime(2026, 10, day, hour, tzinfo=ZONE)


class MobileRecordSafetyTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.user_id = uuid4()
        self.db.add(models.User(user_id=self.user_id, hashed_password="test-only"))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def request(self, *periods, day=DAY, user_id=None):
        return schemas.ReplaceDayRecords(
            user_id=user_id or self.user_id,
            day=day,
            periods=[{"start": start, "end": end} for start, end in periods],
        )

    def active(self, model):
        return self.db.query(model).filter(model.is_active.is_(True)).all()

    def test_same_payload_retry_keeps_same_ids(self):
        payload = self.request((at(23, 19), at(7)))
        first = replace_day_records(self.db, payload, "sleep")
        retry = replace_day_records(self.db, payload, "sleep")
        self.assertEqual(first["ids"], retry["ids"])
        self.assertFalse(retry["changed"])
        self.assertEqual(self.db.query(models.UsersRealSleepData).count(), 1)

    def test_sleep_replacement_preserves_history(self):
        old = replace_day_records(self.db, self.request((at(23, 19), at(7))), "sleep")
        new = replace_day_records(self.db, self.request((at(22, 19), at(8))), "sleep")
        active = self.active(models.UsersRealSleepData)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].id, new["ids"][0])
        self.assertEqual(active[0].edited_from_id, old["ids"][0])

    def test_conflicting_sleep_preserves_original(self):
        old = replace_day_records(self.db, self.request((at(23, 19), at(7))), "sleep")
        replace_day_records(self.db, self.request((at(9), at(11))), "wake")
        with self.assertRaises(HTTPException):
            replace_day_records(self.db, self.request((at(23, 19), at(10))), "sleep")
        self.assertEqual([row.id for row in self.active(models.UsersRealSleepData)], old["ids"])

    def test_one_invalid_wake_slot_preserves_entire_original_set(self):
        original = replace_day_records(self.db, self.request((at(10), at(11)), (at(14), at(15))), "wake")
        replace_day_records(self.db, self.request((at(23, 19), at(8))), "sleep")
        with self.assertRaises(HTTPException):
            replace_day_records(self.db, self.request((at(12), at(13)), (at(6), at(9))), "wake")
        self.assertEqual([row.id for row in self.active(models.UsersTargetWakingPeriod)], original["ids"])

    def test_database_failure_rolls_back_old_and_new(self):
        original = replace_day_records(self.db, self.request((at(10), at(11))), "wake")
        with patch.object(self.db, "commit", side_effect=RuntimeError("simulated write failure")):
            with self.assertRaises(RuntimeError):
                replace_day_records(self.db, self.request((at(12), at(13))), "wake")
        self.assertEqual([row.id for row in self.active(models.UsersTargetWakingPeriod)], original["ids"])
        self.assertEqual(self.db.query(models.UsersTargetWakingPeriod).count(), 1)

    def test_replacement_does_not_touch_other_days(self):
        first = replace_day_records(self.db, self.request((at(9), at(10))), "wake")
        replace_day_records(self.db, self.request((at(9, 21), at(10, 21)), day=date(2026, 10, 21)), "wake")
        replace_day_records(self.db, self.request((at(11, 21), at(12, 21)), day=date(2026, 10, 21)), "wake")
        self.assertIn(first["ids"][0], [row.id for row in self.active(models.UsersTargetWakingPeriod)])
        self.assertEqual(len(self.active(models.UsersTargetWakingPeriod)), 2)

    def test_taipei_day_boundary_and_cross_midnight(self):
        original = replace_day_records(self.db, self.request((at(0), at(2))), "wake")
        new = replace_day_records(self.db, self.request((at(22), at(2, 21))), "wake")
        self.assertNotEqual(original["ids"], new["ids"])
        self.assertEqual(len(self.active(models.UsersTargetWakingPeriod)), 1)

    def test_mismatched_day_rejected(self):
        with self.assertRaises(HTTPException):
            replace_day_records(self.db, self.request((at(9, 21), at(10, 21))), "wake")
        self.assertEqual(self.db.query(models.UsersTargetWakingPeriod).count(), 0)

    def test_naive_time_rejected(self):
        with self.assertRaises(HTTPException):
            replace_day_records(self.db, self.request((at(9).replace(tzinfo=None), at(10))), "wake")

    def test_overlapping_slots_rejected(self):
        with self.assertRaises(HTTPException):
            replace_day_records(self.db, self.request((at(9), at(12)), (at(11), at(13))), "wake")

    def test_neighbor_day_overlap_rejected(self):
        original = replace_day_records(self.db, self.request((at(22, 19), at(2)), day=date(2026, 10, 19)), "wake")
        with self.assertRaises(HTTPException):
            replace_day_records(self.db, self.request((at(1), at(3))), "wake")
        self.assertEqual([row.id for row in self.active(models.UsersTargetWakingPeriod)], original["ids"])

    def test_unknown_user_rejected(self):
        with self.assertRaises(HTTPException):
            replace_day_records(self.db, self.request((at(9), at(10)), user_id=uuid4()), "wake")

    def test_empty_payload_is_not_an_accidental_delete(self):
        with self.assertRaises(ValidationError):
            self.request()

    def test_changed_source_invalidates_future_recommendations(self):
        self.db.add(models.RecommendationsCaffeine(
            user_id=self.user_id,
            recommended_caffeine_amount=25,
            recommended_caffeine_intake_timing=datetime.now(timezone.utc) + timedelta(days=2),
        ))
        self.db.commit()
        replace_day_records(self.db, self.request((at(9), at(10))), "wake")
        self.assertEqual(len(self.active(models.RecommendationsCaffeine)), 0)

    def test_one_hundred_replacements_and_retries(self):
        for index in range(100):
            start = at(9) + timedelta(minutes=index)
            payload = self.request((start, start + timedelta(hours=1)))
            first = replace_day_records(self.db, payload, "wake")
            retry = replace_day_records(self.db, payload, "wake")
            self.assertEqual(first["ids"], retry["ids"])
            self.assertEqual(len(self.active(models.UsersTargetWakingPeriod)), 1)

    def pvt(self, device="Mobile", timestamp=None):
        return schemas.UsersPVTResultsCreate(
            user_id=self.user_id, mean_rt=320, lapses=1, false_starts=0,
            kss_level=3, device=device, test_at=timestamp or at(10),
        )

    def test_pvt_retry_does_not_duplicate_measurements(self):
        payload = self.pvt()
        self.assertEqual(save_pvt_result(self.db, payload), save_pvt_result(self.db, payload))
        self.assertEqual(self.db.query(models.UsersPVTResults).count(), 1)

    def test_two_test_versions_remain_distinct(self):
        save_pvt_result(self.db, self.pvt())
        save_pvt_result(self.db, self.pvt(device="Mobile PVT-B 3min"))
        self.assertEqual(self.db.query(models.UsersPVTResults).count(), 2)

    def test_new_test_keeps_new_measurement(self):
        save_pvt_result(self.db, self.pvt())
        save_pvt_result(self.db, self.pvt(timestamp=at(11)))
        self.assertEqual(self.db.query(models.UsersPVTResults).count(), 2)


if __name__ == "__main__":
    unittest.main()
