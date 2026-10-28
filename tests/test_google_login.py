import json
import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["RUN_BACKGROUND_JOBS"] = "false"

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from google.auth import crypt, jwt
from google.auth.exceptions import TransportError
from passlib.context import CryptContext
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import models
from app.database import Base, get_db
from app.main import app

CLIENT_ID = "1234567890-wakemate-test.apps.googleusercontent.com"
PASSWORD = "Existing-account-password"


class GoogleLoginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.private = key.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        cls.public = key.public_key().public_bytes(serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        cls.signer = crypt.RSASigner.from_string(cls.private, key_id="test-key")

    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.patches = []
        self.use_patch(patch.dict(os.environ, {"WAKEMATE_GOOGLE_WEB_CLIENT_ID": CLIENT_ID}))
        passwords = CryptContext(schemes=["argon2", "bcrypt"], deprecated="auto",
            argon2__memory_cost=512, argon2__time_cost=1, argon2__parallelism=1)
        self.use_patch(patch("app.auth.passwords", passwords))
        self.use_patch(patch("app.google_login.passwords", passwords))
        self.use_patch(patch("app.main.engine", self.engine))
        self.transport = self.use_patch(patch("app.google_login.GoogleRequest"))
        self.transport.return_value.return_value = SimpleNamespace(status=200,
            data=json.dumps({"test-key": self.public}).encode())
        self.owner = models.User(user_id=uuid4(), name="Existing owner", email="owner@example.com",
            hashed_password=passwords.hash(PASSWORD))
        self.db.add(self.owner)
        self.db.commit()

        def get_test_db():
            yield self.db
        app.dependency_overrides[get_db] = get_test_db
        self.client = TestClient(app)
        self.client.__enter__()

    def use_patch(self, patcher):
        self.patches.append(patcher)
        return patcher.start()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        app.dependency_overrides.clear()
        for patcher in reversed(self.patches):
            patcher.stop()
        self.db.close()
        self.engine.dispose()

    def token(self, **overrides):
        timestamp = int(time.time())
        claims = {"iss": "https://accounts.google.com", "aud": CLIENT_ID,
            "sub": "google-person-one", "email": "new@gmail.com", "email_verified": True,
            "name": "Google user", "iat": timestamp - 1, "exp": timestamp + 600}
        claims.update(overrides)
        return jwt.encode(self.signer, claims).decode()

    def google(self, token=None):
        return self.client.post("/auth/google/login", json={"id_token": token or self.token()})

    def owner_headers(self):
        response = self.client.post("/login/", json={"email": self.owner.email, "password": PASSWORD})
        self.assertEqual(response.status_code, 200, response.text)
        return {"Authorization": "Bearer " + response.json()["access_token"]}

    def link(self, token=None, password=PASSWORD, headers=None):
        return self.client.post("/auth/google/link", headers=headers or self.owner_headers(),
            json={"id_token": token or self.token(email="owner@example.com"), "current_password": password})

    def test_unconfigured_or_invalid_client_id_disables_google(self):
        for value in ["", "placeholder", "evil.example.com", "test.apps.googleusercontent.com"]:
            with self.subTest(value=value), patch.dict(os.environ, {"WAKEMATE_GOOGLE_WEB_CLIENT_ID": value}):
                self.assertEqual(self.client.get("/auth/google/config").json(),
                    {"enabled": False, "server_client_id": None})
                response = self.google()
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json()["detail"], "google_not_configured")
        self.transport.assert_not_called()

    def test_public_configuration_contains_no_credentials(self):
        self.assertEqual(self.client.get("/auth/google/config").json(),
            {"enabled": True, "server_client_id": CLIENT_ID})

    def test_valid_signed_google_token_creates_one_owner_and_revocable_session(self):
        response = self.google()
        self.assertEqual(response.status_code, 200, response.text)
        pair = response.json()
        self.assertFalse(pair["user"]["password_login_available"])
        self.assertTrue(pair["user"]["google_linked"])
        self.assertNotIn("hashed_password", response.text)
        self.assertNotIn("id_token", response.text)
        uid = pair["user"]["user_id"]
        self.assertEqual(self.db.query(models.UsersParams).count(), 1)
        self.assertEqual(self.db.query(models.GoogleIdentity).count(), 1)
        headers = {"Authorization": "Bearer " + pair["access_token"]}
        self.assertEqual(self.client.get("/auth/me", headers=headers).json()["user_id"], uid)
        self.assertEqual(self.client.get("/users/?user_id=" + str(self.owner.user_id), headers=headers).status_code, 403)
        self.assertEqual(self.client.post("/auth/logout", headers=headers).status_code, 200)
        self.assertEqual(self.client.get("/auth/me", headers=headers).status_code, 401)
        self.assertEqual(self.transport.return_value.call_args.kwargs["timeout"], 5)

    def test_repeat_signin_uses_stable_google_subject_without_duplicate_accounts(self):
        uid = self.google().json()["user"]["user_id"]
        for _ in range(10):
            response = self.google()
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["user"]["user_id"], uid)
        changed_email = self.google(self.token(email="renamed@gmail.com"))
        self.assertEqual(changed_email.json()["user"]["user_id"], uid)
        self.assertEqual(self.db.query(models.User).count(), 2)

    def test_google_only_account_cannot_use_marker_as_a_password_or_change_password(self):
        pair = self.google().json()
        result = self.client.post("/login/", json={"email": "new@gmail.com", "password": "!external:google"})
        self.assertEqual(result.status_code, 401)
        result = self.client.post("/auth/change_password",
            headers={"Authorization": "Bearer " + pair["access_token"]},
            json={"current_password": "!external:google", "new_password": "test"})
        self.assertEqual(result.status_code, 400)
        self.assertEqual(result.json()["detail"], "password_login_not_available")

    def test_google_sessions_refresh_with_existing_token_rotation(self):
        pair = self.google().json()
        response = self.client.post("/auth/refresh", json={"refresh_token": pair["refresh_token"]})
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(pair["access_token"], response.json()["access_token"])
        self.assertFalse(response.json()["user"]["password_login_available"])

    def test_existing_email_is_not_automatically_merged(self):
        old_hash = self.owner.hashed_password
        response = self.google(self.token(email="OWNER@example.com"))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], "google_existing_account_sign_in_first")
        self.assertEqual(self.db.query(models.GoogleIdentity).count(), 0)
        self.assertEqual(self.db.query(models.User).count(), 1)
        self.db.refresh(self.owner)
        self.assertEqual(self.owner.hashed_password, old_hash)

    def test_link_requires_existing_session_and_correct_password(self):
        data = {"id_token": self.token(email="owner@example.com"), "current_password": PASSWORD}
        self.assertEqual(self.client.post("/auth/google/link", json=data).status_code, 401)
        response = self.link(password="wrong")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "invalid_current_password")
        self.assertEqual(self.db.query(models.GoogleIdentity).count(), 0)
        self.transport.assert_not_called()

    def test_link_requires_same_email(self):
        response = self.link(self.token(email="someoneelse@gmail.com"))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], "google_email_mismatch")
        self.assertEqual(self.db.query(models.GoogleIdentity).count(), 0)

    def test_link_is_idempotent_preserves_user_id_password_and_records(self):
        headers = self.owner_headers()
        old_hash = self.owner.hashed_password
        self.db.add(models.UsersRealTimeIntake(user_id=self.owner.user_id, drink_name="coffee",
            caffeine_amount=50, taking_timestamp=models.datetime.now(models.timezone.utc)))
        self.db.commit()
        for _ in range(2):
            response = self.link(headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.db.query(models.GoogleIdentity).count(), 1)
        response = self.google(self.token(email="owner@example.com"))
        self.assertEqual(response.json()["user"]["user_id"], str(self.owner.user_id))
        self.assertTrue(response.json()["user"]["password_login_available"])
        self.assertEqual(self.db.query(models.UsersRealTimeIntake).count(), 1)
        self.db.refresh(self.owner)
        self.assertEqual(self.owner.hashed_password, old_hash)
        self.assertTrue(self.client.get("/auth/me", headers=headers).json()["google_linked"])

    def test_cannot_link_another_persons_already_linked_subject(self):
        self.google()
        response = self.link(self.token(email="owner@example.com"))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], "google_account_already_linked")

    def test_cannot_replace_an_existing_google_link(self):
        self.assertEqual(self.link().status_code, 200)
        response = self.link(self.token(sub="other-google-person", email="owner@example.com"))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.db.query(models.GoogleIdentity).one().subject, "google-person-one")

    def test_signed_but_invalid_claims_are_rejected(self):
        for claims in [
            {"aud": "attacker-client"}, {"iss": "https://attacker.example"},
            {"exp": int(time.time()) - 100}, {"iat": int(time.time()) + 10000},
            {"email_verified": False}, {"email_verified": "true"},
            {"email": "not-an-email"}, {"email": None}, {"sub": ""},
            {"sub": 12345}, {"sub": "x" * 256},
        ]:
            with self.subTest(claims=claims):
                response = self.google(self.token(**claims))
                self.assertEqual(response.status_code, 401, response.text)
        self.assertEqual(self.db.query(models.GoogleIdentity).count(), 0)

    def test_forged_signature_and_unsigned_tokens_are_rejected(self):
        token = self.token()
        parts = token.split(".")
        parts[2] = "AAAA" + parts[2][4:]
        for forged in [".".join(parts), "not-a-google-token-at-all", "x" * 40]:
            response = self.google(forged)
            self.assertEqual(response.status_code, 401, response.text)
            self.assertNotIn(forged, response.text)

    def test_transport_failure_does_not_create_an_account_or_leak_token(self):
        self.transport.return_value.side_effect = TransportError("offline")
        token = self.token()
        response = self.google(token)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(token, response.text)
        self.assertEqual(self.db.query(models.User).count(), 1)

    def test_oversized_or_missing_token_validation_does_not_echo_credentials(self):
        for payload in [{}, {"id_token": "sensitive" * 2000}, {"id_token": 100}]:
            response = self.client.post("/auth/google/login", json=payload)
            self.assertEqual(response.status_code, 422)
            self.assertNotIn("sensitive", response.text)
            self.assertNotIn('"input"', response.text)

    def test_google_login_has_database_backed_rate_limiting(self):
        self.transport.return_value.side_effect = TransportError("offline")
        for _ in range(60):
            self.assertEqual(self.google().status_code, 503)
        response = self.google()
        self.assertEqual(response.status_code, 429)
        self.assertIn("retry-after", response.headers)
