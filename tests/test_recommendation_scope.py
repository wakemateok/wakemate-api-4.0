import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

os.environ['DATABASE_URL'] = 'sqlite://'
os.environ['RUN_BACKGROUND_JOBS'] = 'false'

from sqlalchemy.orm import sessionmaker
from app import models
from core.caffeine_recommendation import run_caffeine_recommendation
from core.period_validation import first_recommendation_overlap
import test_auth_authorization as authorization

ZONE = timezone(timedelta(hours=8))


class RecommendationScopeTests(unittest.TestCase):
    setUp = authorization.AuthorizationTests.setUp
    tearDown = authorization.AuthorizationTests.tearDown
    login = authorization.AuthorizationTests.login

    def seed_history_and_future(self):
        day = datetime.now(ZONE).replace(hour=0, minute=0, second=0, microsecond=0)
        old = day - timedelta(days=10)
        def utc(value): return value.astimezone(timezone.utc)
        self.old_sleep = models.UsersRealSleepData(user_id=self.a,
            sleep_start_time=utc(old + timedelta(hours=23)), sleep_end_time=utc(old + timedelta(days=1, hours=5)))
        self.old_wake = models.UsersTargetWakingPeriod(user_id=self.a,
            target_start_time=utc(old + timedelta(hours=22, minutes=30)),
            target_end_time=utc(old + timedelta(hours=23, minutes=30)))
        future = day + timedelta(days=2)
        self.future_day = future.date()
        self.current_sleep = models.UsersRealSleepData(user_id=self.a,
            sleep_start_time=utc(future + timedelta(hours=2)), sleep_end_time=utc(future + timedelta(hours=10)))
        self.current_wake = models.UsersTargetWakingPeriod(user_id=self.a,
            target_start_time=utc(future + timedelta(hours=16)), target_end_time=utc(future + timedelta(hours=18)))
        self.db.add_all([self.old_sleep, self.old_wake, self.current_sleep, self.current_wake])
        self.db.commit()

    def assert_history_preserved(self):
        self.db.expire_all()
        for row in [self.old_sleep, self.old_wake]:
            self.assertTrue(row.is_active)
            self.assertIsNone(row.deleted_at)
            self.assertIsNone(row.invalidated_at)

    def test_past_conflict_does_not_block_current_recommendation_reads(self):
        self.seed_history_and_future()
        result = self.client.get('/recommendations/', headers=self.headers)
        self.assertEqual(result.status_code, 200, result.text)
        self.assert_history_preserved()

    def test_past_conflict_does_not_block_manual_calculation(self):
        self.seed_history_and_future()
        result = self.client.post('/calculate/', headers=self.headers)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()['status'], 'ok')
        self.assert_history_preserved()

    def test_manual_retry_forces_a_real_recalculation(self):
        self.seed_history_and_future()
        with patch('app.main.trigger_calculation', return_value={'status': 'ok'}) as calculate:
            result = self.client.post('/calculate/', headers=self.headers)
            self.assertEqual(result.status_code, 200, result.text)
            calculate.assert_called_once_with(self.a, force=True)

    def test_save_trigger_does_not_block_on_unrelated_history(self):
        self.seed_history_and_future()
        self.calc_patch.stop()
        day = self.future_day
        with patch('app.main.SessionLocal', sessionmaker(bind=self.engine)), \
             patch('app.main.get_db_connection') as connection, \
             patch('app.main.run_caffeine_recommendation', return_value={}), \
             patch('app.main.run_alertness_data', return_value={}):
            result = self.client.post('/users_wake/replace_day', headers=self.headers, json={
                'user_id': str(self.a), 'day': day.isoformat(), 'periods': [{
                    'start': day.isoformat() + 'T16:00:00+08:00',
                    'end': day.isoformat() + 'T18:30:00+08:00'}]})
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()['calculation']['status'], 'ok')
            connection.assert_called_once()
        self.assert_history_preserved()

    def test_current_conflict_still_blocks_reads_and_calculation(self):
        self.seed_history_and_future()
        self.current_sleep.sleep_end_time = self.current_wake.target_start_time + timedelta(minutes=30)
        self.db.commit()
        for method in ('get', 'post'):
            path = '/recommendations/' if method == 'get' else '/calculate/'
            result = getattr(self.client, method)(path, headers=self.headers)
            self.assertEqual(result.status_code, 400, result.text)
            conflict = result.json()['detail']['conflicts'][0]
            self.assertEqual(conflict['sleep']['id'], self.current_sleep.id)
            self.assertEqual(conflict['wake']['id'], self.current_wake.id)


