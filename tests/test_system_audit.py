import os
import unittest
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

os.environ['DATABASE_URL'] = 'sqlite://'
os.environ['RUN_BACKGROUND_JOBS'] = 'false'

from app import models
from core.baseline_rt import compute_baseline_rt, sigmoid
from core.caffeine_recommendation import run_caffeine_recommendation, _merge_recommendations_list
from core.personalize_params import update_p0_for_user, DEFAULTS
import test_auth_authorization as authorization

TAIPEI = timezone(timedelta(hours=8))


class RecordEditAuditTests(unittest.TestCase):
    setUp = authorization.AuthorizationTests.setUp
    tearDown = authorization.AuthorizationTests.tearDown
    login = authorization.AuthorizationTests.login

    def test_pvt_response_preserves_personalization_status_for_its_owner(self):
        params = {'trait_updated': False, 'kc_updated': False, 'p0_value': 270.0}
        with patch('app.main.get_db_connection'), patch('app.main.update_user_params', return_value=params):
            result = self.client.post('/users_pvt/', headers=self.headers, json={
                'user_id': str(self.a), 'mean_rt': 320, 'lapses': 1, 'false_starts': 0,
                'kss_level': 3, 'device': 'Mobile', 'test_at': datetime.now(timezone.utc).isoformat()})
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()['params_update'], params)

    def test_create_and_edit_cannot_overlap_the_same_record_type(self):
        day = (datetime.now(timezone.utc) + timedelta(days=3)).replace(hour=0, minute=0, second=0, microsecond=0)
        for kind in ['sleep', 'wake']:
            with self.subTest(kind=kind):
                model = models.UsersRealSleepData if kind == 'sleep' else models.UsersTargetWakingPeriod
                start_name = 'sleep_start_time' if kind == 'sleep' else 'target_start_time'
                end_name = 'sleep_end_time' if kind == 'sleep' else 'target_end_time'
                offset = 0 if kind == 'sleep' else 12
                first_start = day + timedelta(hours=offset)
                second_start = first_start + timedelta(hours=4)
                rows = [model(user_id=self.a, **{start_name: start, end_name: start + timedelta(hours=2)})
                        for start in [first_start, second_start]]
                self.db.add_all(rows)
                self.db.commit()
                body = {start_name: (second_start + timedelta(minutes=30)).isoformat(),
                        end_name: (second_start + timedelta(hours=1)).isoformat()}
                result = self.client.put(f'/users_{kind}/{rows[0].id}', headers=self.headers, json=body)
                self.assertEqual(result.status_code, 400, result.text)
                self.assertEqual(result.json()['detail'], 'periods_overlap')
                self.db.expire_all()
                self.assertTrue(rows[0].is_active)
                self.assertIsNone(rows[0].invalidated_at)
                self.assertEqual(self.db.query(model).count(), 2)
                result = self.client.post(f'/users_{kind}/', headers=self.headers,
                    json={'user_id': str(self.a), **body})
                self.assertEqual(result.status_code, 400, result.text)
                self.assertEqual(self.db.query(model).count(), 2)
                adjacent = {start_name: (second_start + timedelta(hours=2)).isoformat(),
                            end_name: (second_start + timedelta(hours=3)).isoformat()}
                result = self.client.put(f'/users_{kind}/{rows[0].id}', headers=self.headers, json=adjacent)
                self.assertEqual(result.status_code, 200, result.text)


