import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["RUN_BACKGROUND_JOBS"] = "false"

from fastapi.testclient import TestClient
from passlib.context import CryptContext
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from app import models
from app.auth import digest
from app.database import Base, get_db
from app.main import app, protected

PASSWORD = "Test-password-2026!"
NOW = datetime.now(timezone.utc)


class AuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.passwords = CryptContext(schemes=["argon2", "bcrypt"], deprecated="auto", bcrypt__rounds=4,
                                     argon2__memory_cost=512, argon2__time_cost=1, argon2__parallelism=1)
        self.password_patch = patch("app.auth.passwords", self.passwords)
        self.password_patch.start()
        self.calc_patch = patch("app.main.trigger_calculation", return_value={"status": "ok"})
        self.calc_patch.start()
        self.a, self.b = uuid4(), uuid4()
        for uid, email in [(self.a, "a@example.com"), (self.b, "b@example.com")]:
            self.db.add(models.User(user_id=uid, name=email[0], email=email,
                                    hashed_password=self.passwords.hash(PASSWORD)))
        self.db.commit()

        def db():
            yield self.db
        app.dependency_overrides[get_db] = db
        self.engine_patch = patch("app.main.engine", self.engine)
        self.engine_patch.start()
        self.client = TestClient(app)
        self.client.__enter__()
        self.pair = self.login()
        self.headers = {"Authorization": "Bearer " + self.pair["access_token"]}

    def tearDown(self):
        self.client.__exit__(None, None, None)
        app.dependency_overrides.clear()
        self.engine_patch.stop()
        self.password_patch.stop()
        self.calc_patch.stop()
        self.db.close()
        self.engine.dispose()

    def login(self, email="a@example.com", password=PASSWORD):
        result = self.client.post("/login/", json={"email": email, "password": password})
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()

    def seed_records(self, uid):
        rows = [models.UsersRealSleepData(user_id=uid, sleep_start_time=NOW, sleep_end_time=NOW + timedelta(hours=8)),
                models.UsersTargetWakingPeriod(user_id=uid, target_start_time=NOW + timedelta(hours=9), target_end_time=NOW + timedelta(hours=10)),
                models.UsersRealTimeIntake(user_id=uid, drink_name="test", caffeine_amount=25, taking_timestamp=NOW)]
        self.db.add_all(rows)
        self.db.commit()
        return rows

    def test_every_data_route_rejects_missing_and_forged_token(self):
        self.assertGreater(len(protected.routes), 20)
        for route in protected.routes:
            path = route.path.replace("{entry_id}", "1")
            for method in route.methods:
                for headers in ({}, {"Authorization": "Bearer wma_" + "x" * 43}):
                    with self.subTest(path=path, method=method, authenticated=bool(headers)):
                        result = self.client.request(method, path, headers=headers, json={} if method != "GET" else None)
                        self.assertEqual(result.status_code, 401)

    def test_default_reads_scope_to_authenticated_user_and_omit_password(self):
        self.seed_records(self.a)
        self.seed_records(self.b)
        for path in ["/users/", "/login/", "/users_sleep/", "/users_wake/", "/users_intake/"]:
            response = self.client.get(path, headers=self.headers)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(len(response.json()), 1)
            self.assertEqual(response.json()[0]["user_id"], str(self.a))
            self.assertNotIn("hashed_password", response.text)
            self.assertNotIn("access_hash", response.text)

    def test_every_data_query_rejects_other_user(self):
        for route in protected.routes:
            if "GET" not in route.methods:
                continue
            result = self.client.get(route.path, params={"user_id": str(self.b)}, headers=self.headers)
            self.assertEqual(result.status_code, 403, route.path)
        response = self.client.post("/calculate/", params={"user_id": str(self.b)}, headers=self.headers)
        self.assertEqual(response.status_code, 403)

    def test_repeated_user_id_query_cannot_bypass_owner_check(self):
        response = self.client.get("/users/?user_id=" + str(self.b) + "&user_id=" + str(self.a), headers=self.headers)
        self.assertEqual(response.status_code, 403)

    def test_every_create_rejects_forged_user_id(self):
        for route in protected.routes:
            if "POST" in route.methods and route.path != "/calculate/":
                response = self.client.post(route.path, headers=self.headers, json={"user_id": str(self.b)})
                self.assertEqual(response.status_code, 403, route.path)

    def test_other_users_records_cannot_be_updated_or_deleted(self):
        sleep, wake, intake = self.seed_records(self.b)
        for endpoint, record, payload in [
            ("users_sleep", sleep, {"sleep_start_time": NOW.isoformat(), "sleep_end_time": (NOW + timedelta(hours=7)).isoformat()}),
            ("users_wake", wake, {"target_start_time": NOW.isoformat(), "target_end_time": (NOW + timedelta(hours=1)).isoformat()}),
            ("users_intake", intake, {"drink_name": "test", "caffeine_amount": 50, "taking_timestamp": NOW.isoformat()}),
        ]:
            for method in ["PUT", "DELETE"]:
                result = self.client.request(method, f"/{endpoint}/{record.id}", headers=self.headers,
                                             json=payload if method == "PUT" else None)
                self.assertEqual(result.status_code, 404, result.text)
            self.db.refresh(record)
            self.assertTrue(record.is_active)

    def test_bulk_records_cannot_include_other_or_null_owner(self):
        for path in ["/device/heart_rate/bulk", "/device/xyz_time/bulk"]:
            for owner in [str(self.b), None]:
                response = self.client.post(path, headers=self.headers, json={"records": [{"user_id": owner}]})
                self.assertEqual(response.status_code, 403)
                response = self.client.post(path, headers=self.headers,
                    json={"user_id": str(self.a), "records": [{"user_id": owner}]})
                self.assertEqual(response.status_code, 403)

    def test_own_body_settings_and_heart_rate_can_be_saved(self):
        response = self.client.post("/users_body_info/", headers=self.headers, json={"user_id": str(self.a),
            "gender": "M", "age": 30, "height": 170, "weight": 65, "bmi": 22.49})
        self.assertEqual(response.status_code, 200, response.text)
        response = self.client.post("/device/heart_rate/bulk", headers=self.headers, json={"records": [{
            "user_id": str(self.a), "time": NOW.isoformat(), "heartrate": 70, "confidence": 3, "source": "test"}]})
        self.assertEqual(response.status_code, 200, response.text)

    def test_refresh_rotates_tokens_and_old_access_stops_working(self):
        response = self.client.post("/auth/refresh", json={"refresh_token": self.pair["refresh_token"]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotEqual(response.json()["access_token"], self.pair["access_token"])
        self.assertEqual(self.client.get("/auth/me", headers=self.headers).status_code, 401)
        self.assertEqual(self.client.get("/auth/me", headers={"Authorization": "Bearer " + response.json()["access_token"]}).status_code, 200)

    def test_reuse_of_refresh_token_revokes_its_session(self):
        rotated = self.client.post("/auth/refresh", json={"refresh_token": self.pair["refresh_token"]}).json()
        replay = self.client.post("/auth/refresh", json={"refresh_token": self.pair["refresh_token"]})
        self.assertEqual(replay.status_code, 401)
        self.assertEqual(self.client.get("/auth/me", headers={"Authorization": "Bearer " + rotated["access_token"]}).status_code, 401)

    def test_logout_revokes_access_and_refresh(self):
        self.assertEqual(self.client.post("/auth/logout", headers=self.headers).status_code, 200)
        self.assertEqual(self.client.get("/auth/me", headers=self.headers).status_code, 401)
        self.assertEqual(self.client.post("/auth/refresh", json={"refresh_token": self.pair["refresh_token"]}).status_code, 401)

    def test_expired_access_can_refresh_but_expired_session_cannot(self):
        session = self.db.query(models.AuthSession).filter_by(access_hash=digest(self.pair["access_token"])).one()
        session.access_expires_at = NOW - timedelta(seconds=1)
        self.db.commit()
        self.assertEqual(self.client.get("/auth/me", headers=self.headers).status_code, 401)
        response = self.client.post("/auth/refresh", json={"refresh_token": self.pair["refresh_token"]})
        self.assertEqual(response.status_code, 200)
        session.refresh_expires_at = NOW - timedelta(seconds=1)
        self.db.commit()
        self.assertEqual(self.client.post("/auth/refresh", json={"refresh_token": response.json()["refresh_token"]}).status_code, 401)

    def test_database_contains_only_token_hashes(self):
        session = self.db.query(models.AuthSession).one()
        self.assertEqual(session.access_hash, digest(self.pair["access_token"]))
        self.assertEqual(session.refresh_hash, digest(self.pair["refresh_token"]))
        self.assertNotEqual(session.access_hash, self.pair["access_token"])

    def test_existing_blank_password_requires_change_before_data_access(self):
        user = self.db.query(models.User).filter_by(user_id=self.b).one()
        user.hashed_password = self.passwords.hash(" " * 12)
        self.db.commit()
        pair = self.login("b@example.com", " " * 12)
        headers = {"Authorization": "Bearer " + pair["access_token"]}
        self.assertTrue(pair["password_change_required"])
        self.assertEqual(self.client.get("/users/", headers=headers).status_code, 403)
        response = self.client.post("/auth/change_password", headers=headers,
            json={"current_password": " " * 12, "new_password": PASSWORD})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["password_change_required"])
        self.assertEqual(self.client.get("/users/", headers={"Authorization": "Bearer " + response.json()["access_token"]}).status_code, 200)

    def test_password_change_revokes_other_sessions(self):
        other = self.login()
        response = self.client.post("/auth/change_password", headers=self.headers,
            json={"current_password": PASSWORD, "new_password": "Another-password-2026!"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/auth/me", headers={"Authorization": "Bearer " + other["access_token"]}).status_code, 401)

    def test_login_rate_limit_and_generic_invalid_credentials(self):
        results = [self.client.post("/login/", json={"email": "a@example.com", "password": "wrong"}) for _ in range(11)]
        self.assertEqual(results[0].json()["detail"], "invalid_credentials")
        self.assertEqual(results[-1].status_code, 429)
        self.assertIn("retry-after", results[-1].headers)

    def test_registration_rejects_blank_password_and_is_atomic(self):
        response = self.client.post("/users/", json={"name": "test", "email": "new@example.com", "password": "   "})
        self.assertEqual(response.status_code, 422)
        response = self.client.post("/users/", json={"name": "test", "email": "NEW@example.com", "password": PASSWORD})
        self.assertEqual(response.status_code, 200, response.text)
        uid = response.json()["user_id"]
        self.assertNotIn("hashed_password", response.text)
        self.assertIsNotNone(self.db.query(models.UsersParams).filter_by(user_id=UUID(uid)).first())

    def test_no_sensitive_data_is_cached_and_validation_omits_input(self):
        response = self.client.get("/users/", headers=self.headers)
        self.assertEqual(response.headers["cache-control"], "no-store")
        response = self.client.post("/login/", json={"email": "invalid", "password": "private-password"})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("private-password", response.text)

    def test_legacy_bcrypt_password_migrates_to_argon2_after_login(self):
        user = self.db.query(models.User).filter_by(user_id=self.b).one()
        user.hashed_password = self.passwords.hash(PASSWORD, scheme="bcrypt")
        self.db.commit()
        self.login("b@example.com", PASSWORD)
        self.db.refresh(user)
        self.assertTrue(user.hashed_password.startswith("$argon2id$"))
        self.assertTrue(self.passwords.verify(PASSWORD, user.hashed_password))

    def test_one_hundred_cross_account_requests_never_read_or_change_data(self):
        _, _, intake = self.seed_records(self.b)
        for attempt in range(100):
            if attempt % 2:
                response = self.client.get("/users_intake/?user_id=" + str(self.b), headers=self.headers)
                self.assertEqual(response.status_code, 403)
            else:
                response = self.client.delete(f"/users_intake/{intake.id}", headers=self.headers)
                self.assertEqual(response.status_code, 404)
        self.db.refresh(intake)
        self.assertTrue(intake.is_active)

    def test_database_error_does_not_expose_query_or_data(self):
        from sqlalchemy.sql.dml import Insert
        original = self.db.execute
        def execute(statement, *args, **kwargs):
            if isinstance(statement, Insert):
                raise RuntimeError("secret SQL and patient details")
            return original(statement, *args, **kwargs)
        with patch.object(self.db, "execute", side_effect=execute):
            response = self.client.post("/users_body_info/", headers=self.headers, json={
                "user_id": str(self.a), "gender": "M", "age": 30, "height": 170, "weight": 65, "bmi": 22.49})
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("patient", response.text)
        self.assertNotIn("SQL", response.text)

    def test_password_policy_measures_utf8_bytes(self):
        for password in ["a" * 73, "\u5bc6" * 25]:
            response = self.client.post("/users/", json={
                "name": "test", "email": "new@example.com", "password": password})
            self.assertEqual(response.status_code, 422)

    def test_short_password_registration_login_and_change_preserve_authorization(self):
        for index, password in enumerate(["a", "1234", "\u5bc6", " a ", "a" * 72]):
            email = f"short{index}@example.com"
            response = self.client.post("/users/", json={"name": "test", "email": email, "password": password})
            self.assertEqual(response.status_code, 200, response.text)
            pair = self.login(email, password)
            self.assertFalse(pair["password_change_required"])
            headers = {"Authorization": "Bearer " + pair["access_token"]}
            self.assertEqual(self.client.get("/users/", headers=headers).status_code, 200)
            self.assertEqual(self.client.get(f"/users/?user_id={self.a}", headers=headers).status_code, 403)
            result = self.client.post("/auth/change_password", headers=headers,
                json={"current_password": password, "new_password": "z"})
            self.assertEqual(result.status_code, 200, result.text)
            self.login(email, "z")

    def test_existing_short_password_is_not_forced_to_change_after_login(self):
        user = self.db.query(models.User).filter_by(user_id=self.b).one()
        user.hashed_password = self.passwords.hash("1234")
        self.db.commit()
        pair = self.login("b@example.com", "1234")
        self.assertFalse(pair["password_change_required"])
        self.assertEqual(self.client.get("/users/", headers={"Authorization": "Bearer " + pair["access_token"]}).status_code, 200)

    def test_password_change_rejects_empty_blank_and_oversized_values(self):
        for value in ["", " ", "\t\n", "\u3000", "a" * 73, "\u5bc6" * 25]:
            result = self.client.post("/auth/change_password", headers=self.headers,
                json={"current_password": PASSWORD, "new_password": value})
            self.assertEqual(result.status_code, 422, result.text)
        user = self.db.query(models.User).filter_by(user_id=self.a).one()
        self.assertTrue(self.passwords.verify(PASSWORD, user.hashed_password))

    def test_unregistered_email_cannot_grant_any_role(self):
        response = self.client.post("/users/", json={"name": "test", "email": "new@example.com",
            "password": PASSWORD, "role": "researcher", "is_admin": True})
        self.assertEqual(response.status_code, 200)
        pair = self.login("new@example.com")
        headers = {"Authorization": "Bearer " + pair["access_token"]}
        self.assertEqual(self.client.get("/users/?user_id=" + str(self.a), headers=headers).status_code, 403)

    def test_expired_auth_rows_are_pruned_without_touching_active_sessions(self):
        from app.auth import cleanup_expired_sessions
        expired = models.AuthSession(user_id=self.b, access_hash="a" * 64, refresh_hash="r" * 64,
            created_at=NOW-timedelta(days=31), access_expires_at=NOW-timedelta(days=30),
            refresh_expires_at=NOW-timedelta(days=1), password_change_required=False)
        self.db.add(expired)
        self.db.flush()
        self.db.add(models.UsedRefreshToken(token_hash="u"*64, session_id=expired.id,
            expires_at=NOW+timedelta(days=1)))
        self.db.add(models.AuthRateLimit(bucket="expired", count=2, expires_at=NOW-timedelta(seconds=1)))
        self.db.commit()
        cleanup_expired_sessions(self.db)
        self.assertEqual(self.db.query(models.AuthSession).count(), 1)
        self.assertEqual(self.db.query(models.UsedRefreshToken).count(), 0)
        self.assertIsNone(self.db.query(models.AuthRateLimit).filter_by(bucket="expired").first())
        self.assertEqual(self.client.get("/auth/me", headers=self.headers).status_code, 200)


from uuid import UUID

if __name__ == "__main__":
    unittest.main()