class CoreHistoryScopeTests(unittest.TestCase):
    def test_past_overlap_does_not_change_future_numeric_recommendations(self):
        now = datetime.now(timezone.utc)
        day = (now + timedelta(days=2)).replace(hour=0, minute=0, second=0, microsecond=0)
        sleeps = [('user', day - timedelta(days=10, hours=1), day - timedelta(days=9, hours=19)),
                  ('user', day + timedelta(hours=2), day + timedelta(hours=10))]
        current = ('user', day + timedelta(hours=16), day + timedelta(hours=18))
        past = ('user', sleeps[0][1] - timedelta(minutes=30), sleeps[0][1] + timedelta(minutes=30))

        class Cursor:
            def execute(self, sql, params=None): self.sql = sql
            def fetchall(self):
                if 'SELECT user_id, target_start_time' in self.sql: return self.wakes
                if 'SELECT user_id, sleep_start_time' in self.sql: return sleeps
                return []
            def fetchone(self): return (now,)
            def close(self): pass

        class Connection:
            def __init__(self, wakes): self.cur = Cursor(); self.cur.wakes = wakes
            def cursor(self): return self.cur
            def commit(self): pass
            def rollback(self): pass

        def run(wakes, *, force=False, already_processed=False):
            with patch('core.caffeine_recommendation._get_latest_source_ts', return_value=now), \
                 patch('core.caffeine_recommendation._get_last_processed_ts_for_rec', return_value=now if already_processed else now - timedelta(days=1)), \
                 patch('core.caffeine_recommendation.execute_values') as insert:
                result = run_caffeine_recommendation(Connection(wakes), {}, only_user_id='user', force=force)
                values = [] if not insert.called else [row[:3] for row in insert.call_args.args[2]]
                return result, values

        for index in range(100):
            with self.subTest(index=index):
                start = current[1] + timedelta(minutes=index - 50)
                window = ('user', start, start + timedelta(hours=2 + index % 6))
                expected, expected_rows = run([window])
                actual, actual_rows = run([past, window])
                self.assertGreater(expected['inserted_recommendations'], 0)
                self.assertEqual(actual_rows, expected_rows)
                self.assertEqual(actual['processed_users'], 1)
                retried, retried_rows = run([past, window], force=True, already_processed=True)
                self.assertEqual(retried_rows, expected_rows)
                self.assertEqual(retried['processed_users'], 1)

    def test_one_hundred_expired_conflicts_cannot_mask_real_current_conflicts(self):
        now = datetime(2026, 10, 25, 13, tzinfo=ZONE)
        for index in range(100):
            old = now - timedelta(days=10 + index)
            current = now + timedelta(hours=3, minutes=index)
            sleeps = [(old, old + timedelta(hours=8)),
                      (current - timedelta(hours=6), current - timedelta(hours=1))]
            wakes = [(old + timedelta(hours=7), old + timedelta(hours=9)),
                     (current, current + timedelta(hours=2))]
            self.assertIsNone(first_recommendation_overlap(sleeps, wakes, now=now))
            sleeps[1] = (current - timedelta(hours=6), current + timedelta(seconds=1))
            self.assertEqual(first_recommendation_overlap(sleeps, wakes, now=now), (1, 1))

    def test_ongoing_cross_midnight_conflicts_are_not_discarded(self):
        now = datetime(2026, 10, 26, 0, tzinfo=ZONE)
        sleeps = [(now + timedelta(hours=1), now + timedelta(hours=8))]
        wakes = [(now - timedelta(hours=2), now + timedelta(hours=2))]
        self.assertEqual(first_recommendation_overlap(sleeps, wakes, now=now), (0, 0))
        self.assertIsNone(first_recommendation_overlap(sleeps, [(now - timedelta(hours=2), now)], now=now))
