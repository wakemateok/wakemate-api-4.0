"""Opt-in smoke test. Only a newly created synthetic user's records are touched."""

import argparse
import json
import secrets
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

BASE = "https://wakemate-api-4-0-qtgs.onrender.com"
AUTH_TOKEN = None


def call(path, payload=None, method=None):
    headers = {"Content-Type": "application/json"}
    if AUTH_TOKEN:
        headers["Authorization"] = "Bearer " + AUTH_TOKEN
    request = Request(BASE + path, method=method or ("POST" if payload is not None else "GET"),
                      data=json.dumps(payload).encode() if payload is not None else None,
                      headers=headers)
    try:
        with urlopen(request, timeout=120) as response:
            return response.status, json.loads(response.read())
    except HTTPError as error:
        return error.code, json.loads(error.read())


def main():
    global AUTH_TOKEN
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-cloud", action="store_true")
    if not parser.parse_args().run_cloud:
        parser.error("Explicit --run-cloud is required")
    token = uuid4().hex[:16]
    email = "codex_mobile_" + token + "@example.com"
    password = secrets.token_urlsafe(24)
    status, user = call("/users/", {"name": "codex_mobile_regression_" + token,
        "email": email, "password": password})
    assert status == 200, "Synthetic user creation failed"
    user_id = user["user_id"]
    status, pair = call("/login/", {"email": email, "password": password})
    assert status == 200, "Synthetic user login failed"
    AUTH_TOKEN = pair["access_token"]
    results = []
    zone = timezone(timedelta(hours=8))
    day = datetime.now(zone).date() + timedelta(days=2)

    def time(hour, offset=0):
        return datetime.combine(day + timedelta(days=offset), datetime.min.time(), zone).replace(hour=hour).isoformat()

    def payload(*periods):
        return {"user_id": user_id, "day": day.isoformat(),
                "periods": [{"start": start, "end": end} for start, end in periods]}

    def passed(name):
        results.append(name)
        print(json.dumps({"passed": name}), flush=True)

    status, own = call("/users/")
    assert status == 200 and len(own) == 1 and own[0]["user_id"] == user_id
    assert "hashed_password" not in own[0]
    passed("authenticated_reads_are_owner_scoped")
    status, _ = call("/users/?user_id=" + str(uuid4()))
    assert status == 403
    passed("cross_account_reads_are_forbidden")
    AUTH_TOKEN = None
    status, _ = call("/users/")
    assert status == 401
    passed("anonymous_reads_are_forbidden")
    AUTH_TOKEN = pair["access_token"]

    try:
        sleep = payload((time(23, -1), time(7)))
        status, first = call("/users_sleep/replace_day", sleep)
        assert status == 200 and len(first["ids"]) == 1
        status, retry = call("/users_sleep/replace_day", sleep)
        assert status == 200 and retry["ids"] == first["ids"] and not retry["changed"]
        passed("sleep_retry_is_idempotent")

        wake = payload((time(9), time(10)), (time(18), time(19)))
        status, first = call("/users_wake/replace_day", wake)
        assert status == 200 and len(first["ids"]) == 2
        status, retry = call("/users_wake/replace_day", wake)
        assert status == 200 and retry["ids"] == first["ids"]
        passed("multi_slot_wake_retry_is_idempotent")
        status, recommendations = call('/recommendations/?user_id=' + user_id)
        assert status == 200 and isinstance(recommendations, list)
        passed('consistent_inputs_allow_recommendation_reads')

        for _ in range(2):
            status, calculated = call('/calculate/?user_id=' + user_id, method='POST')
            assert status == 200 and calculated['status'] == 'ok'
        passed('manual_retries_perform_real_calculation')
        status, recommendations = call('/recommendations/?user_id=' + user_id)
        assert status == 200 and recommendations
        timings = [row['recommended_caffeine_intake_timing'] for row in recommendations]
        assert len(timings) == len(set(timings))
        assert all(row['user_id'] == user_id and row['is_active'] for row in recommendations)
        passed('repeated_manual_calculation_keeps_one_active_recommendation_per_time')
        totals = {}
        for row in recommendations:
            date = datetime.fromisoformat(row['recommended_caffeine_intake_timing'].replace('Z', '+00:00')).astimezone(zone).date()
            totals[date] = totals.get(date, 0) + row['recommended_caffeine_amount']
        assert all(amount <= 300 for amount in totals.values())
        passed('recommendations_respect_existing_taipei_daily_budget')

        status, rejection = call("/users_wake/replace_day", payload((time(6), time(8)), (time(20), time(21))))
        assert status == 400
        assert rejection['detail']['type'] == 'sleep_wake_overlap'
        assert rejection['detail']['conflicts'][0]['sleep']['id'] is not None
        assert datetime.fromisoformat(rejection['detail']['conflicts'][0]['sleep']['start']).astimezone(zone) == datetime.fromisoformat(time(23, -1))
        passed('overlap_rejection_identifies_conflicting_periods')
        status, active = call("/users_wake/?user_id=" + user_id)
        assert status == 200 and sorted(row["id"] for row in active) == sorted(first["ids"])
        passed("rejected_wake_replacement_preserves_all_originals")

        status, rejected_edit = call('/users_wake/' + str(first['ids'][0]),
            {'target_start_time': time(18), 'target_end_time': time(19)}, method='PUT')
        assert status == 400 and rejected_edit['detail'] == 'periods_overlap'
        passed('history_edit_cannot_overlap_another_wake_window')
        status, active = call('/users_wake/?user_id=' + user_id)
        assert status == 200 and sorted(row['id'] for row in active) == sorted(first['ids'])
        passed('rejected_history_edit_preserves_original_records')

        status, _ = call("/users_sleep/replace_day", payload((time(23, -1), time(11))))
        assert status == 400
        status, active = call("/users_sleep/?user_id=" + user_id)
        assert status == 200 and len(active) == 1
        assert datetime.fromisoformat(active[0]["sleep_end_time"].replace("Z", "+00:00")).astimezone(zone).date() == day
        passed("rejected_sleep_replacement_preserves_original")

        pvt = {"user_id": user_id, "mean_rt": 320, "lapses": 1, "false_starts": 0,
               "kss_level": 3, "device": "Mobile PVT-B 3min",
               "test_at": datetime.now(timezone.utc).isoformat()}
        status, first = call("/users_pvt/", pvt)
        assert status == 200
        status, retry = call("/users_pvt/", pvt)
        assert status == 200 and first["id"] == retry["id"]
        passed("pvt_retry_is_idempotent")
        assert first['params_update']['p0_value'] == retry['params_update']['p0_value']
        assert retry['params_update']['trait_updated'] is False
        passed('pvt_retry_does_not_repeat_personalization_learning')
    finally:
        remaining = 0
        for endpoint in ("users_wake", "users_sleep"):
            status, records = call(f"/{endpoint}/?user_id=" + user_id)
            assert status == 200
            for record in records:
                status, _ = call(f"/{endpoint}/{record['id']}", method="DELETE")
                assert status == 200
            status, records = call(f"/{endpoint}/?user_id=" + user_id)
            assert status == 200
            remaining += len(records)
        assert remaining == 0
        passed("synthetic_source_records_cleaned")
        status, unavailable = call('/recommendations/?user_id=' + user_id)
        assert status == 400 and 'missing_fields' in unavailable['detail']
        passed('missing_inputs_cannot_resurface_old_recommendations')
        status, rotated = call("/auth/refresh", {"refresh_token": pair["refresh_token"]})
        assert status == 200
        status, _ = call("/auth/me")
        assert status == 401
        AUTH_TOKEN = rotated["access_token"]
        status, _ = call("/auth/me")
        assert status == 200
        passed("refresh_rotates_and_invalidates_previous_access")
        status, _ = call("/auth/logout", method="POST")
        assert status == 200
        status, _ = call("/auth/me")
        assert status == 401
        passed("logout_revokes_session")
    print(json.dumps({"passed_count": len(results), "results": results}))


if __name__ == "__main__":
    main()
