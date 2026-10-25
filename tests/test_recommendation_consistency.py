import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

os.environ['DATABASE_URL'] = 'sqlite://'
os.environ['RUN_BACKGROUND_JOBS'] = 'false'

from sqlalchemy.orm import sessionmaker
from app import models
from app.main import trigger_calculation
from core.period_validation import first_sleep_wake_overlap
from core.caffeine_recommendation import run_caffeine_recommendation
import test_auth_authorization as authorization

ZONE = timezone(timedelta(hours=8))


class RecommendationConsistencyTests(unittest.TestCase):
    setUp = authorization.AuthorizationTests.setUp
    tearDown = authorization.AuthorizationTests.tearDown
    login = authorization.AuthorizationTests.login

    def seed_conflict(self):
        self.day = (datetime.now(ZONE) + timedelta(days=2)).date()
        def at(hour, minute=0, offset=0):
            return datetime.combine(self.day + timedelta(days=offset), datetime.min.time(), ZONE).replace(hour=hour, minute=minute).astimezone(timezone.utc)
        self.sleep = models.UsersRealSleepData(user_id=self.a,
            sleep_start_time=at(8), sleep_end_time=at(16, 30))
        self.wake = models.UsersTargetWakingPeriod(user_id=self.a,
            target_start_time=at(16), target_end_time=at(18))
        self.rec = models.RecommendationsCaffeine(user_id=self.a,
            recommended_caffeine_amount=50,
            recommended_caffeine_intake_timing=datetime.now(timezone.utc) + timedelta(days=2))
        self.db.add_all([self.sleep, self.wake, self.rec])
        self.db.commit()

    def test_manual_failure_includes_the_actual_conflicting_records(self):
        self.seed_conflict()
        result = self.client.post('/calculate/', headers=self.headers)
        self.assertEqual(result.status_code, 400)
        detail = result.json()['detail']
        self.assertEqual(detail['type'], 'sleep_wake_overlap')
        conflict = detail['conflicts'][0]
        self.assertEqual(conflict['sleep']['id'], self.sleep.id)
        self.assertEqual(conflict['wake']['id'], self.wake.id)
        self.assertEqual(datetime.fromisoformat(conflict['sleep']['end']).astimezone(ZONE).hour, 16)
        self.db.expire_all()
        self.assertFalse(self.rec.is_active)

    def test_reopening_home_cannot_read_active_recommendations_after_a_conflict(self):
        self.seed_conflict()
        result = self.client.get('/recommendations/', headers=self.headers)
        self.assertEqual(result.status_code, 400)
        self.assertEqual(result.json()['detail']['type'], 'sleep_wake_overlap')
        self.assertNotIn('recommended_caffeine_amount', result.text)

    def test_saved_valid_new_day_reports_saved_but_calculation_blocked(self):
        self.seed_conflict()
        self.calc_patch.stop()
        day = (self.day + timedelta(days=1)).isoformat()
        with patch('app.main.SessionLocal', sessionmaker(bind=self.engine)), patch('app.main.get_db_connection') as connection:
            result = self.client.post('/users_wake/replace_day', headers=self.headers, json={
                'user_id': str(self.a), 'day': day, 'periods': [{
                    'start': day + 'T16:00:00+08:00', 'end': day + 'T18:00:00+08:00'}]})
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()['status'], 'success')
            self.assertEqual(result.json()['calculation']['status'], 'blocked')
            self.assertEqual(result.json()['calculation']['detail']['type'], 'sleep_wake_overlap')
            connection.assert_not_called()
        self.assertTrue(self.db.get(models.UsersTargetWakingPeriod, result.json()['ids'][0]).is_active)

    def test_new_overlapping_input_is_rejected_atomically_with_conflict_details(self):
        self.seed_conflict()
        day = self.day.isoformat()
        result = self.client.post('/users_wake/replace_day', headers=self.headers, json={
            'user_id': str(self.a), 'day': day, 'periods': [{
                'start': day + 'T15:00:00+08:00', 'end': day + 'T17:00:00+08:00'}]})
        self.assertEqual(result.status_code, 400)
        self.assertEqual(result.json()['detail']['conflicts'][0]['sleep']['id'], self.sleep.id)
        self.assertEqual(self.db.query(models.UsersTargetWakingPeriod).count(), 1)

    def test_invalidated_sleep_does_not_cause_a_false_conflict(self):
        self.seed_conflict()
        self.sleep.is_active = False
        self.sleep.invalidated_at = datetime.now(timezone.utc)
        self.db.add(models.UsersRealSleepData(user_id=self.a,
            sleep_start_time=datetime.combine(self.day - timedelta(days=1), datetime.min.time(), ZONE).replace(hour=23).astimezone(timezone.utc),
            sleep_end_time=datetime.combine(self.day, datetime.min.time(), ZONE).replace(hour=7).astimezone(timezone.utc)))
        self.db.commit()
        self.assertEqual(self.client.get('/recommendations/', headers=self.headers).status_code, 200)

    def test_adjacent_sleep_end_and_wake_start_are_allowed(self):
        self.seed_conflict()
        self.sleep.sleep_end_time = self.wake.target_start_time
        self.db.commit()
        self.assertEqual(self.client.post('/calculate/', headers=self.headers).status_code, 200)


class PeriodValidationTests(unittest.TestCase):
    def test_one_hundred_valid_boundaries_and_actual_overlaps(self):
        start = datetime(2026, 10, 25, tzinfo=ZONE)
        for index in range(100):
            end = start + timedelta(minutes=480 + index)
            self.assertIsNone(first_sleep_wake_overlap([(start, end)], [(end, end + timedelta(hours=2))]))
            self.assertEqual(first_sleep_wake_overlap([(start, end)], [(end - timedelta(seconds=1), end + timedelta(hours=2))]), (0, 0))

    def test_scheduled_core_never_generates_from_conflicting_inputs(self):
        start = datetime(2026, 10, 25, tzinfo=ZONE)
        class Cursor:
            def __init__(self): self.sql = []
            def execute(self, sql, params=None): self.sql.append(sql)
            def fetchall(self):
                return [('user', start, start + timedelta(hours=8))] if 'sleep_start_time' in self.sql[-1] else [('user', start + timedelta(hours=7), start + timedelta(hours=9))]
            def close(self): pass
        class Connection:
            def __init__(self): self.cur = Cursor(); self.commits = 0
            def cursor(self): return self.cur
            def commit(self): self.commits += 1
            def rollback(self): raise AssertionError('unexpected rollback')
        conn = Connection()
        with patch('core.caffeine_recommendation.execute_values') as write:
            result = run_caffeine_recommendation(conn, {}, only_user_id='user', reference_time=start)
            self.assertEqual(result['inserted_recommendations'], 0)
            write.assert_not_called()
        self.assertTrue(any('is_active = FALSE' in sql for sql in conn.cur.sql))
        self.assertEqual(conn.commits, 1)
