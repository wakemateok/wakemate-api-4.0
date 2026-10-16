# Account Security Release 1.1.0

## Implemented

- Every participant data route requires a valid Bearer access token. The server checks both query/body ownership and record ownership for edits/deletes. Omitting user_id means the signed-in user, never all participants.
- Public endpoints are limited to registration, login, token refresh, health, root and questionnaire links. OpenAPI describes protected routes but does not grant data access.
- Access tokens expire after 15 minutes. Refresh sessions expire after 30 days; refresh rotation, reuse detection and server-side logout revoke credentials. Only token hashes are stored in the database.
- New passwords use Argon2id (19 MiB, two iterations, one lane). Existing bcrypt hashes migrate at successful login. Passwords must have at least 12 Unicode code points and at most 72 UTF-8 bytes. Short legacy passwords must be changed before data access.
- Database-backed login/registration/password-change rate limits survive application restarts. Expired session and rate-limit rows are cleaned at startup and during the existing hourly background job.
- Password hashes and tokens are never returned by user-list endpoints. Validation responses omit submitted input. Database exceptions exposed to clients contain no SQL details.
- Account caches are cleared on logout. New Android APKs store tokens through flutter_secure_storage and disable Android backup and cleartext HTTP. The API client rejects foreign origins, does not follow redirects with credentials, and drops responses belonging to an earlier login.
- Researcher cross-participant access is NOT enabled, per the user's latest decision. Registration cannot request a privileged role.

## Deployment and Migration

1. Deploy backend main with the existing private DATABASE_URL in Render. Startup creates the additive auth_sessions, auth_used_refresh_tokens and auth_rate_limits tables. No participant tables are dropped.
2. Verify /ping, then run `python -B tests/cloud_mobile_smoke.py --run-cloud`. This creates only a synthetic participant, tests authenticated writes and forbidden reads, deactivates synthetic sleep/wake records and revokes the session. The synthetic account/PVT audit record remains; no real participant is modified.
3. Install both 1.1.0+4 APKs. The new production certificate differs from the prior debug certificate: uninstall the old test APKs first. Cloud accounts/data remain; local questionnaire state, caches and alarms reset. Grant notification/alarm permissions again and regenerate future reminders.
4. Old APKs can no longer access protected data. Existing users sign in again; users with a short password follow the forced password-change screen.
5. Do not roll back to an unauthenticated backend merely to support old APKs. Any incident rollback must retain access controls.

## Operations Still Requiring the Account Owner

- The previously tracked .env is removed from the current Git tree and Docker build context. Its hostname points to the OLD Oregon database, not the newer Virginia database. If those old credentials still work, their owner must revoke them. Deleting the file from main does not erase Git history. This release does not rewrite repository history or rotate hosted credentials automatically.
- Confirm the active Render DATABASE_URL refers to the intended team-owned database. Create a replacement database credential, update/redeploy dependent services, verify access, THEN revoke the old credential. Never paste database passwords in chat or Git.
- Restrict external PostgreSQL access to approved IP ranges where practical. Android users reach the HTTPS API, not PostgreSQL directly; the API should use Render's private database address.
- Confirm paid database continuity, backup retention and a successful restore rehearsal. This code release cannot verify billing or backups without the owner's Render access.
- Email ownership verification and self-service forgotten-password recovery are not included. Do not use email claims as proof of researcher identity. A future recovery flow needs an approved mail provider and verified, expiring one-time links.
- Complete hospital privacy/IRB, retention, consent and incident-response review before collecting real research data. These tests are engineering checks, not a penetration test, clinical validation or compliance certification.
- Test both APKs on actual phones: login/change password/logout, data isolation, both PVT modes, and notifications with the screen locked. No physical Android device was connected during this release's automated checks.

## Signing Key Handoff

Private material is under `%USERPROFILE%\.codex\secrets\wakemate-release`, with restricted Windows permissions. The password XML uses Windows DPAPI and cannot be treated as a portable backup. The account owner must keep the PKCS12 private key AND a separately recoverable password in an approved secure backup/password manager. Do not upload these to the APK sharing folder or GitHub. Losing the production key prevents future APK updates with the same signer.

Frontend commands: `scripts/initialize_signing.ps1` creates the key once; `scripts/build_signed_apks.ps1` builds both universal APKs. Release builds fail closed if signing variables are missing. Never rerun initialization to replace an already published key.

## References

- https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html
- https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html
- https://pub.dev/packages/flutter_secure_storage
- https://render.com/docs/postgresql-credentials
- https://render.com/docs/postgresql-backups