class CalculationAuditTests(unittest.TestCase):
    def test_duplicate_pvt_dataset_does_not_apply_trait_learning_twice(self):
        observed = datetime.now(timezone.utc)
        params = {**DEFAULTS, 'last_trait_update': observed - timedelta(minutes=1)}
        class Cursor:
            def execute(self, sql, params=None): pass
            def close(self): pass
        class Connection:
            def cursor(self): return Cursor()
            def commit(self): pass
            def rollback(self): pass
        with patch('core.personalize_params.fetch_user_params', side_effect=lambda *_: dict(params)), \
             patch('core.personalize_params.update_pvt_7d_stats', return_value=(1, 320, 1)), \
             patch('core.personalize_params.get_pvts_last_7days', return_value=[(observed, 320, 3)]), \
             patch('core.personalize_params.get_all_sleep_intervals', return_value=[]), \
             patch('core.personalize_params.upsert_users_params') as write:
            self.assertTrue(update_p0_for_user(Connection(), 'user', min_pvt_7d=1, min_days=1))
            params.update(write.call_args.args[2])
            self.assertFalse(update_p0_for_user(Connection(), 'user', min_pvt_7d=1, min_days=1))
            write.assert_called_once()

    def test_pvt_parameters_and_deleted_inputs_are_part_of_derived_source_versions(self):
        from core.caffeine_recommendation import _get_latest_source_ts as recommendation_version
        from core.alertness_data import _get_latest_source_ts as graph_version
        class Cursor:
            def execute(self, sql, params): self.sql, self.params = sql, params
            def fetchone(self): return (datetime.now(timezone.utc),)
        for get_version in [recommendation_version, graph_version]:
            cursor = Cursor()
            get_version(cursor, 'user')
            self.assertIn('users_pvt_results', cursor.sql)
            self.assertIn('users_params', cursor.sql)
            self.assertNotIn('is_active', cursor.sql)
            self.assertEqual(cursor.params, ('user',) * 5)

    def test_same_instant_has_same_taipei_circadian_baseline_for_100_times(self):
        start = datetime(2026, 10, 25, 0, tzinfo=TAIPEI)
        for index in range(100):
            observed = start + timedelta(hours=index)
            expected = 270 + sigmoid(observed.hour)
            self.assertAlmostEqual(compute_baseline_rt(observed, [], 270), expected)
            self.assertAlmostEqual(compute_baseline_rt(observed.astimezone(timezone.utc), [], 270), expected)

    def test_recommendation_merge_never_moves_next_days_dose_into_previous_day(self):
        first = datetime(2026, 10, 25, 23, tzinfo=TAIPEI)
        rows = [('user', 300, first), ('user', 300, first + timedelta(hours=1))]
        self.assertEqual(len(_merge_recommendations_list(rows)), 2)

    def test_multiple_windows_share_existing_300mg_recommendation_budget_by_taipei_day(self):
        now = datetime.now(timezone.utc)
        day = (now.astimezone(TAIPEI) + timedelta(days=2)).replace(hour=0, minute=0, second=0, microsecond=0)
        sleeps = [('user', (day - timedelta(hours=1)).astimezone(timezone.utc),
                   (day + timedelta(hours=7)).astimezone(timezone.utc))]
        windows = [('user', (day + timedelta(hours=start)).astimezone(timezone.utc),
                    (day + timedelta(hours=end)).astimezone(timezone.utc)) for start, end in [(9, 11), (18, 21), (24, 27)]]

        class Cursor:
            def execute(self, sql, params=None): self.sql = sql
            def fetchall(self):
                if 'SELECT user_id, target_start_time' in self.sql: return windows
                if 'SELECT user_id, sleep_start_time' in self.sql: return sleeps
                return []
            def fetchone(self): return (now,)
            def close(self): pass

        class Connection:
            def __init__(self): self.cur = Cursor()
            def cursor(self): return self.cur
            def commit(self): pass
            def rollback(self): pass

        with patch('core.caffeine_recommendation._get_latest_source_ts', return_value=now), \
             patch('core.caffeine_recommendation._get_last_processed_ts_for_rec', return_value=now - timedelta(days=1)), \
             patch('core.caffeine_recommendation._compute_precise_dose_for_hour', return_value=300), \
             patch('core.caffeine_recommendation._apply_dose_to_gpd') as apply_dose, \
             patch('core.caffeine_recommendation.execute_values') as insert:
            run_caffeine_recommendation(Connection(), {}, only_user_id='user')
            totals = defaultdict(float)
            for row in insert.call_args.args[2]:
                totals[row[2].astimezone(TAIPEI).date()] += row[1]
            self.assertEqual(len(totals), 2)
            self.assertTrue(all(0 < total <= 300 for total in totals.values()), dict(totals))
            self.assertTrue(any(call.args[2] < 0 for call in apply_dose.call_args_list),
                            'Later windows must include earlier planned doses')

    def test_ongoing_window_does_not_spend_budget_on_unissued_past_doses(self):
        now = datetime.now(timezone.utc).replace(minute=15, second=0, microsecond=0)
        windows = [('user', now - timedelta(hours=2), now + timedelta(hours=4))]
        sleeps = [('user', now - timedelta(hours=12), now - timedelta(hours=4))]
        class Cursor:
            def execute(self, sql, params=None): self.sql = sql
            def fetchall(self):
                if 'SELECT user_id, target_start_time' in self.sql: return windows
                if 'SELECT user_id, sleep_start_time' in self.sql: return sleeps
                return []
            def fetchone(self): return (now,)
            def close(self): pass
        class Connection:
            def cursor(self): return Cursor()
            def commit(self): pass
            def rollback(self): pass
        with patch('core.caffeine_recommendation._get_latest_source_ts', return_value=now), \
             patch('core.caffeine_recommendation._get_last_processed_ts_for_rec', return_value=now - timedelta(days=1)), \
             patch('core.caffeine_recommendation._compute_precise_dose_for_hour', return_value=300), \
             patch('core.caffeine_recommendation._apply_dose_to_gpd'), \
             patch('core.caffeine_recommendation.execute_values') as insert:
            result = run_caffeine_recommendation(Connection(), {}, only_user_id='user')
            self.assertGreater(result['inserted_recommendations'], 0)
            self.assertTrue(all(row[2] >= now for row in insert.call_args.args[2]))
